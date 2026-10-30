# aruco-locator v1.0 —— 接口文档

> 面向**调用方**（把本模块当算法库用的程序）。
> 参数逐项说明见 [参数参考.md](参数参考.md)（由 `python -m aruco_locator.docgen` 自动生成）。
> 项目背景、坐标系由来、算法原理与精度实测见仓库根目录 [README.md](../README.md)。

---

## 0. 它做什么 / 不做什么

**做**：从一张包含四个角标记（ID 1~4，`DICT_4X4_1000`）和一个目标标记（ID 0）的图像中，
解出目标标记在场地坐标系下的**位置、方向**，并给出质量指标与置信标志。

| 项目 | 说明 |
|---|---|
| 输入 | 一张 BGR 图像（`numpy.ndarray`，`uint8`，`H×W×3`），画面须同时含全部 4 个角标记与目标标记 |
| 输出 | 位置 `(x, y)` ∈ [0,1]²、方向 `theta_deg` / 单位向量 |
| 依赖 | `opencv-contrib-python`、`numpy`、`pyyaml`；`scipy` 仅联合精化用（缺失会自动降级） |
| 相机标定 | **不需要**。主路径是无标定单应；标定只在广角畸变明显或需要自动求天底点时用 |
| 多目标 | 不支持。一个定位器 = 一个目标 ID（`detection.target_id`） |
| 时序/跟踪 | 只提供可选的一阶滤波（EMA/滑动平均），不做数据关联、不做预测 |

**不保证**的事：角标记缺失时不会补齐、不会外推，直接判该帧无效并给出 `reason`。

---

## 1. 坐标与输出约定（v1 冻结，不会变）

- **原点** = 离画面左下角最近的场地角点（即场地左下角）。
- **+x** 沿场地底边向右，**+y** 沿左边向上；场地恒归一化为 `[0, 1]²`（除以场地边长）。
- **位置** = 目标标记的几何中心。
- **方向基准** = 目标标记**自身的 +x 轴**（OpenCV 角点序 `corners[0] → corners[1]`），
  不是场地轴、也不是图像轴。
- **`theta_deg` = `atan2(vy, vx)`**，值域 `(-180°, 180°]`，**逆时针为正**。
- `dir_x / dir_y` 与 `theta_deg` 等价（`dir = (cos θ, sin θ)`），直接给出可省掉下游的三角函数。

> 这四条是接口契约的一部分。如果下游需要"顺时针为正"或"原点在左上"，请在调用方转换，
> 不要改本库的约定。

---

## 2. 集成方式

**① 同目录导入（最简单）**
项目根目录本身就是包所在目录，`import aruco_locator` 即可。

**② 拷贝为子目录**
把 `aruco_locator/` 整个目录拷进你的工程，`import aruco_locator`。

**③ 子进程 + JSONL（跨语言推荐）**
```bash
python -m aruco_locator.cli --source input.mp4 --out out_dir --quiet
# 产出 out_dir/results.jsonl（每行一个结构化 JSON）与 results.csv（扁平表）
```
JSONL 每行即一帧结果，字段见 §5。

**④ 参数注入**
配置文件（YAML）与代码内覆盖两种方式，见 §6。

---

## 3. 快速开始

```python
import cv2, numpy as np
from aruco_locator import create_locator, locate

# --- 方式 A：一次性调用（互不相关的图片）---
buf = np.fromfile("photo.jpg", dtype=np.uint8)      # 中文路径请走 fromfile+imdecode
img = cv2.imdecode(buf, cv2.IMREAD_COLOR)

res = locate(img, "config.yaml")                     # 一行搞定
if res.valid:
    print(res.x, res.y, res.theta_deg)
else:
    print("无效：", res.reason)

# --- 方式 B：常驻实例（视频/摄像头，保留滤波状态）---
loc = create_locator("config.yaml", profile="video")
cap = cv2.VideoCapture(0)
loc.reset()
while True:
    ok, frame = cap.read()
    if not ok:
        break
    res = loc.locate(frame)
    if res.valid:
        robot_xy = (res.x, res.y)          # 场地归一化坐标
        heading  = (res.dir_x, res.dir_y)  # 单位向量
    vis = loc.annotate(frame, res)         # 可选：叠加可视化
```

---

## 4. 公开接口清单

`aruco_locator.__all__` 中列出的名字即为 v1.0 的稳定接口；其余名字属内部实现。

### 4.1 装配与调用

