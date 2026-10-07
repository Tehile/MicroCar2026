# -*- coding: utf-8 -*-
"""判据双向路径测试（第 10 轮：G23 的设备身份分组 + 批次规则的降级）。

背景（这一轮为什么动 G23）：
  用户重跑 ② 之后，② 与 6h53m 前的 ③ 三轮——
    · 配置分组  完全相同（1920x1440|YUV_420_888|w5）
    · 采集路径  完全相同（burst_yuv_manual, 1）
    · 时间戳结构逐帧一致（139/139、5 帧组和恒为 166440000 ns）
  而 G23 仍报 WARN，**唯一理由是"报告集时间跨度 6h53m > 60 分钟"**。
  这条规则的问题不是"太严"，而是**与它要防的风险没有因果关系**：
    · 它要防的是"混入别的机器/固件的数据"（fx、读出时间、卷帘行为会变）；
    · 而"同一台机器隔几小时重跑"跨度同样变大 —— 这**正是 ③ 想要的条件**
      （G21 的主题就是跨会话可复现，要求掉电重开摄像头）；
    · 于是这个代理**既会漏**（快速换机时跨度没超限）**也会误**（同机隔夜重跑）。
  ⇒ 改为直接比设备/固件标识（报告 1.8.1 起自带 `device`），跨度只作注记。

本文件覆盖：
  G23 设备分组：一致 PASS / 不一致 WARN / 全缺 PASS+注记 / 部分缺 PASS+注记 /
                只换固件（build_fingerprint 不同）WARN
  G23 批次跨度：超限**不再**判 WARN，但要明说"不判为问题"及残余风险
  G23 配置/路径两类 WARN 仍在（确认没被新分支挤掉）

双向路径的意义：只验 PASS 会写出恒过判据，只验 WARN 会写出恒报错判据。
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

ROUNDS = ("m0_locked_capture_r1.json", "m0_locked_capture_r2.json",
          "m0_locked_capture_r3.json")
MAIN = "m0_locked_capture.json"
WATCH = ("G18", "G23")

DEV_A = {"manufacturer": "Xiaomi", "model": "24129PN74C", "device": "dada",
         "build_fingerprint": "Xiaomi/dada/dada:16/BP2A.250605.031.A3/OS3.0.307.0:user/release-keys",
         "android_sdk": 36}
DEV_B = {"manufacturer": "Xiaomi", "model": "24129PN74C", "device": "dada",
         "build_fingerprint": "Xiaomi/dada/dada:16/BP2A.250701.001.A1/OS3.0.400.0:user/release-keys",
         "android_sdk": 36}


def run(tag, mutate=None, delivery="stream"):
    d = tempfile.mkdtemp(prefix="m0t10_")
    try:
        for n in os.listdir(SRC):
            if n.endswith(".json") and not n.startswith("_"):
                shutil.copy(os.path.join(SRC, n), os.path.join(d, n))
        if mutate:
            mutate(d)
        r = subprocess.run([PY, SCRIPT, d, "--delivery", delivery],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        out = r.stdout
        if r.returncode not in (0, 1, 2):
            print("\n=== %s ===" % tag)
            print("  !! 脚本异常退出 rc=%s\n%s" % (r.returncode, r.stderr[-1200:]))
        want = {}
        for line in out.splitlines():
            for g in WATCH:
                if line.startswith(g + " "):
                    p = line.split(None, 2)
                    want[g] = (p[1] if len(p) > 1 else "?", (p[2] if len(p) > 2 else ""))
        total = [l for l in out.splitlines() if l.startswith("总判定")]
        print("\n=== %s ===" % tag)
        for g, (v, msg) in sorted(want.items()):
            print("  %s: %-9s %s" % (g, v, msg[:300]))
        for t in total:
            print("  " + t)
        return want
    finally:
        shutil.rmtree(d, ignore_errors=True)


def set_device(d, names, dev):
    """给指定报告写/删 device 段（dev=None 表示删除）。"""
    for n in names:
        p = os.path.join(d, n)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            j = json.load(f)
        if dev is None:
            j.pop("device", None)
        else:
            j["device"] = dict(dev)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(j, f, ensure_ascii=False)


def add_one_minute(d):
    """把四份报告的生成时间压到 1 分钟内（模拟"一次跑完"），其余不动。

    注意：真实 m0_out 里 ③ 比 ② 早 6h53m。要测"跨度大不再判 WARN"，
    最稳的做法不是去改真实数据，而是**两个方向都测**：
      · 真实（跨度 6h53m）-> 必须 PASS
      · 压到 1 分钟       -> 也必须 PASS
    两个方向都给 PASS 才能说明"跨度这个量已经不再参与判决"，
    否则"恰好因为别的原因 PASS"会伪装成"规则生效"。
    """
    t0 = 1_790_240_000_000
    for i, n in enumerate((MAIN,) + ROUNDS):
        p = os.path.join(d, n)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            j = json.load(f)
        j["generated_at_wall_ms"] = t0 + i * 1000
        with open(p, "w", encoding="utf-8") as f:
            json.dump(j, f, ensure_ascii=False)


results = []


def check(tag, w, g, expect, needle=None, absent=None):
    v = w.get(g, ("(未输出)", ""))
    ok = v[0] == expect
    if needle is not None:
        ok = ok and needle in v[1]
    if absent is not None:
        ok = ok and absent not in v[1]
    results.append((tag, ok, "%s=%s" % (g, v[0])))
    print("  %-52s %s    %s=%s" % (tag, "OK  " if ok else "FAIL", g, v[0]))


# ================================================ 设备身份分组
w = run("G23：四份都带同一设备标识", lambda d: set_device(d, (MAIN,) + ROUNDS, DEV_A))
check("G23 同一设备 → PASS", w, "G23", "PASS")
check("G23 同一设备 → 报出设备分组", w, "G23", "PASS", "设备/固件分组")
check("G23 同一设备 → 不再提未带标识", w, "G23", "PASS", absent="未带设备标识")

w = run("G23：第2轮换了机器",
        lambda d: (set_device(d, (MAIN,) + ROUNDS, DEV_A),
                   set_device(d, (ROUNDS[1],), DEV_B)))
check("G23 跨设备 → WARN", w, "G23", "WARN", "跨设备/固件")
check("G23 跨设备 → 说明为什么最严重", w, "G23", "WARN", "最严重的一类混批")
check("G23 跨设备 → 给出处置", w, "G23", "WARN", "只用**当前这台机器**的产出")

w = run("G23：只把某轮的 build_fingerprint 改了（刷固件）",
        lambda d: (set_device(d, (MAIN,) + ROUNDS, DEV_A),
                   set_device(d, (ROUNDS[2],), DEV_B)))
check("G23 换固件 → WARN", w, "G23", "WARN", "跨设备/固件")

# ⚠ 第 11 轮改（"断言依赖环境巧合"第四次复发）：以下两条原先跑真实数据，
# 理由分别是"真实 m0_out 里全都没有 device 段"和"真实跨度恰好 6h53m"。
# 用户用 v1.8.1 重跑后，四份报告**都带 device**、跨度只剩 0m49s ⇒ 断言失效。
# 照例改成注入式：想验哪个前提就自己构造它。
w = run("G23：全部报告都没有 device 段（注入：过渡期旧报告）",
        lambda d: set_device(d, (MAIN,) + ROUNDS, None))
check("G23 全无设备标识 → PASS 但记欠账", w, "G23", "PASS", "未带设备标识")
check("G23 全无设备标识 → 明说排除不了混机器", w, "G23", "PASS",
      "无法独立排除「报告来自不同机器」")
check("G23 全无设备标识 → 说明自哪个版本起有", w, "G23", "PASS", "自 1.8.1 起写入")

# 同上：先统一成同一台机器，再抹掉**其中一份**的 device 段（过渡期混合状态）。
# 直接只给主连拍写 DEV_A 是不行的：真实设备串与 DEV_A 的指纹不同，
# 那会变成"两台机器"（WARN），测不到"部分缺标识"这个过渡态。
w = run("G23：部分报告带设备标识（注入过渡期）",
        lambda d: (set_device(d, (MAIN,) + ROUNDS, DEV_A),
                   set_device(d, (ROUNDS[2],), None)))
check("G23 部分带标识 → 逐份点名缺失", w, "G23", "PASS", "第3轮（该报告没有 device 段")
check("G23 部分带标识 → 仍 PASS（混合版本不是设备不一致）", w, "G23", "PASS")

# ================================================ 批次跨度
def big_span(d):
    """注入 6h53m12s 跨度（不再依赖真实数据恰好跨夜）。"""
    p = os.path.join(d, ROUNDS[2])
    with open(p, encoding="utf-8") as f:
        j = json.load(f)
    j["generated_at_wall_ms"] = j["generated_at_wall_ms"] + (6 * 3600 + 53 * 60 + 12) * 1000
    with open(p, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False)


w = run("G23：注入时间跨度 6h53m12s → 不判为问题", big_span)
check("G23 跨度 6h53m → PASS", w, "G23", "PASS")
check("G23 跨度大 → 明说不再判为问题", w, "G23", "PASS", "不判为问题")
check("G23 跨度大 → 保留残余风险", w, "G23", "PASS", "残余风险")
check("G23 跨度大 → 不再要求整批重跑", w, "G23", "PASS", absent="整批重跑")

w = run("G23：把四份压到 1 分钟内 → 必须也是 PASS（两向同判）",
        lambda d: (set_device(d, (MAIN,) + ROUNDS, DEV_A), add_one_minute(d)))
check("G23 跨度 1 分钟 → PASS", w, "G23", "PASS")
check("G23 跨度小 → 不出现跨度注记", w, "G23", "PASS", absent="分钟")

# ================================================ 别的分支没被挤掉
def break_path(d):
    """把**主连拍**（1.8.0，报告自描述）的 rev 改掉。

    为什么不改 r1：1.5.0 的报告**不自描述**路径，`_resolve_capture_path` 会去查
    登记表，而登记表对三份 1.5.0 报告给出的是同一条记录 —— 改它们的报告字段
    根本不参与判定（这本身就是本轮要说明的事实，见下一条用例）。
    """
    p = os.path.join(d, MAIN)
    with open(p, encoding="utf-8") as f:
        j = json.load(f)
    j["capture_path_rev"] = 2
    j["capture_config"]["capture_path_rev"] = 2
    with open(p, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False)


w = run("G23：主连拍采集路径 rev 不同（回归：这条仍要 WARN）", break_path)
check("G23 路径不同 → WARN 仍生效", w, "G23", "WARN", "跨采集路径")

# 这条不是"测试判据"，而是**记录一个判据的边界**：1.5.0 的报告不自描述路径，
# 它们的 (id, rev) 全部由登记表给出 ⇒ 它们之间"路径相同"是同一条声明的一致，
# 而不是测量的一致。判据必须把这件事说出来，否则读者会以为它验证过了。
# ⚠ 第 11 轮改：1.5.0 期的报告已不在数据集里（用户全量重跑），
# 改成注入：把 r1~r3 伪装成 1.5.0 的产出（版本号 + **不自描述路径**），
# 这样 (id, rev) 只能来自登记表 ⇒ 才能验"声明的一致"这条边界。
def old_style(d, names, ver):
    for n in names:
        p = os.path.join(d, n)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            j = json.load(f)
        j["app_version"] = ver
        j.pop("capture_path_id", None)
        j.pop("capture_path_rev", None)
        cfg = j.get("capture_config") or {}
        cfg.pop("capture_path_id", None)
        cfg.pop("capture_path_rev", None)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(j, f, ensure_ascii=False)


w = run("G23：路径靠登记表声明的报告（注入 1.5.0 不自描述）→ 明说这是声明",
        lambda d: old_style(d, ROUNDS, "m0probe-1.5.0"))
check("G23 登记表声明的路径 → 明说不是测量的一致", w, "G23", "PASS",
      "声明的一致")
check("G23 登记表声明的路径 → 逐份点名", w, "G23", "PASS", "登记表（m0probe-1.5.0")


def break_cfg(d):
    p = os.path.join(d, ROUNDS[0])
    with open(p, encoding="utf-8") as f:
        j = json.load(f)
    j["capture_config"]["output_format"] = "JPEG"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False)


w = run("G23：某轮格式不同（回归：这条仍要 WARN）", break_cfg)
check("G23 配置不同 → WARN 仍生效", w, "G23", "WARN", "跨配置")

n_fail = sum(1 for _, ok, _ in results if not ok)
print("\n" + "=" * 74)
print("通过 %d / %d" % (len(results) - n_fail, len(results)))
if n_fail:
    print("未通过：")
    for tag, ok, info in results:
        if not ok:
            print("  - %s  (%s)" % (tag, info))
# 自检：确保断言真的被计入（防"测试脚本静默失效"）
assert len(results) >= 18, "用例数异常，测试可能没跑完"
assert n_fail == 0, "有未通过用例"
