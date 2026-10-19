"""GBK 字节级补丁：MAZE STOP 退出链路的兜底（2026-10-20 真机第三轮）。

背景（真机取证）
----------------
`MAZE STOP` 之后偶发"电机停了、`mazeTask` 恒 `SEARCH`、`objectGoToFlag=1`、
其余遥测全部冻结（只有 `ms8` 在走）、第二次 `MAZE RUN` 恒 `ERR_BUSY`"。

逻辑推导：`objectGoTo()` 的循环体第一条就是 `LP_AbortRequested()` 检查（上一版
`721d568` 加的），却完全没反应 ⇒ 循环体压根没开始 ⇒ 卡在**循环体之前**那句
`mapStepEdit(cXdst, cYdst)` 里。它是纯计算的洪水填充：

```c
while (n) { GucMapStep[cX][cY] = ucStep++;  ... }
```

`ucStep` 是 `uchar`，中止瞬间停在"半更新"地图上时 `GucMapStep[..] > ucStep`
可能永远成立 ⇒ `n` 永不减到 0 ⇒ 死循环，而它不查中止标志。

三处改动（全部远程档限定，按键比赛档预处理后逐字节不变）
--------------------------------------------------------
① `stratagy.c` `objectGoTo()` 入口：中止已挂就不再启动新一轮规划（省掉那次填充）。
② `stratagy.c` `mapStepEdit()` 循环内：中止即返回。
③ `link.c` `LINK_Tick()`：**兜底**——中止请求挂着且 `linkMazeRun` 仍为 1 超过
   `LINK_ABORT_WATCH_MS`，直接 `Sys_Soft_Reset()`。放在中断上下文，所以主循环
   卡在任意死循环里都拦得住；与 `MCU RESET` 等价，但比"人工断电"好。

用法:
    python tools/patch_maze_stop_bailout.py          # dry-run
    python tools/patch_maze_stop_bailout.py --apply
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
STRATAGY = ROOT / "code_competition" / "STRATAGY" / "stratagy.c"
LINK = ROOT / "code_competition" / "REMOTE" / "link.c"

# ---------------- ① objectGoTo 入口早退 ----------------
OG_OLD = "\tledon(4);ledon(5);\r\n\tObjectGoTo_Flag=1;\r\n"
OG_NEW = (
    "#ifdef MOUSE_REMOTE\r\n"
    "    /* 中止请求已挂 => 不再启动新一轮规划。理由: 下面的 mapStepEdit 是纯计算的\r\n"
    "     * 洪水填充, 在\"中止瞬间的半更新地图\"上会退化成死循环(真机 2026-10-20:\r\n"
    "     * objectGoToFlag 恒 1、其余遥测全冻结、第二次 MAZE RUN 恒 ERR_BUSY)。 */\r\n"
    "    if (LP_AbortRequested()) return;\r\n"
    "#endif\r\n"
    "\tledon(4);ledon(5);\r\n"
    "\tObjectGoTo_Flag=1;\r\n"
)

# ---------------- ② mapStepEdit 循环内中止检查 ----------------
# 该行前后有对齐用的行尾空格, 故不用整段文本锚点, 改为"按行定位 + 行前插入"。
MS_LINE = "GucMapStep[cX][cY] = ucStep++;"
MS_INSERT = [
    "#ifdef MOUSE_REMOTE",
    "        /* 中止请求挂着就退出。本函数不驱动电机, SysTick 的中止段对它是无效的:",
    "         * ucStep 是 uchar, 地图处于半更新状态时回绕会让 GucMapStep[..] > ucStep",
    "         * 永远成立 => n 永不减到 0 => 死循环(真机 2026-10-20 实测)。",
    "         * 返回的是半成品步数图, 但调用方的中止检查会一路 unwind 回 maze_done。 */",
    "        if (LP_AbortRequested()) return;",
    "#endif",
]

# ---------------- ③ LINK_Tick 中止兜底 ----------------
DECL_OLD = "static u16 linkMcuResetDelay;\r\n#endif\r\n"
DECL_NEW = (
    "static u16 linkMcuResetDelay;\r\n"
    "/* MAZE STOP / ESTOP 的中止**兜底**超时(ms)。协作式中止要求主循环自己从迷宫\r\n"
    " * 状态机里爬出来, 但迷宫里有不查中止标志的纯计算循环(objectGoTo->mapStepEdit\r\n"
    " * 的洪水填充等) —— 真机 2026-10-20 实测偶发卡死: 电机停了, 但 mazeTask 恒\r\n"
    " * SEARCH、LINK_MazeFinished() 永不执行、第二次 MAZE RUN 恒 ERR_BUSY。\r\n"
    " * 中止请求挂着而 linkMazeRun 仍为 1 超过这个时间即软复位收场(进程等价于\r\n"
    " * MCU RESET), 保证\"MAZE STOP 一定能把车带出来\", 不必人工断电。\r\n"
    " * 正常退出实测 0.2~1.0s, 取 2000ms 留足余量、避免误伤。 */\r\n"
    "#define LINK_ABORT_WATCH_MS 2000\r\n"
    "static u16 linkAbortWatch;\r\n"
    "#endif\r\n"
)

TICK_OLD = (
    "\tif (linkMcuResetDelay)\r\n"
    "\t{\r\n"
    "\t\tlinkMcuResetDelay--;\r\n"
    "\t\tif (linkMcuResetDelay == 0)\r\n"
    "\t\t\tSys_Soft_Reset();\r\n"
    "\t}\r\n"
    "#endif\r\n"
)
TICK_NEW = (
    TICK_OLD
    + "\r\n"
    "\t/* 0a2. MAZE STOP / ESTOP 退出兜底(2026-10-20 真机)。与上一段同理放在中断\r\n"
    "\t * 上下文: 主循环可能卡在某个不查中止标志的纯计算死循环里, 只有这里能救。\r\n"
    "\t * 判据只用\"中止请求挂着\"+\"迷宫仍标记为在跑\" —— 正常退出时 maze_done 会\r\n"
    "\t * 先调 LINK_MazeFinished() 把 linkMazeRun 清 0, 本段随即复位计时, 不会误触发。 */\r\n"
    "\tif (LP_AbortRequested() && linkMazeRun)\r\n"
    "\t{\r\n"
    "\t\tif (++linkAbortWatch >= LINK_ABORT_WATCH_MS)\r\n"
    "\t\t{\r\n"
    "\t\t\tlinkAbortWatch = 0;\r\n"
    "\t\t\tSys_Soft_Reset(); /* 复位原因记成 SW, 下一次 PING 可见 */\r\n"
    "\t\t}\r\n"
    "\t}\r\n"
    "\telse\r\n"
    "\t{\r\n"
    "\t\tlinkAbortWatch = 0;\r\n"
    "\t}\r\n"
)


def insert_before_line(path: pathlib.Path, needle: str, lines_in: list[str]) -> str:
    """在某行之前插入若干行（按行定位，避免行尾空格/缩进差异导致锚点失配）。"""
    text = path.read_bytes().decode("gbk")
    lines = text.split("\r\n")
    hits = [i for i, ln in enumerate(lines) if ln.strip() == needle]
    if len(hits) != 1:
        return f"✗ {path.name}: 行锚点 {needle!r} 命中 {len(hits)} 次（应为 1）"
    i = hits[0]
    lines[i:i] = lines_in
    out = "\r\n".join(lines).encode("gbk")
    path.write_bytes(out)
    return f"✓ {path.name}: 在 {needle!r} 前插入 {len(lines_in)} 行, 共 {len(out)} 字节"


def patch(path: pathlib.Path, rep: list[tuple[str, str]]) -> str:
    raw = path.read_bytes()
    try:
        text = raw.decode("gbk")
    except UnicodeDecodeError as e:
        return f"✗ {path.name}: 不是合法 GBK ({e})"
    crlf_before = text.count("\r\n")
    for old, new in rep:
        n = text.count(old)
        if n != 1:
            return f"✗ {path.name}: 锚点命中 {n} 次（应为 1）\n---\n{old!r}"
        text = text.replace(old, new, 1)
    out = text.encode("gbk")
    crlf_after = out.decode("gbk").count("\r\n")
    if crlf_after - crlf_before != sum(x[1].count("\r\n") - x[0].count("\r\n") for x in rep):
        return f"✗ {path.name}: CRLF 数目异常 {crlf_before} -> {crlf_after}"
    path.write_bytes(out)
    return f"✓ {path.name}: +{len(out) - len(raw)} 字节, CRLF {crlf_before} -> {crlf_after}"


def main() -> int:
    apply = "--apply" in sys.argv
    jobs = [
        (STRATAGY, [(OG_OLD, OG_NEW)]),
        (LINK, [(DECL_OLD, DECL_NEW), (TICK_OLD, TICK_NEW)]),
    ]
    if not apply:
        print("=== dry-run（加 --apply 生效）===")
        for path, rep in jobs:
            for old, _ in rep:
                print(f"{path.name}: 锚点命中 {path.read_bytes().decode('gbk').count(old)} 次")
        n = sum(1 for ln in STRATAGY.read_bytes().decode("gbk").split("\r\n")
                if ln.strip() == MS_LINE)
        print(f"{STRATAGY.name}: 行锚点 {MS_LINE!r} 命中 {n} 次")
        return 0
    for path, rep in jobs:
        print(patch(path, rep))
    print(insert_before_line(STRATAGY, MS_LINE, MS_INSERT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
