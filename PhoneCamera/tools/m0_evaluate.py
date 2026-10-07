#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
M0 机型能力评估

读入 M0 探测器导出的两个 JSON，按准入判据输出结论表与整改建议。

用法：
    python m0_evaluate.py <m0_capability.json> [m0_locked_capture.json]
    python m0_evaluate.py <包含上述文件的目录>     # 目录模式会自动带齐全部报告
    python m0_evaluate.py --pull                   # 自动 adb pull 到本地再评估

目录模式下会一并消费：m0_capability.json、m0_locked_capture.json、
m0_rate_sweep.json、m0_size_sweep.json、m0_still_shot.json、
m0_repeat.json 或 m0_locked_capture_r*.json（③ 的逐轮产出）。

交付路径：`--delivery still|stream`（默认 still = 拍照/按需触发）。
它只决定 G18/G26 是否阻塞交付 —— 不改变任何测量结论。为什么需要：
"帧率可控"与"时间戳在拍照路径上可用"这两件事的**权重**由交付方式决定，
而交付方式不可能从报告里测出来，只能由调用方声明。

退出码：0 = PASS（可有 WARN）；1 = 存在 FAIL；2 = 关键判据未验证（INCOMPLETE）。

判据设计原则：
  - 能从逐帧原始序列重算的，一律重算，不依赖报告里的汇总结论。
    这样新判据能立刻跑在历史数据上，验证它抓不抓得住问题。
  - "没测"和"没异常"必须分开。关键判据未跑 -> INCOMPLETE，不得据此判定合格。
  - 核心要求必须被判据覆盖。若某条要求是项目的核心（如"内参不变"），
    那就必须有一条判据消费它的证据文件，缺证据即 INCOMPLETE —— 否则口头上"核心"
    但脚本里没有对应判据，报告永远会给 PASS。G21 就是这么补上的。
  - **证据与路径绑定**：一条判据的结论只对自己那条采集路径成立。
    G26 存在的理由是：交付路径（拍照）与既有全部证据（连拍）不是同一条路径，
    所以"整体 PASS"这四个字在写进结论之前，必须先问"PASS 覆盖的是哪条路径"。
"""

import argparse
import glob
import json
import math
import os
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
INFO = "INFO"
INCOMPLETE = "INCOMPLETE"

CAP_FILE = "m0_capability.json"
LC_FILE = "m0_locked_capture.json"
SWEEP_FILE = "m0_rate_sweep.json"
SIZE_SWEEP_FILE = "m0_size_sweep.json"      # ⑤ 尺寸扫描（定档依据）
STILL_FILE = "m0_still_shot.json"           # ⑥ 拍照路径（单张捕获）
REPEAT_FILE = "m0_repeat.json"          # ③ 的汇总（新 App 会写；旧 App 只写 r1..r3）
REPEAT_GLOB = "m0_locked_capture_r*.json"  # ③ 的逐轮产出，作为兜底来源

# 判据阈值
JITTER_OK_PP = 5.0         # 帧间隔抖动 <= 5% 视为良好
JITTER_WARN_PP = 15.0      # <= 15% 可用但需注意
TEMP_MIN_DURATION_S = 20.0  # 采样短于此秒数时，温升判据不产生结论
WARMUP_TOLERANCE = 5       # 连续前缀式的镜头收敛帧数上限
REPEAT_FOCUS_OK_PP = 0.5   # 跨会话焦点回读值的相对极差上限
RATE_CLUSTER_TOL_PP = 1.0  # 帧间隔"同档"聚类容差；小于它视为同一档
FPS_MISS_TOL_PP = 5.0      # 请求帧率与实测帧率的相对偏差上限，超出即视为该档不可达
TS_JITTER_OK_PPM = 10.0    # 时间戳残差 std 上限（ppm）。注意：这是**残差**不是"噪声"，
                           # 实测该残差是确定性周期拍频，参考用，不作主判据
TS_CONSIST_OK_PPM = 100.0  # 时间戳实测间隔与回读帧时长的相对一致性上限（ppm）
TS_DRIFT_OK_PPM = 20.0     # 时间戳在整段窗口内的累积漂移上限（ppm）——"时间戳准确"的主判据
ROUND_GAP_RATIO = 1.5      # 逐轮帧间隔超过名义值的该倍数即视为丢帧/掉拍
ROUND_MIN_SESSIONS = 3     # 跨会话重复性至少要有几轮才算数
REPORT_BATCH_MAX_S = 3600.0  # 报告集内各报告的时间跨度上限（秒）；超出即视为跨批次
CAPTURE_REV_MAP_FILE = "capture_path_versions.json"  # 版本号 -> 采集路径 的登记表

# ---- 第 8 轮新增：拍照路径（⑥）的阈值 ----
# ⚠ 第 10 轮删掉了 `STILL_ADVANCE_TOL_MS`（曾=5.0 ms，判"时间戳推进 vs 回调推进"
#   的最大偏差）。**那条判据是错的**：它从"回调到达的抖动"推出"时间戳与真实帧序
#   不一致"。回调到达时刻与曝光时刻本来就差一个固定管线延迟，延迟抖动只说明
#   "通知来得不齐"，与"时间戳属于哪一帧"是两件事。实测数据正好卡在这个错误的
#   阈值上（±10.9 ms > 5 ms），于是给出了一条站不住的结论。
#   现在：帧序一致性由两条**可判定**的检查承担 ——
#     · `sensor_step_min > 0`（逐张时间戳严格递增）
#     · `image_pairing`（图的 `Image.timestamp` 与结果的 `SENSOR_TIMESTAMP` 配对）
#   回调抖动本身降级为注记，用途改为回答"若拿回调时刻当时间源会错多少"。
# "时间戳 − 下发时刻"的下限：为负说明拿到的是缓冲里的旧帧（ZSL），
# 留一点余量覆盖"下发到真正曝光"在这一档下的最短周转。
STILL_TS_MIN_MS = -20.0
# 曝光→回调 延迟的极差上限：延迟本身多大不重要（可标定掉），
# **抖动**才重要（它决定时间戳能否用一个常数反推）。
STILL_LATENCY_SPREAD_OK_MS = 30.0
STILL_LATENCY_SPREAD_WARN_MS = 100.0
# EXIF 亚秒位数下限。datetime_original 只有秒级时，用它当时间戳的误差是 ±0.5 s。
EXIF_SUBSEC_MIN_DIGITS = 3

# ---- 第 9 轮新增：交付采集格式（G27，对应 R24）----
# 采集路径是三元组 {采集模板, 调度方式, HAL 输出格式}。前两项由 (capture_path_id, rev) 表示，
# 而**输出格式此前没有任何判据在看** —— 这正是 R22 那类错误（换了路径却不重新取证）下沉一层的形态：
# 交付时若让 HAL 直接出 JPEG 而证据是 YUV，采集路径就变了，而既有 PASS 一条也覆盖不到交付物。
# 而且 G23 只比较"报告与报告"，**不会比较"报告与交付配置"** ⇒ 它不会主动报警。
# 注意：**由 YUV 再软件编码成 JPEG 不算换路径**（编码发生在采集之后，不碰时间戳）。
CAPTURE_FORMAT_ALIASES = {
    "yuv": ("YUV_420_888", "YUV_422_888", "YUV_444_888", "YUV"),
    "jpeg": ("JPEG",),
    "raw": ("RAW_SENSOR", "RAW10", "RAW12", "RAW_PRIVATE"),
}

# 采集路径登记表（由 main 载入）。用途见 _resolve_capture_path：
# **判断"采集行为是否相同"要问"采集路径"，不能问"版本号"** —— 版本号会因为
# 纯报告字段、注释、UI 改动而变，那类改动不会改变采集行为，却会永久性地让 ③ 作废。
# 实测踩过：第 6 轮我让用户"重跑 ②"，重跑后 ② 变成新版本、③ 三轮留在旧版本，
# G23 于是要求"重跑 ③" —— 而两者的采集路径其实完全相同（只有指纹串与新增报告
# 字段的差别）。这条规则会无限循环下去，每改一行代码就要重跑 ③。
#
# 第 8 轮再补一层：路径必须用 **(id, rev) 这一对**表示，不能只用 rev。
# 理由：v1.8 给 App 加了第六条采集路径（拍照）。若 rev 仍是"App 全局计数器"，
# 加一条新路径就会让连拍路径的证据一起作废 —— 同一个返工循环换个形式再犯。
CAPTURE_REV_MAP = {}


def _resolve_capture_path(report):
    """取一份报告的**采集路径 (id, rev)**。返回 ((id, rev), source) 或 (None, 原因)。

    ⚠ 不可判定时返回的**必须**是 `None`，不能是 `(None, None)` ——
    元组是非空对象、恒为真，`if p else "未登记"` 这类写法会走进真分支、
    然后对 None 取下标而崩掉（实测踩过：一行 f-string 直接把整个脚本打崩，
    于是 G23 整行消失，而"整行消失"看起来和"判据没跑"一模一样）。
    **"判不了"要用一个明确的哨兵值表示，不要用"字段都为空的结构"。**
    """
    rep = report or {}
    cfg = rep.get("capture_config") or {}
    pid = rep.get("capture_path_id", cfg.get("capture_path_id"))
    rev = rep.get("capture_path_rev", cfg.get("capture_path_rev"))
    if isinstance(pid, str) and pid and isinstance(rev, int):
        return (pid, rev), "报告自带"
    ver = rep.get("app_version")
    ent = (CAPTURE_REV_MAP.get("versions") or {}).get(ver) if ver else None
    if isinstance(ent, dict):
        # 2a) 按路径登记的版本（1.8.0 起用这个形状）
        paths = ent.get("paths")
        if isinstance(paths, dict) and isinstance(pid, str) and pid in paths \
                and isinstance(paths[pid], int):
            return (pid, paths[pid]), f"登记表（{ver} / {pid}）"
        # 2b) 单一路径的旧版本（1.5.0~1.7.0）
        eid, erev = ent.get("capture_path_id"), ent.get("capture_path_rev")
        if isinstance(eid, str) and isinstance(erev, int):
            return (eid, erev), f"登记表（{ver}）"
    return None, (f"版本 {ver} 未登记或未登记该路径 id" if ver
                  else "报告未记录版本且无该字段")

# 注意：这里刻意**没有**"帧时长相对偏差"的阈值。
# 早期版本用过"实测帧间隔偏离请求值 ≤5% 即算生效"，结果证明是不可证伪的：
# 请求 83.33 ms 与请求 33.333 ms 得到同一个回读值 33.288 ms，
# 而后者偏差仅 0.14%，会被判 PASS。G15 因此改为"回读值是否逐字等于请求值"。


def script_dir():
    """脚本自身所在目录 —— 登记表等工具自身的数据要按这里定位，
    不能按调用者的当前目录（`--pull` 时 cwd 是项目根，而工具放在 tools/）。"""
    return os.path.dirname(os.path.abspath(__file__))


def load_json(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def pick_camera(cap, camera_id=None):
    """默认取后置、且带 active_array_size 的那颗。"""
    cams = cap.get("cameras", [])
    if not cams:
        return None
    if camera_id is not None:
        for c in cams:
            if str(c.get("id")) == str(camera_id):
                return c
        return None
    for c in cams:
        if c.get("lens_facing") == "BACK" and (c.get("sensor") or {}).get("active_array_size"):
            return c
    for c in cams:
        if c.get("lens_facing") == "BACK":
            return c
    return cams[0]


class Result:
    def __init__(self):
        self.rows = []
        self.unverified = []   # 未能验证的关键判据编号

    def add(self, gid, level, title, detail=""):
        self.rows.append((gid, level, title, detail))

    def add_unverified(self, gid, title, detail=""):
        """关键判据未能验证：记一行 INFO，并把整体判定降级为 INCOMPLETE。

        存在的意义：参数锁定稳定性这类判据，只有真跑过连拍才知道。
        若因为没跑就默认"无异常"，会得出"机型合格"的错误结论。
        """
        self.rows.append((gid, INFO, title, detail))
        self.unverified.append(gid)

    def count(self, level):
        return sum(1 for r in self.rows if r[1] == level)

    def verdict(self):
        if self.count(FAIL) > 0:
            return FAIL
        if self.unverified:
            return INCOMPLETE
        if self.count(WARN) > 0:
            return WARN
        return PASS


# ---------------------------------------------------------------- 静态能力

def evaluate_capability(cam, res):
    focus = cam.get("focus") or {}
    timing = cam.get("timing") or {}
    dist = cam.get("distortion") or {}
    zoom = cam.get("zoom") or {}
    stab = cam.get("stabilization") or {}
    lens = cam.get("lens") or {}
    sizes = cam.get("output_sizes") or {}

    # G1 手动对焦距离
    minf = focus.get("min_focus_distance_diopter", 0) or 0
    manual = bool(focus.get("manual_focus_supported"))
    if manual and minf > 0:
        res.add("G1", PASS, "支持手动对焦距离锁定",
                f"min_focus={minf:.4f} diopt ({fmt1m(focus.get('min_focus_distance_meters'))}), "
                f"hyperfocal={focus.get('hyperfocal_distance_diopter')} diopt")
    else:
        res.add("G1", FAIL, "不支持手动对焦距离锁定",
                f"min_focus={minf} diopt, af_modes={focus.get('af_available_modes')} "
                "-> 该机型无法在标定后冻结对焦，内参不可复现。必须换机型。")

    # G2 对焦距离标定等级
    cal = focus.get("focus_distance_calibration", "UNKNOWN")
    if cal == "CALIBRATED":
        res.add("G2", PASS, "对焦距离为物理标定值", "LENS_FOCUS_DISTANCE 与实际物距一致")
    elif cal == "APPROXIMATE":
        res.add("G2", PASS, "对焦距离为近似标定值",
                "绝对值不可全信，但同一 diopt 值的重复性通常可用")
    elif cal == "UNCALIBRATED":
        res.add("G2", WARN, "对焦距离未标定",
                "LENS_FOCUS_DISTANCE 只是电机位置量，不是真实物距。"
                "锁定重复性仍可用，但不要把它当物理距离用")
    else:
        res.add("G2", WARN, "无法判定对焦距离标定等级", f"值={cal}")

    # G3 时间戳源
    ts = timing.get("timestamp_source", "UNKNOWN")
    if ts == "REALTIME":
        res.add("G3", PASS, "时间戳源 REALTIME", "帧间时间关系可用于运动建模")
    else:
        res.add("G3", FAIL, "时间戳源非 REALTIME",
                f"值={ts} -> 帧时间戳与真实时间不同源，帧间隔与运动补偿不可信")

    # 卷帘读出时间不是 CameraCharacteristics 的特征，只能逐帧从 CaptureResult 读 -> 见 G14

    # G5 变焦锁定能力
    zr = zoom.get("zoom_ratio_range")
    if zr and zr[0] <= 1.0 <= zr[1]:
        res.add("G5", PASS, "支持 CONTROL_ZOOM_RATIO 且含 1.0",
                f"range={zr} -> 可显式锁死变焦，避免数码变焦漂移")
        if zr[0] < 1.0:
            res.add("I8", INFO, "逻辑摄像头含超广角",
                    f"zoom 区间下界={zr[0]} < 1.0 -> 该逻辑摄像头由多颗物理镜头融合，"
                    "zoom < 1.0 会切镜头并跳变内参；必须确保工作档位稳定落在主摄上")
    else:
        res.add("G5", WARN, "无 CONTROL_ZOOM_RATIO 或区间不含 1.0",
                f"range={zr}, max_digital_zoom={zoom.get('max_digital_zoom')} "
                "-> 只能靠 SCALER_CROP_REGION 锁全画幅，可靠性稍弱（需 Android 11+）")

    # G6 畸变矫正可关（决定标定模型）
    dmodes = dist.get("distortion_correction_available_modes") or []
    if "OFF" in dmodes:
        res.add("G6", PASS, "可关闭畸变矫正",
                f"modes={dmodes} -> 能拿到未矫正的原始几何，标定模型可控")
    else:
        res.add("G6", WARN, "无法确认畸变矫正状态",
                f"modes={dmodes} -> 拿到的画面可能已被硬件做过几何矫正，"
                "标定模型必须按“已被矫正”设计，M1 需实测直线畸变")

    # G7 时间戳/手动能力位
    miss = []
    if not cam.get("manual_sensor_supported"):
        miss.append("MANUAL_SENSOR")
    if not cam.get("burst_capture_supported"):
        miss.append("BURST_CAPTURE")
    if miss:
        res.add("G7", FAIL if "MANUAL_SENSOR" in miss else WARN,
                "缺少关键能力位", f"缺失={miss} -> 无法做手动参数控制/高速连拍")
    else:
        res.add("G7", PASS, "MANUAL_SENSOR + BURST_CAPTURE 齐备", "")

    # G8 RAW 能力（标定档）
    raw_cnt = (sizes.get("RAW_SENSOR") or {}).get("count", 0)
    if cam.get("raw_supported") and raw_cnt:
        res.add("G8", PASS, "支持 RAW 输出",
                f"RAW_SENSOR 尺寸数={raw_cnt}, 最大={(sizes.get('RAW_SENSOR') or {}).get('largest')}")
    else:
        res.add("G8", WARN, "不支持 RAW 输出",
                f"raw_supported={cam.get('raw_supported')}, RAW 尺寸数={raw_cnt} "
                "-> 标定档只能用 YUV/高质量 JPEG，亚像素精度上限略降")

    # 参考信息
    res.add("I1", INFO, "硬件等级", f"{cam.get('supported_hardware_level')} "
            f"(建议 FULL 或 LEVEL_3)")
    res.add("I2", INFO, "输出尺寸",
            f"YUV 最大={(sizes.get('YUV_420_888') or {}).get('largest')}, "
            f"JPEG 最大={(sizes.get('JPEG') or {}).get('largest')}")
    res.add("I3", INFO, "OIS/EIS 可关模式",
            f"OIS={lens.get('optical_stabilization_available_modes')}, "
            f"EIS={stab.get('video_stabilization_available_modes')}")


def fmt1m(v):
    if v is None:
        return "n/a"
    try:
        return f"{float(v):.3f} m"
    except Exception:
        return str(v)


# ---------------------------------------------------------------- 光学派生量

def circle_of_confusion_mm(cam):
    """用像元尺寸作容许弥散圆。HAL 报的超焦距就是这么算出来的，口径一致。"""
    sensor = cam.get("sensor") or {}
    phys = sensor.get("physical_size_mm") or {}
    act = sensor.get("active_array_size") or {}
    try:
        return float(phys["w"]) / float(act["w"])
    except Exception:
        return 0.006


def depth_of_field_m(cam, focus_m):
    """薄透镜近似的景深。f 相对 s 很小，简化式误差可忽略。"""
    lens = cam.get("lens") or {}
    try:
        f_mm = float(lens["focal_lengths_mm"][0])
        N = float(lens["apertures"][0])
    except Exception:
        return None
    coc = circle_of_confusion_mm(cam)
    if focus_m <= 0:
        return None
    H_mm = f_mm * f_mm / (N * coc) + f_mm
    s_mm = focus_m * 1000.0
    near = H_mm * s_mm / (H_mm + s_mm)
    far = float("inf") if s_mm >= H_mm else H_mm * s_mm / (H_mm - s_mm)
    return near / 1000.0, (far if math.isinf(far) else far / 1000.0), H_mm / 1000.0


# ---------------------------------------------------------------- 逐帧派生

def frame_series(frames, key):
    return [f.get(key) for f in frames]


def valid_frames(frames):
    """排除预热帧。旧报告没有 is_warmup 标记，就全用。"""
    if any("is_warmup" in f for f in frames):
        return [f for f in frames if not f.get("is_warmup")]
    return list(frames)


def compute_intervals_ns(frames):
    ts = [f.get("sensor_timestamp_ns") for f in frames]
    ts = [t for t in ts if isinstance(t, (int, float)) and t > 0]
    if len(ts) < 2:
        return []
    return [ts[i] - ts[i - 1] for i in range(1, len(ts))]


def compute_lens_timeline(frames):
    """镜头机械状态序列，以及 MOVING 是否构成一个从第 0 帧开始的连续前缀。

    这个形状判定是关键：从第 0 帧起的一段连续 MOVING 是 session 起始的机械
    收敛瞬态（丢帧即可），而散布在中间的 MOVING 说明采集期间镜头还在动，
    那种数据的内参不可信。
    """
    seq = frame_series(frames, "lens_state")
    moving = [i for i, s in enumerate(seq) if s == "MOVING"]
    is_prefix = sorted(moving) == list(range(len(moving)))
    return seq, moving, is_prefix


def _crop_from_fingerprint(fp):
    """从配置指纹里取回 crop 矩形（旧版 ⑤ 扫描的 trial 没单列 crop_region）。

    指纹格式形如 `1920x1440|a4096x3072|c4096x3072@0,0|YUV_420_888|w5 #07ffb85a`。
    取不到就返回 None —— 调用方回退到"用输出宽度"并在结论里说明口径不完整。
    """
    if not isinstance(fp, str):
        return None
    for seg in fp.split("|"):
        if not seg.startswith("c"):
            continue
        body = seg[1:].split("@")[0]
        if "x" not in body:
            continue
        try:
            w, h = body.split("x")
            return {"width": int(w), "height": int(h)}
        except (TypeError, ValueError):
            return None
    return None


def _skew_value(report):
    """取一份报告里的卷帘读出时间（ns）。返回 (值, 来源) 或 (None, 原因)。

    ⚠ 为什么值得专门写一个函数（第 8 轮修掉的一个静默 bug）：
    `stats.lock_stability.<字段>.values` 是 App 用 `get(name).toString()` 写出来的，
    里面是**字符串**（`"9212928"`）。而判据里写的是
    `[v for v in values if isinstance(v, (int, float))]` —— 于是永远过滤掉全部值，
    静默返回"取不到"。判据照样输出了一行看起来正常的结论，只是那个结论是错的：
    它把"我没解析对"说成了"报告里没有"。

    这正是本项目反复踩的同一类坑：**"取不到"与"值不合法"必须分开报**。
    所以这里显式做类型转换，并把来源写进返回值，让报告能看出值是从哪儿来的。
    数值来源优先用 `stats.rolling_shutter_skew_ns.value`（本来就是数字），
    lock_stability 只作兜底。
    """
    stats = ((report or {}).get("stats") or {})
    v = (stats.get("rolling_shutter_skew_ns") or {}).get("value")
    iv = _as_positive_int(v)
    if iv is not None:
        return iv, "stats.rolling_shutter_skew_ns.value"
    raw = ((stats.get("lock_stability") or {}).get("rolling_shutter_skew_ns") or {}) \
        .get("values") or []
    for x in raw:
        iv = _as_positive_int(x)
        if iv is not None:
            return iv, f"lock_stability.values（原始类型 {type(x).__name__}，已显式转换）"
    return None, "报告里既没有 stats.rolling_shutter_skew_ns.value，也没有可转换的 lock_stability 值"


def _as_positive_int(v):
    """把可能是字符串/浮点的 JSON 值转成正整数；转不了返回 None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v > 0 else None
    if isinstance(v, float):
        return int(v) if v > 0 else None
    if isinstance(v, str):
        try:
            iv = int(float(v.strip()))
        except (TypeError, ValueError):
            return None
        return iv if iv > 0 else None
    return None


