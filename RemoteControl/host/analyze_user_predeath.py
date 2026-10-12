# -*- coding: utf-8 -*-
"""断连前兆的精确刻画：
1) 定位最后一次"正常收帧"窗(t_stop)，检查其前 15 窗是否退化(趋势)；
2) 检查全程是否出现"突发补帧"(单窗帧数 > 1.4×标称) —— 积压集中排泄的证据；
3) 打印若干典型轮的断前 12 窗逐秒序列。
"""
import csv
import glob
import os
import re
import statistics as st

HERE = os.path.dirname(os.path.abspath(__file__))


def runs():
    for sp in sorted(glob.glob(os.path.join(HERE, "mtbf_user_fps*_summary.txt"))):
        m = re.search(r"user_fps(\d+)_(\d+)$", sp.replace("_summary.txt", ""))
        if not m:
            continue
        fps = int(m.group(1))
        cp = sp.replace("_summary.txt", ".csv")
        rows = list(csv.DictReader(open(cp, encoding="utf-8-sig")))
        yield fps, int(m.group(2)), sp.replace("_summary.txt", ""), rows


def slope(xs, ys):
    if len(xs) < 3:
        return float("nan")
    mx, my = st.mean(xs), st.mean(ys)
    den = sum((a - mx) ** 2 for a in xs)
    return sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / den if den else float("nan")


print("=" * 100)
print("A. 断连前 15 个'有效'窗的趋势 + 全程突发补帧检测")
print("=" * 100)
print("%-15s %7s %8s %8s %9s %9s %10s %9s %9s" % (
    "tag", "存活s", "t_stop", "末段fps", "段均fps", "fps斜率", "gap斜率ms/s", "突发窗数", "突发/标称"))

all_burst = []
for fps, idx, path, rows in runs():
    nominal = fps
    alive = [r for r in rows if r["alive"] == "1"]
    if not alive or all(int(r["frames"]) == 0 for r in alive):
        print("%-15s   —— 全程 0 帧(疑似上轮死后未复位), 跳过" % os.path.basename(path))
        continue
    fr = [int(r["frames"]) for r in alive]
    # 最后一次"正常收帧"窗: frames >= 0.5*nominal
    tstop = max(i for i, x in enumerate(fr) if x >= 0.5 * nominal)
    lo = max(0, tstop - 14)
    seg = alive[lo:tstop + 1]
    xs = list(range(len(seg)))
    yf = [float(r["fps_obs"]) for r in seg]
    yg = [float(r["gap_max_ms"]) for r in seg if float(r["gap_max_ms"]) > 0]
    xg = [i for i, r in enumerate(seg) if float(r["gap_max_ms"]) > 0]
    bursts = [i for i, x in enumerate(fr) if x > 1.4 * nominal]
    all_burst.append((fps, idx, len(bursts), max(fr)))
    print("%-15s %7.1f %8.1f %8.2f %9.2f %9.3f %10.3f %9d %9s" % (
        os.path.basename(path), float(alive[-1]["t_s"]), float(seg[-1]["t_s"]),
        st.mean(yf[-3:]), st.mean(yf), slope(xs, yf), slope(xg, yg),
        len(bursts), ("%d" % max((x for x in fr), default=0))))

print()
print("=" * 100)
print("B. 全程突发补帧统计（单窗帧数 > 1.4×标称 的窗数）")
print("=" * 100)
for fps in (4, 12, 30):
    vals = [n for (f, i, n, mx) in all_burst if f == fps]
    if vals:
        print("  fps=%2d : 各轮突发窗数 = %s   合计 %d" % (fps, vals, sum(vals)))
print("  ⇒ 若恒为 0 ⇒ 从未出现'攒批后集中排泄' ⇒ 无积压排空现象")

print()
print("=" * 100)
print("C. 典型轮断前 14 窗逐秒序列 (t_s, frames, fps_obs, gap_max_ms)")
print("=" * 100)
for want, fname in [(12, "mtbf_user_fps12_1.csv"), (12, "mtbf_user_fps12_4.csv"),
                    (30, "mtbf_user_fps30_2.csv"), (30, "mtbf_user_fps30_8.csv"),
                    (4, "mtbf_user_fps4_1.csv")]:
    path = os.path.join(HERE, fname)
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    alive = [r for r in rows if r["alive"] == "1"]
    tail = alive[-14:]
    s = " | ".join("%s:%s,f%s" % (r["t_s"].split(".")[0], r["frames"], r["gap_max_ms"].split(".")[0])
                   for r in tail)
    print("  %-22s %s" % (fname, s))
print()
print("  (格式 t:frames,gapms)  fps4 全程无异常; 12/30 皆'正常→骤停', 无渐进劣化")
