/* 小车控制 · 比赛 —— `MAZE RUN RACE` 发车 + 冲刺状态监视 + 迷宫重构（地图预览）。
 *
 * ## 它原来是两页，现在是一页（改版二十三）
 * 上游 2.4.0 曾把发车命令扩成 `MAZE RUN [BLIND|RACE]`，本层于是并列了两张卡
 * （「无记忆走迷宫」/「比赛」）。**新基线只跑比赛跑法**，那张卡整页下线
 * （理由与判据见设计方案 §11.25）；它里面唯一还有独立价值的**中栏那张图**
 * ——"车认为世界长什么样"——搬到了这一页（`C.parts.wallmap`）。
 * 状态机 / 决策时间线 / 记录复盘 / 汇总**没有**跟过来：它们服务的是"趟一遍迷宫"
 * 那个过程，比赛页要的是"按地图冲一圈"，看的是冲刺档与前墙双档。
 *
 * ## 这一页几乎全是"只读"
 * 冲刺档位 `spurtMode` 是固件**只读镜像**（`mazeState`/`taskSt` 的位），
 * 上位机**设不了**它 —— 冲刺入口是比赛流程里"人工贴墙选档"那一段（刻意不动）。
 * ⇒ 这一页的职责是**把固件正在想什么说出来**，不是发指令。只有三个动作：
 *   【停止并保存】（存盘）、【收车】/【复位】（动车）。
 *   少写这句"我们设不了它"，现场会到处找那个不存在的下拉框。
 *
 * ## 为什么【停止并保存】必须在这一页（铁律 28）
 * 记录器在发车时自动开录，但"停止"没有权威信号（固件不发"迷宫结束"事件）⇒
 * 自动收尾只是弱判据（`maze_rec.py` 三条），**人工兜底必须保留**。
 * 改版二十三把走迷宫页下线后，记录的生命周期整段落在这一页 ⇒ 按钮跟过来。
 * ⚠️ 它与【收车】/【复位】**三件事不许合并**：存盘不动车，动车不存盘。
 * ⚠️ 它**不门控**：断链时恰恰最需要它 —— 记录器断链时只置"该收尾"标记，
 *   落盘仍要有人推一把。
 *
 * ## 为什么要专门监视 `frontNear` / `frontFar`（2.3.0 前墙双档）
 * 2.3.0 把前墙拆成互斥的两档：
 *   0 = 无墙 / 1 = 近端（贴脸，**该处理**）/ 2 = 远端（提前量，还没到）。
 * 恒等式：`frontNear | frontFar == wallFront`。比赛档靠"远了提前减速、近了马上转"，
 * 所以**这一档到底判对没有**，直接决定它会不会一头撞上去。
 * ⇒ 这一页把三个量并排画，旁边一行**当场自校验**这条恒等式；对不上立刻红字。
 *
 * ## 终点是**赛道属性**，不是帧里的量
 * 上游 v9 删了 `goalX/goalY`（§11.24 影响面 #1）⇒ 帧里再没有终点信号。
 * 比赛终点 = **赛道固定的中心 2×2 四格**，由后端常量端出来（`maze_rec.GOAL_CELLS`
 * ⇒ `goal_cells`），界面与记录判据**同一个来源**（铁律 32）。
 *
 * ## 数据来源与三态（判不了 ≠ 全是 0）
 *   · `/mouse/maze/state` —— 状态机 + 位置 + `frontNear/Far/Both`；
 *   · `/mouse/meas/meta`  —— 冲刺档位与前墙的**码表**（静态，零设备访问）；
 *   · `/mouse/maze/record` —— 记录状态（本页发车也会自动开始记录）；
 *   · `/mouse/maze/record/stop` —— 【停止并保存】，返回 `path`（完整路径）；
 *   · `/mouse/launch_precheck` —— 发车前低电预检（纯读帧，见 `#rc-hint`）；
 *   · `/mouse/maze/reconstruct` —— 墙图重建（`C.parts.wallmap` 自己取）。
 * ⚠️ 老上游（≤2.2.1）根本没这些量：适配器会回 `spurt: null`。那时界面必须写
 *   "这台设备没有这个量"（升级上游），**不许**把 `null` 画成"未选 / 0"——
 *   "没选档"和"读不到档"是两件事，混起来现场会以为车已经准备好冲了。 */
