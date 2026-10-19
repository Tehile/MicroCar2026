#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给 MAZE STOP/ESTOP 的中止通道补最后两处缺口（GBK 字节级，CRLF 保持）。

背景（真机 2026-10-20 实证）
----------------------------
`MAZE RUN` 正常发车后发 `MAZE STOP`：
  · 电机 1.1 s 内归零并保持 —— SysTick 的协作式中止块生效；
  · 但 `mazeTask` 恒 `SEARCH`、`motion` 恒 `NULL`、`gyro` 冻结，
    紧接着的第二次 `MAZE RUN` 回 `ERR_BUSY`。

`ERR_BUSY` 的判据是 `linkMazeReq || linkMazeRun`（link.c:2786），
而 `linkMazeReq` 在发车时就被消费掉了，所以 `linkMazeRun==1`
⇒ `LINK_MazeFinished()` 从未执行 ⇒ 主循环**根本没回到 `maze_done`**。

卡点定位
--------
中止时遥测的运动任务是 `TURNAROUND`，它来自
`objectGoTo()`（stratagy.c:330）→ `mouseTurnback()` → `taskturnaround()`。
`objectGoTo` 的主循环是

    while ((cX != cXdst) || (cY != cYdst))
    {
        ...
        mouseGoahead(cNBlock);
        ...
        mouseTurnback(); / mouseTurnleft(); / mouseTurnright();
        ...
        cX = GmcMouse.cX;
        cY = GmcMouse.cY;
    }

循环靠**车真的走**来推进 `cX/cY`。中止后 `setvl/setvr` 被 SysTick 归零、
各运动原语 1 ms 内返回，坐标永远不变 ⇒ 外层 while 变成纯空转：
不打印、不动、不响应，主循环回不到 test.c 的状态机顶部。
（`fastway` / `fastway_90` 里的同型循环是纯路径计算，不驱动运动，会自行结束，
 故不动。）

改法
----
① `stratagy.c` 的 `objectGoTo` 循环体顶部加中止出口（MOUSE_REMOTE 门控）。
② `test.c` 的 `maze_done` 把 `GucMouseTask` 复位成 `WAIT`（该标签本就在
   `#ifdef MOUSE_REMOTE` 内）：否则退出状态机后遥测的 `mazeTask` 仍是
   `SEARCH`，上位机 `running_now()` 会把"待命"误判成"在跑"，
   `force_idle()` 因此平白升级到 ESTOP + MCU RESET。

两个改动都在 `#ifdef MOUSE_REMOTE` 之内 ⇒ 按键比赛档预处理结果逐字节不变，
比赛档 hex 红线不受影响。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- ① stratagy.c
STRATAGY = ROOT / "code_competition" / "STRATAGY" / "stratagy.c"

OBJ_OLD = (
    "    while ((cX != cXdst) || (cY != cYdst))\r\n"
    "\t{\r\n"
    "        ucStep = GucMapStep[cX][cY];\r\n"
)

OBJ_NEW = (
    "    while ((cX != cXdst) || (cY != cYdst))\r\n"
    "\t{\r\n"
    "#ifdef MOUSE_REMOTE\r\n"
    "        /* 远程档协作式中止(2026-10-20 真机): 本循环靠 mouseGoahead/mouseTurn*\r\n"
    "         * 让车真的走, 再由末尾 cX = GmcMouse.cX 读回坐标来推进。中止后\r\n"
    "         * setvl/setvr 已被 SysTick 归零、各运动原语 1ms 内返回, 坐标永远不变\r\n"
    "         * => 外层 while 变成纯空转: 不动、不打印、不响应, 主循环回不到\r\n"
    "         * test.c 的状态机顶部, 于是 mazeTask 恒 SEARCH、LINK_MazeFinished\r\n"
    "         * 永不执行、下一次 MAZE RUN 恒 ERR_BUSY(真机实测)。这里直接退出,\r\n"
    "         * 由调用方一路 unwind 回 maze_done。 */\r\n"
    "        if (LP_AbortRequested()) { ObjectGoTo_Flag=0; return; }\r\n"
    "#endif\r\n"
    "        ucStep = GucMapStep[cX][cY];\r\n"
)

# --------------------------------------------------------------------- ② test.c
TEST = ROOT / "code_competition" / "USER" / "test.c"

DONE_OLD = (
    "maze_done:\r\n"
    "    /* 一次跑位结束(正常到 END, 或被 STOP/ESTOP 中止跳出状态机):\r\n"
    "     * 复位链路\"跑位中\"标志使下一次 MAZE RUN 可被接受, 回 fuction_Choose 待命值守 */\r\n"
    "    LINK_MazeFinished();\r\n"
)

DONE_NEW = (
    "maze_done:\r\n"
    "    /* 一次跑位结束(正常到 END, 或被 STOP/ESTOP 中止跳出状态机):\r\n"
    "     * 复位链路\"跑位中\"标志使下一次 MAZE RUN 可被接受, 回 fuction_Choose 待命值守 */\r\n"
    "    GucMouseTask = WAIT; /* 退出状态机后迷宫任务机确实是 WAIT, 必须如实写回:\r\n"
    "                          * 否则遥测的 mazeTask 一直停在 SEARCH, 上位机\r\n"
    "                          * running_now() 会把待命误判成\"在跑\", force_idle() 因此\r\n"
    "                          * 平白升级到 ESTOP + MCU RESET(2026-10-20 真机)。\r\n"
    "                          * 与下方 maze_run 入口的 GucMouseTask=WAIT 是同一条语义,\r\n"
    "                          * 这里只是提前把\"已回到待命\"这件事表达出来。 */\r\n"
    "    LINK_MazeFinished();\r\n"
)


def patch(path: Path, old: str, new: str, tag: str, apply: bool) -> bool:
    raw = path.read_bytes()
    old_b = old.encode("gbk")
    new_b = new.encode("gbk")
    n = raw.count(old_b)
    print(f"[{tag}] {path.name}: 锚点出现 {n} 次")
    if n != 1:
        print(f"[{tag}] ✗ 锚点不唯一/不存在, 放弃")
        return False
    if new_b in raw:
        print(f"[{tag}] = 已是目标状态(幂等), 跳过")
        return True
    out = raw.replace(old_b, new_b, 1)
    # CRLF 复核
    assert out.count(b"\r\n") == out.count(b"\n"), "出现裸 LF, 中止"
    if not apply:
        print(f"[{tag}] (dry-run) {len(raw)}B -> {len(out)}B  (+{len(out)-len(raw)})")
        return True
    path.write_bytes(out)
    chk = path.read_bytes()
    print(f"[{tag}] ✓ 已写入 {len(chk)}B  CRLF={chk.count(b'\r\n')}  "
          f"裸LF={chk.count(b'\n')-chk.count(b'\r\n')}")
    return True


def main() -> int:
    apply = "--apply" in sys.argv
    ok1 = patch(STRATAGY, OBJ_OLD, OBJ_NEW, "objectGoTo", apply)
    ok2 = patch(TEST, DONE_OLD, DONE_NEW, "maze_done", apply)
    if not (ok1 and ok2):
        return 1
    print("\n" + ("已应用。下一步: build_competition_modes.py 双档重编 + 红线校验"
                  if apply else "dry-run 结束。确认后用 --apply 落地。"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
