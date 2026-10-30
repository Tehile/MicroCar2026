"""对外接口：ArucoLocator 与 Localization。

接口契约（每帧）：
    valid、x、y、theta_deg、方向单位向量(dir_x, dir_y)、
    重投影/方正度残差 rms_field、命中的角标记 ID、帧号、时间戳，
    另有 flags 与 diagnostics 供调试。
"""

from __future__ import annotations

import json
import math
import os
from collections import deque
from dataclasses import dataclass, field

import cv2
import numpy as np

from .config import LocatorConfig, load_config
from .detector import MarkerDetector, MarkerObservation
from .geometry import (
    MARKER_LOCAL,
    apply_homography,
    build_frame,
    corner_marker_scales,
    fit_marker_pose,
    fit_marker_pose_from_field,
    fit_similarity,
    refine_homography,
    select_inner_vertices,
)
from .parallax import solve as solve_parallax
from .parameters import apply_params, apply_profile
from .sources import imread_unicode, iter_source

# 标志位
FLAG_AMBIGUOUS_ORIGIN = "ambiguous_origin"
FLAG_AMBIGUOUS_AXIS = "ambiguous_axis"
FLAG_HANDEDNESS = "handedness_flipped"
FLAG_VERTEX_OVERRIDE = "inner_vertex_override"
FLAG_VERTEX_DEGENERATE = "inner_vertex_degenerate"
FLAG_INNER_UNCERTAIN = "inner_vertex_uncertain"
FLAG_TARGET_NOT_SQUARE = "target_not_square"
FLAG_SMOOTHED = "smoothed"
FLAG_HEIGHT_CORRECTED = "height_corrected"
FLAG_PARALLAX_UNCALIBRATED = "parallax_uncalibrated"
FLAG_TARGET_RECTIFIED = "target_rectified"
FLAG_GRID_MATCHED = "grid_matched"


