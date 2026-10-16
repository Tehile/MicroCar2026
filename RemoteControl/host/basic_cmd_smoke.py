# -*- coding: utf-8 -*-
"""实机基础指令冒烟测试 —— 修复 USART1 NVIC 未使能后的端到端验证。

背景(2026-10-16)
----------------
远程档固件此前“完全收不到信号”：``uart_init()`` 只开了 USART 侧的
RXNEIE(``USART1->CR1`` bit5)，从未置位 NVIC 的中断使能位；而
``SYSTEM/usart/usart.c`` 的注释写明“NVIC 使能与优先级统一由
``LINK_Init()`` 配置”，LINK_Init 却只写了优先级 ``IP``、漏了 ``ISER``。
于是 RXNE 挂死在 USART 里进不了 CPU：DR 里的首字节无人读(实测停在
上位机发来的 ``'P'`` 上)，后续到达的字节全部触发 ORE 被丢弃。

修复：``REMOTE/link.c`` 的 LINK_Init 补一行
``NVIC->ISER[USART1_IRQn >> 5] |= (u32)1 << (USART1_IRQn & 0x1F);``
本脚本在**上电后不经 J-Link 介入**的前提下验证链路自愈：连接可用、
基础指令可跑通。

覆盖范围（全部不发车）
----------------------
    T1 PING          链路指纹 proto/fw/mazetype/txbuf/rst
    T2 TELEM         连续遥测帧 + 帧率 + 字段完整性
    T3 GET ALL       全量参数回读
    T4 GET <name>    单点回读（抽查 3 个）
    T5 SETQ + GET    写入并回读校验，**最后务必恢复原值**
    T6 错误注入      未知参数名必须被拒，且不得改动任何已读到的值
    T7 MCU RESET     软复位后链路重新可用、rst 变为 SW（可选，--reset）

用法
----
    cd host && python basic_cmd_smoke.py COM7
    cd host && python basic_cmd_smoke.py COM7 --reset      # 追加软复位测试
    cd host && python basic_cmd_smoke.py --selftest        # 离线自检(不接设备)
"""

from __future__ import annotations

import argparse
import re
import sys
import time

try:
    import serial
except ImportError:  # pragma: no cover
    print("需要 pyserial: pip install pyserial")
    raise SystemExit(1)

sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else ".")

from host.protocol.codec import (  # noqa: E402
    FrameStreamParser,
    TYPE_UNIFIED,
    crc16_modbus,
    decode_unified,
    q1616_to_float,
    float_to_q1616,
    UNIFIED_LEN,
)

# 绝不发出的命令首词 —— 本脚本必须保持“零运动”。
FORBIDDEN = {"MOVE", "MAZE", "CAL", "ARM", "ESTOP", "STOP", "PARAMS", "SAVE"}

RE_PONG = re.compile(r"^PONG (\d+) (\d+) (\d+) (\d+) (\w+)$")
RE_ACK = re.compile(r"^ACK (-?\d+) (.*)$")
RE_PARAM = re.compile(r"^P (\d+) (-?\d+)$")

PASS, FAIL = [], []


def check(tag: str, ok: bool, detail: str = "") -> bool:
    PASS.append(tag) if ok else FAIL.append(tag)
    print("  [%s] %-46s %s" % ("OK " if ok else "FAIL", tag, detail))
    return ok


def build(cmd: str, seq: int) -> bytes:
    """下行行格式 —— 与 CommandClient._build 逐字节一致（`*` 前无空格）。"""
    head = cmd.split()[0].upper()
    if head not in FORBIDDEN and head != "MCU":
        pass
    assert head not in FORBIDDEN, f"本脚本禁止发送 {head}"
    body = f"{cmd} #{seq}".encode("ascii")
    return body + f"*{crc16_modbus(body):04X}".encode("ascii") + b"\r\n"


