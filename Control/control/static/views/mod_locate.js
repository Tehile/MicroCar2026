/* 定位 · 定位测试 —— 给一帧图，看它到底识别到了什么。

   用途：验证"角标记（场地四角）"和"小车标记"能不能被正确认出来。
   灰度图就够（定位链路本来吃的就是交付道的 Y 平面），所以这里不需要彩色输入。

   结果里最要紧的三样：
     · `corner_ids` / `origin_id` —— 认到了哪几个角；少一个就说明取景或参数有问题。
     · `valid` + `reason` —— 无效帧**也留档**，且带原因（判据 I7）。
     · 叠加图 —— 定位器自己画出来的框，比任何数字都直观。 */
C.mods.mod_locate = (function () {
  let el, seq = 0, sid = '';

  function mount(host) {
    el = host;
    el.innerHTML = `
      <div class="pane">
        <h4>定位测试 <span>
          <button class="mini" id="ml-prev">上一帧</button>
          <button class="mini" id="ml-next">下一帧</button>
          <button class="mini primary" id="ml-go">解析</button>
        </span></h4>
        <div class="row">
          <span class="hint" style="flex:0 0 62px">session</span>
          <input id="ml-sid" placeholder="留空 = 当前 / 离线会话" style="flex:1">
        </div>
        <div class="row">
          <span class="hint" style="flex:0 0 62px">seq</span>
          <input id="ml-seq" value="0" style="width:90px">
          <span class="hint" id="ml-count"></span>
        </div>
        <div class="gatehint" id="ml-hint"></div>
      </div>
      <div class="pane" style="margin-top:8px">
        <h4>识别结果 <span class="hint" id="ml-verdict"></span></h4>
        <div class="kv" id="ml-kv"></div>
      </div>
      <div class="pane" style="margin-top:8px">
        <h4>叠加图</h4>
        <img id="ml-img" alt="叠加图" style="max-width:100%;display:none">
        <div class="hint" id="ml-img-ph">未解析</div>
      </div>`;
    el.querySelector('#ml-go').onclick = function () { run(); };
    el.querySelector('#ml-prev').onclick = function () { step(-1); };
    el.querySelector('#ml-next').onclick = function () { step(1); };
    loadCount();
  }

  function step(d) {
    seq = Math.max(0, seq + d);
    const inp = el.querySelector('#ml-seq');
    if (inp) inp.value = String(seq);
    run();
  }

  async function loadCount() {
    const d = await C.guard(function () { return C.api('/api/v1/locate/results?limit=1'); });
    if (d && d.total != null) el.querySelector('#ml-count').textContent = d.total + ' 条结果';
    else el.querySelector('#ml-count').textContent = '';
  }

  async function run() {
    sid = (el.querySelector('#ml-sid').value || '').trim();
    seq = Number(el.querySelector('#ml-seq').value) || 0;
    const q = (sid ? ('session_id=' + encodeURIComponent(sid) + '&') : '') + 'seq=' + seq;
    const d = await C.guard(function () {
      return C.api('/api/v1/locate/preview?' + q);
    }, '定位第 ' + seq + ' 帧');
    if (!d) return;
    render(d);
    const img = el.querySelector('#ml-img');
    const ph = el.querySelector('#ml-img-ph');
    if (d._has_annotated) {
      img.src = '/api/v1/locate/annotated/' + seq + '.jpg?t=' + Date.now();
      img.style.display = ''; if (ph) ph.style.display = 'none';
    } else {
      img.style.display = 'none';
      if (ph) { ph.style.display = ''; ph.textContent = d._annotate_error || '无叠加图'; }
    }
  }

  function render(d) {
    const vd = el.querySelector('#ml-verdict');
    if (vd) vd.textContent = d.valid ? '有效' : ('无效 · ' + (d.reason || '未知原因'));
    const kv = el.querySelector('#ml-kv');
    if (!kv) return;
    const corners = (d.corner_ids || []).join(', ') || '—';
    const miss = (d.missing_corner_ids || []).join(', ') || '—';
    kv.innerHTML = [
      ['x, y', d.x == null ? '—' : (C.util.fmt(d.x, 4) + ', ' + C.util.fmt(d.y, 4))],
      ['θ', d.theta_deg == null ? '—' : (C.util.fmt(d.theta_deg, 2) + ' °')],
      ['角标记', corners],
      ['缺角', miss],
      ['原点 id', d.origin_id == null ? '—' : d.origin_id],
      ['场地 rms', d.rms_field == null ? '—' : C.util.fmt(d.rms_field, 4)],
      ['小车标记 rms', d.target_rms == null ? '—' : C.util.fmt(d.target_rms, 4)],
      ['标记尺度', d.marker_scale == null ? '—' : C.util.fmt(d.marker_scale, 3)],
      ['flags', (d.flags || []).join(' ') || '—'],
      ['原因', d.reason || '—'],
    ].map(function (r) {
      return '<b>' + C.util.esc(r[0]) + '</b><span>' + C.util.esc(String(r[1])) + '</span>';
    }).join('');
  }

  return {
    mount: mount,
    activate: function () { loadCount(); },
    onMsg: function (m) {
      if (m.type === 'locate.result') loadCount();
    },
    onHealth: function () {
      // 定位不依赖设备连接：给帧就能算（离线会话也行），所以不门控。
      const l = ((C.state.health || {}).locator) || {};
      const hint = el.querySelector('#ml-hint');
      if (hint) hint.textContent = l.available === false ? '定位模块不可用' : '';
    },
  };
})();
