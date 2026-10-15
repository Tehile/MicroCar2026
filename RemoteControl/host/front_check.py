"""前墙双档 + 比赛状态 只读验收（v2.9 / PROTO 8 / FW 221 / 远程档 txbuf 1024）。

检验 v2.3.0 新增的 4 个只读监视字节（载荷 45→49 B）是否真的按协议回传：

  偏移 45  前墙位   `frontNear`(b0) / `frontFar`(b1) / `frontBoth`(b2)
  偏移 46  冲刺位   `spurtMode`(b0-2) / `spurtFlag` / `goOrBack` /
                    `spurtTestFlag` / `objectGoToFlag`
  偏移 47  `goaheadnum`（冲刺前行格数）
  偏移 48  `fastwayCount90`（90° 路径计数）

前墙双档是**派生量**：固件在 `link.c` 组帧时算
    frontNear = front && min(f1,f2) <  IRTHRFNEAR(80)
    frontFar  = front && min(f1,f2) >= IRTHRFNEAR(80)
⇒ **恒等式** `(frontNear | frontFar) == wallFront` 且两者**互斥**。
本脚本逐帧核对这条恒等式 —— 它一旦不成立，说明固件组帧或上位机解包有一侧错了。

⚠️ **只发 `PING` / `TELEM`，绝不发运动命令**（见下方 `FORBIDDEN` 断言）。

用法:
    python front_check.py COM7                 # 默认采 10 帧
    python front_check.py COM7 -n 20
    python front_check.py COM7 --spurt         # 顺便打印冲刺状态量
    python front_check.py COM7 --label 贴脸     # 给这一段采样起个名

对应现场判据（设计稿 V6/V7/V8）:
    V6 车头贴墙        -> frontNear=1 frontFar=0
    V7 前方半格~一格   -> frontFar=1  frontNear=0
    V8 前方空旷        -> 两者皆 0
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

try:
    import serial
except ImportError:  # pragma: no cover
    print("需要 pyserial: pip install pyserial")
    raise SystemExit(1)

# 允许从 host/ 或项目根启动
sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else ".")

from host.protocol.codec import (  # noqa: E402
    FRONT_NEAR,
    FrameStreamParser,
    TYPE_UNIFIED,
    crc16_modbus,
    decode_unified,
)

#: 绝不发出的命令首词 —— 本脚本是纯只读验收。
FORBIDDEN = {"MOVE", "MAZE", "CAL", "ARM", "ESTOP", "STOP", "MCU", "PARAMS", "SAVE"}

ACK_RE = re.compile(rb"ACK (-?\d+) ([A-Za-z0-9_ ]+)")


def _fw_expect():
    """从固件 `link.h` 现读 (LINK_PROTO_VER, LINK_FW_VER)，读不到返回 None。

    ⚠️ 故意**不写死** —— 写死的话每次升版本都得来改测试，久了就变成
    "改测试让它过"而不是"发现两侧漂移"（与 pull_smoke 同一做法）。
    注意 `PONG` 的第 4 个字段是 TX 环缓冲大小，它**随档位变**
    （远程档 1024 / 比赛档 256），所以不参与核对。
    """
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", "code_stm32f1_mouse", "HARDWARE", "link.h")
    try:
        h = open(p, encoding="utf-8").read()
        return (int(re.search(r"#define\s+LINK_PROTO_VER\s+(\d+)", h).group(1)),
                int(re.search(r"#define\s+LINK_FW_VER\s+(\d+)", h).group(1)))
    except Exception:
        return None


def build(cmd: str, seq: int) -> bytes:
    """下行行格式 —— 必须与 `CommandClient._build` 逐字节一致（`*` 前无空格）。"""
    head = cmd.split()[0].upper()
    assert head not in FORBIDDEN, f"本脚本禁止发送 {head}"
    body = f"{cmd} #{seq}".encode("ascii")
    return body + f"*{crc16_modbus(body):04X}".encode("ascii") + b"\r\n"


def exchange(ser: serial.Serial, cmd: str, seq: int, timeout: float):
    """严格停等：发一条命令，收到**一帧**即止。返回 (frame_payload, lines, rtt_ms)。"""
    ser.reset_input_buffer()
    ser.write(build(cmd, seq))
    ser.flush()
    t0 = time.monotonic()
    parser = FrameStreamParser()
    frames: list = []
    lines: list = []
    while time.monotonic() - t0 < timeout:
        data = ser.read(256)
        if not data:
            time.sleep(0.005)
            continue
        for item in parser.feed(data):
            if isinstance(item, str):
                lines.append(item)
            elif item.type == TYPE_UNIFIED:
                frames.append(item.payload)
        if frames:
            # 再等一小会儿把同一批的 ACK 行收干净
            tail = ser.read(128)
            if tail:
                for item in parser.feed(tail):
                    if isinstance(item, str):
                        lines.append(item)
            break
    rtt = (time.monotonic() - t0) * 1000.0
    return (frames[0] if frames else None), lines, rtt


def main() -> int:
    ap = argparse.ArgumentParser(description="前墙双档/比赛状态只读验收")
    ap.add_argument("port", nargs="?", default="COM7")
    ap.add_argument("-n", "--count", type=int, default=10, help="采样帧数(默认 10)")
    ap.add_argument("--label", default="", help="本次采样标签, 便于对照现场摆放")
    ap.add_argument("--spurt", action="store_true", help="同时打印冲刺状态量")
    ap.add_argument("--watch", action="store_true",
                    help="实时模式: 持续拉取并逐帧打印前墙两档(搬车时看跳变), Ctrl+C 结束")
    ap.add_argument("--timeout", type=float, default=3.0, help="单帧超时(秒)")
    args = ap.parse_args()

    tag = f"[{args.label}] " if args.label else ""
    print(f"{tag}前墙双档只读验收  {args.port}   （只发 PING/TELEM，不动电机）")

    # ⚠️ seq 必须**每次会话都不同**: 固件有幂等重放簿记, 同一 seq+同内容的命令
    #    会被识别为重复, 只回 `ACK` 而**不重新执行**(PING 就不会再发 PONG)。
    #    这是正确行为, 但会让脚本误读成"没应答"。故用时间派生的基准 seq。
    base_seq = 1000 + (int(time.time()) % 8000)

    ser = serial.Serial(args.port, 115200, timeout=0.05)
    try:
        if args.watch:
            print("实时监视（Ctrl+C 结束）: near/far 随车头距离变化")
            i = 0
            try:
                while True:
                    payload, _, rtt = exchange(ser, "TELEM", base_seq + i, args.timeout)
                    i = (i + 1) % 900
                    if payload is None:
                        print("  ✗ 无帧")
                        continue
                    f = decode_unified(payload)
                    ok = ((f["frontNear"] | f["frontFar"]) == f["wallFront"]
                          and (f["frontNear"] & f["frontFar"]) == 0)
                    print(f"{time.strftime('%H:%M:%S')}  "
                          f"F1={f['irF1']:4d} F2={f['irF2']:4d} mm  "
                          f"front={f['wallFront']} near={f['frontNear']} "
                          f"far={f['frontFar']} both={f['frontBoth']}  "
                          f"{f['frontName']}{'' if ok else '  ⚠️恒等式失败'}")
            except KeyboardInterrupt:
                print("\n已停止。")
            return 0

        # --- 版本确认 ---
        pong = exchange(ser, "PING", base_seq, 3.0)
        ver_line = next((l for l in pong[1] if "PONG" in l), None)
        print(f"  固件: {ver_line or '(无 PONG 应答)'}")
        if ver_line:
            m = re.search(r"PONG (\d+) (\d+)", ver_line)
            exp = _fw_expect()
            if m and exp is not None and (int(m.group(1)), int(m.group(2))) != exp:
                print(f"  ⚠️ 期望 PONG {exp[0]} {exp[1]}（现读自源码 link.h），"
                      f"实际 {m.group(1)} {m.group(2)}"
                      f" —— 板上固件与当前源码不一致，下面的字段可能读成 0")

        rows = []
        for i in range(args.count):
            payload, lines, rtt = exchange(ser, "TELEM", base_seq + 10 + i, args.timeout)
            if payload is None:
                print(f"  [{i + 1:2d}] ✗ 无帧（{rtt:.0f} ms）  链路丢失?")
                continue
            f = decode_unified(payload)
            ok = ((f["frontNear"] | f["frontFar"]) == f["wallFront"]
                  and (f["frontNear"] & f["frontFar"]) == 0)
            rows.append((f, ok))
            flag = "" if ok else " ⚠️恒等式失败"
            print(f"  [{i + 1:2d}] {rtt:5.0f}ms  "
                  f"F1={f['irF1']:4d}mm(adc {f['irAdcF1']:5d})  "
                  f"F2={f['irF2']:4d}mm(adc {f['irAdcF2']:5d})  "
                  f"front={f['wallFront']}  near={f['frontNear']} "
                  f"far={f['frontFar']}  both={f['frontBoth']}  "
                  f"→ {f['frontName']}{flag}")
            if args.spurt:
                print(f"        冲刺: 档 {f['spurtModeName']}({f['spurtMode']})  "
                      f"flag={f['spurtFlag']} goOrBack={f['goOrBack']} "
                      f"test={f['spurtTestFlag']} objGoTo={f['objectGoToFlag']}  "
                      f"goaheadnum={f['goaheadnum']}  fastway90={f['fastwayCount90']}")

        if not rows:
            print("\n没有采到任何帧。")
            return 1

        # --- 汇总 ---
        n = len(rows)
        bad = [i for i, (_, ok) in enumerate(rows) if not ok]
        dist = {"无墙": 0, "近端": 0, "远端": 0, "双路互证": 0}
        for f, _ in rows:
            if f["frontNear"]:
                dist["近端"] += 1
            elif f["frontFar"]:
                dist["远端"] += 1
            else:
                dist["无墙"] += 1
            if f["frontBoth"]:
                dist["双路互证"] += 1

        print(f"\n汇总（{n} 帧, 标签「{args.label or '未命名'}」）")
        print(f"  恒等式 (near|far)==front 且互斥: {'✅ 全部成立' if not bad else f'❌ 失败 {len(bad)} 帧'}")
        print(f"  前墙分布: " + "  ".join(f"{k}={v}" for k, v in dist.items()))
        near_mm = [f["irF1"] if f["irF1"] < f["irF2"] else f["irF2"] for f, _ in rows]
        print(f"  min(f1,f2) mm: 最小 {min(near_mm)}  最大 {max(near_mm)}  "
              f"（近端判据阈值 IRTHRFNEAR=80）")

        # --- V6/V7/V8 判读 ---
        if dist["近端"] == n:
            print("  → 判读: V6 车头贴墙（frontNear）✅")
        elif dist["远端"] == n or dist["远端"] == n:
            print("  → 判读: V7 前方半格~一格有墙（frontFar）✅")
        elif dist["无墙"] == n:
            print("  → 判读: V8 前方空旷（两者皆 0）✅")
        else:
            print("  → 判读: 混合状态 —— 采样期间前方距离在 80mm 附近来回跳，"
                  "或车在动。静止再采一次。")
        return 0 if not bad else 2
    finally:
        ser.close()


if __name__ == "__main__":
    raise SystemExit(main())
