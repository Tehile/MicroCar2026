"""按**显式清单**生成项目归档 zip (v3.0.0 基线: code_competition + tools/ + 十套件)。

为什么不用 `tar -czf` 打包整个目录 / `git archive`:
  工作区里混着**运行现场**与**构建中间产物** ——
    host/sessions/                      5.2 MB  测试跑出来的 raw.bin/CSV
    host/*.csv *.txt *.json(顶层)       数 MB    pace/mtbf/evt 抓取现场
    code_competition/USER/*.o|.crf|.d…  ~11 MB  Keil 中间产物(源码可重建)
    code_competition - 副本/            11 MB   上游副本
    code_stm32f1_mouse - 副本/          19 MB   上游副本
  且 git 跟踪不到构建产物(`code_competition/.gitignore` 连 *.hex/*.map 都忽略),
  而 TEST.hex(红线成品) / Remote/TEST.hex(远程档成品) 是必须入档的证据。
  本脚本用**白名单 + 排除规则**, 结果确定、可复现, 与 `release/归档说明.md` §1.5
  的"刻意不入档"表一一对应。

用法(在 RemoteControl/ 下执行):
    python release/make_archive.py

产物:
    release/RemoteControl-v3.0.0-20261016.zip
"""

from __future__ import annotations

import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE = os.path.join(ROOT, "release")

VERSION = "v3.0.0"
DATE = "20261016"
OUT_NAME = f"RemoteControl-{VERSION}-{DATE}.zip"

#: 入档的顶层条目(文件或目录)
INCLUDE_ROOTS = [
    "docs",
    "host",
    "code_competition",
    "code_stm32f1_mouse",
    "tools",
    ".workbuddy/memory",
    "README.md",
    "build_competition_modes.py",
    "build_all_modes.py",
    "cleanup_workspace.py",
    "release/归档说明.md",
    "release/make_archive.py",
    "release/verify_archive.py",
]

#: 需要排除的**路径前缀**(相对 RemoteControl/, 用 / 分隔) —— 按目录整体排除
EXCLUDE_DIR_PREFIX = [
    "host/sessions",                   # 运行现场(测试跑出来的 raw.bin/CSV)
    "code_competition - 副本",          # 上游原始副本(含它自己的 .git)
    "code_stm32f1_mouse - 副本",        # 上游原始副本
    "code_stm32f1_mouse/USER/Object",  # Keil 中间产物(下面单独放行 Mouse.hex/map)
    "code_competition/USER/RTE",       # Keil RTE 生成文件
    "code_competition/USER/DebugConfig",
]

#: 需要排除的**目录名**(任意层级出现即排除)
EXCLUDE_DIR_NAMES = {
    "__pycache__",   # Python 字节码缓存
}

#: 需要排除的文件(相对 RemoteControl/)
EXCLUDE_FILES = {
    "_link_ids.tmp",
    # 上位机抓取现场(结论已进 docs; 复跑即可再得)
    "host/ev_capture_20261016_184044.json",
    "host/ev_capture_20261016_184404.json",
    "host/drop24.csv",
    "host/drop30.csv",
}

#: 后缀排除: Keil/uVision 中间产物 + 工具现场 + 归档自身
EXCLUDE_SUFFIX = (
    ".o", ".d", ".crf", ".lst", ".lnp", ".dep", ".axf", ".htm", ".sct",
    ".bak", ".Bak", ".log", ".zip", ".pyc",
)

#: 文件名含这些片段即排除(uvguix 各机器名 / JLink 现场)
EXCLUDE_NAME_PARTS = (".uvgui", "JLink")


def _host_top_artifact(rel: str) -> bool:
    """host/ 顶层的抓取现场(*.csv/*.txt/*.json) —— 只作用于 host/ 第一层。"""
    if not rel.startswith("host/"):
        return False
    rest = rel[len("host/"):]
    return "/" not in rest and rest.endswith((".csv", ".txt", ".json"))