| 接口 | 签名要点 | 说明 |
|---|---|---|
| `create_locator` | `(config=None, *, config_file=None, profile=None, params=None, **overrides)` | **推荐入口**。装配一个定位器 |
| `ArucoLocator` | `ArucoLocator(config=None)` | 定位器类；`config` 可为 `LocatorConfig` 或 YAML 路径 |
| `locate` | `(frame_bgr, config=None, *, frame_index=0, timestamp=0.0, profile=None, params=None, **overrides)` | 一次性解算一帧，返回 `Localization` |
| `locate_image` | `(path, config=None)` | 读单图并解算 |
| `locate_folder` | `(path, config=None)` | 图片目录批量解算，返回 `list[Localization]` |
| `locate_video` | `(path, config=None, max_frames=None)` | 视频批量解算 |
| `iter_locations` | `(source, config=None, **kw)` | 生成器：逐帧 `yield Localization`（`source` 可为图片/目录/视频/`camera:0`） |

### 4.2 `ArucoLocator` 方法

| 方法 | 说明 |
|---|---|
| `locate(frame_bgr, frame_index=0, timestamp=0.0) -> Localization` | 解算一帧。**线程不安全**，见 §10 |
| `annotate(frame, loc, obs=None) -> ndarray` | 生成叠加图（检测框、场地多边形、目标位姿箭头、俯视小窗） |
| `reset()` | 清空时序滤波状态（切换视频源时调用） |
| `undistort(frame) -> ndarray` | 按配置做畸变校正（未启用则原样返回） |
| `cfg` | 当前 `LocatorConfig`，可直接读 |
| `last_debug` | 上一帧的中间量（`obs` / `H` / `frame_obj`），仅供调试 |
| `locate_grid(frame_bgr, H=None, frame_index=0, timestamp=0.0) -> Localization` | **格点匹配通道**：不需要 0 号标记被 ArUco 检出，只要四角齐全。`H` 给 `None` 时内部只用四角解一次（见 §4.5） |
| `grid_matcher` | 格点匹配器（按 `config.grid` 惰性构造），可直接用它的 `search()` / `accept()` |

### 4.3 参数接口

| 接口 | 说明 |
|---|---|
| `parameter_catalog(path=None, group=None) -> list[dict]` | 参数目录（机器可读）：名字/类型/默认/单位/范围/说明 |
| `catalog_markdown(group=None) -> str` | 目录渲染成 Markdown 表 |
| `get_param(target, path)` | 读参数。`target` 可以是 `LocatorConfig` 或定位器 |
| `set_param(target, path, value, validate=True)` | 写参数（自动类型转换 + 范围校验） |
| `apply_params(target, params=None, **overrides)` | 批量写；接受点号路径、叶子名简写、嵌套字典 |
| `apply_profile(target, profile)` | 套用预设档位 |
| `flatten_config(target) -> dict` | 导出为"点号路径 → 值"（便于落盘/比对） |
| `list_profiles()` / `describe_profiles()` | 档位名列表 / 档位详情 |
| `PROFILES` | 档位原始定义（`dict[str, dict]`） |

### 4.4 配置类与底层构件（进阶）

| 接口 | 说明 |
|---|---|
| `LocatorConfig` 及 `Detection/Geometry/Refine/Output/Camera/ParallaxConfig` | 配置数据类（dataclass），字段即参数 |
| `load_config(path) -> LocatorConfig` | 读 YAML；未知键会抛 `ValueError`（防拼错） |
| `config_to_dict(cfg) -> dict` | 配置转可序列化字典 |
| `refine_quad_by_edges(gray, quad, ...)` | 边缘直线拟合亚像素精化（独立可用） |
| `correction_factor` / `apply_parallax` / `nadir_from_intrinsics` / `pose_from_plane_homography` / `camera_height_from_factor` / `factor_from_scales` | 差高视差修正的数学构件 |
| `MarkerDetector` / `MarkerObservation` | 标记检测层 |
| `GridConfig` / `GridMatcher` / `marker_bits` | 格点匹配通道（v1.2）：配置、匹配器与 ID 模板生成 |

---

### 4.5 格点匹配通道（v1.2 新增，第二通道）

**做什么**：把 0 号标记的识别做成**一次匹配滤波**。用四个角标记解出的 `H` 把画面
warp 成场地正射图，然后在**整幅正射图上**搜“36 格图案最吻合的位置与朝向”。
它不需要轮廓、不需要位判决 —— 所以运动模糊把卡片内格压成斜坡时（ArUco 的第一段
就死在这里），只要灰度结构还在就能定位。

