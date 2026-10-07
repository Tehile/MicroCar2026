# 电脑鼠蓝牙上位机（host）

配套固件模块 `code_stm32f1_mouse/HARDWARE/link.c`。
完整设计见 [`../docs/蓝牙上位机设计方案.md`](../docs/蓝牙上位机设计方案.md)。

---

## 1. 快速开始

### 无硬件：接模拟器跑通全链路

```bash
cd host

# 终端 A —— 设备模拟器（协议与固件逐字节一致，自带仿真物理 + 7x7 迷宫探索）
python -m host.sim.mouse_sim

# 终端 B —— 上位机
python run_host.py
```

在连接下拉框里选 **“[模拟器] TCP 127.0.0.1:5555”** → 点“连接”。
模拟器会自动开始探索迷宫，你应当立刻看到实时状态读数刷新、迷宫逐步显形。

### 有硬件

1. 与蓝牙 SPP 模块配对（Windows 会在“设备和打印机”里生成一个**传出** COM 口）
2. 点“刷新”，下拉框里选带 `[蓝牙]` 标记的那个口
3. 点“连接”；若 1 秒内没数据，用底部的文本调试台敲 `PING` 回车，应回 `PONG 5 216 7 512 <rst>`

### 标定 / 采集阶段：不起 GUI，直接看实时数据

```bash
python -m host.monitor                       # 自动挑串口；终端仪表盘，不需要 PySide6
python -m host.monitor COM7 --expect-safe    # 并核对固件是否处于安全测试模式(电机禁用)
python -m host.monitor tcp://127.0.0.1:5555  # 无硬件：连模拟器
python -m host.monitor replay://sessions/2026-09-27_0151   # 回放历史会话
```

固件档位自 **v2.4 起只有两档**（`code_stm32f1_mouse/USER/main.h`）：

| 档 | 开关 | 能力 |
|---|---|---|
| **远程档** | `#define MOUSE_REMOTE 1` | 上位机全程接管：监视 + 参数读写 + 手动运动 + 远程发车/收车 + `MCU RESET` |
| **按键比赛档** | **什么都不定义** | 与比赛固件一致（按键 + 物理启动） |

远程档值守态默认 `freemode=1`（电机不输出），只有收到 `MOVE *` / `MAZE RUN` 才会动。
⚠️ v2.4 起**不再有「电机绝不可能动」的档** —— 静态标定时请把轮子架离地面。
详见 [`../docs/双档简化设计方案.md`](../docs/双档简化设计方案.md) §8。

### 标定

```bash
python -m host.cli calib gyro     --target COM7    # 陀螺零偏(车静止，约 1.5s)
python -m host.cli calib walldist --target COM7    # 车心到墙(车左侧平行贴墙)
python -m host.cli save           --target COM7    # 需要落 Flash 时再存(page 115)
python -m host.cli calib irdata   --target COM7    # 红外 8 值(4 个摆位逐点交互采)
python -m host.cli calib angle    --target COM7    # 角度系数(需电机，安全模式下会被拦下)
```

> **安全模式只能做静态的三项**（GYRO / WALLDIST / IRDATA）。需要车动起来的
> `angle` 与里程系数会被 `mc.motor_guard()` 直接拦下并说明原因，不会傻等 90 秒超时。
> 能力边界与切换步骤见 [`../docs/安全测试模式与实时监视.md`](../docs/安全测试模式与实时监视.md) 第 3 节。

### 电机远程控制实验（轮子悬空，双流）

固件烧**远程档**（`#define MOUSE_REMOTE 1`）。停止条件全在固件 1 ms 中断里，
上位机被 kill 也不会失控。

```bash
python -m host.motor_test COM7                       # 40cm/s 前进 2s, 红外阈值 10mm
python -m host.motor_test COM7 --sp 40 -40           # 原地转
python -m host.motor_test COM7 --ir -1               # 关闭红外护栏, 只验证时间护栏
python -m host.motor_test COM7 --estop               # 一键急停(随时可用)
python -m host.cli estop --resume --target COM7      # 解急停/正常停车
```

