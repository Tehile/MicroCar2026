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

  /* 数据源状态（改版四十）：**唯一**一处说明"这些数字是哪来的"。
     以前是左轨底部两格小字（`rail-mode` / `rail-proto`），
       · 写死过 "v9 / PROTO 9"，协议升到 v10 它还在那儿（第二个真相）；
       · 和顶栏那盏写死"实时"的 `#ws` 同时存在，两处说法可以不一致。
     现在合成一盏：`LIVE.on` 一个判据，三种表现（见 style.css 的 `.ws`）。
     ⚠️ `LIVE.on === false` 不等于"后端坏了"：它是"退回原型数据"，
        这句要在 title 里说清楚，否则用户以为界面在骗他。 */
  function renderMode() {
    const el = document.getElementById('ws');
    if (!el) return;
    const txt = el.querySelector('span');
    const on = C.LIVE.on;
    el.className = 'ws' + (on ? '' : (C.LIVE.err ? ' proto' : ' unknown'));
    if (txt) txt.textContent = on ? '实时' : (C.LIVE.err ? '原型' : '—');
    el.title = on ? '已连后端 · 遥测走 WebSocket'
      : (C.LIVE.err ? ('未连后端（' + C.LIVE.err + '）· 界面按原型数据演示')
        : '正在探测后端…');
  }

  /* ── 顶栏设备片：只有两路 ─────────────────────────────────────────────── */
  function devState(kind) {
    const h = S.health;
    if (kind === 'mouse') {
      if (!h.mouse.available) return { cls: 'bad', txt: '模块缺失' };
      if (h.mouse.connected) {
        /* 改版四十七：**串口开着 ≠ 链路正常**。按后端统一状态上色，别一概"已连接"。 */
        if (h.mouse.linkState === 'dead') return { cls: 'bad', txt: '链路无应答' };
        if (h.mouse.linkState === 'suspect') return { cls: 'warn', txt: '链路疑似' };
        return { cls: 'ok', txt: h.mouse.port || '已连接' };
      }
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
    renderMode();
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
    /* 路由挂到 body 上：个别页要按页改**外壳**尺寸（回放页要把主区底部的
       56 px 留白收掉，否则"按视口算高度"会多扣一段白给日志坞的空白）。 */
    document.body.dataset.route = S.route;
    main.scrollTop = 0;
    /* 主区的**行内高度**由页面自己决定（比赛页要钉住"视口 − 顶栏 − 日志坞"）。
       这里先清掉上一次留下的值：不清的话，从比赛页切到别的页，
       那一页会被钉成一屏高、内容全被 `overflow:hidden` 剪掉。 */
    main.style.height = '';
    main._stageH = 0;
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

  /* 可连接目标：**来自后端**（`/mouse/ports` 扫的是本机真实串口）。
     原来这里是写死的三行（COM7 / COM8 / 模拟器），换台机器就全错。 */
  function portRows(m) {
    const list = S.ports;
    if (!list) {
      return '<div class="hint">' + (S.portsErr
        ? ('串口列表取不到：' + U.esc(S.portsErr))
        : '正在读串口列表…') + '</div>';
    }
    if (!list.length) return '<div class="hint">这台机器上没扫到串口。手填 COM7 也行。</div>';
    return list.map(function (p) {
      const cur = !!m.connected && (p.port === m.port || p.target === m.port);
      return '<button class="port-row' + (cur ? ' cur' : '') + '" data-port="' +
        U.esc(p.target) + '">' +
        '<span class="mono" style="font-size:11px">' + U.esc(p.label) + '</span>' +
        '<span class="tag ' + (cur ? 'ok' : '') + '" style="margin-left:auto">' +
        (cur ? '当前' : '选') + '</span></button>';
    }).join('');
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
          // 没连上就别把默认值当读数写出来：fw / 协议 / 通道都是**连上才有**
          ['固件', m.connected
            ? (m.fw || '—') + '（' + (m.fwInt == null ? '4 灯判不了'
              : (m.fwInt >= 311 ? '4 灯可读' : '4 灯未知')) + '）'
            : '—'],
          ['协议', m.connected ? ('v' + (m.proto == null ? '?' : m.proto) + ' · 30 B 帧') : '—'],
          ['通道', m.connected
            ? (String((m.caps && m.caps.unified_channels) || '—') + ' 个') : '—'],
          ['链路', m.link || '—'],
          ['最近错误', m.detail || '—', 'dim'],
        ]) +
        '<div class="sec-title">可连接目标</div>' +
        '<div id="dr-ports">' + portRows(m) + '</div>' +
        '<div class="row" style="margin-top:10px"><input class="input" id="dr-manual" placeholder="COM7 / tcp://…" style="flex:1">' +
        '<button class="btn btn-primary" id="dr-connect">连接</button></div>' +
        '<div class="row" style="margin-top:8px">' +
        '<button class="btn btn-sm" id="dr-disconnect">断开</button>' +
        '<button class="btn btn-sm" id="dr-rescue">软复位</button>' +
        '<button class="btn btn-sm btn-ghost" id="dr-ports-refresh">刷新端口</button></div>' +
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
      const bindPortRows = function (scope) {
        (scope || body).querySelectorAll('[data-port]').forEach(p => {
          p.onclick = function () {
            body.querySelectorAll('[data-port]').forEach(x => x.classList.remove('cur'));
            p.classList.add('cur');
            const m0 = q('dr-manual');
            if (m0) m0.value = p.dataset.port;
          };
        });
      };
      bindPortRows();
      // 端口列表来自后端：先画"正在读…"，到了只重画这一段（不整页重挂，免得把输入框清掉）
      C.listPorts().then(function () {
        const box = body.querySelector('#dr-ports');
        if (!box) return;
        box.innerHTML = portRows(h.mouse);
        bindPortRows(box);
      });
      const b = q('dr-connect');
      if (b) b.onclick = function () {
        // 手填优先；没填就用列表第一个。**不写死 COM7** —— 换台机器就错了。
        let val = (q('dr-manual').value || '').trim();
        if (!val && S.ports && S.ports.length) val = S.ports[0].target;
        if (!val) {
          C.toast('warn', '先选一个端口', '列表里点一个，或手填 COM7 / tcp://…');
          return;
        }
        b.disabled = true; b.textContent = '连接中…';
        C.connectMouse(val).then(function (r) {
          const dv = (r && r.device) || {};
          C.toast('ok', '已连接', val + (dv.fw != null ? (' · fw ' + dv.fw) : ''));
        }).catch(function (e) {
          // 失败要**说清是哪一步错**：端口被占用 / 找不到 / 设备不应答
          C.toast('bad', '连不上', (e && (e.detail || e.message)) || String(e), 8000);
          C.log('mouse', 'error', '连接 ' + val + ' 失败：' +
            ((e && (e.detail || e.message)) || e));
        }).then(function () {
          b.disabled = false; b.textContent = '连接';
          openDev('mouse');                 // 用后端状态重画抽屉（成功失败都一样）
        });
      };
      const d = q('dr-disconnect');
      if (d) d.onclick = function () {
        d.disabled = true; d.textContent = '断开中…';
        C.disconnectMouse().then(function () {
          C.toast('warn', '已断开', '串口已关闭；4 灯转为未知。');
        }).catch(function (e) {
          C.toast('bad', '断开失败', (e && (e.detail || e.message)) || String(e));
        }).then(function () {
          d.disabled = false; d.textContent = '断开';
          openDev('mouse');
        });
      };
      const rf = q('dr-ports-refresh');
      if (rf) rf.onclick = function () {
        S.ports = null; S.portsErr = '';
        q('dr-ports').innerHTML = portRows(h.mouse);
        C.listPorts(true).then(function () {
          const box = q('dr-ports');
          if (box) { box.innerHTML = portRows(h.mouse); bindPortRows(box); }
        });
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

    C.on('tick', function () {
      const v = C.VIEWS[S.route];
      if (v && v.onTick) { try { v.onTick(); } catch (err) { console.error(err); } }
    });
    C.on('state', function () {
      refreshChrome();
      renderPage();
    });
    C.on('log', renderLog);
    /* 每秒一次的 health 只重画**外壳**（顶栏/设备片/准备清单/底栏计数），
       不重挂页面 —— 重挂会把参数页正在输入的框清掉。 */
    C.on('health', function () {
      try { refreshChrome(); } catch (e) { console.error(e); }
      try { renderTop(); } catch (e) { console.error(e); }
    });
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
    /* ⚠️ 首屏也必须走 `C.go()`，不能只写 `S.route` 再手动渲染（改版三十九修正）。
       `go()` 里带着**进这一页该做的事**：
         · 比赛页 `startRaceTick()` —— 不跑它，迷宫图**永远不会去取**，页面上
           只有一张空白网格 + 「等待数据」，而顶栏明明写着"实时·已连接"；
         · 参数页 `syncPull()+needPage()` —— 不跑它，参数表空着（它依赖拉取在跑）。
       为什么一直没被发现：`V12/V9` 这些判据是**用 `C.go('race')` 跳过去**的，
       天然绕开了"刷新时正停在这一页"这条路 —— 判据没走过真实入口。
       `silent=true`：不要因为恢复路由而多压一条 history。 */
    C.go(C.VIEWS[init] ? init : 'overview', true);

    /* 原型便利：?dev=mouse|clock 直接打开设备抽屉。
       （`?scene=` 随左轨"情景"下拉一起删了 —— 它只改浏览器里的演示状态，
         接上真机后一个字节都不影响设备，留着只会让人以为自己在控制设备。） */
    const qs = new URLSearchParams(location.search);
    const dv = qs.get('dev');
    if (dv) setTimeout(function () { openDev(dv); }, 120);

    C.boot();

    /* 真后端接入（改版二十八 · P4）：连上就切真数据，连不上保持原型。
       ⚠️ 必须放在 `C.boot()` **之后** —— 它要读 `S.health` 与 `S.readiness`，
       那两样在 boot 里才初始化完。 */
    try { C.startLive(); } catch (e) { console.error(e); }
  }

  window.addEventListener('DOMContentLoaded', boot);
})();
