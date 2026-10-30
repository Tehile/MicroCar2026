# 变更记录

本文件记录 aruco-locator 的版本变更。版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)：
破坏性变更进主版本号，新增接口/字段进次版本号，修复与内部改进进修订号。

v1.x 的稳定性承诺见 [docs/API.md §11](docs/API.md)。

---

## [未发布]（v1.2.0 开发中）

**新增会话级接口（视频文件 → 轨迹 + 迷宫），供外部程序离线调用。**

- 新增 `aruco_locator/session.py`：`run_session()` 一次调用产出
  `session.json`（视频元数据/时基体检/逐帧统计/会话标志 + 迷宫融合结果）、
  `trajectory.csv`（`t_video_s`/`t_rel_s` 时间轴）、`trajectory.png`、`maze_structure.png`。
  现以 `from aruco_locator.session import run_session` 导入，顶层导出随 v1.2.0。
- 时基体检 `preflight`：CFR/VFR 判定（`std(Δt)/mean(Δt) > 0.005` → `timebase_unreliable`）；
  时间一律"帧号÷标称 fps"并标注来源。
- **S4 迷宫多帧融合**：以"四角齐全"帧（不要求 0 号目标检出）逐帧测墙段 `cover/conf`，
  按段投票融合，输出 `n_frames_used` / `per_segment` / `consistency` 与
  `maze_inconsistent` 标志。配套新增 `ArucoLocator.field_frame_from_obs()`（纯四角求 H，
  纯增量，不改 `locate` 行为）。
- **运动模糊修复：白名单四边形重解码**（新增 `aruco_locator/redecode.py`）。
  运动模糊会让 OpenCV 在位判决阶段把真标记丢进 `rejectedImgPoints`，或解成错误 ID
  （实测 id2 → 998），表现为 `corner_markers_missing`。现在检测器保留这些四边形，
  warp 到 6×6 模块网格后**只在白名单 {角标记 + 目标} 里比 Hamming**（4 旋转），
  接受条件 `dist ≤2 / margin ≥3 / contrast ≥25 / 最短边 ≥10px`；错 ID 的检出重解码后替换。
  实测（121 帧抽样，4K 手持）：**四角齐全率 52% → 64%**，代价 +58ms/帧；
  合成自检回归通过（位置 0.497mm / 角度 0.158°）。
  新增参数 `detection.redecode`（默认 true）与 4 个阈值，置 false 可完全回到旧行为。
- **目标标记兜底：场地平面重投影**（`detection.rectified_target`，默认开启）。
  目标缺失时用四角 `H` 把图 warp 成场地正俯视（`rectified_side=4500` ≈1.9 px/mm），
  在该视图里再检测；命中则把角点映射回原图并打 `target_rectified` 标志。
  实测（121 帧 4K 手持）：**目标检出 8%→12%、有效定位 4%→7%**，代价 +312ms/帧；
  四角齐全 64% 不变（无回归）。精度校验：重投影找回的帧与相邻检测帧位置差 <1mm。
  新增参数 `rectified_target` / `rectified_side` / `rectified_margin`。
- **帧间跟踪补洞**（新增 `aruco_locator/track.py`）：目标检测失败但四角齐全时，
  用相位相关（模糊不改变频谱相位）推进位置，检测成功即重置锚点。
  会话参数 `track` / `track_max_gap` / `track_min_response`；
  `trajectory.csv` 增列 `track_resp`，`session.json` 增 `frames.n_tracked`，
  帧标志加 `tracked`。实测（120407，stride=1）：静止段有效帧 **77.5%**（59/120 由跟踪补出，
  偏差 <0.5mm）；跑动段 34%（锚点稀疏，两端有锚点的段内偏差 1.3mm）。
- 新增 `tools/check_api_surface.py`：断言 v1.x 公开契约、会话级接口、
  `field_frame_from_obs()` 与 `maze_extract` 被依赖符号同时存在。
  起因：`api.py` 曾被回退到旧版本，`field_frame_from_obs` 消失导致会话迷宫融合
  `AttributeError`，而导入与单帧定位全部正常——这类静默回退必须能被一条命令发现。
- `sync_led`（S3 LED 发车对轴）未实现，显式开启抛 `NotImplementedError`；
  视差逐帧化（S5）待相机内参（存疑 U3）。
