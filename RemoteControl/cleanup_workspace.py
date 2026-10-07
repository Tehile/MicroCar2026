"""工作区清理 —— 删掉运行现场与构建残留, 保留源码/文档/可复核的证据。

为什么需要它: 本项目**不是 git 仓库**, 所以没有 `git clean` 可用; 而跑测试与烧录
会不断产生"现场", 越积越多。这个脚本把清理动作固定下来, 可反复执行(幂等)。

清理对象(**默认只预览, 加 `--apply` 才真删**):

    host/sessions/*                运行现场: 每次跑测试/连真机都会新建一个会话目录
                                   (raw.bin / *.csv / *.ts / meta.json)。默认只留最新 N 个
                                   —— 留几个是因为 `monitor_smoke.py` 的 M4 用例会
                                   `sorted(sessions)[-1]` 拿最新一个做回放; 一个都不留时
                                   该用例会退化成 SKIP(不报错, 但少 2 项覆盖)。
    __pycache__/                   Python 字节码缓存(任意层级)
    *.log(仓库根与固件目录)        构建日志。注意 `build_all_modes.py` 现在把日志写进
                                   系统临时目录, 所以不再产生新的; 这里清的是历史遗留。
    临时/调试脚本                  历史上排查问题留下的一次性脚本(显式列名, 不用通配符)

**刻意不清理**(删了会坏事或得不偿失):

    code_stm32f1_mouse/USER/Object/   Keil 中间产物(.o/.d/.crf/.axf) —— 删了能让出约 19 MB,
                                      但下次增量编译会退化成全量重编, 日常开发不值当。
                                      要清就单独跑 Keil 的 "Rebuild"。
    code_stm32f1_mouse - 副本/        上游原始固件副本(含它自己的 .git), 是**参照物**。
    release/*.zip                     各版本归档包, 是历史快照, 不许删。

用法(在**仓库根目录**执行):

    python cleanup_workspace.py                 # 预览(不删任何东西)
    python cleanup_workspace.py --apply         # 实际执行
    python cleanup_workspace.py --keep-sessions 3 --apply
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SESSIONS = os.path.join(ROOT, "host", "sessions")

#: 历史上排查问题留下的一次性脚本(显式列名 —— 绝不用通配符, 免得误删)
STALE_SCRIPTS = (
    # 前提已被真机证伪 / 已被别的套件覆盖 / 零引用
    "host/_stability.py",           # 测的是 v2.0"自时钟持续不饱和", 该前提已被证伪
    "host/shot_status.py",          # 零引用, 且需 PySide6(本机未装)
    "host/link_downlink_test.py",   # 下行链路验证, 已被 contract_test 覆盖
    "host/diag_port.py",            # 能力已内化进 host/api/conn.py
    "host/connectfail_test.py",     # GUI 场景, 需 PySide6, 能力已内化
    # 一次性排查脚本(结论已写进文档)
    "host/real_probe.py",
    "host/real_timeline.py",
    "host/real_link_drop.py",
    "host/diff_link.py",
    "host/real_window_sweep.py",    # 扫的是已删除的 AIMD 窗口机制
    "host/real_tick_latency.py",
    "host/real_latency_breakdown.py",
    "host/real_pull.py",            # 与 real_v21.py 功能重叠
)


def _human(n: int) -> str:
    return f"{n/1048576:.1f} MB" if n >= 1048576 else f"{n/1024:.1f} KB"


def _dir_size(path: str) -> int:
    total = 0
    for cur, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(cur, fn))
            except OSError:
                pass
    return total


def plan_sessions(keep: int) -> tuple[list[str], list[str]]:
    """返回 (要保留的, 要删除的) 会话目录。

    排序方式与 `monitor_smoke.py` 一致(`sorted(...)` 按目录名), 这样"保留最新的"
    与"回放用例挑的那个"是同一个。
    """
    if not os.path.isdir(SESSIONS):
        return [], []
    names = sorted(d for d in os.listdir(SESSIONS)
                   if os.path.isdir(os.path.join(SESSIONS, d)))
    if keep <= 0:
        return [], [os.path.join(SESSIONS, d) for d in names]
    return ([os.path.join(SESSIONS, d) for d in names[-keep:]],
            [os.path.join(SESSIONS, d) for d in names[:-keep]])


def plan_pycache() -> list[str]:
    out = []
    for cur, dirs, _files in os.walk(ROOT):
        if "code_stm32f1_mouse - 副本" in cur:
            continue
        for d in list(dirs):
            if d == "__pycache__":
                out.append(os.path.join(cur, d))
                dirs.remove(d)
    return out


def plan_logs() -> list[str]:
    """仓库根 + 固件目录里的 *.log(固件 Object/ 里的中间产物不在此列)。"""
    out = []
    for fn in os.listdir(ROOT):
        p = os.path.join(ROOT, fn)
        if os.path.isfile(p) and fn.endswith(".log"):
            out.append(p)
    fw = os.path.join(ROOT, "code_stm32f1_mouse")
    for cur, _dirs, files in os.walk(fw):
        for fn in files:
            if fn.endswith(".log"):
                out.append(os.path.join(cur, fn))
    return out


def plan_scripts() -> list[str]:
    return [os.path.join(ROOT, rel) for rel in STALE_SCRIPTS
            if os.path.isfile(os.path.join(ROOT, rel))]


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="cleanup_workspace.py",
        description="清理工作区里的运行现场与构建残留(默认只预览)",
    )
    ap.add_argument("--apply", action="store_true",
                    help="真正执行删除; 不加则只打印计划")
    ap.add_argument("--keep-sessions", type=int, default=1, metavar="N",
                    help="保留最新的 N 个会话目录(默认 1; 0 = 全删)。"
                         "monitor_smoke 的 M4 回放用例需要至少 1 个")
    ap.add_argument("--no-sessions", action="store_true", help="跳过会话清理")
    ap.add_argument("--no-pycache", action="store_true", help="跳过字节码缓存")
    ap.add_argument("--no-logs", action="store_true", help="跳过日志")
    args = ap.parse_args()

    # 必须在仓库根: 有个同名的 host/host 包, 站错位置会把相对路径解析错
    if not os.path.isdir(os.path.join(ROOT, "host", "host")):
        print(f"!! {ROOT} 看起来不是仓库根(找不到 host/host)", file=sys.stderr)
        return 1

    keep, drop = ([], []) if args.no_sessions else plan_sessions(args.keep_sessions)
    pycache = [] if args.no_pycache else plan_pycache()
    logs = [] if args.no_logs else plan_logs()
    scripts = [] if args.no_logs else plan_scripts()

    print("=" * 74)
    print(f"工作区清理   {'【执行模式】' if args.apply else '【预览模式 —— 不会删除任何东西】'}")
    print(f"仓库根: {ROOT}")
    print("=" * 74)

    print(f"\n[1] 运行现场 host/sessions/")
    if args.no_sessions:
        print("    (已跳过)")
    else:
        print(f"    保留 {len(keep)} 个:" + ("".join(f"\n      {os.path.basename(p)}" for p in keep)
                                              or " (无)"))
        size = sum(_dir_size(p) for p in drop)
        print(f"    删除 {len(drop)} 个, 可回收 {_human(size)}")
        if len(drop) > 20:
            print(f"    例: {', '.join(os.path.basename(p) for p in drop[:5])} ... "
                  f"{', '.join(os.path.basename(p) for p in drop[-3:])}")

    print(f"\n[2] Python 字节码缓存 __pycache__/")
    print(f"    删除 {len(pycache)} 个目录, 可回收 {_human(sum(_dir_size(p) for p in pycache))}")

    print(f"\n[3] 构建日志 *.log")
    print(f"    删除 {len(logs)} 个文件, 可回收 {_human(sum(os.path.getsize(p) for p in logs))}")
    for p in logs[:5]:
        print(f"      {os.path.relpath(p, ROOT)}")

    print(f"\n[4] 历史上的一次性调试脚本")
    if not scripts:
        print("    (已全部清理过)")
    for p in scripts:
        print(f"    待删 {os.path.relpath(p, ROOT)}")

    total_files = len(drop) + len(pycache) + len(logs) + len(scripts)
    print(f"\n{'=' * 74}")
    print(f"合计: {total_files} 项")
    print("刻意保留: code_stm32f1_mouse/USER/Object/(Keil 中间产物, 删了要全量重编)、")
    print("          code_stm32f1_mouse - 副本/(上游参照)、release/*.zip(历史归档)")
    print("=" * 74)

    if not args.apply:
        print("\n预览结束。确认无误后加 --apply 执行。")
        return 0

    # ---------------- 执行 ----------------
    print("\n开始删除 ...")
    n_ok = 0
    for p in drop + pycache:
        shutil.rmtree(p, ignore_errors=True)
        n_ok += 1
    for p in logs + scripts:
        try:
            os.remove(p)
            n_ok += 1
        except OSError:
            pass
    print(f"已删除 {n_ok} 项")

    # 复核: 没删掉就报出来(不要静默失败)
    left = [p for p in drop + pycache if os.path.exists(p)]
    left += [p for p in logs + scripts if os.path.exists(p)]
    if left:
        print(f"!! 仍有 {len(left)} 项未删除:", [os.path.relpath(p, ROOT) for p in left[:5]])
        return 2

    if not args.no_sessions:
        names = sorted(d for d in os.listdir(SESSIONS)
                       if os.path.isdir(os.path.join(SESSIONS, d)))
        print(f"host/sessions/ 现在有 {len(names)} 个会话: {names}")
        if not names:
            print("提示: 没有任何会话时 `monitor_smoke.py` 的 M4 回放用例会退化为 SKIP。")

    print("\n完成。建议接着跑一遍全套件确认没删坏:")
    print("    cd host && python selftest.py && python contract_test.py && python pull_smoke.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
