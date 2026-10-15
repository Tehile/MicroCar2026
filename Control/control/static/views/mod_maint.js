/* 小车控制 · 维护与自检 —— 上游 v2.7 把**按键菜单**搬到了链路上（`CAL` 家族 6 个
 * 立即-ACK 子命令：擦红外标定 / 红外自检 / 传感器明细 / 红外 8 值分步标定 /
 * Flash 概览 / 清空路径数据）。
 *
 * ## 为什么单独占一张卡
 * 现有三张卡是「调好 → 跑迷宫 → 比赛」的现场顺序；维护是**赛前与故障时的第 0 件事**，
 * 塞进任何一张卡都会串味。所以它自己一张卡，内部做成 `C.parts.maint` 区块 ——
 * 以后想换宿主（比如并回「实时状态与参数设定」）只改挂载那一行。
 *
 * ## 三个"照直觉写就会错"的点
 *  1. **擦除类是不可逆的**，而上游登记表要求显式 `confirm=true` 才放行（不带就回
 *     `NEED_CONFIRM` 且**完全不碰链路**）。所以界面上这两个按钮做**就地二次确认**，
 *     并且把"擦什么、擦完会怎样"写在场：不是弹一个"确定吗"就完了。
 *  2. **红外 8 值标定就是改版十七删掉的"红外逐点标定"**（摆位文字与旧 `IR_STEPS`
 *     一字不差）。上游 v2.7 给它配了一道新闸：第 1 步前红外页必须为空（全 FF），
 *     已标定过就得**先擦除**。界面上把这条写在明处，否则现场只会看到"第 1 步被拒"。
 *  3. **`BLOCK`（Flash 概览）不是纯只读**：固件先把 Flash 墙图重载进内存 ⇒ 之后
 *     「无记忆走迷宫」页显示的是 Flash 里的图。后端已经把这句 `note` 给出来，
 *     界面照显示，别让一个叫"概览"的按钮默默改掉另一个页面。
 *
 * ## 能力位缺失 ⇒ 整块不画
 * `menu_remote` 为假（老上游）时，这里**不画任何按钮** —— 画出来只会让人点了报错。
 *
 * ## 数据来源
 *   · `GET  /api/v1/caps`              —— 能力位 + 可远程项 `maint_items` + 摆位原文
 *                                          `maint_steps`（**都从上游现取**，本层不写死）
 *   · `POST /api/v1/mouse/maint/{item}` —— 六项动作；`{step}` / `{confirm}`
 * 门控：未连鼠 或 车在跑（`mazeTask` 非 0）时整块禁用（固件运动中一律 `ERR_BUSY`）。 */
