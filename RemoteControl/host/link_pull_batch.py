# -*- coding: utf-8 -*-
"""融合方案: PC 主导的「批量节拍拉取」压测/闭环工具。

思路
----
固件新增 `RATE PULL <n> <t_ms>`: 单片机按 `t_ms` 间隔连发 `n` 帧, 发完静默等下一条
PULL; 期间收到**新 PULL** 立即打断重排。于是"每次拉取发几帧、帧间留多久"完全由 PC
掌控 —— 既摊薄命令往返(RTT), 又能用 (n, t) 组合制造"突发 + 静默"占空比。

本工具两种用法
--------------
1. **定周期**(`--period-ms`): 像 RATE FIX 一样恒定节拍, 但节拍由 PC 下命令决定,
   每次 PULL 都是一次"信用+节拍"授权。用来做和 RATE FIX 的可比对照。
2. **自适应**(默认): PC 从 `--start-period-ms` 起步, 按**实收/应收**比例闭环调 t
   —— 零丢帧就缩短 t(提速率), 出现丢帧就拉长 t(退避)。目标: 找到"零丢帧的最高速率"。
   同时读取帧内 `LINK_U_DROP`(MCU 因 TX 环满丢的帧数), 一旦它上涨说明是单片机侧喂不动,
   立即退避(与链路中段丢帧区分开)。

产物(边跑边落盘)
----------------
* `pull_<tag>.csv`         逐秒: t_s,batches,frames,rate_hz,period_ms,mcu_drop,alive,note
* `pull_<tag>_summary.txt` 结论: 存活/断连时刻、平均速率、丢帧率、最终周期
* `--append-matrix FILE`   跨环境总表追加一行(SURVIVED/BROKE/ABORTED)

用法
----
    python link_pull_batch.py COM7 --batch 2 --start-period-ms 83 --duration 300
    python link_pull_batch.py COM7 --batch 4 --period-ms 5 --duration 300 --tag burst4
    python link_pull_batch.py COM7 --batch 2 --period-ms 83 --duration 600 --append-matrix pull_matrix.csv
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import os
import sys
import time

sys.path.insert(0, ".")

import host.registry as registry
from host.registry import TYPE_UNIFIED

# ------------------------------------------------- 让 decoder 顺便吐出 drop / F
_orig = registry.FRAMES[TYPE_UNIFIED]
_orig_decoder = _orig.decoder


def _decoder_with_extras(payload: bytes) -> dict:
    """在 registry 解码器之上补一个 `mcuDrop`(固件 TX 环累计丢帧低 8 位, offset 44)。

    ⚠️ v2.6 起**不再**补 `fpsBroadcast`: 统一帧尾部的 2 B 目标帧率是实验期字段,
    已随 AIMD 一起删除, 载荷恒定 49 B —— 那两字节不存在。
    """
    d = _orig_decoder(payload)
    if len(payload) > 44:
        d["mcuDrop"] = payload[44]
    return d


registry.FRAMES[TYPE_UNIFIED] = dataclasses.replace(_orig, decoder=_decoder_with_extras)

from host.api import MouseClient  # noqa: E402


class FrameLog:
    """挂在 store.on_frame 上, 线程安全地记录每帧到达时刻。"""

    def __init__(self, store):
        import threading
        self._lock = threading.Lock()
        self.times: list[float] = []
        self.n = 0
        self.drop = None
        self._prev = getattr(store, "on_frame", None)

        def hook(stream, rec):
            if stream == "unified":
                with self._lock:
                    self.n += 1
                    self.times.append(float(rec.get("_rx_mono") or time.monotonic()))
                    if "mcuDrop" in rec:
                        self.drop = rec["mcuDrop"]
            if self._prev:
                self._prev(stream, rec)

        store.on_frame = hook
        self._store = store

    def snapshot(self):
        """返回 (累计帧数, MCU 丢帧字节低 8 位)。"""
        with self._lock:
            return self.n, self.drop

    def since(self, cut: float):
        with self._lock:
            return [t for t in self.times if t >= cut]

    def close(self):
        self._store.on_frame = self._prev


def send_cmd(mc, text, timeout, tries=3):
    """发送命令, 带重试(连接后首条命令常丢)。返回 (ok, status)。"""
    last = "?"
    for i in range(tries):
        try:
            p = mc.cmd.send(text, wait=True, timeout=timeout, no_retry=True)
            last = str(p.status)
            if p.ok:
                return True, last
        except Exception as e:
            last = type(e).__name__
        time.sleep(0.15)
    return False, last


def main() -> int:
    ap = argparse.ArgumentParser(description="PC 主导的批量节拍拉取 (PULL) 压测/闭环")
    ap.add_argument("port")
    ap.add_argument("--batch", type=int, default=2, help="每次拉取发送帧数 n (默认 2)")
    ap.add_argument("--period-ms", type=float, default=None,
                    help="固定批内间隔(ms); 给定则不启用自适应")
    ap.add_argument("--start-period-ms", type=float, default=83.0,
                    help="自适应起始间隔(ms), 默认 83 ≈ 12Hz")
    ap.add_argument("--min-period-ms", type=float, default=33.0, help="自适应下限(≈30Hz)")
    ap.add_argument("--max-period-ms", type=float, default=500.0, help="自适应上限(≈2Hz)")
    ap.add_argument("--adapt-every", type=int, default=8, help="每 N 批调整一次间隔")
    ap.add_argument("--up-factor", type=float, default=0.95, help="零丢帧时的缩短系数")
    ap.add_argument("--down-factor", type=float, default=1.15, help="丢帧时的拉长系数")
    ap.add_argument("--good-ratio", type=float, default=0.995, help="视为零丢帧的交付率")
    ap.add_argument("--bad-ratio", type=float, default=0.98, help="触发退避的交付率")
    ap.add_argument("--duration", type=float, default=300.0, help="最长时长(s)")
    ap.add_argument("--gap-timeout", type=float, default=3.0, help="连续无帧判定断连(s)")
    ap.add_argument("--timeout", type=float, default=2.0, help="单条命令超时(s)")
    ap.add_argument("--tag", default=None, help="环境标签(决定文件名)")
    ap.add_argument("--out", default=None, help="逐秒 CSV 路径(默认 pull_<tag>.csv)")
    ap.add_argument("--report-every", type=float, default=10.0, help="控制台打印间隔(s)")
    ap.add_argument("--append-matrix", default=None, help="追加跨环境总表 CSV")
    a = ap.parse_args()

    tag = a.tag or time.strftime("%m%d-%H%M%S")
    out_csv = a.out or ("pull_%s.csv" % tag)
    n_frames = max(1, min(16, a.batch))

    mc = MouseClient(auto_session=False, auto_stream=False)
    if not mc.connect(a.port):
        print("✗ 连接失败:", mc.state_detail)
        return 2
    time.sleep(0.3)
    fl = FrameLog(mc.store)

    # ---------------------------------------------------------- 启动自检
    ok, st = send_cmd(mc, "PING", a.timeout)
    print("PING ->", "OK" if ok else "失败(%s)" % st)
    if not ok:
        print("✗ 链路无响应, 退出")
        fl.close()
        return 2

    period = a.period_ms if a.period_ms else a.start_period_ms
    adaptive = a.period_ms is None

    # 首条 PULL 兼作"打开自时钟"
    ok, st = send_cmd(mc, "RATE PULL %d %d" % (n_frames, int(round(period))), a.timeout)
    print("RATE PULL %d %d -> %s (%s)" % (n_frames, int(round(period)), "OK" if ok else "失败", st))
    if not ok:
        print("✗ 无法进入 PULL 模式(固件是否支持 RATE PULL?)")
        fl.close()
        return 3

    t0 = time.monotonic()
    # 启动自检: 等第一帧, 核对帧内广播 F 是否 ≈ 1000/period
    wait_until = time.monotonic() + 2.0
    while time.monotonic() < wait_until and fl.snapshot()[0] == 0:
        time.sleep(0.05)
    n0, _ = fl.snapshot()
    if n0 > 0:
        print("启动自检: 首帧已到 (PULL %d 帧 / %.0f ms)" % (n_frames, period))
    else:
        print("启动自检: 2s 内未收到帧 — 若持续为 0 请检查 PULL 参数/固件")

    fh = open(out_csv, "w", newline="", encoding="utf-8-sig")
    w = csv.writer(fh)
    w.writerow(["t_s", "batches", "frames", "rate_hz", "period_ms", "mcu_drop", "alive", "note"])
    fh.flush()

    print("")
    print("%7s %8s %8s %9s %9s %10s" % ("t_s", "批次数", "累计帧", "实测Hz", "周期ms", "MCU丢帧"))
    print("-" * 62)

    batches = 0
    frames_total_live = 0
    last_rep = t0
    last_n = 0
    last_drop = None
    drop_acc = 0
    exp_frames = 0          # 已授权的应收帧数
    win_recv = 0            # 当前自适应窗口内实收
    win_exp = 0             # 当前自适应窗口内应收
    win_batches = 0
    last_rx_time = time.monotonic()

    result = "ABORTED"
    t_fail = None
    end_note = ""

    try:
        while True:
            now = time.monotonic()
            el = now - t0
            if el >= a.duration:
                result = "SURVIVED"
                end_note = "达到时长上限"
                break

            # ---- 断连判定
            if now - last_rx_time > a.gap_timeout:
                result = "BROKE"
                t_fail = el
                end_note = "连续 %.1fs 无帧" % (now - last_rx_time)
                w.writerow(["%.1f" % el, batches, frames_total_live, "%.2f" % 0.0,
                            "%.0f" % period, drop if (drop := fl.snapshot()[1]) is not None else "",
                            0, "DEAD last_frame=%.1fs" % (last_rx_time - t0)])
                fh.flush()
                print("\n✗✗ 断连: %s (t=%.1fs)" % (end_note, el))
                break

            # ---- 下发一次 PULL, 等这批帧到齐(或超时)
            before = fl.snapshot()[0]
            ok, st = send_cmd(mc, "RATE PULL %d %d" % (n_frames, int(round(period))),
                              a.timeout, tries=2)
            batches += 1
            exp_frames += n_frames
            win_exp += n_frames
            win_batches += 1

            if not ok:
                # 命令不通: 不立刻判死, 交给上面的无帧超时判定
                time.sleep(0.2)
                continue

            # 等这批 n 帧到齐, 最多等 n*period + 400ms 余量
            deadline = time.monotonic() + (n_frames * period / 1000.0) + 0.4
            got = 0
            while time.monotonic() < deadline:
                cnt = fl.snapshot()[0]
                got = cnt - before
                if got >= n_frames:
                    break
                time.sleep(0.01)

            if got > 0:
                last_rx_time = time.monotonic()
            win_recv += min(got, n_frames)
            frames_total_live = fl.snapshot()[0]

            # ---- 自适应调周期
            if adaptive and win_batches >= a.adapt_every:
                ratio = (win_recv / float(win_exp)) if win_exp else 1.0
                _, ndrop = fl.snapshot()
                mcu_backpressure = (last_drop is not None and ndrop is not None
                                    and ndrop != last_drop)
                old_p = period
                if mcu_backpressure or ratio < a.bad_ratio:
                    period = min(a.max_period_ms, period * a.down_factor)
                elif ratio >= a.good_ratio:
                    period = max(a.min_period_ms, period * a.up_factor)
                win_recv = 0
                win_exp = 0
                win_batches = 0
                if abs(period - old_p) > 1e-9:
                    pass  # 下一轮 PULL 生效

            # ---- 逐秒上报
            if time.monotonic() - last_rep >= a.report_every:
                now2 = time.monotonic()
                dt = now2 - last_rep
                dn = fl.snapshot()[0] - last_n
                _, ndrop = fl.snapshot()
                if ndrop is not None and last_drop is not None:
                    drop_acc += (ndrop - last_drop) & 0xFF
                last_drop = ndrop
                rate = dn / dt if dt > 0 else 0.0
                print("%7.1f %8d %8d %9.2f %9.0f %10s" % (
                    now2 - t0, batches, fl.snapshot()[0], rate, period,
                    ndrop if ndrop is not None else "-"))
                w.writerow(["%.1f" % (now2 - t0), batches, fl.snapshot()[0],
                            "%.2f" % rate, "%.1f" % period,
                            ndrop if ndrop is not None else "", 1, ""])
                fh.flush()
                last_rep = now2
                last_n = fl.snapshot()[0]
    except KeyboardInterrupt:
        result = "ABORTED"
        end_note = "用户中断"
    finally:
        el = time.monotonic() - t0
        frames_total_live = fl.snapshot()[0]
        alive_s = (t_fail if t_fail is not None else el)
        rate_avg = frames_total_live / alive_s if alive_s > 0 else 0.0
        deliver = (frames_total_live / float(exp_frames)) if exp_frames else 0.0
        _, ndrop = fl.snapshot()
        fh.close()
        fl.close()
        try:
            mc.disconnect()
        except Exception:
            pass

        print("")
        print("=" * 62)
        print("结果: %s   存活 %.1fs %s" % (result, alive_s,
                                          ("(断于 t=%.1fs)" % t_fail) if t_fail else ""))
        print("      批次 %d  授权帧 %d  实收帧 %d  交付率 %.1f%%"
              % (batches, exp_frames, frames_total_live, 100 * deliver))
        print("      平均速率 %.2f Hz  最终周期 %.0f ms  批量 n=%d"
              % (rate_avg, period, n_frames))
        print("      MCU 侧 TX 环丢帧(增量累计) %d" % drop_acc)
        print("=" * 62)

        if True:
            with open(out_csv.replace(".csv", "_summary.txt"), "w", encoding="utf-8") as f:
                f.write("标签: %s\n" % tag)
                f.write("结果: %s\n" % result)
                f.write("存活时长: %.1fs\n" % alive_s)
                f.write("断连时刻: %s\n" % (("%.1fs" % t_fail) if t_fail else "-"))
                f.write("批次: %d\n" % batches)
                f.write("授权帧: %d\n" % exp_frames)
                f.write("实收帧: %d\n" % frames_total_live)
                f.write("交付率: %.1f%%\n" % (100 * deliver))
                f.write("平均速率: %.2f Hz\n" % rate_avg)
                f.write("批量 n: %d\n" % n_frames)
                f.write("最终周期: %.0f ms (%s)\n"
                        % (period, "自适应" if adaptive else "固定"))
                f.write("MCU丢帧增量: %d\n" % drop_acc)
            print("逐秒数据:", out_csv)
            print("汇总:", out_csv.replace(".csv", "_summary.txt"))

        if a.append_matrix:
            new = not os.path.exists(a.append_matrix)
            with open(a.append_matrix, "a", newline="", encoding="utf-8-sig") as f:
                mm = csv.writer(f)
                if new:
                    mm.writerow(["tag", "result", "alive_s", "t_fail_s", "batch_n",
                                 "final_period_ms", "avg_hz", "deliver_pct", "predeath_note"])
                mm.writerow([tag, result, "%.1f" % alive_s,
                             ("%.1f" % t_fail) if t_fail else "",
                             n_frames, "%.0f" % period, "%.2f" % rate_avg,
                             "%.1f" % (100 * deliver), end_note])
            print("总表已追加:", a.append_matrix)
    return 0 if result == "SURVIVED" else 1


if __name__ == "__main__":
    sys.exit(main())
