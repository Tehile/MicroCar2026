/* ⑤ 实验 —— 电机实验（`mc.lab`）+ 台面算参（2.4.0 起走 `host.api.calc` 纯函数）。
 *
 * 这是「小车 · 测试发车」里的**台面实验**那一半：标定位、调参、算系数都在这里做，
 * 和 ④ 发车（场地上真的跑）是两套流程，所以分开两页。
 *
 * ## ⚠️ 上游 2.4.0 把"课程调参实验"整层删了
 * `lesson_lab.c`(751 行) + `host/api/lesson.py`(865 行) + 命令 `lesson.start/stop/status`
 * + `Event.LESSON` + 课程 14 项参数 + Flash page 116 —— **一条都不剩**。
 * ⇒ 本页那一栏"课程调参实验"整个不要了；只剩**电机实验**（`mc.lab`，生产档的台面动作）。
 *   后端这七条路由现在的行为：`list/status` 回 200 + `available:false`（**仍在**，好让老界面
 *   拿到一句"为什么不能用"），`run/stop/log` 回 **409 明确拒绝**，`suggest/apply` **照常可用**
 *   —— 因为算参那几个纯函数上游只是**搬了家**（`api/lesson.py` → `host/api/calc.py`），
 *   逻辑一字未改，仍是"拿一次实验的读数 + 真值 → 算生产参数"。
 *
 * ## 台面实验为什么必须"轮子悬空"
 * ⚠️ 护栏（时间 + 红外）全在固件 1 ms 中断里判定，上位机被 kill 也不会失控 ——
 * 但那是"不失控"，不是"不会撞墙"。
 *
 * ## 两套参数（2.4.0 起只剩一套写回通道）
 * 实验 2/5 算出来的是 **TRACKL/TRACKR/GYRORATIO（生产参数）**，写回走 `mc.params`
 * （Flash page 115）。课程那套（page 116）已经不存在了 —— 本页不再分流。
 */

