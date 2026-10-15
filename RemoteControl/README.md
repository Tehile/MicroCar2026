# RemoteControl — 电脑鼠蓝牙上位机

给 STM32F103 电脑鼠（竞速迷宫鼠）加一条**双向蓝牙链路**与一整套 **PC 工具链**，
把"调参、标定、监视、复现"从"串口 printf 肉眼观察"搬到电脑上。

> 当前版本 **v2.9.0**　|　固件远程档 `PROTO 8 / FW 221`，比赛档 `5 / 216`　|　API `2.9.0`
>
> **底线约束**：两档共用同一份固件源码，**比赛档产物必须逐字节不变**
> （判据是 hex 哈希 `f0a5f8637d96b61def2d…`，不是看代码）。

---

## 1. 这是什么

一条**严格停等**的 115200 串口/蓝牙 SPP 链路 + 一个 Python 上位机，让电脑端可以：

| 能力 | 说明 |
|---|---|
| **实时监视** | 一帧 57 B 的"统一帧"含 49 个通道：转速、姿态、里程、坐标、红外/墙位、电源、迷宫与冲刺状态 |
| **参数在线调** | 远程档 **41 项**参数（Q16.16 精度），写后自动回读校验，`SAVE` 落 Flash page115 掉电保留 |
| **半自动标定** | 陀螺零偏 / 车心到墙 / 红外 8 值 / 角度与里程系数，带理论值与线性反解公式 |
| **手动运动** | `MOVE FWD/ROT/RUN`（双护栏：时间 + 红外），急停**锁存**，全在固件 1 ms 中断里兜底 |
| **远程发车/收车** | `MAZE RUN [BLIND\|RACE]` 让车自己跑迷宫；`MCU RESET` 可靠收车 |
| **时间同步** | 把每一帧映射到"发车以来毫秒"，蓝牙抖动不再污染时间轴；LED 1 Hz 基准灯供独立校核 |
| **离线分析** | 阶跃指标、跟踪误差、按动作分段、直行质量、红外曲线拟合、电池跌落分析 |
| **无硬件可跑** | 设备模拟器协议逐字节一致，自带仿真物理与 7×7 迷宫探索 |

**安全设计**（真机事故后的硬约束）：发车前上位机独立校验电池（`< 7000 mV` 拒绝发车）；
急停/停止命令会**让遥测拉取器让路**，确保命令独占链路即时生效。

---

## 2. 目录结构

```
RemoteControl/
├── code_stm32f1_mouse/      固件（Keil MDK，STM32F103RB）
│   ├── HARDWARE/            link.c/.h 为本项目新增的链路层（其余为上游外设驱动）
│   ├── MOTION/ APP/ STRATEGY/ USER/   上游迷宫搜索/冲刺/控制（本项目仅"加可见性"）
│   └── docs/                上游原始文档（algorithm/architecture/rename_map）
├── host/                    PC 上位机（Python）
│   ├── run_host.py          启动 GUI
│   ├── selftest.py 等 8 个套件
│   ├── host/                包：protocol / transport / model / api / ui / sim
│   └── README.md            ★ 上位机使用指南（装了怎么跑）
├── docs/                    ★ 本项目文档（见下）
├── release/                 归档 zip + SHA256 台账
└── build_all_modes.py       两档批量编译
```

---

## 3. 文档导航

**本项目对外交付的文档只有三类：功能简介（本文）、对外接口及用法、算法详解。**

| 想看什么 | 去哪 |
|---|---|
| 项目功能简介（本文） | `README.md` |
| **算法思路详解**（固件新增 + 上位机全收，供审阅） | [`docs/算法详解.md`](docs/算法详解.md) |
| **线格式权威规格**（帧/命令/参数/错误码） | [`docs/通信协议规格.md`](docs/通信协议规格.md) |
| **可编程接口清单**（`mc.*` 方法 + 命令/帧/参数表） | [`docs/api_reference.md`](docs/api_reference.md) |
| 参数名称-功能-调整速查（仿课件表） | [`docs/参数名称-功能-调整表.md`](docs/参数名称-功能-调整表.md) |
| **链路稳定性压测**（固定 FPS / MTBF / 环境对比） | [`docs/链路MTBF压测方法.md`](docs/链路MTBF压测方法.md) |
| 上位机怎么跑（CLI / GUI / 标定 / 采集） | [`host/README.md`](host/README.md) |
| 上游迷宫算法与源码导览 | [`code_stm32f1_mouse/docs/`](code_stm32f1_mouse/docs/) |

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

连接后第一条命令是 `PING`，应回 `PONG 7 218 7 1024 <rst>`（远程档指纹）。

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

### 上位机（八套件，共 556 项）

```bash
cd host
python selftest.py            # 协议断言（CRC/Q16.16/帧/自恢复/墙图/落盘）
python contract_test.py       # 登记表↔模拟器逐条对齐 + 源码静态断言（253 项）
python facade_smoke.py        # 门面全功能（A–J）
python maze_smoke.py          # 迷宫状态复刻 / 路口分类
python monitor_smoke.py       # 实时监视器
python pull_smoke.py          # 拉取链路（停等不变量 / 停发自愈）
python timesync_smoke.py      # 时间同步（纯函数 / 发车 / 逐帧时间 / 丢帧 / 落盘）
python motor_lab_smoke.py     # 电机实验（双护栏 / 急停锁存）
python uismoke.py             # 离屏 GUI（需 PySide6）
```

`selftest.py --e2e` 与 `uismoke.py` 会自己拉起模拟器，不需要手动开终端 A。

---

## 6. 版本与两档

| 项 | 远程档 | 按键比赛档 |
|---|---|---|
| 开关 | `#define MOUSE_REMOTE 1` | 什么都不定义 |
| 指纹 | `PROTO 7 / FW 218` | `PROTO 5 / FW 216` |
| 参数表 | **41 项** | 24 项 |
| TX 环 | 1024 B | 256 B |
| 能力 | 监视 / 参数 / 手动运动 / 远程发车收车 / `MCU RESET` | 与比赛固件一致（按键 + 物理启动） |

⚠️ 两档**共用同一份 `link.c`**，共享宏（`LINK_PROTO_VER` / `LINK_FW_VER` / `LINK_P_NUM`）
必须 `#ifdef MOUSE_REMOTE` 门控。当前版本历史见 [`docs/通信协议规格.md`](docs/通信协议规格.md) §9。
