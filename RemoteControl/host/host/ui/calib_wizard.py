"""半自动标定向导: 6 项物理标定。

交互模型(见方案文档 §6.2):
    上位机弹步骤 -> 用户把鼠摆好 -> 点按钮 -> 设备自己动作/采样 -> 上位机算系数 -> 用户确认应用

线程模型: 所有按钮回调都在 Qt 主线程; 设备异步消息由主窗口定时器从
CommandClient.drain_events() 取出后调用 handle_async(), 因此这里可以安全操作控件。
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional, Tuple

from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..protocol.codec import parse_event_line

# 与固件 link.h 的 LINK_SRV_REPS 一致
SRV_REPS = 10
SRV_TOTAL_DEG = SRV_REPS * 90  # 900

IR_STEPS = ["左近墙(车左侧贴墙、离墙约 33mm)", "左远墙(车左侧离墙约 94mm)", "前近墙(车头距墙约 32mm)",
            "前远墙(车头距墙约 213mm)"]


class _Step(QGroupBox):
    def __init__(self, idx: int, title: str, desc: str) -> None:
        super().__init__(f"{idx}. {title}")
        self.body = QVBoxLayout(self)
        d = QLabel(desc)
        d.setWordWrap(True)
        d.setStyleSheet("color:#9e9e9e;")
        self.body.addWidget(d)
        self.form = QFormLayout()
        self.body.addLayout(self.form)
        row = QHBoxLayout()
        self.buttons: List[QPushButton] = []
        self.body.addLayout(row)
        self.row = row
        self.status = QLabel("未开始")
        self.status.setStyleSheet("color:#FFD54F;")
        self.body.addWidget(self.status)

    def add_button(self, text: str, slot: Callable[[], None], primary: bool = False) -> QPushButton:
        b = QPushButton(text)
        if primary:
            b.setStyleSheet("background:#1565c0; color:white;")
        b.clicked.connect(lambda _=False: slot())
        self.row.addWidget(b)
        self.buttons.append(b)
        return b

    def set_status(self, text: str, color: str = "#FFD54F") -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"color:{color};")

    def add_field(self, label: str, widget: QWidget) -> None:
        self.form.addRow(label, widget)


class CalibWizard(QWidget):
    def __init__(self, client, session, log: Callable[[str], None], store=None) -> None:
        super().__init__()
        self.client = client
        self.session = session
        self.log = log
        #: 遥测 store —— 里程标定要从统一帧的 trackL/trackR 取运行前后的脉冲差
        #: (v2.0 起固件不再在结束事件里带 dL/dR)。
        self.store = store

        # 各项的中间结果
        self.gyro_bias: Optional[int] = None
        self.walldist: Optional[int] = None
        self.ir_phase = 0
        self.srv_sum_acc: Optional[int] = None
        self.srv_sum_enc: Optional[int] = None
        self.srv_deg: Optional[int] = None
        self.track_dL: Optional[int] = None
        self.track_dR: Optional[int] = None
        #: 直线跑前的 (trackL, trackR) 基准, 用于算运行前后的里程差
        self._track_start: Optional[Tuple[int, int]] = None

        outer = QVBoxLayout(self)
        tip = QLabel(
            "标定项只包含物理系数（陀螺零偏 / 红外 8 值 / 车心到墙 / 角度系数 / 里程系数）。\n"
            "PID 增益、三档冲刺增益、速度常数、墙判阈值均属红线，不参与标定。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#90caf9;")
        outer.addWidget(tip)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        box = QVBoxLayout(inner)

        self._build_gyro(box)
        self._build_walldist(box)
        self._build_irdata(box)
        self._build_angle(box)
        self._build_track(box)
        box.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

    # ============================================================ 1 陀螺零偏

    def _build_gyro(self, box: QVBoxLayout) -> None:
        s = _Step(1, "陀螺零偏 GYROBIAS", "把电脑鼠**静止**放在桌上，点『开始』。设备会自行采样约 1.5s 并写入既有 Flash page127。")
        self.lbl_gyro = QLabel("—")
        s.add_field("零偏", self.lbl_gyro)
        s.add_button("开始", lambda: self._send("CAL GYRO START"), primary=True)
        box.addWidget(s)
        self.step_gyro = s

    # ============================================================ 2 车心到墙

    def _build_walldist(self, box: QVBoxLayout) -> None:
        s = _Step(
            2,
            "车心到墙 WALLDIST",
            "把电脑鼠平行贴住左墙（车身与墙平行、贴紧），点『采样』。设备读取左侧红外稳定均值作为车中心到墙距离。",
        )
        self.lbl_wall = QLabel("—")
        s.add_field("距离", self.lbl_wall)
        s.add_button("采样", lambda: self._send("CAL WALLDIST START"), primary=True)
        box.addWidget(s)
        self.step_wall = s

    # ============================================================ 3 红外 8 值

    def _build_irdata(self, box: QVBoxLayout) -> None:
        s = _Step(
            3,
            "红外 8 值 IRDATA",
            "共 4 个位置。点『开始』后按提示依次摆放电脑鼠，每个位置点一次『采样』。\n"
            "位置顺序：左近墙 → 左远墙 → 前近墙 → 前远墙。",
        )
        self.lbl_ir = QLabel("未开始")
        self.bar_ir = QProgressBar()
        self.bar_ir.setRange(0, 4)
        self.bar_ir.setValue(0)
        s.add_field("进度", self.lbl_ir)
        s.add_field("", self.bar_ir)
        s.add_button("开始", self._start_ir, primary=True)
        self.btn_ir_sample = s.add_button("采样当前点", lambda: self._send("CAL IRDATA SAMPLE"))
        self.btn_ir_sample.setEnabled(False)
        box.addWidget(s)
        self.step_ir = s

    # ============================================================ 4 角度系数

    def _build_angle(self, box: QVBoxLayout) -> None:
        s = _Step(
            4,
            "角度系数 GYRORATIO / ENCANGLE",
            f"把电脑鼠放进迷宫**角格**（相邻两面墙，例如左墙 + 前墙），车头大致朝前，点『开始』。\n"
            f"设备会用『前向红外正对墙』的极小值作为物理角度基准，自动在 0°↔90° 之间往复 {SRV_REPS} 次"
            f"（累计 {SRV_TOTAL_DEG}°），全程约 5–10 秒。**期间请勿触碰**。\n"
            "该基准与陀螺/编码器系数无关，因此不会形成循环依赖——不能用陀螺标编码器，反之亦然。",
        )
        self.lbl_angle = QLabel("—")
        self.lbl_g = QLabel("—")
        self.lbl_e = QLabel("—")
        s.add_field("原始累计", self.lbl_angle)
        s.add_field("gyroRatio", self.lbl_g)
        s.add_field("encAngleRatio", self.lbl_e)
        s.add_button("开始", lambda: self._send("CAL GYRORATIO START"), primary=True)
        self.btn_angle_apply = s.add_button("应用到设备", self._apply_angle)
        self.btn_angle_apply.setEnabled(False)
        box.addWidget(s)
        self.step_angle = s

    # ============================================================ 5 里程系数

    def _build_track(self, box: QVBoxLayout) -> None:
        s = _Step(
            5,
            "里程系数 TRACKL / TRACKR",
            "让电脑鼠在一条**足够长且无障碍**的直线通道上跑一段固定脉冲数，然后用尺量出它实际走了多少毫米。\n"
            "先点『开始直线』（设备开环前进固定脉冲数后自动停车并回报左右轮脉冲数），再填入实测距离。",
        )
        self.sp_pulses = QSpinBox()
        self.sp_pulses.setRange(500, 200000)
        self.sp_pulses.setSingleStep(1000)
        self.sp_pulses.setValue(20000)
        self.sp_speed = QSpinBox()
        self.sp_speed.setRange(10, 120)
        self.sp_speed.setValue(40)
        self.sp_dist = QDoubleSpinBox()
        self.sp_dist.setRange(1.0, 5000.0)
        self.sp_dist.setDecimals(1)
        self.sp_dist.setValue(200.0)
        self.lbl_pulses = QLabel("—")
        self.lbl_track = QLabel("—")
        s.add_field("脉冲数", self.sp_pulses)
        s.add_field("速度 (cm/s)", self.sp_speed)
        s.add_field("回报脉冲", self.lbl_pulses)
        s.add_field("实测距离 (mm)", self.sp_dist)
        s.add_field("新系数", self.lbl_track)
        s.add_button("开始直线", self._start_track, primary=True)
        self.btn_track_apply = s.add_button("计算并应用", self._apply_track)
        self.btn_track_apply.setEnabled(False)
        box.addWidget(s)
        self.step_track = s

    # ============================================================ 发送

    def _send(self, cmd: str) -> None:
        self.log(f"[CAL] -> {cmd}")
        self.client.send(cmd)

    def _start_ir(self) -> None:
        """IRDATA 是逐点采集: START 之后必须由用户逐点触发 SAMPLE。"""
        self.ir_phase = 0
        self.bar_ir.setValue(0)
        self.lbl_ir.setText(f"0/4 — 请摆好第 1 点：{IR_STEPS[0]}")
        self.btn_ir_sample.setEnabled(True)
        self._send("CAL IRDATA START")

    # ============================================================ 异步消息

    def handle_async(self, line: str) -> None:
        """在主线程中处理设备异步消息。"""
        if line.startswith("CALSTEP IRDATA"):
            try:
                self.ir_phase = int(line.split()[2]) + 1
            except (IndexError, ValueError):
                self.ir_phase += 1
            self.bar_ir.setValue(self.ir_phase)
            if self.ir_phase < len(IR_STEPS):
                self.lbl_ir.setText(f"已完成 {self.ir_phase}/4，下一点：{IR_STEPS[self.ir_phase]}")
            else:
                self.lbl_ir.setText("4 点已采完")
            return

        if line.startswith("CALDONE GYRO"):
            try:
                self.gyro_bias = int(line.split()[2])
            except (IndexError, ValueError):
                self.gyro_bias = None
            self.lbl_gyro.setText(str(self.gyro_bias))
            self.step_gyro.set_status("完成（已写入 Flash page127）", "#81c784")
            return

        if line.startswith("CALDONE WALLDIST"):
            try:
                self.walldist = int(line.split()[2])
            except (IndexError, ValueError):
                self.walldist = None
            self.lbl_wall.setText(f"{self.walldist} mm")
            self.step_wall.set_status("完成", "#81c784")
            return

        if line.startswith("CALDONE IRDATA"):
            self.lbl_ir.setText("4 点全部完成，查表已重建并写入 page123")
            self.bar_ir.setValue(4)
            self.btn_ir_sample.setEnabled(False)
            self.step_ir.set_status("完成", "#81c784")
            return

        if line.startswith("CALDONE GYRORATIO"):
            parts = line.split()
            try:
                self.srv_sum_acc = int(parts[2])
                self.srv_sum_enc = int(parts[3])
                self.srv_deg = int(parts[4])
            except (IndexError, ValueError):
                return
            self.lbl_angle.setText(f"ΣangleAcc={self.srv_sum_acc}  ΣencDiff={self.srv_sum_enc}  {self.srv_deg}°")
            if self.srv_deg:
                self.lbl_g.setText(f"{self.srv_sum_acc / self.srv_deg:.3f}  (当前 {self._cur('GYRORATIO')})")
                self.lbl_e.setText(f"{self.srv_sum_enc / self.srv_deg:.3f}  (当前 {self._cur('ENCANGLE')})")
            self.btn_angle_apply.setEnabled(True)
            self.step_angle.set_status("完成，请确认后应用", "#81c784")
            return

        if line.startswith("CALERR"):
            for s in (self.step_gyro, self.step_wall, self.step_ir, self.step_angle):
                s.set_status(f"设备报错: {line}", "#ef5350")
            return

        ev = parse_event_line(line)
        if ev is not None and ev["event"] == "STOPPED":
            # v2.0: 运动结束只有 `STOPPED <reason> #n` 一条事件行(取代 MOVEDONE ...)。
            self._on_stopped(ev)
            return

    def _on_stopped(self, ev: dict) -> None:
        """处理 `STOPPED <reason> #n`。

        脉冲差改由统一帧的 `trackL/trackR` 在运行前后的差值取得 —— 固件已不再把
        明细量塞进长文本行(那些行会被 TX 空间门静默丢弃)。
        """
        if self._track_start is None:
            return
        reason = ev.get("reason_text") or ev.get("reason") or "?"
        f = self.store.latest() if self.store is not None else {}
        if f:
            self.track_dL = int(f.get("trackL", 0)) - self._track_start[0]
            self.track_dR = int(f.get("trackR", 0)) - self._track_start[1]
            self.lbl_pulses.setText(f"左 {self.track_dL} / 右 {self.track_dR}")
            self.btn_track_apply.setEnabled(True)
        self._track_start = None
        self.step_track.set_status(f"已停车({reason})，请量距离并点击『计算并应用』", "#81c784")

    def _cur(self, name: str) -> str:
        from ..protocol.commands import PARAM_IDS
        from ..protocol.codec import q1616_to_float

        q = self.client.params.get(PARAM_IDS[name])
        return "—" if q is None else f"{q1616_to_float(q):.3f}"

    # ============================================================ 应用

    def _apply_angle(self) -> None:
        if not self.srv_deg or self.srv_sum_acc is None:
            return
        g = self.srv_sum_acc / float(self.srv_deg)
        e = self.srv_sum_enc / float(self.srv_deg)
        for name, val in (("GYRORATIO", g), ("ENCANGLE", e)):
            ok, st = self.client.set_param(name, val, readback=True)
            self.log(f"[CAL] SET {name}={val:.4f} -> {st}")
            if not ok:
                self.step_angle.set_status(f"{name} 写入失败: {st}", "#ef5350")
                return
        self.step_angle.set_status(
            f"已应用: gyroRatio={g:.3f}, encAngleRatio={e:.3f}（记得点 SAVE 固化）", "#81c784"
        )

    def _start_track(self) -> None:
        pulses = self.sp_pulses.value()
        speed = self.sp_speed.value()
        if self.session is not None:
            self.session.write_calib_report(
                [
                    f"## TRACK 标定 {pulses} 脉冲 @ {speed} cm/s",
                    f"- 开始: {time.strftime('%H:%M:%S')}",
                ]
            )
        # 记下运行前的里程基准: 结束事件 STOPPED 只带“为什么停”, 脉冲差走统一帧。
        f = self.store.latest() if self.store is not None else {}
        self._track_start = (int(f.get("trackL", 0)), int(f.get("trackR", 0))) if f else None
        self.step_track.set_status("直线运行中，等待设备停车事件 STOPPED…")
        self._send(f"MOVE FWD {pulses} {speed}")

    def _apply_track(self) -> None:
        if not self.track_dL or not self.track_dR:
            return
        d = float(self.sp_dist.value())
        if d <= 0:
            return
        tl = self.track_dL / d
        tr = self.track_dR / d
        self.lbl_track.setText(f"TRACKL={tl:.4f}  TRACKR={tr:.4f}")
        for name, val in (("TRACKL", tl), ("TRACKR", tr)):
            ok, st = self.client.set_param(name, val, readback=True)
            self.log(f"[CAL] SET {name}={val:.4f} -> {st}")
            if not ok:
                self.step_track.set_status(f"{name} 写入失败: {st}", "#ef5350")
                return
        # 强制同步派生: speedRatio 恒 = trackRatio / 100
        for tr_name, sp_name, val in (("TRACKL", "SPEEDL", tl), ("TRACKR", "SPEEDR", tr)):
            self.client.set_param(sp_name, val / 100.0, readback=True)
        self.step_track.set_status(
            f"已应用 TRACKL={tl:.4f} / TRACKR={tr:.4f}，并派生 SPEED=TRACK/100。\n"
            f"建议复跑验证：走 3 格（540mm）误差应 < 5mm。（记得点 SAVE 固化）",
            "#81c784",
        )