C.views.exp = (function () {
  let el, exps = [], theory = null, labRes = null, lessonRes = null, sug = null;
  let lessonOk = null;        // 课程实验在这台上游上还在不在（探 /lesson/list 的 available）

  function mount(host) {
    el = host;
    el.innerHTML = `
      <h2>⑤ 实验</h2>

      <div class="pane">
        <h4>前置状态
          <span><button class="mini" id="x-refresh">刷新</button>
                <button class="mini danger" id="x-estop">急停</button>
                <button class="mini" id="x-resume">解锁急停</button></span></h4>
        <div id="x-status" class="kv"></div>
        <div class="gatehint" id="x-link-hint"></div>
        <div class="warnbox" id="x-guard"></div>
      </div>

      <div class="grid2" style="margin-top:10px">
        <div class="pane">
          <h4>电机实验 · mc.lab</h4>
          
          <div class="kv">
            <b>左轮速度</b><span><input id="x-sp-l" value="40" style="width:70px"> cm/s</span>
            <b>右轮速度</b><span><input id="x-sp-r" value="40" style="width:70px"> cm/s</span>
            <b>最长运行</b><span><input id="x-ms" value="800" style="width:70px"> ms 护栏</span>
            <b>红外阈值</b><span><input id="x-ir" value="-1" style="width:70px"> mm 负值关闭</span>
            <b>采样频率</b><span><input id="x-sample-hz" value="50" style="width:70px"> Hz
              （运动期间抄最新帧）</span>
          </div>
          <div class="row wrap" style="margin-top:6px">
            <button class="primary" id="x-lab-run">执行一次</button>
            <button id="x-lab-live">采 2 s 快照</button>
          </div>
          <div id="x-lab-progress" class="hint"></div>
          <div id="x-lab-out" class="hint" data-raw="1" style="white-space:pre-wrap;font-family:var(--mono);
            font-size:11px"></div>
        </div>

        <div class="pane" id="x-lesson-pane">
          <h4>课程调参实验
            <span><button class="mini" id="x-lesson-stop">中止</button></span></h4>
          <div id="x-lesson-gone" class="hint" style="display:none"></div>
          <div id="x-lesson-body">
            <div class="kv">
              <b>实验</b><span><select id="x-exp"></select></span>
            </div>
            <div id="x-exp-help" class="hint"></div>
            <div id="x-exp-args" class="kv"></div>
            <div class="row wrap" style="margin-top:6px">
              <button class="primary" id="x-lesson-run">执行实验</button>
            </div>
            <div id="x-lesson-progress" class="hint"></div>
            <div id="x-lesson-out" class="hint" data-raw="1" style="white-space:pre-wrap;font-family:var(--mono);
              font-size:11px"></div>
          </div>
        </div>
      </div>

      <div class="pane" id="x-sug-pane" style="margin-top:10px;display:none">
        <h4>自动算参（host.api.calc 纯函数）</h4>
        
        <div class="row wrap">
          <span>实测真值 <input id="x-truth" value="110" style="width:90px"></span>
          <button id="x-calc">计算</button>
          <label style="margin-left:8px"><input type="checkbox" id="x-save"> 同时写入 Flash</label>
          <button class="primary" id="x-apply" disabled>写回设备</button>
        </div>
        <div id="x-sug" class="hint" style="white-space:pre-wrap;font-family:var(--mono);
          font-size:11px"></div>
      </div>

      <div class="pane" style="margin-top:10px">
        <h4>理论系数</h4>
        <div class="hint" id="x-theory"></div>
      </div>`;

    el.querySelector('#x-refresh').onclick = function () { loadStatus(); };
    el.querySelector('#x-estop').onclick = doEstop;
    el.querySelector('#x-resume').onclick = doResume;
    el.querySelector('#x-lab-run').onclick = doLabRun;
    el.querySelector('#x-lab-live').onclick = doLabLive;
    el.querySelector('#x-exp').onchange = renderExpArgs;
    el.querySelector('#x-lesson-run').onclick = doLessonRun;
    el.querySelector('#x-lesson-stop').onclick = doLessonStop;
    el.querySelector('#x-calc').onclick = doSuggest;
    el.querySelector('#x-apply').onclick = doApply;
    loadList();
    loadStatus();
    return el;
  }

  /* 课程实验在**这台上游**上还在不在。2.4.0 起为 false，那一栏改成一句说明。
     为什么不在 mount 里写死 false：这一段代码也要能在 2.3.x 上游上跑（本层是整合层，
     老板子不一定跟着升）。后端的 `available` 是**能力探测**的结果，照它走。 */
  function applyLessonAvail(ok, note) {
    lessonOk = ok;
    const gone = el.querySelector('#x-lesson-gone');
    const body = el.querySelector('#x-lesson-body');
    if (!gone || !body) return;
    if (ok) { gone.style.display = 'none'; body.style.display = ''; return; }
    gone.style.display = '';
    body.style.display = 'none';
    gone.textContent = note ||
      '这台上游没有课程调参实验（2.4.0 已删除）。'
      + '台面动作只剩电机实验；算参在下方「自动算参」。';
  }

  /* ------------------------------------------------------------ 状态 / 清单 */
  async function loadList() {
    const d = await C.guard(function () { return C.api('/api/v1/lesson/list'); });
    if (!d) return;
    exps = d.exps || [];
    theory = d.theory || {};
    applyLessonAvail(d.available !== false, d.note);
    const sel = el.querySelector('#x-exp');
    sel.innerHTML = exps.map(function (e) {
      return '<option value="' + e.no + '">实验 ' + e.no + ' · ' + C.util.esc(e.name) + '</option>';
    }).join('') || '<option value="">无实验清单</option>';
    el.querySelector('#x-theory').innerHTML = Object.keys(theory).map(function (k) {
      const t = theory[k];
      return t ? (C.util.esc(t.label) + ' 理论值 <b>' + t.value + '</b>') : '';
    }).filter(Boolean).join('　') +
      '';

    const missing = Object.keys(d.features || {}).filter(function (k) {
      return !d.features[k];
    });
    if (missing.length) {
      el.querySelector('#x-guard').innerHTML = '上游缺少能力：' +
        C.util.esc(missing.join(', '));
    }
    renderExpArgs();
  }

  function renderExpArgs() {
    const no = Number(el.querySelector('#x-exp').value);
    const e = exps.filter(function (x) { return x.no === no; })[0];
    if (!e) {
      el.querySelector('#x-exp-args').innerHTML = '';
      el.querySelector('#x-exp-help').textContent = '';
      return;
    }
    el.querySelector('#x-exp-help').textContent = e.help || '';
    el.querySelector('#x-exp-args').innerHTML = e.args.map(function (a, i) {
      return '<b>' + C.util.esc(a) + '</b><span><input data-arg="' + C.util.esc(a) +
        '" value="' + (e.defaults[i] === undefined ? '' : e.defaults[i]) +
        '" style="width:80px"></span>';
    }).join('');
  }

  async function loadStatus() {
    // 未连鼠就不问了：这两条都是"问设备现在能不能动"，设备不在线上没有答案。
    if (C.needMouse('跑实验')) {
      el.querySelector('#x-status').innerHTML =
        '<b>前置状态</b><span>需连接电脑鼠</span>';
      return;
    }
    const lab = await C.guard(function () { return C.api('/api/v1/lab/status'); });
    /* 2.4.0 起课程层没了 ⇒ `lesson/status` 回 `available:false`。
       照旧问一次（拿到了才知道这台上游有没有），但**不**把它当成"能跑实验"的依据。 */
    const les = await C.guard(function () { return C.api('/api/v1/lesson/status'); });
    if (!lab && !les) return;
    if (les && les.available === false) applyLessonAvail(false, les.note);
    const L = (lab && lab.connected !== false) ? lab : {};
    const S = (les && les.available !== false) ? les : {};
    el.querySelector('#x-status').innerHTML = [
      ['已连接', L.connected === undefined ? (les && les.connected) : L.connected],
      ['电机被禁用', L.motor_disabled, L.motor_disabled
        ? '<span class="tag bad">安全固件，电机不输出</span>'
        : '<span class="tag ok">可动</span>'],
      ['急停锁存', L.estop_locked, L.estop_locked
        ? '<span class="tag bad">锁存中</span>' : '<span class="tag ok">无</span>'],
      ['1kHz 日志就绪', L.log_ready],
      ['红外最小', L.ir_min, 'mm'],
      ['能跑实验', S.can_run === undefined ? L.can_move : S.can_run],
    ].map(function (r) {
      const extra = r[2];
      const val = (extra && String(extra).indexOf('<') === 0) ? extra : C.util.fmt(r[1]);
      return '<b>' + r[0] + '</b><span>' + val +
        (typeof extra === 'string' && extra.indexOf('<') !== 0 ? ' <i class="dim">' + extra + '</i>' : '') +
        '</span>';
    }).join('');
  }

  /* ------------------------------------------------------------ 电机实验 */
  /* 进度行的措辞是「采集中」不是「回传中」：2.0.0 起数据在**运动期间**就地抄下来
     （`on_sample`），不再有"停车后回传"那一段 —— 说成"回传"会让人以为还在等设备。 */
  function prog(nodeId) {
    return function (m) {
      if (!m.frac && m.frac !== 0) return;
      el.querySelector('#' + nodeId).textContent =
        '采集中 ' + (m.frac * 100).toFixed(0) + '%' +
        (m.got != null ? ' ' + m.got + (m.total ? '/' + m.total : '') : '');
    };
  }

  async function doLabRun() {
    const body = {
      sp_l: Number(el.querySelector('#x-sp-l').value || 40),
      sp_r: Number(el.querySelector('#x-sp-r').value || 40),
      ms: Number(el.querySelector('#x-ms').value || 2000),
      ir_mm: Number(el.querySelector('#x-ir').value || 0),
      sample_hz: Number(el.querySelector('#x-sample-hz').value || 50),
    };
    if (body.ms > 5000 && !confirm('运行时长超过 5 s，轮子已悬空？')) return;
    const b = el.querySelector('#x-lab-run');
    b.disabled = true; b.textContent = '执行中…';
    const d = await C.guard(function () {
      return C.api('/api/v1/lab/run', { method: 'POST', body: body });
    }, '电机实验');
    b.disabled = false; b.textContent = '执行一次';
    el.querySelector('#x-lab-progress').textContent = '';
    if (!d) return;
    labRes = d;
    el.querySelector('#x-lab-out').textContent = d.explain || JSON.stringify(d, null, 1);
    if (d.reason && d.reason !== 'TIME') {
      C.toast('停止原因：' + d.reason_text + ' / ' + d.reason, 'bad', 20000);
    }
  }

  /* 不跑运动，只**按 50 Hz 采 2 s 最新快照** —— 旧「回传板载 1 kHz 环」的替代。
     2.0.0 没有板载环了（`SCOPE`/`DUMP` 已删）：统一帧本身就是全量快照，
     想留一段数据就当场抄下来。 */
  async function doLabLive() {
    const d = await C.guard(function () {
      return C.api('/api/v1/lab/log', { method: 'POST', body: {} });
    }, '采快照');
    if (!d) return;
    el.querySelector('#x-lab-out').textContent =
      '采下 ' + d.points + ' 点（2 s × 50 Hz，取自统一帧）\n' + (d.note || '');
  }

  /* ------------------------------------------------------------ 课程实验
     ⚠️ 2.4.0 起上游把这层删了 ⇒ 后端 `run/stop` 回 **409**。这不是"坏掉了"，
     而是"这台设备没这个功能"；`C.guard` 会把后端那句话说进 toast，够了 —— 但**先**判一次
     `lessonOk`，别在白屏的按钮上白等一次往返（按钮本来就已经藏起来了，这里只是兜底）。 */
  async function doLessonRun() {
    if (lessonOk === false) { C.toast('这台上游没有课程调参实验（2.4.0 已删除）', 'warn'); return; }
    const no = Number(el.querySelector('#x-exp').value);
    const args = {};
    el.querySelectorAll('[data-arg]').forEach(function (i) {
      args[i.dataset.arg] = Number(i.value);
    });
    const b = el.querySelector('#x-lesson-run');
    b.disabled = true; b.textContent = '执行中…';
    const d = await C.guard(function () {
      return C.api('/api/v1/lesson/run', { method: 'POST', body: { exp: no, args: args } });
    }, '课程实验');
    b.disabled = false; b.textContent = '执行实验';
    el.querySelector('#x-lesson-progress').textContent = '';
    if (!d) return;
    lessonRes = d;
    el.querySelector('#x-lesson-out').textContent = d.explain || JSON.stringify(d, null, 1);
    /* 算参那一块：只要拿到一次实验读数就能算（后端走 `host.api.calc` 纯函数，
       与课程层在不在无关）⇒ 不再按 `no===2||no===5` 卡死（那一对是**课程**实验号，
       2.4.0 之后不再出现）。有一次结果就把那一块露出来，让人自己填真值算。 */
    el.querySelector('#x-sug-pane').style.display = '';
    el.querySelector('#x-apply').disabled = true;
    sug = null;
  }

  async function doLessonStop() {
    if (lessonOk === false) { C.toast('这台上游没有课程调参实验（2.4.0 已删除）', 'warn'); return; }
    const d = await C.guard(function () {
      return C.api('/api/v1/lesson/stop', { method: 'POST', body: {} });
    }, '中止实验');
    if (d) C.toast('已中止：' + d.status, 'ok');
  }

  /* ⚠️ 课程实验**没有**"事后回传日志"这条路了（2.0.0 删掉了 `SCOPE`/`DUMP`）：
     曲线在【执行实验】期间就地捕获并随结果返回（判据：`lesson.run` 的 `samples`）。
     所以这一页不再有那个按钮 —— 留着它只会让人点一下、然后收到一条错误。 */

  async function doSuggest() {
    if (!lessonRes) { C.toast('请先执行一次实验', 'bad'); return; }
    const body = {
      exp: lessonRes.exp,
      truth: Number(el.querySelector('#x-truth').value),
      result: lessonRes,
    };
    const d = await C.guard(function () {
      return C.api('/api/v1/lesson/suggest', { method: 'POST', body: body });
    }, '算参');
    if (!d) return;
    sug = d;
    const lines = Object.keys(d.items || {}).map(function (k) {
      const it = d.items[k];
      return k + '：' + it.old + ' → ' + it.new +
        ' 读数 ' + (it.reading_mm !== undefined ? it.reading_mm + ' mm' : it.reading_deg + '°') +
        '，真值 ' + (it.truth_mm !== undefined ? it.truth_mm + ' mm' : it.truth_deg + '°') +
        '，误差 ' + it.err_pct + '% ｜ 写回 ' + it.write_via;
    });
    if (d.accept_note) lines.push('', d.accept_note);
    if (d.hint) lines.push('', d.hint);
    if (d.error) lines.push('', '错误：' + d.error);
    el.querySelector('#x-sug').textContent = lines.join('\n');
    el.querySelector('#x-apply').disabled = !d.ok;
    if (d.ok && d.accept === false) {
      C.toast('误差超验收线', 'bad', 20000);
    }
  }

  async function doApply() {
    if (!sug) return;
    const save = el.querySelector('#x-save').checked;
    if (!confirm('将建议值写入设备？\n' + JSON.stringify(sug.items, null, 1) +
                 (save ? '\n\n并写入 Flash 生产页 99' : ''))) return;
    const d = await C.guard(function () {
      return C.api('/api/v1/lesson/apply', { method: 'POST', body: { suggestion: sug, save: save } });
    }, '写回');
    if (d) {
      C.toast('已写回：' + JSON.stringify(d.applied), 'ok');
      el.querySelector('#x-sug').textContent += '\n\n已写回：' + JSON.stringify(d);
    }
  }

  /* ------------------------------------------------------------ 急停 */
  async function doEstop() {
    const d = await C.guard(function () {
      return C.api('/api/v1/lab/estop', { method: 'POST', body: {} });
    }, '急停');
    if (d) C.toast('已急停锁存：' + d.status, 'bad', 20000);
    loadStatus();
  }

  async function doResume() {
    const d = await C.guard(function () {
      return C.api('/api/v1/lab/resume', { method: 'POST', body: {} });
    }, '解锁急停');
    if (d) C.toast('已解锁：' + d.status, 'ok');
    loadStatus();
  }

  return {
    mount: mount,
    activate: function () { loadStatus(); if (!exps.length) loadList(); },
    onMsg: function (m) {
      if (m.type === 'lab.progress') {
        // 两个实验的进度都走这条；按 what 前缀分派
        const node = String(m.what || '').indexOf('lesson') === 0
          ? 'x-lesson-progress' : 'x-lab-progress';
        if (m.frac != null) {
          el.querySelector('#' + node).textContent =
            '采集中 ' + (m.frac * 100).toFixed(0) + '%';
        }
      } else if (m.type === 'lesson.plan') {
        el.querySelector('#x-lesson-progress').textContent =
          '实验已启动，固件侧预算 ' + (m.budget_ms / 1000).toFixed(1) + ' s';
      } else if (m.type === 'mouse.estop') {
        loadStatus();
      }
    },
    onHealth: function (h) {
      const mo = (h || {}).mouse || {};
      if (mo.estop_locked) {
        el.querySelector('#x-guard').innerHTML =
          '<b class="tag bad">急停锁存中</b> 运动命令返回 ERR_ESTOP';
      }
      /* 电机实验与课程实验都会**真驱动电机** ⇒ 没连上鼠就没意义，按钮先关门。
         课程那两个按钮在 2.4.0 上游上已经藏起来了（`#x-lesson-body` 整块 `display:none`），
         门控照旧走一遍无害。
         不门控的：#x-calc（纯计算，不碰设备）、#x-estop/#x-resume（越连不上越想按）、
         #x-refresh（读了才知道状态）、#x-apply（它自己还有"先算一算"的前提，
         这里不插手它的启用逻辑；真要写回时后端那道 motor_guard 也拦得住）。 */
      const why = C.needMouse('跑实验');
      ['x-lab-run', 'x-lab-live', 'x-lesson-run', 'x-lesson-stop'].forEach(function (id) {
        C.gate(el.querySelector('#' + id), why);
      });
      const hint = el.querySelector('#x-link-hint');
      if (hint) hint.textContent = why || '';
    },
    context: function (box) {
      box.innerHTML = `
        <div class="card">
          <h3>参数与通道</h3>
          <div class="kv">
            <b>生产标定</b><span>TRACKL/TRACKR/SPEEDL/SPEEDR/GYRORATIO/ENCANGLE/WALLDIST<br>
              Flash page <b>115</b> · mc.params（24 项，写页按 SAVE 按钮上的数字为准）</span>
            <b>课程调参</b><span>RemoteControl <b>2.4.0 已整层删除</b><br>
              lesson_lab.c / lesson.start·stop·status / 14 项 / page 116 都不在了</span>
          </div>
          <div class="hint">页号是**固件分区**给的，随上游调整会变（1.5.4 就把生产页从
            99 挪到了 115）。以「实时状态与参数设定」页上 SAVE 按钮里的数字为准。</div>
        </div>`;
    }
  };
})();
