# ArUco 场地定位（Auto Locate）

解算正方形场地中央 **0 号标记**的位置与方向。

场地四角各贴一个角标记（ID 1~4，`DICT_4X4_1000`）；角标记的**朝向与 ID 排列顺序都不固定**，
摄像头拍一张包含全部标记的画面，代码自动识别四个角、建立场地坐标系，再给出 0 号标记的位姿。

- 主路径是**无标定单应**（4 个角标记 × 4 个角点 = 16 个点做最小二乘），**不需要相机内参**。
- 可选棋盘格标定 + 畸变校正 + 联合 `solvePnP` 作为交叉校验，默认关闭。

**当前版本 v1.1.0**（`aruco_locator.__version__`）。文档索引：

| 文档 | 内容 |
|---|---|
| [docs/API.md](docs/API.md) | **接口文档**：坐标约定、集成方式、字段契约、异常、性能、兼容性策略 |
| [docs/参数参考.md](docs/参数参考.md) | 60 个可调参数的手册（由 `python -m aruco_locator.docgen` 自动生成） |
| [docs/验证报告-2026-10-03新样本.md](docs/验证报告-2026-10-03新样本.md) | **新样本验证报告**：六类证据 + 外部参照，含 v1.0.1 修掉的缺陷全过程 |
| [docs/评估-对角线法选内顶点.md](docs/评估-对角线法选内顶点.md) | **对角线法评估**：合成真值正确率、点乘该在哪个空间做、耗时对比 |
| [docs/视频输入与手持拍摄改造-设计方案.md](docs/视频输入与手持拍摄改造-设计方案.md) | **视频/手持改造设计**：三处被打破的常量假设、S1–S5 实施顺序与存疑清单 |
| [docs/会话级接口规范-视频文件处理.md](docs/会话级接口规范-视频文件处理.md) | **会话级调用规范**：`run_session` 输入输出契约、字段、错误与标志（供外部程序调用） |
| [docs/验证报告-2026-10-27最终视频.md](docs/验证报告-2026-10-27最终视频.md) | **最终 4K 视频验证**：检出率对比、精度实测、迷宫融合与根因分析 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更与 v1.0 冻结契约 |
| [examples/quickstart.py](examples/quickstart.py) | 可运行示例 |
| 本文件 | 算法原理、精度实测与现场参数 |

---

## 1. 环境

```
Python 3.12 / opencv-contrib-python 4.11.0 / numpy / pyyaml / scipy
```

环境已固定在 `general_env`（`C:\Users\liuru\.conda\envs\general_env`）。
**不要**换环境、也**不要**在该环境外 `pip install`。

依赖用到的新版 API：`cv2.aruco.ArucoDetector`（4.7+），不是旧的 `cv2.aruco.detectMarkers`。

---

## 2. 目录结构

