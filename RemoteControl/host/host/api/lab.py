"""电机远程控制实验(单流)。

对应阶段: **电机的远程控制实验**(电脑鼠轮子悬空, 只验证"命令 -> 电机 -> 遥测"这条链)。

## v2.0 起为什么不再需要"双流"

v1.5.5 时这条链路被认为只有 ~152 B/s 的可持续吞吐, 于是"实时流(低 Hz 帧)"与
"细节流(停车后 1kHz 整块回传)"在物理上不能同时要。该假设**已在真机证伪**
(SCOPE DUMP 实测 1.38 KB/s 未堵死), 且固件已改**自时钟发送**: 统一帧按物理速率
全速上行(≈217 Hz, 每帧都是全量快照) —— 运动进行中就有 217 个点/秒,
不需要停下来再回传。因此 `SCOPE`/`DUMP` 机制整体删除。

## 谁在保证"停止"

护栏**全部在固件的 1ms ISR 里**(`link_MoveService`), 不在本模块:

    时间护栏   到时(默认 2000 ms)无条件停车
    红外护栏   任一红外读数 < 阈值(默认 10 mm)立即停车
    急停       锁存式, 之后的 MOVE/CAL 一律 ERR_ESTOP, 必须显式 STOP 解锁

上位机这一侧**只负责观察**: 下发命令、把运动期间的统一帧采样下来。即使本进程
被 kill, 电机照样会按时停。停止原因由固件上行的 `STOPPED <reason> #n` 事件给出。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .errors import Result, clamp_timeout
from .events import Event

if TYPE_CHECKING:  # pragma: no cover
    from .client import MouseClient

#: 统一帧的线上字节数(45B 载荷 + 8B 开销)
_FRAME_BYTES = 53

#: 红外四路的通道名(与固件 link_MinIr 的顺序一致)
_IR_CHANNELS = ("F1", "F2", "L", "R")


@dataclass
class RunResult:
    """一次带护栏的远程运动的结果。"""

    ok: bool = False
    status: str = ""
    # ---- 来自固件 `STOPPED <reason> #n` ----
    reason: str = ""
    reason_text: str = ""
    seq: Optional[int] = None
    # ---- 运动期间采到的统一帧样本 ----
    realtime: Dict[str, Any] = field(default_factory=dict)
    samples: List[Dict[str, Any]] = field(default_factory=list)
    # ---- 其它 ----
    warning: str = ""
    error: str = ""
    raw: str = ""

    def to_dict(self, with_samples: bool = False) -> Dict[str, Any]:
        d = {
            "ok": self.ok,
            "status": self.status,
            "reason": self.reason,
            "reason_text": self.reason_text,
            "seq": self.seq,
            "realtime": self.realtime,
        }
        if self.warning:
            d["warning"] = self.warning
        if self.error:
            d["error"] = self.error
        if self.raw:
            d["raw"] = self.raw
        if with_samples:
            d["samples"] = self.samples
        return d


class MotorLabAPI:
    """电机远程控制实验(门面 `mc.lab`)。"""

    def __init__(self, mc: "MouseClient") -> None:
        self._mc = mc

    # ------------------------------------------------------------ 状态

    def status(self) -> Dict[str, Any]:
        """一眼看清"现在能不能动"。"""
        mc = self._mc
        f = mc.telemetry.latest()
        return {
            "connected": mc.is_connected,
            "motor_disabled": mc.motor_disabled,
            "estop_locked": mc.estop_locked,
            "emerg": bool(f.get("emerg", False)),
            "freemode": bool(f.get("freemode", False)),
            "events_incomplete": mc.events_incomplete,
            "ir": {ch: f.get("ir" + ch) for ch in _IR_CHANNELS},
            "ir_min": self._ir_min(),
            "can_move": (mc.is_connected and mc.motor_disabled is False and not mc.estop_locked),
        }

    def _ir_min(self) -> Optional[int]:
        """当前四路红外的最小值(mm)。没收到帧时返回 None。"""
        f = self._mc.telemetry.latest()
        if not f:
            return None
        vals = [f.get("irF1"), f.get("irF2"), f.get("irL"), f.get("irR")]
        vals = [int(v) for v in vals if v is not None]
        return min(vals) if vals else None

    # ------------------------------------------------------------ 急停

    def estop(self, timeout: float = 3.0) -> Result:
        """急停(锁存)。**随时可用**, 不依赖任何先决条件。"""
        return self._mc.move.estop(timeout=timeout)

    def resume(self, timeout: float = 3.0) -> Result:
        """解除急停锁存并正常停车(固件侧 = `STOP`)。"""
        return self._mc.move.resume(timeout=timeout)

    # ------------------------------------------------------------ 远程运动实验

    def run(
        self,
        sp_l: int = 40,
        sp_r: int = 40,
        ms: int = 2000,
        ir_mm: int = 10,
        sample_hz: float = 50.0,
        on_sample: Optional[Dict[str, Any]] = None,
    ) -> RunResult:
        """跑一次带双护栏的远程运动, 并采下运动期间的统一帧样本。

        参数:
            sp_l, sp_r   左右轮速度(cm/s)
            ms           最长运行时间(ms), 护栏之一
            ir_mm        红外停车阈值(mm), 护栏之二; 负值关闭红外护栏
            sample_hz    对最新快照的采样频率(默认 50 Hz, 用于回调/落盘)
            on_sample    实时采样回调, 每采到一个样本调用一次

        返回 `RunResult`。注意: 本方法会**阻塞**, 不要在事件回调里调用。
        """
        mc = self._mc
        mc._check_reentrancy("lab.run()")
        res = RunResult()

        # ---- 前置检查: 能不能动 ----
        if mc.motor_disabled is True:
            res.status = "MOTOR_DISABLED"
            res.error = (
                "设备是安全测试固件(MOUSE_TEST_NO_MOTOR): 电机输出恒 0, 跑这个实验没有意义。"
                "请在 code_stm32f1_mouse/USER/main.h 里改用 MOUSE_TEST_MOTOR_LAB 并重新烧录。"
            )
            return res
        if mc.estop_locked:
            res.status = "ESTOP_LOCKED"
            res.error = "设备处于急停锁存状态。先调用 mc.lab.resume() 解锁再跑。"
            return res

        # ---- 前置检查: 护栏是否已处于"必然立即触发"的状态 ----
        pre_ir = self._ir_min()
        if pre_ir is not None and ir_mm > 0 and pre_ir < ir_mm:
            res.warning = (
                f"开始前就有红外读数 {pre_ir}mm < 阈值 {ir_mm}mm: 动作会立即被护栏停掉。"
                f"请清开传感器前方, 或把 ir_mm 调小(负值=关闭红外护栏)。"
            )

        st0 = mc.link.stats()

        # 用事件订阅而不是 wait_event(): 要一边等一边采实时流, 不能阻塞住。
        hits: List[Any] = []
        estops: List[Any] = []

        def _on_event(msg) -> None:
            if msg.data.get("event") == "STOPPED":
                hits.append(msg)

        off_ev = mc.on(Event.EVENT, _on_event)
        off_estop = mc.on(Event.ESTOP, lambda m: estops.append(m))

        t0 = time.time()
        try:
            ack = mc.send_cmd(
                "move.run", sp_l=int(sp_l), sp_r=int(sp_r), ms=int(ms), ir_mm=int(ir_mm)
            )
            if not ack.ok:
                res.status = ack.status
                res.error = f"设备拒绝了 MOVE RUN: {ack.status}"
                return res

            budget = clamp_timeout(ms / 1000.0 + 5.0, 10.0)
            period = 1.0 / max(0.5, float(sample_hz))
            next_t = t0
            while time.time() - t0 < budget:
                now = time.time()
                if now >= next_t:
                    next_t = now + period
                    f = mc.telemetry.latest()
                    if f:
                        rec = {
                            "t": round(now - t0, 3),
                            "speedL": float(f.get("speedL", 0.0)),
                            "speedR": float(f.get("speedR", 0.0)),
                            "gyroAngle": float(f.get("gyroAngle", 0.0)),
                            "motionTask": int(f.get("motionTask", 0)),
                            "ir_min": self._ir_min(),
                        }
                        res.samples.append(rec)
                        if on_sample is not None:
                            try:
                                on_sample(rec)  # type: ignore[operator]
                            except Exception:
                                pass
                # ⚠️ 结束判据必须放在**采完这一拍之后**。
                # 否则像"红外贴脸"这种在第一个控制周期(约 1ms)就触发的动作,
                # 它的 STOPPED 事件在 `move.run` 的 ACK 等待期间就已经到了 ——
                # 循环一进门就 break, 采样数恒为 0, 调用者拿不到任何帧。
                if hits or estops:
                    break
                time.sleep(0.005)

            st1 = mc.link.stats()
            elapsed = max(1e-6, time.time() - t0)
            n_frames = int(st1.get("unified", 0)) - int(st0.get("unified", 0))
            res.realtime = {
                "seconds": round(elapsed, 3),
                "frames": n_frames,
                "approx_bytes": n_frames * _FRAME_BYTES,
                "approx_bps": round(n_frames * _FRAME_BYTES / elapsed, 1),
                "samples": len(res.samples),
                "bad_crc": int(st1.get("bad_crc", 0)) - int(st0.get("bad_crc", 0)),
                "seq_lost": int(st1.get("seq_lost", 0)) - int(st0.get("seq_lost", 0)),
            }

            if not hits:
                res.status = "ESTOP" if estops else "TIMEOUT"
                res.error = "运动被急停打断" if estops else "等 STOPPED 事件超时"
                return res

            msg = hits[0]
            res.reason = str(msg.data.get("reason", ""))
            res.reason_text = str(msg.data.get("reason_text", ""))
            res.seq = msg.data.get("seq")
            res.raw = msg.data.get("raw") or ""
            res.ok = True
            res.status = res.reason or "OK"
            return res
        finally:
            off_ev()
            off_estop()

    # ------------------------------------------------------------ 采样

    def capture(self, seconds: float = 2.0, hz: float = 50.0) -> List[Dict[str, Any]]:
        """按固定频率采下"最新快照"若干秒(替代旧的 fetch_log), 便于事后画图。"""
        mc = self._mc
        out: List[Dict[str, Any]] = []
        t0 = time.time()
        period = 1.0 / max(0.5, float(hz))
        while time.time() - t0 < seconds:
            f = mc.telemetry.latest()
            if f:
                out.append(dict(f, t=round(time.time() - t0, 3)))
            time.sleep(period)
        return out

    def explain(self, res: RunResult) -> str:
        """把一次实验结果整理成几行人能读的结论。"""
        if not res.ok:
            return f"实验未完成: {res.status}  {res.error}"
        lines = [
            f"停止原因 : {res.reason_text} ({res.reason})",
            "-- 运动期间(统一帧) --",
            f"  时长 {res.realtime.get('seconds')} s, "
            f"{res.realtime.get('frames', 0)} 帧, "
            f"约 {res.realtime.get('approx_bps')} B/s, "
            f"坏CRC {res.realtime.get('bad_crc')}, "
            f"丢帧 {res.realtime.get('seq_lost')}",
            f"  采样点 {len(res.samples)} 个",
        ]
        if res.warning:
            lines.append(f"提示: {res.warning}")
        return "\n".join(lines)
