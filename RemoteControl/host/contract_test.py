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
from host.protocol.commands import PARAM_IDS, PRODUCTION_PARAM_IDS
from host.registry import (
    COMMANDS,
    FRAMES,
    command_line,
    check_frame_dispatch_is_table_driven,
    spec_of,
)

from host import fwprofile  # 固件档位: 两代固件并存, 由 RC_FW_PROFILE 切换

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

    # S4: 数量基线(与固件实际命令集一致: v2.6 = 24 条命令 / 2 种帧 / 41 参数)
    check(f"COMMANDS 登记 {len(COMMANDS)} 条(基线 24)", len(COMMANDS) >= 24, str(len(COMMANDS)))
    check(f"FRAMES 登记 {len(FRAMES)} 种(基线 2)", len(FRAMES) >= 2, str(len(FRAMES)))
    check(f"PARAM_IDS 登记 {len(PARAM_IDS)} 项(基线 24 = 固件 LINK_P_NUM)",
          len(PARAM_IDS) >= 24, str(len(PARAM_IDS)))
    check(f"PRODUCTION_PARAM_IDS 生产段 {len(PRODUCTION_PARAM_IDS)} 项(基线 24)",
          len(PRODUCTION_PARAM_IDS) >= 24, str(len(PRODUCTION_PARAM_IDS)))

    _test_firmware_mode_gates()
    _test_protocol_constants_vs_firmware()
    _test_enum_tables_vs_firmware()


