"""内顶点选择各模式的耗时对比（交错采样，避免排序偏置）。"""
from __future__ import annotations

import dataclasses
import os
import statistics
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aruco_locator.config import load_config  # noqa: E402
from aruco_locator.detector import MarkerDetector  # noqa: E402
from aruco_locator.geometry import select_inner_vertices  # noqa: E402
from aruco_locator.sources import imread_unicode  # noqa: E402

cfg = load_config(os.path.join(ROOT, "config.yaml"))
det = MarkerDetector(cfg.detection)
geo = cfg.geometry
CIDS = cfg.detection.corner_ids

MODES = ["nearest", "xdiag", "xdiag_unit", "xdiag_plane", "auto", "search", "full"]
imgs = [imread_unicode(p) for p in (
    sorted(__import__("glob").glob(os.path.join(ROOT, "samples", "IMG_20261003_*.jpg")))[:4]
)]
obss = [det.detect(im) for im in imgs]
obss = [o for o in obss if len([i for i in CIDS if i in o]) == 4]
print(f"用 {len(obss)} 帧真实检测结果，每模式 300 次，交错采样")

cfgs = {m: dataclasses.replace(geo, vertex_mode=m) for m in MODES}
# 预热
for m in MODES:
    for o in obss:
        select_inner_vertices(o, cfgs[m], CIDS)

times: dict[str, list[float]] = {m: [] for m in MODES}
for _ in range(300):
    for i, o in enumerate(obss):
        for m in MODES:
            t0 = time.perf_counter()
            select_inner_vertices(o, cfgs[m], CIDS)
            times[m].append((time.perf_counter() - t0) * 1e3)

print(f"\n{'模式':>14s} {'中位(ms)':>10s} {'均值(ms)':>10s} {'P95(ms)':>9s} {'相对 nearest':>14s}")
base = statistics.median(times["nearest"])
for m in MODES:
    v = sorted(times[m])
    med = statistics.median(v)
    print(f"{m:>14s} {med:>10.3f} {statistics.mean(v):>10.3f} {v[int(0.95*len(v))]:>9.3f} "
          f"{med/base:>13.2f}x")
