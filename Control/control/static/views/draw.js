/* 自绘工具：波形 / 网格 / 轨迹。
   为什么不用图表库：250 Hz × 9 通道 × 10 s = 22500 点，任何 DOM/SVG 方案都会卡；
   Canvas 手画在 60 fps 下毫无压力，而且不需要构建链（现场没网）。 */

C.draw = (function () {

  function fit(cv, h) {
    const w = cv.clientWidth || 600;
    const dpr = window.devicePixelRatio || 1;
    const H = h || Math.round(w * 0.45);
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(H * dpr)) {
      cv.width = Math.round(w * dpr);
      cv.height = Math.round(H * dpr);
      cv.style.height = H + 'px';
    }
    const g = cv.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, H);
    return { g: g, w: w, h: H };
  }

  function axes(g, w, h, pad, ymin, ymax, label) {
    g.strokeStyle = '#2c313d';
    g.lineWidth = 1;
    g.beginPath();
    g.moveTo(pad.l, pad.t); g.lineTo(pad.l, h - pad.b);
    g.lineTo(w - pad.r, h - pad.b);
    g.stroke();
    g.fillStyle = '#5e6675';
    g.font = '10px Consolas,monospace';
    if (label) {
      g.fillText(label, pad.l + 4, pad.t + 11);
    }
    const n = 4;
    for (let i = 0; i <= n; i++) {
      const y = pad.t + (h - pad.t - pad.b) * i / n;
      const v = ymax - (ymax - ymin) * i / n;
      g.strokeStyle = '#20242e';
      g.beginPath(); g.moveTo(pad.l, y); g.lineTo(w - pad.r, y); g.stroke();
      g.fillStyle = '#5e6675';
      g.fillText(fmtv(v), 2, y + 3);
    }
  }

  function fmtv(v) {
    const a = Math.abs(v);
    if (a >= 10000) return (v / 1000).toFixed(0) + 'k';
    if (a >= 100) return v.toFixed(0);
    if (a >= 1) return v.toFixed(1);
    if (a === 0) return '0';
    return v.toFixed(2);
  }

  /* 多通道波形。series = [{name, data:Float32Array, color, on:bool}] */
  function waves(cv, series, opt) {
    opt = opt || {};
    const r = fit(cv, opt.height);
    const g = r.g, w = r.w, h = r.h;
    const pad = { l: 44, r: 8, t: 14, b: 18 };
    const on = series.filter(function (s) { return s.on && s.data && s.data.length; });
    if (!on.length) {
      g.fillStyle = '#5e6675'; g.font = '12px Consolas,monospace';
      g.fillText(opt.empty || '无数据', pad.l, h / 2);
      return r;
    }
    let ymin = Infinity, ymax = -Infinity;
    on.forEach(function (s) {
      for (let i = 0; i < s.data.length; i++) {
        const v = s.data[i];
        if (!isFinite(v)) continue;
        if (v < ymin) ymin = v;
        if (v > ymax) ymax = v;
      }
    });
    if (!isFinite(ymin)) { ymin = 0; ymax = 1; }
    if (ymax - ymin < 1e-9) { ymax += 1; ymin -= 1; }
    const m = (ymax - ymin) * 0.08;
    ymin -= m; ymax += m;
    axes(g, w, h, pad, ymin, ymax, opt.label);

    const N = on[0].data.length;
    const x0 = pad.l, xw = w - pad.l - pad.r;
    on.forEach(function (s) {
      g.strokeStyle = s.color; g.lineWidth = 1;
      g.beginPath();
      const step = Math.max(1, Math.floor(N / (xw * 2)));   // 降采样到 2 像素/点
      for (let i = 0; i < N; i += step) {
        const x = x0 + xw * (i / Math.max(1, N - 1));
        const y = pad.t + (h - pad.t - pad.b) * (1 - (s.data[i] - ymin) / (ymax - ymin));
        if (i === 0) g.moveTo(x, y); else g.lineTo(x, y);
      }
      g.stroke();
    });
    if (opt.cursorA !== undefined || opt.cursorB !== undefined) {
      [['A', opt.cursorA, '#e8b03c'], ['B', opt.cursorB, '#4da3ff']].forEach(function (c) {
        if (c[1] === undefined || c[1] === null) return;
        const x = x0 + xw * c[1];
        g.strokeStyle = c[2]; g.setLineDash([3, 3]);
        g.beginPath(); g.moveTo(x, pad.t); g.lineTo(x, h - pad.b); g.stroke();
        g.setLineDash([]);
        g.fillStyle = c[2]; g.fillText(c[0], x + 2, pad.t + 10);
      });
    }
    // 图例
    let lx = pad.l + 6;
    on.forEach(function (s) {
      g.fillStyle = s.color;
      g.fillRect(lx, h - 13, 8, 3);
      g.fillStyle = '#8b94a6';
      const t = s.name + (s.unit ? ' (' + s.unit + ')' : '');
      g.fillText(t, lx + 11, h - 9);
      lx += 12 + g.measureText(t).width;
    });
    return r;
  }

  /* 场地：8×8 网格 + 固件位置 + 视觉位置 + 轨迹 */
  function arena(cv, o) {
    const r = fit(cv, o.height);
    const g = r.g, w = r.w, h = r.h;
    const size = Math.min(w, h) - 24;
    const x0 = (w - size) / 2, y0 = (h - size) / 2;
    const px = function (nx, ny) { return [x0 + nx * size, y0 + (1 - ny) * size]; };

    g.fillStyle = '#0b0d12';
    g.fillRect(x0, y0, size, size);
    g.strokeStyle = '#2c313d'; g.lineWidth = 1;
    const G = o.grid || 8;
    for (let i = 0; i <= G; i++) {
      const p = px(i / G, 0);
      g.beginPath(); g.moveTo(p[0], y0); g.lineTo(p[0], y0 + size); g.stroke();
      const q = px(0, i / G);
      g.beginPath(); g.moveTo(x0, q[1]); g.lineTo(x0 + size, q[1]); g.stroke();
    }
    g.strokeStyle = '#4a5468'; g.lineWidth = 2;
    g.strokeRect(x0, y0, size, size);

    // 坐标轴提示（Auto Locate 的原点在左下角）
    g.fillStyle = '#5e6675'; g.font = '10px Consolas,monospace';
    g.fillText('(0,0) 原点在左下', x0, y0 + size + 14);
    g.fillText('+x →', x0 + size - 26, y0 + size + 14);

    if (o.watermark) {
      g.fillStyle = 'rgba(232,176,60,.85)';
      g.font = 'bold 13px Consolas,monospace';
      g.fillText(o.watermark, x0 + 8, y0 + 20);
    }

    // 轨迹
    function poly(pts, color, width, dash) {
      if (!pts || pts.length < 2) return;
      g.strokeStyle = color; g.lineWidth = width || 1.5;
      if (dash) g.setLineDash(dash);
      g.beginPath();
      pts.forEach(function (p, i) {
        const q = px(p[0], p[1]);
        if (i === 0) g.moveTo(q[0], q[1]); else g.lineTo(q[0], q[1]);
      });
      g.stroke();
      g.setLineDash([]);
    }
    poly(o.trackOnline, 'rgba(77,163,255,.55)', 1.5);            // 在线：稀疏、半透明
    poly(o.trackOffline, '#38d17a', 2);                          // 离线：全量、权威

    // 固件自述位置 △
    if (o.fw) {
      const c = px((o.fw[0] + 0.5) / G, (o.fw[1] + 0.5) / G);
      g.fillStyle = '#e8b03c';
      g.beginPath();
      g.moveTo(c[0], c[1] - 7); g.lineTo(c[0] - 6, c[1] + 5); g.lineTo(c[0] + 6, c[1] + 5);
      g.closePath(); g.fill();
    }
    // 视觉位置 ▲
    if (o.vis && o.vis.valid && o.vis.x !== null && o.vis.x !== undefined) {
      const c = px(o.vis.x, o.vis.y);
      g.fillStyle = '#4da3ff';
      g.beginPath();
      g.moveTo(c[0] + 7, c[1] - 6); g.lineTo(c[0] - 5, c[1] - 6); g.lineTo(c[0] + 1, c[1] + 6);
      g.closePath(); g.fill();
      if (o.vis.theta_deg !== null && o.vis.theta_deg !== undefined) {
        const th = o.vis.theta_deg * Math.PI / 180;
        g.strokeStyle = '#4da3ff'; g.lineWidth = 1.5;
        g.beginPath(); g.moveTo(c[0], c[1]);
        g.lineTo(c[0] + Math.cos(th) * 26, c[1] - Math.sin(th) * 26);
        g.stroke();
      }
    }
    if (o.playback) {
      g.fillStyle = '#e8b03c'; g.font = '11px Consolas,monospace';
      g.fillText('回放 @ ' + o.playback, x0 + 8, y0 + size - 8);
    }
    return { r: r, px: px, size: size, x0: x0, y0: y0 };
  }

  /* 时间轴带：一行事件带（source 分三色） */
  function timeline(cv, rows, t0, t1, cursor) {
    const r = fit(cv, 54);
    const g = r.g, w = r.w, h = r.h;
    const pad = { l: 8, r: 8, t: 6, b: 16 };
    const lanes = { mouse: 0, camera: 1, locate: 2, session: 3, arena: 3 };
    const colors = { mouse: '#e8b03c', camera: '#4da3ff', locate: '#38d17a',
                     session: '#a06bff', arena: '#ff7ac6' };
    const xw = w - pad.l - pad.r;
    const laneH = (h - pad.t - pad.b) / 4;
    g.fillStyle = '#5e6675'; g.font = '9px Consolas,monospace';
    ['电脑鼠', '相机', '定位', '会话'].forEach(function (t, i) {
      g.fillText(t, pad.l, pad.t + laneH * i + 8);
    });
    const span = Math.max(1, t1 - t0);
    rows.forEach(function (row) {
      const lane = lanes[row.source];
      if (lane === undefined) return;
      const t = Number(row.t_pc_ms);
      if (!isFinite(t)) return;
      const x = pad.l + xw * ((t - t0) / span);
      const y = pad.t + laneH * lane + laneH - 4;
      g.fillStyle = colors[row.source] || '#8b94a6';
      g.fillRect(x, y - 3, 1.5, 6);
    });
    if (cursor !== undefined && cursor !== null) {
      const x = pad.l + xw * ((cursor - t0) / span);
      g.strokeStyle = '#fff'; g.lineWidth = 1;
      g.beginPath(); g.moveTo(x, pad.t); g.lineTo(x, h - pad.b); g.stroke();
    }
    return { r: r, pad: pad, xw: xw, span: span, t0: t0 };
  }

  /* 迷宫"重构示意"—— 实验 8 的中栏。

     ## 三种边长得不一样，这是这一张图最要紧的事
        · **实线** = 确认**有墙**
        · **虚线** = **还不知道**（没走过那个方向）
        · **不画** = 确认**没有墙**（走得通）
     把"不知道"画成"没有墙"是本层明令禁止的那种错（三态纪律：判不了 ≠ 不通过）——
     现场会据此以为"这条路能走"，而设备其实从没看过那一面。

     ## 入参形态刻意与后端落盘一致
     `known` / `wall` / `visited` 都是**字符串数组**（`known_rows[y][x]` 是十六进制位掩码），
     这样"实时重建"与"复盘重建"两条路的入参**天然同形**，前端不必写两套解析。
     位序：bit0=上(N) bit1=右(E) bit2=下(S) bit3=左(W)（与上游 `WALL_*_OPEN` 同一套方向）。

     ## 坐标
     上游 `decode_map` 是**行主序、x 外层**（`grid[x][y]`），而落盘时转成了
     `rows[y][x]`。这里按 **x 向右、y 向下**画 —— 与 ASCII 迷宫（`maze.to_text()`）一致，
     也和"俯视图里车头朝上"的直觉一致。 */
  function maze(cv, o) {
    o = o || {};
    const r = fit(cv, o.height || 360);
    const g = r.g, w = r.w, h = r.h;
    const n = Math.max(1, o.n || 7);
    const pad = 26;
    const size = Math.max(60, Math.min(w, h) - pad);
    const x0 = (w - size) / 2, y0 = (h - size) / 2;
    const cs = size / n;
    const PX = function (x) { return x0 + x * cs; };
    const PY = function (y) { return y0 + y * cs; };
    const mid = function (x, y) { return [x0 + (x + 0.5) * cs, y0 + (y + 0.5) * cs]; };

    const bit = function (rows, x, y, i) {
      if (!rows || !rows[y] || x >= rows[y].length) return 0;
      const v = parseInt(rows[y][x], 16);
      return (isNaN(v) ? 0 : v) & (1 << i);
    };
    const vis = function (x, y) {
      return !!(o.visited && o.visited[y] && o.visited[y][x] === '1');
    };

    g.fillStyle = '#0b0d12';
    g.fillRect(x0, y0, size, size);

    // ---- 底色：走过 / 没走过（"没走过"不是"不存在"，所以仍画格子，只是更暗）
    for (let y = 0; y < n; y++) {
      for (let x = 0; x < n; x++) {
        g.fillStyle = vis(x, y) ? '#1b2231' : '#101419';
        g.fillRect(PX(x), PY(y), cs, cs);
      }
    }

    // ---- 轨迹（先画，让墙压在它上面 —— 墙是结论，轨迹只是过程）
    if (o.track && o.track.length > 1) {
      g.strokeStyle = 'rgba(77,163,255,.55)';
      g.lineWidth = Math.max(2, cs * 0.12);
      g.lineJoin = 'round';
      g.beginPath();
      for (let i = 0; i < o.track.length; i++) {
        const p = mid(o.track[i][0], o.track[i][1]);
        if (i === 0) g.moveTo(p[0], p[1]); else g.lineTo(p[0], p[1]);
      }
      g.stroke();
    }

    // ---- 目标格（决策结果：它认为该去哪儿）
    if (o.goal && o.goal[0] != null) {
      const p = mid(o.goal[0], o.goal[1]);
      g.strokeStyle = '#38d17a'; g.lineWidth = 2;
      g.beginPath(); g.arc(p[0], p[1], cs * 0.26, 0, Math.PI * 2); g.stroke();
    }

    // ---- 墙：实线=有墙 / 虚线=不知道 / 不画=确认通
    //
    // 每道边只画一次：遍历每格的**上边与左边**，再补最右列与最后一行。
    // 依赖"位掩码是**双向**的"（后端 `MazeWallMap._set()` 写一格时同时写邻居的反向）——
    // 所以从任一侧读同一个 `bit()` 都得到同一个答案，不必在两格之间做或运算。
    const drawEdge = function (ax, ay, bx, by, known, has) {
      if (known && has) {
        g.setLineDash([]); g.strokeStyle = '#e8e8ea'; g.lineWidth = 2.2;
      } else if (known) {
        return;                              // 确认通 —— **不画**（"能过"就是没有线）
      } else {
        g.setLineDash([3, 3]); g.strokeStyle = '#333a47'; g.lineWidth = 1;
      }
      g.beginPath(); g.moveTo(ax, ay); g.lineTo(bx, by); g.stroke();
    };
    g.lineCap = 'butt';
    for (let y = 0; y < n; y++) {
      for (let x = 0; x < n; x++) {
        // 上边（bit0）
        drawEdge(PX(x), PY(y), PX(x + 1), PY(y),
                 bit(o.known, x, y, 0), bit(o.wall, x, y, 0));
        // 左边（bit3）
        drawEdge(PX(x), PY(y), PX(x), PY(y + 1),
                 bit(o.known, x, y, 3), bit(o.wall, x, y, 3));
        // 最右列补右边（bit1）
        if (x === n - 1) {
          drawEdge(PX(n), PY(y), PX(n), PY(y + 1),
                   bit(o.known, x, y, 1), bit(o.wall, x, y, 1));
        }
        // 最后一行补下边（bit2）
        if (y === n - 1) {
          drawEdge(PX(x), PY(n), PX(x + 1), PY(n),
                   bit(o.known, x, y, 2), bit(o.wall, x, y, 2));
        }
      }
    }
    g.setLineDash([]);
    // 外框
    g.strokeStyle = '#4a5468'; g.lineWidth = 1;
    g.strokeRect(x0, y0, size, size);

    // ---- 小车：三角，朝向按 dir（0=上 1=右 2=下 3=左）
    if (o.pos && o.pos[0] != null) {
      const p = mid(o.pos[0], o.pos[1]);
      const d = (o.dir == null ? 0 : o.dir) & 3;
      const rr = cs * 0.34;
      const ang = [(-Math.PI / 2), 0, (Math.PI / 2), Math.PI][d];
      g.save();
      g.translate(p[0], p[1]); g.rotate(ang);
      g.fillStyle = '#e8b03c';
      g.beginPath();
      g.moveTo(rr, 0); g.lineTo(-rr * 0.7, -rr * 0.75); g.lineTo(-rr * 0.7, rr * 0.75);
      g.closePath(); g.fill();
      g.restore();
    }

    // ---- 图例（三种边各什么意思，必须画出来 —— 否则读者只能猜）
    g.font = '10px Consolas,monospace';
    let lx = x0, ly = y0 + size + 15;
    const legend = [['#e8e8ea', [], '有墙'], ['#333a47', [3, 3], '未知'],
                    ['#1b2231', [], '走过']];
    legend.forEach(function (it) {
      g.strokeStyle = it[0]; g.lineWidth = 2; g.setLineDash(it[1]);
      g.beginPath(); g.moveTo(lx, ly - 4); g.lineTo(lx + 14, ly - 4); g.stroke();
      g.setLineDash([]);
      g.fillStyle = '#8b94a6';
      g.fillText(it[2], lx + 18, ly);
      lx += 18 + g.measureText(it[2]).width + 12;
    });
    if (o.label) {
      g.fillStyle = '#5e6675';
      g.font = '10px Consolas,monospace';
      g.fillText(o.label, x0, y0 - 8);
    }
    return r;
  }

  const COLORS = ['#4da3ff', '#e8b03c', '#38d17a', '#ff7ac6', '#a06bff',
                  '#ff9f43', '#4dd6d6', '#d6d64d', '#ff5c5c', '#8b94a6'];

  return { waves: waves, arena: arena, timeline: timeline, maze: maze,
           colors: COLORS, fmtv: fmtv };
})();
