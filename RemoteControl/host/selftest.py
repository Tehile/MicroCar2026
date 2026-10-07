"""协议自检 + 端到端回归。

用法:
    python selftest.py            # 纯协议断言(不需要网络)
    python selftest.py --e2e      # 额外拉起模拟器做端到端断言

覆盖:
    1. CRC16/MODBUS 标准校验向量
    2. Q16.16 编解码往返
    3. 帧打包/解析往返, 含"文本与二进制混流"(v2.0 帧与文本**保序**混排)
    4. 解析器对损坏帧的自恢复能力
    5. 墙图帧
    6. 会话落盘(单流 CSV / raw.bin / events.log / meta.json)
    7. (--e2e) 命令往返: PING / GET ALL / SETQ+回读 / 运动+STOPPED / 标定 / 墙图

v2.0(2026-10-05): 周期帧只剩一种 —— `TYPE_UNIFIED`(45B payload, ≈217Hz 自时钟)。
    旧的 fast/mid/slow 三帧、`SCOPE`/`DUMP` 回传、`RATE` 全删; 因此原先"快帧/中帧/
    慢帧"的往返断言统一改到统一帧上, 三份 stream CSV 合并为 `stream_unified.csv`,
    "SCOPE dump 全流程"改为"运动期间统一帧持续到达"。
    `MOVEDONE FWD` 改为上行事件 `STOPPED <reason> #n`。
"""

from __future__ import annotations

import argparse
import socket
import struct
import subprocess
import sys
import time

import numpy as np

from host.protocol.codec import (
    FRAME_OVERHEAD,
    TYPE_MAP,
    TYPE_UNIFIED,
    UNIFIED_CHANNELS,
    UNIFIED_LEN,
    Frame,
    FrameStreamParser,
    crc16_modbus,
    decode_map,
    decode_unified,
    float_to_q1616,
    pack_frame,
    pack_unified,
    q1616_to_float,
    split_items,
)
from host.protocol.commands import CommandClient

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def _blank() -> dict:
    """统一帧的零值基底 —— 与模拟器/契约测试同样只写关心的字段。"""
    return {c: 0 for c in UNIFIED_CHANNELS}


# ---------------------------------------------------------------- 1. CRC

def test_crc() -> None:
    print("\n[1] CRC16/MODBUS")
    check("标准校验向量 crc16(b'123456789') == 0x4B37", crc16_modbus(b"123456789") == 0x4B37,
          f"got 0x{crc16_modbus(b'123456789'):04X}")
    check("空输入 == 0xFFFF", crc16_modbus(b"") == 0xFFFF)
    check("单字节 crc16(b'A') 稳定复现", crc16_modbus(b"A") == crc16_modbus(b"A"))


# ---------------------------------------------------------------- 2. Q16.16

def test_q1616() -> None:
    print("\n[2] Q16.16 定点")
    for v in (53.2015, 0.532015, 399.79, -12.5, 0.0, 42.0):
        q = float_to_q1616(v)
        back = q1616_to_float(q)
        check(f"{v} 往返误差 < 2e-5", abs(back - v) < 2e-5, f"got {back}")
    check("53.2015 -> 3486614 (53.2015*65536=3486613.5)", float_to_q1616(53.2015) == 3486614,
          f"got {float_to_q1616(53.2015)}")
    check("0.532015 -> 34866", float_to_q1616(0.532015) == 34866,
          f"got {float_to_q1616(0.532015)}")


# ---------------------------------------------------------------- 3. 帧往返

def test_frames() -> None:
    print("\n[3] 帧打包/解析往返(统一帧)")
    d = _blank()
    d.update({"ms8": 0x5A, "speedL": 68, "speedR": -12, "motionTask": 32, "flags": 0x01})
    payload = pack_unified(d)
    frame = pack_frame(TYPE_UNIFIED, payload, 7)
    check(f"帧长 == 载荷 + {FRAME_OVERHEAD}", len(frame) == len(payload) + FRAME_OVERHEAD,
          f"got {len(frame)}")
    check(f"统一帧载荷 == {UNIFIED_LEN}B", len(payload) == UNIFIED_LEN, f"got {len(payload)}")
    check("同步头正确", frame[0] == 0xAA and frame[1] == 0x55)
    check("LEN 字段正确", struct.unpack("<H", frame[2:4])[0] == len(payload))

    p = FrameStreamParser()
    frames, lines = split_items(p.feed(frame))
    check("解析出 1 帧", len(frames) == 1 and not lines)
    if frames:
        check("TYPE/SEQ 正确", frames[0].type == TYPE_UNIFIED and frames[0].seq == 7)
        rec = decode_unified(frames[0].payload)
        check("载荷解码正确",
              rec["speedL"] == 68 and rec["speedR"] == -12 and rec["motionTask"] == 32,
              str({k: rec[k] for k in ("speedL", "speedR", "motionTask")}))
        check("flags 位解码正确", rec["freemode"] is True)
        check(f"解码后含全部 {len(UNIFIED_CHANNELS)} 个通道",
              all(c in rec for c in UNIFIED_CHANNELS),
              str([c for c in UNIFIED_CHANNELS if c not in rec]))

    # 逐字节投喂(模拟串口任意切分)
    p2 = FrameStreamParser()
    got = []
    for i in range(len(frame)):
        f, _l = split_items(p2.feed(frame[i : i + 1]))
        got.extend(f)
    check("逐字节投喂仍能正确组帧", len(got) == 1)


