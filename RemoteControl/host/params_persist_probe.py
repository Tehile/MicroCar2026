"""定点探针: `SETQ` → `SAVE` → `MCU RESET` 之后参数到底有没有留下。

背景(2026-10-07 真机发现): 早期整表验收脚本(已随 v2.6.1 移除)的第 6 步
("写入改动 → SAVE → 复位 → 应读到改动")**读回了原值**。而第 2 步已证明
`SETQ` 本身是生效的(回读 == 写入值)。所以怀疑点落在 `SAVE` / 复位后加载。

更早的线索: 设备上 24 项参数值与固件里的**编译期默认值完全一致**
(`trackRatioL = 62.59*0.85 = 53.2015`、`distToWall = 42`、`battLowMv = 7000` …)
⇒ 说明复位后加载的很可能是 `link_ParamFromSys()` 的源码默认值, 而不是 Flash 页。

本脚本把每一步都打出来(含每条命令的 ACK 状态与独立回读), 用来**区分**:

  H1  `SETQ` 在某一步失败 ⇒ SAVE 写的是旧值;
  H2  `SAVE` 或"复位后加载"失效 ⇒ 参数永远回落源码默认值。

判读: 若 [3] 确认 `SAVE` 前 RAM 已是新值、`SAVE` 回 OK, 而 [5] 复位后变回原值,
      则 H2 成立。

用法:
    python params_persist_probe.py COM7            # 用 BATTLOWMV(行为无害)
    python params_persist_probe.py COM7 --param BATTLOWMV --delta 100 --rounds 2
"""
from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, ".")


def main() -> int:
    ap = argparse.ArgumentParser(prog="params_persist_probe.py")
    ap.add_argument("port", nargs="?", default="COM7")
    ap.add_argument("--param", default="BATTLOWMV")
    ap.add_argument("--delta", type=int, default=100)
    ap.add_argument("--rounds", type=int, default=2, help="重复几轮(排除偶发)")
    args = ap.parse_args()

    from host.protocol.commands import PARAM_IDS
    from host.api import MouseClient

    if args.param not in PARAM_IDS:
        print("未知参数:", args.param)
        return 2

    print("=" * 78)
    print(f"参数持久化定点探针  {args.port}  {args.param}  Δ={args.delta}  "
          f"{args.rounds} 轮")
    print("=" * 78)

    mc = MouseClient(auto_session=False, auto_stream=False)
    if not mc.connect(args.port):
        print("连接失败:", mc.state_detail)
        return 1

    nid = PARAM_IDS[args.param]
    try:
        rp = mc.send_cmd("ping", timeout=5.0)
        print(f"\n指纹: proto/fw={mc.device.get('proto')}/{mc.device.get('fw')} "
              f"txbuf={mc.device.get('txbuf')} rst={mc.reset_cause}")

        orig = mc.params.get_raw(args.param)
        print(f"原始值 {args.param} = {orig}  ({orig / 65536.0:.6f})")

        for rnd in range(1, args.rounds + 1):
            new = orig + args.delta * rnd
            print(f"\n{'─' * 78}\n[轮 {rnd}] 目标写入 = {new}  ({new / 65536.0:.6f})")

            r1 = mc.send_cmd("params.setq", name=args.param, q=new, timeout=4.0)
            print(f"  SETQ  → ok={r1.ok} status={r1.status}")
            rb = mc.params.get_raw(args.param)
            print(f"  回读  → {rb}   {'✅ == 写入值' if rb == new else '❌ 与写入值不符'}")

            r2 = mc.send_cmd("params.save", timeout=8.0)
            print(f"  SAVE  → ok={r2.ok} status={r2.status}")

            # SAVE 后再连发一次 SAVE(排除"第一次写没成"的偶发)
            r2b = mc.send_cmd("params.save", timeout=8.0)
            print(f"  SAVE×2→ ok={r2b.ok} status={r2b.status}")

            rb2 = mc.params.get_raw(args.param)
            print(f"  复位前回读 → {rb2}   "
                  f"{'✅ RAM 仍是写入值' if rb2 == new else '❌ RAM 已变'}")

            res = mc.conn.soft_reset(verify=True, timeout=12.0)
            print(f"  MCU RESET → rst={(res.data or {}).get('rst')} "
                  f"(accepted={(res.data or {}).get('accepted')})")
            time.sleep(1.2)

            after = mc.params.get_raw(args.param)
            verdict = ("✅ 持久化成功" if after == new
                       else f"❌ 复位后回落为 {after} (= 复位前原值 {orig})"
                       if after == orig else f"❌ 复位后变成 {after}")
            print(f"  复位后回读 → {after}   {verdict}")

        print(f"\n{'=' * 78}\n复位后完整参数表(与源码默认值对照)\n{'=' * 78}")
        tbl = mc.params.get_all()
        for n in sorted(PARAM_IDS, key=lambda x: PARAM_IDS[x]):
            v = tbl.get(n)
            print(f"    {n:<12}{'' if v is None else f'{v:>16.6f}'}")
    finally:
        try:
            mc.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
