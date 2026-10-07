/* ③ 参数 —— 21 项，**分两套**（生产标定 7 / 课程调参 14）。
 *
 * 为什么要分开显示而不是拍平一张表：
 *   两套参数在**不同的 Flash 页**（生产 99 / 课程 100），**写入通道也不同** ——
 *   生产走 `mc.params.set_many`，课程走 `mc.lesson.set_params`。
 *   上游 CHANGELOG 专门提过这条：`mc.lesson.set_params()` 只接受课程命名空间，
 *   把生产参数喂进去会失败。拍平一张表的话，用户改一项就有一半概率走错通道。
 *   所以这里按"该走哪条路"分组，写回时由服务端自动分流。
 *
 * 另外：写入**默认带回读校验**；界面显示"ACK + 回读值 + 是否一致"。
 * Q16.16 定点与十进制互显（get_raw / set_raw），否则现场算不对。
 */

C.views.params = (function () {
  let el, groups = {}, dirty = {}, connected = false;

  function mount(host) {
    el = host;
    el.innerHTML = `
      <h2>③ 参数 <span class="tag" id="p-count"></span></h2>
      <div class="gatehint" id="p-link-hint"></div>
      <div class="pane">
        <h4>两套参数
          <span>
            <label class="hint"><input type="checkbox" id="p-readback" checked> 回读校验</label>
            <button class="mini" id="p-reload">读取全部</button>
            <button class="mini" id="p-write">写入改动</button>
          </span></h4>
        <div class="hint" id="p-top"></div>
        <div id="p-groups"></div>
        <div class="row wrap" style="margin-top:8px">
          <button class="mini" id="p-save-prod">SAVE 生产 · page 99</button>
          <button class="mini" id="p-save-lesson">SAVE 课程 · page 100</button>
          <button class="mini" id="p-backup">备份</button>
          <button class="mini" id="p-reset">RESET</button>
        </div>
        <div class="hint" id="p-msg"></div>
      </div>`;

    el.querySelector('#p-reload').onclick = function () { load(); };
    el.querySelector('#p-write').onclick = function () { write(); };
    el.querySelector('#p-save-prod').onclick = function () { saveGroup('生产标定'); };
    el.querySelector('#p-save-lesson').onclick = function () { saveGroup('课程调参'); };
    el.querySelector('#p-backup').onclick = function () { act('backup'); };
    el.querySelector('#p-reset').onclick = function () { act('reset'); };
    el.querySelector('#p-groups').addEventListener('input', function (e) {
      if (e.target.dataset.name) dirty[e.target.dataset.name] = e.target.value;
    });
    load();
    return el;
  }

  async function load() {
    /* 没连鼠时**一个请求都不发**：读参数走的正是那条被命令锁把着的链路，
       未连接时发出去只会白等一轮，再弹一条"读不到参数"的错（而真正的原因
       在上面那行 gatehint 里已经写清楚了）。 */
    if (C.needMouse('读参数')) {
      el.querySelector('#p-groups').innerHTML =
        '<div class="hint">未连接电脑鼠</div>';
      el.querySelector('#p-count').textContent = '';
      return;
    }
    const d = await C.guard(function () {
      return C.api('/api/v1/mouse/params/grouped');
    }, '读参数');
    if (!d) {
      el.querySelector('#p-groups').innerHTML =
        '<div class="hint">参数读取失败</div>';
      return;
    }
    groups = d.groups || {};
    connected = !!d.connected;
    filterZeroSpeed();
    dirty = {};
    render(d);
  }

  /* ⚠️ SPEEDL/SPEEDR 是**派生量**（恒等于 TRACKL/TRACKR 除以 100），直接给输入框
     只会让人两处写岔。所以把它们**只读展示**，要改就改 TRACK 再让固件派生。
     （上游声明的约束："speed 恒等于 track 除以 100"。） */
  function filterZeroSpeed() {
    const prod = groups['生产标定'] || {};
    ['SPEEDL', 'SPEEDR'].forEach(function (n) {
      if (prod[n]) prod[n]._derived = true;
    });
  }

  function render(d) {
    const keys = Object.keys(groups);
    el.querySelector('#p-count').textContent = (d.count || 0) + ' 项';
    el.querySelector('#p-top').innerHTML = C.util.esc(d.note || '');
    if (!keys.length) {
      el.querySelector('#p-groups').innerHTML = '<div class="hint">无参数数据</div>';
      return;
    }
    el.querySelector('#p-groups').innerHTML = keys.map(function (g) {
      const items = groups[g];
      const names = Object.keys(items);
      const page = items[names[0]] ? items[names[0]].flash_page : '?';
      const via = items[names[0]] ? items[names[0]].write_via : '?';
      return '<div class="pane" style="margin-top:8px">' +
        '<h4>' + C.util.esc(g) + ' · ' + names.length + ' 项' +
        ' <span class="hint">Flash page ' + page + ' · <code>' +
        C.util.esc(via) + '</code></span></h4>' +
        '<div class="scroll"><table>' +
        '<thead><tr><th>id</th><th>名字</th><th>十进制</th><th>Q16.16 原值</th>' +
        '<th>单位</th><th>说明</th></tr></thead><tbody>' +
        names.map(function (n) {
          const it = items[n];
          const cell = it._derived
            ? '<span class="dim" title="派生量 = TRACK/100">' +
              C.util.fmt(it.value, 6) + ' 派生</span>'
            : '<input data-name="' + C.util.esc(n) + '" value="' +
              (dirty[n] !== undefined ? dirty[n] : C.util.fmt(it.value, 6)) +
              '" style="width:120px">';
          return '<tr><td>' + C.util.esc(C.util.fmt(it.id)) + '</td>' +
            '<td><b>' + C.util.esc(n) + '</b></td>' +
            '<td>' + cell + '</td>' +
            '<td>' + C.util.esc(C.util.fmt(it.q)) + '</td>' +
            '<td>' + C.util.esc(it.unit || '') + '</td>' +
            '<td>' + C.util.esc(it.desc || '') + '</td></tr>';
        }).join('') + '</tbody></table></div></div>';
    }).join('');
    msg('共 ' + (d.count || 0) + ' 项，分 ' + keys.length + ' 组');
  }

  function msg(t) { el.querySelector('#p-msg').textContent = t; }

  async function write() {
    const ks = Object.keys(dirty);
    if (!ks.length) { msg('无改动'); return; }
    const body = { values: {}, readback: el.querySelector('#p-readback').checked };
    ks.forEach(function (k) { body.values[k] = Number(dirty[k]); });
    const d = await C.guard(function () {
      return C.api('/api/v1/mouse/params/grouped', { method: 'POST', body: body });
    }, '写参数');
    if (!d) return;
    const all = (d.prod || []).concat(d.lesson || []);
    msg(all.map(function (r) {
      return (r.ok ? 'OK  ' : 'FAIL ') + r.name + ' status=' + r.status;
    }).join('\n') + '\n通道：' + JSON.stringify(d.via));
    const bad = all.filter(function (r) { return !r.ok; }).length;
    C.toast(bad ? (bad + ' 条写入失败') : ('全部写入成功 ' + all.length + ' 条'),
      bad ? 'bad' : 'ok');
    dirty = {};
    load();
  }

  async function saveGroup(group) {
    if (!confirm('将「' + group + '」写入 Flash page ' +
                 (group === '课程调参' ? 100 : 99) + '？\n' +
                 '两套参数分属不同 Flash 页，分别保存。')) return;
    const d = await C.guard(function () {
      return C.api('/api/v1/mouse/params/save_group', { method: 'POST', body: { group: group } });
    }, 'SAVE');
    if (d) msg(JSON.stringify(d, null, 1));
  }

  async function act(what) {
    if (what === 'reset' && !confirm('RESET 将擦除参数页，重启后生效。继续？')) return;
    const d = await C.guard(function () {
      return C.api('/api/v1/mouse/params/' + what, { method: 'POST', body: {} });
    }, what);
    if (d) msg(JSON.stringify(d, null, 1).slice(0, 600));
  }

  return {
    mount: mount,
    activate: function () { if (!Object.keys(groups).length) load(); },
    onMsg: function () { },
    /* 参数页的每个动作都要走链路（读、写、备份、复位都是命令）。
       不门控的：回读校验那个勾选框、以及 Q16.16 换算面板（纯前端算术）。
       ⚠️ 这里**不发任何请求** —— onHealth 每 2.5 s 广播一次，加载留给 activate()。 */
    onHealth: function () {
      const why = C.needMouse('读/写参数');
      ['p-reload', 'p-write', 'p-save-prod', 'p-save-lesson', 'p-reset',
       'p-backup'].forEach(function (id) {
        C.gate(el.querySelector('#' + id), why);
      });
      const hint = el.querySelector('#p-link-hint');
      if (hint) hint.textContent = why || '';
    },
    context: function (box) {
      box.innerHTML = `
        <div class="card">
          <h3>两套参数</h3>
          <div class="kv">
            <b>生产标定</b><span>id 1–7 · Flash page <b>99</b><br>TRACKL/R、SPEEDL/R、GYRORATIO、ENCANGLE、WALLDIST</span>
            <b>课程调参</b><span>id 8–21 · Flash page <b>100</b><br>KPL…KDR、IRPL/R、GYROP/D、SPEEDNEED、ALLDIS、SENSORMODE、WALLMODE</span>
          </div>
        </div>`;
    }
  };
})();
