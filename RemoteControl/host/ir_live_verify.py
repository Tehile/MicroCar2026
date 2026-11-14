#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""红外"活着"验收: 判断四路红外是不是随环境变化(而不是冻结在某个常数)。

背景(2026-10-23 真机定位):
  远程档把 `LP_SyncOut` 每 1 ms 把 Qf/Pf 从 `irCfg` 推回固件, 而本工程没有 CORE_Init,
  `irCfg.kalQ/kalP` 恒为 0 => 卡尔曼增益 k = 0/(0+0) = NaN => f1/f2/f/l/r 全 NaN =>
  `rawf1/rawf2/rawl/rawr` 永久冻结, 四路红外读数不再变化(且没有任何报错)。
  修法: LP_SyncIn 回填 kalQ/kalP/kalOn + ApplyOne 把 0 当"未设置"。

判据:
  P1 参数: IRKALQ / IRKALP 必须非 0(100 / 2000), 阈值 23-28 为 150/60/230/245/240/65
  P2 抖动: 采样窗口内每路 ADC 的峰峰值 > 0 —— 冻结时是**逐位相同**的常数
  P3 响应: 若 --wait 给了秒数, 期间移动车体/挡手, 任一路 ADC 变化超过阈值即判"活"

用法:
    python ir_live_verify.py COM7
    python ir_live_verify.py COM7 --seconds 12      # 采样 12 秒, 期间请移动车体
    python ir_live_verify.py COM7 --json out.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time

sys.path.insert(0, ".")

from host.api.client import MouseClient  # noqa: E402

ADC_RE = re.compile(r"adcf1:(-?\d+)\s+adcf2:(-?\d+)\s+adcl:(-?\d+)\s+adcr:(-?\d+)")

#: 通道顺序与固件 getdistance() 的采样顺序一致
CH = ("f1", "f2", "l", "r")

#: 期望的阈值默认值(与 timer.c 的变量初值一致)
THR_EXPECT = {23: 150.0, 24: 60.0, 25: 230.0, 26: 245.0, 27: 240.0, 28: 65.0}


def read_params(mc: MouseClient) -> dict:
    mc.send_cmd("params.get_all", timeout=20.0)
    return {pid: q / 65536.0 for pid, q in mc.cmd.params.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("port", nargs="?", default="COM7")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=10.0, help="采样时长(期间请移动车体)")
    ap.add_argument("--gap", type=float, default=0.5, help="两次 CAL SENSOR 的间隔")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    mc = MouseClient(auto_session=False, auto_stream=False, auto_reconnect=False)
    if not mc.connect(args.port, args.baud):
        print(f"连接 {args.port} 失败: {mc.conn.state_detail}")
        return 2

    cur = {"line": None}
    _orig = mc.cmd.feed_line

    def _hook(line: str) -> None:
        if "adcf1" in line:
            cur["line"] = line
        return _orig(line)

    mc.cmd.feed_line = _hook

    try:
        print("=" * 68)
        print(f"设备指纹: {mc.cmd.device}")
        print("=" * 68)

        # ---------------- P1 参数 ----------------
        print("\n[P1] 卡尔曼与阈值参数")
        par = read_params(mc)
        ok1 = True
        for name, pid, lo in (("IRKALQ", 29, 1.0), ("IRKALP", 30, 1.0)):
            v = par.get(pid, 0.0)
            good = v >= lo
            ok1 &= good
            print(f"   {name:10} id={pid:<3} = {v:8.2f}  {'OK' if good else 'FAIL (为 0 => 卡尔曼 NaN)'}")
        for pid, exp in THR_EXPECT.items():
            v = par.get(pid, 0.0)
            good = abs(v - exp) < 0.51
            ok1 &= good
            print(f"   阈值 id={pid:<3} = {v:8.2f}  期望 {exp:6.1f}  {'OK' if good else 'FAIL'}")
        sw = par.get(31, 0.0)
        print(f"   IRKALSW    id=31  = {sw:8.2f}  (1=卡尔曼开 / 0=直通, 两者都应能出真实读数)")

        # ---------------- P2/P3 采样 ----------------
        print(f"\n[P2/P3] 原始 ADC 采样 {args.seconds:.0f}s —— 请现在移动车体或用手挡传感器")
        samples = []
        t0 = time.time()
        while time.time() - t0 < args.seconds:
            cur["line"] = None
            mc.send_raw("CAL SENSOR", timeout=5.0)
            time.sleep(args.gap)
            m = ADC_RE.search(cur["line"] or "")
            if m:
                samples.append(tuple(int(m.group(i)) for i in range(1, 5)))
        if len(samples) < 3:
            print("   采样太少(链路不稳?), 无法判定")
            return 3

        print(f"\n   共 {len(samples)} 个样本:")
        for i, s in enumerate(samples):
            print("     #%02d  " % i + "  ".join(f"{n}={v:<5}" for n, v in zip(CH, s)))

        stats, ok2, ok3 = {}, True, False
        for k, name in enumerate(CH):
            vals = [s[k] for s in samples]
            lo, hi = min(vals), max(vals)
            spread = hi - lo
            stats[name] = {"min": lo, "max": hi, "spread": spread, "first": vals[0], "last": vals[-1]}
            if spread == 0:
                ok2 = False
            if spread >= 20:
                ok3 = True
            print(f"   {name:3} min={lo:<6} max={hi:<6} 峰峰={spread:<5} {'冻结!' if spread == 0 else ''}")

        # 判"整组逐位相同": 四路全 0 峰峰 = 典型冻结
        allfrozen = all(stats[c]["spread"] == 0 for c in CH)
        print()
        if allfrozen:
            print("   [P2] FAIL  四路峰峰全为 0 —— 读数冻结, 与 Q/P=0 的 NaN 症状一致")
        elif ok2:
            print("   [P2] PASS  至少一路有自然抖动")
        else:
            print("   [P2] WARN  部分通道峰峰为 0(可能是该方向确实没有反射, 见 P3)")

        if ok3:
            print("   [P3] PASS  有通道随动作明显变化 => 红外是实时的")
        elif ok2:
            print("   [P3] N/A   采样期间没有明显动作; 但上面 P2 已有自然抖动,")
            print("              足以排除'冻结'(冻结时是逐位相同的常数, 峰峰恒为 0)")
        else:
            print("   [P3] FAIL  没有任何通道变化超过 20 —— 若这期间确实移动过车体, 则仍未修好")

        # 判定: 参数正常 且 (有自然抖动 或 有明显响应)。
        # 只靠"有没有动作"判会把'车静止 + 传感器正常'误判成 FAIL —— 冻结的特征是
        # **峰峰恒为 0**, 所以自然抖动同样能证明链路是活的。
        verdict = ok1 and (ok2 or ok3)
        print("\n" + "=" * 68)
        print(f"结论: {'PASS —— 红外实时, 参数正常' if verdict else 'FAIL —— 见上面各项'}")
        print("=" * 68)

        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump({"params": {str(k): v for k, v in par.items()},
                           "stats": stats, "ok_param": ok1, "ok_live": ok3,
                           "verdict": bool(verdict)}, f, ensure_ascii=False, indent=2)
            print(f"已写入 {args.json}")
        return 0 if verdict else 1
    finally:
        mc.cmd.feed_line = _orig
        mc.close()


if __name__ == "__main__":
    raise SystemExit(main())
