/* ═══ 维护自检：诊断 / 红外 8 值标定 / 清空路径 ════════════════════════ */
(function () {
  const S = C.S, U = C.util;
  const STEPS = [
    { no: 1, name: '左墙贴齐', desc: '车左侧贴墙 · 采标记 1', wall: 'l', y: 12 },
    { no: 2, name: '右墙贴齐', desc: '车右侧贴墙 · 采标记 2', wall: 'r', y: 12 },
    { no: 3, name: '前墙贴脸', desc: '车头距墙 20 mm', wall: 't', y: 18 },
    { no: 4, name: '前墙远端', desc: '车头距墙 100 mm', wall: 't', y: 26 },
  ];
  let step = 1, done = 0, armed = null, last = null;

  C.VIEWS.maint = {
    eyebrow: '小车控制 · RemoteControl 2.13.0 · PROTO 9',
    title: '维护自检',
    sub: '擦除类不可逆。',

    actions() {
      return '<span class="pill ok"><i></i>按键菜单可远程</span>' +
        '<span class="hint">运动中一律 ERR_BUSY</span>';
    },

    mount(root) {
      this._root = root;
      root.innerHTML =
        '<div id="mt-banner"></div>' +
        '<div class="grid g-1-2" style="align-items:start">' +
          '<div class="stack">' +
            '<section class="card">' +
              '<div class="card-head"><h2>诊断</h2>' +
                '<div class="card-head-r"><span class="hint mono" id="mt-msg"></span></div></div>' +
              '<div class="card-body">' +
                '<div class="seg v" id="mt-diagbar">' +
                  '<button data-diag="IRSELF" class="on">红外自检<em>未执行</em></button>' +
                  '<button data-diag="SENSOR">传感器明细<em>未执行</em></button>' +
                  '<button data-diag="BLOCK">Flash 概览<em>未执行</em></button>' +
                '</div>' +
                '<dl class="kv compact" id="mt-diag" style="margin-top:10px">' +
                  '<div><dt>结果</dt><dd class="dim">点上面一条执行</dd></div>' +
                '</dl>' +
                '<div class="hint" id="mt-diag-note" style="margin-top:8px"></div>' +
              '</div>' +
            '</section>' +

            '<section class="card danger-zone">' +
              '<div class="card-head"><h2>清空路径数据</h2></div>' +
              '<div class="card-body">' +
                '<div class="hint">擦除迷宫与路径 6 页（块 / 点 / 去径 / 回径 / 去径90 / 回径90）。' +
                  '历史录制区不受影响。</div>' +
                '<div class="row" style="margin-top:10px">' +
                  '<button class="btn btn-sm btn-danger" id="mt-erase-all">清空路径数据</button>' +
                  '<span class="hint" id="mt-erase-msg"></span>' +
                '</div>' +
              '</div>' +
            '</section>' +
          '</div>' +

          '<section class="card">' +
            '<div class="card-head"><h2>红外 8 值标定</h2>' +
              '<div class="card-head-r"><span class="hint mono" id="mt-ircap-msg"></span></div></div>' +
            '<div class="card-body">' +
              '<div class="hint">四个摆位依次采一遍（共 8 个值）。必须按顺序；' +
                '若这一页已有标定值，先【擦除红外标定】。</div>' +
              '<div class="step-grid" id="mt-stepper" style="margin-top:10px"></div>' +
              '<div class="banner" id="mt-step" style="margin-top:10px"></div>' +
              '<div class="row wrap" style="margin-top:10px">' +
                '<button class="btn btn-primary" id="mt-next">开始标定</button>' +
                '<button class="btn btn-sm btn-ghost" id="mt-restart">回到第 1 步</button>' +
                '<button class="btn btn-sm btn-danger" id="mt-erase-ir">擦除红外标定</button>' +
              '</div>' +
              '<dl class="kv compact" id="mt-ircap" style="margin-top:10px">' +
                '<div><dt>上一步回读</dt><dd class="dim">—</dd></div>' +
                '<div><dt>已完成到第</dt><dd class="dim">0 步</dd></div>' +
              '</dl>' +
            '</div>' +
          '</section>' +
        '</div>' +

        '<section class="card" style="margin-top:12px">' +
          '<div class="card-head"><h2>远程菜单条目</h2>' +
            '<div class="card-head-r"><span class="hint">CAL6 家族 · 立即 ACK</span></div></div>' +
          '<div class="card-body flush">' +
            '<table class="tbl"><thead><tr><th style="width:150px">条目</th><th style="width:200px">作用</th>' +
            '<th>后果 / 约束</th></tr></thead><tbody>' +
            [
              ['CAL6/IRSELF', '红外自检', '只读：回左右红外与车心到墙参考'],
              ['CAL6/SENSOR', '传感器明细', '打印到底栏（来源 = 电脑鼠），不落盘'],
              ['CAL6/BLOCK', 'Flash 概览', '不是纯只读：固件会把 Flash 墙图重载进内存'],
              ['CAL6/IRCAP', '红外 8 值标定', '4 个摆位依次采；第 1 步要求红外页为空'],
              ['CAL6/ERASEIR', '擦除红外标定', '不可逆：8 值立即作废'],
              ['CAL6/ERASEALL', '擦除路径 6 页', '不可逆：块 / 点 / 去径 / 回径 / 去径90 / 回径90'],
            ].map(r => '<tr><td class="num" style="color:var(--ink)">' + r[0] + '</td>' +
              '<td>' + r[1] + '</td><td class="dim" style="font-size:11px">' + r[2] + '</td></tr>').join('') +
            '</tbody></table>' +
          '</div>' +
        '</section>';

      root.querySelectorAll('[data-diag]').forEach(b => {
        b.onclick = () => this.diag(b.dataset.diag);
      });
      root.querySelector('#mt-next').onclick = () => this.next();
      root.querySelector('#mt-restart').onclick = () => {
        step = 1; done = 0; last = null;
        root.querySelector('#mt-ircap-msg').textContent = '已回到第 1 步';
        this.paint();
      };
      root.querySelector('#mt-erase-ir').onclick = e =>
        this.arm(e.currentTarget, '擦除红外标定', () => {
          root.querySelector('#mt-ircap-msg').textContent = '红外 8 值已作废';
          C.toast('warn', '擦除红外标定', '红外 8 值立即作废，需要重新标一次。');
          C.log('mouse', 'warn', '→ ERASEIR confirm=true');
          done = 0; step = 1; last = null; this.paint();
        });
      root.querySelector('#mt-erase-all').onclick = e =>
        this.arm(e.currentTarget, '清空路径数据', () => {
          root.querySelector('#mt-erase-msg').textContent = '迷宫与路径 6 页已擦除';
          C.toast('warn', '清空路径数据', '6 页已擦除，历史录制区不受影响。');
          C.log('mouse', 'warn', '→ ERASEALL confirm=true');
        });

      this.paint();
    },

    diag(item) {
      const root = this._root;
      const box = root.querySelector('#mt-diag');
      const note = root.querySelector('#mt-diag-note');
      const msg = root.querySelector('#mt-msg');
      if (!S.health.mouse.connected) { C.toast('warn', '未连接电脑鼠', '先到顶栏【电脑鼠】连接'); return; }
      const t = S.tel.mazeTask;
      if (t >= 2 && t <= 5) { C.toast('warn', '车在跑', '运动中固件对一切控制命令只回 ERR_BUSY。'); return; }
      root.querySelectorAll('[data-diag]').forEach(b => b.classList.toggle('on', b.dataset.diag === item));
      const doneTag = root.querySelector('[data-diag="' + item + '"] em');
      if (doneTag) doneTag.textContent = '完成';
      if (item === 'IRSELF') {
        box.innerHTML = kv([['irL', '左红外', '34 mm'], ['irR', '右红外', '38 mm'], ['ref', '车心到墙参考', '96 mm']]);
        note.textContent = '两侧差值 4 mm，在 ±6 mm 容差内。';
        msg.textContent = 'IRSELF 完成';
      } else if (item === 'SENSOR') {
        box.innerHTML = kv([['printed', '输出位置', '底栏 · 来源=电脑鼠'], ['lines', '行数', '18']]);
        note.textContent = '传感器明细已打印到底栏的活动流。';
        msg.textContent = 'SENSOR 完成';
        C.log('mouse', 'info', 'SENSOR · irF1=68 irF2=71 irL=34 irR=38 batt=8412');
      } else {
        box.innerHTML = kv([['path45', '去 / 回路径项数', '128 / 128'], ['ir_valid', '红外有效值', '8 / 8']]);
        note.textContent = 'BLOCK 非只读：会重载 Flash 墙图。';
        msg.textContent = 'BLOCK 完成';
      }
      C.toast('ok', '已完成', msg.textContent);
      C.log('mouse', 'info', '→ CAL6/' + item + ' · 立即 ACK');
    },

    next() {
      const root = this._root;
      if (!S.health.mouse.connected) { C.toast('warn', '未连接电脑鼠', '先到顶栏【电脑鼠】连接'); return; }
      if (done >= STEPS.length) { done = 0; step = 1; }
      const want = step;
      last = { a: 812 + want * 7, b: 806 + want * 9 };
      done = want;
      step = (want >= STEPS.length) ? 1 : want + 1;
      root.querySelector('#mt-ircap-msg').textContent = done >= STEPS.length ? '8 值已写入' : ('第 ' + done + ' 步完成');
      C.log('mouse', 'info', '→ CAL6/IRCAP step=' + want + ' · a=' + last.a + ' b=' + last.b);
      C.toast('ok', '标定第 ' + want + ' 步完成', done >= STEPS.length ? '4 个摆位采完，红外 8 值已写入。' : '摆到下一个位置再点。');
      this.paint();
    },

    arm(btn, label, cb) {
      if (armed && armed.btn === btn) { this.disarm(); cb(); return; }
      this.disarm();
      armed = { btn: btn, label: label, timer: setTimeout(() => this.disarm(), 6000) };
      btn.classList.add('on');
      btn.textContent = '再点一次确认（不可逆）';
    },
    disarm() {
      if (!armed) return;
      clearTimeout(armed.timer);
      armed.btn.textContent = armed.label;
      armed.btn.classList.remove('on');
      armed = null;
    },

    paint() {
      const root = this._root;
      if (!root || !root.querySelector('#mt-stepper')) return;

      root.querySelector('#mt-stepper').innerHTML = STEPS.map(s => {
        const cls = s.no <= done ? 'done' : (s.no === step ? 'now' : '');
        /* 摆位示意图：黑板 = 墙，小方块 = 车 */
        const carStyle = s.wall === 't'
          ? 'left:50%;transform:translateX(-50%);top:' + s.y + 'px;width:20px;height:13px'
          : (s.wall === 'l'
            ? 'left:26px;top:13px;width:20px;height:13px'
            : 'right:26px;top:13px;width:20px;height:13px');
        const wall = s.wall === 'l' ? 'left:6px;top:8px;bottom:8px;width:4px'
          : s.wall === 'r' ? 'right:6px;top:8px;bottom:8px;width:4px'
          : 'left:8px;right:8px;top:6px;height:4px';
        return '<div class="step ' + cls + '">' +
          '<span class="s-no">STEP ' + s.no + '</span>' +
          '<div class="s-diag"><i style="' + wall + '"></i><i style="' + carStyle + ';background:var(--blue)"></i></div>' +
          '<span class="s-name">' + s.name + '</span>' +
          '<span class="s-desc">' + s.desc + '</span>' +
          '</div>';
      }).join('');

      const cur = STEPS[Math.min(step, STEPS.length) - 1];
      root.querySelector('#mt-step').innerHTML = C.icon('target') +
        '<div class="b-body"><b>当前该摆：' + cur.name + '</b> ' + cur.desc + '</div>';
      root.querySelector('#mt-ircap').innerHTML = done
        ? kv([['a / b', '上一步回读', last.a + ' / ' + last.b], ['step_done', '已完成到第', done + ' 步']])
        : '<div><dt>上一步回读</dt><dd class="dim">—</dd></div><div><dt>已完成到第</dt><dd class="dim">0 步</dd></div>';
      root.querySelector('#mt-next').textContent =
        done === 0 ? '开始标定' : (done >= STEPS.length ? '重新标一次' : '下一步（第 ' + step + ' 步）');

      const t = S.tel.mazeTask;
      const busy = t >= 2 && t <= 5;
      const why = !S.health.mouse.connected ? '未连接电脑鼠' : (busy ? '车正在跑迷宫' : '');
      root.querySelectorAll('button').forEach(b => { b.disabled = !!why; });
      const bn = root.querySelector('#mt-banner');
      bn.innerHTML = why ? '<div class="banner warn" style="margin-bottom:12px">' + C.icon('alert') +
        '<div class="b-body"><b>' + why + '</b>：这一页的动作全部禁用。' +
        (busy ? '先回【比赛】页收车；运动中固件对一切控制命令只回 ERR_BUSY。'
              : '到顶栏【电脑鼠】把串口连上再回来。') + '</div></div>' : '';
      if (why) this.disarm();
    },

    onTick() { this.paint(); },
    onState() { this.paint(); },
  };

  function kv(rows) {
    return rows.map(r =>
      '<div><dt>' + r[1] + ' <code>' + r[0] + '</code></dt><dd>' + U.esc(r[2]) + '</dd></div>').join('');
  }
})();
