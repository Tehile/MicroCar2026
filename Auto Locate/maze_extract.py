#!/usr/bin/env python
"""迷宫结构解算（**原型，接口待确认**）—— 从红色墙线解出 8×8 迷宫的墙结构并出图。

⚠ 这是接口原型，不是最终实现。设计意图：**先把输入输出契约钉死**，算法后续再打磨。
    本文件是自包含脚本，不修改 `aruco_locator/` 任何代码（v1.x 对外接口保持冻结）。

────────────────────────────────────────────────────────────────────────────
物理前提（已由实拍确认，见 experiments/README.md 与 _probe_out/ 正射图）
────────────────────────────────────────────────────────────────────────────
场地是 8×8 电脑鼠迷宫，墙是 50mm 高的木条，**墙顶边缘涂红**。所以：

  · 红色在正射图上是沿格线的**线段**，不是整片色块 → 适合做"沿线段测覆盖率"。
  · `H_img2field` 标定在角标记平面 = **墙顶平面**（h_c = 50mm），
    因此墙顶红线**正好落在场地系的 k/8 格线上** —— 这是整套方法成立的前提。
  · 红线是墙的**顶边**，而墙的侧面/阴影会因视差被投影到旁边：
    所以判据必须带一个横向搜索带（墙有厚度），不能只取中心 1 像素。
    · 实拍里有大量非迷宫杂物（笔记本、纸箱、卷尺），会遮挡红线。
    正式场合要求全部露出；遮挡区必须输出 **unknown**，绝不能静默当成"无墙"。

────────────────────────────────────────────────────────────────────────────
接口决策（用户 2026-10-04 拍板，已落进本文件）
────────────────────────────────────────────────────────────────────────────
  ① **迷宫外墙 = 场地边界** → 外墙在 k=0 / k=8，`boundary_is_wall=True`。
  ② **邻接图是主输出** → `to_adjacency()` / `to_adjacency_text()`；
     墙段三态、cover/conf、每格 4bit 位图降为次级视图（仍全部保留）。
  ③ **UNKNOWN 默认当有墙** → `unknown_as_wall=True`；同时提供乐观视图
     `to_adjacency(unknown_as_wall=False)`，不替用户丢掉另一种解释。
  ④ **永久保持独立脚本** → 不并入 `aruco_locator` 包，v1.x 接口保持冻结。

────────────────────────────────────────────────────────────────────────────
约定（与 `aruco_locator` 现有场地系一致）
────────────────────────────────────────────────────────────────────────────
场地系 [0,1]²：原点 = 离画面左下角最近的场地角点，+x 沿底边向右，+y 沿左边向上。
迷宫格索引 (i, j)：i = 沿 +x 的列号 0..7，j = 沿 +y 的行号 0..7。
  · 格 (i, j) 覆盖 x∈[i/s,(i+1)/s]、y∈[j/s,(j+1)/s]，格心 ((i+.5)/s, (j+.5)/s)
  · 线性格号 `k = j*size + i`（行主序）
  · 候选墙段共 2·s·(s+1) = 144 条：
      `ew[i, j]` 竖墙，位于 x=i/s，跨 y∈[j/s, (j+1)/s]      i=0..s, j=0..s-1
      `ns[i, j]` 横墙，位于 y=j/s，跨 x∈[i/s, (i+1)/s]      i=0..s-1, j=0..s
  · 格 (i,j) 的四面墙：W=ew[i,j]  E=ew[i+1,j]  S=ns[i,j]  N=ns[i,j+1]
  · 方向枚举 DIR = 0:N(+y) 1:E(+x) 2:S(-y) 3:W(-x)

用法::

    python maze_extract.py --source samples/IMG_20261003_163808.jpg
    python maze_extract.py --source samples/ --out out_maze --limit 4 --adj
    python maze_extract.py --source samples/IMG_20261003_163808.jpg --ascii --adj
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, field, asdict

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from aruco_locator import create_locator  # noqa: E402
from aruco_locator.sources import imread_unicode, imwrite_unicode, list_images  # noqa: E402

# 三态：不能只有 wall / open —— 遮挡区必须能表达"不知道"
OPEN, WALL, UNKNOWN = 0, 1, -1
DIR_NAME = {0: "N", 1: "E", 2: "S", 3: "W"}
_DELTA = {0: (0, 1), 1: (1, 0), 2: (0, -1), 3: (-1, 0)}  # N=+y, E=+x, S=-y, W=-x


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@dataclass
class MazeConfig:
    """迷宫解算参数。全部走配置，不硬编码（沿用项目约定）。"""

    size: int = 8  # 迷宫格数（8×8）

    # --- 正射重采样 ---
    ortho_side_px: int = 1440  # 正射图边长（像素）。取 1440 → 1px ≈ 1mm（场地 1440mm）
    ortho_margin: float = 0.06  # 四边外扩（场地单位比例），把外圈红线完整含进来

    # --- 红色提取（HSV 双区间，红在色环两端）---
    red_hsv_1: tuple = ((0, 95, 70), (10, 255, 255))
    red_hsv_2: tuple = ((170, 95, 70), (180, 255, 255))
    morph_open_px: int = 3  # 形态学开运算核（去椒盐）

    # --- 墙段判据 ---
    search_halfwidth: float = 0.030  # 横向搜索半宽（场地单位）≈ 43mm，覆盖墙厚/微偏移
    segment_trim: float = 0.25  # 每条墙段两端各裁掉的比例（避开交叉点串扰）
    samples: int = 48  # 沿墙段的采样点数
    cover_wall: float = 0.55  # 覆盖率 ≥ 此值 → 判为墙
    cover_open: float = 0.30  # 覆盖率 ≤ 此值 → 判为无墙；中间 → 不确定

    # --- 可判性（防"遮挡被静默当成无墙"）---
    # 判据：该位置要么看得见红、要么看得见"暗色地面"。两者都没有 → 没看清 → UNKNOWN。
    # ⚠ 这是本原型最粗糙的一环，阈值依赖现场光照，必须按比赛场地重新标定。
    dark_v_max: float = 110.0  # 灰度 V 低于此值视为"黑地面"（可见）
    conf_min: float = 0.60  # 可判性低于此值 → UNKNOWN（不猜）

    # --- 质量门限 ---
    min_red_ratio: float = 0.004  # 场内红色占比低于此值 → 整帧判无效（没拍到红线）
    # 迷宫外墙 = 场地边界（用户 2026-10-04 拍板）。所以外圈恒为墙，
    # 外圈红胶带同时扮演"场地标记"与"迷宫最外圈墙"两个角色，都在 k=0 / k=8 上。
    # 仍会测量外圈覆盖率并报告 —— 它是"场地系有没有锚准"的免费绝对参照。
    boundary_is_wall: bool = True
    field_size_mm: float = 1440.0  # 场地边长，**只**用于把诊断量换算成 mm（不影响归一化结果）
    align_warn_mm: float = 12.0  # 红线到最近 k/8 格线的中位距离超过此值 → 打 flag

    def __post_init__(self):
        if self.size < 1:
            raise ValueError("size 必须 ≥ 1")
        if not 0 <= self.segment_trim < 0.5:
            raise ValueError("segment_trim 必须在 [0, 0.5)")
        if not 0 <= self.cover_open < self.cover_wall <= 1:
            raise ValueError("要求 0 ≤ cover_open < cover_wall ≤ 1")
        if not 0 <= self.conf_min <= 1:
            raise ValueError("conf_min 必须在 [0, 1]")


# --------------------------------------------------------------------------- #
# 输出结构
# --------------------------------------------------------------------------- #
@dataclass
class Maze:
    """单帧解出的迷宫结构。

    `state` / `cover` 用**墙段**索引（2·s·(s+1) 条），而不是"每格 4 面墙"——
    因为相邻两格共享同一条墙段，用墙段索引天然避免重复存储与不一致。
    "每格 4 墙"视图由 :meth:`wall_state` / :meth:`cell_walls` 按需导出。
    """

    size: int = 8
    valid: bool = False
    reason: str = ""

    ew_state: np.ndarray = field(default_factory=lambda: np.zeros((0, 0), np.int8))
    ns_state: np.ndarray = field(default_factory=lambda: np.zeros((0, 0), np.int8))
    ew_cover: np.ndarray = field(default_factory=lambda: np.zeros((0, 0), np.float32))
    ns_cover: np.ndarray = field(default_factory=lambda: np.zeros((0, 0), np.float32))
    ew_conf: np.ndarray = field(default_factory=lambda: np.zeros((0, 0), np.float32))
    ns_conf: np.ndarray = field(default_factory=lambda: np.zeros((0, 0), np.float32))

    # 元数据：把迷宫与"某一帧的场地系"绑定，否则结构无法与定位结果对齐
    frame_index: int = 0
    timestamp: float = 0.0
    origin_id: int | None = None  # 场地系原点角标记 ID
    H_img2field: np.ndarray | None = field(default=None, repr=False)  # 不序列化
    ortho_bbox_px: tuple | None = None  # 正射图 ←→ 原图 的映射参数（供回投影）

    red_ratio: float | None = None  # 场内红色面积占比
    flags: list[str] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)

    # ---------------- 墙段索引访问 ----------------
    def wall_state(self, i: int, j: int, d: int) -> int:
        """格 (i,j) 在方向 d 上那条墙段的三态值。"""
        if d == 3:
            return int(self.ew_state[i, j])
        if d == 1:
            return int(self.ew_state[i + 1, j])
        if d == 2:
            return int(self.ns_state[i, j])
        if d == 0:
            return int(self.ns_state[i, j + 1])
        raise ValueError(f"方向必须 0..3，得到 {d}")

    def cell_walls(self, i: int, j: int) -> tuple[int, int, int, int]:
        """格 (i,j) 的四面墙三态，按 DIR 顺序 (N,E,S,W)。"""
        return tuple(self.wall_state(i, j, d) for d in range(4))

    def has_wall(self, i: int, j: int, d: int) -> bool:
        return self.wall_state(i, j, d) == WALL

    # ---------------- 邻接视图（用户原始思路要的量）----------------
    def can_go(self, i: int, j: int, d: int, unknown_as_wall: bool = True) -> bool:
        """能否从格 (i,j) 沿方向 d 走到邻格（该侧无墙，且不越界）。

        `unknown_as_wall=True`（默认，**已按用户拍板**）：UNKNOWN 一律当"不通"。
        保守方向 —— 宁可让下游认为走不通，也绝不让寻路穿墙。
        """
        di, dj = _DELTA[d]
        ni, nj = i + di, j + dj
        if not (0 <= ni < self.size and 0 <= nj < self.size):
            return False
        st = self.wall_state(i, j, d)
        return st == OPEN if unknown_as_wall else st != WALL

    def neighbors(self, i: int, j: int, unknown_as_wall: bool = True) -> list[tuple[int, int]]:
        """格 (i,j) 的可达邻格（保序：N/E/S/W）。"""
        out = []
        for d in range(4):
            if self.can_go(i, j, d, unknown_as_wall):
                di, dj = _DELTA[d]
                out.append((i + di, j + dj))
        return out

    # ---------------- 邻接图（**主输出**）----------------
    def index_of(self, i: int, j: int) -> int:
        """格 (i,j) → 线性序号 `j*size + i`（行主序，j 沿 +y）。"""
        return j * self.size + i

    def coord_of(self, k: int) -> tuple[int, int]:
        return k % self.size, k // self.size

    def adjacency_edges(self, unknown_as_wall: bool = True) -> list[list[int]]:
        """邻接图的边表：每条可通行关系只列一次，且保证 `a < b`。"""
        s = self.size
        edges = []
        for j in range(s):
            for i in range(s):
                a = self.index_of(i, j)
                if i + 1 < s and self.can_go(i, j, 1, unknown_as_wall):
                    edges.append([a, self.index_of(i + 1, j)])
                if j + 1 < s and self.can_go(i, j, 0, unknown_as_wall):
                    edges.append([a, self.index_of(i, j + 1)])
        return edges

    def to_adjacency(self, unknown_as_wall: bool = True) -> dict:
        """**主输出**：迷宫邻接图。

        `unknown_as_wall=True`（默认）把"没看清"当作"不通"。信息不丢 ——
        UNKNOWN 的数量与具体位置在 `counts.n_unknown` 与 `walls.*_state` 里单独给出，
        下游想改成"乐观解释"只要把这个参数设成 False，或直接读墙段三态。
        """
        nb: dict[str, list[int]] = {}
        for a, b in self.adjacency_edges(unknown_as_wall):
            nb.setdefault(str(a), []).append(b)
            nb.setdefault(str(b), []).append(a)
        n, full = self.reachable_cells(unknown_as_wall)
        return {
            "index_rule": "k = j*size + i   (i 沿 +x，j 沿 +y)",
            "neighbors": {k: sorted(v) for k, v in sorted(nb.items(), key=lambda kv: int(kv[0]))},
            "n_edge": len(self.adjacency_edges(unknown_as_wall)),
            "n_cell_reached": n,
            "connected": bool(full),
        }

    def to_adjacency_text(self, unknown_as_wall: bool = True) -> str:
        """邻接图的紧凑文本：每行 `格号(i,j) -> 邻格...`，只列有邻居的格。"""
        s = self.size
        lines = []
        for j in range(s):
            for i in range(s):
                ns = self.neighbors(i, j, unknown_as_wall)
                if not ns:
                    continue
                a = self.index_of(i, j)
                lbl = ",".join(f"{self.index_of(ni, nj)}" for ni, nj in ns)
                lines.append(f"{a:>3} ({i},{j}) -> {lbl}")
        return "\n".join(lines) if lines else "(无任何可通行关系)"

    # ---------------- 统计 ----------------
    def count(self, value: int) -> int:
        return int((self.ew_state == value).sum() + (self.ns_state == value).sum())

    @property
    def n_wall(self) -> int:
        return self.count(WALL)

    @property
    def n_unknown(self) -> int:
        return self.count(UNKNOWN)

    @property
    def n_segment(self) -> int:
        return int(self.ew_state.size + self.ns_state.size)

    def n_internal_wall(self) -> int:
        """内部墙数（不含外圈）。"""
        return int((self.ew_state[1:-1, :] == WALL).sum() + (self.ns_state[:, 1:-1] == WALL).sum())

    @property
    def expected_internal_walls(self) -> int:
        """**完美迷宫**（生成树：全连通且无环）应有的内部墙数。

        8×8：内部边 2·s·(s−1) = 112 条，格数 64 → 112 − (64−1) = **49** 面。
        实际明显少于它 → 有环或漏检；明显多于 → 误检。**免费的结构自检。**
        """
        s = self.size
        return 2 * s * (s - 1) - (s * s - 1)

    def reachable_cells(self, unknown_as_wall: bool = True) -> tuple[int, bool]:
        """开放邻接下可达的格数、是否全连通。默认**未知墙按不可达**（保守）。"""
        s = self.size
        seen = np.zeros((s, s), bool)
        seen[0, 0] = True
        stack, n = [(0, 0)], 1
        while stack:
            i, j = stack.pop()
            for ni, nj in self.neighbors(i, j, unknown_as_wall):
                if not seen[ni, nj]:
                    seen[ni, nj] = True
                    n += 1
                    stack.append((ni, nj))
        return n, n == s * s

    def sanity(self) -> dict:
        """结构自检 —— 供上层判定"这份迷宫能不能用"。"""
        s = self.size
        n, full = self.reachable_cells()
        n_int = self.n_internal_wall()
        return {
            "n_wall": self.n_wall,
            "n_unknown": self.n_unknown,
            "internal_wall": n_int,
            "expected_internal_wall": self.expected_internal_walls,
            "reachable": n,
            "n_cells": s * s,
            "connected": bool(full),
            "loops": (2 * s * (s - 1) - n_int) - (s * s - 1) if full else None,
        }

    # ---------------- 序列化 ----------------
    def to_dict(self, with_cover: bool = True) -> dict:
        """序列化。**邻接图排在最前**（已按用户拍板：邻接图是主输出），
        墙段三态与 cover/conf 作为次级视图附在后面。"""
        return {
            "size": int(self.size),
            "valid": bool(self.valid),
            "reason": self.reason,
            "frame": int(self.frame_index),
            "time": round(float(self.timestamp), 6),
            "origin_id": self.origin_id,
            "red_ratio": None if self.red_ratio is None else round(float(self.red_ratio), 6),
            # —— 主输出 ——
            "adjacency": self.to_adjacency() if self.valid else None,
            "adjacency_optimistic": (self.to_adjacency(unknown_as_wall=False) if self.valid else None),
            # —— 次级视图 ——
            "walls": {
                "ew_state": [[int(v) for v in row] for row in self.ew_state],
                "ns_state": [[int(v) for v in row] for row in self.ns_state],
                "ew_cover": ([[round(float(v), 4) for v in row] for row in self.ew_cover]
                             if with_cover else None),
                "ns_cover": ([[round(float(v), 4) for v in row] for row in self.ns_cover]
                             if with_cover else None),
                "ew_conf": ([[round(float(v), 4) for v in row] for row in self.ew_conf]
                            if with_cover else None),
                "ns_conf": ([[round(float(v), 4) for v in row] for row in self.ns_conf]
                            if with_cover else None),
            },
            "counts": {"n_segment": self.n_segment, "n_wall": self.n_wall,
                       "n_unknown": self.n_unknown},
            "cell_codes": self.cell_codes() if self.valid else None,
            "flags": list(self.flags),
            "diagnostics": self.diagnostics,
        }

    def to_json(self, **kw) -> str:
        return json.dumps(self.to_dict(**kw), ensure_ascii=False)

    def cell_codes(self) -> list[int]:
        """每格 4 bit（bit0=N bit1=E bit2=S bit3=W），**未知位按 1（有墙）**。

        这是电脑鼠竞赛的通行表示：8×8 → 64 个 0..15 的整数 / 32 字节。
        "未知按有墙"是保守选择，保证下游寻路不会穿墙。
        """
        codes = []
        for j in range(self.size):
            for i in range(self.size):
                c = 0
                for d in range(4):
                    if self.wall_state(i, j, d) != OPEN:  # WALL 或 UNKNOWN 都置位
                        c |= 1 << d
                codes.append(c)
        return codes

    def to_bytes(self) -> bytes:
        """32 字节紧凑表示（64 格 × 4 bit）。"""
        codes = self.cell_codes()
        out = bytearray()
        for k in range(0, len(codes), 2):
            hi = codes[k] & 0xF
            lo = codes[k + 1] & 0xF if k + 1 < len(codes) else 0
            out.append((hi << 4) | lo)
        return bytes(out)

    def to_ascii(self, show_cover: bool = False) -> str:
        """用字符画出迷宫。行序自上而下 = j 从大到小（+y 向上）。"""
        s = self.size
        lines = []
        for j in range(s, -1, -1):
            top = "+"
            for i in range(s):
                st = self.wall_state(i, j - 1, 0) if j > 0 else WALL  # 上方
                top += ("---" if st != OPEN else "   ") + "+"
            lines.append(top)
            if j == 0:
                continue
            mid = "|" if self.wall_state(0, j - 1, 3) != OPEN else " "
            for i in range(s):
                lbl = f"{i}{j-1}"
                st = self.wall_state(i, j - 1, 1)
                mid += lbl.center(3) + ("|" if st != OPEN else " ")
            lines.append(mid)
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 正射重采样
# --------------------------------------------------------------------------- #
def _ortho_matrix(H_img2field: np.ndarray, side: int, margin: float) -> tuple[np.ndarray, int, int]:
    """像素 → 正射图 的 3×3 矩阵。

    正射图坐标：场地 (0,0) 落在 (m, m+S)，场地 (1,1) 落在 (m+S, m)，
    即 **+y 向上**（与场地系一致，看起来像"平面图"而不是"照片"）。
    """
    m = margin * side
    A = np.array([[side, 0.0, m],
                  [0.0, -side, m + side],
                  [0.0, 0.0, 1.0]])
    return A @ np.asarray(H_img2field, np.float64), int(round(side + 2 * m)), int(round(side + 2 * m))


def make_ortho(frame: np.ndarray, H_img2field: np.ndarray, cfg: MazeConfig):
    """返回 (正射图, 红色掩码, 正射灰度, 场内红色占比, (M, W, H))。"""
    M, W, H = _ortho_matrix(H_img2field, cfg.ortho_side_px, cfg.ortho_margin)
    ortho = cv2.warpPerspective(frame, M, (W, H), flags=cv2.INTER_AREA,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=(20, 20, 20))
    gray = cv2.cvtColor(ortho, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(ortho, cv2.COLOR_BGR2HSV)
    red = cv2.bitwise_or(
        cv2.inRange(hsv, np.array(cfg.red_hsv_1[0], np.uint8), np.array(cfg.red_hsv_1[1], np.uint8)),
        cv2.inRange(hsv, np.array(cfg.red_hsv_2[0], np.uint8), np.array(cfg.red_hsv_2[1], np.uint8)),
    )
    if cfg.morph_open_px >= 3:
        k = np.ones((cfg.morph_open_px, cfg.morph_open_px), np.uint8)
        red = cv2.morphologyEx(red, cv2.MORPH_OPEN, k)
    m = int(cfg.ortho_margin * cfg.ortho_side_px)
    inner = red[m:m + cfg.ortho_side_px, m:m + cfg.ortho_side_px]
    ratio = float((inner > 0).mean()) if inner.size else 0.0
    return ortho, red, gray, ratio, (M, W, H)


# --------------------------------------------------------------------------- #
# 墙段测量
# --------------------------------------------------------------------------- #
def _field_to_ortho_px(pts: np.ndarray, side: int, margin: float) -> np.ndarray:
    m = margin * side
    x = m + pts[:, 0] * side
    y = m + (1.0 - pts[:, 1]) * side
    return np.stack([x, y], axis=1)


def _segment_cover(red: np.ndarray, gray: np.ndarray, p0, p1, side: int, margin: float,
                   cfg: MazeConfig) -> tuple[float, float]:
    """沿墙段测 (红色覆盖率, 可判性)。

    用**搜索带**而不是中心 ±1px，是因为：① 墙有厚度，红线是墙的顶边不一定压在中线；
    ② 单应/精化有残余误差。带内"存在红"即算覆盖，因此对横向偏移不敏感；
    垂直穿过的另一条墙只会贡献几毫米的长度覆盖率，被 segment_trim + 阈值滤掉。

    可判性 `conf` = 沿线采样点中"看得见红 或 看得见暗色地面"的比例。
    它是为了挡住一个致命失效：**杂物遮挡 → 测不到红 → 被误判成"无墙"**。
    正式场合迷宫全露出时 conf 应接近 1；低 conf 只能说明"没看清"，不能说明"没墙"。
    """
    ts = np.linspace(cfg.segment_trim, 1.0 - cfg.segment_trim, cfg.samples)
    p0 = np.asarray(p0, float)
    p1 = np.asarray(p1, float)
    pts = p0[None, :] + ts[:, None] * (p1 - p0)[None, :]
    q = _field_to_ortho_px(pts, side, margin)

    d = q[-1] - q[0]
    n = np.linalg.norm(d)
    if n < 1e-9:
        return 0.0, 0.0
    d = d / n
    nvec = np.array([-d[1], d[0]])

    hw = cfg.search_halfwidth * side
    offs = np.arange(-hw, hw + 0.5, 1.0)
    if offs.size == 0:
        offs = np.array([0.0])
    h, w = red.shape
    ns = pts.shape[0]
    any_red = np.zeros(ns, bool)
    min_v = np.full(ns, 255.0, np.float32)
    for o in offs:
        p = q + o * nvec
        xi = np.clip(np.round(p[:, 0]).astype(np.int32), 0, w - 1)
        yi = np.clip(np.round(p[:, 1]).astype(np.int32), 0, h - 1)
        any_red |= red[yi, xi] > 0
        min_v = np.minimum(min_v, gray[yi, xi])
    known = any_red | (min_v < cfg.dark_v_max)
    return float(any_red.mean()), float(known.mean())


def measure_walls(red: np.ndarray, gray: np.ndarray, cfg: MazeConfig):
    """测全部 144 条候选墙段 → ((ew_cover, ew_conf), (ns_cover, ns_conf))。"""
    s = cfg.size
    side, margin = cfg.ortho_side_px, cfg.ortho_margin
    ew = np.zeros((s + 1, s), np.float32)
    ew_cf = np.zeros((s + 1, s), np.float32)
    ns = np.zeros((s, s + 1), np.float32)
    ns_cf = np.zeros((s, s + 1), np.float32)
    for i in range(s + 1):
        x = i / s
        for j in range(s):
            ew[i, j], ew_cf[i, j] = _segment_cover(
                red, gray, (x, j / s), (x, (j + 1) / s), side, margin, cfg)
    for i in range(s):
        for j in range(s + 1):
            y = j / s
            ns[i, j], ns_cf[i, j] = _segment_cover(
                red, gray, (i / s, y), ((i + 1) / s, y), side, margin, cfg)
    return (ew, ew_cf), (ns, ns_cf)


def red_grid_alignment(red: np.ndarray, cfg: MazeConfig) -> dict:
    """**核心前提的在线自检**：红线到最近一条 k/s 格线的距离（mm）。

    整套方法建立在"墙顶红线正好落在场地系 k/s 格线上"。这句话如果不对（场地系锚偏、
    内顶点选错、或者现场根本不按等分格布线），后面全部作废。所以每帧都量一次：
    对每个红像素，取"到最近竖格线"与"到最近横格线"里更小的那个，看分布。
    前提成立时中位数应在个位数 mm 量级（正射图 1px ≈ 1mm）。
    """
    ys, xs = np.nonzero(red)
    if xs.size == 0:
        return {"n": 0}
    m = cfg.ortho_margin * cfg.ortho_side_px
    stride = max(1, xs.size // 60000)  # 抽稀，避免大图上算几百万点
    x = (xs[::stride] - m) / cfg.ortho_side_px  # → 场地坐标
    y = (cfg.ortho_side_px + m - ys[::stride]) / cfg.ortho_side_px
    s = cfg.size
    dx = np.abs(x * s - np.round(x * s)) / s
    dy = np.abs(y * s - np.round(y * s)) / s
    d = np.minimum(dx, dy) * cfg.field_size_mm
    return {
        "n": int(d.size),
        "median_mm": float(np.median(d)),
        "p90_mm": float(np.percentile(d, 90)),
    }


def _to_state(cover: np.ndarray, conf: np.ndarray, cfg: MazeConfig) -> np.ndarray:
    """三态判定。顺序很重要：**先看可判性，再看覆盖率**。

    覆盖率低有两种完全不同的原因 —— "确实没墙"和"根本没看清"。只有 conf 足够高时，
    低覆盖率才允许解释成"无墙"。
    """
    st = np.full(cover.shape, UNKNOWN, np.int8)
    sure = conf >= cfg.conf_min
    st[sure & (cover >= cfg.cover_wall)] = WALL
    st[sure & (cover <= cfg.cover_open)] = OPEN
    return st


# --------------------------------------------------------------------------- #
# 端到端
# --------------------------------------------------------------------------- #
def extract_maze(frame: np.ndarray, H_img2field: np.ndarray, cfg: MazeConfig | None = None,
                 *, frame_index: int = 0, timestamp: float = 0.0,
                 origin_id: int | None = None) -> tuple[Maze, np.ndarray, np.ndarray]:
    """**核心接口**：给一帧图 + 场地系单应，解出迷宫结构。

    返回 (Maze, 正射图, 红色掩码)。正射图/掩码一并返回是刻意的 ——
    调用方几乎总要出图或做二次分析，重算一遍代价不小且可能不一致。
    """
    cfg = cfg or MazeConfig()
    maze = Maze(size=cfg.size, frame_index=frame_index, timestamp=timestamp, origin_id=origin_id,
                H_img2field=np.asarray(H_img2field, np.float64).copy())

    s = cfg.size
    ortho, red, gray, ratio, bbox = make_ortho(frame, H_img2field, cfg)
    maze.ortho_bbox_px = bbox
    maze.red_ratio = ratio

    if ratio < cfg.min_red_ratio:
        maze.reason = "no_red_found"
        maze.ew_state = np.full((s + 1, s), UNKNOWN, np.int8)
        maze.ns_state = np.full((s, s + 1), UNKNOWN, np.int8)
        for name in ("ew_cover", "ew_conf"):
            setattr(maze, name, np.zeros((s + 1, s), np.float32))
        for name in ("ns_cover", "ns_conf"):
            setattr(maze, name, np.zeros((s, s + 1), np.float32))
        return maze, ortho, red

    (ew_c, ew_cf), (ns_c, ns_cf) = measure_walls(red, gray, cfg)
    maze.ew_cover, maze.ns_cover = ew_c, ns_c
    maze.ew_conf, maze.ns_conf = ew_cf, ns_cf
    maze.ew_state = _to_state(ew_c, ew_cf, cfg)
    maze.ns_state = _to_state(ns_c, ns_cf, cfg)

    # 外圈：迷宫里恒为墙（封闭）。有配置时才强制，且**不掩盖测量值** ——
    # 外圈覆盖率是"场地系是否锚准"的免费外部参照，偏差大会打 flag。
    if cfg.boundary_is_wall:
        outer_c = np.concatenate([ew_c[0, :], ew_c[-1, :], ns_c[:, 0], ns_c[:, -1]])
        outer_f = np.concatenate([ew_cf[0, :], ew_cf[-1, :], ns_cf[:, 0], ns_cf[:, -1]])
        if outer_c.size and float(np.max(outer_c * (outer_f >= cfg.conf_min)) -
                                  np.min(outer_c * (outer_f >= cfg.conf_min))) > 0.35:
            maze.flags.append("boundary_uneven")
        if float(np.median(outer_c)) < cfg.cover_open:
            maze.flags.append("boundary_missing")
        maze.ew_state[0, :] = maze.ew_state[-1, :] = WALL
        maze.ns_state[:, 0] = maze.ns_state[:, -1] = WALL

    if maze.n_unknown:
        maze.flags.append("has_unknown")
    if maze.n_unknown > maze.n_segment * 0.25:
        maze.flags.append("mostly_unknown")

    maze.valid = True
    san = maze.sanity()
    al = red_grid_alignment(red, cfg)
    maze.diagnostics["sanity"] = san
    maze.diagnostics["grid_alignment"] = al
    maze.diagnostics["cover_median"] = float(np.median(np.concatenate([ew_c.ravel(), ns_c.ravel()])))
    maze.diagnostics["conf_median"] = float(np.median(np.concatenate([ew_cf.ravel(), ns_cf.ravel()])))
    if not san["connected"]:
        maze.flags.append("disconnected")
    if al.get("n") and al["median_mm"] > cfg.align_warn_mm:
        maze.flags.append("grid_misaligned")
    return maze, ortho, red


# --------------------------------------------------------------------------- #
# 出图
# --------------------------------------------------------------------------- #
C_OPEN, C_WALL, C_UNK = (90, 90, 90), (255, 220, 0), (0, 215, 255)  # BGR: 灰 / 青 / 黄


def render_maze(ortho: np.ndarray, red: np.ndarray, maze: Maze, cfg: MazeConfig,
                dim: float = 0.28, draw_cover: bool = False) -> np.ndarray:
    """把解算结果画到正射图上 —— 这就是"根据红色边界解算迷宫结构"的输出图。

    配色约定：**底图里的暗红 = 原始红掩码**（测量的原料）；
    叠加上去的 **青色实线 = 判定为墙**、**黄色虚线 = 不确定**、**灰色虚线 = 判定无墙**。
    """
    vis = cv2.addWeighted(ortho, dim, np.zeros_like(ortho), 1 - dim, 0)
    vis[red > 0] = (0, 0, 120)  # 原始红掩码压暗显示（暗红）
    s, side, margin = cfg.size, cfg.ortho_side_px, cfg.ortho_margin

    def px(p):
        return _field_to_ortho_px(np.asarray([p], float), side, margin)[0]

    # 格线（细灰）
    for k in range(s + 1):
        cv2.line(vis, tuple(np.int32(px((k / s, 0)))), tuple(np.int32(px((k / s, 1)))), (70, 70, 70), 1)
        cv2.line(vis, tuple(np.int32(px((0, k / s)))), tuple(np.int32(px((1, k / s)))), (70, 70, 70), 1)

    def draw(state, cover, p0, p1):
        a, b = np.int32(px(p0)), np.int32(px(p1))
        if state == WALL:
            cv2.line(vis, tuple(a), tuple(b), C_WALL, 4, cv2.LINE_AA)
        else:
            color = C_UNK if state == UNKNOWN else C_OPEN
            for t in np.linspace(0.12, 0.88, 6):
                q0 = np.int32(a + (b - a) * (t - 0.07))
                q1 = np.int32(a + (b - a) * (t + 0.07))
                cv2.line(vis, tuple(q0), tuple(q1), color, 2, cv2.LINE_AA)
        if draw_cover:
            cv2.putText(vis, f"{cover:.2f}", tuple(np.int32((a + b) / 2) + np.array([2, -4])),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.32, (255, 255, 255), 1)

    for i in range(s + 1):
        for j in range(s):
            draw(int(maze.ew_state[i, j]), float(maze.ew_cover[i, j]),
                 (i / s, j / s), (i / s, (j + 1) / s))
    for i in range(s):
        for j in range(s + 1):
            draw(int(maze.ns_state[i, j]), float(maze.ns_cover[i, j]),
                 (i / s, j / s), ((i + 1) / s, j / s))

    # 格心编号
    for i in range(s):
        for j in range(s):
            c = np.int32(px(((i + 0.5) / s, (j + 0.5) / s)))
            cv2.putText(vis, f"{i}{j}", tuple(c + np.array([-9, 5])), cv2.FONT_HERSHEY_SIMPLEX,
                        0.4, (170, 170, 170), 1, cv2.LINE_AA)

    # 原点与坐标轴
    o = np.int32(px((0, 0)))
    cv2.circle(vis, tuple(o), 6, (230, 200, 60), -1, cv2.LINE_AA)
    cv2.arrowedLine(vis, tuple(o), tuple(np.int32(px((0.18, 0)))), (230, 200, 60), 2, cv2.LINE_AA, tipLength=0.2)
    cv2.arrowedLine(vis, tuple(o), tuple(np.int32(px((0, 0.18)))), (230, 200, 60), 2, cv2.LINE_AA, tipLength=0.2)
    cv2.putText(vis, "+x", tuple(np.int32(px((0.20, 0)))), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (230, 200, 60), 1)
    cv2.putText(vis, "+y", tuple(np.int32(px((0.02, 0.20)))), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (230, 200, 60), 1)

    # HUD
    san = (maze.diagnostics or {}).get("sanity") or {}
    hud = [f"frame {maze.frame_index}  valid={maze.valid}  {maze.reason}",
           f"origin_id={maze.origin_id}  red={0 if maze.red_ratio is None else maze.red_ratio:.4f}",
           f"wall={maze.n_wall}/{maze.n_segment}   unknown={maze.n_unknown}",
           f"internal wall={san.get('internal_wall','?')} (perfect maze={san.get('expected_internal_wall','?')})"
           f"  connected={san.get('connected','?')}  loops={san.get('loops','?')}",
           f"flags: {','.join(maze.flags) or '-'}",
           "cyan solid = WALL   yellow dash = UNKNOWN   grey dash = OPEN"]
    y = 26
    for t in hud:
        cv2.putText(vis, t, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(vis, t, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (240, 240, 240), 1, cv2.LINE_AA)
        y += 24
    return vis


def render_structure(maze: Maze, cfg: MazeConfig, side: int = 900, pad: int = 60) -> np.ndarray:
    """**纯结构示意图**：不贴照片，只画解出的迷宫 —— 这才是最终交付的那张图。

    白底黑墙；三条信息分开表达：
      实线粗黑 = 墙；虚线灰 = 未知；细浅灰 = 确认无墙（保留格线，便于核对）。
    """
    vis = np.full((side + 2 * pad, side + 2 * pad, 3), 255, np.uint8)
    s = cfg.size

    def px(p):
        return (int(pad + p[0] * side), int(pad + (1.0 - p[1]) * side))

    for k in range(s + 1):
        cv2.line(vis, px((k / s, 0)), px((k / s, 1)), (232, 232, 232), 1)
        cv2.line(vis, px((0, k / s)), px((1, k / s)), (232, 232, 232), 1)

    def draw(state, p0, p1):
        a, b = px(p0), px(p1)
        if state == WALL:
            cv2.line(vis, a, b, (30, 30, 30), 7, cv2.LINE_AA)
        elif state == UNKNOWN:
            for t in np.linspace(0.1, 0.9, 7):
                q0 = tuple(np.int32(np.array(a) + (np.array(b) - np.array(a)) * (t - 0.06)))
                q1 = tuple(np.int32(np.array(a) + (np.array(b) - np.array(a)) * (t + 0.06)))
                cv2.line(vis, q0, q1, (170, 190, 255), 4, cv2.LINE_AA)

    for i in range(s + 1):
        for j in range(s):
            draw(int(maze.ew_state[i, j]), (i / s, j / s), (i / s, (j + 1) / s))
    for i in range(s):
        for j in range(s + 1):
            draw(int(maze.ns_state[i, j]), (i / s, j / s), ((i + 1) / s, j / s))

    for i in range(s):
        for j in range(s):
            c = px(((i + 0.5) / s, (j + 0.5) / s))
            cv2.putText(vis, f"{i},{j}", (c[0] - 16, c[1] + 5), cv2.FONT_HERSHEY_SIMPLEX,
                        0.42, (150, 150, 150), 1, cv2.LINE_AA)

    o = px((0, 0))
    cv2.circle(vis, o, 7, (200, 120, 0), -1, cv2.LINE_AA)
    cv2.putText(vis, "origin", (o[0] + 10, o[1] + 26), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 120, 0), 1)
    cv2.arrowedLine(vis, o, px((0.14, 0)), (200, 120, 0), 2, cv2.LINE_AA, tipLength=0.2)
    cv2.arrowedLine(vis, o, px((0, 0.14)), (200, 120, 0), 2, cv2.LINE_AA, tipLength=0.2)

    san = (maze.diagnostics or {}).get("sanity") or {}
    hud = [f"maze {s}x{s}   frame {maze.frame_index}   origin_id={maze.origin_id}",
           f"wall={maze.n_wall}  unknown={maze.n_unknown}"
           f"  internal={san.get('internal_wall','?')}(perfect={san.get('expected_internal_wall','?')})"
           f"  connected={san.get('connected','?')}",
           f"flags: {','.join(maze.flags) or '-'}"]
    y = 26
    for t in hud:
        cv2.putText(vis, t, (pad - 44, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (60, 60, 60), 1, cv2.LINE_AA)
        y += 22
    return vis


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _process(frame, H, cfg, idx, ts, origin_id, out_dir, tag, ascii_out, draw_cover,
             clean=False, adj=False):
    maze, ortho, red = extract_maze(frame, H, cfg, frame_index=idx, timestamp=ts, origin_id=origin_id)
    san = maze.diagnostics.get("sanity") or {}
    print(f"[{tag}] valid={maze.valid} {maze.reason}  wall={maze.n_wall}/{maze.n_segment} "
          f"unknown={maze.n_unknown} red={0 if maze.red_ratio is None else maze.red_ratio:.4f}")
    if san:
        al = maze.diagnostics.get("grid_alignment") or {}
        print(f"      结构自检：内部墙 {san['internal_wall']}（完美迷宫 {san['expected_internal_wall']}）"
              f"  可达 {san['reachable']}/{san['n_cells']}  连通={san['connected']}  环数={san['loops']}")
        if al.get("n"):
            print(f"      前提自检：红线到最近 1/{cfg.size} 格线 中位 {al['median_mm']:.2f}mm / "
                  f"p90 {al['p90_mm']:.2f}mm")
        print(f"      flags={','.join(maze.flags) or '-'}")
    if adj and maze.valid:
        aj = maze.to_adjacency()
        print(f"      邻接图（主输出，UNKNOWN 当有墙）：{aj['n_edge']} 条通路，"
              f"可达 {aj['n_cell_reached']}/{cfg.size ** 2}，连通={aj['connected']}")
        for ln in maze.to_adjacency_text().splitlines():
            print("        " + ln)
    if ascii_out:
        print(maze.to_ascii())
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        vis = render_structure(maze, cfg) if clean else render_maze(ortho, red, maze, cfg, draw_cover=draw_cover)
        stem = os.path.splitext(tag)[0]
        imwrite_unicode(os.path.join(out_dir, f"{stem}_maze.png"), vis)
        with open(os.path.join(out_dir, f"{stem}_maze.json"), "w", encoding="utf-8") as fp:
            json.dump(maze.to_dict(), fp, ensure_ascii=False, indent=1)
    return maze


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="从红色墙线解算 8×8 迷宫结构（原型）")
    ap.add_argument("--source", "-s", required=True, help="图片 / 目录")
    ap.add_argument("--config", "-c", default=os.path.join(ROOT, "config.yaml"))
    ap.add_argument("--out", "-o", default=None, help="输出目录（写 *_maze.png / *.json）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少张（0=不限）")
    ap.add_argument("--ascii", action="store_true", help="打印字符迷宫")
    ap.add_argument("--adj", action="store_true", help="打印邻接图（主输出）")
    ap.add_argument("--clean", action="store_true", help="输出纯结构示意图（不贴照片）")
    ap.add_argument("--draw-cover", action="store_true", help="在墙上标覆盖率数值")
    ap.add_argument("--size", type=int, default=8)
    ap.add_argument("--cover-wall", type=float, default=None)
    ap.add_argument("--cover-open", type=float, default=None)
    ap.add_argument("--search-halfwidth", type=float, default=None)
    ns = ap.parse_args(argv)

    cfg = MazeConfig(size=ns.size)
    for a, b in (("cover_wall", ns.cover_wall), ("cover_open", ns.cover_open),
                 ("search_halfwidth", ns.search_halfwidth)):
        if b is not None:
            setattr(cfg, a, b)
    cfg.__post_init__()

    loc = create_locator(ns.config)
    files = list_images(ns.source) if os.path.isdir(ns.source) else [ns.source]
    if ns.limit:
        files = files[: ns.limit]

    ok = 0
    for k, fp in enumerate(files):
        frame = imread_unicode(fp)
        if frame is None:
            print(f"[{os.path.basename(fp)}] 读不到图")
            continue
        r = loc.locate(frame, frame_index=k)
        if not r.valid:
            print(f"[{os.path.basename(fp)}] 定位无效（{r.reason}）→ 迷宫无法上场地系，跳过")
            continue
        H = loc.last_debug.get("H")
        if H is None:
            print(f"[{os.path.basename(fp)}] 无 H，跳过")
            continue
        _process(frame, H, cfg, k, r.timestamp, r.origin_id, ns.out, os.path.basename(fp),
                 ns.ascii, ns.draw_cover, clean=ns.clean, adj=ns.adj)
        ok += 1
    print(f"\n完成 {ok}/{len(files)} 帧" + (f"，输出到 {ns.out}" if ns.out else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