```
Auto Locate/
├── aruco_locator/            # 主包
│   ├── __init__.py           # 公开接口（v1.0 冻结，见 docs/API.md）
│   ├── api.py                # ArucoLocator / Localization / create_locator / locate
│   ├── parameters.py         # ★ 参数注册表：目录、点号路径读写、校验、预设档位
│   ├── docgen.py             # 参数文档生成器（python -m aruco_locator.docgen）
│   ├── config.py             # 配置数据类 + YAML 载入
│   ├── detector.py           # 标记检测 + 边缘亚像素精化入口
│   ├── corners.py            # ★ 边缘直线拟合亚像素精化（精度核心）
│   ├── geometry.py           # 内顶点选择 / 场地坐标系 / 位姿拟合 / 联合精化
│   ├── parallax.py           # ★ 差高视差修正（角标记与 0 号标记不同高度）
│   ├── sources.py            # 图片/目录/视频/摄像头统一取帧（含 Unicode 路径）
│   ├── viz.py                # 叠加可视化（含俯视小窗）
│   └── cli.py                # 离线批处理命令行
├── docs/
│   ├── API.md                # 接口文档
│   ├── 参数参考.md            # 参数手册（自动生成）
│   ├── 验证报告-*.md          # 实拍验证报告（含缺陷定位全过程）
│   ├── 评估-*.md              # 算法方案评估
│   ├── figures/              # 文档插图
│   └── evidence/             # 验证证据（分析输出留档）
├── examples/quickstart.py    # 可运行示例
├── tools/                    # 开发/验证工具（不参与运行时依赖）
│   ├── make_release.py             # 打发行归档（zip + sha256 + MANIFEST）
│   ├── validate_new_samples.py     # 批量跑图 → CSV/JSON + 标注图
│   ├── validate_new_samples2.py    # 六类独立证据的交叉机位一致性分析
│   ├── compare_vertex_modes.py     # 内顶点策略对比（带红胶带外部参照）
│   ├── validate_vertex_truth.py    # 合成真值回归：内顶点正确率（可证伪）
│   ├── stress_vertex_pairing.py    # 对角线配对安全闸压力测试
│   └── bench_vertex_mode.py        # 内顶点各策略耗时（交错采样）
├── samples/                  # 实拍样本（39 张；**不打进发行归档**）
├── release/                  # 发行归档产物
├── calibrate_camera.py       # 棋盘格标定 → 内参
├── selftest_synthetic.py     # 合成场地自检（无实拍也能验证精度）
├── config.yaml               # 全部阈值 / 相机参数（代码里不硬编码）
├── CHANGELOG.md              # 版本变更
├── 4x4_1000-{0..4}.svg       # ID 0~4 的字典图案（印刷用，单张 50mm×50mm）
└── README.md
```

---

## 3. 坐标与角度约定（不要擅自更改）

- **原点** = 离画面**左下角**最近的场地角点（即场地左下角）。
- **+x** 沿底边向右，**+y** 沿左边向上；场地恒归一化为 `[0, 1]²`（除以场地边长）。
- **位置** = 0 号标记的几何中心。
- **方向基准** = 0 号标记**自身的 +x 轴**（OpenCV 角点序 `corners[0] → corners[1]`）。
- **θ = atan2(vy, vx)**（场地系），值域 `(-180°, 180°]`，**逆时针为正**；同时输出方向单位向量 `(dir_x, dir_y)`。

> 原点刻意选左下角而不是左上角，就是为了让"逆时针为正"和照片观感一致。

标记印刷规格：SVG 为 `viewBox="0 0 6 6"`、`width/height="50mm"`，即 1 模块 = 8.333mm；
`<rect width="6" height="6" fill="black">` 铺满整个 6×6，**黑色方块（含 1 模块黑边框 +
4×4 数据区）边长 = 50mm**，即检测器给出的角点四边形就是这 50mm 的方块。
坐标输出用归一化单位，所以该尺寸只影响绝对毫米换算。

> ⚠ 2026-10-03 核对：算法把角标记边长当**自由量**一起解，实测映射回来约 **45mm**
> （在 `FIELD_MM = 1440mm` 假设下），而 SVG 标称 50mm。二者差 10%，尚待现场用尺复核：
> 要么格距是 180mm（则打印被缩到 90%），要么标记是 50mm（则格距 200mm、场地 1600mm）。
> **归一化输出不受影响**——全流程不使用绝对尺寸，此差异只影响毫米换算与打印设置。

**场地实物参数**（2026-09-25 现场确认）：

| 项 | 值 | 说明 |
|---|---|---|
| 迷宫规格 | **8×8 格**（非标准 16×16） | |
| 格距 | 180 mm | 场地边长 = 8 × 180 = **1440 mm** |
| 墙高 | 50 mm | = 角标记所在参考平面高度 |
| 0 号标记离地 | 23 mm | 装在电脑鼠顶上，比参考平面**低 27 mm** |
| 角标记安装 | 与墙顶平行（支撑件 0~2 mm） | 高出部分仅影响 0.1%，忽略 |

角标记与 0 号标记**不共面**，必须启用第 9 节的视差修正。

---

## 4. 快速开始

### 4.1 Python API

推荐入口（参数可覆盖、可套档位，详见 [docs/API.md](docs/API.md)）：

