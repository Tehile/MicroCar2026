"""P3 §6 三条行为级遗留发现的验证（真机，不发车）。

| # | 项 | 上一轮记录 | 本次判据 |
|---|---|---|---|
| 1 | 卡尔曼 Q/P | 曾推断「远程档 Q/P 被清零」 | `GET IRKALQ`=100、`GET IRKALP`=2000 ⇒ 推断不成立 |
| 1b | 卡尔曼开关 | 参数表 0 / 固件 `kalmanswitch=1` 分裂 | `GET IRKALSW` 与 `HARDWARE/ADC/adc.c:151` 的 1 对照 |
| 2 | 电机 PID | 远程档镜像 `10.75/0.55/0.55`（比赛档 13/0.74/0.9） | `GET MKPL..MKDR` 六项逐项 |
| 3 | TRACKCOORD | 参数表可变，但固件真用 `timer.c:52 const u8 trackcoor=127` | `SETQ` 后 `GET` 变、行为不变 |

用法::

    python p3_findings_check.py COM7
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

#: §6 期望值（来自 docs/P3实机联调测试计划.md 与远程档镜像口径）
EXPECT = {
    "IRKALQ": (100.0, "卡尔曼过程噪声×10000；adc.c 默认 0.01 ⇒ 100"),
    "IRKALP": (2000.0, "卡尔曼测量噪声×10000；adc.c 默认 0.2 ⇒ 2000"),
    "IRKALSW": (1.0, "固件 adc.c:151 kalmanswitch=1（上轮记的是 表0/固件1 分裂）"),
    "MKPL": (10.75, "远程档镜像左轮 P（比赛档 13）"),
    "MKIL": (0.55, "远程档镜像左轮 I（比赛档 0.74）"),
    "MKDL": (0.55, "远程档镜像左轮 D（比赛档 0.9）"),
    "MKPR": (10.20, "远程档镜像右轮 P"),
    "MKIR": (0.50, "远程档镜像右轮 I"),
    "MKDR": (0.50, "远程档镜像右轮 D"),
}
TOL = 1e-4


def main() -> int:
    from host.api import MouseClient

    port = sys.argv[1] if len(sys.argv) > 1 else "COM7"
    mc = MouseClient(auto_session=False, auto_stream=False)
    if not mc.connect(port):
        print("连接失败:", mc.state_detail)
        return 1
    fails = 0
    try:
        mc.send_cmd("ping", timeout=5.0)
        print(f"设备: fw={mc.device.get('fw')} proto={mc.device.get('proto')} "
              f"rst={mc.reset_cause}\n")

        print("[1/1b/2] 参数表读数")
        live = mc.params.get_all()
        for name, (exp, why) in EXPECT.items():
            cur = live.get(name)
            ok = cur is not None and abs(cur - exp) <= max(TOL, abs(exp) * 1e-6)
            if not ok:
                fails += 1
            print(f"  {'✅' if ok else '❌'} {name:<9} live={cur!s:<12} 期望={exp:<8} {why}")

        print("\n[3] TRACKCOORD：参数表可变 vs 固件真用 const 127")
        orig = live.get("TRACKCOORD")
        print(f"  原值 GET = {orig}（docs 记 145；固件 timer.c:52 const u8 trackcoor=127 不读参数表）")
        target = 200.0
        r = mc.send_cmd("params.setq", name="TRACKCOORD", q=int(target * 65536), timeout=4.0)
        got = mc.params.get_raw("TRACKCOORD") / 65536.0
        ok_set = r.ok and abs(got - target) < TOL
        print(f"  SETQ {target} → {r.status}；回读 {got}  "
              f"{'✅ 参数表已变（证明链路可写）' if ok_set else '❌ 未变'}")
        if not ok_set:
            fails += 1
        r = mc.send_cmd("params.setq", name="TRACKCOORD", q=int(round(orig * 65536)), timeout=4.0)
        back = mc.params.get_raw("TRACKCOORD") / 65536.0
        print(f"  恢复 {orig} → {r.status}；回读 {back}  "
              f"{'✅ 已还原' if abs(back - orig) < TOL else '❌ 未还原'}")
        r = mc.send_cmd("params.save", timeout=8.0)
        print(f"  SAVE → {r.status}（把还原值落回 Flash，避免留下 200）")

        print(f"\n{'=' * 66}")
        print("结论: " + ("✅ 全部符合记录" if fails == 0 else f"⚠ {fails} 项与记录不符（见上）"))
        return 0 if fails == 0 else 3
    finally:
        try:
            mc.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
