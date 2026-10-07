"""高度视差修正：把"参考平面上的投影"还原成"0 号标记真实的场地位置"。

## 问题

场地坐标系由四个角标记定义，它们都落在**同一个水平平面**上（参考平面，离地 h_c）。
由四角标记内顶点求出的单应 H 只对这个平面精确。

0 号标记装在电脑鼠顶上，离地 h_r，一般 h_r ≠ h_c。相机不可能在无穷远，于是同一个
水平位置 (X, Y) 在高度 h_r 处的成像点，沿视线打到参考平面上会落到**另一个点**：

    X_c = N + (X - N) / k ,      k = (Hc - h_r) / (Hc - h_c)

反解即得修正：

    X = N + k * (X_c - N)

其中
    N  —— **天底点**：相机光心正下方那块场地位置（场地归一化坐标，[0,1]²）
    Hc —— 相机光心离地高度（mm）

推导：光心 C = (N, Hc)。标记的真实位置 P = (X, h_r)，从 C 出发过 P 的射线打到平面
z = h_c 的参数为 s = (Hc - h_c)/(Hc - h_r) = 1/k，于是交点
X_c = N + s(P_xy - N)，反解即得上式。

## 三个重要性质

1. 这是一个以 N 为中心、比例 1/k 的**位似（相似）变换**：既不改变标记的形状，
   也不改变它的方向。所以**只需修正位置，方向 θ 不用动**（这一点很容易被误当成
   需要旋转整块标记，见 2026-09-25 的推导记录）。
2. 修正量 = (k - 1)·|X_c - N|，**在天底点处恒为 0**，离天底点越远越大。
   所以把相机装在场地正上方误差最小；也正因如此，"在落点处放一个标记、
   读管线输出的坐标"就是天底点的直接标定法（见下）。
3. k 只由三个高度决定，与画面内容、与相机内参、与场地实际边长**都无关**
   （分子分母同除场地边长即知）。k = 1 ⇔ 无需修正（同高，或相机在无穷远）。

## 标定（一次性，与画面无关）

- **Hc**：卷尺量镜头光心到地面的高度即可，**不必测准**。
  因为 (k-1) = (h_c - h_t)/(Hc - h_c) 只依赖 Hc - h_c，Hc 的相对误差会**等比例**
  传给它：±20% 的 Hc 误差 → 修正量本身偏 ±20%。本项目修正量约 9mm（Hc=2m 时），
  故绝对残差只有 ~1.8mm（0.13% 场地边长）。
- **N** ：
  * 相机装在场地中心正上方 → 直接用 (0.5, 0.5)；
  * 否则把 0 号标记（或任意一个平放的标记）放到落点处拍一张 —— 天底点处修正量为 0，
    管线直接给出的 (x, y) 就是 N，不用任何换算；
  * 有内参时也可以由"图像→场地"单应分解位姿自动求出（:func:`nadir_from_intrinsics`）。

## 本项目实测参数（2026-09-25 实拍）

- 迷宫墙高 50mm，四个角标记与墙顶平行（支撑件 0~2mm）→ 参考平面 h_c = 50mm
- 0 号标记装在电脑鼠顶上，离地约 23mm → h_r = 23mm
- 目标比参考平面**低 27mm**，k > 1，修正把结果**推离**天底点。
- 迷宫为 **8×8 格**（格距 180mm）→ 场地边长 1440mm。注意边长**不影响** k、
  也不影响归一化输出，只在把修正量换算成 mm 时用到（Hc=2m 时换算得 ~9mm）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


# --------------------------------------------------------------------------- #
# 修正系数 k
# --------------------------------------------------------------------------- #
def correction_factor(
    h_corner_mm: float, h_target_mm: float, camera_height_mm: float
) -> float | None:
    """k = (Hc - h_target) / (Hc - h_corner)。单位只需一致，mm 即可。

    k > 1：目标低于参考平面，修正把结果推离天底点；
    k < 1：目标高于参考平面，修正把结果拉向天底点；
    k = 1：无需修正。
    相机低于参考平面时无解（返回 None）。
    """
    denom = float(camera_height_mm) - float(h_corner_mm)
    if denom <= 1e-9:
        return None
    k = (float(camera_height_mm) - float(h_target_mm)) / denom
    if not np.isfinite(k) or k <= 0.0:
        return None
    return float(k)


def camera_height_from_factor(k: float | None, h_corner_mm: float, h_target_mm: float) -> float | None:
    """由 k 反推相机高度（mm）。给"用画面自己标定出来"留的口子。

    k = (Hc - h_r)/(Hc - h_c)  =>  Hc = (k·h_c - h_r) / (k - 1)
    """
    if k is None or not np.isfinite(k) or abs(float(k) - 1.0) < 1e-9:
        return None
    return float((float(k) * float(h_corner_mm) - float(h_target_mm)) / (float(k) - 1.0))


def factor_from_scales(corner_scales, target_scale: float | None) -> float | None:
    """由"标记在场地系下的边长"之比反推 k —— 纯图像、不需要任何高度。

    位似把 0 号标记的测量尺度压成真值的 1/k，而角标记都在参考平面上不受影响，
    所以 k ≈ mean(角标记尺度) / 0 号标记尺度。所有标记必须是同一印刷尺寸。

    注意：只有几个像素的小标记上，角点噪声就能让尺度散布 1~3%，而本项目
    (k-1) 只有约 1.4%，所以这条只能当**交叉校验**，不能替代 Hc 实测。
    实拍 226 实测：本式给 0.9826（<1，即"目标更高"），与真值 1.0138 **符号相反**，
    偏差 1.7% 就是尺度噪声本身。结论：本项目尺度下它连符号都不可信，仅供存档。
    """
    s = [float(v) for v in np.ravel(corner_scales) if np.isfinite(v) and v > 0.0]
    if not s or target_scale is None:
        return None
    t = float(target_scale)
    if not np.isfinite(t) or t <= 0.0:
        return None
    return float(np.mean(s) / t)


# --------------------------------------------------------------------------- #
# 修正本身
# --------------------------------------------------------------------------- #
def apply_parallax(pts_field: np.ndarray, nadir_xy, k: float) -> np.ndarray:
    """X = N + k (X_c - N)。pts_field 为场地归一化坐标 (N,2)。"""
    p = np.asarray(pts_field, dtype=np.float64).reshape(-1, 2)
    n = np.asarray(nadir_xy, dtype=np.float64).reshape(1, 2)
    return n + float(k) * (p - n)


# --------------------------------------------------------------------------- #
# 由内参自动求天底点
# --------------------------------------------------------------------------- #
def pose_from_plane_homography(H_field2img: np.ndarray, K: np.ndarray):
    """由"平面单位正方形 → 图像"的单应 + 内参分解位姿。

    平面取它自身的坐标系 (u, v) ∈ [0,1]²、法线为 +z。因为平面是按单位正方形
    标定的，返回的平移量与光心位置都**以场地边长为单位**（即已经是归一化场地
    坐标），完全不需要知道场地实际边长 —— 这正是天底点能"免费"得到的原因。

    **方向很容易搞错**：这里要的是 平面→图像。
    管线里的 `H_img2field` 是反方向，用之前必须先求逆（:func:`solve` 已经处理）。

    返回 (R, t, C)：X_cam = R @ X_plane + t，C = -Rᵀt 是光心在平面系中的位置。
    """
    H = np.asarray(H_field2img, dtype=np.float64).copy()
    K = np.asarray(K, dtype=np.float64)
    if H.shape != (3, 3) or K.shape != (3, 3):
        return None
    if abs(H[2, 2]) > 1e-15:
        H = H / H[2, 2]
    try:
        A = np.linalg.inv(K) @ H
    except np.linalg.LinAlgError:
        return None
    h1, h2, h3 = A[:, 0], A[:, 1], A[:, 2]
    n1, n2 = float(np.linalg.norm(h1)), float(np.linalg.norm(h2))
    if n1 < 1e-12 or n2 < 1e-12:
        return None
    lam = 2.0 / (n1 + n2)  # 两列的尺度取平均，比只用一列稳
    r1, r2 = h1 * lam, h2 * lam

    # 正交化（h1·h2 理论为 0，实测有噪声）
    c = float(r1 @ r2)
    r1o, r2o = r1 - 0.5 * c * r2, r2 - 0.5 * c * r1
    for v in (r1o, r2o):
        if float(np.linalg.norm(v)) < 1e-12:
            return None
    r1o = r1o / np.linalg.norm(r1o)
    r2o = r2o / np.linalg.norm(r2o)
    r3 = np.cross(r1o, r2o)
    R = np.column_stack([r1o, r2o, r3])
    t = h3 * lam

    C = -R.T @ t
    if C[2] < 0.0:  # 光心必须在平面法线正侧（相机在参考平面上方）
        R = R @ np.diag([-1.0, -1.0, 1.0])
        t = -t
        C = -R.T @ t
    if not np.all(np.isfinite(C)):
        return None
    return R, t, C


def nadir_from_intrinsics(H_field2img: np.ndarray, K: np.ndarray) -> np.ndarray | None:
    """光心在场地系中的水平投影 = 天底点（归一化场地坐标）。

    H_field2img 必须是 **场地 → 图像** 方向的单应（`H_img2field` 的逆）。
    """
    res = pose_from_plane_homography(H_field2img, K)
    if res is None:
        return None
    _, _, C = res
    return np.array([float(C[0]), float(C[1])], dtype=np.float64)


# --------------------------------------------------------------------------- #
# 配置 → 解
# --------------------------------------------------------------------------- #
@dataclass
class ParallaxSolution:
    """一次解算的完整结果（写进 diagnostics.parallax）。"""

    k: float
    nadir: np.ndarray
    nadir_source: str  # configured | intrinsics
    camera_height_mm: float | None = None
    info: dict = field(default_factory=dict)

    def apply(self, pts_field: np.ndarray) -> np.ndarray:
        return apply_parallax(pts_field, self.nadir, self.k)

    def to_dict(self) -> dict:
        d = {
            "k": round(float(self.k), 8),
            "nadir_x": round(float(self.nadir[0]), 6),
            "nadir_y": round(float(self.nadir[1]), 6),
            "nadir_source": self.nadir_source,
        }
        if self.camera_height_mm is not None:
            d["camera_height_mm"] = round(float(self.camera_height_mm), 2)
        d.update(self.info)
        return d


def _plausible_nadir(n: np.ndarray | None, lo: float = -1.0, hi: float = 2.0) -> bool:
    """天底点允许落在场地外（相机不必在场地正上方），但不能离谱。"""
    if n is None or n.shape != (2,) or not np.all(np.isfinite(n)):
        return False
    return bool(lo <= n[0] <= hi and lo <= n[1] <= hi)


def _nadir_from_img2field(H_img2field: np.ndarray, K: np.ndarray) -> np.ndarray | None:
    """管线拿到的单应是 图像→场地；位姿分解要 场地→图像，这里替调用方求逆。"""
    H = np.asarray(H_img2field, dtype=np.float64)
    if H.shape != (3, 3):
        return None
    try:
        H_f2i = np.linalg.inv(H)
    except np.linalg.LinAlgError:
        return None
    return nadir_from_intrinsics(H_f2i, K)


def solve(
    cfg,
    H_img2field: np.ndarray,
    K: np.ndarray | None = None,
    corner_scales=None,
    target_scale: float | None = None,
) -> tuple[ParallaxSolution | None, dict]:
    """按配置解出 (k, 天底点)。返回 (解 or None, 诊断字典)。

    配置不全、或解不合理时返回 (None, 诊断)，诊断里会写清楚缺什么 ——
    上层据此打 `parallax_unconfigured` 而不是静默给一个错结果。
    """
    diag: dict = {"enable": bool(getattr(cfg, "enable", False))}
    if not diag["enable"]:
        return None, diag

    h_c = float(cfg.corner_height_mm)
    h_t = float(cfg.target_height_mm)
    diag["corner_height_mm"] = h_c
    diag["target_height_mm"] = h_t
    diag["height_diff_mm"] = round(h_c - h_t, 3)

    # ---- k ----
    if cfg.camera_height_mm is None:
        diag["reason"] = "camera_height_mm 未标定"
        if corner_scales is not None:
            k_scale = factor_from_scales(corner_scales, target_scale)
            if k_scale is not None:
                diag["k_from_scale"] = round(float(k_scale), 5)
        return None, diag
    k = correction_factor(h_c, h_t, float(cfg.camera_height_mm))
    if k is None:
        diag["reason"] = "相机高度必须高于参考平面"
        return None, diag
    diag["camera_height_mm"] = float(cfg.camera_height_mm)
    diag["k"] = round(k, 8)
    diag["k_minus_1_pct"] = round((k - 1.0) * 100.0, 4)

    # ---- 天底点 ----
    nadir, src = None, ""
    if cfg.nadir_xy is not None:
        n = np.asarray(cfg.nadir_xy, dtype=np.float64).ravel()
        if _plausible_nadir(n):
            nadir, src = n, "configured"
        else:
            diag["nadir_rejected"] = "nadir_xy 超出合理范围 [-1,2]²"
    if nadir is None and getattr(cfg, "from_intrinsics", True) and K is not None:
        n = _nadir_from_img2field(H_img2field, K)
        if _plausible_nadir(n):
            nadir, src = n, "intrinsics"
        else:
            diag["nadir_rejected"] = "单应分解得到的天底点不合理"
    if nadir is None:
        diag["reason"] = diag.get("reason") or "缺少天底点（nadir_xy 或相机内参）"
        return None, diag

    sol = ParallaxSolution(
        k=k,
        nadir=np.asarray(nadir, dtype=np.float64),
        nadir_source=src,
        camera_height_mm=float(cfg.camera_height_mm),
    )
    # 与失败路径的 diag 保持同一套字段，调用方不必区分成功/失败就能读
    sol.info["corner_height_mm"] = h_c
    sol.info["target_height_mm"] = h_t
    sol.info["height_diff_mm"] = round(h_c - h_t, 3)
    sol.info["k_minus_1_pct"] = round((k - 1.0) * 100.0, 4)
    # 交叉校验：纯图像估的 k（噪声大，仅供参考）
    if corner_scales is not None:
        k_scale = factor_from_scales(corner_scales, target_scale)
        if k_scale is not None:
            sol.info["k_from_scale"] = round(float(k_scale), 5)
    diag["nadir_x"] = round(float(nadir[0]), 6)
    diag["nadir_y"] = round(float(nadir[1]), 6)
    diag["nadir_source"] = src
    return sol, diag
