/* 「实时状态与参数设定」的一个**区块**：标定例程（鼠侧五项 + 场地/相机几何 + 报告）。

   ⚠️ 2026-10-07（改版十二）起它**不再是一个独立模块**，而是 `mod_params` 那一页里的
   一个 pane（理由见 `mod_motion.js` 顶部：上游 2.4.0 压档"调车"成了一个连续阶段）。
   ⇒ 注册成 `C.parts.calib`，由 `mod_params.mount()` 挂进自己的 pane。
   id 一律带 `mc-` 前缀，与同一页里的 `mp-`（读数/参数）/`mm-`（运动）互不干扰 ——
   四个区块合并到同一页之后，**id 不撞**这件事比以前更要紧。

   ## 关于"取消独立参数标定"（上游 2.4.0）
   用户说的"取消独立参数标定"指的是上游删掉的**课程调参整层**（`lesson_lab.c`）——
   那 14 项是**自走路径里的死参数**。而这里这五项（GYRO / 壁距 / 红外逐点 / 角度 / 里程）
   是**生产标定**（`mc.calib`，Flash page 115 那 24 项），上游**一行没删**，
   远程档下照常可跑。⇒ 这一块**保留**（它正是"把车调好"要做的最后一件事）。 */
C.parts = C.parts || {};
C.parts.calib = (function () {
  let el, obs = [];

  const ITEMS = [
    { id: 'gyro', name: 'GYRO', title: '陀螺标定', how: '车静止 1.5 s' },
    { id: 'wall_distance', name: 'WALLDIST', title: '壁距标定', how: '车平行贴左墙，车心到墙约 33 mm' },
    { id: 'ir_data', name: 'IRDATA', title: '红外逐点', how: '按 4 个摆位依次采集：左近/左远/前近/前远' },
    { id: 'angle', name: 'GYRORATIO + ENCANGLE', title: '角度标定', how: '在迷宫角格原地转一圈' },
    { id: 'track', name: 'TRACK', title: '里程标定', how: '车直行 20000 脉冲' },
  ];

  function mount(host) {
    el = host;
    const box = document.createElement('div');
    box.className = 'pane';
    box.style.marginTop = '8px';
    box.innerHTML = `
      <h4>标定例程 <span><button class="mini" id="mc-refresh">刷新场地</button></span></h4>
      <div id="mc-items"></div>
      <div class="hint" style="margin-top:6px">相机 / 场地几何</div>
      <div class="row"><label class="hint" style="flex:0 0 96px">相机高度 mm</label>
        <input id="mc-height" style="flex:1"></div>
      <div class="row"><label class="hint" style="flex:0 0 96px">天底点 nx,ny</label>
        <input id="mc-nadir" value="0.5, 0.5" style="flex:1"></div>
      <div class="row">
        <button class="mini" id="mc-addobs">记录观测</button>
        <input id="mc-fw" placeholder="固件格 x,y，留空自动读取" style="flex:1">
      </div>
      <div class="row"><button class="mini" id="mc-solve">解 flip</button>
        <span class="hint" id="mc-solve-state"></span></div>
      <div class="scroll" style="max-height:150px;margin-top:6px"><table id="mc-obs"></table></div>
      <button class="mini" id="mc-apply" style="margin-top:6px">落盘至 Control/config.yaml</button>
      <div class="hint" id="mc-arena-msg"></div>
      <div class="hint" style="margin-top:8px">标定报告 · mouse/calib/report.md</div>
      <textarea id="mc-report" style="width:100%;height:100px;background:#0e1015;color:var(--fg);
        border:1px solid var(--line);border-radius:4px;font-family:var(--mono);font-size:11px"></textarea>
      <div class="row"><button class="mini" id="mc-save-report">写入会话</button></div>`;
    el.appendChild(box);
    renderItems();
    box.querySelector('#mc-refresh').onclick = function () { loadArena(); };
    box.querySelector('#mc-addobs').onclick = function () { addObs(); };
    box.querySelector('#mc-solve').onclick = function () { solve(false); };
    box.querySelector('#mc-apply').onclick = function () { solve(true); };
    box.querySelector('#mc-save-report').onclick = async function () {
      const txt = el.querySelector('#mc-report').value.trim();
      if (!txt) return;
      const d = await C.guard(function () {
        return C.api('/api/v1/mouse/calib/report', { method: 'POST', body: { lines: txt } });
      }, '写报告');
      if (d) C.toast('报告已写入 ' + (d.path || 'mouse/calib/report.md'), 'ok');
    };
    renderObs();
    loadArena();
  }

  function renderItems() {
    el.querySelector('#mc-items').innerHTML = ITEMS.map(function (it) {
      return '<div style="border-bottom:1px solid var(--line);padding:6px 0">' +
        '<div><b>' + it.title + '</b> <span class="tag">' + it.name + '</span>' +
        '<button class="mini" data-run="' + it.id + '" style="float:right">跑</button></div>' +
        '<div class="hint">' + it.how + '</div></div>';
    }).join('');
    el.querySelectorAll('#mc-items [data-run]').forEach(function (b) {
      b.onclick = async function () {
        b.disabled = true;
        const d = await C.guard(function () {
          return C.api('/api/v1/mouse/calib/' + b.dataset.run, { method: 'POST', body: {} });
        }, '标定 ' + b.dataset.run);
        b.disabled = false;
        if (d) {
          el.querySelector('#mc-report').value += '\n## ' + b.dataset.run + '\n' +
            JSON.stringify(d.result, null, 1);
          C.toast(b.dataset.run + ' 完成：' + JSON.stringify(d.result).slice(0, 160), 'ok', 9000);
        }
        loadArena();
      };
    });
  }

  function renderObs() {
    const t = el.querySelector('#mc-obs');
    if (!t) return;
    if (!obs.length) { t.innerHTML = '<tr><td class="hint">无观测点，至少 2 个</td></tr>'; return; }
    t.innerHTML = '<thead><tr><th>#</th><th>固件格</th><th>视觉归一化</th></tr></thead><tbody>' +
      obs.map(function (o, i) {
        return '<tr><td>' + i + '</td><td>' + o.fw_xy.join(',') + '</td><td>' +
          o.vis_xy.map(function (v) { return Number(v).toFixed(4); }).join(', ') + '</td></tr>';
      }).join('') + '</tbody>';
  }

  async function addObs() {
    const fwv = el.querySelector('#mc-fw').value.trim();
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
    const h = el.querySelector('#mc-height').value.trim();
    const nd = el.querySelector('#mc-nadir').value.trim().split(/[,\s]+/).map(Number);
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
      '  rms=' + b.rms_cells + ' 格，margin=' + d.margin_cells;
    if (!d.confident) txt += '\n' + d.warning;
    if (d.applied) txt += '\n已落盘并注入定位器';
    else if (d.apply_refused) txt += '\n未落盘：' + d.apply_refused;
    el.querySelector('#mc-solve-state').textContent = txt;
    C.toast(txt, d.confident ? 'ok' : 'warn', 15000);
  }

  async function loadArena() {
    const d = await C.guard(function () { return C.api('/api/v1/arena'); });
    if (!d || !el) return;
    const a = d.arena || {};
    if (a.camera_height_mm && !el.querySelector('#mc-height').value) {
      el.querySelector('#mc-height').value = a.camera_height_mm;
    }
    if (a.nadir_xy) el.querySelector('#mc-nadir').value = a.nadir_xy.join(', ');
    el.querySelector('#mc-arena-msg').textContent =
      (a.calibrated ? '已标定' : '未标定') + '\nfw_to_arena = ' + JSON.stringify(a.fw_to_arena);
  }

  return {
    mount: mount,
    activate: function () { loadArena(); },
    onHealth: function () {
      if (!el) return;
      const why = C.needMouse();
      el.querySelectorAll('#mc-items [data-run]').forEach(function (b) { C.gate(b, why); });
      C.gate(el.querySelector('#mc-addobs'), why);
    },
    onMsg: function (m) {
      if (!el) return;
      if (m.type === 'arena.changed') loadArena();
      if (m.type === 'locate.result' && m.valid) {
        el.querySelector('#mc-fw').placeholder = '固件格 x,y ｜ 视觉 x=' +
          Number(m.x).toFixed(3) + ' y=' + Number(m.y).toFixed(3);
      }
    },
  };
})();
