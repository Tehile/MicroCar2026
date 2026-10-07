"""按**显式清单**生成项目归档 zip。

为什么不用 `tar -czf` 打包整个目录: 工作区里混着**运行现场**与**上游副本** ——
    host/sessions/                      4.7 MB  测试跑出来的 raw.bin/CSV
    code_stm32f1_mouse - 副本/           25  MB  未改动的上游原始固件(含它自己的 .git)
    code_stm32f1_mouse/USER/Object/      19  MB  Keil 中间产物
整目录打包会把它们一起卷进来(约 50 MB 噪声)。本脚本用**白名单 + 排除规则**,
结果确定、可复现, 且与 `release/归档说明.md` §1.5 的"刻意不入档"表一一对应。

用法(在仓库根目录执行):
    python release/make_archive.py

产物:
    release/RemoteControl-v2.2.1-YYYYMMDD.zip
"""

from __future__ import annotations

import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE = os.path.join(ROOT, "release")

VERSION = "v2.2.1"
DATE = "20261005"
OUT_NAME = f"RemoteControl-{VERSION}-{DATE}.zip"

#: 入档的顶层条目(文件或目录)
INCLUDE_ROOTS = [
    "docs",
    "host",
    "code_stm32f1_mouse",
    ".workbuddy/memory",
    "build_all_modes.py",
    "cleanup_workspace.py",
    "release/归档说明.md",
    "release/make_archive.py",
    "release/verify_archive.py",
]

#: 需要排除的**路径前缀**(相对仓库根, 用 / 分隔) —— 按目录整体排除
EXCLUDE_DIR_PREFIX = [
    "host/sessions",                   # 运行现场(测试跑出来的 raw.bin/CSV)
    "code_stm32f1_mouse/USER/Object",  # Keil 中间产物(下面单独放行 Mouse.hex)
    "code_stm32f1_mouse/USER/DebugConfig",
]

#: 需要排除的**目录名**(任意层级出现即排除)
EXCLUDE_DIR_NAMES = {
    "__pycache__",   # Python 字节码缓存
}

#: 需要排除的文件(相对仓库根)
EXCLUDE_FILES = {
    # 构建产物以外的固件工具现场
    "code_stm32f1_mouse/USER/JLinkLog.txt",
    "code_stm32f1_mouse/USER/JLinkSettings.ini",
    "code_stm32f1_mouse/USER/stm32f10x.lst",
    # 排查问题时随手写的临时脚本(下划线前缀), 不是交付物
    "host/_dbg_t3.py",
    "host/_probe_text.py",
    # 归档自身
}
#: 前缀/后缀排除
EXCLUDE_SUFFIX = (".log", ".uvguix.10994", ".uvguix.liuru", ".bak", ".zip")

#: 例外: 即使父目录被排除也要收进来的文件
FORCE_INCLUDE = {
    "code_stm32f1_mouse/USER/Object/Mouse.hex",  # 可直接烧录的成品
    "code_stm32f1_mouse/USER/Mouse.map",         # 资源占用证据
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
    # 任意层级的目录名匹配(如 __pycache__ 出现在 host/host/api/__pycache__)
    if any(part in EXCLUDE_DIR_NAMES for part in rel.split("/")[:-1]):
        return True
    if rel.endswith(EXCLUDE_SUFFIX):
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
        "release/归档说明.md",
        "release/make_archive.py",
        "release/verify_archive.py",
        # 文档(设计 + 自动生成的接口参考)
        "docs/api_reference.md",
        "docs/CHANGELOG.md",
        "docs/传输模式重构设计方案.md",
        "docs/传输模式接口设计.md",
        "docs/上位机接口化设计方案.md",
        "docs/蓝牙上位机设计方案.md",
        "docs/电机远程控制与双流传输.md",
        "docs/安全测试模式与实时监视.md",
        "docs/课程调参实验与参数设定.md",
        "docs/数据收集与时间同步.md",
        "docs/红外与电源测量回传设计方案.md",
        "docs/迷宫状态机回传.md",
        "docs/比赛固件空间不足诊断与瘦身方案.md",
        # 上位机核心 + 各功能子 API
        "host/README.md",
        "host/host/registry.py",
        "host/host/clock.py",
        "host/host/model/ring.py",
        "host/host/monitor.py",
        "host/host/api/telemetry.py",
        "host/host/api/timesync.py",
        "host/host/api/maze.py",
        # 测试套件(v2.0: snap_smoke.py 随 SNAP 删除; v2.1: 新增 pull_smoke.py)
        "host/selftest.py",
        "host/contract_test.py",
        "host/facade_smoke.py",
        "host/lesson_smoke.py",
        "host/monitor_smoke.py",
        "host/motor_lab_smoke.py",
        "host/timesync_smoke.py",
        "host/maze_smoke.py",
        "host/pull_smoke.py",
        # 真机诊断脚本(v2.1 排查自时钟灌死 / 定标停等参数时建立, 排查手法本身是交付物。
        # 2026-10-05 精简: 一次性排查脚本(结论已进文档)已从工作区删除, 只留有复跑价值的)
        "host/real_v21.py",
        "host/real_burst.py",
        "host/maze_launch_probe.py",
        "host/raw_ping.py",
        "host/maze_relaunch_repro.py",
        "host/link_check.py",
        "host/estop_latency.py",
        "host/real_discriminate.py",
        "host/real_throughput.py",
        "host/real_lesson_sampling.py",
        "host/live_read.py",
        # 固件(公共路径 + 各档专属)
        "code_stm32f1_mouse/USER/main.h",
        "code_stm32f1_mouse/HARDWARE/link.c",
        "code_stm32f1_mouse/HARDWARE/link.h",
        "code_stm32f1_mouse/APP/lesson_lab.c",
        "code_stm32f1_mouse/USER/Object/Mouse.hex",
        "code_stm32f1_mouse/USER/Mouse.map",
        # 五档批量构建工具 + 工作区清理工具
        "build_all_modes.py",
        "cleanup_workspace.py",
    ]
    bad = [m for m in must_have if m not in names]
    must_not = [n for n in names
                if n.startswith("host/sessions/")
                or "__pycache__" in n
                or n.startswith("code_stm32f1_mouse - 副本/")
                or (n.startswith("code_stm32f1_mouse/USER/Object/")
                    and not n.endswith("Mouse.hex"))]
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
