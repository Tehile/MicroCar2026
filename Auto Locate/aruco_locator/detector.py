"""ArUco 标记检测：字典构建、参数配置、多尺度检测、亚像素角点精化。"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from .config import DetectionConfig
from .corners import refine_quad_by_edges

_CORNER_REFINE = {
    "none": cv2.aruco.CORNER_REFINE_NONE,
    "contour": cv2.aruco.CORNER_REFINE_CONTOUR,
    "subpix": cv2.aruco.CORNER_REFINE_SUBPIX,
    "apriltag": cv2.aruco.CORNER_REFINE_APRILTAG,
}


@dataclass
class MarkerObservation:
    """单帧里检出的一个标记。

    角点顺序为 OpenCV 约定：以标记印刷方向为基准的 左上 / 右上 / 右下 / 左下，
    因此 corners[0] → corners[1] 就是标记自身的 +x 轴。
    """

    marker_id: int
    corners: np.ndarray  # (4, 2) float32，图像像素坐标
    scale: float = 1.0  # 检出时使用的缩放系数
    refined: bool = field(default=False)
    edge_refined: bool = field(default=False)  # 是否成功做了边缘直线拟合精化
    line_scatter: float = 0.0  # 四条边到各自拟合直线的 rms（像素），越小越可信

    @property
    def center(self) -> np.ndarray:
        return self.corners.mean(axis=0)

    @property
    def side(self) -> float:
        """四边形四条边的平均长度（像素）。"""
        c = self.corners
        return float(np.mean([np.linalg.norm(c[(i + 1) % 4] - c[i]) for i in range(4)]))

    @property
    def area(self) -> float:
        return float(cv2.contourArea(self.corners.reshape(-1, 1, 2).astype(np.float32)))


class MarkerDetector:
    """按配置构建检测器，一帧调用一次 :meth:`detect`。"""

    def __init__(self, cfg: DetectionConfig):
        self.cfg = cfg
        dict_id = getattr(cv2.aruco, cfg.dict_name, None)
        if dict_id is None:
            raise ValueError(f"OpenCV 不认识字典名 {cfg.dict_name!r}")
        self.dictionary = cv2.aruco.getPredefinedDictionary(dict_id)

        p = cv2.aruco.DetectorParameters()
        p.adaptiveThreshWinSizeMin = int(cfg.adaptive_thresh_win_size_min)
        p.adaptiveThreshWinSizeMax = int(cfg.adaptive_thresh_win_size_max)
        p.adaptiveThreshWinSizeStep = int(cfg.adaptive_thresh_win_size_step)
        p.adaptiveThreshConstant = float(cfg.adaptive_thresh_constant)
        p.minMarkerPerimeterRate = float(cfg.min_marker_perimeter_rate)
        p.maxMarkerPerimeterRate = float(cfg.max_marker_perimeter_rate)
        p.polygonalApproxAccuracyRate = float(cfg.polygonal_approx_accuracy_rate)
        p.minDistanceToBorder = int(cfg.min_distance_to_border)
        p.errorCorrectionRate = float(cfg.error_correction_rate)
        p.perspectiveRemovePixelPerCell = int(cfg.perspective_remove_pixel_per_cell)
        p.perspectiveRemoveIgnoredMarginPerCell = float(cfg.perspective_remove_ignored_margin_per_cell)
        p.cornerRefinementMethod = _CORNER_REFINE.get(cfg.corner_refinement, cv2.aruco.CORNER_REFINE_SUBPIX)
        p.cornerRefinementWinSize = int(cfg.corner_refinement_win)
        p.cornerRefinementMaxIterations = int(cfg.corner_refinement_max_iter)
        p.cornerRefinementMinAccuracy = float(cfg.corner_refinement_min_accuracy)
        self.params = p
        self.detector = cv2.aruco.ArucoDetector(self.dictionary, p)
        self._clahe = (
            cv2.createCLAHE(clipLimit=float(cfg.clahe_clip_limit), tileGridSize=(int(cfg.clahe_grid),) * 2)
            if cfg.clahe
            else None
        )

    # ------------------------------------------------------------------ #
    def _gray(self, frame: np.ndarray) -> np.ndarray:
        if frame.ndim == 3:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        else:
            gray = frame
        if self._clahe is not None:
            gray = self._clahe.apply(gray)
        return gray

    # ------------------------------------------------------------------ #
    def _raw_gray(self, frame: np.ndarray) -> np.ndarray:
        """不含 CLAHE 的原始灰度：亚像素精化必须用原始灰度，否则 CLAHE 会改变边缘形状。"""
        if frame.ndim == 3:
            return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return frame

    def detect(self, frame: np.ndarray) -> dict[int, MarkerObservation]:
        """返回 {marker_id: MarkerObservation}；同一 ID 在多尺度下重复时取最可靠的一个。"""
        raw = self._raw_gray(frame)
        gray = self._gray(frame)
        best: dict[int, MarkerObservation] = {}

        for scale in self.cfg.scales:
            scale = float(scale)
            if scale <= 0:
                continue
            if abs(scale - 1.0) < 1e-9:
                work = gray
            else:
                interp = cv2.INTER_LINEAR if scale > 1.0 else cv2.INTER_AREA
                work = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=interp)

            corners, ids, _ = self.detector.detectMarkers(work)
            if ids is None or len(ids) == 0:
                continue

            for quad, mid in zip(corners, ids.flatten()):
                mid = int(mid)
                pts = quad.reshape(4, 2).astype(np.float32)
                if abs(scale - 1.0) > 1e-9:
                    pts = pts / scale
                obs = MarkerObservation(marker_id=mid, corners=pts, scale=scale)
                prev = best.get(mid)
                if prev is None:
                    best[mid] = obs
                else:
                    # 1.0 尺度优先，其次取面积更大的
                    prev_key = (abs(prev.scale - 1.0) < 1e-9, prev.area)
                    new_key = (abs(obs.scale - 1.0) < 1e-9, obs.area)
                    if new_key > prev_key:
                        best[mid] = obs

        if self.cfg.edge_refine and best:
            self._edge_refine(raw, best)
        elif self.cfg.subpixel_polish and best:
            self._polish(gray, best)
        return best

    # ------------------------------------------------------------------ #
    def _edge_refine(self, gray: np.ndarray, obs: dict[int, MarkerObservation]) -> None:
        """边缘直线拟合亚像素精化（逐标记独立，失败则保留原四边形）。"""
        c = self.cfg
        for ob in obs.values():
            res = refine_quad_by_edges(
                gray,
                ob.corners.astype(np.float64),
                search=float(c.edge_search_px),
                step=float(c.edge_step_px),
                samples=int(c.edge_samples) or None,
                trim=float(c.edge_trim),
                iterations=int(c.edge_iterations),
                min_points=int(c.edge_min_points),
                clip_sigma=float(c.edge_clip_sigma),
                bright_outside=bool(c.edge_bright_outside),
                max_shift_ratio=float(c.edge_max_shift_ratio),
            )
            ob.edge_refined = bool(res.ok)
            if res.ok:
                ob.corners = res.quad.astype(np.float32)
                ob.line_scatter = float(np.mean(res.line_scatter)) if res.line_scatter else 0.0
                ob.refined = True

    # ------------------------------------------------------------------ #
    def _polish(self, gray: np.ndarray, obs: dict[int, MarkerObservation]) -> None:
        """对全部角点做一次全局亚像素精化。

        cornerSubPix 在窗口越界时会断言失败，因此靠近边界的角点直接跳过。
        """
        h, w = gray.shape[:2]
        win = int(self.cfg.subpixel_win)
        criteria = (
            cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER,
            int(self.cfg.subpixel_max_iter),
            float(self.cfg.subpixel_min_accuracy),
        )

        idx, pts = [], []
        for mid, ob in obs.items():
            for j in range(4):
                x, y = ob.corners[j]
                if win <= x < w - win and win <= y < h - win:
                    idx.append((mid, j))
                    pts.append([x, y])

        if not pts:
            return
        arr = np.asarray(pts, dtype=np.float32).reshape(-1, 1, 2)
        cv2.cornerSubPix(gray, arr, (win, win), (-1, -1), criteria)
        arr = arr.reshape(-1, 2)
        for (mid, j), pt in zip(idx, arr):
            obs[mid].corners[j] = pt
            obs[mid].refined = True
