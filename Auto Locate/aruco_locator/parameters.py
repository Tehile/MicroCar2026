"""可调参数注册表：目录（introspection）、点号路径读写、类型与范围校验、预设档位。

设计意图
--------
对外调用方不应该去改数据类属性、也不应该猜参数名。这个模块把全部可调参数做成
**可枚举的目录**（名字/类型/默认值/建议范围/影响），并提供

    get_param(cfg, "detection.edge_search_px")
    set_param(cfg, "geometry.vertex_mode", "xdiag_plane")
    apply_params(cfg, {"detection.edge_refine": False})
    parameter_catalog()          # 参数手册（机器可读）

目录由配置数据类**自动派生**，人工只补一份元数据（单位/范围/说明），
因此不会出现"文档里有、代码里没有"的漂移；元数据里写错的参数名会在导入时直接报错。
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import Any

from .config import LocatorConfig

# --------------------------------------------------------------------------- #
# 元数据：点号路径 -> {unit, range, desc}
# 只写"需要解释"的项；没写的参数仍会出现在目录里（desc 为空）。
# --------------------------------------------------------------------------- #
_META: dict[str, dict] = {
    # ---------------- detection ----------------
    "detection.dict_name": {
        "desc": "ArUco 字典名。必须与印刷的标记一致，否则全部标记都解不出来。",
        "tune_when": "换了一批印刷标记时",
    },
    "detection.corner_ids": {"desc": "四个角标记的 ID（顺序不重要，程序自动判环序与原点）。"},
    "detection.target_id": {"desc": "被定位的目标标记 ID。"},
    "detection.adaptive_thresh_win_size_min": {
        "unit": "px",
        "desc": "自适应阈值窗口最小边长（必须 > 1 且为奇数下限）。",
        "tune_when": "标记很大或很小时",
    },
    "detection.adaptive_thresh_win_size_max": {
        "unit": "px",
        "desc": "自适应阈值窗口最大边长。标记越小、光照越不均，需要越大。",
        "tune_when": "大标记在近处漏检 → 调大",
    },
    "detection.adaptive_thresh_win_size_step": {"unit": "px", "desc": "阈值窗口的扫描步长。"},
    "detection.adaptive_thresh_constant": {
        "desc": "自适应阈值的常数偏移（越大越保守，暗背景下可减小）。",
        "tune_when": "整幅偏暗/偏亮导致漏检",
    },
    "detection.min_marker_perimeter_rate": {
        "desc": "标记最小周长 / 画面短边。标记很小（<40px）时需要调小（须小于 "
                "max_marker_perimeter_rate）。",
        "range": "0.001 ~ 4.0",
        "tune_when": "远处的小标记漏检 → 调小到 0.005",
    },
    "detection.max_marker_perimeter_rate": {
        "desc": "标记最大周长 / 画面短边。标记贴近镜头时需调大。",
        "range": "0.01 ~ 6.0",
        "tune_when": "近处的大标记漏检 → 调大到 6.0",
    },
    "detection.polygonal_approx_accuracy_rate": {
        "desc": "四边形逼近精度（相对周长）。越小越严格，太小会提不出候选。",
        "range": "0.01 ~ 0.1",
        "tune_when": "边缘发虚的标记提不出四边形 → 调大到 0.05",
    },
    "detection.min_distance_to_border": {"unit": "cell", "desc": "标记距画面边缘的最小距离（格）。"},
    "detection.error_correction_rate": {
        "desc": "位纠错率。调大会提高检出率但增加误检（重复纹理场地慎调）。",
        "range": "0.0 ~ 1.0",
        "tune_when": "误检多 → 调小；解码失败多 → 调大",
    },
    "detection.perspective_remove_pixel_per_cell": {
        "unit": "px/cell",
        "desc": "解码前把标记透视矫正到每格多少像素来采样。默认 4 太粗：小、斜、模糊的"
                "标记会解码失败。实拍验证 8 可稳定找回（零误检）。",
        "range": "4 ~ 16",
        "tune_when": "标记已提出四边形但解码失败 → 调到 8 或 16",
    },
    "detection.perspective_remove_ignored_margin_per_cell": {
        "desc": "每格采样时忽略的边缘比例，越大越只取格子中心（抗模糊/抗邻近干扰）。",
        "range": "0.0 ~ 0.45",
        "tune_when": "同上，0.13 → 0.25 是常用提升",
    },
    "detection.redecode": {
        "desc": "候选四边形二次解码：对 OpenCV 丢弃的四边形（rejectedImgPoints）以及"
                "被解成错误 ID 的检出，用白名单模板重新解码。运动模糊下能把"
                "\"四边形找到了但 4×4 位判错\"的标记找回来。",
        "tune_when": "需要完全回到旧行为时置 false",
    },
    "detection.redecode_max_dist": {
        "unit": "bit",
        "desc": "重解码接受的最大 Hamming 距离（16 位数据位）。放宽会提高召回、增加误接受；"
                "误接受由 geometry.max_square_rms 的方正度检查兜底。",
        "range": "0 ~ 5",
        "tune_when": "重解码后仍有漏检 → 2→3；出现错位帧 → 收紧到 1",
    },
    "detection.redecode_min_margin": {
        "unit": "bit",
        "desc": "最优 ID 与次优 ID 的距离差下限。防止\"两个 ID 都像\"时乱认。",
        "range": "1 ~ 8",
        "tune_when": "同一标记在相邻帧跳到不同 ID 时调大",
    },
    "detection.redecode_min_contrast": {
        "unit": "灰度级",
        "desc": "重解码时数据区与边框的灰度差下限。过低说明这个四边形不是标记"
                "（墙面、阴影），直接拒绝。",
        "range": "5 ~ 80",
        "tune_when": "暗光/低对比场景下调小，误检多时调大",
    },
    "detection.redecode_min_side": {
        "unit": "px",
        "desc": "参与重解码的四边形最短边。小于它的多是杂物/墙角碎片。",
        "range": "6 ~ 30",
        "tune_when": "远端标记特别小时调小（注意误检）",
    },
    "detection.rectified_target": {
        "desc": "目标标记缺失时，用四角解出的 H 把图 warp 成场地平面正俯视，在该视图里再找一次目标。"
                "车顶标记在正俯视下近似正对、尺寸固定，轮廓与位判决更容易。"
                "实测目标检出 8%→12%、有效定位 4%→7%，代价约 +0.3~0.5s/帧。",
        "tune_when": "追求速度且能接受较低有效帧率时置 false",
    },
    "detection.rectified_side": {
        "unit": "px",
        "desc": "重投影光栅边长。越大越清晰也越慢；4500 时场地内约 1.9 px/mm（48mm 标记 ≈ 134px）。",
        "range": "2000 ~ 8000",
        "tune_when": "标记更小 → 加大；速度优先 → 减小（先用特写确认可读）",
    },
    "detection.rectified_margin": {
        "desc": "重投影时场地外扩比例，用来把四角标记完整包进光栅。",
        "range": "0.02 ~ 0.15",
        "tune_when": "四角标记被裁掉时调大",
    },
    "detection.corner_refinement": {"desc": "cv2 自带的角点精化方式：none | contour | subpix | apriltag。"},
    "detection.corner_refinement_win": {"unit": "px", "desc": "cv2 角点精化窗口半宽。"},
    "detection.corner_refinement_max_iter": {"desc": "cv2 角点精化迭代上限。"},
    "detection.corner_refinement_min_accuracy": {"desc": "cv2 角点精化收敛阈值。"},
    "detection.subpixel_polish": {
        "desc": "检测后对全部角点做一次全局 cornerSubPix 精化。仅在 edge_refine=False 时生效。",
    },
    "detection.subpixel_win": {"unit": "px", "desc": "cornerSubPix 窗口半宽。"},
    "detection.subpixel_max_iter": {"desc": "cornerSubPix 迭代上限。"},
    "detection.subpixel_min_accuracy": {"desc": "cornerSubPix 收敛阈值。"},
    "detection.edge_refine": {
        "desc": "★ 精度核心：边缘直线拟合亚像素精化。关闭后回落到 cv2 cornerSubPix"
                "（位置会引入约 1.6% 的'标记整体缩小'系统偏置，但角度略稳）。",
        "tune_when": "只要位置精度就保持 True；若只关心方向可试 False",
    },
    "detection.edge_search_px": {
        "unit": "px",
        "desc": "沿边缘外法线的初始搜索半宽。要大于角点本身的误差量级。",
        "range": "0.5 ~ 6.0",
        "tune_when": "角点误差大（标记模糊）→ 调到 4.0",
    },
    "detection.edge_step_px": {
        "unit": "px",
        "desc": "灰度剖面采样步长（决定剖面点数：2.5px 半宽下 0.25→21 点、0.5→11 点）。"
                "实测对**耗时几乎无影响**（耗时主要在检测与 ROI 重采样），对结果影响也在"
                "0.1mm 以内，保持默认即可。",
        "range": "0.05 ~ 1.0",
    },
    "detection.edge_samples": {"desc": "每条边的采样点数；0 = 按边长自动。"},
    "detection.edge_trim": {
        "desc": "每条边两端各裁掉的比例，用来避开圆角/溢出。",
        "range": "0.0 ~ 0.4",
        "tune_when": "标记边角有溢出/圆角 → 调大到 0.2",
    },
    "detection.edge_iterations": {"desc": "边缘精化迭代次数，2 次足够。", "range": "1 ~ 4"},
    "detection.edge_min_points": {"desc": "每条边至少需要的有效边缘点数，不足则放弃该边。"},
    "detection.edge_clip_sigma": {
        "desc": "直线拟合的 sigma 截断倍数（抗反光/污点）。",
        "range": "1.0 ~ 5.0",
    },
    "detection.edge_bright_outside": {
        "desc": "标记比背景暗（黑标记贴浅色地面）时为 True；白底黑框反相时改 False。",
        "tune_when": "精化后角点反而变差 → 大概率是这项反了",
    },
    "detection.edge_max_shift_ratio": {
        "desc": "精化后角点位移上限（相对边长），超限则放弃本次精化、保留原角点。",
        "range": "0.1 ~ 0.5",
    },
    "detection.scales": {
        "desc": "多尺度检测的缩放因子列表。标记在画面里很小时加上 2.0 可显著提高检出率"
                "（代价是耗时约×2）。",
        "tune_when": "标记 < 40px 且漏检 → [1.0, 2.0]",
    },
    "detection.clahe": {"desc": "CLAHE 局部直方图均衡，用于光照不均（顶灯偏一侧）。"},
    "detection.clahe_clip_limit": {"desc": "CLAHE 对比度限制。"},
    "detection.clahe_grid": {"desc": "CLAHE 分块数。"},
    # ---------------- geometry ----------------
    "geometry.vertex_mode": {
        "desc": "角标记内顶点选择策略。"
                "xdiag_plane（默认）= 对角线法 + 平面空间点乘，无超参、无搜索，"
                "精度与 auto 持平、强透视下更好、内顶点那一步快约 14 倍；"
                "xdiag_unit = 同上但图像空间点乘；xdiag = 图像空间原始点乘（不推荐）；"
                "nearest = 只用离场地中心最近；auto/search = 形状+余量加权代价；full = 4^4 穷举。",
        "tune_when": "默认 xdiag_plane 已无超参；需要旧行为时用档位 costfn（等价 vertex_mode=auto）",
    },
    "geometry.min_diag_sin": {
        "desc": "对角线法安全闸：四条标记中心的两条对角线相交角正弦下限（默认档 xdiag_plane 生效）。"
                "正常机位下近乎正交（实拍最低 0.807），仅四点近共线会掉到 0.04；"
                "低于此值判退化并回退 nearest，同时打 inner_vertex_degenerate 标志。",
        "range": "0.0 ~ 1.0",
        "tune_when": "若出现 diagonal_pairing_degenerate 频繁，可调低",
    },
    "geometry.max_vertex_search": {
        "desc": "候选组合数上限（仅 auto/search 用；2 近邻 → 2^4=16）；0 表示不限。",
        "tune_when": "标记质量差、2 近邻都没命中真值时调大",
    },
    "geometry.vertex_margin_weight": {
        "desc": "内顶点代价里'距离余量'项权重。**仅对 auto/search/full 生效**（默认档用 xdiag_plane，不读此参数）。"
                "须 >= 0.05：形状残差项在 4 点单应下近乎退化（组合间只差 ~1e-4），"
                "权重过小会让它盖过距离余量、选中错误的内顶点（实测偏 10~45mm）。",
        "range": "0.05 ~ 0.5",
        "tune_when": "改用 auto/search 档位时按需调整（0.05 是实测安全下界，0.1 为默认）",
    },
    "geometry.max_square_rms": {
        "desc": "角标记映射回正方形的残差上限（场地单位）；超限该帧判无效。"
                "0.02 ≈ 场地边长的 2%。",
    },
    "geometry.min_origin_margin": {
        "desc": "原点判定余量；低于该值打 ambiguous_origin（提示原点可能选错）。",
    },
    "geometry.min_axis_margin": {
        "desc": "轴向判定余量；低于该值打 ambiguous_axis。相机绕光轴接近 ±45° 时必然很小，"
                "属固有几何退化。",
    },
    "geometry.max_inner_margin_warn": {"desc": "内顶点余量低于该值仅告警，不影响有效性。"},
    "geometry.min_vertex_cost_gap_rel": {
        "desc": "最优/次优内顶点组合的代价相对差距低于该值 → 打 inner_vertex_uncertain。"
                "**仅对 auto/search/full 生效**（xdiag* 模式无代价函数，不产生此标志）。",
    },
    "geometry.require_all_corners": {
        "desc": "缺任一角标记即判该帧无效（按约定 True，不做补齐兜底）。",
    },
    # ---------------- refine ----------------
    "refine.enable": {
        "desc": "把 4 个角标记的 16 个角点一起做联合最小二乘精化（同时解 H 与各标记的旋转/边长）。",
        "tune_when": "要省时间可关掉，实测精度收益有限",
    },
    "refine.max_nfev": {"desc": "联合精化的最大函数求值次数。"},
    # ---------------- output ----------------
    "output.smooth": {
        "desc": "时序滤波：none | ema | moving_average。默认关闭。",
        "tune_when": "视频抖动大 → ema（静止目标用 moving_average）",
    },
    "output.ema_alpha": {"desc": "EMA 新值权重（越大越跟手、越小越平滑）。", "range": "0.05 ~ 1.0"},
    "output.window": {"desc": "滑动平均窗口帧数。"},
    # ---------------- camera ----------------
    "camera.undistort": {"desc": "是否用标定内参做畸变校正（需要 intrinsic_file）。"},
    "camera.intrinsic_file": {
        "desc": "相机内参文件（calibrate_camera.py 输出，.yaml 或 .npz）。",
        "tune_when": "广角镜头边缘弯曲明显，或要用内参自动求天底点时",
    },
    "camera.field_size_mm": {
        "desc": "场地实际边长（mm）。**只**用于把视差修正量换算成 mm 写进诊断，"
                "不影响归一化结果，填错也无害。",
    },
    "camera.parallax.enable": {"desc": "是否启用差高视差修正（目标与角标记不同高度时必须开）。"},
    "camera.parallax.corner_height_mm": {"unit": "mm", "desc": "参考平面（角标记）离地高度。"},
    "camera.parallax.target_height_mm": {"unit": "mm", "desc": "目标标记（0 号）离地高度。"},
    "camera.parallax.camera_height_mm": {
        "unit": "mm",
        "desc": "★ 镜头光心离地高度。允许 ±20% 误差。为 None 时不修正，只打 "
                "parallax_uncalibrated 告警。",
    },
    "camera.parallax.nadir_xy": {
        "desc": "★ 相机正下方那块场地的归一化坐标（天底点）。修正量最关键的一项，"
                "可直接实测：镜头吊铅垂线，落点处放标记，管线输出的 (x,y) 即为天底点。"
                "为 None 且有内参时自动用单应分解求。",
    },
    "camera.parallax.from_intrinsics": {
        "desc": "nadir_xy 为 None 且有内参时，是否允许用单应分解自动求天底点。",
    },
}

# --------------------------------------------------------------------------- #
# 预设档位
#
# 耗时数据（4080×2296 实拍、交错测量中位值，相对默认档）：
#   默认            1.00x      顶点 full 穷举   1.21x
#   关 edge 精化     0.92x      多尺度 [1,2]     2.66x   ← 真正的耗时大头
#   关联合精化       1.05x      （即几乎不花钱）
# 结论：**分辨率与多尺度才是性能杠杆**；其余参数对速度影响都在噪声量级，
# 因此本模块不提供"靠调参数就能快很多"的档位，避免给出误导性的承诺。
# 注：内顶点那一步从加权代价（auto）换成对角线法（xdiag_plane）可快约 14 倍
#     （6.878ms → 0.479ms），但那一步只占总耗时的不到 2%，对整帧影响在噪声内。
# --------------------------------------------------------------------------- #
PROFILES: dict[str, dict] = {
    "default": {},
    "fast": {
        # 实测只省约 15%，代价是位置精度（cv2 亚像素有约 1.6% 的"标记整体缩小"偏置）
        "detection.edge_refine": False,
        "refine.enable": False,
    },
    "xdiag": {
        # 兼容旧档位名：v1.1.0 起 xdiag_plane 已是默认值，此档位即"显式声明默认"
        "geometry.vertex_mode": "xdiag_plane",
    },
    "costfn": {
        # 退回 v1.0.x 的加权代价判据（auto）+ 安全权重；仅用于对照/回归
        "geometry.vertex_mode": "auto",
        "geometry.vertex_margin_weight": 0.1,
    },
    "accurate": {
        # 精度优先：内顶点 4^4 穷举 + 收紧形状门限
        "geometry.vertex_mode": "full",
        "geometry.max_square_rms": 0.015,
        "detection.edge_search_px": 3.5,
    },
    "robust": {
        # 鲁棒优先：多尺度 + 大搜索窗，能救远处的/模糊的标记，代价约 2.7x 耗时
        "refine.enable": True,
        "geometry.vertex_mode": "full",
        "detection.scales": [1.0, 2.0],
        "detection.edge_search_px": 4.0,
    },
    "small_markers": {
        "detection.scales": [1.0, 2.0],
        "detection.min_marker_perimeter_rate": 0.005,
        "detection.edge_search_px": 4.0,
    },
    "oblique_small": {
        # 小、斜、模糊的标记：加粗位采样、放宽四边形逼近、加大精化搜索
        "detection.perspective_remove_pixel_per_cell": 16,
        "detection.perspective_remove_ignored_margin_per_cell": 0.35,
        "detection.polygonal_approx_accuracy_rate": 0.05,
        "detection.edge_search_px": 4.0,
        "detection.edge_max_shift_ratio": 0.5,
    },
    "video": {
        "output.smooth": "ema",
        "output.ema_alpha": 0.4,
    },
    "no_parallax": {"camera.parallax.enable": False},
}

#: 档位说明（用于文档生成与 :func:`describe_profiles`）
PROFILE_DESC: dict[str, str] = {
    "default": "内置默认值（推荐起点）。内顶点用对角线法（xdiag_plane）。",
    "fast": "关掉边缘精化与联合精化，实测约省 15%；位置精度会降。",
    "xdiag": "显式声明对角线法选内顶点（= 默认值，v1.1.0 起为默认）。",
    "costfn": "退回 v1.0.x 的加权代价判据（vertex_mode=auto）；仅用于对照/回归。",
    "accurate": "精度优先：内顶点 4^4 穷举 + 收紧方正度门限。",
    "robust": "鲁棒优先：多尺度 + 大搜索窗，约 2.7x 耗时。",
    "small_markers": "标记在画面里很小（<40px）时提高检出率。",
    "oblique_small": "标记小而斜、边缘发虚时的位采样/逼近参数组合。",
    "video": "视频输入：打开 EMA 时序滤波。",
    "no_parallax": "关闭差高视差修正（目标与角标记共面时用）。",
}


def list_profiles() -> list[str]:
    """可用预设档位名。"""
    return sorted(PROFILES)


def describe_profiles() -> list[dict]:
    """档位清单：[{name, desc, params}, ...]。"""
    return [
        {"name": n, "desc": PROFILE_DESC.get(n, ""), "params": PROFILES[n]}
        for n in list_profiles()
    ]


# --------------------------------------------------------------------------- #
# 目录（introspection）
# --------------------------------------------------------------------------- #
def _leaf_names(cfg: LocatorConfig = None) -> list[tuple[str, Any, Any, str]]:
    """遍历配置，产出 (点号路径, 当前值, 数据类实例, 字段名)。"""
    cfg = cfg or LocatorConfig()
    out: list[tuple[str, Any, Any, str]] = []

    def walk(obj: Any, prefix: str):
        for f in fields(obj):
            val = getattr(obj, f.name)
            path = f"{prefix}.{f.name}" if prefix else f.name
            if is_dataclass(val):
                walk(val, path)
            else:
                out.append((path, val, obj, f.name))

    walk(cfg, "")
    return out


def _declared_type(owner: Any, name: str) -> str:
    """取字段声明的类型名（本项目用了 `from __future__ import annotations`，是字符串）。"""
    for f in fields(owner):
        if f.name == name:
            return str(f.type)
    return ""


def coerce(value: Any, declared: str) -> Any:
    """按字段声明类型做宽松转换；无法转换时抛 ValueError。"""
    if value is None:
        return None
    d = declared.replace(" ", "")
    try:
        if "bool" in d:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on", "y")
            return bool(value)
        if "list" in d:
            if isinstance(value, str):
                value = [t for t in value.replace(";", ",").split(",") if t.strip()]
            if not isinstance(value, (list, tuple)):
                raise TypeError(f"期望列表，得到 {type(value).__name__}")
            elem_float = "float" in d
            return [float(v) if elem_float else int(v) for v in value]
        if "float" in d:
            return float(value)
        if "int" in d:
            return int(value)
        if "str" in d:
            return str(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"参数类型不匹配：期望 {declared}，得到 {value!r}（{exc}）") from None
    return value


def _check_range(path: str, value: Any) -> None:
    meta = _META.get(path) or {}
    rng = meta.get("range")
    if not rng or not isinstance(value, (int, float)) or isinstance(value, bool):
        return
    try:
        lo_s, hi_s = rng.split("~")
        lo, hi = float(lo_s.strip()), float(hi_s.strip())
    except ValueError:  # 元数据写成符号化范围时不做数值校验
        return
    if not (lo <= float(value) <= hi):
        raise ValueError(f"参数 {path} = {value} 超出建议范围 [{lo}, {hi}]")


def parameter_catalog(path: str | None = None, group: str | None = None) -> list[dict]:
    """可调参数目录（机器可读）。

    path  只取某一项（或某一子树，如 "detection.edge"）
    group 只取某个顶层分组：detection / geometry / refine / output / camera
    返回 [{name, default, type, group, unit, range, desc, tune_when}, ...]
    """
    rows = []
    for p, val, owner, fname in _leaf_names():
        if path and not (p == path or p.startswith(path + ".")):
            continue
        if group and not p.startswith(group + "."):
            continue
        meta = _META.get(p) or {}
        rows.append(
            {
                "name": p,
                "default": val,
                "type": _declared_type(owner, fname),
                "group": p.split(".", 1)[0],
                "unit": meta.get("unit", ""),
                "range": meta.get("range", ""),
                "desc": meta.get("desc", ""),
                "tune_when": meta.get("tune_when", ""),
            }
        )
    return rows


def catalog_markdown(group: str | None = None) -> str:
    """把目录渲染成 Markdown 表（供文档生成/自检比对）。"""
    rows = parameter_catalog(group=group)
    lines = ["| 参数 | 类型 | 默认 | 单位 | 建议范围 | 说明 |", "|---|---|---|---|---|---|"]
    for r in rows:
        d = r["default"]
        if isinstance(d, list):
            d = "[" + ", ".join(str(x) for x in d) + "]"
        desc = r["desc"].replace("\n", " ")
        if r["tune_when"]:
            desc += f" **何时调**：{r['tune_when']}"
        lines.append(
            f"| `{r['name']}` | `{r['type']}` | `{d}` | {r['unit'] or '—'} | "
            f"{r['range'] or '—'} | {desc or '—'} |"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #
def _cfg_of(target: Any) -> LocatorConfig:
    """接受 LocatorConfig 或任何带 .cfg 的对象（ArucoLocator）。"""
    if isinstance(target, LocatorConfig):
        return target
    cfg = getattr(target, "cfg", None)
    if isinstance(cfg, LocatorConfig):
        return cfg
    raise TypeError(f"需要 LocatorConfig 或带 .cfg 的对象，得到 {type(target).__name__}")


def canonical_path(path: str) -> str:
    """把简写补全为完整点号路径。

    允许直接写叶子名（如 ``edge_refine``）；若该名字在多个分组下重复则报错，
    提示调用方写明分组 —— 宁可报错也不猜。
    """
    if "." in path:
        return path
    matches = [p for p, _, _, _ in _leaf_names() if p.rsplit(".", 1)[-1] == path]
    if not matches:
        raise KeyError(f"参数不存在：{path!r}")
    if len(matches) > 1:
        raise KeyError(f"参数名 {path!r} 有歧义，请写明分组：{sorted(matches)}")
    return matches[0]


def _resolve(cfg: LocatorConfig, path: str):
    path = canonical_path(path)
    parts = path.split(".")
    obj: Any = cfg
    for part in parts[:-1]:
        if not is_dataclass(obj) or not hasattr(obj, part):
            raise KeyError(f"参数路径不存在：{path}")
        obj = getattr(obj, part)
    if not is_dataclass(obj) or not any(f.name == parts[-1] for f in fields(obj)):
        raise KeyError(f"参数路径不存在：{path}")
    return obj, parts[-1]


def get_param(target: Any, path: str) -> Any:
    """按点号路径读参数，如 ``get_param(cfg, "detection.edge_search_px")``。"""
    obj, name = _resolve(_cfg_of(target), path)
    return getattr(obj, name)


def set_param(target: Any, path: str, value: Any, validate: bool = True) -> Any:
    """按点号路径写参数（带类型转换与范围校验），返回实际写入的值。"""
    cfg = _cfg_of(target)
    obj, name = _resolve(cfg, path)
    declared = _declared_type(obj, name)
    coerced = coerce(value, declared) if validate else value
    if validate:
        _check_range(path, coerced)
    setattr(obj, name, coerced)
    _invalidate_cache(target)
    return coerced


def apply_params(target: Any, params: dict | None = None, **overrides) -> Any:
    """批量写参数，键为点号路径（也接受叶子名简写）。

        apply_params(loc, {"detection.edge_refine": False, "geometry.vertex_mode": "full"})
        apply_params(loc, {"detection": {"edge_refine": False}})   # 也支持嵌套写法
        apply_params(loc, edge_refine=False)                       # 简写（需无歧义）

    任何一个键写错都会抛 KeyError —— 不静默忽略。值为 ``None`` 表示"保持不动"，
    只有内参、场地边长、相机高度、天底点这几个可空参数允许显式写 None。
    """
    _cfg_of(target)  # 类型检查：target 必须是配置或带 .cfg 的对象
    flat: dict[str, Any] = {}

    def merge(src: dict, prefix: str = "") -> None:
        for key, value in src.items():
            full = f"{prefix}{key}"
            if isinstance(value, dict):
                merge(value, full + ".")
            else:
                flat[full] = value

    for src in (params, overrides):
        if src:
            merge(src)

    for path, value in flat.items():
        if value is None and canonical_path(path) not in _NULLABLE:
            continue
        set_param(target, path, value)
    return target


# 允许写入 None 的参数（其余参数写 None 视为"不修改"）
_NULLABLE = {
    "camera.intrinsic_file",
    "camera.field_size_mm",
    "camera.parallax.camera_height_mm",
    "camera.parallax.nadir_xy",
}


def apply_profile(target: Any, profile: str) -> Any:
    """套用预设档位（见 :func:`list_profiles`）。"""
    if profile not in PROFILES:
        raise KeyError(f"未知档位 {profile!r}，可用：{list_profiles()}")
    return apply_params(target, PROFILES[profile])


def flatten_config(target: Any) -> dict:
    """导出为"点号路径 → 值"的扁平字典（便于落盘、日志、比对）。"""
    cfg = _cfg_of(target)
    return {p: v for p, v, _, _ in _leaf_names(cfg)}


def _invalidate_cache(target: Any) -> None:
    """改了配置要让依赖它的缓存失效（检测器持有参数的副本）。"""
    from .detector import MarkerDetector

    if hasattr(target, "detector") and isinstance(target.detector, MarkerDetector):
        target.detector = MarkerDetector(target.cfg.detection)
    if hasattr(target, "smoother"):
        from .api import _Smoother

        target.smoother = _Smoother(
            target.cfg.output.smooth,
            target.cfg.output.ema_alpha,
            target.cfg.output.window,
        )


# --------------------------------------------------------------------------- #
# 自检：元数据不许写错参数名
# --------------------------------------------------------------------------- #
def _validate_meta() -> None:
    valid = {p for p, _, _, _ in _leaf_names()}
    bad = sorted(set(_META) - valid)
    if bad:
        raise RuntimeError(f"参数元数据里存在不存在的参数名：{bad}")
    for prof, over in PROFILES.items():
        bad = sorted(k for k in over if k not in valid)
        if bad:
            raise RuntimeError(f"预设档位 {prof} 里存在不存在的参数名：{bad}")


_validate_meta()
