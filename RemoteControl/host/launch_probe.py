"""`MAZE RUN` 回 ACK OK 却不发车 —— 时间线探针（现场用，含安全收车）。

背景：2026-10-20 01:5x 满电机器上实测 `v210_launch_pull_test --motor`：
`MAZE RUN` 近零延迟回 `ACK <seq> OK`（说明固件已置 `linkMazeReq`），
但 5 s 观察窗口内 `mazeTask` 恒 `WAIT`、`motion=NULL`、轮速恒 0 —— 车一步没走。

本脚本沿**时间轴**取证据，区分两种成因（这是代码读不出来的部分）：

  A) 复位循环：发车路径上触发 IWDG / HardFault ⇒ MCU 反复重启，
     每次重启 `LINK_Init()` 清 `linkMazeReq` ⇒ 请求永远等不到消费。
     判据：发车后 `PONG` 的复位原因字段从 `POR` 变成 `IWDG`/`PIN`/`SOFT` 之类，
     且 `ms8`（固件毫秒低 8 位）出现**回绕倒退**。

  B) 请求未被消费：主循环没走到 `while (!LINK_MazeRequested())`
     （卡在 `maze_run` 的 Flash 擦除 / 读图 / parameter_search 里）。
     判据：`ms8` 连续单调前进（没有重启），`mazeTask` 恒 `WAIT`。

输出每行：`t  ms8  task  motion  spdL/R  pos  flags`

安全：进 `finally` 一定走 `force_idle(assume_busy=True)`（分级收车 + MCU RESET）。
      车必须悬空或场地空旷 —— 本脚本**真的发车**。

用法:
    python launch_probe.py COM7                 # 发一轮，观察 6 s
    python launch_probe.py COM7 --observe 10 --step 0.2
    python launch_probe.py COM7 --dry           # 只读：不发车，只看时间线基线
"""
from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else ".")

from host.api import MouseClient          # noqa: E402
import v210_launch_pull_test as P         # 复用其分级收车与新鲜帧判据  # noqa: E402


def ms8_of(mc: MouseClient):
    f = mc.telemetry.latest() or {}
    return f.get("ms8")