- 实拍验证见 [docs/验证报告-2026-10-27最终视频.md](docs/验证报告-2026-10-27最终视频.md)：
  几何精度达标（静止重复性 0.4mm/0.9°、零穿墙、跨视频迷宫 121 段零分歧），
  但本素材成像条件下有效帧率仅 2.6%~7.4%，瓶颈在采集端而非算法。

### 新增：格点匹配通道（第二通道，`aruco_locator/gridmatch.py`）

**为什么**：ArUco 解 0 号标记是"找轮廓 → 位判决"两段式，本素材的失败几乎都发生在
第一段——运动模糊把卡片内格压成斜坡，四边形凑不出来（4K 视频 ID0 检出率 3%~8%）。
本通道不需要轮廓：用四角解出的 `H` 把画面 warp 成场地正射图，直接在**整幅图**上
搜"36 格图案最吻合的位置与朝向"，只要灰度结构还在就能定位。

- 新增 `aruco_locator/gridmatch.py`：`GridMatcher` / `GridGeometry` /
  `chroma_weight` / `to_weighted` / `kernel_for` / `grid_means` / `correlate` /
  `gap_surface` / `cell_diag` / `rotate_pad` / `marker_bits`。
  命名空间与外部的 `gm.D_PX` 全局彻底解耦（几何参数进 `GridGeometry`，可并发）。
- 新增 `ArucoLocator.locate_grid(frame, H=None)`：**不需要 0 号标记被 ArUco 检出**。
  `H` 给 `None` 时内部只用四角解一次（复用 `field_frame_from_obs`）。
  返回同一个 `Localization`，所以 CSV 列、`session`、`viz` 全部照旧可用。
- `locate()` 新增分支：`grid.enable=true` 且 `target_marker_missing` 时自动走本通道，
  成功则清空 `reason` 并写 `diagnostics.channel="grid"`（失败写 `"grid_rejected"`）。
  **默认关闭**（+2.8 s/帧），配置 `grid.enable` 或档位 `profile="grid_fallback"` 打开。
- 新增配置段 `grid`（22 个参数）与 `GridConfig`；`parameter_catalog()` 全部可枚举。
- 新增 `tools/render_grid_video.py`：用正式接口在真实视频上跑本通道并合成审阅视频
  （主画面卡片框 + 36 格特写 + HUD），支持四角缺失时单应插值（记 `hgap`）。
- 判据：`gap = min(白格) − max(黑格)`、`sharp = gap / max(白格极差, 黑格极差)`；
  操作点 `sharp ≥ 0.20 且 gap ≥ 0.06`（100 帧标定：0 错收 / 83% 召回）。
  **κ 只进相关面（选解），不进判据**，避免自造打分函数。
- 验收（本次合并）：与实验版 `experiments/preproc.py` 逐帧对拍**位置/角度/诊断量
  全部 0.0000 差异**（同一帧、同一 H，5 个抽样帧覆盖静/动/两种 rot）。
  真实视频整段（120407，592 帧，`--scales 1,2`）：单应实测 521 帧（88.0%）、
  **接受 462 帧（78.0%）**、有效连续段最长 75 帧（2.5 s）；
  与实验版位置差中位 0.00 mm / P90 1.14 mm / 98.5% ≤2 mm（仅看新版接受的帧）。
  详见 [docs/评估-预处理加权与核权重标定-2026-10-30.md](docs/评估-预处理加权与核权重标定-2026-10-30.md) §10、§11。
- 已知量纲：`grid.gap` 是 **X 域**（±1 量级），与旧灰度域 `gap`（单位=灰度级）
  不可直接比较，换算约 ×333（`gap_min 0.06 ≈ 20` 灰度级）。

## [1.1.0] — 2026-10-04

**默认内顶点策略改为「对角线法」（`xdiag_plane`），去掉加权代价里那个悬崖型超参。**

`geometry.vertex_mode` 默认值 `auto` → **`xdiag_plane`**。做法：四角标记**中心**两两配对，
取"两条连线相交"的那一种 = 对角配对（透视保凸保关联，与相机姿态无关）；再沿对角线方向
对 4 个角点做点乘取最大者。

关键细节：点乘必须在**平面空间**做（用该标记自身四边形拟合的局部仿射拉回），否则被投影
各向异性扭曲。合成真值（199 帧四角齐全、`rot_jitter=±40°`）正确率：
`xdiag` 84.4% → `xdiag_unit` 97.0% → **`xdiag_plane` 100%**，与 `auto` 持平。

