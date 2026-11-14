"""M1: the reference velocity profile produced by `taskstraight90`.

This is NOT a trapezoid. `utility.c:1383-1388` is a two-slope polyline with a
hard switch at s* = dis*0.5/1.1 and a clamp to TopSpeed applied after the
branch, so the reference velocity is discontinuous at s*:

    v_ref(s) = min(Top, 1.5*ratio*s      + Bottom)   s <  s*
             = min(Top, 5*dec*(dis - s)  + Bottom)   s >= s*

Both branches end on `+ BottomSpeed`, therefore BottomSpeed is a hard floor:
requesting a lower target speed changes nothing unless BottomSpeed is lowered
too.
"""

SCHEMA_VERSION = "1.0"

GRADES = {
    "LOW": {
        "top": "LTopSpeed", "bottom": "LBottomSpeed",
        "ratio": "SpurtAccRatio45", "dec": "SpurtDecRatio45",
        "source": "UTILITY/utility.c:1377-1380",
    },
    "LOW_90": {
        "top": "L90TopSpeed", "bottom": "L90BottomSpeed",
        "ratio": "SpurtAccRatio90", "dec": "SpurtDecRatio90",
        "source": "UTILITY/utility.c:1364-1367",
    },
    "BACK": {
        "top": "BackTopSpeed", "bottom": "BackBottomSpeed",
        "ratio": "SpurtAccRatio90", "dec": "SpurtDecRatio90",
        "source": "UTILITY/utility.c:1350-1353",
    },
}


def grade_params(ir, grade="LOW"):
    """Pull the speed parameters of one grade out of ParamIR."""
    from ..ir import params as P

    g = GRADES[grade]
    out = {"grade": grade, "source": g["source"]}
    for key in ("top", "bottom", "ratio", "dec"):
        out[key] = P.value(ir, g[key])
    missing = [k for k in ("top", "bottom", "ratio", "dec") if out[k] is None]
    out["complete"] = not missing
    return out


def patch(ir, grade="LOW", **overrides):
    """grade_params with overrides, used by the speed sweep / patch policy."""
    p = grade_params(ir, grade)
    p.update({k: v for k, v in overrides.items() if v is not None})
    return p


def v_ref(s, dis, p):
    """Exact transcription of the firmware branch, including the clamp."""
    if s < dis * 0.5 / 1.1:
        v = 1.5 * p["ratio"] * s + p["bottom"]
    else:
        v = 5.0 * p["dec"] * (dis - s) + p["bottom"]
    return min(v, p["top"])


def sample(dis, p, ds=0.25):
    """Sample v(s) on a uniform grid. Returns [(s, v), ...]."""
    if dis <= 0:
        return [(0.0, p["bottom"])]
    n = max(2, int(dis / ds) + 1)
    pts = []
    for i in range(n):
        s = min(dis, i * dis / (n - 1))
        pts.append((s, v_ref(s, dis, p)))
    return pts


def analyse(dis, p, ds=0.25):
    """Time + shape metrics of the reference profile."""
    pts = sample(dis, p, ds)
    t = 0.0
    t_acc = t_cru = t_dec = 0.0
    s_acc = s_cru = s_dec = 0.0
    for (s0, v0), (s1, v1) in zip(pts, pts[1:]):
        vm = 0.5 * (v0 + v1)
        if vm <= 0:
            continue
        dt = 100.0 * (s1 - s0) / vm
        t += dt
        if v0 >= p["top"] - 0.5 and v1 >= p["top"] - 0.5:
            t_cru += dt
            s_cru += s1 - s0
        elif s0 < dis * 0.5 / 1.1:
            t_acc += dt
            s_acc += s1 - s0
        else:
            t_dec += dt
            s_dec += s1 - s0

    s_sw = dis * 0.5 / 1.1
    v_acc_end = min(p["top"], 1.5 * p["ratio"] * s_sw + p["bottom"])
    v_dec_start = min(p["top"], 5.0 * p["dec"] * (dis - s_sw) + p["bottom"])
    v_peak = max(v for _s, v in pts)

    return {
        "dis_mm": dis,
        "grade": p.get("grade"),
        "T_total_ms": round(t, 2),
        "T_acc_ms": round(t_acc, 2),
        "T_cru_ms": round(t_cru, 2),
        "T_dec_ms": round(t_dec, 2),
        "s_acc_mm": round(s_acc, 1),
        "s_cru_mm": round(s_cru, 1),
        "s_dec_mm": round(s_dec, 1),
        "s_switch_mm": round(s_sw, 2),
        "v_acc_end": round(v_acc_end, 2),
        "v_dec_start": round(v_dec_start, 2),
        "v_jump": round(v_dec_start - v_acc_end, 2),
        "v_peak": round(v_peak, 2),
        "v_avg": round(dis / t * 100.0, 2) if t else 0.0,
        "clamp_ratio": round(s_cru / dis, 3) if dis else 0.0,
        "points": pts,
    }


def sweep(ir, grade="LOW", dis_list=(180, 254, 360), v_set=(60, 80, 100, 140, 180, 240, 320, 400)):
    """Speed sweep under the `anchor` policy: only TopSpeed moves."""
    rows = []
    for v in v_set:
        p = patch(ir, grade, top=v)
        for d in dis_list:
            a = analyse(d, p)
            a.pop("points", None)
            a["v_set"] = v
            rows.append(a)
    return rows