def _test_enum_tables_vs_firmware() -> None:
    """S7: 上位机的**动作名表**必须与固件枚举逐项对齐(v2.3 新增)。

    为什么必须自动化: 这是**同一份枚举的两处实现**, 手工维护必然漂移 ——
    实测(v2.3 之前)主机 `MOTION_TASK_NAMES` **漏了 9 个码**(12/13/14/15/16/18/29/31/40)
    又**多出 8 个幽灵码**(46-53, 正好是那些缺项的"错位影子")。后果是真机跑到
    `TURNLEFTFAST(14)` / `EMERGENCY(16)` / `EDGESTRAIGHT(40)` 时上位机显示 `?` 或错名,
    **现场无法判读比赛动作** —— 而 `mouse_sim` 只产生 `motion_task ∈ {0,2,32}`,
    测试根本覆盖不到这些码, 所以这个缺陷一直没被任何套件发现。

    判据: **数值集合严格相等**(既不能缺、也不能多), 名称在归一化后逐项相等。
    归一化只抹平命名风格(去掉下划线、大写、去掉 `MOTION_TASK_`/`MAZE` 前缀),
    数值映射仍须严格一致。
    """
    print("\n[S7] 动作名表 vs 固件枚举")

    import os
    import re

    from host.protocol import codec as _c

    fw = fwprofile.FW_ROOT
    link_h = fwprofile.link_h_text()
    print(f"       {fwprofile.profile_line()}")

    def _norm(name: str) -> str:
        s = name.upper().replace("_", "")
        for p in ("MOTIONTASK", "MAZE"):
            if s.startswith(p):
                s = s[len(p):]
        return s

    # ---- 动作码表(按档位解析: 新固件 = timer.h/utility.h 的 `#define` 连续块;
    #      旧固件 = core.h 的 `typedef enum {...} MotionTask_t`) ----
    fw_motion = fwprofile.motion_task_map()
    check(f"固件动作码解析出 {len(fw_motion)} 个码",
          len(fw_motion) > 0, str(len(fw_motion)))
    miss = sorted(set(fw_motion) - set(_c.MOTION_TASK_NAMES))
    extra = sorted(set(_c.MOTION_TASK_NAMES) - set(fw_motion))
    check(f"MOTION_TASK_NAMES 数值集合与固件完全一致({len(fw_motion)} 码)",
          not miss and not extra,
          f"缺 {miss} | 多 {extra}")
    nm_bad = [f"{k}: 固件 {fw_motion[k]} != 主机 {_c.MOTION_TASK_NAMES[k]}"
              for k in sorted(fw_motion)
              if k in _c.MOTION_TASK_NAMES and _norm(fw_motion[k]) != _norm(_c.MOTION_TASK_NAMES[k])]
    check("MOTION_TASK_NAMES 名称逐项一致(归一化后)", not nm_bad, " | ".join(nm_bad))

    # ---- mouseTask 宏(名字表随档位: 新固件多一个 END=6) ----
    macro_names = list(fwprofile.PROFILE["maze_macros"])
    fw_maze = fwprofile.maze_task_map()
    check(f"固件解析出 {len(fw_maze)}/{len(macro_names)} 个 mouseTask 宏",
          len(fw_maze) == len(macro_names), str(sorted(fw_maze)))
    miss = sorted(set(fw_maze) - set(_c.MAZE_TASK_NAMES))
    extra = sorted(set(_c.MAZE_TASK_NAMES) - set(fw_maze))
    # 主机名表是**两代固件的并集**: 新基线 code_competition 的 `GucMouseTask` 多一个
    # END=6(跑位收尾态), 旧课程固件没有 ⇒ 在 legacy 档位下多出 6 是**已登记的差异**,
    # 不算两侧漂移。其余任何多/缺都仍然判失败。
    extra_ok = {6}
    real_miss = list(miss)
    real_extra = sorted(set(extra) - extra_ok)
    check(f"MAZE_TASK_NAMES 数值集合与固件一致(允许并集里的 END={sorted(extra_ok)[0]})",
          not real_miss and not real_extra, f"缺 {real_miss} | 多 {real_extra}")
    nm_bad = [f"{k}: 固件 {fw_maze[k]} != 主机 {_c.MAZE_TASK_NAMES[k]}"
              for k in sorted(fw_maze)
              if k in _c.MAZE_TASK_NAMES and _norm(fw_maze[k]) != _norm(_c.MAZE_TASK_NAMES[k])]
    check("MAZE_TASK_NAMES 名称逐项一致(归一化后)", not nm_bad, " | ".join(nm_bad))

    # ---- v2.3 新增位宏: 主机 FRONT_*/SPURTBF_* <-> 固件 LINK_FRONT_*/LINK_SPURTBF_* ----
    def _macro(name: str):
        mm = re.search(r"#define\s+" + re.escape(name) + r"\s+(0[xX][0-9a-fA-F]+|\d+)", link_h)
        return int(mm.group(1), 0) if mm else None

    bad = []
    for hn in ("FRONT_NEAR", "FRONT_FAR", "FRONT_BOTH",
               "SPURTBF_FLAG", "SPURTBF_GOBACK", "SPURTBF_TEST", "SPURTBF_OBJECT"):
        fv = _macro("LINK_" + hn)
        if fv != getattr(_c, hn):
            bad.append(f"{hn}: 主机 {getattr(_c, hn)} != 固件 {fv}")
    check(f"前墙/冲刺位宏 {7} 项与固件一致", not bad, " | ".join(bad))

    # ---- 冲刺档位 60..63(两代含义相同, 来源不同: 旧 spurt.h 的 SpurtSpeed_t;
    #      新 utility.h 的 LOW_90/LOW/MEDIUM/HIGH) ----
    fw_modes = fwprofile.spurt_speeds()
    check(f"固件冲刺档位解析出 4 档(60..63) == {fw_modes}",
          fw_modes == fwprofile.PROFILE["spurt_expect"], str(fw_modes))
    check("SPURT_MODE_NAMES 覆盖 0..4(0=未选 + 4 个固件档)",
          sorted(_c.SPURT_MODE_NAMES) == [0, 1, 2, 3, 4], str(sorted(_c.SPURT_MODE_NAMES)))


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

    fw = fwprofile.FW_ROOT
    link_h = fwprofile.link_h_text()

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
    """S8: 档位宏**唯一性** 与 课程实验层**清除彻底性**(v2.4 重构的静态回归)。

    背景: v2.4 把编译档位由 5 档压成 2 档 ——

        定义 MOUSE_REMOTE  -> 远程档(上位机接管: 监视 + 参数 + 手动运动 + 远程发车)
        什么都不定义       -> 按键比赛档(功能与重构前**逐字节一致**)

    同时删除了整层课程调参实验(固件 `APP/lesson_lab.c` + 上位机 `api/lesson.py`)。

    这个重构最危险的失败模式是**静默降级**: 漏改一处 `#ifdef`, 编译器不会报错,
    但远程档会悄悄退化成比赛语义(或反过来)。历史教训就摆在那里 —— v2.1 之前
    "[4] 档不派生 MOUSE_TEST_MODE" 让 `STOP` 解急停整段失效, 一次急停废掉整场采集。

    所以这里改成"**白名单**"式断言: 档位宏只允许 `MOUSE_REMOTE` 一个名字存在,
    旧宏名在整个固件树里出现次数必须为 **0**。将来谁再引入新档位宏, 必须显式
    来这里登记 —— 让它成为一个"有意识"的决定, 而不是一次疏忽。
    """
    print("\n[S8] 档位宏唯一性与课程层清除")

    import os
    import re

    fw = fwprofile.FW_ROOT

    def _read_utf8(path: str, label: str) -> str:
        """严格 UTF-8 读固件源码, 失败时给**可读**的定位信息。

        2026-10-16 真机踩到: 某次手工"复原行尾"把 `USER/main.h` 一处中文注释写坏
        (UTF-8 多字节里被插入一个 0x20), 结果本测试直接抛 UnicodeDecodeError ——
        什么信息都没有。这里把它变成一句能照做的提示。
        约定: 固件源码全树必须是合法 UTF-8, 工作区行尾 CRLF。
        """
        raw = open(path, "rb").read()
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise SystemExit(
                f"\n[S8] 固件源码 {label} **不是合法 UTF-8**: {e}\n"
                f"     坏字节位置 = {e.start}(上下文 {raw[max(0, e.start - 12):e.start + 12]!r})\n"
                f"     常见原因: 手工改行尾(CRLF↔LF)或改档位宏时把中文注释写坏。\n"
                f"     修法: `git checkout -- <该文件>` 取回 HEAD 内容, 再按既有约定\n"
                f"     复原工作区 CRLF(`\\r\\n`), 最后 `git ls-files --eol <该文件>` + "
                f"`git diff` 复核为空。")

    link_h = fwprofile.link_h_text()  # 两代都能直接用档位读 link.h(编码自动探测)
    if fwprofile.FW_PROFILE == "competition":
        # ---- 新基线 code_competition 的档位不变量 ----
        # 为什么不是 S8a: 新工程的档位**不在源码里**(旧工程写在 USER/main.h 的
        # `#define MOUSE_REMOTE 1`) —— 它来自 Keil Target 的 Define, 所以改从
        # .uvprojx 读, 并补两条与"红线"直接挂钩的断言。
        print("       （新基线: 档位在 Keil Target Define, 非源码宏）")
        uvx = fwprofile.read_text(*fwprofile.PROFILE["uvprojx"], errors="replace")
        m1 = re.search(r"<TargetName>Target 1</TargetName>(.*?)<TargetName>Target_Remote</TargetName>",
                       uvx, re.S)
        m2 = re.search(r"<TargetName>Target_Remote</TargetName>(.*)", uvx, re.S)
        check("uvprojx: 找到 Target 1 / Target_Remote 两个 Target", bool(m1) and bool(m2))
        body1 = m1.group(1) if m1 else ""
        body2 = m2.group(1) if m2 else ""
        d1 = (re.search(r"<Define>([^<]*)</Define>", body1) or [None, ""])[1] if body1 else ""
        d2 = (re.search(r"<Define>([^<]*)</Define>", body2) or [None, ""])[1] if body2 else ""
        check("Target 1(比赛档) Define **不含** MOUSE_REMOTE(红线前提)",
              "MOUSE_REMOTE" not in d1, d1)
        check("Target_Remote Define 含 MOUSE_REMOTE,EN_USART1_RX",
              "MOUSE_REMOTE" in d2 and "EN_USART1_RX" in d2, d2)
        _od = re.search(r"<OutputDirectory>([^<]*)</OutputDirectory>", body2)
        check("Target_Remote 输出目录隔离(对象/hex 不与比赛档同目录)",
              bool(_od) and "Remote" in _od.group(1), _od.group(1) if _od else "未找到")
        rem = os.path.join(fw, "REMOTE")
        bad_gate = []
        for fn in sorted(os.listdir(rem)):
            if not fn.endswith(".c"):
                continue
            txt = open(os.path.join(rem, fn), "rb").read().decode("gbk", "replace").rstrip()
            last = txt.splitlines()[-1].strip() if txt else ""
            if "#ifdef MOUSE_REMOTE" not in txt or not last.startswith("#endif"):
                bad_gate.append("%s(末行=%r)" % (fn, last[:40]))
        check("REMOTE/*.c 整文件 #ifdef MOUSE_REMOTE 门控(Keil 会给每个 Target 都编一遍)",
              not bad_gate, str(bad_gate))
        bld_p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "build_competition_modes.py")
        bld = open(bld_p, encoding="utf-8").read()
        check("build_competition_modes.py: 比赛档红线 sha256 常量在位",
              "68ad6be2272818269d6aac78e5356d9bb91036ddeaaa82c4ec3754a8a0948141" in bld)
        check("registry: 无 lesson.* 命令(课程层在新基线同样不存在)",
              not [n for n in COMMANDS if n.startswith("lesson.")])
    else:
        main_h = _read_utf8(os.path.join(fw, "USER", "main.h"), "USER/main.h")
        link_c = _read_utf8(os.path.join(fw, "HARDWARE", "link.c"), "HARDWARE/link.c")
        isr_c = _read_utf8(os.path.join(fw, "HARDWARE", "isr.c"), "HARDWARE/isr.c")

        # S8a: 档位宏白名单 —— main.h 里 `#define MOUSE_*` 只允许 MOUSE_REMOTE
        defined = set(re.findall(r"#define\s+(MOUSE_\w+)", main_h))
        check("main.h: 档位宏只有 MOUSE_REMOTE", defined == {"MOUSE_REMOTE"}, str(sorted(defined)))
        check("main.h: MOUSE_REMOTE 的定义存在(可注释掉, 但不许消失)",
              "#define MOUSE_REMOTE 1" in main_h)

        # S8b: 旧档位宏在整个固件树里**零残留**(含派生宏)。
        #      `.uvprojx` 也要扫 —— 档位宏曾经同时出现在工程文件的预处理定义里。
        LEGACY = ("MOUSE_TEST_NO_MOTOR", "MOUSE_TEST_MOTOR_LAB", "MOUSE_LESSON_LAB",
                  "MOUSE_DATA_LOG", "MOUSE_TEST_MODE", "MOUSE_ESTOP_RECOVERABLE")
        leftover: list = []
        for dirpath, _dirs, files in os.walk(fw):
            if "tools" in dirpath.replace("\\", "/"):
                continue
            for fn in sorted(files):
                if not fn.endswith((".c", ".h", ".uvprojx")):
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    text = open(path, encoding="utf-8", errors="replace").read()
                except OSError:
                    continue
                for name in LEGACY:
                    n = len(re.findall(r"\b" + name + r"\b", text))
                    if n:
                        leftover.append(f"{os.path.relpath(path, fw)}: {name} x{n}")
        check(f"固件树已无旧档位宏({len(LEGACY)} 个名字 x 0 次)",
              not leftover, " | ".join(leftover[:6]))

        # S8c: 课程实验层**整层**清除 —— 源文件不存在、无人 include、命令关键字不在
        check("APP/lesson_lab.c 已删除",
              not os.path.exists(os.path.join(fw, "APP", "lesson_lab.c")))
        check("APP/lesson_lab.h 已删除",
              not os.path.exists(os.path.join(fw, "APP", "lesson_lab.h")))
        inc = [fn for _dp, _d, fs in os.walk(fw) for fn in fs
               if fn.endswith((".c", ".h")) and 'lesson_lab.h"' in
               open(os.path.join(_dp, fn), encoding="utf-8", errors="replace").read()]
        check("无任何源码 include lesson_lab.h", not inc, str(inc))
        check("link.c: 无 LESSON / LESSONLAB 命令关键字或符号",
              ('"LESSON' not in link_c) and ("LESSONLAB_" not in link_c)
              and ("link_Lesson" not in link_c),
              f'字面量={link_c.count(chr(34) + "LESSON")} LESSONLAB_={link_c.count("LESSONLAB_")}')
        # 上位机侧: 课程模块与登记命令都不该还在
        check("registry: 无 lesson.* 命令",
              not [n for n in COMMANDS if n.startswith("lesson.")],
              str(sorted(n for n in COMMANDS if n.startswith("lesson."))))

    # S8d: 生产参数表条目数**必须**等于固件 LINK_P_NUM(远程档语义)。
    #      为什么钉死: `link_CalibLoad()` 判 `cnt != LINK_P_NUM` 即认为整页无效 ——
    #      上位机若与固件条目数不一致, 参数页会**静默作废**, 标定回落默认值。
    #      v2.5: LINK_P_NUM/PROTO_VER/FW_VER 三宏按 MOUSE_REMOTE 门控(两档编译
    #      同一份 link.c) —— 解析规则 = **`#ifdef MOUSE_REMOTE` 分支内的第一个值**;
    #      并断言"远程分支在前"(front_check/pull_smoke 的"首个匹配"解析依赖此顺序)。
    i_ifdef = link_h.find("#ifdef MOUSE_REMOTE")
    i_pn = link_h.find("#define LINK_P_NUM")
    check("link.h: LINK_P_NUM 在 #ifdef MOUSE_REMOTE 之后(远程分支在前, 解析约定)",
          0 <= i_ifdef < i_pn, f"ifdef@{i_ifdef} pnum@{i_pn}")
    m_pn = re.search(r"#define\s+LINK_P_NUM\s+(\d+)", link_h)
    m_pn_c = re.search(r"#else[^\n]*\n\s*#define\s+LINK_P_NUM\s+(\d+)", link_h)
    check("link.h: 找到 LINK_P_NUM(远程 41 / 比赛 24 双值)", bool(m_pn) and bool(m_pn_c))
    if m_pn and m_pn_c:
        n_fw, n_fw_c = int(m_pn.group(1)), int(m_pn_c.group(1))
        check(f"远程档 LINK_P_NUM({n_fw}) == 41(生产表: 1-7 + 22-38 + 39-55)",
              n_fw == 41, str(n_fw))
        check(f"比赛档 LINK_P_NUM({n_fw_c}) == 24(v2.4 表, 产物逐字节不变)",
              n_fw_c == 24, str(n_fw_c))
        check(f"主机 PARAM_IDS 项数({len(PARAM_IDS)}) == 远程档 LINK_P_NUM({n_fw})",
              len(PARAM_IDS) == n_fw, f"{len(PARAM_IDS)} vs {n_fw}")

    # S8e: 标定算参能力**必须保留**(用户明确要求: 课程层可以删, 算参函数不行)。
    #      它们已从 `api/lesson.py` 迁到 `api/calc.py`, 仍是纯函数, 仍从 host.api 导出。
    from host.api import (error_ratio, ir_error_stats, pick_best_position,  # noqa: F401
                          solve_ratio, step_metrics, straightness_metrics,
                          theory_gyro_ratio, theory_track_ratio)
    from host.api import calc as _calc
    calc_need = ("theory_track_ratio", "theory_gyro_ratio", "solve_ratio", "error_ratio",
                 "step_metrics", "straightness_metrics", "ir_error_stats",
                 "pick_best_position")
    miss = [n for n in calc_need if not hasattr(_calc, n)]
    check(f"标定算参 {len(calc_need)} 个纯函数仍在 api/calc.py", not miss, str(miss))
    check("理论里程系数/角度系数可算(默认参数)",
          abs(theory_track_ratio() - 62.24) < 0.05 and abs(theory_gyro_ratio() - 415.8) < 1.0,
          f"{theory_track_ratio():.2f} / {theory_gyro_ratio():.2f}")
    check("solve_ratio 线性标定公式", abs(solve_ratio(62.24, 100.0, 110.0) - 56.58) < 0.02)
    check("error_ratio 相对误差", abs(error_ratio(1091.0, 1080.0) - 1.0185) < 0.01)

    # S8f: `TelemetryPuller.stop()` 必须**先等在途 `TELEM` 收尾**再丢弃它。
    #      为什么钉死(2026-10-07 真机实测, COM7/蓝牙): `TELEM` 一写出串口就**已经在
    #      线上**了, 固件要等下一个 1 ms tick 才消费它, 期间到达的字节被 `usart.c`
    #      直接丢弃。若 `off()` 立刻返回, 紧接着发来的命令(最自然的"先停流、再发车"
    #      写法)有 **~3/4 概率首发被丢** ⇒ 只能干等 2.5 s 超时重发。实测修复前
    #      223/**2702**/**2700**/**2705** ms, 修复后 0/10。
    #      v2.10 起停等/批式统一为一个类(剖面 n), 契约照旧钉在同一个 stop() 上。
    from host.api.stream import TelemetryPuller as _PR
    check(f"TelemetryPuller.STOP_QUIESCE_MAX_S ≥ 0.3s(实测 RTT ≈300 ms)",
          float(getattr(_PR, "STOP_QUIESCE_MAX_S", 0.0)) >= 0.3,
          str(getattr(_PR, "STOP_QUIESCE_MAX_S", None)))
    _src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "host", "api", "stream.py"), encoding="utf-8").read()
    _stop_body = _src.split("def stop(self)")[1].split("\n    def ")[0]
    check("stop() 丢弃前会等 p.done 收尾(不再无条件 abandon)",
          ("not p.done" in _stop_body) and ("abandon" in _stop_body),
          f"等p.done={'not p.done' in _stop_body} abandon={'abandon' in _stop_body}")

    import os
    import re

    fw = fwprofile.FW_ROOT
    main_h = isr_c = ""  # 只有课程固件有这两个文件(新基线的档位在 Keil Target 里)
    if fwprofile.FW_PROFILE == "competition":
        link_c = open(os.path.join(fw, "REMOTE", "link.c"), "rb").read().decode("gbk", "replace")
    else:
        link_c = open(os.path.join(fw, "HARDWARE", "link.c"), encoding="utf-8").read()

    # ---- 以下为与档位无关的固件静态不变量 ----
    print("\n[S9] 固件静态不变量(Flash 布局 / 注释闭合 / 帧 sink)")

    # S5e: 程序区上界必须与 Flash 数据区起始页严格对齐。
    #      uvprojx 内 0x8000000 出现多次(全芯片描述 + 实际启用的 IROM1),
    #      故取其中**最小**的 Size 作为"程序区上界"; 它若越过 FLASH_ADDR_DATA_NUM,
    #      擦写标定/迷宫数据会覆盖代码区(现场表现为"标定莫名丢失"), 故钉住此不变量。
    if fwprofile.FW_PROFILE == "competition":
        uv = fwprofile.read_text(*fwprofile.PROFILE["uvprojx"], errors="replace")
        flash_h = fwprofile.read_text("HARDWARE", "STMFLASH", "stmflash.h", errors="replace")
        flash_c = fwprofile.read_text("HARDWARE", "STMFLASH", "stmflash.c", errors="replace")
    else:
        uv = open(os.path.join(fw, "USER", "Mouse.uvprojx"), encoding="utf-8").read()
        flash_h = open(os.path.join(fw, "HARDWARE", "flash.h"), encoding="utf-8").read()
        flash_c = open(os.path.join(fw, "HARDWARE", "flash.c"), encoding="utf-8").read()
    link_h = fwprofile.link_h_text()
    sizes = [int(sz, 16) for addr, sz in
             re.findall(r"<StartAddress>(0x[0-9a-fA-F]+)</StartAddress>\s*<Size>(0x[0-9a-fA-F]+)</Size>", uv)
             if int(addr, 16) == 0x08000000]
    m_data = re.search(r"#define\s+FLASH_ADDR_DATA_NUM\s+(0X[0-9a-fA-F]+)", flash_h)
    check("uvprojx: 找到 0x08000000 的程序区描述", bool(sizes), str(sizes))
    check("flash.h: 找到 FLASH_ADDR_DATA_NUM", bool(m_data))
    if fwprofile.FW_PROFILE == "competition":
        # 新基线的 IROM1 是**整片 0x20000**(不像旧工程把上界卡在数据区起始) ⇒
        # 链接器不会因为"代码长过数据页"而报错, 必须在此断言编译产物的 ROM 上界
        # 仍在第一个数据页之下, 否则标定/迷宫页会被代码踩掉(现场表现为标定莫名丢失)。
        _map = os.path.join(fw, "USER", "TEST.map")
        _rom = None
        if os.path.exists(_map):
            _mt = open(_map, encoding="utf-8", errors="replace").read()
            # 取 Load Region 的 Size —— 它才是"真正要烧进 Flash 的字节数"(含 RW 初值)。
            # 注: 新工程开了镜像压缩, map 里另有一个 COMPRESSED[0x13?] 的值; 用未压缩的
            # 装载区大小做上界判断更保守(压缩只会更小)。
            _mm = re.search(r"Load Region \S+ \(Base: [^,]+, Size: (0x[0-9a-fA-F]+)", _mt)
            _rom = int(_mm.group(1), 16) if _mm else None
        check("TEST.map: 解析到装载区(装入 Flash 的字节数)", _rom is not None, str(_rom))
        if _rom is not None and m_data:
            _dstart = int(m_data.group(1), 16) - 0x08000000
            check(f"编译产物 ROM({_rom} B = 0x{_rom:X}) < 数据区起始 0x{_dstart:X}(代码不踩数据页)",
                  _rom < _dstart, f"rom={_rom} data=0x{_dstart:X}")
    elif sizes and m_data:
        data_start = int(m_data.group(1), 16) - 0x08000000
        check(f"程序区上界 0x{min(sizes):X} == 数据区起始 0x{data_start:X}(不越界)",
              min(sizes) == data_start, f"sizes={[hex(x) for x in sizes]} data=0x{data_start:X}")

    def addr_of(macro: str, text: str):
        mm = re.search(r"#define\s+" + macro + r"\s+(0[xX][0-9a-fA-F]+)", text)
        return int(mm.group(1), 16) if mm else None

    if fwprofile.FW_PROFILE == "competition":
        # ---- 新基线的 Flash 页布局不变量 ----
        # 旧工程的 S9a 钉的是"标定页别落在运行数据区擦除范围里"(靠 flash.c 的 saveNum 算页数)。
        # 新基线没有 saveNum/EraseRunData(飞行记录是 stmflash.c 的 run_data 顺序写), 所以
        # 换成**纯头文件层面**的等价不变量: 所有 FLASH_ADDR_* 页 1KB 对齐、互不重叠,
        # 且远程层自用的 SYSCALIB 页不与固件任何一页冲突(固件页 = 数据 88/89 + 迷宫 118-127)。
        fw_pages = {}
        for name, val in re.findall(r"#define\s+(FLASH_ADDR_\w+)\s+(0[xX][0-9a-fA-F]+)", flash_h):
            fw_pages.setdefault(int(val, 16), []).append(name)
        check(f"stmflash.h: 解析出 {len(fw_pages)} 个互异的 FLASH_ADDR_* 页",
              len(fw_pages) >= 8, str(sorted(fw_pages)))
        bad_align = [f"{hex(a)}={n}" for a, n in fw_pages.items() if (a - 0x08000000) % 0x400]
        check("所有 FLASH_ADDR_* 均 1KB 对齐", not bad_align, str(bad_align))
        in_range = [f"{hex(a)}={n}" for a, n in fw_pages.items()
                    if not (0x08000000 <= a < 0x08020000)]
        check("所有 FLASH_ADDR_* 落在片内 128KB", not in_range, str(in_range))
        _sc = addr_of("FLASH_ADDR_SYSCALIB", link_h)
        check("link.h: 找到远程层 SYSCALIB 页", _sc is not None)
        if _sc is not None:
            check(f"SYSCALIB(page {(_sc - 0x08000000) // 0x400}) 1KB 对齐",
                  (_sc - 0x08000000) % 0x400 == 0)
            clash = [n for a, n in fw_pages.items() if a == _sc]
            check("SYSCALIB 页不与固件任何一页重合", not clash, str(clash))
            check(f"SYSCALIB(page {(_sc - 0x08000000) // 0x400}) < 迷宫起始页 118"
                  "(远程层页在两代里都落在数据区之前的空白段)",
                  _sc < 0x0801D800, hex(_sc))
    else:
        # S9a: Flash 页布局不得互相踩踏。
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
            # v2.4: FLASH_ADDR_LESSONPARAM(课程参数页)随课程层删除, 只剩生产标定页。
            for macro_name in ("FLASH_ADDR_SYSCALIB",):
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

    # S9b: 源码"注释提前闭合"扫描。
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
                        # ⚠️ 词法顺序: **行注释优先于块注释** —— 固件源码里常见的
                        # `//************` 横幅, 其 `//*` 里就含一个 `/*`; 不先判 `//`
                        # 就会把整条横幅当成"块注释开始", 一路吞到文件后面某个 `*/`
                        # 才闭合, 然后把中间某行误报成"注释提前闭合"(2026-10-16 实测:
                        # code_competition/SYSTEM/sys/sys.c 的横幅把 127 行误报)。
                        c = rest.find("//")
                        if 0 <= c < o:
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

    # S9c: 每个帧 sink 必须用**自己的** stream 名去 notify。
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

    # S9d: 标定页的 crc 槽**不得先写 0 占位**(2026-10-07 真机实测揪出的缺陷)。
    #      背景: `link_CalibSave()` 原先在 +8 先写个 0 当"crc 占位", 末尾再回填真 CRC;
    #      但 F1 的 Flash 编程**只能把位从 1 清成 0**, 而 `FLASH_WriteHalfWord` 又没有
    #      "目标非 0xFFFF 就跳过/先擦"的保护 ⇒ 存进去的 CRC 恒为 `0x0000 AND crc = 0`
    #      ⇒ `link_CalibLoad()` 的 `crcF != crcC` **永远成立**、整页被判无效
    #      ⇒ 参数每次上电都回落 `link_ParamFromSys()` 的源码默认值, `SAVE` 形同虚设。
    #      症状极隐蔽: `GET`/`SETQ`/`GET ALL` 走 RAM 的 `linkParam[]`, 与 Flash 页无关,
    #      所以"写→读"全程正常; 只有"改值 → SAVE → 复位 → 再看"才暴露。判据脚本
    #      `host/params_persist_probe.py`。修法: 让 +8 保持擦除态 0xFFFF, 末尾写一次。
    _save_body = link_c.split("link_CalibSave(void)")[1].split("static u8 link_CalibLoad")[0]
    _gated = ("#ifdef MOUSE_REMOTE" in _save_body) and ("#else" in _save_body)
    check("link_CalibSave: crc 槽修复被 #ifdef MOUSE_REMOTE 门控(比赛档逐字节不变)", _gated)
    _remote = _save_body.split("#ifdef MOUSE_REMOTE", 1)[1].split("#else", 1)[0] if _gated else ""
    check("link_CalibSave(远程档): 不做 crc 占位写(否则存储的 CRC 恒为 0)",
          bool(_remote) and "FLASH_WriteHalfWord(addr, 0)" not in _remote,
          f"占位写={'FLASH_WriteHalfWord(addr, 0)' in _remote}")
    check("link_CalibSave: 末尾仍把真 CRC 写回 +8",
          "FLASH_ADDR_SYSCALIB + 8" in _save_body, "末尾回填缺失 ⇒ 页面永远无效")

    # v2.4: 课程段已删除 —— 生产段就是全集, 两者必须**完全相等**
    # (若不等, `get_all` 的等待集合会多等或漏等, 表现为"偶发超时")
    check("生产段 == 全集(24 项, 无课程段)",
          set(PRODUCTION_PARAM_IDS) == set(PARAM_IDS),
          f"prod={len(PRODUCTION_PARAM_IDS)} all={len(PARAM_IDS)}")

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
        # v2.4: `lesson` 子命令随课程层删除; 其余保持"现存子命令全覆盖"的检出力。
        need = {"ports", "connect", "get", "get-all", "set", "calib", "save",
                "maze", "monitor", "motor-test", "estop", "diagnose", "scan-baud",
                "replay", "datalog"}
        missing = need - choices
        check(f"CLI 可导入且子命令齐备({len(choices)} 个, 含 motor-test/datalog)",
              not missing, f"缺 {sorted(missing)}")
        check("CLI 已无 lesson 子命令", "lesson" not in choices)
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


