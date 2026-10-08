/* ⑧ 数据 —— 统一时间轴 / 会话浏览器 / 轨迹对比 / 无效帧统计 / 导出。
   这一页存在的理由：**任何人拿到一个会话目录，都能按 t_pc_ms 把三路数据排到一条线上。** */

C.views.data = (function () {
  let el, rows = [];
  let invalidReasons = {};
  let runs = [];

  function mount(host) {
    el = host;
    el.innerHTML = `
      <h2>⑧ 数据</h2>
      <div class="grid2">
        <div class="pane">
          <h4>会话浏览器
            <span><button class="mini" id="d-reload">刷新</button></span></h4>
          <div class="scroll" style="max-height:260px"><table id="d-sessions"></table></div>
          <div class="row">
            <button class="mini" id="d-export-zip">导出 zip</button>
            <button class="mini" id="d-export-csv">导出 timeline.csv</button>
            <button class="mini" id="d-export-jsonl">导出 results.jsonl</button>
          </div>
          <div class="hint" id="d-export-msg"></div>
        </div>
        <div class="pane">
          <h4>轨迹对比</h4>
          <canvas id="d-track"></canvas>
          <div class="kv" id="d-track-info" style="margin-top:6px"></div>
        </div>
      </div>
      <div class="grid2" style="margin-top:10px">
        <div class="pane">
          <h4>统一时间轴 <span class="hint" id="d-tl-count"></span></h4>
          <div class="row"><input id="d-filter" placeholder="按 source/kind 过滤，如 camera 或 move" style="flex:1">
            <button class="mini" id="d-apply">过滤</button></div>
          <div class="scroll" style="max-height:300px"><table id="d-timeline"></table></div>
        </div>
        <div class="pane">
          <h4>无效帧分类计数</h4>
          <div id="d-invalid" class="hint"></div>
          <h4 style="margin-top:10px">定位任务</h4>
          <div class="scroll" style="max-height:160px"><table id="d-runs"></table></div>
        </div>
      </div>`;
    el.querySelector('#d-reload').onclick = function () { reload(); };
    el.querySelector('#d-apply').onclick = function () { renderTimeline(); };
    el.querySelector('#d-export-zip').onclick = function () { doExport('zip'); };
    el.querySelector('#d-export-csv').onclick = function () { doExport('csv'); };
    el.querySelector('#d-export-jsonl').onclick = function () { doExport('jsonl'); };
    reload();
    return el;
  }

  async function reload() {
    const ss = await C.guard(function () { return C.api('/api/v1/sessions'); });
    renderSessions(ss || []);
    const tl = await C.guard(function () { return C.api('/api/v1/session/timeline?limit=40000'); });
    rows = tl || [];
    renderTimeline();
    const res = await C.guard(function () { return C.api('/api/v1/locate/results?limit=100000'); });
    computeInvalid(res);
    const rs = await C.guard(function () { return C.api('/api/v1/locate/runs'); });
    runs = rs || [];
    renderRuns();
    const tk = await C.guard(function () { return C.api('/api/v1/locate/track'); });
    renderTrack(tk);
  }

  function renderSessions(list) {
    const t = el.querySelector('#d-sessions');
    if (!list.length) {
      t.innerHTML = '<tr><td class="hint">无会话</td></tr>';
      return;
    }
    t.innerHTML = '<thead><tr><th>session</th><th>开</th><th>时长</th><th>时间轴行</th>' +
      '<th>定位</th><th>大小</th><th>meta</th></tr></thead><tbody>' +
      list.map(function (s) {
        const m = s.meta || {};
        const cnt = (m.counts) || {};
        return '<tr><td>' + C.util.esc(s.session_id) + '</td>' +
          '<td>' + C.util.esc((m.started_at || '').slice(11, 19)) + '</td>' +
          '<td>' + (m.ended_at ? '已收' : '<span class="tag warn">未完</span>') + '</td>' +
          '<td>' + C.util.fmt(s.timeline_rows) + '</td>' +
          '<td>' + (s.has_locate ? '<span class="tag ok">有</span>' : '—') +
            (cnt.locate_valid !== undefined ? ' ' + cnt.locate_valid + '/' +
              ((cnt.locate_valid || 0) + (cnt.locate_invalid || 0)) : '') + '</td>' +
          '<td>' + ((s.size_bytes || 0) / 1048576).toFixed(1) + ' MB</td>' +
          '<td>' + (s.meta_error ? '<span class="tag bad">' + C.util.esc(s.meta_error) + '</span>' : '正常') + '</td>' +
          '</tr>';
      }).join('') + '</tbody>';
  }

  function renderTimeline() {
    const q = (el.querySelector('#d-filter').value || '').trim().toLowerCase();
    const list = q ? rows.filter(function (r) {
      return (r.source + ' ' + r.kind + ' ' + (r.note || '')).toLowerCase().indexOf(q) >= 0;
    }) : rows;
    el.querySelector('#d-tl-count').textContent = list.length + ' / ' + rows.length + ' 行';
    const t = el.querySelector('#d-timeline');
    if (!list.length) {
      t.innerHTML = '<tr><td class="hint">无事件</td></tr>';
      return;
    }
    const tail = list.slice(-600).reverse();
    // `t_src` 一列是**这次跟新加的**：它说明这一行的 t_pc_ms 是**哪种时间** ——
    //   send    = 帧真正发出/拍摄的时刻（准确的那种）
    //   arrival = 只拿到"主机收到"的时刻（链路抖动被算进去了）
    //   local   = 本层自己产生的事件（开会话、点按钮）
    // 分不清"哪种时间"的数据比没有时间更危险，所以它就在表上。
    t.innerHTML = '<thead><tr><th>t_pc_ms</th><th>时刻</th><th>源</th><th>类型</th>' +
      '<th>ref</th><th>t</th><th>dev_ms</th><th>note</th></tr></thead><tbody>' +
      tail.map(function (r) {
        const src = r.t_src || '';
        const tag = src === 'send' || src === 'capture'
          ? '<span class="tag ok" title="帧发生时刻">' + src + '</span>'
          : src === 'arrival'
            ? '<span class="tag warn" title="仅记录主机接收时刻">arrival</span>'
            : '<span class="dim">' + C.util.esc(src || '—') + '</span>';
        return '<tr><td>' + C.util.esc(r.t_pc_ms) + '</td><td>' +
          C.util.esc((r.t_pc_iso || '').slice(11)) + '</td><td>' +
          C.util.esc(r.source) + '</td><td>' + C.util.esc(r.kind) + '</td><td>' +
          C.util.esc(r.ref || '') + '</td><td>' + tag + '</td><td>' +
          C.util.esc(r.dev_ms || '') + '</td><td>' +
          C.util.esc(r.note || '') + '</td></tr>';
      }).join('') + '</tbody>';
  }

  function computeInvalid(res) {
    invalidReasons = {};
    if (res && res.items) {
      res.items.forEach(function (d) {
        if (d.valid) return;
        const r = d.reason || 'unknown';
        invalidReasons[r] = (invalidReasons[r] || 0) + 1;
      });
    }
    const box = el.querySelector('#d-invalid');
    const ks = Object.keys(invalidReasons);
    if (!ks.length) {
      box.innerHTML = '无定位结果' +
        (res && res.note ? '<br>' + C.util.esc(res.note) : '');
      return;
    }
    const total = ks.reduce(function (a, k) { return a + invalidReasons[k]; }, 0);
    box.innerHTML = ks.sort(function (a, b) { return invalidReasons[b] - invalidReasons[a]; })
      .map(function (k) {
        const pct = (invalidReasons[k] / total * 100);
        return '<div>' + C.util.esc(k) + ' <b>' + invalidReasons[k] + '</b>' +
          '<div class="bar"><i style="width:' + pct.toFixed(1) + '%"></i></div></div>';
      }).join('');
  }

  function renderRuns() {
    const t = el.querySelector('#d-runs');
    if (!runs.length) { t.innerHTML = '<tr><td class="hint">未执行赛后批量</td></tr>'; return; }
    t.innerHTML = '<thead><tr><th>id</th><th>状态</th><th>进度</th><th>有效/无效</th>' +
      '<th>速率</th></tr></thead><tbody>' +
      runs.map(function (r) {
        return '<tr><td>' + C.util.esc(r.id) + '</td><td>' + C.util.esc(r.state) +
          '</td><td>' + r.done + '/' + r.total + '</td><td>' + r.valid + '/' + r.invalid +
          '</td><td>' + C.util.fmt(r.rate_per_s, 2) + ' fps</td></tr>';
      }).join('') + '</tbody>';
  }

  function renderTrack(tk) {
    const cv = el.querySelector('#d-track');
    const pts = (tk && tk.points) || [];
    const valid = pts.filter(function (p) { return p.valid; });
    C.draw.arena(cv, {
      grid: (C.state.health && C.state.health.arena && C.state.health.arena.grid) || 8,
      trackOffline: valid.map(function (p) { return [p.x, p.y]; }),
      watermark: (C.state.health && C.state.health.arena && !C.state.health.arena.calibrated)
        ? '坐标未标定' : null,
      height: Math.max(200, Math.min(360, cv.clientWidth))
    });
    const bad = pts.length - valid.length;
    el.querySelector('#d-track-info').innerHTML =
      '<b>离线（权威）</b><span>' + valid.length + ' 个有效点</span>' +
      '<b>无效</b><span>' + bad + ' 个</span>' +
      '<b>说明</b><span>' +
      (tk && tk.pushed ? '点数过多，走 WS' :
        (pts.length ? '赛后批量（全量）' : '无轨迹')) + '</span>';
  }

  async function doExport(kind) {
    const h = C.state.health || {};
    const sid = (h.session && h.session.session_id) ||
      prompt('导出哪个 session_id');
    if (!sid) return;
    const d = await C.guard(function () {
      return C.api('/api/v1/session/export?kind=' + kind + '&session_id=' + encodeURIComponent(sid));
    }, '导出');
    if (d) {
      el.querySelector('#d-export-msg').innerHTML = '导出到：' + C.util.esc(d.file) +
        '<br><a href="' + d.download + '" download>下载</a>';
    }
  }

  return {
    mount: mount,
    activate: function () { reload(); },
    onMsg: function (m) {
      if (m.type === 'session.phase') reload();
      else if (m.type === 'locate.batch') renderRuns();
    },
    context: function (box) {
      box.innerHTML = '';
    }
  };
})();
