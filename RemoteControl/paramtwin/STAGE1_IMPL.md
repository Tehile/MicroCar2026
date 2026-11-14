# 阶段一实施记录（stage1）

> 对应方案 `STAGE1_DESIGN.md`。本文记录**实际做出来的东西**、跑出来的数字、以及每一条验收线的结果。
> 代码在 `stage1/`，测试在 `tests/`，产物在 `out/`（可随时重生成，不入库）。
> 全程未修改 `code_competition/`、`host/`、`tools/`。

---

## 1 做了什么

一条单向链路，四步，全部只读：

```
tools/params/params.competition.v3.json ─┐
                                         ├─► IR ─► 特征 ─► 三档模型 ─► 单文件 HTML
code_competition/**/*.c（查表 + 窗口）───┘
```

| 层 | 模块 | 职责 | 关键约束 |
|---|---|---|---|
| IR | `ir/params.py` | 72 参数 → `(name, phase) → value` 赋值图；active/runtime/scopes/risks | 运行时值优先于 init；13 套赋值的参数不塌缩成单值 |
| IR | `ir/tables.py` | 43 张 `const short[]` + 21 个可用动作 → 播放窗口、积分角、`b_est`、越界/空表/重复告警 | 注释与 `#ifdef` 感知（同名表有 6 份历史副本，只有 1 份生效） |
| IR | `ir/preproc.py` | 逐行状态机：块注释 / 行注释 / `#if 0` / `#ifdef` 分支 | 不追求通用，只处理本工程实际出现的形态 |
| IR | `ir/units.py` | 单位归一与"未知单位"标注 | 51 个参数无单位线索，显式标 `unknown` 而不是猜 |
| 特征 | `features/{speed,turn,ctrl,health,struct}.py` | 6 族 51 项，每项带 `source` + `method ∈ {exact, derived, assumed}` | 没有出处的数不许进特征 |
| 模型 | `model/playback.py` | **M0**：表播放，零假设 | 转角、耗时、XY 轨迹直接从表积分 |
| 模型 | `model/profile.py` | **M1**：设定剖面，精确复刻 `utility.c:1383-1388`（含阶跃与钳位） | `ds` 可调，默认 0.25 mm |
| 模型 | `model/dynamics.py` | **M2**：1 ms 闭环 + 整数截断 + PWM 限幅 + 一阶电机 | 只有 `τ_m`、`headroom` 是假设值，且必带 `assumed` 标签 |
| 报告 | `report/` | 单文件 HTML，Canvas 自绘，无第三方库、无网络 | 8 张图 / 6 个视图 / 滑块重算走 JS |
| 对拍 | `tests/` | 28 个测试 + JS↔Python 双路对拍 + 无头 DOM 冒烟 + 黄金快照 | 前端与 Python 共用同一公式与同一网格 |

---

## 2 怎么跑

```bash
cd paramtwin

python -m stage1.cli all            # extract → features → sim → report，一条命令
python -m stage1.cli extract        # 只跑 IR
python -m stage1.cli report --grade LOW_90 --dis 254

python -m unittest discover -s tests -v      # 28 tests
node tests/dom_smoke.cjs out/report.html     # 页面能否渲染（不需要浏览器）
python tests/gen_golden.py --diff            # 与黄金快照比，非零退出即有漂移
```

产物：

| 文件 | 内容 | 大小 |
|---|---|---|
| `out/paramir.json` | 参数赋值图 + 结构统计 | 60 KB |
| `out/tableir.json` | 43 张表元信息 + 21 动作窗口 + 告警 | 21 KB |
| `out/features.json` | 6 族 51 项特征 + checks + warnings | 30 KB |
| `out/traces/sweep_LOW.json` | 8 个目标速度 × 3 种格长 = 24 行 | 9 KB |
| `out/traces/m2_LOW.json` | M2 标量 | <1 KB |
| `out/traces/playback.json` | 4 个动作的 M0 标量 | <1 KB |
| `out/report.html` | 完整报告，21 个动作全部内嵌 | **174 KB** |

默认全部写到 `out/`；没有任何 `--commit` 开关，因此**不可能**改动参数表。

---

## 3 验收结果

方案里定的七个验收点，全部通过（`python -m unittest discover -s tests`，28 tests / 2.1 s）。

