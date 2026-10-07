"""上位机可编程接口(门面层)。**不依赖任何 GUI 框架。**

快速开始::

    from host.api import MouseClient, Event

    mc = MouseClient()
    if not mc.connect("COM7"):
        raise SystemExit(mc.state_detail)          # 失败原因可读
    try:
        mc.telemetry.wait_first_frame(timeout=5)
        print(mc.params.get_all())
        mc.params.set("TRACKL", 53.2015)           # 含 ACK + 回读校验
        rec = mc.telemetry.latest()                # 统一帧最新值(扁平 dict)
        print(mc.telemetry.summary_text())
    finally:
        mc.close()

上下文管理器写法(退出时自动停拉取 + 收会话)::

    with MouseClient.connect_to("tcp://127.0.0.1:5555") as mc:   # 模拟器
        ...

无硬件时: 用 `replay://<会话目录>` 回放已录制的 `raw.bin`, 或连模拟器 TCP。

线程契约见 `host/api/client.py` 的模块 docstring。
"""

from .analyze import AnalyzeAPI
from .calc import (
    IrErrorStats,
    StepMetrics,
    StraightMetrics,
    error_ratio,
    ir_error_stats,
    pick_best_position,
    solve_ratio,
    step_metrics,
    straightness_metrics,
    theory_gyro_ratio,
    theory_track_ratio,
)
from .calib import CalibAPI
from .client import MouseClient, coerce_target
from .conn import ConnAPI
from .errors import ReentrancyError, Result, clamp_timeout
from .events import Event, EventBus, EventMsg
from .lab import MotorLabAPI, RunResult
from .maze import MazeAPI
from .move import MoveAPI
from .params import ParamsAPI
from .session_api import SessionAPI
from .stream import StreamAPI
from .telemetry import TelemetryAPI
from .timesync import (
    TimeSyncAPI,
    TimeSyncState,
    drift_ppm,
    fit_clock,
    min_delay_offset,
    resolve_ms8,
)

__all__ = [
    "MouseClient",
    "coerce_target",
    "Event",
    "EventBus",
    "EventMsg",
    "Result",
    "ReentrancyError",
    "clamp_timeout",
    "AnalyzeAPI",
    "CalibAPI",
    "ConnAPI",
    "MazeAPI",
    "MotorLabAPI",
    "RunResult",
    # 标定算参(纯函数, 见 api/calc.py)
    "StepMetrics",
    "StraightMetrics",
    "IrErrorStats",
    "step_metrics",
    "straightness_metrics",
    "ir_error_stats",
    "pick_best_position",
    "solve_ratio",
    "error_ratio",
    "theory_track_ratio",
    "theory_gyro_ratio",
    "MoveAPI",
    "ParamsAPI",
    "SessionAPI",
    "StreamAPI",
    "TelemetryAPI",
    "TimeSyncAPI",
    "TimeSyncState",
    "min_delay_offset",
    "fit_clock",
    "drift_ppm",
    "resolve_ms8",
]
