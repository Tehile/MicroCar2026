#!/usr/bin/env python
"""实验：把输入图像**先转成灰度**再跑定位算法，量化精度与一致性如何变化。

⚠ **本实验不计入正式版本**：不修改 `aruco_locator/` 任何一行代码，只通过公开
接口 `create_locator` / `ArucoLocator.locate` 做只读调用。本目录不在
`tools/make_release.py` 的打包树里，不会进归档。

────────────────────────────────────────────────────────────────────────────
读代码即可确定的前提
────────────────────────────────────────────────────────────────────────────
整条计算链路只用灰度：

    detector.py 是**唯一**接触彩色的文件，而且第一件事就是：
        if frame.ndim == 3: gray = cv2.cvtColor(frame, COLOR_BGR2GRAY)
    corners.py / geometry.py / parallax.py / api.py 全部不含任何 COLOR_ 常量；
    viz.py 只往图上画，从不读通道。

所以"输入预先转灰度"在数学上**不该**改变任何解算量，本实验要做的是把它**量化**，
而不是"证明它对"。真正值得量的是另一种差异：真实 JPEG 里
`imread(IMREAD_GRAYSCALE)` 走的是 libjpeg 的 Y 通道，而
`imread(IMREAD_COLOR)` + `cvtColor` 走的是 YCbCr→BGR→灰度 的往返 ——
两者不是同一个数，差的这一点点会不会改变检出与解算才是问题。

────────────────────────────────────────────────────────────────────────────
四组实验
────────────────────────────────────────────────────────────────────────────
[A] 合成（有真值）精度 —— 用 selftest_synthetic 的渲染器生成同一批场景，
    对**同一张图**施加不同灰度化方式，比较位置/角度误差。
[B] 实拍 39 张（无真值）—— 与基线逐帧比对 Δx/Δy/Δθ/Δrms、检出集合、有效帧数。
[C] 真实解码路径 —— IMREAD_GRAYSCALE vs IMREAD_COLOR+cvtColor（只在实拍上有意义）。
[D] 耗时 —— **交错采样**（每轮换个顺序），避免"单次取最小"的排序偏置。

────────────────────────────────────────────────────────────────────────────
模式定义（"灰度"的几种口径）
────────────────────────────────────────────────────────────────────────────
    color        解码 BGR 原样送入           （= 当前正式版本行为，作为基线）
    gray_cvt     BGR→灰度（OpenCV 光度权重），单通道送入   ← 应与 color 逐位一致
    gray_cvt3    同上但复制成 3 通道                        ← 验证代码路径等价
    gray_mean    三通道**未加权**均值                       ← 只量"光度公式"敏感度
    gray_decode  直接以 IMREAD_GRAYSCALE 解码（JPEG 的 Y 通道）
    gray_jpeg    灰度后重编码为 JPEG(q=95) 再解码（= 真把图存成灰度图的情形）

用法::

    python experiments/exp_gray_input.py                  # 全部四组
    python experiments/exp_gray_input.py --no-real        # 只跑合成
    python experiments/exp_gray_input.py -n 60 --stress   # 加大难度跑合成
    python experiments/exp_gray_input.py --json out.json  # 另存原始结果
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from glob import glob

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aruco_locator import create_locator  # noqa: E402
from aruco_locator.sources import imread_unicode  # noqa: E402

import selftest_synthetic as st  # noqa: E402  （根目录脚本，用作场景渲染器）

FIELD_MM = 1440.0  # 实拍场地边长：8 格 × 180mm
BASELINE = "color"

# 实拍用到的模式（含两种"真·解码"口径）
REAL_MODES = ["color", "gray_cvt", "gray_cvt3", "gray_decode", "gray_jpeg", "gray_mean"]
# 合成没有 JPEG 文件，gray_decode 无对应物；gray_jpeg 用内存编码往返模拟
SYNTH_MODES = ["color", "gray_cvt", "gray_cvt3", "gray_jpeg", "gray_mean"]


# --------------------------------------------------------------------------- #
# 灰度化 / 载入
# --------------------------------------------------------------------------- #
def to_frame(bgr: np.ndarray, mode: str) -> np.ndarray:
    """把解码后的 BGR 帧变成"送进定位器的那张图"。"""
    if mode == "color":
        return bgr
    if mode == "gray_cvt":
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    if mode == "gray_cvt3":
        # 单通道灰度复制成 3 通道：B=G=R=v 时 cvtColor(BGR2GRAY) 恒等还原 v，
        # 所以它应当和 gray_cvt 结果完全一样 —— 用来验证"代码路径"确实无关。
        return cv2.cvtColor(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    if mode == "gray_mean":
        return bgr.mean(axis=2).round().astype(np.uint8)  # 未加权均值
    if mode == "gray_jpeg":
        g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        ok, buf = cv2.imencode(".jpg", g, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        if not ok:
            raise RuntimeError("JPEG 编码失败")
        return cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
    if mode == "gray_decode":
        raise ValueError("gray_decode 是解码期选项，不能用 to_frame 得到（请用 load_frame）")
    raise ValueError(f"未知模式 {mode!r}")


def load_frame(path: str, mode: str) -> np.ndarray | None:
    """实拍单张载入：gray_decode 走 IMREAD_GRAYSCALE，其余先解成彩色再变换。"""
    if mode == "gray_decode":
        return imread_unicode(path, cv2.IMREAD_GRAYSCALE)
    bgr = imread_unicode(path)
    if bgr is None:
        return None
    return to_frame(bgr, mode)


# --------------------------------------------------------------------------- #
# 统计小工具
# --------------------------------------------------------------------------- #
def _dtheta(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _stats(v: np.ndarray) -> dict:
    if v.size == 0:
        return {"n": 0}
    return {
        "n": int(v.size),
        "mean": float(v.mean()),
        "p50": float(np.percentile(v, 50)),
        "p95": float(np.percentile(v, 95)),
        "max": float(v.max()),
    }


def _fmt(s: dict, dec: int = 3, unit: str = "") -> str:
    if not s.get("n"):
        return "—"
    return f"{s['mean']:.{dec}f}/{s['p95']:.{dec}f}/{s['max']:.{dec}f}{unit}"


# --------------------------------------------------------------------------- #
# [A] 合成：有真值的精度
# --------------------------------------------------------------------------- #
def _synth_args(ns):
    """复刻 selftest_synthetic.main 的默认参数（保证与正式自检同分布）。"""
    a = argparse.Namespace(
        cases=ns.cases, seed=ns.seed, config=os.path.join(ROOT, "config.yaml"),
        vertex_mode=None, out=None, save_cases=0,
        canvas=(1280, 960), fill=0.70, tilt=0.16, cam_roll=35.0, rot_jitter=40.0,
        target_range=(0.18, 0.82), mm_per_px=1.0, supersample=2, cam_supersample=2,
        blur=0.8, noise=4.0, gradient=0.06, ideal=False,
        verbose=False, compare_edge=False, distort=False,
        heights=False, field_mm=1000.0, cam_height_mm=2000.0,
        corner_height_mm=50.0, target_height_mm=23.0, nadir_offset=(-0.12, -0.06),
    )
    if ns.stress:
        a.blur, a.noise, a.gradient = 1.6, 12.0, 0.14
    if ns.ideal:
        a.ideal = True
    return a


def run_synthetic(ns) -> dict:
    args = _synth_args(ns)
    base_cfg = st.load_config(args.config)
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, base_cfg.detection.dict_name))
    rng = np.random.default_rng(args.seed)
    mm = float(st.FIELD_MM)

    locators = {m: st.ArucoLocator(base_cfg) for m in SYNTH_MODES}
    acc = {m: {"err": [], "dth": [], "pairs": {}, "invalid": 0, "flip": 0, "skip": 0,
               "reasons": {}, "rms": []} for m in SYNTH_MODES}

    for case in range(args.cases):
        plane, field_quad_px, info = st.render_plane(rng, args, dictionary)
        img, Hm = st.camera_view(plane, field_quad_px, rng, args)
        gt = st.ground_truth(Hm @ st.mm_to_px_homography(info), img.shape, info)
        if gt is None:
            for m in SYNTH_MODES:
                acc[m]["skip"] += 1
            continue

        # 场景只渲染一次；对**同一张**图施加不同灰度化 —— 这样差异只来自灰度化本身
        res = {m: locators[m].locate(to_frame(img, m), frame_index=case) for m in SYNTH_MODES}

        for m in SYNTH_MODES:
            loc = res[m]
            a = acc[m]
            if not loc.valid:
                a["invalid"] += 1
                a["reasons"][loc.reason] = a["reasons"].get(loc.reason, 0) + 1
                continue
            if loc.origin_id != gt["origin_id"]:
                a["flip"] += 1
                continue
            err = math.hypot(loc.x - gt["xy"][0], loc.y - gt["xy"][1])
            err_yx = math.hypot(loc.x - gt["xy"][1], loc.y - gt["xy"][0])
            dth = _dtheta(loc.theta_deg, gt["theta_deg"])
            if err * mm > 5.0 and err_yx * mm < 5.0:
                continue  # ±45° 轴互换，固有歧义，不算误差
            a["err"].append(err * mm)
            a["dth"].append(dth)
            if loc.rms_field is not None:
                a["rms"].append(float(loc.rms_field))
            a["pairs"][case] = {"x": loc.x, "y": loc.y, "th": loc.theta_deg,
                                "err_mm": err * mm, "dth": dth, "valid": True}

        # 与基线逐位比对（同一张图、同一次解算的不同灰度口径）
        for m in SYNTH_MODES:
            if m == BASELINE:
                continue
            b, o = res[BASELINE], res[m]
            a = acc[m]
            if b.valid != o.valid:
                a.setdefault("valid_diff", []).append(case)
                continue
            if not b.valid:
                continue
            a.setdefault("dpos", []).append(math.hypot(b.x - o.x, b.y - o.y) * mm)
            a.setdefault("dth_cmp", []).append(_dtheta(b.theta_deg, o.theta_deg))
            if b.origin_id != o.origin_id:
                a.setdefault("origin_diff", []).append(case)

    return {"args": vars(args), "acc": acc, "modes": SYNTH_MODES}


def report_synthetic(r: dict, ns) -> None:
    acc, modes = r["acc"], r["modes"]
    a0 = r["args"]
    print("\n" + "=" * 118)
    print(f"[A] 合成场景精度（有真值）  {a0['cases']} 个随机场景  seed={a0['seed']}  "
          f"blur={a0['blur']} noise={a0['noise']} gradient={a0['gradient']}"
          + ("  [ideal]" if a0["ideal"] else ""))
    print(f"    场地边长 {st.FIELD_MM:.0f}mm；误差单位 mm / 度")
    print("-" * 118)
    print(f"{'模式':<12}{'有效':>6}{'无效':>6}{'换角':>6}{'跳过':>6}"
          f"{'位置 均值/p95/最大':>26}{'角度 均值/p95/最大':>26}{'rms均值':>12}")
    for m in modes:
        s = acc[m]
        e = np.asarray(s["err"])
        d = np.asarray(s["dth"])
        rm = float(np.mean(s["rms"])) if s["rms"] else float("nan")
        print(f"{m:<12}{e.size:>6}{s['invalid']:>6}{s['flip']:>6}{s['skip']:>6}"
              f"{_fmt(_stats(e)):>26}{_fmt(_stats(d)):>26}{rm:>12.6f}")

    print("-" * 118)
    print(f"  与基线「{BASELINE}」逐位比对（同一张图，只改灰度化口径）：")
    print(f"  {'模式':<12}{'位置差 均值/p95/最大(mm)':>30}{'角度差 均值/最大(°)':>24}"
          f"{'原点差异':>10}{'有效数差异':>12}{'完全一致':>10}")
    n_cases = len(acc[BASELINE]["pairs"])
    for m in modes:
        if m == BASELINE:
            continue
        s = acc[m]
        dp, dt = s.get("dpos", []), s.get("dth_cmp", [])
        od, vd = len(s.get("origin_diff", [])), len(s.get("valid_diff", []))
        allzero = (not dp or max(dp) == 0.0) and (not dt or max(dt) == 0.0)
        ident = "是" if (allzero and not od and not vd and dp) else "否"
        print(f"  {m:<12}{_fmt(_stats(np.asarray(dp))):>30}{_fmt(_stats(np.asarray(dt)), 4, '°'):>24}"
              f"{od:>10}{vd:>12}{ident:>10}")
    print(f"  （完全一致 = 位置差与角度差全为 0，且原点/有效性无差异；基线有效场景 {n_cases} 个）")


# --------------------------------------------------------------------------- #
# [B][C] 实拍：逐帧一致性与检出鲁棒性
# --------------------------------------------------------------------------- #
def run_real(ns) -> dict:
    files = sorted(glob(os.path.join(ROOT, ns.glob)))
    if not files:
        raise SystemExit(f"没找到实拍样本：{ns.glob}")
    loc = create_locator(os.path.join(ROOT, "config.yaml"))

    recs = {m: [] for m in REAL_MODES}
    det_gray_same = 0  # gray_decode 与 color 的灰度图逐位相同？—— 统计用
    gray_max_abs = []
    for i, fp in enumerate(files):
        bgr = imread_unicode(fp)
        if bgr is None:
            continue
        g_cvt = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        g_dec = imread_unicode(fp, cv2.IMREAD_GRAYSCALE)
        if g_dec is not None:
            d = np.abs(g_cvt.astype(np.int16) - g_dec.astype(np.int16))
            gray_max_abs.append(int(d.max()))
            det_gray_same += int(bool((d == 0).all()))
        for m in REAL_MODES:
            frame = to_frame(bgr, m) if m != "gray_decode" else g_dec
            if frame is None:
                continue
            r = loc.locate(frame, frame_index=i)
            recs[m].append({
                "file": os.path.basename(fp), "valid": r.valid, "reason": r.reason,
                "x": r.x, "y": r.y, "th": r.theta_deg, "rms": r.rms_field,
                "origin": r.origin_id, "ids": list(r.corner_ids),
                "detected": list((r.diagnostics or {}).get("detected_ids", [])),
                "flags": list(r.flags),
            })

    ana = {}
    base = recs[BASELINE]
    ana[BASELINE] = {
        "valid": sum(1 for r in base if r["valid"]),
        "det_total": sum(len(r["detected"]) for r in base),
        "invalid_reasons": _count(r["reason"] for r in base if not r["valid"]),
        "baseline": True,
    }
    for m in REAL_MODES:
        cur = recs[m]
        if m == BASELINE:
            continue
        dpos, dth, drms = [], [], []
        origin_diff, valid_diff, ids_diff, det_diff, flags_diff = [], [], [], [], []
        for b, o in zip(base, cur):
            if b["valid"] != o["valid"]:
                valid_diff.append(b["file"])
            if b["origin"] != o["origin"]:
                origin_diff.append(b["file"])
            if b["ids"] != o["ids"]:
                ids_diff.append(b["file"])
            if b["detected"] != o["detected"]:
                det_diff.append(b["file"])
            if b["flags"] != o["flags"]:
                flags_diff.append(b["file"])
            if b["valid"] and o["valid"] and b["origin"] == o["origin"]:
                dpos.append(math.hypot(b["x"] - o["x"], b["y"] - o["y"]) * FIELD_MM)
                dth.append(_dtheta(b["th"], o["th"]))
                if b["rms"] is not None and o["rms"] is not None:
                    drms.append(abs(b["rms"] - o["rms"]))
        ana[m] = {
            "valid": sum(1 for r in cur if r["valid"]),
            "invalid_reasons": _count(r["reason"] for r in cur if not r["valid"]),
            "det_total": sum(len(r["detected"]) for r in cur),
            "dpos": _stats(np.asarray(dpos)),
            "dth": _stats(np.asarray(dth)),
            "drms": _stats(np.asarray(drms)),
            "origin_diff": len(origin_diff), "valid_diff": len(valid_diff),
            "ids_diff": len(ids_diff), "det_diff": len(det_diff), "flags_diff": len(flags_diff),
            "files_det_diff": det_diff[:5],
            "identical": not (dpos and any(v > 0 for v in dpos)) and not (dth and any(v > 0 for v in dth)),
        }
    return {
        "n": len(base), "files": [r["file"] for r in base],
        "ana": ana, "modes": REAL_MODES,
        "gray_dec_vs_cvt": {
            "identical_frames": det_gray_same, "n": len(gray_max_abs),
            "max_abs_diff": int(max(gray_max_abs)) if gray_max_abs else None,
            "mean_max_abs_diff": float(np.mean(gray_max_abs)) if gray_max_abs else None,
        },
        "baseline_invalid": _count(r["reason"] for r in base if not r["valid"]),
    }


def _count(it) -> dict:
    out = {}
    for k in it:
        out[k] = out.get(k, 0) + 1
    return out


def report_real(r: dict) -> None:
    ana, modes = r["ana"], r["modes"]
    n = r["n"]
    print("\n" + "=" * 118)
    print(f"[B][C] 实拍样本（无测绘真值）  {n} 张  场地 {FIELD_MM:.0f}mm")
    print("-" * 118)
    print(f"{'模式':<12}{'有效/' + str(n):>10}{'检出标记总数':>14}")
    for m in modes:
        print(f"{m:<12}{str(ana[m]['valid']) + '/' + str(n):>10}{ana[m]['det_total']:>14}")
    print("-" * 118)
    print(f"  与基线「{BASELINE}」逐帧比对：")
    print(f"  {'模式':<12}{'Δ位置 均值/p95/最大(mm)':>30}{'Δ角度 均值/最大(°)':>24}"
          f"{'Δrms 最大':>12}{'原点异':>8}{'有效异':>8}{'检出异':>8}{'标志异':>8}")
    for m in modes:
        if m == BASELINE:
            continue
        a = ana[m]
        print(f"  {m:<12}{_fmt(a['dpos']):>30}{_fmt(a['dth'], 4, '°'):>24}"
              f"{a['drms']['max'] if a['drms'].get('n') else 0.0:>12.6f}"
              f"{a['origin_diff']:>8}{a['valid_diff']:>8}{a['det_diff']:>8}{a['flags_diff']:>8}")
    print("-" * 118)
    gd = r["gray_dec_vs_cvt"]
    print(f"  [C] JPEG 解码路径：IMREAD_GRAYSCALE 与 IMREAD_COLOR+cvtColor 得到的灰度图")
    print(f"      逐位完全相同的帧：{gd['identical_frames']}/{gd['n']}；"
          f"最大像素差 {gd['max_abs_diff']}（0~255）；平均最大像素差 {gd['mean_max_abs_diff']:.4f}")
    bi = r["baseline_invalid"]
    print(f"  基线无效帧 {sum(bi.values())} 个" + (f"（{_count_str(bi)}）" if bi else ""))
    print(f"  检出标记集合有差异：", end="")
    shown = False
    for m in modes:
        if m == BASELINE:
            continue
        d = ana[m]["ids_diff"]
        if d:
            shown = True
            print(f"\n      {m}: {d} 帧，例：{', '.join(ana[m]['files_det_diff'])}", end="")
    if not shown:
        print(" 无（所有模式检出的角标记集合完全一致）")


def _count_str(d: dict) -> str:
    return "，".join(f"{k}×{v}" for k, v in d.items())


# --------------------------------------------------------------------------- #
# [D] 耗时（交错采样）
# --------------------------------------------------------------------------- #
def run_timing(ns) -> dict:
    files = sorted(glob(os.path.join(ROOT, ns.glob)))
    if not files:
        return {}
    img = imread_unicode(files[0])
    loc = create_locator(os.path.join(ROOT, "config.yaml"))
    frames = {m: to_frame(img, m) for m in REAL_MODES if m != "gray_decode"}
    for _ in range(3):  # 预热
        for f in frames.values():
            loc.locate(f)

    acc = {m: [] for m in frames}
    order = [m for m in frames]
    for rep in range(ns.reps):
        # 每轮把顺序轮换一次，让"排序偏置"平摊到各模式上（单次取最小值会得出相反结论）
        seq = order[rep % len(order):] + order[:rep % len(order)]
        for m in seq:
            t0 = time.perf_counter()
            loc.locate(frames[m])
            acc[m].append((time.perf_counter() - t0) * 1000.0)
    return {m: {"mean_ms": float(np.mean(v)), "min_ms": float(np.min(v)), "n": len(v)}
            for m, v in acc.items()}


def report_timing(t: dict) -> None:
    if not t:
        return
    base = t.get(BASELINE, {}).get("mean_ms")
    print("\n" + "=" * 118)
    print("[D] 单帧耗时（交错采样，每轮轮换顺序）")
    print("-" * 118)
    print(f"  {'模式':<12}{'均值(ms)':>12}{'最小(ms)':>12}{'相对基线':>12}")
    for m, v in t.items():
        rel = f"{base / v['mean_ms']:.3f}×" if base else "—"
        print(f"  {m:<12}{v['mean_ms']:>12.4f}{v['min_ms']:>12.4f}{rel:>12}")


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="灰度输入实验（不计入正式版本）")
    ap.add_argument("--cases", "-n", type=int, default=40, help="合成场景数")
    ap.add_argument("--seed", type=int, default=20260922)
    ap.add_argument("--stress", action="store_true", help="加大难度：blur=1.6 noise=12 gradient=0.14")
    ap.add_argument("--ideal", action="store_true", help="理想成像（无模糊/噪声/光照不均）")
    ap.add_argument("--glob", default="samples/*.jpg", help="实拍样本 glob")
    ap.add_argument("--no-real", action="store_true", help="跳过实拍部分")
    ap.add_argument("--no-timing", action="store_true", help="跳过耗时部分")
    ap.add_argument("--reps", type=int, default=30, help="耗时重复次数")
    ap.add_argument("--json", default=None, help="把原始结果另存为 JSON")
    ns = ap.parse_args(argv)

    out = {}
    syn = run_synthetic(ns)
    report_synthetic(syn, ns)
    out["synthetic"] = {k: v for k, v in syn["acc"].items()}

    if not ns.no_real:
        real = run_real(ns)
        report_real(real)
        out["real"] = real["ana"]
        out["gray_decode_diff"] = real["gray_dec_vs_cvt"]
        if not ns.no_timing:
            tm = run_timing(ns)
            report_timing(tm)
            out["timing"] = tm

    print("\n" + "=" * 118)
    if ns.json:
        with open(ns.json, "w", encoding="utf-8") as fp:
            json.dump(out, fp, ensure_ascii=False, indent=2, default=str)
        print(f"原始结果已写入 {ns.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