# ------------------------------------------------------- 配置指纹的逐段对比

# `config_fingerprint` 的段序（见 App 的 configFingerprint()）：
#   输出尺寸 | 活动阵列 | crop | 输出格式 | 预热帧数 | #摘要
# 段名写在这里是为了**报错能点名是"哪一段"不同**。只甩两个完整指纹给用户看，
# 用户得自己逐段数 —— 实测踩过：两份指纹只差"预热帧数"这一段，
# 而判据说的是"不止动了一个变量"，归因不准，用户也无从下手。
FP_SEGMENT_NAMES = ("输出尺寸", "活动阵列", "crop", "输出格式", "预热帧数", "指纹摘要")


def _fingerprint_diff(a, b):
    """逐段对比两个 `config_fingerprint`，返回 `[(段名, A 值, B 值), ...]`（只列不同的段）。

    段数不一致时，多出来的段按 `(缺)` 参与比较 —— 缺失也是差异，不能静默跳过。
    """
    pa = [x.strip() for x in str(a or "").split("|")]
    pb = [x.strip() for x in str(b or "").split("|")]
    out = []
    for i in range(max(len(pa), len(pb))):
        va = pa[i] if i < len(pa) else "(缺)"
        vb = pb[i] if i < len(pb) else "(缺)"
        if va != vb:
            name = FP_SEGMENT_NAMES[i] if i < len(FP_SEGMENT_NAMES) else f"第{i + 1}段"
            out.append((name, va, vb))
    return out


def _device_key(report):
    """取报告自带的设备/固件标识，返回 `(key, 来源)`；取不到返回 `(None, 原因)`。

    为什么需要它：本方案全部判据都在回答"**这台机器**能不能当测量相机"。
    若把两台机器的报告混在一起，配置指纹与采集路径可以**完全一致**，
    于是所有判据都看不见 —— 而 fx / 读出时间 / 卷帘行为都会不同。
    1.8.1 起每份报告自带 `device`；更早的报告只有①能力报告有。
    """
    d = (report or {}).get("device")
    if not isinstance(d, dict):
        return None, "该报告没有 device 段（1.8.1 起才有）"
    fp = d.get("build_fingerprint")
    if isinstance(fp, str) and fp.strip():
        return fp.strip(), "报告自带 device.build_fingerprint"
    model = d.get("model")
    if isinstance(model, str) and model.strip():
        return f"model={model.strip()}", "报告自带 device.model（无 build_fingerprint）"
    return None, "device 段存在但既无 build_fingerprint 也无 model"


# ---------------------------------------------------------------- 锁定连拍

def _skew_norm(cfg, skew_ns):
    """把 skew 归一到"读出窗口行数"，返回各口径的量。

    唯一恒定的口径（实测五个档位极差 0 ppm）：
        读出窗口行数 = 读出宽度 × outH / outW   (= 读出宽度 / 输出纵横比)
        line_time    = skew / 读出窗口行数
    另外两个口径（skew/输出行数、skew/crop行数）也一并返回，便于在输出里
    直接看见它们**不**恒定 —— 这正是推翻 v1.5 结论的证据形式。

    第 7 轮的判别档把"读出宽度"这个量**部分**钉住了：
      同一输出 3840x2160（16:9），一条常规档 crop=4096x2304、一条判别档
      crop=4096x3072（强制全阵列），两者 skew **完全相同 = 6909696 ns**。
      ⇒ crop 的**高度**与 skew 无关（3072 行 ≠ 2304 行，却没有产生差别）。
    仍未分离的是 crop 的**宽度**与活动阵列宽度 —— 迄今所有档位的 crop 宽
    都等于全阵列宽 4096，两者始终同值。要分离它需要一个 crop 宽 < 全阵列的档
    （预测：4:3 输出 + 半宽 crop 2048x1536 ⇒ 若按 crop 宽则 skew=4.61 ms，
    若按阵列宽则 9.21 ms）。这**不影响正确性**（采集侧直接读报出的 skew，
    不用这个公式），只影响"能否外推"。
    """
    cfg = cfg or {}
    out = cfg.get("output_size") or {}
    crop = cfg.get("crop_region") or {}
    act = cfg.get("active_array") or {}

    def _i(d, k):
        v = d.get(k)
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    out_w, out_h = _i(out, "w"), _i(out, "h")
    crop_w, crop_h = _i(crop, "width"), _i(crop, "height")
    # crop 缺字段时回退到活动阵列（旧版 App 的报告没有 crop_region）
    if not crop_w:
        crop_w = _i(act, "w")
    if not crop_h:
        crop_h = _i(act, "h")
    if not (out_w and out_h and out_w > 0 and out_h > 0):
        return None
    src_w = crop_w or out_w
    read_rows = src_w * out_h / out_w
    if read_rows <= 0:
        return None
    return {
        "read_rows": read_rows,
        "line_time_ns": skew_ns / read_rows,
        "crop_w": src_w,
        "crop_h": crop_h,
        "active_w": _i(act, "w"),
        "out_w": out_w,
        "out_h": out_h,
        # 读出宽度是"crop 宽"还是"阵列宽"尚未分离（实测两者同值）；
        # 这里把两者的行数都算出来，供报告在它们不一致时提示"不可外推"。
        "read_rows_active": ((_i(act, "w") or src_w) * out_h / out_w),
        "per_output_row_ns": skew_ns / out_h,
        "per_crop_row_ns": (skew_ns / crop_h) if crop_h else None,
        "asymmetric_crop": bool(crop_w and crop_h and
                                abs(crop_w / crop_h - out_w / out_h) > 0.01),
    }


LOCKED_CHECKS = [    ("G9", "参数锁定稳定性"),
    ("G10", "帧间隔抖动"),
    ("G20", "时间戳质量"),
    ("G11", "镜头状态"),
    ("G12", "AF 状态"),
    ("G13", "温升"),
    ("G14", "卷帘读出时间"),
    ("G15", "帧时长采纳情况"),
    ("G16", "对焦点与景深"),
    ("G19", "生效物理摄像头恒定"),
]


def evaluate_locked(locked, cam, res, sweep=None):
    if locked is None:
        for gid, title in LOCKED_CHECKS:
            res.add_unverified(gid, f"{title}（未验证）",
                               "未运行锁定连拍验证 -> 请先在 App 里点②，再重跑本脚本")
        return

    applied = locked.get("applied") or {}
    stats = locked.get("stats") or {}
    frames = locked.get("frames") or []

    if not frames:
        res.add("G9", FAIL, "锁定连拍未采到任何帧",
                f"frame_count={locked.get('frame_count')} -> 会话可能配置失败")
        for gid, title in LOCKED_CHECKS[1:]:
            res.add_unverified(gid, f"{title}（无法判定）", "无帧数据")
        return

    valid = valid_frames(frames)
    res.add("I7", INFO, "预热帧与有效帧",
            f"总帧={len(frames)}, 预热={len(frames) - len(valid)}, 有效={len(valid)}"
            + ("" if len(valid) else "  -> 有效帧为 0，统计无意义"))

    # G9 参数锁定稳定性
    lock = stats.get("lock_stability") or {}
    drifted = []
    skipped = []
    for k, e in lock.items():
        if not e or e.get("samples", 0) == 0:
            skipped.append(k)
            continue
        if not e.get("locked"):
            drifted.append((k, e.get("values")))
    if not drifted:
        res.add("G9", PASS, "所有锁定字段在连拍中保持不变",
                f"已校验={len(lock) - len(skipped)} 项, 无样本={skipped}")
    else:
        res.add("G9", FAIL, "存在参数漂移",
                "; ".join(f"{k}={v}" for k, v in drifted) +
                " -> 该字段未真正锁住，标定会失效")

    # G10 帧间隔抖动（只看离散程度）
    intervals = compute_intervals_ns(valid)
    iv = stats.get("interval_ns") or {}
    if intervals:
        mean_ns = sum(intervals) / len(intervals)
        std_ns = math.sqrt(sum((x - mean_ns) ** 2 for x in intervals) / len(intervals))
        jit = std_ns / mean_ns * 100.0 if mean_ns else None
        detail = (f"mean={mean_ns / 1e6:.3f} ms, min={min(intervals) / 1e6:.3f} ms, "
                  f"max={max(intervals) / 1e6:.3f} ms, std={std_ns / 1e6:.4f} ms, "
                  f"抖动={jit:.3f}%")
        if jit is None:
            res.add("G10", WARN, "帧间隔抖动无法计算", detail)
        elif jit <= JITTER_OK_PP:
            res.add("G10", PASS, "帧间隔抖动良好", detail)
        elif jit <= JITTER_WARN_PP:
            res.add("G10", WARN, "帧间隔抖动偏大", detail + " -> 离线按时间戳抽帧可缓解")
        else:
            res.add("G10", FAIL, "帧间隔抖动过大", detail + " -> 疑被降频或与其他负载抢占")
    elif iv.get("mean"):
        res.add("G10", PASS, "帧间隔抖动良好（取自报告汇总）",
                f"抖动={iv.get('jitter_pp_percent')}%")
    else:
        res.add("G10", FAIL, "帧间隔无数据", "只收到 1 帧，无法评估连续性")

    # G20 时间戳质量（"时间戳准确"的可判据化）
    evaluate_timestamp_quality(locked, intervals, res)

    # G11 镜头状态：区分"起始收敛瞬态"与"采集期间持续移动"
    seq, moving, is_prefix = compute_lens_timeline(frames)
    if not moving:
        res.add("G11", PASS, "镜头全程静止",
                f"lens_state 取值={sorted(set(s for s in seq if s))}")
    elif is_prefix:
        head = len(moving)
        detail = (f"MOVING 出现在前 {head} 帧且连续（帧索引 {moving}），之后全程静止 "
                  f"-> session 起始的机械收敛瞬态")
        if head <= WARMUP_TOLERANCE:
            res.add("G11", PASS, "镜头已收敛（起始瞬态）",
                    detail + f"；交付数据必须丢弃前 {head} 帧")
        else:
            res.add("G11", WARN, "镜头收敛偏慢",
                    detail + f"；需丢弃前 {head} 帧，长时采集建议每轮重新收敛")
    else:
        res.add("G11", FAIL, "采集期间镜头仍在移动",
                f"MOVING 帧索引={moving}（非起始连续段）"
                " -> 这些帧的几何不可信，标定假设不成立")

    # G12 AF 状态（AF 关闭时应为 INACTIVE）
    afs = stats.get("states", {}).get("af_state") or []
    if not afs:
        afs = sorted({s for s in frame_series(frames, "af_state") if s})
    if afs and set(afs) <= {"INACTIVE"}:
        res.add("G12", PASS, "AF 已彻底停用", f"af_state={afs}")
    elif afs:
        res.add("G12", WARN, "AF 状态非 INACTIVE",
                f"af_state={afs} -> 可能仍在尝试对焦")
    else:
        res.add("G12", INFO, "AF 状态未采集", "")

    # G13 温升：采样太短就测不出热漂移，此时不产生结论
    tc = stats.get("temperature_c")
    ts_valid = [f.get("sensor_timestamp_ns") for f in frames
                if isinstance(f.get("sensor_timestamp_ns"), (int, float))]
    dur_s = ((max(ts_valid) - min(ts_valid)) / 1e9) if len(ts_valid) >= 2 else 0.0
    if isinstance(tc, dict):
        src = tc.get("source", "unknown")
        d = tc.get("delta")
        detail = (f"[来源={src}] {tc.get('first')} -> {tc.get('last')} C, "
                  f"min={tc.get('min')}, max={tc.get('max')}, delta={d} C, "
                  f"采样时长={dur_s:.1f} s")
        if d is None:
            res.add("G13", INFO, "温度数据不完整", detail)
        elif dur_s < TEMP_MIN_DURATION_S:
            res.add("G13", INFO, "采样时长的尺度测不出热漂移",
                    detail + f" -> 电池温度是滞后量，{TEMP_MIN_DURATION_S:.0f} s 内几乎不变，"
                    "该窗口内的“温升可控”没有信息量。热漂移应作为独立的长时测试项")
        elif d <= 5.0:
            res.add("G13", PASS, "长时温升可控", detail)
        else:
            res.add("G13", WARN, "长时温升明显",
                    detail + " -> 采集需分段并记录温度，可能需重标定")
    else:
        res.add("G13", INFO, "未取到温度",
                "Camera2 公共 API 不提供传感器温度，本报告用电池温度作代理；"
                "若为空说明 STATIC 电池广播不可用")

    # G14 卷帘读出时间（逐帧可读，是卷帘补偿的必需量）
    # 本判据只答"稳不稳"，**不答"该除以谁"** —— 归属单独在下面这段交代。
    #
    # 归属问题的三次改判史（都写下来，因为三次都是"看起来成比例"导致的）：
    #   v1.4 口径：按**整个活动阵列**报告，输出覆盖 N 行须乘 N/H        —— 错
    #   v1.5 口径：就是**输出帧**的读出时间，随输出高度成正比            —— 也错
    #   v1.6 口径：读出窗口行数 = 读出宽度 × outH / outW（= 读出宽度/输出纵横比）
    #               skew = 2999.0 ns/行 × 该行数
    #
    # 六个数据点（第 3/4/5 轮的 1080p + 第 7 轮 ⑤ 尺寸扫描五档）：
    #     输出        crop        skew     skew/outH  skew/cropH  skew/读出窗口行
    #   1920x1080  4096x3072   6909696      6397.87     2249.25        2999.000
    #   1920x1440  4096x3072   9212928      6397.87     2999.00        2999.000
    #   2880x2160  4096x3072   9212928      4265.24     2999.00        2999.000
    #   4096x3072  4096x3072   9212928      2999.00     2999.00        2999.000
    #   3840x2160  4096x2304   6909696      3198.93     2999.00        2999.000
    #   3840x2160  4096x3072   6909696      3198.93     2249.25        2999.000  <- 判别档
    # 只有最后一列恒定（极差 0 ppm）。
    #
    # 前两次错判都源于**同一对数据**：1080→1440 那一对里"输出高度"与"输出纵横比"
    # 同时按 4/3 变化，两个自变量一起动 ⇒ 一对观测分离不出因果（见教训 12）。
    # 第 7 轮的**判别档**是关键：同一个 16:9 输出，一条 crop 高 2304、一条强制
    # crop 高 3072，skew **完全相同** ⇒ 直接排除 crop 高度（注意该行的
    # skew/cropH 列从 2999.00 变成 2249.25，而 skew 没变 —— 这就是"分子不变、分母变"的
    # 排除法）。仍未分离的只有"读出宽度取 crop 宽还是阵列宽"（迄今两者同值 4096）。
    #
    # 操作结论：**不要自己按任何 N/H 缩放，直接用报出的值**；改**输出纵横比**必须
    # 重测；换输出高度**不必**重测（只要纵横比不变）。
    skews = [f.get("rolling_shutter_skew_ns") for f in valid
             if isinstance(f.get("rolling_shutter_skew_ns"), (int, float))
             and f.get("rolling_shutter_skew_ns") > 0]
    if skews:
        dc = len(set(skews))
        cfg = locked.get("capture_config") or {}
        norm = _skew_norm(cfg, skews[0])
        row_txt = ""
        if norm:
            # ⚠ 这里曾经写成 `outH/outW = {out_w}×{out_h}/{out_w}`，渲染出
            # "= 1920×1440/1920" —— 读起来像在说输出尺寸，实际想表达的是比值。
            # 报告文本的错误表述会被下游（包括人）直接采信，所以格式串也要当代码审。
            ratio = norm["out_h"] / norm["out_w"]
            row_txt = (f"，即 {norm['line_time_ns']:.3f} ns/读出窗口行"
                       f"（读出窗口 {norm['read_rows']:.0f} 行 = 读出宽度 {norm['crop_w']}"
                       f" × outH/outW，其中 outH/outW = {norm['out_h']}/{norm['out_w']}"
                       f" = {ratio:.4f}）")
            if abs(norm["read_rows_active"] - norm["read_rows"]) > 1.0:
                row_txt += (f" ⚠ crop 宽（{norm['crop_w']}）≠ 阵列宽"
                            f"（{norm['active_w']}），此时该公式**未经实测标定**，"
                            "不可外推 —— 请直接采信报出的 skew")
        detail = (f"skew={skews[0]:.0f} ns (首行到「末行之后一行」的曝光起点差，"
                  f"即读出窗口的读出时间){row_txt}, 范围=[{min(skews):.0f}, {max(skews):.0f}], "
                  f"不同取值={dc}")
        if dc == 1:
            res.add("G14", PASS, "卷帘读出时间可逐帧取得且恒定", detail
                    + "。⚠ 归属已三次改判，现口径为「读出窗口行数 = 读出宽度 × outH/outW」"
                      "（实测 2999.0 ns/行，跨五个档位极差 0）。第 7 轮判别档"
                      "（同 16:9 输出、crop 2304 行 vs 3072 行）证实 **crop 高度与 skew 无关**；"
                      "读出宽度取 crop 宽还是阵列宽尚未分离（迄今两者同值）。"
                      "**不要自己按输出行数/活动阵列行数/crop 行数缩放**，直接用报出的值；"
                      "改**输出纵横比**必须重测，只改输出高度（纵横比不变）则不必重测。"
                      "判别档见 ⑤ 尺寸扫描")
        else:
            res.add("G14", WARN, "卷帘读出时间在变化",
                    detail + " -> 像素模式、输出纵横比或 crop 发生了改变，"
                             "卷帘补偿量必须重新确定，不能沿用上一个 profile")
    else:
        res.add("G14", WARN, "未取到卷帘读出时间",
                "CaptureResult.SENSOR_ROLLING_SHUTTER_SKEW 为空（需 Android 9+）；"
                "卷帘补偿需改用其他方式估计（如按帧时长比例反推）")

    evaluate_frame_rate(locked, intervals, res, sweep)

    # G19 生效物理摄像头恒定
    # zoom 锁在 1.0 只说明"我们请求了 1.0"，不能证明 HAL 内部没换物理镜头。
    # 逐帧读 LOGICAL_MULTI_CAMERA_ACTIVE_PHYSICAL_ID 才是直接证据。
    lock_entries = stats.get("lock_stability") or {}
    pid_key_present = "active_physical_id" in lock_entries
    pid_entry = lock_entries.get("active_physical_id") or {}
    pid_vals = [str(f.get("active_physical_id")) for f in frames
                if f.get("active_physical_id") is not None]
    if not pid_key_present and not pid_vals:
        res.add_unverified("G19", "生效物理摄像头恒定（未验证）",
                           "报告里没有 active_physical_id 字段 -> 请用新版 App 重跑②"
                           "（这是判定「有没有中途换镜」的唯一直接仪器）")
    elif not pid_vals or pid_entry.get("samples", 0) == 0:
        res.add("G19", WARN, "未取到生效物理摄像头",
                "active_physical_id 全为空（需 Android 10+，且设备须声明 "
                "LOGICAL_MULTI_CAMERA） -> 无法证明采集期间没有换镜，"
                "这是内参可复现性上的一个识别盲区")
    else:
        distinct = sorted(set(pid_vals))
        if len(distinct) == 1:
            res.add("G19", PASS, "生效物理摄像头全程唯一",
                    f"active_physical_id={distinct[0]}（{len(pid_vals)} 帧样本）"
                    " -> 未发生换镜，整段数据共用同一套内参")
        else:
            res.add("G19", FAIL, "采集中途更换了物理摄像头",
                    f"取值为 {distinct} -> 焦距/主点/畸变同时跳变，"
                    "整段数据的内参不一致，必须按镜头分段处理或重采")

    if cam is not None:
        evaluate_focus_depth(locked, cam, stats, res)

    # 参考：实际施加的锁定项
    off = [k for k, v in applied.items() if k.endswith("_applied") and v is True]
    not_off = [k for k, v in applied.items() if k.endswith("_applied") and v is False]
    res.add("I4", INFO, "实际施加成功的锁定项", f"成功={off}")
    if not_off:
        res.add("I5", WARN, "未能施加的锁定项", f"{not_off} -> 对应字段可能在漂移")


def rate_control_authority(sweep):
    """从④的原始档位数据重算"帧时长控不控得住"。

    返回 True / False / None（无数据）。

    **刻意不采信报告里自报的 `control_authority` 字段** —— 那是被测程序自己的结论，
    用它来判被测程序等于自证。判据一律从逐档的实测帧间隔重算。
    """
    if not isinstance(sweep, dict):
        return None
    vals = [t["actual_interval_ns"] for t in (sweep.get("trials") or [])
            if isinstance(t.get("actual_interval_ns"), (int, float))]
    if len(vals) < 2:
        return None
    return len(cluster_values(vals, RATE_CLUSTER_TOL_PP)) >= 2


