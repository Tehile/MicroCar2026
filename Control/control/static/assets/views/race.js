/* ═══ 比赛：左=统一帧通道竖列；中=迷宫重构；右=发车 + 冲刺 + 状态机 ═══ */
(function () {
  const S = C.S, U = C.util;
  const CH = C.channels;

  C.VIEWS.race = {
    eyebrow: '小车控制 · MAZE RUN RACE',
    title: '比赛',
    sub: '',

    actions() {
      const t = S.tel.mazeTask;
      const cls = S.running ? 'run' : (S.estop ? 'bad' : 'idle');
      return '<span class="pill ' + cls + '"><i></i>' + (CH.tasks[t] || t) + '</span>' +
        '<span class="pill mono">x ' + U.fmt(S.tel.posX, 2) + ' · y ' + U.fmt(S.tel.posY, 2) + '</span>' +
        '<button class="btn btn-sm btn-ghost" data-r="refresh">' + C.icon('refresh') + '读取</button>';
    },

    mount(root, actions) {
      this._root = root;
      root.innerHTML =
        '<div class="race-grid">' +

          /* 左：统一帧通道（竖一栏） */
          '<div class="stack race-chan" id="rc-ch"></div>' +

          /* 中：迷宫 */
          '<section class="card">' +
            '<div class="card-head"><h2>迷宫重构</h2>' +
              '<div class="card-head-r">' +
                '<span class="tag blue">16 × 16</span>' +
                '<span class="tag" id="rc-vtag">已探明 0 格</span>' +
                '<span class="hint mono" id="rc-trail">—</span>' +
              '</div>' +
            '</div>' +
            '<div class="card-body" style="padding:10px">' +
              '<div class="maze-box"><canvas id="rc-maze"></canvas></div>' +
              '<div class="maze-foot">' +
                '<span class="legend"><i></i>已知墙</span>' +
                '<span class="legend"><i class="dash"></i>未探明</span>' +
                '<span class="legend"><i class="goal"></i>终点 2×2</span>' +
              '</div>' +
            '</div>' +
          '</section>' +

          /* 右：发车 + 冲刺 + 状态机 */
          '<div class="stack race-side">' +
            '<section class="card launch-card">' +
              '<div>' +
                '<div class="sec-title" style="margin:0 0 6px">发车前检查</div>' +
                '<div class="check-list" id="rc-checks"></div>' +
              '</div>' +
              '<button class="launch-btn" id="rc-run">' + C.icon('flagSolid') + '<span>比赛发车</span></button>' +
              '<div class="hint" id="rc-hint"></div>' +
              '<div class="act-sep"></div>' +
              '<div class="act-3">' +
                '<button class="btn wide" id="rc-recstop">' + C.icon('save') + '停止并保存</button>' +
                '<button class="btn" id="rc-mstop">收车</button>' +
                '<button class="btn btn-danger" id="rc-rescue">复位</button>' +
              '</div>' +
            '</section>' +

            '<section class="card">' +
              '<div class="card-head"><h2>比赛冲刺</h2>' +
                '<div class="card-head-r"><span class="hint">只读</span></div></div>' +
              '<div class="card-body"><dl class="kv compact" id="rc-spurt"></dl></div>' +
            '</section>' +

            '<section class="card">' +
              '<div class="card-head"><h2>迷宫状态机</h2></div>' +
              '<div class="card-body"><dl class="kv compact" id="rc-state"></dl>' +
                '<div class="banner" id="rc-note" style="margin-top:8px"></div></div>' +
            '</section>' +
          '</div>' +
        '</div>';

      root.querySelector('#rc-ch').innerHTML = CH.groups.map(g =>
        '<section class="card"><div class="card-head"><h2>' + g.name + '</h2>' +
        '<div class="card-head-r"><span class="hint mono">' + g.keys.length + '</span></div></div>' +
        '<div class="card-body" style="padding:4px 12px 8px"><dl class="kv compact">' + g.keys.map(k =>
          '<div><dt>' + k[1] + ' <code>' + k[0] + '</code></dt>' +
          '<dd data-k="' + k[0] + '">—</dd></div>').join('') + '</dl></div></section>').join('');

      root.querySelector('#rc-run').onclick = () => {
        if (S.running) { C.toast('warn', '已在跑', '先【收车】'); return; }
        C.act('launch');
        this.paint(true);
      };
      root.querySelector('#rc-mstop').onclick = () => { C.act('mazeStop'); this.paint(true); };
      root.querySelector('#rc-rescue').onclick = () => { C.act('rescue'); this.paint(true); };
      root.querySelector('#rc-recstop').onclick = () => { C.act('saveRecord'); };
      const rf = actions && actions.querySelector('[data-r="refresh"]');
      if (rf) rf.onclick = () => {
        C.log('mouse', 'info', '→ GET maze/state');
        this.paint(true);
      };

      this.paint(true);
    },

    paint(first) {
      const root = this._root;
      if (!root || !root.querySelector('#rc-checks')) return;
      const t = S.tel;

      root.querySelectorAll('[data-k]').forEach(d => {
        d.textContent = CH.text(d.dataset.k, t[d.dataset.k]);
      });

      const rec = S.session.active;
      const checks = [
        { ok: t.battMv > 7000, label: '电池', val: U.fmt(t.battMv, 0) + ' mV' },
        { ok: !S.estop, label: '急停', val: S.estop ? '锁存' : '未锁' },
        { ok: !S.running && (t.mazeTask === 0 || t.mazeTask === 6), label: '任务机', val: CH.tasks[t.mazeTask] || String(t.mazeTask) },
        { ok: rec, label: '记录', val: rec ? (S.session.frames + ' 帧') : '发车自动开录' },
      ];
      root.querySelector('#rc-checks').innerHTML = checks.map(c =>
        '<div class="check-row">' +
        '<span class="ck' + (c.ok ? '' : ' no') + '">' + C.icon(c.ok ? 'check' : 'alert') + '</span>' +
        '<span>' + c.label + '</span>' +
        '<span class="v">' + c.val + '</span></div>').join('');

      const btn = root.querySelector('#rc-run');
      btn.disabled = S.running || S.estop || !S.health.mouse.connected;
      btn.classList.toggle('running', S.running);
      btn.querySelector('span').textContent = S.running ? '已发车' : '比赛发车';

      root.querySelector('#rc-hint').textContent = !S.health.mouse.connected ? '未连接电脑鼠'
        : S.estop ? '急停锁存 · 先解锁'
        : S.running ? '跑位中 · 收车 0.2~1 s 退出'
        : '预检通过';

      root.querySelector('#rc-spurt').innerHTML = kv([
        ['spurtMode', '冲刺档位', (CH.spurt[t.spurtMode] || t.spurtMode) + ' (' + t.spurtMode + ')'],
        ['spurtFlag', '冲刺标志', t.spurtFlag ? '是 (1)' : '否 (0)'],
        ['goOrBack', '去 / 回', t.goOrBack ? '回程 (1)' : '去程 (0)'],
        ['spurtTestFlag', '冲刺试探', t.spurtTestFlag ? '是 (1)' : '否 (0)'],
        ['goaheadnum', '前冲计数', String(t.goaheadnum)],
      ]);

      root.querySelector('#rc-state').innerHTML = kv([
        ['mazeTask', '迷宫任务', (CH.tasks[t.mazeTask] || t.mazeTask) + ' (' + t.mazeTask + ')'],
        ['motionTask', '动作', (CH.motions[t.motionTask] || t.motionTask) + ' (' + t.motionTask + ')'],
        ['dir', '朝向', (CH.dirs[t.dir] || '?') + ' (' + t.dir + ')'],
        ['posX/posY', '位置', U.fmt(t.posX, 2) + ', ' + U.fmt(t.posY, 2)],
        ['GOAL', '终点区', '(7,7)–(8,8)'],
      ]);

      const note = root.querySelector('#rc-note');
      const task = t.mazeTask;
      let n = '', k = '';
      if (!S.health.mouse.connected) { k = 'warn'; n = '未连接 · 读数为缓存'; }
      else if (task === 0) n = 'WAIT · 可发车';
      else if (task === 1) n = 'START · 读 Flash 地图选档';
      else if (task === 2) n = 'SEARCH · 2 s 未出会软复位';
      else if (task === 3 || task === 4 || task === 5) { k = 'ok'; n = '冲刺态'; }
      else if (task === 6) { k = 'ok'; n = 'END · 可收车'; }
      else { k = 'warn'; n = '未知任务机 ' + task; }
      note.className = 'banner ' + k;
      note.innerHTML = C.icon('alert') + '<div class="b-body">' + n + '</div>';

      root.querySelector('#rc-vtag').textContent = '已探明 ' + Object.keys(C.maze.visited).length + ' 格';
      root.querySelector('#rc-trail').textContent = '轨迹 ' + C.maze.idx + '/' + (C.maze.path.length - 1);
      const cv = root.querySelector('#rc-maze');
      if (cv && cv.clientWidth) C.draw.maze(cv);
    },

    onTick() {
      this._n = (this._n || 0) + 1;
      if (this._n % 2) return;
      this.paint();
    },
    onState() { this.paint(true); },
  };

  function kv(rows) {
    return rows.map(r =>
      '<div><dt>' + r[1] + ' <code>' + r[0] + '</code></dt><dd>' + U.esc(r[2]) + '</dd></div>').join('');
  }
})();
