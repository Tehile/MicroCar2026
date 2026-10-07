"""电机远程控制实验 —— 统一帧实时观测(终端界面, 零 GUI 依赖)。

阶段用途: 电脑鼠轮子悬空, 远程让电机转一下, 同时把**运动期间的数据**看到:
    - 运动进行中: 统一帧(≈217 Hz 全量快照), 操作者据此确认"命令真的到了、轮子真的转了"
    - 运动结束后: 由固件上行的 `STOPPED <reason> #n` 给出**为什么停**

v2.0(2026-10-05): 原先的"双流"(运动时低 Hz 实时流 + 停车后 1kHz 日志整块回传)
随 `SCOPE`/`DUMP` 整体删除 —— 单流全速上行后, 运动细节本来就在主机内存里。
本程序只**观察**: 下发命令 + 把运动期间的最新快照按 `--hz` 采下来(可导出 CSV)。

停止条件**全部在固件的 1ms ISR 里**, 不在本程序里:
    到时必停(默认 2 s) / 红外贴脸必停(默认 10 mm) / 急停锁存
所以即使本进程被 kill 或蓝牙断开, 电机照样会按时停 —— 这正是"确保可靠"的落点。

用法(在 host/ 目录下):
    python -m host.motor_test COM7                       # 默认 40cm/s 前进 2 秒, 红外阈值 10mm
    python -m host.motor_test COM7 --sp 40 --time 2 --ir 10
    python -m host.motor_test COM7 --sp 40 -40           # 原地转(左右反向)
    python -m host.motor_test COM7 --ir -1               # 关闭红外护栏, 只验证时间护栏
    python -m host.motor_test COM7 --out run.csv         # 把运动期间的采样导出 CSV
    python -m host.motor_test COM7 --status              # 只看状态, 不动
    python -m host.motor_test COM7 --estop               # 只发急停(随时可用)
    python -m host.motor_test COM7 --resume              # 解急停/正常停车
    python -m host.motor_test COM7 --json                # 结果以 JSON 输出(脚本用)

等价: `python -m host.cli motor-test COM7 ...`
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from typing import Any, Dict, List, Optional

from .protocol.codec import MOTION_TASK_NAMES

# ---------------------------------------------------------------- 终端着色

_CLR = {
    "reset": "\x1b[0m",
    "dim": "\x1b[2m",
    "bold": "\x1b[1m",
    "red": "\x1b[31m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "blue": "\x1b[34m",
    "magenta": "\x1b[35m",
    "cyan": "\x1b[36m",
}


class C:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def __call__(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles:
            return text
        return "".join(_CLR.get(s, "") for s in styles) + text + _CLR["reset"]


def _rule(width: int = 78) -> str:
    return "─" * width


# ---------------------------------------------------------------- 子流程

def cmd_status(mc, c: C, as_json: bool) -> int:
    st = mc.lab.status()
    if as_json:
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0
    print(c("设备状态", "bold", "cyan") + " " + c(_rule(60), "dim"))
    ok = c("可运行", "green") if st["can_move"] else c("不可运行", "red")
    print(f"  连接        {mc.state_detail or '(未连接)'}")
    print(f"  电机        " + (
        c("已禁用(安全模式 MOUSE_TEST_NO_MOTOR)", "green")
        if st["motor_disabled"] else c("可控", "yellow")
    ))
    print(f"  急停锁存    " + (c("是", "red", "bold") if st["estop_locked"] else "否"))
    print(f"  急停/自由   emerg={st['emerg']}  freemode={st['freemode']}  "
          f"事件流完整={'否' if st['events_incomplete'] else '是'}")
    ir = st["ir"]
    print("  红外(mm)    " + "  ".join(f"{k} {v}" for k, v in ir.items()) +
          f"     最小 {st['ir_min']}")
    print(f"  结论        {ok}")
    return 0


def cmd_estop(mc, c: C, resume: bool, as_json: bool) -> int:
    r = mc.lab.resume() if resume else mc.lab.estop()
    if as_json:
        print(json.dumps({"cmd": "STOP" if resume else "ESTOP", "ok": r.ok, "status": r.status},
                         ensure_ascii=False))
        return 0
    what = "解急停/正常停车(STOP)" if resume else "急停(ESTOP)"
    print(c(f"{what}: ", "bold") + (c("OK", "green") if r.ok else c(r.status, "red")))
    time.sleep(0.4)
    st = mc.lab.status()
    print(f"  急停锁存={st['estop_locked']}  emerg={st['emerg']}  freemode={st['freemode']}")
    return 0 if r.ok else 1


def cmd_run(mc, c: C, args) -> int:
    # ---- 起飞前检查 ----
    st = mc.lab.status()
    if args.expect_safe and st["motor_disabled"] is not True:
        print(c("⚠ 期望安全固件但没有看到'电机已禁用'标志 —— 这台设备是可以动的。", "red", "bold"))

    print(c("电机远程控制实验", "bold", "cyan") + " " + c(_rule(56), "dim"))
    print(f"  目标 {mc.state_detail}   设备 {mc.device}")
    print(f"  命令 MOVE RUN {args.sp[0]} {args.sp[1]} {int(args.time * 1000)} {int(args.ir)}")
    print(f"  护栏 时间 {args.time:.2f}s " +
          (c(f"+ 红外 {int(args.ir)}mm", "yellow") if args.ir > 0 else c("(红外护栏已关闭)", "yellow")))
    if st["ir_min"] is not None and args.ir > 0 and st["ir_min"] < args.ir:
        print(c(f"  ⚠ 开始前最小红外 {st['ir_min']}mm 已经低于阈值 {int(args.ir)}mm —— "
                f"动作会在第一个周期内被护栏停掉。", "red", "bold"))
    if mc.motor_disabled:
        print(c("  ⚠ 设备是安全固件(MOUSE_TEST_NO_MOTOR): 电机输出恒 0, 不会动。", "red", "bold"))
    if st["estop_locked"]:
        print(c("  ⚠ 设备处于急停锁存: 先 --resume 解锁。", "red", "bold"))

    # ---- 采样回调: 运动期间逐样本打印(数据来自统一帧 ≈217Hz 的最新快照) ----
    t0 = time.time()

    def _on_sample(rec: Dict[str, Any]) -> None:
        if args.json:
            return
        el = rec["t"]
        ir_txt = rec["ir_min"] if rec["ir_min"] is not None else "—"
        print(
            f"  t={el:5.2f}s  L {rec['speedL']:+7.1f}   R {rec['speedR']:+7.1f} cm/s   "
            f"θ {rec['gyroAngle']:+7.1f}°   红外最小 {ir_txt:<4}  "
            f"{MOTION_TASK_NAMES.get(rec['motionTask'], rec['motionTask'])}",
            flush=True,
        )

    print(c("  ── 运动期间实时采样(统一帧, ≈217Hz)──", "bold"))
    res = mc.lab.run(
        sp_l=int(args.sp[0]),
        sp_r=int(args.sp[1]),
        ms=int(args.time * 1000),
        ir_mm=int(args.ir),
        sample_hz=args.hz,
        on_sample=_on_sample,
    )
    wall = time.time() - t0

    # ---- 结果 ----
    if args.json:
        out = res.to_dict(with_samples=True)
        out["wall_s"] = round(wall, 3)
        out["target"] = mc.state_detail
        print(json.dumps(out, ensure_ascii=False))
        return 0 if res.ok else 1

    print()
    if not res.ok:
        print(c(f"实验未完成: {res.status}", "red", "bold"))
        if res.error:
            print(c(f"  {res.error}", "red"))
        if res.warning:
            print(c(f"  提示: {res.warning}", "yellow"))
        return 1

    reason_color = "green" if res.reason == "TIME" else ("yellow" if res.reason == "IR" else "red")
    print(c("  停止原因  ", "bold") + c(f"{res.reason_text} ({res.reason})", reason_color))
    rt = res.realtime
    print(f"  运动时长  {rt.get('seconds')} s   (墙钟 {wall * 1000:.0f} ms)")
    print(f"  统一帧    {rt.get('frames', 0)} 帧, 约 {rt.get('approx_bps')} B/s, "
          f"坏CRC {rt.get('bad_crc')}, 丢帧 {rt.get('seq_lost')}")
    print(f"  采样点    {len(res.samples)} 个 (回调频率 {args.hz:.0f} Hz)")
    if res.warning:
        print(c(f"  提示: {res.warning}", "yellow"))

    # ---- 导出 ----
    if args.out and res.samples:
        _write_samples_csv(res.samples, args.out)
        print(c(f"  已导出采样 -> {args.out}", "dim"))

    # ---- 分析: 直接用统一帧历史(不需要任何回传往返) ----
    if not args.no_analyze:
        try:
            txt = mc.analyze.explain(mc.telemetry.history_dict())
            if txt:
                print()
                print(c("  ── 运动段分析 ──", "bold"))
                print("    " + txt.replace("\n", "\n    "))
        except Exception as e:  # 分析失败不影响实验结论
            print(c(f"  (分析跳过: {type(e).__name__}: {e})", "dim"))
    return 0


# ---------------------------------------------------------------- 导出

def _write_series_csv(arr, path: str) -> None:
    """把 `(t, motionTask, gyro, speedL, speedR)` 五元组落成 CSV。

    `mc.lesson.run()` 本地捕获的统一帧序列正好是这个形状 —— 供课程实验复用。
    """
    t, task, ang, sl, sr = arr
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["i", "t_ms", "motionTask", "gangle", "speedl", "speedr"])
        for i in range(len(t)):
            w.writerow([i, int(t[i]), int(task[i]), int(ang[i]), int(sl[i]), int(sr[i])])


def _write_samples_csv(samples: List[Dict[str, Any]], path: str) -> None:
    cols = ["t", "speedL", "speedR", "gyroAngle", "motionTask", "ir_min"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(samples)


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="host.motor_test",
        description="电脑鼠 电机远程控制实验(运动期间采样统一帧)",
    )
    p.add_argument("target", nargs="?", default=None,
                   help="串口(COM7) / tcp://host:port / 省略则自动选择")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--sp", type=int, nargs="+", default=[40],
                   metavar="CM_S", help="目标速度 cm/s: 一个值=左右相同, 两个值=左 右")
    p.add_argument("--time", type=float, default=2.0, help="最长运行时间(秒), 默认 2.0")
    p.add_argument("--ir", type=int, default=10,
                   help="红外停车阈值(mm), 默认 10(=1cm); 负值 = 关闭红外护栏")
    p.add_argument("--hz", type=float, default=5.0, help="采样显示频率")
    p.add_argument("--no-analyze", action="store_true", help="跳过运动段分析")
    p.add_argument("--out", help="把运动期间的采样导出为 CSV")
    p.add_argument("--status", action="store_true", help="只打印状态后退出")
    p.add_argument("--estop", action="store_true", help="只发急停后退出")
    p.add_argument("--resume", action="store_true", help="只发 STOP(解急停)后退出")
    p.add_argument("--expect-safe", action="store_true",
                   help="期望设备处于安全测试模式(电机禁用); 缺失时告警")
    p.add_argument("--json", action="store_true", help="结果以 JSON 输出")
    p.add_argument("--no-color", action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # 裸调用(零参数)时没有目标也没有动作, 直接打用法 —— 而不是盲目去连"第一个串口"。
    # 注意: 只带选项(如 `--status`)时仍保留"省略 target 自动选口"的行为。
    if not argv:
        build_parser().print_help()
        return 2
    args = build_parser().parse_args(argv)
    if len(args.sp) == 1:
        args.sp = [args.sp[0], args.sp[0]]
    elif len(args.sp) > 2:
        args.sp = args.sp[:2]

    from .api import MouseClient, coerce_target

    color_on = (not args.no_color) and sys.stdout.isatty() and not args.json
    c = C(color_on)

    target = coerce_target(args.target, baud=args.baud)
    mc = MouseClient(auto_session=False, auto_stream=True)
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
        if args.estop:
            return cmd_estop(mc, c, resume=False, as_json=args.json)
        if args.resume:
            return cmd_estop(mc, c, resume=True, as_json=args.json)
        return cmd_run(mc, c, args)
    except KeyboardInterrupt:
        print()
        print(c("收到 Ctrl+C —— 立即发急停(固件侧另有护栏, 不会失控)", "yellow"))
        try:
            mc.lab.estop()
        except Exception:
            pass
        return 130
    finally:
        try:
            if not args.json:
                st = mc.stats()
                print(c(f"  本次共收统一帧 {int(st.get('unified', 0))} 帧, "
                        f"坏CRC {int(st.get('bad_crc', 0))}, "
                        f"丢帧 {int(st.get('seq_lost', 0))}, "
                        f"链路 {float(st.get('rx_bps', 0.0)):.1f} B/s", "dim"))
        except Exception:
            pass
        mc.close()


if __name__ == "__main__":
    sys.exit(main())
