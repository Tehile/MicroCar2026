r"""按 Target 逐个重编 `code_competition - V6++` 固件, 记录 ROM/RAM/告警并校验红线。

与 `build_competition_modes.py` 的区别: 那边是 code_competition 单工程双 Target;
V6++ 工程(回程补搜改造后)是 **五 Target**; 基线为改造后实测值,
五个 hex 任一变化都应视为意外改动。

五个档位:
    V6_0mm / V6_2mm / V6_4mm / V6_6mm  比赛档, 仅 GOAL_DEPARTURE_EXTRA_MM 取值不同
                                      Define=STM32F10X_MD,USE_STDPERIPH_DRIVE,GOAL_DEPARTURE_EXTRA_MM=N
                                      源码零改动(REMOTE_FULL 未定义), hex 必须与基线逐字节相同
    Target_Remote                    保真远程档
                                      Define=上述(不定义 GOAL_DEPARTURE_EXTRA_MM -> 默认 0)
                                             ,MOUSE_REMOTE,REMOTE_FULL,EN_USART1_RX
                                      输出目录 USER\Remote\(与比赛档隔离)
                                      飞行记录(FLASHSAVE)仍关闭, 但 V5/V6 算法全开

红线: **只比 hex 的 sha256**。四个比赛档任何一个变化都算失败。

⚠️ UV4 的 exit code 不可靠(0=无错 / 1=仅有告警 / 2=有错), 一律以构建日志
   "N Error(s)" 为准。

用法(仓库根目录):
    python build_v6pp_modes.py                        # 五档全量重编 + 红线校验
    python build_v6pp_modes.py --only Target_Remote   # 只编某一档
    python build_v6pp_modes.py --incremental          # 不删产物, 增量编
    python build_v6pp_modes.py --no-redline           # 跳过红线校验(改基线时用)
    python build_v6pp_modes.py --help
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
FW = os.path.join(ROOT, "code_competition - V6++")
UV4 = r"C:\Keil_v5\UV4\UV4.exe"
PROJ = os.path.join(FW, "USER", "TEST.uvprojx")

#: 四个比赛档的 hex 基线(2026-11-05 全量构建实测值)。
#: 改这个值 = 承认比赛档二进制变了, 必须在提交信息里说明原因。
REDLINE = {
    "V6_0mm": "0c1a6fd5a8ea0dacda3e37eab6fa571cda551edbdebf63708e48b6ef6fb351ca",
    "V6_2mm": "77dc595473bfb88f002a3ead912df671714245ce0909a61e72d53e8299d913fd",
    "V6_4mm": "185b4ed927643242d7dfd7ac29e91caa7c7ff875add7f1799b6ddecfba251b12",
    "V6_6mm": "cdca4640a7db35ff9b51fff87692aef2b8e1729c23da08e426e61073a3e43181",
}

#: (Target 名, 输出目录, hex 文件名, 说明)
TARGETS: "list[tuple[str, str, str, str]]" = [
    ("V6_0mm", os.path.join("Build", "V6_0mm"), "TEST_V6_0mm.hex", "比赛档 0mm(红线)"),
    ("V6_2mm", os.path.join("Build", "V6_2mm"), "TEST_V6_2mm.hex", "比赛档 2mm(红线)"),
    ("V6_4mm", os.path.join("Build", "V6_4mm"), "TEST_V6_4mm.hex", "比赛档 4mm(红线)"),
    ("V6_6mm", os.path.join("Build", "V6_6mm"), "TEST_V6_6mm.hex", "比赛档 6mm(红线)"),
    ("Target_Remote", "Remote", "TEST.hex", "保真远程档(MOUSE_REMOTE+REMOTE_FULL)"),
]
_NAMES = [t for t, _, _, _ in TARGETS]

#: 构建产物扩展名(源码/工程/配置都不在这些后缀里)
_ART_EXTS = (".o", ".d", ".crf", ".lst", ".lnp", ".dep", ".axf", ".hex", ".map", ".htm")


def _clean(outdir: str) -> int:
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
    outdir = os.path.join(os.path.dirname(PROJ), dict((t, d) for t, d, _, _ in TARGETS)[tag])
    if clean:
        n = _clean(outdir)
        print(f"[{tag}] 已清构建产物 {n} 个 -> 全量重编", flush=True)
    # ⚠️ 日志落系统临时目录, 不写仓库
    log = os.path.join(tempfile.gettempdir(), f"v6_build_{tag}.log")
    proc = subprocess.run([UV4, "-b", PROJ, "-t", tag, "-o", log],
                          capture_output=True, text=True)
    text = open(log, encoding="gbk", errors="replace").read() if os.path.exists(log) else ""
    m_err = re.search(r'"([^"]+)" - (\d+) Error\(s\), (\d+) Warning\(s\)', text)
    if m_err:
        err, warn = int(m_err.group(2)), int(m_err.group(3))
    else:
        m_err = re.search(r"(\d+) Error\(s\), (\d+) Warning\(s\)", text)
        err, warn = (int(m_err.group(1)), int(m_err.group(2))) if m_err else (-1, -1)
    m_size = re.search(r"Program Size: Code=(\d+) RO-data=(\d+) RW-data=(\d+) ZI-data=(\d+)", text)
    code, ro, rw, zi = (int(x) for x in m_size.groups()) if m_size else (0, 0, 0, 0)
    errs = [l.strip() for l in text.splitlines() if re.search(r"error:\s*#\d+|Error: L\d", l)]
    hexname = dict((t, h) for t, _, h, _ in TARGETS)[tag]
    hexpath = os.path.join(os.path.dirname(PROJ), outdir, hexname)
    return {"tag": tag, "err": err, "warn": warn, "rc": proc.returncode,
            "code": code, "ro": ro, "rw": rw, "zi": zi,
            "rom": code + ro + rw, "ram": rw + zi,
            "hex": hexpath, "sha": _sha256(hexpath) if os.path.exists(hexpath) else "-",
            "errs": errs}


def main() -> int:
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return 0

    only, check_red, clean = None, True, True
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]
        if only not in _NAMES:
            raise SystemExit(f"--only 只能是 {_NAMES}")
    if "--no-redline" in sys.argv:
        check_red = False
    if "--incremental" in sys.argv:
        clean = False

    if not os.path.isdir(FW):
        raise SystemExit(f"找不到工程目录: {FW}")

    rows = []
    for tag, _outdir, _hex, desc in TARGETS:
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

    print("\n" + "=" * 84)
    print(f"{'Target':<16}{'ROM':>9}{'RAM':>9}{'Err':>6}{'Warn':>7}  说明")
    print("-" * 84)
    bad = 0
    for r in rows:
        print(f"[{r['tag']}] {r['rom']:>7} {r['ram']:>8} {r['err']:>6} {r['warn']:>6}  {r['desc']}")
        if r["err"] != 0:
            bad += 1
    print("=" * 84)

    rc = 1 if bad else 0

    # 红线: 只查本次真的编了的比赛档
    checked = 0
    for r in rows:
        base = REDLINE.get(r["tag"])
        if base is None:
            continue
        checked += 1
        if r["sha"] == base:
            print(f"红线 {r['tag']:<9} PASS")
        elif check_red:
            print(f"红线 {r['tag']:<9} FAIL! 期望 {base}\n                        实际 {r['sha']}")
            rc = 1
        else:
            print(f"红线 {r['tag']:<9} (--no-redline 跳过) 实际 {r['sha']}")

    if checked:
        print(f"\n红线小结: {checked} 个比赛档已校验")
    print("结论: 全部 0 Error 且红线通过" if rc == 0 else "结论: 存在问题, 见上")
    return rc


if __name__ == "__main__":
    sys.exit(main())
