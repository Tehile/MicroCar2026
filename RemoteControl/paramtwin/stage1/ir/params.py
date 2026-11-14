"""ParamIR: turn `params.competition.v3.json` into an assignment-event graph.

The JSON already carries the per-phase assignment history. This module does not
re-parse C; it normalises the JSON and adds the three things stage1 needs:

  * `scopes`  : phase -> value, the whole assignment history
  * `active`  : the value that is live at runtime for the competition profile
  * `risks`   : machine-readable flags consumed by feature family F-F
"""

import hashlib
import json
import os

SCHEMA_VERSION = "1.0"

UNIT_HINTS = {
    "LTopSpeed": "cm/s", "LBottomSpeed": "cm/s",
    "L45TopSpeed": "cm/s", "L45BottomSpeed": "cm/s",
    "L90TopSpeed": "cm/s", "L90BottomSpeed": "cm/s",
    "BackTopSpeed": "cm/s", "BackBottomSpeed": "cm/s",
    "ObjectGoTo_TopSpeed": "cm/s", "ObjectGoTo_BottomSpeed": "cm/s",
    "speed_turn": "cm/s", "speed_ss": "cm/s", "speed_straight": "cm/s",
    "SpurtAccRatio45": "1/mm", "SpurtDecRatio45": "1/mm",
    "SpurtAccRatio90": "1/mm", "SpurtDecRatio90": "1/mm",
    "SpeedRatioL": "pulse/ms per cm/s", "SpeedRatioR": "pulse/ms per cm/s",
    "turn_delay": "mm", "beforeturntrack": "mm",
    "track": "mm", "dis": "mm", "dis_mm": "mm",
    "turn_kp": "1", "turn_kd": "1", "turn_p": "1", "turn_d": "1",
}


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _phase_kind(pid):
    if pid in ("init", "menu", "boot"):
        return "boot"
    return "runtime"


def load(path):
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    phase_order = []
    for p in raw["params"]:
        for s in p["sets"]:
            if s["phase"] not in phase_order:
                phase_order.append(s["phase"])
    phase_order.sort(key=lambda x: (0 if x in ("init", "menu", "boot") else 1, x))

    out_params = []
    for p in raw["params"]:
        sets = p.get("sets", [])
        scopes = {}
        for s in sets:
            if s.get("value") is not None:
                scopes[s["phase"]] = s["value"]

        active_phase = p.get("default_phase")
        active = None
        for s in sets:
            if s["phase"] == active_phase:
                active = s
                break
        if active is None and sets:
            active = sets[0]
            active_phase = active["phase"]

        risks = []
        if len(scopes) > 1:
            risks.append("multi-scope")
        if any(s.get("kind") == "assign" and _phase_kind(s["phase"]) == "runtime"
               for s in sets):
            risks.append("runtime-overwrite")
        if sets and not any(s.get("writable", True) for s in sets):
            risks.append("dead")

        # `default_phase` is the declaration site, not what runs in a match.
        # The competition profile executes main(); the last `main#N` assignment
        # is the one in force when the maze starts.
        main_sets = [s for s in sets
                     if str(s.get("phase", "")).startswith("main")
                     and s.get("value") is not None]
        runtime = None
        if main_sets:
            rs = main_sets[-1]
            runtime = {"phase": rs["phase"], "value": rs["value"],
                       "expr": rs.get("expr", ""),
                       "source": "%s:%s" % (rs.get("file", "?"), rs.get("line", "?"))}

        out_params.append({
            "name": p["name"],
            "group": p.get("group", ""),
            "note": p.get("note", ""),
            "unit": UNIT_HINTS.get(p["name"], ""),
            "ctype": (active or {}).get("ctype", ""),
            "scopes": scopes,
            "active": {
                "phase": active_phase,
                "value": p.get("default_value", (active or {}).get("value")),
                "expr": p.get("default_expr", (active or {}).get("expr")),
                "source": "%s:%s" % ((active or {}).get("file", "?"),
                                     (active or {}).get("line", "?")),
            },
            "derived": p.get("derived", []),
            "dropped": p.get("dropped", []),
            "runtime": runtime,
            "risks": risks,
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "profile": raw.get("profile", ""),
        "source": {
            "path": os.path.relpath(path).replace("\\", "/"),
            "sha256": _sha256(path),
            "generated": raw.get("generated", ""),
        },
        "phases": [{"id": pid, "kind": _phase_kind(pid)} for pid in phase_order],
        "params": out_params,
    }


def by_name(ir):
    return {p["name"]: p for p in ir["params"]}


def value(ir, name, default=None):
    """Runtime value for the competition profile (main#N beats init)."""
    p = by_name(ir).get(name)
    if p is None:
        return default
    if p.get("runtime"):
        return p["runtime"]["value"]
    v = p["active"]["value"]
    return default if v is None else v


def phase_value(ir, name, phase, default=None):
    p = by_name(ir).get(name)
    if p is None:
        return default
    return p["scopes"].get(phase, default)


def structure_stats(ir):
    n_sets = [len(p["scopes"]) for p in ir["params"]]
    return {
        "n_params": len(ir["params"]),
        "n_phases": len(ir["phases"]),
        "max_scopes": max(n_sets) if n_sets else 0,
        "n_multi_scope": sum(1 for n in n_sets if n > 1),
        "n_dead": sum(1 for p in ir["params"] if "dead" in p["risks"]),
        "n_runtime_overwrite": sum(1 for p in ir["params"]
                                   if "runtime-overwrite" in p["risks"]),
    }