def main() -> int:
    ap = argparse.ArgumentParser(description="MAZE RUN 不发车时间线探针")
    ap.add_argument("port", nargs="?", default="COM7")
    ap.add_argument("--observe", type=float, default=6.0, help="观察窗口秒数")
    ap.add_argument("--step", type=float, default=0.25, help="采样间隔秒数")
    ap.add_argument("--dry", action="store_true", help="不发车，只采时间线（基线对照）")
    ap.add_argument("--target", default="BLIND", choices=["BLIND", "RACE"])
    args = ap.parse_args()

    mc = MouseClient(auto_session=False, auto_stream=True)
    if not mc.connect(args.port):
        print(f"连接 {args.port} 失败: {mc.state_detail}")
        return 1

    launched = False
    rows = []
    try:
        if not mc.telemetry.wait_first_frame(timeout=8.0):
            print("等首帧超时 —— 链路没起来，中止")
            return 1

        dev = mc.device or {}
        rst0 = dev.get("rst") or mc.reset_cause
        print(f"设备: proto={dev.get('proto')} fw={dev.get('fw')} "
              f"rst={rst0}  stream={mc.stream.mode}")
        print(f"发车前: {P.fmt_state(mc)}")

        # ---- 收车清场（确保是待命态，否则 MAZE RUN 会被正确地回 ERR_BUSY）----
        ok, note = P.force_idle(mc, mc.stream.mode, verbose=False)
        print(f"清场: {'待命' if ok else '未确认待命'}  ({note})")
        if not ok:
            print("清场失败 —— 不发车。")
            return 2
        base = P.fresh_frame(mc, mc.stream.mode, 1.5)
        print(f"清场后: mazeTask={base.get('mazeTaskName')} "
              f"motion={base.get('motionTaskName')} ms8={base.get('ms8')}")

        if args.dry:
            print("\n--dry：只采时间线，不发车。")
        else:
            print(f"\n>>> 发车 MAZE RUN {args.target}（车应悬空/空旷）")
            t_launch = time.time()
            r = mc.maze.run(args.target)
            launched = True
            d = r.data if isinstance(r.data, dict) else {}
            print(f"    应答 {r.status}  ok={r.ok}  attempts={d.get('attempts')} "
                  f"耗时 {(time.time() - t_launch) * 1000:.0f} ms")

        # ---- 时间线 ----
        print(f"\n{'t(s)':>6}  {'ms8':>4}  {'Δ':>5}  {'task':<8} {'motion':<10} "
              f"{'spdL':>5} {'spdR':>5}  {'pos':<7} {'gyro':>6}  flags")
        print("-" * 92)
        t0 = time.time()
        prev_ms8 = None
        wraps = 0
        while time.time() - t0 < args.observe:
            try:
                mc.cmd.telem(timeout=1.0)
            except Exception:
                pass
            f = mc.telemetry.latest() or {}
            m = f.get("ms8")
            delta = ""
            if prev_ms8 is not None and m is not None:
                dv = (m - prev_ms8) % 256
                delta = f"{dv:+d}" if dv < 128 else f"{dv - 256:+d}"
                if dv >= 200:            # 8 位回绕（约 200ms 以上倒退）
                    wraps += 1
            prev_ms8 = m
            flags = []
            if f.get("estop_lock"):
                flags.append("ESTOP")
            if f.get("motors_off"):
                flags.append("MOFF")
            if f.get("freemode"):
                flags.append("FREE")
            rows.append(dict(t=time.time() - t0, ms8=m, f=f))
            print(f"{time.time() - t0:6.2f}  {m if m is not None else '----':>4}  "
                  f"{delta:>5}  {str(f.get('mazeTaskName', '?')):<8} "
                  f"{str(f.get('motionTaskName', '?')):<10} "
                  f"{f.get('speedL', 0):5d} {f.get('speedR', 0):5d}  "
                  f"({f.get('posX')},{f.get('posY')})   {f.get('gyroAngle', 0):6d}  "
                  f"{','.join(flags) or '-'}")
            time.sleep(max(0.0, args.step - 0.05))

        # ---- 发车后再看一次复位原因 ----
        tasks = sorted({str(r["f"].get("mazeTaskName")) for r in rows})
        motions = sorted({str(r["f"].get("motionTaskName")) for r in rows})
        maxspd = max([abs(r["f"].get("speedL", 0) or 0) for r in rows]
                     + [abs(r["f"].get("speedR", 0) or 0) for r in rows] + [0])
        moved = any(P.running_now(r["f"]) for r in rows)

        print("\n" + "=" * 92)
        print(f"窗口内: task={tasks}  motion={motions}  maxSpd={maxspd}  "
              f"判定在跑={moved}  ms8 回绕次数={wraps}")

        try:
            mc.send_cmd("ping", timeout=3.0)
        except Exception:
            pass
        dev2 = mc.device or {}
        rst1 = dev2.get("rst") or mc.reset_cause
        print(f"发车后复位原因: {rst1}   (发车前 {rst0})")

        print("\n判读")
        if not args.dry and not moved and rst1 != rst0:
            print("  ❌ 复位原因变了 ⇒ 成因 A：发车路径触发复位，请求被 LINK_Init 清掉。")
        elif not args.dry and not moved and rst1 == rst0 and wraps <= 1:
            print("  ❌ 无重启、却不跑 ⇒ 成因 B：主循环未消费 linkMazeReq"
                  "（卡在 maze_run 的 Flash 擦除/读图/parameter_search）。")
        elif not args.dry and not moved:
            print("  ⚠️ 不跑，但 ms8 有多次回绕 —— 可疑重启，需查 IWDG 喂狗窗口。")
        elif moved:
            print("  ✅ 本次真的发车了（与历史现象不一致，可能为偶发）。")
        else:
            print("  --dry 基线，无判读。")
        return 0
    finally:
        print("\n[安全出口] 分级收车 …")
        try:
            ok, note = P.force_idle(mc, mc.stream.mode, verbose=True,
                                    assume_busy=launched)
            print(f"[安全出口] {'已确认待命' if ok else '⚠️ 未确认待命，请人工断电'}: {note}")
        except Exception as e:  # noqa: BLE001
            print(f"[安全出口] 收车异常 {type(e).__name__}: {e} —— 请人工断电")
        try:
            mc.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
