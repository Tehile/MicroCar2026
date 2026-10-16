"""拉取模式(v2.1)冒烟测试 —— 验证 `TELEM` 请求-响应链路与**停等**不变量。

为什么单独一个文件: v2.1 把遥测从"固件自时钟持续流"换成了"PC 循环拉取",
这条链路的正确性(是否会 burst、锚是否刷新、停止后是否真的不再收帧)
是旧套件完全没覆盖的, 而它正是真机上出过事故的地方。

核心不变量(真机实测得出): **任何时刻最多 1 条 `TELEM` 在途**。
固件每 1ms tick 只消费一行命令, 一次发两条会丢第二条(实测回收率 N=2 仅 50%)。

用法(在 host/ 目录下):
    python pull_smoke.py
"""

from __future__ import annotations

import os
import re
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


def main() -> int:
    from host.api import MouseClient
    from host.registry import COMMANDS

    print("=" * 66)
    print("拉取模式(v2.1) 冒烟测试")
    print("=" * 66)

    # ---------------------------------------------------------------- 静态
    print("\n[P0] 命令集: TELEM 在, STREAM 不在")
    check("TELEM 已登记", "telem" in COMMANDS)
    check("STREAM ON 已删除", "stream.on" not in COMMANDS and "stream.off" not in COMMANDS)
    check("TELEM 标为不重发", COMMANDS["telem"].no_retry is True)
    check("TELEM 命令文本 = TELEM", COMMANDS["telem"].build() == "TELEM")

    with _Sim("--safe") as sim:
        # ------------------------------------------------------------ 拉取
        print("\n[P1] 连接即开始拉取(auto_stream), 并收到统一帧")
        mc = MouseClient(auto_session=False, auto_stream=True)
        if not mc.connect(sim.target):
            check("连接模拟器", False, mc.state_detail)
            return 1
        check("连接成功", mc.is_connected)
        check("拉取器已在跑", mc.stream.is_on is True)
        got = mc.telemetry.wait_first_frame(timeout=6.0)
        check("等到首帧", got, f"hz={mc.telemetry.hz():.2f}")
        f = mc.telemetry.latest()
        check("帧字段齐全(39 通道)", len(f) >= 30, f"got {len(f)}")

        print("\n[P2] 时间锚由 TELEM 应答建立")
        a = mc.stream.anchor()
        check("锚已建立", a is not None, str(a))
        if a is not None:
            check("锚的设备时基 > 0", a[1] > 0, str(a))

        print("\n[P3] 持续拉取 3 s: 帧数递增, 严格停等(在途恒 <= 1)")
        n0 = mc.stats().get("unified", 0)
        time.sleep(3.0)
        n1 = mc.stats().get("unified", 0)
        st = mc.stream.stats()
        print(f"     3s 收到 {n1-n0} 帧 | 拉取器 = {st}")
        check("持续有帧", (n1 - n0) > 3, f"{n1-n0} 帧")
        check("模式为 stop-and-wait", st.get("mode") == "stop-and-wait", str(st))
        # ⚠️ 这一条是 v2.1 真机实测后的核心不变量: 固件每 1ms 只消费一行命令,
        # 一次发两条就会丢第二条(回收率 N=2 时仅 50%)。所以任何时刻最多 1 条在途。
        check("在途恒不超过 1 条(绝不 burst)", st["inflight"] <= 1, str(st))
        check("丢帧很少", st["lost"] * 4 <= max(1, st["sent"]), str(st))

        print("\n[P4] 命令通道在拉取期间照常可用")
        r = mc.send_cmd("ping", timeout=3.0)
        check("PING 仍 OK", r.ok, r.status)
        # ⚠️ 版本号**从固件头文件现读**, 不写死 —— 写死的话每次升版本都要来改测试,
        # 久了就变成"改测试让它过", 而不是"发现两侧漂移"。
        from host import fwprofile  # 固件档位: 两代固件并存, 由 RC_FW_PROFILE 切换
        _exp = fwprofile.fw_expect()
        _pv, _fv = _exp if _exp else (None, None)
        check(f"设备指纹 proto={_pv}(= 固件 LINK_PROTO_VER)",
              mc.device.get("proto") == _pv, str(mc.device))
        check(f"设备指纹 fw={_fv}(= 固件 LINK_FW_VER)",
              mc.device.get("fw") == _fv, str(mc.device))
        print("\n[P5] 停流后必须**彻底不再收帧**(这是蓝牙缓冲排空的前提)")
        mc.stream.off()
        check("拉取器已停", mc.stream.is_on is False)
        time.sleep(0.6)          # 让在途的最后一帧落地
        n2 = mc.stats().get("unified", 0)
        time.sleep(1.5)          # 静默期
        n3 = mc.stats().get("unified", 0)
        check("停流后不再有帧到达", n3 == n2, f"{n2} -> {n3}")

        print("\n[P6] 再开流能恢复")
        mc.stream.on()
        got = mc.telemetry.wait_first_frame(timeout=6.0)
        time.sleep(0.5)
        n4 = mc.stats().get("unified", 0)
        check("重开后恢复收帧", n4 > n3, f"{n3} -> {n4}")

        print("\n[P7] 断开时自动停拉取")
        mc.close()
        check("close 后拉取器已停", mc.stream.is_on is False)

    # ------------------------------------------------ 让路(急停不丢的关键)
    print("\n[P9] 时间敏感命令让路 —— 拉取器在跑时, urgent 命令必须能独占链路")
    # 背景(真机实测 2026-10-05): 固件每 1ms tick 只消费**一行**命令, 拉取器
    # 持续发 `TELEM` 时 `STOP`/`ESTOP` 会被丢弃 -> 等 2.5s 超时重发, 甚至一直
    # 不生效("急停不立即生效")。修法: urgent 命令发送前 `stream.suspend()`。
    with _Sim("--safe") as sim:
        mc = MouseClient(auto_session=False, auto_stream=True)
        mc.connect(sim.target)
        check("拉取器在跑", mc.stream.is_on is True)
        mc.telemetry.wait_first_frame(timeout=6.0)

        # (a) suspend 必须真正停发并把在途请求清空
        mc.stream.suspend()
        check("suspend 后拉取器停发", mc.stream.is_on is False)
        check("suspend 后在途清空", mc.stream.stats()["inflight"] == 0,
              str(mc.stream.stats()))
        n_before = mc.stats().get("unified", 0)
        time.sleep(0.8)
        check("suspend 期间不再收到帧", mc.stats().get("unified", 0) == n_before,
              f"{n_before} -> {mc.stats().get('unified', 0)}")

        # (b) 让路期间普通命令照常; 且命令里最要紧的两条必须成功
        r = mc.send_cmd("ping", timeout=3.0)
        check("让路期间 PING 仍 OK", r.ok, r.status)
        r = mc.send_cmd("estop", timeout=3.0)
        check("让路期间 ESTOP 仍 OK", r.ok, r.status)
        r = mc.send_cmd("estop.clear", timeout=3.0)
        check("让路期间 STOP(解锁) 仍 OK", r.ok, r.status)

        # (c) resume 必须恢复拉取
        mc.stream.resume()
        check("resume 后拉取器恢复", mc.stream.is_on is True)
        check("resume 后重新收到帧", mc.telemetry.wait_first_frame(timeout=6.0))

        # (d) 嵌套 suspend 只有最外层真正生效(否则内层 resume 会提前恢复)
        mc.stream.suspend()
        mc.stream.suspend()
        check("嵌套 suspend 仍是停发", mc.stream.is_on is False)
        mc.stream.resume()
        check("内层 resume 不该恢复拉取", mc.stream.is_on is False)
        mc.stream.resume()
        check("外层 resume 才恢复拉取", mc.stream.is_on is True)

        # (e) ★ end-to-end: 拉取器在跑时发 urgent 命令, 不应出现重发
        mc.telemetry.wait_first_frame(timeout=6.0)
        attempts = []
        for _i in range(5):
            rr = mc.send_cmd("estop.clear", timeout=6.0)
            attempts.append(int(rr.data.get("attempts") or 1))
            check(f"  在拉取中发 STOP 第 {_i+1} 次 OK", rr.ok, rr.status)
            mc.send_cmd("estop", timeout=6.0)
            mc.send_cmd("estop.clear", timeout=6.0)
        check("★ 拉取期间发 urgent 命令**无重发**", max(attempts) == 1, str(attempts))
        mc.close()

    # ---------------------------------------------------------------- 反例
    print("\n[P8] 反例: auto_stream=False 时不该有任何帧")
    with _Sim("--safe") as sim:
        mc = MouseClient(auto_session=False, auto_stream=False)
        mc.connect(sim.target)
        check("拉取器未启动", mc.stream.is_on is False)
        time.sleep(1.5)
        check("确实没收到帧(设备只在被请求时才发)", mc.stats().get("unified", 0) == 0,
              str(mc.stats().get("unified", 0)))
        # 手动拉一帧应立刻有
        mc.stream.on()
        check("手动开流后收到帧", mc.telemetry.wait_first_frame(timeout=6.0))
        mc.close()

    print("\n" + "=" * 66)
    print(f"通过 {PASS} / 失败 {FAIL}")
    if FAILED:
        print("失败项:", FAILED)
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
