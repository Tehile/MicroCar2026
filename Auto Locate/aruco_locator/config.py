"""配置层：所有阈值、字典、相机参数走 YAML，不硬编码。

结构见项目根目录 config.yaml；缺项一律回落到这里的默认值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any

import yaml


# --------------------------------------------------------------------------- #
# 配置数据类
# --------------------------------------------------------------------------- #
@dataclass
class DetectionConfig:
    """标记检测参数。"""

    dict_name: str = "DICT_4X4_1000"
    corner_ids: list[int] = field(default_factory=lambda: [1, 2, 3, 4])
    target_id: int = 0

    adaptive_thresh_win_size_min: int = 3
    adaptive_thresh_win_size_max: int = 53
    adaptive_thresh_win_size_step: int = 6
    adaptive_thresh_constant: float = 7.0
    min_marker_perimeter_rate: float = 0.01
    max_marker_perimeter_rate: float = 4.0
    polygonal_approx_accuracy_rate: float = 0.03
    min_distance_to_border: int = 1
    error_correction_rate: float = 0.6

    # 透视矫正后的逐格位采样。默认(4, 0.13)在小且斜、有模糊的标记上会解码失败：
    # 实拍验证 8 / 0.25 能找回此类标记，且不引入误检（2026-09-25 迷宫实拍）。
    perspective_remove_pixel_per_cell: int = 8
    perspective_remove_ignored_margin_per_cell: float = 0.25

    corner_refinement: str = "subpix"  # none | contour | subpix | apriltag
    corner_refinement_win: int = 5
    corner_refinement_max_iter: int = 30
    corner_refinement_min_accuracy: float = 0.01

    # 检测完成后再对全部角点做一次全局亚像素精化
    subpixel_polish: bool = True  # edge_refine=False 时生效（cv2 的 cornerSubPix）
    subpixel_win: int = 5
    subpixel_max_iter: int = 30
    subpixel_min_accuracy: float = 0.01

    # 边缘直线拟合亚像素精化（见 corners.py）。精度主要来自这一步：
    # 它比 cv2 的 CORNER_REFINE_SUBPIX 准，且能消掉后者约 1.6% 的"标记整体缩小"系统偏置。
    edge_refine: bool = True
    edge_search_px: float = 2.5  # 沿外法线的初始搜索半宽（像素）
    edge_step_px: float = 0.25  # 灰度剖面采样步长（像素）
    edge_samples: int = 0  # 每条边采样点数，0 = 按边长自动
    edge_trim: float = 0.15  # 每条边两端各裁掉的比例（避开圆角）
    edge_iterations: int = 2
    edge_min_points: int = 5  # 每条边至少需要的边缘点数
    edge_clip_sigma: float = 2.5  # 直线拟合的 sigma 截断倍数
    edge_bright_outside: bool = True  # 标记比背景暗（黑标记贴浅色地面）时保持 True
    edge_max_shift_ratio: float = 0.35  # 角点位移上限（相对边长），超过则放弃精化

    # 多尺度检测：标记在画面里过小时，上采样能显著提高检出率
    scales: list[float] = field(default_factory=lambda: [1.0])

    clahe: bool = False
    clahe_clip_limit: float = 2.0
    clahe_grid: int = 8


@dataclass
class GeometryConfig:
    """几何解算与有效性判定。"""

    # 内顶点选择（详见 geometry.select_inner_vertices）：
    #   xdiag_plane  【默认】对角线法 + 平面空间比角度 —— 无超参、无搜索；
    #                精度与旧默认 auto 持平（实拍逐位一致），强透视下更好，
    #                内顶点那一步快约 14 倍，且没有"调小就静默出错"的悬崖型超参。
    #   xdiag_unit   同上，但在图像空间比角度（省一次局部仿射拟合，精度略逊）
    #   xdiag        对角线法 + 图像空间原始点乘（不推荐：受投影各向异性偏袒）
    #   nearest      只用"离场地中心最近"
    #   auto/search  形状残差 + 距离余量的加权代价（需 vertex_margin_weight >= 0.05）
    #   full         穷举 4^4 组合（最慢）
    vertex_mode: str = "xdiag_plane"  # xdiag_plane | xdiag_unit | xdiag | nearest | auto | search | full
    max_vertex_search: int = 16  # 组合数上限（2 近邻 → 2^4 = 16；0 = 不限）
    vertex_margin_weight: float = 0.1  # 仅 auto/search/full 用；必须 >= 0.05，见 geometry._assignment_cost
    # 对角线法安全闸：两条对角线的相交角正弦下限。正常机位下近乎正交（实测 ≥0.98），
    # 仅"四点近共线"（相机贴地掠过）会掉到 0.04；低于此值判退化并回退 nearest。
    min_diag_sin: float = 0.15

    # 质量门限
    max_square_rms: float = 0.02  # 角标记四边形映射成正方形的残差上限（场地单位）
    min_origin_margin: float = 0.05  # 原点判定余量（除以边长）
    min_axis_margin: float = 0.05  # 轴向判定余量
    max_inner_margin_warn: float = 0.5  # 内顶点判定余量低于该值仅告警
    min_vertex_cost_gap_rel: float = 0.1  # 仅 auto/search/full 用；代价差低于该值 → 告警

    # 角标记缺失是否直接判无效（按约定：True）
    require_all_corners: bool = True


@dataclass
class RefineConfig:
    """联合最小二乘精化（把 16 个角点一起用上）。"""

    enable: bool = True
    max_nfev: int = 100


@dataclass
class OutputConfig:
    """时序滤波。默认关闭。"""

    smooth: str = "none"  # none | ema | moving_average
    ema_alpha: float = 0.4
    window: int = 5


@dataclass
class ParallaxConfig:
    """高度视差修正（0 号标记与角标记不在同一高度时）。

    四个角标记定义参考平面（高度 corner_height_mm），单应只对该平面精确。
    0 号标记离地 target_height_mm，两者不同高时它的位置会沿"天底点方向"偏
    (k-1)·|x - N|，k = (Hc - h_target)/(Hc - h_corner)。详见 parallax.py。

    必须标定的量：camera_height_mm（卷尺量）与天底点（nadir_xy 或相机内参）。
    两者都没有时不做修正，只写 diagnostics.parallax 告警。
    """

    enable: bool = True
    corner_height_mm: float = 50.0  # 参考平面（角标记）离地高度
    target_height_mm: float = 23.0  # 0 号标记离地高度
    camera_height_mm: float | None = None  # 镜头光心离地高度（卷尺实测）
    nadir_xy: list[float] | None = None  # 相机正下方的场地位置；相机居中时填 [0.5, 0.5]
    from_intrinsics: bool = True  # nadir_xy 为 null 且有内参时，用单应分解自动求


@dataclass
class CameraConfig:
    """相机标定与几何修正。"""

    undistort: bool = False
    intrinsic_file: str | None = None
    field_size_mm: float | None = None  # 场地实际边长，仅用于把修正量换算成 mm 写诊断
    parallax: ParallaxConfig = field(default_factory=ParallaxConfig)


@dataclass
class LocatorConfig:
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    geometry: GeometryConfig = field(default_factory=GeometryConfig)
    refine: RefineConfig = field(default_factory=RefineConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)


# --------------------------------------------------------------------------- #
# YAML 载入
# --------------------------------------------------------------------------- #
def _apply(dc: Any, data: dict) -> Any:
    """把 YAML 字典递归填进数据类实例，未知键直接报错（避免拼错参数被静默忽略）。"""
    if not isinstance(data, dict):
        return dc
    known = {f.name: f for f in fields(dc)}
    for key, value in data.items():
        if key not in known:
            raise ValueError(f"配置项 {type(dc).__name__}.{key} 不存在")
        cur = getattr(dc, key)
        if is_dataclass(cur) and isinstance(value, dict):
            setattr(dc, key, _apply(cur, value))
        else:
            setattr(dc, key, value)
    return dc


def load_config(path: str | os.PathLike | None = None) -> LocatorConfig:
    """读取 YAML 配置；path 为 None 时返回全默认配置。"""
    cfg = LocatorConfig()
    if path is None:
        return cfg
    path = os.fspath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"配置文件不存在：{path}")
    with open(path, "r", encoding="utf-8") as fp:
        data = yaml.safe_load(fp) or {}
    return _apply(cfg, data)


def config_to_dict(cfg: LocatorConfig) -> dict:
    """转成可序列化字典（用于随结果落盘）。"""
    from dataclasses import asdict

    return asdict(cfg)
