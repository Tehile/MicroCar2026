"""ArUco 场地定位包（Auto Locate）。

解算正方形场地中央 0 号标记的位置与方向：
    - 四角为 ID 1~4 的角标记（DICT_4X4_1000），朝向与 ID 排列顺序均不固定；
    - 场地坐标系由四个角标记的内顶点自动构建，场地恒为 [0, 1]²；
    - 主路径为无标定单应（不需要相机内参），可选棋盘格标定 + 畸变校正；
    - 角标记与目标标记不同高时，用差高视差修正还原真实位置。

最常用的三个入口：

    >>> from aruco_locator import create_locator, locate, parameter_catalog
    >>> loc = create_locator("config.yaml")          # 装配（推荐）
    >>> res = locate(img_bgr, profile="fast")        # 一次性解算一帧
    >>> res.to_dict()["x"]                            # 结构化结果

完整接口与参数说明见 docs/API.md。
"""

from .api import (
    ArucoLocator,
    Localization,
    create_locator,
    iter_locations,
    locate,
    locate_folder,
    locate_image,
    locate_video,
)
from .config import (
    CameraConfig,
    DetectionConfig,
    GeometryConfig,
    GridConfig,
    LocatorConfig,
    OutputConfig,
    ParallaxConfig,
    RefineConfig,
    config_to_dict,
    load_config,
)
from .corners import refine_quad_by_edges
from .detector import MarkerDetector, MarkerObservation
from .gridmatch import GridMatcher, marker_bits
from .parallax import (
    apply_parallax,
    camera_height_from_factor,
    correction_factor,
    factor_from_scales,
    nadir_from_intrinsics,
    pose_from_plane_homography,
)
from .parameters import (
    PROFILES,
    apply_params,
    apply_profile,
    catalog_markdown,
    describe_profiles,
    flatten_config,
    get_param,
    list_profiles,
    parameter_catalog,
    set_param,
)

__version__ = "1.2.0"

#: v1.0 对外承诺的稳定接口（其余名字视为内部实现，可能随版本变动）
#: 规则：**已有名字不得消失**；新增名字追加在末尾（v1.2 起）。
__all__ = [
    # —— 装配与调用 ——
    "ArucoLocator",
    "Localization",
    "create_locator",
    "locate",
    "locate_image",
    "locate_folder",
    "locate_video",
    "iter_locations",
    # —— 配置 ——
    "LocatorConfig",
    "DetectionConfig",
    "GeometryConfig",
    "RefineConfig",
    "OutputConfig",
    "CameraConfig",
    "ParallaxConfig",
    "load_config",
    "config_to_dict",
    # —— 可调参数 ——
    "parameter_catalog",
    "catalog_markdown",
    "get_param",
    "set_param",
    "apply_params",
    "apply_profile",
    "flatten_config",
    "list_profiles",
    "describe_profiles",
    "PROFILES",
    # —— 底层构件（进阶用）——
    "MarkerDetector",
    "MarkerObservation",
    "refine_quad_by_edges",
    "apply_parallax",
    "correction_factor",
    "camera_height_from_factor",
    "factor_from_scales",
    "nadir_from_intrinsics",
    "pose_from_plane_homography",
    # —— v1.2 格点匹配通道（第二通道）——
    "GridConfig",
    "GridMatcher",
    "marker_bits",
]