@dataclass
class Localization:
    """单帧定位结果。"""

    valid: bool
    frame_index: int = 0
    timestamp: float = 0.0

    x: float | None = None  # 场地坐标，[0,1]²
    y: float | None = None
    theta_deg: float | None = None  # 标记自身 +x 轴相对场地 +x 轴，逆时针为正
    dir_x: float | None = None  # 方向单位向量（场地系）
    dir_y: float | None = None

    x_raw: float | None = None  # 滤波前
    y_raw: float | None = None
    theta_deg_raw: float | None = None

    corner_ids: list[int] = field(default_factory=list)
    missing_corner_ids: list[int] = field(default_factory=list)
    origin_id: int | None = None

    rms_field: float | None = None  # 角标记四边形映射成正方形的残差（场地单位）
    target_rms: float | None = None  # 0 号标记同类残差
    marker_scale: float | None = None  # 标记边长 / 场地边长
    scale_cv: float | None = None  # 五个标记的尺度离散度

    flags: list[str] = field(default_factory=list)
    reason: str = ""
    diagnostics: dict = field(default_factory=dict)

    @property
    def dir_vector(self) -> tuple[float, float] | None:
        if self.dir_x is None or self.dir_y is None:
            return None
        return (self.dir_x, self.dir_y)

    def to_dict(self, include_diagnostics: bool = True, flat: bool = False) -> dict:
        """转成字典。

        flat=False（默认，推荐给其他程序/JSON 用）：``corner_ids`` / ``flags`` 等
        多值字段是**真正的 list**。
        flat=True：把多值字段压成 ``"1;2;3;4"`` 这类分号字符串，方便写 CSV / 打表。
        """
        if flat:
            return self.to_row(include_diagnostics=include_diagnostics)
        d = {
            "frame": int(self.frame_index),
            "time": round(float(self.timestamp), 6),
            "valid": bool(self.valid),
            "x": _r(self.x),
            "y": _r(self.y),
            "theta_deg": _r(self.theta_deg),
            "dir_x": _r(self.dir_x),
            "dir_y": _r(self.dir_y),
            "x_raw": _r(self.x_raw),
            "y_raw": _r(self.y_raw),
            "theta_deg_raw": _r(self.theta_deg_raw),
            "origin_id": self.origin_id,
            "corner_ids": [int(i) for i in self.corner_ids],
            "missing_corner_ids": [int(i) for i in self.missing_corner_ids],
            "rms_field": _r(self.rms_field),
            "target_rms": _r(self.target_rms),
            "marker_scale": _r(self.marker_scale),
            "scale_cv": _r(self.scale_cv),
            "flags": list(self.flags),
            "reason": self.reason,
        }
        if include_diagnostics:
            d["diagnostics"] = self.diagnostics
        return d

    def to_row(self, include_diagnostics: bool = False) -> dict:
        """扁平字典：多值字段用分号连接，列集合与 :data:`CSV_FIELDS` 一致。"""
        d = {
            "frame": int(self.frame_index),
            "time": round(float(self.timestamp), 6),
            "valid": bool(self.valid),
            "x": _r(self.x),
            "y": _r(self.y),
            "theta_deg": _r(self.theta_deg),
            "dir_x": _r(self.dir_x),
            "dir_y": _r(self.dir_y),
            "x_raw": _r(self.x_raw),
            "y_raw": _r(self.y_raw),
            "theta_deg_raw": _r(self.theta_deg_raw),
            "origin_id": self.origin_id,
            "corner_ids": ";".join(str(i) for i in self.corner_ids),
            "missing_corner_ids": ";".join(str(i) for i in self.missing_corner_ids),
            "rms_field": _r(self.rms_field),
            "target_rms": _r(self.target_rms),
            "marker_scale": _r(self.marker_scale),
            "scale_cv": _r(self.scale_cv),
            "flags": ";".join(self.flags),
            "reason": self.reason,
        }
        if include_diagnostics:
            d["diagnostics"] = self.diagnostics
        return d

    def to_json(self, include_diagnostics: bool = True, flat: bool = False) -> str:
        return json.dumps(
            self.to_dict(include_diagnostics=include_diagnostics, flat=flat),
            ensure_ascii=False,
        )

    def summary(self) -> str:
        if not self.valid:
            return f"#{self.frame_index:>5}  invalid  reason={self.reason}  flags={','.join(self.flags)}"
        rms = "n/a" if self.rms_field is None else f"{self.rms_field:.4f}"
        return (
            f"#{self.frame_index:>5}  x={self.x:.4f} y={self.y:.4f} "
            f"theta={self.theta_deg:+7.2f}°  dir=({self.dir_x:+.3f},{self.dir_y:+.3f})  "
            f"rms={rms}  origin={self.origin_id}  corners={self.corner_ids}"
        )


def _r(v):
    return None if v is None else round(float(v), 6)


CSV_FIELDS = [
    "frame",
    "time",
    "valid",
    "x",
    "y",
    "theta_deg",
    "dir_x",
    "dir_y",
    "x_raw",
    "y_raw",
    "theta_deg_raw",
    "origin_id",
    "corner_ids",
    "missing_corner_ids",
    "rms_field",
    "target_rms",
    "marker_scale",
    "scale_cv",
    "flags",
    "reason",
]


# --------------------------------------------------------------------------- #
# 时序滤波
# --------------------------------------------------------------------------- #
class _Smoother:
    def __init__(self, mode: str, alpha: float = 0.4, window: int = 5):
        self.mode = mode
        self.alpha = float(alpha)
        self.window = max(1, int(window))
        self._buf: deque = deque(maxlen=self.window)
        self._state: tuple[float, float, float, float] | None = None

    def reset(self):
        self._buf.clear()
        self._state = None

    def apply(self, x: float, y: float, dx: float, dy: float):
        if self.mode == "ema":
            if self._state is None:
                self._state = (x, y, dx, dy)
            else:
                a = self.alpha
                px, py, pdx, pdy = self._state
                nx, ny = a * x + (1 - a) * px, a * y + (1 - a) * py
                ndx, ndy = a * dx + (1 - a) * pdx, a * dy + (1 - a) * pdy
                self._state = (nx, ny, ndx, ndy)
            sx, sy, sdx, sdy = self._state
        elif self.mode == "moving_average":
            self._buf.append((x, y, dx, dy))
            sx = float(np.mean([b[0] for b in self._buf]))
            sy = float(np.mean([b[1] for b in self._buf]))
            sdx = float(np.mean([b[2] for b in self._buf]))
            sdy = float(np.mean([b[3] for b in self._buf]))
        else:
            return x, y, dx, dy

        norm = math.hypot(sdx, sdy)
        if norm > 1e-9:
            sdx, sdy = sdx / norm, sdy / norm
        return sx, sy, sdx, sdy


