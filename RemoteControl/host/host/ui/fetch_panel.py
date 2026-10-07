"""统一帧查看 / 导出面板。

v2.0 起上行**只有统一帧**一种周期数据(约 217 Hz、39 通道全量快照),
原先的 SCOPE/DUMP 分块回传机制已整体删除 —— 因此本面板不再"停车后拉取",
而是就地查看内存环形缓冲里的统一帧历史, 并可导出为 CSV 存档。

分工:
    实时状态(StatusPanel)  大字读数(当前值)
    本面板                 帧计数/帧率/缓冲占用 + 最新一帧全通道 + CSV 导出
"""

from __future__ import annotations

import csv
import os
import time
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..model.ring import CHANNELS, TelemetryStore


class FetchPanel(QWidget):
    """统一帧查看/导出。由主窗口定时器驱动 refresh()。"""

    def __init__(
        self,
        store: TelemetryStore,
        log: Callable[[str], None],
    ) -> None:
        super().__init__()
        self.store = store
        self.log = log
        self._last_total = 0
        self._last_t = time.time()

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(8)

        hint = QLabel(
            "统一帧是设备唯一的数据源(约 217 Hz、39 通道全量快照),\n"
            "在内存环形缓冲里滚动保存; 这里查看最新一帧并导出历史 CSV。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#FFD54F;")
        root.addWidget(hint)

        # ---------- 缓冲统计 ----------
        box = QGroupBox("缓冲统计")
        v = QVBoxLayout(box)
        self.lbl_frames = QLabel("尚未收到统一帧")
        self.lbl_frames.setStyleSheet("color:#81c784;")
        v.addWidget(self.lbl_frames)
        self.lbl_rate = QLabel("")
        self.lbl_rate.setStyleSheet("color:#9e9e9e;")
        v.addWidget(self.lbl_rate)
        root.addWidget(box)

        # ---------- 最新一帧 ----------
        tbox = QGroupBox("最新一帧")
        tv = QVBoxLayout(tbox)
        self.table = QTableWidget(len(CHANNELS), 2)
        self.table.setHorizontalHeaderLabels(["通道", "值"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 150)
        for r, c in enumerate(CHANNELS):
            name = QTableWidgetItem(c)
            name.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(r, 0, name)
            val = QTableWidgetItem("—")
            val.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(r, 1, val)
        tv.addWidget(self.table)
        root.addWidget(tbox, 1)

        # ---------- 动作 ----------
        row = QHBoxLayout()
        self.btn_export = QPushButton("导出统一帧 CSV")
        self.btn_export.setStyleSheet("background:#1565c0; color:white;")
        self.btn_export.clicked.connect(self._export)
        row.addWidget(self.btn_export)
        self.btn_clear = QPushButton("清空缓冲")
        self.btn_clear.clicked.connect(self._clear)
        row.addWidget(self.btn_clear)
        row.addStretch(1)
        root.addLayout(row)

    # ------------------------------------------------------------ 动作

    def _export(self) -> None:
        """把当前环形缓冲里的统一帧全量写成 CSV。"""
        t, data = self.store.unified.snapshot()
        if len(t) == 0:
            self.log("[导出] 缓冲里还没有统一帧数据")
            return
        default = os.path.join(os.getcwd(), time.strftime("unified_%Y%m%d_%H%M%S.csv"))
        path, _ = QFileDialog.getSaveFileName(self, "导出统一帧 CSV", default, "CSV (*.csv)")
        if not path:
            return
        chans = list(self.store.unified.channels)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["t_epoch"] + chans)
            for i in range(len(t)):
                w.writerow([f"{t[i]:.6f}"] + [data[i, j] for j in range(len(chans))])
        self.log(f"[导出] 已写出 {len(t)} 帧 -> {path}")

    def _clear(self) -> None:
        self.store.unified.clear()
        self.log("[导出] 已清空统一帧环形缓冲")

    # ------------------------------------------------------------ 刷新

    def refresh(self) -> None:
        now = time.time()
        total = int(self.store.unified.total)
        dt = now - self._last_t
        if dt >= 1.0:
            self.lbl_rate.setText(f"平均 {(total - self._last_total) / dt:5.1f} 帧/s")
            self._last_total = total
            self._last_t = now

        rb = self.store.unified
        self.lbl_frames.setText(f"累计 {total} 帧   缓冲 {rb.count}/{rb.capacity}")

        f = self.store.latest()
        for r, c in enumerate(CHANNELS):
            it = self.table.item(r, 1)
            if it is not None:
                it.setText(str(f.get(c, "—")) if f else "—")
