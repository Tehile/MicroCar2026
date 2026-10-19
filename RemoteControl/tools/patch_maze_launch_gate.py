"""字节级改写 `USER/test.c` 的"等红外遮挡才发车"段（远程档整段跳过）。

背景（2026-10-20 真机）：`maze_run` 入口 `parameter_search()` 之后有一段

    if(GucMouseTask!=SPURT_45 && GucMouseTask!=SPURT_90)
    { while(1){ if(disf2<20){goaheadnum=2; break;} }  ledon(6);delay_ms(1000);ledoff(0); }

它是**没有远程链路时的手动发车手段** —— 操作者用手/挡板挡住车头红外（disf2<20）
程序才认为"已在起跑位"并起步。远程发车由 `MAZE RUN` 显式触发, 语义上已就位,
这段必须跳过; 否则悬空/空旷工况 disf2 恒 >100 ⇒ while(1) 永久自旋
（实测: 回 ACK OK 但 task 恒 WAIT、一步不走、MAZE STOP 退不出来、只能 MCU RESET）。

改法：整段包进 `#ifdef MOUSE_REMOTE ... #else ... #endif` ——
  · 远程档: 预处理后这段消失, 直接往下发车;
  · 按键比赛档: 预处理结果与改前**逐字节一致**(hex 红线不变)。

⚠️ 本文件是 GBK + 全 CRLF, 只能用字节级替换; Edit 工具会把它写成 UTF-8/LF。
"""
from __future__ import annotations

import sys

PATH = "code_competition/USER/test.c"

OLD = (
    "\tparameter_search();\r\n"
    "\t\r\n"
    "\tif(GucMouseTask!=SPURT_45 && GucMouseTask!=SPURT_90)\r\n"
    "\t{\r\n"
    "\t\twhile(1)\r\n"
    "\t\t{\r\n"
    "\t\t\tif(disf2<20){goaheadnum=2; break;} \r\n"
    " \r\n"
    "\t\t}\r\n"
    "\t\tledon(6);delay_ms(1000);ledoff(0);\r\n"
    "\t}\r\n"
)

NEW = (
    "\tparameter_search();\r\n"
    "\t\r\n"
    "#ifdef MOUSE_REMOTE\r\n"
    "\t/* 远程档(2026-10-20 修): 下面这段\"等前方红外被遮挡(disf2<20)才起步\"是\r\n"
    "\t * **没有远程链路时的手动发车手段** —— 操作者用手/挡板挡住车头红外, 程序才\r\n"
    "\t * 认为\"已在起跑位\"。远程发车由 `MAZE RUN` 显式触发, 语义上本就已就位, 故整段跳过:\r\n"
    "\t *   · 不等待遮挡 —— 悬空/空旷工况 disf2 恒 >100, 原来的 while(1) 会永久自旋\r\n"
    "\t *     (真机实测: ACK OK 但 task 恒 WAIT、一步不走, 且 MAZE STOP 退不出来,\r\n"
    "\t *      只能 MCU RESET);\r\n"
    "\t *   · 不打那 1 s 的 LED6 就位提示 —— 远程档的发车时刻标记是 `MAZE RUN` 被接受时\r\n"
    "\t *     4 灯齐亮 300 ms(见 link.c 的 LINK_LAUNCH_MARK_MS)。\r\n"
    "\t * goaheadnum 保持原值: 活代码里它只进遥测帧的 U_GOAHEAD 字节, 不参与控制流\r\n"
    "\t * (唯一读取点在下方已被注释掉的 SPURT 段), 故不影响起步。\r\n"
    "\t * ※ 整段包在 #ifdef 里 => 按键比赛档预处理后与改前**逐字节一致**。 */\r\n"
    "#else\r\n"
    "\tif(GucMouseTask!=SPURT_45 && GucMouseTask!=SPURT_90)\r\n"
    "\t{\r\n"
    "\t\twhile(1)\r\n"
    "\t\t{\r\n"
    "\t\t\tif(disf2<20){goaheadnum=2; break;} \r\n"
    " \r\n"
    "\t\t}\r\n"
    "\t\tledon(6);delay_ms(1000);ledoff(0);\r\n"
    "\t}\r\n"
    "#endif\r\n"
)


def main(apply: bool) -> int:
    raw = open(PATH, "rb").read()
    if b"\r\n" not in raw:
        print("✗ 文件不是 CRLF —— 中止（不该走到这里）")
        return 1

    try:
        old_b = OLD.encode("gbk")
        new_b = NEW.encode("gbk")
    except UnicodeEncodeError as e:
        print(f"✗ 新文本含 GBK 无法编码的字符: {e}")
        return 1

    n = raw.count(old_b)
    print(f"锚点命中次数: {n}  (必须恰好 1)")
    if n != 1:
        print("✗ 锚点不唯一或不存在，中止")
        return 1

    if not apply:
        i = raw.find(old_b)
        print("\n--- 现状(字节切片, 不是字符偏移) ---")
        print(raw[i: i + len(old_b)].decode("gbk"))
        print("\n--- 改后 ---")
        print(NEW)
        print("(dry-run，未写盘; 加 --apply 执行)")
        return 0

    out = raw.replace(old_b, new_b, 1)
    open(PATH, "wb").write(out)

    # 复核: 编码/行尾/结构
    chk = open(PATH, "rb").read()
    crlf = chk.count(b"\r\n")
    lf_only = chk.count(b"\n") - crlf
    chk.decode("gbk")
    print(f"\n✓ 已写盘: {len(raw)} -> {len(chk)} B")
    print(f"  CRLF={crlf}  LF-only={lf_only} (必须 0)")
    print(f"  #ifdef MOUSE_REMOTE 数量 = {chk.count(b'#ifdef MOUSE_REMOTE')}, "
          f"#endif 总数 = {chk.count(b'#endif')}")
    # 比赛档视角: 去掉 MOUSE_REMOTE 区块后应等于原文件去掉同法处理的结果
    print(f"  锚点残留(应为 0): {chk.count(old_b)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--apply" in sys.argv))
