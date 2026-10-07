"""时间同步 —— 把"设备毫秒时基"映射到"本机单调时钟", 从而算出**每个接收帧的准确时间**。

对应需求: "为了同步时间, 电脑鼠从发车命令开始其 LED 以 1Hz 闪烁, 同时需要记录其发车时间
和时间间隔来计算每一次接收到数据的准确时间。"

## 为什么必须做这件事

蓝牙透传是**有缓冲、有抖动**的: 一帧从固件发出到主机收到, 延迟不是常数。用"主机收到时刻"
当数据的时间, 时间轴会被链路抖动污染 —— 而"检测运动特性"恰恰要看毫秒级的时间关系。
靠主机自己**无法**恢复真实发送时刻, 所以必须让设备把它的时间告诉我们。

## v2.0 的时间锚机制(与 v1.5.5 的差异)

| 元素 | 载体 | 作用 |
|---|---|---|
| **毫秒时基** | 固件 `linkTimeMs`(SysTick 1ms 自增, u32) | 设备侧单调时间, 不受链路影响 |
| **锚** | `TELEM` 的应答 `ACK <seq> OK <upMs>`(完整 u32) | 每次拉取都刷新 |
| **低 8 位** | 每帧统一帧的 `ms8`(= `linkTimeMs & 0xFF`) | 逐帧定位到 256ms 窗口 |
| **物理灯** | `ARM` 后 LED 以 1Hz(50% 占空)闪烁 | 供摄像头/人眼做**独立校核** |

统一帧**不带完整时基**(省带宽): 主机用"锚 + 本机流逝"预测一个粗值 `predicted`,
再用 `resolve_ms8(ms8, predicted)` 在同余类里挑最接近的那个 —— 只要预测误差 < 128ms
就必然选对(模周期 256ms 容 55 帧, 绰绰有余)。

**失锁判定**: 若还原值偏离预测 > 128ms, 说明时钟跑偏/锚太旧 —— 记一次丢失并**重新索要锚**,
绝不静默给一个错的时间。

## 单程延迟: 只能估, 但估得准

到达时刻 = 发送时刻 + 链路延迟, 而延迟未知且恒为正。经典做法是**取最小值**:
在若干次观测里, `t_recv - dev_ms/1000` 的**最小值**对应"延迟最小的那一次",
是真实偏移的最优估计(其余观测都被更大的延迟污染了)。

    offset = min( t_recv_i - up_ms_i / 1000 )

LED 基准灯则可以给出**独立**的延迟估计(人/摄像头量"灯亮"与"收到 armed"的差),
两边一比就知道估计是否可信 —— 这是把物理灯一起做的意义所在。

参见 `docs/数据收集与时间同步.md`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from ..clock import now_monotonic
from .errors import Result, clamp_timeout

if TYPE_CHECKING:  # pragma: no cover
    from .client import MouseClient


# ============================================================ 纯函数: 拟合与解算
# 全部不碰设备, 因此可以脱离硬件单测(见 host/timesync_smoke.py)。

def min_delay_offset(samples: List[Tuple[float, int]]) -> Optional[float]:
    """单程延迟下的**偏移估计**: `offset = min(t_recv - dev_ms/1000)`。

    参数 samples: [(收到时刻(单调秒), 设备毫秒时基)]。
    返回 offset(秒), 使 `t_pc = offset + dev_ms/1000`。样本为空返回 None。

    为什么取最小而不是平均: 延迟恒为正, 平均值等于"真偏移 + 平均延迟";
    最小值才逼近"真偏移 + 最小延迟"。这是 NTP 里就在用的 min-filter 思想。
    """
    if not samples:
        return None
    return min(t - ms / 1000.0 for t, ms in samples)


def fit_clock(samples: List[Tuple[float, int]]) -> Optional[Tuple[float, float]]:
    """最小二乘拟合 `dev_ms/1000 = a + b * t_recv`, 返回 (a, b)。

    b 是"设备秒 / PC 秒"的比值, 理想为 1.0; `(b-1)*1e6` 即相对频差(ppm)。
    样本少于 3 个(或时间跨度太短)返回 None —— 那样拟合没有意义。
    """
    n = len(samples)
    if n < 3:
        return None
    ts = [s[0] for s in samples]
    ds = [s[1] / 1000.0 for s in samples]
    if max(ts) - min(ts) < 1e-3:
        return None
    mt = sum(ts) / n
    md = sum(ds) / n
    num = sum((t - mt) * (d - md) for t, d in zip(ts, ds))
    den = sum((t - mt) ** 2 for t in ts)
    if den <= 0:
        return None
    b = num / den
    a = md - b * mt
    return a, b


def drift_ppm(samples: List[Tuple[float, int]]) -> Optional[float]:
    """设备相对本机的频率偏差(ppm)。取不到有效拟合时返回 None。"""
    fit = fit_clock(samples)
    if fit is None:
        return None
    return (fit[1] - 1.0) * 1e6


def resolve_ms8(predicted_up_ms: float, ms8: int, window: int = 256) -> int:
    """把统一帧里的 8 位时间戳(低 8 位)**还原成完整设备时基**。

    做法: 以"预测值"为基准, 在 `predicted + k*window` 里挑与 `ms8` 同余且最接近
    预测值的那一个。只要预测误差 < 128ms 就必然选对 —— 而锚 + 单调时钟预测,
    误差远小于此。

    参数 window 缺省 256(= 2^8), 即"低 8 位"的模。

    注意参数顺序: `(predicted_up_ms, ms8)` —— 与 v1.5.5 的 `(ms8, predicted)` 相反,
    因为 v2.0 的调用点总在"先有锚/预测"之后。
    """
    p = float(predicted_up_ms)
    m = int(ms8) & 0xFF
    best = None
    for k in (-1, 0, 1, 2):
        cand = int(p) - (int(p) % window) + k * window + m
        if best is None or abs(cand - p) < abs(best - p):
            best = cand
    return int(best if best is not None else int(p))


#: 同余还原的容差: 偏差超过半个模周期(128ms) 说明同余解不唯一 -> 判失锁。
REQUIRE_ANCHOR_DELTA_MS = 128
#: 锚的最长有效期 —— 超过则主动重新索要(晶振 100ppm = 3.6ms/h, 远未到 128ms,
#: 但锚可能因设备复位而失效, 所以给一个保守上限)。
ANCHOR_MAX_AGE_S = 120.0


# ============================================================ 状态与结果

@dataclass
class TimeSyncState:
    """一次会话的时间同步状态快照。"""

    has_anchor: bool = False
    anchor_dev_ms: Optional[int] = None
    armed: bool = False
    arm_up_ms: Optional[int] = None      # 发车时刻(设备时基)
    arm_pc_mono: Optional[float] = None  # 发车时刻(本机单调)
    offset_s: Optional[float] = None     # min-delay 偏移
    drift_ppm: Optional[float] = None    # 相对频差
    one_way_ms: Optional[float] = None   # 由偏移反推的"最小单程延迟"估计
    samples: int = 0
    latest_up_ms: Optional[int] = None
    locked: bool = True                  # 同余还原是否可信
    lost: int = 0                        # 失锁(需重新索要锚)次数
    anchor_age_s: Optional[float] = None
    maze_run: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "has_anchor": self.has_anchor,
            "anchor_dev_ms": self.anchor_dev_ms,
            "armed": self.armed,
            "arm_up_ms": self.arm_up_ms,
            "offset_s": None if self.offset_s is None else round(self.offset_s, 6),
            "drift_ppm": None if self.drift_ppm is None else round(self.drift_ppm, 2),
            "one_way_ms": None if self.one_way_ms is None else round(self.one_way_ms, 3),
            "samples": self.samples,
            "latest_up_ms": self.latest_up_ms,
            "locked": self.locked,
            "lost": self.lost,
            "anchor_age_s": None if self.anchor_age_s is None else round(self.anchor_age_s, 2),
            "maze_run": self.maze_run,
        }


# ============================================================ 门面

class TimeSyncAPI:
    """时间同步(门面 `mc.timesync`)。

    典型用法::

        mc.stream.on()             # 拉取遥测; 每条 TELEM 应答都带完整 u32 时基, 锚自动建立
        mc.timesync.arm()          # 发车: 固件点亮 1Hz 基准灯, 应答体带 <armMs>
        ...
        mc.timesync.snapshot()     # 看锚/偏移/漂移/样本数
        t_pc = mc.timesync.frame_time(rec)   # 把某一帧换算成"发车以来的准确时刻"
    """

    #: 保留最近 N 个 (收到时刻, 设备时基) 观测用于拟合
    MAX_SAMPLES = 600

    def __init__(self, mc: "MouseClient") -> None:
        self._mc = mc
        self.reset()

    def reset(self) -> None:
        """清空全部观测与发车状态(重连/换设备时必须调)。"""
        self._samples: List[Tuple[float, int]] = []
        self.arm_pc_mono: Optional[float] = None
        self.arm_up_ms: Optional[int] = None
        self.armed = False
        self.locked = True
        self.lost = 0

    # ------------------------------------------------------------ 观测采集

    def note_frame(self, rec: dict) -> Optional[int]:
        """由读取线程喂入一帧统一帧记录(内部用, 也可手工喂)。

        用锚 + 本机流逝预测粗值, 再用 `resolve_ms8` 做同余还原; 顺带记入拟合样本。
        取不到锚或失锁时返回 None(此时该帧不参与时间轴)。
        """
        ms8 = rec.get("ms8")
        if ms8 is None:
            return None
        anchor = self._mc.store.get_anchor()
        if anchor is None:
            return None
        t_anchor, dev_anchor = anchor
        t_recv = float(rec.get("_rx_mono") or now_monotonic())
        predicted = self._predict(dev_anchor, t_anchor, t_recv)
        up = resolve_ms8(predicted, int(ms8))
        if abs(up - predicted) > REQUIRE_ANCHOR_DELTA_MS:
            # 同余解不唯一 -> 锚太旧或时钟跑偏, 判失锁, 交给上层重新索要锚。
            self.locked = False
            self.lost += 1
            return None
        self.locked = True
        self._samples.append((t_recv, up))
        if len(self._samples) > self.MAX_SAMPLES:
            del self._samples[: len(self._samples) - self.MAX_SAMPLES]
        return up

    def _predict(self, dev_anchor: int, t_anchor: float, at: float) -> float:
        ppm = self.drift_ppm() or 0.0
        rate = 1000.0 * (1.0 + ppm / 1e6)
        return float(dev_anchor) + (at - t_anchor) * rate

    def on_arm_ack(self, up_ms: int) -> None:
        """处理 `ARM` 的应答(发车回执), 其状态串是 `OK <armMs>`。"""
        self.armed = True
        self.arm_up_ms = int(up_ms)

    def needs_anchor(self) -> bool:
        """是否需要(重新)索要时间锚。"""
        if not self.locked:
            return True
        anchor = self._mc.store.get_anchor()
        if anchor is None:
            return True
        return (now_monotonic() - anchor[0]) > ANCHOR_MAX_AGE_S

    # ------------------------------------------------------------ 映射

    def offset_s(self) -> Optional[float]:
        """当前偏移估计(秒)。`t_pc_mono = offset + up_ms/1000`。"""
        return min_delay_offset(self._samples)

    def drift_ppm(self) -> Optional[float]:
        return drift_ppm(self._samples)

    def frame_time_up(self, rec: dict) -> Optional[int]:
        """把一帧记录换算成**设备毫秒时基**上的发送时刻。"""
        if "ms8" not in rec:
            return None
        return self.note_frame(rec)

    def frame_time(self, rec: dict) -> Optional[float]:
        """把一帧记录换算成**本机单调秒**(即"这一帧真正发生的时刻")。

        计算: `offset + up_ms/1000`。offset 用 min-delay 估计, 因此它已经把
        "最小单程延迟"吃进去了 —— 绝对值上仍差一个常数(未知的最小延迟),
        但**任意两帧之间的时间差是准确的**, 这正是运动特性分析需要的。
        """
        up = self.frame_time_up(rec)
        if up is None:
            return None
        off = self.offset_s()
        if off is None:
            return None
        return off + up / 1000.0

    def dev_ms(self, rec: dict) -> Optional[int]:
        """把一帧换算成"发车以来毫秒数"。未发车或取不到时基时返回 None。"""
        up = self.frame_time_up(rec)
        if up is None or not self.arm_up_ms:
            return None
        return int(up - self.arm_up_ms)

    def dev_ms_from_recv(self, rec: dict) -> Optional[int]:
        """给取不到毫秒时基的帧估算"发车以来毫秒数"(兜底, 仅为 CSV 有一列时间)。

        用到达时刻投影到设备时基。会**偏大约一个单程延迟**。
        """
        if not self.arm_up_ms:
            return None
        anchor = self._mc.store.get_anchor()
        if anchor is None:
            return None
        t_anchor, dev_anchor = anchor
        t = float(rec.get("_rx_mono") or now_monotonic())
        return int(self._predict(dev_anchor, t_anchor, t) - self.arm_up_ms)

    # ------------------------------------------------------------ 状态

    def snapshot(self) -> TimeSyncState:
        off = self.offset_s()
        anchor = self._mc.store.get_anchor()
        st = TimeSyncState(
            has_anchor=anchor is not None,
            anchor_dev_ms=None if anchor is None else int(anchor[1]),
            armed=self.armed,
            arm_up_ms=self.arm_up_ms,
            arm_pc_mono=self.arm_pc_mono,
            offset_s=off,
            drift_ppm=self.drift_ppm(),
            samples=len(self._samples),
            locked=self.locked,
            lost=self.lost,
        )
        if anchor is not None:
            st.anchor_age_s = now_monotonic() - anchor[0]
        if self.arm_pc_mono is not None and off is not None and self.arm_up_ms is not None:
            st.one_way_ms = (self.arm_pc_mono - (off + self.arm_up_ms / 1000.0)) * 1000.0
        if self._samples:
            st.latest_up_ms = int(self._samples[-1][1])
        return st

    # ------------------------------------------------------------ 命令

    def arm(self, timeout: float = 3.0) -> Result:
        """**发车**: 让固件记下发车时刻并点亮 1Hz 时间基准灯。

        应答体是 `ACK <seq> OK <armMs>` —— 从状态串里取发车时基。
        本机会同时记下"发出命令的单调时刻", 与 armMs 一起给出单程延迟的独立估计。
        """
        self.arm_pc_mono = now_monotonic()
        r = self._mc.send_cmd("arm", timeout=timeout)
        if r.ok:
            parts = str(r.status or "").split()
            if len(parts) >= 2 and parts[0].upper() == "OK":
                try:
                    self.on_arm_ack(int(parts[1]))
                except ValueError:
                    self.armed = True
            else:
                self.armed = True
        return r

    def maze_run(self, target: str = "BLIND", timeout: float = 3.0) -> Result:
        """远程发车(仅**远程档** `MOUSE_REMOTE`; 比赛固件回 ERR_VALUE)。

        `target` 与 `mc.maze.run()` 同义: `"BLIND"`(默认) 无记忆走迷宫,
        `"RACE"` 走正式比赛入口序列。数据收集台通常只用 BLIND。
        """
        return self._mc.send_cmd("maze.run", target=str(target or "BLIND").upper(),
                                 timeout=timeout)

    def wait_sync(self, samples: int = 2, timeout: float = 5.0) -> bool:
        """等至少 `samples` 个可用帧到达(建立可用映射)。返回是否达到。"""
        deadline = now_monotonic() + clamp_timeout(timeout, 1.0)
        while now_monotonic() < deadline:
            if len(self._samples) >= samples:
                return True
            import time as _t

            _t.sleep(0.02)
        return len(self._samples) >= samples

    # ------------------------------------------------------------ 解释

    def explain(self) -> str:
        st = self.snapshot()
        lines = [
            f"时间锚      : "
            + (f"有 (设备 {st.anchor_dev_ms} ms, {st.anchor_age_s:.1f}s 前)"
               if st.has_anchor else "无 —— 需要 TELEM 应答才会建立"),
            f"同余还原    : " + ("正常" if st.locked else f"**失锁**(已发生 {st.lost} 次, 需重新索要锚)"),
            f"发车        : {'已发车' if st.armed else '未发车'}"
            + (f"  (设备时基 {st.arm_up_ms} ms)" if st.arm_up_ms else ""),
            f"可用帧样本  : {st.samples} 帧",
        ]
        if st.offset_s is not None:
            lines.append(f"偏移估计    : {st.offset_s:.6f} s (min-delay, 含最小单程延迟)")
        if st.one_way_ms is not None:
            lines.append(f"单程延迟估计: 约 {st.one_way_ms:.1f} ms")
        if st.drift_ppm is not None:
            lines.append(f"频差        : {st.drift_ppm:+.2f} ppm")
        if st.samples < 3:
            lines.append("提示: 样本还太少, 偏移/漂移估计不收敛 —— 多等几秒。")
        return "\n".join(lines)


# 便于 `from host.api.timesync import ...` 的使用方少写一层
__all__ = [
    "TimeSyncAPI",
    "TimeSyncState",
    "min_delay_offset",
    "fit_clock",
    "drift_ppm",
    "resolve_ms8",
]
