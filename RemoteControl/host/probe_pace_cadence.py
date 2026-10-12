# -*- coding: utf-8 -*-
"""固件在"周期反复下发 PULL"下的真实发帧节拍(单片机 ms8 为准)。

动机
----
`link_pull_pace.py` 跑 12Hz(n=2,t=83) 时, 单片机侧间隔出现 **83/67 交替**;
但"单发一条 PULL"时是精确 83。差别只在于**命令是不是周期性地来**。
本脚本用最笨的办法复现: 只按固定周期发 `RATE PULL n t`, 不等待任何帧,
逐帧记录 ms8(单片机毫秒时基低 8 位)与主机到达时刻, 再统计
  * 每条命令实际换来几帧
  * 单片机侧相邻帧间隔的直方图(是否均匀)
  * 主机侧到达(受 CH340/USB 攒批污染, 仅作对照)

用法
----
    python probe_pace_cadence.py COM7 --n 2 --t 83  --period-ms 166 --duration 20
    python probe_pace_cadence.py COM7 --n 3 --t 83  --period-ms 249 --duration 20
    python probe_pace_cadence.py COM7 --n 6 --t 83  --period-ms 498 --duration 20
只发 PING / RATE PULL / RATE OFF。
"""
from __future__ import annotations

import argparse
import collections
import statistics
import struct
import sys
import threading
import time

import serial

from host.protocol.codec import crc16_modbus


class Tap:
    def __init__(self, ser):
        self.ser = ser
        self.buf = b""
        self.lock = threading.Lock()
        self.frames: list[tuple] = []
        self.stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self.stop:
            try:
                c = self.ser.read(4096)
            except Exception:
                return
            if not c:
                continue
            with self.lock:
                self.buf += c
            self._parse()

    def _parse(self):
        while True:
            with self.lock:
                b = self.buf
            i = b.find(b"\xaa\x55")
            if i < 0 or len(b) < i + 8:
                return
            plen = b[i + 2] | (b[i + 3] << 8)
            total = i + 6 + plen + 2
            if len(b) < total:
                return
            pl = b[i + 6:i + 6 + plen]
            ok = crc16_modbus(b[i + 4:i + 6 + plen]) == struct.unpack_from("<H", b, i + 6 + plen)[0]
            with self.lock:
                self.frames.append((time.monotonic(), b[i + 5], pl[0] if pl else None,
                                    pl[44] if len(pl) > 44 else None, ok))
                self.buf = b[total:]

    def take(self):
        with self.lock:
            out, self.frames = self.frames, []
            return out