def evaluate_frame_rate(locked, intervals, res, sweep=None):
    """G15 请求的帧时长是否被 HAL 采纳。

    **这个判据只能用"逐字相等"来判"是否逐字采纳"，不能用百分比偏差判** —— 后者在这里
    曾经是不可证伪的：

      第 1 轮请求 CONTROL_AE_TARGET_FPS_RANGE=[12,12]（期望 83.33 ms），实测 33.29 ms
      第 2 轮请求 SENSOR_FRAME_DURATION=33333333 ns（期望 33.33 ms），实测 33.29 ms
    两次请求相差 2.5 倍、回读的帧时长完全一样，而按百分比判第 2 轮偏差只有 0.14% → 会被判成 PASS。

    **但"逐字不等"也不能直接推出"请求没被采纳"** —— 这是第 3 轮才修掉的反向错误。
    第 3 轮的 4 档扫描（12/15/24/60）显示：请求 12/15/24 fps 各自得到了 83.23/66.58/41.62 ms，
    与请求的偏差都是 0.12%~0.14%，量级一致 —— 说明请求被采纳了，只是回读值系统性偏低。

    **第 4 轮进一步否掉了"量化到可达网格"这个解释**（那是本判据上一版的说法）：
      - 三个可达档的 回读/请求 比值 = 0.99878385(12fps) / 0.99863986(15fps) / 0.99878385(24fps)
        —— 不是同一个网格步长下的四舍五入（那会让偏差**正负随机**），
        而是**系统性偏低且随档位微变**（极差 144 ppm）
      - gcd(三个回读值) = 1 —— 不存在公共的计时网格步长
    所以正确说法是："请求被采纳，回读值按 HAL 自己的时间单位重新表达，
    结果系统性偏低 0.12%~0.14%，且偏置量随档位有约 144 ppm 的差异"。
    偏置的**机制**（分频/换算）从 4 个点无法确定，也不需要确定 ——
    因为实测帧间隔与回读值一致到 0.1 ppm 量级，回读值就是物理事实本身。

    判定分三态：

      1. 回读值逐字等于请求值            -> PASS
      2. 逐字不等，但 G18 显示控制有效    -> INFO「被采纳但系统性偏低」（回读值仍是唯一事实）
      3. 逐字不等，且 G18 显示控制无效    -> WARN「HAL 自行决定帧时长，请求未被采纳」

    教训：**"值不等"和"值不对"是两件事**，跟"离散度小≠值正确"是同一个坑的两面。
    判定"控制点有没有生效"只能靠多档扫描（G18），不能靠单点比对。
    另一条：**给一个现象起名时要克制**。"量化到网格"听起来专业但当时并无证据，
    它会把"未知的固定偏置"伪装成"已理解的行为"，从而挡掉后续的追问。
    """
    req = locked.get("requested") or {}
    stats = locked.get("stats") or {}
    frames = locked.get("frames") or []
    fdc = stats.get("frame_duration_control") or {}

    want_ns = req.get("sensor_frame_duration_ns")
    if not isinstance(want_ns, (int, float)) or want_ns <= 0:
        res.add("G15", INFO, "无法推出期望帧时长",
                "报告里没有 requested.sensor_frame_duration_ns")
        return

    actual_fd = fdc.get("actual_ns")
    if not isinstance(actual_fd, (int, float)):
        vals = [f.get("sensor_frame_duration_ns") for f in valid_frames(frames)
                if isinstance(f.get("sensor_frame_duration_ns"), (int, float))]
        actual_fd = vals[0] if vals else None

    if actual_fd is None:
        res.add("G15", FAIL, "未取到回读的帧时长", f"请求={want_ns / 1e6:.3f} ms")
        return

    actual_iv = (sum(intervals) / len(intervals)) if intervals else None
    dev_pp = abs(actual_fd - want_ns) / want_ns * 100.0
    detail = (f"请求帧时长={want_ns / 1e6:.3f} ms, 回读帧时长={actual_fd / 1e6:.3f} ms"
              + (f", 实测帧间隔={actual_iv / 1e6:.3f} ms" if actual_iv else "")
              + f"（偏差 {dev_pp:.3f}%）")

    authority = rate_control_authority(sweep)
    if int(actual_fd) == int(want_ns):
        res.add("G15", PASS, "请求的帧时长被逐字采纳", detail)
    elif authority is True:
        # 从扫描数据重算"回读/请求"的比值离散度，说明偏置随档位变化了多少。
        # 不采信任何自报结论，一律从原始档位重算。
        ratios = [t["actual_frame_duration_ns"] / t["requested_ns"]
                  for t in ((sweep or {}).get("trials") or [])
                  if isinstance(t.get("requested_ns"), (int, float))
                  and isinstance(t.get("actual_frame_duration_ns"), (int, float))
                  and t["requested_ns"] > 0 and t["actual_frame_duration_ns"]
                  < t["requested_ns"] * 1.2]          # 排掉回落档（60fps 被顶到 2×）
        bias_txt = ""
        if len(ratios) >= 2:
            lo, hi = min(ratios), max(ratios)
            bias_txt = (f"。各可达档 回读/请求 比值落在 {lo:.7f}~{hi:.7f}"
                        f"（极差 {1e6 * (hi - lo):.0f} ppm，全部 <1）"
                        "—— 是**系统性偏低**而非同一步长的取整，"
                        "所以本项不能叫「量化到网格」")
        consist = (f"。实测帧间隔与回读值一致度={abs(actual_iv - actual_fd) / actual_fd * 1e6:.3f} ppm"
                   if actual_iv else "")
        res.add("G15", INFO, "帧时长被采纳，但回读值系统性偏低",
                detail + "。G18 已证明控制点生效：多个档位的实测帧间隔各自跟随请求变化。"
                "请求被采纳后，HAL 用自己的时间单位重新表达帧长" + bias_txt + consist
                + f"。回读值 {actual_fd / 1e6:.3f} ms ({1e9 / actual_fd:.3f} fps) "
                "仍是唯一事实，采集节奏按它规划即可")
    elif authority is False:
        res.add("G15", WARN, "HAL 改写了帧时长，请求未被采纳",
                detail + f"。回读值 {actual_fd / 1e6:.3f} ms ({1e9 / actual_fd:.3f} fps) "
                "才是唯一事实，采集节奏必须按它规划。帧率能否被控制由 G18 判定")
    else:
        # 没有多档扫描数据时不下结论：单点比对无法区分"被采纳但偏低"与"被忽略"
        res.add("G15", WARN, "帧时长与请求不符，且无法判定控制点是否生效",
                detail + "。仅凭单点比对无法区分「被采纳但偏低」与「被忽略」——"
                "必须跑④帧率权威性测验（多个离默认值足够远的档位）才能定性。"
                "在此之前，采集节奏只能按回读值规划")


def cluster_values(vals, tol_pp=1.0):
    """按相对容差把帧间隔聚成"档"，返回 [(代表值, 样本数), ...]。

    **为什么必须聚类而不是直接对 ns 整数去重**：第 3 轮报告曾把
    [33.288, 33.288, 41.616, 66.576, 66.576, 83.232, 83.232] 报成"7 种实测帧间隔"，
    而其实只有 4 档 —— 多出来的 3 个是同档位内部 26 ns（8e-8 相对量）的测量噪声。
    直接去重的判据在"两个档位只差噪声"时会把噪声当成档位，从而误报 PASS：
    只要有一点点噪声，"互不相同"就永远成立，这条判据就变成了恒过判据。
    所以先按容差聚类，再数簇的个数。
    """
    if not vals:
        return []
    sv = sorted(vals)
    clusters = [[sv[0]]]
    for v in sv[1:]:
        ref = sum(clusters[-1]) / len(clusters[-1])
        if abs(v - ref) <= ref * tol_pp / 100.0:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    return [(sum(c) / len(c), len(c)) for c in clusters]


def evaluate_rate_authority(sweep, res, delivery="still"):
    """G18 帧率控制权威性 —— 单点测试不可证伪，必须扫多个档位。

    请求 30 fps 得到 30.04 fps 看起来"一致"，但那可能只是 sensor 原生帧率
    恰好接近 30 fps，请求根本没被采纳。要证明控制真的生效，只能请求若干个
    **离默认值足够远**的档位，看实测帧间隔是否跟着变。

    只回答两个问题，缺一不可：
      1. 档位之间**能不能被区分**（聚类数 >= 2）—— 说明控制点真的在起作用
      2. **每个档位是否达标**（实测帧率 vs 请求帧率的偏差）—— 说明上限在哪
    第 2 条是第 3 轮补的：当时的判据只数"不同档位个数"，于是
    "请求 60 fps 回落成 30.04 fps"被判成了 PASS —— 档位确实互不相同，
    但有一个档位根本没达到。**"能区分"不等于"都能达到"**。

    ---- 第 8 轮：本项按**交付路径**门控 ----
    交付方式一确定，本项的**权重**就变了，而且方向不定 —— 两种交付方式下
    它的意义完全不同，所以不能只改一个阈值了事：

      · 交付 = 拍照（按需触发）：一次下发得一张，节奏由 PC 决定，
        sensor 的速率上限**不构成任何约束**。此时本项的证据保留，但结论降级为
        INFO —— 强行留成 WARN 会让"总判定"长期挂着一个与交付无关的红灯，
        久而久之所有人都会习惯性无视警告，那比没有警告更糟。
      · 交付 = 连续流 / 抽帧：帧率上限直接决定能不能采到想要的节奏，维持原判。

    ⚠ `delivery` 是**调用方声明的输入**，不是测出来的事实。所以它必须写进
    本项的说明里 —— 否则将来读报告的人会以为"帧率无关"是实测结论。
    """
    if sweep is None:
        res.add_unverified("G18", "帧率控制权威性（未验证）",
                           "未运行 App 的④帧率权威性测验 -> 只按②的单一档位判"
                           "「帧率一致」是无法证伪的，不能算通过")
        return

    trials = sweep.get("trials") or []
    ok = [t for t in trials if isinstance(t.get("actual_interval_ns"), (int, float))]
    failed = [t for t in trials if isinstance(t, dict)
              and not isinstance(t.get("actual_interval_ns"), (int, float))]

    def _fail_summary():
        """把失败档按错误串归组显式列出。

        第 11 轮实测：8 档里 5 档失败（4×「摄像头被断开」+ 1× createDefaultRequest
        ENOSYS），12/15 fps 两慢档整个缺席 —— 而旧实现只拿成功档说事，一句不提。
        「摄像头被断开 / CAMERA_ERROR」是**设备级瞬时故障**，不是"该档不可达"，
        两者若混在一行里，失败档的可达性就被静默当成"已判过"。
        """
        if not failed:
            return ""
        by_err = {}
        for t in failed:
            e = str(t.get("error") or "未记录错误")
            by_err.setdefault(e, []).append(t.get("requested_fps"))
        parts = []
        for e, fps in by_err.items():
            ints = sorted(f for f in fps if isinstance(f, int))
            parts.append(f"{e}（{len(fps)} 档" + (f"：{ints}" if ints else "") + "）")
        return (f"；⚠ {len(failed)}/{len(trials)} 档未取得数据 —— " + "；".join(parts)
                + " ⇒ 本判据只覆盖成功档位，失败档的可达性/跟随性**未知**"
                + "（「摄像头被断开/CAMERA_ERROR」是设备级瞬时故障，不是档位不可达；"
                  "1.8.2 起 App 打开摄像头遇此故障会自动重试并留痕）")

    fail_note = _fail_summary()
    if not ok:
        res.add("G18", WARN, "帧率测验未取得有效数据",
                f"trials={len(trials)}，全部失败或无帧" + fail_note)
        return

    req_fps = sorted({t.get("requested_fps") for t in ok
                      if isinstance(t.get("requested_fps"), int)})
    verbatim = sum(1 for t in ok if t.get("honored_verbatim") is True)
    mode = sorted({bool(t.get("legacy_fps_range")) for t in ok})
    mode_txt = "同时下发 AE fps 档位" if mode and mode[0] else "仅下发 SENSOR_FRAME_DURATION"
    if len(mode) > 1:
        mode_txt = "两种下发方式都试了"

    clusters = cluster_values([t["actual_interval_ns"] for t in ok],
                              RATE_CLUSTER_TOL_PP)

    # 逐档：请求 fps -> 实测 fps，以及偏差
    per_req = {}
    for t in ok:
        rf = t.get("requested_fps")
        if not isinstance(rf, int):
            continue
        per_req.setdefault(rf, []).append(t["actual_interval_ns"])

    tbl, missed = [], []
    for rf in sorted(per_req):
        iv = sum(per_req[rf]) / len(per_req[rf])
        got = 1e9 / iv
        dev = (got - rf) / rf * 100.0
        tbl.append(f"{rf}->{got:.2f}fps({dev:+.2f}%)")
        if abs(dev) > FPS_MISS_TOL_PP:
            missed.append(f"{rf}fps 回落为 {got:.2f}fps")

    # ④ 的每个 trial 应当记录它在**哪个输出尺寸**下跑的（MainActivity 已写该字段，
    # 但磁盘上这份 ④ 没有 —— 说明它是**旧版 APK** 的产出）。不记尺寸，"该档不可达"
    # 就不知道适用于哪一档，也无法回答"上限是否随尺寸变化"。
    # 这一类"报告缺字段导致自我定位失败"本轮已是第三例（⑤缺请求帧率、②③版本语义、
    # ④缺输出尺寸），所以判据要把"能不能定位自己"也当结论的一部分。
    sizes = set()
    for t in ok:
        cs = t.get("capture_size") or {}
        if isinstance(cs.get("w"), int) and isinstance(cs.get("h"), int):
            sizes.add(f"{cs['w']}x{cs['h']}")
    if sizes:
        size_note = f"；④ 覆盖的输出尺寸: {'、'.join(sorted(sizes))}"
    else:
        size_note = ("；⚠ ④ 未记录输出尺寸（磁盘上这份由旧版 APK 写出）"
                     "⇒ **该上限适用于哪个输出尺寸无法确认**，"
                     "更不能据此说「帧率上限与输出尺寸无关」；"
                     "需要该结论时请在当前 APK 下重跑 ④（现在会记 capture_size）")

    detail_base = (f"请求 {req_fps} fps（{mode_txt}）得到 {len(clusters)} 档实测帧间隔 "
                   f"{[round(c[0] / 1e6, 3) for c in clusters]} ms；"
                   f"逐字采纳 {verbatim}/{len(ok)}；逐档实测 {tbl}" + fail_note)

    # 交付路径声明（输入，不是测量结果）—— 必须显式写出来
    if delivery == "still":
        deliv = ("。交付路径=**拍照/按需触发**（调用方声明）：一次下发得一张，节奏由 PC 决定，"
                 "sensor 的速率上限不构成约束 ⇒ 本项结论降级为 INFO，**不阻塞**。"
                 "注意：这不是「帧率问题不存在」，而是「在这个交付方式下它无关」；"
                 "若将来改为连续流或按时间戳抽帧，用 --delivery stream 重跑本判据")
        lvl_warn = INFO
    else:
        deliv = ("。交付路径=**连续流/抽帧**（调用方声明）：帧率上限直接决定能否采到"
                 "目标节奏 ⇒ 本项维持原判")
        lvl_warn = WARN

    if len(clusters) == 1:
        res.add("G18", lvl_warn, "帧率不可控：所有档位实测帧间隔完全相同",
                detail_base + f" -> 该值 {clusters[0][0] / 1e6:.3f} ms "
                f"= {1e9 / clusters[0][0]:.3f} fps 即 sensor 原生帧率，"
                "SENSOR_FRAME_DURATION 未被采纳。"
                "影响：采集节奏只能按回读值规划；要降低数据量只能靠 PC 端按时间戳抽帧，"
                "不能靠降帧率省带宽。这不影响内参可复现性" + size_note + deliv)
    elif missed:
        res.add("G18", lvl_warn, "帧率可控，但有档位不可达",
                detail_base + f" -> 不可达档位：{'; '.join(missed)}。"
                "成功的慢速档位均跟随请求变化，说明控制点确实生效；"
                "不可达的那个是**本会话模式下 sensor 的速率上限**。"
                "若需要更高帧率，须另试更小输出尺寸或 constrained high-speed 会话"
                "（需 REQUEST_AVAILABLE_CAPABILITIES 含 CONSTRAINED_HIGH_SPEED_VIDEO），"
                "本判据未覆盖该路径" + size_note + deliv)
    else:
        res.add("G18", PASS, "帧率可控：实测帧间隔随请求变化且各档达标",
                detail_base + size_note + deliv)


def evaluate_timestamp_quality(locked, intervals, res):
    """G20 时间戳质量 —— 把"时间戳准确"从一句话变成几个可测的数。

    G3 只回答"时间基是不是 REALTIME"。但那只是必要条件，不是充分条件：
    REALTIME 的帧间关系**还可能**被 HZ 级抖动、量化台阶、或与回读帧时长不一致破坏。

    **这里刻意不用"帧间隔抖动 (ppm)"当主判据。** 实测教训：
    同一段 149 个间隔只取 3 个值（33287916 / 33287917 / 33288125），
    按 std/mean 算得 3.06 ppm，看起来是"极小的随机抖动"。但把它当序列看，
    `d[i] == d[i+5]` 在 144/144 处成立 —— 它是一条**严格周期 5 的确定性拍频**，
    周期内净和为 0。性质完全不同：
      - 随机抖动 ⇒ 每帧都可能偏，只能给统计量
      - 确定性拍频 ⇒ 每一帧的偏差可复现、可精确预测，且不累积
    真正该判的是**有无漂移**（累积偏离是否发散）与**是否周期的**，
    而"抖动 ppm"会把这两件事都藏起来。

    所以本判据重算四组量：
      1. 单调递增（非严格单调的序列无法用于运动建模）
      2. **序列是否为严格周期**（先判这个，理由见下）
         - 是周期 p ⇒ 判**每周期之和是否恒定**（精确整数比较，无任何相位污染）
         - 不是周期 ⇒ 判回归斜率 + 前后半窗均值差
      3. 相对"名义间隔的无偏估计"的累积偏离极值（有界即不累积）
      4. 名义间隔与回读 SENSOR_FRAME_DURATION 的相对一致性（ppm）
         - 一致 ⇒ 回读的帧时长可以当名义节奏使用

    **为什么必须先判周期、再判漂移**：实测本机型 149 个间隔只取 3 个值，
    且 d[i]==d[i+5] 在 144/144 处成立 —— 是**严格周期 5 的确定性拍频**（非随机噪声）。
    这时"回归斜率"会带进残段相位的算术效应：149 不是 5 的整数倍，末尾 4 个间隔
    会把窗口均值（33288000.56）拉得比真实周期均值（33288000.0）高，
    于是斜率报出 −0.10 ppm 这种"看起来在漂"的数。而正确的判法是
    **每个完整体周期的和是否恒定**（实测 29 个周期的和全部 = 166440000 ns，极差 0）。
    同理，"累积偏离的终值"恒等于 0（mean 取自同一窗口）—— 那是同义反复，不能当证据。

    另需注意官方语义（已核对 AOSP 文档 + 本机型实测）：
      - SENSOR_TIMESTAMP 是**活动阵列第一行**的曝光起始时刻
      - REALTIME 表示与 SystemClock.elapsedRealtimeNanos() 同一时间基
      - SENSOR_ROLLING_SHUTTER_SKEW 是首行到"末行之后一行"的曝光起始差，即帧读出时间。
        **实测：它随输出尺寸变化**（不是活动阵列的固有量）——
        同一台机、同一次 crop、只把输出高度 1080 改成 1440，它就按 4/3 从
        6909696 ns 变成 9212928 ns（两者都是 6397.8667 ns/行）。
        所以换输出尺寸必须重测，且**不要**再自己按 N/H 缩放一次（详见 G14）。
    """
    frames = valid_frames(locked.get("frames") or [])
    ts = [f.get("sensor_timestamp_ns") for f in frames
          if isinstance(f.get("sensor_timestamp_ns"), (int, float))]
    if len(ts) < 3 or not intervals:
        res.add("G20", WARN, "时间戳序列太短，无法评估质量",
                f"有效时间戳={len(ts)}, 间隔样本={len(intervals)}")
        return

    non_monotonic = sum(1 for i in range(1, len(ts)) if ts[i] <= ts[i - 1])
    mean = sum(intervals) / len(intervals)
    std = math.sqrt(sum((v - mean) ** 2 for v in intervals) / len(intervals))
    jit_ppm = std / mean * 1e6
    uniq = sorted(set(intervals))
    span_ns = uniq[-1] - uniq[0]
    shown = uniq if len(uniq) <= 6 else uniq[:3] + ["..."] + uniq[-3:]

    # ---- 帧缺口（丢帧/掉拍）----
    # 必须与"漂移"分开报：缺口是**采样序列里有个洞**，时间戳本身仍然诚实；
    # 漂移是**节奏本身在变**。两者的处置完全不同（前者弃用该轮，后者只能逐帧用时间戳）。
    # 若不做这一步，一个 2× 名义值的间隔会顺手把回归斜率拉大，
    # 于是报告会把它说成"时间戳存在漂移"—— 结论方向就错了。
    nominal_iv = uniq[0]
    gaps = [(i, v) for i, v in enumerate(intervals)
            if nominal_iv and v > nominal_iv * ROUND_GAP_RATIO]
    gap_txt = ""
    if gaps:
        gap_txt = (f"; **存在 {len(gaps)} 处帧缺口**（间隔序号/值="
                   + ", ".join(f"{i}:{v / 1e6:.3f}ms" for i, v in gaps[:6])
                   + f"，名义 {nominal_iv / 1e6:.3f} ms）")

    # ---- 周期结构：先判"是否确定性周期"，再谈漂移。顺序不能反。----
    #
    # 若序列是严格周期的（实测 d[i]==d[i+5] 在 144/144 处成立），那么"回归斜率"
    # 和"前后半窗均值差"都会带进**残段相位**的算术效应：窗口长度不是周期的整数倍时，
    # 末尾那几个间隔会把均值拉偏，于是报出一个并不存在的"漂移"。
    # 此时该判的是**每周期的和是否恒定**。
    n = len(intervals)
    period = None
    for d in range(2, min(13, n)):
        if all(intervals[i] == intervals[i + d] for i in range(n - d)):
            period = d
            break

    cum, acc = [], 0.0
    for v in intervals:
        acc += v - mean
        cum.append(acc)
    cum_min, cum_max = min(cum), max(cum)
    # 注意：cum[-1] 恒等于 0（mean 取自同一窗口），它**不构成任何证据**。
    # 早期版本把"终值=+0"当"无漂移"的证据打印出来 —— 那是同义反复。

    period_note, drift_txt, nominal_est, drift_ppm = "", "", mean, None
    if gaps:
        # 缺口会把回归斜率拉偏（实测一个 2× 值就能把斜率变成 −37048 ppm），
        # 于是报告会说成"时间戳存在漂移"—— 结论方向就错了。所以此时不给漂移数。
        drift_txt = "漂移判据不适用（序列有缺口，缺口会把回归斜率拉偏）"
    elif period:
        n_cyc = n // period
        cyc = [sum(intervals[i * period:(i + 1) * period]) for i in range(n_cyc)]
        cyc_min, cyc_max = min(cyc), max(cyc)
        nominal_est = cyc_min / period      # 周期和恒定 -> 这才是名义间隔的无偏估计
        drift_ppm = (cyc_max - cyc_min) / nominal_est * 1e6
        drift_txt = (f"周期和极差={cyc_max - cyc_min} ns（{n_cyc} 个完整个周期）"
                     f" -> 漂移 {drift_ppm:+.3f} ppm; "
                     f"由周期和推出的名义间隔={nominal_est:.1f} ns")
        period_note = (f"; 间隔为**严格周期 {period} 帧的确定性拍频**"
                       f"（d[i]==d[i+{period}] 在 {n - period}/{n - period} 处成立），"
                       f"不是随机噪声：每帧偏差可复现、可预测，且每个周期内不累积")
    else:
        xs = list(range(n))
        mx = (n - 1) / 2.0
        denom = sum((x - mx) ** 2 for x in xs)
        slope = (sum((xs[i] - mx) * (intervals[i] - mean) for i in range(n)) / denom
                 if denom else 0.0)
        drift_ppm = slope * n / mean * 1e6
        half = n // 2
        half_diff = (sum(intervals[half:]) / (n - half) - sum(intervals[:half]) / half)
        drift_txt = (f"回归斜率={slope:.4f} ns/帧 -> 全窗口 {drift_ppm:+.3f} ppm; "
                     f"前后半窗均值差={half_diff:+.3f} ns "
                     f"({half_diff / mean * 1e6:+.4f} ppm)")

    fdc = ((locked.get("stats") or {}).get("frame_duration_control") or {})
    rb = fdc.get("actual_ns")
    consist_txt, consist_ppm = "", None
    if isinstance(rb, (int, float)) and rb > 0:
        # 与**名义间隔的无偏估计**比，而不是与受残段相位污染的窗口均值比
        consist_ppm = abs(nominal_est - rb) / rb * 1e6
        consist_txt = (f"; 名义间隔 {nominal_est:.1f} ns vs 回读帧时长 {rb:.0f} ns "
                       f"-> 一致度={consist_ppm:.3f} ppm")

    detail = (f"单调={'是' if non_monotonic == 0 else f'否({non_monotonic} 处非递增)'}, "
              f"取值={shown} ns（{len(uniq)} 个不同值，跨度 {span_ns} ns）; "
              f"累积偏离理想均匀网格 min/max={cum_min:+.0f}/{cum_max:+.0f} ns "
              f"（跨 {n} 帧、跨度 {n * mean / 1e6:.0f} ms）; "
              + drift_txt +
              f"; 残差 std={std:.1f} ns ({jit_ppm:.2f} ppm)"
              + consist_txt + period_note + gap_txt)

    drift_bad = drift_ppm is not None and abs(drift_ppm) > TS_DRIFT_OK_PPM
    if non_monotonic:
        res.add("G20", FAIL, "时间戳非单调",
                detail + " -> 该序列不能直接用于运动建模，须查是否发生回绕/重排")
    elif gaps:
        res.add("G20", WARN, "采集序列存在帧缺口（与时间戳漂移无关）",
                detail + " -> 时间戳本身仍然诚实（单调、与回读帧时长自洽），"
                "但**采样序列有个洞**：断口之后的所有帧相对起始帧多累积了"
                f"{gaps[0][1] - nominal_iv:.0f} ns。离线不得假设均匀网格，"
                "必须逐帧用时间戳；采集协议要逐帧校验间隔，"
                "出现缺口即标记该轮并重采（缺口的成因通常是首轮/会话初期的管线抖动）")
    elif drift_bad:
        res.add("G20", WARN, "时间戳存在漂移，帧节奏不恒定",
                detail + f" -> 全窗口漂移 {drift_ppm:+.1f} ppm 超出 "
                f"{TS_DRIFT_OK_PPM:.0f} ppm。离线必须逐帧用时间戳差值，"
                "不得用名义帧率外推；并查是否与热漂移/时钟调频相关")
    elif consist_ppm is not None and consist_ppm > TS_CONSIST_OK_PPM:
        res.add("G20", WARN, "时间戳无漂移，但与回读帧时长不自洽",
                detail + " -> 回读的 SENSOR_FRAME_DURATION 不可当名义节奏，"
                "只能按实测时间戳差值规划")
    else:
        res.add("G20", PASS, "时间戳质量满足运动建模要求", detail
                + ("；回读帧时长可当名义节奏使用（与实测一致）"
                   if consist_ppm is not None else ""))


