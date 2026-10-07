"""真机诊断: 界定链路可持续吞吐 —— 用非运动命令持续压链路。

目的
    `STREAM ON` 之后设备失联, 但 IWDG 在跑、MCU 没卡死 -> 数据发不出去。
    为区分"代码 bug"还是"链路容量不足", 做一个**不使用流**的持续吞吐实验:
    反复发 `MAP DUMP`(120 B 只读响应, 无副作用、不驱动电机), 测可持续 B/s。

判读
    · 若能稳定跑到接近请求-响应理论值(120B / 往返)而不断链
      => 链路本身能承受该量级, 问题更可能在统一帧路径
    · 若持续压几分钟后断链 => 链路容量问题, v2.0 "无 Hz 上限"的假设不成立

⚠️ 安全: 只用 PING / MAP DUMP / GET ALL(全部只读、不驱动电机)。
   脚本内置黑名单, 任何运动命令直接 raise。

用法: python host/real_throughput.py COM7 [秒数]
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

import serial  # noqa: E402

from host.protocol.codec import crc16_modbus  # noqa: E402

MOTOR_CMDS = ("MOVE", "MAZE RUN", "LESSON START", "CAL", "TARE")


def assert_safe(text: str) -> None:
    up = text.upper()
    for bad in MOTOR_CMDS:
        if up.startswith(bad):
            raise AssertionError(f"拒绝执行可能驱动电机的命令: {text!r}")


def build(cmd: str, seq: int) -> bytes:
    body = f"{cmd} #{seq}".encode("ascii")
    return body + f"*{crc16_modbus(body):04X}".encode("ascii") + b"\r\n"


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "COM7"
    dur = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0

    s = serial.Serial(port, 115200, timeout=0.02)
    s.dtr = False
    s.rts = False
    n = [0]

    def pump(sec: float) -> bytes:
        end = time.time() + sec
        buf = b""
        while time.time() < end:
            d = s.read(65536)
            if d:
                buf += d
        return buf

    # ---- 等待就绪 ----
    print("等待设备就绪(最长 60s, 若已失联请复位) ...")
    t0 = time.time()
    ready = False
    while time.time() - t0 < 60:
        n[0] += 1
        s.reset_input_buffer()
        s.write(build("PING", n[0]))
        s.flush()
        if b"PONG" in pump(0.8):
            print(f"  就绪 (等了 {time.time()-t0:.1f}s)")
            ready = True
            break
    if not ready:
        print("  设备未就绪, 退出")
        s.close()
        return 1

    # ---- 基线往返 ----
    print("\n[A] 基线往返(单条 MAP DUMP, 120 B 响应)")
    rtts = []
    for _ in range(5):
        n[0] += 1
        t1 = time.time()
        s.reset_input_buffer()
        s.write(build("MAP DUMP", n[0]))
        s.flush()
        b = pump(1.2)
        if b:
            rtts.append(time.time() - t1)
    if rtts:
        avg = sum(rtts) / len(rtts)
        print(f"  往返 {[f'{x*1000:.0f}ms' for x in rtts]}  平均 {avg*1000:.0f} ms")
        print(f"  => 请求-响应理论上限 ≈ {120/avg:.0f} B/s")
    else:
        print("  基线失败")
        s.close()
        return 2

    # ---- 持续压 ----
    print(f"\n[B] 持续压链路 {dur:.0f}s (背靠背 MAP DUMP, 每轮 6 条流水线)")
    t_start = time.time()
    total = 0
    rounds = 0
    lost_round = None
    while time.time() - t_start < dur:
        rounds += 1
        # 流水线发 6 条(不等应答), 再统一收
        for _ in range(6):
            n[0] += 1
            s.write(build("MAP DUMP", n[0]))
        s.flush()
        b = pump(1.0)
        total += len(b)
        if lost_round is None and len(b) < 6 * 100:
            lost_round = (rounds, len(b))
        el = time.time() - t_start
        if rounds % 3 == 0 or (lost_round and lost_round[0] == rounds):
            print(f"     t={el:5.1f}s  轮{rounds:3d}  本轮 {len(b):5d} B  "
                  f"累积 {total:6d} B  ({total/el:6.0f} B/s)")

    el = time.time() - t_start
    print(f"\n  合计 {total} B / {el:.1f}s = {total/el:.0f} B/s")
    if lost_round:
        print(f"  ⚠ 第 {lost_round[0]} 轮响应异常(仅 {lost_round[1]} B)")
    else:
        print("  ✅ 全程响应正常, 未出现断链")

    # ---- 存活复查 ----
    print("\n[C] 存活复查")
    still = False
    for i in range(3):
        n[0] += 1
        s.reset_write_timeout() if hasattr(s, "reset_write_timeout") else None
        s.write(build("PING", n[0]))
        s.flush()
        b = pump(1.5)
        print(f"     PING -> {len(b)} B  {b[:60]!r}")
        if b"PONG" in b:
            still = True
            break
    print(f"  => {'设备仍存活 ✓' if still else '设备已无响应 ✗'}")
    s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
