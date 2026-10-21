#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LED 位上报专项实机验证 —— v9.1 / FW 311（状态B1 的 bit2-5 = led1..led4）。

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
命令一律走 `send_checked()`：被打坏就用**同一 seq + 同一正文**重发 —— 固件的
"幂等重放"分支保证不会重复执行。

用法:
    python led_bit_verify.py COM7                 # L1..L4 全跑，末尾软复位清场
    python led_bit_verify.py COM7 --no-launch     # 不跑 L3（不发车）
    python led_bit_verify.py COM7 --no-reset      # 不收尾软复位
    python led_bit_verify.py --selftest           # 离线判据自检（不需要硬件）

安全: 下行命令走本文件白名单 PING/TELEM/RATE/ARM/MAZE/MCU，其余断言拦死。
      L3 会发一次 `MAZE RUN`（车轮必须悬空），2.2 s 后 `MAZE STOP`。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from basic_cmd_smoke import parse_pong              # noqa: E402
from host.protocol import codec                     # noqa: E402

LEDS = ("led1", "led2", "led3", "led4")
MARK_MS = 300          # 固件 LINK_LAUNCH_MARK_MS
BEACON_MS = 1000       # 固件 LINK_TIME_DIV
BATCH_N, BATCH_T = 4, 60     # 每周期 4 帧、间隔 60 ms ⇒ ≈13 Hz（与 link_pull_evt N=4 同款）
MARK_N, MARK_T = 6, 40       # 发车那一刻用更密的一批(240 ms)，覆盖 300 ms 标记窗口
ALLOWED = {"PING", "TELEM", "RATE", "ARM", "MAZE", "MCU"}

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


# --------------------------------------------------------------------------- #
#  串口会话：RATE FIX 连续取流 + 期间插命令
# --------------------------------------------------------------------------- #
class Sess:
    def __init__(self, port: str):
        import serial
        self.ser = serial.Serial(port, 115200, timeout=0.03, write_timeout=1.5)
        self.rb = codec.FrameStreamParser()
        self.seq = 900
        self.frames: list = []
        self.lines: list = []
        self.acks: list = []
        self._t0 = time.time()

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:                                       # noqa: BLE001
            pass

    def cmd(self, text: str) -> int:
        self.seq += 1
        self.ser.write(build_cmd(text, self.seq))
        self.ser.flush()
        self.lines.append((time.time() - self._t0, "<sent> " + text))
        return self.seq

    def send_checked(self, text: str, tries: int = 5, timeout: float = 1.2):
        """下发并**确认 ACK OK**；ERR_CRC / ERR_NOCRC / 超时都用**同一 seq、同一正文**重发。

        为什么要这个：取流期间下行命令会偶发被透传模块打坏（实测 `ACK -1 ERR_CRC`、
        `ERR_NOCRC`），一旦被打坏命令就**根本没执行**（例如 ARM 没执行 ⇒ 基准灯不闪，
        看起来像固件坏了）。重发同一 seq + 同一 CRC 是安全的：固件"幂等重放"分支
        （link.c:3199）会把上次应答原样回一遍，**不会重复执行**命令 —— 所以即使
        第一次其实执行了、只是 ACK 丢了，重发也不会真的再执行一次。
        """
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
              pre_cmds=(), want: int = 0, timeout: float = 1.0) -> int:
        """一个**批量下拉**周期：先发 pre_cmds，再发 PULL，收够 want(缺省 n) 帧。

        这就是 `link_pull_evt.py` 的形态：一批发完固件就静默，等下一条 PULL。
        绝不用 RATE FIX 连续发流 —— 实测连续发流会让透传模块缓冲灌满(0 字节，
        必须断电)，且取流期间下行命令会被打坏(`ACK -1 ERR_CRC`)。
        """
        base = len(self.frames)
        for c in pre_cmds:
            self.send_checked(c)
        self.send_checked("RATE PULL %d %d" % (n, t_ms))
        t0 = time.time()
        need = want or n
        while len(self.frames) - base < need and time.time() - t0 < timeout:
            self.pump()
        return len(self.frames) - base

    def stream(self, dur: float, n: int = BATCH_N, t_ms: int = BATCH_T,
               schedule=(), post_cmds=None) -> None:
        """连续做 dur 秒的批量下拉；schedule=[(相对秒, [命令...])] 在周期边界插命令。

        post_cmds: 每个周期 PULL 之前都要补发的命令（L3 用来在发车后持续加密采样）。
        """
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
        """安全网：确保固件不在自时钟发流（批量模式下本来就静默）。"""
        self.send_checked("RATE OFF", tries=3)
        base = len(self.frames)
        t0 = time.time()
        while time.time() - t0 < 0.25:
            self.pump()
        if len(self.frames) != base:
            note("RATE OFF 后仍有残流", "多收 %d 帧" % (len(self.frames) - base))

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
            return Sess(port)
        except Exception as e:                                  # noqa: BLE001
            last = e
            print("  打开 %s 失败(%d/%d): %s" % (port, k + 1, tries, str(e)[:88]))
            time.sleep(2.0)
    raise SystemExit("串口打不开: %s" % last)