def evaluate_repeatability(repeat, rounds, res):
    """G21 跨会话内参重复性 —— "内参不变"的地基，必须是必备判据。

    为什么它必须存在：整个方案是"标定一次、采集多次"。若同一套请求参数在不同
    session 里落到不同的电机位置 / 不同的物理镜头 / 不同的输出尺寸，那么
    "一次标定覆盖多次采集"就不成立，内参可复现性的地基就是空的。

    而这一条恰恰是最容易"没跑却以为跑了"的：App 里 ③ 会写出
    m0_locked_capture_r1..r3.json，但旧版脚本**根本不读它们** ——
    于是"没跑 ③"和"③ 全过"在报告上完全一样，都是 WARN/PASS。

    本判据要同时回答四个层次的问题，缺一层就会出假 PASS：

      ① 证据在不在（轮数够不够）—— 缺证据 => INCOMPLETE
      ② **每一轮的数据本身是不是完整的**（丢帧/掉拍）
         实测教训：r1 的 149 个间隔里有一个 66575833 ns = 2× 名义值（掉了一帧），
         而只查"焦点/镜头/尺寸"的旧版 G21 给它判了 PASS。判据看不见自己
         所依赖数据的完整性，就会把坏数据当成好证据用。
      ③ **"跨会话"这三个字有没有被证明**（每轮是否真的重开了摄像头）
         只看回读值一致，无法区分"两次独立会话落到同一位置"和
         "其实只开了一次摄像头、同一个 session 被切成三段"。用首帧指纹
         区分：真关闭再开，镜头回到掉电默认（超焦距），首帧必然 MOVING。
      ④ 锁定参数落点是否可复现（焦点/物理镜头/输出尺寸/帧时长）

    **④ 通过不等于"内参不变已被证实"**，这一点必须写死在结论里：
    HAL 对 LENS_FOCUS_DISTANCE 的量化单位实测是 **1 mm 距离**
    （回读 1.001001 diopt ⟺ 999.0000 mm；0.09071941 diopt ⟺ 11023 mm），
    所以回读值很可能是"把请求量化到执行器网格后回显"，而**不一定是实测位置**。
    它只能证明"请求的落点确定且可复现"，证明不了"镜片物理上停在同一处"。
    内参是否真的不变，最终只能靠 M1 做两次独立标定比对 fx。

    数据来源：m0_repeat.json（优先）或 m0_locked_capture_r*.json（兜底）。
    """
    rounds = [r for r in (rounds or []) if isinstance(r, dict)]
    if not rounds and not repeat:
        res.add_unverified("G21", "跨会话内参重复性（未验证）",
                           "未运行 App 的③重复性测试（每轮重开关摄像头），或未取回 "
                           "m0_locked_capture_r*.json -> 「一次标定能否覆盖多次采集」"
                           "是内参可复现性的地基，这一条没测就不能判定机型合格。"
                           "请点③后再重跑本脚本")
        return

    if repeat and isinstance(repeat.get("rounds"), list) and len(repeat["rounds"]) >= 1:
        rounds = repeat["rounds"]

    if len(rounds) < 2:
        res.add("G21", WARN, "跨会话重复性只有 1 轮数据",
                "至少需要 2 轮（3 轮更稳）才能谈重复性 -> 请重跑③")
        return

    focus, pids, sizes, fds, temps = [], set(), set(), set(), []
    bad, parts = [], []
    n_gap_rounds = 0

    # ---- 逐轮：完整性 + 落点 ----
    per_round = []
    for r in rounds:
        tag = str(r.get("round") or "?")
        f = r.get("focus_actual_diopter")
        if isinstance(f, (int, float)) and f > 0:
            focus.append(f)
        p = r.get("active_physical_id")
        if p not in (None, ""):
            pids.add(str(p))
        s = r.get("output_size")
        if s:
            sizes.add(json.dumps(s, sort_keys=True))
        fd = r.get("frame_duration_ns")
        if isinstance(fd, (int, float)) and fd > 0:
            fds.add(int(fd))
        if (r.get("moving_after_warmup") or 0) > 0:
            bad.append(f"{tag} 轮在预热区外仍出现 MOVING")
        t = r.get("battery_temperature_c")
        if isinstance(t, (int, float)):
            temps.append(t)

        seg = f"{tag}: 焦点={_fmt6(f)} diopt"
        nom, mx, ng = (r.get("nominal_interval_ns"), r.get("max_interval_ns"),
                       r.get("gap_count") or 0)
        if isinstance(mx, (int, float)) and isinstance(nom, (int, float)) and nom > 0:
            if ng:
                n_gap_rounds += 1
                seg += (f" 间隔max={mx / 1e6:.3f}ms(={mx / nom:.1f}×名义) "
                        f"丢帧={ng}@间隔序号{r.get('gap_at')} **该轮时间轴有断口**")
            else:
                seg += f" 间隔max={mx / 1e6:.3f}ms(={mx / nom:.3f}×名义) 无丢帧"
        per_round.append(seg)

    if n_gap_rounds:
        bad.append(f"{n_gap_rounds}/{len(rounds)} 轮存在丢帧（帧间隔≥"
                   f"{ROUND_GAP_RATIO}×名义值）-> 该轮帧时间轴有断口，"
                   "运动建模与标定都不得直接用；采集协议必须逐帧校验间隔，"
                   "发现断口即弃用该轮并重采")

    # ---- 会话新鲜度：每轮首帧是否从掉电默认位置起跳 ----
    fresh, stale = [], []
    for r in rounds:
        tag = str(r.get("round") or "?")
        hf, work = r.get("hyperfocal_diopter"), r.get("focus_requested_diopter")
        f0, st = r.get("first_frame_focus_diopter"), r.get("first_frame_lens_state")
        if f0 is None:
            continue
        near_reset = (isinstance(hf, (int, float)) and hf > 0
                      and abs(f0 - hf) < abs(f0 - (work or 0)))
        (fresh if near_reset else stale).append(tag)
        if not near_reset:
            bad.append(f"{tag} 轮首帧焦点={_fmt6(f0)}diopt 不是掉电默认值"
                       f"（超焦距 {_fmt6(hf)}）-> 摄像头很可能没有被真正重开，"
                       "该轮与上一轮共享 session，「跨会话」不成立")
    if fresh:
        parts.append(f"会话新鲜度: {len(fresh)}/{len(fresh) + len(stale)} 轮首帧"
                     f"回到掉电默认（超焦距）并重新移动 -> 每轮确为独立 openCamera")

    # ---- 落点重复性 ----
    if len(focus) >= 2:
        spread = max(focus) - min(focus)
        rel = spread / min(focus) * 100.0
        parts.append(f"焦点回读={['%.6f' % v for v in focus]} diopt, "
                     f"极差={spread:.6f} (相对 {rel:.3f}%, 判据 <{REPEAT_FOCUS_OK_PP}%)")
        if rel >= REPEAT_FOCUS_OK_PP:
            bad.append(f"焦点回读相对极差 {rel:.3f}% ≥ {REPEAT_FOCUS_OK_PP}%，"
                       "每次采集前都要重标定，或把内参按回读值索引")
    else:
        bad.append("两轮以上没取到焦点回读值")

    parts.append(f"生效物理镜头={sorted(pids) if pids else '未取到'}")
    if len(pids) > 1:
        bad.append(f"各轮生效物理镜头不一致 {sorted(pids)} -> 内参在各轮之间就变了")
    parts.append(f"输出尺寸={len(sizes)} 种")
    if len(sizes) > 1:
        bad.append("各轮输出尺寸不一致 -> 输出尺寸是内参的一部分，必须冻结")
    parts.append(f"回读帧时长={sorted(fds) if fds else '未取到'}")
    if len(fds) > 1:
        bad.append(f"各轮回读帧时长不一致 {sorted(fds)} -> 至少说明帧率档位没锁住")
    if temps:
        parts.append(f"电池温度={[round(t, 1) for t in temps]}C（跨轮差 "
                     f"{max(temps) - min(temps):.1f}C，弱对照，非热漂移证据）")

    detail = "; ".join(parts)
    if per_round:
        detail += " || 逐轮: " + "; ".join(per_round)

    if bad:
        res.add("G21", WARN, "跨会话内参重复性不达标",
                detail + " -> " + "；".join(bad))
    else:
        res.add("G21", PASS, "跨会话内参重复性良好",
                detail + " -> 一次标定可覆盖多次采集"
                "（注意：这只证明「锁定参数的落点可复现」，"
                "≠「内参已被标定证实不变」——回读值很可能只是把请求量化后回显"
                "（实测量化单位=1 mm 距离），后者要等 M1 做两次独立标定比对 fx）")


def _fmt6(v):
    return f"{v:.6f}" if isinstance(v, (int, float)) else str(v)


def evaluate_output_geometry(locked, cam, res):
    """G22 输出几何自洽性 —— crop region 与输出的纵横比必须一致。

    为什么这必须是独立判据：`SCALER_CROP_REGION` 的语义是"把这块活动阵列区域
    映射到输出"。若它与输出纵横比不同，映射就是**各向异性**的，等价于一次
    非等比缩放，于是：

      - `fx != fy`（例如 4:3 活动阵列映射到 16:9 输出时 fy = 0.75·fx），
        标定模型里"方形像元"的前提直接失效；而标定仍会收敛、画面看着"正常"
      - 输出覆盖面被白扔（16:9 只用到活动阵列的 75% 高度）
      - fx 也被砍掉（本机 1920x1080 上 fx=1282.5 px，而 4:3 全幅是 2736 px）
        —— 亚像素定位精度大致与 fx 成正比，等于主动放弃一半精度

    这类失效**不会报错**：它不是"参数没锁住"（G9 通过）、不是"时间戳不准"（G20 通过），
    恰恰是把内参模型的前提悄悄换掉了。所以必须单列。

    判定：
      - 纵横比一致（差 <1%）  -> PASS，并给出 fx=fy 的预测值
      - 不一致               -> WARN，给出"该设成什么 crop region"的具体数值
    """
    if not locked:
        res.add_unverified("G22", "输出几何自洽性（未验证）",
                           "未运行锁定连拍验证 -> 无法确认 crop region 与输出的纵横比是否一致")
        return

    cfg = locked.get("capture_config") or {}
    out = cfg.get("output_size") or {}
    active = cfg.get("active_array") or {}
    crop = cfg.get("crop_region") or {}
    if not crop.get("width"):
        for f in (locked.get("frames") or []):
            if f.get("crop_region"):
                crop = f["crop_region"]
                break

    if not out.get("w") or not out.get("h") or not crop.get("width") or not crop.get("height"):
        res.add("G22", INFO, "输出几何信息不全，无法判定",
                f"output={out or '无'} crop={crop or '无'} -> 请用新版 App 重跑②"
                "（新版会写 capture_config.crop_region 与 output_to_crop_scale_*）")
        return

    asp_out = out["w"] / out["h"]
    asp_crop = crop["width"] / crop["height"]
    rel_pp = abs(asp_out - asp_crop) / asp_out * 100.0
    sx = out["w"] / crop["width"]
    sy = out["h"] / crop["height"]

    # fx 预测：优先用 App 记录的 output_to_crop_scale_x（含自动裁切后的真实比例）；
    # 老报告没有该字段时回落到 crop 尺寸自算 —— 一律从几何重算，不采信自报值。
    op = (cam or {}).get("optics") or {}
    fx_full = op.get("fx_px_full_array")
    fx = fy = None
    if isinstance(fx_full, (int, float)):
        fx, fy = fx_full * sx, fx_full * sy

    geo = (f"输出 {out['w']}x{out['h']} (纵横比 {asp_out:.4f}) vs "
           f"crop {crop['width']}x{crop['height']} (纵横比 {asp_crop:.4f}); "
           f"缩放 scale_x={sx:.5f} scale_y={sy:.5f}")
    if fx is not None:
        geo += f"; 光学预测 fx={fx:.1f} px, fy={fy:.1f} px, fy/fx={fy / fx:.4f}"

    active_txt = ""
    if active.get("w"):
        same = (crop["width"] == active["w"] and crop["height"] == active["h"])
        active_txt = ("; crop 覆盖整个活动阵列（无裁切）" if same
                      else f"; crop 只覆盖活动阵列的 "
                           f"{crop['width']}x{crop['height']}（已裁切）")

    if rel_pp < 1.0:
        res.add("G22", PASS, "输出几何自洽：crop 与输出同纵横比",
                geo + active_txt + " -> fx==fy 成立，可安全使用方形像元模型")
    else:
        # 反算"应当设成什么 crop region"，让结论可执行而不只是报警
        want_h = int(round(out["h"] * crop["width"] / out["w"]))
        top = (active.get("h", crop["height"]) - want_h) // 2 if active.get("h") else 0
        fix = (f"应把 crop region 改成以输出纵横比在活动阵列上取最大居中矩形："
               f"约 ({crop.get('left', 0)}, {top}, {crop['width']}, {want_h})"
               if active.get("h") else "应改用与输出同纵横比的 crop region")
        res.add("G22", WARN, "输出几何不自洽：crop 与输出纵横比不一致",
                geo + active_txt
                + f" -> 纵横比差 {rel_pp:.2f}% 意味着一对各向异性的缩放："
                "方形像元假设失效（fx != fy），画面本身也带一次非等比缩放，"
                "而标定会照常收敛、程序不会报错。" + fix
                + "。更省事的做法是把输出尺寸换成与活动阵列同纵横比的档位"
                  "（例如 2048x1536），这样 crop 用整个活动阵列就天然自洽，"
                  "同时保住 fx 与纵向视场")


def _config_signature(report):
    """从一份连拍报告里抽出"决定内参的那几个配置量"。

    **刻意不含 skew**：实测 skew 是「输出纵横比 / crop」的派生量，
    把它算进"配置是否一致"会得出反向的错误结论 —— skew 不同恰恰是**配置生效的证据**。

    **也不含 app_version**（v1.6 修正，v1.5 含了是个错误）："采集配置不同"与
    "代码版本不同"是两个独立问题，混在一个键里会导出错误处置。实测踩过 ——
    ② 那份报告只是由上一版 APK 写的（输出尺寸其实与 ③ 完全相同），
    结果判据报"跨配置，请重跑 ③"，正确处置却是"**重跑 ②**"。
    app_version 作为独立字段返回，单独成组比对。
    """
    cfg = (report or {}).get("capture_config") or {}
    out = cfg.get("output_size") or {}
    crop = cfg.get("crop_region") or {}
    return {
        "output_size": (f"{out.get('w')}x{out.get('h')}" if out.get("w") else None),
        "crop": (f"{crop.get('width')}x{crop.get('height')}"
                 f"@{crop.get('left')},{crop.get('top')}" if crop.get("width") else None),
        "format": cfg.get("output_format"),
        "warmup": (report.get("params") or {}).get("warmup_frames"),
        "app_version": (report or {}).get("app_version"),
        "fingerprint": cfg.get("config_fingerprint"),
    }


def _config_key(sig):
    """配置分组键 —— 只用**所有版本都一定会记**的字段。

    ⚠ 不要用 App 自报的 `config_fingerprint` 当分组键（v1.5 这么做了，是个坑）：
    旧版 App 的报告没有这个字段，于是"没有指纹"与"有指纹"的键**永远不相等**，
    哪怕两者的采集参数完全相同，也会被判成"跨配置"。实测踩过 ——
    用户重跑 ③ 之后四份报告的几何量已完全一致，判据却仍报"跨配置"。
    正确做法：分组键由**原始字段**现算（输出几何 + 格式 + 预热帧数），
    指纹只作为**附加校验**，不参与分组。
    """
    return f"{sig['output_size']}|{sig['format']}|w{sig['warmup']}"


