/* Stage1 report logic. Mirrors model/profile.py exactly (same grid, same order). */
(function () {
  "use strict";
  var D = JSON.parse(document.getElementById("data").textContent);
  var state = { view: "overview", grade: "LOW", vs: 240, dis: 180,
                action: "LT90SEARCH", tau: 10 };

  var analyse = window.PTM.analyse;

  function curParams() {
    var g = D.grades[state.grade] || D.grades.LOW;
    return { top: state.vs, bottom: g.bottom, ratio: g.ratio, dec: g.dec, grade: state.grade };
  }

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, txt) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt !== undefined) e.textContent = txt;
    return e;
  }
  function fmt(v, n) {
    if (v === null || v === undefined || isNaN(v)) return "-";
    return Number(v).toFixed(n === undefined ? 1 : n);
  }

  function kpiCard(label, value, unit, note, method) {
    var c = el("div", "kpi" + (method === "assumed" ? " assumed" : ""));
    c.appendChild(el("div", "kpi-label", label));
    var v = el("div", "kpi-value");
    v.appendChild(document.createTextNode(value));
    if (unit) { var u = el("span", "kpi-unit", unit); v.appendChild(u); }
    c.appendChild(v);
    if (note) c.appendChild(el("div", "kpi-note", note));
    return c;
  }

  function renderOverview() {
    var flat = {};
    D.families.forEach(function (f) {
      f.items.forEach(function (i) { flat[i.key] = i; });
    });
    var box = $("kpi-grid");
    box.innerHTML = "";
    box.appendChild(kpiCard("v_set", fmt(flat["F-A.v_set"].value, 0), "cm/s", "TopSpeed clamp ceiling"));
    box.appendChild(kpiCard("v_peak (accel end)", fmt(flat["F-A.v_acc_end"].value, 1), "cm/s", "reference speed at the switch"));
    box.appendChild(kpiCard("v_jump", fmt(flat["F-A.v_jump"].value, 1), "cm/s", "discontinuity at s*"));
    box.appendChild(kpiCard("T_cell 180mm", fmt(flat["F-B.T_cell_180"].value, 1), "ms", "one cell"));
    box.appendChild(kpiCard("v_avg", fmt(flat["F-B.v_avg_180"].value, 1), "cm/s", "ceiling realisation " + fmt(flat["F-B.v_eff_180"].value, 2)));
    box.appendChild(kpiCard("theta RT90", fmt(flat["F-C.theta_rt90"].value, 1), "deg", "table integral, not 90"));
    box.appendChild(kpiCard("b_est", fmt(D.checks.b_est_mean, 2), "cm", "differential / angle, unit check"));
    box.appendChild(kpiCard("turn asym", fmt(flat["F-C.asym_90"].value, 1), "deg", "left vs right, written in"));

    var wbox = $("warn-list");
    wbox.innerHTML = "";
    (D.warnings || []).forEach(function (w) {
      var d = el("div", "warn");
      d.appendChild(el("span", "warn-code", w.code));
      d.appendChild(el("span", "warn-detail", w.action ? w.action + " - " + (w.detail || "") : (w.detail || "")));
      wbox.appendChild(d);
    });
    if (!(D.warnings || []).length) wbox.appendChild(el("div", "warn ok", "no structural warnings"));

    var st = $("struct-facts");
    st.innerHTML = "";
    var facts = [
      ["parameters", D.summary.n_params],
      ["phases", D.summary.n_phases],
      ["multi-scope", D.summary.n_multi_scope],
      ["runtime overwrite", D.summary.n_runtime_overwrite],
      ["tables", D.summary.n_tables],
      ["actions", D.summary.n_actions]
    ];
    facts.forEach(function (f) {
      var r = el("div", "fact");
      r.appendChild(el("span", "fact-k", f[0]));
      r.appendChild(el("span", "fact-v", String(f[1])));
      st.appendChild(r);
    });
  }

  function renderSweep() {
    var disList = D.meta.dis_list;
    var colors = [PT.colors().accent, PT.colors().good, PT.colors().warn];
    var series = [], i;
    var vsList = D.meta.v_set_grid;
    for (i = 0; i < disList.length; i++) {
      (function (idx) {
        var xs = [], ys = [];
        vsList.forEach(function (v) {
          var p = { top: v, bottom: D.grades[state.grade].bottom, ratio: D.grades[state.grade].ratio, dec: D.grades[state.grade].dec };
          xs.push(v);
          ys.push(analyse(disList[idx], p).T);
        });
        series.push({ x: xs, y: ys, color: colors[idx % 3], label: disList[idx] + " mm" });
      })(i);
    }
    var pNow = curParams();
    var aNow = analyse(180, pNow);
    PT.chart($("c-sweep"), {
      series: series.map(function (s) { return { x: s.x, y: s.y, color: s.color, width: 2 }; }),
      xLabel: "v_set (cm/s)", yLabel: "T_cell (ms)",
      vlines: [{ x: state.vs, label: "v_set " + state.vs, color: PT.colors().bad }],
      marker: { x: state.vs, y: aNow.T, label: fmt(aNow.T, 1) + " ms", color: PT.colors().bad },
      title: "single-cell time vs target speed"
    });

    var lg = $("sweep-legend");
    lg.innerHTML = "";
    disList.forEach(function (d, k) {
      var s = el("span", "lg");
      var sw = el("i");
      sw.style.background = colors[k % 3];
      s.appendChild(sw);
      s.appendChild(document.createTextNode(d + " mm"));
      lg.appendChild(s);
    });

    var rows = $("sweep-rows");
    rows.innerHTML = "";
    vsList.forEach(function (v) {
      var p = { top: v, bottom: D.grades[state.grade].bottom, ratio: D.grades[state.grade].ratio, dec: D.grades[state.grade].dec };
      var a = analyse(180, p);
      var tr = el("tr", v === state.vs ? "sel" : "");
      tr.appendChild(el("td", "", String(v)));
      tr.appendChild(el("td", "", fmt(a.T, 1)));
      tr.appendChild(el("td", "", fmt(a.vAvg, 1)));
      tr.appendChild(el("td", "", fmt(a.vJump, 1)));
      tr.appendChild(el("td", "", fmt(a.clampRatio * 100, 0) + "%"));
      rows.appendChild(tr);
    });
  }

  function renderProfile() {
    var p = curParams();
    var a = analyse(state.dis, p);
    var mc = {
      speed_ratio: D.m2.speed_ratio, kpl: D.m2.kpl, kil: D.m2.kil, kdl: D.m2.kdl,
      tau_ms: state.tau, headroom: D.m2.headroom, pwm_limit: D.m2.pwm_limit,
      t_max: D.m2.t_max
    };
    var m2 = PTM.m2(state.dis, p, mc);

    var xs = a.pts.map(function (q) { return q[0]; });
    var vs = a.pts.map(function (q) { return q[1]; });
    PT.chart($("c-vs"), {
      series: [
        { x: xs, y: vs, color: PT.colors().accent, width: 2 }
      ],
      hlines: [{ y: p.top, label: "v_set " + fmt(p.top, 0), color: PT.colors().warn },
               { y: p.bottom, label: "v_floor " + fmt(p.bottom, 0), color: PT.colors().muted }],
      vlines: [{ x: a.sw, label: "s* " + fmt(a.sw, 1) + " mm", color: PT.colors().bad }],
      xLabel: "distance (mm)", yLabel: "reference speed (cm/s)",
      title: "reference profile v(s), dis=" + state.dis + " mm"
    });
    PT.chart($("c-vt"), {
      series: [
        { x: a.ts, y: vs, color: PT.colors().good, width: 2 },
        { x: m2.t, y: m2.vAct, color: PT.colors().warn, width: 1.4, dash: [5, 3] }
      ],
      xLabel: "time (ms)", yLabel: "v (cm/s)",
      marker: { x: a.T, y: p.bottom, label: "M1 " + fmt(a.T, 1) + " ms", color: PT.colors().bad },
      title: "M1 reference vs M2 first-order plant"
    });
    PT.chart($("c-pwm"), {
      series: [
        { x: m2.t, y: m2.pwm, color: PT.colors().accent, width: 1.2 },
        { x: m2.t, y: m2.vAct, color: PT.colors().warn, width: 1.2, dash: [5, 3] }
      ],
      yRange: [-D.m2.pwm_limit, D.m2.pwm_limit],
      y0: true,
      hlines: [{ y: D.m2.pwm_limit, label: "PWM limit " + D.m2.pwm_limit, color: PT.colors().bad },
               { y: -D.m2.pwm_limit, label: "", color: PT.colors().bad }],
      xLabel: "time (ms)", yLabel: "pwm / v_act",
      title: "M2 command effort (solid: pwm, dashed: v_act)"
    });

    var box = $("profile-facts");
    box.innerHTML = "";
    [["T total", fmt(a.T, 1) + " ms"],
     ["T acc / cru / dec", fmt(a.Tacc, 1) + " / " + fmt(a.Tcru, 1) + " / " + fmt(a.Tdec, 1) + " ms"],
     ["s acc / cru / dec", fmt(a.sAcc, 1) + " / " + fmt(a.sCru, 1) + " / " + fmt(a.sDec, 1) + " mm"],
     ["v at switch", fmt(a.vAccEnd, 1) + " -> " + fmt(a.vDecStart, 1) + " cm/s"],
     ["jump", fmt(a.vJump, 1) + " cm/s"],
     ["clamped travel", fmt(a.clampRatio * 100, 0) + " %"],
     ["v_avg", fmt(a.vAvg, 1) + " cm/s"],
     ["M2 T / v_peak", fmt(m2.T, 1) + " ms / " + fmt(m2.vPeak, 1)],
     ["M2 pwm saturated", fmt(m2.satRatio * 100, 1) + " %"],
     ["M2 overshoot", fmt(m2.overshoot, 1) + " %"]
    ].forEach(function (kv) {
      var r = el("div", "fact");
      r.appendChild(el("span", "fact-k", kv[0]));
      r.appendChild(el("span", "fact-v", kv[1]));
      box.appendChild(r);
    });
  }

  function fillActions() {
    var sel = $("act-sel");
    if (sel.options.length) return;
    Object.keys(D.playback).forEach(function (k) {
      var o = el("option", "", k + " (" + fmt(D.playback[k].scalars.theta_deg, 1) + " deg)");
      o.value = k;
      sel.appendChild(o);
    });
    sel.value = state.action;
  }

  function renderTurn() {
    fillActions();
    var act = D.playback[state.action];
    if (!act) return;
    var ch = act.channels;
    PT.chart($("c-theta"), {
      series: [
        { x: ch.t_ms, y: ch.theta_deg, color: PT.colors().accent, width: 2 }
      ],
      hlines: [{ y: (act.scalars.theta_deg >= 0 ? 1 : -1) * act.nominal_deg,
                 label: "nominal " + fmt(act.nominal_deg, 0) + " deg",
                 color: PT.colors().warn }],
      xLabel: "time (ms)", yLabel: "angle (deg)",
      title: "theta(t): integral of the reference table"
    });
    PT.chart($("c-omega"), {
      series: [{ x: ch.t_ms, y: ch.omega_dps, color: PT.colors().accent, width: 1.6 }],
      y0: true,
      marker: { x: act.tick_max, y: 0, label: "table end", color: PT.colors().bad },
      xLabel: "time (ms)", yLabel: "omega (deg/s)",
      title: "angular rate implied by the reference table"
    });
    PT.chart($("c-xy"), {
      series: [{ x: ch.x_mm, y: ch.y_mm, color: PT.colors().good, width: 2 }],
      xLabel: "x (mm)", yLabel: "y (mm)", y0: false,
      title: "planar path (ideal, no slip, b_est from the tables)"
    });
    var kind = $("turn-kind");
    if (kind) kind.textContent = act.kind === "hard_stop"
      ? "hard stop: plays to tick " + act.tick_max + " then step=1 P-only trim"
      : "switch: plays to length/2-2 then step SMARTSTRAIGHT";
    var box = $("turn-facts");
    box.innerHTML = "";
    var s = act.scalars;
    [["action", act.action],
     ["nominal", fmt(act.nominal_deg, 0) + " deg"],
     ["table integral", fmt(s.theta_deg, 1) + " deg"],
     ["delta vs nominal", (act.delta_deg > 0 ? "+" : "") + fmt(act.delta_deg, 1) + " deg"],
     ["table tail value", fmt(act.table_tail, 1) + " deg"],
     ["duration", s.t_ms + " ms"],
     ["omega avg", fmt(s.omega_avg_dps, 0) + " deg/s"],
     ["omega peak", fmt(s.omega_peak_dps, 0) + " deg/s"],
     ["path length", fmt(s.path_mm, 1) + " mm"],
     ["b_est", (s.b_est_cm === null ? "-" : fmt(s.b_est_cm, 2)) + " cm"],
     ["overrun", act.overrun ? act.overrun + " samples" : "no"]
    ].forEach(function (kv) {
      var r = el("div", "fact");
      r.appendChild(el("span", "fact-k", kv[0]));
      r.appendChild(el("span", "fact-v", kv[1]));
      box.appendChild(r);
    });
  }

  function renderHealth() {
    var rows = D.tables.health.map(function (h) {
      return { label: h.action, value: h.delta_theta };
    });
    PT.bars($("c-health"), { rows: rows, title: "table integral minus nominal angle (deg)" });

    var tb = $("health-table");
    tb.innerHTML = "";
    var head = el("tr");
    ["action", "n", "tick_max", "played", "delta", "residual"].forEach(function (h) {
      head.appendChild(el("th", "", h));
    });
    tb.appendChild(head);
    D.tables.health.forEach(function (h) {
      var tr = el("tr", h.overrun ? "bad" : "");
      tr.appendChild(el("td", "", h.action));
      tr.appendChild(el("td", "", String(h.n)));
      tr.appendChild(el("td", "", String(h.tick_max)));
      tr.appendChild(el("td", "", fmt(h.window_ratio * 100, 0) + "%"));
      tr.appendChild(el("td", "", fmt(h.delta_theta, 1)));
      tr.appendChild(el("td", "", fmt(h.residual_deg, 1)));
      tb.appendChild(tr);
    });

    var dup = $("dup-list");
    dup.innerHTML = "";
    if (!D.tables.duplicates.length) dup.appendChild(el("div", "warn ok", "no byte-identical duplicate tables"));
    D.tables.duplicates.forEach(function (d) {
      var r = el("div", "fact");
      r.appendChild(el("span", "fact-k", d.tables.join(" = ")));
      r.appendChild(el("span", "fact-v", d.sha16));
      dup.appendChild(r);
    });
  }

  function renderParams() {
    var tb = $("param-table");
    tb.innerHTML = "";
    var head = el("tr");
    ["parameter", "group", "unit", "active", "runtime", "scopes", "risks"].forEach(function (h) {
      head.appendChild(el("th", "", h));
    });
    tb.appendChild(head);
    D.params.forEach(function (p) {
      var tr = el("tr", p.risks.length ? "flag" : "");
      tr.appendChild(el("td", "mono", p.name));
      tr.appendChild(el("td", "", p.group));
      tr.appendChild(el("td", "", p.unit || "-"));
      tr.appendChild(el("td", "mono", String(p.active.value === null ? "-" : p.active.value)));
      tr.appendChild(el("td", "mono", p.runtime ? String(p.runtime.value) + " @" + p.runtime.phase : "-"));
      tr.appendChild(el("td", "", String(Object.keys(p.scopes).length)));
      tr.appendChild(el("td", "", p.risks.join(", ") || "-"));
      tb.appendChild(tr);
    });
  }

  function renderAll() {
    renderSweep();
    renderProfile();
    if (state.view === "overview") renderOverview();
    if (state.view === "turn") renderTurn();
    if (state.view === "health") renderHealth();
    if (state.view === "params") renderParams();
  }

  function setView(v) {
    state.view = v;
    Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (t) {
      t.classList.toggle("on", t.dataset.view === v);
    });
    Array.prototype.forEach.call(document.querySelectorAll(".view"), function (s) {
      s.classList.toggle("on", s.id === "view-" + v);
    });
    renderAll();
  }

  function bind() {
    if (D.m2 && D.m2.tau_ms) state.tau = Math.round(D.m2.tau_ms);
    Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (t) {
      t.addEventListener("click", function () { setView(t.dataset.view); });
    });
    var vs = $("vs");
    vs.value = state.vs;
    vs.addEventListener("input", function () {
      state.vs = Number(vs.value);
      $("vs-out").textContent = state.vs + " cm/s";
      renderAll();
    });
    var gs = $("grade");
    gs.value = state.grade;
    gs.addEventListener("change", function () {
      state.grade = gs.value;
      renderAll();
    });
    var ds = $("dis");
    ds.value = state.dis;
    ds.addEventListener("input", function () {
      state.dis = Number(ds.value);
      $("dis-out").textContent = state.dis + " mm";
      renderProfile();
    });
    var tq = $("tau");
    if (tq) {
      tq.value = state.tau;
      $("tau-out").textContent = state.tau + " ms";
      tq.addEventListener("input", function () {
        state.tau = Number(tq.value);
        $("tau-out").textContent = state.tau + " ms";
        renderProfile();
      });
    }
    $("act-sel").addEventListener("change", function (e) {
      state.action = e.target.value;
      renderTurn();
    });
    var to;
    window.addEventListener("resize", function () {
      clearTimeout(to);
      to = setTimeout(renderAll, 120);
    });
    document.addEventListener("keydown", function (e) {
      if (e.key >= "1" && e.key <= "6") {
        var i = Number(e.key) - 1;
        var tabs = document.querySelectorAll(".tab");
        if (tabs[i]) setView(tabs[i].dataset.view);
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    fillActions();
    bind();
    setView("overview");
    $("meta-line").textContent = D.meta.generated + "  ·  spec " + D.meta.spec_version +
      "  ·  params sha " + D.meta.params_sha.slice(0, 12);
  });
})();
