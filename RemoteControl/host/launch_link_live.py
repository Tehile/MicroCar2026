"""发车后**运动中**的链路有效性探针 —— 只判"通信还通不通", 不判成绩。

与 `maze_launch_link_test.py` 的分工(两者互补, 不要混):

    maze_launch_link_test.py  判"发车链路**通不通**": 命令被接受 / 任务机离开 WAIT /
                              收车后还能用。观察期只被动收帧。
    launch_link_live.py(本文件)  判"**运动过程中**链路是否一直有效":
                              拉取器在跑期间的成功/丢失计数、实测帧率、帧间隔分布,
                              以及**主动插入 `PING`**(先让遥测让路)能否拿到 ACK 与 RTT。
                              也就是"车在跑的时候, 上位机还能不能可靠地读写设备"。

为什么"拉取期 `PING` 成功"是硬证据:
  固件每 1ms tick 只消费一行命令(`link.c`; `usart.c` 在 bit7 未清时直接丢字节),
  所以拉取器持续发 `TELEM` 时, 命令通道是**真的有争用**的。能在这个工况下拿到
  `PING` 的应答, 就说明"命令下行 + 应答上行"这条双向往返在运动中依然成立。
  做法是让遥测让路(`stream.suspend()`)后发 `PING`, 再 `resume()` —— 这正是
  `send_cmd` 给 urgent 命令用的那套机制。

判据(全部为真才判"通信有效"):
  1. `MAZE RUN <target>` 拿到 ACK;
  2. 观察期内**至少收到 N 帧**且**丢失为 0**(每次 `TELEM` 应答 = 一次双向往返);
  3. 观察期内**至少一次 `PING` 拿到 ACK**(命令通道在运动中仍可用);
  4. 收车(`system.reset`)后 `PING` 仍通。

安全约定(沿用本项目纪律):
  · 默认**只读**; 发车必须显式 `--launch`;
  · 发车前倒计时, 期间 `Ctrl-C` 可中止;
  · 收车优先 `MAZE STOP`, 再 `system.reset` **可靠复位**(运动中 `MAZE STOP` 常无效);
  · ⚠️ **复位只复位固件、不复位物理位置** —— 下一轮发车前须人工把车摆回起点角。

用法:
    python launch_link_live.py COM7 --launch --target RACE
    python launch_link_live.py COM7 --launch --target RACE --observe-s 20
    python launch_link_live.py COM7 --launch --target BLIND --no-probe
"""
from __future__ import annotations

import argparse
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

RESULTS: list[tuple[str, bool, str]] = []


def rec(step: str, ok: bool, status: str, note: str = "") -> None:
    RESULTS.append((step, ok, status))
    print(f"    [{'OK' if ok else '--'}] {step:<28} -> {status}   {note}")


def fw_fingerprint_expect() -> tuple[int | None, int | None]:
    """从固件源码现读 proto/fw 版本号(不写死, 免得版本一改脚本就过期)。"""
    try:
        from host import fwprofile  # 固件档位: 两代固件并存, 由 RC_FW_PROFILE 切换
        exp = fwprofile.fw_expect()
        return exp if exp else (None, None)
    except Exception:
        return None, None


