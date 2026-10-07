"""电机远程控制实验(单流)冒烟测试 —— 把"确保可靠"变成可断言的事实。

这个文件要回答四个问题, 每一个都是现场可能出错的地方:

    L1 时间护栏   `MOVE RUN` 必须在指定时间后**自己**停(不靠上位机)
    L2 红外护栏   任一红外读数低于阈值时必须立即停
    L3 急停锁存   ESTOP 之后 MOVE/CAL 一律被拒(ERR_ESTOP), 只有 STOP 能解锁
    L4 单流       运动期间统一帧(≈217Hz 全量快照)本地捕获即可, 无需停车后整块回传

外加 L5: 安全固件(MOUSE_TEST_NO_MOTOR)下 `lab.run()` 必须**立即**拒绝, 而不是空等。

v2.0(2026-10-05) 的关键差异(见《传输模式重构设计方案》):
  · `MOVEDONE RUN` 明细行取消 -> 运动结束只有上行事件 `STOPPED <reason> #n`;
    `RunResult` 因此不再有 elapsed_ms / peak_l / dL / ir_chan — 那些量在
    `res.realtime`(墙钟/帧数/吞吐)与 `res.samples`(逐点采样)里;
  · `SCOPE`/`DUMP` 双流机制删除 -> 原 L4"停止后 1kHz 日志整块回传"改为
    "运动期间统一帧本地捕获"; `fetch_log`/`log_ready`/`realtime_hz` 等参数全部取消。

用法(在 host/ 目录下):
    python motor_lab_smoke.py
"""

from __future__ import annotations

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


def _connect(sim: "_Sim"):
    from host.api import MouseClient

    mc = MouseClient(auto_session=False)
    if not mc.connect(sim.target):
        raise RuntimeError(f"连接失败: {mc.state_detail}")
    mc.telemetry.wait_first_frame(timeout=5.0)
    time.sleep(0.3)
    return mc


# ---------------------------------------------------------------- L1/L2

def test_guards() -> None:
    from host.protocol.codec import RUN_REASON_NAMES

    print("\n[L1] 时间护栏: 到时必停(不靠上位机)")
    with _Sim() as sim:
        mc = _connect(sim)
        try:
            r = mc.lab.run(sp_l=40, sp_r=40, ms=400, ir_mm=10, sample_hz=20.0)
            check("MOVE RUN 被接受并回 STOPPED", r.ok, f"{r.status} {r.error}")
            check("停止原因是 TIME", r.reason == "TIME", str(r.reason))
            check("停止原因已译中文", r.reason_text == RUN_REASON_NAMES["TIME"], str(r.reason_text))
            el = r.realtime.get("seconds", 0.0)
            check(f"运行时长≈400ms(实测 {el:.2f}s)", 0.30 <= el <= 0.70, str(el))
            check("运动期间统一帧在流",
                  r.realtime.get("frames", 0) > 0, str(r.realtime))
            check("本地实时采样有数据(电机确实转过)", len(r.samples) > 0, str(len(r.samples)))
            check("采样里出现非零轮速",
                  any(abs(s["speedL"]) > 0 or abs(s["speedR"]) > 0 for s in r.samples),
                  str(r.samples[:3]))
            check("明细行原文可回溯(STOPPED <reason> #n)",
                  (r.raw or "").startswith("STOPPED"), str(r.raw))
        finally:
            mc.close()

    print("\n[L2] 红外护栏: 贴脸必停")
    with _Sim() as sim:
        mc = _connect(sim)
        try:
            st = mc.lab.status()
            ir_min = st["ir_min"]
            check("能读到四路红外", ir_min is not None, str(st["ir"]))
            # 阈值取"比当前最小读数略高" -> 必定在第一个周期触发
            thr = int(ir_min) + 5
            r = mc.lab.run(sp_l=40, sp_r=40, ms=3000, ir_mm=thr, sample_hz=20.0)
            check("MOVE RUN 被接受", r.ok, f"{r.status} {r.error}")
            check("停止原因是 IR", r.reason == "IR", str(r.reason))
            check("远早于时间护栏就停了",
                  r.realtime.get("seconds", 9999) < 1.0, str(r.realtime.get("seconds")))
            # v2.0: 触发通道/最小读数不再由事件行上报 —— 它们在统一帧的
            # irF1/irF2/irL/irR 里逐帧都有, 由本地采样给出。
            check("本地采样带出触发时的最小红外",
                  bool(r.samples) and r.samples[-1].get("ir_min") is not None,
                  str(r.samples[-1] if r.samples else None))

            # 负值 = 关闭红外护栏 -> 时间护栏重新成为唯一裁判
            r2 = mc.lab.run(sp_l=40, sp_r=40, ms=400, ir_mm=-1, sample_hz=20.0)
            check("ir_mm 传负值可关闭红外护栏(改由时间护栏停)",
                  r2.ok and r2.reason == "TIME", f"{r2.status} {r2.reason}")
        finally:
            mc.close()


