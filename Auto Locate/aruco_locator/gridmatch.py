"""格点匹配通道：把目标识别做成**一次匹配滤波**，绕开 ArUco 的两段式检测。

为什么需要第二条通道
--------------------
ArUco 解 0 号标记是"找轮廓 → 位判决"两段式。真实比赛视频里失败几乎都发生在第一段：
运动模糊把卡片内格压成斜坡，四边形根本凑不出来（实测 4K 视频里 ID0 检出率仅 3%~8%）。
本模块换一条路：用四角标记解出的单应 H 把画面 warp 成场地正射图，然后在**整幅正射图
上**直接搜索"36 格图案与模板最吻合的位置与朝向"。它不需要轮廓、不需要位判决，
只要图案的灰度结构还在。

三个关键设计（都有实测依据，见 docs/评估-预处理加权与核权重标定-2026-10-30.md）
----------------------------------------------------------------------------
1. **输入是加权域 X，不是灰度**::

       v = (gray − 127.5) / 127.5          固定映射，绝不按帧拉伸
       S = (max(R,G,B) − min(R,G,B)) / max(max(R,G,B), sat_floor)
       X = v · (1 − S)

   色度高的像素（红胶带、木纹、彩灯）被压向 0，纯黑/纯白保留满权重。
2. **核按圈层加权并保持零和**：白 +1/n_w，内层黑 −1/(n_in+20κ)，外圈黑 −κ/(n_in+20κ)。
   κ=1 即按类归一化的原核。**κ 只进相关面（选解），不进判据。**
3. **格均值只取每格中心 (1−2m)d 见方**（m = inset）。这是唯一被验证为显著有效的
   抗模糊手段：592 帧整段视频最长连续可用段 104 → 313 帧，且与"全套改动"结果相同。

判据（与 ArUco 通道的外部校验一致，不另造打分函数）
--------------------------------------------------
``gap = min(白格) − max(黑格)``：gap > 0 ⟺ 36 格全部判对。
``sharp = gap / max(白格极差, 黑格极差)``：双电平性。灰度平滑区（地面、白墙）
gap 与分母同时趋零，只有 sharp 能把它和真卡片分开。
100 帧标定的零错收操作点：``sharp ≥ 0.20 且 gap ≥ 0.06``（召回 83%）。

坐标与角度
----------
``x, y`` 是 0 号标记的几何中心（场地归一化坐标，[0,1]²）；
``theta`` 是标记自身 +x 轴相对场地 +x 轴的夹角（逆时针为正），
由搜索给出的整体旋转 β 与核旋转 rot 换算：``θ = −β + 90·rot + theta_bias_deg``。
**不做差高视差修正**（与已验收的验证版逐位一致）：本通道解的是"场地平面（墙顶）"
上的位置，0 号标记低 27 mm 时该偏差由调用方按 parallax.py 自行处理。

用法::

    from aruco_locator.gridmatch import GridMatcher
    m = GridMatcher(cfg.grid)
    sol = m.search(frame_bgr, H)        # H = 四角标记解出的 img→field 单应
    sol["x"], sol["y"], sol["theta"], sol["gap"], sol["sharp"]
"""

from __future__ import annotations

import time

import cv2
import numpy as np

from .config import GridConfig

#: 场地实际边长（mm）。仅用于把 px 换算与正射尺寸绑定，不影响归一化坐标。
FIELD_MM_DEFAULT = 1440.0

#: 判据的量纲下限。X 域取值在 ±1 量级，灰度域的 1.0 在这里对应 1e-3，
#: 否则整幅图的分母都会被钉在 1.0，margin 退化成 gap 本身。
EPS = 1e-3

#: 外圈黑格掩膜（6×6 卡片最外一圈共 20 格）
BORDER = np.zeros((6, 6), bool)
BORDER[0, :] = BORDER[-1, :] = True
BORDER[:, 0] = BORDER[:, -1] = True


# --------------------------------------------------------------------------- #
# 模板（DICT_4X4_1000 的 ID0）
# --------------------------------------------------------------------------- #
def marker_bits(marker_id: int = 0) -> np.ndarray:
    """6×6 位图，True = 白。含 1 模块黑边，与印刷卡片的外框一致。"""
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_1000)
    img = cv2.aruco.generateImageMarker(d, int(marker_id), 6)
    return img > 127


