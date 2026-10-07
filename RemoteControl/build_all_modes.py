"""按两个档位逐个重编固件, 并记录真实的 ROM/RAM 与 Error/Warning 数。

为什么需要它: 档位宏写在 `USER/main.h` 里, 手改宏 + 手敲 UV4 命令行 + 抄链接器
输出, 重复两次仍然容易出错(本项目就曾出现过"最后一次编译停在别的档位, 但 Mouse.hex
被当成目标档"的事故)。所以做成脚本: 改宏 -> 编译 -> 解析 -> 复原。

两个档位(v2.4 起的双档简化方案, 见 docs/双档简化设计方案.md):
    R  远程档    `#define MOUSE_REMOTE 1`  —— 上位机接管(监视 + 参数 + 手动运动 + 远程发车)
    C  按键比赛档 不定义任何宏            —— 与原比赛固件逐字节一致, 命令只在板上按键

⚠️ 改写 main.h 时**必须保留该行尾部的说明注释**(`/* 远程档(...) */`),
那是读代码的人唯一的档位索引; 脚本按"宏名 -> 注释"配对保留原样回写。

用法(仓库根目录):
    python build_all_modes.py                 # 编译两档并打印, 结束恢复到 R(远程档)
    python build_all_modes.py --keep-last C   # 指定结束时恢复到哪一档(R / C)
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
FW = os.path.join(ROOT, "code_stm32f1_mouse")
MAIN_H = os.path.join(FW, "USER", "main.h")
UV4 = r"C:\Keil_v5\UV4\UV4.exe"
PROJ = os.path.join(FW, "USER", "Mouse.uvprojx")

#: (档位标签, 宏名(None = 全不定义), 说明) —— 顺序即 main.h 里的顺序
MODES: "list[tuple[str, str | None, str]]" = [
    ("R", "MOUSE_REMOTE", "远程档"),
    ("C", None, "按键比赛档(全不定义)"),
]
_NAMES = ["MOUSE_REMOTE"]

#: 识别一行的形态: 可选注释前缀 + #define <宏> 1 + 尾部说明注释
_ROW_RE = re.compile(
    r"^\s*(?:/\*\s*)?#define\s+(" + "|".join(_NAMES) + r")\s+1\s*(?:\*/)?\s*(/\*.*\*/)\s*$"
)


def _read_rows() -> "dict[str, str]":
    """返回 {宏名: 尾部说明注释} —— 同时校验这一行在。"""
    found = {}
    for ln in open(MAIN_H, encoding="utf-8").read().splitlines():
        m = _ROW_RE.match(ln)
        if m:
            found[m.group(1)] = m.group(2)
    missing = [n for n in _NAMES if n not in found]
    if missing:
        raise SystemExit(f"main.h 里没找到档位宏行: {missing}")
    return found


def set_mode(macro: str | None) -> None:
    """把 main.h 设成"只有 macro 生效"(macro=None 表示该宏注释掉 = 按键比赛档)。"""
    notes = _read_rows()
    lines = open(MAIN_H, encoding="utf-8").read().splitlines(keepends=True)
    out = []
    seen = set()
    for ln in lines:
        m = _ROW_RE.match(ln)
        if not m:
            out.append(ln)
            continue
        name = m.group(1)
        seen.add(name)
        note = notes[name]
        eol = "\n" if ln.endswith("\n") else ""
        if name == macro:
            out.append(f"#define {name} 1 {note}{eol}")
        else:
            out.append(f"/* #define {name} 1 */ {note}{eol}")
    if seen != set(_NAMES):
        raise SystemExit(f"改写的行数异常: {sorted(seen)}")
    with open(MAIN_H, "w", encoding="utf-8", newline="") as f:
        f.write("".join(out))


def build(tag: str) -> dict:
    # ⚠️ 日志落到系统临时目录, **不要写在仓库根** —— 反复跑会在根目录越积越多。
    # 内容已经解析进返回值与下方汇总表, 没有留在仓库里的必要。
    log = os.path.join(tempfile.gettempdir(), f"mouse_build_{tag}.log")
    proc = subprocess.run([UV4, "-b", PROJ, "-o", log], capture_output=True, text=True)
    text = open(log, encoding="utf-8", errors="replace").read() if os.path.exists(log) else ""
    # UV4 的 exit code 不可靠(0=无错 / 1=仅有告警 / 其它), 一律以日志为准
    m_err = re.search(r"(\d+) Error\(s\), (\d+) Warning\(s\)", text)
    err, warn = (int(m_err.group(1)), int(m_err.group(2))) if m_err else (-1, -1)
    m_size = re.search(r"Program Size: Code=(\d+) RO-data=(\d+) RW-data=(\d+) ZI-data=(\d+)", text)
    code, ro, rw, zi = (int(x) for x in m_size.groups()) if m_size else (0, 0, 0, 0)
    errs = [l.strip() for l in text.splitlines() if re.search(r"error:\s*#\d+", l)]
    return {"tag": tag, "err": err, "warn": warn, "rc": proc.returncode,
            "code": code, "ro": ro, "rw": rw, "zi": zi,
            "rom": code + ro + rw, "ram": rw + zi, "errs": errs}


def main() -> int:
    keep = "R"
    if "--keep-last" in sys.argv:
        keep = sys.argv[sys.argv.index("--keep-last") + 1]
    if keep not in dict((t, 1) for t, _, _ in MODES):
        raise SystemExit(f"--keep-last 只能是 {[t for t, _, _ in MODES]}")

    rows = []
    for tag, macro, desc in MODES:
        print(f"\n===== [{tag}] {desc}  ({macro or '全不定义'}) =====", flush=True)
        set_mode(macro)
        r = build(tag)
        r["desc"] = desc
        rows.append(r)
        print(f"[{tag}] {'OK' if r['err'] == 0 else 'FAIL'}  "
              f"Error {r['err']}  Warning {r['warn']}  "
              f"ROM {r['rom']} B (Code {r['code']} + RO {r['ro']} + RW {r['rw']})  "
              f"RAM {r['ram']} B", flush=True)
        for e in r["errs"][:5]:
            print("      " + e, flush=True)

    keep_macro = dict((t, m) for t, m, _ in MODES)[keep]
    set_mode(keep_macro)
    build(keep)
    print(f"\n[复原] main.h 已设回 [{keep}], 并重编 -> 包里放的就是这一档的 Mouse.hex")

    print("\n" + "=" * 78)
    print(f"{'档':<4}{'ROM':>9}{'RAM':>9}{'Err':>6}{'Warn':>7}  说明")
    print("-" * 78)
    bad = 0
    for r in rows:
        print(f"[{r['tag']}] {r['rom']:>7} {r['ram']:>8} {r['err']:>6} {r['warn']:>6}  {r['desc']}")
        if r["err"] != 0:
            bad += 1
    print("=" * 78)
    print("结论: 两档全部 0 Error" if bad == 0 else f"结论: {bad} 个档位有错误!")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
