"""V2 后半真机验收: `SETQ` 写参数 + `SAVE` 落 Flash, 且 **page 115 不被破坏**。

判据(设计方案 §5.2 V2):
  「24 项读回正确; `SAVE` 后重上电仍生效(page 115 未被破坏)」

做法刻意做成**可逆且自证**的:

  1. `GET ALL` 取快照 A(24 项), 顺带复验 TX 缓冲修复后的"完整 24 项";
  2. 选一个**行为无害**的参数(默认 `BATTLOWMV`, 只影响低电显示档, 固件对低电
     无任何保护动作 ⇒ 改它不会让车动、不会改标定行为), 先写成 `orig+Δ`;
  3. `GET` 回读 == `orig+Δ`  ⇒ 证明 `SETQ` **真的写进去了**(不是空回 OK);
  4. 写回 `orig`, `GET` 回读 == `orig` ⇒ 先把现场恢复干净;
  5. `SAVE` ⇒ 写入 Flash;
  6. `MCU RESET`(软复位, **不擦参数**) 后 `GET ALL` == A(24/24)
     ⇒ 证明 Flash 参数页重新加载正常、**page 115 未被破坏**;
  7. **强证明**(可选): 写 `orig+Δ` → `SAVE` → `MCU RESET` → 读到 `orig+Δ`
     ⇒ 证明 `SAVE` 确实把**改动**持久化了(而不是"其实什么都没写");
     然后写回 `orig` → `SAVE` → `MCU RESET` → 再读到 A ⇒ 现场归位。

⚠️ 本脚本**不发任何运动命令**(无 `MOVE*`), 不需要 `--launch` 之外的额外授权。
   写参数的授权用 `--write`。默认只读。

用法:
    python params_write_test.py COM7                 # 只读: 只打 24 项参数 + 校验
    python params_write_test.py COM7 --write         # 走 1-6(可逆, 只改 BATTLOWMV)
    python params_write_test.py COM7 --write --strong # 再加第 7 步(净变更为 0)
    python params_write_test.py COM7 --write --param BATTLOWMV --delta 100
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")


def q2f(q: int) -> float:
    return int(q) / 65536.0


def fw_fingerprint_expect():
    try:
        t = Path("../code_stm32f1_mouse/HARDWARE/link.h").read_text(encoding="utf-8")
        return (int(re.search(r"#define\s+LINK_PROTO_VER\s+(\d+)", t).group(1)),
                int(re.search(r"#define\s+LINK_FW_VER\s+(\d+)", t).group(1)))
    except Exception:
        return None, None


def get_all_raw(mc, label: str) -> dict:
    """发 `GET ALL` 并把 24 项原始 Q16.16 抓回来(不依赖缓存语义)。"""
    from host.protocol.commands import PARAM_IDS, PARAM_NAMES
    with mc.cmd._lock:
        mc.cmd.params.clear()
    r = mc.send_cmd("params.get_all", timeout=4.0)
    want = set(PARAM_IDS.values())
    mc.wait(lambda: want.issubset(set(mc.cmd.params)), timeout=1.5)
    got = dict(mc.cmd.params)
    miss = sorted(n for n, i in PARAM_IDS.items() if i not in got)
    print(f"    {label}: 回 {len(got)}/24 项" + (f"  ⚠ 缺 {miss}" if miss else "  ✅ 完整"))
    return got


def show_table(mc, raw: dict) -> None:
    from host.protocol.commands import PARAM_IDS, PARAM_META
    for n in sorted(PARAM_IDS, key=lambda x: PARAM_IDS[x]):
        i = PARAM_IDS[n]
        if i not in raw:
            continue
        unit, dec, note = PARAM_META.get(n, ("", 4, ""))
        print(f"      {n:<10}{q2f(raw[i]):>16.{dec}f} {unit:<12} {note}")


def main() -> int:
    ap = argparse.ArgumentParser(prog="params_write_test.py")
    ap.add_argument("port", nargs="?", default="COM7")
    ap.add_argument("--write", action="store_true", help="真的写参数(默认只读)")
    ap.add_argument("--param", default="BATTLOWMV",
                    help="被扰动的参数名(默认 BATTLOWMV: 只影响低电显示档, 行为无害)")
    ap.add_argument("--delta", type=int, default=100,
                    help="Q16.16 原始值扰动步长(默认 100 = 0.0015)")
    ap.add_argument("--strong", action="store_true",
                    help="加做第 7 步: 证明 SAVE 确实持久化了『改动』(净变更仍为 0)")
    args = ap.parse_args()

    from host.protocol.commands import PARAM_IDS
    from host.api import MouseClient

    if args.param not in PARAM_IDS:
        print(f"未知参数 {args.param}; 可用: {sorted(PARAM_IDS)}")
        return 2
    if args.param in ("SPEEDL", "SPEEDR", "TRACKL", "TRACKR", "GYRORATIO", "ENCANGLE"):
        print(f"⚠ {args.param} 会直接影响运动/里程, 本测试不要选它。")
        return 2

    print("=" * 78)
    print(f"V2 参数写入验收  端口={args.port}  参数={args.param}  Δ={args.delta}"
          f"{'  【会写 Flash】' if args.write else '  (只读)'}")
    print("=" * 78)

    mc = MouseClient(auto_session=False, auto_stream=False)
    if not mc.connect(args.port):
        print("连接失败:", mc.state_detail)
        return 1

    ok_all = True
    try:
        rp = mc.send_cmd("ping", timeout=5.0)
        if not rp.ok:
            print("!! PING 不通, 退出")
            return 1
        ep, ef = fw_fingerprint_expect()
        print(f"\n[0] 指纹 proto/fw={mc.device.get('proto')}/{mc.device.get('fw')} "
              f"txbuf={mc.device.get('txbuf')} rst={mc.reset_cause} "
              f"(源码 {ep}/{ef})")

        print("\n[1] GET ALL 基线")
        A = get_all_raw(mc, "快照 A")
        show_table(mc, A)
        if len(A) != 24:
            ok_all = False
            print("    ⚠ 未回满 24 项 —— 这正是 TX 缓冲缺陷的症状, 请核对板上固件")

        if not args.write:
            print("\n未指定 --write —— 只读模式, 到此为止。")
            print(f"\n{'=' * 78}\n结论: 24 项读取 {'完整 ✅' if len(A) == 24 else '不完整 ❌'}\n{'=' * 78}")
            return 0 if len(A) == 24 else 1

        nid = PARAM_IDS[args.param]
        orig = int(A[nid])
        new = orig + args.delta
        print(f"\n[2] 扰动写入  {args.param}: {orig} ({q2f(orig):.6f}) → "
              f"{new} ({q2f(new):.6f})")

        r = mc.params.set_raw(args.param, new, readback=True)
        print(f"    SETQ+回读: ok={r.ok} status={r.status}")
        if not r.ok:
            ok_all = False
        got = mc.params.get_raw(args.param)
        print(f"    独立 GET 复核 = {got}  "
              f"{'✅ 与写入一致 ⇒ SETQ 真的生效' if got == new else '❌ 不一致'}")
        if got != new:
            ok_all = False

        print(f"\n[3] 写回原值  {args.param} = {orig}")
        r2 = mc.params.set_raw(args.param, orig, readback=True)
        print(f"    ok={r2.ok} status={r2.status}")
        got2 = mc.params.get_raw(args.param)
        print(f"    独立 GET 复核 = {got2}  "
              f"{'✅ 已归位' if got2 == orig else '❌ 未归位'}")
        if not r2.ok or got2 != orig:
            ok_all = False

        print("\n[4] SAVE 写入 Flash")
        rs = mc.params.save(timeout=8.0)
        print(f"    SAVE → ok={rs.ok} status={rs.status}")
        if not rs.ok:
            ok_all = False

        print("\n[5] MCU RESET 后复验(证明 Flash 参数页重新加载正常)")
        res = mc.conn.soft_reset(verify=True, timeout=12.0)
        print(f"    MCU RESET → rst={(res.data or {}).get('rst')} "
              f"accepted={(res.data or {}).get('accepted')}")
        if (res.data or {}).get("rst") != "SW":
            ok_all = False
        time.sleep(1.0)
        B = get_all_raw(mc, "快照 B(复位后)")
        diff = [n for n, i in PARAM_IDS.items() if A.get(i) != B.get(i)]
        print(f"    A vs B 逐项比对: {'✅ 24/24 一致 ⇒ page 115 未破坏' if not diff else f'❌ 差异 {diff}'}")
        if diff:
            ok_all = False

        if args.strong:
            print(f"\n[6] 强证明: 写入『改动』并 SAVE, 复位后应能读到改动")
            mc.params.set_raw(args.param, new, readback=True)
            mc.params.save(timeout=8.0)
            mc.conn.soft_reset(verify=True, timeout=12.0)
            time.sleep(1.0)
            C = get_all_raw(mc, "快照 C(改动已 SAVE)")
            cok = C.get(nid) == new
            others = [n for n, i in PARAM_IDS.items() if i != nid and C.get(i) != A.get(i)]
            print(f"    {args.param} = {C.get(nid)} "
                  f"{'✅ 改动确实被 SAVE 持久化' if cok else '❌ 改动没持久化'}"
                  f"; 其余项差异 {others if others else '无 ✅'}")
            if not cok or others:
                ok_all = False

            print(f"\n[7] 还原并 SAVE, 现场归位")
            mc.params.set_raw(args.param, orig, readback=True)
            mc.params.save(timeout=8.0)
            mc.conn.soft_reset(verify=True, timeout=12.0)
            time.sleep(1.0)
            D = get_all_raw(mc, "快照 D(已还原)")
            dd = [n for n, i in PARAM_IDS.items() if A.get(i) != D.get(i)]
            print(f"    A vs D 逐项比对: {'✅ 与最初完全一致, 现场已归位' if not dd else f'❌ 差异 {dd}'}")
            if dd:
                ok_all = False

        print("\n" + "=" * 78)
        print("结论")
        print("=" * 78)
        print(f"    V2: SETQ 生效 / SAVE 落盘 / page 115 未破坏 —— "
              f"{'全部通过 ✅' if ok_all else '存在失败项 ❌'}")
        print(f"    最终参数快照:")
        F = get_all_raw(mc, "快照 F(最终)")
        show_table(mc, F)
    finally:
        try:
            mc.close()
        except Exception:
            pass
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
