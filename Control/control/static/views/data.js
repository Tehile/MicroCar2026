/* ⑧ 数据 —— 会话浏览器 / 统一时间轴 / 导出。
   这一页存在的理由：**任何人拿到一个会话目录，都能按 t_pc_ms 把命中的事件排到一条线上。**

   ⚠️ 改版二十四：相机与在线定位整块下线，这一页**去掉了三块**：
   「轨迹对比」（读 `/locate/track`）、「无效帧分类计数」（读 `/locate/results`）、
   「定位任务」（读 `/locate/runs`）—— 那三条端点的数据源已经不存在。
   轨迹现在归「视频复盘 · 轨迹」页（读视频会话产物），不该在这里再来一份。
   删的时候连带删了它的**写入方**（`computeInvalid` / `renderRuns` / `renderTrack`），
   否则就是"读了一个没人写的数"（铁律 32）。 */

C.views.data = (function () {
  let el, rows = [];

  function mount(host) {
    el = host;
    el.innerHTML = `
      <h2>⑧ 数据</h2>
      <div class="grid2">
        <div class="pane">
          <h4>会话浏览器
            <span><button class="mini" id="d-reload">刷新</button></span></h4>
          <div class="scroll" style="max-height:300px"><table id="d-sessions"></table></div>
          <div class="row">
            <button class="mini" id="d-export-zip">导出 zip</button>
            <button class="mini" id="d-export-csv">导出 timeline.csv</button>
          </div>
          <div class="hint" id="d-export-msg"></div>
        </div>
        <div class="pane">
          <h4>统一时间轴 <span class="hint" id="d-tl-count"></span></h4>
          <div class="row"><input id="d-filter" placeholder="按 source/kind 过滤，如 mouse 或 raw" style="flex:1">
            <button class="mini" id="d-apply">过滤</button></div>
          <div class="scroll" style="max-height:420px"><table id="d-timeline"></table></div>
        </div>
      </div>`;
    el.querySelector('#d-reload').onclick = function () { reload(); };
    el.querySelector('#d-apply').onclick = function () { renderTimeline(); };
    el.querySelector('#d-export-zip').onclick = function () { doExport('zip'); };
    el.querySelector('#d-export-csv').onclick = function () { doExport('csv'); };
    reload();
    return el;
  }

  async function reload() {
    const ss = await C.guard(function () { return C.api('/api/v1/sessions'); });
    renderSessions(ss || []);
    const tl = await C.guard(function () { return C.api('/api/v1/session/timeline?limit=40000'); });
    rows = tl || [];
    renderTimeline();
  }

  function renderSessions(list) {
    const t = el.querySelector('#d-sessions');
    if (!list.length) {
      t.innerHTML = '<tr><td class="hint">无会话</td></tr>';
      return;
    }
    /* 列只留"这场记录是什么"：谁、开在哪一刻、收没收到、时间轴多少行、多大、meta 好不好。
       原来的「定位」列（`has_locate` + 有效/无效计数）随定位下线删掉 —— 那一列现在
       永远是"—"，留着等于在界面上挂一个死字段。 */
    t.innerHTML = '<thead><tr><th>session</th><th>开</th><th>时长</th><th>时间轴行</th>' +
      '<th>大小</th><th>meta</th></tr></thead><tbody>' +
      list.map(function (s) {
        const m = s.meta || {};
        return '<tr><td>' + C.util.esc(s.session_id) + '</td>' +
          '<td>' + C.util.esc((m.started_at || '').slice(11, 19)) + '</td>' +
          '<td>' + (m.ended_at ? '已收' : '<span class="tag warn">未完</span>') + '</td>' +
          '<td>' + C.util.fmt(s.timeline_rows) + '</td>' +
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
    // `t_src` 一列说明这一行的 t_pc_ms 是**哪种时间** ——
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
    },
    context: function (box) {
      box.innerHTML = '';
    }
  };
})();
