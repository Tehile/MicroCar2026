"""电脑鼠上位机『实时数据监视器』—— 终端里的实时仪表盘。

阶段用途(数据回传 / 标定):
    固件此时运行在安全测试模式(`MOUSE_TEST_NO_MOTOR`, 见 code_stm32f1_mouse/USER/main.h),
    电机被物理禁用、板子不会启动; 上位机这一侧要做的就是**把它检测到的数据实时显示出来**。

用法(在 host/ 目录下):
    python -m host.monitor                        # 自动挑串口; 没有串口则连模拟器 TCP
    python -m host.monitor COM7 --baud 115200     # 指定串口
    python -m host.monitor tcp://127.0.0.1:5555   # 连模拟器
    python -m host.monitor replay://sessions/2026-09-27_0151   # 回放历史会话
    python -m host.monitor COM7 --record          # 同时把会话落盘
    python -m host.monitor COM7 --json --seconds 10   # 每行一条 JSON(给脚本/管道)

显示内容(全部来自**统一帧**, 约 217 Hz 全量快照):
    链路   实测吞吐 / 帧率 / 坏CRC / 丢帧 / 连接状态 / 安全模式标志
    运动   左右轮实测速度、航向角、pid 误差、当前运动任务
    传感器 四路红外 mm + 原始 ADC、墙位、迷宫坐标与任务
    系统   电池电压、状态位、上行事件流完整性

只依赖 `host` 包, **不需要 PySide6** —— Python 3.8+、SSH/无显示器环境都能跑。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

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
    """着色开关(被 --no-color / 非 tty 关闭后所有方法退化为原字符串)。"""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def __call__(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles:
            return text
        pre = "".join(_CLR.get(s, "") for s in styles)
        return f"{pre}{text}{_CLR['reset']}"


def _bar(value: float, lo: float, hi: float, width: int = 10, fill: str = "█",
         empty: str = "░") -> str:
    """把数值画成定宽进度条(负值按 lo 处理)。"""
    if hi <= lo:
        return empty * width
    frac = (max(lo, min(hi, float(value))) - lo) / (hi - lo)
    n = int(round(frac * width))
    return fill * n + empty * (width - n)


def _hms(seconds: float) -> str:
    s = int(max(0.0, seconds))
    return f"{s // 3600:02d}:{(s // 60) % 60:02d}:{s % 60:02d}"


# ---------------------------------------------------------------- 状态位

def _state_bits(f: Dict[str, Any], c: C, expect_safe: bool = False) -> List[str]:
    """把统一帧里的状态位翻成中文短语(带颜色)。"""
    out: List[str] = []
    if not f:
        return out
    if f.get("motors_off"):
        out.append(c("电机已禁用(安全模式)", "green", "bold"))
    elif expect_safe:
        out.append(c("⚠ 未检测到电机禁用标志!", "red", "bold"))
    if f.get("estop_lock"):
        out.append(c("急停锁存(需 STOP 解锁)", "red", "bold"))
    elif f.get("emerg"):
        out.append(c("急停", "red", "bold"))
    if f.get("sleep"):
        out.append(c("休眠", "yellow"))
    if f.get("freemode"):
        out.append(c("自由模式", "yellow"))
    if f.get("evq"):
        out.append(c("⚠ 事件流有丢弃", "magenta", "bold"))
    return out


# ---------------------------------------------------------------- 渲染

def render(
    f: Dict[str, Any],
    stats: Dict[str, Any],
    fps: Dict[str, float],
    target_label: str,
    device: Dict[str, Any],
    elapsed: float,
    c: C,
    width: int = 78,
    expect_safe: bool = False,
    error: str = "",
    maze: Optional[Dict[str, Any]] = None,
) -> str:
    """把当前快照渲染成一段多行文本(不含清屏/光标控制)。"""
    line = "─" * width
    ts = time.strftime("%H:%M:%S")
    out: List[str] = []
    add = out.append

    # ── 标题
    add(c("电脑鼠实时数据", "bold", "cyan") + " " + c(line, "dim"))
    rst = device.get("rst", "?")
    add(f"  目标 {c(target_label, 'bold')}   设备 proto={device.get('proto', '?')} "
        f"fw={device.get('fw', '?')} maze={device.get('mazetype', '?')} 复位={rst}"
        f"   {c(ts, 'dim')} 已运行 {_hms(elapsed)}")

    # ── 链路
    bps = float(stats.get("rx_bps", 0.0))
    st = c("已连接", "green") if stats.get("_open", True) else c("未连接", "red")
    drop = int(stats.get("bad_crc", 0)) + int(stats.get("bad_sync", 0))
    lost = int(stats.get("seq_lost", 0))
    add(f"  链路 {_bar(bps, 0, 11520, 12)} {bps:7.0f} B/s   "
        f"帧 {fps.get('unified', 0):6.1f}/s   坏CRC {drop}   丢帧 {lost}   状态 {st}")
    bits = _state_bits(f, c, expect_safe=expect_safe)
    if bits:
        add("  状态 " + " / ".join(bits))
    if error:
        add("  " + c("链路错误: " + error, "red"))

    # ── 运动
    if f:
        sl, sr = float(f.get("speedL", 0)), float(f.get("speedR", 0))
        add("")
        add(c("  运动", "bold"))
        add("    左轮 " + c(f"{sl:+7.1f}", "cyan") + " cm/s   "
            "右轮 " + c(f"{sr:+7.1f}", "cyan") + " cm/s")
        add(f"    航向 {float(f.get('gyroAngle', 0)):+8.1f} °            "
            f"任务 {MOTION_TASK_NAMES.get(int(f.get('motionTask', 0)), str(f.get('motionTask', '?')))}")
        md = f.get("mapd") or {}
        add(f"    里程 L{int(f.get('trackL', 0)):6d} R{int(f.get('trackR', 0)):6d} "
            f"地图 +{int(md.get('count', 0))}"
            + (" re" if md.get("resync") else "")
            + (" LY" if md.get("lossy") else ""))
    else:
        add("")
        add(c("  运动  —— 尚未收到统一帧", "dim"))

    # ── 传感器
    if f:
        f1, f2 = int(f.get("irF1", 0)), int(f.get("irF2", 0))
        l, r = int(f.get("irL", 0)), int(f.get("irR", 0))
        add("")
        add(c("  传感器", "bold"))
        add(f"    前 f1 {f1:4d} {_bar(f1, 0, 255, 8)}   f2 {f2:4d} {_bar(f2, 0, 255, 8)}  mm")
        add(f"    左 {l:4d} {_bar(l, 0, 255, 8)}     右 {r:4d} {_bar(r, 0, 255, 8)}  mm")
        names = [("左", int(f.get("wallLeft", 0))), ("前", int(f.get("wallFront", 0))),
                 ("右", int(f.get("wallRight", 0)))]
        walls = " ".join(f"{n}{c('●', 'red') if v else '○'}" for n, v in names)
        add(f"    墙位 {walls}      坐标 ({int(f.get('posX', 0))},{int(f.get('posY', 0))})"
            f"  {f.get('mazeTaskName', '')} {f.get('dirName', '')}  "
            f"前墙 {f.get('frontName', '无墙')}")
    else:
        add("")
        add(c("  传感器  —— 尚未收到统一帧", "dim"))

    # ── 迷宫状态机: 只在有数据时出现
    if maze and maze.get("running"):
        add("")
        add(c("  迷宫状态机", "bold"))
        add(f"    任务 {c(str(maze.get('task', '?')), 'bold', 'cyan')}  "
            f"路口 {c(str(maze.get('roadKind', '?')), 'yellow')}("
            f"{maze.get('road', '?')})  开口(L,F,R) {maze.get('opens')}  "
            f"岔路栈 {int(maze.get('stack', 0))}")
        add(f"    位置 {maze.get('pos')}  终点 {maze.get('goal')}  动作 {maze.get('motion')}")
        if maze.get("evDropped"):
            add("    " + c("⚠️ 有事件被丢弃(链路拥塞): 上行事件不完整", "yellow"))

    # ── 系统
    mv = int(f.get("battMv", 0))
    volt = mv / 1000.0
    vcolor = "red" if volt < 6.5 else ("yellow" if volt < 7.0 else "green")
    add("")
    add(f"  系统   电压 {c(f'{volt:5.2f} V', vcolor)}   统一帧 {int(stats.get('unified', 0))}   "
        f"事件 {int(stats.get('event', 0))}   文本 {int(stats.get('text', 0))}")
    if f:
        add(f"    红外采样 {'开' if f.get('irSwitch') else '关'}   "
            f"冲刺档 {f.get('spurtModeName', '未选')}   前行格数 {int(f.get('goaheadnum', 0))}")
    add(c(line, "dim"))
    return "\n".join(out)


# ---------------------------------------------------------------- fps 统计

class _FpsMeter:
    """由累计帧计数推算每秒帧数(0.5s 平滑窗口)。"""

    def __init__(self) -> None:
        self._t0 = time.time()
        self._prev: Dict[str, int] = {}
        self._val: Dict[str, float] = {"unified": 0.0}

    def update(self, stats: Dict[str, Any]) -> Dict[str, float]:
        now = time.time()
        dt = now - self._t0
        if dt < 0.5:
            return self._val
        cur = int(stats.get("unified", 0))
        self._val["unified"] = (cur - self._prev["unified"]) / dt if "unified" in self._prev else 0.0
        self._prev["unified"] = cur
        self._t0 = now
        return self._val


# ---------------------------------------------------------------- 主流程

def _snapshot(mc) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """取一次显示快照: (unified, stats, device, maze)。"""
    f = mc.telemetry.latest()
    st = mc.stats()
    st["_open"] = mc.is_connected
    dev = mc.device if mc.is_connected else {}
    maze: Dict[str, Any] = {}
    if mc.is_connected:
        try:
            maze = mc.maze.summary()
        except Exception:
            maze = {}
    return f, st, dev, maze


def run_monitor(args) -> int:
    from .api import MouseClient, coerce_target

    target = coerce_target(args.target, baud=args.baud)
    label = target.label()

    color_on = (not args.no_color) and sys.stdout.isatty()
    c = C(color_on)

    mc = MouseClient(auto_session=args.record, auto_stream=not args.no_stream)
    if not mc.connect(target):
        print(c(f"连接失败: {mc.state_detail or '未知原因'}", "red"))
        mc.close()
        return 2

    print(c(f"已连接 {label}", "green", "bold"))
    if mc.auto_stream:
        print(c("已开始拉取遥测(TELEM); 等待首帧…", "dim"))
    if not mc.telemetry.wait_first_frame(timeout=args.timeout):
        print(c("仍未收到统一帧。可能原因: 未开始拉取、"
                "波特率不符、或蓝牙链路缓冲积压(停发片刻即可排空)。", "yellow"))

    fps = _FpsMeter()
    t0 = time.time()

    def _one_frame() -> str:
        f, st, dev, maze = _snapshot(mc)
        return render(
            f, st, fps.update(st), label, dev, time.time() - t0, c,
            width=args.width, expect_safe=args.expect_safe,
            error=(mc.state_detail or "") if mc.state == "error" else "",
            maze=maze,
        )

    try:
        while True:
            if args.json:
                f, st, _dev, maze = _snapshot(mc)
                print(json.dumps(
                    {"t": round(time.time(), 3), "target": label, "unified": f,
                     "rx_bps": round(float(st.get("rx_bps", 0.0)), 1),
                     "counts": {k: st.get(k) for k in ("unified", "map", "bad_crc",
                                                       "seq_gap", "seq_lost", "event", "text")},
                     "event_health": mc.event_health(),
                     "maze": maze or None},
                    ensure_ascii=False), flush=True)
            else:
                text = _one_frame()
                if color_on and not args.no_clear:
                    sys.stdout.write("\x1b[2J\x1b[H")
                print(text, flush=True)
            if args.once:
                break
            if args.seconds and (time.time() - t0) >= args.seconds:
                break
            time.sleep(max(0.02, 1.0 / max(0.5, float(args.hz))))
    except KeyboardInterrupt:
        print()
    finally:
        st = mc.stats()
        if not args.json:
            print(c(f"共收到统一帧 {int(st.get('unified', 0))} 个, "
                    f"平均 {float(st.get('rx_bps', 0)):.0f} B/s, "
                    f"坏CRC {int(st.get('bad_crc', 0))}, 丢帧 {int(st.get('seq_lost', 0))}", "dim"))
        if args.record and mc.session_obj is not None:
            print(c(f"会话已落盘: {mc.session_obj.path}", "dim"))
        mc.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="host.monitor",
        description="电脑鼠上位机实时数据监视器(终端仪表盘, 无需 PySide6)",
    )
    p.add_argument("target", nargs="?", default=None,
                   help="串口(COM7) / tcp://host:port / replay://会话目录; 省略则自动选择")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--hz", type=float, default=10.0, help="刷新率(默认 10)")
    p.add_argument("--width", type=int, default=78, help="面板宽度(字符)")
    p.add_argument("--seconds", type=float, default=0.0, help="持续多少秒后自动退出(0=一直跑)")
    p.add_argument("--once", action="store_true", help="只打印一次当前快照后退出(脚本用)")
    p.add_argument("--json", action="store_true", help="每行输出一条 JSON, 不画面板")
    p.add_argument("--no-color", action="store_true", help="关闭 ANSI 颜色")
    p.add_argument("--no-clear", action="store_true", help="不清屏, 追加打印(便于留日志)")
    p.add_argument("--no-stream", action="store_true",
                   help="不自动开始拉取(mc.stream.on); 设备需已有人在拉")
    p.add_argument("--record", action="store_true", help="同时开启会话落盘(sessions/)")
    p.add_argument("--timeout", type=float, default=5.0, help="等待首帧的秒数")
    p.add_argument("--expect-safe", action="store_true",
                   help="期望设备处于安全测试模式(电机禁用); 缺失该标志时高亮告警")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    return run_monitor(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