### 为什么换

| 指标 | `xdiag_plane`（新默认） | `auto`（旧默认） |
|---|---|---|
| 合成真值正确率（199 帧） | **100%** | 100% |
| 标准合成定位误差（均值/最大） | 0.513 / 1.075 mm | 0.513 / 1.075 mm |
| 强透视 tilt=0.30 | **0.500 / 1.120 mm** | 0.986 / **23.359 mm**（野值） |
| 3D 视差路径 | 与 `auto` 完全一致 | — |
| **实拍 36 帧（28 有效）vs `auto`** | **逐位一致**（位置差 0.0000mm、角度差 0.0000°） | — |
| 内顶点那一步耗时 | **0.41 ms** | 6.9 ms（**慢 17×**） |
| 需要调的超参 | **0 个** | `vertex_margin_weight`，且 <0.05 会静默出错 |

价值不在"更准"（并列），而在于**同样准、快 17 倍、且彻底去掉了一个"调小就静默出错"的
悬崖型超参** —— 上一版只能靠把默认值从 0.02 抬到 0.1 来跨过 0.037 的临界点，安全垫仅 2.7 倍。
详见 [docs/评估-对角线法选内顶点.md](docs/评估-对角线法选内顶点.md)。

### 新增

- `vertex_mode` 新增 3 个取值：`xdiag` / `xdiag_unit` / **`xdiag_plane`（默认）**。
- 参数 `geometry.min_diag_sin`（默认 0.15）：对角线配对的安全闸。正常机位下两条对角线
  近乎正交（实拍最低 0.807），仅"四点近共线"（相机贴地掠过）会掉到 0.04；触发时判退化、
  自动回退 `nearest`。
- 新标志 `inner_vertex_degenerate`：对角线配对退化并回退 `nearest` 时置位。
  （这类兜底属"静默降级"，只看结果指标看不出来，必须显式告知。）
- 档位 `profile="costfn"`：退回 v1.0.x 的加权代价判据，仅用于对照/回归。
  `profile="xdiag"` 保留（= 显式声明默认值）。
- `selftest_synthetic.py` 新增 `--vertex-mode`，可不改配置直接跑指定策略。

### 变更

- `vertex_margin_weight`、`max_vertex_search`、`min_vertex_cost_gap_rel` 标注为
  **仅对 `auto`/`search`/`full` 生效**；默认档不再读它们。
- `vertex_margin_weight` 建议范围收紧为 `0.05 ~ 0.5`，参数校验会**直接拒绝** 0.02
  （v1.0.0 的默认值）—— 让那次事故在配置层面不可能复现。
- 档位 `fast` 不再冗余地显式设 `vertex_mode`（已是默认）。
- **项目整理**：`tools/` 由 38 个文件精简为 7 个常驻工具 —— 删除 29 个一次性排查脚本
  （`_probe_*` / `_crop_*` / `_duel` / `_diag_*` 等）、`validate_vertex_mode.py`（写死
  `nearest vs auto`）与 `eval_xdiag.py`；新增 `compare_vertex_modes.py` 取代二者
  （任意两模式对比 + 红胶带边界贴合的外部绝对参照）；
  `_diag_truth.py` → `validate_vertex_truth.py`、`_stress_pairing.py` → `stress_vertex_pairing.py`。
  清空 `out/` 中间产物 70MB；文档插图迁 `docs/figures/`（转 JPEG，7MB → 1.2MB），
  分析输出留档迁 `docs/evidence/`。**发行归档不再包含 `samples/`**（116MB）→
  v1.1.0 归档 **1.27MB / 40 文件**（v1.0.1 为 121MB）。

### 兼容性

**v1.x 接口未破坏。** `xdiag_plane` 与 v1.0.1 默认在全部可用实拍数据上**输出逐位相同**，
所以下游无需改动；未使用 `vertex_mode` 的调用方行为不变。
仅在以下情况需注意：① 依赖 `inner_vertex_uncertain` / `inner_vertex_override` 标志的调用方
—— 默认档不再产生这两个标志（它们是加权代价路径的产物），改读 `inner_vertex_degenerate`
或 `diagnostics.inner_vertices`；② 显式依赖 `vertex_mode` 默认值为 `auto` 的调用方。

---

## [1.0.1] — 2026-10-03