def kernel_for(bits: np.ndarray, kappa: float = 1.0) -> np.ndarray:
    """零和加权核：白 +1/n_w，内层黑 −1/(n_in+20κ)，外圈黑 −κ/(n_in+20κ)。

    κ = 1 时与"按类归一化"的原始核完全一致（实测 max diff ≈ 0）。
    κ > 1 → 外圈更重（外圈是位置/尺度锚，压低它会让二义位置更容易对上）；
    κ < 1 → 外圈更轻。
    """
    b = np.asarray(bits, bool)
    n_w = int(b.sum())
    n_border = int(BORDER.sum())
    n_in = int((~b).sum()) - n_border
    denom = n_in + n_border * float(kappa)
    k = np.empty((6, 6), np.float32)
    k[b] = 1.0 / max(n_w, 1)
    k[~b] = -1.0 / denom
    k[BORDER] = -float(kappa) / denom
    if abs(float(k.sum())) > 1e-5:
        raise ValueError(f"核非零和：ΣK={k.sum():.3e}")
    return k


# --------------------------------------------------------------------------- #
# 输入侧预处理
# --------------------------------------------------------------------------- #
def chroma_weight(bgr: np.ndarray, sat_floor: float = 60.0) -> np.ndarray:
    """w = 1 − S。分母带下限，避免暗区量化噪声被放大成高饱和度。"""
    f = bgr.astype(np.float32)
    hi = f.max(axis=2)
    lo = f.min(axis=2)
    s = (hi - lo) / np.maximum(hi, float(sat_floor))
    return np.clip(1.0 - s, 0.0, 1.0)


def to_weighted(gray: np.ndarray, w: np.ndarray | None) -> np.ndarray:
    """固定线性映射到 (−1,1)，再乘 (1−S)。w=None 时只做归一化（消融基线）。"""
    v = (gray.astype(np.float32) - 127.5) / 127.5
    return v if w is None else v * w


# --------------------------------------------------------------------------- #
# 格均值 / 相关 / 判据
# --------------------------------------------------------------------------- #
def grid_means(integ: np.ndarray, n: int, d: int, sx: int, sy: int,
               inset: float = 0.0) -> np.ndarray:
    """从积分图取 n×n 个格均值，对齐到亚格相位 (sx, sy)。

    ``inset > 0`` 时每格只取中心 ``(1−2·inset)·d`` 见方 —— 避开格间过渡带，
    这是抗运动模糊的主要手段（m = 0.20 时衰减减少约 42%）。
    """
    di = int(round(d * (1.0 - 2.0 * float(inset))))
    off = (d - di) // 2
    i = np.arange(n)
    y0 = sy + i * d + off
    x0 = sx + i * d + off
    y1, x1 = y0 + di, x0 + di
    s = (integ[np.ix_(y1, x1)] - integ[np.ix_(y0, x1)]
         - integ[np.ix_(y1, x0)] + integ[np.ix_(y0, x0)])
    return (s / float(di * di)).astype(np.float32)


def correlate(g: np.ndarray, k: np.ndarray) -> np.ndarray:
    """6×6 核相关：out[i,j] = Σ K[u,v]·G[i+u, j+v]。"""
    n0, n1 = g.shape[0] - 5, g.shape[1] - 5
    out = np.zeros((n0, n1), np.float32)
    for u in range(6):
        for v in range(6):
            w = k[u, v]
            if w != 0.0:
                out += w * g[u:u + n0, v:v + n1]
    return out


def gap_surface(g: np.ndarray, bits: np.ndarray) -> np.ndarray:
    """逐位置的 gap 面 = min(白格) − max(黑格)，都是掩膜滑窗极值。

    36 次移位 min/max 累加即可，代价与一次相关同量级。
    """
    n0, n1 = g.shape[0] - 5, g.shape[1] - 5
    mn = np.full((n0, n1), np.inf, np.float32)
    mx = np.full((n0, n1), -np.inf, np.float32)
    for u in range(6):
        for v in range(6):
            sl = g[u:u + n0, v:v + n1]
            if bits[u, v]:
                np.minimum(mn, sl, out=mn)
            else:
                np.maximum(mx, sl, out=mx)
    return mn - mx


