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

两种模式: 动态 t(方案本体, 默认) / 固定 t(探针)
------------------------------------------
**动态 t 模式(`--frames N`, 默认)**: 唯一的旋钮是**每周期帧数 N**, 每周期重算
    **t = floor(RTT / N) + 1**
由 (N−1)·t < RTT 且 N·t > RTT 推出 ⇒ 每周期**恰好** N 帧, 间隔极差 = N − (RTT mod N) ∈ [1, N] ms,
速率 ≈ N/RTT。`n` 只需"够大"(≥ N; 自动取 max(N+1, 8)): `n·t ≥ RTT` 之后抢占成为常态, 被丢弃的尾巴帧
**从未发出**(不占链路、不算丢帧), 速率自己跑满; 反过来 `n·t < RTT` 会让单片机发完一批后
**空转**等命令 ⇒ 白丢时间。**⇒ 在动态 t 模式下 t 是导出量, 不该由用户指定。**

**固定 t 模式(`--frames 0`, 仅作探针)**: 旋钮是 t 本身。它的均匀性完全由 `RTT mod t` 决定,
而二者关系毫无规律 —— 实测 RTT=300 时:
    t=83 ⇒ 间隔 83,83,83,51  极差 **32 ms**
    t=76 ⇒ 间隔 76,76,76,72  极差 **4 ms**
    t=75 ⇒ 间隔 75,75,75,75,0 极差 **75 ms**  (75 | 300, 边界间隔直接塌成 0!)
相邻 1 ms 的 t 可以差 20 倍, 而 RTT 本身还会变 ⇒ **固定 t 不是"不好", 而是无法预知好不好** ——
这正是方案本体要用动态 t 的原因。

注意动态 t 是 floor+1 而**不是** round: 取 round 时只要 N | RTT 就会退化成 t = RTT/N, 于是
N·t = RTT、下一帧正落在周期末尾 ⇒ N_eff 变 N+1 且边界间隔塌成 0(见 test_evt_trigger_logic.py)。

RTT 从哪来(这一步必须用 ms8, 不能用主机时间戳):
  * 批起点帧的 ms8 做差 = 周期 = RTT, 且是 **MCU 时基**;
  * 实测同一轮里 ms8 周期 σ ≈ 0.7 ms, 而主机时间戳周期 σ ≈ 14 ms(全是 CH340/USB 攒包与
    调度抖动) —— 差 20 倍。拿主机时间戳去推 t 会抖到 75 那种灾难值, 比固定 t 更糟;
  * 取**滑窗中位数**(--rtt-window, 默认 8)抗离群: 原始周期里有 2/132 个 225/457 ms 的野值。

N 的取值范围: **1..16, 没有下限**(2026-10-13 起)
------------------------------------------------
  * 旧版曾有 `N > RTT/127`(RTT=300 时 N ≥ 3)的下限, 理由是**帧间隙判据**要求
    `t ≤ 127 ms` —— ms8 只有 8 位, `t > 127` 时"丢一帧"(间隔 2t)与 256 折回混淆、
    会被判成"批起点" ⇒ **提前触发** ⇒ 多发命令 ⇒ 顶爆链路。
  * 默认判据改读固件打的 `FLAG_PULLF1` 标记帧后(v2.6.1 / fw 219), 批起点是**读事实**、
    与 `t` 无关 ⇒ 这个下限随之**取消**: `--frames 1`(纯单帧拉取 ≈1/RTT, 与先前长期
    验证稳定的老模式**逐字节相同**)与 `--frames 2` 都直接可用, 不必再借 `--batch` 绕。
  * ⚠️ **仅 `--trigger frame` 仍受 `t ≤ 127` 限制** —— 那条通路主循环里单独告警。

两条关于**帧判据**(`--trigger frame` / 固定 `--t-ms`)的硬约束(离线自测里断言着):
  1. t ≤ 127 ms —— 见上: 只有帧间隙判据需要它(标记帧判据不需要)。
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
(`in_boot` 时无视 `--trigger frame`), 否则起步轮只能干等到 ACK。

触发判据 (--trigger) —— 默认 auto = 标记帧为主 + ACK 兜底
----------------------------------------------------------
三个可用的边界信号:

* ★ **标记帧(mark, v2.6.1/fw 219 起)**: 固件在收到 `RATE PULL` 后发出的**第一帧**把
  统一帧 flags 的 bit3(`FLAG_PULLF1`)置 1(见 link.h/link.c)。于是"哪一帧是批起点"
  从**推算**变成**读事实** —— 判据不再需要 t、不需要 ms8 差值、不受 RTT 换档影响,
  也不会被"归档错/±256 折回/1ms 刀锋"带偏。这就是**用户口径的原始形态**:
  "PC 收到本周期第 1 帧就立刻发下一条 PULL"。
  ⚠️ 旧固件(fw<=218)没有这一位 ⇒ 本工具自动退回下面的 ACK 通路(`mark_supported` 标志),
  行为与 v2.6 完全一致 —— 换固件不需要换命令。
* **ACK(auto/ack)**: `RATE PULL` 会回 `ACK <seq> OK`。固件里顺序是
  `link_PullSet()`(只置 `linkPullTimer=0`, 帧要等**下一个 1ms tick** 才进 TX 环)
  **先**、`link_Ack()`(字节立刻进环)**后** ⇒ **ACK 比本批第 1 帧早约 5~6 ms 到达**
  (ACK 14B vs 帧 59B @115200)。无歧义、与 t 无关、"批内丢帧"也不会误触发; 代价是依赖 ACK 不丢。
  ⚠️ 正因为 ACK 总是**早到**, 若让它无条件抢先, 触发就永远不是"第 1 帧到达"这件事本身
  ⇒ 于是它被降级为**兜底**: 只在标记帧迟迟不来时才用(见 `--mark-grace-ms`)。
* **帧间隙(frame)**: 批内间隔恒 = t(σ=0), 所以"与前一帧间隔 ≠ t"的那一帧就是批起点。
  用 **ms8**(帧内单片机毫秒时基, 真值), 不用主机时间戳(被 CH340/USB 攒包污染 σ≈75 ms)。
  ⚠️ **这是"推算", 有四类误判来路**(t 归档错、ms8 ±256 折回、RTT 换档、比 t 只短 1ms 的
  刀锋)与一个对偶的死角(`RTT ≡ 0 mod t` 时批起点间隔恰好 = t, 判据永远看不到边界 ⇒ 死锁)。
  误判成"批起点"就是**提前触发** ⇒ 多发命令 ⇒ 速率顶过链路容量(见下面"绝不超时补发")。
  ⇒ 保留它只为**对照/探针**(`--trigger frame`), 不再参与默认判据。

