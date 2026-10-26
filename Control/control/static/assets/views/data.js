/* ═══ 记录 · 比赛记录：查（搜索 / 筛选）+ 增（导入）+ 改（改名 / 关联）
       + 删（单条 / 多选批量）+ 回放 ═══════════════════════════════════════
   改版四十大修，四件事：
     1. **查**：工具行加搜索框 + 三个筛选 chip（发车 / 视频 / 账本异常）。
        纯前端过滤 —— 记录已经全在 `S.records` 里（默认取 50 条），零新端点。
     2. **多选**：每行一个 checkbox + 表头全选（只勾**当前筛出来的**那批）。
        勾选集合（`checked`）与"控制面板在看哪一条"（`selId`）**是两件事**：
        前者是"要对谁动手"，后者是"在看谁的详情"，混在一起会误删。
     3. **改**：改名（`POST /records/{name}/rename`，同时搬 `.link.json`）+ 关联视频。
     4. **删**：单条两步确认（原样保留）+ **批量**（一条端点，逐条结果）。
     5. **底部空白**：列表改成 flex 吃满剩余高度、内部滚（原来 `max-height:
        calc(100vh - 460px)` 在 1400×710 下只给 250 px，剩下的高度全空着）。
   ⚠️ 命名注意：`checked` 是"勾选"，`selId` 是"展开详情"，别互相顶替。

   ── 改版四十五：**列表不再一次要 50 条就到底** ────────────────────────────
   三个口径必须分开说，混成一个就会撒谎：
     · `S.records.length` = **已载**条数（随滚动长）；
     · `S.recTotal`       = **全库**条数（后端 `total`，只 glob 不解析）；
     · `visible().length` = **当前筛出来**的条数（只在已载的那批里筛）。
   于是：计数文案写「已载 50 / 共 323」、发车/视频两个数在未载全时加 `≥`、
   全选写成「全选已载的 N 行」。**不许**在未载全时说"共 3 条发车" —— 那只数了 50 行。
   两条加载路径：
     · **滚动续载**：距底 <120 px 且还有没载的 ⇒ 取下一页（`REC_PAGE` 条）；
       续页**不 `emit('state')`**（整页重挂 ⇒ 滚动归零、搜索框被清，变成"滚一下跳回顶部"）。
     · **搜索/筛选自动载全**：`/api/v1/records?n=0` 一次全取。
       为什么要这么做：只在已载的 50 条里搜，"搜不到"会被读成"没有这条记录"，
       而它可能在第 200 条 —— **假否定**比慢一点糟得多。
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  const S = C.S, U = C.util;
  let selId = '';
  let assoc = false;
  let checked = {};       // id → 1（勾选集合）
  let chip = 'all';       // all | launch | video | bad
  let q = '';             // 搜索词
  let delArm = 0;         // 单条删除的两步确认：第一次点击的时刻（0 = 没在等确认）
  let delArmId = '';      // 等的是哪一条（不然点 A 再点 B 会被当成确认 B）
  let bulkArm = 0;        // 批量删除的两步确认（按"这一批"计时，不按条）
  let allP = null;        // 「载全」在飞的那一次（去重：连打三个字不该发三个全量请求）

  /* 这一条是否正"等第二次点击"。**必须按 id 判**：只按时间判的话，
     用户点 A 的删除 → 又点 B 的删除，B 会被立刻删掉（一次都没确认过）。
     而 paint 会重画按钮（看门狗 2 s 一次 `emit('health')`），所以文本也要靠它恢复，
     否则第一次点击后那 0.6 s 里文本会被重绘回"删除记录" ⇒ 用户看不出要再点一次。 */
  function delArmed(id) {
    return !!delArm && delArmId === id && (Date.now() - delArm) <= 5000;
  }
  function bulkArmed() {
    return !!bulkArm && (Date.now() - bulkArm) <= 5000;
  }

  function dur(s) {
    if (s == null) return '—';
    const m = Math.floor(s / 60), r = Math.round(s % 60);
    return m + ':' + String(r).padStart(2, '0');
  }
  function session(id) {
    return S.videoSessions.find(v => v.id === id) || null;
  }

  /* 全库条数（后端 `total`）。**没问过就退回已载条数** —— 那样 `partial()` 判 false，
     界面按"载全了"说话。宁可少喊一句"还有更多"，也不能凭"我只知道 50 条"就
     断言"全库只有 50 条"。真值到了（`applyRecords` 写入 `S.recTotal`）就以它为准。 */
  function total() {
    const n = S.records.length;
    return (S.recTotal == null ? n : S.recTotal);
  }
  function partial() {
    return S.records.length < total();
  }

  /* 距底多少像素算"该续了"。120 px ≈ 两三行，滚轮一档之内。 */
  const MORE_PX = 120;

  /* 当前**筛出来**的那批（表格里看得到的）。搜索与 chip 都在这里收口，
     别的地方（全选 / 计数 / 批量删）一律走它 —— 否则"全选"会勾到看不见的行。 */
  function visible() {
    const kw = q.trim().toLowerCase();
    return S.records.filter(r => {
      if (chip === 'launch' && !r.launch) return false;
      if (chip === 'video' && !r.video) return false;
      if (chip === 'bad' && r.ledger !== false) return false;
      if (!kw) return true;
      const hay = [r.id, r.start, r.video || '', r.endWhy || '', String(r.frames)]
        .join(' ').toLowerCase();
      return hay.indexOf(kw) >= 0;
    });
  }
  function checkedIds() {
    const out = [];
    visible().forEach(r => { if (checked[r.id]) out.push(r.id); });
    return out;
  }
  /* 记录被删 / 改名之后，勾选集合里会留下已不存在的 id。
     不清的话"已选 3"会永远显示，而列表里一条都没打勾。 */
  function pruneChecked() {
    const live = {};
    S.records.forEach(r => { live[r.id] = 1; });
    Object.keys(checked).forEach(id => { if (!live[id]) delete checked[id]; });
  }

  C.VIEWS.data = {
    eyebrow: '记录 · 比赛记录',
    title: '比赛记录',
    sub: '',

    actions() {
      const n = S.records.length;
      const tot = total();
      const full = !partial();
      const nl = S.records.filter(r => r.launch).length;
      const nv = S.records.filter(r => r.video).length;
      /* 未载全时发车/视频两个数只数了"已载那批"，所以加 `≥` 并在 title 里说明。
         原样写"发车 3"会让 323 条里明明有 20 条发车也读成 3 条 —— 静默的假数。 */
      const ge = full ? '' : '≥';
      const tip = full ? '全部记录' : ('只统计已载的 ' + n + ' 条（全库 ' + tot + ' 条）');
      return '<span class="pill idle" title="' + tip + '"><i></i>' +
          (full ? n + ' 条' : '已载 ' + n + ' / ' + tot) + '</span>' +
        '<span class="pill ' + (full && nl === n ? 'ok' : 'idle') + '" title="' + tip + '"><i></i>发车 ' + ge + nl + '</span>' +
        '<span class="pill ' + (full && nv === n ? 'ok' : 'idle') + '" title="' + tip + '"><i></i>视频 ' + ge + nv + '</span>' +
        '<button class="btn btn-sm btn-ghost">' + C.icon('download') + '导出</button>';
    },

    mount(root) {
      this._root = root;
      if (!selId) selId = S.recSel || (S.records[0] && S.records[0].id) || '';
      /* **控制面板在上、列表在下**：选中条目后要能立刻操作，而列表会长
         （真机几十条），面板放底部就会被列表推到视野外 ⇒ 面板置顶，
         列表自己滚，两者互不干扰。 */
      root.innerHTML =
        '<section class="card dt-ctl" id="dt-ctl"></section>' +
        '<section class="card dt-list">' +
          '<div class="card-head"><h2>记录列表</h2>' +
            '<div class="card-head-r"><span class="hint mono" id="dt-count"></span></div></div>' +
          /* 工具行（改版四十）：查 + 批量动作 + 导入。 */
          '<div class="dt-tools">' +
            '<label class="search">' +
              '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
                '<circle cx="11" cy="11" r="6"/><path d="M16 16l4 4"/></svg>' +
              '<input id="dt-q" placeholder="搜索 名字 / 开始时间 / 视频">' +
            '</label>' +
            '<div class="seg" id="dt-chip">' +
              '<button data-chip="all">全部</button>' +
              '<button data-chip="launch">发车</button>' +
              '<button data-chip="video">视频</button>' +
              '<button data-chip="bad">账本异常</button>' +
            '</div>' +
            '<span class="hint mono dt-sel" id="dt-sel">未选</span>' +
            '<button class="btn btn-sm btn-ghost" id="dt-del">' + C.icon('trash') + '删除选中</button>' +
            '<button class="btn btn-sm" id="dt-imp">' + C.icon('download') + '导入…</button>' +
            '<input type="file" id="dt-file" accept=".json,application/json" hidden>' +
          '</div>' +
          '<div class="card-body flush dt-list-scroll">' +
            '<table class="tbl"><thead><tr>' +
              '<th class="ck-col"><input type="checkbox" id="dt-ckall" title="全选当前筛选结果"></th>' +
              '<th>记录</th><th style="width:92px">开始</th><th style="width:74px">时长</th>' +
              '<th style="width:74px">帧</th><th style="width:150px">发车记录</th>' +
              '<th style="width:200px">视频记录</th><th style="width:80px">账本</th>' +
            '</tr></thead><tbody id="dt-rows"></tbody></table>' +
            /* 续载提示放在**表格外面但同一个滚动容器里**（改版四十五）：
               放 tbody 里会多出一行，凡按行数断言的判据都会跟着变。
               空文本时由 CSS `:empty` 收掉，不占高度。 */
            '<div class="dt-more" id="dt-more"></div>' +
          '</div>' +
        '</section>';

      /* ── 行交互：勾选框只改"勾选集合"，行其余部分改"正在看哪一条" ── */
      root.querySelector('#dt-rows').onclick = e => {
        if (e.target.matches('input[data-ck]')) return;   // 勾选由 change 事件处理
        const as = e.target.closest('[data-assoc]');
        if (as) {
          selId = as.dataset.assoc;
          assoc = true;
          this.paint();
          return;
        }
        const tr = e.target.closest('[data-id]');
        if (!tr) return;
        selId = tr.dataset.id;
        assoc = false;
        this.paint();
      };
      root.querySelector('#dt-rows').onchange = e => {
        const box = e.target.closest('input[data-ck]');
        if (!box) return;
        if (box.checked) checked[box.dataset.ck] = 1; else delete checked[box.dataset.ck];
        this.syncTools();
      };

      const ckAll = root.querySelector('#dt-ckall');
      ckAll.onchange = () => {
        const on = ckAll.checked;
        visible().forEach(r => { if (on) checked[r.id] = 1; else delete checked[r.id]; });
        this.paint();
      };

      const qEl = root.querySelector('#dt-q');
      /* 整页重挂（`emit('state')`）会把 input 一起重建，而 `q` 是模块级变量 ⇒
         不写回 value 就会出现"框里空的、列表却已经按旧词筛着"这种自相矛盾的界面。 */
      qEl.value = q;
      qEl.oninput = () => { q = qEl.value; this.paint(); this.ensureAll(); };
      /* 搜索不该被整页重挂清掉：`paint()` 只重画 tbody 与工具行状态，不动 input 本体。
         （`renderPage` 只在路由切换 / `emit('state')` 时发生，而这里不触发 state。） */

      root.querySelectorAll('#dt-chip [data-chip]').forEach(b => {
        b.onclick = () => { chip = b.dataset.chip; this.paint(); this.ensureAll(); };
      });

      /* 滚到底自动续载（改版四十五）。挂 onscroll 而不是定时轮询：
         只在真的滚的时候问一句"到没到边"，空闲时零开销。 */
      const sc = root.querySelector('.dt-list-scroll');
      if (sc) sc.onscroll = () => this.onScroll();

      const delBtn = root.querySelector('#dt-del');
      delBtn.onclick = () => {
        const ids = checkedIds();
        if (!ids.length) return;
        /* 批量删：**按"这一批"两步确认**。不可逆，所以不做"点了就删"。
           计时起点是第一次点击；5 s 内再点同一批才真删。 */
        if (!bulkArmed()) {
          bulkArm = Date.now();
          this.syncTools();
          return;
        }
        bulkArm = 0;
        delBtn.disabled = true;
        C.bulkDeleteRecords(ids).then(res => {
          // 删成功的从勾选集合里去掉（失败的要继续留着，让人看到是哪几条没删掉）
          (res.results || []).forEach(x => { if (x.ok) delete checked[x.name.replace(/\.maze\.json$/, '')]; });
          Object.keys(checked).forEach(id => {
            if (ids.indexOf(id) >= 0 && !S.records.some(r => r.id === id)) delete checked[id];
          });
          if (res.failed) {
            C.toast('warn', '部分没删掉', '成功 ' + res.deleted + ' 条 · 失败 ' + res.failed + ' 条');
          } else {
            C.toast('ok', '已删除 ' + res.deleted + ' 条', '记录与关联文件已移除（视频产物不动）');
          }
          this.paint();
        });
      };

      /* 导入（"增"）：文件内容原样交给后端校验并落盘。
         ⚠️ 不让后端凭文件名猜格式 —— 它校验 `format` / `channels` / `frames` 三者一致，
            对不上就明说，不写半个文件。 */
      const file = root.querySelector('#dt-file');
      root.querySelector('#dt-imp').onclick = () => file.click();
      file.onchange = () => {
        const f = file.files && file.files[0];
        if (!f) return;
        const rd = new FileReader();
        rd.onload = () => {
          let doc = null;
          try { doc = JSON.parse(String(rd.result || '')); }
          catch (e) {
            C.toast('bad', '不是合法的 JSON', String(e.message || e).slice(0, 120));
            file.value = '';
            return;
          }
          C.importRecord(doc, f.name).then(ok => {
            file.value = '';
            if (ok) C.toast('ok', '已导入', f.name);
            this.paint();
          });
        };
        rd.onerror = () => { C.toast('bad', '读不了这个文件', f.name); file.value = ''; };
        rd.readAsText(f);
      };

      this.paint();
    },

    /* ── 续载 / 载全（改版四十五）───────────────────────────────────────────── */

    /* 滚到底：还有没载的就去取下一页。
       **只在"没有搜索/筛选"时走这条路** —— 带筛选时续页拿回来的行很可能一条都进不了
       `visible()`，滚十次也看不到新东西；那种情况交给 `ensureAll()` 一次取全。 */
    onScroll() {
      const root = this._root;
      if (!root || this._more) return;
      const sc = root.querySelector('.dt-list-scroll');
      if (!sc) return;
      if (!C.LIVE.on || !partial()) return;
      if (q.trim() || chip !== 'all') return;
      if (sc.scrollTop + sc.clientHeight < sc.scrollHeight - MORE_PX) return;
      this._more = true;
      this.paintMore();
      C.loadRecords(true).then(n => {
        this._more = false;
        this.paint();
        /* 一页（50 条）铺不满一屏时（窗口很高），滚到底之后就没得再滚了 ⇒ 主动再补一页。
           不补的话"到底了但还有 300 条没载"会停在那儿，看着像已经载全。 */
        if (n) this.onScroll();
      });
    },

    /* 载全：搜索或切筛选时用一次全量请求把 `S.records` 填满。
       ⚠️ 为什么必须载全再筛：只在已载的 50 条里搜，"搜不到"会被读成"没有这条记录"，
          而它可能排在第 200 条 —— **假否定比慢一点糟得多**。
       在飞的那一次不重复发（连打三个字不该发三个 `n=0`）。 */
    ensureAll() {
      if (!C.LIVE.on || !partial()) return;
      if (allP) return;
      allP = C.loadRecords('all').then(add => {
        allP = null;
        if (add) this.paint();
      }).catch(() => { allP = null; });
    },

    /* 表格下面那一行提示。只说三件事：已载多少、共多少、在不在加载。
       载全且没在加载 ⇒ 清空（CSS `:empty` 收掉，不占高度）。 */
    paintMore() {
      const root = this._root;
      const el = root && root.querySelector('#dt-more');
      if (!el) return;
      if (!C.LIVE.on || !partial()) { el.textContent = ''; return; }
      if (this._more) { el.textContent = '正在加载下一批…'; return; }
      el.textContent = (q.trim() || chip !== 'all')
        ? ('已载 ' + S.records.length + ' / 共 ' + total() + ' 条 · 搜索/筛选会自动载入全部')
        : ('已载 ' + S.records.length + ' / 共 ' + total() + ' 条 · 继续往下滚自动加载');
    },

    /* 工具行状态（勾选数 / 按钮门控 / chip 高亮）。**与列表分开**：
       勾一下不该重画整张表（会把滚动位置和正在输入的光标弄丢）。 */
    syncTools() {
      const root = this._root;
      if (!root || !root.querySelector('#dt-sel')) return;
      const vis = visible();
      const ids = checkedIds();
      const par = partial();
      const sel = root.querySelector('#dt-sel');
      /* 未载全时把范围说进文案里：`未选 · 50 条` 会被读成"全库只有 50 条"。 */
      sel.textContent = ids.length
        ? ('已选 ' + ids.length + ' / ' + vis.length + (par ? '（已载）' : ''))
        : ('未选 · ' + (par ? '已载 ' : '') + vis.length + ' 条');
      const del = root.querySelector('#dt-del');
      del.disabled = !ids.length;
      const armed = bulkArmed() && ids.length > 0;
      del.className = 'btn btn-sm ' + (armed ? 'btn-danger' : 'btn-ghost');
      del.innerHTML = C.icon('trash') + (armed ? ('确认删除 ' + ids.length + ' 条？（5s）') : '删除选中');
      const all = root.querySelector('#dt-ckall');
      all.checked = vis.length > 0 && vis.every(r => checked[r.id]);
      all.indeterminate = !all.checked && ids.length > 0;
      /* 全选只能勾到"已经载进内存"的行 —— 未载全时如实写出来，
         否则"全选之后批量删除"会被理解成"删掉全部 323 条"。 */
      all.title = par
        ? ('全选已载的 ' + vis.length + ' 行（另有 ' + (total() - S.records.length) + ' 条还没载入）')
        : '全选当前筛选结果';
      root.querySelectorAll('#dt-chip [data-chip]').forEach(b => {
        b.classList.toggle('on', b.dataset.chip === chip);
      });
    },

    paint() {
      const root = this._root;
      if (!root || !root.querySelector('#dt-rows')) return;
      pruneChecked();

      const vis = visible();
      const ck = id => (checked[id] ? ' checked' : '');
      root.querySelector('#dt-rows').innerHTML = vis.length
        ? vis.map(r => {
            const ses = session(r.video);
            return '<tr class="sess-row' + (r.id === selId ? ' sel' : '') + '" data-id="' + r.id + '">' +
              '<td class="ck-col"><input type="checkbox" data-ck="' + r.id + '"' + ck(r.id) + '></td>' +
              '<td class="num" style="color:var(--ink)">' + U.esc(r.id) + '</td>' +
              '<td class="num dim">' + U.esc(r.start) + '</td>' +
              '<td class="num dim">' + dur(r.dur) + '</td>' +
              '<td class="num">' + U.fmt(r.frames, 0) + '</td>' +
              '<td>' + (r.launch
                /* ⚠️ 不写 `@0.0 s`：`launch_at_s` 对**所有**发车自动开录的记录恒为 0.0
                   （那一位只表示"记录起点就是发车点"），标出来就是个恒定假数。
                   真正的发车/收车时刻在回放页（按动作码算的运行区间）。 */
                ? '<span class="tag ok">自动开录</span>'
                : '<span class="tag">手动</span>') + '</td>' +
              '<td>' + (r.video
                ? '<span class="tag blue">' + U.esc(r.video) + '</span>'
                : '<button class="btn btn-sm btn-ghost" data-assoc="' + r.id + '">' + C.icon('link') + '关联</button>') + '</td>' +
              // 三态：true=通过 / false=丢帧 / null=**判不了**（老记录没有丢帧计数）
              // 判不了不许画成"差 X 帧"—— 那是在替设备作证
              '<td>' + (r.ledger === true
                ? '<span class="tag ok">通过</span>'
                : r.ledger === false
                  ? '<span class="tag bad">差 ' + (r.gap == null ? '?' : r.gap) + ' 帧</span>'
                  : '<span class="tag">判不了</span>') + '</td>' +
              '</tr>';
          }).join('')
        /* 空态（改版四十）：原来筛不到东西时表体是**一片空白**，
           与"加载失败""还没取到"完全分不出来。两种空要说清楚是哪一种。
           改版四十五再补一种：**还没载全**时不许说"没有匹配" —— 那句话是结论，
           而在只载了 50 / 323 条的时候根本没有资格下这个结论（假否定）。 */
        : '<tr><td colspan="8" class="dt-empty">' +
          (!S.records.length
            ? (C.LIVE.on ? '还没有记录 · 发车一次会自动落一份，也可以用【导入…】' : '未连后端 · 判不了有没有记录')
            : (partial()
              ? ('正在载入全部记录（已载 ' + S.records.length + ' / 共 ' + total()
                 + ' 条）… 载全之后才能判断有没有匹配')
              : '没有匹配的记录（清掉搜索词或换一个筛选）'))
          + '</td></tr>';

      const cnt = root.querySelector('#dt-count');
      if (cnt) {
        const nl = S.records.filter(r => r.launch).length;
        const nv = S.records.filter(r => r.video).length;
        const par = partial();
        /* 未载全时**只报"已载 / 共"与筛出数**，不报发车/视频 —— 那两个数此刻只覆盖
           已载那批，写成"发车 3"是替全库作证。载全之后才三个数一起报。 */
        cnt.textContent = par
          ? ('已载 ' + S.records.length + ' / 共 ' + total() + ' 条'
             + (vis.length === S.records.length ? '' : ' · 筛出 ' + vis.length))
          : (S.records.length + ' 条 · 发车 ' + nl + ' · 视频 ' + nv
             + (vis.length === S.records.length ? '' : ' · 筛出 ' + vis.length));
      }
      /* 页头右上角那三个胶囊也是**由 `actions()` 现算**的（已载体量 / 发车 / 视频），
         而 `paint()` 原来只管卡里的表 —— 续载完 344 条之后，那三个胶囊还挂着
         "已载 50 / 344"，要等下一次 watchdog 的 `emit('state')` 才刷。
         截图里就这么对上了：卡头写「344 条」、右上角写「已载 50 / 344」。
         既然两个数都是 `actions()` 算的，就让 `paint()` 一起刷它（内容没变不碰 DOM）。 */
      const pa = document.getElementById('page-actions');
      if (pa) {
        const html = this.actions();
        if (pa._html !== html) { pa.innerHTML = html; pa._html = html; }
      }
      this.paintMore();
      this.syncTools();

      /* ── 控制面板：**看**的那一条（与勾选无关） ────────────────────── */
      const box = root.querySelector('#dt-ctl');
      const r = C.record(selId) || S.records[0] || null;
      if (!r) {
        box.innerHTML = '<div class="card-body"><div class="empty"><b>没有可显示的记录</b>' +
          '列表为空时这里不编一条出来。发车一次、或用【导入…】放一份进来。</div></div>';
        return;
      }
      selId = r.id;
      const ses = session(r.video);

      const info =
        '<div class="card-head dt-head">' +
          '<h2><span class="n">记录</span>' + U.esc(r.id) + '</h2>' +
          '<div class="dt-head-r">' +
            '<span class="tag ' + (r.launch ? 'ok' : '') + '">' +
              (r.launch ? '发车自动开录' : '手动开录') + '</span>' +
            '<span class="tag ' + (r.video ? 'blue' : '') + '">' +
              (r.video ? U.esc(r.video) : '无视频记录') + '</span>' +
            '<div class="dt-acts">' +
              '<button class="btn btn-sm btn-primary" data-rp="go"' + (r.frames > 0 ? '' : ' disabled') + '>' +
                C.icon('play') + '回放</button>' +
              '<button class="btn btn-sm" data-rp="csv">' + C.icon('download') + 'CSV</button>' +
              '<button class="btn btn-sm" data-rp="dir">目录</button>' +
              '<button class="btn btn-sm" data-rp="rename">' + C.icon('save') + '改名</button>' +
              '<button class="btn btn-sm btn-ghost" data-rp="assoc">' + C.icon('link') +
                (r.video ? '改关联' : '关联视频') + '</button>' +
              '<button class="btn btn-sm btn-ghost' + (delArmed(r.id) ? ' btn-danger' : '') +
              '" data-rp="del">' + (delArmed(r.id) ? '确认删除？（5s）' : '删除') +
              '</button>' +
            '</div>' +
          '</div>' +
        '</div>' +
        '<div class="card-body">' +
          '<dl class="kv compact cols4">' +
            '<div><dt>开始</dt><dd>' + U.esc(r.start) + '</dd></div>' +
            '<div><dt>时长</dt><dd>' + dur(r.dur) + '</dd></div>' +
            '<div><dt>遥测帧</dt><dd>' + U.fmt(r.frames, 0) + '</dd></div>' +
            /* 原来是「发车时刻 → 0.0 s」：而 `launch_at_s` 对**所有**发车自动开录的
               记录恒为 0.0（它只表示"记录起点就是发车点"）⇒ 那一格永远是同一个假数。
               真正的发车/收车时刻要按动作码算，在回放页（§11.44）。 */
            '<div><dt>开录方式</dt><dd>' + (r.launch ? '自动（发车触发）' : '手动') + '</dd></div>' +
            '<div><dt>视频会话</dt><dd>' + (r.video || '未关联') + '</dd></div>' +
            '<div><dt>轨迹点</dt><dd>' + (ses ? ses.n_valid : '—') + '</dd></div>' +
            '<div><dt>帧缺口</dt><dd class="' + (r.gap ? 'bad' : '') + '">' + (r.gap == null ? '—' : r.gap) + '</dd></div>' +
            '<div><dt>目录</dt><dd class="ell" title="' + U.esc(r.dir) + '">' + U.esc(r.dir) + '</dd></div>' +
          '</dl>' +
          (r.frames > 0 ? '' : '<div class="banner warn" style="margin-top:10px">' + C.icon('alert') +
            '<div class="b-body"><b>这条记录没有帧</b>回放没有位置可画。</div></div>') +
          (assoc ? this.assocHtml(r) : '') +
        '</div>';

      box.innerHTML = info;

      box.querySelectorAll('[data-rp]').forEach(b => {
        b.onclick = () => {
          const k = b.dataset.rp;
          if (k === 'go') { S.recSel = r.id; C.go('replay'); }
          else if (k === 'csv') {
            C.log('control', 'info', '导出 ' + r.id + '.csv · ' + r.frames + ' 帧');
            C.toast('ok', '已导出', r.id + '.csv');
          } else if (k === 'dir') C.toast('', r.dir, '已在文件管理器打开');
          else if (k === 'assoc') { assoc = !assoc; this.paint(); }
          else if (k === 'rename') {
            /* 改名（改版四十）：默认值给**去掉后缀的当前名**，改完就地重拉列表。
               ⚠️ 冲突 / 非法字符由后端判（409 / 400），前端不抢着校验 ——
                  两处各写一套规则，迟早会在"哪些字符算非法"上分叉。 */
            const cur = String(r.id || '');
            const to = window.prompt('改名（只改文件名，内容一个字节都不动；'
              + '关联文件会跟着搬）', cur);
            if (to == null) return;
            const v = String(to).trim();
            if (!v || v === cur) return;
            C.renameRecord(r.id, v).then(ok => {
              if (!ok) return;                       // 失败已经 toast/日志说过了
              C.toast('ok', '已改名', cur + ' → ' + v);
              this.paint();
            });
          }
          else if (k === 'del') {
            /* 两步确认（与维护页同一套口径）：点一次变红写着"确认删除"，
               5 s 内再点才真删。**不可逆**，所以不做"点了就删"。
               ⚠️ 判定按 **(时间, 记录 id) 一对**：只看时间的话，点完 A 的删除
                  再去点 B 的删除，B 会被当成"第二次点击"立刻删掉。 */
            if (!delArmed(r.id)) {
              delArm = Date.now();
              delArmId = r.id;
              b.textContent = '确认删除？（5s）';
              b.classList.add('btn-danger');
              return;
            }
            delArm = 0;
            delArmId = '';
            b.disabled = true; b.textContent = '删除中…';
            C.deleteRecord(r.id).then(() => {
              C.toast('warn', '已删除', r.id + ' 及其关联文件');
              delete checked[r.id];
              this.paint();
            });
          }
        };
      });

      if (assoc) {
        box.querySelectorAll('[data-link]').forEach(b => {
          b.onclick = () => {
            const vid = b.dataset.link;
            C.linkVideo(r.id, vid).then((done) => {
              if (!done) return;                 // 失败已经 toast/日志说过了
              r.video = vid;
              assoc = false;
              C.toast('ok', '已关联', r.id + ' ← ' + vid);
              this.paint();
            });
          };
        });
        const un = box.querySelector('[data-unlink]');
        if (un) un.onclick = () => {
          C.linkVideo(r.id, null).then((done) => {
            if (!done) return;
            r.video = ''; assoc = false;
            C.toast('warn', '已解除关联', r.id);
            this.paint();
          });
        };
      }
    },

    assocHtml(r) {
      const list = S.videoSessions;
      return '<div class="assoc">' +
        '<div class="assoc-head">选择要挂到 ' + U.esc(r.id) + ' 的视频会话' +
          (r.video ? '<button class="btn btn-sm btn-ghost" data-unlink>解除关联</button>' : '') + '</div>' +
        (list.length
          ? '<div class="assoc-list">' + list.map(v =>
              '<button class="vid-row' + (v.id === r.video ? ' on' : '') + '" data-link="' + v.id + '">' +
              '<span><span class="v-name">' + v.id + '</span>' +
              '<span class="v-meta">' + v.video + ' · ' + v.n_valid + '/' + v.n_total + ' 点</span></span>' +
              '<span class="tag ' + (v.flags.length ? 'warn' : 'ok') + '">' + (v.flags.length ? '有标志' : '正常') + '</span>' +
              '</button>').join('') + '</div>'
          : '<div class="empty"><b>没有视频会话</b></div>') +
        '</div>';
    },

    /* 高度跟着窗口/日志坞走（列表吃满剩余高度，内部滚）。
       `onTick` 每 500 ms 会调一次，`fitMain()` 里值没变就不碰 DOM。 */
    onTick() {
      C.fitMain();
      this._n = (this._n || 0) + 1;
      if (this._n % 4) return;              // 2 s 一次：两步确认的文案要能自己恢复
      const staleDel = !!delArm && !delArmed(delArmId);
      const staleBulk = !!bulkArm && !bulkArmed();
      if (!staleDel && !staleBulk) return;
      if (staleDel) { delArm = 0; delArmId = ''; }
      if (staleBulk) bulkArm = 0;
      this.paint();
    },
    onState() { this.paint(); },
  };
})();
