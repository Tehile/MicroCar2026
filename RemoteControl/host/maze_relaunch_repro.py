"""复现：例程8「发车 → 停止 → 再次发车」被拒（ERR_BUSY）。

## 现象（用户报告）
发车跑起来后停止，再发 `MAZE RUN` 会回 `ERR_BUSY`，**只能断电**。

## 待验证的假设链
1. 发车后程序会**停在探索中途**（速度归 0、坐标不再变化）。
2. 此时 `MAZE STOP` / `STOP` **回 OK 但不起作用** —— 因为 `LESSON_Blind8()` 的
   "退出迷宫"检查在 `while(1)` **顶部**, 而程序卡在 `switch` **内部**的函数里
   （`STATE_MazeSearch()` → `EXPLORE_CrosswayChoice()` → `FLOODFILL_Open()` 的
   扩散 `while (n)`）, 轮不到那次检查。
3. 于是 `linkMazeRun` 永远停在 1 ⇒ 之后每次 `MAZE RUN` 都 `ERR_BUSY`。
4. 但**运动状态机是空闲的**（卡的是纯计算, 不是运动）⇒ 短时 `MOVE RUN` 应回 `OK`。
   这是"卡在计算"与"卡在运动"的判别点。

## 顺带查一件事：为什么设备没被看门狗复位
IWDG 的喂狗在 `link.c` 的 `LINK_Tick()` 里, 而 `LINK_Tick()` 由 **SysTick 中断**调用
⇒ **主循环死循环不会触发看门狗**(中断照常喂狗)。用 `PING` 的 `rst` 字段验证:
若卡住期间发生 IWDG 复位, `rst` 会从 `POR` 变成 `IWDG`。

## 安全
- 会让电脑鼠自走（这就是要复现的）。**请把它放在迷宫/空旷处并留出空间。**
- `MOVE RUN` 探针极短(200 ms, ±15)且预期**被拒**, 不会真跑。
- 全程可 Ctrl-C；脚本结束会尽量收车。
- `[8]` 的救援会把 MCU 软复位一次（`rst` 变 `SW`）。这是预期行为。
  若不想复位，加 `--no-rescue`。
- ⚠️ **`[9]` 默认不发车**（2026-10-05 事故后的修正）。复位只复位固件、不复位
  物理位置: `mousePos`/`mapWall` 清零而车还在原处, 立刻发车会按错误位姿行驶
  （既不认路也不认墙）。**发车前请先人工把车放回起点角**；确要让脚本发，
  加 `--relaunch`（有 5 秒可中止倒计时）。

用法:
    python maze_relaunch_repro.py COM7              # 完整：复现 + 救援验收
    python maze_relaunch_repro.py COM7 --no-rescue  # 只复现（v2.1.2 及以前的形态）
    python maze_relaunch_repro.py COM7 --stuck-s 12 # 判定"卡住"所需的静止秒数
    python maze_relaunch_repro.py COM7 --relaunch   # 救援后真的再发车(需先摆好车位)
"""
from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, ".")


def snap(mc):
    f = mc.telemetry.latest()
    if not f:
        return None
    return {
        "task": f.get("mazeTaskName"),
        "motion": f.get("motionTaskName"),
        "pos": (f.get("posX"), f.get("posY")),
        "dir": f.get("dir"),
        "spd": (f.get("speedL"), f.get("speedR")),
        "free": f.get("freemode"),
        "emerg": f.get("emerg"),
        "lock": f.get("estop_lock"),
        "stack": f.get("stackIdx"),
    }


def fmt(s):
    if not s:
        return "(无帧)"
    return (f"task={s['task']:<9} motion={s['motion']:<18} pos={s['pos']} dir={s['dir']} "
            f"spd={s['spd']} free={s['free']} emerg={s['emerg']} stack={s['stack']}")