| 编号 | 判据 | 结果 |
|---|---|---|
| **V1** 表积分 | `LT90SEARCH` 播到 `tick=189`、积分 90.0°；`RT90SEARCH` `tick=193`、92.0°；掉头 186.0° | ✅ 完全一致的三个数 |
| **V2** 单位链 | `b_est = ∫|v_out−v_in|dt / θ` 落在物理轮距区间 | ✅ 均值 **7.465 cm**，跨动作跨度 0.819 cm（16 个 switch 动作） |
| **V3** 剖面形状 | 存在阶跃；`s*` 位置 | ✅ `s*=81.82 mm`、`v_jump=59.09 cm/s`、`T_cell=111.48 ms` |
| **V4** 网格收敛 | `ds` 0.25 → 0.05 mm，`T` 变化 < 0.3 ms | ✅ 差异 0.02 ms |
| **V5** 越界唯一 | 只有 `RV135L` 越界 | ✅ `tick_max=296 > n-1=293`，越界 3 项 |
| **V6** 前端对拍 | 滑块重算 = Python | ✅ 36 组（3 档 × 4 格长 × 4 速度）M1 全等；**并扩展到 8 组 M2**，`T` 整数相等、`v_peak`/`v_avg` 到 1e-2 |
| **V7** 黄金快照 | 23 动作 + 51 特征 + 9 组剖面 + 2 组 M2 不漂移 | ✅ `gen_golden.py --diff` 无输出 |

V6 的 M2 对拍是这轮补充的：M2 里有 `(long)(setvl*SpeedRatio)` 截断、`errs` 饱和、`pwm` 限幅三处**顺序敏感**的操作，
只要 JS 与 Python 有一处理解不同，`T` 就会差几毫秒。现在两边整数相等。

---

## 4 报告页面

`out/report.html`，单文件、离线、深色自适应。顶部三个全局控制：`v_set`（60–400 cm/s）、`grade`（LOW/LOW_90/BACK）、`cell`（90–360 mm）。
键盘 `1`–`6` 切视图。

| 视图 | 图 | 回答的问题 |
|---|---|---|
| 1 overview | 8 张 KPI 卡 + 告警列表 + 表结构事实 | 这套参数"一眼看上去"是什么状态 |
| 2 speed sweep | `T_cell` vs `v_set`（3 条格长曲线 + 当前点标记）+ 数据表 | 加速还值不值？在哪一点饱和 |
| 3 straight profile | `v(s)` / `v(t)`（M1 与 M2 叠加）/ `pwm` 与 `v_act` / 剖面事实 | 剖面为什么不是梯形；改动被什么吃掉 |
| 4 turn replay | `θ(t)`、`ω(t)`、XY 轨迹 + 11 项事实，**21 个动作全可选** | 每个跳表动作转了多少、多久、轨迹多长 |
| 5 table health | 各动作 `Δθ` 柱图 + 播放窗口表 + 重复表清单 | 哪张表有缺陷 |
| 6 parameter map | 72 行：active / runtime / scope 数 / 风险 | 这个参数到底哪一套值在生效 |

`v_set` 滑块是**真的重算**：`assets/model.js` 里是 `profile.py` 的逐行移植（同公式、同网格、同累加顺序），
前端不读预先算好的曲线，所以拖动时 M1/M2/扫描标记一起变。这就是 V6 存在的理由。

`τ_m` 是 profile 视图里的局部滑块（4–40 ms），只影响 M2 的虚线，不影响 M0/M1——用来看"如果电机更慢，剖面还成不成立"。

---

## 5 三档模型的假设边界

| 级别 | 输入 | 假设 | 承诺 |
|---|---|---|---|
| **M0** 表播放 | 查表本身 | 理想跟踪：实际角 = 参考角；无滑移 | 转角、耗时、轨迹形状 —— 纯数据，不掺假设 |
| **M1** 设定剖面 | 参数表 | 无执行器动力学，速度瞬时跟随 | 时间、`v_jump`、钳位占比 —— 精确复刻 C 代码 |
| **M2** 闭环 | M1 + PID 常数 + `SpeedRatioL` | 一阶电机（`τ_m`）、电流余量 `headroom` | **只承诺趋势**：慢多少、哪段饱和 |

M2 的价值不是更准，而是解释"为什么改了参数没反应"：`gdvl = (long)(setvl·0.56169)` 意味着 `setvl` 变化 1 cm/s 只让 `gdvl` 变 0.56，
再经 `kpl=3` 只有 1.7 个 PWM 计数；`pwm` 限幅 350/1500 = 23% 占空比。这两个数一起构成**量化死区**，
在真车上就是"我调了但车没变"。

---

## 6 速度可变的实测结论

`anchor` 策略（只改 `TopSpeed`，其余不动），格长 180 mm，LOW 档：

