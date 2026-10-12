# -*- coding: utf-8 -*-
"""事件触发式拉取 (Event-triggered PULL): **周期 ≡ RTT, 自锁; 无需测 RTT, 无目标频率。**

用户方案(2026-10-13 确认)
------------------------
1. **基频仍是"PC 拉取、MCU 发送"** —— 架构不变, 单片机只按 `PULL <n> <t_ms>` 倒计时发帧;
2. **一个拉取周期内发多帧**(n > 1), "倍数"就是从这里来的;
3. **下一次拉取的触发 = 本周期第 1 帧到达 PC**(事件驱动, 不是定时发);
4. **单片机侧不等上一批发完** —— 新 PULL 一到即丢弃本批剩余、按新参数从第 1 帧重发(抢占语义);
5. n = 1 时与"已验证可靠"的老拉取模式(n=1 停等)**逐位同构**。

自锁性质(本工具的核心)
--------------------
    首帧到达 = send + d_down + d_up = send + RTT
而"收到本周期第 1 帧"又**立刻**触发下一条命令 ⇒
    S_{k+1} - S_k = RTT   ← **周期恒等于链路往返时间; 不需要测 RTT, 也不需要设目标频率**
由此推出的速度律:
    每周期实发帧数  N = min(n, floor(RTT / t) + 1)
    到达速率        ≈ N / RTT ≈ 1/t + 1/RTT
    命令频率        ≡ 1 / RTT        (与 n、t 都无关 —— 本方案唯一"费"的地方)
    帧间隔序列      = t, t, ..., t, (RTT mod t)
    ⇒ **均匀性只取决于 RTT mod t**; t 越小越均匀、越快。
上行侧: 同一批内的帧全部由**固件倒计时**决定, 实测 σ = 0.0 ms(535/535 精确 = t)。

为什么"n 不是速率旋钮, t 才是"
------------------------------
`n·t ≥ RTT` 之后抢占成为常态, 被丢弃的尾巴帧**从未发出**(不占链路、不算丢帧),
速率自己跑满; 反过来 `n·t < RTT` 会让单片机发完一批后**空转**等命令 ⇒ 白丢时间。
所以 n 只需"够大"(≥ floor(RTT/t)+1 并留余量), 设大了没坏处, 设小了才是损失。

固定 t 的隐含问题 / 动态 t (`--frames N`)
------------------------------------------
上面"帧间隔序列 = t,…,t,(RTT mod t)"里的**最后一项**是个隐患: 均匀性完全由 `RTT mod t`
决定, 而它与 t 的关系毫无规律。实测 RTT=300 时:
    t=83 ⇒ 间隔 83,83,83,51  极差 **32 ms**  (就是本轮跑的那个)
    t=76 ⇒ 间隔 76,76,76,72  极差 **4 ms**
    t=75 ⇒ 间隔 75,75,75,75,0 极差 **75 ms**  (75 | 300, 边界间隔直接塌成 0!)
相邻 1 ms 的 t 可以差 20 倍, 而 RTT 本身还会变 ⇒ **固定 t 不是"不好", 而是无法预知好不好**。

`--frames N` 把旋钮从 t 换成 N(每周期目标帧数, 也就是"基础拉取频率的倍数"), 每周期重算
    **t = floor(RTT / N) + 1**
由 (N−1)·t < RTT 且 N·t > RTT 推出 ⇒ 每周期**恰好** N 帧, 间隔极差降到 N − (RTT mod N) ∈ [1, N] ms。
注意是 floor+1 而**不是** round: 取 round 时只要 N | RTT 就会退化成 t = RTT/N, 于是
N·t = RTT、下一帧正落在周期末尾 ⇒ N_eff 变 N+1 且边界间隔塌成 0(见 test_evt_trigger_logic.py)。

RTT 从哪来(这一步必须用 ms8, 不能用主机时间戳):
  * 批起点帧的 ms8 做差 = 周期 = RTT, 且是 **MCU 时基**;
  * 实测同一轮里 ms8 周期 σ ≈ 0.7 ms, 而主机时间戳周期 σ ≈ 14 ms(全是 CH340/USB 攒包与
    调度抖动) —— 差 20 倍。拿主机时间戳去推 t 会抖到 75 那种灾难值, 比固定 t 更糟;
  * 取**滑窗中位数**(--rtt-window, 默认 8)抗离群: 原始周期里有 2/132 个 225/457 ms 的野值。

两条硬约束(离线自测里断言着):
  1. t ≤ 127 ms(ms8 只有 8 位, 再大就与 256 ms 折回混淆) ⇒ **N 有下限 N > RTT/127**。
     RTT=300 时 N ≥ 3(≈10 Hz); 也就是说**帧判据**做不到 N=1(纯单帧拉取 ≈3.3 Hz)——
     那个档位根本没有"批内间隔"可判, 只能换 `--trigger ack` 或固定 `--t-ms`。
  2. 帧判据的 tol 必须 **< 1 ms**: 批起点间隔最多只比 t 短 (t − bd = N − (RTT mod N)),
     最坏恰好 1 ms; tol ≥ 1 会把边界判成批内 ⇒ **漏触发、环路停摆**。

动态 t 下**判据必须按"上一帧所属批"取 t**(见 `t_of_batch`): 批内间隔精确等于**该批发射时**
的 t, 而 t 会逐周期变 1~5 ms; 一律用当前 t 会让旧批的批内间隔被误判成丢帧。

⚠️ 而"帧属于哪一批"**只能在收到该批命令的 ACK 时打标**(见 `make_ack_cb`)。MCU 收到第 k 条
命令的那一刻才(重新)开始第 k 批的发射, 所以"这条 ACK 之后的帧"才属于第 k 批; 若改在**发送
时刻**打标, 此刻还在途的上一批尾帧会被贴上**新批号**, 而 `t_of_batch` 一查就是**新 t**
⇒ 旧批批内间隔(57)被拿去和新 t(62) 比 ⇒ `|差| > tol` ⇒ **误判成批起点 ⇒ 多发命令 ⇒ 周期
腰斩** ⇒ 主机周期中位数塌一半 ⇒ 解缠参照塌 ⇒ t 继续掉 —— 一个自锁的发散环。2026-10-13 真机
`--frames 4` 实测: t 轨迹 `83→57→62→68→38`、周期 `308→134`, 15 个周期内就崩掉。

⚠️ 同理, **RTT 的样本来源刻意绕开间隙分类**: "哪一帧是批首"由**哪一次 ACK** 定义(收 ACK 时
记下的帧下标 `ack_idx`), 而不是由"短间隔"猜出来 ⇒ 分类判错只影响计数与触发, 污染不到 RTT。

⚠️ **解缠必须按 75 ms 物理格点判, 不能按"离某个参照最近"**(见 `unwrap_rtt`)。ms8 只有 8 位,
周期/RTT 必过 256 折回; 真机实测 RTT 只取 150/225/300/375(全是 75 的整数倍), 而 ±256 的错分支
恰好偏离格点 `256 mod 75 = 31 ms` ⇒ **"哪个分支落在格点上"直接定出真值**。2026-10-13 真机踩到:
第 101 周期链路停顿 547 ms 后 **RTT 由 300 换到 150**, 旧法(离参照最近)把 150 解成 **406**,
一致性闸又按旧的 300 判 `|406-300|=106 > 80` 把它拒掉 ⇒ **RTT 估计冻在旧档、t 不跟手**(实测
204 个样本被拒、`t` 卡在 76/102)。现改为: 候选先过格点筛(无歧义), 闸只做**合理性**
(`60..900 ms` 且"在格点上 或 离参照 ≤80") —— 环路崩塌的防护由**打标时机**承担, 不再靠这道闸。
`early_trig` 计数"明显早于 RTT"的触发, 是判据退化的信号灯(参照取**当前最优估计**)。
另: `off_lattice` 计数落在格点外的样本 —— 它是"75 ms 量化模型还成不成立"的体检指标。

起步 = 单帧拉取 (`--bootstrap K`, 默认 2)
-----------------------------------------
用户口径(2026-10-13): **第一轮一次拉取只传一帧**。理由是**起步阶段是最薄弱的一环**:

* 动态 t 这时还没有 RTT, 只能拿种子 `--t-ms` 去猜 —— 实测 `--frames 4` 的前 3 个周期都在用
  种子 t=83(逐周期表 `t_ms` 列 `83,83,83,76,76,76`), 正确的 t=76 要到第 4 个周期才用上;
* 起步轮的周期本身就最不稳(实测第 2 个周期 348 ms vs 稳态 300 ms);
* n=1 时**不需要 t 正确**: 单帧发完即静默, 下一条命令由"帧到达"驱动 ⇒ 与已验证稳定的老
  拉取模式**逐字节相同**, 不把"多发帧"这个新变量压在第一轮上。

而 n=1 时相邻帧间隔 = 周期 = RTT(主机时间戳无歧义) ⇒ **这几轮顺带把 RTT 测准**, 切到 N 帧时
t 已经收敛, 于是"种子 t 过渡期"整个消失。**最少要 2 轮**: ms8 的 RTT 样本要两个批起点帧做差,
1 轮算不出来(开 `--frames` 时 < 2 会被自动抬到 2)。

⚠️ 起步轮 n=1 ⇒ 没有"批内间隔"可判, **帧判据失效** ⇒ 这一段**强制走 ACK 通路**
(`in_boot` 时无视 `--trigger frame`), 否则会在起步轮干等到 `--max-wait-ms` 才兜底补发。

触发判据 (--trigger) —— 为什么默认是 auto
---------------------------------------
两个可用的边界信号:

* **ACK(auto/ack)**: `RATE PULL` 会回 `ACK <seq> OK`。固件里顺序是
  `link_PullSet()`(只置 `linkPullTimer=0`, 帧要等**下一个 1ms tick** 才进 TX 环)
  **先**、`link_Ack()`(字节立刻进环)**后** ⇒ **ACK 比本批第 1 帧早约 5~6 ms 到达**
  (ACK 14B vs 帧 59B @115200)。无歧义、与 t 无关、"批内丢帧"也不会误触发; 代价是依赖 ACK 不丢。
* **帧间隙(auto/frame)**: 批内间隔恒 = t(σ=0), 所以"与前一帧间隔 ≠ t"的那一帧就是批起点。
  用 **ms8**(帧内单片机毫秒时基, 真值), 不用主机时间戳(被 CH340/USB 攒包污染 σ≈75 ms)。
  ⚠️ **有死角**: 批起点间隔 = RTT mod t; 当 **RTT 恰是 t 的整数倍**时, 本批最后一帧落在
  A+k·t、而新命令也在 A+k·t 到达 ⇒ 那帧被抢占, 真正的前一帧是 A+(k−1)·t ⇒
  **批起点间隔恰好等于 t, 与批内完全一样**, 判据永远看不到边界 ⇒ 死锁。
  (实测 RTT≈150ms 是常见值, 取 t=75 正好踩中 —— 所以 frame 单用不安全。)
  frame 判据另有两条性质: 短间隔(<t) = 批起点; 长间隔(>t) = **丢帧**(不能当批起点)。
  需 t < 128 ms(ms8 只有 8 位)。

⇒ 默认 `--trigger auto` = **ACK 为主 + 帧间隙为备**(谁先到算谁), 兼得鲁棒与"收到第 1 帧"的语义。

兜底: `--max-wait-ms` 内既无触发也无新帧 ⇒ 强制补发一条 PULL(防死锁), 并计数报告。
`--min-period-ms`(默认 30)是**硬闸**: 两条命令不得快于它。正常周期 ≡ RTT(150~300 ms),
故它永不生效; 但它是最后一道保险 —— 踩过的坑: 早期把判死状态写成 `trig="dead"` 而
`trig_kind=None`, 于是"连续 3 s 无帧"既不打印也不跳出, 反被当成兜底补发 ⇒ 链路一死就以
**775 条/秒**狂灌蓝牙模块(实测一次 12493 条)。灌爆的透传模块不自愈、必须物理断电, 所以
现在只用**一个** `trig` 变量承载状态, 并在发送前无条件过一遍最小间隔硬闸。

产物
----
* `evt_<tag>_frames.csv`  逐帧: idx, t_s, gap_host_ms, gap_mcu_ms, ms8, batch, drop_u8
      (`batch` 列 = 周期号, 现在**在收到该批 ACK 时才打标**(读线程), 位置正确;
       但混合码流下仍建议用 `analyze_pace_gaps.py <csv> --target <t>` 的**按值分组**模式)
* `evt_<tag>_periods.csv` 逐周期: period_idx, trig, period_ms, n_frames, boundary_gaps_ms,
                                t_ms, n_used
      (`t_ms` 列只在本周期用了动态 t(且非起步单帧轮)时才有值; `n_used` = 该周期实际拉取的帧数,
       起步轮恒为 1 —— 速度律自检按它算, 否则会把起步轮全报成"不符")
      (用来直观看"周期是否双峰"以及双峰与触发判据 ack/frame 的对应关系)
* `evt_<tag>.csv`         逐秒: t_s, periods, frames, recv_hz, cmd_hz, period_ms,
                                n_per_period, rtt_ms, drop_u8, alive, note
* `evt_<tag>_summary.txt` 汇总: 速率/周期≡RTT 分布/N 分布/ms8 直方图/触发与兜底统计/丢帧字节

用法
----
    python link_pull_evt.py COM7 --batch 8 --t-ms 83 --duration 120 --tag evt_n8t83
    python link_pull_evt.py COM7 --frames 4 --duration 120 --tag evt_N4     # 动态 t(N=4 ⇒ t=76)
    python link_pull_evt.py COM7 --frames 4 --bootstrap 2 --duration 120 --tag evt_N4b  # 起步单帧(默认)
    python link_pull_evt.py COM7 --frames 4 --bootstrap 0 --duration 120 --tag evt_N4raw # 关起步(对照)
    python link_pull_evt.py COM7 --frames 6 --duration 120 --tag evt_N6     # ≈20 Hz 档
    python link_pull_evt.py COM7 --batch 8 --t-ms 45 --duration 300 --append-matrix evt_matrix.csv
    python link_pull_evt.py COM7 --trigger frame --batch 8 --t-ms 83 --duration 120   # 单用帧判据
`--frames N` 只要给 N 就行: t 每周期由 RTT 推出, `--batch` 会自动抬到 ≥ N。
`--bootstrap K` 默认 2: 起步 K 个周期用单帧拉取(与老模式逐字节相同)并顺便测准 RTT, 之后转 N 帧。
只发 PING / RATE PULL / RATE OFF, **不含任何运动命令**。需在本目录(host/)下运行。
"""
from __future__ import annotations

