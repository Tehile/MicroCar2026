# PhoneCamera · 手机采集与回传

将手机作为参数可控、结果可复算的相机：锁定曝光、对焦与变焦后连续取帧，按 `SENSOR_TIMESTAMP` 抽帧，
逐帧携带元数据回传至 PC，落盘前完成分片与整文件校验。

> 该模块已退出主链路。自上位机改版二十四起，位置数据由「视频 → Auto Locate」解算，
> 实时帧流定位已移除。模块继续维护，用途为 M0 机型能力复验、原始帧取证与离线素材采集。

## 1. 组成与版本

| 组成 | 位置 | 说明 |
|---|---|---|
| 采集端 | `android/m0probe/` | Camera2 单模块应用（Kotlin），扫码配对 → 开流 → 分片回传 |
| 接收端 | `pc/server.py` | aiohttp 服务：HTTP 分片交付、WS 控制与监视、分片校验、面板 |

| 端 | 常量 | 当前值 |
|---|---|---|
| App | `LockedCaptureProbe.APP_VERSION` | `m0probe-1.15.0` |
| PC | `SERVER_VERSION` | `1.15.0` |

版本以代码常量为准；Gradle `versionName` 为 `1.0`，不参与版本判定。

## 2. 运行

```bash
cd PhoneCamera/pc
start_service.cmd          # 端口 8765，token ab12cd34，数据目录 ./data，限速 40 Mbps
```

手机与 PC 处于同一局域网，App 中扫码或手输 `ip:port` 完成配对。面板地址 `http://<pc-ip>:8765/`。

构建 APK：

```bash
cd PhoneCamera/android/m0probe
gradle assembleDebug       # 无 wrapper，使用外部 Gradle 8.9；local.properties 需指向 Android SDK
```

## 3. 协议

### 3.1 控制通道

`WS /ws?token=…`：服务端下发 `config` → App 回 `hello` → 服务端下发 `start` / `stop`
→ App 回 `start_ack` / `stop_ack`；运行期间上报 `status`、`thumb`、`session_begin`、`delivery_done`、`error`。

### 3.2 交付通道（HTTP 分片）

```
POST /api/v1/files/init                 声明 session_id / seq / name / size / sha256 / meta
PUT  /api/v1/files/{file_id}            Content-Range + X-Chunk-Crc32
POST /api/v1/files/{file_id}/complete   整文件 SHA-256 通过后置为 verified
```

文件状态仅三种：`partial`、`verified`、`failed`；`verified` 只由整文件校验产生。

### 3.3 落盘格式

交付帧为纯 Y 平面（`format=YUV_Y`，默认 1920 × 1440，2 764 800 B/帧），同名 `.json` 记录逐帧元数据：
`sensor_timestamp_ns`、曝光、ISO、对焦、变焦、`rolling_shutter_skew_ns`、`crop_region`、`sha256`。
实时预览帧为 JPEG，不落盘。

完整契约见 [`pc/README.md`](pc/README.md) 与 [`docs/对外接口与单一交付道.md`](docs/对外接口与单一交付道.md)。

## 4. 目录

```
PhoneCamera/
├── android/m0probe/        Camera2 采集应用（Kotlin，第三方依赖仅 ZXing core jar）
├── pc/
│   ├── server.py           接收服务（HTTP + WS + 面板 + 二维码）
│   ├── hostlink.py         面向其它程序的零依赖客户端库
│   ├── start_service.cmd   启动脚本（端口、token、数据目录、限速）
│   ├── test_server.py      协议端到端测试
│   └── test_interop.py     跨程序可用性测试
├── tools/
│   ├── m0_evaluate.py      M0 判据引擎 G1–G27（读取 m0_out 报告，输出 PASS/WARN/FAIL）
│   ├── capture_path_versions.json  采集路径登记表（App 版本 → path_id / rev）
│   ├── test_chroma_plane.py        P15 色度链路判据
│   └── test_color_source.py        P16 color_source 二分判据
├── m0_out/                 M0 真机证据（JSON，判据输入与夹具）
├── docs/                   设计、接口与迭代记录
└── release/                归档台账
```

`pc/data/` 为运行落盘目录，未纳入版本控制。

## 5. 验证

```bash
cd PhoneCamera/pc
python test_server.py       # 协议正确性
python test_interop.py      # 跨程序可用性
```

## 6. 文档

| 内容 | 文档 |
|---|---|
| 总体方案（需求、决策 D*、风险 R*） | [`手机相机采集与回传_方案设计.md`](手机相机采集与回传_方案设计.md) |
| 当前对外契约 | [`docs/对外接口与单一交付道.md`](docs/对外接口与单一交付道.md) |
| 接收服务使用与排障 | [`pc/README.md`](pc/README.md) |
| 扫码配对契约 | [`docs/扫码配对.md`](docs/扫码配对.md) |
| M0 机型复验规程 | [`docs/M0-机型能力验证.md`](docs/M0-机型能力验证.md) |
| M0 第 3~13 轮复算与 M1/M2 落地记录 | [`docs/`](docs/)（历史迭代，结论已被后续轮次修订） |

历史文档中引用的 `tools/test_criteria_round6~12.py` 为各轮次一次性回归脚本，
已于 2026-11-14 移出仓库，仍可从 git 历史获取；判据引擎 `tools/m0_evaluate.py` 保留，历史证据可用其复算。
