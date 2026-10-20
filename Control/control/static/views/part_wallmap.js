/* 「比赛」页的一个**区块**：迷宫重构（地图预览）。
 *
 * ## 它是从「无记忆走迷宫」页搬过来的（改版二十三）
 * 那张卡整页下线了（原因见设计方案 §11.25：新基线只跑比赛跑法，BLIND 那一套不再维护）。
 * 页里唯一还有独立价值的就是**中栏这张图** —— 它回答"车认为世界长什么样"，
 * 而比赛页回答"它现在按什么在冲"，两件事都发生在同一次发车里 ⇒ 图跟着比赛走。
 * 搬过来时**只搬图**：状态机 / 决策时间线 / 记录复盘 / 汇总都没跟过来。
 * 判据 B44 守着它（控件在、终点画对、缺数据不瞎画）。
 *
 * ## 图是**主机按帧重建**的，不是固件给的
 * 每帧带 `wallLeft/wallFront/wallRight`（**相对车身**）与 `posX/posY/dir`，
 * 所以主机能随走随建：零额外带宽（真机一次命令往返 ≈1.21 s），随帧更新。
 * 数据来自 `GET /mouse/maze/reconstruct`（纯内存读上游环形缓冲 30 s，不发命令）；
 * 固件自述那份只作**对账**，随应答一起回来。
 *
 * ## 终点是**锁死的常量**，不是帧里的量
 * 上游 v9 删了 `goalX/goalY`（§11.24 影响面 #1）⇒ 终点在帧里没有任何信号了。
 * 而比赛终点是赛道固定的**中心 2×2 四格** ⇒ 后端把它作为常量端出来
 * （`maze_rec.GOAL_CELLS` ⇒ `GET /mouse/maze/reconstruct` 的 `goal_cells`），
 * 这里**只消费、不重写**。两份各写一份坐标表 = "图上标了一个区、记录里认另一个区"
 * 的静默分裂（铁律 32）。取不到就**不画**终点，并由 `#wm-kv` 如实说"后端没给终点区"。
 *
 * ## 图例必须画出来
 * 实线 = 有墙 / 虚线 = 未知 / 不画 = 确认通。
 * 把"不知道"画成"没有墙"是这一块最容易犯也最危险的错。 */
