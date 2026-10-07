"""课程调参实验: 参数设定 + 实验启停 + 由回传数据自动算参。

对应课件**实验三~实验八**的"调参工作台"。与 `mc.lab`(电机远程控制)的分工:

    mc.lab    自由地让电机转一下, 验证"命令 -> 电机 -> 遥测"这条链是否通
    mc.lesson 按**课件规定的实验剖面**驱动电机, 并把实验数据换算成**可写回设备的参数**

## 实验代码与最终版代码是分开的

固件侧整层都在 `#ifdef MOUSE_LESSON_LAB` 之内(`APP/lesson_lab.c`), 生产固件里一个字节都不占;
课程参量也单独落在 Flash 的 **page 116**, 与生产标定页(page 115)互不覆盖。
上位机侧则集中在 `host/host/api/lesson.py` 与本模块的 `python -m host.lesson` 入口。
要交比赛固件时, 什么都不定义即可。

## 六个实验怎么落到代码上

| 课件 | 物理量 | 固件参数 | 上位机算参 |
|---|---|---|---|
| 实验三 编码器测距 | 里程系数 `trackRatio` | `TRACKL`/`TRACKR` (已有) | `theory_track_ratio` + `solve_ratio` |
| 实验四 匀变速直线 | 速度环 PID | `KPL/KIL/KDL`、`KPR/KIR/KDR` | `step_metrics` -> σ% / ts / tp |
| 实验五 红外标定 | 红外距离表 | 走既有 `CAL IRDATA` 四步 | `ir_error_stats` + `pick_best_position` |
| 实验六 陀螺标定 | 角度系数 `gyroRatio` | `GYRORATIO` (已有) | `theory_gyro_ratio` + `solve_ratio` |
| 实验七 多传感器直行 | 红外 P / 陀螺 PD | `IRPL/IRPR`、`GYROP/GYROD`、`SENSORMODE` | `straightness_metrics` |
| 实验八 转向控制 | 转向搜索数组 | — (数组是 `const`, 未纳入) | — |

本模块的"算参"部分**全是纯函数**(不碰设备), 因此可以脱离硬件单测。

## 一条贯穿始终的原则

上位机**只负责下发参数与算参**, 停止判定与剖面推进全在固件的 1ms 引擎里
(`LESSONLAB_Tick`)。所以上位机被 kill、蓝牙掉线, 实验照样会按时间护栏停下来。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..clock import now_monotonic
from .errors import Result, clamp_timeout
from .events import Event

if TYPE_CHECKING:  # pragma: no cover
    from .client import MouseClient

#: 统一帧的线上字节数(45B 载荷 + 8B 开销) —— v2.0 只有这一种周期帧。
_FRAME_BYTES = 53

#: 实验结束原因 -> 中文
_REASON_TEXT = {
    "DONE": "剖面走完(正常结束)",
    "TIME": "时间护栏到时",
    "DIST": "到达目标里程",
    "ANGLE": "到达目标角度",
    "IR": "前向红外触发(撞墙保护)",
    "ESTOP": "急停",
    "STOP": "上位机主动停止",
}

#: 课程调参量名字(顺序即显示顺序), 与固件 LL_P_* 一致
LESSON_PARAM_NAMES: Tuple[str, ...] = (
    "KPL", "KIL", "KDL", "KPR", "KIR", "KDR",
    "IRPL", "IRPR", "GYROP", "GYROD",
    "SPEEDNEED", "ALLDIS", "SENSORMODE", "WALLMODE",
)


# ============================================================ 算参: 纯函数

def theory_track_ratio(
    lines_per_rev: int = 256,
    gear_wheel: int = 42,
    gear_encoder: int = 10,
    diameter_mm: float = 22.0,
) -> float:
    """实验三 里程系数**理论值**(课件公式)。

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
    """实验六 陀螺角度系数**理论值**(课件公式)。

        ΔADC = 灵敏度(mV/(度/s)) / ADC最小刻度(mV)
        系数 = ΔADC x 1000   (度/毫秒)

    ADC 最小刻度 = vref / 2^bits。默认(0.67 mV、11 位、3300 mV)算得约 **415.8**。
    """
    scale_mv = float(vref_mv) / float(2 ** int(adc_bits))
    return (float(sensitivity_mv_per_dps) / scale_mv) * 1000.0


