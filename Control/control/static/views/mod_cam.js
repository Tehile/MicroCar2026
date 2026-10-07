/* 相机 · 图像回传测试 —— 实时灰度 / 彩色画面，用于把摄像头调到合适角度。

   两条道（**不能混用**，上游明令）：
     · 灰度 = 监视道 `GET /stream/mjpeg`：480 宽灰度 JPEG，默认 1 Hz 左右，可丢。
       它本来就一直在推（手机 App 连着就有），所以【灰度】= 停止一切采集、只看这条。
     · 彩色 = 实时道（PhoneCamera 1.15.0）：会话级 `capture_mode=live`，
       手机侧"拍一帧 → 阻塞 POST → 再拍下一帧"，PC 收到后**不落盘**、汇入同一条 MJPEG。
       ⇒ 开彩色要发一次 `camera/start{capture_mode:'live'}`（顶层参数，不是 plan 里的）。

   这条画面**只进 <img>，不参与任何数值路径**（看得见 ≠ 算得准）。 */
C.mods.mod_cam = (function () {
  let el, mode = '';

  function mount(host) {
    el = host;
    el.innerHTML = `
      <div class="pane">
        <h4>图像回传测试 <span>
          <button class="mini" id="mcam-gray">灰度</button>
          <button class="mini" id="mcam-color">彩色</button>
          <button class="mini" id="mcam-stop">停止</button>
        </span></h4>
        <div class="gatehint" id="mcam-hint"></div>
        <div style="margin-top:6px;background:#000;border:1px solid var(--line);border-radius:4px;
          min-height:180px;display:flex;align-items:center;justify-content:center">
          <img id="mcam-img" alt="相机画面" style="max-width:100%;max-height:60vh;display:none">
          <span class="hint" id="mcam-ph">未开始</span>
        </div>
        <div class="kv" id="mcam-kv" style="margin-top:6px"></div>
      </div>`;
    el.querySelector('#mcam-gray').onclick = function () { start('gray'); };
    el.querySelector('#mcam-color').onclick = function () { start('color'); };
    el.querySelector('#mcam-stop').onclick = function () { start(''); };
    /* `<img>` 拿到 502（反代如实报"连不上上游"）时浏览器只会显示破图、**一个字都不说**。
       所以这里必须接住 `onerror`，把它变成"条件 + 下一步" —— 否则现场看到的就是
       "什么都没出来"，而真正的原因是上游没起（或者上游起了但手机没在推帧）。
       反过来说：`onload` 到了就说明**画面真的来了**（浏览器按声明的 boundary
       切出了一帧才会触发 load），把提示清掉即可。 */
    const img = el.querySelector('#mcam-img');
    img.addEventListener('error', function () {
      const ph = el.querySelector('#mcam-ph');
      if (!ph) return;
      img.style.display = 'none';
      ph.style.display = '';
      ph.textContent = '画面加载失败 · 到顶栏【相机】看服务是否可达';
    });
    img.addEventListener('load', function () {
      const ph = el.querySelector('#mcam-ph');
      if (ph) ph.style.display = 'none';
    });
    renderKv();
  }

  async function start(m) {
    const img = el.querySelector('#mcam-img');
    const ph = el.querySelector('#mcam-ph');
    if (!m) {
      // 停止：把 <img> 摘掉（关掉 MJPEG 连接），有采集就一并停。
      img.removeAttribute('src'); img.style.display = 'none';
      if (ph) { ph.style.display = ''; ph.textContent = '已停止'; }
      if (mode) {
        await C.guard(function () {
          return C.api('/api/v1/camera/stop', { method: 'POST', body: {} });
        }, '停采集');
      }
      mode = ''; renderKv();
      return;
    }
    const why = C.needCam();
    if (why) { C.toast(why, 'warn', 9000); return; }
    if (m === 'color') {
      // 彩色：开 live 会话（只看不留）。
      // `capture_mode` 是**顶层参数**（上游 `HostLink.start()` 的 kwarg），不从 plan 读。
      const d = await C.guard(function () {
        return C.api('/api/v1/camera/start', { method: 'POST',
          body: { capture_mode: 'live' } });
      }, '开彩色实时道');
      if (!d) return;
    } else {
      // 灰度：先停掉 live 会话，回到监视道。
      if (mode === 'color') {
        await C.guard(function () {
          return C.api('/api/v1/camera/stop', { method: 'POST', body: {} });
        }, '停实时道');
      }
    }
    mode = m;
    // 先写一句"等第一帧"：上游那条约 1 Hz（实时档更快），中间这段空白必须说明是**在等**，
    // 而不是"没有画面"（两者在屏幕上长得一样）。第一帧到了由 `load` 事件清掉。
    if (ph) { ph.textContent = '等待第一帧…'; ph.style.display = ''; }
    img.style.display = '';
    // 换 src 强制浏览器重连这条 multipart 流（否则切模式看不到变化）。
    img.src = '/api/v1/camera/mjpeg?t=' + Date.now();
    renderKv();
  }

  function renderKv() {
    const box = el && el.querySelector('#mcam-kv');
    if (!box) return;
    const c = ((C.state.health || {}).camera) || {};
    const ph = c.phone || {};
    const modeTxt = mode === 'color' ? '彩色 · capture_mode=live'
      : mode === 'gray' ? '灰度 · 监视道' : '—';
    /* 「监视道」这一行是这一页**最要紧**的一格：它区分"上游根本没推画面"
       与"推了但这边没画出来"。没有它，现场在这页只能看到一片空白 + 无线索
       （用户报过这个现象）—— 而上游 `monitor.last_thumb` 是现成的铁证。 */
    const mon = C.monLine(c);
    box.innerHTML =
      '<b>模式</b><span>' + C.util.esc(modeTxt) + '</span>' +
      '<b>监视道</b><span>' + C.util.esc(mon.text) + '</span>' +
      '<b>服务版本</b><span>' + C.util.esc(c.server_version || '—') + '</span>' +
      '<b>手机在线</b><span>' + C.util.esc(
        ph.clients === undefined || ph.clients === null ? '判不了' : (ph.clients + ' 个')) + '</span>';
  }

  return {
    mount: mount,
    activate: function () { renderKv(); },
    onHealth: function () {
      const why = C.needCam();
      C.gate(el.querySelector('#mcam-gray'), why);
      C.gate(el.querySelector('#mcam-color'), why);
      const hint = el.querySelector('#mcam-hint');
      if (hint) hint.textContent = why || '';
      renderKv();
      /* 已经在等画面、而画面还没来 —— 那就把"为什么"写在原位，而不是让它一直写着
         「等待第一帧…」（那句话在"上游根本没推"和"马上就来"两种情况下长得一样）。
         判据取**结构事实**（`naturalWidth`）：`<img>` 解出过帧就再也不改这句提示。 */
      const img = el.querySelector('#mcam-img');
      const ph = el.querySelector('#mcam-ph');
      if (!img || !ph || !mode) return;
      if (img.naturalWidth > 0) return;                  // 有画面了：提示由 load 事件清掉
      const c = ((C.state.health || {}).camera) || {};
      const mon = C.monLine(c);
      if (!mon.pushed && mon.subs) {
        ph.textContent = '上游未推过画面 · 手机端需开启预览';
        ph.style.display = '';
      } else if (!mon.subs && C.camServeUp()) {
        ph.textContent = '等待上游画面…';
        ph.style.display = '';
      }
    },
  };
})();