```python
import cv2, numpy as np
from aruco_locator import create_locator, locate

# 单图：一次性调用（路径含中文时必须 fromfile + imdecode）
img = cv2.imdecode(np.fromfile("shot.png", dtype=np.uint8), cv2.IMREAD_COLOR)
res = locate(img, "config.yaml")
if res.valid:
    print(res.x, res.y, res.theta_deg, res.dir_vector)
else:
    print("无效：", res.reason)

# 视频 / 摄像头：常驻实例（保留滤波状态）
locator = create_locator("config.yaml", profile="video")
cap = cv2.VideoCapture("clip.mp4")
i = 0
while True:
    ok, frame = cap.read()
    if not ok:
        break
    loc = locator.locate(frame, frame_index=i, timestamp=cap.get(cv2.CAP_PROP_POS_MSEC) / 1000)
    if loc.valid:
        vis = locator.annotate(frame, loc)   # 叠加可视化
    i += 1

# 临时调参（点号路径 / 叶子名简写 / 嵌套字典皆可）
loc = create_locator(profile="robust", params={"camera.parallax.camera_height_mm": 2000.0})
loc = create_locator(edge_search_px=4.0, vertex_mode="full")
```

结果序列化：`res.to_dict()` 给程序（多值字段是真 list），`res.to_row()` 写 CSV（分号字符串），
`res.to_json()` 落盘。

参数空间可自助查询：

```python
from aruco_locator import parameter_catalog, describe_profiles
print(parameter_catalog(group="detection"))   # 类型/默认/范围/说明
print(describe_profiles())                     # 预设档位
```

### 4.2 命令行（离线批处理）

```bash
python -m aruco_locator.cli --source out/frames --out out/cli --save-annotated
python -m aruco_locator.cli --source clip.mp4 --out out/cli --save-video out/cli/overlay.mp4
python -m aruco_locator.cli --source camera:0 --show          # 实时
python -m aruco_locator.cli --source shots/ --json r.jsonl --csv r.csv --quiet
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--source/-s` | 图片 / 图片目录 / 视频文件 / `camera:0` |
| `--config/-c` | YAML 配置（缺省用内置默认值） |
| `--out/-o` | 输出目录，自动在其中生成 `results.jsonl` / `results.csv` |
| `--save-annotated` | 每帧叠加图单独存成 `annotated_XXXXXX.png` |
| `--save-video` | 叠加后的视频（`.mp4`） |
| `--show` | 实时显示，按 `q` / `Esc` 退出 |
| `--max-frames` / `--start` | 帧范围 |
| `--recursive` | 目录输入时递归查找 |
| `--print-every` / `--quiet` | 进度打印策略 |

运行结束会打印统计：`帧数 / 有效 / 无效`、无效原因分布、`rms_field` 均值/中位/最大、耗时与帧率。

---

## 5. 每帧输出字段

`Localization`（JSONL / CSV 同名字段）：

| 字段 | 含义 |
| --- | --- |
| `valid` | 该帧是否有效 |
| `x`, `y` | 0 号标记中心，场地坐标 `[0,1]²` |
| `theta_deg` | 方向角，逆时针为正，`(-180,180]` |
| `dir_x`, `dir_y` | 方向单位向量（场地系） |
| `x_raw`, `y_raw`, `theta_deg_raw` | 时序滤波**前**的值（滤波开启时用于对比） |
| `origin_id` | 被判为原点的角标记 ID |
| `corner_ids` | 本帧命中的角标记 ID（排序后） |
| `missing_corner_ids` | 缺失的角标记 ID |
| `rms_field` | 四个角标记映射回正方形的重投影残差（场地单位），质量指标 |
| `target_rms` | 0 号标记同类残差 |
| `marker_scale` | 标记边长 / 场地边长 |
| `scale_cv` | 五个标记尺度的离散度（越小越一致） |
| `flags` | 见下表 |
| `reason` | 无效原因；`valid=True` 时为空 |
| `diagnostics` | 内顶点、坐标系、精化等中间量（仅 JSONL，便于排查） |

`flags` 取值：