⇒ 默认 `--trigger auto` = **标记帧为主 + ACK 兜底**(`--trigger mark` 则只认标记)。
  另外两条兜底通道仍然保留:
    * **ACK 兜底**: 收到本周期 ACK 后若 `--mark-grace-ms`(默认 20ms) 内没有等到标记帧
      (标记帧丢了 / 固件是旧的), 就用 ACK 触发 —— **宁可晚 20ms, 也绝不空转**。
      旧固件在第一个 ACK 时就被判为 `mark_supported=False`, 因此**不**吃这 20ms, 行为与 v2.6 相同。
    * **诊断**: `--max-wait-ms` 仍只计数不补发(见下)。

★ **绝不"超时补发"** —— 本方案是**事件驱动自锁**: 下一条 PULL 只能由"本周期信号到达"触发,
绝不允许因为"等太久了"就补发。补发 = 绕开自锁凭空注入命令 ⇒ 抢占在途批、批起点间隔塌陷、
实际速率顶过链路容量 ⇒ **透传模块缓冲灌满即停转发, 且不自愈(须物理断电)**。
因此 `--max-wait-ms` **只做诊断**: 超时只计数 + 打印(节流), **不发任何命令**, 继续等。
若触发通路真的失效, 单片机会在本批发完后自然静默, 由 `--gap-timeout`(默认 3 s 无帧)判 BROKE ——
宁可如实报断, 也不靠补发去掩盖。反过来: 若 RTT 本来就 > 600 ms, 旧代码会在触发到达前抢先补发,
把**完全正常的长周期**打断, 这是纯粹的破坏。

`--min-period-ms`(默认 30)是**硬闸**: 两条命令不得快于它。正常周期 ≡ RTT(150~300 ms),
故它永不生效; 但它是最后一道保险 —— 踩过的坑: 早期把判死状态写成 `trig="dead"` 而
`trig_kind=None`, 于是"连续 3 s 无帧"既不打印也不跳出, 反被当成补偿通路 ⇒ 链路一死就以
**775 条/秒**狂灌蓝牙模块(实测一次 12493 条)。灌爆的透传模块不自愈、必须物理断电, 所以
现在只用**一个** `trig` 变量承载状态, 并在发送前无条件过一遍最小间隔硬闸。

产物
----
* `evt_<tag>_frames.csv`  逐帧: idx, t_s, gap_host_ms, gap_mcu_ms, ms8, batch, drop_u8, flags
      (`batch` 列 = 周期号, 现在**在收到该批 ACK 时才打标**(读线程), 位置正确;
       但混合码流下仍建议用 `analyze_pace_gaps.py <csv> --target <t>` 的**按值分组**模式。
       `flags` 列的低 3 位里 `0x08` = **批首帧标记**(v2.6.1 固件), 直接读它就能分组,
       不必再按间隔值反演)
* `evt_<tag>_periods.csv` 逐周期: period_idx, trig, period_ms, n_frames, boundary_gaps_ms,
                                t_ms, n_used
      (`trig` ∈ **mark**(读标记帧) / gap(间隙推算, 仅对照) / ack(兜底) / boot(人工起步);
       `t_ms` 列只在本周期用了动态 t(且非起步单帧轮)时才有值; `n_used` = 该周期实际拉取的帧数,
       起步轮恒为 1 —— 速度律自检按它算, 否则会把起步轮全报成"不符")
      (用来直观看"周期是否双峰"以及双峰与触发判据 mark/ack 的对应关系)
* `evt_<tag>.csv`         逐秒: t_s, periods, frames, recv_hz, cmd_hz, period_ms,
                                n_per_period, rtt_ms, drop_u8, alive, note
* `evt_<tag>_summary.txt` 汇总: 速率/周期≡RTT 分布/N 分布/ms8 直方图/触发与无触发超时统计/
                                标记帧统计/丢帧字节

用法 —— **旋钮只有一个: 每周期帧数 N**
----------------------------------
    python link_pull_evt.py COM7 --frames 4 --duration 120 --tag evt_N4     # 就这么简单
    python link_pull_evt.py COM7 --frames 6 --duration 120 --tag evt_N6     # ≈20 Hz 档
    python link_pull_evt.py COM7 --frames 4 --bootstrap 0 --duration 120 --tag evt_N4raw  # 关起步(对照)

    # 固定 t 探针模式(实验用, 不是方案本体): 必须 --frames 0 + 显式 --t-ms
    python link_pull_evt.py COM7 --frames 0 --t-ms 83 --duration 120 --tag evt_fixt83

`--frames N` 默认 **4**, 是**唯一需要给的参数**: `t` 每周期由 RTT 自动推出 (`floor(RTT/N)+1`),
`n` 自动取 max(N+1, 8) —— **`--t-ms` / `--batch` 默认都是 0 = 自动, 正常用法不要传**。
`--t-ms` 只在两个场合显式给: ① 配合 `--frames 0` 走固定 t 探针模式; ② 高级: 指定动态 t 的起步种子。
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
from host.protocol.codec import FLAG_PULLF1  # noqa: E402


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


# ---- CLI 参数推导: **唯一旋钮是 N(每周期帧数)**, n 与 t 全部由它推出 ------------------
#  纯函数, 便于离线自测(test_evt_trigger_logic.test_cli_derive_params)。
N_MAX = 16              # 命令里 n 的上限(固件 LINK_PULL_N_MAX)
N_AUTO_MIN = 8          # 自动 n 的经验下限: 与历史**验证过**的 --batch 8 一致 ⇒ 默认零行为变化
T_MS_MAX_FW = 5000      # 固件 LINK_PULL_T_MS_MAX: 批内帧间隔上限(ms)。**不是** 127 ——
                        #   127 只是"帧间隙判据"的软上限(见 docstring), 与固件无关
T_SEED_INTERNAL = 76    # 动态 t 的内部起步种子: 仅首个 RTT 样本到达前顶一次(≈300/4)
T_FIXED_DEFAULT = 83    # 固定 t 探针模式的兜底值(旧默认, 保持历史可比)
ABSORB_MIN_S = 0.070    # 迟到标记吸收窗下限(覆盖 F1 相对 ACK 的正常迟到, 实测 5~6 ms)
ABSORB_MAX_S = 0.120    # 吸收窗**上限**: 必须 < 最小观测 RTT(150 ms), 否则会把**下一批**
                        #   的正常标记也吃掉 ⇒ 永久退回 ACK 通路(窗是限时的, 不是布尔标志)


