/* ═══ 总览：下一步判断 + 系统结构图（两条来路，一份记录） ═══════════════ */
(function () {
  const S = C.S, U = C.util;

  const LANES = [
    {
      id: 'mouse', no: '01', cls: 'blue', name: '小车控制', up: 'RemoteControl 2.13.0',
      mods: [['live', '实时状态'], ['params', '参数设定'], ['race', '比赛'], ['maint', '维护自检']],
      foot() {
        const m = S.health.mouse;
        /* 拉取停了就没有帧率可言 —— 别把最后一帧的 Hz 当成还在跑 */
        return [m.connected ? m.port : '未连接', '30 B 帧',
          S.pulling ? U.fmt(S.tel.rate, 1) + ' Hz' : '拉取已结束'];
      },
    },
    {
      id: 'video', no: '02', cls: 'green', name: '视频复盘', up: 'Auto Locate · 会话解析',
      mods: [['video', '视频会话'], ['track', '轨迹']],
      foot() {
        const n = S.videoSessions.length;
        const s = S.videoSessions[0];
        return [n + ' 个会话', s ? s.frames + ' 帧' : '无素材', s && s.fps ? U.fmt(s.fps, 2) + ' fps' : '帧号口径'];
      },
    },
    {
      id: 'data', no: '03', cls: 'violet', name: '记录', up: '本层 · 比赛记录',
      mods: [['data', '比赛记录'], ['replay', '回放']],
      foot() {
        const n = S.records.length;
        return [n + ' 条', '发车 ' + S.records.filter(r => r.launch).length,
          '视频 ' + S.records.filter(r => r.video).length];
      },
    },
  ];

  const QUICK = [['TELEM', '遥测流'], ['PING', '链路'], ['STOP', '软停'],
    ['TARE', '零位'], ['MAP', '读迷宫'], ['GET ALL', '全量拉参']];
  const SRC = { mouse: '电脑鼠', video: '视频', control: '本层' };

  C.VIEWS.overview = {
    eyebrow: '总览',
    title: '总览',
    sub: '',

    actions() {
      const ok = S.readiness.filter(r => r.ok).length;
      return '<span class="pill ' + (ok === S.readiness.length ? 'ok' : 'warn') + '"><i></i>' +
        ok + ' / ' + S.readiness.length + ' 就绪</span>' +
        '<button class="btn btn-sm btn-ghost" data-ov="refresh">' + C.icon('refresh') + '刷新</button>';
    },

    mount(root, actions) {
      this._root = root;
      root.innerHTML =
        '<div class="grid g-2-1 stretch">' +
          '<div class="stack">' +
            '<div id="ov-next"></div>' +
            '<div class="ready" id="ov-ready"></div>' +
          '</div>' +
          '<div class="session" id="ov-sess"></div>' +
        '</div>' +

        '<div class="sec-title">系统结构</div>' +
        '<div class="lanes" id="ov-lanes"></div>' +

        '<div class="grid g-2-1 stretch" style="margin-top:12px">' +
          '<section class="card fill">' +
            '<div class="card-head"><h2>快捷指令</h2>' +
              '<div class="card-head-r"><span class="hint" id="ov-quick-hint"></span></div></div>' +
            '<div class="card-body qb">' +
              '<div class="quick-grid" id="ov-quick">' +
                QUICK.map(q => '<button data-q="' + q[0] + '"><b>' + q[0] + '</b><em>' + q[1] + '</em></button>').join('') +
              '</div>' +
              '<div class="row" style="margin-top:10px;border-top:1px solid var(--line);padding-top:10px">' +
                '<span class="mono" style="color:var(--muted)">&gt;</span>' +
                '<input class="input" id="ov-cmd" placeholder="手打一条命令（如 TELEM 100）" spellcheck="false" style="flex:1">' +
                '<button class="btn btn-primary btn-sm" id="ov-send">发送</button>' +
              '</div>' +
            '</div>' +
          '</section>' +
          '<section class="card">' +
            '<div class="card-head"><h2>最近事件</h2>' +
              '<div class="card-head-r"><span class="hint mono">最近 5 条</span></div></div>' +
            '<div class="card-body" style="padding-top:6px"><div class="evt-list" id="ov-evt"></div></div>' +
          '</section>' +
        '</div>';

      root.querySelectorAll('[data-q]').forEach(b => {
        b.onclick = function () { C.act('raw', b.dataset.q); };
      });
      const cmd = root.querySelector('#ov-cmd');
      const send = () => {
        const txt = (cmd.value || '').trim();
        if (!txt) { cmd.focus(); return; }
        C.act('raw', txt);
        cmd.value = '';
      };
      root.querySelector('#ov-send').onclick = send;
      cmd.onkeydown = e => { if (e.key === 'Enter') send(); };

      const rf = actions && actions.querySelector('[data-ov]');
      if (rf) rf.onclick = function () {
        C.log('control', 'info', '刷新状态 · health 200 · 11 ms');
        C.toast('ok', '已刷新', '两路设备与视频目录已重读');
        C.emit('state');
      };

      root.querySelector('#ov-ready').onclick = e => {
        const it = e.target.closest('[data-dev]');
        if (!it) return;
        if (it.dataset.dev === 'video') C.go('video'); else openDevice(it.dataset.dev);
      };
      root.querySelector('#ov-lanes').onclick = e => {
        const it = e.target.closest('[data-go]');
        if (it) C.go(it.dataset.go);
      };
      root.querySelector('#ov-next').onclick = e => {
        const b = e.target.closest('[data-next]');
        if (!b) return;
        const k = b.dataset.next;
        if (k === 'mouse') openDevice('mouse');
        else if (k === 'unlock') C.act('unlock');
        else if (k === 'race') C.go('race');
        else if (k === 'video') C.go('video');
        else if (k === 'clock') openDevice('clock');
      };
      root.querySelector('#ov-sess').onclick = e => {
        const b = e.target.closest('[data-sess]');
        if (!b) return;
        if (b.dataset.sess === 'start') C.sessionStart(); else C.sessionStop();
        C.emit('state');
      };

      this.paint();
    },

    paint() {
      const root = this._root;
      if (!root || !root.querySelector('#ov-ready')) return;
      const t = S.tel;

      /* 下一步：一屏只有一个判断 */
      const n = nextStep();
      root.querySelector('#ov-next').innerHTML =
        '<div class="next-block ' + n.cls + '" data-next="' + n.act + '" style="cursor:pointer">' +
          '<div>' +
            '<div class="nb-k">下一步 · ' + n.kicker + '</div>' +
            '<b>' + n.title + '</b>' +
            '<span class="nb-x">' + n.body + '</span>' +
          '</div>' +
          '<div class="nb-go">→</div>' +
        '</div>';

      /* 准备度：三项，只有电脑鼠拦记录 */
      root.querySelector('#ov-ready').innerHTML = S.readiness.map(r => {
        const cls = r.ok ? 'ok' : (r.blocking ? 'bad' : 'warn');
        return '<button class="ready-cell ' + cls + '" data-dev="' + r.key + '">' +
          '<span class="rc-top"><span class="sq"></span><b>' + r.name + '</b>' +
            (r.blocking ? '<span class="tag" style="margin-left:auto">拦记录</span>' : '') +
          '</span>' +
          '<span class="rc-state">' + U.esc(r.state) + '</span>' +
          '<span class="rc-next">' + (r.ok ? '已就绪' : (r.action ? r.action + ' →' : '去处理 →')) + '</span>' +
          '</button>';
      }).join('');

      /* 会话 */
      const se = S.session;
      const card = root.querySelector('#ov-sess');
      if (se.active) {
        card.innerHTML =
          '<div class="s-top"><span>本次会话</span><span class="tag">录制中</span></div>' +
          '<div class="s-clock" id="ov-timer">' + U.clock(Date.now() - se.t0) + '</div>' +
          '<div class="s-id">' + se.id + '</div>' +
          '<div class="s-meta">' +
            '<div><span>已验帧</span><b id="ov-frames">' + se.frames + '</b></div>' +
            '<div><span>账本</span><b>通过</b></div>' +
            '<div><span>缺口</span><b>0</b></div>' +
          '</div>' +
          '<div class="s-btn"><button class="btn" data-sess="stop">' + C.icon('stop') + '停止记录</button></div>';
      } else {
        card.innerHTML =
          '<div class="s-top"><span>本次会话</span><span class="tag">无会话</span></div>' +
          '<div class="s-clock" style="color:rgba(255,255,255,.28)">00:00:00</div>' +
          '<div class="s-id">' + (S.health.mouse.connected ? '待命' : '未连接电脑鼠') + '</div>' +
          '<div class="s-meta">' +
            '<div><span>已验帧</span><b>—</b></div>' +
            '<div><span>账本</span><b>—</b></div>' +
            '<div><span>缺口</span><b>—</b></div>' +
          '</div>' +
          '<div class="s-btn"><button class="btn" data-sess="start"' +
            (S.health.mouse.connected ? '' : ' disabled') + '>' + C.icon('play') + '开始记录</button></div>';
      }

      /* 结构图 */
      root.querySelector('#ov-lanes').innerHTML = LANES.map((L, li) =>
        '<section class="lane ' + L.cls + '">' +
          '<div class="lane-head">' +
            '<span class="lane-no">' + L.no + '</span>' +
            '<div class="lane-t"><h3>' + L.name + '</h3><span>' + L.up + '</span></div>' +
            stateTag(L.id) +
          '</div>' +
          '<div class="lane-body">' + L.mods.map((m, i) =>
            '<button class="modlink" data-go="' + m[0] + '">' +
              '<span class="idx">' + L.no + '.' + (i + 1) + '</span>' +
              '<span>' + m[1] + '</span><span class="arrow">→</span></button>').join('') +
          '</div>' +
          '<div class="lane-foot">' + L.foot().map(f => '<span>' + U.esc(f) + '</span>').join('') + '</div>' +
        '</section>').join('');

      const qh = root.querySelector('#ov-quick-hint');
      if (qh) qh.textContent = S.health.mouse.connected ? '回车即发 · 不支持的上游会回 ERR' : '电脑鼠未连接';
      root.querySelectorAll('#ov-quick button').forEach(b => { b.disabled = !S.health.mouse.connected; });
      this.paintEvt();
    },

    paintEvt() {
      const box = document.getElementById('ov-evt');
      if (!box) return;
      const rows = S.log.slice(-5).reverse();
      box.innerHTML = rows.map(function (r) {
        const cls = r.level === 'error' ? 'bad' : r.level === 'warn' ? 'warn' : '';
        return '<div class="evt"><span class="evt-t">' + U.hhmmss(r.t) + '</span>' +
          '<span class="tag ' + cls + '">' + (SRC[r.src] || r.src) + '</span>' +
          '<span class="evt-x">' + U.esc(r.text) + '</span></div>';
      }).join('') || '<div class="hint" style="padding:8px 2px">还没有事件。</div>';
    },

    onTick() {
      const t = document.getElementById('ov-timer');
      if (t && S.session.active) t.textContent = U.clock(Date.now() - S.session.t0);
      const f = document.getElementById('ov-frames');
      if (f && S.session.active) f.textContent = S.session.frames;
      if (this._evtLen !== S.log.length) { this._evtLen = S.log.length; this.paintEvt(); }
    },

    onState() { this.paint(); },
  };

  /* 只有一个判断：按“能不能动手”的优先级排 */
  function nextStep() {
    if (!S.health.mouse.connected) {
      return { cls: 'bad', act: 'mouse', kicker: 'BLOCKED',
        title: '串口未连接', body: '发车、参数、维护不可用' };
    }
    if (S.estop) {
      return { cls: 'bad', act: 'unlock', kicker: 'SAFETY',
        title: '急停锁存', body: '电机已断电，先解锁' };
    }
    if (S.running) {
      return { cls: 'ok', act: 'race', kicker: 'RUNNING',
        title: '跑位中 · ' + U.fmt(S.tel.posX, 1) + ', ' + U.fmt(S.tel.posY, 1),
        body: '收车在【比赛】页' };
    }
    if (!S.videoSessions.length) {
      return { cls: 'warn', act: 'video', kicker: 'NO TRACK',
        title: '没有视频会话', body: '位置数据只能来自视频复盘' };
    }
    if (!S.health.timesync.armed) {
      return { cls: 'warn', act: 'clock', kicker: 'TIMEBASE',
        title: '时间基未置位', body: '记录时间列按 assumed' };
    }
    return { cls: 'ok', act: 'race', kicker: 'READY',
      title: '预检通过', body: '去比赛页发车' };
  }

  function stateTag(id) {
    if (id === 'mouse') {
      if (!S.health.mouse.connected) return '<span class="pill warn sm"><i></i>未连接</span>';
      return S.running ? '<span class="pill run sm"><i></i>运行</span>'
        : '<span class="pill ok sm"><i></i>已连接</span>';
    }
    if (id === 'video') {
      const n = S.videoSessions.length;
      if (S.videoRun.state === 'running') return '<span class="pill run sm"><i></i>解析中</span>';
      if (!n) return '<span class="pill warn sm"><i></i>无素材</span>';
      return '<span class="pill ok sm"><i></i>' + n + ' 会话</span>';
    }
    return S.session.active ? '<span class="pill run sm"><i></i>录制</span>'
      : '<span class="pill idle sm"><i></i>待录</span>';
  }

  function openDevice(kind) {
    const chip = document.querySelector('.devchip[data-dev="' + kind + '"]');
    if (chip) chip.click();
  }
})();
