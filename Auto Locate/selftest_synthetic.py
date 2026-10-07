#!/usr/bin/env python
"""合成场地自检：不依赖实拍素材，验证解算的正确性与精度。

做法：
    1. 在"俯视平面图"上按真实尺寸渲染场地与 5 个标记
       （角标记朝向随机、ID 到角的对应关系随机，模拟"朝向和标号顺序都不固定"）；
    2. 施加一个已知的随机透视变换，模拟相机拍摄（可叠加模糊/噪声/光照不均/镜头畸变）；
    3. 用几何关系独立算出真值（不调用本包的几何代码）；
    4. 跑 ArucoLocator，比较 x / y / theta，并对比"精化开/关"。

用法：
    python selftest_synthetic.py                    # 20 个随机场景
    python selftest_synthetic.py -n 50 --ideal      # 理想成像，50 个场景
    python selftest_synthetic.py --distort          # 叠加镜头畸变，验证标定路线
    python selftest_synthetic.py --save out          # 保存叠加调试图
"""

from __future__ import annotations

import argparse
import math
import os
import sys

import cv2
import numpy as np

from aruco_locator.api import ArucoLocator
from aruco_locator.config import LocatorConfig, load_config
from aruco_locator.geometry import MARKER_LOCAL
from aruco_locator.sources import imwrite_unicode

FIELD_MM = 1000.0  # 场地边长（合成用，实际尺寸不影响归一化结果）
# 印刷规格：SVG 为 50mm×50mm、viewBox 6×6 模块（1 模块黑框 + 4×4 码区），
# 所以"标记的黑色方块"边长 = 50mm，内部 4×4 码区 = 33.33mm。
# cv2.aruco 检出的四角正是这个 50mm 黑色方块的四个角。
MARKER_MM = 50.0
MARKER_CODE_MM = 50.0 * 4.0 / 6.0
MARGIN_MM = 160.0
# 角标记从场地角点向外伸展的最大范围（内顶点在角点、中心在半对角线处，最远顶点约 2r）
EXPAND_MM = MARKER_MM * 1.6


def _in_bounds(pts: np.ndarray, w: int, h: int, pad: float = 6.0) -> bool:
    return bool(
        pts[:, 0].min() > pad and pts[:, 0].max() < w - pad and pts[:, 1].min() > pad and pts[:, 1].max() < h - pad
    )

CORNERS = ("A", "B", "C", "D")  # 平面图坐标系（y 向下）里的 左上/右上/右下/左下
CORNER_MM = {"A": (0.0, 0.0), "B": (FIELD_MM, 0.0), "C": (FIELD_MM, FIELD_MM), "D": (0.0, FIELD_MM)}
CORNER_MM_ARR = np.array([CORNER_MM[c] for c in CORNERS], dtype=np.float64)  # 与 CORNERS 同序
# 标记四角相对中心的偏移，**印刷系（y 向下）**，顺序 TL,TR,BR,BL —— 渲染必须用这一套，
# 否则图案被镜像，检不出来。几何解算用的 MARKER_LOCAL_EDGE 是 y 向上的，两者不要混。
PRINT_OFF = np.array([[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5]], dtype=np.float64)
# 标记朝场地中心那个顶点在"印刷四角"里的序号
INNER_INDEX = {"A": 2, "B": 3, "C": 0, "D": 1}


def rot(theta: float) -> np.ndarray:
    c, s = math.cos(theta), math.sin(theta)
    return np.array([[c, -s], [s, c]])


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def marker_quad(center_mm, phi: float, size_mm: float = MARKER_MM) -> np.ndarray:
    """标记四角在平面图里的 mm 坐标，顺序 TL,TR,BR,BL（印刷系，y 向下）。"""
    return (rot(phi) @ (PRINT_OFF * size_mm).T).T + np.asarray(center_mm)


def _marker_bitmap(dictionary, mid: int, black_px: float) -> np.ndarray:
    """生成标记位图：整幅位图就是标记的黑色方块（含 1 模块黑框），边长约 black_px。"""
    side = max(8, int(round(black_px)))
    return cv2.aruco.generateImageMarker(dictionary, mid, side)


