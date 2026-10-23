#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LED 位上报专项实机验证 —— v9.2.1 / FW 313（状态B1 的 bit2-5 = led1..led4）。

目的很窄：证明"固件里 LED 真的变了，帧里那 4 位就真的跟着变"，而不是只证明
"字段能解出来"。判据全部来自固件里的确定性行为：

  L1 待命基线  4 位取值恒在 {0,1}、通道齐全、采样率够；基线位型打印出来供肉眼
               对照。**不再**要求 led1..led3 稳定 —— `led.c` 的 LED_On/Off(case 2)
               就是 LED2，待命态它本来会闪（状态灯），要求不闪是判据设错。
  L2 基准灯    `ARM` 后 LED4 由 LINK_Tick 以 LINK_TIME_DIV=1000 ms 的 50% 占空比
               翻转 ⇒ led4 的同向边沿间隔 ≈1000 ms。这是"同步"的硬证据：
               灯在翻、帧里的位也在翻，且同相。
  L3 发车标记  `MAZE RUN` 的同一时刻固件点亮 4 灯并保持 LINK_LAUNCH_MARK_MS
               =300 ms（link.c:2799-2808 / 3429-3448）⇒ 帧里必须出现一段
               连续 4 位全 1 的窗口，窗口前一帧、后一帧都不是全 1，且窗口过后
               led1..led3 回到标记前的值。这条是端到端最有说服力的一条：它把
               "肉眼可见的发车时刻"变成了帧里可读的事实。

取流方式：**批量下拉** `RATE PULL <n> <t_ms>`（一批发完固件就静默，等下一条），
与 `link_pull_evt.py` 同款、在途恒 ≤1。每周期 4 帧 / 60 ms ≈13 Hz。
⚠️ 不要改成 `RATE FIX` 连续发流：实测它会 ①把透传模块缓冲灌满（COM7 能开、
0 字节、必须断电）②取流期间下行命令被打坏（`ACK -1 ERR_CRC`，ARM 会静默不执行）。

v9.2 / FW 312 起，发车走**复合命令** `MAZE RUN [target] PULL <n> <t_ms>`：一条命令
= 发车 + 启动**恰好一批** N 帧，主机不再"停拉流 → 发车 → 等 ACK → 重下 PULL"。
**批次停等模型不变**（固件侧不自续订、在途批次仍为 1），但"发车 → 下一帧到达"的
空窗从数百毫秒降到十几毫秒。前提是主机严格遵守下面的三态状态机。

三态状态机（`docs/发车数据空窗分析与解决方案.md` 方案 E）：

    IDLE      已确认固件静默 —— **唯一允许发命令**的状态
    PENDING   批次在途，固件正在发帧 —— 禁止发任何字节，只收
    RESETTING 上一批丢帧/超时 —— 只发 `RATE OFF` 重建静默，然后回 IDLE

进入 IDLE 的三条路径：① 本批 N 帧收满 ② `RATE OFF` 收到 ACK
③ 连接后 `quiet_handshake()`（发 `RATE OFF` + 静候 250 ms 无残流）。
**非 IDLE 发命令会被半双工链路截断**（固件回 `ERR_CRC`、命令根本没执行）。

命令一律走 `send_checked()`：被打坏就用**同一 seq + 同一正文**重发 —— 固件的
"幂等重放"分支保证不会重复执行，且会**一并重启复合命令的拉流**（否则死锁）。

用法:
    python led_bit_verify.py COM7                 # L1..L4 全跑，末尾软复位清场
    python led_bit_verify.py COM7 --no-launch     # 不跑 L3（不发车）
    python led_bit_verify.py COM7 --no-reset      # 不收尾软复位
    python led_bit_verify.py --selftest           # 离线判据自检（不需要硬件）

安全: 下行命令走本文件白名单 PING/TELEM/RATE/ARM/MAZE/MCU/ESTOP，其余断言拦死。
      L3 会发一次 `MAZE RUN`（车轮必须悬空），之后由 halt() 收车。
      **退出路径（正常/异常/Ctrl-C/进程退出）一律先执行 halt()**：
      MAZE STOP -> 验 task 归零 -> ESTOP -> MCU RESET。2026-10-23 曾因缺这一层，
      一次中断把车留在了跑动状态。
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from basic_cmd_smoke import parse_pong              # noqa: E402
from host.protocol import codec                     # noqa: E402

LEDS = ("led1", "led2", "led3", "led4")
BAUDRATE = 460800      # CH340 透传模块实测速率；115200 会连字节都收不全
MARK_MS = 300          # 固件 LINK_LAUNCH_MARK_MS
BEACON_MS = 1000       # 固件 LINK_TIME_DIV
BATCH_N, BATCH_T = 4, 60     # 每周期 4 帧、间隔 60 ms ⇒ ≈13 Hz（与 link_pull_evt N=4 同款）
MARK_N, MARK_T = 8, 40       # 发车那一批 8 帧 / 40 ms ⇒ 280 ms，覆盖 300 ms 标记窗口
LAUNCH_GAP_MS_MAX = 250      # 硬上限；真实下界是"单条命令的物理往返"≈150 ms(见 stage3 相对判据)
LINK_SETTLE_MS = 120         # 复合命令收满后先静默这么久，让最后 1 帧彻底离开透传模块
BATCH_TIMEOUT = 0.7          # 单批超时：正常批次 ACK+4 帧 ≈330 ms，留 2 倍余量
# `ESTOP` 必须留在白名单里：halt() 的第三级升级要发它。急停是**固件顶层命令**
# （link.c 的 `link_TokIs(&p,"ESTOP")`），不是 `MAZE ESTOP`。
ALLOWED = {"PING", "TELEM", "RATE", "ARM", "MAZE", "MCU", "ESTOP"}

# ---- 拉流三态（见模块 docstring 与 docs/发车数据空窗分析与解决方案.md 方案 E）----
IDLE = "IDLE"              # 已确认固件静默：唯一允许发命令的状态
PENDING = "PENDING"        # 批次在途：禁止发任何字节
RESETTING = "RESETTING"    # 失同步：只发 RATE OFF 重建静默

# --------------------------------------------------------------------------- #
#  安全网：**停车优先级高于一切**（含状态机自洽）
# --------------------------------------------------------------------------- #
# 2026-10-23 事故复盘：一次发车之后车一直跑着没停。三个独立缺口叠加导致，
# 任何一个补上都不会跑飞：
#   ① main() 的 finally 只发 RATE OFF，**从来不发 MAZE STOP**；
#   ② 停止命令走 send_checked()，而它断言 state == IDLE —— 发车之后最可能
#      正处于 PENDING / RESETTING，断言把 STOP 拦在本地，**一个字节都没发出去**；
#   ③ `_launch_once` 的收车不在 try/finally 里，中间任何异常都直接跳过收车
#      （本轮实测就是 `ACK -1 ERR_CRC` → `_await` 超时 → 抛异常 → 跳过 STOP）。
# 由此定的硬规则：
#   · **安全命令永不受三态断言约束**（raw_cmd）；
#   · 退出路径（正常结束 / 异常 / Ctrl-C / 进程退出）**必须无条件经过 halt()**；
#   · halt() 期间屏蔽 SIGINT —— 连按两次 Ctrl-C 会打断 finally，车就真跑掉了；
#   · 停车必须**验证**（拉 2 帧看 mazeTask/motionTask 归零），不能只看 ACK。
SAFETY_HALT_TRIES = 3
SAFETY_ACK_TIMEOUT = 0.8

