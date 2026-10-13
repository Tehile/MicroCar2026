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

v2.7(2026-10-14) 按键菜单远程化
-------------------------------
固件 `CAL` 家族新增 6 个**立即 ACK** 的子命令(远程档等车循环也支持 BUTTON1
短按进原菜单), 把按键菜单 2/4/5/6/7/8 的功能搬到链路上:
`ERASEIR` / `IRSELF` / `SENSOR` / `IRCAP <1..4>` / `BLOCK` / `ERASEALL`。
与旧五项"采完才 ACK"是两条不同的流, 语义/边界见下面各方法注释;
擦除类(ERASEIR/ERASEALL)靠 `registry` precheck 要求 `confirm=True`。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple

from .errors import Result, clamp_timeout

if TYPE_CHECKING:  # pragma: no cover
    from .client import MouseClient


class CalibAPI:
    """5 项标定 + v2.7 的 6 个菜单远程化动作。

    旧五项(gyro/wall_distance/ir_data/angle/track)默认阻塞到设备 ACK
    (即采集完成); v2.7 六项(erase_ir/ir_self/sensor/ir_cap/block/erase_all)
    是**立即 ACK** 的维护动作, 语义见各方法。"""

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

    # ------------------------------------------------------------ v2.7 菜单远程化

    #: v2.7 由按键菜单远程化而来的子命令(与上面五项**不同流**: 全部**立即 ACK**,
    #: 不等采集; 均仅远程档 MOUSE_REMOTE, 运动中/标定中一律 ERR_BUSY)
    MENU_ITEMS: Tuple[str, ...] = (
        "ERASEIR", "IRSELF", "SENSOR", "IRCAP", "BLOCK", "ERASEALL",
    )

    #: `CAL IRCAP` 的 4 个摆位(顺序必须与固件一致: 1 左近墙 / 2 左远墙 /
    #: 3 前近墙 / 4 前远墙; 第 4 步顺带擦页写 8 个半字)
    IRCAP_STEPS: Tuple[Tuple[str, str], ...] = (
        ("左近墙", "车左侧贴墙, 车心到墙约 33mm"),
        ("左远墙", "车左侧离墙约 94mm"),
        ("前近墙", "车头距墙约 32mm"),
        ("前远墙", "车头距墙约 213mm"),
    )

    def erase_ir(self, confirm: bool = False, timeout: float = 8.0) -> Result:
        """擦除红外标定页(page123) —— 按键菜单 2 的远程化(`CAL ERASEIR`)。

        ⚠️ **不可逆**: Flash 里的红外 8 值立即作废(内存中的 `irCalib` 与
        `dataf1..datar` 查表要等下次上电 `CALIB_InitIr` 才重载; 页为空时
        它回落到内置默认近/远电压)。固件侧**没有**二次确认, 所以上位机
        要求显式 `confirm=True` —— 不传则登记表 precheck 直接拒绝, 返回
        `NEED_CONFIRM`(此时应二次弹窗/询问用户, 再带 confirm=True 重调)。
        """
        r = self._mc.send_cmd(
            "calib.cal", item="ERASEIR", act=None, confirm=bool(confirm),
            timeout=clamp_timeout(timeout, 8.0),
        )
        return self._ok("ERASEIR", r)

    def ir_self(self, timeout: float = 8.0) -> Result:
        """红外自检(**仅传感器段**) —— 按键菜单 4 的远程化(`CAL IRSELF`)。

        原菜单后半段"前向开电机扫 FORWARDIRTEST"**不远程化**: 远程已有
        `MOVE RUN` 的双护栏(时间+红外), 没必要复刻一套无护栏开环动作。
        ACK 附加字段 = `OK <l> <r> <ref>`(左右红外距离 / 车心到墙参考值),
        解析后放在返回 data 的 `ir_l / ir_r / ref` 里。
        """
        r = self._mc.send_cmd(
            "calib.cal", item="IRSELF", act=None, timeout=clamp_timeout(timeout, 8.0),
        )
        if not r.ok:
            return r
        nums = _ints(r.status)
        data: Dict[str, Any] = {"item": "IRSELF", "status": r.status}
        if len(nums) >= 3:
            data.update(ir_l=nums[0], ir_r=nums[1], ref=nums[2])
        return Result(True, "OK", data)

    def sensor(self, timeout: float = 8.0) -> Result:
        """显示传感器数据(固定 4 行) —— 按键菜单 5 的远程化(`CAL SENSOR`)。

        原菜单靠按键切两屏(编码器/红外/陀螺); 远程一次给全, 固件 printf 打
        4 行。⚠️ 这些值**不在 ACK 里**(只在串口日志), ACK 仅表示"已打印" ——
        需要结构化取值请用统一遥测帧(`trackL/trackR`/红外/陀螺通道)。
        """
        r = self._mc.send_cmd(
            "calib.cal", item="SENSOR", act=None, timeout=clamp_timeout(timeout, 8.0),
        )
        return self._ok("SENSOR", r)

    def ir_cap(self, step: int, timeout: float = 12.0) -> Result:
        """红外 8 值分步标定的第 `step` 步(1..4) —— 按键菜单 6 的远程化。

        每一步即原来"按一下按键确认"的远程替代。两条固件规矩:

        1. **必须从 1 开始顺序调**(每步摆位不同, 跳步会把数记到错位置);
           第 1 步随时可重发(等于中止重标)。违规回 `ERR_VALUE`。
        2. 第 1 步前 IRDATA 页必须为全 FF —— 已标定过就**先 `erase_ir()`**
           (远程版新增的安全闸, 菜单 6 本身没有这条)。违规回 `ERR_BUSY`。

        每步**立即 ACK**(不等采集), 附加字段 = `OK <a> <b> <step>`; 第 4 步
        顺带擦页并按读序写 8 个半字, 序列随之复位。
        """
        s = int(step)
        if s < 1 or s > 4:
            return Result(False, "ERR_VALUE",
                          {"item": "IRCAP", "step": s, "hint": "step 必须是 1..4"})
        r = self._mc.send_cmd(
            "calib.cal", item="IRCAP", act=s, timeout=clamp_timeout(timeout, 12.0),
        )
        if not r.ok:
            return r
        nums = _ints(r.status)
        data: Dict[str, Any] = {"item": "IRCAP", "step": s, "status": r.status}
        if len(nums) >= 3:
            data.update(a=nums[0], b=nums[1], step_done=nums[2])
        return Result(True, "OK", data)

    def ir_cap_all(
        self,
        on_prompt: Optional[Callable[[int, str, str], bool]] = None,
        erase_first: bool = False,
        timeout_per_step: float = 12.0,
    ) -> Result:
        """4 步一次跑完(`ir_cap` 的顺序封装)。

        `on_prompt(i, name, desc)` 在摆第 i 点前调用, 返回 False 即中止。
        `erase_first=True` 时先发 `CAL ERASEIR`(**毁掉旧标定**, 与
        "菜单 2 → 菜单 6"的手动流程一致); 默认 False —— 页非空时第 1 步会
        回 `ERR_BUSY`, 此时应提示用户显式选择 `erase_first=True`。
        """
        mc = self._mc
        mc._check_reentrancy("calib.ir_cap_all()")
        if erase_first:
            r = self.erase_ir(confirm=True)
            if not r.ok:
                return Result(False, r.status, {"item": "IRCAP", "at": "erase_ir",
                                                "detail": r.data})
        done: List[int] = []
        for i, (name, desc) in enumerate(self.IRCAP_STEPS, 1):
            if on_prompt is not None:
                try:
                    if not on_prompt(i - 1, name, desc):
                        return Result(False, "ABORTED", {"collected": done})
                except Exception:
                    return Result(False, "PROMPT_ERROR", {"collected": done})
            rs = self.ir_cap(i, timeout=timeout_per_step)
            if not rs.ok:
                return Result(False, rs.status, {"collected": done, "at": name})
            done.append(i)
        return Result(True, "OK", {"item": "IRCAP", "points": len(done)})

    def block(self, timeout: float = 8.0) -> Result:
        """块数据概览 —— 按键菜单 7 的远程化(`CAL BLOCK`)。

        固件先把 Flash 里的墙图**重载进 `mazeState`** 再回 ACK: 随后发
        `maze.dump`(`MAP DUMP`) 拿到的就是 Flash 里的图(否则是内存旧图)。
        ACK 附加字段 = `OK <path45 总项数> <path90 总项数> <红外有效值个数(/8)>`
        (去/回路径各数前 200 项里非 0xFFFF 的条数; 起终点与墙图本身只在
        printf 行里, 结构化取墙图请走 `maze.dump`)。原菜单打印上千行,
        远程只给概览 —— 这是与菜单 7 的有意差异。
        """
        r = self._mc.send_cmd(
            "calib.cal", item="BLOCK", act=None, timeout=clamp_timeout(timeout, 8.0),
        )
        if not r.ok:
            return r
        nums = _ints(r.status)
        data: Dict[str, Any] = {"item": "BLOCK", "status": r.status}
        if len(nums) >= 3:
            data.update(path45=nums[0], path90=nums[1], ir_valid=nums[2])
        return Result(True, "OK", data)

    def erase_all(self, confirm: bool = False, timeout: float = 12.0) -> Result:
        """擦除迷宫/路径 6 页 —— 按键菜单 8 的远程化(`CAL ERASEALL`)。

        擦 `BLOCK / BLOCK_POINT / GOPATH / BACKPATH / GOPATH_90 / BACKPATH_90`
        6 页。与菜单 8 的两点**有意差异**:

        1. 远程版擦完**返回值守、不休眠**(菜单 8 之后进流水灯死循环 ——
           远程设备睡下去就失联了);
        2. **不擦** page101-114 录制区(`FLASH_EraseRunData()` 的 13 页)——
           那是历史采集数据, 要清请单独提需求。

        6 页擦除约 120-240ms(SysTick 上下文, 与 `CAL GYRO` 落盘同量级),
        只在车静止时用 —— 运动中/标定中固件回 `ERR_BUSY`。
        ⚠️ 不可逆, 需显式 `confirm=True`(否则 precheck 返回 `NEED_CONFIRM`)。
        """
        r = self._mc.send_cmd(
            "calib.cal", item="ERASEALL", act=None, confirm=bool(confirm),
            timeout=clamp_timeout(timeout, 12.0),
        )
        return self._ok("ERASEALL", r)

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