def derive_params(frames: int, batch: int, t_ms: int):
    """由**唯一旋钮 N** 推出 (want_N, n, t)。

    * 动态 t 模式(`frames > 0`, 默认): `n` 自动 = max(N+1, 8); `t` 由 RTT 每周期推出
      (起步用内部种子 T_SEED_INTERNAL)。
    * 固定 t 探针模式(`frames == 0`): `n` 自动 = 8; `t` 必须显式给(缺省 T_FIXED_DEFAULT)。
    * `batch` / `t_ms` 传 0 表示"自动"; 非 0 = 用户显式覆盖(高级用法)。

    ⚠️ 为什么 t 不该由用户给: 方案里速率 = N/RTT, `t = floor(RTT/N)+1` 是**导出量**;
       固定 t 的均匀性 = `RTT mod t`, 与 t 的关系毫无规律(见模块 docstring)。
    ⚠️ n 为什么取下限 8: `n` 只需"够大"(>= floor(RTT/t)+1, 设大无代价) —— 取 N+1 是方案
       要求的**下界**, 但历史验证过的运行都用 8, 故自动值取 max(N+1, 8) 以保持零行为变化。
    """
    want_N = max(0, min(N_MAX, frames))
    if want_N:
        n = max(1, min(N_MAX, batch if batch > 0 else max(want_N + 1, N_AUTO_MIN)))
        t = max(1, min(5000, t_ms)) if t_ms > 0 else T_SEED_INTERNAL
    else:
        n = max(1, min(N_MAX, batch if batch > 0 else N_AUTO_MIN))
        t = max(1, min(5000, t_ms)) if t_ms > 0 else T_FIXED_DEFAULT
    return want_N, n, t


