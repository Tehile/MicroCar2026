/* ═══════════════════════════════════════════════════════════════════════════
   draw.js —— 扁平示意图：迷宫 / 帧率 / 视频轨迹
   规则：纯白底、1px 网格、实心色块、无阴影无渐变
   ═══════════════════════════════════════════════════════════════════════════ */
window.C = window.C || {};
C.draw = (function () {
  const U = C.util;
  const INK = '#121316', MUTED = '#787C85';
  const BLUE = '#2B4FF2', GREEN = '#00805A', RED = '#D62B1F';

  function fit(cv, aspect) {
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth || 560;
    const h = aspect ? w / aspect : w;
    cv.width = Math.max(1, Math.round(w * dpr));
    cv.height = Math.max(1, Math.round(h * dpr));
    cv.style.height = Math.round(h) + 'px';
    const g = cv.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    return { g: g, w: w, h: h };
  }

  const DIR_V = [[0, -1], [1, 0], [0, 1], [-1, 0]];   // 0北 1东 2南 3西

  /* ── 迷宫：黑墙 + 蓝终点 + 已走轨迹 ─────────────────────────────────────
     M 缺省 = 实时那份（C.maze）；回放传 recordView() 的结果。
     车坐标是**格中心**口径（0.5 起），不要再 +.5。
     `cap` = 允许的最大边长（px）。回放页要"地图吃掉剩余高度"，容器给的是
     **可用高度**；不传时按容器宽度走正方形（实时/比赛页就是这样，宽度已被
     栅格约束住）。⚠️ 必须在 fit() 之前写 style.width —— fit 读的是 clientWidth，
     先设宽再量，否则量到旧宽度、地图溢出容器。 */
  function maze(cv, M, cap) {
    const m = M || C.maze, N = m.N;
    if (cap) cv.style.width = Math.max(80, Math.round(cap)) + 'px';
    const { g, w } = fit(cv, 1);
    const pad = 20;
    const s = (w - pad * 2) / N;
    /* 坐标：**左下角是 (0,0)，y 轴向上**（= 固件坐标系）。
       `X/Y` 收的是**格线**坐标（0..N），不是格中心；格 y 的屏幕纵向范围是
       `[Y(y+1), Y(y)]`（上沿 `Y(y+1)`、下沿 `Y(y)`）。
       ⚠️ 这只是**显示翻转**：数据下标（`v/h/visited/trail`）里的 y 就是固件 y，
          不动它 —— 早先就是因为把"显示翻转"当成"数据镜像"，把墙记错了隔一格。 */
    const X = x => pad + x * s;
    const Y = y => pad + (N - y) * s;

    g.fillStyle = '#FFFFFF';
    g.fillRect(0, 0, w, w);

    g.strokeStyle = '#EFEEE8';
    g.lineWidth = 1;
    for (let i = 0; i <= N; i++) {
      g.beginPath(); g.moveTo(Math.round(X(i)) + .5, Y(0)); g.lineTo(Math.round(X(i)) + .5, Y(N)); g.stroke();
      g.beginPath(); g.moveTo(X(0), Math.round(Y(i)) + .5); g.lineTo(X(N), Math.round(Y(i)) + .5); g.stroke();
    }

    /* 终点区：中心 2×2，锁死不从帧读。
       ⚠️ `goal` 可能是**空数组**：7×7 时代的老记录装不下 16×16 的终点区，
       后端会如实给空并在 `note` 里说明。空的时候不画框，不许画一个"看着像终点"的框。 */
    const gm = m.goal || [];
    if (gm.length) {
      const minX = Math.min.apply(null, gm.map(p => p[0])), maxX = Math.max.apply(null, gm.map(p => p[0]));
      const minY = Math.min.apply(null, gm.map(p => p[1])), maxY = Math.max.apply(null, gm.map(p => p[1]));
      const gx = X(minX), gy = Y(maxY + 1), gw = (maxX - minX + 1) * s, gh = (maxY - minY + 1) * s;
      g.fillStyle = 'rgba(43,79,242,.13)';
      g.fillRect(gx, gy, gw, gh);
      g.strokeStyle = BLUE; g.lineWidth = 2;
      g.strokeRect(gx + 1, gy + 1, gw - 2, gh - 2);
      g.fillStyle = INK;
      g.font = '700 ' + Math.max(8, s * .32) + 'px Consolas,monospace';
      g.textAlign = 'center'; g.textBaseline = 'middle';
      g.fillText('GOAL', gx + gw / 2, gy + gh / 2);
    }

    /* 墙：已知实线（黑），未探明点线（浅） */
    function known(cx, cy) { return m.visited[cx + ',' + cy]; }
    function line(x1, y1, x2, y2, on) {
      g.beginPath();
      g.setLineDash(on ? [] : [2, 3.5]);
      g.strokeStyle = on ? INK : '#D5D3C9';
      g.lineWidth = on ? 2 : 1;
      g.moveTo(X(x1), Y(y1)); g.lineTo(X(x2), Y(y2));
      g.stroke();
      g.setLineDash([]);
    }
    for (let y = 0; y < N; y++) {
      for (let x = 0; x < N; x++) {
        if (x < N - 1 && m.v[y][x]) line(x + 1, y, x + 1, y + 1, known(x, y) || known(x + 1, y));
        if (y < N - 1 && m.h[y][x]) line(x, y + 1, x + 1, y + 1, known(x, y) || known(x, y + 1));
      }
    }
    g.strokeStyle = INK; g.lineWidth = 2.4;
    g.beginPath(); g.rect(X(0), Y(N), N * s, N * s); g.stroke();

    /* 全程路径：浅蓝虚线。
       ⚠️ `path` 可能**没有**（实时重构/回放只给"走过的 trail"，不给规划路径）——
       这里必须 `|| []`：真机上就是因为读了 `undefined.length` 整页渲染失败。 */
    const path = m.path || [];
    if (path.length > 1) {
      g.beginPath();
      path.forEach((p, i) => {
        const px = X(p[0] + .5), py = Y(p[1] + .5);
        i ? g.lineTo(px, py) : g.moveTo(px, py);
      });
      g.strokeStyle = 'rgba(43,79,242,.28)';
      g.lineWidth = 2; g.setLineDash([4, 3]);
      g.stroke(); g.setLineDash([]);
    }

    /* 已走：外部给 trail 就用 trail（回放），否则按 idx 现算 */
    let trail = m.trail;
    if (!trail || !trail.length) {
      trail = [];
      const upto = Math.min(path.length - 1, m.idx || 0);
      for (let k = 0; k <= upto; k++) trail.push([path[k][0] + .5, path[k][1] + .5]);
      if (trail.length) trail.push([m.car.x, m.car.y]);
    }
    if (trail.length > 1) {
      g.beginPath();
      trail.forEach((p, i) => {
        const px = X(p[0]), py = Y(p[1]);
        i ? g.lineTo(px, py) : g.moveTo(px, py);
      });
      g.strokeStyle = INK;
      g.lineWidth = Math.max(2, s * .13);
      g.lineJoin = 'round'; g.lineCap = 'round';
      g.stroke();
    }

    /* 起点：绿色空心方块 */
    g.strokeStyle = GREEN; g.lineWidth = 2;
    g.strokeRect(X(.5) + s * .26, Y(.5) + s * .26, s * .48, s * .48);

    /* 车：实心方块 + 白色朝向缺口 */
    const cx = X(m.car.x), cy = Y(m.car.y);
    const v = DIR_V[m.car.dir] || DIR_V[0];
    const half = Math.max(4, s * .30);
    g.fillStyle = BLUE;
    g.fillRect(cx - half, cy - half, half * 2, half * 2);
    g.strokeStyle = '#fff'; g.lineWidth = 2;
    g.beginPath();
    g.moveTo(cx + v[0] * half * .55 - v[1] * half * .5, cy + v[1] * half * .55 - v[0] * half * .5);
    g.lineTo(cx + v[0] * half * .95, cy + v[1] * half * .95);
    g.lineTo(cx + v[0] * half * .55 + v[1] * half * .5, cy + v[1] * half * .55 + v[0] * half * .5);
    g.stroke();

    /* 坐标刻度 */
    g.fillStyle = MUTED;
    g.font = '9px Consolas,monospace';
    g.textAlign = 'center'; g.textBaseline = 'middle';
    for (let i = 0; i < N; i += 3) g.fillText(String(i), X(i + .5), Y(0) + 9);
    g.textAlign = 'right';
    for (let i = 0; i < N; i += 3) g.fillText(String(i), pad - 7, Y(i + .5));
  }

  /* ── 帧率曲线：细网格 + 2px 实线 ──────────────────────────────────────── */
  function spark(cv, series, o) {
    o = o || {};
    const dpr = window.devicePixelRatio || 1;
    const cw = cv.clientWidth || 320, ch = cv.clientHeight || 92;
    cv.width = Math.max(1, Math.round(cw * dpr));
    cv.height = Math.max(1, Math.round(ch * dpr));
    cv.style.height = ch + 'px';
    const g = cv.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, cw, ch);

    const padT = 8, padB = 8;
    const all = series.reduce((a, s) => a.concat(s.data), [0]);
    const rawPeak = Math.max(0, ...all);
    const peak = Math.max(.2, rawPeak);
    const maxV = Math.max(1, peak * 1.25);
    const n = Math.max(...series.map(s => s.data.length), 2);

    g.strokeStyle = '#EFEEE8';
    g.lineWidth = 1;
    g.setLineDash([2, 3]);
    [0.25, 0.5, 0.75, 1].forEach(f => {
      const y = Math.round(ch - padB - f * (ch - padT - padB)) + .5;
      g.beginPath(); g.moveTo(0, y); g.lineTo(cw, y); g.stroke();
    });
    g.setLineDash([]);

    series.forEach(s => {
      const data = s.data;
      if (data.length < 2) return;
      const px = i => (i / (n - 1)) * cw;
      const py = v => ch - padB - (v / maxV) * (ch - padT - padB);
      g.beginPath();
      data.forEach((v, i) => { i ? g.lineTo(px(i), py(v)) : g.moveTo(px(i), py(v)); });
      g.strokeStyle = s.color || BLUE;
      g.lineWidth = 2;
      g.lineJoin = 'round';
      g.stroke();
      const lx = px(data.length - 1), ly = py(data[data.length - 1]);
      g.fillStyle = s.color || BLUE;
      g.fillRect(lx - 2, ly - 2, 4, 4);
    });

    g.fillStyle = MUTED;
    g.font = '9px Consolas,monospace';
    if (rawPeak < .2) {
      g.textAlign = 'center'; g.textBaseline = 'middle';
      g.font = '10px Consolas,monospace';
      g.fillText('等待第一批', cw / 2, ch / 2 - 2);
      return;
    }
    g.textAlign = 'left'; g.textBaseline = 'top';
    g.fillText(U.fmt(maxV, 1) + ' Hz', 2, 1);
  }

  /* ── 视频轨迹：0..1 归一化，无效段必须断开 ────────────────────────────── */
  function track(cv, rows, o) {
    o = o || {};
    const maxH = Math.max(280, Math.min(520, window.innerHeight * .5));
    const { g, w, h } = fit(cv, (cv.clientWidth || 640) / maxH);
    const N = 8;
    g.fillStyle = '#FFFFFF';
    g.fillRect(0, 0, w, h);

    g.strokeStyle = '#EDECE6'; g.lineWidth = 1;
    for (let i = 0; i <= N; i++) {
      const p = Math.round(i / N * w) + .5, q = Math.round(i / N * h) + .5;
      g.beginPath(); g.moveTo(p, 0); g.lineTo(p, h); g.stroke();
      g.beginPath(); g.moveTo(0, q); g.lineTo(w, q); g.stroke();
    }
    g.strokeStyle = '#C9C7BE'; g.lineWidth = 1.5;
    g.strokeRect(.75, .75, w - 1.5, h - 1.5);

    if (!rows || !rows.length) {
      g.fillStyle = MUTED;
      g.font = '11px Consolas,monospace';
      g.textAlign = 'center'; g.textBaseline = 'middle';
      g.fillText('没有轨迹点', w / 2, h / 2);
      return;
    }

    const px = r => r.x * w, py = r => r.y * h;

    let prev = null;
    g.strokeStyle = BLUE; g.lineWidth = 2.2; g.lineJoin = 'round';
    rows.forEach(r => {
      if (!r.valid) { prev = null; return; }
      if (prev) { g.beginPath(); g.moveTo(px(prev), py(prev)); g.lineTo(px(r), py(r)); g.stroke(); }
      prev = r;
    });

    for (let i = 1; i < rows.length; i++) {
      if (rows[i].valid === rows[i - 1].valid) continue;
      const r = rows[i].valid ? rows[i] : rows[i - 1];
      g.beginPath();
      g.arc(px(r), py(r), 4, 0, Math.PI * 2);
      g.strokeStyle = RED; g.lineWidth = 1.6; g.stroke();
    }

    g.fillStyle = 'rgba(43,79,242,.55)';
    rows.forEach((r, i) => {
      if (!r.valid || i % 4) return;
      g.fillRect(px(r) - 1, py(r) - 1, 2, 2);
    });

    const vis = rows.filter(r => r.valid);
    if (vis.length) {
      const a = vis[0], b = vis[vis.length - 1];
      g.fillStyle = GREEN; g.fillRect(px(a) - 5, py(a) - 5, 10, 10);
      g.fillStyle = RED; g.fillRect(px(b) - 5, py(b) - 5, 10, 10);
      g.strokeStyle = '#fff'; g.lineWidth = 1.5;
      g.strokeRect(px(b) - 5, py(b) - 5, 10, 10);
      g.fillStyle = '#fff'; g.font = '600 8px Consolas,monospace';
      g.textAlign = 'center'; g.textBaseline = 'middle';
      g.fillText('S', px(a), py(a) + .5);
      g.fillText('E', px(b), py(b) + .5);
    }

    if (o.hover) {
      const hx = px(o.hover), hy = py(o.hover);
      g.strokeStyle = INK; g.lineWidth = 1;
      g.beginPath(); g.moveTo(hx, 0); g.lineTo(hx, h); g.stroke();
      g.beginPath(); g.moveTo(0, hy); g.lineTo(w, hy); g.stroke();
      g.fillStyle = INK; g.fillRect(hx - 3, hy - 3, 6, 6);
    }

    g.fillStyle = MUTED; g.font = '9px Consolas,monospace';
    g.textAlign = 'left'; g.textBaseline = 'top';
    g.fillText('0,0', 4, 4);
    g.textAlign = 'right'; g.textBaseline = 'bottom';
    g.fillText('1,1', w - 4, h - 4);
  }

  return { maze: maze, spark: spark, track: track };
})();