import argparse
import bisect
import collections
import csv
import math
import os
import statistics
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ".")

from link_pull_pace import FrameLog, pctl, send_cmd  # noqa: E402
from host.api import MouseClient  # noqa: E402


class EvtFrameLog(FrameLog):
    """FrameLog + 一个"有新帧到达"的事件(与 ACK 回调共用, 供触发循环低延迟唤醒)。

    ⚠️ 事件在 inner hook **之后** set, 所以主循环看到事件时该帧一定已经登记进
       `times/ms8s`, 不存在"事件到了但帧还没进列表"的竞态。
    """

    def __init__(self, store, ev=None):
        super().__init__(store)
        self.ev = ev if ev is not None else threading.Event()
        inner = store.on_frame

        def hook(stream, rec):
            inner(stream, rec)
            if stream == "unified":
                self.ev.set()

        store.on_frame = hook

    def batch_of(self, i: int):
        """第 i 帧归属的批号(用于按批取该批发射时用的 t —— 动态 t 下判据必须按批取)。"""
        with self._lock:
            return self.batches[i] if 0 <= i < len(self.batches) else None


def unwrap_gap(raw: int, t: int) -> float:
    """把 8 位 ms8 差解缠成"最接近 t 的"真值(批内 = t, 批起点 = RTT mod t < t)。

    ⚠️ **只用于帧间隙**(真值必 < 256: 批内 = t ≤ 127, 批起点 = RTT mod t < t)。
    周期/RTT 可到数百 ms、必过 256 折回, 那种场合要用下面的 `unwrap_rtt`。
    """
    return float(min((raw, raw - 256, raw + 256), key=lambda v: abs(v - t)))


