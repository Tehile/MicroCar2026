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
                '<span class="tag blue" id="rc-dim">16 × 16</span>' +
                '<span class="tag" id="rc-msrc">等待数据</span>' +
                '<span class="tag" id="rc-vtag">已探明 0 格</span>' +
                '<button class="btn btn-sm btn-ghost" id="rc-clear"' +
                  ' title="只清这张图，记录与设备都不动">清空</button>' +
              '</div>' +
            '</div>' +
            '<div class="card-body" style="padding:10px">' +
              '<div class="maze-box"><canvas id="rc-maze"></canvas></div>' +
              /* 图例与"这张图攒了多久"同一行：轨迹/帧数/秒数属于**这张图的来路**，
                 放在卡头会把标题挤成两行（"迷宫重 / 构"），放在图例右侧正好。 */
              '<div class="maze-foot">' +
                '<span class="legend"><i></i>已知墙</span>' +
                '<span class="legend"><i class="dash"></i>未探明</span>' +
                '<span class="legend"><i class="gray"></i>没走到（无数据）</span>' +
                '<span class="legend"><i class="goal"></i>终点 2×2</span>' +
                '<span class="hint mono maze-src" id="rc-trail">—</span>' +
              '</div>' +
              /* 建图诊断（改版三十九）：这六个数回答"这张图可不可信" ——
                 条目为 0 说明还没走过新格；洞 > 0 说明缺了一段；溢出/重锚是
                 固件环的自我说明；对账是两条独立来路的分歧数。 */
              '<div class="hint mono" id="rc-mstat" style="margin-top:6px">—</div>' +
              '<div class="hint" id="rc-mwarn" style="margin-top:3px"></div>' +
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
        msEl.textContent = mstat
          ? ['条目 ' + mstat.entries, '重锚 ' + mstat.resync, '序号断 ' + mstat.seq_gaps,
             '环溢出 ' + mstat.lossy, '坐标差 ' + mstat.pos_mismatch,
             '面冲突 ' + mstat.face_flip,
             '对账 ' + (mdiff ? (mdiff.mismatch + '/' + mdiff.common) : '—')].join(' · ')
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
      root.querySelector('#rc-trail').textContent = mv
        ? ('轨迹 ' + (view.trail || []).length + ' 点 · ' + (mv.frames_total || 0) + ' 帧'
           + (mv.covered_s ? ' · ' + U.fmt(mv.covered_s, 0) + ' s' : ''))
        : '等设备帧';      const cv = root.querySelector('#rc-maze');
      if (cv && cv.clientWidth) {
        C.draw.maze(cv, {
          N: view.N, goal: view.goal || [], v: view.v || [], h: view.h || [],
          visited: view.visited || {}, trail: view.trail || [],
          path: [],                      // 实时图没有"规划路径"，只有走过的 trail
          car: car,
        }, fitMaze(cv));
      }
    },

    onTick() {
      this._n = (this._n || 0) + 1;
      if (this._n % 2) return;
      this.paint();
    },
    onState() { this.paint(true); },
  };

  /* 地图边长由**可用高度**定（改版三十九）。
     地图是正方形 ⇒ 宽度再宽也用不上，唯一能变大的方向是"往上长"。
     原先 `.maze-box canvas{width:100%}` 让它按**列宽**定型：1400×760 下列宽 500
     ⇒ 地图 500×500 ⇒ 卡片正文高 618，把图例和建图诊断顶到视口外（诊断正是这一轮
     新加的六个数，看不见等于没加）。这里改成现算：
       视口底（减日志坞）− 正文顶 − 图例/诊断行高 ⇒ 剩下的才是画布
     再向下取整 1 px（算满会顶出一条滚动条，整页横向窄 15 px）。 */
  function fitMaze(cv) {
    const body = (cv && cv.closest) ? cv.closest('.card-body') : null;
    if (!body) return 0;
    const cs = getComputedStyle(body);
    const padV = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
    const foot = body.querySelector('.maze-foot');
    const st = body.querySelector('#rc-mstat');
    const wn = body.querySelector('#rc-mwarn');
    const others = padV
      + (foot ? foot.offsetHeight : 0)
      + (st ? st.offsetHeight + 6 : 0)
      + (wn ? wn.offsetHeight + 3 : 0);
    const dock = document.getElementById('dock');
    const dockH = dock ? dock.getBoundingClientRect().height : 0;
    const top = body.getBoundingClientRect().top + window.scrollY;
    const avail = window.scrollY + window.innerHeight - dockH - 8 - top - others - 1;
    /* 下限 240：再小就连 16×16 的格子线都糊成一片，不如让页面滚。 */
    return Math.max(240, Math.min(avail, body.clientWidth));
  }

  function kv(rows) {
    return rows.map(r =>
      '<div><dt>' + r[1] + ' <code>' + r[0] + '</code></dt><dd>' + U.esc(r[2]) + '</dd></div>').join('');
  }
})();
