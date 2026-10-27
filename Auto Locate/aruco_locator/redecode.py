"""候选四边形二次解码（quad-level re-decode）—— 应对运动模糊导致的"位判决失败"。

为什么需要
----------
运动模糊不会让标记"消失"，但会把模块边界抹平：OpenCV 的检测器在**位提取阶段**
判定失败，把真标记的几个四边形丢进 `rejectedImgPoints`；或把位错解成别的 ID
（实测 id2 → 998）。两种情况都表现为 `corner_markers_missing` / `target_marker_missing`。

实测证据（2026-10-27 最终 4K 视频，帧 200）：14 个拒绝候选里
`(243,1873) 边长 133` 正是 id4、`(2743,222) 边长 45` 正是 id2、`(1164,179)` 正是 id1；
对它们重解码可得 **Hamming 距离 0** 的正确 ID。

做法
----
1. 把候选四边形 warp 成 6×6 模块网格（默认 72px，即 12px/模块，对小标记是 3× 上采样）；
2. 用"外圈边框（恒黑）中位 vs 数据区中位"的自适应阈值取 4×4 位；
3. 与**白名单模板**（角标记 + 目标标记，通常只有 5 个 ID）比 Hamming，4 个旋转取最小；
4. 接受条件：`dist ≤ max_dist`、与次优间隔 `≥ min_margin`、对比度 `≥ min_contrast`。

白名单是关键：只在 5 个 ID 里选，等价于把"1000 选 1"变成"5 选 1"，
所以模糊帧里那些"差 3~4 位"的图案也能可靠归类。

注意：本模块**不做几何校验**。误接受由上层（`geometry.max_square_rms` 的方正度检查、
重投影残差）兜底 —— 这也是为什么不建议把阈值放宽到"能对上任何 ID"。
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


def _bits_from_marker_image(img: np.ndarray) -> np.ndarray:
    m = cv2.resize(img, (6, 6), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    return (m[1:5, 1:5] > 0.5).astype(np.uint8)


@dataclass
class DecodeResult:
    marker_id: int
    dist: int          # 与模板的 Hamming 距离（16 位）
    margin: int        # 次优 ID 的距离 - 最优距离
    contrast: float    # 数据区中位 - 边框中位（灰度级）


class QuadDecoder:
    """白名单模板匹配解码器（线程内复用；模板只算一次）。"""

    def __init__(self, dictionary: "cv2.aruco.Dictionary", ids: list[int],
                 side: int = 72) -> None:
        self.side = int(side)
        self.templates = {
            int(mid): _bits_from_marker_image(cv2.aruco.generateImageMarker(dictionary, int(mid), 6))
            for mid in ids
        }

    # ------------------------------------------------------------------ #
    def sample(self, gray: np.ndarray, quad: np.ndarray) -> tuple[np.ndarray, float]:
        """返回 (4×4 位图, 对比度)。"""
        side = self.side
        q = np.asarray(quad, np.float32).reshape(4, 2)
        dst = np.array([[0, 0], [side - 1, 0], [side - 1, side - 1], [0, side - 1]], np.float32)
        M = cv2.getPerspectiveTransform(q, dst)
        w = cv2.warpPerspective(gray, M, (side, side), flags=cv2.INTER_CUBIC)
        cell = side / 6.0
        means = np.zeros((6, 6), np.float32)
        lo, hi = int(round(cell * 0.25)), int(round(cell * 0.75))
        for i in range(6):
            for j in range(6):
                means[i, j] = w[int(i * cell) + lo:int(i * cell) + hi,
                                int(j * cell) + lo:int(j * cell) + hi].mean()
        border = np.concatenate([means[0, :], means[-1, :], means[1:-1, 0], means[1:-1, -1]])
        core = means[1:5, 1:5]
        thr = 0.5 * (float(np.median(border)) + float(np.median(core)))
        return (core > thr).astype(np.uint8), float(np.median(core) - np.median(border))

    def decode(self, gray: np.ndarray, quad: np.ndarray) -> DecodeResult:
        bits, contrast = self.sample(gray, quad)
        scores = []
        for mid, tpl in self.templates.items():
            best = 99
            for r in range(4):  # 四边形角点顺序可能任意 → 试 4 个旋转
                best = min(best, int(np.sum(np.rot90(bits, r) != tpl)))
            scores.append((best, mid))
        scores.sort()
        d1, m1 = scores[0]
        d2 = scores[1][0] if len(scores) > 1 else 99
        return DecodeResult(marker_id=m1, dist=d1, margin=d2 - d1, contrast=contrast)


def quad_plausible(quad: np.ndarray, min_side: float = 10.0,
                   max_edge_ratio: float = 2.2) -> bool:
    """形状闸门：最短边 ≥ min_side，且两组对边比都在 [1/max_edge_ratio, max_edge_ratio]。

    透视会把正方形压成梯形，但不会把对边比压到 2 倍以上（本场景实测 <1.8）。
    过小的候选（<10px）是墙面/杂物的典型产物，直接丢。
    """
    q = np.asarray(quad, np.float32).reshape(4, 2)
    e = [float(np.linalg.norm(q[(i + 1) % 4] - q[i])) for i in range(4)]
    if min(e) < min_side:
        return False
    r1, r2 = e[0] / max(e[2], 1e-6), e[1] / max(e[3], 1e-6)
    return (1.0 / max_edge_ratio) <= r1 <= max_edge_ratio and \
           (1.0 / max_edge_ratio) <= r2 <= max_edge_ratio
