"""发车链路最小验收 —— BLIND（无记忆走迷宫）与 RACE（正式比赛）各发一轮。

**目的**：只验证"发车链路是不是通的"，**不要求**跑完整迷宫、不判定成绩。
判据就三条：
  1. `MAZE RUN [BLIND|RACE]` 能拿到 `ACK <seq> OK`（命令被固件接受）；
  2. 发车后遥测里任务机**离开 WAIT**（说明 main() 真的消费了发车请求、进了迷宫）；
  3. 收车 / `MCU RESET` 之后**链路仍在**（`PING` 通），可以再发。

不判"是否跑完""是否走对"，遇到 `ERR_BUSY` / 卡死 / 不认路都算**如实记录**。

安全约定（沿用本项目既有纪律）：
  · 默认**只读**；发车必须显式 `--launch`；
  · 发车前 3 秒倒计时，`Ctrl-C` 可中止；
  · 每轮结束用 `MCU RESET` **可靠收车**（`MAZE STOP` 在车运动时可能无效，原因见
    `host/README.md` §1「收车」一节），复位前固件自己会先 `link_StopMotion()`；
  · ⚠️ **复位只复位固件、不复位物理位置** —— 复位后 `mousePos` 回 (0,0) 但车还在原处，
    下一轮发车前**须人工把车摆回起点角**，否则它按错误位姿行驶（不认路也不认墙）。

用法：
    python maze_launch_link_test.py COM7 --launch
    python maze_launch_link_test.py COM7 --launch --only BLIND
    python maze_launch_link_test.py COM7 --launch --observe-s 8
"""
from __future__ import annotations

import argparse
import re
import sys
import time

sys.path.insert(0, ".")

RESULTS: list[tuple[str, bool, str]] = []


def rec(step: str, ok: bool, status: str, note: str = "") -> None:
    RESULTS.append((step, ok, status))
    print(f"    [{'OK' if ok else '--'}] {step:<30} -> {status}   {note}")


def frame_line(f: dict) -> str:
    return (f"mazeTask={f.get('mazeTaskName')} motionTask={f.get('motionTaskName')} "
            f"pos=({f.get('posX')},{f.get('posY')}) dir={f.get('dir')} "
            f"spd=({f.get('speedL')},{f.get('speedR')}) "
            f"wall(front,left,right)=({f.get('frontName')},{f.get('wallLeft')},{f.get('wallRight')}) "
            f"freemode={f.get('freemode')}")


def observe(mc, seconds: float) -> dict:
    """拉取器全速收帧，返回判读结果。

    关注：任务机序列、是否离开 WAIT、速度是否出现过非零、位姿是否变化。
    """
    mc.stream.on()
    t0 = time.time()
    last_seq = None
    n = 0
    tasks: list[str] = []
    moved = False
    positions: list[tuple] = []
    print(f"      {'t(s)':>6}  mazeTask / motionTask / pos / spd / wall")
    while time.time() - t0 < seconds:
        f = mc.telemetry.latest()
        if f and f.get("_seq") != last_seq:
            last_seq = f.get("_seq")
            n += 1
            tn = str(f.get("mazeTaskName"))
            if not tasks or tasks[-1] != tn:
                tasks.append(tn)
            if (f.get("speedL") or 0) or (f.get("speedR") or 0):
                moved = True
            p = (f.get("posX"), f.get("posY"), f.get("dir"))
            if not positions or positions[-1] != p:
                positions.append(p)
            print(f"      {time.time()-t0:>6.2f}  {tn} / {f.get('motionTaskName')} / "
                  f"({f.get('posX')},{f.get('posY')}) / "
                  f"({f.get('speedL')},{f.get('speedR')}) / "
                  f"({f.get('frontName')},{f.get('wallLeft')},{f.get('wallRight')})")
        time.sleep(0.02)
    mc.stream.off()
    left_wait = bool(tasks) and any(t and t != "WAIT" for t in tasks)
    print(f"      → {n} 帧 | 任务机序列 = {'→'.join(tasks) if tasks else '(无)'} | "
          f"速度{'出现过非零' if moved else '全程 0'} | 位姿变化 {len(positions)} 个")
    return {"frames": n, "tasks": tasks, "left_wait": left_wait,
            "moved": moved, "positions": positions}


def settle_to_wait(mc, budget: float = 14.0) -> dict:
    """MCU RESET 后轮询等到真正新鲜的空闲状态（复位后新帧 ~3s 才到）。"""
    deadline = time.time() + budget
    st: dict = {}
    while time.time() < deadline:
        time.sleep(0.4)
        mc.send_cmd("ping", timeout=8)
        if not mc.stream.is_on:
            mc.stream.on()
        mc.telemetry.wait_first_frame(timeout=4.0)
        st = mc.maze.state() or {}
        free = (mc.telemetry.latest() or {}).get("freemode")
        if st.get("taskName") == "WAIT" and free:
            break
    mc.stream.off()
    return st