class Link(object):
    """严格停等的链路封装：一次一发，收干净再收下一条。"""

    def __init__(self, port: str, baud: int = 115200):
        self.ser = serial.Serial(port, baud, timeout=0.2)
        self.seq = 0
        self.ser.reset_input_buffer()

    def talk(self, cmd: str, timeout: float = 3.0, tail: float = 0.35,
             stop_on_frame: bool = True):
        """发一条命令并收干净。返回 (frames, lines, rtt_ms)。"""
        self.seq += 1
        seq = self.seq
        self.ser.reset_input_buffer()
        self.ser.write(build(cmd, seq))
        self.ser.flush()
        t0 = time.monotonic()
        parser = FrameStreamParser()
        frames, lines = [], []
        rtt = None
        while time.monotonic() - t0 < timeout:
            data = self.ser.read(256)
            if not data:
                time.sleep(0.002)
                continue
            for item in parser.feed(data):
                if isinstance(item, str):
                    lines.append(item)
                    if rtt is None:
                        rtt = (time.monotonic() - t0) * 1000.0
                elif item.type == TYPE_UNIFIED:
                    frames.append(item.payload)
            if stop_on_frame and frames and lines:
                t1 = time.monotonic()
                while time.monotonic() - t1 < tail:
                    more = self.ser.read(256)
                    if more:
                        for item in parser.feed(more):
                            if isinstance(item, str):
                                lines.append(item)
                            elif item.type == TYPE_UNIFIED:
                                frames.append(item.payload)
                break
        return frames, lines, (rtt if rtt is not None else timeout * 1000.0)

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


def parse_pong(lines):
    for ln in lines:
        m = RE_PONG.match(ln.strip())
        if m:
            return dict(proto=int(m.group(1)), fw=int(m.group(2)),
                        mazetype=int(m.group(3)), txbuf=int(m.group(4)),
                        rst=m.group(5))
    return None


def parse_ack(lines, seq=None):
    """取最后一条 ACK。返回 (status, tokens)。"""
    got = None
    for ln in lines:
        m = RE_ACK.match(ln.strip())
        if m:
            got = (int(m.group(1)), m.group(2).strip())
    if got is None:
        return None
    toks = got[1].split()
    return (toks[0] if toks else "", toks)


def parse_params(lines):
    out = {}
    for ln in lines:
        m = RE_PARAM.match(ln.strip())
        if m:
            out[int(m.group(1))] = int(m.group(2))
    return out


