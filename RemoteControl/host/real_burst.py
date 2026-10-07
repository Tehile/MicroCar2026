"""真机: 验证"成串发 TELEM 会掉" —— 定位窗口上限的依据。

假设: 固件的 RX 行消费是**每 1ms tick 处理一行**(USART_RX_STA 的 bit7 未清时,
后续字节被 usart.c 丢弃)。因此**同一毫秒内灌入多条命令会丢**。
若成立 => 拉取器应"一次只放一条在途"(停等), 或最多两条, 而不是窗口 4~16。

做法: 在**一个 burst 内**背靠背发出 N 条 TELEM(不同 seq), 然后等足够久,
数回来的 ACK 条数与帧数。对比 N=1/2/3/4/6 的回收率。

只发 TELEM(读), 不驱动电机。

用法: python host/real_burst.py COM7
"""

from __future__ import annotations

import re
import sys
import time

sys.path.insert(0, ".")

import serial  # noqa: E402

from host.protocol.codec import crc16_modbus  # noqa: E402


def build(cmd: str, seq: int) -> bytes:
    body = f"{cmd} #{seq}".encode("ascii")
    return body + f"*{crc16_modbus(body):04X}".encode("ascii") + b"\r\n"


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "COM7"
    s = serial.Serial(port, 115200, timeout=0.02)
    s.dtr = False
    s.rts = False
    n = 0

    def pump(sec: float) -> bytes:
        end = time.time() + sec
        buf = b""
        while time.time() < end:
            d = s.read(65536)
            if d:
                buf += d
        return buf

    def ping() -> bool:
        nonlocal n
        n += 1
        s.reset_input_buffer()
        s.write(build("PING", n))
        s.flush()
        return b"PONG" in pump(1.0)

    print("等待设备就绪(最长 60s) ...")
    ready = False
    t0 = time.time()
    while time.time() - t0 < 60:
        if ping():
            ready = True
            break
        time.sleep(0.5)
    if not ready:
        print("  未就绪, 退出")
        s.close()
        return 1
    print(f"  就绪 (等了 {time.time()-t0:.1f}s)\n")

    print("=" * 72)
    print("成串请求回收率 (burst 内背靠背发 N 条 TELEM)")
    print("=" * 72)
    print(f"{'N':>3} {'发ACk回':>8} {'收帧':>6} {'回收率':>8}  说明")
    print("-" * 72)

    for N in (1, 2, 3, 4, 6, 8):
        # 清空静默
        s.reset_input_buffer()
        pump(0.4)
        base = n
        seqs = []
        for i in range(N):
            n += 1
            seqs.append(n)
            s.write(build("TELEM", n))     # 背靠背, 无间隔
        s.flush()
        # 等足够久: 单条 150ms, 给 N*150 + 800ms 余量
        budget = N * 0.16 + 1.0
        buf = pump(budget)
        got_frames = buf.count(b"\xaa\x55")
        # ⚠️ ACK 行是**夹在二进制帧之间**的, 所以不能按行 split 后 startswith ——
        # 用正则直接在整段字节里找 `ACK <seq> ` 的出现。
        got_acks = {int(m) for m in re.findall(rb"ACK (\d+) ", buf)}
        recovered = len(got_acks & set(seqs))
        rate = recovered / N * 100
        note = ""
        if recovered < N:
            note = f"丢了 seq {sorted(set(seqs) - got_acks)}"
        print(f"{N:>3} {recovered:>8} {got_frames:>6} {rate:>7.0f}%  {note}")

    # 对照: 间隔 300ms 逐条发 -> 应 100%
    print("\n对照组: 逐条发(间隔 300ms)")
    for N in (4,):
        s.reset_input_buffer()
        pump(0.4)
        seqs = []
        for _ in range(N):
            n += 1
            seqs.append(n)
            s.write(build("TELEM", n))
            s.flush()
            time.sleep(0.30)
        buf = pump(1.0)
        got = {int(m) for m in re.findall(rb"ACK (\d+) ", buf)}
        rec = len(got & set(seqs))
        print(f"  N={N} 回收 {rec}/{N} ({rec/N*100:.0f}%)")

    s.close()
    print("\n判读:")
    print("  · 若回收率随 N 下降 => 固件 RX 每 tick 只消费一行, 成串会丢")
    print("    => 拉取器应改为**停等**(窗口 1)或极小窗口, 并避免 burst 预填")
    print("  · 若都是 100% => 丢包另有原因, 需继续查")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
