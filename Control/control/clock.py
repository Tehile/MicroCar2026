"""统一主时钟。

## 三条时间基，各归各的负责方

整合层要同时面对三个时钟，**它们不是同一个问题**，混在一起必然出错：

| 时间基 | 谁负责 | 怎么算 |
|---|---|---|
| ① **固件毫秒时基**（`linkTimeMs`，发车以来 ms） | **上游 `mc.timesync`** | min-delay 估偏移 + 最小二乘估频差(ppm) + `resolve_ms8` 还原快帧低 8 位 |
| ② **PC 墙钟 epoch 毫秒**（`t_pc_ms`，全系统唯一时间基） | **本模块** | 单调时钟推进 + 周期性跟墙钟重锚 |
| ③ **手机 `wall_clock_ns`**（epoch 纳秒） | 本模块 `Clock` 的 offset | L2 最小延迟（**有偏上界**，带不确定度） |

### 为什么 ① 交给上游（这条是**改过的**，以前不是这样）

上游 1.4.0 起了自己的时间同步：固件有 `linkTimeMs`（SysTick 1ms，u32 49 天不绕），
`ARM` 命令让它在发车时点灯并回 `ARMED <ms>`，之后每秒上行一帧 `TIME`（带完整 u32），
**每个快帧的保留字节也让出时间基的低 8 位** —— 于是每个采样点都能被钉到准确时刻。

固件侧还给了 1Hz 基准方波灯，供摄像头/光电/人眼做**独立校核**。

⇒ 这一整段是上游做对的事，而且它比我们能拿到的观测更直接（我们有不到帧的原始记录）。
本层**不再自己算一遍**，只做三件事：透传、落盘、把结果换算到 ② 上。

### ② 为什么要"单调推进 + 重锚"，而不是直接 `time.time()`

`time.time()` 会被 NTP 回拨/前跳。一次回拨会让时间轴上出现**负的时间间隔**，
而"检测运动特性"要看的恰恰是毫秒级的时间关系 —— 倒挂一次，那份数据就废了。
所以：

    t_pc_ms = anchor_epoch_ms + (now_monotonic() - anchor_mono) * 1000

锚点每 `ANCHOR_REFRESH_S` 秒跟墙钟重对一次，但**只接受小修正**：
偏差超过 `ANCHOR_MAX_STEP_MS` 时，说明墙钟被调过（而不是单调钟漂了），
这时**保留旧锚点**并把差值报在 `wall_gap_ms` 里 —— 宁可时间轴与真实世界差一点，
也不要在一次会话中间跳一下。同时保证**发出的值绝不回退**。

### ③ 手机侧的 offset 语义（不变）

    device_ms = t_pc_ms + offset_ms   ⇒   t_pc_ms = device_ms - offset_ms

`source` 如实记录来源，`uncertainty_ms` 如实记录不确定度
（**不许假装它是精确的** —— 上游把跨机对齐列为显式未决项，我们把它变成"可测量且带误差棒"）：

    assumed        未标定。offset=0，uncertainty 给一个大值（默认 250 ms）。
    manual         人工输入实测值（例如已知两机 NTP 同源，或用户自己量过）。
    sync_pulse     L1+：一次**同时被两路观测到**的动作，两侧各取一个时刻。
    l2_minlatency  L2：用交付帧的到达时刻与帧内 `wall_clock_ns` 做最小延迟拟合。
                   原理：`到达 - 拍摄 = offset + 单向传输延迟`，单向延迟 ≥ 0，
                   所以**最小的那个到达-拍摄差**给出 offset 的上界，且随着首包
                   排队越少越紧。取分位数给出不确定度。
                   ⚠️ 这是**有偏上界**（延迟恒正），不是无偏估计 —— 结果里必须
                   把 method 写成有偏并带上 uncertainty。
"""

from __future__ import annotations

import statistics
import threading
import time
from datetime import datetime
from typing import Iterable, List, Optional, Tuple


# ============================================================ 单调 → 墙钟

