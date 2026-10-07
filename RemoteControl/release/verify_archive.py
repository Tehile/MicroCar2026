"""校验一个归档 zip: 清单是否齐全、有没有混进运行现场。

与 `make_archive.py` 的区别: 那个是"造包时顺手自查"; 这个是**独立校验**——
拿到别人递来的包、或想确认半年前的包没坏,都用它。

用法(在仓库根目录执行):
    python release/verify_archive.py                          # 自动找 release/*.zip
    python release/verify_archive.py path/to/xxx.zip
"""

from __future__ import annotations

import glob
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE = os.path.join(ROOT, "release")

#: 包内必须存在的条目
MUST_HAVE = [
    "release/归档说明.md",
    "release/make_archive.py",
    "release/verify_archive.py",
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
    "host/README.md",
    "host/requirements.txt",
    "host/selftest.py",
    "host/contract_test.py",
    "host/timesync_smoke.py",
    "host/maze_smoke.py",
    "host/pull_smoke.py",
    "host/real_v21.py",
    "host/maze_launch_probe.py",
    "host/raw_ping.py",
    "host/maze_relaunch_repro.py",
    "host/link_check.py",
    "host/estop_latency.py",
    "host/real_burst.py",
    "host/host/registry.py",
    "host/host/clock.py",
    "host/host/api/maze.py",
    "host/host/cli.py",
    "host/host/datalog.py",
    "host/host/api/timesync.py",
    "host/host/protocol/codec.py",
    "host/host/sim/mouse_sim.py",
    "build_all_modes.py",
    "cleanup_workspace.py",
    "code_stm32f1_mouse/USER/main.h",
    "code_stm32f1_mouse/USER/main.c",
    "code_stm32f1_mouse/USER/Mouse.uvprojx",
    "code_stm32f1_mouse/HARDWARE/link.c",
    "code_stm32f1_mouse/HARDWARE/link.h",
    "code_stm32f1_mouse/APP/lesson_lab.c",
    "code_stm32f1_mouse/APP/lesson_lab.h",
    "code_stm32f1_mouse/USER/Object/Mouse.hex",
    "code_stm32f1_mouse/USER/Mouse.map",
]

#: 出现即报错(运行现场 / 缓存 / 上游副本 / Keil 中间产物 / 包自身)
def _forbidden(name: str) -> str:
    if name.startswith("host/sessions/"):
        return "运行现场 sessions/"
    if "__pycache__" in name:
        return "字节码缓存"
    if name.startswith("code_stm32f1_mouse - 副本/"):
        return "上游原始副本"
    if name.startswith("code_stm32f1_mouse/USER/Object/") and not name.endswith("Mouse.hex"):
        return "Keil 中间产物"
    if name.endswith((".pyc", ".uvguix.10994", ".uvguix.liuru")):
        return "工具现场"
    if name.endswith(".zip"):
        return "归档自身"
    return ""


def _human(n: int) -> str:
    return f"{n/1048576:.2f} MB" if n >= 1048576 else f"{n/1024:.1f} KB"


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        path = argv[1]
    else:
        cands = sorted(glob.glob(os.path.join(RELEASE, "*.zip")))
        if not cands:
            print("找不到 release/*.zip")
            return 2
        path = cands[-1]

    print(f"校验: {path}")
    if not os.path.isfile(path):
        print("  文件不存在")
        return 2

    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        names = [i.filename for i in infos]
        raw = sum(i.file_size for i in infos)
        comp = os.path.getsize(path)

    print(f"  条目 {len(names)} 个, 解压后 {_human(raw)}, 压缩包 {_human(comp)}")

    ok = True
    missing = [m for m in MUST_HAVE if m not in set(names)]
    if missing:
        ok = False
        print(f"  [FAIL] 缺少 {len(missing)} 个必需条目:")
        for m in missing:
            print(f"         {m}")
    else:
        print(f"  [OK] {len(MUST_HAVE)} 个必需条目齐全")

    bad = [(n, _forbidden(n)) for n in names if _forbidden(n)]
    if bad:
        ok = False
        print(f"  [FAIL] 混进 {len(bad)} 个不该入档的条目, 例:")
        for n, why in bad[:5]:
            print(f"         {n}   <- {why}")
    else:
        print("  [OK] 无运行现场 / 缓存 / 上游副本 / 中间产物 / 包自身")

    # 顶层目录概览
    tops: dict[str, int] = {}
    for n in names:
        tops[n.split("/")[0]] = tops.get(n.split("/")[0], 0) + 1
    print("  顶层条目: " + ", ".join(f"{k}({v})" for k, v in sorted(tops.items())))

    print("  结果: " + ("通过" if ok else "不通过"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
