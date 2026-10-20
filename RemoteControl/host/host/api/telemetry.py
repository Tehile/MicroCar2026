"""遥测读取: 最新快照、环形历史、吞吐统计。

对应功能清单的 B 组。

v2.0(2026-10-05): **单流**。原先 fast/mid/slow 三路合并为一路上行流
(`unified`), 固件**自时钟**发送 —— 无 Hz 上限, 帧率由 115200-8-N-1 的物理
能力决定(53 B/帧 → 约 217 Hz)。因此"实时曲线只够看趋势"的说法不再成立:
统一帧本身就是全量快照, 不再需要停车后整块回传。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import numpy as np

from ..model.ring import CHANNELS, RingBuffer, UNIFIED_HZ

if TYPE_CHECKING:  # pragma: no cover
    from .client import MouseClient

#: 唯一的遥测流名(保留 `name` 参数只为旧调用点少改一行)
STREAM = "unified"

_FLAG_KEYS = (
    "freemode", "sleep", "emerg", "pullf1", "irSwitch",
    "motors_off", "estop_lock", "evq",
)


class TelemetryAPI:
    """只读遥测。所有方法无副作用, 可随时调用。"""

    def __init__(self, mc: "MouseClient") -> None:
        self._mc = mc

    # ------------------------------------------------------------ 快照

    def latest(self) -> Dict[str, Any]:
        """统一帧的最新值(扁平 dict)。没有数据时为空 dict。"""
        return self._mc.store.latest()

    def buffer(self, name: str = STREAM) -> RingBuffer:
        rb = getattr(self._mc.store, name, None)
        if not isinstance(rb, RingBuffer):
            raise KeyError(f"未知的遥测流: {name!r} (v2.0 只有 {STREAM!r})")
        return rb

    def channels(self, name: str = STREAM) -> List[str]:
        return list(CHANNELS)

    def hz(self, name: str = STREAM) -> float:
        """**实测**帧率(Hz)。

        v2.1 起遥测是**拉取式**的(PC 循环发 `TELEM`), 帧率由链路实测能力与请求窗口
        共同决定, 不再是固定的 ≈217 Hz —— 所以这里按最近 2 秒的到达间隔实算,
        没有数据时返回 0。想看拉取器自身的窗口/丢失统计用 `mc.stream.stats()`。
        """
        t, _ = self.buffer(name).snapshot(seconds=2.0)
        if len(t) < 2:
            return 0.0
        dt = float(t[-1]) - float(t[0])
        return (len(t) - 1) / dt if dt > 1e-9 else 0.0

    # ------------------------------------------------------------ 历史

    def history(self, name: str = STREAM, seconds: Optional[float] = None):
        """返回 `(t_seconds_epoch, data[行=样本, 列=通道])`。"""
        return self.buffer(name).snapshot(seconds)

    def history_dict(self, name: str = STREAM, seconds: Optional[float] = None) -> Dict[str, np.ndarray]:
        """把历史拆成 `{通道名: 一维数组}`。"""
        t, data = self.history(name, seconds)
        out: Dict[str, np.ndarray] = {"t": t}
        for i, c in enumerate(CHANNELS):
            out[c] = data[:, i] if len(data) else np.zeros(0)
        return out

    def samples(self, name: str = STREAM) -> int:
        return int(self.buffer(name).total)

    # ------------------------------------------------------------ 状态

    def last_frame_age(self, name: str = STREAM) -> Optional[float]:
        """最后一帧距今多少秒。None = 从未收到过。"""
        t, _ = self.history(name)
        if len(t) == 0:
            return None
        return max(0.0, time.time() - float(t[-1]))

    def is_streaming(self, max_age: float = 2.5) -> bool:
        """是否正在收到数据。

        v2.1 起遥测是**拉取式**的: 由 `mc.stream.on()` 启动本机拉取循环
        (见 `api/stream.py`), 停止拉取就自然没有帧。判据的时限按**链路实测**
        给 —— 这条蓝牙链路的往返约 1.2 s, 帧到达间隔远大于 v2.0 设想的 4.6 ms,
        所以用量级更宽的 2.5 s, 否则正常拉取也会被判成"没在流"。
        """
        age = self.last_frame_age(STREAM)
        return age is not None and age <= max_age

    def wait_first_frame(self, timeout: float = 5.0) -> bool:
        """等第一帧统一帧到达(连上后最常用的同步点)。"""
        return self._mc.wait(lambda: bool(self.latest()), timeout=timeout)

    def flags(self) -> Dict[str, bool]:
        """统一帧里的状态位。"""
        f = self.latest()
        return {k: bool(f.get(k)) for k in _FLAG_KEYS} if f else {}

    def event_health(self) -> Dict[str, Any]:
        """上行事件流的完整性统计(序号缺口)。"""
        return self._mc.store.event_health()

    # ------------------------------------------------------------ 统计

    def stats(self) -> Dict[str, Any]:
        return self._mc.link.stats()

    def self_check(self) -> Dict[str, Any]:
        return self._mc.link.self_check()

    def bytes_per_second(self) -> float:
        return float(self._mc.link.tp.bps)

    def summary_text(self) -> str:
        """一行摘要(CLI 与日志用)。"""
        f = self.latest()
        if not f:
            return "遥测: 无数据 (未在拉取, 先 mc.stream.on())"
        bits = [
            f"L {f.get('speedL', 0):+.0f}  R {f.get('speedR', 0):+.0f} cm/s",
            f"θ {f.get('gyroAngle', 0):+.0f}°",
            f"{f.get('battMv', 0) / 1000:.2f}V",
            f"task={f.get('motionTaskName', f.get('motionTask'))}",
        ]
        fl = self.flags()
        if fl.get("motors_off"):
            bits.append("安全模式(电机禁用)")
        elif fl.get("emerg"):
            bits.append("急停")
        elif fl.get("sleep"):
            bits.append("休眠")
        if fl.get("evq"):
            bits.append("事件流不完整")
        bits.append(f"({f.get('posX')},{f.get('posY')}) {f.get('mazeTaskName', '')} "
                    f"{f.get('dirName', '')} {f.get('roadName', '')}")
        return "  ".join(bits)

    def live(self, seconds: float = 5.0, interval: float = 0.5, printer=print) -> None:
        """在终端里持续打印状态行(CLI/调试用)。"""
        t0 = time.time()
        while time.time() - t0 < seconds:
            printer(self.summary_text())
            time.sleep(max(0.05, interval))
