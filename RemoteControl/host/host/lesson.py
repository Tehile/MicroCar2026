"""课程调参实验 —— 参数设定 + 实验启停 + 由回传数据自动算参(终端界面, 零 GUI 依赖)。

对应课件**实验三~实验八**的"调参工作台"。与 `python -m host.motor_test` 的分工:

    host.motor_test   自由地让电机转一下, 验证"命令 -> 电机 -> 遥测"这条链通不通
    host.lesson       按**课件规定的实验剖面**驱动电机, 并把实验数据换算成**可写回设备的参数**

三者中的"参数设定"闭环在这里合上:
    上位机设参(--set / --params)  ->  固件按剖面驱动机器人  ->  回传明细行 + 本地捕获的统一帧
        ->  上位机自动算参(--truth)  ->  --apply 把新参数写回设备

v2.0(2026-10-05): 实验曲线不再靠"停车后 1kHz 日志整块回传"(`SCOPE`/`DUMP` 已删),
而是由 `mc.lesson.run(capture=True)` 在**实验期间**把统一帧(≈217 Hz 全量快照)
攒在主机内存里, 实验结束直接算参 —— 数据源唯一, 也省掉一次往返。

停止判定与剖面推进**全在固件的 1ms 引擎里**(`LESSONLAB_Tick`), 不在本程序里 ——
所以本进程被 kill、蓝牙掉线, 实验照样会按时间护栏停下来。

用法(在 host/ 目录下):
    python -m host.lesson COM7 --list                    # 列出可驱动的实验
    python -m host.lesson COM7 --params                  # 打印 14 个课程调参量
    python -m host.lesson COM7 --theory                  # 打印里程/角度系数理论值
    python -m host.lesson COM7 --set KPL=11.5 --set KIL=0.6 --save
    python -m host.lesson COM7 --status                  # 设备/急停/日志就绪状态
    python -m host.lesson COM7 --stop                    # 结束当前实验(固件停车)

    # 实验三 编码器测距: 直行到固件自认的 100mm, 用尺子量出 110mm 后回填
    python -m host.lesson COM7 2 --v 60 --dist 100 --ms 3000 --truth 110
    python -m host.lesson COM7 2 --v 60 --dist 100 --truth 110 --apply --save

    # 实验四 匀变速直线: 加速200ms -> 匀速800ms -> 减速200ms; 回传曲线算 sigma%/ts/tp
    python -m host.lesson COM7 3 --v 50 --t-acc 200 --t-cruise 800 --t-dec 200 --out step.csv

    # 实验六 陀螺仪标定: 原地转 1080 度, 实测 1080 度 -> 修正角度系数
    python -m host.lesson COM7 5 --angle 1080 --v 60 --truth 1080 --apply

    # 实验七 多传感器校准直行: 按 SENSORMODE 开纠偏直行, 回传直行质量指标
    python -m host.lesson COM7 6 --v 50 --dist 1000 --ms 8000

    # 只启动不等结果 / 稍后结束
    python -m host.lesson COM7 3 --start
    python -m host.lesson COM7 --stop

等价: `python -m host.cli lesson COM7 ...`
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Dict, List, Optional

from .api.lesson import (
    LESSON_EXPS,
    LESSON_PARAM_NAMES,
    error_ratio,
    solve_ratio,
    theory_gyro_ratio,
    theory_track_ratio,
)
from .motor_test import C, _rule, _write_series_csv

# 六实验里能被固件引擎驱动的四个(实验五走 mc.calib 的 IRDATA; 实验八转向数组是 const)
_DRIVABLE = tuple(sorted(LESSON_EXPS))


# ---------------------------------------------------------------- 子流程

def cmd_list(c: C, as_json: bool) -> int:
    if as_json:
        print(json.dumps(
            {str(no): {"name": sp.name, "args": list(sp.arg_names),
                       "defaults": list(sp.defaults), "help": sp.help}
             for no, sp in sorted(LESSON_EXPS.items())},
            ensure_ascii=False, indent=2))
        return 0
    print(c("可驱动的课程实验", "bold", "cyan") + " " + c(_rule(58), "dim"))
    for no, sp in sorted(LESSON_EXPS.items()):
        args_txt = " ".join(f"--{n.replace('_', '-')}={d}" for n, d in zip(sp.arg_names, sp.defaults))
        print(f"  实验 {no}  {c(sp.name, 'bold')}")
        print(f"          {c(sp.help, 'dim')}")
        print(f"          默认参数: {args_txt}")
    print()
    print(c("  实验四 红外数据采集标定 走 `python -m host.cli calib irdata`", "dim"))
    print(c("  实验八 转向控制         转向搜索数组是 const, 未纳入在线调参", "dim"))
    return 0


def cmd_theory(c: C, as_json: bool) -> int:
    track = theory_track_ratio()
    gyro = theory_gyro_ratio()
    if as_json:
        print(json.dumps({"track_ratio": round(track, 4), "gyro_ratio": round(gyro, 4)},
                         ensure_ascii=False))
        return 0
    print(c("系数理论值(课件公式)", "bold", "cyan") + " " + c(_rule(58), "dim"))
    print(f"  实验三 里程系数 = 线数 x4 x (轮齿/编码齿) / 周长")
    print(f"          = 256 x4 x (42/10) / (pi x 22mm) = "
          + c(f"{track:.4f}", "bold", "yellow") + c("   (课件示例 62.24)", "dim"))
    print(f"  实验六 角度系数 = [灵敏度/(Vref/2^bits)] x 1000")
    print(f"          = [0.67/(3300/2^11)] x 1000 = "
          + c(f"{gyro:.4f}", "bold", "yellow") + c("   (课件示例 415.8)", "dim"))
    print(c("  理论值只是起点; 车轮实际尺寸/陀螺实际灵敏度有偏差, 以 --truth 实测修正为准。", "dim"))
    return 0


def cmd_params(mc, c: C, as_json: bool) -> int:
    cur = mc.lesson.params()
    meta = mc.lesson.param_meta()
    if as_json:
        print(json.dumps({k: {"value": v, "desc": meta.get(k, ("", 0, ""))[2],
                              "decimals": meta.get(k, ("", 3, ""))[1]}
                          for k, v in cur.items()}, ensure_ascii=False, indent=2))
        return 0
    print(c("课程调参量(设备当前值)", "bold", "cyan") + " " + c(_rule(58), "dim"))
    for name in LESSON_PARAM_NAMES:
        if name not in cur:
            continue
        _, dec, desc = meta.get(name, ("", 3, ""))
        print(f"  {name:<10} {cur[name]:>10.4f}   {c(desc, 'dim')}")
    print(c("  改完用 --save 落 Flash(page 116); 或 `python -m host.cli save`。", "dim"))
    return 0


def cmd_set(mc, c: C, pairs: List[str], readback: bool, save: bool, as_json: bool) -> int:
    mapping: Dict[str, float] = {}
    for p in pairs:
        if "=" not in p:
            print(c(f"参数格式应为 NAME=VALUE, 收到 {p!r}", "red"))
            return 2
        k, v = p.split("=", 1)
        mapping[k.strip().upper()] = float(v)
    bad = [k for k in mapping if k not in LESSON_PARAM_NAMES]
    if bad:
        print(c(f"未知课程参数: {bad}; 可选 {list(LESSON_PARAM_NAMES)}", "red"))
        return 2
    results = mc.lesson.set_params(mapping, readback=readback, save=save)
    ok = all(r.ok for r in results)
    if as_json:
        print(json.dumps({"ok": ok, "set": mapping, "save": save,
                          "results": [{"status": r.status, "data": r.data} for r in results]},
                         ensure_ascii=False))
        return 0 if ok else 1
    print(c("设定课程参数", "bold", "cyan") + " " + c(_rule(58), "dim"))
    for (name, val), r in zip(mapping.items(), results):
        rb = r.data.get("readback")
        tail = f"  回读 {rb}" if rb is not None else ""
        print(f"  {name:<10} -> {val:<10.4f} " +
              (c("OK", "green") if r.ok else c(r.status, "red")) + c(tail, "dim"))
    if save:
        print("  " + (c("已 SAVE 落 Flash(page 116)", "green") if ok
                      else c("未保存(前面有失败项)", "yellow")))
    else:
        print(c("  仅改了 RAM; 要掉电保持请加 --save。", "dim"))
    return 0 if ok else 1


def cmd_status(mc, c: C, as_json: bool) -> int:
    st = mc.lesson.status()
    try:
        cur = mc.lesson.params()
    except Exception:
        cur = {}
    if as_json:
        st = dict(st)
        st["params"] = cur
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0
    print(c("课程实验状态", "bold", "cyan") + " " + c(_rule(58), "dim"))
    print(f"  连接        {mc.state_detail or '(未连接)'}")
    print(f"  电机        " + (c("已禁用(安全模式)", "green")
                                if st["motor_disabled"] else c("可控", "yellow")))
    print(f"  急停锁存    " + (c("是", "red", "bold") if st["estop_locked"] else "否"))
    print(f"  遥测流      {'在流' if st['streaming'] else '未开'}   freemode={st['freemode']}   "
          f"事件流完整={'否' if st['events_incomplete'] else '是'}")
    print(f"  可否做实验  " + (c("可以", "green") if st["can_run"] else c("不可", "red")))
    if cur:
        print("  当前参数    " + "  ".join(f"{k}={v:.3f}" for k, v in cur.items()))
    return 0


def cmd_stop(mc, c: C, as_json: bool) -> int:
    r = mc.lesson.stop()
    if as_json:
        print(json.dumps({"ok": r.ok, "status": r.status}, ensure_ascii=False))
        return 0 if r.ok else 1
    print(c("结束实验(LESSON STOP): ", "bold") +
          (c("OK", "green") if r.ok else c(r.status, "red")))
    time.sleep(0.4)
    st = mc.lesson.status()
    print(f"  急停锁存={st['estop_locked']}  freemode={st['freemode']}  "
          f"在流={st['streaming']}")
    return 0 if r.ok else 1


# ---------------------------------------------------------------- 主流程: 跑一次实验

def _exp_kwargs(exp: int, args) -> Dict[str, int]:
    spec = LESSON_EXPS[exp]
    kw: Dict[str, int] = {}
    for nm, dv in zip(spec.arg_names, spec.defaults):
        val = getattr(args, nm.replace("-", "_"), None)
        kw[nm] = int(dv if val is None else val)
    return kw


def cmd_run(mc, c: C, args) -> int:
    exp = int(args.exp)
    spec = LESSON_EXPS.get(exp)
    if spec is None:
        print(c(f"未知实验号 {exp}; 可驱动: {list(_DRIVABLE)}", "red"))
        print(c("  实验四(红外)走 `python -m host.cli calib irdata`, 实验八未纳入。", "dim"))
        return 2

    kw = _exp_kwargs(exp, args)

    # ---- 起飞前检查 ----
    st = mc.lesson.status()
    if mc.motor_disabled:
        print(c("⚠ 设备跑的是安全测试固件(MOUSE_TEST_NO_MOTOR): 电机恒不输出, 实验会空跑。", "red", "bold"))
        print(c("  请改烧 MOUSE_LESSON_LAB 后再做调参实验。", "dim"))
    if st["estop_locked"]:
        print(c("⚠ 设备处于急停锁存: 先 `python -m host.cli estop --resume` 解锁。", "red", "bold"))

    arg_txt = " ".join(f"{k}={v}" for k, v in kw.items())
    print(c(f"实验 {exp} {spec.name}", "bold", "cyan") + " " + c(_rule(58 - len(spec.name)), "dim"))
    print(f"  设备 {mc.state_detail}   命令 LESSON START {exp} "
          + " ".join(str(v) for v in [kw.get(n, 0) for n in spec.arg_names]))
    print(f"  {c(spec.help, 'dim')}")
    print(f"  实验参数 {arg_txt}")

    # 只启动不等结果
    if args.start:
        r = mc.lesson.start(exp, *[kw.get(n, 0) for n in spec.arg_names][:4])
        if args.json:
            print(json.dumps({"ok": r.ok, "status": r.status}, ensure_ascii=False))
        else:
            print(c("已启动(LESSON START): ", "bold") +
                  (c("OK", "green") if r.ok else c(r.status, "red")) +
                  c("   —— 用 --stop 结束", "dim"))
        return 0 if r.ok else 1

    # ---- 实时流回调(数据来自统一帧的最新快照; 曲线另由 capture 全速攒下) ----
    def _on_sample(rec: Dict[str, Any]) -> None:
        if args.json:
            return
        print(
            f"  t={rec['t']:5.2f}s  L {rec['speedL']:+7.1f}   R {rec['speedR']:+7.1f} cm/s   "
            f"θ {rec['gyroAngle']:+8.1f}°",
            flush=True,
        )

    print(c("  ── 实验期间实时采样(统一帧, ≈217Hz)──", "bold"))

    def _plan(budget_ms: int) -> None:
        if not args.json:
            print(c(f"  (预算 {budget_ms} ms; 超时即判 TIMEOUT)", "dim"))

    t0 = time.time()
    res = mc.lesson.run(
        exp,
        sample_hz=args.hz,
        on_sample=_on_sample,
        on_timeout_plan=_plan,
        **kw,
    )
    wall = time.time() - t0

    # ---- 自动算参(--truth) ----
    suggest = {}
    if res.ok and args.truth is not None:
        suggest = _compute_suggestion(mc, res, float(args.truth))

    # ---- 结果 ----
    if args.json:
        out = res.to_dict(with_series=False)
        out["wall_s"] = round(wall, 3)
        out["suggest"] = suggest
        out["applied"] = bool(args.apply and suggest)
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

    reason_color = "green" if res.reason in ("DONE", "DIST", "ANGLE") else (
        "yellow" if res.reason == "TIME" else "red")
    print(c("  结束原因  ", "bold") + c(f"{res.reason_text} ({res.reason})", reason_color))
    print(f"  运行时长  {res.elapsed_ms} ms   (墙钟 {wall * 1000:.0f} ms)")
    print(f"  轮子位移  L {res.dL} / R {res.dR} 脉冲    "
          f"实测峰值 L {res.peak_l} / R {res.peak_r} cm/s")
    print(f"  结束航向  {res.angle} 度")

    # 各实验的专属指标
    for line in _metric_lines(res):
        print("  " + line)

    rt = res.realtime
    print(f"  统一帧    {rt.get('frames', 0)} 帧, 约 {rt.get('approx_bps')} B/s, "
          f"坏CRC {rt.get('bad_crc')}, 丢帧 {rt.get('seq_lost')}")
    if res.log_fetched:
        print(f"  实验曲线  本地捕获 {res.log_points} 点 (≈217 Hz, 实验期间)" +
              c("   (实验数据已拿到)", "green"))
    else:
        print("  实验曲线  " + c("未捕获(是否已开始拉取?)", "yellow"))
    if res.warning:
        print(c(f"  提示: {res.warning}", "yellow"))

    # ---- 自动算参结论 ----
    if args.truth is not None:
        _print_suggestion(mc, c, res, float(args.truth), suggest,
                          apply=args.apply, save=args.save)
    else:
        _print_truth_hint(c, res)

    # ---- 导出 ----
    if args.out and res.log_arrays is not None:
        _write_series_csv(res.log_arrays, args.out)
        print(c(f"  已导出实验曲线 -> {args.out}", "dim"))
    return 0


def _metric_lines(res) -> List[str]:
    """把 LessonResult.metrics 里各实验的专属指标整理成可读行。"""
    lines: List[str] = []
    for i, tag in enumerate(("左轮", "右轮")):
        if i < len(res.steps):
            m = res.steps[i]
            lines.append(
                f"{tag}阶跃   稳态 {m.steady:.1f} 峰值 {m.peak:.1f}  "
                f"σ% {m.sigma_pct:.2f}%  ts {m.ts_ms} ms  tp {m.tp_ms} ms  "
                f"上升 {m.rise_ms} ms  (误差带 ±{m.band:.2f})"
            )
    s = res.straight
    if s is not None:
        lines.append(
            f"直行质量   航向漂移 {s.drift_deg:+.2f} 度 ({s.drift_rate_dps:+.3f} 度/s)  "
            f"左右轮 {s.speed_mean_l:.1f}/{s.speed_mean_r:.1f} cm/s  "
            f"不对称 {s.bias_pct:+.2f}%"
        )
    enc = res.metrics.get("encoder")
    if enc:
        lines.append(
            f"里程(固件)  L {enc['believed_mm_l']} / R {enc['believed_mm_r']} mm   "
            f"系数 L {enc['ratio_l']} / R {enc['ratio_r']}"
        )
    gy = res.metrics.get("gyro")
    if gy:
        lines.append(f"角度(固件)  报 {gy['reported_deg']} 度")
    return lines


# ---------------------------------------------------------------- 自动算参

def _compute_suggestion(mc, res, truth: float) -> Dict[str, Any]:
    """按实验类型 + 实测真值, 算出**建议写回**的参数(纯计算, 不改设备)。"""
    out: Dict[str, Any] = {}
    try:
        if res.exp == 2:
            # 读数直接用固件明细行里的脉冲数 / 当前系数 —— 不依赖 1kHz 日志是否回传成功。
            # TRACKL/TRACKR 是**生产标定参数**(不在 LESSON_PARAM_NAMES 内), 用 mc.params.get 直读。
            for name, pulses in (("TRACKL", res.dL), ("TRACKR", res.dR)):
                ratio = mc.params.get(name, timeout=2.0)
                if ratio and pulses is not None:
                    reading = pulses / float(ratio)   # 固件自认走了多少 mm
                    out[name] = {
                        "old": round(float(ratio), 4),
                        "new": round(solve_ratio(ratio, reading, truth), 4),
                        "reading": round(reading, 2), "truth": truth,
                        "err_pct": round(error_ratio(reading, truth), 2),
                    }
        elif res.exp == 5:
            # GYRORATIO 也是生产参数; 课程实验五用它做角度系数标定
            ratio = mc.params.get("GYRORATIO", timeout=2.0)
            if ratio and res.angle is not None:
                out["GYRORATIO"] = {
                    "old": round(float(ratio), 4),
                    "new": round(solve_ratio(ratio, abs(res.angle), truth), 4),
                    "reading": abs(res.angle), "truth": truth,
                    "err_pct": round(error_ratio(abs(res.angle), truth), 2),
                }
        elif res.exp == 6:
            s = res.straight
            if s is not None:
                out["_straight"] = s.to_dict()
    except Exception as e:
        out["_error"] = f"{type(e).__name__}: {e}"
    return out


def _print_truth_hint(c: C, res) -> None:
    if res.exp == 2:
        print(c("  自动算参  用尺子量出实际距离 D_true(mm), 加 --truth D_true 即给出修正系数。", "dim"))
    elif res.exp == 5:
        print(c("  自动算参  记下实际转过角度 A_true(度), 加 --truth A_true 即给出修正系数。", "dim"))


def _print_suggestion(mc, c: C, res, truth: float, suggest: Dict[str, Any],
                      apply: bool, save: bool) -> None:
    unit = "mm" if res.exp == 2 else ("度" if res.exp == 5 else "")
    print()
    print(c("  ── 自动算参 ──", "bold"))
    if res.exp == 6:
        s = suggest.get("_straight")
        if s:
            print(f"    直行不对称 {s['bias_pct']:+.2f}%  ->  " +
                  c("偏哪边就把对应轮的增益调大", "dim"))
        return
    if not suggest:
        print(c("    无法算参(缺少回传的读数/系数)", "yellow"))
        return
    print(f"    实测真值 {truth} {unit}")
    for name, d in suggest.items():
        if name.startswith("_"):
            continue
        arrow = c("↓ 调小", "green") if d["new"] < d["old"] else c("↑ 调大", "green")
        print(f"    {name:<10} {d['old']:.4f} -> {d['new']:.4f}  {arrow}   "
              f"(读数 {d['reading']} {unit}, 误差 {d['err_pct']:+.2f}%)")
    if apply:
        # **按命名空间分流**: 课程参量走 mc.lesson.set_params; 生产标定(TRACKL/TRACKR/GYRORATIO)
        # 走 mc.params.set —— 两套参数存在不同的 Flash 页(99/100), 不能混用 API。
        lesson_write = {k: v["new"] for k, v in suggest.items()
                        if not k.startswith("_") and k in LESSON_PARAM_NAMES}
        prod_write = {k: v["new"] for k, v in suggest.items()
                      if not k.startswith("_") and k not in LESSON_PARAM_NAMES}
        results = []
        if lesson_write:
            results.extend(mc.lesson.set_params(lesson_write, readback=True, save=False))
        for k, v in prod_write.items():
            results.append(mc.params.set(k, v, readback=True))
        ok = all(r.ok for r in results)
        if ok and save:
            sv = mc.params.save()
            ok = sv.ok
        print("    " + (c(f"已写回 {'+ 落 Flash' if save else '(仅 RAM)'}: "
                          f"{list(lesson_write) + list(prod_write)}", "green")
                        if ok else c("写回失败", "red")))
    else:
        print(c("    未写回; 加 --apply 应用, --apply --save 落 Flash。", "dim"))


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="host.lesson",
        description="电脑鼠 课程调参实验(参数设定 + 实验启停 + 自动算参)",
    )
    p.add_argument("target", nargs="?", default=None,
                   help="串口(COM7) / tcp://host:port / 省略则自动选择")
    p.add_argument("exp", nargs="?", type=int, default=None,
                   help=f"实验号 {list(_DRIVABLE)} (省略则配合 --list/--params/--status/--stop)")
    p.add_argument("--baud", type=int, default=115200)
    # 动作开关
    p.add_argument("--list", action="store_true", help="列出可驱动的实验")
    p.add_argument("--theory", action="store_true", help="打印里程/角度系数理论值")
    p.add_argument("--params", action="store_true", help="打印 14 个课程调参量")
    p.add_argument("--status", action="store_true", help="只打印状态后退出")
    p.add_argument("--stop", action="store_true", help="结束当前实验(LESSON STOP)")
    p.add_argument("--start", action="store_true", help="只启动实验, 不等结果")
    p.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                   help="设定课程参数, 可重复 (如 --set KPL=11.5 --set KIL=0.6)")
    p.add_argument("--no-readback", dest="readback", action="store_false",
                   help="设参后不回读校验")
    p.add_argument("--save", action="store_true", help="设参/应用后落 Flash(page 116)")
    # 实验参数(具体含义随实验号变化; 省略则用课件默认值)
    p.add_argument("--v", type=int, default=None, help="目标速度 cm/s")
    p.add_argument("--dist", type=int, default=None, help="目标里程 mm")
    p.add_argument("--ms", type=int, default=None, help="时间护栏 ms")
    p.add_argument("--angle", type=int, default=None, help="目标角度 度")
    p.add_argument("--t-acc", dest="t_acc", type=int, default=None, help="加速段 ms")
    p.add_argument("--t-cruise", dest="t_cruise", type=int, default=None, help="匀速段 ms")
    p.add_argument("--t-dec", dest="t_dec", type=int, default=None, help="减速段 ms")
    # 自动算参
    p.add_argument("--truth", type=float, default=None,
                   help="实测真值: 实验三=实际距离(mm), 实验六=实际角度(度)")
    p.add_argument("--apply", action="store_true", help="把算出的修正系数写回设备")
    # 曲线与输出
    p.add_argument("--hz", type=float, default=5.0, help="采样显示频率")
    p.add_argument("--out", help="把实验期间捕获的统一帧导出为 CSV")
    p.add_argument("--json", action="store_true", help="结果以 JSON 输出(脚本用)")
    p.add_argument("--no-color", action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # 裸调用(零参数)时既没有实验号也没有动作 -> 打用法(而不是盲目连"第一个串口")。
    # `--list` / `--theory` 这类不需要设备的动作带上参数照常可用。
    if not argv:
        build_parser().print_help()
        return 2
    args = build_parser().parse_args(argv)
    # 位置参数 target/exp 都是可选的: `python -m host.lesson 2 ...` 里那个 "2" 会被
    # argparse 塞进 target。串口名 / tcp:// / replay:// 都不可能是纯数字, 所以纯数字的
    # 第一个位置参数一定是实验号 —— 这里把它挪正。
    if args.exp is None and args.target is not None and str(args.target).isdigit():
        args.exp, args.target = int(args.target), None
    color_on = (not args.no_color) and sys.stdout.isatty() and not args.json
    c = C(color_on)

    # 不需要连设备的动作
    if args.list:
        return cmd_list(c, args.json)
    if args.theory:
        return cmd_theory(c, args.json)

    from .api import MouseClient, coerce_target

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
        # 顺序: 先做只读/设置类动作, 再考虑跑实验
        if args.params:
            return cmd_params(mc, c, args.json)
        if args.status:
            return cmd_status(mc, c, args.json)
        if args.set:
            rc = cmd_set(mc, c, args.set, args.readback, args.save, args.json)
            if args.exp is None and not args.stop:
                return rc
        if args.stop:
            return cmd_stop(mc, c, args.json)
        if args.exp is None:
            # 没给动作也没给实验号 -> 打印一次状态与实验清单
            cmd_status(mc, c, False)
            print()
            return cmd_list(c, False)
        return cmd_run(mc, c, args)
    except KeyboardInterrupt:
        print()
        print(c("收到 Ctrl+C —— 立即发急停(固件侧另有护栏, 不会失控)", "yellow"))
        try:
            mc.send_cmd("estop")
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
