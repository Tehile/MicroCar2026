"""场地定位（模块 C）适配器 —— 包 `aruco_locator`。

上游的两条硬约束：
  · `ArucoLocator` **线程不安全** ⇒ 单实例、单 worker（§7.3）。
  · 无效帧**不抛异常**，返回 `valid=False` + `reason` ⇒ 我们必须**留档**
    （`results.jsonl` 里 `valid=false` 的行也要在，且带 `reason`，判据 I7）。
    否则"哪一帧为什么失败"就永久丢了。

两段式（§4）：
  · **在线段** ≈2 fps（链路限，不是算力限）：只消费已 verified 的交付帧，有界队列，
    满了**丢最旧并显式计数**，绝不阻塞 `hostlink.events()` 的消费。
  · **赛后批量段** ≈10 fps 且不占网：停采排空后本地全量重算 —— **这条才是建模用的
    权威数据**（覆盖每一帧）。
"""

from __future__ import annotations

import asyncio
import csv
import json
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..bus import Bus
from ..clock import now_ms
from ..decode import decode_y, frames_in, load_meta, resize_for_locate
from . import probe_module


class LocatorError(RuntimeError):
    """定位侧操作失败。"""


class LocatorAdapter:
    def __init__(self, cfg: Any, bus: Bus, store: Any) -> None:
        self.cfg = cfg
        self.bus = bus
        self.store = store
        self._import_error: Optional[str] = None
        self._lock = threading.RLock()
        self._loc = None                                    # ArucoLocator 单实例
        self._mod = None
        self._worker_running = False
        self._queue: "queue.Queue[Tuple[dict, Optional[Path]]]" = queue.Queue(
            maxsize=max(1, int(cfg.get("locator.online.queue_size", 8))))
        self._dropped = 0
        self._proc_lock = threading.Lock()                  # 保证"单实例单线程"
        self._runs: Dict[str, dict] = {}
        self._run_seq = 0
        self._last_out: Optional[Path] = None       # 最近一次批量任务的输出目录
        self._online_enabled = bool(cfg.get("locator.online.enabled", True))
        self._stats = {"processed": 0, "valid": 0, "invalid": 0,
                       "latency_ms_sum": 0.0, "latency_ms_max": 0.0, "dropped": 0}
        self._load()

    # ------------------------------------------------------------ 装载
    def _load(self) -> None:
        paths = self.cfg.get("paths.autolocate", "")
        mod, err = probe_module("aruco_locator", paths)
        if err:
            self._import_error = err
            self.bus.log("warn", f"定位模块不可用：{err.splitlines()[0]}")
            return
        try:
            from aruco_locator import (create_locator, parameter_catalog,   # noqa: F401
                                       apply_params, apply_profile, flatten_config,
                                       describe_profiles)
            self._mod = mod
            self._mk = create_locator
            self._catalog = parameter_catalog
            self._apply_params = apply_params
            self._apply_profile = apply_profile
            self._flatten = flatten_config
            self._profiles = describe_profiles
        except Exception as e:                              # noqa: BLE001
            self._import_error = f"aruco_locator 可导入但门面取不到：{type(e).__name__}: {e}"
            return
        self._import_error = None

    @property
    def available(self) -> bool:
        return self._import_error is None

    @property
    def import_error(self) -> Optional[str]:
        return self._import_error

    def _ensure(self) -> Any:
        if not self.available:
            raise LocatorError(f"定位模块不可用。\n{self._import_error}")
        with self._lock:
            if self._loc is None:
                cfgp = self.cfg.get("locator.config")
                try:
                    self._loc = self._mk(str(cfgp))
                except Exception as e:                      # noqa: BLE001
                    raise LocatorError(
                        f"装配定位器失败：{type(e).__name__}: {e}\n"
                        f"  · 定位器配置：{cfgp}\n"
                        f"  · 检查 camera.parallax.camera_height_mm（必填，见设计方案 §7.4 / U2）。"
                        f"该字段为 null 通常仅产生 parallax_uncalibrated 标志；"
                        f"若装配阶段失败，须另行排查。") from e
                try:
                    prof = self.cfg.get("locator.profile")
                    if prof and prof != "default":
                        self._apply_profile(self._loc, str(prof))
                except Exception as e:                      # noqa: BLE001
                    self.bus.log("warn", f"应用 profile {prof!r} 失败：{e}")
            return self._loc

    # ------------------------------------------------------------ 参数
    def catalog(self) -> List[dict]:
        if not self.available:
            raise LocatorError(self._import_error or "定位模块不可用")
        return self._catalog()

    def params(self) -> dict:
        return self._flatten(self._ensure())

    def profiles(self) -> List[dict]:
        if not self.available:
            raise LocatorError(self._import_error or "定位模块不可用")
        return self._profiles()

    def set_params(self, params: dict) -> dict:
        """写参数。**自动类型转换 + 范围校验**（上游的职责）；超范围会抛 ValueError。"""
        loc = self._ensure()
        with self._lock:
            try:
                self._apply_params(loc, params)
            except (KeyError, ValueError, TypeError) as e:
                raise LocatorError(
                    f"参数写入失败：{type(e).__name__}: {e}\n"
                    f"  · 键名必须为 parameter_catalog() 中的点号路径（或叶子名简写）；"
                    f"上游不会静默忽略写错的键。") from e
            self._invalidate()
            return self._flatten(loc)

    def set_profile(self, name: str) -> dict:
        loc = self._ensure()
        with self._lock:
            try:
                self._apply_profile(loc, name)
            except (KeyError, ValueError) as e:
                raise LocatorError(f"没有这个 profile：{name!r}（可选见 /locate/profiles）") from e
            self._invalidate()
            return self._flatten(loc)

    def _invalidate(self) -> None:
        """改完参数要让依赖它的指纹失效（run_meta 里记的是指纹）。"""
        self._fingerprint_cache = None

    def fingerprint(self) -> dict:
        if getattr(self, "_fingerprint_cache", None) is None:
            loc = self._ensure()
            p = self._flatten(loc)
            interesting = ("camera.parallax.camera_height_mm", "camera.parallax.nadir_xy",
                           "camera.field_size_mm", "detection.target_id",
                           "geometry.vertex_mode", "detection.edge_refine",
                           "output.smooth", "camera.undistort.enabled")
            self._fingerprint_cache = {k: p.get(k) for k in interesting if k in p}
        return self._fingerprint_cache

    # ------------------------------------------------------------ 在线段
    def set_online(self, on: bool) -> None:
        self._online_enabled = bool(on)

    @property
    def online_enabled(self) -> bool:
        return self._online_enabled

    def submit_frame(self, ev: dict, path: Optional[Path]) -> None:
        """帧监听回调：把一帧塞进**有界队列**。

        满了丢最旧 + 显式计数（§4.2）。**绝不阻塞** —— 这个函数是在相机事件线程里
        被调用的，阻塞它等于把 `hostlink.events()` 的消费拖死，后果是"采集本身卡了"。
        """
        if not self._online_enabled or not self.available:
            return
        if self._offline_playback and ev.get("_offline"):
            pass                                    # 离线回放也走在线段（同一条路径）
        cap = max(1, int(self.cfg.get("locator.online.queue_size", 8)))
        if self._queue.maxsize != cap:
            self._queue.maxsize = cap
        try:
            self._queue.put_nowait((ev, path))
        except queue.Full:
            try:
                self._queue.get_nowait()             # 丢最旧
            except queue.Empty:
                pass
            self._dropped += 1
            self._stats["dropped"] = self._dropped
            try:
                self._queue.put_nowait((ev, path))
            except queue.Full:
                pass
            if self._dropped in (1, 10, 100, 1000):
                self.bus.log("warn",
                             f"在线定位积压：已丢 {self._dropped} 帧（queue_size={cap}）。"
                             f"有界队列满时丢弃最旧帧，避免阻塞事件消费。"
                             f"全量数据改用赛后批量（逐帧覆盖）")

    _offline_playback = False

    def start_online(self) -> None:
        if not self.available or self._worker_running:
            return
        self._worker_running = True
        threading.Thread(target=self._online_loop, name="locate-online",
                         daemon=True).start()

    def stop_online(self) -> None:
        self._worker_running = False

    def _online_loop(self) -> None:
        warn = float(self.cfg.get("locator.online.warn_latency_ms", 500))
        while self._worker_running:
            try:
                ev, path = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if path is None or not Path(path).is_file():
                continue
            t0 = now_ms()
            try:
                loc_d = self._locate_file(path, seq=ev.get("seq"),
                                          t_pc_ms=ev.get("t_pc_ms"))
            except Exception as e:                          # noqa: BLE001
                self.bus.log("warn", f"在线定位失败（已隔离，不中断）："
                                     f"{type(e).__name__}: {e}")
                continue
            lat = now_ms() - t0
            self._stats["processed"] += 1
            self._stats["latency_ms_sum"] += lat
            self._stats["latency_ms_max"] = max(self._stats["latency_ms_max"], lat)
            self._stats["valid" if loc_d.get("valid") else "invalid"] += 1
            sess = self.store.current
            if sess is not None:
                key = "locate_valid" if loc_d.get("valid") else "locate_invalid"
                sess.counts[key] = sess.counts.get(key, 0) + 1
            loc_d["_latency_ms"] = lat
            loc_d["_online"] = True
            self.bus.publish("locate.result", loc_d)
            self._append_jsonl(loc_d, out_dir=self.online_out_dir())
            # ⚠️ ref 用 `_seq`（我们显式写进去的那个），不要用 `frame_index` ——
            # 后者是定位器内部的帧号概念，实测恒为 0，写进时间轴就成了
            # "frame_000000" 连续 8 行，等于没记是第几帧。
            self.store.mark("locate", "localization",
                            f"frame_{int(loc_d.get('_seq') or 0):06d}",
                            _brief_loc(loc_d))
            if lat > warn:
                self.bus.log("warn", f"在线定位延迟 {lat} ms > 阈值 {warn:.0f} ms"
                                     f"（链路或算力不足；仅影响在线段，赛后批量不受影响）")

    def _locate_file(self, path: Path, seq: Optional[int] = None,
                     t_pc_ms: Optional[int] = None) -> dict:
        """一帧落盘文件 → Localization dict。**串行**（上游线程不安全）。"""
        loc = self._ensure()
        with self._proc_lock:
            bgr, meta = decode_y(path)
            dw = int(self.cfg.get("locator.offline.downsample_w", 0) or 0)
            bgr, scale = resize_for_locate(bgr, dw)
            res = loc.locate(bgr, frame_index=int(seq if seq is not None else meta.seq),
                             timestamp=(float(t_pc_ms) / 1000.0) if t_pc_ms else 0.0)
            d = res.to_dict(include_diagnostics=True, flat=False)
        d["_seq"] = int(seq if seq is not None else meta.seq)
        d["_frame_file"] = Path(path).name
        d["_frame_json"] = str(Path(path).with_suffix(".json"))
        d["_t_pc_ms"] = int(t_pc_ms) if t_pc_ms else None
        d["_wall_clock_ns"] = meta.wall_clock_ns
        d["_sensor_timestamp_ns"] = meta.sensor_timestamp_ns
        d["_decode"] = {"width": meta.width, "height": meta.height,
                        "y_stride": meta.y_stride, "repacked": meta.repacked,
                        "padded": meta.padded, "downsample_scale": scale}
        return d

    def submit_path(self, path: Path, seq: Optional[int] = None,
                    t_pc_ms: Optional[int] = None) -> dict:
        """同步解一帧（给 `/locate/preview` 用：调参时马上看结果）。"""
        return self._locate_file(Path(path), seq=seq, t_pc_ms=t_pc_ms)

    def annotate_file(self, path: Path, seq: Optional[int] = None) -> Tuple[bytes, dict]:
        """解一帧并画叠加图，返回 (JPEG bytes, 结果 dict)。"""
        import cv2
        loc = self._ensure()
        with self._proc_lock:
            bgr, meta = decode_y(path)
            dw = int(self.cfg.get("locator.offline.downsample_w", 0) or 0)
            bgr, scale = resize_for_locate(bgr, dw)
            res = loc.locate(bgr, frame_index=int(seq if seq is not None else meta.seq))
            try:
                out = loc.annotate(bgr, res)
            except Exception:                               # noqa: BLE001
                out = bgr
            ok, buf = cv2.imencode(".jpg", out, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        if not ok:
            raise LocatorError("叠加图编码失败（cv2.imencode 返回 False）")
        return buf.tobytes(), res.to_dict(include_diagnostics=True)

    # ------------------------------------------------------------ 在线产物
    def online_out_dir(self) -> Path:
        """**在线**结果写哪：会话开着就写会话的 `locate/`；否则写"孤儿"目录。

        **绝不往手机会话目录里写**（§4.3）：那是上游交付物的领地，往里写东西会破坏
        "这个目录就是 App 交付的东西"这个不变量，也会让上游的账本校验面对陌生文件。
        """
        s = self.store.current
        if s is not None:
            return s.locate_dir
        d = self.cfg.session_root / "_orphan" / "locate"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def result_dir(self) -> Path:
        """**读**结果时用哪：优先当前会话，其次最近一次批量任务的输出目录。

        为什么需要这一步：收会话之后 `store.current` 就是 None 了，但那份结果就在
        刚收掉的那个会话的 `locate/` 里。若这里直接返回 `_orphan/`，【数据】页
        会把"刚跑完的权威结果"显示成"还没有结果" —— 而它们只是隔了一层目录。
        """
        s = self.store.current
        if s is not None:
            return s.locate_dir
        last = getattr(self, "_last_out", None)
        if last and (Path(last) / "results.jsonl").is_file():
            return Path(last)
        d = self.cfg.session_root / "_orphan" / "locate"
        if (d / "results.jsonl").is_file():
            return d
        return d

    def _append_jsonl(self, d: dict, out_dir: Path) -> None:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            with open(out_dir / "results_online.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(d, ensure_ascii=False, default=str) + "\n")
        except OSError as e:
            self.bus.log("warn", f"在线结果落盘失败：{e}")

    # ------------------------------------------------------------ 赛后批量段（权威数据）
    def start_batch(self, frame_dir_or_session: str, out_dir: Optional[str] = None,
                    limit: int = 0, skip_existing: bool = True) -> str:
        src = Path(frame_dir_or_session)
        # ---- 去重：同一个 src 上刚跑过或还在跑的，直接复用，不新起一个 ----
        #
        # 为什么需要：`delivery_done` 与「收会话」都会触发赛后复算，两条路都可能走。
        # 第二次跑会因 `skip_existing` 把全部帧跳过，然后在 run_meta 里写
        # `valid=0 invalid=0` —— 那个数字**看起来像灾难性失败**，而真相是"没跑任何帧"。
        # 与其在报告里解释，不如不让它发生。
        for r in self._runs.values():
            if r.get("src") != str(src):
                continue
            if r["state"] == "running":
                return r["id"]
            if r["state"] == "done" and (now_ms() - (r.get("ended_ms") or 0)) < 10000:
                return r["id"]

        self._run_seq += 1
        rid = f"run{self._run_seq:03d}"
        jps = frames_in(src) if (src / "frames").is_dir() else sorted(src.glob("frame_*.json"))
        if limit:
            jps = jps[:limit]
        out = Path(out_dir) if out_dir else (self.online_out_dir().parent / "locate")
        out.mkdir(parents=True, exist_ok=True)
        self._last_out = out
        self._runs[rid] = {
            "id": rid, "state": "running", "total": len(jps), "done": 0,
            "valid": 0, "invalid": 0, "skipped": 0,
            "started_ms": now_ms(), "ended_ms": None,
            "src": str(src), "out": str(out), "abort": threading.Event(),
            "error": None, "elapsed_ms": 0, "rate_per_s": 0.0,
            "invalid_reasons": {},
        }
        threading.Thread(target=self._batch_loop, args=(rid, jps, out, skip_existing),
                         name=f"locate-{rid}", daemon=True).start()
        self.bus.log("info", f"赛后批量定位已启动：{rid}，共 {len(jps)} 帧 → {out}")
        return rid

    def _batch_loop(self, rid: str, jps: List[Path], out: Path, skip_existing: bool) -> None:
        run = self._runs[rid]
        jl = out / "results.jsonl"
        done_seqs = set()
        if skip_existing and jl.is_file():
            try:
                with open(jl, encoding="utf-8") as f:
                    for line in f:
                        try:
                            done_seqs.add(int(json.loads(line).get("_seq")))
                        except (ValueError, TypeError, json.JSONDecodeError):
                            pass
            except OSError:
                pass
        t0 = time.time()
        mode = "a" if (skip_existing and jl.is_file() and done_seqs) else "w"
        try:
            with open(jl, mode, encoding="utf-8") as f:
                for jp in jps:
                    if run["abort"].is_set():
                        run["state"] = "aborted"
                        break
                    try:
                        meta = load_meta(jp)
                    except (OSError, ValueError, json.JSONDecodeError) as e:
                        self._bump_reason(run, f"meta_broken:{type(e).__name__}")
                        run["done"] += 1
                        continue
                    if meta.seq in done_seqs:
                        run["skipped"] += 1
                        run["done"] += 1
                        continue
                    try:
                        d = self._locate_file(jp, seq=meta.seq)
                    except Exception as e:                  # noqa: BLE001
                        self._bump_reason(run, f"exception:{type(e).__name__}")
                        run["done"] += 1
                        continue
                    f.write(json.dumps(d, ensure_ascii=False, default=str) + "\n")
                    f.flush()
                    run["done"] += 1
                    if d.get("valid"):
                        run["valid"] += 1
                    else:
                        run["invalid"] += 1
                        self._bump_reason(run, str(d.get("reason") or "unknown"))
                    if run["done"] % 10 == 0 or run["done"] == run["total"]:
                        el = (time.time() - t0) or 1e-6
                        run["elapsed_ms"] = int(el * 1000)
                        run["rate_per_s"] = round(run["done"] / el, 2)
                        self.bus.publish("locate.batch", self.run_status(rid))
        except OSError as e:
            run["state"] = "failed"
            run["error"] = f"写不出结果：{e}"
        else:
            if run["state"] == "running":
                run["state"] = "done"
        run["ended_ms"] = now_ms()
        # ---- 以**产物文件**为准重算计数 ----
        #
        # 为什么不直接用循环里累加的数：`skip_existing` 会把已算过的帧跳过，
        # 于是"本次新算的"与"这个文件里实际有多少有效帧"是两件事。报告要说的是后者，
        # 因为它才是别人拿去建模时看到的东西。这条与上游"不采信自述、只认落盘"同源。
        self._recount_from_file(jl, run)
        self._write_run_meta(rid, jps, out)
        self._write_results_csv(out)
        self.bus.publish("locate.batch", self.run_status(rid))
        self.bus.log("info", f"赛后批量定位 {run['state']}：{rid} "
                             f"{run['done']}/{run['total']}（跳过已算 {run['skipped']}），"
                             f"文件内有效 {run['valid']}、无效 {run['invalid']}"
                             f"（无效帧亦留档，带 reason）")

    def _recount_from_file(self, jl: Path, run: dict) -> None:
        if not jl.is_file():
            return
        valid = invalid = 0
        reasons: Dict[str, int] = {}
        try:
            with open(jl, encoding="utf-8") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("valid"):
                        valid += 1
                    else:
                        invalid += 1
                        r = str(d.get("reason") or "unknown")
                        reasons[r] = reasons.get(r, 0) + 1
        except OSError:
            return
        run["valid"] = valid
        run["invalid"] = invalid
        run["invalid_reasons"] = reasons

    @staticmethod
    def _bump_reason(run: dict, reason: str) -> None:
        run["invalid_reasons"][reason] = run["invalid_reasons"].get(reason, 0) + 1

    def _write_run_meta(self, rid: str, jps: List[Path], out: Path) -> None:
        run = self._runs[rid]
        fp = self.fingerprint()
        meta = {
            "run_id": rid,
            "state": run["state"],
            "started_at": run["started_ms"],
            "ended_at": run["ended_ms"],
            "elapsed_ms": run["elapsed_ms"],
            "rate_per_s": run["rate_per_s"],
            "src": run["src"],
            "out": run["out"],
            "total_frames": run["total"],
            "processed": run["done"],
            "skipped_already_done": run.get("skipped", 0),
            "valid": run["valid"],
            "invalid": run["invalid"],
            "invalid_reasons": run["invalid_reasons"],
            "counts_source": "以 results.jsonl 为准重新点数（不是本次循环的累加值）",
            "locator_fingerprint": fp,
            "profile": self.cfg.get("locator.profile"),
            "downsample_w": self.cfg.get("locator.offline.downsample_w"),
            "note": "赛后批量结果：覆盖每一帧、不占网，为建模用的权威数据。"
                    "在线段（≈2 fps）仅用于现场观察。",
        }
        try:
            (out / "run_meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        except OSError as e:
            self.bus.log("warn", f"run_meta.json 写不出：{e}")

    def _write_results_csv(self, out: Path) -> None:
        """从 jsonl 生成扁平 CSV（`to_row()` 的等价物：多值字段压成分号串）。"""
        jl = out / "results.jsonl"
        if not jl.is_file():
            return
        cols = ["_seq", "_t_pc_ms", "valid", "x", "y", "theta_deg", "x_raw", "y_raw",
                "theta_deg_raw", "dir_x", "dir_y", "rms_field", "target_rms",
                "marker_scale", "scale_cv", "origin_id", "corner_ids", "missing_corner_ids",
                "flags", "reason"]
        try:
            with open(jl, encoding="utf-8") as fi, \
                    open(out / "results.csv", "w", newline="", encoding="utf-8-sig") as fo:
                w = csv.writer(fo)
                w.writerow(cols)
                for line in fi:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    row = []
                    for c in cols:
                        v = d.get(c)
                        if isinstance(v, list):
                            v = ";".join(str(x) for x in v)
                        row.append(v)
                    w.writerow(row)
        except OSError as e:
            self.bus.log("warn", f"results.csv 写不出：{e}")

    def run_status(self, rid: str) -> dict:
        run = self._runs.get(rid)
        if run is None:
            raise LocatorError(f"没有这个批量任务：{rid}")
        return {k: v for k, v in run.items() if k != "abort"}

    def abort_run(self, rid: str) -> dict:
        run = self._runs.get(rid)
        if run is None:
            raise LocatorError(f"没有这个批量任务：{rid}")
        run["abort"].set()
        return self.run_status(rid)

    def runs(self) -> List[dict]:
        return [self.run_status(k) for k in self._runs]

    # ------------------------------------------------------------ 结果读
    def results(self, limit: int = 500, offset: int = 0,
                out_dir: Optional[str] = None) -> dict:
        out = Path(out_dir) if out_dir else self.result_dir()
        jl = out / "results.jsonl"
        if not jl.is_file():
            jl = out / "results_online.jsonl"
        if not jl.is_file():
            return {"total": 0, "offset": offset, "items": [],
                    "note": "该会话尚无定位结果"}
        items: List[dict] = []
        total = 0
        with open(jl, encoding="utf-8") as f:
            for line in f:
                total += 1
                if total <= offset:
                    continue
                if len(items) >= limit:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return {"total": total, "offset": offset, "limit": limit, "items": items,
                "file": str(jl)}

    def track(self, out_dir: Optional[str] = None) -> List[dict]:
        """轨迹点数组：`[t_pc_ms, x, y, theta, valid]`（前端一次拿走，走二进制）。"""
        r = self.results(limit=10 ** 7, out_dir=out_dir)
        pts = []
        for d in r["items"]:
            pts.append({"seq": d.get("_seq"), "t_pc_ms": d.get("_t_pc_ms"),
                        "x": d.get("x"), "y": d.get("y"),
                        "theta_deg": d.get("theta_deg"), "valid": bool(d.get("valid")),
                        "reason": d.get("reason") or ""})
        return pts

    # ------------------------------------------------------------ 健康
    def health(self) -> dict:
        if not self.available:
            return {"ready": False, "profile": None, "detail": self._import_error,
                    "available": False}
        det = ""
        prof = self.cfg.get("locator.profile")
        flags: List[str] = []
        try:
            loc = self._ensure()
            fp = self.fingerprint()
            if fp.get("camera.parallax.camera_height_mm") in (None, 0):
                flags.append("parallax_uncalibrated")
                det = "视差未标定：位置约有 1.4% 场地边长（≈20 mm）系统偏差"
        except LocatorError as e:
            return {"ready": False, "profile": prof, "detail": str(e), "available": True}
        return {
            "available": True, "ready": True, "profile": prof, "detail": det,
            "flags": flags, "online": self._online_enabled, "stats": self.stats(),
            "config": str(self.cfg.get("locator.config")),
            "fingerprint": self.fingerprint(),
        }

    def stats(self) -> dict:
        s = dict(self._stats)
        n = max(1, s["processed"])
        s["latency_ms_avg"] = round(s["latency_ms_sum"] / n, 2)
        s["dropped"] = self._dropped
        s["queue"] = {"size": self._queue.qsize(), "maxsize": self._queue.maxsize}
        return s

    def caps(self) -> dict:
        if not self.available:
            return {"available": False, "error": self._import_error}
        return {
            "available": True,
            "version": getattr(self._mod, "__version__", None),
            "profiles": [p.get("name") for p in self.profiles()],
            "params": len(self.catalog()),
            "two_stage": {
                "online": {"fps": "≈2", "source": "已 verified 的交付帧",
                           "note": "链路限 5.5 MB/s"},
                "offline": {"fps": "≈10", "source": "本地全量重算",
                            "note": "覆盖每一帧"},
            },
            "authoritative": "offline",
        }


def _brief_loc(d: dict, n: int = 140) -> str:
    if d.get("valid"):
        s = (f"valid=1 x={_f(d.get('x'))} y={_f(d.get('y'))} "
             f"th={_f(d.get('theta_deg'))}")
    else:
        s = f"valid=0 reason={d.get('reason')}"
    extra = f" lat={d.get('_latency_ms')}ms" if d.get("_latency_ms") else ""
    return (s + extra)[:n]


def _f(v: Any) -> str:
    try:
        return f"{float(v):.4f}"
    except (TypeError, ValueError):
        return "None"