# --------------------------------------------------------------------------- #
# 主定位器
# --------------------------------------------------------------------------- #
class ArucoLocator:
    """ArUco 场地定位器。线程不安全，一帧一次调用 :meth:`locate`。"""

    def __init__(self, config: LocatorConfig | str | os.PathLike | None = None):
        if isinstance(config, (str, os.PathLike)):
            config = load_config(config)
        elif config is None:
            config = LocatorConfig()
        self.cfg: LocatorConfig = config
        self.detector = MarkerDetector(self.cfg.detection)
        # 重投影兜底用的检测器：光栅尺寸已由重投影决定，不需要多尺度
        self._rect_detector: MarkerDetector | None = None
        if getattr(self.cfg.detection, "rectified_target", False):
            from dataclasses import replace as _replace

            self._rect_detector = MarkerDetector(_replace(self.cfg.detection, scales=[1.0]))
        self.smoother = _Smoother(
            self.cfg.output.smooth, self.cfg.output.ema_alpha, self.cfg.output.window
        )
        self._K: np.ndarray | None = None
        self._dist: np.ndarray | None = None
        if self.cfg.camera.intrinsic_file:
            # 内参有两个用途：undistort 与"自动求天底点"，所以给了就加载
            self._K, self._dist = load_intrinsics(self.cfg.camera.intrinsic_file)
        # 供 annotate() 复用的中间结果（不进 JSON）
        self.last_debug: dict = {}
        # 格点匹配通道（第二通道）惰性构造：不开启就不付任何代价
        self._grid = None

    # ------------------------------------------------------------------ #
    @property
    def grid_matcher(self):
        """格点匹配器（首次访问时按 ``config.grid`` 构造）。"""
        if self._grid is None:
            from .gridmatch import GridMatcher

            self._grid = GridMatcher(self.cfg.grid)
        return self._grid

    # ------------------------------------------------------------------ #
    def reset(self):
        self.smoother.reset()

    def undistort(self, frame: np.ndarray) -> np.ndarray:
        if not self.cfg.camera.undistort or self._K is None:
            return frame
        return cv2.undistort(frame, self._K, self._dist)

    # ------------------------------------------------------------------ #
    def locate(self, frame_bgr: np.ndarray, frame_index: int = 0, timestamp: float = 0.0) -> Localization:
        cfg = self.cfg
        det = cfg.detection
        geo = cfg.geometry
        res = Localization(valid=False, frame_index=frame_index, timestamp=timestamp)
        self.last_debug = {"index": frame_index, "obs": {}, "H": None, "H_inv": None, "frame_obj": None}

        frame = self.undistort(frame_bgr)
        obs = self.detector.detect(frame)
        self.last_debug["obs"] = obs
        res.diagnostics["detected_ids"] = sorted(int(k) for k in obs)

        corner_ids = [int(i) for i in det.corner_ids]
        present = [i for i in corner_ids if i in obs]
        res.corner_ids = sorted(present)
        res.missing_corner_ids = [i for i in corner_ids if i not in obs]

        if len(present) < len(corner_ids):
            res.reason = "corner_markers_missing"
            return res

        # 1) 内顶点
        inner, inner_info = select_inner_vertices(obs, geo, corner_ids)
        res.diagnostics["inner_vertices"] = inner_info
        if inner is None:
            res.reason = inner_info.get("reason", "inner_vertex_failed")
            return res
        if inner_info.get("vertex_override"):
            res.flags.append(FLAG_VERTEX_OVERRIDE)
        if inner_info.get("chosen_by") == "xdiag_degenerate":
            # 对角线配对退化（四点近共线等）→ 已自动回退 nearest。必须显式告知：
            # 这类兜底是"静默降级"，只看结果指标看不出来。
            res.flags.append(FLAG_VERTEX_DEGENERATE)
        gap_rel = inner_info.get("cost_gap_rel")
        if gap_rel is not None and gap_rel < geo.min_vertex_cost_gap_rel:
            res.flags.append(FLAG_INNER_UNCERTAIN)

        # 2) 场地坐标系
        frame_obj, frame_info = build_frame(inner, obs, frame.shape, geo)
        res.diagnostics["frame"] = frame_info
        if frame_obj is None:
            res.reason = frame_info.get("reason", "frame_failed")
            return res
        res.origin_id = frame_obj.origin_id

        if frame_info.get("origin_margin", 1.0) < geo.min_origin_margin:
            res.flags.append(FLAG_AMBIGUOUS_ORIGIN)
        if frame_info.get("axis_margin", 1.0) < geo.min_axis_margin:
            res.flags.append(FLAG_AMBIGUOUS_AXIS)
        if not frame_info.get("theta_ccw", True):
            res.flags.append(FLAG_HANDEDNESS)

        # 3) 可选：联合最小二乘精化
        H = frame_obj.H_img2field
        H_ref, refine_info = refine_homography(frame_obj, obs, frame.shape, cfg.refine)
        res.diagnostics["refine"] = refine_info
        if refine_info.get("enabled") and refine_info.get("success"):
            H = H_ref
        try:
            H_inv = np.linalg.inv(H)
        except np.linalg.LinAlgError:
            res.reason = "degenerate_homography"
            return res
        self.last_debug.update({"H": H, "H_inv": H_inv, "frame_obj": frame_obj})

        # 3.5) 目标缺失 → 场地平面重投影兜底。
        # 车顶标记在原图里被透视压扁且常被运动模糊抹平；warp 成正俯视后
        # 它近似正对、尺寸固定，轮廓与位判决都更容易（实测目标检出 6% → 12%）。
        if det.target_id not in obs and self._rect_detector is not None:
            found = self._target_from_rectified(frame, H)
            if found is not None:
                obs[found.marker_id] = found
                res.flags.append(FLAG_TARGET_RECTIFIED)
                res.diagnostics["detected_ids"] = sorted(int(k) for k in obs)
        if det.target_id not in obs:
            # 第二通道：目标在 ArUco 阶段就丢了（运动模糊把 36 格压成斜坡，轮廓
            # 凑不出来）。此时四角单应 H 已在手，直接用格点匹配在正射图上找图案。
            if cfg.grid.enable:
                gres = self._solve_grid(frame, H, frame_index, timestamp, res)
                if gres.valid:
                    res.diagnostics["channel"] = "grid"
                    return res
                res.diagnostics["channel"] = "grid_rejected"
            res.reason = "target_marker_missing"
            return res

        # 4) 几何质量：角标记四边形必须是正方形
        scales = corner_marker_scales(H, obs, frame_obj.ring_ids)
        sq_rms = []
        for mid in frame_obj.ring_ids:
            mapped = cv2.perspectiveTransform(
                obs[mid].corners.reshape(-1, 1, 2).astype(np.float64), H
            ).reshape(-1, 2)
            _, _, _, rms = fit_similarity(MARKER_LOCAL, mapped)
            sq_rms.append(float(rms))
        res.rms_field = max(sq_rms) if sq_rms else None
        res.diagnostics["corner_square_rms"] = sq_rms

        # 5) 0 号标记位姿。先做高度视差修正，再拟合 ——
        #    修正只是把四角沿"天底点方向"做位似缩放（不改形状、不改方向），
        #    所以位姿拟合必须放在修正之后，位置和方向才一致。
        mapped_target = apply_homography(H, obs[det.target_id].corners)
        pose_raw = fit_marker_pose_from_field(mapped_target)  # 修正前的观测尺度
        par = cfg.camera.parallax
        sol, par_diag = solve_parallax(
            par,
            H,
            self._K,
            corner_scales=scales,
            target_scale=pose_raw.scale,
        )
        if sol is not None:
            mapped_target = sol.apply(mapped_target)
            res.flags.append(FLAG_HEIGHT_CORRECTED)
            par_info = sol.to_dict()
            # 修正量：位置被挪了多少（场地单位 / mm）
            shift = float(np.linalg.norm(mapped_target.mean(axis=0) - pose_raw.center))
            par_info["shift_field"] = round(shift, 6)
            if cfg.camera.field_size_mm:
                par_info["shift_mm"] = round(shift * float(cfg.camera.field_size_mm), 3)
            res.diagnostics["parallax"] = par_info
        elif par.enable and par_diag.get("reason"):
            # 开了但没标定：不静默、不猜，明确告警
            res.flags.append(FLAG_PARALLAX_UNCALIBRATED)
            res.diagnostics["parallax"] = par_diag
        pose = fit_marker_pose_from_field(mapped_target)
        res.target_rms = pose.rms
        res.marker_scale = pose.scale

        all_scales = scales + [pose.scale]
        if all_scales:
            m = float(np.mean(all_scales))
            res.scale_cv = float(np.std(all_scales) / m) if m > 1e-12 else None
        res.diagnostics["marker_scales"] = {int(k): float(v) for k, v in zip(frame_obj.ring_ids, scales)}

        x, y = float(pose.center[0]), float(pose.center[1])
        dx, dy = float(pose.dir_vector[0]), float(pose.dir_vector[1])
        theta_deg = math.degrees(pose.theta_rad)

        # 6) 有效性判定
        ok = True
        if res.rms_field is not None and res.rms_field > geo.max_square_rms:
            res.reason = "square_check_failed"
            ok = False
        elif res.target_rms is not None and res.target_rms > geo.max_square_rms:
            res.flags.append(FLAG_TARGET_NOT_SQUARE)

        if not ok:
            return res

        # 7) 滤波
        res.x_raw, res.y_raw, res.theta_deg_raw = x, y, theta_deg
        sx, sy, sdx, sdy = self.smoother.apply(x, y, dx, dy)
        if (sx, sy, sdx, sdy) != (x, y, dx, dy):
            res.flags.append(FLAG_SMOOTHED)
        res.x, res.y = float(sx), float(sy)
        res.dir_x, res.dir_y = float(sdx), float(sdy)
        res.theta_deg = math.degrees(math.atan2(sdy, sdx))
        res.valid = True
        return res

    # ------------------------------------------------------------------ #
    def field_frame_from_obs(self, obs: dict, image_shape: tuple) -> tuple[np.ndarray | None, dict]:
        """**仅用四角标记**解场地系单应（不要求 0 号目标在画面里）。

        与 :meth:`locate` 的关系：`locate` 在目标缺失时提前返回、不解 H；
        而会话级迷宫融合只需要"四角 → 场地系"的 H（目标在不在场内不影响迷宫结构）。
        本方法复用同一条内顶点/建系/精化链路，纯增量，不改动 `locate` 行为，
        不写 `last_debug`、不触碰平滑状态。

        返回 (H_img2field | None, info)。`info["ambiguous_axis"]` 为 True 时
        调用方应弃用该帧（轴向存疑 → 迷宫可能被转 90°，会污染多帧融合）。
        """
        det = self.cfg.detection
        geo = self.cfg.geometry
        corner_ids = [int(i) for i in det.corner_ids]
        if any(i not in obs for i in corner_ids):
            return None, {"reason": "corner_markers_missing"}
        inner, inner_info = select_inner_vertices(obs, geo, corner_ids)
        if inner is None:
            return None, {"reason": inner_info.get("reason", "inner_vertex_failed"),
                          "inner_vertices": inner_info}
        frame_obj, frame_info = build_frame(inner, obs, image_shape, geo)
        if frame_obj is None:
            return None, {"reason": frame_info.get("reason", "frame_failed"),
                          "frame": frame_info}
        H = frame_obj.H_img2field
        H_ref, refine_info = refine_homography(frame_obj, obs, image_shape, self.cfg.refine)
        if refine_info.get("enabled") and refine_info.get("success"):
            H = H_ref
        return H, {
            "origin_id": frame_obj.origin_id,
            "frame": frame_info,
            "refine": refine_info,
            "ambiguous_axis": bool(frame_info.get("axis_margin", 1.0) < geo.min_axis_margin),
        }

    # ------------------------------------------------------------------ #
    def _target_from_rectified(self, frame: np.ndarray, H: np.ndarray):
        """在"场地平面正俯视"图里找目标标记，找到则把角点映射回原图坐标。

        为什么有用：车顶标记是水平面内的正方形，在倾斜视角下被各向异性压缩，
        轮廓与 4×4 位判决都更难；重投影把它恢复成正对视角。
        代价：一次大图 warp + 一次检测（~0.5s @ 4500px），只在目标缺失时执行。
        """
        d = self.cfg.detection
        side = int(d.rectified_side)
        margin = float(d.rectified_margin)
        span = 1.0 + 2.0 * margin
        s = side / span
        A = np.array([[s, 0.0, margin * s],
                      [0.0, -s, (1.0 + margin) * s],
                      [0.0, 0.0, 1.0]], np.float64)  # 场地[0,1]² → 光栅像素
        M = A @ np.asarray(H, np.float64)
        try:
            rect = cv2.warpPerspective(frame, M, (side, side), flags=cv2.INTER_CUBIC)
        except cv2.error:
            return None
        obs_r = self._rect_detector.detect(rect)
        ob = obs_r.get(int(d.target_id))
        if ob is None:
            return None
        # 尺寸闸门：场地里的标记边长 ≈ (marker_side/field)*side/span。
        # 不知道物理尺寸时用宽区间（1% ~ 12% 光栅边长），滤掉墙面纹理级候选。
        side_px = float(np.linalg.norm(ob.corners[1] - ob.corners[0]))
        if not (0.01 * side <= side_px <= 0.12 * side):
            return None
        try:
            Minv = np.linalg.inv(M)
        except np.linalg.LinAlgError:
            return None
        pts = cv2.perspectiveTransform(
            ob.corners.reshape(-1, 1, 2).astype(np.float32), Minv).reshape(-1, 2)
        if not np.all(np.isfinite(pts)):
            return None
        return MarkerObservation(marker_id=int(d.target_id),
                                 corners=pts.astype(np.float32), scale=1.0)

    # ------------------------------------------------------------------ #
    def _solve_grid(self, frame: np.ndarray, H: np.ndarray, frame_index: int,
                    timestamp: float, res: Localization) -> Localization:
        """用格点匹配通道填 ``res``（就地修改并返回）。

        只写 x/y/theta/dir/flags/diagnostics 与 valid，**不写 reason** ——
        调用方最清楚"拒绝之后该报什么原因"。也不走时序滤波：本通道逐帧独立，
        没有跟踪语义，混进同一个平滑器会污染 ArUco 路径的状态。
        """
        sol = self.grid_matcher.search(frame, H)
        res.x_raw, res.y_raw, res.theta_deg_raw = sol["x"], sol["y"], sol["theta"]
        res.x, res.y = sol["x"], sol["y"]
        th = math.radians(sol["theta"])
        res.dir_x, res.dir_y = math.cos(th), math.sin(th)
        res.theta_deg = float(sol["theta"])
        res.valid = self.grid_matcher.accept(sol)
        if res.valid:
            # 只在判据通过时打标：`valid=False` + `grid_matched` 同时出现会自相矛盾
            res.flags.append(FLAG_GRID_MATCHED)
        res.diagnostics["grid"] = {
            "gap": round(float(sol["gap"]), 6),
            "sharp": round(float(sol["sharp"]), 6),
            "margin": round(float(sol["margin"]), 6),
            "ncc": round(float(sol["ncc"]), 6),
            "n_bad": int(sol["n_bad"]),
            "beta": round(float(sol["beta"]), 3),
            "rot": int(sol["rot"]),
            "seed_spread_mm": round(float(sol["seed_spread_mm"]), 3),
            "runner_margin": round(float(sol["runner_margin"]), 4),
            "kappa": float(sol["kappa"]),
            "inset": float(sol["inset"]),
            "t_total": float(sol["t_total"]),
            # 本通道解的是"场地平面（角标记所在高度）"上的位置，与 ArUco 通道
            # 同一假设；0 号标记低一截时请自行用 parallax.py 修正，这里不隐式改。
            "parallax": "not_applied",
        }
        return res

    def locate_grid(self, frame_bgr: np.ndarray, H: np.ndarray | None = None,
                    frame_index: int = 0, timestamp: float = 0.0) -> Localization:
        """**只走格点匹配通道**：不需要 0 号标记被 ArUco 检出。

        Parameters
        ----------
        frame_bgr
            原始彩色帧（BGR）。这一路必须用彩色：色度用来压制不可信像素。
        H
            ``img → field`` 单应。给 None 时内部只用四个角标记解一次
            （等价于 :meth:`field_frame_from_obs`），因此"目标完全看不见"也能定位。
        """
        res = Localization(valid=False, frame_index=frame_index, timestamp=timestamp)
        frame = self.undistort(frame_bgr)
        det = self.cfg.detection
        obs: dict = {}
        if H is None:
            obs = self.detector.detect(frame)
            corner_ids = [int(i) for i in det.corner_ids]
            res.corner_ids = sorted(i for i in corner_ids if i in obs)
            res.missing_corner_ids = [i for i in corner_ids if i not in obs]
            res.diagnostics["detected_ids"] = sorted(int(k) for k in obs)
            H, info = self.field_frame_from_obs(obs, frame.shape)
            res.diagnostics["field_frame"] = info
            if H is None:
                res.reason = info.get("reason", "frame_failed")
                return res
            res.origin_id = info.get("origin_id")
            if info.get("ambiguous_axis"):
                res.flags.append(FLAG_AMBIGUOUS_AXIS)
        H = np.asarray(H, np.float64)
        # 让 annotate() 能画出场地框与坐标轴（第二通道没有 frame_obj，H 就够）
        try:
            self.last_debug = {"index": frame_index, "obs": obs, "H": H,
                               "H_inv": np.linalg.inv(H), "frame_obj": None}
        except np.linalg.LinAlgError:
            res.reason = "degenerate_homography"
            return res
        res = self._solve_grid(frame, H, frame_index, timestamp, res)
        if not res.valid:
            res.reason = "grid_low_confidence"
        return res

    # ------------------------------------------------------------------ #
    def annotate(self, frame: np.ndarray, loc: Localization, obs: dict[int, MarkerObservation] | None = None):
        """在画面上叠加检测与解算结果（含俯视小窗）。"""
        from .viz import draw_overlay

        debug = self.last_debug if self.last_debug.get("index") == loc.frame_index else None
        if obs is None:
            obs = (debug or {}).get("obs") or self.detector.detect(self.undistort(frame))
        return draw_overlay(frame, loc, obs, self.cfg, debug)


