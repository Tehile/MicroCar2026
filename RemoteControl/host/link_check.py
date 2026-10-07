"""链路层验收：往返 / 重复指令(幂等重放) / 停止原因事件。

定位：`raw_ping.py` 是最小探活（只证明"能通"），本脚本是**完整链路验收**。
刻意**不走门面层**（直连 pyserial 自己构造命令与解析应答）—— 要检验的正是
"字节有没有发出去 / 有没有按协议回"，走门面反而会掩盖链路层问题。

覆盖三项（对应现场关心的三个问题）:

  L1 往返顺畅度   连续 N 条 `PING`（严格停等，一次一条）→ 成功率 + RTT 分布。
                  同时核对每条应答都带**正确回显的 seq** —— 这是"指令真的被
                  逐条执行"的证据，而不只是"有字节回来"。

  L2 重复指令     用**同一个 seq 且内容相同**的命令发两次，验证固件的幂等重放:
                    · `PING` 重发 → 第二次必须命中 `linkLastStatus` **重放**同一条应答
                      （不是重新执行）。这正是"ACK 丢失后上位机重发"能安全工作的前提。
                    · `TELEM` 重发 → 第二次必须回**新的一帧**（v2.1 特意让 TELEM
                      **绕开**幂等簿记；若被重放成旧帧，持续拉取就会拿到陈旧数据）。
                  同时验证**内容不同时幂等失效**（改 cmd、改 seq 都应重新执行）。

  L3 停止原因     `MOVE RUN` 让电机按时间护栏自停，核对收到
                  `STOPPED <REASON> #n` 事件、且 REASON 与预期一致:
                    · `MOVE RUN ... -1`（关红外）→ 期望 `TIME`
                    · `STOP` 打断         → 期望 `USER`
                    · `ESTOP` 打断        → 期望 `ESTOP`
                  **L3 会让电机转动**，必须显式 `--motor` 才跑。

用法:
    python link_check.py COM7                  # 只跑 L1/L2（不发运动命令，安全）
    python link_check.py COM7 --motor          # 追加 L3（会动车！轮子悬空或用 --motor-ms 控时长）
    python link_check.py COM7 -n 20 --motor --motor-ms 600
"""
from __future__ import annotations

import argparse
import re
import statistics
import sys
import time

try:
    import serial
except ImportError:  # pragma: no cover
    print("需要 pyserial: pip install pyserial")
    raise SystemExit(1)


# ------------------------------------------------------------------ 协议
def crc16_modbus(data: bytes) -> int:
    """与 host/protocol/codec.py 同实现(CRC16-MODBUS, 初值 0xFFFF)。"""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def build(cmd: str, seq: int) -> bytes:
    """下行行格式 —— 必须与 CommandClient._build 逐字节一致。

    ⚠️ `*` 前**没有空格**: `PING #1*C8E0`。多一个空格固件就回 `ERR_CRC`
    （我第一版就踩了这个，一度误判成链路故障）。
    """
    body = f"{cmd} #{seq}".encode("ascii")
    return body + f"*{crc16_modbus(body):04X}".encode("ascii") + b"\r\n"


ACK_RE = re.compile(rb"ACK (-?\d+) ([A-Za-z0-9_ ]+)")
STOPPED_RE = re.compile(rb"STOPPED (\w+) #(\d+)")
FRAME_HDR = b"\xaa\x55"


def send_recv(ser: serial.Serial, line: bytes, timeout: float = 3.0,
              quiet: bool = True) -> tuple[bytes, float]:
    """发一条命令并收集应答, 返回 (原始字节, 往返毫秒)。

    收满"包含 ACK 且自上一批后再无新字节达 quiet_s"即认为结束 —— 不能只等
    第一个 `\\r\\n`, 因为一帧二进制遥测里也可能含 0x0D0A。
    """
    ser.reset_input_buffer()
    t0 = time.time()
    ser.write(line)
    ser.flush()
    buf = b""
    last = t0
    while time.time() - t0 < timeout:
        chunk = ser.read(256)
        if chunk:
            buf += chunk
            last = time.time()
            continue
        if buf and ACK_RE.search(buf) and (time.time() - last) > 0.08:
            break
        time.sleep(0.01)
    return buf, (time.time() - t0) * 1000


