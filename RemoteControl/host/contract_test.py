"""注册表契约测试 —— 扩展性保证(E1/E3)的机器强制。

回答一个问题: **"新增一项功能, 接口有没有自动覆盖?"**

静态断言(不连设备):
    S1  link.py 的帧分派必须走 registry.FRAMES 表, 不得硬编码 decode_*/TYPE_*
    S2  每条 CommandSpec 都能拼出非空命令文本, 且 args 与 build 签名一致
    S3  每条 FrameSpec 都能"打包 -> 解析 -> 解码"往返并产出 dict
    S4  登记表数量不低于基线(防止误删)

动态断言(连模拟器):
    D1  每条 CommandSpec 用示例参数发一次, 收到 ACK ... OK
    D2  每个 PARAM_IDS 可读写, 且回读值与写入一致(Q16.16)

用法:
    python contract_test.py              # 自动拉起模拟器
    python contract_test.py --port 5556 # 连已运行模拟器
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
    TYPE_MAP,
    TYPE_UNIFIED,
    UNIFIED_CHANNELS,
    UNIFIED_LEN,
    FrameStreamParser,
    decode_unified,
    float_to_q1616,
    pack_frame,
    pack_unified,
    split_items,
)
from host.protocol.commands import LESSON_PARAM_IDS, PARAM_IDS, PRODUCTION_PARAM_IDS
from host.registry import (
    COMMANDS,
    FRAMES,
    command_line,
    check_frame_dispatch_is_table_driven,
    spec_of,
)

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


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------- 静态

def test_static() -> None:
    print("\n[S] 静态断言")

    # S1: link.py 分派必须查表
    import os

    from host.transport import link as link_mod

    src = open(link_mod.__file__, encoding="utf-8").read()
    bad = check_frame_dispatch_is_table_driven(src)
    check("link.py 帧分派走 registry.FRAMES(无硬编码 decode_*)", not bad, "\n".join(bad))

    # S4: 数量基线(与固件实际命令集一致: v2.0 = 18 条命令 / 2 种帧 / 38 参数)
    check(f"COMMANDS 登记 {len(COMMANDS)} 条(基线 18)", len(COMMANDS) >= 18, str(len(COMMANDS)))
    check(f"FRAMES 登记 {len(FRAMES)} 种(基线 2)", len(FRAMES) >= 2, str(len(FRAMES)))
    check(f"PARAM_IDS 登记 {len(PARAM_IDS)} 项(基线 38)", len(PARAM_IDS) >= 38, str(len(PARAM_IDS)))
    check(f"PRODUCTION_PARAM_IDS 生产段 {len(PRODUCTION_PARAM_IDS)} 项(基线 24)",
          len(PRODUCTION_PARAM_IDS) >= 24, str(len(PRODUCTION_PARAM_IDS)))

    _test_firmware_mode_gates()
    _test_protocol_constants_vs_firmware()


def _test_protocol_constants_vs_firmware() -> None:
    """S6: 主机侧协议常量必须与固件 link.h 逐项一致(防两侧漂移)。

    v2.0 的 `U_*` 字段偏移与 `FLAG_*` 标志位是**同一份协议的两处实现** ——
    固件在 `link_UnifiedFrame()` 里按 `LINK_U_*` 写, 主机在 `decode_unified()`
    里按 `U_*` 读。任何一侧单独改动都**不会**有编译/导入错误, 只会在真机上
    表现为"整片字段错位"或"某个标志位恒假", 定位成本极高。

    所以这里把固件宏当作唯一真源, 逐个比对主机常量名/值。命名约定:
        主机 `U_XXX`   <->  固件 `LINK_U_XXX`
        主机 `FLAG_XXX` <->  固件 `LINK_FLAG_XXX`
    """
    print("\n[S6] 协议常量 vs 固件 link.h")

    import os
    import re

    from host.protocol import codec as _c

    fw = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "code_stm32f1_mouse")
    link_h = open(os.path.join(fw, "HARDWARE", "link.h"), encoding="utf-8").read()

    def macro(name: str):
        m = re.search(r"#define\s+" + re.escape(name) + r"\s+(0[xX][0-9a-fA-F]+|\d+)", link_h)
        return int(m.group(1), 0) if m else None

    check("link.h: LINK_TYPE_UNIFIED == codec.TYPE_UNIFIED",
          macro("LINK_TYPE_UNIFIED") == TYPE_UNIFIED, str(macro("LINK_TYPE_UNIFIED")))
    check("link.h: LINK_TYPE_MAP == codec.TYPE_MAP",
          macro("LINK_TYPE_MAP") == TYPE_MAP, str(macro("LINK_TYPE_MAP")))
    check(f"link.h: LINK_UNIFIED_LEN == codec.UNIFIED_LEN({UNIFIED_LEN})",
          macro("LINK_UNIFIED_LEN") == UNIFIED_LEN, str(macro("LINK_UNIFIED_LEN")))

    # ---- U_*: 字段偏移 ----
    u_names = sorted(n for n, v in vars(_c).items() if n.startswith("U_") and isinstance(v, int))
    bad = []
    for n in u_names:
        fv = macro("LINK_" + n)
        if fv != getattr(_c, n):
            bad.append(f"{n}: 主机 {getattr(_c, n)} != 固件 {fv}")
    check(f"统一帧 {len(u_names)} 个字段偏移与固件一致", not bad, " | ".join(bad))
    fw_u = sorted(re.findall(r"#define\s+(LINK_U_\w+)", link_h))
    host_u = sorted("LINK_" + n for n in u_names)
    check(f"固件 LINK_U_* 与主机 U_* 一一对应({len(fw_u)} 项)",
          fw_u == host_u, f"固件独有 {sorted(set(fw_u) - set(host_u))} 主机独有 {sorted(set(host_u) - set(fw_u))}")

    # ---- FLAG_*: 位定义 ----
    flg = sorted(n for n, v in vars(_c).items() if n.startswith("FLAG_") and isinstance(v, int))
    bad = []
    for n in flg:
        fv = macro("LINK_" + n)
        if fv != getattr(_c, n):
            bad.append(f"{n}: 主机 {getattr(_c, n)} != 固件 {fv}")
    check(f"flags {len(flg)} 位与固件一致", not bad, " | ".join(bad))
    fw_f = sorted(re.findall(r"#define\s+(LINK_FLAG_\w+)", link_h))
    check(f"固件 LINK_FLAG_* 数量({len(fw_f)}) == 主机 FLAG_* 数量({len(flg)})",
          len(fw_f) == len(flg), str(fw_f))


def _test_firmware_mode_gates() -> None:
    """S5: 固件档位门控一致性(真机实测缺陷的静态回归, 2026-10-04)。

    背景: `MOUSE_TEST_MODE` 只由 [1][2][3] 派生, **[4] MOUSE_DATA_LOG 刻意不派生**
    (它要跑 doTask*/LESSON_ISR 自走)。当"实验档共同行为"被写成裸
    `#ifdef MOUSE_TEST_MODE` 时, [4] 会静默退化成比赛固件语义:
      · 急停锁存无法用 `STOP` 解除 -> 一次急停废掉整场采集;
      · 急停 200ms 后 sleepmode=1 -> isr.c 跳过全部采样, 但 LINK_Tick() 在门外
        照发帧 -> "链路活着、数据冻结", 现场极难察觉。
    这类错误**不会有编译错误**, 而且档位无关的模拟器抓不到 —— 只能静态查源码。
    故这里断言: 实验档共同行为必须走**具名派生宏** `MOUSE_ESTOP_RECOVERABLE`。
    """
    print("\n[S5] 固件档位门控一致性")

    import os
    import re

    fw = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "code_stm32f1_mouse")
    main_h = open(os.path.join(fw, "USER", "main.h"), encoding="utf-8").read()
    link_c = open(os.path.join(fw, "HARDWARE", "link.c"), encoding="utf-8").read()
    isr_c = open(os.path.join(fw, "HARDWARE", "isr.c"), encoding="utf-8").read()

    # S5a: 具名派生宏存在, 且明确覆盖 [4]
    derived = re.search(
        r"#if defined\(MOUSE_TEST_MODE\) \|\| defined\(MOUSE_DATA_LOG\)\s*\n"
        r"#define MOUSE_ESTOP_RECOVERABLE 1", main_h)
    check("main.h: MOUSE_ESTOP_RECOVERABLE = MOUSE_TEST_MODE || MOUSE_DATA_LOG",
          bool(derived), "未找到派生定义")

    # S5b: link.c 的 STOP 解锁块 —— 只认具名宏, 不许残留裸 MOUSE_TEST_MODE
    #      注意 anchor 必须用 `link_StopMotion(LINK_RUN_REASON_USER)`(顶层 STOP 的处理体)：
    #      `link_TokIs(&p, "STOP")` 在 link.c 里出现两次(lessson 子命令 / 顶层), 用前者会抓错块;
    #      而 `link_StopMotion(LINK_RUN_REASON_ESTOP)` 是 ESTOP 分支, 也不是这里要的。
    i = link_c.find("link_StopMotion(LINK_RUN_REASON_USER)")
    stop_blk = link_c[max(0, i - 260):i + 900] if i >= 0 else ""
    check("link.c: 找到顶层 STOP 处理块", bool(stop_blk))
    check("link.c: STOP 解锁走 MOUSE_ESTOP_RECOVERABLE",
          "MOUSE_ESTOP_RECOVERABLE" in stop_blk, stop_blk[:120])
    check("link.c: STOP 块内无裸 MOUSE_TEST_MODE",
          "MOUSE_TEST_MODE" not in stop_blk)

    # S5c: isr.c 的急停休眠块(窗口止于块末尾的 LINK_EStopTrip(); —— 再往后
    #      还有一处合法的 `#ifndef MOUSE_TEST_MODE` 用于 LESSON_ISR, 不能框进来)
    i = isr_c.find("if (emergMon.active == 1)")
    j = isr_c.find("LINK_EStopTrip();", i) if i >= 0 else -1
    em_blk = isr_c[i:j] if (i >= 0 and j > i) else ""
    check("isr.c: 找到急停处理块", bool(em_blk))
    check("isr.c: 急停休眠走 MOUSE_ESTOP_RECOVERABLE",
          "MOUSE_ESTOP_RECOVERABLE" in em_blk, em_blk[:160])
    check("isr.c: 急停块内无裸 MOUSE_TEST_MODE", "MOUSE_TEST_MODE" not in em_blk)

    # S5d: 残留裸门控的**清点基线** —— 将来新加一处会立刻报红
    #      isr.c 余 3 处: 779 #ifdef(运动状态机停用) / 881 #endif / 902 #ifndef(LESSON_ISR)
    check("link.c 已无裸 MOUSE_TEST_MODE(基线 0)", link_c.count("MOUSE_TEST_MODE") == 0,
          str(link_c.count("MOUSE_TEST_MODE")))
    check("isr.c 裸 MOUSE_TEST_MODE 仅剩 3 处(基线 3)", isr_c.count("MOUSE_TEST_MODE") == 3,
          str(isr_c.count("MOUSE_TEST_MODE")))

    # S5e: 程序区上界必须与 Flash 数据区起始页严格对齐。
    #      uvprojx 内 0x8000000 出现多次(全芯片描述 + 实际启用的 IROM1),
    #      故取其中**最小**的 Size 作为"程序区上界"; 它若越过 FLASH_ADDR_DATA_NUM,
    #      擦写标定/迷宫数据会覆盖代码区(现场表现为"标定莫名丢失"), 故钉住此不变量。
    uv = open(os.path.join(fw, "USER", "Mouse.uvprojx"), encoding="utf-8").read()
    flash_h = open(os.path.join(fw, "HARDWARE", "flash.h"), encoding="utf-8").read()
    flash_c = open(os.path.join(fw, "HARDWARE", "flash.c"), encoding="utf-8").read()
    link_h = open(os.path.join(fw, "HARDWARE", "link.h"), encoding="utf-8").read()
    sizes = [int(sz, 16) for addr, sz in
             re.findall(r"<StartAddress>(0x[0-9a-fA-F]+)</StartAddress>\s*<Size>(0x[0-9a-fA-F]+)</Size>", uv)
             if int(addr, 16) == 0x08000000]
    m_data = re.search(r"#define\s+FLASH_ADDR_DATA_NUM\s+(0X[0-9a-fA-F]+)", flash_h)
    check("uvprojx: 找到 0x08000000 的程序区描述", bool(sizes), str(sizes))
    check("flash.h: 找到 FLASH_ADDR_DATA_NUM", bool(m_data))
    if sizes and m_data:
        data_start = int(m_data.group(1), 16) - 0x08000000
        check(f"程序区上界 0x{min(sizes):X} == 数据区起始 0x{data_start:X}(不越界)",
              min(sizes) == data_start, f"sizes={[hex(x) for x in sizes]} data=0x{data_start:X}")

    # S5f: Flash 页布局不得互相踩踏。
    #      背景(2026-10-04 实测): 链路模块原先把标定页取在 page 99/100, 而运行数据区的
    #      **擦除范围**是 `saveNum/100+1 = 13 页` = page 89..101 —— 两个标定页正落在里面,
    #      一次 FLASH_EraseRunData()(calibrate.c / menu.c / state.c 共 3 处调用)
    #      就会把生产标定与课程参数一起擦掉。现已上移到 115/116。此断言钉死这件事。
    def addr_of(macro: str, text: str):
        mm = re.search(r"#define\s+" + macro + r"\s+(0[xX][0-9a-fA-F]+)", text)
        return int(mm.group(1), 16) if mm else None

    # 注意 flash.c 里 saveNum 有两处: `#ifndef FLASHSAVE` 分支的 =1 与
    # `#ifdef FLASHSAVE` 分支的 =1200。**生效的是后者**(flash.c 顶部无条件 `#define FLASHSAVE`),
    # 故必须从 `#ifdef FLASHSAVE` 之后开始找, 否则会取到 1 → 页数算成 1。
    i_fs = flash_c.find("#ifdef FLASHSAVE")
    m_save = re.search(r"const\s+u16\s+saveNum\s*=\s*(\d+)",
                       flash_c[i_fs:] if i_fs >= 0 else flash_c)
    check("flash.c: 找到生效的 saveNum(=1200)", bool(m_save) and m_save.group(1) == "1200",
          m_save.group(0) if m_save else "未找到")
    d_data = addr_of("FLASH_ADDR_DATA", flash_h)
    check("flash.h: 找到 FLASH_ADDR_DATA", d_data is not None)
    if m_save and d_data and m_data:
        pages = int(m_save.group(1)) // 100 + 1          # FLASH_EraseRunData 的页数
        erase_lo, erase_hi = d_data, d_data + pages * 0x400
        check(f"运行数据区擦除范围 = {pages} 页 (page "
              f"{(erase_lo - 0x08000000)//0x400}..{(erase_hi - 0x08000000)//0x400 - 1})",
              pages == 13, f"pages={pages}")
        for macro_name in ("FLASH_ADDR_SYSCALIB", "FLASH_ADDR_LESSONPARAM"):
            a = addr_of(macro_name, link_h)
            check(f"link.h: 找到 {macro_name}", a is not None)
            if a is None:
                continue
            in_data_area = a >= int(m_data.group(1), 16)
            in_erase = erase_lo <= a < erase_hi
            aligned = (a - 0x08000000) % 0x400 == 0
            below_maze = a < int(re.search(r"#define\s+FLASH_ADDR_BLOCK_STACK\s+(0[xX][0-9a-fA-F]+)",
                                          flash_h).group(1), 16)
            check(f"{macro_name}(page {(a - 0x08000000)//0x400}) 位于数据区", in_data_area)
            check(f"{macro_name} **不在运行数据擦除范围内**", not in_erase,
                  f"erase=0x{erase_lo:X}..0x{erase_hi:X} addr=0x{a:X}")
            check(f"{macro_name} 1KB 对齐", aligned)
            check(f"{macro_name} 不越过迷宫页(118)", below_maze)

    # S5g: 源码"注释提前闭合"扫描。
    #      场景: 在 `/* ... */` 块注释里写了形如 `doTask*/LESSON_ISR` 或 `TEE_*/CROSS` 的文本,
    #      其中的 `*/` 会**提前结束注释**, 后面的文字被当成代码 → 报出一堆看似无关的错误
    #      (上次是 main.h 869 个 error, 这次是 link.c 的 "struct has no field")。
    #      本项目已踩两次, 故做成自动检查: 跨行块注释内部的 `*/` 之后不得再有内容。
    bad: list = []
    for dirpath, _dirs, files in os.walk(fw):
        if "tools" in dirpath.replace("\\", "/"):
            continue
        for fn in sorted(files):
            if not fn.endswith((".c", ".h")):
                continue
            path = os.path.join(dirpath, fn)
            try:
                lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
            except OSError:
                continue
            block_line = 0
            for ln, text in enumerate(lines, 1):
                rest = text
                while True:
                    if block_line == 0:
                        o = rest.find("/*")
                        if o < 0:
                            break
                        e = rest.find("*/", o + 2)
                        if e < 0:
                            block_line = ln
                            break
                        rest = rest[e + 2:]
                    else:
                        e = rest.find("*/")
                        if e < 0:
                            break
                        if ln > block_line and rest[e + 2:].strip():
                            bad.append(f"{os.path.relpath(path, fw)}:{ln}: {rest[e + 2:].strip()[:50]}")
                        block_line = 0
                        rest = rest[e + 2:]
    check("固件源码无'注释提前闭合'(跨行注释内的 */ 之后不得再有内容)",
          not bad, " | ".join(bad[:5]))

    # S5h: 每个帧 sink 必须用**自己的** stream 名去 notify。
    #      背景(2026-10-04 实测踩过): 往 ring.py 里插新 sink 时, 锚点选在
    #      `push_time` 的最后一行之前, 结果把它的 `self._notify("time", rec)`
    #      挤进了新函数的末尾 —— 于是 TIME 帧不再通知(时间同步样本恒为 0),
    #      而新帧冒名通知成 "time"。**编译与 import 都不会报错**, 只有跑起来才现形。
    import inspect as _inspect

    from host.model import ring as _ring

    #      判据: 只要这个 sink 里出现了 _notify, 就必须是**自己的** stream 名,
    #      且不得出现别人的; 另外下列"逐帧遥测"流必须确实 notify。
    MUST_NOTIFY = {"unified", "map"}
    streams = {sp.stream for sp in FRAMES.values()}
    sink_bad = []
    for spec in FRAMES.values():
        if not spec.sink.startswith("push_"):
            continue
        fn = getattr(_ring.TelemetryStore, spec.sink, None)
        if fn is None:
            sink_bad.append(f"{spec.sink}: 不存在")
            continue
        body = _inspect.getsource(fn)
        mine = f'self._notify("{spec.stream}", rec)'
        if mine not in body:
            if spec.stream in MUST_NOTIFY or "_notify(" in body:
                sink_bad.append(f"{spec.sink} 缺少 {mine}")
        for other in streams - {spec.stream}:
            if f'self._notify("{other}", rec)' in body:
                sink_bad.append(f"{spec.sink} 误用 {other}")
    check("每个帧 sink 只 notify 自己的 stream(防插错位置)", not sink_bad, " | ".join(sink_bad[:5]))
    # 生产段必须是全集的子集, 且课程段不混入(否则 get_all 的等待集合会算错)
    check("生产段 ∩ 课程段 = ∅",
          not (set(PRODUCTION_PARAM_IDS) & set(LESSON_PARAM_IDS)),
          str(sorted(set(PRODUCTION_PARAM_IDS) & set(LESSON_PARAM_IDS))))

    # FRAMES 覆盖全部 TYPE_* 常量
    all_types = {TYPE_UNIFIED, TYPE_MAP}
    check("FRAMES 覆盖全部 2 种帧类型", all_types.issubset(set(FRAMES)),
          str(all_types - set(FRAMES)))

    # S5: CLI 必须能导入并构建 parser
    #     它的教训很直接: cli.py 曾经因为"两条语句挤在一行"而语法错误,
    #     但没有任何测试 import 它 -> 错误一路潜行到生成文档时才炸。
    try:
        import argparse

        from host import cli as _cli

        parser = _cli._build_parser()
        choices: set = set()
        for act in parser._actions:
            if isinstance(act, argparse._SubParsersAction):
                choices = set(act.choices)
                break
        # v2.0: 子命令里的 `scope` 与 `snap` 已随协议精简删除(命令集 41→18,
        # 帧 9→2); 这里保持"现存子命令全覆盖"的检出力 —— 少一个立刻报红。
        need = {"ports", "connect", "get", "get-all", "set", "calib", "save",
                "maze", "monitor", "motor-test", "estop", "diagnose", "scan-baud",
                "replay", "lesson", "datalog"}
        missing = need - choices
        check(f"CLI 可导入且子命令齐备({len(choices)} 个, 含 motor-test/lesson/datalog)",
              not missing, f"缺 {sorted(missing)}")
    except Exception as e:  # noqa: BLE001
        check("CLI 可导入并构建 parser", False, f"{type(e).__name__}: {e}")


def test_command_build() -> None:
    print("\n[S2] 命令构建")
    for name, spec in COMMANDS.items():
        # 用示例参数拼一条命令
        args = {k: _EXAMPLE_ARGS[k] for k in spec.args}
        try:
            line = command_line(name, **args)
        except Exception as e:
            check(f"{name}: 可拼命令", False, f"{type(e).__name__}: {e}")
            continue
        ok = bool(line) and "\n" not in line and "\r" not in line and line == line.strip()
        check(f"{name}: 产出非空单行命令 '{line}'", ok, repr(line))
        # 命令必须是大写关键字开头(与固件 token 解析一致)
        head = line.split()[0].upper()
        check(f"{name}: 关键字大写 '{head}'", head == line.split()[0], repr(line))


def test_frames_roundtrip() -> None:
    print("\n[S3] 帧打包/解析/解码往返")
    parser = FrameStreamParser()
    for ftype, spec in FRAMES.items():
        payload = _PAYLOAD_BUILDERS[ftype]()
        frame = pack_frame(ftype, payload, 1)
        frames, lines = split_items(parser.feed(frame))
        if not frames:
            check(f"{spec.name}: 解析出帧", False, f"lines={lines}")
            continue
        rec = spec.decoder(frames[0].payload)
        check(f"{spec.name}: 解码产出 dict", isinstance(rec, dict) and bool(rec),
              str(type(rec).__name__))
        check(f"{spec.name}: 序号往返", frames[0].seq == 1, str(frames[0].seq))

    # 统一帧编解码必须**互为对偶** —— `pack_unified` 是模拟器与测试共用的唯一来源,
    # 一旦它与 `decode_unified` 漂移, 模拟器就变成"自己跟自己说话":
    # 全套测试照样绿, 真机却整片错位。故这里做一次全字段往返。
    d = decode_unified(_payload_unified())
    again = decode_unified(pack_unified(d))
    mism = {k: (d.get(k), again.get(k)) for k in set(d) | set(again) if d.get(k) != again.get(k)}
    check("统一帧 pack/decode 往返一致(含派生字段)", not mism, str(mism))
    check(f"统一帧 payload 长度 == {UNIFIED_LEN}", len(_payload_unified()) == UNIFIED_LEN,
          str(len(_payload_unified())))
    missing = [c for c in UNIFIED_CHANNELS if c not in d]
    check(f"UNIFIED_CHANNELS {len(UNIFIED_CHANNELS)} 个通道解码后全含", not missing, str(missing))


# ---------------------------------------------------------------- 动态

def test_commands_e2e(port: int) -> None:
    print("\n[D1] 每条命令在模拟器上收到 ACK OK")
    from host.api import MouseClient

    with MouseClient.connect_to(f"tcp://127.0.0.1:{port}") as mc:
        check("连接成功", mc.is_connected, mc.state_detail)

        # 按顺序执行, 覆盖全部登记命令; 顺序照顾副作用(先只读, 后动作)
        order = [
            # ---- 只读 / 设备信息 ----
            "ping", "params.get", "params.get_all",
            # ---- 遥测拉取(v2.1: TELEM 请求-响应; 前面 D2 已按旧口径验过 STREAM) ----
            "telem", "telem", "ping", "ping",
            # ---- 静态数据回传 ----
            "maze.dump",
            # ---- 参数写 ----
            "params.setq", "params.set", "params.save", "params.reset",
            # ---- 标定(请求-响应: ACK 要等这一步采集完成才回, 见方案 §9) ----
            "calib.cal",
            # 每条运动命令前先 stop: 设备会正确拒绝"运动中的第二条 MOVE"(见 D4),
            # 不清场就会误报 ERR_BUSY。
            "tare", "move.fwd", "stop", "move.rot", "stop", "move.run", "stop",
            "estop", "estop.clear",
            # ---- 课程调参实验(放最后: start 会启动一段剖面, status 应见 running=1) ----
            "lesson.start", "lesson.status", "lesson.stop",
            # ---- 时间同步 / 数据收集 ----
            # maze.stop 收车并复位发车状态 —— 不加它的话设备会停在"探索中",
            # 而 maze.run 的 busy 判据正是"请求位或运行位"（v2.1 真机缺陷的落点）。
            "arm", "maze.run", "maze.stop",
            # ---- 救援: 整机软复位 ----
            # ⚠️ **必须放最后**: 它会把设备打回空闲(等效于重启), 之后的状态
            # 与前面所有用例的前提都不同。放在这里也顺带证明了它自己不会崩链路。
            "system.reset",
        ]
        covered = set()
        for name in order:
            args = {k: _EXAMPLE_ARGS[k] for k in spec_of(name).args}
            r = mc.send_cmd(name, timeout=5.0, **args)
            covered.add(name)
            check(f"{name}: ACK OK", r.ok, f"{r.status} cmd={r.data.get('cmd')!r}")
            # 运动类命令要留出真实结束时间, 否则下一条会被 ERR_BUSY 拒掉(这是正确行为)
            time.sleep(0.25 if name.startswith("move.") else 0.02)

        # 覆盖率: 所有登记命令都必须被跑到
        missing = set(COMMANDS) - covered
        check("覆盖全部登记命令", not missing, f"未覆盖: {missing}")
        # 反向: 顺序里不得有未登记的命令
        unknown = covered - set(COMMANDS)
        check("顺序里无未登记命令", not unknown, str(unknown))


def test_motion_mutex_e2e(port: int) -> None:
    """[D4] 运动互斥: 第二条 MOVE 必须被 ERR_BUSY 拒掉, **且不得重置护栏**。

    真机实测揪出的缺陷(2026-10-04): 固件 `MOVE RUN/FWD/ROT` 启动时都把
    `motionCtrl.task` 置 NULL(它们由 `link_MoveService` 驱动, 不走任务状态机),
    于是四处 busy 检查只查 `motionCtrl.task` 是**查不出正在跑 MOVE 的**。
    后果: 第二条 `MOVE RUN` 被接受并覆盖 `linkMove.elapsed/limit` ——
    **时间护栏被重置**, 连续发就能把电机运行时间无限延长(真机: 首条 1500ms,
    0.48s 处再发一条, 实际跑到 ~1.98s)。

    模拟器当时也没查, 所以整套测试盖不住。现在两边都按 `link_MotionBusy()` 对齐,
    本用例就是那条防线。
    """
    print("\n[D4] 运动互斥: 第二条 MOVE 被拒且不重置护栏")
    from host.api import MouseClient
    from host.api.events import Event

    with MouseClient.connect_to(f"tcp://127.0.0.1:{port}") as mc:
        # v2.0 起运动结束只有 `STOPPED <reason> #n` 一条事件行(不再有 MOVEDONE),
        # 它走 Event.EVENT(ESTOP TRIP 才走 Event.ESTOP), 所以订阅 EVENT。
        moves: list = []
        mc.on(Event.EVENT, lambda m: moves.append(m.data.get("raw", "")))

        # 先回到空闲: 前面的用例跑过 MAZE RUN, 会把设备留在"探索中"——
        # 那正是互斥判据该拦的状态, 所以这里先 STOP 建立干净起点。
        mc.send_cmd("stop")
        time.sleep(0.3)

        t0 = time.time()
        r1 = mc.send_cmd("move.run", sp_l=30, sp_r=30, ms=1200, ir_mm=10)
        check("第 1 条 MOVE RUN 被接受", r1.ok, r1.status)
        r2 = mc.send_cmd("move.run", sp_l=30, sp_r=30, ms=1200, ir_mm=10)
        check("第 2 条(仍在跑)被拒 ERR_BUSY", (not r2.ok) and r2.status == "ERR_BUSY", r2.status)

        # 换一种运动形式也应被拒(三种 MOVE 共用同一判据)
        r3 = mc.send_cmd("move.rot", pwm=200, ms=500)
        check("运动中的 MOVE ROT 也被拒", (not r3.ok) and r3.status == "ERR_BUSY", r3.status)
        r4 = mc.send_cmd("move.fwd", pulses=3000, speed=40)
        check("运动中的 MOVE FWD 也被拒", (not r4.ok) and r4.status == "ERR_BUSY", r4.status)
        r5 = mc.send_cmd("params.save")
        check("运动中的 SAVE 也被拒(擦写停顿会打断 1kHz 环)",
              (not r5.ok) and r5.status == "ERR_BUSY", r5.status)

        # 等这次运动真正结束
        mc.wait(lambda: len([m for m in moves if m.startswith("STOPPED")]) >= 1, timeout=6.0)
        elapsed = time.time() - t0
        done = [m for m in moves if m.startswith("STOPPED")]
        check("只有 1 次运动被执行(1 条 STOPPED)", len(done) == 1, str(done))
        # v2.0 的 STOPPED 事件**不再带 elapsed**(明细量都在 217Hz 的统一帧里),
        # 所以改用墙钟验证"护栏没被重置": 若第 2 条 MOVE 覆盖了 linkMove.limit,
        # 这里实测会跑满两段(≈2.4s), 而不是第一段的 1.2s。
        check("护栏时长未被重置(实测≈1.2s, 不是≈2.4s)",
              1.05 <= elapsed <= 1.90, f"{elapsed:.2f}s")

        # 空下来以后应恢复可接受
        time.sleep(0.4)
        r6 = mc.send_cmd("move.run", sp_l=30, sp_r=30, ms=200, ir_mm=10)
        check("空闲后 MOVE RUN 恢复可接受", r6.ok, r6.status)
        mc.wait(lambda: len([m for m in moves if m.startswith("STOPPED")]) >= 2, timeout=4.0)


def test_estop_recoverable_e2e(port: int) -> None:
    """[D5] 急停必须可恢复: ESTOP 锁存 -> 拒绝运动 -> STOP 解锁 -> 恢复可动。

    真机实测的缺陷(2026-10-04, [4] 数据收集档): "STOP 兼作解急停"整段被包在
    `#ifdef MOUSE_TEST_MODE` 里, 而 [4] 不派生该宏 -> `STOP` 回 OK 但
    `linkEStop` 不复位, 之后的 MOVE/CAL 全部 ERR_ESTOP,**只能断电重启**。
    同时急停 200ms 后 sleepmode=1 -> 采样全停但链路照发 -> 数据冻结。

    本用例钉住"可恢复"这一半(冻结那半边靠 S5 的静态断言)。
    """
    print("\n[D5] 急停可恢复: 锁存 -> 拒绝 -> STOP 解锁 -> 恢复")
    from host.api import MouseClient

    with MouseClient.connect_to(f"tcp://127.0.0.1:{port}") as mc:
        mc.send_cmd("stop")
        time.sleep(0.3)

        r1 = mc.send_cmd("move.run", sp_l=20, sp_r=20, ms=1500, ir_mm=10)
        check("急停前 MOVE RUN 被接受", r1.ok, r1.status)

        r2 = mc.send_cmd("estop")
        check("ESTOP 被接受", r2.ok, r2.status)
        time.sleep(0.4)

        # 锁存期间: 运动命令被拒, 且统一帧 flags 明确标出 ESTOP_LOCK
        r3 = mc.send_cmd("move.run", sp_l=20, sp_r=20, ms=300, ir_mm=10)
        check("锁存中 MOVE RUN 被拒 ERR_ESTOP",
              (not r3.ok) and r3.status == "ERR_ESTOP", r3.status)
        # v2.0: SNAP 已删除 —— 状态位直接从统一帧读(它每 4.6ms 就带一份全量快照)
        flags = mc.telemetry.flags()
        check("统一帧 flags 置 ESTOP_LOCK", flags.get("estop_lock") is True, str(flags))

        # STOP 解锁(上位机 estop.clear 映射到 STOP)
        r4 = mc.send_cmd("estop.clear")
        check("STOP(estop.clear) 被接受", r4.ok, r4.status)
        time.sleep(0.4)

        flags = mc.telemetry.flags()
        check("解锁后 ESTOP_LOCK 已清", not flags.get("estop_lock"), str(flags))

        r5 = mc.send_cmd("move.run", sp_l=20, sp_r=20, ms=300, ir_mm=10)
        check("解锁后 MOVE RUN 恢复可接受", r5.ok, r5.status)
        time.sleep(1.0)
        mc.send_cmd("stop")


def test_idempotency_e2e(port: int) -> None:
    """[D3] 命令幂等语义: 同字节重发 = 重放应答, 不重执行; 不同 seq = 各执行一次。

    这两条是"连续发相同运动命令"能安全工作的前提, 也解释了主机为什么敢在超时后
    原样重发(命令层 `CommandClient.poll()` 的字节不变重发)。
    以前**没有任何测试覆盖它** —— 因为上层 API 从不会发出重复命令,
    所以只能直接构造同一份字节从链路灌进去。

    判据用固件的异步行 `STOPPED <reason> #n`: 它只在运动真正结束时发一次,
    于是"发几次 / 执行几次"一眼可数。v2.0 的 STOPPED 与遥测帧**共用**同一个
    1 字节序号, 所以顺带也验证了序号在同字节重放时**不会**被重复消耗。
    """
    print("\n[D3] 幂等: 同字节重发不重执行 / 不同 seq 各执行一次")
    from host.api import MouseClient
    from host.api.events import Event

    with MouseClient.connect_to(f"tcp://127.0.0.1:{port}", auto_stream=False) as mc:
        # ⚠️ 必须 auto_stream=False: 本用例直接构造裸字节并**硬编码 seq=77** 灌进链路,
        # 而拉取器会持续发 TELEM 并占用同一套 16 位序号 —— 序号一旦撞上 77, 它的
        # `ACK 77 OK <upMs>` 就会被下面的过滤器误收进来, 让"重放应答一条不多不少"
        # 的断言假红。这里要测的是**裸命令通道**的幂等语义, 不该有后台流量。
        got: list = []
        mc.on(Event.TEXT, lambda m: got.append(m.data.get("line", "")))
        # 前序用例跑过自走/运动, 先回到空闲再测幂等
        mc.send_cmd("stop")
        time.sleep(0.3)

        def _run_once(seq: int, times: int, text: str = "MOVE RUN 30 30 1") -> None:
            line = mc.cmd._build(text, seq)
            for _ in range(times):
                mc.link.write(line)
                time.sleep(0.35)

        # (1) 同一份字节发 3 次 -> 应答 3 条(重放), STOPPED 只 1 条
        # 注意: ACK 行会被命令层就地匹配掉、**不会**变成 TEXT 事件,
        # 所以应答条数要从命令层日志里数(它记录了每一行收发)。
        got.clear()
        n_log = len(mc.cmd.log)
        _run_once(77, 3)
        time.sleep(0.8)
        acks = [t for _ts, d, t in mc.cmd.log[n_log:] if d == "<-" and t.startswith("ACK 77 ")]
        stops = [l for l in got if l.startswith("STOPPED")]
        check("同字节 x3: 每条都得到应答(重放)", len(acks) == 3, str(acks))
        check("同字节 x3: 只执行一次(1 条 STOPPED)", len(stops) == 1, str(stops))

        # (2) 相同文本但不同 seq -> 是两条独立命令, 各执行一次
        got.clear()
        _run_once(78, 1)
        time.sleep(0.2)
        _run_once(79, 1)
        time.sleep(0.8)
        stops2 = [l for l in got if l.startswith("STOPPED")]
        check("不同 seq x2: 两条都执行(2 条 STOPPED)", len(stops2) == 2, str(stops2))
        # v2.0 的事件行只剩"为什么停" + 共用序号 —— elapsed/dR 这些明细量
        # 在 217Hz 的帧序列里已经全有, 再塞进不可靠的长文本只是浪费带宽。
        check("不同 seq x2: 事件形态为 `STOPPED <reason> #n`",
              all(len(l.split()) == 3 and l.split()[2].startswith("#") for l in stops2),
              str(stops2))
        check("两次 STOPPED 的共用序号不同",
              len({l.split()[2] for l in stops2}) == 2, str(stops2))


def test_params_e2e(port: int) -> None:
    """[D2] 参数读写往返 —— 并**钉死"课程参数读不回"这个固件不对称**。

    ⚠️ 真机事实(固件 `link.c`): `linkParam[LINK_P_NUM]` 只装**生产命名空间**
    (id 1..7 标定 + 22..38 红外/电源)。`GET ALL` 只遍历它, `GET <name>` 也只在
    它里面查 —— 于是**课程 14 项(id 8..21)压根读不回来**, 一律回 `ERR_NAME`;
    而写侧(`SET`/`SETQ`)有课程分支, 是通的。课程值只活在 [3] 档的**独立**页
    (page 116) 里, 固件读侧没有接上。

    为什么必须把它写成断言而不是"跳过": 模拟器早期版本回全部 38 项,
    **掩盖了这个不对称**, 让真机上的 `GET KPL -> ERR_NAME` 变成一个"随机故障"。
    现在模拟器原样复刻, 本用例就分两组各自断言:

        生产段 (24 项)  读 -> 写回同一值 -> 再读, 逐位一致
        课程段 (14 项)  `GET` 必须回 `ERR_NAME`(读不回); `SETQ` 必须回 `OK`(写得进)
    """
    print("\n[D2] 参数读写往返 + 课程参数读侧不对称")
    from host.api import MouseClient

    with MouseClient.connect_to(f"tcp://127.0.0.1:{port}") as mc:
        # 登记表本身要自洽: 两段不重叠且并集就是全集
        check("生产段 + 课程段 == PARAM_IDS(38 项)",
              set(PRODUCTION_PARAM_IDS) | set(LESSON_PARAM_IDS) == set(PARAM_IDS),
              f"prod={len(PRODUCTION_PARAM_IDS)} lesson={len(LESSON_PARAM_IDS)} "
              f"all={len(PARAM_IDS)}")

        # ---- 生产段: 真正可读可写 ----
        for name, nid in sorted(PRODUCTION_PARAM_IDS.items(), key=lambda kv: kv[1]):
            # 读原始值, 写回同一个值, 再读校验(用 SETQ 保证无精度损失)
            before = mc.params.get_raw(name)
            if before is None:
                check(f"{name}: 可回读", False)
                continue
            r = mc.params.set_raw(name, before, readback=True)
            check(f"{name}: 写回并校验", r.ok, r.status)
            check(f"{name}: 回读与写入一致", r.ok and r.data.get("readback") == before,
                  str(r.data.get("readback")))

        # ---- 课程段: 读不回(固件读侧只有生产表), 但写得进 ----
        for name, nid in sorted(LESSON_PARAM_IDS.items(), key=lambda kv: kv[1]):
            r = mc.send_cmd("params.get", name=name, timeout=3.0)
            check(f"{name}: GET 回 ERR_NAME(读侧无课程表)",
                  (not r.ok) and r.status == "ERR_NAME", r.status)
        w = mc.send_cmd("params.setq", name="KPL", q=float_to_q1616(10.5), timeout=3.0)
        check("KPL: SETQ 回 OK(写侧有课程分支)", w.ok, w.status)


# ---------------------------------------------------------------- 示例参数

#: 示例实参 —— 键必须覆盖全部 `CommandSpec.args` 的并集。
#: v2.0 命令集精简到 18 条后, 与旧帧相关的键(hz/cond/mask/dom/prof)全部删除;
#: 若漏删, `{k: _EXAMPLE_ARGS[k] for k in spec.args}` 会 KeyError 而立刻报红,
#: 所以这里保留的每一项都是"真的还有命令在用"。
_EXAMPLE_ARGS = {
    # ---- 参数读写 ----
    "name": "TRACKL",
    "q": float_to_q1616(53.2),
    "v": "53.2",
    # ---- 标定(请求-响应: START/ABORT, 或 IRDATA 的相位号 0..3) ----
    "item": "GYRO",
    "act": "START",
    # ---- 运动 ----
    "pulses": 3000,
    "speed": 40,
    "pwm": 40,
    "ms": 100,
    "sp_l": 40,
    "sp_r": -40,
    "ir_mm": 10,
    # ---- 课程调参实验 ----
    "exp": 3,           # lesson.start
    "a": 50,            # v  (实验三/四/七) 或 angle (实验六)
    "b": 200,           # dist (实验三/七) 或 t_acc (实验四) 或 v (实验六)
    "c": 800,           # ms (实验三/六/七) 或 t_cruise (实验四)
    "d": 200,           # t_dec (实验四)
}


def _payload_unified() -> bytes:
    """统一帧载荷 —— **用 `pack_unified` 生成**, 与 `decode_unified` 互为对偶。

    自己 struct.pack 一遍会引入"第三处协议实现", 正好掩盖主机侧自身的漂移,
    所以这里刻意复用生产代码路径。
    """
    d = {c: 0 for c in UNIFIED_CHANNELS}
    d.update({
        "ms8": 0x5A,
        "irAdcF1": 2600, "irAdcF2": 2610, "irAdcL": 900, "irAdcR": 620,
        "irF1": 232, "irF2": 231, "irL": 42, "irR": 43,
        "battMv": 7620,
        "speedL": 68, "speedR": -12,
        "gyroAngle": 90, "encAngle": 5,
        "trackL": 1200, "trackR": -300,
        "cellOdom": 88, "pidErr": -12,
        # ---- 状态机位打包三字节 ----
        "dir": 1, "mazeTask": 2,
        "wallLeft": 0, "wallFront": 1, "wallRight": 1,
        "motionTask": 32, "irSwitch": 1,
        "motionStep": 3, "critL": 1, "critR": 0, "left45": 0, "right45": 0,
        "flags": 0x41,
        # ---- 坐标 / 决策 ----
        "posX": 3, "posY": 4, "goalX": 7, "goalY": 7,
        "wallThresh": 140, "stackIdx": 2, "fastwayCount": 9, "dropCnt": 1,
    })
    return pack_unified(d)


def _payload_map() -> bytes:
    n = 7
    wall = bytes([0x03] * (n * n))
    vis = bytes([1] * (n * n))
    return struct.pack("<bbBB", 3, 4, 1, 2) + wall + vis


#: v2.0 起只有两种帧(见 registry.FRAMES / 设计方案 §7)。
_PAYLOAD_BUILDERS = {
    TYPE_UNIFIED: _payload_unified,
    TYPE_MAP: _payload_map,
}


def main() -> int:
    ap = argparse.ArgumentParser(description="注册表契约测试")
    ap.add_argument("--port", type=int, default=0, help="已运行模拟器端口(默认自动拉起)")
    args = ap.parse_args()

    print("=" * 66)
    print("电脑鼠上位机 注册表契约测试")
    print("=" * 66)
    test_static()
    test_command_build()
    test_frames_roundtrip()

    port = args.port or _free_port()
    proc = None
    if not args.port:
        proc = subprocess.Popen(
            [sys.executable, "-m", "host.sim.mouse_sim", "--port", str(port), "--no-auto"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1.2)

    try:
        test_commands_e2e(port)
        test_idempotency_e2e(port)
        test_motion_mutex_e2e(port)
        test_estop_recoverable_e2e(port)
        test_params_e2e(port)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    print("\n" + "=" * 66)
    print(f"通过 {PASS} / 失败 {FAIL}")
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
