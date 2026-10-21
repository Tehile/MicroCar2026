"""坐标系换算：固件格子 ⇄ 场地归一化 ⇄ 画布像素（设计方案 §2.3 / §7.4）。

三套坐标系：

    ① **固件迷宫格子** —— 8×8，整数量 (posX, posY)。原点方位与 y 轴方向**未定**
       （未决项 U1）。猜错不会报错，只会让整张图**左右/上下镜像**。
    ② **场地归一化 [0,1]²** —— Auto Locate 的契约（v1.x 冻结），原点在**左下角**，
       与四个角标记的内顶点构成的正方形绑定。
    ③ **画布像素** —— 浏览器 Canvas 的 y 轴**向下**，所以映射时必须翻一次 y。

所以从 ② 到 ③ 的那次翻转是**纯显示**问题（恒定），而 ① 到 ② 的翻转是**物理**问题
（必须实测）。把这两件事分开，是本模块存在的全部理由 —— 混在一起就再也说不清
"图反了"是显示错了还是标定错了。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, List, Tuple


@dataclass
class FwToArena:
    """固件格子 → 场地归一化的仿射约定（**现场实测**，不要猜）。"""

    y_flip: bool = True
    x_flip: bool = False
    #: 标记中心相对格心的偏移（mm）。默认 0：假设标记就贴在格心。
    marker_offset_mm: Tuple[float, float] = (0.0, 0.0)
    #: 视觉 θ（场地系，逆时针为正，单位度）与固件朝向的固定差
    theta_offset_deg: float = 0.0

    def to_dict(self) -> dict:
        return {"y_flip": self.y_flip, "x_flip": self.x_flip,
                "marker_offset_mm": list(self.marker_offset_mm),
                "theta_offset_deg": self.theta_offset_deg}


@dataclass
class Arena:
    """场地几何 + 两套坐标的换算。

    ⚠️ 改版二十四：原来的相机外参三项（`calibrated` / `nadir_xy` /
    `camera_height_mm`）**已删**。它们是"镜头在场地什么位置、离地多高"的标定结果，
    属于相机那一路；相机下线后没有任何东西能把它们写出来，留着就是一份
    "改了不会有反应"的假配置。视觉侧的标定现在归上游 Auto Locate 的配置。
    """

    grid: int = 8
    pitch_mm: float = 180.0
    field_mm: float = 1440.0
    fw_to_arena: FwToArena = field(default_factory=FwToArena)

    # ------------------------------------------------------------ 派生量
    @property
    def pitch(self) -> float:
        """单格边长（归一化）。"""
        return 1.0 / float(self.grid)

    def cell_center(self, cx: int, cy: int) -> Tuple[float, float]:
        """固件格号 → 该格中心的**归一化**坐标（已应用 flip）。"""
        g = float(self.grid)
        u = (float(cx) + 0.5) / g
        v = (float(cy) + 0.5) / g
        dx, dy = self.fw_to_arena.marker_offset_mm
        if self.field_mm > 0:
            u += dx / self.field_mm
            v += dy / self.field_mm
        if self.fw_to_arena.x_flip:
            u = 1.0 - u
        if self.fw_to_arena.y_flip:
            v = 1.0 - v
        return u, v

    def fw_to_norm(self, px: int, py: int) -> Tuple[float, float]:
        """同 `cell_center`（语义别名：这里 (px,py) 是格号，不是连续位置）。"""
        return self.cell_center(px, py)

    def norm_to_fw(self, x: float, y: float) -> Tuple[float, float]:
        """归一化 → 连续格坐标（浮点，方便与固件整数量比残差）。"""
        u, v = float(x), float(y)
        if self.fw_to_arena.x_flip:
            u = 1.0 - u
        if self.fw_to_arena.y_flip:
            v = 1.0 - v
        dx, dy = self.fw_to_arena.marker_offset_mm
        if self.field_mm > 0:
            u -= dx / self.field_mm
            v -= dy / self.field_mm
        return u * self.grid - 0.5, v * self.grid - 0.5

    # ------------------------------------------------------------ 画布
    @staticmethod
    def norm_to_px(x: float, y: float, w: float, h: float) -> Tuple[float, float]:
        """归一化 → 画布像素。**唯一的一次 y 翻转**（Canvas y 向下）。"""
        return float(x) * w, (1.0 - float(y)) * h

    @staticmethod
    def px_to_norm(px: float, py: float, w: float, h: float) -> Tuple[float, float]:
        return float(px) / w, 1.0 - float(py) / h

    def grid_lines(self) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
        """画 8×8 网格用的归一化线段（含外框）。"""
        g = float(self.grid)
        segs: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
        for i in range(self.grid + 1):
            t = i / g
            segs.append(((t, 0.0), (t, 1.0)))
            segs.append(((0.0, t), (1.0, t)))
        return segs

    # ------------------------------------------------------------ 一致性
    def residual_cells(self, fw_xy: Tuple[int, int], vis_xy: Tuple[float, float]
                       ) -> Tuple[float, float, float]:
        """固件格号 vs 视觉归一化位置 → 连续格坐标下的残差（单位：格）。

        残差 < 0.3 格说明当前 flip 配置正确；`y_flip` 反了时残差会是
        **整场量级**（≈ grid − something），一眼可辨。
        """
        fx, fy = self.norm_to_fw(*vis_xy)
        dx = fx - float(fw_xy[0])
        dy = fy - float(fw_xy[1])
        return dx, dy, math.hypot(dx, dy)

    def to_dict(self) -> dict:
        return {
            "grid": self.grid, "pitch_mm": self.pitch_mm, "field_mm": self.field_mm,
            "fw_to_arena": self.fw_to_arena.to_dict(),
        }


def from_config(cfg: Any) -> Arena:
    """从 Control 的 config 取 arena 段。

    `fw_to_arena` 现在**没有配置来路**（改版二十四删了 `arena.fw_to_arena`）：
    它只在"把固件格号映到视觉归一化坐标"时才需要，而那条对照（`/api/v1/arena` 的 Δ）
    随相机一起下线。所以这里用默认值，等真要做这件事时再实测后加回来。
    """
    return Arena(
        grid=int(cfg.get("arena.grid", 8)),
        pitch_mm=float(cfg.get("arena.pitch_mm", 180.0)),
        field_mm=float(cfg.get("arena.field_mm", 1440.0)),
    )


