# RemoteControl — 电脑鼠蓝牙上位机

给 STM32F103 电脑鼠（竞速迷宫鼠）加一条**双向蓝牙链路**与一整套 **PC 工具链**，
把"调参、标定、监视、复现"从"串口 printf 肉眼观察"搬到电脑上。

> 当前版本　|　固件远程档 `PROTO 10 / FW 314`，比赛档 `PROTO 5 / FW 216`　|　host API `2.14.0`
>
> **底线约束**：两档共用同一份固件源码，**比赛档产物必须逐字节不变**
> （判据是 hex 哈希 `68ad6be2…8141`，不是看代码）。
>
> 模块分层与职责见 [`docs/模块组成总览.md`](docs/模块组成总览.md)；怎么验见
> [`docs/工具与校验.md`](docs/工具与校验.md)。

---

## 1. 这是什么

一条**命令严格停等**的 115200 串口/蓝牙 SPP 链路 + 一个 Python 上位机，让电脑端可以：
（遥测拉取统一为 `TelemetryPuller`：`n=1` 停等、`n≥2` 批式；**容量顶 13.3 Hz**）

| 能力 | 说明 |
|---|---|
| **实时监视** | 统一帧**载荷 30 B / 线上 38 B**，含转速、姿态、里程、坐标、红外/墙位、电源、迷宫与冲刺状态 |
| **建图增量**（v10） | 每帧捎带 3 条 × 2 B 的建图增量（自带绝对坐标），实时同步电脑鼠内部建图 |
| **参数在线调** | 远程档 **41 项**参数（Q16.16 精度），写后自动回读校验，`SAVE` 落 Flash page115 掉电保留 |
| **半自动标定** | 陀螺零偏 / 车心到墙 / 红外 8 值 / 角度与里程系数，带理论值与线性反解公式 |
| **手动运动** | `MOVE FWD/ROT/RUN`（双护栏：时间 + 红外），急停**锁存**，全在固件 1 ms 中断里兜底 |
| **远程发车/收车** | `MAZE RUN [BLIND\|RACE] [PULL n t]` 一条命令完成发车并启动恰好一批帧；`MCU RESET` 可靠收车 |
| **时间同步** | 把每一帧映射到"发车以来毫秒"，蓝牙抖动不再污染时间轴；LED 1 Hz 基准灯供独立校核 |
| **离线分析** | 阶跃指标、跟踪误差、按动作分段、直行质量、红外曲线拟合、电池跌落分析 |
| **无硬件可跑** | 设备模拟器协议逐字节一致，自带仿真物理与迷宫探索 |

**安全设计**（真机事故后的硬约束）：发车前上位机独立校验电池（`< 7000 mV` 拒绝发车）；
急停/停止命令会**让遥测拉取器让路**，确保命令独占链路即时生效；
收车必须**拉帧验证任务归零**，只看 `ACK OK` 不算停住。

---

## 2. 目录结构

```
RemoteControl/
├── code_competition - V6++/  ★ 现行工作副本（V6 算法 + 回程补搜 + 远程档，五 Target）
│   ├── USER/TEST.uvprojx    V6_0mm / V6_2mm / V6_4mm / V6_6mm / Target_Remote
│   ├── REMOTE/              链路层（仅远程档编译）：link.c 协议引擎 + link_port.c 平台适配
│   └── STRATAGY/ UTILITY/ HARDWARE/   比赛代码本体
├── code_competition - V6+/   V6 算法 + 远程档移植（V6++ 的前一步，保留作对照）
├── code_competition/        旧双档基线：Target 1 比赛档 / Target_Remote 远程档
│                            （红线 hex `68ad6be2…8141` 出自此处）
├── code_stm32f1_mouse/      课程基线（MAZETYPE 7），仅参考，非主线
├── code_stm32f1_mouse - 副本/  上游原始固件（含它自己的 .git），参照物，勿改勿删
├── host/                    PC 上位机（Python）
│   ├── run_host.py          启动 GUI
│   ├── *.py                 30 个脚本：契约/自检/模拟器套件/真机验收/安全（见工具与校验）
│   ├── host/                包：protocol / transport / model / api / ui / sim
│   └── README.md            ★ 上位机使用指南（装了怎么跑）
├── paramtwin/               参数孪生（从固件源码抽参数表 → 可回放/扫描的模型）
├── docs/                    ★ 本项目文档（见下，14 篇）
├── tools/                   参数工具链 paramgen.py + paramkit/
├── release/                 归档 zip + SHA256 台账
├── _backup/                 改动前源码快照（本地对照物，不入库）
├── build_all_modes.py           旧工程两档批量编译
├── build_competition_modes.py   旧工程双 Target 编译 + 比赛档 hex 红线校验
├── build_v6plus_modes.py        V6+ 五 Target 批量编译 + 红线
└── build_v6pp_modes.py          V6++ 五 Target 批量编译 + 红线（**日常用这个**）
```

