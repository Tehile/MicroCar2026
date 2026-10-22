/* ═══ 记录回放：时间轴拖动 + 迷宫位置 + 状态 + 参数快照 ═══════════════ */
(function () {
  const S = C.S, U = C.util;
  const CH = C.channels;

  let curId = '';
  let i = 0;
  let playing = false;
  let speed = 1;

  function dur(sec) {
    const m = Math.floor(sec / 60), r = sec % 60;
    return String(m).padStart(2, '0') + ':' + U.fmt(r, 1).padStart(4, '0');
  }

  /* 采样缺口的短文案（进度条上的带子 / 信息栏那行共用一份，别写两处口径）。
     `why` 只有一种：`launch_cmd` = 发车命令独占链路（上游 urgent 语义）。
     其余原因**不猜** —— 真丢帧看 `gap`（seq_lost），别在这里编。 */
  function gapText(g) {
    const ms = (g && g.ms) || 0;
    const core = U.fmt(ms / 1000, 1) + ' s 无采样';
    return g && g.why === 'launch_cmd' ? core + ' · 发车占链路' : core;
  }

  function gapsInfo(rec) {
    const gs = rec.gaps || [];
    if (!gs.length) return '无';
    const max = gs.reduce((a, b) => (b.ms > (a || 0) ? b.ms : a), 0);
    const why = gs.some(g => g.why === 'launch_cmd') ? '（含发车占链路）' : '';
    // 计数取后端的 `gaps_n`（明细有留存上限）；**两个数都写出来**，
    // 免得"明细只留 200 条"被读成"只有 200 处"。
    const n = (rec.gapsN == null ? gs.length : rec.gapsN);
    return n + ' 处 · 最长 ' + U.fmt(max / 1000, 1) + ' s' + why +
      (n > gs.length ? '（明细只留前 ' + gs.length + ' 条）' : '');
  }

  C.VIEWS.replay = {
    eyebrow: '记录 · 回放',
    title: '回放',
    sub: '',

    actions() {
      return '<button class="btn btn-sm btn-ghost" data-rp="back">← 比赛记录</button>' +
        '<span class="pill mono" id="rp-pill">—</span>';
    },

    mount(root, actions) {
      this._root = root;
      const rec = C.record(S.recSel);
      if (!rec) { root.innerHTML = '<div class="banner warn">' + C.icon('alert') + '<div class="b-body">没有记录</div></div>'; return; }
      // 回放数据是**进页时另取**的（一条上千帧）。没取到就如实说，不画一辆停在原点的假车。
      if (!rec.samples || !rec.samples.length) {
        root.innerHTML = '<div class="banner warn">' + C.icon('alert') +
          '<div class="b-body"><b>' + rec.id + ' 还没取到回放数据</b>\n' +
          '到「比赛记录」页刷新一下，或确认后端在跑</div></div>';
        C.loadReplay(rec.id);
        return;
      }
      if (curId !== rec.id) { curId = rec.id; i = 0; playing = false; speed = 1; }

      root.innerHTML =
        '<div class="replay-grid">' +
          '<div class="stack">' +
            '<section class="card">' +
              '<div class="card-head"><h2>位置</h2>' +
                '<div class="card-head-r">' +
                  '<span class="tag blue" id="rp-id">' + rec.id + '</span>' +
                  '<span class="hint mono" id="rp-pos">—</span>' +
                '</div></div>' +
              '<div class="card-body" style="padding:10px">' +
                '<div class="maze-box rp-maze"><canvas id="rp-maze"></canvas></div>' +
                '<div class="maze-foot">' +
                  '<span class="legend"><i></i>已知墙</span>' +
                  '<span class="legend"><i class="dash"></i>未探明</span>' +
                  '<span class="legend"><i class="goal"></i>终点 2×2</span>' +
                  (rec.launch ? '' : '<span class="spacer"></span><span>无发车记录 · 无位置</span>') +
                '</div>' +
              '</div>' +
            '</section>' +

            '<section class="card">' +
              '<div class="card-head"><h2>时间轴</h2>' +
                '<div class="card-head-r"><span class="mono" id="rp-tc">00:00.0 / ' + dur(rec.dur) + '</span></div></div>' +
              '<div class="card-body">' +
                '<div class="scrub-wrap">' +
                  '<input type="range" class="scrub" id="rp-range" min="0" max="' + (rec.samples.length - 1) + '" value="0">' +
                  '<div class="scrub-marks" id="rp-marks"></div>' +
                '</div>' +
                '<div class="row wrap" style="margin-top:10px">' +
                  '<button class="btn btn-primary" id="rp-play">' + C.icon('play') + '<span>播放</span></button>' +
                  '<div class="seg" id="rp-speed">' +
                    '<button data-sp="1" class="on">1×</button>' +
                    '<button data-sp="2">2×</button>' +
                    '<button data-sp="4">4×</button>' +
                  '</div>' +
                  '<button class="btn btn-sm" data-step="-4">−1 s</button>' +
                  '<button class="btn btn-sm" data-step="4">+1 s</button>' +
                  '<span class="spacer"></span>' +
                  '<span class="hint mono" id="rp-frame">帧 —</span>' +
                '</div>' +
              '</div>' +
            '</section>' +
          '</div>' +

          '<div class="stack">' +
            '<section class="card">' +
              '<div class="card-head"><h2>状态</h2>' +
                '<div class="card-head-r"><span class="hint mono" id="rp-t">t 0.0 s</span></div></div>' +
              '<div class="card-body"><dl class="kv compact" id="rp-state"></dl></div>' +
            '</section>' +

            '<section class="card">' +
              '<div class="card-head"><h2>参数</h2>' +
                '<div class="card-head-r"><span class="hint mono">发车前快照</span></div></div>' +
              '<div class="card-body flush"><div class="tbl-scroll" style="max-height:320px">' +
                '<table class="tbl rp-tbl"><thead><tr><th style="width:34px">ID</th>' +
                '<th style="width:118px">名称</th><th>值</th></tr></thead>' +
                '<tbody id="rp-rows"></tbody></table></div></div>' +
            '</section>' +

            '<section class="card">' +
              '<div class="card-head"><h2>记录</h2></div>' +
              '<div class="card-body"><dl class="kv compact" id="rp-info"></dl></div>' +
            '</section>' +
          '</div>' +
        '</div>';

      const range = root.querySelector('#rp-range');
      range.oninput = () => {
        i = Number(range.value);
        playing = false;
        this.paint();
      };
      root.querySelector('#rp-play').onclick = () => {
        /* ⚠️ 门控看**有没有样本**，不是"有没有发车记录"。
           `launch` 只说明这条记录是发车自动开的还是人手动开的；手动开的记录
           帧里照样有 posX/posY，也能回放。以前拿 `launch` 当门，手动记录一律
           点不动 —— 而界面上给出的理由是"只有空闲遥测"，那是在替数据下结论。 */
        if (!rec.samples || rec.samples.length < 2) {
          C.toast('warn', '回放不了', '这条记录不足两帧');
          return;
        }
        if (i >= rec.samples.length - 1) i = 0;
        playing = !playing;
        this.paint();
      };
      root.querySelector('#rp-speed').onclick = e => {
        const b = e.target.closest('[data-sp]');
        if (!b) return;
        speed = Number(b.dataset.sp);
        root.querySelectorAll('#rp-speed button').forEach(x => x.classList.toggle('on', x === b));
      };
      root.querySelectorAll('[data-step]').forEach(b => {
        b.onclick = () => {
          i = U.clamp(i + Number(b.dataset.step), 0, rec.samples.length - 1);
          playing = false;
          this.paint();
        };
      });
      const back = actions && actions.querySelector('[data-rp="back"]');
      if (back) back.onclick = () => C.go('data');

      /* 发车刻度 + **采样缺口**（改版三十八）。
         ⚠️ 缺口画成**带子**（有宽度），不是 1px 刻度：它是一段"没有数据"的区间，
         画成一条线会被读成"某个时刻发生了什么"。位置用的是 `t_ms` 同一口径
         （后端已经把绝对墙钟减成相对毫秒），所以和进度条/帧号严格对齐。 */
      const marks = [];
      if (rec.launch) marks.push({ p: (rec.launchAt + 1) / rec.dur, t: '发车' });
      const durMs = Math.max(1, (rec.dur || 0) * 1000);
      (rec.gaps || []).forEach(g => {
        const a = U.clamp((g.t_ms_from || 0) / durMs, 0, 1);
        const b = U.clamp((g.t_ms_to || 0) / durMs, 0, 1);
        marks.push({ a: a, b: b, t: gapText(g), gap: true });
      });
      marks.push({ p: 1, t: '收车' });
      root.querySelector('#rp-marks').innerHTML = marks.map(m => m.gap
        ? '<i class="gap" style="left:' + (m.a * 100).toFixed(2) + '%;width:' +
          Math.max(0.5, (m.b - m.a) * 100).toFixed(2) + '%"><em>' + U.esc(m.t) + '</em></i>'
        : '<i style="left:' + (m.p * 100).toFixed(2) + '%"><em>' + m.t + '</em></i>').join('');

      /* 参数：只在挂载时写一次。
         `rec.params === null` = **这条记录没冻过快照**（改版二十八之前的老记录）。
         如实写「未记录」—— 拿当前参数顶上就是"发车时的参数"造假。 */
      root.querySelector('#rp-rows').innerHTML = !rec.params
        ? '<tr><td colspan="3" class="dim">这条记录没有发车前参数快照（老记录）。' +
          '新记录会在开录时冻结一份。</td></tr>'
        : rec.params.map(g =>
        '<tr class="rp-grp"><td colspan="3">' + g.name + ' · id ' + g.range + '</td></tr>' +
        g.items.map(it => '<tr><td class="num dim">' + it.id + '</td>' +
          '<td class="name"><b>' + it.name + '</b></td>' +
          '<td class="num">' + U.fmt(it.value, it.value % 1 ? (String(it.value).split('.')[1] || '').length : 0) +
          (it.unit ? ' <span class="dim" style="font-size:9.5px">' + it.unit + '</span>' : '') + '</td></tr>').join('')
      ).join('');

      this.paint(true);
    },

    paint(first) {
      const root = this._root;
      if (!root || !root.querySelector('#rp-range')) return;
      const rec = C.record(S.recSel);
      if (!rec) return;

      i = U.clamp(i, 0, rec.samples.length - 1);
      const sm = rec.samples[i];
      const range = root.querySelector('#rp-range');
      if (range.value !== String(i)) range.value = String(i);

      root.querySelector('#rp-tc').textContent = dur(sm.t) + ' / ' + dur(rec.dur);
      root.querySelector('#rp-frame').textContent = '帧 ' + sm.frame;
      root.querySelector('#rp-t').textContent = 't ' + U.fmt(sm.t, 1) + ' s';
      const pill = document.getElementById('rp-pill');
      if (pill) pill.textContent = playing ? '播放 ' + speed + '×' : '暂停';

      const pb = root.querySelector('#rp-play');
      pb.innerHTML = C.icon(playing ? 'stop' : 'play') + '<span>' + (playing ? '暂停' : '播放') + '</span>';

      const posEl = root.querySelector('#rp-pos');
      posEl.textContent = rec.launch
        ? 'x ' + U.fmt(sm.posX, 2) + ' · y ' + U.fmt(sm.posY, 2) + ' · ' + (CH.dirs[sm.dir] || '?')
        : '无位置';

      root.querySelector('#rp-state').innerHTML = [
        ['t', '时刻', U.fmt(sm.t, 2) + ' s'],
        ['frame', '帧号', String(sm.frame)],
        ['posX/posY', '位置', U.fmt(sm.posX, 2) + ', ' + U.fmt(sm.posY, 2)],
        ['dir', '朝向', (CH.dirs[sm.dir] || '?') + ' (' + sm.dir + ')'],
        ['mazeTask', '迷宫任务', (CH.tasks[sm.mazeTask] || sm.mazeTask) + ' (' + sm.mazeTask + ')'],
        ['motionTask', '动作', (CH.motions[sm.motionTask] || sm.motionTask) + ' (' + sm.motionTask + ')'],
        ['speedL/speedR', '轮速', U.fmt(sm.speedL, 1) + ' / ' + U.fmt(sm.speedR, 1) + ' cm/s'],
        ['wallL/F/R', '墙', sm.wallLeft + ' / ' + sm.wallFront + ' / ' + sm.wallRight],
        ['battMv', '电池', U.fmt(sm.battMv, 0) + ' mV'],
        ['flags', '标志位', CH.text('flags', sm.flags)],
      ].map(r => '<div><dt>' + r[1] + ' <code>' + r[0] + '</code></dt><dd>' + U.esc(r[2]) + '</dd></div>').join('');

      const ses = S.videoSessions.find(v => v.id === rec.video) || null;
      root.querySelector('#rp-info').innerHTML = [
        ['start', '开始', rec.start],
        ['dur', '时长', dur(rec.dur)],
        ['frames', '遥测帧', String(rec.frames)],
        ['launch', '发车记录', rec.launch ? '有 @' + U.fmt(rec.launchAt, 1) + ' s' : '无'],
        ['video', '视频记录', rec.video || '未关联'],
        ['points', '轨迹点', ses ? String(ses.n_valid) : '—'],
        ['ledger', '账本', rec.ledger === true ? '通过'
          : rec.ledger === false ? ('差 ' + (rec.gap == null ? '?' : rec.gap) + ' 帧')
            : '判不了（这条记录没有丢帧计数）'],
        // 采样缺口（改版三十八）：与上面那行**分开**——那行是"丢帧"，这行是
        // "这段没请求"（发车命令独占链路）。混成一句就再也分不清"没采"和"丢了"。
        ['gaps', '采样缺口', gapsInfo(rec)],
      ].map(r => '<div><dt>' + r[1] + ' <code>' + r[0] + '</code></dt><dd' +
        (r[0] === 'ledger' && !rec.ledger ? ' class="bad"' : '') + '>' + U.esc(r[2]) + '</dd></div>').join('');

      const cv = root.querySelector('#rp-maze');
      if (cv && cv.clientWidth) C.draw.maze(cv, C.recordView(rec, i));
    },

    onTick() {
      const root = this._root;
      if (!root || !root.querySelector('#rp-range')) return;
      const rec = C.record(S.recSel);
      if (!rec) return;
      if (!playing) return;
      i += speed;
      if (i >= rec.samples.length - 1) { i = rec.samples.length - 1; playing = false; }
      this.paint();
    },
    onState() { this.paint(); },
  };
})();