```python
loc = create_locator("config.yaml")

r = loc.locate_grid(frame)            # H 内部用四角自解；不需要 0 号被检出
print(r.x, r.y, r.theta_deg, r.valid, r.diagnostics["grid"])

r = loc.locate_grid(frame, H)         # 复用已有 H（session 里已经解过时）
```

**前提与代价**

| 项 | 值 |
| --- | --- |
| 输入 | **彩色帧**（BGR）。色度用来压制不可信像素，灰度图会丢掉这一维 |
| 前提 | 四个角标记齐全（要它们的 `H`）。缺角即无效，不做兜底 |
| 代价 | 约 2.8 s/帧（4207 px 正射 + 两级搜索）；`grid.enable` 因此默认 `false` |
| 精度 | 与 ArUco 成功帧的位置差：中位 1.3 mm / 最大 3.1 mm（两套独立算法互证） |
| 高度 | **不做差高视差修正**。解的是“场地平面（角标记高度）”上的位置，`diagnostics.grid.parallax = "not_applied"`；目标低一截时请按 §9 自行修正 |
| 线程 | `GridMatcher` 无共享可变状态（几何参数在 `GridGeometry` 里），可多进程并行 |

**判据**（`diagnostics.grid`）

| 量 | 定义 | 用法 |
| --- | --- | --- |
| `gap` | `min(白格) − max(黑格)` | `gap > 0` ⟺ 36 格全部判对。**X 域量纲**（±1 量级），旧灰度域的 20 级 ≈ 0.06 |
| `sharp` | `gap / max(白格极差, 黑格极差)` | **主判据**：双电平性。平滑灰度区（地面、白墙）分子分母同时趋零，只有它能区分真假 |
| `margin` | `gap / abs(mean(白) − mean(黑))` | **只作选解准则**（全局最大），不作接受门限 |
| `ncc` / `n_bad` | 与核的归一化相关 / 错格数 | 仅诊断 |

操作点：`grid.sharp_min = 0.20`、`grid.gap_min = 0.06`（100 帧标定：0 错收 / 83% 召回）。
`accept(sol)` 即按这两个门限判定；`κ` 只进相关面（选解），**不进判据** ——
否则就是自造打分函数。

**与 `locate()` 的关系**：`locate()` 在 `target_marker_missing` 且 `grid.enable`
时自动调用本通道；成功则清空 `reason`、打 `grid_matched` 标志、写
`diagnostics.channel = "grid"`；失败写 `"grid_rejected"` 并保留原 `reason`。

---

## 5. 结果字段契约（`Localization`）

### 5.1 直接属性

| 字段 | 类型 | 单位/范围 | 含义 |
|---|---|---|---|
| `valid` | bool | — | 该帧是否可信。**False 时下列数值字段均为 None，不要使用** |
| `x`, `y` | float | 场地归一化 `[0,1]²` | 目标标记中心。正常应落在 `[0,1]`，略超出说明几何有问题 |
| `theta_deg` | float | `(-180, 180]` | 方向角，逆时针为正 |
| `dir_x`, `dir_y` | float | 单位向量 | 与 `theta_deg` 等价 |
| `x_raw`, `y_raw`, `theta_deg_raw` | float | 同 `x/y/theta_deg` | **滤波前**的值（`smooth: none` 时与上面相同） |
| `frame_index` | int | — | 帧号（调用方传入或由迭代器给出） |
| `timestamp` | float | 秒 | 时间戳（摄像头为系统时间，视频为 `POS_MSEC`，图片为 0） |
| `origin_id` | int \| None | — | 被判为原点的角标记 ID（每次自动判定，不做登记） |
| `corner_ids` | list[int] | — | 命中的角标记 ID（升序） |
| `missing_corner_ids` | list[int] | — | 缺失的角标记 ID |
| `rms_field` | float \| None | 场地单位 | 角标记映射回正方形的**最大**残差。健康值 < 0.005 |
| `target_rms` | float \| None | 场地单位 | 目标标记同类残差 |
| `marker_scale` | float | 场地单位 | 目标标记边长 / 场地边长 |
| `scale_cv` | float \| None | — | 五个标记尺度的离散度（标准差/均值）。健康值 < 0.05 |
| `flags` | list[str] | — | 质量标志，见 §5.3 |
| `reason` | str | — | 无效原因，`valid=True` 时为空串 |
| `diagnostics` | dict | — | 中间量（内顶点余量、天底点、视差修正量等），仅供调试 |

### 5.2 序列化