def evaluate_report_set(locked, round_reports, sweep, res):
    """G23 报告集自洽性 —— 手上的报告是不是同一套配置、同一批次、同一代码版本。

    为什么需要它：判据天然只会比较"这些报告彼此之间"（例如 G21 统计各轮的
    输出尺寸是否一致）。若整批报告都是**旧配置 / 旧版本**的产出，它们彼此
    完全一致，判据照样给 PASS —— 而它们与**将要交付的配置**根本不是一回事。
    这不是假想，是实测踩到的（三次，每次暴露的盲区都更深一层）：

      第 5 轮：6 份报告里 3 份打的是 16.5 小时前的墙钟，输出 1920x1080，且是旧版 App
              （capture_config 里连 crop_region 都没有）；主连拍已是 1920x1440。
              G21 报"输出尺寸=1 种"，只在那 3 轮内部比较，看不见自己整批都过期了。
      第 6 轮：用户重跑了 ③ 之后，四份报告的输出尺寸/几何量**已经完全相同**，
              唯一差别是主连拍由**上一版 APK** 写出（无 app_version / 无指纹）。
              此时判据仍报"输出尺寸/App 版本不同，请重跑 ③" —— **归因与处置都错了**，
              正确处置是"重跑 ②"。根因是把 app_version 掺进了配置指纹。
      第 7 轮：按上一条改完后用户重跑了 ②，于是 ② 变新版本、③ 留在旧版本，
              判据又要求"重跑 ③" —— 而两者**采集路径完全相同**（差异只在指纹串
              与新增报告字段）。⇒ 把版本号当判据会**无限循环**，每改一行代码就
              让 ③ 作废一次。改为判"采集路径修订号"，并把版本差异降级为注记。
      第 8 轮：给 App 加了**第六条采集路径**（⑥ 拍照）。若修订号仍是"App 全局
              计数器"，那么"加一条新路径"会把连拍路径的证据一起作废 —— 同一个
              返工循环换个形式再犯一次。⇒ 路径改为 **(id, rev) 这一对**，
              逐路径各自计数；本版新增 ⑥ 时 burst_yuv_manual 的 rev 保持 1。

     四条检查（第 10 轮起为五条）：
       1. 设备身份一致性：所有报告的 `device.build_fingerprint` 必须相同
       2. 配置一致性：按"输出几何+格式+预热帧数"现算指纹分组，必须只有一组
       3. 采集路径一致性：(capture_path_id, capture_path_rev) 必须成对相同
          （**不是**版本号相同，也**不是**只看 rev）；
          版本号不同且路径不可解析 -> 不可判定（fail closed）
       4. skew 归属：在多种输出纵横比/crop 之间检验三套归一化口径哪个恒定
       5. 批次新鲜度：报告生成时间的跨度 —— **第 10 轮降级为注记**，见下

     【为什么第 10 轮把"批次新鲜度"从 WARN 降为注记】
     原先的规则是"报告集时间跨度 > 60 min ⇒ WARN，整批重跑"。它的问题不是
     "太严"，而是**与它声称要防的风险没有因果关系**：
       · 它要防的是"把不同状态下的数据当成同一批证据"，其中真正会改变结论的是
         **换了机器 / 升了固件**（fx、读出时间、卷帘行为都会变）。
       · 而"同一台机器隔了几小时再跑一遍"跨度同样变大 —— 这**恰恰是 ③ 想要的
         条件**：G21 的主题就是跨会话可复现，它要求掉电重开摄像头。跨度大是
         证据更强的标志，不是更弱。
       · 于是这个代理**既会漏**（快速换机时跨度没超限）**也会误**（隔夜重跑）。
     实测踩到：第 10 轮用户重跑 ② 之后，② 与 6h53m 前的 ③ 三轮配置指纹、采集
     路径 (id, rev) 全部相同（1.8.0 与 1.5.0 产出的时间戳结构也逐帧一致：
     139/139、5 帧组和恒为 166440000 ns），判据却只因"跨度 6h53m"要整批重跑。
     ⇒ 改为直接比**设备与固件标识**（报告 1.8.1 起自带），跨度只作注记。
     【保留的残余风险】报告里没有任何字段能反映"热状态"与"现场是否被改过"，
     那部分只能靠 ③ 的多轮独立会话（G21）与长时热漂移测试覆盖。
    """
    items = []
    if isinstance(locked, dict):
        items.append(("主连拍", locked))
    for i, r in enumerate(round_reports or [], 1):
        if isinstance(r, dict):
            items.append((f"第{i}轮", r))
    if len(items) < 2:
        res.add("G23", INFO, "报告集只有 1 份，无从比对配置",
                "需要主连拍 + 至少一轮 ③ 才能判断它们是否同一套配置")
        return

    sigs = [(name, _config_signature(rep), rep.get("generated_at_wall_ms"))
            for name, rep in items]

    # ---- 0) 设备身份一致性 ----
    # 本项目全部判据都在回答"**这台机器**能不能当测量相机"，而配置指纹与采集
    # 路径**证明不了**这一点：两台机器的这两项可以完全一致，fx / 读出时间 /
    # 卷帘行为却不同。所以先比设备与固件标识本身。
    devs, dev_missing = {}, []
    for name, rep in items:
        k, src = _device_key(rep)
        if k:
            devs.setdefault(k, []).append(name)
        else:
            dev_missing.append(f"{name}（{src}）")
    dev_mixed = len(devs) > 1
    dev_txt = ""
    if devs:
        dev_txt = "设备/固件分组: " + "；".join(
            f"[{'/'.join(v)}] {k}" for k, v in devs.items())
    if dev_missing:
        _miss = ("；⚠ 未带设备标识：" + "、".join(dev_missing)
                 + "⇒ 本次无法独立排除「报告来自不同机器」"
                   "（device 段自 1.8.1 起写入）")
        dev_txt = (dev_txt + _miss) if dev_txt else _miss.lstrip("；")

    # ---- 1) 配置一致性（只看采集参数）----
    groups = {}
    for name, s, _ in sigs:
        groups.setdefault(_config_key(s), []).append(name)
    mixed = len(groups) > 1
    # crop 是分组键的**附加**校验：旧版报告没有这个字段，所以只在"记了的几份之间"比，
    # 不能因为"有一份没记"就判成跨配置（那会把记录差异当成配置差异）。
    crops = {s["crop"] for _, s, _ in sigs if s["crop"]}
    crop_mixed = len(crops) > 1
    if crop_mixed:
        mixed = True
    grp_txt = "；".join(
        f"[{'/'.join(v)}] {k}" for k, v in groups.items())
    if crop_mixed:
        grp_txt += "；crop 分组: " + "、".join(sorted(crops))

    # ---- 2) 采集路径一致性（问 (id, rev)，**不问版本号**，也不能只问 rev）----
    # 【为什么不是「版本号一致」】版本号会因为**纯报告字段、注释、UI 改动**而变，
    # 那类改动不改变采集行为，却会让「一次标定覆盖多次采集」的证据被永久判废。
    # 实测踩过：第 6 轮让用户重跑 ②，重跑后 ② 是新版本、③ 三轮留在旧版本，
    # 判据于是要求「重跑 ③」—— 而两者采集路径完全相同（差异只在指纹串与新增
    # 报告字段）。这条规则**会无限循环**：以后每改一行代码都得重跑 ③ 才罢休。
    # 【为什么也不能只问 rev】第 8 轮给 App 加了第六条采集路径（⑥ 拍照）。若 rev
    # 是"App 全局计数器"，加一条新路径就会把连拍路径的证据一起作废 —— 同一个
    # 返工循环换个形式再犯。所以路径是 **(id, rev) 这一对**，逐路径各自计数。
    # 判法：
    #   各报告版本号完全相同 -> 同一份代码 -> 采集路径必然相同（不必查表）
    #   版本号有差异         -> 逐个解析 (id, rev)（报告自带字段 > 登记表）
    #        都解析出来且成对相同 -> 采集路径相同，仅把版本差异作注记
    #        有解析不出来的       -> **不可判定**（fail closed，不是「没问题」）
    #        解析出来不同         -> 采集路径不同，跨会话证据可能无效
    vgroups = {}
    for name, s, _ in sigs:
        vgroups.setdefault(s.get("app_version") or "(未记录)", []).append(name)
    ver_mixed = len(vgroups) > 1
    ver_txt = "；".join(f"[{'/'.join(v)}] {k}" for k, v in vgroups.items())
    paths = {name: _resolve_capture_path(rep) for name, rep in items}
    known = all(p is not None for p, _ in paths.values())
    path_mixed = known and len({p for p, _ in paths.values()}) > 1
    path_unknown = [f"{n}（{src}）" for n, (p, src) in paths.items() if p is None]
    # 哪些报告的路径是**登记表声明**的（那些版本不自描述）。
    # 必须说出来：这类报告的"路径相同"是**声明的一致**，不是测量的一致 ——
    # 它们全都查同一条登记记录，所以这条检查对它们没有信息量。
    # （报告自描述自 1.8.0 起；1.5.0~1.7.0 都靠登记表。）
    path_declared = [f"{n}（{src}）" for n, (p, src) in paths.items()
                     if p is not None and "登记表" in str(src)]
    path_txt = ""
    if ver_mixed:
        path_txt = "采集路径 (id, rev): " + "，".join(
            f"{n}={('(' + p[0] + ', ' + str(p[1]) + ')') if p else '未登记'}"
            for n, (p, _) in paths.items())
    path_note_txt = ""
    if path_declared:
        path_note_txt = ("⚠ 以下报告的 (id, rev) 来自**登记表声明**、而非报告自带"
                         "（这些版本不自描述）：" + "、".join(path_declared)
                         + "⇒ 它们之间的「路径相同」是**声明的一致**，"
                           "不是测量的一致。报告自描述自 1.8.0 起")

    # ---- 3) 批次新鲜度 ----
    times = [t for _, _, t in sigs if isinstance(t, (int, float))]
    spread_s, batch_txt = None, ""
    if len(times) >= 2:
        spread_s = (max(times) - min(times)) / 1000.0
        mins, rem = divmod(int(spread_s), 60)
        hrs, mins = divmod(mins, 60)
        batch_txt = (f"报告集时间跨度={hrs}h{mins}m{rem}s"
                     if hrs else f"报告集时间跨度={mins}m{rem}s")
        oldest = min(sigs, key=lambda x: x[2] if isinstance(x[2], (int, float)) else 1 << 62)
        newest = max(sigs, key=lambda x: x[2] if isinstance(x[2], (int, float)) else -1)
        batch_txt += f"（最早={oldest[0]}，最新={newest[0]}）"

    # ---- 4) skew 的自洽性（**刻意不做归属判定**）----
    # 【为什么本段不判归属】"ns/crop行数" 与 "ns/读出窗口行数" 在**输出纵横比与
    # crop 一致**时是同一个数：read_rows = crop_w·outH/outW，而 crop 与输出同纵横比
    # 时 outH/outW = crop_h/crop_w ⇒ read_rows = crop_h。而 ②③ 各报告正是这种配置，
    # 所以在本报告集里这两个口径**不可能被分开** —— 任何"恒定的是它"都只是
    # min() 在相等值里挑到了排在前面的那个，是任意的。
    # 实测踩过（第 8 轮）：本段曾输出"恒定的是 ns/输出行数"，而那是第 5 轮就被
    # 证伪的口径；根因是四份报告只有一种几何，三列极差全为 0。
    # 能分开它们的只有 ⑤ 的**跨纵横比判别档**（16:9 输出 + 全阵列 crop：
    # read_rows=2304 而 crop_h=3072）—— 所以**归属判定放在 G24**，
    # 本段只判"同一几何配置内 skew 是否自洽"。
    skew_by_geo = {}
    asym = []
    for name, rep in items:
        sk, _src = _skew_value(rep)
        if not sk:
            continue
        n = _skew_norm(rep.get("capture_config") or {}, sk)
        if not n:
            continue
        if n["asymmetric_crop"]:
            asym.append(name)
        key = (f"{n['out_w']}x{n['out_h']}/crop{n['crop_w']}x{n['crop_h']}")
        skew_by_geo.setdefault(key, {}).setdefault(int(sk), []).append(name)
    skew_anom = []
    for k, v in skew_by_geo.items():
        if len(v) > 1:
            skew_anom.append(f"{k} 出现了多个 skew：" +
                             "、".join(f"{s} ns({'/'.join(ns)})" for s, ns in sorted(v.items())))
    skew_txt = ""
    if skew_by_geo:
        skew_txt = ("各几何配置的 skew：" + "；".join(
            f"{k}→{sorted(v)[0]} ns" for k, v in skew_by_geo.items())
            + "。**本段不做 skew 归属判定**：「ns/crop行数」与「ns/读出窗口行数」在"
              "输出纵横比与 crop 一致时是**同一个数**（read_rows = crop_h），"
              "而 ②③ 全是这种配置 ⇒ 两个口径在此无法分离，任何「恒定的是它」都是任意的。"
              "归属只能由 ⑤ 的跨纵横比判别档判定（见 G24 / G14）")
    if asym:
        skew_txt += (f"。⚠ {'、'.join(asym)} 的 crop 纵横比与输出不一致（各向异性），"
                     "fx≠fy，其几何不可用于标定")

    parts = [f"配置分组: {grp_txt}"]
    if dev_txt:
        parts.insert(0, dev_txt)
    if ver_mixed:
        parts.append(f"代码版本分组: {ver_txt}")
        if path_txt:
            parts.append(path_txt)
    if batch_txt:
        parts.append(batch_txt)
    if path_note_txt:
        parts.append(path_note_txt)
    if skew_txt:
        parts.append(skew_txt)
    # 哪些成员与主连拍不同（分配置/版本两类说清，便于直接看出该重跑哪一步）
    main_sig = sigs[0][1]
    stale_cfg = [name for name, s, _ in sigs
                 if _config_key(s) != _config_key(main_sig)]
    detail = "; ".join(parts)

    if dev_mixed:
        res.add("G23", WARN, "报告集跨设备/固件：产出不是同一台机器",
                detail + " -> 各组设备/固件标识不同。**这是最严重的一类混批**："
                "配置指纹与采集路径可以完全一致，而 fx、读出时间、卷帘行为"
                "都可能不同 ⇒ 把它们放在一起比较，等于把两台机器的差异记到"
                "被研究的那个变量头上。处置：只用**当前这台机器**的产出；"
                "若确实要合并多台，必须按 (device, 配置, 路径) 分组分别下结论")
    elif mixed:
        res.add("G23", WARN, "报告集跨配置：部分产出不属于交付配置",
                detail + f" -> {('、'.join(stale_cfg))} 与主连拍不是同一套采集配置"
                "（输出几何 / crop / 格式 / 预热帧数）。**「一次标定覆盖多次采集」的证据"
                "必须来自交付配置**，否则它证明的是另一套配置的可复现性。"
                "处置：用当前版本重跑这些报告对应的步骤（② / ③），并确认 "
                "capture_config.config_fingerprint 与主连拍一致")
    elif path_mixed:
        res.add("G23", WARN, "报告集跨采集路径：部分产出不是同一份采集行为",
                detail + " -> 各报告的 (capture_path_id, capture_path_rev) 不同。"
                "「一次标定覆盖多次采集」要求的是**采集行为相同**，而配置指纹只能"
                "证明「请求参数」相同，证明不了「同一份代码在同一条路径上采集」。"
                "处置：在当前采集路径下重跑不一致的那几步（②/③）；"
                f"若确认差异与采集无关，则在同一文件 {CAPTURE_REV_MAP_FILE} 里"
                "把它们登记为同一 (id, rev)，并写明依据（说明相对上一版动了什么）")
    elif ver_mixed and not known:
        res.add("G23", WARN, "报告集跨版本，且采集路径是否相同**不可判定**",
                detail + " -> 未登记进 " + CAPTURE_REV_MAP_FILE + " 的产出："
                + "、".join(path_unknown)
                + "。**这不是「没异常」，是「判不了」**：版本号不同意味着代码有差异，"
                "而差异是否触及采集路径（请求参数/会话配置/模板/输出格式/单张 vs "
                "重复请求/crop 与选档/帧率下发/预热与丢弃规则/锁定字段集合）"
                "只能由人审阅确认。"
                "处置：为该版本补一条 (capture_path_id, capture_path_rev) 并写明"
                "相对上一版动了什么；若确实触及采集路径，再重跑对应步骤。"
                "（不要为了「让它变绿」而随手填一个与别人相同的值 —— 那只会把"
                "不可判定伪装成已验证）")
    elif skew_anom:
        res.add("G23", WARN, "同一几何配置内部 skew 不一致",
                detail + " -> " + "；".join(skew_anom)
                + "。同一套输出几何带来的 skew 必须相同（它是「输出纵横比 / crop」"
                "的派生量）—— 出现差异说明有**未被记录的变量**在变"
                "（例如 pixel mode、不同物理镜头、或报告字段被手工改动）。"
                "处置：逐帧比对 capture_config 与 lock_stability，找出真正在变的量")
    elif spread_s is not None and spread_s > REPORT_BATCH_MAX_S:
        # 第 10 轮：从 WARN 降为 PASS + 注记。理由见函数开头（该代理与它要防的
        # 风险无因果关系：跨度大既会漏"快速换机"，也会误"同机隔夜重跑"，
        # 而后者**正是 ③ 想要的跨会话条件**）。真正该比的是设备/固件标识，
        # 已由上面的 dev_mixed 分支承担。
        res.add("G23", PASS, "报告集自洽：同一台设备、同一套配置、同一采集路径",
                detail + f" -> 时间跨度 {spread_s / 60:.0f} 分钟（> "
                f"{REPORT_BATCH_MAX_S / 60:.0f} 分钟）**不判为问题**：跨度本身不是"
                "风险，它只提示「这些产出不是一次跑完的」。③ 的主题就是跨会话"
                "可复现（要求掉电重开摄像头），跨度大恰恰是它想要的条件。"
                "**残余风险**：报告里没有任何字段反映「热状态」与「现场是否被改过」，"
                "那部分由 ③ 的多轮独立会话（G21）与长时热漂移测试覆盖。")
    else:
        _extra = ""
        if ver_mixed:
            _extra = (" -> 版本号有差异，但采集路径 (id, rev) 相同（按登记表/报告自带字段核定），"
                      "采集行为一致，跨会话证据**有效**；差异属报告字段层面。"
                      "注：该结论依赖「版本号与代码一一对应」，"
                      "所以发版时必须每次 +1（哪怕只改注释）")
        res.add("G23", PASS, "报告集自洽：同一套配置、同一采集路径、同一批次",
                detail + _extra + " -> 各判据的证据确实覆盖当前交付配置")


def evaluate_timestamp_uncertainty(locked, cam, res, still=None):
    """G25 时间戳的**参考行**与**单帧固有不确定度** —— 把 skew 接到"时间戳准确"上。

    【为什么单开一条】
    G14 只证明了"skew 逐帧可取得且恒定"。那句话本身**不构成**任何关于时间戳
    准不准的结论 —— 它是一个中间量。交付方式定为"拍照"之后，真正决定时间戳
    精度的是另外两件事，而它们此前都没被判据消费：

      1. **参考行是否就是图像首行。**
         `SENSOR_TIMESTAMP` 的语义是"活动阵列**第一行**的曝光起点"（AOSP 原文，
         方案 §8.1 已核对）。而输出图像的第 0 行对应活动阵列的第 `crop.top` 行。
         crop 从 0 起始时两者重合（偏置 0）；一旦 crop 为了迁就输出纵横比而
         纵向收窄（本机 16:9 档 crop.top = 384），输出首行就比时间戳晚
         `crop.top × 行时间`。
         这个偏置**不会报错**：画面正常、时间戳单调、抖动为 0 —— 只是整体偏了。
         它属于"静默失效"，正是本项目反复踩的那一类。

      2. **一张照片本就没有"一个时刻"。**
         卷帘快门让首末行的曝光起点相差 skew（本机 4:3 档 9.21 ms，占 33.288 ms
         帧周期的 27.7%）。所以"把整张照片当作某一时刻"的模型，其固有不确定度
         下限就是 skew。它**不能靠多张平均消掉**（同一张图内部的空间-时间耦合），
         只能①记账，或②限定只用某一列/行像素做测量。

    【判据判什么、不判什么】
    判：交付配置的参考行偏置是否为 0；skew 是否逐帧可复现。
    **不判**：skew 有多大 —— 它是否可接受取决于下游运动速度，那是方案参数，
    不是机型属性。这里把它算出来写进结论，逼下游去核对，而不是替它拍板。
    """
    cfg = ((locked or {}).get("capture_config") or {})
    if not cfg:
        res.add("G25", INFO, "无锁定连拍报告，无法判定时间戳的参考行",
                "需要 m0_locked_capture.json（②）")
        return
    crop = cfg.get("crop_region") or {}
    skew, skew_src = _skew_value(locked)
    try:
        crop_top = int(crop.get("top"))
    except (TypeError, ValueError):
        crop_top = None

    if skew is None:
        res.add("G25", WARN, "取不到卷帘读出时间，单帧时间戳的不确定度无法量化",
                "报告里没有可用的 rolling_shutter_skew_ns（" + skew_src + "）。"
                "它同时是「参考行偏置」的换算系数与「单帧固有不确定度」本身，"
                "缺它无法判定。注意：这可能是**解析问题**而不是缺数据 —— "
                "lock_stability 里的 values 存的是字符串，须显式转换")
        return

    n = _skew_norm(cfg, skew)
    if not n:
        res.add("G25", WARN, "读出窗口行数不可解析（输出/h 或 crop/w 缺失）",
                f"skew={skew} ns，缺输出尺寸或 crop 宽度 -> 无法换算行时间")
        return
    lt = n["line_time_ns"]
    bias = (crop_top or 0) * lt

    # 交付配置（crop 与输出同纵横比、未收窄）为基准；否则按实测档位说明
    delivery = not n["asymmetric_crop"] and (crop_top == 0)
    unc_txt = (f"skew={skew} ns（= 读出窗口 {n['read_rows']:.1f} 行 × {lt:.1f} ns/行）")

    if crop_top is None:
        res.add("G25", WARN, "crop 顶部偏移未知，参考行偏置无法判定",
                unc_txt + "。报告缺 capture_config.crop_region.top，"
                "而它正是把「时间戳参考行（活动阵列第 0 行）」换算到「图像第 0 行」"
                "的唯一系数。缺它判不了，不要当成 0")
        return

    if crop_top != 0:
        res.add("G25", WARN, "时间戳参考行与图像首行不重合，须显式补偿",
                unc_txt + f"；crop.top={crop_top} ⇒ 图像第 0 行的曝光时刻比"
                f"SENSOR_TIMESTAMP 晚 {bias / 1e6:.4f} ms"
                f"（= {crop_top} 行 × {lt:.1f} ns/行）。"
                "这是**静默**偏置：画面正常、时间戳单调、抖动为 0，只是整体偏了。"
                "处置：在流水线里按 `(crop.top + 行号) × 行时间` 记账，"
                "或把输出档换回 crop.top==0 的档（交付档 4:3 全阵列 crop 即是）")
        return

    # crop.top == 0：参考行就是图像首行，偏置 0
    img_last_ms = (n["out_h"] - 1) * (n["crop_h"] / n["out_h"]) * lt / 1e6 if n["out_h"] else None
    res.add("G25", PASS, "时间戳参考行与图像首行重合；单帧不确定度已量化",
            unc_txt + "；crop.top=0 ⇒ 图像第 0 行的曝光时刻**就是** SENSOR_TIMESTAMP"
            "（参考行偏置 0 ns，无需补偿）。"
            + (f"单帧固有不确定度 = 整幅图的读出时间 = {skew / 1e6:.3f} ms"
               f"（占 33.288 ms 帧周期 {skew / 33288000.0 * 100:.1f}%；"
               f"图像末行的曝光起点在时间戳之后约 {img_last_ms:.3f} ms）"
               if img_last_ms else "")
            + " —— 卷帘快门让一张照片**没有单一时刻**，首末行相差这段时间，"
            "且**不能靠多张平均消掉**（同一张图内部的空间-时间耦合）。"
            "处置：要么把它作为时间戳的不确定度记账（时间戳±半幅 ≈ ±"
            f"{skew / 2e6:.2f} ms），要么限定只用某一列/行像素做测量。"
            "下游能否接受取决于运动速度 —— 那是方案参数，本判据不替它拍板"
            + ("。注：本配置即交付配置" if delivery else ""))


