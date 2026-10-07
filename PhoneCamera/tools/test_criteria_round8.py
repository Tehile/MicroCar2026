# -*- coding: utf-8 -*-
"""判据双向路径测试（第 8 轮新增/改动的判据）。

背景（为什么这一轮新增的判据特别需要双向测试）：
  交付方式明确为「回传使用拍照」，而此前**全部**证据都来自
  `TEMPLATE_MANUAL` + `setRepeatingRequest` 的连续流。于是本轮新增：
    G25 时间戳的参考行 + 单帧固有不确定度（把 skew 接到「时间戳准确」上）
    G26 拍照路径证据（交付走拍照时，证据必须来自拍照那条路径）
    G18 按交付路径门控（拍照下帧率上限不构成约束 -> 降级 INFO）
    G23 采集路径改用 (id, rev) 成对表示；skew 段改为**不做归属判定**
  这些都是"把闸门装窄"的改动。**只验 PASS 路径会写出恒过判据；
  只验 FAIL 路径会写出恒报错判据。**所以每条判据的三种出口都要走到。

本轮还专门覆盖了两个**已发生过的静默 bug**（回归测试）：
  · lock_stability 的 values 是字符串 —— 直接 isinstance 过滤会静默丢空值
  · 不可判定时返回 (None, None) 而不是 None —— 元组恒为真，会崩在 f-string 里
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
STILL = "m0_still_shot.json"
WATCH = ("G18", "G23", "G25", "G26")

# 与 App 端 LockedCaptureProbe 的默认值保持一致；测试自己造报告，不依赖磁盘数据
BASE_SKEW = 9212928
CROP_FULL = {"left": 0, "top": 0, "width": 4096, "height": 3072}


def load(name):
    with open(os.path.join(SRC, name), encoding="utf-8") as f:
        return json.load(f)


def run(tag, mutate=None, args=(), delivery="still"):
    """把 m0_out 复制到临时目录，施加 mutate，跑评估并抽出关心的判据行。

    **第 9 轮改动：交付路线改为显式传入（默认 still）。**
    本文件里一大批 G26/G18 用例原先依赖「`--delivery` 的**默认值**是 still」——
    而第 9 轮 D24 把默认值改成了 stream，那些用例会**集体失效**。
    这属于「断言依赖环境巧合」：默认值是产品决定，不是判据语义。
    现在路线作为显式参数传进去，测的是**门控本身**；默认值由 round9 单独断言。
    （调用方若在 args 里再传一次 `--delivery`，argparse 取最后一个 ⇒ 以 args 为准。）
    """
    d = tempfile.mkdtemp(prefix="m0t8_")
    try:
        for n in os.listdir(SRC):
            if n.endswith(".json") and not n.startswith("_"):
                shutil.copy(os.path.join(SRC, n), os.path.join(d, n))
        if mutate:
            mutate(d)
        argv = [PY, SCRIPT, d, "--delivery", delivery] + list(args)
        r = subprocess.run(argv, capture_output=True,
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
                                    (v[1] if v else "")[:150]))
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


# ---------------------------------------------------------------- 造一份 ⑥ 报告
def make_trial(tid, fmt, tmpl, size, crop, skew, fp, *, n=8,
               start=1_000_000_000_000, step=500_000_000,
               latency=45_000_000, req_ahead=30_000_000,
               lag_negative=0, adv_diff_ms=0.5, lat_spread_ms=2.0,
               edge="OFF", nr="OFF", zsl=False, exif_digits=3,
               with_subsec=None, out_w=None, out_h=None):
    out_w = out_w or size["w"]
    out_h = out_h or size["h"]
    frames = []
    for i in range(n):
        st = start + i * step
        req = st - req_ahead
        if i < lag_negative:
            # 时间戳早于下发 = 交出的是零快门延迟缓冲里的旧帧
            req = st + req_ahead
        frames.append({
            "frame_number": 100 + i,
            "frame_index": i,
            "is_warmup": i == 0,
            "sensor_timestamp_ns": st,
            "callback_elapsed_ns": st + latency,
            "wall_clock_ms": 1790000000000 + i * 500,
            "callback_latency_ns": latency,
            "request_sent_elapsed_ns": req,
            "request_to_result_ns": st + latency - req,
            "sensor_ts_minus_request_ns": st - req,
            "control_enable_zsl": zsl,
            "rolling_shutter_skew_ns": skew,
            "sensor_exposure_time_ns": 10_000_000,
            "sensor_sensitivity_iso": 200,
            "sensor_frame_duration_ns": 33_287_995,
            "sensor_pixel_mode": 0,
            "lens_focus_distance_diopter": 1.001001,
            "zoom_ratio": 1.0,
            "active_physical_id": "2",
            "ois_mode": "OFF", "video_stab_mode": "OFF",
            "edge_mode": edge, "noise_reduction_mode": nr,
            "aberration_mode": "OFF", "distortion_correction_mode": "OFF",
            "crop_region": dict(crop),
            "jpeg": ({"bytes": 4_500_000,
                      "datetime_original": "2026:09:24 16:20:00",
                      "subsec_time_original": "123" if exif_digits else None,
                      "offset_time_original": "+08:00",
                      "datetime": "2026:09:24 16:20:00",
                      "subsec_digits": exif_digits,
                      "has_subsec": bool(exif_digits)}
                     if fmt == "JPEG" else None),
        })
    locks = {}
    for k, v in (("lens_focus_distance_diopter", "1.001001"), ("zoom_ratio", "1.0"),
                 ("ois_mode", "OFF"), ("video_stab_mode", "OFF"),
                 ("sensor_exposure_time_ns", "10000000"),
                 ("sensor_sensitivity_iso", "200"), ("sensor_pixel_mode", "0"),
                 ("sensor_frame_duration_ns", "33287995"),
                 ("rolling_shutter_skew_ns", str(skew)),
                 ("distortion_correction_mode", "OFF"),
                 ("edge_mode", edge), ("noise_reduction_mode", nr),
                 ("active_physical_id", "2"),
                 ("control_enable_zsl", str(zsl).lower())):
        locks[k] = {"samples": n - 1, "distinct_count": 1, "values": [v], "locked": True}
    n_sub = n if with_subsec is None else with_subsec
    stats = {
        "total_frames": n, "warmup_frames": 1, "valid_frames": n - 1,
        "capture_duration_s": (n - 1) * step / 1e9,
        "interval_ns": {"n": n - 1, "mean": float(step), "min": step, "max": step,
                        "std": 0.0, "jitter_pp_percent": 0.0,
                        "note": "单张捕获模式：这里的间隔是应用下发节奏"},
        "lock_stability": locks,
        "rolling_shutter_skew_ns": {"n": n - 1, "value": skew, "min": skew,
                                    "max": skew, "distinct_count": 1},
        "shot_path": {"shots_requested": n, "shots_dispatched": n,
                      "serial_dispatch": True, "shot_interval_ms": 500,
                      "frame_duration_requested": tmpl != "TEMPLATE_STILL_CAPTURE"},
        # v1.8.1：图与结果的配对核验（见 evaluate_still_path 的检查 5）。
        # 基线是"一一配对"，测试用 _dig 把它改成缺图/多图。
        "image_pairing": {
            "pairing_key": "Image.timestamp == SENSOR_TIMESTAMP（同一时间基）",
            "shots_dispatched": n, "images_acquired": n,
            "images_matched": n, "images_missing": 0,
            "images_surplus": 0, "surplus_ts": [],
            "acquire_null": 0, "read_errors": 0,
        },
        "exposure_to_callback_ns": {
            "n": n - 1, "min": latency, "max": latency + int(lat_spread_ms * 1e6),
            "mean": latency + lat_spread_ms * 1e6 / 2, "std": 1e6,
            "spread": int(lat_spread_ms * 1e6), "spread_ms": lat_spread_ms},
        "sensor_vs_callback_advance": {
            "n": n - 2, "sensor_step_min": step, "sensor_step_max": step,
            "callback_step_min": step, "callback_step_max": step,
            "diff_min": -int(adv_diff_ms * 1e6), "diff_max": int(adv_diff_ms * 1e6),
            "spread_ms": 2 * adv_diff_ms, "max_abs_diff_ms": adv_diff_ms},
        "sensor_ts_minus_request": {
            "n": n, "min": -req_ahead if lag_negative else req_ahead,
            "max": req_ahead,
            "negative_count": lag_negative,
            "all_capture_after_request": lag_negative == 0},
        "states": {"af_state": ["INACTIVE"], "ae_state": ["INACTIVE"]},
        "lens_state_timeline": {"sequence_head": ["MOVING"], "moving_frame_indices": [0],
                                "moving_count": 1, "moving_after_warmup": 0,
                                "moving_all_within_warmup": True},
        "temperature_c": {"source": "battery", "n": n, "first": 33.0, "last": 33.0,
                          "min": 33.0, "max": 33.0, "delta": 0,
                          "sampling_duration_s": 4.0},
    }
    if fmt == "JPEG":
        stats["exif"] = {"n": n,
                         "subsec_digits_values": ([exif_digits] if exif_digits else [0]),
                         "with_subsec": n_sub,
                         "sample_datetime_original": "2026:09:24 16:20:00",
                         "sample_offset_time_original": "+08:00",
                         "first_bytes": 4_500_000}
    cfg = {
        "size_choice_note": "测试合成",
        "output_size": {"w": out_w, "h": out_h},
        "output_format": fmt,
        "template_name": tmpl,
        "single_shot": True,
        "shot_interval_ms": 500,
        "active_array": {"w": 4096, "h": 3072},
        "crop_region": dict(crop),
        "output_to_crop_scale_x": out_w / crop["width"],
        "output_to_crop_scale_y": out_h / crop["height"],
        "crop_shrunk_for_aspect": (crop["width"] != 4096 or crop["height"] != 3072),
        "forced_full_crop": False,
        "app_version": "m0probe-1.8.0",
        "capture_path_id": tid,
        "capture_path_rev": 1,
        "config_fingerprint": fp,
    }
    return {
        "report_kind": "m0_still_shot_trial",
        "generated_at_wall_ms": 1790237000000,
        "app_version": "m0probe-1.8.0",
        "capture_path_id": tid,
        "capture_path_rev": 1,
        "camera_id": "0",
        "capture_size": {"w": out_w, "h": out_h},
        "capture_config": cfg,
        "requested": {"focus_policy": "explicit_working_distance",
                      "focus_diopter": 1.0, "target_fps": 30,
                      "sensor_frame_duration_ns": None,
                      "frame_duration_requested": tmpl != "TEMPLATE_STILL_CAPTURE"},
        "applied": {"control_mode_off": True, "zsl_off_requested":
                    (True if tmpl == "TEMPLATE_STILL_CAPTURE" else None)},
        "params": {"focus_distance_m": 1.0, "target_fps": 30, "target_frames": n,
                   "warmup_frames": 1, "legacy_fps_range": False},
        "frame_count": n,
        "frames": frames,
        "stats": stats,
        "trial_id": tid,
        "label": tid,
    }


def make_still(**over):
    """按真实 ② 报告生成一份健康的 ⑥ 报告，再按 over 打补丁。"""
    base = load(BASE)
    bcfg = base.get("capture_config") or {}
    bfp = bcfg.get("config_fingerprint")
    bskew = base["stats"]["rolling_shutter_skew_ns"]["value"]
    yuv = make_trial("still_yuv_single", "YUV_420_888", "TEMPLATE_MANUAL",
                     {"w": 1920, "h": 1440}, CROP_FULL, bskew, bfp)
    jpg = make_trial("still_jpeg_template", "JPEG", "TEMPLATE_STILL_CAPTURE",
                     {"w": 4096, "h": 3072}, CROP_FULL, bskew,
                     "4096x3072|a4096x3072|c4096x3072@0,0|JPEG|w1 #deadbeef")
    rep = {
        "report_kind": "m0_still_shot",
        "generated_at_wall_ms": 1790237100000,
        "app_version": "m0probe-1.8.0",
        "capture_path_id_per_trial": True,
        "camera_id": "0",
        "shots_per_trial": 8,
        "warmup_shots": 1,
        "shot_interval_ms": 500,
        "trials": [yuv, jpg],
    }
    for k, v in (over or {}).items():
        if k.startswith("yuv__"):
            _dig(yuv, k[5:], v)
        elif k.startswith("jpg__"):
            _dig(jpg, k[5:], v)
        elif k.startswith("top__"):
            rep[k[5:]] = v
        else:
            rep[k] = v
    return rep


def _dig(obj, path, value):
    """按 'stats.exif.with_subsec' 这类路径写值。

    删除某个键有两种写法：整条路径以 '-' 开头，或**最后一段**以 '-' 开头
    （如 'stats.-image_pairing'）。后者更常用 —— 写测试时想删的通常是末级键。
    """
    delete = path.startswith("-")
    if delete:
        path = path[1:]
    parts = path.split(".")
    for p in parts[:-1]:
        obj = obj.setdefault(p, {}) if isinstance(obj, dict) else obj[p]
    last = parts[-1]
    if last.startswith("-"):
        delete, last = True, last[1:]
    if delete:
        obj.pop(last, None)
    else:
        obj[last] = value


def write_still(d, **over):
    with open(os.path.join(d, STILL), "w", encoding="utf-8") as f:
        json.dump(make_still(**over), f, ensure_ascii=False)


# ================================================================ G26
w = run("G26 基线：健康的 ⑥ 报告", lambda d: write_still(d))
check("G26 健康 → PASS", w, "G26", "PASS", "拍照路径的时间戳语义与连拍一致且可用")
check("G26 基线报告 ZSL 未被误报", w, "G26", "PASS", absent="零快门延迟缓冲里的")


w = run("G26：⑥ 报告缺失 → 不可判定",
        lambda d: os.remove(os.path.join(d, STILL)))
check("G26 缺报告 → INFO 未验证", w, "G26", "INFO", "不等于没问题")
check("G26 缺报告 → 总判定 INCOMPLETE", w, "G26", "INFO")


def still_remove(d):
    os.remove(os.path.join(d, STILL))


w = run("G26：⑥ 报告存在但为空 trials", lambda d: write_still(d, trials=[]))
check("G26 空报告 → INFO 未验证", w, "G26", "INFO", "没有 trials")


def no_jpg(d):
    def f(j):
        j["trials"] = [t for t in j["trials"] if t["trial_id"] != "still_jpeg_template"]
    write_still(d)
    patch(d, STILL, f)


w = run("G26：缺交付档（只有对照档）", no_jpg)
check("G26 缺交付档 → INFO 未验证", w, "G26", "INFO", "未验证")


w = run("G26：交付路径=连续流（不适用）", lambda d: write_still(d),
        args=("--delivery", "stream"))
check("G26 stream → INFO 不适用", w, "G26", "INFO", "本项不适用")


w = run("G26 FAIL：时间戳早于下发（ZSL 旧帧）",
        lambda d: write_still(d, **{"jpg__stats.sensor_ts_minus_request.negative_count": 2,
                                    "jpg__stats.sensor_ts_minus_request.all_capture_after_request": False}))
check("G26 ZSL 旧帧 → FAIL", w, "G26", "FAIL", "零快门延迟缓冲里的")


w = run("G26 FAIL：降噪被改写为 ZERO_SHUTTER_LAG",
        lambda d: write_still(d, **{"jpg__stats.lock_stability.noise_reduction_mode.values":
                                    ["ZERO_SHUTTER_LAG"],
                                    "jpg__stats.lock_stability.noise_reduction_mode.distinct_count": 1}))
check("G26 多帧合成 → FAIL", w, "G26", "FAIL", "多帧合成")


w = run("G26 FAIL：边缘增强被改写为 HIGH_QUALITY",
        lambda d: write_still(d, **{"jpg__stats.lock_stability.edge_mode.values": ["HIGH_QUALITY"]}))
check("G26 edge 被改写 → FAIL", w, "G26", "FAIL", "多帧合成")


w = run("G26 WARN：EXIF 无亚秒",
        lambda d: write_still(d, **{"jpg__stats.exif.with_subsec": 0,
                                    "jpg__stats.exif.subsec_digits_values": [0]}))
check("G26 无亚秒 → WARN", w, "G26", "WARN", "不得**用 EXIF 作时间源")


w = run("G26 WARN：EXIF 亚秒只有 1 位",
        lambda d: write_still(d, **{"jpg__stats.exif.subsec_digits_values": [1]}))
check("G26 亚秒 1 位 → WARN", w, "G26", "WARN", "达不到本方案需求")


w = run("G26 WARN：对照档 skew 与②不同",
        lambda d: write_still(d, **{"yuv__stats.rolling_shutter_skew_ns.value": 6_909_696}))
check("G26 skew 不一致 → WARN", w, "G26", "WARN", "不可外推")


# 【第 10 轮改语义】原先这条断言"时间戳推进与回调推进不一致 ⇒ WARN，逐张对应关系
# 不可靠"。那个推理是错的（回调抖动只说明"通知来得不齐"，与"时间戳属于哪一帧"
# 是两件事），实测数据正好卡在错误的阈值上。现在这个量降级为注记，
# 帧序一致性改由 sensor_step_min 与 image_pairing 两条承担。
w = run("G26：回调推进抖动很大（旧判据的误报场景）",
        lambda d: write_still(d, **{"jpg__stats.sensor_vs_callback_advance.max_abs_diff_ms": 80.0}))
check("G26 回调抖动大 → 不再判 WARN", w, "G26", "PASS")
check("G26 回调抖动 → 仍报出数值", w, "G26", "PASS", "回调到达抖动 ±80.0 ms")
check("G26 回调抖动 → 不再下'帧序不一致'的结论", w, "G26", "PASS", absent="逐张对应关系不可靠")

w = run("G26 FAIL：逐张时间戳不递增（混入旧帧）",
        lambda d: write_still(d, **{"jpg__stats.sensor_vs_callback_advance.sensor_step_min": 0}))
check("G26 时间戳不递增 → FAIL", w, "G26", "FAIL", "不是严格递增")

w = run("G26 WARN：交付档有结果没配到图（丢图）",
        lambda d: write_still(d, **{"jpg__stats.image_pairing.images_missing": 2,
                                    "jpg__stats.image_pairing.images_matched": 6}))
check("G26 丢图 → WARN", w, "G26", "WARN", "没有配对到图")
check("G26 丢图 → 给出定位线索", w, "G26", "WARN", "acquire_null>0 是取图竞态")

w = run("G26 WARN：有图没被任何结果认领（配错）",
        lambda d: write_still(d, **{"jpg__stats.image_pairing.images_surplus": 1}))
check("G26 多余图 → WARN", w, "G26", "WARN", "配错比少图更严重")

w = run("G26：报告没有 image_pairing 段（≤1.8.0）",
        lambda d: write_still(d, **{"jpg__stats.-image_pairing": None}))
check("G26 无配对段 → WARN 未核验", w, "G26", "WARN", "同源」这一条**未核验**")

w = run("G26：摘要说配对上了但逐张没有字节（汇总与明细打架）",
        lambda d: (write_still(d), patch(d, STILL, lambda j: j["trials"][1]["frames"][3].update(
            {"jpeg": None})))[0])
check("G26 配对上了却没字节 → WARN", w, "G26", "WARN", "配对上了却没有字节")

w = run("G26：无配对段 + 有效张缺图像字节（1.8.0 实测到的那种）",
        lambda d: (write_still(d, **{"jpg__stats.-image_pairing": None}),
                   patch(d, STILL, lambda j: j["trials"][1]["frames"][3].update(
                       {"jpeg": None})))[0])
check("G26 缺图像字节 → WARN 丢图风险", w, "G26", "WARN", "没有图像字节")

w = run("G26 WARN：交付档 crop.top≠0（静默偏置）",
        lambda d: write_still(d, **{"jpg__capture_config.crop_region": {"left": 0, "top": 384,
                                                                       "width": 4096, "height": 3072}}))
check("G26 交付档 crop.top≠0 → WARN", w, "G26", "WARN", "静默偏置")

w = run("G26 注记：指纹只差预热帧数",
        lambda d: write_still(d, **{"yuv__capture_config.config_fingerprint":
                                    "1920x1440|a4096x3072|c4096x3072@0,0|YUV_420_888|w1 #5ba6b49a"}))
check("G26 只差预热帧数 → 不判 WARN", w, "G26", "PASS")
check("G26 只差预热帧数 → 逐段点名", w, "G26", "PASS", "只差软段")
check("G26 只差预热帧数 → 明说仍可归因", w, "G26", "PASS", "仍可归因于调度方式")

w = run("G26 WARN：指纹差在硬段（输出格式）",
        lambda d: write_still(d, **{"yuv__capture_config.config_fingerprint":
                                    "1920x1440|a4096x3072|c4096x3072@0,0|JPEG|w5 #5ba6b49a"}))
check("G26 硬段不同 → WARN", w, "G26", "WARN", "结论无法归因")
check("G26 硬段不同 → 点名段名", w, "G26", "WARN", "输出格式")


w = run("G26 WARN：曝光→回调延迟抖动过大",
        lambda d: write_still(d, **{"jpg__stats.exposure_to_callback_ns.spread_ms": 250.0}))
check("G26 延迟抖动 → WARN", w, "G26", "WARN", "无法用常数反推曝光时刻")


w = run("G26 WARN：对照档配置指纹与②不一致",
        lambda d: write_still(d, **{"yuv__capture_config.config_fingerprint": "x"}))
check("G26 指纹不一致 → WARN", w, "G26", "WARN", "结论无法归因")


# ================================================================ G25
w = run("G25 基线：crop.top=0（交付档）", None)
check("G25 crop.top=0 → PASS", w, "G25", "PASS", "参考行偏置 0 ns")
check("G25 报出单帧固有不确定度", w, "G25", "PASS", "不能靠多张平均消掉")
check("G25 报出不确定度数值", w, "G25", "PASS", "9.213 ms")


w = run("G25：crop.top=384（16:9 收窄档）",
        lambda d: patch(d, BASE, lambda j: j["capture_config"]["crop_region"].update(
            {"top": 384, "height": 2304})))
check("G25 crop.top≠0 → WARN 须补偿", w, "G25", "WARN", "须显式补偿")
check("G25 crop.top≠0 → 报出偏置数值", w, "G25", "WARN", "1.1516 ms")


w = run("G25：crop.top 缺失",
        lambda d: patch(d, BASE, lambda j: j["capture_config"]["crop_region"].pop("top", None)))
check("G25 缺 crop.top → WARN 判不了", w, "G25", "WARN", "参考行偏置无法判定")


def skew_only_in_lock_stability(d):
    """回归测试：skew 只有 lock_stability.values（**字符串**）这一个来源。

    这正是那个静默 bug 的现场：`isinstance(v, (int, float))` 会把 "9212928"
    全部过滤掉，判据于是报"取不到"。修好之后它必须取到值、并且如实说明
    "原始类型 str，已显式转换"。
    """
    def f(j):
        j["stats"].pop("rolling_shutter_skew_ns", None)
        j["stats"]["lock_stability"]["rolling_shutter_skew_ns"]["values"] = ["9212928"]
    patch(d, BASE, f)


w = run("G25 回归：skew 只存在于 lock_stability（字符串）", skew_only_in_lock_stability)
check("G25 字符串 skew 仍能取到 → PASS", w, "G25", "PASS", "参考行偏置 0 ns")


def skew_absent(d):
    def f(j):
        j["stats"].pop("rolling_shutter_skew_ns", None)
        j["stats"]["lock_stability"].pop("rolling_shutter_skew_ns", None)
    patch(d, BASE, f)


w = run("G25：完全没有 skew", skew_absent)
check("G25 无 skew → WARN", w, "G25", "WARN", "取不到卷帘读出时间")


# ================================================================ G23 / G18
w = run("G23 回归：②③ 只有一种几何 → 不做归属判定", None)
check("G23 单几何 → 不宣布归属", w, "G23", "PASS", absent="-> 恒定的是「")
check("G23 单几何 → 明说判不了", w, "G23", "PASS", "本段不做 skew 归属判定")


def same_geo_two_skews(d):
    def f(j):
        j["stats"]["rolling_shutter_skew_ns"]["value"] = 6_909_696
        j["stats"]["lock_stability"]["rolling_shutter_skew_ns"]["values"] = ["6909696"]
    patch(d, "m0_locked_capture_r2.json", f)


w = run("G23：同一几何配置内部 skew 不一致", same_geo_two_skews)
check("G23 同几何异 skew → WARN", w, "G23", "WARN", "同一几何配置内部 skew 不一致")


w = run("G18 门控：交付=拍照 → INFO", None, delivery="still")
check("G18 拍照 → INFO 不阻塞", w, "G18", "INFO", "交付路径=**拍照")
check("G18 拍照 → 明说声明来源", w, "G18", "INFO", "调用方声明")


w = run("G18 门控：交付=连续流 → WARN", None, delivery="stream")
check("G18 连续流 → WARN", w, "G18", "WARN", "维持原判")


# ================================================================ 汇总
print("\n" + "=" * 74)
bad = 0
for tag, ok, note in results:
    print("%-48s %s  %s" % (tag, "OK  " if ok else "FAIL", note))
    if not ok:
        bad += 1
print("=" * 74)
print("通过 %d / %d" % (len(results) - bad, len(results)))
sys.exit(1 if bad else 0)
