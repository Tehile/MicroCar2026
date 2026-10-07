/* 小车控制 · 比赛 —— `MAZE RUN RACE` 发车 + 比赛冲刺状态监视。
 *
 * ## 它与「无记忆走迷宫」页为什么是两页（上游 2.4.0）
 * 上游把迷宫发车命令扩成 `MAZE RUN [BLIND|RACE]`，两个目标走**两条不同的固件路径**：
 *   · `BLIND` = `LESSON_Blind8()`，从零开始无记忆趟迷宫（= 原来那句"实验 8"）；
 *   · `RACE`  = **先读 Flash 里的地图**（哪一格是终点、哪几条路是快路），
 *              据此**选冲刺档**（90_LOW / 45_LOW / MEDIUM / HIGH）再进主循环。
 * ⇒ 这是**两种跑法**，不是同一次跑的两个开关：现场要么"让它趟一遍迷宫"，
 *   要么"按地图冲一圈"。所以它们各占一张卡，而不是塞进一页的两个按钮。
 *
 * ## 这一页为什么几乎全是"只读"
 * 冲刺档位 `spurtMode` 是固件**只读镜像**（`mazeState`/`taskSt` 的位），
 * 上位机**设不了**它 —— 冲刺入口是比赛流程里"人工贴墙选档"那一段（刻意不动）。
 * ⇒ 这一页的职责是**把固件正在想什么说出来**，不是发指令。只有两个动作：
 *   【比赛发车】`MAZE RUN RACE`、【收车】/【复位】（与走迷宫页同一套收尾动作）。
 *   少写这句"我们设不了它"，现场会到处找那个不存在的下拉框。
 *
 * ## 为什么要专门监视 `frontNear` / `frontFar`（2.3.0 前墙双档）
 * 2.3.0 把前墙拆成互斥的两档：
 *   0 = 无墙 / 1 = 近端（贴脸，**该处理**）/ 2 = 远端（提前量，还没到）。
 * 恒等式：`frontNear | frontFar == wallFront`。比赛档靠"远了提前减速、近了马上转"，
 * 所以**这一档到底判对没有**，直接决定它会不会一头撞上去。
 * ⇒ 这一页把三个量并排画，旁边一行**当场自校验**这条恒等式；对不上立刻红字。
 *
 * ## 数据来源与三态（判不了 ≠ 全是 0）
 *   · `/mouse/maze/state` —— 状态机 + 位置 + `frontNear/Far/Both`；
 *   · `/mouse/meas/meta`  —— 冲刺档位与前墙的**码表**（静态，零设备访问）；
 *   · `/mouse/maze/record` —— 记录状态（本页发车也会自动开始记录）。
 * ⚠️ 老上游（≤2.2.1）根本没这些量：适配器会回 `spurt: null`。那时界面必须写
 *   "这台设备没有这个量"（升级上游），**不许**把 `null` 画成"未选 / 0"——
 *   "没选档"和"读不到档"是两件事，混起来现场会以为车已经准备好冲了。 */
