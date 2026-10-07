"""标定: 5 项物理系数(**请求-响应** 交互模型)。

对应功能清单的 F 组。

与设计文档约定的两条硬边界:

  1. 只标**物理系数**(陀螺零偏 / 红外 8 值 / 车心到墙 / 角度系数 / 里程系数)。
     PID 增益、三档 spurt 增益、速度常数、墙判阈值均属项目红线, 不在这里。
  2. `gyroRatio` 与 `encAngleRatio` **不能互相标定** —— 那是循环依赖, 误差会
     互相吸收。本实现用『前向红外正对墙时取极小值』作为物理角度基准。

v2.0(2026-10-05) 交互模型变更
----------------------------
固件不再上行 `CALSTEP` / `CALDONE` / `CALERR` 事件行 —— 那些行会被 TX 空间门
静默丢弃, 靠不住。现在 **`CAL` 是请求-响应**:

    START 命令到达 -> 固件开始采集, **不立刻 ACK**;
    采够/超时 -> 回 `ACK <seq> OK`(或 `ERR_TIMEOUT`)。

于是"ACK 到达"就等于"这一步完成, 请摆下一个位置"。结果值通过 `GET` 回读;
角度标定的三个数(sumAcc/sumEnc/deg)直接放在 ACK 的附加字段里
(`ACK <seq> OK <a> <b> <c>`, 见固件 `link_AckI3`)。

因为一次采集可能数秒, `CAL` 归入 `no_retry`(不重发), 上位机超时给到 10 s 以上。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple

from .errors import Result, clamp_timeout

if TYPE_CHECKING:  # pragma: no cover
    from .client import MouseClient


class CalibAPI:
    """5 项标定。全部方法默认阻塞到设备 ACK(即采集完成)。"""

    #: 固件支持的标定项(与 `CAL <item>` 的 item 一致)
    ITEMS: Tuple[str, ...] = ("GYRO", "WALLDIST", "IRDATA", "GYRORATIO", "ENCANGLE")

    #: 无需电机即可完成的项 —— 安全测试固件(MOUSE_TEST_NO_MOTOR)下**可以**做
    STATIC_ITEMS: Tuple[str, ...] = ("GYRO", "WALLDIST", "IRDATA")

    #: 需要电机转动的项 —— 安全模式下 `mc.motor_guard()` 会直接拦下(不空等超时)
    MOTION_ITEMS: Tuple[str, ...] = ("GYRORATIO", "ENCANGLE")

    #: 红外逐点采集的 4 个摆位, 顺序必须与固件一致(`CAL IRDATA <0..3>`)
    IR_STEPS: Tuple[Tuple[str, str], ...] = (
        ("左近墙", "车左侧贴墙, 车心到墙约 33mm"),
        ("左远墙", "车左侧离墙约 94mm"),
        ("前近墙", "车头距墙约 32mm"),
        ("前远墙", "车头距墙约 213mm"),
    )

    def __init__(self, mc: "MouseClient") -> None:
        self._mc = mc

    # ------------------------------------------------------------ 基础

    def items(self) -> List[str]:
        return list(self.ITEMS)

    def step(self, item: str, act: Any = "START", timeout: float = 12.0) -> Result:
        """手工单步: `CAL <item> <act>`。

        act ∈ {START, ABORT}, 或 IRDATA 的相位号 0..3。
        **这是请求-响应**: 返回时采集已完成(或超时)。
        """
        return self._mc.send_cmd(
            "calib.cal", item=str(item).upper(), act=act,
            timeout=clamp_timeout(timeout, 12.0),
        )

    @staticmethod
    def _ok(item: str, r: Result, **extra: Any) -> Result:
        if not r.ok:
            return r
        return Result(True, "OK", {"item": item, "status": r.status, **extra})

    # ------------------------------------------------------------ 逐项

    def gyro(self, timeout: float = 25.0) -> Result:
        """陀螺零偏。要求电脑鼠**静止**; 固件自行采样约 1.5s 并写入既有 Flash 页。

        结果(零偏)写在 `FLASH_ADDR_GYRO0`, **不是参数**, 因此没有回读 ——
        ACK 即表示已落盘。
        """
        r = self.step("GYRO", "START", timeout=timeout)
        return self._ok("GYRO", r)

    def wall_distance(self, timeout: float = 25.0) -> Result:
        """车心到墙距离。要求电脑鼠**平行贴住左墙**。结果写 WALLDIST 参数, 回读之。"""
        r = self.step("WALLDIST", "START", timeout=timeout)
        if not r.ok:
            return r
        val = self._mc.params.get("WALLDIST")
        return self._ok("WALLDIST", r, value=val)

    # ------------------------------------------------------------ 红外逐点

    def ir_start(self) -> Result:
        """开始红外逐点: 采第 0 个相位(左近墙)。"""
        return self.step("IRDATA", 0, timeout=12.0)

    def ir_sample(self, phase: int) -> Result:
        """采集相位 `phase` ∈ 0..3(每一相位 = 一条命令, 采完才 ACK)。"""
        return self.step("IRDATA", int(phase), timeout=12.0)

    def ir_data(
        self,
        on_prompt: Optional[Callable[[int, str, str], bool]] = None,
        timeout_per_point: float = 30.0,
    ) -> Result:
        """红外 8 值(4 个摆位各采一点)。

        `on_prompt(i, name, desc)` 在需要摆放第 i 点时调用, 返回 False 即中止。
        固件端**逐相位采集**: 发 `CAL IRDATA <i>` 后, 采够就回 ACK, 上位机再摆
        下一个位置、发下一条。

        **注意**: 固件的 `CALIB_InitIr()` 会擦除已探索的迷宫, 所以这里走的是
        逐点 `CAL IRDATA <n>` 路径, 不重建查表。
        """
        mc = self._mc
        mc._check_reentrancy("calib.ir_data()")
        done: List[int] = []
        for i, (name, desc) in enumerate(self.IR_STEPS):
            if on_prompt is not None:
                try:
                    if not on_prompt(i, name, desc):
                        self.step("IRDATA", "ABORT", timeout=5.0)
                        return Result(False, "ABORTED", {"collected": len(done)})
                except Exception:
                    self.step("IRDATA", "ABORT", timeout=5.0)
                    return Result(False, "PROMPT_ERROR", {"collected": len(done)})
            rs = self.step("IRDATA", i, timeout=clamp_timeout(timeout_per_point, 30.0))
            if not rs.ok:
                return Result(False, rs.status, {"collected": len(done), "at": name})
            done.append(i)
        return Result(True, "OK", {"item": "IRDATA", "points": len(done)})

    # ------------------------------------------------------------ 角度系数

    def angle(
        self, timeout: float = 120.0, apply: bool = True, readback: bool = True
    ) -> Result:
        """角度系数 GYRORATIO / ENCANGLE。

        要求电脑鼠放在**角格**(相邻两面墙), 车头大致朝前。固件会自己往复
        0°↔90° 若干次; 结束时的 ACK 带三个数: `ACK <seq> OK <sumAcc> <sumEnc> <deg>`。
        `apply=True` 时把算得的两个系数直接写回设备。

        返回的 `data` 里带 `sum_acc / sum_enc / deg / gyroRatio / encAngleRatio`。
        """
        mc = self._mc
        mc._check_reentrancy("calib.angle()")
        guard = mc.motor_guard("calib.angle()")
        if guard is not None:
            return guard
        r = self.step("GYRORATIO", "START", timeout=clamp_timeout(timeout, 120.0))
        if not r.ok:
            return r
        nums = _ints(r.status)
        # status 形如 "OK <sumAcc> <sumEnc> <deg>"; 去掉 "OK" 后的三个数
        if len(nums) < 3:
            return Result(False, "BAD_REPLY", {"item": "GYRORATIO", "status": r.status})
        sum_acc, sum_enc, deg = nums[0], nums[1], nums[2]
        if deg == 0:
            return Result(False, "ZERO_DEG", {"item": "GYRORATIO", "status": r.status})
        g = sum_acc / float(deg)
        e = sum_enc / float(deg)
        info: Dict[str, Any] = {
            "item": "GYRORATIO",
            "sum_acc": sum_acc, "sum_enc": sum_enc, "deg": deg,
            "gyroRatio": g, "encAngleRatio": e,
        }
        if apply:
            applied = self.apply_angle(g, e, readback=readback)
            info["applied"] = [a.status for a in applied]
            if not all(a.ok for a in applied):
                return Result(False, "APPLY_FAILED", info)
        return Result(True, "OK", info)

    def apply_angle(self, gyro_ratio: float, enc_angle: float, readback: bool = True) -> List[Result]:
        """写回两个角度系数。"""
        return [
            self._mc.params.set("GYRORATIO", float(gyro_ratio), readback=readback),
            self._mc.params.set("ENCANGLE", float(enc_angle), readback=readback),
        ]

    # ------------------------------------------------------------ 里程系数

    def run_straight(self, pulses: int = 20000, speed: int = 40, timeout: Optional[float] = None) -> Result:
        """开环跑一段固定脉冲的直线, 返回停止原因(见 `codec.RUN_REASON_NAMES`)。

        运行前后的轮子里程差可由统一帧的 `trackL/trackR` 取得 —— 固件已把
        `MOVEDONE ... dL dR` 明细行取消, 因此这里不再直接给 dL/dR。
        """
        return self._mc.move.forward(pulses=pulses, speed=speed, wait_done=True, timeout=timeout)

    @staticmethod
    def compute_track(dL: int, dR: int, measured_mm: float) -> Tuple[float, float]:
        """由『实际跑出的脉冲数』与『尺量距离』算里程系数: 新系数 = 脉冲 / 毫米。"""
        if measured_mm <= 0:
            raise ValueError("measured_mm 必须为正")
        return float(dL) / float(measured_mm), float(dR) / float(measured_mm)

    def apply_track(
        self,
        dL: int,
        dR: int,
        measured_mm: float,
        derive_speed: bool = True,
        readback: bool = True,
    ) -> List[Result]:
        """应用里程系数, 默认同时派生 SPEEDL/R = TRACKL/R ÷ 100。

        强制派生是关键: 两者是同一物理量的两种表达, 各自独立标定会出现
        "里程准了但速度指令偏高 3%" 这种隐性不一致。
        """
        tl, tr = self.compute_track(dL, dR, measured_mm)
        return self._mc.params.set_many(
            {"TRACKL": tl, "TRACKR": tr}, readback=readback, derive_speed=derive_speed
        )

    def track(
        self,
        pulses: int = 20000,
        speed: int = 40,
        measured_mm: Optional[float] = None,
        dL: Optional[int] = None,
        dR: Optional[int] = None,
        derive_speed: bool = True,
        timeout: Optional[float] = None,
    ) -> Result:
        """一步到位: 跑直线 → (给了实测距离与里程差就) 计算并应用。

        物理上没法省掉那把尺子, 也没法省掉"实际脉冲差"—— v2.0 起后者不再由
        固件在结束事件里给出, 请用统一帧的 `trackL/trackR` 差值, 通过 `dL/dR` 传入。
        """
        r = self.run_straight(pulses=pulses, speed=speed, timeout=timeout)
        if not r.ok:
            return r
        info: Dict[str, Any] = {"pulses": pulses, "speed": speed, "dL": dL, "dR": dR}
        if measured_mm is None or not dL or not dR:
            return Result(True, "OK", {**info, "applied": False})
        applied = self.apply_track(dL, dR, measured_mm, derive_speed=derive_speed)
        info["applied"] = [a.status for a in applied]
        tl, tr = self.compute_track(dL, dR, measured_mm)
        info["TRACKL"] = tl
        info["TRACKR"] = tr
        if not all(a.ok for a in applied):
            return Result(False, "APPLY_FAILED", info)
        return Result(True, "OK", info)

    def apply_angle_and_track(
        self, gyro_ratio: float, enc_angle: float, dL: int, dR: int, measured_mm: float
    ) -> List[Result]:
        """把角度与里程两项一次写回(界面上"应用"按钮的语义)。"""
        out = self.apply_angle(gyro_ratio, enc_angle)
        out.extend(self.apply_track(dL, dR, measured_mm))
        return out

    # ------------------------------------------------------------ 报告

    def report(self, lines: Sequence[str]) -> Optional[str]:
        """把标定过程写入 `calib/report.md`(会话未开则为 None)。"""
        return self._mc.session.report(list(lines))


def _ints(s: Any) -> List[int]:
    """从状态串里抽出所有整数(跳过 "OK" 之类的非数字 token)。"""
    out: List[int] = []
    for tok in str(s or "").split():
        try:
            out.append(int(tok))
        except ValueError:
            continue
    return out
