"""扫描串口的所有常见波特率, 找出能收到数据的那个。

为什么需要: 蓝牙透传模块的 UART 侧速率是用 AT 指令设定的, 出厂常见 9600。
PC 端必须按【电脑侧那一个模块】的 UART 速率来读 —— 这与 STM32 那一端的速率无关
(两端模块各自完成速率转换)。所以猜错速率时 PC 只会收到乱码甚至收不到。

判据:
  收到可解析的遥测帧(0xAA 0x55 同步头 + CRC 通过) -> 就是它
  只收到乱码                                      -> 速率不对(但说明有数据在流!)
  完全 0 字节                                     -> 链路没建立, 与速率无关

用法: python scan_baud.py [COM7]
"""

from __future__ import annotations

import sys
import time

import serial

from host.protocol.codec import FrameStreamParser

BAUDS = [115200, 9600, 38400, 57600, 19200, 230400, 4800, 1200]
READ_S = 1.0


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "COM7"
    print("=" * 74)
    print(f"波特率扫描 @ {port}  (每个试 {READ_S:.1f}s)")
    print("=" * 74)
    print(f"{'波特率':>8} {'字节数':>8} {'帧数':>6} {'文本行':>7} {'坏CRC':>7}   样例")
    print("-" * 74)

    best = None
    for baud in BAUDS:
        try:
            s = serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1, timeout=0.15)
        except Exception as e:
            print(f"{baud:>8}   打开失败: {e}")
            continue
        try:
            time.sleep(0.15)
            try:
                s.reset_input_buffer()
            except Exception:
                pass
            got = bytearray()
            t0 = time.time()
            while time.time() - t0 < READ_S:
                d = s.read(4096)
                if d:
                    got += d
        finally:
            s.close()

        p = FrameStreamParser()
        frames, lines = ([], [])
        if got:
            try:
                from host.protocol.codec import split_items
                frames, lines = split_items(p.feed(bytes(got)))
            except Exception:
                pass
        sample = bytes(got[:24])
        print(f"{baud:>8} {len(got):>8} {len(frames):>6} {len(lines):>7} "
              f"{p.stats['bad_crc']:>7}   {sample!r}")
        if frames and best is None:
            best = (baud, len(got), len(frames))
        elif got and best is None and len(got) > 64:
            best = (baud, len(got), 0)

    print("\n" + "=" * 74)
    if best is None:
        print("所有波特率都是 0 字节 —— 说明对端(电脑侧蓝牙模块)根本没有吐数据,")
        print("与速率无关。请检查蓝牙链路是否已建立。")
    elif best[2] > 0:
        print(f"命中: {best[0]} 波特率, {best[1]} 字节, 解析出 {best[2]} 个遥测帧。")
        print(f"=> 在上位机顶栏把波特率设为 {best[0]} 即可。")
    else:
        print(f"{best[0]} 收到 {best[1]} 字节但解析不出帧 —— 有数据在流, 速率或许接近但不对,")
        print("  或者对端发送的数据格式与协议不符。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
