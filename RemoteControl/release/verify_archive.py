"""校验一个归档 zip: 清单是否齐全、有没有混进运行现场 (v3.0.0 基线)。

与 `make_archive.py` 的区别: 那个是"造包时顺手自查"; 这个是**独立校验**——
拿到别人递来的包、或想确认半年前的包没坏,都用它。

用法(在 RemoteControl/ 下执行):
    python release/verify_archive.py                          # 自动找 release/*.zip 取最新
    python release/verify_archive.py path/to/xxx.zip
"""

from __future__ import annotations

import glob
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE = os.path.join(ROOT, "release")

#: 包内必须存在的条目(与 make_archive.py 的 must_have 一致)
MUST_HAVE = [
    "release/归档说明.md",
    "release/make_archive.py",
    "release/verify_archive.py",
    # 文档(13 篇)
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
    # 上位机
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
    # 比赛固件
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
    # 课程固件(legacy 档依赖)
    "code_stm32f1_mouse/USER/Mouse.uvprojx",
    "code_stm32f1_mouse/USER/Object/Mouse.hex",
    "code_stm32f1_mouse/USER/Mouse.map",
    "code_stm32f1_mouse/HARDWARE/link.c",
    "code_stm32f1_mouse/HARDWARE/link.h",
    # 工具 + 工作记忆
    "build_competition_modes.py",
    "build_all_modes.py",
    "cleanup_workspace.py",
    "README.md",
    ".workbuddy/memory/MEMORY.md",
]

#: 出现即报错(运行现场 / 缓存 / 上游副本 / Keil 中间产物 / 抓取现场 / 包自身)
def _forbidden(name: str) -> str:
    if name.startswith("host/sessions/"):
        return "运行现场 sessions/"
    if "__pycache__" in name or name.endswith(".pyc"):
        return "字节码缓存"
    if name.startswith("code_competition - 副本/"):
        return "上游原始副本"
    if name.startswith("code_stm32f1_mouse - 副本/"):
        return "上游原始副本"
    if name.startswith("code_stm32f1_mouse/USER/Object/") and not name.endswith("Mouse.hex"):
        return "Keil 中间产物"
    if name.startswith("code_competition/USER/Remote/") and not (name.endswith("TEST.hex") or name.endswith("TEST.map")):
        return "Target_Remote 中间产物"
    if name.endswith((".o", ".crf", ".d", ".axf", ".lnp", ".dep", ".lst")):
        return "Keil 中间产物"
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
