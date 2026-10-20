"""实时状态面板 —— 统一帧(约 217 Hz 全量快照)的当前值读数。

v2.0 起上行只有统一帧一路(39 通道全量快照), 因此这里直接读最新一帧的平铺字段;
不再有 fast/mid/slow 三流, 也没有停车后的 1kHz 回传 —— 需要历史/导出时见右侧
『统一帧查看』面板(FetchPanel)。
"""

from __future__ import annotations

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..model.ring import TelemetryStore
from ..protocol.codec import MOTION_TASK_NAMES

# 主题色(深色)
C_L = "#4FC3F7"   # 左轮
C_R = "#FFB74D"   # 右轮
C_OK = "#81C784"  # 正常
C_WARN = "#FFD54F"  # 注意
C_BAD = "#E57373"  # 异常
C_DIM = "#9e9e9e"


class _Field:
    """一行'名称 + 数值(可选单位)'。数值用等宽大字。"""

    def __init__(self, name: str, unit: str = "", width: int = 0) -> None:
        self.name = QLabel(name)
        self.name.setStyleSheet(f"color:{C_DIM};")
        self.value = QLabel("—")
        f = QFont("Consolas")
        f.setPointSize(11)
        f.setBold(True)
        self.value.setFont(f)
        self.value.setStyleSheet("color:#e0e0e0;")
        self.unit = QLabel(unit)
        self.unit.setStyleSheet(f"color:{C_DIM};")
        if width:
            self.value.setMinimumWidth(width)

    def set(self, text: str, color: str = "#e0e0e0") -> None:
        self.value.setText(text)
        self.value.setStyleSheet(f"color:{color};")


class _Card(QFrame):
    """带标题的卡片容器。"""

    def __init__(self, title: str) -> None:
        super().__init__()
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "QFrame { border: 1px solid #3a3a3a; border-radius: 4px; background: #232323; }"
        )
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(10, 8, 10, 10)
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(4)
        t = QLabel(title)
        t.setStyleSheet("color:#bdbdbd; font-weight:600; border: none;")
        self.grid.addWidget(t, 0, 0, 1, 4)
        self.row = 1

    def add(self, field: _Field, col: int = 0, span: int = 4) -> None:
        self.grid.addWidget(field.name, self.row, col)
        self.grid.addWidget(field.value, self.row, col + 1)
        if field.unit is not None:
            self.grid.addWidget(field.unit, self.row, col + 2)
        self.row += 1


