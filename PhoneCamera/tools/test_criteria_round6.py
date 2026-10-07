# -*- coding: utf-8 -*-
"""判据双向路径测试（第 6 轮新增/改动的判据）。

原则：只验 FAIL 路径 -> 写出"永远报错"的判据；只验 PASS 路径 -> 写出恒过判据。
本轮改动的判据：G14（skew 归属口径）、G23（配置/版本/批次）、G24（定档与归属判别）。
"""
import copy
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


def load(name):
    with open(os.path.join(SRC, name), encoding="utf-8") as f:
        return json.load(f)


def run(tag, mutate):
    """把 m0_out 复制到临时目录，施加 mutate，跑评估并抽出关心的判据行。"""
    d = tempfile.mkdtemp(prefix="m0t_")
    try:
        for n in os.listdir(SRC):
            if n.endswith(".json"):
                shutil.copy(os.path.join(SRC, n), os.path.join(d, n))
        if mutate:
            mutate(d)
        out = subprocess.run([PY, SCRIPT, d], capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout
        want = {}
        for line in out.splitlines():
            for g in ("G14", "G23", "G24"):
                if line.startswith(g + " "):
                    parts = line.split(None, 2)
                    want[g] = (parts[1] if len(parts) > 1 else "?",
                               (parts[2] if len(parts) > 2 else ""))
        total = [l for l in out.splitlines() if l.startswith("总判定")]
        print(f"\n=== {tag} ===")
        for g in ("G14", "G23", "G24"):
            v = want.get(g)
            print(f"  {g}: {v[0] if v else '(未输出)'}  {(v[1] if v else '')[:150]}")
            if g == "G24" and v:
                # G24 的判决在句子末尾，前 150 字看不到，单独抽出来
                tail = v[1]
                k = tail.find("⑤ 的 skew 归属")
                print("      → 归属判决: " + (tail[k:] if k >= 0 else "(无)"))
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


results = []

# ---------- 基线与真人真数据 ----------
results.append(("基线（真实数据）", run("基线：主连拍旧版 + ③三轮新版", None)))

# ---------- G23 四个路径 ----------
def same_all(d):
    """PASS 路径：把主连拍也标成新版 App、并把时间戳挪到同一批次。"""
    def f(j):
        j["app_version"] = "m0probe-1.5.0"
        j["capture_config"]["config_fingerprint"] = (
            "1920x1440|a4096x3072|c4096x3072@0,0|YUV_420_888|w5 #07ffb85a")
        j["generated_at_wall_ms"] = load(ROUNDS[-1])["generated_at_wall_ms"] - 3000
    patch(d, BASE, f)
    for r in ROUNDS:
        patch(d, r, lambda j: j.update(app_version="m0probe-1.5.0"))


results.append(("G23 PASS", run("G23 PASS 路径（同配置+同版本+同批次）", same_all)))


def cross_cfg(d):
    """WARN 路径：把某一轮的输出尺寸改掉（跨配置）。"""
    def f(j):
        j["capture_config"]["output_size"] = {"w": 1920, "h": 1080}
        j["capture_config"]["config_fingerprint"] = "1920x1080|... #deadbeef"
    patch(d, "m0_locked_capture_r2.json", f)


results.append(("G23 WARN 跨配置",
                run("G23 WARN 路径（某轮换了输出尺寸）", cross_cfg)))


def cross_ver(d):
    """WARN 路径：只把版本改掉、配置不动（本次真实情形）。"""
    patch(d, "m0_locked_capture_r3.json", lambda j: j.update(app_version="m0probe-9.9.9"))


results.append(("G23 WARN 跨版本",
                run("G23 WARN 路径（某轮换了 App 版本）", cross_ver)))


def stale_batch(d):
    """WARN 路径：同为最新版但时间差了 2 小时。"""
    def f(j):
        j["app_version"] = "m0probe-1.5.0"
    patch(d, BASE, f)
    for r in ROUNDS:
        patch(d, r, f)
    patch(d, "m0_locked_capture_r1.json",
          lambda j: j.update(generated_at_wall_ms=j["generated_at_wall_ms"] - 7200_000))


results.append(("G23 WARN 跨批次",
                run("G23 WARN 路径（差 2 小时）", stale_batch)))


def only_one(d):
    """INFO 路径：删掉三轮，只剩主连拍。"""
    for r in ROUNDS:
        os.remove(os.path.join(d, r))


results.append(("G23 INFO 仅1份", run("G23 INFO 路径（只有主连拍）", only_one)))

# ---------- G14 ----------
def skew_varies(d):
    """WARN 路径：让**有效帧里的一部分** skew 变成另一个值。

    注意注入位置：预热帧（前 warmup_frames 帧）会被判据剔除，所以只改前几帧
    等于没改 —— 第一版测试就是这么写错的，结果"FAIL 路径"跑出了 PASS。
    """
    def f(j):
        warm = (j.get("params") or {}).get("warmup_frames", 0) or 0
        for i, x in enumerate(j["frames"]):
            if i >= warm + 6 and isinstance(x.get("rolling_shutter_skew_ns"), (int, float)):
                if i % 3 == 0:
                    x["rolling_shutter_skew_ns"] += 100000
    patch(d, BASE, f)


results.append(("G14 WARN 变化",
                run("G14 FAIL 路径（有效帧里 skew 出现两个值）", skew_varies)))

# ---------- G24 ----------
def no_trials(d):
    os.remove(os.path.join(d, "m0_size_sweep.json"))


results.append(("G24 无扫描",
                run("G24 INFO 路径（无 ⑤ 扫描）", no_trials)))


def fake_169(d, skew, tag):
    """G24 判别路径：伪造一条"16:9 输出 + 全阵列 crop"的判别档。

    - skew 按**输出纵横比**算（2999 × 4096×9/16 = 6909696）⇒ 与其余档同一行时间；
    - skew 按 **crop 高度**算（= 9212928）⇒ 行时间变成 3999.5 ns，口径不唯一。
    两条都要跑，否则只验了一条路径。
    """
    def f(j):
        j["trials"].append({
            "label": f"**判别档** 16:9 输出 + 强制全阵列 crop（{tag}）",
            "target_width": 1920, "forced_full_crop": True,
            "requested_size": {"w": 1920, "h": 1080},
            "actual_size": {"w": 1920, "h": 1080},
            "crop_region": {"left": 0, "top": 384, "width": 4096, "height": 3072},
            "config_fingerprint": "1920x1080|a4096x3072|c4096x3072@0,0|YUV_420_888|w5 #cafe0001",
            "actual_interval_ns": 33288002.0, "actual_fps": 30.04,
            "rolling_shutter_skew_ns": skew,
            "valid_frames": 40,
        })
    patch(d, "m0_size_sweep.json", f)


results.append(("G24 判别档-归属纵横比",
                run("G24 判别档（skew 按输出纵横比 = 6909696）",
                    lambda d: fake_169(d, 6909696, "按纵横比"))))
results.append(("G24 判别档-归属crop",
                run("G24 判别档（skew 按 crop 高度 = 9212928）",
                    lambda d: fake_169(d, 9212928, "按crop高度"))))

# ---------- 汇总 ----------
print("\n" + "=" * 70)
for tag, w in results:
    print(f"{tag:16s} G14={w.get('G14', ('-',))[0]:5s} G23={w.get('G23', ('-',))[0]:5s} "
          f"G24={w.get('G24', ('-',))[0]:5s}")
