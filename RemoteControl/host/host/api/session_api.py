"""会话记录: 目录、落盘开关、备注、标定报告、历史会话列表。

对应功能清单的 I 组。

落盘内容(每会话一个目录):
    raw.bin             原始字节流 —— 协议迭代后可用新解析器复算全部历史
    raw.ts              每批原始字节的到达时刻(供回放按原始节奏重放)
    stream_unified.csv  统一帧(逐帧一行, 列 = codec.UNIFIED_CHANNELS)
    events.log          上行事件行(STOPPED / ESTOP TRIP)
    text.log            设备文本 + 主机日志
    meta.json           起止时间、统计、链路状态
    calib/              参数备份、标定报告
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:  # pragma: no cover
    from .client import MouseClient


class SessionAPI:
    """会话记录控制。连接时默认自动开启(auto_session=True)。"""

    def __init__(self, mc: "MouseClient") -> None:
        self._mc = mc

    def open(self, name: Optional[str] = None) -> Optional[str]:
        """开启会话, 返回目录路径。已开启则直接返回当前路径。"""
        s = self._mc.open_session(name)
        return None if s is None else s.path

    def close(self, extra: Optional[dict] = None) -> Optional[str]:
        s = self._mc.session_obj
        if s is None:
            return None
        path = s.path
        self._mc.close_session(extra)
        return path

    @property
    def is_open(self) -> bool:
        """会话是否已打开(有 `Session` 对象即视为已开)。"""
        return self._mc.session_obj is not None

    @property
    def path(self) -> Optional[str]:
        """当前会话目录路径(帧/事件落盘处); 未开会话时为 `None`。"""
        s = self._mc.session_obj
        return None if s is None else s.path

    def counts(self) -> Dict[str, int]:
        s = self._mc.session_obj
        return dict(s.counts) if s is not None else {}

    def note(self, text: str) -> None:
        """写一条备注。会话未开启时先暂存, 开启后补写。"""
        s = self._mc.session_obj
        if s is None:
            self._mc._session_notes.append(text)
            return
        s.write_text(text, "##")

    def report(self, lines: List[str]) -> Optional[str]:
        """追加写入 `calib/report.md`。"""
        s = self._mc.session_obj
        if s is None:
            return None
        try:
            return s.write_calib_report(lines)
        except Exception:
            return None

    # ------------------------------------------------------------ 历史会话

    @staticmethod
    def list_sessions(root: str = "sessions") -> List[Dict[str, Any]]:
        """列出历史会话目录及其 meta(按时间倒序)。供 CLI 与回放选择。"""
        out: List[Dict[str, Any]] = []
        if not os.path.isdir(root):
            return out
        for name in sorted(os.listdir(root), reverse=True):
            d = os.path.join(root, name)
            if not os.path.isdir(d):
                continue
            item: Dict[str, Any] = {"name": name, "path": d}
            meta_path = os.path.join(d, "meta.json")
            if os.path.isfile(meta_path):
                try:
                    with open(meta_path, encoding="utf-8") as f:
                        item["meta"] = json.load(f)
                except Exception:
                    pass
            raw = os.path.join(d, "raw.bin")
            if os.path.isfile(raw):
                try:
                    item["raw_bytes"] = os.path.getsize(raw)
                except OSError:
                    pass
            out.append(item)
        return out
