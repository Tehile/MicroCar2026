/* ⑦ 波形 —— 直接画**唯一那条统一帧**（上游 2.0.0 / 2.1.0）。

   ## 一次改版：从两块变一块
   旧版是两块：
     · 「实时 · 250 Hz」—— 走二进制帧的连续波形；
     · 「1 kHz dump」—— 用 `SCOPE` 在固件侧本地录制，停下后 `DUMP` 整块回传，
       再做两条游标 ΔA-B 与按 `motionTask` 的分段表。
   2.0.0 把 `SCOPE` / `DUMP` **整套删除**，理由写在设计方案 §6：每帧都是 45 B 全量快照，
   "实验期间的数据"本来就在主机内存里，不需要"停车-回传"那一趟。所以：
     · 「1 kHz dump」这一块**没有对应物了**，整块删除（不是改名）；
     · 游标 ΔA-B 的用法**保留**，只是改成在【冻结】后的实时画布上点两下 ——
       冻结的窗口就是本层环形缓冲里那段历史（默认 10 s，见 `mouse.wave_window_ms`）。

   ## 一次修正：帧率不是 217 Hz（2.1.0「拉取模式」）
   2.0.0 的"固件自时钟全速推"把蓝牙透传模块缓冲灌死了（那条链路只有 ≈100~200 B/s），
   2.1.0 改成上位机逐条拉取 ⇒ 帧率 = **1/RTT**，真机 **4.4~6.5 Hz**。
   所以这一页**不许再写 ≈217 Hz**（差约 50 倍），标题只写"统一帧 · 实测帧率"，
   真实节拍在信息行里按实测显示。

   ⚠️ 判别"这块数据是什么"的唯一依据是**帧里的通道名**（`f.meta.channels`）——
   不要在这里写死通道清单：上游加字段时画面会自动多一条，而不是静默漏掉。 */