def solve_ratio(current: float, reading: float, truth: float) -> float:
    """按"固件读数 vs 真实值"修正系数 —— 实验三与实验六共用的一条公式。

        新系数 = 旧系数 x (固件读数 / 真实值)

    两个实验方向看起来相反, 其实是同一条:

    * **实验三(里程)**: 固件按**它自己算出的**里程停车, 所以"固件读数"就是命令的目标值,
      "真实值"是拿尺子量到的实际距离::

          solve_ratio(62.24, reading=100.0, truth=110.0)   -> 56.58
          # 读得偏小(以为走了100, 实际110) => 系数要调小

    * **实验六(角度)**: 命令转 1080 度, 固件报 1091 度, 而实际就是 1080 度::

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


@dataclass
class StepMetrics:
    """阶跃响应指标(实验四的任务 2/3: σ% / ts / tp)。"""

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
        band_pct      ts 的误差带(默认 ±5%, 与课件一致)

    定义与课件一致:
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


@dataclass
class StraightMetrics:
    """直行姿态指标(实验七: "稳快准"的量化)。"""

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
    """实验七的直行质量: 航向漂移 + 左右轮速度不对称。

    课件里"两轮速度一致性"是判断能不能走直线的最直接依据 ——
    不一致说明两电机特性/编码器系数/PID 参数不对称, 车会向低速一侧偏。
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


@dataclass
class IrErrorStats:
    """实验五: 一组"测量值 vs 真实值"的误差统计。"""

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
    """实验五: 误差 = |测量值 - 真实值|, 并给出最大/平均/RMS 与最差点。"""
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
    """实验五: 在若干标定位置里选出综合误差最小的那个。

    课件强调"不同红外的最优标定方案可能不同", 因此本函数按位置**分别**比;
    调用方应每个红外通道各调一次。
    """
    if not stats_by_pos:
        return None
    return min(stats_by_pos.items(), key=lambda kv: kv[1].mean_abs)[0]


# ============================================================ 实验元数据

@dataclass(frozen=True)
class ExpSpec:
    """一个可由 `LESSON START` 启动的实验。"""

    no: int
    name: str
    arg_names: Tuple[str, ...]
    defaults: Tuple[int, ...]
    help: str

    def pack(self, **kw: Any) -> Tuple[int, int, int, int, int]:
        """把关键字参数按 arg_names 摆成 (exp, a, b, c, d)。"""
        vals: List[int] = []
        for nm, dv in zip(self.arg_names, self.defaults):
            vals.append(int(kw.get(nm, dv)))
        while len(vals) < 4:
            vals.append(0)
        return (self.no, vals[0], vals[1], vals[2], vals[3])


#: 可由固件引擎驱动的实验(实验五走 CAL IRDATA; 实验八未纳入)
LESSON_EXPS: Dict[int, ExpSpec] = {
    2: ExpSpec(
        2, "编码器测距", ("v", "dist", "ms"), (60, 100, 3000),
        "直行到固件自认为的 dist 毫米; 用尺子量实际距离后算修正系数",
    ),
    3: ExpSpec(
        3, "匀变速直线控制", ("v", "t_acc", "t_cruise", "t_dec"), (50, 200, 800, 200),
        "加速 -> 匀速 -> 减速 -> 停车; 回传 1kHz 曲线算 σ%/ts/tp",
    ),
    5: ExpSpec(
        5, "陀螺仪标定", ("angle", "v", "ms"), (1080, 60, 10000),
        "原地转到 angle 度并回读实测角度(默认 1080 度 = 3 圈)",
    ),
    6: ExpSpec(
        6, "多传感器校准直行", ("v", "dist", "ms"), (50, 1000, 8000),
        "按 SENSORMODE 开启红外/陀螺纠偏直行到 dist 毫米",
    ),
}