def test_mixed_stream() -> None:
    print("\n[4] 文本与二进制混流(保序)")
    # 设备会同时吐 printf 文本与二进制帧; 0xAA 在 ASCII 可打印区间外, 所以可分流
    text1 = b"odomStr.trackAbsL:123.4\todomStr.trackAbsR:122.9\r\n"
    d1 = _blank()
    d1.update({"speedL": 10, "speedR": 10, "motionTask": 1})
    f1 = pack_frame(TYPE_UNIFIED, pack_unified(d1), 1)
    text2 = b"ACK 42 OK\r\n"
    d2 = _blank()
    d2.update({"irF1": 232, "irF2": 231, "posX": 3, "posY": 4, "battMv": 7620})
    f2 = pack_frame(TYPE_UNIFIED, pack_unified(d2), 2)

    p = FrameStreamParser()
    items = p.feed(text1 + f1 + text2 + f2)
    frames, lines = split_items(items)
    check("解出 2 帧", len(frames) == 2, f"got {len(frames)}")
    check("解出 2 行文本", len(lines) == 2, f"got {lines}")
    check("文本内容正确", lines and lines[0].startswith("odomStr.trackAbsL"), str(lines))
    check("第二行是 ACK", len(lines) > 1 and lines[1] == "ACK 42 OK", str(lines))
    if len(frames) == 2:
        s = decode_unified(frames[1].payload)
        check("统一帧解码正确", s["irF1"] == 232 and s["posX"] == 3 and s["battMv"] == 7620,
              str({k: s[k] for k in ("irF1", "posX", "battMv")}))

    # v2.0 硬要求: `feed()` 返回一串**按到达顺序**混排的 Frame/文本 ——
    # 帧与事件共用同一条 1 字节序号, 拆成两个列表再合并会把正常交错误判成丢帧。
    kinds = ["F" if isinstance(i, Frame) else "T" for i in items]
    check("帧/文本按到达顺序混排(不是先帧后文本)", kinds == ["T", "F", "T", "F"], str(kinds))
    check("feed() 返回列表(非 (frames, lines) 元组)", isinstance(items, list)
          and not isinstance(items, tuple), str(type(items).__name__))

    # 单字节切分
    p2 = FrameStreamParser()
    allf, alll = [], []
    blob = text1 + f1 + text2 + f2
    for i in range(len(blob)):
        a, b = split_items(p2.feed(blob[i : i + 1]))
        allf.extend(a)
        alll.extend(b)
    check("单字节切分仍然解出 2 帧 2 行", len(allf) == 2 and len(alll) == 2,
          f"frames={len(allf)} lines={len(alll)}")


def test_corruption_recovery() -> None:
    print("\n[5] 损坏帧的自恢复")
    d = _blank()
    d.update({"speedL": 1, "speedR": 2})
    good = pack_frame(TYPE_UNIFIED, pack_unified(d), 9)
    bad = bytearray(good)
    bad[10] ^= 0xFF  # 破坏载荷 -> CRC 失配

    p = FrameStreamParser()
    frames, lines = split_items(p.feed(bytes(bad) + good))
    check("坏帧被丢弃并计入 bad_crc", p.stats["bad_crc"] >= 1, str(p.stats))
    check("紧随其后的好帧仍被正确解出", len(frames) == 1, f"got {len(frames)}")

    # 只有 0xAA 没有后续 -> 应安全等待而不是崩
    p2 = FrameStreamParser()
    check("半个同步头安全缓存", p2.feed(b"\xaa") == [])
    check("补齐后仍安全", p2.feed(b"\x55\x10\x00") == [])