| 方法 | 产物 | 用途 |
|---|---|---|
| `to_dict(include_diagnostics=True, flat=False)` | 结构化 dict | **给程序/JSON 用**：`corner_ids`、`flags` 是真正的 list |
| `to_dict(flat=True)` / `to_row()` | 扁平 dict | 写 CSV：多值字段压成 `"1;2;3;4"` |
| `to_json(include_diagnostics=True, flat=False)` | JSON 字符串 | 落盘 / 跨进程传输 |
| `summary()` | str | 单行人类可读摘要 |

```python
>>> res.to_dict()["corner_ids"]
[1, 2, 3, 4]
>>> res.to_row()["corner_ids"]
'1;2;3;4'
```

### 5.3 `flags` 全集

| flag | 含义 | 建议处理 |
|---|---|---|
| `ambiguous_origin` | 原点判定余量过小 | 检查角标记是否贴得太靠里；必要时人工确认坐标方向 |
| `ambiguous_axis` | 轴向余量过小，`x/y` 可能整体互换 | **固有几何退化**（相机绕光轴 ≈ ±45°）。调整相机安装角，或在下游按此标志处理 |
| `handedness_flipped` | 手性翻转 | 确认是否从场地背面拍摄 |
| `inner_vertex_override` | 加权代价推翻了"最近顶点"启发式（**仅 `auto`/`search`/`full`**；默认档 `xdiag_plane` 不产生） | 一般伴随角点质量差，建议换 `vertex_mode: xdiag_plane` |
| `inner_vertex_degenerate` | 对角线配对退化（四点近共线），已自动回退 `nearest`（**仅 `xdiag*`**） | 相机接近贴地掠过；抬高机位即可。看 `diagnostics.inner_vertices.diag_pair_sin` |
| `inner_vertex_uncertain` | 最优/次优内顶点组合代价太接近（**仅 `auto`/`search`/`full`**） | 看 `diagnostics.inner_vertices.cost_gap_rel`；频繁出现说明标记太小/太斜 |
| `target_not_square` | 目标标记映射后不够方正 | 可能被遮挡或变形，结果可用但需留意 |
| `target_rectified` | 目标在**场地平面重投影视图**里找回（原图未检出） | 正常，通常出现在标记被透视压扁的帧；位置精度与正常检出同级（实测差 <1mm） |
| `smoothed` | 该帧结果被时序滤波修改过 | 正常；需要原始值读 `*_raw` |
| `height_corrected` | 已施加差高视差修正 | 正常 |
| `parallax_uncalibrated` | 开了视差修正但参数不全 | **结果未修正**，按 `diagnostics.parallax.reason` 补齐相机高度/天底点 |
| `grid_matched` | 该帧的解来自**格点匹配通道**（不是 ArUco 位判决） | 正常；看 `diagnostics.grid.gap/sharp` 判断可信度。**只在判据通过时打**（`valid=False` 的帧即便走了本通道也不带此标志，用 `diagnostics.channel == "grid_rejected"` 区分） |

`valid=True` 也可能带 flag —— flag 是**质量提示**，不是失败。下游可据此降级使用
（例如 `ambiguous_axis` 时同时接受 `(x,y)` 与 `(y,x)`）。

### 5.4 `reason` 全集（`valid=False` 时）

| reason | 含义 | 处理 |
|---|---|---|
| `corner_markers_missing` | 角标记不全（`missing_corner_ids` 给出是谁） | 检查拍摄范围与标记摆放 |
| `no_corner_markers` | 一个角标记都没检出 | 检查字典名、标记是否印反 |
| `target_marker_missing` | 目标标记未检出 | 检查遮挡 |
| `need_four_corners` | 角标记数量不足以定位 | 同上 |
| `inner_vertex_failed` | 内顶点选择失败 | 多为角点质量差 |
| `degenerate_homography` | 单应退化（四点近共线等） | 检查角标记是否被误检 |
| `square_check_failed` | 角标记映射后不是正方形（`rms_field` 超门限） | 有误检，或角标记不在同一平面 |
| `grid_low_confidence` | 只走 `locate_grid()` 且 `sharp`/`gap` 未过门限 | 看 `diagnostics.grid`：`sharp` 低=双电平性差（糊/不是卡片），`gap` 低=对比度不足 |
| `no_homography` | （工具层）四角缺失且插值范围外，取不到 `H` | 换机位/补角标记 |

---

## 6. 可调参数

