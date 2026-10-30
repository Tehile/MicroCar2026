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

    # 候选四边形二次解码（见 redecode.py）：运动模糊会让 OpenCV 在位判决阶段
    # 丢弃真标记（进 rejectedImgPoints）或解成错误 ID。这里对这类四边形用
    # **白名单模板**重解码，把"1000 选 1"降为"5 选 1"。
    redecode: bool = True
    redecode_max_dist: int = 2  # 16 位里允许错几位
    redecode_min_margin: int = 3  # 与次优 ID 的最小距离差（防"两个 ID 都像"）
    redecode_min_contrast: float = 25.0  # 数据区与边框的灰度差下限（防对墙面对齐）
    redecode_min_side: float = 10.0  # 候选四边形最短边（px），过小直接丢

    # 目标缺失时的"场地平面重投影"兜底：用四角解出的 H 把图 warp 成正俯视，
    # 车顶标记在该视图里近似正对、尺寸固定，轮廓/位判决都更容易。
    # 实测量级：四角齐全帧里目标检出 6% → 12%（严格超集，没有一帧变差）；
    # 代价约 +0.47s/帧（S=4500）。
    rectified_target: bool = True
    rectified_side: int = 4500  # 重投影光栅边长（px），越大越清晰也越慢
    rectified_margin: float = 0.06  # 场地外扩比例（把四角标记包进来）

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
class GridConfig:
    """格点匹配通道（第二通道）：在场地正射图上直接匹配 0 号标记的 36 格图案。

    与 ArUco 通道的关系：ArUco 走"找轮廓 → 位判决"，运动模糊下第一段就死了；
    本通道不需要轮廓，只要图案的灰度结构还在。它需要四个角标记解出的单应 H。

    默认 ``enable=False``：不改变既有定位行为。开启后 ``ArucoLocator.locate``
    在 0 号标记缺失时会自动走本通道兜底（代价约 +2.8 s/帧）。
    也可以直接调用 ``ArucoLocator.locate_grid(frame)`` 单独取这一路的解。
    """

    enable: bool = False

    # —— 输入侧 ——
    # v=(gray−127.5)/127.5 是固定映射，**不要改成按帧拉伸**：
    # 平移对判据无害，而按帧拉伸会让 gap 的绝对值不可跨帧比较。
    chroma_weight: bool = True  # X = v·(1−S)；False 时只做 (−1,1) 归一化（消融基线）
    sat_floor: float = 60.0  # 饱和度分母下限，防暗区噪声被放大成"高饱和"

    # —— 采样几何 ——
    card_mm: float = 46.0  # 等效卡片边长；46/48/50 只影响置信度不影响位置（已实测）
    field_size_mm: float = 1440.0  # 场地边长（8 格 × 180mm）
    field_margin: float = 0.06  # 正射图外扩比例（把四角标记包进来）
    d_px: int = 20  # 全分辨率格距（整数像素）
    coarse_d_px: int = 10  # 粗搜格距（半分辨率，仍是整数像素）
    inset: float = 0.20  # 格均值内缩比例：只取每格中心 (1−2m)d 见方（抗模糊主手段）

    # —— 核 ——
    marker_id: int = 0
    # 外圈黑 / 内层黑 的权重比。κ=1 即按类归一化的原核。
    # 真实运动帧实测 κ 不可辨（0.5→3.0 只差 0.7%，方向略偏 κ<1）；
    # 合成高斯模糊给 κ≈1.5 是失真的（丢掉了"外圈贴卡外背景"与亚采样混叠）。
    kappa: float = 1.5

    # —— 搜索 ——
    coarse_beta_step: float = 3.0  # 粗搜 β 步长（度）
    coarse_phase: int = 2  # 粗搜亚相位数
    refine_phase: int = 4  # 细化亚相位数
    refine_beta_step: float = 1.0  # 细化 β 步长（度）
    beta_half: float = 10.0  # 细化时围绕种子 β 的搜索半宽（度）
    n_seed: int = 6  # 进入细化的种子数
    span: int = 1  # 细化允许的位置偏移（格）
    nms_mm: float = 90.0  # 粗搜峰的非极大抑制半径（mm）

    # —— 判据（100 帧标定：0 错收 / 83% 召回）——
    sharp_min: float = 0.20  # 双电平性下限 gap/max(白极差,黑极差)
    gap_min: float = 0.06  # 绝对对比度下限（X 域，对应灰度域的 20 级）

    # —— 角度换算 ——
    # θ = −β + 90·rot + theta_bias_deg。−1.02° 是实测零点偏置（网格-图案对齐残差）。
    theta_bias_deg: float = -1.02


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
    grid: GridConfig = field(default_factory=GridConfig)


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