# ---- RTT 的物理格点: 下行命令通路的 75 ms 量化(docs §11 / §14.9) ----
#  真机实测 RTT 只落在 **150 / 225 / 300 / 375 ms**, 全是 75 的整数倍。
#  这给 8 位 ms8 的解缠一个**无歧义判据**: ±256 的错分支把真值挪到
#  `256 mod 75 = 31 ms` 的**格点外**, 而真值精确在格点上 ⇒ "哪个分支落在格点上"直接
#  定出真值。不再用"离某个参照最近"—— 那个判据在 RTT **换档**时会把 150 误判成 406
#  (2026-10-13 真机 `--frames 4` 跑到第 101 周期时踩到: RTT 300→150, 样本被解成 406,
#   一致性闸按 300 判又把它拒掉 ⇒ RTT 估计冻在旧档, t 不跟手)。
RTT_QUANT = 75.0
LATTICE_TOL = 12.0       # 允许偏离格点的余量(真值精确在格点上, 留一点给固件抖动)


def unwrap_rtt(raw: int, ref: float | None = None, lo: float = 60.0, hi: float = 900.0):
    """把 ms8 的 8 位差解缠成**周期/RTT**(可达数百 ms, 必过 256 折回)。

    候选 {raw, raw±256} 里先取"落在 75 ms 格点上"的; 若都不在格点上(异常样本),
    退化成"离 ref 最近"(ref 缺省 = raw)。返回 `(值, 是否落在格点上)`。
    """
    cands = [v for v in (raw, raw - 256, raw + 256) if lo <= v <= hi]
    if not cands:
        return float(raw), False

    def resid(v: float) -> float:
        return abs(v - RTT_QUANT * round(v / RTT_QUANT))

    on_lat = [v for v in cands if resid(v) <= LATTICE_TOL]
    if on_lat:
        pick = min(on_lat) if ref is None else min(on_lat, key=lambda v: abs(v - ref))
        return float(pick), True
    if ref is None:
        return float(min(cands, key=lambda v: abs(v - raw))), False
    return float(min(cands, key=lambda v: abs(v - ref))), False