| v_set | T_cell (ms) | v_avg | v_peak | jump | 钳位段占比 |
|---|---|---|---|---|---|
| 60 | 300.00 | 60.0 | 60.0 | 0 | 100% |
| 80 | 225.00 | 80.0 | 80.0 | 0 | 100% |
| 100 | 180.16 | 99.9 | 100.0 | 0 | 96.7% |
| 140 | 136.78 | 131.6 | 140.0 | 0 | 69.3% |
| 180 | 120.66 | 149.2 | 180.0 | 0 | 41.8% |
| **240** | **111.48** | 161.5 | 240.0 | 59.1 | 31.4% |
| 320 | 106.64 | 168.8 | 320.0 | 139.1 | 18.8% |
| 400 | 105.18 | 171.1 | 400.0 | 219.1 | 6.1% |

三件事只能在仿真里看到：

1. **低端是纯平台**。`v_set ≤ 80` 时整段剖面被钳到目标值，`T = dis/v` 精确成立，`v_set=60` 给 300.00 ms——因为没有一段低于目标。
   注意 `BottomSpeed=95` 只在"剖面被整体压缩"时表现为地板；`anchor` 策略不动它，所以低端反而是**目标值说了算**。
2. **240 以上基本无收益**。400 cm/s 只比 240 快 6 个百分点（111.5 → 105.2 ms），因为加速段斜率 `1.5·ratio` 与顶速无关，瓶颈在爬升。
3. **阶跃从 240 才开始出现**。`v_jump` 在 `v_set ≤ 180` 时为 0，说明此时钳位把两段都压到目标值；一旦顶速高于"减速段起点要求"，切换点就暴露不连续。

第 3 点修正了方案里的表述：阶跃不是无条件存在，而是**顶速高于减速段起点时才显现**。

---

## 7 抽取层发现的缺陷（已量化，未改代码）

| 现象 | 证据 | 影响 |
|---|---|---|
| `RV135L` 越界读 Flash | `length = sizeof+8 = 596 → tick 296`，表只有 294 项 | 每圈右 135° 多读 3 个样本，角度可能跳变 |
| 2 张 spurt 表为空 | `gyro_l_spurt/gyro_r_spurt` 全 0 | `LT90SPURT` 立即返回，真正转向靠后续 `SMARTSTRAIGHT` 大差速 |
| 4 对逐字节相同的表 | `*_search` ≡ `*_t90_low`（v/vi/gyro_l/gyro_r） | 改一处必须手工同步两处，否则行为分叉 |
| 左右 90° 差 2° | 189 vs 193 tick 的窗口差写死在 `sizeof-8` | 右转系统性过转 2°，长直道才能吸收 |
| 掉头过转 6° | 186.0° 名义 180° | 由掉头后那对 `POINTX90` 吃掉 |
| 51 个参数无单位 | v3 表未标注 | 特征里显式标 `unknown`，不做数值推断 |

这些不是"仿真误差"，是**代码事实**，报告里作为告警直接呈现（overview 与 table health 两个视图）。

---

## 8 局限

- **不含滑移、噪声、电池压降、轮径磨耗**。M0/M1 是理想模型，M2 只有一阶滞后；任何绝对成绩都不该相信，只有相对排序可信。
- **不含迷宫决策**。`stratagy.c` 的搜索策略不在范围内，回放只到"单个动作"粒度。
- **`τ_m` 未标定**。默认 10 ms 来自经验，`dynamics.step_response()` 已留好台架标定入口，但没有实机数据就不该引用绝对时间。
- **表头"勿改动"**：表不参与重建，只做一致性校验。
- **`paramir` 的 `active` 是启发式**：优先取运行时相位，但 `LTopSpeed` 这类有 13 套赋值的参数，最终值取决于当次运行走了哪条分支；报告里同时给出 `runtime` 与 `scopes`，不假装只有一个答案。

---

## 9 下一步（阶段一收尾后的自然延伸）

1. 用真机 `host/sessions/*/stream_unified.csv` 标定 `τ_m` 与 `headroom`，把 M2 从"趋势"升级到"量级"。
2. 把 `SmartStraight` 的 `speed_ss` 逐毫秒规划也纳入 M1（目前 `v_ref` 用解析式，等价，但少了 `TopSpeed` 二次钳位的路径）。
3. 多动作串联仿真（转 → 直 → 转），验证 2° 过转与 6° 掉头超转在真实格口序列上的累积。
4. 阶段二的 DOE / 灵敏度，直接吃本阶段的 `features.json`。
