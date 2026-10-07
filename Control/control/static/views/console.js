/* ⑧ 调试台 —— 设备文本流 + 原始命令逃生舱 + 服务日志尾。 */

C.views.console = (function () {
  let el, device, service, hist = [], hi = -1;

  function mount(host) {
    el = host;
    el.innerHTML = `
      <h2>⑧ 调试台</h2>
      <div class="grid2">
        <div class="pane">
          <h4>设备文本流 · printf
            <span><button class="mini" id="k-clear">清空</button></span></h4>
          <div id="k-device" class="console" data-log="1" style="height:300px;border:1px solid var(--line);
            border-radius:4px;border-left:none;padding:6px"></div>
        </div>
        <div class="pane">
          <h4>原始命令</h4>
          <div class="row">
            <input id="k-cmd" placeholder="如 PING / MAP / CAL GYRO" style="flex:1">
            <button class="mini" id="k-send">发送</button>
          </div>
          <div class="gatehint" id="k-link-hint"></div>
          <div id="k-result" class="hint"></div>
          <div class="row wrap">
            ${['PING', 'TELEM', 'MAP', 'CAL GYRO', 'GET ALL',
               'MOVE RUN 40 40 800', 'STOP', 'TARE', 'ESTOP', 'SAVE']
              .map(function (c) { return '<button class="mini" data-c="' + c + '">' + c + '</button>'; }).join('')}
          </div>
          
        </div>
      </div>
      <div class="pane" style="margin-top:10px">
        <h4>Control 服务日志
          <span><button class="mini" id="k-log-refresh">刷新</button></span></h4>
        <div id="k-service" class="console" data-log="1" style="height:200px;border:1px solid var(--line);
          border-radius:4px;border-left:none;padding:6px"></div>
      </div>`;
    device = el.querySelector('#k-device');
    service = el.querySelector('#k-service');
    el.querySelector('#k-clear').onclick = function () { device.innerHTML = ''; };
    el.querySelector('#k-log-refresh').onclick = function () { loadLog(); };
    el.querySelector('#k-send').onclick = function () { send(el.querySelector('#k-cmd').value); };
    el.querySelector('#k-cmd').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { send(this.value); }
      else if (e.key === 'ArrowUp') { if (hi > 0) this.value = hist[--hi]; e.preventDefault(); }
      else if (e.key === 'ArrowDown') { if (hi < hist.length - 1) this.value = hist[++hi]; e.preventDefault(); }
    });
    el.querySelectorAll('[data-c]').forEach(function (b) {
      b.onclick = function () { send(b.dataset.c); };
    });
    loadLog();
    return el;
  }

  function line(node, kind, text) {
    const d = document.createElement('div');
    d.className = kind || '';
    d.textContent = text;
    node.appendChild(d);
    while (node.childNodes.length > 500) node.removeChild(node.firstChild);
    node.scrollTop = node.scrollHeight;
  }

  async function send(cmd) {
    cmd = (cmd || '').trim();
    if (!cmd) return;
    hist.push(cmd); hi = hist.length;
    line(device, 'tx', '→ ' + cmd);
    const d = await C.guard(function () {
      return C.api('/api/v1/mouse/raw', { method: 'POST', body: { line: cmd } });
    }, '原始命令');
    if (d) {
      line(device, d.ok ? 'rx' : '', '← ' + (d.ok ? 'OK' : 'FAIL') + ' ' + d.status +
        '  ' + JSON.stringify(d.data || {}));
    }
  }

  async function loadLog() {
    const d = await C.guard(function () {
      return C.api('/api/v1/logs/tail?n=300');
    });
    service.innerHTML = '';
    (d || []).forEach(function (m) {
      line(service, m.level === 'warn' ? '' : '', '[' + (m.level || 'info') + '] ' +
        C.util.esc(m.text || JSON.stringify(m)));
    });
    if (!(d || []).length) line(service, '', '（无日志）');
  }

  return {
    mount: mount,
    activate: function () { loadLog(); },
    /* 逃生舱虽然"什么都发得出去"，但没有链路时发出去也没人接。
       门控它只是省一次必错的等待；真要排查串口本身，顶栏【电脑鼠】窗口里的
       【连接】才是入口。 */
    onHealth: function () {
      const why = C.needMouse('发命令');
      ['k-send'].forEach(function (id) { C.gate(el.querySelector('#' + id), why); });
      el.querySelectorAll('[data-c]').forEach(function (b) { C.gate(b, why); });
      const hint = el.querySelector('#k-link-hint');
      if (hint) hint.textContent = why || '';
    },
    onMsg: function (m) {
      if (m.type === 'mouse.text') {
        line(device, m.direction === '<-' ? 'rx' : 'tx',
          (m.direction === '<-' ? '← ' : '→ ') + m.line);
      } else if (m.type === 'mouse.error') {
        line(device, '', '[error] ' + m.kind + ': ' + m.msg);
      } else if (m.type === 'log') {
        line(service, '', '[' + (m.level || 'info') + '] ' + m.text);
      }
    },
    context: function (box) {
      box.innerHTML = `
        <div class="card">
          <h3>常用命令</h3>
          <div class="kv">
            <b>PING</b><span>设备信息（含复位原因）</span>
            <b>TELEM</b><span>拉取一帧遥测（带时间锚）</span>
            <b>MAP</b><span>墙图</span>
            <b>MOVE RUN l r ms</b><span>左右轮 cm/s + 时长</span>
          </div>
          <div class="hint">遥测已改为按需拉取：平时由本机自动逐条发 TELEM，
            这里手发一条只是取一份当前快照。</div>
        </div>`;
    }
  };
})();