| flag | 含义 | 处理建议 |
| --- | --- | --- |
| `inner_vertex_uncertain` | 最优/次优内顶点组合代价太接近 | 可结合 `diagnostics.inner_vertices.cost_gap_rel` 判断 |
| `ambiguous_axis` | 轴向判定余量过小（疑似 ±45° 轴歧义，x/y 可能互换） | 见第 8 节 |
| `ambiguous_origin` | 原点判定余量过小 | 检查角标记是否贴得太靠里 |
| `handedness_flipped` | 手性翻转（相机从场地背面拍） | 确认拍摄方向 |
| `inner_vertex_override` | 内顶点走了兜底路径 | 一般是角点检测质量差 |
| `target_not_square` | 0 号标记映射后不够方正 | 可能被遮挡/变形 |
| `smoothed` | 该帧结果被时序滤波修改 | 正常 |
| `height_corrected` | 施加了视差（差高）修正 | 仅在 `camera.parallax.enable` 且标定齐全时出现 |
| `parallax_uncalibrated` | 开了视差修正但参数不全（缺相机高度/天底点） | 按 `diagnostics.parallax.reason` 补齐，见第 9 节 |

**无效原因**（`reason`）：
`corner_markers_missing` / `target_marker_missing` / `inner_vertex_failed` /
`frame_failed` / `degenerate_homography` / `square_check_failed`。

---

## 6. 精度（合成自检实测）

`selftest_synthetic.py` 生成随机位姿的合成场地（含透视、镜头滚动、模糊、噪声、光照不均），
用真值直接比对：

```bash
python selftest_synthetic.py -n 150 --seed 20260922
python selftest_synthetic.py -n 150 --compare-edge     # 额外对比 cv2 亚像素
python selftest_synthetic.py -n 150 --distort         # 叠加镜头畸变，验证校正路线
```

**150 场景 / seed 20260922（1280×960）结果：**

| 配置 | 位置误差 均值 / 最大 | 角度误差 均值 / 最大 | rms_field |
| --- | --- | --- | --- |
| **默认（edge_refine=on）** | **0.489 mm / 1.195 mm** | 0.247° / 0.917° | 0.00045 |
| cv2 亚像素（edge_refine=off） | 0.792 mm / **34.369 mm** | **0.123°** / 1.363° | — |

内顶点选择在 300+ 场景上验证 **零错选**（4 个种子 198/199、`w∈[0.005,0.5]` 时 139/139）。

耗时（1280×960 单帧）：默认约 **40.5 ms（≈24.7 fps）**；cv2 路线 26.1 ms；
`vertex_mode=full` 约 60 ms；多尺度 `scales=[1,2]` 约 80 ms。

### 6.1 edge_refine 与 cv2 亚像素的取舍（重要）

- **位置精度**要靠 **`edge_refine`（边缘直线拟合）**。cv2 的 `CORNER_REFINE_SUBPIX` 有约 **1.6%**
  的"标记整体缩小"**系统偏置**（径向 −0.29px，会让位置整体内缩），边缘拟合把它压到 +0.002px。
  上面 cv2 那一列 34mm 的最坏误差就是该偏置在小标记上被放大导致的。
- **角度精度**反而 cv2 略好（0.123° vs 0.247°）。因为 cv2 的精化是**各向同性收缩**，
  对方向几乎无偏；边缘拟合的角点由相邻边线求交得到，直线拟合噪声会略微"抖动"方向。
- 结论：**默认用 edge（位置优先）**。若你的场景更看重方向角、且能接受位置内缩，
  把 `detection.edge_refine: false` 即可切回 cv2 路线（此时 `subpixel_polish` 生效）。
- `edge_bright_outside`：黑标记贴浅色地面时为 `true`；若标记比背景亮，改 `false`。

---

## 7. 标定路线（可选，交叉校验）

默认不启用。需要时用棋盘格标定内参，再让管线做畸变校正：

```bash
# 拍一组棋盘图（或直接连摄像头采集）
python calibrate_camera.py --images calib/*.jpg --cols 9 --rows 6 --square-mm 25 \
    --out calib/intrinsics.yaml
python calibrate_camera.py --camera 0 --shots 20 --out calib/intrinsics.yaml --save-shots calib/shots
```

