"""`MAZE STOP` 能不能真的退出状态机 —— 三步验收探针（现场用，含安全收车）。

要验的承诺（2026-10-20 修的 `721d568`）
--------------------------------------
1. `MAZE RUN` 能真发车（上一版 `176a4b1` 已验，这里顺带回归）；
2. `MAZE STOP` 之后 `mazeTask` 能自己回到 `WAIT`（电机归零是**必要条件但不是充分条件** ——
   修前电机 1.1 s 内就归零了，可 `mazeTask` 恒 `SEARCH`、状态机卡在 `objectGoTo` 空转）；
3. 停住之后**立刻**再发一次 `MAZE RUN` 必须回 `OK`。这一条才是"真退出"的硬证据：
   `ERR_BUSY` 判据是 `linkMazeReq || linkMazeRun`，`linkMazeRun` 只有
   `LINK_MazeFinished()` 会清，而后者只在主循环走到 `maze_done` 时才执行。

判据表
------
| 观测 | 含义 |
|---|---|
| STOP 后 `running_now()==False`（`mazeTask==WAIT` 且轮速 0） | ✅ 状态机退出了 |
| STOP 后只有轮速 0、`mazeTask` 仍 `SEARCH` | ❌ 卡在阻塞/空转里（本次修复的目标） |
| 第二次 `MAZE RUN` 回 `OK` | ✅ 跑位标志已复位，可重复发车 |
| 第二次 `MAZE RUN` 回 `ERR_BUSY` | ❌ `LINK_MazeFinished()` 没执行 |

安全
----
车**必须悬空或场地空旷** —— 本脚本真的发车（可能发两次）。
进 `finally` 一定走 `force_idle(assume_busy=True)`：分级收车 + `MCU RESET`。

用法:
    python stop_exit_probe.py COM7                     # 三步全跑
    python stop_exit_probe.py COM7 --hold 5            # 跑位观察窗 5 s
    python stop_exit_probe.py COM7 --no-relaunch       # 只验 1、2（不发第二次车）
"""
from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else ".")

from host.api import MouseClient          # noqa: E402
import v210_launch_pull_test as P         # noqa: E402


def snap(mc: MouseClient, tag: str):
    """取一帧**新鲜**遥测并打印关键量。"""
    try:
        mc.cmd.telem(timeout=1.0)
    except Exception:                     # 单帧失败不影响判读
        pass
    f = mc.telemetry.latest() or {}
    print(f"  {tag:<12} task={str(f.get('mazeTaskName')):<8} "
          f"motion={str(f.get('motionTaskName')):<20} "
          f"spd=({f.get('speedL', 0):+4d},{f.get('speedR', 0):+4d})  "
          f"free={f.get('freemode')}  pos=({f.get('posX')},{f.get('posY')})")
    return f


def wait_idle(mc: MouseClient, seconds: float):
    """等一个**非空且空闲**的新鲜帧。返回 (是否停住, 用时秒, 末帧)。"""
    t0 = time.time()
    while time.time() - t0 < max(0.0, seconds):
        try:
            f = P.fresh_frame(mc, mc.stream.mode, 1.2)
        except Exception:
            f = None
        if f and not P.running_now(f):
            return True, time.time() - t0, f
        time.sleep(0.15)
    try:
        f = P.fresh_frame(mc, mc.stream.mode, 1.2)
    except Exception:
        f = None
    return False, time.time() - t0, f


