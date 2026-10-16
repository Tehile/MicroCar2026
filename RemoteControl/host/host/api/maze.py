"""迷宫: 墙图拉取/渲染 + **状态从统一帧读取**(实验8 无记忆走迷宫)。

对应功能清单的 H 组。

墙位编码(与固件 `mapWall[MAZETYPE][MAZETYPE]` 低四位一致, 置位 = 无墙/可通行):
    bit0 上  bit1 右  bit2 下  bit3 左

迷宫尺寸取自设备上报的 `mazetype`(当前固件为 7×7), 上位机不写死。

## v2.0(2026-10-05) 的重大简化

原先迷宫状态有三条通道: `MAZE EV` 事件流 / `LINK_TYPE_MAZE` 1Hz 状态帧 / `SNAP MAZE`
点查。**三条全部删除** —— 因为状态量已全部在统一帧里:
`mazeTask` / `posX,posY` / `wallLeft,Front,Right` / `goalX,goalY` / `stackIdx`(已废弃·恒 0) /
`motionTask`+`motionStep`。于是本 API 变成"读帧"而不是"攒事件":

    state()     从 `mc.telemetry.latest()` 直接取
    road()      用 `codec.classify_road()` 由三位墙原语在主机侧重算路口类型
    position()  (x, y, dir, mazeTask)

**代价**: 固件原先对 ROAD 的 8 类分类带 20ms 去抖; 主机侧复刻的 `classify_road()`
是**逐帧瞬时**判断, 需要在调用侧自行做去抖(见 `road_debounced()`)。

任务/事件的**迁移序列**不再由固件上报 —— 需要的话由主机差分相邻帧得到
(`task_changes()` 给出简易实现)。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from ..protocol.codec import (
    DIR_NAMES,
    MAZE_TASK_NAMES,
    MOTION_TASK_NAMES,
    ROAD_KINDS,
    ROAD_NAMES,
    SPURT_MODE_NAMES,
    WALL_DOWN_OPEN,
    WALL_LEFT_OPEN,
    WALL_RIGHT_OPEN,
    WALL_UP_OPEN,
    classify_road,
)
from .events import Event

if TYPE_CHECKING:  # pragma: no cover
    from .client import MouseClient

#: 路口大类 -> 是否算"岔路口"
JUNCTION_KINDS = ("TEE_FL", "TEE_FR", "TEE_LR", "CROSS")


class MazeAPI:
    """墙图 + 迷宫状态(从统一帧读取)。"""

    def __init__(self, mc: "MouseClient") -> None:
        self._mc = mc
        self._task_changes: List[Tuple[float, int, int]] = []
        self._prev_task: Optional[int] = None
        self._hook_off = None

    # ============================================================ 状态(从帧读)

    def _frame(self) -> Dict[str, Any]:
        return self._mc.telemetry.latest()

    def state(self) -> Optional[Dict[str, Any]]:
        """统一的迷宫视图(直接来自最新帧)。无数据时 None。"""
        f = self._frame()
        if not f:
            return None
        return {
            "task": int(f.get("mazeTask", 0)),
            "taskName": MAZE_TASK_NAMES.get(int(f.get("mazeTask", 0)), "?"),
            "dir": int(f.get("dir", 0)),
            "dirName": DIR_NAMES.get(int(f.get("dir", 0)), "?"),
            "posX": int(f.get("posX", 0)),
            "posY": int(f.get("posY", 0)),
            "goalX": int(f.get("goalX", 0)),
            "goalY": int(f.get("goalY", 0)),
            "wallLeft": int(f.get("wallLeft", 0)),
            "wallFront": int(f.get("wallFront", 0)),
            "wallRight": int(f.get("wallRight", 0)),
            # v2.3: 前墙拆成两档(互斥, 恒等式 near|far == wallFront)。
            # "有墙" 用 wallFront(任一档); "贴脸该处理" 用 frontNear。
            "frontNear": int(f.get("frontNear", 0)),
            "frontFar": int(f.get("frontFar", 0)),
            "frontBoth": int(f.get("frontBoth", 0)),
            "frontName": str(f.get("frontName", "无墙")),
            "motion": int(f.get("motionTask", 0)),
            "motionName": MOTION_TASK_NAMES.get(int(f.get("motionTask", 0)), "?"),
            "motionStep": int(f.get("motionStep", 0)),
            "stack": int(f.get("stackIdx", 0)),
            "fastwayCount": int(f.get("fastwayCount", 0)),
            "road": int(f.get("road", 0)),
            "roadName": str(f.get("roadName", "NONE")),
            "roadKind": ROAD_KINDS.get(str(f.get("roadName", "NONE")), "未判定"),
            "evDropped": bool(f.get("evq", False)),
        }

    def spurt(self) -> Optional[Dict[str, Any]]:
        """比赛冲刺状态(v2.3, 只读)。无数据时 None。

        这些量来自固件**只读镜像**(比赛固件 [5] 才有意义; 测试档不跑冲刺时
        恒为 `mode=0(未选)` 且各标志 0)。它们**不能**由上位机设置 ——
        冲刺入口是比赛流程里"人工贴墙选档"那一段(红线, 刻意不动)。
        """
        f = self._frame()
        if not f:
            return None
        mode = int(f.get("spurtMode", 0))
        return {
            "mode": mode,
            "modeName": SPURT_MODE_NAMES.get(mode, "?"),
            "spurtFlag": int(f.get("spurtFlag", 0)),
            "goOrBack": int(f.get("goOrBack", 0)),
            "spurtTestFlag": int(f.get("spurtTestFlag", 0)),
            "objectGoToFlag": int(f.get("objectGoToFlag", 0)),
            "goaheadnum": int(f.get("goaheadnum", 0)),
            "fastwayCount90": int(f.get("fastwayCount90", 0)),
        }

    def road(self) -> int:
        """当前路口类型码(由三位墙原语逐帧分类)。"""
        f = self._frame()
        if not f:
            return 0
        return classify_road(int(f.get("wallLeft", 0)), int(f.get("wallFront", 0)),
                             int(f.get("wallRight", 0)))

    def road_name(self) -> str:
        return ROAD_NAMES.get(self.road(), "NONE")

    def road_debounced(self, samples: int = 2, gap_s: float = 0.02) -> str:
        """连续 `samples` 次(每次间隔 `gap_s`)分类一致才认。复刻固件 20ms 去抖的意图。"""
        last = None
        same = 0
        for _ in range(max(1, samples)):
            k = self.road_name()
            if k == last:
                same += 1
            else:
                last, same = k, 1
            if same >= samples:
                return k
            time.sleep(gap_s)
        return last or "NONE"

    def position(self) -> Optional[Tuple[int, int, int, int]]:
        """(x, y, direction, mazeTask)。优先用墙图帧, 否则用统一帧。"""
        m = self.last()
        if m:
            return (int(m.get("posX", 0)), int(m.get("posY", 0)),
                    int(m.get("direction", 0)), int(m.get("mouseTask", 0)))
        f = self._frame()
        if f:
            return (int(f.get("posX", 0)), int(f.get("posY", 0)), int(f.get("dir", 0)),
                    int(f.get("mazeTask", 0)))
        return None

    # ------------------------------------------------------------ 任务迁移(差分)

    def _ensure_hooked(self) -> None:
        if self._hook_off is not None:
            return

        def _cb(msg: Any) -> None:
            f = msg.data.get("rec") or {}
            t = int(f.get("mazeTask", 0))
            if self._prev_task is not None and t != self._prev_task:
                self._task_changes.append((msg.ts, self._prev_task, t))
                if len(self._task_changes) > 2000:
                    del self._task_changes[:1000]
            self._prev_task = t

        self._hook_off = self._mc.on(Event.TELEMETRY, _cb)

    def task_changes(self, n: Optional[int] = None) -> List[Tuple[float, int, int]]:
        """迷宫任务的迁移序列 `(时刻, from, to)` —— 主机侧差分得到。"""
        self._ensure_hooked()
        return self._task_changes[-n:] if n else list(self._task_changes)

    def clear_changes(self) -> None:
        self._ensure_hooked()
        self._task_changes.clear()

    # ------------------------------------------------------------ 发车

    def run(self, target: str = "BLIND", timeout: float = 3.0):
        """远程发车(仅**远程档** `MOUSE_REMOTE`; 比赛固件回 ERR_VALUE)。

        `target` 决定发车后跑哪套剖面:

          · `"BLIND"`(默认) —— 无记忆走迷宫(实验八的 `LESSON_Blind8()`)
          · `"RACE"`  —— 正式比赛代码: 先读图选档/定冲刺, 再进主循环

        ⚠️ **发车前有现场安全检查**, 在登记表层(`registry._precheck_maze_run`),
        任何走 `send_cmd` 的路径都绕不开:

          · 电池过低(`battMv` < `registry.LAUNCH_MIN_MV`, 默认 7000 mV)时**拒绝发车**,
            返回 `ERR_LOWBATT`。依据是 2026-10-05 真机事故: 当时 `battMv` 一路
            7681 → 6747 → **3962 mV**, 现场表现为"电机一直转、且对红外障碍物不再
            敏感"。低电时红外发射管变暗 ⇒ 墙检测失效; 而**固件对低电没有任何保护
            动作**, 所以这道闸只能由上位机把。
          · 确需强行发车(台架调试)走逃生舱: `mc.send_raw("MAZE RUN")`。

        ⚠️ 另一条不变量: **发车前电脑鼠必须物理摆在起点角**。远程档的探索假设
        "我在 (0,0)", 内部位姿/地图与物理位置不一致时会按错图行驶(不认路也不认墙)。
        复位(软复位/断电)只复位固件、不复位物理位置。

        RACE 目标下还会重跑 `STATE_LoadMapAndSelectSpeed()` / `STATE_DetermineGoaheadNum()`,
        也就是**读 Flash 地图** —— 没有有效地图时固件会退化到默认图(起点格 0x11),
        两个分支都不进 ⇒ 车不动。所以 RACE 前请先确认设备里有一张好图。
        """
        mc = self._mc
        mc._check_reentrancy("maze.run()")
        return mc.send_cmd("maze.run", target=str(target or "BLIND").upper(), timeout=timeout)

    def stop(self, timeout: float = 3.0):
        """把电脑鼠**带出迷宫**, 复位发车状态并解锁急停（`MAZE STOP`）。

        为什么需要它: `MAZE RUN` 之后固件的主循环停在 `LESSON_Blind8()` 的 while(1)
        里, 光靠 `ESTOP` / `STOP` 只能停电机、停不掉那个循环 —— 于是发车状态无法
        复位, 再发 `MAZE RUN` 会一直回 `ERR_BUSY`（v2.1 之前的真机缺陷）。
        本命令让主循环返回等待态, 因此**不必断电就能重新发车**。

        与 `mc.move.estop()` 的分工: ESTOP 是"立刻停车 + 锁存", 本命令是
        "正常收车 + 复位发车状态 + 解锁急停"。两者都会把电脑鼠带出迷宫循环。
        """
        mc = self._mc
        mc._check_reentrancy("maze.stop()")
        return mc.send_cmd("maze.stop", timeout=timeout)

    # ============================================================ 汇总 / 人话

    def summary(self) -> Dict[str, Any]:
        st = self.state() or {}
        return {
            "running": bool(st),
            "task": st.get("taskName"),
            "road": st.get("roadName"),
            "roadKind": st.get("roadKind"),
            "opens": (st.get("wallLeft"), st.get("wallFront"), st.get("wallRight")) if st else None,
            "pos": (st.get("posX"), st.get("posY")) if st else None,
            "goal": (st.get("goalX"), st.get("goalY")) if st else None,
            "dir": st.get("dirName"),
            "stack": st.get("stack"),
            "motion": st.get("motionName"),
            "fastwayCount": st.get("fastwayCount"),
            "evDropped": bool(st.get("evDropped", False)),
            "taskSeq": [(a, b) for _t, a, b in self.task_changes(20)],
        }

    def explain(self) -> List[str]:
        s = self.summary()
        if not s["running"]:
            return ["（还没有迷宫状态数据: 先 maze.run 发车并开始拉取 mc.stream.on()）"]
        lines = [
            f"状态机   : 任务 {s['task']}  动作 {s['motion']}  朝向 {s['dir']}",
            f"路口     : {s['roadKind']}({s['road']})  开口(L,F,R)={s['opens']}  岔路栈 {s['stack']}",
            f"位置     : ({s['pos'][0]},{s['pos'][1]})   终点 ({s['goal'][0]},{s['goal'][1]})",
            f"快路径计数: {s['fastwayCount']}",
        ]
        if s["taskSeq"]:
            seq = " → ".join([str(s["taskSeq"][0][0])] + [str(t[1]) for t in s["taskSeq"]])
            lines.append("任务序列 : " + seq)
        if s["evDropped"]:
            lines.append("⚠️ 事件行有丢弃(链路拥塞): 状态帧仍连续, 但上行事件不完整")
        return lines

    # ------------------------------------------------------------ 拉取

    def fetch(self, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
        """请求并等待墙图。返回 `decode_map` 的结果。"""
        mc = self._mc
        mc._check_reentrancy("maze.fetch()")
        hits: List[Any] = []
        off = mc.on(Event.MAP, lambda m: hits.append(m))
        try:
            r = mc.send_cmd("maze.dump", timeout=5.0)
            if not r.ok and not hits:
                return mc.store.get_map()
            mc.wait(lambda: bool(hits), timeout=timeout)
        finally:
            off()
        if hits:
            return hits[0].data.get("rec")
        return mc.store.get_map()

    def last(self) -> Optional[Dict[str, Any]]:
        """最近一次收到的墙图(不发命令)。"""
        return self._mc.store.get_map()

    # ------------------------------------------------------------ 访问

    def mazetype(self) -> int:
        return int(self._mc.store.mazetype)

    def grid(self) -> Optional[List[List[int]]]:
        """墙位二维数组, `grid[x][y]`。"""
        m = self.last()
        return None if not m else m["mapWall"]

    def visited(self) -> Optional[List[List[bool]]]:
        m = self.last()
        return None if not m else m["mapVisited"]

    def cell(self, x: int, y: int) -> Optional[Dict[str, Any]]:
        """某一格的四方向通行情况。"""
        g = self.grid()
        if g is None:
            return None
        code = int(g[x][y])
        vis = self.visited()
        return {
            "x": x, "y": y, "code": code,
            "up": bool(code & WALL_UP_OPEN),
            "right": bool(code & WALL_RIGHT_OPEN),
            "down": bool(code & WALL_DOWN_OPEN),
            "left": bool(code & WALL_LEFT_OPEN),
            "visited": bool(vis[x][y]) if vis else None,
        }

    # ------------------------------------------------------------ 渲染

    def to_text(self, mark: Optional[Tuple[int, int]] = None) -> str:
        """渲染成 ASCII 迷宫。

        坐标约定: 列 = x(向右增大), 行 = y(向下增大), 因此 bit0「上」= y-1 方向。
        `mark` 给出当前位置, 会画成 `##`。标记缺省时用当前帧的坐标。
        """
        g = self.grid()
        if g is None:
            return "（还没有墙图数据, 先调 maze.fetch()）"
        n = self.mazetype()
        vis = self.visited()
        if mark is None:
            p = self.position()
            if p is not None and 0 <= p[0] < n and 0 <= p[1] < n:
                mark = (p[0], p[1])

        lines: List[str] = []
        for y in range(n):
            top = "+"
            for x in range(n):
                top += ("  " if (int(g[x][y]) & WALL_UP_OPEN) else "--") + "+"
            lines.append(top)
            mid = ""
            for x in range(n):
                left_open = bool(int(g[x][y]) & WALL_LEFT_OPEN)
                mid += (" " if left_open or x == 0 else "|")
                if mark is not None and (x, y) == mark:
                    mid += "##"
                elif vis is not None and vis[x][y]:
                    mid += "··"
                else:
                    mid += "  "
            mid += (" " if (int(g[n - 1][y]) & WALL_RIGHT_OPEN) else "|")
            lines.append(mid)
        bottom = "+"
        for x in range(n):
            bottom += ("  " if (int(g[x][n - 1]) & WALL_DOWN_OPEN) else "--") + "+"
        lines.append(bottom)
        return "\n".join(lines)

    def describe(self) -> str:
        """一行摘要: 尺寸、坐标、朝向、已探索格数。"""
        m = self.last()
        if not m:
            return "墙图: 未获取"
        n = self.mazetype()
        vis = self.visited()
        seen = sum(1 for col in vis for v in col if v) if vis else 0
        pos = self.position()
        return (
            f"墙图: {n}×{n}  坐标 ({pos[0]},{pos[1]}) 朝向 "
            f"{DIR_NAMES.get(pos[2], pos[2])}  已探索 {seen}/{n * n} 格"
        )