def render_plane(rng, args, dictionary):
    """渲染俯视平面图，返回 (图像, 场地四边形像素坐标, 真值信息)。"""
    mm_per_px = args.mm_per_px
    ss = args.supersample
    scale = mm_per_px * ss
    size_mm = FIELD_MM + 2 * MARGIN_MM
    W = int(round(size_mm * scale))
    img = np.full((W, W, 3), 255, np.uint8)

    def to_px(p_mm):
        return (np.asarray(p_mm) + MARGIN_MM) * scale

    # 场地边框（仅是给人看的，算法不使用）
    a, b = to_px((0, 0)), to_px((FIELD_MM, FIELD_MM))
    cv2.rectangle(img, (int(a[0]), int(a[1])), (int(b[0]), int(b[1])), (215, 215, 215), max(1, int(scale * 2)))

    ids_layout = list(rng.permutation([1, 2, 3, 4]))
    corner_info = {}
    for corner, mid in zip(CORNERS, ids_layout):
        phi = math.radians(rng.uniform(-args.rot_jitter, args.rot_jitter))
        inner_off = PRINT_OFF[INNER_INDEX[corner]] * MARKER_MM
        center_mm = np.asarray(CORNER_MM[corner]) - rot(phi) @ inner_off
        quad_mm = marker_quad(center_mm, phi)
        _draw_marker(img, dictionary, int(mid), quad_mm, to_px, scale)
        corner_info[corner] = {"id": int(mid), "center_mm": center_mm, "phi": phi}

    # 0 号标记
    tx, ty = rng.uniform(*args.target_range), rng.uniform(*args.target_range)
    theta = math.radians(rng.uniform(0.0, 360.0))
    target_center_mm = np.array([tx * FIELD_MM, ty * FIELD_MM])
    target_quad_mm = marker_quad(target_center_mm, theta)
    _draw_marker(img, dictionary, 0, target_quad_mm, to_px, scale)

    field_quad_px = np.array([to_px(CORNER_MM[c]) for c in CORNERS], dtype=np.float64)

    if ss > 1:
        img = cv2.resize(img, (W // ss, W // ss), interpolation=cv2.INTER_AREA)
        field_quad_px = field_quad_px / ss

    return img, field_quad_px, {
        "ids": {c: corner_info[c]["id"] for c in CORNERS},
        "phi": {c: corner_info[c]["phi"] for c in CORNERS},
        "target_center_mm": target_center_mm,
        "target_theta": theta,
        "mm_to_px": (mm_per_px, MARGIN_MM * mm_per_px),  # 平面图 mm → 像素 的仿射参数
    }


def _draw_marker(img, dictionary, mid, quad_mm, to_px, scale):
    """把一个标记按目标四边形贴到平面图上（只在局部 ROI 内做透视变换）。"""
    dst = np.array([to_px(p) for p in quad_mm], dtype=np.float32)
    black_px = MARKER_MM * scale
    bmp = _marker_bitmap(dictionary, mid, black_px)
    side = bmp.shape[0]
    # 整幅位图 = 标记黑色方块，四角直接对应目标四边形的四角
    src = np.array([[0, 0], [side, 0], [side, side], [0, side]], dtype=np.float32)

    pad = 4
    x0 = max(0, int(math.floor(dst[:, 0].min())) - pad)
    y0 = max(0, int(math.floor(dst[:, 1].min())) - pad)
    x1 = min(img.shape[1], int(math.ceil(dst[:, 0].max())) + pad)
    y1 = min(img.shape[0], int(math.ceil(dst[:, 1].max())) + pad)
    if x1 <= x0 or y1 <= y0:
        return
    M = cv2.getPerspectiveTransform(src, dst - np.array([x0, y0], dtype=np.float32))
    roi = cv2.warpPerspective(
        bmp, M, (x1 - x0, y1 - y0), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=255
    )
    target = img[y0:y1, x0:x1]
    if target.ndim == 3:
        roi = cv2.cvtColor(roi, cv2.COLOR_GRAY2BGR)
    img[y0:y1, x0:x1] = np.minimum(target, roi)


# --------------------------------------------------------------------------- #
# 相机
# --------------------------------------------------------------------------- #
def camera_view(plane: np.ndarray, field_quad_px: np.ndarray, rng, args):
    """把平面图映射到画布，返回 (图像, 平面图→画布 的单应)。

    两点注意：
    1. 先用 2 倍画布做透视变换、再用 INTER_AREA 缩小。直接双线性下采样会产生混叠
       （真实镜头有光学低通，不会这么糟），那会让小标记的解码变得不公平。
    2. 取景必须让"场地 + 四个角标记的整体外扩范围"都落在画面内，否则标记会被裁掉，
       检测不到——那是取景问题，不是算法问题。
    """
    W, H = args.canvas
    ss = int(max(1, getattr(args, "cam_supersample", 2)))
    base = min(W, H) * args.fill
    cx = W / 2 + rng.uniform(-0.05, 0.05) * W
    cy = H / 2 + rng.uniform(-0.05, 0.05) * H
    ang = math.radians(rng.uniform(-args.cam_roll, args.cam_roll))
    jitter = rng.uniform(-1, 1, (4, 2)) * (args.tilt * base)

    # 场地四角 + 四个角标记向外扩展后的范围（平面图像素坐标）
    center = field_quad_px.mean(axis=0)
    outward = field_quad_px - center
    outward = outward / np.linalg.norm(outward, axis=1, keepdims=True)
    expand_px = field_quad_px + outward * (EXPAND_MM * args.mm_per_px)

    u = np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]], dtype=np.float64) * (base / 2.0)
    rot_u = (rot(ang) @ u.T).T

    Hm = None
    shrink = 1.0
    for _ in range(15):
        target = rot_u * shrink + np.array([cx, cy]) + jitter * shrink
        Hm = cv2.getPerspectiveTransform(field_quad_px.astype(np.float32), target.astype(np.float32))
        if _in_bounds(cv2.perspectiveTransform(expand_px.reshape(-1, 1, 2), Hm).reshape(-1, 2), W, H):
            break
        shrink *= 0.92

    if ss > 1:
        # INTER_AREA 把放大画布缩回 ss 倍时，输出像素 i 采样输入区间 [ss*i, ss*(i+1))，
        # 其"质量中心"落在输入坐标 ss*i + (ss-1)/2 处。因此"平面 → 放大画布"的单应
        # 必须在 ss*Hm 之后再平移 (ss-1)/2 个放大像素；否则整幅画面相对真值会整体偏移
        # (ss-1)/(2*ss) 个输出像素（ss=2 时是 0.25px），凭空放大所有测量误差。
        off = (ss - 1.0) / 2.0
        Hm_ss = np.array([[ss, 0.0, off], [0.0, ss, off], [0.0, 0.0, 1.0]]) @ Hm
        big = cv2.warpPerspective(
            plane,
            Hm_ss,
            (W * ss, H * ss),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(235, 235, 235),
        )
        img = cv2.resize(big, (W, H), interpolation=cv2.INTER_AREA)
    else:
        img = cv2.warpPerspective(
            plane, Hm, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(235, 235, 235)
        )

    if not args.ideal:
        if args.blur > 0:
            img = cv2.GaussianBlur(img, (0, 0), args.blur)
        if args.noise > 0:
            noise = rng.normal(0, args.noise, img.shape)
            img = np.clip(img.astype(np.float64) + noise, 0, 255).astype(np.uint8)
        if args.gradient > 0:
            ramp = np.linspace(-1.0, 1.0, W)[None, :, None]
            img = np.clip(img.astype(np.float64) * (1.0 + args.gradient * ramp), 0, 255).astype(np.uint8)
    return img, Hm


