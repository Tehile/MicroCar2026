/* 「实时状态与参数设定」的一个**区块**：手动运动（前进 / 后退 / 左转 / 右转 / 自定义）。

   ⚠️ 2026-10-07（改版十二）起它**不再是一个独立模块**，而是 `mod_params` 那一页里的
   一个 pane。为什么并进去：上游 2.4.0 把编译档位压到两档之后，"把车调好"（看状态 →
   调参数 → 手动动一下 → 标定）成了**同一个现场阶段**里的连续动作，共用同一组门控
   （"连上电脑鼠"）；在导航上给它们各开一张卡，只是让这件事多两级。
   ⇒ 它注册成 `C.parts.motion`，由 `mod_params.mount()` 调 `mount(el)` 挂进自己的 pane。
   判据 B30 仍逐 id 检查这些控件在不在 —— **并进一页不等于不用守**。

   映射到上游（`mc.move`）：
     前进/后退 → `run(sp_l, sp_r, ms)`：两侧同号；**上游没有 rear**，后退用负速度。
     左转/右转 → `rotate(pwm, ms)`：`pwm` 实为旋转速度(cm/s)，开环定长。
   所以界面统一暴露「速度 + 持续秒数」，因为固件的运动本就是以 ms 计的定长动作。 */
C.parts = C.parts || {};
C.parts.motion = (function () {
  let el;
  const DIRS = [
    ['fwd', '前进'], ['back', '后退'], ['left', '左转'], ['right', '右转'],
    ['custom', '自定义'],
  ];

  function mount(host) {
    el = host;
    const box = document.createElement('div');
    box.className = 'pane';
    box.style.marginTop = '8px';
    box.innerHTML = `
      <h4>手动运动 <span><button class="mini" id="mm-run">执行</button></span></h4>
      <div class="row wrap" id="mm-dirs">
        ${DIRS.map(function (d, i) {
          return '<button class="mini' + (i === 0 ? ' on' : '') + '" data-dir="' + d[0] + '">' +
            d[1] + '</button>';
        }).join('')}
      </div>
      <div class="row">
        <span class="hint" style="flex:0 0 70px">速度 cm/s</span>
        <input id="mm-speed" value="40" style="width:80px">
        <span class="hint" style="flex:0 0 62px">持续 s</span>
        <input id="mm-sec" value="1.0" style="width:70px">
      </div>
      <div class="row" id="mm-custom" style="display:none">
        <span class="hint" style="flex:0 0 70px">左轮 cm/s</span>
        <input id="mm-sl" value="40" style="width:80px">
        <span class="hint" style="flex:0 0 62px">右轮</span>
        <input id="mm-sr" value="40" style="width:80px">
      </div>
      <div class="row wrap">
        <button class="mini primary" id="mm-go">执行</button>
        <button class="mini" id="mm-stop">停止</button>
        <button class="mini danger" id="mm-estop">急停</button>
      </div>
      <div class="hint" id="mm-msg"></div>`;
    el.appendChild(box);
    box.querySelectorAll('#mm-dirs button[data-dir]').forEach(function (b) {
      b.onclick = function () { pick(b.dataset.dir); };
    });
    box.querySelector('#mm-go').onclick = function () { go(); };
    box.querySelector('#mm-run').onclick = function () { go(); };
    box.querySelector('#mm-stop').onclick = function () { act('stop', '停止'); };
    box.querySelector('#mm-estop').onclick = function () { act('estop', '急停'); };
  }

  let dir = 'fwd';

  function pick(d) {
    dir = d;
    el.querySelectorAll('#mm-dirs button[data-dir]').forEach(function (b) {
      b.classList.toggle('on', b.dataset.dir === d);
    });
    const cu = d === 'custom';
    el.querySelector('#mm-custom').style.display = cu ? 'flex' : 'none';
  }

  function body() {
    const v = Number(el.querySelector('#mm-speed').value) || 40;
    const ms = Math.round((Number(el.querySelector('#mm-sec').value) || 1) * 1000);
    if (dir === 'fwd') return { kind: 'run', sp_l: v, sp_r: v, ms: ms };
    if (dir === 'back') return { kind: 'run', sp_l: -v, sp_r: -v, ms: ms };
    if (dir === 'left') return { kind: 'rotate', pwm: v, ms: ms };
    if (dir === 'right') return { kind: 'rotate', pwm: -v, ms: ms };
    return { kind: 'run',
             sp_l: Number(el.querySelector('#mm-sl').value) || 0,
             sp_r: Number(el.querySelector('#mm-sr').value) || 0, ms: ms };
  }

  async function go() {
    const b = body();
    msg('下发：' + JSON.stringify(b));
    const d = await C.guard(function () {
      return C.api('/api/v1/mouse/move', { method: 'POST', body: b });
    }, '运动指令');
    if (d) msg('结果：' + JSON.stringify(d.result));
  }

  async function act(what, label) {
    const d = await C.guard(function () {
      return C.api('/api/v1/mouse/' + what, { method: 'POST', body: {} });
    }, label);
    if (d) C.toast(label + '已下发', what === 'estop' ? 'warn' : 'ok');
  }

  function msg(t) { const m = el && el.querySelector('#mm-msg'); if (m) m.textContent = t; }

  return {
    mount: mount,
    onHealth: function () {
      if (!el) return;
      const why = C.needMouse();
      ['mm-go', 'mm-run', 'mm-stop'].forEach(function (id) {
        C.gate(el.querySelector('#' + id), why);
      });
      // 急停不门控：恰恰是连不上的时候最可能想按它（与顶栏一致）。
    },
  };
})();
