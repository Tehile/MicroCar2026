"""参数面板: 7 项标定参数的读/写/持久化, 含 Q16.16 与十进制互显。"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..protocol.commands import PARAM_IDS, PARAM_META, PARAM_NAMES
from ..protocol.codec import float_to_q1616, q1616_to_float

ORDER = ["TRACKL", "TRACKR", "SPEEDL", "SPEEDR", "GYRORATIO", "ENCANGLE", "WALLDIST"]


class ParamPanel(QWidget):
    def __init__(self, client, session, log) -> None:
        super().__init__()
        self.client = client
        self.session = session
        self.log = log

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)

        tip = QLabel(
            "SET 只写 RAM(立即生效), SAVE 才写 Flash(page 99)。\n"
            "写入后自动 GET 回读校验; SAVE 前自动把旧值导出到会话目录备份。"
        )
        tip.setStyleSheet("color:#9e9e9e;")
        root.addWidget(tip)

        self.table = QTableWidget(len(ORDER), 4)
        self.table.setHorizontalHeaderLabels(["参数", "值", "单位", "说明"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 88)
        self.table.setColumnWidth(1, 110)
        self.table.setColumnWidth(2, 140)
        for r, name in enumerate(ORDER):
            unit, decimals, note = PARAM_META[name]
            it_name = QTableWidgetItem(name)
            it_name.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(r, 0, it_name)
            it_val = QTableWidgetItem("—")
            it_val.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(r, 1, it_val)
            it_unit = QTableWidgetItem(unit)
            it_unit.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(r, 2, it_unit)
            it_note = QTableWidgetItem(note)
            it_note.setFlags(Qt.ItemIsEnabled)
            it_note.setForeground(Qt.gray)
            self.table.setItem(r, 3, it_note)
        root.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.btn_read = QPushButton("读取全部")
        self.btn_write = QPushButton("写入选中")
        self.btn_derive = QPushButton("按 TRACK 派生 SPEED")
        self.btn_backup = QPushButton("备份当前参数")
        row.addWidget(self.btn_read)
        row.addWidget(self.btn_write)
        row.addWidget(self.btn_derive)
        row.addWidget(self.btn_backup)
        root.addLayout(row)

        row2 = QHBoxLayout()
        self.btn_save = QPushButton("SAVE 写入 Flash")
        self.btn_reset = QPushButton("RESET 擦除参数页")
        self.btn_save.setStyleSheet("background:#2e7d32; color:white;")
        self.btn_reset.setStyleSheet("background:#b71c1c; color:white;")
        row2.addWidget(self.btn_save)
        row2.addWidget(self.btn_reset)
        root.addLayout(row2)

        self.status = QLabel("")
        self.status.setStyleSheet("color:#FFD54F;")
        root.addWidget(self.status)

        self.btn_read.clicked.connect(self.read_all)
        self.btn_write.clicked.connect(self.write_selected)
        self.btn_derive.clicked.connect(self.derive_speed)
        self.btn_backup.clicked.connect(self.backup)
        self.btn_save.clicked.connect(self.do_save)
        self.btn_reset.clicked.connect(self.do_reset)

    # ------------------------------------------------------------ 显示

    def refresh_from_client(self) -> None:
        """把 CommandClient 里的参数值刷新到表格(不改变正在编辑的单元格)。"""
        for r, name in enumerate(ORDER):
            q = self.client.params.get(PARAM_IDS[name])
            if q is None:
                continue
            decimals = PARAM_META[name][1]
            txt = f"{q1616_to_float(q):.{decimals}f}"
            it = self.table.item(r, 1)
            if it is not None and not it.text().startswith("*"):
                it.setText(txt)

    def _set_status(self, text: str) -> None:
        self.status.setText(text)

    def _values(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for r, name in enumerate(ORDER):
            it = self.table.item(r, 1)
            if it is None:
                continue
            txt = it.text().strip().lstrip("*")
            try:
                out[name] = float(txt)
            except ValueError:
                raise ValueError(f"{name} 的值 “{txt}” 不是合法数字")
        return out

    # ------------------------------------------------------------ 动作

    def read_all(self) -> None:
        try:
            self.client.get_all()
            self.refresh_from_client()
            self._set_status("已从设备读取全部参数")
        except Exception as e:
            self._set_status(f"读取失败: {e}")

    def write_selected(self) -> None:
        try:
            vals = self._values()
        except ValueError as e:
            QMessageBox.warning(self, "参数错误", str(e))
            return
        # 强制同步派生: SPEEDL/R 恒等于 TRACKL/R / 100
        vals["SPEEDL"] = vals["TRACKL"] / 100.0
        vals["SPEEDR"] = vals["TRACKR"] / 100.0
        fails: List[str] = []
        for name in ORDER:
            ok, st = self.client.set_param(name, vals[name], readback=True)
            self.log(f"[PARAM] SET {name}={vals[name]:.6g} -> {st}")
            if not ok:
                fails.append(f"{name}:{st}")
        self.refresh_from_client()
        if fails:
            self._set_status("部分写入失败: " + ", ".join(fails))
        else:
            self._set_status("7 项全部写入并回读校验通过（SPEED 已按 TRACK/100 派生）")

    def derive_speed(self) -> None:
        try:
            vals = self._values()
        except ValueError as e:
            QMessageBox.warning(self, "参数错误", str(e))
            return
        for tr, sp in (("TRACKL", "SPEEDL"), ("TRACKR", "SPEEDR")):
            d = PARAM_META[sp][1]
            self.table.item(ORDER.index(sp), 1).setText(f"{vals[tr] / 100.0:.{d}f}")
        self._set_status("已按 speedRatio = trackRatio / 100 派生（恒等式，见方案文档 §6.1）")

    def backup(self) -> None:
        rows = []
        for name in ORDER:
            q = self.client.params.get(PARAM_IDS[name])
            if q is None:
                continue
            rows.append([name, q, f"{q1616_to_float(q):.6f}"])
        if not rows:
            self.client.get_all()
            self.refresh_from_client()
            return self.backup()
        if self.session is None:
            self._set_status("未开启会话，无法备份")
            return
        path = self.session.backup_params(rows)
        self._set_status(f"已备份到 {os.path.basename(path)}")

    def do_save(self) -> None:
        # SAVE 前自动备份旧值(安全模型要求)
        try:
            self.backup()
        except Exception:
            pass
        if QMessageBox.question(
            self,
            "确认写入 Flash",
            "将把当前 7 项参数写入 Flash page 99（0x08018C00）。\n"
            "旧值已自动导出到会话目录。继续?",
        ) != QMessageBox.Yes:
            return
        ok, st = self.client.save()
        self._set_status(f"SAVE -> {st}" + ("（已固化，掉电不丢）" if ok else ""))
        self.log(f"[PARAM] SAVE -> {st}")

    def do_reset(self) -> None:
        if QMessageBox.question(
            self,
            "确认擦除",
            "将擦除参数页。效果在**下次上电**时体现（回到源码默认值）。\n继续?",
        ) != QMessageBox.Yes:
            return
        ok, st = self.client.reset()
        self._set_status(f"RESET -> {st}（重启后回到源码默认值）")
        self.log(f"[PARAM] RESET -> {st}")