def apply_distortion(img, K, dist):
    """按 (K, dist) 生成带径向畸变的图像（用牛顿迭代求反向映射）。"""
    H, W = img.shape[:2]
    yy, xx = np.meshgrid(np.arange(H, dtype=np.float64), np.arange(W, dtype=np.float64), indexing="ij")
    pts = np.stack([xx.ravel(), yy.ravel(), np.ones(xx.size)], axis=0)
    norm = np.linalg.inv(K) @ pts
    x_d, y_d = norm[0], norm[1]
    x, y = x_d.copy(), y_d.copy()
    k1, k2 = float(dist[0]), float(dist[1])
    for _ in range(8):
        r2 = x * x + y * y
        f = 1.0 + k1 * r2 + k2 * r2 * r2
        x, y = x_d / f, y_d / f
    proj = K @ np.stack([x, y, np.ones_like(x)], axis=0)
    mapx = proj[0].reshape(H, W).astype(np.float32)
    mapy = proj[1].reshape(H, W).astype(np.float32)
    return cv2.remap(img, mapx, mapy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(235, 235, 235))


# --------------------------------------------------------------------------- #
# 3D 合成场景（各标记可以不在同一高度）—— 验证高度视差修正
#
# 上面那套是"俯视平面图 + 单个透视变换"，所有标记必然共面，永远测不出视差。
# 这里改用标准针孔投影：X_cam = R·X_world + t，每个标记按自己的高度成像。
#
# 世界坐标：z 轴向上，场地为 [0, S]²（mm）。注意这是**数学约定**（y 向上），
# 与上面平面图的"y 向下"约定无关，两套各自自洽。
# --------------------------------------------------------------------------- #
# 场地四角（数学约定）：A=(0,0) B=(S,0) C=(S,S) D=(0,S)
CORNER_UV = {"A": (0.0, 0.0), "B": (1.0, 0.0), "C": (1.0, 1.0), "D": (0.0, 1.0)}
# 在 MARKER_LOCAL（y 向上，顺序 TL,TR,BR,BL）里，各角"朝场地中心"的顶点序号
INNER_LOCAL = {"A": 1, "B": 0, "C": 3, "D": 2}
# 正方形环上每个角的两个邻居
CYCLE = {"A": ("B", "D"), "B": ("A", "C"), "C": ("B", "D"), "D": ("A", "C")}


