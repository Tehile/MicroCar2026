#!/usr/bin/env python
"""棋盘格相机标定：输出无标定路线可选用的内参与畸变系数。

主路径（无标定单应）**不需要**这个脚本；只有在下面两种情况下才需要：

* 镜头畸变明显（广角、近距离、标记贴画面边缘），希望先校正再解算；
* 想用 solvePnP 路线做交叉校验。

用法
----
从已有图片标定（图片放在一个目录里，文件名排序无所谓）::

    python calibrate_camera.py --images calib/shots --cols 9 --rows 6 --square-mm 25 \\
        --out calib/intrinsics.yaml

用摄像头现场采集（SPACE 拍一张、C 标定并退出、ESC 放弃）::

    python calibrate_camera.py --camera 0 --cols 9 --rows 6 --square-mm 25 \\
        --out calib/intrinsics.yaml

标定结果同时写一份 .npz（`camera_matrix` / `dist_coeffs`），
`aruco_locator.load_intrinsics` 两种格式都能读。

注意
----
* `--cols/--rows` 指的是棋盘**内角点**数量（即方格数减一）。
* 采集时请让棋盘覆盖画面四角与中央、多姿态多角度，否则畸变系数会不可信。
* 标定用的分辨率必须和定位时一致，否则内参不可直接使用。
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

import cv2
import numpy as np

from aruco_locator.sources import imread_unicode, imwrite_unicode, parse_source, open_source

CRITERIA = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 40, 1e-4)


# --------------------------------------------------------------------------- #
# 棋盘角点
# --------------------------------------------------------------------------- #
def make_object_points(cols: int, rows: int, square_mm: float) -> np.ndarray:
    """棋盘内角点在"棋盘平面"上的三维坐标（z=0）。"""
    grid = np.zeros((rows * cols, 3), np.float32)
    grid[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2)
    return grid * float(square_mm)


def find_corners(gray: np.ndarray, cols: int, rows: int, sb: bool, fast: bool):
    """找棋盘内角点并做亚像素精化。返回 (corners, 用的方法名) 或 (None, 原因)。"""
    if sb:
        flags = cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY
        if fast:
            flags = cv2.CALIB_CB_FAST_CHECK
        found, corners = cv2.findChessboardCornersSB(gray, (cols, rows), flags=flags)
        if found:
            return corners.reshape(-1, 1, 2).astype(np.float32), "SB"
    flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE
    if fast:
        flags |= cv2.CALIB_CB_FAST_CHECK
    found, corners = cv2.findChessboardCorners(gray, (cols, rows), flags=flags)
    if not found:
        return None, "未检出棋盘"
    corners = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), CRITERIA)
    return corners, "classic"


# --------------------------------------------------------------------------- #
# 采集
# --------------------------------------------------------------------------- #
def collect_from_camera(index: str, cols: int, rows: int, want: int, sb: bool) -> list[np.ndarray]:
    cap = open_source(f"camera:{index}")
    if cap is None:  # pragma: no cover
        raise RuntimeError("打不开摄像头")
    board = (cols, rows)
    shots: list[np.ndarray] = []
    print(f"摄像头 {index}：SPACE 拍摄（当前 0/{want}），C 开始标定，ESC 取消。")
    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            found, _ = find_corners(gray, cols, rows, sb, fast=True)
            view = frame.copy()
            tip = f"{len(shots)}/{want}"
            cv2.putText(view, ("DETECTED  " if found else "searching ") + tip,
                        (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                        (0, 220, 0) if found else (0, 160, 255), 2, cv2.LINE_AA)
            cv2.imshow("calibrate_camera", view)
            key = cv2.waitKey(1) & 0xFF
            if key == 27:  # ESC
                print("已取消。")
                return []
            if key == ord(" ") and found:
                shots.append(frame.copy())
                print(f"  已拍摄 {len(shots)}/{want}")
            if key == ord("c") or (want and len(shots) >= want and key == 13):
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()
    return shots


# --------------------------------------------------------------------------- #
# 标定
# --------------------------------------------------------------------------- #
def calibrate(images: list[np.ndarray], objp: np.ndarray, cols: int, rows: int, sb: bool, verbose: bool):
    """返回 (K, dist, rms, image_size, 每张的 rms, 用到的张数)。"""
    obj_points, img_points, size, used = [], [], None, 0
    per_image = []
    for i, img in enumerate(images):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        if size is None:
            size = (gray.shape[1], gray.shape[0])
        elif (gray.shape[1], gray.shape[0]) != size:
            if verbose:
                print(f"  [跳过] 第 {i} 张分辨率 {gray.shape[1]}x{gray.shape[0]} 与首张 {size[0]}x{size[1]} 不一致")
            continue
        corners, how = find_corners(gray, cols, rows, sb, fast=False)
        if corners is None:
            if verbose:
                print(f"  [跳过] 第 {i} 张：{how}")
            continue
        obj_points.append(objp)
        img_points.append(corners)
        used += 1

    if used < 6:
        raise RuntimeError(
            f"可用棋盘图片只有 {used} 张，至少需要 6 张（建议 15 张以上，且覆盖画面四角）"
        )

    rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(
        obj_points, img_points, size, None, None, flags=cv2.CALIB_FIX_K3 if len(obj_points) < 12 else 0
    )

    # 逐张重投影误差，便于发现姿态覆盖不全
    for idx, (op, ip, rv, tv) in enumerate(zip(obj_points, img_points, rvecs, tvecs)):
        proj, _ = cv2.projectPoints(op, rv, tv, K, dist)
        e = float(np.sqrt(np.mean(np.sum((proj.reshape(-1, 2) - ip.reshape(-1, 2)) ** 2, axis=1))))
        per_image.append((idx, e))
    return K, dist, float(rms), size, per_image, used


def write_intrinsics(path: str, K: np.ndarray, dist: np.ndarray, size, rms: float, extra: dict):
    """写 YAML（人可读）与 npz（可直接被 load_intrinsics 读取）两份。"""
    base, ext = os.path.splitext(path)
    ext = ext.lower() or ".yaml"
    payload = {
        "camera_matrix": [[float(v) for v in row] for row in K],
        "dist_coeffs": [float(v) for v in np.asarray(dist).ravel()],
        "image_size": [int(size[0]), int(size[1])],
        "reprojection_error": float(rms),
    }
    payload.update(extra)

    if ext in (".yaml", ".yml"):
        import yaml

        with open(path, "w", encoding="utf-8") as fp:
            yaml.safe_dump(payload, fp, allow_unicode=True, sort_keys=False)
        npz_path = base + ".npz"
    elif ext == ".npz":
        npz_path = path
        path = base + ".yaml"
        import yaml

        with open(path, "w", encoding="utf-8") as fp:
            yaml.safe_dump(payload, fp, allow_unicode=True, sort_keys=False)
    else:
        raise ValueError(f"输出后缀应为 .yaml / .yml / .npz，收到 {ext}")

    np.savez(
        npz_path,
        camera_matrix=K.astype(np.float64),
        dist_coeffs=np.asarray(dist, dtype=np.float64).reshape(1, -1),
    )
    return path, npz_path


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="棋盘格相机标定", formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_argument_group("图像来源（二选一）")
    src.add_argument("--images", help="棋盘图片所在目录（或 glob，如 calib/*.jpg）")
    src.add_argument("--camera", help="摄像头编号，如 0；现场采集")
    ap.add_argument("--cols", type=int, default=9, help="棋盘内角点列数（默认 9）")
    ap.add_argument("--rows", type=int, default=6, help="棋盘内角点行数（默认 6）")
    ap.add_argument("--square-mm", type=float, default=25.0, help="棋盘方格实际边长（mm）")
    ap.add_argument("--shots", type=int, default=20, help="摄像头模式目标张数")
    ap.add_argument("--no-sb", action="store_true", help="不用 findChessboardCornersSB，改用经典方法")
    ap.add_argument("--out", default="calib/intrinsics.yaml", help="输出路径（同时写 .yaml 与 .npz）")
    ap.add_argument("--save-shots", help="把采集到的图片也存一份到该目录")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    if not args.images and not args.camera:
        ap.error("请给出 --images 或 --camera")

    sb = not args.no_sb
    cols, rows = int(args.cols), int(args.rows)
    objp = make_object_points(cols, rows, float(args.square_mm))

    # 1) 取图
    frames: list[np.ndarray] = []
    if args.camera:
        frames = collect_from_camera(str(args.camera), cols, rows, int(args.shots), sb)
        if not frames:
            print("没有采集到图片，什么都没做。")
            return 1
    # --images 也接受来自摄像头的图片；两者都给时以 images 为准并追加
    if args.images:
        pattern = args.images
        if os.path.isdir(pattern):
            pattern = os.path.join(pattern, "*")
        files = sorted(glob.glob(pattern))
        files = [f for f in files if os.path.splitext(f)[1].lower() in
                 (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp")]
        if not files:
            print(f"{args.images} 下没有找到图片。")
            return 1
        for f in files:
            img = imread_unicode(f)
            if img is None:
                print(f"  [跳过] 读不到 {f}")
                continue
            frames.append(img)
        print(f"读入 {len(frames)} 张图片。")

    if args.save_shots:
        os.makedirs(args.save_shots, exist_ok=True)
        for i, fr in enumerate(frames):
            imwrite_unicode(os.path.join(args.save_shots, f"shot{i:03d}.png"), fr)
        print(f"采集图已存到 {args.save_shots}")

    # 2) 标定
    K, dist, rms, size, per_image, used = calibrate(frames, objp, cols, rows, sb, args.verbose)

    # 3) 报告
    print(f"\n用上 {used} 张，分辨率 {size[0]}×{size[1]}，整体重投影 RMS = {rms:.4f} px")
    print(f"内参 K =\n{np.array2string(K, precision=3, suppress_small=True)}")
    print(f"畸变系数 = {np.round(np.asarray(dist).ravel(), 5).tolist()}")
    worst = sorted(per_image, key=lambda t: -t[1])[:5]
    print("重投影最差的 5 张：" + "，".join(f"#{i}:{e:.3f}px" for i, e in worst))
    if rms > 1.0:
        print("⚠ RMS 偏大（>1px）：检查棋盘是否平整、方格边长是否填对、是否有运动模糊。")
    elif rms > 0.5:
        print("提示：RMS 略大，可再多采几张不同姿态。")

    # 4) 落盘
    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    ypath, npath = write_intrinsics(
        args.out, K, dist, size, rms,
        {"board_inner_corners": [cols, rows], "square_mm": float(args.square_mm), "n_images": int(used)},
    )
    print(f"\n已写出：\n  {ypath}\n  {npath}")
    print("在 config.yaml 里这样启用：")
    print(f"  camera:\n    undistort: true\n    intrinsic_file: {npath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