# --------------------------------------------------------------------------- #
# 便捷函数
# --------------------------------------------------------------------------- #
def create_locator(
    config: LocatorConfig | str | os.PathLike | None = None,
    *,
    config_file: str | os.PathLike | None = None,
    profile: str | None = None,
    params: dict | None = None,
    **overrides,
) -> ArucoLocator:
    """**推荐入口**：装配一个定位器。

    Parameters
    ----------
    config
        一个 :class:`LocatorConfig`，或 YAML 配置路径；None 表示用内置默认值。
    config_file
        YAML 配置路径（与 ``config`` 二选一）。
    profile
        预设档位名，见 :func:`aruco_locator.list_profiles`（如 "fast" / "robust"）。
    params
        参数覆盖字典，键为点号路径或嵌套字典，见
        :func:`aruco_locator.apply_params`。
    **overrides
        参数覆盖（叶子名简写即可，如 ``edge_refine=False``）。

    Examples
    --------
    >>> loc = create_locator("config.yaml", profile="fast", params={"output.smooth": "ema"})
    >>> loc = create_locator(profile="robust", edge_search_px=4.0)
    """
    if config is not None and config_file is not None:
        raise ValueError("config 与 config_file 只能给一个")
    if config_file is not None:
        cfg = load_config(config_file)
    elif isinstance(config, (str, os.PathLike)):
        cfg = load_config(config)
    elif config is None:
        cfg = LocatorConfig()
    else:
        cfg = config

    if profile or params or overrides:
        import copy

        # 调用方给了自己的配置对象时，不就地改它的参数
        cfg = copy.deepcopy(cfg)
        if profile:
            apply_profile(cfg, profile)
        if params or overrides:
            apply_params(cfg, params, **overrides)
    return ArucoLocator(cfg)


