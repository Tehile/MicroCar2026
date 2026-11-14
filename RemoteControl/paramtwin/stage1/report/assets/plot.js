/* Minimal Canvas chart library. No dependencies, no network, offline-safe. */
(function (global) {
  "use strict";

  var CSS = {};
  function readTheme() {
    var s = getComputedStyle(document.documentElement);
    CSS.text = s.getPropertyValue("--color-text-primary").trim() || "#1a1a1a";
    CSS.muted = s.getPropertyValue("--color-text-secondary").trim() || "#6b6b6b";
    CSS.grid = s.getPropertyValue("--color-border-tertiary").trim() || "rgba(120,120,120,.25)";
    CSS.bg = s.getPropertyValue("--color-background-secondary").trim() || "#ffffff";
    CSS.accent = s.getPropertyValue("--color-text-info").trim() || "#2f6fd0";
    CSS.warn = s.getPropertyValue("--color-text-warning").trim() || "#b5761a";
    CSS.bad = s.getPropertyValue("--color-text-danger").trim() || "#c0392b";
    CSS.good = s.getPropertyValue("--color-text-success").trim() || "#2e7d5b";
  }

  function fit(canvas) {
    var dpr = global.devicePixelRatio || 1;
    var r = canvas.getBoundingClientRect();
    var w = Math.max(200, r.width || canvas.clientWidth || 400);
    var h = Math.max(120, r.height || canvas.clientHeight || 220);
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx: ctx, w: w, h: h };
  }

  function extent(series, key) {
    var lo = Infinity, hi = -Infinity;
    series.forEach(function (s) {
      s[key].forEach(function (v) {
        if (v === null || isNaN(v)) return;
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      });
    });
    if (!isFinite(lo)) { lo = 0; hi = 1; }
    if (lo === hi) { lo -= 1; hi += 1; }
    return [lo, hi];
  }

  function niceTicks(lo, hi, n) {
    var span = hi - lo;
    var raw = span / Math.max(1, n);
    var mag = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
    var norm = raw / mag;
    var step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
    var out = [];
    for (var v = Math.ceil(lo / step) * step; v <= hi + step * 1e-6; v += step) out.push(v);
    return out;
  }

  function fmt(v) {
    if (v === null || v === undefined || isNaN(v)) return "-";
    var a = Math.abs(v);
    if (a >= 1000) return (v / 1000).toFixed(1) + "k";
    if (a >= 100) return v.toFixed(0);
    if (a >= 10) return v.toFixed(1);
    if (a >= 1) return v.toFixed(2);
    return v.toFixed(3);
  }

  function chart(canvas, cfg) {
    readTheme();
    var f = fit(canvas), ctx = f.ctx, w = f.w, h = f.h;
    var pad = { l: 52, r: 16, t: 14, b: 34 };
    var series = (cfg.series || []).filter(function (s) { return s && s.x && s.y; });
    if (!series.length) {
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = CSS.muted;
      ctx.font = "13px system-ui, sans-serif";
      ctx.fillText("no data", pad.l, h / 2);
      return;
    }
    var xr = cfg.xRange || extent(series, "x");
    var yr = cfg.yRange || extent(series, "y");
    if (cfg.y0) { yr[0] = Math.min(yr[0], 0); }
    var yPad = (yr[1] - yr[0]) * 0.08;
    yr = [yr[0] - (cfg.y0 ? 0 : yPad), yr[1] + yPad];

    var X = function (v) { return pad.l + (v - xr[0]) / (xr[1] - xr[0]) * (w - pad.l - pad.r); };
    var Y = function (v) { return h - pad.b - (v - yr[0]) / (yr[1] - yr[0]) * (h - pad.t - pad.b); };

    ctx.clearRect(0, 0, w, h);

    var yt = niceTicks(yr[0], yr[1], 5);
    ctx.font = "11px system-ui, sans-serif";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    yt.forEach(function (v) {
      var y = Y(v);
      ctx.strokeStyle = CSS.grid;
      ctx.lineWidth = 0.5;
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
      ctx.fillStyle = CSS.muted;
      ctx.fillText(fmt(v), pad.l - 8, y);
    });

    var xt = niceTicks(xr[0], xr[1], 6);
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    xt.forEach(function (v) {
      var x = X(v);
      ctx.strokeStyle = CSS.grid;
      ctx.lineWidth = 0.5;
      ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, h - pad.b); ctx.stroke();
      ctx.fillStyle = CSS.muted;
      ctx.fillText(fmt(v), x, h - pad.b + 8);
    });

    if (cfg.hlines) {
      cfg.hlines.forEach(function (hl) {
        if (hl.y < yr[0] || hl.y > yr[1]) return;
        ctx.save();
        ctx.setLineDash([4, 3]);
        ctx.strokeStyle = hl.color || CSS.warn;
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(pad.l, Y(hl.y)); ctx.lineTo(w - pad.r, Y(hl.y)); ctx.stroke();
        ctx.restore();
        ctx.fillStyle = hl.color || CSS.warn;
        ctx.font = "11px system-ui, sans-serif";
        ctx.textAlign = "left";
        ctx.textBaseline = "bottom";
        ctx.fillText(hl.label, pad.l + 4, Y(hl.y) - 3);
      });
    }

    if (cfg.vlines) {
      cfg.vlines.forEach(function (vl) {
        if (vl.x < xr[0] || vl.x > xr[1]) return;
        ctx.save();
        ctx.setLineDash([4, 3]);
        ctx.strokeStyle = vl.color || CSS.muted;
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(X(vl.x), pad.t); ctx.lineTo(X(vl.x), h - pad.b); ctx.stroke();
        ctx.restore();
        ctx.fillStyle = vl.color || CSS.muted;
        ctx.font = "11px system-ui, sans-serif";
        ctx.textAlign = "left";
        ctx.textBaseline = "top";
        ctx.fillText(vl.label, X(vl.x) + 4, pad.t + 2);
      });
    }

    series.forEach(function (s) {
      ctx.strokeStyle = s.color || CSS.accent;
      ctx.lineWidth = s.width || 1.8;
      ctx.lineJoin = "round";
      if (s.dash) ctx.setLineDash(s.dash); else ctx.setLineDash([]);
      ctx.beginPath();
      var started = false;
      for (var i = 0; i < s.x.length; i++) {
        var px = X(s.x[i]), py = Y(s.y[i]);
        if (!started) { ctx.moveTo(px, py); started = true; } else { ctx.lineTo(px, py); }
      }
      ctx.stroke();
      ctx.setLineDash([]);
      if (s.dots) {
        ctx.fillStyle = s.color || CSS.accent;
        for (var j = 0; j < s.x.length; j += (s.dotEvery || 1)) {
          ctx.beginPath();
          ctx.arc(X(s.x[j]), Y(s.y[j]), 2.2, 0, Math.PI * 2);
          ctx.fill();
        }
      }
    });

    if (cfg.marker) {
      var m = cfg.marker;
      ctx.fillStyle = m.color || CSS.bad;
      ctx.beginPath();
      ctx.arc(X(m.x), Y(m.y), 4.5, 0, Math.PI * 2);
      ctx.fill();
      if (m.label) {
        ctx.font = "11px system-ui, sans-serif";
        ctx.textAlign = "left";
        ctx.textBaseline = "bottom";
        ctx.fillText(m.label, X(m.x) + 7, Y(m.y) - 4);
      }
    }

    if (cfg.xLabel) {
      ctx.fillStyle = CSS.muted;
      ctx.font = "11px system-ui, sans-serif";
      ctx.textAlign = "right";
      ctx.textBaseline = "bottom";
      ctx.fillText(cfg.xLabel, w - pad.r, h - 2);
    }
    if (cfg.yLabel) {
      ctx.fillStyle = CSS.muted;
      ctx.font = "11px system-ui, sans-serif";
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.fillText(cfg.yLabel, 4, 2);
    }
    if (cfg.title) {
      ctx.fillStyle = CSS.text;
      ctx.font = "500 12px system-ui, sans-serif";
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.fillText(cfg.title, pad.l, 0);
    }
  }

  function bars(canvas, cfg) {
    readTheme();
    var f = fit(canvas), ctx = f.ctx, w = f.w, h = f.h;
    var pad = { l: 52, r: 16, t: 12, b: 46 };
    var rows = cfg.rows || [];
    ctx.clearRect(0, 0, w, h);
    if (!rows.length) return;
    var vals = rows.map(function (r) { return r.value || 0; });
    var lo = Math.min.apply(null, vals.concat([0]));
    var hi = Math.max.apply(null, vals.concat([0]));
    if (lo === hi) hi = lo + 1;
    var Y = function (v) { return h - pad.b - (v - lo) / (hi - lo) * (h - pad.t - pad.b); };
    var zero = Y(0);

    ctx.strokeStyle = CSS.grid;
    ctx.lineWidth = 0.5;
    ctx.beginPath(); ctx.moveTo(pad.l, zero); ctx.lineTo(w - pad.r, zero); ctx.stroke();

    var iw = (w - pad.l - pad.r) / rows.length;
    rows.forEach(function (r, i) {
      var x = pad.l + i * iw + iw * 0.18;
      var bw = iw * 0.64;
      var v = r.value || 0;
      ctx.fillStyle = v === 0 ? CSS.muted : (v > 0 ? CSS.bad : CSS.accent);
      var y0 = Math.min(zero, Y(v)), hh = Math.abs(Y(v) - zero);
      ctx.fillRect(x, y0, bw, Math.max(1, hh));
      ctx.fillStyle = CSS.text;
      ctx.font = "10px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = v >= 0 ? "bottom" : "top";
      ctx.fillText(fmt(v), x + bw / 2, v >= 0 ? y0 - 2 : y0 + hh + 2);
      ctx.save();
      ctx.translate(x + bw / 2, h - pad.b + 6);
      ctx.rotate(-Math.PI / 3.2);
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      ctx.fillStyle = CSS.muted;
      ctx.fillText(r.label, 0, 0);
      ctx.restore();
    });
    if (cfg.title) {
      ctx.fillStyle = CSS.text;
      ctx.font = "500 12px system-ui, sans-serif";
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.fillText(cfg.title, pad.l, 0);
    }
  }

  global.PT = { chart: chart, bars: bars, colors: function () { readTheme(); return CSS; } };
})(window);
