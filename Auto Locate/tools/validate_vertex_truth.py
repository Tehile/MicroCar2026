"""内顶点选择的**合成真值**回归测试 —— 唯一能真正证伪的判据。

原理：合成渲染器把每个角标记的内顶点**正好放在场地角点上**，
所以"离最近场地角点最近的检出角点"就是真值内顶点，不需要任何角度推理。

因此可以在**无实拍、无测绘**的情况下统计各 `vertex_mode` 的逐帧全对率。

用法::

    python tools/validate_vertex_truth.py -n 200 --rot-jitter 40 -v

预期（v1.1.0 默认档，`tilt=0.16 / cam_roll=±35°`）：`xdiag_plane` 100%，
`auto` 100%，`nearest` 约 93%，`xdiag`（图像空间原始点乘）约 84%。
任何一个默认判据掉下来都是回归，必须查。
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import sys

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import selftest_synthetic as ST  # noqa: E402
from aruco_locator.config import load_config  # noqa: E402
from aruco_locator.detector import MarkerDetector  # noqa: E402
from aruco_locator.geometry import apply_homography, select_inner_vertices  # noqa: E402

cfg = load_config(os.path.join(ROOT, "config.yaml"))
det = MarkerDetector(cfg.detection)
geo = cfg.geometry
CIDS = cfg.detection.corner_ids

p = argparse.ArgumentParser()
p.add_argument("-n", "--n", type=int, default=60)
p.add_argument("--seed", type=int, default=20260922)
p.add_argument("--rot-jitter", type=float, default=40.0)
p.add_argument("--tilt", type=float, default=0.16)
p.add_argument("--cam-roll", type=float, default=35.0)
p.add_argument("--verbose", "-v", action="store_true")
a = p.parse_args()

args = argparse.Namespace(
    canvas=(1280, 960), fill=0.70, cam_roll=a.cam_roll, tilt=a.tilt,
    mm_per_px=1.0, supersample=2, cam_supersample=2, ideal=False,
    blur=0.8, noise=4.0, gradient=0.06, rot_jitter=a.rot_jitter,
    target_range=(0.18, 0.82),
)
dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, cfg.detection.dict_name))
rng = np.random.default_rng(a.seed)

MODES = ["nearest", "xdiag", "xdiag_unit", "xdiag_plane", "auto"]
hits = {m: 0 for m in MODES}
tot = 0
percase = {m: [] for m in MODES}
bad_examples = []

for case in range(a.n):
    plane, field_quad_px, info = ST.render_plane(rng, args, dictionary)
    img, Hm = ST.camera_view(plane, field_quad_px, rng, args)
    obs = det.detect(img)
    if len([i for i in CIDS if i in obs]) < 4:
        continue
    H_img2plane = np.linalg.inv(Hm @ ST.mm_to_px_homography(info))
    # 真值：渲染器把每个标记的内顶点正好放在场地角点上
    fc = np.array([ST.CORNER_MM[c] for c in ST.CORNERS], dtype=np.float64)

    truth = {}
    for mid in CIDS:
        cp = apply_homography(H_img2plane, obs[mid].corners)
        # 各角点到最近场地角点的距离（mm）
        d = np.min(np.linalg.norm(cp[:, None, :] - fc[None, :, :], axis=2), axis=1)
        truth[mid] = int(np.argmin(d))
        if d.min() > 3.0:
            truth[mid] = None  # 真值不明确（不该发生）
    if any(v is None for v in truth.values()):
        continue

    tot += 1
    row = {}
    for m in MODES:
        g = dataclasses.replace(geo, vertex_mode=m)
        sel, _ = select_inner_vertices(obs, g, CIDS)
        got = {k: int(sel[k]) for k in sorted(sel)}
        ok = all(got[k] == truth[k] for k in sorted(truth))
        row[m] = ok
        hits[m] += int(ok)
        if not ok:
            percase[m].append(case)
    if a.verbose and not row["xdiag_unit"]:
        wrong = [k for k in sorted(truth) if True]
        # 打印出错的标记
        g = dataclasses.replace(geo, vertex_mode="xdiag_unit")
        sel, _ = select_inner_vertices(obs, g, CIDS)
        det_bad = {k: (truth[k], int(sel[k])) for k in sorted(truth) if int(sel[k]) != truth[k]}
        bad_examples.append((case, det_bad))

print(f"\n合成真值判定（{tot} 帧四角齐全，rot_jitter=±{a.rot_jitter:.0f}°, "
      f"tilt={a.tilt}, cam_roll=±{a.cam_roll:.0f}°）")
print(f"{'模式':>12s} {'全对帧':>8s} {'正确率':>8s} {'错误帧号'}")
for m in MODES:
    rate = 100.0 * hits[m] / max(tot, 1)
    bad = percase[m]
    txt = "" if not bad else ", ".join(str(c) for c in bad[:12]) + ("..." if len(bad) > 12 else "")
    print(f"{m:>12s} {hits[m]:>8d} {rate:>7.1f}% {txt}")
if bad_examples:
    print("\n出错示例（真值顶点 → xdiag_unit 选的顶点）：")
    for case, d in bad_examples[:6]:
        print(f"  case {case}: {d}")
