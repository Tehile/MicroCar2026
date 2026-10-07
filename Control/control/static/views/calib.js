/* ④ 标定 —— 6 项物理系数 + **相机/场地标定**。
   场地标定是本整合层强加给流程的一步，理由是它错了**不报任何错**：
   y_flip 猜错只会让整张图镜像，而图看着完全正常。 */

C.views.calib = (function () {
  let el, obs = [];

  /* 每项只留**摆放/操作**（`how`）—— 它是动手前必须知道的一句；
     "为什么这么做"属于文档，不进界面。 */
  const ITEMS = [
    { id: 'gyro', name: 'GYRO', title: '陀螺标定', how: '车静止 1.5 s' },
    { id: 'wall_distance', name: 'WALLDIST', title: '壁距标定', how: '车平行贴左墙，车心到墙约 33 mm' },
    { id: 'ir_data', name: 'IRDATA', title: '红外逐点', how: '按 4 个摆位依次采集：左近/左远/前近/前远，顺序固定' },
    { id: 'angle', name: 'GYRORATIO + ENCANGLE', title: '角度标定', how: '在迷宫角格原地转一圈' },
    { id: 'track', name: 'TRACK', title: '里程标定', how: '车直行已知距离 20000 脉冲' },
    { id: 'step', name: 'CAL <item> <act>', title: '手工单步', how: 'act ∈ START / SAMPLE / ABORT' }
  ];

  function mount(host) {
    el = host;
    el.innerHTML = `
      <h2>④ 标定</h2>
      <div class="gatehint" id="c-link-hint"></div>
      <div class="grid2">
        <div class="pane">
          <h4>小车侧 6 项</h4>
          <div id="c-items"></div>
        </div>
        <div class="pane">
          <h4>相机 / 场地</h4>
          <div id="c-arena"></div>
        </div>
      </div>
      <div class="pane" style="margin-top:10px">
        <h4>标定报告 · mouse/calib/report.md</h4>
        <textarea id="c-report" style="width:100%;height:110px;background:#0e1015;color:var(--fg);
          border:1px solid var(--line);border-radius:4px;font-family:var(--mono);font-size:11px"></textarea>
        <div class="row"><button class="mini" id="c-save-report">写入会话</button>
          </div>
      </div>`;
    renderItems();
    renderArena();
    el.querySelector('#c-save-report').onclick = async function () {
      const txt = el.querySelector('#c-report').value.trim();
      if (!txt) return;
      const d = await C.guard(function () {
        return C.api('/api/v1/mouse/calib/report', { method: 'POST', body: { lines: txt } });
      }, '写报告');
      if (d) C.toast('报告已写入 ' + (d.path || 'mouse/calib/report.md'), 'ok');
      else {
        await C.guard(function () {
          return C.api('/api/v1/session/note', { method: 'POST', body: { text: txt.slice(0, 4000) } });
        }, '记录备注');
      }
    };
    loadArena();
    return el;
  }

  function renderItems() {
    const box = el.querySelector('#c-items');
    box.innerHTML = ITEMS.map(function (it, i) {
      return `<div style="border-bottom:1px solid var(--line);padding:6px 0">
        <div><b>${it.title}</b> <span class="tag">${it.name}</span>
          <button class="mini" data-run="${it.id}" style="float:right">跑</button></div>
        <div class="hint">${it.how}</div>
      </div>`;
    }).join('');
    box.querySelectorAll('[data-run]').forEach(function (b) {
      b.onclick = async function () {
        b.disabled = true;
        const d = await C.guard(function () {
          return C.api('/api/v1/mouse/calib/' + b.dataset.run, { method: 'POST', body: {} });
        }, '标定 ' + b.dataset.run);
        b.disabled = false;
        if (d) {
          const txt = JSON.stringify(d.result, null, 1);
          el.querySelector('#c-report').value += '\n## ' + b.dataset.run + '\n' + txt;
          C.toast(b.dataset.run + ' 完成：' + JSON.stringify(d.result).slice(0, 160), 'ok', 9000);
        }
        loadArena();
      };
    });
  }

  function renderArena() {
    el.querySelector('#c-arena').innerHTML = `
            <div class="row"><label class="hint" style="flex:0 0 96px">相机高度 mm</label>
        <input id="c-height" placeholder="镜头光心离地高度，±20%" style="flex:1"></div>
      <div class="row"><label class="hint" style="flex:0 0 96px">天底点 nx,ny</label>
        <input id="c-nadir" value="0.5, 0.5" style="flex:1"></div>
      <div class="row">
        <button class="mini" id="c-addobs">记录观测</button>
        <input id="c-fw" placeholder="固件格 x,y，留空自动读取" style="flex:1">
      </div>
      <div class="row"><button class="mini" id="c-solve">解 flip</button>
        <span class="hint" id="c-solve-state"></span></div>
      <div class="scroll" style="max-height:150px;margin-top:6px"><table id="c-obs"></table></div>
      <button class="mini" id="c-apply" style="margin-top:6px">落盘至 Control/config.yaml</button>
      <div class="hint" id="c-arena-msg"></div>`;

    el.querySelector('#c-addobs').onclick = function () { addObs(); };
    el.querySelector('#c-solve').onclick = function () { solve(false); };
    el.querySelector('#c-apply').onclick = function () { solve(true); };
    renderObs();
  }

  function renderObs() {
    const t = el.querySelector('#c-obs');
    if (!obs.length) {
      t.innerHTML = '<tr><td class="hint">无观测点，至少 2 个</td></tr>';
      return;
    }
    t.innerHTML = '<thead><tr><th>#</th><th>固件格</th><th>视觉归一化</th></tr></thead><tbody>' +
      obs.map(function (o, i) {
        return '<tr><td>' + i + '</td><td>' + o.fw_xy.join(',') + '</td><td>' +
          o.vis_xy.map(function (v) { return Number(v).toFixed(4); }).join(', ') + '</td></tr>';
      }).join('') + '</tbody>';
  }

  async function addObs() {
    const fwv = el.querySelector('#c-fw').value.trim();
    const body = {};
    if (fwv) {
      const p = fwv.split(/[,\s]+/).map(Number);
      if (p.length < 2 || p.some(isNaN)) { C.toast('固件格格式：x,y 整数格号', 'bad'); return; }
      body.fw_xy = [p[0], p[1]];
    }
    const d = await C.guard(function () {
      return C.api('/api/v1/arena/observations', { method: 'POST', body: body });
    }, '记录观测');
    if (d) { obs = d.observations; renderObs(); C.toast('已记录观测点 ' + obs.length, 'ok'); }
  }

  async function solve(apply) {
    if (obs.length < 2) { C.toast('至少需要 2 个观测点', 'warn'); return; }
    const h = el.querySelector('#c-height').value.trim();
    const nd = el.querySelector('#c-nadir').value.trim().split(/[,\s]+/).map(Number);
    if (apply && h && !isNaN(Number(h))) {
      await C.guard(function () {
        return C.api('/api/v1/arena/calibrate', { method: 'POST', body: {
          y_flip: true, x_flip: false, camera_height_mm: Number(h),
          nadir_xy: (nd.length === 2 && !nd.some(isNaN)) ? nd : undefined,
          calibrated: false, note: '仅写入相机高度' } });
      }, '落相机高度');
    }
    const d = await C.guard(function () {
      return C.api('/api/v1/arena/solve', { method: 'POST', body: { observations: obs, apply: !!apply } });
    }, '解 flip');
    if (!d) return;
    const b = d.best;
    let txt = '最优 y_flip=' + b.y_flip + ' x_flip=' + b.x_flip +
      '  rms=' + b.rms_cells + ' 格，次优 rms=' + d.second.rms_cells +
      ' 格，margin=' + d.margin_cells;
    if (!d.confident) txt += '\n' + d.warning;
    if (d.applied) txt += '\n已落盘并注入定位器';
    else if (d.apply_refused) txt += '\n未落盘：' + d.apply_refused;
    el.querySelector('#c-solve-state').textContent = txt;
    C.toast(txt, d.confident ? 'ok' : 'warn', 15000);
  }

  async function loadArena() {
    const d = await C.guard(function () { return C.api('/api/v1/arena'); });
    if (!d) return;
    const a = d.arena || {};
    if (a.camera_height_mm && !el.querySelector('#c-height').value) {
      el.querySelector('#c-height').value = a.camera_height_mm;
    }
    if (a.nadir_xy) el.querySelector('#c-nadir').value = a.nadir_xy.join(', ');
    el.querySelector('#c-arena-msg').textContent =
      (a.calibrated ? '已标定' : '未标定')
      + '\nfw_to_arena = ' + JSON.stringify(a.fw_to_arena);
  }

  return {
    mount: mount,
    activate: function () { loadArena(); },
    /* 鼠侧 6 项要驱动电机、读传感器 ⇒ 没连上就没意义，按钮先关门。
       不门控的：#c-height / #c-nadir（卷尺 + 眼睛就能填）、#c-solve / #c-apply
       （纯本地计算与落盘）—— 这几个恰恰是"没鼠的时候也能先把场地几何填好"的部分。 */
    onHealth: function () {
      const why = C.needMouse('标定');
      el.querySelectorAll('[data-run]').forEach(function (b) { C.gate(b, why); });
      C.gate(el.querySelector('#c-addobs'), why);      // 要读固件格号
      const hint = el.querySelector('#c-link-hint');
      if (hint) hint.textContent = why || '';
    },
    onMsg: function (m) {
      if (m.type === 'arena.changed') loadArena();
      if (m.type === 'locate.result' && m.valid) {
        el.querySelector('#c-fw').placeholder = '固件格 x,y ｜ 视觉 x=' +
          Number(m.x).toFixed(3) + ' y=' + Number(m.y).toFixed(3);
      }
    },
    context: function (box) {
      box.innerHTML = '';
    }
  };
})();
