/* 小车控制 · 实时状态与参数设定 —— 「把车调好」的那一段（四个区块）。

   ## 改版十六（上游 2.5.0）：参数表 24 → 41，多了「运动控制」一组
   上游把课件《电脑鼠参数调整表》除速度表外的参数全量入表（id 39–55，17 项，
   仅远程档生效），分组标签由上游 `PARAM_GROUPS` 给出 ⇒ 本页**零结构改动**：
   分组、说明列、SAVE 按钮页合并全部现算。每项的「说明」列直接显示上游
   `PARAM_META` 的功能短句（后端已清洗 ⚠️/破折号），现场不用翻文档就知道
   这个参数管什么、默认多少、会不会被覆写。

   ## 改版十二（上游 2.4.0）：四卡 → 三卡，这一页变宽了
   上游 2.4.0 把编译档位由 5 档压到 2 档（远程档 / 按键比赛档），并**删掉课程调参整层**。
   于是「小车控制」按**现场真会做的三件事**重排，本页接的是第一件：
     · 实时读数（唯一那条统一帧的全部通道）
     · 参数表（生产命名空间 24 项）
     · 手动运动   ← 原「运动控制」卡，现挂 `C.parts.motion`
     · 标定例程   ← 原「校准例程」卡，现挂 `C.parts.calib`
   后两个原先是独立模块，现在**并进这一页**：它们与参数设定是同一个现场阶段里的连续动作，
   共用同一组门控（"连上电脑鼠"）。合成一页之后顺序就是现场顺序：
   先看状态 → 再调参数 → 需要就手动动一下 → 标定。

   ⚠️ 「取消独立参数标定」指的是上游删掉的**课程调参整层**（`lesson_lab.c`，
   课程 14 项死参数）—— 不是这里的「标定例程」。生产标定（GYRO / 壁距 / 红外逐点 /
   角度 / 里程，Flash page 115）上游一行没删，远程档下照常可跑，**保留**。

   ## 一次改版（上游 2.0.0「传输模式重构」）
   原先这里是**两个**模块：「实时参数」读 fast/mid/slow 三档流 + 参数表；
   「分组测量」用 `SNAP` 一问一答地点查全部测量量（含红外/电源）+ SCOPE 剖面。
   2.0.0 把 `SNAP` / `SCOPE` **整套删除**，测量量只剩**一条统一帧**（53 B，
   39 个扁平通道，红外 ADC+mm 双传、电池 mV、完整迷宫状态机都在里面。
   ⚠️ 2.3.0 起这条帧涨到 **49** 个通道：新增前墙双档 `frontNear`/`frontFar`/`frontBoth`
   与比赛冲刺状态 `spurtMode`/`spurtFlag`/`goOrBack`/`spurtTestFlag`/`objectGoToFlag`/
   `goaheadnum`/`fastwayCount90` 共 10 个。**界面不写死通道数** —— 它读 `meas/meta`。）
   ⇒ 两者读的东西**变成了同一样**，于是合并为一页：
     · 上面 = 这条流的实时读数（原「分组测量」的红外/电源并进这里）；
     · 下面 = 参数表（原样保留）。
   界面仍要说清两种刷新方式的区别：读数随拉取走，参数表只在点【读取全部】时读一次。

   ## 一次修正（上游 2.1.0「拉取模式」）
   2.0.0 的"固件自时钟全速推流"在真机上把蓝牙透传模块的缓冲灌死了（那条链路端到端
   只有 ≈100~200 B/s，而自时钟压上去 ≈4.3 KB/s），且**不会自行恢复**（须断电）。
   2.1.0 改成**上位机逐条拉取**（`PC → TELEM`，设备回一帧 + 时间锚）：
   节奏完全由本机决定，停发就让缓冲自然排空、**无需断电即可自愈**。
   ⇒ 对**这一页**的影响只有一条，但很显眼：**帧率不再是 217 Hz**。
   真机实测 **4.4~6.5 Hz**（由链路往返 1/RTT 决定，不是软件限速）。

   ## 速率一律用**服务端量出来的**
   协议常量 `UNIFIED_HZ = 217.0` 只是"纯串口线"上的理论上限（2.1.0 起上游也把它降级成
   "只用于缓冲容量估算"）。真机实测帧率是它的**约 1/50**。
   所以这里显示实测值（`telemetry.rate.unified.hz`），协议常量只作对照 ——
   把 217 当成"现在在跑的速率"会让现场把整整 50 倍的落差看成正常。

   ## `EVQ` 必须显式报出来
   帧 `flags` 的 bit7（`EVQ`）意味着"有事件行被丢弃 ⇒ 事件流不完整"。
   少了这句话，界面会假装那份事件流是完整的 —— 那比缺数据更坏。 */
