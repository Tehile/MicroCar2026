"""固件档位解析 —— 上位机侧唯一的"固件真源"入口。

**为什么需要它**：2026-10-16 起工程里**两代固件并存**：

| 档位 | 目录 | 迷宫 | fw | link.h 位置 | 动作枚举 |
|---|---|---|---|---|---|
| `competition`（默认，新基线） | `code_competition/` | MAZETYPE **16** | **310** | `REMOTE/link.h` | `#define`(timer.h + utility.h) |
| `legacy`（课程固件，历史参考） | `code_stm32f1_mouse/` | MAZETYPE 7 | 222 | `HARDWARE/link.h` | `typedef enum`(core.h) |

两代的**目录布局不同**，而离线套件与探针都遵循一条纪律：**版本号从不写死，一律
"从固件头文件现读"**（写死就会变成"改测试让它过"，而不是"发现两侧漂移"）。
把这件事实集中在本模块，避免十来个脚本各自硬编码 `code_stm32f1_mouse`。

**选择方式**：环境变量 `RC_FW_PROFILE`（默认 `competition`）。

    python pull_smoke.py                          # 按新基线校验
    RC_FW_PROFILE=legacy python pull_smoke.py      # 按课程固件校验

`fw_expect()` 的返回值同时用于：`PONG` 指纹核对（sim 与固件必须一致）、
`front_check`/`pull_smoke` 的设备指纹断言。
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Sequence, Tuple

# host/host/fwprofile.py -> host/host/ -> host/ -> RemoteControl/
_HERE = os.path.dirname(os.path.abspath(__file__))
_RC = os.path.dirname(os.path.dirname(_HERE))

#: 固件档位表。`motion` 是"动作码 -> 名字"的来源文件列表（逐文件解析 `#define 名字 值`）。
PROFILES: Dict[str, dict] = {
    "competition": {
        "dir": "code_competition",
        "label": "新基线 code_competition（MAZETYPE 16 / fw 310 / 单工程双 Target）",
        "mazetype": 16,
        "link_h": ("REMOTE", "link.h"),
        "uvprojx": ("USER", "TEST.uvprojx"),
        # 动作码在**连续区块**里（timer.h 的 NULL..SMARTSTRAIGHTSPURTL、
        # utility.h 的 LT90SEARCH..RT90SPURT）—— 必须按块取，否则会把 MAZETYPE/
        # MAXPWM/SEARCHSPEED 这类无关常量一起收进来（实测不设界会得到 72 个"码"）。
        "motion": (
            {"path": ("HARDWARE", "TIMER", "timer.h"),
             "from": "#define NULL", "to": "#define SMARTSTRAIGHTSPURTL"},
            {"path": ("UTILITY", "utility.h"),
             "from": "#define LT90SEARCH", "to": "#define RT90SPURT"},
        ),
        "maze": ("STRATAGY", "stratagy.h"),
        # 新固件 `GucMouseTask` 比旧的多 END=6（跑位收尾态）
        "maze_macros": ("WAIT", "START", "MAZESEARCH", "SPURT", "SPURT_45", "SPURT_90", "END"),
        "spurt": ("UTILITY", "utility.h"),
        "spurt_re": r"#define\s+(?:LOW_90|LOW|MEDIUM|HIGH)\s+(\d+)\b",
        "spurt_expect": [60, 61, 62, 63],
    },
    "legacy": {
        "dir": "code_stm32f1_mouse",
        "label": "课程固件 code_stm32f1_mouse（MAZETYPE 7 / fw 222，历史参考）",
        "mazetype": 7,
        "link_h": ("HARDWARE", "link.h"),
        "uvprojx": ("USER", "Mouse.uvprojx"),
        "motion": ({"path": ("HARDWARE", "core.h"), "enum": "MotionTask_t"},),
        "maze": ("STRATEGY", "maze.h"),
        "maze_macros": ("WAIT", "START", "MAZESEARCH", "SPURT", "SPURT_45", "SPURT_90"),
        "spurt": ("MOTION", "spurt.h"),
        "spurt_re": r"SPURT_SPEED_\w+\s*=\s*(\d+)",
        "spurt_expect": [60, 61, 62, 63],
    },
}

#: 当前档位（环境变量 `RC_FW_PROFILE`，非法值回落默认档）
FW_PROFILE: str = (os.environ.get("RC_FW_PROFILE") or "competition").strip().lower()
if FW_PROFILE not in PROFILES:
    FW_PROFILE = "competition"
PROFILE: dict = PROFILES[FW_PROFILE]

#: 固件根目录（绝对路径）
FW_ROOT: str = os.path.join(_RC, PROFILE["dir"])


def fw_path(*parts: str) -> str:
    """固件根目录下的路径。"""
    return os.path.join(FW_ROOT, *parts)


def _decode(raw: bytes, errors: str = "strict") -> str:
    """按档位探测编码解码固件源码。

    ⚠️ **两代固件的编码不同**：`legacy` 全树是合法 UTF-8（契约测试 S8 有断言）；
    而 `competition` 的远程层（`REMOTE/*.c/.h`）跟着固件走 **GBK**（见方案 §3.2）——
    它们要和 14 个 GBK 固件文件互相 include，混编码必炸。
    所以这里 UTF-8 试一次、GBK 试一次，最后才退化成"替换非法字节"（ASCII 的宏定义
    与数值不受影响，中文注释会变问号）。
    """
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors=errors if errors != "strict" else "replace")


def read_text(*parts: str, errors: str = "strict") -> str:
    """读固件源码（自动 UTF-8/GBK；`errors="strict"` 时不会静默损坏文本）。"""
    with open(fw_path(*parts), "rb") as fh:
        return _decode(fh.read(), errors)


def link_h_text() -> str:
    """当前档位的 `link.h` 文本。"""
    return read_text(*PROFILE["link_h"])


def link_macro(name: str, text: Optional[str] = None) -> Optional[int]:
    """读 `link.h` 里的一个宏值（十进制或 0x…），读不到返回 None。

    ⚠️ 取**首次匹配** —— 两档 `LINK_P_NUM`/`LINK_PROTO_VER`/`LINK_FW_VER` 都按
    `#ifdef MOUSE_REMOTE` 门控，远程分支在前（`contract_test` 的 S8d 会钉这个顺序）。
    """
    if text is None:
        text = link_h_text()
    m = re.search(r"#define\s+" + re.escape(name) + r"\s+(0[xX][0-9a-fA-F]+|\d+)", text)
    return int(m.group(1), 0) if m else None


def fw_expect() -> Optional[Tuple[int, int]]:
    """从固件 `link.h` 现读 `(LINK_PROTO_VER, LINK_FW_VER)`，读不到返回 None。"""
    try:
        t = link_h_text()
        pv, fv = link_macro("LINK_PROTO_VER", t), link_macro("LINK_FW_VER", t)
        return (pv, fv) if pv is not None and fv is not None else None
    except OSError:
        return None


def _anchor_re(anchor: str) -> str:
    """锚点 -> 空白容错的匹配式（固件头文件里 `#define` 后常见是**制表符**，
    用 `str.find` 会因为一个空格之差匹配不上，切片就静默失效）。"""
    return r"\s+".join(re.escape(p) for p in anchor.split())


def _slice(text: str, start: Optional[str], end: Optional[str]) -> str:
    """取 `start` 锚点到 `end` 锚点之间的片段（锚点缺失时**抛错**，不静默放行）。"""
    if start:
        m = re.search(_anchor_re(start), text)
        if not m:
            raise ValueError("动作码块起始锚点找不到: %r" % start)
        text = text[m.start():]
    if end:
        m = re.search(_anchor_re(end), text)
        if not m:
            raise ValueError("动作码块结束锚点找不到: %r" % end)
        nl = text.find("\n", m.end())
        text = text[:nl if nl >= 0 else len(text)]  # 含结束锚点**整行**(它自己也是动作码)
    return text


def motion_task_map() -> Dict[int, str]:
    """动作码表。

    · 新固件 `competition`：两个头文件里的 `#define <名字> <值>` **连续区块**
      （见 `PROFILE["motion"]` 的 from/to 锚点，避免把无关常量当动作码）；
    · 旧固件 `legacy`：`core.h` 的 `typedef enum {...} MotionTask_t`。
    """
    out: Dict[int, str] = {}
    for spec in PROFILE["motion"]:
        try:
            text = read_text(*spec["path"], errors="replace")
        except OSError:
            continue
        if spec.get("enum"):
            m = re.search(r"typedef\s+enum\s*\{(.*?)\}\s*" + spec["enum"] + r"\s*;", text, re.S)
            if not m:
                continue
            b = re.sub(r"/\*.*?\*/", " ", m.group(1), flags=re.S)
            b = re.sub(r"//[^\n]*", " ", b)
            for n, v in re.findall(r"(\w+)\s*=\s*(\d+)", b):
                out.setdefault(int(v), n)
        else:
            body = _slice(text, spec.get("from"), spec.get("to"))
            for n, v in re.findall(r"#define\s+(\w+)\s+(\d+)\b", body):
                out.setdefault(int(v), n)
    return out


def maze_task_map() -> Dict[int, str]:
    """迷宫任务码表（`GucMouseTask`）。"""
    text = read_text(*PROFILE["maze"], errors="replace")
    out: Dict[int, str] = {}
    for name in PROFILE["maze_macros"]:
        m = re.search(r"#define\s+" + name + r"\s+(\d+)\b", text)
        if m:
            out[int(m.group(1))] = name
    return out


def spurt_speeds() -> List[int]:
    """冲刺档位码（应恒为 60..63；两代含义相同，来源文件不同）。"""
    text = read_text(*PROFILE["spurt"], errors="replace")
    return sorted(int(v) for v in re.findall(PROFILE["spurt_re"], text))


def profile_line() -> str:
    """一行档位摘要，供各套件打印（一眼看出这次是在验哪代固件）。"""
    exp = fw_expect()
    fw = f"PROTO {exp[0]} / FW {exp[1]}" if exp else "版本读不到"
    return f"[固件档位] {FW_PROFILE} —— {PROFILE['label']}；{fw}"