输出同时写 `.yaml` 与 `.npz`，两者都能被 `load_intrinsics` 读取。然后把结果填进 `config.yaml`：

```yaml
camera:
  undistort: true
  intrinsic_file: calib/intrinsics.yaml
```

合成自检里 `--distort` 会叠加镜头畸变并对比：**校正后 0.503mm 均值 vs 不校正 8.98mm**。

---

## 8. 已知局限

- **±45° 轴歧义（固有）**：当相机绕光轴的滚动角接近 ±45° 时，"底边"和"左边"在图像上无法区分，
  会导致 x/y 互换。此时 `axis_margin` 很小，会打上 **`ambiguous_axis`** 标记。
  这是几何上的固有退化，不是 bug；要么调整相机安装角避开 ±45°，要么在应用层用 `ambiguous_axis` 做处理。
- **共面假设**：单应只对四个角标记所在的参考平面精确。本场地角标记在墙顶（离地 50mm）、
  0 号标记在电脑鼠顶上（离地 23mm），**两者不共面**，必须启用第 9 节的视差修正；
  不修正时位置误差约 1.4% 场地边长（≈20mm）。
- **角标记缺失即判无效**：按约定 `require_all_corners: true`，不做补齐、不做兜底。

---

## 9. 视差（差高）修正 —— 角标记与 0 号标记不在同一平面

四个角标记定义**参考平面**，单应对该平面精确。0 号标记高度不同时，它在参考平面上的
投影会沿**天底点**（相机光心正下方那块场地）方向被位似缩放：

```
X = N + k · (X_c − N),        k = (Hc − h_target) / (Hc − h_corner)
```

`k` 只由三个高度决定，**与画面内容、相机内参、场地实际边长都无关**（分子分母同除边长即知）；
同高或相机在无穷远时 `k = 1`。本场地：`h_corner = 50mm`（= 墙高）、`h_target = 23mm`，
目标比参考平面**低 27mm** → `k > 1`，修正把结果**推离**天底点。

修正有两个容易搞错的点，代码里已按此实现：
- 它是**位似变换**，不改形状、不改方向 → **只修正位置，`theta` 不动**（不需要旋转标记）。
- 位姿拟合必须**放在修正之后**，位置与方向才自洽。

```yaml
camera:
  field_size_mm: 1440.0       # 8×8 格 × 180mm；只用于把修正量换算成 mm
  parallax:
    enable: true
    corner_height_mm: 50.0    # 参考平面（角标记）离地高度 = 墙高
    target_height_mm: 23.0    # 0 号标记离地高度
    camera_height_mm: null    # ★ 必填：镜头光心离地高度
    nadir_xy: [0.5, 0.5]      # 相机正下方那块场地（归一化坐标）
    from_intrinsics: true     # nadir_xy=null 且有内参时，自动求天底点
```

各输入的量级要求（都不苛刻）：

| 参数 | 精度要求 | 原因 |
|---|---|---|
| `corner_height_mm` / `target_height_mm` | ±2 mm | 直接进 `(Hc − h)` 的差 |
| `camera_height_mm` | ±20% | `k−1 = Δh/(Hc−h_c)`，`Hc` 的相对误差**等比例**传入修正量；本场地修正量 ≈9mm，故绝对残差仅 ~1.8mm（0.13% 边长） |
| `nadir_xy` | **最关键** | 修正量正比于 `\|x − N\|`，天底点错 0.1 结果就偏 0.1 |

天底点两条获取途径：
1. **直接标定**（推荐）：从镜头吊铅垂线，落点处**与角标记同高**平放一张标记，管线给出的
   `(x, y)` 就是天底点 —— 天底点处修正量恒为 0，无需任何换算。
2. **内参**：标定相机后把 `intrinsic_file` 指向结果、`nadir_xy: null`，程序用「图像→场地」
   单应的**逆** + 内参分解出光心位置（这一步方向极易搞反，`parallax.py` 内部已代为求逆）。
   纯数值验证（内参相同时）误差 0.00mm；在完整合成场景里因检测噪声，天底点约偏 2% 场地边长
   （30mm），但位置误差仍优于"真天底"路线（3.88mm vs 4.04mm）。

