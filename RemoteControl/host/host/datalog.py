"""数据收集台 —— 远程发车 + 时间同步 + 完整采集(终端界面, 零 GUI 依赖)。

对应需求: "检测电脑鼠运动特性并收集数据" + "从发车命令开始 LED 以 1Hz 闪烁, 记录发车时间
和时间间隔来计算每一次接收到数据的准确时间"。

## 一次采集的完整流程

    1. 连上设备(并开始拉取遥测, 必要时开会话落盘)
    2. `ARM`  —— 发车: 固件记下发车时刻 + 点亮 1Hz 时间基准灯; 本机记下发出时刻
    3. 等时间同步收敛(收到若干统一帧, min-delay 偏移估计算出来)
    4. `MAZE RUN` —— 远程让电脑鼠开始跑无记忆走迷宫(或在此刻做别的动作)
    5. 实时监视 + **全程捕获统一帧**: 速度/航向/任务码/发车以来毫秒
    6. 结束时停车 → 由整段统一帧(≈217 Hz 全量快照)导出 CSV → 按**绝对设备时刻**报告

第 2 步之后, 每个采集到的样本都带 `dev_ms`(发车以来毫秒):
统一帧自带 `ms8`(低 8 位时基), 用每次 `TELEM` 应答里的完整 u32 锚做同余还原,
于是"蓝牙链路抖了多少"不再污染数据的时间轴。

v2.0(2026-10-05): 数据源唯一 —— 就是统一帧。原先"停车后 `SCOPE DUMP` 回传 1kHz"
的机制已随协议整体删除; 采集时把统一帧整段攒下即为完整数据, 会话目录里同时落
`stream_unified.csv`。

**LED 1Hz 基准灯**独立于软件时间戳, 供摄像头/光电/人眼做校核 ——
软件时间戳说第 N 秒到, 灯也应该正好翻转。

用法(在 host/ 目录下):
    python -m host.datalog COM7                      # 完整流程: 发车 -> 跑迷宫 -> 采集 -> 报告
    python -m host.datalog COM7 --arm-only           # 只发车(武装计时 + 点灯), 不动
    python -m host.datalog COM7 --status             # 只看时间同步状态
    python -m host.datalog COM7 --maze-only          # 只远程发车跑迷宫
    python -m host.datalog COM7 --seconds 20 --out run.csv   # 采 20 秒并把统一帧导出 CSV
    python -m host.datalog COM7 --no-maze --seconds 10   # 只采集, 不让它自己跑
    python -m host.datalog COM7 --json               # 结果以 JSON 输出

等价: `python -m host.cli datalog COM7 ...`
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from typing import Any, Dict, List, Optional

from .motor_test import C, _rule

# ---------------------------------------------------------------- 子流程


def cmd_status(mc, c: C, as_json: bool) -> int:
    st = mc.timesync.snapshot()
    if as_json:
        print(json.dumps(st.to_dict(), ensure_ascii=False, indent=2))
        return 0
    print(c("时间同步状态", "bold", "cyan") + " " + c(_rule(58), "dim"))
    print(mc.timesync.explain())
    return 0


def cmd_arm(mc, c: C, as_json: bool) -> int:
    r = mc.timesync.arm()
    if not r.ok:
        print(json.dumps({"ok": False, "status": r.status}, ensure_ascii=False) if as_json
              else c(f"ARM 失败: {r.status}", "red"))
        return 1
    # 等回执(ARMED <ms>)与几帧时间帧, 让偏移估计先收敛
    mc.timesync.wait_sync(samples=2, timeout=4.0)
    st = mc.timesync.snapshot()
    if as_json:
        print(json.dumps({"ok": True, "arm_up_ms": st.arm_up_ms}, ensure_ascii=False))
        return 0
    print(c("已发车(ARM): ", "bold") + c("OK", "green") +
          f"  设备时基 {st.arm_up_ms} ms")
    print(c("  1Hz 时间基准灯已点亮(LED4/PC7); 时基锚由每次 TELEM 应答刷新, "
            "统一帧逐帧带 ms8 做同余还原。", "dim"))
    return 0


def cmd_maze(mc, c: C, as_json: bool) -> int:
    r = mc.timesync.maze_run()
    if as_json:
        print(json.dumps({"ok": r.ok, "status": r.status}, ensure_ascii=False))
        return 0 if r.ok else 1
    if r.ok:
        print(c("远程发车(MAZE RUN): ", "bold") + c("OK", "green") +
              c("  电脑鼠开始跑无记忆走迷宫", "dim"))
    else:
        print(c("远程发车(MAZE RUN): ", "bold") + c(r.status, "red"))
        if r.status == "ERR_VALUE":
            print(c("  该固件不是数据收集档(MOUSE_DATA_LOG), 不会响应远程发车。", "dim"))
    return 0 if r.ok else 1


def cmd_run(mc, c: C, args) -> int:
    print(c("数据收集台", "bold", "cyan") + " " + c(_rule(64), "dim"))
    print(f"  设备 {mc.state_detail}   会话 {mc.session.path or '(未开启)'}")

    # ---- 1. 发车 ----
    if args.arm:
        if cmd_arm(mc, c, False) != 0:
            return 1
    else:
        print(c("  (跳过 ARM: 数据不带\"发车以来毫秒\")", "yellow"))

    st = mc.timesync.snapshot()
    if st.samples < 3:
        print(c("  ⚠ 时间同步样本还少, 偏移估计未收敛 —— 建议多等几秒再发车。", "yellow"))

    # ---- 2. 全程捕获统一帧(数据源唯一; 会话目录同时落 stream_unified.csv) ----
    from .api import Event

    frames: List[Dict[str, Any]] = []

    def _on_telem(msg) -> None:
        if msg.data.get("stream") != "unified":
            return
        rec = msg.data.get("rec")
        if rec:
            frames.append(dict(rec))

    off_telem = mc.on(Event.TELEMETRY, _on_telem)
    print(c("  统一帧全程捕获已开启(≈217 Hz; 会话目录同时落 stream_unified.csv)", "dim"))

    # ---- 3. 远程发车跑迷宫 ----
    if args.maze:
        if cmd_maze(mc, c, False) != 0 and not args.force:
            off_telem()
            return 1

    # ---- 4. 实时监视 ----
    # 注意: `mc.telemetry.live()` 是"定期打印摘要"的便捷方法, 拿不到逐帧的 ms8,
    # 所以这里自己按 hz 采样 —— 我们要的正是"每个样本的 dev_ms"。
    def _show(rec: Dict[str, Any]) -> None:
        dev = None
        if rec.get("ms8") is not None:
            try:
                dev = mc.timesync.frame_time_up(rec)
                if dev is not None and mc.timesync.arm_up_ms:
                    dev = dev - mc.timesync.arm_up_ms
            except Exception:
                dev = None
        dev_txt = f"{dev:7d}" if dev is not None else "    ..."
        print(
            f"  dev {dev_txt} ms  L {rec.get('speedL', 0):+7.1f}  "
            f"R {rec.get('speedR', 0):+7.1f} cm/s   "
            f"θ {rec.get('gyroAngle', 0):+8.1f}°   "
            f"{rec.get('motionTaskName', rec.get('motionTask', 0))}",
            flush=True,
        )

    print(c("  ── 实时流(dev_ms = 发车以来毫秒, 取自设备时基, 不受链路抖动影响)──", "bold"))
    t0 = time.time()
    seconds = float(args.seconds)
    period = 1.0 / max(0.5, float(args.hz))
    next_t = t0
    try:
        while time.time() - t0 < seconds:
            now = time.time()
            if now >= next_t:
                next_t = now + period
                f = mc.telemetry.latest()
                if f:
                    _show(f)
            time.sleep(0.005)
    except KeyboardInterrupt:
        print()
        print(c("  (提前停止采集)", "yellow"))
    wall = time.time() - t0

    # ---- 5. 停车 ----
    print()
    print(c("  停车…", "dim"))
    mc.send_cmd("stop")
    time.sleep(0.4)
    off_telem()

    stats = mc.stats()
    lost = int(stats.get("seq_lost", 0))
    gaps = int(stats.get("seq_gap", 0))

    out: Dict[str, Any] = {
        "wall_s": round(wall, 3),
        "seconds": seconds,
        "timesync": mc.timesync.snapshot().to_dict(),
        "link": {
            "unified": int(stats.get("unified", 0)),
            "map": int(stats.get("map", 0)),
            "event": int(stats.get("event", 0)),
            "text": int(stats.get("text", 0)),
            "bad_crc": int(stats.get("bad_crc", 0)),
            "seq_gap": gaps,
            "seq_lost": lost,
            "rx_bps": round(float(stats.get("rx_bps", 0.0)), 1),
        },
        "capture": {"points": len(frames)},
    }

    if frames:
        dev = [d for d in (_safe_dev_ms(mc, r) for r in frames) if d is not None]
        if dev:
            out["capture"]["dev_ms_first"] = int(dev[0])
            out["capture"]["dev_ms_last"] = int(dev[-1])
        if args.out:
            _write_unified_csv(mc, frames, args.out)
            out["out"] = args.out

    # ---- 6. 报告 ----
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
        return 0

    print()
    print(c("  ── 采集报告 ──", "bold"))
    ts = out["timesync"]
    print(f"  发车            " + (f"设备时基 {ts['arm_up_ms']} ms" if ts.get("armed")
                                    else c("未发车", "yellow")))
    if ts.get("offset_s") is not None:
        print(f"  时间偏移估计    {ts['offset_s']:.6f} s (min-delay)")
    if ts.get("drift_ppm") is not None:
        print(f"  设备时钟频差    {ts['drift_ppm']:+.2f} ppm")
    lk = out["link"]
    print(f"  收到上行        统一帧{lk['unified']} 墙图{lk['map']} "
          f"事件{lk['event']} 文本{lk['text']} (链路 {lk['rx_bps']} B/s)")
    lost_color = "green" if lost == 0 else ("yellow" if lost < 10 else "red")
    print(c(f"  丢帧            间隙 {gaps} 次 / 共丢 {lost} 条", lost_color) +
          (c("   ← 序号连续, 无丢帧", "dim") if lost == 0 else
           c("   ← 有丢帧! 原始字节仍在 raw.bin 里, 可用新解析器重跑", "dim")))
    print(f"  坏 CRC          {lk['bad_crc']}")
    cap = out["capture"]
    if cap["points"]:
        span = ""
        if "dev_ms_first" in cap:
            span = f", 发车以来 {cap['dev_ms_first']}→{cap['dev_ms_last']} ms"
        print(f"  统一帧捕获      {cap['points']} 点{span}")
        print(c("                  (时间轴已是绝对设备时刻, 可直接与发车时刻对齐)", "dim"))
    else:
        print(c("  统一帧捕获      0 点 —— 是否已开始拉取?", "yellow"))
    if out.get("out"):
        print(c(f"  已导出统一帧 -> {out['out']}", "dim"))
    return 0


def _safe_dev_ms(mc, rec: Dict[str, Any]) -> Optional[int]:
    try:
        d = mc.timesync.dev_ms(rec)
        if d is None:
            d = mc.timesync.dev_ms_from_recv(rec)
        return d
    except Exception:
        return None


def _write_unified_csv(mc, frames: List[Dict[str, Any]], path: str) -> None:
    """把整段捕获的统一帧落成 CSV(列 = codec.UNIFIED_CHANNELS, 与会话落盘同源)。"""
    from .protocol.codec import UNIFIED_CHANNELS

    cols = list(UNIFIED_CHANNELS)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["i", "dev_ms"] + cols)
        for i, rec in enumerate(frames):
            dev = _safe_dev_ms(mc, rec)
            w.writerow([i, "" if dev is None else int(dev)] + [rec.get(k, 0) for k in cols])


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="host.datalog",
        description="电脑鼠 数据收集台(远程发车 + 时间同步 + 完整采集)",
    )
    p.add_argument("target", nargs="?", default=None,
                   help="串口(COM7) / tcp://host:port / 省略则自动选择")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--seconds", type=float, default=10.0, help="采集时长(秒), 默认 10")
    p.add_argument("--hz", type=float, default=5.0, help="实时流显示频率")
    p.add_argument("--no-maze", dest="maze", action="store_false", default=True,
                   help="不远程发车跑迷宫, 只采集")
    p.add_argument("--no-arm", dest="arm", action="store_false", default=True,
                   help="不发车(不点基准灯、数据不带 dev_ms)")
    p.add_argument("--force", action="store_true",
                   help="MAZE RUN 被拒时也继续采集")
    p.add_argument("--out", help="把采集期间捕获的统一帧导出为 CSV")
    # 单步动作
    p.add_argument("--arm-only", action="store_true", help="只发车后退出")
    p.add_argument("--maze-only", dest="maze_only", action="store_true",
                   help="只远程发车跑迷宫后退出")
    p.add_argument("--status", action="store_true", help="只打印时间同步状态后退出")
    p.add_argument("--json", action="store_true", help="结果以 JSON 输出")
    p.add_argument("--no-color", action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # 裸调用(零参数)时既没有目标也没有动作 -> 打用法(而不是盲目连"第一个串口")。
    # 只带选项(如 `--status` / `--arm-only`)时仍保留"省略 target 自动选口"的行为。
    if not argv:
        build_parser().print_help()
        return 2
    args = build_parser().parse_args(argv)
    color_on = (not args.no_color) and sys.stdout.isatty() and not args.json
    c = C(color_on)

    from .api import MouseClient, coerce_target

    target = coerce_target(args.target, baud=args.baud)
    mc = MouseClient(auto_session=True, auto_stream=True)
    if not mc.connect(target):
        msg = f"连接失败: {mc.state_detail or '未知原因'}"
        print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False) if args.json
              else c(msg, "red"))
        mc.close()
        return 2
    try:
        if not mc.telemetry.wait_first_frame(timeout=5.0):
            print(c("未收到遥测首帧(设备可能默认静默或链路异常), 仍继续…", "yellow"))
        if args.status:
            return cmd_status(mc, c, args.json)
        if args.arm_only:
            return cmd_arm(mc, c, args.json)
        if args.maze_only:
            return cmd_maze(mc, c, args.json)
        return cmd_run(mc, c, args)
    except KeyboardInterrupt:
        print()
        print(c("收到 Ctrl+C —— 立即停车(固件侧另有护栏, 不会失控)", "yellow"))
        try:
            mc.send_cmd("stop")
        except Exception:
            pass
        return 130
    finally:
        try:
            if not args.json:
                st = mc.stats()
                print(c(f"  会话 {mc.session.path or '(未开启)'}", "dim"))
                print(c(f"  本次共收统一帧 {int(st.get('unified', 0))} 帧, "
                        f"坏CRC {int(st.get('bad_crc', 0))}, "
                        f"丢帧 {int(st.get('seq_lost', 0))}, "
                        f"链路 {float(st.get('rx_bps', 0)):.1f} B/s", "dim"))
        except Exception:
            pass
        mc.close()


if __name__ == "__main__":
    sys.exit(main())
