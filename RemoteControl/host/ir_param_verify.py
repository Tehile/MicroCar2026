# -*- coding: utf-8 -*-
"""红外/增益参数"是否真的驱动固件"专项验收（零运动，不需要发车）。

背景（2026-11-05 实测）：FW 314 之前，参数表 23-28 / 32 / 33-39 / 43 / 47-49 这些 ID
只写进 link.c 的镜像结构（irCfg / powerCfg / sysCfg.trackCoord / linkPtKp 系列），
**固件里没有任何消费者** —— SETQ 能存、GET 能回读，但车的行为一个字节都不变。
真机判据就是"设了没反应"。

本脚本验证修复后的接线：
  P1  默认值快照     23-28 应为 150/60/230/245/240/65；39=127；47-49=2.5/0.01/0.1
  P2  IRTHRWALL 端到端 130 -> 255 应让 wallLeft/wallRight 由 0 变 1（侧墙阈值）
  P3  IRTHRFNEAR 端到端 65 -> 255 应让 wallFront 由 0 变 1（前墙近距判据）
  P4  回读一致性      SETQ -> GET 必须一致（幂等存储层）
  P5  恢复现场        全部参数改回默认值

用法::

    python ir_param_verify.py COM7

判据（期望）::

    P1 全部匹配 / P2 wallL+wallR 都变 1 / P3 wallF 变 1 / P4 一致 / P5 恢复

零运动：只发 PING / GET / SETQ / TELEM，不发任何 MAZE / MOVE / CAL 命令。
"""

from __future__ import annotations

import sys
import time

from host.api.client import MouseClient

# 参数 ID -> (名字, 期望默认值)
EXPECT_DEFAULTS = [
    ("IRTHRWALL", 130.0),
    ("IRTHR45", 150.0),
    ("IRTHRCRIT", 60.0),
    ("IRTHRFLO", 230.0),
    ("IRTHRFMID", 245.0),
    ("IRTHRFHI", 240.0),
    ("IRTHRFNEAR", 65.0),
    ("TRACKCOORD", 127.0),
    ("PTKP", 2.5),
    ("PTKI", 0.01),
    ("PTKD", 0.1),
]

results: list[tuple[str, bool, str]] = []


def check(tag: str, ok: bool, detail: str) -> None:
    results.append((tag, ok, detail))
    print(f"  [{'OK  ' if ok else 'FAIL'}] {tag:46} {detail}")


def snap(mc: MouseClient, settle: float = 1.3) -> dict:
    time.sleep(settle)
    return mc.store.latest()


def main(port: str) -> int:
    mc = MouseClient(auto_session=False, auto_stream=True, auto_reconnect=False)
    print("=" * 78)
    print(f" 红外/增益参数驱动验收   端口 {port}")
    print("=" * 78)

    if not mc.connect(port, 115200):
        print(f"[FATAL] 打不开 {port}: {mc.conn.state_detail}")
        return 2

    pong = [t for _, d, t in mc.cmd.log if t.startswith("PONG")]
    print(f"  指纹: {pong[-1] if pong else '(无 PONG)'}")

    # ------------------------------------------------------------ P1
    print("\n[P1] 默认值快照 —— 这些 ID 以前恒为 0（镜像结构没人初始化）")
    for name, want in EXPECT_DEFAULTS:
        raw = mc.cmd.readback(name)
        got = None if raw is None else raw / 65536.0
        ok = got is not None and abs(got - want) < 1e-3
        check(f"{name} = {want:g}", ok, f"实读 {got}")

    # ------------------------------------------------------------ P2
    print("\n[P2] IRTHRWALL 端到端 —— 侧墙阈值 > 当前读数应立刻判「两侧有墙」")
    base = snap(mc)
    print(f"  基线 irL={base.get('irL')} irR={base.get('irR')}"
          f" wallL={base.get('wallLeft')} wallR={base.get('wallRight')}")
    mc.cmd.set_param("IRTHRWALL", 255)
    hi = snap(mc)
    print(f"  阈值255 irL={hi.get('irL')} irR={hi.get('irR')}"
          f" wallL={hi.get('wallLeft')} wallR={hi.get('wallRight')}")
    check("IRTHRWALL=255 -> wallLeft/Right 均为 1",
          hi.get("wallLeft") == 1 and hi.get("wallRight") == 1,
          f"wallL={hi.get('wallLeft')} wallR={hi.get('wallRight')}")
    mc.cmd.set_param("IRTHRWALL", 130)
    back = snap(mc)
    check("IRTHRWALL=130 -> 恢复 0",
          back.get("wallLeft") == 0 and back.get("wallRight") == 0,
          f"wallL={back.get('wallLeft')} wallR={back.get('wallRight')}")

    # ------------------------------------------------------------ P3
    print("\n[P3] IRTHRFNEAR 端到端 —— 前墙近距阈值 > 读数应判「前方有墙」")
    print(f"  基线 irF1={back.get('irF1')} irF2={back.get('irF2')} wallF={back.get('wallFront')}")
    mc.cmd.set_param("IRTHRFNEAR", 255)
    f_hi = snap(mc)
    print(f"  阈值255 irF1={f_hi.get('irF1')} irF2={f_hi.get('irF2')} wallF={f_hi.get('wallFront')}")
    check("IRTHRFNEAR=255 -> wallFront = 1", f_hi.get("wallFront") == 1,
          f"wallF={f_hi.get('wallFront')}")
    mc.cmd.set_param("IRTHRFNEAR", 65)
    f_back = snap(mc)
    check("IRTHRFNEAR=65 -> 恢复 0", f_back.get("wallFront") == 0,
          f"wallF={f_back.get('wallFront')}")

    # ------------------------------------------------------------ P4
    print("\n[P4] 回读一致性（存储层幂等）")
    for name, want in (("IRTHR45", 111.0), ("PTKP", 3.25), ("TRACKCOORD", 100.0)):
        ok, st = mc.cmd.set_param(name, want)
        got = mc.cmd.readback(name)
        gotf = None if got is None else got / 65536.0
        check(f"SETQ {name}={want:g} 回读一致", ok and gotf is not None and abs(gotf - want) < 1e-3,
              f"{st} / 回读 {gotf}")
        mc.cmd.set_param(name, dict(EXPECT_DEFAULTS)[name])

    # ------------------------------------------------------------ P5
    print("\n[P5] 恢复现场")
    bad = []
    for name, want in EXPECT_DEFAULTS:
        mc.cmd.set_param(name, want)
        got = mc.cmd.readback(name)
        gotf = None if got is None else got / 65536.0
        if gotf is None or abs(gotf - want) > 1e-3:
            bad.append(f"{name}={gotf}(want {want:g})")
    check("全部参数已恢复默认", not bad, "、".join(bad) if bad else "11/11 恢复")

    final = snap(mc)
    print(f"\n  末帧 wallL={final.get('wallLeft')} wallR={final.get('wallRight')}"
          f" wallF={final.get('wallFront')}  链路统计 "
          f"{ {k: v for k, v in mc.conn.stats().items() if k in ('unified','bad_crc','seq_lost')} }")
    mc.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n" + "=" * 78)
    print(f"结论: {passed} 通过 / {len(results) - passed} 失败")
    for tag, ok, detail in results:
        if not ok:
            print(f"  !! {tag}  ->  {detail}")
    print("=" * 78)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "COM7"))
