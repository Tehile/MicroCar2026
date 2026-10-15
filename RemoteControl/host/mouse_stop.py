"""紧急收车(panic button): 把电脑鼠**立刻**带回待命态。

什么时候用: 探针 / 下游界面把车发出去了, 而脚本已经退出或崩了 —— **车还在转**。

与探针共用同一套分级收车逻辑(`v210_launch_pull_test.force_idle`):
    MAZE STOP  →  ESTOP(立刻砍电机)  →  MCU RESET(整机软复位)
每一步都用**新鲜帧**验证, 最后确认 `mazeTask = WAIT` / 轮速 ≈ 0。

为什么最后的 `MCU RESET` 不能省: `MAZE STOP`/`ESTOP` 只是置一个"请退出"标志,
而迷宫主循环是**非协作式阻塞等待**(等编码器里程/传感器), 标志常常轮不到 ⇒
常见"回 OK 但 task 仍在 SEARCH"; 并且该标志(`linkMazeAbort`)**全固件无处清除**,
不复位的话下一次发车会被 `LESSON_Blind8()` 进门第一次检查就返回(发车 OK 却不动)。

用法(在 host/ 目录下):
    python mouse_stop.py COM7              # 收车并确认待命
    python mouse_stop.py COM7 --no-reset   # 只到 STOP/ESTOP(不推荐)

⚠️ 本工具**只做收车、绝不发车**; 连不上会明确报错并提示人工断电。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import v210_launch_pull_test as probe  # noqa: E402
from host.api import MouseClient  # noqa: E402


def _state_line(mc: MouseClient) -> str:
    s = mc.maze.state() or {}
    f = probe.fresh_frame(mc, "off", 2.0)
    return (f"mazeTask={s.get('taskName', '?')} motion={s.get('motionName', '?')} "
            f"spd=({float(f.get('speedL', 0) or 0):+.0f},"
            f"{float(f.get('speedR', 0) or 0):+.0f}) "
            f"batt={int(f.get('battMv', 0) or 0)}mV")


def main() -> int:
    ap = argparse.ArgumentParser(description="紧急收车: 把电脑鼠带回待命态")
    ap.add_argument("port", help="串口, 如 COM7")
    ap.add_argument("--no-reset", action="store_true",
                    help="禁止 MCU RESET 兜底(不推荐: STOP/ESTOP 在阻塞等待里不可靠)")
    args = ap.parse_args()

    mc = MouseClient(auto_session=False, auto_stream=True)
    if not mc.connect(args.port):
        print(f"连接 {args.port} 失败: {mc.state_detail}")
        print("⚠️ 连不上就只能**人工断电**了。")
        return 1
    try:
        mc.telemetry.wait_first_frame(timeout=8.0)
        print(f"收车前: {_state_line(mc)}")
        ok, note = probe.force_idle(mc, "off", allow_reset=not args.no_reset,
                                    verbose=True)
        print(f"\n收车结果: {'已确认待命 ✅' if ok else '**未确认待命**'}: {note}")
        if not ok:
            print("⚠️ 请**立即人工断电**!")
            return 2
        print(f"收车后: {_state_line(mc)}")
        return 0
    finally:
        try:
            mc.stream.off()
        except Exception:
            pass
        try:
            mc.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
