/* ═══ 比赛：左=统一帧通道竖列；中=迷宫重构；右=发车 + 冲刺 + 状态机 ═══ */
(function () {
  const S = C.S, U = C.util;
  const CH = C.channels;

  /* 收车/复位这条"待办"的标题（mount 与 paint 共用一份，别两处各写一句） */
  function pendTitle(kind) { return kind === 'reset' ? '复位' : '收车'; }
  function pendBusy(kind) { return kind === 'reset' ? '复位中…' : '收车中…'; }
  /* 动作码 → 文字。**走 `C.channels.motionText`**（后端 `/caps` 下发的码表），
     不再 `CH.motions[v] || v` —— 那个 `|| v` 会把未知码显示成裸数字。 */
  function motionName(v) {
    if (v == null) return '—';
    return typeof CH.motionText === 'function' ? CH.motionText(v) : (CH.motions[v] || v);
  }
  /* 帧龄 → 一句话。`null` = 从没来过（不是"刚刚"）。 */
  function fmtAge(ms) {
    if (ms == null) return '从没来过';
    return ms >= 1000 ? U.fmt(ms / 1000, 1) + ' s' : U.fmt(ms, 0) + ' ms';
  }

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
                '<span class="tag blue" id="rc-dim">16 × 16</span>' +
                '<span class="tag" id="rc-msrc">等待数据</span>' +
                '<span class="tag" id="rc-vtag">已探明 0 格</span>' +
                '<button class="btn btn-sm btn-ghost" id="rc-clear"' +
                  ' title="只清这张图，记录与设备都不动">清空</button>' +
              '</div>' +
            '</div>' +
            '<div class="card-body maze-body" style="padding:10px">' +
              /* 改版四十：图例与建图诊断压成**紧凑两行**放在图下面。
                 试过把图例搬进右侧窄栏（图是正方形，两侧本来有空白），实测**更糟**：
                 1400×710 下中栏只有 ~466 px 宽，窄栏吃掉 186 px ⇒ 宽度成了新的瓶颈，
                 地图从 351 掉到 300。地图边长是 `min(可用高, 可用宽)`，**只能在
                 已经富余的那条边上省空间**，而这里富余的是高度。所以还是放下面，
                 但把原来的"图例一行 + 诊断两三行"压成一紧凑行：省下的垂直线全部归地图。 */
              '<div class="maze-box"><canvas id="rc-maze"></canvas></div>' +
              '<div class="maze-foot">' +
                /* 图例标签一律短到 2~3 字：括号里的解释（"探索前沿"/"无数据"）留在
                   `title` 里 —— 界面上多一个字就可能把这一行挤成两行，而这一页
                   每多 1 px 文字，正方形地图就少 1 px。 */
                '<span class="legend" title="已确认为墙的面"><i></i>已知墙</span>' +
                '<span class="legend" title="只在探索前沿画点线：这一段还没确认过"><i class="dash"></i>未探明</span>' +
                '<span class="legend" title="车没走到过、也没有任何数据（不等于空地）"><i class="gray"></i>没走到</span>' +
                '<span class="legend" title="终点 2×2 格，由后端锁死"><i class="goal"></i>终点 2×2</span>' +
                '<span class="maze-src mono" id="rc-trail">—</span>' +
              '</div>' +
              '<div class="maze-diag mono" id="rc-mstat">—</div>' +
              '<div class="hint" id="rc-mwarn"></div>' +
            '</div>' +
          '</section>' +

          /* 右：发车 + 冲刺 + 状态机 */
          '<div class="stack race-side">' +
            '<section class="card launch-card">' +
              /* 拉取小面板（改版四十一，四十七改口径）：拉取**默认常开**，这一页
                 不再有"进页开一次"的特权；面板留在这里是因为发车前要能一眼看到
                 帧龄/帧率，以及那个人工【结束】的位置（手动停粘住到本会话结束）。
                 面板压成一行塞在卡片顶部：中栏地图吃高度，右栏一滚就看不到发车按钮。 */
              '<div id="rc-pull"></div>' +
              '<div>' +
                '<div class="sec-title" style="margin:0 0 6px">发车前检查</div>' +
                '<div class="check-list" id="rc-checks"></div>' +
              '</div>' +
              '<button class="launch-btn" id="rc-run">' + C.icon('flagSolid') + '<span>比赛发车</span></button>' +
              '<div class="hint" id="rc-hint"></div>' +
              '<div class="act-sep"></div>' +
              '<div class="act-3">' +
                /* 改版四十：这个按钮要能变成"已保存 · <名> · N 帧" ——
                   车自己停下时后端**已经**收尾落盘了（`_maybe_finish_maze_rec`），
                   不需要人再点一次。文案与门控全在 `paint()` 里，按 `S.rec` 走。 */
                '<button class="btn wide" id="rc-recstop">' + C.icon('save') +
                  '<span>停止并保存</span></button>' +
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
                '<div class="banner" id="rc-note" style="margin-top:8px"></div>' +
                /* 停止原因（改版四十）：`STOPPED <reason>` 是唯一能回答"为什么停"的
                   来路 —— 时间护栏到与红外护栏触发在帧里长得一模一样。
                   没停过就不显示（空着，不写"未知"，那会像一条故障）。 */
                '<div class="banner ok" id="rc-stop" style="margin-top:8px;display:none"></div></div>' +
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
        if (this._pend) { C.toast('warn', pendTitle(this._pend.kind) + '中', '等这一条走完再发车。'); return; }
        C.act('launch');
        this.paint(true);
      };
      /* ── 收车 / 复位（改版四十三：**点击即有反馈，停流放到确认之后**）────────
         旧写法有两个错：
           ① 点下去之后界面**一声不响**（要等后端 3 s 命令 + 2.5 s 等新帧才回话），
              而且当时 `maze_stop` 在流已停时永远等不到新帧 ⇒ "点了没反应"；
           ② 收车还**先停流**，等于为确认先把帧源掐掉。
         现在：`_startPend()` 当拍进 pending（按钮禁用 + 文案 + toast），
         命令回来后进**观察窗**（不停流），看到动作码"没在动"才收尾停流。 */
      root.querySelector('#rc-mstop').onclick = () => {
        if (this._pend) return;          // 一条链路只排一条：重复点被吞（不是"再发一次"）
        this._startPend('stop');
      };
      root.querySelector('#rc-rescue').onclick = () => {
        if (this._pend) return;
        this._startPend('reset');
      };
      root.querySelector('#rc-recstop').onclick = () => { C.act('saveRecord'); };

      /* 拉取小面板（与实时页同一套门控：切 N 必须先停流） */
      this._pp = C.pullPanel(root.querySelector('#rc-pull'));
      /* 收车/复位这条待办（改版四十三）。`null` = 没有在等任何东西。
         `{kind:'stop'|'reset', at, phase:'cmd'|'watch', t0, res}`：
           cmd   = 命令已下发、还没回来（这期间界面必须给反馈，不许静默）；
           watch = 命令回来了、在**观察窗**里等"车真的停"（这期间**不停流**）。 */
      this._pend = null;
      /* 清空：只清**这张图**（后端 `reset_live_map`），记录、设备、拉取都不动。
         凭什么敢直接删（不二次确认）：它**可重建**——继续跑几秒图就回来了，
         而且清的只是"我看到了什么"，不是任何落盘的东西。 */
      const cl = root.querySelector('#rc-clear');
      if (cl) cl.onclick = () => {
        C.clearMazeView().then((okc) => {
          if (!okc) return;
          S.mazeView = null;
          this.paint(true);
        });
      };
      const rf = actions && actions.querySelector('[data-r="refresh"]');
      if (rf) rf.onclick = () => {
        C.log('mouse', 'info', '→ GET maze/state');
        this.paint(true);
      };

      fitStage();
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
      /* 发车前检查改成**三态**（改版四十）：`ok===null` = 判不了（还没读到帧）。
         原来 `t.battMv > 7000` 在没数据时算 false ⇒ 画成一个红叉"电池"，
         看起来像"电池电压不够"，而真相是"根本没连上"。 */
      const checks = [
        { ok: t.battMv == null ? null : t.battMv > 7000, label: '电池',
          val: t.battMv == null ? '—' : U.fmt(t.battMv, 0) + ' mV' },
        { ok: !S.estop, label: '急停', val: S.estop ? '锁存' : '未锁' },
        { ok: t.mazeTask == null ? null : (!S.running && (t.mazeTask === 0 || t.mazeTask === 6)),
          label: '任务机', val: CH.tasks[t.mazeTask] || (t.mazeTask == null ? '—' : String(t.mazeTask)) },
        { ok: rec, label: '记录', val: rec ? (S.session.frames + ' 帧') : '发车自动开录' },
      ];
      root.querySelector('#rc-checks').innerHTML = checks.map(c =>
        '<div class="check-row">' +
        '<span class="ck' + (c.ok === null ? ' unk' : (c.ok ? '' : ' no')) + '">' +
        C.icon(c.ok === null ? 'x' : (c.ok ? 'check' : 'alert')) + '</span>' +
        '<span>' + c.label + '</span>' +
        '<span class="v">' + c.val + '</span></div>').join('');

      /* ── 发车键（改版四十三）────────────────────────────────────────────
         `S.running` 现在读**动作码**（不再读迷宫任务）；pending 期间也禁用 ——
         那是"车上还有一条命令在飞"，不是"按钮坏了"。
         ⚠️ **灰着必须说得出为什么**：同一个字符串既进 `title` 也进 `#rc-hint`，
         两处各写一句就会出现"灰着但没人说原因"（旧版 hint 只看三个条件、
         真正的置灰理由却可能是 `S.running` 被一帧旧数据翻起来）。 */
      this._watchTick();                 // 先判一次观察窗（可能就在这一拍收尾）
      const btn = root.querySelector('#rc-run');
      const pend = this._pend;
      const mt = t.motionTask;
      let why = '';
      if (!S.health.mouse.connected) why = '未连接电脑鼠';
      else if (pend) why = pendTitle(pend.kind) + '中 · 等设备确认';
      else if (S.estop) why = '急停锁存 · 先解锁';
      else if (S.running) why = '跑位中（动作 ' + motionName(mt) + '） · 先【收车】';
      btn.disabled = !!why;
      btn.title = why || '发车：先确认拉取在跑，再下发 MAZE RUN RACE';
      btn.classList.toggle('running', S.running);
      btn.querySelector('span').textContent = S.running ? '已发车' : '比赛发车';

      root.querySelector('#rc-hint').textContent = why || '预检通过';

      /* 收车 / 复位两个键的 pending 态：**点下去当拍就要有变化**。
         以前这三个键在点击后的 2 s 内一个字都不变（探针实测），看起来像没点上。 */
      const mb = root.querySelector('#rc-mstop');
      if (mb) {
        mb.disabled = !!pend || !S.health.mouse.connected;
        mb.textContent = (pend && pend.kind === 'stop') ? pendBusy('stop') : '收车';
        mb.title = pend ? ('等' + pendTitle(pend.kind) + '走完（≤'
          + (pend.kind === 'stop' ? S.watchS.stop : S.watchS.reset) + ' s）')
          : '收车：MAZE STOP（复位发车状态 + 解锁急停）';
      }
      const rbR = root.querySelector('#rc-rescue');
      if (rbR) {
        rbR.disabled = !!pend || !S.health.mouse.connected;
        rbR.textContent = (pend && pend.kind === 'reset') ? pendBusy('reset') : '复位';
        rbR.title = pend ? ('等' + pendTitle(pend.kind) + '走完')
          : '整机软复位（物理位置不变，复位后请放回起点角）';
      }

      /* ── 【停止并保存】= 记录器状态的镜像（改版四十）───────────────────────
         为什么不再是一个单纯的按钮：后端在**车停 / 回 WAIT / 断链 / 超帧数**时
         已经自动收尾落盘了。界面若还写着"停止并保存"，人会去点它，
         然后得到一句"当前没有在记录"—— 那看起来像"保存失败了"。
         三态：
           在记        → 可点（提前收尾）
           正在落盘    → 不可点，写"保存中…"
           已落盘      → 不可点，写"已保存 · <名> · N 帧"
           连后端都没  → 不可点（不知道在不在记，不许假装能停）
         ⚠️ **不重复发 `record/stop`**：收尾是后端的决定，界面只读结果。 */
      const rb = root.querySelector('#rc-recstop');
      if (rb) {
        const R = S.rec;
        const lbl = rb.querySelector('span');
        rb.className = 'btn wide';
        if (R.on) {
          lbl.textContent = '停止并保存';
          rb.disabled = false;
          rb.title = '提前收尾并落盘（车自己停了后端也会自动存）';
        } else if (R.saving) {
          lbl.textContent = '保存中…';
          rb.disabled = true;
          rb.title = '记录器正在写盘';
        } else if (R.last && R.last.ok) {
          /* ⚠️ "已保存 · 名 · N 帧"是**中间态**（改版四十一）：只显示 4 s，
             之后必须回落成按钮本来的"停止并保存"。
             原来它一旦成立就永久停在那里 ⇒ 下次要用这个按钮时，按钮上写着
             上一份的名字、还是灰的，人只能猜"是不是已经存好了、我还能不能停"。 */
          const fresh = R.savedAt && (Date.now() - R.savedAt) < 4000;
          if (fresh) {
            lbl.textContent = '已保存 · ' + shortName(R.last.name) + ' · ' + (R.last.frames || 0) + ' 帧';
            rb.disabled = true;
            rb.className = 'btn wide ok';
            rb.title = '已落盘（' + String(R.last.name || '') + '）· 路径 ' + String(R.last.path || '');
          } else {
            lbl.textContent = '停止并保存';
            rb.disabled = true;
            rb.className = 'btn wide';
            rb.title = '当前没有在记录 · 上一份已保存（' + shortName(R.last.name) + '）；发车会自动开录';
          }
        } else if (R.last && R.last.ok === false) {
          lbl.textContent = '没记成 · ' + shortName(R.last.note || '没有收到帧');
          rb.disabled = true;
          rb.className = 'btn wide warn';
          rb.title = String(R.last.note || '');
        } else {
          lbl.textContent = '停止并保存';
          rb.disabled = true;
          rb.title = C.LIVE.on ? '当前没有在记录（发车会自动开录）' : '未连后端 · 判不了在不在记';
        }
      }
      /* 停止原因：只报**这一次**的话（`last_stop` 在新一跑开始时被后端清掉） */
      const stopEl = root.querySelector('#rc-stop');
      if (stopEl) {
        const txt = String(S.rec.stopText || '');
        if (txt) {
          const at = S.rec.stopAt ? ' · ' + U.hhmmss(S.rec.stopAt) : '';
          const code = String(S.rec.stopReason || '');
          stopEl.style.display = '';
          stopEl.className = 'banner ok';
          stopEl.innerHTML = C.icon('check') + '<div class="b-body"><b>已停车</b> '
            + U.esc(txt) + (code && code !== txt ? '（' + U.esc(code) + '）' : '') + at + '</div>';
        } else {
          stopEl.style.display = 'none';
        }
      }

      root.querySelector('#rc-spurt').innerHTML = kv([
        ['spurtMode', '冲刺档位', (CH.spurt[t.spurtMode] || t.spurtMode) + ' (' + t.spurtMode + ')'],
        ['spurtFlag', '冲刺标志', t.spurtFlag ? '是 (1)' : '否 (0)'],
        ['goOrBack', '去 / 回', t.goOrBack ? '回程 (1)' : '去程 (0)'],
        ['spurtTestFlag', '冲刺试探', t.spurtTestFlag ? '是 (1)' : '否 (0)'],
        ['goaheadnum', '前冲计数', String(t.goaheadnum)],
      ]);

      root.querySelector('#rc-state').innerHTML = kv([
        ['mazeTask', '迷宫任务', (CH.tasks[t.mazeTask] || t.mazeTask) + ' (' + t.mazeTask + ')'],
        ['motionTask', '动作', motionName(t.motionTask) + ' (' + t.motionTask + ')'],
        ['dir', '朝向', (CH.dirs[t.dir] || '?') + ' (' + t.dir + ')'],
        ['posX/posY', '位置', U.fmt(t.posX, 2) + ', ' + U.fmt(t.posY, 2)],
        ['GOAL', '终点区', '(7,7)–(8,8)'],
      ]);

      const note = root.querySelector('#rc-note');
      const task = t.mazeTask;
      let n = '', k = '';
      if (!S.health.mouse.connected) {
        /* 改版四十三（用户裁决）：断开就**清空并标注**，不保留旧图。
           旧图留着会被当成"当前的墙"读 —— 而它只是上一段跑出来的历史，
           连着的时候看着一样、断开之后含义完全变了（"这里没墙" vs "这里不知道"）。 */
        k = 'warn'; n = '未连接 · 无数据（上图已清空，不拿上一段的墙当现状）';
      }
      else if (task === 0) n = 'WAIT · 可发车';
      else if (task === 1) n = 'START · 读 Flash 地图选档';
      else if (task === 2) n = 'SEARCH · 2 s 未出会软复位';
      else if (task === 3 || task === 4 || task === 5) { k = 'ok'; n = '冲刺态'; }
      else if (task === 6) { k = 'ok'; n = 'END · 可收车'; }
      else { k = 'warn'; n = '未知任务机 ' + task; }
      note.className = 'banner ' + k;
      note.innerHTML = C.icon('alert') + '<div class="b-body">' + n + '</div>';

      /* 迷宫：**只用设备重构出来的图**（`GET /mouse/maze/reconstruct` 的 `view`）。
         没取到就画一张**空白网格** —— 初始状态本来就该是空白，不许拿原型那张
         随机生成的迷宫冒充（"一进来就有一条路径"，等于编了一个没发生过的走法）。 */
      const mv = S.mazeView || null;
      const N = (mv && mv.N) || S.mazeN || 16;
      /* 空白网格：**墙矩阵要是真的 N×N（全 0）**，不能给空数组 ——
         `draw.maze` 会按 y/x 下标取 `m.v[y][x]`，空数组在 (0,0) 处就抛
         "Cannot read properties of undefined"（真机就是这么整页渲染失败的）。 */
      const blank = function (n) {
        const v = [], h = [];
        for (let y = 0; y < n; y++) {
          v.push(new Array(Math.max(0, n - 1)).fill(0));
          h.push(new Array(Math.max(0, n - 1)).fill(0));
        }
        return v;
      };
      const view = mv || {
        N: N, goal: (N >= 9 ? [[7, 7], [7, 8], [8, 7], [8, 8]] : []),
        v: blank(N), h: blank(N), visited: {}, trail: [],
      };
      const car = {
        x: (S.tel.posX == null ? .5 : S.tel.posX + .5),
        y: (S.tel.posY == null ? .5 : S.tel.posY + .5),
        dir: (S.tel.dir == null ? 0 : S.tel.dir),
      };
      const nv = Object.keys(view.visited || {}).length;
      const nTotal = view.cells_total || (view.N * view.N);
      root.querySelector('#rc-dim').textContent = view.N + ' × ' + view.N;
      root.querySelector('#rc-vtag').textContent = '已探明 ' + nv + (nTotal ? ' / ' + nTotal : '') + ' 格';
      /* 这张图是哪条来路（一元素一真相）：`mapd` = 固件增量（v10 事实），
         `ring` = 老固件退回主机推断。**不许不标** —— 两种图的"未探明"含义不同，
         混在一起看会得出相反结论（"这里没墙" vs "这里不知道"）。 */
      const mstat = (mv && mv.stats) || null;
      const mdiff = (mv && mv.diff) || null;
      const msrc = (mv && mv.src) || '';
      const sEl = root.querySelector('#rc-msrc');
      if (sEl) {
        sEl.textContent = msrc === 'mapd' ? '固件增量' : (msrc === 'ring' ? '主机推断' : '等待数据');
        sEl.className = 'tag' + (msrc === 'mapd' ? ' blue' : '');
      }
      const msEl = root.querySelector('#rc-mstat');
      if (msEl) {
        /* 紧凑行（改版四十）：原来是"一行 ` · ` 串联"，七项在 466 px 里会折两三行，
           白白吃掉地图的高度（地图边长 = 可用高与可用宽取小）。这里改成
           `标签 值` 成对排、可换行但每对不拆断，实测一行放得下，省下的高度全给图。 */
        msEl.innerHTML = mstat
          ? [['条目', mstat.entries], ['重锚', mstat.resync], ['序号断', mstat.seq_gaps],
             ['环溢出', mstat.lossy], ['坐标差', mstat.pos_mismatch],
             ['面冲突', mstat.face_flip],
             ['对账', mdiff ? (mdiff.mismatch + '/' + mdiff.common) : '—']]
            .map(r => '<span class="ms-i">' + r[0] + '<b>' + r[1] + '</b></span>')
            .join('')
          : '—';
      }
      const mwEl = root.querySelector('#rc-mwarn');
      if (mwEl) {
        const warn = [];
        if (!mv) warn.push('');
        else if (msrc !== 'mapd') {
          warn.push('这台设备的统一帧里没有建图增量（v9 及更早）⇒ 这张图是主机按车身朝向推断的，'
            + '"未探明"只能表达成"没走到"');
        } else if (mstat) {
          if (!mstat.entries) warn.push('增量通道还没有条目：车还没走过新格子');
          if (mstat.seq_gaps) warn.push('帧序号断过 ' + mstat.seq_gaps + ' 次 ⇒ 可能有格子没收到（图上缺墙）');
          if (mstat.lossy) warn.push('固件环溢出 ' + mstat.lossy + ' 帧 ⇒ 更早的格子已经丢了');
          if (mstat.pos_mismatch) warn.push('环排空时最后一条与当前坐标相差 ≥2 格 ' + mstat.pos_mismatch
            + ' 次 ⇒ 有格子没被报上来（图上缺墙）');
          /* ⚠️ `对账` **不在这里告警**，只在上面的数里报。
             它是两条**独立来路**的分歧数，而主机那套（相对朝向 + 远近墙两档推断）
             本来就比固件的绝对坐标粗 —— 实测一次跑下来 3/47、更长的跑到 54/370，
             **恒非 0 是预期**。做成告警就成了常亮红灯，现场会当噪音忽略；
             它的价值在**突变**（3 突然跳到 50 ⇒ 八成是映射写错了），那要跟上一跑比，
             界面比不了，留给复盘看记录。
             所以只在**分歧过半**时才提一句 —— 那时已经不是"粗细之差"了。 */
          if (mdiff && mdiff.common >= 8 && mdiff.mismatch * 2 >= mdiff.common) {
            warn.push('对账：' + mdiff.mismatch + '/' + mdiff.common
              + ' 面与主机推断不一致（过半）⇒ 可能是方向映射写反了，先别信这张图');
          }
          /* 建图余量：规格要求运图时 N≥2。判据来自上游（`MAPD_MIN_PULL_N`），
             这里只提示、**不自动改档** —— 档位是人设的，程序不该偷偷调。 */
          if (S.running && S.pullN < 2) warn.push('N=' + S.pullN
            + '：冲刺 129 ms/格时建图余量只有 1.3 倍，跟不上就会丢格（规格要求 N≥2）');
        }
        mwEl.textContent = warn.join('；');
      }
      const trEl = root.querySelector('#rc-trail');
      trEl.textContent = mv
        ? ('轨迹 ' + (view.trail || []).length + ' 点 · ' + (mv.frames_total || 0) + ' 帧'
           + (mv.covered_s ? ' · ' + U.fmt(mv.covered_s, 0) + ' s' : ''))
        : (S.health.mouse.connected ? '等设备帧' : '未连接 · 无帧');
      /* 这一行是 `flex-wrap:nowrap` + 省略号：窄窗口下可能被截，所以把全文放 `title`。
         不给 `title` 就成了"数字被截掉还看不出截了"。 */
      trEl.title = trEl.textContent;
      const cv = root.querySelector('#rc-maze');
      /* ⚠️ 这里**不能**再用 `cv.clientWidth` 当"布局好了没"的闸：
         画布宽度现在完全由 `fitMaze()` 写进 `style.width`（CSS 里那条 `width:100%`
         已删，否则它会把边长拉成地图格的宽、跟高度算出来的边长打架）。
         `clientWidth` 在首帧必然是 0 ⇒ 变成"永远不画"的死锁。闸改成 `cap` 非 0。 */
      const cap = cv ? fitMaze(cv) : 0;
      if (cap) {
        C.draw.maze(cv, {
          N: view.N, goal: view.goal || [], v: view.v || [], h: view.h || [],
          visited: view.visited || {}, trail: view.trail || [],
          path: [],                      // 实时图没有"规划路径"，只有走过的 trail
          car: car,
        }, cap);
      }

      /* 拉取小面板（改版四十一）：与实时页同源的数字，只是压成一行。 */
      if (this._pp) this._pp.paint();
      /* 改版四十七：**收车/停车/记录收尾都不再关流**（拉取默认常开，只有人按
         【结束】才停）。这里原先记 `_wasRun`/`_wasRec` 的跳变去停流 —— 两个字段
         与那三个分支一起删掉了：留着它们只会让下一个人以为"这页还在管拉取"。 */
    },

    /* ── 收车/复位：点击即反馈，停流放到确认之后（改版四十三）────────────────
       旧顺序是「点击 → 命令 → 等新帧 → 回执 → **立刻停流**」：等于为了确认先把
       帧源掐掉，还要最长 5.5 s（3 s 命令 + 2.5 s 等新帧）静默无反馈。现在：
         ① 点击**当拍**给反馈（按钮禁用 + 文案 + toast），不等后端；
         ② 命令回来**不停流**，进观察窗，每拍看**动作码**；看到"没在动"就算收住；
         ③ 确认后才停流；超时才停流，并**如实**说没等到 + 给出下一步。
       ⚠️ 停流不能提前，也不能延后太久：半手动时人可能早就停了流，那时**没有帧可判**——
          后端 0.3 s 内就回"判不了"（`waited=false`），界面当场照实说，不干等。 */
    _startPend(kind) {
      const now = Date.now();
      const isStop = kind === 'stop';
      this._pend = { kind: kind, at: now, phase: 'cmd', t0: now, res: null };
      C.log('mouse', 'info', isStop ? '→ MAZE STOP · 收车已下发'
                                    : '→ MCU RESET · 复位已下发');
      C.toast('', isStop ? '收车已下发' : '复位已下发',
        '等设备回待命（≤' + (isStop ? S.watchS.stop : S.watchS.reset) + ' s）');
      this.paint(true);
      const p = C.act(isStop ? 'mazeStop' : 'rescue');
      const done = (d) => {
        if (!this._pend || this._pend.at !== now) return;   // 这一轮已被接管
        /* 命令失败走的是 `{error}`（不是 reject —— 见 `lib.js` 的 `mazeStop`）：
           失败是**结论**，不是异常；但界面必须当场说，不许把它当成"判不了"。 */
        if (d && d.error) { bad(d); return; }
        this._pend.phase = 'watch';
        this._pend.t0 = Date.now();
        this._pend.res = d || null;
        if (isStop) {
          /* 后端三种结论，界面**照抄**，不许自己猜（三态纪律）：
               reliable=true  → 它等到过新帧且任务机已回待命 ⇒ 当场收尾；
               reliable=false → 新帧说"还没退出来" ⇒ 进观察窗等它；
               reliable=null  → 判不了。**没有帧源时不要等满 8 s**（那是假等待）。 */
          if (d && d.reliable === true) {
            C.pinRunning(false);
            this._settle(true, '已收车', '任务机回待命 · ' + this._stateText(d));
            return;
          }
          const noSrc = (d && d.waited === false) || !S.pulling;
          if (noSrc && !(d && d.reliable === false)) {
            this._settle(false, '收车已下发 · 判不了',
              '没有拉到新帧可判（帧龄 ' + fmtAge(d && d.frame_age_ms)
              + '） · 点【开始】拉一次帧即可确认');
            return;
          }
        } else {
          /* 复位：后端已经复核过 `rst=SW` ⇒ 不必等帧（复位后约 3 s 才有首帧）。
             没核实到才进观察窗等首帧。 */
          const okRst = !!(d && (d.verified || d.rst === 'SW'));
          if (okRst) {
            this._settle(true, '已复位', '设备已重启 · 物理位置不变，发车前请放回起点角');
            return;
          }
          if (!S.pulling) {
            this._settle(false, '复位已下发 · 判不了',
              '没拉到帧确认重启（' + ((d && d.status) || '无回执') + '） · 看串口是否还在');
            return;
          }
        }
        this.paint(true);                // 进观察窗：按钮保持 pending，hint 说在等什么
      };
      const bad = (e) => {
        if (!this._pend || this._pend.at !== now) return;
        const why = (e && (e.detail || e.message)) || '命令未返回结果';
        this._pend = null;
        C.toast('bad', isStop ? '收车失败' : '复位失败', why);
        this.paint(true);
      };
      if (p && p.then) p.then(done, bad); else done(null);
    },

    /* 收尾（成功/判不了/超时都走这里）。**不在这里调 `paint()`**：
       它会在 `paint()` 里被 `_watchTick()` 调到，重入一次 paint 只会烧掉一帧。 */
    _settle(ok, title, note) {
      this._pend = null;
      C.toast(ok ? 'ok' : 'warn', title, note || '');
      if (!ok) C.log('mouse', 'warn', title + (note ? ' · ' + note : ''));
    },

    /* 观察窗的每拍检查（由 `paint()` 顶部调；1 s 心跳 + 每次数据变化都会进来）。
       判据是**动作码**：车真停 ⇔ 动作机说停（`S.motionStop`，由 `/caps` 下发）。
       用 `mazeTask` 判会漏：车停稳后流程态可能还停在高位。 */
    _watchTick() {
      const p = this._pend;
      if (!p || p.phase !== 'watch') return;
      const stop = p.kind === 'stop';
      /* ⚠️ 等确认期间断了链路 ⇒ **不许**把"归零的动作码"当成"车停了"。
         `resetTelemetry()` 在断开时会把 `motionTask` 清成 0（那是"没数据"的占位），
         拿它判 `stopped` 就会报一个假成功 —— 这正是"判据假绿"的典型形态。 */
      if (!S.health.mouse.connected) {
        this._settle(false, (stop ? '收车' : '复位') + '未确认',
          '等确认期间链路断开 · ' + (S.health.mouse.linkDead
            ? '后端已判链路死，需断电 3~5 s 再重连' : '重连串口后再确认'));
        return;
      }
      const mt = S.tel.motionTask;
      const stopped = (mt != null) && S.motionStop.indexOf(mt | 0) >= 0;
      const limit = (stop ? S.watchS.stop : S.watchS.reset) * 1000;
      const spent = Date.now() - (p.t0 || p.at);
      if (stopped) {
        if (stop) {
          C.pinRunning(false);
          this._settle(true, '已收车', '动作机 ' + motionName(mt) + ' · 任务机 '
            + (CH.tasks[S.tel.mazeTask] || S.tel.mazeTask));
          return;
        }
        /* 复位：动作码"空闲"还必须**配一帧新的**（复位前那一帧也可能是空闲），
           并且给 0.8 s 的起步时间，否则会在设备还没重启完时就下结论。
           ⚠️ 用 `S.telRxAt`（**帧到达的墙钟**）判"新"，不用 `t.ageMs`：
              那是后端推帧那一刻的龄期，恒接近 0，拿它判"刚来过"永远成立。 */
        const fresh = S.telRxAt && (Date.now() - S.telRxAt) < 1500;
        if (spent > 800 && fresh) {
          this._settle(true, '已复位', '首帧已到 · 物理位置不变，发车前请放回起点角');
          return;
        }
      }
      if (spent >= limit) {
        this._settle(false,
          stop ? '收车未确认' : '复位未确认',
          '等了 ' + (limit / 1000) + ' s 没看到待命 · '
          + (stop ? '点【开始】拉一次帧可确认，或再点一次【收车】'
                  : '检查串口是否还在（复位后可能掉线）'));
      }
    },

    /* 后端回执里的任务机/动作，压成一句给人看的（`—` = 后端没给） */
    _stateText(d) {
      const st = (d && d.state) || null;
      if (!st) return '状态判不了';
      const t = CH.tasks[st.task];
      const mo = motionName(st.motion);
      return '任务机 ' + (t || st.task) + ' · 动作 ' + mo;
    },
    onTick() {
      /* 高度先量：窗口大小 / 日志坞展开收起都会变，而这三个区是"一边跑一边看"的。
         每拍都量（两次 `getBoundingClientRect`，代价可忽略），值没变就不碰 DOM。 */
      fitStage();
      this._n = (this._n || 0) + 1;
      if (this._n % 2) return;
      this.paint();
    },
    onState() { this.paint(true); },
  };

  /* 地图边长由**中栏卡片自己的可用高度**定（改版四十）。
     原先量的是 `body.getBoundingClientRect().top + window.scrollY` —— **视口相对位移**。
     可这一页原来整页滚：往下滚 top 变小 ⇒ `avail` 变大 ⇒ 每 250 ms 的 `onTick`
     重画就换一个边长。**"滚轮会缩放地图"就是这么来的，代码里根本没有缩放功能。**
     现在三列各滚各的（`.main` 固定高度 + 栏内 `overflow:auto`），中栏卡片由 flex
     分配到一个确定高度 ⇒ 直接读 `clientHeight`，与滚动彻底解耦。

     画布是正方形，宽度再富余也用不上，所以只能由**较紧的那一边**定：
       可用高 = 正文内高 − 上下 padding − 图例行 − 诊断行（都实测）− 1 px
       可用宽 = 正文内宽
     ⚠️ 这两行必须**实测 `offsetHeight`**，不能写常数：诊断那一行在有统计时是七个数
        （可能折行）、没统计时只有一个 `—`，告警块也是有时才出现。写死就等于
        "图要么被裁、要么白留一截"。
     ⚠️ 也别想着把图例搬到右边省高度 —— 试过了，中间栏只有 ~466 px 宽，
        窄栏占 186 px 后宽度变成瓶颈，地图反而从 351 掉到 300（实测）。
        正方形只吃 `min(高, 宽)`，能省的必须是**已经富余**的那条边。
     下限 220：再小连 16×16 的格子线都糊成一片。 */
  function fitMaze(cv) {
    const body = (cv && cv.closest) ? cv.closest('.card-body') : null;
    if (!body) return 0;
    const cs = getComputedStyle(body);
    const padV = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
    const padH = (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0);
    let below = 0;
    body.querySelectorAll('.maze-foot, .maze-diag, #rc-mwarn').forEach(function (e) {
      /* 告警块没话说时是空的：`:empty` 高度本来就是 0，但 margin 仍在 ⇒ 用 offsetHeight 判。 */
      if (e.offsetHeight > 0) below += e.offsetHeight + 4;
    });
    const availH = body.clientHeight - padV - below - 1;
    const availW = body.clientWidth - padH;
    if (availH <= 0 || availW <= 0) return 0;      // 还没布局好：这一拍不画
    return Math.max(220, Math.min(availH, availW));
  }

  /* 主区高度 = 视口 − 顶栏 − 日志坞（实测，**不写常数**）。实现收在 `lib.js` 的
     `fitMain()`（记录页也用同一条口径，两处不要各写一份）。
     每次 `onTick` 都量一次，值没变就不碰 DOM（避免无谓的重排）。 */
  function fitStage() {
    C.fitMain();
  }

  /* 记录名去掉后缀：`20261024_210412_mz7.maze.json` → `20261024_210412_mz7`。
     按钮上要留得住（一行），完整名字在 `title` 里。判不了就给 `—`。 */
  function shortName(s) {
    const v = String(s == null ? '' : s).replace(/\.maze\.json$/, '');
    return v || '—';
  }

  function kv(rows) {
    return rows.map(r =>
      '<div><dt>' + r[1] + ' <code>' + r[0] + '</code></dt><dd>' + U.esc(r[2]) + '</dd></div>').join('');
  }
})();
