/* ═══ 实时状态：帧率（批式 n）+ 4 灯 + 统一帧通道 + 手动运动 ═══════════ */
(function () {
  const S = C.S, U = C.util;
  const CH = C.channels;

  C.VIEWS.live = {
    eyebrow: '小车控制 · 统一帧 v9',
    title: '实时状态',
    sub: '',

    actions() {
      const cls = S.running ? 'run' : (S.estop ? 'bad' : (S.health.mouse.connected ? 'ok' : 'idle'));
      const txt = S.running ? '运行 ' + (CH.tasks[S.tel.mazeTask] || S.tel.mazeTask)
        : S.estop ? '急停锁存'
        : S.health.mouse.connected ? '待机' : '未连接';
      return '<span class="pill ' + cls + '"><i></i>' + txt + '</span>' +
        '<button class="btn btn-sm" data-act="tare">' + C.icon('refresh') + 'TARE</button>';
    },

    mount(root, actions) {
      this._root = root;
      root.innerHTML =
        '<div id="lv-banners"></div>' +

        '<div class="lv-top">' +
          '<section class="card">' +
            '<div class="card-head"><h2>实时帧率</h2>' +
              '<div class="card-head-r"><span class="tag blue">批式</span>' +
                '<span class="pill mono sm" id="lv-pull-state">拉取中</span>' +
                '<span class="hint mono" id="lv-age"></span></div></div>' +
            '<div class="card-body">' +
              '<div class="rate-row">' +
                '<div class="rate-big"><b id="lv-rate">—</b><span>Hz</span></div>' +
                '<div class="rate-delta" id="lv-delta">—</div>' +
                '<div class="rate-spark"><canvas id="lv-spark"></canvas></div>' +
              '</div>' +
              /* 大数是**最近 2 s 实测**（改版四十一），这一行给另外两个口径：
                 6 s 趋势（看"是不是真在降"）与最近一批（看"这一趟多快"）。
                 三个数放一处必须标明各自是什么 —— 否则现场一定读错层级。 */
              '<div class="hint mono" id="lv-rate-note" style="margin:-4px 0 6px">—</div>' +
              '<div class="pull-row">' +
                '<span class="pu-k">拉取</span>' +
                '<label class="pu-n">N <input class="input sm" id="lv-n" value="1" inputmode="numeric"></label>' +
                '<button class="btn btn-sm btn-primary" id="lv-pull-on">' + C.icon('play') + '开始拉取</button>' +
                '<button class="btn btn-sm" id="lv-pull-off">' + C.icon('stop') + '结束拉取</button>' +
                '<span class="hint mono pu-note" id="lv-n-note"></span>' +
              '</div>' +
            '</div>' +
          '</section>' +

          /* 右侧：4 灯 + 链路层。链路这几项是 `mouse.telemetry` 的**顶层字段**
             （bps / streaming），不是帧内通道 —— 以前一个都没用，字节率还是
             前端拿 `rate × 30` 自己乘出来的。 */
          '<section class="card">' +
            '<div class="card-head"><h2>状态行 · 4 灯</h2>' +
              '<div class="card-head-r"><span class="pill mono sm" id="lv-ir">—</span></div></div>' +
            '<div class="card-body">' +
              '<div class="led-strip" id="lv-leds"></div>' +
              '<div class="led-note" id="lv-led-note"></div>' +
              '<div class="sec-title" style="margin:9px 0 4px">链路</div>' +
              '<dl class="kv compact pair2" id="lv-link"></dl>' +
              /* 解码器现算的派生键里，`dirName`/`mazeTaskName`/`roadName`/`freemode`
                 与下面通道卡的 `dir`/`mazeTask`/`goOrBack`/`flags` **是同一份事实**
                 （一条真相原则：两处显示就会两处不一样）。这张小字只报"还有 N 个
                 前端不认识的键"，既不让信息丢掉，也不摆一份重复的读数。 */
              '<div class="hint mono" id="lv-unk" style="margin-top:6px"></div>' +
            '</div>' +
          '</section>' +
        '</div>' +

        /* 通道数**不写死**（v10 从 33 掉到 32，写死的数字立刻就成谎）。
           真值只有一处：`/caps` 的 `unified_channels.length` → `S.health.mouse.caps`。
           判不了（还没探到能力）就显示 `—`，不拿一个记忆中的数顶上去。 */
        '<div class="sec-title">统一帧通道 <span class="mono" style="letter-spacing:0" id="lv-chan-n">· —</span>' +
          '<span class="hint" style="margin-left:8px">4 灯在状态行走三态，不在这里重复</span></div>' +
        '<div class="grid g3" id="lv-ch"></div>' +

        '<section class="card" style="margin-top:12px">' +
          '<div class="card-head"><h2>手动运动</h2>' +
            '<div class="card-head-r"><span class="hint mono" id="lv-mm">未下发</span></div></div>' +
          '<div class="card-body">' +
            '<div class="grid g2" style="gap:20px;align-items:center">' +
              '<div class="dpad" id="lv-dpad">' +
                '<span></span>' +
                '<button data-dir="fwd" title="前进">' + C.icon('arrowUp') + '</button><span></span>' +
                '<button data-dir="left" title="左转">' + C.icon('rotateL') + '</button>' +
                '<div class="d-center" id="lv-dname">前进</div>' +
                '<button data-dir="right" title="右转">' + C.icon('rotateR') + '</button>' +
                '<span></span>' +
                '<button data-dir="back" title="后退">' + C.icon('arrowDown') + '</button><span></span>' +
              '</div>' +
              '<div class="stack" style="gap:10px">' +
                '<div class="row wrap"><span class="hint" style="width:64px">速度 cm/s</span>' +
                  '<input class="input sm" id="lv-speed" value="40" style="width:82px">' +
                  '<span class="hint" style="width:44px">持续 s</span>' +
                  '<input class="input sm" id="lv-sec" value="1.0" style="width:70px"></div>' +
                '<div class="row wrap">' +
                  '<button class="btn btn-primary" id="lv-go">' + C.icon('play') + '执行</button>' +
                  '<button class="btn" id="lv-stop">停止</button>' +
                  '<button class="btn btn-danger" id="lv-estop">急停</button></div>' +
              '</div>' +
            '</div>' +
          '</div>' +
        '</section>';

      /* 4 项起就双列：单列的组会把整行撑到 4 行高，而 1600 宽下每列还有 ~200px，
         放得下"中文标签 + 读数（带单位）"。窄屏另用 `#lv-ch .kv > div code{display:none}` 兜住。 */
      root.querySelector('#lv-ch').innerHTML = CH.groups.map(g =>
        '<section class="card"><div class="card-head"><h2>' + g.name + '</h2>' +
        '<div class="card-head-r"><span class="hint mono">' + g.keys.length + '</span></div></div>' +
        '<div class="card-body" style="padding-top:4px"><dl class="kv compact'
        + (g.keys.length >= 4 ? ' pair2' : '') + '">' + g.keys.map(k =>
          '<div><dt>' + k[1] + ' <code>' + k[0] + '</code></dt>' +
          '<dd data-k="' + k[0] + '">—</dd></div>').join('') + '</dl></div></section>').join('');

      const tare = actions && actions.querySelector('[data-act="tare"]');
      if (tare) tare.onclick = () => C.act('tare');

      /* 切 N 的正确姿势：**结束拉取 → 改 N → 开始拉取**。
         开流前上游会 `configure(1, t_ms)`，档没在位就开流 ⇒ 设备按 n=1 发、
         界面写着 n=3，现场看不出来。所以「开始」里先落档再开流。 */
      const nin = root.querySelector('#lv-n');
      const onBtn = root.querySelector('#lv-pull-on');
      const offBtn = root.querySelector('#lv-pull-off');
      onBtn.onclick = () => {
        onBtn.disabled = true;
        C.setPull(true, nin.value).then(() => { this.paint(true); });
      };
      offBtn.onclick = () => {
        offBtn.disabled = true;
        C.setPull(false).then(() => { this.paint(true); });
      };
      nin.onkeydown = e => {
        if (e.key === 'Enter' && !S.pulling) { e.preventDefault(); onBtn.click(); }
      };

      /* 手动运动 */
      let dir = 'fwd';
      const dn = root.querySelector('#lv-dname');
      root.querySelectorAll('#lv-dpad [data-dir]').forEach(b => {
        b.onclick = function () {
          dir = b.dataset.dir;
          root.querySelectorAll('#lv-dpad [data-dir]').forEach(x => x.classList.toggle('on', x === b));
          dn.textContent = { fwd: '前进', back: '后退', left: '左转', right: '右转' }[dir];
        };
      });
      root.querySelector('#lv-go').onclick = () => {
        if (!S.health.mouse.connected) { C.toast('warn', '未连接电脑鼠', '顶栏【电脑鼠】'); return; }
        const v = Number(root.querySelector('#lv-speed').value) || 40;
        const ms = Math.round((Number(root.querySelector('#lv-sec').value) || 1) * 1000);
        const body = dir === 'fwd' ? { kind: 'run', sp_l: v, sp_r: v, ms: ms }
          : dir === 'back' ? { kind: 'run', sp_l: -v, sp_r: -v, ms: ms }
          : dir === 'left' ? { kind: 'rotate', pwm: v, ms: ms }
          : { kind: 'rotate', pwm: -v, ms: ms };
        root.querySelector('#lv-mm').textContent = 'MOVE ' + JSON.stringify(body);
        C.log('mouse', 'info', '→ MOVE ' + JSON.stringify(body));
        C.toast('', '运动指令已下发', dir + ' · ' + v + ' cm/s · ' + ms + ' ms');
      };
      root.querySelector('#lv-stop').onclick = () => {
        root.querySelector('#lv-mm').textContent = 'STOP';
        C.act('raw', 'STOP');
      };
      root.querySelector('#lv-estop').onclick = () => C.act('estop');

      this.paint(true);
    },

    paint(first) {
      const root = this._root;
      if (!root || !root.querySelector('#lv-ch')) return;
      const t = S.tel;
      const pulling = S.pulling;

      /* 通道数：能力探到才显示（v10 = 32；判不了给 `—`，不拿记忆里的数顶） */
      const cnEl = root.querySelector('#lv-chan-n');
      if (cnEl) {
        const n = (S.health.mouse.caps || {}).unified_channels;
        cnEl.textContent = '· ' + (n ? n : '—');
      }

      /* 帧率（改版四十一）：大数换成**最近 2 s 实测**（`t.hz2`）。
         原来那个数是 6 s 滑动平均 —— 突然掉一半时它要 3 s 才走到中间值，
         看起来像"慢慢降速"，而真相是这一秒就没有帧了。
         `t.rate`（6 s）不删：它降为脚注里的"趋势"，判据与历史读数都还读它。 */
      const rateEl = root.querySelector('#lv-rate');
      /* ⚠️ 改版四十三：**未连接就写 0**，不写 `—`。
         "没连设备"是**可判定的事实**（一帧/秒都不进），不是"判不了"；
         而 `—` 留给"连着、但这一项没读到"。同时 `resetTelemetry()` 已经把
         `hz2` 清成 null 且清空了 `S.hist.rate` —— 不清的话大数与曲线会**冻在
         最后一帧**上（实测："拔线后帧率还是 10.41，一动不动"）。 */
      const conn = !!S.health.mouse.connected;
      const shown = !conn ? 0 : ((t.hz2 != null) ? t.hz2 : t.rate);
      if (rateEl) {
        rateEl.textContent = (shown == null) ? '—' : U.fmt(shown, 1);
        rateEl.classList.toggle('off', shown == null || !conn);
      }
      const rnEl = root.querySelector('#lv-rate-note');
      if (rnEl) {
        if (!conn) {
          rnEl.textContent = '未连接 · 无帧（0 是事实，不是占位）';
          rnEl.classList.remove('bad');
        } else {
          const parts = [];
          /* 三态：`null` 写"—"（判不了），`0` 照实写 0（真的没帧）。 */
          parts.push('2 s 实测');
          parts.push('6 s 趋势 ' + (t.rate == null ? '—' : U.fmt(t.rate, 1)));
          parts.push('最近一批 ' + (t.hzInst == null ? '—' : U.fmt(t.hzInst, 1))
            + (t.hzInstAge == null ? '' : '（' + (t.hzInstAge / 1000 >= 1
              ? U.fmt(t.hzInstAge / 1000, 1) + ' s' : U.fmt(t.hzInstAge, 0) + ' ms') + ' 前）'));
          rnEl.textContent = parts.join(' · ');
          /* 数字冻住要说出来：批式两批之间不动是正常的，但"冻了十几秒"就是另一回事。 */
          rnEl.classList.toggle('bad', (t.hzInstAge != null && t.hzInstAge > 15000));
        }
      }
      const log = S.rateLog;
      const last = log[log.length - 1], prev = log[log.length - 2];
      const dEl = root.querySelector('#lv-delta');
      if (dEl) {
        if (!last || !prev) { dEl.className = 'rate-delta'; dEl.textContent = '—'; }
        else {
          const d = last.hz - prev.hz;
          dEl.className = 'rate-delta ' + (d > .05 ? 'up' : d < -.05 ? 'down' : 'flat');
          dEl.textContent = (d > 0 ? '▲ +' : d < 0 ? '▼ ' : '· ') + U.fmt(Math.abs(d), 1);
        }
      }
      const ageEl = root.querySelector('#lv-age');
      if (ageEl) ageEl.textContent = t.ageMs == null ? '最新一帧 —' : '最新一帧 ' + U.fmt(t.ageMs, 0) + ' ms 前';
      const stEl = root.querySelector('#lv-pull-state');
      if (stEl) {
        /* 三态：没连设备时"已结束"是编的（从没开始过）。 */
        const stTxt = !S.health.mouse.connected ? '判不了' : (pulling ? '拉取中' : '已结束');
        stEl.textContent = stTxt;
        stEl.className = 'pill mono sm ' + (!S.health.mouse.connected ? 'idle' : (pulling ? 'ok' : 'idle'));
      }
      const noteEl = root.querySelector('#lv-n-note');
      if (noteEl) {
        /* 字节率取后端的 `bps`，**不要**拿 `rate × 30` 自己乘：帧长是 30 B 没错，
           但那是链路常量，真实字节率还含帧间静默与批头，前端算出来的只是"理论值"。 */
        noteEl.textContent = pulling
          ? 'n=' + S.pullN + ' · 往返 ' + U.fmt(t.batchMs, 0) + ' ms'
            + (t.bps == null ? '' : ' · ' + U.fmt(t.bps, 0) + ' B/s')
          : '已结束 · 档 n=' + S.pullN + ' · 改 N 后按【开始拉取】';
      }
      /* 门控：拉取中锁住 N（切档必须先停），停了只放【开始拉取】 */
      const nin = root.querySelector('#lv-n');
      if (nin) {
        nin.disabled = pulling;
        if (document.activeElement !== nin) nin.value = S.pullN;
      }
      const onB = root.querySelector('#lv-pull-on');
      if (onB) onB.disabled = pulling;
      const offB = root.querySelector('#lv-pull-off');
      if (offB) offB.disabled = !pulling;

      const sp = root.querySelector('#lv-spark');
      if (sp && sp.clientWidth) {
        /* 未连接 ⇒ 画一条**贴底的水平线**（那是"0 Hz"的样子），不是留白。
           留白会被读成"还没画"；而事实是"这段确实一帧都没有"。 */
        C.draw.spark(sp, [{ data: conn ? S.hist.rate : [0, 0], color: '#2B4FF2' }], {});
      }

      /* 4 灯：只画后端判定的三态 */
      const leds = S.leds;
      root.querySelector('#lv-leds').innerHTML = ['led1', 'led2', 'led3', 'led4'].map((k, i) => {
        const v = leds[k];
        const cls = v === true ? 'on' : v === false ? 'off' : 'unknown';
        const txt = v === true ? '亮' : v === false ? '灭' : '未知';
        return '<div class="led-s ' + cls + '">' +
          '<span class="l-name">LED' + (i + 1) + '</span>' +
          '<span class="l-state">' + txt + '</span></div>';
      }).join('');

      /* IR 开关位（flags bit4）。`flags == null` ⇒ **判不了**，不许按 0 算成"关"——
         "红外没开"与"还没读到帧"是两件完全不同的事，后者去查红外是白查。 */
      const ir = root.querySelector('#lv-ir');
      if (ir) {
        if (t.flags == null) {
          ir.textContent = 'IR 判不了';
          ir.className = 'pill mono sm idle';
        } else {
          const irOn = !!(t.flags & 16);
          ir.textContent = 'IR ' + (irOn ? '开' : '关') + ' 0x10';
          ir.className = 'pill mono sm ' + (irOn ? 'ok' : 'idle');
        }
      }
      const note = root.querySelector('#lv-led-note');
      if (note) {
        const unknown = ['led1', 'led2', 'led3', 'led4'].some(k => leds[k] === null);
        note.innerHTML = unknown
          ? '<span class="sq"></span>' + (!S.health.mouse.connected ? '未连接 ⇒ 4 灯未知'
            : 'fw ' + S.health.mouse.fw + ' < 311 ⇒ 4 灯未知') : '';
        note.style.display = unknown ? '' : 'none';
      }

      /* 链路层：bps / streaming / 批次 seq / 序号缺口。
         ⚠️ `streaming` 是**设备事实**，`S.pulling` 是**人的操作**，两码事：
             点了"结束拉取"之后设备可能还在发（后端没来得及停），所以这里
             不许拿 pulling 顶替 streaming，也不许反着来。 */
      const link = root.querySelector('#lv-link');
      if (link) {
        const st = t.streaming;
        const stTxt = st === true ? '在流' : st === false ? '停' : '判不了';
        const stCls = st === true ? 'ok' : st === false ? 'dim' : 'warn';
        link.innerHTML =
          '<div><dt>字节率 <code>bps</code></dt><dd>' +
            (t.bps == null ? '—' : U.fmt(t.bps, 0) + ' B/s') + '</dd></div>' +
          '<div><dt>设备 <code>streaming</code></dt><dd class="' + stCls + '">' + stTxt + '</dd></div>' +
          '<div><dt>批次号 <code>seq</code></dt><dd>' + (t.seq == null ? '—' : t.seq) + '</dd></div>' +
          '<div><dt>序号缺口 <code>lost</code></dt><dd class="' +
            ((t.lost | 0) > 0 ? 'bad' : '') + '">' + (t.lost == null ? '—' : t.lost) + '</dd></div>';
      }

      /* 派生键：与已有通道同义的四个不重复列（见 HTML 处的说明），
         剩下的"前端不认识"的才报数 —— 报数而不是画一遍，避免两条真相。 */
      const unk = root.querySelector('#lv-unk');
      if (unk) {
        const KNOWN_DUP = { dirName: 1, mazeTaskName: 1, roadName: 1, freemode: 1 };
        const extra = t.extra || {};
        const rest = Object.keys(extra).filter(k => !KNOWN_DUP[k]);
        unk.textContent = rest.length
          ? '另有 ' + rest.length + ' 个未识别键：' + rest.slice(0, 4).join(', ')
            + (rest.length > 4 ? ' …' : '')
          : '';
      }

      root.querySelectorAll('[data-k]').forEach(d => {
        d.textContent = CH.text(d.dataset.k, t[d.dataset.k]);
      });

      /* 提示条：只说发生了什么。⚠️ "拉取已结束"只有在**连过设备**才说 ——
         从没连上时那是"判不了"，不是"结束了"（改版四十）。 */
      const arr = [];
      if (S.estop) arr.push(banner('bad', '急停锁存', '电机已断电'));
      if (!S.health.mouse.connected) arr.push(banner('warn', '未连接电脑鼠', '读数为最后一帧'));
      if (S.health.mouse.connected && !pulling) {
        arr.push(banner('', '拉取已结束', '没有新帧 · 改 N 后按【开始拉取】'));
      } else if (S.health.mouse.connected && !S.running && t.ageMs != null && t.ageMs > 1500) {
        arr.push(banner('', '无数据', '最新一帧 ' + U.fmt(t.ageMs / 1000, 1) + ' s 前'));
      }
      root.querySelector('#lv-banners').innerHTML = arr.join('');
    },

    onTick() { this.paint(); },
    onState() { this.paint(); },
  };

  function banner(kind, title, body) {
    return '<div class="banner ' + kind + '" style="margin-bottom:12px">' + C.icon('alert') +
      '<div class="b-body"><b>' + title + '</b> ' + body + '</div></div>';
  }
})();