def _project(K, R, t, pts_mm):
    """针孔投影。返回 (像素坐标 (N,2), 相机系深度 z (N,))。"""
    P = np.asarray(pts_mm, dtype=np.float64).reshape(-1, 3)
    Xc = P @ np.asarray(R, dtype=np.float64).T + np.asarray(t, dtype=np.float64).reshape(1, 3)
    z = Xc[:, 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        p = (np.asarray(K)[:2, :2] @ (Xc[:, :2] / z[:, None]).T).T + np.asarray(K)[:2, 2]
    return p, z


def build_camera_3d(rng, args):
    """构造一个俯拍相机；焦距自动选到把整场收进画面，且场地中心落在画面中心。

    返回 (K, R, t, C, K_super)，K_super 是超采样画布上的内参。
    """
    S = float(args.field_mm)
    Hc = float(args.cam_height_mm)
    W, H = args.canvas
    ox, oy = args.nadir_offset
    # 光心：场地中心 + 偏移；它的水平投影就是天底点
    C = np.array([S * (0.5 + ox), S * (0.5 + oy), Hc], dtype=np.float64)
    center_xy = np.array([S * 0.5, S * 0.5])

    d = center_xy - C[:2]
    psi = math.atan2(float(d[1]), float(d[0])) + rng.uniform(-0.22, 0.22)
    tau = math.atan2(float(np.linalg.norm(d)), Hc) * rng.uniform(0.78, 1.22)
    tau = max(tau, math.radians(12.0))
    roll = math.radians(rng.uniform(-args.cam_roll, args.cam_roll))

    z_cam = np.array([math.sin(tau) * math.cos(psi), math.sin(tau) * math.sin(psi), -math.cos(tau)])
    x_cam = np.cross(np.array([0.0, 0.0, 1.0]), z_cam)
    nx = float(np.linalg.norm(x_cam))
    if nx < 1e-9:
        return None
    x_cam /= nx
    y_cam = np.cross(z_cam, x_cam)
    x2 = math.cos(roll) * x_cam + math.sin(roll) * y_cam
    y2 = -math.sin(roll) * x_cam + math.cos(roll) * y_cam
    R = np.vstack([x2, y2, z_cam])
    t = -R @ C

    # 用 f=1 试投一次，估出需要的焦距（改变 f 只是把像绕主点缩放）
    K1 = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    ext = np.array([[*np.array(CORNER_UV[c], float) * S, args.corner_height_mm] for c in CORNERS])
    ctr = ext[:, :2].mean(axis=0)
    for i in range(4):  # 角标记会伸出场地外，留出余量
        v = ext[i, :2] - ctr
        ext[i, :2] = ctr + v * 1.4
    p, z = _project(K1, R, t, ext)
    if np.any(z <= 0.0):
        return None
    m = 20.0
    f = min(
        (W - 2 * m) / max(p[:, 0].max() - p[:, 0].min(), 1e-6),
        (H - 2 * m) / max(p[:, 1].max() - p[:, 1].min(), 1e-6),
    )
    if not np.isfinite(f) or f <= 1.0:
        return None
    K = np.array([[f, 0.0, W / 2.0], [0.0, f, H / 2.0], [0.0, 0.0, 1.0]])
    # 把场地中心平移到画面正中（主点随之偏移，后续自动求天底点用的是同一个 K）
    pc, zc = _project(K, R, t, np.array([[center_xy[0], center_xy[1], args.corner_height_mm]]))
    if zc[0] <= 0.0:
        return None
    K[0, 2] += W / 2.0 - pc[0, 0]
    K[1, 2] += H / 2.0 - pc[0, 1]
    ss = int(max(1, getattr(args, "cam_supersample", 2)))
    Kss = K.copy()
    Kss[0, :] *= ss
    Kss[1, :] *= ss
    return K, R, t, C, Kss


def _paste_marker_3d(canvas, Kss, R, t, dictionary, mid, loc_uv, S, height_mm):
    """把标记位图按 3D 投影贴到超采样画布上。loc_uv 为标记四角（场地归一化）。"""
    world = np.column_stack([np.asarray(loc_uv, dtype=np.float64) * S, np.full(4, float(height_mm))])
    p, z = _project(Kss, R, t, world)
    if np.any(z <= 0.0):
        return False
    pad = 6.0
    Hc_px, Wc_px = canvas.shape[:2]
    if (
        p[:, 0].min() < pad or p[:, 0].max() > Wc_px - pad
        or p[:, 1].min() < pad or p[:, 1].max() > Hc_px - pad
    ):
        return False  # 标记被裁 / 出画：整场重来，别拿残缺标记测
    dst = p.astype(np.float32)
    side = int(max(16, round(float(np.mean([np.linalg.norm(dst[(i + 1) % 4] - dst[i]) for i in range(4)])))))
    bmp = cv2.aruco.generateImageMarker(dictionary, int(mid), side)
    src = np.array([[0, 0], [side, 0], [side, side], [0, side]], dtype=np.float32)
    x0 = max(0, int(math.floor(dst[:, 0].min())) - 4)
    y0 = max(0, int(math.floor(dst[:, 1].min())) - 4)
    x1 = min(Wc_px, int(math.ceil(dst[:, 0].max())) + 4)
    y1 = min(Hc_px, int(math.ceil(dst[:, 1].max())) + 4)
    if x1 <= x0 or y1 <= y0:
        return False
    M = cv2.getPerspectiveTransform(src, dst - np.array([x0, y0], dtype=np.float32))
    roi = cv2.warpPerspective(
        bmp, M, (x1 - x0, y1 - y0), flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT, borderValue=255,
    )
    if canvas.ndim == 3:
        roi = cv2.cvtColor(roi, cv2.COLOR_GRAY2BGR)
    canvas[y0:y1, x0:x1] = np.minimum(canvas[y0:y1, x0:x1], roi)
    return True


def _degrade(img, rng, args):
    if args.ideal:
        return img
    if args.blur > 0:
        img = cv2.GaussianBlur(img, (0, 0), args.blur)
    if args.noise > 0:
        noise = rng.normal(0, args.noise, img.shape)
        img = np.clip(img.astype(np.float64) + noise, 0, 255).astype(np.uint8)
    if args.gradient > 0:
        ramp = np.linspace(-1.0, 1.0, img.shape[1])[None, :, None]
        img = np.clip(img.astype(np.float64) * (1.0 + args.gradient * ramp), 0, 255).astype(np.uint8)
    return img


def render_scene_3d(rng, args, dictionary):
    """3D 场景：角标记在 corner_height_mm、0 号标记在 target_height_mm。"""
    built = build_camera_3d(rng, args)
    if built is None:
        return None
    K, R, t, C, Kss = built
    S = float(args.field_mm)
    W, H = args.canvas
    ss = int(max(1, getattr(args, "cam_supersample", 2)))
    canvas = np.full((H * ss, W * ss, 3), 255, np.uint8)

    # 场地底板（灰），只是给标记一个背景
    board = np.array([[*np.array(CORNER_UV[c], float) * S, 0.0] for c in CORNERS])
    pb, zb = _project(Kss, R, t, board)
    if np.all(zb > 0.0):
        cv2.fillConvexPoly(canvas, np.round(pb).astype(np.int32), (150, 150, 150))

    size_uv = MARKER_MM / S
    ids_layout = list(rng.permutation([1, 2, 3, 4]))
    corner_info = {}
    for corner, mid in zip(CORNERS, ids_layout):
        phi = math.radians(rng.uniform(-args.rot_jitter, args.rot_jitter))
        inner_uv = np.array(CORNER_UV[corner], dtype=np.float64)
        cuv = inner_uv - rot(phi) @ (MARKER_LOCAL[INNER_LOCAL[corner]] * size_uv)
        loc = (rot(phi) @ (MARKER_LOCAL * size_uv).T).T + cuv
        if not _paste_marker_3d(canvas, Kss, R, t, dictionary, int(mid), loc, S, args.corner_height_mm):
            return None
        corner_info[corner] = {"id": int(mid), "phi": phi, "uv": cuv}

    tu = float(rng.uniform(*args.target_range))
    tv = float(rng.uniform(*args.target_range))
    theta = math.radians(rng.uniform(0.0, 360.0))
    tuv = np.array([tu, tv])
    tloc = (rot(theta) @ (MARKER_LOCAL * size_uv).T).T + tuv
    if not _paste_marker_3d(canvas, Kss, R, t, dictionary, 0, tloc, S, args.target_height_mm):
        return None

    img = cv2.resize(canvas, (W, H), interpolation=cv2.INTER_AREA)
    img = _degrade(img, rng, args)
    info = {
        "ids": {c: corner_info[c]["id"] for c in CORNERS},
        "phi": {c: corner_info[c]["phi"] for c in CORNERS},
        "target_uv": tuv,
        "target_theta": theta,
        "cam_xy": (float(C[0]), float(C[1])),
        "cam_height_mm": float(args.cam_height_mm),
        "K": K,
        "R": R,
        "t": t,
    }
    return img, info


def ground_truth_3d(K, R, t, args, info, canvas_shape):
    """3D 场景真值（独立实现）：原点取"离画面左下角最近的场地角点"，
    +x 取两条边里在画面中更偏右的一条 —— 与管线约定完全一致。"""
    S = float(args.field_mm)
    H, W = canvas_shape[:2]
    names = list(CORNERS)
    xy = {c: np.array(CORNER_UV[c], dtype=np.float64) * S for c in names}
    pts = np.array([[*xy[c], float(args.corner_height_mm)] for c in names])
    p, z = _project(K, R, t, pts)
    if np.any(z <= 0.0):
        return None

    d_bl = np.linalg.norm(p - np.array([0.0, H - 1.0]), axis=1)
    o = int(np.argmin(d_bl))
    if float(np.sort(d_bl)[1] - d_bl[o]) < 1e-6:
        return None
    origin = names[o]
    nb = CYCLE[origin]
    dxs = [float(p[names.index(k)][0] - p[o][0]) for k in nb]
    xi = 0 if dxs[0] >= dxs[1] else 1
    x_name, y_name = nb[xi], nb[1 - xi]

    org = xy[origin]
    ex = _unit_axis(xy[x_name] - org)
    ey = _unit_axis(xy[y_name] - org)
    if abs(float(ex @ ey)) > 1e-9:
        return None

    def to_field(xy_mm):
        v = (np.asarray(xy_mm, dtype=np.float64) - org) / S
        return np.array([float(v @ ex), float(v @ ey)])

    v_marker = np.array([math.cos(info["target_theta"]), math.sin(info["target_theta"])])
    theta = math.atan2(float(v_marker @ ey), float(v_marker @ ex))
    return {
        "origin_id": info["ids"][origin],
        "origin_corner": origin,
        "xy": to_field(info["target_uv"] * S),
        "theta_deg": math.degrees(theta),
        "nadir_xy": to_field(np.array(info["cam_xy"], dtype=np.float64)),
    }


# --------------------------------------------------------------------------- #
# 真值（独立实现，不使用本包的几何代码）
# --------------------------------------------------------------------------- #
def ground_truth(H_mm2canvas: np.ndarray, canvas_shape, info: dict):
    """由已知的相机单应独立算出真值。

    场地坐标系约定：原点 = 离画面左下角最近的场地角点，+x 取两条边里更偏右的一条。
    本函数直接判断"哪个角在左下、哪条边更偏右"，并把场地系写成平面图 mm 坐标系下的
    正交基，从而解析地给出真值。
    """
    H, W = canvas_shape[:2]
    pts_canvas = cv2.perspectiveTransform(
        CORNER_MM_ARR.reshape(-1, 1, 2), H_mm2canvas
    ).reshape(-1, 2)

    d_bl = np.linalg.norm(pts_canvas - np.array([0.0, H - 1.0]), axis=1)
    o = int(np.argmin(d_bl))
    order = np.argsort(d_bl)
    if d_bl[order[1]] - d_bl[order[0]] < 1e-6:
        return None

    others = [k for k in range(4) if k != o]
    # 与原点相邻的两个角：到原点距离最近的
    d_o = np.linalg.norm(pts_canvas[others] - pts_canvas[o], axis=1)
    nb = [others[int(k)] for k in np.argsort(d_o)[:2]]
    dirs = [pts_canvas[k] - pts_canvas[o] for k in nb]
    xi = 0 if dirs[0][0] >= dirs[1][0] else 1
    x_corner, y_corner = nb[xi], nb[1 - xi]

    origin_name = CORNERS[o]
    origin_mm = CORNER_MM_ARR[o]
    ex_mm = _unit_axis(CORNER_MM_ARR[x_corner] - CORNER_MM_ARR[o])
    ey_mm = _unit_axis(CORNER_MM_ARR[y_corner] - CORNER_MM_ARR[o])

    if ex_mm[1] + ey_mm[1] >= 0:  # 两条边都应从原点朝画面上方延伸
        return None
    if abs(ex_mm @ ey_mm) > 1e-9:
        return None

    def to_field(p_mm):
        v = (np.asarray(p_mm) - origin_mm) / FIELD_MM
        return np.array([v @ ex_mm, v @ ey_mm])

    v_marker = np.array([math.cos(info["target_theta"]), math.sin(info["target_theta"])])
    theta = math.atan2(v_marker @ ey_mm, v_marker @ ex_mm)
    return {
        "origin_id": info["ids"][origin_name],
        "origin_corner": origin_name,
        "xy": to_field(info["target_center_mm"]),
        "theta_deg": math.degrees(theta),
    }


def _unit_axis(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float64)
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def mm_to_px_homography(info: dict) -> np.ndarray:
    """平面图 mm → 像素 的仿射矩阵（与 render_plane 里的换算保持一致）。"""
    s, m = info["mm_to_px"]
    return np.array([[s, 0.0, m], [0.0, s, m], [0.0, 0.0, 1.0]], dtype=np.float64)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="合成场地自检")
    ap.add_argument("--cases", "-n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20260922)
    ap.add_argument("--config", "-c", default=None)
    ap.add_argument("--vertex-mode", default=None,
                    help="覆盖 geometry.vertex_mode：xdiag_plane | xdiag_unit | xdiag | "
                         "nearest | auto | search | full")
    ap.add_argument("--out", default=None, help="保存调试图的目录")
    ap.add_argument("--save-cases", type=int, default=3, help="保存多少个场景的调试图")
    ap.add_argument("--canvas", type=int, nargs=2, default=(1280, 960))
    ap.add_argument("--fill", type=float, default=0.70, help="场地在画面里占短边的比例")
    ap.add_argument("--tilt", type=float, default=0.16, help="透视倾斜强度")
    ap.add_argument("--cam-roll", type=float, default=35.0, help="相机绕光轴旋转范围（度）")
    ap.add_argument("--rot-jitter", type=float, default=40.0, help="角标记朝向随机范围（度）")
    ap.add_argument("--target-range", type=float, nargs=2, default=(0.18, 0.82), help="0 号标记活动范围")
    ap.add_argument("--mm-per-px", type=float, default=1.0)
    ap.add_argument("--supersample", type=int, default=2)
    ap.add_argument("--cam-supersample", type=int, default=2, help="相机透视变换的超采样倍数（抗混叠）")
    ap.add_argument("--blur", type=float, default=0.8)
    ap.add_argument("--noise", type=float, default=4.0)
    ap.add_argument("--gradient", type=float, default=0.06)
    ap.add_argument("--ideal", action="store_true", help="理想成像（无模糊/噪声/光照不均）")
    ap.add_argument("--verbose", "-v", action="store_true", help="打印每个配置最差的 5 个场景")
    ap.add_argument("--compare-edge", action="store_true", help="额外对比 cv2 亚像素（关闭边缘拟合）")
    ap.add_argument("--distort", action="store_true", help="叠加镜头畸变，并验证畸变校正路线")
    # ---- 3D 场景：验证高度视差修正（各标记不在同一高度）----
    ap.add_argument("--heights", action="store_true", help="改用 3D 场景，标记可以不同高度")
    ap.add_argument("--field-mm", type=float, default=1000.0, help="3D 场景场地边长（mm）")
    ap.add_argument("--cam-height-mm", type=float, default=2000.0, help="相机光心离地高度（mm）")
    ap.add_argument("--corner-height-mm", type=float, default=50.0, help="角标记（参考平面）离地高度")
    ap.add_argument("--target-height-mm", type=float, default=23.0, help="0 号标记离地高度")
    ap.add_argument("--nadir-offset", type=float, nargs=2, default=(-0.12, -0.06),
                    help="天底点相对场地中心的偏移（占场地边长的比例）")
    args = ap.parse_args(argv)
    if not hasattr(args, "cam_supersample"):
        args.cam_supersample = 2

    base_cfg = load_config(args.config)
    if args.vertex_mode:
        base_cfg.geometry.vertex_mode = args.vertex_mode
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, base_cfg.detection.dict_name))
    rng = np.random.default_rng(args.seed)

    K = np.array([[900.0, 0.0, args.canvas[0] / 2], [0.0, 900.0, args.canvas[1] / 2], [0.0, 0.0, 1.0]])
    dist = np.array([-0.18, 0.05, 0.0, 0.0, 0.0])
    mm_scale = float(args.field_mm) if args.heights else FIELD_MM

    if args.heights:
        # 3D 场景：不修正 / 用真值天底点修正（隔离修正本身）/ 由内参自动求天底点
        # 高度必须取命令行给的值：合成场景就是按它们渲染的，配置里的数字只对实拍有意义
        _h = dict(corner_height_mm=args.corner_height_mm, target_height_mm=args.target_height_mm)
        variants = [
            ("3D 不修正", _variant(base_cfg, refine=True, parallax=False, **_h), None, None),
            (
                "3D 修正(真天底)",
                _variant(base_cfg, refine=True, parallax=True, cam_height_mm=args.cam_height_mm,
                         from_intrinsics=False, **_h),
                None,
                _setup_nadir("truth"),
            ),
            (
                "3D 修正(内参)",
                _variant(base_cfg, refine=True, parallax=True, cam_height_mm=args.cam_height_mm,
                         nadir_xy=None, from_intrinsics=True, **_h),
                "percase",
                None,
            ),
        ]
    else:
        # 三套配置：精化开 / 精化关 /（可选）畸变 + 校正
        variants = [
            ("refine=on", _variant(base_cfg, refine=True), None, None),
            ("refine=off", _variant(base_cfg, refine=False), None, None),
        ]
        if args.compare_edge:
            variants.append(("cv2subpix", _variant(base_cfg, refine=True, edge=False), None, None))
        if args.distort:
            variants.append(
                ("distort+undistort", _variant(base_cfg, refine=True, undistort=True), (K, dist), None)
            )

    locators, stats = {}, {}
    for name, cfg, kd, _setup in variants:
        lo = ArucoLocator(cfg)
        if isinstance(kd, tuple):  # 直接把已知内参注进去（等价于读标定文件）
            lo._K, lo._dist = kd
        locators[name] = lo
        stats[name] = {
            "err": [], "dtheta": [], "invalid": 0, "flip": 0, "skip": 0, "rms": [],
            "reasons": {}, "records": [], "axis_swap": 0, "ambiguous": 0,
            "shift": [], "nadir_err": [],
        }

    saved = 0
    if args.out:
        os.makedirs(args.out, exist_ok=True)

    for case in range(args.cases):
        if args.heights:
            built = render_scene_3d(rng, args, dictionary)
            if built is None:
                for s in stats.values():
                    s["skip"] += 1
                continue
            img, info = built
            gt = ground_truth_3d(info["K"], info["R"], info["t"], args, info, img.shape)
        else:
            plane, field_quad_px, info = render_plane(rng, args, dictionary)
            img, Hm = camera_view(plane, field_quad_px, rng, args)
            if args.distort:
                img = apply_distortion(img, K, dist)
            # 平面图 mm → 画布 的完整单应（用于独立算真值）
            gt = ground_truth(Hm @ mm_to_px_homography(info), img.shape, info)
        if gt is None:
            for s in stats.values():
                s["skip"] += 1
            continue

        last: dict[str, object] = {}
        for name, _, kd, setup in variants:
            lo = locators[name]
            if kd == "percase" and args.heights:
                lo._K = np.asarray(info["K"], dtype=np.float64)
                lo._dist = np.zeros((1, 5))
            if setup is not None:
                setup(lo, gt)
            loc = lo.locate(img)
            last[name] = loc
            s = stats[name]
            if not loc.valid:
                s["invalid"] += 1
                s["reasons"][loc.reason] = s["reasons"].get(loc.reason, 0) + 1
                continue
            if loc.origin_id != gt["origin_id"]:
                s["flip"] += 1
                continue
            err = math.hypot(loc.x - gt["xy"][0], loc.y - gt["xy"][1])
            dth = abs((loc.theta_deg - gt["theta_deg"] + 180.0) % 360.0 - 180.0)
            # "底边/左边"无法区分时（相机绕光轴 ≈ ±45°）x/y 会整体互换，
            # 这是画面本身不含信息的固有歧义，单列一栏统计，不混进位置误差。
            if "ambiguous_axis" in loc.flags:
                s["ambiguous"] += 1
            err_yx = math.hypot(loc.x - gt["xy"][1], loc.y - gt["xy"][0])
            if err * mm_scale > 5.0 and err_yx * mm_scale < 5.0:
                s["axis_swap"] += 1
                continue
            s["err"].append(err)
            s["dtheta"].append(dth)
            par = (loc.diagnostics or {}).get("parallax") or {}
            if par.get("shift_field") is not None:
                s["shift"].append(float(par["shift_field"]))
                s["nadir_err"].append(
                    math.hypot(par["nadir_x"] - gt["nadir_xy"][0], par["nadir_y"] - gt["nadir_xy"][1])
                )
            s["records"].append(
                {
                    "case": case,
                    "err_mm": err * mm_scale,
                    "dtheta": dth,
                    "x": loc.x,
                    "y": loc.y,
                    "theta": loc.theta_deg,
                    "gt_x": float(gt["xy"][0]),
                    "gt_y": float(gt["xy"][1]),
                    "gt_theta": gt["theta_deg"],
                    "rms": loc.rms_field,
                    "tgt_rms": loc.target_rms,
                }
            )
            if loc.rms_field is not None:
                s["rms"].append(loc.rms_field)

        if args.out and saved < args.save_cases:
            key = "3D 不修正" if args.heights else "refine=on"
            loc0 = last.get(key)
            if loc0 is not None:
                vis = locators[key].annotate(img, loc0)
                vis = cv2.resize(vis, None, fx=0.75, fy=0.75, interpolation=cv2.INTER_AREA)
                imwrite_unicode(os.path.join(args.out, f"case{case:03d}.png"), vis)
                saved += 1

    _report(stats, args, variants, verbose=args.verbose, mm=mm_scale)
    return 0


