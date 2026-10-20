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

     ## 前墙双档（上游 2.3.0）—— **两档落在不同的边上**
     上游把前墙拆成互斥两档（`frontNear | frontFar == wallFront`）：
       · **近端墙** = 前方最近一个有墙（贴脸，< 80 mm）⇒ 画在**本格前沿**，加粗暖红；
       · **远端墙** = 当前方向**下一个判断**有墙（提前量，一格以内）⇒ 画在**外一格前沿**，
         短划琥珀。
     这两档是**传感器读数**、不是地图属性 ⇒ 只画在这两条前沿上，
     不写进 `known`/`wall` 位掩码（写进去就变成"地图里的一堵墙"，而那堵墙可能还没进格）。
     ⚠️ 两档**必须分开画**：早先都落在同一条前沿上，"贴脸"看起来却像隔了一格，
     照图判断会误以为"还有一格能走"。

     ## 起点角（(0,0) 画在左下）
     上游不变量：**发车前电脑鼠必须物理摆在左下角格、车头朝上（dir=0）**。
     图里用虚线圈标出这个"应该在哪"，让现场能当场判"是不是摆错了"。

     ## 入参形态刻意与后端落盘一致
     `known` / `wall` / `visited` 都是**字符串数组**（`known_rows[y][x]` 是十六进制位掩码），
     这样"实时重建"与"复盘重建"两条路的入参**天然同形**，前端不必写两套解析。
     位序：bit0=上(N) bit1=右(E) bit2=下(S) bit3=左(W)（与上游 `WALL_*_OPEN` 同一套方向）。

     ## 坐标（**起点在左下、朝向向上**）
     上游 `decode_map` 是**行主序、x 外层**（`grid[x][y]`），落盘时转成 `rows[y][x]`。
     位掩码里的"上/下"是**固件的绝对方向**：bit0=上(N) / bit2=下(S)。
     固件的格 y **向上增大**（`mouseDirection=0`(UP) 时 `mousePos.cY += num`）⇒
     原点 **(0,0) 在左下角**，而发车要求车头**朝上**（dir=0）。
     ⇒ 渲染时**把屏幕 y 翻转**（`PY(y)=(n-1-y)`），使 (0,0) 落在画面**左下**、
     "上(+Y)"朝画面上方 —— 与现场摆车的方向一致（见函数体内的详注）。 */
  function maze(cv, o) {
    o = o || {};
    /* 出口标记（本函数自己写、图例自己读）：先清零再画 —— 同一份入参若被复用两次
       绘制，第二次绝不能读到上一次的残留（那会让图例报一个这次没画的档位）。 */
    o._frontDrawn = null;
    const r = fit(cv, o.height || 360);
    const g = r.g, w = r.w, h = r.h;
    const n = Math.max(1, o.n || 7);
    const pad = 26;
    const size = Math.max(60, Math.min(w, h) - pad);
    const x0 = (w - size) / 2, y0 = (h - size) / 2;
    const cs = size / n;
    /* ⚠️ **y 轴翻转**：固件的格坐标 y **向上增大**（`mouseDirection` 0=UP 时
       `mazeState.mousePos.cY += num`，见 `STRATEGY/maze.c` 的 `MAZE_CoorUpdateFast`），
       原点 (0,0) 在**左下角**。而画布 y 向下增大 ⇒ 直接把 `y` 当屏幕 y 会把整张图
       **上下镜像**：(0,0) 画到左上、起点角跑到画面另一头。
       两者都是"自洽"的（拓扑没错），但图上标的"起点在左下"与看到的车位置对不上 ——
       现场据此判"摆错了"就会误判。⇒ 这里显式翻转：屏幕 y = (n-1-y)。 */
    const PX = function (x) { return x0 + x * cs; };
    const PY = function (y) { return y0 + (n - 1 - y) * cs; };
    const mid = function (x, y) {
      return [x0 + (x + 0.5) * cs, y0 + (n - 1 - y + 0.5) * cs];
    };

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

    // ---- 终点区（**锁死的固定格**，不是帧里的量）
    //
    // 上游 v9 把 `goalX/goalY` 整字节删了（§11.24 影响面 #1），终点也不再有帧内信号。
    // 而比赛的终点本来就是**赛道固定的中心 2×2 四格** ⇒ 由调用方传常量进来
    // （`o.goals`，见 `part_wallmap.js` 的 `GOAL_CELLS`），不从数据里猜。
    // ⚠️ 画**整格**（淡底 + 外框），不是一个圆点：四格是一个"区"，
    //   画成点会让人以为只有某一格算终点，从而误判"还没到"。
    // ⚠️ 越界的格**不画**（`mazetype` 不是 16 时 (8,7)/(8,8) 不存在）——
    //   宁可少画，也不把一格画到画布外面去。
    if (o.goals && o.goals.length) {
      g.strokeStyle = '#38d17a'; g.lineWidth = 2;
      for (let i = 0; i < o.goals.length; i++) {
        const gx = o.goals[i][0], gy = o.goals[i][1];
        if (gx == null || gy == null) continue;
        if (gx < 0 || gy < 0 || gx >= n || gy >= n) continue;
        g.fillStyle = 'rgba(56,209,122,.16)';
        g.fillRect(PX(gx), PY(gy), cs, cs);
        g.strokeRect(PX(gx) + 1, PY(gy) + 1, Math.max(1, cs - 2), Math.max(1, cs - 2));
      }
    }

    // ---- 墙：实线=有墙 / 虚线=不知道 / 不画=确认通
    //
    // 位序是**固件的绝对方向**：bit0=上(N)=+Y / bit1=右(E)=+X / bit2=下(S)=−Y / bit3=左(W)=−X。
    // ⚠️ **格 (x,y) 的屏幕纵向范围是 `[PY(y), PY(y)+cs]`** —— `PY` 把固件 y 翻成了屏幕 y，
    //   所以 `PY(y)` 是这一格的**上**沿（不是下沿）。四条边因此是：
    //   · bit0（上/N，+Y）= 该格**上**沿横线 `PY(y)`（与格 y+1 的共享边）；
    //   · bit2（下/S，−Y）= 该格**下**沿横线 `PY(y)+cs`（与格 y−1 的共享边）；
    //   · bit1（右/E）= 右边竖线 `PX(x+1)`；bit3（左/W）= 左边竖线 `PX(x)`，
    //     两条竖线都跨该格**整高** `PY(y)`→`PY(y)+cs`。
    // ⚠️ 这里曾把 bit0 画在 `PY(y+1)`、竖线跨 `PY(y)`→`PY(y+1)`（**整体上移了整整一格**）
    //   ⇒ 墙画进了上一格的格子里，而**最底行一格竖线都没有**（现场报"最下面一行缺少画线格"）。
    // 每道边只画一次：遍历每格的**上沿与左沿**，再补最右列（bit1）与最底行（bit2）。
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
        // 上沿 = 固件的"上"（bit0）：本格的上边线 `PY(y)`
        drawEdge(PX(x), PY(y), PX(x + 1), PY(y),
                 bit(o.known, x, y, 0), bit(o.wall, x, y, 0));
        // 左沿 = 固件的"左"（bit3）：`PX(x)`，跨本格整高
        drawEdge(PX(x), PY(y), PX(x), PY(y) + cs,
                 bit(o.known, x, y, 3), bit(o.wall, x, y, 3));
        // 最右列补右边 = 固件的"右"（bit1）：`PX(n)`，跨本格整高
        if (x === n - 1) {
          drawEdge(PX(n), PY(y), PX(n), PY(y) + cs,
                   bit(o.known, x, y, 1), bit(o.wall, x, y, 1));
        }
        // 最底行（y=0）补下沿 = 固件的"下"（bit2）：`PY(0)+cs`（= 画布下边界）
        if (y === 0) {
          drawEdge(PX(x), PY(0) + cs, PX(x + 1), PY(0) + cs,
                   bit(o.known, x, y, 2), bit(o.wall, x, y, 2));
        }
      }
    }
    g.setLineDash([]);
    // 外框
    g.strokeStyle = '#4a5468'; g.lineWidth = 1;
    g.strokeRect(x0, y0, size, size);

    /* ---- 起点角标记（左下角 = (0,0)）
       上游的不变量：**发车前电脑鼠必须物理摆在左下角格、且车头朝上（dir=0）**。
       图里把这个"应该在哪"画出来 —— 否则现场看到车在别处、或知道有这条约定却
       对不上图上的位置，无法当场判"是不是摆错了"。虚线圈 + "起点"两字，
       不压任何真实数据（它是约定，不是读数）。
       ⚠️ 起点是 (x=0, y=0)（固件原点），不是 (0, n-1) —— `PY` 已翻转，直接 `mid(0,0)`
       就落在画面左下角。 */
    (function () {
      const p = mid(0, 0);
      g.setLineDash([2, 2]);
      g.strokeStyle = 'rgba(232,176,60,.55)'; g.lineWidth = 1.2;
      g.beginPath(); g.arc(p[0], p[1], cs * 0.40, 0, Math.PI * 2); g.stroke();
      g.setLineDash([]);
      g.fillStyle = 'rgba(232,176,60,.75)';
      g.font = '9px Consolas,monospace';
      g.fillText('起点', p[0] - 9, p[1] - cs * 0.40 - 3);
    })();

    // ---- 小车：三角，朝向按 dir（0=上 1=右 2=下 3=左）
    // ⚠️ 起点要求"朝上" ⇒ dir=0 时三角指向上方（画布 y 减小）。
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

    /* ---- 前墙双档（2.3.0）：**近端画本格前沿、远端画外一格前沿**
          · 近端墙（`frontNear`，前方最近一个有墙，< 80 mm 贴脸）= 加粗实线、暖红；
          · 远端墙（`frontFar`，当前方向下一个判断有墙，≥ 80 mm 提前量）= 短划、琥珀。
         为什么要**在这张图上**画这两档：中栏回答的是"它认为世界长什么样"，
         而近/远两档决定的正是**下一步该怎么走**（减速还是马上转）。
         只在实际读到那一档时才画 —— 读不到（老上游）就什么都不画，不猜。 */
    if (o.pos && o.pos[0] != null && o.front) {
      const x = o.pos[0], y = o.pos[1];
      const d = (o.dir == null ? 0 : o.dir) & 3;
      /* ⚠️ 屏幕 y 已翻转，且格 (x,y) 的纵向范围是 `[PY(y), PY(y)+cs]` ⇒ 与墙位掩码
         同一套对应（格自身的边）：
           d=0(UP,+Y)   = 上沿 `PY(y)`；      d=2(DOWN,−Y) = 下沿 `PY(y)+cs`；
           d=1(RIGHT)   = 右边 `PX(x+1)`；    d=3(LEFT)    = 左边 `PX(x)`。
         **两档必须落在不同的边上**（这是本轮修的真缺陷）：
           · 近端墙（`frontNear`，< 80 mm 贴脸）= **本格**前沿 —— 就在车头这一格边上；
           · 远端墙（`frontFar`，≥ 80 mm 提前量）= 沿 d **再往前一格**的前沿。
         早先两档都画在同一条边（`PY(y+1)` / `PY(y)`），于是近端墙看起来"隔了一格"，
         与"贴脸"这个语义正好相反 ⇒ 现场照着图判断"还有一格能走"就撞了。 */
      const front = function (k) {
        // k=0 → 本格前沿；k=1 → 外一格前沿
        if (d === 0) { const yy = PY(y) - k * cs; return [PX(x), yy, PX(x + 1), yy]; }
        if (d === 2) { const yy = PY(y) + cs + k * cs; return [PX(x), yy, PX(x + 1), yy]; }
        if (d === 1) { const xx = PX(x + 1) + k * cs; return [xx, PY(y), xx, PY(y) + cs]; }
        const xx = PX(x) - k * cs; return [xx, PY(y), xx, PY(y) + cs];
      };
      /* 远端墙那一格的前沿可能落在**画布外**（车贴着最外一行、还朝着外面走）⇒
         这时画了也看不见，只剩左栏报"远端"而图上什么都没有 = 静默。
         ⇒ 判是否整段落在网格内（`x0..x0+size` / `y0..y0+size`），落在外面就**不画**
         （图例也不列，左栏那行仍如实报档位）。 */
      const inside = function (e) {
        const m = 1;   // 容 1 px：线宽一半落在格线上也不丢
        return e[0] >= x0 - m && e[2] <= x0 + size + m &&
               e[1] >= y0 - m && e[3] <= y0 + size + m;
      };
      const want = o.front.near ? 'near' : (o.front.far ? 'far' : null);
      let e = want ? front(want === 'near' ? 0 : 1) : null;
      if (e && !inside(e)) e = null;
      if (e) {
        if (want === 'near') {
          g.setLineDash([]);
          g.strokeStyle = '#ff7a5c'; g.lineWidth = 3.4; g.lineCap = 'round';
        } else {
          g.setLineDash([5, 3]);
          g.strokeStyle = '#e8b03c'; g.lineWidth = 2.4; g.lineCap = 'butt';
        }
        g.beginPath(); g.moveTo(e[0], e[1]); g.lineTo(e[2], e[3]); g.stroke();
      }
      g.setLineDash([]); g.lineCap = 'butt';
      // 画出来的那一条供图例复用（画在画布外就不进图例 —— 否则满图找那条线）
      o._frontDrawn = e ? want : null;
    }

    // ---- 图例（每种边各什么意思，必须画出来 —— 否则读者只能猜）
    // ⚠️ 前墙两档**只在图上真的画了**时才进图例：① 老上游读不到这两档时不画，
    //    列一条"从不出现的图例"只会让人到处找；② 远端墙那一格前沿落在画布外
    //    （车贴着最外一行还朝外走）时也不画 —— 这里判 `o._frontDrawn`（**画出来了什么**），
    //    不是判"传进来了什么"。
    g.font = '10px Consolas,monospace';
    let lx = x0, ly = y0 + size + 15;
    const legend = [['#e8e8ea', [], '有墙'], ['#333a47', [3, 3], '未知'],
                    ['#1b2231', [], '走过']];
    if (o._frontDrawn === 'near') { legend.push(['#ff7a5c', [], '近端墙']); }
    else if (o._frontDrawn === 'far') { legend.push(['#e8b03c', [5, 3], '远端墙']); }
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