详见 [`../docs/电机远程控制与双流传输.md`](../docs/电机远程控制与双流传输.md)。

### 数据收集与时间同步（远程发车 + 完整采集）

固件烧**远程档**（`#define MOUSE_REMOTE 1`；含自走策略 + 远程发车）。
`ARM` 让电脑鼠从发车命令开始以 **1Hz 闪 LED** 并回传毫秒时基；上位机据此把**每一个接收到的
样本**都换算成"发车以来毫秒"—— 蓝牙链路抖动不再污染数据的时间轴。

```bash
python -m host.datalog COM7                      # 完整流程: 发车 -> 跑迷宫 -> 采集 -> 报告
python -m host.datalog COM7 --status             # 只看时间同步状态(偏移/频差/样本数)
python -m host.datalog COM7 --arm-only           # 只发车(武装计时 + 点亮 1Hz 基准灯)
python -m host.datalog COM7 --maze-only          # 只远程发车跑无记忆迷宫
python -m host.datalog COM7 --seconds 20 --out run.csv
python -m host.datalog COM7 --no-maze            # 只采集, 不让它自己跑
```

关键点: 统一帧每帧只带 `ms8`(低 8 位); `TELEM` 的应答里带**完整 u32 时基**作为锚(每次拉取都刷新),
主机据此做**同余还原**, 于是每个样本都能换算成"发车以来毫秒" —— 蓝牙抖动不再污染时间轴;
`mc.timesync` 用 **min-delay** 估偏移、最小二乘估**频差(ppm)**;
上行序号(帧与事件**共用**一个 1 字节计数器)的间隙被用来做丢帧/丢事件检测(`mc.stats()['seq_lost']`)。

详见 [`../docs/数据收集与时间同步.md`](../docs/数据收集与时间同步.md)。

#### ⚠️ 发车安全（2026-10-05 真机事故后的硬约束）

`[4]` 档的探索**假设"我在起点 (0,0)"**，且**固件对低电没有任何保护动作**。
两条约束必须守住：

1. **发车前必须确认电池充足。** 低电时红外发射管变暗 ⇒ 回波微弱 ⇒ 墙检测失效，
   同时电机驱动在跌落电压下行为不再确定。事故现场表现就是
   "**电机一直转、且对红外障碍物不再敏感**"，操作者被迫强行断电。
   → 由此加了**上位机硬闸**：`maze.run` 在登记表层带前置校验
   （`registry._precheck_maze_run`），`battMv < registry.LAUNCH_MIN_MV`（默认 7000 mV）
   时**拒绝发车**并回 `ERR_LOWBATT`（带实测电压）。这一层在 `send_cmd` 里执行，
   所以**脚本 / CLI / datalog 任何路径都绕不开**；确需台架强发走逃生舱
   `mc.send_raw("MAZE RUN")`（把车架离地面）。

2. **发车前电脑鼠必须物理摆在起点角**（且朝向正确）。
   **复位（软复位 / 断电）只复位固件、不复位物理位置** —— `mousePos`/`mapWall`/`stackIdx`
   全被清零而车还在原地，此时发车会按一张**错的地图**行驶：既不认路也不认墙。
   所以 `MCU RESET` 之后不要立刻再发车，先人工把车放回起点。

#### ⚠️ 收车：用 `MCU RESET`，别指望 `MAZE STOP`（真机实测 2026-10-05）

`MAZE STOP` 只置一个"请退出"标志（`linkMazeAbort`），而这个标志的检查在
`LESSON_Blind8()` 的 `while(1)` **顶部**；主循环却有 30 余处**非协作式阻塞等待**
（`motion.c` 的 `while (cellOdom < dis)` 等）。**车在运动时**程序正卡在那些循环
**内部**，标志根本轮不到 ⇒ 现场表现为：

```
MAZE STOP -> OK        但 mazeTask 仍 SEARCH
MAZE RUN  -> ERR_BUSY  (发车状态没被复位)
```

