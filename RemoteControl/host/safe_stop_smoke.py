"""离线冒烟: **收车安全逻辑**(不接设备, 不发任何真实命令)。

被测对象是 `v210_launch_pull_test.force_idle()` —— 它决定了"脚本退出时电脑鼠
一定处于待命态"这条硬保证。2026-10-16 真机事故的复盘结论:

  · 收车判据**必须读新鲜帧**; 读 `telemetry.latest()` 的缓存会把"车在跑"看成"没跑"
    ⇒ 于是跳过收车、车一直转到脚本结束后;
  · `MAZE STOP` 在迷宫主循环的**非协作式阻塞等待**里不可靠(registry 原话:
    "常见现象是回 OK 但 task 仍在 SEARCH; 真正可靠的收车是 `system.reset`");
  · `MAZE STOP`/`ESTOP` 置的 `linkMazeAbort` **全固件无处清除** ⇒ 只要发过它们,
    就必须补一次 `MCU RESET`, 否则下一次发车被 `LESSON_Blind8()` 立刻返回。

  · 收车途中**任何一条命令抛异常**都不得中断分级收车 —— 真机形态: `MCU RESET` 已生效,
    但 `send_cmd` 收尾的让路恢复(重发 `RATE PULL`)撞上固件 ~3.2s 重启窗口而超时抛异常;
    该异常曾一路逃逸把整个收车流程打断(脚本崩、车停在半途)。

本测试用桩对象把四条升级路径全跑一遍, 并**反向验证**: 怎么都停不住时必须
诚实报"未确认待命", 绝不许谎报成功; 以及 `assume_busy` 时不得走"本来就待命"快捷路径。

跑法(host/ 目录下): `python safe_stop_smoke.py`
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import v210_launch_pull_test as P  # noqa: E402

_N = 0
_FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _N, _FAIL
    _N += 1
    if cond:
        print(f"  ok   {name}")
    else:
        _FAIL += 1
        print(f"  FAIL {name}  {detail}")


class Cmd:
    """模仿 `CommandClient.telem` 的**真签名**(形参只有 timeout)。

    旧探针传了 `wait=True` ⇒ TypeError 被静默吞掉 ⇒ 遥测永不刷新。
    这里保留真签名, 谁再传 `wait` 就会在本测试里炸出来。
    """

    def __init__(self, mc) -> None:
        self.mc = mc
        self.telem_calls = 0

    def telem(self, timeout: float = 3.0):
        self.telem_calls += 1
        return NS(status="OK")


class FakeMC:
    """桩: stopped 决定"车是否在跑"; mode 决定哪一步命令真的能停住它。"""

    def __init__(self, mode: str) -> None:
        self.mode = mode          # maze_stop_works/estop_works/reset_only/never
        self.log: list = []
        self.stopped = False
        self.store = NS(unified=NS(total=0))
        self.cmd = Cmd(self)
        self.telemetry = NS(latest=self._latest)
        self.maze = NS(stop=self._stop)

    def _latest(self):
        self.store.unified.total += 1
        run = not self.stopped
        return {"mazeTask": 2 if run else 0, "motionTask": 1 if run else 0,
                "speedL": 22.0 if run else 0.0, "speedR": 22.0 if run else 0.0}

    def _stop(self):
        self.log.append("MAZE STOP")
        if self.mode == "maze_stop_works":
            self.stopped = True
        return NS(status="OK")

    def send_cmd(self, name, timeout=None, **kw):
        self.log.append(name)
        if name == "estop" and self.mode == "estop_works":
            self.stopped = True
        if name == "system.reset" and self.mode != "never":
            self.stopped = True
        if self.mode == "raise_all":
            # 真机事故 2026-10-16 的形态: `MCU RESET` **已生效**(固件先 ACK 再复位),
            # 但 `send_cmd` 收尾的让路恢复(重发 `RATE PULL`)撞上固件重启窗口而抛异常。
            # 收车流程**必须**能穿过这种异常继续, 并照样确认待命。
            if name == "estop":
                raise RuntimeError("模拟 ESTOP 命令异常")
            if name == "system.reset":
                self.stopped = True
                raise RuntimeError("模拟 MCU RESET 后让路恢复超时")
        return NS(status="OK")


def main() -> int:
    print("safe_stop_smoke —— 收车安全逻辑(离线, 不接设备)")

    # T1 本来就待命: 必须**一条命令都不发**(避免用 MAZE STOP 白污染 abort 位)
    mc = FakeMC("never")
    mc.stopped = True
    ok, _note = P.force_idle(mc, "telem", verbose=False)
    check("T1 本来就待命 -> 不发任何命令", ok and mc.log == [], f"log={mc.log}")

    # T2 MAZE STOP 有效: 停住后仍**必须补 MCU RESET**(清粘滞的 linkMazeAbort)
    mc = FakeMC("maze_stop_works")
    ok, _note = P.force_idle(mc, "telem", verbose=False)
    check("T2 MAZE STOP 有效 -> 补 MCU RESET 清 abort",
          ok and mc.log[:1] == ["MAZE STOP"] and "system.reset" in mc.log, f"log={mc.log}")

    # T3 MAZE STOP 不停 -> 升级 ESTOP -> 再补 MCU RESET
    mc = FakeMC("estop_works")
    ok, _note = P.force_idle(mc, "telem", verbose=False,
                             stop_timeout=0.4, estop_timeout=0.4)
    check("T3 STOP 不停 -> ESTOP -> MCU RESET",
          ok and mc.log == ["MAZE STOP", "estop", "system.reset"], f"log={mc.log}")

    # T4 只有 MCU RESET 能停 -> 仍判定待命
    mc = FakeMC("reset_only")
    ok, _note = P.force_idle(mc, "telem", verbose=False,
                             stop_timeout=0.4, estop_timeout=0.4)
    check("T4 仅 MCU RESET 能停 -> 判定待命",
          ok and "system.reset" in mc.log, f"log={mc.log}")

    # T5 怎么都停不住 -> 必须**报未确认**(要求人工断电), 不许谎报成功
    mc = FakeMC("never")
    ok, note = P.force_idle(mc, "telem", verbose=False,
                            stop_timeout=0.4, estop_timeout=0.4)
    check("T5 停不住 -> 诚实报未确认(提示断电)", ok is False and "断电" in note,
          f"ok={ok} note={note}")

    # T6 --no-reset: 不升级到复位, 且**如实说明未复位**
    mc = FakeMC("reset_only")
    ok, note = P.force_idle(mc, "telem", verbose=False, allow_reset=False,
                            stop_timeout=0.4, estop_timeout=0.4)
    check("T6 --no-reset -> 不发复位且标注未复位",
          "system.reset" not in mc.log and "禁止复位" not in note
          and _note_has_no_reset(note), f"log={mc.log} note={note}")

    # T7 off 工况取帧必须走真签名, 且真的刷新到新值
    mc = FakeMC("never")
    mc.stopped = True
    f = P.fresh_frame(mc, "off", 1.0)
    check("T7 off 取帧: telem(timeout=) 且拿到新帧",
          mc.cmd.telem_calls >= 1 and int(f.get("mazeTask", 9)) == 0,
          f"calls={mc.cmd.telem_calls} f={f}")

    # T8 陈旧帧陷阱对照: running_now 只看帧内容, 必须能分辨跑/停
    mc = FakeMC("never")
    check("T8 跑时 running_now=True",
          P.running_now(P.fresh_frame(mc, "telem", 0.3)) is True)
    mc.stopped = True
    check("T8 停时 running_now=False",
          P.running_now(P.fresh_frame(mc, "telem", 0.3)) is False)

    # T9 命令抛异常(模拟 MCU RESET 后让路恢复超时) -> 收车**不得中断**, 仍须确认待命
    mc = FakeMC("raise_all")
    ok, note = P.force_idle(mc, "telem", verbose=False,
                            stop_timeout=0.4, estop_timeout=0.4)
    check("T9 命令抛异常 -> 收车不中断且确认待命",
          ok is True and "system.reset" in mc.log, f"ok={ok} note={note} log={mc.log}")

    # T10 assume_busy=True: 即使帧上看着待命, 也必须走分级收车(不赌"本来就待命")
    mc = FakeMC("maze_stop_works")
    mc.stopped = True
    ok, _note = P.force_idle(mc, "telem", verbose=False, assume_busy=True)
    check("T10 assume_busy 跳过快捷路径 -> 发 MAZE STOP + RESET",
          ok and mc.log[:1] == ["MAZE STOP"] and "system.reset" in mc.log,
          f"log={mc.log}")

    # T11 默认(assume_busy=False)仍走快捷路径: 看起来待命就一条命令都不发
    mc = FakeMC("maze_stop_works")
    mc.stopped = True
    ok, _note = P.force_idle(mc, "telem", verbose=False)
    check("T11 默认待命 -> 仍不发任何命令", ok and mc.log == [], f"log={mc.log}")

    # T12 链路读不到帧(空帧) -> **绝不能谎报"待命"**(2026-10-16 真机踩到: 设备掉线时
    #     安全出口却打印"已确认待命" —— 因为空帧被 running_now 当成了"空闲")
    class DeadMC(FakeMC):
        def _latest(self):
            return {}          # 链路不通: 没有任何帧

    mc = DeadMC("never")
    ok, note = P.force_idle(mc, "telem", verbose=False,
                            stop_timeout=0.3, estop_timeout=0.3)
    check("T12 链路不通(空帧) -> 诚实报未确认(提示断电), 不许谎报待命",
          ok is False and "断电" in note, f"ok={ok} note={note}")

    print(f"\n  {_N - _FAIL}/{_N} passed")
    if _FAIL:
        print("  ❌ FAILED")
        return 1
    print("  ✅ safe_stop_smoke 全绿")
    return 0


def _note_has_no_reset(note: str) -> bool:
    """--no-reset 时说明里应标出"未复位"。"""
    return "未复位" in note


if __name__ == "__main__":
    sys.exit(main())
