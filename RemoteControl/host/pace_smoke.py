"""批式拉取(v2.9 剖面, v2.10 起统一于 `TelemetryPuller`)冒烟测试 —— `RATE PULL n t` 稀疏命令+每批多帧。

为什么单独一个文件: v2.9 新增的第二种拉取剖面(n≥2 批式; 与 n=1 停等式互斥),
它直接来自 2026-10-15 的真机压测结论(详见 `docs/链路MTBF压测方法.md` §13-15):
  · 停等式单条 `TELEM` 受 1/RTT 限制只有 ~4Hz;
  · 链路有 ~150ms 转发量子 ⇒ 真实容量 ≈13.3Hz, 26Hz 级必死;
  · 稀疏命令(3.3Hz) + 每批 3 帧(t=100ms) = 10Hz 留余量档: 300s/240.9s 两组存活,
    交付率 99.9%/98.6%, 传输量 2.5 倍 —— 即本类的默认配置。

本测试全部跑在**模拟器**上(同构实现了 `RATE` 族), 不断真机。真机验收另见
`front_check.py` / `link_pull_pace.py`。

用法(在 host/ 目录下):
    python pace_smoke.py
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

    def kill(self) -> None:
        """模拟"模块停转发": 直接杀进程, 不等 graceful。"""
        if self.proc is not None:
            self.proc.kill()
            self.proc = None

    def __exit__(self, *exc) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ---------------------------------------------------------------- 纯参数(无设备)

def p1_params() -> None:
    from host.api.stream import TelemetryPuller

    print("\n[P1] 参数与配置(不需要设备)")
    p = TelemetryPuller(None, 3, 100)
    check("默认 n=3", p.n == 3, str(p.n))
    check("默认 t=100ms", p.t_ms == 100, str(p.t_ms))
    check("T=300ms", abs(p.T_s - 0.3) < 1e-9, str(p.T_s))
    check("目标帧率 10Hz", abs(p.fps - 10.0) < 1e-9, str(p.fps))
    cfg = p.configure(4, 75)
    check("configure(4,75) 生效", (p.n, p.t_ms) == (4, 75), str(cfg))
    check("configure 返回 T=300ms", cfg["T_ms"] == 300, str(cfg))
    check("configure 返回 13.33Hz", abs(cfg["fps"] - 13.33) < 0.01, str(cfg))
    for bad in ((0, 75), (17, 75), (3, 0)):
        try:
            p.configure(*bad)
            check(f"configure{bad} 应拒绝", False, "未抛 ValueError")
        except ValueError:
            check(f"configure{bad} 正确拒绝", True)
    p.configure(16, 1)
    check("configure(16,1) 边界接受", (p.n, p.t_ms) == (16, 1))


# ---------------------------------------------------------------- 端到端(模拟器)

def p2_pull() -> None:
    from host.api import MouseClient

    print("\n[P2] 基本拉取: pace() 后按 ~10Hz 收帧、命令 3.3Hz、零丢批")
    with _Sim("--safe", "--rtt-ms", "80") as sim:
        mc = MouseClient(auto_session=False, auto_stream=False)
        mc.connect(sim.target)
        try:
            check("初始 mode=off", mc.stream.mode == "off", mc.stream.mode)
            r = mc.stream.pace()
            check("pace() 返回 OK", r.ok, r.status)
            check("mode=pace", mc.stream.mode == "pace", mc.stream.mode)
            check("is_on=True", mc.stream.is_on is True)
            st = mc.stream.stats()
            check("stats.mode=pace", st.get("mode") == "pace", str(st))
            check("stats.stream_mode=pace", st.get("stream_mode") == "pace", str(st))

            t0 = time.time()
            time.sleep(5.0)
            st = mc.stream.stats()
            el = time.time() - t0
            fps = st["frames"] / el
            print(f"     5s: frames={st['frames']} batches={st['batches']} "
                  f"fps={fps:.2f} rtt={st['rtt_ms']:.0f}ms lost={st['lost_batches']}")
            check("帧率在 8~12Hz(目标 10)", 8.0 <= fps <= 12.0, f"{fps:.2f}Hz")
            check("帧数 > 40", st["frames"] > 40, str(st["frames"]))
            check("批次数 ≈ 3.3Hz×5s", 12 <= st["batches"] <= 22,
                  str(st["batches"]))
            check("每批 3 帧(帧数≈3×批数)",
                  abs(st["frames"] - 3 * st["batches"]) <= 3,
                  f"frames={st['frames']} batches={st['batches']}")
            check("零丢批", st["lost_batches"] == 0, str(st["lost_batches"]))
            # RTT 估计应收敛到注入值附近(注入 80ms; 允许 ±60ms 的模拟器抖动)
            check("RTT 估计收敛(20~160ms)", 20.0 <= st["rtt_ms"] <= 160.0,
                  f"{st['rtt_ms']:.0f}ms")
            check("fps_target=10", st["fps_target"] == 10.0, str(st["fps_target"]))
        finally:
            mc.close()


def p3_exclusive() -> None:
    from host.api import MouseClient

    print("\n[P3] 模式互斥: pace/stopwait 不会双开")
    with _Sim("--safe", "--rtt-ms", "80") as sim:
        mc = MouseClient(auto_session=False, auto_stream=False)
        mc.connect(sim.target)
        try:
            mc.stream.pace()
            check("pace 在跑", mc.stream.mode == "pace")
            mc.stream.on()
            check("on() 后切回停等式", mc.stream.mode == "stopwait",
                  mc.stream.mode)
            st = mc.stream.stats()
            check("停等式 stats 正常(mode=stop-and-wait)",
                  st.get("mode") == "stop-and-wait", str(st))
            check("stream_mode=stopwait", st.get("stream_mode") == "stopwait",
                  str(st))
            mc.stream.pace()
            check("pace() 再切回批式", mc.stream.mode == "pace", mc.stream.mode)
            time.sleep(1.0)
            check("批式仍在收帧", mc.stream.stats()["frames"] > 5,
                  str(mc.stream.stats()))
            r = mc.stream.pace_off()
            check("pace_off() OK", r.ok, r.status)
            check("pace_off 后 mode=off", mc.stream.mode == "off",
                  mc.stream.mode)
            n0 = mc.stats().get("unified", 0)
            time.sleep(1.2)
            check("pace_off 后不再收帧(RATE OFF 生效)",
                  mc.stats().get("unified", 0) == n0,
                  f"{n0} -> {mc.stats().get('unified', 0)}")
        finally:
            mc.close()


def p4_yield() -> None:
    from host.api import MouseClient

    print("\n[P4] 让路契约: urgent 命令抢占链路, 嵌套 suspend 不提前恢复")
    with _Sim("--safe", "--rtt-ms", "80") as sim:
        mc = MouseClient(auto_session=False, auto_stream=False)
        mc.connect(sim.target)
        try:
            mc.stream.pace()
            mc.telemetry.wait_first_frame(timeout=6.0)
            check("pace 在跑", mc.stream.is_on is True)

            mc.stream.suspend()
            check("suspend 后停发", mc.stream.is_on is False)
            check("suspend 后 running=False", mc.stream.stats()["running"] is False,
                  str(mc.stream.stats()))
            n0 = mc.stats().get("unified", 0)
            time.sleep(1.0)
            check("suspend 期间无新帧", mc.stats().get("unified", 0) == n0,
                  f"{n0} -> {mc.stats().get('unified', 0)}")

            r = mc.send_cmd("estop", timeout=3.0)
            check("让路期间 ESTOP OK", r.ok, r.status)
            r = mc.send_cmd("estop.clear", timeout=3.0)
            check("让路期间 ESTOP CLEAR OK", r.ok, r.status)

            mc.stream.resume()
            check("resume 后恢复", mc.stream.is_on is True)
            got = mc.telemetry.wait_first_frame(timeout=6.0)
            check("resume 后重新收到帧", got)

            mc.stream.suspend()
            mc.stream.suspend()
            check("嵌套 suspend 仍停发", mc.stream.is_on is False)
            mc.stream.resume()
            check("内层 resume 不恢复", mc.stream.is_on is False)
            mc.stream.resume()
            check("外层 resume 才恢复", mc.stream.is_on is True)
        finally:
            mc.close()


def p5_link_lost() -> None:
    from host.api import MouseClient
    from host.api.events import Event

    print("\n[P5] 断连检测: 连续 3s 无帧 ⇒ link_lost 事件 + dead(不再自动重试)")
    sim = _Sim("--safe", "--rtt-ms", "80")
    with sim:
        mc = MouseClient(auto_session=False, auto_stream=False)
        mc.connect(sim.target)
        try:
            mc.stream.pace()
            mc.telemetry.wait_first_frame(timeout=6.0)
            time.sleep(1.0)
            lost_evts: List[dict] = []

            def _on_err(msg) -> None:
                d = getattr(msg, "data", None) or {}
                if d.get("kind") == "link_lost":
                    lost_evts.append(d)

            mc.on(Event.ERROR, _on_err)
            sim.kill()                      # 模拟模块停转发
            deadline = time.time() + 6.0
            while time.time() < deadline and not lost_evts:
                time.sleep(0.1)
            check("收到 link_lost 事件", len(lost_evts) == 1,
                  f"{len(lost_evts)} 次")
            if lost_evts:
                check("事件提示物理断电复位",
                      "断电" in lost_evts[0].get("msg", ""),
                      lost_evts[0].get("msg", ""))
            st = mc.stream.stats()
            check("dead=True", st.get("dead") is True, str(st))
            check("running=False", st.get("running") is False, str(st))
            check("mode=off(不再自动重试)", mc.stream.mode == "off",
                  mc.stream.mode)
        finally:
            mc.close()


def p6_tune() -> None:
    from host.api import MouseClient

    print("\n[P6] 运行期调参: tune(n,t) 与 pace(n,t) 动态切档")
    with _Sim("--safe", "--rtt-ms", "80") as sim:
        mc = MouseClient(auto_session=False, auto_stream=False)
        mc.connect(sim.target)
        try:
            mc.stream.pace()
            mc.telemetry.wait_first_frame(timeout=6.0)
            cfg = mc.stream.tune(n=2, t_ms=150)
            check("tune 返回 pace 配置", cfg["pace"]["n"] == 2
                  and cfg["pace"]["t_ms"] == 150, str(cfg))
            check("tune 后 T=300ms", cfg["pace"]["T_ms"] == 300, str(cfg))
            check("tune 后 6.67Hz", abs(cfg["pace"]["fps"] - 6.67) < 0.01,
                  str(cfg))
            time.sleep(4.0)
            st = mc.stream.stats()
            fps = st["frames"] / max(0.1, time.time() - 0)  # 近似: 用累计即可
            check("切档后批号继续增长", st["batches"] > 8, str(st["batches"]))
            check("切档后每批 2 帧", abs(st["frames"] - 2 * st["batches"]) <= 3,
                  f"frames={st['frames']} batches={st['batches']}")

            r = mc.stream.pace(n=3, t_ms=100)
            check("pace(n,t) 切回 10Hz 档", r.ok, r.status)
            check("切回后 stats n=3", mc.stream.stats()["n"] == 3,
                  str(mc.stream.stats()))
            time.sleep(3.0)
            st = mc.stream.stats()
            check("10Hz 档恢复收帧", st["frames"] > 20, str(st["frames"]))
            check("tune 不接受非法 n", _reject_tune(mc))
        finally:
            mc.close()


def _reject_tune(mc) -> bool:
    try:
        mc.stream.tune(n=99)
        return False
    except ValueError:
        return True


def main() -> int:
    from host.api import MouseClient  # noqa: F401  (提前暴露 import 错误)

    print("=" * 66)
    print("批式拉取冒烟(v2.9 剖面 / v2.10 统一类, 模拟器)")
    print("=" * 66)
    p1_params()
    p2_pull()
    p3_exclusive()
    p4_yield()
    p5_link_lost()
    p6_tune()
    print("\n" + "=" * 66)
    print(f"通过 {PASS} / 失败 {FAIL}")
    if FAILED:
        print("失败项:", FAILED)
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