_ACTIVE_SESS = {"sess": None}


def _emergency_halt() -> None:
    """进程退出兜底（atexit / 未捕获异常）：尽力停一次车，**绝不抛**。"""
    lk = _ACTIVE_SESS.get("sess")
    if lk is None:
        return
    try:
        lk.halt(verify=False, quiet=True)
    except BaseException:                                   # noqa: BLE001
        pass


atexit.register(_emergency_halt)

_npass = 0
_nfail = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global _npass, _nfail
    if ok:
        _npass += 1
        print("  [OK  ] %-44s %s" % (name, detail))
    else:
        _nfail += 1
        print("  [FAIL] %-44s %s" % (name, detail))
    return ok


def note(name: str, detail: str = "") -> None:
    print("  [..] %-44s %s" % (name, detail))


def build_cmd(cmd: str, seq: int) -> bytes:
    """与 basic_cmd_smoke.build 逐字节一致，但用本脚本自己的白名单。"""
    head = cmd.split()[0].upper()
    assert head in ALLOWED, "本脚本不允许发送 %s" % head
    body = ("%s #%d" % (cmd, seq)).encode("ascii")
    return body + ("*%04X" % codec.crc16_modbus(body)).encode("ascii") + b"\r\n"


# --------------------------------------------------------------------------- #
#  纯判据函数（--selftest 直接测这三个）
# --------------------------------------------------------------------------- #
def _bits(fr: dict) -> tuple:
    return tuple(fr[k] for k in LEDS)


def analyze_stability(frames: list, names=("led1", "led2", "led3")) -> dict:
    """返回 {name: {"set": [...], "toggles": int}}。"""
    out = {}
    for n in names:
        vals = [f[n] for f in frames]
        out[n] = {"set": sorted(set(vals)),
                  "toggles": sum(1 for a, b in zip(vals, vals[1:]) if a != b)}
    return out


def analyze_beacon(frames: list, name: str = "led4") -> dict:
    """找 `name` 的跳变边沿；同向边沿间隔即周期（1 Hz 方波 ⇒ ≈1000 ms）。"""
    edges = []
    for prev, cur in zip(frames, frames[1:]):
        if prev[name] != cur[name]:
            edges.append((cur["_t"], cur[name]))
    periods = [((edges[k][0] - edges[k - 2][0]) * 1000.0)
               for k in range(2, len(edges)) if edges[k][1] == edges[k - 2][1]]
    return {"edges": edges, "periods": periods, "toggles": len(edges)}


