"""F-F parameter-sheet structure: scopes, dead values, unit ambiguity."""

from collections import Counter

from .base import family, feat, r2
from ..ir import params as P


def struct(ir, tir=None):
    st = P.structure_stats(ir)
    by = P.by_name(ir)

    unknown_unit = [p["name"] for p in ir["params"] if not p["unit"]]
    groups = {}
    for p in ir["params"]:
        groups.setdefault(p["group"], set()).add(p["unit"])
    mixed = {g: sorted(u) for g, u in groups.items() if len(u) > 1}

    items = [
        feat("F-F.n_params", st["n_params"], "1", "exact", "ParamIR", ""),
        feat("F-F.n_phases", st["n_phases"], "1", "exact", "ParamIR", ""),
        feat("F-F.max_scopes", st["max_scopes"], "1", "exact", "ParamIR",
             "deepest assignment chain of a single parameter"),
        feat("F-F.n_multi_scope", st["n_multi_scope"], "1", "exact", "ParamIR",
             "parameters whose value depends on phase"),
        feat("F-F.n_runtime_overwrite", st["n_runtime_overwrite"], "1", "exact",
             "ParamIR", "overwritten outside init/menu"),
        feat("F-F.n_dead", st["n_dead"], "1", "exact", "ParamIR",
             "parameters with no writable assignment"),
        feat("F-F.n_unknown_unit", len(unknown_unit), "1", "derived", "unit hints",
             "no declared unit; cannot be dimensionally audited"),
        feat("F-F.n_mixed_groups", len(mixed), "1", "derived", "unit hints",
             "groups mixing mm and cm/s"),
    ]

    grade = []
    for name in ("turn_kp", "turn_kd", "speed_turn"):
        p = by.get(name)
        if not p:
            continue
        for phase, val in p["scopes"].items():
            grade.append({"param": name, "phase": phase, "value": val})

    return family("F-F", "parameter structure",
                  "where does this sheet hide its semantics?", items), \
        {"unknown_unit": unknown_unit, "mixed": mixed, "grade_chain": grade,
         "top_scopes": sorted(((p["name"], len(p["scopes"])) for p in ir["params"]),
                              key=lambda x: -x[1])[:8]}