**可靠路径是 `MCU RESET`**（整机软复位，BSS 清零把发车状态一起清掉）：

```
python -m host.cli rescue --target COM7     # 卡住/失控时的收车与救援
# 或:  mc.conn.soft_reset(verify=True) / mc.conn.rescue()
```

实测：卡住 → `MCU RESET` → 回到 `WAIT`（**约 1.1 s，无需断电**）→ `MAZE RUN` 重新被接受。

两条配套注意：

- **复位后要约 3 s 才有新遥测帧**（此前读到的是复位前的旧帧，或 `main()` 尚未初始化完
  的早帧，例如 `freemode=False`）⇒ 判读状态要**轮询等到新鲜帧**，别读一次就下结论。
- **复位后不自动恢复运行**：机器人还在原处，需人工放回起点角。

### 迷宫状态（实验 8 无记忆走迷宫）

仅在**远程档**下有效。v2.0 起固件**不再单发迷宫帧/事件**（`MAZE EV` 与
`LINK_TYPE_MAZE` 已删除），迷宫状态改由主机侧从统一帧里的三位墙原语
（`wallLeft/wallFront/wallRight`）**逐帧复刻**：路口类型（直道 / 左弯 / 右弯 / T 型 / 十字 / 死路）、
朝向、坐标、任务迁移、岔路栈深度。

```bash
python -m host.cli maze state   --target COM7     # 最近一帧状态(来自统一帧)
python -m host.cli maze summary --target COM7     # 汇总(识别的路口/转弯/死路统计)
python -m host.cli maze spurt   --target COM7     # 冲刺档位/前行格数(只读监视)

# 发车(v2.4: 可选目标) —— 会动! 且带低电硬闸
python -m host.cli maze run --goal blind --target COM7   # 无记忆走迷宫(实验8, 默认)
python -m host.cli maze run --goal race  --target COM7   # 正式比赛入口(需有效 Flash 地图)
python -m host.cli maze stop --target COM7               # 收车(停车+复位发车状态+解急停)
```

上位机 `mc.maze`（墙图拉取 + 状态复刻）；`monitor` 收到统一帧后自动多出一节迷宫面板。
详见 [`../docs/迷宫状态机回传.md`](../docs/迷宫状态机回传.md)。

### 自检与回归

```bash
python selftest.py          # 协议断言(CRC/Q16.16/帧/自恢复/墙图/落盘)
python selftest.py --e2e    # 额外拉起模拟器做端到端回归
python contract_test.py     # 契约断言(登记表↔模拟器逐条对齐; 21 命令 / 2 帧 / 24 参数
                            #   + [S8]档位宏唯一性、[S9]Flash布局/注释闭合、[S2b]发车目标词)
python maze_smoke.py        # 迷宫状态断言(路口分类/状态复刻/发车-收车-再发车)
python motor_lab_smoke.py   # 电机远程控制断言(时间/红外护栏、急停锁存)
python timesync_smoke.py    # 时间同步断言(纯函数/发车/逐帧时间/丢帧/落盘/远程发车)
python facade_smoke.py      # 门面断言(MouseClient 全功能组 A–J)
python monitor_smoke.py     # 实时监视器断言(含 --expect-safe 正/反例、motor_guard 拦截)
python pull_smoke.py        # 拉取链路断言(停等: 在途恒<=1、停流后彻底不再收帧、锚刷新)
python uismoke.py           # 离屏构建 GUI、接模拟器、跑通并截图到 ../docs/ui_*.png
```

> 上表即「**八套件**」（v2.4 起，原九套件的 `lesson_smoke` 已随课程层删除）。
> `uismoke.py` 需要 PySide6；未装则跳过，其余七套无需 GUI 依赖。

> `selftest.py --e2e` 与 `uismoke.py` 会自己拉起模拟器，不需要手动开终端 A。

---

## 2. 依赖

