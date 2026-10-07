"""接线验证: 电脑 -> COM7 发数据, 读 STM32 的 USART1 接收缓冲, 判断 RX 通路是否存在。

背景: J-Link 已证明 STM32 的 PA9 上有真实数据波形, 但 CH340(COM7) 收不到。
      本脚本从反方向验证 —— 若电脑发出的字节能出现在 STM32 的接收缓冲里,
      说明 CH340 与 USART1 之间确实有连线(只是某个方向/引脚不对); 若缓冲始终为空,
      则 CH340 根本没接到 USART1(PA9/PA10)。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time

import serial

JL = r"C:\Program Files\SEGGER\JLink_V976a\JLink.exe"
PORT = "COM7"

RXBUF = 0x20000BD8   # USART_RX_BUF (64B)
RXSTA = 0x2000001C   # USART_RX_STA
STATUS = 0x2000387C  # linkLastStatus (14B)

SCRIPT = (
    "si SWD\n"
    "speed 4000\n"
    "device STM32F103RB\n"
    "connect\n"
    "halt\n"
    f"mem8 0x{RXBUF:08X}, 64\n"
    f"mem8 0x{RXSTA:08X}, 4\n"
    f"mem8 0x{STATUS:08X}, 16\n"
    "q\n"
)


def read_state() -> str:
    fd, path = tempfile.mkstemp(suffix=".jlink", text=True)
    os.close(fd)
    with open(path, "w", encoding="ascii") as f:
        f.write(SCRIPT)
    try:
        p = subprocess.run([JL, "-CommanderScript", path, "-ExitOnError", "1"],
                           capture_output=True, text=True, timeout=120,
                           encoding="utf-8", errors="replace")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return p.stdout or ""


def parse(out: str) -> dict:
    """把 J-Link 的 mem8 输出按地址聚合成 bytes。"""
    acc: dict = {}
    for line in out.splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        a, _, rest = line.partition("=")
        a = a.strip()
        if len(a) != 8:
            continue
        try:
            base = int(a, 16)
        except ValueError:
            continue
        if base < 0x20000000 or base > 0x2000FFFF:
            continue
        try:
            vals = [int(x, 16) for x in rest.split()]
        except ValueError:
            continue
        for i, v in enumerate(vals):
            acc[base + i] = v
    return acc


def region(acc: dict, base: int, n: int) -> bytes:
    return bytes(acc.get(base + i, 0) for i in range(n))


def show(tag: str, acc: dict) -> None:
    buf = region(acc, RXBUF, 64)
    sta = region(acc, RXSTA, 1)
    st = region(acc, STATUS, 14).split(b"\x00")[0]
    print(f"  [{tag}] RX_STA=0x{sta[0]:02X}  linkLastStatus={st!r}")
    nonz = buf.rstrip(b"\x00")
    print(f"        RX_BUF(非零部分)={nonz!r}")


def main() -> int:
    print("=" * 74)
    print("接线验证: 电脑 -> COM7 -> STM32 USART1")
    print("=" * 74)

    before = parse(read_state())
    print("\n发送前:")
    show("before", before)

    try:
        s = serial.Serial(PORT, 115200, bytesize=8, parity="N", stopbits=1, timeout=0.2)
    except Exception as e:
        print(f"\n打开 {PORT} 失败: {e}")
        return 1
    try:
        s.reset_input_buffer()
        s.reset_output_buffer()
    except Exception:
        pass
    s.write(b"PING\r\n")
    s.flush()
    time.sleep(0.8)
    s.close()
    print(f"\n已向 {PORT} 发送 b'PING\\r\\n'")

    after = parse(read_state())
    print("\n发送后:")
    show("after", after)

    buf_after = region(after, RXBUF, 64)
    st_after = region(after, STATUS, 14).split(b"\x00")[0]

    print("\n" + "-" * 74)
    if b"PING" in buf_after or b"I" in buf_after[:8]:
        print("结论: STM32 收到了电脑发出的字节 -> CH340 与 USART1 之间【有】连线")
        if st_after:
            print(f"      固件已处理该行, linkLastStatus={st_after!r}")
    elif st_after != region(before, STATUS, 14).split(b"\x00")[0]:
        print("结论: linkLastStatus 变化 -> STM32 收到了数据")
    else:
        print("结论: STM32 接收缓冲始终为空 -> CH340 根本没有连到 STM32 的 USART1")
        print("      (PA9/PA10 上接的是蓝牙模块; CH340 要么未接, 要么接在别的 UART 上,")
        print("       要么板上有跳线/0欧电阻选择, 当前选的是蓝牙侧)")
        print("\n      注: 单项验证 —— 已知 STM32 的 PA9 在持续发送数据(J-Link 实测到电平跳变),")
        print("          所以 TX 方向是好的, 缺的是 CH340 到 PA9/PA10 的连接。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