class WallAnchor:
    """把"本机单调秒"换算成"墙钟 epoch 毫秒"，且**保证不回退**。

    设计取舍见模块 docstring 的 ② 节：宁可与真实世界差一点，也不要在会话中间跳。
    """

    #: 多久跟墙钟重对一次（秒）
    REFRESH_S = 5.0
    #: 单次重锚允许的最大修正量（ms）。超过 ⇒ 认为墙钟被调过，不跟。
    MAX_STEP_MS = 250.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        mono = time.monotonic()
        self._anchor_mono = mono
        self._anchor_epoch_ms = time.time() * 1000.0
        self._last_ms = int(self._anchor_epoch_ms)
        self._wall_gap_ms = 0.0          # 当前墙钟 − 单调推出的值
        self._max_gap_ms = 0.0           # 历史最大偏离（诊断用）
        self._refreshes = 0
        self._rejects = 0

    def from_mono(self, mono: float) -> int:
        """单调秒 → 墙钟 epoch 毫秒（单调不回退）。"""
        with self._lock:
            self._maybe_reanchor(mono)
            ms = int(round(self._anchor_epoch_ms + (mono - self._anchor_mono) * 1000.0))
            if ms < self._last_ms:
                ms = self._last_ms
            else:
                self._last_ms = ms
            return ms

    def now_ms(self) -> int:
        return self.from_mono(time.monotonic())

    def _maybe_reanchor(self, mono: float) -> None:
        if (mono - self._anchor_mono) < self.REFRESH_S:
            return
        predicted = self._anchor_epoch_ms + (mono - self._anchor_mono) * 1000.0
        wall = time.time() * 1000.0
        gap = wall - predicted
        self._wall_gap_ms = gap
        self._max_gap_ms = max(self._max_gap_ms, abs(gap))
        if abs(gap) > self.MAX_STEP_MS:
            # 墙钟被调过了（NTP 回拨/前跳）。**不跟**，只记下来。
            self._anchor_mono = mono
            self._anchor_epoch_ms = predicted
            self._rejects += 1
            return
        self._anchor_mono = mono
        self._anchor_epoch_ms = wall
        self._refreshes += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "source": "monotonic+wall-anchor",
                "refreshes": self._refreshes,
                "rejects": self._rejects,
                "wall_gap_ms": round(self._wall_gap_ms, 3),
                "max_gap_ms": round(self._max_gap_ms, 3),
                "note": ("墙钟被调过（|gap| > 250ms）时不跟随，只在此报告："
                         "宁可绝对值略差，也不在会话中途跳变"),
            }


_ANCHOR = WallAnchor()


def now_mono() -> float:
    """本机单调秒。**数据链路算时间差一律用它。**"""
    return time.monotonic()


def mono_to_ms(mono: Optional[float]) -> Optional[int]:
    """本机单调秒 → PC 墙钟 epoch 毫秒。"""
    return None if mono is None else _ANCHOR.from_mono(float(mono))


def now_ms() -> int:
    """PC 墙钟 epoch 毫秒 —— 全系统唯一的时间基。

    ⚠️ 由**单调时钟推进**（见模块 docstring ②），不直接取 `time.time()`：
    后者会被 NTP 回拨，一次回拨就能让时间轴上出现负的时间间隔。
    """
    return _ANCHOR.now_ms()


def anchor_snapshot() -> dict:
    return _ANCHOR.snapshot()


def now_iso(ms: Optional[int] = None) -> str:
    ms = now_ms() if ms is None else int(ms)
    return datetime.fromtimestamp(ms / 1000.0).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{ms % 1000:03d}"


def ns_to_ms(ns: Optional[int]) -> Optional[float]:
    return None if ns is None else float(ns) / 1e6


# ============================================================ 手机侧跨机偏移

