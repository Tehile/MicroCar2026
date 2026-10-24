"""v10 建图增量通道 → 比赛页墙图（三态）+ 建图诊断（改版三十九）。

**为什么不再用 `MazeWallMap` 当唯一真相**：v10（PROTO 10 / 上游 `17845f3`）之后，
墙不再由主机从 `wallLeft/Front/Right + dir`（**相对朝向**）推断，而是固件在
`LINK_Tick` 里每走一格把该格 `GucMapBlock[x][y] & 0x0F`（四向**可通位**，1=通，
**绝对方向**）压进 32 条环、每帧带最多 3 条。也就是说：**固件报的是事实，
主机那套是推断**。两张图并存 = 两条真相（铁律 32）。

**`MazeWallMap` 没有退役，但降级为对账诊断**：它走的是完全独立的来路
（相对朝向 → 绝对方向的映射），正好能抓"协议映射写反"这类 bug —— 这个项目在
方向上已经错过两次（`_STEP` 的 dir0/dir2 dy、`dir0=y+1`）。两者对不上的面数
在 `face_diff()` 里数出来，界面上报数，**不合并、不互相覆盖**。

**三态是必须的，不是讲究**：固件那张图里**没有"未探明"这个信息** ——
`GucMapBlock` 初值 0（四面皆墙），只在被 `__wallCheck()` 写过的格子上才有意义。
于是"没写过的格"和"四面都是墙的格"在固件里长得**一模一样**。直接把 0 读成
"有墙"，就会把**整个没走过的区域画成实心墙** —— 看着像一张"完整的迷宫"，
实际全是编的（这是本项目最忌讳的"有内容但错位"）。所以：只有 `open4 != 0`
（`__wallCheck()` 里 `ucMap |= MOUSEWAY_B` ⇒ 写过的格低半字节恒非 0）才认，
其余一律 `UNKNOWN`。

**起点格永不入增量流**（固件那边它也是 0，因为车从静止启动时坐标没有"变化"），
所以它一定显示为未探明 —— 这不是 bug，`draw.maze` 另外用绿色空方块标起点。

**为什么容器边长写死 16**：`GucMapBlock[MAZETYPE][MAZETYPE]` 的 `MAZETYPE` 上限
就是 16，而这张图是**按需喂**的（`on_telemetry` 里，跑在上游调度线程）——
那条线程上**绝不能**为了问一次 mazetype 去发 `PING`（`mazetype()` 会，
见 `mouse._device_fresh`）。所以容器一律开到 16（只作越界界），渲染时按
`mazetype` 裁剪。老 7×7 迷宫从 (0,0) 起、是 16 网格的子块，裁剪即可。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

#: 固件 `MAZETYPE` 的上限（`GucMapBlock[MAZETYPE][MAZETYPE]`）⇒ 容器边长。
MAP_N_MAX = 16

#: 面的三态。**三个值都要能画出来**：0/1 是"已经知道"，2 是"不知道"。
#: 2 不许落到"没墙"（那样未探明区看着像开阔地），也不许落到"有墙"（实心墙）。
UNKNOWN = 2

#: 轨迹点上限。与 `MazeWallMap` 同一个数：一盘几千帧，240 个格子点足够看出走法。
TRAIL_MAX = 240


def _open_has_wall(open4: int, d: int) -> int:
    """`open4` 的第 d 位 → 面的三态值（1=有墙 / 0=可通）。d 与固件同序。"""
    return 0 if ((open4 >> d) & 1) else 1


class LiveWallMap:
    """把统一帧里的 `mapd` 增量块重建成"界面口径"的墙图 + 诊断。

    ## 为什么不是直接继承 `host.api.mapd.MapReplayer`

    上游那个类的口径是**与固件逐字节相同**（`wall[x][y]` 低半字节 open4、
    高半字节是固件第二张图），它答的是"我和固件一样吗"。界面要答的是另一个问题：
    **"这一面我到底知不知道"**。两者结论不同（未写过的格在固件里是 0=四面皆墙，
    在界面上必须是"不知道"），所以这里不复制它的位图，而是照抄它的三条写入
    规矩（低半字节门控 / 邻居清位 / 绝对坐标）后**另存一份三态面**。

    这也是 `face_diff()` 能当对账用的前提：两份数据是**独立来路**。

    ## 面的编号（与 `_wallmap_view` / 回放 / `draw.maze` 完全一致）

    * `fv[y][x]` = (x,y) 与 **(x+1,y)** 之间那一面 —— 即 (x,y) 的 **+X** 面
    * `fh[y][x]` = (x,y) 与 **(x,y+1)** 之间那一面 —— 即 (x,y) 的 **+Y** 面

    方向 d 与固件同序：0=+Y 1=+X 2=-Y 3=-X（`MOUSEWAY_F = 1 << GucMouseDir`，
    而 `dir` 的 0..3 就是 UP/RIGHT/DOWN/LEFT）。**不要**在这里引入显示翻转 ——
    显示翻转只发生在 `draw.js` 的 `Y(y)` 里（血泪：混过一次就是整图错位）。
    """

    def __init__(self, n: int = MAP_N_MAX) -> None:
        self.n = max(2, min(int(n or MAP_N_MAX), MAP_N_MAX))
        self.reset()

    # ------------------------------------------------------------ 生命周期

    def reset(self) -> None:
        """清成"全新未探索"。发车 / 重连 / 界面清空都要走这里。"""
        n = self.n
        #: 竖面：`fv[y][x]`，x ∈ 0..n-2；最后一行 x=n-1 不存在（外框）
        self.fv: List[List[int]] = [[UNKNOWN] * (n - 1) for _ in range(n)]
        #: 横面：`fh[y][x]`，y ∈ 0..n-2；最后一行用不上（外框另画）
        self.fh: List[List[int]] = [[UNKNOWN] * (n - 1) for _ in range(n)]
        #: 每格的低半字节（固件口径 open4）。0 = **没被报过**（`__wallCheck()` 写过的
        #: 格恒非 0），落盘时按 `-` 写，**不许当成"四面皆墙"**。
        self.open4: List[List[int]] = [[0] * n for _ in range(n)]
        #: 每格是否被增量流报过（= 车到过）。**与"面已知"不是一回事**。
        self.visited: List[List[int]] = [[0] * n for _ in range(n)]
        #: 走过的格子序列（格中心口径在 view() 里再 +0.5）
        self.trail: List[List[int]] = []
        # -- 写入/冲突计数 --
        self.face_writes = 0     # 面写入次数（含覆盖）
        self.face_flip = 0       # 同一个面被后一次读数**改写**的次数
        # -- 帧序号（`_seq` 与事件行共用一个 1 字节计数器）--
        self.last_seq: Optional[int] = None
        self.seq_gaps = 0        # 帧序号不连续次数（≥ 真正丢的帧：事件行也占号）
        # -- 帧计数 --
        self.frames = 0
        self.frames_mapd = 0     # 带 `mapd` 子结构的帧数（判"这台固件到底报不报建图"）
        self.entries = 0         # 收到的条目总数
        self.applied = 0         # 真正写进图的格子数（被门控跳过的不算）
        self.lossy_n = 0         # 固件环溢出的帧数
        self.resync_n = 0        # 固件要求重新锚定的次数
        self.pos_mismatch_n = 0  # 环排空时"最后一格 ≠ 当前坐标"的次数
        self.last_cell: Optional[List[int]] = None
        # -- 覆盖窗口（按单调时钟，只用来回答"这张图跨了多久"）--
        self.t_first: Optional[float] = None
        self.t_last: Optional[float] = None

    # ------------------------------------------------------------ 吃一帧

    def feed(self, frame: Dict[str, Any]) -> int:
        """吃一帧解码后的统一帧，返回本帧新写入的格子数。**不抛异常、不发命令。**

        ⚠️ 帧号键叫 **`_seq`**（上游 `TelemetryStore` 加的），而上游
        `MapReplayer.apply()` 读的是 **`seq`** ⇒ 直接透传会让它以为"从来没序号"，
        `holes` 恒 0 = "没丢过"的**假绿**（上游自己的 `status_panel` 也中过这一枪）。
        所以这一层负责把 `_seq` 桥成 `seq`，调用方不用记这件事。
        """
        if not frame:
            return 0
        if frame.get("seq") is None and frame.get("_seq") is not None:
            frame = dict(frame, seq=frame["_seq"])
        self.frames += 1
        # 覆盖窗口用**单调时钟**（墙钟会被对时拨回去，拨一次就出现"负的覆盖时长"）。
        mono = time.monotonic()
        if self.t_first is None:
            self.t_first = mono
        self.t_last = mono

        seq = frame.get("seq")
        if seq is not None:
            seq = int(seq) & 0xFF
            if self.last_seq is not None and ((seq - self.last_seq) & 0xFF) != 1:
                self.seq_gaps += 1
            self.last_seq = seq

        wrote = 0
        m = frame.get("mapd")
        if isinstance(m, dict):
            self.frames_mapd += 1
            if m.get("lossy"):
                self.lossy_n += 1
            if m.get("resync"):
                self.resync_n += 1
            ents = list(m.get("entries") or [])[:3]
            last_cell = None
            for e in ents:
                self.entries += 1
                ex = int(e.get("x", 0)) & 0x0F
                ey = int(e.get("y", 0)) & 0x0F
                last_cell = [ex, ey]
                if self._write(ex, ey, int(e.get("open4", 0)) & 0x0F):
                    wrote += 1
            self.applied += wrote
            # ⚠️ `last_cell` 取**这一帧最后一条条目**，不能取 `_write()` 里写成功的那个：
            #    `_write()` 对"已经写过的格"直接早退（固件那条门控），于是车**重走**
            #    老格子时 `last_cell` 会停在上一个**新**格子上 —— 距离越拉越远，
            #    判据就变成常亮（实测 762/1374，55%，现场只会以为固件采样坏了）。
            if last_cell is not None:
                self.last_cell = last_cell
            # 环已排空（本帧没装满）⇒ 最后一条**应当**追上当前坐标。
            # ⚠️ 判"差 1 格"是**正常的**，不能算错位（改版三十九实测）：
            #    车刚跨过去的那一格，固件要等下一个 tick 才把条目压进环，而帧里的
            #    `posX/posY` 已经是新格 ⇒ 每次都差 1。按"不相等"报，一次跑下来能报
            #    出 118/339（35%），警告行常亮、现场以为固件采样坏了。
            #    真正的错位是"差 ≥2 格"（跳过格子没报）。
            if 1 <= len(ents) < 3 and self.last_cell is not None:
                cx = int(frame.get("posX", -1)) & 0x0F
                cy = int(frame.get("posY", -1)) & 0x0F
                if max(abs(self.last_cell[0] - cx), abs(self.last_cell[1] - cy)) > 1:
                    self.pos_mismatch_n += 1

        # 轨迹：位置来自**帧**（mapd 里没有坐标序列）。相邻重复不记。
        try:
            cx = int(frame.get("posX")) & 0x0F
            cy = int(frame.get("posY")) & 0x0F
        except (TypeError, ValueError):
            cx = cy = None
        if cx is not None and (not self.trail or self.trail[-1] != [cx, cy]):
            if len(self.trail) < TRAIL_MAX:
                self.trail.append([cx, cy])
        return wrote

    # ------------------------------------------------------------ 写一格

    def _write(self, x: int, y: int, open4: int) -> bool:
        """照抄固件 `__wallCheck()` 的门控，返回 True 表示这次真的写了。

        固件那条规矩（低半字节非 0 即跳过）必须抄：不抄的话，重复收到同一格会
        把邻居后来清掉的位重置回去，图上就会出现"已经确认要拆、又自己长回来"的墙。
        """
        n = self.n
        if not (0 <= x < n and 0 <= y < n):
            return False
        self.visited[x][y] = 1
        if self.open4[x][y] != 0:
            return False
        if open4 == 0:
            # 写过的格 open4 恒非 0（固件先 `|= MOUSEWAY_B`）。真收到 0 说明这一条
            # 是坏的 ⇒ 只记"到过"，不写任何面（写下去就是四面皆墙，纯编）。
            return False
        self.open4[x][y] = open4 & 0x0F
        for d, (dx, dy) in ((0, (0, 1)), (1, (1, 0)), (2, (0, -1)), (3, (-1, 0))):
            has = _open_has_wall(open4, d)
            nx, ny = x + dx, y + dy
            if not (0 <= nx < n and 0 <= ny < n):
                continue                      # 外框：`draw.maze` 单独画
            if d == 0:
                self._face(self.fh, y, x, has)
            elif d == 2:
                self._face(self.fh, y - 1, x, has)
            elif d == 1:
                self._face(self.fv, y, x, has)
            else:
                self._face(self.fv, y, x - 1, has)
        return True

    def _set_low(self, x: int, y: int, open4: int) -> None:
        self.open4[x][y] = open4 & 0x0F

    def _low(self, x: int, y: int) -> int:
        return self.open4[x][y] if (0 <= x < self.n and 0 <= y < self.n) else 0

    def _face(self, grid: List[List[int]], i: int, j: int, has: int) -> None:
        """写一个面。**最新覆盖**（同一条边被反复观测时，最后一次读数说了算）。"""
        if not (0 <= i < len(grid) and 0 <= j < len(grid[i])):
            return
        old = grid[i][j]
        if old != UNKNOWN and old != has:
            self.face_flip += 1
        grid[i][j] = has
        self.face_writes += 1

    # ------------------------------------------------------------ 读

    def view(self, n: Optional[int] = None, note: str = "") -> Dict[str, Any]:
        """界面直接能画的形状：`{N, v, h, visited, trail, cells, cells_total}`。

        `n` 传**设备自述的迷宫边长**（裁剪用）；不传就用容器边长。
        `v/h` 的元素是**三态**：0 可通 / 1 有墙 / 2 未探明。
        """
        nn = int(n or self.n)
        nn = max(2, min(nn, self.n))
        # 裁剪到 nn：两个面数组都是 n×(n-1)，而 nn ≤ n ⇒ 下标天然有效，不需再判界。
        v = [[self.fv[y][x] for x in range(nn - 1)] for y in range(nn)]
        h = [[self.fh[y][x] for x in range(nn - 1)] for y in range(nn)]
        visited = {}
        for y in range(nn):
            for x in range(nn):
                if self.visited[x][y]:
                    visited[f"{x},{y}"] = 1
        out: Dict[str, Any] = {
            "N": nn,
            "v": v, "h": h, "visited": visited,
            "trail": [[c[0] + 0.5, c[1] + 0.5] for c in self.trail if len(c) >= 2],
            "cells": len(visited),
            "cells_total": nn * nn,
            "src": "mapd" if self.frames_mapd else "none",
        }
        if note:
            out["note"] = note
        return out

    def covered_s(self) -> float:
        """这张图跨了多久（秒，单调时钟）。**判不了给 0.0 而不是编一个数**。"""
        if self.t_first is None or self.t_last is None:
            return 0.0
        return round(max(0.0, self.t_last - self.t_first), 1)

    def stats(self) -> Dict[str, Any]:
        """建图诊断六项 + 冲突计数。**判不了就给 None，不给 0**（三态纪律）。"""
        return {
            "frames": self.frames,
            "frames_mapd": self.frames_mapd,
            "entries": self.entries,
            "applied": self.applied,
            # 已重建格数（"车上报过的格"），不是"图上画了几格"
            "cells": sum(1 for x in range(self.n) for y in range(self.n)
                         if self.visited[x][y]),
            # 序号不连续：事件行与帧共用 1 字节计数器 ⇒ 这个数 ≥ 真正丢的帧
            "seq_gaps": self.seq_gaps,
            "resync": self.resync_n,
            "lossy": self.lossy_n,
            "pos_mismatch": self.pos_mismatch_n,
            "face_flip": self.face_flip,
        }

    def face_diff(self, wm: Any, n: int) -> Dict[str, Any]:
        """与 `MazeWallMap` 逐面比对 —— **对账诊断**，不是纠错。

        两份数据来路独立：这份是固件的绝对坐标增量，那份是主机用
        `wallLeft/Front/Right + dir` 反推的。只比**两边都知道**的面；
        一边不知道就不算（"判不了"不是"不一致"）。
        """
        out: Dict[str, Any] = {"common": 0, "mismatch": 0, "samples": []}
        if wm is None:
            return out
        wn = int(getattr(wm, "n", 0) or 0)
        # ⚠️ 两个面数组的形状**不一样**，界必须分开写：
        #    `fv[y][x]` 是 **n 行 × (n-1) 列**（竖面在每行有 n-1 条）
        #    `fh[y][x]` 是 **(n-1) 行 × (n-1) 列**（横面在每列也有 n-1 条，但只有 n-1 行）
        # 只守 `y < self.n - 1` 而不管 x 的话，`fh[y][n-1]` 一定会越界 ——
        # 而这条路径**平时根本跑不到**（没有 `mapd` 帧时 `diff` 恒 `None`），
        # 于是它一上线就 500：界面上是"整块图没了"，看起来像设备没数据（实测踩到）。
        for y in range(min(n, wn)):
            for x in range(min(n, wn)):
                # 竖面（+X）：fv[y][x] ↔ MazeWallMap.wall[x][y][1]
                if x + 1 < n and y < self.n and x < self.n - 1:
                    self._cmp(out, "v", x, y, self.fv[y][x],
                              1 if wm.wall[x][y][1] else 0, bool(wm.known[x][y][1]))
                # 横面（+Y）：fh[y][x] ↔ MazeWallMap.wall[x][y][0]
                if y + 1 < n and y < self.n - 1 and x < self.n - 1:
                    self._cmp(out, "h", x, y, self.fh[y][x],
                              1 if wm.wall[x][y][0] else 0, bool(wm.known[x][y][0]))
        return out

    @staticmethod
    def _cmp(out: Dict[str, Any], kind: str, x: int, y: int,
             mine: int, theirs: int, known: bool) -> None:
        if mine == UNKNOWN or not known:
            return
        out["common"] += 1
        if int(mine) != int(theirs):
            out["mismatch"] += 1
            if len(out["samples"]) < 8:
                out["samples"].append({"x": x, "y": y, "dir": kind,
                                       "mapd": int(mine), "host": int(theirs)})


class MapdRows:
    """`mapd` 重建结果的**落盘形态**：n 行十六进制 + 诊断。

    为什么要落这个而不是只落 `MazeWallMap` 的 `wall_rows`：回放要的是
    **与比赛页同一张图**（同一来路、同一口径）。`MazeWallMap` 那份继续落，
    但只用于对账；界面显示走这份。
    """

    #: 空（未探明）格在行里的字符。**不能是 0** —— 0 在固件里表示"四面皆墙"。
    UNKNOWN_CH = "-"

    @staticmethod
    def dump(lm: LiveWallMap, n: int) -> Dict[str, Any]:
        nn = max(2, min(int(n or lm.n), lm.n))
        rows = []
        for y in range(nn):
            row = []
            for x in range(nn):
                if x >= lm.n or y >= lm.n or not lm.visited[x][y]:
                    row.append(MapdRows.UNKNOWN_CH)     # 没报过 ⇒ 不编
                else:
                    row.append(format(lm._low(x, y) & 0x0F, "x"))
            rows.append("".join(row))
        return {"n": nn, "rows": rows, "stats": lm.stats()}

    @staticmethod
    def view(rows: List[str], n: int, trail: Optional[List] = None) -> Dict[str, Any]:
        """从落盘行还原三态图。`rows` 里的 `-` = 该格没被报过。"""
        nn = max(2, min(int(n or len(rows) or 2), 16))
        fv = [[UNKNOWN] * (nn - 1) for _ in range(nn)]
        fh = [[UNKNOWN] * (nn - 1) for _ in range(nn)]
        visited = {}

        def low(x: int, y: int) -> Optional[int]:
            if not (0 <= y < len(rows)) or not (0 <= x < len(rows[y])):
                return None
            ch = rows[y][x]
            try:
                return int(ch, 16)
            except (TypeError, ValueError):
                return None

        def put(grid: List[List[int]], i: int, j: int, has: int) -> None:
            if 0 <= i < len(grid) and 0 <= j < len(grid[i]):
                grid[i][j] = has

        for y in range(nn):
            for x in range(nn):
                o4 = low(x, y)
                if o4 is None:
                    # `-` = 这一格**从来没被报过**（连"到过"都没有）。
                    continue
                # 报过就算到过 —— 包括 `open4=0` 那种"到过但条目是坏的"。
                # ⚠️ 这两种在落盘行里是**不同字符**（`0` vs `-`），读回来必须分开处理：
                #    一起当成"没报过"会让 `visited` 少一格（回放图上那格没有车辙，
                #    而实时看的时候有）—— 两份图不一致、且看不出来是读错了。
                visited[f"{x},{y}"] = 1
                if o4 == 0:
                    # 到过、但条目是坏的 ⇒ 不写任何面（与 `_write` 同一道门控）。
                    continue
                for d, (dx, dy) in ((0, (0, 1)), (1, (1, 0)), (2, (0, -1)), (3, (-1, 0))):
                    nx, ny = x + dx, y + dy
                    if not (0 <= nx < nn and 0 <= ny < nn):
                        continue
                    has = _open_has_wall(o4, d)
                    if d == 0:
                        put(fh, y, x, has)
                    elif d == 2:
                        put(fh, y - 1, x, has)
                    elif d == 1:
                        put(fv, y, x, has)
                    else:
                        put(fv, y, x - 1, has)
        return {"N": nn, "v": fv, "h": fh, "visited": visited,
                "trail": [list(p) for p in (trail or [])],
                "cells": len(visited), "cells_total": nn * nn, "src": "mapd"}