**修复内顶点误选导致的定位偏差。** 在 2026-10-03 新样本（36 帧手持俯拍、4 种角标记排布）
验证时发现：`geometry.vertex_margin_weight` 的默认值 0.02 偏低，会让代价函数里的
「形状残差」项盖过「距离余量」项，从而**选中错误的内顶点**。

- **现象**：15/18 有效帧选错，0 号标记位置偏 10~45 mm；同一机位连拍帧间跳 9.6 mm。
- **根因**：4 个点唯一确定一个单应，因此「形状残差」在不同候选组合之间只差 ~1e-4
  （实测 0.02996 vs 0.03310），且它本质上是"用候选自己定的错误正方形去拟合"，
  错误组合反而可能被压低（过拟合）。而正确的「距离余量」项量级为 0.0 vs 0.086，
  比形状项大 3 个数量级 —— 权重不足时被噪声淹没。
- **修复**：`geometry.vertex_margin_weight` 默认 `0.02 → 0.1`（实测 0.05 即为安全下界）。
  同时把该参数的建议范围收紧为 `0.05 ~ 0.5`，并在 `geometry._assignment_cost`
  的文档串里写清"为什么形状项只能是辅助判据"。

### 修复效果（2026-10-03 新样本，28 个有效帧）

| 指标 | 1.0.0 | 1.0.1 |
| --- | --- | --- |
| 模型正方形与场地边界红胶带的四边偏移极差（中位） | 21.9 mm | **3.5 mm** |
| 同一机位重复拍摄的位置最大偏差（中位 / 最大） | 0.23 / **7.17** mm | 0.24 / **0.99** mm |
| 推翻 `nearest` 的帧数 | 26/28 | **0/28** |
| 带 `inner_vertex_uncertain` 标志的帧数 | 28/28 | **0/28** |
| 合成自检（20 场景）位置均值 / 角度均值 | 0.432 mm / 0.226° | 0.432 mm / 0.226°（无回归） |

接口、字段名、`flags` / `reason` 字符串**均无变化**，属纯参数默认值修复，向后兼容。

### 新增

- `tools/validate_new_samples.py` / `validate_new_samples2.py`：
  新样本批量验证与六类证据分析（有效性 / 内部几何自洽 / 单应畸变 / 跨机位不变性 /
  内顶点敏感性 / 备选判据对照）。
- `tools/validate_vertex_mode.py`：内顶点判据的 A/B 对比。
- `docs/验证报告-2026-10-03新样本.md`：完整验证报告。

### 已否决的备选判据（记录在案，勿再尝试）

- **「四个角标记映射尺寸一致性最好」**（`tools/_probe_*` 系列探查）：能把尺寸离散度
  从 3.5% 压到 1.3%，但给出的内顶点组合**跨帧不自洽**（同一机位连拍会来回跳、位置差 24 mm），
  属过拟合，不可作主判据。
- **「角标记中心必须在场地外」**：在标记被旋转摆放时该条件不成立，会漏判。

---

## [1.0.0] — 2026-09-25

**首个归档版本。** 从此版本起，[docs/API.md](docs/API.md) 中列出的接口、字段名、
`flags` / `reason` 字符串视为**冻结契约**。

### 核心能力

- **无标定单应主路径**：由 4 个角标记（ID 1~4，`DICT_4X4_1000`）的 16 个角点联合最小二乘
  求出「图像 → 场地」单应，**不需要相机内参**；角标记朝向与 ID 排列顺序可任意。
- **场地坐标系自动判定**：每帧由内顶点自动定出原点（离画面左下角最近的场角）与 +x/+y 轴向，
  不做首次登记。输出恒为归一化 `[0,1]²`。
- **边缘直线拟合亚像素精化**（`corners.py`）：沿标记边缘外法线取灰度剖面，
  以导数极值定位亚像素边缘点，再用带 sigma 截断的总体最小二乘拟合直线、相邻直线求交得角点。
  实测消掉了 cv2 `cornerSubPix` 约 1.6% 的「标记整体缩小」系统偏置。
- **差高视差修正**（`parallax.py`）：角标记定义参考平面，目标标记不同高时结果会沿**天底点**
  方向被位似缩放 `k = (Hc − h_t)/(Hc − h_c)`；修正为纯位似（只改位置、不改方向）。
  天底点支持直接实测或由内参单应分解自动求出。
