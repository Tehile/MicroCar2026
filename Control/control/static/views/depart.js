/* ⑤ 发车 —— 时间同步 + 远程发车 + 链路完整性。
 *
 * 这一页是「小车 · 测试发车」那套系统的入口。它回答三个问题：
 *   ① **现在能不能发车**（链路活不活、急停是否锁存、固件是哪一档）
 *   ② **发车前时间对齐了没有**（上游 mc.timesync 的偏移/频差/基准灯/样本数）
 *   ③ **链路有没有偷偷丢帧**（seq_lost / seq_gap / bad_crc）
 *
 * 为什么这三件事必须放在一起：它们是**同一个动作**的前置条件。发车之前不看这三样，
 * 事后就只能拿到一份"时间对不上、还少了若干帧"的数据 —— 而那时已经无法补救了。
 *
 * ⚠️ 这一页的核心是上游 1.4.0 新增的 `mc.timesync`：固件从 ARM 起以 1Hz 闪烁
 * 基准灯并每秒上行一帧 TIME（带完整 u32 毫秒时基），每个快帧的保留字节也让出
 * 时基低 8 位。于是**每一个采样点**都能被钉到准确时刻，而不是只有"主机收到的那一刻"。
 */

C.views.depart = (function () {
  let el, last = null, armed = false, timer = null;

  function mount(host) {
    el = host;
    el.innerHTML = `
      <h2>⑤ 发车 · 时间同步</h2>

      <div class="grid2">
        <div class="pane">
          <h4>发车
            <span><button class="mini" id="d-refresh">刷新</button></span></h4>
          <div class="row wrap">
            <button class="primary" id="d-arm">发车（ARM）</button>
            <button id="d-maze">远程发车（跑迷宫）</button>
            <button class="danger" id="d-estop">急停</button>
            <button id="d-resume">解锁急停</button>
          </div>
          <div id="d-sync-hint" class="gatehint"></div>
                    <div id="d-arm-result" class="hint"></div>
        </div>

        <div class="pane">
          <h4>时间同步状态</h4>
          <div id="d-sync" class="kv"></div>
          <div id="d-explain" class="hint" data-raw="1" style="white-space:pre-wrap;font-family:var(--mono);
            font-size:11px;margin-top:6px"></div>
        </div>
      </div>

      <div class="grid2" style="margin-top:10px">
        <div class="pane">
          <h4>最新帧时基</h4>
          <div id="d-sample" class="kv"></div>
        </div>
        <div class="pane">
          <h4>链路完整性</h4>
          <div id="d-link" class="kv"></div>
          <div class="hint" id="d-link-note"></div>
        </div>
      </div>

      `;

    el.querySelector('#d-refresh').onclick = function () { load(true); };
    el.querySelector('#d-arm').onclick = function () { doArm(); };
    el.querySelector('#d-maze').onclick = function () { doMaze(); };
    el.querySelector('#d-estop').onclick = function () { doEstop(); };
    el.querySelector('#d-resume').onclick = function () { doResume(); };
    return el;
  }

  /* ------------------------------------------------------------ 读 */
  async function load(manual) {
    const d = await C.guard(function () { return C.api('/api/v1/timesync'); });
    if (!d) return;
    last = d;
    const t = d.timesync || {};
    armed = !!t.armed;

    const rows = [
      ['armed', t.armed === undefined ? null : !!t.armed,
        t.armed ? '<span class="tag ok">已发车</span>' : '<span class="tag warn">未发车</span>'],
      ['发车时刻', t.arm_up_ms, '设备时基 ms'],
      ['样本数', t.samples, 'TIME 帧数'],
      ['偏移', t.offset_s, 's min-delay'],
      ['频差', t.drift_ppm, 'ppm'],
      ['单程延迟', t.one_way_ms, 'ms'],
      ['最新时基', t.latest_up_ms, '设备 ms'],
      ['基准灯', t.beacon_on, t.beacon_on
        ? '<span class="tag ok">亮</span>' : '<span class="tag warn">灭</span>'],
      ['迷宫在跑', t.maze_run, t.maze_run ? '是' : '否'],
    ];
    el.querySelector('#d-sync').innerHTML = rows.map(function (r) {
      const v = (r[1] === null || r[1] === undefined) ? '—'
        : (typeof r[1] === 'boolean' ? (r[1] ? '是' : '否') : C.util.fmt(r[1]));
      return '<b>' + r[0] + '</b><span>' + (r[2] && String(r[2]).indexOf('<') === 0 ? r[2] : v) +
        (r[2] && String(r[2]).indexOf('<') !== 0 ? ' <i class="dim">' + r[2] + '</i>' : '') +
        '</span>';
    }).join('');
    el.querySelector('#d-explain').textContent = t.explain || '无同步信息';

    const s = d.sample;
    if (!s) {
      el.querySelector('#d-sample').innerHTML =
        '<b>—</b><span>无帧数据</span>';
    } else {
      const gap = (s.t_send_pc_ms != null && s.arrival_pc_ms != null)
        ? (s.arrival_pc_ms - s.t_send_pc_ms) : null;
      el.querySelector('#d-sample').innerHTML = [
        ['发车以来', s.dev_ms, 'ms 设备侧'],
        ['快帧低 8 位', s.ms8, 'ms8'],
        ['帧发生时刻', s.t_send_pc_ms, 't_pc_ms'],
        ['主机收到', s.arrival_pc_ms, 't_pc_ms'],
        ['链路耗时', gap, 'ms'],
      ].map(function (r) {
        return '<b>' + r[0] + '</b><span>' + C.util.fmt(r[1]) +
          ' <i class="dim">' + r[2] + '</i></span>';
      }).join('');
    }

    const lk = d.link || {};
    const lost = lk.seq_lost, gap_n = lk.seq_gap;
    el.querySelector('#d-link').innerHTML = [
      ['丢帧 seq_lost', lost, lost === 0 ? '<span class="tag ok">没有丢</span>'
        : '<span class="tag bad">丢了</span>'],
      ['缺口次数 seq_gap', gap_n, ''],
      ['坏 CRC', lk.bad_crc, ''],
      ['同步错 bad_sync', lk.bad_sync, ''],
      ['不可识别帧', lk.unknown_frame, ''],
      ['接收速率', lk.rx_bps, 'B/s'],
    ].map(function (r) {
      return '<b>' + r[0] + '</b><span>' + (r[2] && r[2].indexOf('<') === 0 ? r[2]
        : C.util.fmt(r[1]) + ' <i class="dim">' + (r[2] || '') + '</i>') + '</span>';
    }).join('');
    const counts = lk.counts || {};
    el.querySelector('#d-link-note').innerHTML =
      '各流帧数：' + Object.keys(counts).map(function (k) {
        return k + '=' + counts[k];
      }).join('  ') +
      (lk.seq_lost == null
        ? '<br><b class="tag warn">上游没给丢帧统计</b>'
        : '') +
      '<br>连接统计：' + C.util.esc(JSON.stringify(lk.cmd || {}));

    if (armed && !timer) { timer = setInterval(function () { load(false); }, 2000); }
    if (!armed && timer) { clearInterval(timer); timer = null; }
    if (manual && !armed) { C.toast('尚未发车', 'ok'); }
  }

  /* ------------------------------------------------------------ 写 */
  async function doArm() {
    const b = el.querySelector('#d-arm');
    b.disabled = true; b.textContent = '发车中…';
    const d = await C.guard(function () {
      return C.api('/api/v1/timesync/arm', { method: 'POST', body: {} });
    }, '发车');
    b.disabled = false; b.textContent = '发车（ARM）';
    if (d && d.ok) {
      C.toast('已发车：设备时基 ' + d.timesync.arm_up_ms + ' ms', 'ok');
      el.querySelector('#d-arm-result').innerHTML =
        '已发车 @ ' + d.timesync.arm_up_ms + ' ms';
      load(false);
    }
  }

  async function doMaze() {
    if (!armed && !confirm('尚未发车，仍要执行？')) {
      return;
    }
    const d = await C.guard(function () {
      return C.api('/api/v1/timesync/maze_run', { method: 'POST', body: {} });
    }, '远程发车');
    if (d) C.toast('远程发车已下达：' + d.status, 'ok');
  }

  async function doEstop() {
    const d = await C.guard(function () {
      return C.api('/api/v1/lab/estop', { method: 'POST', body: {} });
    }, '急停');
    if (d) C.toast('已急停锁存：' + d.status, 'bad', 20000);
    load(false);
  }

  async function doResume() {
    const d = await C.guard(function () {
      return C.api('/api/v1/lab/resume', { method: 'POST', body: {} });
    }, '解锁急停');
    if (d) C.toast('已解锁：' + d.status, 'ok');
    load(false);
  }

  function context(box) {
    const t = (last && last.timesync) || {};
    box.innerHTML = `
      <div class="card">
        <h3>当前</h3>
        <div class="kv">
          <b>状态</b><span>${t.armed ? '已发车' : '未发车'}</span>
          <b>样本</b><span>${C.util.fmt(t.samples)} 帧 TIME</span>
          <b>偏移</b><span>${C.util.fmt(t.offset_s, 3)} s</span>
          <b>频差</b><span>${C.util.fmt(t.drift_ppm, 2)} ppm</span>
        </div>
      </div>`;
  }

  return {
    mount: mount,
    activate: function () { load(false); },
    onMsg: function (m) {
      if (m.type === 'mouse.timesync') {
        C.toast('发车回执：' + JSON.stringify(m.data || {}), 'ok');
        load(false);
      } else if (m.type === 'mouse.estop') {
        C.toast('急停事件：' + JSON.stringify(m.data || {}), 'bad', 20000);
        load(false);
      }
    },
    onHealth: function (h) {
      // 灯的文案由 app.js 统一管；这里只跟着刷新同步状态
      const mo = h.mouse || {};
      /* 发车是**最典型的**"没连上就没意义"的动作：车都不在线上，ARM 了也没人听。
         所以这三个按钮先关门，并把"该做什么"写进 title。
         急停 (#d-estop) 和刷新**不门控** —— 连不上的时候恰恰最想按急停，
         刷新则是"读了才知道现在什么状态"。 */
      const why = C.needMouse('发车');
      ['d-arm', 'd-maze', 'd-resume'].forEach(function (id) {
        C.gate(el.querySelector('#' + id), why);
      });
      if (why) {
        el.querySelector('#d-sync-hint').textContent = why;
      } else {
        el.querySelector('#d-sync-hint').textContent = '';
      }
      if (mo.timesync && mo.timesync.armed !== armed) { load(false); }
    },
    context: context
  };
})();
