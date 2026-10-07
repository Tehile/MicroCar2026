"""决定性实验: 绕过固件逻辑, 用 J-Link 直接把字节写进 USART1->DR,
看 COM7(CH340) 能否收到。用于二分"固件问题" vs "物理链路问题"。

同时读出 PA9/PA10 的 GPIO 配置与 USART 寄存器。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time

import serial

JL = r"C:\Program Files\SEGGER\JLink_V976a\JLink.exe"
PORT = "COM7"

SCRIPT = """si SWD
speed 4000
device STM32F103RB
connect
halt
mem32 0x40010804, 2
mem32 0x4001080C, 1
mem32 0x40013800, 2
w4 0x40013804, 0x50
sleep 6
w4 0x40013804, 0x49
sleep 6
w4 0x40013804, 0x4E
sleep 6
w4 0x40013804, 0x47
sleep 6
w4 0x40013804, 0x0D
sleep 6
w4 0x40013804, 0x0A
sleep 6
mem32 0x40013800, 2
q
"""


def main() -> int:
    fd, path = tempfile.mkstemp(suffix=".jlink", text=True)
    os.close(fd)
    with open(path, "w", encoding="ascii") as f:
        f.write(SCRIPT)

    try:
        s = serial.Serial(PORT, 115200, bytesize=8, parity="N", stopbits=1, timeout=0.2)
    except Exception as e:
        print(f"打开 {PORT} 失败: {e}")
        return 1

    try:
        s.reset_input_buffer()
    except Exception:
        pass
    print(f"{PORT} 已打开, 缓冲已清空。启动 J-Link 写 DR ...")

    p = subprocess.run([JL, "-CommanderScript", path, "-ExitOnError", "1"],
                       capture_output=True, text=True, timeout=180,
                       encoding="utf-8", errors="replace")

    out = p.stdout or ""
    print("\n--- J-Link 关键输出 ---")
    for line in out.splitlines():
        ls = line.strip()
        if ls.startswith(("4001080", "400108", "4001380", "J-Link>mem32", "w4",
                          "2000", "E000")) or "=" in ls and ls.startswith("400"):
            print("  " + line)

    got = bytearray()
    t0 = time.time()
    while time.time() - t0 < 3.0:
        d = s.read(4096)
        if d:
            got += d
    s.close()

    print("\n--- 实验结果 ---")
    print(f"  串口收到 {len(got)} 字节")
    if got:
        print(f"  hex : {bytes(got[:64]).hex(' ')}")
        print(f"  repr: {bytes(got[:64])!r}")
        print("  => 物理链路通! 问题在固件的发送路径(链路逻辑)")
    else:
        print("  => 物理链路不通。J-Link 直接写 DR 都收不到,")
        print("     说明 CH340 与 STM32 的 USART1 之间没有通路(接线/引脚/串口对象不对)")

    try:
        os.unlink(path)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
