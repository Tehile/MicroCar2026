"""raw.bin 回放后端 —— 无硬件时按**原始节奏**重放历史会话。

与 `_SerialBackend` / `_TcpBackend` 实现相同的三个方法(`read/write/close`),
因此 `Link` 一行不用改, 用 `Target(kind="replay", path=...)` 即可接入。

设计依据(见接口化设计 §6.4):
    raw.bin 是完整原始字节流, raw.ts 记录每段的到达时刻 —— 两者合起来
    足以在无硬件时按当时的速率与间隔重放, 让时间相关分析(实时趋势、超时、
    指标计算)仍有意义。协议迭代后也能用新解析器重跑旧数据。

时序语义:
    read() 按 raw.ts 的时间戳逐步释放字节; 尚未到点返回空串(链路层会继续轮询);
    无 raw.ts(旧会话)则退化为"尽快灌"。
"""

from __future__ import annotations

import os
import time
from typing import List, Optional, Tuple


def resolve_raw_path(path: str) -> Tuple[str, Optional[str]]:
    """把会话目录或 raw.bin 文件归一到 (raw.bin, raw.ts) 两个路径。"""
    if os.path.isdir(path):
        return os.path.join(path, "raw.bin"), os.path.join(path, "raw.ts")
    ts = path + ".ts"
    return path, ts if os.path.isfile(ts) else None


def load_ts(ts_path: Optional[str]) -> List[Tuple[int, int]]:
    """解析 raw.ts 为 [(t_ms, nbytes), ...]。文件缺失/损坏时返回空表。"""
    if not ts_path or not os.path.isfile(ts_path):
        return []
    out: List[Tuple[int, int]] = []
    with open(ts_path, encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) != 2:
                continue
            try:
                out.append((int(parts[0]), int(parts[1])))
            except ValueError:
                continue
    return out


class ReplayBackend:
    """只读回放后端。write 丢弃(命令在录制时已生效), close 无操作。"""

    def __init__(self, path: str, speed: float = 1.0) -> None:
        self.speed = max(0.01, float(speed))
        raw_path, ts_path = resolve_raw_path(path)
        if not os.path.isfile(raw_path):
            raise FileNotFoundError(f"找不到 raw.bin: {raw_path}")
        with open(raw_path, "rb") as f:
            self._raw = f.read()
        self._ts = load_ts(ts_path)
        self._pos = 0
        self._t0_wall = time.time()
        self._t0_rec = self._ts[0][0] if self._ts else 0

    # ------------------------------------------------------------ 后端接口

    def read(self, n: int) -> bytes:
        if self._pos >= len(self._raw):
            return b""
        due = self._due_bytes()
        if due <= self._pos:
            # 还没到下一段的时间点; 短暂让出 CPU, 链路层会继续轮询
            time.sleep(0.002)
            return b""
        end = min(len(self._raw), due, self._pos + max(1, n))
        data = self._raw[self._pos:end]
        self._pos = end
        return data

    def write(self, data: bytes) -> None:
        # 回放是只读的: 主机此刻发的命令在原会话里已经生效过了。
        pass

    def close(self) -> None:
        pass

    # ------------------------------------------------------------ 内部

    def _due_bytes(self) -> int:
        """按回放时钟返回"已经到点"的字节数(绝对偏移)。"""
        if not self._ts:
            return len(self._raw)
        elapsed_ms = (time.time() - self._t0_wall) * self.speed * 1000.0
        due = 0
        for t_ms, nbytes in self._ts:
            if (t_ms - self._t0_rec) <= elapsed_ms:
                due += nbytes
            else:
                break
        return due

    @property
    def total(self) -> int:
        return len(self._raw)

    @property
    def position(self) -> int:
        return self._pos
