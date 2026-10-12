# -*- coding: utf-8 -*-
"""定位探针: PULL 模式下 payload[44] 到底是什么(边发拉取请求边收)。

背景
----
`LINK_U_DROP`(载荷偏移 44)= 固件 TX 环满累计丢帧低 8 位。此前 FIX 档(24/30Hz)实测**
恒定 0**(见 drop24.csv / drop30.csv)。但 `pull_pull-fix83.csv` 里该字节**逐帧 +1**,
末值恰好等于实收帧数 —— 与"丢帧计数"语义矛盾, 必须实测判定。

判定依据(三条同时看)
--------------------
  * `p44 == 帧序数`        且 FIX 档为 0 -> 该字节更像是**帧计数**而非丢帧计数
  * `p44` 台阶式增长(增量>1)          -> 真是丢帧计数(有整帧被 TX 环满丢掉)
  * 每帧 `ms8`(载荷 0) 与到达间隔     -> 帧内时基是否单调, 用于排除"重复帧"

用法
----
    python probe_u44.py COM7 --pull-n 1 --pull-t 200 --duration 6
    python probe_u44.py COM7 --pull-n 2 --pull-t 83  --duration 6
只发 RATE PING/PULL/OFF(纯遥测, **不含任何运动命令**)。
"""
from __future__ import annotations

import argparse
import struct
import sys
import threading
import time

import serial

from host.protocol.codec import crc16_modbus

MASK = 0xFFFFFFFF


class Reader(threading.Thread):
    """后台收帧线程: 解析 AA55 帧, 记录每帧的载荷特征。"""

    def __init__(self, ser, on_frame):
        super().__init__(daemon=True)
        self.ser = ser
        self.on_frame = on_frame
        self.buf = b""
        self.bytes = 0
        self.stop = False

    def run(self):
        while not self.stop:
            try:
                chunk = self.ser.read(4096)
            except Exception:
                return
            if not chunk:
                continue
            self.bytes += len(chunk)
            self.buf += chunk
            while True:
                i = self.buf.find(b"\xaa\x55")
                if i < 0:
                    self.buf = self.buf[-1:]
                    break
                if len(self.buf) < i + 6:
                    break
                plen = self.buf[i + 2] | (self.buf[i + 3] << 8)
                total = i + 6 + plen + 2
                if len(self.buf) < total:
                    break
                payload = self.buf[i + 6:i + 6 + plen]
                crc_rx = struct.unpack_from("<H", self.buf, i + 6 + plen)[0]
                body = self.buf[i + 4:i + 6 + plen]
                seq = self.buf[i + 5]
                self.buf = self.buf[total:]
                self.on_frame(seq, payload, crc16_modbus(body) == crc_rx)


def try_send(ser, text: str, tries: int = 6, wait_ack: bool = True) -> bool:
    """发一条命令并等 ACK(连接后首条命令常丢, 故重试)。"""
    for _ in range(tries):
        ser.write(text.encode() + b"\r\n")
        if not wait_ack:
            return True
        deadline = time.monotonic() + 0.6
        buf = b""
        while time.monotonic() < deadline:
            buf += ser.read(256)
            if b"ACK" in buf:
                return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="探针: PULL 模式下 payload[44] 的语义")
    ap.add_argument("port")
    ap.add_argument("--pull-n", type=int, default=2)
    ap.add_argument("--pull-t", type=int, default=200)
    ap.add_argument("--send-every-ms", type=float, default=None,
                    help="拉取请求间隔(默认 = --pull-t)")
    ap.add_argument("--duration", type=float, default=8.0)
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--quiet", action="store_true", help="只打印汇总")
    a = ap.parse_args()

    send_every = a.send_every_ms if a.send_every_ms else float(a.pull_t)

    ser = serial.Serial(a.port, a.baud, timeout=0.05)
    time.sleep(0.3)
    if not try_send(ser, "PING"):
        print("✗ 链路无响应")
        ser.close()
        return 2

    rows: list[tuple] = []
    lock = threading.Lock()

    def on_frame(seq, payload, ok):
        with lock:
            rows.append((
                len(rows) + 1,
                time.monotonic(),
                seq,
                payload[0] if payload else None,
                payload[43] if len(payload) > 43 else None,
                payload[44] if len(payload) > 44 else None,
                ok))

    rd = Reader(ser, on_frame)
    rd.start()

    cmd = "RATE PULL %d %d" % (a.pull_n, a.pull_t)
    print("模式: %s, 每 %.0f ms 重发一次, 持续 %.0fs" % (cmd, send_every, a.duration))
    if not a.quiet:
        print("")
        print("%6s %8s %5s %6s %5s %5s %6s" % (
            "序号", "到达t(s)", "seq", "ms8", "p43", "p44", "Δ44"))
        print("-" * 62)

    t0 = time.monotonic()
    printed = 0
    prev44 = None
    seq_sent = 0
    try:
        while True:
            now = time.monotonic()
            if now - t0 >= a.duration:
                break
            # 与上位机同格式: `CMD #seq*CRC\r\n`(`RATE` 在固件里被提前拦截, 不写幂等簿记)
            seq_sent = (seq_sent + 1) & 0xFFFF
            body = ("%s #%d" % (cmd, seq_sent)).encode()
            ser.write(body + ("*%04X" % crc16_modbus(body)).encode() + b"\r\n")
            time.sleep(max(0.005, send_every / 1000.0))

        time.sleep(1.2)  # 收尾: 让在途帧到齐
    except KeyboardInterrupt:
        pass
    finally:
        rd.stop = True
        time.sleep(0.1)
        try_send(ser, "RATE OFF", wait_ack=False)
        ser.close()

    with lock:
        data = list(rows)
    if not a.quiet:
        for (idx, t, seq, ms8, p43, p44, ok) in data:
            d = "" if prev44 is None else ((p44 - prev44) & 0xFF)
            prev44 = p44
            print("%6d %8.2f %5d %6s %5s %5s %6s   %s%s" % (
                idx, t - t0, seq, ms8, p43, p44, d,
                "" if ok else "CRC✗ ",
                "p44==idx" if p44 == (idx & 0xFF) else ""))

    print("")
    print("=" * 62)
    print("收字节 %d  解析帧 %d  窗口 %.1fs  => 实收 %.2f Hz"
          % (rd.bytes, len(data), a.duration, len(data) / a.duration))
    print("拉取请求(每批 %d 帧) 下发 %d 次 => 名义 %.2f Hz"
          % (a.pull_n, int(a.duration * 1000.0 / send_every),
             int(a.duration * 1000.0 / send_every) * a.pull_n / a.duration))
    if data:
        last = data[-1]
        print("末帧: 序号 %d, p44=%s, ms8=%s" % (last[0], last[5], last[3]))
        eq = sum(1 for r in data if r[5] == (r[0] & 0xFF))
        print("p44 == 帧序数(mod 256) 的比例: %d/%d" % (eq, len(data)))
        deltas = [((data[i][5] - data[i - 1][5]) & 0xFF) for i in range(1, len(data))]
        from collections import Counter
        print("Δ44 分布(前 8):", Counter(deltas).most_common(8))
        print("Δt44 单位=帧: 平均 Δ44 = %.3f / 帧" % (sum(deltas) / max(1, len(deltas))))
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
