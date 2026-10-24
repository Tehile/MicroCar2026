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

      const btn = root.querySelector('#rc-run');
      btn.disabled = S.running || S.estop || !S.health.mouse.connected;
      btn.classList.toggle('running', S.running);
      btn.querySelector('span').textContent = S.running ? '已发车' : '比赛发车';

      root.querySelector('#rc-hint').textContent = !S.health.mouse.connected ? '未连接电脑鼠'
        : S.estop ? '急停锁存 · 先解锁'
        : S.running ? '跑位中 · 收车 0.2~1 s 退出'
        : '预检通过';

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
          lbl.textContent = '已保存 · ' + shortName(R.last.name) + ' · ' + (R.last.frames || 0) + ' 帧';
          rb.disabled = true;
          rb.className = 'btn wide ok';
          rb.title = '已落盘（' + String(R.last.name || '') + '）· 路径 ' + String(R.last.path || '');
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
        : '等设备帧';
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