def _setup_nadir(mode: str | None):
    """返回"每个场景注入真值天底点"的回调（隔离修正本身与天底点估计精度）。"""

    def fn(locator, gt):
        if mode == "truth" and gt is not None:
            locator.cfg.camera.parallax.nadir_xy = [float(gt["nadir_xy"][0]), float(gt["nadir_xy"][1])]

    return fn


def _variant(
    cfg: LocatorConfig,
    refine: bool,
    undistort: bool = False,
    edge: bool = True,
    parallax: bool | None = None,
    cam_height_mm: float | None = None,
    nadir_xy="keep",
    from_intrinsics: bool | None = None,
    corner_height_mm: float | None = None,
    target_height_mm: float | None = None,
) -> LocatorConfig:
    import copy

    c = copy.deepcopy(cfg)
    c.refine.enable = refine
    c.camera.undistort = undistort
    c.detection.edge_refine = edge
    if parallax is not None:
        c.camera.parallax.enable = bool(parallax)
    if cam_height_mm is not None:
        c.camera.parallax.camera_height_mm = float(cam_height_mm)
    if nadir_xy != "keep":
        c.camera.parallax.nadir_xy = None if nadir_xy is None else [float(v) for v in nadir_xy]
    if from_intrinsics is not None:
        c.camera.parallax.from_intrinsics = bool(from_intrinsics)
    if corner_height_mm is not None:
        c.camera.parallax.corner_height_mm = float(corner_height_mm)
    if target_height_mm is not None:
        c.camera.parallax.target_height_mm = float(target_height_mm)
    return c


