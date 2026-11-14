"""M0: table playback. Zero assumptions, pure data.

The gyro table is a *reference angle*, not a rate. Under ideal tracking the
vehicle angle equals the reference, so

    theta(t) = g[0] - g[t]        (signed; left positive, right negative)
    omega(t) = -(g[t+1] - g[t])   [deg/ms]

Wheel speeds come straight from the v tables (cm/s). The equivalent wheel
track is *measured*, not assumed:

    b = integral(|v_out - v_in| dt) / theta[rad]

which is how stage1 validates the mm / cm-s unit chain.
"""

import math

SCHEMA_VERSION = "1.0"


def _table(tir, name):
    for t in tir["tables"]:
        if t["name"] == name:
            return t["vals"]
    return []


def run(tir, action_id, ds=1.0):
    act = next((a for a in tir["actions"] if a["id"] == action_id), None)
    if act is None:
        raise KeyError("action not found: %s" % action_id)

    g = _table(tir, act["gyro"])
    vo = _table(tir, act["v_outer"])
    vi = _table(tir, act["v_inner"])
    if not g:
        return {"action": action_id, "empty": True, "channels": {}, "scalars": {}}

    tmax = max(0, min(act["tick_max"], len(g) - 1))
    g0 = g[0]
    heading = 0.0
    x = y = 0.0

    ch = {"t_ms": [], "theta_deg": [], "omega_dps": [], "v_outer": [],
          "v_inner": [], "v_center": [], "x_mm": [], "y_mm": [], "heading_deg": []}

    prev_theta = 0.0
    for t in range(tmax + 1):
        theta = g0 - g[t]
        omega = (theta - prev_theta) * 1000.0
        prev_theta = theta

        vl = vi[t] if t < len(vi) else (vi[-1] if vi else 0)
        vr = vo[t] if t < len(vo) else (vo[-1] if vo else 0)
        vc = 0.5 * (vl + vr)

        ch["t_ms"].append(t)
        ch["theta_deg"].append(round(theta, 3))
        ch["omega_dps"].append(round(omega, 1))
        ch["v_outer"].append(vr)
        ch["v_inner"].append(vl)
        ch["v_center"].append(round(vc, 2))
        ch["x_mm"].append(round(x, 2))
        ch["y_mm"].append(round(y, 2))
        ch["heading_deg"].append(round(heading, 2))

        step_mm = vc * 0.01 * ds
        heading = theta
        x += step_mm * math.cos(math.radians(heading))
        y += step_mm * math.sin(math.radians(heading))

    thetas = ch["theta_deg"]
    omegas = [abs(w) for w in ch["omega_dps"]]
    path = sum(abs(ch["v_center"][i]) * 0.01 for i in range(len(ch["v_center"])))
    return {
        "action": action_id,
        "kind": act["kind"],
        "nominal_deg": act["nominal_deg"],
        "tick_max": tmax,
        "theta_end_deg": round(g0 - g[tmax], 2),
        "empty": act["empty"],
        "channels": ch,
        "scalars": {
            "theta_deg": round(thetas[-1], 2),
            "t_ms": tmax,
            "omega_avg_dps": round(abs(thetas[-1]) / tmax * 1000.0, 1) if tmax else 0,
            "omega_peak_dps": round(max(omegas), 1) if omegas else 0,
            "v_center_avg": round(sum(ch["v_center"]) / len(ch["v_center"]), 2),
            "path_mm": round(path, 1),
            "b_est_cm": act["b_est_cm"],
            "overrun": act["overrun"],
        },
    }


def all_actions(tir):
    out = {}
    for a in tir["actions"]:
        if a["empty"]:
            continue
        r = run(tir, a["id"])
        out[a["id"]] = r
    return out