C.views.scope = (function () {
  let el, cvLive;
  let live = null;            // {channels, shape, floats, t0_ms, dt_ms}
  let frozenFrame = null;     // 冻结那一刻的快照（游标就打在它上面）
  let onCh = {};              // 通道开关
  let frozen = false;
  let curA = null, curB = null;

  function mount(host) {
    el = host;
    el.innerHTML = `
      <h2>⑦ 波形</h2>
      <div class="gatehint" id="s-link-hint"></div>
      <div class="pane">
        <h4>实时 · 统一帧
          <span>
            <button class="mini" id="s-freeze">冻结</button>
            <button class="mini" id="s-clear">清空通道</button>
          </span></h4>
        <canvas id="s-live"></canvas>
        <div class="hint" id="s-chans"></div>
        <div class="hint" id="s-info"></div>
      </div>
      <div class="pane" style="margin-top:10px">
        <h4>游标测量 <span class="hint">先【冻结】，再在图上点两下（A → B）</span>
          <span><button class="mini" id="s-clear-cur">清游标</button></span></h4>
        <div class="hint" id="s-cur"
          style="white-space:pre-wrap;font-family:var(--mono);font-size:11px"></div>
      </div>`;
    cvLive = el.querySelector('#s-live');
    el.querySelector('#s-freeze').onclick = function () {
      frozen = !frozen;
      // 冻结 = **钉住当前这一帧**并停止跟随。游标必须打在静止的数据上，
      // 否则点完 A、数据又走了，两次读数不来自同一段窗口 —— 那个 Δ 是假的。
      if (frozen) { frozenFrame = live; this.textContent = '已冻结'; }
      else { frozenFrame = null; curA = null; curB = null; this.textContent = '冻结'; }
      renderInfo(); drawCur();
    };
    el.querySelector('#s-clear').onclick = function () { onCh = {}; renderChans(); draw(); };
    el.querySelector('#s-clear-cur').onclick = function () { curA = null; curB = null; drawCur(); };
    cvLive.addEventListener('click', function (e) {
      const src = frozenFrame;
      if (!src) return;
      const r = cvLive.getBoundingClientRect();
      const pad = { l: 44, r: 8 };
      const pct = Math.min(1, Math.max(0, (e.clientX - r.left - pad.l) / (r.width - pad.l - pad.r)));
      if (curA === null || (curA !== null && curB !== null)) { curA = pct; curB = null; }
      else curB = pct;
      drawCur();
    });
    window.addEventListener('resize', draw);
    renderChans();
    return el;
  }

  function cur() { return frozenFrame || live; }

  function renderChans() {
    const box = el.querySelector('#s-chans');
    if (!box) return;
    if (!live) { box.textContent = '无波形数据'; return; }
    box.innerHTML = '';
    live.channels.forEach(function (c, i) {
      const b = document.createElement('button');
      b.className = 'mini';
      b.style.marginRight = '4px';
      const on = onCh[c] === undefined ? i < 4 : onCh[c];
      b.style.opacity = on ? '1' : '.45';
      b.textContent = c;
      b.onclick = function () {
        onCh[c] = !(onCh[c] === undefined ? i < 4 : onCh[c]);
        renderChans(); draw();
      };
      box.appendChild(b);
    });
    box.appendChild(document.createTextNode(
      '  ' + live.shape[0] + ' 点 × ' + live.shape[1] + ' 通道'));
  }

  /* 信息行的三个数字分别回答不同问题，所以都要给：
     点数×通道数 = 这段窗口有多大；t0/Δ = 它是哪一段（设备时基）；dt = 真实节拍。
     ⚠️ `dt_ms` 是**从时间列算出来的**（上游 `history_dict` 的 `t` 列），
     不是写死的额定值 —— 2.1.0 起帧率由链路往返决定（真机 4.4~6.5 Hz，即间隔 150~230 ms），
     按 1/217 硬算会让 Δt 偏小约 50 倍。 */
  function renderInfo() {
    const bar = el.querySelector('#s-info');
    if (!bar) return;
    const d = cur();
    if (!d) { bar.textContent = '还没收到波形（先连鼠；遥测拉取要开着）'; return; }
    const t = C.state.telemetry || {};
    const r = (t.rate || {}).unified || {};
    const hz = (r.hz == null) ? '—' : (C.util.fmt(r.hz, 1) + ' Hz');
    bar.textContent = d.shape[0] + ' 点 × ' + d.shape[1] + ' 通道 · '
      + 't0=' + C.util.fmt(d.t0_ms / 1000, 3) + ' s（设备时基）· '
      + 'Δt=' + C.util.fmt(d.dt_ms, 2) + ' ms · 实测 ' + hz
      + (frozen ? ' · 已冻结（停在这一点）' : '');
  }

  function columnOf(f, i, N, Cc) {
    const out = new Float32Array(N);
    for (let k = 0; k < N; k++) out[k] = f[k * Cc + i];
    return out;
  }

  function seriesOf(d) {
    if (!d) return [];
    const N = d.shape[0], Cc = d.shape[1];
    return d.channels.map(function (c, i) {
      const on = frozen ? true : (onCh[c] === undefined ? i < 4 : onCh[c]);
      return { name: c, on: on, color: C.draw.colors[i % C.draw.colors.length],
               data: on ? columnOf(d.floats, i, N, Cc) : null };
    });
  }

  function draw() {
    const d = cur();
    C.draw.waves(cvLive, seriesOf(d), {
      height: 300,
      label: frozen ? '统一帧（已冻结）' : '统一帧 · 实时',
      cursorA: curA, cursorB: curB,
      empty: '无波形数据',
    });
    renderInfo();
    drawCur();
  }

  /* 游标读数：Δt + 每个通道的 Δ。这里只报"两个游标之间的差"，
     不报绝对量 —— 绝对量归「实时参数」那一页（它一直在刷新，本来就更适合看当前值）。

     ⚠️ 起点按**小者**算（A 可以点在 B 右边），否则 Δ 会带着一个负数符号到处跑。 */
  function drawCur() {
    const box = el.querySelector('#s-cur');
    const d = frozenFrame;
    if (!box) return;
    if (!d) { box.textContent = '冻结之后才能打游标（否则数据在动，两次读数不来自同一段窗口）。'; return; }
    if (curA === null || curB === null) {
      box.textContent = '已冻结 ' + d.shape[0] + ' 点。在图上点两下设定 A / B。';
      return;
    }
    const N = d.shape[0], Cc = d.shape[1];
    const ia = Math.round(curA * (N - 1)), ib = Math.round(curB * (N - 1));
    const a = Math.min(ia, ib), b = Math.max(ia, ib);
    const dt = d.dt_ms || 0;
    const lines = ['A → B：第 ' + a + ' → ' + b + ' 点，ΔN=' + (b - a)
      + ' 点，Δt=' + (dt * (b - a)).toFixed(1) + ' ms'];
    if (d.t0_ms) {
      lines.push('设备时基 ' + (d.t0_ms + dt * a).toFixed(0) + ' → '
        + (d.t0_ms + dt * b).toFixed(0) + ' ms');
    }
    d.channels.forEach(function (c, i) {
      const v = d.floats[b * Cc + i] - d.floats[a * Cc + i];
      lines.push('  Δ' + c + '=' + v.toFixed(3));
    });
    box.textContent = lines.join('\n');
  }

  return {
    mount: mount,
    activate: function () { draw(); },

    onMsg: function (m) {
      if (m.type === 'mouse.telemetry') draw();   // 帧率那一行跟着它刷新
    },

    bin: {
      /* `mouse.wave` 由服务端 `sampler_loop` 推来：**本层环形缓冲里的整段历史**
         （默认 10 s），不是"这一瞬间的一个采样点"。所以画面一上来就是满的。
         冻结时不再覆盖 —— 冻结的是**数据**，不只是画面。 */
      'mouse.wave': function (f) {
        if (frozen) return;
        const prev = live ? live.channels.join(',') : '';
        live = { channels: f.meta.channels, shape: f.meta.shape,
                 floats: f.floats, t0_ms: f.meta.t0_ms, dt_ms: f.meta.dt_ms };
        if (live.channels.join(',') !== prev) renderChans();
        draw();
      },
    },

    /* 波形页只有"要设备在推流"这一个前提了（dump 那条路已随 2.0.0 删除）。
       不门控的：#s-clear / #s-clear-cur（清的是本地显示）、#s-freeze（冻的是本地缓冲）。 */
    onHealth: function () {
      const why = C.needMouse('读波形');
      ['s-live'].forEach(function (id) {
        C.gate(el.querySelector('#' + id), why);
      });
      const h2 = el.querySelector('#s-link-hint');
      if (h2) h2.textContent = why || '';
    },

    context: function (box) {
      box.innerHTML = '';
    }
  };
})();