def main() -> int:
    ap = argparse.ArgumentParser(description="MAZE STOP 退出状态机三步验收")
    ap.add_argument("port", nargs="?", default="COM7")
    ap.add_argument("--hold", type=float, default=4.0, help="发车后跑位观察秒数")
    ap.add_argument("--stop-timeout", type=float, default=4.0,
                    help="MAZE STOP 后等待命的秒数（应为 1 秒级）")
    ap.add_argument("--no-relaunch", action="store_true", help="不做第二次发车（第③步）")
    args = ap.parse_args()

    mc = MouseClient(auto_session=False, auto_stream=True)
    if not mc.connect(args.port):
        print(f"连接 {args.port} 失败: {mc.state_detail}")
        return 1

    launched = False
    verdict = {}
    try:
        if not mc.telemetry.wait_first_frame(timeout=8.0):
            print("等首帧超时 —— 链路没起来，中止")
            return 1
        dev = mc.device or {}
        print(f"设备: proto={dev.get('proto')} fw={dev.get('fw')} "
              f"rst={dev.get('rst') or mc.reset_cause}  stream={mc.stream.mode}")

        # ---------------- 步骤 1：发车 ----------------
        print("\n=== ① MAZE RUN（车应悬空/空旷）===")
        ok, note = P.force_idle(mc, mc.stream.mode, verbose=False)
        print(f"  清场: {'待命' if ok else '未确认待命'}  ({note})")
        if not ok:
            print("  清场失败 —— 不发车。")
            return 2

        t = time.time()
        r = mc.maze.run("BLIND")
        launched = True
        print(f"  MAZE RUN -> {r.status}  ({(time.time() - t) * 1000:.0f} ms)")
        if not r.ok:
            print("  发车被拒 —— 后面两步无意义，直接收尾。")
            return 3

        moved = False
        t0 = time.time()
        while time.time() - t0 < args.hold:
            f = snap(mc, f"跑+{time.time() - t0:.1f}s")
            if P.running_now(f):
                moved = True
            time.sleep(max(0.0, 0.5 - 0.05))
        verdict["launch"] = moved
        print(f"  => {'✅ 真的发车了' if moved else '❌ 一步没走（发车未生效）'}")

        # ---------------- 步骤 2：MAZE STOP 能否自己停回待命 ----------------
        print(f"\n=== ② MAZE STOP（等待命 ≤ {args.stop_timeout:.1f}s）===")
        t = time.time()
        try:
            r = mc.maze.stop()
            print(f"  MAZE STOP -> {r.status}  ({(time.time() - t) * 1000:.0f} ms)")
        except Exception as e:            # noqa: BLE001
            print(f"  MAZE STOP 异常 {type(e).__name__}: {e}")

        idle, took, f = wait_idle(mc, args.stop_timeout)
        verdict["stop"] = idle
        if idle:
            print(f"  => ✅ {took:.1f}s 内自己回到待命（mazeTask={f.get('mazeTaskName')}）")
        else:
            mt = (f or {}).get("mazeTaskName")
            print(f"  => ❌ {took:.1f}s 内未回到待命（mazeTask={mt}）")
            if mt == "SEARCH":
                print("     mazeTask 仍 SEARCH ⇒ 主循环没走到 maze_done，"
                      "还有阻塞/空转点没清（看 objectGoTo 那条链之外的循环）")

        # ---------------- 步骤 3：能不能立刻再发一次车 ----------------
        if args.no_relaunch:
            print("\n=== ③ 跳过（--no-relaunch）===")
        else:
            print("\n=== ③ 立刻再发一次 MAZE RUN（验 LINK_MazeFinished 是否执行）===")
            t = time.time()
            r2 = mc.maze.run("BLIND")
            good = (r2.status or "").upper().endswith("OK")
            verdict["relaunch"] = good
            print(f"  MAZE RUN #2 -> {r2.status}  ({(time.time() - t) * 1000:.0f} ms)")
            if good:
                print("  => ✅ 跑位标志已复位，可重复发车")
                time.sleep(2.0)
                snap(mc, "跑2")
                try:
                    mc.maze.stop()
                except Exception:
                    pass
            else:
                print("  => ❌ 被拒 ⇒ LINK_MazeFinished() 未执行，状态机没真退出")

        # ---------------- 汇总 ----------------
        print("\n" + "=" * 70)
        print("汇总")
        print(f"  ① 发车真跑      : {'✅' if verdict.get('launch') else '❌'}")
        print(f"  ② STOP 自回待命 : {'✅' if verdict.get('stop') else '❌'}")
        if "relaunch" in verdict:
            print(f"  ③ 二次发车接受  : {'✅' if verdict['relaunch'] else '❌'}")
        all_ok = verdict.get("launch") and verdict.get("stop") and \
            verdict.get("relaunch", True)
        print(f"  => {'全部通过' if all_ok else '有未通过项，见上面逐条判读'}")
        return 0 if all_ok else 4
    finally:
        print("\n[安全出口] 分级收车 …")
        try:
            ok, note = P.force_idle(mc, mc.stream.mode, verbose=True,
                                    assume_busy=launched)
            print(f"[安全出口] {'已确认待命' if ok else '⚠️ 未确认待命，请人工断电'}: {note}")
        except Exception as e:            # noqa: BLE001
            print(f"[安全出口] 收车异常 {type(e).__name__}: {e} —— 请人工断电")
        try:
            mc.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
