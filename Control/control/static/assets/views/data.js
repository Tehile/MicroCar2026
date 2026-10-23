/* ═══ 记录 · 比赛记录：发车记录 / 视频记录 两个标志 + 关联 + 回放 ══════ */
(function () {
  const S = C.S, U = C.util;
  let selId = '';
  let assoc = false;
  let delArm = 0;      // 删除的**两步确认**：第一次点击的时刻（0 = 没在等确认）
  let delArmId = '';   // 等的是哪一条（不然点 A 再点 B 会被当成确认 B）

  /* 这一条是否正"等第二次点击"。**必须按 id 判**：只按时间判的话，
     用户点 A 的删除 → 又点 B 的删除，B 会被立刻删掉（一次都没确认过）。
     而 paint 会重画按钮（看门狗 2 s 一次 `emit('health')`），所以文本也要靠它恢复，
     否则第一次点击后那 0.6 s 里文本会被重绘回"删除记录" ⇒ 用户看不出要再点一次。 */
  function delArmed(id) {
    return !!delArm && delArmId === id && (Date.now() - delArm) <= 5000;
  }

  function dur(s) {
    const m = Math.floor(s / 60), r = Math.round(s % 60);
    return m + ':' + String(r).padStart(2, '0');
  }
  function session(id) {
    return S.videoSessions.find(v => v.id === id) || null;
  }

  C.VIEWS.data = {
    eyebrow: '记录 · 比赛记录',
    title: '比赛记录',
    sub: '',

    actions() {
      const n = S.records.length;
      const nl = S.records.filter(r => r.launch).length;
      const nv = S.records.filter(r => r.video).length;
      return '<span class="pill idle"><i></i>' + n + ' 条</span>' +
        '<span class="pill ' + (nl === n ? 'ok' : 'idle') + '"><i></i>发车 ' + nl + '</span>' +
        '<span class="pill ' + (nv === n ? 'ok' : 'idle') + '"><i></i>视频 ' + nv + '</span>' +
        '<button class="btn btn-sm btn-ghost">' + C.icon('download') + '导出</button>';
    },

    mount(root) {
      this._root = root;
      if (!selId) selId = S.recSel || (S.records[0] && S.records[0].id) || '';
      /* **控制面板在上、列表在下**：选中条目后要能立刻操作，而列表会长
         （真机几十条），面板放底部就会被列表推到视野外 ⇒ 面板置顶 + 吸顶，
         列表自己滚，两者互不干扰。 */
      root.innerHTML =
        '<section class="card dt-ctl" id="dt-ctl"></section>' +
        '<section class="card dt-list">' +
          '<div class="card-head"><h2>记录列表</h2>' +
            '<div class="card-head-r"><span class="hint mono" id="dt-count"></span></div></div>' +
          '<div class="card-body flush dt-list-scroll">' +
            '<table class="tbl"><thead><tr>' +
              '<th>记录</th><th style="width:92px">开始</th><th style="width:74px">时长</th>' +
              '<th style="width:74px">帧</th><th style="width:150px">发车记录</th>' +
              '<th style="width:200px">视频记录</th><th style="width:80px">账本</th>' +
            '</tr></thead><tbody id="dt-rows"></tbody></table>' +
          '</div>' +
        '</section>';

      root.querySelector('#dt-rows').onclick = e => {
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
      this.paint();
    },

    paint() {
      const root = this._root;
      if (!root || !root.querySelector('#dt-rows')) return;

      root.querySelector('#dt-rows').innerHTML = S.records.map(r => {
        const ses = session(r.video);
        return '<tr class="sess-row' + (r.id === selId ? ' sel' : '') + '" data-id="' + r.id + '">' +
          '<td class="num" style="color:var(--ink)">' + r.id + '</td>' +
          '<td class="num dim">' + r.start + '</td>' +
          '<td class="num dim">' + dur(r.dur) + '</td>' +
          '<td class="num">' + r.frames + '</td>' +
          '<td>' + (r.launch
            ? '<span class="tag ok">发车 @' + U.fmt(r.launchAt, 1) + ' s</span>'
            : '<span class="tag">无</span>') + '</td>' +
          '<td>' + (r.video
            ? '<span class="tag blue">' + r.video + '</span>'
            : '<button class="btn btn-sm btn-ghost" data-assoc="' + r.id + '">' + C.icon('link') + '关联</button>') + '</td>' +
          // 三态：true=通过 / false=丢帧 / null=**判不了**（老记录没有丢帧计数）
          // 判不了不许画成"差 X 帧"—— 那是在替设备作证
          '<td>' + (r.ledger === true
            ? '<span class="tag ok">通过</span>'
            : r.ledger === false
              ? '<span class="tag bad">差 ' + (r.gap == null ? '?' : r.gap) + ' 帧</span>'
              : '<span class="tag">判不了</span>') + '</td>' +
          '</tr>';
      }).join('');

      const cnt = root.querySelector('#dt-count');
      if (cnt) {
        const nl = S.records.filter(r => r.launch).length;
        const nv = S.records.filter(r => r.video).length;
        cnt.textContent = S.records.length + ' 条 · 发车 ' + nl + ' · 视频 ' + nv;
      }

      const r = C.record(selId) || S.records[0];
      if (!r) return;
      selId = r.id;
      const ses = session(r.video);
      const box = root.querySelector('#dt-ctl');

      const info =
        '<div class="card-head dt-head">' +
          '<h2><span class="n">记录</span>' + r.id + '</h2>' +
          '<div class="dt-head-r">' +
            '<span class="tag ' + (r.launch ? 'ok' : '') + '">' +
              (r.launch ? '发车 @' + U.fmt(r.launchAt, 1) + ' s' : '无发车记录') + '</span>' +
            '<span class="tag ' + (r.video ? 'blue' : '') + '">' +
              (r.video ? r.video : '无视频记录') + '</span>' +
            '<div class="dt-acts">' +
              '<button class="btn btn-sm btn-primary" data-rp="go"' + (r.frames > 0 ? '' : ' disabled') + '>' +
                C.icon('play') + '回放</button>' +
              '<button class="btn btn-sm" data-rp="csv">' + C.icon('download') + 'CSV</button>' +
              '<button class="btn btn-sm" data-rp="dir">目录</button>' +
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
            '<div><dt>开始</dt><dd>' + r.start + '</dd></div>' +
            '<div><dt>时长</dt><dd>' + dur(r.dur) + '</dd></div>' +
            '<div><dt>遥测帧</dt><dd>' + r.frames + '</dd></div>' +
            '<div><dt>发车时刻</dt><dd>' + (r.launch ? U.fmt(r.launchAt, 1) + ' s' : '—') + '</dd></div>' +
            '<div><dt>视频会话</dt><dd>' + (r.video || '未关联') + '</dd></div>' +
            '<div><dt>轨迹点</dt><dd>' + (ses ? ses.n_valid : '—') + '</dd></div>' +
            '<div><dt>帧缺口</dt><dd class="' + (r.gap ? 'bad' : '') + '">' + r.gap + '</dd></div>' +
            '<div><dt>目录</dt><dd class="ell" title="' + r.dir + '">' + r.dir + '</dd></div>' +
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
        '<div class="assoc-head">选择要挂到 ' + r.id + ' 的视频会话' +
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

    onState() { this.paint(); },
  };
})();