#: 例外: 即使父目录被排除也要收进来的文件
FORCE_INCLUDE = {
    # 比赛档红线成品(sha256 = 68ad6be2…8141, 可直接烧录)
    "code_competition/USER/TEST.hex",
    "code_competition/USER/TEST.map",           # 资源占用证据
    # 远程档成品(sha256 = a71b5e9d…, ERR_ESTOP 修复后基线)
    "code_competition/USER/Remote/TEST.hex",
    # 课程固件成品 + 资源证据
    "code_stm32f1_mouse/USER/Object/Mouse.hex",
    "code_stm32f1_mouse/USER/Mouse.map",
}


def _rel(p: str) -> str:
    return os.path.relpath(p, ROOT).replace("\\", "/")


def _excluded(rel: str) -> bool:
    if rel in FORCE_INCLUDE:
        return False
    if rel in EXCLUDE_FILES:
        return True
    for pre in EXCLUDE_DIR_PREFIX:
        if rel == pre or rel.startswith(pre + "/"):
            return True
    # 任意层级的目录名匹配(如 host/host/api/__pycache__)
    if any(part in EXCLUDE_DIR_NAMES for part in rel.split("/")[:-1]):
        return True
    name = rel.split("/")[-1]
    if rel.endswith(EXCLUDE_SUFFIX):
        return True
    if any(part in name for part in EXCLUDE_NAME_PARTS):
        return True
    if _host_top_artifact(rel):
        return True
    return False


def collect() -> list[str]:
    out: list[str] = []
    for entry in INCLUDE_ROOTS:
        ap = os.path.join(ROOT, entry)
        if os.path.isfile(ap):
            if not _excluded(_rel(ap)):
                out.append(_rel(ap))
            continue
        if not os.path.isdir(ap):
            raise SystemExit(f"清单里的条目不存在: {entry}")
        for cur, dirs, files in os.walk(ap):
            dirs[:] = [d for d in dirs
                       if not _excluded(_rel(os.path.join(cur, d)))]
            for fn in files:
                rel = _rel(os.path.join(cur, fn))
                if not _excluded(rel):
                    out.append(rel)
    # FORCE_INCLUDE 里可能被 walk 漏掉(父目录被整目录排除), 手工补
    for rel in FORCE_INCLUDE:
        if os.path.isfile(os.path.join(ROOT, rel)) and rel not in out:
            out.append(rel)
    return sorted(set(out))


