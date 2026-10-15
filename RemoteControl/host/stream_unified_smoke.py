"""统一拉取器(v2.10 `TelemetryPuller`)冒烟测试 —— 停等/批式合并后的新行为。

覆盖合并带来的**新能力/新语义**(停等与批式各自的老契约仍由 `pull_smoke.py` /
`pace_smoke.py` 钉着, 本文件不重复):
  U1 退化: `pace(1, t)` 字面退化为停等(n=1 时 t_ms 被忽略);
  U2 双向切换: on()→pace()→on() 全程 is_on 不灭(一个对象, 互斥是结构性的);
  U3 锚刷新: 批式模式批中点注入 `TELEM`, `anchor_refreshes` 增长且锚年龄有界;
  U4 自节流: RTT>T(退化域)时命令间距 ≥T、整批完整、帧率 ≈ n/RTT;
  U5 telem 断连: 停等式连续无帧 ⇒ 7s 级 `link_lost` 事件 + dead(v2.10 新增);
  U6 tune 切档: telem 在跑时 `tune(n=2)` 立即变批式(跨模式自动重 arm);
  U7 统计并集: 两种剖面下 stats() 字段齐全且类型正确(下游两套旧读法都不破)。

设计依据: `docs/遥测拉取器统一设计方案.md` §9.2。全部跑在**模拟器**上, 不断真机。

用法(在 host/ 目录下):
    python stream_unified_smoke.py
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


# stats() 字段并集(设计 §8.2): 名字 -> 类型。两种剖面都必须给全。
UNION_FIELDS = {
    "mode": str, "running": bool, "n": int, "t_ms": int, "T_ms": int,
    "fps_target": float, "fps_obs": float, "sent": int, "done": int,
    "lost": int, "frames": int, "batches": int, "lost_batches": int,
    "inflight": int, "hz": float, "best_hz": float, "rtt_ms": float,
    "dead": bool, "suspends": int, "anchor_refreshes": int,
    "stream_mode": str,
}


def _check_union(tag: str, st: dict) -> None:
    miss = [k for k in UNION_FIELDS if k not in st]
    check(f"{tag}: stats 字段并集齐全(21 项)", not miss, str(miss))
    bad = [f"{k}:{type(st[k]).__name__}" for k, t in UNION_FIELDS.items()
           if k in st and not isinstance(st[k], t)]
    check(f"{tag}: stats 字段类型正确", not bad, str(bad))


# ---------------------------------------------------------------- U1 退化

def u1_degenerate() -> None:
    from host.api import MouseClient

    print("\n[U1] 退化: pace(1, t) 字面退化为停等(n=1 时 t_ms 被忽略)")
    with _Sim("--safe", "--rtt-ms", "80") as sim:
        mc = MouseClient(auto_session=False, auto_stream=False)
        mc.connect(sim.target)
        try:
            r = mc.stream.pace(1, 100)
            check("pace(1,100) 返回 OK", r.ok, r.status)
            check("mode=stopwait(不是 pace)", mc.stream.mode == "stopwait",
                  mc.stream.mode)
            st = mc.stream.stats()
            check("stats mode=stop-and-wait", st.get("mode") == "stop-and-wait",
                  str(st))
            check("stats n=1", st.get("n") == 1, str(st.get("n")))
            check("is_on=True", mc.stream.is_on is True)
            check("等到首帧", mc.telemetry.wait_first_frame(timeout=6.0))
            time.sleep(3.0)
            st = mc.stream.stats()
            print(f"     3s: {st}")
            check("在途恒不超过 1 条", st["inflight"] <= 1, str(st))
            check("请求三态自洽(sent=done+inflight, 无丢失)",
                  st["sent"] == st["done"] + st["inflight"] and st["lost"] == 0,
                  f"sent={st['sent']} done={st['done']} "
                  f"inflight={st['inflight']} lost={st['lost']}")
            check("帧=命令(1 命令 1 帧, 容一拍延迟)",
                  st["done"] <= st["frames"] <= st["done"] + 1,
                  f"done={st['done']} frames={st['frames']}")
            check("batches 退化字面成立(=sent)", st["batches"] == st["sent"],
                  str(st))
            check("帧率 ≈ 1/RTT(5~20Hz)", 5.0 <= st["hz"] <= 20.0,
                  f"hz={st['hz']} fps_target={st['fps_target']}")
            check("fps_target ≈ hz(都是 1/RTT)",
                  abs(st["fps_target"] - st["hz"]) < 6.0,
                  f"target={st['fps_target']} hz={st['hz']}")
            a = mc.stream.anchor()
            check("锚已建立", a is not None, str(a))
            snap = mc.timesync.snapshot()
            check("锚逐条刷新(年龄 <1s)", snap.anchor_age_s is not None
                  and snap.anchor_age_s < 1.0, str(snap.anchor_age_s))
            check("telem 模式不注入锚(anchor_refreshes=0)",
                  st["anchor_refreshes"] == 0, str(st["anchor_refreshes"]))
            _check_union("U1", st)
        finally:
            mc.close()


# ---------------------------------------------------------------- U2 双向切换

def u2_switch() -> None:
    from host.api import MouseClient

    print("\n[U2] 双向切换: on()→pace()→on() 全程 is_on 不灭(单对象互斥)")
    with _Sim("--safe", "--rtt-ms", "80") as sim:
        mc = MouseClient(auto_session=False, auto_stream=False)
        mc.connect(sim.target)
        try:
            r = mc.stream.on()
            check("on() OK", r.ok, r.status)
            check("on 后 is_on 不灭", mc.stream.is_on is True)
            check("on 后 mode=stopwait", mc.stream.mode == "stopwait")
            check("on 后收到帧", mc.telemetry.wait_first_frame(timeout=6.0))

            r = mc.stream.pace()
            check("pace() OK", r.ok, r.status)
            check("pace 后 is_on 不灭", mc.stream.is_on is True)
            check("pace 后 mode=pace", mc.stream.mode == "pace")
            check("pace 后 n=3/t=100(批式档记忆)",
                  (mc.stream.stats()["n"], mc.stream.stats()["t_ms"]) == (3, 100),
                  str(mc.stream.stats()))
            check("pace 后重新收到帧", mc.telemetry.wait_first_frame(timeout=6.0))

            r = mc.stream.on()
            check("再 on() OK", r.ok, r.status)
            check("再 on 后 is_on 不灭", mc.stream.is_on is True)
            check("再 on 后 mode=stopwait", mc.stream.mode == "stopwait")
            check("再 on 后 n=1", mc.stream.stats()["n"] == 1,
                  str(mc.stream.stats()))
            check("再 on 后收到帧", mc.telemetry.wait_first_frame(timeout=6.0))
            time.sleep(1.0)
            st = mc.stream.stats()
            check("切回停等后在途 ≤1", st["inflight"] <= 1, str(st))
            check("停等式持续收帧", st["hz"] > 1.0, str(st["hz"]))
            # 批式档记忆没被 on() 冲掉: 省略参数的 pace() 还是 3/100
            r = mc.stream.pace()
            check("省略参数 pace() 仍是 3/100 档",
                  (mc.stream.stats()["n"], mc.stream.stats()["t_ms"]) == (3, 100),
                  str(mc.stream.stats()))
            check("pace 后 mode=pace", mc.stream.mode == "pace")
        finally:
            mc.close()


# ---------------------------------------------------------------- U3 锚刷新

def u3_anchor() -> None:
    from host.api import MouseClient
    from host.api.stream import TelemetryPuller

    print("\n[U3] 锚刷新: 批中点注入 TELEM(ANCHOR_REFRESH_S 调小到 0.5s 验证)")
    old = TelemetryPuller.ANCHOR_REFRESH_S
    TelemetryPuller.ANCHOR_REFRESH_S = 0.5
    try:
        with _Sim("--safe", "--rtt-ms", "80") as sim:
            mc = MouseClient(auto_session=False, auto_stream=False)
            mc.connect(sim.target)
            try:
                mc.stream.pace()
                check("pace 在跑", mc.stream.mode == "pace")
                check("等到首帧", mc.telemetry.wait_first_frame(timeout=6.0))
                time.sleep(3.0)
                st = mc.stream.stats()
                snap = mc.timesync.snapshot()
                print(f"     3s: anchor_refreshes={st['anchor_refreshes']} "
                      f"anchor_age={snap.anchor_age_s} locked={snap.locked}")
                check("注入发生(>3 次)", st["anchor_refreshes"] > 3,
                      str(st["anchor_refreshes"]))
                check("锚年龄有界(<1.5s)", snap.anchor_age_s is not None
                      and snap.anchor_age_s < 1.5, str(snap.anchor_age_s))
                check("同余还原未失锁", snap.locked is True, str(snap.lost))
                check("批式仍在收帧", st["frames"] > 20, str(st["frames"]))
            finally:
                mc.close()
    finally:
        TelemetryPuller.ANCHOR_REFRESH_S = old


# ---------------------------------------------------------------- U4 自节流

def u4_throttle() -> None:
    from host.api import MouseClient

    print("\n[U4] 自节流: RTT(500ms)>T(300ms) 时命令间距 ≥T、整批完整")
    with _Sim("--safe", "--rtt-ms", "500") as sim:
        mc = MouseClient(auto_session=False, auto_stream=False)
        mc.connect(sim.target)
        try:
            mc.stream.pace(2, 150)          # T=300ms, 目标 6.67Hz
            check("pace(2,150) OK", mc.stream.mode == "pace")
            check("等到首帧", mc.telemetry.wait_first_frame(timeout=6.0))
            t0 = time.time()
            time.sleep(4.0)
            el = time.time() - t0
            st = mc.stream.stats()
            fps = st["frames"] / el
            cmd_hz = st["batches"] / el
            print(f"     4s: batches={st['batches']} frames={st['frames']} "
                  f"lost={st['lost_batches']} rtt={st['rtt_ms']:.0f}ms "
                  f"cmd_hz={cmd_hz:.2f} fps={fps:.2f}")
            check("命令间距 ≥T(cmd_hz ≤ 1/T=3.33)", cmd_hz <= 3.4,
                  f"cmd_hz={cmd_hz:.2f}")
            check("命令间距 ≈ 1/RTT(1.5~2.5Hz)", 1.5 <= cmd_hz <= 2.5,
                  f"cmd_hz={cmd_hz:.2f}")
            check("零丢批(整批完整)", st["lost_batches"] == 0,
                  str(st["lost_batches"]))
            check("每批 2 帧(帧数≈2×批数)",
                  abs(st["frames"] - 2 * st["batches"]) <= 2,
                  f"frames={st['frames']} batches={st['batches']}")
            check("帧率 ≈ n/RTT=4Hz(2.5~5.5)", 2.5 <= fps <= 5.5,
                  f"fps={fps:.2f}")
        finally:
            mc.close()


# ---------------------------------------------------------------- U5 telem 断连

def u5_telem_lost() -> None:
    from host.api import MouseClient
    from host.api.events import Event

    print("\n[U5] telem 断连: 停等式连续无帧 ⇒ 7s 级 link_lost + dead")
    sim = _Sim("--safe", "--rtt-ms", "80")
    with sim:
        mc = MouseClient(auto_session=False, auto_stream=True)
        mc.connect(sim.target)
        try:
            check("拉取器在跑(停等)", mc.stream.mode == "stopwait",
                  mc.stream.mode)
            check("等到首帧", mc.telemetry.wait_first_frame(timeout=6.0))
            time.sleep(1.0)
            lost_evts: List[dict] = []

            def _on_err(msg) -> None:
                d = getattr(msg, "data", None) or {}
                if d.get("kind") == "link_lost":
                    lost_evts.append(d)

            mc.on(Event.ERROR, _on_err)
            sim.kill()                      # 模拟模块停转发
            deadline = time.time() + 11.0
            while time.time() < deadline and not lost_evts:
                time.sleep(0.1)
            check("收到 link_lost 事件(7s 级)", len(lost_evts) == 1,
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
            # 判过后可恢复: start() 清 dead
            r = mc.stream.on()
            check("on() 后 dead 清除", mc.stream.stats().get("dead") is False,
                  str(mc.stream.stats()))
            check("on() 返回 OK", r.ok, r.status)
        finally:
            mc.close()


# ---------------------------------------------------------------- U6 tune 切档

def u6_tune() -> None:
    from host.api import MouseClient

    print("\n[U6] tune 切档: telem 在跑时 tune(n=2) 立即变批式(跨模式重 arm)")
    with _Sim("--safe", "--rtt-ms", "80") as sim:
        mc = MouseClient(auto_session=False, auto_stream=False)
        mc.connect(sim.target)
        try:
            mc.stream.on()
            check("初始 mode=stopwait", mc.stream.mode == "stopwait")
            check("等到首帧", mc.telemetry.wait_first_frame(timeout=6.0))
            cfg = mc.stream.tune(n=2, t_ms=150)
            check("tune 返回批式档 2/150",
                  cfg["pace"]["n"] == 2 and cfg["pace"]["t_ms"] == 150, str(cfg))
            check("tune 后立即变批式", mc.stream.mode == "pace",
                  mc.stream.mode)
            check("tune 后 is_on 不灭", mc.stream.is_on is True)
            check("批式收到帧", mc.telemetry.wait_first_frame(timeout=6.0))
            time.sleep(2.0)
            st = mc.stream.stats()
            check("批式在收帧(n=2)", st["n"] == 2 and st["frames"] > 10,
                  str(st))
            # 同 kind 内改档不重启(下一条命令带新参数)
            cfg = mc.stream.tune(n=3, t_ms=100)
            check("tune(3,100) 不打断运行", mc.stream.mode == "pace"
                  and mc.stream.is_on is True, mc.stream.mode)
            check("tune 后 T=300ms/10Hz", cfg["pace"]["T_ms"] == 300
                  and abs(cfg["pace"]["fps"] - 10.0) < 0.01, str(cfg))
            base = mc.stream.stats()        # 切档前基线(此前是 n=2 段, 不能混算)
            time.sleep(2.0)
            st = mc.stream.stats()
            check("切回 10Hz 档后每批 3 帧(增量口径)",
                  abs((st["frames"] - base["frames"])
                      - 3 * (st["batches"] - base["batches"])) <= 3,
                  f"Δframes={st['frames']-base['frames']} "
                  f"Δbatches={st['batches']-base['batches']}")
            # 批式 → 停等: tune(n=1) 也应立即生效
            cfg = mc.stream.tune(n=1)
            check("tune(n=1) 退回停等", mc.stream.mode == "stopwait",
                  mc.stream.mode)
            check("退回停等后 n=1", mc.stream.stats()["n"] == 1,
                  str(mc.stream.stats()))
            check("停等式恢复收帧", mc.telemetry.wait_first_frame(timeout=6.0))
            # 不传 n/t 的 tune 不该把停等式悄悄切走
            cfg = mc.stream.tune(request_timeout=5.0)
            check("tune(request_timeout) 不切模式", mc.stream.mode == "stopwait",
                  mc.stream.mode)
            check("request_timeout 生效", cfg["stopwait_request_timeout"] == 5.0,
                  str(cfg))
            _check_union("U6", mc.stream.stats())
        finally:
            mc.close()


# ---------------------------------------------------------------- U7 统计并集

def u7_union() -> None:
    from host.api import MouseClient

    print("\n[U7] 统计并集: 两种剖面 stats() 字段齐全且类型正确")
    with _Sim("--safe", "--rtt-ms", "80") as sim:
        mc = MouseClient(auto_session=False, auto_stream=False)
        mc.connect(sim.target)
        try:
            mc.stream.on()
            mc.telemetry.wait_first_frame(timeout=6.0)
            time.sleep(1.5)
            _check_union("stopwait", mc.stream.stats())
            mc.stream.pace()
            mc.telemetry.wait_first_frame(timeout=6.0)
            time.sleep(1.5)
            _check_union("pace", mc.stream.stats())
        finally:
            mc.close()


def main() -> int:
    from host.api import MouseClient  # noqa: F401  (提前暴露 import 错误)

    print("=" * 66)
    print("统一拉取器冒烟(v2.10 TelemetryPuller, 模拟器)")
    print("=" * 66)
    u1_degenerate()
    u2_switch()
    u3_anchor()
    u4_throttle()
    u5_telem_lost()
    u6_tune()
    u7_union()
    print("\n" + "=" * 66)
    print(f"通过 {PASS} / 失败 {FAIL}")
    if FAILED:
        print("失败项:", FAILED)
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
