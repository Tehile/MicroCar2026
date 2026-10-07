"""标定算参: **纯函数**, 不碰设备、不作副作用。

这些函数原本长在课程调参模块(`api/lesson.py`)里。v2.4 把课程实验层整体删除后,
它们被**原样迁移**到这里 —— 因为它们的价值与"课程"无关:

    theory_track_ratio   里程系数的**理论值**(由机械参数算)
    theory_gyro_ratio    陀螺角速率系数的**理论值**(由 ADC 刻度算)
    solve_ratio          线性标定公式: 新系数 = 旧系数 × 读数 / 真值
    error_ratio          相对误差(%), 用于"误差 <= 3%"这条验收线
    StepMetrics / step_metrics           阶跃响应指标 σ% / ts / tp
    StraightMetrics / straightness_metrics   直行质量: 航向漂移 + 左右轮不对称
    IrErrorStats / ir_error_stats       一组"测量 vs 真值"的误差统计
    pick_best_position   在若干标定位置里选综合误差最小的

一条纪律: **只算数**。参数怎么下发(`mc.params.set`)、真值怎么量(卡尺/量角器)
都在调用方; 所以这些函数可以脱离硬件单测, 也可以在离线回放里用。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

# ============================================================ 系数: 理论值与反解

def theory_track_ratio(
    lines_per_rev: int = 256,
    gear_wheel: int = 42,
    gear_encoder: int = 10,
    diameter_mm: float = 22.0,
) -> float:
    """里程系数**理论值**。

        系数 = 编码器线数 x 4 x (轮子齿数 / 编码器齿数) / 轮子周长

    默认参数(线数 256 / 轮齿 42 / 编码齿 10 / 轮径 22 mm)算得约 **62.24**。
    课件示例取 62.95, 是因为所配车轮实际尺寸略有差异 —— 以实物测量值为准。
    """
    circumference = math.pi * float(diameter_mm)
    if circumference <= 0:
        raise ValueError("轮子周长必须为正")
    return float(lines_per_rev) * 4.0 * (float(gear_wheel) / float(gear_encoder)) / circumference


def theory_gyro_ratio(
    sensitivity_mv_per_dps: float = 0.67,
    adc_bits: int = 11,
    vref_mv: float = 3300.0,
) -> float:
    """陀螺角速率系数**理论值**。

        ΔADC = 灵敏度(mV/(度/s)) / ADC最小刻度(mV)
        系数 = ΔADC x 1000   (度/毫秒)

    ADC 最小刻度 = vref / 2^bits。默认(0.67 mV、11 位、3300 mV)算得约 **415.8**。
    """
    scale_mv = float(vref_mv) / float(2 ** int(adc_bits))
    return (float(sensitivity_mv_per_dps) / scale_mv) * 1000.0


def solve_ratio(current: float, reading: float, truth: float) -> float:
    """按"固件读数 vs 真实值"修正系数 —— 里程与角度共用的一条公式。

        新系数 = 旧系数 x (固件读数 / 真实值)

    两个方向看起来相反, 其实是同一条:

    * **里程**: 固件按**它自己算出的**里程停车, 所以"固件读数"就是命令的目标值,
      "真实值"是拿尺子量到的实际距离::

          solve_ratio(62.24, reading=100.0, truth=110.0)   -> 56.58
          # 读得偏小(以为走了100, 实际110) => 系数要调小

    * **角度**: 命令转 1080 度, 固件报 1091 度, 而实际就是 1080 度::

          solve_ratio(415.8, reading=1091.0, truth=1080.0) -> 420.03
          # 读得偏大 => 系数要调大

    统一规则: **读得偏大就把系数调小, 读得偏小就把系数调大**, 一步到位(线性标定)。
    """
    t = float(truth)
    if t == 0:
        raise ValueError("真实值不能为 0")
    return float(current) * float(reading) / t


def error_ratio(reading: float, truth: float) -> float:
    """相对误差(%)。用于"测距/测角误差 <= 3%"这条验收线。"""
    if truth == 0:
        raise ValueError("真实值不能为 0")
    return (float(reading) - float(truth)) / float(truth) * 100.0


# ============================================================ 阶跃响应

@dataclass
class StepMetrics:
    """阶跃响应指标(σ% / ts / tp)。"""

    n: int = 0
    steady: float = 0.0        # 稳态值(末段均值)
    peak: float = 0.0          # 峰值
    sigma_pct: float = 0.0     # 超调量 (%)
    tp_ms: Optional[float] = None   # 峰值时间(相对起点)
    ts_ms: Optional[float] = None   # 调节时间(±5% 误差带)
    rise_ms: Optional[float] = None  # 10% -> 90% 上升时间
    band: float = 0.0          # 误差带绝对宽度
    steady_frac: float = 0.3   # 取末段多少比例当稳态

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n": self.n,
            "steady": round(self.steady, 3),
            "peak": round(self.peak, 3),
            "sigma_pct": round(self.sigma_pct, 3),
            "tp_ms": None if self.tp_ms is None else round(self.tp_ms, 2),
            "ts_ms": None if self.ts_ms is None else round(self.ts_ms, 2),
            "rise_ms": None if self.rise_ms is None else round(self.rise_ms, 2),
            "band": round(self.band, 3),
        }


def step_metrics(
    t_ms: Sequence[float],
    values: Sequence[float],
    steady_frac: float = 0.3,
    band_pct: float = 5.0,
) -> StepMetrics:
    """从一段阶跃响应曲线算 σ% / ts / tp。

    参数:
        t_ms, values  时间轴(ms)与响应(如左轮速度 cm/s), 等长且时间递增
        steady_frac   稳态值取末段的比例(默认后 30%)。**必须取末段**: 若整段平均,
                      加速段会把"稳态"拉低, 超调量随之虚高。
        band_pct      ts 的误差带(默认 ±5%)

    定义:
        σ% = [h(tp) - h(∞)] / h(∞) x 100%
        tp = 首次达到峰值的时间
        ts = 最后一次离开 ±band 之后的时刻(此后不再超出)
    """
    n = len(values)
    if n != len(t_ms):
        raise ValueError("时间轴与响应长度不一致")
    if n < 4:
        raise ValueError("至少需要 4 个采样点才能评估阶跃响应")

    k = max(3, int(n * float(steady_frac)))
    tail = [float(v) for v in values[n - k:]]
    steady = sum(tail) / len(tail)

    peak = max(float(v) for v in values)
    peak_i = max(range(n), key=lambda i: float(values[i]))

    if steady != 0:
        sigma = (peak - steady) / steady * 100.0
    else:
        sigma = 0.0

    band = abs(steady) * float(band_pct) / 100.0
    t0 = float(t_ms[0])

    last_out = -1
    for i in range(n):
        if abs(float(values[i]) - steady) > band:
            last_out = i
    ts = float(t_ms[last_out + 1 if last_out + 1 < n else n - 1]) - t0

    # 10% -> 90% 上升时间(以稳态为基准, 用绝对值以兼容负向阶跃)
    lo, hi = 0.1 * abs(steady), 0.9 * abs(steady)
    t_lo = t_hi = None
    for i in range(n):
        a = abs(float(values[i]))
        if t_lo is None and a >= lo:
            t_lo = float(t_ms[i])
        if t_hi is None and a >= hi:
            t_hi = float(t_ms[i])
            break
    rise = None if (t_lo is None or t_hi is None) else (t_hi - t_lo)

    return StepMetrics(
        n=n,
        steady=steady,
        peak=peak,
        sigma_pct=sigma,
        tp_ms=float(t_ms[peak_i]) - t0,
        ts_ms=ts,
        rise_ms=rise,
        band=band,
        steady_frac=float(steady_frac),
    )


# ============================================================ 直行质量

@dataclass
class StraightMetrics:
    """直行姿态指标("稳快准"的量化)。"""

    n: int = 0
    seconds: float = 0.0
    drift_deg: float = 0.0        # 结束时的航向偏转(度)
    drift_rate_dps: float = 0.0   # 平均漂移速率
    speed_mean_l: float = 0.0
    speed_mean_r: float = 0.0
    speed_bias: float = 0.0       # 左右轮平均速度差(cm/s)
    bias_pct: float = 0.0         # 速度不对称度(%)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n": self.n,
            "seconds": round(self.seconds, 3),
            "drift_deg": round(self.drift_deg, 2),
            "drift_rate_dps": round(self.drift_rate_dps, 3),
            "speed_mean_l": round(self.speed_mean_l, 2),
            "speed_mean_r": round(self.speed_mean_r, 2),
            "speed_bias": round(self.speed_bias, 2),
            "bias_pct": round(self.bias_pct, 2),
        }


def straightness_metrics(
    t_ms: Sequence[float],
    speed_l: Sequence[float],
    speed_r: Sequence[float],
    gangle: Optional[Sequence[float]] = None,
) -> StraightMetrics:
    """直行质量: 航向漂移 + 左右轮速度不对称。

    "两轮速度一致性"是判断能不能走直线的最直接依据 —— 不一致说明两电机特性/
    编码器系数/PID 参数不对称, 车会向低速一侧偏。
    """
    n = len(speed_l)
    if n == 0 or len(speed_r) != n:
        raise ValueError("左右轮速度序列必须等长且非空")
    secs = (float(t_ms[-1]) - float(t_ms[0])) / 1000.0 if n > 1 else 0.0
    ml = sum(float(v) for v in speed_l) / n
    mr = sum(float(v) for v in speed_r) / n
    bias = ml - mr
    denom = (abs(ml) + abs(mr)) / 2.0
    bias_pct = (bias / denom * 100.0) if denom else 0.0

    drift = 0.0
    if gangle is not None and len(gangle) == n and n > 1:
        drift = float(gangle[-1]) - float(gangle[0])

    return StraightMetrics(
        n=n,
        seconds=secs,
        drift_deg=drift,
        drift_rate_dps=(drift / secs) if secs > 0 else 0.0,
        speed_mean_l=ml,
        speed_mean_r=mr,
        speed_bias=bias,
        bias_pct=bias_pct,
    )


# ============================================================ 红外标定误差

@dataclass
class IrErrorStats:
    """一组"测量值 vs 真实值"的误差统计。"""

    n: int = 0
    max_abs: float = 0.0
    mean_abs: float = 0.0
    rms: float = 0.0
    worst_index: int = -1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n": self.n,
            "max_abs": round(self.max_abs, 2),
            "mean_abs": round(self.mean_abs, 2),
            "rms": round(self.rms, 2),
            "worst_index": self.worst_index,
        }


def ir_error_stats(measured: Sequence[float], truth: Sequence[float]) -> IrErrorStats:
    """误差 = |测量值 - 真实值|, 并给出最大/平均/RMS 与最差点。"""
    if len(measured) != len(truth) or not measured:
        raise ValueError("测量与真实序列必须等长且非空")
    errs = [abs(float(m) - float(t)) for m, t in zip(measured, truth)]
    n = len(errs)
    worst = max(range(n), key=lambda i: errs[i])
    return IrErrorStats(
        n=n,
        max_abs=errs[worst],
        mean_abs=sum(errs) / n,
        rms=math.sqrt(sum(e * e for e in errs) / n),
        worst_index=worst,
    )


def pick_best_position(stats_by_pos: Dict[Any, IrErrorStats]) -> Optional[Any]:
    """在若干标定位置里选出综合误差最小的那个。

    "不同红外的最优标定方案可能不同", 因此本函数按位置**分别**比;
    调用方应每个红外通道各调一次。
    """
    if not stats_by_pos:
        return None
    return min(stats_by_pos.items(), key=lambda kv: kv[1].mean_abs)[0]
