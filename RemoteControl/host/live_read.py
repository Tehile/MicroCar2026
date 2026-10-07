"""实时读取串口内容: 打印原始字节 + 可读文本。

用法: python live_read.py [COM7] [115200] [秒数]
"""

from __future__ import annotations

import sys
import time

import serial


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "COM7"
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else 115200
    dur = float(sys.argv[3]) if len(sys.argv) > 3 else 5.0

    try:
        s = serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1, timeout=0.2)
    except Exception as e:
        print(f"打开 {port} 失败: {e}")
        return 1

    print("=" * 74)
    print(f"读取 {port} @ {baud}, 持续 {dur} 秒")
    print("=" * 74)

    got = bytearray()
    marks = []
    t0 = time.time()
    try:
        while time.time() - t0 < dur:
            d = s.read(4096)
            if d:
                got += d
                marks.append((time.time() - t0, len(d)))
    finally:
        s.close()

    print(f"\n共收到 {len(got)} 字节, {len(marks)} 次到达")
    if marks:
        for t, n in marks[:10]:
            print(f"  t={t:6.3f}s  +{n} 字节")
        if len(marks) > 10:
            print(f"  ... 共 {len(marks)} 次")
    if not got:
        print("\n(无数据)")
        return 0

    print("\n--- 前 256 字节 ---")
    print("hex :", bytes(got[:256]).hex(" "))
    print("repr:", bytes(got[:256]))

    print("\n--- 按行展开(前 4000 字节) ---")
    txt = bytes(got[:4000]).decode("utf-8", errors="replace")
    for i, line in enumerate(txt.splitlines()[:60]):
        print(f"  {i:3d}| {line}")
    if len(txt.splitlines()) > 60:
        print(f"  ... 共 {len(txt.splitlines())} 行")

    # 统计
    print("\n--- 统计 ---")
    print(f"  换行数(0x0A) = {got.count(bytes([0x0A]))}")
    print(f"  0xAA 出现 {got.count(bytes([0xAA]))} 次 (遥测帧同步头; 原版例程应为 0)")
    printable = sum(1 for b in got if 9 <= b <= 13 or 32 <= b <= 126)
    print(f"  可打印字符占比 = {printable / len(got) * 100:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
