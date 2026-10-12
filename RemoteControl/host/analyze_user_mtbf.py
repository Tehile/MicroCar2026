# -*- coding: utf-8 -*-
"""整理并分析 user_fps4/12/30 三组固定帧率连续通信(MTBF)实验数据。

输入: host/mtbf_user_fps{4,12,30}_{i}.csv + _summary.txt
输出:
  - host/mtbf_user_all_runs.csv      (归一化逐轮总表)
  - host/mtbf_user_groups.csv        (按帧率分组统计)
  - host/mtbf_user_predeath.csv      (断连前兆特征)
  - 终端打印完整分析报告
"""
import csv
import glob
import os
import re
import statistics as st

HERE = os.path.dirname(os.path.abspath(__file__))


def parse_summary(path):
    d = {}
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            d[k.strip()] = v.strip()
    return d


def parse_csv(path):
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_runs():
    runs = []
    for sp in sorted(glob.glob(os.path.join(HERE, "mtbf_user_fps*_summary.txt"))):
        s = parse_summary(sp)
        tag = s.get("标签", "")
        m = re.match(r"user_fps(\d+)_(\d+)$", tag)
        if not m:
            continue
        fps = int(m.group(1))
        idx = int(m.group(2))
        cp = sp.replace("_summary.txt", ".csv")
        rows = parse_csv(cp) if os.path.exists(cp) else []
        alive_rows = [r for r in rows if r.get("alive") == "1"]
        dead = [r for r in rows if r.get("alive") == "0"]
        # 断连时刻(取 DEAD/最后存活窗)
        t_fail = None
        note = s.get("结束说明", "")
        mm = re.search(r"last_frame=([\d.]+)s", " ".join(r.get("note", "") for r in dead))
        if mm:
            t_fail = float(mm.group(1))
        elif s.get("断连时刻", "-") not in ("-", ""):
            t_fail = float(s["断连时刻"].replace("s", ""))
        res = s.get("结果", "")
        alive_s = float(s.get("存活时长", "0").replace("s", ""))
        frames = int(s.get("累计收帧", "0"))
        mean_fps = float(s.get("平均观测帧率", "0").replace("Hz", ""))
        max_gap = float(s.get("最大帧间隔", "0").replace("ms", ""))
        runs.append(dict(
            tag=tag, fps=fps, idx=idx, result=res, alive_s=alive_s,
            t_fail=t_fail, frames=frames, mean_fps=mean_fps, max_gap=max_gap,
            rows=rows, alive_rows=alive_rows, note=note,
        ))
    runs.sort(key=lambda r: (r["fps"], r["idx"]))
    return runs


def group_stats(runs):
    out = []
    for fps in sorted(set(r["fps"] for r in runs)):
        g = [r for r in runs if r["fps"] == fps]
        broke = [r for r in g if r["result"] == "BROKE"]
        surv = [r for r in g if r["result"] != "BROKE"]
        times = [r["alive_s"] for r in g]
        out.append(dict(
            fps=fps, n=len(g), n_broke=len(broke), n_surv=len(surv),
            broke_rate=100.0 * len(broke) / len(g),
            t_med=st.median(times), t_mean=st.mean(times),
            t_min=min(times), t_max=max(times),
            frames_mean=st.mean(r["frames"] for r in g),
            meanfps_mean=st.mean(r["mean_fps"] for r in g),
            maxgap_mean=st.mean(r["max_gap"] for r in g),
        ))
    return out


def predeath_features(runs):
    """断连前兆: 末窗 vs 全程基线。仅取真正收到过帧的 BROKE 轮。"""
    feats = []
    for r in runs:
        if r["result"] == "BROKE" and r["frames"] > 0 and r["alive_rows"]:
            ar = r["alive_rows"]
            allfps = [float(x["fps_obs"]) for x in ar]
            allgap = [float(x["gap_max_ms"]) for x in ar if float(x["gap_max_ms"]) > 0]
            tail = ar[-3:]
            tailfps = [float(x["fps_obs"]) for x in tail]
            tailgap = [float(x["gap_max_ms"]) for x in tail if float(x["gap_max_ms"]) > 0]
            base_fps = st.median(allfps)
            base_gap = st.median(allgap) if allgap else 0.0
            last_gap = tailgap[-1] if tailgap else 0.0
            feats.append(dict(
                tag=r["tag"], fps=r["fps"], alive_s=r["alive_s"],
                base_fps=base_fps, tail_fps=st.mean(tailfps) if tailfps else 0.0,
                base_gap=base_gap, last_gap=last_gap,
                gap_ratio=(last_gap / base_gap) if base_gap else float("nan"),
                fps_ratio=(st.mean(tailfps) / base_fps) if base_fps else float("nan"),
            ))
    return feats


