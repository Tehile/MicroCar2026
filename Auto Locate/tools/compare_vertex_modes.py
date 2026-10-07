"""对比不同 `vertex_mode` 在实拍数据上的表现 —— 带**外部绝对参照**。

为什么需要外部参照
------------------
内建的自洽性指标（方正度、跨机位重复性、单应畸变）**抓不到系统性错误**：
同一机位连拍会稳定地错在同一个地方，帧间一致性根本看不出来。
2026-10-03 那次内顶点误选事故，所有内部指标全部正常。

所以这里用一个与算法完全无关的物理参照：**场地边界红胶带**。
正确解应当让"模型正方形"的四条边同样贴合胶带内沿；锚定偏了，某一边就会明显错位。
判据 = 四边偏移的**极差**（mm），越小越准。

用法::

    python tools/compare_vertex_modes.py                       # 默认 xdiag_plane / nearest / auto
    python tools/compare_vertex_modes.py xdiag_plane auto      # 任意指定（第一个为基准）
    python tools/compare_vertex_modes.py xdiag_plane nearest "samples/IMG_20260925*.jpg"

输出：逐帧的顶点选择、边界贴合极差、相对基准的位置/角度差，以及汇总中位数。
"""
from __future__ import annotations

import dataclasses
import glob
import os
import sys

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aruco_locator import create_locator  # noqa: E402
from aruco_locator.detector import MarkerDetector  # noqa: E402
from aruco_locator.geometry import build_frame, select_inner_vertices  # noqa: E402

FIELD_MM = 1440.0   # 场地边长；只影响绝对毫米换算，归一化结果与它无关
SIDE = 1600         # 正射图边长（像素）


def boundary_spread(im, obs, geo, cnt) -> float:
    """模型正方形到最外圈红胶带的四边偏移极差（mm）。越小说明锚定越准。"""
    fo, _ = build_frame(cnt, obs, im.shape, geo)
    if fo is None:
        return float("nan")
    S = np.array([[SIDE, 0, 0], [0, SIDE, 0], [0, 0, 1]], float) @ fo.H_img2field
    warp = cv2.warpPerspective(im, S, (SIDE, SIDE), flags=cv2.INTER_AREA)
    hsv = cv2.cvtColor(warp, cv2.COLOR_BGR2HSV)
    red = cv2.bitwise_or(cv2.inRange(hsv, (0, 95, 70), (10, 255, 255)),
                         cv2.inRange(hsv, (170, 95, 70), (180, 255, 255)))
    band = int(SIDE * 0.09)
    offs = []
    for axis, sign in (("v", +1), ("v", -1), ("h", +1), ("h", -1)):
        a = []
        for t in np.linspace(0.18, 0.82, 9):
            i = int((1 - t if sign < 0 else t) * SIDE)
            line = red[i, :] if axis == "h" else red[:, i]
            idx = np.nonzero(line)[0]
            if len(idx) == 0:
                continue
            j = idx[idx < band] if sign > 0 else idx[idx > SIDE - band]
            if len(j) == 0:
                continue
            a.append(float(j.max() + 1) / SIDE if sign > 0 else float(j.min()) / SIDE)
        if len(a) >= 3:
            m = float(np.mean(a))
            offs.append(m if sign > 0 else 1 - m)
    if len(offs) < 4:
        return float("nan")
    return float((max(offs) - min(offs)) * FIELD_MM)