```
PySide6 >= 6.6      # GUI
pyqtgraph >= 0.13   # 图形配置(启动器仍会初始化它)
pyserial >= 3.5      # 串口/蓝牙 SPP
numpy >= 1.26       # 环形缓冲
```

本机已有可直接使用的环境：**conda `stmlink`**（numpy 2.4.6 / pyserial 3.5 / PySide6 6.11.1 / pyqtgraph 0.14.0）。

---

## 3. 目录结构

```
host/
├── run_host.py              启动器(含暗色主题与 pyqtgraph 全局配置)
├── selftest.py              协议自检 + 端到端回归
├── contract_test.py         注册表契约(登记表↔模拟器逐条对齐 + 源码静态断言)
├── maze_smoke.py            迷宫状态冒烟(路口分类/状态复刻)
├── motor_lab_smoke.py       电机远程控制实验冒烟(双护栏/急停)
├── timesync_smoke.py        数据收集与时间同步冒烟(纯函数/发车/逐帧时间/丢帧/落盘)
├── facade_smoke.py          门面全功能冒烟
├── monitor_smoke.py         实时监视器冒烟
├── pull_smoke.py            遥测拉取链路冒烟(停等不变量/停发自愈)
├── uismoke.py               离屏 GUI 冒烟测试 + 截图
│
│   -- 真机诊断 / 验收脚本(需接真设备; 多数默认只读, 发车类须显式开关) --
├── maze_launch_probe.py     例程8 诊断/验收: 只读体检 / --launch 发车观察 / --cycle 发车状态复位验收
├── maze_launch_link_test.py **发车链路最小验收**: BLIND 与 RACE 各发一轮, 只判"链路是否通"(--launch)
├── raw_ping.py              零依赖探活(裸串口, 不经门面层) —— 区分"链路坏"与"上位机代码坏"
├── link_check.py            **链路层验收**: 往返顺畅度 / 重复指令(幂等重放) / 停止原因事件
├── launch_link_live.py      **运动中链路有效性探针**: 发车后在车运动期间持续收帧 + 主动插 PING(--launch, --target BLIND|RACE)
├── params_write_test.py     V2 参数验收: GET ALL 完整性 / SETQ 生效 / SAVE 后复位仍生效(--write [--strong])
├── params_persist_probe.py  **参数持久化定点探针**: 逐步骤打印 SETQ/SAVE/MCU RESET 的回读, 定位"SAVE 不持久化"
├── move_run_guard_test.py   V3 验收: MOVE RUN 双护栏(TIME/IR) + 运动中二次 MOVE 应 ERR_BUSY + ESTOP 锁存/STOP 解锁(--motor; --safe-ir N 给 T1 加保护阈值)
├── estop_latency.py         急停/停止的**实际生效延迟**测量(含拉取器开/关对照)
├── maze_relaunch_repro.py   复现/判别「发车→停止→再次发车被拒」(含卡死判别探针)
├── real_v21.py              v2.1 分阶段真机验证(指纹/单帧/停等拉取/停发自愈)
├── real_burst.py            成串请求回收率(停等设计的实测依据)
├── real_discriminate.py     非运动命令探活(定位"失联")
├── real_throughput.py       吞吐压测
├── requirements.txt
└── host/
    ├── protocol/
    │   ├── codec.py         CRC16 / Q16.16 / 帧打包解包 / 流式分流解析器
    │   └── commands.py      命令构造、ACK 状态机、超时重发、幂等、参数回读
    ├── transport/
    │   ├── ports.py         串口枚举 + 蓝牙 SPP 口识别
    │   └── link.py          串口/TCP 双模、后台读取线程、吞吐实测与自检
    ├── model/
    │   ├── ring.py          统一帧 numpy 环形缓冲 + 最新值 + 墙图 + 时间锚
    │   └── session.py       会话目录：CSV / raw.bin / meta.json / 参数备份
    ├── analysis/
    │   └── metrics.py       阶跃指标(超调/上升/整定/稳态误差)、跟踪误差、动作分段
    ├── registry.py          命令/帧的单一登记表(新增功能只改这里)
    ├── cli.py               命令行入口(每个子命令 = 一次 API 调用)
    ├── monitor.py           实时数据监视器(终端仪表盘)
    ├── motor_test.py        电机远程控制实验终端入口
    ├── datalog.py           数据收集台(远程发车 + 时间同步 + 完整采集)
    ├── replay.py            离线回放后端
    ├── api/                 门面 MouseClient + 子 API
    │   ├── client.py        门面本体
    │   ├── lab.py           mc.lab: 电机实验(远程运动)
    │   ├── calc.py         标定算参**纯函数**(理论系数/线性标定/阶跃/直行/红外误差)
    │   ├── maze.py          mc.maze: 墙图 + 迷宫状态(从统一帧复刻, 实验8)
    │   ├── timesync.py      mc.timesync: 时间同步 + 偏移/漂移拟合纯函数
    │   └── ...              conn / params / telemetry / stream / calib / move /
    │                        analyze / session
    ├── clock.py             统一单调时钟(数据链路算时间差一律用它)
    ├── ui/
    │   ├── main_window.py   装配与 40Hz 刷新
    │   ├── status_panel.py  实时状态读数(统一帧当前值)
    │   ├── fetch_panel.py   统一帧查看 / 导出 CSV
    │   ├── maze_view.py     7x7 网格 / 墙 / 朝向 / 轨迹
    │   ├── param_panel.py   参数面板(Q16.16 互显、回读校验、SAVE/RESET)
    │   ├── calib_wizard.py  5 项半自动标定向导
    │   └── console.py       文本调试台(显示设备 printf 输出)
    └── sim/
        └── mouse_sim.py     设备模拟器(协议一致 + 仿真物理 + 迷宫探索 + 时间同步)
```

