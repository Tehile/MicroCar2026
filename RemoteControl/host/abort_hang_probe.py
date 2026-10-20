"""中止卡点取证：MAZE RUN 短跑后 MAZE STOP，若回不到待命就打印**全帧字段**定位卡在哪个运动任务。

用途
----
`stop_exit_probe.py` 断言"STOP 后能回 WAIT"，但现场发现它**不是每次都成立**：
短跑 3 s 就 STOP 时，偶尔 task 恒 SEARCH、速度恒 0，紧接的第二次 MAZE RUN 回 ERR_BUSY。
本探针专门用来复现这种"中止未生效"，并把卡住瞬间的整帧关键量打出来：

  · motionTaskName —— 卡在哪个运动原语（TURN / STRAIGHTEYECLOSE / TURNAROUND …）
  · step / track / tick —— SysTick 中止块把这三个推成极值（step=1, track=MAX, tick=60000），
    如果它们仍是正常值，说明中止块没跑到，或者循环条件用的是别的量
  · pos / gyro —— 是否还有推进；冻结 = 纯空转
  · freemode —— 1 表示已回值守循环（但**不可单独当证据**，LP_StopMotion 也会置 1）

用法:
    python abort_hang_probe.py COM7                 # 3 轮，每轮跑 3 s 后 STOP
    python abort_hang_probe.py COM7 --rounds 5 --hold 3
"""
from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else ".")

from host.api import MouseClient          # noqa: E402
import v210_launch_pull_test as P         # noqa: E402

KEYS = ("mazeTaskName", "motionTaskName", "speedL", "speedR", "freemode",
        "posX", "posY", "gyroAngle", "goaheadnum", "estop_lock", "flags",
        "objectGoToFlag", "spurtFlag",
        "spurtModeName", "roadName", "frontName",
        "irF1", "irF2", "irL", "irR", "trackL", "trackR")

SKIP = ("_rx_mono", "_seq")


def frame(mc: MouseClient):
    try:
        mc.cmd.telem(timeout=1.0)
    except Exception:                     # noqa: BLE001
        pass
    return mc.telemetry.latest() or {}


def line(f: dict) -> str:
    return " ".join(f"{k}={f.get(k)}" for k in KEYS if k in f)


def main() -> int:
    ap = argparse.ArgumentParser(description="中止卡点取证")
    ap.add_argument("port", nargs="?", default="COM7")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--hold", type=float, default=3.0, help="每轮跑位秒数")
    ap.add_argument("--stop-wait", type=float, default=8.0, help="STOP 后等待命秒数")
    args = ap.parse_args()

    mc = MouseClient(auto_session=False, auto_stream=True)
    if not mc.connect(args.port):
        print(f"连接失败: {mc.state_detail}")
        return 1
    mode = mc.stream.mode
    hangs = 0
    try:
        if not mc.telemetry.wait_first_frame(timeout=8.0):
            print("等首帧超时")
            return 1
        dev = mc.device or {}
        print(f"设备 fw={dev.get('fw')} proto={dev.get('proto')} rst={dev.get('rst') or mc.reset_cause}")
        print(f"帧字段: {sorted((frame(mc) or {}).keys())}\n")

        ok, note = P.force_idle(mc, mode, verbose=False)
        print(f"清场: {ok}  ({note})\n")

        for k in range(1, args.rounds + 1):
            r = mc.maze.run("BLIND")
            if not (r.status or "").upper().endswith("OK"):
                print(f"轮{k}: RUN -> {r.status}（被拒，说明上一轮没真退出 → 先收车再重来）")
                hangs += 1
                break
            t0 = time.time()
            moved_at, peak = None, 0
            while time.time() - t0 < args.hold:
                f = frame(mc)
                if P.running_now(f) and moved_at is None:
                    moved_at = time.time() - t0
                peak = max(peak, abs(f.get("speedL", 0)), abs(f.get("speedR", 0)))
                time.sleep(0.2)
            rs = mc.maze.stop()
            t1 = time.time()
            idle = False
            while time.time() - t1 < args.stop_wait:
                f = frame(mc)
                if not P.running_now(f):
                    idle = True
                    break
                time.sleep(0.2)
            took = time.time() - t1
            print(f"轮{k}: RUN={r.status} 起步={('%.1fs' % moved_at) if moved_at else '-'} 峰值={peak} "
                  f"| STOP={rs.status} 回待命={'%.1fs' % took if idle else '❌ 超时'}")
            if not idle:
                hangs += 1
                f1 = frame(mc)
                print(f"   ⚠ 卡住帧: {line(f1)}")
                time.sleep(1.5)
                f2 = frame(mc)
                moving = [k for k in sorted(f2) if k not in SKIP and f1.get(k) != f2.get(k)]
                print(f"   ⏱ 1.5s 后仍在变的量: {moving if moving else '（全部冻结 = 纯空转）'}")
                print(f"   motionTask={f2.get('motionTaskName')} objectGoToFlag={f2.get('objectGoToFlag')} "
                      f"spurtMode={f2.get('spurtModeName')} "
                      f"road={f2.get('roadName')} front={f2.get('frontName')}")
                print(f"   whole: { {k: v for k, v in sorted(f2.items()) if k not in SKIP} }")
                break
            print(f"   {line(frame(mc))}\n")

        print(f"\n结果: {hangs} 次未通过 / {args.rounds} 轮")
        return 0 if hangs == 0 else 4
    finally:
        print("\n[安全出口] 分级收车 …")
        try:
            ok, note = P.force_idle(mc, mode, verbose=True, assume_busy=True)
            print(f"[安全出口] {'已确认待命' if ok else '⚠️ 未确认，请人工断电'}: {note}")
        except Exception as e:            # noqa: BLE001
            print(f"[安全出口] 异常 {type(e).__name__}: {e} —— 请人工断电")
        try:
            mc.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
