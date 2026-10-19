"""`RATE PULL` 静默故障最小复现探针（只读，绝不发运动命令）。

背景：v2.10 现场发现发过 `RATE PULL 3 100` 之后固件整体静默（0 字节返回），
后续收车三步全 TIMEOUT。本脚本按"最小侵入"顺序逐段确认，定位是哪一步开始哑：

    S1  PING ×3            —— 复位后是否在线
    S2  TELEM 停等 ×3      —— 纯拉取通路基线
    S3  单发 RATE PULL 3 100，裸字节抓 3 s —— 观察 ACK + 3 帧，逐帧标注
    S4  TELEM ×3           —— PULL 批次走完之后是否还能拉
    S5  RATE OFF + PING    —— 若 S4 哑，判断是"PULL 模式残留"还是"整机挂死"

判读：
    S3 收到 ACK+3 帧、S4 正常      -> PULL 无问题，之前的静默另有原因
    S3 收到 ACK+3 帧、S4 全哑      -> 固件 TX 环/速率态残留，方向在 link_RateService
    S3 连 ACK 都没有               -> 命令解析阶段就挂，查 link_TokNum / PULL 分支
    S5 RATE OFF 后 PING 仍哑       -> 整机挂死（HardFault/阻塞），需断电

用法:
    python rate_pull_probe.py                # COM7, 默认参数
    python rate_pull_probe.py COM7 --n 3 --t 100
    python rate_pull_probe.py COM7 --no-pull  # 只跑 S1/S2 基线（对照）
"""
from __future__ import annotations

import argparse
import re
import sys
import time

try:
    import serial
except ImportError:  # pragma: no cover
    print("需要 pyserial")
    raise SystemExit(1)

sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else ".")

from host.protocol.codec import (  # noqa: E402
    FrameStreamParser,
    TYPE_MAP,
    TYPE_UNIFIED,
    crc16_modbus,
    decode_unified,
)

#: 本探针是纯只读诊断 —— 任何运动/停车/参数写入命令一律禁止。
FORBIDDEN = {
    "MOVE", "MAZE", "CAL", "ARM", "PARAMS", "SAVE", "STOP", "ESTOP",
    "MCU", "SETQ", "SETVL", "SETVR", "MOTOR", "PID", "SELFTEST",
}
#: RATE 家族允许（这正是被测对象），故不在 FORBIDDEN 内。


def build(cmd: str, seq: int) -> bytes:
    head = cmd.split()[0].upper()
    assert head not in FORBIDDEN, f"探针禁止发送 {head}"
    body = f"{cmd} #{seq}".encode("ascii")
    return body + f"*{crc16_modbus(body):04X}".encode("ascii") + b"\r\n"


def _classify(items):
    """把 parser.feed 的输出拆成 (lines, unified_payloads, map_payloads, junk)。"""
    lines, uni, maps = [], [], []
    for it in items:
        if isinstance(it, str):
            lines.append(it)
        elif it.type == TYPE_UNIFIED:
            uni.append(it.payload)
        elif it.type == TYPE_MAP:
            maps.append(it.payload)
    return lines, uni, maps


def exchange(ser, cmd: str, seq: int, timeout: float, need_frame: bool = True):
    """严格停等：发一条，等第一帧遥测（need_frame=False 时等第一行文本）为止。

    返回 (payload, lines, raw, rtt_ms)。
    """
    ser.reset_input_buffer()
    wire = build(cmd, seq)
    ser.write(wire)
    ser.flush()
    t0 = time.monotonic()
    parser = FrameStreamParser()
    raw = bytearray()
    lines, uni = [], []
    while time.monotonic() - t0 < timeout:
        chunk = ser.read(256)
        if not chunk:
            time.sleep(0.005)
            continue
        raw += chunk
        l, u, _ = _classify(parser.feed(chunk))
        lines += l
        uni += u
        if uni or (lines and not need_frame):
            tail = ser.read(128)
            if tail:
                raw += tail
                l2, _, _ = _classify(parser.feed(tail))
                lines += l2
            break
    return (uni[0] if uni else None), lines, bytes(raw), (time.monotonic() - t0) * 1000.0