---

## 4. 界面速览

| 区域 | 内容 |
|---|---|
| 顶栏 | 连接目标选择 / 状态 / **实测吞吐 vs 需求** / 统一帧计数 / 会话记录开关 |
| 左栏 · 实时状态 | 转速、姿态与误差、坐标、红外/墙位、电源与安全模式位（大字读数） |
| 右栏 · 统一帧查看 | 帧率与缓冲占用、最新一帧全部 39 通道、导出历史 CSV |
| 右栏 · 迷宫 | 7x7 网格、已探索区域、墙、当前位置与朝向三角、探索轨迹 |
| 右栏 · 标定向导 | 5 项，逐项给出摆放说明与结果 |
| 右栏 · 参数 | 7 项标定参数，十进制与 Q16.16 互显；写入自动回读校验；SAVE 前自动备份旧值 |
| 底部 | 控制条(开流/停流/墙图/TARE/STOP/ESTOP) + 实时读数条 + 文本调试台 |

---

## 5. 会话目录

每次点“开始会话记录”会建一个目录：

```
sessions/2026-09-25_0132/
├── meta.json               会话元数据(时间、固件版本、实测吞吐、计数)
├── text.log                原始 printf 文本流 + 时间戳
├── events.log              上行事件行(STOPPED / ESTOP TRIP) + 时间戳
├── stream_unified.csv      统一帧历史(t_host_ms, dev_ms, seq, 39 通道)
├── calib/report.md         标定报告
├── calib/params_before_*.csv  写 Flash 前的旧值备份
└── raw.bin                 原始字节流 —— 协议改了也能用新解析器重跑全部历史数据
```

CSV 可直接用 pandas 打开：

```python
import pandas as pd
df = pd.read_csv("sessions/2026-09-25_0132/stream_unified.csv")
df.plot(x="t_host_ms", y=["speedL", "speedR", "gyroAngle", "pidErr"])
```

---

## 6. 常见问题

**连上了但没有任何数据**
在文本调试台敲 `PING`。若回 `ERR_CRC` 说明链路通但字节被破坏——检查蓝牙模块的 UART 波特率是否 115200（模块侧，不是上位机侧）。
若完全无回应，见设计文档 §11 的模块探测方法。