def cell_diag(g: np.ndarray, i: int, j: int, K: np.ndarray,
              bits: np.ndarray) -> dict:
    """36 格诊断：gap / margin / ncc / sharp / n_bad。"""
    X = g[i:i + 6, j:j + 6].astype(np.float64)
    w, b = X[bits], X[~bits]
    w_min, b_max = float(w.min()), float(b.max())
    gap = w_min - b_max
    mw, mb = float(w.mean()), float(b.mean())
    margin = gap / max(abs(mw - mb), EPS)
    thr = 0.5 * (w_min + b_max)
    n_bad = int(np.sum((X > thr) != bits))
    sharp = gap / max(float(w.max() - w.min()), float(b.max() - b.min()), EPS)
    # ncc：核零和 → ΣK·X = ΣK·(X−X̄)，分母按幅度归一（与对比度无关，只作诊断）
    num = float((K * X).sum())
    den = float(np.linalg.norm(X - X.mean())) * float(np.linalg.norm(K))
    return dict(gap=gap, margin=margin, n_bad=n_bad, thr=thr, mw=mw, mb=mb,
                ncc=(num / den if den > 1e-12 else 0.0), sharp=sharp)


# --------------------------------------------------------------------------- #
# 几何：格距 → px/mm → 正射图边长
# --------------------------------------------------------------------------- #
class GridGeometry:
    """把"格距 d 取整数像素"的约束反算成 px/mm 与正射图边长。

    d 必须是整数像素：亚格相位按 ``d/n_phase`` 取整，若 d 非整数，6 格累积误差
    会让卡片边缘明显错位。因此这里以 d 为主参数、由卡片物理尺寸反算 px/mm。
    """

    __slots__ = ("d_px", "card_mm", "field_mm", "margin", "px_per_mm", "rect_side")

    def __init__(self, d_px: int, card_mm: float, field_mm: float, margin: float):
        self.d_px = int(d_px)
        self.card_mm = float(card_mm)
        self.field_mm = float(field_mm)
        self.margin = float(margin)
        self.px_per_mm = self.d_px * 6.0 / max(self.card_mm, 1e-9)
        self.rect_side = int(round(self.px_per_mm * self.field_mm
                                   * (1.0 + 2.0 * self.margin)))

    def affine(self) -> np.ndarray:
        """场地归一化坐标 → 正射图像素（+y 向上，故 y 轴翻转）。"""
        p = self.px_per_mm * self.field_mm
        return np.array([[p, 0.0, self.margin * p],
                         [0.0, -p, (1.0 + self.margin) * p],
                         [0.0, 0.0, 1.0]], np.float64)

    def warp(self, img: np.ndarray, H: np.ndarray) -> np.ndarray:
        """原图 → 场地正射图。"""
        T = self.affine() @ np.asarray(H, np.float64)
        return cv2.warpPerspective(img, T, (self.rect_side, self.rect_side),
                                   flags=cv2.INTER_LINEAR, borderValue=0)


def rotate_pad(img: np.ndarray, beta_deg: float):
    """整体旋转 + 零填充。返回 (旋转图, 3×3 变换 原图→旋转图, 边长)。"""
    h, w = img.shape[:2]
    s = int(np.ceil(max(h, w) * 1.4143)) + 2
    m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), beta_deg, 1.0)
    m[0, 2] += s / 2.0 - w / 2.0
    m[1, 2] += s / 2.0 - h / 2.0
    rot = cv2.warpAffine(img, m, (s, s), flags=cv2.INTER_LINEAR, borderValue=0)
    return rot, np.vstack([m, [0.0, 0.0, 1.0]]), s