def parse_ack(buf: bytes):
    """返回 (seq, status) 或 None。ACK 行夹在二进制帧之间, 必须用正则。"""
    m = ACK_RE.search(buf)
    return (int(m.group(1)), m.group(2).decode("ascii", "replace").strip()) if m else None


def count_frames(buf: bytes) -> int:
    return buf.count(FRAME_HDR)


def ok(msg):
    print(f"    [OK] {msg}")


def bad(msg):
    print(f"    [!!] {msg}")


def info(msg):
    print(f"         {msg}")


# ------------------------------------------------------------------ L1
def l1_roundtrip(ser: serial.Serial, n: int) -> dict:
    print(f"\n[L1] 往返顺畅度 —— 连续 {n} 条 PING（严格停等, 一次一条）")
    rtts: list[float] = []
    seq_ok = 0
    replies = 0
    for i in range(1, n + 1):
        line = build("PING", i)
        buf, ms = send_recv(ser, line)
        a = parse_ack(buf)
        if a is None:
            bad(f"#{i:<3d} 无 ACK   ({ms:6.0f} ms)  raw={buf[:60]!r}")
            continue
        replies += 1
        rtts.append(ms)
        if a[0] == i:
            seq_ok += 1
        else:
            bad(f"#{i:<3d} seq 回显不符: 收到 {a[0]}")
        if i <= 3 or i == n:
            info(f"#{i:<3d} -> ACK {a[0]} {a[1]:<12s} ({ms:6.0f} ms)")

    print()
    if rtts:
        rtts_s = sorted(rtts)
        med = statistics.median(rtts_s)
        p90 = rtts_s[min(len(rtts_s) - 1, int(len(rtts_s) * 0.9))]
        print(f"         应答 {replies}/{n}  ({replies/n*100:.0f}%)   "
              f"seq 回显正确 {seq_ok}/{replies}")
        print(f"         RTT  最小 {min(rtts_s):.0f} / 中位 {med:.0f} / "
              f"P90 {p90:.0f} / 最大 {max(rtts_s):.0f} ms")
        # 这条链路的实测特性: 中位 ≈200ms 是蓝牙转发延迟主导
        if med > 150:
            info("中位 RTT > 150 ms 属正常 —— 瓶颈是蓝牙模块转发延迟, 不是串口带宽")
    else:
        bad("一条应答都没收到 —— 链路或固件有问题")
    return {"replies": replies, "n": n, "rtts": rtts, "seq_ok": seq_ok}


# ------------------------------------------------------------------ L2
def l2_duplicate(ser: serial.Serial) -> dict:
    print("\n[L2] 重复指令 —— 同一 seq 同内容重发两次, 看是否幂等重放")
    res = {}

    # ---- (a) PING: 第二次应**重放**同一条应答(不重新执行) ----
    print("\n  (a) PING 重发 —— 期望: 第二次命中 linkLastStatus, 重放而非重执行")
    line = build("PING", 101)
    b1, t1 = send_recv(ser, line)
    b2, t2 = send_recv(ser, line)
    a1, a2 = parse_ack(b1), parse_ack(b2)
    info(f"第 1 次: {a1}   ({t1:6.0f} ms)")
    info(f"第 2 次: {a2}   ({t2:6.0f} ms)")
    same = (a1 == a2) and a1 is not None
    res["ping_replay"] = same
    if same:
        ok("PING 重发被幂等重放（应答一致, 未重复执行）")
    else:
        bad(f"PING 重发应答不一致: {a1} vs {a2}")

    # ---- (b) 幂等必须只在"seq + CRC 都相同"时生效 ----
    print("\n  (b) 换 seq / 换内容 —— 期望: 幂等**失效**, 重新执行")
    b3, _ = send_recv(ser, build("PING", 102))          # 同内容、不同 seq
    a3 = parse_ack(b3)
    b4, _ = send_recv(ser, build("GET ALL", 102))        # 同 seq、不同内容
    a4 = parse_ack(b4)
    info(f"PING #102 -> {a3}")
    info(f"GET ALL #102 -> {a4}")
    ok_change_seq = (a3 is not None and a3[0] == 102)
    ok_change_cmd = (a4 is not None and a4[0] == 102 and a4[1] == "OK")
    res["seq_reuse_ok"] = ok_change_seq
    res["cmd_change_ok"] = ok_change_cmd
    (ok if ok_change_seq else bad)("不同 seq 各自被正常执行")
    (ok if ok_change_cmd else bad)("同 seq 但内容不同 → 未误重放旧应答")

    # ---- (c) TELEM: 必须**绕开**幂等, 每次回新帧 ----
    print("\n  (c) TELEM 重发 —— 期望: **每次都是新帧**（v2.1 特意让它绕开幂等簿记）")
    tl = build("TELEM", 103)
    b1, t1 = send_recv(ser, tl)
    b2, t2 = send_recv(ser, tl)
    f1, f2 = count_frames(b1), count_frames(b2)
    a1, a2 = parse_ack(b1), parse_ack(b2)
    info(f"第 1 次: {f1} 帧 {a1}   ({t1:6.0f} ms)")
    info(f"第 2 次: {f2} 帧 {a2}   ({t2:6.0f} ms)")
    got_frames = f1 >= 1 and f2 >= 1
    res["telem_frames"] = got_frames
    (ok if got_frames else bad)(
        "两条 TELEM 各自换回一帧（未被重放成旧帧）" if got_frames
        else f"TELEM 未取到帧: {f1} / {f2}")

    # 锚必须每次新鲜: 比较两次应答里的 upMs
    if a1 and a2 and a1[1] != a2[1]:
        ok(f"两次应答的锚不同（{a1[1]} vs {a2[1]}）—— 锚是新鲜的")
        res["telem_fresh_anchor"] = True
    else:
        bad(f"两次 TELEM 应答的锚相同: {a1} / {a2} —— 锚可能被重放")
        res["telem_fresh_anchor"] = False

    return res


