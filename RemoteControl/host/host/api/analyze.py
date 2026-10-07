"""离线分析: 阶跃指标、跟踪误差、按运动任务分段。

对应功能清单的 J 组。**不需要连接设备** —— 输入是 numpy 数组或
`mc.telemetry.history_dict()` 的输出, 所以随时可用(含离线回放)。

v2.0(2026-10-05): 输入不再是 1kHz dump 数组(该机制已删), 而是**统一帧**的历史
(`mc.telemetry.history()`, 约 217 Hz 全量快照)。做运动特性最该用的时间轴是
`mc.timesync.frame_time(rec)` 换算出的设备时刻 —— 它不受蓝牙抖动影响。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence

import numpy as np

from ..analysis.metrics import (
    format_metrics,
    segment_by_task,
    step_metrics,
    tracking_error,
)
from ..protocol.codec import MOTION_TASK_NAMES

if TYPE_CHECKING:  # pragma: no cover
    from .client import MouseClient

#: 统一帧里与运动控制分析最相关的通道
MOTION_CHANNELS = ("motionTask", "gyroAngle", "speedL", "speedR", "pidErr")


class AnalyzeAPI:
    """纯计算, 无副作用, 可反复调用。"""

    def __init__(self, mc: Optional["MouseClient"] = None) -> None:
        self._mc = mc

    # ------------------------------------------------------------ 基本指标

    def step(
        self, t: Sequence[float], y: Sequence[float], target: float, band: float = 0.02
    ) -> Dict[str, float]:
        """阶跃响应指标: rise_s / overshoot_pct / settle_s / sse / peak / final。"""
        return step_metrics(np.asarray(t, dtype=float), np.asarray(y, dtype=float), target, band)

    def tracking(
        self, t: Sequence[float], y: Sequence[float], ref: Sequence[float], tail_frac: float = 0.2
    ) -> Dict[str, float]:
        """跟踪误差: rms / maxabs / tail_mean / tail_rms。"""
        return tracking_error(np.asarray(t), np.asarray(y), np.asarray(ref), tail_frac)

    def segments(self, t_ms: Sequence[float], task: Sequence[int]) -> List[Dict[str, Any]]:
        """按 `motionTask` 变化切段。一次动作就是一段。"""
        return segment_by_task(np.asarray(t_ms), np.asarray(task))

    def format(self, m: Dict[str, float]) -> str:
        return format_metrics(m)

    def task_name(self, code: int) -> str:
        return MOTION_TASK_NAMES.get(int(code), str(int(code)))

    # ------------------------------------------------------------ 统一帧历史

    def history_channels(self, seconds: Optional[float] = None) -> Dict[str, np.ndarray]:
        """取统一帧历史(具名通道)。需要连接设备或回放。"""
        if self._mc is None:
            return {}
        return self._mc.telemetry.history_dict(seconds=seconds)

    def channels(self, h: Dict[str, np.ndarray]) -> Dict[str, Any]:
        """把 `history_dict()` 的输出整理成分析用的具名通道。"""
        if not h:
            return {}
        t = np.asarray(h.get("t", []), dtype=float)
        return {
            "t_epoch": t,
            "t_s": (t - t[0]) if t.size else t,
            "motionTask": np.asarray(h.get("motionTask", []), dtype=float),
            "gyroAngle": np.asarray(h.get("gyroAngle", []), dtype=float),
            "speedL": np.asarray(h.get("speedL", []), dtype=float),
            "speedR": np.asarray(h.get("speedR", []), dtype=float),
            "pidErr": np.asarray(h.get("pidErr", []), dtype=float),
        }

    def history_segments(self, h: Dict[str, np.ndarray]) -> List[Dict[str, Any]]:
        """把历史按运动任务切段(段内带各通道切片)。"""
        ch = self.channels(h)
        if not ch:
            return []
        segs = self.segments(ch["t_s"], ch["motionTask"])
        for s in segs:
            i0, i1 = int(s["i0"]), int(s["i1"])
            s["task_name"] = self.task_name(s["task"])
            s["slices"] = {
                "t_s": ch["t_s"][i0 : i1 + 1],
                "speedL": ch["speedL"][i0 : i1 + 1],
                "speedR": ch["speedR"][i0 : i1 + 1],
                "gyroAngle": ch["gyroAngle"][i0 : i1 + 1],
            }
        return segs

    def summary(self, h: Dict[str, np.ndarray], band: float = 0.02) -> List[Dict[str, Any]]:
        """对每一段算左右轮速的阶跃指标。

        target 取该段最后 5% 的均值 —— 比取"理论目标值"更稳(帧里没有下发的目标转速,
        而段尾通常已进入稳态)。
        """
        out: List[Dict[str, Any]] = []
        for s in self.history_segments(h):
            sl = np.asarray(s["slices"]["speedL"], dtype=float)
            sr = np.asarray(s["slices"]["speedR"], dtype=float)
            t_s = np.asarray(s["slices"]["t_s"], dtype=float)
            if len(t_s) < 3:
                continue
            tail = max(1, len(sl) // 20)
            item: Dict[str, Any] = {
                "task": s["task"],
                "task_name": s["task_name"],
                "duration_ms": s["duration_ms"],
                "points": len(t_s),
            }
            for name, y in (("speedL", sl), ("speedR", sr)):
                target = float(np.mean(y[-tail:]))
                item[name] = self.step(t_s, y, target, band) if abs(target) > 1e-6 else {}
            out.append(item)
        return out

    def explain(self, h: Dict[str, np.ndarray]) -> str:
        """把 summary 变成一段人可读的文本(CLI 与日志用)。"""
        rows = self.summary(h)
        if not rows:
            return "（无有效动作段）"
        lines = []
        for r in rows:
            lines.append(
                f"#{r['task']:<3}{r['task_name']:<22}{r['duration_ms']:>7.0f}ms  "
                f"L[{self.format(r.get('speedL', {}))}]  R[{self.format(r.get('speedR', {}))}]"
            )
        return "\n".join(lines)

    # ============================================================ 红外测距分析

    def ir_saturation_stats(self, series: Any = None) -> Dict[str, Any]:
        """统计各通道的**饱和占比**与量程使用情况。

        `series` 可以是:
          - 一个 4 列数组, 或 `{通道名: 序列}`(通常来自统一帧历史的 irF1/irF2/irL/irR)。

        **为什么必须看饱和**: 查表测距在两端被夹紧 —— 贴太近返回 0mm、超出量程
        返回 249mm。单看距离会把"近得查不出"误当成"贴住了", 据此调参必然跑偏。
        """
        out: Dict[str, Any] = {"channels": {}, "any_saturated": False, "worst": None}
        chans = ("F1", "F2", "L", "R")
        if series is None:
            return out
        if isinstance(series, dict):
            # 允许传 history_dict 的键名 irF1/irF2/irL/irR
            cols = {}
            for k in chans:
                key = "ir" + k
                if key in series:
                    cols[k] = np.asarray(series[key], dtype=float)
                elif k in series:
                    cols[k] = np.asarray(series[k], dtype=float)
        else:
            a = np.asarray(series, dtype=float)
            if a.ndim != 2 or a.shape[1] < 4:
                return out
            cols = {k: a[:, i] for i, k in enumerate(chans)}
        for k, y in cols.items():
            if y.size == 0:
                continue
            near = float(np.mean(y <= 0.0))
            far = float(np.mean(y >= 249.0))
            frac = near + far
            out["channels"][k] = {
                "n": int(y.size),
                "min": float(np.min(y)),
                "max": float(np.max(y)),
                "mean": float(np.mean(y)),
                "frac_near_sat": near,
                "frac_far_sat": far,
                "frac_sat": frac,
            }
            if frac > 0:
                out["any_saturated"] = True
            if out["worst"] is None or frac > out["channels"][out["worst"]]["frac_sat"]:
                out["worst"] = k
        return out

    def ir_range_curve(
        self,
        adc: Any,
        mm: Any,
        bins: int = 24,
    ) -> Dict[str, Any]:
        """从"原始 ADC ↔ 距离"配对序列里拟合标定曲线(幂律 `ADC = a * d^b`)。

        数据来自统一帧: `irAdcF1..irAdcR` 与 `irF1..irR` 本身就是逐帧同步的,
        把鼠标推着走一遍就能得到整条测距曲线, 用来**校验或重建** `irs.c` 里的
        250 点标定表, 而不是靠手工摆两次(近点/远点)。

        返回 `{ok, a, b, r2, n, note}`; `r2` 是拟合优度, 低于 0.9 就别用。
        """
        x = np.asarray(mm, dtype=float)
        y = np.asarray(adc, dtype=float)
        n = int(min(x.size, y.size))
        out: Dict[str, Any] = {"ok": False, "a": None, "b": None, "r2": None, "n": n,
                               "note": ""}
        if n < 8:
            out["note"] = "样本太少(<8), 拟合无意义"
            return out
        x, y = x[:n], y[:n]
        keep = (x > 1.0) & (y > 1.0) & (x < 400.0)  # 剔掉 0/饱和/离谱点
        x, y = x[keep], y[keep]
        if x.size < 8:
            out["note"] = "有效样本太少(去掉饱和点后 <8)"
            return out
        lx, ly = np.log(x), np.log(y)
        b, loga = np.polyfit(lx, ly, 1)
        pred = loga + b * lx
        ss_res = float(np.sum((ly - pred) ** 2))
        ss_tot = float(np.sum((ly - np.mean(ly)) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        out.update({"ok": bool(r2 > 0.9), "a": float(np.exp(loga)), "b": float(b),
                    "r2": float(r2), "n": int(x.size)})
        if not out["ok"]:
            out["note"] = f"拟合优度偏低(r2={r2:.3f}), 检查采样是否覆盖足够量程"
        return out

    # ============================================================ 电源分析

    def batt_low_events(self, mv: Any, low_mv: float = 7000.0) -> List[Dict[str, Any]]:
        """从 mV 序列里找出**低电区间**(用于"跑到一半掉电"的复盘)。

        入参是统一帧历史的 `battMv` 序列。返回 `[{start_i, end_i, n, min_mv, mean_mv}]`。
        """
        y = np.asarray(mv, dtype=float)
        if y.size == 0:
            return []
        below = y < float(low_mv)
        out: List[Dict[str, Any]] = []
        i = 0
        n = y.size
        while i < n:
            if not below[i]:
                i += 1
                continue
            j = i
            while j < n and below[j]:
                j += 1
            seg = y[i:j]
            out.append({"start_i": int(i), "end_i": int(j - 1), "n": int(j - i),
                        "min_mv": float(np.min(seg)), "mean_mv": float(np.mean(seg))})
            i = j
        return out

    def batt_discharge(self, mv: Any, t_s: Any = None) -> Dict[str, Any]:
        """电池跌落分析: 起止电压、总跌落、最大瞬时跌落速率(内阻的间接指标)。

        **最大瞬时跌落速率**是判断"电池够不够劲"的关键数: 电机启动瞬间电流陡增,
        若内阻偏大, mV 会在一两个采样点内掉一大截 —— 那种掉法会把 MCU 拉到复位。
        v2.0 统一帧约 217 Hz(4.6ms/点), 比旧 0.5Hz 遥测帧强得多。
        """
        y = np.asarray(mv, dtype=float)
        out: Dict[str, Any] = {"n": int(y.size), "v_start_mv": None, "v_end_mv": None,
                               "v_min_mv": None, "drop_total_mv": None,
                               "max_slope_mv_per_sample": None, "max_slope_at": None}
        if y.size == 0:
            return out
        out["v_start_mv"] = float(y[0])
        out["v_end_mv"] = float(y[-1])
        out["v_min_mv"] = float(np.min(y))
        out["drop_total_mv"] = float(y[0] - y[-1])
        if y.size >= 2:
            d = np.diff(y)
            k = int(np.argmin(d))
            out["max_slope_mv_per_sample"] = float(d[k])
            if t_s is not None:
                tt = np.asarray(t_s, dtype=float)
                out["max_slope_at"] = float(tt[k]) if k < tt.size else None
            else:
                out["max_slope_at"] = k
        return out
