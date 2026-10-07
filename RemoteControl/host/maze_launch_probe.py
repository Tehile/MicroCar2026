"""例程8（远程发车走迷宫）诊断与验收脚本。

覆盖三件事, 按需选择:
  · **只读体检**（默认）: 设备指纹 / 标定是否丢失 / 发车前标志位。
  · **发车行为**（--launch）: 发车后密集收帧, 看 speed / mazeTask / motionTask
    的时间序列 —— 用来定位"发车时快速后退"这类异常。
  · **发车循环验收**（--cycle, 与 --launch 同用或单独用）: 验证
        MAZE RUN → 已在跑时 ERR_BUSY
        MAZE STOP → 收车（**可能无效**, 见下）
        MCU RESET → **可靠收车**: 带回 WAIT
        再次 MAZE RUN → **应 OK**
        ESTOP → 锁存, 发车回 ERR_ESTOP
        STOP → 解锁, 再次 MAZE RUN → **应 OK**

⚠️ 为什么"收车"以 `MCU RESET` 为准, 而不是 `MAZE STOP`（2026-10-05 真机实测）:
    `MAZE STOP` 只置一个"请退出"标志(`linkMazeAbort`), 而主循环有 30 余处
    **非协作式阻塞等待**(`MOTION/motion.c` 的 `while (cellOdom < dis)` 等)。
    车在运动时程序正卡在那些循环**内部**, 而标志检查在 `LESSON_Blind8()` 的
    while(1) **顶部** —— 根本轮不到 ⇒ `MAZE STOP` 回 OK 却带不出迷宫,
    `MAZE RUN` 依旧 `ERR_BUSY`。**`MCU RESET` 整机软复位 (BSS 清零) 总能收车。**
    故 `--cycle` 把 MAZE STOP 记为"参考项"（如实报告是否真的收车了）,
    而把 MCU RESET 作为必过项。

背景（2026-10-05 真机复现 → 已修）:
    旧固件里 `MAZE RUN` 的忙判据是 `linkMazeReq || linkMazeRun`, 而 `linkMazeReq`
    置位后**只有上电才清零**、`LESSON_Blind8()` 又是 while(1) 不返回 ——
    于是发过一次车之后 `MAZE RUN` 永远回 ERR_BUSY, 且 `STOP` 也救不回来。
    新固件: 请求位在 main() 消费时清掉; `MAZE STOP` / `STOP` / `ESTOP` 都会让
    LESSON_Blind8() 返回, main() 随即复位发车状态。

安全约定:
    · 默认**只读**; 发车必须显式 `--launch`。
    · 发车前 5 秒倒计时, 期间 Ctrl-C 可中止。
    · 运动命令黑名单: 除发车/急停外不发任何会自走的命令。
    · ⚠️ 带 `--cycle` 会**多次发车**; 且 `MCU RESET` **只复位固件、不复位物理位置**
      —— 复位后须**人工把电脑鼠放回起点角**再让它跑, 否则会按错误位姿行驶
      （既不认路也不认墙）。

用法:
    python maze_launch_probe.py COM7                 # 只读体检
    python maze_launch_probe.py COM7 --launch        # 发车 + 观察（会自走!）
    python maze_launch_probe.py COM7 --cycle         # 只验收发车循环（需设备已空闲）
    python maze_launch_probe.py COM7 --launch --cycle
"""
from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, ".")

#: 会驱动电机的命令 —— 未经 --launch / --cycle 一律不发
MOTOR_CMDS = ("MOVE", "MAZE RUN", "MCU RESET", "CAL", "TARE", "ARM")

#: 固件源码里的生产参数默认值(见 ISR_Init / mouse_sim.PARAM_DEFAULTS)。
#: 读到的值与这些**完全相同** => 板上没有有效标定, 回落到了默认值。
SRC_DEFAULTS = {
    1: 62.59 * 0.85,   # TRACKL
    2: 62.68 * 0.85,   # TRACKR
    3: 62.59 * 0.85 / 100,  # SPEEDL (= TRACKL/100)
    4: 62.68 * 0.85 / 100,  # SPEEDR (= TRACKR/100)
}

RESULTS: list[tuple[str, bool, str]] = []


def rec(step: str, ok: bool, status: str, note: str = "") -> None:
    RESULTS.append((step, ok, status))
    print(f"    [{'OK' if ok else '--'}] {step:26s} -> {status}   {note}")


