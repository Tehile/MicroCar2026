"""V3 真机验收: `MOVE RUN` 的**双护栏** + `ESTOP` 锁存 / `STOP` 解锁。

判据(设计方案 §5.2 V3): 「值守态 `MOVE RUN ...` | 双护栏生效; `ESTOP` 锁存; `STOP` 解锁」

安全设计 —— 尽量做到**零位移**:

  T1 时间护栏   `MOVE RUN +25 -25 800 -1`       左右轮等速反向 = **原地自旋**, 红外护栏关闭,
               只考验"到时必停"。预期 `STOPPED TIME`, 实测 ~800ms。
  T2 红外护栏   `MOVE RUN +20 +20 1500 <minIr+25>`
               把阈值设在**当前最小红外读数之上** ⇒ 固件第一个 1ms tick 就触发,
               位移 ~0mm。预期 `STOPPED IR`。
               (`link_MinIr()` 取四路红外的**最小**值, 所以贴墙/窄道里必然立即触发。)
  T3 护栏不可重置 `MOVE RUN +25 -25 3000 -1`(自旋) 后, 运行中**再发一条** `MOVE RUN`
               ⇒ 预期 `ERR_BUSY`(否则时间护栏会被二次命令重置 —— 这是 link.c 里
               明确记着的旧缺陷)。随后 `ESTOP` 收。
  T4 锁存/解锁  急停后 `MOVE RUN` 应被拒(`ERR_ESTOP`); `STOP` 后应恢复可用。

⚠️ 会让电脑鼠**原地自旋**, 不发车、不前进(除 T2 的极小位移)。默认只读, 需 `--motor`。
⚠️ `MCU RESET` 只复位固件、不复位物理位置。

安全网 `--safe-ir N`: T1 原本把红外护栏**关掉**(`ir_mm=-1`)以纯粹考验时间护栏。
若场地里前向红外仍读到较近的距离(例如 ~100mm), 可用 `--safe-ir N`(N 取**低于当前最小
读数**的一个值, 如 80) 让 T1 带一个保护阈值 —— 只要自旋全程最小红外 > N 就不会误停,
一旦某路红外掉到 N 以下则立即停(说明车在逼近物体)。判读时看 T1 的 reason 仍是 `TIME` 即可。

用法:
    python move_run_guard_test.py COM7 --motor
    python move_run_guard_test.py COM7 --motor --safe-ir 80
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")


def fw_expect():
    try:
        t = Path("../code_stm32f1_mouse/HARDWARE/link.h").read_text(encoding="utf-8")
        return (int(re.search(r"#define\s+LINK_PROTO_VER\s+(\d+)", t).group(1)),
                int(re.search(r"#define\s+LINK_FW_VER\s+(\d+)", t).group(1)))
    except Exception:
        return None, None


def ir_snapshot(mc) -> dict:
    mc.stream.on()
    mc.telemetry.wait_first_frame(timeout=5.0)
    f = mc.telemetry.latest() or {}
    mc.stream.off()
    d = {k: f.get(k) for k in ("irF1", "irF2", "irL", "irR")}
    vals = [v for v in d.values() if isinstance(v, (int, float))]
    d["min"] = min(vals) if vals else None
    d["frame"] = f
    return d


def fire(mc, sp_l: int, sp_r: int, ms: int, ir_mm: int, label: str,
         since: float | None = None) -> tuple:
    """发一条 MOVE RUN 并等 `STOPPED <reason>`。返回 (ack_result, event_msg, 实际耗时s)。"""
    t0 = time.time()
    r = mc.send_cmd("move.run", sp_l=sp_l, sp_r=sp_r, ms=ms, ir_mm=ir_mm, timeout=6.0)
    print(f"    {label}: ACK ok={r.ok} status={r.status} "
          f"(往返 {(time.time()-t0)*1000:.0f}ms)")
    if not r.ok:
        return r, None, time.time() - t0
    msg = mc.move.wait_stopped(timeout=max(6.0, ms / 1000.0 * 3 + 6.0),
                               since=since if since is not None else t0)
    el = time.time() - t0
    if msg is None:
        print(f"      ⚠ 未收到 STOPPED 事件(等了 {el:.1f}s)")
        return r, None, el
    print(f"      STOPPED {msg.data.get('reason')}  (自 ACK 起 {el*1000:.0f} ms) "
          f"raw={msg.data.get('raw')!r}")
    return r, msg, el


def main() -> int:
    ap = argparse.ArgumentParser(prog="move_run_guard_test.py")
    ap.add_argument("port", nargs="?", default="COM7")
    ap.add_argument("--motor", action="store_true", help="真的驱动电机(默认只读)")
    ap.add_argument("--countdown", type=float, default=4.0)
    ap.add_argument("--safe-ir", type=int, default=0,
                    help="T1 用的保护阈值(mm); 0=关闭(纯测时间护栏)。建议取低于当前最小读数")
    args = ap.parse_args()

    print("=" * 78)
    print(f"V3 MOVE RUN 双护栏 / 急停锁存验收  {args.port}"
          f"{'  【会转轮子!】' if args.motor else '  (只读)'}")
    print("=" * 78)

    from host.api import MouseClient

    mc = MouseClient(auto_session=False, auto_stream=False)
    if not mc.connect(args.port):
        print("连接失败:", mc.state_detail)
        return 1

    res: list[tuple[str, bool, str]] = []

    def rec(step, ok, note=""):
        res.append((step, ok, note))
        print(f"    [{'OK' if ok else '--'}] {step:<34} {note}")

    try:
        rp = mc.send_cmd("ping", timeout=5.0)
        ep, ef = fw_expect()
        print(f"\n[0] 指纹 proto/fw={mc.device.get('proto')}/{mc.device.get('fw')} "
              f"txbuf={mc.device.get('txbuf')} rst={mc.reset_cause} (源码 {ep}/{ef})")

        s = ir_snapshot(mc)
        f = s["frame"]
        print(f"    值守态: task={f.get('mazeTaskName')} pos=({f.get('posX')},{f.get('posY')}) "
              f"spd=({f.get('speedL')},{f.get('speedR')}) freemode={f.get('freemode')}")
        print(f"    红外读数(mm): f1={s['irF1']} f2={s['irF2']} l={s['irL']} r={s['irR']} "
              f"→ 最小 {s['min']}")
        rec("值守态读取", bool(f))

        if not args.motor:
            print("\n未指定 --motor —— 只读模式, 到此为止。")
            return 0

        min_ir = int(s["min"] or 200)
        thr = min_ir + 25

        print(f"\n    {args.countdown:.0f} 秒后开始(每步都会原地转, Ctrl-C 可中止)…")
        try:
            time.sleep(args.countdown)
        except KeyboardInterrupt:
            print("    已中止。")
            return 0

        # ---------------------------------------------------- T1 时间护栏
        t1_ir = args.safe_ir if args.safe_ir > 0 else -1
        if args.safe_ir > 0:
            print(f"\n[T1] 时间护栏(原地自旋; 带安全网 ir={args.safe_ir}mm, "
                  f"预期 STOPPED TIME)")
        else:
            print("\n[T1] 时间护栏(原地自旋 + 关闭红外护栏, 预期 STOPPED TIME)")
        r, m, el = fire(mc, 25, -25, 800, t1_ir, f"MOVE RUN 25 -25 800 {t1_ir}")
        ok = bool(m and m.data.get("reason") == "TIME" and 0.5 <= el <= 2.5)
        rec("T1 时间护栏 = TIME", ok, f"reason={m.data.get('reason') if m else None} 耗时 {el:.2f}s")

        # ---------------------------------------------------- T2 红外护栏
        print(f"\n[T2] 红外护栏(阈值 {thr}mm > 当前最小 {min_ir}mm ⇒ 预期立刻 STOPPED IR)")
        r, m, el = fire(mc, 20, 20, 1500, thr, f"MOVE RUN 20 20 1500 {thr}")
        ok = bool(m and m.data.get("reason") == "IR")
        rec("T2 红外护栏 = IR", ok, f"reason={m.data.get('reason') if m else None} 耗时 {el:.2f}s")

        # ---------------------------------------------------- T3 护栏不可被重置
        print("\n[T3] 护栏不可被重置(自旋中再发一条 MOVE RUN, 预期 ERR_BUSY)")
        t0 = time.time()
        r1 = mc.send_cmd("move.run", sp_l=25, sp_r=-25, ms=3000, ir_mm=-1, timeout=6.0)
        print(f"    第 1 条: ok={r1.ok} status={r1.status}")
        time.sleep(0.6)
        r2 = mc.send_cmd("move.run", sp_l=30, sp_r=30, ms=500, ir_mm=-1, timeout=6.0)
        print(f"    第 2 条(0.6s 后): ok={r2.ok} status={r2.status}")
        rec("T3 运动中二次 MOVE = ERR_BUSY", r2.status == "ERR_BUSY", f"status={r2.status}")

        # ---- ESTOP 收(顺便测锁存) ----
        t_es = time.time()
        re_ = mc.send_cmd("estop", timeout=6.0)
        print(f"    ESTOP: ok={re_.ok} status={re_.status}")
        mm = mc.move.wait_stopped(timeout=8.0, since=t_es)
        if mm is not None:
            print(f"      STOPPED {mm.data.get('reason')}  "
                  f"(自命令起 {(time.time()-t_es)*1000:.0f} ms)")
        rec("T3 自旋中 ESTOP = STOPPED ESTOP",
            bool(mm and mm.data.get("reason") == "ESTOP"),
            f"reason={mm.data.get('reason') if mm else None} 耗时 {(time.time()-t_es)*1000:.0f}ms")

        f2 = ir_snapshot(mc)["frame"]
        spd0 = (int(f2.get("speedL") or 0) == 0 and int(f2.get("speedR") or 0) == 0)
        rec("T4 急停后速度归零", spd0, f"spd=({f2.get('speedL')},{f2.get('speedR')}) "
                                      f"freemode={f2.get('freemode')}")

        # ---------------------------------------------------- T4 锁存 / 解锁
        print("\n[T4] 锁存态下 MOVE 应被拒; STOP 后应恢复")
        r_lock = mc.send_cmd("move.run", sp_l=25, sp_r=-25, ms=500, ir_mm=-1, timeout=6.0)
        print(f"    锁存态 MOVE RUN: ok={r_lock.ok} status={r_lock.status}")
        rec("T4 锁存态 MOVE 被拒", r_lock.status in ("ERR_ESTOP", "ERR_BUSY"),
            f"status={r_lock.status}")

        rs = mc.send_cmd("stop", timeout=6.0)
        print(f"    STOP: ok={rs.ok} status={rs.status}")
        time.sleep(0.4)
        r, m, el = fire(mc, 25, -25, 600, -1, "STOP 后 MOVE RUN 25 -25 600 -1")
        rec("T4 STOP 解锁后可再运动", bool(r.ok and m is not None),
            f"ACK={r.status} reason={m.data.get('reason') if m else None}")

        # ---------------------------------------------------- 收尾
        print("\n[收尾] 停车 + MCU RESET + 复核")
        mc.send_cmd("stop", timeout=5.0)
        rr = mc.conn.soft_reset(verify=True, timeout=12.0)
        rec("MCU RESET", (rr.data or {}).get("rst") == "SW",
            f"rst={(rr.data or {}).get('rst')}")
        time.sleep(1.0)
        rp2 = mc.send_cmd("ping", timeout=5.0)
        rec("链路仍在", rp2.ok, f"rst={mc.reset_cause}")
        s3 = ir_snapshot(mc)["frame"]
        rec("回值守态(速度 0)", int(s3.get("speedL") or 0) == 0 and int(s3.get("speedR") or 0) == 0,
            f"task={s3.get('mazeTaskName')} spd=({s3.get('speedL')},{s3.get('speedR')}) "
            f"freemode={s3.get('freemode')} battMv={s3.get('battMv')}")

        print("\n" + "=" * 78)
        print("V3 结论")
        print("=" * 78)
        for st, ok, note in res:
            print(f"    {'[OK]' if ok else '[--]'} {st:<34} {note}")
        bad = [st for st, ok, _ in res if not ok]
        print(f"\n    >>> {'全部通过 ✅' if not bad else '需关注: ' + str(bad)}")
    finally:
        try:
            mc.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
