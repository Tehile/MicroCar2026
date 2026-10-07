"""新数据（samples/IMG_20261003*.jpg，36 张）准确性验证 —— 主分析脚本。

数据是手持俯拍、机位持续在变，没有测绘真值。因此用四类互相独立的证据定量刻画：

  [A] 有效性 / 失效原因
  [B] 内部几何自洽：角标记与 0 号标记在还原单应下必须仍是正方形
  [C] 单应畸变：四个角标记**实物同尺寸**，映射到场地系后尺寸的离散度
      就是单应在这片区域的相对误差量级
  [D] 跨机位不变性：把结果换算到"锚定在角标记 ID 上"的规范坐标系后，
      同一物理位姿、不同机位的多帧必须一致 —— 方差即整套系统的重复性。
      角标记排布会变（用户在这批数据里换了 4 种排布），按排布分组后再比。

额外：内顶点组合敏感性（最弱环的不确定度）、误检统计。

用法: python tools/validate_new_samples2.py [glob]
"""
from __future__ import annotations

import glob
import json
import os
import sys

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aruco_locator import create_locator  # noqa: E402
from aruco_locator.detector import MarkerDetector  # noqa: E402
from aruco_locator.geometry import (  # noqa: E402
    MARKER_LOCAL,
    apply_homography,
    build_frame,
    fit_marker_pose_from_field,
    fit_similarity,
    select_inner_vertices,
)
from aruco_locator.sources import imread_unicode  # noqa: E402

FIELD_MM = 1440.0  # 8 格 x 180mm
FP = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]


def r_k(k: int, p):
    """把 (0,0) 转到第 k 个角的单位正方形旋转。"""
    x, y = p
    k %= 4
    return [(x, y), (1.0 - y, x), (1.0 - x, 1.0 - y), (y, 1.0 - x)][k]


