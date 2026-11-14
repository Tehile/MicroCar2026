"""F-C turn geometry, straight from the tables (no simulation needed)."""

from .base import family, feat, r2

KEY = ("LT90SEARCH", "RT90SEARCH", "L180L", "TURNAROUND", "POINTTURN90_L")


def _map(tir):
    return {a["id"]: a for a in tir["actions"]}


def turn(tir):
    acts = _map(tir)
    items = []
    lt = acts.get("LT90SEARCH")
    rt = acts.get("RT90SEARCH")
    h180 = acts.get("L180L")

    if lt:
        items.append(feat("F-C.theta_lt90", lt["theta_tab"], "deg", "exact",
                          "table:%s" % lt["gyro"], "tick_max=%d/%d" % (lt["tick_max"], lt["n"])))
        items.append(feat("F-C.T_turn_lt90", lt["t_turn_ms"], "ms", "exact",
                          "table:%s" % lt["gyro"], ""))
        items.append(feat("F-C.b_est_lt90", lt["b_est_cm"], "cm", "derived",
                          "table:%s / %s" % (lt["v_outer"], lt["v_inner"]),
                          "integral |v_out-v_in| dt / theta"))
    if rt:
        items.append(feat("F-C.theta_rt90", rt["theta_tab"], "deg", "exact",
                          "table:%s" % rt["gyro"], "tick_max=%d/%d" % (rt["tick_max"], rt["n"])))
        items.append(feat("F-C.delta_rt90", rt["delta_theta"], "deg", "exact",
                          "table:%s" % rt["gyro"], "over-rotation written into the table"))
    if lt and rt:
        items.append(feat("F-C.asym_90", r2(rt["theta_tab"] - lt["theta_tab"], 2),
                          "deg", "exact", "LT vs RT",
                          "left and right windows differ by 4 ms by construction"))
    if h180:
        items.append(feat("F-C.theta_180", h180["theta_tab"], "deg", "exact",
                          "table:%s" % h180["gyro"], "over-rotation written into the table"))
        items.append(feat("F-C.delta_180", h180["delta_theta"], "deg", "exact",
                          "table:%s" % h180["gyro"], ""))
        items.append(feat("F-C.b_est_180", h180["b_est_cm"], "cm", "derived",
                          "table:%s" % h180["gyro"], "independent check of b_est"))

    rows = [a for a in tir["actions"] if not a["empty"]]
    return family("F-C", "turn geometry",
                  "how far does each turn really rotate?", items), rows


def residual(tir):
    """Hard-stop actions play only part of the table; the rest is P-settled."""
    out = []
    for a in tir["actions"]:
        if a["kind"] == "hard_stop" and not a["empty"]:
            out.append({
                "action": a["id"],
                "played_deg": a["theta_tab"],
                "residual_deg": a["residual_deg"],
                "window_ms": a["tick_max"],
                "table_n": a["n"],
            })
    return out
