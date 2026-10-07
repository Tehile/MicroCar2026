"""真机判别探针 —— **纯非运动命令**, 用于定位 v2.0 "STREAM ON 之后失联"。

⚠️ 安全约束(本脚本的硬性设计前提)
    本探针**只发不驱动电机的命令**。已移除早前版本里的 `MAZE RUN` ——
    它会让 [4] 档固件进入 `LESSON_Blind8()` 自走迷宫, 属危险命令, 不应出现在诊断脚本里。
    脚本内置 MOTOR_CMDS 黑名单, 命中即 raise, 防止以后有人顺手加回去。

命令安全性逐条说明(依据固件源码):
    PING          读: 回 PONG + ACK                              —— 无副作用
    ARM           写: 仅 linkArmed/linkArmMs/基准灯 LED4          —— **不碰 speedSt/motionCtrl/motor**
    GET ALL       读: 24 项生产参数                                —— 无副作用
    GET <name>    读: 单个参数                                     —— 无副作用
    LESSON STATUS 读: 档位指纹(非 [3] 档回 ERR_VALUE)              —— 无副作用
    MAP DUMP      读: 98 B 墙图突发                                —— 无副作用
    STREAM ON/OFF 开关遥测流(本次要定位的对象)                    —— 不驱动电机
    --- 以下**一律禁止**: MOVE * / MAZE RUN / LESSON START / CAL * ---

用法: python host/real_discriminate.py COM7
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

import serial  # noqa: E402

from host.protocol.codec import crc16_modbus  # noqa: E402

#: 会驱动电机的命令前缀 —— 命中即拒绝执行(安全闸)
MOTOR_CMDS = (
    "MOVE",
    "MAZE RUN",
    "LESSON START",
    "CAL",
    "TARE",
)


def assert_safe(text: str) -> None:
    up = text.upper()
    for bad in MOTOR_CMDS:
        if up.startswith(bad):
            raise AssertionError(
                f"拒绝执行可能驱动电机的命令: {text!r} (命中黑名单 {bad!r})"
            )


def build(cmd: str, seq: int) -> bytes:
    body = f"{cmd} #{seq}".encode("ascii")
    return body + f"*{crc16_modbus(body):04X}".encode("ascii") + b"\r\n"


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "COM7"
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

    def step(text: str, wait: float = 2.5, quiet: bool = False) -> bytes:
        assert_safe(text)
        n[0] += 1
        s.reset_input_buffer()
        s.write(build(text, n[0]))
        s.flush()
        b = pump(wait)
        if not quiet:
            tag = "OK " if b else "!! 无应答"
            print(f"  [{tag}] {text:14s} #{n[0]:3d}  {len(b):6d} B")
            for ln in b.split(b"\r\n")[:3]:
                if ln:
                    print(f"           {ln[:100]!r}")
            nlines = len([x for x in b.split(b"\r\n") if x])
            if nlines > 3:
                print(f"           ... 共 {nlines} 行")
        return b

    print("=" * 72)
    print(f"判别探针(纯非运动命令)  端口={port}")
    print("=" * 72)

    # ---------- 阶段 0: 等设备就绪 ----------
    print("\n[0] 等待设备就绪 —— 若设备当前无响应, 请复位它, 最长等 90s")
    t_wait = time.time()
    alive = False
    while time.time() - t_wait < 90.0:
        b = step("PING", 1.0, quiet=True)
        if b"PONG" in b:
            print(f"    设备已就绪 (等了 {time.time()-t_wait:.1f}s)")
            alive = True
            break
    if not alive:
        print("    90s 内仍未就绪, 退出。")
        s.close()
        return 1

    print("\n[1] PING —— link_Ack 基线")
    b = step("PING", 3.0)
    print("     指纹 =", b.split(b"\r\n")[0] if b else None)

    print("\n[2] ARM —— 验证 link_AckU 路径(仅武装计时 + 点基准灯, **不驱动电机**)")
    a = step("ARM", 3.0)
    if not a:
        print("  >>> 结论: link_AckU 路径有问题 —— ARM 与 STREAM ON 共用它")
        s.close()
        return 2
    print("  >>> link_AckU 正常, 时间锚 =", a.split(b"\r\n")[0])

    print("\n[3] GET ALL —— 24 项生产参数(验证多行文本)")
    step("GET ALL", 4.0)

    print("\n[4] LESSON STATUS —— 档位指纹(非 [3] 档应回 ERR_VALUE; 无副作用)")
    step("LESSON STATUS", 2.5)
    print("     (注: [4] 档无法在不触发自走的前提下探明, 故本脚本不测)")

    print("\n[5] MAP DUMP —— 120 B 突发(验证链路突发能力)")
    step("MAP DUMP", 4.0)

    print("\n[6] STREAM ON —— 最后才试开流")
    seq_stream = n[0] + 1
    step("STREAM ON", 6.0)
    print("     再等 1s 看有没有帧涌进来 ...")
    extra = pump(1.0)
    print(f"     额外收到 {len(extra)} B  帧头 AA55 x{extra.count(b'\xaa\x55')}")

    print("\n[7] 开流后再 PING(若哑 => 开流把链路/MCU 弄死了)")
    pb = step("PING", 2.5, quiet=True)
    print(f"     PING -> {len(pb)} B  {pb[:80]!r}")

    print("\n[8] STREAM OFF(尽力而为)")
    step("STREAM OFF", 2.5, quiet=True)
    print("     已尝试关流")

    s.close()
    print("\n" + "=" * 72)
    print("判读要点")
    print("  · [2] ARM 有应答   => link_AckU 没问题, 问题出在'开流之后'")
    print("  · [2] ARM 无应答   => link_AckU 路径有问题")
    print("  · [7] PING 哑      => 开流把链路或 MCU 弄死了")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