**吞吐显示偏低**
顶栏显示“实测/需求”。统一帧 53 B/帧，**由主机按需拉取**（`TELEM`，严格停等）。可达帧率 = 1/RTT，**真机实测约 4.4~6.5 Hz**（≈230 B/s 载荷）—— 瓶颈是蓝牙透传的转发延迟，不是串口带宽；速率档 `RATE` 已删除，也没有软件限速。

**画面刷新慢**
实时读数页刷新很快；若觉得卡，可关掉右侧『统一帧查看』里那张全通道大表格。

**标定向导第 4 项（角度系数）一直不结束**
必须放在迷宫**角格**（相邻两面互相垂直的墙），否则找不到两个相邻的红外极小值。约 5–10 秒后应回报。

**SAVE 之后重启参数没变**
`SET` 只写 RAM；要断电保留必须点 `SAVE`。`RESET` 的效果同样在下次上电体现（运行中突变运动参数是危险的，所以刻意延后）。

---

## 7. 与固件的对接约定

上位机的字节级定义必须与 `HARDWARE/link.h` 一致。改动任一侧都要跑 `contract_test.py`
（它会把主机的 `U_*` / `FLAG_*` 常量与固件宏**逐项比对**）。关键常量在两处成对出现，改一处必须同步另一处：

| 常量 | 固件 | 上位机 |
|---|---|---|
| 帧开销 8B | `LINK_FRAME_OVERHEAD` | `codec.FRAME_OVERHEAD` |
| 帧类型码 | `LINK_TYPE_UNIFIED` / `LINK_TYPE_MAP` | `codec.TYPE_UNIFIED` / `codec.TYPE_MAP` |
| 统一帧长度 49B | `LINK_UNIFIED_LEN` | `codec.UNIFIED_LEN` |
| 统一帧字段偏移 | `LINK_U_*` | `codec.U_*` |
| 统一帧标志位 | `LINK_FLAG_*` | `codec.FLAG_*` |
| 生产参数 id 1..7 + 22..38 | `LINK_P_*` | `commands.PARAM_IDS`（= `PRODUCTION_PARAM_IDS`） |
| 档位开关（唯一） | `MOUSE_REMOTE` | 不涉及（主机只看 `PONG` 的 proto/fw） |
| 时间锚(ms8 同余还原) | 每条 `TELEM` 的 ACK 都带完整 u32 时基(每次刷新) | `store.set_anchor` / `mc.timesync` |
| 角度基准往复 10 次 | `LINK_SRV_REPS` | `calib_wizard.SRV_REPS` |
| 迷宫尺寸 | `MAZETYPE` | 由 `PONG` 上报，不写死 |

固件档位开关（`code_stm32f1_mouse/USER/main.h`）—— **v2.4 起只有两档**，且不再有派生宏：

| 宏 | 自走策略 | 用途 | 上位机入口 |
|---|---|---|---|
| `#define MOUSE_REMOTE 1` | 有（`MAZE RUN [BLIND\|RACE]` 触发） | **远程档**：监视 / 参数 / 手动运动 / 远程发车收车 / `MCU RESET` | `monitor` · `calib` · `motor_test` · `cli maze` · `datalog` |
| 都不定义 | 有（按键启动） | **按键比赛档**（与比赛固件逐字节一致） | — |

> 历史：v2.3 及以前有五档（`MOUSE_TEST_NO_MOTOR` / `MOUSE_TEST_MOTOR_LAB` /
> `MOUSE_LESSON_LAB` / `MOUSE_DATA_LOG` / 比赛），并用 `MOUSE_TEST_MODE`、
> `MOUSE_ESTOP_RECOVERABLE` 两个**派生宏**描述「实验档共同行为」。档位压成两档后
> 无共同行为可派生，两个派生宏一并删除；`contract_test` 的 `[S8]` 用**白名单**断言
> 钉死这件事（旧宏名在整个固件树出现次数必须为 0）。