def locate(
    frame_bgr: np.ndarray,
    config: LocatorConfig | str | os.PathLike | None = None,
    *,
    frame_index: int = 0,
    timestamp: float = 0.0,
    profile: str | None = None,
    params: dict | None = None,
    **overrides,
) -> Localization:
    """一次性调用：装配临时定位器并解算一帧，返回 :class:`Localization`。

    适合"给一批互不相关的图片各算一次"的场景。**视频/摄像头请改用
    :class:`ArucoLocator` 常驻实例** —— 它会保留时序滤波状态，也省去每帧重建的
    开销（重建约几毫秒）。

    Examples
    --------
    >>> res = locate(img)                      # 全部默认参数
    >>> res = locate(img, "config.yaml")
    >>> res = locate(img, edge_refine=False)   # 临时改一个参数
    >>> res.to_dict()["x"], res.to_dict()["y"]
    """
    locator = create_locator(
        config, profile=profile, params=params, **overrides
    )
    return locator.locate(frame_bgr, frame_index=frame_index, timestamp=timestamp)


def locate_image(path: str | os.PathLike, config: LocatorConfig | str | None = None) -> Localization:
    frame = imread_unicode(os.fspath(path))
    if frame is None:
        raise RuntimeError(f"读不到图片：{path}")
    return ArucoLocator(config).locate(frame, frame_index=0, timestamp=0.0)


