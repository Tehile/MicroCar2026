# MicroCar2026

## 项目介绍

电脑鼠（Micromouse）竞速赛的完整工程实现，以上位机为核心，包含视觉定位、蓝牙数据回传和单片机固件三部分，各子系统可以独立运行。

视觉定位侧完成图像采集与位姿解算；蓝牙数据回传完成数据记录、实时状态分析、参数标定和迷宫地图重建；相关数据统一在上位机中汇总与绑定。

比赛成绩主要取决于车体运动精度、参数整定和赛后复盘能力。本仓库把观测、调参和记录放在 PC 侧，固件只保留运动控制、建图与链路协议，因此改参数不需要重新烧录。

系统包含以下模块，上位机负责汇总数据：

| 模块 | 职责 | 版本 |
|---|---|---|
| [`Auto Locate/`](Auto%20Locate/) | 从图片或视频解算场地中央标记的位置与朝向 | 1.2.0 |
| [`Control/`](Control/) | 本机 Web 上位机，显示轨迹、参数、记录，支持发车与回放 | 改版四十七 |
| [`RemoteControl/host`](RemoteControl/host/) | 串口/蓝牙命令与遥测、参数镜像、设备模拟器 | 2.14.0 |
| [`RemoteControl/code_competition*`](RemoteControl/) | STM32F103RB 固件，含比赛策略与远程控制两个模式 | PROTO 10 / FW 314 |

另有 `PhoneCamera/`，用手机采集图像并回传到 PC。该模块已不参与主流程，保留用于机型复验和原始帧取证。

数据流向：

```
手机录像 / 相机拍摄
  → Auto Locate          逐帧解算位姿，输出 trajectory.csv、session.json
  → Control              本机 Web 界面，读取轨迹与遥测
  → RemoteControl/host   串口或蓝牙收发命令与遥测帧
  → STM32F103RB 固件      运动控制、建图、比赛策略
```

## 部署

### 1. 环境要求

- Windows 10/11。仓库内的脚本、路径和串口命名按 Windows 编写，Python 部分未在 Linux/macOS 验证。
- Python 3.10 及以上，开发与验证使用 3.12.13。
- 编译固件需要 Keil MDK 5（ARMCC V5.06）和 STM32F103RB 目标板。
- 构建手机端需要 JDK 17、Gradle 8.9、Android SDK 34。
- 运行上位机和定位功能不需要连接硬件。

### 2. 安装依赖

```bash
git clone git@github.com:Tehile/MicroCar2026.git
cd MicroCar2026

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt` 只包含运行必需的五个包：`numpy`、`opencv-contrib-python`、`PyYAML`、`pyserial`、`aiohttp`。

定位模块使用 `cv2.aruco`，必须安装 `opencv-contrib-python`。如果装的是 `opencv-python`，导入时会缺少该模块。

可选依赖：

| 包 | 用途 |
|---|---|
| `PySide6` | `RemoteControl/host` 的桌面窗口（`run_host.py`）。Web 上位机不需要 |
| `scipy` | 位姿精化。未安装时精化自动关闭 |
| `qrcode`、`Pillow` | 采集服务的配对二维码。未安装时改为手输地址 |

```bash
pip install -r requirements-optional.txt
```

### 3. 检查安装

```bash
cd Control
python tools/preflight.py
```

脚本检查依赖版本、上游模块能否导入、串口是否存在、端口是否被占用。全部通过时退出码为 0。

没有硬件时，串口一项会显示为空，不影响后续步骤。

## 运行上位机

先启动设备模拟器，再启动上位机：

```bash
# 终端 1：设备模拟器，监听 127.0.0.1:5555
cd RemoteControl/host
python -m host.sim.mouse_sim

# 终端 2：上位机，默认监听 8770
cd Control
python app.py
```

浏览器打开 `http://127.0.0.1:8770`，在顶栏「电脑鼠」中选择 `tcp://127.0.0.1:5555` 连接。

端到端判据验收会自行启动模拟器：

```bash
cd Control
python tools/drill.py --sim            # 68 条判据，最近一次 67 通过 / 1 跳过
python tools/drill.py --sim --keep     # 失败时保留 .tmp/drill 现场
```

## 离线定位

对单个视频或目录执行解算：

```bash
cd "Auto Locate"
python -m aruco_locator.cli --source /path/to/run.mp4 \
                            --out out/run1 -c config.yaml
```

产物在 `out/run1/`：

| 文件 | 内容 |
|---|---|
| `results.csv` | 逐帧位姿：`t_ms, x_mm, y_mm, theta_deg, quality` |
| `results.jsonl` | 逐帧明细：角点坐标、质量标志、耗时 |

`-c config.yaml` 不能省略。缺省时使用内置默认值，720p 和 4K 素材所需的 `detection.scales=[1,2,3]`、相机高度和视差参数都不生效，结果会大量标记为无效帧。