def main() -> int:
    ap = argparse.ArgumentParser(description="周期 PULL 驱动下的真实发帧节拍")
    ap.add_argument("port")
    ap.add_argument("--n", type=int, default=2)
    ap.add_argument("--t", type=int, default=83)
    ap.add_argument("--period-ms", type=float, default=None, help="命令周期(默认 n*t)")
    ap.add_argument("--duration", type=float, default=20.0)
    ap.add_argument("--fix", type=float, default=None,
                    help="改用自主均匀源 `RATE FIX <hz>`(单条命令, 之后不再发命令)")
    a = ap.parse_args()

    period = (a.period_ms if a.period_ms else a.n * a.t) / 1000.0
    ser = serial.Serial(a.port, 115200, timeout=0.05)
    time.sleep(0.3)
    tap = Tap(ser)
    seq = 0

    def send(cmd=None, s=None):
        nonlocal seq
        seq = (seq + 1) & 0xFFFF
        c = cmd or ("RATE PULL %d %d" % (a.n, a.t))
        body = ("%s #%d" % (c, s if s is not None else seq)).encode()
        t = time.monotonic()
        ser.write(body + ("*%04X" % crc16_modbus(body)).encode() + b"\r\n")
        return t

    send("PING", 900)          # 握手(顺带把可能残留的 PULL 打断)
    time.sleep(0.5)
    tap.take()

    sends: list[float] = []
    if a.fix:
        print("自主均匀源 `RATE FIX %g`(单条命令, 之后不再发命令), 共 %.0fs; 期望间隔 %.1f ms"
              % (a.fix, a.duration, 1000.0 / a.fix))
        sends.append(send("RATE FIX %g" % a.fix, 901))
        time.sleep(a.duration)
    else:
        print("按周期 %.0f ms 反复下发 `RATE PULL %d %d`(期望每周期 %d 帧, 帧内间隔 %d ms), 共 %.0fs"
              % (period * 1000, a.n, a.t, a.n, a.t, a.duration))
        t0 = time.monotonic()
        k = 0
        while True:
            target = t0 + k * period
            now = time.monotonic()
            if now - t0 >= a.duration:
                break
            if now < target:
                time.sleep(min(0.004, target - now))
                continue
            sends.append(send())
            k += 1
        time.sleep(0.8)          # 收尾: 让最后一批发完
    got = tap.take()
    tap.stop = True
    send("RATE OFF", 999)
    ser.close()

    # ---- 统计
    print("命令 %d 条, 收到帧 %d 帧 => 平均每命令 %.2f 帧(期望 %d)"
          % (len(sends), len(got), len(got) / max(1, len(sends)), a.n))
    if len(sends) > 1:
        sd = [(sends[i] - sends[i - 1]) * 1000 for i in range(1, len(sends))]
        print("命令周期实测: 均值 %.1f ms σ %.1f ms" % (statistics.fmean(sd), statistics.pstdev(sd)))

    # 解开 ms8 的 256 整数倍:
    #  * 帧间隔 < 250ms 的场合(FIX / 高频 PULL)直接取 raw, 不做任何主机侧参考 ——
    #    这样"单片机时钟 vs 墙钟"的比较才有独立性
    #  * 帧间隔可能 > 256ms 的场合(低频 PULL)才用主机到达差作粗参考
    use_raw = bool(a.fix) or period <= 0.25
    dms, dhost = [], []
    for i in range(1, len(got)):
        raw = (got[i][2] - got[i - 1][2]) & 0xFF
        dh = (got[i][0] - got[i - 1][0]) * 1000
        if use_raw or dh < 20:
            dm = raw
        else:
            cand = [raw + 256 * k for k in range(-3, 4)]
            dm = min(cand, key=lambda v: abs(v - dh))
        dms.append(dm)
        dhost.append(dh)

    if dms:
        c = collections.Counter([int(round(x)) for x in dms])
        print("")
        print("单片机侧帧间隔(ms8) 直方图(前 10): %s" % c.most_common(10))
        print("   均值 %.1f ms  σ %.1f ms  min %d  max %d  (期望 %d ms => %.2f Hz)"
              % (statistics.fmean(dms), statistics.pstdev(dms), min(dms), max(dms),
                 a.t, 1000.0 / statistics.fmean(dms)))
        near = sum(1 for x in dms if abs(x - a.t) <= max(5, 0.1 * a.t))
        print("   落在 t±10%% 内的比例: %.1f%%" % (100.0 * near / len(dms)))
        print("主机侧帧间隔: 均值 %.1f ms σ %.1f ms(含 USB 攒批, 仅对照)"
              % (statistics.fmean(dhost), statistics.pstdev(dhost)))
        span_m, span_h = sum(dms), sum(dhost)
        print("★时钟比对: 单片机 ms8 累计推进 %.0f ms  vs  墙钟累计 %.0f ms  => 比值 %.4f"
              % (span_m, span_h, span_m / span_h if span_h else float("nan")))
        print("")
        print("前 24 帧(idx, 主机t, ms8, Δms8, Δhost):")
        prev = None
        for i, (th, sq, m8, dr, ok) in enumerate(got[:24]):
            if prev is None:
                print("   %-3d %8.3f ms8=%-4d" % (i + 1, th - t0 if 't0' in dir() else th, m8))
            else:
                print("   %-3d %8.3f ms8=%-4d Δms8=%-5s Δhost=%.1f" % (
                    i + 1, th - t0, m8,
                    ((m8 - prev) & 0xFF), (th - got[i - 1][0]) * 1000))
            prev = m8
    drops = [g[3] for g in got if g[3]]
    print("")
    print("MCU 丢帧字节: 末值 %s, 非零帧数 %d" % (drops[-1] if drops else 0, len(drops)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
