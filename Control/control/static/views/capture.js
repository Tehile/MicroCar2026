/* ② 采集 —— 相机侧的完整功能。
   三条不能违反的显示规矩：
     ① 账本的三个数**分开显示**：frames_expected / gaps_on_device（=重采）/ upload_gaps（=重传）
     ② complete=null 必须显示为「**判不了**」，**不许**显示成通过（判据 I11）
     ③ plan.interval_ms ≠ 0 是**语义变化**（抽帧），必须显式提示 */

C.views.capture = (function () {
  let el, sessions = [], ledger = null, status = null;

  function mount(host) {
    el = host;
    el.innerHTML = `
      <h2>② 采集</h2>
      <div class="grid2">
        <div class="pane">
          <h4>采集控制 <span class="hint" id="cg-ov"></span></h4>
          <div class="gatehint" id="cg-link-hint"></div>
          <div class="row">
            <button class="mini" id="cg-start">开始</button>
            <button class="mini" id="cg-stop">停止</button>
            <button class="mini" id="cg-refresh">刷新</button>
          </div>
          <div class="row">
            <label class="hint" style="flex:0 0 84px">抽帧 interval_ms</label>
            <input id="cg-interval" value="0" style="width:80px">
            <input id="cg-count" value="0" placeholder="count" style="width:70px">
          </div>
          <div class="hint" id="cg-plan-warn"></div>
          <div class="kv" id="cg-status" style="margin-top:6px"></div>
        </div>
        <div class="pane">
          <h4>留存账本</h4>
          <div id="cg-ledger" class="kv"></div>
          <div class="hint" id="cg-ledger-note"></div>
        </div>
      </div>

      <div class="grid2" style="margin-top:10px">
        <div class="pane">
          <h4>会话表</h4>
          <div class="scroll"><table id="cg-sessions"></table></div>
        </div>
        <div class="pane">
          <h4>配对 <span class="tag warn">含 token · 勿投屏</span></h4>
          <div class="row"><button class="mini" id="cg-pair">配对与连接</button></div>
        </div>
      </div>

      <div class="pane" style="margin-top:10px">
        <h4>离线会话导入</h4>
        <div class="row">
          <button class="mini" id="cg-offline-list">列出本地会话</button>
          <button class="mini" id="cg-offline-exit">退出离线模式</button>
          <span class="hint" id="cg-offline-state"></span>
        </div>
        <div class="scroll" style="max-height:190px;margin-top:6px"><table id="cg-offline"></table></div>
        
      </div>`;

    el.querySelector('#cg-start').onclick = function () { camStart(); };
    el.querySelector('#cg-stop').onclick = function () { camStop(); };
    el.querySelector('#cg-refresh').onclick = function () { refresh(); listOffline(); };
    // 配对二维码**只保留一处实现**（顶栏【相机】那个窗口）。
    // 两份实现各画一次的下场是"面板上的能扫、另一个画出来的不能" —— 而且看着都对。
    el.querySelector('#cg-pair').onclick = function () { C.openPair(); };
    el.querySelector('#cg-interval').oninput = renderPlanWarn;
    el.querySelector('#cg-offline-list').onclick = function () { listOffline(); };
    el.querySelector('#cg-offline-exit').onclick = function () {
      C.guard(function () { return C.api('/api/v1/camera/exit_offline', { method: 'POST', body: {} }); })
        .then(function () { C.toast('已退出离线模式', 'ok'); listOffline(); });
    };
    refresh();
    listOffline();
    return el;
  }

  function renderPlanWarn() {
    const v = Number(el.querySelector('#cg-interval').value || 0);
    el.querySelector('#cg-plan-warn').innerHTML = v !== 0
      ? '<span class="tag bad">抽帧</span> interval_ms=' + v + ' · every_frame=false'
      : '<span class="tag ok">每一帧都留</span> interval_ms=0';
  }

  async function refresh() {
    // 相机不可达就**一个请求都不发**：这几个端点在服务端每次要干等 ~2 s，
    // 而这一页在每次切页签时都会刷一遍 —— 白等之外还会把同一条错误刷满屏。
    if (C.camUp() === false) {
      status = null;
      sessions = [];
      ledger = null;
      renderStatus(null, true);           // true = 已跳过探测，把原因写出来
      renderSessions();
      renderLedger();
      listOffline();
      return;
    }
    status = await C.guard(function () { return C.api('/api/v1/camera/status'); });
    sessions = await C.guard(function () { return C.api('/api/v1/camera/sessions'); });
    const ov = await C.guard(function () { return C.api('/api/v1/camera/overview'); });
    ledger = await C.guard(function () { return C.api('/api/v1/camera/ledger'); });
    renderStatus(ov);
    renderSessions();
    renderLedger();
    listOffline();
  }

  function renderStatus(ov, skipped) {
    const s = status || {};
    if (skipped) {
      C.camDownHint(el.querySelector('#cg-ov'));
    } else {
      el.querySelector('#cg-ov').textContent = ov
        ? ('profile=' + (ov.profile_id || '?') + '  data_dir=' + (ov.data_dir || '?') +
           '  ws_clients=' + (ov.ws_clients === undefined ? '?' : ov.ws_clients))
        : '';
    }
    const keys = ['state', 'session_id', 'device_serial', 'capture_path_id', 'every_frame',
                  'interval_ms', 'frames_verified_on_pc', 'app_online', 'gate'];
    el.querySelector('#cg-status').innerHTML = keys.map(function (k) {
      let v = s[k];
      if (typeof v === 'object' && v !== null) v = JSON.stringify(v);
      return '<b>' + k + '</b><span>' + C.util.esc(String(v === undefined ? '—' : v)) + '</span>';
    }).join('');
  }

  function renderSessions() {
    const t = el.querySelector('#cg-sessions');
    if (!sessions || !sessions.length) {
      t.innerHTML = '<tr><td class="hint">相机服务无会话</td></tr>';
      return;
    }
    const keys = [];
    sessions.forEach(function (s) {
      Object.keys(s).forEach(function (k) { if (keys.indexOf(k) < 0) keys.push(k); });
    });
    t.innerHTML = '<thead><tr>' + keys.map(function (k) { return '<th>' + C.util.esc(k) + '</th>'; }).join('') +
      '</tr></thead><tbody>' + sessions.map(function (s) {
        return '<tr>' + keys.map(function (k) {
          let v = s[k];
          if (typeof v === 'object' && v !== null) v = JSON.stringify(v);
          return '<td>' + C.util.esc(String(v === undefined ? '' : v)) + '</td>';
        }).join('') + '</tr>';
      }).join('') + '</tbody>';
  }

  /* 账本的几个数**分开显示**：合成一个数现场必然选错处置（采集缺口只能重采、
     回传缺口可以重传）。字段缺失就整行不显示 —— 不拿 0 冒充"没有缺口"。 */
  const LEDGER_FIELDS = [
    ['frames_expected', '应有帧数'],
    ['frames_on_device', '设备留存'],
    ['frames_verified_on_pc', 'PC 实收'],
    ['gaps_on_device', '采集缺口'],
    ['upload_gaps', '回传缺口'],
    ['every_frame', '每一帧都留']
  ];

  function renderLedger() {
    const box = el.querySelector('#cg-ledger');
    const led = ledger;
    if (!led) { box.innerHTML = '<b>—</b><span>服务不可达</span>'; return; }
    const rows = LEDGER_FIELDS.filter(function (f) { return led[f[0]] !== undefined; })
      .map(function (f) {
        const v = led[f[0]];
        return '<b>' + f[1] + '</b><span>' +
          C.util.esc(v === null ? '—' : String(v)) + '</span>';
      });
    if (led.complete !== undefined) {
      const c = led.complete;
      rows.unshift('<b>完整</b><span class="tag ' +
        (c === true ? 'ok' : c === false ? 'bad' : 'warn') + '">' +
        (c === true ? '通过' : c === false ? '不通过' : '判不了') + '</span>');
    }
    box.innerHTML = rows.join('') || '<b>—</b><span>无账本数据</span>';
    let note = led.note || '';
    if (led.missing_seqs_note) note += '\n' + led.missing_seqs_note;
    if (led.missing_seqs && led.missing_seqs.length) {
      note += '\n缺号前 40：' + led.missing_seqs.slice(0, 40).join(', ');
    }
    el.querySelector('#cg-ledger-note').textContent = note;
  }

  async function camStart() {
    const body = { plan: { mode: 'interval',
      interval_ms: Number(el.querySelector('#cg-interval').value || 0),
      count: Number(el.querySelector('#cg-count').value || 0) } };
    const d = await C.guard(function () {
      return C.api('/api/v1/camera/start', { method: 'POST', body: body });
    }, 'start');
    if (d) {
      if (d.sent_to === 0) C.toast('sent_to=0：无手机接收', 'warn', 12000);
      C.toast('采集已启动：' + d.session_id, 'ok');
      refresh();
    }
  }

  async function camStop() {
    const h = C.state.health || {};
    const sid = (h.session && h.session.camera_ref && h.session.camera_ref.session_id) || '';
    const d = await C.guard(function () {
      return C.api('/api/v1/camera/stop', { method: 'POST', body: { session_id: sid } });
    }, 'stop');
    if (d) { C.toast('已发送停止指令', 'ok'); refresh(); }
  }

  async function listOffline() {
    const d = await C.guard(function () { return C.api('/api/v1/camera/offline/sessions'); });
    if (!d) return;
    const t = el.querySelector('#cg-offline');
    el.querySelector('#cg-offline-state').textContent =
      'root=' + d.root + (d.current ? '  |  当前离线：' + d.current.session_id : '');
    if (!d.sessions.length) {
      t.innerHTML = '<tr><td class="hint">' + C.util.esc(d.note || '无可导入的本地会话') + '</td></tr>';
      return;
    }
    t.innerHTML = '<thead><tr><th>session</th><th>帧数</th><th>state</th><th>device</th><th>磁盘</th><th></th></tr></thead><tbody>' +
      d.sessions.map(function (s) {
        return '<tr><td>' + C.util.esc(s.session_id) + '</td><td>' + s.frames + '</td><td>' +
          C.util.esc(s.state || '—') + '</td><td>' + C.util.esc(s.device_serial || '—') + '</td><td>' +
          (s.bytes / 1048576).toFixed(1) + ' MB</td>' +
          '<td><button class="mini" data-imp="' + C.util.esc(s.session_id) + '"' +
          (s.frames ? '' : ' disabled') + '>导入</button></td></tr>';
      }).join('') + '</tbody>';
    t.querySelectorAll('[data-imp]').forEach(function (b) {
      b.onclick = async function () {
        const r = await C.guard(function () {
          return C.api('/api/v1/camera/import', { method: 'POST', body: {
            session_id: b.dataset.imp, replay: true, rate_hz: 30 } });
        }, '导入');
        if (r) C.toast('已导入 ' + r.session_id + '：' + r.frames + ' 帧 @ ' + r.rate_hz + ' fps', 'ok');
      };
    });
  }

  return {
    mount: mount,
    activate: function () { refresh(); },
    onMsg: function (m) {
      if (m.type === 'camera.status' && m.reconnected) {
        C.toast('相机事件流已重连', 'warn', 10000);
        refresh();
        return;
      }
      if (m.type === 'camera.delivery_done') { refresh(); return; }
      if (m.type === 'camera.event' && m.type2 !== 'frame_verified') { /* 忽略高频 */ }
      if (m.type === 'log' && /账本|采集|delivery|重连/.test(m.text || '')) renderLedger();
    },
    /* 采集控制要"相机服务可达 **且** 手机已连"才开门 —— 只看服务可达会得到一个空会话
       （上游回 sent_to=0），那正是这一页最容易白等的地方。
       不门控的：刷新、离线导入/退出（离线模式下"相机"是本地目录）、抽帧参数输入。 */
    onHealth: function (h) {
      const why = C.needCam('采集');
      ['cg-start', 'cg-stop'].forEach(function (id) {
        C.gate(el.querySelector('#' + id), why);
      });
      const c = (h || {}).camera || {};
      // 配对按钮：离线模式不需要配对；服务不可达时配对信息也取不到（弹窗里会解释）。
      C.gate(el.querySelector('#cg-pair'),
        c.offline ? '离线模式无需配对' : null);
      const note = el.querySelector('#cg-link-hint');
      if (note) note.textContent = why || '';
    },
    context: function (box) {
      box.innerHTML = '';
    }
  };
})();
