# 2026 电脑鼠（Micromouse）竞速系统

一套完整的电脑鼠参赛工程：**手机/相机视觉定位 → 整合上位机 → 蓝牙链路 → STM32 固件**。
四个子系统各自可独立运行，串起来就是"拍一段场地视频 → 解出轨迹 → 看车怎么跑 → 调参 → 发车"的闭环。

| 层 | 模块 | 版本 | 语言 |
|---|---|---|---|
| 视觉定位 | [`Auto Locate/`](Auto%20Locate/) | `1.2.0` | Python 3.12 + OpenCV |
| 整合上位机 | [`Control/`](Control/) | 改版四十七 | Python + 原生 JS（本机 Web） |
| 链路与工具链 | [`RemoteControl/host`](RemoteControl/host/) | host `2.14.0` | Python |
| 固件 | [`RemoteControl/code_competition*`](RemoteControl/) | PROTO 10 / FW 314 | C（Keil MDK / STM32F103RB） |
| 采集端 | [`PhoneCamera/`](PhoneCamera/) | `m0probe-1.15.0` | Kotlin（Camera2）+ aiohttp |

---

## 1. 这是什么

电脑鼠要在 16×16 的迷宫里边探路边找最短路径，最后冲刺。
比赛中真正决定成绩的是三件事：**车走得准不准、参数调得对不对、跑完能不能复盘**。
这个工程把这三件事都搬到了电脑上。

| 能力 | 怎么实现 |
|---|---|
| **不看车的眼睛** | 场地四角贴 ArUco 标记，拍一张 / 一段视频就能解出中央 0 号标记的位置与朝向（±mm 级，无需相机内参） |
| **一个界面看全部** | 本机 Web（`127.0.0.1:8770`）九页：总览 / 实时波形 / 参数 / 比赛 / 维护 / 视频复盘 / 轨迹 / 记录 / 迷宫回放 |
| **不改固件也能调参** | 41 项参数在线读写（Q16.16 精度），写后自动回读校验，`SAVE` 落 Flash 掉电保留 |
| **跑完能复盘** | 每帧捎带建图增量，实时同步车内的迷宫；跑完的记录可从帧**重放建图**，而不是只信一张最终快照 |
| **无硬件也能验** | 设备模拟器协议逐字节一致，自带物理与迷宫探索；`drill.py --sim` 跑 68 条判据 |

---

## 2. 系统全景

```
        ┌──────────────┐   视频/图像     ┌──────────────────┐
        │ PhoneCamera  │ ─────────────► │   Auto Locate    │
        │ 手机采集/回传 │   (HTTP 分片)   │  ArUco 场地定位  │
        └──────────────┘                └────────┬─────────┘
                                                 │ 逐帧位姿 + 时间轴
                                                 │ (子进程，会话级 API)
                                        ┌────────▼─────────┐
                                        │     Control      │  本机 Web 8770
                                        │   整合上位机     │  九页 + 回放 + 记录
                                        └────────┬─────────┘
                                                 │ 同进程 import
                                        ┌────────▼─────────┐
                                        │ RemoteControl/   │  host.api.MouseClient
                                        │      host        │  遥测/参数/发车/收车
                                        └────────┬─────────┘
                                                 │ 串口·蓝牙 SPP 460800
                                        ┌────────▼─────────┐
                                        │   STM32F103RB    │  Target_Remote (PROTO 10)
                                        │      固件 V6++    │  Target V6_0/2/4/6mm（比赛）
                                        └──────────────────┘
```

**两条边界值得记住**：上位机与 `host` 是**同进程**（直接 `import`），与 `Auto Locate` 是**子进程**（`python -m aruco_locator.session`，超时/取消各自独立）。

---

## 3. 目录

