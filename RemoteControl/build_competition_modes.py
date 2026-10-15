r"""按 Target 逐个重编 code_competition 固件, 记录 ROM/RAM/告警数并抄录 hex sha256。

为什么需要它: `code_competition/USER/TEST.uvprojx` 是**单工程双 Target**
(Target 1 比赛档源码零改动 / Target_Remote 远程档), 档位由 Keil `-t` 选择;
手敲命令行 + 手抄哈希容易出错, 且**比赛档 hex 是红线**——每次改固件都要能
一键证明它没变(见 docs/比赛固件远程化移植方案.md P0 验收)。

两个档位(2026-10-16 建, P0.2):
    Target 1       比赛档    Define=STM32F10X_MD,USE_STDPERIPH_DRIVE
                   源码零改动, hex 必须与基线逐字节相同
    Target_Remote  远程档   上述 + MOUSE_REMOTE,EN_USART1_RX
                   输出目录 USER\Remote\(对象/hex 与比赛档隔离)
                   飞行记录(FLASHSAVE)关闭, RAM 20480->10960, 让位链路层

默认**全量重编**(先删本档构建产物再编, ~2s/档): 增量构建时日志里没有
Program Size 行, 抄不到 ROM/RAM, 红线校验也会被"没重编"架空。

⚠️ UV4 的 exit code 不可靠(0=无错 / 1=仅有告警 / 2=有错), 一律以构建日志
   "N Error(s)" 为准(与 build_all_modes.py 同一教训)。

用法(仓库根目录):
    python build_competition_modes.py                        # 两档全量重编 + 红线校验
    python build_competition_modes.py --only Target_Remote   # 只编某一档
    python build_competition_modes.py --incremental          # 不删产物, 增量编
    python build_competition_modes.py --no-redline           # 跳过红线校验(改基线时用)
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
FW = os.path.join(ROOT, "code_competition")
UV4 = r"C:\Keil_v5\UV4\UV4.exe"
PROJ = os.path.join(FW, "USER", "TEST.uvprojx")

#: 比赛档 hex 基线(本机 Keil V5.06 全量构建, 2026-10-16; ROM 82040 / RAM 20480)
#: 改这个值 = 承认比赛档二进制变了, 必须在提交信息里说明原因。
REDLINE_SHA256 = "68ad6be2272818269d6aac78e5356d9bb91036ddeaaa82c4ec3754a8a0948141"

#: (Target 名, hex 相对 USER 目录的路径, 说明)
TARGETS: "list[tuple[str, str, str]]" = [
    ("Target 1", "TEST.hex", "比赛档(源码零改动, 红线)"),
    ("Target_Remote", os.path.join("Remote", "TEST.hex"), "远程档(MOUSE_REMOTE+EN_USART1_RX)"),
]
_NAMES = [t for t, _, _ in TARGETS]

#: 构建产物扩展名(源码/工程/配置都不在这些后缀里, 见 .gitignore 同款清单)
_ART_EXTS = (".o", ".d", ".crf", ".lst", ".lnp", ".dep", ".axf", ".hex", ".map", ".htm")


def _clean(tag: str) -> int:
    """删掉该 Target 输出目录里的构建产物(全量重编的前置)。返回删除个数。"""
    outdir = os.path.dirname(PROJ) if tag == "Target 1" else os.path.join(os.path.dirname(PROJ), "Remote")
    n = 0
    if os.path.isdir(outdir):
        for f in sorted(os.listdir(outdir)):
            if f.endswith(_ART_EXTS):
                os.remove(os.path.join(outdir, f))
                n += 1
    return n


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def build(tag: str, clean: bool = True) -> dict:
    if clean:
        n = _clean(tag)
        print(f"[{tag}] 已清构建产物 {n} 个 -> 全量重编", flush=True)
    # ⚠️ 日志落系统临时目录, 不写仓库(同 build_all_modes.py 的纪律)
    log = os.path.join(tempfile.gettempdir(), f"cc_build_{tag}.log")
    proc = subprocess.run([UV4, "-b", PROJ, "-t", tag, "-o", log],
                          capture_output=True, text=True)
    text = open(log, encoding="utf-8", errors="replace").read() if os.path.exists(log) else ""
    m_err = re.search(r'"([^"]+)" - (\d+) Error\(s\), (\d+) Warning\(s\)', text)
    if not m_err:
        m_err = re.search(r"(\d+) Error\(s\), (\d+) Warning\(s\)", text)
        err, warn = (int(m_err.group(1)), int(m_err.group(2))) if m_err else (-1, -1)
    else:
        err, warn = int(m_err.group(2)), int(m_err.group(3))
    m_size = re.search(r"Program Size: Code=(\d+) RO-data=(\d+) RW-data=(\d+) ZI-data=(\d+)", text)
    code, ro, rw, zi = (int(x) for x in m_size.groups()) if m_size else (0, 0, 0, 0)
    errs = [l.strip() for l in text.splitlines() if re.search(r"error:\s*#\d+|Error: L\d", l)]
    hexrel = dict((t, h) for t, h, _ in TARGETS)[tag]
    hexpath = os.path.join(os.path.dirname(PROJ), hexrel)
    return {"tag": tag, "err": err, "warn": warn, "rc": proc.returncode,
            "code": code, "ro": ro, "rw": rw, "zi": zi,
            "rom": code + ro + rw, "ram": rw + zi,
            "hex": hexpath, "sha": _sha256(hexpath) if os.path.exists(hexpath) else "-",
            "errs": errs}


def main() -> int:
    only, check_red, clean = None, True, True
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]
        if only not in _NAMES:
            raise SystemExit(f"--only 只能是 {_NAMES}")
    if "--no-redline" in sys.argv:
        check_red = False
    if "--incremental" in sys.argv:
        clean = False

    rows = []
    for tag, _, desc in TARGETS:
        if only and tag != only:
            continue
        print(f"\n===== [{tag}] {desc} =====", flush=True)
        r = build(tag, clean=clean)
        r["desc"] = desc
        rows.append(r)
        print(f"[{tag}] {'OK' if r['err'] == 0 else 'FAIL'}  Error {r['err']}  Warning {r['warn']}  "
              f"ROM {r['rom']} B (Code {r['code']} + RO {r['ro']} + RW {r['rw']})  "
              f"RAM {r['ram']} B", flush=True)
        print(f"        hex {r['hex']}", flush=True)
        print(f"        sha256 {r['sha']}", flush=True)
        for e in r["errs"][:5]:
            print("      " + e, flush=True)

    print("\n" + "=" * 78)
    print(f"{'Target':<16}{'ROM':>9}{'RAM':>9}{'Err':>6}{'Warn':>7}  说明")
    print("-" * 78)
    bad = 0
    for r in rows:
        print(f"[{r['tag']}] {r['rom']:>7} {r['ram']:>8} {r['err']:>6} {r['warn']:>6}  {r['desc']}")
        if r["err"] != 0:
            bad += 1
    print("=" * 78)

    rc = 1 if bad else 0
    t1 = next((r for r in rows if r["tag"] == "Target 1"), None)
    if t1:
        if t1["sha"] == REDLINE_SHA256:
            print("比赛档红线: PASS (hex 与基线逐字节相同)")
        elif check_red:
            print(f"比赛档红线: FAIL! hex 变了\n  期望 {REDLINE_SHA256}\n  实际 {t1['sha']}")
            rc = 1
        else:
            print(f"比赛档红线: (--no-redline 跳过校验) 实际 {t1['sha']}")
    print("结论: 全部 0 Error 且红线通过" if rc == 0 else "结论: 存在问题, 见上")
    return rc


if __name__ == "__main__":
    sys.exit(main())