def iter_locations(source, config=None, **kw):
    """逐帧定位，产出 Localization（kwargs 透传给 iter_source）。"""
    locator = ArucoLocator(config)
    for item in iter_source(source, **kw):
        yield locator.locate(item.frame, item.index, item.timestamp)


def locate_folder(path: str | os.PathLike, config=None) -> list[Localization]:
    return list(iter_locations(os.fspath(path), config))


def locate_video(path: str | os.PathLike, config=None, max_frames: int | None = None) -> list[Localization]:
    return list(iter_locations(os.fspath(path), config, max_frames=max_frames))


# --------------------------------------------------------------------------- #
# 相机内参
# --------------------------------------------------------------------------- #
def load_intrinsics(path: str | os.PathLike):
    """读取内参：支持 .npz（camera_matrix / dist_coeffs）与 .yaml/.json。"""
    import yaml

    path = os.fspath(path)
    ext = os.path.splitext(path)[1].lower()
    if ext == ".npz":
        data = np.load(path)
        K = np.asarray(data["camera_matrix"], dtype=np.float64)
        dist = np.asarray(data["dist_coeffs"], dtype=np.float64)
    else:
        with open(path, "r", encoding="utf-8") as fp:
            data = yaml.safe_load(fp) if ext in (".yaml", ".yml") else json.load(fp)
        K = np.asarray(data["camera_matrix"], dtype=np.float64)
        dist = np.asarray(data["dist_coeffs"], dtype=np.float64)
    if dist.ndim == 2:
        dist = dist.ravel()
    return K, dist.reshape(1, -1)