> 四套固件工程**并存是有意的**：`V6++` 是当前工作副本，`V6+` 是它的前一步（远程档刚移植完），
> `code_competition` 是红线 hex 的出处与双档基线，`code_stm32f1_mouse` 是课程原版。
> 改哪一个、考不考红线，看那一份的 README 与 `build_*_modes.py` 的 `REDLINE` 常量。

---

## 3. 文档导航

文档按**总体 → 模块 → 接口 → 专题 → 工具**组织：

| 想看什么 | 去哪 |
|---|---|
| 项目功能简介（本文） | `README.md` |
| **模块组成总览**（分层拓扑 / 各模块职责 / 运行时模型 / 数据落点） | [`docs/模块组成总览.md`](docs/模块组成总览.md) |
| **线格式权威规格**（帧/命令/参数/错误码） | [`docs/通信协议规格.md`](docs/通信协议规格.md) |
| **可编程接口清单**（`mc.*` 方法 + 命令/帧/参数表） | [`docs/api_reference.md`](docs/api_reference.md) |
| **怎么验**（脚本清单 / 构建 / 压测 / 真机四条铁律） | [`docs/工具与校验.md`](docs/工具与校验.md) |
| **算法思路详解**（固件新增 + 上位机全收，供审阅） | [`docs/算法详解.md`](docs/算法详解.md) |
| 转向查表算法（`objectGoTo` 的曲线来源） | [`docs/转向查表算法.md`](docs/转向查表算法.md) |
| **V6+ 远程档**：考察与方案 / 落地记录 | [`docs/V6+工程远程档移植方案.md`](docs/V6+工程远程档移植方案.md)、[`docs/V6+工程远程档落地记录-20261105.md`](docs/V6+工程远程档落地记录-20261105.md) |
| **V6+ 远程档**：两个真机根因 | [`docs/V6+远程档参数接线修复-20261105.md`](docs/V6+远程档参数接线修复-20261105.md)、[`docs/V6+远程档红外冻结根因-20261105.md`](docs/V6+远程档红外冻结根因-20261105.md) |
| 参数名称-功能-调整速查（仿课件表） | [`docs/参数名称-功能-调整表.md`](docs/参数名称-功能-调整表.md) |
| **迷宫数据回传**（变量总表 / 增量通道 / 已知断点） | [`docs/迷宫数据回传与重建.md`](docs/迷宫数据回传与重建.md) |
| **链路稳定性压测**（固定 FPS / MTBF / 环境对比） | [`docs/链路MTBF压测方法.md`](docs/链路MTBF压测方法.md) |
| **历史决策**（已落地方案与代码落点） | [`docs/已落地方案与决策记录.md`](docs/已落地方案与决策记录.md) |
| **未实现 / 遗留**（`MAP LIVE`、冻结项、待判发现） | [`docs/未实现功能.md`](docs/未实现功能.md) |
| 上位机怎么跑（CLI / GUI / 标定 / 采集） | [`host/README.md`](host/README.md) |

---

## 4. 快速开始

### 无硬件：接模拟器

```bash
cd host
python -m host.sim.mouse_sim     # 终端 A —— 设备模拟器（TCP 5555）
python run_host.py               # 终端 B —— 上位机，连接下拉框选“[模拟器] TCP 127.0.0.1:5555”
```

### 有硬件

1. 与蓝牙 SPP 模块配对（Windows 生成一个**传出** COM 口）；
2. `host/` 里 `python -m host.monitor COM7`（终端仪表盘）；
3. 或 `python run_host.py` → 选带 `[蓝牙]` 标记的口 → 连接。

连接后第一条命令是 `PING`，应回 `PONG 10 314 16 1024 <rst>`（远程档指纹：
`PROTO 10` / `FW 314` / `MAZETYPE 16` / TX 环 1024 B）。第六字段 `<rst>` 是复位源
（`POR` 上电 / `SW` 软复位）。若 `PING` 永远无 `PONG`，先怀疑**烧错档**（比赛档无 `LINK_Init`）。