# --------------------------------------------------------------------------- #
# 主体
# --------------------------------------------------------------------------- #
class GridMatcher:
    """在场地正射图上做全局匹配滤波，返回 0 号标记的位置与朝向。"""

    def __init__(self, cfg: GridConfig | None = None):
        self.cfg = cfg or GridConfig()
        self.bits = marker_bits(int(self.cfg.marker_id))
        self.K = kernel_for(self.bits, float(self.cfg.kappa))
        self._geo = GridGeometry(self.cfg.d_px, self.cfg.card_mm,
                                 self.cfg.field_size_mm, self.cfg.field_margin)
        self.last_debug: dict = {}

    # ------------------------------------------------------------------ #
    # 尺度
    # ------------------------------------------------------------------ #
    def geometry(self, d_px: int | None = None) -> GridGeometry:
        if d_px is None or int(d_px) == self._geo.d_px:
            return self._geo
        return GridGeometry(int(d_px), self.cfg.card_mm,
                            self.cfg.field_size_mm, self.cfg.field_margin)

    def weighted_rect(self, bgr: np.ndarray, H: np.ndarray,
                      d_px: int | None = None) -> np.ndarray:
        """原图 → 正射 → 加权域 X（float32）。"""
        g = self.geometry(d_px)
        rect = g.warp(bgr, H)
        w = chroma_weight(rect, self.cfg.sat_floor) if self.cfg.chroma_weight else None
        gray = cv2.cvtColor(rect, cv2.COLOR_BGR2GRAY)
        del rect
        return to_weighted(gray, w)

    # ------------------------------------------------------------------ #
    # 一级：半分辨率全局粗搜
    # ------------------------------------------------------------------ #
    def search_coarse(self, X: np.ndarray, d: int, beta_step: float,
                      n_phase: int, topk: int) -> dict:
        """全画布 × 全 β × 4 核旋转 × 亚相位，按余量面取全局峰（不做位置先验）。"""
        bits = self.bits
        K = self.K
        inset = float(self.cfg.inset)
        phases = [int(round(t * d / n_phase)) for t in range(n_phase)]
        pmax = max(phases)
        ones = np.ones(X.shape[:2], np.float32)
        Ms: dict[float, np.ndarray] = {}
        cands: list[tuple] = []
        t0 = time.perf_counter()
        for beta in np.arange(0.0, 90.0, beta_step):
            rot, M, s = rotate_pad(X, beta)
            Ms[round(float(beta), 3)] = M
            integ = cv2.integral(rot, sdepth=cv2.CV_64F)
            # 合法区：把 ones 用同一变换旋转，取窗口内面积占比。
            # X 的真值本身可以是 0 甚至负数，不能用 X>0 当掩膜。
            rotm = cv2.warpAffine(ones, M[:2], (s, s), flags=cv2.INTER_LINEAR,
                                  borderValue=0)
            integm = cv2.integral(rotm, sdepth=cv2.CV_64F)
            del rotm
            n = (s - d - pmax) // d
            for sy in phases:
                for sx in phases:
                    g = grid_means(integ, n, d, sx, sy, inset)
                    i = np.arange(n - 5)
                    y0, y1 = sy + i * d, sy + i * d + 6 * d
                    x0, x1 = sx + i * d, sx + i * d + 6 * d
                    vs = (integm[np.ix_(y1, x1)] - integm[np.ix_(y0, x1)]
                          - integm[np.ix_(y1, x0)] + integm[np.ix_(y0, x0)])
                    ok = vs >= 36.0 * d * d * 0.999
                    for r in range(4):
                        sc = correlate(g, np.rot90(K, r))
                        gp = gap_surface(g, np.rot90(bits, r))
                        mg = gp / np.maximum(np.abs(sc), EPS)
                        mg[~ok] = -9.0
                        k = int(np.argmax(mg))
                        ii, jj = divmod(k, n - 5)
                        cands.append((float(mg.ravel()[k]), round(float(beta), 3), r,
                                      sx, sy, int(ii), int(jj), int(n),
                                      float(sc.ravel()[k])))
            del rot, integ, integm
        A = self.geometry(d).affine()

        def to_field(beta, sx, sy, ii, jj):
            p = np.linalg.inv(Ms[beta]) @ np.array(
                [sx + (jj + 3) * d, sy + (ii + 3) * d, 1.0])
            return (float((p[0] - A[0, 2]) / A[0, 0]),
                    float((p[1] - A[1, 2]) / A[1, 1]))

        cands.sort(key=lambda c: -c[0])
        nms = float(self.cfg.nms_mm)
        peaks: list[dict] = []
        for c in cands:
            x, y = to_field(c[1], c[3], c[4], c[5], c[6])
            if any(((x - p["x"]) ** 2 + (y - p["y"]) ** 2)
                   * self.cfg.field_size_mm ** 2 < nms ** 2 for p in peaks):
                continue
            peaks.append(dict(margin=c[0], beta=c[1], rot=c[2], x=x, y=y,
                              score=c[8], phase=(c[3], c[4]), n=c[7],
                              ij=c[5] * c[7] + c[6]))
            if len(peaks) >= topk:
                break
        return dict(peaks=peaks, t=time.perf_counter() - t0, n_eval=len(cands))

    # ------------------------------------------------------------------ #
    # 二级：全分辨率局部细化
    # ------------------------------------------------------------------ #
    def _refine(self, X: np.ndarray, d: int, seeds: list[dict]) -> list[dict]:
        """只 warp 局部块（不生成整幅旋转图），这是主要提速来源。"""
        A = self.geometry(d).affine()
        bits = self.bits
        K = self.K
        inset = float(self.cfg.inset)
        span = int(self.cfg.span)
        n_phase = int(self.cfg.refine_phase)
        beta_half = float(self.cfg.beta_half)
        beta_step = float(self.cfg.refine_beta_step)
        h, w = X.shape[:2]
        ctr = (w / 2.0, h / 2.0)
        phases = [int(round(t * d / n_phase)) for t in range(n_phase)]
        pmax = max(phases)
        win = (2 * span + 7) * d + pmax
        nloc = (win - pmax) // d
        lim = 2 * span + 1
        out: list[dict] = []
        for sd in seeds:
            cx = A[0, 0] * sd["x"] + A[0, 2]
            cy = A[1, 1] * sd["y"] + A[1, 2]
            best = None
            for beta in np.arange(sd["beta"] - beta_half,
                                  sd["beta"] + beta_half + 1e-6, beta_step):
                m = cv2.getRotationMatrix2D(ctr, beta, 1.0)
                Mi = np.linalg.inv(np.vstack([m, [0.0, 0.0, 1.0]]))
                q = np.vstack([m, [0.0, 0.0, 1.0]]) @ np.array([cx, cy, 1.0])
                qx, qy = q[0] / q[2], q[1] / q[2]
                j0 = int(round(qx / d - 3.0))
                i0 = int(round(qy / d - 3.0))
                X0, Y0 = (j0 - span) * d, (i0 - span) * d
                Mloc = np.hstack([Mi[:2, :2],
                                  (Mi[:2, :2] @ np.array([X0, Y0])
                                   + Mi[:2, 2]).reshape(2, 1)])
                patch = cv2.warpAffine(X, Mloc, (win, win),
                                       flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                                       borderValue=0)
                integ = cv2.integral(patch, sdepth=cv2.CV_64F)
                del patch
                for sy in phases:
                    for sx in phases:
                        g = grid_means(integ, nloc, d, sx, sy, inset)
                        for r in range(4):
                            sc = correlate(g, np.rot90(K, r))
                            gp = gap_surface(g, np.rot90(bits, r))
                            sub = (gp / np.maximum(np.abs(sc), EPS))[:lim, :lim]
                            k = int(np.argmax(sub))
                            val = float(sub.ravel()[k])
                            if best is not None and val <= best["margin"]:
                                continue
                            ii, jj = divmod(k, lim)
                            gx = X0 + sx + (jj + 3) * d
                            gy = Y0 + sy + (ii + 3) * d
                            p = Mi @ np.array([gx, gy, 1.0])
                            best = dict(margin=val, beta=float(beta), rot=int(r),
                                        x=float((p[0] - A[0, 2]) / A[0, 0]),
                                        y=float((p[1] - A[1, 2]) / A[1, 1]),
                                        _sx=sx, _sy=sy, _ii=ii, _jj=jj,
                                        _X0=X0, _Y0=Y0, _beta=float(beta))
            if best is None:
                continue
            # 用与搜索**完全相同的几何**重算一次 36 格，补齐诊断量
            beta = best["_beta"]
            m = cv2.getRotationMatrix2D(ctr, beta, 1.0)
            Mi = np.linalg.inv(np.vstack([m, [0.0, 0.0, 1.0]]))
            Mloc = np.hstack([Mi[:2, :2],
                              (Mi[:2, :2] @ np.array([best["_X0"], best["_Y0"]])
                               + Mi[:2, 2]).reshape(2, 1)])
            patch = cv2.warpAffine(X, Mloc, (win, win),
                                   flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                                   borderValue=0)
            g = grid_means(cv2.integral(patch, sdepth=cv2.CV_64F), nloc, d,
                           best["_sx"], best["_sy"], inset)
            cd = cell_diag(g, best["_ii"], best["_jj"],
                           np.rot90(K, best["rot"]), np.rot90(bits, best["rot"]))
            best.update(cd)
            best["seed_x"], best["seed_y"] = sd["x"], sd["y"]
            best["seed_margin"] = sd["margin"]
            for t in ("_sx", "_sy", "_ii", "_jj", "_X0", "_Y0", "_beta"):
                best.pop(t, None)
            out.append(best)
        return out

    # ------------------------------------------------------------------ #
    # 对外：两级搜索
    # ------------------------------------------------------------------ #
    def search(self, bgr: np.ndarray, H: np.ndarray) -> dict:
        """彩色原图 + 单应 → 最优解（选解准则 = 36 格最小余量 margin）。

        返回 dict 至少含 x / y / theta / gap / sharp / margin / beta / rot /
        n_bad / ncc / seed_spread_mm / t_total。
        """
        cfg = self.cfg
        t0 = time.perf_counter()
        Xc = self.weighted_rect(bgr, H, int(cfg.coarse_d_px))
        ec = self.search_coarse(Xc, int(cfg.coarse_d_px), float(cfg.coarse_beta_step),
                                int(cfg.coarse_phase), max(8, int(cfg.n_seed)))
        del Xc
        t1 = time.perf_counter()
        X = self.weighted_rect(bgr, H, int(cfg.d_px))
        seeds = sorted(ec["peaks"], key=lambda p: -p["margin"])[:int(cfg.n_seed)]
        cands = self._refine(X, int(cfg.d_px), seeds)
        t2 = time.perf_counter()
        best = dict(max(cands, key=lambda c: c["margin"]))
        best.update(theta=self.theta_of(best["beta"], best["rot"]),
                    kappa=float(cfg.kappa), inset=float(cfg.inset),
                    d_px=int(cfg.d_px), t_coarse=round(t1 - t0, 2),
                    t_fine=round(t2 - t1, 2), t_total=round(t2 - t0, 2),
                    n_seed=len(seeds), n_cand=len(cands),
                    coarse_x=float(seeds[0]["x"]), coarse_y=float(seeds[0]["y"]),
                    coarse_beta=float(seeds[0]["beta"]),
                    coarse_margin=float(seeds[0]["margin"]))
        xs = [c["x"] for c in cands]
        ys = [c["y"] for c in cands]
        best["seed_spread_mm"] = float(
            np.hypot((max(xs) - min(xs)) * cfg.field_size_mm,
                     (max(ys) - min(ys)) * cfg.field_size_mm))
        best["runner_margin"] = (float(sorted(c["margin"] for c in cands)[-2])
                                 if len(cands) > 1 else -9.0)
        self.last_debug = dict(best)
        return best

    def theta_of(self, beta: float, rot: int) -> float:
        """(β, rot) → 标记自身 +x 轴相对场地 +x 轴的夹角，折到 (−180, 180]。"""
        t = (-float(beta) + 90.0 * int(rot) + float(self.cfg.theta_bias_deg)) % 360.0
        return t - 360.0 if t > 180.0 else t

    # ------------------------------------------------------------------ #
    def accept(self, sol: dict) -> bool:
        """操作点判定：sharp ≥ sharp_min 且 gap ≥ gap_min（100 帧标定，0 错收）。"""
        return (float(sol.get("sharp", -9.0)) >= float(self.cfg.sharp_min)
                and float(sol.get("gap", -9.0)) >= float(self.cfg.gap_min))
