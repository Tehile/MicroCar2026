"""校验「Control 端点 ↔ 上游方法」三者一致 —— 防漂移的那条命令。

    python tools/check_iface_table.py

检查三件事：

  ① **上游符号真的存在**：`docs/接口对照表.md` 里写到的每个 `HostLink.ledger` 这类
     符号，都去上游真的 `getattr` 一下。上游改名/删方法，这里立刻红。
  ② **端点没有漏登记**：`control/api/routes.py` 里注册的每个路由都必须在表里出现，
     反过来表里的每条路由也必须真的注册过。两边任何一侧多出来都报错。
  ③ **设计方案附录 A 已经收敛到本表**：不允许方案里再各自维护一份端点表
     （两份表 = 一定漂移）。

为什么要有这条命令：我们不碰上游源码（判据 I9），代价是上游一变我们**只能在运行时**
才发现，而且往往是在现场那一页。有了它，改完上游先跑一遍即可。
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TABLE = ROOT / "docs" / "接口对照表.md"
DESIGN = ROOT / "docs" / "上位机整合设计方案.md"

#: 表里允许出现的上游符号根 -> 从哪导入
#:
#: ⚠️ 有些符号**必须从子模块取，不能从包取**：`host.api` 的 `__all__` 没有把
#: `error_ratio` 再导出（它只在 `host/api/lesson.py` 里）。从包上 getattr 会
#: AttributeError —— 而"取不到就当没有"会让报错指向错误的方向。
RESOLVER = {
    # 模块 A：RemoteControl/host（包 host）
    "MouseClient": "host.api",
    "ConnAPI": "host.api",
    "ParamsAPI": "host.api",
    "StreamAPI": "host.api",
    "MazeAPI": "host.api",
    "MoveAPI": "host.api",
    "CalibAPI": "host.api",
    "SessionAPI": "host.api",
    "AnalyzeAPI": "host.api",
    "TelemetryAPI": "host.api",
    # ⚠️ 已删除、**故意不列**（列了会让人以为"上游还有这个名字"，
    #    而它们在上游 2.0.0 就没了：`ScopeAPI`/`SnapAPI` 连模块都不存在）：
    #    ScopeAPI（SCOPE/DUMP 整套）、SnapAPI（SNAP 整套）、SNAP_DOMAINS、SNAP_FIELDS、
    #    MAZE_FLAG_NAMES、MAZE_EV_BITS。
    #    ⇒ 本表只放**当前真的存在**的符号 —— 否则"名字写错"会被误读成"上游改名了"。
    "Link": "host.transport.link",
    "Event": "host.api.events",
    # 2.1.2 / 2.2.0：拉取器的让路机制与救援口（`PullRequester` 是 2.1.0 起的停等拉取器）
    "PullRequester": "host.api.stream",
    # 1.2.0 / 1.3.0 / 1.4.0 新增的三个子 API 与它们的纯函数
    "MotorLabAPI": "host.api.lab",
    "RunResult": "host.api.lab",
    # ⚠️ 2.4.0 把**课程层整层删了**（`host.api.lesson` 连模块都不存在）：
    #    LessonAPI / LessonResult / LESSON_EXPS 一律**不列** —— 列了 A0 会去 import
    #    一个不存在的模块，报出来的错看着像"本层拼错了"，其实是"上游删了"。
    #    ⇒ 本表只放**当前真的存在**的符号（同 2.0.0 处理 `SnapAPI` 的做法）。
    #
    # ⚠️ 但**算参那几个纯函数只是搬了家**（`host/api/lesson.py` → **`host/api/calc.py`**），
    #    逻辑一字未改，且 `host.api` 仍把其中一部分再导出。它们的家现在是 `calc`。
    #    注：`theory_gyro_ratio` / `pick_best_position` 等 `host.api` 未再导出的，
    #    从 `host.api.calc` 取（与当年从 `host.api.lesson` 取同理）。
    "solve_ratio": "host.api.calc",
    "error_ratio": "host.api.calc",
    "step_metrics": "host.api.calc",
    "straightness_metrics": "host.api.calc",
    "theory_track_ratio": "host.api.calc",
    "theory_gyro_ratio": "host.api.calc",
    "TimeSyncAPI": "host.api.timesync",
    "TimeSyncState": "host.api.timesync",
    "min_delay_offset": "host.api.timesync",
    "fit_clock": "host.api.timesync",
    "drift_ppm": "host.api.timesync",
    "resolve_ms8": "host.api.timesync",
    # 迷宫状态机（实验 8）—— `MazeAPI` 在 `host.api` 包上。
    # ⚠️ 这里**故意不再列** `SnapAPI` / `SNAP_DOMAINS` / `SNAP_FIELDS` /
    #    `MAZE_FLAG_NAMES` / `MAZE_EV_BITS`：它们随上游 2.0.0 的整体替换**全没了**
    #    （`host.api.snap` 连模块都不存在）。留着死条目会让"符号写错"被误读成
    #    "上游改名了"——**本表只放当前真的存在的符号**。
    # 协议层**静态码表**：`GET /mouse/meas/meta` 直接读它们 ——
    # 因为这条端点的契约是"不连也能答"，而方法要连上调实例。上游改字段名/路口名
    # 会在这里立刻现形（这一层的名字是**跨语言的**：固件、上位机、界面三处必须同源）。
    "MAZE_TASK_NAMES": "host.protocol.codec",
    "ROAD_NAMES": "host.protocol.codec",
    "ROAD_KINDS": "host.protocol.codec",
    "MOTION_TASK_NAMES": "host.protocol.codec",
    # 2.3.0 新增的两张码表（前墙双档 / 比赛冲刺档）—— `/mouse/meas/meta` 端出它们
    "SPURT_MODE_NAMES": "host.protocol.codec",
    "FRONT_NAMES": "host.protocol.codec",
    "DIR_NAMES": "host.api.maze",
    "JUNCTION_KINDS": "host.api.maze",
    # 2.2.1 发车前低电闸：阈值是**登记表**里的常量（本层从它取，不写死），
    # 而 `precheck` 是挂在 `CommandSpec` 上的字段（表单层执行 ⇒ 脚本也绕不开）。
    # ⚠️ 本表的写法约定是"**根名映射到模块**"：`LAUNCH_MIN_MV` → `host.registry`、
    #    然后 `getattr(host.registry, "LAUNCH_MIN_MV")`。所以这里**不能**写
    #    `"registry": "host.registry"` 再去表里写 `registry.XXX` —— 那会变成
    #    `getattr(host.registry, "registry")`，直接 AttributeError（踩过）。
    "LAUNCH_MIN_MV": "host.registry",
    "CommandSpec": "host.registry",
    # 模块 B：PhoneCamera/pc
    "HostLink": "hostlink",
    # 模块 C：Auto Locate
    "ArucoLocator": "aruco_locator",
    "create_locator": "aruco_locator",
    "parameter_catalog": "aruco_locator",
    "flatten_config": "aruco_locator",
    "apply_params": "aruco_locator",
    "apply_profile": "aruco_locator",
    "describe_profiles": "aruco_locator",
    "list_profiles": "aruco_locator",
    "catalog_markdown": "aruco_locator",
}

#: 这些路径不是"端点表"的一部分：首页/静态资源由 app.py 直接挂，不属于 Upstream 映射
IGNORE_ROUTES = {("/GET", "/"), ("/GET", "/static/")}

RESULTS: list = []


def record(cid: str, verdict: str, note: str) -> None:
    RESULTS.append((cid, verdict, note))
    tag = {"PASS": "[PASS]", "FAIL": "[FAIL]", "SKIP": "[SKIP]", "INFO": "[INFO]"}[verdict]
    print(f"  {tag} {cid:5s} {note}")


def head(t: str) -> None:
    print(f"\n── {t} " + "─" * max(0, 66 - len(t)))


def ensure_paths() -> None:
    """把三个上游目录挂上 sys.path —— 它们是**平级目录**，不是包。"""
    from control.adapters import ensure_path
    from control.config import bootstrap
    cfg = bootstrap()
    for k in ("remotecontrol_host", "autolocate", "phonecamera_pc"):
        p = cfg.get(f"paths.{k}")
        if p:
            ensure_path(str(p))


# ---------------------------------------------------------------- 解析

CODE = re.compile(r"`([^`]+)`")
ROUTE = re.compile(r"^(?:GET|POST|DELETE|PUT|PATCH)\s+(/\S*)$")


def parse_table() -> tuple:
    """返回 (表里声明的路由集合, 表里提到的上游符号集合)。"""
    if not TABLE.is_file():
        raise SystemExit(f"没有 {TABLE}")
    text = TABLE.read_text(encoding="utf-8")
    routes, symbols = set(), set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        for tok in CODE.findall(line):
            tok = tok.strip()
            m = ROUTE.match(tok)
            if m:
                method, path = tok.split(None, 1)
                routes.add((method, path.strip()))
                continue
            # 上游符号：`Class.attr` / `Class.attr(...)` / 模块级函数
            base = tok.split("(")[0].strip()
            if not base or " " in base or "/" in base:
                continue
            root = base.split(".")[0]
            if root in RESOLVER:
                symbols.add(base)
    return routes, symbols


def actual_routes() -> set:
    src = (ROOT / "control" / "api" / "routes.py").read_text(encoding="utf-8")
    out = set()
    for m in re.finditer(r'r\.add_(get|post|delete|put)\(\s*"([^"]+)"', src):
        out.add((m.group(1).upper(), m.group(2)))
    return {r for r in out if r not in IGNORE_ROUTES}


def main() -> int:
    print("=" * 72)
    print("接口对照表校验（上游 ↔ Control ↔ 文档）")
    print("=" * 72)

    head("0. 挂上游路径")
    try:
        ensure_paths()
        record("S0", "PASS", "三个上游目录已加入 sys.path")
    except Exception as e:                                    # noqa: BLE001
        record("S0", "FAIL", f"挂不上上游路径：{type(e).__name__}: {e}")
        return 1

    head("1. 表里写到的上游符号是否真的存在")
    table_routes, symbols = parse_table()
    if not symbols:
        record("A0", "FAIL", "一个上游符号都没解析到 —— 表的格式变了？")
        return 1
    missing, ok_n = [], 0
    mods = {}
    for sym in sorted(symbols):
        parts = sym.split(".")
        modname = RESOLVER[parts[0]]
        try:
            if modname not in mods:
                mods[modname] = importlib.import_module(modname)
            obj = getattr(mods[modname], parts[0])
            for p in parts[1:]:
                obj = getattr(obj, p)
            ok_n += 1
        except Exception as e:                                # noqa: BLE001
            missing.append((sym, f"{type(e).__name__}: {e}"))
    record("A0", "PASS" if not missing else "FAIL",
           f"{ok_n}/{len(symbols)} 个上游符号都存在"
           + (f"；**缺 {len(missing)} 个**：{missing[:5]}" if missing else ""))

    head("2. 端点两侧是否一一对应（漏登记 / 幽灵端点）")
    actual = actual_routes()
    only_code = sorted(actual - table_routes)      # 注册了但表里没写
    only_table = sorted(table_routes - actual)     # 表里写了但没注册
    record("A1", "PASS" if not only_code else "FAIL",
           f"注册了 {len(actual)} 条路由，全部在表里登记"
           if not only_code else f"**{len(only_code)} 条注册了但没登记**：{only_code[:6]}")
    record("A2", "PASS" if not only_table else "FAIL",
           "表里的每条路由都真的注册过"
           if not only_table else f"**{len(only_table)} 条是幽灵端点**：{only_table[:6]}")

    head("3. 设计方案不再各自维护一份端点表")
    if not DESIGN.is_file():
        record("A3", "SKIP", f"没有 {DESIGN.name}")
    else:
        dtxt = DESIGN.read_text(encoding="utf-8")
        points = "接口对照表.md" in dtxt
        record("A3", "PASS" if points else "FAIL",
               "附录 A 已收敛为指向 docs/接口对照表.md 的指针"
               if points else "附录 A 没有指向 docs/接口对照表.md —— 两份表一定会漂移")

    head("汇总")
    n_fail = sum(1 for _, v, _ in RESULTS if v == "FAIL")
    n_pass = sum(1 for _, v, _ in RESULTS if v == "PASS")
    print(f"  PASS {n_pass}   FAIL {n_fail}")
    if n_fail:
        print("\n  没过的：")
        for cid, v, note in RESULTS:
            if v == "FAIL":
                print(f"    · {cid}: {note}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