# --------------------------------------------------------------------------- #
#  L0 前置指纹
# --------------------------------------------------------------------------- #
def stage0(lk: Sess, res: dict) -> bool:
    print("\n[L0] 前置指纹 —— 确认烧的是 FW 311(带 LED 位)")
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
    check("fw = 311 (LED 位版本)", pong["fw"] == 311, "fw=%d" % pong["fw"])
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
    """一轮完整的"采基线 → 发车 → 密采样 → 收车"。返回该轮的帧与 ACK。

    关键时序：`MAZE RUN` **不能等 ACK 再发 PULL** —— 标记窗口只有 300 ms，而
    RTT≈300 ms，等 ACK 就已经错过。所以发车行写完 20 ms 就紧跟一条密 PULL 批次
    （20 ms 远大于固件 1 行/ms 的消费速率，不会撞掉第二行）。
    """
    lk.reset_log()
    lk.batch()                                        # ① 窗口前的上下文帧
    while time.time() - lk._t0 < 0.70:                # ② 采到 ~0.7 s
        lk.batch()
    lk.seq += 1
    run_seq = lk.seq
    lk.ser.write(build_cmd("MAZE RUN", run_seq))      # ③ 发车（不等 ACK）
    lk.ser.flush()
    t_run = time.time() - lk._t0
    lk.lines.append((t_run, "<sent> MAZE RUN"))
    time.sleep(0.02)
    lk.batch(MARK_N, MARK_T, want=MARK_N, timeout=1.2)  # ④ 紧跟一批密采样覆盖窗口
    while time.time() - lk._t0 < 2.30:                # ⑤ 继续常规采样到 2.3 s
        lk.batch()
    lk.send_checked("MAZE STOP")                      # ⑥ 收车
    while time.time() - lk._t0 < 3.10:
        lk.batch()
    ack = [s for _, s in lk.lines if s.startswith("ACK %d " % run_seq)]
    return {"frames": list(lk.frames), "acks": list(lk.acks),
            "run_ack": ack[0] if ack else "无 ACK", "t_run": t_run}


def stage3(lk: Sess, res: dict) -> None:
    print("\n[L3] 发车标记 —— MAZE RUN 后 4 位应同时为 1 约 %d ms" % MARK_MS)
    run = None
    for k in range(3):
        run = _launch_once(lk)
        m = find_mark_window(run["frames"])
        note("第 %d 次尝试" % (k + 1), "%d 帧 · MAZE RUN -> %s · 全 1 窗口 %s" %
             (len(run["frames"]), run["run_ack"],
              ("%d 帧" % m["count"]) if m.get("found") else "未出现"))
        if m.get("found") and " OK" in run["run_ack"]:
            break
        if k < 2:
            note("本轮不干净，清场重试", "（被打坏的 MAZE RUN 不会执行，重发才安全）")
            lk.send_checked("MAZE STOP")
            time.sleep(1.5)
    fr = run["frames"]
    res["l3_run_ack"] = run["run_ack"]
    res["l3_frames"] = len(fr)
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
        check("窗口过后 led1..led3 回到标记前",
              all(before[j] == after[j] for j in range(3)),
              "前 %s 后 %s" % (list(before[:3]), list(after[:3])))

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
    for bad in ("MOVE RUN 1 2 3 4", "ESTOP"):
        try:
            build_cmd(bad, 1)
            ok = False
        except AssertionError:
            pass
    check("白名单拦住 MOVE/ESTOP", ok, "两条都拒绝")
    good = build_cmd("MAZE RUN", 7)
    check("MAZE RUN 报文格式", good.endswith(b"\r\n") and b" #7" in good,
          good.decode("ascii").strip())

    print("\n%s  ——  自检 %d 通过 / %d 失败" %
          ("[PASS]" if _nfail == 0 else "[FAIL]", _npass, _nfail))
    return 0 if _nfail == 0 else 1


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="LED 位上报专项实机验证 (FW 311)")
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
    print(" LED 位上报专项验证   端口 %s   批量下拉 %d 帧/%d ms ≈13 Hz" %
          (a.port, BATCH_N, BATCH_T))
    print("=" * 70)
    res = {"port": a.port, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    lk = open_link(a.port)
    try:
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
    finally:
        try:
            lk.send_checked("RATE OFF", tries=2)
        except Exception:                                       # noqa: BLE001
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