C.parts = C.parts || {};
C.parts.wallmap = (function () {
  /*: 终点区（`[[x,y], ...]`）—— **来自后端**，不是这里写死的坐标。 */
  let goals = [];
  /*: 新建的墙图尺寸。上游 2.11 换基线后迷宫 7 → 16；这里只用来**兜底显示**，
     真实值永远取设备上报的 `mazetype`（取不到就不建图，见 `draw()`）。 */
  const FALLBACK_N = 16;

  let el = null, wall = null, timer = null, built = false;

  function q(sel) { return el ? el.querySelector(sel) : null; }

  /* ------------------------------------------------------------ 骨架 */
  function mount(host) {
    el = host;
    if (built) return;                    // 一个宿主只搭一次（区块被多张卡挂时也不重复）
    built = true;
    const box = document.createElement('div');
    box.className = 'pane';
    box.style.marginTop = '8px';
    box.innerHTML = `
      <h4>迷宫重构（地图预览） <span class="hint" id="wm-info"></span></h4>
      <canvas id="wm-cv" style="width:100%"></canvas>
      <div class="hint" id="wm-src"></div>
      <div class="kv" id="wm-kv"></div>`;
    host.appendChild(box);
    load();
  }

  function activate() {
    load();
    if (!timer) timer = setInterval(function () {
      if (!el) return;
      if (C.needMouse()) return;          // 未连接：一个请求都不发（零网络纪律）
      load();
    }, 1000);
  }

  function onHealth() {
    if (!el) return;
    if (C.needMouse()) { wall = null; draw(); }
  }

  /* ------------------------------------------------------------ 取数 */
  async function load() {
    if (C.needMouse()) { wall = null; draw(); return; }
    /* `seconds=30` 与上游环形缓冲默认长度一致：再长也取不到，再短会丢轨迹开头。 */
    const d = await C.api('/api/v1/mouse/maze/reconstruct?seconds=30')
      .catch(function () { return null; });
    if (!d) return;                        // 失败保留上一份（图比空白有用，且 `#wm-src` 会说明）
    wall = d;
    goals = Array.isArray(d.goal_cells) ? d.goal_cells : [];
    draw();
  }

  /* ------------------------------------------------------------ 画 */
  function draw() {
    const cv = q('#wm-cv');
    if (!cv || !cv.getContext) return;
    const g = cv.getContext('2d');
    const wd = wall || {};
    const info = q('#wm-info');
    const kv = q('#wm-kv');
    const src = q('#wm-src');

    /* 没有数据 / 只是"未连接" —— 两种都说清楚，**不画一张空网格冒充"没墙"**。 */
    if (!wd || !wd.n) {
      const w = Math.round(cv.clientWidth || 320);
      const H = 180;
      cv.width = w; cv.height = H; cv.style.height = H + 'px';
      g.clearRect(0, 0, w, H);
      g.fillStyle = '#5e6675'; g.font = '12px Consolas,monospace';
      const why = (wd && wd.note) ? String(wd.note) : (C.needMouse() ? '未连接电脑鼠' : '还没有重建数据');
      g.fillText(why, 8, 24);
      if (info) info.textContent = '';
      if (kv) kv.innerHTML = '';
      if (src) src.textContent = '';
      return;
    }

    const f = latestFrame();
    const realN = Number(wd.n) || FALLBACK_N;
    const track = wd.track || [];
    /* 前墙双档只有**实时读到**时才有（v9 起 `frontNear/frontFar` 是现算派生量，
       仍在帧里返回，但不占环缓冲列 ⇒ 只能从最新帧取）。三态：
       `undefined/null`（这台设备没有这个量）**不等于** 0（无墙）。 */
    const near = (f && f.frontNear != null) ? !!f.frontNear : null;
    const far = (f && f.frontFar != null) ? !!f.frontFar : null;
    const hasFront = (near !== null || far !== null);

    C.draw.maze(cv, {
      height: 360,
      n: realN,
      known: wd.known_rows || null,
      wall: wd.wall_rows || null,
      visited: wd.visited || null,
      pos: (f && f.posX != null) ? [f.posX, f.posY] : null,
      dir: (f && f.dir != null) ? f.dir : null,
      goals: goals,
      front: hasFront ? { near: !!near, far: !!far } : null,
      track: track,
      label: '实时 · 重构示意（最近 30 s）',
    });

    if (info) {
      info.textContent = (wd.frames_used != null) ? (C.util.fmt(wd.frames_used, 0) + ' 帧') : '';
    }
    if (kv) {
      let vis = 0;
      (wd.visited || []).forEach(function (r) {
        for (let i = 0; i < r.length; i++) if (r[i] === '1') vis++;
      });
      const rows = [
        ['已探索格子', vis + ' / ' + (realN * realN)],
        ['轨迹点', track.length],
        ['终点区', goals.length ? goalText() : '—（后端没给终点区）'],
        ['前墙档位', hasFront ? (near && far ? '矛盾' : (near ? '近端' : (far ? '远端' : '无'))) : '—（本机无此量）'],
      ];
      /* 终点锁死的是 16×16 的中心四格。设备报别的尺寸时**如实说**：
         那四格在这张图上不成立（越界的格不会画出来）。 */
      if (realN !== 16 && goals.length) {
        rows.push(['终点区说明', '锁死的终点格按 16×16 定义，这份固件报 ' + realN + '×' + realN]);
      }
      kv.innerHTML = rows.map(function (r) {
        return '<b>' + C.util.esc(r[0]) + '</b><span>' + C.util.esc(String(r[1])) + '</span>';
      }).join('');
    }
    if (src) renderSrc(src, wd);
  }

  function goalText() {
    return goals.map(function (c) { return '(' + c[0] + ',' + c[1] + ')'; }).join(' ');
  }

  /* 对账：主机重建 vs 固件自述。两边不平就**如实说** ——
     这真是真机要验的一条（固件不再上报路口分类，主机侧的复刻对不对，
     只有拿固件那份墙图比才知道）。固件那份没拉过就直说"未拉过"。 */
  function renderSrc(src, d) {
    const fw = d && d.firmware;
    if (!fw) { src.textContent = '主机按帧重建（未拉过固件墙图）'; return; }
    let diff = 0, same = 0;
    const known = d.known_rows, mine = d.wall_rows;
    if (known && fw.grid) {
      for (let y = 0; y < known.length; y++) {
        for (let x = 0; x < (known[y] || '').length; x++) {
          const gcell = (fw.grid[x] || [])[y];
          if (gcell == null || gcell === 0) continue;          // 固件这一格没信息
          const kw = parseInt(known[y][x], 16) & 0x0F;
          const mw = parseInt(mine[y][x], 16) & 0x0F;
          if ((((gcell & 0x0F) ^ mw) & kw) === 0) same++; else diff++;
        }
      }
    }
    src.textContent = '主机重建 ' + (d.frames_used || 0) + ' 帧 · 对账固件：一致 ' +
      same + ' / 不一致 ' + diff + ' 格';
  }

  /* 最新一帧（WS 推来的统一帧）。**零网络**，没有就回 null（图上不画车）。 */
  function latestFrame() {
    const t = (C.state && C.state.telemetry) || {};
    return t.unified || null;
  }

  function onMsg(m) {
    /* 帧驱动重画：位置/朝向随帧动，不必等那 1 s 的刷新。
       ⚠️ 只重画、**不取数**（墙图的来路只有 `load()` 一条，铁律 32）。 */
    if (m && m.type === 'mouse.telemetry') draw();
  }

  return {
    mount: mount,
    activate: activate,
    onHealth: onHealth,
    onMsg: onMsg,
    draw: draw,
    load: load,
    /* 终点区文字（" (7,7) (7,8) …"）—— 比赛卡的状态行也用它。
       ⚠️ 别让比赛卡自己再取一份 `/reconstruct`：那就有两条来路了（铁律 32）。 */
    goalsText: goalText,
    hasGoals: function () { return goals.length > 0; },
  };
})();