def launch_one(mc, target: str, observe_s: float, idx: int) -> None:
    print(f"\n{'=' * 78}")
    print(f"[{idx}] 发车目标 = {target}   ({'无记忆走迷宫' if target == 'BLIND' else '正式比赛'})")
    print("=" * 78)

    # --- 发车前 ---
    rp = mc.send_cmd("ping", timeout=5.0)
    rec(f"{target} 发车前 PING", rp.ok, rp.status, f"device={mc.device}")
    mc.stream.on()
    mc.telemetry.wait_first_frame(timeout=5.0)
    f = mc.telemetry.latest() or {}
    mc.stream.off()
    print(f"    发车前: {frame_line(f)}")
    pre_task = f.get("mazeTaskName")
    if pre_task != "WAIT":
        print(f"    ⚠ 发车前任务机 = {pre_task}（非 WAIT）—— 先 MCU RESET 收干净")
        mc.conn.soft_reset(verify=True, timeout=12.0)
        settle_to_wait(mc)

    # --- 发车 ---
    t_send = time.time()
    rm = mc.maze.run(target=target, timeout=6.0)
    rtt = (time.time() - t_send) * 1000
    rec(f"{target} MAZE RUN", rm.ok, rm.status, f"往返 {rtt:.0f}ms")

    if not rm.ok:
        print(f"    → 发车未被接受（{rm.status}）。可能是 ERR_BUSY/ERR_ESTOP/ERR_LOWBATT。")
        print("      对'链路是否通'的判定：**命令已送达并得到明确应答** ⇒ 链路通。")
        return

    # --- 观察 ---
    print(f"\n    发车后观察 {observe_s:.0f}s：")
    o = observe(mc, observe_s)
    rec(f"{target} 离开 WAIT",
        o["left_wait"], "→".join(o["tasks"]) if o["tasks"] else "(无帧)",
        "发车请求被 main() 消费的证据")
    rec(f"{target} 电机转动", o["moved"],
        "有非零速度" if o["moved"] else "全程 0",
        "(仅记录, 不作判据)")

    # --- 收车：MCU STOP（参考）+ MCU RESET（必过） ---
    print("\n    收车：")
    rs = mc.maze.stop()
    rec(f"{target} MAZE STOP", rs.ok, rs.status, "参考项, 运动中常无效")
    res = mc.conn.soft_reset(verify=True, timeout=12.0)
    rec(f"{target} ★ MCU RESET", bool(res.ok), res.status,
        f"rst={(res.data or {}).get('rst')}")
    st = settle_to_wait(mc)
    rec(f"{target} ★ 复位后回 WAIT", st.get("taskName") == "WAIT", str(st.get("taskName")))
    rp2 = mc.send_cmd("ping", timeout=5.0)
    rec(f"{target} 复位后链路仍在", rp2.ok, rp2.status, f"rst={mc.reset_cause}")


def main() -> int:
    ap = argparse.ArgumentParser(prog="maze_launch_link_test.py")
    ap.add_argument("port", nargs="?", default="COM7")
    ap.add_argument("--launch", action="store_true",
                    help="真的发车（会让电脑鼠自走！）")
    ap.add_argument("--only", choices=["BLIND", "RACE"], help="只测其中一个目标")
    ap.add_argument("--observe-s", type=float, default=6.0, help="每轮发车后观察秒数")
    ap.add_argument("--countdown", type=float, default=3.0, help="每次发车前的倒计时")
    args = ap.parse_args()

    targets = [args.only] if args.only else ["BLIND", "RACE"]

    print("=" * 78)
    print(f"发车链路最小验收  端口={args.port}  目标={targets}"
          f"{'  【会发车!】' if args.launch else '  (只读)'}")
    print("=" * 78)

    from host.api import MouseClient

    mc = MouseClient(auto_session=False, auto_stream=False)
    if not mc.connect(args.port):
        print("连接失败:", mc.state_detail)
        return 1

    try:
        rp = mc.send_cmd("ping", timeout=5.0)
        print(f"\n[0] 设备指纹 PING ok={rp.ok} device={mc.device}")
        if not rp.ok:
            print("    !! PING 不通，退出")
            return 1
        # 指纹与 link.h 现读版本比对
        try:
            from host import fwprofile  # 固件档位: 两代固件并存, RC_FW_PROFILE 切换
            ep, ef = fwprofile.fw_expect() or (None, None)
            if mc.device.get("proto") != ep or mc.device.get("fw") != ef:
                print(f"    ⚠ 板上 proto/fw = {mc.device.get('proto')}/{mc.device.get('fw')}，"
                      f"源码 = {ep}/{ef} —— 板上不是当前源码编的固件")
            else:
                print(f"    ✓ 与源码一致 (proto={ep} fw={ef})  "
                      f"txbuf={mc.device.get('txbuf')} rst={mc.reset_cause}")
        except Exception as e:
            print("    (版本比对跳过:", e, ")")

        if not args.launch:
            print("\n未指定 --launch —— 只读模式，到此为止。")
            return 0

        for i, tgt in enumerate(targets, start=1):
            if i > 1:
                print(f"\n⚠ 下一轮（{tgt}）前请确认**电脑鼠已摆回起点角**"
                      f"（上一轮 MCU RESET 只复位固件、不复位物理位置）。")
            print(f"\n    {args.countdown:.0f} 秒后发车 {tgt} …（Ctrl-C 可中止）")
            try:
                time.sleep(args.countdown)
            except KeyboardInterrupt:
                print("    已中止。")
                return 0
            launch_one(mc, tgt, args.observe_s, i)

        # ---------------- 结论 ----------------
        print("\n" + "=" * 78)
        print("验收结论")
        print("=" * 78)
        for s, ok, status in RESULTS:
            print(f"    {'[OK]' if ok else '[--]'} {s:<30} {status}")
        print()
        core = [r for r in RESULTS if "MAZE RUN" in r[0] or "离开 WAIT" in r[0]
                or "链路仍在" in r[0] or "回 WAIT" in r[0]]
        bad = [r[0] for r in core if not r[1]]
        if not bad:
            print("    >>> 发车链路判定：**通** —— 两个目标都被固件接受、任务机离开 WAIT、"
                  "收车后链路可继续使用。")
        else:
            print(f"    >>> 需关注项：{bad}（详见上方逐条记录）")
    finally:
        try:
            mc.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
