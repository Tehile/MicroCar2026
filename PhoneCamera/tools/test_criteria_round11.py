# -*- coding: utf-8 -*-
"""判据双向路径测试（第 11 轮：设备级瞬时故障的显式化 —— G18/G24/G26）。

背景（这一轮为什么动这三条判据）：
  第 11 轮实测：④ 的 5/8 档、⑤ 的全部 5 档、⑥ 的全部 2 档在同一时间窗内失败，
  错误是「摄像头被断开」（onDisconnected）与 createDefaultRequest ENOSYS。
  旧判据在这种批次上的三处过度结论：
    · G18 只拿成功档说事，一句不提失败档；
    · G24 在 0 个有效档时仍打印"各档 skew 完全相同（9.21 ms）"与
      "本批未记录请求帧率（各档实测均 ≈30.04 fps）"—— 本批一个实测都没有；
    · G26 对 error 档继续走字段检查，缺指纹的对照档被判成"指纹一致"（假阳性）。

本文件覆盖（双向）：
  G18 部分失败：列出失败档与错误分组 + 慢档措辞限定为"成功的慢速档"；
                无失败档的对照 ⇒ 不得出现失败提示
  G24 全灭批：明示"本批没有任何实测"、历史结论标轮次、不再打印"未记录请求帧率"；
              有有效档（缺 target_fps）⇒ 该警告保留；有效档带 target_fps ⇒ 恢复批次结论
  G26 交付档 error ⇒ INCOMPLETE（不是 WARN，也不是字段缺失的流水账）；
      对照档 error ⇒ WARN"对照检查缺席"且不得报"指纹一致"；
      对照档缺指纹字段 ⇒ "无法逐段比对"，不得报"指纹一致"；
      整条对照档缺席 ⇒ WARN（"与连拍一致"需要对照档才有意义）

⚠ 第 12 轮修正：本文件原先直接拿"真实数据恰好是坏的"当夹具（④ 5/8 失败、
  ⑤ 5/5 失败、⑥ 2/2 失败）。用户用 v1.8.2 重跑成功后，13 条断言集体失效 ——
  同一个坑的**第五次**复发。⇒ 现在所有前提一律**注入构造**，
  与 m0_out 的现状无关（健康数据只当反面控制用）。
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

RATE = "m0_rate_sweep.json"
SIZE = "m0_size_sweep.json"
STILL = "m0_still_shot.json"
WATCH = ("G18", "G24", "G26")

results = []


def run(tag, mutate=None, delivery="stream", watch=WATCH):
    d = tempfile.mkdtemp(prefix="m0t11_")
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
            for g in watch:
                if line.startswith(g + " "):
                    p = line.split(None, 2)
                    want[g] = (p[1] if len(p) > 1 else "?", line)
        total = [l for l in out.splitlines() if l.startswith("总判定")]
        print("\n=== %s ===" % tag)
        for g, (v, msg) in sorted(want.items()):
            print("  %s: %-9s %s" % (g, v, msg[:200]))
        for t in total:
            print("  " + t)
        return want
    finally:
        shutil.rmtree(d, ignore_errors=True)


def patch(name, fn):
    def m(d):
        p = os.path.join(d, name)
        with open(p, encoding="utf-8") as f:
            j = json.load(f)
        fn(j)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(j, f, ensure_ascii=False)
    return m


def check(tag, w, g, expect_verdict, needle=None, absent=None):
    got = w.get(g)
    ok = got is not None and got[0] == expect_verdict
    detail = got[1] if got else ""
    if ok and needle:
        ok = needle in detail
    if ok and absent:
        ok = absent not in detail
    results.append((tag, ok))
    print("  [%s] %s" % ("PASS" if ok else "FAIL", tag))
    if not ok:
        print("      期望 %s 含 %r 不含 %r；实际 %s" %
              (expect_verdict, needle, absent, (got[0], got[1][:260]) if got else None))


# ============ 夹具：自造前提，不依赖 m0_out 的现状 ============
def rate_healthy(d):
    """合成一份健康的 ④：4 个档位 × 2 种下发方式，全部有数据。"""
    trials = []
    for legacy in (False, True):
        for fps, iv in ((12, 83232000.0), (15, 66576000.0), (24, 41616000.0),
                        (60, 33287995.0)):
            trials.append({
                "requested_fps": fps, "legacy_fps_range": legacy,
                "requested_ns": int(1e9 / fps),
                "actual_frame_duration_ns": int(iv), "honored_verbatim": False,
                "actual_interval_ns": iv, "actual_fps": 1e9 / iv, "valid_frames": 9,
                "capture_size": {"w": 1920, "h": 1440},
                "config_fingerprint": "1920x1440|a4096x3072|c4096x3072@0,0|YUV_420_888|w3 #t",
            })
    patch(RATE, lambda j: j.update(trials=trials))(d)


def rate_fail_first(d, n=5):
    """把前 n 档改成失败：4 档「摄像头被断开」+ 1 档 createDefaultRequest ENOSYS。"""
    def f(j):
        for i, t in enumerate(j["trials"]):
            if i >= n:
                break
            t.pop("actual_interval_ns", None)
            t.pop("actual_fps", None)
            t["error"] = ("摄像头被断开" if i < 4 else
                          "下发请求失败: CameraAccessException: CAMERA_ERROR (3): "
                          "createDefaultRequest:1722: Camera 0: Error creating default "
                          "request for template 6: Function not implemented (-38)")
    patch(RATE, f)(d)


def sweep_trials(*, req_fps=30):
    """合成健康的 ⑤：三档 4:3 + 16:9 普通档 + 16:9 全crop 判别档，行时间统一 2999 ns。"""
    SK43, SK169 = 9212928, 6909696

    def t(w, h, cw, ch, *, forced=False, skew=SK43, fps=30.0367):
        o = {"label": "合成档", "target_width": w, "forced_full_crop": forced,
             "requested_size": {"w": w, "h": h}, "actual_size": {"w": w, "h": h},
             "actual_interval_ns": 1e9 / fps, "rolling_shutter_skew_ns": float(skew),
             "crop_region": {"left": 0, "top": 0, "width": cw, "height": ch},
             "crop_shrunk_for_aspect": False, "valid_frames": 40}
        if req_fps is not None:
            o["target_fps"] = req_fps
        return o
    return [t(1920, 1440, 4096, 3072), t(2880, 2160, 4096, 3072),
            t(4096, 3072, 4096, 3072), t(3840, 2160, 4096, 2304, skew=SK169),
            t(3840, 2160, 4096, 3072, forced=True, skew=SK169)]


def sweep_healthy(d, req_fps=30):
    patch(SIZE, lambda j: j.update(trials=sweep_trials(req_fps=req_fps)))(d)


def sweep_no_reqfps(d):
    sweep_healthy(d, req_fps=None)


def sweep_all_fail(d):
    def f(j):
        kept = []
        for t in j.get("trials") or []:
            kept.append({"label": t.get("label"),
                         "target_width": t.get("target_width"),
                         "forced_full_crop": t.get("forced_full_crop"),
                         "requested_size": t.get("requested_size"),
                         "error": "摄像头被断开"})
        j["trials"] = kept
    patch(SIZE, f)(d)


def still_error(trial_ids):
    """把指定 trial 改成 error（只留 id/label，模拟"跑了但失败"）。"""
    def m(d):
        p = os.path.join(d, STILL)
        with open(p, encoding="utf-8") as f:
            j = json.load(f)
        for t in j.get("trials") or []:
            if t.get("trial_id") in trial_ids:
                tid, lab = t.get("trial_id"), t.get("label")
                t.clear()
                t["trial_id"] = tid
                t["label"] = lab
                t["error"] = "摄像头被断开"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(j, f, ensure_ascii=False)
    return m


def still_yuv_stripped(d):
    """对照档在、但没有可用字段（缺 config_fingerprint / stats / frames）。"""
    p = os.path.join(d, STILL)
    with open(p, encoding="utf-8") as f:
        j = json.load(f)
    for t in j.get("trials") or []:
        if t.get("trial_id") == "still_yuv_single":
            tid, lab = t.get("trial_id"), t.get("label")
            t.clear()
            t.update({"trial_id": tid, "label": lab,
                      "report_kind": "m0_still_shot_trial",
                      "capture_path_id": tid, "capture_path_rev": 2,
                      "camera_id": "0", "capture_size": {"w": 1920, "h": 1440},
                      "capture_config": {}, "frame_count": 0, "frames": [], "stats": {}})
    with open(p, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False)


def still_no_yuv(d):
    p = os.path.join(d, STILL)
    with open(p, encoding="utf-8") as f:
        j = json.load(f)
    j["trials"] = [t for t in j.get("trials") or []
                   if t.get("trial_id") != "still_yuv_single"]
    with open(p, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False)


# ============ G18：部分失败必须显式化 ============
w = run("G18 注入：前 5 档失败（摄像头被断开×4 + ENOSYS×1）", rate_fail_first)
check("G18 部分失败 → WARN 维持", w, "G18", "WARN", "帧率可控，但有档位不可达")
check("G18 部分失败 → 报出失败计数", w, "G18", "WARN", "5/8 档未取得数据")
check("G18 部分失败 → 错误按串分组", w, "G18", "WARN", "摄像头被断开（4 档")
check("G18 部分失败 → 慢档措辞限定成功档", w, "G18", "WARN", "成功的慢速档位均跟随")
check("G18 部分失败 → 明说只覆盖成功档", w, "G18", "WARN", "只覆盖成功档位")

w = run("G18 对照：注入健康 ④（无失败档）", rate_healthy)
check("G18 无失败档 → 不出现失败提示", w, "G18", "WARN", absent="未取得数据")
check("G18 无失败档 → 不提瞬时故障", w, "G18", "WARN", absent="设备级瞬时故障")


# ============ G24：全灭批不得拿历史结论当本批数据 ============
w = run("G24 注入：⑤ 5/5 档全灭", sweep_all_fail)
check("G24 全灭 → 明示本批无实测", w, "G24", "INFO", "本批没有取到任何档位的实测")
check("G24 全灭 → 历史结论标轮次", w, "G24", "INFO", "历史证据")
check("G24 全灭 → 不再打印「未记录请求帧率」", w, "G24", "INFO", absent="未记录请求帧率")
check("G24 全灭 → 不打印本批 skew 结论", w, "G24", "INFO", absent="**skew 完全相同**")

w = run("G24 对照：有效档但缺请求帧率", sweep_no_reqfps)
check("G24 有效档缺请求帧率 → 警告保留", w, "G24", "INFO", "未记录请求帧率")
check("G24 有效档 → skew 结论恢复", w, "G24", "INFO", "**skew 完全相同**")
check("G24 有效档 → 不再说本批无实测", w, "G24", "INFO",
      absent="本批没有取到任何档位的实测")

w = run("G24 对照：有效档且带请求帧率", sweep_healthy)
check("G24 有效档带请求帧率 → 无警告", w, "G24", "INFO", absent="未记录请求帧率")
check("G24 有效档带请求帧率 → 打印请求→实测", w, "G24", "INFO", "请求30→实测")


# ============ G26：error 档 ⇒ INCOMPLETE / 对照档缺席 ⇒ 不得报"一致" ============
w = run("G26 注入：交付档 error（--delivery still）",
        still_error({"still_jpeg_template"}), delivery="still")
check("G26 交付档 error → INCOMPLETE", w, "G26", "INFO", "本次未跑成")
check("G26 交付档 error → 指认瞬时故障", w, "G26", "INFO", "设备级瞬时故障")
check("G26 交付档 error → 不得报指纹一致", w, "G26", "INFO", absent="指纹一致")

w = run("G26 注入：两档都 error（--delivery still）",
        still_error({"still_yuv_single", "still_jpeg_template"}), delivery="still")
check("G26 两档都 error → 带上对照档也失败", w, "G26", "INFO", "对照档本次也同样失败")

w = run("G26 注入：对照档 error + 交付档正常（--delivery still）",
        still_error({"still_yuv_single"}), delivery="still")
check("G26 对照档 error → WARN 对照缺席", w, "G26", "WARN", "对照档本次未跑成")
check("G26 对照档 error → 不得报指纹一致", w, "G26", "WARN", absent="指纹一致")

w = run("G26 注入：对照档缺指纹字段（--delivery still）",
        still_yuv_stripped, delivery="still")
check("G26 对照档缺指纹 → WARN 无法比对", w, "G26", "WARN", "无法逐段比对")
check("G26 对照档缺指纹 → 对照检查缺席", w, "G26", "WARN", "对照检查缺席")
check("G26 对照档缺指纹 → 不得报指纹一致", w, "G26", "WARN", absent="指纹一致")

w = run("G26 注入：对照档整条缺席（--delivery still）", still_no_yuv, delivery="still")
check("G26 无对照档 → WARN 对照缺席", w, "G26", "WARN", "没有 still_yuv_single 对照档")
check("G26 无对照档 → 不报指纹一致", w, "G26", "WARN", absent="指纹一致")

# 反面控制：真实健康批次 ⇒ 不得出现上面任何一类告警
# （这条依赖 m0_out 当前健康；若数据变了它会失败，届时按第 12 轮的办法改成注入）
w = run("G26 对照：健康批次（真实数据，不注入）", None, delivery="still")
check("G26 健康批次 → 不报未跑成", w, "G26", "PASS", absent="未跑成")
check("G26 健康批次 → 配对无告警", w, "G26", "PASS", absent="没有被任何结果认领")


# ============ 汇总 ============
n_ok = sum(1 for _, ok in results if ok)
print("\n" + "=" * 60)
print("round11 合计：%d/%d 通过" % (n_ok, len(results)))
sys.exit(0 if n_ok == len(results) else 1)