def evaluate_still_path(still, locked, cam, res, delivery="still"):
    """G26 拍照路径证据 —— 交付走拍照时，证据必须来自拍照那条路径。

    【本判据存在的唯一理由】
    交付要求是"回传使用拍照"，而②③④⑤**全部**是 `TEMPLATE_MANUAL` +
    `setRepeatingRequest` 的连续流（YUV_420_888）。模板、输出格式、调度方式
    三样都不同，**任何一样都可能改变时间戳的语义**：

      · `TEMPLATE_STILL_CAPTURE` 常触发零快门延迟(ZSL) / 多帧合成(MFNR/HDR)。
        一旦开启，交出的可能是**环形缓冲里的旧帧**，`SENSOR_TIMESTAMP` 随之
        指向更早的时刻 —— 画面与通知完全正常，偏差却不受控且逐张随机。
        连续流里根本不存在这个风险，所以**只有单张模式才测得到**。
      · 输出格式换成 JPEG 后，EXIF 的 `DateTimeOriginal` 是**系统墙钟**，
        精度由厂商决定。若 PC 端拿 JPEG 的 EXIF 当时间戳，误差可达秒级。
        这件事必须实测（有无 `SubSecTime`、几位），不能凭常识断言。

    所以：**集成路径变了，就是证据基础变了。** 这条判据把"有没有在拍照路径上
    取证"变成一条可判的事；缺证据时判**不可判定**（INCOMPLETE），而不是
    "没异常"—— 否则等于用一条从没跑过的路径给交付放行。

    检查清单（第 10 轮按真机数据补全）：
      1. 对照档相对②是不是只动了"单张 vs 重复请求"一个变量（**指纹逐段点名**）
      2. 对照档的 skew 与②是否相同（相同才能把②的读出模式结论外推到单张）
      3. 交付档的参考行偏置是否为 0（`crop.top==0`，否则是静默偏置）
      4. 交付档是否拿到 ZSL 旧帧 / 逐张时间戳是否严格递增（两条独立证据）
      5. **图与结果是否配对**（`Image.timestamp` vs `SENSOR_TIMESTAMP`）——
         交付物是"图+时间戳"，此前只验证了时间戳那一半
      6. 是否触发多帧合成（ISP 档位被改写为 ZSL/HIGH_QUALITY）
      7. EXIF 能否当时间源（亚秒位数、且它是系统墙钟）
      8. 回调到达抖动（**注记**，不判帧序一致性，见文件顶部注释）
    """
    if delivery != "still":
        res.add("G26", INFO, "拍照路径证据（交付路径=连续流，本项不适用）",
                "调用方声明交付=连续流/抽帧 ⇒ ②③④⑤ 的连续流证据即为交付路径证据。"
                "若交付实际是拍照，请用 --delivery still 重跑本判据")
        return

    def _j(o, k):
        v = (o or {}).get(k)
        return v if isinstance(v, (int, float)) else None

    if not isinstance(still, dict):
        res.add_unverified("G26", "拍照路径时间戳语义（未验证）",
                           "缺少 " + STILL_FILE + "。**这不等于没问题**："
                           "现有 ②③④⑤ 的证据全部来自 TEMPLATE_MANUAL + "
                           "setRepeatingRequest 的连续流（YUV_420_888），"
                           "而交付是拍照。模板 / 输出格式 / 调度方式三者任一不同，"
                           "时间戳的语义都可能跟着变（最典型的是 STILL 模板触发 ZSL："
                           "交出的可能是缓冲里的旧帧，时间戳与「按下那一刻」脱钩）。"
                           "**既有 PASS 一条也不能外推到拍照路径。**"
                           "处置：装当前 APK，跑 App 的⑥拍照路径，再重跑本脚本")
        return

    trials = [t for t in (still.get("trials") or []) if isinstance(t, dict)]
    by_id = {t.get("trial_id"): t for t in trials if t.get("trial_id")}
    yuv = by_id.get("still_yuv_single")
    jpg = by_id.get("still_jpeg_template")
    if not trials:
        res.add_unverified("G26", "拍照路径时间戳语义（报告为空）",
                           f"{STILL_FILE} 里没有 trials —— ⑥ 没跑成功")
        return
    if not jpg:
        res.add_unverified("G26", "拍照交付档（JPEG + TEMPLATE_STILL_CAPTURE）未验证",
                           "报告里没有 still_jpeg_template 这条 trial。"
                           "受控对照档（YUV 单张）只能证明「单张 vs 重复请求」是否等价，"
                           "证明不了交付档（JPEG + STILL 模板）的时间戳语义")
        return

    # 档**跑了但失败**（trial 里只有 error）—— 这与"没跑"是两种缺席，但都不可判。
    # 第 11 轮实测：⑥ 两档「摄像头被断开」，旧实现会继续往下走，把缺字段解释成
    # 一堆"缺失/无法确认"的 WARN，甚至在对照档分支报出"指纹一致"的假阳性。
    if jpg.get("error"):
        res.add_unverified("G26", "拍照交付档本次未跑成（无证据）",
                           f"still_jpeg_template 档失败：{jpg.get('error')}。"
                           "交付档是本判据的全部对象 —— 它没跑成，就没有任何可判的东西。"
                           "若错误是「摄像头被断开 / 摄像头错误 code=N」：这是**设备级瞬时"
                           "故障**（第 11 轮实测：同一分钟内的 ④⑤ 成组失败，约 20 秒后"
                           "自行恢复，随后 ③ 三轮全部成功），不是路径不支持。"
                           "1.8.2 起的 APK 打开摄像头遇此类故障会自动重试并留痕；"
                           "重跑⑥后重跑本脚本"
                           + ("。对照档本次也同样失败" if (yuv or {}).get("error") else ""))
        return

    fails, warns, notes = [], [], []
    yuv_failed = bool((yuv or {}).get("error"))
    if yuv_failed:
        warns.append(f"对照档本次未跑成（{yuv.get('error')}）⇒ 第 1/2 项对照检查"
                     "（指纹逐段比对、skew 一致性）**缺席**：交付档自身的检查仍然有效，"
                     "但「单张 vs 重复请求是否等价」这次没有证据。重跑⑥补齐")
    if yuv is None:
        warns.append("报告里没有 still_yuv_single 对照档 ⇒ 第 1/2 项对照检查缺席："
                     "「单张 vs 重复请求是否等价」没有证据，"
                     "「拍照路径与连拍一致」这句话无从谈起")

    # ---- 1) 对照档：与 ② 是否同几何同格式（受控实验的前提）----
    lc_cfg = ((locked or {}).get("capture_config") or {})
    yuv_cfg = (yuv.get("capture_config") or {}) if yuv else {}
    y_sk, _ys = _skew_value(yuv)
    l_sk, _ls = _skew_value(locked)
    if yuv and not yuv_failed:
        fp_y = yuv_cfg.get("config_fingerprint")
        fp_l = lc_cfg.get("config_fingerprint")
        if fp_y and fp_l and fp_y != fp_l:
            diffs = _fingerprint_diff(fp_y, fp_l)
            diff_txt = "；".join(f"{n}: 对照档={va} vs ②={vb}" for n, va, vb in diffs)
            # 「指纹摘要」是**派生量**（它是其余各段的散列），差异必然由别的段引起，
            # 所以不单独判它 —— 否则"只差预热帧数"会因为摘要串跟着变而被判成
            # 两处不同。若将来指纹新增了段而两版生成方式不同，新段会以
            # 「第N段」的形式出现并计入硬差异，不会漏。
            # 「预热帧数」是丢弃规则，不进入下发给 HAL 的请求。
            soft = ("预热帧数", "指纹摘要")
            hard = [d for d in diffs if d[0] not in soft]
            if hard:
                warns.append(f"对照档的配置指纹与②有 {len(hard)} 处设置不同（{diff_txt}）"
                             "⇒ 它相对②不止动了「单张 vs 重复请求」一个变量，"
                             "结论无法归因")
            else:
                notes.append(f"对照档与②的指纹只差软段（{diff_txt}）—— "
                             "「预热帧数」是**丢弃规则**、不进入下发给 HAL 的请求，"
                             "「指纹摘要」是派生量，所以读出模式/几何类结论仍可"
                             "归因于调度方式；但严格单变量对照要求它相同"
                             "（v1.8.1 已把⑥对照档的预热帧数改成与②一致）")
        elif fp_y and fp_l:
            notes.append("对照档与② 同几何/同格式/同模板/同预热（指纹一致）")
        else:
            # 指纹缺失时**不能**走"一致"分支 —— "判不了"与"判过了"必须分开；
            # 且"与连拍一致"这个结论本身就依赖这次比对：比对缺席 ⇒ 第 1 项不可判定，
            # 与"对照档缺席"同级处置（WARN），不能放进 PASS 的"已确证"清单
            warns.append("对照档或②缺配置指纹 ⇒ 无法逐段比对（判不了不等于一致）："
                         "「单张 vs 重复请求只动了一个变量」**不可判定**，"
                         "第 1 项对照检查缺席")
        if y_sk and l_sk:
            if y_sk == l_sk:
                notes.append(f"对照档的 skew 与②完全相同（{y_sk} ns）⇒ 单张与重复请求"
                             "用的是同一读出模式，②的 skew/几何结论可外推到单张")
            else:
                warns.append(f"对照档的 skew={y_sk} ns 与②的 {l_sk} ns **不同** ⇒ "
                             "单张捕获改变了读出模式，②③④⑤ 关于 skew/读出时间的"
                             "结论**不可外推**到拍照路径，必须改用本报告的值")

    # ---- 1b) 交付档的参考行偏置（G25 是对②算的，交付档要按它自己的几何算）----
    # SENSOR_TIMESTAMP 指的是**活动阵列第一行**，不是图像第 0 行。交付档 crop 一旦
    # 不是从第 0 行起，图像第 0 行就比时间戳晚 `crop.top × 行时间` —— 画面正常、
    # 时间戳单调、抖动为 0，g10/g15/g20/g22 一个都不报警，所以只能显式查。
    cr_j = (jpg.get("capture_config") or {}).get("crop_region") or {}
    ctop = cr_j.get("top") if isinstance(cr_j, dict) else None
    if isinstance(ctop, (int, float)):
        if ctop != 0:
            warns.append(f"交付档的 crop.top={ctop} ⇒ SENSOR_TIMESTAMP 指向活动阵列首行"
                         "而非图像第 0 行，存在 `crop.top × 行时间` 的**静默偏置**"
                         "（见 G25）。处置：交付档取 crop.top==0 的尺寸，"
                         "或按该偏置补偿后再用")
        else:
            notes.append("交付档 crop.top=0 ⇒ 参考行与图像首行重合（无偏置）")
    else:
        warns.append("交付档的 crop_region 缺失 ⇒ 无法判断参考行偏置是否为 0"
                     "（判不了不等于没问题）")

    # ---- 2) 交付档：时间戳是不是"按下那一刻"的帧 ----
    jstats = jpg.get("stats") or {}
    jsig = _j(jstats.get("sensor_ts_minus_request") or {}, "negative_count")
    all_after = (jstats.get("sensor_ts_minus_request") or {}).get("all_capture_after_request")
    lag_min = _j(jstats.get("sensor_ts_minus_request") or {}, "min")
    if all_after is False or (isinstance(jsig, int) and jsig > 0):
        fails.append(f"交付档有 {jsig} 张的时间戳**早于**请求下发时刻"
                     f"（最小差 {lag_min / 1e6:.1f} ms）⇒ 交出的是零快门延迟缓冲里的"
                     "旧帧，时间戳与「按下那一刻」脱钩。必须显式关掉 ZSL"
                     "（CONTROL_ENABLE_ZSL=false）并回读确认")
    elif all_after is True:
        notes.append("交付档每张的时间戳都晚于请求下发时刻 ⇒ 不是 ZSL 旧帧")

    # ---- 2b) 逐张时间戳是否严格递增（与上一条互为独立证据）----
    # 上一条问"时间戳是否早于下发时刻"；这条问"逐张是否递增"。两者不能互相替代：
    # ZSL 也可能交出**更早的一张但仍晚于下发**的帧（缓冲里排在前面的那张），
    # 那时逐张序列会出现"推进量异常小甚至为 0"，而上一条看不出问题。
    sstep = _j(jstats.get("sensor_vs_callback_advance") or {}, "sensor_step_min")
    if sstep is not None:
        if sstep <= 0:
            fails.append(f"交付档相邻两张的传感器时间戳推进最小值为 {sstep} ns（≤0）"
                         "⇒ 逐张时间戳不是严格递增，混入了缓冲里的旧帧")
        else:
            notes.append(f"交付档逐张时间戳严格递增（最小步进 {sstep / 1e6:.1f} ms）")

    # ---- 2c) 图与结果是否配对（v1.8.1 起的字段）----
    # 交付物是「图 + 时间戳」，而在此之前判据只验证了时间戳这一半。图走
    # ImageReader 回调、结果走 onCaptureCompleted，**两条独立通路，顺序不保证**，
    # 所以"这张图确实是这个时间戳那张"必须有字段能回答 —— 否则只能假定它成立。
    # （1.8.1 之前 App 用的是"回调时刻最后拿到的那张图"，图晚到就会被记到下一张
    #   头上：图是真的、时间戳也是真的，只是错配，报告里任何字段都不会自相矛盾。）
    jframes = [f for f in (jpg.get("frames") or []) if isinstance(f, dict)]
    jvalid = [f for f in jframes if not f.get("is_warmup")]
    pair = jstats.get("image_pairing") or {}
    if pair:
        sur = _j(pair, "images_surplus")
        diag = (f"取到图 {_j(pair, 'images_acquired')} 次、"
                f"acquireLatestImage 返回 null {_j(pair, 'acquire_null')} 次、"
                f"读字节失败 {_j(pair, 'read_errors')} 次、"
                f"未被任何结果认领的图 {sur} 张")

        # ---- v1.8.3：把配对判到**逐帧**，而不是只看汇总 ----
        # 汇总里"丢图 / 多余图 / 预热"三件事混在一起，逐帧才分得开。三个必须分开的量：
        #   ① **时序假象**：某帧被判"没有图"，而它的时间戳同时出现在"未被认领的图"里
        #      —— 同一时间戳两处出现，只可能是"图比结果晚到"。≤1.8.2 的在线配对
        #      （在结果回调里查表）必然产生这种假象，**不是丢图**。
        #   ② **有效张真丢图**：交付少一张（这是要 WARN 的）。
        #   ③ **预热张缺图**：预热张会被丢弃，不缺交付；只说明管线前几张交图慢。
        surplus_ts = set(pair.get("surplus_ts") or [])
        per_flagged = [f for f in jframes if isinstance(f.get("image_matched"), bool)]
        if per_flagged:
            def _ts(f):
                return f.get("sensor_timestamp_ns")

            per_missing = [f for f in per_flagged if f["image_matched"] is False]
            per_matched = [f for f in per_flagged if f["image_matched"] is True]
            artifacts = [f for f in per_missing if _ts(f) in surplus_ts]
            genuine = [f for f in per_missing if _ts(f) not in surplus_ts]
            gen_valid = [f for f in genuine if not f.get("is_warmup")]
            gen_warm = [f for f in genuine if f.get("is_warmup")]
            unclaimed = sorted(t for t in surplus_ts
                               if t not in {_ts(f) for f in per_missing})
            pending = _j(pair, "images_pending")
            n_valid = len([f for f in per_flagged if not f.get("is_warmup")])
            n_valid_ok = len([f for f in per_matched if not f.get("is_warmup")])

            if gen_valid:
                warns.append(
                    f"交付档有 {len(gen_valid)}/{n_valid} 张**有效张**没有配对到图"
                    f"（时间戳 {[int(_ts(f)) for f in gen_valid]}）⇒ 交付会少图。"
                    + diag)
            if unclaimed:
                warns.append(
                    f"交付档有 {len(unclaimed)} 张图没有被任何结果认领"
                    f"（时间戳 {[int(t) for t in unclaimed]}）⇒ 图与结果的配对不完整。"
                    "**配错比少图更严重**：时间戳对、图也对，只是两者不是同一张，"
                    "报告里没有任何字段会自相矛盾" + diag)
            if pending:
                warns.append(
                    f"结算时有 {pending} 张图尚未取到（`images_pending`）⇒ 报告在末帧图"
                    "到达之前就结算了。1.8.3 起会先等最多 1.2 s（`settle_wait_ms`）"
                    "再结算；请用 1.8.3+ 重跑⑥。**「还没到」与「丢了」必须分开**")
            if artifacts:
                notes.append(
                    f"{len(artifacts)} 张的图**比结果晚到**（其时间戳同时出现在"
                    "「未被任何结果认领」列表里）—— 这是 ≤1.8.2 在线配对（在结果回调里"
                    "查表）的**时序假象**，不是丢图。1.8.3 起改为结算时按时间戳集合"
                    "离线配对，此现象消失")
            if gen_warm:
                notes.append(
                    f"{len(gen_warm)} 张**预热张**没有配对到图（预热张会被丢弃、"
                    "不影响交付；它说明管线前几张交图较慢）")
            if not gen_valid and not unclaimed and not pending:
                notes.append(
                    f"图与结果一一配对（有效张 {n_valid_ok}/{n_valid}；{diag}）"
                    "⇒ 每张交付的图与其时间戳同源")
            # 汇总与逐帧互校（v1.8.3 起有分项汇总，可直接比）
            mv = _j(pair, "images_missing_valid")
            if mv is not None and mv != len(gen_valid):
                warns.append(
                    f"汇总与逐帧打架：`images_missing_valid`={int(mv)}，"
                    f"但逐帧只有 {len(gen_valid)} 张有效张缺图 ⇒ 汇总计数不可信")
        else:
            # ≤1.8.0 的报告没有逐帧 `image_matched`，只能按汇总判（保留原语义）
            miss = _j(pair, "images_missing")
            if sur and sur > 0:
                warns.append(f"交付档有 {sur} 张图没有被任何结果认领（{diag}）"
                             "⇒ 图与结果的配对不完整。**配错比少图更严重**："
                             "时间戳对、图也对，只是两者不是同一张，"
                             "报告里没有任何字段会自相矛盾")
            if miss and miss > 0:
                warns.append(f"交付档有 {miss} 张结果没有配对到图（{diag}）"
                             "⇒ 交付会少图。定位：acquire_null>0 是取图竞态；"
                             "read_errors>0 是读字节失败；两者都为 0 则说明管线没交图")
            if (not miss) and (not sur):
                notes.append(f"图与结果一一配对（{diag}）⇒ 每张交付的图与其时间戳同源")
        # 摘要与逐张数据互校：摘要说"配对了 N 张"，逐张里就该有 N 张带字节。
        # 两者由同一个 App 写出，但一个是累加计数、一个是逐张记录 ——
        # 不一致说明有「配对上了却读不到字节」，只可能是读字节失败。
        # 这类"汇总与明细打架"正是最容易静默通过的东西，所以交叉查一次。
        if str((jpg.get("capture_config") or {}).get("output_format", "")).upper() == "JPEG":
            n_bytes = sum(1 for f in jframes if f.get("jpeg") is not None)
            n_match = _j(pair, "images_matched")
            if n_match is not None and n_match > n_bytes:
                warns.append(f"交付档 image_pairing 记「配对了 {n_match} 张」，"
                             f"但逐张里只有 {n_bytes} 张带图像字节 ⇒ "
                             "有张数配对上了却没有字节（read_errors>0 才会这样），"
                             "或摘要计数与逐张记录不一致")
    else:
        # ≤1.8.0 的报告没有该段。此时只能退而核"有没有图"，核不到"图是不是这一张"。
        n_null = sum(1 for f in jvalid if "jpeg" in f and f.get("jpeg") is None)
        if n_null:
            warns.append(f"交付档 {len(jvalid)} 张有效张里有 {n_null} 张没有图像字节"
                         "（该版报告无法区分「管线没给图」与「读字节失败」）"
                         "⇒ 单张交付存在丢图风险，交付链路必须有 ACK/重传兜底。"
                         "用 1.8.1 起的 APK 重跑⑥可定位原因（新增 image_pairing 段）")
        else:
            warns.append("交付档有效张都带图像，但报告**没有** image_pairing 段"
                         "（1.8.1 起才有）⇒ 「图与时间戳同源」这一条**未核验**："
                         "只能确认有图，确认不了这张图就是该时间戳那张。"
                         "处置：用 1.8.1 起的 APK 重跑⑥")

    # ---- 回调到达抖动：**只说明"通知来得齐不齐"，不判帧序一致性** ----
    # ⚠ 这里改过一次（第 10 轮）：原实现拿这个量判"时间戳与真实帧序不一致，
    #   逐张对应关系不可靠"，**那个推理不成立** —— 回调到达时刻与曝光时刻本来就
    #   差一个固定管线延迟，延迟抖动只说明"通知不齐"，与"时间戳属于哪一帧"是
    #   两件事。真正能判后者的只有上面两条（sensor_step_min>0、image_pairing）。
    # 它的正确用途是回答：「若拿回调时刻当时间源，会错多少」。
    adv = _j(jstats.get("sensor_vs_callback_advance") or {}, "max_abs_diff_ms")
    if adv is not None:
        sk_ms = (l_sk or 0) / 1e6
        notes.append(f"回调到达抖动 ±{adv:.1f} ms（通知比曝光晚多少的不稳定量；"
                     f"单帧读出 {sk_ms:.1f} ms）⇒ 回调时刻比图像自身的时间戳更差，"
                     "不得作为时间源（本方案用 SENSOR_TIMESTAMP）")

    lat = jstats.get("exposure_to_callback_ns") or {}
    spread = _j(lat, "spread_ms")
    if spread is not None:
        if spread > STILL_LATENCY_SPREAD_WARN_MS:
            warns.append(f"曝光→回调延迟极差 {spread:.1f} ms 过大，"
                         "无法用常数反推曝光时刻")
        elif spread > STILL_LATENCY_SPREAD_OK_MS:
            warns.append(f"曝光→回调延迟极差 {spread:.1f} ms（> {STILL_LATENCY_SPREAD_OK_MS} ms）"
                         "，反推曝光时刻时需按张记录延迟")
        else:
            notes.append(f"曝光→回调延迟稳定（极差 {spread:.1f} ms，"
                         f"均值 {(_j(lat, 'mean') or 0) / 1e6:.1f} ms）")

    # ---- 3) 是否触发多帧合成：ISP 档位是否被 HAL 改写 ----
    lock = jstats.get("lock_stability") or {}
    for k, bad in (("noise_reduction_mode", ("ZERO_SHUTTER_LAG", "HIGH_QUALITY")),
                   ("edge_mode", ("ZERO_SHUTTER_LAG", "HIGH_QUALITY"))):
        e = lock.get(k) or {}
        vs = [str(v) for v in (e.get("values") or [])]
        hit = [v for v in vs if any(b in v for b in bad)]
        if hit:
            fails.append(f"交付档的 {k} 回读为 {hit} ⇒ HAL 把它改写成了多帧/时域处理。"
                         "多帧合成下 `SENSOR_TIMESTAMP` 只指向其中一帧（甚至合成起点），"
                         "「这张照片的时刻」不再是单值。必须显式设 OFF 并确认回读")
        elif vs:
            notes.append(f"{k}={vs}")
    zsl = [str(v) for v in ((lock.get("control_enable_zsl") or {}).get("values") or [])]
    if zsl:
        notes.append(f"control_enable_zsl 回读={zsl}")

    # ---- 4) EXIF 能不能当时间源 ----
    ex = jstats.get("exif") or {}
    digits = ex.get("subsec_digits_values")
    if ex:
        if not ex.get("with_subsec"):
            warns.append("JPEG 的 EXIF **没有** SubSecTimeOriginal ⇒ EXIF 时间只有秒级，"
                         "用它当时间戳误差 ±0.5 s。**不得**用 EXIF 作时间源，"
                         "必须把 SENSOR_TIMESTAMP 与图像一起回传")
        else:
            dmin = min([d for d in (digits or []) if isinstance(d, int)] or [0])
            if dmin < EXIF_SUBSEC_MIN_DIGITS:
                warns.append(f"JPEG 的 EXIF SubSecTimeOriginal 只有 {dmin} 位小数"
                             f"(< {EXIF_SUBSEC_MIN_DIGITS}) ⇒ 分辨率约 "
                             f"{10 ** (3 - dmin)} ms 量级，达不到本方案需求。"
                             "不得用 EXIF 作时间源，必须回传 SENSOR_TIMESTAMP")
            else:
                notes.append(f"EXIF SubSecTimeOriginal 有 {dmin} 位小数（{digits}），"
                             "但父字段仍是**系统墙钟** —— 可用于交叉核对，"
                             "不可替代 SENSOR_TIMESTAMP")
        notes.append(f"EXIF DateTimeOriginal 样例={ex.get('sample_datetime_original')}，"
                     f"首个 JPEG {ex.get('first_bytes')} B")

    shots = jpg.get("frame_count")
    detail = (f"交付档 {jpg.get('capture_size')} "
              f"{(jpg.get('capture_config') or {}).get('output_format')} / "
              f"{(jpg.get('capture_config') or {}).get('template_name')}，"
              f"{shots} 张；对照档 "
              + ("失败（" + str(yuv.get("error")) + "）" if yuv_failed
                 else ("有" if yuv else "**缺**")) + "。")

    if fails:
        res.add("G26", FAIL, "拍照路径的时间戳语义不成立", detail + " ".join(fails)
                + "。附注：" + "；".join(notes))
    elif warns:
        res.add("G26", WARN, "拍照路径已取证，但有需要处理项", detail + " ".join(warns)
                + "。附注：" + "；".join(notes))
    else:
        res.add("G26", PASS, "拍照路径的时间戳语义与连拍一致且可用",
                detail + " -> 已确证：" + "；".join(notes)
                + "。⇒ 单张捕获没有引入 ZSL 旧帧、没有触发多帧合成、"
                "参考行为图像首行；**交付路径上的时间戳可作时间轴基准**。"
                "注意本项只覆盖已测的那一档配置（见报告的 capture_config），"
                "改模板/格式/尺寸/crop 后必须重测")