# ------------------------------------------------------------------ L3
def l3_stop_reason(ser: serial.Serial, ms: int) -> dict:
    print(f"\n[L3] 停止原因事件 —— MOVE RUN（{ms} ms, 关红外护栏 → 期望 TIME）")
    res = {}

    # 先清场: 让设备回到空闲(不可发车时也无害)
    send_recv(ser, build("STOP", 201), timeout=2.0)
    time.sleep(0.2)

    def run_and_wait(cmd: str, seq: int, want: str, timeout: float) -> bool:
        """发一条命令, 在 timeout 内找 STOPPED 事件并核对原因。"""
        # 读线程式收集: 边发边读, 直到出现 STOPPED 或超时
        ser.reset_input_buffer()
        t0 = time.time()
        ser.write(build(cmd, seq))
        ser.flush()
        buf = b""
        while time.time() - t0 < timeout:
            chunk = ser.read(256)
            if chunk:
                buf += chunk
            m = STOPPED_RE.search(buf)
            if m:
                reason = m.group(1).decode()
                ev_seq = int(m.group(2))
                el = time.time() - t0
                mark = "[OK]" if reason == want else "[!!]"
                print(f"    {mark} t={el:4.2f}s  STOPPED {reason} #{ev_seq}"
                      f"{'' if reason == want else f'   (期望 {want})'}")
                return reason == want
            time.sleep(0.01)
        bad(f"{timeout:.0f}s 内未收到 STOPPED 事件（cmd={cmd}）")
        return False

    # (1) 时间护栏自停
    ok1 = run_and_wait(f"MOVE RUN 40 40 {ms} -1", 201, "TIME", ms / 1000.0 + 12.0)
    res["time"] = ok1
    if ok1:
        ok("时间护栏自停 → `STOPPED TIME #n` 收到")

    time.sleep(0.3)

    # (2) STOP 打断 → USER
    print(f"\n    （2）MOVE RUN 长时长, 1s 后用 STOP 打断 → 期望 USER")
    ser.reset_input_buffer()
    ser.write(build(f"MOVE RUN 40 40 8000 -1", 202))
    ser.flush()
    buf = b""
    t0 = time.time()
    while time.time() - t0 < 1.0:
        buf += ser.read(256)
    ack = parse_ack(buf)
    info(f"发车应答 {ack}  |  转速 {buf.count(FRAME_HDR)} 帧")
    ack2 = None
    for _ in range(3):
        b, _ms = send_recv(ser, build("STOP", 203), timeout=4.0)
        ack2 = parse_ack(b)
        buf += b
        if ack2:
            break
    m = STOPPED_RE.search(buf)
    info(f"STOP 应答 {ack2}")
    ok2 = bool(m and m.group(1) == b"USER")
    (ok if ok2 else bad)(
        f"STOP 打断 → STOPPED {m.group(1).decode() if m else '(未收到)'}"
        f"{'' if ok2 else '  (期望 USER)'}")
    res["user"] = ok2

    time.sleep(0.3)

    # (3) ESTOP → ESTOP, 然后解锁
    print(f"\n    （3）MOVE RUN 长时长, ESTOP 打断 → 期望 ESTOP; 随后 STOP 解锁")
    ser.reset_input_buffer()
    ser.write(build("MOVE RUN 40 40 8000 -1", 204))
    ser.flush()
    buf = b""
    t0 = time.time()
    while time.time() - t0 < 1.0:
        buf += ser.read(256)
    for _ in range(3):
        b, _ms = send_recv(ser, build("ESTOP", 205), timeout=4.0)
        buf += b
        if parse_ack(b):
            break
    m = STOPPED_RE.search(buf)
    ok3 = bool(m and m.group(1) == b"ESTOP")
    (ok if ok3 else bad)(
        f"ESTOP 打断 → STOPPED {m.group(1).decode() if m else '(未收到)'}"
        f"{'' if ok3 else '  (期望 ESTOP)'}")
    res["estop"] = ok3

    b, _ = send_recv(ser, build("STOP", 206), timeout=4.0)
    info(f"解锁 STOP -> {parse_ack(b)}")
    return res


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser(prog="link_check.py")
    ap.add_argument("port", nargs="?", default="COM7")
    ap.add_argument("-n", type=int, default=12, help="L1 的 PING 条数")
    ap.add_argument("--motor", action="store_true",
                    help="追加 L3（会让电机转动！）")
    ap.add_argument("--motor-ms", type=int, default=600,
                    help="L3 时间护栏时长（默认 600 ms）")
    args = ap.parse_args()

    print("=" * 78)
    print(f"链路层验收   端口={args.port}"
          f"{'  【L3 会动车!】' if args.motor else '  (仅 L1/L2, 不发运动命令)'}")
    print("=" * 78)

    with serial.Serial(args.port, 115200, timeout=0.05) as ser:
        time.sleep(0.3)
        b, ms = send_recv(ser, build("PING", 1))
        a = parse_ack(b)
        print(f"\n[L0] 握手  PING -> {a}   ({ms:.0f} ms)")
        if not a or not a[1].startswith("OK"):
            bad("设备未应答 —— 请检查上电/电池/串口占用, 退出")
            return 1
        pong = re.search(rb"PONG ([0-9 ]+)", b)
        if pong:
            info(f"指纹 PONG {pong.group(1).decode().strip()}")

        r1 = l1_roundtrip(ser, args.n)
        r2 = l2_duplicate(ser)
        r3 = l3_stop_reason(ser, args.motor_ms) if args.motor else {}

    # ---------------- 汇总 ----------------
    print("\n" + "=" * 78)
    print("汇总")
    print("=" * 78)
    checks = [
        ("L1 往返成功", r1["replies"] == r1["n"]),
        ("L1 seq 回显全对", r1["seq_ok"] == r1["replies"] and r1["replies"] > 0),
        ("L2 PING 幂等重放", r2.get("ping_replay", False)),
        ("L2 不同 seq 正常执行", r2.get("seq_reuse_ok", False)),
        ("L2 同 seq 换内容不误重放", r2.get("cmd_change_ok", False)),
        ("L2 TELEM 每次新帧", r2.get("telem_frames", False)),
        ("L2 TELEM 锚新鲜", r2.get("telem_fresh_anchor", False)),
    ]
    if args.motor:
        checks += [
            ("L3 STOPPED TIME", r3.get("time", False)),
            ("L3 STOPPED USER", r3.get("user", False)),
            ("L3 STOPPED ESTOP", r3.get("estop", False)),
        ]
    for name, good in checks:
        print(f"    {'[OK]' if good else '[!!]'} {name}")
    fails = [n for n, g in checks if not g]
    print()
    if fails:
        print(f"    未通过: {fails}")
    else:
        print("    全部通过。" if args.motor
              else "    L1/L2 全部通过（L3 需加 --motor）。")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