```
2026 Computer Mouse/
├── README.md                  本文件
├── docs/                      跨模块文档：架构 / 快速开始 / 版本与发布 / 硬件与场地
├── Auto Locate/               ArUco 场地定位引擎（v1.2.0）
│   ├── aruco_locator/         主包：检测 → 亚像素角点 → 单应解算 → 位姿
│   ├── docs/                  接口、参数手册、验证报告、评估记录（历史件在 docs/archive）
│   └── release/               版本归档（1.1.0）
├── Control/                   整合上位机（本机 Web）
│   ├── app.py                 唯一启动入口
│   ├── control/               后端：adapters / api / static(前端) / store
│   ├── tools/                 preflight / drill / browser_check / 接口对照校验
│   └── docs/                  设计方案、模块总览、接口对照表（md → html 成对生成）
├── RemoteControl/             链路 + 工具链 + 四套固件工程
│   ├── host/                  PC 上位机库（`host.api.MouseClient`）+ 30 个验收脚本
│   ├── code_competition - V6++/  ★ 现行工作副本：五 Target，回程补搜 + 远程档
│   ├── code_competition - V6+/   V6 算法 + 远程档移植（V6++ 的前一步）
│   ├── code_competition/         旧双档基线（比赛档 / 远程档，红线 hex 出自此处）
│   ├── code_stm32f1_mouse/       课程基线（MAZETYPE 7），仅参考
│   └── docs/                  协议规格、算法详解、工具与校验、决策记录
├── PhoneCamera/               手机采集端 + PC 接收服务
│   ├── android/m0probe/       Camera2 采集 App（扫码配对 / 连续流 / 分片回传）
│   ├── pc/server.py           aiohttp 接收服务（8765）+ hostlink 客户端库
│   └── docs/                  M0 机型能力验证与各轮复算结论
├── code_competition_搜索连续转弯九成速_V6_20261029/
│                              V6 交付快照（10-29，只有比赛档，无 Target_Remote）
├── 课件/                      电脑鼠参数调整表（参数表 41 项的出处）
└── .gitignore                 构建产物 / 运行现场 / 教学资料一律不入库
```

**五套固件工程并存是有意的**，别急着合并：`V6++` 是当前工作副本（含远程档 + 回程补搜），
`V6+` 是它的前一步（远程档刚移植完），`code_competition` 是双档基线与红线 hex 的出处，
`code_stm32f1_mouse` 是课程原版，根目录 `code_competition_搜索…V6_20261029` 是 10-29 的交付快照。
判"要改哪一份"看 `RemoteControl/README.md` 的目录表。

---

## 4. 快速开始（无硬件，5 分钟）

全部 Python 代码固定跑在 **`general_env`**（Python 3.12）。这是硬约束：
`Auto Locate` 明确禁止在其他环境 `pip install`，`Control` 也只在 `general_env` 里验过。

```bash
conda activate general_env
# 或直接用解释器：C:/Users/liuru/.conda/envs/general_env/python.exe

cd Control
python tools/preflight.py     # ① 自检：依赖 / 上游可 import / 串口 / 端口
python app.py                 # ② 起服务 → 打开 http://127.0.0.1:8770
python tools/drill.py --sim   # ③ 无鼠无场地，端到端跑完 I1~I68 判据
```

定位模块单独跑：

```bash
cd "Auto Locate"
python -m aruco_locator.cli --source samples/VID_20261027_120407.mp4 --out out/
python selftest_synthetic.py            # 合成真值自检，不依赖任何素材
```

更多（有硬件、发车、蓝牙配对、手机采集）见 [`docs/快速开始.md`](docs/快速开始.md)。

---

## 5. 版本矩阵

