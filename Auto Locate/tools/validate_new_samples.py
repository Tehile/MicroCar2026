"""在新补充的 samples 数据上验证定位算法。

用法：
    python tools/validate_new_samples.py [glob] [--tag NAME]

输出：
    out/validate_<tag>.csv        每帧一行的扁平结果
    out/validate_<tag>.json       完整（含 diagnostics）
    out/validate_<tag>_overlay/   标注图
运行日志打印汇总统计。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from aruco_locator import create_locator  # noqa: E402
from aruco_locator.sources import imread_unicode  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pattern", nargs="?", default="samples/IMG_20261003*.jpg")
    ap.add_argument("--tag", default=None)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--overlay", action="store_true", help="输出标注图")
    a = ap.parse_args()

    files = sorted(glob.glob(os.path.join(ROOT, a.pattern)))
    if not files:
        print("没有匹配的图片:", a.pattern)
        return 2
    tag = a.tag or os.path.splitext(os.path.basename(files[0]))[0].split("_")[0]
    out_dir = os.path.join(ROOT, "out")
    os.makedirs(out_dir, exist_ok=True)

    cfg_path = os.path.join(ROOT, a.config)
    locator = create_locator(cfg_path if os.path.exists(cfg_path) else None)

    ov_dir = os.path.join(out_dir, f"validate_{tag}_overlay")
    if a.overlay:
        os.makedirs(ov_dir, exist_ok=True)

    rows, full = [], []
    t_all = time.perf_counter()
    for i, fp in enumerate(files):
        frame = imread_unicode(fp)
        t0 = time.perf_counter()
        loc = locator.locate(frame, frame_index=i, timestamp=0.0)
        dt = (time.perf_counter() - t0) * 1e3
        name = os.path.basename(fp)
        d = loc.to_dict()
        d["file"] = name
        full.append(d)
        rows.append(
            [
                name,
                int(loc.valid),
                f"{loc.x:.6f}" if loc.x is not None else "",
                f"{loc.y:.6f}" if loc.y is not None else "",
                f"{loc.theta_deg:.3f}" if loc.theta_deg is not None else "",
                f"{loc.rms_field:.5f}" if loc.rms_field is not None else "",
                f"{loc.target_rms:.5f}" if loc.target_rms is not None else "",
                f"{dt:.0f}",
                ";".join(str(i) for i in loc.corner_ids),
                "|".join(loc.flags),
                loc.reason,
                f'{frame.shape[1]}x{frame.shape[0]}',
            ]
        )
        if a.overlay and loc.valid:
            import cv2

            vis = locator.annotate(frame, loc)
            cv2.imencode(".jpg", vis, [int(cv2.IMWRITE_JPEG_QUALITY), 85])[1].tofile(
                os.path.join(ov_dir, name)
            )
    total = time.perf_counter() - t_all

    header = [
        "file", "valid", "x", "y", "theta_deg", "rms_field", "target_rms",
        "ms", "corner_ids", "flags", "reason", "size",
    ]
    csv_path = os.path.join(out_dir, f"validate_{tag}.csv")
    with open(csv_path, "w", encoding="utf-8") as fp:
        fp.write(",".join(header) + "\n")
        for r in rows:
            fp.write(",".join(str(c) for c in r) + "\n")
    json_path = os.path.join(out_dir, f"validate_{tag}.json")
    with open(json_path, "w", encoding="utf-8") as fp:
        json.dump(full, fp, ensure_ascii=False, indent=1)

    # ---- 汇总 ----
    n = len(rows)
    ok = [r for r in rows if r[1]]
    print(f"== {tag}: {n} 帧, 有效 {len(ok)} ({100*len(ok)/n:.1f}%) ==")
    reasons = {}
    flags = {}
    for r in rows:
        if not r[1]:
            reasons[r[10]] = reasons.get(r[10], 0) + 1
        for f in filter(None, r[9].split("|")):
            flags[f] = flags.get(f, 0) + 1
    if reasons:
        print("  无效原因:", reasons)
    if flags:
        print("  flags:", flags)
    if ok:
        rms = [float(r[5]) for r in ok if r[5]]
        trms = [float(r[6]) for r in ok if r[6]]
        ms = [float(r[7]) for r in ok]
        print(f"  corner_square_rms  max={max(rms):.5f} mean={np.mean(rms):.5f}")
        print(f"  target_rms         max={max(trms):.5f} mean={np.mean(trms):.5f}")
        print(f"  单帧耗时 mean={np.mean(ms):.0f}ms  总 {total:.1f}s")
    print("写出:", os.path.relpath(csv_path, ROOT), "|", os.path.relpath(json_path, ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
