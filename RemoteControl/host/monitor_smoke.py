"""实时监视器冒烟测试 —— 证明"计算机侧实时显示数据"这条链真的通。

为什么要单独一个文件: `host/monitor.py` 是标定/回传阶段的**主用出口**,
而它的正确性没法靠"人能看见"保证(没人天天手动跑)。这里用模拟器把它变成可断言的:

    M1 --once --json  : 单帧快照能被机器读到, 且字段齐全、安全标志正确
    M2 面板文本        : 关键分区(链路/运动/传感器/系统)与"电机已禁用"都在
    M3 --expect-safe  : 设备**没有**置禁用位时必须告警(否则这个检查是安慰剂)
    M4 回放目标        : 同一套代码能跑 replay:// 会话(三种目标同码)

v2.0(2026-10-05): 监视器的数据源由"快/中/慢三路帧"改为**唯一的统一帧**
(`LINK_TYPE_UNIFIED`), 故 `--json` 的键是 `unified` 而非 `fast`;
`runlog`(运动日志就绪)位随双流一并删除, 取而代之的是 `evq`(事件队列丢弃)。
迷宫面板改由**主机侧**从统一帧状态量复刻(`classify_road()`), 不再是设备状态帧。

用法(在 host/ 目录下):
    python monitor_smoke.py
"""

from __future__ import annotations

import contextlib
import io
import json
import socket
import subprocess
import sys
import time
from typing import List

PASS = 0
FAIL = 0
FAILED: List[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f"  <- {detail}" if detail else ""))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _Sim:
    """上下文管理器: 拉起模拟器并在退出时收掉。"""

    def __init__(self, *extra: str) -> None:
        self.port = _free_port()
        self.cmd = [sys.executable, "-m", "host.sim.mouse_sim",
                    "--port", str(self.port), "--no-auto", *extra]
        self.proc: subprocess.Popen | None = None

    @property
    def target(self) -> str:
        return f"tcp://127.0.0.1:{self.port}"

    def __enter__(self) -> "_Sim":
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        time.sleep(1.2)
        return self

    def __exit__(self, *exc) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def _run_monitor(argv: List[str]) -> str:
    """跑一次监视器并捕获 stdout(监视器直接 print, 不返回字符串)。"""
    from host import monitor

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = monitor.main(argv)
    assert rc == 0, f"monitor 退出码 {rc}"
    return buf.getvalue()