# ---------------------------------------------------------------- L3

def test_estop_lock() -> None:
    print("\n[L3] 急停锁存: ESTOP 之后不能动, 只有 STOP 能解锁")
    with _Sim() as sim:
        mc = _connect(sim)
        try:
            check("初始未锁存", mc.estop_locked is False, str(mc.estop_locked))

            r = mc.lab.estop()
            check("ESTOP 返回 OK", r.ok, r.status)
            time.sleep(0.3)
            check("统一帧标志已置'急停锁存'", mc.estop_locked is True, str(mc.estop_locked))

            r = mc.move.run(sp_l=40, sp_r=40, ms=200)
            check("锁存期间 MOVE RUN 被拒", not r.ok and r.status == "ESTOP_LOCKED",
                  f"{r.ok} {r.status} {r.data}")

            r = mc.move.forward(pulses=1000, wait_done=False)
            check("锁存期间 MOVE FWD 也被固件拒(ERR_ESTOP)",
                  (not r.ok) and r.status == "ERR_ESTOP", f"{r.ok} {r.status}")

            r = mc.calib.angle()
            check("锁存期间需要运动的标定项被拒", r.status == "ERR_ESTOP", r.status)

            r = mc.calib.wall_distance()
            check("锁存期间静态标定项照常放行", r.ok, r.status)

            r = mc.lab.resume()
            check("STOP 解锁返回 OK", r.ok, r.status)
            time.sleep(0.3)
            check("解锁后标志清除", mc.estop_locked is False, str(mc.estop_locked))

            r = mc.lab.run(sp_l=40, sp_r=40, ms=300, sample_hz=20.0)
            check("解锁后 MOVE RUN 恢复可用", r.ok, f"{r.status} {r.error}")
        finally:
            mc.close()


# ---------------------------------------------------------------- L4

def test_single_stream() -> None:
    print("\n[L4] 单流: 运动期间统一帧全速本地捕获(取代 v1.5.5 的双流)")
    with _Sim() as sim:
        mc = _connect(sim)
        try:
            res = mc.lab.run(sp_l=40, sp_r=40, ms=1500, ir_mm=10, sample_hz=50.0)
            check("实验完成", res.ok, f"{res.status} {res.error}")
            check("停止原因是 TIME", res.reason == "TIME", str(res.reason))
            check("实时流在运动期间确实有帧",
                  res.realtime.get("frames", 0) > 0, str(res.realtime))
            check("实时流没有坏帧", res.realtime.get("bad_crc", 1) == 0, str(res.realtime))
            # 53B/帧 @≈200Hz -> 约 10 KB/s: 这就是"不需要停车后回传"的底气
            check("统一帧全速在流(量级正确, 无需 SCOPE)",
                  res.realtime.get("approx_bps", 0) > 1000, str(res.realtime))
            check("本地实时采样有数据", len(res.samples) > 0, str(len(res.samples)))

            # 波形本身可信: 采样里出现过明显轮速
            peak = max((abs(s["speedL"]) for s in res.samples), default=0)
            check("采样里出现过明显轮速(电机真转过)", peak > 0, f"peak={peak}")

            txt = mc.lab.explain(res)
            check("explain() 给出可读结论", "停止原因" in txt, txt)
            check("explain() 带统一帧时长/帧数", "统一帧" in txt, txt)
        finally:
            mc.close()


# ---------------------------------------------------------------- L5

def test_safe_firmware() -> None:
    print("\n[L5] 安全固件(电机禁用)下必须快速拒绝, 不空等")
    with _Sim("--safe") as sim:
        mc = _connect(sim)
        try:
            check("识别出电机已禁用", mc.motor_disabled is True, str(mc.motor_disabled))
            st = mc.lab.status()
            check("status().can_move 为 False", st["can_move"] is False, str(st))
            t0 = time.time()
            res = mc.lab.run(sp_l=40, sp_r=40, ms=2000)
            dt = time.time() - t0
            check("lab.run 立即返回 MOTOR_DISABLED", res.status == "MOTOR_DISABLED", res.status)
            check("没有空等(<2s)", dt < 2.0, f"{dt:.2f}s")
            check("给出可操作提示", "MOUSE_TEST_MOTOR_LAB" in (res.error or ""), res.error or "")
        finally:
            mc.close()


def main() -> int:
    print("=" * 66)
    print("电机远程控制实验(单流)冒烟测试")
    print("=" * 66)
    test_guards()
    test_estop_lock()
    test_single_stream()
    test_safe_firmware()
    print("\n" + "=" * 66)
    print(f"通过 {PASS} / 失败 {FAIL}")
    if FAILED:
        print("失败项: " + ", ".join(FAILED))
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
