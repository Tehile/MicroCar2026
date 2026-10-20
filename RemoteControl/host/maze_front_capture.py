"""发车 + 运动期前墙双档采样（v2.3.0 真机验收）。

目的：在**持续拉取 + 持续运动**的真实工况下，验收 v2.3.0 的三件事：

  1. 恒等式 `(frontNear | frontFar) == wallFront` 且互斥 —— 逐帧核对；
  2. 冲刺/前墙新字节在运动中是否有越界值（偏移错位会读到邻居字节）；
  3. 真实工况下链路是否稳定（坏 CRC / 丢帧 / 帧率），以及
     `frontFar → frontNear` 的过渡是否真的出现（这是双档拆分的意义所在）。

安全护栏（三重，全部默认开启）：
  · `--max-sec`   硬性时长上限，到点自动收车（默认 40 s）；
  · 低电即停      `battMv` **连续 N 帧**（`--lowbatt-streak`，默认 3）低于
                  `--lowbatt-mv`（默认 6500）立即收车。阈值刻意**低于**发车闸
                  `LAUNCH_MIN_MV`(7000)：实测空载 7.66 V / 满载 **6.83 V**，
                  电机负载压降达 830 mV，直接用发车闸当运动中阈值会正常跑也误停。
                  这是"发车前闸"拦不住的场景 —— 车跑起来之后才掉电；
  · Ctrl+C        立即收车。
  收车走 `MCU RESET`（先停车再复位，v2.2.1 起固件已保证），这是真机验证过唯一
  可靠的收车方式（`MAZE STOP` 在运动中依赖协作式检查点，不可靠）。
  成功判据：之后 `PING` 报 `rst=SW`。

用法:
    python maze_front_capture.py COM7                     # 发车 → 采 40s → 收车
    python maze_front_capture.py COM7 --max-sec 90
    python maze_front_capture.py COM7 --dry-run           # 只自检不发车
    python maze_front_capture.py COM7 --out run.csv
    python maze_front_capture.py COM7 --no-rescue         # 结束后不收车(留现场)
"""
from __future__ import annotations

import argparse
import csv
import sys
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else ".")

from host.api import MouseClient  # noqa: E402
from host.registry import LAUNCH_MIN_MV  # noqa: E402

#: 每帧记录这些字段
FIELDS = [
    "t", "ms8", "battMv", "mazeTask", "motionTask",
    "speedL", "speedR", "irF1", "irF2",
    "wallFront", "frontNear", "frontFar", "frontBoth",
    "spurtMode", "spurtFlag", "goaheadnum",
    "posX", "posY", "roadName",
]