C.mods.mod_race = (function () {
  let el, meta = null, dev = null, st = null, rec = null, sp = null, eno = null;
  let pre = null;   // 发车前低电预检（GET /launch_precheck，纯读帧）
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

  /* 终点区文字 —— 来路是 `C.parts.wallmap`（它从 `/reconstruct` 的 `goal_cells` 取，
     那个值最终来自 `maze_rec.GOAL_CELLS`）。**不在这里再写一份坐标表**：
     两份各写一份 = "图上标了一个区、状态行写另一个区"（铁律 32）。 */
  function goalText() {
    const wm = C.parts.wallmap;
    if (wm && wm.hasGoals && wm.hasGoals()) return wm.goalsText();
    return '—（还没取到）';
  }

  /* ------------------------------------------------------------ 骨架 */
  function mount(host) {
    el = host;
    el.innerHTML = `
      <div class="pane">
        <h4>比赛发车 <span class="hint" id="rc-msg"></span>
          <span><button class="mini primary" id="rc-run">比赛发车</button></span></h4>
        <div class="row wrap">
          <button class="mini" id="rc-recstop">停止并保存</button>
          <button class="mini" id="rc-mstop">收车</button>
          <button class="mini danger" id="rc-rescue">复位</button>
          <button class="mini" id="rc-refresh">刷新</button>
        </div>
        <div class="gatehint" id="rc-hint"></div>
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
    el.querySelector('#rc-recstop').onclick = function () { stopRec(); };
    el.querySelector('#rc-mstop').onclick = function () { act('maze/stop', '收车'); };
    el.querySelector('#rc-rescue').onclick = function () { act('rescue', '复位'); };
    el.querySelector('#rc-refresh').onclick = function () { reload(); };
    /* 迷宫重构（地图预览）是**区块**，它自己 append 一个 pane。
       ⚠️ 必须在 `innerHTML` 之后挂 —— 否则会被下面那次整体替换冲掉。 */
    if (C.parts.wallmap) C.parts.wallmap.mount(el);
    startTick();
    loadMeta();
    reload();
    if (C.parts.wallmap) C.parts.wallmap.activate();
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
      loadPrecheck();
    }, TICK_MS);
  }

  function reload() { loadState(); loadRec(); loadMeta(); loadPrecheck(); }

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

  /* 发车前低电预检（纯读帧，零设备访问）。
     ⚠️ 它**不是闸**，是"提前可见"：真正的闸在上游 `send_cmd` 的登记表层。
     判据逐条对齐上游，否则会出现"界面说没问题、发车却被拒"：
       · battMv==0 是"电池监视未开启"**不是没电**（比赛固件从不打开）⇒ 不拦；
       · 取不到帧 ⇒ 不拦，标 unknown（判不了 ≠ 不通过）。 */
  async function loadPrecheck() {
    if (C.needMouse()) { pre = null; renderHint(); return; }
    const d = await C.api('/api/v1/mouse/launch_precheck').catch(function () { return null; });
    pre = d || null;
    renderHint();
  }

  /* 门控行（`#rc-hint`）只有一行，合并两个来源：
       ① 没连接时的"做什么才能用"（`C.needMouse()`）；
       ② 发车前低电预检的警告。
     合并规则：门控优先（连都没连上时，说电池没意义）。 */
  function renderHint() {
    const h = el && el.querySelector('#rc-hint');
    if (!h) return;
    const why = C.needMouse();
    if (why) { h.textContent = why; return; }
    h.textContent = (pre && pre.level === 'warn') ? (pre.note || '') : '';
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
      kv('goaheadnum', '前冲计数', num(sp.goaheadnum));
    /* ⚠️ `fastwayCount90`（90° 快路计数）**删了**：上游 v9 把它整字节删掉，
       而方案里说的召回路径（`PATHPLAN` 事件 / 低频诊断）**都没落地** ⇒ 真拿不到。
       这里不留一行"—"：一个永远空着的读数会让人以为"这段在报 0"或"接口坏了"，
       比没有更坏。要找回它应是**给上游提需求**，不是本层塞回帧（§11.24 §三.4）。 */
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
      /* 终点**不是帧里的量了**（v9 删了 `goalX/goalY`）⇒ 报赛道常量，并说明它的来路。
         没有 `goalX/goalY` 这一行了 —— 留着只会显示 "0, 0"（读到缺字段的默认值），
         那是"替固件宣布终点在原点"，比不显示坏得多。 */
      '<b>终点区 <code>GOAL_CELLS</code></b><span>' +
        C.util.esc(goalText()) + '</span>' +
      kv('roadName', '当前路口', st.roadName ? (st.roadName + ' · ' + (st.roadKind || '')) : '—');

    /* 任务机处在什么位置，决定"接下来该点哪" —— 门控只报条件，故障必须给可照做清单。
       这几档对应上游 2.4.0 的三种发车失败与"已在跑"，外加 2.11 的 `END=6`。 */
    const note = el.querySelector('#rc-note');
    if (note) {
      const t = st.task;
      if (t === 0) {
        note.textContent = '任务机 WAIT（待机）⇒ 可以点【比赛发车】。';
      } else if (t === 6) {
        /* ⚠️ `END(6)` 是 2.11 换基线时新增的**跑位收尾态**（固件已进 `maze_done`）。
           它不是"还在跑"：下面 `running` 判据、维护页的门控都按 `2..5` 算，不含 6。 */
        note.textContent = '任务机 END（跑位收尾）⇒ 这一趟已经收尾，可以点【收车】清发车状态。';
      } else if (t === 3 || t === 4 || t === 5) {
        note.textContent = '任务机处在冲刺态 ⇒ 正在按地图冲。急停随时可用。';
      } else if (t === 1) {
        note.textContent = '任务机 START（刚发车）⇒ 正在读地图选档。';
      } else if (t === 2) {
        /* 收车可靠性在 2026-10-16 被上游修了（终止出口 + `mapStepEdit` 出口 +
           `LINK_Tick` 2 s 兜底软复位）⇒ 旧文案"运动中不可靠、改用复位"已过期。
           ⚠️ 但**三个按钮仍不合并**：收车 / 复位 / 停止并保存是三件事（铁律 28）。 */
        note.textContent = '任务机 SEARCH ⇒ 车正在走。要停就点【收车】：'
          + '正常 0.2~1 s 就退出；若 2 s 后它还在 SEARCH，固件会自己软复位'
          + '（PING 的 rst 由 POR 变 SW），不必人工断电。';
      } else {
        note.textContent = '任务机 ' + (st.taskName || t) + ' ⇒ 状态不认识（上游新档位？），'
          + '先点【收车】。';
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
          /* ⚠️ 原文写的是"只能用【无记忆走迷宫】页" —— 那页在改版二十三下线了。
             删一个页要连**指向它的文案**一起改（铁律 37），否则现场会去找一张
             不存在的卡。 */
          m0.textContent = '这台设备不支持比赛发车（上游早于 2.4.0）⇒ 需要升级 RemoteControl。';
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

  /* 【停止并保存】= 只存盘、不动车（与收车/复位是三件事，铁律 28）。
     记录器的自动收尾是弱判据（固件没有"迷宫结束"事件）⇒ 人工兜底走这里。
     ⚠️ 保存后要**看得见存到哪儿了**：文件名进页内提示（留得住），
     **完整路径进弹窗**（可复制）；路径长，放一份到 `title` 上悬停可看全。 */
  async function stopRec() {
    const d = await C.guard(function () {
      return C.api('/api/v1/mouse/maze/record/stop', { method: 'POST', body: {} });
    }, '停止并保存');
    if (!d) return;
    const m = el.querySelector('#rc-msg');
    if (d.ok) {
      const where = d.path || d.name || '(未知路径)';
      if (m) {
        m.textContent = '已保存 ' + d.name + '（' + d.frames + ' 帧）' +
          (d.tiny ? ' · 帧数偏少，可能没真跑起来' : '');
        m.title = '保存位置：' + where;
      }
      C.toast('记录已保存到：\n' + where +
              '\n（' + d.frames + ' 帧' + (d.tiny ? ' · 帧数偏少' : '') + '）',
              d.tiny ? '' : 'ok', 16000);
    } else if (m) {
      m.textContent = d.note || '当前没有在记录';
    }
    loadRec();
  }

  return {
    mount: mount,
    activate: function () {
      startTick();
      reload();
      /* 切回本页时地图区块也刷新一次（它有自己的 1 s 定时器，`activate` 幂等）。 */
      if (C.parts.wallmap) C.parts.wallmap.activate();
    },
    onMsg: function (m) {
      /* 帧驱动：冲刺量就在帧里，所以跟着帧重渲染 —— 这页是"监视"，
         秒级延迟就够，但帧一到就该更新（比赛档读数是随帧变的）。 */
      if (m.type === 'mouse.telemetry') {
        /* 地图区块先收这一帧（位置/朝向/前墙要跟着帧动）。 */
        if (C.parts.wallmap) C.parts.wallmap.onMsg(m);
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
              goaheadnum: u.goaheadnum,
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
      /* 复位**不门控**：恰恰是连不上、卡死的时候最需要它（与顶栏急停一致）。
         ⚠️ `rc-recstop`（停止并保存）**也不门控**：断链时记录器只置"该收尾"标记，
         落盘仍要有人推一把；它是纯本地存盘，与设备无关。 */
      renderHint();
      /* 门控挡住之后把过期读数清掉 —— 留着上一轮的值会让人以为还在跑。 */
      if (why) { st = null; sp = null; rec = null; pre = null; render(); }
      if (C.parts.wallmap) C.parts.wallmap.onHealth();
    },
  };
})();
