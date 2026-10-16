# -*- coding: utf-8 -*-
"""实机取证采集器 —— **只读 / 不发车**，一次性尽可能多采数据。

定位与用途
----------
真机窗口有限（电池），而"改代码"应当发生在**证据齐备之后**。本脚本因此
只做一件事: 在尽量短的时间内把能拿到的**实测事实**全部落盘, 供后续离线分析。

与 `basic_cmd_smoke.py` 的分工
-----------------------------
    basic_cmd_smoke.py  判"协议与通信是否正确"(断言式, 失败即 FAIL)
    本文件              **采集**(记录式), 任何异常都记进报告而不中断

设计要点(都是踩过的坑)
--------------------
1. 接收条件**不能**写成"帧和 ACK 同时到" —— 统一帧与 ACK 行可能分批到达,
   等两者的脚本会静默超时, 表现为"设备无响应"。这里改成: 拿到任一有效
   回包后再给一个短宽限窗口(`settle`), 收干净就走。
2. 所有原始行/帧都保留, 出错时打印**最后一次请求 + 收到的全部字节**, 便于
   事后复现。
3. 任何单项失败都**不中断**整体采集, 一律记入 `errors[]`。

用法
----
    python ev_capture.py COM7                 # 默认采集套件
    python ev_capture.py COM7 --secs 20       # 遥测连续采样 20 s
    python ev_capture.py COM7 --out ev.json   # 指定输出(默认 ev_capture_<ts>.json)
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from host.protocol.codec import (  # noqa: E402
    FrameStreamParser,
    TYPE_UNIFIED,
    crc16_modbus,
    decode_unified,
    q1616_to_float,
    UNIFIED_LEN,
)

#: 本脚本**只读**: 任何可能让车动的命令都不发
READONLY = {
    "PING", "TELEM", "GET", "GETQ", "VERSION", "STATUS", "EVENTS",
}

#: **安全类**命令: 只会让车更停, 绝不可能让它动。允许用于解除 ERR_ESTOP
#: 锁存(固件 link.c:2696 的 STOP 清 linkEStop=0) —— 否则车会一直锁死,
#: 发车命令恒回 ERR_ESTOP, 什么也测不了。
SAFE = {"STOP", "TARE", "MCU"}

#: 绝不允许出现在取证脚本里(会真的让车跑)
NEVER = {"MOVE", "MAZE", "ARM", "ESTOP", "CAL", "PARAMS", "SAVE", "RESET"}


class Probe:
    """一次性把设备状态抓下来。绝不发运动命令。"""

    def __init__(self, port: str, baud: int = 115200, timeout: float = 0.05):
        import serial
        self.ser = serial.Serial(port, baud, timeout=timeout)
        self.seq = 1000
        self.errors = []
        self.log = []

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    # ---------------------------------------------------------------- 底层
    def _raw(self, cmd: str, timeout: float, settle: float):
        """发一条命令并把这一批收干净。返回 (lines, frames, rtt_ms)。"""
        self.seq += 1
        seq = self.seq
        body = f"{cmd} #{seq}".encode()
        pkt = body + f"*{crc16_modbus(body):04X}".encode() + b"\r\n"
        head = cmd.split()[0].upper()
        assert head not in NEVER, f"取证脚本绝不发送 {head}(会让车动作)"
        assert head in READONLY or head in SAFE, f"非白名单命令 {head}"

        self.ser.reset_input_buffer()
        self.ser.write(pkt)
        self.ser.flush()
        t0 = time.monotonic()
        parser = FrameStreamParser()
        lines, frames = [], []
        # 阶段一: 收到"第一个有效回包"或超时
        while time.monotonic() - t0 < timeout:
            data = self.ser.read(256)
            if not data:
                continue
            for item in parser.feed(data):
                if isinstance(item, str):
                    lines.append(item)
                elif item.type == TYPE_UNIFIED:
                    frames.append(item.payload)
            if lines or frames:
                break
        # 阶段二: 宽限窗口, 把同批的 ACK / 事件行收干净
        if lines or frames:
            t1 = time.monotonic()
            while time.monotonic() - t1 < settle:
                data = self.ser.read(256)
                if not data:
                    continue
                for item in parser.feed(data):
                    if isinstance(item, str):
                        lines.append(item)
                    elif item.type == TYPE_UNIFIED:
                        frames.append(item.payload)
        return lines, frames, (time.monotonic() - t0) * 1000.0

    def cmd(self, cmd: str, timeout: float = 2.5, settle: float = 0.25,
            allow_safe: bool = False):
        """返回 (ack_status, frames, lines, rtt_ms)。ack_status 为 None 表示无 ACK。"""
        if allow_safe:
            head = cmd.split()[0].upper()
            assert head in SAFE, f"allow_safe 只放行 SAFE, 不放行 {head}"
            self.seq += 1
            seq = self.seq
            body = f"{cmd} #{seq}".encode()
            pkt = body + f"*{crc16_modbus(body):04X}".encode() + b"\r\n"
            self.ser.reset_input_buffer()
            self.ser.write(pkt)
            self.ser.flush()
            t0 = time.monotonic()
            lines, frames = [], []
            while time.monotonic() - t0 < timeout:
                data = self.ser.read(256)
                if not data:
                    continue
                for item in FrameStreamParser().feed(data):
                    if isinstance(item, str):
                        lines.append(item)
                if any(l.strip().startswith("ACK") for l in lines):
                    break
            rtt = (time.monotonic() - t0) * 1000.0
        else:
            lines, frames, rtt = self._raw(cmd, timeout, settle)
        ack = None
        for ln in lines:
            ln = ln.strip()
            if ln.startswith("ACK "):
                parts = ln.split()
                ack = parts[2] if len(parts) > 2 else ""
        return ack, frames, lines, rtt

    def telemetry(self):
        """取一帧统一帧并解码。拿不到返回 None。"""
        _ack, frames, lines, _rtt = self.cmd("TELEM", timeout=2.5)
        if not frames:
            return None, lines
        try:
            return decode_unified(frames[0]), lines
        except Exception as e:
            self.errors.append("decode_unified 失败: %s: %s" % (type(e).__name__, e))
            return None, lines

    def params(self):
        """`GET ALL` -> {name: float}。"""
        from host.protocol.commands import PARAM_IDS, PARAM_META
        _ack, _frames, lines, _rtt = self.cmd("GET ALL", timeout=5.0, settle=1.2)
        out = {}
        rev = {v: k for k, v in PARAM_IDS.items()}
        for ln in lines:
            parts = ln.strip().split()
            if len(parts) == 3 and parts[0] == "P":
                try:
                    q = int(parts[2])
                except ValueError:
                    continue
                nm = rev.get(int(parts[1]))
                if nm:
                    dec = PARAM_META.get(nm, ("", 4, ""))[1]
                    out[nm] = round(q1616_to_float(q), dec)
        return out, lines


def main() -> int:
    ap = argparse.ArgumentParser(description="实机只读取证采集(不发车)")
    ap.add_argument("port", nargs="?", default="COM7")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--secs", type=float, default=10.0, help="遥测连续采样秒数")
    ap.add_argument("--out", default="", help="输出 json 路径")
    ap.add_argument("--label", default="", help="本次采集标签")
    args = ap.parse_args()

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = Path(args.out or ("ev_capture_%s.json" % ts))

    print("=" * 78)
    print("实机只读取证采集  %s @%d  标签=%s" % (args.port, args.baud, args.label or "-"))
    print("=" * 78)

    try:
        pr = Probe(args.port, args.baud)
    except Exception as e:
        print("打开串口失败: %s: %s" % (type(e).__name__, e))
        return 2

    rep = {
        "ts": ts, "port": args.port, "baud": args.baud, "label": args.label,
        "probe": {}, "telemetry_stream": {}, "params": {},
        "commands": {}, "errors": pr.errors, "notes": [],
    }

    try:
        # ---------------------------------------------------------- 1 指纹
        print("\n[1] 设备指纹")
        ack, _f, lines, rtt = pr.cmd("PING", timeout=3.0)
        pong = next((l.strip() for l in lines if l.strip().startswith("PONG")), None)
        rep["probe"]["ping_raw"] = lines
        rep["probe"]["ping_rtt_ms"] = round(rtt, 1)
        print("    PONG: %s" % (pong or "<无>"))
        print("    ACK=%s  RTT=%.0f ms" % (ack, rtt))
        if not pong:
            pr.errors.append("PING 无 PONG: lines=%r" % (lines,))
        parts = (pong or "").split()
        if len(parts) == 6:
            rep["probe"].update(proto=int(parts[1]), fw=int(parts[2]),
                                mazetype=int(parts[3]), txbuf=int(parts[4]),
                                rst=parts[5])

        # ---------------------------------------------------------- 2 单帧全字段
        print("\n[2] 统一帧全字段快照")
        rec, _lines = pr.telemetry()
        if rec:
            rep["probe"]["frame_fields"] = {k: rec[k] for k in sorted(rec)}
            nz = {k: v for k, v in rec.items() if v not in (0, None, "", [])}
            print("    字段 %d 个, 非零 %d 个: %s" % (len(rec), len(nz), nz))
        else:
            pr.errors.append("TELEM 未取到统一帧")

        # ---------------------------------------------------------- 3 遥测连续采样
        print("\n[3] 遥测连续采样 %.0f 秒" % args.secs)
        rtts, ms8s, tracks, speeds = [], [], [], []
        t_end = time.monotonic() + args.secs
        n_ok = n_miss = 0
        miss_detail = []
        while time.monotonic() < t_end:
            _ack, frames, _lines, rtt = pr.cmd("TELEM", timeout=2.0, settle=0.06)
            if not frames:
                n_miss += 1
                if len(miss_detail) < 5:
                    miss_detail.append(round(rtt, 1))
                continue
            n_ok += 1
            rtts.append(rtt)
            try:
                d = decode_unified(frames[0])
                ms8s.append(d["ms8"])
                tracks.append((d["trackL"], d["trackR"]))
                speeds.append((d["speedL"], d["speedR"]))
            except Exception as e:
                pr.errors.append("采样解码失败: %s" % e)
        print("    成功 %d 帧 / 失败 %d 帧" % (n_ok, n_miss))
        if rtts:
            print("    RTT 平均 %.0f ms 中位 %.0f ms 最大 %.0f ms"
                  % (statistics.mean(rtts), statistics.median(rtts), max(rtts)))
        if ms8s:
            uniq = sorted(set(ms8s))
            print("    ms8 样本 %d 个, 不同值 %d 个, 范围 [%d, %d]"
                  % (len(ms8s), len(uniq), min(ms8s), max(ms8s)))
            if len(uniq) > 1:
                deltas = [ms8s[i + 1] - ms8s[i] for i in range(len(ms8s) - 1)]
                print("    ms8 逐帧差: %s" % sorted(set(deltas))[:8])
        if tracks:
            tl = [t[0] for t in tracks]
            print("    trackL 范围 [%d, %d] 唯一值 %s"
                  % (min(tl), max(tl), sorted(set(tl))[:6]))
        rep["telemetry_stream"] = {
            "ok": n_ok, "miss": n_miss, "miss_rtts": miss_detail,
            "rtt_ms": {"avg": round(statistics.mean(rtts), 1) if rtts else None,
                       "median": round(statistics.median(rtts), 1) if rtts else None,
                       "max": round(max(rtts), 1) if rtts else None},
            "ms8_samples": ms8s[:60],
            "trackL": sorted(set(t[0] for t in tracks))[:20] if tracks else [],
            "speed_nonzero": [s for s in speeds if s != (0, 0)][:5],
        }

        # ---------------------------------------------------------- 4 只读命令矩阵
        print("\n[4] 只读命令矩阵")
        cmds = ["PING", "TELEM", "GET ALL", "GET TRACKL", "GET NOSUCH",
                "VERSION", "STATUS", "EVENTS", "GETQ TRACKL", "BOGUSCMD"]
        for c in cmds:
            try:
                ack, frames, lines, rtt = pr.cmd(c, timeout=2.5)
                rep["commands"][c] = {
                    "ack": ack, "frames": len(frames),
                    "lines": [l.strip() for l in lines][:6],
                    "rtt_ms": round(rtt, 1),
                }
                print("    %-16s ACK=%-12s 帧=%d  %.0fms  %s"
                      % (c, ack, len(frames), rtt,
                         (lines[0].strip()[:44] if lines else "")))
            except AssertionError:
                print("    %-16s [被只读护栏拦下]" % c)
            except Exception as e:
                pr.errors.append("命令 %s 异常: %s: %s" % (c, type(e).__name__, e))
                print("    %-16s 异常 %s: %s" % (c, type(e).__name__, e))

        # ---------------------------------------------------------- 5 参数表
        print("\n[5] 参数表全量")
        tbl, plines = pr.params()
        rep["params"] = tbl
        print("    收到 %d 项" % len(tbl))
        if tbl:
            for k in sorted(tbl):
                print("      %-12s %s" % (k, tbl[k]))
        else:
            pr.errors.append("GET ALL 未解析出参数")

    finally:
        pr.close()

    rep["errors"] = pr.errors
    out_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 78)
    print("采集完成 -> %s" % out_path)
    if pr.errors:
        print("记录到 %d 条异常(已写入报告, 未中断采集):" % len(pr.errors))
        for e in pr.errors:
            print("   ! %s" % e)
    else:
        print("无异常")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())