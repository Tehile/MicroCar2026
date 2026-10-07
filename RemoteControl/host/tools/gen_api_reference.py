"""由 registry + docstring 生成 `docs/api_reference.md`。

用法(在 host/ 目录下):
    python tools/gen_api_reference.py

设计目标(E5): 手写文档不再是真相来源 —— 本文档从登记表与源码 docstring
直接反射生成, 新增一条命令/一个参数/一个方法都会自动反映进来, 结构上不可能漂移。
"""

from __future__ import annotations

import inspect
import os
import sys
import time

# 允许从 host/ 目录直接运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from host.registry import API_VERSION, COMMANDS, FRAMES  # noqa: E402
from host.protocol.commands import PARAM_IDS, PARAM_META  # noqa: E402
from host.api.events import Event  # noqa: E402
from host.api import MouseClient  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "docs", "api_reference.md")


def _first_line(doc: str) -> str:
    if not doc:
        return ""
    return doc.strip().splitlines()[0].strip()


def _public_methods(cls) -> list:
    out = []
    for name, fn in inspect.getmembers(cls, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        if getattr(fn, "__module__", "").startswith("host."):
            out.append((name, fn))
    return out


def _subapi(cls):
    """返回 (类名, 模块 docstring 首行, [(方法名, 签名, 说明), ...])"""
    methods = []
    for name, fn in _public_methods(cls):
        try:
            sig = str(inspect.signature(fn))
        except (ValueError, TypeError):
            sig = "(...)"
        methods.append((name, sig, _first_line(fn.__doc__)))
    return cls.__name__, _first_line(inspect.getmodule(cls).__doc__ or ""), methods


def _facade_members():
    """MouseClient 自身的公开成员 = 方法 + 属性(motor_disabled 这类必须是属性)。"""
    out = []
    for name, obj in inspect.getmembers(MouseClient):
        if name.startswith("_"):
            continue
        if isinstance(obj, property):
            out.append((name, "(属性)", _first_line(obj.__doc__)))
            continue
        if (inspect.isfunction(obj) or inspect.ismethod(obj)) and \
                getattr(obj, "__module__", "").startswith("host."):
            try:
                sig = str(inspect.signature(obj))
            except (ValueError, TypeError):
                sig = "(...)"
            out.append((name, sig, _first_line(obj.__doc__)))
    return out


def build() -> str:
    L = []
    L.append("# 上位机可编程接口参考（API Reference）")
    L.append("")
    L.append(f"> 由 `tools/gen_api_reference.py` 自动生成，勿手改。生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    L.append(f"> API 版本：`{API_VERSION}`　|　包：`host`")
    L.append("")
    L.append("> 这是「让上位机做一件事 = 调一个方法」的完整接口清单。")
    L.append("> 每个方法都会等待设备应答（默认 `wait=True` 且带超时），脚本能写直线代码。")
    L.append("")
    L.append("## 1. 快速开始")
    L.append("")
    L.append("```python")
    L.append("from host.api import MouseClient, Event")
    L.append("")
    L.append("with MouseClient.connect_to(\"COM7\") as mc:        # 退出自动断开+收会话")
    L.append("    mc.telemetry.wait_first_frame(timeout=5)        # 等首帧")
    L.append("    print(mc.device)                                # 设备自述(proto/fw/mazetype)")
    L.append("    mc.params.set(\"TRACKL\", 53.2015)                # 写参数(ACK+回读校验)")
    L.append("    h = mc.telemetry.history_dict(seconds=10)       # 统一帧历史(拉取式, 帧率实测)")
    L.append("    print(mc.analyze.explain(h))                    # 按动作段给出指标")
    L.append("```")
    L.append("")
    L.append("无硬件时用 `mc.connect(\"tcp://127.0.0.1:5555\")` 连模拟器，或 `replay://<会话目录>` 回放。")
    L.append("")
    L.append("## 2. 线程契约")
    L.append("")
    L.append("| 线程 | 职责 |")
    L.append("|---|---|")
    L.append("| 读取线程 | 读字节 → 解析 → 写 store / 回调里只入队 |")
    L.append("| 调度线程 | `poll()` / 事件派发 / 订阅式落盘 / 周期任务；**订阅回调在此线程执行** |")
    L.append("| 调用者线程 | 调 API；`wait=True` 时阻塞等待 |")
    L.append("")
    L.append("> 事件订阅回调里**不得**调用会阻塞等待设备的方法（会自锁），误用会抛 `ReentrancyError`。")
    L.append("")

    L.append("## 3. 命令登记表（`registry.COMMANDS`）")
    L.append("")
    L.append("新增功能只改这一张表。| name | 线上示例 | args | since | 说明 |")
    L.append("|---|---|---|---|---|")
    for name, spec in sorted(COMMANDS.items()):
        try:
            example = spec.build(**{k: {"name": "TRACKL", "q": 3486515, "v": "53.2",
                                        "item": "GYRO", "act": "START",
                                        "pulses": 3000, "speed": 40, "pwm": 2000,
                                        "ms": 100, "sp_l": 40, "sp_r": 40, "ir_mm": 10,
                                        "exp": 3, "a": 50, "b": 200, "c": 800,
                                        "d": 200,
                                    }.get(k, 0) for k in spec.args})
        except Exception:
            example = spec.name
        args = ", ".join(spec.args) or "—"
        L.append(f"| `{name}` | `{example}` | `{args}` | {spec.since} | {spec.note} |")
    L.append("")

    L.append("## 4. 帧登记表（`registry.FRAMES`）")
    L.append("")
    L.append("| type | 名称 | 流 | since |")
    L.append("|---|---|---|---|")
    for ftype, spec in sorted(FRAMES.items()):
        L.append(f"| 0x{ftype:02X} | {spec.name} | {spec.stream} | {spec.since} |")
    L.append("")

    L.append("## 5. 参数表（`PARAM_IDS`）")
    L.append("")
    L.append("| name | id | 单位 | 小数位 | 说明 |")
    L.append("|---|---|---|---|---|")
    for name, nid in sorted(PARAM_IDS.items(), key=lambda kv: kv[1]):
        unit, dec, note = PARAM_META.get(name, ("", 4, ""))
        L.append(f"| `{name}` | {nid} | {unit} | {dec} | {note} |")
    L.append("")

    L.append("## 6. 事件（`Event`）")
    L.append("")
    L.append("| 事件 | 载荷 |")
    L.append("|---|---|")
    for ev in Event:
        if ev is Event.ANY:
            continue
        doc = {
            "state": "链路状态变更 {state, detail}",
            "error": "打开/读/解码错误 {kind, msg}",
            "telemetry": "一帧遥测 {stream, rec}",
            "text": "设备文本(含 printf) {line, direction}",
            "param": "参数回读 {id, name, q}",
            "calib": "标定消息 {kind, fields, raw}(v2.0 起 CAL 为请求-响应, 一般经 ACK 返回)",
            "move": "运动结束消息 {kind, fields, raw}(v2.0 起经 EVENT 派发 `STOPPED`)",
            "estop": "急停触发 {kind, fields, raw}",
            "event": "统一上行事件 {event, reason, seq, text, raw} (STOPPED / ESTOP TRIP)",
            "timesync": "时间同步消息 {kind, fields, raw} (kind=anchor/armed/lost)",
            "map": "墙图 {mazetype, posX, posY, ...}",
            "advice": "链路诊断 {level, got_bps, need_bps, note}",
            "session": "会话开关/落盘 {op, path}",
        }.get(ev.value, "")
        L.append(f"| `{ev.value}` | {doc} |")
    L.append("")

    L.append("## 7. 门面与子 API 方法")
    L.append("")
    L.append("### 7.1 `MouseClient` — 门面本身")
    L.append("")
    L.append("| 成员 | 签名 | 说明 |")
    L.append("|---|---|---|")
    for mname, sig, note in _facade_members():
        L.append(f"| `{mname}` | `{sig}` | {note} |")
    L.append("")
    subapis = [
        ("conn", "连接管理"),
        ("params", "参数"),
        ("telemetry", "遥测"),
        ("stream", "遥测流控制"),
        ("calib", "标定"),
        ("move", "开环移动"),
        ("lab", "电机实验(远程运动)"),
        ("timesync", "时间同步 / 数据收集"),
        ("maze", "迷宫"),
        ("analyze", "离线分析"),
        ("session", "会话记录"),
    ]
    for i, (attr, title) in enumerate(subapis, start=2):
        name, moddoc, methods = _subapi(_class_of(attr))
        L.append(f"### 7.{i} `mc.{attr}` — {title}")
        L.append("")
        if moddoc:
            L.append(f"{moddoc}")
            L.append("")
        L.append("| 方法 | 签名 | 说明 |")
        L.append("|---|---|---|")
        for mname, sig, note in methods:
            L.append(f"| `{mname}` | `{sig}` | {note} |")
        L.append("")

    L.append("### 7.%d `host.api.calc` — 标定算参(纯函数)" % (len(subapis) + 2))
    L.append("")
    L.append("不碰设备、不作副作用, 可脱离硬件单测; 也可用于离线回放。")
    L.append("v2.4 从课程层迁出后独立成模块 —— **能力保留, 与课程无关**。")
    L.append("")
    L.append("| 函数 / 类型 | 作用 |")
    L.append("|---|---|")
    for line in [
        "`theory_track_ratio(...) -> float` | 里程系数理论值(由机械参数算, 默认 ≈62.24)",
        "`theory_gyro_ratio(...) -> float` | 陀螺角速率系数理论值(由 ADC 刻度算, 默认 ≈415.8)",
        "`solve_ratio(cur, reading, truth) -> float` | 线性标定: 新系数 = 旧系数 × 读数 / 真值",
        "`error_ratio(reading, truth) -> float` | 相对误差(%), 用于「误差 ≤3%」验收线",
        "`StepMetrics` / `step_metrics(t_ms, y, ...)` | 阶跃响应 σ% / ts / tp / rise",
        "`StraightMetrics` / `straightness_metrics(t, sl, sr, gy)` | 直行质量: 航向漂移 + 左右轮不对称",
        "`IrErrorStats` / `ir_error_stats(measured, truth)` | 红外标定误差: max/mean/RMS/最差点",
        "`pick_best_position(stats_by_pos)` | 在若干标定位置里选平均误差最小的",
    ]:
        L.append(f"| {line} |")
    L.append("")

    L.append("## 8. 命令行工具")
    L.append("")
    L.append("```bash")
    L.append("python -m host.cli <子命令>        # 每个子命令 = 一次 API 调用; --json 输出结构化结果")
    L.append("python -m host.monitor [目标]      # 实时数据仪表盘(标定/回传阶段主用)")
    L.append("```")
    L.append("")
    L.append("| 子命令 | 说明 |")
    L.append("|---|---|")
    for name, help_text in _cli_subcommands().items():
        L.append(f"| `{name}` | {help_text} |")
    L.append("")
    L.append("`host.monitor` 参数: " + "　".join(_monitor_options()))
    L.append("")
    return "\n".join(L).rstrip() + "\n"


def _subcommands(parser):
    """取出 argparse 的子命令 -> 帮助文本(不依赖私有属性名的方式尽量稳)。"""
    import argparse

    for act in parser._actions:  # noqa: SLF001
        if isinstance(act, argparse._SubParsersAction):  # noqa: SLF001
            helps = {ca.dest: (ca.help or "") for ca in getattr(act, "_choices_actions", [])}
            return {name: helps.get(name, "") for name in act.choices}
    return {}


def _cli_subcommands():
    from host import cli as _cli

    return _subcommands(_cli._build_parser())


def _monitor_options():
    from host import monitor as _mon

    parser = getattr(_mon, "build_parser", None) or getattr(_mon, "_build_parser")
    out = []
    for act in parser()._actions:  # noqa: SLF001
        if not act.option_strings:
            continue
        flag = act.option_strings[0]
        if flag in ("-h", "--help"):
            continue
        out.append(f"`{flag}`" + (f" {act.help}" if act.help else ""))
    return out


def _class_of(attr: str):
    from host.api import (
        AnalyzeAPI, CalibAPI, ConnAPI, MazeAPI, MotorLabAPI, MoveAPI,
        ParamsAPI, SessionAPI, StreamAPI, TelemetryAPI, TimeSyncAPI,
    )
    return {
        "conn": ConnAPI, "params": ParamsAPI, "telemetry": TelemetryAPI,
        "stream": StreamAPI, "calib": CalibAPI, "move": MoveAPI,
        "lab": MotorLabAPI, "timesync": TimeSyncAPI,
        "maze": MazeAPI, "analyze": AnalyzeAPI,
        "session": SessionAPI,
    }[attr]


def main() -> int:
    md = build()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"已生成 {OUT}  ({len(md)} 字符)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