def _evidence_output_formats(*reports):
    """列出报告里出现过的 HAL 输出格式，返回 `[(格式, 来源), ...]`（按格式去重，保留首个来源）。

    优先 `capture_config.output_format`（1.7.0 起记录）；
    退回 `config_fingerprint` 的第 4 段（所有版本都有，形如
    `1920x1440|a4096x3072|c4096x3072@0,0|YUV_420_888|w5 #57a6ae4e`）。

    两个刻意的设计（都是踩过坑之后加的）：
      · **一个都取不到时返回空列表**，不返回空串或占位符 —— 空串会被下游当成一个"格式"，
        于是"判不了"被静默当成"判过了"。（本文件踩过一次同类坑：不可判定时返回
        `(None, None)` 而不是 `None`，元组恒为真。判不了要用**明确的哨兵**。）
      · **返回全部，而不只是第一个** —— 若各报告记的格式不一致，那本身就说明它们
        不可能来自同一条采集路径（格式是路径三元组的一员）；"取第一个、当其余没发生"
        正是 R20 那类漏洞的原型。
    """
    out, seen = [], set()
    for r in reports:
        if not isinstance(r, dict):
            continue
        fmt = src = None
        cfg = r.get("capture_config") or {}
        f = cfg.get("output_format")
        if isinstance(f, str) and f.strip():
            fmt, src = f.strip(), "capture_config.output_format"
        else:
            fp = cfg.get("config_fingerprint")
            if isinstance(fp, str):
                parts = fp.split("|")
                if len(parts) >= 4 and parts[3].strip():
                    fmt, src = parts[3].strip(), "config_fingerprint[3]"
            if fmt is None:
                f = r.get("output_format")
                if isinstance(f, str) and f.strip():
                    fmt, src = f.strip(), "顶层 output_format"
        if fmt and fmt.upper() not in seen:
            seen.add(fmt.upper())
            out.append((fmt, src))
    return out


def evaluate_delivery_format(delivery, declared, locked, rounds, res):
    """G27 交付采集格式一致性 —— 交付用的 HAL 输出格式，是不是取证时用的那一个。

    **为什么需要这条（R24）**：采集路径是三元组 {模板, 调度, 输出格式}。
    模板与调度由 `(capture_path_id, rev)` + G23 看着，但**输出格式此前没有任何判据在看**。
    于是「为省带宽，把交付改成让 HAL 直接出 JPEG」这类改动会静默地让交付物离开已取证路径 ——
    与 R22 是**同一类错误，只是下沉了一层**。G23 救不了：它只比较**报告之间**，
    不会把报告与**交付配置**比（R20 已经写明这一点）。本段补的正是缺掉的那一半。

    三态：
      - 一致               -> PASS
      - 不一致             -> WARN「交付采集格式未被取证」（给两条处置）
      - 报告里取不到格式   -> WARN「无法判定」**fail closed，不是 PASS**
    交付路线声明为拍照时本项不适用（⑥ 报告自描述该档格式，由 G26 判）。

    边界说明：**由 YUV 再软件编码成 JPEG 不算换路径** —— 编码发生在采集之后，
    不参与曝光与时间戳；只有让 HAL 直接输出另一种格式才是换采集路径。
    """
    declared = (declared or "yuv").strip().lower()
    if delivery == "still":
        res.add("G27", INFO, "交付采集格式一致性",
                f"交付路线声明为拍照（--delivery still），交付档的格式由 ⑥ 报告自描述，"
                f"本条不重复判定（见 G26）；声明值 {declared} 仅作记录")
        return

    found = _evidence_output_formats(locked, *(rounds or []))
    if not found:
        res.add("G27", WARN, "交付采集格式无法判定",
                f"声明交付格式={declared}，但报告里找不到证据实际使用的 HAL 输出格式"
                f"（既无 capture_config.output_format，指纹里也没有格式段）⇒ "
                f"**判不了不等于没问题**（fail closed）。"
                f"处置：用一个会记该字段的版本重跑 ②，再重跑本脚本")
        return

    uniq = sorted({f.upper() for f, _ in found})
    if len(uniq) > 1:
        res.add("G27", WARN, "证据内部采集格式不一致",
                f"声明交付格式={declared}，而报告们记的输出格式不止一种："
                + "；".join(f"{f}（{s}）" for f, s in found)
                + " ⇒ 格式是采集路径三元组的一员，**这些报告不可能来自同一条采集路径**。"
                  "处置：先解决报告集内部的路径分裂（见 G23 与 R20），再判交付格式")
        return

    ev_fmt, src = found[0]
    allowed = tuple(x.upper() for x in CAPTURE_FORMAT_ALIASES.get(declared, (declared.upper(),)))
    if ev_fmt.upper() in allowed:
        res.add("G27", PASS, "交付采集格式已被取证",
                f"声明交付格式={declared}（证据实际={ev_fmt}，取自 {src}）"
                f"⇒ 交付物落在已取证路径上。注：由该格式**软件编码**成 JPEG/PNG 不算换路径")
    else:
        res.add("G27", WARN, "交付采集格式未被取证",
                f"声明交付格式={declared}，而证据实际使用的 HAL 输出格式是 **{ev_fmt}**"
                f"（取自 {src}）⇒ **采集路径的输出格式变了，既有 PASS 覆盖不到交付物**（R24）。"
                f"处置二选一：① 把交付格式改回 {ev_fmt}"
                f"（设计 5.3 的推荐，零新增取证）；"
                f"② 补一次**该格式**的对齐采集（同模板、同调度、只换输出格式），"
                f"产出新报告后重跑本脚本")


def evaluate_size_choice(locked, size_sweep, cam, res):
    """G24 输出尺寸定档依据 —— 当前档位是不是有实测支撑选出来的。

    输出尺寸属于内参，而 **fx 与输出宽度成正比**（本机 1920 -> 1282.5 px，
    4096 -> 2736 px），亚像素定位精度大致与 fx 成正比。
    也就是说"定哪一档"直接决定方案的精度上限，不能靠推断。

    App 目前的自动选档规则是"同纵横比、面积不超过一个人为上限中最大者"，
    那个上限（2048x1536 = 3.15 MP）没有任何实测依据 —— 它是为了避开 16:9
    才随手定的。所以本判据把"有没有实测表"作为结论依据。
    """
    cfg = ((locked or {}).get("capture_config") or {})
    out = cfg.get("output_size") or {}
    active_w = (cfg.get("active_array") or {}).get("w")
    fx_full = ((cam or {}).get("optics") or {}).get("fx_px_full_array")

    def fx_at(width):
        """把全幅预测 fx 折算到某个输出宽度。fx 与输出宽度成正比。"""
        if isinstance(fx_full, (int, float)) and isinstance(active_w, int) and active_w > 0:
            return fx_full * width / active_w
        return None

    cur_fx = fx_at(out.get("w")) if isinstance(out.get("w"), int) else None
    fx_txt = f"optical fx≈{cur_fx:.0f} px" if cur_fx else "fx 未知（旧版①报告没有 optics 段）"

    trials = []
    if isinstance(size_sweep, dict):
        trials = [t for t in (size_sweep.get("trials") or []) if isinstance(t, dict)]

    if not trials:
        res.add("G24", INFO, "输出尺寸仍由人为面积上限决定，缺实测依据",
                f"当前={out.get('w')}x{out.get('h')}（{fx_txt}）。"
                "选档规则是「同纵横比、面积不超过 2048x1536 中最大者」——"
                "那个上限只是为了避开 16:9 才定的，没有实测支撑。"
                "请跑 App 的⑤尺寸扫描（各候选尺寸的实际帧率与卷帘读出时间），"
                "再按 精度（fx ∝ 宽度）与 带宽/延迟 一起定档")
        return

    rows, miss = [], []
    asy = []
    valid = []
    for t in trials:
        act = t.get("actual_size") or {}
        w = act.get("w")
        if t.get("error") or not isinstance(w, int) or w <= 0:
            miss.append(f"{t.get('target_width')}px 目标档失败（{t.get('error')}）")
        else:
            valid.append(t)
    # 「有没有记请求帧率」只在**成功档位**上有意义 —— 全灭的一批没有任何实测，
    # 谈不上"未记录请求帧率"（第 11 轮：5/5 档全灭时旧实现仍打印该警告，
    # 括号里"各档实测均 ≈30.04 fps"在本批一个实测都没有，是凭空断言）。
    has_req_fps = any(isinstance(t.get("target_fps"), (int, float)) for t in valid)
    for t in valid:
        act = t.get("actual_size") or {}
        w, h = act.get("w"), act.get("h")
        iv = t.get("actual_interval_ns")
        fps = (1e9 / iv) if isinstance(iv, (int, float)) and iv > 0 else None
        sk = t.get("rolling_shutter_skew_ns")
        crop = t.get("crop_region") or {}
        one = f"{w}x{h}"
        if t.get("forced_full_crop"):
            # 判别档故意各向异性（fx≠fy），**不能**当作可选档位；必须明写，
            # 否则它会混在候选表里，让人以为"3840x2160 全crop"是个可用档。
            one += "(全crop判别档，几何不可用，仅用于分离 skew 归属)"
            asy.append(f"{w}x{h}")
        elif t.get("crop_shrunk_for_aspect"):
            one += "(crop 已收窄到输出纵横比)"
        req = t.get("target_fps")
        one += ": "
        if fps and isinstance(req, (int, float)):
            one += f"请求{req:.0f}→实测{fps:.2f} fps"
        elif fps:
            one += f"{fps:.2f} fps"
        else:
            one += "帧率未知"
        mb = w * h * 1.5 / 1e6
        if isinstance(sk, (int, float)):
            one += f", skew={sk / 1e6:.2f} ms"
        one += f", 每帧 YUV≈{mb:.1f} MB"
        if fps:
            one += f"（≈{mb * fps:.0f} MB/s）"
        if crop.get("width"):
            one += f", crop={crop.get('width')}x{crop.get('height')}"
        f = fx_at(w)
        if f:
            one += f", fx≈{f:.0f} px"
        rows.append(one)

    # skew 归属的判别结果（⑤ 新增的"只动纵横比、不动 crop"档就是为了这个）
    verdict = ""
    lat = {}
    for t in trials:
        if t.get("error"):
            continue
        sk = t.get("rolling_shutter_skew_ns")
        cfg_t = {"output_size": t.get("actual_size"),
                 "crop_region": t.get("crop_region") or _crop_from_fingerprint(
                     t.get("config_fingerprint"))}
        n = _skew_norm(cfg_t, sk) if isinstance(sk, (int, float)) and sk > 0 else None
        if n:
            lat[round(n["line_time_ns"], 1)] = lat.get(round(n["line_time_ns"], 1), 0) + 1
    if len(lat) == 1:
        verdict = (f"。⑤ 的 skew 归属判别：全部档位的「skew / 读出窗口行数」"
                   f"都等于 {next(iter(lat)):.1f} ns/行 -> 该口径是唯一不变量"
                   "（输出高度、crop 高度都不是）")
    elif lat:
        verdict = ("。⑤ 的 skew 归属仍不唯一（各口径算出的行时间不一致）-> "
                   "需要更多档位，或报告里缺少 crop_region")
    # 判别档的**直接**结论（比"哪个口径恒定"更有力）：同一输出、只把 crop 高度
    # 从 2304 换成 3072，skew 一模一样 ⇒ crop 高度被排除。这正是当初设这条档的目的，
    # 所以必须在报告里显式说出来，否则下次又要重新推断一遍。
    by_out = {}
    for t in trials:
        if t.get("error"):
            continue
        a = t.get("actual_size") or {}
        key = (a.get("w"), a.get("h"))
        sk = t.get("rolling_shutter_skew_ns")
        if key[0] and isinstance(sk, (int, float)) and sk > 0:
            by_out.setdefault(key, []).append(
                ((t.get("crop_region") or {}).get("height") or 0, sk))
    for key, lst in by_out.items():
        if len({sk for _, sk in lst}) == 1 and len({ch for ch, _ in lst}) > 1:
            chs = "、".join(str(c) for c, _ in sorted(lst))
            verdict += (f"。判别档结论：同一输出 {key[0]}x{key[1]} 下，crop 高度取 "
                        f"{chs} 行时 skew 完全相同（{lst[0][1]} ns）"
                        "⇒ **crop 高度与 skew 无关**（否则 skew 会按行数比例变）")

    detail = "; ".join(rows) if rows else "无有效档位"
    if miss:
        detail += f"；未取到: {'、'.join(miss)}"
    tail = ("取舍：宽度翻倍 -> fx 翻倍（亚像素精度大致同倍提升），"
            "但每帧字节数约翻 4 倍 -> 带宽/回传延迟同步恶化")
    if not valid:
        # 全灭批（第 11 轮实测：5/5 档「摄像头被断开」）：任何"各档实测/skew 相同"
        # 的话在本批都没有证据。历史轮次的实测可以引用，但必须**标明轮次** ——
        # 否则更早批次的数据会被读成本批的数据（结论超出证据）。
        tail += ("。⚠ **本批没有取到任何档位的实测** ⇒ 定档所需的「各档实测帧率 / skew」"
                 "本批缺席；既往实测（4:3 各档 skew 同为 9.21 ms、16:9 为 6.91 ms、"
                 "各档均达 30.04 fps）来自更早的轮次，是**历史证据**，引用时须标明轮次，"
                 "不得当作本批数据。请重跑⑤（「摄像头被断开」是设备级瞬时故障，"
                 "1.8.2 起 App 会自动重试打开）")
    else:
        tail += ("。本机所有 4:3 档（1920x1440/2880x2160/4096x3072）的 **skew 完全相同**"
                 "（9.21 ms），16:9 输出只有 3/4 的读出窗口行数（6.91 ms）"
                 "⇒ 定档的差异在 fx、带宽，以及 16:9 会白扔 25% 垂直视场与更短的读出时间")
        if not has_req_fps:
            # 这一条是第 7 轮补的：⑤ 原先没记请求帧率，而各档实测都是 30.04 fps ——
            # 读起来像"帧率不随尺寸下降"，但那只是因为**各档都只请求了默认的 30 fps**。
            # 从"达到了 30"推不出"上限就是 30"，更不能推出"上限与尺寸无关"。
            tail += ("；⚠ 本批扫描未记录请求帧率（各档实测均 ≈30.04 fps）："
                     "这只能说明各档**达到**了默认档位，**不能**说明帧率上限与尺寸无关 —— "
                     "要回答那个问题须在各档请求更高的帧率（如 60）再比")
    res.add("G24", INFO, "输出尺寸候选表（定档依据）",
            detail + f"。当前生效={out.get('w')}x{out.get('h')}（{fx_txt}）。"
            + tail + verdict)


def evaluate_multicam(cam, locked, res):
    """I9 物理镜头清单与光学预测 fx。

    逻辑摄像头含超广角时，必须知道 1.0 档位落在哪颗物理镜头上，
    否则标定结果无法解释、也无法发现换镜。
    """
    mc = (cam or {}).get("multi_camera")
    if not mc:
        res.add("I9", INFO, "无多摄信息",
                "报告里没有 multi_camera 段 -> 请用新版 App 重跑①")
        return

    n = mc.get("physical_camera_count", 0)
    phy = mc.get("physical_cameras") or []
    parts = []
    for p in phy:
        fl = (p.get("focal_lengths_mm") or [None])[0]
        eq = p.get("equivalent_focal_length_mm")
        seg = f"id={p.get('id')} f={fl}mm"
        if isinstance(eq, (int, float)):
            seg += f" 等效={eq:.1f}mm"
        if p.get("error"):
            seg += f" [读取失败: {p['error']}]"
        parts.append(seg)
    res.add("I9", INFO,
            f"逻辑多摄：is_logical={mc.get('is_logical')}，物理镜头 {n} 颗"
            + (f"，同步类型={mc.get('sensor_sync_type')}" if mc.get("sensor_sync_type") else ""),
            "; ".join(parts) if parts else "无物理镜头明细")

    # 光学预测 fx：标定得到的 fx 应落在它附近。偏离一个焦距比 = 生效的是另一颗镜头。
    op = (cam or {}).get("optics") or {}
    fx_full = op.get("fx_px_full_array")
    cfg = (locked or {}).get("capture_config") or {}
    # 优先用新 App 记录的"输出/实际crop"缩放；它已包含为满足纵横比一致性
    # 而做的自动裁切。老报告只有"输出/活动阵列"，那在两者纵横比不同时是错的。
    sx = cfg.get("output_to_crop_scale_x")
    if not isinstance(sx, (int, float)):
        sx = cfg.get("output_to_active_scale_x")
    if isinstance(fx_full, (int, float)) and isinstance(sx, (int, float)) and sx > 0:
        out = cfg.get("output_size") or {}
        note = (f"光学预测 fx={fx_full * sx:.1f} px @ {out.get('w')}x{out.get('h')}"
                f"（f={op.get('focal_length_mm')}mm, 像元={op.get('pixel_pitch_um'):.3f}um"
                f", 裁切系数={op.get('crop_factor'):.2f}）")
        # 超广角与主摄的焦距比，用来说明"若换镜会偏多少"
        ratios = []
        for p in phy:
            fl = (p.get("focal_lengths_mm") or [None])[0]
            base = op.get("focal_length_mm")
            if isinstance(fl, (int, float)) and isinstance(base, (int, float)) and base:
                ratios.append(fl / base)
        if ratios:
            note += ("；物理镜头相对主摄的焦距比="
                     + ", ".join(f"{r:.3f}" for r in sorted(ratios))
                     + " -> 标定 fx 若与预测值相差接近某个比值，说明生效的是该镜头")
        res.add("I10", INFO, "光学预测 fx（标定交叉校验）", note)
    else:
        res.add("I10", INFO, "无法给出光学预测 fx",
                "需要新版①报告的 optics.fx_px_full_array 与②的 capture_config "
                "-> 两者齐备后才能用 fx 交叉校验是否换镜")