def watch(mc, seconds: float, label: str = "") -> None:
    """用拉取器全速收帧, 打印 speed / 任务 的时间序列。

    v2.1 是停等拉取, 实测约 4.4 Hz —— 对"快速后退"这类暂态**采样偏稀**,
    但足以看出任务机走到哪一步、速度有没有非零。
    """
    mc.stream.on()
    t0 = time.time()
    last_seq = None
    n = 0
    seen_tasks: list[str] = []
    moved = False
    print(f"      {'t(s)':>6}{'spdL':>7}{'spdR':>7}{'mazeTask':>11}{'motionTask':>18}"
          f"{'pos':>7}{'dir':>4}{'wallLFR':>9}{'free':>6}{'emerg':>7}")
    while time.time() - t0 < seconds:
        f = mc.telemetry.latest()
        if f and f.get("_seq") != last_seq:
            last_seq = f.get("_seq")
            n += 1
            pos = f"({f.get('posX')},{f.get('posY')})"
            wl = "".join("1" if f.get(k) else "0"
                         for k in ("wallLeft", "wallFront", "wallRight"))
            tn = str(f.get("mazeTaskName"))
            if not seen_tasks or seen_tasks[-1] != tn:
                seen_tasks.append(tn)
            if (f.get("speedL") or 0) or (f.get("speedR") or 0):
                moved = True
            print(f"      {time.time()-t0:>6.2f}{str(f.get('speedL')):>7}"
                  f"{str(f.get('speedR')):>7}{tn:>11}"
                  f"{str(f.get('motionTaskName')):>18}{pos:>7}"
                  f"{str(f.get('dir')):>4}{wl:>9}"
                  f"{str(f.get('freemode')):>6}{str(f.get('emerg')):>7}")
        time.sleep(0.02)
    mc.stream.off()

    # ---- 一眼判读 ----
    if n == 0:
        print("      判读: 一帧都没收到 —— 链路或固件有问题")
        return None
    print(f"      判读: {n} 帧 | mazeTask 序列 = {' → '.join(seen_tasks)} | "
          f"速度{'出现过非零' if moved else '**全程为 0**'}")
    if seen_tasks and seen_tasks[-1] == "WAIT" and not moved:
        print("      ⚠️ 仍是 WAIT 且速度全 0 —— 主循环没有进入迷宫"
              "（阻塞在发车等待之前的初始化里）")
    elif not moved:
        print("      ⚠️ 任务机离开了 WAIT 但速度全 0 —— 进了迷宫却没驱动电机")
    else:
        print("      ✅ 已真的走起来")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(prog="maze_launch_probe.py")
    ap.add_argument("port", nargs="?", default="COM7")
    ap.add_argument("--launch", action="store_true",
                    help="真的发 ARM + MAZE RUN（会让电脑鼠自走!）")
    ap.add_argument("--cycle", action="store_true",
                    help="验收发车循环: 重复发车被拒 / MAZE STOP 收车 / 收车后再发 / 急停解锁")
    ap.add_argument("--watch-s", type=float, default=3.0, help="发车后观察秒数")
    args = ap.parse_args()

    from host.api import MouseClient

    print("=" * 78)
    print(f"例程8 诊断/验收   端口={args.port}"
          f"{'  【会发车!】' if args.launch else '  (只读)'}"
          f"{'  [+发车循环验收]' if args.cycle else ''}")
    print("=" * 78)

    mc = MouseClient(auto_session=False, auto_stream=False)
    if not mc.connect(args.port):
        print("连接失败:", mc.state_detail)
        return 1

    try:
        # ---------------- 0) 设备体检 ----------------
        r = mc.send_cmd("ping", timeout=5.0)
        print(f"\n[0] 设备指纹  PING ok={r.ok}  device={mc.device}")
        if not r.ok:
            print("    !! PING 不通 —— LED/电池/链路问题, 退出")
            return 1
        if mc.device.get("proto") != 3:
            print(f"    ⚠ 协议号 {mc.device.get('proto')} != 3 —— 板上可能不是 v2.1 固件")
        fw = mc.device.get("fw") or 0
        if fw < 214:
            print(f"    ⚠ 固件 fw={fw} < 214 —— 板上不是最新 hex。各版本补齐的能力：")
            print("      212: main() 不再卡在'等前方红外就位'(MAZE RUN 被接受却永不执行)")
            print("      213: `MCU RESET` 远程软复位(卡死救援口, 不必断电)")
            print("      214: `MCU RESET` **复位前先停车**(事故修正); + 上位机发车低电闸")
            print("      请烧录 code_stm32f1_mouse/USER/Object/Mouse.hex")
        print(f"    上次复位原因 = {mc.reset_cause}")

        # ---------------- A) 标定是否丢失 ----------------
        print("\n[A] 标定体检：读生产参数, 与源码默认值比对")
        vals = mc.params.get_all(refresh=True)
        if not vals:
            print("    !! 没读到参数（GET ALL 无应答？）")
        else:
            print(f"    {'名称':<12}{'板上值':>14}{'源码默认':>14}   判定")
            default_hits = 0
            for pid, name in ((1, "TRACKL"), (2, "TRACKR"), (3, "SPEEDL"), (4, "SPEEDR")):
                got = vals.get(name)
                if got is None:
                    continue
                dflt = SRC_DEFAULTS.get(pid)
                same = dflt is not None and abs(got - dflt) < 1e-3
                if same:
                    default_hits += 1
                print(f"    {name:<12}{got:>14.5f}{(dflt if dflt else 0):>14.5f}   "
                      f"{'**= 源码默认值**' if same else '已标定(与默认不同)'}")
            print(f"\n    另有全部 {len(vals)} 项生产参数已读出。")
            if default_hits >= 3:
                print("    >>> **强烈指向标定丢失**：TRACKL/TRACKR/SPEEDL/SPEEDR 都等于源码默认值。")
                print("        v1.5.4 把标定页从 99/100 搬到 115/116，刷固件后板上旧标定读不到，")
                print("        会回落默认值（这是**预期行为**，不是 bug）。")
                print("        处理：重做一次标定并 `SAVE`（`mc.params.save()`），再试发车。")
            else:
                print("    >>> 标定看起来是有效值（未回落到默认）—— 标定丢失可以排除。")

        # ---------------- 1) 发车前状态 ----------------
        print("\n[1] 发车前状态")
        mc.stream.on()
        got = mc.telemetry.wait_first_frame(timeout=6.0)
        f = mc.telemetry.latest()
        mc.stream.off()
        if not got or not f:
            print("    !! 拉不到帧 —— 链路或固件有问题")
            return 1
        print(f"    freemode={f.get('freemode')}  emerg={f.get('emerg')}  "
              f"estop_lock={f.get('estop_lock')}  mazeTask={f.get('mazeTaskName')}  "
              f"motionTask={f.get('motionTaskName')}")
        print(f"    speedL={f.get('speedL')}  speedR={f.get('speedR')}  "
              f"battMv={f.get('battMv')}")

        if not (args.launch or args.cycle):
            print("\n[2] 未指定 --launch/--cycle —— 只读模式, 到此为止。")
            return 0

        # ================= 2) 发车 =================
        if args.launch:
            print("\n[2] ⚠ 即将发车：ARM + MAZE RUN —— 电脑鼠会自走")
            print("    5 秒后执行；现在 Ctrl-C 可中止")
            try:
                time.sleep(5)
            except KeyboardInterrupt:
                print("    已中止, 未发车。")
                return 0

            ra = mc.send_cmd("arm", timeout=5.0)
            rec("ARM", ra.ok, ra.status)
            t_send = time.time()
            rm = mc.send_cmd("maze.run", timeout=6.0)
            rec("MAZE RUN #1", rm.ok, rm.status, f"往返 {(time.time()-t_send)*1000:.0f}ms")
            if not rm.ok:
                print("\n    !! 发车被拒 —— 若为 ERR_BUSY 说明设备还停在上一轮迷宫里,")
                print("       先 `python -m host.cli maze stop --target <port>` 收车, 或断电。")
                return 1

            print(f"\n[3] 发车后观察 {args.watch_s:.0f}s（看速度/任务机序列）")
            watch(mc, args.watch_s)

        if not args.cycle:
            return 0

        # ================= 3) 收车路径验收 =================
        # ⚠️ 这里**不再把 MAZE STOP 当必过项**（v2.2.1 起，真机实测修正）。
        # 原因：`MAZE STOP` 只置一个"请退出"标志（`linkMazeAbort`），而主循环有
        # 30 余处**非协作式阻塞等待**（`MOTION/motion.c` 的 `while (cellOdom < dis)`
        # 等）。车在运动时程序正卡在那些循环**内部**，标志检查在 `LESSON_Blind8()`
        # 的 while(1) **顶部** —— 根本轮不到。于是现场表现为
        # "`MAZE STOP` 回 OK 却带不出迷宫，`MAZE RUN` 依旧 ERR_BUSY"。
        # → **可靠的收车手段是 `MCU RESET`**（整机软复位，BSS 清零把发车状态一起清掉）。
        print("\n[4] 收车路径验收（v2.2.1：以 MCU RESET 为可靠路径）")
        print("    ⚠ 后续会**发车**（电脑鼠自走），请确认它在起点角且留足空间。")

        r2 = mc.maze.run()
        rec("已在跑时再发车", not r2.ok and "BUSY" in r2.status, r2.status, "应 ERR_BUSY")

        # 4a) 常规收车：可能有效也可能无效（取决于程序是否恰在循环顶部）
        rs = mc.maze.stop()
        rec("MAZE STOP（常规收车）", rs.ok, rs.status, "应 OK")
        soft_ok = False
        if rs.ok:
            mc.wait(lambda: (mc.maze.state() or {}).get("taskName") == "WAIT", timeout=6.0)
            st = mc.maze.state() or {}
            soft_ok = st.get("taskName") == "WAIT"
            rec("  它是否真的收车了", soft_ok, str(st.get("taskName")),
                "车在运动中时通常**无效**(已知限制, 见本文件开头说明)")

        # 4b) 可靠收车：MCU RESET（复位只复位固件, 不复位物理位置）
        if not soft_ok:
            print("      -> 落到 MCU RESET 救援（这才是文档化的可靠收车路径）")
        res = mc.conn.soft_reset(verify=True, timeout=12.0)
        rec("★ MCU RESET 救援", bool(res.ok), res.status,
            f"rst={(res.data or {}).get('rst')}")
        # 复位后 main() 需要一点时间才执行到 `speedSt.freemode = 1`;
        # 而更关键的是: **PC 侧的 store 里还留着复位前的旧帧**, 且复位后新帧
        # 要 ~3s 才到（实测: t=0.22s 仍是旧帧, t=3.23s 才是复位后第一帧）。
        # 直接读会拿到"旧帧"或"main() 尚未初始化完的早帧", 两者都会误判。
        # 故**轮询等到真正的新鲜状态**为止。
        settle_deadline = time.time() + 8.0
        st, free = {}, None
        while time.time() < settle_deadline:
            time.sleep(0.4)
            mc.send_cmd("ping", timeout=8)
            if not mc.stream.is_on:
                mc.stream.on()
            mc.telemetry.wait_first_frame(timeout=4.0)
            st = mc.maze.state() or {}
            free = (mc.telemetry.latest() or {}).get("freemode")
            if st.get("taskName") == "WAIT" and free:
                break
        rec("★ 救援后回到 WAIT", st.get("taskName") == "WAIT",
            f"{st.get('taskName')}  (等到 t≈{8.0 - max(0.0, settle_deadline - time.time()):.1f}s)")
        rec("  救援后 freemode", str(free), "True = 电机输出被切断(静态)")

        # 4c) 再发车（复位把 linkMazeRun/linkMazeReq 随 BSS 一起清零）
        r3 = mc.maze.run()
        rec("★ 救援后再次发车", r3.ok, r3.status, "修复前此处永久 ERR_BUSY")

        # 4d) 急停语义
        re_ = mc.move.estop()
        rec("ESTOP", re_.ok, re_.status)
        r4 = mc.maze.run()
        rec("急停锁存中发车", not r4.ok and "ESTOP" in r4.status, r4.status, "应 ERR_ESTOP")

        rr = mc.move.resume()
        rec("STOP（解锁）", rr.ok, rr.status)
        r5 = mc.maze.run()
        rec("★ 解锁后再次发车", r5.ok, r5.status)

        mc.maze.stop()
        rec("收尾 MAZE STOP", True, "(已发出收车; 若仍 ERR_BUSY 用 MCU RESET 收)")

        rp = mc.send_cmd("ping", timeout=5.0)
        rec("PING（链路仍在）", rp.ok, rp.status)

        # ---------------- 结论 ----------------
        print("\n" + "=" * 78)
        print("验收结论")
        print("=" * 78)
        bad = [s for s, ok, _ in RESULTS if not ok]
        for s, ok, status in RESULTS:
            print(f"    {'[OK]' if ok else '[!!]'} {s:26s} {status}")
        print()
        if not bad:
            print("    >>> 全部通过：发车/收车/急停之后都能继续下达指令, 无需断电。")
        else:
            print(f"    >>> 未通过: {bad}")
    finally:
        try:
            mc.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
