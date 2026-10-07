"""迷宫视图: 7x7(随 MAZETYPE 自适应)网格 + 墙 + 当前位置/朝向 + 探索轨迹。

坐标约定(已核对 STRATEGY/maze.c 的 MAZE_CoorUpdate):
    UP=0 -> cY++, RIGHT=1 -> cX++, DOWN=2 -> cY--, LEFT=3 -> cX--
    即 cX 向右增长(屏幕 x 方向), cY 向上增长(屏幕 y 需翻转)
墙位(mapWall 低四位, 置位=无墙/可通行):
    bit0=上(与 y+1 之间) bit1=右(与 x+1 之间) bit2=下(与 y-1 之间) bit3=左(与 x-1 之间)
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

WALL_UP_OPEN = 0x01
WALL_RIGHT_OPEN = 0x02
WALL_DOWN_OPEN = 0x04
WALL_LEFT_OPEN = 0x08

_DIR_NAME = {0: "UP", 1: "RIGHT", 2: "DOWN", 3: "LEFT"}


class MazeWidget(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(280, 280)
        self._wall: List[List[int]] = []
        self._visited: List[List[int]] = []
        self._n = 7
        self._pos: Tuple[int, int] = (0, 0)
        self._dir = 0
        self._task = 0
        self._trail: List[Tuple[int, int]] = []
        self._show_visited = True

    # ------------------------------------------------------------ 数据

    def set_map(self, rec: dict) -> None:
        self._wall = rec["mapWall"]
        self._visited = rec["mapVisited"]
        self._n = rec["mazetype"]
        self._pos = (rec["posX"], rec["posY"])
        self._dir = rec["direction"]
        self._task = rec["mouseTask"]
        self._append_trail()
        self.update()

    def set_position(self, x: int, y: int, task: int = -1) -> None:
        """来自慢帧的实时坐标(5Hz), 无需整块墙图。"""
        if (x, y) != self._pos:
            self._pos = (x, y)
            self._append_trail()
            self.update()
        elif task >= 0 and task != self._task:
            self._task = task
            self.update()

    def set_direction(self, d: int) -> None:
        if d != self._dir:
            self._dir = d
            self.update()

    def reset_trail(self) -> None:
        self._trail = []
        self.update()

    def _append_trail(self) -> None:
        if not self._trail or self._trail[-1] != self._pos:
            self._trail.append(self._pos)
            if len(self._trail) > 400:
                del self._trail[:200]

    # ------------------------------------------------------------ 绘制

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        n = self._n
        margin = 14
        cell = min((w - 2 * margin) / n, (h - 2 * margin) / n)
        ox = (w - cell * n) / 2.0
        oy = (h - cell * n) / 2.0

        def cxy(cx: int, cy: int) -> QPointF:
            # cY 向上增长 -> 屏幕 y 翻转
            return QPointF(ox + cx * cell, oy + (n - 1 - cy) * cell)

        p.fillRect(self.rect(), QColor("#1b1b1b"))

        # 未访问格底色
        for x in range(n):
            for y in range(n):
                r = QRectF(cxy(x, y), cxy(x, y) + QPointF(cell, cell))
                if self._show_visited and self._visited and x < len(self._visited) and y < len(self._visited[x]):
                    if not self._visited[x][y]:
                        p.fillRect(r, QColor("#262626"))

        # 网格
        p.setPen(QPen(QColor("#333333"), 1))
        for i in range(n + 1):
            p.drawLine(QPointF(ox + i * cell, oy), QPointF(ox + i * cell, oy + n * cell))
            p.drawLine(QPointF(ox, oy + i * cell), QPointF(ox + n * cell, oy + i * cell))

        # 外框(迷宫外边界一定是有墙的)
        p.setPen(QPen(QColor("#e0e0e0"), 2.5))
        p.drawRect(QRectF(ox, oy, cell * n, cell * n))

        # 探索轨迹
        if len(self._trail) > 1:
            p.setPen(QPen(QColor(255, 213, 79, 110), 1.6))
            for i in range(1, len(self._trail)):
                x0, y0 = self._trail[i - 1]
                x1, y1 = self._trail[i]
                p.drawLine(cxy(x0, y0) + QPointF(cell / 2, cell / 2), cxy(x1, y1) + QPointF(cell / 2, cell / 2))

        # 内墙: 只按"本格低四位"画 (x+1,y) 与 (x,y+1) 两条, 避免重复
        p.setPen(QPen(QColor("#e0e0e0"), 2.2))
        for x in range(n):
            for y in range(n):
                if not self._wall or x >= len(self._wall) or y >= len(self._wall[x]):
                    continue
                b = self._wall[x][y]
                pt = cxy(x, y)
                if not (b & WALL_UP_OPEN) and y + 1 < n:
                    p.drawLine(pt + QPointF(0, 0), pt + QPointF(cell, 0))
                if not (b & WALL_RIGHT_OPEN) and x + 1 < n:
                    p.drawLine(pt + QPointF(cell, 0), pt + QPointF(cell, cell))
                if not (b & WALL_DOWN_OPEN) and y - 1 >= 0:
                    p.drawLine(pt + QPointF(0, cell), pt + QPointF(cell, cell))
                if not (b & WALL_LEFT_OPEN) and x - 1 >= 0:
                    p.drawLine(pt + QPointF(0, 0), pt + QPointF(0, cell))

        # 起点与终点
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(129, 199, 132, 90)))
        p.drawEllipse(cxy(0, 0) + QPointF(cell / 2, cell / 2), cell * 0.16, cell * 0.16)
        p.setBrush(QBrush(QColor(229, 115, 115, 90)))
        p.drawEllipse(cxy(n - 1, n - 1) + QPointF(cell / 2, cell / 2), cell * 0.16, cell * 0.16)

        # 电脑鼠(三角, 指向朝向)
        px, py = self._pos
        if 0 <= px < n and 0 <= py < n:
            c = cxy(px, py) + QPointF(cell / 2, cell / 2)
            s = cell * 0.30
            ang = self._dir * 90.0  # 0=上, 1=右, 2=下, 3=左
            import math

            a = math.radians(ang)
            fwd = QPointF(math.sin(a), -math.cos(a))
            left = QPointF(-fwd.y(), fwd.x())
            tri = QPolygonF([c + fwd * s, c - fwd * s * 0.55 + left * s * 0.7, c - fwd * s * 0.55 - left * s * 0.7])
            p.setBrush(QBrush(QColor("#FFD54F")))
            p.setPen(QPen(QColor("#212121"), 1))
            p.drawPolygon(tri)

        # 标注
        p.setPen(QPen(QColor("#9e9e9e")))
        f = QFont()
        f.setPointSize(8)
        p.setFont(f)
        p.drawText(
            6,
            h - 6,
            f"({px},{py}) {_DIR_NAME.get(self._dir, '?')}  task={self._task}  {n}x{n}",
        )
