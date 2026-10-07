"""文本调试台: 显示链路日志(含 printf 输出), 并允许手工敲任意命令。

手工输入会自动补 CRC 与序号, 因此与上位机下发的命令走完全相同的可靠性通路。
"""

from __future__ import annotations

from typing import Callable, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..protocol.commands import CommandClient

QUICK = [
    "PING",
    "GET ALL",
    "TARE",
    "STOP",
    "ESTOP",
    "MAP DUMP",
    # v2.1: `STREAM ON` / `STREAM OFF` 已删除 —— 遥测改由 `TELEM` 按需拉取
    # (固件每 1ms tick 只消费一行命令, 所以只能一次发一条)。
    "TELEM",
]


class ConsoleWidget(QWidget):
    def __init__(self, client: CommandClient, session=None) -> None:
        super().__init__()
        self.client = client
        self.session = session
        self._shown = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("快速命令:"))
        for q in QUICK:
            b = QPushButton(q)
            b.setMaximumWidth(110)
            b.clicked.connect(lambda _=False, c=q: self._quick(c))
            bar.addWidget(b)
        bar.addStretch(1)
        self.cb_raw = QCheckBox("显示设备 printf 文本")
        self.cb_raw.setChecked(True)
        bar.addWidget(self.cb_raw)
        self.btn_clear = QPushButton("清屏")
        self.btn_clear.clicked.connect(lambda: (self.view.clear(), setattr(self, "_shown", len(self.client.log))))
        bar.addWidget(self.btn_clear)
        root.addLayout(bar)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(3000)
        f = QFont("Consolas")
        f.setPointSize(9)
        self.view.setFont(f)
        root.addWidget(self.view, 1)

        row = QHBoxLayout()
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("输入命令(不含 CRC, 例如: SET TRACKL 53.2015)，回车发送")
        self.edit.returnPressed.connect(self._send_manual)
        row.addWidget(self.edit, 1)
        b = QPushButton("发送")
        b.clicked.connect(self._send_manual)
        row.addWidget(b)
        root.addLayout(row)

    def _quick(self, cmd: str) -> None:
        try:
            self.client.send(cmd)
        except Exception as e:
            self.append_system(f"发送失败: {e}")

    def _send_manual(self) -> None:
        cmd = self.edit.text().strip()
        if not cmd:
            return
        self.edit.clear()
        try:
            self.client.send(cmd)
        except Exception as e:
            self.append_system(f"发送失败: {e}")

    def append_system(self, text: str) -> None:
        self.view.appendPlainText(f"[上位机] {text}")
        self._shown = len(self.client.log)

    def refresh(self) -> None:
        """把 CommandClient 新增的日志刷进视图。"""
        log = self.client.log
        if self._shown > len(log):
            self._shown = 0
        show_raw = self.cb_raw.isChecked()
        for ts, direction, text in log[self._shown :]:
            if direction == "<-":
                # 设备 printf 文本与 ACK/P 应答一起混在文本流里;
                # 只过滤掉明显的二进制噪声
                if "\ufffd" in text:
                    continue
                if not show_raw and not (
                    text.startswith(("ACK ", "P ", "PONG ", "CAL", "STOPPED", "ESTOP "))
                ):
                    continue
                self.view.appendPlainText(f"← {text}")
            else:
                self.view.appendPlainText(f"→ {text}")
        self._shown = len(log)
