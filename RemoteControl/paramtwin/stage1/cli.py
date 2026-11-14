"""stage1 CLI.

    python -m stage1.cli all --params ../tools/params/params.competition.v3.json \
                             --src ../code_competition --out out

Writes only inside --out (default: ./out). Nothing under code_competition/,
host/ or tools/ is ever touched.
"""

import argparse
import json
import os
import sys

from . import SPEC_VERSION, features
from .ir import params as P
from .ir import tables as T
from .model import dynamics, playback, profile
from .report import build as report_build

DEFAULTS = {
    "params": "../tools/params/params.competition.v3.json",
    "src": "../code_competition",
    "out": "out",
}


def _dump(obj, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    return os.path.getsize(path)


def cmd_extract(args):
    ir = P.load(args.params)
    tir = T.load(args.src)
    n1 = _dump(ir, os.path.join(args.out, "paramir.json"))
    slim = {k: v for k, v in tir.items() if k != "tables"}
    slim["tables"] = [{k: v for k, v in t.items() if k != "vals"}
                      for t in tir["tables"]]
    n2 = _dump(slim, os.path.join(args.out, "tableir.json"))
    st = P.structure_stats(ir)
    sm = T.summary(tir)
    print("[extract] params %d / phases %d / multi-scope %d  -> paramir.json %.1f KB"
          % (st["n_params"], st["n_phases"], st["n_multi_scope"], n1 / 1024))
    print("[extract] tables %d / actions %d / overrun %d  -> tableir.json %.1f KB"
          % (sm["n_tables"], sm["n_actions"], sm["n_overrun"], n2 / 1024))
    return ir, tir


def cmd_features(args, ir=None, tir=None):
    ir = ir or P.load(args.params)
    tir = tir or T.load(args.src)
    feat = features.extract(ir, tir)
    n = sum(len(f["items"]) for f in feat["families"])
    size = _dump(feat, os.path.join(args.out, "features.json"))
    flat = features.flat(feat)
    print("[features] 6 families / %d items -> features.json %.1f KB" % (n, size / 1024))
    print("           v_set %.0f  v_jump %.1f  T_cell180 %.1f ms  b_est %.3f cm  asym %.1f deg"
          % (flat["F-A.v_set"], flat["F-A.v_jump"], flat["F-B.T_cell_180"],
             feat["checks"]["b_est_mean"] or 0, flat["F-C.asym_90"]))
    for w in feat["warnings"]:
        print("           ! %s %s" % (w["code"], w.get("detail", "")))
    return feat


def cmd_sim(args, ir=None, tir=None):
    ir = ir or P.load(args.params)
    tir = tir or T.load(args.src)
    out = args.out
    sweep = profile.sweep(ir, grade=args.grade)
    size = _dump({"grade": args.grade, "rows": sweep},
                 os.path.join(out, "traces", "sweep_%s.json" % args.grade))
    m2 = dynamics.run(ir, profile, dis=args.dis, grade=args.grade)
    m2.pop("channels", None)
    m2s = _dump({"grade": args.grade, "dis": args.dis, "scalars": m2["scalars"]},
                os.path.join(out, "traces", "m2_%s.json" % args.grade))
    play = {}
    for aid in ("LT90SEARCH", "RT90SEARCH", "L180L", "TURNAROUND"):
        try:
            r = playback.run(tir, aid)
        except KeyError:
            continue
        play[aid] = r["scalars"]
    _dump(play, os.path.join(out, "traces", "playback.json"))
    print("[sim] sweep %d rows (%.1f KB), m2 T=%.1f ms v_peak=%.1f"
          % (len(sweep), size / 1024, m2["scalars"]["T_total_ms"], m2["scalars"]["v_peak"]))
    print("      m2 assumes tau_m=%.1f ms, headroom=%.2f (see scalars.assumed)"
          % (m2["assumed"]["tau_m_ms"], m2["assumed"]["headroom"]))
    return sweep


def cmd_report(args, ir=None, tir=None, feat=None):
    ir = ir or P.load(args.params)
    tir = tir or T.load(args.src)
    feat = feat or features.extract(ir, tir)
    path, size, data = report_build.build(ir, tir, feat, args.out)
    print("[report] %s  %.1f KB  (playback actions: %s)"
          % (path, size / 1024, ", ".join(sorted(data["playback"]))))
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(prog="stage1", description=__doc__)
    ap.add_argument("--params", default=DEFAULTS["params"])
    ap.add_argument("--src", default=DEFAULTS["src"])
    ap.add_argument("--out", default=DEFAULTS["out"])
    ap.add_argument("--grade", default="LOW", choices=list(profile.GRADES))
    ap.add_argument("--dis", type=int, default=180)
    sub = ap.add_subparsers(dest="cmd")

    for name in ("extract", "features", "sim", "report", "all"):
        sub.add_parser(name)

    args = ap.parse_args(argv)
    args.params = os.path.normpath(args.params)
    args.src = os.path.normpath(args.src)
    args.out = os.path.normpath(args.out)

    if not os.path.exists(args.params):
        ap.error("params file not found: %s" % args.params)
    if not os.path.isdir(args.src):
        ap.error("source tree not found: %s" % args.src)

    print("stage1 %s  params=%s  src=%s  out=%s"
          % (SPEC_VERSION, args.params, args.src, args.out))

    if args.cmd == "extract":
        cmd_extract(args)
    elif args.cmd == "features":
        cmd_features(args)
    elif args.cmd == "sim":
        cmd_sim(args)
    elif args.cmd == "report":
        cmd_report(args)
    elif args.cmd == "all":
        ir, tir = cmd_extract(args)
        feat = cmd_features(args, ir, tir)
        cmd_sim(args, ir, tir)
        cmd_report(args, ir, tir, feat)
        print("done.")
    else:
        ap.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