class StatusPanel(QWidget):
    """由主窗口定时器驱动 refresh()。"""

    def __init__(self, store: TelemetryStore) -> None:
        super().__init__()
        self.store = store

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(8)

        # 顶部提示: 说明数据来源
        hint = QLabel("统一帧约 217 Hz、39 通道全量快照 —— 实时页给出当前值读数;"
                      " 历史与导出见右侧『统一帧查看』")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{C_WARN}; padding:2px;")
        root.addWidget(hint)

        # ---------- 运动 ----------
        self.c_speed = _Card("运动")
        self.f_speedL = _Field("左轮速度", "cm/s")
        self.f_speedR = _Field("右轮速度", "cm/s")
        self.f_angle = _Field("陀螺航向", "deg")
        self.f_err = _Field("pidErr", "")
        for f in (self.f_speedL, self.f_speedR, self.f_angle, self.f_err):
            self.c_speed.add(f)

        # ---------- 状态 ----------
        self.c_state = _Card("状态")
        self.f_task = _Field("任务", "")
        self.f_pos = _Field("坐标", "")
        self.f_mode = _Field("模式", "")
        for f in (self.f_task, self.f_pos, self.f_mode):
            self.c_state.add(f)

        # ---------- 传感器 ----------
        self.c_sense = _Card("红外 / 墙")
        self.f_irF = _Field("前向 f1/f2", "mm", 96)
        self.f_irLR = _Field("左 / 右", "mm", 96)
        self.f_wall = _Field("墙位", "")
        for f in (self.f_irF, self.f_irLR, self.f_wall):
            self.c_sense.add(f)

        # ---------- 系统 ----------
        self.c_sys = _Card("系统")
        self.f_volt = _Field("电池电压", "V")
        self.f_drop = _Field("红外采样", "")
        self.f_lost = _Field("上行丢失", "")
        for f in (self.f_volt, self.f_drop, self.f_lost):
            self.c_sys.add(f)

        for c in (self.c_speed, self.c_state):
            root.addWidget(c)
        for c in (self.c_sense, self.c_sys):
            root.addWidget(c)
        root.addStretch(1)

    # ------------------------------------------------------------ 刷新

    @staticmethod
    def _speed_color(v: float) -> str:
        """速度读数着色: 基本静止用暗色, 转动中用量亮色。"""
        return C_OK if abs(v) >= 0.5 else C_DIM

    def refresh(self) -> None:
        f = self.store.latest()
        if not f:
            return

        sl = float(f.get("speedL", 0))
        sr = float(f.get("speedR", 0))
        self.f_speedL.set(f"{sl:+6.1f}", self._speed_color(sl))
        self.f_speedR.set(f"{sr:+6.1f}", self._speed_color(sr))

        ang = float(f.get("gyroAngle", 0))
        self.f_angle.set(f"{ang:+7.1f}")

        err = f.get("pidErr", 0)
        try:
            errf = float(err)
        except (TypeError, ValueError):
            errf = 0.0
        self.f_err.set(f"{errf:+7.1f}",
                       C_OK if abs(errf) < 3 else (C_WARN if abs(errf) < 10 else C_BAD))

        task = int(f.get("motionTask", 0))
        self.f_task.set(MOTION_TASK_NAMES.get(task, str(task)))

        mode = []
        if f.get("motors_off"):
            mode.append("电机禁用")
        if f.get("estop_lock"):
            mode.append("急停锁存")
        elif f.get("emerg"):
            mode.append("急停")
        if f.get("freemode"):
            mode.append("自由模式")
        if f.get("sleep"):
            mode.append("休眠")
        if f.get("evq"):
            mode.append("事件流不完整")
        self.f_mode.set(" / ".join(mode) if mode else "运行",
                        C_BAD if (f.get("emerg") or f.get("sleep")) else C_OK)

        volt = float(f.get("battMv", 0)) / 1000.0
        self.f_volt.set(f"{volt:5.2f}",
                        C_BAD if volt < 6.5 else (C_WARN if volt < 7.0 else C_OK))

        self.f_irF.set(f"{int(f.get('irF1', 0)):4d}/{int(f.get('irF2', 0)):4d}")
        self.f_irLR.set(f"{int(f.get('irL', 0)):4d}/{int(f.get('irR', 0)):4d}")
        bits = [("左", int(f.get("wallLeft", 0))), ("右", int(f.get("wallRight", 0))),
                ("前", int(f.get("wallFront", 0))), ("远", int(f.get("frontFar", 0))),
                ("互", int(f.get("frontBoth", 0)))]
        self.f_wall.set(" ".join(f"{n}{'●' if v else '○'}" for n, v in bits))

        irsw = bool(f.get("irSwitch"))
        self.f_drop.set("开" if irsw else "关", C_OK if irsw else C_WARN)
        lost = int(self.store.event_health().get("up_lost", 0))
        self.f_lost.set(f"{lost:5d}", C_OK if lost == 0 else C_WARN)

        self.f_pos.set(f"({int(f.get('posX', 0)):2d},{int(f.get('posY', 0)):2d})")

    # ------------------------------------------------------------ 供外部读取

    def snapshot_text(self) -> str:
        """一行摘要, 供状态栏/日志使用。"""
        f = self.store.latest()
        if not f:
            return "—"
        return (f"L {f.get('speedL', 0):+.1f} R {f.get('speedR', 0):+.1f} "
                f"θ {f.get('gyroAngle', 0):+.1f} V {f.get('battMv', 0) / 1000:.2f} "
                f"task {f.get('motionTaskName', '?')}")
