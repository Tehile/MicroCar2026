# -*- coding: utf-8 -*-
"""PC 主导的**间隔闭环匀速拉取**(取代 `--period-ms` 定值做法)。

设计意图(用户口径)
------------------
周期不是拍脑袋的定值, 而是**PC 按目标频率算出来的时间表**, 目标是:
  1. 让数据**尽可能均匀**地到达 PC(而不是"2 帧一顿 + 一大段静默");
  2. **不在电脑鼠上做任何计算** —— 单片机只按 `PULL <n> <t_ms>` 做倒计时,
     "该发几帧、帧间留多久"全部由 PC 决定。

怎么做到"不用知道基础频率也能命中目标频率"
------------------------------------------
**目标频率是自变量, 不是测量值。** 全部由两个输入算出:
    t_cmd_ms = round(1000 / fps)       ← 写进固件的帧内间隔
    due_k    = t0 + k * (n / fps)      ← 第 k 批**首帧应到达**的时刻(纯算术)
    due     += n / fps                 ← 每批固定推进, **永不锁到实测**
持续速率 = n/(n*t) = 1000/t = fps, 与链路状况无关。
实测只用来算"提前量"(`rtt_est`, EWMA): 它只**平移**发送时刻, 不改速率。

为什么"提前量"是**整趟 RTT**, 不是半趟
--------------------------------------
固件收到命令**立即发第 1 帧** ⇒
    首帧到达 = send + RTT_下行 + RTT_上行 = send + RTT(整趟)
想让首帧落在 `due` ⇒ `send = due - RTT`。
早期版本误写 `due - RTT/2`: 每批都晚 RTT/2 到达, 实测被压到 10.4 Hz。

为什么会"不均" & 旧工具为什么只有 5.5 Hz
----------------------------------------
固件 `PULL n t` 的语义是"立即发第 1 帧, 之后每 t ms 一帧, 共 n 帧, 发完静默";
且**收到新 PULL 会丢弃本批剩余帧**(抢占式)。若 PC **等本批收齐再发下一条**
(早期 `RATE PULL` 工具的老做法, 该脚本已随 v2.6.1 移除), 则每批多出一个
"命令往返 + 轮询"的窟窿:

    收帧:  F F  ................  F F  ................
           |83ms|   ~280ms 静默     |83ms|
    实测:  真实上限 12.05 Hz(1000/t)  ->  实收 5.52 Hz   ← 2.18 倍差, 全在这个窟窿

正确做法(本工具): **提前发下一条拉取请求**, 让命令在途时间被"本批还在发帧"盖住,
相邻帧间隔恒 = t, 与"每批几帧"无关(这就是均匀的来源)。
   * 防打断: `send` 不得早于"上一批末帧发出 + 余量"(固件收到新 PULL 会**丢弃**
     本批剩余帧), 否则白丢帧; 由 `--safety-ms` 保证。
     实测把发送时刻往**早**推(`--phase-gain > 0`)会让交付率掉到 ~91% ⇒ 默认 0。

产物
----
* `pace_<tag>_frames.csv`   逐帧: idx, t_s, gap_host_ms, gap_mcu_ms, ms8, batch, drop_u8
* `pace_<tag>.csv`          逐秒: recv_hz(PC 实收) / cmd_hz / frame_hz(1000/t, **真实速率上限**)
                                  / mcu_gap_ms / rtt_ms(EWMA) / drop_u8 / alive
      ⚠️ 列 `cmd_hz` 存的是 `n*1000/t`, **比真值大 n 倍, 口径错误**(见 docs §10.2),
         判速率一律看 `recv_hz ≈ --fps`, 忽略该列。
* `pace_<tag>_summary.txt`  汇总: 实收率、交付率、间隔抖动(均值/σ/p95/超 1.5t 次数)、MCU 丢帧
* `--append-matrix FILE`    跨环境总表追加一行

用法
----
    python link_pull_pace.py COM7 --fps 12 --batch 2 --duration 120 --tag pace12n2
    python link_pull_pace.py COM7 --fps 12 --batch 4 --duration 120 --tag pace12n4
    python link_pull_pace.py COM7 --fps 12 --batch 2 --aimd --duration 300   # 自动找上限
只发 PING / RATE PULL / RATE OFF, **不含任何运动命令**。
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import os
import statistics
import sys
import threading
import time

sys.path.insert(0, ".")

import host.registry as registry
from host.registry import TYPE_UNIFIED

# ------------------------------------------------- 让 decoder 顺便吐出 drop / F
_orig = registry.FRAMES[TYPE_UNIFIED]
_orig_decoder = _orig.decoder


def _decoder_with_extras(payload: bytes) -> dict:
    """在 registry 解码器之上补一个 `mcuDrop`(固件 TX 环累计丢帧低 8 位, **旧布局** offset 44)。

    ⚠️ v2.6 起**不再**补 `fpsBroadcast`: 统一帧尾部的 2 B 目标帧率是实验期字段,
    已随 AIMD 一起删除 —— 那两字节不存在。
    ⚠️ **v9(30 B 载荷)起帧内没有丢帧计数了**(`dropCnt` 被删, 见 docs/通信协议v9-统一帧重排方案.md §6)。
    于是本函数在 v9 板上**不会**产出 `mcuDrop`, 逐帧 drop 记为 -1(= 不可用), `fl.drop()` 回 None。
    这是**预期行为**, 不是链路异常: 健康度改看主机侧 `seq_lost` / `bad_crc`(事件流)与
    `gap` 统计; 若日后固件把 `linkDrop` 挂到 `PING`/`GET ALL` 上, 再从这里补一条低频读取。
    """
    d = _orig_decoder(payload)
    if len(payload) >= 45:                  # 仅 v8(49 B)布局有 offset 44
        d["mcuDrop"] = payload[44]          # u8: 固件 TX 环满累计丢帧低 8 位
    return d


registry.FRAMES[TYPE_UNIFIED] = dataclasses.replace(_orig, decoder=_decoder_with_extras)

from host.api import MouseClient  # noqa: E402


class FrameLog:
    """挂在 store.on_frame 上记录每帧到达时刻/丢帧字节(线程安全)。

    ⚠️ 老脚本曾用 `ndrop, _, _ = snapshot()` 把"帧数"当成了丢帧数
    (见 pull_pull-fix83.csv 的 mcu_drop 列 = 帧数); 这里一律用访问器 `n()` /
    `drop()` 取, 避免同类错误。
    """

    def __init__(self, store):
        self._lock = threading.Lock()
        self.times: list[float] = []
        self.ms8s: list[int] = []             # 帧内单片机毫秒时基低 8 位(判定均匀性的真值)
        self.drops: list[int] = []            # 帧内 MCU 丢帧字节(逐帧)
        self.flags: list[int] = []            # 帧内 flags 字节(逐帧; bit3 = FLAG_PULLF1 批首帧)
        self.batches: list[int] = []          # 每帧归属的"第几次 PULL"(主循环发送前写入)
        self.cur_batch = 0                    # 主循环维护的当前批号(钩子在读线程里读它)
        self._drop = None                     # 最近一帧的 MCU 丢帧字节
        self.drop_seen = 0                    # 非零出现次数(区分"恒 0"与"偶尔跳")
        self._prev = getattr(store, "on_frame", None)

        def hook(stream, rec):
            if stream == "unified":
                with self._lock:
                    self.times.append(float(rec.get("_rx_mono") or time.monotonic()))
                    self.ms8s.append(int(rec.get("ms8", 0)) & 0xFF)
                    self.drops.append(int(rec.get("mcuDrop", 0)) if "mcuDrop" in rec else -1)
                    self.flags.append(int(rec.get("flags", 0)) & 0xFF)
                    self.batches.append(self.cur_batch)
                    if "mcuDrop" in rec:
                        self._drop = rec["mcuDrop"]
                        if rec["mcuDrop"]:
                            self.drop_seen += 1
            if self._prev:
                self._prev(stream, rec)

        store.on_frame = hook
        self._store = store

    def n(self) -> int:
        with self._lock:
            return len(self.times)

    def drop(self):
        """最近一帧携带的 MCU 丢帧字节(低 8 位); 未收到过则为 None。"""
        with self._lock:
            return self._drop

    def time_of(self, i: int):
        with self._lock:
            return self.times[i] if 0 <= i < len(self.times) else None

    def ms8_of(self, i: int):
        with self._lock:
            return self.ms8s[i] if 0 <= i < len(self.ms8s) else None

    def flags_of(self, i: int):
        """第 i 帧的 flags 字节; 越界/未到返回 None。

        v2.6.1: `flags & 0x08`(FLAG_PULLF1) = "这一帧是收到 PULL 后发出的第一帧" ——
        事件触发式拉取的**批起点判据直接读它**(读事实), 不再靠 ms8 帧间隙推算。
        """
        with self._lock:
            return self.flags[i] if 0 <= i < len(self.flags) else None

    def dump_flags(self):
        """flags 全量快照(与 dump_batches 同源同序)。"""
        with self._lock:
            return list(self.flags)

    def times_from(self, i: int):
        with self._lock:
            return list(self.times[i:])

    def dump(self):
        """(times, ms8s, drops) 全量快照 —— 结束时一次性落盘, 避免漏帧。"""
        with self._lock:
            return list(self.times), list(self.ms8s), list(self.drops)

    def dump_batches(self):
        """带批号的全量快照 (times, ms8s, drops, batches)。"""
        with self._lock:
            return (list(self.times), list(self.ms8s), list(self.drops),
                    list(self.batches))

    def close(self):
        self._store.on_frame = self._prev


def send_cmd(mc, text, timeout, tries=3):
    """带重试的命令发送(连接后首条命令常丢)。返回 (ok, status)。"""
    last = "?"
    for _ in range(tries):
        try:
            p = mc.cmd.send(text, wait=True, timeout=timeout, no_retry=True)
            last = str(p.status)
            if p.ok:
                return True, last
        except Exception as e:
            last = type(e).__name__
        time.sleep(0.15)
    return False, last


def pctl(xs, q):
    if not xs:
        return float("nan")
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def main() -> int:
    ap = argparse.ArgumentParser(description="PC 主导的间隔闭环匀速拉取(PULL pacing)")
    ap.add_argument("port")
    ap.add_argument("--fps", type=float, default=12.0, help="目标**到达**帧率(Hz)")
    ap.add_argument("--batch", type=int, default=2, help="每次拉取的帧数 n(1..16)")
    ap.add_argument("--duration", type=float, default=120.0)
    ap.add_argument("--phase-gain", type=float, default=0.0,
                    help="ms8 相位闭环增益(0=关闭)。实测打开会把发送推得过早, "
                         "令新 PULL 打断上一批而丢帧, 且不改善 67/142 抖动 —— 仅作实验")
    ap.add_argument("--safety-ms", type=float, default=12.0,
                    help="拉取请求不得早于上一批末帧应到时刻的量(避免打断丢帧)")
    ap.add_argument("--warmup-rtt-ms", type=float, default=100.0,
                    help="首轮**整趟往返(RTT)**估计的初值: 命令下行 + 首帧上行")
    ap.add_argument("--gap-timeout", type=float, default=3.0, help="连续无帧判断连(s)")
    ap.add_argument("--timeout", type=float, default=2.0)
    ap.add_argument("--aimd", action="store_true",
                    help="自动提速: 窗口内零丢帧则 fps*=1.03, 出现大间隔/丢帧则 fps*=0.8")
    ap.add_argument("--aimd-every", type=float, default=15.0, help="AIMD 判定窗口(s)")
    ap.add_argument("--fps-max", type=float, default=30.0)
    ap.add_argument("--fps-min", type=float, default=2.0)
    ap.add_argument("--lag-warn-ms", type=float, default=40.0,
                    help="打印'发送已过期'告警的阈值(说明 n 或 RTT 不匹配)")
    ap.add_argument("--tag", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--report-every", type=float, default=10.0)
    ap.add_argument("--append-matrix", default=None)
    a = ap.parse_args()

    tag = a.tag or time.strftime("%m%d-%H%M%S")
    out_csv = a.out or ("pace_%s.csv" % tag)
    frames_csv = out_csv.replace(".csv", "_frames.csv")
    n_frames = max(1, min(16, a.batch))

    print("⚠ 这是**排程式**拉取(按时间表无条件发 PULL), 已被事件触发式"
          " `link_pull_evt.py` 取代。\n"
          "  2026-10-20 远程档(fw=300)实测: 12Hz/n=2 跑到 6.9s 批尾即**链路全静默**\n"
          "  (CH340 仍在、模块不再转发, 静候 25s 不自愈, 须物理断电)。\n"
          "  ⇒ 真机长跑请用 `link_pull_evt.py`(周期≡RTT 自锁, 在途恒 ≤1)。\n")

    mc = MouseClient(auto_session=False, auto_stream=False)
    if not mc.connect(a.port):
        print("✗ 连接失败:", mc.state_detail)
        return 2
    time.sleep(0.3)
    fl = FrameLog(mc.store)

    ok, st = send_cmd(mc, "PING", a.timeout)
    print("PING ->", "OK" if ok else "失败(%s)" % st)
    if not ok:
        print("✗ 链路无响应, 退出")
        fl.close()
        return 2

    # ---------------------------------------------------------- 起步
    fps = a.fps
    t_cmd_ms = int(round(1000.0 / fps))
    ok, st = send_cmd(mc, "RATE PULL %d %d" % (n_frames, t_cmd_ms), a.timeout)
    print("RATE PULL %d %d -> %s (%s)" % (n_frames, t_cmd_ms, "OK" if ok else "失败", st))
    if not ok:
        print("✗ 无法进入 PULL 模式(固件是否支持 RATE PULL?)")
        fl.close()
        return 3

    rtt_est = a.warmup_rtt_ms / 1000.0
    warm_until = time.monotonic() + 2.0
    while time.monotonic() < warm_until and fl.n() == 0:
        time.sleep(0.05)
    if fl.n() == 0:
        print("✗ 2s 内无帧: 检查固件/参数")
        fl.close()
        return 3

    fh = open(out_csv, "w", newline="", encoding="utf-8-sig")
    w = csv.writer(fh)
    w.writerow(["t_s", "batches", "frames", "recv_hz", "cmd_hz", "frame_hz",
                "mcu_gap_ms", "rtt_ms", "drop_u8", "alive", "note"])
    fh.flush()
    ffr = open(frames_csv, "w", newline="", encoding="utf-8-sig")
    wfr = csv.writer(ffr)
    # gap_host_ms: 主机侧到达差(**含 CH340/USB 攒批抖动, 只能用于速率**)
    # gap_mcu_ms : 帧内 ms8 差(单片机发帧时刻, **判定均匀性的真值**)
    wfr.writerow(["idx", "t_s", "gap_host_ms", "gap_mcu_ms", "ms8", "batch", "drop_u8"])
    ffr.flush()

    print("")
    print("目标 %.2f Hz (帧内节拍 t=%d ms, 每批 %d 帧 ⇒ 授权 %.2f Hz)"
          % (1000.0 / t_cmd_ms, t_cmd_ms, n_frames, n_frames * 1000.0 / t_cmd_ms))
    print("%7s %8s %8s %9s %9s %9s %8s %8s %6s" % (
        "t_s", "批次数", "累计帧", "实收Hz", "授权Hz", "帧内Hz", "到达间隔", "RTT_ms", "丢帧"))
    print("-" * 78)

    t0 = time.monotonic()
    due = t0 + 0.05                # 下一批**首帧应到达**的时刻
    batches = 0
    exp_frames = 0
    last_frames_idx = fl.n()
    last_rep = t0
    last_n = fl.n()
    last_rx = time.monotonic()
    last_seen = fl.n()
    prev_frame_t = None
    prev_ms8 = None
    last_logged = fl.n()
    pending = None                 # (send_time, idx_before, send_time) 待反馈的批次
    prev_batch_m8 = None           # 上一批**首帧**的 ms8 / 到达时刻(相位闭环用)
    prev_batch_t = None
    phase_corr = 0.0               # ms8 相位闭环的发送时刻修正(s)
    emit_gaps: list[float] = []    # 批起点间隔(单片机 ms8, ms)
    gaps: list[float] = []         # 主机侧相邻帧到达间隔(ms, 含攒批抖动)
    host_gaps: list[float] = []    # 同 gaps
    mcu_gaps: list[float] = []     # 帧内 ms8 相邻帧间隔(ms, 真值)
    last_drop = fl.drop()
    drop_acc = 0
    over_warn = 0
    interrupt_hits = 0             # "发送已过期" 次数
    last_aimd = t0
    win_frames_at = 0
    win_lost = 0                   # 窗口内"批次没收到首帧"的次数
    result, t_fail, end_note = "ABORTED", None, ""

    try:
        while True:
            now = time.monotonic()
            el = now - t0
            if el >= a.duration:
                result, end_note = "SURVIVED", "达到时长上限"
                break

            # ---------------- 断连判定
            if now - last_rx > a.gap_timeout:
                result, t_fail = "BROKE", el
                end_note = "连续 %.1fs 无帧" % (now - last_rx)
                print("\n✗✗ 断连: %s (t=%.1fs)" % (end_note, el))
                break

            # ---------------- 计算本批发送时刻: send = due - RTT(整趟, 不是半趟!)
            #  单片机收到 PULL 后**立即**发第 1 帧, 该帧要"命令下行 + 帧上行"整整一趟
            #  才到 PC ⇒ 想让帧落在 due, 必须在 due - RTT 发出命令。
            #  ⚠️ 早期版本写成 due - RTT/2: 于是每批都晚 RTT/2 到达, 而网格又锁到实测
            #     首帧 ⇒ 误差逐批累积成"周期变长", 实测被压到 10.4Hz(目标 12.05)。
            #  另一条约束的对象是"命令**到达单片机**的时刻"(= send + RTT/2):
            #     必须 >= 上一批末帧应发时刻(固件收到新 PULL 会丢弃本批剩余帧),
            #     上一批末帧 ≈ due - t ⇒ send >= (due - t) - RTT/2 + safety。
            send_at = due - rtt_est + phase_corr
            earliest = (due - t_cmd_ms / 1000.0) - rtt_est / 2.0 + a.safety_ms / 1000.0
            if send_at < earliest:
                send_at = earliest
            if now - send_at > a.lag_warn_ms / 1000.0:
                interrupt_hits += 1
                if interrupt_hits <= 3 or interrupt_hits % 50 == 0:
                    print("⚠ 发送落后 %.0f ms(n=%d, RTT≈%.0f ms): 让位立即发"
                          % ((now - send_at) * 1000, n_frames, rtt_est * 1000))
                send_at = now
                due = now + rtt_est

            # ---------------- 等到点再发
            #  ⚠️ 不要用长自旋(busy-wait)等: 主线程自旋会挤占 GIL, 收帧线程读串口的
            #     节奏被打乱 -> CH340/BT 模块缓冲灌满 -> 触发"模块停转发"的已知故障。
            #     实测自旋**并不能**改善抖动(67/142 尖峰照旧), 所以老实 sleep。
            #     Windows 的 sleep 粒度 ~15.6ms, 对 ~166ms 的批周期来说够用。
            while True:
                now = time.monotonic()
                # 断连判定的"最后收帧时刻"直接由帧计数驱动 —— 与 pending 逻辑解耦,
                # 免得某一处漏更就把整轮跑成假断连(这是踩过的坑)。
                if fl.n() > last_seen:
                    last_seen = fl.n()
                    last_rx = time.monotonic()
                d = send_at - now
                if d <= 0:
                    break
                if now - last_rx > a.gap_timeout:
                    break
                time.sleep(max(0.001, min(0.005, d)))

            # ---------------- 上一批反馈: RTT 估计 + ms8 相位闭环
            #  ⚠️ 位置很关键: 必须放在"等到点"**之后**。放在循环顶部时, 它紧接着上一次
            #     发送执行(两者之间没有耗时), 于是 `fl.n() > pending[1]` 永远是 `N > N`
            #     ⇒ 分支永不触发 ⇒ last_rx 不更新(3s 后误判断连)、rtt_est/phase_corr 空转。
            #     放在这里: 距上次发送已过去约一个批周期, 首帧早就到了, 反馈是真实且非阻塞的。
            if pending is not None and fl.n() > pending[1]:
                _, p_idx, p_send = pending
                first_t = fl.time_of(p_idx)
                m8f = fl.ms8_of(p_idx)
                rtt = first_t - p_send                     # 命令下行 + 首帧上行 = 整趟
                if 0.005 < rtt < 2.0:
                    rtt_est = 0.75 * rtt_est + 0.25 * rtt
                if m8f is not None and prev_batch_m8 is not None and prev_batch_t is not None:
                    raw = (m8f - prev_batch_m8) & 0xFF     # 批起点间隔(单片机 ms8, 只有低 8 位)
                    # ⚠️ 解缠的参考必须用**名义批周期 T_ms = n*t_cmd_ms**, 不能用主机时间戳:
                    #    主机侧被 CH340/USB 攒批污染(σ≈75ms), 拿它当 ref 会挑错分支 ——
                    #    实测 n=2/t=75 时真值 150ms 被判成 181ms。
                    #    单片机 ms8 时钟与墙上时钟比率 1.0002, 故真值必在 T_ms 附近 ±75ms 内,
                    #    以 T_ms 为参考选最近的候选是稳的。
                    T_ms = float(n_frames * t_cmd_ms)
                    if T_ms < 240.0:
                        gap_emit = float(raw)              # 真值 <256ms, 无需解缠
                    else:
                        k_max = int(T_ms // 256.0) + 2
                        cand = [raw + 256 * k for k in range(-k_max, k_max + 1)]
                        gap_emit = float(min(cand, key=lambda v: abs(v - T_ms)))
                    err = gap_emit - T_ms              # 目标: 批起点间隔 = n*T
                    phase_corr -= a.phase_gain * err / 1000.0
                    _lim = 0.3 * n_frames / fps
                    phase_corr = max(-_lim, min(_lim, phase_corr))
                    emit_gaps.append(gap_emit)
                if m8f is not None:
                    prev_batch_m8, prev_batch_t = m8f, first_t
                pending = None
                last_rx = time.monotonic()

            idx_before = fl.n()
            send_time = time.monotonic()
            # 批号必须在发出命令**之前**写入: 读线程钩子按 cur_batch 给到达帧打标。
            fl.cur_batch = batches + 1
            mc.cmd.send("RATE PULL %d %d" % (n_frames, t_cmd_ms), wait=False, no_retry=True)
            batches += 1
            exp_frames += n_frames
            pending = (send_time, idx_before, send_time)

            # 网格推进恒为 n/fps —— **不锁实测首帧**:
            #  锁实测会把 RTT 抖动/偏差写进网格而累积成速率误差(见上面的注释);
            #  恒定推进则只留下"相位偏移(常数)"与"抖动(零均值)", 平均速率不受影响。
            due = due + n_frames / fps

            # 逐帧登记主机侧 gap(全量, 不漏帧); MCU(ms8) 侧留到收尾一次性算
            new_idx = fl.n()
            for i in range(last_logged, new_idx):
                tt = fl.time_of(i)
                if prev_frame_t is not None:
                    gaps.append((tt - prev_frame_t) * 1000.0)
                prev_frame_t = tt
            last_logged = new_idx

            # ---------------- AIMD(可选)
            if a.aimd and (time.monotonic() - last_aimd) >= a.aimd_every:
                seg = [g for g in gaps[win_frames_at:] if isinstance(g, float)]
                big = sum(1 for g in seg if g > 1.5 * (1000.0 / fps) + 20)
                nd = fl.drop()
                mcu_back = (last_drop is not None and nd is not None and nd != last_drop)
                if win_lost or mcu_back or (seg and big / len(seg) > 0.05):
                    fps = max(a.fps_min, fps * 0.8)
                elif seg:
                    fps = min(a.fps_max, fps * 1.03)
                t_cmd_ms = max(1, int(round(1000.0 / fps)))
                win_frames_at = len(gaps)
                win_lost = 0
                last_aimd = time.monotonic()

            # ---------------- 逐秒上报
            if time.monotonic() - last_rep >= a.report_every:
                now2 = time.monotonic()
                dt = now2 - last_rep
                n_now = fl.n()
                dn = n_now - last_n
                recv_hz = dn / dt if dt > 0 else 0.0
                nd = fl.drop()
                if nd is not None and last_drop is not None:
                    drop_acc += (nd - last_drop) & 0xFF
                last_drop = nd
                seg = [g for g in gaps if isinstance(g, float)][-int(max(1, dn)):]
                gap_mean = statistics.fmean(seg) if seg else float("nan")
                w.writerow(["%.1f" % (now2 - t0), batches, n_now, "%.2f" % recv_hz,
                            "%.2f" % (n_frames * 1000.0 / t_cmd_ms),
                            "%.2f" % (1000.0 / t_cmd_ms),
                            "%.1f" % gap_mean, "%.1f" % (rtt_est * 1000),
                            nd if nd is not None else "", 1, ""])
                fh.flush()
                ffr.flush()
                print("%7.1f %8d %8d %9.2f %9.2f %9.2f %8.1f %8.0f %6s" % (
                    now2 - t0, batches, n_now, recv_hz, n_frames * 1000.0 / t_cmd_ms,
                    1000.0 / t_cmd_ms, gap_mean, rtt_est * 1000,
                    nd if nd is not None else "-"))
                last_rep, last_n = now2, n_now

    except KeyboardInterrupt:
        result, end_note = "ABORTED", "用户中断"
    finally:
        el = time.monotonic() - t0
        total = fl.n()
        alive_s = t_fail if t_fail is not None else el
        recv_hz = total / alive_s if alive_s > 0 else 0.0
        deliver = total / float(exp_frames) if exp_frames else 0.0
        fh.close()
        fl.close()
        try:
            mc.disconnect()
        except Exception:
            pass

        # ---------------- 全量转储逐帧数据(收尾一次性写, 避免漏帧)
        times, ms8s, drops, batchnos = fl.dump_batches()
        mcu_gaps = []
        mcu_ambig = 0
        prev_m8 = None
        for i in range(len(times)):
            hg = (times[i] - times[i - 1]) * 1000.0 if i else None
            mg = ""
            if prev_m8 is not None and i < len(ms8s):
                raw = (ms8s[i] - prev_m8) & 0xFF
                if hg is None or hg < 20.0:
                    # 主机侧被攒批污染(≈0): 直接取 raw。仅当真实间隔 < 256ms 时成立
                    mg = float(raw)
                    if raw < 20:
                        mcu_ambig += 1
                else:
                    cand = [raw + 256 * k for k in range(-4, 5)]
                    mg = float(min(cand, key=lambda v: abs(v - hg)))
                    if raw < 20:
                        mcu_ambig += 1
                mcu_gaps.append(mg)
            wfr.writerow([i + 1, "%.4f" % (times[i] - t0),
                          ("%.2f" % hg) if hg is not None else "",
                          ("%.1f" % mg) if mg != "" else "",
                          ms8s[i] if i < len(ms8s) else "",
                          batchnos[i] if i < len(batchnos) else "",
                          drops[i] if i < len(drops) else ""])
            prev_m8 = ms8s[i] if i < len(ms8s) else prev_m8
        ffr.close()

        gg = [g for g in gaps if isinstance(g, float)]
        tgt = 1000.0 / fps
        big = [g for g in gg if g > 1.5 * tgt + 20]
        cv = (statistics.pstdev(gg) / statistics.fmean(gg)) if len(gg) > 2 else float("nan")
        mm = [g for g in mcu_gaps if isinstance(g, float)]
        mcu_cv = (statistics.pstdev(mm) / statistics.fmean(mm)) if len(mm) > 2 else float("nan")
        mcu_big = [g for g in mm if abs(g - tgt) > 0.25 * tgt + 5]

        print("")
        print("=" * 78)
        print("结果: %s  存活 %.1fs %s" % (result, alive_s,
                                          ("(断于 t=%.1fs)" % t_fail) if t_fail else ""))
        print("      批次 %d  授权帧 %d  实收帧 %d  交付率 %.1f%%"
              % (batches, exp_frames, total, 100 * deliver))
        print("      **PC 实收 %.2f Hz**  vs  授权(n/t) %.2f Hz  vs  帧内(1000/t) %.2f Hz"
              % (recv_hz, n_frames * 1000.0 / t_cmd_ms, 1000.0 / t_cmd_ms))
        print("      主机侧间隔: 均值 %.1f ms  σ %.1f ms  p95 %.1f ms  最大 %.1f ms"
              % ((statistics.fmean(gg) if gg else float("nan")),
                 (statistics.pstdev(gg) if len(gg) > 1 else float("nan")),
                 pctl(gg, 0.95), (max(gg) if gg else float("nan"))))
        print("      ★单片机侧间隔(ms8, 真值): 均值 %.1f ms  σ %.1f ms  p95 %.1f ms  最大 %.1f ms  "
              "目标 %.1f ms" % ((statistics.fmean(mm) if mm else float("nan")),
                                (statistics.pstdev(mm) if len(mm) > 1 else float("nan")),
                                pctl(mm, 0.95), (max(mm) if mm else float("nan")), tgt))
        import collections as _c
        _hist = _c.Counter([int(round(x)) for x in mm]).most_common(8) if mm else []
        print("      MCU 间隔直方图: %s%s" % (_hist, ("(含 %d 个疑似跨 256 折叠的样本)" % mcu_ambig) if mcu_ambig else ""))
        print("      抖动: 主机 CV %.3f -> MCU CV %.3f;MCU 侧偏离目标 >25%%+5ms 的间隔 %d/%d;"
              "发送过期让位 %d 次;批次无首帧 %d 次"
              % (cv, mcu_cv, len(mcu_big), len(mm), interrupt_hits, win_lost))
        print("      MCU 丢帧字节: 末值 %s(非零出现 %d 次, 增量累计 %s)"
              % (fl.drop(), fl.drop_seen, drop_acc))
        if emit_gaps:
            tgt_batch = n_frames * 1000.0 / fps
            print("      ★批起点间隔(ms8) 均值 %.1f ms σ %.1f ms(目标 %.1f ms = n*T)"
                  % (statistics.fmean(emit_gaps), statistics.pstdev(emit_gaps), tgt_batch))
        print("      RTT(整趟) 估计收敛到 %.1f ms;  相位修正 %.1f ms"
              % (rtt_est * 1000, phase_corr * 1000))
        print("=" * 78)

        with open(out_csv.replace(".csv", "_summary.txt"), "w", encoding="utf-8") as f:
            f.write("标签: %s\n" % tag)
            f.write("结果: %s\n" % result)
            f.write("存活时长: %.1fs\n" % alive_s)
            f.write("断连时刻: %s\n" % (("%.1fs" % t_fail) if t_fail else "-"))
            f.write("目标到达帧率: %.2f Hz (帧内 t=%d ms, 每批 n=%d)\n"
                    % (1000.0 / t_cmd_ms, t_cmd_ms, n_frames))
            f.write("批次: %d\n授权帧: %d\n实收帧: %d\n交付率: %.1f%%\n"
                    % (batches, exp_frames, total, 100 * deliver))
            f.write("PC 实收: %.2f Hz\n授权(n/t): %.2f Hz\n帧内(1000/t): %.2f Hz\n"
                    % (recv_hz, n_frames * 1000.0 / t_cmd_ms, 1000.0 / t_cmd_ms))
            f.write("主机侧间隔均值: %.2f ms\n主机侧间隔σ: %.2f ms\n主机侧p95: %.2f ms\n"
                    % (statistics.fmean(gg) if gg else float("nan"),
                       statistics.pstdev(gg) if len(gg) > 1 else float("nan"),
                       pctl(gg, 0.95)))
            f.write("单片机侧间隔均值(ms8): %.2f ms\n单片机侧间隔σ: %.2f ms\n"
                    "单片机侧p95: %.2f ms\n单片机侧最大: %.2f ms\n"
                    % ((statistics.fmean(mm) if mm else float("nan")),
                       (statistics.pstdev(mm) if len(mm) > 1 else float("nan")),
                       pctl(mm, 0.95), max(mm) if mm else float("nan")))
            f.write("CV 主机: %.4f\nCV 单片机: %.4f\n"
                    % (cv, mcu_cv))
            f.write("主机大间隔(>1.5t+20ms): %d/%d\n单片机偏离(>25%%t+5ms): %d/%d\n"
                    % (len(big), len(gg), len(mcu_big), len(mm)))
            f.write("发送过期让位: %d\n批次无首帧: %d\n" % (interrupt_hits, win_lost))
            f.write("MCU丢帧字节末值: %s (非零出现 %d 次, 增量 %s)\n"
                    % (fl.drop(), fl.drop_seen, drop_acc))
            f.write("RTT(整趟) 估计: %.1f ms\n" % (rtt_est * 1000))
        print("逐帧数据:", frames_csv)
        print("逐秒数据:", out_csv)
        print("汇总:", out_csv.replace(".csv", "_summary.txt"))

        if a.append_matrix:
            new = not os.path.exists(a.append_matrix)
            with open(a.append_matrix, "a", newline="", encoding="utf-8-sig") as f:
                mm = csv.writer(f)
                if new:
                    mm.writerow(["tag", "result", "alive_s", "t_fail_s", "batch_n",
                                 "t_cmd_ms", "target_hz", "recv_hz", "deliver_pct",
                                 "gap_mean_ms", "gap_cv", "large_gaps"])
                mm.writerow([tag, result, "%.1f" % alive_s,
                             ("%.1f" % t_fail) if t_fail else "", n_frames, t_cmd_ms,
                             "%.2f" % (1000.0 / t_cmd_ms), "%.2f" % recv_hz,
                             "%.1f" % (100 * deliver),
                             ("%.1f" % statistics.fmean(gg)) if gg else "",
                             "%.3f" % cv, "%d/%d" % (len(big), len(gg))])
            print("总表已追加:", a.append_matrix)
    return 0 if result == "SURVIVED" else 1


if __name__ == "__main__":
    sys.exit(main())