共 **59** 个参数，分 5 组：`detection` / `geometry` / `refine` / `output` / `camera`。
完整表格与逐条说明见 **[参数参考.md](参数参考.md)**。

### 三种覆盖方式

```python
# ① 装配时直传（叶子名简写，需无歧义）
loc = create_locator(edge_search_px=4.0, vertex_mode="full")

# ② 参数字典（点号路径 / 嵌套两种写法都行）
loc = create_locator("config.yaml", params={
    "detection.scales": [1.0, 2.0],
    "output": {"smooth": "ema"},
})

# ③ 运行期改
from aruco_locator import set_param, apply_params
set_param(loc, "geometry.vertex_mode", "full")
apply_params(loc, {"camera.parallax.camera_height_mm": 2000.0})
```

### 校验行为（重要）

- 参数名写错 → `KeyError`（叶子名有歧义也会提示可用全名）。
- 类型不符 → 自动宽松转换（`"4.0"` → `4.0`，`"1.0,2.0"` → `[1.0, 2.0]`）；转不动才抛 `ValueError`。
- 数值超出建议范围 → `ValueError`（**不是警告**，会挡住明显手滑）。
- YAML 里出现未知键 → `ValueError`（避免拼错的键被静默忽略）。
- 值为 `None` 表示"保持不动"；只有内参、场地边长、相机高度、天底点允许显式写 None。

### 预设档位

`create_locator(profile=...)`。档位只覆盖少量参数，可与 `params` / `**overrides` 叠加：

| 档位 | 用途 |
|---|---|
| `default` | 内置默认值，推荐起点 |
| `fast` | 关边缘/联合精化，实测约省 15%，位置精度略降 |
| `accurate` | 精度优先：内顶点 4⁴ 穷举 + 收紧方正度门限 |
| `robust` | 鲁棒优先：多尺度 + 大搜索窗，约 2.7x 耗时 |
| `small_markers` | 标记在画面里很小（<40px） |
| `oblique_small` | 标记小而斜、边缘发虚 |
| `video` | 打开 EMA 时序滤波 |
| `no_parallax` | 关闭差高视差修正（目标与角标记共面时） |

> **性能提示**：实测唯一显著的性能杠杆是**图像分辨率**与**多尺度 `detection.scales`**
> （×2.7）。其余参数对耗时的影响都在噪声量级 —— 不要指望调参数显著提速。

---

## 7. 异常约定

| 异常 | 触发条件 |
|---|---|
| `FileNotFoundError` | YAML 配置不存在；输入源路径无法识别 |
| `RuntimeError` | 图片读不出来、视频/摄像头打不开、目录里没有图片 |
| `ValueError` | YAML 出现未知配置键；参数类型转换失败；参数超范围；`config` 与 `config_file` 同时给 |
| `KeyError` | 参数路径不存在；叶子名简写有歧义；档位名未知 |
| `TypeError` | 参数接口的 `target` 不是 `LocatorConfig` 也不是带 `.cfg` 的对象 |

**不会**抛异常的情况：一帧解不出来（返回 `valid=False` + `reason`）、边缘精化失败
（静默回退到原始角点）、`scipy` 缺失（自动跳过联合精化）。这样设计是为了让
"某一帧质量差"永远不打断视频流。

---

## 8. 性能

实测环境：本项目开发机（Windows / Python 3.12 / opencv-contrib 4.11），
输入为真实迷宫照片缩放后的各分辨率，交错测量取中位值。

| 分辨率 | 兆像素 | `default` | `robust` |
|---|---|---|---|
| 4080×2296 | 9.4 | 258 ms | 678 ms |
| 2560×1440 | 3.7 | 97 ms | 273 ms |
| 1920×1080 | 2.1 | 66 ms | 188 ms |
| 1280×720 | 0.9 | 38 ms | 83 ms |
| 960×540 | 0.5 | 25 ms | 62 ms |

耗时近似正比于像素数。**实时场景建议在 1280×720 附近**（默认档约 26 fps；
若标记在画面里很小，可上 1080p 配合 `small_markers` 档）。

参数对耗时的实测影响（4080×2296，相对默认档）：多尺度 `[1,2]` **2.66x**、
顶点 `full` 穷举 1.21x、关边缘精化 0.92x、关联合精化 1.05x（几乎免费）。

---

## 9. 已知局限

- **±45° 轴歧义（固有）**：相机绕光轴的滚动角接近 ±45° 时"底边"与"左边"在图像上不可区分，
  会导致 `x/y` 互换，打 `ambiguous_axis`。这不是 bug，调整相机安装角即可避开。
