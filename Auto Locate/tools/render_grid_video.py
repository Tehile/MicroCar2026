"""用**正式接口**在真实视频上跑格点匹配通道，并合成审阅视频。

为什么单独一个工具
------------------
``ArucoLocator.locate_grid()`` 是算法出口：它要求帧里四个角标记齐全（要它们的
单应），不齐全就返回无效。整段视频有大量帧四角不齐（运动模糊/遮挡），逐帧丢帧
会让审阅视频看起来"什么都没跑"。这个工具补上**编排层**那一步：四角缺失时用
时间上最近的实测单应插值（记录 ``hgap`` = 距最近实测帧的帧数），与已验收的
实验版（``experiments/vid_stage2_locate.py``）同一做法。

流程（三遍读视频）
------------------
    1. 解单应   逐帧只解 H → {帧号: H | None}（约 0.4 s/帧，串行）
    2. 求解     分块并行跑 locate_grid（单帧约 8 s，4 进程约 2.5 s/帧有效吞吐）
    3. 渲染     顺序重读视频，只用第 2 步的解画图（约 1 s/帧）

产物（默认 ``--out out/grid``）：
    grid_<name>.json          逐帧解与诊断（hgap / gap / sharp / n_bad / 耗时）
    grid_summary_<name>.json  汇总（单应覆盖率、接受率、耗时）
    grid_review_<name>.mp4    审阅视频：主画面 + 卡片外框 + 36 格特写 + HUD

用法::

    python tools/render_grid_video.py --video samples/VID_20261027_120407.mp4 --scales 1,2
    python tools/render_grid_video.py --video ... --start 56 --end 368 --scale 0.5
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import os
import sys
import time

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aruco_locator import Localization, create_locator, load_config  # noqa: E402
from aruco_locator.session import preflight  # noqa: E402
from aruco_locator.viz import _hud_lines, draw_overlay  # noqa: E402

C_OK = (120, 235, 120)
C_BAD = (80, 80, 235)
C_WARN = (60, 200, 230)
C_TARGET = (0, 165, 255)
C_GRID = (200, 160, 60)


# --------------------------------------------------------------------------- #
# 视频读写
# --------------------------------------------------------------------------- #
def _open_cap(path: str):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():  # 中文路径兜底：换 ffmpeg 后端再试
        cap.release()
        cap = cv2.VideoCapture(path, cv2.CAP_FFMPEG)
    return cap


def _skip(cap, n: int) -> None:
    for _ in range(int(n)):
        if not cap.grab():
            break


def _writer(path: str, size: tuple[int, int], fps: float):
    """非 ASCII 路径下 VideoWriter 会静默失败：先写临时文件再改名。"""
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    tmp = os.path.join(os.path.dirname(os.path.abspath(path)) or ".", "__tmp_grid.mp4")
    w = cv2.VideoWriter(tmp, fourcc, fps, size)
    if not w.isOpened():
        raise RuntimeError(f"无法创建视频写入器：{tmp}")
    return w, tmp


# --------------------------------------------------------------------------- #
# 第 1 遍：只解单应
# --------------------------------------------------------------------------- #
def pass_homography(locator, video: str, s0: int, s1: int, quiet: bool = False) -> dict:
    cap = _open_cap(video)
    out: dict[int, dict] = {}
    t0 = time.time()
    cids = [int(i) for i in locator.cfg.detection.corner_ids]
    _skip(cap, s0)
    for k in range(s0, s1):
        okr, frame = cap.read()
        if not okr or frame is None:
            break
        frame = locator.undistort(frame)
        obs = locator.detector.detect(frame)
        H, info = locator.field_frame_from_obs(obs, frame.shape)
        if H is not None and info.get("ambiguous_axis"):
            H = None  # 轴向存疑 → 场地可能被转 90°，宁缺勿错
            info = dict(info, reason="ambiguous_axis")
        # 只记"角标记里本帧命中的"：obs 里可能有误检 ID（实测出现过 944），
        # 混进 corner_ids 会让审阅视频显示成 5 个角标记。
        out[k] = {"H": H, "origin_id": info.get("origin_id"),
                  "reason": info.get("reason", ""), "n_detected": len(obs),
                  "ids": sorted(int(i) for i in obs if int(i) in cids)}
        if not quiet and (k - s0 + 1) % 50 == 0:
            nok = sum(1 for v in out.values() if v["H"] is not None)
            print(f"  [1/3] {k - s0 + 1}/{s1 - s0} 帧  实测单应 {nok}  "
                  f"{time.time() - t0:.0f}s", end="\r", flush=True)
    cap.release()
    nok = sum(1 for v in out.values() if v["H"] is not None)
    if not quiet:
        print(f"  [1/3] 完成：{len(out)} 帧，实测单应 {nok} "
              f"({nok / max(len(out), 1):.1%})  {time.time() - t0:.0f}s")
    return out


def save_hs(path: str, hs: dict) -> None:
    """把单应序列存成 npz（重渲染时无需再跑一遍检测）。"""
    ks = np.array(sorted(hs), np.int32)
    H = np.zeros((len(ks), 3, 3), np.float64)
    ok = np.zeros(len(ks), np.int8)
    oid = np.full(len(ks), -1, np.int32)
    ids = []
    for i, k in enumerate(ks):
        v = hs.get(int(k)) or {}
        if v.get("H") is not None:
            H[i] = np.asarray(v["H"], np.float64)
            ok[i] = 1
        o = v.get("origin_id")
        if o is not None:
            oid[i] = int(o)
        ids.append(";".join(str(x) for x in (v.get("ids") or [])))
    np.savez_compressed(path, k=ks, H=H, ok=ok, origin_id=oid,
                        ids=np.array(ids, dtype=object),
                        reason=np.array([(hs.get(int(k)) or {}).get("reason", "")
                                         for k in ks], dtype=object),
                        n_detected=np.array([(hs.get(int(k)) or {}).get("n_detected", 0)
                                             for k in ks], np.int32))


def load_hs(path: str) -> dict:
    z = np.load(path, allow_pickle=True)
    out: dict[int, dict] = {}
    for i, k in enumerate(z["k"].astype(int)):
        out[int(k)] = {
            "H": np.asarray(z["H"][i], np.float64) if z["ok"][i] else None,
            "origin_id": None if z["origin_id"][i] < 0 else int(z["origin_id"][i]),
            "reason": str(z["reason"][i]),
            "n_detected": int(z["n_detected"][i]),
            "ids": [int(t) for t in str(z["ids"][i]).split(";") if t],
        }
    return out


def interpolate_h(hs: dict, k: int, max_gap: int) -> tuple[np.ndarray | None, int]:
    """取帧 k 的单应：实测优先，否则用最近实测（左右都有则线性插值）。"""
    cur = hs.get(k)
    if cur is not None and cur["H"] is not None:
        return np.asarray(cur["H"], np.float64), 0
    left = right = None
    for s in range(1, max_gap + 1):
        l = hs.get(k - s)
        r = hs.get(k + s)
        if left is None and l is not None and l["H"] is not None:
            left = (k - s, np.asarray(l["H"], np.float64))
        if right is None and r is not None and r["H"] is not None:
            right = (k + s, np.asarray(r["H"], np.float64))
        if left is not None or right is not None:
            break
    if left is not None and right is not None:
        w = (k - left[0]) / float(right[0] - left[0])
        H = (1.0 - w) * left[1] + w * right[1]
        gap = min(k - left[0], right[0] - k)
    elif left is not None:
        H, gap = left[1], k - left[0]
    elif right is not None:
        H, gap = right[1], right[0] - k
    else:
        return None, 9999
    if abs(H[2, 2]) < 1e-9:
        return None, 9999
    return H / H[2, 2], int(gap)


# --------------------------------------------------------------------------- #
# 第 2 遍：并行求解
# --------------------------------------------------------------------------- #
def _solve_chunk(job) -> list[dict]:
    video, s0, s1, hs, cfg, max_gap, fps, threads = job
    if threads > 0:
        cv2.setNumThreads(int(threads))
    locator = create_locator(cfg)
    cap = _open_cap(video)
    _skip(cap, s0)
    rows: list[dict] = []
    for k in range(s0, s1):
        okr, frame = cap.read()
        if not okr or frame is None:
            break
        H, hgap = interpolate_h(hs, k, max_gap)
        row = {"k": k, "hgap": int(hgap), "ok_H": int(hgap == 0)}
        if H is None:
            row.update(valid=False, reason="no_homography")
            rows.append(row)
            continue
        loc = locator.locate_grid(frame, H, k, k / max(fps, 1e-9))
        g = (loc.diagnostics or {}).get("grid") or {}
        row.update(dict(
            valid=bool(loc.valid),
            x=None if loc.x is None else round(float(loc.x), 6),
            y=None if loc.y is None else round(float(loc.y), 6),
            theta=None if loc.theta_deg is None else round(float(loc.theta_deg), 3),
            gap=g.get("gap"), sharp=g.get("sharp"), margin=g.get("margin"),
            ncc=g.get("ncc"), n_bad=g.get("n_bad"), beta=g.get("beta"),
            rot=g.get("rot"), seed_spread_mm=g.get("seed_spread_mm"),
            t=g.get("t_total"), reason=loc.reason or "",
        ))
        rows.append(row)
        del frame
    cap.release()
    return rows


def solve_parallel(video: str, s0: int, s1: int, hs: dict, cfg, max_gap: int,
                   fps: float, workers: int, threads: int, quiet: bool) -> list[dict]:
    n = s1 - s0
    if workers <= 1:
        return _solve_chunk((video, s0, s1, hs, cfg, max_gap, fps, threads))
    step = int(math.ceil(n / workers))
    jobs = [(video, i, min(i + step, s1), hs, cfg, max_gap, fps, threads)
            for i in range(s0, s1, step)]
    rows: list[dict] = []
    t0 = time.time()
    with mp.get_context("spawn").Pool(len(jobs)) as pool:
        for part in pool.imap_unordered(_solve_chunk, jobs):
            rows.extend(part)
            if not quiet:
                print(f"  [2/3] {len(rows)}/{n} 帧  {time.time() - t0:.0f}s",
                      end="\r", flush=True)
    rows.sort(key=lambda r: r["k"])
    if not quiet:
        print(f"  [2/3] 完成：{len(rows)} 帧  {time.time() - t0:.0f}s "
              f"({(time.time() - t0) / max(len(rows), 1):.2f}s/帧有效)")
    return rows


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def card_quad_field(x: float, y: float, theta_deg: float, side: float) -> np.ndarray:
    """卡片 6×6 外框在场地系的四个角（side = 卡片边长，场地归一化单位）。"""
    th = math.radians(theta_deg)
    u = np.array([math.cos(th), math.sin(th)]) * (side / 2.0)
    v = np.array([-math.sin(th), math.cos(th)]) * (side / 2.0)
    c = np.array([x, y])
    return np.array([c - u - v, c + u - v, c + u + v, c - u + v])


def draw_card(frame, matcher, H_inv, sol):
    """把解出的卡片外框、中心与朝向画回原图。"""
    side = float(matcher.cfg.card_mm) / float(matcher.cfg.field_size_mm)
    quad = card_quad_field(sol["x"], sol["y"], sol["theta"], side)
    pts = cv2.perspectiveTransform(
        quad.reshape(-1, 1, 2), np.asarray(H_inv, np.float64)).reshape(-1, 2)
    if not np.all(np.isfinite(pts)):
        return frame
    ip = pts.astype(np.int32)
    col = C_OK if matcher.accept(sol) else C_BAD
    cv2.polylines(frame, [ip.reshape(-1, 1, 2)], True, col, 2, cv2.LINE_AA)
    c = ip.mean(axis=0).astype(int)
    cv2.circle(frame, tuple(c), 4, C_TARGET, -1, cv2.LINE_AA)
    th = math.radians(sol["theta"])
    tip_f = np.array([[sol["x"] + math.cos(th) * side * 0.55,
                       sol["y"] + math.sin(th) * side * 0.55]])
    tip = cv2.perspectiveTransform(
        tip_f.reshape(-1, 1, 2), np.asarray(H_inv, np.float64)).reshape(-1, 2)[0]
    cv2.arrowedLine(frame, tuple(c), (int(tip[0]), int(tip[1])), C_TARGET, 3,
                    cv2.LINE_AA, tipLength=0.28)
    return frame


def draw_zoom_panel(dst, src, matcher, H, sol, panel: int = 300, half_cells: float = 4.0):
    """卡片邻域 + 36 格网格线，放大贴到 ``dst`` 的右下角。

    ``src`` 是**采样源**（未标注的原图）：若用已标注的图，主画面画的卡片框与长箭头
    会一起被 warp 进小窗，把要看的格线遮住。``dst`` 才是贴图目标。

    只 warp 卡片邻域，不生成整幅 4207² 正射图（那一步每帧 0.3 s / 50 MB）。
    """
    geo = matcher.geometry()
    A = geo.affine()
    cx = A[0, 0] * sol["x"] + A[0, 2]
    cy = A[1, 1] * sol["y"] + A[1, 2]
    d = float(geo.d_px)
    r = int(round(d * half_cells * 1.25))
    side = 2 * r
    S = np.array([[1.0, 0.0, -(cx - r)],
                  [0.0, 1.0, -(cy - r)],
                  [0.0, 0.0, 1.0]], np.float64)
    T = S @ A @ np.asarray(H, np.float64)
    pad = cv2.warpPerspective(src, T, (side, side), flags=cv2.INTER_LINEAR,
                              borderValue=0)
    # 像素系 y 向下，场地 +y 向上 → 方向 (cosθ, −sinθ) / (sinθ, cosθ)
    th = math.radians(sol["theta"])
    u = np.array([math.cos(th), -math.sin(th)]) * d
    v = np.array([math.sin(th), math.cos(th)]) * d
    c = np.array([r, r], float)
    for t in np.arange(-3.0, 3.0 + 1e-6, 1.0):
        p0, p1 = c + u * t - v * 3.0, c + u * t + v * 3.0
        cv2.line(pad, tuple(np.round(p0).astype(int)), tuple(np.round(p1).astype(int)),
                 C_GRID, 1, cv2.LINE_AA)
        q0, q1 = c + v * t - u * 3.0, c + v * t + u * 3.0
        cv2.line(pad, tuple(np.round(q0).astype(int)), tuple(np.round(q1).astype(int)),
                 C_GRID, 1, cv2.LINE_AA)
    cv2.drawMarker(pad, (r, r), C_TARGET, cv2.MARKER_CROSS, 24, 2)
    big = cv2.resize(pad, (panel, panel), interpolation=cv2.INTER_NEAREST)
    tag = "card x6 zoom" if matcher.accept(sol) else "card x6 zoom (rejected)"
    cv2.putText(big, tag, (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (240, 240, 240), 1,
                cv2.LINE_AA)
    h, w = dst.shape[:2]
    m = 10
    y, x = h - panel - m, w - panel - m
    roi = dst[y:y + panel, x:x + panel]
    cv2.addWeighted(big, 0.9, roi, 0.1, 0, roi)
    cv2.rectangle(dst, (x, y), (x + panel, y + panel), (200, 200, 200), 1)
    return dst


def hud(frame, rows: list[tuple[str, tuple]], y0: int = 24):
    y = y0
    for text, color in rows:
        cv2.putText(frame, text, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3,
                    cv2.LINE_AA)
        cv2.putText(frame, text, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1,
                    cv2.LINE_AA)
        y += 22
    return frame


def _make_loc(r: dict, fps: float, hinfo: dict | None = None) -> Localization:
    """把一行记录还原成 Localization（供 viz.draw_overlay 复用同一个 HUD）。"""
    th = r.get("theta")
    loc = Localization(valid=bool(r.get("valid")), frame_index=int(r["k"]),
                       timestamp=float(r["k"]) / max(fps, 1e-9))
    if hinfo:
        loc.origin_id = hinfo.get("origin_id")
        loc.corner_ids = list(hinfo.get("ids") or [])
    loc.x, loc.y, loc.theta_deg = r.get("x"), r.get("y"), th
    if th is not None:
        loc.dir_x, loc.dir_y = math.cos(math.radians(th)), math.sin(math.radians(th))
    loc.flags = ["grid_matched"] if r.get("valid") else []
    loc.reason = r.get("reason", "")
    if r.get("x") is not None:
        loc.diagnostics = {"grid": {k: r.get(k) for k in
                                    ("gap", "sharp", "margin", "ncc", "n_bad",
                                     "beta", "rot", "seed_spread_mm", "t")}}
    return loc


def render_pass(locator, video: str, s0: int, s1: int, rows: list[dict], hs: dict,
                out_mp4: str, fps: float, scale: float, quiet: bool) -> None:
    byk = {r["k"]: r for r in rows}
    cap = _open_cap(video)
    _skip(cap, s0)
    writer = tmp = None
    t0 = time.time()
    try:
        for k in range(s0, s1):
            okr, frame = cap.read()
            if not okr or frame is None:
                break
            r = byk.get(k) or {"k": k, "hgap": 9999, "valid": False,
                               "reason": "not_processed"}
            vis = frame
            if r.get("x") is not None:
                H, _ = interpolate_h(hs, k, 9999)
                inv = np.linalg.inv(np.asarray(H, np.float64))
                loc = _make_loc(r, fps, hs.get(k))
                vis = draw_overlay(frame, loc, {}, locator.cfg,
                                   debug={"H": np.asarray(H, np.float64), "H_inv": inv})
                sol = {"x": r["x"], "y": r["y"], "theta": r["theta"],
                       "gap": r["gap"], "sharp": r["sharp"]}
                draw_card(vis, locator.grid_matcher, inv, sol)
                # 特写窗从**未标注的原图**采样，画到已标注的 vis 上
                draw_zoom_panel(vis, frame, locator.grid_matcher, H, sol)
                lines = [(f"{'ACCEPT' if r['valid'] else 'REJECT'}"
                          f"   hgap={r.get('hgap')}",
                          C_OK if r["valid"] else C_BAD)]
                # draw_overlay 自己画了几行就读几行：无效帧只有 2 行（frame/reason），
                # 有效帧 6~8 行。写死 8 会在 REJECT 帧上留一道空洞。
                hud(vis, lines, y0=24 + 22 * len(_hud_lines(loc)))
            else:
                hud(vis, [(f"frame {k}  INVALID", C_BAD),
                          (f"reason: {r.get('reason', '')}", C_BAD),
                          (f"hgap={r.get('hgap')}", C_WARN)])
            if scale != 1.0:
                vis = cv2.resize(vis, None, fx=scale, fy=scale,
                                 interpolation=cv2.INTER_AREA)
            if writer is None:
                h, w = vis.shape[:2]
                writer, tmp = _writer(out_mp4, (w, h), fps)
            writer.write(vis)
            if not quiet and (k - s0 + 1) % 20 == 0:
                print(f"  [3/3] {k - s0 + 1}/{s1 - s0} 帧  {time.time() - t0:.0f}s",
                      end="\r", flush=True)
    finally:
        cap.release()
        if writer is not None:
            writer.release()
            if tmp and os.path.exists(tmp):
                os.replace(tmp, out_mp4)
    if not quiet:
        print(f"  [3/3] 完成：{s1 - s0} 帧  {time.time() - t0:.0f}s")


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="正式接口 + 真实视频：格点匹配通道跑通并合成审阅视频")
    ap.add_argument("--video", required=True, help="视频路径（4K 4:2:0 亦可）")
    ap.add_argument("--config", default=None, help="YAML 配置（缺省用根目录 config.yaml）")
    ap.add_argument("--out", default=os.path.join(ROOT, "out", "grid"), help="输出目录")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--end", type=int, default=0, help="0 = 到结尾")
    ap.add_argument("--scale", type=float, default=0.5, help="输出视频缩放（0.5 = 半尺寸）")
    ap.add_argument("--h-interp", type=int, default=60,
                    help="四角缺失时用最近实测单应的最大帧距；0 = 不插值")
    ap.add_argument("--scales", default=None,
                    help="检测多尺度，如 '1,2'。4K 远景（角标记 10~34px）必须上采样，"
                         "否则四角检出率仅四成")
    ap.add_argument("--clahe", action="store_true", help="开启 CLAHE（光照不均时）")
    ap.add_argument("--workers", type=int, default=4, help="求解并行进程数")
    ap.add_argument("--threads", type=int, default=2,
                    help="每进程的 OpenCV 线程数。并发时别开大：会抢内存带宽")
    ap.add_argument("--rerender", action="store_true",
                    help="只重做渲染：复用已有 grid_*.json 的解与 grid_h_*.npz 的单应，"
                         "跳过求解（约 2 分钟而不是 40 分钟）")
    ap.add_argument("--tag", default="", help="输出文件名后缀")
    ap.add_argument("--quiet", action="store_true")
    ns = ap.parse_args(argv)

    cfg_path = ns.config
    if cfg_path is None and os.path.isfile(os.path.join(ROOT, "config.yaml")):
        cfg_path = os.path.join(ROOT, "config.yaml")
    cfg = load_config(cfg_path)
    cfg.grid.enable = True
    if ns.scales:
        cfg.detection.scales = [float(t) for t in
                                ns.scales.replace(";", ",").split(",") if t.strip()]
    if ns.clahe:
        cfg.detection.clahe = True
    if ns.threads > 0:
        cv2.setNumThreads(int(ns.threads))
    locator = create_locator(cfg)

    name = os.path.splitext(os.path.basename(ns.video))[0]
    os.makedirs(ns.out, exist_ok=True)
    meta = preflight(ns.video)
    s0 = int(ns.start)
    s1 = int(ns.end) if ns.end else meta.frames
    fps = float(meta.fps_declared or 30.0)
    print(f"=== {name}  帧 {s0}..{s1 - 1}  {meta.frames} 帧 @ {fps:.2f}fps  "
          f"{meta.width}×{meta.height}")
    print(f"    detect: scales={cfg.detection.scales} clahe={cfg.detection.clahe}")
    print(f"    grid  : κ={cfg.grid.kappa} m={cfg.grid.inset} d={cfg.grid.d_px}px "
          f"门限 sharp≥{cfg.grid.sharp_min} gap≥{cfg.grid.gap_min}")

    t_all = time.time()
    json_path = os.path.join(ns.out, f"grid_{name}{ns.tag}.json")
    hs_path = os.path.join(ns.out, f"grid_h_{name}{ns.tag}.npz")
    if ns.rerender:
        if not os.path.isfile(json_path):
            raise SystemExit(f"--rerender 需要已有结果：{json_path}")
        rows = json.load(open(json_path, encoding="utf-8"))["rows"]
        if os.path.isfile(hs_path):
            hs = load_hs(hs_path)
            print(f"    rerender：复用 {len(rows)} 行解 + {len(hs)} 帧单应")
        else:
            print("    rerender：无单应缓存，重算第 1 遍")
            hs = pass_homography(locator, ns.video, s0, s1, ns.quiet)
            save_hs(hs_path, hs)
    else:
        hs = pass_homography(locator, ns.video, s0, s1, ns.quiet)
        save_hs(hs_path, hs)
        rows = solve_parallel(ns.video, s0, s1, hs, cfg, int(ns.h_interp), fps,
                              int(ns.workers), int(ns.threads), ns.quiet)
    out_mp4 = os.path.join(ns.out, f"grid_review_{name}{ns.tag}.mp4")
    render_pass(locator, ns.video, s0, s1, rows, hs, out_mp4, fps, ns.scale, ns.quiet)

    if not ns.rerender:
        json.dump(dict(video=name, fps=fps, n_frames=meta.frames, start=s0, end=s1,
                       config=cfg_path, detection=dict(vars(cfg.detection)),
                       grid=dict(vars(cfg.grid)), rows=rows),
                  open(json_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    n = len(rows)
    with_h = [r for r in rows if r.get("x") is not None]
    nacc = sum(1 for r in rows if r.get("valid"))
    nh = sum(1 for r in rows if r.get("ok_H"))
    nhi = sum(1 for r in rows if 0 < r.get("hgap", 9999) < 9999)
    nnoh = sum(1 for r in rows if r.get("hgap", 9999) == 9999)
    ts = [r["t"] for r in rows if isinstance(r.get("t"), (int, float))]
    gaps = [r["gap"] for r in with_h if isinstance(r.get("gap"), float)]
    sharps = [r["sharp"] for r in with_h if isinstance(r.get("sharp"), float)]
    summary = {
        "frames": n, "measured_H": nh, "interpolated_H": nhi, "no_H": nnoh,
        "solved": len(with_h), "accepted": nacc,
        "accept_rate": round(nacc / max(n, 1), 4),
        "accept_rate_of_solved": round(nacc / max(len(with_h), 1), 4),
        "t_median_s": round(float(np.median(ts)), 3) if ts else None,
        "gap_median": round(float(np.median(gaps)), 4) if gaps else None,
        "sharp_median": round(float(np.median(sharps)), 4) if sharps else None,
        "wall_clock_s": round(time.time() - t_all, 1),
        "video_out": out_mp4,
    }
    json.dump(summary, open(os.path.join(ns.out, f"grid_summary_{name}{ns.tag}.json"),
                            "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print("\n=== 汇总 ===")
    print(f"帧数 {n}  实测单应 {nh} ({nh / max(n, 1):.1%})  插值 {nhi}  无 H {nnoh}")
    print(f"出解 {len(with_h)}  接受 {nacc} = {nacc / max(n, 1):.1%}"
          f"（占出解帧 {nacc / max(len(with_h), 1):.1%}）")
    if summary["t_median_s"]:
        print(f"单帧求解 中位 {summary['t_median_s']:.2f}s   "
              f"总墙钟 {summary['wall_clock_s']:.0f}s")
    print(f"视频 {out_mp4}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
