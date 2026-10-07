"""对角线配对的**安全闸回归测试**（`geometry.min_diag_sin`）。

扫描透视强度（tilt）与取景大小（fill），看"两两配对取相交"这一步的余量会不会坍塌，
并统计 `xdiag_plane` 在每组条件下选错内顶点的帧数。

用法::

    python tools/stress_vertex_pairing.py

预期：合法机位（含 tilt=0.60 的极端斜视）下**多解 0、无解 0、选错 0**，
最小 sin 远高于默认阈值 0.15；只有人为构造的"四点近共线"才会掉到 0.04 附近。
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
from aruco_locator.geometry import diagonal_pairs, select_inner_vertices  # noqa: E402

cfg = load_config(os.path.join(ROOT, "config.yaml"))
det = MarkerDetector(cfg.detection)
geo = cfg.geometry
CIDS = cfg.detection.corner_ids
dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, cfg.detection.dict_name))

print(f"{'tilt':>6s} {'fill':>6s} {'帧':>4s} {'配对余量中位':>12s} {'最小':>8s} "
      f"{"sin中位":>8s} {"sin最小":>9s} {'多解帧':>7s} {'无解帧':>7s} {'xdiag_plane 错':>14s}")
for tilt, fill in [(0.16, 0.70), (0.30, 0.70), (0.45, 0.70), (0.60, 0.70), (0.30, 0.40), (0.45, 0.35)]:
    args = argparse.Namespace(
        canvas=(1280, 960), fill=fill, cam_roll=35.0, tilt=tilt,
        mm_per_px=1.0, supersample=2, cam_supersample=2, ideal=False,
        blur=0.8, noise=4.0, gradient=0.06, rot_jitter=40.0, target_range=(0.18, 0.82),
    )
    rng = np.random.default_rng(20260922)
    margs, sins, n_multi, n_none, n_bad, n = [], [], 0, 0, 0, 0
    for case in range(80):
        plane, fq, info = ST.render_plane(rng, args, dictionary)
        img, Hm = ST.camera_view(plane, fq, rng, args)
        obs = det.detect(img)
        if len([i for i in CIDS if i in obs]) < 4:
            continue
        n += 1
        cts = np.array([obs[i].center for i in CIDS], dtype=np.float64)
        # 数一数有几种配对"相交"
        hit = 0
        for (a, b), (c, d) in (((0, 1), (2, 3)), ((0, 2), (1, 3)), ((0, 3), (1, 2))):
            from aruco_locator.geometry import _segment_intersect
            if _segment_intersect(cts[a], cts[b], cts[c], cts[d]) is not None:
                hit += 1
        if hit > 1:
            n_multi += 1
        got = diagonal_pairs(cts)
        if got is None:
            n_none += 1
            continue
        margs.append(got[2])
        sins.append(got[3])
        # 与"真值内顶点"对比（渲染器把内顶点放在场地角点上）
        H_img2plane = np.linalg.inv(Hm @ ST.mm_to_px_homography(info))
        fc = np.array([ST.CORNER_MM[c] for c in ST.CORNERS], dtype=np.float64)
        wrong = False
        sel, _ = select_inner_vertices(
            obs, dataclasses.replace(geo, vertex_mode="xdiag_plane"), CIDS
        )
        for mid in CIDS:
            cp = np.asarray(obs[mid].corners, dtype=np.float64).reshape(-1, 2)
            cp = cv2.perspectiveTransform(cp.reshape(-1, 1, 2), H_img2plane).reshape(-1, 2)
            d = np.min(np.linalg.norm(cp[:, None, :] - fc[None, :, :], axis=2), axis=1)
            if int(np.argmin(d)) != int(sel[mid]):
                wrong = True
        n_bad += int(wrong)
    if not margs:
        print(f"{tilt:>6.2f} {fill:>6.2f} {n:>4d}   全部无解")
        continue
    print(f"{tilt:>6.2f} {fill:>6.2f} {n:>4d} {np.median(margs):>12.3f} {min(margs):>8.3f} "
          f"{np.median(sins):>8.3f} {min(sins):>8.3f} {n_multi:>7d} {n_none:>7d} {n_bad:>14d}")
