"""会话落盘: 每会话一个目录, CSV(人可读) + raw.bin(原始字节留档) + meta.json。

保留 raw.bin 是刻意的: 协议一旦迭代(加字段/改缩放), 只要有原始字节流,
就能用新解析器重跑全部历史数据, 不会丢历史。

v2.0(2026-10-05): 单流 —— 原先 fast/mid/slow 三份 CSV 合并为一份
`stream_unified.csv`(列取自 `protocol.codec.UNIFIED_CHANNELS`); 1kHz dump CSV
随 SCOPE/dump 机制一并删除。事件行另存 `events.log`。
"""

from __future__ import annotations

import csv
import json
import os
import threading
import time
from typing import Dict, List, Optional

from ..model.ring import CHANNELS


class Session:
    def __init__(self, root: str = "sessions", name: Optional[str] = None) -> None:
        self.root = root
        stamp = time.strftime("%Y-%m-%d_%H%M")
        self.name = name or stamp
        self.path = os.path.join(root, self.name)
        os.makedirs(self.path, exist_ok=True)
        os.makedirs(os.path.join(self.path, "calib"), exist_ok=True)

        self.lock = threading.Lock()
        self.enabled = True
        self.counts: Dict[str, int] = {"unified": 0, "text": 0, "event": 0}

        self._raw = open(os.path.join(self.path, "raw.bin"), "ab", buffering=1 << 16)
        # raw.ts: 每行 `<t_ms> <nbytes>`, 记录原始字节的到达时刻。
        # 供离线回放(replay.py)按**原始节奏**重放 —— 只有 raw.bin 的话只能尽快灌,
        # 那样时间相关的分析(看实时趋势、超时)就没有意义了。
        self._raw_ts = open(os.path.join(self.path, "raw.ts"), "a", encoding="utf-8", buffering=1)
        self._text = open(os.path.join(self.path, "text.log"), "a", encoding="utf-8", buffering=1)
        self._events = open(os.path.join(self.path, "events.log"), "a", encoding="utf-8", buffering=1)

        self._uni_f = open(os.path.join(self.path, "stream_unified.csv"), "w", newline="", encoding="utf-8")
        self._uni_w = csv.writer(self._uni_f)

        # t_host_ms 便于与外部记录对齐; 时间戳精度到毫秒
        # dev_ms  = "发车(ARM)以来毫秒", 由设备时基换算 —— 它**不受链路抖动影响**,
        #           是做运动特性分析时真正该用的时间轴(见 docs/数据收集与时间同步.md)。
        #           未发车时为空字符串。
        self._uni_w.writerow(["t_host_ms", "dev_ms", "seq"] + CHANNELS)

        self.meta: Dict[str, object] = {
            "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            "host_version": "2.0.0",
        }

    # ------------------------------------------------------------ 写入

    @staticmethod
    def _ms() -> int:
        return int(time.time() * 1000)

    def write_raw(self, data: bytes) -> None:
        if not self.enabled or not data:
            return
        with self.lock:
            self._raw.write(data)
            self._raw_ts.write(f"{self._ms()} {len(data)}\n")

    def write_text(self, line: str, direction: str = "<-") -> None:
        if not self.enabled:
            return
        with self.lock:
            self._text.write(f"{self._ms()} {direction} {line}\n")
            self.counts["text"] += 1

    @staticmethod
    def _dev(rec: dict):
        """落盘的 dev_ms 列: 未发车时留空, 有值就写整数。"""
        v = rec.get("_dev_ms")
        return "" if v is None else int(v)

    def write_unified(self, rec: dict) -> None:
        """统一遥测帧: 一帧一行。列顺序 = codec.UNIFIED_CHANNELS。"""
        if not self.enabled:
            return
        with self.lock:
            self._uni_w.writerow(
                [self._ms(), self._dev(rec), rec.get("_seq", "")]
                + [rec.get(c, 0) for c in CHANNELS]
            )
            self.counts["unified"] += 1

    def write_event(self, rec: dict) -> None:
        """上行事件行(`STOPPED`/`ESTOP TRIP`): 单独一份可读日志。"""
        if not self.enabled:
            return
        with self.lock:
            self._events.write(f"{self._ms()} {rec.get('raw', '')}\n")
            self.counts["event"] += 1

    def write_calib_report(self, lines: List[str]) -> str:
        p = os.path.join(self.path, "calib", "report.md")
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return p

    def backup_params(self, rows: List[List[str]]) -> str:
        """写入前把旧参数导出作备份(SAVE 之前自动调用)。"""
        p = os.path.join(self.path, "calib", f"params_before_{self._ms()}.csv")
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["name", "q1616", "value"])
            w.writerows(rows)
        return p

    # ------------------------------------------------------------ 收尾

    def close(self, extra_meta: Optional[dict] = None) -> None:
        with self.lock:
            self.enabled = False
            if extra_meta:
                self.meta.update(extra_meta)
            self.meta["counts"] = dict(self.counts)
            self.meta["ended"] = time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                with open(os.path.join(self.path, "meta.json"), "w", encoding="utf-8") as f:
                    json.dump(self.meta, f, ensure_ascii=False, indent=2)
            except OSError:
                pass
            for h in (
                self._raw,
                self._raw_ts,
                self._text,
                self._events,
                self._uni_f,
            ):
                try:
                    if h:
                        h.close()
                except OSError:
                    pass