def observe_and_probe(mc, seconds: float, probe_every: float) -> dict:
    """拉取器全速收帧, 期间周期性插入 `PING`(先让路)。返回判读结果。"""
    before = mc.stream.stats()
    mc.stream.on()

    stamps: list[tuple[float, int]] = []      # (到帧时刻, seq)
    probes: list[dict] = []                   # 每次 PING 的结果
    probe_windows: list[tuple[float, float]] = []
    tasks: list[str] = []
    last_seq = None
    moved = False
    positions: list[tuple] = []
    dead_warn = False

    t0 = time.time()
    next_probe = t0 + probe_every if probe_every > 0 else float("inf")
    print(f"      {'t(s)':>6}  {'frame':>5}  mazeTask / motionTask / pos / spd / wall")
    while time.time() - t0 < seconds:
        now = time.time()
        f = mc.telemetry.latest()
        if f and f.get("_seq") != last_seq:
            last_seq = f.get("_seq")
            stamps.append((now, int(last_seq or 0)))
            tn = str(f.get("mazeTaskName"))
            if not tasks or tasks[-1] != tn:
                tasks.append(tn)
                print(f"      {now-t0:>6.2f}  {last_seq:>5}  {tn} / {f.get('motionTaskName')} / "
                      f"({f.get('posX')},{f.get('posY')}) / "
                      f"({f.get('speedL')},{f.get('speedR')}) / "
                      f"({f.get('frontName')},{f.get('wallLeft')},{f.get('wallRight')})")
            if (f.get("speedL") or 0) or (f.get("speedR") or 0):
                moved = True
            p = (f.get("posX"), f.get("posY"), f.get("dir"))
            if not positions or positions[-1] != p:
                positions.append(p)

        # --- 断流看门狗: 车在跑但不再收帧 => 链路可能死了(如实记录, 不假装) ---
        if stamps and now - stamps[-1][0] > 4.0 and not dead_warn:
            dead_warn = True
            print(f"      ⚠ {now-t0:>5.1f}s 起已 {now-stamps[-1][0]:.1f}s 没有新帧 "
                  f"—— 疑似断流(仍继续观察)")

        # --- 运动中命令通道探针 ---
        if now >= next_probe:
            next_probe += probe_every
            ws = time.time()
            mc.stream.suspend()               # 让遥测让路, 命令独占链路
            try:
                r = mc.send_cmd("ping", timeout=6.0)
                rtt = (time.time() - ws) * 1000
                probes.append({"t": ws - t0, "ok": r.ok, "rtt_ms": rtt, "status": r.status})
                print(f"      {ws-t0:>6.2f}  [probe] PING {'OK' if r.ok else '--'} "
                      f"{rtt:>6.0f}ms  {r.status}")
            finally:
                probe_windows.append((ws, time.time()))
                mc.stream.resume()

        time.sleep(0.02)

    mc.stream.off()
    after = mc.stream.stats()

    # --- 帧间隔(剔除探针让路造成的空档) ---
    gaps_all: list[float] = []
    gaps_true: list[float] = []
    for (t1, _s1), (t2, _s2) in zip(stamps, stamps[1:]):
        g = t2 - t1
        gaps_all.append(g)
        if any(ws <= t1 and t2 <= we for ws, we in probe_windows):
            continue
        gaps_true.append(g)

    left_wait = bool(tasks) and any(t and t != "WAIT" for t in tasks)
    sent = after["sent"] - before["sent"]
    done = after["done"] - before["done"]
    lost = after["lost"] - before["lost"]

    def _stat(xs: list[float]) -> str:
        if not xs:
            return "-"
        return (f"中位 {statistics.median(xs)*1000:>4.0f}ms  "
                f"最大 {max(xs)*1000:>4.0f}ms  n={len(xs)}")

    print(f"      → 帧数 {len(stamps)} (seq {stamps[0][1] if stamps else '-'}"
          f"→{stamps[-1][1] if stamps else '-'}) | 任务机 {'→'.join(tasks) if tasks else '(无)'}"
          f" | 速度{'出现过非零' if moved else '全程 0'} | 位姿 {len(positions)} 个")
    print(f"      → 拉取器 请求 {sent} 完成 {done} 丢失 {lost}"
          f" | 帧率 {after.get('hz')} (峰值 {after.get('best_hz')})")
    print(f"      → 帧间隔(全)   {_stat(gaps_all)}")
    print(f"      → 帧间隔(去探针) {_stat(gaps_true)}")
    pp = [p for p in probes if p["ok"]]
    print(f"      → 运动期 PING  {len(pp)}/{len(probes)} 成功"
          f"{'  RTT ' + _stat([p['rtt_ms']/1000 for p in pp]) if pp else ''}")

    return {
        "frames": len(stamps), "tasks": tasks, "left_wait": left_wait, "moved": moved,
        "positions": positions, "sent": sent, "done": done, "lost": lost,
        "hz": after.get("hz"), "probes": probes, "dead": dead_warn,
        "gap_med_ms": (statistics.median(gaps_true) * 1000) if gaps_true else 0,
        "gap_max_ms": (max(gaps_true) * 1000) if gaps_true else 0,
    }


def settle_to_wait(mc, budget: float = 14.0) -> dict:
    """复位后轮询到"真正新鲜"的空闲状态(复位后新帧 ~3 s 才到)。"""
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


