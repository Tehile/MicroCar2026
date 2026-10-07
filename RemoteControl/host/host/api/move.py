"""开环移动(标定辅助)与远程双护栏运动。

对应功能清单的 F 组里"需要物理动作"的那一半。

这些命令**绕过运动状态机** —— 它们不走路、不纠偏、不看墙, 只是以固定脉冲数
或固定 PWM 驱动一段时间。用途只有两个: 标定里程系数、标定角度系数。

安全提示: `forward` 是开环的, 不会因为前方有墙而停。标定时请放在足够长的
直线通道上, 并保持 `STOP`/`ESTOP` 可达。

v2.0(2026-10-05): 结束事件统一成 `STOPPED <REASON> #n` —— 原先按动作类型分开的
`MOVEDONE FWD/ROT/RUN` 三条明细行全部取消(运动过程已由统一帧逐帧记录, 明细冗余)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Optional

from ..protocol.codec import MOTION_TASK_NAMES
from .errors import Result, clamp_timeout
from .events import Event

if TYPE_CHECKING:  # pragma: no cover
    from .client import MouseClient

#: 估算超时用的保守脉冲率(脉冲/秒)。
_PULSES_PER_SEC_FLOOR = 300.0
#: 粗略的脉冲->毫米换算, 仅用于估时(trackRatio 当前量级 ~53 pulse/mm)。
_PULSE_PER_MM_EST = 53.2


class MoveAPI:
    """固定量开环动作 + 远程双护栏运动。"""

    def __init__(self, mc: "MouseClient") -> None:
        self._mc = mc

    # ------------------------------------------------------------ 估算

    @classmethod
    def estimate_timeout(cls, pulses: int, speed: int = 0) -> float:
        """按脉冲数与速度给一个"足够长"的超时。"""
        dist_cm = abs(int(pulses)) / _PULSE_PER_MM_EST / 10.0
        if speed and speed > 0:
            est = dist_cm / float(speed)
        else:
            est = abs(int(pulses)) / _PULSES_PER_SEC_FLOOR
        return max(15.0, est * 6.0 + 5.0)

    # ------------------------------------------------------------ 动作

    def forward(
        self, pulses: int = 20000, speed: int = 0, wait_done: bool = True,
        timeout: Optional[float] = None,
    ) -> Result:
        """固定脉冲直线(开环)。`wait_done=True` 时等 `STOPPED`。"""
        mc = self._mc
        mc._check_reentrancy("move.forward()")
        guard = mc.motor_guard("move.forward()")
        if guard is not None:
            return guard
        t0 = _now()
        r = mc.send_cmd("move.fwd", pulses=int(pulses), speed=int(speed), timeout=5.0)
        if not r.ok:
            return r
        if not wait_done:
            return Result(True, "OK", {"pulses": int(pulses), "async": True})
        msg = self.wait_stopped(timeout=clamp_timeout(timeout, self.estimate_timeout(pulses, speed)), since=t0)
        if msg is None:
            return Result(False, "TIMEOUT", {"cmd": r.data.get("cmd")})
        return Result(True, "OK", {"pulses": int(pulses), "reason": msg.data.get("reason"),
                                   "raw": msg.data.get("raw")})

    def rotate(
        self, pwm: int = 40, ms: int = 0, wait_done: bool = True,
        timeout: Optional[float] = None,
    ) -> Result:
        """固定速度定长原地转(开环)。`pwm` 实为旋转速度(cm/s), 与前进速度同量级。"""
        mc = self._mc
        mc._check_reentrancy("move.rotate()")
        guard = mc.motor_guard("move.rotate()")
        if guard is not None:
            return guard
        t0 = _now()
        r = mc.send_cmd("move.rot", pwm=int(pwm), ms=int(ms), timeout=5.0)
        if not r.ok:
            return r
        if not wait_done:
            return Result(True, "OK", {"pwm": int(pwm), "ms": int(ms), "async": True})
        wait = clamp_timeout(timeout, (ms / 1000.0 * 4.0 + 5.0) if ms else 20.0)
        msg = self.wait_stopped(timeout=wait, since=t0)
        if msg is None:
            return Result(False, "TIMEOUT", {"cmd": r.data.get("cmd")})
        return Result(True, "OK", {"pwm": int(pwm), "ms": int(ms),
                                   "reason": msg.data.get("reason"), "raw": msg.data.get("raw")})

    def servo(self, act: str = "SWEEP", wait_done: bool = False, timeout: Optional[float] = None) -> Result:
        """前向红外极值扫描(角度基准)。

        注意: 固件没有独立的 `MOVE SERVO` 命令 —— 角度基准扫描由标定命令
        `CAL GYRORATIO START` 触发(见 `mc.calib.angle()`)。保留本方法仅为语义
        便利, 内部转发到标定流程。
        """
        return self._mc.calib.angle(timeout=clamp_timeout(timeout, 90.0))

    # ------------------------------------------------------------ 远程控制(双护栏)

    def run(
        self,
        sp_l: int = 40,
        sp_r: int = 40,
        ms: int = 2000,
        ir_mm: int = 10,
        wait_done: bool = True,
        timeout: Optional[float] = None,
    ) -> Result:
        """受**时间 + 红外**双护栏约束的远程运动(电机远程控制实验主入口)。

        `MOVE RUN <spL> <spR> [ms] [ir_mm]`

        与 `forward()`/`rotate()` 的本质区别: 那两个是"跑完固定量才停", 一旦
        参数写错就只能靠人喊停; 本命令把停止条件做成**有界且自动**的 ——
        到时必停、贴脸必停, 两者都在固件的 1ms ISR 内判定, 上位机卡死/蓝牙断开
        都不影响。

        参数:
            sp_l, sp_r  左右轮目标速度(cm/s)。允许单轮为 0, 不能同时为 0
            ms          最长运行时间, 默认 2000 ms。传 0 表示用固件默认值
            ir_mm       红外停车阈值(mm), 默认 10(=1cm)。**传负值关闭红外护栏**

        返回 Result, `data` 里带: reason / reason_text / raw。
        停止原因见 `codec.RUN_REASON_NAMES`(TIME/IR/ESTOP/USER/END)。
        """
        mc = self._mc
        mc._check_reentrancy("move.run()")
        guard = mc.motor_guard("move.run()")
        if guard is not None:
            return guard
        if mc.estop_locked:
            return Result(
                False,
                "ESTOP_LOCKED",
                {
                    "op": "move.run()",
                    "hint": "设备处于急停锁存状态, MOVE 会被拒绝。先 mc.lab.resume()(即 STOP)解锁。",
                },
            )
        t0 = _now()
        r = mc.send_cmd("move.run", sp_l=int(sp_l), sp_r=int(sp_r), ms=int(ms), ir_mm=int(ir_mm))
        if not r.ok:
            return r
        if not wait_done:
            return Result(True, "OK", {"sp_l": int(sp_l), "sp_r": int(sp_r), "async": True})
        wait = clamp_timeout(timeout, max(5.0, ms / 1000.0 * 4.0 + 5.0))
        msg = self.wait_stopped(timeout=wait, since=t0)
        if msg is None:
            return Result(False, "TIMEOUT", {"cmd": r.data.get("cmd")})
        return Result(True, "OK", {
            "reason": msg.data.get("reason"),
            "reason_text": msg.data.get("reason_text"),
            "seq": msg.data.get("seq"),
            "raw": msg.data.get("raw"),
        })

    def estop(self, timeout: float = 3.0) -> Result:
        """急停(锁存)。立即停车 + 置 `estop_lock`, 之后的 MOVE/CAL 会被拒。"""
        mc = self._mc
        mc._check_reentrancy("move.estop()")
        return mc.send_cmd("estop", timeout=timeout)

    def resume(self, timeout: float = 3.0) -> Result:
        """正常停车并**解除急停锁存**(固件侧就是 `STOP`)。

        测试模式固件里 STOP 兼作"复位急停"; 比赛固件保持原语义(急停后必休眠,
        不接受远程复位)。
        """
        mc = self._mc
        mc._check_reentrancy("move.resume()")
        return mc.send_cmd("estop.clear", timeout=timeout)

    def stop(self, timeout: float = 3.0) -> Result:
        """正常停车(等价于 `resume`, 保留更常用的名字)。"""
        return self.resume(timeout=timeout)

    # ------------------------------------------------------------ 等待

    def wait_stopped(self, reason: Optional[str] = None, timeout: float = 60.0,
                     since: Optional[float] = None):
        """等待 `STOPPED <reason> #n` 事件。返回 EventMsg 或 None。

        `reason` 给定时只在原因匹配时命中(如 "IR"), 否则任意 STOPPED 都算。
        """
        want = (reason or "").upper()

        def _pred(m) -> bool:
            if m.data.get("event") != "STOPPED":
                return False
            return (not want) or str(m.data.get("reason", "")).upper() == want

        return self._mc.wait_event(Event.EVENT, _pred, timeout=timeout, since=since)

    # 兼容旧名(部分脚本仍在用)
    def wait_done(self, what: str = "FWD", timeout: float = 60.0, since: Optional[float] = None):
        return self.wait_stopped(timeout=timeout, since=since)

    # ------------------------------------------------------------ 状态

    def current_task(self) -> Optional[str]:
        """当前运动任务名(来自统一帧), 便于判断"是否已经停下来"。"""
        f = self._mc.store.latest()
        if not f:
            return None
        code = int(f.get("motionTask", 0))
        return MOTION_TASK_NAMES.get(code, str(code))

    def wait_idle(self, timeout: float = 30.0, idle_speed: float = 2.0) -> bool:
        """等到左右轮速都接近 0 —— "车停了"。"""
        def _idle() -> bool:
            f = self._mc.store.latest()
            if not f:
                return False
            return abs(float(f.get("speedL", 99))) <= idle_speed and abs(float(f.get("speedR", 99))) <= idle_speed

        return self._mc.wait(_idle, timeout=timeout)


def _now() -> float:
    import time

    return time.time()