def evaluate_focus_depth(locked, cam, stats, res):
    """G16 对焦点与景深合理性。

    对焦点选错是静默失效：程序不报错、标定也能做，但目标整体离焦会直接
    削掉亚像素定位精度。所以要把景深范围算出来，和工作距离对照。

    **必须用回读值算，不用请求值。** 本判据早期版本直接用 requested.focus_diopter，
    与别处"回读值才是唯一事实"的原则自相矛盾 —— 第 5 轮翻出来：报告里
    requested=1.0 diopt 而回读=1.001001 diopt（999.0 mm，HAL 量化到 1 mm）。
    本机型上偏差 0.1% 无所谓，但在量化更粗的机型上（超焦距档实测差 0.36%）
    这会把景深边界算错，而景深在 1 m 处只有十几厘米宽。
    """
    req = locked.get("requested") or {}
    policy = req.get("focus_policy")
    focus_diopter = req.get("focus_diopter")

    # 回读优先：lock_stability 的逐帧值 > requested_vs_actual > requested
    # ⚠ lock_stability 的 values 是**字符串**（App 用 toString() 写的），
    # 必须显式转换 —— 否则这里永远取空、静默退到 requested_vs_actual，
    # 于是"逐帧值优先"这层设计等于不存在（第 8 轮修）。
    rb = None
    lk = ((locked.get("stats") or {}).get("lock_stability") or {}) \
        .get("lens_focus_distance_diopter") or {}
    vals = []
    for v in (lk.get("values") or []):
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv > 0:
            vals.append(fv)
    if vals:
        rb = vals[0]
    if rb is None:
        rv = (((locked.get("stats") or {}).get("requested_vs_actual") or {})
              .get("focus_diopter") or {}).get("actual")
        if isinstance(rv, (int, float)) and rv > 0:
            rb = rv

    if rb is None and not isinstance(focus_diopter, (int, float)):
        res.add("G16", INFO, "无法判定对焦点", "报告里既没有回读值也没有 requested.focus_diopter")
        return
    if rb is None:
        rb = focus_diopter
        src = "请求值（未取到回读值）"
    else:
        src = "回读值"

    focus_diopter = rb
    focus_m = (1.0 / focus_diopter) if focus_diopter > 0 else float("inf")
    dof = depth_of_field_m(cam, focus_m)
    focus_desc = ("无穷远" if focus_diopter <= 0
                  else f"{focus_m:.4f} m ({focus_diopter:.6f} diopt, {src})")

    if dof is None:
        res.add("G16", INFO, "无法计算景深", f"对焦在 {focus_desc}，缺焦距/光圈参数")
        return

    near, far, hyper = dof
    far_txt = "∞" if (math.isinf(far) or far > 1000) else f"{far:.3f} m"
    dof_txt = f"对焦={focus_desc}, 景深={near:.3f} m ~ {far_txt}"

    working = (locked.get("params") or {}).get("focus_distance_m")
    hyper_ref = (cam.get("focus") or {}).get("hyperfocal_distance_diopter")

    # 识别"对焦点落在超焦距上"这种泛焦选择：远处清晰，近处全虚
    if policy and "hyperfocal" in str(policy):
        res.add("G16", WARN, "对焦点是超焦距泛焦，近处目标会离焦",
                dof_txt + f"；超焦距≈{hyper:.2f} m -> 任何近于 {near:.2f} m 的目标都在景深外。"
                "若实际工作距离小于该值，必须改为按工作距离对焦，否则亚像素精度会被静默削掉")
        return
    if not working and isinstance(hyper_ref, (int, float)) and hyper_ref > 0:
        if abs(focus_diopter - hyper_ref) / hyper_ref < 0.01:
            res.add("G16", WARN, "对焦点落在超焦距上（自动识别）",
                    dof_txt + f" -> 近于 {near:.2f} m 的目标都在景深外，"
                    "须确认实际工作距离是否落在景深内")
            return

    if working is None:
        res.add("G16", INFO, "未指定工作距离，只报告景深",
                dof_txt + " -> 请给出实际工作距离，以判定对焦点是否合适")
        return

    try:
        working = float(working)
    except Exception:
        working = None

    if working is None or working <= 0:
        res.add("G16", INFO, "工作距离无效", dof_txt)
        return

    if near <= working <= far:
        res.add("G16", PASS, "对焦点与工作距离匹配",
                f"工作距离={working:.3f} m 落在景深内，{dof_txt}")
    else:
        res.add("G16", FAIL, "工作距离落在景深之外",
                f"工作距离={working:.3f} m，但 {dof_txt} -> 目标整体离焦，"
                f"亚像素定位精度会大幅下降。应把对焦距离设为 {working:.3f} m "
                f"(即 {1.0 / working:.4f} diopt) 并重新标定")


# ---------------------------------------------------------------- 跨会话轮次

def extract_round(report, index=None):
    """从一轮锁定连拍报告里抽出跨会话重复性关心的量。

    与 App 内 ③ 的汇总口径保持一致，但**从原始报告重算**而不是读它的结论，
    这样即使只有 r1..r3.json（旧 App 不写汇总）也能判。

    除"锁定参数落点"之外，这里还要抽三类东西 —— 它们都属于"这一轮数据本身
    能不能用"，而不是"这一轮的参数对不对"：

    1. **逐轮帧完整性**（max_interval_ns / gap_count / gap_at）
       实测教训：r1 那一轮的 149 个间隔里有一个 66575833 ns = 2× 名义值，
       即**中途少了一帧**。而旧版 G21 只看焦点/镜头/尺寸，把这一轮判成了 PASS ——
       于是"某一轮的数据是断的"这件事，在 16 个 PASS 的报告里完全看不见。
       判据必须能看见它自己依赖的那批数据有没有坏。

    2. **会话新鲜度指纹**（first_frame_focus_diopter / first_frame_lens_state）
       "跨会话"这三个字得被证明，不能靠"App 里写了重开摄像头"来假定。
       摄像头真正关闭后再开，镜头会回到掉电默认位置（本机实测=超焦距
       0.09071941 diopt），首帧必然是 MOVING 并开始向工作距离移动。
       若某轮首帧**已经**停在锁定值上，说明摄像头根本没被重新打开过，
       这一轮与上一轮共享同一个 session，"跨会话重复性"就是假的。

    3. **温度与几何**（battery_temperature_c / crop_region / output_aspect…）
       温度是"内参不变"目前唯一没有仪器的漂移源；顺带在这里记录，
       让不同轮之间的温度差成为一个免费（但弱）的对照。
    """
    stats = report.get("stats") or {}
    lock = stats.get("lock_stability") or {}
    rv = stats.get("requested_vs_actual") or {}
    cfg = report.get("capture_config") or {}
    lt = stats.get("lens_state_timeline") or {}
    req = report.get("requested") or {}
    rv_focus = (rv.get("focus_diopter") or {})
    rv_fd = (rv.get("frame_duration_ns") or {})
    pid_e = (lock.get("active_physical_id") or {})
    pid_vals = pid_e.get("values") or []

    frames = report.get("frames") or []
    warm = ((report.get("params") or {}).get("warmup_frames") or 0)
    ts = [f.get("sensor_timestamp_ns") for f in frames
          if isinstance(f.get("sensor_timestamp_ns"), (int, float))]
    ivs = [ts[i] - ts[i - 1] for i in range(1, len(ts))]
    nominal = min(ivs) if ivs else None
    gaps = [(i, v) for i, v in enumerate(ivs)
            if nominal and v > nominal * ROUND_GAP_RATIO]

    f0 = frames[0] if frames else {}
    out = cfg.get("output_size") or {}
    crop = (frames[len(frames) // 2].get("crop_region") if frames else None) or {}
    asp_out = (out.get("w") / out.get("h")) if out.get("h") else None
    asp_crop = (crop.get("width") / crop.get("height")) if crop.get("height") else None

    return {
        "round": index,
        "source": report.get("generated_at_wall_ms"),
        "focus_actual_diopter": rv_focus.get("actual"),
        "focus_requested_diopter": rv_focus.get("requested"),
        "hyperfocal_diopter": req.get("hyperfocal_distance_diopter"),
        "frame_duration_ns": rv_fd.get("actual"),
        "active_physical_id": (pid_vals[0] if len(pid_vals) == 1 else None),
        "active_physical_ids": pid_vals,
        "output_size": cfg.get("output_size"),
        "valid_frames": stats.get("valid_frames"),
        "moving_after_warmup": lt.get("moving_after_warmup"),
        # --- 逐轮帧完整性 ---
        "interval_count": len(ivs),
        "nominal_interval_ns": nominal,
        "max_interval_ns": (max(ivs) if ivs else None),
        "gap_count": len(gaps),
        "gap_at": [i for i, _ in gaps],
        "span_ns": (ts[-1] - ts[0]) if len(ts) >= 2 else None,
        "warmup_frames": warm,
        # --- 会话新鲜度 ---
        "first_frame_focus_diopter": f0.get("lens_focus_distance_diopter"),
        "first_frame_lens_state": f0.get("lens_state"),
        "first_timestamp_ns": (ts[0] if ts else None),
        # --- 温度与几何 ---
        "battery_temperature_c": f0.get("battery_temperature_c"),
        "crop_region": crop,
        "output_aspect": asp_out,
        "crop_aspect": asp_crop,
        "sensor_pixel_mode": f0.get("sensor_pixel_mode"),
    }


def load_rounds(paths):
    """把若干轮 r*.json 读成 rounds 列表。路径按文件名里的轮次号排序。"""
    rounds = []
    for p in sorted(paths, key=lambda x: os.path.basename(x)):
        try:
            rounds.append(extract_round(load_json(p), os.path.basename(p)))
        except Exception as e:
            print(f"[skip] {p}: {e}")
    return rounds


# ---------------------------------------------------------------- 输出

def print_report(cap, cam, res):
    dev = cap.get("device") or {}
    print("=" * 78)
    print("M0 机型能力评估")
    print("=" * 78)
    print(f"设备      : {dev.get('manufacturer')} {dev.get('model')}")
    print(f"Android   : {dev.get('android_release')} (API {dev.get('sdk_int')})")
    print(f"指纹      : {dev.get('build_fingerprint')}")
    if cam:
        print(f"评估摄像头: id={cam.get('id')} facing={cam.get('lens_facing')} "
              f"level={cam.get('supported_hardware_level')}")
    print(f"摄像头总数: {len(cap.get('cameras') or [])}")
    print("-" * 78)
    print(f"{'判据':<5} {'结论':<5} {'项目':<30} 说明")
    print("-" * 78)
    for gid, level, title, detail in res.rows:
        print(f"{gid:<5} {level:<5} {title:<30} {detail}")
    print("-" * 78)

    v = res.verdict()
    print(f"总判定: {v}   (PASS={res.count(PASS)} WARN={res.count(WARN)} "
          f"FAIL={res.count(FAIL)} INFO={res.count(INFO)})")
    print("-" * 78)

    if v == FAIL:
        print("结论：该机型不满足准入要求。")
        for gid, level, title, detail in res.rows:
            if level == FAIL:
                print(f"  - [{gid}] {title}: {detail}")
    elif v == INCOMPLETE:
        print("结论：关键判据尚未验证，**不得据此判定机型合格**。")
        print(f"  未验证项: {res.unverified}")
        print("  处置：在手机上运行 App 的①（静态能力探测）、②（锁定连拍验证）、"
              "③（跨会话重复性）、④（帧率权威性测验），再重跑本脚本。")
        print("        ③ 会写出 m0_locked_capture_r1..r3.json，G21 直接消费它们 ——"
              "「一次标定覆盖多次采集」是内参可复现性的地基，缺它不能判合格。")
        print("        若交付路线声明为拍照（--delivery still），还必须跑 ⑥ 拍照路径：")
        print("        ②③④⑤ 全是「TEMPLATE_MANUAL + setRepeatingRequest」的连续流，"
              "而交付走拍照 —— 模板/格式/调度方式任一不同，时间戳语义都可能不同，")
        print("        既有 PASS 一条也不能外推。⑥ 会写出 m0_still_shot.json，G26 消费它。")
        print("        现行路线是连续流 + 抽帧（D24，即 --delivery stream，默认）⇒ ⑥ 是可选项。")
        print("        缺哪一项就补哪一项；判据少跑一项就下结论，等于没做 M0。")
    elif v == WARN:
        print("结论：可用，但有需要处理的项目。")
        for gid, level, title, detail in res.rows:
            if level == WARN:
                print(f"  - [{gid}] {title}: {detail}")
    else:
        print("结论：完全满足准入要求，可以进入 M1。")
    print("=" * 78)


def adb_pull(out_dir, camera_id=None):
    """从设备上拉取报告到本地目录。

    **先列目录再拉取，不要按固定文件名清单硬拉。**
    早期版本按写死的名字（含一个 App 根本不产出的 m0_repeat.json）逐个 pull，
    于是每轮都会打印一行 "No such file or directory" —— 它看起来像故障，
    实际只是清单里有个不存在的名字；而真正缺的文件反而淹没在这些噪声里。
    更糟的是：清单是"我以为 App 会写什么"，一旦 App 改了命名，
    拉取就会静默少拿文件，而"少拿文件"看起来和"那一项没测"一模一样。
    """
    pkg = "com.mouse.phoneprobe"
    os.makedirs(out_dir, exist_ok=True)
    remote_dir = f"/sdcard/Android/data/{pkg}/files"

    ls = subprocess.run(["adb", "shell", "ls", remote_dir],
                        capture_output=True, text=True)
    if ls.returncode != 0:
        print(f"[adb] 无法列目录 {remote_dir}: "
              f"{(ls.stderr or ls.stdout).strip()}")
        return []

    names = [n.strip() for n in (ls.stdout or "").split()
             if n.strip().endswith(".json")]
    if not names:
        print(f"[adb] {remote_dir} 里没有 .json 报告 —— 先在手机上跑 ①②③④")
        return []

    got, missing = [], []
    for name in sorted(names):
        local = os.path.join(out_dir, name)
        r = subprocess.run(["adb", "pull", f"{remote_dir}/{name}", local],
                           capture_output=True, text=True)
        if r.returncode == 0 and os.path.exists(local):
            got.append(local)
        else:
            missing.append(name)
    print(f"[adb] 设备上有 {len(names)} 个报告，成功拉取 {len(got)} 个: "
          f"{[os.path.basename(p) for p in got]}")
    if missing:
        print(f"[adb] 拉取失败: {missing}")

    # 关键判据所依赖的文件缺了哪一个，必须说清楚 —— "没拿到"和"没测"要分开
    need = [CAP_FILE, LC_FILE, SWEEP_FILE]
    absent = [n for n in need if n not in names]
    if absent:
        print(f"[adb] 设备上不存在: {absent} -> 对应判据会给 INCOMPLETE")
    rounds = [n for n in names if n.startswith("m0_locked_capture_r")]
    if not rounds:
        print(f"[adb] 设备上不存在 m0_locked_capture_r*.json -> "
              f"③ 跨会话重复性没跑过，G21 会给 INCOMPLETE")
    if STILL_FILE not in names:
        print(f"[adb] 设备上不存在 {STILL_FILE} -> "
              f"⑥ 拍照路径没跑过。**它是可选项**：D24 已把交付路线定为连续流，"
              f"只有交付改回拍照（--delivery still）时 G26 才会给 INCOMPLETE")
    return got


def main():
    ap = argparse.ArgumentParser(description="M0 机型能力评估")
    ap.add_argument("paths", nargs="*", help="JSON 文件或包含它们的目录")
    ap.add_argument("--pull", action="store_true", help="先 adb pull 再评估")
    ap.add_argument("--delivery", choices=["still", "stream"], default="stream",
                    help="交付路线声明：stream=连续流 + 按 sensor_timestamp 抽帧（默认，= D24）；"
                         "still=拍照/单张捕获（逆向后才用）。"
                         "**这是调用方声明的输入，不是测出来的事实** —— "
                         "它只改变「哪些判据阻塞交付」，不改变任何测量结论")
    ap.add_argument("--capture-format", choices=["yuv", "jpeg", "raw"], default="yuv",
                    help="交付时**交给 HAL 的输出格式**声明（默认 yuv，= D24）。"
                         "它是采集路径三元组的一员，必须与证据实际使用的格式一致（G27）："
                         "**由 YUV 再软件编码成 JPEG 不算换路径**，只有让 HAL 直接输出"
                         "另一种格式才是换采集路径")
    ap.add_argument("--pull-dir", default="m0_out", help="adb pull 落地目录")
    ap.add_argument("--camera", default=None, help="指定评估的摄像头 id")
    args = ap.parse_args()

    cap_path = None
    lc_path = None
    sw_path = None
    ss_path = None
    rp_path = None
    st_path = None
    round_paths = []

    if args.pull:
        got = adb_pull(args.pull_dir, args.camera)
        for p in got:
            b = os.path.basename(p)
            if b == CAP_FILE:
                cap_path = p
            elif b == LC_FILE:
                lc_path = p
            elif b == SWEEP_FILE:
                sw_path = p
            elif b == SIZE_SWEEP_FILE:
                ss_path = p
            elif b == STILL_FILE:
                st_path = p
            elif b == REPEAT_FILE:
                rp_path = p
            elif b.startswith("m0_locked_capture_r"):
                round_paths.append(p)
    else:
        for p in args.paths:
            if os.path.isdir(p):
                for name in (CAP_FILE, LC_FILE, SWEEP_FILE, SIZE_SWEEP_FILE,
                             STILL_FILE, REPEAT_FILE):
                    f = os.path.join(p, name)
                    if os.path.exists(f):
                        if name == CAP_FILE:
                            cap_path = f
                        elif name == LC_FILE:
                            lc_path = f
                        elif name == SWEEP_FILE:
                            sw_path = f
                        elif name == SIZE_SWEEP_FILE:
                            ss_path = f
                        elif name == STILL_FILE:
                            st_path = f
                        else:
                            rp_path = f
                round_paths += sorted(glob.glob(os.path.join(p, REPEAT_GLOB)))
            elif os.path.basename(p) == CAP_FILE:
                cap_path = p
            elif os.path.basename(p) == LC_FILE:
                lc_path = p
            elif os.path.basename(p) == SWEEP_FILE:
                sw_path = p
            elif os.path.basename(p) == SIZE_SWEEP_FILE:
                ss_path = p
            elif os.path.basename(p) == STILL_FILE:
                st_path = p
            elif os.path.basename(p) == REPEAT_FILE:
                rp_path = p
            elif p.endswith(".json") and os.path.basename(p).startswith(
                    "m0_locked_capture_r"):
                round_paths.append(p)
            elif os.path.exists(p):
                # 文件名不是约定的名字时按 report_kind 判断。
                # 不能直接塞给 cap_path：那会把 locked 报告覆盖成 capability，
                # 导致"报告里没有摄像头信息"这种误导性报错。
                kind = ""
                try:
                    kind = str(load_json(p).get("report_kind", ""))
                except Exception:
                    pass
                if "size_sweep" in kind:
                    ss_path = p
                elif "still" in kind:
                    st_path = p
                elif "rate_sweep" in kind:
                    sw_path = p
                elif "repeat" in kind:
                    rp_path = p
                elif "locked" in kind:
                    lc_path = p
                elif cap_path is None:
                    cap_path = p

    if not cap_path:
        print(f"找不到 {CAP_FILE}。请先运行 App 的①，或加上 --pull。")
        return 2

    cap = load_json(cap_path)
    cam = pick_camera(cap, args.camera)
    if cam is None:
        print("报告里没有摄像头信息。")
        return 2

    locked = None
    if lc_path and os.path.exists(lc_path):
        locked = load_json(lc_path)
        # 若报告里有多个摄像头，确认是同一颗
        if str(locked.get("camera_id")) != str(cam.get("id")):
            print(f"[warn] 锁定连拍用的是摄像头 {locked.get('camera_id')}，"
                  f"而静态能力评估的是 {cam.get('id')}")

    sweep = None
    if sw_path and os.path.exists(sw_path):
        sweep = load_json(sw_path)

    size_sweep = None
    if ss_path and os.path.exists(ss_path):
        size_sweep = load_json(ss_path)

    still = None
    if st_path and os.path.exists(st_path):
        try:
            still = load_json(st_path)
        except Exception as e:
            print(f"[warn] 读不到 {st_path}: {e}")

    print(f"[delivery] 路线声明 = {args.delivery}"
          + ("（拍照/单张捕获）" if args.delivery == "still" else "（连续流 + 抽帧，D24）")
          + f"；交付采集格式 = {args.capture_format}"
          + " —— 两者都是**调用方声明的输入**，不是测出来的事实；"
            "它们只决定 G18 是否阻塞、G26/G27 怎么判，不改变任何测量结论")

    res = Result()
    evaluate_capability(cam, res)
    evaluate_locked(locked, cam, res, sweep)
    evaluate_rate_authority(sweep, res, args.delivery)
    evaluate_output_geometry(locked, cam, res)
    evaluate_size_choice(locked, size_sweep, cam, res)
    evaluate_timestamp_uncertainty(locked, cam, res, still)

    repeat = None
    if rp_path and os.path.exists(rp_path):
        try:
            repeat = load_json(rp_path)
        except Exception as e:
            print(f"[warn] 读不到 {rp_path}: {e}")
    valid_round_paths = [p for p in round_paths if os.path.exists(p)]
    rounds = load_rounds(valid_round_paths)
    # G23 需要**原始**报告（比对配置与批次），不只是抽出来的那几个量
    raw_rounds = []
    for p in valid_round_paths:
        try:
            raw_rounds.append(load_json(p))
        except Exception:
            pass
    # 采集路径登记表：G23 靠它把"版本号不同"翻译成"采集路径是否相同"
    global CAPTURE_REV_MAP
    try:
        CAPTURE_REV_MAP = load_json(os.path.join(script_dir(), CAPTURE_REV_MAP_FILE))
        _n = len(CAPTURE_REV_MAP.get("versions") or {})
        print(f"[rev] 采集路径登记表：{_n} 个版本（{CAPTURE_REV_MAP_FILE}）")
    except Exception as e:
        CAPTURE_REV_MAP = {}
        print(f"[warn] 读不到 {CAPTURE_REV_MAP_FILE}: {e} —— "
              "版本不一致时将判为「不可判定」而不是「没问题」")
    evaluate_report_set(locked, raw_rounds, sweep, res)
    evaluate_repeatability(repeat, rounds, res)
    evaluate_still_path(still, locked, cam, res, args.delivery)
    evaluate_delivery_format(args.delivery, args.capture_format, locked, raw_rounds, res)

    evaluate_multicam(cam, locked, res)
    print_report(cap, cam, res)

    v = res.verdict()
    if v == FAIL:
        return 1
    if v == INCOMPLETE:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
