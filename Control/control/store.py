"""统一会话目录 + 时间轴 + meta.json（设计方案 §5）。

目录布局见 §5.1：`mouse/` 是鼠侧原生产物（复用上游格式，原样），
`timeline.csv` 是"整合"这件事唯一的可验证产物。

⚠️ **改版二十四：相机与场地定位整块下线。** 位置数据不再来自实时帧流，而是
"一段视频 → Auto Locate 会话解析 → 轨迹"，那条链的产物在 `video.out_root` 下、
**按视频会话单独成目录**，不并进这里的录制会话（两者的时间基不同源：
录制会话按 `t_pc_ms`，视频按帧号/`t_video_s`）。于是本类只剩两件事：
**开/停鼠会话** 与 **写 timeline.csv**。
历史会话目录里可能仍留着 `locate/`、`camera.json`、`thumbs/`、`frames/` ——
**只读容忍、不删**（那是当时那场记录的证据，本层没有资格替人清掉）。

原子性（§5.3）：开失败必须回滚干净。
    开：先建目录 → 再开鼠会话 → 任一步失败**回滚已成功的步骤**
        （判据 I5：不许留下半个会话目录）。
    停：收鼠会话 → 写 meta.json（时间轴最后落盘）。
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
    """读一个上游目录的 commit 与**它自己那块**的洁净度。

    **为什么要记**（§5.4）：上游都在活跃迭代，不记 commit 就等于复现不了；
    工作区脏（有未提交改动）时更要记 —— 那意味着产物的前提不是任何已发布的版本。

    ⚠️ **必须按 pathspec 限定到该目录**：本工程的目录布局是"单仓库 + 若干上游子目录"
    （`RemoteControl/`、`Auto Locate/` 都在同一个 git 仓库里，没有各自的 `.git`）。
    不限定的话，`git status` 报的是**整个仓库**的改动 —— 包括 Control 自己正在改的文件。
    那样这条判据永远红、且红得毫无意义（"上游脏了"其实是"我刚改了 Control"）。
    """
    p = Path(path).resolve()
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
        rel = str(p.relative_to(repo)) or "."
    except ValueError:
        rel = "."
    try:
        out["git"] = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5).stdout.strip() or None
        dirty = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--", rel],
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
    #: 各路 phase：closed | opening | open | closing | failed
    #: 改版二十四只剩鼠一路（相机/定位下线）。保留 dict 而不是改成单值：
    #: `session.phase` 事件与历史 meta.json 里都是这个形状，前端照旧按 key 遍历。
    phase: Dict[str, str] = field(default_factory=lambda: {"mouse": "closed"})
    detail: Dict[str, str] = field(default_factory=dict)
    counts: Dict[str, int] = field(default_factory=lambda: {"timeline_rows": 0})
    mouse_path: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    #: 本次会话冻结的 arena / clock 快照（写进 meta.json）
    snapshot: Dict[str, Any] = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return self.root / self.session_id

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
            s = Session(session_id=sid, root=self.root, notes=list(notes),
                        snapshot=dict(snapshot or {}))
            s.phase["mouse"] = "opening"
            # timeline 表头 —— 先建好，再让鼠那路上线（顺序在 §5.3 有讲究：
            # 表头落到盘上之后才可能有事件，否则第一行会被当成表头吃掉）
            self._tl_f = open(s.timeline_path, "w", newline="", encoding="utf-8-sig")
            self._tl_w = csv.writer(self._tl_f)
            self._tl_w.writerow(TIMELINE_HEADER)
            self._tl_f.flush()
            self._current = s
            return s

    # ------------------------------------------------------------ timeline
    def mark(self, source: str, kind: str, ref: str = "", note: str = "",
             t_pc_ms: Optional[int] = None, t_src: str = "", dev_ms: Optional[int] = None) -> None:
        """往 timeline.csv 追加一行。**所有跨来源的事件都必须经过这里。**

        后两列是**跟新加的关键**，都在回答同一个问题：**这一行的 t_pc_ms 是哪一种时间？**

          · `t_src`  —— `send`：帧真正**发出**的时刻（固件时基，经各自偏移换算过来）。
                        这是**准确**的那种。
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

    # ------------------------------------------------------------ 备注
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
            for k in ("mouse",):
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
            # ⚠️ 改版二十四：原来这里还报 `has_locate`（`locate/results.jsonl` 在不在）。
            # 定位那一路随相机下线，读数没有去处 ⇒ 整条删掉，不用 `false` 占位
            # （占位会被读成"这次没定位"，而不是"这个功能已经不在了"）。
            # 历史目录里的 `locate/` **不动**，`size_bytes` 照样把它算进体积。
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
        """导出会话。`zip` 打包整个目录；`csv` 只取 `timeline.csv`。

        **改版二十四删掉了 `jsonl`**（那是 `locate/results.jsonl` 的快捷方式，
        定位下线后它没有来路）。历史会话里那份文件仍在目录内，用 `zip` 能拿到 ——
        所以删掉的只是一个会指向空文件的按钮，不是数据。
        """
        d = self.root / session_id
        if not d.is_dir():
            raise FileNotFoundError(f"没有这个会话：{session_id}")
        if kind == "zip":
            base = self.root / f"_export_{session_id}"
            return Path(shutil.make_archive(str(base), "zip", root_dir=str(d)))
        if kind == "csv":
            return d / "timeline.csv"
        raise ValueError(f"不支持的导出类型 {kind!r}（可选 zip / csv）")


def _dir_size(p: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(p):
        for fn in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    return total
