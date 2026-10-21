/* Control 前端主控：路由 / WS / 状态 / 设备窗口。
   刻意不引入任何框架：这是个**比赛现场的调试工具**，现场没有网，
   "改一行界面"不能变成"先 npm install"。 */

(function () {
  const S = C.state;
  S.health = null;
  S.caps = null;
  S.session = null;
  S.telemetry = null;
  S.textLines = [];
  S.serviceLog = [];
  S.ws = null;
  /* ⚠️ 这个必须显式给初值：`mountViews()` 里第一件事就是 `S.views[name] = ...`，
     而 `C.state` 只是个空对象 —— 少了这一行，赋值会抛 TypeError，forEach 当场中断，
     结果就是"页面看着打开了，但视图只挂载了一个、页签一个都点不动"。
     而它**不会在服务端日志里留下任何痕迹**。真·踩过一次。 */
  S.views = {};
  /* 子系统模块（与 `S.views` 分开注册）：它们是"可验证的最小实验"，
     不属于任何一栏，而是挂在某个子系统的子页面里。 */
  S.mods = {};
  S.open = { sys: '', mid: '' };   // 当前打开的模块界面（mid = '' = 停在入口页）

  /* 页面归属：每个视图属于哪一栏。**这个表是布局的唯一真相** ——
     index.html 的容器、这里的分派、以及 tools/browser_check.py 的断言都读它。
     为什么要两栏：这是两套系统的整合层，「场地（相机侧）」与「小车控制」
     是并列的两件事，现场经常要**同时**看着两边（车在跑、画面在场地上）。
     全局整合的东西（准备清单 / 会话 / ⑧数据）不属于任何一路，进底部可收纳栏。 */
  const RAIL_OF = {
    params: 'mouse', depart: 'mouse',
    exp: 'mouse', scope: 'mouse', console: 'mouse',
    data: 'dock'
  };
  const RAIL_NAME = { mouse: '小车 · 控制', dock: '整合' };
  /* 改版二十四：相机两页（① 场地 / ② 采集）随手机相机整块下线，
     位置数据改由「视频复盘」给 ⇒ 页签 7 → 5，加上底部「数据」= **6**。 */
  const TABS = ['params', 'depart', 'exp', 'scope', 'console'];
  /* 页签数 = index.html 两栏 nav 里的按钮数 = TABS.length。
     三处必须一致：index.html 的按钮、这里的表、tools/browser_check.py 的 TABS。
     写死数字是有意的 —— 加页面时忘了改任何一处都会**当场报出来**，
     而不是让某一页静默地变成空白（那种问题在现场极难定位）。 */
  const EXPECTED_VIEWS = 6;

  /* ------------------------------------------------------------ 子系统 / 模块
     主区是**两级**：入口页（#nav）**只做导航**（子系统分组 + 模块卡片，不放控件）；
     点卡片**单开一个整屏的模块界面**（#mod）做那一个实验，点【← 返回】回入口。
     **这个表是子系统与模块的唯一真相**：分组标题、卡片、模块界面头部、
     以及 `tools/browser_check.py` 的断言都读它。加模块时改这里 + 注册 `C.mods` 即可。
     `sub` 是这一路的上游来源（仅作标签，不是解释）；
     「综合运动」的卡片后面不是单个 JS 视图 —— 而是原有的两栏九页（推迟实现，先保留），
     所以它的 `mid` 固定为 `@all`，对应 index.html 里那个 `.view[data-mod="@all"]`。

     ## 2026-10-07（改版十二）：小车控制 四卡 → **三卡**
     上游 RemoteControl **2.4.0** 把编译档位由 5 档压到 2 档，并**删掉了课程调参整层**
     （`lesson_lab.c` 751 行、`host/api/lesson.py` 865 行、`Event.LESSON`、课程 14 项参数、
     `lesson.start/stop/status` 三条命令）。同时补了两件本来就该有的事：
     **`MAZE RUN [BLIND|RACE]`**（比赛发车）与**前墙双档 + 比赛冲刺状态进帧**（2.3.0）。
     于是「小车控制」按**现场真会做的三件事**重排，与上游的档位/入口一一对应：

       ① 实时状态与参数设定  —— 接远程档的全部：监视 + 参数读写 + 手动动一下，
                                 是"把车调好"的那一段（原四张卡里除了走迷宫都在这里）；
       ② 无记忆走迷宫        —— `MAZE RUN BLIND`（= 实验八剖面），原「实验 8」改名；
       ③ 比赛                —— `MAZE RUN RACE`（读 Flash 地图选档/定冲刺）+ 比赛状态量监视。

     ⚠️ 为什么**不**再单列「运动控制」「校准例程」两张卡：
       它们是**同一个现场阶段**（把车调好）里的两个动作，与参数设定共用同一组门控
       （"连上电脑鼠"），拆成三张卡只是让"调车"这件事在导航上多两级。合成一页之后
       顺序就是现场顺序：先看状态 → 再调参数 → 需要就手动动一下。
       而「走迷宫」与「比赛」是**两种跑法**，是并列的，所以它们各占一张卡。 */
  const SYS_DEF = [
    { id: 'mouse', name: '小车控制', sub: 'RemoteControl', mods: [
      { id: 'mod_params', name: '实时状态与参数设定',
        desc: '统一帧实时读数 + 参数表 · 手动运动' },
      { id: 'mod_race', name: '比赛', desc: '比赛发车（读地图选档）+ 冲刺状态 + 迷宫重构' },
      { id: 'mod_maint', name: '维护与自检',
        desc: '按键菜单远程化 · 诊断 / 红外标定 / 清空路径' },
    ] },
    /* 改版二十四：**相机**（PhoneCamera / 8765 / 实时回传）与**定位两张调试卡**
       （定位测试 / 迷宫结构解析）整组下线 —— 位置数据不再来自实时帧流，
       而是"一段视频 → Auto Locate 会话解析 → 轨迹"。所以这一组换成「视频复盘」两卡：
       一张起解析、一张看轨迹（拍板①：调试卡不保留成"读产物"的页面）。 */
    { id: 'vid', name: '视频复盘', sub: 'Auto Locate', mods: [
      { id: 'mod_video', name: '视频会话', desc: '选视频 → 解析 → 产物与帧统计' },
      { id: 'mod_track', name: '轨迹', desc: '会话轨迹 + 时间轴自洽状态' },
    ] },
    { id: 'all', name: '综合运动', sub: '整体操作', mods: [
      { id: '@all', name: '综合运动台', desc: '单栏五页 + 数据 · 小车控制' },
    ] },
  ];
  /* mid → 卡片名：模块界面头部要用它，所以从 SYS_DEF 现推，避免第二处真相。 */
  const MOD_NAME = {};
  SYS_DEF.forEach(function (s) {
    s.mods.forEach(function (m) { MOD_NAME[m.id] = m.name; });
  });

  /* ------------------------------------------------------------ API */
  C.api = async function (path, opts) {
    opts = opts || {};
    const init = { method: opts.method || 'GET', headers: {} };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    let r;
    try {
      r = await fetch(path, init);
    } catch (e) {
      throw new Error('请求失败：' + e.message);
    }
    let j = null;
    const txt = await r.text();
    try { j = txt ? JSON.parse(txt) : null; } catch (e) { /* 非 JSON */ }
    if (!r.ok) {
      const msg = (j && j.error && j.error.message) || txt || ('HTTP ' + r.status);
      const err = new Error(msg);
      err.status = r.status;
      err.type = j && j.error && j.error.type;
      throw err;
    }
    return j && ('data' in j) ? j.data : j;
  };

  /* toast 去重是**必须**的：九个视图挂载时都会各自去拉一次"相机状态/会话表/参数"，
     于是同一条「相机服务没在跑」会在一秒内被推 6 遍，把真正要紧的那条挤没。
     同样的文字只留一条并**续命**，不重复堆。 */
  C.toast = function (msg, kind, ms) {
    const box = document.getElementById('toast');
    const text = String(msg == null ? '' : msg);
    const old = Array.from(box.children).find(function (c) {
      return c.textContent === text;
    });
    if (old) {
      clearTimeout(old._ctlTimer);
      old._ctlTimer = setTimeout(function () { old.remove(); },
        ms || (kind === 'bad' ? 12000 : 6000));
      return old;
    }
    const d = document.createElement('div');
    d.className = kind || '';
    d.textContent = text;
    box.appendChild(d);
    d._ctlTimer = setTimeout(function () { d.remove(); }, ms || (kind === 'bad' ? 12000 : 6000));
    return d;
  };

  C.guard = async function (fn, label) {
    try { return await fn(); }
    catch (e) {
      C.toast((label ? label + '：' : '') + e.message, 'bad');
      console.error(e);
      return null;
    }
  };

  /* ------------------------------------------------------------ 准备度门控 */
  /* 「先手动把设备连好、再测试」不是一句提示，而是一道**闸**：
     所有需要某一路的动作（发车 / 实验 / 波形 / 快捷动作 / 采集）
     统一在这里问一句。各视图各写一遍判断的下场，是"某页说能跑、某页说不能"。
     返回 `null` = 放行；返回字符串 = 拦住，字符串就是要照做的下一步。 */
  C.needMouse = function () {
    const m = (S.health || {}).mouse || {};
    if (m.connected) return null;
    if (m.available === false) return '鼠标模块不可用';
    // 上游有一条"能打开但对端不存在"的串口会把命令锁钉死（见 mouse.py 的 connect），
    // 那时连什么都连不上，且表象像网络问题 —— 这里如实把这句端出来。
    if (m.connect_in_flight) return '上一次连接未返回，请重启服务';
    return '未连接电脑鼠';
  };
  /* 把门控结果落到按钮上：禁用 + 把"该做什么"写进 title（悬停就能看到） */
  C.gate = function (btn, reason) {
    if (!btn) return false;
    btn.disabled = !!reason;
    if (reason) { btn.title = reason; } else { btn.removeAttribute('title'); }
    return !!reason;
  };

  /* ------------------------------------------------------------ 准备清单 */
  /* 清单只读、且**按"要动手做的"组织**：每一项 = 一个物理前提 + 一句"去哪点"。
     放在底部整合栏里，因为它是**三路合起来**的结论（哪一路缺都不算齐），
     而"怎么把缺的补上"由顶栏那四个设备窗口负责。 */
  function renderPrep() {
    const h = S.health || {};
    const rd = h.readiness;
    const box = document.getElementById('prep-list');
    const sum = document.getElementById('prep-summary');
    if (!box || !sum) return;
    if (!rd) { sum.textContent = '读取中…'; return; }
    sum.className = 'tag ' + (rd.ready ? 'ok' : 'bad');
    sum.textContent = rd.ready ? '就绪'
      : (rd.can_record ? (rd.blockers.length + ' 项未连接') : '未连接');
    /* 每项只显示三样东西：名字、状态、以及未就绪时**该做什么**。
       `manual`（物理前提）与 `sub`（后果说明）不进界面 —— 它们属于 README。
       颜色区分严重程度：拦住记录 = 红，不拦但未就绪 = 黄。 */
    box.innerHTML = rd.items.map(function (it, i) {
      const cls = it.ok ? 'ok' : (it.blocking ? 'bad' : 'soft');
      return '<div class="it ' + cls + '">' +
        '<div class="hd"><span class="dot"></span>' + (i + 1) + '. ' +
        C.util.esc(it.name) +
        (it.blocking ? '' : '<span class="nb">不拦记录</span>') +
        '<span class="st">' + C.util.esc(it.state || '') + '</span></div>' +
        (it.ok ? '' : '<div class="ac">' + C.util.esc(it.action || '') + '</div>') +
        '</div>';
    }).join('');
  }

  /* ------------------------------------------------------------ 设备窗口 */
  /* 顶栏那几个标签点开的就是这一个窗口，只是换面板。
     为什么合成一个窗口而不是几个弹窗：**面板里有三样东西是同一套写法**
     （读回状态 / 一个"连接"按钮 / 一句"下一步点哪"），分开写迟早会写岔，
     而且门控（能不能点）只应该有一处判断。

     改版二十四：相机与场地定位两路随手机相机下线（位置数据改由视频会话给），
     所以这里只剩**电脑鼠**与**时间基**两块。 */
  const DEV_TITLE = { mouse: '电脑鼠', clock: '时间基' };
  let devKind = '';

  function openDev(kind) {
    devKind = kind;
    const m = document.getElementById('dev-modal');
    m.classList.add('on');
    document.getElementById('dev-title').textContent = DEV_TITLE[kind] || '设备';
    document.querySelectorAll('.dev-pane').forEach(function (p) {
      p.classList.toggle('on', p.dataset.pane === kind);
    });
    renderDev();
    if (kind === 'mouse') loadTargets();
    if (kind === 'clock') refreshClock();
  }
  C.openDev = openDev;

  function closeDev() {
    document.getElementById('dev-modal').classList.remove('on');
    devKind = '';
  }

  function renderDev() {
    renderDevMouse();
    renderDevClock();
  }

  /* 一行 KV：`[名, 值]`，第三项传 'raw' 表示**这个值原样来自上游/服务端**
     （如 HostLinkError 的原文，里面可能有上游自己的口语与括号措辞 ——
     PhoneCamera 的 test_interop.py 甚至在断言那句原文）。
     ⇒ 打上 `data-raw`，文案判据（B13/B23）跳过它：**判据只管我们自己写的界面文案。** */
  const kv = function (box, rows) {
    if (!box) return;
    box.innerHTML = rows.filter(function (r) { return r && r[1] !== undefined && r[1] !== null; })
      .map(function (r) {
        return '<b>' + C.util.esc(r[0]) + '</b>' +
          '<span' + (r[2] === 'raw' ? ' data-raw' : '') + '>' +
          C.util.esc(String(r[1])) + '</span>';
      })
      .join('');
  };

  function renderDevMouse() {
    const m = (S.health || {}).mouse || {};
    const el = document.getElementById('dm-state');
    const hint = document.getElementById('dm-hint');
    if (!el) return;
    const tag = !m.available ? 'bad' : (m.connected ? 'ok' : 'warn');
    const st = !m.available ? '模块缺失' : (m.connected ? '已连接' : '未连接');
    kv(el, [
      ['状态', st],
      ['端口', m.port || '—'],
      ['固件', (m.device && m.device.fw) || (m.fw || '—')],
      ['协议', (m.device && m.device.proto) || '—'],
      ['链路', m.link || '—'],
      ['最近错误', (m.detail || '').split('\n')[0] || '—']
    ]);
    const warn = document.getElementById('dm-warn');
    if (warn) {
      warn.textContent = (m.detail && !m.connected)
        ? m.detail
        : (m.connect_in_flight ? '上一次连接未返回，请重启服务' : '');
    }
    if (hint) {
      hint.textContent = !m.available ? '鼠标模块不可用'
        : (m.connected ? '' : '未连接电脑鼠');
    }
    const btn = document.getElementById('dm-connect');
    if (btn) { btn.className = 'mini ' + (tag === 'ok' ? '' : 'primary'); }
    /* 【复位】只在**已连接**时可点：整机软复位走的还是同一条命令通道，
       断开时它做不了任何事（点了只会吃一个 409，还让人以为"复位也不好使"）。 */
    C.gate(document.getElementById('dm-rescue'),
           m.connected ? '' : '先连接电脑鼠');
  }



  function renderDevClock() {
    const cl = (S.health || {}).clock || {};
    const m = (S.health || {}).mouse || {};
    const ts = m.timesync || {};
    const el = document.getElementById('dk-state');
    if (!el) return;
    kv(el, [
      ['手机偏移', cl.offset_ms != null ? (cl.offset_ms + ' ms') : '—'],
      ['不确定度', cl.uncertainty_ms != null ? ('±' + cl.uncertainty_ms + ' ms') : '—'],
      ['来源', cl.source || '—'],
      ['方法', cl.method || '—'],
      ['样本', cl.samples != null ? cl.samples : '—'],
      ['设备时基', ts.armed ? ('已发车 @' + ts.arm_up_ms + 'ms') : '未发车'],
      ['漂移', ts.drift_ppm != null ? (ts.drift_ppm + ' ppm') : '—']
    ]);
    const note = document.getElementById('dk-note');
    if (note) {
      note.textContent = (!cl.calibrated)
        ? '手机偏移未标定：跨机对齐按 assumed 记录。有采集会话时可执行【自动拟合】。'
        : '';
    }
  }


  async function refreshClock() {
    const d = await C.guard(function () { return C.api('/api/v1/clock'); });
    if (d) { S.clock = d; }
    renderDevClock();
  }

  /* ------------------------------------------------------------ WS */
  function wsConnect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(proto + '://' + location.host + '/ws');
    ws.binaryType = 'arraybuffer';
    S.ws = ws;
    ws.onopen = function () { setWs(true); };
    ws.onclose = function () {
      setWs(false);
      setTimeout(wsConnect, 1500);          // 服务端重启时自动回来
    };
    ws.onerror = function () { };
    ws.onmessage = function (ev) {
      if (typeof ev.data === 'string') {
        let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
        onJson(m);
      } else {
        const f = WSProto.decode(ev.data);
        // ① 全局二进制订阅（C.onBin）
        (C.onBin[f.meta.type] || []).forEach(function (h) {
          try { h(f); } catch (e) { console.error(e); }
        });
        // ② 视图自己的二进制订阅（views/<name>.bin）
        Object.keys(S.views).forEach(function (k) {
          const b = S.views[k].bin;
          if (b && b[f.meta.type]) {
            try { b[f.meta.type](f); } catch (e) { console.error(e); }
          }
        });
      }
    };
  }
  function setWs(ok) {
    const d = document.getElementById('ws-dot');
    d.className = 'ws ' + (ok ? 'live' : 'offline');
    d.textContent = ok ? 'WS' : 'WS断';
  }

  /* 所有 JSON 消息**统一广播给每个已挂载的视图**（各自 onMsg 里挑自己关心的）。
     两栏同时可见之后，"只喂当前页"就不再成立了：左栏在看场地、右栏在发车，
     两边都得实时更新 —— 各视图的 onMsg 本来就写了过滤，广播才是对的。 */
  function onJson(m) {
    switch (m.type) {
      case 'hello':
        if (m.health) applyHealth(m.health);
        break;
      case 'health':
        applyHealth(m);
        break;
      case 'mouse.telemetry':
        S.telemetry = m;
        break;
      case 'mouse.text':
        /* 设备文本流进底栏 = **鼠**来源；方向只体现在正文的箭头里。 */
        pushConsole('', m.line, { type: 'mouse.text', level: 'info',
                                  source: 'mouse', direction: m.direction,
                                  line: m.line, t_pc_ms: m.t_pc_ms });
        break;
      case 'log':
        pushConsole('', m.text, m);
        S.serviceLog.push(m);
        break;
      case 'session.phase':
        S.session = m;
        renderSession();
        break;
    }
    dispatch(m);
  }
  function dispatch(m) {
    const all = Object.keys(S.views).concat(Object.keys(S.mods));
    all.forEach(function (k) {
      const v = S.views[k] || S.mods[k];
      if (v && v.onMsg) { try { v.onMsg(m); } catch (e) { /* 隔离：一个视图崩不拖累别人 */ } }
    });
  }

  /* ------------------------------------------------------------ 顶栏 */
  function applyHealth(h) {
    S.health = h;
    /* 改版二十四：顶栏由**四灯变两灯** —— 相机/定位两路随手机相机下线（拍板：
       位置数据改由视频会话给，那一路是离线的，没有"现在通不通"这种状态可报）。
       剩下的两盏回答的仍是现场两件事：车连上了没有、时间能不能对上。 */
    const mo = h.mouse || {}, cl = h.clock || {};
    setLamp('lamp-mouse',
      !mo.available ? 'err' : (mo.connected ? 'on' : ''),
      mo.connected ? (mo.port || '已连接') : (mo.available ? '未连接' : '模块缺失'),
      '电脑鼠 · ' + (mo.connected ? (mo.port || '已连接') : '未连接'));
    // 时钟灯显示的是「时间到底能不能对上」：
    //   · `clock.offset` 标定过（跨机对齐有依据）
    //   · 设备时基发过车（`ARM`，帧的发送时刻才算得出来）
    // 两个都不满足时不能只写"未标定"就完事 —— 那是两件不同的事。
    // ⚠️ 改版二十四：相机下线后**不再有自动拟合**（`/clock/autofit` 依赖相机帧），
    //    `offset` 只能手填；这一盏灯因此只反映"标没标过"，不再暗示"能自动标"。
    const ts = mo.timesync || {};
    const camOk = !!cl.calibrated, devOk = !!ts.armed;
    setLamp('lamp-clock',
      (camOk && devOk) ? 'on' : 'warn',
      devOk ? ('发车 ' + (ts.drift_ppm != null ? ts.drift_ppm + 'ppm' : '已对齐'))
            : (camOk ? ('偏移 ±' + cl.uncertainty_ms + 'ms') : '未标定'),
      '时间基 · 偏移 ' + cl.source + ' / ' + cl.offset_ms + 'ms（手动）· 设备时基 ' +
      (devOk ? ('已发车 @' + ts.arm_up_ms + 'ms，样本 ' + ts.samples) : '未发车'));

    renderDev();
    renderPrep();
    /* 顶栏的动作按"准备度"开门/关门。
       注意急停**不门控** —— 恰恰是连不上的时候最可能想按它。 */
    const rd = h.readiness;
    C.gate(document.getElementById('btn-open'), (rd && !rd.can_record)
      ? '未连接设备' : null);
    C.gate(document.getElementById('btn-close'), h.session ? null : '当前无进行中的会话');
    const qn = document.getElementById('quick-note');
    if (qn) qn.textContent = mo.connected ? '' : '电脑鼠未连接';
    document.querySelectorAll('[data-quick]').forEach(function (b) {
      C.gate(b, mo.connected ? null : '未连接电脑鼠');
    });
    // 顶栏标签在未连时给一个黄色状态字，让"可点且待办"一眼可见
    ['lamp-mouse', 'lamp-clock'].forEach(function (id) {
      const e = document.getElementById(id);
      if (!e) return;
      const want = (id === 'lamp-mouse') ? !mo.connected : !(camOk && devOk);
      e.classList.toggle('gated', !!want);
    });

    renderSession();
    broadcastHealth(h);
  }

  /* 把 health 广播给**所有已挂载的视图**，不只当前页 —— 两栏同时可见之后，
     左栏在看场地、右栏在发车，两边都得跟着刷新（各视图的 onHealth 本来就只挑
     自己关心的部分）。抽成具名函数是为了让 `mountViews()` 也能调：视图挂载的时刻
     晚于第一份 health 到手，如果只靠 2.5 s 的心跳补，那些"只在 onHealth 里写"的
     东西（比如发车页的未连提示）会**空着**直到下一次心跳 —— 中途任何一步慢一点，
     这里就会变成一个谜。 */
  function broadcastHealth(h) {
    const all = Object.keys(S.views).concat(Object.keys(S.mods));
    all.forEach(function (k) {
      const v = S.views[k] || S.mods[k];
      if (v && v.onHealth) {
        try { v.onHealth(h); } catch (e) { /* 一个视图崩不拖累别的 */ }
      }
    });
  }

  function setLamp(id, cls, main, sub) {
    const e = document.getElementById(id);
    if (!e) return;
    e.className = 'lamp ' + (cls || '');
    e.querySelector('em').textContent = main || '';
    e.title = sub || '';
  }

  function renderSession() {
    const h = S.health || {};
    const se = h.session;
    const st = document.getElementById('session-state');
    if (S.session && S.session.op === 'close') {
      st.textContent = '●会话已收 ' + S.session.session_id;
      (S.session.warnings || []).forEach(function (w) { C.toast('收会话告警：' + w, 'warn', 14000); });
      S.session = null;
    } else if (se) {
      const ph = se.phase || {};
      st.textContent = '●录制中 ' + se.session_id + '  [' +
        Object.keys(ph).map(function (k) { return k + ':' + ph[k]; }).join(' ') + ']';
    } else {
      st.textContent = '●无会话';
    }
    /* 改版二十四：原来的「已验帧 / 投递账本」是**相机帧**的核对（帧数、账本完整度），
       相机下线后这两个数没有来路 —— 整条删掉，不用"—"占位（占位会让人以为"暂时没数"）。 */
    const info = document.getElementById('session-info');
    if (info) {
      if (se) info.textContent = se.session_id + '\n' + se.path + '\n已开 ' + se.duration_s + ' s';
      else info.textContent = '无进行中的会话';
    }
  }

  /* ------------------------------------------------------------ 底部信息集成栏
     底栏**只放信息输出**（原来的"实时读数"条已删 —— 那些量在「实时状态与参数设定」
     页里本来就有，底栏不该再抄一份）。两条筛选维度：

       · **来源**：这条信息是从哪来的 —— 电脑鼠 / 相机 / 场地定位 / 本层。
         后端 `bus.log(level, text, source=...)` 给的就是这个（唯一真相在 `bus.SOURCES`）。
       · **级别**：info / warn / error（`log` 事件带的 `level`）。

     两维都是"多选 + 全部"：默认**全部来源、只看 warn 及以上？** 不 —— 默认全看，
     因为现场第一眼要的是"现在到底在发生什么"，筛是去噪手段不是默认。
     计数写在工具条上（"显示 N / 共 M"），**筛掉了多少要看得见**，否则会以为丢了信息。 */
  /* 底栏「来源」筛选。改版二十四：相机/场地定位两路下线 ⇒ 两个死选项删掉
     （留着的话它们永远筛不出任何内容 —— 一个死筛选项比没有更让人困惑）。
     顺序与后端 `bus.SOURCES` 一致：电脑鼠 / 视频 / 本层。 */
  const SRC_DEF = [
    { id: 'mouse',   name: '电脑鼠' },
    { id: 'video',   name: '视频复盘' },
    { id: 'control', name: '本层' }
  ];
  const LVL_DEF = [
    { id: 'info',  name: '普通' },
    { id: 'warn',  name: '警告' },
    { id: 'error', name: '错误' }
  ];
  const SRC_NAME = {};
  SRC_DEF.forEach(function (s) { SRC_NAME[s.id] = s.name; });
  /* 筛选状态：`null` = 全部。用 Set 存选中的 id。 */
  let infoFilterSrc = null;              // null = 全部来源
  let infoFilterLvl = null;              // null = 全部级别
  const INFO_CAP = 400;                  // DOM 里最多留这么多行（环形）

  /* 一条信息**从哪来**：优先看后端给的 `source`；老后端没给就按类型兜底
     （`mouse.text` 一定是鼠、其余归本层）—— **不猜文案**。 */
  function infoSourceOf(m) {
    const s = m && m.source;
    if (s && SRC_NAME[s]) return s;
    if (m && m.type === 'mouse.text') return 'mouse';
    return 'control';
  }

  function infoLevelOf(m) {
    const l = (m && m.level) || 'info';
    return (l === 'warn' || l === 'error') ? l : 'info';
  }

  function infoLineText(m) {
    if (m.type === 'mouse.text') {
      const arrow = m.direction === '<-' ? '← ' : '→ ';
      return arrow + (m.line || '');
    }
    return m.text || '';
  }

  function infoPass(m) {
    if (infoFilterSrc && !infoFilterSrc.has(infoSourceOf(m))) return false;
    if (infoFilterLvl && !infoFilterLvl.has(infoLevelOf(m))) return false;
    return true;
  }

  function fmtClock(ms) {
    const d = new Date(ms);
    const p = function (n) { return (n < 10 ? '0' : '') + n; };
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  function infoRow(m) {
    const src = infoSourceOf(m), lvl = infoLevelOf(m);
    const div = document.createElement('div');
    div.className = 'il ' + lvl;
    const t = document.createElement('span');
    t.className = 't';
    t.textContent = fmtClock(m.t_pc_ms || Date.now());
    const s = document.createElement('span');
    s.className = 's src-' + src;
    s.textContent = '[' + SRC_NAME[src] + ']';
    const txt = document.createElement('span');
    txt.className = 'm';
    txt.textContent = infoLineText(m);
    div.appendChild(t); div.appendChild(s); div.appendChild(txt);
    return div;
  }

  function renderInfoCount() {
    const el = document.getElementById('ib-count');
    if (!el) return;
    const shown = S.textLines.filter(infoPass).length;
    const total = S.textLines.length;
    el.textContent = shown === total
      ? (total + ' 条')
      : ('显示 ' + shown + ' / 共 ' + total);
  }

  /* 全量重画（改筛选时用）—— 日志量有上限，重画比"增量 + 回滚"简单且不会漏。 */
  function renderInfoLog() {
    const box = document.getElementById('info-log');
    if (!box) return;
    box.innerHTML = '';
    const rows = S.textLines.filter(infoPass).slice(-INFO_CAP);
    rows.forEach(function (m) { box.appendChild(infoRow(m)); });
    box.scrollTop = box.scrollHeight;
    renderInfoCount();
  }

  /* 追加一条（新信息到达时用）—— 只有过筛选才进 DOM（省开销、也顺手守住筛选）。 */
  function pushConsole(kind, line, m) {
    const rec = m || { type: 'console', level: kind === 'warn' ? 'warn' : 'info', text: line };
    S.textLines.push(rec);
    if (S.textLines.length > 2000) S.textLines.splice(0, 500);
    const box = document.getElementById('info-log');
    if (!box) return;
    if (!infoPass(rec)) { renderInfoCount(); return; }
    box.appendChild(infoRow(rec));
    while (box.childNodes.length > INFO_CAP) box.removeChild(box.firstChild);
    box.scrollTop = box.scrollHeight;
    renderInfoCount();
  }
  C.pushConsole = pushConsole;
  /* 全量重画底栏 —— 给自动化用（喂过假信息后把环清干净再看）。 */
  C.renderInfoLog = renderInfoLog;
  /* 自动化清理：把带指定前缀的假信息从环里拿掉（browser_check 喂完要收干净，
     又不该用【清空】把真日志一起抹了 —— 那会让后面的判据失去"WS 真在推"的证据）。 */
  C.infoDropPrefix = function (prefix) {
    S.textLines = (S.textLines || []).filter(function (m) {
      return !(m && m.text && String(m.text).indexOf(prefix) === 0);
    });
    renderInfoLog();
  };

  function buildInfoBar() {
    const srcBox = document.getElementById('ib-src');
    const lvlBox = document.getElementById('ib-lvl');
    if (!srcBox || !lvlBox) return;
    const all = document.createElement('button');
    all.textContent = '全部';
    all.dataset.all = '1';
    srcBox.appendChild(all);
    SRC_DEF.forEach(function (s) {
      const b = document.createElement('button');
      b.textContent = s.name; b.dataset.src = s.id;
      srcBox.appendChild(b);
    });
    const allL = document.createElement('button');
    allL.textContent = '全部'; allL.dataset.all = '1';
    lvlBox.appendChild(allL);
    LVL_DEF.forEach(function (s) {
      const b = document.createElement('button');
      b.textContent = s.name; b.dataset.lvl = s.id;
      lvlBox.appendChild(b);
    });
    /* 单选式切换：点"全部"=清成 null；点某个源 = 只留它（再点一次回到全部）。
       设成"多选"会让工具条有 2^4 种状态，现场反而不知道自己在看什么。 */
    const wire = function (box2, getSet, setSet) {
      box2.querySelectorAll('button').forEach(function (b) {
        b.onclick = function () {
          if (b.dataset.all) { setSet(null); }
          else {
            const id = b.dataset.src || b.dataset.lvl;
            const cur = getSet();
            if (cur && cur.size === 1 && cur.has(id)) setSet(null);  // 再点回到全部
            else setSet(new Set([id]));
          }
          paintInfoBar();
          renderInfoLog();
        };
      });
    };
    wire(srcBox, function () { return infoFilterSrc; },
         function (v) { infoFilterSrc = v; });
    wire(lvlBox, function () { return infoFilterLvl; },
         function (v) { infoFilterLvl = v; });
    const clr = document.getElementById('ib-clear');
    if (clr) clr.onclick = function () {
      S.textLines.length = 0;
      renderInfoLog();
    };
    paintInfoBar();
  }

  function paintInfoBar() {
    const srcBox = document.getElementById('ib-src');
    const lvlBox = document.getElementById('ib-lvl');
    if (srcBox) {
      srcBox.querySelectorAll('button').forEach(function (b) {
        const on = b.dataset.all ? (infoFilterSrc === null)
                                 : (infoFilterSrc && infoFilterSrc.has(b.dataset.src));
        b.classList.toggle('on', !!on);
      });
    }
    if (lvlBox) {
      lvlBox.querySelectorAll('button').forEach(function (b) {
        const on = b.dataset.all ? (infoFilterLvl === null)
                                 : (infoFilterLvl && infoFilterLvl.has(b.dataset.lvl));
        b.classList.toggle('on', !!on);
      });
    }
  }

  /* ------------------------------------------------------------ 底部整合栏 */
  function toggleDock(on, silent) {
    const d = document.getElementById('dock');
    if (!d) return;
    const want = (on === undefined) ? !d.classList.contains('on') : !!on;
    d.classList.toggle('on', want);
    if (want && !silent) {
      const v = S.views.data;
      if (v && v.activate) { try { v.activate(); } catch (e) { C.toast('数据页激活失败：' + e.message, 'bad'); } }
    }
  }

  /* ------------------------------------------------------------ 子系统分段路由
     **入口页只做导航，模块界面一次只开一个** —— 只有"现在开着哪个模块"这一份状态，
     记在 `S.open`。URL 里带 `#/<子系统>/<模块>`：刷新、贴链接、浏览器后退都落得回去。 */
  function renderNavHeads() {
    SYS_DEF.forEach(function (s) {
      const sec = document.querySelector('.nav-sec[data-sys="' + s.id + '"]');
      if (!sec) return;
      const h = sec.querySelector('.sec-head');
      if (!h) return;
      h.innerHTML = '<b>' + C.util.esc(s.name) + '</b>' +
        '<span class="sec-sub">' + C.util.esc(s.sub || '') + '</span>';
    });
  }

  /* 打开模块界面；`mid` 为空 = 回入口页。
     `noHistory` 用在"按 URL 落位"那条路径上 —— 那时地址栏已经是对的，
     再写一次历史只会让后退键原地打转。 */
  function openMod(sys, mid, noHistory) {
    S.open = { sys: sys || '', mid: mid || '' };
    document.getElementById('sys').classList.toggle('mod-open', !!mid);
    if (!mid) {
      if (!noHistory) pushRoute('', '');
      return;
    }
    document.getElementById('mod-title').textContent = MOD_NAME[mid] || mid;
    const def = SYS_DEF.filter(function (s) { return s.id === sys; })[0];
    document.getElementById('mod-sub').textContent =
      def ? (def.name + ' · ' + (def.sub || '')) : '';
    /* 一次只显示一个子系统的工作区，子系统内一次只显示一个模块。 */
    document.querySelectorAll('#mod-body > .ws').forEach(function (w) {
      w.classList.toggle('on', w.id === 'ws-' + sys);
    });
    const ws = document.getElementById('ws-' + sys);
    if (ws) {
      ws.querySelectorAll('.view').forEach(function (v) {
        v.classList.toggle('on', v.dataset.mod === mid);
      });
    }
    const v = S.mods[mid];
    if (v && v.activate) {
      try { v.activate(); } catch (e) { C.toast('模块激活失败：' + e.message, 'bad'); }
    }
    /* 「综合运动」界面里是单栏五页：它们不靠切页激活，进来就激活默认那一页。 */
    if (sys === 'all') { activate('params'); }
    if (!noHistory) pushRoute(sys, mid);
  }
  function closeMod(noHistory) { openMod('', '', noHistory); }
  C.openMod = openMod;

  function pushRoute(sys, mid) {
    const want = (sys && mid) ? ('#/' + sys + '/' + mid) : '';
    if ((location.hash || '') === want) return;    // 已经在这一格，别堆历史
    history.pushState({ sys: sys || '', mid: mid || '' }, '',
      want || (location.pathname + location.search));
  }

  /* 按当前 URL 落位：`#/<子系统>/<模块>` 合法就开它，否则回入口。
     后退键走的就是这条路径（popstate），所以这里绝不能再写历史。 */
  function routeFromHash() {
    const m = /^#\/([^\/]+)\/(.+)$/.exec(location.hash || '');
    const def = m && SYS_DEF.filter(function (s) { return s.id === m[1]; })[0];
    const ok = def && def.mods.some(function (x) { return x.id === m[2]; });
    if (ok) openMod(m[1], m[2], true); else closeMod(true);
  }

  function renderModuleGrid(sys) {
    const def = SYS_DEF.filter(function (s) { return s.id === sys; })[0];
    const box = document.getElementById('mods-' + sys);
    if (!def || !box || !def.mods.length) return;
    box.innerHTML = def.mods.map(function (m) {
      const has = !!C.mods[m.id];
      /* `@all` 那个模块不是 JS 视图，而是 index.html 里写好的两栏九页 —— 不算未实现。 */
      const miss = (!has && m.id !== '@all') ? '<span class="miss">未实现 · 见文档</span>' : '';
      return '<button class="mod-card" data-mod="' + m.id + '">' +
        '<b>' + C.util.esc(m.name) + '</b>' +
        '<span>' + C.util.esc(m.desc || '') + '</span>' + miss +
        '</button>';
    }).join('');
    box.querySelectorAll('.mod-card').forEach(function (b) {
      b.onclick = function () { openMod(sys, b.dataset.mod); };
    });
  }

  /* 把已注册的模块视图挂到 `#mod-body` 里各自的子系统容器（`#ws-<sys>`）。
     返回入口的按钮在**模块界面头部**（`#mod-head`），模块自己不带 —— 一处就够。 */
  function mountModules() {
    SYS_DEF.forEach(function (s) {
      renderModuleGrid(s.id);
      const ws = document.getElementById('ws-' + s.id);
      if (!ws) return;
      s.mods.forEach(function (m) {
        const v = C.mods[m.id];
        if (!v) return;                       // 未实现（`@all` 是纯 HTML 视图，也没有）
        const el = document.createElement('div');
        el.className = 'view';
        el.dataset.mod = m.id;
        ws.appendChild(el);
        S.mods[m.id] = v;
        try { v.mount(el); } catch (e) {
          el.innerHTML = '<div class="errbox">模块 ' + m.id + ' 挂载失败：' +
            C.util.esc(e.message) + '</div>';
        }
      });
    });
  }

  /* ------------------------------------------------------------ 视图路由 */
  function activate(tab) {
    const rail = RAIL_OF[tab];
    if (!rail) return;
    S.tabs = S.tabs || { mouse: 'params', dock: '' };
    if (rail === 'dock') {
      toggleDock(true, true);
    } else {
      const nav = document.getElementById('rail-' + rail);
      if (nav) {
        nav.querySelectorAll('button[data-tab]').forEach(function (b) {
          b.classList.toggle('on', b.dataset.tab === tab);
        });
      }
    }
    S.tabs[rail] = tab;
    const host = document.querySelector('.rail-views[data-rail="' + rail + '"]');
    if (host) {
      host.querySelectorAll('.view').forEach(function (v) {
        v.classList.toggle('on', v.dataset.tab === tab);
      });
    }
    const v = S.views[tab];
    if (v && v.activate) { try { v.activate(); } catch (e) { C.toast('视图激活失败：' + e.message, 'bad'); } }
    renderContext();
  }
  // 对外暴露：底栏的 ⑧数据 没有页签按钮（"展开即激活"），检查器与别处要能直接切过去。
  C.activate = activate;

  function mountViews() {
    const names = Object.keys(C.views);
    if (names.length !== EXPECTED_VIEWS) {
      // 视图数与注册数必须一致（见 EXPECTED_VIEWS 的说明）。这种"少了一个视图"
      // 如果只表现为"那一页是空的"，排查会非常费时。
      const host = document.querySelector('.rail-views[data-rail="mouse"]');
      const box = document.createElement('div');
      box.className = 'errbox';
      box.textContent = '已注册视图 ' + names.length + ' 个，应为 ' + EXPECTED_VIEWS +
        '：' + names.join(',');
      host.appendChild(box);
    }
    mountModules();
    names.forEach(function (name) {
      const rail = RAIL_OF[name] || 'mouse';
      const host = document.querySelector('.rail-views[data-rail="' + rail + '"]');
      if (!host) return;
      const el = document.createElement('div');
      el.className = 'view';
      el.dataset.tab = name;
      host.appendChild(el);
      S.views[name] = C.views[name];
      try { C.views[name].mount(el); } catch (e) {
        el.innerHTML = '<div class="errbox">视图 ' + name + ' 挂载失败：' + C.util.esc(e.message) + '</div>';
      }
    });
    activate(S.tabs.mouse || 'params');
    /* 挂载完成后再喂一遍当前 health：视图是在第一份 health **之后**才挂上的，
       它们挂载时来不及听广播。少了这一句，"页面上那些只在 onHealth 里写的东西"
       就得等 2.5 s 后的心跳才出现 —— 而心跳一旦因为任何原因没跑，它就永远是空的。 */
    if (S.health) broadcastHealth(S.health);
  }

  /* 每栏底部的「数据」条只放**这一路当前页**的 KV。没有数据的页面就整条收起来，
     该栏的视图直接多吃 150px —— 空条目比没有条目更浪费。
     这也是为什么各视图的 `context()` 要么填 KV，要么什么都不填。 */
  function renderContext() {
    S.tabs = S.tabs || { mouse: 'params', dock: '' };
    ['mouse'].forEach(function (rail) {
      const box = document.getElementById('ctx-' + rail);
      if (!box) return;
      const v = S.views[S.tabs[rail]];
      if (v && v.context) {
        try { v.context(box); } catch (e) {
          box.innerHTML = '<div class="errbox">' + C.util.esc(e.message) + '</div>';
        }
      } else {
        box.innerHTML = '';
      }
    });
  }
  C.renderContext = renderContext;

  /* ------------------------------------------------------------ 交互 */
  async function loadTargets() {
    const box = document.getElementById('dm-targets');
    if (!box) return;
    box.textContent = '读取中…';
    const d = await C.guard(function () { return C.api('/api/v1/mouse/ports'); }, '列端口');
    if (!d) { box.textContent = '鼠标模块不可用'; return; }
    const list = d.targets || [];
    box.innerHTML = '';
    list.forEach(function (t) {
      const el = document.createElement('div');
      el.className = 'item' + (S.mouseTarget === t.target ? ' sel' : '');
      el.innerHTML = C.util.esc(t.label) + ' <span class="kind">[' + C.util.esc(t.kind) + ']</span>';
      el.title = t.target;
      /* **点一下只是选中，不连接。**
         连接必须是单独一次点击 —— 这是"手动连接"这条要求的落点：
         现场最怕的就是手指滑一下就把设备切到别的口上，而界面上毫无痕迹。 */
      el.onclick = function () {
        S.mouseTarget = t.target;
        Array.prototype.forEach.call(box.children, function (c) { c.classList.remove('sel'); });
        el.classList.add('sel');
        const inp = document.getElementById('dm-manual');
        if (inp) inp.value = '';
        const c = document.getElementById('dm-confirm');
        if (c) c.innerHTML = '<b>已选中</b><span>' + C.util.esc(t.target) + '</span>';
      };
      box.appendChild(el);
    });
    if (!list.length) box.textContent = '无可用目标';
    // 只有一个串口目标时替用户选中它（但不替他连）—— 少一步鼠标移动，
    // 同时"连接"仍然是一次明确的人工动作。
    const serials = list.filter(function (t) { return t.kind === 'serial'; });
    if (!S.mouseTarget && serials.length === 1) {
      const first = box.children[list.indexOf(serials[0])];
      if (first) first.click();
    }
    if (!S.mouseTarget && list.length && d.note) {
      const c = document.getElementById('dm-confirm');
      if (c && !c.textContent) c.innerHTML = '<b>提示</b><span>' + C.util.esc(d.note) + '</span>';
    }
  }

  /* 手动连接：连接**选中的目标**，或手动输入框里那个（手填优先，因为那是明确意图）。
     连上后回读设备信息当作"确认"—— 只看 toast 说"成功"是不够的，
     得能看见连的到底是哪个口、哪版固件。 */
  async function doConnect() {
    const inp = document.getElementById('dm-manual');
    const manual = inp ? (inp.value || '').trim() : '';
    const target = manual || S.mouseTarget;
    if (!target) {
      C.toast('请先选择目标或手动输入', 'warn', 9000);
      return;
    }
    const btn = document.getElementById('dm-connect');
    const old = btn.textContent;
    btn.disabled = true; btn.textContent = '连接中…';
    try {
      const d = await C.guard(function () {
        return C.api('/api/v1/mouse/connect', { method: 'POST', body: { target: target } });
      }, '连接 ' + target);
      if (d) {
        const dev = d.device || {};
        const bits = [d.target || target];
        if (dev.proto !== undefined && dev.proto !== null) bits.push('proto ' + dev.proto);
        if (dev.fw) bits.push('fw ' + dev.fw);
        if (d.state) bits.push('状态 ' + d.state);
        const line = '已连接：' + bits.join(' · ');
        C.toast(line, 'ok', 14000);
        const c = document.getElementById('dm-confirm');
        if (c) c.innerHTML = '<b>连接结果</b><span>' + C.util.esc(line) + '</span>';
        S.mouseTarget = d.target || target;
        if (inp) inp.value = '';
      }
    } finally {
      btn.disabled = false; btn.textContent = old;
      await refreshHealth();
    }
  }

  async function refreshHealth() {
    const h = await C.guard(function () { return C.api('/api/v1/health'); });
    if (h) applyHealth(h);
  }
  // 别处（手动动作之后）需要立刻刷一次健康，而不是等 2.5 s 轮询
  C.refreshHealthNow = refreshHealth;

  function bindTop() {
    document.getElementById('btn-open').onclick = async function () {
      // 前端这一道只是为了**当场解释**（不用等服务端 409 再弹）。真正的闸在
      // `ctx.session_open` 里 —— 从任何入口（脚本、curl）进来都拦得住。
      const rd = (S.health || {}).readiness;
      if (rd && !rd.can_record) {
        C.toast('未连接任何设备\n' + rd.blockers.join('\n'), 'bad', 18000);
        return;
      }
      const d = await C.guard(function () { return C.api('/api/v1/session/open', { method: 'POST', body: {} }); }, '开会话');
      if (d) {
        // 只连了一路时把"另一路是空的、因为没连"当场说清，
        // 而不是等收会话翻 timeline 才发现少了一半。
        (d.warnings || []).forEach(function (w) { C.toast('会话提醒：' + w, 'warn', 18000); });
        C.toast('会话已开：' + d.session_id, 'ok');
        refreshHealth();
      }
    };
    document.getElementById('btn-close').onclick = async function () {
      const d = await C.guard(function () { return C.api('/api/v1/session/close', { method: 'POST', body: {} }); }, '收会话');
      if (d) {
        (d.warnings || []).forEach(function (w) { C.toast(w, 'warn', 15000); });
        C.toast('会话已收' + (d.batch ? '，赛后复算 ' + d.batch.run_id : ''), 'ok');
        refreshHealth();
      }
    };
    document.getElementById('btn-estop').onclick = function () {
      C.guard(function () { return C.api('/api/v1/mouse/estop', { method: 'POST', body: {} }); }, '急停');
    };
    /* 复位（整机软复位，上游 2.2.0 的 `MCU RESET`）—— 卡死复位口。
       放在设备窗口里而不是只放实验 8：卡死**不只在跑迷宫时发生**（任何一次
       非协作式阻塞等待卡住都一样），而"设备不动了"的第一反应是打开设备窗口看。
       ⚠️ 它是**整机复位**，所以提示必须把两条后果一起说（不复位物理位置 / 约 3 s 才有新帧），
       否则现场会以为"复位了怎么还没动"或复位后立刻发车按错地图走。 */
    const rsc = document.getElementById('dm-rescue');
    if (rsc) {
      rsc.onclick = async function () {
        const d = await C.guard(function () {
          return C.api('/api/v1/mouse/rescue', { method: 'POST', body: {} });
        }, '复位');
        if (!d) return;
        if (d.verified) {
          C.toast('复位成功：设备已整机复位（rst=' + d.rst + '，等了 ' +
                  C.util.fmt(d.elapsed_s, 1) + ' s）' +
                  '\n⇒ 发车前把电脑鼠放回起点角。', 'ok', 30000);
        } else {
          C.toast('复位没有复核到复位：rst=' + (d.rst || '—') + '\n' + (d.note || ''), 'warn', 30000);
        }
        refreshHealth();
      };
    }
    // 顶栏状态标签 = 设备窗口的入口
    document.querySelectorAll('.lamp[data-dev]').forEach(function (b) {
      b.onclick = function () { openDev(b.dataset.dev); };
    });
    // 单栏页签（「综合运动」界面内部）
    ['rail-mouse'].forEach(function (id) {
      const nav = document.getElementById(id);
      if (nav) {
        nav.onclick = function (e) { if (e.target.dataset.tab) activate(e.target.dataset.tab); };
      }
    });
    // 模块界面头部的【← 返回】= 回入口页（顺手把 URL 上的 hash 清掉）
    const mb = document.querySelector('#mod-head [data-back]');
    if (mb) mb.onclick = function () { closeMod(); };
    // 浏览器后退/前进：按当前 URL 落位（routeFromHash 不会再写历史）
    window.addEventListener('popstate', routeFromHash);
    const dh = document.getElementById('dock-head');
    if (dh) dh.onclick = function () { toggleDock(); };
    document.querySelectorAll('[data-quick]').forEach(function (b) {
      b.onclick = function () {
        const why = C.needMouse(b.dataset.quick);
        if (why) { C.toast(why, 'warn', 10000); return; }
        C.guard(function () {
          return C.api('/api/v1/mouse/raw', { method: 'POST', body: { line: b.dataset.quick } });
        }, b.dataset.quick);
      };
    });
  }

  function bindDev() {
    const modal = document.getElementById('dev-modal');
    document.getElementById('dev-close').onclick = closeDev;
    modal.onclick = function (e) { if (e.target.id === 'dev-modal') closeDev(); };
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && modal.classList.contains('on')) closeDev();
    });

    /* ── 电脑鼠 ── */
    document.getElementById('dm-connect').onclick = function () { doConnect(); };
    // 手填目标后按回车 = 点【连接】（现场没人愿意为了连一次口去够小按钮）
    document.getElementById('dm-manual').onkeydown = function (e) {
      if (e.key === 'Enter') { e.preventDefault(); doConnect(); }
    };
    document.getElementById('dm-refresh').onclick = function () { loadTargets(); };
    document.getElementById('dm-disconnect').onclick = async function () {
      await C.guard(function () { return C.api('/api/v1/mouse/disconnect', { method: 'POST', body: {} }); });
      refreshHealth();
    };

    document.getElementById('dk-mark').onclick = async function () {
      const d = await C.guard(function () {
        return C.api('/api/v1/clock/mark', { method: 'POST', body: {} });
      }, '打点');
      if (d) C.toast('已记录同步点', 'ok', 9000);
    };
  }

  /* ------------------------------------------------------------ 启动 */
  function bootFail(stage, e) {
    // 启动期只要抛一次未捕获异常，后面的 wsConnect/refreshHealth **全都不会跑** ——
    // 表现是"页面能打开但一切都是死的"，而服务端日志干干净净。
    // 所以这里必须把失败**摆到界面上**，而不是留给控制台。
    const host = document.querySelector('.rail-views[data-rail="mouse"]');
    const box = document.createElement('div');
    box.className = 'errbox';
    box.textContent = '前端启动失败 · ' + stage + ' · ' + (e && e.message ? e.message : e);
    if (host) host.appendChild(box);
    try { C.toast('前端启动失败 · ' + stage + ' · ' + (e && e.message ? e.message : e), 'bad', 60000); }
    catch (e2) { /* 连 toast 都失败就别再抛了 */ }
    console.error('[boot:' + stage + ']', e);
  }

  window.addEventListener('DOMContentLoaded', async function () {
    let stage = 'assets';
    try {
      /* 资源版本自检：`C.views` / `C.mods` 由 `wsproto.js` 第 10 行起建。
         浏览器把**新旧资源混着用**时它们会不存在，模块脚本一个都没注册上，
         随后在 `mountViews` 里炸成 `Cannot read properties of undefined
         (reading 'mod_params')` —— 那句话看不出该做什么。这里提前判一次。
         （服务端已给界面资源加 `Cache-Control: no-cache`，正常情况下不会再发生。） */
      if (!C.views || !C.mods) {
        throw new Error('静态资源版本不一致 · 强制刷新 Ctrl+Shift+R');
      }
      stage = 'bind';
      S.tabs = { mouse: 'params', dock: '' };
      bindTop();
      bindDev();
      /* 底部信息栏（来源/级别筛选）**不依赖任何网络**，绑上就能用；
         后到的 WS 日志走 `pushConsole` 追加。 */
      buildInfoBar();
      /* ⚠️ **心跳必须第一个装上**，排在所有 await 之前。
         这一行以前写在启动链的**最后**（`mountViews()` 和 `/caps` 之后），
         当时的想法是"先拿 health 再挂载"，但它顺带把心跳也变成了
         "前面每一步都得顺利"的下游 —— 而前面有网络请求。
         实测（faulthandler + 注入器）：链路刚断时 `/caps` 会把服务端的
         事件循环占住十几秒，于是 `await C.api('/api/v1/caps')` 迟迟不返回，
         `setInterval` 永远装不上 ⇒ **这一页再也不会更新**：顶栏停在启动那一刻的
         状态、每个视图停在挂载时的空壳，而且服务端日志干干净净、零异常。
         心跳不依赖任何一步，所以它必须站在最前面。 */
      setInterval(refreshHealth, 2500);
      stage = 'wsConnect';
      wsConnect();
      /* 顺序：**先拿到第一份 health，再挂载视图**。
         反过来的话，每个视图在挂载时都会按"我还不知道相机怎么样"去问一遍相机端点，
         于是在"相机没在跑"的常态下，一次冷启动就白等 4 个端点 × 2 s，还会刷出一屏
         一模一样的错误。先问 health（11 ms）就能让所有视图一次都不用问。
         （这一步即使失败也只是"晚 2.5 s 补上"，因为心跳已经在跑了。） */
      stage = 'refreshHealth';
      await refreshHealth();
      stage = 'mountViews';
      renderNavHeads();
      mountViews();
      /* 按 URL 落位：带着 `#/<子系统>/<模块>` 进来的（刷新 / 贴链接）直接开那个界面，
         否则停在入口页。 */
      stage = 'route';
      routeFromHash();
      /* 能力清单**不 await、也不在关键路径上**：
         它要去问设备（`caps()` 以前会枚举串口 + 逐条读参数），链路刚断时能把
         服务端拖住十几秒 —— 而界面里根本没人读它的结果（`S.caps` 只写不读，
         留着是为了将来某个视图要看"这台设备支持什么"）。 */
      stage = 'caps';
      C.guard(function () { return C.api('/api/v1/caps'); }).then(function (caps) {
        if (caps) S.caps = caps;
      });
      window.addEventListener('beforeunload', function () {
        if (S.ws) { S.ws.onclose = null; S.ws.close(); }
      });
    } catch (e) {
      bootFail(stage, e);
    }
  });
})();
