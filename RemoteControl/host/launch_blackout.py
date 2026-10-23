"""量化 `MAZE RUN PULL 8 40` 之后固件的**静默窗口**, 并区分两种性质。

A. 运行期静默 —— 迷宫在跑时不应答, 停/复位后恢复
B. 死锁       —— 连 MAZE STOP 也不应答, 只能靠 2 s 软复位兜底

探测手段: 发车后按固定周期**逐条递增 seq** 发 PING, 记录每个 PONG 与每个统一帧
的到达时刻; 中途发一次 MAZE STOP, 观察恢复时刻。

⚠️ 安全: 本脚本会真的发车。因此它**必须无条件停车** —— 探测循环包在
try/except/finally 里, Ctrl-C 也走同一出口: MAZE STOP -> 验 task 归零 -> ESTOP
-> MCU RESET。2026-10-23 就是本脚本被中断且没有 finally, 车一直在跑。

用法: python launch_blackout.py COM7 [--sec 10] [--stop-at 4]
"""
import argparse
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import serial                                              # noqa: E402
from host.protocol import codec                            # noqa: E402

BAUD = 460800
PROBE_MS = 100
# `ESTOP` 必须在白名单里：panic_stop 的第三级要发它（固件顶层命令，非 MAZE 子命令）。
ALLOWED = {"PING", "TELEM", "RATE", "MAZE", "MCU", "ESTOP"}


def build_cmd(cmd: str, seq: int) -> bytes:
    """与 basic_cmd_smoke.build 逐字节一致，但用本脚本自己的白名单。"""
    head = cmd.split()[0].upper()
    assert head in ALLOWED, "本脚本不允许发送 %s" % head
    body = ("%s #%d" % (cmd, seq)).encode("ascii")
    return body + ("*%04X" % codec.crc16_modbus(body)).encode("ascii") + b"\r\n"


