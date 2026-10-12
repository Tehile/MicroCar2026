"""把 link_pull_pace.py 的逐帧 CSV 拆成"批内间隔 / 跨批间隔"两张分布表。

背景(为什么要按批拆):
  PULL 模式下, 同一批内的帧间隔由**单片机自己**倒计时(t), 是精确的;
  跨批的帧间隔由**上位机下一次命令何时到达**决定, 才是抖动的来源。
  两者混在一张直方图里会互相掩盖 —— 之前 83/67/142 的三峰就是这么被看到的,
  却说不清哪个峰属于哪一类。

用法:
    python analyze_pace_gaps.py pace_<tag>_frames.csv [--target 83.3]

判读要点:
  * 批内间隔应几乎全部等于 t(±2ms)。出现别的值 => 固件批内节拍有问题。
  * 跨批间隔呈双峰、两峰相差一个常数 Q => **下行命令路径存在 Q ms 量化**。
    这才是"数据不均匀"的根因;它是命令通道的性质, 与上位机怎么算周期无关。
"""
from __future__ import annotations

import argparse
import collections
import csv
import statistics
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("--target", type=float, default=83.3,
                    help="名义帧间隔 ms(=1000/fps)")
    ap.add_argument("--by-batch", action="store_true",
                    help="按 CSV 的 batch 列分组(默认按值分组, 见下)")
    ap.add_argument("--tol", type=float, default=3.0,
                    help="按值分组时 批内间隔 的容差 ms")
    a = ap.parse_args()

    rows = []
    with open(a.csv_path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("gap_mcu_ms"):
                rows.append((int(r["batch"]), float(r["gap_mcu_ms"])))
    if not rows:
        print("没有可用的 gap_mcu_ms 数据")
        return 1

    # ⚠️ 默认**不**用 batch 列: 钩子在**读线程**里读 cur_batch, 而主循环可能已经推进到
    #    下一批 ⇒ 迟到的帧会被贴上下一批的号(实测 n=1 时出现重复批号)。按值分组没有这个
    #    竞态: 批内间隔由固件倒计时决定, 恒等于 t; 凡不落在 t±tol 的必是跨批间隔。
    inb, cross = [], []
    if a.by_batch:
        prev_b = None
        for b, g in rows:
            if prev_b is None:
                prev_b = b
                continue
            (inb if b == prev_b else cross).append(g)
            prev_b = b
    else:
        for _, g in rows:
            (inb if abs(g - a.target) <= a.tol else cross).append(g)

    def report(name: str, arr: list[float]) -> None:
        if not arr:
            print(f"{name}: 空")
            return
        s = sorted(arr)
        print(f"{name}: n={len(s)} 均值={statistics.fmean(s):.1f} "
              f"中位={s[len(s) // 2]:.0f} σ={statistics.pstdev(s):.1f} "
              f"min={s[0]:.0f} max={s[-1]:.0f}")
        c = collections.Counter(round(v) for v in s)
        top = sorted(c.items(), key=lambda kv: -kv[1])[:10]
        tot = len(s)
        print("   直方图 " + "  ".join(f"{k}:{v}({100.0 * v / tot:.1f}%)" for k, v in top))

    print(f"文件 {a.csv_path}   名义帧间隔 {a.target:.1f} ms")
    report("批内间隔", inb)
    report("跨批间隔", cross)

    # 跨批双峰判定: 取众数两侧的簇, 看间隔差是否是个稳定常数
    if len(cross) >= 20:
        c = collections.Counter(round(v) for v in cross)
        modes = [k for k, _ in c.most_common(4)]
        if len(modes) >= 2:
            modes.sort()
            print(f"   跨批众数簇: {modes}  相邻差: "
                  f"{[modes[i + 1] - modes[i] for i in range(len(modes) - 1)]}")
            print("   ↑ 若相邻差为稳定常数, 即为下行命令路径的量化步长(ms)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