修正命中后打 `height_corrected`，`diagnostics.parallax` 记录 `k` / `nadir_source` /
`shift_field` / `shift_mm`；开了但没标定则不静默、不猜，打 `parallax_uncalibrated` 并把
缺什么写进 `diagnostics.parallax.reason`。

合成自检 `--heights` 用真正的 3D 场景验证（角标记与目标不同高度）：
**同高对照组修正量恰为 0.000mm**（空操作护栏）；真实高度下修正后误差与"零视差基线"
统计上完全一致（4.231mm vs 4.432mm），即高度差的影响被消干净。

---

## 10. 关键配置项（`config.yaml`）

```yaml
detection:
  dict_name: DICT_4X4_1000
  corner_ids: [1, 2, 3, 4]
  target_id: 0
  edge_refine: true            # ★ 精度核心：边缘直线拟合亚像素精化
  edge_search_px: 2.5          # 沿外法线初始搜索半宽
  edge_step_px: 0.25           # 灰度剖面采样步长
  edge_trim: 0.15              # 每条边两端裁掉比例（避开圆角）
  edge_bright_outside: true    # 黑标记贴浅色地面
  edge_max_shift_ratio: 0.35   # 角点位移上限（相对边长），超限放弃精化
  scales: [1.0]                # 标记很小时改 [1.0, 2.0] 提高检出率
  clahe: false                 # 光照不均（顶灯偏一侧）时打开

geometry:
  vertex_mode: auto            # auto | nearest | search | full
  vertex_margin_weight: 0.1    # 内顶点代价里"距离余量"项权重；必须 >= 0.05（见下）
  max_square_rms: 0.02         # 方正度门限（场地单位）
  min_origin_margin: 0.05      # 低于则标 ambiguous_origin
  min_axis_margin: 0.05        # 低于则标 ambiguous_axis
  require_all_corners: true    # 缺角标记即判无效

refine:
  enable: true                 # 16 点联合最小二乘精化

output:
  smooth: none                 # none | ema | moving_average
  ema_alpha: 0.4
  window: 5

camera:
  undistort: false             # 走标定时打开
  intrinsic_file: null         # calibrate_camera.py 输出；也给"自动求天底点"用
  field_size_mm: 1440.0        # 8×8 格 × 180mm，仅用于把修正量换算成 mm
  parallax:                    # 差高修正，见第 9 节
    enable: true
    corner_height_mm: 50.0     # 角标记（= 墙顶）离地高度
    target_height_mm: 23.0     # 0 号标记离地高度
    camera_height_mm: null     # ★ 必填：镜头光心离地高度
    nadir_xy: [0.5, 0.5]       # 相机正下方场地位置（归一化）
    from_intrinsics: true      # nadir_xy=null 且有内参时自动求天底点
```

**内顶点选择**：四角标记的内侧角点（朝向场内的那个角）需要从 4 个角里挑出来。共 7 种取值，
**v1.1.0 起默认是 `xdiag_plane`**：

| `vertex_mode` | 做法 | 内顶点那一步耗时 | 备注 |
|---|---|---|---|
| `xdiag_plane` | **对角线法** + 平面空间比角度 | 0.41 ms | **【默认】无超参、无搜索**；精度与 `auto` 持平，强透视下更好 |
| `xdiag_unit` | 对角线法 + 图像空间比角度 | 0.26 ms | 省一次局部仿射拟合，精度略逊 |
| `xdiag` | 对角线法 + 图像空间原始点乘 | 0.25 ms | 不推荐：受投影各向异性偏袒 |
| `nearest` | 离场地中心最近的角点 | 0.13 ms | 最快，但强透视/标记转动大时易错 |
| `auto`/`search` | 形状残差 + 距离余量**加权代价** | 6.9 ms | 需 `vertex_margin_weight`，见下方告警 |
| `full` | 穷举 4⁴ 组合 | 8.4 ms | 最慢 |