def main() -> int:
    ap = argparse.ArgumentParser(
        description="事件触发式拉取(收到本周期第 1 帧就立刻发下一条 PULL; 周期≡RTT, 自锁)")
    ap.add_argument("port")
    ap.add_argument("--batch", type=int, default=0,
                    help="命令里的 n(授权上限, 1..16)。**默认 0 = 自动**: 动态 t 模式取 "
                         "max(N+1, 8), 固定 t 模式取 8。**一般无需指定** —— 方案里 n 只需"
                         "「够大」(>= floor(RTT/t)+1; 设大无代价), 由每周期帧数 N 推出")
    ap.add_argument("--t-ms", type=int, default=0,
                    help="批内帧间隔 ms。**默认 0 = 自动**: 每周期由 RTT 推出 "
                         "t = floor(RTT/N)+1(首个 RTT 样本到达前用内部种子顶一次)。"
                         "**正常用法不要传它** —— t 是导出量、不是旋钮。给非 0 值只有两个用途: "
                         "① 固定 t 探针模式(须配 --frames 0); ② 高级: 指定动态 t 的起步种子"
                         "(⚠️ 只有 --trigger frame 才怕 t >=128: ms8 的 256 折回会混淆间隙判据)")
    ap.add_argument("--frames", type=int, default=4,
                    help="【唯一的旋钮】每周期目标帧数 N(默认 4, 范围 1..16, **无下限**)。"
                         "t 与 n 都由它自动推出 ⇒ **只需给 N**。N=0 = 关闭动态 t, 退回固定 t "
                         "探针模式(需显式 --t-ms)。N=1 = 纯单帧拉取(≈1/RTT, 老模式); N=2 也直接"
                         "可用 —— 旧的 N>RTT/127 下限已随标记帧判据取消。"
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
    ap.add_argument("--trigger", choices=["auto", "mark", "ack", "frame"], default="auto",
                    help="auto=**标记帧为主**(固件在 PULL 首帧打的 FLAG_PULLF1)+ACK 兜底"
                         "(默认, 旧固件自动退回 ACK) | mark=只认标记帧(最严, 新固件) | "
                         "ack=只用本周期 PULL 的 ACK | frame=只用 ms8 间隙推算(旧判据, 有 "
                         "RTT≡0 mod t 的死角, 仅作对照/探针)")
    ap.add_argument("--mark-grace-ms", type=float, default=20.0,
                    help="标记帧为主时的 **ACK 兜底延迟**: 收到本周期 ACK 后等这么久仍没有"
                         "标记帧, 就用 ACK 触发(防止标记帧被链路丢掉后空转)。"
                         "仅对**已确认支持标记**的固件生效 —— 旧固件(fw<=218)不认这 20ms")
    ap.add_argument("--t-tol", type=float, default=0.75,
                    help="帧判据的容差 ms。批内间隔实测**精确** = t(σ=0), 故可取得很小; "
                         "若固件端出现 1ms 抖动就调大")
    ap.add_argument("--max-wait-ms", type=float, default=600.0,
                    help="**诊断**阈值(不是兜底): 这么久没有触发就计一次「无触发超时」并打印。"
                         "⚠️ 只报告, **绝不补发命令** —— 补发会绕开自锁、抢占在途批、"
                         "把实际速率顶过链路容量, 灌爆透传模块(不可自愈)")
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

    # ⚠️ t ≥ 128 只对**帧间隙判据**有害(ms8 的 256 折回会把"丢一帧"读成"批起点")。
    #    标记帧/ACK 判据与 t 无关 ⇒ 新固件下(默认 auto 走标记)根本不怕它; 只在 gap 通路
    #    真会启用时提示 —— ack/mark 档不再误报。
    if a.t_ms >= 128 and a.trigger in ("auto", "frame"):
        print("⚠ t-ms >= 128 且判据含 frame 间隙: ms8 只有 8 位, 会与 256ms 折回混淆; "
              "建议 --trigger auto/ack(标记帧判据与 t 无关)")

    tag = a.tag or time.strftime("%m%d-%H%M%S")
    out_csv = a.out or ("evt_%s.csv" % tag)
    frames_csv = out_csv.replace(".csv", "_frames.csv")
    # 三个边界信号各司其职: 标记帧 = 主判据(v2.6.1 固件), ACK = 兜底, 间隙推算 = 仅对照/探针。
    # ⚠️ `auto` **不再**让间隙推算直接触发 —— 那正是"提前触发"的唯一来路(见模块 docstring)。
    #    间隙分类仍然照跑, 只是降级为**诊断**(bound_gaps / loss_gaps / 两条判据的交叉计数)。
    use_mark = a.trigger in ("auto", "mark")
    use_ack = a.trigger in ("auto", "ack")
    use_frame = a.trigger == "frame"

    # ============ 唯一的旋钮是 N(每周期帧数); n 与 t **全部由它推出** ============
    #  方案里: 速率 = N / RTT。N 一改, 帧间隔 t 自动跟上(RTT 每周期实测), 无需人工调 t。
    #  ⇒ CLI 上 n / t 默认都是 0 = 自动; 非 0 才算"高级覆盖"(见 argparse help)。
    #  ⚠️ 动态 t 为什么是 floor(RTT/N)+1, 而不是看上去更自然的 round(RTT/N):
    #     单片机每批**固定发 n 帧**, 下一批 PULL 只会在 RTT 之后到达并抢占剩余, 所以
    #     实际每周期帧数 N_eff = floor(RTT/t)+1。要让它**恰好** = 我们要的 N, 必须
    #     同时满足 (N-1)·t < RTT 且 N·t > RTT  ⇒  t = floor(RTT/N)+1。
    #     取 round 时只要 N | RTT 就退化成 t = RTT/N ⇒ N_eff = N+1、边界间隔塌成 0。
    want_N, n, t = derive_params(a.frames, a.batch, a.t_ms)
    if not want_N:
        print("⚠ --frames 0 = 固定 t 探针模式(非方案本体): t 已固定为 %d ms。"
              "方案用法是 --frames N(N 是唯一旋钮)" % t)
    rtt_hist: collections.deque = collections.deque(maxlen=max(3, a.rtt_window))
    # ★ 解缠参照改用**每条命令自己的往返时间**(send → ACK), 不再用"触发→触发"的周期:
    #    周期是**环路健康的产物** —— 环路一旦被判据带偏(误发命令), 周期会腰斩, 参照跟着塌,
    #    于是 ±80ms 一致性闸的锚点失效、错样本反被放行, 越错越信 ⇒ 发散。
    #    2026-10-13 真机实测就是这条路: t 83→57→62→68→38、周期 308→134, 15 期内全崩。
    #    send→ACK 是**逐条命令独立测量**的链路往返, 环路怎么抖它都还是 ~RTT, 天然免疫。
    rtt_send: collections.deque = collections.deque(maxlen=max(3, a.rtt_window))
    ack_idx: list[int] = []           # 每次 ACK 时刻的帧计数(= 该批 F1 将来占的下标)
    # ---- v2.6.1 标记帧(FLAG_PULLF1)相关状态 ----
    mark_idx: list[int] = []          # 标记帧的下标(固件亲口说"这是批首帧") —— RTT 样本来源
    mark_gaps: list[float] = []       # 相邻标记帧的 ms8 差 = **批起点间隔 = RTT mod t**(读事实)
    mark_cur = 0                      # mark_idx 的消费游标
    last_mark_m8 = None               # 上一个标记帧的 ms8(扫描侧, 用于批起点间隔统计)
    last_mark_sample_m8 = None        # 上一个**已采入 RTT** 的标记帧 ms8(消费侧, 与上者分开:
                                      #  扫描侧每帧都跟, 消费侧只在触发时批量补, 两者不能共用)
    frames_since_mark = 0             # 自上一个标记帧以来过了几帧(判"上一条标记帧是否丢了")
    mark_supported = False            # 见过至少一个标记帧 ⇒ 板上固件确实在打标记(实证)
    mark_capable = False              # PONG 的 fw >= 219 ⇒ 板上固件**声称**支持(见下方 PING)
    # ★ 刚用"非标记"通路(ACK/gap/boot)触发过 ⇒ 本批迟到的 F1 标记**只记账、不再触发**,
    #   否则会在 5ms 内多塞一条命令、周期瞬间塌掉。**必须是限时窗**(不是布尔): 若那一帧
    #   标记真的丢了, 布尔标志会一直挂着, 把**下一批**的正常标记也吃掉 ⇒ 永久退回 ACK 通路。
    #   窗宽 ~70ms 远小于任何 RTT(≥150ms) ⇒ 只会吃掉"迟到的自己", 不会吃掉下一批。
    absorb_until = None
    stale_marks = 0                   # 被上述规则吃掉(不触发)的迟到标记帧数
    marks_seen = 0                    # 标记帧计数
    gap_false_bound = 0               # ★间隙判据说"批起点"、标记说"不是" ⇒ 旧判据的**提前触发**隐患
    mark_gap_miss = 0                 # 标记说"批起点"、间隙判据说"不是" ⇒ 旧判据的**盲区**(含死角)
    mark_skip_rtt = 0                 # 因"上一条标记帧丢了"(间隔帧数翻倍)而跳过的 RTT 样本
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
    if not ok:
        print("✗ 链路无响应(PING 失败 %s), 退出(若刚跑过高负载实验, 蓝牙模块可能需要**物理断电**)" % st)
        fl.close()
        return 2

    # ---- 标记帧能力: 先用 **PONG 的 fw 版本**判(板上自述), 再看有没有真的收到 ----
    #  为什么不能只等"收到第一个标记帧"再打开标记通路: ACK 比 F1 早到 ~5ms, 若第一个周期
    #  先用 ACK 触发, 那一批迟到的 F1 标记随后又会被当成新批首 ⇒ 会在 5ms 内多塞一条命令
    #  (周期瞬间塌成 5ms)。所以这里提前问固件版本, fw >= 219 就从第一周期起以标记为主,
    #  并用 `skip_next_mark` 兜住任何"ACK 先触发、标记后到"的残余情形。
    fw = mc.device.get("fw")
    mark_capable = isinstance(fw, int) and fw >= 219
    print("固件 fw=%s   标记帧判据 %s"
          % (fw if fw is not None else "?", "可用" if mark_capable else "不可用(退回 ACK 判据)"))
    if mark_capable and not use_mark:
        print("  (--trigger %s: 不使用标记帧)" % a.trigger)

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
    if want_N:
        print("目标: 每周期 N=%d 帧  ⇒  速率 ≈ N/RTT, 周期 ≡ RTT (自锁)   [n=%d, t 由 RTT 自动推出]"
              % (want_N, n))
        if boot > 0:
            print("      起步 %d 轮单帧拉取测 RTT;  触发判据 %s" % (boot, a.trigger))
    else:
        print("模式: 固定 t 探针(非方案本体)   n=%d   t=%d ms   触发判据 %s" % (n, t, a.trigger))
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
    no_trig = 0                              # 无触发超时次数(**只诊断, 不补偿发送**)
    trig_mark = 0                            # 触发来源: 标记帧(读事实, v2.6.1 主判据)
    trig_gap = 0                             # 触发来源: ms8 间隙推算(旧判据, 仅对照)
    trig_ack = 0                             # 触发来源: ACK(兜底)
    period_arr: list[float] = []
    per_period: list[int] = []
    bound_gaps: list[float] = []
    loss_gaps: list[float] = []
    period_rows: list[list] = []            # 逐周期: idx, trig, period_ms, n_frames, 边界间隔
    bound_mark = 0
    ack_mark = 0                            # ack_idx 的消费游标
    last_ack_m8 = None                      # 上一批 F1 的 ms8(相邻两批做差 = 周期 ≡ RTT)
    rtt_ref = float(t) * max(1, want_N)     # ms8 解缠参照的**起步种子**(拿到 rtt_send 后改为其中位数)
    frame_t_warned = False                  # frame 档 t>127 的告警只提示一次
    def scan_frames(allow_trigger: bool, mark_ok: bool = True,
                    gap_ok: bool = True):
        """把缓冲区里新到的帧逐帧过一遍: **记账 + 判批起点**。

        返回触发的**来源字符串**:
          * "mark" —— 固件在 PULL 首帧打的 `FLAG_PULLF1`(v2.6.1): **读事实**, 不用推算;
          * "gap"  —— ms8 间隙推算出来的批起点(旧判据, 只在 `--trigger frame` 时用);
          * None   —— 本批还没到批起点。
        `allow_trigger=False` 是"结算"模式: 把触发时缓冲区里**已经发生**的既有事件全部
        记账掉而不再触发 —— 否则一次延迟到达的突发里若含 2 个批起点, 就会在 0.5ms 内连发
        2 条命令互相抢占(实测周期被压成 0.5ms, 见 docs §14)。

        ★ 两条判据**互相独立**: 标记帧即使被间隙判据判成"in"(RTT ≡ 0 mod t 的死角就是
          这种情形)也照样触发 —— 这正是它要解决的问题。两者在这帧上是否一致会分别计数
          (`gap_false_bound` = 旧判据会**提前触发**; `mark_gap_miss` = 旧判据的**盲区**),
          于是"旧判据错在哪"有了可量化的证据。
        """
        nonlocal prev_m8, cursor, last_seen, last_rx, prev_batch
        nonlocal last_mark_m8, frames_since_mark, marks_seen, mark_supported
        nonlocal gap_false_bound, mark_gap_miss, absorb_until, stale_marks
        nn = fl.n()
        if nn > last_seen:
            last_seen, last_rx = nn, time.monotonic()
        cause = None
        while cursor < nn:
            m8 = fl.ms8_of(cursor)
            b_cur = fl.batch_of(cursor)
            fg = fl.flags_of(cursor)
            is_mark = bool(fg is not None and (fg & FLAG_PULLF1))
            absorbed = False
            if is_mark:
                # ---- 标记帧: 记账(RTT 样本 / 批起点间隔 / 一致性交叉计数) ----
                marks_seen += 1
                mark_supported = True
                if last_mark_m8 is not None:
                    # 相邻两个标记帧的 ms8 差 = **批起点间隔 = RTT mod t** —— 直接读事实,
                    # 不再需要"按值分组"去猜(旧诊断的反演)。
                    mark_gaps.append(float((m8 - last_mark_m8) & 0xFF))
                last_mark_m8 = m8
                mark_idx.append(cursor)
                if absorb_until is not None:
                    # ★ 刚才用"非标记"通路触发过 ⇒ 这一帧就是**那一批迟到的 F1**:
                    #   只记账(RTT 样本 / 批起点间隔照收)、不触发。
                    absorbed = time.monotonic() <= absorb_until
                    absorb_until = None
                    if absorbed:
                        stale_marks += 1
                frames_since_mark = 0
            else:
                frames_since_mark += 1

            # ---- 间隙分类: 始终跑, 但只作**诊断**; 只有 --trigger frame 时它能触发 ----
            gap_kind = None
            t_cls = t_of_batch.get(prev_batch, t) if prev_batch is not None else t
            if prev_m8 is not None and t_cls > 0:
                # ⚠️ 动态 t 下**必须按"上一帧所属批"取 t**: 批内间隔精确等于**该批发射时**的 t,
                #    而 t 会随 RTT 估计逐周期变 1ms。若一律用当前 t, 旧批的批内间隔(76)会被拿去
                #    和新的 t(75) 比, |76-75| > tol ⇒ 整批被误判成丢帧; 更糟的是边界间隔本来就
                #    只比 t 短 1~3ms, 一旦放宽 tol 去兼容, 边界又会漏检。按批取 t 两个问题一起消失。
                #  (起步批 n=1 时 t_cls == 0: 相邻帧间隔 = 周期 = RTT, 没有"批内间隔"可判 ⇒ 跳过;
                #   此时标记帧仍然有效 —— 起步轮每一帧都是批首, 而它照旧在打标记。)
                gap_kind, d = classify_gap(m8, prev_m8, t_cls, a.t_tol)
                if gap_kind == "loss":
                    # 长间隔 ⇒ 丢帧, 不是批起点。**只记账、不报警**(2026-10-13): 链路传输时间
                    # 变长时批内间隔自然 > t, 该条件不再是"异常", 报警会持续误报。
                    loss_gaps.append(d)
                elif gap_kind == "bound":
                    bound_gaps.append(d)                     # 短间隔 ⇒ 旧判据认定是批起点
                # ---- 两条判据的交叉计数: 这就是"旧判据错在哪"的可量化证据 ----
                if is_mark and gap_kind != "bound":
                    mark_gap_miss += 1        # 标记说批首、旧判据说不是 ⇒ 旧判据的**盲区**
                elif (not is_mark) and gap_kind == "bound":
                    gap_false_bound += 1      # ★旧判据说批首、标记说不是 ⇒ 旧判据**提前触发**
            prev_m8, prev_batch, cursor = m8, b_cur, cursor + 1

            # ---- 触发来源: 只认这一遍里的**第一处**(调用方随即发命令, 其余由结算吃掉) ----
            if cause is None and allow_trigger:
                if mark_ok and is_mark and not absorbed:
                    cause = "mark"
                elif gap_ok and gap_kind == "bound":
                    cause = "gap"
        return cause

    result, t_fail, end_note = "ABORTED", None, ""
    try:
        while True:
            el = time.monotonic() - t0
            if el >= a.duration:
                result, end_note = "SURVIVED", "达到时长上限"
                break

            # 本周期是否在"起步单帧轮"里: 正在飞的那条 PULL 的批号 = periods+1
            #  起步轮 n=1 ⇒ 间隙判据没有"批内间隔"可咬(scan_frames 里 t_cls<=0 直接跳过),
            #  那时只有 ACK 与标记帧可用。标记帧在起步轮**照样有效**(每帧都是批首)。
            in_boot = boot > 0 and (periods + 1) <= boot

            # ---- 触发通道可用性(每周期重算) ----
            #  ★ 自我修正: 板上**声称**支持标记(fw>=219)却连跑几个周期一个标记都没见到,
            #    就撤回这个假设 —— 否则每周期白等 --mark-grace-ms, 速率白掉一截。
            if mark_capable and not mark_supported and periods >= 5:
                mark_capable = False
                print("  ⚠ 固件自述 fw>=219 但 %d 个周期都没收到标记帧 ⇒ 退回 ACK+间隙判据"
                      % periods)
            mark_ok = use_mark and (mark_capable or mark_supported)
            # 间隙推算: --trigger frame 常开; auto 只在**标记帧不可用**(旧固件/自称支持却没打)
            # 时作为最后兜底 —— 与 v2.6 的 auto(ACK 为主 + 间隙为备)逐字对齐。
            gap_ok = (use_frame or (a.trigger == "auto" and not mark_ok)) and not in_boot
            # ACK 兜底: auto/ack 常开; --trigger frame 在起步单帧轮里也必须靠它(帧判据那时失效)
            ack_ok = use_ack or (a.trigger == "frame" and in_boot)
            # 标记帧可用时让 ACK **晚一点**生效(等标记先到): 稳态下标记总在 ACK 之后 ~5ms 到,
            # 于是 ACK 实际永不触发 ⇒ 触发点 = 真正的"第 1 帧到达"(用户口径)。
            # 标记帧丢了才会由 ACK 兜住 —— 宁晚 20ms, 也不空转。
            # 旧固件(不信标记)不吃这个延迟 ⇒ 与 v2.6 行为逐字相同。
            ack_grace = (a.mark_grace_ms / 1000.0) if (mark_ok and not in_boot) else 0.0

            # ================= 等触发 =================
            wake.clear()
            deadline = time.monotonic() + a.max_wait_ms / 1000.0   # **仅诊断**(绝不触发发送)
            # ⚠️ 只用一个变量 `trig` 承载状态, 取值 "boot"/"mark"/"gap"/"ack"/"stoptime"/"dead"。
            #    曾经用 `trig` + `trig_kind` 两个变量, 结果 dead 分支写成
            #    `trig="dead", trig_kind=None` ⇒ 判死条件既不打印也不跳出, 反而被当成
            #    补偿路径 ⇒ 链路一死就以 775 条/秒狂灌蓝牙模块(实测 12493 次)。别再分两个。
            trig, trig_t = None, None
            # 间隙判据**无法自举**: 边界只在"下一条命令到达单片机"之后才出现,
            # 而发命令又依赖边界 ⇒ 那个档位必须人工起步(这里: 立刻发第二条)。
            # ★ 标记帧判据**不需要**这一脚 —— 首帧自带标记, 它自己就会来(这才是它的价值)。
            if a.trigger == "frame" and last_trig_t is None:
                trig, trig_t = "boot", time.monotonic()
            while trig is None:
                # 1) 先扫帧 —— 批起点是主判据(用户口径), 也最省事(不需要 ACK 参与)
                cause = scan_frames(mark_ok or gap_ok, mark_ok=mark_ok, gap_ok=gap_ok)
                if cause is not None:
                    trig, trig_t = cause, time.monotonic()
                    break
                now = time.monotonic()
                # 2) ACK 兜底(auto/ack; 以及 frame 档的起步轮)。等过 ack_grace 才生效。
                if ack_ok and cur_pending is not None and cur_pending.done:
                    a_t = cur_rec.get("ack") or now
                    if now - a_t >= ack_grace:
                        trig, trig_t = "ack", a_t
                        break
                if now - last_rx > a.gap_timeout:
                    trig, trig_t = "dead", now
                    break
                if now >= deadline:
                    # ★ **只诊断, 绝不补发** —— 见模块 docstring「绝不超时补发」:
                    #   补发 = 凭空注入命令、抢占在途批、把实际速率顶过链路容量 ⇒ 模块缓冲灌满即死。
                    no_trig += 1
                    if no_trig <= 3 or no_trig % 20 == 0:
                        hint = ""
                        if a.trigger == "frame":
                            hint = "(frame 档 RTT≡0(mod t) 死角? 可换 --trigger ack)"
                        elif mark_ok:
                            hint = "(标记帧与 ACK 都没到? 多半是链路已断)"
                        print("  ⚠ 无触发超时(%.0f ms 内 ACK 与批起点都没到) —— **不补发**, 继续等 %s"
                              % (a.max_wait_ms, hint))
                    deadline = now + a.max_wait_ms / 1000.0     # 重新计时(仍只诊断)
                    if time.monotonic() - t0 >= a.duration:
                        # 内层 while 会挡住外层的 duration 检查 ⇒ 到点必须在这里收工
                        trig, trig_t = "stoptime", now
                        break
                wake.wait(min(0.05, max(0.001, deadline - now)))

            if trig == "stoptime":
                result, end_note = "STALLED", "达时长上限时仍未出现过触发"
                print("\n⚠ 结束: 全程无触发(ACK 与批起点都没到), **未补发**; 本周期收到 %d 帧"
                      % (cursor - last_cursor))
                break

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
            #  (没有"超时补发"分支: `trig` 只可能是 mark/gap/ack —— 见 docstring「绝不超时补发」)
            if trig in ("ack", "gap", "mark"):
                if trig == "mark":
                    trig_mark += 1
                elif trig == "ack":
                    trig_ack += 1
                else:
                    trig_gap += 1
                if trig != "mark" and use_mark:
                    # ★ 这一批**自己的**标记帧还没到(否则就是标记触发了) ⇒ 开一个**限时**吸收窗:
                    #   它随后到达时只记账、不再触发。窗取 clamp(3×grace, 70ms, 120ms) ——
                    #   **上限必须 < 最小观测 RTT(150ms)**, 所以只会吃掉"迟到的自己", 不会吃
                    #   下一批的正常标记(限时而非布尔, 否则丢一帧就永久退回 ACK 通路)。
                    absorb_until = time.monotonic() + min(
                        ABSORB_MAX_S, max(ABSORB_MIN_S, 3.0 * ack_grace))
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
                                  "(累计 %d 次); v2.6.1 的标记帧判据不会发生这种情况 —— "
                                  "若仍出现请查标记帧是否被链路丢"
                                  % (hp, 0.5 * rtt_est, early_trig))
                period_rows.append([periods + 1, trig,
                                    "%.1f" % ((trig_t - last_trig_t) * 1000.0)
                                    if last_trig_t is not None else "",
                                    cursor - last_cursor if last_trig_t is not None else "",
                                    ";".join("%.0f" % v for v in bg),
                                    (t if want_N else "") if last_n_use > 1 else "",
                                    last_n_use])
                last_trig_t, last_cursor = trig_t, cursor

                # ---------- 动态 t: 用**批首帧的 ms8** 推 RTT(MCU 时基, 不含 USB 抖动) ----------
                #  样本来源**刻意绕开间隙分类**: "哪一帧是批首"由**固件打的标记**(v2.6.1)或
                #  **哪一次 ACK**(旧固件)定义, 而不是由"短间隔"猜出来 ⇒ 分类判错只影响计数
                #  与触发, **污染不到 RTT 估计** —— 这正是把发散环掐断的地方。
                #  相邻两批的批首帧相差恰好 = 周期, 自锁工况下周期 ≡ RTT。
                #  实测差 20 倍: 主机时间戳推的周期 σ≈14ms(全是 CH340/USB 攒包与调度抖动),
                #  换成 ms8 后 σ≈0.7ms —— 所以这一步**必须**用 ms8。用主机时间戳会把 t 抖到
                #  75 这种"灾难值"(N|RTT ⇒ 边界间隔塌成 0), 反而比固定 t 更糟。
                if want_N:
                    if rtt_send:
                        # 解缠的**并列参照**(仅用于"多个格点候选取哪个"): 逐条命令的 send→ACK
                        #  往返。它无 256ms 分支歧义、且不随环路健康度变化。真正的定值判据是
                        #  **75ms 物理格点**(见 unwrap_rtt), 参照只当 tie-break。
                        rtt_ref = statistics.median(rtt_send)

                    def feed_rtt(mm, prev):
                        """两个批首帧的 ms8 差 → 解缠 → 过闸 → rtt_hist。返回 (新 prev, 是否入库)。"""
                        nonlocal off_lattice, bad_rtt
                        if prev is None:
                            return mm, False
                        raw = (mm - prev) & 0xFF
                        # ★ 解缠走**物理格点**: RTT 实测只取 150/225/300/375(=75 的整数倍),
                        #   而 ±256 的错分支偏离格点 31ms ⇒ 无歧义。旧法"离参照最近"在 RTT
                        #   换档时会把 150 解成 406, 再被 ±80 闸按旧的 300 拒掉 ⇒ 估计冻住。
                        val, on_lat = unwrap_rtt(raw, rtt_ref)
                        if not on_lat:
                            off_lattice += 1
                        # 闸只做**合理性**: 落在物理区间 + (在格点上, 或与参照相差不大)。
                        #  不再要求"必须离参照 ±80 内"——那正是拒绝真实换档样本的元凶。
                        if 60.0 <= val <= 900.0 and (on_lat or abs(val - rtt_ref) <= 80.0):
                            rtt_hist.append(val)
                            return mm, True
                        bad_rtt += 1
                        return mm, False

                    avail = fl.n()
                    if mark_supported:
                        # ★ 标记帧通路(v2.6.1): 下标在**扫描时**就记下了 ⇒ 帧必定已落盘,
                        #   没有下面 ACK 通路那种"收 ACK 时 F1 还没到"的坑。
                        while mark_cur < len(mark_idx) and mark_idx[mark_cur] < avail:
                            mm = fl.ms8_of(mark_idx[mark_cur])
                            gap_n = (mark_idx[mark_cur] - mark_idx[mark_cur - 1]
                                     if mark_cur > 0 else None)
                            mark_cur += 1
                            if mm is None:
                                continue
                            # ⚠️ 上一条标记帧若被链路丢了, 这里的差就是 **2 个周期**(= 2·RTT),
                            #   而 2·RTT 常常也落在 75ms 格点上(300→600) ⇒ 会被闸放行 ⇒ t 翻倍
                            #   ⇒ 速率腰斩。判据: 两个标记帧之间的**帧数**应当 ≈ 每周期帧数,
                            #   翻倍就说明中间少了一个标记 ⇒ 丢这个样本(但仍重新对齐 prev)。
                            if (gap_n is not None and last_mark_sample_m8 is not None
                                    and gap_n > 1.8 * max(1, last_n_use)):
                                mark_skip_rtt += 1
                                last_mark_sample_m8 = mm
                                continue
                            last_mark_sample_m8, _ok = feed_rtt(mm, last_mark_sample_m8)
                    else:
                        # ---- ACK 通路(旧固件 fw<=218; 与 v2.6 逐字相同) ----
                        # ⚠️ 只消费**已经落盘**的 ack_idx。收 ACK 那一刻, 那批的 F1 还没到
                        #    (线上 ACK 在前) ⇒ `fl.ms8_of(ii)` 返回 None。若照样把游标推过去,
                        #    这个样本就**永久丢掉**: 每次触发都发生在 ACK 之后、F1 之前
                        #    ⇒ RTT 估计永远空转、t 一直用种子值。所以游标只推进到**第一个
                        #    未落盘**的下标为止, 剩下的留到下一周期再取。
                        j = ack_mark
                        while j < len(ack_idx) and ack_idx[j] < avail:
                            mm = fl.ms8_of(ack_idx[j])
                            j += 1
                            if mm is None:
                                continue
                            last_ack_m8, _ok2 = feed_rtt(mm, last_ack_m8)
                        ack_mark = j
                    if rtt_hist:
                        rtt_ref = statistics.median(rtt_hist)
                        # ★ N **没有下限**(2026-10-13 起): 旧的 `N > RTT/127` 只是 ms8 间隙
                        #   判据的需要(帧内 ms8 只有 8 位, t > 127 ms 时"丢一帧"的间隔 2t 与
                        #   256 折回混淆 ⇒ 被判成批起点 ⇒ 提前触发)。默认判据改读固件打的
                        #   `FLAG_PULLF1` 标记帧后, 批起点是**读事实**、与 t 无关 ⇒ 下限取消。
                        #   N=1 时 t = floor(RTT/1)+1 = RTT+1 > RTT ⇒ 第 2 帧永远等不到发射就被
                        #   下一条 PULL 抢占 ⇒ N_eff = 1, 即**先前长期稳定的单帧拉取模式**。
                        #   ⚠️ 只有 `--trigger frame` 还怕 t > 127 —— 那条通路在此单独告警。
                        t = max(1, min(T_MS_MAX_FW, int(math.floor(rtt_ref / want_N)) + 1))
                        if use_frame and t > 127 and not frame_t_warned:
                            frame_t_warned = True
                            print("  ⚠ --trigger frame 且 t=%d > 127 ms: ms8 间隙判据会与 "
                                  "256 ms 折回混淆, 批起点可能误判(丢帧被读成批起点); "
                                  "建议改用 --trigger auto/ack" % t)
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
        flagss = fl.dump_flags()
        mcu_gaps: list[float] = []
        hist_src: list[int] = []
        prev_m8 = None
        with open(frames_csv, "w", newline="", encoding="utf-8-sig") as ffr:
            wfr = csv.writer(ffr)
            # gap_host_ms: 主机侧到达差(**含 CH340/USB 攒包抖动, 只能看速率, 不能看抖动**)
            # gap_mcu_ms : 帧内 ms8 差(**判定均匀性的真值**)
            # batch      : 周期号(读线程打标有竞态, 仅参考 —— 分析请按值分组)
            # flags      : 帧内 flags 字节; `flags & 0x08`(FLAG_PULLF1)= **批首帧**(v2.6.1 固件打的)
            wfr.writerow(["idx", "t_s", "gap_host_ms", "gap_mcu_ms", "ms8",
                          "batch", "drop_u8", "flags"])
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
                              drops[i] if i < len(drops) else "",
                              ("%d" % flagss[i]) if i < len(flagss) else ""])
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
        print("结果: %s   存活 %.1fs %s" % (result, alive_s,
                                          ("(断于 t=%.1fs)" % t_fail) if t_fail else ""))
        print("  周期 %d   实收帧 %d   **PC 实收 %.2f Hz**   命令频率 %.3f Hz(=1/周期)"
              % (periods, total, recv_hz, cmd_hz))
        if want_N:
            ts = t_summary or [t]
            tstr = ("%d" % ts[0]) if len(set(ts)) == 1 else "%d~%d" % (min(ts), max(ts))
            print("  旋钮 N=%d (每周期帧数)   n=%d   起步单帧 %d 轮   实际 t=%s ms"
                  % (want_N, n, boot, tstr))
        print("  周期(≡RTT) 均值 %.1f ms   σ %.1f   p95 %.1f   最大 %.1f"
              % (p_mean, p_sd, p_p95, p_max))
        print("  每周期实收帧 均值 %.2f   直方图 %s" % (n_mean, hist_N))
        if want_N and (mcu_gaps or bound_gaps):
            _ag = mcu_gaps + bound_gaps
            print("  帧间隔极差(均匀性) %.0f ms   [max %.0f − min %.0f]"
                  % (max(_ag) - min(_ag), max(_ag), min(_ag)))
        print("  触发: 标记帧 %d / 间隙 %d / ACK %d   超时 %d(未补发)   标记帧判据 fw>=219=%s"
              % (trig_mark, trig_gap, trig_ack, no_trig,
                 "可用" if mark_capable else "不可用"))
        if use_mark and not mark_supported:
            print("  ⚠ 一次标记帧都没收到: 固件没打标记(旧固件?) ⇒ 判据已退回 ACK/间隙")
        # 上面只打"最关键"的一屏; RTT 阶梯/ms8 直方图/批起点间隔/模型不符/ACK 提前量等
        # 诊断明细仍在 *_summary.txt 里, 不再刷屏。
        print("  MCU 丢帧字节 末值 %s   增量累计 %s" % (fl.drop(), drop_acc))
        print("=" * 82)

        with open(out_csv.replace(".csv", "_summary.txt"), "w", encoding="utf-8") as f:
            f.write("标签: %s\n" % tag)
            f.write("方案: 事件触发式拉取(周期≡RTT, 自锁)  触发判据: %s\n" % a.trigger)
            if want_N:
                f.write("模式: 动态 t(唯一旋钮 N=%d, n 自动=%d)\n" % (want_N, n))
            else:
                f.write("模式: 固定 t 探针(非方案本体)  n=%d  t=%d ms\n" % (n, t))
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
            f.write("触发: 标记帧 %d / 间隙推算 %d / ACK %d\n无触发超时: %d (未补发)\n"
                    % (trig_mark, trig_gap, trig_ack, no_trig))
            f.write("标记帧: 收到 %d 帧; 固件自述 fw>=219=%s; 旧判据漏判 %d 次; "
                    "旧判据误判批首 %d 次; 迟到吸收 %d; RTT 样本跳过 %d\n"
                    % (marks_seen, mark_capable, mark_gap_miss, gap_false_bound,
                       stale_marks, mark_skip_rtt))
            f.write("批起点间隔(读标记帧) 样本: %d\n批起点间隔(读标记帧) 均值: %.2f ms\n"
                    % (len(mark_gaps),
                       statistics.fmean(mark_gaps) if mark_gaps else float("nan")))
            if ack_lead:
                f.write("ACK 提前量 均值: %.2f ms\nACK 提前量 σ: %.2f ms\n"
                        % (st_(ack_lead)[0], st_(ack_lead)[1]))
            f.write("MCU 丢帧字节末值: %s\nMCU 丢帧增量: %s\n" % (fl.drop(), drop_acc))
        print("数据: %s (+_periods.csv/_frames.csv/_summary.txt)"
              % out_csv)

        if a.append_matrix:
            new = not os.path.exists(a.append_matrix)
            with open(a.append_matrix, "a", newline="", encoding="utf-8-sig") as f:
                mm = csv.writer(f)
                if new:
                    mm.writerow(["tag", "scheme", "trigger", "n", "t_ms", "result",
                                 "alive_s", "periods", "recv_hz", "cmd_hz",
                                 "period_mean_ms", "period_sd_ms", "n_per_period", "no_trig"])
                mm.writerow([tag, "evt", a.trigger, n, t, result, "%.1f" % alive_s,
                             periods, "%.2f" % recv_hz, "%.3f" % cmd_hz,
                             "%.1f" % p_mean, "%.1f" % p_sd, "%.2f" % n_mean, no_trig])
            print("总表已追加:", a.append_matrix)
    return 0 if result == "SURVIVED" else 1


if __name__ == "__main__":
    sys.exit(main())
