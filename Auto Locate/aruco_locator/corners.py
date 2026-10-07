"""基于边缘直线拟合的亚像素角点精化。

为什么需要它
------------
``cv2.aruco`` 的 ``CORNER_REFINE_SUBPIX`` 只在角点附近 ±2px 的窗口里找一个
"角点响应极值"。标记在画面里只有十几像素时（本项目里 33mm 有效区 / 1m 场地，
在 1280×960 画面上大约 18~35px），这个方法的信息量太少，实测角点误差可达
1~2px，即标记边长的 5%~10%。而这个误差会直接传递到单应矩阵，进而毁掉位置精度。

本模块改用**边缘直线拟合**：

1. 沿标记四条外边各采样一条灰度剖面（沿外法线方向，双线性/双三次插值到亚像素）；
2. 用一阶导数极值定位每条边上的亚像素边缘点；利用"标记是黑的"这一先验，
   只取"由暗到亮"的梯度极值，自然排除内侧码区与黑框之间那条方向相反的边缘；
3. 每条边对几十个边缘点做带 sigma 截断的总体最小二乘直线拟合；
4. 相邻两条直线求交，得到角点。再迭代一轮，用更准的基准线重新采样。

每条边用上了几十个像素的信息（而不是局部 5×5 窗口），角点精度通常提升 3~10 倍。
"""

from __future__ import annotations

import cv2
import numpy as np

__all__ = ["refine_quad_by_edges", "EdgeRefineResult"]


class EdgeRefineResult:
    """单次精化的结果，附带质量指标供上层判断是否采纳。"""

    __slots__ = ("quad", "ok", "line_scatter", "n_points", "shift")

    def __init__(self, quad, ok, line_scatter, n_points, shift):
        self.quad = quad
        self.ok = bool(ok)
        self.line_scatter = line_scatter  # 每条边到拟合直线的 rms（像素）
        self.n_points = n_points  # 每条边实际用到的边缘点数
        self.shift = shift  # 相对输入四边形的平均位移（像素）


# --------------------------------------------------------------------------- #
# 采样与拟合工具
# --------------------------------------------------------------------------- #
def _crop_sampler(gray: np.ndarray, quad: np.ndarray, pad: int):
    """围绕四边形裁一块 ROI，返回一个把"整图坐标"映射成灰度的采样函数。

    ROI + INTER_CUBIC 比逐点手工双线性更快，且剖面更光滑（亚像素定位更稳）。
    """
    h, w = gray.shape[:2]
    x0 = max(0, int(np.floor(quad[:, 0].min())) - pad)
    y0 = max(0, int(np.floor(quad[:, 1].min())) - pad)
    x1 = min(w, int(np.ceil(quad[:, 0].max())) + pad + 1)
    y1 = min(h, int(np.ceil(quad[:, 1].max())) + pad + 1)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None, None
    roi = np.ascontiguousarray(gray[y0:y1, x0:x1], dtype=np.float32)
    ox, oy = float(x0), float(y0)

    def sample(pts: np.ndarray) -> np.ndarray:
        # pts: (..., 2) 整图坐标 → (...,) 灰度
        mx = (pts[..., 0] - ox).astype(np.float32)
        my = (pts[..., 1] - oy).astype(np.float32)
        return cv2.remap(roi, mx, my, cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)

    return sample, (x0, y0, x1, y1)