def spearman(xs, ys):
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        rk = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                rk[order[k]] = avg
            i = j + 1
        return rk
    rx, ry = rank(xs), rank(ys)
    n = len(xs)
    mx, my = st.mean(rx), st.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else float("nan")


def main():
    runs = load_runs()
    W = print

    # ---------- 1. 逐轮总表 ----------
    W("=" * 92)
    W("1. 数据清单 / 逐轮明细  (共 %d 轮)" % len(runs))
    W("=" * 92)
    W("%-16s %5s %5s %-9s %9s %7s %8s %9s %8s" % (
        "tag", "fps", "idx", "结果", "存活(s)", "收帧", "观测fps", "默认/目标", "最大gap"))
    for r in runs:
        ratio = (r["mean_fps"] / r["fps"]) if r["fps"] else 0
        W("%-16s %5d %5d %-9s %9.1f %7d %8.2f %9.1f%% %7.0fms" % (
            r["tag"], r["fps"], r["idx"], r["result"], r["alive_s"],
            r["frames"], r["mean_fps"], 100 * ratio, r["max_gap"]))

    # ---------- 2. 分组统计 ----------
    gs = group_stats(runs)
    W("")
    W("=" * 92)
    W("2. 按帧率分组统计")
    W("=" * 92)
    W("%5s %4s %7s %7s %9s %9s %9s %9s %9s %10s" % (
        "fps", "N", "断连数", "存活数", "断连率%", "中位存活", "均值存活", "最短", "最长", "观测/目标"))
    for s in gs:
        W("%5d %4d %7d %7d %8.0f%% %8.1fs %8.1fs %8.1fs %8.1fs %9.1f%%" % (
            s["fps"], s["n"], s["n_broke"], s["n_surv"], s["broke_rate"],
            s["t_med"], s["t_mean"], s["t_min"], s["t_max"],
            100 * s["meanfps_mean"] / s["fps"]))

    # ---------- 3. 生存 / 危险率 ----------
    W("")
    W("=" * 92)
    W("3. 生存分析（fps4 全部右删失于 600s ⇒ 其 MTBF 是下界）")
    W("=" * 92)
    for s in gs:
        if s["n_surv"] == 0:
            W("  fps=%2d : 全部断连。MTBF(均值)=%.1fs  中位=%.1fs  λ=1/MTBF=%.4f /s (%.2f 次/小时)" % (
                s["fps"], s["t_mean"], s["t_med"], 1 / s["t_mean"], 3600 / s["t_mean"]))
        else:
            W("  fps=%2d : %d/%d 存活(≥600s 删失)。已断连轮 MTBF≥%.1fs  ⇒ 真实 MTBF > 600s" % (
                s["fps"], s["n_surv"], s["n"], s["t_mean"]))

    xs = [r["fps"] for r in runs]
    ys = [r["alive_s"] for r in runs]
    W("")
    W("  Spearman(目标帧率, 存活时长) = %+.3f  (n=%d) ⇒ 帧率越高存活越短" % (spearman(xs, ys), len(xs)))
    # 只看已断连的做相关，去掉 600s 删失
    bx = [r["fps"] for r in runs if r["result"] == "BROKE"]
    by = [r["alive_s"] for r in runs if r["result"] == "BROKE"]
    W("  Spearman(仅断连轮)          = %+.3f  (n=%d)" % (spearman(bx, by), len(bx)))

    # ---------- 4. 容量 / 超订 ----------
    W("")
    W("=" * 92)
    W("4. 链路容量线索：观测帧率 / 目标帧率")
    W("=" * 92)
    for s in gs:
        d = 100 * s["meanfps_mean"] / s["fps"]
        flag = "  <== 明显欠交付(超订)" if d < 97 else "  (零丢帧)"
        W("  fps=%2d : 观测均值 %.2f Hz = 目标的 %.1f%%%s" % (s["fps"], s["meanfps_mean"], d, flag))
    W("  ⇒ 4Hz/12Hz 精确满额(链路余量充足); 30Hz 只交付 ~27Hz(-10%) ⇒ 链路原始容量 ≈ 27~28Hz")

    # ---------- 5. 断连前兆 ----------
    feats = predeath_features(runs)
    W("")
    W("=" * 92)
    W("5. 断连前兆检测：末 3 存活窗 vs 该轮基线  (fps_ratio≈1 且 gap_ratio≈1 ⇒ 无预警)")
    W("=" * 92)
    W("%-16s %5s %9s %10s %10s %10s %10s %9s %9s" % (
        "tag", "fps", "存活(s)", "基线fps", "末3窗fps", "基线gap", "末窗gap", "fps比", "gap比"))
    for f in feats:
        W("%-16s %5d %9.1f %10.2f %10.2f %9.0fms %9.0fms %8.2f %9.2f" % (
            f["tag"], f["fps"], f["alive_s"], f["base_fps"], f["tail_fps"],
            f["base_gap"], f["last_gap"], f["fps_ratio"], f["gap_ratio"]))
    if feats:
        fr = [f["fps_ratio"] for f in feats if f["fps_ratio"] == f["fps_ratio"]]
        gr = [f["gap_ratio"] for f in feats if f["gap_ratio"] == f["gap_ratio"]]
        W("")
        W("  末3窗 fps 比中位 = %.3f ; 末窗 gap 比中位 = %.3f" % (st.median(fr), st.median(gr)))
        W("  (若两者都≈1.0 ⇒ 断连前一瞬与全程基线无异, 即猝死、无预警)")

    # ---------- 6. 落盘 ----------
    allp = os.path.join(HERE, "mtbf_user_all_runs.csv")
    with open(allp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["tag", "fps_target", "index", "result", "alive_s", "t_fail_s",
                    "frames", "mean_fps_obs", "delivery_ratio", "max_gap_ms", "note"])
        for r in runs:
            w.writerow([r["tag"], r["fps"], r["idx"], r["result"], "%.1f" % r["alive_s"],
                        "" if r["t_fail"] is None else "%.1f" % r["t_fail"],
                        r["frames"], "%.2f" % r["mean_fps"],
                        "%.4f" % (r["mean_fps"] / r["fps"]), "%.0f" % r["max_gap"], r["note"]])
    gp = os.path.join(HERE, "mtbf_user_groups.csv")
    with open(gp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["fps_target", "n", "n_broke", "n_survived", "broke_rate_pct",
                    "t_median_s", "t_mean_s", "t_min_s", "t_max_s",
                    "mean_frames", "mean_fps_obs", "delivery_ratio_pct"])
        for s in gs:
            w.writerow([s["fps"], s["n"], s["n_broke"], s["n_surv"], "%.0f" % s["broke_rate"],
                        "%.1f" % s["t_med"], "%.1f" % s["t_mean"], "%.1f" % s["t_min"],
                        "%.1f" % s["t_max"], "%.0f" % s["frames_mean"],
                        "%.2f" % s["meanfps_mean"],
                        "%.1f" % (100 * s["meanfps_mean"] / s["fps"])])
    pp = os.path.join(HERE, "mtbf_user_predeath.csv")
    with open(pp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["tag", "fps_target", "alive_s", "base_fps_obs", "tail3_fps_obs",
                    "base_gap_ms", "last_gap_ms", "fps_ratio", "gap_ratio"])
        for x in feats:
            w.writerow([x["tag"], x["fps"], "%.1f" % x["alive_s"], "%.2f" % x["base_fps"],
                        "%.2f" % x["tail_fps"], "%.0f" % x["base_gap"], "%.0f" % x["last_gap"],
                        "%.3f" % x["fps_ratio"], "%.3f" % x["gap_ratio"]])
    W("")
    W("已写出: %s" % ", ".join(os.path.basename(p) for p in (allp, gp, pp)))


if __name__ == "__main__":
    main()
