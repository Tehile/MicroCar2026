# -*- coding: utf-8 -*-
"""固件 `PULL <n> <t_ms>` 定时语义验证: 批内帧真的按 t 间隔发吗?

发现的问题
----------
`link_pull_pace.py` 逐帧数据显示: 每批 2 帧**背靠背**到达(gap≈0.08ms), 然后静默
~150ms —— 与"帧内间隔 t=83ms"矛盾。也可能是**链路把帧攒起来一起放**(蓝牙/CH340
缓冲), 两种解释必须分开: 靠帧内 `ms8`(单片机毫秒时基低 8 位)判定 ——
   * ms8 差 ≈ 0     -> 单片机就是背靠背发的(固件定时逻辑问题)
   * ms8 差 ≈ t     -> 单片机按时发, 是**链路中段把帧攒批**(重要链路结论)

用法
----
    python probe_pull_timing.py COM7 --n 2 --t 500 --reps 3
    python probe_pull_timing.py COM7 --n 2 --t 83  --reps 3
只发 PING / RATE PULL / RATE OFF。
"""
from __future__ import annotations

import argparse
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
        self.th = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.th.start()
        return self

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
                self.frames.append((time.monotonic(), b[i + 5], pl[0],
                                    pl[44] if plen > 44 else None, ok, plen))
                self.buf = b[total:]

    def take(self):
        with self.lock:
            out, self.frames = self.frames, []
            return out


def main() -> int:
    ap = argparse.ArgumentParser(description="验证 PULL 批内间隔")
    ap.add_argument("port")
    ap.add_argument("--n", type=int, default=2)
    ap.add_argument("--t", type=int, default=500)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--wait", type=float, default=None, help="每轮观察窗(s, 默认 2.5t)")
    a = ap.parse_args()

    wait = a.wait if a.wait else max(1.5, a.t / 1000.0 * 2.5 + 0.5)
    ser = serial.Serial(a.port, 115200, timeout=0.05)
    time.sleep(0.3)
    tap = Tap(ser).start()

    def send(cmd, seq):
        body = ("%s #%d" % (cmd, seq)).encode()
        ser.write(body + ("*%04X" % crc16_modbus(body)).encode() + b"\r\n")

    # 握手(命令可能丢, 重试到出现任意帧/ACK)
    send("PING", 900)
    time.sleep(0.5)

    for rep in range(1, a.reps + 1):
        tap.take()
        t0 = time.monotonic()
        send("RATE PULL %d %d" % (a.n, a.t), 100 + rep)
        time.sleep(wait)
        got = tap.take()
        print("第 %d 轮 单发 `RATE PULL %d %d`, %.1fs 内收到 %d 帧(期望 %d):"
              % (rep, a.n, a.t, wait, len(got), a.n))
        prev_t = prev_ms = None
        for (t, seq, ms8, drop, ok, plen) in got:
            d_arr = ("%+.0f" % ((t - prev_t) * 1000)) if prev_t else "-"
            d_ms = ("%+d" % (((ms8 - prev_ms) + 128) % 256 - 128)) if prev_ms is not None else "-"
            print("   +%7.1f ms  seq=%-3d ms8=%-3d Δ到达=%-8s Δms8=%-8s drop=%s len=%d %s"
                  % ((t - t0) * 1000, seq, ms8, d_arr, d_ms, drop, plen,
                     "OK" if ok else "CRC✗"))
            prev_t, prev_ms = t, ms8
        if len(got) >= 2:
            arr = [(got[i][0] - got[i - 1][0]) * 1000 for i in range(1, len(got))]
            msd = [((got[i][2] - got[i - 1][2]) + 128) % 256 - 128 for i in range(1, len(got))]
            print("   => 批内到达间隔 %s ms | 单片机自报(ms8)间隔 %s ms | 设定 t=%d ms"
                  % (["%.1f" % x for x in arr], ["%d" % x for x in msd], a.t))
        print("")
        time.sleep(0.3)

    send("RATE OFF", 800)
    time.sleep(0.2)
    tap.stop = True
    ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
