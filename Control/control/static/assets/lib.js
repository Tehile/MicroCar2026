/* ═══════════════════════════════════════════════════════════════════════════
   lib.js —— 原型内核：假数据 / 迷宫 / 视频会话 / 事件 / 路由
   对齐 Control 改版二十四（上游 2.13.0 / PROTO 9）：
     · 手机相机整组下线，顶栏只留「电脑鼠 + 时钟」
     · 位置数据来自「一段视频 → Auto Locate 会话 → 轨迹」
     · 状态行新增 4 灯三态（真 / 假 / 未知，未知绝不画成“灭”）
   ⚠️ 这里没有一行网络请求。
   ═══════════════════════════════════════════════════════════════════════════ */
window.C = (function () {
  'use strict';

  /* ── 工具 ─────────────────────────────────────────────────────────────── */
  const util = {
    fmt(n, d) {
      if (n === null || n === undefined || n === '' || Number.isNaN(Number(n))) return '—';
      return Number(n).toFixed(d === undefined ? 0 : d);
    },
    pad(n) { return (n < 10 ? '0' : '') + n; },
    clock(ms) {
      const s = Math.floor(ms / 1000);
      return util.pad(Math.floor(s / 3600)) + ':' + util.pad(Math.floor(s / 60) % 60) + ':' + util.pad(s % 60);
    },
    hhmmss(ts) {
      const d = new Date(ts);
      return util.pad(d.getHours()) + ':' + util.pad(d.getMinutes()) + ':' + util.pad(d.getSeconds());
    },
    esc(s) {
      return String(s === null || s === undefined ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    },
    clamp(v, a, b) { return Math.max(a, Math.min(b, v)); },
    lerp(a, b, t) { return a + (b - a) * t; },
  };

  /* ── 图标（16px 线性） ───────────────────────────────────────────────── */
  const ICONS = {
    grid: '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/>',
    pulse: '<path d="M3 12h4l2.5-6 3.5 12 2.5-6H21"/>',
    sliders: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2.2"/><circle cx="9" cy="17" r="2.2"/>',
    flag: '<path d="M5 21V4m0 0h9l-1.5 4L14 12H5"/>',
    flagSolid: '<path d="M5 22V3" stroke-width="2.6"/><path d="M5.8 3.6h10.4l-2.4 4.9 2.4 4.9H5.8z" fill="currentColor" stroke="none"/>',
    wrench: '<path d="M14.5 6.5a3.7 3.7 0 105.2 5.2l-8.4 8.4a2.6 2.6 0 01-3.7-3.7l8.4-8.4a3.7 3.7 0 01-1.5-1.5z"/>',
    film: '<rect x="3" y="5" width="18" height="14"/><path d="M7 5v14M17 5v14M3 9h4M3 15h4M17 9h4M17 15h4"/>',
    route: '<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="18" r="2.5"/><path d="M8.5 6H14a3 3 0 010 6H9a3 3 0 000 6h6.5"/>',
    database: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
    play: '<path d="M7 4.5l12 7.5-12 7.5z"/>',
    stop: '<rect x="6" y="6" width="12" height="12" fill="currentColor" stroke="none"/>',
    alert: '<path d="M12 4.5L21 19H3z"/><path d="M12 10v4"/><circle cx="12" cy="16.6" r=".8" fill="currentColor" stroke="none"/>',
    check: '<path d="M4.5 12.5l5 5L20 6.5"/>',
    x: '<path d="M6 6l12 12M18 6L6 18"/>',
    chevron: '<path d="M9 6l6 6-6 6"/>',
    down: '<path d="M6 9l6 6 6-6"/>',
    arrowUp: '<path d="M12 19V5M6 11l6-6 6 6"/>',
    arrowDown: '<path d="M12 5v14M18 13l-6 6-6-6"/>',
    rotateL: '<path d="M4 10a8 8 0 1114 5"/><path d="M4 5v5h5"/>',
    rotateR: '<path d="M20 10a8 8 0 10-14 5"/><path d="M20 5v5h-5"/>',
    link: '<path d="M10 14a4 4 0 015.7 0l2.3-2.3a4 4 0 00-5.7-5.7L10 8.3"/><path d="M14 10a4 4 0 01-5.7 0L6 12.3a4 4 0 005.7 5.7L14 15.7"/>',
    refresh: '<path d="M20 11a8 8 0 10-2.3 5.7"/><path d="M20 4v7h-7"/>',
    download: '<path d="M12 4v11M7 11l5 5 5-5M4 20h16"/>',
    trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
    save: '<path d="M5 4h11l3 3v13H5z"/><path d="M8 4v5h7V4M8 20v-6h8v6"/>',
    power: '<path d="M12 3v9"/><path d="M6.5 7a8 8 0 1011 0"/>',
    map: '<path d="M9 4L3 6.5v14L9 18l6 3 6-2.5v-14L15 7z"/><path d="M9 4v14M15 7v14"/>',
    bolt: '<path d="M13 3L5 14h5l-1 7 8-11h-5z"/>',
    wave: '<path d="M3 12c2 0 2-7 4-7s2 14 4 14 2-9 4-9 2 4 4 4"/>',
    terminal: '<path d="M4 5h16v14H4z"/><path d="M8 10l2.5 2.5L8 15M13 15h4"/>',
  };
  function icon(name, cls) {
    const p = ICONS[name] || ICONS.grid;
    return '<svg class="' + (cls || '') + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' + p + '</svg>';
  }

  /* ── 全局状态 ─────────────────────────────────────────────────────────── */
  const S = {
    scene: 'ready',
    route: 'overview',
    running: false,
    estop: false,
    session: { active: false, id: '', t0: 0, frames: 0, ledger: 'pass' },
    /* 拉取只有批式：一次往返取 N 帧，N 决定帧率 */
    pullMode: 'batch',
    pullN: 1,
    /* 拉取开关（人工刹车）。
       ⚠️ 改版三十一起，**真值来自后端** `health.mouse.stream_on` —— 本地这位只是
       在两次轮询之间给界面一个即时反馈。连上设备但没进"要拉的页面"时它是 false。 */
    pulling: false,
    rateLog: [],
    /* 比赛页的实时迷宫：`null` = 还什么都没探到（图上就是空白网格，不许编） */
    mazeView: null,
    mazeN: 0,
    recSel: '',
    params: null,
    log: [],
    logFilter: { src: null, lvl: null, q: '' },
    tel: {
      speedL: 0, speedR: 0, trackL: 4812, trackR: 4805, posX: 1.5, posY: 1.5,
      gyroAngle: 12.4, encAngle: 274.1, dir: 0, motionTask: 0, mazeTask: 0,
      wallLeft: 1, wallFront: 0, wallRight: 1, frontNear: 0, frontFar: 0, frontBoth: 0,
      irSwitch: 1, spurtMode: 0, spurtFlag: 0, goOrBack: 0, spurtTestFlag: 0,
      objectGoToFlag: 0, goaheadnum: 0,
      irF1: 68, irF2: 71, irL: 34, irR: 38, battMv: 8412, ms8: 37, flags: 17,
      evq: false, rate: 4.6, ageMs: 118, fifo: 0, batchN: 1, batchMs: 244,
    },
    hist: { rate: [] },
    /* 4 灯三态（led1..led4）。null = 未知：固件没实现 / 通道不在帧里 / 没连接。
       与后端 leds.py 同一套判据：先看 fw ≥ 311，再看通道清单。 */
    leds: { led1: true, led2: true, led3: true, led4: false },
    ledSupported: true,
    /* 视频复盘：一次只跑一个会话（上游子进程） */
    videoRun: { state: 'done', id: '20261021-210412', elapsed: 0, error: '', t0: 0 },
    videoSel: '20261021-210412',
  };
  let rateSeq = 0;

  /* ── 设备状态：三态（true / false / null=判不了）—— null 不许涂成红 */
  S.health = {
    mouse: {
      available: true, connected: true, port: 'COM7', fw: '2.13.0', fwInt: 313, proto: 9,
      link: 'uart 115200', detail: '', caps: { unified_channels: 33, led: true },
    },
    clock: { calibrated: true, offsetMs: 3.2, uncertaintyMs: 1.4, source: 'visual', method: 'rls', samples: 214 },
    timesync: { armed: true, armUpMs: 1420, driftPpm: 12.5, samples: 64 },
    video: { available: true, root: 'D:\\video', running: false },
  };

  /* 准备清单：改版二十四只剩三项，且**只有电脑鼠拦记录**
     （视频是离线批处理，不拦；时间基未标定只影响跨机对齐）。 */
  S.readiness = [
    { key: 'mouse', name: '电脑鼠', ok: true, blocking: true, state: 'COM7 · fw 2.13.0 · 4.6 Hz', action: '' },
    { key: 'clock', name: '时间基', ok: false, blocking: false, state: '未发车 · 对齐按 assumed', action: '打开时钟窗口' },
    { key: 'video', name: '视频复盘', ok: true, blocking: false, state: '3 个会话 · 最近 21:04', action: '' },
  ];

  /* ── 迷宫：确定性递归回溯 + BFS 路径 ─────────────────────────────────── */
  const maze = {
    N: 16,
    goal: [[7, 7], [7, 8], [8, 7], [8, 8]],
    v: [], h: [], path: [], visited: {}, idx: 0, frac: 0,
    car: { x: 0, y: 0, dir: 1 },
    cellCm: 18,
  };
  function mulberry32(a) {
    return function () {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  /* 生成一份独立迷宫（同一 seed 恒定）；车坐标统一用**格中心**口径 0.5–15.5 */
  function genMaze(seed) {
    const N = maze.N, rnd = mulberry32(seed || 20261021);
    const M = {
      N: N, goal: maze.goal, v: [], h: [], path: [], visited: {},
      idx: 0, frac: 0, cellCm: 18, car: { x: .5, y: .5, dir: 1 },
    };
    for (let y = 0; y < N; y++) {
      M.v.push(new Array(N - 1).fill(true));
      M.h.push(new Array(N).fill(true));
    }
    const seen = {}, stack = [[0, 0]];
    seen['0,0'] = 1;
    while (stack.length) {
      const [x, y] = stack[stack.length - 1];
      const nb = [];
      if (x > 0 && !seen[(x - 1) + ',' + y]) nb.push([x - 1, y, 'L']);
      if (x < N - 1 && !seen[(x + 1) + ',' + y]) nb.push([x + 1, y, 'R']);
      if (y > 0 && !seen[x + ',' + (y - 1)]) nb.push([x, y - 1, 'U']);
      if (y < N - 1 && !seen[x + ',' + (y + 1)]) nb.push([x, y + 1, 'D']);
      if (!nb.length) { stack.pop(); continue; }
      const pick = nb[Math.floor(rnd() * nb.length)];
      const [nx, ny, d] = pick;
      if (d === 'R') M.v[y][x] = false;
      if (d === 'L') M.v[ny][nx] = false;
      if (d === 'D') M.h[y][x] = false;
      if (d === 'U') M.h[ny][nx] = false;
      seen[nx + ',' + ny] = 1;
      stack.push([nx, ny]);
    }
    const isGoal = (x, y) => M.goal.some(g => g[0] === x && g[1] === y);
    const prev = {}, q = [[0, 0]], seen2 = { '0,0': 1 };
    let hit = null;
    while (q.length && !hit) {
      const [x, y] = q.shift();
      if (isGoal(x, y)) { hit = [x, y]; break; }
      const moves = [];
      if (x > 0 && !M.v[y][x - 1]) moves.push([x - 1, y]);
      if (x < N - 1 && !M.v[y][x]) moves.push([x + 1, y]);
      if (y > 0 && !M.h[y - 1][x]) moves.push([x, y - 1]);
      if (y < N - 1 && !M.h[y][x]) moves.push([x, y + 1]);
      moves.forEach(m => {
        const k = m[0] + ',' + m[1];
        if (seen2[k]) return;
        seen2[k] = 1; prev[k] = [x, y]; q.push(m);
      });
    }
    const path = [];
    if (hit) {
      let cur = hit;
      while (cur) { path.unshift(cur); cur = prev[cur[0] + ',' + cur[1]]; }
    } else { path.push([0, 0]); }
    M.path = path;
    path.forEach((p, i) => { if (i % 2 === 0 || i === path.length - 1) M.visited[p[0] + ',' + p[1]] = 1; });
    M.car = { x: path[0][0] + .5, y: path[0][1] + .5, dir: dirOf(path[0], path[1] || path[0]) };
    return M;
  }

  function buildMaze(seed) {
    const m = genMaze(seed);
    maze.v = m.v; maze.h = m.h; maze.path = m.path; maze.visited = m.visited;
    maze.idx = 0; maze.frac = 0; maze.car = m.car;
  }
  function dirOf(a, b) {
    if (!a || !b) return 0;
    const dx = b[0] - a[0], dy = b[1] - a[1];
    if (dx > 0) return 1; if (dx < 0) return 3;
    if (dy > 0) return 2; if (dy < 0) return 0;
    return 0;
  }
  buildMaze(20261021);

  /* ── 视频会话（改版二十四的“位置数据唯一来路”） ──────────────────────── */
  /* 轨迹点是 0..1 的归一化图像坐标（y 向下），与 Auto Locate 的 trajectory.csv 同口径。
     `valid=false` 的段必须断开：把断点连起来等于宣称“这一段走得通”。 */
  const TRACK_WAYPOINTS = [
    [0.09, 0.86], [0.09, 0.62], [0.26, 0.62], [0.26, 0.40], [0.42, 0.40],
    [0.42, 0.21], [0.58, 0.21], [0.58, 0.44], [0.74, 0.44], [0.74, 0.66],
    [0.50, 0.66], [0.50, 0.88], [0.66, 0.88],
  ];
  function buildTrajectory(seed, nTotal, gaps) {
    const rnd = mulberry32(seed || 7);
    const rows = [];
    const segs = TRACK_WAYPOINTS.length - 1;
    for (let i = 0; i < nTotal; i++) {
      const u = i / (nTotal - 1);
      const f = u * segs;
      const si = Math.min(segs - 1, Math.floor(f));
      const lt = f - si;
      const a = TRACK_WAYPOINTS[si], b = TRACK_WAYPOINTS[si + 1];
      const wob = (rnd() - 0.5) * 0.006;
      rows.push({
        frame: 12 + i * 2,
        t_video_s: +(i / (nTotal - 1) * 61.4).toFixed(3),
        x: +(util.lerp(a[0], b[0], lt) + wob).toFixed(4),
        y: +(util.lerp(a[1], b[1], lt) + wob).toFixed(4),
        rms_field: +(0.55 + rnd() * 0.5).toFixed(2),
        valid: true,
      });
    }
    (gaps || []).forEach(g => {
      for (let i = g[0]; i <= g[1] && i < rows.length; i++) {
        rows[i].valid = false;
        rows[i].rms_field = null;
      }
    });
    return rows;
  }

  const VCASES = [
    {
      id: '20261021-210412', video: 'race_1021_2104.mp4', size: 41820544, dur: 61.4, fps: 29.97,
      n_total: 892, n_valid: 877, frames: 1841, created: '21:04:12', state: 'done',
      flags: [], invalid_reasons: { decode: 9, no_marker: 6 },
      maze: { n_wall: 96, n_segment: 12, consistency: 0.98 },
      out_dir: 'D:\\AutoLocate\\sessions\\20261021-210412',
      timebase: { ok: true, trust: 'seconds', fps: 29.97, n_rows: 877, vfr: false, issues: [] },
      gaps: [[402, 409], [518, 521]],
      seed: 41,
    },
    {
      id: '20261021-204106', video: 'race_1021_2041.mp4', size: 12058624, dur: 21.8, fps: 30.0,
      n_total: 654, n_valid: 648, frames: 654, created: '20:41:06', state: 'done',
      flags: [], invalid_reasons: { decode: 6 },
      maze: { n_wall: 64, n_segment: 5, consistency: 0.91 },
      out_dir: 'D:\\AutoLocate\\sessions\\20261021-204106',
      timebase: { ok: true, trust: 'seconds', fps: 30, n_rows: 648, vfr: false, issues: [] },
      gaps: [[210, 215]],
      seed: 12,
    },
    {
      id: '20261020-195844', video: 'vfr_1020_1958.mp4', size: 20971520, dur: 33.2, fps: null,
      n_total: 741, n_valid: 690, frames: 741, created: '19:58:44', state: 'done',
      flags: ['vfr_suspected', 'maze_inconsistent'],
      invalid_reasons: { no_marker: 31, decode: 20 },
      maze: { n_wall: 96, n_segment: 19, consistency: 0.72 },
      out_dir: 'D:\\AutoLocate\\sessions\\20261020-195844',
      timebase: { ok: false, trust: 'frames', fps: null, n_rows: 690, vfr: true,
        issues: [{ code: 'vfr', detail: '相邻帧间隔抖动 12–84 ms' }] },
      gaps: [[88, 101], [330, 336], [501, 512]],
      seed: 29,
    },
  ];
  /* 场景切换要能重来，所以列表可重建（不是一次性常量） */
  function makeSessions() {
    return VCASES.map(c => {
      const rows = buildTrajectory(c.seed, c.n_total, c.gaps);
      return Object.assign({}, c, { rows: rows });
    });
  }
  S.videoSessions = makeSessions();

  /* ── 统一帧通道：29 个 / 六组；实时与比赛共用同一份码表 ──────────────── */
  const DIRS = ['北', '东', '南', '西'];
  const TASKS = ['WAIT', 'START', 'SEARCH', 'SPURT', 'SPURT45', 'SPURT90', 'END'];
  const MOTIONS = ['停止', '直行', '转弯', '斜走', '贴墙'];
  const SPURT = ['未选', '90_LOW', '45_LOW', 'MEDIUM', 'HIGH'];
  const FRONT = ['无墙', '近端', '远端'];

  const CODE = {
    dir: v => (DIRS[v] || v) + ' (' + v + ')',
    mazeTask: v => (TASKS[v] || v) + ' (' + v + ')',
    motionTask: v => (MOTIONS[v] || v) + ' (' + v + ')',
    spurtMode: v => (SPURT[v] || v) + ' (' + v + ')',
    frontNear: v => (FRONT[v] || v) + ' (' + v + ')',
    frontFar: v => (FRONT[v] || v) + ' (' + v + ')',
    frontBoth: v => (FRONT[v] || v) + ' (' + v + ')',
    goOrBack: v => (v ? '回程' : '去程') + ' (' + v + ')',
  };

  const GROUPS = [
    { name: '运动与姿态', keys: [
      ['speedL', '左轮速', ' cm/s', 1], ['speedR', '右轮速', ' cm/s', 1],
      ['gyroAngle', '陀螺角', ' °', 1], ['encAngle', '编码器角', ' °', 1],
      ['motionTask', '动作'], ['dir', '朝向']] },
    { name: '里程与位置', keys: [
      ['trackL', '左里程', ' mm', 0], ['trackR', '右里程', ' mm', 0],
      ['posX', '位置 X', '', 2], ['posY', '位置 Y', '', 2]] },
    { name: '迷宫判定', keys: [
      ['mazeTask', '迷宫任务'], ['wallLeft', '墙·左'], ['wallFront', '墙·前'],
      ['wallRight', '墙·右'], ['frontNear', '前墙·近端'], ['frontFar', '前墙·远端'],
      ['frontBoth', '双档命中'], ['irSwitch', '红外采样']] },
    { name: '比赛冲刺', keys: [
      ['spurtMode', '冲刺档位'], ['spurtFlag', '冲刺标志'], ['goOrBack', '去/回'],
      ['spurtTestFlag', '冲刺试探'], ['objectGoToFlag', '物体去向'], ['goaheadnum', '前冲计数']] },
    { name: '四路红外', keys: [
      ['irF1', '前左', ' mm', 0], ['irF2', '前右', ' mm', 0],
      ['irL', '左', ' mm', 0], ['irR', '右', ' mm', 0]] },
    { name: '电源与链路', keys: [
      ['battMv', '电池', ' mV', 0], ['ms8', '帧时基低 8 位', ' ms', 0], ['flags', '标志位']] },
  ];

  function valText(k, v) {
    if (v === null || v === undefined) return '—';
    if (CODE[k]) return CODE[k](v);
    if (k === 'flags') {
      /* 位定义**照抄上游** `host/protocol/codec.py`（改版二十六修正）：
           FLAG_FREEMODE   = 0x01  (1)   自由模式（电机停）
           FLAG_IR_SWITCH  = 0x10  (16)  红外采样轮在跑
           FLAG_ESTOP_LOCK = 0x40  (64)  急停**锁存**（新的 MOVE/CAL 会被拒）
         原来这里把急停写成 128（0x80）——那一位上游没有，于是"急停锁存中"
         这个最该看见的状态**永远画不出来**，而 0x40 又被漏掉 ⇒ 两个错同时存在。 */
      const bits = [];
      if (v & 1) bits.push('自由');
      if (v & 16) bits.push('IR');
      if (v & 64) bits.push('急停锁存');
      return (bits.length ? bits.join('+') : '无') + ' (' + v + ')';
    }
    if (typeof v === 'boolean') return v ? '是' : '否';
    return util.fmt(v, 0);
  }

  /* ── 比赛记录：5 条历史；launch=有没有发车记录，video=关联的视频会话 ───── */
  const RECORDS = [
    { id: 'R-20261021-03', start: '21:04:12', dur: 228, frames: 1049, launch: true, launchAt: 6.4,
      video: '20261021-210412', ledger: true, gap: 0, seed: 41, mazeSeed: 20261021,
      dir: 'sessions/race_1021_2104' },
    { id: 'R-20261021-02', start: '20:41:06', dur: 132, frames: 607, launch: true, launchAt: 4.1,
      video: '20261021-204106', ledger: true, gap: 0, seed: 12, mazeSeed: 20261021,
      dir: 'sessions/race_1021_2041' },
    { id: 'R-20261020-03', start: '19:58:44', dur: 271, frames: 1247, launch: true, launchAt: 5,
      video: '20261020-195844', ledger: false, gap: 3, seed: 29, mazeSeed: 20261018,
      dir: 'sessions/race_1020_1958' },
    { id: 'R-20261020-02', start: '17:22:09', dur: 58, frames: 267, launch: false, launchAt: null,
      video: '', ledger: true, gap: 0, seed: 7, mazeSeed: 20261018,
      dir: 'sessions/race_1020_1722' },
    { id: 'R-20261020-01', start: '16:50:31', dur: 127, frames: 584, launch: true, launchAt: 3.2,
      video: '', ledger: true, gap: 0, seed: 3, mazeSeed: 20261017,
      dir: 'sessions/race_1020_1650' },
  ];

  function vary(it, seed) {
    if (it.derived) return it.value;
    if (it.id === 1) return +(it.value + ((seed % 7) - 3) * .1).toFixed(4);
    if (it.id === 41) return it.value + (seed % 5) - 2;
    return it.value;
  }

  function buildRecord(rec) {
    const m = genMaze(rec.mazeSeed);
    const n = rec.dur * 4 + 1;
    const samples = [];
    const t0 = rec.launch ? rec.launchAt : 0;
    const span = Math.max(1, rec.dur - 4 - t0);
    for (let i = 0; i < n; i++) {
      const s = i / 4;
      const p = rec.launch ? util.clamp((s - t0) / span, 0, 1) : 0;
      const f = p * Math.max(1, m.path.length - 1);
      const k = Math.min(m.path.length - 2, Math.max(0, Math.floor(f)));
      const a = m.path[k] || m.path[0], b = m.path[k + 1] || m.path[0];
      const x = util.lerp(a[0] + .5, b[0] + .5, f - k);
      const y = util.lerp(a[1] + .5, b[1] + .5, f - k);
      const dir = dirOf(a, b);
      const cx = util.clamp(Math.floor(x), 0, m.N - 1);
      const cy = util.clamp(Math.floor(y), 0, m.N - 1);
      const running = !!rec.launch && s > t0 && p < 1;
      samples.push({
        t: +s.toFixed(2),
        frame: 100 + Math.round(i * 1.07),
        pi: f,
        posX: +x.toFixed(2), posY: +y.toFixed(2),
        dir: dir,
        speedL: running ? +(34 + Math.sin(i / 5) * 2.4).toFixed(1) : 0,
        speedR: running ? +(34 - Math.sin(i / 5) * 2.4).toFixed(1) : 0,
        mazeTask: rec.launch ? (p <= 0 ? 1 : (p >= 1 ? 6 : 3)) : 0,
        motionTask: running ? 2 : 0,
        wallLeft: running ? wallAheadIn(m, cx, cy, (dir + 3) % 4) : 0,
        wallFront: running ? wallAheadIn(m, cx, cy, dir) : 0,
        wallRight: running ? wallAheadIn(m, cx, cy, (dir + 1) % 4) : 0,
        battMv: Math.round(8412 - p * 120),
        flags: running ? 17 : 1,
      });
    }
    const params = S.params.map(g => ({
      name: g.name, range: g.range,
      items: g.items.map(it => ({
        id: it.id, name: it.name, unit: it.unit, derived: it.derived,
        value: vary(it, rec.seed),
      })),
    }));
    return Object.assign({}, rec, { maze: m, samples: samples, params: params });
  }

  function makeRecords() { return RECORDS.map(buildRecord); }

  /* 记录 → 某一时刻的迷宫视图（车在格中心，轨迹只到当前帧） */
  function recordView(rec, i) {
    if (!rec) return null;
    // 真记录（后端回放视图）：迷宫来自帧重放，样本是设备给的
    if (rec.maze && rec.maze.N && rec.samples && rec.samples.length) {
      return replayViewLive(rec, i);
    }
    return recordViewProto(rec, i);
  }

  /* 真记录：`visited` 与 `trail` **按进度现算**（只算到当前样本）——
     一上来就把整张图点亮，等于提前告诉观众"车去过哪儿"。 */
  function replayViewLive(rec, i) {
    const m = rec.maze;
    const k = util.clamp(i | 0, 0, rec.samples.length - 1);
    const visited = {}, trail = [];
    let last = null;
    for (let j = 0; j <= k; j++) {
      const s = rec.samples[j];
      if (!s || s.posX == null || s.posY == null) continue;
      const cx = Math.floor(s.posX), cy = Math.floor(s.posY);
      visited[cx + ',' + cy] = 1;
      if (!last || last[0] !== cx || last[1] !== cy) {
        trail.push([cx + .5, cy + .5]);
        last = [cx, cy];
      }
    }
    const sm = rec.samples[k] || {};
    return {
      N: m.N,
      goal: m.goal || [],
      v: m.v, h: m.h,
      visited: visited,
      trail: trail,
      path: [],                       // 回放同样只有 trail，没有规划路径
      car: { x: (sm.posX == null ? .5 : sm.posX),
             y: (sm.posY == null ? .5 : sm.posY),
             dir: (sm.dir == null ? 0 : sm.dir) },
    };
  }

  function recordViewProto(rec, i) {
    const m = rec.maze;
    const sm = rec.samples[util.clamp(i | 0, 0, rec.samples.length - 1)];
    const visited = {};
    const trail = [];
    if (rec.launch) {
      const upto = Math.min(m.path.length - 1, Math.floor(sm.pi));
      for (let k = 0; k <= upto; k++) {
        visited[m.path[k][0] + ',' + m.path[k][1]] = 1;
        trail.push([m.path[k][0] + .5, m.path[k][1] + .5]);
      }
    }
    return {
      N: m.N, goal: m.goal, v: m.v, h: m.h, path: m.path,
      visited: visited, trail: trail,
      car: { x: sm.posX, y: sm.posY, dir: sm.dir },
    };
  }

  S.records = [];
  S.recSel = '';

  /* ── 事件总线 ─────────────────────────────────────────────────────────── */
  const bus = {};
  function on(evt, fn) { (bus[evt] = bus[evt] || []).push(fn); }
  function emit(evt, payload) { (bus[evt] || []).forEach(fn => { try { fn(payload); } catch (e) { console.error(e); } }); }

  /* ── 后端调用（改版二十六：**全前端唯一一处网络**）───────────────────────
     为什么只开这一个口：其余数字仍是本地造的（迷宫、记录、轨迹），但"每批帧数 N"
     不是一个能假装的量 —— 它改的是**设备怎么发帧**。假装就等于"界面显示 10 Hz、
     设备还在 4 Hz"，而现场看不出来（最坏的一种静默错）。

     ⚠️ 失败一律**说出来**（后端没起 / 没连鼠 / 上游不支持），不许吞掉之后
        让界面继续维持"已生效"的样子。三态纪律：判不了就说判不了。 */
  const PULL_N_MIN = 1, PULL_N_MAX = 16, PULL_T_MS = 100;

  function api(path, body) {
    const opt = body === undefined
      ? { method: 'GET' }
      : { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body) };
    return fetch(path, opt).then(function (r) {
      return r.json().catch(function () { return null; }).then(function (j) {
        return { status: r.status, ok: r.ok, body: j };
      });
    });
  }

  /* ── 真后端接入（改版二十八 · P4）───────────────────────────────────────
     **一条原则：能连上后端就用真数据，连不上就退回原型。**
     切换只发生在 `LIVE.on` 这一处 —— 否则真值与假值会互相覆盖，
     而且没人看得出哪一半是真的。

     · `LIVE.on === false`（默认 / 后端没起）：250 ms 模拟 tick 照旧，
       界面仍可脱离设备做视觉评审（这是这版界面原本的用途）。
     · `LIVE.on === true`：`tick()` 不再伪造遥测，`S.tel` / `S.leds` / `S.rateLog`
       全部来自 WS `mouse.telemetry`；动作走 REST。

     ⚠️ **已经接上的**：health/caps、WS 遥测与 4 灯、8 个动作、视频起/取消、
     会话开/收。
     ⚠️ **还没接的**（要按 shape 转换，见设计方案 §11.30）：参数表
     （后端按"组名→项名"索引，前端要数组）、比赛记录表、回放（要 `recordView`）、
     迷宫图（后端 `wall` 结构 ≠ 前端 `Maze`）。这些页面在 live 模式下会显示
     原型数据 —— **看它们之前先看左轨那句"原型 · 纯前端"**。 */
  const LIVE = { on: false, err: '', ws: null, caps: null, timer: null, vtimer: null,
    // 拉取相关的运行态：`pullBusy` 防重入、`pullOffAt` 记"人工停的时刻"（看门狗据此让 8 s）
    pullBusy: false, pullOffAt: 0, guard: null };

  function liveCleanup() {
    if (LIVE.timer) { clearInterval(LIVE.timer); LIVE.timer = null; }
    if (LIVE.vtimer) { clearInterval(LIVE.vtimer); LIVE.vtimer = null; }
  }

  function unwrap(res) {
    const b = res.body || {};
    if (res.ok && b.ok) return b.data || {};
    const e = new Error((b.error && (b.error.message || b.error.type))
      || ('HTTP ' + res.status));
    e.status = res.status;
    e.detail = (b.error && b.error.message) || '';
    throw e;
  }

  /* ⚠️ `post()` 一定会发 **POST**。第一次接线时拿它去打 `/api/v1/health`
     （只挂了 GET）⇒ 405 ⇒ 整个实时模式没起来，而页面上只表现为"还是原型数据"。
     ⇒ 读用 `get()`、写用 `post()`，别混。 */
  function post(path, body) {
    return api(path, body === undefined ? {} : body).then(unwrap);
  }

  function get(path) {
    return api(path).then(unwrap);
  }

  /* DELETE：`api()` 只做 GET/POST（按有没有 body 区分），删除得单独一条。
     ⚠️ 不把 DELETE 塞进 `api()`：那个函数被四个地方依赖，改它的行为要连带验一遍。 */
  function del(path) {
    return fetch(path, { method: 'DELETE' }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        return { status: r.status, ok: r.ok, body: j };
      });
    }).then(unwrap);
  }

  function applyCaps(d) {
    if (!d) return;
    LIVE.caps = d;
    const m = S.health.mouse;
    if (d.firmware != null) m.fw = String(d.firmware);
    if (d.fw_int != null) m.fwInt = Number(d.fw_int);
    if (d.unified_channels) {
      m.caps = m.caps || {};
      m.caps.unified_channels = d.unified_channels.length;
      m.caps.led = true;
      S.ledSupported = d.unified_channels.indexOf('led1') >= 0;
    }
    if (d.pull && d.pull.n != null) { S.pullN = Number(d.pull.n); S.tel.batchN = S.pullN; }
  }

  /* ⚠️ `/health` 的形状：`{ok, all_ready, readiness:{items}, mouse:{...}, video:{...},
     clock:{...}, session: <to_dict|null>, ...}` —— **`connected` 在 `mouse` 底下**，
     不是顶层。写错过一次：把 `d.connected` 读成顶层，结果永远 `false`，
     而界面上只表现为"一直未连接"（不报错）。 */
  function applyHealth(d) {
    if (!d) return;
    const mo = d.mouse || {};
    const m = S.health.mouse;
    m.available = mo.available !== false;
    m.connected = !!mo.connected;
    m.port = mo.port || '';
    if (mo.device && mo.device.fw != null) m.fw = String(mo.device.fw);
    m.detail = mo.detail || '';
    m.streamMode = mo.stream_mode || m.streamMode || '';
    // 拉取的**真值**来自后端（改版三十一：连上不再自动拉，按页面开）
    if (typeof mo.stream_on === 'boolean') S.pulling = mo.stream_on;
    S.estop = !!mo.estop_locked;
    if (d.clock) {
      S.health.clock.calibrated = !!d.clock.calibrated;
      if (d.clock.offset_ms != null) S.health.clock.offsetMs = d.clock.offset_ms;
    }
    const v = d.video || {};
    S.health.video.state = v.state || 'idle';
    S.health.video.running = v.state === 'running';
    // 准备清单：字段与后端 `readiness.items` 一一对应（key/name/blocking/ok/state/action）
    const rd = (d.readiness && d.readiness.items) || null;
    if (rd) {
      S.readiness = rd.map(function (it) {
        return { key: it.key, name: it.name, ok: !!it.ok, blocking: !!it.blocking,
                 state: it.state || '', action: it.action || '' };
      });
    }
    // 会话：`null` = 没开（这是后端给的权威三态，不是"没读到"）
    if (d.session && d.session.session_id) {
      S.session.active = true;
      S.session.id = d.session.session_id;
    } else if (d.session === null) {
      S.session.active = false;
    }
    /* ⚠️ **必须通知外壳重画**（只重画顶栏/设备片/准备清单，不重挂页面）。
       踩过：数据更新了但顶栏还挂着上一次渲染的"已连接 COM7"，
       于是"后端说没连、界面上还写着 COM7"——判据 V5 就是这么抓到的。
       为什么不用 `emit('state')`：那个会**整页重挂**，每秒一次会把参数页
       正在输入的框清掉。 */
    emit('health');
  }

  /* WS 遥测 → S。**4 灯直接取后端的 `leds`，不再前端重算**（铁律 54：
     接线时删前端那份，否则同一件事两个真相）。 */
  function applyTelemetry(msg) {
    const u = msg.unified || {};
    Object.keys(u).forEach(function (k) { if (k in S.tel) S.tel[k] = u[k]; });
    if (msg.leds) S.leds = msg.leds;
    const r = (msg.rate && msg.rate.unified) || {};
    if (r.hz != null) S.tel.rate = r.hz;
    if (r.age_ms != null) S.tel.ageMs = r.age_ms;
    if (Array.isArray(r.batches) && r.batches.length) {
      S.rateLog = r.batches.map(function (b) {
        return { seq: b.seq, n: b.n, ms: b.ms, hz: b.hz };
      });
      const last = r.batches[r.batches.length - 1];
      S.tel.batchN = last.n; S.tel.batchMs = last.ms;
    }
    // 状态位：`mazeTask >= 2` 才算"在跑"（速度过零会闪，任务机是状态不是瞬时量）
    S.running = (S.tel.mazeTask | 0) >= 2;
  }

  function applyMaze(msg) {
    const st = msg.state || msg;
    if (!st) return;
    S.mazeLive = st;
    if (st.task != null) S.tel.mazeTask = st.task;
  }

  function wsOpen() {
    if (LIVE.ws) { try { LIVE.ws.close(); } catch (e) { /* 已关 */ } LIVE.ws = null; }
    let ws;
    try {
      ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://')
        + location.host + '/ws');
    } catch (e) { return; }
    LIVE.ws = ws;
    ws.onmessage = function (ev) {
      // 二进制帧（`mouse.wave`）本版没有解码器，也没有波形页 ⇒ 直接忽略
      if (typeof ev.data !== 'string') return;
      let m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m.type === 'mouse.telemetry') applyTelemetry(m);
      else if (m.type === 'mouse.maze') applyMaze(m);
      else if (m.type === 'hello' && m.health) applyHealth(m.health);
      else if (m.type === 'log') log(m.source || 'control', m.level || 'info', m.text || '');
    };
    ws.onclose = function () { LIVE.ws = null; };
  }

  /* 连上后端就切到真数据；连不上什么都不改（保持原型）。只试一次 + 定频轮询。 */
  function startLive() {
    const tryOnce = function () {
      // 读一律用 `get()`：`post()` 会发 POST，而 `/health` 只挂了 GET（405 ⇒ 静默不生效）
      return get('/api/v1/health').then(function () {
        return get('/api/v1/caps').then(function (c) {
          applyCaps((c && c.mouse) || null);
        });
      }).then(function () {
        LIVE.on = true; LIVE.err = '';
        wsOpen();
        log('control', 'info', '已连后端 · 切到真数据（遥测走 WS）');
        emit('state');
        // 先按"当前这页需不需要"决定拉不拉，再取数：参数表读表依赖拉取在跑。
        syncPull(S.route).then(function () {
          loadParams(); loadRecords(); loadVideoSessions();
        });
        // 看门狗常驻：哪一页要帧就保证它在拉（自己掉的会重开，人工停的让 8 s）
        startPullGuard();
        if (!LIVE.timer) {
          LIVE.timer = setInterval(function () {
            get('/api/v1/health').then(applyHealth).catch(function () {
              LIVE.on = false;
              if (LIVE.ws) { try { LIVE.ws.close(); } catch (e) { /* 已关 */ } }
              liveCleanup();
              log('control', 'warn', '与后端的连接断了 · 退回原型数据');
              emit('state');
            });
          }, 1000);
        }
      });
    };
    tryOnce().then(function () {
      return get('/api/v1/health');
    }).then(applyHealth).catch(function (e) {
      LIVE.on = false;
      LIVE.err = (e && (e.detail || e.message)) || String(e);
      // 原型模式：不吵人，日志里留一条就好（"没起后端"是常态，不是故障）
      log('control', 'info', '后端未连（' + LIVE.err + '）· 按原型数据演示');
    });
  }

  /* ── 四页数据：参数表 / 比赛记录 / 视频会话 / 回放 ─────────────────────────
     这一层只做**形状翻译**（后端字段 → 界面字段），**不算任何业务量** ——
     派生量、三态、口径全部由后端给（铁律 32：一个元素一条真相）。

     ⚠️ 翻译里最容易出错的是"缺值"。后端给 `null` 就是"判不了 / 没记录"，
     一律**照传 null**，绝不用 `0` / `''` / `false` 顶：
     那样界面会把"不知道"显示成"没问题"或"没数据"，是最难查的一类错。 */

  // 参数组：**顺序与显示名**是界面的事（后端只保证"有哪些组、每组有哪些项"）。
  // `key` 只用于前端自己定位，不上后端。
  const GROUP_UI = [
    { name: '生产标定', key: 'base' },
    { name: '红外测量', key: 'ir' },
    { name: '电源监控', key: 'batt' },
    { name: '运动控制', key: 'motion' },
    { name: '课程调参', key: 'lesson' },
  ];

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  /* 分组字典 → 界面用的分组数组。**回放快照与实时参数表共用这一份**
     （`rec.params` 以前直接拿去 `.map`，而它其实是 `{groups:{...}}` 字典 ⇒
      `rec.params.map is not a function`，整页渲染失败）。 */
  function paramsToArray(d) {
    if (!d || !d.groups) return null;
    const meta = d.groups_meta || {};
    const out = [];
    GROUP_UI.forEach(function (g) {
      const g0 = d.groups[g.name];
      if (!g0) return;
      const gm = meta[g.name] || {};
      const items = [];
      Object.keys(g0).forEach(function (nm) {
        const it = g0[nm] || {};
        items.push({
          id: (it.id == null ? null : it.id),
          name: nm,
          value: (it.value == null ? null : it.value),
          unit: it.unit || '',
          desc: it.desc || '',
          // 派生量只读：后端连 `readable` 都给 false，写它没有意义
          derived: !!it.derived,
          derivedFrom: it.derived_from || '',
          dec: (it.decimals == null ? 0 : it.decimals),
          // `q16` 是 Q16.16 原值；派生量没有原值（不占 Flash）⇒ null，不是 0
          q16: (it.derived ? null : (it.q == null ? null : it.q)),
        });
      });
      items.sort(function (a, b) {
        if (a.id == null) return 1;
        if (b.id == null) return -1;
        return a.id - b.id;
      });
      const lo = gm.id_min, hi = gm.id_max;
      out.push({
        key: g.key,
        name: g.name,
        range: (lo != null && hi != null) ? (pad2(lo) + '–' + pad2(hi)) : '',
        page: (gm.flash_page == null ? null : gm.flash_page),
        via: (gm.write_via ? String(gm.write_via).replace('mc.', '') + ' + SAVE' : ''),
        note: '',
        items: items,
      });
    });
    return out.length ? out : null;
  }

  function applyParams(d) {
    const out = paramsToArray(d);
    if (!out) return false;
    S.params = out;
    S.paramsNote = (d && d.note) || '';
    S.paramsCount = (d && d.count);
    return true;
  }

  function loadParams() {
    if (!LIVE.on) return Promise.resolve(false);
    return get('/api/v1/mouse/params/grouped').then(function (d) {
      const okp = applyParams(d);
      if (okp) emit('state');
      return okp;
    }).catch(function (e) {
      // 没连鼠时这个端点是 409：如实说一句，不把"没连"说成"参数读不到"
      log('mouse', 'warn', '参数表取不到：' + ((e && (e.detail || e.message)) || e));
      return false;
    });
  }

  function applyRecords(d) {
    const rows = (d && d.records) || [];
    const list = rows.filter(function (r) { return !r.error; }).map(function (r) {
      return {
        id: r.id,
        name: r.name,
        start: r.start || '',
        dur: (r.dur_s == null ? null : r.dur_s),
        frames: (r.frames == null ? null : r.frames),
        launch: !!r.launch,
        // 无发车记录 ⇒ null（**不是 0**：0 是"发车就在第 0 秒"，那是另一件事）
        launchAt: (r.launch_at_s == null ? null : r.launch_at_s),
        video: r.video_sid || '',
        // 三态：true/false/null（老记录没有 seq_lost_delta ⇒ 判不了）
        ledger: (r.ledger === true ? true : (r.ledger === false ? false : null)),
        gap: (r.gap == null ? null : r.gap),
        seed: null,                    // 记录里没有参数扰动种子 ⇒ 恒 null
        mazeSeed: (r.maze_seed == null ? null : r.maze_seed),
        dir: r.dir || '',
        size: (r.size == null ? null : r.size),
        endWhy: r.end_why || '',
        // 回放要用的两块：**按需另取**（一条记录上千帧，列表不该带着走）
        samples: [], params: null, maze: null,
      };
    });
    S.records.length = 0;
    Array.prototype.push.apply(S.records, list);
    if (!S.records.some(function (r) { return r.id === S.recSel; })) {
      S.recSel = (list.length ? list[0].id : '');
    }
    S.recRoot = (d && d.root) || '';
    return list.length;
  }

  function loadRecords() {
    if (!LIVE.on) return Promise.resolve(0);
    return get('/api/v1/records?n=50').then(function (d) {
      const n = applyRecords(d);
      emit('state');
      return n;
    }).catch(function (e) {
      log('control', 'warn', '比赛记录取不到：' + ((e && (e.detail || e.message)) || e));
      return 0;
    });
  }

  function applyVideoSessions(d) {
    const rows = (d && d.sessions) || [];
    S.videoSessions = rows.map(function (v) {
      return {
        id: v.id,
        video: v.video_name || '',
        size: (v.size_bytes == null ? null : v.size_bytes),
        dur: (v.dur_s == null ? null : v.dur_s),
        fps: (v.fps == null ? null : v.fps),
        n_total: (v.n_total == null ? null : v.n_total),
        n_valid: (v.n_valid == null ? null : v.n_valid),
        frames: (v.frames == null ? null : v.frames),
        // ⚠️ 后端给的是 `HH:MM`（界面按 `created.slice(0,5)` 用）；
        //    给 ISO 就会显示成"最近 2026-"。要完整时间用 `createdAt`。
        created: v.created || '',
        createdAt: v.created_at || '',
        state: 'done',
        flags: Array.isArray(v.flags) ? v.flags : [],
        invalid_reasons: v.invalid_reasons || null,
        maze: null,
        out_dir: v.out_dir || '',
        hasTrajectory: !!v.has_trajectory,
        // 轨迹与时间轴：选中时再取（一行几千点，列表不该带着走）
        rows: [], timebase: null,
      };
    });
    if (!S.videoSessions.some(function (v) { return v.id === S.videoSel; })) {
      S.videoSel = (S.videoSessions.length ? S.videoSessions[0].id : '');
    }
    S.videoRoot = (d && d.root) || S.videoRoot || '';
    return S.videoSessions.length;
  }

  function loadVideoSessions() {
    if (!LIVE.on) return Promise.resolve(0);
    return get('/api/v1/video/sessions').then(function (d) {
      const n = applyVideoSessions(d);
      emit('state');
      return n;
    }).catch(function (e) {
      log('video', 'warn', '视频会话列表取不到：' + ((e && (e.detail || e.message)) || e));
      return 0;
    });
  }

  /* 轨迹页要的 `rows` + `timebase`：**选中哪个取哪个**（一次一条）。
     `sampled > 1` 时帧号是跳着的，界面上要能说出来（后端在 `sampled` 里自报）。 */
  function loadTrajectory(sid) {
    if (!LIVE.on || !sid) return Promise.resolve(null);
    const ses = S.videoSessions.find(function (v) { return v.id === sid; });
    if (!ses) return Promise.resolve(null);
    if (ses.rows && ses.rows.length) return Promise.resolve(ses);
    return get('/api/v1/video/trajectory/' + encodeURIComponent(sid)).then(function (d) {
      ses.rows = (d && d.rows) || [];
      ses.timebase = (d && d.timebase) || null;
      ses.sampled = (d && d.sampled) || 1;
      emit('state');
      return ses;
    }).catch(function (e) {
      log('video', 'warn', '轨迹取不到：' + ((e && (e.detail || e.message)) || e));
      return null;
    });
  }

  /* 回放：**一条一次取全**（含迷宫、逐样本状态、发车前参数快照）。
     取回之前 `samples` 是空数组 —— 界面必须显示"还没取到"，不许画一辆假车。 */
  function loadReplay(id) {
    if (!LIVE.on) return Promise.resolve(null);
    const rec = S.records.find(function (r) { return r.id === id; });
    if (!rec) return Promise.resolve(null);
    if (rec.samples && rec.samples.length) return Promise.resolve(rec);
    return get('/api/v1/records/' + encodeURIComponent(rec.name) + '/replay').then(function (d) {
      rec.dur = (d.dur_s == null ? rec.dur : d.dur_s);
      rec.frames = (d.frames == null ? rec.frames : d.frames);
      rec.launch = !!d.launch;
      rec.launchAt = (d.launch_at_s == null ? null : d.launch_at_s);
      if (d.video_sid) rec.video = d.video_sid;
      rec.ledger = (d.ledger === true ? true : (d.ledger === false ? false : null));
      rec.gap = (d.gap == null ? null : d.gap);
      // 采样缺口（改版三十八）：**与 `gap` 不是一回事** ——
      // `gap` 是协议层丢帧，`gaps` 是"这段时间压根没请求"（发车命令独占链路）。
      rec.gaps = Array.isArray(d.gaps) ? d.gaps : [];
      rec.gapsN = (d.gaps_n == null ? rec.gaps.length : d.gaps_n);
      rec.launchCmd = d.launch_cmd || null;
      rec.maze = d.maze || null;
      rec.mazeNote = (d.maze && d.maze.note) || '';
      rec.samples = d.samples || [];
      // 老记录 → null（界面显示「未记录」）；有快照就转成**分组数组**
      rec.params = d.params ? paramsToArray(d.params) : null;
      rec.paramsAtMs = d.params_at_ms;
      rec.sampled = d.sampled || 1;
      emit('state');
      return rec;
    }).catch(function (e) {
      log('control', 'warn', '回放取不到：' + ((e && (e.detail || e.message)) || e));
      return null;
    });
  }

  /* 一页要用什么，进页时才拉（一次进入最多一到两个请求）。 */
  function needPage(id) {
    if (!LIVE.on) return;
    if (id === 'params' && !S.params) loadParams();
    else if (id === 'data') loadRecords();
    else if (id === 'video') loadVideoSessions();
    else if (id === 'track') loadVideoSessions().then(function () { loadTrajectory(S.videoSel); });
    else if (id === 'replay') loadRecords().then(function () { loadReplay(S.recSel); });
  }

  /* ── 设备连接：**真的去连** ────────────────────────────────────────────────
     为什么单独有这一层：抽屉里原来的【连接】只改本地状态就弹"已连接"，一个请求
     都不发；而 1 s 的 `/health` 轮询会把真话拉回来 ⇒ 界面表现成"连上又断开"，
     现场只会怀疑串口/驱动。**凡是要动硬件的按钮，必须真的发请求。**

     连上之后立刻刷 `caps`（fw / 通道数）与 `health`（状态 / 准备清单），
     不等下一秒轮询 —— 否则那 1 s 里界面还是旧状态，看起来像没连上。 */
  function connectMouse(target, baud) {
    if (!LIVE.on) {
      const e = new Error('还没连上后端（页面在原型模式）');
      e.detail = '后端没起来或不可达，先把服务起起来再连设备。';
      return Promise.reject(e);
    }
    return post('/api/v1/mouse/connect',
      { target: target, baud: baud == null ? undefined : baud })
      .then(function (r) {
        return Promise.all([
          get('/api/v1/caps').catch(function () { return null; }),
          get('/api/v1/health').catch(function () { return null; }),
        ]).then(function (pair) {
          if (pair[0]) applyCaps(pair[0].mouse || null);
          if (pair[1]) applyHealth(pair[1]);
          // 连接是"整体状态变了"：页面上的门控（发车/开始记录/维护动作）要跟着重算。
          // 这里用 `state` 而不是 `health`，因为页内按钮的 disabled 在页面自己手里。
          emit('state');
          // 连上后**不一定**要拉：只在当前这页需要时开（改版三十一）。
          // ⚠️ 顺序：先 syncPull 再 loadParams —— 参数表读表依赖拉取在跑。
          syncPull(S.route).then(function () { loadParams(); });
          return r;
        });
      });
  }

  function disconnectMouse() {
    if (!LIVE.on) return Promise.resolve({ ok: true });
    return post('/api/v1/mouse/disconnect', {}).then(function (r) {
      return get('/api/v1/health').catch(function () { return null; }).then(function (h) {
        if (h) applyHealth(h);
        emit('state');
        return r;
      });
    });
  }

  function applyPorts(d) {
    const t = (d && d.targets) || [];
    S.ports = t.map(function (x) {
      return {
        label: x.label || x.port || x.target || '',
        target: x.target || x.port || '',
        port: x.port || '',
        kind: x.kind || '',
        baud: x.baud,
      };
    });
    S.portsNote = (d && d.note) || '';
    return S.ports.length;
  }

  /* 端口列表也来自后端（`/mouse/ports` 扫的是本机真实串口）。
     带缓存：列表在抽屉打开时就要有，不能每次都等一次串口枚举。 */
  function listPorts(force) {
    if (!LIVE.on) return Promise.resolve(S.ports || []);
    if (!force && S.ports) return Promise.resolve(S.ports);
    return get('/api/v1/mouse/ports').then(function (d) {
      applyPorts(d);
      return S.ports;
    }).catch(function (e) {
      S.portsErr = (e && (e.detail || e.message)) || String(e);
      log('mouse', 'warn', '串口列表取不到：' + S.portsErr);
      return S.ports || [];
    });
  }

  /* ── 比赛页的实时迷宫 ─────────────────────────────────────────────────────
     数据源 `GET /mouse/maze/reconstruct`（上游 30 s 环形缓冲重建）+ 它的 `view`
     （后端已经把位掩码翻成 `v/h/visited/trail`，界面不再解二进制）。

     **一帧都没用上 ⇒ 置 null**：清空/换场后地图必须能重新变空白，
     不能留着上一张图当"现在的地图"（那是编的）。 */
  function loadMazeView() {
    if (!LIVE.on || !S.health.mouse.connected) return Promise.resolve(null);
    return get('/api/v1/mouse/maze/reconstruct').then(function (d) {
      if (d && d.mazetype) S.mazeN = d.mazetype;
      const v = (d && d.view) || null;
      /* 有没有内容看 **`frames_total`（这张图一共攒了多少帧）**，不是 `frames_used`。
         `frames_used` 是"这一轮新喂了几帧"——车停着 1.2 s 内没有新帧时它是 0，
         拿它判断就会让图每 1.2 s 闪成一次空白（把"没动"误读成"没有数据"）。 */
      S.mazeView = (v && (d.frames_total || 0) > 0)
        ? Object.assign({
          frames_used: d.frames_used, frames_total: d.frames_total,
          covered_s: d.covered_s, window_s: d.window_s,
        }, v)
        : null;
      emit('health');                     // 只重画，不整页重挂
      return S.mazeView;
    }).catch(function (e) {
      log('mouse', 'warn', '迷宫重构取不到：' + ((e && (e.detail || e.message)) || e));
      return null;
    });
  }

  /* 清空比赛页的实时墙图（只清这张图；记录 / 设备 / 拉取都不动）。 */
  function clearMazeView() {
    if (!LIVE.on) { S.mazeView = null; emit('health'); return Promise.resolve(true); }
    return post('/api/v1/mouse/maze/reconstruct/clear', {})
      .then(function () {
        S.mazeView = null;
        log('mouse', 'info', '实时墙图已清空 · 从下一帧重新累积');
        toast('', '已清空', '记录与设备都没动，继续跑就会重新画出来');
        emit('health');
        return true;
      })
      .catch(function (e) {
        const msg = (e && (e.detail || e.message)) || String(e);
        log('mouse', 'warn', '清空失败：' + msg);
        toast('bad', '清空失败', String(msg).slice(0, 120));
        return false;
      });
  }

  let raceTimer = null;
  function startRaceTick() {
    if (raceTimer) return;
    loadMazeView();
    raceTimer = setInterval(function () {
      // 人已经走了就停（别在后台一直请求）
      if (S.route !== 'race') { stopRaceTick(); return; }
      loadMazeView();
    }, 1200);
  }
  function stopRaceTick() {
    if (raceTimer) { clearInterval(raceTimer); raceTimer = null; }
  }

  /* ── 拉取看门狗 ───────────────────────────────────────────────────────────
     为什么需要：拉取可能被别处关掉（另一次 setPull 的响应晚到、上游自己掉、
     人工点了结束忘了开）。而**比赛页没有新帧就等于瞎了**（发车前检查、任务机、
     迷宫全读帧）。所以只要"这页需要帧 + 连着设备 + 拉取是停的"，就自动重开。

     不跟用户抢：#人工**主动**停止是明确意图（排障），所以停止后 8 s 内不自动重开
     （`LIVE.pullOffAt`），超过 8 s 仍停着才认为"是被意外关掉的"。
     这也是"不能有间断"的具体落法 —— 间隙最多就是这么一次重连（~1 s）。 */
  function startPullGuard() {
    if (LIVE.guard) return;
    LIVE.guard = setInterval(function () {
      if (!LIVE.on || !S.health.mouse.connected) return;
      if (!NEED_PULL[S.route]) return;
      if (S.pulling || LIVE.pullBusy) return;
      if (LIVE.pullOffAt && (Date.now() - LIVE.pullOffAt) < 8000) return;
      log('mouse', 'warn', '这页要实时帧，但拉取是停的 ⇒ 自动重开');
      setPull(true, S.pullN);
    }, 2000);
  }

  /* ── 记录 ↔ 视频的关联（**真的写盘**，不是只改本地对象） ───────────────── */
  function linkVideo(id, sid) {
    const rec = S.records.find(function (r) { return r.id === id; });
    if (!rec || !LIVE.on) return Promise.resolve(false);
    // `sid === null` = 解除关联（后端要求**显式**给 null；不带这个键会被判 400）
    return post('/api/v1/records/' + encodeURIComponent(rec.name) + '/video',
      { video_sid: sid == null ? null : String(sid) })
      .then(function () {
        log('control', 'info', (sid ? '关联 ' : '解除关联 ') + rec.id
          + (sid ? (' ← ' + sid) : ''));
        return true;
      })
      .catch(function (e) {
        const msg = (e && (e.detail || e.message)) || String(e);
        log('control', 'warn', '关联没写进去：' + msg);
        toast('bad', '关联失败', String(msg).slice(0, 120));
        return false;
      });
  }

  /* ── 删除一条比赛记录（不可逆，二次确认在前端做） ─────────────────────── */
  function deleteRecord(id) {
    const rec = S.records.find(function (r) { return r.id === id; });
    if (!rec) return Promise.resolve(false);
    if (!LIVE.on) return Promise.resolve(false);
    return del('/api/v1/records/' + encodeURIComponent(rec.name)).then(function () {
      log('control', 'warn', '记录已删除 · ' + rec.id);
      /* 先**就地摘掉**这一条，再重拉列表。
         只重拉的话，只要这次请求失败（或慢），界面上那条记录会一直留着，
         用户就会再点一次删除 —— 而后端已经 404 了，于是"按了没反应"。 */
      const i = S.records.findIndex(function (r) { return r.id === id; });
      if (i >= 0) S.records.splice(i, 1);
      if (S.recSel === id) S.recSel = (S.records[0] ? S.records[0].id : '');
      emit('state');
      return loadRecords();
    }).catch(function (e) {
      const msg = (e && (e.detail || e.message)) || String(e);
      log('control', 'warn', '删不掉 ' + rec.id + '：' + msg);
      toast('bad', '删不掉', String(msg).slice(0, 120));
      return false;
    });
  }

  /* ── 日志 ─────────────────────────────────────────────────────────────── */
  const SRC = { mouse: '电脑鼠', video: '视频', control: '本层' };
  function log(src, level, text) {
    S.log.push({ t: Date.now(), src: src, level: level || 'info', text: text });
    if (S.log.length > 600) S.log.splice(0, 200);
    emit('log');
  }

  /* ── Toast ───────────────────────────────────────────────────────────── */
  function toast(kind, title, body, ms) {
    const box = document.getElementById('toasts');
    if (!box) return;
    const d = document.createElement('div');
    d.className = 'toast ' + (kind || '');
    const ic = kind === 'ok' ? 'check' : (kind === 'warn' || kind === 'bad') ? 'alert' : 'bolt';
    d.innerHTML = icon(ic, 'ti') +
      '<div class="t-body"><b>' + util.esc(title) + '</b>' +
      (body ? '\n' + util.esc(body) : '') + '</div>';
    box.appendChild(d);
    setTimeout(function () {
      d.classList.add('leaving');
      setTimeout(function () { d.remove(); }, 200);
    }, ms || (kind === 'bad' ? 7000 : 4500));
  }

  /* ── 会话 ─────────────────────────────────────────────────────────────── */
  function sessionStart() {
    if (S.session.active) return;
    if (!S.health.mouse.connected) {
      toast('warn', '开不了记录', '只有电脑鼠这一路拦记录：先到顶栏连上串口。');
      return;
    }
    if (LIVE.on) {                      // 真机：真开会话（失败会说话，不假装开着）
      post('/api/v1/session/open', {}).then(function (d) {
        S.session.active = true;
        S.session.t0 = Date.now();
        S.session.frames = 0;
        S.session.id = (d && d.session_id) || '';
        S.session.warnings = (d && d.warnings) || [];
        log('control', 'info', '会话已开 · ' + S.session.id);
        toast('ok', '会话已开', S.session.id + ' · 遥测入库');
        emit('state');
      }).catch(function (e) {
        const msg = (e && (e.detail || e.message)) || String(e);
        log('control', 'warn', '开会话失败：' + msg);
        toast('bad', '开会话失败', String(msg).slice(0, 120));
      });
      return;
    }
    S.session.active = true;
    S.session.t0 = Date.now();
    S.session.frames = 0;
    S.session.id = 'S-' + new Date().toISOString().slice(0, 10).replace(/-/g, '') + '-03';
    S.session.ledger = 'pending';
    log('control', 'info', '会话已开 · ' + S.session.id);
    toast('ok', '会话已开', S.session.id + ' · 遥测入库；视频解析可随时另起');
    emit('state');
  }
  function sessionStop() {
    if (!S.session.active) return;
    if (LIVE.on) {                      // 真机：真收会话（账本结论由后端给）
      post('/api/v1/session/close', {}).then(function (d) {
        S.session.active = false;
        S.session.ledger = (d && d.ledger) || 'unknown';
        const gaps = (d && d.gap !== undefined) ? d.gap : null;
        log('control', 'info', '会话已收 · ' + S.session.id
          + (gaps === null ? '' : ' · 帧缺口 ' + gaps));
        toast('ok', '会话已收', S.session.id + (gaps === null ? '' : ' · 帧缺口 ' + gaps));
        emit('state');
      }).catch(function (e) {
        const msg = (e && (e.detail || e.message)) || String(e);
        log('control', 'warn', '收会话失败：' + msg);
        toast('bad', '收会话失败', String(msg).slice(0, 120));
      });
      return;
    }
    S.session.active = false;
    S.session.ledger = 'pass';
    log('control', 'info', '会话已收 · ' + S.session.id + ' · 账本通过');
    toast('ok', '会话已收', S.session.id + ' · 账本通过，帧缺口 0');
    emit('state');
  }

  /* ── 动作 ─────────────────────────────────────────────────────────────── */
  function act(name, arg) {
    // 真机模式：走后端（下面那套 switch 是原型的本地状态机，只在连不上时用）
    if (LIVE.on) { actLive(name, arg); return; }
    switch (name) {
      case 'estop':
        S.estop = true; S.running = false;
        S.tel.mazeTask = 0; S.tel.speedL = 0; S.tel.speedR = 0;
        log('mouse', 'warn', '急停已下发 · 锁存中，需【解锁】才能再发车');
        toast('bad', '急停已下发', '电机断电并锁存。');
        break;
      case 'unlock':
        S.estop = false;
        log('mouse', 'info', '急停已解锁');
        toast('ok', '急停已解锁', '现在可以发车。');
        break;
      case 'launch':
        if (S.estop) { toast('warn', '发车被拦', '急停锁存中 ⇒ 先解锁急停。'); return; }
        if (!S.health.mouse.connected) { toast('warn', '发车被拦', '未连接电脑鼠 ⇒ 先到顶栏连串口。'); return; }
        S.running = true; maze.idx = 0; maze.frac = 0;
        S.tel.mazeTask = 1; S.tel.motionTask = 2;
        S.tel.trackL = 0; S.tel.trackR = 0; S.tel.goaheadnum = 0;
        log('mouse', 'info', '→ MAZE RUN RACE · 已发车，按 Flash 地图选档');
        toast('ok', '已发车（RACE）', '记录器自动开录 · 目标锁定中心 2×2');
        if (!S.session.active) sessionStart();
        break;
      case 'mazeStop':
        if (!S.running) { toast('warn', '没有在跑', '任务机已是待机。'); return; }
        S.running = false; S.tel.mazeTask = 0; S.tel.speedL = 0; S.tel.speedR = 0;
        log('mouse', 'info', '→ MAZE STOP · 已收车');
        toast('ok', '已收车', '任务机回 WAIT。');
        break;
      case 'rescue':
        S.running = false; S.tel.mazeTask = 0; S.estop = false;
        log('mouse', 'warn', '→ MCU RESET · 整机软复位，约 3 s 后首帧');
        toast('warn', '已复位', '整机软复位。物理位置不变，发车前请放回起点角。');
        break;
      case 'tare':
        S.tel.trackL = 0; S.tel.trackR = 0; S.tel.gyroAngle = 0; S.tel.encAngle = 0;
        log('mouse', 'info', '→ TARE · 陀螺角与里程基准清零');
        toast('ok', '已清零', '陀螺角累加器与里程基准归零。');
        break;
      case 'raw':
        log('mouse', 'info', '→ ' + arg);
        toast('', arg, '已下发，等待设备应答');
        break;
      case 'saveRecord':
        log('mouse', 'info', '迷宫记录已保存 · race_1021_2118.json · 1864 帧');
        toast('ok', '记录已保存', 'race_1021_2118.json · 1864 帧');
        break;
      default:
        break;
    }
    emit('state');
  }

  /* ── 真机动作（改版二十八 · P4）──────────────────────────────────────
     只在 `LIVE.on` 时走这里。**失败一律说出来**（`bad` toast + 日志），
     绝不先改本地状态再假装成功 —— 那样界面会显示"已发车"而车没动。 */
  function actLive(name, arg) {
    const fail = function (e) {
      const msg = (e && (e.detail || e.message)) || String(e);
      log('mouse', 'warn', '动作没下发（' + name + '）：' + msg);
      toast('bad', '没下发：' + name, String(msg).slice(0, 120));
    };
    switch (name) {
      case 'estop':
        post('/api/v1/mouse/estop').then(function () {
          S.estop = true; S.running = false;
          log('mouse', 'warn', '急停已下发 · 锁存中，需【解锁】才能再发车');
          toast('bad', '急停已下发', '电机断电并锁存。');
          emit('state');
        }).catch(fail);
        return;
      case 'unlock':
        // 解锁发的是 STOP —— 上游语义：`STOP` 会解锁急停锁存
        post('/api/v1/mouse/stop').then(function () {
          S.estop = false;
          log('mouse', 'info', '急停已解锁（STOP）');
          toast('ok', '急停已解锁', '现在可以发车。');
          emit('state');
        }).catch(fail);
        return;
      case 'launch':
        post('/api/v1/timesync/maze_run', { target: 'RACE' }).then(function (d) {
          S.running = true; S.estop = false;
          log('mouse', 'info', '→ MAZE RUN RACE · 已发车'
            + (d && d.record && d.record.name ? ' · 记录 ' + d.record.name : ''));
          toast('ok', '已发车（RACE）', '记录器自动开录 · 目标锁定中心 2×2');
          emit('state');
        }).catch(fail);
        return;
      case 'mazeStop':
        post('/api/v1/mouse/maze/stop').then(function (d) {
          S.running = false;
          log('mouse', 'info', '→ MAZE STOP · 已收车'
            + (d && d.reliable === false ? '（⚠ 运动中来不及，如实报告）' : ''));
          toast('ok', '已收车', '任务机回 WAIT。');
          emit('state');
        }).catch(fail);
        return;
      case 'rescue':
        post('/api/v1/mouse/rescue').then(function () {
          S.running = false; S.estop = false;
          log('mouse', 'warn', '→ MCU RESET · 整机软复位，约 3 s 后首帧');
          toast('warn', '已复位', '物理位置不变，发车前请放回起点角。');
          emit('state');
        }).catch(fail);
        return;
      case 'tare':
        post('/api/v1/mouse/tare').then(function () {
          log('mouse', 'info', '→ TARE · 陀螺角与里程基准清零');
          toast('ok', '已清零', '');
        }).catch(fail);
        return;
      case 'raw':
        post('/api/v1/mouse/raw', { line: String(arg || '') }).then(function () {
          log('mouse', 'info', '→ ' + arg);
          toast('', arg, '已下发，等待设备应答');
        }).catch(fail);
        return;
      case 'saveRecord':
        post('/api/v1/mouse/maze/record/stop').then(function (d) {
          log('mouse', 'info', '记录已保存 · ' + ((d && d.name) || ''));
          toast('ok', '记录已保存', ((d && d.name) || ''));
        }).catch(fail);
        return;
      default:
        log('control', 'warn', '这个动作还没有后端通路：' + name);
        return;
    }
  }

  /* ── 4 灯三态 ─────────────────────────────────────────────────────────── */
  /* 判据与后端 leds.py 一致，顺序不能倒：
     ① 固件会不会填这 4 位（fw < 311 ⇒ 全未知，不猜 0）
     ② 这条流有没有这 4 个通道（没有 ⇒ 未知）
     只有两条都过，0 才算“灭”。 */
  function recomputeLeds() {
    const m = S.health.mouse;
    const keys = ['led1', 'led2', 'led3', 'led4'];
    const out = {};
    const supported = m.connected && m.caps && m.caps.led &&
      m.fwInt >= 311 && S.ledSupported;
    keys.forEach(k => { out[k] = supported ? !!S.leds[k] : null; });
    S.leds = out;
    return out;
  }

  /* ── 拉取档：N = 一次往返取几帧 ─────────────────────────────────────────
     ⚠️ **N 就是拉取档，不是"批内帧数"的修饰符**（上游 `TelemetryPuller` 的 `kind`
        由它决定）：`1` = 停等（TELEM，一帧一答），`≥2` = 批式（RATE PULL n t）。
        所以"n 设回 1"= 回停等，不是"批式但每批 1 帧"。
     ⚠️ 区间 1~16 与后端 / 上游同源（固件 RATE PULL 上限）。原来这里夹 1..32 ——
        多出来的那一半**设备一律拒**，而界面会显示"已设成 24"。 */
  function pullBatch(n, quiet) {
    const v = util.clamp(Number(n) | 0, PULL_N_MIN, PULL_N_MAX);
    if (v === S.pullN) { S.tel.batchN = v; return Promise.resolve({ ok: true, n: v, effective: true, msg: '' }); }
    S.pullN = v;
    S.tel.batchN = v;
    log('mouse', 'info', '→ RATE PULL n=' + v + ' t=' + PULL_T_MS + ' ms');
    return api('/api/v1/mouse/stream/batch', { n: v, t_ms: PULL_T_MS })
      .then(function (res) {
        const b = res.body || {};
        const d = b.data || {};
        if (res.ok && b.ok) {
          /* 后端回了**实际**的 n（可能被它夹过）⇒ 以它为准，别拿本地的想当然 */
          S.pullN = (d.n === 0 || d.n) ? Number(d.n) : v;
          S.tel.batchN = S.pullN;
          /* ⚠️ `effective=false` = **档已记下、设备侧还没变**（流没开）——
             这不是失败，但也不是"生效了"。两件事要分开说。 */
          const eff = d.effective !== false;
          log('mouse', 'info', '拉取档已生效 · n=' + S.pullN
            + (d.fps != null ? ' · 目标 ' + util.fmt(d.fps, 1) + ' Hz' : '')
            + (d.mode ? ' · 模式 ' + d.mode : '')
            + (eff ? '' : ' · ⚠ 流没开：档位已记下，设备侧还没变'));
          if (!quiet) {
            if (!eff) {
              toast('warn', '档位已记下 · n=' + S.pullN,
                    '流没开 ⇒ 设备侧还没变。开流后才按这个 n 发帧。');
            } else {
              toast('ok', 'n=' + S.pullN, '一次往返取 ' + S.pullN + ' 帧'
                + (d.fps != null ? '，目标 ' + util.fmt(d.fps, 1) + ' Hz' : ''));
            }
          }
          emit('health');
          return { ok: true, n: S.pullN, effective: eff, msg: '' };
        }
        const msg = (b.error && (b.error.message || b.error.type))
          || ('HTTP ' + res.status);
        log('mouse', 'warn', '拉取档没下发：' + msg);
        if (!quiet) toast('bad', '没生效 · n=' + v, String(msg).slice(0, 120));
        emit('health');
        return { ok: false, n: v, effective: false, msg: String(msg) };
      })
      .catch(function (e) {
        const msg = (e && e.message) ? e.message : String(e);
        log('mouse', 'warn', '拉取档没下发（后端未连）：' + msg);
        if (!quiet) toast('warn', '没连上后端',
              'n 只改在界面上了；后端起来后再点一次【开始拉取】。');
        emit('health');
        return { ok: false, n: v, effective: false, msg: msg };
      });
  }

  /* ── 拉取开关（人工刹车）────────────────────────────────────────────────
     **顺序不能反**：开拉取 = 先落 N（`/stream/batch`）再开流（`/stream`）——
     上游 `stream.on()` 每次都 `configure(1, t_ms)`，档没在位就开流 ⇒
     设备按 n=1 发、界面写着 n=3，现场看不出来。
     结束拉取 = 关流。上游 v2.1 的 `off()` **立刻丢弃在途请求**，是排空蓝牙
     透传缓冲的唯一手段 ⇒ 它是**排障手段**，不是省流量开关。
     三态纪律：没生效就说没生效，不许让界面继续显示"已生效"。 */
  function setPull(on, n, opts) {
    // `auto=true` = 页面切走顺带停。后端在"车在跑"时会**扣留**（返回 kept=true），
    // 那时必须继续显示"拉取中"，否则界面说结束、设备还在发帧。
    const auto = !!(opts && opts.auto);
    // 防重入：看门狗每 2 s 看一眼，若上一次开流还没回来就再发一条，
    // 会在链路上堆命令（停等是串行的，堆两条就多两次往返）。
    LIVE.pullBusy = true;
    const done = function (v) { LIVE.pullBusy = false; return v; };
    if (!on) {
      if (!LIVE.on) {
        S.pulling = false;
        log('mouse', 'info', '拉取已结束（原型）· 档 n=' + S.pullN);
        emit('health');
        return Promise.resolve(false);
      }
      return post('/api/v1/mouse/stream', { on: false, auto: auto }).then(function (d) {
        // 后端扣留（车在跑）：**不许**把界面改成"已结束"
        if (d && d.kept) {
          S.pulling = true;
          log('mouse', 'info', '想停拉取但被扣留（'
            + (d.reason === 'racing' ? '迷宫任务机在跑' : String(d.reason)) + '）');
          emit('health');
          return true;
        }
        S.pulling = false;
        if (!auto) LIVE.pullOffAt = Date.now();   // 人工停 ⇒ 看门狗让 8 s
        log('mouse', 'info', '→ 拉取器已关 · 在途已丢弃');
        if (!auto) toast('', '拉取已结束', '档位保留 n=' + S.pullN + '（8 s 内不会被自动重开）');
        emit('health');
        return done(true);
      }).catch(function (e) {
        const msg = (e && e.message) ? e.message : String(e);
        log('mouse', 'warn', '拉取器没关掉：' + msg);
        toast('bad', '没关掉', String(msg).slice(0, 120));
        emit('health');
        return done(false);
      });
    }
    /* 开：N 先落，再开流
       ⚠️ 原型模式（后端没起 ⇒ `LIVE.on=false`）**不打后端**：打过去必然失败，
          只会每次都弹"没连上后端"，而开关还起不来。原型里 N 与开关都只改本地。 */
    if (!LIVE.on) {
      const v = util.clamp(Number(n) | 0, PULL_N_MIN, PULL_N_MAX);
      S.pullN = v;
      S.tel.batchN = v;
      S.pulling = true;
      log('mouse', 'info', '拉取已开始（原型）· n=' + v);
      toast('ok', '拉取已开始', '档 n=' + v + ' · '
        + (v === 1 ? '停等（一帧一答）' : '批式（一次 ' + v + ' 帧）'));
      emit('health');
      return Promise.resolve(true);
    }
    return pullBatch(n, true).then(function (r) {
      if (!r.ok) {
        toast('bad', '拉取没开起来', String(r.msg).slice(0, 120));
        return false;
      }
      if (S.pulling) {   /* 流本来就是开的 ⇒ 这次只换了 N */
        toast('ok', '已切到 n=' + r.n, '一次往返取 ' + r.n + ' 帧');
        emit('health');
        return true;
      }
      return post('/api/v1/mouse/stream', { on: true }).then(function (d) {
        S.pulling = true;
        log('mouse', 'info', '→ 拉取器已开 · n=' + r.n
          + (d && d.hz != null ? ' · 实测 ' + util.fmt(d.hz, 1) + ' Hz' : ''));
        toast('ok', '拉取已开始', '档 n=' + r.n
          + (d && d.hz != null ? ' · 实测 ' + util.fmt(d.hz, 1) + ' Hz' : ''));
        emit('health');
        return done(true);
      }).catch(function (e) {
        const msg = (e && e.message) ? e.message : String(e);
        log('mouse', 'warn', '拉取没开起来：' + msg);
        toast('bad', '拉取没开起来', String(msg).slice(0, 120));
        emit('health');
        return false;
      });
    });
  }

  /* ── 按页开门：**只有要看的页面才拉**（改版三十一）────────────────────────
     拉取是唯一持续占链路的动作（停等 1 条/往返、批式 10 Hz）。而人大部分时间在
     看参数/记录/视频 —— 那些页面一帧都不需要，却一直在吃带宽。

     `NEED_PULL` 里这几页是"没有新帧就根本没法用"的：
       · overview 总览：显示帧率与跑位
       · live     实时状态：整个页面就是遥测
       · race     比赛：发车前检查、迷宫状态机都读帧
       · maint    维护自检：要靠 `mazeTask` 判断"车在跑 ⇒ 禁操作"
     其余（params / data / video / track / replay）纯读盘或纯写参数，**不拉**。

     ⚠️ 切走时发的是 `{on:false, auto:true}`：后端在"车在跑"时会**扣留**，
        避免"人切到记录页看看，结果把这一跑的遥测砍了"。

     ⚠️ **参数页也要拉**（改版三十一实测）：停流时上游 `GET ALL` 的应答凑不齐
        （3 s 超时、只回 38/41 项）⇒ 参数表会空。所以「参数设定」在这张表里。 */
  const NEED_PULL = { overview: 1, live: 1, params: 1, race: 1, maint: 1 };

  /* 返回 Promise：进页时要**先开拉取再取数**（参数页尤其如此，否则读表 409）。 */
  function syncPull(route) {
    if (!LIVE.on) return Promise.resolve(false);
    // 没连设备就没什么可开的 —— 打过去必然 409，只会在日志里刷"拉取没开起来"
    if (!S.health.mouse.connected) return Promise.resolve(false);
    const id = route || S.route;
    const need = !!NEED_PULL[id];
    if (need && !S.pulling) return setPull(true, S.pullN);
    if (!need && S.pulling) return setPull(false, null, { auto: true });
    return Promise.resolve(S.pulling);
  }

  /* ── 视频会话动作 ─────────────────────────────────────────────────────── */
  function videoStart(source) {
    if (S.videoRun.state === 'running') return;
    if (!source) { toast('warn', '先选一个视频', '列表里挑一个，或在下面填服务器绝对路径。'); return; }
    if (LIVE.on) {                      // 真机：起真子进程，并开始轮询状态
      log('video', 'info', '→ POST /video/run · ' + source);
      post('/api/v1/video/run', { source: source }).then(function (d) {
        S.videoRun = { state: 'running', id: (d && d.id) || '', elapsed: 0,
                       error: '', t0: Date.now(), source: source };
        S.health.video.running = true;
        toast('', '解析已起', '子进程在读帧；上游没有进度回调，这里只报已用时间。');
        pollVideo();
        emit('state');
      }).catch(function (e) {
        const msg = (e && (e.detail || e.message)) || String(e);
        S.videoRun.error = msg;
        log('video', 'warn', '解析没起来：' + msg);
        toast('bad', '解析没起来', String(msg).slice(0, 120));
        emit('state');
      });
      return;
    }
    S.videoRun = { state: 'running', id: '', elapsed: 0, error: '', t0: Date.now(), source: source };
    S.health.video.running = true;
    log('video', 'info', '→ aruco_locator.session · ' + source);
    toast('', '解析已起', '子进程在读帧；上游没有进度回调，这里只报已用时间。');
    emit('state');
  }
  function videoCancel() {
    if (S.videoRun.state !== 'running') return;
    if (LIVE.on) {                      // 真机：只 kill 本次起的那个子进程
      post('/api/v1/video/cancel', {}).then(function () {
        log('video', 'warn', '解析已取消 · 产物可能不完整');
        toast('warn', '已取消', '产物目录里可能留下半份 trajectory.csv。');
      }).catch(function (e) {
        const msg = (e && (e.detail || e.message)) || String(e);
        log('video', 'warn', '取消失败：' + msg);
        toast('bad', '取消失败', String(msg).slice(0, 120));
      });
      return;
    }
    S.videoRun.state = 'cancelled';
    S.health.video.running = false;
    log('video', 'warn', '解析已取消 · 产物可能不完整');
    toast('warn', '已取消', '产物目录里可能留下半份 trajectory.csv。');
    emit('state');
  }

  /* 解析进度：**只轮询 `/video/status`（纯内存、零 IO）**。
     上游没有进度回调，所以我们能报的只有"已用时间"和状态机——
     别自己编百分比。 */
  function pollVideo() {
    if (LIVE.vtimer) clearInterval(LIVE.vtimer);
    LIVE.vtimer = setInterval(function () {
      get('/api/v1/video/status').then(function (d) {
        const st = String(d.state || 'idle');
        if (st === 'running') {
          S.videoRun.elapsed = Math.round(Number(d.elapsed_s || 0) * 10) / 10;
          emit('tick', S.tel);
          return;
        }
        clearInterval(LIVE.vtimer); LIVE.vtimer = null;
        S.health.video.running = false;
        if (st === 'done') {
          S.videoRun.state = 'done';
          S.videoRun.id = d.id || '';
          log('video', 'info', '解析完成 · ' + (d.id || '')
            + '（点【刷新列表】看产物）');
          toast('ok', '解析完成', (d.id || '') + ' · 产物已落盘');
        } else if (st === 'cancelled') {
          S.videoRun.state = 'cancelled';
          log('video', 'warn', '解析已取消 · 产物可能不完整');
        } else if (st === 'failed') {
          S.videoRun.state = 'failed';
          S.videoRun.error = d.error || '上游失败（看产物目录里的 run.log）';
          log('video', 'warn', '解析失败：' + S.videoRun.error);
          toast('bad', '解析失败', String(S.videoRun.error).slice(0, 120));
        }
        emit('state');
      }).catch(function () {
        clearInterval(LIVE.vtimer); LIVE.vtimer = null;
      });
    }, 1000);
  }
  function videoTick() {
    const v = S.videoRun;
    if (v.state !== 'running') return;
    v.elapsed = (Date.now() - v.t0) / 1000;
    if (v.elapsed >= 4.2) {
      const s = S.videoSessions[0];
      v.state = 'done';
      v.id = s.id;
      v.elapsed = 4.2;
      S.videoSel = s.id;
      S.health.video.running = false;
      S.readiness[2].ok = true;
      S.readiness[2].state = S.videoSessions.length + ' 个会话 · 最近 ' + s.created.slice(0, 5);
      log('video', 'info', '解析完成 · ' + s.n_total + ' 帧（有效 ' + s.n_valid + '）· ' + s.n_total + ' 帧产物已落盘');
      toast('ok', '解析完成', s.n_total + ' 帧，有效 ' + s.n_valid + ' · 产物在 ' + s.out_dir);
      emit('state');
    }
  }

  /* ── 遥测仿真 ─────────────────────────────────────────────────────────── */
  const baseSpeed = 34;
  const Maze = { turn: 0 };
  function stepMaze() {
    const p = maze.path;
    if (!p.length) return;
    const dirAt = i => dirOf(p[i], p[Math.min(i + 1, p.length - 1)]);
    const rate = (Maze.turn > 0 ? 1.05 : 1.9) * 0.25;
    maze.frac += rate;
    if (maze.frac >= 1) {
      maze.frac = 0;
      maze.idx++;
      if (maze.idx >= p.length - 1) {
        maze.idx = p.length - 1;
        S.running = false;
        S.tel.mazeTask = 6;
        S.tel.motionTask = 0;
        S.tel.speedL = 0; S.tel.speedR = 0;
        log('mouse', 'info', '任务机 END（跑位收尾）· 已到终点区');
        toast('ok', '跑完一趟', '任务机 END ⇒ 可以【收车】清发车状态。');
        emit('state');
      }
      maze.car.dir = dirAt(maze.idx);
      maze.visited[p[maze.idx][0] + ',' + p[maze.idx][1]] = 1;
    }
    const a = p[Math.min(maze.idx, p.length - 1)];
    const b = p[Math.min(maze.idx + 1, p.length - 1)];
    maze.car.x = util.lerp(a[0] + .5, b[0] + .5, maze.frac);
    maze.car.y = util.lerp(a[1] + .5, b[1] + .5, maze.frac);
  }

  let tickN = 0;
  function tick() {
    tickN++;
    /* 真机模式：遥测由 WS 写入，这里**只负责让视图刷新** ——
       绝不能一边收真帧一边伪造，那种混合数据没人分得清哪一半是真的。 */
    if (LIVE.on) { emit('tick', S.tel); return; }
    const t = S.tel;

    /* 拉取停了 ⇒ **没有新帧**：读数全停在最后一帧，只让帧龄往上走。
       别一边说"没有新帧"一边照旧刷新通道 —— 那是在编数据。 */
    if (!S.pulling) {
      t.ageMs = util.clamp(t.ageMs + 8, 60, 1400);
      videoTick();
      emit('tick', t);
      return;
    }

    if (S.running) {
      const before = maze.car.dir;
      stepMaze();
      if (before !== maze.car.dir) Maze.turn = 6;
      if (Maze.turn > 0) Maze.turn--;
      const wobble = Math.sin(tickN / 7) * 2.2 + Math.sin(tickN / 2.3) * .7;
      const sp = S.running ? baseSpeed + wobble + (Maze.turn > 3 ? -12 : 0) : 0;
      t.speedL = Math.max(0, sp + Math.sin(tickN / 3) * 1.1);
      t.speedR = Math.max(0, sp - Math.sin(tickN / 3) * 1.1);
      t.trackL += t.speedL * 10 * 0.25 * 0.6;
      t.trackR += t.speedR * 10 * 0.25 * 0.6;
      t.posX = Math.round(maze.car.x * 100) / 100;
      t.posY = Math.round(maze.car.y * 100) / 100;
      t.dir = maze.car.dir;
      t.gyroAngle = (t.gyroAngle + (maze.car.dir !== before ? (before === 1 ? 90 : -90) : 0)) % 360;
      t.encAngle = (t.encAngle + t.speedL * .12) % 360;
      t.mazeTask = maze.idx < 2 ? 1 : 3;
      t.battMv = Math.max(7600, t.battMv - 0.35);
      const cx = maze.car.x | 0, cy = maze.car.y | 0;
      const ahead = wallAhead(cx, cy, t.dir);
      t.frontNear = ahead === 1 ? 1 : 0;
      t.frontFar = ahead === 2 ? 2 : 0;
      t.frontBoth = t.frontNear | t.frontFar;
      t.wallFront = t.frontBoth;
      t.wallLeft = wallAhead(cx, cy, (t.dir + 3) % 4);
      t.wallRight = wallAhead(cx, cy, (t.dir + 1) % 4);
      t.spurtFlag = S.running ? 1 : 0;
      t.spurtMode = S.running ? 4 : 0;
      t.spurtTestFlag = 0;
      t.goaheadnum = Math.floor(maze.idx / 3);
      t.goOrBack = 0;
    } else {
      t.speedL = 0; t.speedR = 0;
    }
    /* 批式拉取：一次往返取 N 帧 ⇒ 帧率 ≈ N / 单批往返。
       ⚠️ 夹的是 1..16（与 `pullBatch` / 后端 / 上游同源）；原来写 32，
          于是 n=24 时这里照样算出"看着合理"的 Hz，把"设备根本不会按这个 n 发"
          这件事盖住了。 */
    const N = util.clamp(S.pullN | 0, PULL_N_MIN, PULL_N_MAX);
    t.batchN = N;
    t.batchMs = 232 + (N - 1) * 16 + Math.random() * 12;
    t.rate = N * 1000 / t.batchMs;
    t.ageMs = S.running ? 100 + Math.round(Math.random() * 60)
      : util.clamp(t.ageMs + 8, 60, 1400);
    if (tickN % 4 === 0) {
      const h = S.hist;
      h.rate.push(+t.rate.toFixed(2));
      if (h.rate.length > 60) h.rate.shift();
      S.rateLog.push({ seq: ++rateSeq, n: N, ms: Math.round(t.batchMs), hz: +t.rate.toFixed(2) });
      if (S.rateLog.length > 8) S.rateLog.shift();
    }
    t.ms8 = (tickN * 37) % 256;
    t.flags = S.estop ? 129 : (S.running ? 17 : 1);
    t.irF1 = 62 + Math.round(Math.sin(tickN / 9) * 8 + Math.random() * 3);
    t.irF2 = 66 + Math.round(Math.cos(tickN / 8) * 8 + Math.random() * 3);
    t.irL = 32 + Math.round(Math.random() * 6);
    t.irR = 36 + Math.round(Math.random() * 6);
    t.irSwitch = 1;

    /* 4 灯：跑起来后 LED1/2 交替闪 —— 只是演示灯位在动，不做业务判断 */
    if (S.health.mouse.connected && S.ledSupported && S.health.mouse.fwInt >= 311) {
      const on = Math.floor(tickN / 4) % 2 === 0;
      S.leds = { led1: true, led2: on, led3: !on, led4: S.running };
    }

    if (S.session.active) {
      S.session.frames += 1 + Math.round(Math.random() * 2);
    }
    videoTick();
    emit('tick', t);
  }

  function wallAheadIn(m, cx, cy, dir) {
    const N = m.N;
    let blocked = false;
    if (dir === 0) blocked = cy <= 0 || m.h[cy - 1][cx];
    if (dir === 2) blocked = cy >= N - 1 || m.h[cy][cx];
    if (dir === 1) blocked = cx >= N - 1 || m.v[cy][cx];
    if (dir === 3) blocked = cx <= 0 || m.v[cy][cx - 1];
    if (!blocked) return 0;
    return (Math.sin(cx * 7 + cy * 13) > -0.4) ? 1 : 2;
  }
  function wallAhead(cx, cy, dir) { return wallAheadIn(maze, cx, cy, dir); }

  /* ── 场景（原型演示用） ───────────────────────────────────────────────── */
  function setScene(name) {
    S.scene = name;
    const h = S.health;
    if (name !== 'oldfw') {
      h.mouse.fwInt = 313; h.mouse.fw = '2.13.0'; h.mouse.caps.led = true; S.ledSupported = true;
    }
    if (name === 'ready') {
      h.mouse.connected = true; h.mouse.port = 'COM7'; h.mouse.available = true;
      h.clock.calibrated = true;
      S.estop = false; S.running = false;
      S.tel.mazeTask = 0; S.tel.battMv = 8412;
      maze.idx = 0; maze.frac = 0;
      S.videoSessions = makeSessions();
      S.videoRun = { state: 'done', id: S.videoSessions[0].id, elapsed: 0, error: '', t0: 0 };
      S.readiness[0].ok = true; S.readiness[0].state = 'COM7 · fw 2.13.0 · 4.6 Hz';
      S.readiness[1].ok = false; S.readiness[1].state = '未发车 · 对齐按 assumed';
      S.readiness[2].ok = true; S.readiness[2].state = '3 个会话 · 最近 21:04';
      recomputeLeds();
      log('control', 'info', '情景：就绪 · 待发车');
    } else if (name === 'run') {
      act('launch');
    } else if (name === 'estop') {
      act('estop');
    } else if (name === 'down') {
      h.mouse.connected = false; h.mouse.port = '';
      h.clock.calibrated = false;
      S.running = false; S.estop = false;
      S.tel.mazeTask = 0; S.tel.speedL = 0; S.tel.speedR = 0;
      h.video.root = 'D:\\video';
      S.readiness[0].ok = false; S.readiness[0].state = '未连接 · 串口为空';
      S.readiness[1].ok = false; S.readiness[1].state = '未标定';
      S.readiness[2].ok = false; S.readiness[2].state = '素材目录里没有视频';
      S.videoSessions = [];
      S.videoRun = { state: 'idle', id: '', elapsed: 0, error: '', t0: 0 };
      recomputeLeds();
      log('mouse', 'warn', '串口 COM7 已断开 · 读数停在最后一帧');
      log('video', 'warn', '视频目录 D:\\video 里没有视频 · 放一个 mp4 再刷新');
    } else if (name === 'oldfw') {
      h.mouse.connected = true; h.mouse.port = 'COM7';
      h.mouse.fw = '310'; h.mouse.fwInt = 310; h.mouse.caps.led = false;
      S.ledSupported = false;
      S.estop = false; S.running = false;
      recomputeLeds();
      S.readiness[0].ok = true; S.readiness[0].state = 'COM7 · fw 310 · 4.6 Hz';
      log('mouse', 'warn', '固件 310：4 个 LED 位是预留位（恒 0）⇒ 界面显示未知，不显示“灭”');
    } else if (name === 'novideo') {
      S.videoSessions = [];
      S.videoRun = { state: 'idle', id: '', elapsed: 0, error: '', t0: 0 };
      S.readiness[2].ok = false; S.readiness[2].state = '还没有解析过视频';
      log('video', 'info', '视频目录为空 · 放一个 mp4 再刷新');
    }
    emit('state');
  }

  /* ── 参数表：41 项 / 四组（上游 2.5.0 起的分组） ──────────────────────── */
  function buildParams() {
    const mk = (id, name, value, unit, desc, o) => Object.assign({
      id, name, value, unit: unit || '', desc, derived: false, dec: 0,
      q16: Math.round(value * 65536),
    }, o || {});
    const G = [
      { key: 'base', name: '生产标定', range: '01–07', page: 115, via: 'SET + SAVE',
        note: '任何固件都有 · 落 Flash page 115', items: [
          mk(1, 'TRACKL', 91.2, 'pulse/mm', '左轮里程系数；走一格判据的直接因子', { dec: 4 }),
          mk(2, 'TRACKR', 90.8, 'pulse/mm', '右轮里程系数', { dec: 4 }),
          mk(3, 'SPEEDL', 0.912, '(pulse/ms)/(cm/s)', '左轮速度换算；恒 = TRACKL/100 · 上位机派生', { derived: true, dec: 6, q16: 0 }),
          mk(4, 'SPEEDR', 0.908, '(pulse/ms)/(cm/s)', '右轮速度换算；恒 = TRACKR/100 · 上位机派生', { derived: true, dec: 6, q16: 0 }),
          mk(5, 'GYRORATIO', 14, 'LSB/度', '陀螺角速率积分系数'),
          mk(6, 'ENCANGLE', 26, 'pulse/度', '左右轮脉冲差 → 角度；固件为 u16，取整'),
          mk(7, 'WALLDIST', 96, 'mm', '车中心到墙距离；固件为 u8，取整'),
        ] },
      { key: 'ir', name: '红外测量', range: '22–32', page: 115, via: 'SETQ + SAVE',
        note: '阈值与滤波 · 默认值 = 原编译期常量', items: [
          mk(22, 'IRTHRWALL', 160, 'mm', '侧墙判定阈值；唯一真源 = wallSns.wallThresh'),
          mk(23, 'IRTHR45', 150, 'mm', '45° 侧墙阈值（斜线矫正用）'),
          mk(24, 'IRTHRCRIT', 60, 'mm', '临界墙阈值；低于此值禁动'),
          mk(25, 'IRTHRFLO', 230, 'mm', '前墙远距-低（双传感器确认有墙）'),
          mk(26, 'IRTHRFMID', 240, 'mm', '前墙远距-中'),
          mk(27, 'IRTHRFHI', 245, 'mm', '前墙远距-高'),
          mk(28, 'IRTHRFNEAR', 80, 'mm', '前墙近距（单传感器即判定有墙）'),
          mk(29, 'IRKALQ', 100, '1e-4', '卡尔曼过程噪声 ×10000（越小越信任预测）'),
          mk(30, 'IRKALP', 2000, '1e-4', '卡尔曼测量噪声 ×10000（越大越平滑但迟钝）'),
          mk(31, 'IRKALSW', 1, '0|1', '卡尔曼滤波开关', { enumText: '1 = 启用' }),
          mk(32, 'IRFRONTMODE', 0, '0–2', '前向综合距离：0 均值 / 1 取小 / 2 加权', { enumText: '0 = 均值' }),
        ] },
      { key: 'batt', name: '电源监控', range: '33–38', page: 115, via: 'SETQ + SAVE',
        note: '分压比与电量档', items: [
          mk(33, 'BATTKV', 2417, 'µV/LSB', '分压系数：mV = adcBatt × BATTKV / 1000'),
          mk(34, 'BATTLOWMV', 7000, 'mV', '电量档下限（低于此值报低电）'),
          mk(35, 'BATTHIGHMV', 7960, 'mV', '满电参考；step = (HIGH-LOW)/MAXPCT'),
          mk(36, 'BATTALPHA', 100, '1e-3', '电压一阶 IIR 系数 ×1000'),
          mk(37, 'BATTMAXPCT', 8, '1–8', '电量档上限'),
          mk(38, 'BATTMINPCT', 1, '0–8', '电量档下限'),
        ] },
      { key: 'motion', name: '运动控制', range: '39–55', page: 115, via: 'SETQ + SAVE',
        note: '⚠ 仅远程档生效；比赛档不读这些 id', items: [
          mk(39, 'TRACKCOORD', 145, '格', '纵向矫正灵敏度（有墙→无墙时重置格内里程）'),
          mk(40, 'TURNDELAY', 16.5, '格', '转弯延迟格数（浮点）；转弯后回落点固定 70', { dec: 1 }),
          mk(41, 'SPDSEARCH', 95, 'cm/s', '搜索直线速度初值；运动中按工况重算'),
          mk(42, 'SPDTURN', 60, 'cm/s', '转弯速度；冲刺路径会覆写为 80'),
          mk(43, 'DISF60', 60, 'mm', '180° 前对正判据阈值'),
          mk(44, 'GSKP', 0.4, '', '盲走直行 P 增益', { dec: 3 }),
          mk(45, 'TURNP', 5.0, '', '转弯 P 增益；冲刺会覆写且不恢复', { dec: 3 }),
          mk(46, 'TURND', 4.5, '', '转弯 D 增益', { dec: 3 }),
          mk(47, 'PTKP', 2.5, '', '原地转 P 增益（POINTTURN90 / TURNAROUND）', { dec: 3 }),
          mk(48, 'PTKI', 0.01, '', '原地转 I 增益（仅 POINTTURN90）', { dec: 3 }),
          mk(49, 'PTKD', 0.1, '', '原地转 D 增益', { dec: 3 }),
          mk(50, 'MKPL', 10.75, '', '左轮电机 P（freemode=0 每周期生效）', { dec: 3 }),
          mk(51, 'MKIL', 0.55, '', '左轮电机 I', { dec: 3 }),
          mk(52, 'MKDL', 0.55, '', '左轮电机 D', { dec: 3 }),
          mk(53, 'MKPR', 10.2, '', '右轮电机 P', { dec: 3 }),
          mk(54, 'MKIR', 0.5, '', '右轮电机 I', { dec: 3 }),
          mk(55, 'MKDR', 0.5, '', '右轮电机 D', { dec: 3 }),
        ] },
    ];
    S.params = G;
  }
  buildParams();
  /* 记录要用参数表做快照，所以放在 buildParams 之后 */
  S.records = makeRecords();
  S.recSel = S.records[0].id;

  /* 预填几批：一进来帧率表就有读数 */
  for (let i = 0; i < 5; i++) {
    const ms = 232 + Math.round(Math.random() * 12);
    S.rateLog.push({ seq: ++rateSeq, n: S.pullN, ms: ms, hz: +(S.pullN * 1000 / ms).toFixed(2) });
  }
  /* 统一帧通道数 = 33（上游 2.13.0：29 + `led1..led4`，改版二十六修正）。
     ⚠️ 原来写 29 —— 那是 2.12.0 的口径。4 个灯单列在状态行（`S.leds`）**不代表**
        它们不在帧里；写 29 会与后端 `/health` 的 `mouse.unified_channels` 打架。
     ⚠️ 通道卡按 `GROUPS` 渲染 31 行 = 29 个真通道 + `frontNear`/`irSwitch` 两个
        上游派生键。三个数字各有含义，别互相替代。 */
  S.caps = { unified_channels: 33, led: true };

  /* ── 路由 ─────────────────────────────────────────────────────────────── */
  const NAV = [
    { label: '', items: [{ id: 'overview', name: '总览', icon: 'grid' }] },
    { label: '小车控制', items: [
      { id: 'live', name: '实时状态', icon: 'pulse' },
      { id: 'params', name: '参数设定', icon: 'sliders' },
      { id: 'race', name: '比赛', icon: 'flag' },
      { id: 'maint', name: '维护自检', icon: 'wrench' },
    ] },
    { label: '视频复盘', items: [
      { id: 'video', name: '视频会话', icon: 'film' },
      { id: 'track', name: '轨迹', icon: 'route' },
    ] },
    { label: '记录', items: [
      { id: 'data', name: '比赛记录', icon: 'database' },
      { id: 'replay', name: '回放', icon: 'play' },
    ] },
  ];
  const VIEWS = {};

  function go(id, silent) {
    if (!VIEWS[id]) id = 'overview';
    S.route = id;
    if (!silent) {
      const h = '#/' + id;
      if (location.hash !== h) history.pushState({ id }, '', h);
    }
    emit('route', id);
    // 先定"这页要不要拉取"，再按页取数：参数页读表**依赖拉取在跑**（见 NEED_PULL），
    // 反过来做会先吃一个 409、再补一次请求。
    syncPull(id).then(function () { needPage(id); });
    // 比赛页要"实时图 + 持续帧"：进页面就起重构轮询与拉取看门狗，离开就停
    if (id === 'race') startRaceTick(); else stopRaceTick();
  }

  let timer = null;
  function boot() {
    if (timer) return;
    timer = setInterval(tick, 250);
  }

  /* 初始日志 */
  [
    ['control', 'info', '服务已起 · 127.0.0.1:8770 · 协议 v9（30 B 统一帧 / 33 通道）'],
    ['mouse', 'info', '串口 COM7 已连接 · fw 2.13.0 · proto 9'],
    ['video', 'info', '视频目录 D:\\video · 3 个 mp4 · 最近解析 21:04'],
    ['control', 'warn', '时间基：设备侧未发车（ARM 未置位），跨机对齐按 assumed 记录'],
  ].forEach(x => S.log.push({ t: Date.now() - 60000 + Math.random() * 50000, src: x[0], level: x[1], text: x[2] }));

  recomputeLeds();

  return {
    S, util, icon, on, emit, log, toast, go, boot, act,
    sessionStart, sessionStop, setScene,
    videoStart, videoCancel, videoTick,
    recomputeLeds, pullBatch,
    setPull, syncPull,
    loadMazeView, clearMazeView, startRaceTick, stopRaceTick, deleteRecord, linkVideo,
    // 真后端接入：`startLive()` 由 shell 的 `boot()` 起；`LIVE` 只读（判据与排障要看它）
    startLive, LIVE,
    // 四页数据（按页拉取 + 形状翻译）
    loadParams, loadRecords, loadVideoSessions, loadTrajectory, loadReplay, needPage,
    replayViewLive,
    // 设备连接（真的要动硬件的按钮走这里）
    connectMouse, disconnectMouse, listPorts,
    channels: { groups: GROUPS, text: valText, dirs: DIRS, tasks: TASKS, motions: MOTIONS, spurt: SPURT, front: FRONT },
    records: S.records,
    record(id) { return S.records.find(r => r.id === id) || S.records[0] || null; },
    recordView: recordView,
    maze, Maze,
    NAV, VIEWS,
    fmt: util.fmt,
  };
})();
