# -*- coding: utf-8 -*-
"""判据双向路径测试（第 9 轮新增/改动的判据）。

本轮的任务是**确定路线**（D24：连续流 + 抽帧交付，见 docs/M0-路线决策.md）。
路线一定，就冒出一个新的证据边界：

    采集路径是三元组 {采集模板, 调度方式, HAL 输出格式}。
    前两项由 (capture_path_id, rev) + G23 看着；
    **HAL 输出格式此前没有任何判据在看。**

于是「为了省带宽，把交付改成让 HAL 直接出 JPEG」这类改动，会让交付物**静默地**
离开已取证路径 —— 与 R22 是同一类错误，只是下沉了一层。
G23 救不了：它只比较「报告之间」，不比较「报告与交付配置」（R20 已写明）。

所以本轮新增 **G27**（交付采集格式一致性），并把 `--delivery` 默认值改成 stream。
**两件事都必须双向验**：
  · 只验 PASS 会写出恒过判据 ⇒ 要有「不一致 → WARN」「取不到 → WARN」的反向用例；
  · 只验 FAIL 会写出恒报错判据 ⇒ 要有「默认值就是对的」「旧报告缺字段也能判」的正向用例。

本文件额外盯住两个**已经踩过的坑**：
  · **断言依赖环境巧合** —— round8 里一大批 G26 用例原先靠「`--delivery` 默认值是 still」
    进入拍照分支；默认值一改就集体失效。这里「默认值」是被**单独断言**的对象，
    不作为其它用例的隐含前提。
  · **判据只看"第一个"** —— 若取格式时只取第一个找到的，各报告格式不一致会被静默忽略。
    反向用例专门制造这种不一致，要求判据自己报出来。
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

WATCH = ("G26", "G27", "G18")
ROUND_REPORTS = ("m0_locked_capture.json", "m0_locked_capture_r1.json",
                 "m0_locked_capture_r2.json", "m0_locked_capture_r3.json")


def run(tag, mutate=None, args=()):
    """把 m0_out 复制到临时目录，施加 mutate，跑评估并抽出关心的判据行。

    **参数原样透传**（不替调用方补 `--delivery`）—— 本文件要能测"不传参数时的默认行为"。
    返回 (判据字典, 总判定那一行)。
    """
    d = tempfile.mkdtemp(prefix="m0t9_")
    try:
        for n in os.listdir(SRC):
            if n.endswith(".json") and not n.startswith("_"):
                shutil.copy(os.path.join(SRC, n), os.path.join(d, n))
        if mutate:
            mutate(d)
        r = subprocess.run([PY, SCRIPT, d] + list(args), capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        out = r.stdout
        if r.returncode not in (0, 1, 2):
            print("\n=== %s ===" % tag)
            print("  !! 脚本异常退出 rc=%s\n%s" % (r.returncode, r.stderr[-1200:]))
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
                                    (v[1] if v else "")[:160]))
        if total:
            print("  " + total[0])
        return want, (total[0] if total else "")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def patch(d, name, fn):
    p = os.path.join(d, name)
    with open(p, encoding="utf-8") as f:
        j = json.load(f)
    fn(j)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False)


def set_fmt_all(d, fmt):
    """把四份连拍报告**全部**改成一个格式（只改 output_format 字段）。"""
    def f(j):
        j["capture_config"]["output_format"] = fmt
    for n in ROUND_REPORTS:
        patch(d, n, f)


results = []


def check(tag, w, g, expect_verdict, needle=None, absent=None):
    v = w.get(g, ("(未输出)", ""))
    ok = (v[0] == expect_verdict)
    if needle is not None:
        ok = ok and (needle in v[1])
    if absent is not None:
        ok = ok and (absent not in v[1])
    results.append((tag, ok, "%s=%s" % (g, v[0])))


def check_total(tag, total, needle):
    ok = needle in (total or "")
    results.append((tag, ok, (total or "(无总判定)")[:60]))


# ================================================================ 正向：默认值
print("=" * 74)
print("正向路径：默认声明（不传任何参数）")
print("=" * 74)

w, t = run("G27 基线：默认声明", None)
check("G27 默认 = yuv，与证据一致 → PASS", w, "G27", "PASS", "交付物落在已取证路径上")
check("G27 报出格式的来源出处", w, "G27", "PASS", "capture_config.output_format")
check("默认 --delivery = stream（D24）→ G26 不适用", w, "G26", "INFO", "本项不适用")
check_total("默认路线下总判定是 WARN（不再 INCOMPLETE）", t, "总判定: WARN")


# ================================================================ 正向：回退取值
print("\n" + "=" * 74)
print("正向路径：报告缺 output_format 字段时，从配置指纹里取回格式")
print("=" * 74)


def drop_out_fmt_keep_fp(d):
    """删掉 capture_config.output_format，指纹保留 —— 模拟 1.6.0 及更早的报告。"""
    def f(j):
        j["capture_config"].pop("output_format", None)
    patch(d, "m0_locked_capture.json", f)


w, t = run("G27 正向：只有指纹里有格式", drop_out_fmt_keep_fp)
check("G27 指纹回退仍判 PASS（不因缺字段就报 WARN）", w, "G27", "PASS", "交付物落在已取证路径上")
check("G27 标明来源是指纹第 4 段", w, "G27", "PASS", "config_fingerprint[3]")


# ================================================================ 正向：别名/大小写
print("\n" + "=" * 74)
print("正向路径：同一格式族的别名与大小写")
print("=" * 74)

w, t = run("G27 正向：YUV_422_888（同族）", lambda d: set_fmt_all(d, "YUV_422_888"))
check("G27 yuv 族别名 → PASS", w, "G27", "PASS", "交付物落在已取证路径上")

w, t = run("G27 正向：小写 yuv_420_888", lambda d: set_fmt_all(d, "yuv_420_888"))
check("G27 格式大小写不敏感 → PASS", w, "G27", "PASS", "交付物落在已取证路径上")


# ================================================================ 反向：不一致
print("\n" + "=" * 74)
print("反向路径：声明格式与证据格式不一致")
print("=" * 74)

w, t = run("G27 反向：声明 jpeg，证据是 YUV", None, args=("--capture-format", "jpeg"))
check("G27 不一致 → WARN 未被取证", w, "G27", "WARN", "交付采集格式未被取证")
check("G27 指出证据真实格式", w, "G27", "WARN", "YUV_420_888")
check("G27 指出采集路径变了", w, "G27", "WARN", "采集路径的输出格式变了")
check("G27 给出两条处置", w, "G27", "WARN", "二选一")

w, t = run("G27 反向：声明 raw，证据是 YUV", None, args=("--capture-format", "raw"))
check("G27 声明 raw → 也 WARN", w, "G27", "WARN", "交付采集格式未被取证")

# 反向的反向：防止写成"证据格式永远通过"的恒过判据
w, t = run("G27 反向：证据是 JPEG 而声明 yuv", lambda d: set_fmt_all(d, "JPEG"))
check("G27 证据 JPEG / 声明 yuv → WARN", w, "G27", "WARN", "交付采集格式未被取证")


# ================================================================ 反向：判不了 / 内部不一致
print("\n" + "=" * 74)
print("反向路径：判不了，以及证据内部自相矛盾")
print("=" * 74)


def drop_fmt_everywhere(d):
    """四份报告的 output_format 与指纹段落全部拿掉 —— 格式无从判定。"""
    def f(j):
        j["capture_config"].pop("output_format", None)
        j["capture_config"]["config_fingerprint"] = "no-format-in-this-string"
    for n in ROUND_REPORTS:
        patch(d, n, f)


w, t = run("G27 反向：报告里完全没有格式信息", drop_fmt_everywhere)
check("G27 取不到 → WARN（fail closed，不是 PASS）", w, "G27", "WARN", "判不了不等于没问题")


def one_report_other_format(d):
    """只把 r2 的格式改掉 —— 报告集内部自相矛盾。"""
    def f(j):
        j["capture_config"]["output_format"] = "JPEG"
    patch(d, "m0_locked_capture_r2.json", f)


w, t = run("G27 反向：某份报告记的格式不同", one_report_other_format)
check("G27 证据内部不一致 → WARN（不静默取第一个）", w, "G27", "WARN", "证据内部采集格式不一致")


# ================================================================ 门控互斥
print("\n" + "=" * 74)
print("路线门控：换回拍照时，闸门不能同时松开")
print("=" * 74)

# 【第 10 轮修正】这里原先直接依赖"磁盘上没有 m0_still_shot.json"——那是**环境巧合**：
# 第 10 轮用户真的跑了 ⑥，文件出现了，这条断言就失效了（判据没错）。
# 同一类坑第 8/9 轮已经踩过一次（见 round8 的 run() 注释）。现在显式删掉文件，
# 测的是"缺证据时闸门是否关着"，与磁盘状态无关。
w, t = run("门控互斥：交付=拍照，且 ⑥ 报告缺失",
           lambda d: os.remove(os.path.join(d, "m0_still_shot.json")),
           args=("--delivery", "still"))
check("G27 在拍照路线下不重复判定", w, "G27", "INFO", "本条不重复判定")
check("G27 明说交给 G26", w, "G27", "INFO", "见 G26")
check("G26 缺 ⑥ 报告 → 仍是不可判定", w, "G26", "INFO", "不等于没问题")
check_total("拍照路线下缺证据 → 总判定 INCOMPLETE", t, "INCOMPLETE")
check("拍照路线下 G18 不再阻塞交付", w, "G18", "INFO", "交付路径=**拍照")


# ================================================================ 汇总
print("\n" + "=" * 74)
bad = 0
for tag, ok, note in results:
    print("%-52s %s  %s" % (tag, "OK  " if ok else "FAIL", note))
    if not ok:
        bad += 1
print("=" * 74)
print("通过 %d / %d" % (len(results) - bad, len(results)))

# 防止"用例被整段注释掉/整行不再输出"时的静默失效：结果条数必须有下限
MIN_EXPECTED = 20
if len(results) < MIN_EXPECTED:
    print("!! 只跑了 %d 条断言（预期 >= %d）—— 有用例没被计入，判定不可信"
          % (len(results), MIN_EXPECTED))
    bad += 1
sys.exit(1 if bad else 0)
