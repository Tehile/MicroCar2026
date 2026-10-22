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
    rateLog: [],
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
      link: 'uart 115200', detail: '', caps: { unified_channels: 29, led: true },
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
      const bits = [];
      if (v & 1) bits.push('自由');
      if (v & 16) bits.push('IR');
      if (v & 128) bits.push('急停');
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
    S.session.active = false;
    S.session.ledger = 'pass';
    log('control', 'info', '会话已收 · ' + S.session.id + ' · 账本通过');
    toast('ok', '会话已收', S.session.id + ' · 账本通过，帧缺口 0');
    emit('state');
  }

  /* ── 动作 ─────────────────────────────────────────────────────────────── */
  function act(name, arg) {
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

  /* ── 批式拉取：N = 一次往返取几帧 ─────────────────────────────────────── */
  function pullBatch(n) {
    const v = util.clamp(Number(n) | 0, 1, 32);
    if (v === S.pullN) return S.pullN;
    S.pullN = v;
    log('mouse', 'info', '→ TELEM BATCH n=' + v);
    toast('', '批式 n=' + v, '目标 ' + util.fmt(v * 1000 / (232 + (v - 1) * 16), 1) + ' Hz');
    emit('state');
    return v;
  }

  /* ── 视频会话动作 ─────────────────────────────────────────────────────── */
  function videoStart(source) {
    if (S.videoRun.state === 'running') return;
    if (!source) { toast('warn', '先选一个视频', '列表里挑一个，或在下面填服务器绝对路径。'); return; }
    S.videoRun = { state: 'running', id: '', elapsed: 0, error: '', t0: Date.now(), source: source };
    S.health.video.running = true;
    log('video', 'info', '→ aruco_locator.session · ' + source);
    toast('', '解析已起', '子进程在读帧；上游没有进度回调，这里只报已用时间。');
    emit('state');
  }
  function videoCancel() {
    if (S.videoRun.state !== 'running') return;
    S.videoRun.state = 'cancelled';
    S.health.video.running = false;
    log('video', 'warn', '解析已取消 · 产物可能不完整');
    toast('warn', '已取消', '产物目录里可能留下半份 trajectory.csv。');
    emit('state');
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
    const t = S.tel;

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
      t.ageMs = util.clamp(t.ageMs + 8, 60, 1400);
    }
    /* 批式拉取：一次往返取 N 帧 ⇒ 帧率 ≈ N / 单批往返 */
    const N = util.clamp(S.pullN | 0, 1, 32);
    t.batchN = N;
    t.batchMs = 232 + (N - 1) * 16 + Math.random() * 12;
    t.rate = N * 1000 / t.batchMs;
    t.ageMs = S.running ? 100 + Math.round(Math.random() * 60) : t.ageMs;
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

    if (tickN % 4 === 0) {
      const h = S.hist;
      h.rate.push(+t.rate.toFixed(2));
      if (h.rate.length > 60) h.rate.shift();
      S.rateLog.push({ seq: ++rateSeq, n: N, ms: Math.round(t.batchMs), hz: +t.rate.toFixed(2) });
      if (S.rateLog.length > 8) S.rateLog.shift();
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
  S.caps = { unified_channels: 29, led: true };

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
  }

  let timer = null;
  function boot() {
    if (timer) return;
    timer = setInterval(tick, 250);
  }

  /* 初始日志 */
  [
    ['control', 'info', '服务已起 · 127.0.0.1:8770 · 协议 v9（30 B 统一帧 / 29 通道）'],
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
    channels: { groups: GROUPS, text: valText, dirs: DIRS, tasks: TASKS, motions: MOTIONS, spurt: SPURT, front: FRONT },
    records: S.records,
    record(id) { return S.records.find(r => r.id === id) || S.records[0] || null; },
    recordView: recordView,
    maze, Maze,
    NAV, VIEWS,
    fmt: util.fmt,
  };
})();
