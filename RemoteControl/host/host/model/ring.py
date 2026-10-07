"""数据模型: 线程安全的遥测环形缓冲与最新值快照。

读取线程往里写, Qt 主线程用定时器读 —— 所有访问都过锁,
所以两边都不需要跨线程信号。

v2.0(2026-10-05): **单流**。原先 fast/mid/slow 三条 RingBuffer 合并为一条
`unified`(通道表来自 `protocol.codec.UNIFIED_CHANNELS`, 单一来源防漂移);
dump/scope 的累积结构整套删除。
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional, Tuple

import numpy as np

from ..clock import now_monotonic
from ..protocol.codec import UNIFIED_CHANNELS, UNIFIED_HZ

#: 统一帧的通道顺序即 numpy 列顺序(与 codec 同源)
CHANNELS = list(UNIFIED_CHANNELS)

#: 时间锚(设备时基 ↔ 本机单调时钟)的容量上限 —— 只保留最近若干个,
#: 用于估算晶振漂移(见 api/timesync.py)。
ANCHOR_KEEP = 600


class RingBuffer:
    """定长环形缓冲。容量满后覆盖最旧数据。"""

    def __init__(self, channels: List[str], capacity: int, hz: float) -> None:
        self.channels = channels
        self.capacity = capacity
        self.hz = hz
        self.buf = np.zeros((capacity, len(channels)), dtype=np.float64)
        self.ts = np.zeros(capacity, dtype=np.float64)  # 宿主时间(秒)
        self.idx = 0
        self.count = 0
        self.total = 0
        self.lock = threading.Lock()

    def push(self, values: List[float], t: Optional[float] = None) -> None:
        if t is None:
            t = time.time()
        with self.lock:
            self.buf[self.idx] = values
            self.ts[self.idx] = t
            self.idx = (self.idx + 1) % self.capacity
            if self.count < self.capacity:
                self.count += 1
            self.total += 1

    def snapshot(self, seconds: Optional[float] = None) -> Tuple[np.ndarray, np.ndarray]:
        """按时间正序返回 (t, data)。seconds 给定时只取最近这么多秒。"""
        with self.lock:
            n = self.count
            if n == 0:
                return np.zeros(0), np.zeros((0, len(self.channels)))
            if n < self.capacity:
                t = self.ts[:n].copy()
                d = self.buf[:n].copy()
            else:
                i = self.idx
                t = np.concatenate([self.ts[i:], self.ts[:i]])
                d = np.concatenate([self.buf[i:], self.buf[:i]])
        if seconds is not None and len(t) > 1:
            t0 = t[-1] - seconds
            k = int(np.searchsorted(t, t0))
            t = t[k:]
            d = d[k:]
        return t, d

    def clear(self) -> None:
        with self.lock:
            self.idx = 0
            self.count = 0
            self.total = 0


class TelemetryStore:
    """单路遥测流 + 最新值 + 墙图 + 时间锚 + 事件序号连续性。"""

    def __init__(self, window_s: float = 30.0) -> None:
        self.unified = RingBuffer(CHANNELS, max(int(window_s * UNIFIED_HZ), 256), UNIFIED_HZ)

        self.lock = threading.Lock()
        self.latest_unified: Dict[str, float] = {}
        self.map_data: Optional[dict] = None
        self.mazetype: int = 7

        # ---- 时间锚: (本机单调秒, 设备 ms) ----
        #: 由 `TELEM` 的应答建立(带完整 u32 时基), 之后每帧的 ms8 靠同余还原。
        #: v2.1 起**每次拉取都会刷新锚**, 所以它不会陈旧, 失锁能立刻自愈。
        #: 用**单调时钟**记录到达时刻 —— 它绝不能受墙钟回拨影响。
        self.anchor: Optional[Tuple[float, int]] = None
        self.anchor_history: List[Tuple[float, int]] = []
        self.anchor_count = 0

        # ---- 上行消息序号连续性(见《方案》§8.3) ----
        #: 帧与事件行**共用**同一 1 字节计数器(固件 linkSeq)。于是主机看相邻两条
        #: 上行消息(帧或事件)的序号差 step = (n-prev)&0xFF:
        #:   - step >= 2 说明中间少了 step-1 条 → 事件流不完整(置标记)。
        #: 注意帧号与事件号共用计数器, 所以"帧号跳 1"是正常的, **不是**丢帧 ——
        #: 这也正是 v2.0 把丢帧统计从"逐帧比较"移到"逐条上行消息比较"的原因。
        self.up_last_seq: Optional[int] = None
        self.ev_last_seq: Optional[int] = None
        self.ev_gaps = 0       # 事件流出现缺口的次数
        self.ev_lost = 0       # 推算丢失的事件条数
        self.up_gaps = 0       # 相邻上行消息(帧或事件)出现缺口的次数
        self.up_lost = 0       # 推算丢失的上行消息条数(帧 + 事件合计)

        # 事件流(来自文本异步行)
        self.events: List[Tuple[float, dict]] = []

        self.on_map = None
        #: 每收到一帧统一帧就回调一次, 签名 cb(stream: str, rec: dict)。
        #: **在读取线程中触发**, 因此回调必须轻量(门面里只做"入队"/"落盘")。
        self.on_frame = None
        #: 每收到一条事件行回调一次, 签名 cb(rec: dict)。
        self.on_event = None

    # ------------------------------------------------------------ 写入

    def _notify(self, stream: str, rec: dict) -> None:
        cb = self.on_frame
        if cb is None:
            return
        try:
            cb(stream, rec)
        except Exception:
            pass  # 订阅者异常绝不能打断读取线程

    def push_unified(self, rec: dict) -> None:
        """统一帧: 唯一的数据流。"""
        self.unified.push([rec.get(c, 0) for c in CHANNELS])
        seq = rec.get("_seq")
        with self.lock:
            self.latest_unified = rec
            if seq is not None:
                self._track_up_seq(int(seq))
        self._notify("unified", rec)

    def _track_up_seq(self, seq: int) -> None:
        """相邻上行消息的序号连续性(调用时须已持有 self.lock)。"""
        prev = self.up_last_seq
        if prev is not None:
            step = (seq - prev) & 0xFF
            if step >= 2:
                self.up_gaps += 1
                self.up_lost += step - 1
        self.up_last_seq = seq

    def note_event_seq(self, seq: Optional[int], is_event: bool = True) -> None:
        """记录一条上行事件行的序号, 与帧走**同一套**连续性判定。

        序号与帧共用同一 1 字节计数器(设计方案 §8.3) ⇒ 主机看到的是一串
        "帧/事件交织"的统一编号。任何相邻两条之间的步进 >= 2 都意味着
        **中间少了消息**(可能是事件被 TX 空间门丢弃, 也可能是帧没收到/CRC 错)。
        这正是"只加序号、不做重发"能给出的最强保证: 丢失可检测, 但不可恢复。
        """
        if seq is None:
            return
        with self.lock:
            prev = self.up_last_seq
            step = 0 if prev is None else (int(seq) - prev) & 0xFF
            if prev is not None and step >= 2:
                self.up_gaps += 1
                self.up_lost += step - 1
                if is_event:
                    self.ev_gaps += 1
                    self.ev_lost += step - 1
            self.up_last_seq = int(seq)
            if is_event:
                self.ev_last_seq = int(seq)

    def push_event(self, rec: dict) -> None:
        with self.lock:
            self.events.append((time.time(), rec))
            if len(self.events) > 500:
                del self.events[:250]
            cb = self.on_event
        if cb is not None:
            try:
                cb(rec)
            except Exception:
                pass

    def push_map(self, rec: dict) -> None:
        with self.lock:
            self.map_data = rec
            self.mazetype = rec["mazetype"]
        cb = self.on_map
        if cb is not None:
            try:
                cb(rec)
            except Exception:
                pass
        self._notify("map", rec)

    # ------------------------------------------------------------ 时间锚

    def set_anchor(self, dev_ms: int, mono_t: Optional[float] = None) -> None:
        """记下一次"设备时基 ↔ 本机单调时钟"的对应(`TELEM` 的应答)。

        这不是"校准", 而是**同余还原的模基准**: 统一帧每帧只带 ms8(低 8 位),
        只靠它只能定位到 256ms 的窗口; 有了一个完整 u32 锚 + 本机粗估的流逝,
        就能把每帧唯一地还原到绝对设备时刻(见 api/timesync.py 的 resolve_ms8)。
        """
        t = float(mono_t if mono_t is not None else now_monotonic())
        with self.lock:
            self.anchor = (t, int(dev_ms))
            self.anchor_history.append((t, int(dev_ms)))
            if len(self.anchor_history) > ANCHOR_KEEP:
                del self.anchor_history[: len(self.anchor_history) - ANCHOR_KEEP]
            self.anchor_count += 1

    def get_anchor(self) -> Optional[Tuple[float, int]]:
        with self.lock:
            return self.anchor

    # ------------------------------------------------------------ 读

    def latest(self) -> Dict[str, float]:
        """统一帧的最新值(dict)。没有数据时为空 dict。"""
        with self.lock:
            return dict(self.latest_unified)

    def get_map(self) -> Optional[dict]:
        with self.lock:
            return self.map_data

    def event_health(self) -> dict:
        """事件/上行流完整性(序号缺口的累计统计)。

        `up_*` 覆盖**全部**上行消息(帧 + 事件), `ev_*` 只覆盖事件行 ——
        因为两者共用同一个 1 字节序号, 只有逐条比较才能判出"丢的是哪一种"。
        """
        with self.lock:
            return {
                "ev_last_seq": self.ev_last_seq,
                "ev_gaps": self.ev_gaps,
                "ev_lost": self.ev_lost,
                "up_gaps": self.up_gaps,
                "up_lost": self.up_lost,
                "up_last_seq": self.up_last_seq,
            }

    def clear(self) -> None:
        self.unified.clear()
        with self.lock:
            self.latest_unified = {}
            self.up_last_seq = None
            self.ev_last_seq = None
            self.ev_gaps = 0
            self.ev_lost = 0
            self.up_gaps = 0
            self.up_lost = 0
            self.anchor = None
            self.anchor_history = []
