"""建图增量通道的上位机重建器(v10, PROTO 10)。

固件在 `REMOTE/link.c` 的 `LINK_Tick` 里以 1 kHz 采样 `GmcMouse` 坐标变化,
每走一格就把该格的 `GucMapBlock[x][y] & 0x0F`(四向**可通位**, 1=通) 压进一个
32 条的环, 每帧最多带 3 条(偏移 23..29)。本模块把这些条目重建成与固件
**逐字节相同**的墙图。

为什么值得单独一层:
    1. `MAP DUMP` 走的是 Flash 存档快照(`LP_SyncMap()` 全仓无调用点), 上电未
       `CAL BLOCK` 时回全 0 图, 跑完一轮也拿不到本轮结果; 增量通道是实时的。
    2. `CAL BLOCK` 从不填 `mapVisited`, 所以固件侧访问图恒全 0; 这里用"该格
       出现在任何一条 entry 里"自建访问图, 顺带解决了那个老问题。
    3. 真机对拍(`map_live_verify.py`)需要"host 重建 vs 固件整图"逐字节比,
       判据就在本模块。

逐字节一致的关键 —— `_write()` 必须**照抄**固件 `__wallCheck()` 的三条规矩:
    · 低半字节 = 四向可通位(bit0=+Y bit1=+X bit2=-Y bit3=-X, **1=通**);
      高半字节 = 同向"未探明"位, 初值 0xF0。
    · **带门控**: 只有本格低半字节仍为 0x00 才写入并清理邻居 —— 一旦写过就不
      再更新, 所以重复收到同一格是无害的(幂等), 但也别指望它刷新。
    · 本格某一面有墙(对应位为 0)时, 清**邻居**高半字节里指向本格的那一位
      (`0x10 << d`), 表示"那个方向对邻居来说已经探明了"。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..protocol.codec import MAPD_MAX_PER_FRAME

#: 方向码, 与固件 `GucMouseDir` / stateA.dir 同序
DIR_DY: Tuple[int, ...] = (1, 0, -1, 0)
DIR_DX: Tuple[int, ...] = (0, 1, 0, -1)
DIR_NAMES: Tuple[str, ...] = ("N", "E", "S", "W")

#: 墙图高半字节里"指向 d 方向"的位 —— 邻居清理用(0x10=+Y 0x20=+X 0x40=-Y 0x80=-X)
_UNKNOWN_BIT: Tuple[int, ...] = (0x10, 0x20, 0x40, 0x80)


class MapReplayer:
    """把统一帧里的 `mapd` 增量块重建成墙图。

    用法::

        rp = MapReplayer(16)
        for frame in frames:          # frame 是 decode_unified() 的结果
            rp.apply(frame)
        rp.wall[x][y]                 # 与固件 GucMapBlock 逐字节相同
        rp.walls(x, y)                # {'N': True, ...} True = **有墙**(已取反)

    `wall` 的口径刻意与固件一致(**1 = 可通**), 显示时再取反 —— 中间多转一道手
    是本项目已经踩过的反相 bug 来源。
    """

    def __init__(self, mazetype: int = 16) -> None:
        self.n = int(mazetype)
        self.reset()

    # ------------------------------------------------------------ 生命周期

    def reset(self) -> None:
        """清空成"全新未探索"状态。发车(`MAZE RUN` 回 OK)时由上层调用。"""
        n = self.n
        self.wall: List[List[int]] = [[0] * n for _ in range(n)]
        self.visited: List[List[int]] = [[0] * n for _ in range(n)]
        self.last_seq: Optional[int] = None
        # -- 计数(全部是只增不减的诊断量) --
        self.frames = 0
        self.applied = 0     # 实际写进图的格子数(不含"已写过被门控跳过"的)
        self.received = 0    # 收到的条目数(含重复/跳过的)
        self.holes = 0       # SEQ 跳号次数(≈ 丢掉的地图段数)
        self.resync_n = 0    # 固件要求重新锚定的次数
        self.lossy_n = 0     # 固件环溢出过的帧数
        self.pos_mismatch_n = 0
        self.last_pos: Optional[Tuple[int, int]] = None
        self.last_cell: Optional[Tuple[int, int]] = None

    # ------------------------------------------------------------ 应用一帧

    def apply(self, frame: Dict[str, Any]) -> int:
        """吃进一帧统一帧(解码后的 dict), 返回本帧新写入的格子数。

        不抛异常: 数据异常只记计数。它会更新 `holes / resync_n / lossy_n /
        pos_mismatch_n`, 但**不会**因为计数异常而丢弃数据 —— 真相以每条 entry
        自带的绝对坐标为准, 计数只用于告警。
        """
        seq = frame.get("seq")
        if seq is not None:
            seq = int(seq) & 0xFF
            if self.last_seq is not None and ((seq - self.last_seq) & 0xFF) != 1:
                # 帧丢了 => 那一段的格子在图上是"洞", 不断言墙, 只记一笔
                self.holes += 1
            self.last_seq = seq

        m = frame.get("mapd") or {}
        if m.get("resync"):
            self.resync_n += 1
        if m.get("lossy"):
            self.lossy_n += 1

        ents: List[Dict[str, Any]] = list(m.get("entries") or [])
        n = int(m.get("count") or 0)
        if n > MAPD_MAX_PER_FRAME:
            n = MAPD_MAX_PER_FRAME
        ents = ents[:n]

        wrote = 0
        for e in ents:
            x = int(e.get("x", 0)) & 0x0F
            y = int(e.get("y", 0)) & 0x0F
            if self._write(x, y, int(e.get("open4", 0)) & 0x0F):
                wrote += 1
            self.last_cell = (x, y)

        self.frames += 1
        self.received += len(ents)
        self.applied += wrote

        # -- 交叉校验(advisory): 本帧没装满 => 环已排空 => 最后一格应等于 pos --
        # 只用来抓"固件采样点错位"的 bug, 不参与纠错; 阈值超了由上层告警。
        if 0 < n < MAPD_MAX_PER_FRAME and self.last_cell is not None:
            pos = (int(frame.get("posX", 0)) & 0x0F, int(frame.get("posY", 0)) & 0x0F)
            if self.last_cell != pos:
                self.pos_mismatch_n += 1
        if "posX" in frame and "posY" in frame:
            self.last_pos = (int(frame["posX"]) & 0x0F, int(frame["posY"]) & 0x0F)
        return wrote

    # ------------------------------------------------------------ 写一格

    def _write(self, x: int, y: int, open4: int) -> bool:
        """照抄固件 `__wallCheck()`。返回 True 表示本次真的写了(未被门控跳过)。"""
        n = self.n
        if not (0 <= x < n and 0 <= y < n):
            return False
        self.visited[x][y] = 1
        if (self.wall[x][y] & 0x0F) != 0:
            # 固件里本格一旦写过就不会再写(低半字节非 0 即跳过) => 这里也必须跳过,
            # 否则会把邻居后来清掉的"未探明"位又重置回去, 图就不逐字节了。
            return False
        self.wall[x][y] = (open4 | (open4 << 4)) & 0xFF
        for d in range(4):
            if (open4 >> d) & 1:
                continue  # 这一面通 => 不需要清邻居
            nx = x + DIR_DX[d]
            ny = y + DIR_DY[d]
            if 0 <= nx < n and 0 <= ny < n:
                self.wall[nx][ny] &= (~_UNKNOWN_BIT[d]) & 0xFF
        return True

    # ------------------------------------------------------------ 读

    def walls(self, x: int, y: int) -> Dict[str, bool]:
        """某格四向**是否有墙**(已取反, 给人看的口径)。越界全 True。"""
        if not (0 <= x < self.n and 0 <= y < self.n):
            return {k: True for k in DIR_NAMES}
        low = self.wall[x][y] & 0x0F
        return {DIR_NAMES[d]: not bool((low >> d) & 1) for d in range(4)}

    def open4(self, x: int, y: int) -> int:
        if not (0 <= x < self.n and 0 <= y < self.n):
            return 0
        return self.wall[x][y] & 0x0F

    def wall_rows(self) -> List[List[int]]:
        """整图行主序(x 外层) —— 与 `MAP DUMP` 的 `mapWall` 同排布, 便于逐字节比。"""
        return [row[:] for row in self.wall]

    def load_wall(self, grid: List[List[int]]) -> None:
        """把整张图直接灌进来(用于对拍: 把固件 `MAP DUMP` 的结果当基线)。"""
        self.wall = [list(row) for row in grid]

    # ------------------------------------------------------------ 诊断

    def stats(self) -> Dict[str, int]:
        return {
            "frames": self.frames,
            "entries": self.received,
            "applied": self.applied,
            "cells": sum(1 for x in range(self.n) for y in range(self.n) if self.visited[x][y]),
            "holes": self.holes,
            "resync": self.resync_n,
            "lossy": self.lossy_n,
            "pos_mismatch": self.pos_mismatch_n,
        }

    def render(self) -> str:
        """两行一格的 ASCII 图: 上排是各格的"东墙/南墙 + 访问标记", 便于肉眼看。"""
        n = self.n
        out = ["    " + " ".join("%X " % (x & 0x0F) for x in range(n))]
        for y in range(n - 1, -1, -1):
            row1 = []
            for x in range(n):
                w = self.walls(x, y)
                row1.append(("+" if w["N"] else " ") + ("-|" if w["E"] else "  "))
            out.append("y=%X " % (y & 0x0F) + "".join(row1))
            row2 = []
            for x in range(n):
                w = self.walls(x, y)
                mark = "*" if self.visited[x][y] else " "
                row2.append(("|" if w["W"] else " ") + mark + " ")
            out.append("    " + "".join(row2))
        out.append("    " + "".join("+-+" if self.walls(x, 0)["S"] else "+ +" for x in range(n)))
        return "\n".join(out)