def main() -> int:
    from host.monitor import C, render

    print("=" * 66)
    print("实时监视器冒烟测试")
    print("=" * 66)

    # ---------------------------------------------------------------- 纯函数部分
    print("\n[0] 渲染函数(不连设备)")
    # v2.0: 只有一个数据源(统一帧), 键名与 `UNIFIED_CHANNELS` 一致。
    fake = {
        # 状态位(来自统一帧的 flags 字节)
        "motors_off": True, "estop_lock": False, "emerg": False,
        "sleep": False, "freemode": True, "evq": False,
        # 运动
        "speedL": 12.0, "speedR": -3.0, "gyroAngle": 45.0, "pidErr": 1.0,
        "motionTask": 32, "motionStep": 3,
        "trackL": 120, "trackR": 118, "cellOdom": 1,
        # 传感器
        "irF1": 180, "irF2": 179, "irL": 40, "irR": 236,
        "irAdcF1": 1200, "irAdcF2": 1190, "irAdcL": 400, "irAdcR": 2300,
        "wallLeft": 1, "wallFront": 0, "wallRight": 1,
        "posX": 0, "posY": 0, "mazeTaskName": "SEARCH", "dirName": "N",
        "wallThresh": 110,
        # 系统
        "battMv": 7420, "fastwayCount": 0, "dropCnt": 0, "stackIdx": 2,
    }
    fake_stats = {"unified": 10, "map": 1, "rx_bps": 150.0, "bad_crc": 0,
                  "seq_lost": 0, "event": 1, "text": 0, "_open": True}
    fake_fps = {"unified": 5.0}
    txt = render(fake, fake_stats, fake_fps, "TEST",
                 {"proto": 2, "fw": 200, "mazetype": 7}, 1.0, C(False), expect_safe=True)
    check("渲染含四个分区", all(k in txt for k in ("链路", "运动", "传感器", "系统")), txt[:160])
    check("安全标志被翻译成中文", "电机已禁用" in txt)
    check("红外/墙位/坐标都在", "f1" in txt and "墙位" in txt and "(0,0)" in txt)
    check("无 ANSI 转义(非 tty)", "\x1b[" not in txt)
    txt2 = render(fake, fake_stats, fake_fps, "TEST", {}, 1.0, C(False), expect_safe=False)
    check("不带 --expect-safe 时不告警", "未检测到" not in txt2)

    # v2.0 的两个状态位: 急停锁存(优先于普通急停) / 事件队列丢弃
    lab = dict(fake, motors_off=False, freemode=False, estop_lock=True, emerg=True)
    txt3 = render(lab, fake_stats, fake_fps, "TEST", {}, 1.0, C(False))
    check("急停锁存被显示出来", "急停锁存" in txt3, txt3.splitlines()[3:6])
    check("锁存优先于普通急停(不重复显示)", txt3.count("急停") == 1, txt3)
    evq = dict(fake, motors_off=False, freemode=True, estop_lock=False, emerg=False, evq=True)
    txt4 = render(evq, fake_stats, fake_fps, "TEST", {}, 1.0, C(False))
    check("事件队列丢弃被显示出来", "事件流有丢弃" in txt4, txt4)
    plain = dict(fake, motors_off=False, freemode=True, estop_lock=False, emerg=False, evq=False)
    txt5 = render(plain, fake_stats, fake_fps, "TEST", {}, 1.0, C(False))
    check("无异常位时不显示锁存/事件丢弃",
          "急停" not in txt5 and "事件流有丢弃" not in txt5, txt5)

    # 迷宫面板: v2.0 起由**主机侧**从统一帧状态量复刻(不再是设备上报的状态帧)
    fake_maze = {"running": True, "task": "SEARCH", "roadKind": "T型", "road": 4,
                 "opens": (1, 0, 1), "stack": 2, "pos": (1, 2), "goal": (3, 3),
                 "motion": "FWD", "evDropped": True}
    txt6 = render(fake, fake_stats, fake_fps, "TEST", {}, 1.0, C(False), maze=fake_maze)
    check("迷宫状态机面板出现", "迷宫状态机" in txt6 and "路口" in txt6, txt6)
    check("迷宫事件丢弃告警出现", "事件被丢弃" in txt6)
    txt7 = render(fake, fake_stats, fake_fps, "TEST", {}, 1.0, C(False),
                  maze=dict(fake_maze, running=False))
    check("未跑迷宫时不显示该面板", "迷宫状态机" not in txt7)

    # ---------------------------------------------------------------- 连模拟器: 安全模式
    print("\n[1] 连模拟器(--safe): M1/M2/M3 正例")
    with _Sim("--safe") as sim:
        out = _run_monitor([sim.target, "--once", "--json", "--timeout", "6"])
        line = [ln for ln in out.splitlines() if ln.strip().startswith("{")][-1]
        obj = json.loads(line)
        check("M1 JSON 可解析", isinstance(obj, dict))
        check("M1 统一帧字段齐全",
              {"speedL", "speedR", "gyroAngle", "motionTask", "battMv"} <= set(obj["unified"]),
              str(obj["unified"]))
        check("M1 电机禁用位为真", obj["unified"]["motors_off"] is True, str(obj["unified"]))
        check("M1 counts 按 v2.0 口径(只有一个数据流)",
              {"unified", "map", "bad_crc", "seq_lost", "event"} <= set(obj["counts"]),
              str(obj["counts"]))

        panel = _run_monitor([sim.target, "--seconds", "2", "--no-clear", "--no-color",
                              "--hz", "2", "--timeout", "6", "--expect-safe"])
        check("M2 面板显示电机已禁用", "电机已禁用" in panel)
        check("M2 无 --expect-safe 告警", "未检测到电机禁用标志" not in panel)
        check("M2 传感器读数非空", "f1" in panel and "V" in panel)
        check("M2 结尾有帧统计", "共收到" in panel)

    # ---------------------------------------------------------------- 正例的反面: 没禁用位必须告警
    print("\n[2] 连模拟器(普通模式): M3 反例 —— 期望安全却没置位")
    with _Sim() as sim:
        panel = _run_monitor([sim.target, "--seconds", "2", "--no-clear", "--no-color",
                              "--hz", "2", "--timeout", "6", "--expect-safe"])
        check("M3 缺失禁用位时告警", "未检测到电机禁用标志" in panel, panel[:200])
        panel2 = _run_monitor([sim.target, "--once", "--no-clear", "--no-color",
                               "--timeout", "6"])
        check("M3 反例成立: 不带期望时不告警", "未检测到电机禁用标志" not in panel2)

    # ---------------------------------------------------------------- 回放
    print("\n[3] M4 回放目标(同一套代码)")
    import glob
    import os

    sess = sorted(glob.glob(os.path.join("sessions", "*")))
    sess = [s for s in sess if os.path.isdir(s)]
    if not sess:
        print("  [SKIP] 没有历史会话可回放")
    else:
        out = _run_monitor(["replay://" + sess[-1].replace("\\", "/"), "--once",
                            "--no-clear", "--no-color", "--timeout", "10"])
        check("M4 回放能出面板", "电脑鼠实时数据" in out)
        check("M4 回放标出目标为会话目录", "回放" in out)

    # ---------------------------------------------------------------- 需电机动作的前置拦截
    print("\n[4] motor_guard: 安全模式下「要电机转」的动作必须快失败(不空等 90s)")
    from host.api import MouseClient

    with _Sim("--safe") as sim:
        mc = MouseClient(auto_session=False)
        if mc.connect(sim.target):
            time.sleep(1.5)
            check("识别出电机已禁用", mc.motor_disabled is True, str(mc.motor_disabled))
            t0 = time.time()
            r = mc.calib.angle()
            dt = time.time() - t0
            check("calib.angle 立即失败(非超时)", r.status == "MOTOR_DISABLED", f"{r.status} {dt:.1f}s")
            check("失败很快(<3s, 不是等 90s 超时)", dt < 3.0, f"{dt:.2f}s")
            check("给出可操作的提示", "MOUSE_TEST_NO_MOTOR" in str(r.data.get("hint", "")), str(r.data))
            check("move.forward 同样被拦", mc.move.forward(wait_done=False).status == "MOTOR_DISABLED")
            # 底层通道不受限: 通路验证仍可用
            check("底层 calib.step 仍可发命令", mc.calib.step("GYRO", "ABORT").ok)
            mc.close()
        else:
            check("连接模拟器(--safe)", False, mc.state_detail)
            mc.close()

    with _Sim() as sim:
        mc = MouseClient(auto_session=False)
        if mc.connect(sim.target):
            time.sleep(1.5)
            check("正常固件下 motor_disabled=False", mc.motor_disabled is False, str(mc.motor_disabled))
            check("正常固件下 move.forward 放行", mc.move.forward(wait_done=False).ok)
            mc.close()
        else:
            check("连接模拟器(正常)", False, mc.state_detail)
            mc.close()

    print("\n" + "=" * 66)
    print(f"通过 {PASS} / 失败 {FAIL}")
    if FAILED:
        print("失败项: " + ", ".join(FAILED))
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
