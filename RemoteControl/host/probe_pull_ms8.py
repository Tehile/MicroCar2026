# -*- coding: utf-8 -*-
"""两件事一次说清: (1) 帧内 ms8 是不是可信的 1ms 时钟 (2) 固件 PULL 的批内间隔是否 = t。

为什么必须做
------------
* 主机侧"到达时刻"是**读取线程解析到帧的时刻**, 一次 read() 可能同时吐出好几帧
  (CH340/USB 延迟定时器攒批) ⇒ 两帧会被打上几乎相同的时间戳(实测 0.08ms),
  但 57B/帧 在 115200 上就要 4.9ms —— **物理上不可能**。故主机到达时刻不能用来
  判定批内间隔, 只能用来算速率。
* 帧内 `ms8`(载荷 0, 单片机毫秒时基低 8 位)才能反映单片机**发帧时刻**。
  先用已知间隔(n=1, 每 1000ms 一条 PULL)标定它, 再拿它量批内间隔。

用法
----
    python probe_pull_ms8.py COM7
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
        self.frames = []
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
                self.frames.append((time.monotonic(), b[i + 5], pl[0] if pl else None, ok))
                self.buf = b[total:]

    def take(self):
        with self.lock:
            out, self.frames = self.frames, []
            return out


def dms8(a, b):
    """ms8 差(带符号, 假设真间隔 < 256ms)。"""
    return ((b - a) + 128) % 256 - 128


def main() -> int:
    ap = argparse.ArgumentParser(description="ms8 时钟标定 + PULL 批内间隔量测")
    ap.add_argument("port")
    ap.add_argument("--baud", type=int, default=115200)
    a = ap.parse_args()

    ser = serial.Serial(a.port, a.baud, timeout=0.05)
    time.sleep(0.3)
    tap = Tap(ser)

    def send(cmd, seq):
        body = ("%s #%d" % (cmd, seq)).encode()
        ser.write(body + ("*%04X" % crc16_modbus(body)).encode() + b"\r\n")

    send("PING", 1)
    time.sleep(0.4)

    print("=" * 74)
    print("① ms8 时钟标定: n=1, 每 1000ms 发一条 PULL ⇒ 帧间应为 1000ms(ms8: 1000 mod 256 = -24)")
    print("-" * 74)
    tap.take()
    prev_ms = prev_t = None
    for k in range(8):
        send("RATE PULL 1 1000", 10 + k)
        time.sleep(1.0)
    time.sleep(1.0)
    got = tap.take()
    for (t, seq, ms8, ok) in got:
        da = ("%+.0f" % ((t - prev_t) * 1000)) if prev_t else "-"
        dm = ("%+d" % dms8(prev_ms, ms8)) if prev_ms is not None else "-"
        print("   seq=%-3d ms8=%-3d  Δ到达=%-8s Δms8=%-6s %s" % (seq, ms8, da, dm, "OK" if ok else "CRC✗"))
        prev_t, prev_ms = t, ms8
    print("")

    print("=" * 74)
    print("② 批内间隔: 单发 `RATE PULL n t`, 看两帧的 Δms8 是否 = t")
    print("-" * 74)
    seq = 100
    for (n, t) in [(2, 83), (2, 100), (2, 200), (2, 400), (4, 100), (4, 250)]:
        tap.take()
        seq += 1
        send("RATE PULL %d %d" % (n, t), seq)
        time.sleep(0.3 + n * t / 1000.0 + 0.6)
        got = tap.take()
        chain, prev = [], None
        for (tt, sq, ms8, ok) in got:
            if prev is not None:
                chain.append(dms8(prev, ms8))
            prev = ms8
        arr = [(got[i][0] - got[i - 1][0]) * 1000 for i in range(1, len(got))]
        print("   n=%d t=%-4d 收到 %d 帧 | Δms8(单片机) %-22s | Δ到达(主机) %-24s"
              % (n, t, len(got), str(chain), str(["%.1f" % x for x in arr])))
    print("")

    send("RATE OFF", 900)
    time.sleep(0.2)
    tap.stop = True
    ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
