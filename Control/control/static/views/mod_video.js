/* 视频复盘 · 视频会话（改版二十四）—— 把一段视频交给 Auto Locate 解析出轨迹。
   ============================================================
   这一页替代了原来的「相机」两页：位置数据不再来自实时回传，而是来自**一段视频**
   （跑完再解析，离线批处理）。所以这一页只做四件事：

     选视频 → 起解析 → 看状态 → 看产物在哪

   三条纪律：
   · **不给假进度**：上游没有进度回调，只有"已用多少秒"；编百分比就是骗人；
   · **缺数据不瞎画**：没跑过写「还没有解析过视频」，失败写原因，不画空表冒充"0 帧"；
   · **失败给可照做清单**（视频打不开 / 无有效帧 / 缺依赖 / 超时），不给散文。
*/
C.mods.mod_video = (function () {
  let el = null;
  let timer = null;          // 状态轮询（只在 running 时开）
  let sessionOf = '';        // 已经拉过明细的会话 id（避免每秒重复读文件）
  let lastState = '';

  function q(sel) { return el ? el.querySelector(sel) : null; }
  function esc(s) { return C.util.esc(s == null ? '' : String(s)); }

  function sizeText(n) {
    if (n == null) return '—';
    const mb = n / (1024 * 1024);
    return mb >= 1024 ? (C.util.fmt(mb / 1024, 2) + ' GB') : (C.util.fmt(mb, 1) + ' MB');
  }

  function durText(s) {
    if (s == null) return '—';
    const t = Math.max(0, Math.round(s));
    const m = Math.floor(t / 60);
    return m ? (m + ' 分 ' + (t % 60) + ' 秒') : (t + ' 秒');
  }

  /* 上游 flags → 一句能看懂的话。**认不出的原样印码**（不臆造含义）——
     新 flag 出现时"看到一个陌生码"比"看到一句错的解释"好。 */
  const FLAG_TEXT = {
    vfr_suspected: '时间轴不可信（疑似可变帧率）',
    no_valid_frames: '没有一帧定位成功',
    maze_inconsistent: '迷宫各段不一致（融合结果不可信）',
    maze_not_enabled: '本次未做迷宫融合',
    decode_errors: '有帧解码失败',
    maze_missing: '迷宫融合没有结果',
  };

  function flagsText(flags) {
    const list = (flags || []).map(function (f) {
      return FLAG_TEXT[f] ? (FLAG_TEXT[f] + '（' + f + '）') : String(f);
    });
    return list.length ? list.join('；') : '无';
  }

  /* ------------------------------------------------------------ 视频列表 */
  async function loadVideos() {
    const d = await C.guard(function () { return C.api('/api/v1/video/list'); }, '读视频列表');
    if (!d) return;
    const sel = q('#vd-list');
    if (!sel) return;
    const vs = d.videos || [];
    if (!vs.length) {
      sel.innerHTML = '<option value="">（' + esc(d.root) + ' 里没有视频）</option>';
      const note = q('#vd-list-note');
      if (note) note.textContent = '把视频放到 ' + d.root + '，或直接在下面填绝对路径';
      return;
    }
    sel.innerHTML = vs.map(function (v) {
      return '<option value="' + esc(v.path) + '">' + esc(v.name) +
        ' · ' + sizeText(v.size_bytes) + '</option>';
    }).join('');
    const note = q('#vd-list-note');
    if (note) note.textContent = vs.length + ' 个 · ' + d.root;
  }

  /* ------------------------------------------------------------ 状态 */
  function stateLine(j) {
    const st = j.state || 'idle';
    if (st === 'running') {
      return '运行中 · 已用 ' + durText(j.elapsed_s) + '（上游没有进度回调，只有用时）';
    }
    if (st === 'done') {
      const n = j.n_total, v = j.n_valid;
      const r = (j.valid_ratio != null) ? ('，' + C.util.fmt(j.valid_ratio * 100, 0) + '%')
        : (n ? ('，' + C.util.fmt(v / n * 100, 0) + '%') : '');
      return '完成 · ' + (n == null ? '—' : n) + ' 帧（有效 ' + (v == null ? '—' : v) + r + '）';
    }
    if (st === 'cancelled') return '已取消 · 产物可能不完整';
    if (st === 'failed') return '失败';
    return '还没有解析过视频';
  }

  function renderStatus(j) {
    j = j || {};
    lastState = j.state || 'idle';
    const line = q('#vd-state');
    if (line) line.textContent = stateLine(j);

    const run = q('#vd-run');
    const cancel = q('#vd-cancel');
    const busy = (j.state === 'running');
    if (run) { run.disabled = busy; run.textContent = busy ? '解析中…' : '开始解析'; }
    if (cancel) cancel.disabled = !busy;

    const err = q('#vd-err');
    if (err) {
      err.textContent = (j.state === 'failed' && j.error)
        ? ('原因：' + j.error + (j.log ? ('\n日志：' + j.log) : ''))
        : '';
    }

    const out = q('#vd-out');
    if (out) {
      const rows = [];
      if (j.out_dir) rows.push(['输出目录', j.out_dir]);
      if (j.source) rows.push(['视频', j.source]);
      if (j.flags && j.flags.length) rows.push(['标志', flagsText(j.flags)]);
      out.innerHTML = rows.map(function (r) {
        return '<b>' + esc(r[0]) + '</b><span>' + esc(r[1]) + '</span>';
      }).join('');
    }
    return busy;
  }

  /* 完成之后读一次明细（帧统计 / flags / 迷宫块）。**按会话 id 缓存**：
     状态端点是零 IO 的，每秒去读一次 session.json 就把这条好处抵消了。 */
  async function loadDetail(id) {
    if (!id || id === sessionOf) return;
    sessionOf = id;
    const d = await C.guard(function () {
      return C.api('/api/v1/video/session/' + encodeURIComponent(id));
    }, '读会话摘要');
    const box = q('#vd-detail');
    if (!d || !box) return;
    const fr = d.frames || {};
    const mz = d.maze || null;
    const rows = [
      ['会话', d.id],
      ['帧', (fr.n_total == null ? '—' : fr.n_total) + ' 总 / ' +
             (fr.n_valid == null ? '—' : fr.n_valid) + ' 有效'],
      ['标志', flagsText(d.flags)],
    ];
    if (mz) {
      rows.push(['迷宫', '墙 ' + (mz.n_wall == null ? '—' : mz.n_wall) +
        ' · 段 ' + (mz.n_segment == null ? '—' : mz.n_segment) +
        ' · 一致率 ' + (mz.consistency == null ? '—' : C.util.fmt(mz.consistency, 2))]);
    }
    const ir = (fr.invalid_reasons && Object.keys(fr.invalid_reasons).length)
      ? Object.keys(fr.invalid_reasons).map(function (k) {
          return k + '×' + fr.invalid_reasons[k];
        }).join(' · ')
      : '';
    if (ir) rows.push(['无效原因', ir]);
    box.innerHTML = rows.map(function (r) {
      return '<b>' + esc(r[0]) + '</b><span>' + esc(r[1]) + '</span>';
    }).join('');
  }

  async function refresh() {
    const j = await C.guard(function () { return C.api('/api/v1/video/status'); }, '读解析状态');
    if (!j) return;
    const busy = renderStatus(j);
    if (busy) poll();
    else {
      stopPoll();
      if (j.state === 'done') loadDetail(j.id);
    }
  }

  /* 轮询只在"正在跑"时开：跑完就停。**不常驻 1 Hz** —— 没人看的时候服务端不该做功。 */
  function poll() {
    if (timer) return;
    timer = setInterval(refresh, 1000);
  }
  function stopPoll() {
    if (timer) { clearInterval(timer); timer = null; }
  }

  /* ------------------------------------------------------------ 动作 */
  function numOrNull(sel) {
    const v = q(sel).value;
    if (v == null || !String(v).trim()) return null;
    const n = Number(v);
    return isFinite(n) ? n : null;
  }

  async function startRun() {
    const typed = (q('#vd-path').value || '').trim();
    const picked = (q('#vd-list').value || '').trim();
    const src = typed || picked;
    if (!src) {
      C.toast('先选一个视频，或在下面填服务器上的绝对路径', 'bad');
      return;
    }
    const body = {
      source: src,
      start_s: numOrNull('#vd-start'),
      end_s: numOrNull('#vd-end'),
      stride: numOrNull('#vd-stride'),
      maze: !!q('#vd-maze').checked,
    };
    const j = await C.guard(function () {
      return C.api('/api/v1/video/run', { method: 'POST', body: body });
    }, '开始解析');
    if (!j) return;
    sessionOf = '';                       // 新会话 ⇒ 明细要重读
    q('#vd-detail').innerHTML = '';
    renderStatus(j);
    poll();
  }

  async function cancelRun() {
    const j = await C.guard(function () {
      return C.api('/api/v1/video/cancel', { method: 'POST', body: {} });
    }, '取消解析');
    if (j) { C.toast('已请求取消（产物可能不完整）', 'warn', 8000); refresh(); }
  }

  /* ------------------------------------------------------------ 挂载 */
  function mount(host) {
    el = host;
    el.innerHTML = `
      <div class="pane">
        <h4>视频 <span class="hint" id="vd-list-note"></span>
          <span><button class="mini" id="vd-refresh">刷新列表</button></span></h4>
        <div class="row">
          <select id="vd-list" style="flex:1"></select>
        </div>
        <div class="row">
          <input id="vd-path" placeholder="或填服务器上的绝对路径（如 D:\\video\\run.mp4）" style="flex:1">
        </div>
        <div class="row wrap" style="gap:10px;align-items:center;margin-top:6px">
          <label class="hint">起点 <input id="vd-start" class="tiny" placeholder="秒" size="6"></label>
          <label class="hint">终点 <input id="vd-end" class="tiny" placeholder="秒" size="6"></label>
          <label class="hint">抽帧步长 <input id="vd-stride" class="tiny" value="1" size="4"></label>
          <label class="hint"><input type="checkbox" id="vd-maze" checked> 迷宫融合</label>
          <button class="mini primary" id="vd-run">开始解析</button>
          <button class="mini warn" id="vd-cancel" disabled>取消</button>
        </div>
      </div>
      <div class="pane" style="margin-top:8px">
        <h4>状态</h4>
        <div class="hint" id="vd-state">还没有解析过视频</div>
        <div class="errbox" id="vd-err"></div>
        <div class="kv" id="vd-out"></div>
        <div class="kv" id="vd-detail" style="margin-top:6px"></div>
      </div>`;
    q('#vd-refresh').onclick = loadVideos;
    q('#vd-run').onclick = startRun;
    q('#vd-cancel').onclick = cancelRun;
    loadVideos();
    refresh();            // 进页面就看一眼：可能上一次还在跑（刷新页面/换模块都能接上）
  }

  return {
    mount: mount,
    activate: function () { refresh(); },
  };
})();