C.mods.mod_race = (function () {
  let el, meta = null, dev = null, st = null, rec = null, sp = null, eno = null;
  let timer = null;

  /* 冲刺相关的原码 → 码表键。表来自 `/mouse/meas/meta`：
       meta.spurt.modes = {0:'未选',1:'90_LOW',2:'45_LOW',3:'MEDIUM',4:'HIGH'}
       meta.spurt.front = {0:'无墙',1:'近端(贴脸)',2:'远端(提前量)'}
     查不到就**回原码**（老上游没这两张表，数字照样印，只是没名字 —— 见纪律：不臆造）。 */
  function name(table, code) {
    if (table == null || code == null) return null;
    return table[String(code)] || null;
  }

  /* 0/1 标志位翻"是 / 否"。上游这几个是 `int`（0/1），不是 bool。 */
  function flag(v) {
    if (v == null) return '—';
    return v ? '是' : '否';
  }

  /* 一行 KV：`标签 <code>键名</code>` + 值。键名是上游帧里的原名，
     现场拿它去对上游文档时能直接对上（与「实时状态与参数设定」页同一口径）。 */
  function kv(key, label, txt, extraCls) {
    return '<b>' + C.util.esc(label) + ' <code>' + C.util.esc(key) + '</code></b>' +
      '<span' + (extraCls ? ' class="' + extraCls + '"' : '') + '>' + C.util.esc(txt) + '</span>';
  }

  function num(v) { return (v == null) ? '—' : String(v); }

  /* ------------------------------------------------------------ 骨架 */
  function mount(host) {
    el = host;
    el.innerHTML = `
      <div class="pane">
        <h4>比赛发车 <span class="hint" id="rc-msg"></span>
          <span><button class="mini primary" id="rc-run">比赛发车</button></span></h4>
        <div class="row wrap">
          <button class="mini" id="rc-mstop">收车</button>
          <button class="mini danger" id="rc-rescue">复位</button>
          <button class="mini" id="rc-refresh">刷新</button>
        </div>
        <div class="hint" id="rc-rec"></div>
      </div>

      <div class="pane" style="margin-top:8px">
        <h4>前墙双档 <span class="hint" id="rc-front-ok"></span></h4>
        <div class="hint">近端 = 贴脸，该处理；远端 = 提前量，还没到。
          两档互斥，且必有 frontNear | frontFar == wallFront。</div>
        <div class="kv" id="rc-front"></div>
      </div>

      <div class="pane" style="margin-top:8px">
        <h4>比赛冲刺状态 <span class="hint">固件只读镜像，上位机设不了</span></h4>
        <div class="kv" id="rc-spurt"></div>
      </div>

      <div class="pane" style="margin-top:8px">
        <h4>迷宫状态机</h4>
        <div class="kv" id="rc-state"></div>
        <div class="hint" id="rc-note"></div>
      </div>`;
    el.querySelector('#rc-run').onclick = function () { run(); };
    el.querySelector('#rc-mstop').onclick = function () { act('maze/stop', '收车'); };
    el.querySelector('#rc-rescue').onclick = function () { act('rescue', '复位'); };
    el.querySelector('#rc-refresh').onclick = function () { reload(); };
    startTick();
    loadMeta();
    reload();
  }

  /* 1 s 一次：状态/记录**全是零设备访问**（读本地缓存），所以可以跟着走。
     ⚠️ 不写 `setInterval` 在 `await` 之前的坑：这里 mount 里直接装，
     它不依赖任何异步结果（心跳纪律：任何会挡住定时器的东西都不许排在它前面）。 */
  const TICK_MS = 1000;
  function startTick() {
    if (timer) clearInterval(timer);
    timer = setInterval(function () {
      if (!el) return;
      if (C.needMouse()) return;      // 未连接：一个请求都不发
      loadState();
      loadRec();
    }, TICK_MS);
  }

  function reload() { loadState(); loadRec(); loadMeta(); }

  /* 码表（静态）：冲刺档位 + 前墙档位。未连接时也读得到，所以进来就读一次。 */
  async function loadMeta() {
    const d = await C.guard(function () { return C.api('/api/v1/mouse/meas/meta'); }, '读码表');
    meta = (d && d.spurt) || null;
    render();
  }

  async function loadState() {
    const d = await C.guard(function () { return C.api('/api/v1/mouse/maze/state'); });
    if (!d) return;
    st = d.state || null;
    /* ⚠️ `spurt` 三态：适配器在"老上游没这个方法"时回 `null`。
       这里**照原样**存下来，由 render 决定怎么显示 —— 不能折算成 0。 */
    sp = (d.spurt === undefined) ? null : d.spurt;
    if (sp === null && d.spurt === undefined) eno = 'no-field';
    render();
  }

  async function loadRec() {
    const d = await C.guard(function () { return C.api('/api/v1/mouse/maze/record'); });
    if (!d) return;
    rec = d;
    renderRec();
  }

  /* ------------------------------------------------------------ 渲染 */
  function renderFront() {
    const box = el.querySelector('#rc-front');
    const okbox = el.querySelector('#rc-front-ok');
    if (!box) return;
    if (!st) { box.innerHTML = '<span class="hint">无迷宫状态</span>'; if (okbox) okbox.textContent = ''; return; }
    const near = st.frontNear, far = st.frontFar, both = st.frontBoth, wf = st.wallFront;
    box.innerHTML =
      kv('frontNear', '近端(贴脸)', (st.frontName && near) ? (st.frontName + ' (' + near + ')') : num(near)) +
      kv('frontFar', '远端(提前量)', num(far)) +
      kv('frontBoth', '双档命中', num(both)) +
      kv('wallFront', '前墙(任一指)', num(wf));
    /* **当场自校验**恒等式（near | far == wallFront）与互斥（near & far == 0）。
       为什么值得在这页印出来：比赛档就是靠这一档决定"减速还是马上转"，
       判错就是撞墙；而这两个量都是**固件给的**，界面能做的只有"发现它自相矛盾时喊一声"。 */
    if (okbox) {
      if (near == null || far == null || wf == null) {
        okbox.textContent = '';
      } else {
        const ored = (near | far), anded = (near & far);
        if (ored !== wf) {
          okbox.textContent = '⚠ 前墙双档对不上：frontNear|frontFar=' + ored +
            ' ≠ wallFront=' + wf + '（固件这一档的判定有问题）';
        } else if (anded !== 0) {
          okbox.textContent = '⚠ 前墙两档同时命中（应互斥）：frontNear&frontFar=' + anded;
        } else {
          okbox.textContent = '恒等式成立：' + near + ' | ' + far + ' = ' + wf;
        }
      }
    }
  }

  function renderSpurt() {
    const box = el.querySelector('#rc-spurt');
    if (!box) return;
    /* ⚠️ 三态分岔要先判：
         · `sp === null` 且**字段在**（老上游） ⇒ "这台设备没有这些量"；
         · `sp === null` 但字段**不在**（本层太老/别的原因） ⇒ 同样说读不到，指向上游版本；
         · `!sp` 且无 state ⇒ 未连接。
       把 null 画成"未选"是最坏的一种错 —— 那是**在替固件宣布"它没在冲刺"**。 */
    if (!sp) {
      if (!st) { box.innerHTML = '<span class="hint">未连接电脑鼠</span>'; return; }
      box.innerHTML = '<span class="hint">读不到比赛冲刺状态（上游 2.3.0 起才有）</span>';
      return;
    }
    const modes = meta && meta.modes;
    const modeTxt = (function () {
      if (sp.mode == null) return '—';
      const n = (sp.modeName && sp.modeName !== '?') ? sp.modeName : name(modes, sp.mode);
      return n ? (n + ' (' + sp.mode + ')') : String(sp.mode);
    })();
    box.innerHTML =
      kv('spurtMode', '冲刺档位', modeTxt) +
      kv('spurtFlag', '冲刺标志', flag(sp.spurtFlag)) +
      kv('goOrBack', '去 / 回', sp.goOrBack == null ? '—'
          : (sp.goOrBack ? '回（返程）' : '去（去程）') + ' (' + sp.goOrBack + ')') +
      kv('spurtTestFlag', '冲刺试探', flag(sp.spurtTestFlag)) +
      kv('objectGoToFlag', '物体去向（回坐标中）', flag(sp.objectGoToFlag)) +
      kv('goaheadnum', '前冲计数', num(sp.goaheadnum)) +
      kv('fastwayCount90', '90° 快路计数', num(sp.fastwayCount90));
  }

  function renderState() {
    const box = el.querySelector('#rc-state');
    if (!box) return;
    if (!st) { box.innerHTML = '<span class="hint">未运行</span>'; return; }
    box.innerHTML =
      kv('mazeTask', '迷宫任务', st.taskName ? (st.taskName + ' (' + st.task + ')') : num(st.task)) +
      kv('motionTask', '动作', st.motionName ? (st.motionName + ' (' + st.motion + ')') : num(st.motion)) +
      kv('dir', '朝向', st.dirName ? (st.dirName + ' (' + st.dir + ')') : num(st.dir)) +
      kv('posX/posY', '位置', num(st.posX) + ', ' + num(st.posY)) +
      kv('goalX/goalY', '目标', num(st.goalX) + ', ' + num(st.goalY)) +
      kv('roadName', '当前路口', st.roadName ? (st.roadName + ' · ' + (st.roadKind || '')) : '—');

    /* 任务机处在什么位置，决定"接下来该点哪" —— 门控只报条件，故障必须给可照做清单。
       这四档对应上游 2.4.0 的三种发车失败与"已在跑"。 */
    const note = el.querySelector('#rc-note');
    if (note) {
      const t = st.task;
      if (t === 0) {
        note.textContent = '任务机 WAIT（待机）⇒ 可以点【比赛发车】。';
      } else if (t === 3 || t === 4 || t === 5) {
        note.textContent = '任务机处在冲刺态 ⇒ 正在按地图冲。急停随时可用。';
      } else if (t === 1) {
        note.textContent = '任务机 START（刚发车）⇒ 正在读地图选档。';
      } else {
        note.textContent = '任务机 SEARCH ⇒ 车正在走。要停下：先点【收车】；'
          + '收车不生效（还在 SEARCH）就点【复位】。';
      }
    }
  }

  function renderRec() {
    const box = el.querySelector('#rc-rec');
    if (!box) return;
    if (!rec) { box.textContent = ''; return; }
    if (rec.recording) {
      box.textContent = '● 记录中 ' + rec.frames + ' 帧 · 已 ' +
        C.util.fmt((rec.duration_ms || 0) / 1000, 1) + ' s';
      return;
    }
    const last = rec.last;
    if (!last) { box.textContent = '未在记录'; return; }
    if (last.ok === false) {
      box.textContent = '上次没有保存成 · ' + (last.note || '一帧都没收到');
      return;
    }
    box.textContent = '上次保存 ' + (last.name || '(未知文件名)') + ' · ' +
      (last.frames == null ? '?' : last.frames) + ' 帧';
  }

  function render() {
    if (!el) return;
    renderFront();
    renderSpurt();
    renderState();
    renderRec();
  }

  /* ------------------------------------------------------------ 动作 */
  async function run() {
    /* ⚠️ 与走迷宫页同一套错误分支（不用 `C.guard`）：发车失败有三种、
       处置各不相同，toast 会消失，所以要在 `#rc-msg` 上留一句"下一步"。
       新增的第四种：老上游不支持 RACE（后端 400 说明）。 */
    let d = null;
    const m0 = el.querySelector('#rc-msg');
    if (C.needMouse()) { if (m0) m0.textContent = '未连接电脑鼠'; return; }
    try {
      d = await C.api('/api/v1/timesync/maze_run', { method: 'POST', body: { target: 'RACE' } });
    } catch (e) {
      const msg = String((e && e.message) || '');
      if (m0) {
        if (msg.indexOf('电池') >= 0 || msg.indexOf('LOWBATT') >= 0) {
          m0.textContent = '发车被低电闸拦下（命令没发出去）⇒ 先给车充电。';
        } else if (msg.indexOf('急停') >= 0 || msg.indexOf('ESTOP') >= 0) {
          m0.textContent = '发车失败：急停锁存中 ⇒ 先解锁急停。';
        } else if (msg.indexOf('BUSY') >= 0) {
          m0.textContent = '发车失败：已在跑迷宫 ⇒ 先点【收车】。';
        } else if (msg.indexOf('RACE') >= 0 || msg.indexOf('target') >= 0) {
          m0.textContent = '这台设备不支持比赛发车（上游早于 2.4.0）⇒ 只能用【无记忆走迷宫】页。';
        } else {
          m0.textContent = '发车失败：' + msg;
        }
      }
      C.toast('比赛发车：' + msg, 'bad', 22000);
      console.error(e);
      loadState();
      return;
    }
    if (m0) m0.textContent = '已发车（RACE）' + (d.record_note || '');
    loadRec();
    loadState();
  }

  async function act(path, label) {
    const d = await C.guard(function () {
      return C.api('/api/v1/mouse/' + path, { method: 'POST', body: {} });
    }, label);
    if (d) {
      C.toast(label + '已下发', path === 'rescue' ? 'warn' : 'ok');
      /* 收车/复位之后状态会变，催一次；复位约 3 s 才有新帧，所以不指望立刻变。 */
      loadState();
      loadRec();
    }
  }

  return {
    mount: mount,
    activate: function () { startTick(); reload(); },
    onMsg: function (m) {
      /* 帧驱动：冲刺量就在帧里，所以跟着帧重渲染 —— 这页是"监视"，
         秒级延迟就够，但帧一到就该更新（比赛档读数是随帧变的）。 */
      if (m.type === 'mouse.telemetry') {
        const t = C.state.telemetry || {};
        const u = t.unified;
        if (u) {
          /* 直接用帧里的值（与后端 `spurt()` 同源），避免 1 s 轮询把冲刺标志拖慢。
             缺字段就退回流到的 `sp`（三态不变）。 */
          if (u.spurtMode !== undefined) {
            sp = {
              mode: u.spurtMode, modeName: u.spurtModeName,
              spurtFlag: u.spurtFlag, goOrBack: u.goOrBack,
              spurtTestFlag: u.spurtTestFlag, objectGoToFlag: u.objectGoToFlag,
              goaheadnum: u.goaheadnum, fastwayCount90: u.fastwayCount90,
            };
          }
          if (u.frontNear !== undefined && st) {
            st = Object.assign({}, st, {
              frontNear: u.frontNear, frontFar: u.frontFar, frontBoth: u.frontBoth,
              wallFront: u.wallFront,
            });
          }
          render();
        }
      }
    },
    onHealth: function () {
      if (!el) return;
      const why = C.needMouse();
      ['rc-run', 'rc-mstop', 'rc-refresh'].forEach(function (id) {
        C.gate(el.querySelector('#' + id), why);
      });
      /* 复位**不门控**：恰恰是连不上、卡死的时候最需要它（与顶栏急停一致）。 */
      if (why) { st = null; sp = null; rec = null; render(); }
    },
  };
})();
