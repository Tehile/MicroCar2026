"""主窗口: 装配传输、协议、模型、视图, 并用一个 40Hz 定时器驱动界面刷新。

线程模型: 读取线程只往 TelemetryStore / CommandClient 里写(两者自带锁),
主线程定时器 refresh() 读取并重绘 —— 不存在跨线程 Qt 调用。
"""

from __future__ import annotations

import threading
import time
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..model.ring import TelemetryStore
from ..model.session import Session
from ..protocol.codec import MOTION_TASK_NAMES, parse_event_line
from ..protocol.commands import CommandClient
from ..transport.link import Link, LinkState, Target
from ..transport.ports import list_ports
from .calib_wizard import CalibWizard
from .console import ConsoleWidget
from .fetch_panel import FetchPanel
from .maze_view import MazeWidget
from .param_panel import ParamPanel
from .status_panel import StatusPanel

REFRESH_MS = 25  # 40Hz
POLL_MS = 20


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"电脑鼠蓝牙上位机 v{__version__}  ——  STM32F103RB / USART1 @115200")
        self.resize(1500, 950)

        # ---- 模型/协议 ----
        self.store = TelemetryStore(window_s=30.0)
        self.client = CommandClient(send_bytes=self._send_bytes)
        self.session: Optional[Session] = None
        self.link = Link(
            store=self.store,
            client=self.client,
            on_state=self._on_link_state,
            on_session_raw=self._on_session_raw,
        )

        # 链路状态从读取线程回流到主线程的队列(见 _on_link_state)
        self._state_queue: List[Tuple[str, str]] = []
        self._state_lock = threading.Lock()

        # ---- 遥测拉取状态(v2.1: 主机按需拉取, 停等) ----
        self._pulling = False
        self._telem_pending = None

        # ---- 界面 ----
        self._build_ui()

        # ---- 定时器 ----
        self.t_refresh = QTimer(self)
        self.t_refresh.timeout.connect(self._refresh)
        self.t_refresh.start(REFRESH_MS)
        self.t_poll = QTimer(self)
        self.t_poll.timeout.connect(self.client.poll)
        self.t_poll.timeout.connect(self._pull_tick)   # 停等拉取: 每拍最多补一条 TELEM
        self.t_poll.start(POLL_MS)

        self._scan_ports()
        self._log("上位机就绪。无硬件时可选 TCP 连接到 sim/mouse_sim.py 做全链路演练。")

    # ============================================================ 构建界面

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)

        # ---- 顶栏 ----
        bar = QHBoxLayout()
        bar.addWidget(QLabel("目标:"))
        self.cb_target = QComboBox()
        self.cb_target.setMinimumWidth(320)
        bar.addWidget(self.cb_target)
        self.btn_scan = QPushButton("刷新")
        self.btn_scan.clicked.connect(self._scan_ports)
        bar.addWidget(self.btn_scan)
        self.btn_connect = QPushButton("连接")
        self.btn_connect.clicked.connect(self._toggle_connect)
        bar.addWidget(self.btn_connect)
        bar.addSpacing(8)

        # 波特率: 蓝牙透传模块的 UART 侧速率必须与这里一致, 否则收到的是乱码。
        # 早期版本把它写死 115200, 换个模块就得改代码 —— 这里做成可选 + 可手输。
        bar.addWidget(QLabel("波特率:"))
        self.cb_baud = QComboBox()
        self.cb_baud.setEditable(True)
        self.cb_baud.addItems(["115200", "9600", "38400", "57600", "19200", "230400", "4800"])
        self.cb_baud.setCurrentText("115200")
        self.cb_baud.setFixedWidth(90)
        bar.addWidget(self.cb_baud)
        bar.addSpacing(16)

        self.lbl_state = QLabel("未连接")
        self.lbl_state.setStyleSheet("color:#ef5350; font-weight:500;")
        bar.addWidget(self.lbl_state)
        bar.addSpacing(16)
        self.lbl_tp = QLabel("吞吐 —")
        bar.addWidget(self.lbl_tp)
        self.lbl_rate = QLabel("统一帧 —")
        bar.addWidget(self.lbl_rate)
        bar.addSpacing(16)
        bar.addStretch(1)
        self.btn_session = QPushButton("开始会话记录")
        self.btn_session.clicked.connect(self._toggle_session)
        bar.addWidget(self.btn_session)
        root.addLayout(bar)

        # ---- 主体 ----
        split = QSplitter(Qt.Horizontal)

        left = QTabWidget()
        # v2.0: 上行只有统一帧一路(约 217 Hz 全量快照), 实时页以状态读数为纲。
        self.status = StatusPanel(self.store)
        left.addTab(self.status, "实时状态")
        left.setCurrentIndex(0)
        self.left_tabs = left

        self.console = ConsoleWidget(self.client)
        self.wizard = CalibWizard(self.client, None, self._log, self.store)
        self.params = ParamPanel(self.client, None, self._log)

        right = QTabWidget()
        self.fetch = FetchPanel(self.store, self._log)
        self.maze = MazeWidget()
        right.addTab(self.fetch, "统一帧查看")
        right.addTab(self.maze, "迷宫")
        right.addTab(self.wizard, "标定向导")
        right.addTab(self.params, "参数")
        right.setMaximumWidth(560)
        self.right_tabs = right

        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        root.addWidget(split, 1)

        # ---- 控制条 + 状态 ----
        ctl = QHBoxLayout()
        for text, slot, color in (
            # v2.1: 遥测由**主机按需拉取**(`TELEM`), 固件不再自时钟发送。
            # 固件每 1ms tick 只消费一行命令 -> 必须**停等**, 一次只发一条。
            ("▶ 开始拉取", self._pull_start, "#2e7d32"),
            ("■ 停止拉取", self._pull_stop, "#424242"),
            ("拉取墙图", lambda: self.client.send("MAP DUMP"), "#5e35b1"),
            ("TARE 清零", lambda: self.client.send("TARE"), "#424242"),
            ("STOP", lambda: self.client.send("STOP"), "#e65100"),
            ("ESTOP 急停", lambda: self.client.send("ESTOP"), "#b71c1c"),
        ):
            b = QPushButton(text)
            b.setStyleSheet(f"background:{color}; color:white;")
            b.clicked.connect(lambda _=False, s=slot: s())
            ctl.addWidget(b)
        ctl.addStretch(1)
        self.lbl_robot = QLabel("—")
        f = QFont("Consolas")
        f.setPointSize(9)
        self.lbl_robot.setFont(f)
        ctl.addWidget(self.lbl_robot)
        root.addLayout(ctl)
        root.addWidget(self.console, 0)
        self.console.setMaximumHeight(200)

        self.setCentralWidget(central)
        sb = QStatusBar()
        self.setStatusBar(sb)
        self.lbl_status = QLabel("")
        sb.addWidget(self.lbl_status)
        self.lbl_counts = QLabel("")
        sb.addPermanentWidget(self.lbl_counts)

    # ============================================================ 连接

    def _scan_ports(self) -> None:
        self.cb_target.clear()
        ports = list_ports()
        self._targets: List[Target] = []
        for p in ports:
            self._targets.append(Target(kind="serial", port=p.device, baud=115200))
            self.cb_target.addItem(p.label())
        self._targets.append(Target(kind="tcp", host="127.0.0.1", tcp_port=5555))
        self.cb_target.addItem("[模拟器] TCP 127.0.0.1:5555")
        self._targets.append(Target(kind="tcp", host="127.0.0.1", tcp_port=5556))
        self.cb_target.addItem("[模拟器] TCP 127.0.0.1:5556")

        # list_ports() 已把"可用 > 有线 > 蓝牙传出 > 蓝牙传入"排好序,
        # 所以 0 号就是最该选的那个, 不必再猜。
        if ports:
            self.cb_target.setCurrentIndex(0)
        else:
            self.cb_target.setCurrentIndex(len(self._targets) - 2)

        for p in ports:
            if p.direction == "incoming":
                self._log(f"[端口] {p.device} 是蓝牙『传入』端口 —— 能打开但永远收不到数据, "
                          f"请改用『传出』端口。")
        if ports:
            self._log(f"[端口] 已识别 {len(ports)} 个串口, 默认选中最上方的 "
                      f"{ports[0].device}。")
        if self.link.state == LinkState.OPEN:
            self.cb_target.setEnabled(False)

    def _toggle_connect(self) -> None:
        if self.link.is_open:
            # 先停拉取再断开: 一旦不再请求, 设备就不再发送, 蓝牙模块缓冲自然排空
            # (v2.1 的自愈路径 —— 不必给模块重新上电)。
            self._pull_stop()
            self.link.close()
            self.btn_connect.setText("连接")
            self.cb_target.setEnabled(True)
            self.cb_baud.setEnabled(True)
            self._close_session()
            return
        idx = self.cb_target.currentIndex()
        if idx < 0 or idx >= len(self._targets):
            return
        target = self._targets[idx]
        if target.kind == "serial":
            target = Target(kind="serial", port=target.port, baud=self._current_baud())
        if not self.link.open(target):
            # 打开失败: 不改按钮、不禁用下拉、不建会话目录。
            # 早期版本无论成败都切成"已连接"并建了会话, 界面上像是连上了,
            # 实际后续每次发送都被静默丢弃("链路未打开, 发送被丢弃"),
            # 而失败原因只显示在顶栏一行小字里 —— 这正是排查困难的根源。
            # 具体原因由 _drain_link_state() 在主线程写进日志。
            return
        self.btn_connect.setText("断开")
        self.cb_target.setEnabled(False)
        self.cb_baud.setEnabled(False)
        self._open_session()
        # v2.1: 连上后开始**停等拉取**遥测(见 _pull_tick)。
        # 不再有 `STREAM ON`: 那条命令已删除, 现在改由主机按需发 `TELEM`。
        self._pull_start()

    def _current_baud(self) -> int:
        try:
            return int(str(self.cb_baud.currentText()).strip())
        except (ValueError, TypeError):
            self._log(f"[波特率] {self.cb_baud.currentText()!r} 不是有效整数, 回落 115200")
            return 115200

    # ------------------------------------------------------------ 遥测拉取(v2.1)

    def _pull_start(self) -> None:
        """开始按需拉取遥测。非阻塞 —— 由 `_pull_tick` 每拍推一条 `TELEM`。"""
        if self._pulling:
            return
        self._pulling = True
        self._log("[拉取] 开始按需拉取遥测 (TELEM, 停等)")

    def _pull_stop(self) -> None:
        """停止拉取。**立刻不再发请求** —— 设备随之停发, 蓝牙缓冲自然排空。"""
        if not self._pulling:
            return
        self._pulling = False
        self._telem_pending = None
        self._log("[拉取] 已停止拉取")

    def _pull_tick(self) -> None:
        """**停等式**拉取: 只有在途为空时才发下一条。

        ⚠️ 绝不一次发多条: 固件每 1ms tick 只消费一行命令(见 `link.c`),
        成串发会丢第二条(实测背靠背 2 条的回收率仅 50%)。
        与 `host/api/stream.py` 的 `PullRequester` 是同一条纪律。
        """
        if not self._pulling or not self.link.is_open:
            return
        p = self._telem_pending
        if p is not None and not p.done:
            return
        self._telem_pending = self.client.send("TELEM", no_retry=True)
        self._telem_pending.callbacks.append(self._absorb_anchor)

    def _absorb_anchor(self, ok: bool, status: str) -> None:
        """从 `ACK <seq> OK <upMs>` 取时间锚写进 store(每拉一帧就刷新一次)。"""
        if not ok:
            return
        parts = str(status or "").split()
        if len(parts) >= 2 and parts[0].upper() == "OK":
            try:
                self.store.set_anchor(int(parts[1]))
            except (ValueError, AttributeError):
                pass

    # ------------------------------------------------------------ 收发

    def _send_bytes(self, data: bytes) -> None:
        if not self.link.write(data):
            self._log("链路未打开, 发送被丢弃")

    def _on_link_state(self, state: str, detail: str) -> None:
        """链路状态回调 —— 可能在读取线程里触发, 因此只入队, 绝不直接碰 Qt 控件。"""
        with self._state_lock:
            self._state_queue.append((state, detail))

    def _drain_link_state(self) -> None:
        """在主线程消费状态队列: 刷新顶栏 + 把错误写进日志。"""
        with self._state_lock:
            items = list(self._state_queue)
            self._state_queue.clear()
        for state, detail in items:
            if state == LinkState.OPEN:
                self.lbl_state.setText(f"已连接 {detail}")
                self.lbl_state.setStyleSheet("color:#81c784; font-weight:500;")
            elif state == LinkState.ERROR:
                self.lbl_state.setText(f"错误: {detail}")
                self.lbl_state.setStyleSheet("color:#ef5350; font-weight:500;")
                self._log(f"[链路错误] {detail}")
                if self.target_looks_like_incoming():
                    self._log("           提示: 该端口是蓝牙『传入』端口, 它永远不会收到数据,"
                              " 请改选『传出』端口。")
                # 运行期断链(蓝牙掉线等): 恢复按钮, 让用户能重新连接
                if not self.link.is_open:
                    self.btn_connect.setText("连接")
                    self.cb_target.setEnabled(True)
                    self.cb_baud.setEnabled(True)
            elif state == LinkState.CONNECTING:
                self.lbl_state.setText(f"连接中 {detail}")
                self.lbl_state.setStyleSheet("color:#FFD54F; font-weight:500;")
            else:
                self.lbl_state.setText("未连接")
                self.lbl_state.setStyleSheet("color:#ef5350; font-weight:500;")

    def target_looks_like_incoming(self) -> bool:
        """当前选中的目标是否是蓝牙『传入』端口(永远收不到数据那种)。"""
        idx = self.cb_target.currentIndex()
        if idx < 0 or idx >= len(self._targets):
            return False
        t = self._targets[idx]
        if t.kind != "serial":
            return False
        for p in list_ports():
            if p.device == t.port:
                return p.direction == "incoming"
        return False

    # ============================================================ 会话

    def _open_session(self) -> None:
        if self.session is not None:
            return
        try:
            self.session = Session()
        except OSError as e:
            self._log(f"无法建立会话目录: {e}")
            return
        self.wizard.session = self.session
        self.params.session = self.session
        self.console.session = self.session
        self.btn_session.setText("结束会话记录")
        self._log(f"[会话] 目录 {self.session.path}")

    def _close_session(self) -> None:
        if self.session is None:
            return
        self.session.close({"link_stats": self.link.stats()})
        self._log(f"[会话] 已收尾 {self.session.path}")
        self.session = None
        self.btn_session.setText("开始会话记录")

    def _toggle_session(self) -> None:
        if self.session is None:
            self._open_session()
        else:
            self._close_session()

    def _on_session_raw(self, data: bytes) -> None:
        if self.session is not None:
            self.session.write_raw(data)

    # ============================================================ 刷新

    def _refresh(self) -> None:
        self._drain_link_state()
        self.status.refresh()
        self.fetch.refresh()

        # 异步消息 -> 标定向导 + 事件入 store + 会话日志(在主线程处理, 保证线程安全)
        for line in self.client.drain_events():
            self.wizard.handle_async(line)
            ev = parse_event_line(line)
            if ev is not None:
                self.store.push_event(ev)
            if self.session is not None:
                self.session.write_text(line)

        # 统一帧驱动迷宫坐标
        f = self.store.latest()
        if f:
            self.maze.set_position(int(f.get("posX", 0)), int(f.get("posY", 0)),
                                   int(f.get("mazeTask", 0)))

        # 会话落盘(本面板每轮取最新一帧写一行; 高频逐帧落盘由门面/会话订阅负责)
        if self.session is not None and f:
            self.session.write_unified(f)

        self._refresh_labels()

    def _refresh_labels(self) -> None:
        st = self.link.stats()
        if not self.link.is_open:
            return
        chk = self.link.self_check()
        need = chk["need_bps"]
        got = chk["got_bps"]
        pct = (got / need * 100.0) if need else 0.0
        color = "#81c784" if pct >= 100 or chk["level"] == "ok" else ("#FFD54F" if pct >= 60 else "#ef5350")
        self.lbl_tp.setText(f"吞吐 {got:,.0f}/{need:,.0f} B/s ({pct:.0f}%)")
        self.lbl_tp.setStyleSheet(f"color:{color}; font-weight:500;")
        self.lbl_rate.setText(f"统一帧 {st['unified']}")
        self.lbl_counts.setText(
            f"统一帧 {st['unified']} 墙图 {st['map']} | 文本 {st['text']} 事件 {st['event']} | "
            f"CRC错 {st['bad_crc']} 失步 {st['bad_sync']} | 序号缺口 {st['seq_gap']} 丢失 {st['seq_lost']} | "
            f"重发 {self.client.stats['retries']} 超时 {self.client.stats['timeouts']}"
        )
        # 链路诊断(§5.8): 实测吞吐离物理上限过远时提示排查(不再有"降级速率"一说)
        if chk["level"] == "degraded" and not getattr(self, "_warned_rate", False):
            self._warned_rate = True
            self._log(f"[链路自检] {chk.get('note') or '实测吞吐偏低, 请检查蓝牙/串口驱动'}")

        # 实时读数条(统一帧)
        f = self.store.latest()
        if f:
            task = f.get("motionTaskName") or MOTION_TASK_NAMES.get(
                int(f.get("motionTask", 0)), str(f.get("motionTask", 0)))
            extra = ""
            if f.get("emerg"):
                extra = "  ⚠急停"
            elif f.get("freemode"):
                extra = "  [自由模式:电机停]"
            self.lbl_robot.setText(
                f"L {f.get('speedL',0):+5.0f}  R {f.get('speedR',0):+5.0f} cm/s   "
                f"θ {f.get('gyroAngle',0):+4.0f}°   V {f.get('battMv',0)/1000:.2f}V   "
                f"{task}{extra}"
            )
            self.lbl_status.setText(
                f"坐标 ({f.get('posX')},{f.get('posY')})  {f.get('mazeTaskName','')}   "
                f"红外 F{f.get('irF1')}/{f.get('irF2')} L{f.get('irL')} R{f.get('irR')} mm   "
                f"里程 L{f.get('trackL')} R{f.get('trackR')}  格 {f.get('cellOdom')}"
            )

    def _log(self, text: str) -> None:
        self.console.append_system(text)
        if self.session is not None:
            self.session.write_text(text, "##")

    # ============================================================ 收尾

    def closeEvent(self, ev) -> None:
        try:
            self.link.close()
        finally:
            self._close_session()
        super().closeEvent(ev)
