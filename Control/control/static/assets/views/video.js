/* ═══ 视频复盘 · 视频会话：选视频 → 子进程解析 → 产物 ═══════════════════ */
(function () {
  const S = C.S, U = C.util;
  let picked = '';
  let path = '';

  const FLAG_TEXT = {
    vfr_suspected: '时间轴不可信（疑似可变帧率）',
    no_valid_frames: '没有一帧定位成功',
    maze_inconsistent: '迷宫各段不一致（融合结果不可信）',
    maze_not_enabled: '本次未做迷宫融合',
    decode_errors: '有帧解码失败',
    maze_missing: '迷宫融合没有结果',
  };

  function flagsText(flags) {
    const list = (flags || []).map(f => FLAG_TEXT[f] ? (FLAG_TEXT[f] + '（' + f + '）') : f);
    return list.length ? list.join('；') : '无';
  }
  function sizeText(n) {
    if (n == null) return '—';
    return n >= 1024 * 1024 * 1024 ? U.fmt(n / 1024 / 1024 / 1024, 2) + ' GB' : U.fmt(n / 1024 / 1024, 0) + ' MB';
  }
  function durText(s) {
    if (s == null) return '—';
    const t = Math.max(0, Math.round(s));
    return Math.floor(t / 60) ? (Math.floor(t / 60) + ' 分 ' + (t % 60) + ' 秒') : (t + ' 秒');
  }

  C.VIEWS.video = {
    eyebrow: '视频复盘 · Auto Locate',
    title: '视频会话',
    sub: '只认服务器路径；产物 trajectory.csv。',

    actions() {
      const st = S.videoRun.state;
      const cls = st === 'running' ? 'run' : st === 'done' ? 'ok' : st === 'failed' || st === 'cancelled' ? 'bad' : 'idle';
      const txt = { running: '解析中', done: '已完成', failed: '失败', cancelled: '已取消', idle: '未开始' }[st] || st;
      return '<span class="pill ' + cls + '"><i></i>' + txt + '</span>' +
        '<button class="btn btn-sm btn-ghost" data-v="refresh">' + C.icon('refresh') + '刷新列表</button>';
    },

    mount(root, actions) {
      this._root = root;
      root.innerHTML =
        '<div class="video-grid">' +
          '<div class="stack">' +
            '<section class="card">' +
              '<div class="card-head"><h2>01 选视频</h2>' +
                '<div class="card-head-r"><span class="hint" id="vd-root"></span></div></div>' +
              '<div class="card-body">' +
                '<div class="file-pick">' +
                  '<div class="file-row">' +
                    '<select class="select" id="vd-list"></select>' +
                    '<button class="btn btn-sm" id="vd-pick">用这个</button>' +
                  '</div>' +
                  '<div class="file-row">' +
                    '<input class="input" id="vd-path" placeholder="或填服务器绝对路径（如 D:\\video\\run.mp4）" style="flex:1">' +
                  '</div>' +
                  '<div class="row wrap" style="gap:10px;margin-top:2px">' +
                    '<label class="hint">起点 <input class="input sm" id="vd-start" placeholder="秒" style="width:62px"></label>' +
                    '<label class="hint">终点 <input class="input sm" id="vd-end" placeholder="秒" style="width:62px"></label>' +
                    '<label class="hint">抽帧步长 <input class="input sm" id="vd-stride" value="1" style="width:52px"></label>' +
                    '<label class="switch"><input type="checkbox" id="vd-maze" checked> 迷宫融合</label>' +
                  '</div>' +
                  '<div class="row" style="margin-top:4px">' +
                    '<button class="btn btn-primary" id="vd-run">' + C.icon('play') + '开始解析</button>' +
                    '<button class="btn btn-danger" id="vd-cancel" disabled>取消</button>' +
                    '<span class="spacer"></span>' +
                    '<span class="hint">不做网页上传 · 拍板②</span>' +
                  '</div>' +
                '</div>' +
              '</div>' +
            '</section>' +

            '<section class="card">' +
              '<div class="card-head"><h2>02 解析状态</h2>' +
                '<div class="card-head-r"><span class="hint" id="vd-elapsed"></span></div></div>' +
              '<div class="card-body">' +
                '<div class="row" style="align-items:baseline;gap:8px">' +
                  '<b id="vd-state" style="font-size:13px">还没有解析过视频</b>' +
                  '<span class="hint" id="vd-sub"></span></div>' +
                '<div class="run-strip" id="vd-strip" style="margin-top:10px"></div>' +
                '<div class="banner bad" id="vd-err" style="display:none;margin-top:10px"></div>' +
                '<div class="sec-title" style="margin:14px 0 6px">产物</div>' +
                '<div class="out-path" id="vd-out">—</div>' +
              '</div>' +
            '</section>' +
          '</div>' +

          '<div class="stack">' +
            '<section class="card">' +
              '<div class="card-head"><h2>流水线</h2>' +
                '<div class="card-head-r"><span class="hint">本层只转交配置与读产物</span></div></div>' +
              '<div class="card-body"><div class="flow" id="vd-flow"></div></div>' +
            '</section>' +

            '<section class="card">' +
              '<div class="card-head"><h2>会话摘要</h2>' +
                '<div class="card-head-r"><span class="hint mono" id="vd-sid">—</span></div></div>' +
              '<div class="card-body"><dl class="kv compact" id="vd-detail">' +
                '<div><dt>帧</dt><dd class="dim">—</dd></div>' +
                '<div><dt>无效原因</dt><dd class="dim">—</dd></div>' +
                '<div><dt>标志</dt><dd class="dim">—</dd></div>' +
                '<div><dt>迷宫</dt><dd class="dim">—</dd></div>' +
                '</dl>' +
                '<div class="banner" style="margin-top:10px">' + C.icon('alert') +
                '<div class="b-body">解析质量归上游；本层只验产物读取与时间轴自洽。' +
                '<span class="b-next">⇒ 轨迹去【视频复盘 · 轨迹】看。</span></div></div>' +
              '</div>' +
            '</section>' +

            '<section class="card">' +
              '<div class="card-head"><h2>素材目录</h2>' +
                '<div class="card-head-r"><span class="hint mono" id="vd-count"></span></div></div>' +
              '<div class="card-body flush"><div class="vid-list" id="vd-videos"></div></div>' +
            '</section>' +
          '</div>' +
        '</div>';

      root.querySelector('#vd-list').onchange = e => { picked = e.target.value; this.paint(); };
      root.querySelector('#vd-pick').onclick = () => {
        const sel = root.querySelector('#vd-list');
        if (!sel.value) return;
        picked = sel.value;
        root.querySelector('#vd-path').value = '';
        this.paint();
      };
      root.querySelector('#vd-run').onclick = () => {
        const typed = (root.querySelector('#vd-path').value || '').trim();
        const src = typed || picked || (S.videoSessions[0] && S.videoSessions[0].video) || '';
        if (!src) { C.toast('warn', '先选一个视频', '列表里挑一个，或填服务器绝对路径。'); return; }
        C.videoStart(src);
        this.paint();
      };
      root.querySelector('#vd-cancel').onclick = () => { C.videoCancel(); this.paint(); };
      root.querySelector('#vd-path').oninput = () => {
        const typed = root.querySelector('#vd-path').value.trim();
        const b = root.querySelector('#vd-run');
        if (b) b.disabled = S.videoRun.state === 'running' || !(typed || picked || (S.videoSessions[0] && S.videoSessions[0].video));
      };
      root.querySelector('#vd-videos').onclick = e => {
        const r = e.target.closest('[data-vid]');
        if (!r) return;
        picked = r.dataset.vid;
        root.querySelector('#vd-path').value = '';
        this.paint();
      };
      const rf = actions && actions.querySelector('[data-v="refresh"]');
      if (rf) rf.onclick = () => {
        C.log('video', 'info', '→ GET /api/v1/video/list · 零设备访问');
        this.paint();
        C.toast('ok', '列表已刷新', S.videoSessions.length + ' 个视频 · ' + S.health.video.root);
      };

      this.paint();
    },

    paint() {
      const root = this._root;
      if (!root || !root.querySelector('#vd-list')) return;
      const S2 = S.videoRun;
      const sessions = S.videoSessions;
      const sel = sessions.find(s => s.video === picked) || sessions[0] || null;
      const typedPath = (root.querySelector('#vd-path') || {}).value || '';
      const hasFile = !!(typedPath.trim() || picked || (sessions[0] && sessions[0].video));

      /* 视频下拉 */
      const list = root.querySelector('#vd-list');
      list.innerHTML = sessions.length
        ? sessions.map(s => '<option value="' + U.esc(s.video) + '">' + U.esc(s.video) +
            ' · ' + sizeText(s.size) + '</option>').join('')
        : '<option value="">（' + U.esc(S.health.video.root) + ' 里没有视频）</option>';
      if (sel) list.value = sel.video;
      root.querySelector('#vd-root').textContent = S.health.video.root;

      /* 素材列表 */
      root.querySelector('#vd-videos').innerHTML = sessions.length
        ? sessions.map(s => '<button class="vid-row' + (s.video === (picked || (sel && sel.video)) ? ' on' : '') +
            '" data-vid="' + U.esc(s.video) + '">' +
            '<span><span class="v-name">' + U.esc(s.video) + '</span>' +
            '<span class="v-meta">' + sizeText(s.size) + ' · ' + durText(s.dur) + ' · ' +
            (s.fps ? U.fmt(s.fps, 2) + ' fps' : 'VFR') + '</span></span>' +
            '<span class="tag ' + (s.flags.length ? 'warn' : 'ok') + '">' + (s.flags.length ? '有标志' : '正常') + '</span>' +
            '</button>').join('')
        : '<div class="empty"><b>素材目录里没有视频</b><span>' + U.esc(S.health.video.root) +
          '</span><span>丢一个 mp4 后点【刷新列表】。</span></div>';
      root.querySelector('#vd-count').textContent = sessions.length + ' 个';

      /* 状态 */
      const st = S2.state;
      const busy = st === 'running';
      const stateEl = root.querySelector('#vd-state');
      const subEl = root.querySelector('#vd-sub');
      const strip = root.querySelector('#vd-strip');
      const runBtn = root.querySelector('#vd-run');
      const cancelBtn = root.querySelector('#vd-cancel');
      runBtn.disabled = busy || !hasFile;
      runBtn.textContent = busy ? '解析中…' : '开始解析';
      cancelBtn.disabled = !busy;

      let line = '还没有解析过视频', sub = '';
      if (st === 'running') {
        line = '解析中 · 已用 ' + durText(S2.elapsed);
        sub = '上游没有进度回调，只有用时';
      } else if (st === 'done') {
        const s = sessions.find(x => x.id === S2.id) || sessions[0];
        if (s) {
          line = '完成 · ' + s.n_total + ' 帧（有效 ' + s.n_valid + '，' +
            U.fmt(s.n_valid / s.n_total * 100, 0) + '%）';
          sub = s.video;
        }
      } else if (st === 'cancelled') {
        line = '已取消 · 产物可能不完整';
        sub = '再跑一次会覆盖同名会话';
      } else if (st === 'failed') {
        line = '失败';
        sub = S2.error || '';
      }
      stateEl.textContent = line;
      subEl.textContent = sub;
      root.querySelector('#vd-elapsed').textContent = busy ? (U.fmt(S2.elapsed, 1) + ' s') : '';

      const cur = (st === 'done' && sessions.find(x => x.id === S2.id)) || null;
      strip.innerHTML = [
        ['总帧', cur ? cur.n_total : '—', '帧'],
        ['有效', cur ? cur.n_valid : '—', '帧'],
        ['有效比例', cur ? U.fmt(cur.n_valid / cur.n_total * 100, 0) : '—', '%'],
        ['帧率', cur ? (cur.fps ? U.fmt(cur.fps, 2) : 'VFR') : '—', cur && cur.fps ? 'fps' : ''],
      ].map(x => '<div class="rs"><span>' + x[0] + '</span><b>' + x[1] +
        (x[2] ? ' <span style="font-size:9px;color:var(--muted)">' + x[2] + '</span>' : '') + '</b></div>').join('');

      const err = root.querySelector('#vd-err');
      if (st === 'failed') {
        err.style.display = '';
        err.innerHTML = C.icon('alert') + '<div class="b-body"><b>解析失败</b>' + U.esc(S2.error || '上游没给原因') +
          '<span class="b-next">⇒ 视频打不开 / 无有效帧 / 缺依赖。</span></div>';
      } else { err.style.display = 'none'; }

      /* 产物路径 */
      const out = root.querySelector('#vd-out');
      out.textContent = cur ? cur.out_dir : (busy ? '解析中；产物目录在会话结束后才可读' : '还没有产物');

      /* 摘要 */
      const sid = root.querySelector('#vd-sid');
      const det = root.querySelector('#vd-detail');
      if (cur) {
        sid.textContent = cur.id;
        const ir = Object.keys(cur.invalid_reasons || {}).map(k => k + ' × ' + cur.invalid_reasons[k]).join(' · ');
        det.innerHTML = kv([
          ['frames', '帧', (cur.n_total == null ? '—' : cur.n_total) + ' 总 / ' +
            (cur.n_valid == null ? '—' : cur.n_valid) + ' 有效'],
          ['invalid', '无效原因', ir || '无'],
          ['flags', '标志', flagsText(cur.flags)],
          // 迷宫一致性来自详情端点；列表里没有 ⇒ 显示「—」，别读 null 崩掉
          ['maze', '迷宫', cur.maze
            ? ('墙 ' + cur.maze.n_wall + ' · 段 ' + cur.maze.n_segment +
               ' · 一致率 ' + U.fmt(cur.maze.consistency, 2))
            : '—'],
          ['out', '输出目录', cur.out_dir || '—'],
        ]);
      } else {
        sid.textContent = '—';
        det.innerHTML = kv([
          ['frames', '帧', '—'],
          ['invalid', '无效原因', '—'],
          ['flags', '标志', '—'],
          ['maze', '迷宫', '—'],
          ['out', '输出目录', '—'],
        ]);
      }

      /* 流水线 */
      const s1 = (picked || (sel && sel.video)) ? 'done' : 'wait';
      const s2 = busy ? 'now' : (st === 'done' ? 'done' : (st === 'failed' ? 'bad' : 'wait'));
      const s3 = st === 'done' ? 'done' : 'wait';
      root.querySelector('#vd-flow').innerHTML =
        step(s1, '01', '视频文件', (picked || (sel && sel.video)) || '未选') + '<div class="flow-arrow">→</div>' +
        step(s2, '02', 'aruco 子进程', busy ? ('已用 ' + U.fmt(S2.elapsed, 1) + ' s') : (st === 'done' ? 'exit 0' : '待起')) + '<div class="flow-arrow">→</div>' +
        step(s3, '03', '产物', st === 'done' ? 'trajectory.csv' : '—');
    },

    onTick() {
      if (S.videoRun.state === 'running') this.paint();
    },
    onState() { this.paint(); },
  };

  function step(cls, no, name, meta) {
    return '<div class="flow-step ' + cls + '">' +
      '<span class="fs-no">' + no + '</span><b>' + name + '</b><span>' + U.esc(meta) + '</span></div>';
  }
  function kv(rows) {
    return rows.map(r =>
      '<div><dt>' + r[1] + ' <code>' + r[0] + '</code></dt><dd>' + U.esc(r[2]) + '</dd></div>').join('');
  }
})();
