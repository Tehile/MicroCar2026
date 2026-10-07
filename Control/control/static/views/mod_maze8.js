/* 小车控制 · 无记忆走迷宫（三栏 + 记录 / 复盘）
 *
 * ## 改名（改版十二 · 上游 2.4.0）
 * 上游把"实验八剖面"正式收进了 `MAZE RUN BLIND`（无记忆走迷宫）这条命令，
 * 而新增的 `MAZE RUN RACE`（比赛）是**另一种跑法**。⇒ 这一页从「实验 8」
 * 改叫「无记忆走迷宫」，与「比赛」并列；内部三栏与记录/复盘**一点没动**。
 *（原来的"实验 8"是下游的叫法，上游 2.4.0 才把这两个目标词显式写进命令。）
 *
 * ## 为什么是三栏
 * 跑迷宫时要同时看三样**不同时间尺度**的东西，缺一样就得来回找：
 *   · **左** 此刻的状态机与传感器 —— "它现在在干什么"（秒级）
 *   · **中** 重构出来的迷宫 —— "它认为世界长什么样"（累积，越跑越清楚）
 *   · **右** 决策与迁移的历程 —— "它一路是怎么想的"（整场，要能翻回去）
 * 三者由**同一个时间游标**驱动：实时态跟着帧走，复盘态跟着游标走。
 * 所有"现在是什么"都走 `cur()` 这一个出口 —— 否则迟早出现"左栏是现在、中栏是过去"。
 *
 * ## 中栏那张图是**主机重建**的，不是固件给的
 * 每帧都带 `wallLeft/wallFront/wallRight`（**相对车身**）与 `posX/posY/dir`，
 * 所以主机能随走随建。好处：零额外带宽（不用反复 `maze.dump`，真机一次往返 ≈1.21 s）、
 * 随帧更新。固件自述的那份（`GET /mouse/maze`）**只作对账**，界面点一下才拉。
 * ⚠️ 图例必须画出来（实线=有墙 / 虚线=未知 / 无线=确认通）——
 * 把"不知道"画成"没有墙"是这一页最容易犯也最危险的错。
 *
 * ## 记录与复盘
 *   ·【发车】= 发车 + **自动开始记录**（顺序在后端保证：先开记录再发车，
 *     否则开头那几帧——"它一开始看到了什么"——会丢）。
 *   · 停止：自动判据（`STOPPED` 事件 / 任务机回 WAIT 持续 3 s / 链路断开）
 *     **+【停止并保存】人工兜底**。固件**没有"迷宫结束"事件**，所以自动判据是**弱判据**；
 *     没有兜底，模拟器上根本收不了尾（见 §11.9）。
 *   · 记录落 `records/maze/*.maze.json`（自包含：全量 39 通道每帧 + 迁移 + 事件 + 完整性）。
 *   · 复盘 = 导入（本机列表 / 上传文件）→ 时间游标 → 三栏联动。
 *     复盘时**实时数据不再驱动界面**（`onMsg` 里对 `rep` 早退）——
 *     否则"过去的画面"会被此刻到的帧冲掉，那就不是复盘了。
 *
 * ## 收车 / 救援（2.1.1 / 2.2.0）
 * 上游 2.1.1 之前，发过车之后**没有任何**复位发车状态的路径，只能断电（真机复现）。
 * 现在有两个动作，**分工不同、都要有**：
 *   · 【收车】`MAZE STOP` —— 停车 + 复位发车状态 + 解锁急停。**车在运动时不可靠**
 *     （标志检查在 `LESSON_Blind8()` 的 while(1) 顶部，而主循环有 30 余处非协作式
 *     阻塞等待，运动时程序卡在那些循环内部）⇒ 所以按完**当场看任务机**，
 *     还在 SEARCH 就提示改用救援。
 *   · 【救援】`MCU RESET` —— 整机软复位，**可靠**（BSS 清零把发车状态一起清掉）。
 *     代价：复位**不复位物理位置**，且**约 3 s 才有新帧** ⇒ 两条都写进提示里。
 * ⚠️ 这两个都是**动车**，与【停止并保存】（存盘）是三件事，不合并 ——
 * 合并之后"车还在跑时先把已收到的存一份"就做不到了。
 *
 * ## 2.1.0 起帧率不是 217 Hz
 * 遥测改为上位机逐条拉取（`TELEM`），帧率 = 1/RTT，真机 **4.4~6.5 Hz**。
 * 所以这一页**不写 ≈217 Hz**，只报实测帧率。
 *
 * ## `EVQ` 必须显式报出来（两个态各报各的）
 * 实时态报的是"现在"（帧 `flags` bit7 / `link_stats` 的 `ev_lost`）；
 * 复盘态报的是"**这份记录当时**"（文件里的 `integrity.evq_seen`）。
 * 两者不能混 —— 拿"现在的完整性"去解释"过去的一段数据"是错的。 */