**对角线法**（默认）：① 四个标记**中心**两两配对，取"两条连线相交"的那一种 = 真正的对角配对
（凸四边形只有对角配对相交，而透视保凸保关联 → 与相机姿态无关）；
② 每条对角线取"指向对端"的方向，两个端点的标记各取 4 个角点中**与它点乘最大**的作为内顶点。

关键点是这个点乘在**哪个空间**里做。方向与角点偏角都要先用该标记自身四边形拟合出的
**局部仿射**拉回平面空间再比 —— 图像空间直接被透视剪切扭曲，实测正确率
84%（原始点乘）→ 97%（单位向量）→ **100%（平面空间）**。见
[docs/评估-对角线法选内顶点.md](docs/评估-对角线法选内顶点.md)。

> 安全闸 `geometry.min_diag_sin = 0.15`：两条对角线相交角的正弦下限。正常机位下近乎正交
> （实测最低 0.807），仅"四点近共线"（相机贴地掠过）会掉到 0.04；触发时判退化并回退 `nearest`。

**加权代价**（仅 `auto`/`search`/`full` 用，非默认）：

```
cost = mean(相对形状残差) + vertex_margin_weight × mean(顶点距离余量惩罚)
```

> ⚠ **`vertex_margin_weight` 必须 >= 0.05，默认 0.1（临界值实测 0.037）；仅对
> `auto`/`search`/`full` 生效**（默认档 `xdiag_plane` 不读此参数，参数校验会直接拒绝 <0.05）。
> 「形状残差」是**辅助**判据且**退化**：「离场地中心最近的那个角就是内侧角」才是**主力**判据。
> 原因：4 个点唯一确定一个单应，因此形状残差在不同候选组合之间只差 ~1e-4
> （实测 0.02996 vs 0.03310），且它本质上是"用候选自己定的（可能是错的）正方形去拟合"，
> 错误组合反而可能把它压低（过拟合）。而「距离余量」项的量级是 0.000 vs 0.086，
> 比形状项大 3 个数量级。权重取 0.02 时形状项会盖过距离余量 → 选中错误内顶点
> （2026-10-03 新样本上 15/18 帧中招，位置偏 10~45mm、连拍帧间跳 9.6mm）；
> 取 0.05 及以上即与 `nearest` 完全一致，见 [CHANGELOG §1.0.1](CHANGELOG.md)。

`max_vertex_search` 限制组合数上限（仅 `auto`/`search` 用；`2` 近邻 → `2⁴=16`；`0` 表示不限）。

---

## 11. 实现要点

1. **检测**：`ArucoDetector` 检出全部标记 → 取 ID 1~4 与 ID 0。
2. **亚像素精化**（`corners.py`）：对每个标记的 4 条边，沿外法线采样灰度剖面，
   用一阶导数极值定位边缘（梯度符号排除内部 code 区域的假边），
   再做总体最小二乘直线拟合 + sigma 截断，相邻边求交得到亚像素角点；迭代 2 次。
   带多重稳健性守卫（边长 ≥ 4px、每条边最少点数、位移上限、凸性检查），
   任一条不满足就放弃本次精化、保留原角点。
   > 关键细节：**不能**按质心角重排顶点 —— 正方形有 4 重对称，重排会把标记的 +x 轴整体转 90°。
   > 外法线由遍历方向（有向面积符号）确定。
3. **内顶点选择** → **场地坐标系构建**（自动判定原点与轴向，不做首次登记）。
4. **联合最小二乘精化**：把 16 个角点一起用上（`refine.enable`）。
5. **方正度校验**：角标记映射回正方形，`rms_field` 超门限判无效。
6. **0 号标记位姿**：中心 + 方向单位向量 + θ。
7. **可选时序滤波**（默认关闭）。

---

## 12. 约定与红线

- 所有阈值 / 字典 / 相机参数**必须走 `config.yaml`**，代码里不硬编码。
- 缺角标记 → 该帧无效，**不补齐、不兜底**。
- 每帧**自动判定坐标系**，不做"首次登记后锁定"。
- 按**共面假设**处理，高度/倾角逐差修正作为预留参数。
- 时序滤波**默认关闭**，只留开关。
