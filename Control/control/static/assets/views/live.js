/* ═══ 实时状态：帧率（批式 n）+ 4 灯 + 统一帧通道 + 手动运动 ═══════════ */
(function () {
  const S = C.S, U = C.util;
  const CH = C.channels;

  C.VIEWS.live = {
    eyebrow: '小车控制 · 统一帧 v9',
    title: '实时状态',
    sub: '',

    actions() {
      const cls = S.running ? 'run' : (S.estop ? 'bad' : (S.health.mouse.connected ? 'ok' : 'idle'));
      const txt = S.running ? '运行 ' + (CH.tasks[S.tel.mazeTask] || S.tel.mazeTask)
        : S.estop ? '急停锁存'
        : S.health.mouse.connected ? '待机' : '未连接';
      return '<span class="pill ' + cls + '"><i></i>' + txt + '</span>' +
        '<button class="btn btn-sm" data-act="tare">' + C.icon('refresh') + 'TARE</button>';
    },

    mount(root, actions) {
      this._root = root;
      root.innerHTML =
        '<div id="lv-banners"></div>' +

        '<div class="lv-top">' +
          '<section class="card">' +
            '<div class="card-head"><h2>实时帧率</h2>' +
              '<div class="card-head-r"><span class="tag blue">批式</span>' +
                '<span class="hint mono" id="lv-age"></span></div></div>' +
            '<div class="card-body">' +
              '<div class="rate-row">' +
                '<div class="rate-big"><b id="lv-rate">—</b><span>Hz</span></div>' +
                '<div class="rate-delta" id="lv-delta">—</div>' +
                '<div class="rate-spark"><canvas id="lv-spark"></canvas></div>' +
              '</div>' +
              '<div class="pull-row">' +
                '<span class="pu-k">拉取</span>' +
                '<span class="tag">批式</span>' +
                '<label class="pu-n">N <input class="input sm" id="lv-n" value="1" inputmode="numeric"></label>' +
                '<button class="btn btn-sm" id="lv-n-apply">应用</button>' +
                '<span class="hint mono" id="lv-n-note"></span>' +
              '</div>' +
              '<table class="tbl rate-tbl"><thead><tr>' +
                '<th style="width:44px">#</th><th style="width:64px">帧/批</th>' +
                '<th style="width:86px">往返 ms</th><th style="width:86px">帧率 Hz</th>' +
                '<th style="width:64px">Δ</th></tr></thead>' +
                '<tbody id="lv-rate-rows"></tbody></table>' +
            '</div>' +
          '</section>' +

          '<section class="card">' +
            '<div class="card-head"><h2>状态行 · 4 灯</h2>' +
              '<div class="card-head-r"><span class="pill mono sm" id="lv-ir">—</span></div></div>' +
            '<div class="card-body">' +
              '<div class="led-strip" id="lv-leds"></div>' +
              '<div class="led-note" id="lv-led-note"></div>' +
            '</div>' +
          '</section>' +
        '</div>' +

        '<div class="sec-title">统一帧通道 <span class="mono" style="letter-spacing:0">· 33</span></div>' +
        '<div class="grid g3" id="lv-ch"></div>' +

        '<section class="card" style="margin-top:12px">' +
          '<div class="card-head"><h2>手动运动</h2>' +
            '<div class="card-head-r"><span class="hint mono" id="lv-mm">未下发</span></div></div>' +
          '<div class="card-body">' +
            '<div class="grid g2" style="gap:20px;align-items:center">' +
              '<div class="dpad" id="lv-dpad">' +
                '<span></span>' +
                '<button data-dir="fwd" title="前进">' + C.icon('arrowUp') + '</button><span></span>' +
                '<button data-dir="left" title="左转">' + C.icon('rotateL') + '</button>' +
                '<div class="d-center" id="lv-dname">前进</div>' +
                '<button data-dir="right" title="右转">' + C.icon('rotateR') + '</button>' +
                '<span></span>' +
                '<button data-dir="back" title="后退">' + C.icon('arrowDown') + '</button><span></span>' +
              '</div>' +
              '<div class="stack" style="gap:10px">' +
                '<div class="row wrap"><span class="hint" style="width:64px">速度 cm/s</span>' +
                  '<input class="input sm" id="lv-speed" value="40" style="width:82px">' +
                  '<span class="hint" style="width:44px">持续 s</span>' +
                  '<input class="input sm" id="lv-sec" value="1.0" style="width:70px"></div>' +
                '<div class="row wrap">' +
                  '<button class="btn btn-primary" id="lv-go">' + C.icon('play') + '执行</button>' +
                  '<button class="btn" id="lv-stop">停止</button>' +
                  '<button class="btn btn-danger" id="lv-estop">急停</button></div>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</section>';

      root.querySelector('#lv-ch').innerHTML = CH.groups.map(g =>
        '<section class="card"><div class="card-head"><h2>' + g.name + '</h2>' +
        '<div class="card-head-r"><span class="hint mono">' + g.keys.length + '</span></div></div>' +
        '<div class="card-body" style="padding-top:4px"><dl class="kv compact">' + g.keys.map(k =>
          '<div><dt>' + k[1] + ' <code>' + k[0] + '</code></dt>' +
          '<dd data-k="' + k[0] + '">—</dd></div>').join('') + '</dl></div></section>').join('');

      const tare = actions && actions.querySelector('[data-act="tare"]');
      if (tare) tare.onclick = () => C.act('tare');

      const nin = root.querySelector('#lv-n');
      const apply = () => {
        const v = C.pullBatch(nin.value);
        nin.value = v;
        this.paint(true);
      };
      root.querySelector('#lv-n-apply').onclick = apply;
      nin.onkeydown = e => { if (e.key === 'Enter') apply(); };

      /* 手动运动 */
      let dir = 'fwd';
      const dn = root.querySelector('#lv-dname');
      root.querySelectorAll('#lv-dpad [data-dir]').forEach(b => {
        b.onclick = function () {
          dir = b.dataset.dir;
          root.querySelectorAll('#lv-dpad [data-dir]').forEach(x => x.classList.toggle('on', x === b));
          dn.textContent = { fwd: '前进', back: '后退', left: '左转', right: '右转' }[dir];
        };
      });
      root.querySelector('#lv-go').onclick = () => {
        if (!S.health.mouse.connected) { C.toast('warn', '未连接电脑鼠', '顶栏【电脑鼠】'); return; }
        const v = Number(root.querySelector('#lv-speed').value) || 40;
        const ms = Math.round((Number(root.querySelector('#lv-sec').value) || 1) * 1000);
        const body = dir === 'fwd' ? { kind: 'run', sp_l: v, sp_r: v, ms: ms }
          : dir === 'back' ? { kind: 'run', sp_l: -v, sp_r: -v, ms: ms }
          : dir === 'left' ? { kind: 'rotate', pwm: v, ms: ms }
          : { kind: 'rotate', pwm: -v, ms: ms };
        root.querySelector('#lv-mm').textContent = 'MOVE ' + JSON.stringify(body);
        C.log('mouse', 'info', '→ MOVE ' + JSON.stringify(body));
        C.toast('', '运动指令已下发', dir + ' · ' + v + ' cm/s · ' + ms + ' ms');
      };
      root.querySelector('#lv-stop').onclick = () => {
        root.querySelector('#lv-mm').textContent = 'STOP';
        C.act('raw', 'STOP');
      };
      root.querySelector('#lv-estop').onclick = () => C.act('estop');

      this.paint(true);
    },

    paint(first) {
      const root = this._root;
      if (!root || !root.querySelector('#lv-ch')) return;
      const t = S.tel;

      /* 帧率 */
      const rateEl = root.querySelector('#lv-rate');
      if (rateEl) rateEl.textContent = U.fmt(t.rate, 1);
      const log = S.rateLog;
      const last = log[log.length - 1], prev = log[log.length - 2];
      const dEl = root.querySelector('#lv-delta');
      if (dEl) {
        if (!last || !prev) { dEl.className = 'rate-delta'; dEl.textContent = '—'; }
        else {
          const d = last.hz - prev.hz;
          dEl.className = 'rate-delta ' + (d > .05 ? 'up' : d < -.05 ? 'down' : 'flat');
          dEl.textContent = (d > 0 ? '▲ +' : d < 0 ? '▼ ' : '· ') + U.fmt(Math.abs(d), 1);
        }
      }
      const ageEl = root.querySelector('#lv-age');
      if (ageEl) ageEl.textContent = '最新一帧 ' + U.fmt(t.ageMs, 0) + ' ms 前';
      const noteEl = root.querySelector('#lv-n-note');
      if (noteEl) {
        noteEl.textContent = 'n=' + S.pullN + ' · 往返 ' + U.fmt(t.batchMs, 0) + ' ms · ' +
          U.fmt(t.rate * 30, 0) + ' B/s';
      }
      const nin = root.querySelector('#lv-n');
      if (nin && document.activeElement !== nin) nin.value = S.pullN;

      const rows = log.slice().reverse().map((r, i) => {
        const j = log.length - 1 - i;
        const p = log[j - 1];
        const d = p ? r.hz - p.hz : null;
        return '<tr' + (i === 0 ? ' class="sel"' : '') + '>' +
          '<td class="num dim">' + String(r.seq).padStart(3, '0') + '</td>' +
          '<td class="num">' + r.n + '</td>' +
          '<td class="num dim">' + r.ms + '</td>' +
          '<td class="num" style="color:var(--ink);font-weight:600">' + U.fmt(r.hz, 1) + '</td>' +
          '<td class="num ' + (d === null ? 'dim' : d > 0 ? 'up' : d < 0 ? 'down' : 'dim') + '">' +
            (d === null ? '—' : (d > 0 ? '+' : '') + U.fmt(d, 1)) + '</td>' +
          '</tr>';
      }).join('');
      root.querySelector('#lv-rate-rows').innerHTML = rows ||
        '<tr><td colspan="5"><div class="empty"><b>等待第一批</b></div></td></tr>';

      const sp = root.querySelector('#lv-spark');
      if (sp && sp.clientWidth) C.draw.spark(sp, [{ data: S.hist.rate, color: '#2B4FF2' }], {});

      /* 4 灯：只画后端判定的三态 */
      const leds = S.leds;
      root.querySelector('#lv-leds').innerHTML = ['led1', 'led2', 'led3', 'led4'].map((k, i) => {
        const v = leds[k];
        const cls = v === true ? 'on' : v === false ? 'off' : 'unknown';
        const txt = v === true ? '亮' : v === false ? '灭' : '未知';
        return '<div class="led-s ' + cls + '">' +
          '<span class="l-name">LED' + (i + 1) + '</span>' +
          '<span class="l-state">' + txt + '</span></div>';
      }).join('');

      const irOn = !!(t.flags & 16);
      const ir = root.querySelector('#lv-ir');
      if (ir) {
        ir.textContent = 'IR ' + (irOn ? '开' : '关') + ' 0x10';
        ir.className = 'pill mono sm ' + (irOn ? 'ok' : 'idle');
      }
      const note = root.querySelector('#lv-led-note');
      if (note) {
        const unknown = ['led1', 'led2', 'led3', 'led4'].some(k => leds[k] === null);
        note.innerHTML = unknown
          ? '<span class="sq"></span>' + (!S.health.mouse.connected ? '未连接 ⇒ 4 灯未知'
            : 'fw ' + S.health.mouse.fw + ' < 311 ⇒ 4 灯未知') : '';
        note.style.display = unknown ? '' : 'none';
      }

      root.querySelectorAll('[data-k]').forEach(d => {
        d.textContent = CH.text(d.dataset.k, t[d.dataset.k]);
      });

      /* 提示条：只说发生了什么 */
      const arr = [];
      if (S.estop) arr.push(banner('bad', '急停锁存', '电机已断电'));
      if (!S.health.mouse.connected) arr.push(banner('warn', '未连接电脑鼠', '读数为最后一帧'));
      if (S.health.mouse.connected && !S.running && t.ageMs > 1500) {
        arr.push(banner('', '无数据', '最新一帧 ' + U.fmt(t.ageMs / 1000, 1) + ' s 前'));
      }
      root.querySelector('#lv-banners').innerHTML = arr.join('');
    },

    onTick() { this.paint(); },
    onState() { this.paint(); },
  };

  function banner(kind, title, body) {
    return '<div class="banner ' + kind + '" style="margin-bottom:12px">' + C.icon('alert') +
      '<div class="b-body"><b>' + title + '</b> ' + body + '</div></div>';
  }
})();
