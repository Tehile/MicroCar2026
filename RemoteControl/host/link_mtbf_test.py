#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""固定 FPS 连续通信 / MTBF 压测器 (v2.6-exp)

以**恒定 FPS** 为通信基准, 对 "PC <-> 蓝牙透传 <-> 电脑鼠(MCU)" 链路做长时间
连续通信测试, 记录**首次断连时刻**并落盘, 便于在**不同环境**下重复测试、
比对"存活时长"分布, 用于排查随机掉链的物理原因(供电 / USB 口 / 2.4G 干扰 / 距离)。

与 AIMD 实验的区别
-----------------
本工具**不做速率自适应**。固件侧 `RATE FIX <hz>` 进入"固定节拍"模式:
恒定频率持续发帧, 不看门狗、不降速、不停发 —— 负载恒定, 存活时长才可跨环境比较。

用法
----
  python link_mtbf_test.py COM7 --fps 12 --tag baseline --duration 600
  python link_mtbf_test.py COM7 --fps 12 --tag usb2-rear --append-matrix mtbf_matrix.csv
  python link_mtbf_test.py COM7 --fps 10 --keepalive-sec 1     # 双向: 每秒一条命令

产物
----
  mtbf_<tag>.csv          逐秒: t_s, frames, fps_obs, gap_max_ms, total, alive, note
  mtbf_<tag>_summary.txt  本次结论(断连时刻 / 存活时长 / 帧数 / 观测帧率)
  mtbf_matrix.csv         跨环境总表(--append-matrix 时追加一行)