def test_maze_run_target() -> None:
    """[S2b] `MAZE RUN` 的目标词拼装 —— 默认 BLIND 必须**省略**, 保持旧日志格式。"""
    print("\n[S2b] MAZE RUN 目标词")
    check("默认 = 'MAZE RUN'(不带词)",
          command_line("maze.run") == "MAZE RUN", command_line("maze.run"))
    check("target='BLIND' 仍省略",
          command_line("maze.run", target="BLIND") == "MAZE RUN",
          command_line("maze.run", target="BLIND"))
    check("target='RACE' 显式带上",
          command_line("maze.run", target="RACE") == "MAZE RUN RACE",
          command_line("maze.run", target="RACE"))
    check("小写输入归一化为大写",
          command_line("maze.run", target="race") == "MAZE RUN RACE",
          command_line("maze.run", target="race"))


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
            # ---- 时间同步 / 数据收集 ----
            # maze.stop 收车并复位发车状态 —— 不加它的话设备会停在"探索中",
            # 而 maze.run 的 busy 判据正是"请求位或运行位"（v2.1 真机缺陷的落点）。
            "arm", "maze.run", "maze.stop",
            # ---- 速率族(v2.6): 只设节拍, 每条都立刻 OFF 收尾, 免得干扰后续用例 ----
            # hz 用最低档(见 _EXAMPLE_ARGS), PULL 只发 1 帧。
            "rate.fix", "rate.off", "rate.pull", "rate.off",
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

        # v2.4: 发车目标词也要各跑一次(默认 BLIND 在上面已覆盖)
        r = mc.send_cmd("maze.run", target="RACE", timeout=5.0)
        check("maze.run RACE: ACK OK", r.ok, f"{r.status} cmd={r.data.get('cmd')!r}")
        time.sleep(0.25)
        r = mc.send_cmd("maze.stop", timeout=5.0)
        check("maze.stop(RACE 后): ACK OK", r.ok, r.status)
        r = mc.send_cmd("maze.run", target="NOPE", timeout=5.0)
        check("maze.run 非法目标被拒", not r.ok, f"{r.status}(拼装阶段就该拦下)")

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
    """[D2] 参数读写往返(24 项生产表)。

    v2.4: 原先这里还要单独验"课程参数 GET 回 ERR_NAME(`ERR_NAME`)"——
    固件的 `GET <name>` 只查生产表, 而写侧有课程分支, 形成"写得进、读不回"的
    不对称。课程层整体删除后**不再有第二个命名空间**, 于是本用例简化为一条:
    生产表 24 项逐项"读 -> 写回同一值 -> 再读", 必须逐位一致。

    为什么必须写成断言而不是"跳过": 模拟器早期版本回全部 38 项, 掩盖了真机上
    的 `ERR_NAME`, 让"读参数偶发失败"变成一个随机故障。现在两侧都只有一张表。
    """
    print("\n[D2] 参数读写往返(24 项)")
    from host.api import MouseClient

    with MouseClient.connect_to(f"tcp://127.0.0.1:{port}") as mc:
        check("生产段 == 全集(24 项)",
              set(PRODUCTION_PARAM_IDS) == set(PARAM_IDS),
              f"prod={len(PRODUCTION_PARAM_IDS)} all={len(PARAM_IDS)}")

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

        # ---- 已删除的课程参数名必须**彻底不认**(防"半删"留下幻影项) ----
        for name in ("KPL", "KIR", "GYROP", "SENSORMODE", "WALLMODE"):
            r = mc.send_cmd("params.get", name=name, timeout=3.0)
            check(f"{name}: 已随课程层删除, GET 回 ERR_NAME",
                  (not r.ok), f"{r.status}")


# ---------------------------------------------------------------- 示例参数

#: 示例实参 —— 键必须覆盖全部 `CommandSpec.args` 的并集。
#: v2.0 命令集精简到 18 条后, 与旧帧相关的键(cond/mask/dom/prof)全部删除;
#: v2.6 速率族又把 `hz` 带了回来(语义不同: 固定节拍的帧率)。
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
    # ---- 迷宫发车目标(v2.4: `MAZE RUN [BLIND|RACE]`) ----
    "target": "RACE",
    # ---- 速率族(v2.6) ----
    # hz 取 1: 硬件用例里 `RATE FIX` 会**真的**开始连续发帧, 用最低档 + 紧跟 rate.off 收尾。
    "hz": 1,
    "n": 1,
    "t_ms": 500,
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
        # ---- v2.3: 前墙双档 / 比赛冲刺状态 ----
        "frontNear": 1, "frontFar": 0, "frontBoth": 0,
        "spurtMode": 3, "spurtFlag": 1, "goOrBack": 0,
        "spurtTestFlag": 1, "objectGoToFlag": 0,
        "goaheadnum": 5, "fastwayCount90": 7,
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
    test_maze_run_target()
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
