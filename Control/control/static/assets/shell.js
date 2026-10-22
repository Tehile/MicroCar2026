/* ═══════════════════════════════════════════════════════════════════════════
   shell.js —— 外壳：左轨导航 / 顶栏两灯 / 页面容器 / 日志坞 / 设备抽屉
   改版二十四：顶栏四灯 → 两灯（电脑鼠 + 时钟）；相机与场地定位整块下线。
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  const S = C.S, U = C.util;
  const SRC_NAME = { mouse: '电脑鼠', video: '视频', control: '本层' };
  const LVL_NAME = { info: '普通', warn: '警告', error: '错误' };
  const DEV = {
    mouse: { name: '电脑鼠', eyebrow: '设备 · 串口链路' },
    clock: { name: '时间基', eyebrow: '设备 · 帧时刻' },
  };

  /* ── 左轨导航 ─────────────────────────────────────────────────────────── */
  function renderNav() {
    const box = document.getElementById('nav');
    box.innerHTML = C.NAV.map(g =>
      '<div class="nav-group">' +
      (g.label ? '<div class="nav-label">' + g.label + '</div>' : '') +
      g.items.map(it =>
        '<button class="nav-item' + (S.route === it.id ? ' on' : '') + '" data-route="' + it.id + '" title="' + it.name + '">' +
        C.icon(it.icon) + '<span>' + it.name + '</span>' +
        '</button>').join('') +
      '</div>').join('');
    box.querySelectorAll('[data-route]').forEach(b => {
      b.onclick = function () { C.go(b.dataset.route); };
    });
  }

  /* ── 顶栏设备片：只有两路 ─────────────────────────────────────────────── */
  function devState(kind) {
    const h = S.health;
    if (kind === 'mouse') {
      if (!h.mouse.available) return { cls: 'bad', txt: '模块缺失' };
      if (h.mouse.connected) return { cls: 'ok', txt: h.mouse.port || '已连接' };
      if (h.mouse.connect_in_flight) return { cls: 'warn', txt: '连接未返回' };
      return { cls: 'warn', txt: '未连接' };
    }
    const cl = h.clock, ts = h.timesync;
    if (!h.mouse.connected) return { cls: 'unknown', txt: '判不了' };
    if (cl.calibrated && ts.armed) return { cls: 'ok', txt: '±' + cl.uncertaintyMs + ' ms' };
    if (cl.calibrated) return { cls: 'warn', txt: '未发车' };
    return { cls: 'warn', txt: '未标定' };
  }

  function renderDevices() {
    const box = document.getElementById('devices');
    box.innerHTML = Object.keys(DEV).map(k => {
      const st = devState(k);
      return '<button class="devchip ' + st.cls + '" data-dev="' + k + '" title="' + DEV[k].name + ' · ' + st.txt + '">' +
        '<span class="dot"></span><b>' + DEV[k].name + '</b><em>' + U.esc(st.txt) + '</em></button>';
    }).join('');
    box.querySelectorAll('[data-dev]').forEach(b => {
      b.onclick = function () { openDev(b.dataset.dev); };
    });
  }

  /* ── 顶栏右侧 ─────────────────────────────────────────────────────────── */
  function renderTop() {
    const rec = document.getElementById('btn-record');
    rec.className = 'btn' + (S.session.active ? ' btn-danger' : '');
    rec.querySelector('span').textContent = S.session.active ? '停止记录' : '开始记录';
    rec.disabled = !S.health.mouse.connected && !S.session.active;
    rec.title = rec.disabled ? '只有电脑鼠这一路拦记录 · 先连串口' : '视频是离线批处理，不拦记录';

    const es = document.getElementById('btn-estop');
    es.classList.toggle('latched', S.estop);
    es.querySelector('span').textContent = S.estop ? '解锁急停' : '急停';

    const crumb = document.getElementById('crumb');
    const nav = C.NAV.reduce((a, g) => a.concat(g.items), []).find(i => i.id === S.route);
    const group = C.NAV.find(g => g.items.some(i => i.id === S.route));
    crumb.innerHTML = (group && group.label ? group.label + ' <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 6l6 6-6 6"/></svg> ' : '') +
      '<b>' + (nav ? nav.name : '总览') + '</b>';
  }

  /* ── 页面 ─────────────────────────────────────────────────────────────── */
  function renderPage() {
    const main = document.getElementById('main');
    const v = C.VIEWS[S.route] || C.VIEWS.overview;
    main.scrollTop = 0;
    main.innerHTML =
      '<div class="page">' +
        '<header class="page-head">' +
          '<div>' +
            '<div class="eyebrow">' + U.esc(v.eyebrow || '') + '</div>' +
            '<h1>' + U.esc(v.title || '') + '</h1>' +
            (v.sub ? '<div class="page-sub">' + v.sub + '</div>' : '') +
          '</div>' +
          '<div class="page-actions" id="page-actions">' + (v.actions ? v.actions() : '') + '</div>' +
        '</header>' +
        '<div id="view"></div>' +
      '</div>';
    try { v.mount(document.getElementById('view'), document.getElementById('page-actions')); }
    catch (e) {
      console.error(e);
      document.getElementById('view').innerHTML = '<div class="banner bad">' + C.icon('alert') +
        '<div class="b-body"><b>这一页渲染失败</b>' + U.esc(e.message) + '</div></div>';
    }
    if (v.onTick) { try { v.onTick(); } catch (e) { console.error(e); } }
  }

  function refreshChrome() {
    renderDevices();
    renderTop();
    renderNav();
    const v = C.VIEWS[S.route];
    if (v && v.onState) { try { v.onState(); } catch (e) { console.error(e); } }
    renderLog();
  }

  /* ── 日志坞 ───────────────────────────────────────────────────────────── */
  let dockOpen = false;
  function initDock() {
    const src = document.getElementById('log-src');
    src.innerHTML = '<button data-all="1">全部来源</button>' +
      Object.keys(SRC_NAME).map(k => '<button data-src="' + k + '">' + SRC_NAME[k] + '</button>').join('');
    const lvl = document.getElementById('log-lvl');
    lvl.innerHTML = '<button data-all="1">全部级别</button>' +
      Object.keys(LVL_NAME).map(k => '<button data-lvl="' + k + '">' + LVL_NAME[k] + '</button>').join('');

    src.querySelectorAll('button').forEach(b => {
      b.onclick = function () {
        S.logFilter.src = b.dataset.all ? null : b.dataset.src; paintSeg(); renderLog();
      };
    });
    lvl.querySelectorAll('button').forEach(b => {
      b.onclick = function () {
        S.logFilter.lvl = b.dataset.all ? null : b.dataset.lvl; paintSeg(); renderLog();
      };
    });
    document.getElementById('log-q').oninput = function (e) {
      S.logFilter.q = e.target.value.trim(); renderLog();
    };
    document.getElementById('log-clear').onclick = function () {
      S.log.length = 0; renderLog(); C.toast('', '日志已清空', '只清界面，不影响记录');
    };
    document.getElementById('dock-toggle').onclick = function () {
      dockOpen = !dockOpen;
      document.getElementById('dock').classList.toggle('open', dockOpen);
      if (dockOpen) renderLog();
    };
  }

  function paintSeg() {
    document.querySelectorAll('#log-src button').forEach(b => {
      b.classList.toggle('on', b.dataset.all ? !S.logFilter.src : S.logFilter.src === b.dataset.src);
    });
    document.querySelectorAll('#log-lvl button').forEach(b => {
      b.classList.toggle('on', b.dataset.all ? !S.logFilter.lvl : S.logFilter.lvl === b.dataset.lvl);
    });
  }

  function logRows() {
    const f = S.logFilter;
    return S.log.filter(r =>
      (!f.src || r.src === f.src) &&
      (!f.lvl || r.level === f.lvl) &&
      (!f.q || (r.text + SRC_NAME[r.src]).toLowerCase().indexOf(f.q.toLowerCase()) >= 0));
  }

  function renderLog() {
    const rows = logRows();
    const box = document.getElementById('log');
    if (box) {
      box.innerHTML = rows.length
        ? rows.slice(-300).map(r =>
            '<div class="log-row ' + r.level + '">' +
            '<span class="t">' + U.hhmmss(r.t) + '</span>' +
            '<span class="s">' + (SRC_NAME[r.src] || r.src) + '</span>' +
            '<span class="m">' + U.esc(r.text) + '</span></div>').join('')
        : '<div class="log-empty">没有符合筛选的条目</div>';
      box.scrollTop = box.scrollHeight;
    }
    const last = S.log[S.log.length - 1];
    const lastEl = document.getElementById('dock-last');
    if (lastEl) {
      lastEl.textContent = last ? ('[' + U.hhmmss(last.t) + '] ' + SRC_NAME[last.src] + ' · ' + last.text) : '';
    }
    const cnt = document.getElementById('dock-count');
    if (cnt) cnt.textContent = S.log.length ? (rows.length + ' / ' + S.log.length + ' 条') : '空';
  }

  /* ── 设备抽屉 ─────────────────────────────────────────────────────────── */
  const kvRows = rows => '<dl class="kv">' + rows.map(r =>
    '<div><dt>' + U.esc(r[0]) + '</dt><dd class="' + (r[2] || '') + '">' + U.esc(r[1]) + '</dd></div>').join('') + '</dl>';

  function devLog(kind) {
    const src = kind === 'clock' ? 'mouse' : 'mouse';
    const rows = S.log.filter(r => r.src === src).slice(-4).reverse();
    return '<div class="sec-title">这台设备最近的事件</div>' +
      (rows.length ? '<div class="evt-list">' + rows.map(function (r) {
        const cls = r.level === 'error' ? 'bad' : r.level === 'warn' ? 'warn' : '';
        return '<div class="evt"><span class="evt-t">' + U.hhmmss(r.t) + '</span>' +
          '<span class="tag ' + cls + '">' + (r.level === 'warn' ? '警告' : r.level === 'error' ? '故障' : '信息') + '</span>' +
          '<span class="evt-x">' + U.esc(r.text) + '</span></div>';
      }).join('') + '</div>'
        : '<div class="hint">还没有事件。</div>');
  }

  function openDev(kind) {
    if (!DEV[kind]) kind = 'mouse';
    document.body.classList.add('drawer-open');
    const h = S.health;
    document.getElementById('dr-eyebrow').textContent = DEV[kind].eyebrow;
    document.getElementById('dr-title').textContent = DEV[kind].name;
    const body = document.getElementById('dr-body');
    let html = '';

    if (kind === 'mouse') {
      const m = h.mouse;
      html =
        '<div class="sec-title">链路</div>' +
        kvRows([
          ['状态', m.connected ? '已连接' : '未连接', m.connected ? 'ok' : 'warn'],
          ['端口', m.port || '—'],
          ['固件', m.fw + '（' + (m.fwInt >= 311 ? '4 灯可读' : '4 灯未知') + '）'],
          ['协议', 'v' + m.proto + ' · 30 B 帧'],
          ['通道', String(m.caps.unified_channels) + ' 个'],
          ['链路', m.link || '—'],
          ['最近错误', m.detail || '—', 'dim'],
        ]) +
        '<div class="sec-title">可连接目标</div>' +
        ['COM7 · USB-SERIAL CH340', 'COM8 · CP2102', 'tcp://127.0.0.1:5555 · 模拟器'].map((t, i) =>
          '<button class="port-row' + (i === 0 ? ' cur' : '') + '" data-port="' + t.split(' ')[0] + '">' +
          '<span class="mono" style="font-size:11px">' + U.esc(t) + '</span>' +
          (i === 0 ? '<span class="tag ok" style="margin-left:auto">当前</span>' : '<span class="tag" style="margin-left:auto">选</span>') +
          '</button>').join('') +
        '<div class="row" style="margin-top:10px"><input class="input" id="dr-manual" placeholder="COM11 / tcp://…" style="flex:1">' +
        '<button class="btn btn-primary" id="dr-connect">连接</button></div>' +
        '<div class="row" style="margin-top:8px">' +
        '<button class="btn btn-sm" id="dr-disconnect">断开</button>' +
        '<button class="btn btn-sm" id="dr-rescue">软复位</button>' +
        '<button class="btn btn-sm btn-ghost">刷新端口</button></div>' +
        '<div class="banner" style="margin-top:12px">' + C.icon('alert') +
        '<div class="b-body">整机软复位 · 约 3 s 出首帧' +
        '<span class="b-next">⇒ 发车前放回起点角。</span></div></div>';
    } else {
      const cl = h.clock, ts = h.timesync;
      html =
        '<div class="sec-title">设备时基</div>' +
        kvRows([
          ['状态', !h.mouse.connected ? '判不了（没连接）' : (cl.calibrated ? '已标定' : '未标定'),
            !h.mouse.connected ? 'dim' : (cl.calibrated ? 'ok' : 'warn')],
          ['偏移', (cl.offsetMs > 0 ? '+' : '') + U.fmt(cl.offsetMs, 1) + ' ms'],
          ['不确定度', '±' + U.fmt(cl.uncertaintyMs, 1) + ' ms'],
          ['方法', cl.method + ' · ' + cl.samples + ' 样本'],
          ['发车 ARM', ts.armed ? ('已置位 @' + ts.armUpMs + ' ms') : '未置位'],
          ['漂移', U.fmt(ts.driftPpm, 1) + ' ppm'],
        ]) +
        '<div class="sec-title">对齐口径</div>' +
        '<div class="banner">' + C.icon('alert') +
        '<div class="b-body">未标定时记录仍可开，时间列标 <code>assumed</code>。</div></div>' +
        '<div class="row" style="margin-top:12px">' +
        '<button class="btn btn-primary btn-sm" id="dr-fit">自动拟合</button>' +
        '<button class="btn btn-sm">记录同步点</button>' +
        '<button class="btn btn-sm btn-ghost">刷新</button></div>';
    }
    body.innerHTML = html + devLog(kind);

    const q = id => body.querySelector('#' + id);
    if (kind === 'mouse') {
      body.querySelectorAll('[data-port]').forEach(p => {
        p.onclick = function () {
          body.querySelectorAll('[data-port]').forEach(x => x.classList.remove('cur'));
          p.classList.add('cur');
          q('dr-manual').value = p.dataset.port;
        };
      });
      const b = q('dr-connect');
      if (b) b.onclick = function () {
        const val = (q('dr-manual').value || 'COM7').trim();
        h.mouse.connected = true; h.mouse.port = val;
        C.log('mouse', 'info', '→ 连接 ' + val + ' · fw 2.13.0 · proto 9');
        C.toast('ok', '已连接', val + ' · proto 9 · 状态 WAIT');
        S.readiness[0].ok = true; S.readiness[0].state = val + ' · fw 2.13.0';
        C.recomputeLeds();
        refreshChrome();
      };
      const d = q('dr-disconnect');
      if (d) d.onclick = function () {
        h.mouse.connected = false; h.mouse.port = '';
        S.running = false; S.tel.mazeTask = 0;
        S.readiness[0].ok = false; S.readiness[0].state = '未连接 · 串口为空';
        C.recomputeLeds();
        C.log('mouse', 'warn', '串口已断开 · 4 灯转为未知');
        C.toast('warn', '已断开', '电脑鼠连接已关闭；4 灯不再显示亮/灭。');
        refreshChrome();
      };
      const r = q('dr-rescue');
      if (r) r.onclick = function () { C.act('rescue'); };
    } else {
      const b = q('dr-fit');
      if (b) b.onclick = function () {
        h.clock.uncertaintyMs = 1.1; h.clock.samples += 24;
        C.log('mouse', 'info', '时钟自动拟合完成 · ±1.1 ms · RLS 238 样本');
        C.toast('ok', '拟合完成', '偏移 +3.0 ms · 不确定度 ±1.1 ms');
        openDev('clock'); refreshChrome();
      };
    }

    document.getElementById('drawer').classList.add('on');
    document.getElementById('drawer').setAttribute('aria-hidden', 'false');
    document.getElementById('scrim').classList.add('on');
  }

  function closeDev() {
    document.body.classList.remove('drawer-open');
    document.getElementById('drawer').classList.remove('on');
    document.getElementById('drawer').setAttribute('aria-hidden', 'true');
    document.getElementById('scrim').classList.remove('on');
  }

  /* ── 启动 ─────────────────────────────────────────────────────────────── */
  function boot() {
    renderNav();
    initDock();
    paintSeg();
    renderPage();
    refreshChrome();

    document.getElementById('scrim').onclick = closeDev;
    document.getElementById('dr-close').onclick = closeDev;
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeDev();
    });

    document.getElementById('btn-record').onclick = function () {
      if (S.session.active) C.sessionStop(); else C.sessionStart();
      refreshChrome();
    };
    document.getElementById('btn-estop').onclick = function () {
      if (S.estop) C.act('unlock'); else C.act('estop');
      refreshChrome();
    };
    document.getElementById('scene').onchange = function (e) {
      C.setScene(e.target.value);
    };

    C.on('tick', function () {
      const v = C.VIEWS[S.route];
      if (v && v.onTick) { try { v.onTick(); } catch (err) { console.error(err); } }
    });
    C.on('state', function () {
      refreshChrome();
      renderPage();
    });
    C.on('log', renderLog);
    C.on('route', function () {
      renderNav();
      renderTop();
      renderPage();
    });

    window.addEventListener('popstate', function () {
      const id = (location.hash || '').replace(/^#\//, '');
      C.go(C.VIEWS[id] ? id : 'overview', true);
    });
    const init = (location.hash || '').replace(/^#\//, '');
    S.route = C.VIEWS[init] ? init : 'overview';
    renderNav(); renderTop(); renderPage();

    /* 原型便利：?scene=run|estop|down|oldfw|novideo，?dev=mouse|clock */
    const qs = new URLSearchParams(location.search);
    const sc = qs.get('scene');
    if (sc) {
      const sel = document.getElementById('scene');
      if (sel) sel.value = sc;
      C.setScene(sc);
    }
    const dv = qs.get('dev');
    if (dv) setTimeout(function () { openDev(dv); }, 120);

    C.boot();
  }

  window.addEventListener('DOMContentLoaded', boot);
})();
