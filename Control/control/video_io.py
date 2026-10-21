"""视频会话产物读取 + 时间轴自洽校验（改版二十四，设计方案 §11.26 §四 / §五）。

本模块**只读文件**，不跑视觉、不起子进程、不 import cv2 —— 因此可以被判据
（`tools/drill.py` 的 I38 / I39）在临时目录里直接喂合成产物验，不需要设备也不需要视频。

三条纪律：

* **产物只有一个权威**：`session.json` 是权威（帧统计、视频元数据、迷宫融合），
  `trajectory.csv` 是逐帧明细。两者不一致时报告，不"取平均"；
* **缺列/畸形要明确报错**，不许静默出 0 点 —— 一张空轨迹图看起来像"车没动"，
  而真实原因是文件格式变了（这正是最贵的那种静默错）；
* **时间口径二选一**（铁律 42）：`trust="seconds"` 才显示秒；VFR 或时间轴不自洽
  时 `trust="frames"`，界面只显示帧号，不宣称精确秒。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

#: 界面列视频时可选的扩展名（只在扫盘用，不做内容嗅探）。
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi")

#: 轨迹返回给前端的点数上限。8×8 场地画一屏几千个点没有信息增量，
#: 但一次会话可能几万帧 ⇒ 超过就等间隔抽样（**保留首末**，且标 `sampled`）。
MAX_POINTS = 2000

#: 时间戳抖动判定：相邻帧间隔偏离 `stride/fps` 超过这个比例算一次异常。
_JITTER_RATIO = 0.5

#: 异常帧占比超过这个值才判"时间轴不可信"（个别帧的容器抖动不该让整条轴作废）。
_JITTER_TOL = 0.05


class VideoError(RuntimeError):
    """会话产物不可用。消息面向现场操作者：说清**哪个文件、缺什么、下一步做什么**。"""


class VideoBusy(VideoError):
    """已经有一次解析在跑（端点据此回 409，而不是 500）。"""


class VideoNotFound(VideoError):
    """会话/文件不存在（端点据此回 404）。"""


def _num(v: Any) -> Optional[float]:
    """宽容地取一个数：`None` / 空串 / 非数字 ⇒ None（**不返回 0**）。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _truthy(v: Any) -> bool:
    """CSV 里的布尔：上游写 `True`/`False`，也兼容 1/0（不猜其它词）。"""
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in ("true", "1", "yes", "y")


def read_session(out_dir: str | Path) -> Dict[str, Any]:
    """读 `session.json`（权威产物）。"""
    p = Path(out_dir) / "session.json"
    if not p.is_file():
        raise VideoNotFound(
            f"这个会话没有 session.json：{p}。解析可能没跑完，或被取消了 —— "
            f"看同目录的 run.log 末尾几行。")
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise VideoError(f"session.json 读不出来（{type(e).__name__}）：{p}") from e
    if not isinstance(d, dict):
        raise VideoError(f"session.json 顶层不是对象：{p}")
    return d


def _row(raw: Dict[str, str], idx: int) -> Optional[Dict[str, Any]]:
    """一行 CSV → 精简轨迹点。帧号取不到就返回 None（这一行没有意义）。"""
    f = _num(raw.get("frame"))
    if f is None:
        return None
    x, y = _num(raw.get("x")), _num(raw.get("y"))
    valid = _truthy(raw.get("valid")) and x is not None and y is not None
    return {
        "frame": int(f),
        "t_video_s": _num(raw.get("t_video_s")),
        "valid": bool(valid),
        "x": x,
        "y": y,
        "rms_field": _num(raw.get("rms_field")),
        "reason": (raw.get("reason") or "").strip(),
        "_i": idx,
    }