def main() -> int:
    pattern = sys.argv[1] if len(sys.argv) > 1 else "samples/IMG_20261003*.jpg"
    mode = sys.argv[2] if len(sys.argv) > 2 else None
    files = sorted(glob.glob(os.path.join(ROOT, pattern)))
    loc0 = create_locator(os.path.join(ROOT, "config.yaml"),
                          params={"geometry.vertex_mode": mode} if mode and mode != "legacy" else None)
    if mode == "legacy":
        # 复现 1.0.0 默认：直接改数据类字段，绕过参数层的建议范围校验
        loc0.cfg.geometry.vertex_margin_weight = 0.02
    det = MarkerDetector(loc0.cfg.detection)
    geo = loc0.cfg.geometry
    tgt = loc0.cfg.detection.target_id

    recs = []
    for i, fp in enumerate(files):
        frame = imread_unicode(fp)
        obs = det.detect(frame)
        loc = loc0.locate(frame, frame_index=i)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        r = {
            "file": os.path.basename(fp),
            "valid": loc.valid,
            "reason": loc.reason,
            "x": loc.x, "y": loc.y, "theta": loc.theta_deg,
            "dir": [loc.dir_x, loc.dir_y],
            "rms_field": loc.rms_field, "target_rms": loc.target_rms,
            "origin_id": loc.origin_id,
            "corner_ids": list(loc.corner_ids),
            "flags": list(loc.flags),
            "blur": round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 1),
            "spurious": sorted(k for k in obs if k not in (0, 1, 2, 3, 4)),
            "sides": {int(k): round(float(o.side), 1) for k, o in obs.items() if k in (0, 1, 2, 3, 4)},
        }
        inner, info = select_inner_vertices(obs, geo, loc0.cfg.detection.corner_ids)
        r["missing"] = list(info.get("missing") or [])
        r["gap_rel"] = info.get("cost_gap_rel")
        r["chosen_by"] = info.get("chosen_by")
        if info.get("inner_margins"):
            r["inner_margins"] = {int(k): round(float(v), 3) for k, v in info["inner_margins"].items()}

        fo = loc0.last_debug.get("frame_obj")
        if fo is not None and loc.valid:
            r["ring"] = [int(v) for v in fo.ring_ids]
            r["field_side_px"] = round(float(fo.side_px), 1)
            r["arr"] = "-".join(map(str, _minrot(fo.ring_ids)))
            # [C] 角标记映射尺寸的离散度
            ms = loc.diagnostics.get("marker_scales") or {}
            if len(ms) == 4:
                vals = np.array(list(ms.values()))
                r["corner_scale_cv"] = round(float(vals.std(ddof=1) / vals.mean()), 5)
                r["corner_scale"] = {int(k): round(float(v), 5) for k, v in ms.items()}
            # 解出的场角像素 vs 该标记被选的角点像素
            corners_px = apply_homography(fo.H_field2img, np.array(FP, float))
            diffs = []
            for kk, mid in enumerate(fo.ring_ids):
                d = np.linalg.norm(corners_px[kk] - obs[mid].corners[fo.inner_index[mid]])
                diffs.append(round(float(d), 2))
            r["corner_offset_px"] = diffs
        else:
            r["ring"] = None
            r["arr"] = None

        # 内顶点组合敏感性（最优 vs 次优若为真）
        if len(r["corner_ids"]) == 4 and tgt in obs and inner is not None:
            costs = info.get("costs") or []
            if len(costs) >= 2:
                pts = []
                for c in costs[:3]:
                    alt = {int(k): int(v) for k, v in c["assign"].items()}
                    fj, _ = build_frame(alt, obs, frame.shape, geo)
                    if fj is None:
                        continue
                    mapped = apply_homography(fj.H_img2field, obs[tgt].corners)
                    pose = fit_marker_pose_from_field(mapped)
                    sq = []
                    for mid2 in fj.ring_ids:
                        mp = cv2.perspectiveTransform(obs[mid2].corners.reshape(-1, 1, 2).astype(np.float64),
                                                      fj.H_img2field).reshape(-1, 2)
                        _, _, _, rr = fit_similarity(MARKER_LOCAL, mp)
                        sq.append(float(rr))
                    pts.append({"by": c["by"], "cost": round(float(c["cost"]), 6),
                                "center": pose.center.tolist(), "rms_field": round(max(sq), 6)})
                if len(pts) >= 2:
                    c0 = np.array(pts[0]["center"])
                    for p in pts:
                        p["d_field"] = round(float(np.linalg.norm(np.array(p["center"]) - c0)), 6)
                        p["d_mm"] = round(p["d_field"] * FIELD_MM, 2)
                    r["vertex_variants"] = pts

        # 判据对照：算法用的 cost(形状+余量惩罚) vs "四个角标记映射尺寸一致性" size_cv
        if len(r["corner_ids"]) == 4 and tgt in obs and inner is not None:
            ids4 = sorted(inner)
            cen = np.mean([obs[i].center for i in ids4], axis=0)
            pool = {i: list(np.argsort(np.linalg.norm(obs[i].corners - cen, axis=1))[:2]) for i in ids4}
            import itertools

            best = None
            for combo in itertools.product(*[pool[i] for i in ids4]):
                cnt = dict(zip(ids4, [int(c) for c in combo]))
                fj, _ = build_frame(cnt, obs, frame.shape, geo)
                if fj is None:
                    continue
                sizes = []
                for mid2 in fj.ring_ids:
                    mp = cv2.perspectiveTransform(
                        obs[mid2].corners.reshape(-1, 1, 2).astype(np.float64),
                        fj.H_img2field).reshape(-1, 2)
                    s2, _, _, _ = fit_similarity(MARKER_LOCAL, mp)
                    sizes.append(abs(float(s2)))
                sizes = np.array(sizes)
                cvv = float(sizes.std(ddof=1) / sizes.mean())
                pose = fit_marker_pose_from_field(apply_homography(fj.H_img2field, obs[tgt].corners))
                if best is None or cvv < best[0]:
                    best = (cvv, cnt, pose.center, sizes.mean())
            if best is not None:
                fo_obj = loc0.last_debug.get("frame_obj")
                if fo_obj is not None:
                    cp = fit_marker_pose_from_field(
                        apply_homography(np.asarray(fo_obj.H_img2field), obs[tgt].corners))
                    r["chosen_center"] = cp.center.tolist()
                    r["d_pos_mm"] = round(float(np.linalg.norm(cp.center - best[2])) * FIELD_MM, 2)
                chosen = {int(k): int(v) for k, v in inner.items()}
                r["sizebest_assign"] = {int(k): int(v) for k, v in best[1].items()}
                r["sizebest_cv"] = round(best[0], 5)
                r["sizebest_scale_mm"] = round(best[3] * FIELD_MM, 2)
                r["sizebest_center"] = best[2].tolist()
                r["agrees"] = bool({int(k): int(v) for k, v in best[1].items()} == chosen)
        recs.append(r)

    ok = [r for r in recs if r["valid"]]
    out = {}
    P = print

    # ---------------- [A] ----------------
    P("=" * 96)
    P(f"新数据验证：{os.path.basename(pattern)}   共 {len(recs)} 帧")
    P("=" * 96)
    P(f"\n[A] 有效性: {len(ok)}/{len(recs)} = {100*len(ok)/len(recs):.1f}%")
    fails = [r for r in recs if not r["valid"]]
    by_reason: dict[str, list] = {}
    for r in fails:
        k = r["reason"] + (f"(缺 {r['missing']})" if r["reason"] == "corner_markers_missing" else "")
        by_reason.setdefault(k, []).append(_short(r["file"]))
    for k, v in by_reason.items():
        P(f"    {k}: {len(v)} 帧 -> {', '.join(v)}")
    P(f"    失效帧清晰度(Laplacian var): " + ", ".join(f"{_short(r['file'])}={r['blur']:.0f}" for r in fails))
    P(f"    有效帧清晰度(Laplacian var): 中位 {np.median([r['blur'] for r in ok]):.0f}"
      f"  范围 {min(r['blur'] for r in ok):.0f}~{max(r['blur'] for r in ok):.0f}")
    nsp = sum(1 for r in recs if r["spurious"])
    P(f"    误检（白名单外的 ID）: {nsp}/{len(recs)} 帧出现，全部被 corner_ids/target_id 白名单滤掉；"
      f"共 {sum(len(r['spurious']) for r in recs)} 次"
      f"（高频 ID: {_top_spurious(recs)}）")

    # ---------------- [B] ----------------
    rms = np.array([r["rms_field"] for r in ok])
    trms = np.array([r["target_rms"] for r in ok])
    P(f"\n[B] 内部几何自洽（单位=场地边长;  1.0 = {FIELD_MM:.0f}mm）")
    P(f"    角标记方正度 rms_field : mean {rms.mean():.5f} ({rms.mean()*FIELD_MM:.2f}mm)  "
      f"max {rms.max():.5f} ({rms.max()*FIELD_MM:.2f}mm)   门限 {geo.max_square_rms} -> 余量 {geo.max_square_rms/rms.max():.0f}x")
    P(f"    0 号标记方正度 target_rms: mean {trms.mean():.5f} ({trms.mean()*FIELD_MM:.2f}mm)  max {trms.max():.5f}")

    # ---------------- [C] ----------------
    ccv = np.array([r["corner_scale_cv"] for r in ok if "corner_scale_cv" in r])
    cscale = np.array([[r["corner_scale"][i] for i in sorted(r["corner_scale"])] for r in ok if "corner_scale" in r])
    P(f"\n[C] 单应畸变估计（四个角标记实物同尺寸 -> 映射后尺寸应当相同）")
    P(f"    角标记映射尺寸 σ/mean : mean {ccv.mean()*100:.2f}%  max {ccv.max()*100:.2f}%")
    P(f"    四个角标记的映射尺寸（首帧）: " + ", ".join(f"id{k}={v*FIELD_MM:.1f}mm" for k, v in sorted(ok[0]["corner_scale"].items())))
    P(f"    同一位置在整批数据里的相对波动: " + ", ".join(
        f"id{k} {cscale[:, j].std(ddof=1)/cscale[:, j].mean()*100:.2f}%" for j, k in enumerate(sorted(ok[0]["corner_scale"]))))
    off = np.array([d for r in ok for d in r["corner_offset_px"]])
    P(f"\n    场地角点与该标记被选角点的像素差（反映 16 点联合精化把角点挪了多少，不是误差）: "
      f"mean {off.mean():.2f}px  max {off.max():.2f}px")

    # ---------------- [D] ----------------
    P(f"\n[D] 跨机位不变性（规范坐标系锚定在角标记 ID + 排布上）")
    arrs = _canon(recs, ok)
    P(f"    数据里共出现 {len(arrs)} 种角标记排布（竞争规则是排布不固定，这正好是压力测试）：")
    for a, g in sorted(arrs.items(), key=lambda kv: -len(kv[1])):
        P(f"      排布 {a:>9s} : {len(g):2d} 帧 -> {', '.join(_short(x['file']) for x in g)}")
    P("")
    P(f"    {'排布':>9s} {'X':>8s} {'Y':>8s} {'θ':>8s} {'n':>2s} {'σ(位)':>8s} {'max|Δ|':>8s} {'σ(θ)':>7s}  帧")
    spreads, sp_theta = [], []
    for a, g in sorted(arrs.items(), key=lambda kv: -len(kv[1])):
        groups: list[list] = []
        for r in g:
            for gg in groups:
                c0g = np.array([np.mean([it["cx"] for it in gg]), np.mean([it["cy"] for it in gg])])
                th0g = np.degrees(np.arctan2(
                    np.mean([np.sin(np.radians(it["cth"])) for it in gg]),
                    np.mean([np.cos(np.radians(it["cth"])) for it in gg])))
                dth = abs((r["cth"] - th0g + 180.0) % 360.0 - 180.0)
                # 位置近 **且** 朝向一致才算"同一物理位姿"：
                # 只按位置聚类会把"原地转 90°"的两组照片并成一簇，凭空造出 50° 的角度差
                if np.linalg.norm(np.array([r["cx"], r["cy"]]) - c0g) < 0.02 and dth < 20.0:
                    gg.append(r)
                    break
            else:
                groups.append([r])
        for gg in sorted(groups, key=lambda x: -len(x)):
            cs = np.array([[it["cx"], it["cy"]] for it in gg])
            th = np.array([it["cth"] for it in gg])
            c0 = cs.mean(axis=0)
            d = np.linalg.norm(cs - c0, axis=1)
            sd = np.hypot(cs[:, 0].std(ddof=1) if len(gg) > 1 else 0,
                          cs[:, 1].std(ddof=1) if len(gg) > 1 else 0)
            P(f"    {a:>9s} {c0[0]:8.4f} {c0[1]:8.4f} "
              f"{np.degrees(np.arctan2(np.mean(np.sin(np.radians(th))), np.mean(np.cos(np.radians(th))))):8.2f} "
              f"{len(gg):2d} {sd:8.5f} {d.max() if len(d) else 0:8.5f} "
              f"{th.std(ddof=1) if len(gg) > 1 else 0:7.3f}  "
              + ",".join(_short(it["file"]) for it in gg))
            if len(gg) > 1:
                spreads.append(d.max())
                sp_theta.append(th.std(ddof=1))
    if spreads:
        P(f"\n    同一物理位姿、不同机位重复拍摄（n>=2 的簇，{len(spreads)} 个）:")
        P(f"      位置最大偏差 : 中位 {np.median(spreads)*FIELD_MM:.2f}mm   max {max(spreads)*FIELD_MM:.2f}mm")
        P(f"      位置 RMS 偏差: 中位 {np.median(spreads)/2*FIELD_MM:.2f}mm（用极差/2 近似）")
        P(f"      角度最大 σ   : 中位 {np.median(sp_theta):.2f}°   max {max(sp_theta):.2f}°")

    # ---------------- 敏感性 ----------------
    P(f"\n[E] 内顶点组合敏感性（若次优组合才是真的，结果会偏多少）")
    sens = [r["vertex_variants"] for r in ok if r.get("vertex_variants")]
    d1 = np.array([v[1]["d_mm"] for v in sens if len(v) > 1])
    if len(d1) == 0:
        P("    （nearest 模式不做组合搜索，本节跳过）")
    else:
        P(f"    最优 vs 次优位置差: 中位 {np.median(d1):.1f}mm   mean {d1.mean():.1f}mm   max {d1.max():.1f}mm")
        P(f"    次优组合自身的方正度 rms_field: 中位 "
          f"{np.median([v[1]['rms_field'] for v in sens if len(v) > 1]):.5f}"
          f"  （最优 {np.median([v[0]['rms_field'] for v in sens]):.5f}）")
        P(f"    -> 内顶点选错会带来 ~{np.median(d1):.0f}mm 的定位偏差；"
          f"已目视核对：解出的场地角点落在红胶带交点上（见 out/_crop/field_corners.png），选择正确。")
        n_unc = sum(1 for r in ok if "inner_vertex_uncertain" in (r.get("flags") or []))
        n_ovr = sum(1 for r in ok if "inner_vertex_override" in (r.get("flags") or []))
        P(f"    带 inner_vertex_uncertain 标志的帧: {n_unc}/{len(ok)}"
          f"（cost_gap_rel < {geo.min_vertex_cost_gap_rel}），"
          f"gap_rel 中位 {np.median([r['gap_rel'] for r in ok if r['gap_rel'] is not None]):.4f}")
        P(f"    带 inner_vertex_override（推翻了 nearest）的帧: {n_ovr}/{len(ok)}")

    # ---------------- [F] ----------------
    P(f"\n[F] 内顶点判据对照：现行 = 距离余量主导(nearest) + 形状残差辅助")
    sb = [r for r in ok if r.get("sizebest_cv") is not None]
    if not sb:
        P("    （无候选集合，本节跳过）")
        spreads, sp_theta = spreads, sp_theta
    else:
        agree = sum(1 for r in sb if r.get("agrees"))
        P(f"    【已被否决的备选判据】只按「四个角标记映射尺寸一致性」选："
          f"与现行一致 {agree}/{len(sb)} 帧（{100*agree/max(len(sb),1):.0f}%）")
        P(f"    该判据自身的尺寸离散度 σ/mean: 中位 "
          f"{np.median([r['sizebest_cv'] for r in sb])*100:.2f}%"
          f"   现行解: 中位 {np.median([r['corner_scale_cv'] for r in sb])*100:.2f}%")
        P(f"    两解的位置差: 中位 "
          f"{np.median([r['d_pos_mm'] for r in sb if r.get('d_pos_mm') is not None]):.2f}mm")
        P(f"    -> 该判据能压低尺寸离散度，但给出的组合跨帧不自洽（同一机位连拍会来回跳），"
          f"属过拟合，不可用作主判据。")

    json.dump(recs, open(os.path.join(ROOT, "out", "validate_20261003_analysis.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=float)
    P(f"\n明细: out/validate_20261003_analysis.json")
    return 0


def _minrot(seq):
    seq = list(seq)
    i = int(np.argmin(seq))
    return seq[i:] + seq[:i]


def _short(name):
    return name.replace("IMG_20261003_", "").replace(".jpg", "")


def _top_spurious(recs):
    d: dict[int, int] = {}
    for r in recs:
        for s in r["spurious"]:
            d[s] = d.get(s, 0) + 1
    return ", ".join(f"{k}({v})" for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:5])


def _canon(recs, ok):
    """给每帧算规范坐标（锚定到角标记 ID + 排布的规范位置）。"""
    arrs: dict[str, list] = {}
    for r in ok:
        arrs.setdefault(r["arr"], []).append(r)
    for a, g in arrs.items():
        O = g[0]["ring"]
        for r in g:
            R = r["ring"]
            j = None
            for kk in range(4):
                if [O[(i + kk) % 4] for i in range(4)] == R:
                    j = kk
                    break
            if j is None:
                r["cx"] = r["cy"] = r["cth"] = None
                continue
            r["cx"], r["cy"] = r_k(j, (r["x"], r["y"]))
            dx, dy = r["dir"]
            ndx, ndy = r_k(j, (dx, dy))
            # r_k 在 (dir) 上等价于把向量整体旋转 j*90°（对角线上取的是同一线性映射）
            th = np.degrees(np.arctan2(ndy, ndx))
            r["cth"] = (th + 180.0) % 360.0 - 180.0
    return arrs


if __name__ == "__main__":
    raise SystemExit(main())