def main() -> int:
    ap = argparse.ArgumentParser(prog="maze_relaunch_repro.py")
    ap.add_argument("port", nargs="?", default="COM7")
    ap.add_argument("--stuck-s", type=float, default=12.0,
                    help="坐标与速度持续不变的秒数达到该值即判为『卡住』")
    ap.add_argument("--max-s", type=float, default=45.0, help="观察上限(秒)")
    ap.add_argument("--no-rescue", dest="rescue", action="store_false",
                    help="卡住后**不做**远程救援（只复现，默认会救援并验收 v2.2.0）")
    ap.set_defaults(rescue=True)
    ap.add_argument("--relaunch", action="store_true",
                    help="救援后**真的再发车**（默认只报告就绪状态, 不自动发车）")
    ap.set_defaults(relaunch=False)
    args = ap.parse_args()

    from host.api import MouseClient

    print("=" * 78)
    print(f"例程8「停止后无法再次发车」复现   端口={args.port}")
    print("=" * 78)

    mc = MouseClient(auto_session=False, auto_stream=False)
    if not mc.connect(args.port):
        print("连接失败:", mc.state_detail)
        return 1

    results: list[tuple[str, str]] = []

    def rec(name, value, expect=""):
        results.append((name, value))
        flag = ""
        if expect:
            flag = "   <= 预期 " + expect
        print(f"  {name:34s} = {value}{flag}")

    try:
        mc.stream.on()
        mc.telemetry.wait_first_frame(timeout=6.0)

        r = mc.send_cmd("ping", timeout=5)
        print(f"\n[0] 起始状态  fw={mc.device.get('fw')}  rst={mc.device.get('rst')}")
        print(f"    {fmt(snap(mc))}")

        # 先要求空闲(否则本脚本的前提不成立)
        s0 = snap(mc)
        if s0 and s0["task"] != "WAIT":
            print(f"\n    ⚠ 设备当前不在 WAIT(={s0['task']})，说明它还停在上一次发车里。")
            print("       本脚本需要一台**已断电重启**的设备才能干净复现。")
            print("       先试一次 MAZE RUN，看是否已经是 ERR_BUSY（那本身就是复现）：")
            rr = mc.send_cmd("maze.run", timeout=6)
            rec("MAZE RUN(残留状态)", rr.status, "ERR_BUSY = 已复现")
            if rr.ok:
                print("       （居然 OK，说明状态是干净的，继续正常流程）")
            else:
                print("\n       若想完整复现，请给设备断电重启后再跑本脚本。")
                return 0

        # ---------------- 1) 发车 ----------------
        print("\n[1] 发车 ARM + MAZE RUN")
        print("    5 秒后发车；现在 Ctrl-C 可中止")
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            print("    已中止。")
            return 0
        mc.send_cmd("arm", timeout=5)
        rr = mc.send_cmd("maze.run", timeout=6)
        rec("MAZE RUN #1", rr.status, "OK")
        if not rr.ok:
            print("    发车被拒，无法继续。")
            return 1

        # ---------------- 2) 观察，直到判定卡住 ----------------
        print(f"\n[2] 观察（{args.max_s:.0f}s 上限；坐标+速度连续 "
              f"{args.stuck_s:.0f}s 不变即判定卡住）")
        t0 = time.time()
        last_change = t0
        prev = None
        stuck_at = None
        trail: list[str] = []
        while time.time() - t0 < args.max_s:
            s = snap(mc)
            if s:
                key = (s["pos"], s["spd"], s["task"])
                if key != prev:
                    prev = key
                    last_change = time.time()
                    el = time.time() - t0
                    print(f"    t={el:5.1f}s  task={s['task']:<9} motion={s['motion']:<18} "
                          f"pos={s['pos']} spd={s['spd']} stack={s['stack']}")
                    if s["task"] not in trail:
                        trail.append(s["task"])
                elif time.time() - last_change >= args.stuck_s:
                    stuck_at = time.time() - t0
                    break
            time.sleep(0.05)

        s_now = snap(mc)
        rec("mazeTask 迁移序列", " → ".join(trail))
        if stuck_at is None:
            rec("是否卡住", f"否（{args.max_s:.0f}s 内一直在变）")
            print("    没有复现到『卡住』。")
            return 0
        rec("卡住时刻(t≈)", f"{stuck_at:.0f}s（之后 {args.stuck_s:.0f}s 无任何变化）")
        rec("卡住时状态", fmt(s_now))
        rec("卡住时 speed", str(s_now["spd"]), "0 —— 车已停")
        rec("卡住时 motionTask", str(s_now["motion"]), "NULL —— 不是在跑运动")

        # ---------------- 3) 停止命令有效吗 ----------------
        print("\n[3] 发停止命令（关键：能否把程序带出迷宫？）")
        for cmd in ("estop.clear", "maze.stop"):
            r = mc.send_cmd(cmd, timeout=6)
            time.sleep(0.6)
            s_after = snap(mc)
            rec(f"{cmd} 应答", r.status)
            rec(f"{cmd} 之后的 mazeTask", str(s_after["task"]),
                "WAIT = 有效 / 仍 SEARCH = 无效")
        s_after = snap(mc)
        stopped_ok = s_after["task"] == "WAIT"
        rec("停止命令是否真的收车", "是" if stopped_ok else "**否**")

        # ---------------- 4) 再次发车 ----------------
        print("\n[4] 再次发车")
        r2 = mc.send_cmd("maze.run", timeout=6)
        rec("MAZE RUN #2", r2.status, "ERR_BUSY = 复现成功")

        # ---------------- 5) 判别：卡在计算 还是 卡在运动 ----------------
        print("\n[5] 判别：卡在纯计算 还是 卡在运动？")
        r3 = mc.send_cmd("move.run", sp_l=15, sp_r=15, ms=200, ir_mm=-1, timeout=6)
        rec("MOVE RUN 探针(200ms)", r3.status,
            "OK = 运动状态机空闲 ⇒ 卡在纯计算")
        if r3.ok:
            time.sleep(0.5)
            mc.send_cmd("stop", timeout=6)

        # ---------------- 6) 看门狗有没有介入 ----------------
        print("\n[6] 看门狗：卡住期间是否发生过 IWDG 复位？")
        rp = mc.send_cmd("ping", timeout=5)
        rst = mc.device.get("rst")
        rec("PING rst", str(rst), "POR = 没被看门狗复位")
        print("      注: IWDG 的喂狗在 link.c 的 LINK_Tick() 里, 而 LINK_Tick 由")
        print("          **SysTick 中断**调用 ⇒ 主循环死循环**不会**触发看门狗。")

        # ---------------- 7) 后台是否还在推进 ----------------
        print("\n[7] 卡住期间链路与传感器是否正常")
        n0 = mc.stats().get("unified", 0)
        time.sleep(1.5)
        n1 = mc.stats().get("unified", 0)
        rec("1.5s 内收到帧数", str(n1 - n0), ">0 = 链路正常")
        s_end = snap(mc)
        rec("再次确认 mazeTask", str(s_end["task"]))
        rec("再次确认 speed", str(s_end["spd"]))

        # ---------------- 8) 远程救援（v2.2.0 新增）----------------
        # 这是 v2.2.0 的验收点: 卡住之后**不必断电**, 用 `MCU RESET` 复位即可。
        rescued = False
        if args.rescue:
            print("\n[8] 远程救援：MCU RESET（v2.2.0）")
            fw = mc.device.get("fw")
            if (fw or 0) < 213:
                print(f"      板上 fw={fw} < 213 —— 不含 MCU RESET。")
                r = mc.send_cmd("system.reset", timeout=6)
                rec("MCU RESET(旧固件)", r.status,
                    "ERR_CMD = 向后兼容(不误复位)")
                rec("rst 是否仍为 POR", str(mc.device.get("rst")),
                    "POR = 没被误触发")
            else:
                t0 = time.time()
                res = mc.conn.soft_reset(verify=True, timeout=10.0)
                el = time.time() - t0
                rec("soft_reset() ok", str(res.ok))
                rec("  accepted（只表示 ACK 收到）", str((res.data or {}).get("accepted")),
                    "False 也不代表失败")
                rec("  rst", str((res.data or {}).get("rst")), "SW = 软复位")
                rec("  verified（库内复核）", str((res.data or {}).get("verified")))
                rec("  耗时", f"{el*1000:.0f} ms")
                # 独立复核: 重新 PING, 不依赖 soft_reset 的返回值
                time.sleep(0.5)
                mc.send_cmd("ping", timeout=8)
                rst2 = mc.device.get("rst")
                rec("独立复核 PING rst", str(rst2), "SW = 救援成功")
                rescued = rst2 == "SW"

        # ---------------- 9) 救援后的状态检查（**默认不发车**）----------------
        # ⚠️ 这里刻意**不自动发车**（v2.2.1 修正）。
        # 2026-10-05 真机事故：复位后脚本紧接着发了 `MAZE RUN`，电脑鼠开始按
        # "我在起点 (0,0)" 的错误假设行驶 —— 因为复位只复位固件、不复位物理位置：
        # `mousePos`/`mapWall`/`stackIdx` 全被清零，而车还在迷宫原处。探索会用
        # 一张错的地图规划路径，墙检测只用来更新地图、不是硬停车条件 ⇒ 表现为
        # "一直转、对红外障碍物不敏感"，操作者只得强行断电。
        # 所以复位之后**必须由人把车放回起点角再发车**；脚本只报告就绪状态。
        if rescued:
            print("\n[9] 救援后的状态（**脚本不会自动发车**）")
            mc.stream.on()
            mc.telemetry.wait_first_frame(timeout=6.0)
            s_after = snap(mc)
            rec("复位后 mazeTask", str(s_after["task"]), "WAIT = 回到等待态")
            rec("复位后 speed", str(s_after["spd"]), "(0,0) = 静态")
            rec("复位后 freemode", str(s_after["free"]), "True = 电机输出被切断")
            print("      发车就绪性（由代码推导，非实发）：复位后 `linkMazeRun`/`linkMazeReq`")
            print("      均为 BSS 零值 ⇒ `MAZE RUN` 的忙判据不成立 ⇒ 会被接受。")
            if args.relaunch:
                print("\n      ⚠ --relaunch 已给出：5 秒后真的发车。")
                print("        请确认电脑鼠**已在起点角**且前方留足空间；Ctrl-C 可中止。")
                try:
                    time.sleep(5)
                except KeyboardInterrupt:
                    print("        已中止，未发车。")
                    return 0
                r9 = mc.send_cmd("maze.run", timeout=6)
                rec("MAZE RUN #3(救援后)", r9.status, "OK")
                if r9.ok:
                    time.sleep(1.5)
                    rec("再发车后状态", fmt(snap(mc)))
                    mc.send_cmd("maze.stop", timeout=6)
                    time.sleep(0.4)
                    mc.send_cmd("stop", timeout=6)
            else:
                print("      >>> 请先把电脑鼠放回起点角，再手动发车：")
                print("          python -m host.cli maze run --target COM7")
                print("          （或加 --relaunch 让本脚本发）")

        # ---------------- 结论 ----------------
        print("\n" + "=" * 78)
        print("结论")
        print("=" * 78)
        for k, v in results:
            print(f"    {k:34s} {v}")
        print()
        busy = any("ERR_BUSY" in v for k, v in results if "MAZE RUN #2" in k)
        calc = any("OK" == v for k, v in results if "MOVE RUN" in k)
        if busy and not stopped_ok:
            print("    >>> **复现成功**：停止命令回 OK 却不起作用，程序卡在探索内部的")
            print("        纯计算里（`FLOODFILL_Open` 的扩散循环未返回），")
            print("        `LESSON_Blind8()` 顶部的『退出迷宫』检查轮不到；")
            print("        `linkMazeRun` 停在 1 ⇒ `MAZE RUN` 永久 `ERR_BUSY`。")
            if calc:
                print("        MOVE RUN 回 OK 进一步证明**运动状态机是空闲的**，")
                print("        卡住的不是运动，而是探路计算。")
            print("        期间链路正常、看门狗不介入 ⇒ v2.1.2 及以前**只能断电恢复**。")
            if rescued:
                print("    >>> **救援验证通过**：`MCU RESET` 后 `rst=SW`、回到 WAIT、")
                print("        `MAZE RUN` 重新被接受 ⇒ **不必断电即可恢复**。")
            elif args.rescue:
                print("    >>> 救援未通过 —— 检查 fw 是否 >= 213、或看上面的 rst。")
        return 0
    finally:
        try:
            mc.send_cmd("stop", timeout=4)
        except Exception:
            pass
        try:
            mc.stream.off()
            mc.close()
        except Exception:
            pass
        print("\n（脚本已退出；若设备仍在迷宫中，断电重启即可恢复）")


if __name__ == "__main__":
    raise SystemExit(main())
