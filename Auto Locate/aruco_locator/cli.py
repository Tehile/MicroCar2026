"""离线批处理 CLI。

用法：
    python -m aruco_locator.cli --source video.mp4 --out out
    python -m aruco_locator.cli --source D:/shots --json out/results.jsonl --show
    python -m aruco_locator.cli --source camera:0 --show
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter

import cv2
import numpy as np

from .api import CSV_FIELDS, ArucoLocator, Localization
from .config import load_config
from .sources import imwrite_unicode, iter_source, parse_source


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aruco_locator",
        description="ArUco 场地定位 —— 解算场地中央 0 号标记的位置与方向",
    )
    p.add_argument("--source", "-s", required=True, help="图片 / 图片目录 / 视频文件 / camera:0")
    p.add_argument("--config", "-c", default=None, help="YAML 配置文件（缺省用内置默认值）")
    p.add_argument("--out", "-o", default=None, help="输出目录（自动创建）")
    p.add_argument("--json", dest="json_path", default=None, help="逐帧结果 JSONL 路径")
    p.add_argument("--csv", dest="csv_path", default=None, help="逐帧结果 CSV 路径")
    p.add_argument("--save-video", default=None, help="叠加后的视频输出路径（.mp4）")
    p.add_argument("--save-annotated", action="store_true", help="把每一帧叠加图单独存成图片")
    p.add_argument("--show", action="store_true", help="实时显示叠加画面（按 q 退出）")
    p.add_argument("--max-frames", type=int, default=None, help="最多处理多少帧")
    p.add_argument("--start", type=int, default=0, help="从第几帧开始")
    p.add_argument("--recursive", action="store_true", help="目录输入时递归查找")
    p.add_argument("--camera-width", type=int, default=None)
    p.add_argument("--camera-height", type=int, default=None)
    p.add_argument("--print-every", type=int, default=30, help="每多少帧打印一次进度，0 表示不打印")
    p.add_argument("--grid", action="store_true",
                   help="开启格点匹配第二通道（0 号标记被 ArUco 漏检时兜底，+2.8s/帧）")
    p.add_argument("--grid-only", action="store_true",
                   help="只走格点匹配通道（不需要 0 号标记被 ArUco 检出，但需四角齐全）")
    p.add_argument("--quiet", action="store_true", help="只输出最终统计")
    return p


def _ensure_dir(path: str | None) -> None:
    if path:
        os.makedirs(path, exist_ok=True)


def _open_writer(path: str, size: tuple[int, int], fps: float):
    """非 ASCII 路径下 VideoWriter 可能失败，先写临时文件再改名。"""
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    tmp = os.path.join(os.path.dirname(os.path.abspath(path)) or ".", "__tmp_out.mp4")
    writer = cv2.VideoWriter(tmp, fourcc, fps, size)
    return writer, tmp, path


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    if args.grid or args.grid_only:
        cfg.grid.enable = True
    locator = ArucoLocator(cfg)

    kind, _ = parse_source(args.source)
    out_dir = args.out
    _ensure_dir(out_dir)

    if out_dir:
        args.json_path = args.json_path or os.path.join(out_dir, "results.jsonl")
        args.csv_path = args.csv_path or os.path.join(out_dir, "results.csv")

    json_fp = open(args.json_path, "w", encoding="utf-8") if args.json_path else None
    csv_fp = open(args.csv_path, "w", encoding="utf-8-sig", newline="") if args.csv_path else None
    csv_writer = csv.DictWriter(csv_fp, fieldnames=CSV_FIELDS) if csv_fp else None
    if csv_writer:
        csv_writer.writeheader()

    writer = tmp_video = None
    total = valid = 0
    reasons: Counter = Counter()
    rms_vals: list[float] = []
    t0 = time.time()

    try:
        for item in iter_source(
            args.source,
            start=args.start,
            max_frames=args.max_frames,
            recursive=args.recursive,
            camera_width=args.camera_width,
            camera_height=args.camera_height,
        ):
            loc: Localization = (
                locator.locate_grid(item.frame, None, item.index, item.timestamp)
                if args.grid_only
                else locator.locate(item.frame, item.index, item.timestamp)
            )
            total += 1
            if loc.valid:
                valid += 1
                if loc.rms_field is not None:
                    rms_vals.append(loc.rms_field)
            else:
                reasons[loc.reason or "unknown"] += 1

            if json_fp:
                json_fp.write(loc.to_json() + "\n")
            if csv_writer:
                csv_writer.writerow({k: v for k, v in loc.to_row().items() if k in CSV_FIELDS})

            need_visual = args.show or args.save_video or args.save_annotated
            vis = None
            if need_visual:
                vis = locator.annotate(item.frame, loc)
                if args.save_annotated and out_dir:
                    imwrite_unicode(os.path.join(out_dir, f"annotated_{item.index:06d}.png"), vis)
                if args.save_video:
                    if writer is None:
                        h, w = vis.shape[:2]
                        fps = 25.0
                        if kind == "video":
                            cap_fps = cv2.VideoCapture(args.source).get(cv2.CAP_PROP_FPS)
                            fps = float(cap_fps) if cap_fps and cap_fps > 0 else 25.0
                        writer, tmp_video, _ = _open_writer(args.save_video, (w, h), fps)
                    writer.write(vis)
                if args.show:
                    cv2.imshow("aruco_locator", vis)
                    if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                        break

            if not args.quiet and args.print_every and total % args.print_every == 0:
                print(loc.summary())
            elif args.quiet and total % 200 == 0:
                print(f"... {total} frames", file=sys.stderr)

    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
    finally:
        if json_fp:
            json_fp.close()
        if csv_fp:
            csv_fp.close()
        if writer is not None:
            writer.release()
            if tmp_video and os.path.exists(tmp_video):
                os.replace(tmp_video, args.save_video)
        if args.show:
            cv2.destroyAllWindows()

    dt = time.time() - t0
    print("\n=== 统计 ===")
    print(f"帧数 {total}  有效 {valid}  无效 {total - valid}  ({valid / max(total, 1):.1%})")
    if reasons:
        print("无效原因：" + "，".join(f"{k}×{v}" for k, v in reasons.most_common()))
    if rms_vals:
        arr = np.asarray(rms_vals)
        print(f"rms_field：均值 {arr.mean():.5f}  中位 {np.median(arr):.5f}  最大 {arr.max():.5f}")
    print(f"耗时 {dt:.2f}s  平均 {total / max(dt, 1e-9):.1f} 帧/秒")
    if args.json_path:
        print(f"JSONL: {args.json_path}")
    if args.csv_path:
        print(f"CSV  : {args.csv_path}")
    if args.save_video:
        print(f"视频 : {args.save_video}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