def drain(ser, seconds: float, parser=None):
    """裸抓 seconds 秒，返回 (raw_bytes, lines, unified_payloads, map_payloads)。"""
    parser = parser or FrameStreamParser()
    raw = bytearray()
    lines, uni, maps = [], [], []
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        chunk = ser.read(256)
        if not chunk:
            time.sleep(0.005)
            continue
        raw += chunk
        l, u, m = _classify(parser.feed(chunk))
        lines += l
        uni += u
        maps += m
    return bytes(raw), lines, uni, maps


def fmt_payload(p) -> str:
    if p is None:
        return "无帧"
    try:
        f = decode_unified(p)
    except Exception as e:  # noqa: BLE001
        return f"解包异常 {e}"
    return (f"ms8={f.get('ms8')} "
            f"batt={f.get('battMv')}mV "
            f"f1={f.get('irF1')} f2={f.get('irF2')} "
            f"front={f.get('wallFront')} near={f.get('frontNear')} "
            f"stackIdx={f.get('stackIdx')} pullf1={int(f.get('pullf1', 0))} "
            f"err={f.get('pidErr')}")


def show(tag: str, payload, lines, raw: bytes, rtt: float, need_frame: bool = True) -> bool:
    text = [l for l in lines if l.startswith("PONG") or l.startswith("ACK")
            or l.startswith("ERR")]
    ok = (payload is not None) if need_frame else bool(text)
    n_line = text[0] if text else None
    print(f"    {'✓' if ok else '✗'} {tag:<22} {rtt:6.0f} ms  raw={len(raw)}B  "
          f"行={n_line or '(无)'}  帧={fmt_payload(payload)}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="RATE PULL 静默故障最小复现探针")
    ap.add_argument("port", nargs="?", default="COM7")
    ap.add_argument("--n", type=int, default=3, help="PULL 帧数（默认 3）")
    ap.add_argument("--t", type=int, default=100, help="PULL 帧间隔 ms（默认 100）")
    ap.add_argument("--no-pull", action="store_true", help="只跑 S1/S2 基线，不发 RATE")
    ap.add_argument("--timeout", type=float, default=2.5, help="单次停等超时 s")
    ap.add_argument("--drain", type=float, default=3.0, help="PULL 后裸抓时长 s")
    args = ap.parse_args()

    # seq 时间派生：固件有幂等重放簿记，同 seq+同内容只会被当重复而不再执行
    seq = 4000 + (int(time.time()) % 5000)

    print(f"RATE PULL 静默探针  {args.port}   PULL n={args.n} t={args.t}ms")
    print("=" * 88)

    ser = serial.Serial(args.port, 115200, timeout=0.05)
    hard_dead = False
    pull_ok = None
    tail_ok = None
    try:
        # ---- S1 PING ×3 ----
        print("\n[S1] PING ×3  （复位后是否在线）")
        pongs = 0
        for i in range(3):
            p, lines, raw, rtt = exchange(ser, "PING", seq, args.timeout, need_frame=False)
            seq += 1
            if show(f"PING #{i + 1}", p, lines, raw, rtt, need_frame=False):
                pongs += 1
        if pongs == 0:
            print("\n  ✗ 三次 PING 全无应答 —— 设备不在线（未上电/未复位/烧错档）。停止。")
            return 1
        print(f"  在线 {pongs}/3")

        # ---- S2 TELEM ×3 基线 ----
        print("\n[S2] TELEM 停等 ×3  （纯拉取通路基线）")
        telem_ok = 0
        for i in range(3):
            p, lines, raw, rtt = exchange(ser, "TELEM", seq, args.timeout)
            seq += 1
            if show(f"TELEM #{i + 1}", p, lines, raw, rtt):
                telem_ok += 1
        print(f"  拉取正常 {telem_ok}/3")
        if telem_ok < 3:
            print("\n  ⚠️ 基线就不稳 —— 先查链路本身（蓝牙/波特率），PULL 结论无意义。")

        if args.no_pull:
            print("\n--no-pull：跳过 S3–S5。")
            return 0

        # ---- S3 单发 RATE PULL，裸抓 ----
        cmd = f"RATE PULL {args.n} {args.t}"
        print(f"\n[S3] 单发 `{cmd}`，随后裸抓 {args.drain:.1f}s 原始字节")
        ser.reset_input_buffer()
        wire = build(cmd, seq)
        seq += 1
        print(f"    下行 {len(wire)}B: {wire!r}")
        ser.write(wire)
        ser.flush()
        raw, lines, uni, maps = drain(ser, args.drain)
        ack = [l for l in lines if l.startswith("ACK")]
        print(f"    上行 {len(raw)}B  行 {len(lines)} 条  遥测帧 {len(uni)}  (MAP {len(maps)})")
        for l in lines[:8]:
            print(f"      · {l}")
        for i, p in enumerate(uni[:8]):
            print(f"      帧{i + 1}: {len(p)}B  {fmt_payload(p)}")
        if len(uni) > 8:
            print(f"      … 另有 {len(uni) - 8} 帧")
        pull_ok = bool(ack) and len(uni) >= args.n
        print(f"    → 判读: {'ACK 到齐且帧数达标' if pull_ok else 'ACK 或帧数缺'}")

        # ---- S4 PULL 之后是否还能拉 ----
        time.sleep(0.5)
        print(f"\n[S4] PULL 批次走完后 TELEM ×3  （自时钟是否残留/整机是否挂死）")
        tail_ok = 0
        for i in range(3):
            p, lines, raw, rtt = exchange(ser, "TELEM", seq, args.timeout)
            seq += 1
            if show(f"TELEM after #{i + 1}", p, lines, raw, rtt):
                tail_ok += 1
        print(f"  PULL 后拉取正常 {tail_ok}/3")

        if tail_ok < 3:
            # ---- S5 分级探活 ----
            print("\n[S5] 分级探活：RATE OFF → PING，分辨'速率态残留'与'整机挂死'")
            ser.reset_input_buffer()
            ser.write(build("RATE OFF", seq))
            seq += 1
            ser.flush()
            raw, lines, uni, _ = drain(ser, 1.5)
            print(f"    RATE OFF 后 {len(raw)}B  行={[l for l in lines[:4]]}  帧={len(uni)}")
            p, lines, raw, rtt = exchange(ser, "PING", seq, args.timeout, need_frame=False)
            seq += 1
            if show("PING after RATE OFF", p, lines, raw, rtt, need_frame=False):
                print("    → 判读: RATE OFF 救回来了 ⇒ 故障在 PULL/FIX 自时钟态残留，非整机挂死")
            else:
                hard_dead = True
                print("    → 判读: RATE OFF 无效 ⇒ 整机无响应（HardFault 或 TX 环/ISR 阻塞）")

        # ---- 结论 ----
        print("\n" + "=" * 88)
        print("结论")
        if pull_ok and tail_ok == 3:
            print("  ✅ PULL 全通：ACK+帧数正常，随后纯拉取仍正常。")
            print("     => `RATE PULL` 本身不复现静默，之前的故障另有来源"
                  "（运动中/收车链路/蓝牙缓冲）。")
        elif pull_ok and tail_ok < 3:
            print("  ⚠️ PULL 应答正常，但之后 TELEM 拉不动 —— 速率态或 TX 环残留。")
            print("     下一步：查 link_RateService 退出条件与 link_SendFrame 的 TX 环回压。")
        elif not pull_ok and tail_ok < 3:
            print("  ❌ PULL 阶段就无完整应答且之后哑 —— 命令解析/PULL 分支故障。")
            print("     下一步：查 link_TokNum 连续两次取数是否前进、link_PullSet 钳位。")
        else:
            print("  ? PULL 应答缺但之后拉取恢复 —— 疑似瞬时抢占，需重复 3 次确认。")
        if hard_dead:
            print("  ⚠️ 出现过整机无响应 —— 断电重上电后再测。")
        print("=" * 88)
        return 0 if (pull_ok and tail_ok == 3) else 2
    finally:
        ser.close()


if __name__ == "__main__":
    raise SystemExit(main())
