"""上位机门面 MouseClient。

把原先散落在 `ui/main_window.py` 里的编排逻辑收拢到一处，使"用完整功能"
= 调 API，而不是"读三种源码再拼一遍"。

被收拢进来的四件事（原先都在 MainWindow）：

    1. 20ms 调度 `CommandClient.poll()`  —— 没有它，超时/重发机制完全失效，
       每条命令都会静静地等到 TIMEOUT
    2. 事件分发：设备异步消息、链路状态、遥测帧统一进事件总线
    3. 会话落盘：**每收到一帧写一行**。原先用 40Hz 定时器读"最新快照"，
       高采样率下同一轮询周期里的多帧只有最后一帧能落盘（会漏数据）
    4. 连接业务规则：连上自动**开始拉取**(mc.stream.on)、断开前先停拉取、
       打开失败不建会话目录

v2.0(2026-10-05): **单流**。上行只有"持续数据流(统一帧) + 异常事件(STOPPED/ESTOP TRIP)"。
`SNAP` / `SCOPE` / `DUMP` 三个机制整体删除, 对应的子 API 一并取消。

线程模型::

    读取线程 (Link)    read -> raw.bin -> parser -> store / client
                      store.on_frame 回调里**只入队**，不做重活
    调度线程 (本类)     poll / 事件派发 / 落盘 / 周期任务
    调用者线程          调 API；wait=True 时阻塞等待

事件订阅回调**运行在调度线程**，因此回调里不得调用会阻塞等待设备的方法
（会与调度线程自锁）。误用会抛 `ReentrancyError`，并由门面转成一个 ERROR
事件送到事件流里，不会静默。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, List, Optional

from ..model.ring import TelemetryStore
from ..model.session import Session
from ..protocol.codec import parse_event_line, q1616_to_float
from ..protocol.commands import CommandClient, CommandError
from ..registry import command_line, spec_of
from ..transport.link import Link, LinkState, Target, explain_open_error
from .analyze import AnalyzeAPI
from .calib import CalibAPI
from .conn import ConnAPI
from .errors import Result, ReentrancyError, clamp_timeout
from .events import Event, EventBus, EventMsg
from .lab import MotorLabAPI
from .maze import MazeAPI
from .move import MoveAPI
from .params import ParamsAPI
from .session_api import SessionAPI
from .stream import StreamAPI
from .telemetry import TelemetryAPI
from .timesync import TimeSyncAPI

__all__ = ["MouseClient", "ReentrancyError", "Result", "clamp_timeout"]


# ReentrancyError / Result / clamp_timeout 定义在 api/errors.py ——
# 单独成文件是为了让子 API 不必 import 本模块(否则循环导入)。


_CTOR_KEYS = (
    "session_dir",
    "auto_session",
    "auto_stream",
    "scheduler_hz",
    "telemetry_window_s",
    "auto_reconnect",
)


class MouseClient:
    """电脑鼠上位机门面。非 GUI、线程安全、可阻塞。"""

    def __init__(
        self,
        session_dir: str = "sessions",
        auto_session: bool = True,
        auto_stream: bool = True,
        scheduler_hz: float = 50.0,
        telemetry_window_s: float = 30.0,
        auto_reconnect: bool = True,
    ) -> None:
        self.session_dir = session_dir
        self.auto_session = auto_session
        self.auto_stream = auto_stream
        self.auto_reconnect = auto_reconnect
        self.scheduler_hz = max(5.0, float(scheduler_hz))

        # ---- 机制层 ----
        self.store = TelemetryStore(window_s=telemetry_window_s)
        self.cmd = CommandClient(send_bytes=self._send_bytes)
        self.link = Link(
            store=self.store,
            client=self.cmd,
            on_state=self._on_link_state,
            on_session_raw=self._on_session_raw,
        )
        self.link.auto_reconnect = auto_reconnect

        # ---- 事件总线 ----
        self.bus = EventBus()
        self.store.on_frame = self._on_frame  # 读取线程: 只入队
        self.cmd.on_async = self._on_async_line  # 读取线程: 解析 + 入队

        # ---- 会话 ----
        self._session: Optional[Session] = None
        self._session_notes: List[str] = []

        # ---- 调度线程 ----
        self._sched_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._tick_depth = 0
        self._last_advice: Optional[tuple] = None
        self._last_conn_error: str = ""

        # ---- 子 API ----
        self.conn = ConnAPI(self)
        self.params = ParamsAPI(self)
        self.calib = CalibAPI(self)
        self.move = MoveAPI(self)
        self.maze = MazeAPI(self)
        self.lab = MotorLabAPI(self)
        self.timesync = TimeSyncAPI(self)
        self.analyze = AnalyzeAPI(self)
        self.session = SessionAPI(self)
        self.stream = StreamAPI(self)
        self.telemetry = TelemetryAPI(self)

        self._start_scheduler()

    # ============================================================ 调度线程

    def _start_scheduler(self) -> None:
        if self._sched_thread is not None:
            return
        self._stop.clear()
        t = threading.Thread(target=self._scheduler_loop, name="mousectl-sched", daemon=True)
        self._sched_thread = t
        t.start()

    def _scheduler_loop(self) -> None:
        interval = 1.0 / self.scheduler_hz
        while not self._stop.wait(interval):
            try:
                self._tick()
            except Exception as e:  # 调度线程绝不能死
                self.bus.emit(Event.ERROR, {"kind": "scheduler", "msg": f"{type(e).__name__}: {e}"})

    def _tick(self) -> None:
        self._tick_depth += 1
        try:
            # 1) 超时与重发。没有这一步, 命令永远等不到 ACK 重发。
            self.cmd.poll()
            # 2) 派发给订阅者, 同时拿到这一批消息用于落盘
            msgs = self.bus.pump()
            # 3) 会话落盘: 逐帧写, 不是读最新快照
            self._persist(msgs)
            # 4) 周期任务: 链路诊断
            self._periodic_advice()
            # 5) 拉取器: 收割在途请求 -> 按窗口补发 -> 自适应窗口深度。
            #    必须放在 bs.pump() 之后 —— Pending 的完成回调由 pump 触发,
            #    先补窗才能把"刚腾出来的槽位"当拍用掉, 否则吞吐会掉一档。
            #    v2.10: `stream.tick()` 委派唯一的统一拉取器 `TelemetryPuller`
            #    (n=1 停等 TELEM / n≥2 批式 RATE PULL, 一个对象两种剖面)。
            self.stream.tick()
        finally:
            self._tick_depth -= 1

    # ============================================================ 数据入口(读取线程)

    def _on_frame(self, stream: str, rec: dict) -> None:
        """TelemetryStore 每收到一帧就回调。**运行在读取线程**, 只入队。"""
        if stream == "map":
            # 墙图单独走 MAP 事件: 它不是"遥测流", 混进去会干扰订阅者与落盘
            self.bus.emit(Event.MAP, {"rec": rec})
            return
        # 时间锚还原: 把 ms8 还原成完整设备时基, 供落盘/UI 使用(很轻, 只是查表)。
        try:
            self.timesync.note_frame(rec)
        except Exception:
            pass
        self.bus.emit(Event.TELEMETRY, {"stream": stream, "rec": rec})

    def _on_link_state(self, state: str, detail: str) -> None:
        self.bus.emit(Event.STATE, {"state": state, "detail": detail})
        if state == LinkState.ERROR:
            self._last_conn_error = detail
            self.bus.emit(Event.ERROR, {"kind": "link", "msg": detail})

    def _on_session_raw(self, data: bytes) -> None:
        sess = self._session
        if sess is not None:
            sess.write_raw(data)

    # ------------------------------------------------------------ 异步文本行

    def _on_async_line(self, line: str) -> None:
        """设备主动发的文本行(非 ACK)。在读取线程里做分类并投事件。"""
        head = line.split(None, 1)[0] if line else ""

        # ---- 上行事件(v2.0 只剩这两条): `STOPPED <reason> #n` / `ESTOP TRIP #n` ----
        ev = parse_event_line(line)
        if ev is not None:
            # 序号连续性: 事件与遥测帧**共用**同一个 1 字节计数器, 所以与帧走同一套判定。
            self.store.note_event_seq(ev.get("seq"))
            self.store.push_event(ev)
            kind = Event.ESTOP if ev["event"] == "ESTOP TRIP" else Event.EVENT
            data = dict(ev)
            if ev["event"] == "ESTOP TRIP":
                data["kind"] = "trip"
            else:
                data["kind"] = "stopped"
                data.setdefault("fields", [ev.get("reason", "")])
            self.bus.emit(kind, data)
            self.bus.emit(Event.TEXT, {"line": line, "direction": "<-", "kind": head})
            return

        # v2.4: 这里原来还有一个 `head == "LESSON"` 分支(课程调参实验的异步行
        # `LESSON DONE ...` / `LESSON STATUS ...`)。课程层整体删除后固件不再发这些行,
        # 分支与 `Event.LESSON` 一并移除。
        self.bus.emit(Event.TEXT, {"line": line, "direction": "<-", "kind": head})

    # ============================================================ 连接

    def connect(self, target: "Any" = None, baud: Optional[int] = None, **kw) -> bool:
        """打开链路。返回是否成功 —— 失败时**不建会话、不开流**，原因见事件流。"""
        t = coerce_target(target, baud=baud, **kw)
        self._last_advice = None
        self._last_conn_error = ""
        # 新连接: 设备时基从头开始(固件上电/重连重置), 旧的观测样本与锚必须清掉。
        self.timesync.reset()
        self.store.clear()
        ok = self.link.open(t)
        if not ok:
            # 立刻把失败原因推给订阅者与 recent(), 不等下一个 tick
            self.bus.pump()
            return False
        if self.auto_session:
            self.open_session()
        # ★ 先取设备指纹, 再开流 —— 这两个的顺序不能反。
        #   固件每 1ms tick 只消费一行命令(严格停等, 在途 ≤1): 若 PING 与拉取器的
        #   首条 `TELEM` 背靠背发出, 后发的 PING 会被丢弃 ⇒ device 只能等
        #   `TIMEOUT_S`(2.5s) 超时重发才拿到, 而 monitor 顶栏与 `reset_cause` 是在
        #   连接后**立刻**读 `mc.device` 的(真机实测: 前 ~2.5s 恒为空)。
        #   用 wait=True 自适应等应答(真机实测 RTT≈300ms)取代原先写死的
        #   `sleep(0.05)` —— 那个值短于真机 RTT, 即使 PING 没丢也读不到 PONG。
        try:
            self.cmd.send("PING", wait=True, timeout=self.cmd.TIMEOUT_S)
        except CommandError:
            # 指纹拿不到不应阻断连接; 后续 `send_cmd("ping")` 或自动重发会补齐。
            pass
        self.bus.pump()
        if self.auto_stream:
            # v2.1 拉取模式: 不在固件侧"开流", 而是**本机开始循环请求** TELEM。
            # 非阻塞 —— 首发一条请求就返回, 应答里的完整 u32 时基作为第一次时间锚
            # (之后每次拉取都会刷新它)。见 host/api/stream.py 的说明。
            self.stream.on()
        return True

    def _absorb_anchor(self, ok: bool, status: str) -> None:
        """`TELEM` 应答 `OK <upMs>` -> 写入时间锚(运行在读取线程)。"""
        if not ok:
            return
        parts = str(status or "").split()
        if len(parts) >= 2 and parts[0].upper() == "OK":
            try:
                self.store.set_anchor(int(parts[1]))
                self.bus.emit(Event.TIMESYNC, {"kind": "anchor", "seq": None,
                                               "dev_ms": int(parts[1]), "raw": status})
            except ValueError:
                pass

    def reanchor(self, timeout: float = 3.0) -> Result:
        """重新索要时间锚(注入一条 `TELEM`, 应答即带完整 u32 时基)。

        v2.10 起是真动作: 批式模式下也会注入(旧实现是 `stream.on()` ——
        已在跑时是空操作, 且会把批式切成停等)。
        """
        return self.stream.refresh_anchor(timeout=timeout)

    def disconnect(self) -> None:
        """断开链路。**先停拉取** —— 这是让蓝牙模块缓冲排空的唯一手段。

        v2.1 起"停流"完全发生在主机侧(固件没有开流状态): 不再请求 => 设备不再发送
        => 缓冲自然排空。所以这里**不发任何命令**, 只是把拉取器关掉。
        """
        if self.stream.is_on:
            try:
                self.stream.off()
            except Exception:
                pass
        self.link.close()
        self.bus.pump()
        if self.auto_session:
            self.close_session()

    def close(self) -> None:
        """收尾: 断开 + 关会话 + 停调度线程。"""
        try:
            self.disconnect()
        finally:
            self._stop.set()
            t = self._sched_thread
            if t is not None and t.is_alive() and t is not threading.current_thread():
                t.join(timeout=1.0)
            self._sched_thread = None
            self.bus.pump()

    def __enter__(self) -> "MouseClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @classmethod
    @contextmanager
    def connect_to(cls, target: Any = None, **kwargs):
        """`with MouseClient.connect_to("COM7") as mc:` —— 退出时自动断开并收会话。"""
        ctor = {k: v for k, v in kwargs.items() if k in _CTOR_KEYS}
        conn = {k: v for k, v in kwargs.items() if k not in _CTOR_KEYS}
        mc = cls(**ctor)
        if not mc.connect(target, **conn):
            detail = mc.state_detail or "未知原因"
            mc.close()
            raise ConnectionError(f"无法连接 {target!r}: {detail}")
        try:
            yield mc
        finally:
            mc.close()

    # ------------------------------------------------------------ 状态

    @property
    def is_connected(self) -> bool:
        """链路是否已连通(`LinkState.OPEN` 且后端已建立)。"""
        return self.link.is_open

    @property
    def state(self) -> str:
        """链路状态字符串(`LinkState`): `closed`/`connecting`/`open`/`error`。"""
        return self.link.state

    @property
    def state_detail(self) -> str:
        """状态的人读补充: 失败原因/最近一次连接错误; 为空串表示无附加信息。"""
        return self.link.state_detail or self._last_conn_error

    @property
    def device(self) -> Dict[str, Any]:
        """设备自述信息(PING 的回话): proto / fw / mazetype / txbuf / rst。"""
        return dict(self.cmd.device)

    @property
    def reset_cause(self) -> Optional[str]:
        """上次复位原因(POR/SW/IWDG/PIN), 来自 PING 应答。未知时 None。"""
        v = self.cmd.device.get("rst")
        return str(v) if v else None

    @property
    def motor_disabled(self) -> Optional[bool]:
        """设备是否处于『电机已禁用』安全测试模式。

        `True`  = 固件带 `MOUSE_TEST_NO_MOTOR`（电机输出恒 0，物理上不会动）
        `False` = 正常比赛固件
        `None`  = 还没收到帧，未知

        依据是统一帧状态位 `LINK_FLAG_MOTOR_OFF`，它是固件**编译期**事实的镜像，
        不是"当前速度恰好为 0"这种运行时巧合。
        """
        f = self.store.latest()
        if not f:
            return None
        return bool(f.get("motors_off", False))

    def motor_guard(self, what: str) -> "Optional[Result]":
        """需要电机转动的前置检查。

        返回 `None` 表示可以继续；否则返回一个 `MOTOR_DISABLED` 的失败 Result。

        为什么要有它: 安全模式下 `MOVE FWD` / `CAL GYRORATIO` 这类动作的固件侧
        状态机照常推进、也会照常回 ACK，只是电机不转 —— 于是调用方会一直等到
        超时（角度标定默认 90s）才失败，且失败原因看起来像"设备没反应"。
        这里提前用编译期标志判定，把 90 秒的困惑换成一行的明确原因。

        底层 `send_cmd()` / `calib.step()` 不受影响 —— 需要单纯验证命令通路时
        请直接用它们。
        """
        if self.motor_disabled:
            return Result(
                False,
                "MOTOR_DISABLED",
                {
                    "op": what,
                    "hint": (
                        "设备运行的是安全测试固件(主开关 MOUSE_TEST_NO_MOTOR=1), "
                        "电机输出恒为 0, 本动作不会产生任何物理运动。"
                        "要做电机实验请改用 MOUSE_TEST_MOTOR_LAB 并重新编译烧录。"
                    ),
                },
            )
        return None

    @property
    def estop_locked(self) -> Optional[bool]:
        """设备是否处于『急停锁存』状态(帧 flags bit6)。

        `True`  = 急停已触发并锁死: 新的 MOVE/CAL 会被固件拒绝(`ERR_ESTOP`)
        `False` = 未锁存
        `None`  = 还没收到帧, 未知

        注意它与 `flags['emerg']` 的区别: `emerg` 是 `emergMon.active` 的镜像(会随
        STOP 清零), `estop_lock` 是链路层自己的锁存位。**只有 STOP 能解**。
        """
        f = self.store.latest()
        if not f:
            return None
        return bool(f.get("estop_lock", False))

    @property
    def events_incomplete(self) -> Optional[bool]:
        """事件流是否出现过丢弃(帧 flags bit7 = EVQ)。None = 还没收到帧。"""
        f = self.store.latest()
        if not f:
            return None
        return bool(f.get("evq", False))

    def event_health(self) -> Dict[str, Any]:
        """上行消息序号连续性的累计统计(丢帧/丢事件的可检测证据)。"""
        return self.store.event_health()

    # ============================================================ 事件

    def on(self, kind: "Event | str", cb: Callable[[EventMsg], None]) -> Callable[[], None]:
        """订阅事件。回调在**调度线程**执行(见模块 docstring 的线程契约)。"""

        def _wrapped(msg: EventMsg) -> None:
            try:
                cb(msg)
            except ReentrancyError as e:
                self.bus.emit(Event.ERROR, {"kind": "reentrancy", "msg": str(e)})
            except Exception as e:
                self.bus.emit(Event.ERROR, {"kind": "callback", "msg": f"{type(e).__name__}: {e}"})

        return self.bus.on(kind, _wrapped)

    def events_queue(self, maxsize: Optional[int] = None):
        """拿到一个线程安全队列, 所有事件都会投递进去。

        给"想在别的线程消费事件"的场景用(例如 GUI 在 Qt 主线程拉取)。
        不依赖任何 GUI 框架。
        """
        return self.bus.queue_view(maxsize)

    def recent_events(self, n: int = 50) -> List[EventMsg]:
        return self.bus.recent(n)

    # ============================================================ 阻塞辅助

    def _check_reentrancy(self, what: str) -> None:
        t = self._sched_thread
        if t is not None and threading.current_thread() is t:
            raise ReentrancyError(
                f"不能在事件回调里调用阻塞方法 {what}: 回调运行在调度线程上, "
                f"会与它自锁。请改用 wait=False 并订阅相应事件。"
            )

    def wait(self, pred: Callable[[], Any], timeout: float = 10.0, interval: float = 0.01) -> bool:
        """阻塞直到 `pred()` 为真或超时。脚本写同步代码用。"""
        self._check_reentrancy("wait()")
        deadline = time.time() + clamp_timeout(timeout, 10.0)
        while time.time() < deadline:
            try:
                if pred():
                    return True
            except Exception:
                pass
            time.sleep(interval)
        return False

    def wait_event(
        self, kind: "Event | str", pred: Optional[Callable[[EventMsg], bool]] = None,
        timeout: float = 10.0, since: Optional[float] = None,
    ) -> Optional[EventMsg]:
        """阻塞直到出现满足条件的事件, 返回它; 超时返回 None。"""
        self._check_reentrancy("wait_event()")
        key = kind.value if isinstance(kind, Event) else str(kind)
        t0 = since if since is not None else time.time()
        hits: List[EventMsg] = []

        def _cb(msg: EventMsg) -> None:
            if pred is None or pred(msg):
                hits.append(msg)

        off = self.bus.on(key, _cb)
        try:
            deadline = time.time() + clamp_timeout(timeout, 10.0)
            while time.time() < deadline:
                if hits:
                    return hits[0]
                # 也回看最近的, 避免"事件在订阅之前刚到"
                for m in self.bus.recent(200):
                    if m.ts >= t0 and m.kind == key and (pred is None or pred(m)):
                        return m
                time.sleep(0.01)
            return hits[0] if hits else None
        finally:
            off()

    # ============================================================ 命令执行

    def _send_bytes(self, data: bytes) -> None:
        if not self.link.write(data):
            self.bus.emit(Event.ERROR, {"kind": "write", "msg": "链路未打开, 发送被丢弃"})

    def _wait_pending(self, p, timeout: float) -> bool:
        deadline = time.time() + clamp_timeout(timeout, 3.0)
        while time.time() < deadline:
            if p.done:
                return True
            time.sleep(0.005)
        # 超时收尾: 对不重发的命令(no_retry)这是唯一会把 Pending 摘掉的地方,
        # 必须交给命令层清表, 否则该 Pending 会永久留在 _pending 里。
        self.cmd.abandon(p)
        return False

    def send_cmd(self, cmd: str, timeout: Optional[float] = None, **kw: Any) -> Result:
        """按 `registry.COMMANDS` 里的名字执行一条登记命令，并等待应答。

        **时间敏感命令**(`CommandSpec.urgent`, 如 `ESTOP`/`STOP`/`MOVE`/`MAZE *`/`CAL`)
        发送前会让遥测拉取器**让路**(`stream.suspend()`), 使命令独占链路, 应答到达后恢复。
        原因见 `TelemetryPuller.suspend()`: 固件每 1ms 只消费一行命令, 拉取器持续发命令
        会让急停被丢弃 —— 真机实测"急停不立即生效"就是这个原因。普通读写命令照旧走
        快路径(它们有重发兜底, 且实测在拉取器开着时 8/8 成功)。
        """
        self._check_reentrancy(f"send_cmd({cmd!r})")
        line = command_line(cmd, **kw)
        spec = spec_of(cmd)
        # 登记表级**现场安全检查**(如 maze.run 的低电闸、calib.cal 的擦除确认)。
        # 返回非 None 即拒绝发送, 不碰链路 —— 放在这里而不是某个 API 方法里,
        # 是为了让脚本/CLI/datalog 等所有走 send_cmd 的路径都绕不开
        # (真机事故 2026-10-05 的教训)。v2.7 起把 `**kw` 一并传给 precheck,
        # 使 `confirm=True` 这类"只参与安全检查、不进命令文本"的参数能穿过来
        # (build 侧用 `**_kw` 吞掉, 见 registry._cal)。
        if spec.precheck is not None:
            bad = spec.precheck(self, **kw)
            if bad is not None:
                status, data = bad
                return Result(False, status, dict(data or {}, cmd=line, blocked=True))
        to = timeout if timeout is not None else (spec.timeout if spec.timeout is not None else 3.0)
        # `no_retry` 的命令(目前 CAL / MAP DUMP)超时不重发: 它们一发就已产生持续性
        # 副作用, 重发不会让效果更快, 反而把命令插进正在进行的流程里。
        if spec.urgent:
            self.stream.suspend()   # ★ 急停/停止等慢不得 —— 抢占链路
        try:
            p = self.cmd.send(line, wait=False, no_retry=spec.no_retry)
            if not self._wait_pending(p, to):
                return Result(False, "TIMEOUT", {"cmd": line, "attempts": p.attempts})
            return Result(p.ok, p.status, {"cmd": line, "attempts": p.attempts})
        finally:
            if spec.urgent:
                self.stream.resume()

    def send_async(self, cmd: str, **kw: Any):
        """发送但不等待（返回 Pending）。需要自己 `wait(lambda: p.done)`。"""
        spec = spec_of(cmd)
        return self.cmd.send(command_line(cmd, **kw), wait=False, no_retry=spec.no_retry)

    def send_raw(self, line: str, timeout: float = 3.0) -> Result:
        """逃生舱: 直接发一条原始命令（不经过登记表）。

        仅用于临时调试。正式功能必须进 `registry.COMMANDS` ——
        否则契约测试覆盖不到它。
        """
        self._check_reentrancy("send_raw()")
        self.stream.suspend()   # 同 send_cmd: 请拉取器让路(见其注释)
        try:
            p = self.cmd.send(line, wait=False)
            if not self._wait_pending(p, timeout):
                return Result(False, "TIMEOUT", {"cmd": line, "attempts": p.attempts})
            return Result(p.ok, p.status, {"cmd": line, "attempts": p.attempts})
        finally:
            self.stream.resume()

    # ============================================================ 会话

    def open_session(self, name: Optional[str] = None) -> Optional[Session]:
        if self._session is not None:
            return self._session
        try:
            s = Session(root=self.session_dir, name=name)
        except OSError as e:
            self.bus.emit(Event.ERROR, {"kind": "session", "msg": f"{type(e).__name__}: {e}"})
            return None
        for note in self._session_notes:
            s.write_text(note, "##")
        self._session = s
        self.bus.emit(Event.SESSION, {"op": "open", "path": s.path})
        return s

    def close_session(self, extra: Optional[dict] = None) -> None:
        s = self._session
        if s is None:
            return
        meta = {"link_stats": self.link.stats()}
        if extra:
            meta.update(extra)
        s.close(meta)
        self._session = None
        self.bus.emit(Event.SESSION, {"op": "close", "path": s.path})

    @property
    def session_obj(self) -> Optional[Session]:
        """当前会话对象(`Session`); 未开会话时为 `None`。"""
        return self._session

    def _persist(self, msgs: List[EventMsg]) -> None:
        """按帧到达顺序落盘 —— 订阅式, 不漏帧。"""
        sess = self._session
        if sess is None:
            return
        ts = self.timesync
        for m in msgs:
            if m.kind == Event.EVENT or m.kind == Event.ESTOP:
                rec = m.data
                if rec.get("event"):
                    try:
                        sess.write_event(rec)
                    except Exception:
                        pass
                continue
            if m.kind != Event.TELEMETRY:
                continue
            rec = m.data.get("rec") or {}
            # 时间同步: 给记录补上"发车以来毫秒"。
            #   统一帧自带 ms8 -> 用锚做同余还原, 精确;
            #   锚/发车缺失时用到达时刻投影(会略偏大), 仍保证 CSV 有一列时间。
            try:
                rec["_dev_ms"] = ts.dev_ms(rec)
                if rec["_dev_ms"] is None:
                    rec["_dev_ms"] = ts.dev_ms_from_recv(rec)
            except Exception:
                rec["_dev_ms"] = None
            sess.write_unified(rec)

    # ============================================================ 周期任务

    def _periodic_advice(self) -> None:
        """链路诊断(带去重, 避免每 20ms 刷一条)。"""
        if not self.link.is_open:
            self._last_advice = None
            return
        chk = self.link.self_check()
        if chk["level"] == "degraded":
            key = ("degraded", chk.get("note"))
            if key != self._last_advice:
                self._last_advice = key
                self.bus.emit(Event.ADVICE, dict(chk))
        elif chk["level"] == "ok":
            self._last_advice = None

    # ============================================================ 统计

    def stats(self) -> Dict[str, Any]:
        st = self.link.stats()
        st["cmd"] = dict(self.cmd.stats)
        st["events"] = {"emitted": self.bus.emitted, "dropped": self.bus.dropped}
        return st


# ---------------------------------------------------------------- target 归一化

_SERIAL_BAUD_DEFAULT = 115200


def coerce_target(target: Any = None, baud: Optional[int] = None, **kw) -> Target:
    """把多种写法归一成 `Target`。

    支持:
        "COM7"                        串口
        "COM7", baud=9600             串口指定波特率
        "tcp://127.0.0.1:5555"        TCP(模拟器)
        "replay://sessions/xxx"       离线回放一个会话目录
        Target(...)                   直接用
        None                          自动挑第一个可用串口
    """
    if isinstance(target, Target):
        return target
    if target is None:
        ports = _usable_ports()
        if not ports:
            return Target(kind="tcp", host="127.0.0.1", tcp_port=5555)
        return Target(kind="serial", port=ports[0].device, baud=baud or _SERIAL_BAUD_DEFAULT)
    if not isinstance(target, str):
        raise TypeError(f"无法解析的连接目标: {target!r}")
    s = target.strip()
    if s.startswith("tcp://"):
        host, _, port = s[6:].partition(":")
        return Target(kind="tcp", host=host or "127.0.0.1", tcp_port=int(port or 5555))
    if s.startswith("replay://"):
        return Target(kind="replay", path=s[9:])
    if s.startswith("sim"):
        return Target(kind="tcp", host="127.0.0.1", tcp_port=int(kw.get("tcp_port", 5555)))
    return Target(kind="serial", port=s, baud=baud or _SERIAL_BAUD_DEFAULT)


def _usable_ports():
    from ..transport.ports import list_ports

    return [p for p in list_ports() if p.usable]
