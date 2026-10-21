"""视频会话执行器（改版二十四）：起一次 Auto Locate 解析，**子进程 + 状态轮询**。

为什么是子进程而不是在线程里 `run_session()`：

* 一次解析几分钟到十几分钟，要能**取消**（`kill`）；线程里的 OpenCV 循环没法安全打断；
* 上游崩溃/`import` 失败不会把本服务带走（视觉依赖是重型外部件）；
* 日志天然分开（`run.log`），现场排查时"这一次为什么失败"有原文可看。

四条纪律：

1. **端点零阻塞**（铁律 3）：这里只做 `Popen` 与内存状态读写；扫盘/读产物由端点放 executor；
2. **不给假进度**：上游没有进度回调 ⇒ 只报 `elapsed_s`，不编百分比；
3. **编码由子进程接管**（铁律 19）：`PYTHONIOENCODING/PYTHONUTF8/PYTHONUNBUFFERED`；
4. **产物为准**：`done` 之后从 `session.json` 读摘要，不拿子进程 stdout 当结果
   （stdout 只有一行人类可读的摘要，格式随时可能变）。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..clock import now_ms
from ..video_io import VideoBusy, VideoError, VideoNotFound, read_session


def _stamp(ms: int) -> str:
    """`now_ms` → `YYYYmmdd_HHMMSS`（本地时区，与记录文件名同一口径）。"""
    t = time.localtime(ms / 1000.0)
    return time.strftime("%Y%m%d_%H%M%S", t)


def _safe_stem(name: str) -> str:
    """只留 `[A-Za-z0-9_-]`，其余换 `-`；空 ⇒ `video`。

    产物目录名**不保留中文**：上游能处理中文路径，但会话 id 还会进 URL
    （`/api/v1/video/session/{id}`）；让它只含 ASCII 能省掉一层转义问题。
    """
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", (name or "").strip())
    s = s.strip("-")
    return s[:40] or "video"


class VideoAdapter:
    """一次只跑一个会话（同一台机器上并发跑视觉只会互相拖慢，且没有业务场景）。"""

    def __init__(self, cfg: Any, bus: Any) -> None:
        self.cfg = cfg
        self.bus = bus
        self.root = Path(str(cfg.get("video.root") or (cfg.root / "video")))
        self.out_root = Path(str(cfg.get("video.out_root") or (cfg.root / "records/video")))
        self.config_path = str(cfg.get("video.config") or "")
        self.stride = max(1, int(cfg.get("video.stride") or 1))
        self.maze = bool(cfg.get("video.maze", True))
        self.timeout_s = float(cfg.get("video.timeout_s") or 1800)
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._log_fp = None
        self._job: Dict[str, Any] = {"id": None, "state": "idle"}

    # ------------------------------------------------------------ 只读
    def caps(self) -> Dict[str, Any]:
        """零设备零扫盘的静态能力（`/caps` 同步调用，绝不能摸盘）。"""
        return {
            "root": str(self.root),
            "out_root": str(self.out_root),
            "config": self.config_path,
            "config_ok": bool(self.config_path) and Path(self.config_path).is_file(),
            "stride": self.stride,
            "maze": self.maze,
            "timeout_s": self.timeout_s,
            "video_exts": [".mp4", ".mov", ".mkv", ".avi"],
        }

    def status(self) -> Dict[str, Any]:
        """**纯内存**状态（铁律 3：状态端点不许扫盘/读文件）。"""
        with self._lock:
            j = dict(self._job)
            proc = self._proc
        st = j.get("state") or "idle"
        if st == "running":
            t0 = j.get("started_ms") or now_ms()
            j["elapsed_s"] = round((now_ms() - t0) / 1000.0, 1)
            j["pid"] = getattr(proc, "pid", None)
        else:
            j.setdefault("elapsed_s", None)
        return j

    # ------------------------------------------------------------ 启动 / 取消
    def run(self, source: str, *, start_s: Optional[float] = None,
            end_s: Optional[float] = None, stride: Optional[int] = None,
            maze: Optional[bool] = None) -> Dict[str, Any]:
        """起一次解析。已在跑 ⇒ 抛 `VideoError`（端点翻 409）。"""
        src = Path(str(source or "").strip())
        if not str(src):
            raise VideoError("没给视频路径：填一个服务器上的绝对路径，或从列表里选。")
        if not src.is_file():
            raise VideoError(f"视频文件不存在：{src}。检查路径（含盘符）与文件名大小写。")

        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                raise VideoBusy(
                    f"已经有一次解析在跑（{self._job.get('id')}）。"
                    f"等它结束，或先点【取消】。")
            sid = f"{_stamp(now_ms())}_{_safe_stem(src.stem)}"
            out_dir = self.out_root / sid
            n = 2
            while out_dir.exists():
                out_dir = self.out_root / f"{sid}_{n}"
                n += 1
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise VideoError(f"建不了产物目录：{out_dir}（{type(e).__name__}: {e}）") from e

            cmd = [sys.executable, str(Path(__file__).resolve().parents[2] / "tools" / "video_session.py"),
                   "--source", str(src), "--out", str(out_dir),
                   "--stride", str(int(stride or self.stride))]
            if self.config_path:
                cmd += ["--config", self.config_path]
            if start_s is not None:
                cmd += ["--start-s", str(float(start_s))]
            if end_s is not None:
                cmd += ["--end-s", str(float(end_s))]
            use_maze = self.maze if maze is None else bool(maze)
            if not use_maze:
                cmd += ["--no-maze"]

            env = dict(os.environ)
            # 铁律 19：子进程接管编码，不让 Windows 控制台代码页决定日志长什么样
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            env["PYTHONUNBUFFERED"] = "1"
            # 子进程与本机服务无关，但 `NO_PROXY` 一起带上：上游若走 HTTP 也不会被代理劫持
            env["NO_PROXY"] = "127.0.0.1,localhost"
            env["no_proxy"] = "127.0.0.1,localhost"

            log_path = out_dir / "run.log"
            try:
                log_fp = open(log_path, "wb")
                proc = subprocess.Popen(
                    cmd, cwd=str(Path(__file__).resolve().parents[2]),
                    stdout=log_fp, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as e:
                try:
                    log_fp.close()
                except Exception:                             # noqa: BLE001
                    pass
                raise VideoError(f"起不了解析进程（{type(e).__name__}: {e}）") from e

            self._proc = proc
            self._log_fp = log_fp
            self._job = {
                "id": sid, "state": "running", "source": str(src),
                "out_dir": str(out_dir), "log": str(log_path),
                "started_ms": now_ms(), "elapsed_s": 0.0,
                "stride": int(stride or self.stride), "maze": use_maze,
                "flags": [], "error": None, "n_total": None, "n_valid": None,
                "cancel_requested": False,
            }
            job = dict(self._job)

        self.bus.log("info", f"视频会话开始：{src.name} → {out_dir.name}", source="video")
        threading.Thread(target=self._watch, args=(proc, job["id"], log_fp),
                         name=f"video-{job['id']}", daemon=True).start()
        return job

    def cancel(self) -> Dict[str, Any]:
        """kill 子进程。产物可能不完整 ⇒ 状态标 `cancelled`（不冒充 done）。"""
        with self._lock:
            proc, j = self._proc, dict(self._job)
            if proc is None or proc.poll() is not None:
                return {"ok": False, "state": j.get("state"), "id": j.get("id"),
                        "note": "没有正在跑的解析"}
            self._job["cancel_requested"] = True
        try:
            proc.kill()
        except Exception as e:                                # noqa: BLE001
            return {"ok": False, "state": "running", "id": j.get("id"),
                    "error": f"{type(e).__name__}: {e}"}
        self.bus.log("warn", f"视频会话被取消：{j.get('id')}", source="video")
        return {"ok": True, "state": "cancelled", "id": j.get("id")}

    def shutdown(self) -> None:
        """服务收尾：只停**自己起的**那一个（铁律 25：别去杀别人的进程）。"""
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:                                 # noqa: BLE001
                pass

    # ------------------------------------------------------------ 监视线程
    def _watch(self, proc: subprocess.Popen, sid: str, log_fp) -> None:
        timed_out = False
        t0 = time.time()
        code: Optional[int] = None
        try:
            while True:
                try:
                    code = proc.wait(timeout=1.0)
                    break
                except subprocess.TimeoutExpired:
                    if self.timeout_s and (time.time() - t0) > self.timeout_s:
                        timed_out = True
                        try:
                            proc.kill()
                        except Exception:                     # noqa: BLE001
                            pass
                        code = proc.wait()
                        break
        finally:
            try:
                log_fp.close()
            except Exception:                                 # noqa: BLE001
                pass

        with self._lock:
            cancelled = bool(self._job.get("cancel_requested"))
            out_dir = Path(str(self._job.get("out_dir") or ""))
        state = "cancelled" if cancelled else ("failed" if (timed_out or code != 0) else "done")
        err = None
        summary: Dict[str, Any] = {"flags": [], "n_total": None, "n_valid": None}
        if state == "done":
            try:
                sj = read_session(out_dir)
                fr = sj.get("frames") or {}
                summary = {"flags": list(sj.get("flags") or []),
                           "n_total": fr.get("n_total"),
                           "n_valid": fr.get("n_valid"),
                           "valid_ratio": fr.get("valid_ratio")}
            except VideoError as e:
                # 退出码 0 却没有产物 —— 这比"失败"更该说清（不是超时、不是崩溃）
                state, err = "failed", f"进程退出码 0 但产物不可用：{e}"
        elif timed_out:
            err = f"超时（>{int(self.timeout_s)} s）被终止；产物可能不完整"
        elif not cancelled:
            err = self._tail_log(out_dir / "run.log")

        with self._lock:
            self._job.update({
                "state": state, "exit_code": code, "error": err,
                "finished_ms": now_ms(), "flags": summary["flags"],
                "n_total": summary["n_total"], "n_valid": summary["n_valid"],
            })
            if summary.get("valid_ratio") is not None:
                self._job["valid_ratio"] = summary["valid_ratio"]
            self._proc = None
            self._log_fp = None

        lvl = "info" if state == "done" else ("warn" if state == "cancelled" else "error")
        msg = (f"视频会话结束（{sid}）：{state}"
               + (f" · {summary['n_valid']}/{summary['n_total']} 帧有效"
                  if summary.get("n_total") is not None else "")
               + (f" · {err}" if err else ""))
        try:
            self.bus.log(lvl, msg, source="video")
        except Exception:                                     # noqa: BLE001
            pass

    @staticmethod
    def _tail_log(path: Path, n: int = 8) -> str:
        """失败时给**原文末尾**几行（不给"解析失败"四个字，那没法照着做）。

        两段解码（铁律 19）：先按 UTF-8，失败再按 GBK —— 子进程已强 UTF-8，
        但上游某些 C 库的 stderr 仍可能是本地代码页。
        """
        try:
            raw = path.read_bytes()[-8000:]
        except OSError:
            return "（没有日志文件）"
        txt = None
        for enc in ("utf-8", "gbk"):
            try:
                txt = raw.decode(enc, "replace")
                break
            except Exception:                                 # noqa: BLE001
                continue
        lines = [ln for ln in (txt or "").splitlines() if ln.strip()]
        return "\n".join(lines[-n:]) if lines else "（日志是空的）"

    # ------------------------------------------------------------ 产物
    def session(self, sid: str) -> Dict[str, Any]:
        """读一个会话的 `session.json` **摘要**（不含逐帧），顺带把时间轴校验带上。"""
        from ..video_io import infer_stride, read_trajectory, timebase_check

        out_dir = self._safe_out_dir(sid)
        sj = read_session(out_dir)
        # 时间轴校验必须用**全量行**（抽样后的行不连续，会把正常数据判成抖动）；
        # `stride` 不猜配置值，从数据本身推（上一次会话可能是别的步长跑的）。
        rows = read_trajectory(out_dir, limit=0).get("rows") or []
        tb = timebase_check(rows, sj.get("video"), stride=infer_stride(rows))
        fr = sj.get("frames") or {}
        vd = sj.get("video") or {}
        return {
            "id": sid,
            "out_dir": str(out_dir),
            "video": vd,
            "frames": fr,
            "maze": sj.get("maze"),
            "flags": list(sj.get("flags") or []),
            "timebase": tb,
        }

    def trajectory(self, sid: str, limit: int = 2000) -> Dict[str, Any]:
        """读轨迹点（给画布用）。时间轴校验用**全量行**，返回给前端的是抽样后的点。

        ⚠️ 抽样后的行不连续 ⇒ 不能拿它判抖动（会把正常数据判成抖），
        所以 `sampled` 时回读一次全量只为校验 —— 这是唯一允许的重复读。
        """
        from ..video_io import (infer_stride, read_session, read_trajectory,
                                timebase_check)

        out_dir = self._safe_out_dir(sid)
        tj = read_trajectory(out_dir, limit=limit)
        try:
            video = (read_session(out_dir).get("video") or {})
        except VideoError:
            video = {}
        base = read_trajectory(out_dir, limit=0).get("rows") if tj.get("sampled") else tj["rows"]
        tj["timebase"] = timebase_check(base or [], video, stride=infer_stride(base or []))
        tj["id"] = sid
        return tj

    def _safe_out_dir(self, sid: str) -> Path:
        """会话 id 只允许单层名字（防 `../` 穿出 out_root）。"""
        name = str(sid or "").strip()
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise VideoNotFound(f"会话 id 不合法：{sid!r}")
        d = self.out_root / name
        if not d.is_dir():
            raise VideoNotFound(f"没有这个会话：{name}（产物根 {self.out_root}）")
        return d


def _all_rows_for_check(out_dir: Path) -> List[Dict[str, Any]]:
    """（保留给外部脚本）读全量轨迹行。"""
    from ..video_io import read_trajectory
    return read_trajectory(out_dir, limit=0).get("rows") or []
