"""帧间跟踪：在检测锚点之间补洞（相位相关 + 速度预测 + 响应门控）。

为什么用相位相关
----------------
运动模糊会把标记的 4×4 位图案抹平（模块仅 8mm），但**模糊不改变图像频谱的相位**——
标记的平移量在模糊帧里依然可测。这是"检测失败但仍能定位"的物理基础。

边界（务必如实标注）
--------------------
* 跟踪只给**平移**（位置），朝向沿用锚点帧；旋转/尺度变化会让模板失配；
* 逐帧累积会漂移（实测 4K 下约 1 px/帧 ≈ 0.4mm/帧），因此：
  - 每次检测成功即**重置**模板与位置（锚点）；
  - 连续跟踪超过 `max_gap` 帧、或相关响应低于 `min_response` 即判定丢失，停止输出；
* 输出必须打 `tracked` 标志，精度量级与检测帧不同（检测 ~0.4mm，跟踪 ~mm~cm）。
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class TrackState:
    center: np.ndarray      # (2,) 上一帧的图像坐标
    velocity: np.ndarray    # (2,) 最近一次位移（px/帧）
    age: int = 0            # 距最近一次检测锚点的帧数
    patch: np.ndarray | None = None  # 模板（灰度）

    def to_dict(self) -> dict:
        return {"center": [float(self.center[0]), float(self.center[1])],
                "velocity": [float(self.velocity[0]), float(self.velocity[1])],
                "age": int(self.age)}


class PatchTracker:
    """标记补丁的平移跟踪器。每次检测成功后 `anchor()`，检测失败时 `step()`。"""

    def __init__(self, max_gap: int = 30, min_response: float = 0.08,
                 patch_scale: float = 1.6) -> None:
        self.max_gap = int(max_gap)
        self.min_response = float(min_response)
        self.patch_scale = float(patch_scale)
        self.state: TrackState | None = None

    # ------------------------------------------------------------------ #
    def reset(self) -> None:
        self.state = None

    def anchor(self, gray: np.ndarray, corners: np.ndarray) -> None:
        """用一次成功检测重置跟踪状态（模板取标记外扩 patch_scale 倍）。"""
        c = np.asarray(corners, np.float32).reshape(4, 2)
        center = c.mean(axis=0)
        side = float(np.linalg.norm(c[1] - c[0]))
        half = max(8, int(side * self.patch_scale / 2))
        patch = self._crop(gray, center, half)
        if patch is None:
            self.state = None
            return
        vel = np.zeros(2, np.float32) if self.state is None else self.state.velocity
        self.state = TrackState(center=center.copy(), velocity=vel, age=0, patch=patch)

    def step(self, gray: np.ndarray) -> tuple[np.ndarray | None, float]:
        """推进一帧：先按速度预测，再用相位相关求残差位移。

        返回 (新中心 | None, 相关响应)。响应低于阈值或超出 max_gap 时返回 None。
        """
        s = self.state
        if s is None or s.patch is None:
            return None, 0.0
        if s.age >= self.max_gap:
            return None, 0.0
        pred = s.center + s.velocity
        half = s.patch.shape[0] // 2
        win = self._crop(gray, pred, half)
        if win is None or win.shape != s.patch.shape:
            self.reset()
            return None, 0.0
        (dx, dy), resp = cv2.phaseCorrelate(np.float32(s.patch), np.float32(win))
        new_center = pred + np.array([dx, dy], np.float32)
        if resp < self.min_response:
            self.state = None
            return None, float(resp)
        s.velocity = 0.5 * s.velocity + 0.5 * (new_center - s.center)
        s.center = new_center
        s.age += 1
        # 模板逐帧更新（缓慢跟随外观变化；漂移靠锚点重置来消除）
        new_patch = self._crop(gray, new_center, half)
        if new_patch is not None and new_patch.shape == s.patch.shape:
            s.patch = new_patch
        return new_center.copy(), float(resp)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _crop(gray: np.ndarray, center: np.ndarray, half: int) -> np.ndarray | None:
        h, w = gray.shape[:2]
        x, y = int(round(float(center[0]))), int(round(float(center[1])))
        if x - half < 0 or y - half < 0 or x + half >= w or y + half >= h:
            return None
        return gray[y - half:y + half, x - half:x + half].copy()
