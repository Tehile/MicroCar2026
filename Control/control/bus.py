"""统一事件总线：多源 → 一个 WS 广播 + 一个二进制通道。

线程模型（§7.3）：
    · 鼠侧事件来自 MouseClient 的调度线程 / 读取线程
    · 视频会话的状态来自 `VideoAdapter._watch()` 自己的等待线程
    · 二者都是"非 asyncio 线程"，所以 `publish()` 必须**线程安全**：
      统一走 `loop.call_soon_threadsafe`，绝不在别的线程里直接碰 asyncio.Queue。

二进制通道（§6.2）与 JSON 通道分开：**payload > 64 KB 必须走二进制**（判据 I10）。
这不是优化，是正确性 —— 250 Hz × 9 通道用 JSON 会把浏览器直接卡死。
"""

from __future__ import annotations

import asyncio
import json
import struct
import threading
from collections import deque
from typing import Any, Deque, Dict, Iterable, List, Optional, Set, Tuple

#: JSON 帧的载荷上限。超过就必须走二进制（判据 I10 的阈值）。
JSON_PAYLOAD_LIMIT = 64 * 1024

#: 日志来源（**唯一真相**）—— 前端底栏按它分类，不许在别处另写一份。
#: 顺序 = 界面上显示的先后（与子系统顺序一致：鼠 / 视频复盘 / 本层）。
#: ⚠️ 改版二十四：`camera` / `locate` 两个来源随手机相机下线 —— 留着的话前端会多出
#: 两个**永远筛不出内容**的死选项，那比没有更让人困惑（前端 SRC_DEF 已同步）。
SOURCES = ("mouse", "video", "control")
_SOURCES = set(SOURCES)


class Bus:
    """事件广播中枢。"""

    def __init__(self, loop: Optional[asyncio.AbstractEventLoop] = None,
                 log_ring: int = 800) -> None:
        self.loop = loop
        self._subs: Set[asyncio.Queue] = set()
        self._bin_subs: Set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self._log: Deque[dict] = deque(maxlen=max(50, int(log_ring)))
        self._seq = 0

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    # ------------------------------------------------------------ 订阅
    def subscribe(self, maxsize: int = 2000) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subs.add(q)
        return q

    def subscribe_bin(self, maxsize: int = 64) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._bin_subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)
        self._bin_subs.discard(q)

    # ------------------------------------------------------------ 发布
    def log(self, level: str, text: str, source: str = "control", **extra: Any) -> None:
        """写一条服务日志（进环形缓冲，并可被前端拉取）。同时广播为 `log` 事件。

        `source`：这条信息**是从哪来的** —— 前端底栏按它分类（见 `SOURCES`）。
        取值只能是三个之一：
          · `"mouse"`   电脑鼠（RemoteControl，含那条串口/TCP 链路）
          · `"video"`   视频复盘（Auto Locate 会话解析，含子进程的起停与报错）
          · `"control"` 本层（整合层自己的调度、配置、会话、收尾）
        默认 `"control"`：**本层自己的调度信息**是多数（其余两类都发生在
        "取上游一个值"的地方，那些地方要**显式**声明来源）。
        ⚠️ 不许在这里猜来源（比如按文案里有"视频"就归视频）——
        猜错会让现场"照着分类去找"时找错地方；不确定就归 `control` 并如实写清。
        """
        src = source if source in _SOURCES else "control"
        rec = {"type": "log", "level": level, "text": text,
               "source": src, "t_pc_ms": _now_ms(), **extra}
        with self._lock:
            self._log.append(rec)
        self.publish("log", rec, log=False)

    def tail(self, n: int = 200) -> List[dict]:
        with self._lock:
            items = list(self._log)
        return items[-max(1, int(n)):]

    def publish(self, topic: str, payload: Dict[str, Any], *, log: bool = False) -> None:
        """线程安全地广播一条 JSON 事件。可在任意线程调用。"""
        msg = dict(payload)
        msg.setdefault("type", topic)
        msg.setdefault("t_pc_ms", _now_ms())
        self._enqueue(self._subs, msg)
        if log:
            with self._lock:
                self._log.append(msg)

    def publish_bin(self, meta: Dict[str, Any], payload: bytes) -> None:
        """线程安全地广播一条二进制帧（大数组专用）。"""
        with self._lock:
            self._seq += 1
            m = dict(meta)
            m.setdefault("type", "bin")
            m["payload_bytes"] = len(payload)
            m["seq"] = self._seq
            m.setdefault("t_pc_ms", _now_ms())
        for q in list(self._bin_subs):
            self._safe_put(q, (m, payload))

    # ------------------------------------------------------------ 内部
    def _enqueue(self, subs: Iterable[asyncio.Queue], msg: dict) -> None:
        for q in list(subs):
            self._safe_put(q, msg)

    def _safe_put(self, q: asyncio.Queue, item: Any) -> None:
        """把 item 投进 asyncio.Queue，必要时跨线程调度。

        满了就**丢最旧**而不是阻塞 —— 上游的教训（§4.2）：在别人的消息循环里阻塞，
        等于把那条循环的生死交出去。丢帧必须显式计数，不能静默。
        """
        def _put() -> None:
            while True:
                try:
                    q.put_nowait(item)
                    return
                except asyncio.QueueFull:
                    try:
                        q.get_nowait()          # 丢最旧
                        drop = getattr(q, "_control_dropped", 0) + 1
                        setattr(q, "_control_dropped", drop)
                    except asyncio.QueueEmpty:
                        return

        loop = self.loop
        if loop is None or not loop.is_running():
            return                       # 还没进入事件循环：丢弃（不是错误）
        try:
            if threading.current_thread() is threading.main_thread() and \
                    asyncio.get_running_loop() is loop:
                _put()
            else:
                loop.call_soon_threadsafe(_put)
        except RuntimeError:
            pass                          # 循环正在关闭


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


# ---------------------------------------------------------------- 编解码（双端契约）

_BIN_HEADER_LEN = struct.Struct("<I")


def encode_bin(meta: Dict[str, Any], payload: bytes) -> bytes:
    """`[4B uint32 header_len][header JSON][payload]` —— 见设计方案 §6.2。"""
    h = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _BIN_HEADER_LEN.pack(len(h)) + h + payload


def decode_bin(blob: bytes) -> Tuple[dict, bytes]:
    (hl,) = _BIN_HEADER_LEN.unpack_from(blob, 0)
    meta = json.loads(blob[4:4 + hl].decode("utf-8"))
    return meta, blob[4 + hl:]


def f32(payload: List[float]) -> bytes:
    import numpy as np
    return np.asarray(payload, dtype="<f4").tobytes()


def f32_2d(rows: Iterable[Iterable[float]]) -> bytes:
    import numpy as np
    return np.asarray(rows, dtype="<f4").tobytes()
