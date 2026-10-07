/* ① 场地 —— 整合的核心视图，也是这个项目存在的理由。
   把**两个完全独立的定位源**画在同一张图上：固件自述的格子坐标 △ 与
   手机俯拍的视觉定位 ▲，直接给出 Δpos / Δθ / Δt。
   它回答的都是真问题：视觉可信吗？里程计漂了多少？两者时间对齐对不对？ */

C.views.arena = (function () {
  let el, cvArena, cvTime, img;
  let live = { fw: null, vis: null, delta: null, arena: null, clock: null };
  let trackOnline = [], trackOffline = [];
  let rows = [], t0 = 0, t1 = 1, cursorPct = null;
  let mode = 'monitor';

  function mount(host) {
    el = host;
    el.innerHTML = `
      <h2>① 场地 <span class="tag" id="a-watermark"></span></h2>
      <div class="grid2">
        <div class="pane">
          <h4>手机实时画面
            <span>
              <button class="mini" data-mode="monitor">监视道</button>
              <button class="mini" data-mode="annot">叠加图</button>
            </span></h4>
          <img id="a-mjpeg" alt="等待 MJPEG…" style="width:100%;border:1px solid var(--line);border-radius:4px;background:#0b0d12;min-height:180px">
          <div class="hint" id="a-mjpeg-note"></div>
          <div class="row">
            <button class="mini" id="a-preview">试解最新帧</button>
            <button class="mini" id="a-batch">复算本会话</button>
            <span class="hint" id="a-batch-state"></span>
          </div>
        </div>
        <div class="pane">
          <h4>场地俯视图
            <span><button class="mini" id="a-clear">清轨迹</button></span></h4>
          <canvas id="a-canvas"></canvas>
        </div>
      </div>
      <div class="pane" style="margin-top:10px">
        <h4>时间轴
          <span class="hint" id="a-time-label"></span></h4>
        <canvas id="a-time" style="cursor:ew-resize"></canvas>
      </div>
      <div class="pane" style="margin-top:10px">
        <h4>Δpos 读数</h4>
        <div id="a-delta" class="kv"></div>
      </div>`;
    cvArena = el.querySelector('#a-canvas');
    cvTime = el.querySelector('#a-time');
    img = el.querySelector('#a-mjpeg');
    /* `<img>` 拿到 502 只会显示破图、**一个字都不说**（反代在"连不上上游"时如实回 502）。
       不接住它，现场看到的就是"画面出不来"，而原因是上游没起 / 手机没在推帧 ——
       两者要在提示里分开（`camera.reachable` 就是那个分界）。 */
    img.addEventListener('error', function () {
      const n = el && el.querySelector('#a-mjpeg-note');
      if (n) n.textContent = '画面加载失败 · 到顶栏【相机】看服务是否可达';
    });
    img.addEventListener('load', function () {
      const n = el && el.querySelector('#a-mjpeg-note');
      if (n) n.textContent = '';
    });

    el.querySelectorAll('[data-mode]').forEach(function (b) {
      b.onclick = function () { setMode(b.dataset.mode); };
    });
    el.querySelector('#a-clear').onclick = function () {
      trackOnline = []; trackOffline = []; draw();
    };
    el.querySelector('#a-preview').onclick = function () { previewLatest(); };
    el.querySelector('#a-batch').onclick = function () { runBatch(); };
    cvTime.addEventListener('click', function (e) {
      const r = cvTime.getBoundingClientRect();
      cursorPct = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
      draw();
    });
    window.addEventListener('resize', draw);
    setMode('monitor');
    refreshArena();
    loadTimeline();
    loadTrack();
    return el;
  }

  function setMode(m) {
    mode = m;
    if (m === 'monitor') {
      img.src = '/api/v1/camera/mjpeg?t=' + Date.now();
      img.alt = '监视道（MJPEG）';
    } else {      const seq = lastSeq();
      img.src = '/api/v1/locate/annotated/' + seq + '.jpg?t=' + Date.now();
      img.alt = '叠加图';
    }
    el.querySelectorAll('[data-mode]').forEach(function (b) {
      b.classList.toggle('on', b.dataset.mode === m);
    });
  }

  function lastSeq() {
    const fw = live.vis;
    if (fw && fw._seq !== undefined && fw._seq !== null) return fw._seq;
    return 1;
  }

  function draw() {
    if (!cvArena) return;
    const a = live.arena || { grid: 8, calibrated: false };
    C.draw.arena(cvArena, {
      grid: a.grid || 8,
      fw: live.fw,
      vis: live.vis,
      trackOnline: trackOnline,
      trackOffline: trackOffline,
      watermark: a.calibrated ? null : '坐标未标定',
      playback: cursorPct === null ? null : fmtT(lerpTime()),
      height: Math.max(220, Math.min(430, cvArena.clientWidth))
    });
    drawTime();
    drawDelta();
    const wm = el.querySelector('#a-watermark');
    wm.className = 'tag ' + (a.calibrated ? 'ok' : 'warn');
    wm.textContent = a.calibrated ? '已标定' : '未标定';
  }

  function lerpTime() {
    return cursorPct === null ? null : t0 + (t1 - t0) * cursorPct;
  }
  function fmtT(ms) {
    if (!ms) return '—';
    const d = new Date(Number(ms));
    return d.toTimeString().slice(0, 8) + '.' + String(Math.round(ms % 1000)).padStart(3, '0');
  }

  function drawTime() {
    if (!cvTime) return;
    C.draw.timeline(cvTime, rows, t0, t1, lerpTime());
    el.querySelector('#a-time-label').textContent =
      rows.length + ' 条事件  ' + fmtT(t0) + ' → ' + fmtT(t1) +
      (cursorPct === null ? '' : '   当前 ' + fmtT(lerpTime()));
  }

  function drawDelta() {
    const d = live.delta;
    const box = el.querySelector('#a-delta');
    if (!d) { box.innerHTML = '<b>Δpos</b><span class="hint">无读数</span>'; return; }
    const dth = d.dtheta_deg === null || d.dtheta_deg === undefined ? '—' : d.dtheta_deg + '°';
    box.innerHTML = `
      <b>Δpos</b><span>${C.util.fmt(d.dx_cells)} , ${C.util.fmt(d.dy_cells)} 格`
      + (Math.abs(d.rms_cells) > 0.3 ? ' <span class="tag bad">超差</span>' : ' <span class="tag ok">合格</span>')
      + `</span>
      <b>Δθ</b><span>${dth}</span>
      <b>Δt</b><span>${d.dt_ms === null ? '—' : d.dt_ms + ' ms'}</span>
      <b>结论</b><span>${C.util.esc(d.note || '')}</span>`;
  }

  async function refreshArena() {
    const d = await C.guard(function () { return C.api('/api/v1/arena'); });
    if (!d) return;
    live.arena = d.arena;
    live.fw = d.fw_position;
    live.vis = d.vis;
    live.delta = d.delta;
    live.clock = d.clock;
    draw();
    C.renderContext();
  }

  async function loadTimeline() {
    const d = await C.guard(function () { return C.api('/api/v1/session/timeline?limit=20000'); });
    if (!d || !d.length) { rows = []; t0 = Date.now() - 1000; t1 = Date.now(); draw(); return; }
    rows = d;
    t0 = Number(d[0].t_pc_ms);
    t1 = Number(d[d.length - 1].t_pc_ms);
    if (t1 - t0 < 1000) t1 = t0 + 1000;
    draw();
  }

  async function loadTrack() {
    const d = await C.guard(function () { return C.api('/api/v1/locate/track'); });
    if (!d || !d.points) return;
    trackOffline = d.points.filter(function (p) { return p.valid && p.x !== null; })
      .map(function (p) { return [p.x, p.y]; });
    draw();
  }

  async function previewLatest() {
    const f = await C.guard(function () { return C.api('/api/v1/camera/frames?limit=1'); });
    if (!f || !f.count) { C.toast('当前会话无帧', 'warn'); return; }
    const seq = f.frames[f.frames.length - 1].seq;
    const d = await C.guard(function () {
      return C.api('/api/v1/locate/preview?seq=' + seq + '&session_id=' +
        encodeURIComponent(f.session_id));
    }, '试解');
    if (d) {
      C.toast((d.valid ? '有效' : '无效：' + (d.reason || '')) +
        '  x=' + C.util.fmt(d.x) + ' y=' + C.util.fmt(d.y), d.valid ? 'ok' : 'warn');
      if (mode === 'annot') img.src = '/api/v1/locate/annotated/' + seq + '.jpg?t=' + Date.now();
    }
  }

  async function runBatch() {
    const d = await C.guard(function () {
      return C.api('/api/v1/locate/run', { method: 'POST', body: {} });
    }, '复算');
    if (d) {
      C.toast('赛后批量已启动 ' + d.run_id, 'ok');
      el.querySelector('#a-batch-state').textContent = d.run_id + ' 运行中…';
    }
  }

  return {
    mount: mount,
    activate: function () { refreshArena(); loadTimeline(); loadTrack(); draw(); },

    onMsg: function (m) {
      if (m.type === 'locate.result') {
        live.vis = m;
        if (m.x !== null && m.valid) trackOnline.push([m.x, m.y]);
        if (trackOnline.length > 4000) trackOnline.splice(0, 1000);
        draw();
      } else if (m.type === 'mouse.event' && m.data && m.data.kind === 'map') {
        live.fw = [m.data.posX, m.data.posY];
        draw();
      } else if (m.type === 'mouse.telemetry') {
        // 固件自述位置：2.0.0 起只有一条统一帧，坐标就在里面（`posX`/`posY`）。
        // ⚠️ `gangle` 已随 fast/mid/slow 一起消失 —— 角度现在是帧里的 `gyroAngle`。
        const u = m.unified || {};
        if (u.posX !== undefined && u.posY !== undefined) {
          live.fw = [u.posX, u.posY, u.gyroAngle];
          draw();
        }
      } else if (m.type === 'session.phase') {
        loadTimeline();
      } else if (m.type === 'locate.batch') {
        const st = el.querySelector('#a-batch-state');
        if (st) st.textContent = m.id + ' ' + m.state + ' ' + m.done + '/' + m.total +
          ' 有效 ' + m.valid + ' 无效 ' + m.invalid;
        if (m.state === 'done' || m.state === 'aborted') { loadTrack(); C.toast('复算 ' + m.state + '：' + m.done + ' 帧', 'ok'); }
      } else if (m.type === 'arena.changed') {
        live.arena = m; draw();
      }
    },

    onHealth: function () { },

    context: function (box) {
      const a = live.arena || {};
      const cl = live.clock || {};
      const f = a.fw_to_arena || {};
      box.innerHTML = `
        <div class="card">
          <h3>场地标定</h3>
          <div class="kv">
            <b>网格</b><span>${a.grid} × ${a.grid}，格宽 ${a.pitch_mm} mm</span>
            <b>y_flip</b><span>${f.y_flip} ${a.calibrated ? '' : '<span class="tag warn">未实测</span>'}</span>
            <b>x_flip</b><span>${f.x_flip}</span>
            <b>相机高度</b><span>${a.camera_height_mm ? a.camera_height_mm + ' mm' : '—'}</span>
            <b>天底点</b><span>${(a.nadir_xy || []).join(', ')}</span>
          </div>
        </div>
        <div class="card">
          <h3>主时钟</h3>
          <div class="kv">
            <b>offset</b><span>${cl.offset_ms} ms</span>
            <b>不确定度</b><span>±${cl.uncertainty_ms} ms</span>
            <b>来源</b><span>${cl.source}</span>
            <b>方法</b><span>${cl.method || '—'}</span>
          </div>
        </div>`;
    }
  };
})();
