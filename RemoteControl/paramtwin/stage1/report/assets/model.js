/* M1 reference profile in JS. This is the SAME formula, the SAME grid and the
   SAME accumulation order as stage1/model/profile.py; tests/parity.cjs drives
   both over a case grid so the two cannot drift. */
(function (g) {
  "use strict";

  function vRef(s, dis, p) {
    var v = (s < dis * 0.5 / 1.1)
      ? 1.5 * p.ratio * s + p.bottom
      : 5 * p.dec * (dis - s) + p.bottom;
    return Math.min(v, p.top);
  }

  function analyse(dis, p, ds) {
    ds = ds || 0.25;
    var n = Math.max(2, Math.floor(dis / ds) + 1);
    var pts = [], ts = [], i, s, sw = dis * 0.5 / 1.1;
    for (i = 0; i < n; i++) {
      s = Math.min(dis, i * dis / (n - 1));
      pts.push([s, vRef(s, dis, p)]);
    }
    var t = 0, ta = 0, tc = 0, td = 0, sa = 0, sc = 0, sd = 0;
    ts.push(0);
    for (i = 0; i < pts.length - 1; i++) {
      var s0 = pts[i][0], v0 = pts[i][1], s1 = pts[i + 1][0], v1 = pts[i + 1][1];
      var vm = 0.5 * (v0 + v1);
      if (vm <= 0) { ts.push(t); continue; }
      var dt = 100 * (s1 - s0) / vm;
      t += dt;
      ts.push(t);
      if (v0 >= p.top - 0.5 && v1 >= p.top - 0.5) { tc += dt; sc += s1 - s0; }
      else if (s0 < sw) { ta += dt; sa += s1 - s0; }
      else { td += dt; sd += s1 - s0; }
    }
    var vAccEnd = Math.min(p.top, 1.5 * p.ratio * sw + p.bottom);
    var vDecStart = Math.min(p.top, 5 * p.dec * (dis - sw) + p.bottom);
    return {
      pts: pts, ts: ts, T: t, Tacc: ta, Tcru: tc, Tdec: td,
      sAcc: sa, sCru: sc, sDec: sd, sw: sw,
      vAccEnd: vAccEnd, vDecStart: vDecStart, vJump: vDecStart - vAccEnd,
      vAvg: t ? dis / t * 100 : 0, clampRatio: sc / dis
    };
  }

  /* M2: same 1 ms loop, same integer truncation, same clamp order as
     stage1/model/dynamics.py. Only tau_m and headroom are assumed. */
  function m2(dis, p, c) {
    var sr = c.speed_ratio, tau = c.tau_ms, head = c.headroom;
    var dt = c.dt || 1, tMax = c.t_max || 3000, lim = c.pwm_limit || 350;
    var kGain = head * p.top * sr / lim;
    var track = 0, vPulse = 0, errs = 0, errH = 0, t = 0, sat = 0, vPeak = 0;
    var out = { t: [], vRef: [], vAct: [], pwm: [], s: [] };
    while (track < dis && t < tMax) {
      var vr = vRef(Math.min(track, dis), dis, p);
      var gdvl = Math.trunc(vr * sr);
      var err = gdvl - vPulse;
      errs += err;
      errs = Math.max(-1200, Math.min(1200, errs));
      var pwm = c.kpl * err + c.kil * errs + c.kdl * (err - errH);
      errH = err;
      if (pwm > lim) { pwm = lim; sat++; } else if (pwm < -lim) { pwm = -lim; sat++; }
      vPulse += (kGain * pwm - vPulse) * dt / tau;
      var vc = vPulse / sr;
      track += vc * 0.01 * dt;
      if (vc > vPeak) vPeak = vc;
      out.t.push(t); out.vRef.push(vr); out.vAct.push(vc);
      out.pwm.push(pwm); out.s.push(track);
      t += dt;
    }
    var refT = vRef(dis * 0.5, dis, p);
    out.T = t;
    out.vPeak = vPeak;
    out.overshoot = refT ? (vPeak - refT) / refT * 100 : 0;
    out.satRatio = sat / Math.max(t, 1);
    out.vAvg = dis / Math.max(t, 1) * 100;
    return out;
  }

  g.PTM = { vRef: vRef, analyse: analyse, m2: m2 };
})(typeof window !== "undefined" ? window : globalThis);