def run(pattern: str, modes: list[str]) -> None:
    loc = create_locator(os.path.join(ROOT, "config.yaml"))
    det = MarkerDetector(loc.cfg.detection)
    cids = loc.cfg.detection.corner_ids
    base = modes[0]
    locs = {m: create_locator(os.path.join(ROOT, "config.yaml"),
                              params={"geometry.vertex_mode": m}) for m in modes}

    files = sorted(glob.glob(os.path.join(ROOT, pattern)))
    if not files:
        print(f"（{pattern} 无匹配文件）")
        return
    print(f"\n{'=' * 100}\n{pattern}  共 {len(files)} 帧   基准 = {base}\n{'=' * 100}")
    hdr = (f"{'file':>14s} | " + " ".join(f"{m:>12s}" for m in modes)
           + " | " + " ".join(f"{'边界' + m[:6]:>10s}" for m in modes)
           + f" | {'Δ位置mm':>8s} {'Δ角度°':>7s}")
    print(hdr)
    print("-" * len(hdr))

    spread = {m: [] for m in modes}
    dpos, dth = [], []
    picks = {m: 0 for m in modes}
    for fp in files:
        im = cv2.imdecode(np.fromfile(fp, dtype=np.uint8), cv2.IMREAD_COLOR)
        obs = det.detect(im)
        if len([i for i in cids if i in obs]) < 4 or 0 not in obs:
            print(f"{os.path.basename(fp)[-14:]:>14s} | (缺标记，跳过)")
            continue
        inbound = {i: obs[i] for i in cids if i in obs}
        sel, sp = {}, {}
        for m in modes:
            g = dataclasses.replace(loc.cfg.geometry, vertex_mode=m)
            cnt, _ = select_inner_vertices(inbound, g, cids)
            sel[m] = None if cnt is None else {k: int(cnt[k]) for k in sorted(cnt)}
            if cnt is None:
                sp[m] = float("nan")
                continue
            picks[m] += int(sel[m] != sel[base])
            sp[m] = boundary_spread(im, inbound, g, cnt)
            spread[m].append(sp[m])

        rb = locs[base].locate(im)
        if not rb.valid:
            print(f"{os.path.basename(fp)[-14:]:>14s} | 基准无效，跳过")
            continue
        dp_this = []
        for m in modes[1:]:
            rm = locs[m].locate(im)
            if not rm.valid:
                dp_this.append(float("nan"))
                continue
            dp_this.append(float(np.hypot(rb.x - rm.x, rb.y - rm.y)) * FIELD_MM)
        if dp_this:
            dpos.append(dp_this[0])
            rm0 = locs[modes[1]].locate(im)
            if rm0.valid:
                dth.append(abs((rb.theta_deg - rm0.theta_deg + 180) % 360 - 180))

        nm = os.path.basename(fp).replace(".jpg", "").replace("IMG_2026", "")[-12:]
        print(f"{nm:>14s} | " + " ".join(f"{str(tuple(sel[m].values())):>12s}" for m in modes)
              + " | " + " ".join(f"{sp[m]:>10.1f}" for m in modes)
              + (f" | {dp_this[0]:>8.2f}" if dp_this else " |        -"))

    print("-" * len(hdr))
    for m in modes:
        s = np.asarray(spread[m], dtype=float)
        s = s[np.isfinite(s)]
        if len(s) == 0:
            continue
        tag = "（基准）" if m == base else f"（{picks[m]} 帧与基准选择不同）"
        print(f"  边界贴合极差 {m:<12s} 中位 {np.median(s):6.1f} mm   "
              f"均值 {s.mean():6.1f} mm   max {s.max():6.1f} mm  {tag}")
    if dpos:
        print(f"  相对基准位置差 中位 {np.median(dpos):.2f} mm   max {max(dpos):.2f} mm")
    if dth:
        print(f"  相对基准角度差 中位 {np.median(dth):.2f}°   max {max(dth):.2f}°")
    print("  判读：边界贴合极差越小越准；数值接近说明该模式与基准等价。")


if __name__ == "__main__":
    argv = sys.argv[1:]
    pats = [a for a in argv if "*" in a or a.endswith((".jpg", ".png")) or os.sep in a]
    mods = [a for a in argv if a not in pats]
    if not mods:
        mods = ["xdiag_plane", "nearest", "auto"]
    if not pats:
        pats = ["samples/IMG_20261003*.jpg"]
    for pt in pats:
        run(pt, mods)