def _fit_line(pts: np.ndarray, clip_sigma: float, iters: int = 3):
    """总体最小二乘直线拟合（带 sigma 截断）。返回 (点, 单位方向, rms)。"""
    p = np.asarray(pts, dtype=np.float64)
    if len(p) < 2:
        return None
    c = p.mean(axis=0)
    u = np.array([1.0, 0.0])
    rms = float("inf")
    for _ in range(iters):
        q = p - c
        if len(q) < 2:
            break
        try:
            _, _, vt = np.linalg.svd(q, full_matrices=False)
        except np.linalg.LinAlgError:
            return None
        u = vt[0]
        nvec = np.array([-u[1], u[0]])
        r = q @ nvec
        rms = float(np.sqrt(np.mean(r**2)))
        s = float(np.std(r))
        if s < 1e-9:
            break
        keep = np.abs(r) <= clip_sigma * s
        if keep.all() or keep.sum() < max(2, len(p) // 2):
            break
        p, c = p[keep], p[keep].mean(axis=0)
    return c, u, rms


def _intersect(l1, l2):
    """两条直线 (点, 方向) 求交。"""
    a1, u1 = l1[0], l1[1]
    a2, u2 = l2[0], l2[1]
    den = u1[0] * u2[1] - u1[1] * u2[0]
    if abs(den) < 1e-12:
        return None
    t = ((a2[0] - a1[0]) * u2[1] - (a2[1] - a1[1]) * u2[0]) / den
    return a1 + t * u1


# --------------------------------------------------------------------------- #
# 单条边：提取亚像素边缘点
# --------------------------------------------------------------------------- #
def _edge_points(
    sample,
    p0: np.ndarray,
    p1: np.ndarray,
    outward: np.ndarray,
    search: float,
    step: float,
    n_samples: int,
    trim: float,
    bright_outside: bool,
    min_rel_strength: float,
):
    """沿一边采样剖面并定位亚像素边缘点。

    ``outward`` 是该边指向标记外侧的单位法线。

    返回 (edge_pts (N,2), strengths (N,))；样本不足时返回 (None, None)。
    """
    d = p1 - p0
    length = float(np.linalg.norm(d))
    if length < 4.0:
        return None, None
    u = d / length
    n = np.array([-u[1], u[0]], dtype=np.float64)
    if float(np.dot(n, outward)) < 0.0:
        n = -n  # n 指向标记外侧

    ts = np.linspace(trim, 1.0 - trim, max(3, int(n_samples)))
    base = p0 + ts[:, None] * d
    ss = np.arange(-search, search + 1e-9, step)
    grid = base[:, None, :] + ss[None, :, None] * n[None, None, :]
    prof = sample(grid)  # (N, M)

    der = np.gradient(prof, step, axis=1)  # dI/ds，沿外法线方向
    if not bright_outside:
        der = -der  # 标记比背景亮时，边缘方向反过来

    m = len(ss)
    lo = max(1, min(2, m // 4))
    hi = m - lo
    core = der[:, lo:hi]
    if core.size == 0:
        return None, None

    idx = np.argmax(core, axis=1)  # 最"由暗到亮"的位置
    peak = core[np.arange(len(idx)), idx]
    strong = peak > 0
    if not np.any(strong):
        return None, None

    ref = float(np.median(peak[strong]))
    if ref <= 1e-9:
        return None, None
    strong &= peak >= min_rel_strength * ref
    if strong.sum() < 3:
        return None, None

    # 亚像素：在峰值 ±2 个采样点内，对正向导数做加权质心
    rows = np.nonzero(strong)[0]
    loc = np.empty(len(rows))
    gidx = idx[rows] + lo
    for k, (ri, gi) in enumerate(zip(rows, gidx)):
        a = max(0, gi - 2)
        b = min(m, gi + 3)
        w = np.clip(der[ri, a:b], 0.0, None)
        tot = w.sum()
        if tot <= 1e-12:
            loc[k] = ss[gi]
        else:
            loc[k] = float((w * ss[a:b]).sum() / tot)

    pts = base[rows] + loc[:, None] * n[None, :]
    return pts, peak[rows]


# --------------------------------------------------------------------------- #
# 主函数
# --------------------------------------------------------------------------- #
def refine_quad_by_edges(
    gray: np.ndarray,
    quad: np.ndarray,
    search: float = 2.5,
    step: float = 0.25,
    samples: int | None = None,
    trim: float = 0.15,
    iterations: int = 2,
    min_points: int = 5,
    clip_sigma: float = 2.5,
    bright_outside: bool = True,
    min_rel_strength: float = 0.35,
    max_shift_ratio: float = 0.35,
) -> EdgeRefineResult:
    """把四边形（4×2，图像像素）精化到亚像素精度。

    ``search`` 是沿外法线的初始搜索半宽（像素）；``min_points`` 是每条边至少需要
    的边缘点数；任何一条边拟合失败都会导致整体 ``ok=False``（上层可回退原值）。
    """
    q = np.asarray(quad, dtype=np.float64).reshape(4, 2).copy()
    original = q.copy()
    if not np.all(np.isfinite(q)):
        return EdgeRefineResult(original, False, [], [], 0.0)

    # ⚠ 绝对不要按质心角排序去"整理"顶点顺序。
    # cv2 给出的角点顺序是"以标记自身印刷方向为基准"的规范顺序（corners[0]→corners[1]
    # 就是标记自身的 +x 轴）。正方形有 4 次旋转对称，一旦重排，后续的相似变换拟合
    # 依然能得到零残差（因为对正方形来说"转 90°"本身就是一个相似变换），
    # 但标记自身 +x 轴的方向会被整体偏移 90° 的整数倍，方向解算直接废掉。
    # 这里只根据遍历方向定出"外法线该往哪边"，顶点本身保持原序。
    signed = float(np.sum(q[:, 0] * np.roll(q[:, 1], -1) - np.roll(q[:, 0], -1) * q[:, 1]))
    orient = 1.0 if signed > 0 else -1.0  # 正 = 逆时针（数学意义）

    pad = int(np.ceil(search)) + 4
    line_scatter, n_points = [], []
    for it in range(max(1, int(iterations))):
        sample, box = _crop_sampler(gray, q, pad)
        if sample is None:
            return EdgeRefineResult(original, False, [], [], 0.0)
        lines = []
        for i in range(4):
            p0, p1 = q[i], q[(i + 1) % 4]
            d = p1 - p0
            ln = float(np.linalg.norm(d))
            if ln < 1e-9:
                lines.append(None)
                continue
            outward = orient * np.array([d[1], -d[0]], dtype=np.float64) / ln
            ns = samples if samples else int(np.clip(ln / 2.0, 6, 160))
            # 迭代到后面基准线已经很准，收紧搜索范围
            cur_search = search if it == 0 else max(0.75, search * 0.5)
            pts, _ = _edge_points(
                sample, p0, p1, outward, cur_search, step, ns, trim,
                bright_outside, min_rel_strength,
            )
            if pts is None or len(pts) < max(2, int(min_points)):
                lines.append(None)
                continue
            fit = _fit_line(pts, clip_sigma)
            if fit is None:
                lines.append(None)
                continue
            lines.append(fit)
            if it == max(1, int(iterations)) - 1:
                line_scatter.append(fit[2])
                n_points.append(len(pts))

        if any(l is None for l in lines):
            return EdgeRefineResult(original, False, [], [], 0.0)

        new = q.copy()
        for i in range(4):
            x = _intersect(lines[(i - 1) % 4], lines[i])
            if x is None or not np.all(np.isfinite(x)):
                return EdgeRefineResult(original, False, [], [], 0.0)
            new[i] = x
        # 形状合理性：角点位移不能超过边长的 max_shift_ratio
        span = float(np.mean([np.linalg.norm(q[(k + 1) % 4] - q[k]) for k in range(4)]))
        if float(np.linalg.norm(new - q, axis=1).max()) > max_shift_ratio * span:
            return EdgeRefineResult(original, False, [], [], 0.0)
        q = new

    # 凸性检查（精化后必须仍是凸四边形，否则不可信）
    cross = []
    for i in range(4):
        a, b, cc = q[i], q[(i + 1) % 4], q[(i + 2) % 4]
        cross.append((b[0] - a[0]) * (cc[1] - b[1]) - (b[1] - a[1]) * (cc[0] - b[0]))
    cross = np.asarray(cross)
    if not (np.all(cross > 0) or np.all(cross < 0)):
        return EdgeRefineResult(original, False, [], [], 0.0)

    shift = float(np.linalg.norm(q - original, axis=1).mean())
    return EdgeRefineResult(q, True, line_scatter, n_points, shift)
