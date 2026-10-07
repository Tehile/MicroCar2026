"""最小原始探活：直接开串口发 PING, 看板上回什么。

用途：当门面层(mc.send_cmd)报 PING 失败时, 用来区分
  - 链路/端口问题（连原文都读不到）
  - 上位机解析问题（原文有但没被识别）
不发任何运动命令。

用法:  python raw_ping.py COM7 [次数]
"""
from __future__ import annotations

import sys
import time

import serial


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def build(cmd: str, seq: int = 1) -> bytes:
    body = f"{cmd} #{seq}".encode("ascii")
    # 格式必须与 host/protocol/commands.CommandClient._build 完全一致: "*" 前**没有空格**
    return body + f"*{crc16(body):04X}".encode("ascii") + b"\r\n"


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "COM7"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    print(f"原始探活 {port}  (只发 PING/TELEM, 不发运动命令)")
    try:
        s = serial.Serial(port, 115200, timeout=1.0)
    except Exception as e:
        print(f"  打不开端口: {type(e).__name__}: {e}")
        return 1
    print(f"  已打开 {s.name} @115200-8-N-1, 清输入缓冲")

    with s:
        s.reset_input_buffer()
        for i in range(n):
            line = build("PING", i + 1)
            s.reset_input_buffer()
            s.write(line)
            s.flush()
            t0 = time.time()
            buf = b""
            while time.time() - t0 < 2.0:
                chunk = s.read(256)
                if chunk:
                    buf += chunk
                if b"\r\n" in buf and len(buf) > 3:
                    break
            dt = (time.time() - t0) * 1000
            tx = line.decode("ascii").strip()
            print(f"  [{i+1}] -> {tx}")
            print(f"      <- {buf!r}   ({dt:.0f} ms, {len(buf)} B)")
            time.sleep(0.3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
