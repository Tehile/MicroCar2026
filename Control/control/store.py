"""统一会话目录 + 时间轴 + meta.json（设计方案 §5）。

目录布局见 §5.1：`mouse/` 是鼠侧原生产物（复用上游格式，原样），`camera.json` 只记
**引用**不搬帧（3.9 GB/会话，复制一份毫无意义），`locate/` 是定位产物，
`timeline.csv` 是"整合"这件事唯一的可验证产物。

原子性（§5.3）：三路同生同死。
    开：先开鼠会话（本地目录，失败代价≈0）→ 再起手机采集（物理副作用，贵）→
        任一步失败**回滚已成功的步骤**（判据 I5：不许留下半个会话目录）。
    停：先停手机采集 → 等 delivery_done → 再收鼠会话
        （因为要先把 `locate/` 写进那个会话，鼠会话必须最后收）。
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import __version__
from .clock import now_iso, now_ms

TIMELINE_HEADER = ["t_pc_ms", "t_pc_iso", "source", "kind", "ref", "note",
                   "t_src", "dev_ms"]

#: 会话 id 生成：`YYYY-MM-DD_HHMM_<4位随机>`
_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def _rmtree_quiet(path: Any) -> bool:
    """递归删掉一棵**我们自己刚建的**目录树。**绝不抛异常。**

    ⚠️ 为什么不用 `shutil.rmtree(..., ignore_errors=True)`：
    它只吞 `OSError`。而"删目录"这件事恰恰最容易碰到**非 OSError** 的干扰 ——
    杀毒软件占着文件、被进程环境包装过、甚至有人把 rmtree 换成一个会 `raise
    SystemExit` 的实现。这些都继承自 `BaseException`，会**穿过** `except Exception`
    一路冒到 aiohttp 的请求处理器外面，**把整个服务进程带走**（实测踩到过：
    一次开会话失败的回滚，最后表现成"服务整个没了"）。

    回滚是"收拾自己刚拉下的摊子"，它无论如何都必须成功或者安静地失败 ——
    绝不该成为把服务搞崩的那件事。所以这里自己走一遍 `os.scandir`，
    逐项吞掉**所有**异常（含 BaseException），并在最后如实返回是否删干净。
    """
    def _one(p: Any) -> None:
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                for name in os.listdir(p):
                    _one(os.path.join(p, name))
                os.rmdir(p)
            else:
                os.remove(p)
        except BaseException:                                 # noqa: BLE001
            pass

    try:
        if not os.path.exists(path):
            return True
        _one(str(path))
        return not os.path.exists(path)
    except BaseException:                                     # noqa: BLE001
        return False


def new_session_id(when: Optional[int] = None) -> str:
    import random
    ms = now_ms() if when is None else int(when)
    stamp = time.strftime("%Y-%m-%d_%H%M", time.localtime(ms / 1000.0))
    tail = "".join(random.choice(_ID_ALPHABET) for _ in range(4))
    return f"{stamp}_{tail}"


def git_info(path: str | os.PathLike) -> dict:
    """读一个仓库的 commit 与工作区洁净度。

    **为什么要记**（§5.4）：三个上游都在活跃迭代，不记 commit 就等于复现不了；
    工作区脏（有未提交改动）时更要记 —— 那意味着产物的前提不是任何已发布的版本。
    """
    p = Path(path)
    out: Dict[str, Any] = {"path": str(p)}
    repo = p
    for _ in range(4):                          # 向上找 .git
        if (repo / ".git").exists():
            break
        if repo.parent == repo:
            return out
        repo = repo.parent
    else:
        return out
    try:
        out["git"] = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5).stdout.strip() or None
        dirty = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True, text=True, timeout=8).stdout.strip()
        out["dirty"] = bool(dirty)
        out["dirty_files"] = len(dirty.splitlines())
    except (OSError, subprocess.SubprocessError):
        pass
    return out


@dataclass
class Session:
    """一个统一会话的目录与状态。"""

    session_id: str
    root: Path
    started_ms: int = field(default_factory=now_ms)
    ended_ms: Optional[int] = None
    #: 三路各自的 phase：closed | opening | open | closing | failed
    phase: Dict[str, str] = field(default_factory=lambda: {
        "mouse": "closed", "camera": "closed", "locate": "closed"})
    detail: Dict[str, str] = field(default_factory=dict)
    counts: Dict[str, int] = field(default_factory=lambda: {
        "camera_frames_verified": 0, "locate_valid": 0, "locate_invalid": 0,
        "timeline_rows": 0})
    mouse_path: Optional[str] = None
    camera_ref: Optional[dict] = None
    notes: List[str] = field(default_factory=list)
    #: 本次会话冻结的 arena / locator / clock 快照（写进 meta.json）
    snapshot: Dict[str, Any] = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return self.root / self.session_id

    @property
    def locate_dir(self) -> Path:
        return self.path / "locate"

    @property
    def thumbs_dir(self) -> Path:
        return self.path / "thumbs"

    @property
    def frames_dir(self) -> Path:
        return self.path / "frames"

    @property
    def timeline_path(self) -> Path:
        return self.path / "timeline.csv"

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "path": str(self.path),
            "started_ms": self.started_ms,
            "started_at": now_iso(self.started_ms),
            "ended_ms": self.ended_ms,
            "ended_at": now_iso(self.ended_ms) if self.ended_ms else None,
            "duration_s": round(((self.ended_ms or now_ms()) - self.started_ms) / 1000.0, 3),
            "phase": dict(self.phase),
            "detail": dict(self.detail),
            "counts": dict(self.counts),
            "mouse_path": self.mouse_path,
            "camera_ref": self.camera_ref,
            "notes": list(self.notes),
        }


class Store:
    """会话目录管理 + timeline.csv 追加。线程安全。"""

    def __init__(self, root: str | os.PathLike) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._current: Optional[Session] = None
        self._tl_f = None
        self._tl_w = None

    # ------------------------------------------------------------ 当前会话
    @property
    def current(self) -> Optional[Session]:
        return self._current

    @property
    def is_open(self) -> bool:
        return self._current is not None

    # ------------------------------------------------------------ 开
    def open(self, session_id: Optional[str] = None, notes: Sequence[str] = (),
             snapshot: Optional[Dict[str, Any]] = None) -> Session:
        with self._lock:
            if self._current is not None:
                return self._current
            sid = session_id or new_session_id()
            d = self.root / sid
            if d.exists():
                raise FileExistsError(
                    f"会话目录已存在：{d}。换一个 session_id —— 覆盖已有会话是不可逆的。")
            d.mkdir(parents=True)
            (d / "locate").mkdir()
            (d / "thumbs").mkdir()
            (d / "frames").mkdir()
            s = Session(session_id=sid, root=self.root, notes=list(notes),
                        snapshot=dict(snapshot or {}))
            s.phase["mouse"] = "opening"
            # timeline 表头 —— 先建好，再让三路上线（顺序在 §5.3 有讲究）
            self._tl_f = open(s.timeline_path, "w", newline="", encoding="utf-8-sig")
            self._tl_w = csv.writer(self._tl_f)
            self._tl_w.writerow(TIMELINE_HEADER)
            self._tl_f.flush()
            self._current = s
            return s

    # ------------------------------------------------------------ timeline
    def mark(self, source: str, kind: str, ref: str = "", note: str = "",
             t_pc_ms: Optional[int] = None, t_src: str = "", dev_ms: Optional[int] = None) -> None:
        """往 timeline.csv 追加一行。**所有跨三路的事件都必须经过这里。**

        后两列是**这次跟新加的关键**，都在回答同一个问题：**这一行的 t_pc_ms 是哪一种时间？**

          · `t_src`  —— `send`：帧真正**发出/拍摄**的时刻（固件时基或手机 epoch，
                        经各自偏移换算过来）。这是**准确**的那种。
                       `arrival`：只能拿到"主机收到"的时刻（链路抖动被算进去了）。
                       `local`：本层自己产生的事件（开会话、点按钮），没有别的来源。
          · `dev_ms` —— 发车以来的毫秒数（设备时基）。有它才能和上游的
                        `stream_*.csv`、`results.jsonl` 直接对齐。

        ⚠️ 为什么不把 `arrival` 悄悄当成真相：蓝牙/WiFi 的延迟不是常数，
        用到达时刻当发生时刻，时间轴就被链路抖动污染了 —— 而"检测运动特性"
        要看的恰恰是毫秒级的时间关系。分不清"哪种时间"的数据比没有时间更危险。
        """
        with self._lock:
            s = self._current
            if s is None or self._tl_w is None:
                return
            t = now_ms() if t_pc_ms is None else int(t_pc_ms)
            self._tl_w.writerow([t, now_iso(t), source, kind, ref, note,
                                 t_src or "local",
                                 "" if dev_ms is None else int(dev_ms)])
            s.counts["timeline_rows"] += 1
            self._tl_f.flush()

    def count_rows(self, source: str, kind: str) -> int:
        """数 timeline 里某类行的条数（判据 I6 的行数守恒靠它）。"""
        with self._lock:
            s = self._current
            if s is None:
                return 0
            n = 0
            with open(s.timeline_path, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    if row.get("source") == source and row.get("kind") == kind:
                        n += 1
            return n

    # ------------------------------------------------------------ 三路快照
    def set_camera_ref(self, ref: dict) -> None:
        with self._lock:
            s = self._current
            if s is None:
                return
            s.camera_ref = ref
            (s.path / "camera.json").write_text(
                json.dumps(ref, ensure_ascii=False, indent=2), encoding="utf-8")

    def note(self, text: str) -> None:
        with self._lock:
            s = self._current
            if s is None:
                return
            s.notes.append(text)
            with open(s.path / "notes.md", "a", encoding="utf-8") as f:
                f.write(f"- {now_iso()} {text}\n")

    # ------------------------------------------------------------ 关
    def close(self, extra_meta: Optional[dict] = None) -> Optional[Session]:
        with self._lock:
            s = self._current
            if s is None:
                return None
            s.ended_ms = now_ms()
            for k in ("mouse", "camera", "locate"):
                if s.phase.get(k) == "open":
                    s.phase[k] = "closed"
            if self._tl_f is not None:
                try:
                    self._tl_f.flush()
                    self._tl_f.close()
                except OSError:
                    pass
                self._tl_f = None
                self._tl_w = None
            self._write_meta(s, extra_meta)
            self._current = None
            return s

    def rollback(self) -> Optional[Session]:
        """开失败时的回滚：**删掉整个会话目录**（判据 I5：不许留半个会话）。"""
        with self._lock:
            s = self._current
            if s is None:
                return None
            if self._tl_f is not None:
                try:
                    self._tl_f.close()
                except OSError:
                    pass
                self._tl_f = None
                self._tl_w = None
            path = s.path
            self._current = None
            _rmtree_quiet(path)
            return s

    def abort_if_half_open(self) -> Optional[str]:
        """检查当前会话是不是"半个"：缺哪一路就说哪一路。返回问题描述或 None。"""
        s = self._current
        if s is None:
            return None
        missing = [k for k, v in s.phase.items() if v != "open"]
        if missing:
            return "会话不完整，缺：" + "、".join(missing)
        return None

    # ------------------------------------------------------------ meta.json
    def _write_meta(self, s: Session, extra: Optional[dict]) -> None:
        meta = {
            "session_id": s.session_id,
            "started_at": now_iso(s.started_ms),
            "ended_at": now_iso(s.ended_ms) if s.ended_ms else None,
            "host_version": __version__,
            **s.snapshot,
            "counts": dict(s.counts),
        }
        if s.camera_ref:
            meta.setdefault("camera", {})
            meta["camera"] = {
                "session_id": s.camera_ref.get("session_id"),
                "data_dir": s.camera_ref.get("data_dir"),
                "store": s.camera_ref.get("store"),
            }
        if s.mouse_path:
            meta["mouse_path"] = s.mouse_path
        if extra:
            meta.update(extra)
        (s.path / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # ------------------------------------------------------------ 列举 / 读
    def list_sessions(self) -> List[dict]:
        out: List[dict] = []
        for d in sorted(self.root.iterdir(), reverse=True):
            if not d.is_dir() or d.name.startswith("."):
                continue
            item: Dict[str, Any] = {"session_id": d.name, "path": str(d)}
            mp = d / "meta.json"
            if mp.is_file():
                try:
                    item["meta"] = json.loads(mp.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    item["meta_error"] = "meta.json 读不出来"
            else:
                item["meta_error"] = "没有 meta.json（可能是中断的会话）"
            tl = d / "timeline.csv"
            item["has_timeline"] = tl.is_file()
            if tl.is_file():
                try:
                    with open(tl, encoding="utf-8-sig") as f:
                        item["timeline_rows"] = max(0, sum(1 for _ in f) - 1)
                except OSError:
                    pass
            rj = d / "locate" / "results.jsonl"
            item["has_locate"] = rj.is_file()
            item["size_bytes"] = _dir_size(d)
            out.append(item)
        return out

    def read_timeline(self, session_id: Optional[str] = None,
                      since_ms: Optional[int] = None,
                      limit: int = 20000) -> List[dict]:
        sid = session_id or (self._current.session_id if self._current else None)
        if not sid:
            return []
        p = self.root / sid / "timeline.csv"
        if not p.is_file():
            return []
        rows: List[dict] = []
        with open(p, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                try:
                    t = int(row["t_pc_ms"])
                except (KeyError, ValueError):
                    continue
                if since_ms is not None and t < since_ms:
                    continue
                rows.append(row)
                if len(rows) >= limit:
                    break
        return rows

    def export(self, session_id: str, kind: str = "zip") -> Path:
        """导出会话。`zip` 打包整个目录；`csv`/`jsonl` 只取那一份主产物。"""
        d = self.root / session_id
        if not d.is_dir():
            raise FileNotFoundError(f"没有这个会话：{session_id}")
        if kind == "zip":
            base = self.root / f"_export_{session_id}"
            return Path(shutil.make_archive(str(base), "zip", root_dir=str(d)))
        if kind == "csv":
            return d / "timeline.csv"
        if kind == "jsonl":
            return d / "locate" / "results.jsonl"
        raise ValueError(f"不支持的导出类型 {kind!r}（可选 zip / csv / jsonl）")


def _dir_size(p: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(p):
        for fn in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    return total
