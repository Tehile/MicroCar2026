"""事件总线: 读取线程 / 调度线程 / 调用者线程 之间唯一的通道。

线程契约(重要, 违反会自锁或丢事件):
    emit()   任意线程可调(读取线程、调度线程、调用者线程), **只入队不派发**
    pump()   只应由调度线程调用: 排空队列并派发给订阅者
    订阅回调 总在**调度线程**执行 —— 因此回调里不得调用会阻塞等待设备的 API
             (门面会检测并抛 ReentrancyError)
    需要把事件搬回自己的线程(例如 Qt 主线程)时, 用 queue_view() 拿一个
             线程安全队列, 在那边消费。

载荷统一为 dict: 便于 --json 输出、JSON-RPC 序列化, 以及"新增功能不改事件结构"。
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Deque, Dict, List, Optional


class Event(str, Enum):
    """事件种类。值是字符串, 便于跨进程与 JSON 使用。"""

    ANY = "*"  # 订阅全部
    STATE = "state"  # 链路状态变更          data: {state, detail}
    ERROR = "error"  # 打开/读/解码错误       data: {kind, msg}
    TELEMETRY = "telemetry"  # 一帧遥测      data: {stream, rec}
    TEXT = "text"  # 设备文本(含 printf)     data: {line, direction}
    PARAM = "param"  # 参数回读值            data: {id, name, q}
    CALIB = "calib"  # 标定异步消息          data: {kind, fields, raw}
    MOVE = "move"  # 运动任务结束            data: {kind, fields, raw}
    #                kind="done"(线路终止/正常跑完) / "stopped"(统一帧时代的 STOPPED)
    ESTOP = "estop"  # 设备侧急停触发          data: {kind, fields, raw}
    LESSON = "lesson"  # 课程实验异步消息       data: {kind, fields, raw}
    #                    kind="DONE"(实验结束) / "STATUS"(状态查询回话)
    EVENT = "event"  # 统一上行事件行          data: {event, reason, seq, text, raw}
    #                 v2.0: 只剩 `STOPPED <reason> #n` / `ESTOP TRIP #n`
    TIMESYNC = "timesync"  # 时间同步异步消息    data: {kind, fields, raw}
    #                        kind="anchor"(时基锚) / "armed"(发车) / "lost"(失锁)
    MAP = "map"  # 墙图                       data: {mazetype, posX, posY, ...}
    ADVICE = "advice"  # 链路诊断            data: {level, got_bps, need_bps, note}
    SESSION = "session"  # 会话开关/落盘        data: {op, path}


@dataclass
class EventMsg:
    ts: float
    kind: str
    data: Dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"EventMsg({self.ts:.3f}, {self.kind}, {self.data!r})"


class EventBus:
    """线程安全事件总线。emit 任意线程, pump 只在调度线程。"""

    def __init__(self, maxlen: int = 8192, recent: int = 500) -> None:
        self._q: Deque[EventMsg] = deque()
        self._lock = threading.Lock()
        self._subs: Dict[str, List[Callable[[EventMsg], None]]] = {}
        self._recent: Deque[EventMsg] = deque(maxlen=recent)
        self.maxlen = maxlen
        self.dropped = 0
        self.emitted = 0

    # ------------------------------------------------------------ 订阅

    def on(self, kind: "Event | str", cb: Callable[[EventMsg], None]) -> Callable[[], None]:
        """订阅。返回一个注销函数。"""
        key = _key(kind)
        with self._lock:
            self._subs.setdefault(key, []).append(cb)

        def _off() -> None:
            with self._lock:
                try:
                    self._subs.get(key, []).remove(cb)
                except ValueError:
                    pass

        return _off

    def queue_view(self, maxsize: Optional[int] = None) -> "queue.Queue":
        """返回一个线程安全队列, 所有事件都会投递进去。

        用途: GUI 在自己的线程(如 Qt 主线程)拉取 —— 本方法**不依赖任何 GUI 框架**,
        只是标准库 queue。
        """
        q: "queue.Queue" = queue.Queue(maxsize=maxsize or self.maxlen)

        def _put(msg: EventMsg) -> None:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass

        self.on(Event.ANY, _put)
        return q

    # ------------------------------------------------------------ 投递

    def emit(self, kind: "Event | str", data: Optional[Dict[str, Any]] = None) -> None:
        """任意线程可调。队列满时丢**最旧**的一条并计数, 不阻塞生产者。"""
        msg = EventMsg(time.time(), _key(kind), data or {})
        with self._lock:
            if len(self._q) >= self.maxlen:
                self._q.popleft()
                self.dropped += 1
            self._q.append(msg)
            self.emitted += 1
            self._recent.append(msg)

    def pump(self) -> List[EventMsg]:
        """排空队列并派发给订阅者。**只由调度线程调用。** 返回本次取出的消息。"""
        with self._lock:
            if not self._q:
                return []
            msgs = list(self._q)
            self._q.clear()
            subs = {k: list(v) for k, v in self._subs.items()}
        any_subs = subs.get("*", [])
        for m in msgs:
            for cb in subs.get(m.kind, ()):
                _safe(cb, m)
            for cb in any_subs:
                _safe(cb, m)
        return msgs

    def recent(self, n: int = 50) -> List[EventMsg]:
        with self._lock:
            return list(self._recent)[-n:]

    def clear(self) -> None:
        with self._lock:
            self._q.clear()
            self._recent.clear()


def _key(kind: "Event | str") -> str:
    return kind.value if isinstance(kind, Event) else str(kind)


def _safe(cb: Callable[[EventMsg], None], msg: EventMsg) -> None:
    try:
        cb(msg)
    except Exception:
        pass  # 一个订阅者出错不能影响其它订阅者与调度线程
