"""Golden snapshot: the numbers every refactor must not move.

    python tests/gen_golden.py            # rewrite tests/golden/stage1_golden.json
    python tests/gen_golden.py --diff     # print the current diff, exit 1 if any

The snapshot deliberately stores *rounded* values (the same rounding the IR and
features already apply), so it catches real drift, not float noise in the 3rd
decimal. tests/test_stage1.py compares against it on every run.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from stage1 import features                                  # noqa: E402
from stage1.ir import params as P                            # noqa: E402
from stage1.ir import tables as T                            # noqa: E402
from stage1.model import dynamics, playback, profile         # noqa: E402

GOLDEN = os.path.join(ROOT, "tests", "golden", "stage1_golden.json")
PARAMS = os.path.join(ROOT, "..", "tools", "params",
                      "params.competition.v3.json")
SRC = os.path.join(ROOT, "..", "code_competition")

TOL = 1e-6


def snapshot():
    ir = P.load(PARAMS)
    tir = T.load(SRC)
    feat = features.extract(ir, tir)

    actions = {}
    for a in tir["actions"]:
        actions[a["id"]] = {
            "n": a["n"], "tick_max": a["tick_max"],
            "theta_tab": a["theta_tab"], "delta_theta": a["delta_theta"],
            "b_est_cm": a["b_est_cm"], "overrun": a["overrun"],
        }

    play = {}
    for aid in ("LT90SEARCH", "RT90SEARCH", "L180L", "TURNAROUND"):
        try:
            s = playback.run(tir, aid)["scalars"]
        except KeyError:
            continue
        play[aid] = {k: s[k] for k in ("theta_deg", "t_ms", "omega_avg_dps",
                                       "path_mm", "b_est_cm")}

    prof = {}
    for grade in ("LOW", "LOW_90", "BACK"):
        gp = profile.grade_params(ir, grade)
        for dis in (180, 254, 360):
            a = profile.analyse(dis, gp)
            prof["%s@%d" % (grade, dis)] = {k: a[k] for k in
                                            ("T_total_ms", "v_avg", "v_peak",
                                             "v_jump", "clamp_ratio",
                                             "s_switch_mm")}

    m2 = {}
    for dis in (180, 360):
        m2[str(dis)] = dynamics.run(ir, profile, dis=dis)["scalars"]

    return {
        "spec": "stage1-1.0",
        "actions": actions,
        "playback": play,
        "profile": prof,
        "m2": m2,
        "features": features.flat(feat),
        "checks": feat["checks"],
        "warnings": sorted(w["code"] for w in feat["warnings"]),
    }


def diff(a, b, path="", out=None):
    out = [] if out is None else out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append("%s.%s only in golden" % (path, k))
            elif k not in b:
                out.append("%s.%s missing" % (path, k))
            else:
                diff(a[k], b[k], "%s.%s" % (path, k), out)
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        if abs(a - b) > TOL:
            out.append("%s: golden %r vs now %r" % (path, a, b))
    elif a != b:
        out.append("%s: golden %r vs now %r" % (path, a, b))
    return out


def main(argv):
    snap = snapshot()
    if "--diff" in argv:
        with open(GOLDEN, encoding="utf-8") as fh:
            old = json.load(fh)
        d = diff(old, snap)
        print("\n".join(d) if d else "golden matches")
        return 1 if d else 0
    os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
    with open(GOLDEN, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, ensure_ascii=False, indent=1, sort_keys=True)
    print("wrote %s (%d actions, %d features)"
          % (GOLDEN, len(snap["actions"]), len(snap["features"])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
