"""几何解算核心。

思路（全部基于共面假设，不需要相机内参）：

1. 四个角标记各取一个"内顶点"（朝向场地中心的那个顶点）→ 四个场地角点；
2. 由四点求出 图像 → 场地单位正方形 的单应 H（getPerspectiveTransform，四点精确解）；
3. 用"另外 12 个角点在 H 下必须映射成正方形"这一约束来：
   - 校验/挑选内顶点（标记是正方形，这条约束与场地约定无关，非常硬）；
   - 可选地做一次联合最小二乘精化，把 16 个角点全部用上，压低单点噪声；
4. 对 0 号标记的四个角点做相似变换拟合（Umeyama），一次得到中心、方向、尺度与残差。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from .config import GeometryConfig, RefineConfig
from .detector import MarkerObservation

try:  # 精化功能可选，scipy 缺失时自动降级
    from scipy.optimize import least_squares

    _HAS_SCIPY = True
except Exception:  # pragma: no cover
    least_squares = None
    _HAS_SCIPY = False


# --------------------------------------------------------------------------- #
# 标记自身的局部坐标系
#
# 角点顺序沿用 OpenCV 约定：以印刷方向为准的 左上 / 右上 / 右下 / 左下，
# 因此 corners[0] → corners[1] 恒为"标记印刷时的 +x（向右）"。
#
# 关键：局部系取 **x 向右、y 向上**（而不是印刷图像里的 y 向下）。
# 因为场地坐标系是"原点在左下、+y 朝画面上方"，如果把局部系也定义成印刷那样
# 的 y 向下，两者之间就是"镜像 + 旋转"而不是纯旋转，后面所有位姿拟合都会崩。
# 取 y 向上后，标记局部系 → 场地系 是纯旋转，模型才自洽。
# --------------------------------------------------------------------------- #
MARKER_LOCAL = np.array(
    [[-0.5, 0.5], [0.5, 0.5], [0.5, -0.5], [-0.5, -0.5]], dtype=np.float64
)
# 同一套角的"顶点式"坐标（左上为参考点，y 向上），用于拼内顶点的相对位置
MARKER_LOCAL_EDGE = np.array([[0.0, 1.0], [1.0, 1.0], [1.0, 0.0], [0.0, 0.0]], dtype=np.float64)
# 场地四个角的场地坐标，顺序与 FieldFrame.ring_ids 一致
FIELD_SQUARE_CORNERS = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]], dtype=np.float64)


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def apply_homography(H: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """把 (N,2) 点集通过 3x3 单应映射过去。"""
    pts = np.asarray(pts, dtype=np.float64).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(pts, H).reshape(-1, 2)


def fit_similarity(src: np.ndarray, dst: np.ndarray, allow_reflection: bool = False):
    """Umeyama：求 dst ≈ s·R·src + t。

    返回 (scale, R, t, rms)。默认 R 为真旋转（禁止镜像）；
    allow_reflection=True 时允许镜像，用于"这个四边形是否正方形"这种与坐标系手性
    无关的判断（此时返回的 R 可能是镜像，仅 rms 有意义）。
    """
    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    n = len(src)
    ms, md = src.mean(axis=0), dst.mean(axis=0)
    X, Y = src - ms, dst - md
    cov = Y.T @ X / n
    U, S, Vt = np.linalg.svd(cov)
    var = float((X**2).sum() / n)
    det_uv = float(np.linalg.det(U @ Vt))

    signs = (1.0, -1.0) if allow_reflection else (1.0,)
    best = None
    for d in signs:
        R = U @ np.diag([1.0, d]) @ Vt
        scale = float((S * np.array([1.0, d])).sum() / var) if var > 0 else 0.0
        t = md - scale * (R @ ms)
        pred = (scale * (R @ X.T)).T + md
        rms = float(np.sqrt(np.mean(np.sum((pred - dst) ** 2, axis=1))))
        if best is None or rms < best[3]:
            best = (scale, R, t, rms)
    if best is None:  # pragma: no cover
        return 0.0, np.eye(2), md.copy(), float("inf")
    if not allow_reflection and det_uv <= 0:
        # 手性与期望相反，说明数据本身有问题，仍返回以保证下游不崩
        pass
    return best


def rotation_matrix(theta: float) -> np.ndarray:
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[c, -s], [s, c]], dtype=np.float64)


def homography_quad_to_unit(P: np.ndarray) -> np.ndarray:
    """把任意凸四边形映射到单位正方形（不涉及原点/轴向约定）。"""
    idx = _order_cyclic(P)
    src = P[idx].astype(np.float32)
    dst = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=np.float32)
    return cv2.getPerspectiveTransform(src, dst)


def _order_cyclic(pts: np.ndarray) -> np.ndarray:
    """按绕质心的角度排序，返回环形顺序的索引。"""
    c = pts.mean(axis=0)
    ang = np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0])
    return np.argsort(ang)


def _normalise_angle(deg: float) -> float:
    return float((deg + 180.0) % 360.0 - 180.0)


# --------------------------------------------------------------------------- #
# 场地坐标系
# --------------------------------------------------------------------------- #
@dataclass
class FieldFrame:
    """由四个角标记内顶点构建出的场地坐标系。"""

    ring_ids: list[int]  # [原点, +x 邻居, 对角, +y 邻居]
    corners_px: np.ndarray  # (4,2) 与 ring_ids 一一对应
    inner_index: dict[int, int]  # 每个角标记的"内顶点"在它自己四角中的序号
    H_img2field: np.ndarray  # 3x3，像素 → 场地（场地为 [0,1]²）
    H_field2img: np.ndarray
    side_px: float
    diagnostics: dict = field(default_factory=dict)

    @property
    def origin_id(self) -> int:
        return self.ring_ids[0]

    def to_field(self, pts: np.ndarray) -> np.ndarray:
        return apply_homography(self.H_img2field, pts)

    def to_image(self, pts: np.ndarray) -> np.ndarray:
        return apply_homography(self.H_field2img, pts)


# --------------------------------------------------------------------------- #
# 内顶点选择
# --------------------------------------------------------------------------- #
def _assignment_cost(
    cand: dict[int, int],
    obs: dict[int, MarkerObservation],
    w_margin: float,
) -> tuple[float, dict]:
    """评估一组内顶点选择，返回 (代价, 明细)。

    两个物理约束都用上了：

    1. **形状**（辅助判据）：把四个候选点映射到单位正方形得到单应 H，则每个角标记
       自己的四边形在 H 下必须还是正方形（标记是正方形 —— 与场地约定无关，很硬）。
       判据用 **相对** 残差 rms/边长，避免"离相机近的标记残差天然更大"造成的偏袒；
       且取平均而不是最大（实测平均明显更稳）。
    2. **位置**（主力判据）：正确的内顶点是"离场地中心最近"的那个顶点。
       场地中心用所选四点的质心自洽估计（不能用标记中心质心 —— 后者偏得厉害），
       惩罚项取 (d_选 - d_最近)/边长，正确组合恒为 0。

    ⚠ 权重必须给足（`vertex_margin_weight >= 0.05`）。原因：**4 个点唯一确定一个单应**，
    所以形状项在不同组合之间只差 ~1e-4 量级（实测 0.02996 vs 0.03310），而且它是
    "用错误的正方形去拟合"——错误组合反而可能把它压低（过拟合）。位置项的量级大得多
    （正确 0.000、次优 0.086），量级差 3 个数量级。若权重取得过小（如 0.02），
    形状项的噪声会盖过位置项，选出错误内顶点 —— 2026-10-03 新数据上 15/18 帧中招，
    位置偏 10~45mm、同一机位连拍帧间跳 9.6mm。0.05 以上即与 nearest 完全一致。
    """
    present = list(cand.keys())
    pts = np.array([obs[i].corners[cand[i]] for i in present], dtype=np.float64)
    H = homography_quad_to_unit(pts)
    if not np.all(np.isfinite(H)):
        return float("inf"), {}

    centre = pts.mean(axis=0)
    shape_sum = 0.0
    margin_sum = 0.0
    per_marker: dict[int, float] = {}
    for i in present:
        mapped = apply_homography(H, obs[i].corners)
        # 允许镜像：这里只问"这个四边形方不方"，与坐标系手性无关
        s, _, _, rms = fit_similarity(MARKER_LOCAL, mapped, allow_reflection=True)
        side_field = max(abs(float(s)), 1e-9)
        shape_sum += float(rms) / side_field
        per_marker[i] = float(rms) / side_field

        d = np.linalg.norm(obs[i].corners - centre, axis=1)
        scale = max(float(obs[i].side), 1e-6)
        margin_sum += (float(d[cand[i]]) - float(d.min())) / scale

    n = len(present)
    shape = shape_sum / n
    margin = margin_sum / n
    return shape + w_margin * margin, {
        "shape": shape,
        "margin": margin,
        "per_marker_shape": per_marker,
    }


def _segment_intersect(p1, p2, p3, p4):
    """线段 p1p2 与 p3p4 是否真交叉；交叉返回 (交点, 端点余量, 两线夹角)。

    "端点余量" = 交点在两条线段上距最近端点的相对位置（0 = 正好落在端点上，越接近
    0 表示这个配对越接近退化，凸性判据越不可靠）。
    """
    d1, d2 = p2 - p1, p4 - p3
    den = d1[0] * d2[1] - d1[1] * d2[0]
    n1, n2 = float(np.linalg.norm(d1)), float(np.linalg.norm(d2))
    if abs(den) < 1e-12 * max(n1 * n2, 1e-12):  # 两线平行或极近共线
        return None
    rel = p3 - p1
    t = (rel[0] * d2[1] - rel[1] * d2[0]) / den
    u = (rel[0] * d1[1] - rel[1] * d1[0]) / den
    if not (0.0 <= t <= 1.0 and 0.0 <= u <= 1.0):
        return None
    margin = min(t, 1.0 - t, u, 1.0 - u)  # 0 = 退化，0.5 = 最稳（交点在正中）
    cross = abs(den) / max(n1 * n2, 1e-12)  # = |sin 夹角|
    return p1 + t * d1, float(margin), float(cross)


def diagonal_pairs(centers: np.ndarray, min_sin: float = 0.15):
    """把四个点两两配成两条线段，取出"两条线段真正相交"的那一种配对。

    凸四边形只有**对角配对**的两条线段相交（相邻边配对、对边配对都不交），
    而透视投影保凸、保关联，所以这个判据在强透视下依然成立 —— 与相机姿态无关。

    `min_sin` 是安全闸：两条对角线的相交角正弦（=|sin 夹角|）。正常机位下场地
    四角的两条对角线近乎正交（实拍与合成 tilt≤0.60 实测中位 0.98、最小 0.98），
    而"四点近共线"（相机贴地掠过）会掉到 0.04 —— 那种几何下整个定位问题都不成立。
    低于 `min_sin` 一律判退化，交由调用方回退。

    返回 (pairs, P, margin, cross)，pairs = ((i,j),(k,l))，P = 交点（≈ 场地中心），
    margin = 交点在段内的位置余量（0.5 最稳），cross = |sin 夹角|；
    退化（四点共线/退化成凹/过薄）时返回 None。
    """
    cands = []
    for (a, b), (c, d) in (((0, 1), (2, 3)), ((0, 2), (1, 3)), ((0, 3), (1, 2))):
        got = _segment_intersect(centers[a], centers[b], centers[c], centers[d])
        if got is not None:
            cands.append((((a, b), (c, d)), got[0], got[1], got[2]))
    cands = [c for c in cands if c[3] >= min_sin]
    if not cands:
        return None
    # 正常情况恰好一种；若出现多种，取交点居中的那个配对
    cands.sort(key=lambda x: -x[2])
    (pairs, P, margin, cross) = cands[0]
    return pairs, P, float(margin), float(cross)


def select_inner_vertices(
    obs: dict[int, MarkerObservation],
    cfg: GeometryConfig,
    corner_ids: list[int],
) -> tuple[dict[int, int] | None, dict]:
    """为每个角标记挑出"朝向场地中心"的那个顶点，返回 {id: 顶点序号}。"""
    present = [i for i in corner_ids if i in obs]
    info: dict = {"present": present, "mode": cfg.vertex_mode}

    if len(present) < len(corner_ids):
        info["reason"] = "corner_markers_missing"
        info["missing"] = [i for i in corner_ids if i not in obs]
        if cfg.require_all_corners:
            return None, info
    if not present:
        info["reason"] = "no_corner_markers"
        return None, info

    centroid = np.mean([obs[i].center for i in present], axis=0)

    # 候选 1：离场地中心最近的顶点（物理约定：标记朝向场地中央的那个角压在场地角点上）
    dists = {i: np.linalg.norm(obs[i].corners - centroid, axis=1) for i in present}
    order = {i: np.argsort(dists[i]) for i in present}
    nearest = {i: int(order[i][0]) for i in present}
    margins = {
        i: float(dists[i][order[i][1]] - dists[i][order[i][0]]) / max(obs[i].side, 1e-6)
        for i in present
    }
    info["inner_margins"] = margins

    # 顶点数不足 4 个时无法求单应，只能直接采信"最近顶点"
    if len(present) < 4 or cfg.vertex_mode == "nearest":
        info["chosen_by"] = "nearest"
        info["assign"] = {int(k): int(v) for k, v in nearest.items()}
        info["cost"] = None
        return nearest, info

    # ---------------------------------------------------------------- #
    # 对角线法（xdiag / xdiag_unit）—— 纯几何构造，无代价函数、无权重、无搜索
    #   ① 四个标记中心两两配对成 3 种方案，取"两条线段相交"的那一种 = 对角配对
    #   ② 每条对角线取"指向对端"的方向 d；两个端点的标记各自取 4 个角点中
    #      与 d 点乘最大者 = 内顶点
    #   角点偏移方向与 d 的夹角理论余量 ≈ 88°（见下方注释），远大于距离/形状判据。
    # ---------------------------------------------------------------- #
    if cfg.vertex_mode in ("xdiag", "xdiag_unit", "xdiag_plane"):
        cts = np.array([obs[i].center for i in present], dtype=np.float64)
        got = diagonal_pairs(cts, float(getattr(cfg, "min_diag_sin", 0.15)))
        if got is None:
            info["chosen_by"] = "xdiag_degenerate"
            info["reason"] = "diagonal_pairing_degenerate"
            info["assign"] = {int(k): int(v) for k, v in nearest.items()}
            info["cost"] = None
            return nearest, info
        (ia, ib), (ic, idd) = got[0]
        info["diag_pair_margin"] = got[2]
        info["diag_pair_sin"] = got[3]
        info["diag_center_px"] = [float(got[1][0]), float(got[1][1])]
        info["diag_pairs"] = [
            [int(present[ia]), int(present[ib])], [int(present[ic]), int(present[idd])]
        ]
        use_unit = cfg.vertex_mode in ("xdiag_unit", "xdiag_plane")
        use_plane = cfg.vertex_mode == "xdiag_plane"
        assign: dict[int, int] = {}
        cos_margin: dict[int, float] = {}
        off_angle: dict[int, float] = {}
        for i0, j0 in ((ia, ib), (ic, idd)):
            for src, dst in ((i0, j0), (j0, i0)):
                mi = present[src]
                d = cts[dst] - cts[src]
                dn = float(np.linalg.norm(d))
                if dn < 1e-9:
                    continue
                offs = obs[mi].corners - obs[mi].center
                if use_plane:
                    # 用该标记自身四边形拟合"平面正方形 → 图像"的局部仿射，
                    # 再把方向与角点偏移都拉回**平面空间**比角度 —— 消掉透视的局部剪切。
                    # 不引入任何全局模型，也不需要内顶点（无鸡生蛋问题）。
                    src_pt = np.hstack([MARKER_LOCAL, np.ones((4, 1))])
                    dst_pt = np.asarray(obs[mi].corners, dtype=np.float64)
                    A, *_ = np.linalg.lstsq(src_pt, dst_pt, rcond=None)
                    M = A[:2, :].T  # 平面方向 → 图像方向
                    if abs(float(np.linalg.det(M))) < 1e-12:
                        continue
                    Minv = np.linalg.inv(M)
                    d = Minv @ d
                    offs = (Minv @ offs.T).T
                    dn = float(np.linalg.norm(d))
                    if dn < 1e-12:
                        continue
                if use_unit:  # 归一化后点乘 = 纯角度判据
                    offs = offs / np.maximum(np.linalg.norm(offs, axis=1, keepdims=True), 1e-12)
                proj = offs @ (d / dn)
                o = np.argsort(-proj)
                assign[mi] = int(o[0])
                cos_margin[mi] = float(proj[o[0]] - proj[o[1]])
                off_angle[mi] = float(np.degrees(np.arccos(np.clip(proj[o[0]], -1.0, 1.0))))
        if len(assign) == len(present):
            info["chosen_by"] = "xdiag"
            info["assign"] = {int(k): int(v) for k, v in assign.items()}
            info["xdiag_cos_margin"] = {int(k): v for k, v in cos_margin.items()}
            info["xdiag_best_angle_deg"] = {int(k): v for k, v in off_angle.items()}
            info["cost"] = None
            return assign, info

    candidates: list[tuple[str, dict[int, int]]] = [("nearest", nearest)]
    if cfg.vertex_mode in ("auto", "search", "full"):
        from itertools import product

        if cfg.vertex_mode == "full":
            pool = {i: list(order[i][:4]) for i in present}
        else:
            second = {i: int(order[i][1]) for i in present}
            pool = {i: [nearest[i], second[i]] for i in present}

        combos = list(product(*[pool[i] for i in present]))
        if cfg.max_vertex_search and len(combos) > cfg.max_vertex_search:
            # 组合过多时，先按"每个标记的顶点距离秩之和"排序，保留最有希望的前 N 组
            combos.sort(key=lambda c: sum(int(np.where(order[i] == k)[0][0]) for i, k in zip(present, c)))
            combos = combos[: cfg.max_vertex_search]
        for c in combos:
            cand = {i: int(k) for i, k in zip(present, c)}
            if cand == nearest:
                continue
            candidates.append(("search", cand))

    w = float(cfg.vertex_margin_weight)
    scored = []
    for name, cand in candidates:
        cost, detail = _assignment_cost(cand, obs, w)
        scored.append((cost, name, cand, detail))
    scored.sort(key=lambda x: x[0])

    best_cost, best_name, best, best_detail = scored[0]
    info["cost"] = best_cost
    info["chosen_by"] = best_name
    info["assign"] = {int(k): int(v) for k, v in best.items()}
    info["shape"] = best_detail.get("shape")
    info["margin_penalty"] = best_detail.get("margin")
    info["per_marker_shape"] = {
        int(k): float(v) for k, v in (best_detail.get("per_marker_shape") or {}).items()
    }
    info["costs"] = [
        {"by": n, "cost": c, "shape": (d or {}).get("shape"), "assign": {int(k): int(v) for k, v in a.items()}}
        for c, n, a, d in scored[:4]
    ]
    # 与次优组合的代价差距，供上层判断是否"险胜"
    if len(scored) > 1 and np.isfinite(scored[1][0]) and scored[0][0] > -np.inf:
        gap = scored[1][0] - scored[0][0]
        info["cost_gap"] = float(gap)
        info["cost_gap_rel"] = float(gap / max(abs(scored[0][0]), 1e-9))

    if best_name != "nearest":
        info["vertex_override"] = True
    return best, info


# --------------------------------------------------------------------------- #
# 由内顶点构建场地坐标系
# --------------------------------------------------------------------------- #
def build_frame(
    inner: dict[int, int],
    obs: dict[int, MarkerObservation],
    image_shape: tuple[int, int],
    cfg: GeometryConfig,
) -> tuple[FieldFrame | None, dict]:
    """内顶点 → 场地坐标系。原点取"离画面左下角最近的场地角点"。"""
    info: dict = {}
    ids = list(inner.keys())
    if len(ids) != 4:
        info["reason"] = "need_four_corners"
        return None, info

    pts = np.array([obs[i].corners[inner[i]] for i in ids], dtype=np.float64)
    h, w = image_shape[:2]
    bl = np.array([0.0, float(h - 1)])

    # 原点：离画面左下角最近的场地角点
    d_bl = np.linalg.norm(pts - bl, axis=1)
    o = int(np.argmin(d_bl))
    d_sorted = np.sort(d_bl)

    # 环序：绕质心角度排序天然非自交。
    # 注意不能用"离原点最近的两个点 = 邻居"：强透视下近处的边在画面里被放大，
    # 对角点可能比真正的邻居离原点更近（2026-09-25 迷宫实拍踩过这个坑，
    # 环序变成蝶形后 0 号标记会映射到场地外）。
    cyc = _order_cyclic(pts)
    pos = int(np.where(cyc == o)[0][0])
    ring = np.roll(cyc, -pos)  # [o, c1, 对角, c3]
    n1, far, n2 = int(ring[1]), int(ring[2]), int(ring[3])

    o_pt, p1, p2 = pts[o], pts[n1], pts[n2]
    side_px = float(np.mean([np.linalg.norm(pts[(k + 1) % 4] - pts[k]) for k in _order_cyclic(pts)]))

    # +x：两条边里在画面中更偏右的一条
    e1, e2 = (p1 - o_pt), (p2 - o_pt)
    e1u, e2u = e1 / np.linalg.norm(e1), e2 / np.linalg.norm(e2)
    if e1u[0] >= e2u[0]:
        x_pt, y_pt = p1, p2
        ex_u, ey_u = e1u, e2u
        x_id, y_id = ids[n1], ids[n2]
    else:
        x_pt, y_pt = p2, p1
        ex_u, ey_u = e2u, e1u
        x_id, y_id = ids[n2], ids[n1]

    info["origin_margin"] = float((d_sorted[1] - d_sorted[0]) / max(side_px, 1e-6))
    info["axis_margin"] = float(abs(ex_u[0]) - abs(ey_u[0]))
    # 手性检查：画面是 y 向下，cross(ex, ey) < 0 才等价于"θ 变大在画面上是逆时针"。
    # 若这里为假，说明"更偏右"这条判据选错了轴（相机绕光轴接近 ±45° 时会发生）。
    cross = float(ex_u[0] * ey_u[1] - ex_u[1] * ey_u[0])
    info["handedness_cross"] = cross
    info["theta_ccw"] = bool(cross < 0.0)

    src = np.array([o_pt, x_pt, pts[far], y_pt], dtype=np.float32)
    dst = FIELD_SQUARE_CORNERS.astype(np.float32)
    H = cv2.getPerspectiveTransform(src, dst)
    if not np.all(np.isfinite(H)):
        info["reason"] = "degenerate_homography"
        return None, info
    H_inv = np.linalg.inv(H)

    frame = FieldFrame(
        ring_ids=[ids[o], x_id, ids[far], y_id],
        corners_px=src.astype(np.float64),
        inner_index={i: int(inner[i]) for i in ids},
        H_img2field=H,
        H_field2img=H_inv,
        side_px=side_px,
        diagnostics=info,
    )
    return frame, info


# --------------------------------------------------------------------------- #
# 联合最小二乘精化
# --------------------------------------------------------------------------- #
def refine_homography(
    frame: FieldFrame,
    obs: dict[int, MarkerObservation],
    image_shape: tuple[int, int],
    cfg: RefineConfig,
) -> tuple[np.ndarray, dict]:
    """把 16 个角点一起用上，精化单应。

    未知量：H 的 8 个参数 + 4 个角标记各自的旋转与边长（共 16 个）；
    观测量：16 个角点 × 2 = 32 个残差。
    模型只用两条物理事实：标记是正方形、内顶点正好压在场地角点上。
    """
    info: dict = {"enabled": False}
    if not cfg.enable or not _HAS_SCIPY:
        info["reason"] = "scipy_missing" if not _HAS_SCIPY else "disabled"
        return frame.H_img2field, info

    w = float(image_shape[1])
    T = np.diag([1.0 / w, 1.0 / w, 1.0])  # 像素 → 归一化图像坐标

    H0 = np.asarray(frame.H_img2field, dtype=np.float64)
    if abs(H0[2, 2]) < 1e-12:
        info["reason"] = "bad_init"
        return frame.H_img2field, info
    H0 = H0 / H0[2, 2]
    Hn0 = H0 @ np.diag([w, w, 1.0])  # 归一化图像坐标下的初值

    ring = frame.ring_ids
    target_corners = FIELD_SQUARE_CORNERS  # 与 ring 一一对应

    # 初值：θ、s 由映射后的四边形拟合得到
    theta0, side0 = [], []
    for k, mid in enumerate(ring):
        mapped = apply_homography(H0, obs[mid].corners)
        s, R, _, _ = fit_similarity(MARKER_LOCAL, mapped)
        theta0.append(np.arctan2(R[1, 0], R[0, 0]))
        side0.append(max(s, 1e-4))
    info["enabled"] = True

    x0 = np.concatenate([Hn0.flatten()[:8], np.array(theta0), np.array(side0)])
    img_pts_n = [np.asarray(obs[mid].corners, dtype=np.float64) * (1.0 / w) for mid in ring]

    def unpack(x):
        Hn = np.append(x[:8], 1.0).reshape(3, 3)
        thetas = x[8:12]
        sides = x[12:16]
        return Hn, thetas, sides

    def residuals(x):
        Hn, thetas, sides = unpack(x)
        res = []
        for k, mid in enumerate(ring):
            inner_idx = frame.inner_index[mid]
            rel = MARKER_LOCAL_EDGE - MARKER_LOCAL_EDGE[inner_idx]
            pred = (sides[k] * (rotation_matrix(thetas[k]) @ rel.T)).T + target_corners[k]
            obs_field = apply_homography(Hn, img_pts_n[k])
            res.append((obs_field - pred).ravel())
        return np.concatenate(res)

    lower = np.concatenate([np.full(8, -1e3), np.full(4, -np.pi * 4), np.full(4, 1e-4)])
    upper = np.concatenate([np.full(8, 1e3), np.full(4, np.pi * 4), np.full(4, 0.5)])

    try:
        sol = least_squares(
            residuals, x0, bounds=(lower, upper), method="trf", x_scale="jac", max_nfev=int(cfg.max_nfev)
        )
    except Exception as exc:  # pragma: no cover
        info["reason"] = f"solver_failed:{exc}"
        return frame.H_img2field, info

    Hn, thetas, sides = unpack(sol.x)
    H_new = Hn @ T
    H_new = H_new / H_new[2, 2]

    r = sol.fun.reshape(-1, 2)
    info.update(
        {
            "cost_before": float(np.sqrt(np.mean(residuals(x0) ** 2))),
            "cost_after": float(np.sqrt(np.mean(sol.fun**2))),
            "rms_field": float(np.sqrt(np.mean(np.sum(r**2, axis=1)))),
            "success": bool(sol.success),
            "nfev": int(sol.nfev),
            "sides": [float(s) for s in sides],
        }
    )
    return H_new, info


# --------------------------------------------------------------------------- #
# 目标标记位姿
# --------------------------------------------------------------------------- #
@dataclass
class MarkerPose:
    center: np.ndarray  # 场地坐标
    theta_rad: float
    dir_vector: np.ndarray  # 场地系单位方向向量（标记自身 +x 轴）
    scale: float  # 标记边长 / 场地边长
    rms: float  # 四边形映射成正方形的残差（场地单位）


def fit_marker_pose_from_field(mapped: np.ndarray) -> MarkerPose:
    """由**已经映射到场地系**的四角拟合位姿。

    单独暴露这一层，是为了让调用方能在映射之后、拟合之前插入高度视差修正
    （修正只挪位置不改形状，见 parallax.py）。
    """
    scale, R, t, rms = fit_similarity(MARKER_LOCAL, mapped)
    theta = float(np.arctan2(R[1, 0], R[0, 0]))
    return MarkerPose(
        center=t,
        theta_rad=theta,
        dir_vector=np.array([R[0, 0], R[1, 0]], dtype=np.float64),
        scale=float(scale),
        rms=float(rms),
    )


def fit_marker_pose(H: np.ndarray, corners_px: np.ndarray) -> MarkerPose:
    """把标记四角映射到场地系，再拟合出中心 / 方向 / 尺度 / 残差。"""
    return fit_marker_pose_from_field(apply_homography(H, corners_px))


def corner_marker_scales(H: np.ndarray, obs: dict[int, MarkerObservation], ids: list[int]) -> list[float]:
    """各角标记在场地系下的边长估计，用于检查所有标记是否同尺寸。"""
    out = []
    for mid in ids:
        if mid in obs:
            scaled = apply_homography(H, obs[mid].corners)
            s, _, _, _ = fit_similarity(MARKER_LOCAL, scaled)
            out.append(float(s))
    return out
