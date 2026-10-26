/* ═══ 记录回放：时间轴拖动 + 迷宫位置 + 状态 + 参数快照（二级抽屉） ═════
   布局守则：**一屏放下位置图与时间轴**，不来回滚；且地图要尽量大。
   - 地图是正方形 ⇒ 大小只由**可用高度**决定（宽度再宽也没用）。
     于是"舞台"的总高度**现算**（`fitStage()`：布局顶边 → 视口底 − 日志坞），
     不写 `vh−常数` —— 常数一估错（窗口变矮）就是从地图身上割肉；
   - 一切挡在高度路上的东西都搬家：底部的「参数 · 记录」摘要条进页头动作区，
     时间轴的"标题/时间码"行并进按钮行；
   - 左列是"舞台"：地图卡 `flex:1` 吃剩余高度，时间轴 `flex:none` 钉在底部；
   - 「参数」「记录」是**静态的、和时间轴无关** ⇒ 不占右栏，走二级抽屉。 */
(function () {
  const S = C.S, U = C.util;
  const CH = C.channels;

  let curId = '';
  let i = 0;
  let playing = false;
  let speed = 1;
  /* 地图重绘的两个"上次值"：只在地图尺寸或帧号变了才重画。
     ResizeObserver 会因为 canvas 自身尺寸变化再回调一次 —— 不判就会自激。 */
  let lastSide = 0, lastI = -1;
  /* 舞台高度同理：设成同一个值不重复写 style（不然 resize 时来回抖）。 */
  let lastH = 0;
  let ro = null;

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

  /* 运行区间（改版四十四）：**全部取自后端 `phases`，前端一次都不自算**。
     `rec.phases == null` = 这条记录没有动作码通道 ⇒ 判不了；
     `rec.runFrom == null` = 有通道、但车一次没动。两者**不许混成一句**。 */
  function runInfo(rec) {
    if (rec.phases == null) {
      return '判不了（' + (rec.phaseNote || '这条记录没有 motionTask 通道') + '）';
    }
    if (rec.runFrom == null) return '全程未动（动作码一直是停）';
    const to = (rec.runTo == null ? rec.runFrom : rec.runTo);
    return '发车 ' + dur(rec.runFrom) + ' → 收车 ' + dur(to) +
      ' · 运行 ' + U.fmt(rec.runTotal == null ? 0 : rec.runTotal, 1) + ' s' +
      ' / 时长 ' + dur(rec.dur) +
      (rec.stopGaps ? ' · 中途停过 ' + rec.stopGaps + ' 次' : '');
  }

  /* 这条记录**有没有位置读数**。判据是样本里真有 `posX/posY`，
     **不是** `rec.launch` —— `launch` 只说"这条记录是怎么开的"（发车自动/手动），
     而手动开的记录照样有坐标（改版三十三起回放就是靠这个）。拿 `launch` 当门，
     手动记录会被写成"无位置"，而右栏同时又把坐标算出来了 —— 自相矛盾。 */
  function hasPos(rec) {
    const sm = (rec && rec.samples && rec.samples[0]) || null;
    return !!(sm && sm.posX != null && sm.posY != null);
  }

  /* 某一时刻属"在动"还是"停着" —— 查后端给的段，**不是**再判一次动作码。
     判据只留一份（后端 `motion_phases`），前端自己复算迟早两边分家。 */
  function phaseAt(rec, t) {
    if (!rec.phases || t == null) return null;
    for (let k = 0; k < rec.phases.length; k++) {
      const p = rec.phases[k];
      if (t >= p.from_s && t <= p.to_s) return p.kind;
    }
    return null;
  }

  C.VIEWS.replay = {
    eyebrow: '记录 · 回放',
    title: '回放',
    sub: '',

    actions() {
      return '<button class="btn btn-sm btn-ghost" data-rp="back">← 比赛记录</button>' +
        /* 「参数 · 记录」是静态信息、与时间轴无关 ⇒ 二级入口放页头动作区。
           原来它是页面底部一条 45 px 的摘要条 —— 而地图按可用高度定边长，
           那条栏等于直接从地图上割掉 45 px（+12 间距）。 */
        '<button class="btn btn-sm btn-ghost" data-rp="sheet">参数 · 记录</button>' +
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
      lastSide = 0; lastI = -1;

      root.innerHTML =
        '<div class="rp-layout">' +

          /* 位置图 + 时间轴**同一张卡**：两个独立卡各带一个卡头，白白吃掉
             26px 头 + 12px 间距 —— 而地图是"按可用高度定边长"的，那 38px
             直接换算成地图小一圈。地图 flex:1、时间轴 flex:none。 */
          '<section class="card rp-map-card">' +
            '<div class="card-head"><h2>位置</h2>' +
              '<div class="card-head-r">' +
                '<span class="tag blue" id="rp-id">' + rec.id + '</span>' +
                '<span class="hint mono" id="rp-pos">—</span>' +
                /* 图例并进卡头：单占一行图例要吃掉 26px，而地图是"按可用高度
                   定边长"的 —— 这 26px 直接等于地图小 26px。 */
                '<span class="legend"><i></i>已知墙</span>' +
                '<span class="legend"><i class="dash"></i>未探明</span>' +
                '<span class="legend"><i class="goal"></i>终点</span>' +
                (hasPos(rec) ? '' : '<span class="hint">这条记录没有位置读数</span>') +
              '</div></div>' +
            '<div class="rp-canvas-wrap" id="rp-mapwrap">' +
              '<canvas id="rp-maze"></canvas>' +
            '</div>' +
            '<div class="rp-tl">' +
              /* 一行搬运控件 + 一行刻度条。时间码与帧号搬进这一行 ——
                 单占一行要多吃 20 px，而地图是按可用高度定边长的。 */
              '<div class="rp-tl-bar">' +
                '<button class="btn btn-primary btn-sm" id="rp-play">' + C.icon('play') + '<span>播放</span></button>' +
                '<div class="seg" id="rp-speed">' +
                  '<button data-sp="1" class="on">1×</button>' +
                  '<button data-sp="2">2×</button>' +
                  '<button data-sp="4">4×</button>' +
                '</div>' +
                '<button class="btn btn-sm btn-ghost" data-step="-4">−1 s</button>' +
                '<button class="btn btn-sm btn-ghost" data-step="4">+1 s</button>' +
                '<span class="spacer"></span>' +
                '<span class="mono" id="rp-tc">00:00.0 / ' + dur(rec.dur) + '</span>' +
                '<span class="hint mono" id="rp-frame">帧 —</span>' +
              '</div>' +
              '<div class="scrub-wrap">' +
                '<input type="range" class="scrub" id="rp-range" min="0" max="' + (rec.samples.length - 1) + '" value="0">' +
                '<div class="scrub-marks" id="rp-marks"></div>' +
              '</div>' +
            '</div>' +
          '</section>' +

          '<div class="rp-side">' +
            '<section class="card">' +
              '<div class="card-head"><h2>状态</h2>' +
                '<div class="card-head-r"><span class="hint mono" id="rp-t">t 0.0 s</span></div></div>' +
              '<div class="card-body"><dl class="kv compact" id="rp-state"></dl></div>' +
            '</section>' +
          '</div>' +
        '</div>' +

        /* 二级抽屉：41 项发车前参数 + 记录元信息 */
        '<div class="rp-sheet" id="rp-sheet" aria-hidden="true">' +
          '<div class="rp-sheet-head">' +
            '<h2>参数 · 记录</h2>' +
            '<span class="hint mono">' + rec.id + '</span>' +
            '<span class="f-v mono" id="rp-foot-sum">—</span>' +
            '<span class="spacer"></span>' +
            '<button class="btn btn-sm" id="rp-sheet-close">关闭</button>' +
          '</div>' +
          '<div class="rp-sheet-body">' +
            '<div class="rp-sheet-col">' +
              '<div class="sec-title">记录</div>' +
              '<dl class="kv compact" id="rp-info"></dl>' +
            '</div>' +
            '<div class="rp-sheet-col grow">' +
              '<div class="sec-title">发车前参数快照</div>' +
              '<div class="rp-param-scroll"><table class="tbl rp-tbl"><thead><tr>' +
                '<th style="width:34px">ID</th><th style="width:130px">名称</th><th>值</th>' +
              '</tr></thead><tbody id="rp-rows"></tbody></table></div>' +
            '</div>' +
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

      /* 二级抽屉开关。不开动画（这套风格没有过渡），直接显隐。 */
      const sheet = root.querySelector('#rp-sheet');
      const setSheet = on => {
        sheet.classList.toggle('on', on);
        sheet.setAttribute('aria-hidden', on ? 'false' : 'true');
      };
      /* 「参数 · 记录」在**页头动作区**（不是 root 里），要走 actions 查 */
      const sheetBtn = actions && actions.querySelector('[data-rp="sheet"]');
      if (sheetBtn) sheetBtn.onclick = () => setSheet(true);
      root.querySelector('#rp-sheet-close').onclick = () => setSheet(false);
      /* Esc 关闭：视图没有 unmount 钩子，所以**先摘掉上一次的**再挂新的，
         否则每进一次页面就多一个 document 监听（切页几十次就堆几十个）。 */
      if (this._esc) document.removeEventListener('keydown', this._esc);
      this._esc = e => { if (e.key === 'Escape') setSheet(false); };
      document.addEventListener('keydown', this._esc);

      /* 发车/收车标记 + 采样缺口 ⇒ 单独一个方法，见下面的 `paintMarks()` */
      this.paintMarks();

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

      /* 抽屉头摘要：只写"有多少、有没有"，细节在下面的表里 */
      const nParam = rec.params
        ? rec.params.reduce((a, g) => a + g.items.length, 0) : null;
      root.querySelector('#rp-foot-sum').textContent =
        (nParam == null ? '参数快照 未记录（老记录）' : '参数快照 ' + nParam + ' 项') +
        ' · 采样缺口 ' + ((rec.gaps || []).length ? gapsInfo(rec) : '无') +
        ' · 视频 ' + (rec.video || '未关联');

      /* ── 舞台高度：**现算**，不写死常数 ─────────────────────────────
         布局顶边的文档坐标 → 视口底，减去日志坞高度和主区底内边距。
         以前 CSS 写 `vh−326`（按"页头 + 底条 + 内边距"估的一堆常数），
         窗口一矮就扣多：用户侧 1400×710 实测地图只剩 219 px。
         现算之后同一窗口是 414 px；窗口长高它跟着长，不用再改常数。
         下限 360 px：窗口矮到放不下时宁可让页面滚一点，也不把地图压成缩略图。 */
      const layout = root.querySelector('.rp-layout');
      const fitStage = () => {
        if (!layout) return;
        /* ≤1220 是堆叠布局（地图在上、状态在下），高度交给内容 ⇒ 摘掉内联高度 */
        if (window.innerWidth <= 1220) {
          if (layout.style.height) layout.style.height = '';
          lastH = 0;
          return;
        }
        const dock = document.getElementById('dock');
        const dockH = dock ? Math.round(dock.getBoundingClientRect().height) : 0;
        const top = layout.getBoundingClientRect().top + window.scrollY;
        /* 14 = 主区底内边距(12) + 1 px 取整余量：算满会出现 1 px 溢出 ⇒
           整页冒出纵向滚动条 ⇒ 顶栏与卡片整体窄 15 px（看着像布局抖了一下）。 */
        const h = Math.max(360, Math.floor(window.innerHeight - top - dockH - 14));
        if (h !== lastH) { lastH = h; layout.style.height = h + 'px'; }
      };
      lastH = 0;
      fitStage();
      if (this._rs) window.removeEventListener('resize', this._rs);
      this._rs = () => { fitStage(); this.paint(); };
      window.addEventListener('resize', this._rs);

      /* 地图尺寸跟着容器走：窗口一变化就重排（否则切回窗口还按旧尺寸画） */
      if (ro) { ro.disconnect(); ro = null; }
      const wrap = root.querySelector('#rp-mapwrap');
      if (window.ResizeObserver && wrap) {
        ro = new ResizeObserver(() => {
          if (this._root && this._root.querySelector('#rp-range')) this.paint();
        });
        ro.observe(wrap);
      }

      this.paint(true);
    },

    /* 发车 / 收车标记 + **采样缺口**（改版三十八 / 四十四）。
       ⚠️ 发车收车的判据是**动作码**（后端 `phases`，`motionTask ∉ {0,5}`），
       不是 `rec.launch` —— 后者只说明"这条记录是发车自动开的还是人手动开的"，
       而 `launchAt` 对发车记录**恒为 0.0** ⇒ 原来那个标记永远贴在进度条最左端。
       收车原来更是硬编码 `p:1`（永远在末尾）：实测一条车 4.2 s 就停住、
       记录却录到 173.1 s，收车刻度错了 169 s。
       缺口画成**带子**（有宽度），不是 1px 刻度：它是一段"没有数据"的区间，
       画成一条线会被读成"某个时刻发生了什么"。位置用 `t_ms` 同一口径。

       **为什么单独成方法**（不是写在 `mount()` 里）：判据要能直接驱动它验
       "判不了时一根都不画"。藏在 mount 里的活，验的人只能重挂页面 ——
       而重挂会顺带重新拉记录，刚摘掉的 `phases` 又被后端数据盖回来
       （v44 的 V22 自己踩过一次：改成 `C.go` 之后反而恒红）。 */
    paintMarks() {
      const root = this._root;
      if (!root || !root.querySelector('#rp-marks')) return 0;
      const rec = C.record(S.recSel);
      if (!rec) return 0;
      const marks = [];
      const durMs = Math.max(1, (rec.dur || 0) * 1000);
      const pct = v => U.clamp(v, 0, 1) * 100;
      /* 缺口照画（那是"没采样"，与动作码无关），三个分支共用一份 */
      const pushGaps = () => {
        (rec.gaps || []).forEach(g => {
          const a = U.clamp((g.t_ms_from || 0) / durMs, 0, 1);
          const b = U.clamp((g.t_ms_to || 0) / durMs, 0, 1);
          marks.push({ a: a, b: b, t: gapText(g), gap: true });
        });
      };
      /* ⚠️ **判不了排第一**：`phases` 是这件事的唯一权威。
         只看 `runFrom` 的话，一旦出现"`phases` 没了而 `runFrom` 还留着"的状态，
         就会照样画两根假的。三个分支的先后本身就是一条判据。 */
      if (rec.phases == null) {
        /* 这条记录没有动作码通道 ⇒ 一根发车/收车都不许画（画在哪都是编的） */
        pushGaps();
        marks.push({ a: 0, b: 1, t: '无动作码 · 判不了发车/收车', unk: true });
      } else if (rec.runFrom != null && rec.dur) {
        const pA = rec.runFrom / rec.dur;
        const pB = (rec.runTo == null ? pA : rec.runTo / rec.dur);
        /* 两线挨得近时标签会叠在一起 ⇒ 把「发车」标签翻到线的**左侧**，
           两个标签朝外分开。判据用 18% 宽：再窄就是这盘车几乎没跑。 */
        const tight = (pB - pA) < 0.18;
        /* 窄到放不下两个标签时：左侧还有空间就把「发车」翻到线的左边（`roll`），
           没有（发车点本身就在最左 15%）就把标签缩成两个字 —— 时刻丢不了，
           它在「参数 · 记录」那行「运行」里写着。**宁可短，不许叠字**。 */
        const canRoll = pA > 0.15;
        /* 两根线之间铺一条**淡色带**：一眼看出"从哪开始到哪结束是发车"。
           它不是另一条时间轴（还是这条 16 px 的条），只是把区间填出来 ——
           两根孤线在窄窗口里很容易被当成普通刻度。 */
        marks.push({ span: true, p: pA, w: Math.max(0, pB - pA) });
        marks.push({ p: pA,
          t: (tight && !canRoll) ? '发车' : ('发车 ' + dur(rec.runFrom)),
          cls: 'mk-start' + ((tight && canRoll) ? ' roll' : '') });
        pushGaps();
        marks.push({ p: pB, t: '收车 ' + dur(rec.runTo == null ? rec.runFrom : rec.runTo), cls: 'mk-end' });
      } else {
        /* 有通道、但**全程没动过**（`phases` 一整段 stop）。照样不画两根假刻度，
           只写一行"全程未动" —— 这也是个结论，不是"判不了"。 */
        pushGaps();
        marks.push({ a: 0, b: 1, t: '全程未动（动作码一直是停）', unk: true });
      }
      root.querySelector('#rp-marks').innerHTML = marks.map(m => {
        if (m.unk) return '<i class="unk"></i><span class="mk-unk">' + U.esc(m.t) + '</span>';
        if (m.span) {
          return '<i class="mk-span" style="left:' + pct(m.p).toFixed(2) + '%;width:' +
            (m.w * 100).toFixed(2) + '%"></i>';
        }
        if (m.gap) {
          /* 带子只画区间、**不带字**：16 px 的条上放两行字会互相压住，
             完整文案交给 `title`（悬停可看）与「参数 · 记录」里的缺口明细。 */
          return '<i class="gap" title="' + U.esc(m.t) + '" style="left:' +
            pct(m.a).toFixed(2) + '%;width:' +
            Math.max(0.5, (m.b - m.a) * 100).toFixed(2) + '%"></i>';
        }
        return '<i class="' + (m.cls || '') + '" style="left:' + pct(m.p).toFixed(2) +
          '%"><em>' + U.esc(m.t) + '</em></i>';
      }).join('');
      return marks.length;
    },

    paint(first) {
      const root = this._root;
      if (!root || !root.querySelector('#rp-range')) return;
      const rec = C.record(S.recSel);
      if (!rec || !rec.samples || !rec.samples.length) return;

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
      posEl.textContent = (sm.posX == null || sm.posY == null)
        ? '无位置读数'
        : 'x ' + U.fmt(sm.posX, 2) + ' · y ' + U.fmt(sm.posY, 2) + ' · ' + (CH.dirs[sm.dir] || '?');

      root.querySelector('#rp-state').innerHTML = [
        ['t', '时刻', U.fmt(sm.t, 2) + ' s'],
        ['frame', '帧号', String(sm.frame)],
        ['posX/posY', '位置', U.fmt(sm.posX, 2) + ', ' + U.fmt(sm.posY, 2)],
        ['dir', '朝向', (CH.dirs[sm.dir] || '?') + ' (' + sm.dir + ')'],
        ['mazeTask', '迷宫任务', (CH.tasks[sm.mazeTask] || sm.mazeTask) + ' (' + sm.mazeTask + ')'],
        ['motionTask', '动作', (CH.motionText ? CH.motionText(sm.motionTask)
          : (CH.motions[sm.motionTask] || sm.motionTask)) + ' (' + sm.motionTask + ')'],
        ['run', '运行', (function () {
          const k = phaseAt(rec, sm.t);
          return k === 'run' ? '在动' : k === 'stop' ? '停着' : '判不了';
        })()],
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
        ['launch', '发车记录', rec.launch ? '自动开录（发车触发）' : '手动开录'],
        // 运行区间（改版四十四）：与上面那行**分开**——那行说"记录是怎么开始录的"，
        // 这行说"车什么时候真的在动"。混成一句就会把"发车自动开录"读成"车一直在跑"。
        ['run', '运行', runInfo(rec)],
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

      /* 地图：**边长取容器可用高宽的最小值**。按宽度走正方形是原来页面被撑出
         一屏的原因（左栏 1000 px 宽 ⇒ 1000 px 高的图）。 */
      const cv = root.querySelector('#rp-maze');
      const wrap = root.querySelector('#rp-mapwrap');
      if (cv && wrap) {
        const aw = Math.max(80, wrap.clientWidth - 16);
        const ah = Math.max(80, (wrap.clientHeight || aw) - 16);
        const side = Math.round(Math.min(aw, ah));
        if (side !== lastSide || i !== lastI) {
          C.draw.maze(cv, C.recordView(rec, i), side);
          lastSide = side; lastI = i;
        }
      }
    },

    onTick() {
      const root = this._root;
      if (!root || !root.querySelector('#rp-range')) return;
      const rec = C.record(S.recSel);
      if (!rec || !rec.samples || !rec.samples.length) return;
      if (!playing) return;
      i += speed;
      if (i >= rec.samples.length - 1) { i = rec.samples.length - 1; playing = false; }
      this.paint();
    },
    onState() { this.paint(); },
  };
})();