C.parts = C.parts || {};
C.parts.maint = (function () {
  let el = null, caps = null, built = false;
  let capStep = 1, capDone = 0, capLast = null;
  let armed = null;                 // 就地二次确认的武装状态

  function q(sel) { return el ? el.querySelector(sel) : null; }

  /* ------------------------------------------------------------ 骨架 */
  function mount(host) {
    el = host;
    /* ⚠️ `built` **每次挂载都要归零**：上一轮的 DOM 已被下面这行换掉，而 `build()`
       见到 `built=true` 会直接 return ⇒ 骨架没了、页面空白（且不报错）。 */
    built = false;
    el.innerHTML = '<div id="mt-wrap"><div class="hint">读取能力…</div></div>';
    loadCaps();
  }

  function activate() {
    /* 能力位**每次切回本页都重读**：`menu_remote` 在装载期就定了，但"刚才在哪一页"
       期间可能换了上游/重启过服务。读失败保持旧值（不猜"支持"）。 */
    loadCaps();
  }

  async function loadCaps() {
    const d = await C.guard(function () { return C.api('/api/v1/caps'); }, '读能力');
    if (!d || !d.mouse) return;
    caps = d.mouse;
    build();
  }

  function build() {
    const wrap = q('#mt-wrap');
    if (!wrap) return;
    if (!caps || !caps.menu_remote) {
      wrap.innerHTML = '<div class="hint">这份上游没有按键菜单远程化' +
        '（需要 RemoteControl 2.7.0 以上）。</div>';
      built = false;
      return;
    }
    if (built) { renderIrcap(); return; }       // 不重复搭骨架（会丢现场状态）
    built = true;
    wrap.innerHTML = `
      <div class="pane">
        <h4>诊断 <span class="hint" id="mt-msg"></span></h4>
        <div class="row wrap">
          <button class="mini" id="mt-irself">红外自检</button>
          <button class="mini" id="mt-sensor">传感器明细</button>
          <button class="mini" id="mt-block">Flash 概览</button>
        </div>
        <div class="kv" id="mt-diag"></div>
        <div class="hint" id="mt-diag-note"></div>
      </div>

      <div class="pane" style="margin-top:8px">
        <h4>红外标定 <span class="hint" id="mt-ircap-msg"></span></h4>
        <div class="hint">4 个摆位依次采一遍（共 8 个值）。要求按顺序来；
          若这一页里已有标定值，先点【擦除红外标定】。</div>
        <div class="hint" id="mt-step"></div>
        <div class="row wrap">
          <button class="mini primary" id="mt-ircap-next">开始标定</button>
          <button class="mini" id="mt-ircap-restart">回到第 1 步</button>
          <button class="mini danger" id="mt-ir-erase">擦除红外标定</button>
        </div>
        <div class="kv" id="mt-ircap"></div>
      </div>

      <div class="pane" style="margin-top:8px">
        <h4>清空路径数据 <span class="hint" id="mt-erase-msg"></span></h4>
        <div class="hint">擦除迷宫与路径 6 页（块 / 点 / 去径 / 回径 / 去径90 / 回径90）。
          历史录制区不受影响。</div>
        <div class="row wrap">
          <button class="mini danger" id="mt-erase-all">清空路径数据</button>
        </div>
      </div>`;
    q('#mt-irself').onclick = function () { diag('IRSELF'); };
    q('#mt-sensor').onclick = function () { diag('SENSOR'); };
    q('#mt-block').onclick = function () { diag('BLOCK'); };
    q('#mt-ircap-next').onclick = function () { irCapNext(); };
    q('#mt-ircap-restart').onclick = function () { capStep = 1; capLast = null; capDone = 0; note('IRCAP', '已回到第 1 步。'); renderIrcap(); };
    q('#mt-ir-erase').onclick = function () {
      armErase(this, '擦除红外标定', function () {
        note('IRCAP', '擦除红外标定：红外 8 值立即作废，要重新标一次。');
        doIt('ERASEIR', { confirm: true }, '擦除红外标定');
      });
    };
    q('#mt-erase-all').onclick = function () {
      armErase(this, '清空路径数据', function () {
        note('ERASEALL', '正在擦除迷宫与路径 6 页。');
        doIt('ERASEALL', { confirm: true }, '清空路径数据');
      });
    };
    renderIrcap();
    if (C.state && C.state.health) onHealth();
  }

  /* ------------------------------------------------------------ 就地二次确认 */
  /* 为什么不用浏览器弹窗：现场戴着手套点两次，比在一个飘在中间的框上点"确定"
     更不容易点错；而且按钮自己会变成"再点一次确认（不可逆）"，看得见当前处在确认态。 */
  function disarm() {
    if (!armed) return;
    armed.btn.textContent = armed.label;
    armed.btn.classList.remove('on');
    clearTimeout(armed.timer);
    armed = null;
  }

  function armErase(btn, label, cb) {
    if (!btn) return;
    if (armed && armed.btn === btn) { disarm(); cb(); return; }
    disarm();
    armed = { btn: btn, label: label, timer: null };
    btn.classList.add('on');
    btn.textContent = '再点一次确认擦除（不可逆）';
    armed.timer = setTimeout(disarm, 8000);
  }

  /* ------------------------------------------------------------ 动作 */
  async function doIt(item, body, label) {
    if (C.needMouse()) { note(item, '未连接电脑鼠。'); return null; }
    let d = null;
    try {
      d = await C.api('/api/v1/mouse/maint/' + item, { method: 'POST', body: body || {} });
    } catch (e) {
      const t = String((e && e.message) || '请求失败');
      /* 409 + type=need_confirm ⇒ 上游登记表把不可逆操作拦下了（命令没发出去）。
         这不是错误操作，是"还差一次确认"，所以文案要写清。 */
      if (e && e.type === 'need_confirm') {
        note(item, '需要确认：' + t);
      } else {
        note(item, t);
      }
      C.toast(label + '：' + t, 'bad', 18000);
      return null;
    }
    const r = (d && d.result) || {};
    const data = (r.data) || {};
    if (r.ok) {
      const extra = data.note ? (' ' + data.note) : '';
      note(item, label + '完成。' + extra);
      C.toast(label + '完成', 'ok');
    } else {
      /* 上游拒绝码原样端出（协议词），后面跟后端给的**可照做**下一步。 */
      note(item, (data.hint || ('设备回了 ' + (r.status || '空状态') + '。')));
      C.toast(label + '：' + (r.status || '失败'), 'bad', 18000);
    }
    return d;
  }

  async function diag(item) {
    const label = { IRSELF: '红外自检', SENSOR: '传感器明细', BLOCK: 'Flash 概览' }[item] || item;
    const d = await doIt(item, {}, label);
    const box = q('#mt-diag');
    const nb = q('#mt-diag-note');
    if (!d || !box) return;
    const r = d.result || {};
    const data = r.data || {};
    if (!r.ok) { box.innerHTML = ''; return; }
    if (item === 'IRSELF') {
      box.innerHTML = kv('irL', '左红外', num(data.ir_l)) + kv('irR', '右红外', num(data.ir_r)) +
        kv('ref', '车心到墙参考', num(data.ref));
    } else if (item === 'SENSOR') {
      box.innerHTML = '<span class="hint">传感器明细已打印到底栏的集成栏（来源=电脑鼠）。</span>';
    } else {
      box.innerHTML = kv('path45', '去 / 回路径项数', num(data.path45) + ' / ' + num(data.path90)) +
        kv('ir_valid', '红外有效值', num(data.ir_valid) + ' / 8');
    }
    if (nb) nb.textContent = data.note || '';
  }

  async function irCapNext() {
    const steps = (caps && caps.maint_steps) || [];
    if (!steps.length) { note('IRCAP', '这台上游没给出摆位表，无法标定。'); return; }
    const want = capStep;
    const d = await doIt('IRCAP', { step: want }, '红外标定第 ' + want + ' 步');
    if (!d) return;
    const r = d.result || {};
    if (!r.ok) {
      /* 第 1 步被拒 = 页里已有标定值（上游的新安全闸）⇒ 直接告诉他先擦。 */
      if (want === 1 && r.status === 'ERR_BUSY') {
        note('IRCAP', '这一步被拒：红外页里已有标定值。先点【擦除红外标定】，再回来标。');
      }
      return;
    }
    capLast = (r.data) || {};
    capDone = want;
    capStep = (want >= steps.length) ? 1 : want + 1;
    if (capDone >= steps.length) {
      note('IRCAP', '4 个摆位都采完，红外 8 值已写入。');
    }
    renderIrcap();
  }

  /* ------------------------------------------------------------ 渲染 */
  function kv(key, label, txt) {
    return '<b>' + C.util.esc(label) + ' <code>' + C.util.esc(key) + '</code></b>' +
      '<span>' + C.util.esc(txt) + '</span>';
  }

  function num(v) { return (v == null) ? '—' : String(v); }

  function renderIrcap() {
    const steps = (caps && caps.maint_steps) || [];
    const btn = q('#mt-ircap-next');
    const st = q('#mt-step');
    const box = q('#mt-ircap');
    if (!btn || !st) return;
    if (!steps.length) { btn.textContent = '开始标定'; st.textContent = ''; return; }
    const cur = steps[Math.min(capStep, steps.length) - 1];
    btn.textContent = (capDone === 0
      ? '开始标定'
      : (capDone >= steps.length ? '重新标一次' : '下一步（第 ' + capStep + ' 步）'));
    st.textContent = '当前该摆：第 ' + cur.no + ' / ' + steps.length + ' 步，' +
      cur.name + '（' + cur.desc + '）';
    if (box) {
      box.innerHTML = (capDone === 0)
        ? '<span class="hint">还没开始</span>'
        : (kv('a / b', '上一步回读', num(capLast && capLast.a) + ' / ' + num(capLast && capLast.b)) +
           kv('step_done', '已完成到第', num(capDone) + ' 步'));
    }
  }

  function note(item, text) {
    const id = (item === 'IRCAP' || item === 'ERASEIR') ? '#mt-ircap-msg'
      : (item === 'ERASEALL' ? '#mt-erase-msg' : '#mt-msg');
    const n = q(id);
    if (n) n.textContent = text;
  }

  /* ------------------------------------------------------------ 门控 */
  /* 车在跑时固件一律 `ERR_BUSY` ⇒ 事先别让他点。
     ⚠️ 判据取 `mazeTask >= 2`（SEARCH 起）：上游码表 `0=WAIT / 1=START / 2=SEARCH…`，
     `START` 只是发车那一瞬的过渡态，把它算成"在跑"会让整块在待机时也点不动。
     **这条阈值必须与后端同源** ——`routes.py` 的 `_maze_decisions` 里
     `run = int(task) >= 2`（注释原文：0 WAIT / 1 START 是待命），`mod_maze8.js` 的
     `runState()` 也按这一条判。三处是同一个判据，改一处就要一起改。 */
  function busyWhy() {
    const u = ((C.state || {}).telemetry || {}).unified;
    if (u && Number(u.mazeTask) >= 2) return '车正在跑迷宫，先点【收车】，等它停稳';
    return null;
  }

  function why() { return C.needMouse() || busyWhy(); }

  function onHealth() {
    if (!el || !built) return;
    const w = why();
    ['mt-irself', 'mt-sensor', 'mt-block', 'mt-ircap-next', 'mt-ircap-restart',
      'mt-ir-erase', 'mt-erase-all'].forEach(function (id) {
      C.gate(q('#' + id), w);
    });
    if (w) disarm();          // 断连/在跑时把确认态撤掉，免得回来一点就擦
  }

  return {
    mount: mount,
    activate: activate,
    onHealth: onHealth,
    onMsg: function (m) {
      /* 帧到了就重算一次门控（车"开始跑/停下"不产生 health 广播）。 */
      if (m.type === 'mouse.telemetry') onHealth();
    },
  };
})();

/* 卡片视图：把上面那个区块挂进来。**换宿主只改这一处**。 */
C.mods = C.mods || {};
C.mods.mod_maint = {
  mount: function (host) { C.parts.maint.mount(host); },
  activate: function () { C.parts.maint.activate(); },
  onHealth: function () { C.parts.maint.onHealth(); },
  onMsg: function (m) { C.parts.maint.onMsg(m); },
};