def read_trajectory(out_dir: str | Path, limit: int = MAX_POINTS) -> Dict[str, Any]:
    """读 `trajectory.csv` → `{rows, n_total, n_valid, sampled, timebase, ...}`。

    `x/y` 是**场地归一化 [0,1]²**（Auto Locate 契约），前端映射画布时与 `arena` 同源。
    """
    p = Path(out_dir) / "trajectory.csv"
    if not p.is_file():
        raise VideoNotFound(
            f"这个会话没有 trajectory.csv：{p}。没有逐帧产物就没法画轨迹 —— "
            f"看同目录的 run.log 末尾几行。")
    rows: List[Dict[str, Any]] = []
    n_seen = 0
    try:
        with p.open("r", encoding="utf-8-sig", newline="") as fp:
            rd = csv.DictReader(fp)
            cols = rd.fieldnames or []
            need = ("frame", "t_video_s", "valid", "x", "y")
            miss = [c for c in need if c not in cols]
            if miss:
                raise VideoError(
                    f"trajectory.csv 缺列 {miss}（实际列：{cols}）。"
                    f"上游产物格式变了 —— 本层不猜列名，先看 Auto Locate 的 CHANGELOG。")
            for i, raw in enumerate(rd):
                n_seen += 1
                r = _row(raw, i)
                if r is not None:
                    rows.append(r)
    except OSError as e:
        raise VideoError(f"trajectory.csv 读不出来（{type(e).__name__}）：{p}") from e

    # 有内容却一行都解析不出来（帧号列全是空/非数字）—— 这必须报错。
    # 静默返回 0 点会画出一张空轨迹图，看起来像"车没动"，而原因是列格式变了。
    if n_seen and not rows:
        raise VideoError(
            f"trajectory.csv 有 {n_seen} 行，但没有一行能解析出帧号（frame 列全是空或非数字）：{p}")

    n_total = len(rows)
    sampled = False
    if limit and n_total > limit:
        # 等间隔抽样但**保留首末**：末尾那一帧往往是"车停在哪"，抽样抽掉了就看不出来。
        step = max(1, n_total // limit)
        rows = rows[::step]
        if rows and rows[-1].get("_i") != n_total - 1:
            rows.append(_row_from_last(p, n_total - 1))
        sampled = True

    return {
        "n_total": n_total,
        "n_valid": sum(1 for r in rows if r.get("valid")),
        "sampled": sampled,
        "rows": rows,
    }


def _row_from_last(p: Path, idx: int) -> Dict[str, Any]:
    """补最后一行（抽样时用）。失败就给一个占位——**不抛**，抽样不是致命错。"""
    try:
        with p.open("r", encoding="utf-8-sig", newline="") as fp:
            rd = csv.DictReader(fp)
            last = None
            for i, raw in enumerate(rd):
                if i == idx:
                    last = raw
                    break
        if last is not None:
            r = _row(last, idx)
            if r is not None:
                return r
    except OSError:
        pass
    return {"frame": -1, "t_video_s": None, "valid": False, "x": None, "y": None,
            "rms_field": None, "reason": "", "_i": idx}


def infer_stride(rows: List[Dict[str, Any]]) -> int:
    """从帧号差推出抽帧步长（众数，取 1 为兜底）。

    为什么不读配置：配置是"下次跑用多少"，而这里要判的是**这份产物**当初怎么跑的
    —— 上一次可能是 `--stride 3` 跑的，拿现在的配置 1 去判，会把正常数据判成抖动。
    """
    diffs: Dict[int, int] = {}
    prev = None
    for r in rows:
        f = r.get("frame")
        if f is None:
            continue
        if prev is not None:
            d = f - prev
            if d > 0:
                diffs[d] = diffs.get(d, 0) + 1
        prev = f
    if not diffs:
        return 1
    return max(diffs.items(), key=lambda kv: kv[1])[0]


def timebase_check(rows: List[Dict[str, Any]], video: Optional[Dict[str, Any]] = None,
                   *, stride: Optional[int] = None) -> Dict[str, Any]:
    """校验**识别侧给出来的这条时间轴**是否自洽（设计方案 §5.2）。

    用户拍板③是"识别侧保证时间轴一致，前端不处理对轴" ⇒ 本层不再做人工对轴，
    但仍然要**验**这条轴：拿一条自相矛盾的轴去画"12.34 s"，比不画更坏。

    三条（全部只看字段，不跑视觉）：

    ① `frame` 严格递增（`stride>1` 时步长是 `stride`，"严格递增"同样成立）；
    ② `t_video_s` 严格递增，且相邻差落在 `stride/fps` 的 ±50% 内（超出的比例过大才算）；
    ③ 与 `session.json.video` 自洽：末帧号 + 1 与 `frames` 相符、末行 `t_video_s` 与
       `末帧号/fps` 相符（容差 2 帧）。

    返回 `trust`：`"seconds"` 才允许界面显示秒；否则 `"frames"`（只显示帧号）。
    """
    issues: List[Dict[str, Any]] = []
    v = video if isinstance(video, dict) else {}
    fps = _num(v.get("fps_declared"))
    if fps is None:
        fps = _num(v.get("fps"))
    declared_frames = _num(v.get("frames"))
    vfr = bool(v.get("vfr_suspected")) or not (fps and fps > 0)
    if stride is None:
        stride = infer_stride(rows)
    stride = max(1, int(stride or 1))

    n = len(rows)
    if n == 0:
        issues.append({"code": "no_rows", "at": 0,
                       "detail": "轨迹是空的（一帧都没写进 CSV）"})

    # ① 帧号严格递增
    prev_f: Optional[int] = None
    for i, r in enumerate(rows):
        f = r.get("frame")
        if f is None:
            issues.append({"code": "frame_missing", "at": i, "detail": f"第 {i} 行没有帧号"})
            continue
        if prev_f is not None and f <= prev_f:
            issues.append({"code": "frame_gap", "at": i,
                           "detail": f"帧号在第 {i} 行从 {prev_f} 变成 {f}（不再递增）"})
            break
        prev_f = f

    # ② 时间戳单调 + 抖动
    prev_t: Optional[float] = None
    jitter = 0
    missing_ts = 0
    for i, r in enumerate(rows):
        t = r.get("t_video_s")
        if t is None:
            missing_ts += 1
            prev_t = None
            continue
        if prev_t is not None:
            dt = t - prev_t
            if dt <= 0:
                issues.append({"code": "ts_backwards", "at": i,
                               "detail": f"时间戳在第 {i} 行回退（{prev_t:.3f} → {t:.3f} s）"})
                break
            if fps:
                exp = stride / fps
                if abs(dt - exp) > _JITTER_RATIO * exp:
                    jitter += 1
        prev_t = t
    if missing_ts:
        issues.append({"code": "ts_missing", "at": missing_ts,
                       "detail": f"{missing_ts} 行没有 t_video_s（时间轴有洞）"})
    if fps and n > 1 and jitter > max(3, int(_JITTER_TOL * n)):
        issues.append({"code": "ts_jitter", "at": jitter,
                       "detail": f"{jitter}/{n} 处帧间隔偏离 1/fps 超过 50%"})

    # ③ 与 session.json.video 自洽
    last_f = None
    last_t = None
    for r in reversed(rows):
        if r.get("frame") is not None:
            last_f = r.get("frame")
            last_t = r.get("t_video_s")
            break
    if declared_frames and last_f is not None:
        if abs((last_f + 1) - declared_frames) > 2:
            issues.append({"code": "n_frames_mismatch", "at": last_f,
                           "detail": f"末帧号 {last_f} 与元数据 frames={int(declared_frames)} 不符"})
    if fps and last_f is not None and last_t is not None:
        exp_t = last_f / fps
        if abs(last_t - exp_t) > 2.0 / fps:
            issues.append({"code": "meta_mismatch", "at": last_f,
                           "detail": f"末行 t_video_s={last_t:.3f} 与 帧号/fps={exp_t:.3f} 不符"})

    ok = not issues
    return {
        "ok": ok,
        "fps": fps,
        "vfr": bool(vfr),
        "stride": stride,
        "n_rows": n,
        "declared_frames": int(declared_frames) if declared_frames else None,
        "issues": issues,
        # 口径二选一：只有"自洽 + CFR"才敢显示秒
        "trust": "seconds" if (ok and not vfr) else "frames",
    }


def list_sessions(out_root: str | Path) -> List[Dict[str, Any]]:
    """列 `out_root` 下已有会话（按名字倒序 = 新的在前）。

    扫盘是同步 IO ⇒ 调用方（端点）必须放 executor（铁律 3）。
    """
    root = Path(out_root)
    if not root.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for d in sorted(root.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        sj = d / "session.json"
        tc = d / "trajectory.csv"
        if not sj.is_file() and not tc.is_file():
            continue
        item: Dict[str, Any] = {"id": d.name, "has_session": sj.is_file(),
                                "has_trajectory": tc.is_file()}
        # 摘要只取"列表上要显示的那几个数"：读整份 session.json 只为三行没有必要
        if sj.is_file():
            try:
                d0 = json.loads(sj.read_text(encoding="utf-8"))
                fr = d0.get("frames") or {}
                vd = d0.get("video") or {}
                item.update({
                    "n_total": fr.get("n_total"),
                    "n_valid": fr.get("n_valid"),
                    "valid_ratio": fr.get("valid_ratio"),
                    "fps": vd.get("fps_declared") or vd.get("fps"),
                    "frames": vd.get("frames"),
                    "flags": list(d0.get("flags") or []),
                })
            except (OSError, json.JSONDecodeError):
                item["error"] = "session.json 读不出来"
        out.append(item)
    return out


def list_videos(root: str | Path) -> List[Dict[str, Any]]:
    """列视频目录（同步 IO ⇒ 调用方放 executor）。"""
    r = Path(root)
    if not r.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for p in sorted(r.iterdir(), key=lambda q: q.name.lower(), reverse=True):
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            size = None
        out.append({"name": p.name, "path": str(p), "size_bytes": size})
    return out