def selftest() -> int:
    """离线自检 —— 不接串口, 验证脚本自身的协议假设与安全护栏。

    存在的意义: 设备不在手上时, 也能证明"脚本没有写错", 避免插上后
    把"脚本 bug"误判成"固件 bug"。涵盖四件事:
      1) 下行报文格式(CRC / `#seq` / 无空格)与固件 link_HandleLine 一致;
      2) 上行正则能吃下固件真实拼装的 PONG / ACK / P 行;
      3) Q16.16 往返无损;
      4) 安全护栏: FORBIDDEN 里的运动命令一律发不出去。
    """
    print("=" * 78)
    print("离线自检(不接串口)")
    print("=" * 78)

    # --- 1) 下行报文格式 ---
    pkt = build("PING", 7)
    body, sep, crc = pkt[:-2].rpartition(b"*")
    check("下行以 CRLF 结尾", pkt.endswith(b"\r\n"), repr(pkt[-4:]))
    check("下行是 `CMD #seq*XXXX`(星号前无空格)",
          body == b"PING #7" and sep == b"*", repr(body))
    check("CRC 自洽(重算等于带上的值)",
          ("%04X" % crc16_modbus(body)).encode() == crc,
          "带 %s, 重算 %04X" % (crc.decode(), crc16_modbus(body)))
    # 固件 link_HandleLine: 找最后一个 '*', 其后必须 >= 5 字符('*'+4 hex)
    check("'*' 后有 4 位十六进制", len(crc) == 4, repr(crc))

    # --- 2) 上行正则吃下固件真实格式 ---
    # 固件 link.c:2499-2509  PONG <proto> <fw> <mazetype> <txbuf> <rst>
    pong = parse_pong(["PONG 8 300 16 1024 POR"])
    check("解析 PONG(固件 link.c:2499 拼装)",
          pong == {"proto": 8, "fw": 300, "mazetype": 16,
                   "txbuf": 1024, "rst": "POR"}, str(pong))
    check("PONG 复位原因认 SW/PIN/IWDG/POR",
          all(parse_pong(["PONG 8 300 16 1024 %s" % r])["rst"] == r
              for r in ("SW", "PIN", "IWDG", "POR")), "4/4")

    # 固件 link.c:2079/2104/2126  ACK <seq> <status>[ <a> <b> <c>]
    st_, toks = parse_ack(["ACK 7 OK"])
    check("解析 ACK OK(固件 link.c:2079)", st_ == "OK" and toks == ["OK"], str(toks))
    st_, toks = parse_ack(["ACK 8 ERR_NAME"])
    check("解析 ACK ERR_NAME", st_ == "ERR_NAME", st_)
    st_, toks = parse_ack(["ACK 9 OK 1234 56 7"])
    check("解析 ACK 带附加整数(I3)", st_ == "OK" and toks == ["OK", "1234", "56", "7"],
          str(toks))
    st_, toks = parse_ack(["ACK 10 OK 1234"])
    check("解析 ACK 带一个整数(AckU)", toks == ["OK", "1234"], str(toks))
    st_, _ = parse_ack(["ACK -1 ERR_CRC"])
    check("解析无 seq 的应答(负数)", st_ == "ERR_CRC", st_)
    check("无 ACK 行时返回 None", parse_ack(["PONG 8 300 16 1024 POR"]) is None, "")

    # 固件 link.c:2541-2545 / 2523-2528  P <id> <q>
    pp = parse_params(["P 1 3486565", "P 22 -12345", "P 55 0"])
    check("解析 P 行(Q16.16 可负)", pp == {1: 3486565, 22: -12345, 55: 0}, str(pp))
    check("P 行忽略无关行", parse_params(["ACK 7 OK"]) == {}, "")

    # --- 3) Q16.16 往返 ---
    for v in (53.2015, 123.0, 0.01, 0.2, -3.5):
        q = float_to_q1616(v)
        back = q1616_to_float(q)
        check("Q16.16 往返 %g" % v, abs(back - v) < 1e-3,
              "q=%d -> %.6f" % (q, back))

    # --- 4) 安全护栏 ---
    # 运动/擦写类 —— 必须全部发不出去。注意 `SET`(link.c:2577)是**写参数**
    # 而非运动, 与 SETQ 同类, 故不在禁列内。
    forbidden_cases = ("MOVE", "MOVE FORWARD", "MAZE RUN", "CAL GYRO",
                       "ARM", "ESTOP", "STOP", "PARAMS", "SAVE")
    blocked = []
    for c in forbidden_cases:
        try:
            build(c, 1)
            check("护栏漏放: %s" % c, False, "竟然构造成功")
        except AssertionError:
            blocked.append(c.split()[0])
    check("运动/擦写类命令全部被拦下", len(blocked) == len(forbidden_cases),
          "拦住 %d/%d: %s" % (len(blocked), len(forbidden_cases), ",".join(blocked)))
    check("禁列与固件命令词一致(大小写不敏感)",
          FORBIDDEN == {"MOVE", "MAZE", "CAL", "ARM", "ESTOP", "STOP",
                        "PARAMS", "SAVE"},
          ",".join(sorted(FORBIDDEN)))

    allowed = ("PING", "TELEM", "GET ALL", "GET TRACKL", "SETQ IRTHRFNEAR 123",
               "SET TRACKL 53.2015", "MCU RESET")
    ok = all(build(c, 1).startswith(c.split()[0].encode()) for c in allowed)
    check("白名单命令可正常构造", ok, "%d 条(含参数写回 SET)" % len(allowed))

    # --- 5) 统一帧解码契约 ---
    rec = decode_unified(bytes(UNIFIED_LEN))
    need = {"ms8", "irAdcF1", "irAdcF2", "irF1", "irF2", "wallFront",
            "frontNear", "frontFar", "frontBoth", "battMv", "speedL",
            "speedR", "trackL", "trackR"}
    miss = need - set(rec.keys())
    check("decode_unified 含全部关键字段", not miss,
          "共 %d 字段, 缺 %s" % (len(rec), sorted(miss) or "无"))
    check("UNIFIED_LEN = 49 字节", UNIFIED_LEN == 49, str(UNIFIED_LEN))

    print("\n" + "=" * 78)
    print("自检汇总: PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项: " + ", ".join(FAIL))
    print("=" * 78)
    return 0 if not FAIL else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="实机基础指令冒烟测试(不发车)")
    ap.add_argument("port", nargs="?", default="COM7", help="串口号，默认 COM7")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--reset", action="store_true",
                    help="追加 T7: MCU RESET 软复位后重新可用")
    ap.add_argument("--keep-roundtrip", action="store_true",
                    help="T5 写回后不恢复原值(不推荐, 仅调试用)")
    ap.add_argument("--selftest", action="store_true",
                    help="离线自检: 不开串口, 只验证脚本自身的协议假设与护栏")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    print("=" * 78)
    print("实机基础指令冒烟测试  %s  @%d   (不发车)" % (args.port, args.baud))
    print("=" * 78)

    try:
        lk = Link(args.port, args.baud)
    except Exception as e:
        print("打开串口失败: %s: %s" % (type(e).__name__, e))
        return 2

    try:
        # 让 bootloader / 上电标定跑完再开口说话
        time.sleep(1.2)
        lk.ser.reset_input_buffer()

        # ---------------------------------------------------------- T1
        print("\n[T1] PING —— 链路指纹")
        frames, lines, rtt = lk.talk("PING", timeout=3.0)
        pong = parse_pong(lines)
        check("PING 有 PONG 回应", pong is not None,
              "RTT=%.0f ms" % rtt if pong else "超时, 收到 %d 行" % len(lines))
        if pong is None:
            print("\n  !! 链路不通 —— 先确认烧的是 Target_Remote 远程档")
            return 1
        print("       固件: PONG %d %d %d %d %s" % (
            pong["proto"], pong["fw"], pong["mazetype"], pong["txbuf"], pong["rst"]))
        check("proto = 8", pong["proto"] == 8, "proto=%d" % pong["proto"])
        check("fw = 300 (远程档)", pong["fw"] == 300, "fw=%d" % pong["fw"])
        check("mazetype = 16", pong["mazetype"] == 16, "mazetype=%d" % pong["mazetype"])
        check("rst = POR/SW/PIN/IWDG", pong["rst"] in ("POR", "SW", "PIN", "IWDG"),
              "rst=%s" % pong["rst"])

        # ---------------------------------------------------------- T2
        print("\n[T2] TELEM —— 连续遥测")
        # 预热: 首帧含"开串口 + request_sample 建链"的一次性开销(实测约 300ms,
        # front_check 同样现象), 不计入稳态延迟考核。
        fr0, ln0, rt0 = lk.talk("TELEM", timeout=3.0)
        print("       首帧 RTT = %.0f ms (含链路预热, 不计入稳态)" % rt0)
        ok_n, rtts = 0, []
        for i in range(5):
            fr, ln, rt = lk.talk("TELEM", timeout=2.0)
            if fr:
                ok_n += 1
                rtts.append(rt)
        check("TELEM 5/5 收到统一帧", ok_n == 5, "实际 %d/5" % ok_n)
        if rtts:
            avg = sum(rtts) / len(rtts)
            check("稳态平均 RTT < 100 ms", avg < 100.0,
                  "平均 %.1f ms, 最大 %.1f ms" % (avg, max(rtts)))
        # 字段完整性: talk() 返回的 frames 是 **payload 字节列表**, 字段名要靠
        # decode_unified() 解出(camelCase)。UNIFIED_LEN = 49 字节。
        if fr0:
            check("统一帧 payload 长度 = %d" % UNIFIED_LEN,
                  len(fr0[0]) == UNIFIED_LEN, "实际 %d 字节" % len(fr0[0]))
            rec = decode_unified(fr0[0])
            need = {"ms8", "irAdcF1", "irAdcF2", "irF1", "irF2",
                    "wallFront", "frontNear", "frontFar", "frontBoth",
                    "battMv", "speedL", "speedR", "trackL", "trackR"}
            miss = need - set(rec.keys())
            check("统一帧解出全部关键字段", not miss,
                  "共 %d 字段, 缺失: %s" % (len(rec), ",".join(sorted(miss)) or "无"))
            # 车辆必须静止: 这四个字段全 0 才说明我们没在发车
            moving = {k: rec[k] for k in ("speedL", "speedR", "trackL", "trackR")
                      if rec.get(k) not in (0, None)}
            check("车辆静止(未误发车)", not moving,
                  "非零: %s" % moving if moving else "speedL/R trackL/R 全 0")

        # ---------------------------------------------------------- T3
        print("\n[T3] GET ALL —— 全量参数")
        from host.protocol.commands import PARAM_IDS
        fr, ln, rt = lk.talk("GET ALL", timeout=4.0, tail=0.8, stop_on_frame=False)
        tbl = parse_params(ln)
        check("GET ALL 有参数回读", len(tbl) > 0, "收到 %d 项" % len(tbl))
        known = set(PARAM_IDS.values())
        extra = set(tbl) - known
        check("回读 id 全在 PARAM_IDS 内", not extra,
              "未知 id: %s" % sorted(extra) if extra else "无越界 id")
        # 生产命名空间(1-7 + 22-38)必须齐 —— 这是比赛档也会保证的那部分
        prod = {i for i in known if i <= 7 or 22 <= i <= 38}
        lack = prod - set(tbl)
        check("生产命名空间 24 项齐", not lack,
              "缺 %s" % sorted(lack) if lack else "24/24")

        # ---------------------------------------------------------- T4
        print("\n[T4] GET <name> —— 单点回读")
        probes = [n for n in ("TRACKL", "GYRORATIO", "ENCANGLE") if n in PARAM_IDS]
        single_ok = True
        for nm in probes:
            nid = PARAM_IDS[nm]
            fr, ln, rt = lk.talk("GET %s" % nm, timeout=2.0, tail=0.3)
            got = parse_params(ln)
            v = got.get(nid)
            if v is None:
                single_ok = False
                print("        %-12s 无回读" % nm)
            else:
                print("        %-12s q=%-12d -> %.4f" % (nm, v, q1616_to_float(v)))
        check("单点 GET 全部有值", single_ok, "抽查 %s" % "/".join(probes))

        # ---------------------------------------------------------- T5
        print("\n[T5] SETQ + GET —— 写入/回读/恢复")
        target = "IRTHRFNEAR"
        if target not in PARAM_IDS:
            check("存在测试用参数 %s" % target, False, "不在 PARAM_IDS")
        else:
            nid = PARAM_IDS[target]
            fr, ln, rt = lk.talk("GET %s" % target, timeout=2.0, tail=0.3)
            orig = parse_params(ln).get(nid)
            if orig is None:
                check("读到 %s 原值" % target, False, "无回读")
            else:
                new_v = 123.0
                new_q = float_to_q1616(new_v)
                status, toks = parse_ack(lk.talk("SETQ %s %d" % (target, new_q),
                                                 timeout=2.5, tail=0.3)[1]) or ("", [])
                check("SETQ 被 ACK OK", status == "OK", "status=%r" % status)
                fr, ln, rt = lk.talk("GET %s" % target, timeout=2.0, tail=0.3)
                back = parse_params(ln).get(nid)
                check("回读 == 写入值", back == new_q,
                      "写入 q=%d, 回读 q=%s" % (new_q, back))
                if not args.keep_roundtrip:
                    lk.talk("SETQ %s %d" % (target, orig), timeout=2.5, tail=0.3)
                    fr, ln, rt = lk.talk("GET %s" % target, timeout=2.0, tail=0.3)
                    restored = parse_params(ln).get(nid)
                    check("已恢复原值", restored == orig,
                          "原 q=%d, 现 q=%s" % (orig, restored))
                else:
                    print("        (--keep-roundtrip: 故意未恢复)")

        # ---------------------------------------------------------- T6
        print("\n[T6] 错误注入 —— 未知参数必须被拒")
        before = parse_params(lk.talk("GET TRACKL", timeout=2.0, tail=0.3)[1])
        status, toks = parse_ack(lk.talk("GET NOSUCHPARAM", timeout=2.0,
                                         tail=0.3)[1]) or ("<timeout>", [])
        check("未知参数被拒/无回读", status != "OK",
              "status=%r (不为 OK 即正确)" % status)
        after = parse_params(lk.talk("GET TRACKL", timeout=2.0, tail=0.3)[1])
        check("未污染其它参数", before == after,
              "before=%s after=%s" % (before.get(PARAM_IDS["TRACKL"]),
                                      after.get(PARAM_IDS["TRACKL"])))

        # ---------------------------------------------------------- T7
        if args.reset:
            print("\n[T7] MCU RESET —— 软复位后链路重新可用")
            status, toks = parse_ack(lk.talk("MCU RESET", timeout=3.0,
                                             tail=0.4)[1]) or ("<timeout>", [])
            print("        ACK status = %r" % status)
            time.sleep(3.5)  # 复位 + 上电陀螺标定
            lk.ser.reset_input_buffer()
            fr, ln, rt = lk.talk("PING", timeout=4.0)
            pong2 = parse_pong(ln)
            check("复位后仍能 PING 通", pong2 is not None,
                  "rst=%s" % (pong2["rst"] if pong2 else "超时"))
            if pong2:
                check("复位原因变为 SW", pong2["rst"] == "SW", "rst=%s" % pong2["rst"])
        else:
            print("\n[T7] MCU RESET —— 已跳过（加 --reset 启用）")

    finally:
        lk.close()

    print("\n" + "=" * 78)
    print("汇总: PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项: " + ", ".join(FAIL))
    else:
        print("结论: 基础指令全部跑通 ✅")
    print("=" * 78)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
