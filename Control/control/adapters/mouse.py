"""鼠（模块 A）适配器 —— 包 `host.api.MouseClient`。

关键契约（读自 `host/api/client.py` 的模块 docstring）：
  · **订阅回调总在调度线程执行** ⇒ 回调里**绝不能**调用会阻塞等设备的 API
    （`ReentrancyError`）。所以我们回调里只做一件事：把事件投进总线（线程安全）。
  · `connect()` 失败时**不建会话、不开流**，原因进事件流 ⇒ 我们把它翻成可读信息。
  · **遥测只有一条流**（上游 2.0.0 起）：统一帧 53 B，一份帧就是**全量快照**
    （45 B 载荷 / 39 个扁平通道）。**2.1.0 起它由上位机逐条拉取**，实测 `4.4~6.5 Hz`
    （见下）。我们按 `SAMPLE_HZ` 推读数条，并按窗口推二进制波形
    （判据 I10：payload > 64 KB 必须走二进制帧）。

## 关于上游 2.1.0（「拉取模式」—— v2.0 的自时钟被真机推翻）

**这是本项目最重要的一条实测教训，值得记在这里**：v2.0 让固件"自时钟"全速推流
（53 B × ≈217 Hz ≈ **4.3 KB/s**），而这条链路（CH340 + 蓝牙透传）端到端实测只有
**≈100~200 B/s** —— 超了约 **20 倍**。后果是蓝牙模块的**内部缓冲被灌满后停止转发、
且不会自行恢复（必须给模块断电重上）**。现场现象辨识度极高：`PING`/`ARM`/`GET ALL`/
`MAP DUMP` 这些**小的、一次性的突发全部正常**，而一经 `STREAM ON` 开始持续流就**立刻 0 字节**，
之后**连 `PING` 都不应答**。

**根因不在固件，而在"谁能控制发送节奏"**：固件把 TX 环当背压信号，但**蓝牙模块的内部缓冲
对 MCU 完全不可见** —— MCU 侧 TX 环空不空，与蓝牙模块灌没灌满毫无关系。
（v1.5.5 的 `link.c` 里本来就写着这条约束并因此把速率压到 152 B/s，v2.0 重写时删掉了。）

**2.1.0 的改法**：删除 `STREAM ON`/`STREAM OFF`，新增 **`TELEM`** —— `PC → TELEM`，
设备回**一帧 + `ACK <seq> OK <upMs>`**。**收到一条请求才发一帧**，节奏完全由上位机决定。
于是 "PC 停止请求 → 固件不再发送 → 蓝牙缓冲自然排空 → **无需断电即可自愈**"。

**对本层的直接影响：几乎为零**（这是好设计的结果）。`mc.stream.on()` / `off()` 的
**方法名与签名都没变**，变的只是**语义**：从"命令固件开流"变成"**启停本机拉取器**"。
本层只在这几处跟着改（都是文案/语义，不是逻辑）：

- **帧率不再是 217 Hz**：`UNIFIED_HZ = 217.0` 被上游降级为"只用于环形缓冲容量估算 +
  缺省显示"。实际帧率用 `telemetry.hz()`（按到达间隔**实算**，无数据返回 0）。
  ⇒ 界面上**任何 "≈217 Hz" 都必须删掉**（真机差约 50 倍）。
- **`mc.telemetry.hz()` 现在是实测值**，不是额定值 ⇒ 本节 `_rate_snapshot()` 里那个
  "额定 vs 实测"的对照**已失效**，`nominal` 直接取 `UNIFIED_HZ`（协议常量）作参照。
- **时间锚改由每次 `TELEM` 应答给**（v2.0 是"开流时给一次"）⇒ **锚随每次拉取刷新**，
  失锁能立刻自愈。代价：拉取模式下 RTT 抖动大（143~527 ms），`drift_ppm` 拟合会更噪。
- **`stream.off()` 立刻丢弃在途请求** —— 这是让蓝牙缓冲排空的唯一手段，不能改成"等它回来"。
- `CommandClient.TIMEOUT_S` **0.50 → 2.5 s**（真机单命令往返 ≈1.21 s）。
- **版本号终于修了**：`host.__version__` **1.1.0 → 2.1.0**、`registry.API_VERSION` → **2.1.0**、
  `PROTO_VER` → **3**。⇒ `caps.api_version` 现在**可信**。但本层**不退回"信版本号"**：
  能力探测多花的是一次 `import`，而信一个字符串的代价是"某天它又忘了改"。

**为什么必须停等（在途恒 1）**：固件**每 1 ms tick 只消费一行命令**
（`link.c` 的 `USART_RX_STA & 0x80` 未清时，`usart.c` 把后续字节直接丢弃）。
真机背靠背回收率：1 条 **100%** / 2 条 50% / 3 条 33% / 4 条 25% / 8 条 12%；
**间隔 300 ms 逐条发则 100%**。固定窗口扫描：窗口 1 = **6.50 Hz**（已拿到 1/RTT 满速），
窗口 4/6 = 7.15 Hz（只多约 10% 却开始成串丢）。
⇒ 可达帧率 = **1/RTT**，**不是软件限速**，且停等比堆在途请求更快（零浪费、零丢失）。

## 关于上游 2.4.0（编译档位 5 → 2 + 删除课程调参整层）

上游把编译档位从 5 档砍到 **2 档**（远程档 `MOUSE_REMOTE` / 按键比赛档"什么都不定义"），
其中 `MOUSE_LESSON_LAB`（课程剖面引擎）**整层删除** —— 这是本轮对本层影响最大的一条。
细节见 `docs/上位机整合设计方案.md` §11.15，这里只列**改了什么**：

- **删**：`mc.lesson` 子 API（`host/api/lesson.py` 865 行 + `host/lesson.py` 568 行）、
  三条命令 `lesson.start`/`lesson.stop`/`lesson.status`、`Event.LESSON` 事件、
  课程 14 项参数（id 8–21）、Flash page 116。
- **删（本层）**：⑨ 实验页的"课程调参实验"半屏、`/api/v1/lesson/*` 七条路由、
  参数表的"课程调参"组。**课程层删了，但"实验页"本身还在**（它还有 `mc.lab` 电机实验）。
- **新增**：`maze.run` 带**可选目标词** `BLIND|RACE`。`RACE` = 正式比赛代码
  （先读 Flash 地图选档/定冲刺，再进主循环）—— 这是新增的「比赛」子页面的**唯一**上行入口。
- **迁移**：算参纯函数（`solve_ratio` / `error_ratio` / `theory_*` …）从 `api/lesson.py`
  **原样搬到 `host/api/calc.py`**，`host.api` 仍然导出 ⇒ **本层只改 import 路径**。
  注意 `error_ratio` **不在包的 `__all__` 里**（历史上就只在 lesson.py 内定义），
  所以取法要"先从模块、再退回包"两条都试 —— 只试包会 AttributeError，
  而"取不到就置 None"会把错误方向指成"上游缺少算参纯函数"。

⚠️ 版本号：`registry.API_VERSION` → **2.4.0**、`LINK_FW_VER` → **216**、
`LINK_PROTO_VER` → **5**、设备指纹第 4 字段（TX 环缓冲）256 → **512**。
本层**照旧不退回信版本号** —— 一律看 features。

## 关于上游 2.0.0（协议整体替换）

上游把"三通道并存"（周期帧 + `SNAP` 阻塞问答 + `SCOPE`/`DUMP` 双流）**整体替换**为
**单流 + 请求-响应**：① 上行只有"持续数据流 + 异常事件"；② 下行一律请求-响应；
③ 上位机能算的不传。对本层的直接影响（详见 `docs/上位机整合设计方案.md` §11.9）：

- **删**：`mc.snap`（组测发送）、`mc.scope`（1 kHz 录制+整块回传）、`RATE`（速率命令）、
  `VER`（并入 `PING`）、`MAZE EV`（迷宫事件流）与 1 Hz 迷宫状态帧。
- **改**：`telemetry.latest()` 由 `(fast, mid, slow)` 三元组变为**单个 dict**；
  参数 `GET ALL` 只回**生产命名空间**（24 项），课程 14 项**读侧不对称**（一律 `ERR_NAME`，
  写侧 SET/SETQ 通）；`CAL` 改请求-响应（采完才 ACK，超时 10 s、不重发）。
- **留**：`TARE`、`mc.move`、`mc.lab.capture()`、`mc.maze`（墙图 +
  状态从统一帧读）。迷宫的"它刚才识别到了什么"改由**主机差分相邻帧**取得，不再有事件流。
  ⚠️ **2.4.0 起 `mc.lesson` 连这个"留"字都不算了** —— 课程层整层删除，见上。

## 关于上游 1.2 / 1.3 / 1.4 的三次更新

上游今天从 1.1.0 连跳到 1.4.0，新增三组子 API：

| 子 API | 干什么 | 落在这层的哪里 |
|---|---|---|
| `mc.lab` | 电机远程控制实验（轮子悬空，时间+红外双护栏，停止后回传 1kHz 日志） | ⑨ 实验页 |
| `mc.lesson` | 课程调参实验（实验三~八）+ **由回传数据自动算参** | ⑨ 实验页 |
| `mc.timesync` | 固件毫秒时基 → 本机单调秒（min-delay 偏移 + 最小二乘 ppm） | ⑧ 发车页 + 时间轴 |

⚠️ **版本号不可信 → 后来可信了，但我们不改判据**：`host.__version__` 曾长期停在 `1.1.0`
（CHANGELOG 已到 1.4.0），2.1.0 才把它修成 `2.1.0`（与 `registry.API_VERSION`、`PROTO_VER=3` 一致）。
本层**仍然一律用 `inspect`/`hasattr`/`import` 做能力探测**（见 `_probe_features()`），
不因为"这次它改对了"就退回信字符串 —— 能力探测多花的是一次 `import`，而信版本号的代价是
"某天它又忘了改"（这个代价已经付过一次了）。

⚠️ **参数有两套、两个 Flash 页**（**这条在 2.4.0 已作废，留在这里是为了让后人不要再翻回去**）：
生产标定（id 1–7）走 `mc.params.set()`，课程调参（id 8–21）走 `mc.lesson.set_params()`。
2.4.0 删掉课程层后**只剩生产命名空间**（page 115，单通道 `mc.params`；v2.4.0 时 24 项，
**v2.5.0 起扩到 41 项**，+「运动控制」组 id 39–55，仅远程档生效）⇒
`params_grouped()` 不再需要"按组选通道"，但**保留了分组返回的形状**（组数 4 → 3，
2.5.0 起上游自己加回第四组「运动控制」），
免得前端和判据要跟着改一遍形状。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import asdict, is_dataclass

from ..bus import Bus, f32_2d
from ..clock import mono_to_ms, now_ms
# 墙图重建（实验 8 的中栏"重构示意"）。复用**同一个类**：实时从环形缓冲重建、
# 复盘从记录文件重放 —— 两边走同一套方向转换，才不会"实时看到的图与复盘的不一样"。
from ..maze_rec import MazeWallMap
from . import probe_module

#: 高频遥测的采样节拍（Hz）—— 10 Hz 足够人看。
#: ⚠️ 别把它当成"实时感"的来源：2.1.0 起帧率由**链路往返**决定（真机 4.4~6.5 Hz），
#: 所以这里 10 Hz 是个**上限**，实际每拍常常没有新帧可取（那就照旧推上一份，不造数）。
SAMPLE_HZ = 10.0

#: 波形页要看的通道（从统一帧的 39 个扁平通道里挑"随时间有用"的那几个）。
#: **不把 39 路全推**：一屏画 39 条线没有意义，39×N 的二进制帧既大又慢（判据 I10）。
#: 名字必须与上游 `UNIFIED_CHANNELS` 一致 —— 缺的那几路会在采样时自动跳过。
WAVE_CHANNELS = ("speedL", "speedR", "gyroAngle", "encAngle", "pidErr",
                 "trackL", "trackR", "cellOdom",
                 "irF1", "irF2", "irL", "irR", "battMv")


class MouseError(RuntimeError):
    """鼠侧操作失败。消息面向现场操作者，必须能照着做。"""


#: 上游 state_detail 里出现这些词，就补一句"先关谁"
_OCCUPIED_HINTS = ("占用", "拒绝访问", "Access is denied", "PermissionError", "busy")

#: 参数落在哪一页 Flash。⚠️ **这两个数会随固件分区调整而变** ——
#: 上游 1.5.4 把运行数据区上移后，生产标定页由 99 → **115**、课程参数页由 100 → **116**
#: （原值正落在 `FLASH_EraseRunData()` 的擦除范围内，会被整片抹掉）。
#: 上游没有把页号程序化暴露出来（CLI 的 help 文本里写的是 115/116），
#: 所以这里如实抄下来**并把出处写清楚**：以后改了要一起改，别让它变成一个没人记得的数字。
FLASH_PAGE_PROD = 115      # 生产命名空间：id 1–7 + 1.5.0 新增的 22–38（红外 / 电源）
FLASH_PAGE_LESSON = 116    # 课程调参：id 8–21


def _enhance(detail: str) -> str:
    d = detail or "未知原因"
    if any(h.lower() in d.lower() for h in _OCCUPIED_HINTS):
        d += ("\n  · 常见原因：RemoteControl 自带的 Qt 上位机（run_host.py）未关闭，它独占串口，"
              "必须先关掉。\n"
              "  · 或串口助手 / Keil 的串口窗口未关闭。\n"
              "  · 无硬件时可直接连模拟器：tcp://127.0.0.1:5555"
              "（RemoteControl/host/sim/mouse_sim.py）")
    return d


class MouseAdapter:
    """鼠侧统一入口。所有方法都可能抛 `MouseError`（消息可照做）。"""

    def __init__(self, cfg: Any, bus: Bus, store: Any) -> None:
        self.cfg = cfg
        self.bus = bus
        self.store = store
        self._mc = None
        self._lock = threading.RLock()
        self._unsubs: List[Any] = []
        self._api_version: Optional[str] = None
        self._module_version: Optional[str] = None
        self._import_error: Optional[str] = None
        #: 能力探测结果（**不信版本号**，见模块 docstring）
        self._features: Dict[str, bool] = {}
        self._last_advice: Any = None
        #: 实验 8 记录器**第一次**拒收帧时是否已报过（见 `on_telemetry` 的说明：
        #: 只吞一次异常、第一次必须留痕，否则"记录中但 0 帧"这种错完全无声）。
        self._rec_feed_failed = False
        #: 记录器（由 `Ctx.__init__` 注入）。这里先给个 None，免得 `_attach()` 早于注入时
        #: `getattr` 落到"没有这个属性"那条路上去。
        self.maze_recorder: Any = None
        #: 「连接中」闸门：上游 connect 可能永久阻塞（死串口占住命令锁），
        #: 这把锁一旦没被释放，就说明上一次连接还没回来 —— 见 connect() 的注释。
        self._connecting = threading.Lock()
        self._connect_started_ms: Optional[int] = None
        #: 实测速率窗口：`[(单调秒, 累计样本数), …]`（只有一条流，键固定 `unified`）。
        #: 为什么要自己量：**不能拿协议常量当实测**。`UNIFIED_HZ`（217）是"纯串口线"上的
        #: 理论上限，而真机这条蓝牙链路的实测帧率是 **4.4~6.5 Hz**（差约 50 倍）。
        #: 2.1.0 起 `mc.telemetry.hz()` 确实已改成"按到达间隔实算"，但那个值依赖上游
        #: 环形缓冲里的到达时刻；本层按**样本累计数的差分**自己量一遍，这样
        #: 它与"采样循环真的取到了几帧"是同一个口径（上游换实现也不影响这里）。
        #: 把额定值摆到界面上，现场会把"帧率掉了一半"当成正常（真踩过，见 §11.7）。
        #: ⇒ 只报量出来的。
        self._rate_hist: Dict[str, Any] = {}
        self._unified_hz_nominal: Optional[float] = None
        self._load()

    # ------------------------------------------------------------ 装载
    def _load(self) -> None:
        paths = self.cfg.get("paths.remotecontrol_host", "")
        mod, err = probe_module("host.api", paths)
        if err:
            self._import_error = err
            self.bus.log("warn", f"鼠模块不可用：{err.splitlines()[0]}", source='mouse')
            return
        try:
            from host.api import MouseClient, Event, StreamAPI   # noqa: F401
            from host import __version__ as host_ver
            self._MouseClient = MouseClient
            self._Event = Event
            # `StreamAPI` 在 v2.0 只剩开关（`RATE` 命令已删）—— 但它仍是**类**上的
            # 一套方法，所以"没连接也能宣告能力"这条口径不变。
            # ⚠️ 2026-10-15 核对（v2.6.1）：`RATE` 命令族**回来了**（rate.fix/pull/off），
            #    上面这句"已删"只对 v2.0~v2.5 成立；到 v2.9 门面又端出 pace 批式拉取。
            #    留着这句是为了记"类一直在、能力不一直在"，不是当前事实。
            # ⚠️ `ScopeAPI` 在 v2.0 整个模块被删除（SCOPE/dump 机制取消），
            # 以前 `from host.api import ... ScopeAPI` 会 ImportError ⇒ **整个门面装载失败**、
            # 鼠模块直接不可用。所以这里只 import 还存在的名字。
            self._StreamAPI = StreamAPI
            # ⚠️ 上游有**两个**版本字符串，而且它们的维护程度不一样（2026-10-05 核对）：
            #    · `host.__version__`          —— 停在 **2.1.0**（没人维护）
            #    · `registry.API_VERSION`      —— **2.2.1**（每次发版都改，CHANGELOG 逐条对照）
            # 所以"上游版本"这条**以 `API_VERSION` 为准**，`__version__` 只作人类参考。
            # （本层仍**不靠版本号判能力** —— 能力一律走 `_probe_features()`；
            #  版本号只用来在界面上说"你这份上游是哪一版"。）
            try:
                from host import registry as _reg
                self._api_version = str(getattr(_reg, "API_VERSION", "") or "") or None
                self._module_version = (getattr(host_ver, "__str__", lambda: None)() or None)
            except Exception:                                 # noqa: BLE001
                self._api_version = (getattr(host_ver, "__str__", lambda: None)() or None)
                self._module_version = None
            self._probe_features()
        except Exception as e:                               # noqa: BLE001
            self._import_error = f"host.api 可导入但门面装载失败：{type(e).__name__}: {e}"
            return
        self._import_error = None
        # 装载成功 ⇒ 把决策差分要的**码表**补给记录器。
        # ⚠️ 这一步必须放在 `_load()` 里（而不是只在 `ctx` 构造时给一次）：`ctx` 建记录器
        #    的时刻与本适配器装载完成的时刻**不一定谁先**（适配器在 `ctx` 里被 new 出来，
        #    但 `_load()` 的路径注入/探测有一堆可能失败的步骤）。只给一次的话，
        #    "装载晚于 ctx"这条路上码表永远是空的 —— 表现是右栏**只有任务迁移、
        #    没有路口/转向**，而且不会有任何报错（差分器会"如实降级"）。
        rec = getattr(self, "maze_recorder", None)
        if rec is not None:
            try:
                rec.set_codes(self.maze_codes())
            except Exception as e:                            # noqa: BLE001
                self.bus.log("warn", f"实验 8 决策码表注入失败：{type(e).__name__}: {e}", source='mouse')

    def _probe_features(self) -> None:
        """能力探测。**不读版本号** —— `host.__version__` 停在 1.1.0 而 CHANGELOG 到 1.4.0。

        探法分两种，按"这个能力是不是靠类暴露的"来选：
          · 子 API 是 `__init__` 里挂的**实例属性**（`mc.timesync`），类上取不到
            ⇒ 只能试着 **import 那个类**（`TimeSyncAPI` 在 1.4.0 才有）。
          · 门面上的方法/属性（`motor_guard` / `estop_locked`）在**类**上
            ⇒ `hasattr(MouseClient, ...)` 就够。
        """
        f = {"timesync": False, "lab": False, "lesson": False, "params_grouped": False,
             "motor_guard": hasattr(self._MouseClient, "motor_guard"),
             "estop_locked": hasattr(self._MouseClient, "estop_locked"),
             "log_ready": hasattr(self._MouseClient, "log_ready"),
             "motor_disabled": hasattr(self._MouseClient, "motor_disabled"),
             # 2.0.0：单一统一帧（`TYPE_UNIFIED`，45 B 载荷 / 39 个扁平通道）
             "unified": False,
             # 2.0.0：上行事件带序号（`STOPPED <reason> #n`）⇒ 丢失**可检测**
             "event_seq": False,
             # 2.0.0：实验 8 无记忆走迷宫 —— 状态**从统一帧读**（无 MAZE EV / 无状态帧）
             "maze_state": False,
             # 2.1.0：遥测改为**上位机停等拉取**（`TELEM`）⇒ `mc.stream.on/off` 语义变成
             # "启停本机拉取器"；帧率由 1/RTT 决定（真机 4.4~6.5 Hz），不再是 217 Hz
             "pull_mode": False,
             # 2.9.0：**批式拉取** `mc.stream.pace()`（稀疏基频 + 每批多帧 ⇒ 10 Hz，
             # 停等式 ~4 Hz 的 2.5 倍）；v2.6 回来的 `RATE` 族只是裸命令，
             # 判"批式可用"要看门面三件套（pace/pace_off/mode）
             "pace_pull": False,
             # 2.1.1：**收车命令** `MAZE STOP`（`mc.maze.stop()`）—— 推翻"发过车就只能断电"
             "maze_stop": False,
             # 2.2.0：**整机软复位**复位口 `mc.conn.soft_reset()` / `rescue()`
             "soft_reset": False,
             # 2.2.1：发车前**低电硬闸**（上游 `registry.LAUNCH_MIN_MV`，默认 7000 mV）
             "launch_gate": False,
             # 2.4.0：`maze.run(target=...)` 带**可选目标词** `BLIND|RACE`。
             # ⚠️ 探测的是**方法签名里有没有 `target`**，不是版本号 ——
             # 上游 2.3.0 的 `run()` 没有这个形参，传了会 TypeError。
             "maze_target": False,
             # 2.4.0：算参纯函数搬到了 `host/api/calc.py`（课程层删除，能力保留）
             "calc": False,
             # 2.4.0：课程调参整层**已删除**。这是个**否定式**能力：为 True 表示
             # "上游还有课程层"（老上游）。界面据此决定要不要画课程那一半。
             "lesson_gone": False,
             "seq_stats": False}
        mods = {}
        for key, name in (("timesync", "TimeSyncAPI"), ("lab", "MotorLabAPI"),
                          ("lesson", "LessonAPI")):
            try:
                mods[key] = getattr(__import__("host.api", fromlist=[name]), name)
                f[key] = True
            except Exception:                                 # noqa: BLE001
                mods[key] = None
        # 课程参数表（1.3.0 起 PARAM_IDS 7 → 21；**2.4.0 起课程条目整体删除**）
        #
        # ⚠️ 这三个符号在 2.4.0 **已经不存在**，所以这里**只能**用 `getattr` 兜底，
        #    不能写 `from ... import LESSON_PARAM_IDS` —— 那个 import 会抛 ImportError，
        #    被 `except` 吞掉之后 `PARAM_IDS` 也一并丢失 ⇒ 参数表整张变空，
        #    而报出来的现象是"这台设备没有参数"，**完全指错方向**。
        try:
            from host.protocol import commands as _cmds
            f["params_grouped"] = True
            self._param_ids = dict(getattr(_cmds, "PARAM_IDS", {}) or {})
            self._lesson_param_ids = dict(getattr(_cmds, "LESSON_PARAM_IDS", {}) or {})
            self._param_groups = dict(getattr(_cmds, "PARAM_GROUPS", {}) or {})
            f["lesson_gone"] = not self._lesson_param_ids
        except Exception:                                     # noqa: BLE001
            self._param_ids = {}
            self._lesson_param_ids = {}
            self._param_groups = {}
        # 时间同步的纯函数（可脱硬件单测的那几个）—— 我们直接复用，不自己写一遍
        if f["timesync"]:
            for fn in ("min_delay_offset", "fit_clock", "drift_ppm", "resolve_ms8"):
                try:
                    mods[fn] = getattr(__import__("host.api", fromlist=[fn]), fn)
                except Exception:                             # noqa: BLE001
                    mods[fn] = None
        # 算参纯函数（实验三/五/六 的自动算参靠它们）—— 同上，复用不重写。
        #
        # ⚠️ **2.4.0 把家搬了**：课程层删除，这几个函数从 `host.api.lesson` 原样迁到
        #    `host/api/calc.py`（上游 CHANGELOG「新增：标定算参独立成模块（能力保留）」）。
        #    所以候选模块要**三个都试**：`host.api.calc`（2.4.0）→ `host.api.lesson`（≤2.3）
        #    → `host.api` 包。
        # ⚠️ 还要注意 `error_ratio` **不在包的 `__all__` 里**（历史上就只在 lesson.py 内定义），
        #    只从包上 getattr 会 AttributeError —— 而"取不到就置 None"会让后面报出
        #    "上游缺少算参纯函数"这种**指错了方向**的错。多试几个模块正是为了这个。
        _calc_srcs = []
        for _mn in ("host.api.calc", "host.api.lesson"):
            try:
                _calc_srcs.append(importlib.import_module(_mn))
            except Exception:                                 # noqa: BLE001
                pass
        try:
            _calc_srcs.append(__import__("host.api", fromlist=["*"]))
        except Exception:                                     # noqa: BLE001
            pass
        for fn in ("solve_ratio", "error_ratio", "theory_track_ratio",
                   "theory_gyro_ratio"):
            mods[fn] = None
            for src in _calc_srcs:
                if hasattr(src, fn):
                    mods[fn] = getattr(src, fn)
                    break
        f["calc"] = all(mods.get(fn) is not None for fn in
                        ("solve_ratio", "error_ratio",
                         "theory_track_ratio", "theory_gyro_ratio"))
        try:
            from host import registry
            f["seq_stats"] = True
            self._registry = registry
        except Exception:                                     # noqa: BLE001
            self._registry = None
        # ---- 2.0.0 统一帧：判据是"协议层有通道清单 + 有解码器"。
        # 没连接也问得到（`UNIFIED_CHANNELS` / `decode_unified` 都是模块级符号）。
        # 顺带把 `UNIFIED_HZ`（协议常量，2.1.0 起只作参照）一起取来，省得在采样循环里重复 import。
        #
        # ⚠️ 通道数**不许写死**。2.0.0 是 **39**、2.3.0 起是 **49**（前墙双档 + 比赛冲刺状态
        #    各追加了通道，见上游 CHANGELOG 2.3.0 与 `codec.UNIFIED_CHANNELS` 的注释）。
        #    这里原来写的是 `len(...) == 39`，于是 2.3.0 上游一上来这条能力位就**静默变 False**
        #    —— 而"统一帧不可用"会让依赖它的界面整块降级，且**没有任何判据会红**
        #    （判据只查 `caps.unified_channels` 这个列表，不查这条能力位）。
        #    ⇒ 判据取"清单非空 + 有解码器"；数量对不对由 `caps` 的清单与 drill 的 I22 负责
        #    （它们都是**从协议层逐项比对**，不是数个数）。
        self._unified_channels: List[str] = []
        self._unified_hz_nominal: Optional[float] = None
        try:
            from host.protocol.codec import (UNIFIED_CHANNELS,   # noqa: F401
                                             UNIFIED_HZ, decode_unified)
            self._unified_channels = list(UNIFIED_CHANNELS)
            self._unified_hz_nominal = float(UNIFIED_HZ)
            f["unified"] = (len(self._unified_channels) > 0
                            and callable(decode_unified))
        except Exception:                                     # noqa: BLE001
            pass
        # ---- 2.1.0 拉取模式：判据是"门面暴露了拉取器的状态查询 + 锚"。
        # 有 `anchor()` 就说明是 v2.1 那套 `PullRequester`（v2.0 的 `StreamAPI` 没有它）。
        # ⚠️ 探的是**存在性**而不是版本号：`StreamAPI` 类在两个版本都在，方法名 `on/off` 也没变，
        #   所以"是不是拉取模式"只能靠这个新增的符号来分。
        f["pull_mode"] = False
        try:
            from host.api import StreamAPI
            f["pull_mode"] = all(hasattr(StreamAPI, m) for m in ("anchor", "stats"))
        except Exception:                                     # noqa: BLE001
            pass
        # ---- 2.9.0 批式拉取 `mc.stream.pace()`：`pace` / `pace_off` / `mode` 三件套。
        # ⚠️ 探存在性而不是版本号（`StreamAPI` 类一直都在，方法名 `on/off` 也没变，
        #   只有这套新符号能把"支持批式"分出来）。`RATE` 命令在 v2.6 回来了，
        #   但门面直到 v2.9 才把批式拉取端出来 ⇒ 早先的 `rate.fix/pull/off` 是
        #   裸命令，没有门面方法，判不了"批式可用"。
        f["pace_pull"] = False
        try:
            from host.api import StreamAPI
            f["pace_pull"] = all(hasattr(StreamAPI, m)
                                 for m in ("pace", "pace_off", "mode"))
        except Exception:                                     # noqa: BLE001
            pass
        # ---- 2.0.0 上行事件序号：门面把"事件流是否完整"直接端出来
        # （`events_incomplete` 读的是帧 flags 的 EVQ 位；`event_health` 给缺口统计）。
        f["event_seq"] = (hasattr(self._MouseClient, "events_incomplete")
                          and hasattr(self._MouseClient, "event_health"))
        # ---- 2.1.1 收车命令 `maze.stop`（`MAZE STOP`）：带出迷宫 + 复位发车状态 + 解锁急停。
        # ⚠️ 这是**推翻 v2.0 一条结论**的新能力：以前"发过一次车就只能断电"（发车状态
        #   永不复位），现在有正经的收车口。所以探到它之后，本层"收车"这件事从
        #   "只能复位/重连"变成"有一个明确的动作"，界面上也就该有那个按钮。
        # 判据取两处任一：`MazeAPI.stop`（门面方法）或登记表里有 `maze.stop`（命令）；
        # 上游可能在两处只落一处，取"任一存在"才不至于把可用功能判成不可用。
        f["maze_stop"] = False
        try:
            from host.api import MazeAPI
            f["maze_stop"] = hasattr(MazeAPI, "stop")
        except Exception:                                     # noqa: BLE001
            pass
        # ---- 2.2.0 复位（整机软复位）：`ConnAPI.soft_reset` / 别名 `rescue`，命令 `system.reset`。
        # ⚠️ 判据是**方法存在**而不是版本号：`soft_reset` 是 2.2.0 新增的，v2.1 的 `ConnAPI`
        #   没有它，而 `ConnAPI` 这个类本身两版都在 ⇒ 只有新增符号能区分。
        try:
            from host.api import ConnAPI
            f["soft_reset"] = (hasattr(ConnAPI, "soft_reset")
                               and hasattr(ConnAPI, "rescue"))
        except Exception:                                     # noqa: BLE001
            pass
        # ---- 2.2.1 发车前低电闸：`registry.LAUNCH_MIN_MV` + `CommandSpec.precheck`。
        # 本层把它**端到界面上**（发车前先自己看一眼电压），所以要把那个阈值取过来；
        # 取不到就不预判（宁可少报，也不要写死一个 7000 在上游改动后变成谎）。
        self._launch_min_mv: Optional[int] = None
        if self._registry is not None:
            try:
                self._launch_min_mv = int(getattr(self._registry, "LAUNCH_MIN_MV"))
                f["launch_gate"] = True
            except Exception:                                 # noqa: BLE001
                pass
        # ---- 2.4.0 `MAZE RUN [BLIND|RACE]`：判据是**方法签名里有没有 `target`**。
        # ⚠️ 必须探签名而不是版本号：2.3.0 的 `run()` 是零参的，传 `target=` 会 TypeError，
        #   而那个 TypeError 会冒成 500，看上去像"本层拼错了请求"。
        f["maze_target"] = False
        try:
            import inspect as _inspect
            from host.api import MazeAPI
            f["maze_target"] = ("target" in _inspect.signature(MazeAPI.run).parameters)
        except Exception:                                     # noqa: BLE001
            pass
        # ---- 2.0.0 迷宫（实验 8）：**状态从统一帧读**。菜单上的成员缺一不可，
        # 所以逐个 hasattr，而不是只看类存在 —— 上游删了哪一项都不该静默降级。
        # ⚠️ `reset` 在 v2.0 **已删除**（`MAZE RST` 随之取消），所以门槛是
        # "能读状态 + 能差分 + 能汇总"，清统计只清**本层**的差分缓冲。
        try:
            from host.api import MazeAPI
            f["maze_state"] = all(hasattr(MazeAPI, m) for m in
                                  ("state", "position", "road_name", "task_changes",
                                   "summary", "clear_changes"))
        except Exception:                                     # noqa: BLE001
            pass
        self._api = mods
        # ⚠️ 这两个**已经在上面探参数表那段里赋过值**（2.4.0 起用 `getattr` 兜底，
        # 因为 `LESSON_PARAM_IDS` 可能整个不存在）。这里**不许**再赋一次 ——
        # 再赋就会把上面那个兜底结果覆盖成 `NameError`/`None`，而症状是
        # "参数表突然空了、且没有任何报错"。
        self._features = f
        missing = sorted(k for k, v in f.items() if not v)
        if missing:
            self.bus.log("warn", f"上游缺少这些能力（按缺失项禁用对应功能）：{missing}", source='mouse')

    @property
    def available(self) -> bool:
        return self._import_error is None

    @property
    def import_error(self) -> Optional[str]:
        return self._import_error

    # ------------------------------------------------------------ 生命周期
    def _ensure(self) -> Any:
        if not self.available:
            raise MouseError(f"鼠模块不可用。\n{self._import_error}")
        with self._lock:
            if self._mc is None:
                # session_dir 指向整合层的会话根；auto_session=False ⇒ 会话开关由
                # Store 统一编排（判据 I5 的原子性要求"谁开谁关"是同一个主体）。
                self._mc = self._MouseClient(
                    session_dir=str(self.cfg.session_root),
                    auto_session=False,
                    auto_stream=bool(self.cfg.get("mouse.auto_stream", True)),
                    scheduler_hz=float(self.cfg.get("mouse.scheduler_hz", 50.0)),
                    telemetry_window_s=float(self.cfg.get("mouse.telemetry_window_s", 30.0)),
                    auto_reconnect=bool(self.cfg.get("mouse.auto_reconnect", True)),
                )
                self._attach()
            return self._mc

    @property
    def mc(self) -> Any:
        return self._mc

    @property
    def features(self) -> Dict[str, bool]:
        """能力位快照（`_probe_features()` 的产物，**零设备访问**）。

        ⚠️ 返回副本：调用方（端点门控）只读，但副本能防"谁改了就影响全模块"。
        判能力一律用这个，**不退回信版本号**（上游版本字符串维护程度不一）。
        """
        return dict(self._features)

    @property
    def connected(self) -> bool:
        return self._mc is not None and self._mc.is_connected

    def close(self) -> None:
        with self._lock:
            if self._mc is not None:
                try:
                    self._mc.close()
                except Exception:                            # noqa: BLE001
                    pass
                self._mc = None

    # ------------------------------------------------------------ 事件桥
    def _attach(self) -> None:
        mc, Event = self._mc, self._Event
        mark = self.store.mark

        def on_state(m) -> None:
            self.bus.publish("mouse.state", {"state": m.data.get("state"),
                                             "detail": m.data.get("detail", "")})
            mark("mouse", "link.state", m.data.get("state", ""), m.data.get("detail", "")[:120])
            # 链路断了就把实验 8 的记录**当场收尾**（判据③）。不收尾的话
            # 那一整段数据只存在内存里，服务一停就没了 —— 而"跑了一盘没存上"
            # 是最令人沮丧的一种失败。收尾失败也不能影响状态派发。
            if str(m.data.get("state") or "") != "connected":
                rec = getattr(self, "maze_recorder", None)
                if rec is not None:
                    try:
                        rec.feed_link_down()
                    except Exception:                         # noqa: BLE001
                        pass

        def on_error(m) -> None:
            d = m.data or {}
            # v2.9 的 `link_lost`（连续 3 s 无帧判死）：上游 msg 里带的是
            # **API 术语**（"mc.stream.pace() 恢复"）—— 现场照不了。
            # ⇒ `hint` 给**界面动作**版的可照做清单，msg 仍留上游原话（取证）。
            # ⚠️ 两者都要发：msg 是真相，hint 是"下一步点哪"，缺一个都违背界面纪律。
            hint = ""
            if d.get("kind") == "link_lost":
                hint = ("给电脑鼠与蓝牙模块断电 3~5 秒，重新连串口，"
                        "再把拉取模式选一次（停等或批式）。"
                        "实测断连不会自行恢复，等下去没有用。")
            self.bus.publish("mouse.error", {"kind": d.get("kind"), "msg": d.get("msg"),
                                             "hint": hint})
            mark("mouse", "link.error", d.get("kind", ""), str(d.get("msg", ""))[:160])

        def on_text(m) -> None:
            d = m.data or {}
            self.bus.publish("mouse.text", {"line": d.get("line", ""),
                                            "direction": d.get("direction", "->")})

        def on_event(kind: str):
            def _cb(m) -> None:
                self.bus.publish("mouse.event", {"kind": kind, "data": m.data})
                ref = ""
                note = ""
                d = m.data or {}
                if kind == "move":
                    ref = str(d.get("kind", ""))
                    note = _short(d)
                elif kind == "map":
                    ref = "map"
                    note = f"mazetype={d.get('mazetype')} pos=({d.get('posX')},{d.get('posY')})"
                # `t_src=arrival`：这类事件来自设备的**文本明细行**，我们手上只有
                # "主机收到的那一刻"。行里自带 `ms`（发车以来毫秒）就顺手记进 dev_ms ——
                # 那是设备侧的真相，比到达时刻可信。
                mark("mouse", f"mouse.{kind}", ref, note,
                     t_src="arrival", dev_ms=_dev_ms_of(d))
            return _cb

        def on_advice(m) -> None:
            # 降级建议**去重**：链路抖的时候它 50 Hz 地来，不去重就是刷屏
            d = m.data or {}
            key = (d.get("level"), d.get("suggest_rate"))
            if key == getattr(self, "_last_advice", None):
                return
            self._last_advice = key
            self.bus.publish("mouse.advice", d)

        def on_param(m) -> None:
            d = m.data or {}
            self.bus.publish("mouse.param", {"id": d.get("id"), "name": d.get("name"),
                                             "q": d.get("q")})
            mark("mouse", "mouse.param", str(d.get("name", "")), f"q={d.get('q')}")

        def on_session(m) -> None:
            self.bus.publish("mouse.session", m.data or {})

        def on_upevent(m) -> None:
            """**统一上行事件**（2.0.0）：`STOPPED <reason> #n` / `ESTOP TRIP #n`。

            `STOPPED` 的 reason（TIME / IR / ESTOP / USER / END）**推不出来** ——
            "时间护栏到"与"红外护栏触发"在帧里长得一样。所以它是"运动为什么停"的
            唯一答案，必须进去事件流并落时间轴。
            事件行与遥测帧**共用一个 1 字节序号** ⇒ `step>=2` 说明中间丢了几条，
            界面要显式报"事件流不完整"，不许假装完整。
            """
            d = m.data or {}
            self.bus.publish("mouse.upevent", d)
            mark("mouse", "mouse.upevent." + str(d.get("event", "")),
                 str(d.get("reason", "")), _short(d), t_src="arrival",
                 dev_ms=_dev_ms_of(d))
            # 实验 8 的记录器要这条：`reason` **从帧里推不出来**，
            # 复盘时想知道"它为什么停的"只有这一处能答（见 `maze_rec.py`）。
            rec = getattr(self, "maze_recorder", None)
            if rec is not None:
                try:
                    rec.feed_event({**d, "status": str(d.get("reason") or "")})
                except Exception:                             # noqa: BLE001
                    pass      # 记录失败**绝不能**把事件派发搞断

        def on_telemetry(m) -> None:
            """**每一帧**统一帧。实验 8 的记录器靠它拿数据。

            ⚠️ 为什么不用本层的 `mouse.telemetry` 总线消息：那条由 `sampler_loop` 发，
            而它**只在有浏览器订阅二进制帧时才发**（`if not self.bus._bin_subs: continue`）
            ⇒ 记录器会"没有数据，而且一声不响"。上游这条 `Event.TELEMETRY` 是
            **每收到一帧就 emit 一次**，与有没有人看无关。

            ⚠️ 这里跑在**上游调度线程**上 ⇒ 只允许纯计算与内存写：
            `rec_time_ms()` 读的是本地锚与帧里的 `ms8`（不发命令、不阻塞），是安全的。

            ⚠️ **异常只吞一次**（第一次记 warn，之后静默）。为什么不是"永远静默"：
            这里曾踩过一个**完全无声**的坑 —— 记录器的 `feed()` 参数名从 `t_ms` 改成
            `t_dev_ms`，而这个回调还在传 `t_ms=` ⇒ `TypeError` 被 `except` 吞掉
            ⇒ 界面上"记录中…0 帧"，一路跑完保存下来是个空文件，**看不到任何报错**。
            所以：第一次失败必须留痕（写清是哪个调用、什么异常），否则这类错只能靠猜。
            """
            rec = self.maze_recorder if hasattr(self, "maze_recorder") else None
            if rec is None:
                return
            d = m.data or {}
            frame = d.get("rec") or {}
            if not frame:
                return
            # ⚠️ **不能因为"没在记录"就早退**：记录器里还有一个**常驻的决策差分器**，
            # 它要在没记录的时候也把帧差分成决策 —— 否则右栏在"发车之前 / 收尾之后"
            # 会是空的，而那两段时间用户恰恰在看（"停车后数据仍保持实时"）。
            # "记不记盘"由记录器自己判（`feed()` 里 `if r is None: return`）。
            t_dev_ms = None
            if rec.recording:
                # "帧发生时刻"只有落盘才用得上（另存一列 `t_dev_ms`）；没记就不白算。
                try:
                    t_dev_ms = self.rec_time_ms(frame)
                except Exception:                             # noqa: BLE001
                    t_dev_ms = None
            try:
                rec.feed(frame, t_dev_ms=t_dev_ms)
            except Exception as e:                            # noqa: BLE001
                if not self._rec_feed_failed:
                    self._rec_feed_failed = True
                    self.bus.log("warn",
                                 f"实验 8 记录器拒收帧（后续同类不再重复报）："
                                 f"{type(e).__name__}: {e}", source='mouse')

        def on_estop(m) -> None:
            """急停锁存/触发。**必须进时间轴**：它解释了"为什么后面的命令全被拒"。"""
            d = m.data or {}
            self.bus.publish("mouse.estop", d)
            mark("mouse", "mouse.estop", str(d.get("kind", "")), _short(d),
                 t_src="arrival", dev_ms=_dev_ms_of(d))

        def on_lesson(m) -> None:
            """课程调参实验事件。**2.4.0 起上游已删除 `Event.LESSON`**
            （固件不再发 `LESSON DONE/STATUS` 行）—— 这里留着只是为了"老上游
            仍有该事件"的兜底，新上游上这个回调**永远不会被调用**。
            真收到就登记进时间轴：课程实验的 `DONE/STATUS` 是判"跑到哪一步"的依据。"""
            d = m.data or {}
            self.bus.publish("mouse.lesson", d)
            mark("mouse", "mouse.lesson." + str(d.get("kind", "")),
                 str(d.get("exp", "")), _short(d),
                 t_src="arrival", dev_ms=_dev_ms_of(d))

        def on_timesync(m) -> None:
            """发车回执（`kind=armed`）。时间同步的**起点**，落盘要留。"""
            d = m.data or {}
            self.bus.publish("mouse.timesync", d)
            mark("mouse", "mouse.timesync." + str(d.get("kind", "")), "",
                 _short(d), t_src="arrival", dev_ms=_dev_ms_of(d))

        def on_maze(m) -> None:
            """迷宫相关事件。**2.0.0 起上游不再有 `MAZE` 事件**（`MAZE EV` 整条删除，
            状态量全在统一帧里），所以这里只作为"老上游仍有该事件"的兜底 ——
            被广播出去、**不落时间轴**（一次迷宫跑下来 5~6 条/s，落进去会把
            真正要紧的行淹掉）。新版的边沿由**主机差分相邻帧**得到（见 `maze_view`）。"""
            self.bus.publish("mouse.maze", m.data or {})

        self._unsubs = [
            mc.on(Event.STATE, on_state),
            mc.on(Event.ERROR, on_error),
            mc.on(Event.TEXT, on_text),
            mc.on(Event.PARAM, on_param),
            mc.on(Event.SESSION, on_session),
            mc.on(Event.MOVE, on_event("move")),
            mc.on(Event.MAP, on_event("map")),
            mc.on(Event.ADVICE, on_advice),
        ]
        # 2.0.0 起**只留** `STOPPED` / `ESTOP TRIP` 两种上行事件（`Event.EVENT`）。
        # ⚠️ 旧版的 `Event.DUMP_PROGRESS` / `Event.DUMP_DONE` **已删除** ——
        # 直接 `Event.DUMP_*` 会 AttributeError ⇒ `_attach()` 抛 ⇒ **连都连不上**。
        # 所以这里一律 `getattr(Event, ...)` 探测（老上游/新上游都能跑）。
        for feat, name, cb in (("event_seq", "EVENT", on_upevent),
                               ("estop_locked", "ESTOP", on_estop),
                               ("lesson", "LESSON", on_lesson),
                               ("timesync", "TIMESYNC", on_timesync),
                               ("maze_state", "MAZE", on_maze)):
            ev = getattr(Event, name, None)
            if ev is None:
                continue
            try:
                self._unsubs.append(mc.on(ev, cb))
            except Exception as e:                           # noqa: BLE001
                self.bus.log("warn", f"订阅 {name} 事件失败（不影响其余）：{e}", source='mouse')
        # ---- `TELEMETRY`（每帧一次）：只给实验 8 的记录器用。
        # 探不到就跳过（老上游也一直有它，但这里仍按"能力探测"的规矩写）。
        ev = getattr(Event, "TELEMETRY", None)
        if ev is not None:
            try:
                self._unsubs.append(mc.on(ev, on_telemetry))
            except Exception as e:                           # noqa: BLE001
                self.bus.log("warn", f"订阅 TELEMETRY 事件失败（实验 8 记录会没有数据）：{e}", source='mouse')

    # ------------------------------------------------------------ 连接
    def ports(self) -> List[dict]:
        mc = self._ensure()
        out = []
        for t in mc.conn.targets():
            out.append({"label": t.label(), "kind": t.kind, "port": t.port,
                        "host": t.host, "tcp_port": t.tcp_port, "baud": t.baud,
                        "target": _t(target=t)})
        try:
            bt = mc.conn.bluetooth_port()
        except Exception:                                    # noqa: BLE001
            bt = ""
        return {"targets": out, "bluetooth_port": bt or None,
                "note": "末尾两个 TCP 为模拟器端口"}

    def connect(self, target: Any = None, baud: Optional[int] = None) -> dict:
        """连接。**有防挂死闸门**（见下）。"""
        mc = self._ensure()
        # ⚠️ 上游的 connect **可能永久阻塞**，而且我们改不了它（判据 I9：只消费上游）。
        #
        # 实测到的现场（看门狗 dump 线程栈）：
        #   · 上游**调度线程**卡在 `link.write → serial.write → win32.GetOverlappedResult(True)`
        #     —— 那条串口**能打开但对端不存在**，写操作无限等；
        #   · 它握着 `CommandClient._lock`；
        #   · 于是主线程的 `mc.connect()` 卡在 `cmd.send("STREAM ON")` 的 `with self._lock`，
        #     **永不返回**。
        # 症状极有迷惑性：HTTP 请求挂住、而 `/health` 照样 200（那个端点是纯内存读），
        # 看起来像"网络问题"或"服务卡了"，其实是一条死串口把命令锁占死了。
        #
        # 我们的对策不是去改上游，而是**不进第二次**：用一把非阻塞闸把"还在途中的连接"
        # 挡住。第一次挂住之后所有后续调用**立刻**返回一行能照做的报错，
        # 而不是排队一起挂死。
        if not self._connecting.acquire(blocking=False):
            raise MouseError(
                "上一次连接尝试尚未返回：上游在某个串口上写阻塞"
                "（端口可打开但对端不存在），并持有命令锁。\n"
                "  · 此状态下后续连接均不会成功，只能重启 Control 服务"
                "（Ctrl+C 后重新执行 python app.py）。\n"
                "  · 同时拔掉或关闭那个可疑串口，或先在上游 run_host.py 中释放它。\n"
                "  · 若只需模拟器空跑，把 config.yaml 的 mouse.target 设为 "
                "tcp://127.0.0.1:5555，不要再点串口。")
        self._connect_started_ms = now_ms()
        try:
            ok = mc.connect(target, baud=baud)
        except Exception as e:                               # noqa: BLE001
            raise MouseError(_enhance(f"连接 {target!r} 抛异常：{type(e).__name__}: {e}")) from e
        finally:
            # 正常返回（成功或失败）才放闸；**挂死时这里永远不会执行** —— 这正是闸门的用途
            self._connecting.release()
        if not ok:
            raise MouseError(_enhance(mc.state_detail or "连接失败，但上游没给原因"))
        self._last_advice = None
        out = {"ok": True, "target": _t(mc.link.target), "device": mc.device,
               "state": mc.state}
        # ---- 连上之后的默认拉取模式（上游 2.9.0 起有二选一）
        # `auto_stream` 已经开了**停等式**（上游 connect 的行为）；按配置再切成批式。
        # ⚠️ 只在 `pace_pull` 为真时切 —— 老上游上 `pace` 不存在，硬调会 AttributeError。
        # ⚠️ 这一步最坏 6 s（pace 内部 cmd.send wait=True 试 3 次）⇒ 必须在 executor 里
        #    （connect 本来就在 executor 里，见 routes）。失败**不阻断连接**：
        #    停等式照样在跑，"没切过去"是降级不是故障，但要留一句日志。
        mode = str(self.cfg.get("mouse.stream_mode", "pace") or "pace").strip().lower()
        out["stream_mode"] = _safe(lambda: str(mc.stream.mode)) or "off"
        if mode == "pace" and self._features.get("pace_pull") and mc.stream.is_on:
            try:
                r = mc.stream.pace()
                out["stream_mode"] = _safe(lambda: str(mc.stream.mode)) or "off"
                out["pace"] = {"ok": bool(getattr(r, "ok", False)),
                               "status": getattr(r, "status", "")}
            except Exception as e:                            # noqa: BLE001
                self.bus.log("warn", f"批式拉取切换失败，继续用停等："
                                     f"{type(e).__name__}: {e}", source='mouse')
        return out

    def disconnect(self) -> dict:
        if self._mc is None:
            return {"ok": True, "note": "未连接"}
        self._mc.disconnect()
        return {"ok": True, "state": self._mc.state}

    # ------------------------------------------------------------ 会话
    def open_session(self, name: str) -> str:
        """开鼠侧会话。`name` 用 `<sid>/mouse` ⇒ 目录落在整合层会话里（§5.1）。"""
        mc = self._ensure()
        path = mc.session.open(name)
        if path is None:
            raise MouseError(
                "鼠侧会话开不出来（上游返回 None）。常见原因：会话根目录不可写 —— "
                f"检查 {self.cfg.session_root}")
        return path

    def close_session(self, extra: Optional[dict] = None) -> Optional[str]:
        if self._mc is None:
            return None
        return self._mc.session.close(extra)

    @property
    def session_open(self) -> bool:
        return self._mc is not None and self._mc.session.is_open

    # ------------------------------------------------------------ 状态 / 能力
    def health(self) -> dict:
        if not self.available:
            return {"connected": False, "port": None, "detail": self._import_error,
                    "available": False}
        if self._mc is None:
            return {"connected": False, "port": None, "available": True,
                    "detail": "未连接"}
        mc = self._mc
        try:
            stats = mc.conn.stats()
        except Exception as e:                               # noqa: BLE001
            stats = {"error": f"{type(e).__name__}: {e}"}
        out = {
            "available": True,
            "connected": bool(mc.is_connected),
            "port": _t(mc.link.target) if mc.is_connected else None,
            "state": mc.state,
            "detail": mc.state_detail or "",
            "device": mc.device,
            "stats": stats,
            "degraded": bool(mc.conn.is_degraded()) if mc.is_connected else None,
            "session_open": mc.session.is_open,
            "stream_on": bool(mc.stream.is_on),
        }
        # ---- 1.2/1.3/1.4 新增的状态位（老上游没有就不给这个键）
        out["connect_in_flight"] = self._connecting.locked()
        out["connect_started_ms"] = self._connect_started_ms
        if self._connecting.locked():
            out["detail"] = (
                "上一次连接尚未返回：上游多卡在一条「可打开但无对端」的串口上，"
                "命令锁被其持有，当前无法连接任何目标。请重启 Control 服务。")
        if self._features.get("estop_locked"):
            try:
                out["estop_locked"] = bool(mc.estop_locked)
            except Exception:                                # noqa: BLE001
                out["estop_locked"] = None
        if self._features.get("motor_disabled"):
            try:
                out["motor_disabled"] = bool(mc.motor_disabled)
            except Exception:                                # noqa: BLE001
                out["motor_disabled"] = None
        if self._features.get("log_ready"):
            try:
                out["log_ready"] = bool(mc.log_ready)
            except Exception:                                # noqa: BLE001
                out["log_ready"] = None
        # ---- 时间同步（上游 1.4.0）。**这是"这一帧到底发生在什么时候"的唯一来源。**
        out["timesync"] = self.timesync_snapshot()
        return out

    def maze_codes(self) -> Optional[dict]:
        """给决策差分器用的**码表**（路口分类器 + 名字表）。没装载好就给 `None`。

        为什么由这里提供而不是让记录器自己 import 上游：本层的"摸上游"只有一处
        （本适配器的 `_load()` / `_probe_features()`），别处再摸一遍就会多出一个
        "上游不可用时行为不同"的地方。而且记录器**要能在上游不可用时继续工作**
        （帧已经收到了，就该能存下来、能画出来）。

        返回 `None` 是完全正常的一态：决策差分器会**如实降级**成只产不需要码表的四类
        （`TASK`/`CELL`/`HEAD`/`GOAL`），而不是自己编一份码表（编一份必然与上游漂移）。
        """
        if not self.available:
            return None
        try:
            from host.api.maze import JUNCTION_KINDS
            from host.protocol.codec import (DIR_NAMES, MOTION_TASK_NAMES,
                                             ROAD_KINDS, ROAD_NAMES, classify_road)
        except Exception:                                     # noqa: BLE001
            return None
        return {
            "classify_road": classify_road,
            "road_name": lambda c: ROAD_NAMES.get(int(c), "NONE"),
            "road_kind": lambda n: ROAD_KINDS.get(str(n), ""),
            "motion_name": lambda c: MOTION_TASK_NAMES.get(int(c), str(c)),
            "dir_name": lambda d: DIR_NAMES.get(int(d), str(d)),
            "junction_kinds": JUNCTION_KINDS,
        }

    def caps(self) -> dict:
        if not self.available:
            return {"available": False, "error": self._import_error}
        mc = self._mc
        dev = mc.device if mc is not None else {}
        exps = None
        if self._features.get("lesson"):
            # ⚠️ `LESSON_EXPS` 是课程层的静态表，2.4.0 起**已不存在** ⇒ 必须
            # `getattr` 兜底。这里先 `self._features.get("lesson")` 判一道，
            # 所以新上游上根本走不进来 —— 但两道保险都要有：课程层被删时
            # `LessonAPI` 没了、`LESSON_EXPS` 也没了，两处是**独立**消失的。
            try:
                _api = __import__("host.api", fromlist=["LESSON_EXPS"])
                exps = [{"no": int(e.no), "name": e.name, "args": list(e.arg_names),
                         "defaults": list(e.defaults), "help": e.help}
                        for e in getattr(_api, "LESSON_EXPS").values()]
            except Exception:                                # noqa: BLE001
                exps = None
        return {
            "available": True,
            # ⚠️ 2026-10-05 改：这个数**取的是 `registry.API_VERSION`**（上游每次都改、
            # 与 CHANGELOG 逐条对得上：2.1.0 → 2.1.1 → 2.1.2 → 2.2.0 → 2.2.1）。
            # 以前读的是 `host.__version__`，那个**停在 2.1.0 不动**（没人维护）⇒ 会低估。
            # `module_version` 把那个字符串一并端出来，好让"两处不一致"一眼可见 ——
            # 不一致**不是故障**，是上游的既有状况；但看的人得知道该信哪一个。
            # ⚠️ 判能力仍**一律看 features**（`_probe_features()`），不退回信版本号。
            "api_version": self._api_version or "(未知)",
            "api_version_trustworthy": True,
            "module_version": self._module_version,
            "features": dict(self._features),
            # 2.2.1 发车前低电闸的阈值（mV）。上游 `registry.LAUNCH_MIN_MV` —— 本层
            # **不写死**：取不到就给 None，由界面改成"不预判"（写死一个 7000 会在上游
            # 改了阈值之后变成谎）。界面拿它做**发车前的预检**，早于设备拒绝就说清。
            "launch_min_mv": self._launch_min_mv,
            "protocol": dev.get("proto"),
            "firmware": dev.get("fw"),
            "mazetype": dev.get("mazetype"),
            # 只有**一条流**（2.0.0）：统一帧 53 B / 39 个扁平通道。
            # 没连接也要答得出通道清单（能力探测时已从协议层取到 `_unified_channels`），
            # 否则"未连接时页面画不出要读什么"。
            # ⚠️ 这句原来写"`RATE` 已删 ⇒ 没有可调速率"：`RATE` 族在 v2.6 回来了，
            #    v2.9 又端出 pace 批式 ⇒ 现在**有**可调速率（见 features.pace_pull）。
            # ⚠️ 2.1.0 起帧率**不再是 217 Hz**（改成上位机停等拉取，真机 4.4~6.5 Hz），
            # 所以这里给的 `stream_hz` 是**实测值**（`telemetry.hz()` 已改为按到达间隔实算）；
            # 想拿协议常量作参照用 `stream_hz_nominal`。
            "streams": {"unified": (mc.telemetry.channels() if mc
                                    else list(self._unified_channels))},
            "stream_hz": {"unified": (mc.telemetry.hz() if mc else None)},
            "stream_hz_nominal": {"unified": self._unified_hz_nominal},
            "unified_channels": (mc.telemetry.channels() if mc
                                 else list(self._unified_channels)),
            # ⚠️ **这里故意不给"候选连接目标"**（`mc.conn.targets()`）。
            # 那个列表每次都要枚举一遍串口（Windows 上 `list_ports` 是同步慢调用），
            # 而本方法是被 `GET /api/v1/caps` **同步**调用、跑在事件循环上的 ——
            # 枚举一次就能把整服务卡住几百毫秒到几秒（真踩过，见 `params_grouped` 的说明）。
            # 端口列表归 `GET /api/v1/mouse/ports`：它走线程池、由顶栏【电脑鼠】窗口里
            # 那个【刷新】按钮**由人按**，慢一点是等得起的；而本端点的契约是
            # "**不连也能答**"（`docs/接口对照表.md`），不该摸设备。
            "commands": (sorted(getattr(self._registry, "COMMANDS", {}))
                         if self._registry else None),
            "frames": ([f.name for f in getattr(self._registry, "FRAMES", {}).values()]
                       if self._registry else None),
            "params": self.params_grouped(refresh=False),
            "lesson_exps": exps,
            # 2.4.0：`MAZE RUN` 能不能带目标词。界面的「比赛」页**只能**在它为真时
            # 画出来 —— 老上游上那个按钮按下去是 TypeError，比没有按钮更坏。
            "maze_target": bool(self._features.get("maze_target")),
            # 能选的发车目标。上游 `_maze_run()` 只认这两个词（大小写不敏感，
            # 默认 BLIND 时**省略**目标词）。
            "maze_targets": (["BLIND", "RACE"]
                             if self._features.get("maze_target") else ["BLIND"]),
            # 2.9.0：批式拉取可用性。**摊平到顶层**（不埋在 features 里）——
            # 与 `maze_target` 同一套路：界面据此画/不画模式切换，
            # 端点据此决定收不收 pace 请求。老上游上是 False ⇒ 切换不画。
            "pace_pull": bool(self._features.get("pace_pull")),
            # 当前拉取模式（门面三态 off/stopwait/pace）。⚠️ 取的是**门面属性**
            # `mc.stream.mode`，不是 `stats()["mode"]` —— 后者停等式恒为
            # "stop-and-wait"，门面全关时也返回它（会把"没开流"显示成批式）。
            # 没连接时答 "off"（三态纪律：没探过就不是 stopwait）。
            "stream_mode": (_safe(lambda: str(mc.stream.mode))
                            if (mc is not None and mc.is_connected) else "off"),
        }

    # ============================================================ 时间同步
    # 上游 1.4.0 起，固件→本机的时间映射由 `mc.timesync` 负责（min-delay + ppm）。
    # 本层**只透传 + 落盘 + 换算到墙钟**，不自己再算一遍。

    def timesync_snapshot(self) -> Optional[dict]:
        """当前时间同步状态。未连接或上游没有这个能力时返回 None。"""
        mc = self._mc
        if mc is None or not self._features.get("timesync"):
            return None
        try:
            st = mc.timesync.snapshot()
            d = st.to_dict() if hasattr(st, "to_dict") else dict(st)
        except Exception as e:                               # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}
        try:
            d["explain"] = mc.timesync.explain()
        except Exception:                                    # noqa: BLE001
            pass
        return d

    def arm(self) -> dict:
        """**发车**：固件记下发车时刻、点亮 1Hz 时间基准灯，并回 `ARMED <ms>`。

        点灯是为了让摄像头/光电/人眼能**独立校核**软件估出来的延迟 ——
        光靠软件两边永远只能互相印证，加一个物理基准才算闭环。
        """
        mc = self._need("timesync", "时间同步（发车）")
        r = mc.timesync.arm(timeout=3.0)
        self.store.mark("mouse", "mouse.arm", "", f"ok={r.ok} status={r.status}")
        if not r.ok:
            raise MouseError(f"发车（ARM）失败：{r.status}"
                             + (f"\n  ⇒ {r.data.get('hint')}" if isinstance(r.data, dict)
                                and r.data.get("hint") else ""))
        return {"ok": True, "status": r.status, "data": r.data,
                "timesync": self.timesync_snapshot()}

    def launch_precheck(self) -> dict:
        """**发车前预检**（纯读内存里的最新帧，**不发任何命令**）。

        为什么本层要自己预判一遍（上游 2.2.1 已经有硬闸了）：
        上游那道闸是**发送时**才拦的，现场看到的是一个 `ERR_LOWBATT` 报错；而"电池够不够"
        这件事在**点发车之前**就该看得见 —— 界面上先摆出来，人就不会白点一次。
        本层这道**只是预判，不是闸**（真正的闸在上游的 `send_cmd` 里，脚本/CLI 也绕不开）。

        ⚠️ 判据与上游**逐条对齐**（免得出现"界面说没问题、发车却被拒"）：
          · `battMv == 0` ⇒ **不拦**。那是"电池监视未开启"，不是"没电"
            （比赛固件从不打开电池监视）；把它当低电会**误锁死台架**。
          · 取不到帧 ⇒ **不拦**，并如实标 `unknown`（判不了 ≠ 不通过）。
          · 其余按 `battMv < LAUNCH_MIN_MV`（上游给的阈值，取不到就**不预判**）。
        """
        mc = self._mc
        mn = self._launch_min_mv
        mv: Optional[int] = None
        if mc is not None and mc.is_connected:
            try:
                f = mc.telemetry.latest()
                mv = int((f or {}).get("battMv") or 0)
            except Exception:                                 # noqa: BLE001
                mv = None
        if mn is None:
            return {"level": "unknown", "batt_mv": mv, "min_mv": None,
                    "note": "本机上游没有发车低电阈值，不做预判（发车时以设备应答为准）。"}
        if mv is None:
            return {"level": "unknown", "batt_mv": None, "min_mv": mn,
                    "note": "读不到电池电压（未连接或还没有帧），无法预判；发车时以设备应答为准。"}
        if mv == 0:
            return {"level": "unknown", "batt_mv": 0, "min_mv": mn,
                    "note": "电池监视未开启（读数为 0，不是没电），不预判；发车时以设备应答为准。"}
        if mv < mn:
            return {"level": "warn", "batt_mv": mv, "min_mv": mn,
                    "note": (f"电池只有 {mv} mV，低于发车下限 {mn} mV，发车会被拦下。"
                             f"低电时红外发射管变暗、墙检测会失效，而固件对低电没有任何保护动作，"
                             f"低电发车可能失控。请充电或换电池后重试。"
                             f"确需台架调试，把电脑鼠架离地面后用逃生舱 send_raw 强发。")}
        return {"level": "ok", "batt_mv": mv, "min_mv": mn, "note": ""}

    def maze_run(self, target: str = "BLIND") -> dict:
        """远程发车跑迷宫（**仅远程档 `MOUSE_REMOTE` 实现**，比赛固件回 `ERR_VALUE`）。

        `target` 决定跑哪套剖面（**2.4.0 新增**）：

        | target | 固件路径 | 界面入口 |
        |---|---|---|
        | `BLIND`（默认） | `LESSON_Blind8()` 无记忆走迷宫 | 「无记忆走迷宫」页 |
        | `RACE` | 先 `STATE_LoadMapAndSelectSpeed()` + `STATE_DetermineGoaheadNum()`（读 Flash 地图选档/定冲刺），再进主循环 | 「比赛」页 |

        ⚠️ **`BLIND` 时把目标词省掉**（`MAZE RUN` 而不是 `MAZE RUN BLIND`）—— 与上游
        `_maze_run()` 的拼装一致，于是 v2.3 时代的调用点与日志格式一字不改。
        这里**不自己拼字符串**，交给上游的 `MazeAPI.run(target=...)`（它保证同构）。

        ⚠️ `RACE` 需要设备里有一张**有效 Flash 地图**：读到默认空图（`MAZE_Init()` 的
        `[0][0]=0x11, [1][0]=0x70, 其余 0xF0`）时会规划出 7×7 之外的格子，
        表现是"卡在越界格"。**这是上游既有的"目标格越界"行为**（上游已决定不改）⇒
        本层如实转达，不假装它跑对了。

        ⚠️ 2.1.1 / 2.2.1 起，这条命令的失败**有三种，处置完全不同**，所以必须分开报
        （以前只有 `ERR_VALUE` 一种说法，把三种混成一句会把现场引到错方向）：

        | status | 含义 | 该做什么 |
        |---|---|---|
        | `ERR_LOWBATT` | **上位机自己的低电闸**拦下了（压根没发出去，`data.blocked=True`） | 充电/换电；确需台架强发把车架离地面后用 `send_raw` |
        | `ERR_ESTOP` | 急停**锁存中**（固件会接受请求然后被 ISR 每帧清零速度，现场表现为"发了车却不动"） | 先点【解锁急停】 |
        | `ERR_BUSY` | 已经在跑迷宫（发车状态没复位） | 先【收车】（`MAZE STOP`），不行再【复位】 |
        | `ERR_VALUE` | 该固件档位没有这条命令 | 换远程档固件 |

        ⚠️ `ERR_LOWBATT` **不是设备回的** —— 它是上游在 `send_cmd` 的登记表层拦的，
        所以它连链路都没碰（这也是为什么它带得出实测电压）。本层照样如实转达，
        但文案要说清"**是上位机拦的，不是设备拒绝的**"，否则现场会去查固件。
        """
        tgt = str(target or "BLIND").strip().upper() or "BLIND"
        if tgt not in ("BLIND", "RACE"):
            raise MouseError(f"发车目标只能是 BLIND 或 RACE，收到 {tgt!r}")
        mc = self._need("timesync", "远程发车跑迷宫")
        r = mc.timesync.maze_run(target=tgt, timeout=3.0)
        self.store.mark("mouse", "mouse.maze_run", f"{tgt} {r.status}", f"ok={r.ok}")
        if r.ok:
            return {"ok": True, "status": r.status}
        d = r.data if isinstance(r.data, dict) else {}
        st = str(r.status or "")
        if st == "ERR_LOWBATT":
            mv = d.get("battMv")
            mn = d.get("minMv") or self._launch_min_mv
            raise MouseError(
                f"发车被拦下：电池只有 {mv} mV（下限 {mn} mV）"
                f"\n  ⇒ 这是**上位机的保护闸**，不是设备拒绝 —— 命令压根没发出去。"
                f"\n  ⇒ 低电时红外发射管变暗、墙检测会失效，而固件对低电**没有任何保护动作**，"
                f"所以低电发车可能失控（真机事故：电机一直转且对红外不敏感）。"
                f"\n  ⇒ 请充电或换电池后重试。确需台架调试，把电脑鼠**架离地面**，"
                f"用逃生舱 `send_raw(\"MAZE RUN\")` 强发。")
        if st == "ERR_ESTOP":
            raise MouseError(
                f"发车失败：急停锁存中（ERR_ESTOP）"
                f"\n  ⇒ 急停是**锁存**的：不解锁的话，固件会收下请求、再被 ISR 每帧把速度清零，"
                f"现场看起来就是\"发了车却不动\"。"
                f"\n  ⇒ 先点【解锁急停】（发的是 `STOP`），再发车。")
        if st == "ERR_BUSY":
            raise MouseError(
                f"发车失败：已经在跑迷宫（ERR_BUSY）"
                f"\n  ⇒ 发车状态没复位。以前这种情况只能断电，现在有正经的收车口："
                f"\n  ⇒ 先点【收车】（`MAZE STOP`）。若任务机**仍停在 SEARCH**，说明车还在动"
                f"（`MAZE STOP` 只置\"请退出\"标志，车在运动时轮不到检查），改用【复位】（软复位）。")
        raise MouseError(
            f"远程发车失败：{st}\n"
            f"  ⇒ `MAZE RUN` 只在 MOUSE_DATA_LOG 档固件里实现；其余档位会返回 ERR_VALUE。"
            f"当前设备：{mc.device}")

    def maze_stop(self) -> dict:
        """**收车**：把电脑鼠带出迷宫（`MAZE STOP`）—— 停车 + 复位发车状态 + 解锁急停。

        ⚠️ 与【急停】的分工（两条都要有，不是二选一）：
          · 【急停】`ESTOP` = **立刻停车 + 锁存**；它停电机，但**停不掉**固件那个
            `while(1)` 迷宫循环 ⇒ 发车状态不复位 ⇒ 再发车仍 `ERR_BUSY`；
          · 【收车】`MAZE STOP` = **正常收车 + 复位发车状态 + 解锁急停**。
        两个都能把车带出迷宫循环（本层两处都做成"带出"，见 §11.12）。

        ⚠️⚠️ **车在运动时它不可靠**（这一点必须如实告诉现场，否则会以为命令失效）：
        `MAZE STOP` 只置一个"请退出"标志（`linkMazeAbort`），而标志的检查在
        `LESSON_Blind8()` 的 `while(1)` **顶部**；而主循环有 30 余处**非协作式阻塞等待**
        （`motion.c` 的 `while (cellOdom < dis)` 等）。车在运动时程序正卡在那些循环
        **内部**，标志根本轮不到 ⇒ 真机实测：`MAZE STOP` 回 `OK`，但 `mazeTask` 仍 `SEARCH`、
        `MAZE RUN` 仍 `ERR_BUSY`。
        ⇒ **可靠收车手段是【复位】（`MCU RESET`，整机软复位，BSS 清零把发车状态一起清掉）。**

        所以本方法**把实测状态一并返回**（`task`），让界面能当场判"到底收住没有"，
        而不是只看一个 `OK` 就以为收车成功（`OK` 只表示"命令被接受"）。
        """
        mc = self._need("timesync", "收车（带出迷宫）")
        if not self._features.get("maze_stop"):
            raise MouseError(
                "本机上游没有收车命令（`MAZE STOP`，2.1.1 新增）"
                "\n  ⇒ 旧版上游在发车之后**没有**任何复位发车状态的路径，只能断电或用【复位】。"
                "\n  ⇒ 升级 `RemoteControl` 到 2.1.1 及以上即可。")
        # 先记下"现在有几帧"，收车后**等到帧数涨了**才去读状态 —— 见下面 `_wait_fresh_frame`
        # 的说明（不等会读到停车前那一帧，把"收住了"误判成"没收住"）。
        n0 = self._frame_count(mc)
        r = mc.maze.stop(timeout=3.0)
        self.store.mark("mouse", "mouse.maze_stop", str(r.status), f"ok={r.ok}")
        if not r.ok:
            d = r.data if isinstance(r.data, dict) else {}
            raise MouseError(
                f"收车失败：{r.status}"
                + (f"\n  ⇒ {d.get('hint')}" if d.get("hint") else "")
                + (f"\n  ⇒ `MAZE STOP` 只在 MOUSE_DATA_LOG 档固件里实现，"
                   f"其余档位回 ERR_VALUE。当前设备：{mc.device}"
                   if str(r.status) == "ERR_VALUE" else ""))
        # 收车后**当场读一下任务机**：`OK` 只说明命令被接受，收住没有要看状态。
        fresh = self._wait_fresh_frame(mc, n0, timeout=2.5)
        task = None
        if fresh:
            try:
                task = mc.maze.state()
            except Exception:                                 # noqa: BLE001
                task = None
        # 三态：**判不了不是"没收住"**（没等到新帧就下结论，会把成功说成失败）。
        reliable = _task_wait(task) if (fresh and isinstance(task, dict)) else None
        return {"ok": True, "status": r.status, "state": task,
                "fresh": bool(fresh), "reliable": reliable}

    @staticmethod
    def _frame_count(mc: Any) -> Optional[int]:
        """拿到"迄今收到多少帧"这个单调计数（用于判"有没有新帧到"）。取不到给 None。"""
        try:
            return int(mc.telemetry.samples())
        except Exception:                                     # noqa: BLE001
            return None

    @classmethod
    def _wait_fresh_frame(cls, mc: Any, n0: Optional[int], timeout: float = 2.5) -> bool:
        """等到**比 `n0` 更新的一帧**到达（或超时）。返回"等到了没有"。

        ⚠️ 为什么必须等（探针实测，不是理论）：`MAZE STOP` 的应答到达时，上游环形缓冲里
        **最新那一帧仍然是"停车前"的**（帧是 1/RTT ≈ 5 Hz 拉来的，命令以约 300 ms 返回）。
        拿那一帧判"收住没有"，就会把**已经成功**的收车报成失败 ——
        模拟器上实测：命令回 `OK` 后立刻读 → `task=SEARCH`（看着没收住），
        1 s 后再读 → `task=WAIT`（其实收住了）。
        ⇒ 判据要等的是**条件**（有新帧），不是固定时长（本层纪律：等固定时长必然 flaky）。
        ⚠️ `n0 is None`（拿不到帧计数）⇒ 不假装知道，直接给一个保守的短等。
        """
        if n0 is None:
            time.sleep(0.8)
            return False
        deadline = time.monotonic() + max(0.5, float(timeout))
        while time.monotonic() < deadline:
            n = cls._frame_count(mc)
            if n is not None and n > n0:
                return True
            time.sleep(0.05)
        return False

    def rescue(self, verify: bool = True) -> dict:
        """**复位**：整机软复位（`MCU RESET` → 固件 `Sys_Soft_Reset()`）—— 卡死复位口。

        为什么需要它（2.2.0，真机复现的现场缺陷）：走迷宫的主循环里有大量**非协作式阻塞
        等待**（`motion.c` 的 `while (odomStr.cellOdom < dis) ;` 等 30 余处），它们等的是
        **物理进度**（编码器里程/传感器）。而 `STOP` 会把 `freemode` 置 1 ⇒ 电机 PWM 恒 0
        ⇒ 轮子永不再转 ⇒ **那些条件永远不成立** ⇒ "**越停越死**"：命令通道照常回 `OK`，
        但程序卡在函数内部，退出检查根本轮不到 ⇒ 只能断电。
        本方法给出一个不必断电的出口（真机实测：卡住 → 复位 → 回 `WAIT` 约 1.1 s → 可再发车）。

        ⚠️⚠️ **成功判据是复核 `rst == "SW"`，不是"ACK 收到"**：固件先回 ACK 再延迟 ~150 ms
        复位，ACK 可能与复位抢在一起而没出链路 ⇒ **`accepted=False` 不代表失败**。
        上游 `soft_reset(verify=True)` 已经做了这件事（返回 `data.verified`），本层不自己重写。

        ⚠️ 复位**只复位固件，不复位物理位置**：`mousePos`/`mapWall`/`stackIdx` 全被清零，
        而车还在迷宫原处。此时发车会按一张**错的地图**走（"我在起点 (0,0)"的假设不成立），
        既不认路也不认墙 ⇒ **复位后必须人工把电脑鼠放回起点角**（这一条要进界面文案）。

        ⚠️ 复位后**约 3 s 才有新遥测帧**：此前读到的可能是复位前的旧帧，或 `main()` 尚未
        初始化完的早帧（带 BSS 初值，例如 `freemode=False`）⇒ **判读状态要等新鲜帧**，
        读一次就下结论会拿到误导性的值。本方法把这个等待交给上游的复核循环，
        并把 `elapsed_s` 一起返回，让界面能显示"等了多久才等到"。
        """
        mc = self._need("timesync", "复位（整机软复位）")
        if not self._features.get("soft_reset"):
            raise MouseError(
                "本机上游没有复位命令（`mc.conn.soft_reset()`，2.2.0 新增）"
                "\n  ⇒ 旧版上游遇到固件卡死只能断电，或手工重连串口。"
                "\n  ⇒ 升级 `RemoteControl` 到 2.2.0 及以上即可。")
        r = mc.conn.soft_reset(verify=bool(verify))
        d = r.data if isinstance(r.data, dict) else {}
        self.store.mark("mouse", "mouse.rescue", str(r.status),
                        f"verified={d.get('verified')} rst={d.get('rst')} "
                        f"accepted={d.get('accepted')} elapsed={d.get('elapsed_s')}")
        out = {"ok": bool(r.ok), "status": str(r.status),
               "accepted": bool(d.get("accepted")), "rst": d.get("rst"),
               "verified": bool(d.get("verified")),
               "elapsed_s": d.get("elapsed_s")}
        if not out["verified"]:
            # 不抛错（`accepted=False` 不代表失败），但要把"没复核成"如实说出来，
            # 并且**给下一步** —— 否则现场只能看到"复位按钮点了没反应"。
            out["note"] = (
                "复位**没有复核到**（`rst` 不是 `SW`）"
                + (f"，当前 rst={d.get('rst')}" if d.get("rst") else "，设备没有回应")
                + "。可能是：旧固件（收到 `MCU RESET` 会回 `ERR_CMD`，复位原因保持 `POR`）、"
                  "复位后还没等到新帧、或链路已断。⇒ 先看这一行 `rst`："
                  "仍是 `POR` 说明**没复位成功**；`—` 说明设备不答话，检查串口。"
            )
        return out

    def wait_sync(self, samples: int = 2, timeout: float = 5.0) -> bool:
        """等至少 `samples` 个**可用的统一帧**到达（建立可用的时间映射）。

        时间锚（完整 u32 时基）来自 `TELEM` 应答里的 `ACK <seq> OK <upMs>`；
        每帧只带 `ms8`（低 8 位），靠 `timesync.resolve_ms8()` 同余还原。
        ⚠️ 2.1.0 起锚是**每次拉取都刷新**的（v2.0 只在 `STREAM ON` 时给一次），
        所以"失锁"能立刻自愈 —— 副作用是拉取模式下 RTT 抖动大，`drift_ppm` 拟合会更噪。
        """
        mc = self._mc
        if mc is None or not self._features.get("timesync"):
            return False
        return bool(mc.timesync.wait_sync(samples=samples, timeout=timeout))

    def rec_time_ms(self, rec: Optional[dict]) -> Optional[int]:
        """把**某一帧记录**换算成 PC 墙钟毫秒 —— 即"这一帧真正发生的时刻"。

        这是本次跟新要的**核心收益**：以前我们拿"帧到达主机的时刻"当时间
        （被蓝牙缓冲与抖动污染），现在用固件自己盖的发送时刻 + min-delay 偏移。

        上游给的是**本机单调秒**（`mc.timesync.frame_time`），墙钟由本层的
        `WallAnchor` 补上 —— 单调→墙钟只有本层知道（相机侧说的是 epoch）。
        取不到时间信息时返回 None，调用方**必须**自己决定回退策略（不许假装精确）。
        """
        mc = self._mc
        if mc is None or not isinstance(rec, dict):
            return None
        if not self._features.get("timesync"):
            return None
        try:
            t_mono = mc.timesync.frame_time(rec)
        except Exception:                                    # noqa: BLE001
            return None
        return mono_to_ms(t_mono)

    def rec_dev_ms(self, rec: Optional[dict]) -> Optional[int]:
        """这一帧"发车以来多少毫秒"（设备时基）。未发车时返回 None。"""
        mc = self._mc
        if mc is None or not self._features.get("timesync") or not isinstance(rec, dict):
            return None
        try:
            v = mc.timesync.dev_ms(rec)
        except Exception:                                    # noqa: BLE001
            return None
        return None if v is None else int(v)

    def rec_age_ms(self, rec: Optional[dict]) -> Optional[float]:
        """这一帧从**设备发出**到**本机收到**实际花了多久（单程延迟实测）。

        ⚠️ 上游 2.0.0 **删掉了** `TimeSyncAPI.frame_age_ms`（旧 `TIME` 流一并删除，
        现在锚由 `TELEM` 应答给、之后靠 `ms8` 同余还原）。
        所以这里**自己算**：`到达单调时刻 − 帧的发送单调时刻`。
        `_rx_mono` 是**上游在解析那一帧时盖的**（`transport/link.py`），不是现在取一次
        —— 拿"现在"去减会得到一个随查询时刻增长的假延迟。

        取不到就返回 `None`：调用方**必须**自己决定回退策略（不许假装精确）。
        """
        mc = self._mc
        if mc is None or not self._features.get("timesync") or not isinstance(rec, dict):
            return None
        t_send = self.rec_time_mono(rec)
        rx = rec.get("_rx_mono")
        if t_send is None or rx is None:
            return None
        try:
            return (float(rx) - float(t_send)) * 1000.0
        except (TypeError, ValueError):
            return None

    def rec_time_mono(self, rec: Optional[dict]) -> Optional[float]:
        """这一帧的**发送时刻（本机单调秒）** —— `rec_time_ms()` 的无墙钟版本。

        单列一个函数是因为墙钟那一段（`mono_to_ms`）在本层，而上游只给单调秒；
        要算"发送→到达"的差就必须用**同一个时基**，不能一边单调一边墙钟。
        """
        mc = self._mc
        if mc is None or not self._features.get("timesync") or not isinstance(rec, dict):
            return None
        try:
            return mc.timesync.frame_time(rec)
        except Exception:                                    # noqa: BLE001
            return None

    def link_stats(self) -> dict:
        """链路统计，含 **丢帧**（`seq_gap` / `seq_lost`）。

        丢帧是数据完整性的硬事实：帧头 SEQ 的缺口意味着"设备发了但我们没收到"。
        它必须能被回答，否则"数据齐不齐"只能靠猜。

        ⚠️ `mc.stats()` 返回的是**平铺**的 dict（各流计数 + `seq_gap` + `seq_lost`
        + `cmd` + `events` 一起放在顶层），不是 `{"link": {...}}`。以前按嵌套读过一次，
        结果四个字段全是 None —— 而 None 看起来像"没有丢帧"，正是最危险的错法。
        """
        mc = self._mc
        if mc is None:
            return {}
        try:
            st = dict(mc.stats())
        except Exception as e:                               # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}
        out = {
            "seq_gap": st.get("seq_gap"),
            "seq_lost": st.get("seq_lost"),
            "bad_crc": st.get("bad_crc"),
            "bad_sync": st.get("bad_sync"),
            "unknown_frame": st.get("unknown_frame"),
            "rx_bps": st.get("rx_bps"),
            # 2.0.0 的计数键：`unified`（唯一周期帧）/ `map`（墙图）/ `text`（文本行）
            # / `event`（其中的上行事件行）。旧键 `fast/mid/slow/time/dump_blk` 已不存在。
            "counts": {k: st[k] for k in ("unified", "map", "text", "event", "frames")
                       if k in st},
            "cmd": st.get("cmd"),
            "events": st.get("events"),
        }
        # 上行事件流完整性（2.0.0）：帧与事件**共用同一个 1 字节序号**，
        # 所以 `up_lost` 管全部上行消息、`ev_lost` 只管事件行。这是"事件丢没丢"
        # 唯一能回答的地方 —— 不许静默。
        if self._features.get("event_seq") and mc is not None:
            try:
                out["event_health"] = dict(mc.event_health())
            except Exception as e:                           # noqa: BLE001
                out["event_health"] = {"error": f"{type(e).__name__}: {e}"}
            try:
                out["events_incomplete"] = mc.events_incomplete
            except Exception:                                # noqa: BLE001
                out["events_incomplete"] = None
        if out["seq_gap"] is None and self._features.get("seq_stats"):
            out["note"] = "上游声明支持丢帧统计，但 stats() 里没有 seq_gap —— 上游改了键名"
        return out

    # ============================================================ 参数（2.4.0 起只剩一套）
    def params_grouped(self, refresh: bool = True) -> dict:
        """把参数表按**落哪一页 Flash / 属于哪一类**分组返回。

        **2.4.0 起只剩生产命名空间** —— 课程层（id 8–21 / page 116 / `mc.lesson`）整层删除；
        **2.5.0 起参数表扩到 41 项**（+17 项「运动控制」id 39–55，`PARAM_IDS_MOTION`）。
        于是现在有**四组**、**同一页 115**、**同一条通道 `mc.params`**：
        生产标定(1–7) / 红外测量(22–32) / 电源监控(33–38) / 运动控制(39–55)，合计 41 项，
        与固件 `LINK_P_NUM = 41` 严格相等。⚠️ 2.5.0 的 17 项**只对远程档固件生效**
        （比赛档运动代码读编译期常量，不吃参数表）—— 分组名**不写死**：
        组标签逐项取自上游 `PARAM_GROUPS`，上游再加组时这里自动跟随（初始 dict
        只定**阅读顺序**）。

        ⚠️ 分组**仍然返回**（而不是拍平成一张表）：它是界面的**阅读顺序**，
        也是判据 I16 的抓手。变了的是——**不再有"选错通道"这回事**了：
        以前课程参数必须走 `mc.lesson.set_params()`，混着传会写错页；
        现在 `write_via` 恒为 `mc.params.set`。所以 I16 的断言也随之改了
        （见 §11.15「判据与校验工具的同批改动」）。

        ⚠️ **读侧不再有不对称**：以前课程 14 项 `GET` 一律回 `ERR_NAME`
        （值读得到才怪），现在 41 项全都读得到 ⇒ `readable` 恒真。
        保留这个键是为了让旧前端/旧判据不至于 KeyError。

        ⚠️ **`desc` 是上游 `PARAM_META` 的说明列，但**过一遍 `_short_desc()` 清洗
        （去 ⚠️ 警示符 / 破折号 / 反引号，压空白）—— 那是写给文档看的标点，
        原样上界面会违反界面文案纪律（判据 B13/B23 扫得到）。内容**不改写**：
        功能句、默认值、覆写警示全部保留（与上游《参数名称-功能-调整表》同源）。

        ⚠️ `refresh=False` 表示**一次设备都不碰**（只用本地快照 + 静态元数据）。
        这一点必须守住：`get_raw()` 是**带 3 s 超时的实时往返**（上游
        `host/api/params.py`），而本方法在 `refresh=False` 时会被 `caps()` 同步调用 ——
        也就是跑在 **aiohttp 的事件循环上**。逐项各来一次，链路刚断时就是几十秒的
        **整服务冻结**：连纯内存的 `/health` 都不答。真踩过 ——
        线程栈里明明白白停在 `get_raw → client._wait_pending`。
        """
        mc = self._mc
        if mc is None:
            # 形状与正常返回保持一致 —— 半截的 JSON 形状会让前端分成两条代码路径
            return {"groups": {}, "count": 0, "connected": False,
                    "note": "未连接"}
        try:
            vals = mc.params.get_all(refresh=refresh) if refresh else mc.params.snapshot()
        except Exception as e:                               # noqa: BLE001
            raise MouseError(f"读参数失败：{type(e).__name__}: {e}") from e
        # 生产命名空间（`GET ALL` 覆盖到的 24 项），能读值、能读 Q16.16。
        prod_ids = {}
        try:
            from host.protocol.commands import PRODUCTION_PARAM_IDS   # noqa: F401
            prod_ids = set(PRODUCTION_PARAM_IDS)
        except Exception:                                     # noqa: BLE001
            prod_ids = set()
        if not prod_ids:
            # 取不到静态表就退化：`get_all` 覆盖到的就是生产侧。
            prod_ids = set(vals or {})
        groups: Dict[str, Dict[str, dict]] = {
            "生产标定": {}, "红外测量": {}, "电源监控": {}, "运动控制": {}}
        names: List[str] = []
        for n in (vals or {}):
            if n not in names:
                names.append(n)
        # 老上游上还有课程条目（读不到值），照样列出来并归到"课程调参"组 ——
        # 这样"从 2.3 退回去"时界面不会突然少一半条目。
        for n in sorted(self._lesson_param_ids or {}):
            if n not in names:
                names.append(n)
        if self._lesson_param_ids:
            groups["课程调参"] = {}
        for name in names:
            v = (vals or {}).get(name)
            g = (self._param_groups or {}).get(name)
            if g is None:
                g = "课程调参" if (self._lesson_param_ids
                                   and name in self._lesson_param_ids) else "生产标定"
            group = groups.setdefault(g, {})
            meta = None
            try:
                meta = mc.params.meta(name)
            except Exception:                                # noqa: BLE001
                pass
            q = None
            readable = name in prod_ids
            if refresh and readable:
                # 原始 Q16.16 值只在明确要求回读、且该名字在 GET 覆盖范围内时才去问设备。
                try:
                    q = mc.params.get_raw(name)
                except Exception:                            # noqa: BLE001
                    pass
            group[name] = {
                "value": v, "q": q, "readable": readable,
                "unit": (meta[0] if meta else ""),
                "decimals": (meta[1] if meta else None),
                "desc": self._short_desc(meta[2] if meta else ""),
                "id": self._param_ids.get(name),
                "write_via": ("mc.lesson.set_params" if g == "课程调参"
                              else "mc.params.set"),
                "flash_page": (FLASH_PAGE_LESSON if g == "课程调参" else FLASH_PAGE_PROD),
            }
        live = {k: v for k, v in groups.items() if v}
        return {
            "groups": live,
            "count": sum(len(v) for v in live.values()),
            "connected": True,
            # ⚠️ 这段话会**原样出现在界面上**（`params.js` 把 `note` 直接写进 `#p-top`），
            # 所以它必须遵守界面文案纪律：**不许有 Markdown 标记 / 解释性破折号 / 判据编号**
            # （判据 B13 会扫出来）。这里的行文只能是"状态 + 下一步点哪"。
            # 组数**现算**，不写死（2.4.0 是三组 24 项、2.5.0 起四组 41 项，上游还会加）。
            "note": (f"参数分{len(live)}组、同落在 Flash page 115，一次整表保存。"
                     if not self._lesson_param_ids else
                     "参数分四组、落在两个 Flash 页：生产侧 115，课程 116，需分别保存。"
                     "课程 14 项的值读不到（固件只对生产表答读取），可以写。"),
        }

    @staticmethod
    def _short_desc(raw: str) -> str:
        """上游 `PARAM_META` 说明列的**界面版**：只清洗字符，不改写内容。

        上游的 desc 是仿《参数名称-功能-调整表》"变量功能"列写的（功能句 +
        默认值 + 覆写警示），信息全部要保留 —— 但 ⚠️ 是图形标记、`——` 是
        界面禁用破折号（判据 B13/B23 扫的是**可见文本**，不区分"我们写的"
        和"透传的"）。功能句/默认值/警示原样保留，标点换成界面允许的。
        """
        s = (raw or "").replace("⚠️", " ").replace("——", "，").replace("`", "'")
        return " ".join(s.split())

    def set_params_grouped(self, values: Dict[str, float], readback: bool = True,
                           save: bool = False) -> dict:
        """按参数所在组**自动选写入通道**。混着传也行 —— 会拆成两次调用。

        ⚠️ **2.4.0 起只剩生产侧**，所以 `les` 恒空、走不到 `mc.lesson` 那条分支。
        这段分流**故意保留**：① 从 2.4 退回 2.3 时界面不用改；
        ② `mc.lesson` 是**被删的符号**，这里用 `self._features.get("lesson")` 判，
        绝不直接 `mc.lesson.xxx` —— 那会 AttributeError 冒成 500。

        ⚠️ 课程组默认 `readback=False`：固件 `GET` 历史上只查生产表，课程项回读必
        `ERR_NAME` ⇒ 带着回读去写会把**成功**的写入报成失败。这条规矩在 2.4 已无对象，
        但代码留着（同上，为"退回去也能跑"）。
        """
        mc = self._mc
        if mc is None:
            raise MouseError("未连接：请先连接再写参数")
        grp = self._param_groups or {}
        les_names = set(self._lesson_param_ids or {})
        prod = {k: v for k, v in values.items()
                if k not in les_names and grp.get(k, "生产标定") != "课程调参"}
        les = {k: v for k, v in values.items() if k not in prod}
        out: Dict[str, Any] = {"prod": [], "lesson": []}
        if prod:
            rs = mc.params.set_many(prod, readback=readback)
            out["prod"] = [{"name": getattr(r, "name", "?"), "ok": r.ok,
                            "status": r.status} for r in rs]
        if les:
            if not self._features.get("lesson"):
                raise MouseError(
                    "这些参数属于课程命名空间，但上游已经删掉了课程层（2.4.0）。\n"
                    f"  · 涉及的项：{sorted(les)}\n"
                    "  · 课程 14 项（id 8–21）已随整层删除，任何通道都写不了；\n"
                    "  · 生产参数（id 1–7 / 22 起，2.5.0 起 41 项）走 mc.params，不受影响。")
            # 课程侧**不做回读**（见 docstring）；只认 ACK。
            rs = mc.lesson.set_params(les, readback=False, save=save)
            out["lesson"] = [{"name": getattr(r, "name", "?"), "ok": r.ok,
                              "status": r.status} for r in rs]
        out["via"] = {"生产标定": "mc.params.set_many" if prod else None,
                      "课程调参": "mc.lesson.set_params" if les else None}
        out["readback"] = {"生产标定": bool(readback), "课程调参": False}
        for r in out["prod"] + out["lesson"]:
            self.store.mark("mouse", "mouse.param.write",
                            str(r.get("name")), f"ok={r.get('ok')} {r.get('status')}")
        bad = [r for r in out["prod"] + out["lesson"] if not r["ok"]]
        if bad:
            raise MouseError(f"{len(bad)} 项参数写入失败："
                             + "; ".join(f"{r['name']}={r['status']}" for r in bad[:4]))
        return out

    def save_params(self, group: str = "生产标定",
                    values: Optional[Dict[str, float]] = None) -> dict:
        """落 Flash。**2.4.0 起只有一套参数、一个页（115）、一条命令。**

        ⚠️ 课程组那条路在 2.4 已无对象（课程层删除）。代码保留是为"退回 2.3 也能跑"，
        且**必须**走能力探测 —— 直接碰 `mc.lesson` 会在新上游上 AttributeError。
        """
        mc = self._mc
        if mc is None:
            raise MouseError("未连接：请先连接再落盘")
        if group == "课程调参":
            if not self._features.get("lesson"):
                raise MouseError(
                    "上游已经删掉了课程调参整层（2.4.0），没有「课程调参」这一组可落盘。\n"
                    "  · 生产参数（2.5.0 起 41 项）都落在 Flash page 115；\n"
                    "  · 点生产侧那个 SAVE 即可。")
            vals = {str(k): float(v) for k, v in (values or {}).items()}
            if not vals:
                raise MouseError(
                    "课程参数的值**读不回来**（固件 `GET` 只查生产表），"
                    "所以无法「读出当前值再落盘」。\n"
                    "  · 请在参数表里填入要写入的数值，再点这一组的 SAVE —— 它会**写入并落盘**。")
            rs = mc.lesson.set_params(vals, readback=False, save=True)
            self.store.mark("mouse", "mouse.param.save", "lesson",
                            f"n={len(rs)} page={FLASH_PAGE_LESSON}")
            bad = [r for r in rs if not r.ok]
            if bad:
                raise MouseError(f"{len(bad)} 项课程参数落盘失败")
            return {"ok": True, "group": group, "saved": len(vals),
                    "flash_page": FLASH_PAGE_LESSON}
        backup = mc.params.backup()
        r = mc.params.save()
        self.store.mark("mouse", "mouse.param.save", "prod",
                        f"backup={backup} ok={r.ok} page={FLASH_PAGE_PROD}")
        if not r.ok:
            raise MouseError(f"参数落盘失败：{r.status}\n  · 已备份一份于 {backup}")
        return {"ok": True, "group": group, "backup": backup,
                "flash_page": FLASH_PAGE_PROD}

    # ============================================================ 统一帧码表 / 迷宫状态
    # 这里原先挂的是「组测发送（SNAP，上游 1.5.0）」那一节；**2.0.0 把 `SNAP` 整个删除了**
    # （理由见 §11.9：`SNAP` 存在的唯一依据是"物理速率不足"，而单流全量帧已经每帧都是快照）。
    # 留在下面的是它的**替代物**：一份"这条流里有什么"的静态码表（零设备访问）。

    def meas_meta(self) -> dict:
        """统一帧的**通道清单** + 迷宫**静态码表** —— 一次设备都不碰。

        为什么要有这么一条"纯元数据"的端点：界面在**没连接**时也要能把
        "这条流里有哪些量、路口类型有哪 8 种"画出来。否则未连接时整页空白，
        而这一页的价值恰恰是"连之前就知道要读什么"。

        2.0.0 起**没有 SNAP**：全部测量量都在唯一的那条统一帧里（**2.0.0 是 39 个、
        2.3.0 起是 49 个**扁平通道 —— 数量从协议层取，不写死）。
        所以这里的 `channels` 就是这条流的列名 —— 它同时是"界面上能显示些什么"的清单。

        `spurt` 是**比赛冲刺码表**（2.3.0 起有）：冲刺档位的名字。
        它和 `maze` 一样是**静态表**、零设备访问 —— 于是「比赛」页在**没连上**的时候
        也能把"要看哪几个量、档位各叫什么"画出来（这一页的价值正在于此）。
        """
        maze: Dict[str, Any] = {}
        spurt: Dict[str, Any] = {}
        try:
            from host.protocol.codec import (DIR_NAMES, FLAG_NAMES,
                                             MAZE_TASK_NAMES, MOTION_TASK_NAMES,
                                             ROAD_KINDS, ROAD_NAMES)
            from host.api.maze import JUNCTION_KINDS
            maze = {
                "tasks": {str(k): v for k, v in MAZE_TASK_NAMES.items()},
                "roads": {str(k): v for k, v in ROAD_NAMES.items()},
                "road_kinds": dict(ROAD_KINDS),
                "motions": {str(k): v for k, v in MOTION_TASK_NAMES.items()},
                "dirs": {str(k): v for k, v in DIR_NAMES.items()},
                "flags": {str(k): v for k, v in FLAG_NAMES.items()},
                "junctions": list(JUNCTION_KINDS),
            }
            # 前墙两档码表（2.3.0）：挂在 maze 下，让「无记忆走迷宫」页在**未连接**时
            # 也能把两档的名字画出来（与 spurt.front 是同一张表，只是那一页也用它）。
            # `FRONT_NAMES` 是 2.3.0 才有的符号 ⇒ getattr 兜底（老上游上少这张表不是故障）。
            from host.protocol import codec as _cd0
            maze["fronts"] = {str(k): v for k, v in
                              (getattr(_cd0, "FRONT_NAMES", {}) or {}).items()}
        except Exception as e:                                # noqa: BLE001
            maze = {"error": f"{type(e).__name__}: {e}"}
        try:
            # `SPURT_MODE_NAMES` 与 `FRONT_NAMES` 都是 2.3.0 才有的符号 ⇒ 一律 getattr 兜底
            # （老上游上少这两张表**不是故障**，只是那一页画不出名字）。
            from host.protocol import codec as _cd
            spurt = {
                "modes": {str(k): v for k, v in
                          (getattr(_cd, "SPURT_MODE_NAMES", {}) or {}).items()},
                "front": {str(k): v for k, v in
                          (getattr(_cd, "FRONT_NAMES", {}) or {}).items()},
            }
        except Exception:                                     # noqa: BLE001
            spurt = {}
        chans = list(self._unified_channels or [])
        if not chans and self._mc is not None:
            try:
                chans = list(self._mc.telemetry.channels())
            except Exception:                                 # noqa: BLE001
                chans = []
        hz = None
        if self._mc is not None:
            try:
                hz = self._mc.telemetry.hz()
            except Exception:                                 # noqa: BLE001
                hz = None
        return {
            "channels": chans,
            "stream": "unified",
            "hz": hz,
            "maze": maze,
            "spurt": spurt,
            "features": {"unified": self._features.get("unified", False),
                         "maze_state": self._features.get("maze_state", False),
                         "event_seq": self._features.get("event_seq", False)},
        }

    # ============================================================ 迷宫（实验 8，上游 2.0.0）
    # **状态从统一帧读**：`mazeTask` / `posX,posY` / `wallLeft,Front,Right` / `goalX,goalY` /
    # `stackIdx` / `motionTask`+`motionStep` 都在每一帧里（一份帧就是全量快照）。
    # 所以本层不再"攒事件"：
    #   ① 在屏看它此刻在哪 —— `mc.maze.state()`（零设备访问，纯读最新帧）；
    #   ② "它刚才识别到了什么" —— 由**主机差分相邻帧**得到（`task_changes()`）；
    #   ③ 路口类型 —— `classify_road()` 在主机侧用三位墙原语复刻（固件不再上报）。
    # ⚠️ 差分的**前提是相邻帧连续**：`SEQ` 跳变时该段边沿不可信，`task_changes()`
    # 内部已按"未知"处理，本层不伪造。

    def maze_reconstruct(self, seconds: float = 30.0) -> dict:
        """把**最近一段**帧重建成墙图 —— 界面中栏的"重构示意"实时版。

        数据来自**上游的环形缓冲**（默认 30 s），按需算、**零常驻开销**：
        不在记录时也随时能点一下看"它最近走过哪儿、认为哪儿有墙"。

        为什么不在这里自己常驻攒帧：那等于让服务端**为了没人看的数据一直做功**。
        整盘的完整重建归复盘（`maze_rec.rebuild_map` 从记录文件重放）。
        """
        mc = self._mc
        if mc is None or not mc.is_connected:
            return {"n": 0, "note": "未连接"}
        try:
            arr = mc.telemetry.history_dict(seconds=float(seconds))
        except Exception as e:                                # noqa: BLE001
            return {"n": 0, "error": f"{type(e).__name__}: {e}"}
        keys = ("posX", "posY", "dir", "wallLeft", "wallFront", "wallRight")
        n_frames = 0
        for k in keys:
            v = arr.get(k)
            if v is not None and len(v):
                n_frames = len(v)
                break
        try:
            n = int(mc.maze.mazetype())
        except Exception:                                     # noqa: BLE001
            n = 7
        m = MazeWallMap(n)
        if n_frames:
            # 逐帧喂。⚠️ **不能用 `latest()`**：重建要的是**序列**（走过的每一格），
            # 而不是最后那一帧的瞬时值 —— 用 latest 会得到"只有终点一格"的图。
            for i in range(n_frames):
                m.feed({k: (arr[k][i] if k in arr and i < len(arr[k]) else None)
                        for k in keys})
        out = m.to_json()
        out.update({"frames_used": n_frames, "window_s": float(seconds),
                    "mazetype": n, "source": "ring"})
        # 固件自述的那份（对账用）—— 只是本地缓存读，不发命令
        try:
            fm = mc.maze.last()
        except Exception:                                     # noqa: BLE001
            fm = None
        if fm:
            out["firmware"] = {"grid": fm.get("mapWall"),
                               "visited": fm.get("mapVisited"),
                               "pos": [fm.get("posX"), fm.get("posY")],
                               "dir": fm.get("direction")}
        return out

    def maze_view(self) -> dict:
        """当前迷宫状态 + 位置 + **迁移序列**。**这一条不发任何命令**（全是本地缓存）。"""
        mc = self._need("maze_state", "迷宫状态（实验 8）")
        st = mc.maze.state()
        try:
            pos = mc.maze.position()
        except Exception:                                     # noqa: BLE001
            pos = None
        try:
            mazetype = mc.maze.mazetype()
        except Exception:                                     # noqa: BLE001
            mazetype = None
        try:
            changes = [{"t": t, "from": a, "to": b}
                       for t, a, b in mc.maze.task_changes(50)]
        except Exception:                                     # noqa: BLE001
            changes = []
        try:
            ev_incomplete = mc.events_incomplete
        except Exception:                                     # noqa: BLE001
            ev_incomplete = None
        try:
            road_name = mc.maze.road_name()
        except Exception:                                     # noqa: BLE001
            road_name = None
        return {"state": st, "position": pos, "mazetype": mazetype,
                "changes": changes, "roadName": road_name,
                "eventsIncomplete": ev_incomplete,
                "spurt": self._spurt_of(mc)}

    def _spurt_of(self, mc) -> Optional[Dict[str, Any]]:
        """**比赛冲刺状态**（上游 2.3.0 起进帧，纯只读监视量）—— 见 `maze.spurt()`。

        为什么单独包一层而不是直接 `mc.maze.spurt()`：
          · 老上游（≤2.2.1）**没有**这个方法 ⇒ 裸取会 `AttributeError`，而报出来
            看着像"上游改名了"，指不到方向（纪律 6 / 14）；
          · 这些量**比赛档固件才有意义**，远程档不跑冲刺时恒为"未选 + 各标志 0" ⇒
            未连接 / 拿不到时回 `None`（三态：判不了 ≠ 全是 0）。
        """
        fn = getattr(mc.maze, "spurt", None)
        if not callable(fn):
            return None
        try:
            return fn()
        except Exception:                                     # noqa: BLE001
            return None

    def maze_history(self, n: Optional[int] = None) -> dict:
        """任务**迁移序列**（时间正序）：`(时刻, from, to)`。不发命令。

        这就是"它刚才识别到了什么"的替代品 —— 旧版靠固件的 `MAZE EV` 事件流，
        2.0.0 删掉事件流后改由主机差分相邻帧取得（见 `host/api/maze.py`）。
        """
        mc = self._need("maze_state", "迷宫状态（实验 8）")
        ch = [{"t": t, "from": a, "to": b} for t, a, b in mc.maze.task_changes(n)]
        return {"changes": ch, "count": len(ch)}

    def maze_summary(self) -> dict:
        """把"最新帧 + 迁移序列"汇总成一份可打印/落盘的统计（含人话版）。"""
        mc = self._need("maze_state", "迷宫状态（实验 8）")
        out = {"summary": mc.maze.summary()}
        try:
            out["explain"] = list(mc.maze.explain() or [])
        except Exception:                                     # noqa: BLE001
            out["explain"] = []
        return out

    def maze_reset(self) -> dict:
        """清**本层**的迁移差分缓冲（`mc.maze.clear_changes()`）。

        2.0.0 删除了 `MAZE RST`（固件侧统计随之失去上报通道），所以这里**只清本层缓存**，
        界面措辞也照此写 —— 不许说成"清了固件统计"。
        """
        mc = self._need("maze_state", "迷宫状态（实验 8）")
        try:
            mc.maze.clear_changes()
            okk = True
        except Exception:                                     # noqa: BLE001
            okk = False
        self.store.mark("mouse", "mouse.maze.clear", "", f"ok={okk}")
        return {"ok": okk, "note": "只清本层的任务迁移差分缓冲（固件侧无统计可清）"}

    # ============================================================ 电机实验 / 课程实验
    # ⚠️ 2.4.0 起上游把**课程调参实验整层删了**，只剩下「电机实验」（`mc.lab`）。
    #    下面 `lesson_*` 那一组**故意保留**（不是死代码，是"退回 2.3 也能跑"的兼容面）：
    #      · 每个入口第一句都是 `if not self._features.get("lesson")` ⇒ 新上游上
    #        **一行上游代码都不碰**，直接回一句解释得清楚的错；
    #      · 绝不写 `mc.lesson.xxx` 裸取 —— 那是**被删的符号**，会 AttributeError，
    #        报出来看着像"上游改名了"，指不到方向（纪律 6 / 14）。
    def guard(self, what: str) -> None:
        """**需要电机转动的前置检查**（上游 `mc.motor_guard`）。

        为什么必须有这道闸：安全测试固件下 `MOVE`/`CAL` 的状态机照常推进、也照常回
        ACK，只是电机不转 —— 调用方会一直等到超时（角度标定默认 **90 s**）才失败，
        而失败原因看起来像"设备没反应"。上游能提前用编译期标志判定，
        把 90 秒的困惑换成一行的明确原因。

        通过则**什么也不做**；不通过则抛 `MouseError`（消息里带上游给的 hint）。
        """
        mc = self._mc
        if mc is None:
            raise MouseError("未连接：请先连接")
        if not self._features.get("motor_guard"):
            return
        res = mc.motor_guard(what)
        if res is None:
            return
        hint = res.data.get("hint") if isinstance(res.data, dict) else ""
        self.store.mark("mouse", "mouse.motor_guard", what, res.status)
        raise MouseError(f"该动作已被拦截：{what}\n  · {hint or res.status}")

    def lab_status(self) -> dict:
        mc = self._need("lab", "电机实验")
        return dict(mc.lab.status())

    def lab_run(self, sp_l: int = 40, sp_r: int = 40, ms: int = 2000, ir_mm: int = 10,
                sample_hz: float = 50.0, on_sample=None) -> dict:
        """跑一次带**双护栏**的远程运动，并采下运动期间的统一帧样本。

        2.0.0 起**没有"停止后整块回传"**（`SCOPE`/`DUMP` 整套删除）：统一帧本身
        就是全量快照，所以运动**进行中**就有数据 —— `sample_hz` 只是
        "每秒钟从最新快照里抄几个点下来"。⚠️ 2.1.0 起帧率由链路往返决定
        （真机 4.4~6.5 Hz），所以 `sample_hz=50` 并不意味着真能采到 50 个不同的点 ——
        中间那些拍拿到的是同一个快照（不造数、不插值）。

        ⚠️ **轮子必须悬空**。护栏（时间 + 红外）全在固件 1 ms 中断里判定，
        上位机被 kill 也不会失控 —— 但那是"不失控"，不是"不会撞墙"。
        """
        self.guard(f"电机实验 spL={sp_l} spR={sp_r} {ms}ms 红外{ir_mm}mm")
        mc = self._need("lab", "电机实验")
        self.store.mark("mouse", "lab.run.start", "",
                        f"spL={sp_l} spR={sp_r} ms={ms} ir_mm={ir_mm} hz={sample_hz}")
        res = mc.lab.run(sp_l=sp_l, sp_r=sp_r, ms=ms, ir_mm=ir_mm,
                         sample_hz=sample_hz, on_sample=on_sample)
        out = _asdict(res)
        self.store.mark("mouse", "lab.run.done", "",
                        f"reason={out.get('reason')} ms={out.get('ms')}")
        try:
            out["explain"] = mc.lab.explain(res)
        except Exception:                                    # noqa: BLE001
            pass
        return out

    def lab_fetch_log(self, on_progress=None) -> dict:
        """按固定频率采下**最新快照**若干秒 —— 旧 `fetch_log` 的替代（上游 2.0.0）。

        旧接口是"停车后从板载 1 kHz 环里整块捞"；2.0.0 没有板载环了，
        取而代之的是 `mc.lab.capture(seconds, hz)`（`docs/传输模式重构设计方案.md` §11.2）。
        """
        mc = self._need("lab", "电机实验")
        arr = mc.lab.capture(seconds=2.0, hz=50.0)
        n = len(arr or [])
        self.store.mark("mouse", "lab.capture", "", f"points={n}")
        return {"points": n, "samples": (arr or [])[:2000],
                "note": "2.0.0 起改为按频率采最新快照（板载整块回传已取消）"}

    def lab_estop(self) -> dict:
        """急停（锁存）。**随时可用，不依赖任何先决条件** —— 所以它不过 guard。"""
        mc = self._need("lab", "电机实验")
        r = mc.lab.estop()
        self.store.mark("mouse", "mouse.estop", "OP", f"ok={r.ok} {r.status}")
        return {"ok": bool(r.ok), "status": r.status}

    def lab_resume(self) -> dict:
        """解急停锁存并正常停车。**这是重新获得控制权的唯一入口。**"""
        mc = self._need("lab", "电机实验")
        r = mc.lab.resume()
        self.store.mark("mouse", "mouse.estop", "CLEAR", f"ok={r.ok} {r.status}")
        return {"ok": bool(r.ok), "status": r.status}

    def lesson_exps(self) -> list:
        if not self._features.get("lesson"):
            return []
        try:
            _api = __import__("host.api", fromlist=["LESSON_EXPS"])
            return [{"no": int(e.no), "name": e.name, "args": list(e.arg_names),
                     "defaults": list(e.defaults), "help": e.help}
                    for e in getattr(_api, "LESSON_EXPS").values()]
        except Exception:                                    # noqa: BLE001
            return []

    #: 课程层被上游删除时，所有课程口统一回这一句 —— 便于现场一眼认出来。
    _LESSON_GONE = (
        "上游 RemoteControl 2.4.0 已**删除课程调参实验整层**（编译档位 5 → 2，"
        "`MOUSE_LESSON_LAB` 连同 `lesson_lab.c` 751 行一并移除）。\n"
        "  · 三条命令 lesson.start / lesson.stop / lesson.status 已退出登记表；\n"
        "  · 课程 14 项参数（id 8–21）与 Flash page 116 一并释放；\n"
        "  · `Event.LESSON` 事件不再由固件发出。\n"
        "  · 算参纯函数（solve_ratio / error_ratio / theory_*）**保留**，"
        "已迁到 host/api/calc.py，本层的自动算参照常可用。")

    def lesson_status(self) -> dict:
        if not self._features.get("lesson"):
            return {"available": False, "reason": self._LESSON_GONE}
        mc = self._need("lesson", "课程实验")
        return dict(mc.lesson.status())

    def lesson_run(self, exp: int, capture: bool = True, sample_hz: float = 5.0,
                   on_progress=None, on_sample=None, on_timeout_plan=None, **kw) -> dict:
        """跑一次课程实验：启动 → 等 DONE → **实验期间本地捕获统一帧** → 自动算参。

        2.0.0 起曲线在**实验期间就地捕获**（`capture=True`），实验结束不再有
        "回传往返" —— `fetch_log`/`realtime_hz` 两个参数随之消失。

        ⚠️ 需要电机，所以先过 guard。固件侧每个实验都自带时间护栏。

        ⚠️ **2.4.0 起这条路对上游不存在了**（课程层删除）。这里显式拒绝并说清原因 ——
        不返回空壳，也不抛一个 `AttributeError: 'MouseClient' object has no attribute 'lesson'`
        （那种报错看着像"上游改名了"，**指不到方向**）。
        """
        if not self._features.get("lesson"):
            raise MouseError(self._LESSON_GONE)
        self.guard(f"课程实验 {exp}")
        mc = self._need("lesson", "课程实验")
        # ⚠️ 记 timeline 时**必须把回调剔除**：`json.dumps` 遇到函数会抛
        # `TypeError: Object of type function is not JSON serializable`，
        # 而那个报错会一路冒成 HTTP 400，看起来像"请求参数错了"——
        # 完全指不到"只是日志写不下"。实测踩过（drill 的 S7 就是这么红的）。
        self.store.mark("mouse", "lesson.run.start", str(exp),
                        json.dumps(_drops_callables(kw), ensure_ascii=False))
        res = mc.lesson.run(exp, capture=capture, sample_hz=sample_hz,
                            on_progress=on_progress, on_sample=on_sample,
                            on_timeout_plan=on_timeout_plan, **kw)
        out = _asdict(res)
        self.store.mark("mouse", "lesson.run.done", str(exp),
                        f"reason={out.get('reason')} metrics={bool(out.get('metrics'))}")
        try:
            out["explain"] = mc.lesson.explain(res)
        except Exception:                                    # noqa: BLE001
            pass
        return out

    def lesson_stop(self) -> dict:
        if not self._features.get("lesson"):
            raise MouseError(self._LESSON_GONE)
        mc = self._need("lesson", "课程实验")
        r = mc.lesson.stop()
        self.store.mark("mouse", "lesson.stop", "", f"ok={r.ok} {r.status}")
        return {"ok": bool(r.ok), "status": r.status}

    def lesson_fetch_log(self, on_progress=None) -> dict:
        """旧接口，2.0.0 起**没有**对应的上游方法。

        课程实验的数据在 `lesson_run(capture=True)` 期间就已被本层收下（`mc.lesson.run`
        返回的 `samples`/`metrics` 里），所以"事后再回传一次"这件事在新协议里不存在。
        这里**明确失败**而不是返回空数组 —— 空数组会被界面画成"零个点"，那是个谎。
        """
        raise MouseError(
            "2.0.0 起没有「课程实验日志回传」这条通道（SCOPE/DUMP 已删除）。\n"
            "  · 曲线在 `POST /api/v1/lesson/run` 期间**就地捕获**并随结果返回；\n"
            "  · 想再看一遍请重跑实验（`capture=true`），或从会话目录读 `mouse/` 下的落盘。")

    def lesson_theory(self) -> dict:
        """两个理论系数（里程 / 角度）。**只是起点** —— 车轮实际尺寸与陀螺灵敏度有偏差，
        以实测修正为准。"""
        out = {}
        for k, label in (("theory_track_ratio", "里程系数 TRACK*"),
                         ("theory_gyro_ratio", "角度系数 GYRORATIO")):
            fn = (self._api or {}).get(k)
            try:
                out[k] = {"label": label, "value": round(float(fn()), 6)} if fn else None
            except Exception:                                 # noqa: BLE001
                out[k] = None
        return out

    def lesson_suggest(self, exp: int, res: dict, truth: float) -> dict:
        """按实验类型 + **实测真值**，算出建议写回的参数（**纯计算，不改设备**）。

        这段编排是照 `host/host/lesson.py::_compute_suggestion` 抄的（上游 CLI 里的
        同一件事），但**数学用上游的纯函数** `solve_ratio` / `error_ratio` ——
        我们只负责"从界面上收真值、把结论摆出来、按对通道写回"。

        ⚠️ 实验 2/5 修正的是 **TRACKL/TRACKR/GYRORATIO，属于生产标定参数**
        （不在课程命名空间里）⇒ 写回必须走 `mc.params.set_many`。
        上游 CHANGELOG 专门提过这条：`mc.lesson.set_params()` 只接受课程命名空间。
        ⚠️ **2.4.0 之后这个口仍然有用，而且更要紧**：它算的是 TRACKL/TRACKR/GYRORATIO，
        **全部属生产标定参数**（走 `mc.params`），与课程层毫无关系 ——
        上游把算参函数专门从 lesson.py 迁到 `api/calc.py` 就是为了保住这条能力。
        所以这里**不再要求 `mc.lesson` 存在**，只要算参纯函数在（`calc` 能力）。
        """
        mc = self._ensure()
        solve = (self._api or {}).get("solve_ratio")
        errp = (self._api or {}).get("error_ratio")
        if solve is None or errp is None:
            raise MouseError(
                "上游缺少算参纯函数（solve_ratio / error_ratio）。\n"
                "  · 2.4.0 起它们在 host/api/calc.py（包也导出）；\n"
                "  · 取不到说明上游版本过老或源码不完整。")
        out: Dict[str, Any] = {"exp": int(exp), "truth": float(truth), "items": {}}
        try:
            if int(exp) == 2:
                for name, pulses in (("TRACKL", res.get("dL")), ("TRACKR", res.get("dR"))):
                    ratio = mc.params.get(name, timeout=2.0)
                    if ratio and pulses is not None:
                        reading = float(pulses) / float(ratio)
                        out["items"][name] = {
                            "old": round(float(ratio), 4),
                            "new": round(float(solve(ratio, reading, truth)), 4),
                            "reading_mm": round(reading, 2), "truth_mm": truth,
                            "err_pct": round(float(errp(reading, truth)), 2),
                            "write_via": "mc.params.set_many",
                        }
            elif int(exp) == 5:
                ratio = mc.params.get("GYRORATIO", timeout=2.0)
                ang = res.get("angle")
                if ratio and ang is not None:
                    out["items"]["GYRORATIO"] = {
                        "old": round(float(ratio), 4),
                        "new": round(float(solve(ratio, abs(ang), truth)), 4),
                        "reading_deg": abs(ang), "truth_deg": truth,
                        "err_pct": round(float(errp(abs(ang), truth)), 2),
                        "write_via": "mc.params.set_many",
                    }
            elif int(exp) == 6:
                out["straight"] = res.get("straight")
        except Exception as e:                                # noqa: BLE001
            out["error"] = f"{type(e).__name__}: {e}"
        out["ok"] = bool(out["items"]) or "straight" in out
        if not out["items"] and "straight" not in out:
            out["hint"] = ("这个实验没有可自动算出的参数（实验 3 看曲线指标 σ%/ts/tp，"
                           "实验 6 看直线度）。能算的是实验 2（TRACKL/TRACKR）与 5（GYRORATIO）。")
        if int(exp) in (2, 5) and out["items"]:
            # ⚠️ 必须取 **绝对值**：`error_ratio` 返回的是**带符号**的相对误差
            # （读数小于真值时为负）。直接拿带符号值比 3% 的话，-8.92% 也会"通过"
            # —— 那等于把"差得很多"报成"可接受"。实测踩到过。
            worst = max(abs(float(v["err_pct"])) for v in out["items"].values())
            out["accept"] = worst <= 3.0
            out["accept_note"] = (
                f"最大绝对相对误差 {worst:.2f}%（课程验收线 ≤3%："
                f"{'可接受' if worst <= 3.0 else '超线，建议重做实验或检查真值'}）")
        return out

    def lesson_apply(self, suggestion: dict, save: bool = False) -> dict:
        """把建议值写回设备。**按参数所在组自动选通道** —— 实验 2/5 改的是生产参数。"""
        vals = {k: v["new"] for k, v in (suggestion.get("items") or {}).items()}
        if not vals:
            raise MouseError("没有可写回的参数（先算一次建议值）")
        res = self.set_params_grouped(vals, readback=True, save=False)
        out = {"applied": vals, "via": res.get("via")}
        if save:
            groups = sorted({(self._param_groups or {}).get(k, "生产标定") for k in vals})
            saved = []
            for g in groups:
                if g == "课程调参":
                    continue          # 课程参数没有"单独落盘"，只有 set_params(save=True)
                saved.append(self.save_params(g))
            out["saved"] = saved
        self.store.mark("mouse", "lesson.apply", ",".join(vals),
                        json.dumps(vals, ensure_ascii=False))
        return out

    # ------------------------------------------------------------ 内部
    def _need(self, feature: str, what: str) -> Any:
        mc = self._ensure()
        if not self._features.get(feature):
            raise MouseError(
                f"上游不支持「{what}」（能力探测：{feature}=False）。\n"
                f"  ⇒ 请确认 paths.remotecontrol_host 指向的是 **RemoteControl 2.0.0**"
                f"（协议整体替换后的那份源码）。")
        return mc

    # ------------------------------------------------------------ 遥测采样（二进制）
    async def sampler_loop(self) -> None:
        """按 `SAMPLE_HZ` 把遥测窗口推给前端。

        为什么在这里而不是在 GET 轮询里：波形是"连续看"的东西，HTTP 轮询要么太慢
        要么就把服务端拖成忙等；而且它必须走**二进制帧**（判据 I10）。
        没有浏览器订阅时**一次都不发** —— 服务端不该为了没人看的数据做功。

        ⚠️ 顺带在这里做**实验 8 的自动收尾轮询**。为什么不直接在事件回调里收尾：
        收尾要**落盘**（IO），而回调跑在**上游调度线程**上 —— 在那里写文件会把
        1 kHz 调度链拖慢（那是本层最不能碰的一条线程）。所以回调只**置标记**，
        由这个循环（asyncio，10 Hz）看到标记后走线程池落盘。

        ⚠️ 注意这一段**必须放在 `mc is None / not connected` 的早退之前**：
        链路断了正是最该收尾的时候，而那时 `mc.is_connected` 已经为假。
        """
        dt = 1.0 / SAMPLE_HZ
        win_s = float(self.cfg.get("mouse.wave_window_ms", 10000)) / 1000.0
        while True:
            await asyncio.sleep(dt)
            await self._maybe_finish_maze_rec()
            mc = self._mc
            if mc is None or not mc.is_connected:
                continue
            if not self.bus._bin_subs:
                continue
            try:
                arr = mc.telemetry.history_dict(seconds=win_s)
            except Exception as e:                           # noqa: BLE001
                self.bus.log("warn", f"波形采样失败：{type(e).__name__}: {e}", source='mouse')
                continue
            if not arr:
                continue
            chans = [c for c in WAVE_CHANNELS if c in arr]
            if not chans:
                chans = sorted(k for k in arr if k != "t_ms")
            n = min(len(arr[c]) for c in chans) if chans else 0
            if n == 0:
                continue
            t = arr.get("t_ms")
            t0 = float(t[0]) if t is not None and len(t) else 0.0
            dt_ms = ((float(t[1]) - float(t[0])) if t is not None and len(t) > 1 else 0.0)
            data = [[float(arr[c][i]) for c in chans] for i in range(n)]
            self.bus.publish_bin(
                {"type": "mouse.wave", "channels": chans, "t0_ms": t0, "dt_ms": dt_ms,
                 "shape": [n, len(chans)], "frozen": False},
                f32_2d(data))
            # 读数条（JSON，小）：**单流**统一帧的一份全量快照（39 个扁平通道 +
            # 上游在解码时顺手算出来的派生键，如 `dirName`/`mazeTaskName`/`roadName`）。
            # 2.0.0 没有 fast/mid/slow 之分 —— 一份帧就是全量。
            # `keep=64` 是有意的：全部标量键是 52 个，裁到 40/48 会把派生键悄悄砍掉，
            # 前端那个字段就永远是 `—`（`_small` 的注释里有详细说明）。
            rec = mc.telemetry.latest() or {}
            self.bus.publish("mouse.telemetry", {
                "unified": _small(rec, 64),
                "channels": list(self._unified_channels or []),
                "rate": self._rate_snapshot(mc),
                "bps": mc.telemetry.bytes_per_second(),
                "streaming": mc.telemetry.is_streaming(),
                "flags": _safe(mc.telemetry.flags),
                "events": self._events_snapshot(mc),
            }, log=False)

    # ------------------------------------------------------------ 实验 8 记录

    async def _maybe_finish_maze_rec(self) -> None:
        """看到自动收尾标记就落盘。失败只记一行日志 —— **绝不让它把采样循环打停**。"""
        rec = getattr(self, "maze_recorder", None)
        if rec is None or not rec.recording:
            return
        try:
            st = rec.status()
        except Exception:                                     # noqa: BLE001
            return
        if not st.get("recording"):
            return
        # `auto_end` 只有在回调里被置上才说明"该收尾了"；`status()` 里没直接给，
        # 所以这里问一次内部的收尾原因（`recorder.auto_end_reason()`）。
        why = rec.auto_end_reason()
        if not why:
            return
        loop = asyncio.get_running_loop()
        try:
            out = await loop.run_in_executor(None, lambda: rec.stop(why=why))
        except Exception as e:                                # noqa: BLE001
            self.bus.log("warn", f"实验 8 自动收尾落盘失败：{type(e).__name__}: {e}", source='mouse')
            return
        if out.get("ok"):
            self.bus.log("info", f"实验 8 记录已保存（{why}）：{out.get('name')} · "
                                 f"{out.get('frames')} 帧 / {out.get('duration_ms')} ms"
                                 + ("　⚠️ 帧数偏少" if out.get("tiny") else ""), source='mouse')
            self.store.mark("mouse", "maze.record.saved", str(out.get("name") or ""),
                            f"frames={out.get('frames')} why={why}")

    # ------------------------------------------------------------ 实测速率

    #: 速率窗口长度（秒）。真机拉取帧率只有个位数 Hz（4.4~6.5），
    #: 所以窗口要够长才分得清"慢"与"断" —— 给 6 s（约 30 个样本）。
    #: ⚠️ 别按 217 Hz 估：那个数会让窗口看起来"1 s 就有 200 多个样本"，
    #: 从而把窗口设得短到看不出趋势（这正是 2.0.0 时代留下的错觉）。
    RATE_WIN_S = 6.0

    def _events_snapshot(self, mc: Any) -> Dict[str, Any]:
        """上行事件流完整性（2.0.0）：`STOPPED <reason> #n` 与遥测帧**共用一个序号**。

        `EVQ`（帧 flags bit7）就是"有事件行被丢弃"的硬事实 —— 界面必须显式报出来，
        否则会假装那份事件流是完整的。
        """
        out: Dict[str, Any] = {}
        try:
            out.update(dict(mc.event_health()))
        except Exception:                                     # noqa: BLE001
            pass
        try:
            out["incomplete"] = mc.events_incomplete
        except Exception:                                     # noqa: BLE001
            out["incomplete"] = None
        return out

    def _rate_snapshot(self, mc: Any) -> Dict[str, Any]:
        """统一帧的**实测**速率 + 数据年龄。纯内存读，零设备访问。

        三样东西分开报，因为它们回答不同的问题：
          · `hz`    —— 现在每秒真的来几帧（**自己量的**，见下）；
          · `age_ms` —— 最近一帧是多久以前的（"延时大不大"的直接度量）；
          · `nominal` —— 协议常量 `UNIFIED_HZ`，**只作参照**，界面不该拿它当真相。

        ⚠️ **2.1.0 起 `mc.telemetry.hz()` 本身就是实测值**（上游改成按最近 2 s 的到达间隔
        实算，无数据返回 0），所以以前那种"nominal = mc.telemetry.hz()"的写法**已经错了** ——
        它会把实测值显示成"标称值"，于是界面上"实测 XX（标称 XX）"两个数永远相等，
        看起来像"完全达到了额定速率"，而真相是 4.4~6.5 Hz vs 217 Hz 差约 50 倍。
        ⇒ 这里自己量（样本数差分），`nominal` 直接取协议常量。
        """
        try:
            n = int(mc.telemetry.samples())
            age = mc.telemetry.last_frame_age()
        except Exception:                                     # noqa: BLE001
            return {}
        now = time.monotonic()
        hist = self._rate_hist.setdefault("unified", [])
        hist.append((now, n))
        while len(hist) > 2 and (now - hist[1][0]) >= self.RATE_WIN_S:
            hist.pop(0)
        t0, n0 = hist[0]
        span = now - t0
        # 窗口太短就不给数（"判不了" ≠ 0 Hz）—— 三态纪律（判据 I11 的同源要求）。
        hz = (n - n0) / span if span >= 2.0 else None
        return {"unified": {
            "hz": (round(hz, 2) if hz is not None else None),
            # 协议常量，不是"上游此刻报的额定值"（见 docstring 的 2.1.0 说明）
            "nominal": self._unified_hz_nominal,
            "age_ms": (round(age * 1000.0) if age is not None else None),
            "samples": n,
            # 窗口内一帧都没来 ⇒ 拉取实际上是断的（与"慢"要分得开）
            "stalled": bool(span >= 2.0 and (n - n0) == 0),
            # 拉取器自己的状态（在途数/实测 hz/丢了几条）—— 只有 2.1.0 有，老上游给 {}
            "pull": _safe(lambda: dict(mc.stream.stats())),
            # 门面三态（off/stopwait/pace）。⚠️ 必须在这里**再端一次**：
            # `stats()` 里虽也有 `stream_mode`，但 `pull` 在老上游上整体是 {}、
            # 而"现在是什么模式"是界面分支的唯一依据 ⇒ 提到 unified 同级，
            # 让前端不用先探 pull 再探里面的键。
            "stream_mode": _safe(lambda: str(mc.stream.mode)),
        }}


# ---------------------------------------------------------------- 小工具

def _asdict(obj: Any) -> dict:
    """把上游的 dataclass 结果（`RunResult` / `LessonResult`）转成纯 JSON 能过的 dict。

    为什么要自己转：上游结果里带 `min`/`max` 这种**保留字**字段（`metrics` 里就有），
    而且可能有 numpy 标量。统一过一道，前端与落盘都不用操心。
    """
    if obj is None:
        return {}
    if is_dataclass(obj):
        raw = asdict(obj)
    elif isinstance(obj, dict):
        raw = dict(obj)
    else:
        raw = {k: v for k, v in vars(obj).items() if not k.startswith("_")} \
            if hasattr(obj, "__dict__") else {"repr": repr(obj)}
    return _jsonable(raw)


def _jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, str)):
        return v
    if isinstance(v, float):
        return round(v, 6)
    if hasattr(v, "tolist"):                                  # numpy 数组/标量
        try:
            return _jsonable(v.tolist())
        except Exception:                                     # noqa: BLE001
            return str(v)
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return str(v)


def _arrays_to_json(arr: Any, max_pts: int = 0) -> Optional[dict]:
    """上游回传的 1kHz 日志（dict[str, ndarray]）→ 可 JSON 的 dict。

    `max_pts=0` 表示全给；给正数则等间隔抽稀（波形页不需要 1200 个点全画，
    但**落盘要全量**，所以默认不抽）。
    """
    if arr is None:
        return None
    out: Dict[str, list] = {}
    try:
        items = (arr.items() if isinstance(arr, dict)
                 else [(k, getattr(arr, k)) for k in dir(arr) if not k.startswith("_")])
        for k, v in items:
            try:
                lst = v.tolist() if hasattr(v, "tolist") else list(v)
            except Exception:                                 # noqa: BLE001
                continue
            if max_pts and len(lst) > max_pts:
                step = max(1, len(lst) // max_pts)
                lst = lst[::step]
            out[str(k)] = _jsonable(lst)
    except Exception:                                         # noqa: BLE001
        return None
    return out or None


def _t(target: Any) -> Optional[str]:
    if target is None:
        return None
    if isinstance(target, str):
        return target
    if hasattr(target, "kind"):
        if target.kind == "tcp":
            return f"tcp://{target.host}:{target.tcp_port}"
        if target.kind == "replay":
            return f"replay://{target.path}"
        return target.port
    return str(target)


def _drops_callables(d: Any) -> dict:
    """把 kw 里的**回调函数**剔掉，只留能 JSON 序列化的东西。

    为什么必须剔：timeline 的 note 是 `json.dumps(kw)` 写的，而 kw 里常常带着
    `on_progress` / `on_sample` / `on_timeout_plan` 这些回调。函数不可序列化 ⇒
    抛 TypeError ⇒ 被错误中间件映射成 **HTTP 400**，看起来像"请求参数不对"，
    完全指不到真正的原因。日志宁可少记一个回调，也不能让整个请求失败。
    """
    if not isinstance(d, dict):
        return {}
    return {k: v for k, v in d.items() if callable(v) is False}


def _dev_ms_of(d: Dict[str, Any]) -> Optional[int]:
    """从设备的文本明细行里取"发车以来毫秒"。

    `MOVEDONE RUN <REASON> <ms> ...` 与 `LESSON DONE <exp> <REASON> <ms> ...` 都带 `ms`。
    有它就记进 timeline 的 `dev_ms` 列 —— 那是设备侧的真相，比"主机到达时刻"可信，
    也是与上游 `stream_*.csv` 对齐的唯一钥匙。
    """
    if not isinstance(d, dict):
        return None
    for k in ("ms", "devMs", "dev_ms"):
        v = d.get(k)
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, (int, float)):
            return int(v)
    return None


def _task_wait(state: Any) -> bool:
    """迷宫任务机是否**已经不在跑**（`mazeTask` ∈ {WAIT=0, START=1}）。

    为什么要拿**数值**判而不是拿 `taskName` 比字符串：码表是上游的自然语言
    （`MAZE_TASK_NAMES`），改名不该让"收车收住没有"这个判断失效。
    数值 `0/1` 才是结构事实（与 `docs/上位机整合设计方案.md` 的"断言绑结构"一致）。

    用在收车之后：`MAZE STOP` 回 `OK` **只说明命令被接受**，收住没有要看任务机
    （车在运动时它根本轮不到检查，见 `maze_stop()` 的说明）。
    """
    if not isinstance(state, dict):
        return False
    try:
        return int(state.get("task", -1)) <= 1
    except Exception:                                         # noqa: BLE001
        return False


def _short(d: Dict[str, Any], n: int = 120) -> str:
    parts = []
    for k in ("dL", "dR", "task", "frac", "ok"):
        if k in d:
            parts.append(f"{k}={d[k]}")
    if not parts:
        parts = [f"{k}={v}" for k, v in list(d.items())[:4]]
    return " ".join(parts)[:n]


def _small(rec: Dict[str, Any], keep: int = 40) -> Dict[str, Any]:
    """把一帧遥测裁到"够显示"的大小（读数条只需要这些）。

    ⚠️ 传 `keep` 时**要算够**：`telemetry.latest()` 给的是 `decode_unified()` 的完整
    dict —— 39 个通道**之后**还有一批派生键（`freemode`/`evq`/`dirName`/
    `mazeTaskName`/`roadName` 等）。裁短了不会报错，只会让前端的某个字段**永远是 `—`**
    （真踩过：`roadName` 被截在 48 之外）。2.0.0 的全部标量键是 **52** 个，所以调用处传 64。
    """
    if not isinstance(rec, dict):
        return {}
    out: Dict[str, Any] = {}
    for k, v in rec.items():
        if isinstance(v, (int, float, str, bool)) or v is None:
            out[k] = v
        if len(out) >= keep:
            break
    return out


def _safe(fn: Any) -> Any:
    """调用一个"读本地缓存"的方法；失败就返回 `{}`，绝不把采样循环打断。

    采样循环跑在事件循环上，一个抛异常的读会把这个 asyncio task **直接杀掉** ——
    表现是"页面还在、读数永远不动"，比一次报错难查得多。
    """
    try:
        return fn()
    except Exception:                                         # noqa: BLE001
        return {}