def main() -> int:
    ap = argparse.ArgumentParser(description="发车 + 运动期前墙双档采样")
    ap.add_argument("target", nargs="?", default="COM7")
    ap.add_argument("--max-sec", type=float, default=40.0, help="最长采样/运行时间(秒)")
    ap.add_argument("--lowbatt-mv", type=float, default=6500.0,
                    help="运动中低电收车阈值(mV, 默认 6500)。**比发车闸低** —— "
                         "电机负载会造成明显瞬时压降(实测空载 7.66V / 满载 6.83V, "
                         "压降 830mV), 用发车闸当运动中阈值会正常跑也误停")
    ap.add_argument("--lowbatt-streak", type=int, default=3,
                    help="连续多少帧低于阈值才收车(默认 3), 滤掉单个瞬时压降")
    ap.add_argument("--out", help="把采样导出为 CSV")
    ap.add_argument("--dry-run", action="store_true", help="只做发车前自检, 不发车")
    ap.add_argument("--no-rescue", action="store_true", help="结束后不自动收车")
    args = ap.parse_args()

    mc = MouseClient()
    if not mc.connect(args.target):
        print(f"连接失败: {mc.state_detail}")
        return 1

    rescued = False

    def rescue(reason: str) -> None:
        nonlocal rescued
        if rescued or args.no_rescue:
            return
        rescued = True
        print(f"\n→ 收车（{reason}）…")
        try:
            r = mc.conn.rescue()
            print(f"   MCU RESET -> {r.status}")
        except Exception as exc:  # noqa: BLE001
            print(f"   ⚠️ 收车异常: {exc}  —— 必要时手动断电")

    try:
        # ---------------- 发车前自检 ----------------
        print(f"连接 {args.target}  ok")
        if not mc.telemetry.wait_first_frame(5.0):
            print("✗ 5s 内没收到统一帧，链路可能没起来")
            return 1
        f = mc.telemetry.latest()
        # `connect()` 只 sleep 50ms 就返回，而蓝牙单程 ≈1.1s —— PONG 还没到。
        # 等设备自述信息真正填上（最多 3s），否则 proto/fw 会读成 None。
        for _ in range(60):
            if mc.device.get("proto"):
                break
            time.sleep(0.05)
        dev = mc.device
        print(f"  固件 proto={dev.get('proto')} fw={dev.get('fw')} "
              f"maze={dev.get('mazetype')} 复位={dev.get('rst')}")
        print(f"  电压 {f['battMv']} mV（发车闸 {LAUNCH_MIN_MV}）  "
              f"任务 {f['mazeTaskName']}  动作 {f['motionTaskName']}  "
              f"速度 {f['speedL']}/{f['speedR']}  "
              f"墙 左{f['wallLeft']} 前{f['wallFront']} 右{f['wallRight']}")
        if dev.get("proto") != 4 or dev.get("fw") != 215:
            print(f"  ⚠️ 期望 PROTO=4 / FW=215（v2.3.0），实际 "
                  f"{dev.get('proto')}/{dev.get('fw')} —— 新字段可能无效")
        if f["battMv"] and f["battMv"] < LAUNCH_MIN_MV:
            print(f"✗ 电压低于发车闸（{f['battMv']} < {LAUNCH_MIN_MV}），拒绝发车")
            return 1
        if args.dry_run:
            print("--dry-run：自检通过，未发车。")
            return 0

        # ---------------- 发车 ----------------
        print("\n发车 MAZE RUN …")
        r = mc.maze.run(timeout=4.0)
        print(f"  -> {r.status}")
        if not r.ok:
            print("✗ 发车被拒。若是 ERR_LOWBATT，换电池后再来；"
                  "若是 ERR_BUSY，先收车复位。")
            return 1

        # ---------------- 采样 ----------------
        rows: list[dict] = []
        t0 = time.monotonic()
        last_key = None
        low_batt_hit = False
        low_streak = 0
        print(f"\n采样 {args.max_sec:.0f}s（Ctrl+C 可立即收车）…")
        print("   时刻      任务/动作      格内里程  前墙(f/n/far)  F1/F2mm   速度L/R  电压")
        try:
            while time.monotonic() - t0 < args.max_sec:
                f = mc.telemetry.latest()
                if f is None:
                    time.sleep(0.05)
                    continue
                # 去重: 同一帧会被多次读到
                key = (f.get("ms8"), f.get("cellOdom"), f.get("motionTask"),
                       f.get("speedL"), f.get("speedR"), f.get("irF1"), f.get("irF2"))
                if key == last_key:
                    time.sleep(0.05)
                    continue
                last_key = key

                row = {k: f.get(k) for k in FIELDS}
                row["t"] = round(time.monotonic() - t0, 3)
                rows.append(row)

                ok = ((f["frontNear"] | f["frontFar"]) == f["wallFront"]
                      and (f["frontNear"] & f["frontFar"]) == 0)
                print(f"  {row['t']:6.2f}s  {f['mazeTaskName']:>8}/{f['motionTaskName']:<16} "
                      f"{f['cellOdom']:6d}  f={f['wallFront']}n={f['frontNear']}"
                      f"far={f['frontFar']}  {f['irF1']:4d}/{f['irF2']:4d}  "
                      f"{f['speedL']:4d}/{f['speedR']:4d}  {f['battMv']}mV"
                      f"{'' if ok else '   ⚠️恒等式失败'}")

                if f["battMv"] and f["battMv"] < args.lowbatt_mv:
                    low_streak += 1
                    if low_streak >= args.lowbatt_streak:
                        low_batt_hit = True
                        print(f"  ⚠️ 连续 {low_streak} 帧低于 {args.lowbatt_mv:.0f} mV"
                              f"（当前 {f['battMv']}）—— 立即收车")
                        break
                else:
                    low_streak = 0
                time.sleep(0.05)
        except KeyboardInterrupt:
            print("\n（收到 Ctrl+C）")
            rescue("Ctrl+C")

        # ---------------- 汇总 ----------------
        print(f"\n采样结束：{len(rows)} 个新帧")
        if rows:
            bad = [r for r in rows
                   if ((r["frontNear"] | r["frontFar"]) != r["wallFront"]
                       or (r["frontNear"] & r["frontFar"]) != 0)]
            print(f"  恒等式 (near|far)==front 且互斥: "
                  f"{'✅ 全部成立' if not bad else f'❌ 失败 {len(bad)}/{len(rows)} 帧'}")
            n_near = sum(1 for r in rows if r["frontNear"])
            n_far = sum(1 for r in rows if r["frontFar"])
            n_none = sum(1 for r in rows if not r["wallFront"])
            n_both = sum(1 for r in rows if r["frontBoth"])
            print(f"  前墙分布: 无墙={n_none}  近端={n_near}  远端={n_far}  双路互证={n_both}")
            if n_near and n_far:
                print("  ✅ 两档在运动中**都出现过** —— 双档拆分确实在区分'贴脸'与'远墙'")
            elif n_far and not n_near:
                print("  → 只见远端：车没走到贴墙的程度（或未跑到有墙的路段）")
            elif n_near and not n_far:
                print("  → 只见近端：远墙从未进入 80~230mm 窗口（走得少或有近墙直接跳近端）")
            else:
                print("  → 前墙一直无墙：车可能没动，或整段都在空旷区")
            tasks = sorted({r["mazeTask"] for r in rows})
            print(f"  出现过的迷宫任务: {tasks}")
            sp_max = max(r["spurtMode"] or 0 for r in rows)
            gah = sorted({r["goaheadnum"] for r in rows})
            print(f"  冲刺档最大值 {sp_max}；goaheadnum 取值 {gah}")
            print(f"  电压 min/max: {min(r['battMv'] for r in rows)}/"
                  f"{max(r['battMv'] for r in rows)} mV")
            if args.out:
                with open(args.out, "w", newline="", encoding="utf-8") as fp:
                    w = csv.DictWriter(fp, fieldnames=FIELDS)
                    w.writeheader()
                    w.writerows(rows)
                print(f"  CSV -> {args.out}")

        st = mc.telemetry.stats()
        print(f"  链路统计: {st}")

        if low_batt_hit:
            rescue("低电保护")
        elif not args.no_rescue:
            rescue("采样到点")
        return 0
    finally:
        mc.close()


if __name__ == "__main__":
    raise SystemExit(main())
