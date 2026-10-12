"""统一时钟 —— 数据链路里"算时间差"一律用单调时钟。

为什么单独开一个模块: 原工程的**所有**时间戳都取自 `time.time()`(墙钟), 而墙钟会被
NTP 回拨/跳变。对"实时波形窗口"这类只是显示时间轴的场景无所谓, 但对
**时间同步**(把设备时基映射到 PC 时刻, 再算每帧的准确时间)是致命的 ——
一次回拨就会让映射倒挂, 丢掉的数据时间再也算不回来。

因此约定:

    now_monotonic()  任何"差值/时长/映射"计算
    now_wall_ms()    只有"给人看的时间戳"(文件名、CSV 里的可读时间)才用

参见 `docs/算法详解.md` §3（时间同步）。
"""

from __future__ import annotations

import time


def now_monotonic() -> float:
    """单调秒。不受系统时间调整影响, 适合做时长与偏移量计算。"""
    return time.monotonic()


def now_wall_ms() -> int:
    """墙钟毫秒(epoch)。仅用于人类可读的时间标记, **不要**用它算差。"""
    return int(time.time() * 1000)