- **强透视下的环序**：已修复（改用绕质心角度排序）。但极端透视（相机几乎贴地）仍可能失败，
  表现为 `rms_field` 变大。
- **角标记必须落在同一平面**，且目标标记与它们的**高度差**要用 `camera.parallax.*` 修正，
  否则位置会有约 `(k-1)·|x-N|` 的系统偏差（本项目实测约 1.4% 场地边长）。
- **标记与深色结构粘连**会导致连候选四边形都提不出来（二值化后连成一片）。
  这是**物理摆放问题**：请让标记与深色背景之间留出 ≥1 个标记宽的浅色间隙。
- **误检风险**：场地上有重复纹理时，放宽 `error_correction_rate` /
  `polygonal_approx_accuracy_rate` 会解出大量伪 ID。调参时务必盯伪 ID 数量。

---

## 10. 线程安全与并发

- `ArucoLocator` **线程不安全**（内部持有 `last_debug`、滤波器状态）。一个实例一个线程。
- 多相机 / 多线程：每个流各自 `create_locator(...)`，互不共享。
- `locate()` 便捷函数每次都新建定位器，适合无状态的并行调用（会重复付出装配开销，约数毫秒）。
- 只读操作（`parameter_catalog()` 等）无状态，可并发。

---

## 11. 版本与兼容性

- 当前版本 **1.2.0**（`aruco_locator.__version__`）。变更记录见 [CHANGELOG.md](../CHANGELOG.md)。
- `__all__` 规则（v1.2 起）：**已有 36 个名字不得消失**，新名字追加在末尾。
  当前 39 名（新增 `GridConfig` / `GridMatcher` / `marker_bits`）。
  `tools/check_api_surface.py` 同时断言“基线名全在”与“基线清单 = 36 名”，
  既能抓住静默回退，也不会阻止新增。
- **v1.x 承诺**：§1 的坐标与角度约定、§4.1/4.2/4.3 中列出的接口名与语义、
  §5 的字段名、§5.3/5.4 的标志与原因字符串**不会破坏性变更**。
- 新增接口、新增字段、新增 flag 属于**向后兼容**变更，会在次版本号（1.1.0）中给出。
- 弃用流程：先标记 `DeprecationWarning` 并保留至少一个次版本，再于 2.0 移除。
- 内部模块（`detector` / `corners` / `geometry` / `viz` / `sources` 的函数）不承诺稳定性，
  但它们中的「精化/视差构件」已通过 §4.4 显式导出，这部分按公开接口对待。

---

## 12. 文件清单

```
Auto Locate/
├── aruco_locator/                 # 算法包（可整体拷走）
│   ├── __init__.py                # 公开接口与 __all__
│   ├── api.py                     # ArucoLocator / Localization / create_locator / locate
│   ├── parameters.py              # 参数注册表：目录、读写、校验、档位
│   ├── docgen.py                  # 参数文档生成器
│   ├── config.py                  # 配置数据类与 YAML 载入
│   ├── detector.py                # 标记检测
│   ├── corners.py                 # 边缘直线拟合亚像素精化
│   ├── geometry.py                # 内顶点选择 / 坐标系 / 位姿拟合 / 联合精化
│   ├── parallax.py                # 差高视差修正
│   ├── sources.py                 # 图片/目录/视频/摄像头统一取帧（Unicode 路径）
│   ├── viz.py                     # 叠加可视化
│   └── cli.py                     # 离线批处理命令行
├── docs/
│   ├── API.md                     # 本文件
│   └── 参数参考.md                 # 自动生成的参数手册
├── examples/quickstart.py         # 可运行示例
├── calibrate_camera.py            # 棋盘格标定 → 内参
├── selftest_synthetic.py          # 合成自检（无实拍也能验证精度）
├── config.yaml                    # 本项目场地参数（可按需替换）
└── README.md                      # 项目说明与算法原理
```

---

## 13. 最小集成示例（子进程 + JSONL）

```python
import json, subprocess

subprocess.run([
    "python", "-m", "aruco_locator.cli",
    "--source", "arena.mp4",
    "--out", "out",
    "--quiet",
], check=True)

for line in open("out/results.jsonl", encoding="utf-8"):
    r = json.loads(line)
    if r["valid"]:
        x, y = r["x"], r["y"]
        dx, dy = r["dir_x"], r["dir_y"]
        if "ambiguous_axis" in r["flags"]:
            x, y = y, x          # 固有退化：×45° 轴向歧义
```