def main() -> int:
    files = collect()
    os.makedirs(RELEASE, exist_ok=True)
    out_path = os.path.join(RELEASE, OUT_NAME)

    total = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in files:
            ap = os.path.join(ROOT, rel)
            z.write(ap, rel)
            total += os.path.getsize(ap)

    size = os.path.getsize(out_path)
    print(f"已生成 {out_path}")
    print(f"  入档文件 {len(files)} 个, 原始 {total/1048576:.2f} MB, 压缩后 {size/1048576:.2f} MB")

    # ---- 自检: 关键文件必须在, 运行现场必须不在 ----
    with zipfile.ZipFile(out_path) as z:
        names = set(z.namelist())
    must_have = [
        # 归档三件套
        "release/归档说明.md",
        "release/make_archive.py",
        "release/verify_archive.py",
        # 文档(13 篇, 含 v3.0 方案/核查/参数表 + 自动生成的接口参考)
        "docs/参数全量整定常量化-方案v3.0.md",
        "docs/参数名称-功能-调整表.md",
        "docs/参数JSON化与双向转换工具链设计方案.md",
        "docs/参数JSON化-一致性核查报告.md",
        "docs/比赛固件远程化移植方案.md",
        "docs/通信协议规格.md",
        "docs/算法详解.md",
        "docs/遥测拉取器统一设计方案.md",
        "docs/链路MTBF压测方法.md",
        "docs/api_reference.md",
        "docs/P3实机联调测试计划.md",
        "docs/实机验证-USART1-NVIC修复.md",
        "docs/真机取证记录-20261016.md",
        # 上位机: 门面 + 协议 + 档位 + 模拟器 + 十套件 + 真机诊断
        "host/README.md",
        "host/requirements.txt",
        "host/host/registry.py",
        "host/host/fwprofile.py",
        "host/host/protocol/codec.py",
        "host/host/protocol/commands.py",
        "host/host/sim/mouse_sim.py",
        "host/host/api/client.py",
        "host/host/cli.py",
        "host/host/monitor.py",
        "host/host/api/calc.py",
        "host/tools/gen_api_reference.py",
        "host/selftest.py",
        "host/contract_test.py",
        "host/facade_smoke.py",
        "host/maze_smoke.py",
        "host/monitor_smoke.py",
        "host/pull_smoke.py",
        "host/pace_smoke.py",
        "host/stream_unified_smoke.py",
        "host/timesync_smoke.py",
        "host/motor_lab_smoke.py",
        "host/safe_stop_smoke.py",
        "host/basic_cmd_smoke.py",
        "host/ev_capture.py",
        # 参数工具链 v2 + v3
        "tools/paramgen.py",
        "tools/paramkit/tunable.py",
        "tools/paramkit/registry.py",
        "tools/paramkit/patch.py",
        "tools/paramkit/render.py",
        "tools/paramkit/pipeline.py",
        "tools/params/params.competition.json",
        "tools/params/params.competition.v3.json",
        # 比赛固件: 工程 + 源码 + 两个 Target 的成品 hex
        "code_competition/USER/TEST.uvprojx",
        "code_competition/USER/TEST.hex",
        "code_competition/USER/TEST.map",
        "code_competition/USER/Remote/TEST.hex",
        "code_competition/USER/test.c",
        "code_competition/USER/user.c",
        "code_competition/HARDWARE/TIMER/timer.c",
        "code_competition/HARDWARE/ADC/adc.c",
        "code_competition/UTILITY/utility.c",
        "code_competition/STRATAGY/stratagy.c",
        "code_competition/REMOTE/link.c",
        "code_competition/REMOTE/link_port.c",
        "code_competition/REMOTE/link.h",
        # 课程固件(legacy 档依赖, fwprofile.py 指向)
        "code_stm32f1_mouse/USER/Mouse.uvprojx",
        "code_stm32f1_mouse/USER/Object/Mouse.hex",
        "code_stm32f1_mouse/USER/Mouse.map",
        "code_stm32f1_mouse/HARDWARE/link.c",
        "code_stm32f1_mouse/HARDWARE/link.h",
        # 构建/清理工具 + 工作记忆
        "build_competition_modes.py",
        "build_all_modes.py",
        "cleanup_workspace.py",
        "README.md",
        ".workbuddy/memory/MEMORY.md",
    ]
    bad = [m for m in must_have if m not in names]
    must_not = [n for n in names
                if n.startswith("host/sessions/")
                or "__pycache__" in n
                or n.endswith(".pyc")
                or n.startswith("code_competition - 副本/")
                or n.startswith("code_stm32f1_mouse - 副本/")
                or (n.startswith("code_stm32f1_mouse/USER/Object/")
                    and not n.endswith("Mouse.hex"))
                or (n.startswith("code_competition/USER/Remote/")
                    and not (n.endswith("TEST.hex") or n.endswith("TEST.map")))
                or n.endswith((".o", ".crf", ".d", ".axf", ".lnp", ".dep", ".zip"))]
    ok = True
    if bad:
        ok = False
        print("  [FAIL] 缺少关键文件:", bad)
    if must_not:
        ok = False
        print(f"  [FAIL] 混进了不该入档的 {len(must_not)} 个文件, 例:", must_not[:5])
    if ok:
        print("  [OK] 关键文件齐全; 无运行现场/缓存/上游副本/Keil 中间产物")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