# ============================================================ 实验结果

@dataclass
class LessonResult:
    """一次课程实验的完整结果(固件明细行 + 实验期间捕获的统一帧序列 + 自动算出的指标)。"""

    ok: bool = False
    status: str = ""
    exp: int = 0
    exp_name: str = ""
    reason: str = ""
    reason_text: str = ""
    elapsed_ms: Optional[int] = None
    dL: Optional[int] = None
    dR: Optional[int] = None
    peak_l: Optional[int] = None
    peak_r: Optional[int] = None
    angle: Optional[int] = None            # 结束时航向角(度)
    # ---- 由日志算出的指标 ----
    metrics: Dict[str, Any] = field(default_factory=dict)
    steps: List[StepMetrics] = field(default_factory=list)
    straight: Optional[StraightMetrics] = None
    # ---- 日志 ----
    log_points: int = 0
    log_fetched: bool = False
    log_seconds: float = 0.0
    log_arrays: Any = None
    # ---- 其它 ----
    realtime: Dict[str, Any] = field(default_factory=dict)
    warning: str = ""
    error: str = ""
    raw: str = ""

    def to_dict(self, with_series: bool = False) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "ok": self.ok,
            "status": self.status,
            "exp": self.exp,
            "exp_name": self.exp_name,
            "reason": self.reason,
            "reason_text": self.reason_text,
            "elapsed_ms": self.elapsed_ms,
            "dL": self.dL,
            "dR": self.dR,
            "peak_l": self.peak_l,
            "peak_r": self.peak_r,
            "angle": self.angle,
            "metrics": self.metrics,
            "steps": [s.to_dict() for s in self.steps],
            "straight": None if self.straight is None else self.straight.to_dict(),
            "log_points": self.log_points,
            "log_fetched": self.log_fetched,
            "log_seconds": round(self.log_seconds, 3),
            "realtime": self.realtime,
        }
        if self.warning:
            d["warning"] = self.warning
        if self.error:
            d["error"] = self.error
        if self.raw:
            d["raw"] = self.raw
        if with_series and self.log_arrays is not None:
            t, task, gy, sl, sr = self.log_arrays
            d["series"] = {
                "t_ms": [round(float(x), 2) for x in t],
                "task": [int(x) for x in task],
                "gangle": [float(x) for x in gy],
                "speed_l": [float(x) for x in sl],
                "speed_r": [float(x) for x in sr],
            }
        return d


# ============================================================ 门面