- **统一输入源**：单图 / 图片目录 / 视频 / 摄像头共用一套帧迭代接口，含 Unicode 路径支持。

### 对外接口（新增）

- `create_locator(...)`：推荐装配入口，支持 `config_file` / `profile` / `params` / `**overrides`。
- `locate(frame, ...)`：无状态一次性调用，返回 `Localization`。
- `Localization.to_dict(flat=)` / `to_row()` / `to_json()`：**结构化**（多值字段为真 list，
  供程序与 JSON 使用）与**扁平**（分号字符串，供 CSV）两种输出明确分离。
- `parameters.py` 参数层：`parameter_catalog()` / `catalog_markdown()` / `get_param` / `set_param` /
  `apply_params` / `apply_profile` / `flatten_config` / `list_profiles` / `describe_profiles`。
  - 参数目录**由配置数据类自动派生**，共 59 个参数，附单位、建议范围与「何时调」说明；
    元数据里写错的参数名会在导入时直接报错，杜绝文档漂移。
  - 支持点号路径、叶子名简写、嵌套字典三种写法；类型自动宽松转换；超范围直接抛异常。
- `docgen.py`：`python -m aruco_locator.docgen -o docs/参数参考.md` 自动生成参数手册。
- 预设档位：`default` / `fast` / `accurate` / `robust` / `small_markers` / `oblique_small` /
  `video` / `no_parallax`。
- `python -m aruco_locator.cli`：离线批处理（JSONL + CSV + 标注图 + 叠加视频）。

### 文档与示例（新增）

- `docs/API.md`：接口文档（坐标约定、集成方式、字段契约、异常约定、性能、线程安全、兼容性策略）。
- `docs/参数参考.md`：自动生成的 59 参数手册。
- `examples/quickstart.py`：可运行示例（单图 / 档位 / 常驻实例 + 滤波 / 参数目录 / 配置导出）。

### 实拍验证中修复的问题

- **位采样过粗导致解码失败**：`perspectiveRemovePixelPerCell` 4 → 8、
  `IgnoredMarginPerCell` 0.13 → 0.25，找回小、斜、模糊的角标记且不引入误检。
- **强透视下环序变蝶形**（严重）：旧逻辑用「离原点最近的两点 = 邻居」推环序，
  强透视下近边被拉长可超过对角线，判据反转导致自交，目标被映射到场外。
  改为绕质心角度排序构建环序。
- **单应方向传反**：由单应分解相机位姿时需要「平面 → 图像」，管线里拿到的是反方向，
  导致解出的相机高度荒谬（0.32 vs 真值 1.95）。已在 `parallax.py` 内代为求逆。
- **诊断字段不一致**：视差修正成功路径缺少 `height_diff_mm` / `k_minus_1_pct`（只写在告警路径），
  按同一套字段读取会 KeyError。已统一成功/失败路径的字段集。

### 验证

- 合成自检（2D，150 场景）：位置均值 **0.489 mm** / 最大 1.195 mm；角度均值 **0.247°**。
- 合成 3D 差高自检：同高对照组修正量**恰为 0.000 mm**（空操作护栏）；
  实测高度（角标记 50 mm / 目标 23 mm）下不修正 6.567 mm → 修正后 4.040 mm。
- 畸变校正路线：校正后 0.503 mm vs 不校正 8.98 mm。
- 实拍（4080×2296 迷宫照片）：`rms_field` 0.0019，`scale_cv` 0.029，结果与画面一致。

### 已知限制（v1.0 未解决，属物理或固有几何问题）

- 相机绕光轴滚动角接近 ±45° 时存在**固有轴歧义**（`x/y` 可能互换），以 `ambiguous_axis` 标注。
- 标记与深色结构**二值粘连**时无法提出候选四边形，属物理摆放问题，软件无法稳健救回。
- 场地上存在重复纹理时，放宽检测参数会产生伪 ID；调参需同时监控伪检数量。
- `camera_height_mm` 与天底点必须按**实际比赛机位**实测，手持照片只能用于验证机制。

### 兼容性说明

- 初始版本，无历史兼容负担。
- `Localization.to_dict()` 的默认输出由旧版的「分号字符串」改为**结构化 list**；
  需要旧格式请用 `to_dict(flat=True)` 或 `to_row()`（CLI 的 CSV 输出不受影响）。

---

[1.0.0]: #100--2026-09-25
