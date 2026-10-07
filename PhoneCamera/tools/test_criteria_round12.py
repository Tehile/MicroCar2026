# -*- coding: utf-8 -*-
"""判据双向路径测试（第 12 轮：图与结果的配对 —— 逐帧判到"时序假象/真丢/未到达"）。

背景（这一轮为什么改 G26 与 App）：
  用户用 v1.8.2 重跑 ⑥⑤④ 全部成功。⑥ 的**交付档**配对 8/8 完美，但**对照档**
  报出 `matched=3 / missing=5 / surplus=4`。逐帧核验发现：被判 missing 的 5 张里
  **有 4 张的时间戳同时出现在 "未被任何结果认领的图" 列表里** —— 同一时间戳两处出现，
  只可能是"**图比结果晚到**"。≤1.8.2 的配对是在结果回调里查表（在线），
  真丢图与晚到的图**区分不开**；另外第 8 张（有效张）在结果回调到齐时还没取到，
  被记成丢图，其实只是"还在飞"。
  ⇒ App v1.8.3：改为**结算时按时间戳集合离线配对** + 结算前等末帧图到达（≤1.2 s）；
     G26：改用**逐帧**结果判定，把三件事分开 ——
       ① 时序假象（时间戳同时出现在 surplus 里）→ 注记，不 WARN
       ② 有效张真丢图 → WARN
       ③ 预热张缺图 → 注记
     外加：真·未被认领的图 → WARN；`images_pending>0` → WARN（"还没到"≠"丢了"）；
     汇总与逐帧打架 → WARN。

本文件覆盖（双向）：假象不 WARN / 真丢 WARN / 多余图 WARN / 未到达 WARN /
汇总与逐帧不一致 WARN / 归一化健康批 PASS。
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
STILL = "m0_still_shot.json"

results = []


def run(tag, mutate=None, delivery="still"):
    d = tempfile.mkdtemp(prefix="m0t12_")
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
            if line.startswith("G26 "):
                p = line.split(None, 2)
                want["G26"] = (p[1] if len(p) > 1 else "?", line)
        print("\n=== %s ===" % tag)
        for g, (v, msg) in sorted(want.items()):
            print("  %s: %-9s %s" % (g, v, msg[:260]))
        for t in out.splitlines():
            if t.startswith("总判定"):
                print("  " + t)
        return want
    finally:
        shutil.rmtree(d, ignore_errors=True)


def check(tag, w, expect, needle=None, absent=None):
    got = w.get("G26")
    ok = got is not None and got[0] == expect
    detail = got[1] if got else ""
    if ok and needle:
        ok = needle in detail
    if ok and absent:
        ok = absent not in detail
    results.append((tag, ok))
    print("  [%s] %s" % ("PASS" if ok else "FAIL", tag))
    if not ok:
        print("      期望 %s 含 %r 不含 %r；实际 %s" %
              (expect, needle, absent, (got[0], got[1][:300]) if got else None))


def _edit_still(d, fn):
    p = os.path.join(d, STILL)
    with open(p, encoding="utf-8") as f:
        j = json.load(f)
    fn(j)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False)


def jpg_trial(j):
    for t in j.get("trials") or []:
        if t.get("trial_id") == "still_jpeg_template":
            return t
    return None


def normalize(d, *, mode="1.8.3"):
    """把交付档的配对归一化成"一一配对"（清掉当前数据集的一切配对特征），
    再让每条用例只引入**一个**配对问题。"""
    def f(j):
        t = jpg_trial(j)
        if t is None:
            raise SystemExit("夹具缺 still_jpeg_template")
        pair = (t.get("stats") or {}).get("image_pairing")
        if not isinstance(pair, dict):
            raise SystemExit("夹具缺 image_pairing")
        frames = t.get("frames") or []
        for fr in frames:
            fr["image_matched"] = True
            if isinstance(fr.get("jpeg"), dict):
                pass
        n = len(frames)
        n_warm = len([x for x in frames if x.get("is_warmup")])
        pair["images_acquired"] = n
        pair["images_matched"] = n
        pair["images_matched_valid"] = n - n_warm
        pair["images_missing"] = 0
        pair["images_missing_valid"] = 0
        pair["images_missing_warmup"] = 0
        pair["images_surplus"] = 0
        pair["surplus_ts"] = []
        pair["images_pending"] = 0
        pair["acquire_null"] = 0
        pair["read_errors"] = 0
        pair["pairing_mode"] = ("离线：结算时按图时间戳集合逐帧配对（与回调先后无关）"
                                if mode == "1.8.3" else "在线：结果回调时查表")
    _edit_still(d, f)


def mk(mode="1.8.3", extra=None):
    def m(d):
        normalize(d, mode=mode)
        if extra:
            _edit_still(d, extra)
    return m


def ts_of(fr):
    return fr.get("sensor_timestamp_ns")


def set_missing(fr, pair, flag):
    fr["image_matched"] = flag


# ---------------- 基线：归一化后必须 PASS 且给出"一一配对" ----------------
w = run("基线：配对归一化为一一对应（注入）", mk())
check("基线 → PASS", w, "PASS", "一一配对")


# ---------------- ① 时序假象：≤1.8.2 的在线配对 ----------------
def artifact(j):
    t = jpg_trial(j)
    frames = t.get("frames") or []
    pair = t["stats"]["image_pairing"]
    warm = [f for f in frames if f.get("is_warmup")][:2]
    for fr in warm:
        set_missing(fr, pair, False)
    pair["images_matched"] = len(frames) - len(warm)
    pair["images_missing"] = len(warm)
    pair["images_missing_valid"] = 0
    pair["images_missing_warmup"] = len(warm)
    pair["images_surplus"] = len(warm)
    pair["surplus_ts"] = [ts_of(f) for f in warm]
    pair["pairing_mode"] = "在线：结果回调时查表"
    for k in ("images_matched_valid",):
        pair[k] = pair.get(k, len(frames) - len(warm))


w = run("① 时序假象（missing 的时间戳同时在 surplus 里，全部是预热张）",
        mk(mode="1.8.2", extra=artifact))
check("① 假象 → 不因它 WARN", w, "PASS", "比结果晚到")
check("① 假象 → 点名是时序假象", w, "PASS", "时序假象")
check("① 假象 → 不得说有效张缺图", w, "PASS", absent="有效张**没有配对到图")


# ---------------- ② 有效张真丢图 ----------------
def genuine_valid_missing(j):
    t = jpg_trial(j)
    frames = t.get("frames") or []
    pair = t["stats"]["image_pairing"]
    valid = [f for f in frames if not f.get("is_warmup")]
    if not valid:
        raise SystemExit("夹具没有有效张")
    victim = valid[0]
    set_missing(victim, pair, False)
    pair["images_matched"] = len(frames) - 1
    pair["images_missing"] = 1
    pair["images_missing_valid"] = 1
    pair["images_matched_valid"] = len(valid) - 1
    pair["surplus_ts"] = []          # 关键：时间戳**不在** surplus 里 ⇒ 不是晚到
    pair["images_surplus"] = 0


w = run("② 有效张真丢图（时间戳不在 surplus 里）", mk(extra=genuine_valid_missing))
check("② 真丢 → WARN", w, "WARN", "有效张")
check("② 真丢 → 说明交付会少图", w, "WARN", "交付会少图")
check("② 真丢 → 不得说成时序假象", w, "WARN", absent="时序假象")


# ---------------- ③ 真·未被任何结果认领的图 ----------------
def unclaimed(j):
    t = jpg_trial(j)
    frames = t.get("frames") or []
    pair = t["stats"]["image_pairing"]
    pair["images_surplus"] = 1
    pair["surplus_ts"] = [ts_of(frames[0]) + 999_999_999]   # 不属于任何结果
    pair["images_acquired"] = len(frames) + 1


w = run("③ 有图没被任何结果认领", mk(extra=unclaimed))
check("③ 多余图 → WARN", w, "WARN", "没有被任何结果认领")
check("③ 多余图 → 说明配错比少图更严重", w, "WARN", "配错比少图更严重")


# ---------------- ④ 末帧还在飞（images_pending） ----------------
def pending(j):
    t = jpg_trial(j)
    frames = t.get("frames") or []
    pair = t["stats"]["image_pairing"]
    pair["images_pending"] = 1
    pair["images_acquired"] = len(frames) - 1
    pair["images_matched"] = len(frames) - 1
    last = frames[-1]
    set_missing(last, pair, False)
    pair["images_missing"] = 1
    if last.get("is_warmup"):
        pair["images_missing_warmup"] = 1
    else:
        pair["images_missing_valid"] = 1


w = run("④ 结算时仍有图未到达（images_pending>0）", mk(extra=pending))
check("④ 未到达 → WARN", w, "WARN", "尚未取到")
check("④ 未到达 → 区分「还没到」与「丢了」", w, "WARN", "没到")


# ---------------- ⑤ 汇总与逐帧打架 ----------------
def mismatch(j):
    t = jpg_trial(j)
    pair = t["stats"]["image_pairing"]
    pair["images_missing_valid"] = 3      # 逐帧里一张都不缺


w = run("⑤ 汇总说有效张缺 3，逐帧一张不缺", mk(extra=mismatch))
check("⑤ 打架 → WARN", w, "WARN", "汇总与逐帧打架")


# ---------------- ⑥ 1.8.3 的健康批（离线配对 + 分项汇总一致） ----------------
def v183(j):
    t = jpg_trial(j)
    frames = t.get("frames") or []
    pair = t["stats"]["image_pairing"]
    for fr in frames:
        fr["image_matched"] = True
    n = len(frames)
    n_warm = len([x for x in frames if x.get("is_warmup")])
    pair.update({"images_acquired": n, "images_matched": n,
                 "images_matched_valid": n - n_warm, "images_missing": 0,
                 "images_missing_valid": 0, "images_missing_warmup": 0,
                 "images_surplus": 0, "surplus_ts": [], "images_pending": 0,
                 "settle_wait_ms": 300,
                 "pairing_mode": "离线：结算时按图时间戳集合逐帧配对（与回调先后无关）"})


w = run("⑥ 1.8.3 健康批（离线配对；分项汇总与逐帧一致）", mk(extra=v183))
check("⑥ 健康 → PASS", w, "PASS", "一一配对")
check("⑥ 健康 → 报出有效张数", w, "PASS", "有效张")


# ============ 汇总 ============
n_ok = sum(1 for _, ok in results if ok)
print("\n" + "=" * 60)
print("round12 合计：%d/%d 通过" % (n_ok, len(results)))
sys.exit(0 if n_ok == len(results) else 1)