class LessonAPI:
    """课程调参实验(门面 `mc.lesson`)。"""

    def __init__(self, mc: "MouseClient") -> None:
        self._mc = mc

    # ------------------------------------------------------------ 参数

    def params(self, refresh: bool = True) -> Dict[str, float]:
        """当前课程调参量的值(只返回课程命名空间里的那 14 项)。"""
        snap = self._mc.params.get_all(refresh=refresh) if refresh else self._mc.params.snapshot()
        return {n: snap[n] for n in LESSON_PARAM_NAMES if n in snap}

    def param_meta(self) -> Dict[str, Any]:
        from ..protocol.commands import PARAM_META

        return {n: PARAM_META.get(n, ("", 3, "")) for n in LESSON_PARAM_NAMES}

    def set_params(
        self,
        mapping: Dict[str, float],
        readback: bool = True,
        save: bool = False,
        timeout: float = 3.0,
    ) -> List[Result]:
        """批量下发课程参数(默认逐项回读校验; save=True 时再落 Flash)。

        `speedRatio` 那套派生关系**不适用**于课程参数 —— 这里改的是 PID/纠偏增益,
        与里程系数没有换算关系, 所以不走 `ParamsAPI.set_many(derive_speed=...)`。
        """
        bad = [n for n in mapping if n not in LESSON_PARAM_NAMES]
        if bad:
            return [Result(False, "ERR_NAME", {"names": bad})]
        out = [self._mc.params.set(n, v, readback=readback, timeout=timeout)
               for n, v in mapping.items()]
        if save and all(r.ok for r in out):
            out.append(self._mc.params.save())
        return out

    def read_param(self, name: str, timeout: float = 3.0) -> Optional[float]:
        """读单个课程参数(强刷)。"""
        if name not in LESSON_PARAM_NAMES:
            return None
        return self._mc.params.get(name, timeout=timeout)

    # ------------------------------------------------------------ 状态与启停

    def status(self) -> Dict[str, Any]:
        """课程实验视角的状态。"""
        mc = self._mc
        f = mc.telemetry.latest()
        return {
            "connected": mc.is_connected,
            "motor_disabled": mc.motor_disabled,
            "estop_locked": mc.estop_locked,
            "freemode": bool(f.get("freemode", False)),
            "events_incomplete": mc.events_incomplete,
            "streaming": mc.telemetry.is_streaming(),
            "can_run": (mc.is_connected and mc.motor_disabled is False and not mc.estop_locked),
        }

    def start(self, exp: int, a: int = 0, b: int = 0, c: int = 0, d: int = 0,
              timeout: float = 3.0) -> Result:
        """只启动实验, 不等结果(要阻塞跑完整流程请用 `run()`)。"""
        if exp not in LESSON_EXPS:
            return Result(False, "ERR_EXP", {"exp": exp, "valid": sorted(LESSON_EXPS)})
        return self._mc.send_cmd("lesson.start", exp=int(exp), a=int(a), b=int(b),
                                 c=int(c), d=int(d), timeout=timeout)

    def stop(self, timeout: float = 3.0) -> Result:
        """中止当前实验(固件会停车并回一行 `LESSON DONE ... STOP`)。"""
        return self._mc.send_cmd("lesson.stop", timeout=timeout)

    # ------------------------------------------------------------ 跑完整流程

    def run(
        self,
        exp: int,
        capture: bool = True,
        sample_hz: float = 5.0,
        budget_extra: float = 8.0,
        on_progress: Optional[Callable[[float, Optional[int], Optional[int]], None]] = None,
        on_sample: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_timeout_plan: Optional[Callable[[int], None]] = None,
        **kwargs: Any,
    ) -> LessonResult:
        """跑一次课程实验: 启动 -> 等 DONE -> 由**本地捕获的统一帧序列**自动算参。

        参数(除 exp 外均可由 `**kwargs` 按 `LESSON_EXPS[exp].arg_names` 指定):
            exp       实验号(2/3/5/6)
            capture   是否在实验期间把统一帧整段收下来并据此算 σ%/ts/tp 等指标
            sample_hz 供 `on_sample` 回调的预览采样频率(与捕获无关: 捕获是全速的)
            kwargs    实验参数, 如 `run(3, v=50, t_acc=200, t_cruise=800, t_dec=200)`

        ## v2.0 为什么不再有"实验结束后回传 1kHz 日志"

        v1.5.5 用 `SCOPE` 在固件侧 1kHz 本地录制, 停下后 `DUMP` 整块回传。这条
        双流机制的**唯一理由是"物理速率不足"**, 而该前提已被真机证伪, 且固件已改
        **自时钟发送**: 统一帧按链路能力全速上行(≈217 Hz, 每帧都是 45B 全量快照)。
        于是"实验期间的数据"本来就在主机内存里 —— 直接边收边攒即可,
        既省掉一次"停车-回传"的往返, 也不会因为回传失败而丢掉整段曲线。

        **本方法会阻塞**, 不要在事件回调里调用。
        """
        mc = self._mc
        mc._check_reentrancy("lesson.run()")

        res = LessonResult(exp=int(exp))
        spec = LESSON_EXPS.get(int(exp))
        if spec is None:
            res.status = "ERR_EXP"
            res.error = f"未知实验号 {exp}; 可用: {sorted(LESSON_EXPS)} (实验五走 mc.calib 的 IRDATA)"
            return res
        res.exp_name = spec.name

        # 严格校验 `**kwargs` —— ExpSpec.pack() 会**静默忽略**不认识的键,
        # 于是改名后残留的旧调用点(如 v1.5.5 的 realtime_hz/log_timeout)
        # 会一声不响地跑出"参数全默认"的结果。宁可在这里直接报错。
        unknown = sorted(set(kwargs) - set(spec.arg_names))
        if unknown:
            res.status = "ERR_ARG"
            res.error = (f"实验 {exp} 不认识参数 {unknown}; "
                         f"可用: {list(spec.arg_names)}")
            return res

        # ---- 前置检查 ----
        if mc.motor_disabled is True:
            res.status = "MOTOR_DISABLED"
            res.error = (
                "设备跑的是安全测试固件(MOUSE_TEST_NO_MOTOR), 电机恒不输出。"
                "请改用 MOUSE_TEST_LESSON_LAB 重新烧录后再做调参实验。"
            )
            return res
        if mc.estop_locked:
            res.status = "ESTOP_LOCKED"
            res.error = "设备处于急停锁存状态。先 mc.lab.resume() 解锁。"
            return res

        plan = spec.pack(**kwargs)
        # 预估时间上限: 用实验自身的时间护栏(参数 c/ms), 取不到就用 10s
        ms_hint = int(kwargs.get("ms", 10000))
        if int(exp) == 3:
            ms_hint = int(kwargs.get("t_acc", spec.defaults[1])) + \
                      int(kwargs.get("t_cruise", spec.defaults[2])) + \
                      int(kwargs.get("t_dec", spec.defaults[3]))
        budget = clamp_timeout(ms_hint / 1000.0 + float(budget_extra), 20.0)
        if on_timeout_plan is not None:
            try:
                on_timeout_plan(int(budget * 1000))
            except Exception:
                pass

        # ---- 等 DONE 的订阅 ----
        dones: List[Any] = []
        estops: List[Any] = []

        def _on_lesson(msg) -> None:
            if str(msg.data.get("kind", "")).upper() == "DONE":
                dones.append(msg)

        # ---- 实验曲线的本地捕获(取代 v1.5.5 的 SCOPE/DUMP 双流) ----
        # 回调运行在**调度线程**, 所以这里只做 append —— 不能碰设备。
        series: List[tuple] = []
        t0_mono = now_monotonic()

        def _on_telem(msg) -> None:
            if msg.data.get("stream") != "unified":
                return
            rec = msg.data.get("rec") or {}
            t_ms = (now_monotonic() - t0_mono) * 1000.0
            series.append((
                t_ms,
                int(rec.get("motionTask", 0) or 0),
                float(rec.get("gyroAngle", 0.0) or 0.0),
                float(rec.get("speedL", 0.0) or 0.0),
                float(rec.get("speedR", 0.0) or 0.0),
            ))

        off_lesson = mc.on(Event.LESSON, _on_lesson)
        off_estop = mc.on(Event.ESTOP, lambda m: estops.append(m))
        off_telem = mc.on(Event.TELEMETRY, _on_telem) if capture else (lambda: None)

        t0 = time.time()
        st0 = mc.link.stats()
        try:
            ack = mc.send_cmd("lesson.start", exp=plan[0], a=plan[1], b=plan[2],
                              c=plan[3], d=plan[4])
            if not ack.ok:
                res.status = ack.status
                res.error = f"设备拒绝启动实验: {ack.status}"
                return res

            # 实时预览采样: 与 lab.run 同样的做法 —— 等事件的同时把预览值采下来
            period = 1.0 / max(0.5, float(sample_hz))
            next_t = t0
            while time.time() - t0 < budget:
                if dones or estops:
                    break
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
                        }
                        if on_sample is not None:
                            try:
                                on_sample(rec)
                            except Exception:
                                pass
                time.sleep(0.005)

            st1 = mc.link.stats()
            elapsed = max(1e-6, time.time() - t0)
            n_frames = int(st1.get("unified", 0)) - int(st0.get("unified", 0))
            res.realtime = {
                "seconds": round(elapsed, 3),
                "frames": n_frames,
                "approx_bytes": n_frames * _FRAME_BYTES,
                "approx_bps": round(n_frames * _FRAME_BYTES / elapsed, 1),
                "captured": len(series),
                "bad_crc": int(st1.get("bad_crc", 0)) - int(st0.get("bad_crc", 0)),
                "seq_lost": int(st1.get("seq_lost", 0)) - int(st0.get("seq_lost", 0)),
            }

            if not dones:
                res.status = "ESTOP" if estops else "TIMEOUT"
                res.error = "实验被急停打断" if estops else "等 LESSON DONE 超时(设备没回明细行)"
                return res

            msg = dones[0]
            rep = _parse_lesson_done(msg.data.get("fields") or [])
            res.reason = rep["reason"]
            res.reason_text = _REASON_TEXT.get(rep["reason"], rep["reason"])
            res.elapsed_ms = rep["elapsed_ms"]
            res.dL = rep["dL"]
            res.dR = rep["dR"]
            res.peak_l = rep["peak_l"]
            res.peak_r = rep["peak_r"]
            res.angle = rep["angle"]
            res.raw = msg.data.get("raw") or ""
            res.ok = True
            res.status = rep["reason"] or "OK"

            if capture and series:
                # 列成 (t, task, gyro, speedL, speedR) —— 沿用 v1.5.5 的"五列
                # 数组"形状, 下游 _compute / 绘图一行不用改。v2.0 起数据源改为
                # 本地订阅统一帧(Event.TELEMETRY), 不再走已删除的 SCOPE 回传。
                arr = (
                    [r[0] for r in series],
                    [r[1] for r in series],
                    [r[2] for r in series],
                    [r[3] for r in series],
                    [r[4] for r in series],
                )
                res.log_arrays = arr
                res.log_points = len(arr[0])
                res.log_fetched = True
                res.log_seconds = round(elapsed, 3)
                if on_progress is not None:
                    try:
                        on_progress(1.0, res.log_points, res.log_points)
                    except Exception:
                        pass
                self._compute(res, arr)
            elif capture:
                res.warning = (res.warning + "  " if res.warning else "") + \
                    "实验期间未收到统一帧(是否已开始拉取? mc.stream.on()), 无法自动算参"
            return res
        finally:
            off_lesson()
            off_estop()
            off_telem()

    # ------------------------------------------------------------ 自动算参

    def _compute(self, res: LessonResult, arr: Any) -> None:
        """按实验类型, 用捕获到的统一帧序列自动算出该实验关心的指标。"""
        t, _task, gy, sl, sr = arr
        if len(t) < 4:
            res.warning = (res.warning + "  " if res.warning else "") + "日志点太少, 跳过自动算参"
            return

        try:
            if res.exp == 3:
                # 实验四: 分别对左右轮算阶跃指标
                res.steps = [
                    step_metrics(t, sl),
                    step_metrics(t, sr),
                ]
                res.metrics["step"] = {
                    "left": res.steps[0].to_dict(),
                    "right": res.steps[1].to_dict(),
                }

            if res.exp == 2:
                # 实验三: 把脉冲换算成"固件认为走了多远", 供与尺子实测值比对
                ratio_l = self._safe_param("TRACKL")
                ratio_r = self._safe_param("TRACKR")
                believed_l = (res.dL / ratio_l) if (ratio_l and res.dL is not None) else None
                believed_r = (res.dR / ratio_r) if (ratio_r and res.dR is not None) else None
                res.metrics["encoder"] = {
                    "dL_pulse": res.dL,
                    "dR_pulse": res.dR,
                    "ratio_l": ratio_l,
                    "ratio_r": ratio_r,
                    "believed_mm_l": None if believed_l is None else round(believed_l, 2),
                    "believed_mm_r": None if believed_r is None else round(believed_r, 2),
                    "hint": "用尺子量实际距离 D_true, 再 solve_ratio(ratio, believed_mm, D_true)",
                }

            if res.exp == 6:
                # 实验七: 直行质量
                res.straight = straightness_metrics(t, sl, sr, gy)
                res.metrics["straight"] = res.straight.to_dict()

            if res.exp == 5:
                # 实验六: 只报"固件认为转了多少度", 由调用方与真实角度比对
                res.metrics["gyro"] = {
                    "reported_deg": res.angle,
                    "hint": "solve_ratio(GYRORATIO, reading=reported_deg, truth=真实转过角度)",
                }
        except Exception as e:  # 算参失败不该让整次实验算失败
            res.warning = (res.warning + "  " if res.warning else "") + \
                f"自动算参失败: {type(e).__name__}: {e}"

    def _safe_param(self, name: str) -> Optional[float]:
        try:
            return self._mc.params.get(name, timeout=2.0)
        except Exception:
            return None

    # ------------------------------------------------------------ 解释

    def explain(self, res: LessonResult) -> str:
        """把一次课程实验整理成人能读的结论。"""
        if not res.ok:
            return f"实验未完成: {res.status}  {res.error}"
        lines = [
            f"实验      : {res.exp} {res.exp_name}",
            f"结束原因  : {res.reason_text} ({res.reason})",
            f"运行时长  : {res.elapsed_ms} ms",
            f"轮子位移  : L {res.dL} / R {res.dR} 脉冲",
            f"速度峰值  : L {res.peak_l} / R {res.peak_r} cm/s",
            f"结束航向  : {res.angle} 度",
        ]
        for tag, m in (("左轮", res.steps[0] if len(res.steps) > 0 else None),
                       ("右轮", res.steps[1] if len(res.steps) > 1 else None)):
            if m is not None:
                lines.append(
                    f"{tag}阶跃  : 稳态 {m.steady:.1f} 峰值 {m.peak:.1f} "
                    f"σ% {m.sigma_pct:.2f}%  ts {m.ts_ms} ms  tp {m.tp_ms} ms"
                )
        if res.straight is not None:
            s = res.straight
            lines.append(
                f"直行质量  : 漂移 {s.drift_deg:+.2f} 度 ({s.drift_rate_dps:+.3f} 度/s), "
                f"左右轮 {s.speed_mean_l:.1f}/{s.speed_mean_r:.1f} cm/s 不对称 {s.bias_pct:+.2f}%"
            )
        enc = res.metrics.get("encoder")
        if enc:
            lines.append(
                f"里程(固件): L {enc['believed_mm_l']} / R {enc['believed_mm_r']} mm   "
                f"系数 L {enc['ratio_l']} / R {enc['ratio_r']}"
            )
        lines.append(f"1kHz 日志 : {res.log_points} 点" + ("" if res.log_fetched else " (未回传)"))
        if res.warning:
            lines.append(f"提示: {res.warning}")
        return "\n".join(lines)


def _parse_lesson_done(fields: list) -> Dict[str, Any]:
    """解析 `LESSON DONE <exp> <REASON> <ms> <dL> <dR> <vpkL> <vpkR> <angle>`。

    容错: 字段缺失一律填 None, 不抛异常 —— 明细行用的是文本, 少一个字段不该
    让整次实验的数据都拿不到。
    """
    out: Dict[str, Any] = {
        "exp": None, "reason": "", "elapsed_ms": None, "dL": None, "dR": None,
        "peak_l": None, "peak_r": None, "angle": None,
    }
    keys = ["exp", "reason", "elapsed_ms", "dL", "dR", "peak_l", "peak_r", "angle"]
    for i, key in enumerate(keys):
        if i >= len(fields):
            break
        raw = fields[i]
        if key == "reason":
            out[key] = str(raw)
            continue
        try:
            out[key] = int(raw)
        except (TypeError, ValueError):
            out[key] = None
    return out
