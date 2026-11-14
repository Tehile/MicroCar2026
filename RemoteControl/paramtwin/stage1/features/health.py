"""F-E table integrity: windows, overruns, residual, duplicates."""

from .base import family, feat, r2


def _max_step(vals):
    if len(vals) < 2:
        return 0
    return max(abs(b - a) for a, b in zip(vals, vals[1:]))


def health(tir):
    tmap = {t["name"]: t for t in tir["tables"]}
    acts = [a for a in tir["actions"] if not a["empty"] and a["n"] > 1]
    rows = []
    for a in acts:
        first = tmap.get(a["gyro"], {}).get("first", 0)
        rows.append({
            "action": a["id"],
            "n": a["n"],
            "tick_max": a["tick_max"],
            "window_ratio": r2(a["tick_max"] / a["n"], 3) if a["n"] else None,
            "overrun": a["overrun"],
            "head_offset": r2(abs(abs(first) - a["nominal_deg"]), 2),
            "residual_deg": a["residual_deg"],
            "delta_theta": a["delta_theta"],
        })

    by_sha = {}
    for t in tir["tables"]:
        if t["n"] < 2:
            continue
        by_sha.setdefault(t["sha16"], []).append(t["name"])
    dup = [{"sha16": k, "tables": v} for k, v in by_sha.items() if len(v) > 1]

    worst_step = 0
    worst_table = ""
    for t in tir["tables"]:
        if t["n"] < 2:
            continue
        m = _max_step(t["vals"])
        if m > worst_step:
            worst_step, worst_table = m, t["name"]

    items = [
        feat("F-E.n_actions", len(acts), "1", "exact", "TableIR", "non-empty actions"),
        feat("F-E.n_overrun", sum(r["overrun"] > 0 for r in rows), "1", "exact",
             "TableIR", "actions reading past the array end"),
        feat("F-E.window_min", r2(min((r["window_ratio"] for r in rows), default=0), 3),
             "1", "exact", "TableIR", "smallest played fraction of a table"),
        feat("F-E.max_step", worst_step, "deg/ms", "exact", "table:%s" % worst_table,
             "largest sample-to-sample jump"),
        feat("F-E.n_duplicate_sets", len(dup), "1", "exact", "sha256 of table body",
             "byte-identical tables stored twice"),
    ]
    return family("F-E", "table integrity",
                  "which table is structurally broken?", items), rows, dup