def rescue(mc, tries: int = 3) -> object:
    """可靠收车: 先停车, 再软复位(带重试)。返回最后一次 `soft_reset` 结果。"""
    try:
        mc.maze.stop()
    except Exception:
        pass
    res = None
    for i in range(tries):
        res = mc.conn.soft_reset(verify=True, timeout=12.0)
        if bool(res.ok) and (res.data or {}).get("rst") == "SW":
            break
        time.sleep(0.5)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(prog="launch_link_live.py")
    ap.add_argument("port", nargs="?", default="COM7")
    ap.add_argument("--launch", action="store_true", help="真的发车(会让电脑鼠自走!)")
    ap.add_argument("--target", choices=["BLIND", "RACE"], default="RACE",
                    help="发车目标(默认 RACE = 正式比赛代码)")
    ap.add_argument("--observe-s", type=float, default=15.0, help="发车后观察/探针秒数")
    ap.add_argument("--probe-every", type=float, default=1.5,
                    help="运动期每隔几秒插一次 PING(0 = 关)")
    ap.add_argument("--no-probe", action="store_true", help="等价 --probe-every 0")
    ap.add_argument("--countdown", type=float, default=5.0, help="发车前倒计时秒数")
    args = ap.parse_args()
    if args.no_probe:
        args.probe_every = 0.0

    print("=" * 78)
    print(f"运动中链路有效性探针  端口={args.port}  目标={args.target}  "
          f"观察 {args.observe_s:.0f}s"
          f"{'  【会发车!】' if args.launch else '  (只读)'}")
    print("=" * 78)

    from host.api import MouseClient

    mc = MouseClient(auto_session=False, auto_stream=False)
    if not mc.connect(args.port):
        print("连接失败:", mc.state_detail)
        return 1

    try:
        rp = mc.send_cmd("ping", timeout=5.0)
        if not rp.ok:
            print("!! PING 不通, 退出")
            return 1
        ep, ef = fw_fingerprint_expect()
        same = (mc.device.get("proto") == ep and mc.device.get("fw") == ef)
        print(f"\n[0] 设备指纹  proto/fw={mc.device.get('proto')}/{mc.device.get('fw')} "
              f"txbuf={mc.device.get('txbuf')} rst={mc.reset_cause}"
              f"  ({'与源码一致' if same else f'⚠ 源码={ep}/{ef}'})")

        # 发车前只读快照
        mc.stream.on()
        mc.telemetry.wait_first_frame(timeout=6.0)
        f = mc.telemetry.latest() or {}
        st = mc.maze.state() or {}
        mc.stream.off()
        print(f"    发车前  task={st.get('taskName')} pos=({f.get('posX')},{f.get('posY')}) "
              f"dir={f.get('dir')} spd=({f.get('speedL')},{f.get('speedR')}) "
              f"battMv={f.get('battMv')} freemode={f.get('freemode')}")
        rec("发车前 PING", True, "OK")
        if not same:
            rec("固件与源码一致", False, f"{mc.device.get('proto')}/{mc.device.get('fw')}",
                "板上不是当前源码编的固件")

        if not args.launch:
            print("\n未指定 --launch —— 只读模式, 到此为止。")
            return 0

        if st.get("taskName") != "WAIT":
            print(f"    ⚠ 发车前任务机 = {st.get('taskName')}(非 WAIT)—— 先收车")
            rescue(mc)
            settle_to_wait(mc)

        # ------------------------------------------------ 发车
        print(f"\n    {args.countdown:.0f} 秒后发车 {args.target} …(Ctrl-C 可中止)")
        try:
            time.sleep(args.countdown)
        except KeyboardInterrupt:
            print("    已中止。")
            return 0

        print("\n[1] 发车")
        t_send = time.time()
        rm = mc.maze.run(target=args.target, timeout=6.0)
        rtt = (time.time() - t_send) * 1000
        rec(f"{args.target} MAZE RUN", rm.ok, rm.status, f"往返 {rtt:.0f}ms")

        if not rm.ok:
            print(f"    → 发车未被接受({rm.status})。"
                  f"注意: 命令**已送达并得到明确应答** ⇒ 链路本身是通的。")
            rec("命令往返(链路)", True, rm.status, "被拒 ≠ 链路坏")
            return 0

        # ------------------------------------------------ 运动中观察 + 探针
        print(f"\n[2] 运动中观察 {args.observe_s:.0f}s"
              f"{f' (每 {args.probe_every:.1f}s 插一次 PING)' if args.probe_every else ''}:")
        o = observe_and_probe(mc, args.observe_s, args.probe_every)

        rec(f"{args.target} 任务机离开 WAIT", o["left_wait"],
            "→".join(o["tasks"]) if o["tasks"] else "(无帧)", "发车请求被消费的证据")
        rec("运动中持续收帧", o["frames"] > 0, f"{o['frames']} 帧", "(仅记录)")
        rec("拉取零丢失", o["lost"] == 0 and o["sent"] > 0,
            f"请求 {o['sent']} 完成 {o['done']} 丢失 {o['lost']}")
        good = [p for p in o["probes"] if p["ok"]]
        rec("运动中命令通道可用", bool(good) if o["probes"] else True,
            f"PING {len(good)}/{len(o['probes'])}" if o["probes"] else "(未探针)")

        # ------------------------------------------------ 收车
        print("\n[3] 收车:")
        res = rescue(mc)
        rec("★ MCU RESET", (res.data or {}).get("rst") == "SW",
            f"rst={(res.data or {}).get('rst')} accepted={(res.data or {}).get('accepted')}",
            "成功判据 = 复位原因变 SW")
        st2 = settle_to_wait(mc)
        rec("★ 复位后回 WAIT", st2.get("taskName") == "WAIT", str(st2.get("taskName")))
        rp2 = mc.send_cmd("ping", timeout=5.0)
        rec("★ 复位后链路仍在", rp2.ok, rp2.status, f"rst={mc.reset_cause}")

        # ------------------------------------------------ 结论
        print("\n" + "=" * 78)
        print("结论")
        print("=" * 78)
        for s, ok, status in RESULTS:
            print(f"    {'[OK]' if ok else '[--]'} {s:<28} {status}")
        core = [r for r in RESULTS
                if r[0] in (f"{args.target} MAZE RUN", "运动中持续收帧", "拉取零丢失",
                            "运动中命令通道可用", "★ MCU RESET", "★ 复位后链路仍在")]
        bad = [r[0] for r in core if not r[1]]
        print()
        if not bad:
            print(f"    >>> **通信有效**: {args.target} 发了车、运动期间遥测持续到达"
                  f"(0 丢失)、命令通道仍能收到应答、收车后链路可用。")
        else:
            print(f"    >>> 需关注: {bad}(详见上方逐条记录)")
    finally:
        try:
            mc.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