"""

import argparse
import csv
import os
import sys
import threading
import time
from collections import deque

# 让本脚本从任意 cwd 都能 import host 包(仓库根 = host/ 的上一级)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from host.api import MouseClient  # noqa: E402

MATRIX_HEADER = ["tag", "started_at", "port", "fps_hz", "duration_limit_s",
                 "keepalive_s", "result", "t_fail_s", "alive_s",
                 "total_frames", "mean_fps_obs", "max_gap_ms"]


# --------------------------------------------------------------- 帧到达统计
class FrameLog:
    """线程安全: 只记 unified 帧的到达时刻(单调时钟), 主循环按窗口取用。

    hook 在**接收线程**里被调用, 主循环在**主线程**读 —— 用锁保护。
    """

    def __init__(self, keep_s: float = 20.0) -> None:
        self._lock = threading.Lock()
        self._times: deque = deque()
        self._keep = keep_s
        self.total = 0
        self.last_t = None

    def add(self, t: float) -> None:
        with self._lock:
            self._times.append(t)
            self.total += 1
            self.last_t = t
            cut = t - self._keep
            while self._times and self._times[0] < cut:
                self._times.popleft()

    def since(self, cut: float):
        with self._lock:
            return [x for x in self._times if x >= cut]

    def reset(self) -> None:
        """清空计数(启动自检后调用, 让存活统计从正式计时点开始)。"""
        with self._lock:
            self._times.clear()
            self.total = 0
            self.last_t = None


def install_hook(mc: "MouseClient", fl: FrameLog):
    """patch `mc.store.on_frame`(逐帧回调), 返回卸载函数。"""
    store = mc.store
    prev = getattr(store, "on_frame", None)

    def hook(stream, rec):
        if stream == "unified":
            try:
                t = float(rec.get("_rx_mono") or time.monotonic())
            except Exception:
                t = time.monotonic()
            fl.add(t)
        if prev:
            try:
                prev(stream, rec)
            except Exception:
                pass

    store.on_frame = hook
    return lambda: setattr(store, "on_frame", prev)


# --------------------------------------------------------------- 链路原语
def cmd(mc: "MouseClient", word: str, timeout: float):
    """发一条命令并等 ACK。返回 (ok, status_text)。异常一律吞成 (False, 类名)。"""
    try:
        p = mc.cmd.send(word, wait=True, timeout=timeout, no_retry=True)
        return bool(p.ok), str(p.status)
    except Exception as e:
        return False, type(e).__name__


def connect(mc: "MouseClient", port: str) -> bool:
    try:
        return bool(mc.connect(port))
    except Exception:
        return False


def disconnect(mc: "MouseClient") -> None:
    try:
        mc.disconnect()
    except Exception:
        pass


def ensure_fix(mc: "MouseClient", fps_i: int, timeout: float, tries: int = 8):
    """反复下发 `RATE FIX <fps>` 直到 ACK。

    ⚠️ 连接后**首条命令常丢**(真机实测), 必须重试; 否则固件永远停在"上电静默"
    (linkRateOn=0), 会得到"命令通但零帧"的假象。
    返回 (ok, 尝试次数, 最后一次状态文本)。
    """
    word = "RATE FIX %d" % fps_i
    last = "-"
    for i in range(tries):
        ok, note = cmd(mc, word, timeout)
        last = note
        if ok:
            return True, i + 1, note
        time.sleep(0.3)
    return False, tries, last


def probe_recover(mc: "MouseClient", port: str, count: int, gap: float,
                  fps_i: int, timeout: float):
    """断连后周期性"关串口 -> 重开 -> 探活", 观察能否**免断电自愈**。"""
    res = []
    for i in range(count):
        disconnect(mc)
        time.sleep(gap)
        okc = connect(mc, port)
        note = "-"
        ok = False
        if okc:
            ok, note = ensure_fix(mc, fps_i, timeout, tries=2)
        res.append((i + 1, okc, ok, note))
        print("  [探测 #%d] 串口ok=%s  响应=%s (%s)" % (i + 1, okc, ok, note))
        if ok:
            break
    return res


# --------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(
        description="固定 FPS 连续通信 / MTBF 压测器",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("port", help="串口, 如 COM7(CH340 = 实验链路)")
    ap.add_argument("--fps", type=float, default=12.0, help="固定帧率(整数 Hz, 1~30)")
    ap.add_argument("--duration", type=float, default=600.0, help="最长测试时长(s)")
    ap.add_argument("--gap-timeout", type=float, default=3.0,
                    help="连续多少秒无遥测帧即判定断连")
    ap.add_argument("--keepalive-sec", type=float, default=0.0,
                    help="双向模式: 每 N 秒补发一条 RATE FIX(0 = 纯下行压力测试)")
    ap.add_argument("--timeout", type=float, default=1.0, help="单条命令超时(s)")
    ap.add_argument("--tag", default="", help="环境标签(默认取时间戳)")
    ap.add_argument("--out", default="", help="输出前缀(默认 mtbf_<tag>)")
    ap.add_argument("--report-every", type=float, default=5.0, help="控制台进度打印间隔(s)")
    ap.add_argument("--probe-count", type=int, default=0, help="断连后重连探测次数(0=不探测)")
    ap.add_argument("--probe-gap", type=float, default=10.0, help="重连探测间隔(s)")
    ap.add_argument("--append-matrix", default="", help="把本次结论追加到该总表 CSV")
    args = ap.parse_args()

    fps_i = int(round(args.fps))
    if fps_i < 1:
        fps_i = 1
    if fps_i > 30:
        fps_i = 30
    if abs(args.fps - fps_i) > 1e-6:
        print("! 固件按整数 Hz 处理, 已取整: %g -> %d" % (args.fps, fps_i))

    tag = args.tag or time.strftime("%Y%m%d-%H%M%S")
    prefix = args.out or ("mtbf_%s" % tag)
    csv_path = prefix + ".csv"
    sum_path = prefix + "_summary.txt"

    print("=" * 72)
    print("固定 FPS 连续通信 / MTBF 压测")
    print("  端口       : %s" % args.port)
    print("  固定帧率   : %d Hz" % fps_i)
    print("  最长时长   : %.0f s" % args.duration)
    print("  断连判定   : 连续 %.1f s 无帧" % args.gap_timeout)
    print("  双向心跳   : %s" % ("每 %.1f s 一条" % args.keepalive_sec
                                 if args.keepalive_sec > 0 else "关(纯下行)"))
    print("  标签       : %s" % tag)
    print("=" * 72)

    mc = MouseClient(auto_session=False, auto_stream=False)
    fl = FrameLog()
    uninstall = install_hook(mc, fl)

    if not connect(mc, args.port):
        print("✗ 连接失败: %s" % getattr(mc, "state_detail", ""))
        uninstall()
        return 2

    ok, tries, note = ensure_fix(mc, fps_i, args.timeout)
    print("RATE FIX %d -> %s (第 %d 次尝试, %s)" % (fps_i, "OK" if ok else "失败", tries, note))
    if not ok:
        print("✗ 无法进入固定节拍模式, 放弃")
        disconnect(mc)
        uninstall()
        return 3

    # ---- 启动自检: 用"实收帧率"核对是否真的进了固定节拍 ----
    # v2.6 起统一帧**不再**尾部广播目标帧率(该字段随 AIMD 一并删除), 所以改成观测实收:
    # 3 s 内一帧都没来、或实收明显低于目标 ⇒ 大概没进固定模式(旧固件会回 ERR_NAME,
    # 已由上面的 ensure_fix 先拦下, 这里兜的是"命令通了但没发帧"这类假象)。
    t_probe = time.monotonic()
    n0 = fl.total
    time.sleep(3.0)
    got = fl.total - n0
    obs = got / max(1e-6, time.monotonic() - t_probe)
    ok_rate = got > 0 and obs >= 0.5 * fps_i
    print("自检: 3s 实收 %d 帧 = %.2f Hz (目标 %d) -> %s"
          % (got, obs, fps_i, "OK" if ok_rate else "**偏低**"))
    if not ok_rate:
        print("! 实收明显低于目标: 可能未进入固定节拍(旧固件?)或链路已在丢帧 —— 结果仅供参考")

    # ---------------------------------------------------------- 主循环
    fl.reset()  # 存活统计从正式计时点开始(自检期的帧不计入)
    t0 = time.monotonic()
    wall0 = time.strftime("%Y-%m-%d %H:%M:%S")
    last_row = t0
    last_ka = t0
    last_report = t0
    dead_t = None
    end_note = ""
    max_gap_seen = 0.0

    fcsv = open(csv_path, "w", newline="", encoding="utf-8-sig")
    writer = csv.writer(fcsv)
    writer.writerow(["t_s", "frames", "fps_obs", "gap_max_ms", "total", "alive", "note"])
    fcsv.flush()

    def emit(t_s, frames, fps_obs, gap_max, alive, note):
        writer.writerow(["%.1f" % t_s, frames, "%.2f" % fps_obs,
                         "%.1f" % gap_max, fl.total, alive, note])
        fcsv.flush()

    try:
        while True:
            time.sleep(0.1)
            now = time.monotonic()
            el = now - t0

            # 可选双向心跳(用 RATE FIX 幂等重发: 既保活又能在复位后自建状态)
            if args.keepalive_sec > 0 and (now - last_ka) >= args.keepalive_sec:
                cmd(mc, "RATE FIX %d" % fps_i, args.timeout)
                last_ka = now

            # 每秒落一行
            if (now - last_row) >= 1.0:
                dt = now - last_row
                seg = fl.since(last_row)
                gap_max = 0.0
                if len(seg) >= 2:
                    gap_max = max(b - a for a, b in zip(seg, seg[1:])) * 1000.0
                if gap_max > max_gap_seen:
                    max_gap_seen = gap_max
                emit(now - t0, len(seg), (len(seg) / dt) if dt > 0 else 0.0, gap_max, 1, "")
                last_row = now

            # 判死: 超过 gap_timeout 无帧
            lt = fl.last_t
            gap = (now - lt) if lt is not None else (now - t0)
            if gap > args.gap_timeout:
                dead_t = (lt - t0) if lt is not None else el
                end_note = "DEAD: 连续 %.1fs 无帧" % gap
                # 判定行用**当前时刻**(保证 CSV 时间单调); 真实断连时刻(最后收帧)放 note。
                emit(el, 0, 0.0, gap * 1000.0, 0, "DEAD last_frame=%.1fs" % dead_t)
                break

            if (now - last_report) >= args.report_every:
                last_report = now
                live = len(fl.since(now - 1.0))
                print("  t=%7.1fs  存活  帧率 %5.2f Hz  累计 %6d 帧" % (el, live, fl.total))

            if el >= args.duration:
                end_note = "SURVIVED: 达到时长上限"
                break
    except KeyboardInterrupt:
        end_note = "ABORTED: Ctrl+C"
    finally:
        try:
            fcsv.close()
        except Exception:
            pass

    # ---------------------------------------------------------- 收尾
    alive_s = (dead_t if dead_t is not None else (time.monotonic() - t0))
    if dead_t is not None:
        result = "BROKE"
    elif "Ctrl+C" in end_note:
        result = "ABORTED"
    else:
        result = "SURVIVED"
    mean_fps = (fl.total / alive_s) if alive_s > 0 else 0.0

    probes = []
    if dead_t is not None and args.probe_count > 0:
        print("断连后重连探测 (每 %.0fs 一次, 最多 %d 次) ..." % (args.probe_gap, args.probe_count))
        probes = probe_recover(mc, args.port, args.probe_count, args.probe_gap,
                               fps_i, args.timeout)
    disconnect(mc)
    uninstall()

    # summary 文本
    lines = []
    lines.append("===== 固定 FPS 连续通信 / MTBF 测试报告 =====")
    lines.append("标签        : %s" % tag)
    lines.append("端口        : %s" % args.port)
    lines.append("固定帧率    : %d Hz" % fps_i)
    lines.append("开始墙钟    : %s" % wall0)
    lines.append("结果        : %s" % result)
    lines.append("断连时刻    : %s" % ("%.1f s" % dead_t if dead_t is not None else "-"))
    lines.append("存活时长    : %.1f s" % alive_s)
    lines.append("累计收帧    : %d" % fl.total)
    lines.append("平均观测帧率: %.2f Hz" % mean_fps)
    lines.append("最大帧间隔  : %.1f ms" % max_gap_seen)
    lines.append("结束说明    : %s" % end_note)
    if probes:
        lines.append("重连探测    : %d 次, %s"
                     % (len(probes), "已自愈" if probes[-1][2] else "全部无响应(仍需断电)"))
    else:
        lines.append("重连探测    : 未做")
    lines.append("逐秒数据    : %s" % csv_path)
    report = "\n".join(lines)
    with open(sum_path, "w", encoding="utf-8") as f:
        f.write(report + "\n")

    if args.append_matrix:
        row = {
            "tag": tag, "started_at": wall0, "port": args.port, "fps_hz": fps_i,
            "duration_limit_s": "%.0f" % args.duration,
            "keepalive_s": "%.0f" % args.keepalive_sec, "result": result,
            "t_fail_s": ("%.1f" % dead_t) if dead_t is not None else "",
            "alive_s": "%.1f" % alive_s, "total_frames": fl.total,
            "mean_fps_obs": "%.2f" % mean_fps, "max_gap_ms": "%.1f" % max_gap_seen,
        }
        new_file = not os.path.exists(args.append_matrix)
        with open(args.append_matrix, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=MATRIX_HEADER)
            if new_file:
                w.writeheader()
            w.writerow(row)
        print("总表已追加: %s" % args.append_matrix)

    print()
    print(report)
    return 0 if result == "SURVIVED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