C.mods.mod_params = (function () {
  let el, groups = {}, dirty = {}, meta = null;

  /* 「实时读数」到底多"实时"：**量出来的**，不是标称的。
     两样分开显示，因为它们回答不同的问题：
       · 界面 FPS —— 这条 WS 消息每秒到几条（前端自己数，最诚实）；
       · 帧率 / 年龄 —— 服务端用样本差分量的，以及"延时到底多大"。 */
  let fpsT = [];
  let paramStamp = null;      // 参数表上次刷新时刻（毫秒）

  function noteMsg() {
    const t = Date.now();
    fpsT.push(t);
    while (fpsT.length > 2 && (t - fpsT[0]) > 3000) fpsT.shift();
  }

  function fpsText() {
    if (fpsT.length < 2) return '界面 —';
    const span = (fpsT[fpsT.length - 1] - fpsT[0]) / 1000;
    if (span < 0.5) return '界面 —';
    return '界面 ' + C.util.fmt((fpsT.length - 1) / span, 1) + ' Hz';
  }

  /* ------------------------------------------------------------ 读数分组
     分组只是**看的顺序**，不是协议概念（协议里几十个键是平铺的）。
     契约：`keys` 里的每个键名都出自上游 `UNIFIED_CHANNELS` —— **键名照上游原样**
     标在行上，因为现场对着上游文档查字段时，能对得上的只有键名。

     ⚠️ 单位只写**上游明确标过**的那些（`link.h` 的 `LINK_U_*` 注释 / 设计方案 §4 表）：
       speedL/R = cm/s、trackL/trackR/cellOdom = mm、ir* = mm、battMv = mV、ms8 = ms。
       `gyroAngle` / `encAngle` / `pidErr` 上游**没标单位** ⇒ 这里也**不臆造**一个
       （写错的单位比没有单位更坏：现场会拿它去换算）。
     第 3、4 项分别是单位与小数位，省略即"无单位 / 整数"。

     ## 2.3.0 新增的两组（前墙双档 + 比赛冲刺状态）
     这两组是**这一页与「比赛」页共用的量**：这一页只把它们当"传感器/状态机"平铺出来
     （现场调参时要知道"前墙现在算的是近端还是远端"）；「比赛」页负责解释它们。
     ⚠️ **恒等式**：`frontNear | frontFar == wallFront`，且二者互斥（码表 0=无墙 /
     1=近端（贴脸）/ 2=远端（提前量））。分组里把它俩并排放在 `wallFront` 旁边，
     就是为了让这个恒等式在界面上一眼可比 —— 对不上就是固件那一档的判断出问题了。 */
  const CH_GROUPS = [
    { name: '运动与姿态', keys: [
      ['speedL', '左轮速', ' cm/s', 1], ['speedR', '右轮速', ' cm/s', 1],
      ['pidErr', '控制误差'],
      ['gyroAngle', '陀螺角'], ['encAngle', '编码器角'],
      ['motionTask', '动作'], ['motionStep', '动作步'], ['dir', '朝向'],
    ] },
    { name: '里程与位置', keys: [
      ['trackL', '左里程', ' mm', 0], ['trackR', '右里程', ' mm', 0],
      ['cellOdom', '格内里程', ' mm', 0],
      ['posX', '位置 X'], ['posY', '位置 Y'],
      ['goalX', '目标 X'], ['goalY', '目标 Y'],
      ['stackIdx', '岔路栈'], ['fastwayCount', '路径动作计数'],
    ] },
    { name: '迷宫判定', keys: [
      ['mazeTask', '迷宫任务'],
      ['wallLeft', '墙·左'], ['wallFront', '墙·前'], ['wallRight', '墙·右'],
      ['frontNear', '前墙·近端'], ['frontFar', '前墙·远端'], ['frontBoth', '前墙·双档'],
      ['critL', '临界·左'], ['critR', '临界·右'],
      ['left45', '左 45°'], ['right45', '右 45°'],
      ['wallThresh', '侧墙阈值'], ['irSwitch', '红外档位'],
    ] },
    { name: '比赛冲刺（2.3.0）', keys: [
      ['spurtMode', '冲刺档位'], ['spurtFlag', '冲刺标志'],
      ['goOrBack', '去/回'], ['spurtTestFlag', '冲刺试探'],
      ['objectGoToFlag', '物体去向'], ['goaheadnum', '前冲计数'],
      ['fastwayCount90', '90° 快路计数'],
    ] },
    { name: '四路红外', keys: [
      ['irF1', '前左', ' mm', 0], ['irF2', '前右', ' mm', 0],
      ['irL', '左', ' mm', 0], ['irR', '右', ' mm', 0],
      ['irAdcF1', '前左 ADC'], ['irAdcF2', '前右 ADC'],
      ['irAdcL', '左 ADC'], ['irAdcR', '右 ADC'],
    ] },
    { name: '电源与链路', keys: [
      ['battMv', '电池', ' mV', 0], ['ms8', '帧时基低 8 位', ' ms', 0],
      ['dropCnt', '丢帧计数'], ['flags', '标志位'],
    ] },
  ];

  /* 哪些键要拿**码表**翻成人话。表来自 `/api/v1/mouse/meas/meta`（静态，零设备访问）。
     `spurtMode` / `frontNear` 的码表是 2.3.0 新增的，`meta.spurt.modes`/`meta.spurt.front`。 */
  const CODE_OF = {
    dir: 'dirs', mazeTask: 'tasks', motionTask: 'motions',
    spurtMode: 'spurtModes', frontNear: 'frontNames', frontFar: 'frontNames',
    frontBoth: 'frontNames', goOrBack: 'goOrBack',
  };

  let chans = [];             // 这条流里**实际有**的通道名（meta 或 WS 消息里带来的）

  function applyChans(list) {
    if (list && list.length) chans = list.slice();
  }

  /* 不在任何分组里的键也要画出来（"其他通道"）——
     上游**下次加字段**时，界面不许静默地把它藏掉：藏掉就等于"这个量不存在"。
     反过来，分组里有、而这条流里没有的键**不画**（老固件/字段改名时不至于显示一排 `—`）。 */
  function liveGroups() {
    const known = {};
    CH_GROUPS.forEach(function (g) { g.keys.forEach(function (r) { known[r[0]] = 1; }); });
    const out = CH_GROUPS.map(function (g) {
      return { name: g.name, keys: g.keys.filter(function (r) { return !chans.length || chans.indexOf(r[0]) >= 0; }) };
    }).filter(function (g) { return g.keys.length; });
    const rest = chans.filter(function (k) { return !known[k]; });
    if (rest.length) {
      out.push({ name: '其他通道', keys: rest.map(function (k) { return [k, k]; }) });
    }
    return out;
  }

  /* 码表查名字。**没查到就回原码**（不臆造）—— 老上游没这些表时，
     数字照样印出来，只是没有名字，而不是显示 `—`。 */
  function codeName(kind, v) {
    const m = meta && meta[kind];
    if (!m || v == null) return null;
    if (kind === 'spurtModes') return (m[String(v)] || null);
    return m[String(v)] || null;
  }

  /* flags 是**位掩码**：只显示当前置起来的位（全 0 就写"无"）。
     直接显示十进制数字在现场毫无用处 —— 要知道的是"现在是自由模式还是急停锁存"。 */
  function flagsText(v) {
    const m = (meta && meta.flags) || null;
    if (v == null) return '—';
    if (!m) return String(v);
    const on = Object.keys(m).filter(function (b) { return Number(v) & Number(b); })
      .map(function (b) { return m[b]; });
    return (on.length ? on.join(' + ') : '无') + ' (' + v + ')';
  }

  /* 一行读数：`标签 <code>键名</code>` + 值。键名和标签都要有 ——
     标签是给人读的，键名是给"对着上游文档查字段"用的。 */
  function row(key, label, v, unit, dec) {
    let txt;
    if (v == null) {
      txt = '—';
    } else if (key === 'flags') {
      txt = flagsText(v);
    } else if (CODE_OF[key]) {
      const n = codeName(CODE_OF[key], v);
      txt = n ? (n + ' (' + v + ')') : String(v);
    } else if (typeof v === 'boolean') {
      txt = v ? '是' : '否';
    } else {
      txt = C.util.fmt(v, dec == null ? 0 : dec) + (unit || '');
    }
    return '<b>' + C.util.esc(label) + ' <code>' + C.util.esc(key) + '</code></b>' +
      '<span>' + C.util.esc(txt) + '</span>';
  }

  /* ------------------------------------------------------------ 渲染 */
  function renderLive() {
    const t = C.state.telemetry || {};
    const rec = t.unified || null;
    applyChans(t.channels);

    const fps = el.querySelector('#mp-fps');
    if (fps) fps.textContent = fpsText();

    /* 流概览一行：这条流有多少通道、**实测**多少 Hz、最新一帧多久以前。
       协议常量（217 Hz）只作参照，且必须标成"协议常量"而不是"标称" ——
       2.1.0 起真机实测是它的约 1/50，把两个数并排写成"实测 / 标称"会被读成
       "只差一点"，那正好掩盖了唯一要紧的事实。 */
    const r = (t.rate || {}).unified || {};
    const st = el.querySelector('#mp-stream');
    if (st) {
      if (!chans.length && !rec) {
        st.textContent = '';
      } else {
        const hz = (r.hz == null) ? '—' : (C.util.fmt(r.hz, 1) + ' Hz');
        const nom = (r.nominal == null) ? '' : ('（协议常量 ' + C.util.fmt(r.nominal, 0) + '）');
        const age = (r.age_ms == null) ? '—'
          : (r.age_ms < 1000 ? (C.util.fmt(r.age_ms, 0) + ' ms')
                             : (C.util.fmt(r.age_ms / 1000, 1) + ' s'));
        st.textContent = '统一帧 ' + chans.length + ' 通道 · 实测 ' + hz + nom +
          ' · 最新一帧 ' + age + ' 前 · 合计 ' + C.util.fmt(t.bps, 1) + ' B/s' +
          (t.streaming ? '' : ' · 流已停');
      }
    }

    /* 没数据时**也要把"这条流里有什么"列出来**（通道名来自 `meas/meta`，静态表）。
       否则未连接时这一页是空白 —— 而准备阶段恰恰是最该看它的时候。 */
    const box = el.querySelector('#mp-live');
    if (!box) return;
    if (!rec) {
      box.innerHTML = chans.length
        ? '<div class="hint">还没收到数据。这条流的通道（' + chans.length + ' 个）：</div>' +
          '<div class="hint" data-raw style="font-family:var(--mono)">' +
          C.util.esc(chans.join(' ')) + '</div>'
        : '<div class="hint">未连接电脑鼠</div>';
      return;
    }
    box.innerHTML = liveGroups().map(function (g) {
      return '<div class="hint" style="margin-top:6px">' + C.util.esc(g.name) + '</div>' +
        '<div class="kv">' + g.keys.map(function (a) {
          return row(a[0], a[1], rec[a[0]], a[2], a[3]);
        }).join('') + '</div>';
    }).join('');
  }

  /* 「不动」的下一步点哪。只有一条流 ⇒ 空档无非两种：
     `stalled`（没有样本在来）或明显偏大的年龄（停了但连接还在）。
     两种都要给**可照做的**动作，而不是只报数字。

     ⚠️ 2.1.0 的措辞换了：遥测不再是"设备在推、我们收"，而是"**本机逐条去拉**"。
     所以"没数据"的第一嫌疑是**拉取器没在跑**（`mc.stream.on()` 没开），
     第二才是设备/链路。写成"必须发一次 STREAM ON"会指错方向 —— 那条命令已经不存在了。 */
  function renderLag() {
    const t = C.state.telemetry || {};
    const r = (t.rate || {}).unified || {};
    const lag = el.querySelector('#mp-lag');
    if (!lag) return;
    if (!t.rate) { lag.textContent = ''; return; }
    if (r.stalled) {
      const inflight = ((r.pull || {}).inflight);
      const detail = (inflight == null) ? '' : ('（在途 ' + inflight + ' 条）');
      lag.textContent = '没有数据在来' + detail + ' ⇒ 先确认已连接，'
        + '再看顶栏【电脑鼠】窗口里的遥测开关是否打开。';
    } else if (r.age_ms != null && r.age_ms > 1500) {
      lag.textContent = '最新一帧已是 ' + C.util.fmt(r.age_ms / 1000, 1)
        + ' s 前 ⇒ 拉取停了。'
        + '⇒ 查 USB 线与串口占用；或把遥测开关关片刻再开（排空蓝牙缓冲，不用断电）。';
    } else {
      lag.textContent = '';
    }
  }

  /* `EVQ`（帧 flags bit7）= 有事件行被丢弃 ⇒ 事件流不完整。
     这是硬事实，必须有一块专门的地方说出来，不能只藏在 flags 那一行里。 */
  function renderFault() {
    const f = el.querySelector('#mp-fault');
    if (!f) return;
    const t = C.state.telemetry || {};
    const ev = t.events || {};
    const rec = t.unified || {};
    const bad = (ev.incomplete === true) || rec.evq === true;
    if (!bad) { f.textContent = ''; return; }
    const lost = (ev.ev_lost == null) ? '—' : ev.ev_lost;
    f.textContent = '事件流不完整（EVQ）：已丢 ' + lost + ' 条 · 累计缺口 '
      + ((ev.ev_gaps == null) ? '—' : ev.ev_gaps)
      + '\n⇒ 复位设备后重跑一次，才能拿到完整过程';
  }

  /* 参数表**不是**自动刷新的：只在点【读取全部】时读一次。
     不写这一句的话，现场会把"参数一直不动"当成读失败（真踩过）。 */
  function renderParamAge() {
    const pa = el.querySelector('#mp-param-age');
    if (pa) {
      pa.textContent = paramStamp
        ? ('读取于 ' + new Date(paramStamp).toLocaleTimeString() + ' · 按需刷新，非自动')
        : '未读取 · 按【读取全部】';
    }
  }

  function renderAll() { renderLive(); renderLag(); renderFault(); renderParamAge(); }

  function mount(host) {
    el = host;
    el.innerHTML = `
      <div class="pane">
        <h4>实时读数 <span class="hint" id="mp-fps"></span>
          <span><button class="mini" id="mp-tare">清零 TARE</button></span></h4>
        <div class="hint" id="mp-stream"></div>
        <div class="hint" id="mp-lag"></div>
        <div class="errbox" id="mp-fault"></div>
        <div id="mp-live"></div>
      </div>
      <div class="pane" style="margin-top:8px">
        <h4>全部参数 <span class="hint" id="mp-param-age"></span>
          <span>
            <label class="hint"><input type="checkbox" id="mp-readback" checked> 回读校验</label>
            <button class="mini" id="mp-reload">读取全部</button>
            <button class="mini" id="mp-write">写入改动</button>
          </span></h4>
        <div id="mp-groups"></div>
        <div class="row wrap" style="margin-top:8px" id="mp-saves"></div>
        <div class="hint" id="mp-msg"></div>
      </div>`;
    el.querySelector('#mp-reload').onclick = function () { load(); };
    el.querySelector('#mp-write').onclick = function () { write(); };
    el.querySelector('#mp-tare').onclick = function () { tare(); };
    el.querySelector('#mp-groups').addEventListener('input', function (e) {
      if (e.target.dataset.name) dirty[e.target.dataset.name] = e.target.value;
    });
    /* 两个**区块**（原「运动控制」「校准例程」两张卡）挂进本页 ——
       它们已经是 `C.parts.*`，各自 mount 时**追加自己的 pane**，
       与上面的读数/参数共享同一个 `el`（所以 id 前缀 mm-/mc- 必须与 mp- 不撞）。 */
    if (C.parts.motion) C.parts.motion.mount(el);
    if (C.parts.calib) C.parts.calib.mount(el);
    loadMeta();
    renderAll();
    load();
  }

  /* 码表（静态，零设备访问）：通道清单 + 迷宫/冲刺码表。
     未连接时它是这一页**唯一**能画出来的东西，所以进来就读一次。

     ⚠️ `meta.spurt`（`modes` / `front` 两张表）是 2.3.0 新增的 —— 老上游没有，
     拿不到就退化成"只印原码"（`codeName` 返回 null ⇒ 走 `String(v)`）。
     这里**不设** fallback 表：上游改了档位编号，界面写死的表就会骗人。 */
  async function loadMeta() {
    const d = await C.guard(function () { return C.api('/api/v1/mouse/meas/meta'); }, '读码表');
    const mz = (d && d.maze) || {};
    const sp = (d && d.spurt) || {};
    meta = Object.assign({}, mz);
    if (sp.modes) meta.spurtModes = sp.modes;
    if (sp.front) meta.frontNames = sp.front;
    applyChans(d && d.channels);
    renderAll();
  }

  /* SAVE 按钮**按数据里的 flash_page 现生成**，不写死页号。
     页号属于固件分区，会随上游调整而变（1.5.4 就把生产页从 99 挪到了 115）——
     写死在界面里等于给现场留一个会悄悄过期的数字。同页的几组共用一个按钮：
     生产命名空间是**一次写整表**，三组同页，拆三个按钮反而像三件事。 */
  function renderSaves() {
    const box = el.querySelector('#mp-saves');
    if (!box) return;
    const byPage = {};
    Object.keys(groups).forEach(function (g) {
      const items = groups[g];
      const n = Object.keys(items).length;
      if (!n) return;
      const p = items[Object.keys(items)[0]].flash_page;
      (byPage[p] = byPage[p] || []).push(g);
    });
    const pages = Object.keys(byPage);
    if (!pages.length) { box.innerHTML = ''; return; }
    box.innerHTML = pages.map(function (p) {
      return '<button class="mini" data-page="' + C.util.esc(p) + '">SAVE ' +
        C.util.esc(byPage[p].join(' + ')) + ' · page ' + C.util.esc(p) + '</button>';
    }).join('');
    box.querySelectorAll('[data-page]').forEach(function (b) {
      const gs = byPage[b.dataset.page];
      b.onclick = function () { saveGroup(gs[0], gs); };
      C.gate(b, C.needMouse());       // 现生成的按钮要当场门控（onHealth 只管当时存在的）
    });
  }

  async function tare() {
    const d = await C.guard(function () {
      return C.api('/api/v1/mouse/tare', { method: 'POST', body: {} });
    }, '清零累加量');
    if (d) C.toast('已下发 TARE：陀螺角累加器与里程基准清零', 'ok', 9000);
  }

  async function load() {
    if (C.needMouse()) {
      el.querySelector('#mp-groups').innerHTML = '<div class="hint">未连接电脑鼠</div>';
      return;
    }
    const d = await C.guard(function () {
      return C.api('/api/v1/mouse/params/grouped');
    }, '读参数');
    if (!d) { el.querySelector('#mp-groups').innerHTML = '<div class="hint">参数读取失败</div>'; return; }
    groups = d.groups || {};
    const prod = groups['生产标定'] || {};
    ['SPEEDL', 'SPEEDR'].forEach(function (n) { if (prod[n]) prod[n]._derived = true; });
    dirty = {};
    render(d);
  }

  function render(d) {
    const keys = Object.keys(groups).filter(function (k) { return Object.keys(groups[k]).length; });
    if (!keys.length) {
      el.querySelector('#mp-groups').innerHTML = '<div class="hint">无参数数据</div>';
      renderSaves();
      return;
    }
    el.querySelector('#mp-groups').innerHTML = keys.map(function (g) {
      const items = groups[g];
      const names = Object.keys(items);
      const it0 = items[names[0]] || {};
      return '<div class="pane" style="margin-top:8px">' +
        '<h4>' + C.util.esc(g) + ' · ' + names.length + ' 项' +
        ' <span class="hint">Flash page ' + C.util.esc(it0.flash_page) +
        ' · <code>' + C.util.esc(it0.write_via || '') + '</code>' +
        '</span></h4>' +
        '<div class="scroll"><table><thead><tr><th>id</th><th>名字</th><th>十进制</th>' +
        '<th>Q16.16</th><th>单位</th><th>说明</th></tr></thead><tbody>' +
        names.map(function (n) {
          const it = items[n];
          const cur = (dirty[n] !== undefined) ? dirty[n]
            : (it.readable === false ? '' : C.util.fmt(it.value, 6));
          const cell = it._derived
            ? '<span class="dim" title="派生量 = TRACK/100">' + C.util.fmt(it.value, 6) +
              ' 派生</span>'
            : '<input data-name="' + C.util.esc(n) + '" value="' + C.util.esc(cur) + '"' +
              (it.readable === false ? ' placeholder="填入新值"' : '') + ' style="width:120px">';
          const q = (it.readable === false)
            ? '<span class="dim" title="GET 不覆盖这一项">—</span>'
            : C.util.esc(C.util.fmt(it.q));
          return '<tr><td>' + C.util.esc(C.util.fmt(it.id)) + '</td>' +
            '<td><b>' + C.util.esc(n) + '</b></td><td>' + cell + '</td>' +
            '<td>' + q + '</td>' +
            '<td>' + C.util.esc(it.unit || '') + '</td>' +
            '<td>' + C.util.esc(it.desc || '') + '</td></tr>';
        }).join('') + '</tbody></table></div></div>';
    }).join('');
    msg('共 ' + (d.count || 0) + ' 项，分 ' + keys.length + ' 组');
    renderSaves();
    // 记下这次读取的时刻：界面据此写出"读取于 …，按需刷新" ——
    // 参数表**不会**自己动（它是命令往返，不是流），这句就是给它的自证。
    paramStamp = Date.now();
    renderParamAge();
  }

  function msg(t) { const m = el.querySelector('#mp-msg'); if (m) m.textContent = t; }

  async function write() {
    const ks = Object.keys(dirty);
    if (!ks.length) { msg('无改动'); return; }
    const body = { values: {}, readback: el.querySelector('#mp-readback').checked };
    ks.forEach(function (k) { body.values[k] = Number(dirty[k]); });
    const d = await C.guard(function () {
      return C.api('/api/v1/mouse/params/grouped', { method: 'POST', body: body });
    }, '写参数');
    if (!d) return;
    /* 2.4.0 起**没有课程组** ⇒ 只剩 `prod` 一条回执。保留 `lesson` 的兜底写法
       （老上游 2.3.x 还会回它），但**不再**把它当成常态。 */
    const all = (d.prod || []).concat(d.lesson || []);
    const bad = all.filter(function (r) { return !r.ok; }).length;
    msg(all.map(function (r) {
      return (r.ok ? 'OK  ' : 'FAIL ') + r.name + ' status=' + r.status;
    }).join('\n'));
    C.toast(bad ? (bad + ' 条写入失败') : ('全部写入成功 ' + all.length + ' 条'),
      bad ? 'bad' : 'ok');
    dirty = {};
    load();
  }

  /* SAVE = **写 Flash**（比 SET 更重：擦写会打断 1 kHz 控制环）。
     生产侧几组同页、固件一次写整表，所以按钮按页合并。 */
  async function saveGroup(group, groupList) {
    const label = (groupList && groupList.length > 1) ? groupList.join(' + ') : group;
    const vals = {};
    (groupList || [group]).forEach(function (g) {
      const items = groups[g] || {};
      Object.keys(items).forEach(function (n) {
        if (dirty[n] !== undefined && dirty[n] !== '') vals[n] = Number(dirty[n]);
      });
    });
    if (!confirm('把「' + label + '」写入 Flash？')) return;
    const body = { group: group };
    if (Object.keys(vals).length) body.values = vals;
    const d = await C.guard(function () {
      return C.api('/api/v1/mouse/params/save_group', { method: 'POST', body: body });
    }, 'SAVE');
    if (d) msg('已写入 page ' + C.util.fmt(d.flash_page) + (d.backup ? (' · 备份 ' + d.backup) : ''));
  }

  /* 把一次 health 广播转给两个区块 —— 它们是独立挂载的，收不到本页的 onHealth。 */
  function fanHealth() {
    if (C.parts.motion && C.parts.motion.onHealth) C.parts.motion.onHealth();
    if (C.parts.calib && C.parts.calib.onHealth) C.parts.calib.onHealth();
  }

  return {
    mount: mount,
    activate: function () {
      if (!meta) loadMeta();
      if (!Object.keys(groups).length) load();
      if (C.parts.calib && C.parts.calib.activate) C.parts.calib.activate();
    },
    onMsg: function (m) {
      if (C.parts.calib && C.parts.calib.onMsg) C.parts.calib.onMsg(m);
      if (m.type !== 'mouse.telemetry') return;
      noteMsg();                 // 界面 FPS 由**前端自己数**到达时刻（最诚实）
      renderAll();
    },
    onHealth: function () {
      const why = C.needMouse();
      ['mp-reload', 'mp-write', 'mp-tare'].forEach(function (id) {
        C.gate(el.querySelector('#' + id), why);
      });
      el.querySelectorAll('#mp-saves [data-page]').forEach(function (b) { C.gate(b, why); });
      fanHealth();
    },
  };
})();
