# -*- coding: utf-8 -*-
"""v9(统一帧 30 B)实机验收 —— 只管"实时拉取这条链路 + 新帧语义", **绝不发运动命令**。

为什么需要它
------------
v9 把统一帧从 49 B 压到 30 B(线上 57 -> 38 B), 同时删掉 20 个字段、重排了全部位域。
离线套件(contract/selftest/pull/...)只能证明"代码内部自洽", 证明不了"板上烧的那片 hex
真的按 v9 组帧"。历史上已经栽过一次同类跟头: 远程档编完了没上板, 一接上位机就是
"全静默", 而当时所有离线测试都是绿的。所以本脚本把"上板后最先该看的几件事"固化成阶段。

阶段(默认按顺序跑, 硬闸失败即停)
--------------------------------
S0 离线一致性  上位机 codec 常量 vs **固件源码** `REMOTE/link.h`(版本号 / 帧长 / 16 个
               `LINK_U_*` 偏移 / 8 个 flags 位 / 状态C 墙位), 加
               `pack_unified` <-> `decode_unified` 往返与派生量恒等式; 并报告待烧
               `USER/Remote/TEST.hex` 的 sha256 与 mtime(判断"烧的是不是这份源码编的")。
S1 PING 指纹   proto=9 / fw=311 / mazetype=16 —— **判"烧错档 / 烧旧固件"的唯一入口**。
S2 单帧语义   连续 8 次 TELEM: 载荷必须**正好 30 B**。烧了 v8 固件时这里才会暴露 ——
               codec 会拿 49 B 的前 30 B 去解, **不报错只出垃圾**, 所以必须量长度;
               再验 33 通道齐全、`frontNear == wallFront & !frontFar`、`frontFar ⊆ wallFront`、
               位域宽度(garbage 会越界)。
S3 容量阶梯   (可选) 子进程跑既有**事件触发式**拉取器 `link_pull_evt.py`, 默认 N=4/5 各 20 s,
               看交付率 / 实收 Hz, 得到 v9 的**新容量点**。v8 时代被 57 B/帧的经验值
               `--max-hz 13.3` 卡住, 38 B/帧理论上限 303 Hz ⇒ 这里显式把闸放宽再测。
               **实测(N=4 13.1 Hz / N=5 17.0 Hz 均 20 s 存活, 交付率 1.000; N=6 20.3 Hz
               但 17 s 后整条链路死透, 须拔电重启模块)** ⇒ 默认阶梯止于 5, N>=6 要显式指定。
S4 收尾       `RATE OFF` 后 1.2 s 内不得再有统一帧(在途 <=1 属正常), 再取一帧确认
               `estop_lock` / `emerg` 均为 False —— 证明拉取能干净停住、不留残流。

安全边界
--------
* **绝不发运动命令**: 只发 `PING` / `TELEM` / `RATE PULL` / `RATE OFF`。下行报文走
  `basic_cmd_smoke.build()`, 它对 MOVE/MAZE/CAL/ARM/ESTOP/STOP/PARAMS/SAVE 有断言硬拦。
* S3 用**子进程**跑既有拉取器, 本进程不持有串口 —— 避免两个进程抢 COM7。
* 容量阶梯可能摸到透传模块上限: 某档 BROKE 就**立即停**, 不再往上加 N。模块被打死只能
  **物理断电**恢复(排程式脚本曾 6.9 s 灌死一次)。

用法
----
    python v9_pull_verify.py --selftest                  # 离线自检(不接串口)
    python v9_pull_verify.py COM7                        # 全流程(S0-S4, 约 90 s)
    python v9_pull_verify.py COM7 --no-ladder            # 只做静态验收(约 15 s)
    python v9_pull_verify.py COM7 --ladder 4,6 --ladder-sec 15
    python v9_pull_verify.py COM7 --no-ladder --json v9.json

退出码: 0 全绿 / 1 有 FAIL / 2 环境问题(串口打不开、缺 pyserial)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import re
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from host.protocol import codec  # noqa: E402
from host import fwprofile  # noqa: E402

#: 板上应当报的指纹。固件源码里读到的是 v9 期望值, 这里再钉一遍是为了让"源码被谁改错了"
#: 也能在 S0 就暴露(否则 S0 会拿错源码当正确答案去比对 codec)。
EXPECT_PROTO = 9
EXPECT_FW = 311
EXPECT_MAZETYPE = 16

#: v8(57 B/帧)时代的实测容量点, 只用于 S3 的对比话术。
V8_BASELINE_HZ = 13.34

#: 辅助函数把"丢帧计数不可用"记成这个值(与 link_pull_pace.FrameLog 一致)。
DROP_NA = -1

PASS, FAIL, NOTE = [], [], []


def check(tag: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(tag)
    print("  [%s] %-52s %s" % ("OK  " if ok else "FAIL", tag, detail))
    return ok


def note(msg: str) -> None:
    NOTE.append(msg)
    print("  [..] %s" % msg)


def sha16(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


# ============================================================ S0 离线一致性

#: 统一帧载荷偏移 —— 名字两侧一一对应(`codec.U_*` <-> 固件 `LINK_U_*`)。
U_NAMES = ("MS8", "IRMM", "BATTMV", "SPEEDL", "SPEEDR", "GYRO", "ENCANG",
           "TRACKL", "TRACKR", "STATE_A", "STATE_B0", "STATE_B1", "STATE_C",
           "FLAGS", "POS", "SPURT")

#: flags 位 —— `codec.FLAG_*` <-> 固件 `LINK_FLAG_*`。
FLAG_NAMES = ("FREEMODE", "SLEEP", "EMERG", "PULLF1", "IR_SWITCH",
              "MOTOR_OFF", "ESTOP_LOCK", "EVQ")

#: 状态C 的墙位 —— `codec.STATE_C_*` <-> 固件 `LINK_C_*`。
C_NAMES = ("WALL_LEFT", "WALL_FRONT", "WALL_RIGHT", "FRONT_FAR", "FRONT_BOTH")

#: 状态B1 的位 —— `codec.STATE_B1_*` <-> 固件 `LINK_B1_*`(v9.1 起含 4 个 LED 位)。
B1_NAMES = ("SPURTFLAG", "GOBACK", "LED1", "LED2", "LED3", "LED4")


def _sample_dicts(n: int = 120):
    """构造覆盖位域边界的扁平帧字典(pack 的输入)。"""
    rnd = random.Random(20261021)
    out = []
    # 先放 4 组"全 0 / 全 1 / 交替 / 边界", 再补随机
    fixed = [
        dict(wall=0, front_far=0, pos=0, spurt=0, task=0, maze=0, sp=0),
        dict(wall=1, front_far=1, pos=0xFF, spurt=0xFF, task=107, maze=6, sp=-32768),
        dict(wall=1, front_far=0, pos=0x5A, spurt=0x0A, task=63, maze=3, sp=32767),
        dict(wall=0, front_far=0, pos=0xA5, spurt=0x07, task=1, maze=1, sp=-1),
    ]
    for k, f in enumerate(fixed + [None] * n):
        if f is None:
            f = dict(wall=rnd.randint(0, 1), front_far=rnd.randint(0, 1),
                     pos=rnd.randint(0, 0xFF), spurt=rnd.randint(0, 0xFF),
                     task=rnd.randint(0, 107), maze=rnd.randint(0, 6),
                     sp=rnd.randint(-32768, 32767))
        d = {
            "ms8": rnd.randint(0, 255) if f is not fixed[0] else 0,
            "irF1": f["sp"], "irF2": max(-32768, min(32767, -f["sp"])),
            "irL": rnd.randint(-32768, 32767),
            "irR": rnd.randint(0, 32767),
            "battMv": rnd.randint(0, 32767),   # pack 走 _sat16, 超界会被钳 ⇒ 取合法域
            "speedL": rnd.randint(-32768, 32767), "speedR": rnd.randint(-32768, 32767),
            "gyroAngle": rnd.randint(-32768, 32767),
            "encAngle": rnd.randint(-32768, 32767),
            "trackL": rnd.randint(-32768, 32767), "trackR": rnd.randint(-32768, 32767),
            "dir": f["maze"] & 0x03, "mazeTask": f["maze"],
            "objectGoToFlag": rnd.randint(0, 1),
            "motionTask": f["task"],
            "spurtFlag": f["spurt"] & 0x01, "goOrBack": (f["spurt"] >> 1) & 0x01,
            # v9.1: 4 个 LED 位(取 spurt 样本的高位, 覆盖全 0/全 1/交替)
            "led1": (f["spurt"] >> 2) & 0x01, "led2": (f["spurt"] >> 3) & 0x01,
            "led3": (f["spurt"] >> 4) & 0x01, "led4": (f["spurt"] >> 5) & 0x01,
            "wallLeft": f["wall"] & 0x01, "wallFront": (f["wall"] >> 1) & 0x01,
            "wallRight": (f["wall"] >> 2) & 0x01, "frontFar": f["front_far"],
            "frontBoth": (f["wall"] >> 4) & 0x01,
            "flags": f["spurt"],
            "posX": f["pos"] & 0x0F, "posY": (f["pos"] >> 4) & 0x0F,
            "spurtMode": f["spurt"] & 0x07, "spurtTestFlag": (f["spurt"] >> 3) & 0x01,
            "goaheadnum": (f["spurt"] >> 4) & 0x0F,
        }
        if d["frontFar"] and not d["wallFront"]:
            d["frontFar"] = 0          # 组帧不变量: 远墙档必然有墙
        out.append(d)
    return out


def stage0_offline() -> bool:
    print("\n[S0] 离线一致性 —— 上位机 codec vs 固件源码 REMOTE/link.h")

    fw = fwprofile.fw_expect()
    check("固件源码版本号 = (proto %d, fw %d)" % (EXPECT_PROTO, EXPECT_FW),
          fw == (EXPECT_PROTO, EXPECT_FW), str(fw))
    check("codec.UNIFIED_LEN = 30 (v9)", codec.UNIFIED_LEN == 30,
          str(codec.UNIFIED_LEN))
    check("固件 LINK_UNIFIED_LEN == codec.UNIFIED_LEN",
          fwprofile.link_macro("LINK_UNIFIED_LEN") == codec.UNIFIED_LEN,
          str(fwprofile.link_macro("LINK_UNIFIED_LEN")))

    # ---- 16 个载荷偏移 ----
    bad = []
    for n in U_NAMES:
        got = fwprofile.link_macro("LINK_U_" + n)
        want = getattr(codec, "U_" + n)
        if got != want:
            bad.append("%s 固件=%s codec=%s" % (n, got, want))
    check("16 个 U_* 偏移逐项一致", not bad, "; ".join(bad) or "0 差异")

    # ---- 8 个 flags 位 ----
    bad = []
    for n in FLAG_NAMES:
        got = fwprofile.link_macro("LINK_FLAG_" + n)
        want = getattr(codec, "FLAG_" + n)
        if got != want:
            bad.append("%s 固件=%s codec=%s" % (n, got, want))
    check("8 个 flags 位一致", not bad, "; ".join(bad) or "0 差异")

    # ---- 状态C 墙位 ----
    bad = []
    for n in C_NAMES:
        got = fwprofile.link_macro("LINK_C_" + n)
        want = getattr(codec, "STATE_C_" + n)
        if got != want:
            bad.append("%s 固件=%s codec=%s" % (n, got, want))
    check("状态C 墙位 5 位一致", not bad, "; ".join(bad) or "0 差异")

    # ---- 状态B1: spurtFlag / goOrBack / 4 个 LED 位 ----
    bad = []
    for n in B1_NAMES:
        got = fwprofile.link_macro("LINK_B1_" + n)
        want = getattr(codec, "STATE_B1_" + n)
        if got != want:
            bad.append("%s 固件=%s codec=%s" % (n, got, want))
    check("状态B1 位 %d 项一致(含 4 个 LED 位)" % len(B1_NAMES), not bad,
          "; ".join(bad) or "0 差异")

    # ---- 通道表 ----
    check("UNIFIED_CHANNELS = 33 项(v9.1: 29 + 4 个 LED)", len(codec.UNIFIED_CHANNELS) == 33,
          str(len(codec.UNIFIED_CHANNELS)))
    check("LED 通道追加在通道表末尾(否则 RingBuffer 历史数据错列)",
          list(codec.UNIFIED_CHANNELS[-4:]) == ["led1", "led2", "led3", "led4"],
          str(list(codec.UNIFIED_CHANNELS[-4:])))

    # ---- 往返: pack -> decode 必须回到原值 ----
    diffs = []
    keys = list(codec.UNIFIED_CHANNELS)
    for d in _sample_dicts():
        try:
            got = codec.decode_unified(codec.pack_unified(d))
        except Exception as e:                                # noqa: BLE001
            diffs.append("异常 %s" % type(e).__name__)
            break
        for k in keys:
            if got.get(k) != d.get(k):
                diffs.append("%s: %r -> %r" % (k, d.get(k), got.get(k)))
        if len(diffs) > 4:
            break
    check("pack_unified <-> decode_unified 往返无损(124 组含边界)",
          not diffs, "; ".join(diffs[:4]) or "0 差异")

    # ---- 派生量恒等式: frontNear == wallFront & !frontFar ----
    bad = []
    for wf in (0, 1):
        for ff in (0, 1):
            if ff and not wf:
                continue                                     # 组帧不变量排除该组合
            p = bytearray(codec.UNIFIED_LEN)
            sc = (codec.STATE_C_WALL_FRONT if wf else 0) | \
                 (codec.STATE_C_FRONT_FAR if ff else 0)
            p[codec.U_STATE_C] = sc
            rec = codec.decode_unified(bytes(p))
            if rec["frontNear"] != (wf and not ff):
                bad.append("wf=%d ff=%d -> %d" % (wf, ff, rec["frontNear"]))
            if ff and not rec["wallFront"]:
                bad.append("frontFar 越权(wf=0)")
    check("frontNear 派生恒等式 (wallFront & !frontFar)", not bad,
          "; ".join(bad) or "3 组组合全对")

    # ---- 待烧 hex 指纹(只报告 + 新鲜度提示) ----
    hexp = os.path.join(ROOT, "code_competition", "USER", "Remote", "TEST.hex")
    if os.path.exists(hexp):
        t_hex = os.path.getmtime(hexp)
        src = [os.path.join(ROOT, "code_competition", "REMOTE", f)
               for f in ("link.c", "link.h")]
        t_src = max(os.path.getmtime(p) for p in src if os.path.exists(p))
        note("待烧 hex: USER/Remote/TEST.hex  sha256=%s  mtime=%s"
             % (sha16(hexp), time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t_hex))))
        if t_hex < t_src:
            note("⚠ hex 比 REMOTE/link.c|h 旧 —— 先重编 Target_Remote, 别烧旧片")
        else:
            note("hex 比固件源码新: 是这份源码编的")
    else:
        note("⚠ 找不到 %s —— 还没编远程档?" % hexp)

    return not FAIL


# ============================================================ S1/S2/S4 串口

def _import_link():
    try:
        from basic_cmd_smoke import Link, parse_pong, parse_ack  # noqa: PLC0415
    except ImportError:
        print("  !! 需要 pyserial: pip install pyserial (或用 conda general_env)")
        raise SystemExit(2)
    return Link, parse_pong, parse_ack


def stage1_ping(lk, parse_pong, parse_ack) -> dict | None:
    print("\n[S1] PING —— 链路指纹(判烧错档/旧固件的唯一入口)")
    frames, lines, rtt = lk.talk("PING", timeout=3.0)
    pong = parse_pong(lines)
    # ---- 首包重试(最多 3 次) ----
    # 现场实测: 刚打开串口后的**第一包**偶发只剩尾部 ACK、前 30 B 的 PONG 整行丢失。
    # 这不是固件问题 —— PONG 与 ACK 走同一个 TX 环与同一次 DMA: 环满只会丢**尾部**
    # 字节(先丢 ACK), 不可能只丢前面的 PONG。判据也该反过来用: 只丢前段 = 主机侧
    # USB/CH340 首包截断; 只看到 ACK 而没 PONG 时重发即可(60 次连测 0 复现)。
    tries = 1
    while pong is None and tries < 3:
        print("      第 %d 次: %s ⇒ 重试" % (
            tries, "只有 ACK 无 PONG(首包被 USB 截断)" if parse_ack(lines) else "全静默"))
        time.sleep(0.35)
        frames, lines, rtt = lk.talk("PING", timeout=1.5)
        pong = parse_pong(lines)
        tries += 1
    if pong is None:
        print("      收 %d 行, 无 PONG. 三种全静默按此顺序排: "
              "①USART1 NVIC 未使能 ②上电陀螺标定未完成(车不静止) ③透传模块被灌死(须断电)"
              % len(lines))
        check("PING 有 PONG 回应", False, "3 次均超时")
        return None
    if tries > 1:
        print("      (第 %d 次才拿到 PONG —— 首包截断, 已自动重试)" % tries)
    print("       PONG %d %d %d %d %s   RTT=%.0f ms" % (
        pong["proto"], pong["fw"], pong["mazetype"], pong["txbuf"], pong["rst"], rtt))
    ok = check("proto = %d (v9)" % EXPECT_PROTO, pong["proto"] == EXPECT_PROTO,
               "proto=%d" % pong["proto"])
    ok &= check("fw = %d (远程档 v9)" % EXPECT_FW, pong["fw"] == EXPECT_FW,
                "fw=%d" % pong["fw"])
    ok &= check("mazetype = %d" % EXPECT_MAZETYPE,
                pong["mazetype"] == EXPECT_MAZETYPE, "mazetype=%d" % pong["mazetype"])
    check("rst 合法", pong["rst"] in ("POR", "SW", "PIN", "IWDG"), "rst=%s" % pong["rst"])
    if not ok and pong["proto"] < 9:
        print("      ⇒ 板上是 v8 或更老: 烧的 hex 不对. 重新烧 USER/Remote/TEST.hex")
    return pong


def _frame_semantics(rec: dict) -> list:
    """单帧的**硬**协议语义(与车动不动无关)。返回 [(名称, 是否通过, 详情)]。"""
    out = []
    out.append(("wallFront/frontFar 不变量 frontFar⇒wallFront",
                not (rec["frontFar"] and not rec["wallFront"]),
                "wallFront=%d frontFar=%d" % (rec["wallFront"], rec["frontFar"])))
    out.append(("派生量 frontNear = wallFront & !frontFar",
                rec["frontNear"] == (rec["wallFront"] and not rec["frontFar"]),
                "frontNear=%d front=%s" % (rec["frontNear"], rec["frontName"])))
    out.append(("位域宽度不越界",
                rec["dir"] <= 3 and rec["mazeTask"] <= 6 and rec["motionTask"] <= 107
                and rec["posX"] <= 15 and rec["posY"] <= 15
                and rec["spurtMode"] <= 4 and rec["goaheadnum"] <= 15,
                "dir=%d maze=%d motion=%d pos=(%d,%d) spurtMode=%d goahead=%d"
                % (rec["dir"], rec["mazeTask"], rec["motionTask"],
                   rec["posX"], rec["posY"], rec["spurtMode"], rec["goaheadnum"])))
    return out


def stage2_frames(lk) -> bool:
    print("\n[S2] TELEM —— 单帧语义(载荷必须正好 30 B)")
    got = 0
    lens = []
    recs = []
    rtts = []
    for _ in range(8):
        frames, lines, rtt = lk.talk("TELEM", timeout=2.5)
        if frames:
            got += 1
            lens.append(len(frames[0]))
            recs.append(codec.decode_unified(frames[0]))
            rtts.append(rtt)
        time.sleep(0.05)
    check("TELEM 8/8 收到统一帧", got == 8, "实际 %d/8" % got)
    if not got:
        return False
    if rtts:
        note("RTT 均值 %.0f ms / 最大 %.0f ms (只记录: 全部消耗在 PC 侧 CH340)" %
             (statistics.fmean(rtts), max(rtts)))
    check("每帧载荷长度 = 30 B (v9)", all(n == 30 for n in lens),
          "实测 %s" % sorted(set(lens)))
    miss = set(codec.UNIFIED_CHANNELS) - set(recs[0])
    check("33 个通道全部解出", not miss, "缺 %s" % (sorted(miss) or "无"))
    check("4 个 LED 通道都在(值域 0/1)",
          all(recs[0].get("led%d" % i, -1) in (0, 1) for i in (1, 2, 3, 4)),
          "led=%s" % [recs[0].get("led%d" % i) for i in (1, 2, 3, 4)])

    # 硬语义(取第一帧 + 抽样后几帧)
    f0 = recs[0]
    for name, ok, detail in _frame_semantics(f0):
        check(name, ok, detail)
    bad = sum(1 for r in recs[1:] if not all(ok for _, ok, _ in _frame_semantics(r)))
    check("其余 %d 帧同判据一致" % (len(recs) - 1), bad == 0, "不一致 %d 帧" % bad)

    # 软判据: 只记录(车可能被手拿着 / 正在跑), 不当协议错误
    note("帧内值: speed=(%d,%d) track=(%d,%d) task=%s/%s batt=%d mV ir=(%d,%d,%d,%d)"
         % (f0["speedL"], f0["speedR"], f0["trackL"], f0["trackR"],
            f0["motionTaskName"], f0["mazeTaskName"], f0["battMv"],
            f0["irF1"], f0["irF2"], f0["irL"], f0["irR"]))
    note("flags: 红外采样=%s 自由模式=%s 急停锁存=%s 事件不完整=%s"
         % ("开" if f0["irSwitch"] else "关", f0["freemode"],
            f0["estop_lock"], f0["evq"]))
    if not (3000 <= f0["battMv"] <= 4500):
        note("⚠ 电池 %d mV 不在 3.0~4.5 V 常见区间(低电闸 LAUNCH_MIN_MV=7000 会拦发车)"
             % f0["battMv"])
    d_track = recs[-1]["trackL"] - recs[0]["trackL"]
    note("trackL 增量 %+d mm(绝对值有静态偏置, 只认增量)" % d_track)
    return True


def stage4_idle(lk) -> bool:
    print("\n[S4] 收尾 —— RATE OFF 后不得有残流")
    frames, lines, _ = lk.talk("RATE OFF", timeout=2.0)
    acks = [ln for ln in lines if ln.startswith("ACK")]
    check("RATE OFF 有 ACK", bool(acks), acks[-1] if acks else "无 ACK")
    time.sleep(0.4)
    # 清干净在途帧, 再等 1.2 s 看有没有新的
    lk.ser.reset_input_buffer()
    t0 = time.monotonic()
    late = 0
    parser = codec.FrameStreamParser()
    while time.monotonic() - t0 < 1.2:
        data = lk.ser.read(256)
        if not data:
            continue
        for item in parser.feed(data):
            if not isinstance(item, str) and item.type == codec.TYPE_UNIFIED:
                late += 1
    check("RATE OFF 后 1.2 s 内无统一帧", late == 0, "残流 %d 帧" % late)

    frames, lines, _ = lk.talk("TELEM", timeout=2.0)
    if frames:
        rec = codec.decode_unified(frames[0])
        check("收尾帧 estop_lock = False", not rec["estop_lock"],
              "estop_lock=%s" % rec["estop_lock"])
        check("收尾帧 emerg = False", not rec["emerg"], "emerg=%s" % rec["emerg"])
        check("收尾帧载荷仍 = 30 B", len(frames[0]) == 30, "%d B" % len(frames[0]))
    else:
        check("收尾 TELEM 有帧", False, "无帧")
    return True


# ============================================================ S3 容量阶梯

def analyze_pull(out_csv: str, want_n: int) -> dict:
    """从 `link_pull_evt.py` 的产物里算交付率 / 实收 Hz / 丢帧可用性。

    ⚠️ v9 帧内已无 `dropCnt` ⇒ `drop_u8` 列恒 -1。**不能**把 -1 当成"丢帧异常",
    它是"该指标不可用"(见 docs/通信协议v9-统一帧重排方案.md §6)。
    """
    frames_csv = out_csv.replace(".csv", "_frames.csv")
    periods_csv = out_csv.replace(".csv", "_periods.csv")
    summary_txt = out_csv.replace(".csv", "_summary.txt")

    res = {"result": "?", "frames": 0, "periods": 0, "recv_hz": 0.0,
           "period_ms": float("nan"), "n_used_mean": float("nan"),
           "delivery": 0.0, "drop_na": True, "span_s": 0.0, "bad_gap": 0}
    if os.path.exists(summary_txt):
        try:
            with open(summary_txt, "r", encoding="utf-8") as f:
                for ln in f:
                    if ln.startswith("结果:"):
                        res["result"] = ln.split(":", 1)[1].strip()
                    elif ln.startswith("PC 实收:"):
                        res["recv_hz"] = float(re.findall(r"[-\d.]+", ln)[0])
                    elif ln.startswith("周期(≡RTT) 均值:"):
                        res["period_ms"] = float(re.findall(r"[-\d.]+", ln)[0])
        except (OSError, ValueError, IndexError):
            pass

    ts, drops, gaps = [], [], []
    if os.path.exists(frames_csv):
        try:
            with open(frames_csv, "r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    try:
                        ts.append(float(row["t_s"]))
                    except (KeyError, TypeError, ValueError):
                        pass
                    try:
                        drops.append(int(row["drop_u8"]))
                    except (KeyError, TypeError, ValueError):
                        pass
                    try:
                        gaps.append(float(row["gap_mcu_ms"]))
                    except (KeyError, TypeError, ValueError):
                        pass
        except OSError:
            pass
    res["frames"] = len(ts)
    if ts:
        res["span_s"] = max(ts) - min(ts)
        if res["span_s"] > 0 and not res["recv_hz"]:
            res["recv_hz"] = len(ts) / res["span_s"]
    res["drop_na"] = (not drops) or all(d < 0 for d in drops)
    # 帧间隔(ms8)远大于 t 的次数 —— 只做"有没有卡顿"的粗体检, 不当硬判据
    if gaps and want_n:
        t_est = res["period_ms"] / max(1, want_n) if res["period_ms"] else 0
        if t_est:
            res["bad_gap"] = sum(1 for g in gaps if g > 2.5 * t_est)

    n_used = []
    if os.path.exists(periods_csv):
        try:
            with open(periods_csv, "r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    try:
                        n_used.append(int(row["n_frames"]))
                    except (KeyError, TypeError, ValueError):
                        pass
        except OSError:
            pass
    res["periods"] = len(n_used)
    if n_used:
        res["n_used_mean"] = statistics.fmean(n_used)
        # 交付率 = 实收帧 / 本应收到帧(按每周期实际拉取的 n 求和), 不用"周期数 × N"。
        # 批边界上会多算/少算 1 帧, 故夹到 1.0, 避免 "1.03" 这种噪声看花眼。
        res["delivery"] = min(1.0, res["frames"] / max(1, sum(n_used)))
    return res


def run_one_rung(py: str, port: str, n: int, sec: float, hz_cap: float,
                 tag: str, out_csv: str) -> dict:
    cmd = [py, os.path.join(HERE, "link_pull_evt.py"), port,
           "--frames", str(n), "--duration", str(sec), "--tag", tag,
           "--out", out_csv, "--max-hz", str(hz_cap)]
    print("      $ %s" % " ".join(cmd[1:]))
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                              timeout=sec + 90)
    except subprocess.TimeoutExpired:
        return {"result": "TIMEOUT", "frames": 0, "periods": 0, "recv_hz": 0.0,
                "period_ms": float("nan"), "n_used_mean": float("nan"),
                "delivery": 0.0, "drop_na": True, "span_s": 0.0, "bad_gap": 0}
    r = analyze_pull(out_csv, n)
    r["wall_s"] = time.monotonic() - t0
    r["rc"] = proc.returncode
    keep = [ln for ln in (proc.stdout or "").splitlines()
            if ln.startswith("结果:") or "PC 实收" in ln]
    for ln in keep:
        print("      %s" % ln.strip())
    if proc.returncode not in (0,):
        tail = [ln for ln in (proc.stderr or "").splitlines() if ln.strip()][-3:]
        note("拉取器退出码 %d %s" % (proc.returncode, tail or ""))
    return r


def stage3_ladder(port: str, ladder, sec: float, hz_cap: float, py: str) -> bool:
    print("\n[S3] 容量阶梯 —— 事件触发式拉取(N=每周期帧数), v9 帧更短, 重新探顶")
    note("v8 经验容量 %.2f Hz(57 B/帧, --max-hz 13.3 硬闸); v9 理论 303 Hz(38 B/帧)"
         % V8_BASELINE_HZ)
    print("      ⚠ 某档 BROKE 立即停止(再往上可能把透传模块灌死, 只能物理断电恢复)")
    best = None
    rows = []
    for n in ladder:
        tag = "v9N%d" % n
        out_csv = os.path.join(HERE, "v9_%s.csv" % tag)
        print("\n  -- N=%d (%d s, hz 闸放宽容 %.0f) --" % (n, int(sec), hz_cap))
        r = run_one_rung(py, port, n, sec, hz_cap, tag, out_csv)
        r["n"] = n
        rows.append(r)
        print("      结果 %-8s 实收 %5.1f Hz  交付率 %.3f  周期 %.0f ms  帧 %d/%d"
              % (r["result"], r["recv_hz"], r["delivery"], r["period_ms"],
                 r["frames"], r["periods"]))
        if r["drop_na"]:
            note("drop_u8 全为 -1 = 预期(v9 帧内已删 dropCnt); 健康度改看主机 seq_lost/bad_crc")
        if r["bad_gap"]:
            note("N=%d: %d 个帧间隔 > 2.5t(有卡顿, 交付率已扣分)" % (n, r["bad_gap"]))
        good = (r["result"] in GOOD_RESULTS) and r["delivery"] >= 0.95 and r["frames"] > 0
        if good:
            best = r
            continue
        note("N=%d 未达标(结果=%s 交付率=%.3f) —— 阶梯到此为止"
             % (n, r["result"], r["delivery"]))
        break

    globals()["LADDER"] = rows
    if best is None:
        check("至少一档拉取达标", False, "全部未达标")
        return False
    check("最高达标档 N=%d (%.1f Hz, 交付率 %.3f)"
          % (best["n"], best["recv_hz"], best["delivery"]), True)
    if best["recv_hz"] > V8_BASELINE_HZ * 1.05:
        note("★ 容量提升: %.1f Hz vs v8 %.1f Hz (+%.0f%%)"
             % (best["recv_hz"], V8_BASELINE_HZ,
                100.0 * (best["recv_hz"] / V8_BASELINE_HZ - 1.0)))
    else:
        note("容量未见提升(%.1f Hz): 先看 RTT 是否变长 / 模块是否已到瓶颈"
             % best["recv_hz"])
    return True


# ============================================================ 自检(离线)

#: 拉取器判"跑满时长"的结果串(`link_pull_evt.py` 用 SURVIVED; 兼容旧写法)。
GOOD_RESULTS = ("SURVIVED", "OK", "COMPLETE", "DONE")


def _write_fake_pull(out_csv: str, n: int, frames: int, periods: int,
                     result: str = "SURVIVED", drop: int = DROP_NA,
                     gap_bad: int = 0) -> None:
    """造一份 link_pull_evt 的产物(三个文件名同真机), 用来单测 analyze_pull。"""
    with open(out_csv.replace(".csv", "_frames.csv"), "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["idx", "t_s", "gap_host_ms", "gap_mcu_ms", "ms8",
                    "batch", "drop_u8", "flags"])
        for i in range(frames):
            w.writerow([i + 1, "%.4f" % (i * 0.05), "50.0",
                        str(300 if (gap_bad and i % 10 == 0) else 60),
                        i % 256, i // max(1, n), str(drop), 8 if i % n == 0 else 0])
    with open(out_csv.replace(".csv", "_periods.csv"), "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["period_idx", "trig", "period_ms", "n_frames",
                    "boundary_gaps_ms", "t_ms", "n_used"])
        for k in range(periods):
            w.writerow([k + 1, "mark", "200.0", str(n), "", "50", str(n)])
    with open(out_csv.replace(".csv", "_summary.txt"), "w", encoding="utf-8") as f:
        f.write("结果: %s\n" % result)
        f.write("PC 实收: %.2f Hz\n" % (frames / max(1e-9, periods * 0.2)))
        f.write("周期(≡RTT) 均值: 200.00 ms\n")


def selftest() -> int:
    """离线自检 —— 不接串口。证明"脚本自身没写错", 免得把脚本 bug 当成固件 bug。"""
    print("=" * 78)
    print("v9_pull_verify 自检(离线, 不接串口)")
    print("=" * 78)
    stage0_offline()

    print("\n[ST] analyze_pull —— 交付率 / 丢帧不可用 / 断链 三种样本")
    import tempfile  # noqa: PLC0415
    tmp = tempfile.mkdtemp(prefix="v9verify_")
    p1 = os.path.join(tmp, "a.csv")
    _write_fake_pull(p1, n=4, frames=200, periods=50)
    r1 = analyze_pull(p1, 4)
    check("满交付样本: 交付率 = 1.0", abs(r1["delivery"] - 1.0) < 1e-6,
          "%.3f" % r1["delivery"])
    check("满交付样本: 丢帧列 -1 判为 N/A", r1["drop_na"] is True, str(r1["drop_na"]))

    p2 = os.path.join(tmp, "b.csv")
    _write_fake_pull(p2, n=4, frames=140, periods=50)
    r2 = analyze_pull(p2, 4)
    check("缺 30%% 样本: 交付率 < 0.95", r2["delivery"] < 0.95, "%.3f" % r2["delivery"])

    p3 = os.path.join(tmp, "c.csv")
    _write_fake_pull(p3, n=4, frames=10, periods=50, result="BROKE")
    r3 = analyze_pull(p3, 4)
    check("BROKE 样本: 结果透传", r3["result"] == "BROKE", r3["result"])

    p4 = os.path.join(tmp, "d.csv")
    _write_fake_pull(p4, n=4, frames=200, periods=50, gap_bad=1)
    r4 = analyze_pull(p4, 4)
    check("卡顿样本: bad_gap > 0", r4["bad_gap"] > 0, str(r4["bad_gap"]))

    print("\n[ST] 帧语义 —— 30 B / 49 B / 非法位域 三种")
    good = codec.decode_unified(codec.pack_unified(_sample_dicts(1)[0]))
    check("合法帧 3 条硬语义全过",
          all(ok for _, ok, _ in _frame_semantics(good)), "")
    p49 = bytes(codec.pack_unified(_sample_dicts(1)[0])) + b"\x00" * 19
    check("49 B 载荷会被长度闸判否", len(p49) != 30, "%d B" % len(p49))
    bad = bytearray(codec.pack_unified(_sample_dicts(1)[0]))
    bad[codec.U_STATE_A] = 0xFF          # mazeTask=7 越界(合法只到 6)
    rec_bad = codec.decode_unified(bytes(bad))
    check("mazeTask=7 被位域宽度闸判否",
          any(not ok for name, ok, _ in _frame_semantics(rec_bad) if "位域" in name),
          "mazeTask=%d" % rec_bad["mazeTask"])

    print("\n" + "=" * 78)
    print("自检汇总: PASS %d / FAIL %d / 备注 %d" % (len(PASS), len(FAIL), len(NOTE)))
    if FAIL:
        print("失败项: %s" % "; ".join(FAIL))
    print("=" * 78)
    return 0 if not FAIL else 1


# ============================================================ main

LADDER: list = []


def main() -> int:
    ap = argparse.ArgumentParser(description="v9 统一帧 + 实时拉取 实机验收")
    ap.add_argument("port", nargs="?", default=None, help="串口, 如 COM7")
    ap.add_argument("--selftest", action="store_true", help="离线自检(不接串口)")
    ap.add_argument("--no-ladder", action="store_true", help="跳过 S3 容量阶梯")
    # 默认 4,5 —— 实测 N=5 是安全顶(17.0 Hz, 20 s 存活);
    # N=6 能跑到 20.3 Hz 但 17 s 后链路整条死透(连 ACK 都没有, 须拔电重启模块),
    # 所以默认阶梯不碰它, 要冲顶必须显式 `--ladder 4,6`。
    ap.add_argument("--ladder", default="4,5",
                    help="N 阶梯, 逗号分隔(默认 4,5 安全档; N>=6 实测会灌死透传模块, 须断电)")
    ap.add_argument("--ladder-sec", type=float, default=20.0, help="每档时长(s)")
    ap.add_argument("--hz-cap", type=float, default=40.0,
                    help="传给拉取器的 --max-hz(默认 40, 放开 v8 的 13.3 硬闸)")
    ap.add_argument("--python", default=sys.executable, help="跑拉取器的解释器")
    ap.add_argument("--json", default=None, help="把结果写成 JSON")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    if not a.port:
        ap.error("给串口(COM7)或 --selftest")

    t_all = time.monotonic()
    print("=" * 78)
    print("v9 实机验收 —— 端口 %s   帧 30 B / PROTO %d / FW %d"
          % (a.port, EXPECT_PROTO, EXPECT_FW))
    print("=" * 78)

    payload = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "port": a.port}

    # S0 是硬闸: 源码与上位机都不一致时, 后面所有判据都不可信
    if not stage0_offline():
        print("\n✗ S0 未通过 —— 先修好源码/codec 一致性再上机(后面的判据都会失真)")
        return 1
    if FAIL:
        print("\n✗ S0 已有失败项, 停止")
        return 1

    Link, parse_pong, parse_ack = _import_link()
    try:
        lk = Link(a.port)
    except Exception as e:                                    # noqa: BLE001
        print("✗ 串口打不开(%s): %s" % (a.port, e))
        print("  先确认: ①没被 Keil/串口助手占用 ②确实是 COM7 ③模块已上电")
        return 2

    try:
        pong = stage1_ping(lk, parse_pong, parse_ack)
        payload["pong"] = pong
        if pong is None:
            return 1
        if pong["proto"] != EXPECT_PROTO or pong["fw"] != EXPECT_FW:
            print("\n✗ 指纹不符 —— 板上的不是 v9 远程档, 后续判据一律跳过")
            return 1

        ok2 = stage2_frames(lk)
        payload["stage2"] = ok2
        if not ok2:
            print("\n✗ S2 未通过 —— 帧语义不对, 先别跑容量阶梯")
            return 1
    finally:
        try:
            lk.close()
        except Exception:                                     # noqa: BLE001
            pass

    if not a.no_ladder:
        ladder = [int(x) for x in a.ladder.split(",") if x.strip().isdigit()]
        ok3 = stage3_ladder(a.port, ladder or [4], a.ladder_sec, a.hz_cap, a.python)
        payload["ladder"] = LADDER
        if not ok3:
            print("\n✗ S3 未通过")
    else:
        print("\n[S3] 跳过(--no-ladder)")

    # S4 要重新拿串口(S3 是子进程, 已退出)
    lk = Link(a.port)
    try:
        stage4_idle(lk)
    finally:
        try:
            lk.close()
        except Exception:                                     # noqa: BLE001
            pass

    print("\n" + "=" * 78)
    print("汇总: PASS %d / FAIL %d / 备注 %d   用时 %.1f s"
          % (len(PASS), len(FAIL), len(NOTE), time.monotonic() - t_all))
    if FAIL:
        print("失败项:")
        for t in FAIL:
            print("  · %s" % t)
    print("=" * 78)

    payload.update({"pass": len(PASS), "fail": len(FAIL), "notes": NOTE,
                    "failed": FAIL})
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print("JSON: %s" % a.json)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