| 项 | 当前值 | 说明 |
|---|---|---|
| `Auto Locate` | `1.2.0` | 新增格点匹配第二通道，运动模糊下兜底 |
| `RemoteControl/host` API | `2.14.0` | 与固件 proto 分开编号 |
| 固件远程档 | PROTO **10** / FW **314** | `Target_Remote`，41 项参数全接 |
| 固件比赛档 | V6++ 五档 | `V6_0mm/2mm/4mm/6mm` + `Target_Remote`，默认 `V6_4mm` |
| `PhoneCamera` App | `m0probe-1.15.0` | 语义版本在代码常量里，不在 `build.gradle` |
| `PhoneCamera` PC | `server 1.15.0` | `pc/server.py` 顶部 `SERVER_VERSION` |
| `Control` | 改版四十七 | `control.__version__` 仍是 `0.1.0` 占位，不要拿它判版本 |

判"当前是哪个版本"的权威位置：**代码常量**（`aruco_locator.__version__`、`host.__version__`、
`LockedCaptureProbe.APP_VERSION`、`SERVER_VERSION`），不是文件名、不是 Gradle 字段。

---

## 6. 文档地图

| 想了解 | 看这里 |
|---|---|
| 四层怎么分工、数据怎么流 | [`docs/系统架构.md`](docs/系统架构.md) |
| 装什么、怎么跑起来、怎么排障 | [`docs/快速开始.md`](docs/快速开始.md) |
| 版本怎么定、固件红线怎么比、怎么归档 | [`docs/版本与发布.md`](docs/版本与发布.md) |
| 车上有什么、场地怎么贴、串口怎么接 | [`docs/硬件与场地.md`](docs/硬件与场地.md) |
| 定位算法原理与精度实测 | [`Auto Locate/README.md`](Auto%20Locate/README.md) |
| 上位机九页与模块组成 | [`Control/README.md`](Control/README.md)、[`Control/docs/模块组成总览.md`](Control/docs/模块组成总览.md) |
| 通信协议、远程档、算法细节 | [`RemoteControl/docs/通信协议规格.md`](RemoteControl/docs/通信协议规格.md) |
| 手机采集端协议与判据 | [`PhoneCamera/pc/README.md`](PhoneCamera/pc/README.md)、[`PhoneCamera/docs/对外接口与单一交付道.md`](PhoneCamera/docs/对外接口与单一交付道.md) |

---

## 7. 安全红线（写在代码里的硬约束）

1. **发车前独立校验电池**：上位机自己读电压，`< 7000 mV` 直接拒绝发车——不信车上的判断。
2. **`ACK OK` 不等于停住**：收车必须再拉一帧验证 `mazeTask/motionTask` 归零。
3. **链路断死时软件收车网完全无效**（STOP→ESTOP→RESET 全超时），只能人工停车 + 断电。
   安全须知不能写"脚本兜住"。
4. **遥测帧率顶在 13~17 Hz**：`RTT ≈ 300 ms` 在 PC 侧 CH340 上。`N=4` → 13.1 Hz，`N=5` → 17.0 Hz 是安全顶，
   **`N=6` 十几秒把透传模块灌死，须物理断电**。禁止 `RATE FIX <hz>` 连续发流。
5. **实验前轮子必须悬空**；`维护 → 擦除` 不可逆。

---

## 8. 环境

| 用途 | 环境 |
|---|---|
| 全部 Python 代码 | conda `general_env`（Python 3.12.13，cv2 4.11.0 / numpy / scipy / pyyaml / aiohttp / pyserial 齐备） |
| 固件编译 | Keil MDK（UV4，ARMCC V5.06，C90）+ STM32F103RB |
| Android 采集端 | AGP 8.5.2 / Kotlin 1.9.24 / JDK 17，`compileSdk 34`、`minSdk 24` |

依赖清单：`Control/requirements.txt`、`RemoteControl/host/requirements.txt`。
两份都只是**记录**，不要拿去新建环境——`general_env` 里已经齐了。

---

## 9. 许可与致谢

本仓库为参赛/课程工程，未附开源许可证。第三方内容：ArUco 字典 `DICT_4X4_1000`（OpenCV contrib）、
ZXing core 3.5.3（扫码，随 App 以 jar 形式入库）、CH340 串口驱动。
课程实验指导与课件不在本仓库内。
