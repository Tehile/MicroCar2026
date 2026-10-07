# -*- coding: utf-8 -*-
"""判据双向路径测试（第 7 轮新增/改动的判据）。

原则：只验 FAIL 路径 -> 写出「永远报错」的判据；只验 PASS 路径 -> 写出恒过判据。
本轮改动的判据：
  G23 把「版本号一致」换成「采集路径修订号一致」（含 fail-closed 的不可判定路径）
  G18 补上「④ 未记录输出尺寸」的证据边界
  G24 补上「未记录请求帧率」的证据边界 + 判别档结论（crop 高度排除）
  G14 补上「crop 宽 ≠ 阵列宽时该公式不可外推」的提示

⚠ 第 8 轮的两处适配（**测试的意图未变，只是被测对象改了**）：
  1. 本轮的 G18 期望 WARN，而第 8 轮 G18 改为**按交付路径门控**。
     这里显式传 `--delivery stream`，把被测对象固定在"连续流"语境下，
     本轮对 G18 的期望才继续成立（拍照语境下的行为由 round8 测试覆盖）。
  2. G23 的措辞由「采集路径修订号相同」升级为「采集路径 (id, rev) 相同」
     （第 8 轮起路径用 (id, rev) 成对表示），断言随之更新。

注：本文件是**历史测试**，其期望值反映的是当时的判据设计。
     改判据导致它失败时，应先判断"是判据错了"还是"测试的前提被推翻了"，
     不要把断言改成能过就算 —— 那等于把测试变成装饰。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "m0_out")
PY = sys.executable
SCRIPT = os.path.join(ROOT, "tools", "m0_evaluate.py")

BASE = "m0_locked_capture.json"
ROUNDS = ["m0_locked_capture_r1.json", "m0_locked_capture_r2.json", "m0_locked_capture_r3.json"]
SWEEP = "m0_size_sweep.json"
RATE = "m0_rate_sweep.json"
WATCH = ("G14", "G18", "G23", "G24")


def load(name):
    with open(os.path.join(SRC, name), encoding="utf-8") as f:
        return json.load(f)


def run(tag, mutate):
    """把 m0_out 复制到临时目录，施加 mutate，跑评估并抽出关心的判据行。"""
    d = tempfile.mkdtemp(prefix="m0t7_")
    try:
        for n in os.listdir(SRC):
            if n.endswith(".json"):
                shutil.copy(os.path.join(SRC, n), os.path.join(d, n))
        if mutate:
            mutate(d)
        out = subprocess.run([PY, SCRIPT, d, "--delivery", "stream"],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout
        want = {}
        for line in out.splitlines():
            for g in WATCH:
                if line.startswith(g + " "):
                    parts = line.split(None, 2)
                    want[g] = (parts[1] if len(parts) > 1 else "?",
                               (parts[2] if len(parts) > 2 else ""))
        total = [l for l in out.splitlines() if l.startswith("总判定")]
        print("\n=== %s ===" % tag)
        for g in WATCH:
            v = want.get(g)
            print("  %s: %s  %s" % (g, v[0] if v else "(未输出)",
                                    (v[1] if v else "")[:170]))
        if total:
            print("  " + total[0])
        return want
    finally:
        shutil.rmtree(d, ignore_errors=True)


def patch(d, name, fn):
    p = os.path.join(d, name)
    with open(p, encoding="utf-8") as f:
        j = json.load(f)
    fn(j)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False)


def has(w, g, needle):
    return needle in w.get(g, ("", ""))[1]


results = []


def check(tag, w, g, expect_verdict, needle=None, absent=None):
    v = w.get(g, ("(未输出)", ""))
    ok = (v[0] == expect_verdict)
    if needle is not None:
        ok = ok and has(w, g, needle)
    if absent is not None:
        ok = ok and not has(w, g, absent)
    results.append((tag, ok, "%s=%s" % (g, v[0])))


# ---------------- 基线 ----------------
# ⚠ 第 11 轮改：这条原先直接跑真实数据，理由是"真实 m0_out 里恰好版本不同"。
# 用户把全部科目用 v1.8.1 重跑之后，四份报告的版本**全相同** ⇒ 断言失效。
# 这是"断言依赖环境巧合"的第四次复发，所以照例改成**注入式构造前提**：
# 把 r1~r3 伪装成 1.5.0 期的报告（版本号 + **不自描述路径**），
# 主连拍保持当前版本 —— 这才是"版本不同、(id,rev) 相同"这条规则要处理的对象。
def old_style(d, names, ver):
    """把指定报告伪装成 pre-1.8.0 的产出：只留版本号，不给自己报路径。"""
    for n in names:
        def f(j):
            j["app_version"] = ver
            j.pop("capture_path_id", None)
            j.pop("capture_path_rev", None)
            cfg = j.get("capture_config") or {}
            cfg.pop("capture_path_id", None)
            cfg.pop("capture_path_rev", None)
        patch(d, n, f)


w = run("注入：1.5.0 期报告（不自描述路径）+ 当前主连拍",
        lambda d: old_style(d, ROUNDS, "m0probe-1.5.0"))
check("G23 版本不同但 rev 相同 → PASS", w, "G23", "PASS", "采集路径 (id, rev):")
# 第 10 轮新增：1.5.0 的报告不自描述路径，它们的 (id, rev) 全部由登记表给出 ⇒
# 它们之间"路径相同"是同一条**声明**的一致，不是测量的一致。判据必须说出来。
check("G23 明说哪几份的路径只是声明", w, "G23", "PASS", "声明的一致")
# G24 的判别档结论改到下面用合成扫描验（真实 ⑤ 本轮全灭，不再是有效夹具）。
# ⚠ 第 8 轮改：原先这里直接对基线断言"未记录输出尺寸/未记录请求帧率"，
# 依据的是**磁盘上那份报告恰好缺字段**。用户后来用新 APK 重跑 ④⑤ 之后，
# 字段补上了，断言随之失效 —— 但**判据并没有错**。
# 教训：测试不能依赖"环境恰好长这样"，想验哪条前提就**自己构造**它。
# 所以这两条改成下面的注入式测试（rate_without_size / sweep_without_reqfps）。

# ---------------- G23：六条路径 ----------------
def same_ver(d):
    for r in ROUNDS:
        patch(d, r, lambda j: j.update(app_version="m0probe-1.6.0"))


w = run("G23：全部同版本（连 rev 都不必查）", same_ver)
check("G23 同版本 → PASS", w, "G23", "PASS")


def unregistered(d):
    """版本不同，且新版本没登记 -> 不可判定（fail closed）。

    这是**必须**能走到的路径：登记表一旦缺项，绝不能静默放过。
    """
    # ⚠ 第 11 轮改：只改版本号不够 —— 当前 APK 的报告**自描述**路径，
    # `_resolve_capture_path` 优先用自带字段，于是"未登记"这条路径永远走不到
    # （假 PASS）。要验的恰恰是"既未登记、又不自描述"⇒ 必须把这个前提构造出来。
    old_style(d, ["m0_locked_capture_r3.json"], "m0probe-9.9.9")


w = run("G23：版本不同且未登记 → 不可判定", unregistered)
check("G23 未登记 → WARN 不可判定", w, "G23", "WARN", "不可判定")


def rev_conflict(d):
    """两条报告自报同一路径但 rev 分别为 1 与 2 -> 采集路径不同。

    ⚠ 第 8 轮改：自报字段由单个 `capture_path_rev` 升级为 **成对的
    (capture_path_id, capture_path_rev)**。原因：两条完全不同的路径各自的
    第 1 版 rev 都是 1，单看数字分不开。所以本用例改成自报成对字段；
    「只给 rev 不给 id」的行为由下面的 rev_only_no_id 单独覆盖。
    """
    patch(d, "m0_locked_capture_r2.json",
          lambda j: j.update(app_version="m0probe-9.9.9",
                             capture_path_id="burst_yuv_manual",
                             capture_path_rev=2))


w = run("G23：自报 (id, rev) 冲突（rev 1 vs 2）→ 跨采集路径", rev_conflict)
check("G23 rev 冲突 → WARN", w, "G23", "WARN", "跨采集路径")


def rev_only_no_id(d):
    """只自报 rev、不给 id -> **不可判定**（fail closed）。

    这是"成对比较"的代价，也是它必须承担的严格性：拿到一个孤零零的 rev=2，
    无法知道它是哪条路径的修订号（不同路径的修订号可以重号），
    因此**不能**拿它去和别人比。宁可判"判不了"，不可判"没问题"。
    """
    # ⚠ 第 11 轮改：当前报告自带**成对**字段，只改 rev 得到的仍是可解析的
    # (burst_yuv_manual, 2) ⇒ 走进"跨采集路径"而不是"不可判定"。
    # 要验的是"孤零零一个 rev"这个前提：必须先把 id 拿掉。
    def f(j):
        j["app_version"] = "m0probe-9.9.9"
        j.pop("capture_path_id", None)
        j["capture_path_rev"] = 2
        cfg = j.get("capture_config") or {}
        cfg.pop("capture_path_id", None)
    patch(d, "m0_locked_capture_r2.json", f)


w = run("G23：只自报 rev 不给 id → 不可判定", rev_only_no_id)
check("G23 只有 rev → WARN 不可判定", w, "G23", "WARN", "不可判定")


def self_reported_wins(d):
    """版本未登记，但报告自带成对字段 (burst_yuv_manual, 1) -> 用自带值，应当 PASS。

    与上一条对照：**同样是未登记的版本**，差别只在报告自带了 (id, rev)。
    两条一起跑才能证明"自带字段优先"这条规则真的生效，
    而不是碰巧都走进不可判定分支。
    """
    patch(d, "m0_locked_capture_r2.json",
          lambda j: j.update(app_version="m0probe-9.9.9",
                             capture_path_id="burst_yuv_manual",
                             capture_path_rev=1))


w = run("G23：版本未登记但自带 (id,rev) 相同 → PASS", self_reported_wins)
check("G23 自带成对字段优先 → PASS", w, "G23", "PASS")


def stale_batch(d):
    for r in ROUNDS:
        patch(d, r, lambda j: j.update(app_version="m0probe-1.6.0"))
    patch(d, "m0_locked_capture_r1.json",
          lambda j: j.update(generated_at_wall_ms=j["generated_at_wall_ms"] - 7200_000))


# 【第 10 轮改判据】这条原先是"跨批次 2 小时 → WARN"。
# 第 10 轮把"报告集时间跨度"从 WARN 降为注记，理由不是"太严"，而是它与它要防的
# 风险**没有因果关系**：要防的是"混入别的机器/固件的数据"，而"同一台机器隔几小时
# 重跑"跨度同样变大 —— 后者**正是 ③ 想要的条件**（G21 的主题就是跨会话可复现）。
# 于是这个代理既会漏（快速换机时跨度没超限）也会误（同机隔夜重跑）。
# 现在改判设备/固件标识（报告 1.8.1 起自带 device），跨度只作注记。
# 这条断言随之改为"跨度大 → 仍是 PASS，但必须说明为什么不判"。
w = run("G23：时间跨度 2 小时 → 不再判 WARN（第 10 轮改判据）", stale_batch)
check("G23 跨度大 → PASS（设备与路径均一致）", w, "G23", "PASS")
check("G23 跨度大 → 明说跨度不判为问题", w, "G23", "PASS", "不判为问题")
check("G23 跨度大 → 说明 ③ 本就要跨会话", w, "G23", "PASS", "跨会话")
check("G23 跨度大 → 保留残余风险", w, "G23", "PASS", "残余风险")


def cross_cfg(d):
    def f(j):
        j["capture_config"]["output_size"] = {"w": 1920, "h": 1080}
    patch(d, "m0_locked_capture_r2.json", f)


w = run("G23：跨配置（某轮 1080p）→ WARN", cross_cfg)
check("G23 跨配置 → WARN", w, "G23", "WARN", "跨配置")


def only_one(d):
    for r in ROUNDS:
        os.remove(os.path.join(d, r))


w = run("G23：只剩 1 份 → INFO", only_one)
check("G23 仅 1 份 → INFO", w, "G23", "INFO")


# ---------------- G18：④ 记了尺寸 ----------------
def rate_without_size(d):
    """注入"缺输出尺寸"这个前提，验证据边界提示真的会出现。"""
    def f(j):
        for t in j.get("trials") or []:
            t.pop("capture_size", None)
    patch(d, RATE, f)


w = run("G18：④ 未记录输出尺寸（注入）", rate_without_size)
check("G18 缺尺寸 → 提示", w, "G18", "WARN", "未记录输出尺寸")


def rate_with_size(d):
    def f(j):
        for t in j.get("trials") or []:
            t["capture_size"] = {"w": 1920, "h": 1440}
    patch(d, RATE, f)


w = run("G18：④ 记了输出尺寸", rate_with_size)
check("G18 有尺寸 → 报出尺寸", w, "G18", "WARN", "④ 覆盖的输出尺寸")
check("G18 有尺寸 → 不再提示缺尺寸", w, "G18", "WARN", absent="未记录输出尺寸")


# ---------------- G24：请求帧率与判别档（合成扫描夹具）----------------
# ⚠ 第 11 轮改：这四条原先在**真实 ⑤ 数据**上做增删字段的注入，前提是"磁盘上
# 那份扫描恰好有有效档位"。用户重跑后 ⑤ 五档全灭（摄像头被断开），
# 有效档位归零 ⇒ 四条断言一起失效（其中"反向"那条还会**真空通过**：
# 没数据当然不会宣布结论 —— 那验的是"没跑"，不是"判据克制"）。
# 照例改成自建前提：直接把 ⑤ 换成一份**自造的有效扫描**。
def sweep_trials(*, req_fps=30, disc_same_skew=True):
    """合成一份健康的 ⑤：三档 4:3 + 16:9 普通档 + 16:9 全crop 判别档。

    行时间全部对齐到 2999 ns/读出窗口行（16:9 档 skew=6.91 ms，与真机一致），
    这样"skew/读出窗口行数 是唯一不变量"这条结论也能被验证。
    判别档与 16:9 普通档**输出相同、crop 高度不同**：skew 相同 ⇒ 排除 crop 高度。
    """
    SK43 = 9212928                       # 3072 行 × 2999 ns
    SK169 = 6909696                      # 2304 行 × 2999 ns（= 3/4 的读出窗口）
    def t(w, h, cw, ch, *, forced=False, skew=SK43, fps=30.0367):
        d = {"label": "合成档", "target_width": w, "forced_full_crop": forced,
             "requested_size": {"w": w, "h": h},
             "actual_size": {"w": w, "h": h},
             "actual_interval_ns": 1e9 / fps,
             "rolling_shutter_skew_ns": float(skew),
             "crop_region": {"left": 0, "top": 0, "width": cw, "height": ch},
             "crop_shrunk_for_aspect": False, "valid_frames": 40}
        if req_fps is not None:
            d["target_fps"] = req_fps
        return d
    trials = [t(1920, 1440, 4096, 3072), t(2880, 2160, 4096, 3072),
              t(4096, 3072, 4096, 3072), t(3840, 2160, 4096, 2304, skew=SK169)]
    trials.append(t(3840, 2160, 4096, 3072, forced=True,
                    skew=SK169 if disc_same_skew else SK43))
    return trials


def write_sweep(d, trials):
    patch(d, SWEEP, lambda j: j.update(trials=trials))


def sweep_without_reqfps(d):
    """注入"缺请求帧率"这个前提。不注入就验不了 —— 新 APK 会记它。"""
    write_sweep(d, sweep_trials(req_fps=None))


w = run("G24：⑤ 未记录请求帧率（注入）", sweep_without_reqfps)
check("G24 缺请求帧率 → 提示", w, "G24", "INFO", "未记录请求帧率")


def sweep_with_reqfps(d):
    write_sweep(d, sweep_trials(req_fps=30))


w = run("G24：⑤ 记了请求帧率", sweep_with_reqfps)
check("G24 有请求帧率 → 打印请求→实测", w, "G24", "INFO", "请求30→实测")
check("G24 有请求帧率 → 不再提示缺帧率", w, "G24", "INFO", absent="未记录请求帧率")


w = run("G24 正向：判别档同输出、crop 高度不同、skew 相同 → 宣布结论", sweep_with_reqfps)
check("G24 判别档结论（crop 高度无关）", w, "G24", "INFO", "crop 高度与 skew 无关")
check("G24 判别档 → 同时给出唯一不变量", w, "G24", "INFO", "唯一不变量")


def judge_trial_skew_differs(d):
    """反向路径：同输出、crop 高度不同，但 skew **不同**。

    此时判别档**没有**排除 crop 高度（crop 3072 行 → skew 9212928），
    判据就不该打印"crop 高度与 skew 无关"这条结论。
    只验正向那条会写出「永远宣布结论」的判据。
    """
    write_sweep(d, sweep_trials(req_fps=30, disc_same_skew=False))


w = run("G24 反向：判别档 skew 按 crop 高度变 → 不该宣布结论",
        judge_trial_skew_differs)
check("G24 反向 → 无判别档结论", w, "G24", "INFO", absent="crop 高度与 skew 无关")


# ---------------- G14：crop 宽 ≠ 阵列宽 ----------------
def crop_narrower_than_array(d):
    """把 crop 收窄到 2048 宽 -> 读出宽度到底是 crop 宽还是阵列宽就分岔了，
    此时该公式**不可外推**，判据必须提示（而不是照旧给出 2999 ns/行）。"""
    def f(j):
        j["capture_config"]["crop_region"]["width"] = 2048
    patch(d, BASE, f)


w = run("G14：crop 宽 2048 ≠ 阵列宽 4096", crop_narrower_than_array)
check("G14 crop≠阵列 → 提示不可外推", w, "G14", "PASS", "不可外推")


# ---------------- 汇总 ----------------
print("\n" + "=" * 74)
bad = 0
for tag, ok, note in results:
    print("%-46s %s  %s" % (tag, "OK  " if ok else "FAIL", note))
    if not ok:
        bad += 1
print("=" * 74)
print("通过 %d / %d" % (len(results) - bad, len(results)))
sys.exit(1 if bad else 0)
