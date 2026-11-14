"""Render the single-file report. Everything is inlined; no network at runtime."""

import json
import os
import sys
from datetime import datetime

from .. import SPEC_VERSION
from ..model import dynamics, playback, profile

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "template.html")
ASSETS = os.path.join(HERE, "assets")

CHANNELS = ("t_ms", "theta_deg", "omega_dps", "x_mm", "y_mm")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _playback_payload(tir):
    """Every non-empty action. The turn view is a data browser, so coverage
    beats payload size: 21 actions / 4458 samples / ~120 KB inline."""
    out = {}
    acts = {}
    for a in tir["actions"]:
        acts[a["id"]] = a
        if a["empty"] or a["n"] < 2:
            continue
        try:
            r = playback.run(tir, a["id"])
        except KeyError:
            continue
        if not r.get("channels"):
            continue
        ch = {k: r["channels"][k] for k in CHANNELS if k in r["channels"]}
        out[a["id"]] = {
            "action": r["action"],
            "kind": r["kind"],
            "nominal_deg": r["nominal_deg"],
            "tick_max": r["tick_max"],
            "delta_deg": a["delta_theta"],
            "table_tail": a["residual_deg"],
            "overrun": a["overrun"],
            "channels": ch,
            "scalars": r["scalars"],
        }
    return out


def _grades(ir):
    out = {}
    for g in ("LOW", "LOW_90", "BACK"):
        p = profile.grade_params(ir, g)
        out[g] = {k: p.get(k) for k in ("top", "bottom", "ratio", "dec")}
        out[g]["complete"] = p.get("complete", False)
    return out


def collect(ir, tir, feat, v_set_grid=(60, 80, 100, 140, 180, 240, 320, 400)):
    from ..ir import tables as T
    summary = T.summary(tir)
    summary.update({
        "n_params": len(ir["params"]),
        "n_phases": len(ir["phases"]),
        "n_multi_scope": sum(1 for p in ir["params"] if len(p["scopes"]) > 1),
        "n_runtime_overwrite": sum(1 for p in ir["params"]
                                   if "runtime-overwrite" in p["risks"]),
    })
    return {
        "meta": {
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "spec_version": SPEC_VERSION,
            "params_sha": ir["source"]["sha256"],
            "params_path": ir["source"]["path"],
            "dis_list": [180, 254, 360],
            "v_set_grid": list(v_set_grid),
        },
        "summary": summary,
        "checks": feat["checks"],
        "families": feat["families"],
        "tables": {
            "health": feat["tables"]["health"],
            "duplicates": feat["tables"]["duplicates"],
            "residual": feat["tables"]["residual"],
            "timing": feat["tables"]["timing"],
        },
        "params": [{
            "name": p["name"], "group": p["group"], "unit": p["unit"],
            "active": {"phase": p["active"]["phase"], "value": p["active"]["value"]},
            "runtime": p["runtime"],
            "scopes": p["scopes"],
            "risks": p["risks"],
        } for p in ir["params"]],
        "grades": _grades(ir),
        "m2": dynamics.constants(ir),
        "playback": _playback_payload(tir),
        "warnings": feat["warnings"],
    }


def render(data, out_path):
    tpl = _read(TEMPLATE)
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    html = (tpl
            .replace("__DATA__", blob.replace("</", "<\\/"))
            .replace("__MODEL_JS__", _read(os.path.join(ASSETS, "model.js")))
            .replace("__PLOT_JS__", _read(os.path.join(ASSETS, "plot.js")))
            .replace("__APP_JS__", _read(os.path.join(ASSETS, "app.js"))))
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return out_path, len(html.encode("utf-8"))


def build(ir, tir, feat, out_dir):
    data = collect(ir, tir, feat)
    os.makedirs(out_dir, exist_ok=True)
    path, size = render(data, os.path.join(out_dir, "report.html"))
    return path, size, data
