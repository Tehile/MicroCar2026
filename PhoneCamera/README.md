# PhoneCamera —— 手机相机采集与回传

把手机当成**可控、可复算的相机**：锁曝光/对焦/变焦连续取帧，按 `SENSOR_TIMESTAMP` 抽帧，
每一帧带完整元数据回传到 PC 并逐文件校验，PC 侧落盘后可以直接用于建模与定位。

> **它现在不再是主路。** 整合上位机改版二十四起，位置数据改走"一段视频 → Auto Locate"，
> 手机实时帧流那一路已下线。本模块仍在维护，用途变成：M0 机型能力复验、原始帧取证、离线素材采集。

---

## 1. 两个半边

| 半边 | 位置 | 是什么 |
|---|---|---|
| 采集端 | `android/m0probe/` | Camera2 单模块 App（Kotlin），扫码配对 → 开流 → 分片回传 |
| 接收端 | `pc/server.py` | aiohttp 服务，HTTP 分片交付 + WS 控制/监视 + 分片校验 + 面板 |

版本（**以代码常量为准**，别看 Gradle `versionName`）：

| 端 | 常量 | 当前值 |
|---|---|---|
| App | `LockedCaptureProbe.APP_VERSION` | `m0probe-1.15.0` |
| PC | `SERVER_VERSION` | `1.15.0` |

---

## 2. 跑起来

```bash
cd PhoneCamera/pc
start_service.cmd          # 端口 8765，token ab12cd34，数据落 ./data，限速 40 Mbps
```

手机与 PC 同一局域网，App 里扫码（或手输 `ip:port`）即可配对。
面板地址：`http://<pc-ip>:8765/`。

构建 APK：

```bash
cd PhoneCamera/android/m0probe
gradle assembleDebug        # 无 wrapper，用外部 Gradle；需先写 local.properties 指向 SDK
```

---

## 3. 协议一览

**控制道** `WS /ws?token=…`：服务端下发 `config` → App 回 `hello` → 服务端 `start` / `stop`
→ App 回 `start_ack` / `stop_ack`（闭环），运行中上报 `status` / `thumb` / `session_begin` / `delivery_done` / `error`。

**交付道**（HTTP 分片）：

```
POST /api/v1/files/init                     声明 session_id/seq/name/size/sha256/meta
PUT  /api/v1/files/{file_id}                Content-Range + X-Chunk-Crc32
POST /api/v1/files/{file_id}/complete       整文件 SHA-256 通过才 verified
```

**落盘格式**：交付帧是**纯 Y 平面**（`format=YUV_Y`，默认 1920×1440 = 2 764 800 B/帧），
同名 `.json` 记逐帧元数据（`sensor_timestamp_ns`、曝光/ISO/对焦/变焦、`rolling_shutter_skew_ns`、
`crop_region`、`sha256`…）。实时帧是 JPEG，**不落盘**。

完整契约见 [`pc/README.md`](pc/README.md)、[`docs/对外接口与单一交付道.md`](docs/对外接口与单一交付道.md)。

---

## 4. 目录

```
PhoneCamera/
├── android/m0probe/        Camera2 采集 App（Kotlin，唯一第三方依赖 ZXing core jar）
├── pc/
│   ├── server.py           接收服务（3716 行，HTTP + WS + 面板 + 二维码）
│   ├── hostlink.py         给其它程序用的零依赖客户端库
│   ├── start_service.cmd   一键启动（端口/token/数据目录/限速都写死在这里）
│   ├── test_server.py      协议正确性端到端测试
│   └── test_interop.py     跨程序可用性测试
├── tools/
│   ├── m0_evaluate.py      M0 判据引擎 G1–G27（读 m0_out 报告，出 PASS/WARN/FAIL）
│   ├── capture_path_versions.json  采集路径登记表（App 版本 → path_id/rev）
│   ├── test_chroma_plane.py   P15 色度链路判据（真编译真执行 Kotlin 函数体）
│   └── test_color_source.py   P16 color_source 二分判据
├── m0_out/                 M0 真机证据（8 份 JSON，判据的输入与夹具）
├── docs/                   设计、接口、迭代记录
└── release/                归档台账（1.13.x 的二进制已移出仓库）
```

`pc/data/` 是运行落盘（**不入库**，体积随跑随涨）。

---

## 5. 文档怎么读

| 想了解 | 看这里 |
|---|---|
| 总体方案（需求 / 决策 D* / 风险 R*） | [`手机相机采集与回传_方案设计.md`](手机相机采集与回传_方案设计.md) |
| **当前对外契约**（单一交付道、接口面、判据） | [`docs/对外接口与单一交付道.md`](docs/对外接口与单一交付道.md) |
| PC 服务怎么用、怎么排障 | [`pc/README.md`](pc/README.md) |
| 扫码配对契约（三种地址写法） | [`docs/扫码配对.md`](docs/扫码配对.md) |
| M0 复采规程（换机/重验按它执行） | [`docs/M0-机型能力验证.md`](docs/M0-机型能力验证.md) |
| M0 第 3~13 轮复算、M1/M2 落地记录 | [`docs/`](docs/)（历史迭代，结论已被后续轮次修订） |

**读历史的注意**：`docs/M0-第N轮复算结论.md` 里提到的 `tools/test_criteria_round6~12.py`
是每一轮的一次性判据双向回归脚本，已于 2026-11-14 发布整理时移出仓库
（仍在 git 历史里：`git show pre-release-20261114:PhoneCamera/tools/test_criteria_round8.py`）。
判据引擎 `tools/m0_evaluate.py` 保留，历史证据仍可用它复算。