def panic_stop(ser, drain, seq_box, tries=3, verbose=True) -> bool:
    """无条件停车 + 验证, **屏蔽 SIGINT**（连按两次 Ctrl-C 不能打断停车）。

    `seq_box` 是一个 [int] 容器: 停车命令必须用**新 seq**, 否则会命中固件幂等
    重放分支（只补 ACK、不重做副作用）。
    """
    old = None
    try:
        old = signal.signal(signal.SIGINT, signal.SIG_IGN)
    except Exception:                                       # noqa: BLE001
        old = None
    try:
        def send(text, wait, frames_needed=0):
            seq_box[0] += 1
            ser.write(build_cmd(text, seq_box[0]))
            ser.flush()
            got, nfr = None, 0
            t0 = time.time()
            while time.time() - t0 < wait:
                fr, ln = drain(time.time())
                nfr += len(fr)
                for _, s in ln:
                    if s.startswith("ACK %d " % seq_box[0]):
                        got = s
                if got and nfr >= frames_needed:
                    break
            if verbose:
                print("  停车: %-24s -> %s" % (text, got or "无应答"))
            return got

        ack = None
        for _ in range(tries):
            ack = send("MAZE STOP", 0.8)
            if ack and " OK" in ack:
                break
            time.sleep(0.12)
        ok = bool(ack and " OK" in ack)
        if ok:
            # 验证: 拉 2 帧看 mazeTask/motionTask 归零。只看 ACK 不算停住。
            time.sleep(0.12)
            send("RATE PULL 2 60", 1.0, frames_needed=2)
        else:
            send("ESTOP", 0.6)
            time.sleep(0.15)
            send("MCU RESET", 0.4)
        if verbose:
            print("  停车结果: %s" % ("已发 MAZE STOP 并验证取帧" if ok
                                     else "MAZE STOP 无应答 -> 已升级 ESTOP/RESET"))
        return ok
    finally:
        if old is not None:
            try:
                signal.signal(signal.SIGINT, old)
            except Exception:                               # noqa: BLE001
                pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("port", nargs="?", default="COM7")
    ap.add_argument("--sec", type=float, default=10.0, help="总探测时长")
    ap.add_argument("--stop-at", type=float, default=4.0, help="发车后多少秒发 MAZE STOP")
    a = ap.parse_args()

    ser = serial.Serial(a.port, BAUD, timeout=0.005, write_timeout=1.5)
    rb = codec.FrameStreamParser()
    t0 = time.time()

    def drain(now):
        fr, ln = [], []
        while True:
            d = ser.read(512)
            if not d:
                break
            for it in rb.feed(d):
                if isinstance(it, codec.Frame):
                    if it.type == codec.TYPE_UNIFIED and len(it.payload) == codec.UNIFIED_LEN:
                        fr.append(now)
                else:
                    ln.append((now, it))
        return fr, ln

    # --- 握手 ---
    ser.reset_input_buffer()
    ser.write(build_cmd("PING", 1200))
    ser.flush()
    time.sleep(0.5)
    _, ln0 = drain(time.time() - t0)
    pong = [s for _, s in ln0 if s.startswith("PONG")]
    print("握手 PONG: %s" % (pong[0] if pong else "无"))
    if not pong:
        print("链路不通, 退出")
        return 1

    seq_run, seq_stop = 1300, 1400
    seq_box = [1200]         # 探测/停车共用: **每条递增**（见下方注释）
    body_run = build_cmd("MAZE RUN PULL 8 40", seq_run)
    body_stop = build_cmd("MAZE STOP", seq_stop)

    ser.reset_input_buffer()
    base = time.time()
    frames, lines = [], []
    t_stop = None
    stop_sent = False
    interrupted = False
    try:
        ser.write(body_run)
        ser.flush()
        print("t=+0.000s  发出 MAZE RUN PULL 8 40 (seq=%d)" % seq_run)
        while True:
            el = time.time() - base
            if el >= a.sec:
                break
            if not stop_sent and el >= a.stop_at:
                ser.write(body_stop)
                ser.flush()
                t_stop = el
                stop_sent = True
                print("t=+%.3fs  发出 MAZE STOP (seq=%d)" % (el, seq_stop))
            # ⚠️ 每次必须用**新的 seq**: 固定 seq 重发会命中固件幂等重放分支,
            # 它只补一条 ACK、**不重做 PONG 这个副作用** —— 于是看起来像"链路静默",
            # 实际是探针自己被吞了。这个坑实测误判过一次。
            seq_box[0] += 1
            ser.write(build_cmd("PING", seq_box[0]))
            ser.flush()
            tp = time.time()
            while time.time() - tp < PROBE_MS / 1000.0:
                now = time.time() - base
                f, l = drain(now)
                frames += f
                lines += l
    except KeyboardInterrupt:
        # ★ 2026-10-23 事故现场: 这里原本没有 except/finally, Ctrl-C 一按
        #   探测循环退出, 后面既不发 STOP 也不关串口 —— 车就一直在跑。
        interrupted = True
        print("\n[中断] Ctrl-C —— 立即停车")
    finally:
        # 无条件停车（幂等）。正常结束也走一遍，作为"确实停住"的最终确认。
        print("\n[停车] 探测结束，执行强制停车:")
        try:
            panic_stop(ser, drain, seq_box)
        except BaseException:                               # noqa: BLE001
            pass
        try:
            ser.close()
        except BaseException:                               # noqa: BLE001
            pass

    if interrupted:
        print("\n[判定] 被用户中断 —— 已强制停车；本次数据不足以判定静默性质")
        return 130

    # --- 报告（所有时刻统一相对"发车时刻 base"，单位 ms）---
    print("\n帧到达(+ms, 相对发车):")
    print("  " + " ".join("%.0f" % (x * 1000) for x in frames))
    pongs = [x * 1000 for x, s in lines if s.startswith("PONG")]
    print("\nPONG 到达(+ms):")
    print("  " + " ".join("%.0f" % x for x in pongs))
    acks = [(x * 1000, s) for x, s in lines if s.startswith("ACK")]
    print("\nACK:")
    for t, s in acks:
        print("  +%.0f ms  %s" % (t, s))

    # 恢复时刻: STOP 之后第一个应答/帧
    if t_stop is not None:
        rec = [x * 1000 for x in frames if x > t_stop]
        rec += [x * 1000 for x, s in lines if s.startswith("PONG") and x > t_stop]
        first = min(rec) if rec else None
        if first is None:
            print("\n[判定] MAZE STOP 后 %.1f s 内仍无任何应答 => 疑似死锁(需 2 s 软复位兜底)" %
                  (a.sec - t_stop))
        else:
            print("\n[判定] MAZE STOP(+%.0f ms) 后 +%.0f ms 恢复应答 => 运行期静默, 非死锁" %
                  (t_stop * 1000, first - t_stop * 1000))
    return 0


if __name__ == "__main__":
    sys.exit(main())