def classify_gap(m8: int, prev_m8: int, t: int, tol: float):
    """把一个帧间 ms8 差分类 —— 这是帧判据的**全部**逻辑(抽出来便于离线自测)。

    返回 (kind, d):
      * "in"    —— 批内间隔, 精确等于 t(固件倒计时, 实测 535/535 精确 = t)
      * "bound" —— 批起点: 比 t **短**的间隔(= RTT mod t, 恒 < t)
      * "loss"  —— 丢帧: 比 t **长**的间隔(k·t, k≥2); **绝不能**当批起点, 否则误发命令
    """
    d = unwrap_gap((m8 - prev_m8) & 0xFF, t)
    if abs(d - t) <= tol:
        return "in", d
    return ("bound", d) if d < t else ("loss", d)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="事件触发式拉取(收到本周期第 1 帧就立刻发下一条 PULL; 周期≡RTT, 自锁)")
    ap.add_argument("port")
    ap.add_argument("--batch", type=int, default=8,
                    help="每次拉取的帧数 n(1..16)。只需 >= floor(RTT/t)+1, 设大无代价、"
                         "设小会让单片机在周期尾部空转")
    ap.add_argument("--t-ms", type=int, default=83,
                    help="批内帧间隔 ms(建议 10..127; >=128 会与 ms8 的 256 折回混淆)。"
                         "开 --frames 后它只当**起步种子**(第一个周期用), 之后由 RTT 推出")
    ap.add_argument("--frames", type=int, default=0,
                    help="【动态 t】目标帧数 N(0=关闭, 用固定 --t-ms)。开启后每周期重算 "
                         "t = floor(RTT/N)+1, 使 N 帧在周期内**等距铺满**。"
                         "N 就是「基础拉取频率的倍数」(基频 = 1/RTT = 单帧拉取), 故它才是速率旋钮")
    ap.add_argument("--rtt-window", type=int, default=8,
                    help="动态 t 的 RTT 滑窗长度(周期数), 取**中位数**。中位数抗离群, 别换均值")
    ap.add_argument("--bootstrap", type=int, default=2,
                    help="起步用**单帧拉取**(RATE PULL 1 t)的周期数(含第一条, 0=关闭)。"
                         "单帧拉取就是先前真机验证稳定的原模式 ⇒ 起步与它**逐字节相同**, "
                         "不拿种子 t 去多发帧; 且 n=1 时帧间隔 = 周期 = RTT ⇒ 这几轮顺带把 "
                         "RTT 测准, 切到 N 帧时 t 已经是**对的**。"
                         "**最少要 2**(ms8 的 RTT 样本要两个批起点帧做差, 1 轮算不出来), "
                         "开 --frames 时小于 2 会被自动抬到 2")
    ap.add_argument("--trigger", choices=["auto", "ack", "frame"], default="auto",
                    help="auto=ACK 为主+帧间隙为备(默认) | ack=只用本周期 PULL 的 ACK | "
                         "frame=只用 ms8 间隙(有 RTT≡0 mod t 的死角, 见模块说明)")
    ap.add_argument("--t-tol", type=float, default=0.75,
                    help="帧判据的容差 ms。批内间隔实测**精确** = t(σ=0), 故可取得很小; "
                         "若固件端出现 1ms 抖动就调大")
    ap.add_argument("--max-wait-ms", type=float, default=600.0,
                    help="兜底: 这么久没有触发也没有新帧就强制补发一条 PULL(防死锁)")
    ap.add_argument("--min-period-ms", type=float, default=30.0,
                    help="硬闸: 两条 PULL 的最小间隔。正常周期≡RTT(远超此值)故不生效, "
                         "但它保证任何异常通路都不能把蓝牙模块灌爆(灌爆后须物理断电)")
    ap.add_argument("--duration", type=float, default=120.0)
    ap.add_argument("--gap-timeout", type=float, default=3.0, help="连续无帧判断连(s)")
    ap.add_argument("--timeout", type=float, default=2.0, help="单条命令超时(s)")
    ap.add_argument("--tag", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--report-every", type=float, default=10.0)
    ap.add_argument("--append-matrix", default=None)
    a = ap.parse_args()

    if a.t_ms >= 128 and a.trigger != "ack":
        print("⚠ t-ms >= 128 且判据含 frame: ms8 只有 8 位, 会与 256ms 折回混淆; 建议 --trigger ack")

    tag = a.tag or time.strftime("%m%d-%H%M%S")
    out_csv = a.out or ("evt_%s.csv" % tag)
    frames_csv = out_csv.replace(".csv", "_frames.csv")
    n = max(1, min(16, a.batch))
    t = max(1, min(5000, a.t_ms))
    use_ack = a.trigger in ("auto", "ack")
    use_frame = a.trigger in ("auto", "frame")

    # ============ 动态 t = floor(RTT/N)+1 (--frames N 开启; 关掉即逐字节回到固定 t 行为) ======
    # ⚠️ 为什么是 floor(RTT/N)+1, 而不是看上去更自然的 round(RTT/N):
    #    单片机每批**固定发 n 帧**, 下一批 PULL 只会在 RTT 之后到达并抢占剩余, 所以
    #    实际每周期帧数 N_eff = floor(RTT/t)+1。要让它**恰好** = 我们要的 N, 必须
    #    同时满足 (N-1)·t < RTT 且 N·t > RTT  ⇒  t = floor(RTT/N)+1。
    #    取 round(RTT/N) 时, 只要 N | RTT 就会退化成 t = RTT/N, 于是 N·t = RTT,
    #    下一帧正好落在周期末尾 ⇒ N_eff 变 N+1、边界间隔塌成 0~1ms。
    #    实例: RTT=300, N=4 ⇒ t=75 是灾难(间隔 75,75,75,75,0); t=76 才对(76,76,76,72)。
    want_N = max(0, min(16, a.frames))
    if want_N:
        n = max(n, want_N)          # 每周期要 N 帧, n 至少得给到 N
    rtt_hist: collections.deque = collections.deque(maxlen=max(3, a.rtt_window))
    # ★ 解缠参照改用**每条命令自己的往返时间**(send → ACK), 不再用"触发→触发"的周期:
    #    周期是**环路健康的产物** —— 环路一旦被判据带偏(误发命令), 周期会腰斩, 参照跟着塌,
    #    于是 ±80ms 一致性闸的锚点失效、错样本反被放行, 越错越信 ⇒ 发散。
    #    2026-10-13 真机实测就是这条路: t 83→57→62→68→38、周期 308→134, 15 期内全崩。
    #    send→ACK 是**逐条命令独立测量**的链路往返, 环路怎么抖它都还是 ~RTT, 天然免疫。
    rtt_send: collections.deque = collections.deque(maxlen=max(3, a.rtt_window))
    ack_idx: list[int] = []           # 每次 ACK 时刻的帧计数(= 该批 F1 将来占的下标)
    host_hist: collections.deque = collections.deque(maxlen=8)   # 触发周期, 仅用于报告
    bad_rtt = 0                       # 一致性闸丢掉的样本数(正常应为 0 或极少)
    off_lattice = 0                   # 解缠结果**不落在 75ms 格点上**的样本数(异常信号)
    early_trig = 0                    # "明显早于 RTT"的触发次数 —— 判据退化的信号
    t_of_batch: dict[int, int] = {}   # 批号 → 该批发射时用的 t(动态 t 下判据必须按批取)
    t_summary: list[int] = []         # 每个周期实际用的 t(审计用)

    mc = MouseClient(auto_session=False, auto_stream=False)
    if not mc.connect(a.port):
        print("✗ 连接失败:", mc.state_detail)
        return 2
    time.sleep(0.3)
    wake = threading.Event()                      # 帧到达 与 ACK 到达 共用
    fl = EvtFrameLog(mc.store, wake)
    ack_times: list[float] = []

    ok, st = send_cmd(mc, "PING", a.timeout)
    print("PING ->", "OK" if ok else "失败(%s)" % st)
    if not ok:
        print("✗ 链路无响应, 退出(若刚跑过高负载实验, 蓝牙模块可能需要**物理断电**)")
        fl.close()
        return 2

    # ---------------------------------------------------------- 起步: 第一条 PULL
    #  用 wait=False + 自己等 ACK, 这样"第一条命令的 ACK"与后续命令走同一条路径,
    #  起步不引入特例(否则 ack 判据在第一个周期无锚可用)。
    def make_ack_cb(rec):
        def cb(_ok, _st):
            tt = time.monotonic()
            rec["ack"] = tt
            ack_times.append(tt)
            # ★★ 批次打标的**唯一正确时机就是这里**。MCU 收到第 k 条命令的那一刻才(重新)
            #    开始第 k 批的发射, 所以"这条 ACK 之后的帧"才属于第 k 批。
            #    若改在**发送时刻**打标(原本的写法), 此刻还在途的上一批尾帧会被贴上**新批号**;
            #    而 t_of_batch 一查就拿到**新 t**(动态 t 下新 t 与旧 t 差 1~5ms) ⇒ 旧批的批内
            #    间隔被拿去和新 t 比 ⇒ |差| > tol ⇒ **误判成批起点 ⇒ 多发命令 ⇒ 周期腰斩** ⇒
            #    → 见上面 rtt_send 的注释, 一路发散。真机 `--frames 4` 已复现。
            if rec.get("batch"):
                fl.cur_batch = rec["batch"]
            # ★ 收 ACK 时新批的**第一帧还没落盘**(线上顺序是 ACK 在前、F1 在下一 tick),
            #   所以此刻的帧计数**正好**就是那帧将来占的下标 ⇒ 先记下标, 稍后再取 ms8。
            #   这样 RTT 的样本来源**完全不经过间隙分类** ⇒ 分类判错也污染不到 RTT 估计。
            rec["ack_idx"] = fl.n()
            ack_idx.append(rec["ack_idx"])
            if rec.get("send"):
                rtt_send.append((tt - rec["send"]) * 1000.0)   # 逐条命令的整趟往返
            wake.set()
        return cb

    all_pendings: list = []
    cur_rec = {"send": time.monotonic(), "ack": None, "trig": None, "kind": "boot", "batch": 1}
    # ------------------------------ 起步: 单帧拉取(用户口径: 第一轮一次只传一帧) ----------
    #  为什么起步要退回单帧拉取(`RATE PULL 1 t`):
    #   ① 单帧拉取 = 先前真机验证过稳定可靠的原模式, 起步阶段与它**逐字节相同**,
    #      不把"多发帧"这个新变量压在第一轮上;
    #   ② 起步时动态 t 还没有 RTT, 只能拿种子 --t-ms 去猜(实测前 3 个周期都在用种子 83),
    #      而 n=1 时**不需要** t 正确 —— 单帧发完就静默, 下一条命令由"帧到达"驱动;
    #   ③ n=1 时相邻帧间隔 = 周期 = RTT, 主机时间戳无歧义 ⇒ 这几轮顺带把 RTT 测准,
    #      切到 N 帧时 t 已经收敛(否则第 4 个周期才第一次用上真实 RTT)。
    boot = max(0, min(16, a.bootstrap))
    if want_N and 0 < boot < 2:
        print("⚠ --bootstrap %d < 2: ms8 的 RTT 样本要两个批起点帧做差, 1 轮算不出来 ⇒ 抬到 2" % boot)
        boot = 2
    fl.cur_batch = 1
    n_boot = 1 if boot > 0 else n
    t_of_batch[1] = (0 if boot > 0 else t)     # 0 = 起步批(不参与帧分类, 只喂 RTT)
    cur_pending = mc.cmd.send("RATE PULL %d %d" % (n_boot, t), wait=False, no_retry=True)
    cur_pending.callbacks.append(make_ack_cb(cur_rec))
    all_pendings.append(cur_pending)
    dl = time.monotonic() + a.timeout
    while not cur_pending.done and time.monotonic() < dl:
        time.sleep(0.005)
    if not cur_pending.done:
        print("✗ 首条 `RATE PULL %d %d` 无应答(固件是否支持 RATE PULL?)" % (n_boot, t))
        fl.close()
        return 3
    if not cur_pending.ok:
        print("✗ 固件拒绝: %s" % cur_pending.status)
        fl.close()
        return 3

    # 等第一批的帧真的开始到(证明上行通)
    warm_until = time.monotonic() + 2.0
    while time.monotonic() < warm_until and fl.n() == 0:
        time.sleep(0.01)
    if fl.n() == 0:
        print("✗ 2s 内无帧: 上行不通(或固件未进 PULL 模式)")
        fl.close()
        return 3

    print("")
    print("触发判据 = %s;  n=%d  t=%d ms  ⇒  每周期 N = min(n, floor(RTT/%d)+1) 帧, 周期 ≡ RTT"
          % (a.trigger, n, t, t))
    print("         速率 ≈ 1/%d + 1/RTT Hz;  命令频率 ≈ 1/RTT Hz  (无目标频率, 自锁)" % t)
    if boot > 0:
        print("         起步 %d 个周期用**单帧拉取**(RATE PULL 1 %d) 测 RTT, 之后转 N 帧"
              % (boot, t))
    print("")
    print("%7s %8s %8s %9s %9s %10s %9s %8s %6s"
          % ("t_s", "周期数", "累计帧", "实收Hz", "命令Hz", "周期ms", "每周期帧", "RTT_ms", "丢帧"))
    print("-" * 82)

    fh = open(out_csv, "w", newline="", encoding="utf-8-sig")
    w = csv.writer(fh)
    w.writerow(["t_s", "periods", "frames", "recv_hz", "cmd_hz", "period_ms",
                "n_per_period", "rtt_ms", "drop_u8", "alive", "note"])
    fh.flush()

    t0 = time.monotonic()
    cursor = fl.n()
    prev_m8 = fl.ms8_of(cursor - 1) if cursor > 0 else None
    prev_batch = fl.batch_of(cursor - 1) if cursor > 0 else None
    last_cursor = cursor
    last_trig_t = None
    last_n_use = n_boot                     # 上一批实际用的 n(起步轮 = 1) → 速度律自检要用
    last_send_t = time.monotonic()          # 起步那条 PULL 已发出, 硬闸从这里算
    last_rx = time.monotonic()
    last_seen = fl.n()
    last_rep = t0
    last_n = fl.n()
    last_drop = fl.drop()
    drop_acc = 0

    periods = 0
    forced = 0
    trig_frame = 0
    trig_ack = 0
    period_arr: list[float] = []
    per_period: list[int] = []
    bound_gaps: list[float] = []
    loss_gaps: list[float] = []
    period_rows: list[list] = []            # 逐周期: idx, trig, period_ms, n_frames, 边界间隔
    bound_mark = 0
    ack_mark = 0                            # ack_idx 的消费游标
    last_ack_m8 = None                      # 上一批 F1 的 ms8(相邻两批做差 = 周期 ≡ RTT)
    rtt_ref = float(t) * max(1, want_N)     # ms8 解缠参照的**起步种子**(拿到 rtt_send 后改为其中位数)
    n_min_warned = False                    # N 下限保护只提示一次
    def scan_frames(allow_trigger: bool) -> bool:
        """把缓冲区里新到的帧逐帧过一遍判据(用 ms8 真值, 不用主机时间戳)。

        返回 True = 在**批起点**处停下且允许触发(调用方随即发命令)。
        `allow_trigger=False` 是"结算"模式: 把触发时缓冲区里**已经发生**的既有事件
        全部记账掉而不再各自触发 —— 否则一次延迟到达的突发里若含 2 个批起点, 就会在
        0.5ms 内连发 2 条命令互相抢占(实测周期被压成 0.5ms, 见 docs §14)。
        """
        nonlocal prev_m8, cursor, last_seen, last_rx, prev_batch
        nn = fl.n()
        if nn > last_seen:
            last_seen, last_rx = nn, time.monotonic()
        while cursor < nn:
            m8 = fl.ms8_of(cursor)
            b_cur = fl.batch_of(cursor)
            if prev_m8 is None:
                prev_m8, prev_batch, cursor = m8, b_cur, cursor + 1
                continue
            # ⚠️ 动态 t 下**必须按"上一帧所属批"取 t**: 批内间隔精确等于**该批发射时**的 t,
            #    而 t 会随 RTT 估计逐周期变 1ms。若一律用当前 t, 旧批的批内间隔(76)会被拿去
            #    和新的 t(75) 比, |76-75| > tol ⇒ 整批被误判成丢帧; 更糟的是边界间隔本来就
            #    只比 t 短 1~3ms, 一旦放宽 tol 去兼容, 边界又会漏检。按批取 t 两个问题一起消失。
            t_cls = t_of_batch.get(prev_batch, t) if prev_batch is not None else t
            if t_cls <= 0:
                # 起步批(n=1): 相邻帧间隔 = 周期 = RTT, **没有"批内间隔"可判** ⇒ 不分类、不触发,
                #  只把 ms8 送进 RTT 估计。n=1 时每一帧都是批起点, 正好是 RTT 的干净样本。
                #  (起步轮的触发只能靠 ACK —— 见循环里 in_boot 时强制走 ack 通路。)
                prev_m8, prev_batch, cursor = m8, b_cur, cursor + 1
                continue
            kind, d = classify_gap(m8, prev_m8, t_cls, a.t_tol)
            prev_m8, prev_batch, cursor = m8, b_cur, cursor + 1
            if kind == "in":
                continue                                 # 批内: 正是 t
            if kind == "loss":
                loss_gaps.append(d)                      # 长间隔 ⇒ 丢帧, 不是批起点
                if len(loss_gaps) <= 3 or len(loss_gaps) % 20 == 0:
                    print("  ⚠ 疑似丢帧: ms8 间隔 %.0f ms > t=%d" % (d, t))
                continue
            bound_gaps.append(d)                         # 短间隔 ⇒ 批起点
            if allow_trigger:
                return True
        return False

    result, t_fail, end_note = "ABORTED", None, ""
    try:
        while True:
            el = time.monotonic() - t0
            if el >= a.duration:
                result, end_note = "SURVIVED", "达到时长上限"
                break

            # 本周期是否在"起步单帧轮"里: 正在飞的那条 PULL 的批号 = periods+1
            #  起步轮 n=1 ⇒ 帧判据没有"批内间隔"可咬(scan_frames 里 t_cls<=0 直接跳过),
            #  触发只能靠 ACK ⇒ 这一段强制打开 ack 通路, 否则 --trigger frame 会在起步轮
            #  干等到 max-wait 才兜底补发(周期被拉成 600ms 而不是 RTT)。
            in_boot = boot > 0 and (periods + 1) <= boot

            # ================= 等触发 =================
            wake.clear()
            deadline = time.monotonic() + a.max_wait_ms / 1000.0
            # ⚠️ 只用一个变量 `trig` 承载状态, 取值 "boot"/"ack"/"frame"/"timeout"/"dead"。
            #    曾经用 `trig` + `trig_kind` 两个变量, 结果 dead 分支写成
            #    `trig="dead", trig_kind=None` ⇒ 判死条件既不打印也不跳出, 反而被当成
            #    "兜底补发" ⇒ 链路一死就以 775 条/秒狂灌蓝牙模块(实测 12493 次)。别再分两个。
            trig, trig_t = None, None
            # frame 判据**无法自举**: 边界只在"下一条命令到达单片机"之后才出现,
            # 而发命令又依赖边界 ⇒ 第一个周期必须人工起步(这里: 立刻发第二条)。
            if a.trigger == "frame" and last_trig_t is None:
                trig, trig_t = "boot", time.monotonic()
            while trig is None:
                # 1) 先扫帧 —— 批起点是主判据(用户口径), 也最省事(不需要 ACK 参与)
                if scan_frames(use_frame and not in_boot):
                    trig, trig_t = "frame", time.monotonic()
                    break
                # 2) 再看本周期 PULL 的 ACK(auto/ack)。批起点不可见时(如 RTT≡0 mod t 的死角,
                #    或边界帧丢失)由它兜住; 两者同源同时, 不会被结算逻辑重复计数。
                #    起步单帧轮(in_boot)强制走这条。
                if (use_ack or in_boot) and cur_pending is not None and cur_pending.done:
                    trig = "ack"
                    trig_t = cur_rec.get("ack") or time.monotonic()
                    break
                now = time.monotonic()
                if now - last_rx > a.gap_timeout:
                    trig, trig_t = "dead", now
                    break
                if now >= deadline:
                    trig, trig_t = "timeout", now
                    break
                wake.wait(min(0.05, max(0.001, deadline - now)))

            if trig == "dead":
                result, t_fail = "BROKE", el
                end_note = "连续 %.1fs 无帧" % (time.monotonic() - last_rx)
                print("\n✗✗ 断连: %s (t=%.1fs)" % (end_note, el))
                break

            # ================= 触发后先"结算"缓冲区(关键的一步) =================
            #  触发信号(ACK)与它对应的那一帧常常**不在同一批**到达(主机侧是 USB 突发),
            #  若不结算, 下一轮扫描会把**同一个**批起点又当成新事件 ⇒ 0.5ms 内连发两条命令、
            #  后者抢占前者 ⇒ 周期塌成 0.5ms。实测就是靠这一步修掉的。
            scan_frames(False)

            # ================= 统计本次触发 =================
            if trig == "timeout":                        # 兜底补发
                forced += 1
                if forced <= 3 or forced % 20 == 0:
                    hint = ("(frame 判据的 RTT≡0(mod t) 死角? 换 --trigger ack)"
                            if a.trigger == "frame" else "")
                    print("  ⚠ 兜底补发(%.0f ms 内无触发) %s" % (a.max_wait_ms, hint))
            else:
                if trig == "ack":
                    trig_ack += 1
                elif trig == "frame":
                    trig_frame += 1
                # 本周期内识别到的"批起点间隔"(边界帧可能在 ACK 之后几 ms 才到, 故归属
                # 上一个触发周期 —— 用游标切片保证不重不漏)
                bg = bound_gaps[bound_mark:]
                bound_mark = len(bound_gaps)
                if last_trig_t is not None:
                    hp = (trig_t - last_trig_t) * 1000.0
                    period_arr.append(trig_t - last_trig_t)
                    per_period.append(cursor - last_cursor)
                    if 5.0 <= hp <= 5000.0:
                        host_hist.append(hp)
                    # 判据退化的信号灯: 触发到触发**明显早于** RTT ⇒ 那不是真边界, 大概率是
                    #  间隙分类被带偏(上一批的批内间隔被当成了批起点)。它是发散的前兆, 值得连报。
                    #  ⚠️ 参照用**当前最优估计**(优先 ms8 的 rtt_hist, 退回 send→ACK), 不能用
                    #    send→ACK 单独当锚: RTT 换档时(实测 300→150)它若没跟手, 就会把**正常**
                    #    触发(150ms)全报成"过早"—— 2026-10-13 那次 146 次报警里绝大多数是这种误报。
                    rtt_est = (statistics.median(rtt_hist) if rtt_hist
                               else (statistics.median(rtt_send) if rtt_send else None))
                    if rtt_est and hp < 0.5 * rtt_est:
                        early_trig += 1
                        if early_trig <= 3 or early_trig % 20 == 0:
                            print("  ⚠ 触发过早(%.0f ms < RTT/2=%.0f): 疑似间隙被误判成批起点"
                                  "(累计 %d 次); 根治办法是 --trigger ack"
                                  % (hp, 0.5 * rtt_est, early_trig))
                period_rows.append([periods + 1, trig,
                                    "%.1f" % ((trig_t - last_trig_t) * 1000.0)
                                    if last_trig_t is not None else "",
                                    cursor - last_cursor if last_trig_t is not None else "",
                                    ";".join("%.0f" % v for v in bg),
                                    (t if want_N else "") if last_n_use > 1 else "",
                                    last_n_use])
                last_trig_t, last_cursor = trig_t, cursor

                # ---------- 动态 t: 用 **ACK 标记的批首帧 ms8** 推 RTT(MCU 时基, 不含 USB 抖动) ----------
                #  样本来源**刻意绕开间隙分类**: "哪一帧是批首"由**哪一次 ACK**定义(见 make_ack_cb
                #  里记下的 ack_idx), 而不是由"短间隔"猜出来。于是分类判错只影响计数与触发,
                #  **污染不到 RTT 估计** —— 这正是把发散环掐断的地方。
                #  同一批内取同一相对位置的帧, 相邻两批相差恰好 = 周期, 自锁工况下周期 ≡ RTT。
                #  实测差 20 倍: 主机时间戳推的周期 σ≈14ms(全是 CH340/USB 攒包与调度抖动),
                #  换成 ms8 后 σ≈0.7ms —— 所以这一步**必须**用 ms8。用主机时间戳会把 t 抖到
                #  75 这种"灾难值"(N|RTT ⇒ 边界间隔塌成 0), 反而比固定 t 更糟。
                if want_N:
                    if rtt_send:
                        # 解缠的**并列参照**(仅用于"多个格点候选取哪个"): 逐条命令的 send→ACK
                        #  往返。它无 256ms 分支歧义、且不随环路健康度变化。真正的定值判据是
                        #  **75ms 物理格点**(见 unwrap_rtt), 参照只当 tie-break。
                        rtt_ref = statistics.median(rtt_send)
                    # ⚠️ 只消费**已经落盘**的 ack_idx。收 ACK 那一刻, 那批的 F1 还没到(线上 ACK 在前)
                    #    ⇒ `fl.ms8_of(ii)` 返回 None。若照样把游标推过去, 这个样本就**永久丢掉**:
                    #    每次触发都发生在 ACK 之后、F1 之前 ⇒ RTT 估计永远空转、t 一直用种子值。
                    #    所以游标只推进到**第一个未落盘**的下标为止, 剩下的留到下一周期再取。
                    avail = fl.n()
                    j = ack_mark
                    while j < len(ack_idx) and ack_idx[j] < avail:
                        mm = fl.ms8_of(ack_idx[j])
                        j += 1
                        if mm is None:
                            continue
                        if last_ack_m8 is not None:
                            raw = (mm - last_ack_m8) & 0xFF
                            # ★ 解缠走**物理格点**: RTT 实测只取 150/225/300/375(=75 的整数倍),
                            #   而 ±256 的错分支偏离格点 31ms ⇒ 无歧义。旧法"离参照最近"在 RTT
                            #   换档时会把 150 解成 406, 再被 ±80 闸按旧的 300 拒掉 ⇒ 估计冻住。
                            rtt_mcu, on_lat = unwrap_rtt(raw, rtt_ref)
                            if not on_lat:
                                off_lattice += 1
                            # 闸只做**合理性**: 落在物理区间 + (在格点上, 或与参照相差不大)。
                            #  不再要求"必须离参照 ±80 内"——那正是拒绝真实换档样本的元凶。
                            #  环路崩塌的防护已由**打标时机**(ACK 才打标)承担, 不靠这道闸。
                            if (60.0 <= rtt_mcu <= 900.0
                                    and (on_lat or abs(rtt_mcu - rtt_ref) <= 80.0)):
                                rtt_hist.append(rtt_mcu)
                            else:
                                bad_rtt += 1
                        last_ack_m8 = mm
                    ack_mark = j
                    if rtt_hist:
                        rtt_ref = statistics.median(rtt_hist)
                        # ⚠️ t 有**硬上限 127 ms**(ms8 只有 8 位, 再大就与 256ms 折回混淆),
                        #    于是 N 有一个**下限**: floor(RTT/N)+1 <= 127  ⇒  N > RTT/127。
                        #    RTT=300 时 N ≥ 3(≈10 Hz) ⇒ 本方案在**帧判据**下做不到 N=1
                        #    (= 纯单帧拉取 ≈3.3 Hz) —— 那个档位压根没有"批内间隔"可判,
                        #    只有 ACK 判据能用。想跑低频请用 --trigger ack 或固定 --t-ms。
                        n_min = int(math.floor(rtt_ref / 127.0)) + 1
                        if want_N < n_min:
                            if not n_min_warned:
                                n_min_warned = True
                                print("  ⚠ --frames %d 对 RTT≈%.0f ms 太小(t 会 >127ms 而折回): "
                                      "自动抬到 %d, 命令参数 n 同步抬到 %d"
                                      % (want_N, rtt_ref, min(16, n_min), max(n, min(16, n_min))))
                            want_N = min(16, n_min)
                            n = max(n, want_N)
                        t = max(1, min(127, int(math.floor(rtt_ref / want_N)) + 1))
                        t_summary.append(t)

            # ================= 立刻发下一条 PULL(这才是"事件触发") =================
            # 硬闸(必须保留): 两次命令不得快于 --min-period-ms。正常周期是 RTT(~150~300ms),
            # 该闸永不生效; 但它保证**任何**异常通路(判据失效/链路假死/时钟跳变)都不能
            # 把蓝牙透传模块灌爆 —— 那个故障是不可自愈的(须物理断电)。
            if last_send_t is not None:
                remain = a.min_period_ms / 1000.0 - (time.monotonic() - last_send_t)
                if remain > 0:
                    time.sleep(remain)
            # ⚠️ 批次编号: 起步批 = 1, 循环里从 2 起。原来写 `periods + 1` 会让**起步那条**
            #    与第 1 次循环发送共用批号 1 ⇒ t_of_batch[1] 被覆盖, 起步批(0)和正常批(t)
            #    混在一起。固定 t 时两者同值所以没暴雷, 有了起步单帧轮就会误判。
            #    ★ 这里**只编号、绝不改 `fl.cur_batch`** —— 给帧打标要等这条命令的 ACK 回来
            #      (见 make_ack_cb)。在发送时刻打标, 在途的上一批尾帧会被贴上**新批号**,
            #      而 t_of_batch 一查就是**新 t** ⇒ 旧批批内间隔被判成批起点 ⇒ 发散。
            nb = periods + 2
            still_boot = boot > 0 and nb <= boot
            t_of_batch[nb] = (0 if still_boot else t)   # 判据按批取 t, 不能省
            n_use = 1 if still_boot else n
            last_n_use = n_use
            cur_rec = {"send": time.monotonic(), "ack": None, "trig": trig_t,
                       "kind": trig, "batch": nb}
            last_send_t = cur_rec["send"]
            cur_pending = mc.cmd.send("RATE PULL %d %d" % (n_use, t), wait=False, no_retry=True)
            cur_pending.callbacks.append(make_ack_cb(cur_rec))
            all_pendings.append(cur_pending)
            periods += 1

            # ================= 逐秒上报 =================
            if time.monotonic() - last_rep >= a.report_every:
                now2 = time.monotonic()
                dt = now2 - last_rep
                n_now = fl.n()
                recv_hz = (n_now - last_n) / dt if dt > 0 else 0.0
                cmd_hz = periods / max(1e-9, now2 - t0)
                nd = fl.drop()
                if nd is not None and last_drop is not None:
                    drop_acc += (nd - last_drop) & 0xFF
                last_drop = nd
                seg = period_arr[-max(1, int(round(cmd_hz * dt))):]
                pmean = statistics.fmean(seg) * 1000.0 if seg else float("nan")
                segn = per_period[-len(seg):] if seg else []
                nmean = statistics.fmean(segn) if segn else float("nan")
                w.writerow(["%.1f" % (now2 - t0), periods, n_now, "%.2f" % recv_hz,
                            "%.3f" % cmd_hz, "%.1f" % pmean, "%.2f" % nmean,
                            "%.1f" % pmean, nd if nd is not None else "", 1, ""])
                fh.flush()
                print("%7.1f %8d %8d %9.2f %9.3f %10.1f %9.2f %8.1f %6s" % (
                    now2 - t0, periods, n_now, recv_hz, cmd_hz, pmean, nmean,
                    pmean, nd if nd is not None else "-"))
                last_rep, last_n = now2, n_now

    except KeyboardInterrupt:
        result, end_note = "ABORTED", "用户中断"
    finally:
        el = time.monotonic() - t0
        total = fl.n()
        alive_s = t_fail if t_fail is not None else el
        recv_hz = total / alive_s if alive_s > 0 else 0.0
        cmd_hz = periods / alive_s if alive_s > 0 else 0.0

        fh.close()
        try:                                          # 退出 PULL 模式, 让单片机停止推帧
            mc.cmd.send("RATE OFF", wait=False, no_retry=True)
        except Exception:
            pass
        for p in all_pendings:
            if not p.done:
                try:
                    mc.cmd.abandon(p)
                except Exception:
                    pass
        fl.close()
        try:
            mc.disconnect()
        except Exception:
            pass

        # ---------------- 逐帧全量转储(收尾一次性写, 避免漏帧)
        times, ms8s, drops, batchnos = fl.dump_batches()
        mcu_gaps: list[float] = []
        hist_src: list[int] = []
        prev_m8 = None
        with open(frames_csv, "w", newline="", encoding="utf-8-sig") as ffr:
            wfr = csv.writer(ffr)
            # gap_host_ms: 主机侧到达差(**含 CH340/USB 攒包抖动, 只能看速率, 不能看抖动**)
            # gap_mcu_ms : 帧内 ms8 差(**判定均匀性的真值**)
            # batch      : 周期号(读线程打标有竞态, 仅参考 —— 分析请按值分组)
            wfr.writerow(["idx", "t_s", "gap_host_ms", "gap_mcu_ms", "ms8",
                          "batch", "drop_u8"])
            for i in range(len(times)):
                hg = (times[i] - times[i - 1]) * 1000.0 if i else None
                mg = ""
                if prev_m8 is not None and i < len(ms8s):
                    # ★ 帧间 ms8 差**恒 < 256**(批内 = t ≤ 127, 批起点 = RTT mod t < t) ⇒
                    #   `& 0xFF` 就是真值, **根本不需要解缠**。旧代码拿主机间隔 hg 当参照去挑
                    #   ±256 分支: 攒包时 hg 会虚高到 ~200-300 ⇒ 把 76 ms 的批内间隔解成 332 ms
                    #   的假"长间隔"(实测 17 个)⇒ "丢帧/间隔极差"结论全错。已改直取 raw。
                    mg = float((ms8s[i] - prev_m8) & 0xFF)
                    mcu_gaps.append(mg)
                    hist_src.append(int(mg))
                wfr.writerow([i + 1, "%.4f" % (times[i] - t0),
                              ("%.2f" % hg) if hg is not None else "",
                              ("%.1f" % mg) if mg != "" else "",
                              ms8s[i] if i < len(ms8s) else "",
                              batchnos[i] if i < len(batchnos) else "",
                              drops[i] if i < len(drops) else ""])
                prev_m8 = ms8s[i] if i < len(ms8s) else prev_m8
        ffr.close()

        # 逐周期表: 用来直接看出"周期是否双峰"以及双峰与触发判据的对应关系
        periods_csv = out_csv.replace(".csv", "_periods.csv")
        with open(periods_csv, "w", newline="", encoding="utf-8-sig") as fpr:
            wpr = csv.writer(fpr)
            wpr.writerow(["period_idx", "trig", "period_ms", "n_frames",
                          "boundary_gaps_ms", "t_ms", "n_used"])
            wpr.writerows(period_rows)

        # ---------------- ACK 提前量: 每个 ACK 之后到达的第 1 帧与它的时间差
        #  稳态下 ACK 与 F1 之间的所有尾巴帧都在 ACK 之前到齐(RTT mod t > 0 时),
        #  所以"ACK 之后的第 1 帧"就是本批 F1 ⇒ 差值即 ACK 相对 F1 的提前量。
        ack_lead: list[float] = []
        for at in ack_times:
            j = bisect.bisect_right(times, at)
            if j < len(times):
                lead = (times[j] - at) * 1000.0
                if 0.0 <= lead < 300.0:
                    ack_lead.append(lead)

        pms = [v * 1000.0 for v in period_arr]
        hist_mcu = collections.Counter(hist_src).most_common(10)
        hist_N = collections.Counter(per_period).most_common(10)
        hist_bd = collections.Counter(int(round(v)) for v in bound_gaps).most_common(10)
        # 模型自检: 动态 t 下每个周期的 t 不同, 必须**按周期取**; 起步单帧轮(n_used=1)
        # 没有"批内间隔", 模型就是 1 帧 —— 不排除它会把起步轮全报成"不符"。
        # period_rows 比 period_arr 多一行(第 1 行是起步触发, 那时 last_trig_t 还是 None),
        # 所以整体后移一位对齐。
        # 速度律自检: N_eff = min(n, floor(周期/t)+1)。
        # ⚠️ 周期必须代**中位数**, 不能逐周期代主机周期 —— 主机周期 σ≈30 ms(CH340 攒包),
        #    `floor(305/76)+1 = 5` 而 `floor(300/76)+1 = 4`, 一跨过 4/5 边界就虚报一次
        #    "不符"(修复前的跑法因此报出 167/394, 把好结果说成坏结果)。
        _pm = [p for p in pms if p]
        p_ref = statistics.median(_pm) if _pm else float(t * n)
        pred = []
        for i, p in enumerate(pms):
            row = period_rows[i + 1] if i + 1 < len(period_rows) else None
            n_used = row[6] if (row and len(row) > 6) else n
            if n_used is not None and int(n_used) <= 1:
                pred.append(1)
                continue
            pred_t = row[5] if (row and len(row) > 5 and row[5] != "") else t
            pred.append(min(int(n_used), int(p_ref / pred_t) + 1))
        n_pred = min(len(pred), len(per_period))
        mism = sum(1 for i in range(n_pred) if pred[i] != per_period[i])

        def st_(arr):
            if not arr:
                return (float("nan"),) * 4
            return (statistics.fmean(arr),
                    statistics.pstdev(arr) if len(arr) > 1 else 0.0,
                    pctl(arr, 0.95), max(arr))

        p_mean, p_sd, p_p95, p_max = st_(pms)
        n_mean = statistics.fmean(per_period) if per_period else float("nan")

        print("")
        print("=" * 82)
        print("结果: %s  存活 %.1fs %s" % (result, alive_s,
                                          ("(断于 t=%.1fs)" % t_fail) if t_fail else ""))
        print("      周期 %d  实收帧 %d" % (periods, total))
        if want_N:
            ts = t_summary or [t]
            tstr = ("%d" % ts[0]) if len(set(ts)) == 1 else "%d~%d" % (min(ts), max(ts))
            print("      **PC 实收 %.2f Hz**   命令频率 %.3f Hz(=1/周期)" % (recv_hz, cmd_hz))
            print("      参数 n=%d  --frames %d(动态 t)   起步单帧 %d 轮   实际 t %s ms"
                  % (n, want_N, boot, tstr))
            anchor = statistics.median(rtt_send) if rtt_send else float("nan")
            rh = statistics.median(rtt_hist) if rtt_hist else float("nan")
            print("      RTT(send→ACK 逐条命令) 中位数 %.1f ms;  ms8 周期中位数 %.1f ms(样本 %d)"
                  % (anchor, rh, len(rtt_hist)))
            if rtt_hist:
                # RTT 阶梯: 按 75ms 格点分桶, 直接看有没有换档(实测只应出现 150/225/300/375)
                lad = collections.Counter(int(RTT_QUANT * round(v / RTT_QUANT)) for v in rtt_hist)
                print("      RTT 阶梯(75ms 格点分桶): %s"
                      % sorted(lad.items(), key=lambda kv: kv[0]))
            print("      RTT 样本被闸丢弃 %d 个;  解缠落在格点外 %d 个;  触发过早(判据退化前兆) %d 次"
                  % (bad_rtt, off_lattice, early_trig))
        else:
            print("      **PC 实收 %.2f Hz**   命令频率 %.3f Hz(=1/周期)   参数 n=%d t=%d ms  起步单帧 %d 轮"
                  % (recv_hz, cmd_hz, n, t, boot))
        print("      自锁校验: 周期均值 %.1f ms  σ %.1f  p95 %.1f  最大 %.1f  (周期 ≡ RTT)"
              % (p_mean, p_sd, p_p95, p_max))
        print("      速度律: 每周期实收帧 均值 %.2f  直方图 %s" % (n_mean, hist_N))
        print("              模型 min(n,floor(周期中位数/t)+1) 与实际不符 %d/%d 个周期"
              % (mism, n_pred))
        print("      ★帧间隔(ms8, 真值): 均值 %.1f σ %.1f  直方图 %s"
              % ((statistics.fmean(mcu_gaps) if mcu_gaps else float("nan")),
                 (statistics.pstdev(mcu_gaps) if len(mcu_gaps) > 1 else float("nan")),
                 hist_mcu))
        print("      批起点间隔(%s): n=%d 均值 %.1f 直方图 %s"
              % ("动态 t ⇒ ≈ t−(1..N−1)" if want_N else "= RTT mod t",
                 len(bound_gaps),
                 statistics.fmean(bound_gaps) if bound_gaps else float("nan"), hist_bd))
        if want_N and (mcu_gaps or bound_gaps):
            _ag = mcu_gaps + bound_gaps
            print("      ★均匀性(动态 t 的全部意义): 间隔极差 %.0f ms  =  max %.0f − min %.0f"
                  % (max(_ag) - min(_ag), max(_ag), min(_ag)))
        print("      丢帧长间隔: n=%d %s"
              % (len(loss_gaps),
                 ("直方图 " + str(collections.Counter(
                     int(round(v)) for v in loss_gaps).most_common(6))) if loss_gaps else ""))
        print("      触发统计: ack %d  frame %d  兜底补发 %d" % (trig_ack, trig_frame, forced))
        if ack_lead:
            al = st_(ack_lead)
            print("      ★ACK 提前量(F1 到达 − ACK 到达): 均值 %.1f ms σ %.1f  p95 %.1f  最大 %.1f"
                  % al)
        print("      MCU 丢帧字节: 末值 %s (增量累计 %s)" % (fl.drop(), drop_acc))
        print("=" * 82)

        with open(out_csv.replace(".csv", "_summary.txt"), "w", encoding="utf-8") as f:
            f.write("标签: %s\n" % tag)
            f.write("方案: 事件触发式拉取(周期≡RTT, 自锁)  触发判据: %s\n" % a.trigger)
            f.write("参数: n=%d  t=%d ms\n" % (n, t))
            f.write("结果: %s\n存活时长: %.1fs\n断连时刻: %s\n"
                    % (result, alive_s, ("%.1fs" % t_fail) if t_fail else "-"))
            f.write("周期数: %d\n实收帧: %d\nPC 实收: %.2f Hz\n命令频率: %.3f Hz\n"
                    % (periods, total, recv_hz, cmd_hz))
            f.write("周期(≡RTT) 均值: %.2f ms\n周期σ: %.2f ms\n周期p95: %.2f ms\n周期最大: %.2f ms\n"
                    % (p_mean, p_sd, p_p95, p_max))
            f.write("每周期帧数 均值: %.2f\n每周期帧数 直方图: %s\n" % (n_mean, hist_N))
            f.write("模型 floor(周期中位数/t)+1 不符: %d/%d\n" % (mism, n_pred))
            f.write("帧间隔(ms8) 均值: %.2f ms\n帧间隔(ms8) σ: %.2f ms\n帧间隔(ms8) 直方图: %s\n"
                    % ((statistics.fmean(mcu_gaps) if mcu_gaps else float("nan")),
                       (statistics.pstdev(mcu_gaps) if len(mcu_gaps) > 1 else float("nan")),
                       hist_mcu))
            f.write("批起点间隔(=RTT mod t) 样本: %d\n批起点间隔 均值: %.2f ms\n批起点间隔 直方图: %s\n"
                    % (len(bound_gaps),
                       statistics.fmean(bound_gaps) if bound_gaps else float("nan"), hist_bd))
            f.write("丢帧长间隔 样本: %d\n" % len(loss_gaps))
            f.write("触发: ack %d / frame %d\n兜底补发: %d\n" % (trig_ack, trig_frame, forced))
            if ack_lead:
                f.write("ACK 提前量 均值: %.2f ms\nACK 提前量 σ: %.2f ms\n"
                        % (st_(ack_lead)[0], st_(ack_lead)[1]))
            f.write("MCU 丢帧字节末值: %s\nMCU 丢帧增量: %s\n" % (fl.drop(), drop_acc))
        print("逐帧数据:", frames_csv)
        print("逐周期数据:", periods_csv)
        print("逐秒数据:", out_csv)
        print("汇总:", out_csv.replace(".csv", "_summary.txt"))

        if a.append_matrix:
            new = not os.path.exists(a.append_matrix)
            with open(a.append_matrix, "a", newline="", encoding="utf-8-sig") as f:
                mm = csv.writer(f)
                if new:
                    mm.writerow(["tag", "scheme", "trigger", "n", "t_ms", "result",
                                 "alive_s", "periods", "recv_hz", "cmd_hz",
                                 "period_mean_ms", "period_sd_ms", "n_per_period", "forced"])
                mm.writerow([tag, "evt", a.trigger, n, t, result, "%.1f" % alive_s,
                             periods, "%.2f" % recv_hz, "%.3f" % cmd_hz,
                             "%.1f" % p_mean, "%.1f" % p_sd, "%.2f" % n_mean, forced])
            print("总表已追加:", a.append_matrix)
    return 0 if result == "SURVIVED" else 1


if __name__ == "__main__":
    sys.exit(main())