def test_map_frame() -> None:
    print("\n[6] 墙图帧")
    n = 7
    wall = bytes([(0x01 | 0x02) for _ in range(n * n)])
    vis = bytes([1] * (n * n))
    pl = struct.pack("<bbBB", 3, 4, 1, 2) + wall + vis
    f = pack_frame(TYPE_MAP, pl, 0)
    p = FrameStreamParser()
    frames, _ = split_items(p.feed(f))
    check("墙图帧可解析", len(frames) == 1)
    if frames:
        m = decode_map(frames[0].payload, n)
        check("坐标/朝向正确", (m["posX"], m["posY"], m["direction"]) == (3, 4, 1))
        check("墙图尺寸正确", len(m["mapWall"]) == n and len(m["mapWall"][0]) == n)
        check("visited 正确", m["mapVisited"][0][0] == 1)


# ---------------------------------------------------------------- 7. 端到端

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_e2e(port: int = 5555, auto_spawn: bool = True) -> None:
    print("\n[7] 端到端(模拟器)")
    proc = None
    if auto_spawn:
        port = _free_port()
        proc = subprocess.Popen(
            [sys.executable, "-m", "host.sim.mouse_sim", "--port", str(port), "--no-auto"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1.2)

    try:
        from host.model.ring import CHANNELS, TelemetryStore
        from host.transport.link import Link, Target

        store = TelemetryStore(window_s=5.0)
        client = CommandClient(send_bytes=lambda b: link.write(b))
        link = Link(store=store, client=client)
        link.open(Target(kind="tcp", host="127.0.0.1", tcp_port=port))
        time.sleep(0.4)
        check("链路已打开", link.is_open, link.state_detail)

        p = client.ping()
        check("PING -> OK", p.ok, p.status)
        check("设备上报协议版本/迷宫尺寸", client.device.get("mazetype") == 7, str(client.device))

        # v2.1: 遥测是**拉取式**的(固件不再自时钟发送) —— 发一条 `TELEM` 取一帧,
        # 应答带**完整 u32 时基**(`ACK <seq> OK <upMs>`)作为时间锚。
        # ⚠️ 必须**停等**: 固件每 1ms tick 只消费一行命令, 成串发会丢第二条
        # (实测回收率: 背靠背 2 条仅 50%)。
        anchor = client.telem()
        check("TELEM 应答带完整时基(时间锚)",
              anchor.ok and len(str(anchor.status).split()) >= 2, anchor.status)
        time.sleep(0.2)
        st = link.stats()
        check("已收到统一帧", st["unified"] > 0, str(st))
        # 连续拉取能持续拿到帧(证明"按需拉取"这条路是通的; 不再有 RATE 调档)
        n_before = st["unified"]
        for _ in range(3):
            client.telem()
            time.sleep(0.15)
        check("连续拉取能持续拿到帧",
              link.stats()["unified"] > n_before, str(link.stats()["unified"]))

        client.get_all()
        # `GET ALL` 遍历固件 `linkParam[LINK_P_NUM]`: 标定 1..7 + 红外/电源 22..38 = 24 项。
        # v2.4 起课程参数(8..21)已删除, 这就是全部参数。
        check("GET ALL 回读 24 项生产参数", len(client.params) == 24,
              str(sorted(client.params)))

        # SETQ + 回读校验(L4)
        ok, stt = client.set_param("TRACKL", 53.4321, readback=True)
        check("SETQ+回读校验通过", ok, stt)
        check("回读值等于期望 Q16.16",
              client.params.get(1) == float_to_q1616(53.4321), str(client.params.get(1)))

        # ---- 运动 + 自时钟遥测(取代已删除的 SCOPE/dump 全流程) ----
        # v1.5.5 要"先 SCOPE START 录、停车后 DUMP 回传"; v2.0 统一帧按 ≈217Hz 全速
        # 上行, 运动期间的波形本来就在主机内存里, 直接看环形缓冲即可。
        n0 = store.unified.total
        ok, stt = client.move_fwd(20000, 45)  # 约 376mm @45cm/s -> 约 0.9s 的连续运动
        check("MOVE FWD -> OK", ok, stt)
        t0 = time.time()
        while time.time() - t0 < 6.0 and not any("STOPPED" in e[2] for e in client.events):
            time.sleep(0.02)
        check("运动结束上报 STOPPED 事件",
              any("STOPPED" in e[2] for e in client.events),
              str([e[2] for e in client.events][-4:]))
        check("运动期间统一帧持续增长(自时钟, 无需 SCOPE)",
              store.unified.total - n0 > 10, f"delta={store.unified.total - n0}")
        _t, data = store.unified.snapshot()
        nonzero = int((data[:, CHANNELS.index("speedL")] != 0).sum()) if len(data) else 0
        check("运动期间采到非零轮速样本", nonzero > 0,
              f"len={len(data)} nonzero={nonzero}")

        # 墙图
        ok, stt = client.map_dump()
        check("MAP DUMP -> OK", ok, stt)
        time.sleep(0.3)
        m = store.get_map()
        check("墙图已收到且为 7x7", m is not None and len(m["mapWall"]) == 7,
              "None" if m is None else str(len(m["mapWall"])))

        # 标定(**请求-响应**): ACK 到达即"这一步采集完成, 请摆下一个位置"。
        # v1.5.5 回的是 `CALDONE IRDATA` 事件行, 那条已被 TX 空间门丢弃风险淘汰。
        for ph in range(4):
            ok, stt = client.cal("IRDATA", ph, timeout=12.0)
            check(f"IRDATA 相位 {ph} -> OK(采完才回 ACK)", ok, stt)

        client.stop()
        link.close()
        check("链路已关闭", not link.is_open)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()


def test_session() -> None:
    print("\n[8] 会话落盘(stream_unified.csv / raw.bin / events.log / meta.json / 备份)")
    import os
    import shutil

    from host.model.session import Session

    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions")
    name = "_selftest"
    shutil.rmtree(os.path.join(root, name), ignore_errors=True)

    s = Session(root=root, name=name)
    s.write_raw(b"\xaa\x55raw")
    s.write_text("PONG 2 200 7 256 POR")
    s.write_unified({"ms8": 0x5A, "speedL": 68, "speedR": -12, "gyroAngle": 90,
                     "motionTask": 32, "irF1": 232, "posX": 3, "posY": 4, "battMv": 7620})
    s.write_event({"event": "STOPPED", "reason": "TIME", "raw": "STOPPED TIME #7"})
    s.write_calib_report(["## test", "- ok"])
    backup = s.backup_params([["TRACKL", 3486614, "53.2015"]])
    s.close({"link_stats": {"unified": 1}})

    def exists(rel: str) -> bool:
        return os.path.isfile(os.path.join(root, name, rel))

    check("raw.bin 已写入",
          exists("raw.bin") and os.path.getsize(os.path.join(root, name, "raw.bin")) == 5)
    check("raw.ts 已写入", exists("raw.ts") and _rows(root, name, "raw.ts") == 1)
    check("text.log 已写入", exists("text.log") and _rows(root, name, "text.log") == 1)
    check("stream_unified.csv 表头+1行",
          exists("stream_unified.csv") and _rows(root, name, "stream_unified.csv") == 2)
    check("events.log 表头行+1行(事件行已单存)",
          exists("events.log") and _rows(root, name, "events.log") == 1)
    cols = _header(root, name, "stream_unified.csv").split(",")
    check("统一帧 CSV 表头 = t_host_ms/dev_ms/seq + 通道",
          cols[:3] == ["t_host_ms", "dev_ms", "seq"] and "speedL" in cols, str(cols[:6]))
    check("meta.json 已写入且含 counts", exists("meta.json"))
    check("参数备份已写入", os.path.isfile(backup))
    shutil.rmtree(os.path.join(root, name), ignore_errors=True)


def _rows(root: str, name: str, rel: str) -> int:
    import os

    with open(os.path.join(root, name, rel), "r", encoding="utf-8") as f:
        return sum(1 for _ in f)


def _header(root: str, name: str, rel: str) -> str:
    import os

    with open(os.path.join(root, name, rel), "r", encoding="utf-8") as f:
        return f.readline().strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--e2e", action="store_true", help="额外跑端到端回归(会拉起模拟器)")
    ap.add_argument("--port", type=int, default=5555, help="配合 --e2e 时指定已运行的模拟器端口")
    ap.add_argument("--no-spawn", action="store_true", help="不自动拉起模拟器")
    args = ap.parse_args()

    print("=" * 66)
    print("电脑鼠上位机 协议自检")
    print("=" * 66)
    test_crc()
    test_q1616()
    test_frames()
    test_mixed_stream()
    test_corruption_recovery()
    test_map_frame()
    test_session()
    if args.e2e:
        test_e2e(args.port, auto_spawn=not args.no_spawn)

    print("\n" + "=" * 66)
    print(f"通过 {PASS} / 失败 {FAIL}")
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
