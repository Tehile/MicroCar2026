"""输入源：单张图片 / 图片目录 / 视频文件 / 摄像头，统一成同一条帧迭代接口。

Windows 下 cv2.imread / imwrite 无法处理非 ASCII 路径（本项目路径就含中文），
因此这里统一走 np.fromfile + imdecode / imencode + tofile。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Iterator

import cv2
import numpy as np

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".pgm", ".ppm"}
VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".wmv", ".m4v", ".mpg", ".mpeg", ".flv", ".ts"}


@dataclass
class FrameItem:
    index: int
    frame: np.ndarray
    timestamp: float  # 秒
    label: str = ""


# --------------------------------------------------------------------------- #
# 非 ASCII 路径读写
# --------------------------------------------------------------------------- #
def imread_unicode(path: str, flags: int = cv2.IMREAD_COLOR) -> np.ndarray | None:
    try:
        data = np.fromfile(path, dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, flags)


def imwrite_unicode(path: str, image: np.ndarray) -> bool:
    ext = os.path.splitext(path)[1] or ".png"
    ok, buf = cv2.imencode(ext, image)
    if not ok:
        return False
    buf.tofile(path)
    return True


# --------------------------------------------------------------------------- #
# 源解析
# --------------------------------------------------------------------------- #
def parse_source(source) -> tuple[str, str]:
    """返回 (kind, value)，kind ∈ image | directory | video | camera。"""
    if isinstance(source, int):
        return "camera", str(source)
    s = str(source).strip()
    m = re.fullmatch(r"(?:camera|cam|dev)[:=]?(\d*)", s, flags=re.IGNORECASE)
    if m:
        return "camera", m.group(1) or "0"
    if os.path.isdir(s):
        return "directory", s
    ext = os.path.splitext(s)[1].lower()
    if ext in VIDEO_EXTS:
        return "video", s
    if ext in IMAGE_EXTS:
        return "image", s
    if os.path.isfile(s):
        return "video", s
    raise FileNotFoundError(f"无法识别的输入源：{source!r}")


def _natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def list_images(directory: str, recursive: bool = False) -> list[str]:
    out = []
    if recursive:
        for root, _, files in os.walk(directory):
            for f in files:
                if os.path.splitext(f)[1].lower() in IMAGE_EXTS:
                    out.append(os.path.join(root, f))
    else:
        for f in os.listdir(directory):
            p = os.path.join(directory, f)
            if os.path.isfile(p) and os.path.splitext(f)[1].lower() in IMAGE_EXTS:
                out.append(p)
    out.sort(key=lambda p: _natural_key(os.path.basename(p)))
    return out


def open_source(source, camera_width: int | None = None, camera_height: int | None = None):
    """打开视频/摄像头，返回 cv2.VideoCapture。"""
    kind, value = parse_source(source)
    if kind == "camera":
        idx = int(value)
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(idx)
        if not cap.isOpened():
            cap = cv2.VideoCapture(idx)
        if camera_width:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(camera_width))
        if camera_height:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(camera_height))
        if not cap.isOpened():
            raise RuntimeError(f"打不开摄像头 {idx}")
        return cap
    if kind == "video":
        cap = cv2.VideoCapture(value)
        if not cap.isOpened():
            raise RuntimeError(f"打不开视频文件 {value}")
        return cap
    raise ValueError(f"{kind} 不是流式输入源")


def iter_source(
    source,
    start: int = 0,
    max_frames: int | None = None,
    recursive: bool = False,
    camera_width: int | None = None,
    camera_height: int | None = None,
) -> Iterator[FrameItem]:
    """统一帧迭代：图片 / 目录 / 视频 / 摄像头。"""
    kind, value = parse_source(source)

    if kind == "image":
        frame = imread_unicode(value)
        if frame is None:
            raise RuntimeError(f"读不到图片：{value}")
        yield FrameItem(index=0, frame=frame, timestamp=0.0, label=os.path.basename(value))
        return

    if kind in ("video", "camera"):
        cap = open_source(source, camera_width, camera_height)
        is_cam = kind == "camera"
        idx = -1
        try:
            while True:
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                idx += 1
                if idx < start:
                    continue
                ts = time.time() if is_cam else cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
                yield FrameItem(index=idx, frame=frame, timestamp=float(ts), label=value)
                if max_frames is not None and (idx - start + 1) >= max_frames:
                    break
        finally:
            cap.release()
        return

    # directory
    files = list_images(value, recursive=recursive)
    total = len(files)
    count = 0
    for i, path in enumerate(files):
        if i < start:
            continue
        frame = imread_unicode(path)
        if frame is None:
            continue
        yield FrameItem(index=i, frame=frame, timestamp=float(i), label=os.path.basename(path))
        count += 1
        if max_frames is not None and count >= max_frames:
            break
    if total == 0:
        raise RuntimeError(f"目录里没有图片：{value}")
