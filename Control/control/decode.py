"""Y 平面帧解码（设计方案 §4.4）。

一个容易踩的坑：交付帧是**裸 Y 平面**（YUV_Y），尺寸由同名 `.json` 给出，而且
**行尾可能有填充**。硬编码 `reshape(H, W)` 在 stride≠W 时会产出一张
"形状对、内容错位"的图 —— 定位结果全错，而且不报任何错。

所以：按元数据切行，并且这条路径要有源码锚定的判据（I13）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np


class FrameMeta:
    """交付帧的元数据（`frame_XXXXXX.json`）。"""

    __slots__ = ("path", "json_path", "seq", "width", "height", "y_stride",
                 "source_row_stride", "repacked", "size_bytes", "sha256",
                 "wall_clock_ns", "sensor_timestamp_ns", "session_id",
                 "capture_path_id", "capture_path_rev", "raw")

    def __init__(self, d: Dict[str, Any], json_path: Path, yuv_path: Path) -> None:
        self.raw = d
        self.json_path = json_path
        self.path = yuv_path
        self.seq = int(d.get("seq") or 0)
        self.width = int(d.get("width") or 0)
        self.height = int(d.get("height") or 0)
        self.y_stride = int(d.get("y_stride") or self.width)
        self.source_row_stride = int(d.get("source_row_stride") or self.y_stride)
        self.repacked = bool(d.get("repacked", True))
        self.size_bytes = int(d.get("size_bytes") or 0)
        self.sha256 = d.get("sha256")
        self.wall_clock_ns = d.get("wall_clock_ns")
        self.sensor_timestamp_ns = d.get("sensor_timestamp_ns")
        self.session_id = d.get("session_id")
        self.capture_path_id = d.get("capture_path_id")
        self.capture_path_rev = d.get("capture_path_rev")

    @property
    def padded(self) -> bool:
        """行尾是否有填充（有填充就必须按 stride 切）。"""
        return (not self.repacked) or (self.y_stride != self.width)

    def __repr__(self) -> str:  # pragma: no cover
        return (f"FrameMeta(seq={self.seq}, {self.width}x{self.height}, "
                f"stride={self.y_stride}, repacked={self.repacked})")


def load_meta(json_path: str | Path) -> FrameMeta:
    """读帧元数据。**传 `.json` 或 `.yuv`/`.raw` 路径都可以** —— 给数据文件时会
    自动去找同名元数据（这个便利不是可有可无的：把 .yuv 当 json 去 json.loads
    会得到 `Expecting value: line 1 column 1`，那个报错完全指不到真正的问题）。"""
    jp = Path(json_path)
    if jp.suffix.lower() != ".json":
        jp = jp.with_suffix(".json")
    if not jp.is_file():
        raise FileNotFoundError(f"帧元数据不存在：{jp}")
    d = json.loads(jp.read_text(encoding="utf-8"))
    yuv = jp.with_suffix(".yuv")
    if not yuv.is_file():                       # 有的实现用 .raw
        alt = jp.with_suffix(".raw")
        if alt.is_file():
            yuv = alt
    return FrameMeta(d, jp, yuv)


def decode_y(path_or_meta: str | Path | FrameMeta,
             *, need_bgr: bool = True) -> Tuple[np.ndarray, FrameMeta]:
    """裸 Y 平面文件 → (灰度或 BGR 图, 元数据)。

    实现上**先按 stride 切成 (H, stride)，再取前 W 列**（对"无填充"情形这是恒等的），
    所以 `stride == W && repacked == False` 与"人工按 stride 切行"两条路径逐像素相同
    —— 这正是判据 I13 要钉死的性质。
    """
    meta = path_or_meta if isinstance(path_or_meta, FrameMeta) else load_meta(path_or_meta)
    raw = np.fromfile(str(meta.path), dtype=np.uint8)
    if meta.width <= 0 or meta.height <= 0:
        raise ValueError(f"{meta.json_path} 里没有 width/height，拒绝按猜测解码")
    need = meta.height * meta.y_stride
    if raw.size < need:
        raise ValueError(
            f"帧数据不完整：{meta.path} 有 {raw.size} 字节，但按 height={meta.height} × "
            f"y_stride={meta.y_stride} 需要 {need} 字节。该文件已损坏，不能用于定位。")
    gray = raw[:need].reshape(meta.height, meta.y_stride)[:, :meta.width]
    gray = np.ascontiguousarray(gray)
    if not need_bgr:
        return gray, meta
    import cv2
    bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    return bgr, meta


def resize_for_locate(bgr: np.ndarray, target_w: int = 0) -> Tuple[np.ndarray, float]:
    """把图降到定位档（`downsample_w > 0` 时）。返回 (图, 缩放比)。

    缩放比是**面积意义上的一致缩放**：x 与 y 同比例，所以归一化坐标不受影响。
    """
    if target_w and 0 < target_w < bgr.shape[1]:
        import cv2
        scale = float(target_w) / float(bgr.shape[1])
        return cv2.resize(bgr, (int(target_w), int(round(bgr.shape[0] * scale))),
                          interpolation=cv2.INTER_AREA), scale
    return bgr, 1.0


def frames_in(camera_session_dir: str | Path) -> list[Path]:
    """列出一个手机会话里**已交付**的帧元数据文件（按 seq 排序）。

    这里刻意只列 `.json`：PC 侧的铁律是"不采信自报"，而帧是否在场由
    **元数据文件 + 数据文件同时在**判定，而不是由 App 说"我传完了"。
    """
    d = Path(camera_session_dir) / "frames"
    if not d.is_dir():
        return []
    out = [p for p in d.glob("frame_*.json")
           if p.with_suffix(".yuv").is_file() or p.with_suffix(".raw").is_file()]
    out.sort(key=lambda p: p.name)
    return out
