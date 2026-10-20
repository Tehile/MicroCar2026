"""实验 8「无记忆走迷宫」的记录器 —— **从发车到停止**的一段完整数据。

## 它解决什么

跑迷宫这件事有个特点：**过程比结果重要**。跑完之后要回答的问题是
"它一路上识别到了什么、怎么决策的、在哪儿卡过" —— 而这些只能靠**过程数据**。
上游的环形缓冲只有 30 s（`store.py` 的 `window_s=30`），真机拉取只有个位数 Hz，
一盘迷宫往往几分钟 ⇒ **必须自己缓存**。

## 数据从哪来（一个容易踩错的点）

**订阅上游的 `Event.TELEMETRY`**，不是订阅本层的 `mouse.telemetry` 总线消息。
区别很要紧：

- 本层那条 `mouse.telemetry` 由 `sampler_loop` 发，而它**只在有浏览器订阅二进制帧时**才发
  （`if not self.bus._bin_subs: continue`）⇒ 记录器会**没有数据**，而且一声不响。
- 上游的 `Event.TELEMETRY` **每收到一帧就 emit 一次**（`client.py`），与有没有人看无关。

⇒ 记录器拿的是**每一帧**（真机个位数 Hz，模拟器可到十几 Hz），不是 10 Hz 的抽样。

## 墙图不存，**重建**

每帧都带 `wallLeft / wallFront / wallRight`（**相对车身朝向**）与 `posX / posY / dir`，
所以主机侧可以**随走随建**一张"我认为墙在哪"的图 —— 这正是界面中栏要的"重构示意"。
好处有三：

1. **零额外带宽**：不用反复 `maze.dump`（一次往返真机 ≈1.21 s，跑动中拉会打断）。
2. **随帧更新**：中栏能跟着车走，而不是几秒才刷一次。
3. **复盘不必复述存墙图**：重放帧序列就能**确定性**重建出与当时一模一样的过程图。
   存一份反而危险 —— 两份数据迟早会不一致，而那时没人知道该信哪个。

⚠️ 相对方向转绝对方向是这里唯一容易写错的地方：
`dir` 是 0=UP / 1=RIGHT / 2=DOWN / 3=LEFT（上游 `DIR_NAMES`），
"前墙"在 dir=1 时指的是**东**墙，不是北墙。转错了整张图会镜像/旋转，
而且**看起来仍然是一张合理的迷宫**（这类错最难发现）。

## 起止怎么圈定

- **开始**：点【发车】时由路由触发（`start()`）。
- **结束**（三个自动判据任一命中 + 人工兜底）：
  1. 收到上行 `STOPPED <reason>` —— 最直接，但**迷宫跑完不一定会发**；
  2. `mazeTask` **先离开 WAIT**（见到 ≥2 SEARCH）后又**回到 WAIT 并持续 `AUTO_END_S`**；
     "先离开"这个前提是必须的 —— 否则发车瞬间任务机还是 0，3 s 后就会误收尾（**空记录**）；
  3. 链路断开（`on_state` 非 connected）—— 不收尾就把整段丢了。
- **人工兜底**：【停止并保存】。**必须保留**，因为固件**没有"迷宫结束"事件**
  （`MAZE RUN` 之后就一直处于迷宫态）—— 自动判据是**弱判据**，只有模拟器/真机
  跑完那一刻才碰巧命中。
  ⚠️ 2.1.1 / 2.2.0 起"收尾"多了两个**正经动作**，但它们是**动车**、不是存盘，
  所以各自独立成按钮，**不并进本记录器**：
    · 【收车】`MAZE STOP` —— 停车 + 复位发车状态 + 解锁急停
      （2026-10-16 上游修好：正常 0.2~1 s 退出，爬不出来则 2 s 后固件自己软复位）；
    · 【复位】`MCU RESET` —— 整机软复位（连不上/卡死时用它；不复位物理位置）。
  ⇒ 记录器只管"把已经收到的东西存下来"，**不主动发任何命令**：
    这样"车还在跑时先把已收到的存一份"这件事才成立（现场真的需要）。

## 落盘

`<records_root>/<时间戳>_<mazetype>.maze.json`，**自包含**（单文件可拷走、可导入复盘）。
⚠️ 与"跨三路产物进会话目录"那条纪律的关系：迷宫记录是**鼠单路**的产物，
且要在**没开会话**时也能记（用户可能只跑一盘迷宫不想开全套会话），
所以它落独立目录；但**若会话开着，会在 timeline 里记一行**指向文件名，
这样"这一盘属于哪个会话"仍然查得到。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: 落盘格式号。**改结构就要改它** —— 导入端按它判断能不能读。
#: 为什么要个格式号：这个文件是要**跨时间**用的（今天的记录明天复盘、甚至换机器），
#: 而"读到一个不认识的字段就静默忽略"会让复盘结果悄悄少一块。
FORMAT = "control.maze-run/1"

#: 自动收尾：任务机回到 WAIT 之后再等这么久才认（秒）。
#: 为什么要等：迷宫搜索过程中任务机**会短暂回到 WAIT**（换段之间），
#: 立刻收尾会把一盘棋腰斩成好几段。3 s 是"比换段长、又比人等待短"的折中。
AUTO_END_S = 3.0

#: 收尾时至少要有的帧数。低于它就标 `tiny=true` —— 现实里这多半意味着
#: "发车没成功"或"拉取没开"，而不是"跑了一盘很短的迷宫"。
MIN_FRAMES = 10

#: 绝对方向（0=UP/北, 1=RIGHT/东, 2=DOWN/南, 3=LEFT/西）。
#: 用于把**相对车身**的三面墙转到绝对方向。
_DIR = {"UP": 0, "RIGHT": 1, "DOWN": 2, "LEFT": 3}

#: `dir` 值 → 前/左/右 三面墙各自的**绝对方向**。
#: ⚠️ 这张表是整套重建的正确性所在，所以写死并逐项列出来（比用三角函数推更难写错）。
#:   dir=0 UP    : 前=UP(0)    左=LEFT(3)   右=RIGHT(1)
#:   dir=1 RIGHT : 前=RIGHT(1) 左=UP(0)     右=DOWN(2)
#:   dir=2 DOWN  : 前=DOWN(2)  左=RIGHT(1)  右=LEFT(3)
#:   dir=3 LEFT  : 前=LEFT(3)  左=DOWN(2)   右=UP(0)
_FACE = {
    0: {"front": 0, "left": 3, "right": 1},
    1: {"front": 1, "left": 0, "right": 2},
    2: {"front": 2, "left": 1, "right": 3},
    3: {"front": 3, "left": 2, "right": 0},
}

#: 绝对方向 → (dx, dy)。⚠️ 这里的 y 是**落盘数组的行号**（`rows[y][x]`），
#: 与上游 `decode_map` 的行主序一致：行号向下增大。而**固件的格 y 向上增大**
#: （`mouseDirection=0`(UP) 时 `mousePos.cY += num` ⇒ 原点 (0,0) 在左下角），
#: 所以本表把 UP 映成 `dy=-1`（"上一行"）—— 即数组行号是**固件 y 的镜像**。
#: 这**不影响拓扑的正确性**（`_FACE`/`_STEP`/`_OPP` 三者自洽），只影响"数组长什么样"：
#: 显示层必须再翻一次（`draw.js` 的 `PY(y)=(n-1-y)`），才能让 (0,0) 落在画面左下、
#: 与"车摆在左下角、车头朝上"的现场约定对上。详见 `docs/上位机整合设计方案.md` §11.16。
_STEP = {0: (0, -1), 1: (1, 0), 2: (0, 1), 3: (-1, 0)}

#: 反向。用于"这道墙的另一面属于邻居格"。
_OPP = {0: 2, 1: 3, 2: 0, 3: 1}

#: 比赛终点区 —— 赛道固定的**中心 2×2 四格**（16×16 迷宫 ⇒ 索引 7 / 8）。
#:
#: ⚠️ 为什么是常量而不是从帧里读：上游 v9 把 `goalX` / `goalY` 整字节删了
#: （`RemoteControl/docs/通信协议规格.md` §9），终点在帧里**没有任何信号**。
#: 而终点位置本来就是**赛道属性**（由场地决定），不是设备状态 ⇒ 写在唯一一处，
#: 由 `GET /mouse/maze/reconstruct` 的 `goal_cells` 端给界面。
#: ⚠️ 前端**不许**自己再写一份同值的数组（铁律 32：一个元素只许一条真相来路）——
#: 两份各写一个坐标表 = "图上标了一个区、记录里认另一个区"的静默分裂。
#: ⚠️ 换赛道 = 只改这里；判据 I34 直接比对"记录判据用的集合"与"端点端出去的"。
GOAL_CELLS: Tuple[Tuple[int, int], ...] = ((7, 7), (7, 8), (8, 7), (8, 8))
_GOAL_SET = frozenset(GOAL_CELLS)


def _i(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


class MazeWallMap:
    """主机侧**随帧累积**的墙图重建。

    两个独立的事实分开记（混在一起就再也分不开了）：
      · `known[x][y][d]` —— 我**确认过**这一格这个方向（有墙或无墙）；
      · `wall[x][y][d]`  —— 确认**有墙**。
    只记"有墙"是不够的：没走过的方向 = **不知道**，而"不知道"与"确认没有"在界面上
    必须长得不一样（同本层的三态纪律：判不了 ≠ 不通过）。
    """

    #: 兜底边长。上游 2.11 换基线后迷宫 **7 → 16**（`MAP` 帧自适应）。
    #: ⚠️ 这只是"调用方没给尺寸时"的默认值；**真实尺寸永远取设备上报的 `mazetype`**
    #: （`mouse.maze_reconstruct` / `mouse.maze.mazetype()`）。给不出尺寸时
    #: `mouse.py` 的兜底是"不建图 + 如实报 None"，不是猜一个 7（那会画出错尺寸的图）。
    DEFAULT_N = 16

    def __init__(self, n: int = DEFAULT_N) -> None:
        self.n = int(n)
        self.reset()

    def reset(self) -> None:
        n = self.n
        self.known = [[[False] * 4 for _ in range(n)] for _ in range(n)]
        self.wall = [[[False] * 4 for _ in range(n)] for _ in range(n)]
        self.visited = [[False] * n for _ in range(n)]
        #: 走过的格子序列（去重相邻重复），用于画轨迹
        self.track: List[List[int]] = []

    def _set(self, x: int, y: int, d: int, has_wall: bool) -> None:
        """写一格的一个方向；**同时写邻居格的反向** —— 一道墙是两格共有的。

        这一步不是优化，是**正确性**：不写邻居的话，界面会画出"这道墙只有一半"
        （从 A 看有墙、从 B 看能通过），现场会当成识别错误。
        """
        n = self.n
        if not (0 <= x < n and 0 <= y < n):
            return
        self.known[x][y][d] = True
        self.wall[x][y][d] = bool(has_wall)
        dx, dy = _STEP[d]
        nx, ny = x + dx, y + dy
        if 0 <= nx < n and 0 <= ny < n:
            rd = _OPP[d]
            self.known[nx][ny][rd] = True
            self.wall[nx][ny][rd] = bool(has_wall)

    def feed(self, rec: Dict[str, Any]) -> None:
        """吃一帧统一帧记录，把这一格看到的三面墙写进图。"""
        if not rec:
            return
        n = self.n
        x, y = _i(rec.get("posX"), -1), _i(rec.get("posY"), -1)
        if not (0 <= x < n and 0 <= y < n):
            return
        d = _i(rec.get("dir"), 0) & 0x03
        f = _FACE.get(d)
        if f is None:
            return
        self.visited[x][y] = True
        if not self.track or self.track[-1] != [x, y]:
            # 轨迹太长就没用了（一盘迷宫几千帧）；240 个格子点足够画出走法
            if len(self.track) < 240:
                self.track.append([x, y])
        # 三面墙：值 1 = **有墙**（上游 `wallLeft/Front/Right` 的语义）
        for key, has in (("front", rec.get("wallFront")),
                         ("left", rec.get("wallLeft")),
                         ("right", rec.get("wallRight"))):
            if has is None:
                continue          # 这一路没给 ⇒ **不写**（"没读到"不是"没有"）
            self._set(x, y, f[key], bool(_i(has)))

    def to_json(self) -> Dict[str, Any]:
        return {
            "n": self.n,
            # 压成位串：`known` 用 4 bit 掩码、`wall` 用 4 bit 掩码，每格一个十六进制字符。
            # 为什么不用嵌套数组：n=7 时嵌套是 7×7×4 个布尔，JSON 里几百行、人也没法读。
            # 位掩码既短又**逐位可对**（`known` 的 bit3..0 依次是 UP/RIGHT/DOWN/LEFT）。
            "known_rows": ["".join(format(sum(1 << i for i in range(4)
                                            if self.known[x][y][i]), "x")
                                   for x in range(self.n)) for y in range(self.n)],
            "wall_rows": ["".join(format(sum(1 << i for i in range(4)
                                           if self.wall[x][y][i]), "x")
                                  for x in range(self.n)) for y in range(self.n)],
            "visited": ["".join("1" if self.visited[x][y] else "0"
                                for x in range(self.n)) for y in range(self.n)],
            "track": list(self.track),
        }

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "MazeWallMap":
        """从落盘形态还原（复盘时用它，或直接重放帧序列再建一张）。"""
        n = _i((d or {}).get("n"), 7)
        m = cls(n)
        try:
            kr = d.get("known_rows") or []
            wr = d.get("wall_rows") or []
            vs = d.get("visited") or []
            for y in range(min(n, len(kr))):
                for x in range(min(n, len(kr[y]))):
                    km = int(kr[y][x], 16)
                    wm = int(wr[y][x], 16) if y < len(wr) and x < len(wr[y]) else 0
                    for i in range(4):
                        m.known[x][y][i] = bool(km & (1 << i))
                        m.wall[x][y][i] = bool(wm & (1 << i))
                    m.visited[x][y] = bool(y < len(vs) and x < len(vs[y]) and vs[y][x] == "1")
            m.track = [[_i(p[0]), _i(p[1])] for p in (d.get("track") or [])
                       if isinstance(p, (list, tuple)) and len(p) >= 2]
        except Exception:                                     # noqa: BLE001
            # 解析不了就退回一张空图，**而不是抛** —— 复盘时"图画不出来"远好过
            # "整份文件读不进来"（帧数据才是主线，墙图只是它的一个视图）
            m.reset()
            m.broken = True
        return m


# ================================================================ 决策差分
#: 转弯类动作码（`motionTask`）。依据上游 `MOTION_TASK_NAMES`：这是"进入转弯/掉头动作"，
#: 也就是当年 `MAZE EV TURN <MOTION>` 报的东西。
_TURN_MOTION = frozenset({2, 4, 27, 28, 39, 42, 43, 46, 47,
                          101, 102, 103, 104, 105, 106, 107})
#: 直行类动作码。⚠️ **当年的事件流没有 `STRAIGHT` 这一条** —— 这是本层按现场要求
#: （"要看得见直行"）加的分类，判据同样取 `motionTask`，但**上游没有对应事件可对照**。
_STRAIGHT_MOTION = frozenset({1, 17, 30, 32, 33, 34, 44, 45, 49, 50, 51, 52})

#: `ROAD` 去抖：连续这么多帧分类一致才认。
#: 依据上游 `迷宫状态机回传.md` §4 —— 固件侧做 20 ms 去抖（连续 2 个采样一致），
#: 而主机侧复刻的 `classify_road()` 是**逐帧瞬时**判断，文档明确要求调用方自行去抖。
ROAD_DEBOUNCE_N = 2

#: 常驻决策环的容量（给"实时"那条线看，不落盘）。
#: 真机一盘 20 分钟的决策量在几百条量级，给 3000 足够；超了丢最老的。
DEC_RING = 3000

#: 常驻**事件**环的容量。上行事件（`STOPPED` / `ESTOP TRIP`）一盘也就几条到几十条，
#: 但它是"为什么停的"唯一的答案，所以照样要给右栏那条线供货 —— 见 `feed_event`。
EV_RING = 500


class MazeDecisions:
    """把统一帧序列差分成**决策事件**（右栏那条时间线）。

    ## 为什么决策必须由主机差分

    上游 v1.5.5 曾有一条 `MAZE EV <EVENT>` 事件流（`TASK`/`ROAD`/`JUNC`/`TURN`/`GOAL`/`CELL`）
    —— 那是"它识别到了什么"最直接的表达。**v2.0 把它整条删了**
    （`RemoteControl/docs/迷宫状态机回传.md` §3.1），理由与 `SNAP`/`SCOPE` 相同：
    状态量已全部在统一帧里。代价是"事件"这一层没了 —— 上游自己只在
    `MazeAPI.task_changes()` 里补了**任务机**一种差分，其余要调用方自己来。
    ⇒ 本类就是那个"其余"：把帧序列差分成与当年事件流**同构**的决策序列。

    ## 判据全部取帧里的返回值（**不许凭印象**）

    | kind | 判据（帧通道） | 上游依据 |
    |---|---|---|
    | `TASK` | `mazeTask` 变化 | 上游 `task_changes()` 同判据 |
    | `ROAD` | 路口分类变化，**仅 `task==SEARCH`** 且**连续 2 帧一致** | §4（固件只在决策态评估 + 20 ms 去抖） |
    | `TURN` | `motionTask` 进转弯类码 | `MOTION_TASK_NAMES`（当年 `MAZE EV TURN`） |
    | `STRAIGHT` | `motionTask` 进直行类码 | **本层加的**，当年事件流没有这一条 |
    | `CELL` | `(posX,posY)` 变化 | 当年 `MAZE EV CELL`（**默认关**，因为量最大） |
    | `HEAD` | `dir` 变化 | 帧里的 `dir`；"朝向变了"是**事实**，与"转向动作"不是一回事 |
    | `GOAL` | `(posX,posY)` 进 **`GOAL_CELLS`**（赛道固定的中心 2×2）**首次** | 赛道属性，见常量注释 |

    ⚠️ **`JUNC` 已下线（改版二十三）**：它原本靠 `stackIdx`（岔路栈深度）变化触发，
    而上游换基线那轮就明说"新固件无全局岔路栈索引 ⇒ 恒 0"，v9 顺手把那个恒 0 的字节
    也删了 ⇒ 判据 `st != prev` **恒假**，那条事件其实已经两周没产出过（因为取不到时
    `_i(...,0)` 让它安静地永不触发，没人发现）。⇒ 直接摘掉，**不拿别的量凑一个形似的
    替代**（铁律 35）。要恢复得等上游真端出岔路信息。

    ⚠️ **`GOAL` 现在不读任何帧字段**：v9 删了 `goalX/goalY`，原来"posX==goalX"的等值
    判定会静默失效（`_i(...,-2)` 恒 −2）。终点是**赛道属性**（由场地决定、与跑法无关）
    ⇒ 写成常量 `GOAL_CELLS`，口径与界面那张图**同一处来源**（端点端出去）。

    ## 码表从外面注入

    本模块**不 import 上游**（记录器要能在上游不可用时继续工作：帧已经收到了，就该能存下来）。
    所以 `road` / `motion` 的**名字**由适配器从上游码表构造后注入（`codes`）。
    注入不到时本类**如实降级**：只产不需要码表的四类（`TASK`/`CELL`/`HEAD`/`GOAL`），
    而不是自己编一份码表（编一份就会与上游漂移，且没人会发现）。
    """

    #: 会用到的码表接口（由适配器注入）。缺任何一项都不算"有码表"。
    NEEDED = ("classify_road", "road_name", "road_kind", "motion_name", "junction_kinds")

    def __init__(self, codes: Optional[Dict[str, Any]] = None) -> None:
        self.codes = codes if self._codes_ok(codes) else None
        self.reset()

    @classmethod
    def _codes_ok(cls, codes: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(codes, dict):
            return False
        return all(callable(codes.get(k)) or isinstance(codes.get(k), (list, tuple, set))
                   for k in cls.NEEDED)

    @property
    def with_codes(self) -> bool:
        """有没有码表 —— 界面上要把这件事说清楚（没有时 ROAD/TURN 那几类缺席）。"""
        return self.codes is not None

    def reset(self) -> None:
        self.items: List[Dict[str, Any]] = []
        self._prev: Optional[Dict[str, Any]] = None
        self._road: Optional[str] = None        # 去抖后的当前路口名
        self._road_run: Optional[str] = None    # 去抖计数用的"候选"
        self._road_n = 0
        self._motion: Optional[int] = None
        self._at_goal = False
        self._covered = False                   # 有没有见过"离开 WAIT"（用于 GOAL 的"首次"）

    # ------------------------------------------------------------ 逐帧

    def feed(self, rec: Dict[str, Any], t_ms: int) -> List[Dict[str, Any]]:
        """吃一帧，返回**这一帧新产生的**决策（0~2 条，通常 0 条）。"""
        if not isinstance(rec, dict) or not rec:
            return []
        task = _i(rec.get("mazeTask"), 0)
        x, y = _i(rec.get("posX"), 0), _i(rec.get("posY"), 0)
        d = _i(rec.get("dir"), 0)
        mo = _i(rec.get("motionTask"), 0)
        out: List[Dict[str, Any]] = []
        p = self._prev

        if p is None:
            # 第一帧：只播种，不产事件（否则时间轴开头会冒出一条"从 0 变到 X"的假迁移）。
            # 但"已在目标格"这个事实要记下来，不然下一帧会补报一次 GOAL。
            self._at_goal = self._is_goal(rec)
            self._motion = mo
            self._covered = task >= 2
            self._prev = rec
            self._seed_road(rec, task)
            return []

        # ---- TASK：任务机迁移（与上游 `task_changes()` 同一判据）
        pt = _i(p.get("mazeTask"), 0)
        if pt != task:
            out.append(self._mk(t_ms, "TASK", from_=pt, to=task, task=task))
        if task >= 2:
            self._covered = True

        # ---- ROAD：路口识别（**仅搜索决策态** + 2 帧去抖）
        # 上游 §4：固件只在 `task == SEARCH` 评估，因为"行驶途中侧向开口会一闪而过，
        # 全程评估会大量抖动"。我们照做，并把"当时在哪个任务态"记进事件里。
        if self.codes is not None:
            nm = self._classify(rec)
            if task == 2:
                if nm == self._road_run:
                    self._road_n += 1
                else:
                    self._road_run, self._road_n = nm, 1
                # `NONE` 是"还没判定"，不是一种路口 ⇒ 不算一条决策。
                if self._road_n >= ROAD_DEBOUNCE_N and nm != "NONE" and nm != self._road:
                    # ⚠️ 字段名用 `roadKind`（与上游 `state()['roadKind']` 同名）而不是
                    #    `kind` —— 后者已经被 `_mk` 用来表示**事件种类**了，
                    #    同名会让"这条决策是哪一类"和"路口是哪一类"混成一个键。
                    out.append(self._mk(t_ms, "ROAD", road=nm, roadKind=self._kind(nm),
                                        task=task, x=x, y=y, dir=d))
                    self._road = nm
            else:
                self._road_run, self._road_n = nm, 0

        # ---- TURN / STRAIGHT：动作码进出（**转向**与**直行**）
        # 只在**跨越类边界**时报（进转弯报一次、进直行报一次），不是每帧都报 ——
        # 否则一盘下来几百条"直行"，把真正的决策淹掉。
        if self.codes is not None and mo != self._motion:
            was_t = self._motion in _TURN_MOTION
            was_s = self._motion in _STRAIGHT_MOTION
            if mo in _TURN_MOTION and not was_t:
                out.append(self._mk(t_ms, "TURN", motion=mo,
                                    motionName=self._mname(mo), x=x, y=y, dir=d))
            elif mo in _STRAIGHT_MOTION and not was_s:
                out.append(self._mk(t_ms, "STRAIGHT", motion=mo,
                                    motionName=self._mname(mo), x=x, y=y, dir=d))
        if mo != self._motion:
            self._motion = mo

        # ---- CELL：进入新格（当年 `MAZE EV CELL <x> <y> <dir>`）
        if (x, y) != (_i(p.get("posX"), 0), _i(p.get("posY"), 0)):
            out.append(self._mk(t_ms, "CELL", x=x, y=y, dir=d))

        # ---- HEAD：朝向变化（"它转向了"的最终结果；与 TURN 动作不是一回事）
        if d != _i(p.get("dir"), 0):
            out.append(self._mk(t_ms, "HEAD", dir=d, prev=_i(p.get("dir"), 0), x=x, y=y))

        # ---- GOAL：首次进入终点区（**只报一次**）
        # ⚠️ 判据是常量（`GOAL_CELLS`），不读帧里的 `goalX/goalY` —— 那两个字节
        #    v9 已删，读出来恒 0（详见常量注释）。
        g = self._is_goal(rec)
        if g and not self._at_goal:
            out.append(self._mk(t_ms, "GOAL", x=x, y=y))
        self._at_goal = g

        self._prev = rec
        self.items.extend(out)
        return out

    # ------------------------------------------------------------ 判据辅助

    def _classify(self, rec: Dict[str, Any]) -> str:
        fn = self.codes.get("classify_road")
        nm = self.codes.get("road_name")
        try:
            return str(nm(int(fn(_i(rec.get("wallLeft"), 0), _i(rec.get("wallFront"), 0),
                                _i(rec.get("wallRight"), 0)))))
        except Exception:                                     # noqa: BLE001
            return "NONE"

    def _kind(self, road_name: str) -> str:
        try:
            return str(self.codes.get("road_kind")(road_name))
        except Exception:                                     # noqa: BLE001
            return ""

    def _mname(self, code: int) -> str:
        try:
            return str(self.codes.get("motion_name")(code))
        except Exception:                                     # noqa: BLE001
            return str(code)

    def is_junction(self, road_name: str) -> bool:
        jk = self.codes.get("junction_kinds") if self.codes else None
        return bool(jk and road_name in jk)

    def _seed_road(self, rec: Dict[str, Any], task: int) -> None:
        """播种路口去抖状态（第一帧不产事件）。"""
        if self.codes is None:
            return
        nm = self._classify(rec)
        self._road_run, self._road_n = nm, (ROAD_DEBOUNCE_N if task == 2 else 0)
        self._road = nm if task == 2 else None

    def _is_goal(self, rec: Dict[str, Any]) -> bool:
        """是否在**终点区**里（赛道固定的中心 2×2 四格，见 `GOAL_CELLS`）。

        ⚠️ 历史：v9 之前这里读帧里的 `goalX/goalY` 做等值判定。v9 把那两个字节删了
        ⇒ `_i(..., -2)` 恒 −2、永不等于 `posX` ⇒ GOAL 事件**静默消失**（不报错、
        只是少一类记录）。现在判据**不依赖任何帧字段**，位置取不到时按"不在终点"处理。
        """
        x = _i(rec.get("posX"), -1)
        y = _i(rec.get("posY"), -1)
        return (x, y) in _GOAL_SET

    def _mk(self, t_ms: int, kind: str, **kw: Any) -> Dict[str, Any]:
        d: Dict[str, Any] = {"t_ms": int(t_ms), "kind": kind}
        for k, v in kw.items():
            d["from" if k == "from_" else k] = v
        return d


def rebuild_decisions(doc: Dict[str, Any],
                      codes: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """**从记录文件里的帧序列重放**出决策序列。

    为什么要有这条：差分逻辑会随现场需求演进，而**旧记录文件里的 `decisions`
    是当时那份逻辑的产物**。重放帧总能得到"用现在这份逻辑算出来的"决策 —— 于是
    "旧文件看不出新事件"不是死路。另外它也是拿到**没有 `decisions` 字段**的老文件
    （本字段是本版新增）时唯一的路。

    这也顺带证明了记录格式的自包含性：帧在，决策就在。
    """
    ch = doc.get("channels") or []
    t = doc.get("t_ms") or []
    rows = doc.get("frames") or []
    dec = MazeDecisions(codes)
    out: List[Dict[str, Any]] = []
    for i, row in enumerate(rows):
        try:
            rec = {c: row[j] for j, c in enumerate(ch) if j < len(row)}
        except Exception:                                     # noqa: BLE001
            continue
        out.extend(dec.feed(rec, _i(t[i] if i < len(t) else 0)))
    return out


class MazeRecorder:
    """记录器本体。线程契约：`feed*` 由**上游调度线程**调用，其余由 HTTP 线程池调用。"""

    #: 帧数上限（保险丝）。真机个位数 Hz，一盘 20 分钟也才 ~6000 帧；
    #: 给到 20 万帧是为了"万一有人开着不关"，而不是鼓励长跑。
    MAX_FRAMES = 200_000

    def __init__(self, root: Path, get_channels=None,
                 codes: Optional[Dict[str, Any]] = None,
                 get_link_stats=None) -> None:
        self.root = Path(root)
        self._get_channels = get_channels or (lambda: [])
        # 完整性差量的取数（可缺）：回**上位机本地**链路统计 dict（含
        # `seq_lost` / `bad_crc`）。为什么必须从外面拿，见 `_probe_losses`。
        self._get_link_stats = get_link_stats
        self._lock = threading.RLock()
        self._rec: Optional[Dict[str, Any]] = None      # 在记的一段（None = 没在记）
        self.start_ms: Optional[int] = None
        self._last: Optional[Dict[str, Any]] = None     # 最近一次的收尾摘要
        self._maps: Dict[str, MazeWallMap] = {}         # 复盘缓存：name → 重建图
        # 决策差分的**码表**（上游的名字表与路口分类器）——由适配器注入。
        # 注入不到时 `MazeDecisions` 会如实降级（只产不需要码表的那几类），
        # 而不是自己编一份码表（编一份必然与上游漂移）。见其 docstring。
        self._codes = codes
        # ---- **常驻**决策差分器：**不管在不在记录**都跟着帧走。
        # 为什么不能只在 recording 时算：发车之前、或收尾之后，用户仍会盯着右栏看
        # "它刚才做了什么"（"停车后数据仍保持实时"）。只在 recording 时算的话，
        # 那两段时间右栏就是空的 —— 而它本来有内容。
        self._dec = MazeDecisions(codes)
        self._dec_ring: List[Dict[str, Any]] = []
        self._dec_t0: Optional[int] = None
        #: 常驻环零点的**语义**：`"launch"` = 发车那一刻（`start()` 设过零点）；
        #: `"rx"` = 只是开始收到帧（还没发过车）。界面据此说"发车后 x s"还是
        #: "从收帧起 x s" —— **两个数都不假，但含义不同**，不写清楚会让人以为跑了 25 分钟。
        self._dec_t0_kind: str = "rx"
        self._dec_prev_ms: Optional[int] = None
        #: 常驻**上行事件**环。与决策环**同一个纪元**（`start()` 时一起清）——
        #: 界面上那条时间线只有一条来路：`/maze/decisions` 同时给出 `decisions` 与
        #: `events`。以前事件是前端自己攒的（`evs`），于是"同一条线"由两个来源拼起来，
        #: 刷新一次页面就少一半（见 `feed_event` 的说明）。
        self._ev_ring: List[Dict[str, Any]] = []
        #: 开新的一段时把上一段存这儿，好让**发车失败**能整体放回去（见 `restore_epoch`）。
        self._dec_stash: Optional[tuple] = None
        #: 可选日志钩子（`bus.log`）。差分失败要**进日志**，不能一声不响 ——
        #: 那会让右栏"悄悄变空"，而看的人以为是"这一段本来就没决策"。
        self._log: Any = None

    # ------------------------------------------------------------ 码表（可后补）

    def set_codes(self, codes: Optional[Dict[str, Any]]) -> None:
        """补上（或换掉）决策差分的码表。

        适配器装载上游的时机与本对象构造的时机不一定相同（本对象在 `ctx` 早期就建，
        而上游 `host.api` 可能那时还没探到）。补上之后**立即生效**，且把常驻环清掉 ——
        否则环里会混着"没有码表时算出来的"那几条，两类事件的密度完全不同。
        """
        with self._lock:
            dec = MazeDecisions(codes)
            if not dec.with_codes:
                # 给的是空码表：**不动**现有的（别把一个能用的差分器降级掉）
                return
            self._codes = codes
            self._dec = dec
            self._dec_ring = []
            self._ev_ring = []
            self._dec_t0 = None
            self._dec_t0_kind = "rx"
            self._dec_prev_ms = None

    # ------------------------------------------------------------ 完整性（本机丢帧计数）

    def _probe_losses(self) -> Optional[Dict[str, Optional[int]]]:
        """读**上位机本地**的丢帧计数（`stats()["seq_lost"]` / `bad_crc`）。

        ⚠️ 这**不是**设备侧的 `dropCnt`（TX 环写失败计数）—— 上游 v9 已删那个通道，
        召回路径（低频诊断）也没落地，现在真拿不到。本机这份是**按 seq 缺口推算的
        上行丢失条数**（帧 + 事件共用一个 1 字节序号，见上游 `model/ring.py`），
        语义比设备计数**窄一圈**：分不清"设备没发出来"与"链路没送到"，
        只回答"主机少收到了几条"。**不冒充设备计数**（§11.24 第二组 #3）。

        ⚠️ 为什么不在 `feed()` 里自己比帧 seq：事件行与帧**共用**同一计数器，
        中间还夹着 TEXT/PONG 等上行消息 —— 记录器只见其中两类，自己比会把
        "正常的交织"误报成丢帧。上游 `stats()["seq_lost"]` 是唯一把**全部**上行
        消息都算进去的计数（铁律 32：一个量只许一条真相来路）。

        取不到（没注入 / 没连接 / 上游没这个键）回 `None` —— **没探到 ≠ 没丢**。
        """
        fn = self._get_link_stats
        if fn is None:
            return None
        try:
            st = fn()
        except Exception:                                    # noqa: BLE001
            return None
        if not isinstance(st, dict):
            return None
        out: Dict[str, Optional[int]] = {}
        for k in ("seq_lost", "bad_crc"):
            v = st.get(k)
            try:
                out[k] = None if v is None else int(v)
            except (TypeError, ValueError):
                out[k] = None
        if out["seq_lost"] is None and out["bad_crc"] is None:
            return None
        return out

    # ------------------------------------------------------------ 纪元（一段的边界）

    def _reset_epoch(self, kind: str = "launch", stash: bool = True) -> None:
        """**开新的一段**：把常驻决策环与事件环都清掉，并换一个全新的差分器。

        ⚠️ 为什么必须"清环"而不只是"挪零点"（这是本轮修的 bug）：
        常驻环的零点是 `_dec_t0`。若只把零点挪到发车时刻、环不清，那么
        **上一段**（以及**发车之前**那些帧）的决策会原封不动留在环里，相对新零点
        就成了**负时间** —— 界面上出现"‑14.2s 转向"，而回放那份（永远是单段）
        里没有这些条目 ⇒ 用户看到的是"回放读到的和退出回放的完全不一样"。

        零点也**不在这里钉**，而是留给"发车后的**第一帧**"（`feed()` 里）——
        与落盘那份的 `t0`（也是第一帧的到达时刻）**是同一个数**，于是
        实时线与回放线**逐条数值相等**（不只是"看着差不多"）。
        """
        if stash:
            self._dec_stash = (self._dec, self._dec_ring, self._dec_t0,
                               self._dec_t0_kind, self._ev_ring)
        self._dec = MazeDecisions(self._codes)
        self._dec_ring = []
        self._ev_ring = []
        self._dec_t0 = None
        self._dec_t0_kind = str(kind)
        self._dec_prev_ms = None

    def restore_epoch(self) -> bool:
        """把上一次 `_reset_epoch()` 之前的常驻状态**整体放回去**。

        用途只有一个：**发车失败**。`_timesync_maze_run` 是"先开记录再发车"，
        发车被拒时那条空记录会被丢掉 —— 既然"这一段根本没开始"，常驻环也该跟着回去，
        否则一次没开成的发车会把右栏**正在看的那一段**抹掉（用户会以为数据丢了）。
        """
        with self._lock:
            st = self._dec_stash
            if st is None:
                return False
            (self._dec, self._dec_ring, self._dec_t0,
             self._dec_t0_kind, self._ev_ring) = st
            self._dec_stash = None
            return True

    # ------------------------------------------------------------ 状态

    @property
    def recording(self) -> bool:
        with self._lock:
            return self._rec is not None

    def auto_end_reason(self) -> Optional[str]:
        """该收尾了吗？返回原因（`stopped_event` / `back_to_wait` / `link_down` /
        `max_frames`），不该收尾时返回 `None`。

        ⚠️ 这个方法在**事件回调之外**被轮询（`sampler_loop`），所以它只读标记、不做 IO ——
        真正的落盘由调用方走线程池。分开的原因是回调跑在上游调度线程上（见模块 docstring）。
        """
        with self._lock:
            r = self._rec
            return None if r is None else r.get("auto_end")

    def status(self) -> Dict[str, Any]:
        """给界面的一行状态。**不发命令、零设备访问。**"""
        with self._lock:
            r = self._rec
            out = {
                "recording": r is not None,
                "frames": (len(r["frames"]) if r else 0),
                "duration_ms": (int(time.time() * 1000) - self.start_ms
                                if (r and self.start_ms) else 0),
                "last": self._last,
            }
            if r is not None:
                out["why"] = r.get("why")
                out["mazetype"] = r.get("mazetype")
            return out

    # ------------------------------------------------------------ 起

    def start(self, why: str = "manual", mazetype: Optional[int] = None) -> Dict[str, Any]:
        """开始记录。**已在记就返回现状而不是重置** —— 重复点【发车】不该把前一段抹掉。

        返回值里的 `fresh_epoch` 很要紧：`True` 表示**这一次调用真的新开了一段**
        （常驻决策环已被清成新纪元）。调用方（`_timesync_maze_run`）靠它判断
        "发车被拒时该不该把这一段丢掉、把环放回去" —— 回 `already` 的那次
        **什么都没动**，绝不能回滚（那会把一段正在跑的数据换成更早的旧环）。
        """
        with self._lock:
            if self._rec is not None:
                return {"ok": False, "already": True,
                        "frames": len(self._rec["frames"]),
                        "note": "已经在记录中"}
            self._maps.clear()
            self.start_ms = int(time.time() * 1000)
            # 常驻决策环/事件环：**清空**并换一个全新的差分器 —— 见 `_reset_epoch`
            # 的 docstring（只挪零点不清环会让上一段的决策以**负时间**残留在界面上，
            # 而回放那份是单段的 ⇒ 两条来路看起来"完全不一样"）。
            # 零点**不在这里钉**：交给"发车后的第一帧"，与落盘那份的 `t0` 同源。
            self._reset_epoch("launch")
            self._rec = {
                "why": str(why or "manual"),
                "mazetype": (None if mazetype is None else _i(mazetype, 7)),
                "frames": [],           # [(t_ms, rec), …]
                "events": [],
                "changes": [],
                # 决策序列（右栏时间线的数据源）。与 `changes` 的区别：
                # `changes` 只有任务机迁移，而这里是**超集**（含 ROAD/TURN/STRAIGHT/
                # JUNC/CELL/HEAD/GOAL）。见 `MazeDecisions` 的 docstring。
                "decisions": [],
                # 这一段**自己的**差分器与零点 —— **不复用常驻那个**。
                # 虽然 `start()` 已经把常驻环清成同一纪元（两者现在会算出同一串），
                # 但常驻环是**环形缓冲**（超 `DEC_RING` 就丢最老的），而落盘那份必须
                # 是**完整**的一段 ⇒ 不能拿环当"这一段"的数据源。
                "_dec": MazeDecisions(self._codes),
                "left_wait": False,     # 见 AUTO_END_S 的说明
                "wait_since_ms": None,
                "evq_seen": False,
                # 这一段开头的**本机**丢帧计数快照（v9 起设备侧 `dropCnt` 没了）。
                # 收尾时再读一次做差量 —— 见 `_probe_losses`。取不到就是 None。
                "drop_base": self._probe_losses(),
                "auto_end": None,
            }
            return {"ok": True, "started_ms": self.start_ms, "fresh_epoch": True}

    # ------------------------------------------------------------ 喂

    def feed(self, rec: Dict[str, Any], t_dev_ms: Optional[int] = None) -> None:
        """吃一帧。**由上游调度线程调用** ⇒ 只做最小动作（append + 两个判据），不落盘、不动 IO。

        ## 时间轴用**到达时刻**，不用"帧发生时刻"

        这是个容易反着做的地方，所以写清楚：`STOPPED` 事件与任务迁移**只能**在
        到达时才被知道（没法回溯），所以它们天然是"到达时基"。
        如果帧用"帧发生时刻"（靠 timesync 锚算）、事件用到达时刻，同一份文件里就
        **混了两种时基** —— 复盘时"事件插在帧序列的哪一帧"会系统性偏移
        （偏移量正好是链路时延，几百 ms，看起来却像"状态机反应慢"）。

        ⇒ 统一用到达时刻。需要设备时基时**不会丢**：帧里本来就有 `ms8`（通道列），
        而"帧发生时刻"另存一列 `t_dev_ms`（**独立列，不参与排序**）。

        `t_dev_ms` 给定时会记进 `dev_t` 列，用于"链路时延到底多大"这类分析。
        """
        if not isinstance(rec, dict) or not rec:
            return
        now_ms = int(time.time() * 1000)
        # ---- ① **常驻**决策差分：**不管在不在记录**都跑（右栏在收尾之后仍要有内容）。
        #     环零点在**本纪元的第一次 feed** 时钉住：
        #       · 没发过车 ⇒ 就是"开始收到帧"（`rx`）；
        #       · 发过车   ⇒ 就是"**发车后的第一帧**"（`launch`，由 `start()` 置好语义）。
        #     后者与落盘那份的 `t0`（同样取第一帧的到达时刻）**是同一个数**
        #     ⇒ 实时线与回放线逐条数值相等，而不是"看着差不多"。
        if self._dec_t0 is None:
            self._dec_t0 = now_ms
            if self._dec_t0_kind != "launch":
                self._dec_t0_kind = "rx"
        try:
            # ⚠️ 差分器**一律吃绝对毫秒**（与 `changes` / `events` 完全同源），
            #    相对化统一留到"出门"那一刻做（落盘减 `t0`、实时输出减环零点）。
            #    曾经把"算的时候就用相对值"写进设计里 —— 那会让同一份数据在
            #    两条出口上各减一个不同的零点，复盘时事件整体错位几百 ms，
            #    而图看着还是对的（`feed()` 的 docstring 里记过这个坑）。
            for it in self._dec.feed(rec, now_ms):
                self._dec_ring.append(it)
            if len(self._dec_ring) > DEC_RING:
                del self._dec_ring[:len(self._dec_ring) - DEC_RING]
        except Exception as e:                                # noqa: BLE001
            # 决策差分失败**绝不能影响记录**（帧才是主线，决策是它的一个视图）。
            # 但也不能一声不响 —— 首次失败进日志。
            if not getattr(self, "_dec_failed", False):
                self._dec_failed = True
                if self._log:
                    self._log("warn", f"实验 8 决策差分器失败（后续同类不再重复报）："
                                      f"{type(e).__name__}: {e}")
        self._dec_prev_ms = now_ms
        with self._lock:
            r = self._rec
            if r is None:
                return
            frames = r["frames"]
            if len(frames) >= self.MAX_FRAMES:
                r["auto_end"] = "max_frames"
                return
            if self.start_ms is None or not frames:
                # 第一帧**才是**这条时间轴的零点 —— 不是"点发车那一刻"。
                # 两者差着"发车命令往返 + 第一帧到达"（真机加起来能到 1 s 以上），
                # 用后者会让时间轴开头凭空多出一段空白。
                self.start_ms = now_ms
            frames.append((now_ms, rec, (None if t_dev_ms is None else int(t_dev_ms))))
            # ---- 完整性：EVQ 要**如实记**（不是告警，是"这段数据完不完整"）。
            # ⚠️ 丢帧**不在这里逐帧读**：设备侧的 `dropCnt` v9 已删（召回也没落地），
            #    改在收尾时按**本机** `seq_lost` 差量算 —— 语义与不自己比 seq 的
            #    理由都写在 `_probe_losses` 的 docstring 里。
            if rec.get("evq"):
                r["evq_seen"] = True
            # ---- 决策：这一段自己的差分器（**绝对毫秒**，落盘时再减零点）
            # ⚠️ **不在常驻环里顺手切片**是有意的：常驻环的零点是"开始收到帧"，
            #    与这一段的零点不同（差着发车前的那些帧）⇒ 用环去凑会整体错位。
            try:
                for it in r["_dec"].feed(rec, now_ms):
                    r["decisions"].append(it)
            except Exception as e:                            # noqa: BLE001
                if not r.get("_dec_failed"):
                    r["_dec_failed"] = True
                    if self._log:
                        self._log("warn", f"实验 8 记录内决策差分失败："
                                          f"{type(e).__name__}: {e}")
            # ---- 自动收尾判据②：先离开 WAIT，再回到 WAIT 并持续 AUTO_END_S
            task = _i(rec.get("mazeTask"), 0)
            if task >= 2:
                r["left_wait"] = True
                r["wait_since_ms"] = None
            elif task <= 1 and r["left_wait"]:
                if r["wait_since_ms"] is None:
                    r["wait_since_ms"] = now_ms
                elif (now_ms - r["wait_since_ms"]) >= AUTO_END_S * 1000:
                    r["auto_end"] = "back_to_wait"
            # ---- 任务迁移（与上游 `task_changes` 同一判据，但**记进文件**）
            # 这条仍保留：`changes` 是**老字段**，外部工具/旧界面在读它（`_timeline` 也用它）。
            # `decisions` 里的 `TASK` 是同一件事的**超集表达**（带更多上下文）。
            prev = frames[-2][1].get("mazeTask") if len(frames) >= 2 else None
            if prev is not None and _i(prev, 0) != task:
                r["changes"].append({"t_ms": now_ms, "from": _i(prev, 0), "to": task})

    def feed_event(self, d: Dict[str, Any]) -> None:
        """吃一条**上行事件**（`STOPPED` / `ESTOP TRIP`）。

        为什么它们必须进记录：`reason` **从帧里推不出来** ——
        "时间护栏到"与"红外护栏触发"在帧里长得一样（同 §11.9）。
        复盘时想知道"它为什么停的"，只有这一处能答。

        ⚠️ 同时也要进**常驻环**（`_ev_ring`），不只是进记录。以前界面上那条时间线的
        事件是**前端自己攒的**（浏览器里一个 `evs` 数组），于是它有两个毛病：
          · 只有"这个页面开着"的那段时间的事件 —— **刷新一次就全没了**；
          · 与"决策"那条线**零点不同、来路不同**，两条拼在一条线上必然对不齐。
        ⇒ 现在与决策**同一个纪元、同一个零点**，由 `/maze/decisions` 一起端出去。
        """
        if not isinstance(d, dict) or not d:
            return
        now_ms = int(time.time() * 1000)
        ev = {
            "t_ms": now_ms,
            "event": str(d.get("event") or ""),
            "reason": str(d.get("reason") or ""),
            # ⚠️ `reason_text` 是上游给的**人话**（"时间护栏到" / "近墙保护(红外护栏)" …，
            #    见上游 `codec.RUN_REASON_NAMES`）。界面右栏直接写它 ——
            #    只丢一个 `TIME` / `IR` 码过去，现场得翻文档才知道"为什么停的"，
            #    而"为什么停"恰恰是这一条存在的唯一理由。老上游若没给，
            #    退回 `reason` 码本身（总比空白强，且不编造人话）。
            "reason_text": str(d.get("reason_text") or d.get("reason") or ""),
            "text": str(d.get("text") or ""),
            "seq": d.get("seq"),
            "status": str(d.get("status") or ""),
        }
        with self._lock:
            self._ev_ring.append(ev)
            if len(self._ev_ring) > EV_RING:
                del self._ev_ring[:len(self._ev_ring) - EV_RING]
            r = self._rec
            if r is None:
                return
            r["events"].append(ev)
            # 判据①：STOPPED 是最直接的收尾信号
            if str(d.get("event") or "") == "STOPPED":
                r["auto_end"] = r["auto_end"] or "stopped_event"

    def feed_link_down(self) -> None:
        """链路断开 ⇒ 收尾（判据③）。不收尾就把整段丢了。"""
        with self._lock:
            r = self._rec
            if r is not None:
                r["auto_end"] = r["auto_end"] or "link_down"

    def note_map(self, m: Optional[Dict[str, Any]]) -> None:
        """记一份**固件自述的墙图**（对账用，可选）。

        ⚠️ 刻意**不把它当主数据**：主数据是主机的随帧重建（零带宽、跟得上车）。
        固件这份只在结束时（或人点【读取墙图】时）拉一次，用来回答
        "主机重建得对不对"——那是真机要验的一条（`classify_road` 复刻的同源问题）。
        """
        if not m:
            return
        with self._lock:
            r = self._rec
            if r is None:
                return
            r["firmware_map"] = {
                "pos": [m.get("posX"), m.get("posY")],
                "dir": m.get("direction"),
                "mazetype": m.get("mazetype"),
                "grid": m.get("mapWall"),
                "visited": m.get("mapVisited"),
            }

    # ------------------------------------------------------------ 止

    def stop(self, why: str = "manual", firmware_map: Optional[Dict[str, Any]] = None,
             text: Optional[str] = None) -> Dict[str, Any]:
        """收尾并落盘。返回摘要（含文件名）。"""
        with self._lock:
            r = self._rec
            if r is None:
                return {"ok": False, "note": "当前没有在记录"}
            self._rec = None

        # ---- 落盘（**在锁外**做 IO：调度线程可能正等锁，别让它等磁盘）
        # ⚠️ `firmware_map` 只作**对账基准**，可以缺（跑动中不一定会拉墙图）。
        # 主数据是主机按帧重建的那张图，见模块 docstring 的"墙图不存，重建"。
        frames: List = r["frames"]                 # [(到达ms, rec, 帧发生ms|None), …]
        if not frames:
            # **一帧都没有 ⇒ 拒绝落盘**：这种文件对复盘毫无价值（没有可重放的东西），
            # 只会留一个"看着像记录"的垃圾。而且它多半意味着"发车没成功 / 拉取没开"，
            # 那是**要现场解决的问题**，不是一个文件能记下来的。
            # ⇒ 明确回 ok=False + 一句可照做的话，让界面把它当"没记成"来显示。
            # （1~9 帧**仍然写**，只是标 `tiny` —— 那是"真跑了一小段"与"没跑起来"的区分。）
            with self._lock:
                self._last = {"ok": False, "frames": 0, "end_why": why,
                              "note": "一帧都没收到，没有可记录的内容（未写文件）"}
            return dict(self._last)
        doc = self._build_doc(r, why=why, firmware_map=firmware_map, text=text)

        path = self._write(doc)
        summary = {
            "ok": True, "name": path.name, "path": str(path),
            "frames": len(frames), "duration_ms": doc["duration_ms"],
            "end_why": why, "auto_end": r.get("auto_end"),
            "tiny": doc["integrity"]["tiny"],
            "evq_seen": doc["integrity"]["evq_seen"],
            "drop_seen": doc["integrity"]["drop_seen"],
            "seq_lost_delta": doc["integrity"]["seq_lost_delta"],
            "decisions": len(doc.get("decisions") or []),
        }
        with self._lock:
            self._last = summary
        return summary

    def snapshot(self) -> Optional[Dict[str, Any]]:
        """**当前这一段**的 doc（与落盘那份**同一形状**），**不落盘、不改状态**。

        用途只有一个：让用户**在跑的过程中**就能回放刚才那一段。
        为什么必须与落盘那份同形状：界面只有一套回放渲染 —— 如果"实时回放"喂的是
        另一种结构，就得写第二条渲染路径，而两条路径迟早会画出不同的东西。

        ⚠️ 拿到的是**调用那一刻的快照**：之后新到的帧不会进来。这是有意的 ——
        冻结才叫"回放"；想回到最新，退出回放即可（界面上就这么写的）。
        没在记录时返回 `None`（那时没有"这一段"）。
        """
        with self._lock:
            r = self._rec
            if r is None or not r["frames"]:
                return None
            return self._build_doc(r, why="(实时快照)", live=True)

    def live_decisions(self) -> Dict[str, Any]:
        """**实时决策流 + 上行事件**（都从常驻环取，`t_ms` 已相对环零点）。

        ⚠️ 它是"落盘/快照那份的同构版"：**同一套差分算法、同一个零点口径**。
        零点在 `start()` 之后由"发车后的第一帧"钉住，与那份的 `t0` 是同一个数；
        没发过车时才退化成"开始收到帧"。所以：

          · 发车之后 ⇒ 这条线与你点【进入回放】看到的那条线**逐条数值相等**；
          · 空环（刚发车、还没帧）⇒ `t0_pc_ms` 是 `None`，前端会显示"等待"。

        `events` 一并给出（**与 decisions 同一个环纪元**）—— 界面上那条时间线
        因此只有**一条来路**，不用再让前端自己攒（那样刷新一次就丢一半）。
        """
        with self._lock:
            z = self._dec_t0
            return {
                "t0_pc_ms": z,
                "t0_kind": self._dec_t0_kind,
                "now_ms": self._dec_prev_ms,
                "duration_ms": (0 if (z is None or self._dec_prev_ms is None)
                                else max(0, self._dec_prev_ms - z)),
                "with_codes": bool(self._dec.with_codes),
                "decisions": [dict(d, t_ms=_i(d.get("t_ms")) - _i(z, 0))
                              for d in self._dec_ring],
                # 零点还没有（刚清过环、还没收到帧）时**不猜时刻**：给 `None`，
                # 前端会把这些行放到末尾，并如实说"时刻未知"。
                "events": [dict(e, t_ms=(None if z is None
                                         else _i(e.get("t_ms")) - z))
                           for e in self._ev_ring],
            }

    def clear_decisions(self) -> Dict[str, Any]:
        """**清空常驻决策环与事件环**（右栏"决策历程"上那个【清空】按钮）。

        ⚠️ 与 `_reset_epoch()` 的分工（**不能互相替代**）：
          · `_reset_epoch(kind="launch")` 由 `start()`（发车/开记录）调用，且**带 stash**
            （发车失败要能 `restore_epoch()` 放回去）；
          · 这里是**用户手工清**，不带 stash（清了就不该被别处"放回来"），
            纪元语义标成 `"manual"` —— 它与"发车"不是一回事，界面上要能区分。

        为什么必须**清环 + 换差分器**、而不是只把前端那几行 DOM 抹掉：
        决策与上行事件的**唯一来路**就是这个常驻环（`live_decisions()`）——
        前端每 1 s 全量重拉一次，所以"只清显示"在下一次刷新就全回来。
        这正是上一轮踩过的坑（`#m8-ev-clear` 原叫【清空显示】，点了什么都不发生）。
        用户要的是"方便下一次发车的记录"，本质是**这一段到此为止、下一段从零开始**，
        所以正确做法是开一个新纪元（清环 + 换差分器 + 零点交给下一帧）。

        注意：**不影响正在记录的那一段**（`_rec` 不动）—— 清的是"右栏那条线"，
        不是存盘数据。所以可以随时按，用来把上一段残留从界面上赶走。
        """
        with self._lock:
            self._reset_epoch("manual", stash=False)
            return {"ok": True, "t0_kind": self._dec_t0_kind,
                    "note": "已清空决策历程"}

    @staticmethod
    def _num(v: Any) -> Any:
        """把帧值转成 JSON 能过的数。布尔→0/1（保持数值列同质，便于直接算/画）。"""
        if v is None:
            return None
        if isinstance(v, bool):
            return 1 if v else 0
        if isinstance(v, (int, float)):
            return v
        return str(v)

    def _build_doc(self, r: Dict[str, Any], why: str = "manual",
                   firmware_map: Optional[Dict[str, Any]] = None,
                   text: Optional[str] = None, end_at: Optional[int] = None,
                   live: bool = False) -> Dict[str, Any]:
        """把"在记的一段"组成一份**自包含的 doc**。

        抽出来是为了**两处共用**：
          · `stop()`   —— 组完落盘；
          · `snapshot()` —— 组完**不落盘**直接给界面（"边跑边回放"要用它）。
        两处**必须组出同一形状**，否则"实时回放"与"事后复盘"会分叉成两套画法，
        而分叉之后"复盘看到的不是当时看到的"这件事根本不会报错。

        时间轴（重要）：**所有 `t_ms` 都相对 `t0` = 第一帧的到达时刻**。
        `changes` / `events` / `decisions` 三条序列与 `t_ms` 列**共用这一个零点** ——
        混用两种时基会让复盘时"事件插在帧序列的哪一帧"系统性偏移几百 ms，
        而图看着还是对的（`feed()` 的 docstring 里记过这个坑）。
        """
        frames: List = r["frames"]                 # [(到达ms, rec, 帧发生ms|None), …]
        channels = list(self._get_channels() or [])
        if not channels and frames:
            # 拿不到通道清单就按帧里的键**按字母序**取 —— 这样导出的列名至少是自洽的。
            # ⚠️ 但这会让不同次导出**列序不同**（dict 序依赖上游）⇒ 导入端必须**读
            # 文件里的 `channels`**，不能假设一个固定顺序。
            channels = sorted(k for k in frames[0][1].keys()
                              if not k.startswith("_")) if frames else []
        t0 = frames[0][0] if frames else int(time.time() * 1000)
        end_ms = int(end_at if end_at is not None else time.time() * 1000)
        self.start_ms = t0          # 让 `_timeline` 与帧序列**减同一个零点**

        # 墙图：**从帧重放重建**（不存快照）—— 见模块 docstring 的"墙图不存，重建"
        mazetype = r.get("mazetype")
        if mazetype is None and frames:
            mazetype = _i(frames[-1][1].get("mazetype"), 7)
        wm = MazeWallMap(_i(mazetype, 7))
        for _t, rec, _d in frames:
            wm.feed(rec)

        # 决策：**绝对毫秒 → 相对 t0**（与 `t_ms` 列同源）。
        # `t_ms` 是第 0 个字段，所以直接换掉它。
        dec = [dict(d, t_ms=_i(d.get("t_ms")) - t0) for d in (r.get("decisions") or [])]
        # 缺码表时如实标注：界面要能说"这几类事件本机没有码表所以没有"，
        # 而不是让人以为"它从来没转过弯"。
        try:
            has_codes = bool(r["_dec"].with_codes)
        except Exception:                                     # noqa: BLE001
            has_codes = bool(self._codes)

        # ---- 完整性：`drop_seen` 按**本机**丢帧计数的**差量**算（不是设备计数）。
        # 语义窄一圈与"不自己比 seq"的理由见 `_probe_losses`。
        base = r.get("drop_base")
        now = self._probe_losses()

        def _dlt(key: str) -> Optional[int]:
            b = (base or {}).get(key)
            n = (now or {}).get(key)
            if b is None or n is None:
                return None          # 没探到 ≠ 没丢（三态：None 就是 None，不编 0）
            return int(n) - int(b)

        d_lost, d_crc = _dlt("seq_lost"), _dlt("bad_crc")
        # 差量 ≠ 0 就是这段数据不完整：>0 丢了；<0 是计数器中途被清零（链路重开过）
        # ⇒ 上行流断过，同样不完整。
        drop_seen = any(d is not None and d != 0 for d in (d_lost, d_crc))

        doc: Dict[str, Any] = {
            "format": FORMAT,
            "created_ms": end_ms,
            "start_ms": t0,
            "end_ms": end_ms,
            "duration_ms": end_ms - t0,
            "why": r.get("why"),
            "end_why": why,
            "auto_end": r.get("auto_end"),
            "mazetype": _i(mazetype, 7),
            "channels": channels,
            # 帧：`t_ms` 是**相对起点的到达毫秒**（整数，省一大截空间），
            # `frames[i]` 是按 `channels` 排的值。
            # 用列表而不是字典：一帧 39 个键名重复写几千次，文件会大好几倍。
            "t_ms": [int(t - t0) for t, _r, _d in frames],
            "frames": [[self._num(_r.get(c)) for c in channels] for _t, _r, _d in frames],
            "changes": r["changes"],
            "events": r["events"],
            # 决策序列（右栏时间线的数据源）。是 `changes` 的**超集**：
            # 除任务机迁移外还含 ROAD/TURN/STRAIGHT/JUNC/CELL/HEAD/GOAL。
            # 见 `MazeDecisions` 的 docstring（每一类的判据与上游依据都在那儿）。
            "decisions": dec,
            "decisions_have_codes": has_codes,
            "integrity": {
                "frames": len(frames),
                "t0_pc_ms": t0,
                "evq_seen": bool(r.get("evq_seen")),
                # 本机口径（§11.24 第二组 #3）：seq 缺口推算的上行丢失，语义比
                # 设备计数窄一圈。`seq_lost_delta`/`bad_crc_delta` 为 None =
                # 没探到（≠ 0），别当"没丢"读。
                "drop_seen": drop_seen,
                "seq_lost_delta": d_lost,
                "bad_crc_delta": d_crc,
                "tiny": len(frames) < MIN_FRAMES,
                "decisions": len(dec),
            },
            "wall": wm.to_json(),
        }
        if live:
            # 快照**不是**一份完成的记录：`end_why` 与 `duration_ms` 都还是"到目前为止"。
            # 标出来，免得前端把它当成"已经收尾的一段"（比如据此显示"已停止"）。
            doc["live"] = True
        # `t_dev_ms`：**帧发生时刻**（靠 timesync 锚算），只作附加列、**不参与排序**。
        #
        # ⚠️ 两道门都得过才写这一列：
        #   ① **每一帧都算得出** —— 部分有部分没有的列，读的人不知道该信哪一半；
        #   ② **量级合理** —— 它是"墙钟毫秒"，与到达时刻同一量级。如果锚错/失锁，
        #      `frame_time()` 会返回一个荒谬的数（探针里就造出过差 55 年的值）。
        #      这种列**宁可没有**：它一进文件，读的人会当成真的（"这一帧怎么发得这么早"），
        #      而真相只是"那次锚没对上"。⇒ 不合理就整列不写，并在 `integrity` 里留一句。
        dev_ok = False
        if frames and all(d is not None for _t, _r, d in frames):
            # 允许 [-30 s, +5 s]：设备时基与主机墙钟之间本来就有个未知常数偏移
            # （min-delay 估计只能消掉"最小单程延迟"那部分），但绝不该差到分钟以上。
            lo = min(t for t, _r, _d in frames) - 30_000
            hi = max(t for t, _r, _d in frames) + 5_000
            dev_ok = all(lo <= d <= hi for _t, _r, d in frames)
        if dev_ok:
            doc["t_dev_ms"] = [int(d - t0) for _t, _r, d in frames]
        elif frames:
            doc["integrity"]["dev_t_implausible"] = True
        fm = firmware_map or r.get("firmware_map")
        if fm:
            doc["firmware_map"] = fm
        if text:
            doc["text"] = str(text)
        return doc

    def _write(self, doc: Dict[str, Any]) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime(doc["created_ms"] / 1000.0))
        name = f"{ts}_mz{doc.get('mazetype')}.maze.json"
        p = self.root / name
        # 同一秒收两次（反复点发车/停止）时不要互相覆盖
        i = 2
        while p.exists():
            p = self.root / f"{ts}_{i}_mz{doc.get('mazetype')}.maze.json"
            i += 1
        tmp = p.with_suffix(".json.part")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
        tmp.replace(p)          # 原子替换：**读到半个文件**是最难查的一种
        return p

    # ------------------------------------------------------------ 复盘

    def records(self, limit: int = 50) -> List[Dict[str, Any]]:
        """列出已有记录（按时间倒序）。只读文件名与大小，**不解析内容**。"""
        out: List[Dict[str, Any]] = []
        try:
            files = sorted(self.root.glob("*.maze.json"), reverse=True)
        except OSError:
            return out
        for p in files[: max(1, int(limit))]:
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({"name": p.name, "size": st.st_size, "mtime_ms": int(st.st_mtime * 1000)})
        return out

    def load(self, name: str, with_map: bool = True) -> Dict[str, Any]:
        """读一条记录。返回 `{...doc, "timeline": [...]}` —— 时间线是**给右栏直接用的**。"""
        p = self._safe_path(name)
        doc = json.loads(p.read_text(encoding="utf-8"))
        if doc.get("format") != FORMAT:
            raise ValueError(f"不认识的记录格式：{doc.get('format')!r}（本层支持 {FORMAT}）")
        doc["name"] = p.name
        doc["size"] = p.stat().st_size
        # 时间线：把三样东西**并成一条按时间排的序列**，右栏照它渲染即可。
        # 为什么在后端合并：迁移序列是设备时基（由主机差分/记帧得到），
        # 事件是到达时刻，两边的时基不同 —— 让前端各排各的，迟早会排错。
        doc["timeline"] = self._timeline(doc)
        # ---- 决策序列
        # `decisions` 是本版新增字段。两种情况下它不在文件里：
        #   ① 老记录（本版之前写的）；② 外部导入的文件只带了帧。
        # 两种情况都**从帧重放重建** —— 而不是给一个空列表：
        # 帧在，决策就在（`rebuild_decisions` 的 docstring 说明了这一点）。
        # 重建出来的要标 `rebuilt`，好让"这条是算出来的、不是当时记的"看得见。
        dec = doc.get("decisions")
        if not isinstance(dec, list) or not dec:
            try:
                dec = rebuild_decisions(doc, self._codes)
                doc["decisions"] = dec
                doc["decisions_rebuilt"] = True
            except Exception:                                 # noqa: BLE001
                doc["decisions"] = []
                doc["decisions_rebuilt"] = False
        else:
            doc["decisions_rebuilt"] = False
        doc.setdefault("decisions_have_codes", bool(self._codes))
        if with_map:
            doc["wall"] = doc.get("wall") or {}
        return doc

    @staticmethod
    def _timeline(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        t0 = _i(doc.get("start_ms"), 0)
        for c in (doc.get("changes") or []):
            rows.append({"kind": "change", "t_ms": _i(c.get("t_ms")) - t0,
                         "from": c.get("from"), "to": c.get("to")})
        for e in (doc.get("events") or []):
            rows.append({"kind": "event", "t_ms": _i(e.get("t_ms")) - t0,
                         "event": e.get("event"), "reason": e.get("reason"),
                         "reason_text": e.get("reason_text"),
                         "seq": e.get("seq")})
        rows.sort(key=lambda r: _i(r.get("t_ms"), 0))
        return rows

    def import_doc(self, doc: Dict[str, Any], name: str = "") -> Dict[str, Any]:
        """导入一份**外部**记录（前端上传的文件内容），校验并落在记录目录里。

        为什么要落一份：复盘是"以后还要再看"的事，只存在浏览器内存里，
        刷新一次就没了。落下来之后它就和本机跑出来的记录**同等可检索**。
        """
        if not isinstance(doc, dict):
            raise ValueError("不是一个 JSON 对象")
        fmt = doc.get("format")
        if fmt != FORMAT:
            raise ValueError(f"不认识的记录格式：{fmt!r}（本层支持 {FORMAT}）")
        ch = doc.get("channels")
        fr = doc.get("frames")
        if not isinstance(ch, list) or not isinstance(fr, list):
            raise ValueError("缺少 channels / frames")
        if len(fr) and (not isinstance(fr[0], list) or len(fr[0]) != len(ch)):
            raise ValueError(f"帧宽与通道数不符：帧 {len(fr[0]) if fr else 0} vs 通道 {len(ch)}")
        t = doc.get("t_ms")
        if not isinstance(t, list) or len(t) != len(fr):
            raise ValueError(f"时间列与帧数不符：t_ms {len(t) if isinstance(t, list) else '?'} vs 帧 {len(fr)}")
        # 留个来源标记：**导入的**与**本机跑的**在列表里要能分得开
        doc = dict(doc)
        doc["imported"] = True
        doc["imported_from"] = str(name or "")[:120]
        p = self._write(doc)
        return {"ok": True, "name": p.name, "frames": len(fr),
                "note": "已导入并存到记录目录"}

    def rebuild_map(self, name: str) -> MazeWallMap:
        """**重放帧序列**重建墙图（复盘用）—— 与落盘时的 `wall` 互为校验。

        既然两种算法都在，就可以对账：重建结果与落盘结果不一致 ⇒ 说明
        "<某个版本重建逻辑变过"或"文件被改过"。这条对账让**复盘看到的图**
        与**当时看到的图**是同一个东西，而不是"另一张看起来也合理的图"。
        """
        key = str(name)
        with self._lock:
            hit = self._maps.get(key)
        if hit is not None:
            return hit
        doc = self.load(key, with_map=False)
        m = MazeWallMap(_i(doc.get("mazetype"), 7))
        ch = doc.get("channels") or []
        idx = {c: i for i, c in enumerate(ch)}
        for row in (doc.get("frames") or []):
            rec = {c: row[i] for c, i in idx.items() if i < len(row)}
            m.feed(rec)
        with self._lock:
            if len(self._maps) > 8:
                self._maps.clear()
            self._maps[key] = m
        return m

    def _safe_path(self, name: str) -> Path:
        """只允许读本目录下的 `*.maze.json` —— 防目录穿越（`../..`）。"""
        n = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
        if not n.endswith(".maze.json"):
            raise ValueError(f"只能读 *.maze.json：{name!r}")
        p = (self.root / n).resolve()
        if p.parent != self.root.resolve() or not p.is_file():
            raise ValueError(f"没有这份记录：{name!r}")
        return p
