"""链路层: 串口 / TCP 双模, 后台读取线程, 帧与文本分流, 吞吐实测。

双模的意义: 没有硬件时可以把上位机接到 sim/mouse_sim.py(TCP) 上跑通全链路,
有硬件时切换为串口, 上层代码一行不用改。

线程模型:
    读取线程   读字节 -> 写 raw.bin -> FrameStreamParser.feed() -> 分发
               二进制帧 -> TelemetryStore; 文本行 -> CommandClient.feed_line()
    Qt 主线程  定时器轮询 TelemetryStore 重绘; 发命令走 CommandClient

所有共享结构自带锁, 因此不需要跨线程信号。
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from ..clock import now_monotonic
from ..model.ring import TelemetryStore
from ..protocol.codec import (
    FRAME_OVERHEAD,
    UNIFIED_HZ,
    UNIFIED_LEN,
    Frame,
    FrameStreamParser,
    parse_event_line,
)
from ..protocol.commands import CommandClient
from ..registry import frame_spec


class LinkState:
    CLOSED = "closed"
    CONNECTING = "connecting"
    OPEN = "open"
    ERROR = "error"


@dataclass
class Target:
    """连接目标。kind ∈ {"serial", "tcp", "replay"}"""

    kind: str
    port: str = ""
    baud: int = 115200
    host: str = "127.0.0.1"
    tcp_port: int = 5555
    path: str = ""

    def label(self) -> str:
        if self.kind == "tcp":
            return f"TCP {self.host}:{self.tcp_port}"
        if self.kind == "replay":
            return f"回放 {self.path}"
        return f"{self.port} @ {self.baud}"


def explain_open_error(target: "Target", err: Exception) -> str:
    """把底层异常翻译成能直接照做的中文诊断。

    这几类错误在实机上各自对应一个完全不同的处理动作, 混成一句
    "could not open port" 会让排查时间成倍增加, 所以分开说明。
    """
    raw = f"{type(err).__name__}: {err}"
    low = raw.lower()
    who = target.port or target.label()

    if "拒绝访问" in raw or "permissionerror" in low or "access is denied" in low:
        return (f"{who} 被占用 —— 端口已被其它程序打开"
                f"(串口助手/另一个上位机/Keil 的串口窗口)。请关闭后重试。 [{raw}]")

    if "信号灯超时" in raw or ", 121)" in raw or "semaphore" in low:
        return (f"{who} 蓝牙链路未建立 —— Windows 试图连接该端口绑定的设备但超时。"
                f"请确认电脑鼠已上电、蓝牙已连接(不只是配对), 且不在休眠。 [{raw}]")

    if "file not found" in low or "系统找不到" in raw or "不存在" in raw:
        return f"{who} 不存在 —— 端口可能已被移除(拔线/蓝牙掉线), 请点『刷新』重新枚举。 [{raw}]"

    if "busy" in low or "占用" in raw:
        return f"{who} 忙 —— 端口被占用, 请关闭其它串口程序。 [{raw}]"

    return f"打开 {who} 失败: {raw}"


# ---------------------------------------------------------------- 后端

class _SerialBackend:
    def __init__(self, port: str, baud: int) -> None:
        import serial  # 延迟导入, 使无 pyserial 时仍可用 TCP

        self._ser = serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=0.05,
            write_timeout=0.5,
        )

    def read(self, n: int) -> bytes:
        try:
            return self._ser.read(n) or b""
        except Exception:
            return b""

    def write(self, data: bytes) -> None:
        self._ser.write(data)

    def close(self) -> None:
        try:
            self._ser.close()
        except Exception:
            pass


class _TcpBackend:
    def __init__(self, host: str, port: int) -> None:
        self._sock = socket.create_connection((host, port), timeout=3.0)
        self._sock.settimeout(0.05)
        self._recv = bytearray()

    def read(self, n: int) -> bytes:
        try:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("peer closed")
        except socket.timeout:
            return b""
        except Exception:
            raise
        self._recv += chunk
        if len(self._recv) >= n:
            out = bytes(self._recv[:n])
            del self._recv[:n]
            return out
        out = bytes(self._recv)
        self._recv.clear()
        return out

    def write(self, data: bytes) -> None:
        self._sock.sendall(data)

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 吞吐统计

class Throughput:
    """滑动窗口吞吐实测。用于 §5.8 的链路自检与自适应降级。"""

    def __init__(self, window_s: float = 2.0) -> None:
        self.window_s = window_s
        self.lock = threading.Lock()
        self._pts: List[Tuple[float, int]] = []

    def add(self, nbytes: int) -> None:
        now = time.time()
        with self.lock:
            self._pts.append((now, nbytes))
            cutoff = now - self.window_s
            while self._pts and self._pts[0][0] < cutoff:
                self._pts.pop(0)

    @property
    def bps(self) -> float:
        now = time.time()
        with self.lock:
            cutoff = now - self.window_s
            pts = [p for p in self._pts if p[0] >= cutoff]
            if len(pts) < 2:
                return 0.0
            span = max(now - pts[0][0], 1e-3)
            total = sum(p[1] for p in pts)
        return total / span

    def reset(self) -> None:
        with self.lock:
            self._pts.clear()


# ---------------------------------------------------------------- Link

class Link:
    """链路: 打开/关闭、后台读取、帧分发、吞吐统计、自动重连。"""

    def __init__(
        self,
        store: TelemetryStore,
        client: CommandClient,
        on_state: Optional[Callable[[str, str], None]] = None,
        on_session_raw: Optional[Callable[[bytes], None]] = None,
    ) -> None:
        self.store = store
        self.client = client
        self.on_state = on_state
        self.on_session_raw = on_session_raw

        self._backend = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.state = LinkState.CLOSED
        self.state_detail = ""
        self.target: Optional[Target] = None
        self.parser = FrameStreamParser()
        self.tp = Throughput()
        self.counts: Dict[str, int] = {
            "unified": 0,  # 统一遥测帧(唯一周期帧)
            "map": 0,      # 墙图整块回传帧
            "text": 0,     # 文本/事件行总数
            "event": 0,    # 其中被识别为上行事件的行(STOPPED / ESTOP TRIP)
            "bad_crc": 0,
            "unknown_frame": 0,
            "seq_gap": 0,  # 上行消息序号出现缺口的次数(见下)
            "seq_lost": 0,  # 累计推算丢掉的上行消息条数(不是"次数"而是"条数")
        }
        #: 丢序号检测的**唯一真源是 `TelemetryStore`**, 不是本类。
        #: 为什么: v2.0 起帧与事件行**共用**同一个 1 字节序号(固件 linkSeq),
        #: 所以"相邻两帧序号差 != 1"是**正常**的(中间可能插了事件行) ——
        #: 早期在这里逐帧比较的写法会把它误判成丢帧。只有同时看见帧与事件的
        #: store 才能正确判定, 故这里只做"把 store 的计数抄进 counts"这一步。
        self.auto_reconnect = True
        self._last_rx = 0.0

    # ------------------------------------------------------------ 状态

    def _set_state(self, st: str, detail: str = "") -> None:
        self.state = st
        self.state_detail = detail
        if self.on_state:
            try:
                self.on_state(st, detail)
            except Exception:
                pass

    # ------------------------------------------------------------ 打开/关闭

    def open(self, target: Target) -> bool:
        """打开链路。返回是否成功 —— 调用方必须据此决定 UI 状态,
        不要像早期版本那样无论成败都切成"已连接"。"""
        self.close()
        self.target = target
        self._stop.clear()
        self._set_state(LinkState.CONNECTING, target.label())
        try:
            if target.kind == "tcp":
                self._backend = _TcpBackend(target.host, target.tcp_port)
            elif target.kind == "replay":
                from ..replay import ReplayBackend

                self._backend = ReplayBackend(target.path)
            else:
                self._backend = _SerialBackend(target.port, target.baud)
        except Exception as e:
            self._backend = None
            self._set_state(LinkState.ERROR, explain_open_error(target, e))
            return False
        self.parser = FrameStreamParser()
        self.tp.reset()
        self._sync_seq_counts()  # 新连接: 序号连续性从零开始建立
        self._thread = threading.Thread(target=self._reader_loop, name="link-reader", daemon=True)
        self._thread.start()
        self._set_state(LinkState.OPEN, target.label())
        return True

    def close(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1.0)
        self._thread = None
        with self._lock:
            if self._backend is not None:
                try:
                    self._backend.close()
                except Exception:
                    pass
                self._backend = None
        if self.state != LinkState.ERROR:
            self._set_state(LinkState.CLOSED)

    @property
    def is_open(self) -> bool:
        return self.state == LinkState.OPEN and self._backend is not None

    # ------------------------------------------------------------ 发送

    def write(self, data: bytes) -> bool:
        b = self._backend
        if b is None:
            return False
        try:
            b.write(data)
            return True
        except Exception as e:
            self._set_state(LinkState.ERROR, f"write: {e}")
            return False

    # ------------------------------------------------------------ 读取循环

    def _reader_loop(self) -> None:
        while not self._stop.is_set():
            b = self._backend
            if b is None:
                break
            try:
                data = b.read(256)
            except Exception as e:
                self._set_state(LinkState.ERROR, f"read: {e}")
                break
            if not data:
                # 空闲超时; 若长时间无数据且目标已断开, TCP 后端内部会抛异常
                if self._last_rx and (time.time() - self._last_rx) > 5.0:
                    pass  # 保留连接, 蓝牙空闲是正常的
                continue
            self._last_rx = time.time()
            self.tp.add(len(data))
            if self.on_session_raw:
                try:
                    self.on_session_raw(data)
                except Exception:
                    pass
            # ⚠️ **按到达顺序逐个处理** —— 不能用 split_items 拆成两个列表再分别跑。
            # v2.0 起帧与事件行共用一个 1 字节序号, 交错顺序就是协议的一部分;
            # 拆开会让"帧, 事件, 帧"变成"帧, 帧, 事件", 序号看起来就跳了。
            for item in self.parser.feed(data):
                if isinstance(item, Frame):
                    self._dispatch_frame(item)
                    continue
                self.counts["text"] += 1
                if parse_event_line(item) is not None:
                    self.counts["event"] += 1
                self.client.feed_line(item)
                # 事件行的序号由 client 的异步回调记进 store(见 api/client.py),
                # 所以抄计数必须放在 feed_line **之后**。
                self._sync_seq_counts()

    def _sync_seq_counts(self) -> None:
        """把 store 的序号完整性计数抄进 `counts`(单一真源在 store, 见 __init__)。"""
        h = self.store.event_health()
        self.counts["seq_gap"] = int(h["up_gaps"])
        self.counts["seq_lost"] = int(h["up_lost"])

    def _dispatch_frame(self, f) -> None:
        """帧分派走 `registry.FRAMES` —— 单一登记点, 新增帧不改本文件。

        未登记的帧类型**静默忽略并计数**, 不置 ERROR: 固件加了新帧而主机尚未
        升级时, 丢掉这一种帧比让整条链路停机合理(前向兼容)。
        """
        spec = frame_spec(f.type)
        if spec is None:
            self.counts["unknown_frame"] = self.counts.get("unknown_frame", 0) + 1
            return
        try:
            rec = spec.decoder(f.payload)
        except Exception as e:
            self._set_state(LinkState.ERROR, f"decode {spec.name}: {e}")
            return
        # 给记录附上"到达时刻"与"帧序号", 让它们**跟着数据走**:
        #   _rx_mono  单调时钟秒 —— 时间同步的锚点(不能用墙钟, 会受 NTP 回拨影响)
        #   _seq      1 字节上行序号 —— 离线也能复核丢失(在线计数见 counts["seq_lost"])
        # 这两个键不参与 RingBuffer 取值(它按显式通道名取), 也不影响 CSV 列, 只是搭车。
        rec["_rx_mono"] = now_monotonic()
        rec["_seq"] = f.seq
        try:
            # sink 与 count 都取自登记表, 因此这里没有针对具体帧类型的任何分支
            getattr(self.store, spec.sink)(rec)
        except Exception as e:
            self._set_state(LinkState.ERROR, f"sink {spec.sink}: {e}")
            return
        if spec.count:
            self.counts[spec.count] = self.counts.get(spec.count, 0) + 1
        self._sync_seq_counts()

    # ------------------------------------------------------------ 统计

    def stats(self) -> Dict[str, float]:
        s = dict(self.counts)
        s["rx_bps"] = self.tp.bps
        s["bad_crc"] = self.parser.stats["bad_crc"]
        s["bad_sync"] = self.parser.stats["bad_sync"]
        s["frames"] = self.parser.stats["frames"]
        return s

    # v2.0 起固件**自时钟发送**: 无 Hz 上限, 帧率由链路能力决定。
    # 理论上限 = 115200-8-N-1 = 11520 B/s; v9 一帧 38 B(30 载荷 + 8 开销)
    # → 额定 ≈303 Hz / ≈11514 B/s(v8 是 57 B / ≈202 Hz)。
    _THEORETICAL_BPS = 11520.0
    _FRAME_BYTES = FRAME_OVERHEAD + UNIFIED_LEN  # 38 (v9)

    def self_check(self) -> Dict[str, object]:
        """链路自检(仅**诊断**, 不再给出"降级速率"建议)。

        v1.5.5 的 self_check 建立在"可持续吞吐只有 152 B/s"的假设上, 据此反推
        `RATE` 降级值 —— 该假设**已在真机上被证伪**(SCOPE DUMP 实测 1.38 KB/s
        未堵死), 且 v2.0 已删除 `RATE` 命令, 故本方法只回答一个问题:
        "实测吞吐离物理天花板有多远"。低 = 链路/波特率/驱动有问题, 与固件无关。
        """
        need = self._FRAME_BYTES * UNIFIED_HZ  # ≈11501 B/s
        got = self.tp.bps
        if got <= 1.0:
            return {"need_bps": need, "got_bps": got, "level": "unknown",
                    "suggest_rate": None, "note": "无数据(未开 STREAM 或链路空闲)"}
        ratio = got / self._THEORETICAL_BPS
        if ratio >= 0.70:
            return {"need_bps": need, "got_bps": got, "level": "ok",
                    "suggest_rate": None, "note": ""}
        return {
            "need_bps": need, "got_bps": got, "level": "degraded", "suggest_rate": None,
            "note": (f"实测 {got:.0f} B/s, 仅为物理上限的 {ratio * 100:.0f}% —— "
                     "检查蓝牙信号/串口驱动; 固件自时钟, 无需也不能下发速率"),
        }
