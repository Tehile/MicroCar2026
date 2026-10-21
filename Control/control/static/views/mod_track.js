/* 视频复盘 · 轨迹（改版二十四）—— 读会话产物，把 x/y 画在场地归一化坐标上。
   ============================================================
   位置数据的唯一来路是 `trajectory.csv`（Auto Locate 会话产物），本页**只读**：
   不跑视觉、不改文件（显示尺度 ≠ 改数据）。

   三条纪律：
   · **有效段实线、无效段断开**：把断点连成直线等于宣称"这一段车走得通"，而它可能
     根本没定位上（那是另一种事实）；
   · **时间口径二选一**：`timebase.trust === 'seconds'` 才显示秒，否则只显示帧号
     —— 拿一条不自洽的时间轴去写"12.34 s"，看起来和真的一样准；
   · **没有对轴控件**（拍板③）：识别侧保证时间轴一致，本层不做人工指帧、不写对齐文件。
*/
C.mods.mod_track = (function () {
  let el = null;
  let data = null;            // 最近一次读到的轨迹
  let sessions = [];

  function q(sel) { return el ? el.querySelector(sel) : null; }
  function esc(s) { return C.util.esc(s == null ? '' : String(s)); }

  /* ------------------------------------------------------------ 时间口径 */
  function tbOf(d) { return (d && d.timebase) || {}; }

  function timeText(tb, row) {
    if (!row) return '';
    if (tb.trust === 'seconds' && row.t_video_s != null) {
      return C.util.fmt(row.t_video_s, 2) + ' s';
    }
    return '帧 ' + row.frame;
  }

  function timebaseText(tb) {
    if (!tb || tb.ok == null) return '时间轴：产物里没有可校验的信息';
    const issues = (tb.issues || []).map(function (i) { return i.detail || i.code; });
    if (tb.vfr) {
      return '时间轴：可变帧率（VFR）⇒ 只显示帧号，不显示秒';
    }
    if (!tb.ok) {
      return '时间轴不可信：' + (issues.length ? issues.join('；') : '原因未知');
    }
    const fps = (tb.fps == null) ? '—' : C.util.fmt(tb.fps, 1);
    return '时间轴：' + fps + ' fps · ' + tb.n_rows + ' 帧 · 自洽';
  }

  /* ------------------------------------------------------------ 画轨迹 */
  function draw() {
    const cv = q('#mt-canvas');
    if (!cv) return;
    const g = cv.getContext('2d');
    const W = cv.width, H = cv.height, N = 8;
    g.clearRect(0, 0, W, H);

    /* 网格 + 边框：0..1 的归一化场地。**不标格号**——格号属于迷宫尺寸（mazetype），
       而这条轨迹本身不带迷宫尺寸，硬编一个 8 或 16 都会在另一种场地上骗人。 */
    g.strokeStyle = 'rgba(140,150,170,0.22)';
    g.lineWidth = 1;
    for (let i = 0; i <= N; i++) {
      const p = Math.round((i / N) * W) + 0.5;
      const ly = Math.round((i / N) * H) + 0.5;
      g.beginPath(); g.moveTo(p, 0); g.lineTo(p, H); g.stroke();
      g.beginPath(); g.moveTo(0, ly); g.lineTo(W, ly); g.stroke();
    }
    const rows = (data && data.rows) || [];
    if (!rows.length) {
      g.fillStyle = 'rgba(200,190,140,0.95)';
      g.font = '12px monospace';
      g.fillText('这个会话没有轨迹点', 12, H / 2);
      return;
    }

    function px(r) { return r.x * W; }
    function py(r) { return r.y * H; }          // 图像坐标 y 向下，与画布一致，不翻

    // 无效段断开：只在"上一点有效且这一点也有效"时连线
    let prev = null;
    g.lineWidth = 2;
    g.strokeStyle = 'rgba(90,170,255,0.95)';
    rows.forEach(function (r) {
      if (!r.valid) { prev = null; return; }
      if (prev) {
        g.beginPath(); g.moveTo(px(prev), py(prev)); g.lineTo(px(r), py(r)); g.stroke();
      }
      prev = r;
    });

    // 点：有效的画小点（密度高时也看得见走向）
    g.fillStyle = 'rgba(90,170,255,0.55)';
    rows.forEach(function (r) {
      if (!r.valid) return;
      g.beginPath(); g.arc(px(r), py(r), 1.4, 0, Math.PI * 2); g.fill();
    });

    // 起点 / 终点：现场第一眼要回答"从哪儿出发、停在哪儿"
    const vis = rows.filter(function (r) { return r.valid; });
    if (vis.length) {
      g.fillStyle = 'rgba(56,209,122,0.95)';
      g.beginPath(); g.arc(px(vis[0]), py(vis[0]), 4, 0, Math.PI * 2); g.fill();
      g.fillStyle = 'rgba(255,92,92,0.95)';
      const last = vis[vis.length - 1];
      g.beginPath(); g.arc(px(last), py(last), 4, 0, Math.PI * 2); g.fill();
    }

    // 无效点位置**不画**（没有坐标就没有坐标；画成灰点会看起来像"定到了但偏了"）
  }

  /* 悬停读数：找最近的一个有效点。给"这一点是什么时候、在哪"，
     不解释、不推断（解释归文档）。 */
  function hover(ev) {
    const cv = q('#mt-canvas');
    const out = q('#mt-hover');
    if (!cv || !out || !data || !data.rows) return;
    const rect = cv.getBoundingClientRect();
    const mx = (ev.clientX - rect.left) / rect.width * cv.width;
    const my = (ev.clientY - rect.top) / rect.height * cv.height;
    let best = null, bd = 1e9;
    data.rows.forEach(function (r) {
      if (!r.valid) return;
      const dx = r.x * cv.width - mx, dy = r.y * cv.height - my;
      const d = dx * dx + dy * dy;
      if (d < bd) { bd = d; best = r; }
    });
    if (!best || bd > 24 * 24) { out.textContent = ''; return; }
    const tb = tbOf(data);
    out.textContent = timeText(tb, best) + ' · x ' + C.util.fmt(best.x, 3) +
      ' · y ' + C.util.fmt(best.y, 3) +
      (best.rms_field == null ? '' : (' · rms ' + C.util.fmt(best.rms_field, 2)));
  }

  /* ------------------------------------------------------------ 会话列表 */
  async function loadSessions(keep) {
    const d = await Q('/api/v1/video/sessions', '读会话列表');
    if (!d) return;
    sessions = d.sessions || [];
    const sel = q('#mt-sess');
    if (!sel) return;
    if (!sessions.length) {
      sel.innerHTML = '<option value="">（' + esc(d.root) + ' 里没有会话）</option>';
      data = null;
      draw();
      const sum = q('#mt-summary');
      if (sum) sum.textContent = '还没有解析过视频：先去「视频会话」页跑一次';
      return;
    }
    const cur = keep || sel.value || sessions[0].id;
    sel.innerHTML = sessions.map(function (s) {
      const n = (s.n_valid == null) ? '?' : s.n_valid;
      const t = (s.n_total == null) ? '?' : s.n_total;
      return '<option value="' + esc(s.id) + '">' + esc(s.id) +
        ' · ' + n + '/' + t + ' 有效</option>';
    }).join('');
    sel.value = sessions.some(function (s) { return s.id === cur; }) ? cur : sessions[0].id;
    await loadTrajectory(sel.value);
  }

  async function Q(path, label) {
    return C.guard(function () { return C.api(path); }, label);
  }

  async function loadTrajectory(id) {
    const sum = q('#mt-summary');
    if (!id) { data = null; draw(); return; }
    const d = await Q('/api/v1/video/trajectory/' + encodeURIComponent(id), '读轨迹');
    if (!d) { data = null; draw(); return; }
    data = d;
    const tb = tbOf(d);
    const tbLine = q('#mt-timebase');
    if (tbLine) {
      tbLine.textContent = timebaseText(tb);
      tbLine.className = (tb.ok && !tb.vfr) ? 'hint' : 'warnbox';
    }
    if (sum) {
      const s = sessions.filter(function (x) { return x.id === id; })[0] || {};
      sum.textContent = '有效 ' + d.n_valid + '/' + d.n_total + ' 点' +
        (d.sampled ? '（点多，画的是抽样）' : '') +
        (s.frames ? (' · 视频 ' + s.frames + ' 帧') : '') +
        ((s.fps != null) ? (' · ' + C.util.fmt(s.fps, 1) + ' fps') : '');
    }
    draw();
    const out = q('#mt-hover');
    if (out) out.textContent = '';
  }

  /* ------------------------------------------------------------ 挂载 */
  function mount(host) {
    el = host;
    el.innerHTML = `
      <div class="pane">
        <h4>会话 <span class="hint" id="mt-summary"></span>
          <span><button class="mini" id="mt-refresh">刷新</button></span></h4>
        <div class="row"><select id="mt-sess" style="flex:1"></select></div>
        <div class="hint" id="mt-timebase" style="margin-top:4px"></div>
        <div class="row" style="margin-top:6px">
          <canvas id="mt-canvas" width="360" height="360"
            style="background:rgba(20,24,32,0.6);border:1px solid var(--line);border-radius:6px"></canvas>
        </div>
        <div class="hint" id="mt-hover" style="font-family:var(--mono)"></div>
      </div>`;
    q('#mt-sess').onchange = function () { loadTrajectory(q('#mt-sess').value); };
    q('#mt-refresh').onclick = function () { loadSessions(q('#mt-sess').value); };
    q('#mt-canvas').addEventListener('mousemove', hover);
    loadSessions();
  }

  return {
    mount: mount,
    activate: function () { loadSessions(q('#mt-sess') ? q('#mt-sess').value : ''); },
  };
})();
