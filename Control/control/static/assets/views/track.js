/* ═══ 视频复盘 · 轨迹：只读 trajectory.csv，断点不连线 ═════════════════ */
(function () {
  const S = C.S, U = C.util;
  let selId = '';
  let hoverRow = null;

  function timebaseText(tb) {
    if (!tb || tb.ok == null) return { cls: 'idle', txt: '产物里没有可校验的时间信息' };
    if (tb.vfr) return { cls: 'warn', txt: '可变帧率（VFR）⇒ 只显示帧号，不显示秒' };
    if (!tb.ok) {
      const issues = (tb.issues || []).map(i => i.detail || i.code);
      return { cls: 'bad', txt: '时间轴不可信：' + (issues.join('；') || '原因未知') };
    }
    return { cls: 'ok', txt: U.fmt(tb.fps, 2) + ' fps · ' + tb.n_rows + ' 帧 · 自洽' };
  }

  function current() {
    const list = S.videoSessions;
    return list.find(s => s.id === selId) || list[0] || null;
  }

  C.VIEWS.track = {
    eyebrow: '视频复盘 · 轨迹',
    title: '轨迹',
    sub: 'x / y 来自会话产物。',

    actions() {
      const s = current();
      const tb = s ? timebaseText(s.timebase) : { cls: 'idle', txt: '无会话' };
      return '<span class="pill ' + tb.cls + '"><i></i>' + tb.txt + '</span>' +
        '<button class="btn btn-sm btn-ghost" data-t="refresh">' + C.icon('refresh') + '刷新</button>';
    },

    mount(root, actions) {
      this._root = root;
      root.innerHTML =
        '<div class="track-grid">' +
          '<section class="card">' +
            '<div class="card-head"><h2>会话轨迹</h2>' +
              '<div class="card-head-r"><span class="hint mono" id="tk-meta"></span></div></div>' +
            '<div class="hoverbar" id="tk-hover">把鼠标移到轨迹上读点</div>' +
            '<div class="track-canvas"><canvas id="tk-cv"></canvas></div>' +
            '<div class="track-legend">' +
              '<span><i></i>有效段</span>' +
              '<span><i class="bad"></i>断点（不连线）</span>' +
              '<span><i class="start"></i>起点</span>' +
              '<span><i class="end"></i>终点</span>' +
              '<span class="spacer"></span>' +
              '<span>归一化 0–1 · 不标格号</span>' +
            '</div>' +
          '</section>' +

          '<div class="stack">' +
            '<section class="card">' +
              '<div class="card-head"><h2>会话</h2>' +
                '<div class="card-head-r"><span class="hint" id="tk-count"></span></div></div>' +
              '<div class="card-body">' +
                '<div class="file-row"><select class="select" id="tk-sess" style="flex:1"></select>' +
                  '<button class="btn btn-sm" id="tk-go">去解析</button></div>' +
                '<div class="hint" id="tk-summary" style="margin-top:8px"></div>' +
              '</div>' +
            '</section>' +

            '<section class="card">' +
              '<div class="card-head"><h2>时间轴自洽</h2>' +
                '<div class="card-head-r"><span class="hint">前端不做对轴</span></div></div>' +
              '<div class="card-body">' +
                '<div class="banner" id="tk-tb" style="margin-bottom:10px"></div>' +
                '<dl class="kv compact" id="tk-tbkv"></dl>' +
                '<div class="hint" style="margin-top:8px">帧号单调、时间戳不倒流、VFR 只信帧号。' +
                  '识别侧保证时间轴一致；这里只做校验，不改数据。</div>' +
              '</div>' +
            '</section>' +

            '<section class="card">' +
              '<div class="card-head"><h2>点数</h2></div>' +
              '<div class="card-body">' +
                '<div class="stats g3" style="gap:6px">' +
                  '<div class="stat" style="padding:8px 10px"><div class="stat-label">有效</div>' +
                    '<div class="stat-value" id="tk-valid" style="font-size:19px">—</div></div>' +
                  '<div class="stat" style="padding:8px 10px"><div class="stat-label">无效</div>' +
                    '<div class="stat-value" id="tk-invalid" style="font-size:19px">—</div></div>' +
                  '<div class="stat" style="padding:8px 10px"><div class="stat-label">断点</div>' +
                    '<div class="stat-value" id="tk-breaks" style="font-size:19px">—</div></div>' +
                '</div>' +
                '<div class="hint" style="margin-top:8px">断点 = 有效 → 无效的切换次数。' +
                  '一段无效帧只会产生两个断点，不会把轨迹劈成很多段。</div>' +
              '</div>' +
            '</section>' +
          '</div>' +
        '</div>';

      root.querySelector('#tk-sess').onchange = e => {
        selId = e.target.value;
        this.paint();
      };
      root.querySelector('#tk-go').onclick = () => C.go('video');
      root.querySelector('#tk-cv').addEventListener('mousemove', e => {
        const s = current();
        const cv = root.querySelector('#tk-cv');
        if (!s || !s.rows.length) return;
        const rect = cv.getBoundingClientRect();
        const nx = (e.clientX - rect.left) / rect.width;
        const ny = (e.clientY - rect.top) / rect.height;
        let best = null, bd = 1e9;
        s.rows.forEach(r => {
          if (!r.valid) return;
          const dx = r.x - nx, dy = r.y - ny;
          const d = dx * dx + dy * dy;
          if (d < bd) { bd = d; best = r; }
        });
        if (!best || bd > 0.0045) {
          if (hoverRow) { hoverRow = null; this.paint(); }
          root.querySelector('#tk-hover').textContent = '把鼠标移到轨迹上读点';
          return;
        }
        hoverRow = best;
        const tb = s.timebase;
        root.querySelector('#tk-hover').textContent =
          (tb.trust === 'seconds' && best.t_video_s != null ? U.fmt(best.t_video_s, 2) + ' s' : '帧 ' + best.frame) +
          ' · x ' + U.fmt(best.x, 3) + ' · y ' + U.fmt(best.y, 3) +
          (best.rms_field == null ? '' : ' · rms ' + U.fmt(best.rms_field, 2));
        this.paint();
      });
      root.querySelector('#tk-cv').addEventListener('mouseleave', () => {
        if (!hoverRow) return;
        hoverRow = null; this.paint();
        root.querySelector('#tk-hover').textContent = '把鼠标移到轨迹上读点';
      });

      const rf = actions && actions.querySelector('[data-t="refresh"]');
      if (rf) rf.onclick = () => {
        C.log('video', 'info', '→ GET /api/v1/video/sessions · 零设备访问');
        this.paint();
      };

      this.paint();
    },

    paint() {
      const root = this._root;
      if (!root || !root.querySelector('#tk-sess')) return;
      const list = S.videoSessions;
      const select = root.querySelector('#tk-sess');
      root.querySelector('#tk-count').textContent = list.length + ' 个会话';

      if (!list.length) {
        select.innerHTML = '<option value="">（' + U.esc(S.health.video.root) + ' 里没有会话）</option>';
        root.querySelector('#tk-summary').textContent = '还没有解析过视频：先去【视频会话】跑一次。';
        root.querySelector('#tk-meta').textContent = '';
        root.querySelector('#tk-tb').className = 'banner idle';
        root.querySelector('#tk-tb').innerHTML = C.icon('alert') + '<div class="b-body"><b>没有可校验的时间轴</b>' +
          '<span class="b-next">先去【视频会话】解析一段视频。</span></div>';
        root.querySelector('#tk-tbkv').innerHTML = '';
        ['tk-valid', 'tk-invalid', 'tk-breaks'].forEach(id => {
          const el = root.querySelector('#' + id); if (el) el.textContent = '—';
        });
        const cv = root.querySelector('#tk-cv');
        if (cv && cv.clientWidth) C.draw.track(cv, null, {});
        return;
      }

      if (!selId || !list.some(s => s.id === selId)) selId = S.videoSel && list.some(s => s.id === S.videoSel) ? S.videoSel : list[0].id;
      select.innerHTML = list.map(s =>
        '<option value="' + U.esc(s.id) + '">' + U.esc(s.id) + ' · ' + s.n_valid + '/' + s.n_total + ' 有效</option>').join('');
      select.value = selId;

      const s = current();
      const tb = s.timebase;
      root.querySelector('#tk-meta').textContent = s.id + ' · ' + (s.fps ? U.fmt(s.fps, 2) + ' fps' : 'VFR');
      root.querySelector('#tk-summary').textContent = '有效 ' + s.n_valid + ' / ' + s.n_total + ' 点 · 视频 ' +
        s.frames + ' 帧 · ' + durText(s.dur);

      const t = timebaseText(tb);
      const bn = root.querySelector('#tk-tb');
      bn.className = 'banner ' + t.cls;
      bn.innerHTML = C.icon(tb.ok && !tb.vfr ? 'check' : 'alert') + '<div class="b-body"><b>' + t.txt + '</b>' +
        (tb.vfr ? '<span class="b-next">VFR 下秒数没有意义 ⇒ 横轴与读数只用帧号。</span>'
          : (!tb.ok ? '<span class="b-next">对轴问题归识别侧；本层不写对齐文件。</span>' : '')) + '</div>';

      root.querySelector('#tk-tbkv').innerHTML = [
        ['trust', '秒口径', tb.trust === 'seconds' ? '可信' : '不可信 · 只用帧号'],
        ['fps', '帧率', tb.fps ? U.fmt(tb.fps, 2) + ' fps' : '—'],
        ['rows', '产物行数', String(tb.n_rows == null ? '—' : tb.n_rows)],
        ['vfr', '可变帧率', tb.vfr ? '是' : '否'],
        ['issues', '问题', (tb.issues || []).map(i => i.detail || i.code).join('；') || '无'],
      ].map(r => '<div><dt>' + r[1] + ' <code>' + r[0] + '</code></dt><dd' +
        (r[0] === 'vfr' && tb.vfr ? ' class="warn"' : '') + '>' + U.esc(r[2]) + '</dd></div>').join('');

      const invalid = s.rows.filter(r => !r.valid).length;
      let breaks = 0;
      for (let i = 1; i < s.rows.length; i++) if (s.rows[i].valid !== s.rows[i - 1].valid) breaks++;
      root.querySelector('#tk-valid').textContent = String(s.n_valid);
      root.querySelector('#tk-invalid').textContent = String(invalid);
      root.querySelector('#tk-breaks').textContent = String(breaks);

      const cv = root.querySelector('#tk-cv');
      if (cv && cv.clientWidth) C.draw.track(cv, s.rows, { hover: hoverRow });
    },

    onState() { this.paint(); },
  };

  function durText(s) {
    if (s == null) return '—';
    const t = Math.max(0, Math.round(s));
    return Math.floor(t / 60) ? (Math.floor(t / 60) + ' 分 ' + (t % 60) + ' 秒') : (t + ' 秒');
  }
})();