上位机使用会话方式调用，产物包含 `session.json`、`trajectory.csv` 和图像：

```bash
python Control/tools/video_session.py \
    --source /path/to/run.mp4 \
    --out out/session1 \
    --config "Auto Locate/config.yaml"
```

`session.json` 是权威结果，包含帧数、检出率、内参、逐帧摘要与迷宫融合。
`python -m aruco_locator.session` 不读取 `--config`，不要直接调用。

`Auto Locate/samples/` 是本地评估素材，未纳入版本控制。没有素材时可以先跑合成真值自检：

```bash
cd "Auto Locate"
python selftest_synthetic.py
```

## 连接电脑鼠

串口参数 115200 8-N-1，使用 USB 转 TTL（CH340）或蓝牙 SPP 模块。

连接后第一帧应返回 `PONG`。远程档为 `PROTO=10`、`FW=314`；没有响应通常是烧写了比赛档，比赛档不响应串口命令。

界面页面：

| 页面 | 用途 |
|---|---|
| `#/live` | 遥测波形与四灯状态 |
| `#/params` | 41 项参数读写，写入后自动回读校验 |
| `#/race` | 发车与收车 |
| `#/video`、`#/track` | 视频解析与轨迹回放 |
| `#/records`、`#/maze` | 跑车记录与迷宫回放 |

收车后确认动作码归零。`ACK OK` 只说明命令收到了，不代表车已经停下。

不开界面时，可以用以下脚本：

```bash
cd RemoteControl/host
python selftest.py                        # 协议断言，不需要硬件
python basic_cmd_smoke.py COM7            # 基础指令
python monitor_smoke.py COM7              # 取流并监视
python link_pull_evt.py COM7 --mark-n 2   # 长时间取流，事件触发，在途命令不超过 1 条
```

`--mark-n` 是批量深度，值越大上行越密。真机从 2 开始，确认稳定后再升到 4 或 5。
设为 6 会在十几秒内把透传模块灌满，只能断电恢复。

## 编译固件

Keil 中打开 `RemoteControl/code_competition - V6++/USER/TEST.uvprojx`。

| 用途 | Target |
|---|---|
| 比赛 | `V6_0mm` / `V6_2mm` / `V6_4mm` / `V6_6mm`，默认 `V6_4mm` |
| 上位机接管 | `Target_Remote` |

切换 Target 后需要 Rebuild，增量编译会沿用上一档的目标文件。

命令行重编并比对四个比赛档的 hex 哈希：

```bash
cd RemoteControl
python build_v6pp_modes.py
```

注意事项：

- UV4 的退出码不可靠（0 无错误、1 仅告警、2 有错误），以构建日志中的 `N Error(s)` 为准。
- Keil 运行时命令行无法占用工程，重编前先关闭 Keil。
- `code_competition/` 和 `V6+/` 的 `.c/.h` 多为 GBK + CRLF，例外是 `UTILITY/utility.c`（UTF-8）。改写前先确认编码，否则中文注释会损坏。

五套固件工程各自对应一个阶段，不合并：`V6++` 是当前工作副本，`V6+` 是远程档移植的前一步，`code_competition` 是旧双档基线，`code_stm32f1_mouse` 是课程原版，根目录 `code_competition_搜索连续转弯九成速_V6_20261029` 是 10-29 的交付快照。

## 配置

| 项 | 位置 | 当前值 |
|---|---|---|
| 上位机端口 | `Control/config.yaml` | 8770 |
| 模拟器端口 | `host.sim.mouse_sim --port` | 5555 |
| 采集服务端口 | `PhoneCamera/pc/start_service.cmd` | 8765 |
| 场地边长 | `Control/config.yaml::arena`、`Auto Locate/config.yaml::field.field_size_mm` | 1440.0 mm |
| 格距 | 同上 | 180 mm |

场地边长在两处配置中都要改。只改一处时，轨迹回放的缩放与解算结果不一致。

定位参数共 60 项，说明见 `Auto Locate/docs/参数参考.md`；固件参数共 41 项，功能与调整方法见 `RemoteControl/docs/参数名称-功能-调整表.md`。

## 项目结构

```
.
├── README.md
├── requirements.txt
├── requirements-optional.txt
├── docs/                            架构、安装运行、版本发布、硬件场地
├── Auto Locate/                     ArUco 场地定位
│   ├── aruco_locator/               检测、角点精化、单应解算、迷宫融合
│   ├── docs/                        接口、参数手册、验证报告
│   └── samples/                     评估素材，未入库
├── Control/                         整合上位机
│   ├── app.py                       启动入口
│   ├── control/                     后端与前端资源
│   ├── tools/                       preflight、drill、browser_check 等
│   └── docs/
├── RemoteControl/                   链路层、上位机库、固件工程
│   ├── host/                        上位机库与验收脚本
│   ├── code_competition - V6++/     当前工作副本
│   ├── code_competition - V6+/      远程档移植前一步
│   ├── code_competition/            旧双档基线
│   ├── code_stm32f1_mouse/          课程原版
│   └── docs/
├── PhoneCamera/                     手机采集端与 PC 接收服务
├── code_competition_搜索连续转弯九成速_V6_20261029/
└── 课件/                            参数调整表
```