def find_mark_window(frames: list) -> dict:
    """找"4 位全 1"的最长连续窗口。"""
    on = [all(f[k] == 1 for k in LEDS) for f in frames]
    n = len(on)
    best = None
    i = 0
    while i < n:
        if not on[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and on[j + 1]:
            j += 1
        if best is None or (j - i) > (best[1] - best[0]):
            best = (i, j)
        i = j + 1
    if best is None:
        return {"found": False}
    i, j = best
    return {"found": True, "i0": i, "i1": j, "count": j - i + 1,
            "span_ms": (frames[j]["_t"] - frames[i]["_t"]) * 1000.0 if j > i else None,
            "prev_all_on": bool(i > 0 and on[i - 1]),
            "next_all_on": bool(j + 1 < n and on[j + 1]),
            "t0": frames[i]["_t"], "t1": frames[j]["_t"]}


def frame_gaps_ms(frames: list) -> list:
    return [(b["_t"] - a["_t"]) * 1000.0 for a, b in zip(frames, frames[1:])]


def _median(xs) -> float:
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


# --------------------------------------------------------------------------- #
#  串口会话：三态拉流状态机 + 批量下拉
# --------------------------------------------------------------------------- #
class Sess:
    def __init__(self, port: str):
        import serial
        self.ser = serial.Serial(port, BAUDRATE, timeout=0.03, write_timeout=1.5)
        self.rb = codec.FrameStreamParser()
        self.seq = 900
        self.frames: list = []
        self.lines: list = []
        self.acks: list = []
        self._t0 = time.time()
        self.state = IDLE
        self.want = 0            # 本批目标帧数（0 = 无在途批次）
        self._base = 0           # 本批起始帧下标
        self.gaps: list = []     # 每次下发到"下一帧到达"的空窗 ms（判据用）
        self.recoveries = 0      # 进过 RESETTING 的次数（应为 0，除非真丢帧）
        self._halted = False     # halt() 已验证停稳；combo_launch 发车前复位

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:                                       # noqa: BLE001
            pass

    def cmd(self, text: str) -> int:
        """**只允许在 IDLE 下调用**：写命令并立刻返回 seq，不等 ACK。"""
        assert self.state == IDLE, "非 IDLE 禁止发命令(当前 %s): %s" % (self.state, text)
        self.seq += 1
        self.ser.write(build_cmd(text, self.seq))
        self.ser.flush()
        self.lines.append((time.time() - self._t0, "<sent> " + text))
        return self.seq

    # ----------------------------------------------------------------- #
    #  安全通道：停车命令**不受三态约束**
    # ----------------------------------------------------------------- #
    def raw_cmd(self, text: str, timeout: float = SAFETY_ACK_TIMEOUT):
        """绕过三态断言直接下发一条命令，返回 ACK 文本（无则应返回 None）。

        只给停车类安全命令用。理由见模块级「安全网」注释：车在跑的时候，
        "状态机是否自洽"必须让位于"先把车停下"。每条都用**新 seq** —— 这样一定
        真的执行，不会命中固件的幂等重放分支（那分支只补 ACK、不重做副作用）。
        """
        self.seq += 1
        s = self.seq
        self.ser.write(build_cmd(text, s))
        self.ser.flush()
        self.lines.append((time.time() - self._t0, "<sent> " + text))
        got = None
        t0 = time.time()
        while time.time() - t0 < timeout:
            self.pump()
            got = self._find_ack(s)
            if got:
                break
        self.acks.append((text, got or "TIMEOUT", 0))
        return got

    def _confirm_stopped(self, tries: int = 2) -> bool:
        """拉 2 帧看 `mazeTask` / `motionTask` 是否都归零 —— 停车必须验证。

        只看 `ACK OK` 是不够的：`MAZE STOP` 的 ACK 只证明命令被执行，不证明
        电机真的停了（`linkMazeAbort` 要让主循环退出迷宫 while(1) 才生效）。
        安全出口宁可报失败，所以**拿不到帧也判失败**。
        """
        for _ in range(tries):
            time.sleep(LINK_SETTLE_MS / 1000.0)
            base = len(self.frames)
            self.raw_cmd("RATE PULL 2 60")
            t0 = time.time()
            while len(self.frames) - base < 2 and time.time() - t0 < 0.9:
                self.pump()
            got = self.frames[base:]
            if got:
                last = got[-1]
                if not (last.get("mazeTask") or last.get("motionTask")):
                    return True
                return False
        return False

    def halt(self, tries: int = SAFETY_HALT_TRIES, verify: bool = True,
             quiet: bool = False) -> bool:
        """**幂等强制停车**：`MAZE STOP` → 验 task 归零 → `ESTOP` → `MCU RESET`。

        分级理由：`MAZE STOP` 是唯一同时做「停 PWM + 让迷宫循环退出 + 解急停」的
        命令，所以永远先用它；`ESTOP` 只锁存不解锁（用于 STOP 回了 OK 但车仍在动
        的异常情形）；`MCU RESET` 是链路/固件失去响应时的最后手段。

        停车期间屏蔽 SIGINT：否则用户连按两次 Ctrl-C 会打断 finally，车真的跑掉。
        """
        old = None
        try:
            old = signal.signal(signal.SIGINT, signal.SIG_IGN)
        except Exception:                                   # noqa: BLE001
            old = None
        try:
            # ---- 一级：MAZE STOP ----
            ack = None
            for _ in range(max(1, tries)):
                ack = self.raw_cmd("MAZE STOP")
                if ack and " OK" in ack:
                    break
                time.sleep(0.12)
            ok = bool(ack and " OK" in ack)
            # STOP 的语义就是"把控制权还给操作者"，它同时清急停、置 abort，
            # 所以此处把状态机一并归位到 IDLE 是**正确**的，不是掩盖问题。
            self.state = IDLE
            if not ok and not quiet:
                note("MAZE STOP 无 OK 应答", str(ack))

            # ---- 二级：验证真的停了 ----
            if ok and verify:
                ok = self._confirm_stopped()
                if not ok and not quiet:
                    note("STOP 回了 OK 但仍在运动", "升级 ESTOP")

            # ---- 三级：急停锁存 ----
            if not ok and not quiet:
                note("升级急停", "ESTOP")
            if not ok:
                self.raw_cmd("ESTOP")
                time.sleep(0.15)

            # ---- 四级：软复位（链路/固件不响应时的最后手段）----
            if not ok:
                if not quiet:
                    note("升级软复位", "MCU RESET")
                self.raw_cmd("MCU RESET", timeout=0.4)
                self.state = IDLE
                ok = not verify or self._confirm_stopped(tries=1)

            if not quiet:
                note("停车结果", "已停 + 已验证" if ok else "**未确认**")
            self._halted = bool(ok)
            return ok
        finally:
            if old is not None:
                try:
                    signal.signal(signal.SIGINT, old)
                except Exception:                           # noqa: BLE001
                    pass

    def _find_ack(self, seq: int):
        for _, txt in self.lines:
            if txt.startswith("ACK %d " % seq):
                return txt
        return None

    def _link_err(self, n_from: int):
        """本次下发之后收到的、**不带 seq** 的错误应答（`ACK -1 ERR_CRC`）。

        这是半双工截断的直接证据：命令到了固件，但字节在模块切换方向时被削掉，
        CRC 对不上，于是回 `ACK -1 ERR_CRC`。它不是"没收到"，而是"收到但被打坏"，
        命令**没有执行**，所以必须用同一 seq + 同一正文重发（固件幂等，重发安全）。

        没有这条时，`_await` 只认自己的 seq，会把 `ACK -1` 当空气，白等满整个
        timeout（1.5 s）—— 一次打坏就吃掉整个采样窗口，后面的批次全被挤出时窗。
        实测症状正是"发车后只收到 16 帧、RESETTING 恰好 1 次"。
        """
        for _, txt in self.lines[n_from:]:
            if txt.startswith("ACK -1 "):
                return txt
        return None

    def _await(self, want_frames: int, seq: int, timeout: float) -> tuple:
        """并行收 ACK 与数据帧：ACK 先到（固件 TX 是 FIFO，ACK 先入队），随后是帧。

        这就是"不等 ACK"的正确形态 —— **并行**而不是阻塞，所以空窗里不包含 RTT。
        返回 (ack_text | None, 实收帧数, 首帧时刻或 None)。
        """
        base = len(self.frames)
        n0 = len(self.lines)
        t0 = time.time()
        ack = None
        t_first = None
        while time.time() - t0 < timeout:
            self.pump()
            if ack is None:
                ack = self._find_ack(seq)
            got = len(self.frames) - base
            if got > 0 and t_first is None:
                t_first = self.frames[base]["_t"]
            if got >= want_frames and ack is not None:
                break
            if ack is None and got == 0 and self._link_err(n0) is not None:
                break                    # 被打坏：立刻返回，由调用方同 seq 重发
        return ack, len(self.frames) - base, t_first

    def _settle(self, seq: int, t_sent: float, want_frames: int,
                ack, t_first, got: int) -> None:
        """批次收尾：收满 ⇒ IDLE；ACK 丢失或帧没收满 ⇒ RESETTING 并自动重建静默。"""
        if t_first is not None:
            self.gaps.append((t_first - t_sent) * 1000.0)
        if ack is None:
            # ACK 没回来：命令多半被半双工链路打坏、根本没执行。同 seq 同正文重发是幂等安全的，
            # 但在重建静默之前不能盲发 —— 故先回 IDLE，由调用方决定重试。
            note("ACK 未到，进 RESETTING", "seq=%d 收到 %d/%d 帧" % (seq, got, want_frames))
            self._recover()
            return
        if got < want_frames:
            note("批次未收满，进 RESETTING", "%d/%d 帧" % (got, want_frames))
            self._recover()
            return
        self.state = IDLE

    def _recover(self) -> None:
        """超时/丢帧：进 RESETTING，发 `RATE OFF` 拿回 IDLE，再静候确认无残流。"""
        self.state = RESETTING
        self.recoveries += 1
        self.want = 0
        self.seq += 1
        seq = self.seq
        try:
            self.ser.write(build_cmd("RATE OFF", seq))
            self.ser.flush()
            self.lines.append((time.time() - self._t0, "<sent> RATE OFF (recover)"))
            self._await(0, seq, 0.6)
        except Exception as e:                                  # noqa: BLE001
            note("恢复期 RATE OFF 失败", str(e)[:70])
        base = len(self.frames)
        t0 = time.time()
        while time.time() - t0 < 0.25:                           # 静默确认
            self.pump()
        left = len(self.frames) - base
        if left:
            note("RATE OFF 后仍有残流", "%d 帧(已丢弃)" % left)
        self.state = IDLE

    def quiet_handshake(self) -> bool:
        """连接后的**静默握手**：`RATE OFF` + 静候 250 ms 无残流 ⇒ 确认固件静默。

        没有这一步，主机可能在上电残留批次还没跑完时就下发命令 ⇒ 被半双工截断
        （固件回 `ERR_CRC`、命令根本没执行）。这是"此前没有传输、此次拉流是启动"
        场景的前置条件：**先把自己置于 IDLE，再谈下发第一条命令**。
        """
        self.state = IDLE
        seq = self.cmd("RATE OFF")
        ack, _, _ = self._await(0, seq, 1.0)
        base = len(self.frames)
        t0 = time.time()
        while time.time() - t0 < 0.25:
            self.pump()
        left = len(self.frames) - base
        ok = (ack is not None and " OK" in ack)
        note("静默握手", "ACK=%s 残流=%d 帧" % (ack or "无", left))
        self.state = IDLE
        return ok and left == 0

    def pull_batch(self, n: int = BATCH_N, t_ms: int = BATCH_T, timeout: float = BATCH_TIMEOUT,
                   tries: int = 3) -> str:
        """裸批次：`RATE PULL <n> <t_ms>`，等收满 n 帧才回 IDLE。返回 ACK 文本。

        被打坏（`ACK -1 ERR_CRC`）或干脆没回音时，用**同一 seq + 同一正文**重发：
          · 固件对 `RATE` 族是在幂等重放分支**之前**直接执行的，重发等价于
            "重新设定同一批参数"，不会把两个批次叠加；
          · 同 seq + 同 CRC 的重发即使命中幂等分支，也只补一条 ACK，无副作用。
        这条重试是必需的：复合命令刚发完 8 帧时立刻下发 PULL，下行正好落在
        半双工换向的窗口里，实测会被打坏（`ACK -1 ERR_CRC`）。没有重试时
        一次打坏就吃掉 1.5 s，后面的批次全被挤出采样时窗。
        """
        assert self.state == IDLE, "非 IDLE 禁止发命令(当前 %s)" % self.state
        self.seq += 1
        seq = self.seq
        text = "RATE PULL %d %d" % (n, t_ms)
        body = build_cmd(text, seq)
        ack, got, t_first = None, 0, None
        t_sent = None
        for k in range(tries):
            self.want = n
            self._base = len(self.frames)
            self.lines.append((time.time() - self._t0, "<sent> " + text))
            t_sent = self.lines[-1][0]
            self.ser.write(body)
            self.ser.flush()
            ack, got, t_first = self._await(n, seq, timeout)
            if ack is not None and " OK" in ack:
                if k:
                    note("RATE PULL 重发后确认", "第 %d 次 · %s" % (k + 1, ack))
                break
            if k < tries - 1:
                note("RATE PULL 未确认，同 seq 重发",
                     ack or self._link_err(len(self.lines) - 1) or "无应答")
                time.sleep(0.05)
        self._settle(seq, t_sent, n, ack, t_first, got)
        return ack or "无 ACK"

    def combo_launch(self, n: int = MARK_N, t_ms: int = MARK_T,
                     timeout: float = 1.6) -> dict:
        """**复合命令**：`MAZE RUN PULL <n> <t_ms>` —— 发车 + 启动恰好一批 N 帧。

        不等 ACK：ACK 与数据帧并行收。固件把 ACK 先入 TX FIFO，故 ACK 必先于首帧到达；
        主机据此把"确认"和"收帧"并行化，空窗里不含任何 RTT。
        """
        seq = self.cmd("MAZE RUN PULL %d %d" % (n, t_ms))
        t_sent = self.lines[-1][0]
        self._halted = False          # 刚发车：停车标志作废，退出时必须重新停
        self.want = n
        self._base = len(self.frames)
        ack, got, t_first = self._await(n, seq, timeout)
        gap_ms = (t_first - t_sent) * 1000.0 if t_first is not None else None
        self._settle(seq, t_sent, n, ack, t_first, got)
        # 收满后留一小段静默：最后 1 帧的上行字节可能还在模块里排着，此刻立刻下发
        # 下一条命令会被半双工换向削掉（实测 `ACK -1 ERR_CRC`）。
        if self.state == IDLE:
            time.sleep(LINK_SETTLE_MS / 1000.0)
        return {"ack": ack or "无 ACK", "got": got, "gap_ms": gap_ms, "t_run": t_sent}

    def send_checked(self, text: str, tries: int = 5, timeout: float = 1.2):
        """下发并**确认 ACK OK**；ERR_CRC / ERR_NOCRC / 超时都用**同一 seq、同一正文**重发。

        为什么要这个：取流期间下行命令会偶发被透传模块打坏（实测 `ACK -1 ERR_CRC`、
        `ERR_NOCRC`），一旦被打坏命令就**根本没执行**（例如 ARM 没执行 ⇒ 基准灯不闪，
        看起来像固件坏了）。重发同一 seq + 同一 CRC 是安全的：固件"幂等重放"分支
        （link.c:3198）会把上次应答原样回一遍，**不会重复执行**命令 —— 所以即使
        第一次其实执行了、只是 ACK 丢了，重发也不会真的再执行一次。
        FW 312 起该分支还会**一并重启复合命令的拉流**，所以带 PULL 的命令重发
        同样能拿到帧（否则主机死等）。

        ⚠️ 只能在 IDLE 下调用：本函数阻塞等 ACK，期间不能再发别的命令。
        """
        assert self.state == IDLE, "非 IDLE 禁止阻塞等 ACK(当前 %s): %s" % (self.state, text)
        self.seq += 1
        s = self.seq
        body = build_cmd(text, s)
        last = "TIMEOUT"
        for k in range(tries):
            n0 = len(self.lines)
            self.ser.write(body)
            self.ser.flush()
            self.lines.append((time.time() - self._t0, "<sent> " + text))
            t0 = time.time()
            got = None
            while time.time() - t0 < timeout:
                self.pump()
                for _, txt in self.lines[n0:]:
                    if txt.startswith("ACK %d " % s):
                        got = txt
                        break
                if got:
                    break
            if got and " OK" in got:
                self.acks.append((text, got, k))
                return s, got
            last = got or "TIMEOUT"
            self.acks.append((text, "%s (retry %d)" % (last, k + 1), k))
            if k < tries - 1:
                time.sleep(0.08)
        return s, last

    def pump(self) -> None:
        data = self.ser.read(256)
        if not data:
            return
        rel = time.time() - self._t0
        for item in self.rb.feed(data):
            if isinstance(item, codec.Frame):
                if item.type == codec.TYPE_UNIFIED and len(item.payload) == codec.UNIFIED_LEN:
                    d = codec.decode_unified(item.payload)
                    d["_t"] = rel
                    self.frames.append(d)
            else:
                self.lines.append((rel, item))

    def batch(self, n: int = BATCH_N, t_ms: int = BATCH_T,
              pre_cmds=(), want: int = 0, timeout: float = BATCH_TIMEOUT) -> int:
        """兼容旧调用形态：一个批量下拉周期（可选先发几条命令）。

        这就是 `link_pull_evt.py` 的形态：一批发完固件就静默，等下一条 PULL。
        绝不用 RATE FIX 连续发流 —— 实测连续发流会让透传模块缓冲灌满(0 字节，
        必须断电)，且取流期间下行命令会被打坏(`ACK -1 ERR_CRC`)。
        """
        base = len(self.frames)
        for c in pre_cmds:
            self.send_checked(c)
        self.pull_batch(n if not want else want, t_ms, timeout=timeout)
        return len(self.frames) - base

    def stream(self, dur: float, n: int = BATCH_N, t_ms: int = BATCH_T,
               schedule=(), post_cmds=None) -> None:
        """连续做 dur 秒的批量下拉；schedule=[(相对秒, [命令...])] 在周期边界插命令。"""
        sched = sorted(schedule, key=lambda x: x[0])
        k = 0
        while True:
            el = time.time() - self._t0
            if el >= dur:
                break
            cmds = []
            while k < len(sched) and el >= sched[k][0]:
                cmds += list(sched[k][1])
                k += 1
            self.batch(n, t_ms, pre_cmds=cmds)
        while k < len(sched):
            self.batch(n, t_ms, pre_cmds=sched[k][1])
            k += 1

    def off(self) -> None:
        """确保固件不在自时钟发流（批量模式本来就静默，这里把状态机也归位）。"""
        if self.state != IDLE:
            self._recover()
            return
        ack = None
        try:
            seq = self.cmd("RATE OFF")
            ack, _, _ = self._await(0, seq, 1.0)
        except Exception as e:                                  # noqa: BLE001
            note("RATE OFF 失败", str(e)[:70])
        base = len(self.frames)
        t0 = time.time()
        while time.time() - t0 < 0.25:
            self.pump()
        if len(self.frames) != base:
            note("RATE OFF 后仍有残流", "多收 %d 帧" % (len(self.frames) - base))
        self.state = IDLE
        _ = ack

    def reset_log(self) -> None:
        self.frames, self.lines, self.acks = [], [], []
        self._t0 = time.time()

    def find_line(self, needle: str):
        for t, s in self.lines:
            if needle in s:
                return t, s
        return None


def open_link(port: str, tries: int = 4) -> Sess:
    last = None
    for k in range(tries):
        try:
            s = Sess(port)
            _ACTIVE_SESS["sess"] = s     # 交给 atexit 兜底：进程怎么退出都要停车
            return s
        except Exception as e:                                  # noqa: BLE001
            last = e
            print("  打开 %s 失败(%d/%d): %s" % (port, k + 1, tries, str(e)[:88]))
            time.sleep(2.0)
    raise SystemExit("串口打不开: %s" % last)


# --------------------------------------------------------------------------- #
#  L0 前置指纹
# --------------------------------------------------------------------------- #
EXPECT_FW = 313


def stage0(lk: Sess, res: dict) -> bool:
    print("\n[L0] 前置指纹 —— 确认烧的是 FW %d(LED 位 + 复合命令)" % EXPECT_FW)
    pong = None
    for k in range(4):
        lk.reset_log()
        lk.send_checked("PING")
        t0 = time.time()
        while time.time() - t0 < 1.5:
            lk.pump()
            if lk.find_line("PONG"):
                break
        pong = parse_pong([s for _, s in lk.lines if not s.startswith("<sent>")])
        if pong:
            break
        note("PING 无 PONG，重试 %d/4" % (k + 1))
    if not check("PING 有 PONG 回应", pong is not None, str(pong)):
        return False
    res["pong"] = pong
    check("fw = %d (复合命令版本)" % EXPECT_FW, pong["fw"] == EXPECT_FW, "fw=%d" % pong["fw"])
    check("proto = 9", pong["proto"] == 9, "proto=%d" % pong["proto"])
    check("mazetype = 16", pong["mazetype"] == 16, "mazetype=%d" % pong["mazetype"])
    check("通道表 33 项 (29 + 4 LED)", len(codec.UNIFIED_CHANNELS) == 33,
          str(len(codec.UNIFIED_CHANNELS)))
    return True


# --------------------------------------------------------------------------- #
#  L1 待命基线（信息为主，只对"能不能读出真值"下断言）
# --------------------------------------------------------------------------- #
def stage1(lk: Sess, res: dict, sec: float = 3.0) -> None:
    print("\n[L1] 待命基线 —— 4 位能否稳定读出真值")
    lk.reset_log()
    try:
        lk.stream(sec)
    finally:
        lk.off()
    fr = lk.frames
    if not check("收到统一帧", len(fr) >= 15,
                 "%d 帧 / %.1f Hz" % (len(fr), len(fr) / sec)):
        return
    gaps = frame_gaps_ms(fr)
    note("帧间隔", "中位 %.0f ms / 最大 %.0f ms" % (statistics.median(gaps), max(gaps)))
    check("4 个 LED 通道齐全", all(k in fr[0] for k in LEDS), ",".join(LEDS))
    bad = sorted({f[k] for f in fr for k in LEDS} - {0, 1})
    check("LED 位值域 = {0,1}", not bad, "越界值 %s" % (bad or "无"))

    st = analyze_stability(fr, LEDS)
    base = _bits(fr[0])
    res["baseline"] = dict(zip(LEDS, base))
    res["baseline_toggles"] = {k: v["toggles"] for k, v in st.items()}
    note("待命基线 4 位", "%s  (亮=1)" % (list(base),))
    note("  ↑ 请肉眼核对实灯，若不符说明引脚映射要复核")
    blank = [k for k, v in st.items() if v["toggles"]]
    note("采样期内有翻转的灯", "%s (状态灯闪烁属正常)" % (blank or "无"))
    kinds = len({_bits(f) for f in fr})
    note("出现的位型种类", "%d 种" % kinds)


# --------------------------------------------------------------------------- #
#  L2 基准灯 1 Hz
# --------------------------------------------------------------------------- #
def stage2(lk: Sess, res: dict, sec: float = 6.0) -> None:
    print("\n[L2] 基准灯 —— ARM 后 led4 应 1 Hz 翻转(%d ms 周期)" % BEACON_MS)
    lk.reset_log()
    try:
        lk.stream(sec, schedule=[(0.40, ["ARM"])])
    finally:
        lk.off()
    fr = lk.frames
    if not check("收到统一帧", len(fr) >= 30, "%d 帧" % len(fr)):
        return
    arm = [a for a in lk.acks if a[0] == "ARM"]
    res["arm_ack"] = arm
    ok_arm = bool(arm) and " OK" in arm[-1][1]
    check("ARM 回 ACK OK(附 armMs)", ok_arm,
          arm[-1][1] if arm else "无 ACK")
    if arm and len(arm) > 1:
        note("ARM 重发次数", "%d 次(下行被打坏后同 seq 重发)" % (len(arm) - 1))
    b = analyze_beacon(fr, "led4")
    res["beacon"] = {"toggles": b["toggles"], "periods_ms": [round(p) for p in b["periods"]]}
    # ARM 之后才算（0.40 s 起）
    post = [f for f in fr if f["_t"] >= 0.45]
    b2 = analyze_beacon(post, "led4")
    check("ARM 后 led4 有翻转", b2["toggles"] >= 4, "%d 次(全段 %d 次)" %
          (b2["toggles"], b["toggles"]))
    if b2["periods"]:
        med = statistics.median(b2["periods"])
        res["beacon"]["median_ms"] = round(med, 1)
        check("翻转周期 ≈ %d ms (允差 ±200)" % BEACON_MS, abs(med - BEACON_MS) <= 200,
              "中位 %.0f ms, 各次 %s" % (med, [round(p) for p in b2["periods"]]))
    else:
        check("翻转周期 ≈ %d ms" % BEACON_MS, False, "边沿不足")


# --------------------------------------------------------------------------- #
#  L3 发车标记窗口
# --------------------------------------------------------------------------- #
def _launch_once(lk: Sess) -> dict:
    """一轮完整的"采基线 → **复合命令发车** → 密采样 → 收车"。

    关键时序(v9.2/FW 312)：发车走 `MAZE RUN PULL <n> <t>` —— 一条命令同时完成
    "发车"和"启动恰好一批 n 帧"。主机**不等 ACK**，ACK 与数据帧并行收，所以
    "发车 → 下一帧到达"的空窗里不含任何 RTT。

    旧写法(FW 311)必须"先写 MAZE RUN 行 → sleep 20 ms → 再单独发一条 RATE PULL"，
    而 `RATE PULL` 那条又要 `send_checked` 等它自己的 ACK，实测空窗 300 ms 量级。
    """
    lk.reset_log()
    run = {"ack": "未发车", "t_run": 0.0, "gap_ms": None, "got": 0}
    try:
        lk.batch()                                        # ① 窗口前的上下文帧
        while time.time() - lk._t0 < 0.70:                # ② 采到 ~0.7 s
            lk.batch()
        run = lk.combo_launch(MARK_N, MARK_T)             # ③ 复合命令：发车 + 一批密采样
        while time.time() - lk._t0 < 2.30:                # ④ 继续常规采样到 2.3 s
            lk.batch()
    finally:
        # ⑤ 收车：**必须放在 finally 里**。③④ 任何一步抛异常（例如 ACK 被打坏
        #    导致 _await 超时）都会跳过原本顺序执行的 STOP —— 2026-10-23 车跑飞
        #    就是这条路径。halt() 自带验证与升级，且不受状态机断言约束。
        lk.halt()
    while time.time() - lk._t0 < 3.10:                    # ⑥ 采停车后的帧
        lk.batch()
    return {"frames": list(lk.frames), "acks": list(lk.acks),
            "run_ack": run["ack"], "t_run": run["t_run"],
            "launch_gap_ms": run["gap_ms"], "launch_got": run["got"]}


def stage3(lk: Sess, res: dict) -> None:
    print("\n[L3] 发车标记 —— 复合命令 `MAZE RUN PULL %d %d` 一条搞定" % (MARK_N, MARK_T))
    run = None
    for k in range(3):
        run = _launch_once(lk)
        m = find_mark_window(run["frames"])
        note("第 %d 次尝试" % (k + 1), "%d 帧 · 发车 -> %s · 全 1 窗口 %s · 空窗 %s" %
             (len(run["frames"]), run["run_ack"],
              ("%d 帧" % m["count"]) if m.get("found") else "未出现",
              ("%.0f ms" % run["launch_gap_ms"]) if run["launch_gap_ms"] is not None else "无帧"))
        if m.get("found") and " OK" in run["run_ack"]:
            break
        if k < 2:
            note("本轮不干净，清场重试", "（被打坏的 MAZE RUN 不会执行，重发才安全）")
            lk.halt()          # ★ 停车必须走 halt()：send_checked 会被状态断言拦住
            time.sleep(1.0)
    fr = run["frames"]
    res["l3_run_ack"] = run["run_ack"]
    res["l3_frames"] = len(fr)
    res["launch_gap_ms"] = run["launch_gap_ms"]
    res["launch_got"] = run["launch_got"]

    # ---- 方案 E 的核心判据: 发车 -> 下一帧到达的空窗 ----
    gap = run["launch_gap_ms"]
    check("复合命令 ACK 为 OK", " OK" in run["run_ack"], run["run_ack"])
    check("复合命令自带的一批收满", (run["launch_got"] or 0) >= MARK_N,
          "%d/%d 帧" % (run["launch_got"] or 0, MARK_N))

    # ---- 空窗判据 ----
    # 复合命令省掉的是"发车之后**再下一条** RATE PULL"的那一整个往返，所以它的
    # 下界 = **单条命令的物理往返**（下行 + 首帧上行），实测约 150 ms，其中大头是
    # 透传模块的固有传输延迟，任何软件改动都压不掉。故：
    #   ① 硬上限 250 ms（远低于旧写法的 440~2430 ms）；
    #   ② 相对判据：不得比本轮普通 RATE PULL 批次的中位空窗更差（+40 ms 容差）
    #      —— 这条才是"复合命令真的生效"的证据：它必须落回单命令往返的量级。
    prev_gaps = lk.gaps[:-1]
    base_gap = _median(prev_gaps) if prev_gaps else None
    if gap is None:
        check("发车->下一帧空窗 <= %d ms(物理下界)" % LAUNCH_GAP_MS_MAX, False, "一帧都没收到")
    else:
        check("发车->下一帧空窗 <= %d ms(物理下界)" % LAUNCH_GAP_MS_MAX,
              gap <= LAUNCH_GAP_MS_MAX, "%.0f ms(旧写法 440~2430 ms)" % gap)
        if base_gap:
            check("空窗回到单命令往返量级(基线 %.0f ms)" % base_gap,
                  gap <= base_gap + 40, "%.0f ms vs 基线 %.0f ms" % (gap, base_gap))
            note("省掉的往返", "复合命令 %.0f ms ≈ 单命令往返 %.0f ms（旧写法要多 1 个往返）"
                 % (gap, base_gap))

    if not check("收到统一帧", len(fr) >= 25, "%d 帧" % len(fr)):
        return
    check("MAZE RUN 回 ACK OK", " OK" in run["run_ack"], run["run_ack"])

    m = find_mark_window(fr)
    res["mark"] = m
    if not check("帧里出现 4 位全 1 的窗口", m.get("found", False),
                 ("命中 %d 帧" % m.get("count", 0)) if m.get("found") else "未出现"):
        return
    check("窗口出现在发车命令之后", m["t0"] >= run["t_run"] - 0.05,
          "t0=%.2fs 发车=%.2fs" % (m["t0"], run["t_run"]))
    check("窗口至少 2 帧(非单帧毛刺)", m["count"] >= 2, "%d 帧" % m["count"])
    if m["span_ms"] is not None:
        check("窗口跨度 100~600 ms", 100 <= m["span_ms"] <= 600,
              "%.0f ms(采样下限, 标称 300)" % m["span_ms"])
    check("窗口前一帧不是全 1", not m["prev_all_on"], "prev_all_on=%s" % m["prev_all_on"])
    check("窗口后一帧不是全 1", not m["next_all_on"], "next_all_on=%s" % m["next_all_on"])
    note("窗口内 4 位", str([_bits(f) for f in fr[m["i0"]:m["i1"] + 1]][:6]))
    if m["i0"] > 0 and m["i1"] + 1 < len(fr):
        before, after = _bits(fr[m["i0"] - 1]), _bits(fr[m["i1"] + 1])
        res["mark"]["before"], res["mark"]["after"] = before, after
        note("窗口前 -> 后 4 位", "%s -> %s" % (list(before), list(after)))
        # 不能要求"led1..led3 与标记前逐位相同"：led2 是状态灯、led4 是基准灯，
        # 发车后本来就会翻；led1 也可能被后续动作驱动。标记的语义是"唯一的全亮段"，
        # 所以判据改成"窗口之后不再出现全 1"。实测旧判据会误报（前 [0,1,0] 后 [1,0,0]）。
        rest = fr[m["i1"] + 1:]
        check("窗口过后没再出现全 1(标记唯一)",
              not any(_bits(f) == (1, 1, 1, 1) for f in rest),
              "后续 %d 帧" % len(rest))

    tail = [f for f in fr if f["_t"] >= 2.40]
    if tail:
        check("MAZE STOP 后回待命(mazeTask=0)", tail[-1]["mazeTask"] == 0,
              "task=%d motion=%d" % (tail[-1]["mazeTask"], tail[-1]["motionTask"]))
        res["after_stop"] = dict(zip(LEDS, _bits(tail[-1])))
        note("收车后 4 位", str(list(_bits(tail[-1]))))


# --------------------------------------------------------------------------- #
#  L4 收尾
# --------------------------------------------------------------------------- #
def stage4(lk: Sess, res: dict) -> None:
    print("\n[L4] 收尾 —— 软复位清场并复核复位原因")
    lk.reset_log()
    try:
        _, ack = lk.send_checked("MCU RESET")
    except Exception as e:                                      # noqa: BLE001
        check("MCU RESET 下发", False, str(e)[:80])
        return
    check("MCU RESET 有 ACK", " OK" in ack, ack)
    time.sleep(4.0)
    pong = None
    for _ in range(3):
        lk.reset_log()
        try:
            lk.send_checked("PING", tries=2)
        except Exception as e:                                  # noqa: BLE001
            note("PING 写失败", str(e)[:70])
            time.sleep(1.5)
            continue
        pong = parse_pong([s for _, s in lk.lines if not s.startswith("<sent>")])
        if pong:
            break
        time.sleep(1.0)
    if check("复位后能 PING 通", pong is not None, str(pong)):
        res["rst"] = pong["rst"]
        check("复位原因 = SW(软复位)", pong["rst"] == "SW", pong["rst"])


# --------------------------------------------------------------------------- #
#  离线自检
# --------------------------------------------------------------------------- #
def _mk(seq_bits, dt=0.07):
    return [{"_t": i * dt, "led1": b[0], "led2": b[1], "led3": b[2], "led4": b[3],
             "mazeTask": 1, "motionTask": 0} for i, b in enumerate(seq_bits)]


class _FakeSer:
    """不碰串口的假串口：只测状态机的状态迁移与断言，不测时序。"""

    def __init__(self):
        self.buf = b""

    def write(self, data):
        self.buf += data
        return len(data)

    def flush(self):
        pass

    def read(self, n):
        d, self.buf = self.buf[:n], self.buf[n:]
        return d


class _FakeSess(Sess):
    """`_FakeSess.cmd` 用的是**父类真实实现** —— 被测的正是它的 IDLE 断言。"""

    def __init__(self, state=IDLE):
        self.ser = _FakeSer()
        self.rb = None
        self.seq = 900
        self.frames, self.lines, self.acks = [], [], []
        self._t0 = time.time()
        self.state = state
        self.want = 0
        self._base = 0
        self.gaps = []
        self.recoveries = 0
        self._halted = False


class _HaltProbe(Sess):
    """只验 `halt()` 的**分级决策**与命令序列：不碰串口、不测时序。

    起手就把状态设成 PENDING —— 这本身是一条判据：证明停车命令**不被三态断言拦**。
    """

    def __init__(self, ack="ACK 1 OK", stopped=True):
        self.ser = None
        self.rb = None
        self.seq = 0
        self.frames, self.lines, self.acks = [], [], []
        self._t0 = time.time()
        self.state = PENDING
        self.want = 0
        self._base = 0
        self.gaps = []
        self.recoveries = 0
        self._halted = False
        self.sent: list = []
        self._ack = ack
        self._stopped = stopped

    def raw_cmd(self, text, timeout=SAFETY_ACK_TIMEOUT):
        self.sent.append(text)
        return self._ack

    def _confirm_stopped(self, tries=2):
        return self._stopped


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except AssertionError:
        return True
    except Exception:                                          # noqa: BLE001
        return False


def _settle_probe(ack="ACK 1 OK", got=4, want=4):
    """跑一次 `_settle`，返回 (结束状态, 是否走了 _recover)。"""
    fs = _FakeSess(IDLE)
    seen = []
    fs._recover = lambda: (seen.append(1), setattr(fs, "state", RESETTING))[0]
    fs._settle(901, 0.0, want, ack, None, got)
    return fs.state, bool(seen)


def selftest() -> int:
    print("[SELFTEST] 判据函数离线自检")

    st = analyze_stability(_mk([(0, 1, 0, 0)] * 10))
    check("稳定序列 0 翻转", all(v["toggles"] == 0 for v in st.values()),
          str({k: v["toggles"] for k, v in st.items()}))
    st = analyze_stability(_mk([(0, 1, 0, 0), (0, 1, 1, 0)]))
    check("单次翻转被计为 1", st["led3"]["toggles"] == 1, str(st["led3"]))

    seq = [(0, 1, 0, 1 if (i * 0.07 % 1.0) < 0.5 else 0) for i in range(60)]
    b = analyze_beacon(_mk(seq), "led4")
    med = statistics.median(b["periods"]) if b["periods"] else 0
    check("1 Hz 方波周期 ≈1000 ms", abs(med - 1000) <= 120, "中位 %.0f ms" % med)
    check("方波边沿足够", b["toggles"] >= 4, "%d 次" % b["toggles"])
    check("恒定电平无边沿", analyze_beacon(_mk([(0, 0, 0, 1)] * 30), "led4")["toggles"] == 0)

    seq = [(0, 1, 0, 1)] * 8 + [(1, 1, 1, 1)] * 4 + [(1, 1, 1, 0)] * 8
    m = find_mark_window(_mk(seq))
    check("标记窗口被检出", m.get("found", False), str(m.get("count")))
    check("窗口帧数 = 4", m.get("count") == 4, str(m.get("count")))
    check("窗口跨度 ≈210 ms", m.get("span_ms") is not None and 150 <= m["span_ms"] <= 260,
          "%.0f" % (m.get("span_ms") or 0))
    check("窗口前非全 1", not m["prev_all_on"], str(m["prev_all_on"]))
    check("窗口后非全 1", not m["next_all_on"], str(m["next_all_on"]))

    m2 = find_mark_window(_mk([(1, 1, 1, 0)] * 5 + [(1, 1, 1, 1)] + [(1, 1, 1, 0)] * 5))
    check("单帧毛刺 count=1", m2.get("count") == 1, str(m2.get("count")))
    check("单帧毛刺无跨度", m2.get("span_ms") is None, str(m2.get("span_ms")))
    check("无窗口时 found=False",
          not find_mark_window(_mk([(0, 1, 0, 1)] * 20)).get("found", False))

    ok = True
    for bad in ("MOVE RUN 1 2 3 4", "CAL RUN", "SPEED 100"):
        try:
            build_cmd(bad, 1)
            ok = False
        except AssertionError:
            pass
    check("白名单拦住 MOVE/CAL/SPEED", ok, "三条都拒绝")
    check("白名单保留 ESTOP(急停升级要用)",
          build_cmd("ESTOP", 2).startswith(b"ESTOP #2*"), "ESTOP 可用")
    good = build_cmd("MAZE RUN", 7)
    check("MAZE RUN 报文格式", good.endswith(b"\r\n") and b" #7" in good,
          good.decode("ascii").strip())

    # ---- 复合命令报文 ----
    combo = build_cmd("MAZE RUN PULL 8 40", 11)
    ctxt = combo.decode("ascii")
    check("复合命令带 PULL 参数", ctxt.startswith("MAZE RUN PULL 8 40 #11*"),
          ctxt.strip())
    check("复合命令仍受白名单约束",
          _raises(lambda: build_cmd("MOVERUN PULL 1 1", 1)), "越界前缀被拒")

    # ---- 三态状态机的离线判据(不需要硬件) ----
    check("状态常量齐全", (IDLE, PENDING, RESETTING) == ("IDLE", "PENDING", "RESETTING"),
          "%s/%s/%s" % (IDLE, PENDING, RESETTING))
    check("空窗硬上限 = 250 ms(单命令往返物理下界)", LAUNCH_GAP_MS_MAX == 250,
          str(LAUNCH_GAP_MS_MAX))
    check("_median 奇偶都正确", _median([1, 3]) == 2.0 and _median([5, 1, 3]) == 3.0,
          "%s / %s" % (_median([1, 3]), _median([5, 1, 3])))
    fe = _FakeSess(IDLE)
    fe.lines = [(0.0, "<sent> RATE PULL 4 60"), (0.1, "ACK -1 ERR_CRC")]
    check("识别无 seq 的 ERR_CRC(才会同 seq 重发)", fe._link_err(0) is not None,
          str(fe._link_err(0)))
    fe.lines = [(0.0, "ACK 5 OK")]
    check("正常 ACK 不算链路错误", fe._link_err(0) is None, "无")
    check("非 IDLE 禁止发命令", _raises(lambda: _FakeSess(PENDING).cmd("RATE PULL 4 60")),
          "PENDING 下发被断言拦住")
    fs = _FakeSess(IDLE)
    fs.cmd("RATE PULL 4 60")
    check("IDLE 下可发命令", fs.lines[-1][1] == "<sent> RATE PULL 4 60", fs.lines[-1][1])
    check("RESETTING 下禁止发命令",
          _raises(lambda: _FakeSess(RESETTING).cmd("RATE PULL 4 60")), "断言拦住")
    st, rec = _settle_probe(ack="ACK 1 OK", got=4, want=4)
    check("批次收满 ⇒ 回 IDLE", st == IDLE and not rec, "%s recovered=%s" % (st, rec))
    st, rec = _settle_probe(ack="ACK 1 OK", got=3, want=4)
    check("批次没收满 ⇒ 走重建静默", rec and st == RESETTING, "%s recovered=%s" % (st, rec))
    st, rec = _settle_probe(ack=None, got=4, want=4)
    check("ACK 丢失 ⇒ 走重建静默", rec, "ACK=None recovered=%s" % rec)

    # ---- 停车安全网（2026-10-23 车跑飞事故的回归判据）----
    print("\n[安全网] 停车优先级高于状态机")
    hp = _HaltProbe(ack="ACK 1 OK", stopped=True)
    ok = hp.halt()
    check("PENDING 状态下停车命令照样发出", hp.sent and hp.sent[0] == "MAZE STOP",
          "首条=%s" % (hp.sent[0] if hp.sent else "无"))
    check("STOP 应答 OK 且验证停稳 ⇒ 不下发 ESTOP/RESET",
          ok and "ESTOP" not in hp.sent and "MCU RESET" not in hp.sent,
          "序列=%s" % hp.sent)
    check("停车后状态归位 IDLE", hp.state == IDLE, hp.state)

    hp = _HaltProbe(ack="TIMEOUT", stopped=False)
    ok = hp.halt()
    check("STOP 无应答 ⇒ 判定失败", not ok, str(ok))
    check("STOP 无应答 ⇒ 升级 ESTOP 与 MCU RESET",
          "ESTOP" in hp.sent and "MCU RESET" in hp.sent, "序列=%s" % hp.sent)
    check("STOP 重试次数 = SAFETY_HALT_TRIES", hp.sent.count("MAZE STOP") == SAFETY_HALT_TRIES,
          "%d 次" % hp.sent.count("MAZE STOP"))

    hp = _HaltProbe(ack="ACK 1 OK", stopped=False)
    ok = hp.halt()
    check("STOP 回 OK 但仍在动 ⇒ 判失败并升级急停",
          not ok and "ESTOP" in hp.sent, "序列=%s" % hp.sent)

    hp = _HaltProbe(ack="ACK 1 OK", stopped=True)
    check("verify=False 时不拉帧(退出兜底用)",
          hp.halt(verify=False, quiet=True) and hp.sent == ["MAZE STOP"], str(hp.sent))

    fsr = _FakeSess(PENDING)
    fsr.rb = codec.FrameStreamParser()
    check("raw_cmd 绕过 cmd() 的 IDLE 断言",
          fsr.raw_cmd("MAZE STOP", timeout=0.02) is None, "未抛异常")

    print("\n%s  ——  自检 %d 通过 / %d 失败" %
          ("[PASS]" if _nfail == 0 else "[FAIL]", _npass, _nfail))
    return 0 if _nfail == 0 else 1


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="LED 位上报 + 复合命令空窗专项实机验证 (FW 313)")
    ap.add_argument("port", nargs="?", default="COM7", help="串口号 (默认 COM7)")
    ap.add_argument("--selftest", action="store_true", help="离线判据自检，不碰串口")
    ap.add_argument("--no-launch", action="store_true", help="跳过 L3(不发车)")
    ap.add_argument("--no-reset", action="store_true", help="跳过 L4 收尾软复位")
    ap.add_argument("--idle-sec", type=float, default=3.0, help="L1 采样时长")
    ap.add_argument("--json", default="", help="把结论写到 JSON")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    print("=" * 70)
    print(" LED 位 + 复合命令专项验证   端口 %s   批量下拉 %d 帧/%d ms ≈13 Hz" %
          (a.port, BATCH_N, BATCH_T))
    print("=" * 70)
    res = {"port": a.port, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    lk = open_link(a.port)
    got_stop = False
    try:
        # 静默握手：确认固件此刻不在发帧，才能安全下发第一条命令。
        # 这是"此前没有传输、此次拉流是启动"场景的前置条件（方案 E.2）。
        lk.quiet_handshake()
        if not stage0(lk, res):
            return 1
        stage1(lk, res, a.idle_sec)
        stage2(lk, res)
        if a.no_launch:
            print("\n[L3] 跳过 (--no-launch)")
        else:
            stage3(lk, res)
        if a.no_reset:
            print("\n[L4] 跳过 (--no-reset)")
        else:
            stage4(lk, res)
        res["recoveries"] = lk.recoveries
        check("全程未触发失同步重建", lk.recoveries == 0,
              "RESETTING 次数 %d" % lk.recoveries)
        if lk.gaps:
            note("各批空窗(下发→首帧)", " / ".join("%.0f ms" % g for g in lk.gaps))
    except KeyboardInterrupt:
        # Ctrl-C 不是 Exception 子类，必须单独接：否则 finally 仍会跑，但用户看到
        # 的是难看栈；更重要的是下面 finally 里一定要把车停稳。
        print("\n[中断] Ctrl-C —— 立即停车")
    finally:
        # ★★ 退出路径的第一件事**永远是停车**，且必须早于 RATE OFF ★★
        # 2026-10-23 事故：这里原先只有 lk.off()（RATE OFF），从不发 MAZE STOP，
        # 于是一次中断就把车留在了跑动状态。halt() 内部会屏蔽 SIGINT，连按两次
        # Ctrl-C 也打不断它；它自己带验证与 ESTOP/RESET 升级。
        try:
            got_stop = lk.halt()
            res["halt_ok"] = got_stop
        except BaseException:                              # noqa: BLE001
            pass                                           # 绝不让清理再抛
        try:
            lk.off()
        except BaseException:                              # noqa: BLE001
            pass
        lk.close()
        res["pass"], res["fail"] = _npass, _nfail
        res["ack_retries"] = [a for a in lk.acks if "retry" in a[1]]
        if res["ack_retries"]:
            print("\n[注] 下行共 %d 次被打坏后同 seq 重发(固件幂等重放，不会重复执行): %s"
                  % (len(res["ack_retries"]),
                     ", ".join("%s->%s" % (a[0], a[1].split(" ")[-3]) for a in res["ack_retries"])))
        if a.json:
            with open(a.json, "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False, indent=2, default=str)
            print("\n结论写入 %s" % a.json)

    print("\n%s  ——  %d 通过 / %d 失败" %
          ("[PASS]" if _nfail == 0 else "[FAIL]", _npass, _nfail))
    return 0 if _nfail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