def _report(stats, args, variants, verbose: bool = False, mm: float = FIELD_MM):
    print(f"\n合成场地自检：{args.cases} 个随机场景  seed={args.seed}"
          + ("  [3D 场景，各标记不同高度]" if args.heights else ""))
    print(f"{'配置':<20}{'有效':>6}{'无效':>6}{'换角':>6}{'轴歧义':>8}{'跳过':>6}"
          f"{'位置均值':>12}{'位置最大':>12}{'角度均值':>10}{'角度最大':>10}{'rms均值':>10}")
    for name, _, _, _ in variants:
        s = stats[name]
        n = len(s["err"])
        if n == 0:
            print(f"{name:<20}{0:>6}{s['invalid']:>6}{s['flip']:>6}{s['axis_swap']:>8}{s['skip']:>6}"
                  f"   (无可比数据)")
            continue
        e = np.asarray(s["err"]) * mm
        d = np.asarray(s["dtheta"])
        r = np.asarray(s["rms"]) if s["rms"] else np.array([float("nan")])
        print(
            f"{name:<20}{n:>6}{s['invalid']:>6}{s['flip']:>6}{s['axis_swap']:>8}{s['skip']:>6}"
            f"{e.mean():>10.3f}mm{e.max():>10.3f}mm{d.mean():>8.3f}°{d.max():>8.3f}°{r.mean():>10.5f}"
        )
        if s["reasons"]:
            print("      无效原因：" + "，".join(f"{k}×{v}" for k, v in s["reasons"].items()))
        if s["ambiguous"]:
            print(f"      其中 {s['ambiguous']} 帧带 ambiguous_axis 标志"
                  f"（相机绕光轴接近 ±45°，底边/左边本身不可区分）")
        if s.get("shift"):
            sh = np.asarray(s["shift"]) * mm
            extra = ""
            if s.get("nadir_err"):
                ne = np.asarray(s["nadir_err"])
                extra = f"；天底点误差均值 {ne.mean() * mm:.2f}mm 最大 {ne.max() * mm:.2f}mm"
            print(f"      高度修正量：均值 {sh.mean():.3f}mm 最大 {sh.max():.3f}mm{extra}")
    print(f"\n位置误差为场地毫米（本次场地边长 {mm:.0f}mm）；rms 为四边形映射残差（场地归一化单位）")
    if verbose:
        for name, _, _, _ in variants:
            recs = sorted(stats[name]["records"], key=lambda r: -r["err_mm"])[:5]
            if not recs:
                continue
            print(f"\n{name} 最差的 5 个场景：")
            for r in recs:
                print(
                    f"  case{r['case']:>3}  位置误差 {r['err_mm']:>7.3f}mm  角度 {r['dtheta']:>6.3f}°  "
                    f"解算({r['x']:.4f},{r['y']:.4f},{r['theta']:+.2f}°)  真值({r['gt_x']:.4f},{r['gt_y']:.4f},{r['gt_theta']:+.2f}°)  "
                    f"rms={r['rms']:.5f} tgt_rms={r['tgt_rms']:.5f}"
                )
    print("换角 = 检出结果的坐标系原点与真值不同（相机绕光轴旋转接近 ±45° 时的固有歧义）")
    print("轴歧义 = 结果里 x/y 整体互换的帧（同样源于 ±45°：此时\"底边\"与\"左边\"无法区分），"
          "已在 flags 里标 ambiguous_axis")


if __name__ == "__main__":
    raise SystemExit(main())