## 测试

| 模块 | 命令 | 内容 |
|---|---|---|
| `Control` | `python tools/preflight.py` | 依赖、上游导入、串口、端口 |
| `Control` | `python tools/drill.py --sim` | 端到端判据 |
| `Control` | `python tools/check_iface_table.py` | 接口文档与实现一致性 |
| `Auto Locate` | `python selftest_synthetic.py` | 合成真值，20 场景位置误差均值 0.43 mm |
| `Auto Locate` | `python tools/check_api_surface.py` | 公开接口是否变化 |
| `host` | `python selftest.py` | 协议断言，45 项 |
| `PhoneCamera` | `python pc/test_server.py` | 接收服务协议 |

`Control` 与 `Auto Locate` 的命令需要先进入对应模块目录执行。

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `ModuleNotFoundError: host` | 工作目录不对 | 在 `RemoteControl/host` 下执行，或设置 `PYTHONPATH` |
| `cv2` 没有 `aruco` 属性 | 装了 `opencv-python` | 改为 `opencv-contrib-python` |
| 页面能打开但内容为空 | 前端启动期异常 | 查看页面提示的阶段；`Ctrl+Shift+R` 清除缓存 |
| 串口能打开但收不到数据 | 烧写了比赛档，或波特率不对 | 改烧 `Target_Remote`，确认 115200 |
| 取流十几秒后静默 | 透传模块被灌满 | 断电复位，降低 `--mark-n` |
| 视频解析结果全部无效 | 没有传 `--config` | 用 `video_session.py` 或显式指定 `-c config.yaml` |
| 发车被拒绝 | 电池电压低于 7000 mV | 充电或换电池，该限制为有意设计 |
| 轨迹缩放错误 | 两处场地尺寸不一致 | 保持 `arena` 与 `field_size_mm` 相同 |

## 已知问题

| 问题 | 影响 | 状态 |
|---|---|---|
| `host/selftest.py --e2e` 在连续拉取后报 `timeout waiting for: GET ALL` | 仅影响该项自检，纯协议自检正常 | 既有缺陷，发布前的快照上同样复现。先停流再执行 `GET ALL`，或改用 `link_pull_evt.py` |
| `control.__version__` 为 `0.1.0` | 不能用于判断版本 | 占位值，以 `Control/README.md` 的改版序为准 |
| Android `versionName` 为 `1.0` | 与 App 内部版本不一致 | 以 `LockedCaptureProbe.APP_VERSION` 为准 |
| 手机实时帧流已下线 | 位置数据只来自视频解算 | 采集端保留用于机型复验与取证 |

## 安全说明

- 发车前上位机会自行读取电池电压，低于 7000 mV 拒绝发车，不依赖车端判断。
- 收车后确认动作码归零，`ACK OK` 不代表已停止。
- 链路完全断开时 `STOP`、`ESTOP`、`RESET` 都会超时，软件无法收车，只能人工停车并断电。
- 遥测一律用 `RATE PULL <n> <t>`，不要用 `RATE FIX <hz>` 连续发流。
- 上电时车体必须静止，陀螺零偏标定是阻塞流程，运动中上电会失败。
- 调试时轮子悬空，「维护 → 擦除」不可恢复。

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/系统架构.md`](docs/系统架构.md) | 分层职责、集成方式、数据流、时钟 |
| [`docs/安装与运行.md`](docs/安装与运行.md) | 安装、各模块运行方式、故障排查 |
| [`docs/版本与发布.md`](docs/版本与发布.md) | 版本矩阵、五档 Target、红线基线、归档流程 |
| [`docs/硬件与场地.md`](docs/硬件与场地.md) | 场地几何、标记布局、车体、接线、相机 |
| [`Auto Locate/README.md`](Auto%20Locate/README.md) | 定位算法原理与精度数据 |
| [`Auto Locate/docs/API.md`](Auto%20Locate/docs/API.md) | 定位库接口 |
| [`Control/README.md`](Control/README.md) | 上位机模块与页面 |
| [`Control/docs/接口对照表.md`](Control/docs/接口对照表.md) | HTTP/WS 端点与上游符号对照 |
| [`RemoteControl/docs/通信协议规格.md`](RemoteControl/docs/通信协议规格.md) | 帧布局、命令表、参数表 |
| [`PhoneCamera/README.md`](PhoneCamera/README.md) | 采集端与接收服务 |

## 许可证

本仓库为参赛与课程工程，未附带开源许可证。

使用的第三方组件：ArUco 字典 `DICT_4X4_1000`（OpenCV contrib）、ZXing core 3.5.3（扫码，以 jar 形式随 App 入库）、CH340 串口驱动。
