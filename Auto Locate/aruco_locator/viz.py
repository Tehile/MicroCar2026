"""可视化：在画面叠加检测与解算结果，并附一个俯视小窗。

注意：OpenCV 的 putText 只能画 ASCII，所以叠加文字一律用英文缩写。
"""

from __future__ import annotations

import cv2
import numpy as np

from .geometry import MARKER_LOCAL, apply_homography

# BGR
C_LINE = (120, 235, 120)  # 角标记/场地边框
C_AXIS = (230, 200, 60)  # 场地坐标轴
C_TARGET = (0, 165, 255)  # 0 号标记
C_OK = (120, 235, 120)
C_BAD = (80, 80, 235)
C_TEXT = (240, 240, 240)
C_DIM = (170, 170, 170)


def _frame_to_field(H, pts):
    return apply_homography(H, pts)


def draw_overlay(frame: np.ndarray, loc, obs: dict, cfg, debug: dict | None = None) -> np.ndarray:
    """返回叠加后的新图。"""
    out = frame.copy()
    H = (debug or {}).get("H")
    H_inv = (debug or {}).get("H_inv")

    # 1) 检出的标记
    for mid, ob in obs.items():
        pts = ob.corners.astype(np.int32).reshape(-1, 1, 2)
        is_target = mid == cfg.detection.target_id
        color = C_TARGET if is_target else C_LINE
        cv2.polylines(out, [pts], True, color, 2, cv2.LINE_AA)
        c = ob.center
        cv2.circle(out, (int(c[0]), int(c[1])), 3, color, -1, cv2.LINE_AA)
        label = f"id{mid}"
        cv2.putText(out, label, (int(c[0]) + 6, int(c[1]) - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA)

    # 2) 场地边框、原点、坐标轴
    #    只看 H / H_inv：格点匹配通道（locate_grid）不建 frame_obj，但同样有 H，
    #    场地框与坐标轴一样要画出来，否则第二通道的审阅视频没有参照。
    if H is not None and H_inv is not None:
        square = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=np.float64)
        sq_img = apply_homography(H_inv, square).astype(np.int32)
        cv2.polylines(out, [sq_img.reshape(-1, 1, 2)], True, C_LINE, 1, cv2.LINE_AA)

        origin_img = apply_homography(H_inv, np.array([[0.0, 0.0]]))[0]
        o = (int(origin_img[0]), int(origin_img[1]))
        cv2.circle(out, o, 5, C_AXIS, -1, cv2.LINE_AA)
        cv2.putText(out, "origin", (o[0] + 8, o[1] + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, C_AXIS, 1, cv2.LINE_AA)

        axis_len = 0.25
        for vec, name in ((np.array([axis_len, 0.0]), "+x"), (np.array([0.0, axis_len]), "+y")):
            tip = apply_homography(H_inv, vec.reshape(1, 2))[0]
            cv2.arrowedLine(out, o, (int(tip[0]), int(tip[1])), C_AXIS, 2, cv2.LINE_AA, tipLength=0.15)
            cv2.putText(
                out, name, (int(tip[0]) + 4, int(tip[1]) + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.5, C_AXIS, 1, cv2.LINE_AA
            )

        # 3) 0 号标记的中心与方向（把场地系方向映射回图像）
        if loc.valid and loc.x is not None:
            c_field = np.array([[loc.x, loc.y]])
            c_img = apply_homography(H_inv, c_field)[0]
            tip_field = c_field + np.array([[loc.dir_x or 0.0, loc.dir_y or 0.0]]) * 0.12
            tip_img = apply_homography(H_inv, tip_field)[0]
            cv2.circle(out, (int(c_img[0]), int(c_img[1])), 4, C_TARGET, -1, cv2.LINE_AA)
            cv2.arrowedLine(
                out,
                (int(c_img[0]), int(c_img[1])),
                (int(tip_img[0]), int(tip_img[1])),
                C_TARGET,
                3,
                cv2.LINE_AA,
                tipLength=0.25,
            )

    # 4) 文字面板
    lines = _hud_lines(loc)
    y = 24
    for text, color in lines:
        cv2.putText(out, text, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(out, text, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1, cv2.LINE_AA)
        y += 22

    # 5) 俯视小窗
    if H is not None and debug:
        panel = draw_field_panel(loc, obs, cfg, H)
        if panel is not None:
            out = paste_panel(out, panel)
    return out


def _hud_lines(loc) -> list[tuple[str, tuple]]:
    if not loc.valid:
        return [
            (f"frame {loc.frame_index}  INVALID", C_BAD),
            (f"reason: {loc.reason}", C_BAD),
        ]
    rms = "n/a" if loc.rms_field is None else f"{loc.rms_field:.4f}"
    trms = "n/a" if loc.target_rms is None else f"{loc.target_rms:.4f}"
    lines = [
        (f"frame {loc.frame_index}  valid", C_OK),
        (f"x = {loc.x:.4f}   y = {loc.y:.4f}", C_TEXT),
        (f"theta = {loc.theta_deg:+.2f} deg", C_TEXT),
        (f"dir = ({loc.dir_x:+.3f}, {loc.dir_y:+.3f})", C_TEXT),
        (f"rms = {rms}  tgt_rms = {trms}", C_DIM),
        (f"origin id = {loc.origin_id}   corners = {loc.corner_ids}", C_DIM),
    ]
    g = (loc.diagnostics or {}).get("grid")
    if g:
        lines.append((f"grid gap={g['gap']:+.3f} sharp={g['sharp']:.3f} "
                      f"n_bad={g['n_bad']} beta={g['beta']:.0f}", C_DIM))
    if loc.flags:
        lines.append((f"flags: {','.join(loc.flags)}", C_AXIS))
    return lines


def draw_field_panel(loc, obs: dict, cfg, H, size: int = 340, margin: int = 26) -> np.ndarray | None:
    """俯视小窗：把场地摆正后画出各标记的场地位置与 0 号方向。"""
    panel = np.full((size, size, 3), 28, dtype=np.uint8)
    ax = size - 2 * margin
    bg = (60, 60, 60)

    def to_px(p):
        x = margin + p[0] * ax
        y = size - margin - p[1] * ax  # 场地 +y 向上
        return int(round(x)), int(round(y))

    # 网格
    for k in range(5):
        t = k / 4.0
        p0, p1 = to_px((t, 0.0)), to_px((t, 1.0))
        cv2.line(panel, p0, p1, bg, 1)
        p0, p1 = to_px((0.0, t)), to_px((1.0, t))
        cv2.line(panel, p0, p1, bg, 1)

    # 场地边框
    sq = np.array([to_px((0, 0)), to_px((1, 0)), to_px((1, 1)), to_px((0, 1))], dtype=np.int32)
    cv2.polylines(panel, [sq.reshape(-1, 1, 2)], True, C_LINE, 2, cv2.LINE_AA)

    # 各标记的真实形状（把四角映射到场地系再画）
    for mid, ob in obs.items():
        mapped = np.array([to_px(p) for p in apply_homography(H, ob.corners)], dtype=np.int32)
        color = C_TARGET if mid == cfg.detection.target_id else C_DIM
        cv2.polylines(panel, [mapped.reshape(-1, 1, 2)], True, color, 2, cv2.LINE_AA)
        c = ob.center
        fc = apply_homography(H, c.reshape(1, 2))[0]
        cv2.putText(
            panel,
            str(mid),
            (to_px(fc)[0] - 5, to_px(fc)[1] + 5),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            color,
            1,
            cv2.LINE_AA,
        )

    # 坐标轴与 0 号方向
    o = to_px((0.0, 0.0))
    cv2.arrowedLine(panel, o, to_px((0.22, 0.0)), C_AXIS, 2, cv2.LINE_AA, tipLength=0.2)
    cv2.arrowedLine(panel, o, to_px((0.0, 0.22)), C_AXIS, 2, cv2.LINE_AA, tipLength=0.2)
    cv2.putText(panel, "+x", to_px((0.24, 0.0)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, C_AXIS, 1, cv2.LINE_AA)
    cv2.putText(panel, "+y", to_px((0.02, 0.24)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, C_AXIS, 1, cv2.LINE_AA)

    if loc.valid and loc.x is not None:
        c = to_px((loc.x, loc.y))
        tip = to_px((loc.x + (loc.dir_x or 0) * 0.09, loc.y + (loc.dir_y or 0) * 0.09))
        cv2.arrowedLine(panel, c, tip, C_TARGET, 3, cv2.LINE_AA, tipLength=0.3)
        cv2.circle(panel, c, 4, C_TARGET, -1, cv2.LINE_AA)

    cv2.putText(panel, "field view (top-down)", (margin, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, C_TEXT, 1, cv2.LINE_AA)
    return panel


def paste_panel(frame: np.ndarray, panel: np.ndarray, scale: float = 0.34) -> np.ndarray:
    """把俯视小窗缩放到画面高度的一定比例，贴到右上角。"""
    h, w = frame.shape[:2]
    target = max(120, int(min(h, w) * scale))
    small = cv2.resize(panel, (target, target), interpolation=cv2.INTER_AREA)
    m = 10
    y0, x0 = m, w - target - m
    if y0 + target > h or x0 < 0:
        return frame
    roi = frame[y0 : y0 + target, x0 : x0 + target]
    cv2.addWeighted(small, 0.82, roi, 0.18, 0, roi)
    cv2.rectangle(frame, (x0, y0), (x0 + target, y0 + target), (200, 200, 200), 1)
    return frame