### 上位机依赖

```
PySide6 >= 6.6 · pyqtgraph >= 0.13 · pyserial >= 3.5 · numpy >= 1.26
```

详细步骤、标定流程、发车/收车、常见问题见 [`host/README.md`](host/README.md)。

---

## 5. 构建与测试

### 固件（两档）

```bash
python build_all_modes.py        # 编译远程档 + 按键比赛档（收尾停在远程档）
# 单档（Keil 命令行）：
"/c/Keil_v5/UV4/UV4.exe" -b "<绝对路径>\USER\Mouse.uvprojx" -o "<绝对日志>"
```

- **远程档**：`code_stm32f1_mouse/USER/main.h` 里 `#define MOUSE_REMOTE 1`
- **按键比赛档**：**什么都不定义**（产物与比赛固件逐字节一致）

### 固件（新比赛工程 code_competition，单工程双 Target）

```bash
python build_competition_modes.py   # 两档全量重编 + ROM/RAM/告警汇总 + 比赛档 hex 红线校验
# 单档（Keil 命令行）：
"/c/Keil_v5/UV4/UV4.exe" -b "<绝对路径>\USER\TEST.uvprojx" -t "Target_Remote" -o "<绝对日志>"
```

- **Target 1（比赛档）**：Define 仅 `STM32F10X_MD,USE_STDPERIPH_DRIVE`，源码零改动；
  hex 与移植前逐字节相同（红线 `68ad6be2…8141`，ROM 82040 / RAM 20480，脚本自动校验）
- **Target_Remote（远程档）**：上述 + `MOUSE_REMOTE,EN_USART1_RX`，产物落 `USER\Remote\`；
  远程档关闭 `FLASHSAVE` 板载飞行记录（让位 TELEM 实时流），RAM 20480→10960

### 上位机（30 个脚本，分类见 `docs/工具与校验.md`）

> ⚠️ 用 `C:/ProgramData/anaconda3/python.exe` 跑（托管 python 无 numpy）。

```bash
cd host
python selftest.py            # 协议断言（CRC/Q16.16/帧/自恢复/墙图/落盘）45 项
python contract_test.py       # 登记表↔模拟器逐条对齐 + 源码静态断言（278 项）
python facade_smoke.py        # 门面全功能（A–J）
python maze_smoke.py          # 迷宫状态复刻 / 路口分类
python monitor_smoke.py       # 实时监视器
python pull_smoke.py          # 拉取链路（停等不变量 / 停发自愈）
python pace_smoke.py          # 批式拉取（10Hz / 互斥 / 让路 / 断连检测）
python stream_unified_smoke.py  # 统一拉取器（n=1 退化 / 自节流 / 锚刷新 / 断连）
python timesync_smoke.py      # 时间同步（纯函数 / 发车 / 逐帧时间 / 丢帧 / 落盘）
python motor_lab_smoke.py     # 电机实验（双护栏 / 急停锁存）
python uismoke.py             # 离屏 GUI（需 PySide6）
```

`selftest.py --e2e` 与 `uismoke.py` 会自己拉起模拟器，不需要手动开终端 A。

另有**不接设备**的纯离线脚本（不计入上面十套件）：`test_evt_trigger_logic.py`（事件触发逻辑）、
`safe_stop_smoke.py`（**收车安全逻辑 13 项**：分级升级 / 新鲜帧判据 / 命令异常不中断 / 空帧不许谎报待命 / 停不住时必须诚实报警）。

---

## 6. 版本与两档

| 项 | 远程档 | 按键比赛档 |
|---|---|---|
| 开关 | `#define MOUSE_REMOTE 1` | 什么都不定义 |
| 指纹 | `PROTO 8 / FW 222` | `PROTO 5 / FW 216` |
| 参数表 | **41 项** | 24 项 |
| TX 环 | 1024 B | 256 B |
| 能力 | 监视 / 参数 / 手动运动 / 远程发车收车 / `MCU RESET` | 与比赛固件一致（按键 + 物理启动） |

⚠️ 两档**共用同一份 `link.c`**，共享宏（`LINK_PROTO_VER` / `LINK_FW_VER` / `LINK_P_NUM`）
必须 `#ifdef MOUSE_REMOTE` 门控。当前版本历史见 [`docs/通信协议规格.md`](docs/通信协议规格.md) §9。