class Clock:
    """手机侧（跨机）偏移估计 + 主时钟快照。

    ⚠️ **固件毫秒时基不在这里估** —— 那是上游 `mc.timesync` 的活（见模块 docstring ①）。
    这个类只管 ②（单调↔墙钟锚点，已由 `WallAnchor` 全局维护）与 ③（手机 epoch 偏移）。
    线程安全（状态只读为主，写入是原子的）。
    """

    #: 未标定时的诚实不确定度（ms）
    UNKNOWN_UNCERTAINTY_MS = 250.0

    def __init__(self, offset_ms: float = 0.0, uncertainty_ms: float = UNKNOWN_UNCERTAINTY_MS,
                 source: str = "assumed") -> None:
        self._lock = threading.Lock()
        self._offset_ms = float(offset_ms)
        self._uncertainty_ms = float(uncertainty_ms)
        self._source = str(source)
        self._method = ""
        self._samples: List[Tuple[int, float]] = []   # (pc_ms, device_ms)
        self._history: List[dict] = []

    # ------------------------------------------------------------ 读
    @property
    def offset_ms(self) -> float:
        return self._offset_ms

    @property
    def uncertainty_ms(self) -> float:
        return self._uncertainty_ms

    @property
    def source(self) -> str:
        return self._source

    @property
    def calibrated(self) -> bool:
        return self._source != "assumed"

    def to_pc_ms(self, device_ms: Optional[float]) -> Optional[int]:
        """手机时钟毫秒 → PC 墙钟毫秒。"""
        if device_ms is None:
            return None
        return int(round(float(device_ms) - self._offset_ms))

    def to_pc_ms_from_ns(self, device_ns: Optional[int]) -> Optional[int]:
        return self.to_pc_ms(ns_to_ms(device_ns))

    def snapshot(self) -> dict:
        """⚠️ 键名保持向后兼容（判据 I4 会在 meta.json 里读 offset_ms/source/uncertainty_ms）。

        `device` 段由 `Ctx` 另行并入 —— 那是上游 `mc.timesync` 的结论，
        与本类的"手机偏移"是两回事，**不要合并成一个数**。
        """
        return {
            "offset_ms": round(self._offset_ms, 3),
            "uncertainty_ms": round(self._uncertainty_ms, 3),
            "source": self._source,
            "method": self._method,
            "calibrated": self.calibrated,
            "samples": len(self._samples),
            "which": "手机侧(wall_clock_ns) 相对 PC 的偏移",
            "anchor": anchor_snapshot(),
            "note": ("offset 未标定时，视觉轨迹与遥测可能有整体错位；"
                     "场地页的 Δt 读数会因此失真"),
        }

    # ------------------------------------------------------------ 写
    def set_offset(self, offset_ms: float, uncertainty_ms: float, source: str,
                   method: str = "") -> dict:
        with self._lock:
            self._offset_ms = float(offset_ms)
            self._uncertainty_ms = float(uncertainty_ms)
            self._source = str(source)
            self._method = method or str(source)
            self._history.append({"t_pc_ms": now_ms(), **self.snapshot()})
            return self.snapshot()

    # ------------------------------------------------------------ L1 同步脉冲
    def add_sample(self, pc_ms: int, device_ms: float) -> None:
        """记一对"同一物理事件"在两侧的时刻。

        怎么拿到这样一对：见 `fit_sync_pulse` 的说明。sample 差 = offset 的一个观测。
        """
        with self._lock:
            self._samples.append((int(pc_ms), float(device_ms)))

    def clear_samples(self) -> None:
        with self._lock:
            self._samples.clear()

    def fit_sync_pulse(self) -> dict:
        """用已收集的同步脉冲样本拟合 offset。

        ⚠️ **怎么才算一对合格的样本**（这是这一步唯一难的地方）：
        需要一次**同时被两路观测到**的事件。当前可用的两种做法：

          a) 手机侧取一帧的 `wall_clock_ns`，PC 侧用**人工按键**时刻
             —— 误差 = 人的反应时间（~150 ms），只够粗对齐，`source=sync_pulse`，
             不确定度必须按实测反应时间散布写（本函数用样本 MAD）。
          b) 让手机 App 与 PC **短暂共享一次网络时刻**（例如让手机与控制机
             对同一个 NTP 源），此时 offset 直接可算。

        样本数 < 3 时**拒绝给出结论**（一个样本不能叫标定）。
        """
        with self._lock:
            samples = list(self._samples)
        if len(samples) < 3:
            raise ValueError(
                f"同步脉冲样本仅 {len(samples)} 个（少于 3 个）：拒绝拟合。"
                "一个样本不能叫标定：请按 §7.4 的标定流程多做几次。")
        diffs = [dev - pc for pc, dev in samples]
        med = statistics.median(diffs)
        mad = statistics.median([abs(d - med) for d in diffs]) or 0.0
        # MAD -> 正态 sigma 的常用换算（1.4826），再放大 3 倍当作保守上界
        unc = max(1.0, 1.4826 * mad * 3.0)
        return self.set_offset(med, unc, "sync_pulse",
                               method=f"L1 同步脉冲 n={len(samples)} MAD={mad:.1f}ms")

    # ------------------------------------------------------------ L2 最小延迟
    def fit_min_latency(self, pairs: Iterable[Tuple[int, float]],
                        quantile: float = 0.05) -> dict:
        """`(pc_arrival_ms, device_capture_ms)` 样本 → offset 的有偏上界。

        取分位数而不是最小值：最小值的方差太大（一个偶然的调度抖动就能污染它），
        分位数既免疫奇异点又保持"接近上界"。

        ⚠️ 上游 `mc.timesync.min_delay_offset()` 用的是**取最小值**（NTP 的 min-filter），
        因为我们这边拿到的是手机的 `wall_clock_ns`（拍摄时刻，**不是发送时刻**），
        而且样本量小（一场会话几十帧），分位数更稳。**两个函数解决的是两个跨机问题**，
        不要因为"看起来像"就合并。
        """
        pairs = [(int(p), float(d)) for p, d in pairs]
        if len(pairs) < 20:
            raise ValueError(
                f"最小延迟拟合需要 ≥20 帧样本，当前只有 {len(pairs)} 帧。"
                "帧太少时该估计没有任何统计意义。")
        diffs = sorted(d - p for p, d in pairs)
        k = max(0, min(len(diffs) - 1, int(quantile * len(diffs))))
        est = diffs[k]
        lo, hi = diffs[0], diffs[max(0, min(len(diffs) - 1, int(0.5 * len(diffs))))]
        unc = max(1.0, (hi - low_guard(lo)))
        return self.set_offset(est, unc, "l2_minlatency",
                               method=(f"L2 最小延迟 q{quantile:.2f} n={len(pairs)} "
                                       f"范围[{lo:.1f},{hi:.1f}]ms（偏上界）"))


def low_guard(x: float) -> float:
    """内部小工具：把下界保持为 0 参与宽度计算，避免负数放大不确定度。"""
    return min(0.0, x)
