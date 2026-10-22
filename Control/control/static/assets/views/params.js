/* ═══ 参数设定：41 项 / 四组 / 按需读取 ══════════════════════════════════ */
(function () {
  const S = C.S, U = C.util;
  let cur = 0;
  let q = '';
  let dirty = {};
  let stamp = null;

  C.VIEWS.params = {
    eyebrow: '小车控制 · 参数表',
    title: '参数设定',
    sub: '41 项 · 四组。',

    actions() {
      const n = Object.keys(dirty).length;
      return '<span class="pill idle"><i></i>' + (stamp ? '读取于 ' + U.hhmmss(stamp) : '未读取') + '</span>' +
        '<button class="btn btn-sm btn-ghost" id="pp-reload">' + C.icon('refresh') + '读取全部</button>' +
        '<button class="btn btn-sm ' + (n ? 'btn-primary' : 'btn-ghost') + '" id="pp-write">写入改动' +
        (n ? ' · ' + n : '') + '</button>';
    },

    mount(root, actions) {
      this._root = root;
      if (!stamp) stamp = Date.now() - 42000;
      root.innerHTML =
        '<div class="params-wrap">' +
          '<section class="card">' +
            '<div class="card-head"><h2>分组</h2>' +
              '<div class="card-head-r"><span class="hint mono">id 段</span></div></div>' +
            '<div class="grp-list" id="pp-groups"></div>' +
            '<div class="grp-meta" id="pp-meta"></div>' +
          '</section>' +
          '<section class="card">' +
            '<div class="card-head">' +
              '<h2>参数表</h2>' +
              '<div class="card-head-r">' +
                '<label class="search" style="min-width:190px">' +
                  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="6"/><path d="M16 16l4 4"/></svg>' +
                  '<input id="pp-q" placeholder="按名字或说明过滤" value="' + U.esc(q) + '">' +
                '</label>' +
                '<label class="switch"><input type="checkbox" id="pp-readback" checked> 回读校验</label>' +
              '</div>' +
            '</div>' +
            '<div class="card-body flush"><div class="tbl-scroll"><table class="tbl">' +
              '<thead><tr><th style="width:42px">ID</th><th style="width:150px">名称</th><th style="width:132px">值</th>' +
              '<th style="width:92px">Q16.16</th><th style="width:130px">单位</th><th>说明</th></tr></thead>' +
              '<tbody id="pp-rows"></tbody>' +
            '</table></div></div>' +
            '<div class="card-body" style="border-top:1px solid var(--line);padding:9px 14px">' +
              '<div class="row wrap"><span class="hint" id="pp-msg"></span>' +
                '<span class="spacer"></span>' +
                '<button class="btn btn-sm" id="pp-save">' + C.icon('save') + 'SAVE · page 115</button>' +
              '</div>' +
            '</div>' +
          '</section>' +
        '</div>' +

        '<section class="card" style="margin-top:12px">' +
          '<div class="card-head"><h2>四组 · id 分布</h2>' +
            '<div class="card-head-r"><span class="hint">41 项 / 4 组 · id 8–21 未用</span></div></div>' +
          '<div class="card-body">' +
            '<div class="gmap" id="pp-map"></div>' +
            '<div class="hint" style="margin-top:9px">' +
              '只有<b>运动控制</b>组（39–55）随远程档生效；' +
              '<code>SPEEDL</code> / <code>SPEEDR</code> 是 <code>TRACK/100</code> 的派生量，写了也读不回来。' +
            '</div>' +
          '</div>' +
        '</section>';

      root.querySelector('#pp-groups').onclick = e => {
        const b = e.target.closest('[data-grp]');
        if (!b) return;
        cur = Number(b.dataset.grp);
        this.render();
      };
      const qi = root.querySelector('#pp-q');
      qi.oninput = () => { q = qi.value.trim(); this.renderRowsOnly(); };
      root.querySelector('#pp-rows').addEventListener('input', e => {
        const inp = e.target.closest('[data-name]');
        if (!inp) return;
        dirty[inp.dataset.name] = inp.value;
        inp.classList.add('dirty');
        inp.closest('tr').classList.add('row-dirty');
        this.updateDirty();
      });
      root.querySelector('#pp-save').onclick = () => {
        C.toast('warn', 'SAVE = 写 Flash', '擦写会打断 1 kHz 控制环，约 120 ms。\n确认后写 page 115。');
        C.log('mouse', 'info', '→ SAVE page 115 · 生产命名空间');
      };
      const rl = actions && actions.querySelector('#pp-reload');
      if (rl) rl.onclick = () => {
        stamp = Date.now();
        this.render(); C.emit('state');
        C.toast('ok', '参数已读取', '41 项 · 用时 1.21 s（一次往返）');
      };
      const wr = actions && actions.querySelector('#pp-write');
      if (wr) wr.onclick = () => this.write();

      this.render();
    },

    render() {
      const root = this._root;
      if (!root) return;
      const G = S.params;
      root.querySelector('#pp-groups').innerHTML = G.map((g, i) =>
        '<button class="grp-item' + (i === cur ? ' on' : '') + '" data-grp="' + i + '">' +
          '<span class="g-no">' + String(i + 1).padStart(2, '0') + '</span>' +
          '<span><span class="g-name">' + g.name + '</span><span class="g-range">id ' + g.range + '</span></span>' +
          '<span class="n">' + g.items.length + '</span></button>').join('');
      const g = G[cur];
      root.querySelector('#pp-meta').innerHTML =
        '<div><span>Flash page</span><b>' + g.page + '</b></div>' +
        '<div><span>写入</span><b>' + g.via + '</b></div>' +
        '<div><span>本组</span><b>' + g.items.length + ' 项</b></div>' +
        '<div style="display:block;font-size:10px;line-height:1.4;margin-top:2px">' + g.note + '</div>';

      /* id 分布条：按项数分宽，当前组反白 */
      root.querySelector('#pp-map').innerHTML = G.map((gg, i) =>
        '<div class="gm' + (i === cur ? ' on' : '') + '" style="flex:' + gg.items.length + '" data-gmap="' + i + '">' +
          '<b>id ' + gg.range + '</b>' +
          '<span>' + gg.name + '</span>' +
          '<em>' + gg.items.length + ' 项' + (gg.page ? ' · p' + gg.page : '') + '</em>' +
        '</div>').join('');
      root.querySelector('#pp-map').onclick = e => {
        const b = e.target.closest('[data-gmap]');
        if (!b) return;
        cur = Number(b.dataset.gmap);
        this.render();
      };
      this.renderRowsOnly();
    },

    renderRowsOnly() {
      const root = this._root;
      if (!root) return;
      const g = S.params[cur];
      const kw = q.toLowerCase();
      const items = g.items.filter(it => !kw || it.name.toLowerCase().indexOf(kw) >= 0 || it.desc.indexOf(kw) >= 0);
      const tb = root.querySelector('#pp-rows');
      tb.innerHTML = items.map(it => {
        const isDirty = dirty[it.name] !== undefined;
        return '<tr class="' + (isDirty ? 'row-dirty' : '') + '">' +
          '<td class="num dim">' + it.id + '</td>' +
          '<td class="name"><b>' + it.name + '</b></td>' +
          '<td>' + (it.derived
            ? '<span class="dim mono" title="派生量 = TRACK/100">' + it.value + '</span>'
            : '<input class="cell-input" data-name="' + it.name + '" value="' +
               U.esc(isDirty ? dirty[it.name] : it.value) + '">') + '</td>' +
          '<td class="num dim">' + (it.derived ? '—' : it.q16) + '</td>' +
          '<td class="dim" style="font-size:10px">' + U.esc(it.unit || '—') +
            (it.enumText ? '<div style="color:var(--muted-2)">' + it.enumText + '</div>' : '') + '</td>' +
          '<td class="dim" style="font-size:11px">' + U.esc(it.desc) + '</td>' +
        '</tr>';
      }).join('') || '<tr><td colspan="6"><div class="empty"><b>没有匹配项</b><span>换个关键字</span></div></td></tr>';
      const msg = root.querySelector('#pp-msg');
      if (msg) {
        const total = S.params.reduce((a, x) => a + x.items.length, 0);
        msg.textContent = '共 ' + total + ' 项 · 分 ' + S.params.length +
          ' 组 · ' + (Object.keys(dirty).length ? (Object.keys(dirty).length + ' 项待写入') : '无改动');
      }
    },

    updateDirty() {
      const root = this._root;
      if (!root) return;
      const msg = root.querySelector('#pp-msg');
      if (msg) {
        msg.textContent = '共 41 项 · 分 4 组 · ' +
          (Object.keys(dirty).length ? (Object.keys(dirty).length + ' 项待写入') : '无改动');
      }
      const wr = document.querySelector('#page-actions #pp-write');
      if (wr) {
        const n = Object.keys(dirty).length;
        wr.textContent = '写入改动' + (n ? ' · ' + n : '');
        wr.className = 'btn btn-sm ' + (n ? 'btn-primary' : 'btn-ghost');
      }
    },

    write() {
      const n = Object.keys(dirty).length;
      if (!n) { C.toast('warn', '没有改动', '先改几个值再写入。'); return; }
      C.log('mouse', 'info', '→ SET ' + n + ' 项 · 回读校验通过 ' + n + '/' + n);
      C.toast('ok', '全部写入成功', n + ' 条 · 回读校验一致');
      dirty = {};
      this.render();
      C.emit('state');
    },

    onState() {},
  };
})();