C.mods.mod_maze8 = (function () {
  let el, meta = null, state = null, timer = null;
  let wall = null;               // 中栏实时那张图（上游环形缓冲重建）
  let rec = null;                // 记录状态（GET /maze/record）
  let pre = null;                // 发车前低电预检（GET /launch_precheck，纯读帧）
  let live = null;               // 实时决策流（GET /maze/decisions）
  let rep = null;                // 回放态：{doc, name, ts, idx, playing, wall, track, live}
  let repTimer = null;
  let tlAnchor = -1;             // 右栏已经滚到过哪一条（避免每 1 s 重渲染把用户的滚动顶掉）

  /* ------------------------------------------------------------ 骨架 */
  function mount(host) {
    el = host;
  /* **安全绑定**：`el.querySelector(id)` 找不到时**只警告、不炸**。

     ⚠️ 为什么需要它（本层踩过两次，第二次就是本轮）：`mount()` 里写
     `el.querySelector('#x').onclick = …`，只要那个 id 不在 HTML 里，就会
     `TypeError: Cannot set properties of null` ⇒ **整个 view 挂载失败**、
     三栏全空、`C.mods` 里那一条注册不上 —— 而报错信息只指到"某个 onclick"，
     完全不提是哪个 id 不见了（真踩过：删了一个按钮的元素、忘了删它的绑定）。

     跳过之后**不会掩盖问题**：B30 会逐 id 检查关键控件在不在（那是专门的判据）。
     所以这里的取舍是"缺元素时报一条能读的日志 + 由判据兜底"，
     而不是"让整页挂掉" —— 后者把一个小遗漏放大成"这个模块不可用"。 */
  function bind(id, ev, fn) {
    const node = document.querySelector(id) || (el && el.querySelector(id));
    if (!node) {
      try { console.error('无记忆走迷宫：界面元素缺失，无法绑定 ' + id); } catch (e) { /* 忽略 */ }
      return null;
    }
    node[ev] = fn;
    return node;
  }

    el.innerHTML = `
      <div class="pane">
        <h4>无记忆走迷宫 <span class="hint" id="m8-msg"></span>
          <span>
            <button class="mini primary" id="m8-run">发车</button>
            <button class="mini" id="m8-mstop">收车</button>
            <button class="mini warn" id="m8-rescue">救援</button>
            <button class="mini" id="m8-stop">停止并保存</button>
            <button class="mini" id="m8-state">读取状态</button>
            <button class="mini" id="m8-map">读墙图</button>
            <button class="mini" id="m8-rst">清本层差分</button>
          </span></h4>
        <div class="gatehint" id="m8-hint"></div>
        <div class="errbox" id="m8-evfault"></div>
        <div class="row wrap" style="margin-top:6px">
          <span class="hint" id="m8-recinfo"></span>
          <span>
            <button class="mini" id="m8-reclist">记录列表</button>
            <button class="mini" id="m8-import">导入复盘</button>
            <input type="file" id="m8-file" accept=".json,application/json" style="display:none">
          </span>
        </div>
        <div id="m8-recbox"></div>
      </div>

      <div class="mz3">
        <div class="pane">
          <h4>实时状态 <span class="hint" id="m8-hz"></span></h4>
          <div class="kv" id="m8-kv"></div>
          <div class="hint" style="margin-top:6px">传感器</div>
          <div class="kv" id="m8-sens"></div>
        </div>
        <div class="pane">
          <h4>迷宫重构 <span class="hint" id="m8-winfo"></span></h4>
          <canvas id="m8-cv" style="width:100%"></canvas>
          <div class="hint" id="m8-wsrc"></div>
          <div class="kv" id="m8-wkv"></div>
        </div>
        <div class="pane">
          <h4>决策历程 <span class="hint" id="m8-tlcount"></span>
            <span><button class="mini" id="m8-ev-clear">刷新</button></span></h4>
          <!-- ★ 时间线进度条放在**这一栏的最顶上**：它驱动的正是下面这条时间线，
               以及左/中两栏的"此刻"。放在别处（比如整页顶部）会让人以为它只管某一部分。 -->
          <div class="tltop">
            <span class="runpill" id="m8-runpill">—</span>
            <span class="hint" id="m8-tlwhen"></span>
            <span style="margin-left:auto">
              <button class="mini" id="m8-rep-enter">进入回放</button>
            </span>
          </div>
          <input type="range" id="m8-tlsld" min="0" max="0" value="0" step="1">
          <div class="tltop" id="m8-repctl" style="display:none">
            <button class="mini" id="m8-rep-prev">◀ 上一帧</button>
            <button class="mini" id="m8-rep-play">播放</button>
            <button class="mini" id="m8-rep-next">下一帧 ▶</button>
            <span class="hint" id="m8-rep-t"></span>
            <button class="mini" id="m8-rep-exit" style="margin-left:auto">退出回放</button>
          </div>
          <div class="hint" id="m8-tlhint">决策按时间排列（点进度条或某一条可跳到那一刻）。</div>
          <div class="scroll" id="m8-tl"
            style="max-height:300px;font-family:var(--mono);font-size:11px"></div>
        </div>
      </div>

      <div class="pane" style="margin-top:8px">
        <h4>汇总 <span class="hint" id="m8-sum-hz"></span>
          <span><button class="mini" id="m8-sum">取汇总</button></span></h4>
        <div class="kv" id="m8-sum-kv"></div>
        <div class="hint" id="m8-sum-txt" data-raw
          style="white-space:pre-wrap;font-family:var(--mono);font-size:11px"></div>
      </div>`;

    bind('#m8-run', 'onclick', function () { run(); });
    bind('#m8-stop', 'onclick', function () { stopRec(); });
    bind('#m8-state', 'onclick', function () { loadState(); });
    bind('#m8-map', 'onclick', function () { loadFirmwareMap(); });
    bind('#m8-rst', 'onclick', function () { doReset(); });
    bind('#m8-mstop', 'onclick', function () { collect(); });
    bind('#m8-rescue', 'onclick', function () { rescue(); });
    bind('#m8-sum', 'onclick', function () { loadSummary(); });
    bind('#m8-reclist', 'onclick', function () { showRecords(); });
    bind('#m8-import', 'onclick', function () { el.querySelector('#m8-file').click(); });
    bind('#m8-file', 'onchange', function (e) { importFile(e.target.files[0]); });
    bind('#m8-ev-clear', 'onclick', function () {
      /* 这个按钮是【刷新】，不是"清空"。
         它原来叫【清空显示】—— 那时决策是前端自己攒的，清一下有意义；
         现在决策来自后端（每 1 s 全量拉），"清空"点了什么也不会发生。
         ⇒ 一个点了没反应的按钮比没有按钮更坏（现场会以为它坏了、或以为清空成功了）。
         改名成它真正做的事：立刻重拉一次（不等那 1 s 的定时器）。 */
      if (rep) return;               // 回放态的数据来自这份文件，重拉没有意义
      loadState(); loadDecisions(); loadRecStatus();
    });
    bind('#m8-rep-prev', 'onclick', function () { seekRep(repIdx() - 1); });
    bind('#m8-rep-next', 'onclick', function () { seekRep(repIdx() + 1); });
    bind('#m8-rep-play', 'onclick', function () { togglePlay(); });
    bind('#m8-rep-exit', 'onclick', function () { exitReplay(); });
    bind('#m8-rep-enter', 'onclick', function () { enterReplay(); });
    /* 时间线进度条 —— 它是"回放"的主控件：
         · 回放态：拖它 = 改重放时间点（左/中/右三栏一起跟着走）；
         · 实时态：拖它 = **自动进入回放**（并跳到拖到的位置）。
           为什么不做成"实时态禁用"：现场看到一屏数据想回看刚才那一刻，
           第一反应就是去拖它；禁用会让人以为"这功能只有复盘才有"。
           代价是拖动那一瞬要先取一次快照（异步）—— 期间滑块先按用户拖的值显示，
           取回来再落到真实帧上（不闪、不跳）。 */
    bind('#m8-tlsld', 'oninput', function (e) {
      const t = Number(e.target.value);
      if (!rep) {
        const pend = t;                 // 记下用户拖到的位置
        enterReplay().then(function () {
          if (rep) seekRepTime(pend); else renderTlBar();
        });
        return;
      }
      seekRepTime(t);
    });
    bind('#m8-tlsld', 'onchange', function () {
      // 松开滑块时若正在自动播放，停掉（手动定位优先于自动播放）
      if (rep && rep.playing) togglePlay();
    });
    // 右栏点一条 → 跳到那一刻（复盘态才有效；实时态没有"那一刻"可跳）
    el.querySelector('#m8-tl').addEventListener('click', function (e) {
      const d = e.target.closest('[data-tl]');
      if (!d || !rep) return;
      const i = Number(d.dataset.tl);
      if (!isNaN(i)) seekRep(i);
    });
    window.addEventListener('resize', drawMaze);
    loadMeta();
    loadPrecheck();
  }

  /* 门控行（`#m8-hint`）只有一行，所以要**合并**两个来源：
       ① 没连接时的"做什么才能用"（`C.needMouse()`）；
       ② 发车前低电预检的警告。
     合并规则：门控优先（连都没连上时，说电池没意义）。 */
  function renderHint() {
    const h = el && el.querySelector('#m8-hint');
    if (!h) return;
    const why = C.needMouse();
    if (why) { h.textContent = why; return; }
    h.textContent = (pre && pre.level === 'warn') ? (pre.note || '') : '';
  }

  /* ------------------------------------------------------------ 码表 */
  async function loadMeta() {
    const d = await C.guard(function () { return C.api('/api/v1/mouse/meas/meta'); }, '读迷宫码表');
    meta = (d && d.maze) || {};
    renderAll();
  }

  function taskName(v) {
    const T = (meta && meta.tasks) || {};
    return T[String(v)] || (v == null ? '—' : String(v));
  }
  function roadName(v) {
    const T = (meta && meta.roads) || {};
    return T[String(v)] || (v == null ? '—' : String(v));
  }
  function motionName(v) {
    const M = (meta && meta.motions) || {};
    return M[String(v)] || (v == null ? '—' : String(v));
  }
  function dirName(v) {
    const D = (meta && meta.dirs) || {};
    return D[String(v)] || (v == null ? '—' : String(v));
  }
  function flagsOf(f) {
    if (f == null) return [];
    const out = [];
    Object.keys((meta && meta.flags) || {}).forEach(function (bit) {
      if (Number(f) & Number(bit)) out.push(meta.flags[bit]);
    });
    return out;
  }

  /* ------------------------------------------------------------ 三栏联动的枢纽 */
  function cur() {
    /* 复盘态取"游标那一刻"的帧；实时态取最新帧。
       **所有"现在是什么"都必须走这里** —— 否则迟早出现"左栏是现在、中栏是过去"。 */
    if (rep) { const f = repFrame(); return f ? f.rec : null; }
    return (C.state.telemetry || {}).unified || null;
  }

  function repFrame() {
    /* 游标落在哪一帧。用**二分**而不是线性扫 —— 拖滑块会高频调用，
       而一盘迷宫有几千帧。 */
    if (!rep || !rep.doc) return null;
    const T = rep.doc.t_ms || [];
    if (!T.length) return null;
    const t = rep.ts[rep.idx];
    let lo = 0, hi = T.length - 1, best = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (T[mid] <= t) { best = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    if (best < 0) best = 0;
    const row = (rep.doc.frames || [])[best] || [];
    const ch = rep.doc.channels || [];
    const rec = {};
    ch.forEach(function (c, i) { rec[c] = row[i]; });
    return { idx: best, rec: rec, t_ms: T[best] };
  }

  function fmt(v, d, u) {
    /* ⚠️ 单位只写上游明确标过的（cm/s、mm、mV）。
       `gyroAngle` / `encAngle` 上游**没标单位** ⇒ 这里也**不给**
       （写错的单位比没有单位更坏：现场会拿它去换算）。 */
    if (v == null || isNaN(v)) return '—';
    return C.util.fmt(v, d == null ? 0 : d) + (u || '');
  }

  /* ------------------------------------------------------------ 左栏 */
  function renderLeft() {
    const box = el.querySelector('#m8-kv');
    const sb = el.querySelector('#m8-sens');
    const hz = el.querySelector('#m8-hz');
    if (!box || !sb) return;
    if (hz) {
      if (rep) hz.textContent = '复盘';
      else {
        const r = ((C.state.telemetry || {}).rate || {}).unified || {};
        hz.textContent = (r.hz == null) ? '' : ('实测 ' + C.util.fmt(r.hz, 1) + ' Hz');
      }
    }
    const f = cur();
    if (!f) {
      box.innerHTML = '<b>迷宫</b><span>' + (rep ? '复盘：没有帧' : '未运行') + '</span>';
      sb.innerHTML = '';
      return;
    }
    const st = state || {};
    const show = function (a) { return (a == null) ? '—' : a; };
    const wallTxt = function (v) { return (v == null) ? '—' : (v ? '有' : '无'); };
    /* 前墙双档（上游 2.3.0）：`frontNear` / `frontFar` 是**互斥**的两档，
       恒等式 `frontNear | frontFar == wallFront`。所以前墙那一格里写的是
       "近 / 远" 两档各自的**三态**（有 / 无 / —）。为什么两档都写：
       近端 = 前方最近一个有墙（贴脸，必须处理），远端 = 当前方向下一个判断有墙
       （提前量）—— 这两个现场做的动作不一样，只写一个"有墙"看不出该做什么。 */
    const frontTxt = function (near, far) {
      if (near == null && far == null) return '—';
      if (near && far) return '矛盾(两档同时置位)';    // 恒等式破了 —— 当场说出来
      if (near) return '近端';
      if (far) return '远端';
      return '无';
    };
    const frontNear = f.frontNear, frontFar = f.frontFar;
    /* ⚠️ 老上游（≤2.2.1）帧里**没有**这两个通道 ⇒ 两个都是 `undefined`（不是 0）。
       这时不许画成"无墙"（那是"确认没有"，与"读不到"是两件事）⇒ 写 `—` 并注明。 */
    const hasFront = (frontNear !== undefined && frontNear !== null) ||
                     (frontFar !== undefined && frontFar !== null);
    box.innerHTML = [
      ['任务', taskName(f.mazeTask)],
      ['路口', roadName(rep ? f.road : (st.road == null ? f.road : st.road))],
      ['开口(左/前/右)', wallTxt(f.wallLeft) + ' / ' + wallTxt(f.wallFront) + ' / ' + wallTxt(f.wallRight)],
      /* 前墙两档独立一行；读不到时如实写"这台设备没有这个量（升级上游）"。 */
      ['前墙近/远', hasFront ? (frontTxt(frontNear, frontFar) +
        '　（恒等式 ' + ((frontNear != null && frontFar != null &&
          (((frontNear ? 1 : 0) | (frontFar ? 1 : 0)) === (f.wallFront ? 1 : 0)) &&
          !(frontNear && frontFar)) ? '成立' : '不成立') + '）')
        : '—（本机无此量）'],
      ['动作', motionName(f.motionTask) + (f.motionStep == null ? '' : (' · 步 ' + f.motionStep))],
      /* 位置：起点要求 **左下角 (0,0) 且朝向向上（dir=0）**。
         现场最容易犯的错就是"复位后没把车摆回起点就发车"（复位只复位固件、
         不复位物理位置）⇒ 位置这一行顺带标出"在不在起点角"，
         让人一眼看出该不该先摆车。判"在起点角"只认 (0,0)（左上/右下都不是）。 */
      ['位置', (f.posX == null) ? '—'
        : ((f.posX + ', ' + f.posY) + ((f.posX === 0 && f.posY === 0) ? '　（起点角）' : ''))],
      ['终点', (f.goalX == null) ? '—' : (f.goalX + ', ' + f.goalY)],
      ['朝向', dirName(f.dir) + ((f.dir === 0) ? '' : '　（起点要求朝上）')],
      ['岔路栈', show(f.stackIdx)],
      ['快路径计数', show(f.fastwayCount)],
      ['标志', flagsOf(f.flags).join(' ') || '—'],
    ].map(function (r) {
      return '<b>' + C.util.esc(r[0]) + '</b><span>' + C.util.esc(String(r[1])) + '</span>';
    }).join('');
    sb.innerHTML = [
      ['左轮速', fmt(f.speedL, 1, ' cm/s')],
      ['右轮速', fmt(f.speedR, 1, ' cm/s')],
      ['陀螺角', fmt(f.gyroAngle, 0, '')],
      ['编码器角', fmt(f.encAngle, 0, '')],
      ['红外 前左', fmt(f.irF1, 0, ' mm')],
      ['红外 前右', fmt(f.irF2, 0, ' mm')],
      ['红外 左', fmt(f.irL, 0, ' mm')],
      ['红外 右', fmt(f.irR, 0, ' mm')],
      ['电池', fmt(f.battMv, 0, ' mV')],
    ].map(function (r) {
      return '<b>' + C.util.esc(r[0]) + '</b><span>' + C.util.esc(String(r[1])) + '</span>';
    }).join('');
  }

  /* ------------------------------------------------------------ 中栏 */
  function wallData() { return rep ? (rep.wall || null) : (wall || null); }

  function drawMaze() {
    const cv = el.querySelector('#m8-cv');
    if (!cv || !cv.getContext) return;
    const wd = wallData();
    if (!wd || !wd.n) {
      const g = cv.getContext('2d');
      const w = Math.round(cv.clientWidth || 320);
      const H = 180;
      cv.width = w; cv.height = H; cv.style.height = H + 'px';
      g.clearRect(0, 0, w, H);
      g.fillStyle = '#5e6675'; g.font = '12px Consolas,monospace';
      g.fillText(rep ? '这份记录没有墙图数据'
                     : '还没有重建数据（先连接；发车后随帧累积）', 8, 24);
      const wi = el.querySelector('#m8-winfo');
      if (wi) wi.textContent = '';
      const kv = el.querySelector('#m8-wkv');
      if (kv) kv.innerHTML = '';
      return;
    }
    const f = cur();
    const track = (rep && rep.track) ? rep.track : (wd.track || []);
    /* 前墙双档只在**实时读到**时才算得出（复盘那份记录里如果没有这两个通道就没有）。
       ⚠️ 三态：`undefined`/`null`（这台设备/这份记录没有这个量）**不等于** 0（无墙）。
       ⇒ 两个都是空时才把 `front` 传 `null`（绘图侧只在实际读到那一档时才画）。 */
    const near = (f && f.frontNear != null) ? !!f.frontNear : null;
    const far = (f && f.frontFar != null) ? !!f.frontFar : null;
    const hasFront = (near !== null || far !== null);
    C.draw.maze(cv, {
      height: 320,
      n: wd.n,
      known: wd.known_rows, wall: wd.wall_rows, visited: wd.visited,
      pos: (f && f.posX != null) ? [f.posX, f.posY] : null,
      dir: (f && f.dir != null) ? f.dir : null,
      goal: (f && f.goalX != null) ? [f.goalX, f.goalY] : null,
      front: hasFront ? { near: !!near, far: !!far } : null,
      track: track,
      label: rep ? '复盘 · 重构示意（截至游标）' : '实时 · 重构示意（最近一段）',
    });
    const wi = el.querySelector('#m8-winfo');
    if (wi) {
      wi.textContent = (wd.frames_used != null)
        ? (C.util.fmt(wd.frames_used, 0) + ' 帧') : '';
    }
    const kv = el.querySelector('#m8-wkv');
    if (kv) {
      let vis = 0;
      (wd.visited || []).forEach(function (r) {
        for (let i = 0; i < r.length; i++) if (r[i] === '1') vis++;
      });
      /* 前墙两档在小字里再报一次（图例只在画了那条线时才出现；
         这里给的是"这一帧读到的是哪一档"，读不到就如实说）。 */
      const rows = [
        ['已探索格子', vis + ' / ' + (wd.n * wd.n)],
        ['轨迹点', (track || []).length],
      ];
      if (hasFront) {
        rows.push(['前墙档位', near && far ? '矛盾' : (near ? '近端' : (far ? '远端' : '无'))]);
      } else {
        rows.push(['前墙档位', '—（本机无此量）']);
      }
      kv.innerHTML = rows.map(function (r) {
        return '<b>' + C.util.esc(r[0]) + '</b><span>' + C.util.esc(String(r[1])) + '</span>';
      }).join('');
    }
  }

  function renderWallSrc(d) {
    const src = el.querySelector('#m8-wsrc');
    if (!src) return;
    const fw = d && d.firmware;
    if (!fw) { src.textContent = '主机按帧重建（未拉过固件墙图）'; return; }
    /* 对账：主机重建 vs 固件自述。两边不平就**如实说** ——
       这真是真机要验的一条（固件不再上报路口分类，主机侧的复刻/重建对不对，
       只有拿固件那份墙图比才知道）。 */
    let diff = 0, same = 0;
    const wd = wall || {};
    if (wd.known_rows && fw.grid) {
      for (let y = 0; y < wd.known_rows.length; y++) {
        for (let x = 0; x < (wd.known_rows[y] || '').length; x++) {
          const g = (fw.grid[x] || [])[y];
          if (g == null || g === 0) continue;          // 固件这一格没信息
          const kw = parseInt(wd.known_rows[y][x], 16) & 0x0F;
          const mw = parseInt(wd.wall_rows[y][x], 16) & 0x0F;
          const gw = g & 0x0F;
          if (((gw ^ mw) & kw) === 0) same++; else diff++;
        }
      }
    }
    src.textContent = '主机重建 ' + (d.frames_used || 0) + ' 帧 · 对账固件：一致 ' +
      same + ' / 不一致 ' + diff + ' 格';
  }

  /* ------------------------------------------------------------ 右栏 */
  /* ------------------------------------------------------------ 决策时间线 */

  /* 每一类决策**怎么念**。`kind` 是后端差分的种类（判据与上游依据见
     `control/maze_rec.py` 的 `MazeDecisions`）。
     ⚠️ 这里只做"把结构化字段念成人话"，**不做任何推断** —— 界面上写的每个词
     都要能追到帧里的一个字段（这是本项目对"界面文案"的一贯要求）。 */
  const DEC_KIND = {
    TASK:     { tag: '任务', cls: 'k-task' },
    ROAD:     { tag: '路口', cls: 'k-road' },
    TURN:     { tag: '转向', cls: 'k-turn' },
    STRAIGHT: { tag: '直行', cls: 'k-straight' },
    JUNC:     { tag: '岔路', cls: 'k-junc' },
    CELL:     { tag: '进格', cls: 'k-cell' },
    HEAD:     { tag: '朝向', cls: 'k-head' },
    GOAL:     { tag: '终点', cls: 'k-goal' },
    EVENT:    { tag: '事件', cls: 'k-event' },
  };

  function decText(d) {
    const dirN = function (v) { return dirName(v) || String(v); };
    const at = (d.x == null ? '' : (' (' + d.x + ',' + d.y + ')'));
    switch (d.kind) {
      case 'TASK':
        return taskName(d.from) + ' → ' + taskName(d.to);
      case 'ROAD':
        // 路口大类用上游的中文名（`ROAD_KINDS`），并把码名一并写出来便于对照上游文档
        return (d.roadKind || '') + (d.road ? (' [' + d.road + ']') : '') + at;
      case 'TURN':
        return (d.motionName || '?') + '（转向动作）' + at + ' 朝 ' + dirN(d.dir);
      case 'STRAIGHT':
        return (d.motionName || '?') + at;
      case 'JUNC':
        return '岔路栈 ' + d.prev + ' → ' + d.stack + at;
      case 'CELL':
        return '进入 (' + d.x + ',' + d.y + ') 朝 ' + dirN(d.dir);
      case 'HEAD':
        return dirN(d.prev) + ' → ' + dirN(d.dir) + at;
      case 'GOAL':
        return '到达终点区 (' + d.x + ',' + d.y + ')' +
          (d.goalX == null ? '' : ('　目标 (' + d.goalX + ',' + d.goalY + ')'));
      case 'EVENT':
        return String(d.event || '') + (d.reason ? (' · ' + d.reason) : '') +
          (d.seq == null ? '' : (' #' + d.seq));
      default:
        /* ⚠️ **不把原始对象吐到界面上**（曾经这里 `JSON.stringify(d)`，
           于是有一坨 `{"kind":…}` 出现在右栏里）。原因那条例子的来源是本层自己的
           一个残留（见 `loadState()` 的注释），已经修掉；但"不认识的一类"这条**防御**
           要留住 —— 它意味着**前后端版本不一致**，得能诊断，只是不该污染界面。
           ⇒ 界面给一句可读的，细节进 console。 */
        try { console.warn('无记忆走迷宫：不认识的决策种类', d); } catch (e) { /* 忽略 */ }
        return '不认识的一类（' + String(d && d.kind) + '）';
    }
  }

  function renderTimeline() {
    const box = el.querySelector('#m8-tl');
    const cnt = el.querySelector('#m8-tlcount');
    if (!box) return;
    let rows;
    if (rep) {
      /* 回放：直接用记录文件里的 `decisions`（后端已算好、且已相对化到同一零点）。
         ⚠️ 老文件没有这个字段时后端会**从帧重放重建**并标 `decisions_rebuilt`，
         所以"文件里有没有存"不影响这里能不能看 —— 帧在，决策就在。 */
      const T = rep.doc.t_ms || [];
      rows = (rep.doc.decisions || []).map(function (r) {
        const t = r.t_ms == null ? 0 : r.t_ms;
        let lo = 0, hi = T.length - 1, fi = 0;
        while (lo <= hi) {
          const mid = (lo + hi) >> 1;
          if (T[mid] <= t) { fi = mid; lo = mid + 1; } else { hi = mid - 1; }
        }
        return Object.assign({}, r, { _idx: fi, _t: t });
      });
    } else {
      /* 实时：来自 `/maze/decisions` 的常驻环。**决策与上行事件都由它一条来路给**
         —— 同一个环纪元、同一个零点（后端 `MazeRecorder` 的 `_dec_ring` / `_ev_ring`）。
         以前事件是这里自己攒的（`evs`）：刷新一次页面就丢一半，而且它是**墙钟**时刻、
         与决策的零点不是同一个 ⇒ 两条来路拼在一条时间线上必然对不齐
         （本轮用户报的"回放读到的和退出回放的完全不一样"里就有它一份）。 */
      const ds = (live && live.decisions) || [];
      rows = ds.map(function (r) {
        return Object.assign({}, r, { _idx: null, _t: r.t_ms == null ? 0 : r.t_ms });
      });
      ((live && live.events) || []).forEach(function (e) {
        rows.push(Object.assign({}, e, {
          kind: 'EVENT', _idx: null,
          /* 后端还没钉出零点（刚清过环、一帧未到）⇒ 不猜时刻，如实放到末尾 */
          _t: (e.t_ms == null ? 1e12 : e.t_ms),
        }));
      });
      rows.sort(function (a, b) { return a._t - b._t; });
    }
    // 上行事件也并进来（它们**推不出来**，只能靠上行那条线；见 maze_rec 的说明）
    const evs2 = rep ? (rep.doc.events || []).map(function (e) {
      return { kind: 'EVENT', t_ms: e.t_ms, event: e.event, reason: e.reason, seq: e.seq,
               _idx: null, _t: (e.t_ms || 0) - ((rep.doc.start_ms || 0)) };
    }) : [];
    if (evs2.length) {
      rows = rows.concat(evs2).sort(function (a, b) { return a._t - b._t; });
    }
    if (cnt) {
      const miss = (!rep && live && live.with_codes === false);
      cnt.textContent = rows.length + ' 条' + (miss ? ' · 缺码表' : '');
    }
    if (!rows.length) {
      box.innerHTML = '<span class="hint">' +
        (rep ? '这一段没有决策（帧太少，或它一直待命没动）'
             : '还没有决策（发车后开始记）') + '</span>';
      return;
    }
    /* 当前时刻：回放看游标，实时看最新。
       **高亮 = 不晚于当前时刻的最后一条**，并把它滚进视野 ——
       "右栏定位到对应时间"就是这件事（用户明确要的）。 */
    const now_t = rep ? (rep.cur_ms || 0)
      : (rows.length ? rows[rows.length - 1]._t : 0);
    let anchor = -1;
    rows.forEach(function (r, i) { if (r._t <= now_t + 1) anchor = i; });
    const slice = rows.slice(-400);
    const off = rows.length - slice.length;
    const at = anchor - off;
    box.innerHTML = slice.map(function (r, i) {
      const K = DEC_KIND[r.kind] || { tag: r.kind, cls: '' };
      const hi = (i === at) ? ' class="tlnow"' : '';
      const idxAttr = (r._idx == null) ? '' : String(r._idx);
      return '<div data-tl="' + idxAttr + '"' + hi + '>' +
        '<span class="dim">' + C.util.esc(C.util.fmt(r._t / 1000, 2) + 's') + '</span> ' +
        '<b class="' + K.cls + '">' + C.util.esc(K.tag) + '</b> ' +
        C.util.esc(decText(r)) + '</div>';
    }).join('');
    /* 只在"锚点变了"时滚动 —— 否则实时态每 1 s 重渲染都会把用户的手动滚动顶掉。 */
    if (at >= 0 && at !== tlAnchor) {
      tlAnchor = at;
      const node = box.children[at];
      if (node && node.scrollIntoView) node.scrollIntoView({ block: 'nearest' });
    }
  }

  /* 实时决策流 + 运行状态。**零设备访问**（后端读记录器内存里的常驻差分器），
     所以可以跟着 1 s 的界面刷新走。

     ⚠️ 它与左栏的 `loadState()` 是**两条不同的路**，不能合并：
     `state` 给的是"此刻的快照"，而这里给的是"一路做过什么"。
     "它刚才识别到岔路口了"这句话在快照里表达不出来（快照只有现在）。 */
  async function loadDecisions() {
    if (C.needMouse()) { live = null; renderTlBar(); renderTimeline(); return; }
    const d = await C.api('/api/v1/mouse/maze/decisions').catch(function () { return null; });
    live = d || null;
    renderTlBar();
    renderTimeline();
  }

  /* ------------------------------------------------------------ 取数 */
  async function loadState() {
    const d = await C.guard(function () { return C.api('/api/v1/mouse/maze/state'); }, '读迷宫状态');
    if (!d) return;
    state = d.state || null;
    /* ⚠️ **这里曾经还把 `d.changes` 灌进时间线**（上轮的旧做法：那时决策只有
       "任务迁移 + 上行事件"两类，而 `changes` 是前者的来源）。
       本轮决策改成从 `/maze/decisions` 统一取（8 类、后端差分），于是那段成了
       **重复的真相**：同一个任务迁移被喂两遍，而且旧结构（`kind:'change'` + `text`）
       不认识新渲染 ⇒ 界面上冒出一条原始对象被 stringify 出来的东西。
       ⇒ 决策与上行事件现在**各只有一条来路**，都在 `/maze/decisions` 里
        （后端同一个环纪元、同一个零点）。这一页**不再自己攒任何时间线数据**。 */
    renderAll();
  }

  async function loadWall() {
    /* 实时重建：纯内存读（上游环形缓冲，30 s）⇒ 可以跟着 1 s 的刷新走，
       与 `loadFirmwareMap` 那条**命令往返**（真机 ≈1.21 s）完全不同。 */
    if (rep || C.needMouse()) return;
    const d = await C.api('/api/v1/mouse/maze/reconstruct?seconds=30')
      .catch(function () { return null; });
    if (d) { wall = d; drawMaze(); renderWallSrc(d); }
  }

  async function loadFirmwareMap() {
    const d = await C.guard(function () { return C.api('/api/v1/mouse/maze'); }, '读墙图');
    if (!d) return;
    const m = el.querySelector('#m8-msg');
    if (m) m.textContent = d.text ? '已读到固件墙图' : '固件没给墙图';
    const t = el.querySelector('#m8-sum-txt');
    if (t) t.textContent = d.text || '';
    await loadWall();
  }

  async function loadRecStatus() {
    const d = await C.api('/api/v1/mouse/maze/record').catch(function () { return null; });
    rec = d || null;
    renderRecInfo();
  }

  function renderRecInfo() {
    const box = el.querySelector('#m8-recinfo');
    if (!box) return;
    if (rep) {
      const d = rep.doc || {};
      box.textContent = '复盘 ' + (rep.name || '') + ' · ' + rep.ts.length + ' 帧 · ' +
        C.util.fmt((d.duration_ms || 0) / 1000, 1) + ' s';
      return;
    }
    if (!rec) { box.textContent = ''; return; }
    if (rec.recording) {
      box.textContent = '● 记录中 ' + rec.frames + ' 帧 · 已 ' +
        C.util.fmt((rec.duration_ms || 0) / 1000, 1) + ' s';
      return;
    }
    const last = rec.last;
    /* ⚠️ `last` **不一定是一次成功的保存**：`stop()` 在"一帧都没收到"时回
       `{ok:false, frames:0, note:…}`（没写文件 ⇒ 没有 `name`）。
       以前这里直接写 `last.name` ⇒ 界面上出现 **`上次保存 undefined · 0 帧`**
       —— 一句语法对、语义全错的话（`undefined` 露在界面上是明确的缺陷，
       而且"上次保存"这个说法让人以为**存成功了**）。
       ⇒ 分两支写：存成了就报文件名，没存成就如实说没存成 + 为什么。 */
    if (!last) { box.textContent = '未在记录'; return; }
    if (last.ok === false) {
      const why = last.note || ('一帧都没收到（' + (last.end_why || '') + '）');
      box.textContent = '上次没有保存成 · ' + why;
      return;
    }
    box.textContent = '上次保存 ' + (last.name || '(未知文件名)') + ' · ' +
      (last.frames == null ? '?' : last.frames) + ' 帧' +
      (last.tiny ? ' · 帧数偏少' : '');
  }

  /* ------------------------------------------------------------ 记录控制 */
  async function run() {
    /* ⚠️ 这里**不用 `C.guard`**：发车失败有三种，处置完全不同（低电 / 急停锁存 / 已在跑），
       而 `guard` 只把 `e.message` 丢进 toast 就完了 —— toast 会消失，而"为什么没发出去"
       这件事要在页面上留得住。所以自己接错误，按 `status` 在 `#m8-msg` 上留一句下一步。 */
    let d = null;
    try {
      d = await C.api('/api/v1/timesync/maze_run', { method: 'POST', body: {} });
    } catch (e) {
      const msg = String((e && e.message) || '');
      const m0 = el.querySelector('#m8-msg');
      if (m0) {
        if (msg.indexOf('电池') >= 0 || msg.indexOf('LOWBATT') >= 0) {
          m0.textContent = '发车被低电闸拦下（命令没发出去）';
        } else if (msg.indexOf('急停') >= 0 || msg.indexOf('ESTOP') >= 0) {
          m0.textContent = '发车失败：急停锁存中，先点【解锁急停】';
        } else if (msg.indexOf('BUSY') >= 0) {
          m0.textContent = '发车失败：已在跑迷宫，先点【收车】';
        } else {
          m0.textContent = '发车失败';
        }
      }
      C.toast('发车跑迷宫：' + msg, 'bad', 22000);
      console.error(e);
      loadPrecheck();
      return;
    }
    pre = d.precheck || pre;
    renderHint();
    const m = el.querySelector('#m8-msg');
    if (m) m.textContent = '已发车' + (d.record_note || '');
    exitReplay();               // 发车必然回实时态（否则新数据会被复盘盖住）
    loadRecStatus();
    loadState();
    loadWall();
  }

  async function stopRec() {
    const d = await C.guard(function () {
      return C.api('/api/v1/mouse/maze/record/stop', { method: 'POST', body: {} });
    }, '停止并保存');
    if (!d) return;
    const m = el.querySelector('#m8-msg');
    if (d.ok) {
      if (m) m.textContent = '已保存 ' + d.name + '（' + d.frames + ' 帧）' +
        (d.tiny ? ' · 帧数偏少，可能没真跑起来' : '');
      C.toast('记录已保存：' + d.name, d.tiny ? '' : 'ok', 12000);
    } else if (m) {
      m.textContent = d.note || '当前没有在记录';
    }
    loadRecStatus();
  }

  /* ------------------------------------------------------------ 收车 / 救援 */
  /* 两个动作的分工（都不是"停止并保存"的替代品，那是存盘、这是动车）：
       · 收车 `MAZE STOP`  = 停车 + 复位发车状态 + 解锁急停；
       · 救援 `MCU RESET`  = 整机软复位（固件主循环卡在非协作阻塞等待里时的唯一出口）。
     ⚠️ 为什么两个都要有：收车是"正常收"，但它**车在运动时不可靠** ——
     `MAZE STOP` 只置一个"请退出"标志，而标志的检查在 `LESSON_Blind8()` 的 while(1)
     顶部；主循环有 30 余处非协作式阻塞等待（等的是编码器里程/传感器这类**物理进度**），
     运动时程序正卡在那些循环内部，标志根本轮不到。
     ⇒ 所以收车之后**当场看任务机**：还在 SEARCH 就说明没收住，该上救援。 */
  async function collect() {
    const d = await C.guard(function () {
      return C.api('/api/v1/mouse/maze/stop', { method: 'POST', body: {} });
    }, '收车');
    if (!d) return;
    const m = el.querySelector('#m8-msg');
    const st = d.state || {};
    /* ⚠️ `reliable` 是**三态**（true / false / null），不许把 null 当 false：
       null = "还没等到新帧，判不了"，那是"先别下结论"，不是"没收住"。
       后端已经替我们等过一帧（旧帧会把成功报成失败，见适配器 `_wait_fresh_frame`），
       但它等不到时如实返回 null。 */
    if (d.reliable === true) {
      if (m) m.textContent = '已收车，任务 ' + (st.taskName || '?') + '（车已带出迷宫）';
      C.toast('已收车：任务回到 ' + (st.taskName || '?'), 'ok', 12000);
    } else if (d.reliable === false) {
      /* **这是本按钮最要紧的一句话**：`OK` 只说明命令被接受，
         收住没有要看任务机。车在运动时它多半收不住 ⇒ 直接给下一步。 */
      if (m) {
        m.textContent = 'MAZE STOP 回 OK，但任务仍是 ' + (st.taskName || '?') +
          '（车还在动，此命令此时收不住）';
      }
      C.toast('收车没生效：任务仍是 ' + (st.taskName || '?') +
              '\nMAZE STOP 只置"请退出"标志，车在运动时轮不到检查。' +
              '\n⇒ 请点【救援】（整机软复位），那是可靠手段。', 'warn', 26000);
    } else {
      if (m) m.textContent = '已发收车命令，但还没等到新帧，暂时判不了收住没有';
      C.toast('收车命令已发出，但没等到新遥测帧' +
              '\n⇒ 稍后看左栏的任务机：不是 SEARCH 就说明收住了；' +
              '仍是 SEARCH 就点【救援】。', 'warn', 22000);
    }
    loadState();
  }

  async function rescue() {
    const d = await C.guard(function () {
      return C.api('/api/v1/mouse/rescue', { method: 'POST', body: {} });
    }, '救援（整机软复位）');
    if (!d) return;
    const m = el.querySelector('#m8-msg');
    if (d.verified) {
      if (m) m.textContent = '救援成功：复位原因 ' + d.rst + '，等了 ' +
        C.util.fmt(d.elapsed_s, 1) + ' s';
      /* 三件事必须一起说，少一件现场就会踩：
           ① 复位只复位固件、**不复位物理位置**（车还在迷宫原处）；
           ② 复位后约 3 s 才有新帧，马上读状态会拿到旧帧；
           ③ 复位后不会自动恢复跑。 */
      C.toast('救援成功（rst=' + d.rst + '）' +
              '\n车的位置假设已被清零，但车还在原处。' +
              '\n⇒ 发车前必须把电脑鼠放回起点角，否则它会按一张错的地图走。' +
              '\n⇒ 复位后约 3 s 才有新遥测帧，别急着判读状态。', 'ok', 30000);
    } else {
      if (m) m.textContent = '救援未复核通过：rst=' + (d.rst || '—');
      C.toast('救援没有复核到复位\n' + (d.note || ''), 'warn', 30000);
    }
    // 复位后要等新鲜帧（约 3 s）⇒ 这里**故意晚一点**再读状态，而不是立刻读。
    // 立刻读会读到复位前的旧帧或 BSS 初值帧，看着像"复位没生效"。
    setTimeout(function () { loadState(); loadRecStatus(); }, 3200);
  }

  /* 发车前低电预检（纯读帧，零设备访问）。
     ⚠️ 它**不是闸**，是"提前可见"：真正的闸在上游 `send_cmd` 的登记表层（脚本也绕不开）。
     上游的判据要逐条对齐，否则会出现"界面说没问题、发车却被拒"：
       · battMv==0 是"电池监视未开启"**不是没电**（比赛固件从不打开）⇒ 不拦；
       · 取不到帧 ⇒ 不拦，标 unknown（判不了 ≠ 不通过）。 */
  async function loadPrecheck() {
    if (C.needMouse()) { pre = null; renderHint(); return; }
    const d = await C.api('/api/v1/mouse/launch_precheck').catch(function () { return null; });
    pre = d || null;
    renderHint();
  }

  async function doReset() {
    const d = await C.guard(function () {
      return C.api('/api/v1/mouse/maze/reset', { method: 'POST', body: {} });
    }, '清本层差分');
    if (d) {
      renderTimeline();
      const m = el.querySelector('#m8-msg');
      if (m) m.textContent = d.note || '已清本层差分缓冲';
    }
  }

  /* ------------------------------------------------------------ 复盘 */
  async function showRecords() {
    const box = el.querySelector('#m8-recbox');
    const d = await C.guard(function () {
      return C.api('/api/v1/mouse/maze/records?n=30');
    }, '读记录列表');
    if (!d || !box) return;
    const rs = d.records || [];
    if (!rs.length) {
      box.innerHTML = '<div class="hint" style="margin-top:6px">'
        + '还没有记录。跑一盘迷宫（点【发车】）或上传一份记录文件。</div>';
      return;
    }
    /* 列表**内联渲染成可点的按钮**，不用弹窗选序号：
       弹窗阻塞、样式不由我们管，而且**没法被自动化测试驱动** ——
       一个"只能靠人点"的入口，等于每次改动都得手动回归一遍。 */
    box.innerHTML = '<div class="row wrap" style="margin-top:6px">'
      + '<span class="hint">点一份进入复盘：</span>'
      + rs.map(function (r) {
        return '<button class="mini" data-rec="' + C.util.esc(r.name) + '">'
          + C.util.esc(r.name.replace('.maze.json', '')) + ' · '
          + Math.max(1, Math.round(r.size / 1024)) + ' KB</button>';
      }).join('') + '<button class="mini" id="m8-rec-hide">收起</button></div>';
    box.querySelectorAll('[data-rec]').forEach(function (b) {
      b.onclick = function () { loadRecord(b.dataset.rec); };
    });
    const hide = box.querySelector('#m8-rec-hide');
    if (hide) hide.onclick = function () { box.innerHTML = ''; };
  }

  async function loadRecord(name) {
    const d = await C.guard(function () {
      return C.api('/api/v1/mouse/maze/records/' + encodeURIComponent(name));
    }, '读记录');
    if (!d || !d.format) return;
    beginReplay(d, name, false);
  }

  async function importFile(file) {
    if (!file) return;
    const input = el.querySelector('#m8-file');
    let doc = null;
    try {
      doc = JSON.parse(await file.text());
    } catch (e) {
      C.toast('这不是一个 JSON 文件', 'bad', 12000);
      if (input) input.value = '';
      return;
    }
    const d = await C.guard(function () {
      return C.api('/api/v1/mouse/maze/records/import',
        { method: 'POST', body: { doc: doc, name: file.name } });
    }, '导入复盘');
    if (input) input.value = '';
    if (!d) return;
    C.toast('已导入 ' + d.name, 'ok', 10000);
    loadRecord(d.name);
  }

  function buildReplayWall(doc) {
    /* **本地重放**重建墙图：与后端 `MazeWallMap` 同一套规则（同一张 `_FACE` 表）。
       为什么前端也要有一份：拖动游标时"截至这一刻的图"是高频操作，
       每拖一下就发一次网络请求不现实。
       ⚠️ 两张表（Python 与 JS）必须一致 —— 不一致的表现是"实时看到的图与复盘的不一样"，
       而这种差异**看起来都合理**，极难发现。所以后端也存了一份 `wall`，用来对账。 */
    const ch = doc.channels || [];
    const idx = {};
    ch.forEach(function (c, i) { idx[c] = i; });
    if (idx.posX == null || idx.posY == null || idx.dir == null) return doc.wall || null;
    const n = (doc.wall && doc.wall.n) || doc.mazetype || 7;
    const FACE = { 0: { front: 0, left: 3, right: 1 }, 1: { front: 1, left: 0, right: 2 },
                   2: { front: 2, left: 1, right: 3 }, 3: { front: 3, left: 2, right: 0 } };
    const STEP = { 0: [0, -1], 1: [1, 0], 2: [0, 1], 3: [-1, 0] };
    const OPP = { 0: 2, 1: 3, 2: 0, 3: 1 };
    const known = [], wl = [], visited = [];
    for (let y = 0; y < n; y++) {
      known.push(new Array(n).fill(0)); wl.push(new Array(n).fill(0));
      visited.push(new Array(n).fill('0'));
    }
    const set1 = function (x, y, dd, has) {
      if (x < 0 || y < 0 || x >= n || y >= n) return;
      known[y][x] |= (1 << dd);
      if (has) wl[y][x] |= (1 << dd);
      const p = STEP[dd];
      const nx = x + p[0], ny = y + p[1];
      if (nx >= 0 && ny >= 0 && nx < n && ny < n) {
        const r = OPP[dd];
        known[ny][nx] |= (1 << r);
        if (has) wl[ny][nx] |= (1 << r);
      }
    };
    const track = [];
    let built = 0;
    (doc.frames || []).forEach(function (row) {
      const x = row[idx.posX], y = row[idx.posY], dir = row[idx.dir];
      if (x == null || y == null || dir == null) return;
      if (x < 0 || y < 0 || x >= n || y >= n) return;
      const F = FACE[dir & 3];
      if (!F) return;
      visited[y][x] = '1';
      if (!track.length || track[track.length - 1][0] !== x || track[track.length - 1][1] !== y) {
        if (track.length < 240) track.push([x, y]);
      }
      [['front', 'wallFront'], ['left', 'wallLeft'], ['right', 'wallRight']].forEach(function (p) {
        const v = row[idx[p[1]]];
        if (v == null) return;
        set1(x, y, F[p[0]], !!v);
      });
      built++;
    });
    if (!built) return doc.wall || null;
    return {
      n: n,
      known_rows: known.map(function (r) {
        return r.map(function (v) { return v.toString(16); }).join('');
      }),
      wall_rows: wl.map(function (r) {
        return r.map(function (v) { return v.toString(16); }).join('');
      }),
      visited: visited.map(function (r) { return r.join(''); }),
      track: track,
    };
  }

  function repIdx() { return rep ? rep.idx : 0; }

  /* ------------------------------------------------------------ 运行状态 + 时间线进度条 */

  /* 运行状态从**最新一帧的任务机**读（后端 `/maze/decisions` 已经把它算好端出来）。
     三态，**不许把"判不了"画成"已停止"**：
       running=true  → 运行中（醒目，绿）
       running=false → 已停止（它确实不在跑了）
       running=null  → 判不了（没有帧 / 没连上）—— 与"停止"是两件事

     ⚠️ 判据取 `mazeTask >= 2`（SEARCH 起）而不是"速度非零"：速度在换段的瞬间会过零，
       拿它当"在跑"会让胶囊一秒闪好几次。任务机是**状态**，不是瞬时量。 */
  function runState() {
    if (rep) return { key: 'replay', text: '回放中', task: null };
    if (!live || live.running == null) return { key: 'unknown', text: '判不了', task: null };
    return live.running
      ? { key: 'run', text: '运行中', task: live.taskName || '' }
      : { key: 'stop', text: '已停止', task: live.taskName || '' };
  }

  function renderTlBar() {
    const pill = el.querySelector('#m8-runpill');
    if (!pill) return;
    const s = runState();
    pill.className = 'runpill ' + s.key;
    pill.textContent = (s.key === 'run' ? '● ' : (s.key === 'stop' ? '■ ' : '— ')) + s.text +
      (s.task ? (' · ' + s.task) : '');
    // 进入回放的按钮**只在实时态显示**；退出回放的那个**只有控制条里一个**
    // （曾经两处都有 ⇒ 屏幕上并排两个"退出回放"，那是个纯多余的重复）
    const be = el.querySelector('#m8-rep-enter');
    if (be) be.style.display = rep ? 'none' : '';
    const ctl = el.querySelector('#m8-repctl');
    if (ctl) ctl.style.display = rep ? '' : 'none';
    const hint = el.querySelector('#m8-tlhint');
    if (hint) {
      hint.textContent = rep
        ? '回放中：拖上面的进度条改时间点，左中右三栏一起走；点某一条跳到那一刻。'
        : '决策按时间排列（拖动进度条可回看刚才那一刻）。';
    }
    const sld = el.querySelector('#m8-tlsld');
    const when = el.querySelector('#m8-tlwhen');
    if (sld) {
      if (rep) {
        /* 回放态：进度条的**量程是这一段的时间长度**，位置是游标时刻。
           用时间（ms）而不是帧下标当量程 —— 用户拖的是"时间点"，
           而帧在时间上**不均匀**（真机 4.4~6.5 Hz，还有丢帧）；
           用帧下标当量程会让"拖到中间"落在时间上的偏早或偏晚。 */
        const dur = Math.max(1, rep.dur_ms || 0);
        sld.max = String(dur);
        sld.value = String(Math.min(dur, Math.max(0, rep.cur_ms || 0)));
        sld.disabled = false;
        if (when) {
          when.textContent = C.util.fmt((rep.cur_ms || 0) / 1000, 1) + ' / ' +
            C.util.fmt(dur / 1000, 1) + ' s';
        }
      } else {
        const dur = Math.max(1, (live && live.duration_ms) || 0);
        sld.max = String(dur);
        sld.value = String(dur);        // 实时态永远停在最右（"现在"就是最右端）
        sld.disabled = false;           // 可拖：拖了就自动进回放（见绑定处）
        if (when) {
          /* ⚠️ 零点**语义要写出来**：`t0_kind === 'launch'` 时是"发车后"，
             否则只是"从开始收到帧起"。两个数都不假，但含义不同 ——
             不写清楚会出现"跑了 1518 s"而车其实才动一分钟（现场会以为看错）。 */
          const base = (live && live.t0_kind === 'launch') ? '发车后 ' : '从收帧起 ';
          when.textContent = (live && live.duration_ms)
            ? (base + C.util.fmt((live.duration_ms) / 1000, 1) + ' s')
            : '';
        }
      }
    }
  }

  /* 按**时间**定位（回放态）。二分到不晚于 t 的那一帧 —— 与 `repFrame()` 同一把尺子。 */
  function seekRepTime(t_ms) {
    if (!rep) return;
    const T = rep.doc.t_ms || [];
    if (!T.length) return;
    let lo = 0, hi = T.length - 1, best = 0;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (T[mid] <= t_ms) { best = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    seekRep(best);
  }

  function seekRep(i) {
    if (!rep) return;
    rep.idx = Math.max(0, Math.min(rep.ts.length - 1, i));
    rep.cur_ms = rep.ts[rep.idx] || 0;
    // 轨迹按游标截断（画到这一刻为止走过的路）—— "搜索了哪里"就是这个意思
    const full = (rep.wall && rep.wall.track) ? rep.wall.track : [];
    const k = Math.max(1, Math.round(full.length * (rep.idx + 1) / rep.ts.length));
    rep.track = full.slice(0, k);
    renderAll();
  }

  /* 进入回放。
     数据源有两个，**优先"正在跑的这段"**：
       · 在记录中 ⇒ 取实时快照（`/maze/record/live`）—— 不落盘、**不打断记录**，
         所以能在车还在跑的时候回看刚才那一段；
       · 不在记录 ⇒ 用最近一条已保存的记录（没有就提示怎么办，不假装能回放）。
     ⚠️ 快照是**点击那一刻的**：之后新帧不会进来（冻结才叫回放）；想回到最新点退出。 */
  async function enterReplay() {
    if (rep) return true;
    let doc = null, name = '';
    const lv = await C.api('/api/v1/mouse/maze/record/live').catch(function () { return null; });
    if (lv && lv.doc && ((lv.doc.frames || []).length)) {
      doc = lv.doc; name = '实时快照';
    } else {
      const list = await C.api('/api/v1/mouse/maze/records?n=1').catch(function () { return null; });
      const first = ((list && list.records) || [])[0];
      if (first) { await loadRecord(first.name); return !!rep; }
    }
    if (!doc) {
      C.toast('还没有可回放的这一段：先点【发车】跑一会儿，' +
              '或从【记录列表】选一份已有的记录。', 'warn', 16000);
      renderTlBar();
      return false;
    }
    beginReplay(doc, name, true);
    return true;
  }

  /* 装填回放态。`asLive` = 这份是从"正在跑的那段"取的快照（不是一份已完成的记录），
     界面上要**如实标注**（否则会有人以为"这段已经跑完了"）。 */
  function beginReplay(doc, name, asLive) {
    if (repTimer) { clearInterval(repTimer); repTimer = null; }
    const T = doc.t_ms || [];
    const wm = (doc.wall && doc.wall.track) ? doc.wall.track : [];
    rep = {
      doc: doc, name: name || doc.name || '', asLive: !!asLive,
      ts: T.slice(),
      idx: Math.max(0, T.length - 1), playing: false,
      cur_ms: T.length ? T[T.length - 1] : 0,
      dur_ms: (doc.duration_ms || (T.length ? T[T.length - 1] : 0)),
      wall: doc.wall || null,
      track: [],
    };
    tlAnchor = -1;
    // 墙图：**本地重放重建**（拖动游标时"截至这一刻的图"是高频操作，不能每拖一下发一次请求）
    rep.wall = buildReplayWall(doc);
    rep.track = (rep.wall && rep.wall.track) ? rep.wall.track.slice() : [];
    /* 默认停在**末尾**（"刚才跑完的样子"）—— 进回放的人多半先想看"跑到哪了"，
       再往回拖。若调用方是拖进度条进来的，随后会 `seekRepTime()` 覆盖它。 */
    renderTlBar();
    renderAll();
    C.toast('进入回放：' + rep.name +
            (asLive ? '（实时快照，之后的新帧不会进来）' : ''), 'ok', 9000);
    return rep;
  }

  function exitReplay() {
    if (repTimer) { clearInterval(repTimer); repTimer = null; }
    rep = null;
    tlAnchor = -1;
    /* 退出回放 = **立刻回到最新**（用户明确要的"实时发车后所有面板显示最新画面"）。
       这里显式刷一次，而不是等下一个 1 s 定时器 —— 等一秒会让人觉得"退出没生效"。 */
    renderTlBar();
    renderAll();
    if (!C.needMouse()) { loadState(); loadWall(); loadDecisions(); loadRecStatus(); }
  }

  function togglePlay() {
    if (!rep) return;
    const btn = el.querySelector('#m8-rep-play');
    if (rep.playing) {
      rep.playing = false;
      if (repTimer) { clearInterval(repTimer); repTimer = null; }
      if (btn) btn.textContent = '播放';
      return;
    }
    rep.playing = true;
    if (btn) btn.textContent = '暂停';
    repTimer = setInterval(function () {
      if (!rep) { if (repTimer) { clearInterval(repTimer); repTimer = null; } return; }
      if (rep.idx >= rep.ts.length - 1) { togglePlay(); return; }
      /* 一次跳**若干帧**而不是一帧：真机只有 4.4~6.5 Hz，逐帧放会慢得看不下去。
         按总帧数定步长，让整段大约 20 s 放完。 */
      const step = Math.max(1, Math.round(rep.ts.length / 160));
      seekRep(rep.idx + step);
    }, 100);
  }

  /* ------------------------------------------------------------ 汇总 */
  async function loadSummary() {
    const s = await C.guard(function () { return C.api('/api/v1/mouse/maze/summary'); }, '取汇总');
    if (!s) return;
    const sm = s.summary || {};
    const seq = function (a) {
      return (a || []).map(function (t) {
        return taskName(t[0]) + '→' + taskName(t[1]);
      }).join('  ') || '—';
    };
    const pair = function (p) { return p ? (p[0] + ', ' + p[1]) : '—'; };
    const badge = el.querySelector('#m8-sum-kv');
    if (badge) {
      badge.innerHTML = [
        ['任务', sm.task == null ? '—' : sm.task],
        ['路口', (sm.road || '—') + ' · ' + (sm.roadKind || '')],
        ['位置 → 终点', pair(sm.pos) + ' → ' + pair(sm.goal)],
        ['快路径计数', sm.fastwayCount == null ? '—' : sm.fastwayCount],
        ['任务序列', seq(sm.taskSeq)],
      ].map(function (r) {
        return '<b>' + C.util.esc(r[0]) + '</b><span>' + C.util.esc(String(r[1])) + '</span>';
      }).join('');
    }
    const txt = el.querySelector('#m8-sum-txt');
    if (txt) txt.textContent = (s.explain || []).join('\n');
    const hz = el.querySelector('#m8-sum-hz');
    if (hz) hz.textContent = sm.running ? '' : '迷宫未运行';
  }

  /* ------------------------------------------------------------ EVQ */
  function renderFault() {
    const f2 = el.querySelector('#m8-evfault');
    if (!f2) return;
    if (rep) {
      /* 复盘报的是"**这份记录当时**"的完整性（文件里的 `integrity`），
         不是"现在"的 —— 拿现在的完整性去解释过去的一段数据是错的。 */
      const it = (rep.doc && rep.doc.integrity) || {};
      if (!it.evq_seen && !it.drop_seen) { f2.textContent = ''; return; }
      f2.textContent = '这段记录本身不完整：'
        + (it.evq_seen ? '设备侧丢过事件行（EVQ）' : '')
        + (it.drop_seen ? ((it.evq_seen ? '；' : '') + '丢过帧（dropCnt 非 0）') : '')
        + '\n· 复盘看到的是当时记下来的东西，缺的那部分补不回来'
        + '\n· 位置与墙图仍可信（它们在帧里），只有事件与迁移可能缺条目';
      return;
    }
    const t = C.state.telemetry || {};
    const ev = t.events || {};
    const dropped = (state && state.evDropped) || ev.incomplete === true ||
      ((t.unified || {}).evq === true);
    if (!dropped) { f2.textContent = ''; return; }
    const lost = (ev.ev_lost == null) ? '—' : ev.ev_lost;
    f2.textContent = '事件流不完整：设备侧有事件行被丢弃（EVQ），已丢 ' + lost + ' 条。'
      + '\n· STOPPED 若被丢掉，就不知道这次运动是"时间到"还是"红外触发"停的'
      + '\n· 通信拥塞或调试 printf 抢环时会这样；复位设备后重跑一次才能拿到完整过程';
  }

  /* ------------------------------------------------------------ 总入口 */
  function renderAll() {
    renderLeft();
    drawMaze();
    renderTimeline();
    renderFault();
    renderRecInfo();
    renderTlBar();          // 运行状态胶囊 + 进度条位置（它跟着游标走，必须一起刷）
    const t = el.querySelector('#m8-rep-t');
    if (t) {
      if (!rep) t.textContent = '';
      else {
        const f = repFrame();
        t.textContent = (rep.idx + 1) + ' / ' + rep.ts.length +
          (f ? ('　' + C.util.fmt(f.t_ms / 1000, 2) + ' s') : '');
      }
    }
  }

  const TICK_MS = 1000;

  return {
    mount: mount,

    /* 复盘入口暴露出来：`C.mods.mod_maze8.openRecord(name)`。
       为什么要给外面用：复盘是这一页的核心动作，深链/自动化要能直接进
       （`#/mouse/mod_maze8` 只是打开模块，"看哪一份"得另外指）。 */
    openRecord: function (name) { return loadRecord(name); },

    /* 重渲染三栏 —— 给自动化用（`browser_check` 的 B37 要喂"老上游形状的帧"
       再立刻看左栏写的是什么）。它**不取新数据**，只按当前 `C.state.telemetry.latest`
       重画；生产的实时刷新走 `onMsg`（WS 推一帧画一次），那才是唯一真相来路。 */
    refresh: function () { renderAll(); },

    activate: function () {
      if (!meta) loadMeta();
      /* **未连接就一个设备请求都不发**：那几条端点会回 409，白等一轮还把控制台刷成
         "请求失败"。连接的判断只做一次、且在这里做（相机侧同一条纪律）。 */
      if (!C.needMouse()) { loadState(); loadWall(); }
      loadRecStatus();
      loadPrecheck();
      loadDecisions();
      /* 实时态每秒刷三栏；回放态由游标驱动，**定时器不接管**（否则会来回跳）。 */
      if (!timer) {
        timer = setInterval(function () {
          if (el.offsetParent === null) return;   // 看不见就不干活
          if (rep) return;
          if (C.needMouse()) return;
          loadState();
          loadWall();
          loadRecStatus();
          loadPrecheck();     // 低电预检跟着刷新（纯读帧、零设备访问 ⇒ 可以跟 1 s 走）
          loadDecisions();    // 决策流 + 运行状态（同上：零设备访问）
        }, TICK_MS);
      }
    },

    onMsg: function (m) {
      if (rep) return;     // 复盘态：**实时数据不驱动界面**（否则过去的画面会被冲掉）
      if (m.type === 'mouse.upevent' || m.type === 'mouse.estop') {
        /* 上行事件（`STOPPED <reason>`）**不在帧里**，`reason` 从帧里推不出来
           （"时间护栏到"与"红外护栏触发"在帧里长得一样，见设计方案 §11.9）。
           但**不在前端攒**：后端记录器已经有一个与决策同纪元的常驻事件环
           （`MazeRecorder._ev_ring`），由 `/maze/decisions` 一起端出来。
           ⇒ 这里只催一次重拉，让这条事件**立刻**上屏（不等那 1 s 的定时器）。
           为什么不能在前端攒：① 刷新一次就丢一半；② 它的时刻是**墙钟**，
           与决策那条线的零点不是同一个 ⇒ 两条拼在一条时间线上必然错位。 */
        loadDecisions();
      } else if (m.type === 'mouse.telemetry') {
        renderLeft();      // 左栏跟着帧走 —— 这是"实时"两个字的意义所在
      }
    },

    onHealth: function () {
      const why = C.needMouse();
      ['m8-run', 'm8-mstop', 'm8-rescue', 'm8-state', 'm8-map', 'm8-rst', 'm8-sum']
        .forEach(function (id) { C.gate(el.querySelector('#' + id), why); });
      /* ⚠️ `m8-stop`（停止并保存）与 `m8-reclist`（记录列表）**不门控**：
         断开时恰恰最需要它们 —— 记录器在链路断开时只置了"该收尾"的标记，
         落盘仍要有人推一把；而记录列表是纯本地文件操作，与设备无关。 */
      renderHint();
      /* 门控挡住之后把过期的读数清掉 —— 留着上一轮的"任务 SEARCH"会让人
         以为它还在跑（"此时判不了"与"上一次的值"是两回事）。 */
      if (why && !rep) { state = null; wall = null; pre = null; renderAll(); }
    },
  };
})();
