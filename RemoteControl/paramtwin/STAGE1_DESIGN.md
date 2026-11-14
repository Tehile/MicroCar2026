# paramtwin 阶段一：参数表特征提取与理想建模（设计 v1.0）

> **本轮只出方案，不改动任何代码。**
> 阶段一只回答三个问题：**这张表想跑多快、实际能跑多快、转弯会转多少度**，并把答案画出来。
> 反解 / 参数重建 / DOE / 代理模型属于阶段二，见 `DESIGN.md`，本阶段不实施。

---

## 0. 范围

**做：**

1. 从 `tools/params/params.competition.v3.json` 与源码中的 6 组查表，抽出 `ParamIR` / `TableIR`；
2. 由 IR 计算 6 族特征（F-A ~ F-F），全部带单位、公式与 `file:line` 出处；
3. 理想模型 M0 / M1 / M2，输入「参数表 + 情景」，输出 trace；
4. **速度作为一等情景变量**，支持任意速度点与速度扫描；
5. 单文件 HTML 报告：离线可用、可交互、可导出。

**不做：** 反解与参数重建、真机闭环、滑移与噪声、参数写回、固件改动。

---

## 1. 考察结论：决定设计的 6 条事实

### 1.1 参数表是「赋值事件图」，不是字典

`params.competition.v3.json`：72 参数 / 49 phase / 40 条 `derived` / 40 条 `dropped`。
`speed_ss` 有 13 处赋值，同一参数存在 `init` / `spurt.*` / `menu` 三套口径。

⇒ 特征提取必须以 **phase 为上下文**，不能取 `default_value` 当运行值。

### 1.2 速度不是参数，是规划器的输出

`utility.c:1383-1388`（`taskstraight90` 主循环）：

```c
if(track < dis*(0.5/(0.6+0.5)))          // 切换点 s* = 0.4545·dis，硬编码
    speed_ss = SpurtAccRatio*track*1.5 + BottomSpeed;
else
    speed_ss = SpurtDecRatio*(dis-track)*5 + BottomSpeed;
if(speed_ss > TopSpeed) speed_ss = TopSpeed;
```

三条推论，全部与直觉相反：

| 事实 | 数值（LOW 档，`dis=180`，v3 表） |
|---|---|
| 加速段斜率 | `1.5×0.7 = 1.05` (cm/s)/mm ≈ **10.5 m/s²** |
| 减速段斜率 | `5×0.7 = 3.5` (cm/s)/mm ≈ **35 m/s²** |
| 切换点 | `s* = 81.8 mm` |
| 切换点两侧设定值 | 加速末 **181 cm/s** → 减速初 **439 cm/s**（被 `TopSpeed` 钳到 240） |
| 结论 | **不是梯形，是「双斜率折线 + 钳位」，且切换点不保证连续** |

所以「`LTopSpeed=240` 是速度」这句话只在钳位段成立；加速段实际只爬到 181。任何把它当常量的模型都会低估时间 20% 以上。

### 1.3 速度下限被 `BottomSpeed` 锁死

`speed_ss` 的两段都以 `+ BottomSpeed` 收尾，而 `BottomSpeed=95`。
⇒ **目标速度设成 50 也不会更慢**，除非同时改 `BottomSpeed`。这是速度扫描曲线会出现「低速平台」的根因，也是后续参数重建必须处理的耦合。

### 1.4 查表是独立数据资产，且左右不对称

6 组 1 ms 轨迹，播放窗口由 `taskturn()` 手工裁剪（`length=sizeof(tbl)-N`，`tick_max = length/2-2`）：

| 动作 | 表 | 项数 | 播放到 tick | 积分角 `θ_tab` | 名义角 | 偏差 |
|---|---|---|---|---|---|---|
| `LT90SEARCH` | `gyro_l_search` | 195 | 189 | **90.0°** | 90 | 0.0 |
| `RT90SEARCH` | `gyro_r_search` | 195 | 193 | **92.0°** | 90 | **+2.0** |
| `LT90L` | `gyro_lt90_low` | 341 | 331 | 90.0° | 90 | 0.0 |
| `RT90L` | `gyro_rt90_low` | 341 | 337 | 92.0° | 90 | **+2.0** |
| `L180L` | `gyro_l180_low` | 391 | 389 | **186.0°** | 180 | **+6.0** |

左右窗口差 4 ms（`LT` 减 8 字节、`RT` 不减），是**写死的**，不是噪声。任何「对称转向」的假设都会丢 2°。

### 1.5 单位是混的，但可以闭式自洽

`dis`/`track` 用 **mm**，`setvl`/`speed_ss`/`LTopSpeed` 用 **cm/s**（`timer.c:1159` 注释），中间靠 `SpeedRatioL=0.56169` 换算。

用「差速积分 = 角度」反推等效轮距 `b_est = ∫|v_out-v_in|dt / θ_tab`：

| 动作 | ∫Δv dt | θ | `b_est` |
|---|---|---|---|
| `LT90SEARCH` | 12.1 cm | 90.0° | **7.68 cm** |
| `L180L` | 24.2 cm | 186.0° | **7.46 cm** |

两个独立动作一致到 3%，且落在电脑鼠真实轮距量级 ⇒ **单位链成立**。
这条等式将作为 V2 验收：`b_est` 偏离 [7.2, 8.0] cm 说明单位或表被改坏了。

### 1.6 现有 `host/host/sim/mouse_sim.py` 不能复用

它是协议级仿真（`speed += (target-speed)*0.22`），无 PID、无查表、不读参数表。
可复用的只有迷宫/墙图/帧编解码，与本阶段无关。

---

## 2. 架构：三层 + 单向数据流

```
          ┌──────────────┐   ┌──────────────┐
sources → │ ParamIR      │   │ TableIR      │      ← L1 抽取（只读源码/JSON）
          │ phase 赋值图  │   │ 6 组表+窗口  │
          └──────┬───────┘   └──────┬───────┘
                 └───────┬──────────┘
                         ↓
          ┌──────────────────────────────┐
          │ FeatureEngine  F-A … F-F      │      ← L2 特征（静态，不需要仿真）
          └──────────┬───────────────────┘
                     ↓  Scenario（v_set / dis / 动作 / 档位）
          ┌──────────────────────────────┐
          │ IdealModel  M0 → M1 → M2      │      ← L3 理想前向
          └──────────┬───────────────────┘
                     ↓ Trace + DynFeature
          ┌──────────────────────────────┐
          │ ReportBuilder → report.html   │      ← L4 可视化（单文件）
          └──────────────────────────────┘
```

**四条取舍：**

1. **唯一事实源是 Python。** HTML 里的 JS 只做「解析式重算」（M0/M1），M2 不下放。所有数字由 Python 生成并内嵌。
2. **特征必须带 provenance。** 每个特征字段附 `source`（`file:line` 或 `table:name`）与 `method`（`exact` / `derived` / `assumed`）。`assumed` 的特征在 UI 上打灰标，不允许进结论区。
3. **模型分级承诺。** M0/M1 是「精确复刻代码」，可作回归基线；M2 含待标定常数，只作趋势参考。
4. **渲染与计算分离。** HTML 不 import 任何库、不联网；数据以 `<script type="application/json">` 内嵌。

---

## 3. 数据契约

五份 JSON，全部写入 `out/`，均带 `schema_version` 与 `paramir_sha`。

### 3.1 `paramir.json`

```json
{
  "schema_version": "1.0",
  "profile": "competition",
  "source": {"path": "tools/params/params.competition.v3.json", "sha256": "...", "generated": "..."},
  "phases": [{"id": "init", "kind": "boot"},
             {"id": "spurt.low", "kind": "runtime", "trigger": "spurt(LOW)", "recoverable": false}],
  "params": [{
    "name": "LTopSpeed", "group": "speed_profile", "unit": "cm/s", "ctype": "int",
    "scopes": {"init": 140, "main#3": 240, "spurt.low": 240},
    "active": {"phase": "main#3", "value": 240, "source": "USER/test.c:1262"},
    "derived": [{"expr": "LTopSpeedh", "phase": "main#2"}],
    "risks": ["multi-scope", "runtime-overwritten"]
  }]
}
```

### 3.2 `tableir.json`

```json
{
  "tables": [{
    "name": "gyro_l_search", "file": "USER/user.c", "line": 54,
    "n": 195, "dtype": "s16", "unit": "deg",
    "sha256_16": "...", "first": 90, "last": -3, "min": -3, "max": 90,
    "monotonic": "decreasing", "max_step": 2, "plateau": 0
  }],
  "actions": [{
    "id": "LT90SEARCH", "nominal_deg": 90,
    "gyro": "gyro_l_search", "v_outer": "vouter_search", "v_inner": "vinner_search",
    "window_expr": "sizeof(gyro_l_search)-8", "tick_max": 189,
    "theta_tab": 90.0, "theta_nom": 90, "delta_theta": 0.0,
    "b_est_cm": 7.68, "overrun_elems": 0
  }]
}
```

`window_expr` 与 `tick_max` 都要存：表达式是源码事实，`tick_max` 是求值结果，二者不一致时报警。

### 3.3 `scenario.json`

```json
{
  "id": "sweep_v_LOW_dis180",
  "mode": "straight",
  "grade": "LOW",
  "speed_policy": {"kind": "anchor", "v_set": [60, 80, 100, 140, 180, 240, 320, 400]},
  "geometry": {"dis_mm": 180, "cell_mm": 180},
  "model": {"level": "M2", "tau_m_ms": 10.0, "dt_ms": 1},
  "run": {"repeats": 1, "seed": 0}
}
```

### 3.4 `trace.json`（每情景一份）

```json
{
  "scenario_id": "...", "model": "M2", "dt_ms": 1,
  "channels": {
    "t_ms": [0, 1, 2], "s_mm": [0, 0.15, 0.31],
    "v_set_cms": [95, 96.05, 97.1], "v_act_cms": [0, 3.2, 9.1],
    "pwm_l": [0, 12, 30], "theta_deg": [90, 89.9, 89.8]
  },
  "scalars": {"T_total_ms": 111.5, "v_peak_cms": 240, "v_avg_cms": 161.4,
              "v_jump_cms": 58.7, "overshoot_pct": 3.1}
}
```

### 3.5 `features.json`

```json
{
  "features": {
    "F-A.v_set":        {"value": 240, "unit": "cm/s", "method": "exact",  "source": "USER/test.c:1262"},
    "F-A.v_peak_kin":   {"value": 181, "unit": "cm/s", "method": "exact",  "source": "utility.c:1383"},
    "F-A.v_jump":       {"value": 58.7,"unit": "cm/s", "method": "exact",  "source": "utility.c:1383-1388"},
    "F-C.delta_theta":  {"value": 2.0, "unit": "deg",  "method": "exact",  "source": "table:gyro_r_search"}
  },
  "warnings": [{"code": "TABLE_OVERRUN", "action": "RV135L", "detail": "tick_max 296 > n 294"}]
}
```

---

## 4. 特征体系（6 族）

全部为**静态可算**（不依赖仿真），仿真只用来补 F-B 的动力学项。

### F-A 速度能力

| 特征 | 定义 | 值示例 |
|---|---|---|
| `v_set` | 当前 phase 的 `TopSpeed` | 240 cm/s |
| `v_floor` | `BottomSpeed`，速度下限 | 95 cm/s |
| `a_acc` | `1.5·SpurtAccRatio` × 10 | 10.5 m/s² |
| `a_dec` | `5·SpurtDecRatio` × 10 | 35 m/s² |
| `s_switch` | `dis·0.5/1.1` | 81.8 mm |
| `v_acc_end` | `a_acc·s_switch/10 + v_floor` | 181 cm/s |
| `v_jump` | 切换点两侧设定值之差（钳位后） | 58.7 cm/s |
| `clamp_ratio` | 减速段被 `TopSpeed` 钳位的行程占比 | 32% |
| `v_peak_kin` | `min(v_set, max(v_acc_end, 减速段初值))` | 240 cm/s |

**`v_jump` 是本阶段新增的核心指标**：它是「设定值阶跃」，车身在一阶惯性下追不上，落差全部变成切换点的冲击与跟踪误差——这解释了「为什么看着顶速很高，实际单格时间没那么短」。

### F-B 时间结构（M1/M2）

| 特征 | 定义 |
|---|---|
| `T_cell` | 跑完 `dis` 的总时间 |
| `T_acc / T_cru / T_dec` | 三段占比（`T_cru` 可能为 0） |
| `v_avg` | `dis / T_cell` |
| `T_cell(v_set)` | 速度扫描曲线，分段解析 |
| `v_eff_ratio` | `v_avg / v_set`，说明「标称顶速」的兑现率 |

### F-C 转向几何（纯表驱动，M0）

| 特征 | 定义 | 示例 |
|---|---|---|
| `theta_tab` | `g[0]-g[tick_max]` | RT 92.0° |
| `delta_theta` | `theta_tab - theta_nom` | RT +2.0° |
| `T_turn` | `tick_max` ms | 193 ms |
| `omega_avg` | `theta_tab/T_turn` | 477 °/s |
| `omega_max` | `max\|Δg\|/dt` | 由表差分 |
| `b_est` | `∫\|Δv\|dt / θ_tab` | 7.68 cm |
| `asym` | `Δθ_R - Δθ_L` | +2.0° |
| `R_turn` | `v_center/ω`，理想转弯半径 | 由 M0 逐点算 |

### F-D 控制强度

| 特征 | 定义 | 备注 |
|---|---|---|
| `Kp/Kd` | 当前档位的 `turn_kp`/`turn_kd` | LOW 4/2、MED 8/5、HIGH 10/7，默认 4/4 |
| `K_deg2pulse` | `Kp · SpeedRatioL` | 每度误差对应的 pulse/ms |
| `v_deadband` | `1/SpeedRatioL` | 1 LSB 对应的 cm/s，量化死区 |
| `tau_eq` | 由 M2 阶跃响应拟合 | 待标定 |

`v_deadband` 的用途：**判断「改参数有没有效果」**。若某参数 ±5% 引起的 `speed_ss` 变化小于死区，UI 直接标 `quantized_flat`。

### F-E 表健康度

| 特征 | 判据 |
|---|---|
`window_ratio` | `tick_max/n`，比值 <0.9 说明表有 10% 以上从未播放 |
`overrun` | `tick_max > n-1` → 越界读（`RV135L`: 296 vs 294） |
`head_offset` | `g[0]` 与 `±theta_nom` 的差 |
`tail_residual` | `g[tick_max]`，应为 0 |
`max_step` | 相邻项最大跳变，过大意味着可跟随性差 |
`duplicate_of` | 与其它表逐元素比对（`*_search` ≡ `*_t90_low`） |

### F-F 参数表结构

| 特征 | 判据 |
|---|---|
`n_phases` / `n_sets_per_param` | 覆写链深度 |
`dead_params` | `writable=false` 或被后续 phase 全覆盖 |
`unit_ambiguity` | 同一 phase 内 mm 与 cm/s 混用（必然命中，作为已知风险输出） |
`grade_delta` | LOW/MED/HIGH 之间被改动的参数集合 |
`unreachable_mode` | `HIGH`（`HIGH_ENABLE` 未定义）等不可达分支 |

---

## 5. 理想模型

「理想」定义为四条假设，缺一不可地写进 UI 的脚注：

1. **无滑移**：轮速 × 轮径 = 地速，`b_est` 恒定；
2. **无噪声**：陀螺、编码器读数等于真值；
3. **标定正确**：`SpeedRatio` / `angleratio` 为真值，不是估计；
4. **电池无限**：电压恒定，PWM 满量程可用。

### M0 表播放（Table Playback）

纯数据，零假设。给定动作 `a`：

```
θ(t)   = g[0] - g[t]
ω(t)   = -(g[t+1] - g[t])            [deg/ms]
v_o(t) = v_outer[t],  v_i(t) = v_inner[t]
b      = ∫|v_o-v_i|dt / θ(T)          ← 自洽校验，不是输入
x(t), y(t) = ∫ v_center·[cos θ, sin θ] dt
```

输出：角度剖面、角速度剖面、俯视轨迹、`b_est`。
**M0 不引入任何可调常数**，因此可作为 V1 回归基线。

### M1 设定剖面（Reference Profile）

复刻 `utility.c:1383-1388` 三段式，**保留阶跃，不做平滑**：

```
s* = dis · 0.5/1.1
v_ref(s) = 1.5·ratio·s + Bottom                      s ∈ [0, s*)
         = min(Top, 5·dec·(dis-s) + Bottom)          s ∈ [s*, dis]
```

解析求解得到 `T_acc/T_cru/T_dec`、`v_jump`、`clamp_ratio`，并用 1 ms 数值积分交叉验证（误差 > 0.5 ms 报警）。

### M2 理想动力学（本阶段的「主模型」）

在 M1 之上加两层，仍然无噪声：

```
setvl(k) → gdvl = (long)(setvl · SpeedRatioL)        // 截断，非四舍五入
err = gdvl - speed_pulse;  errs += clamp(err, ±1200)
pwm = clamp(Kp·err + Ki·errs + Kd·(err-err_h), ±350)  // ARR=1500 ⇒ 23.3% 上限
v_wheel += (K_m·pwm - v_wheel) · dt/τ_m               // 一阶电机
v_body   = (v_l + v_r)/2 ;  ω = (v_r - v_l)/b
```

三个常数：`τ_m`（默认 10 ms，标 `assumed`）、`K_m`、`Kp/Ki/Kd`（从 `timer.c` PID 分支取）。

M2 的用途不是「更准」，而是回答**参数改了为什么没反应**：量化、PWM 限幅、`τ_m` 三者会吃掉小改动。

### 保真度声明

| 级别 | 可承诺 | 不可承诺 |
|---|---|---|
| M0 | 转角、转向耗时、轨迹形状（纯表） | 任何速度量 |
| M1 | 设定剖面、`v_jump`、`T` 的解析值 | 实际能否跟上 |
| M2 | 趋势、量化/限幅效应 | 绝对时间（依赖 `τ_m`） |

---

## 6. 速度可变设计

### 6.1 三个「速度」必须分开命名

| 名称 | 含义 | 谁决定 |
|---|---|---|
| `v_set` | 参数表里的 `TopSpeed` | 参数 |
| `v_peak` | 剖面峰值 | `v_set` 与钳位 |
| `v_avg` | `dis / T_cell` | 前两者 + 形状 |

UI 上任何一处出现裸「速度」都算缺陷。扫描图固定画 `v_avg` 与 `v_peak` 两条，`v_set` 作横轴。

### 6.2 三种注入策略

```json
{"kind": "anchor", "v_set": 300}
```

| 策略 | 行为 | 用途 |
|---|---|---|
`anchor` | 等比缩放 `LTopSpeed / L45TopSpeed / BackTopSpeed / L90TopSpeed / speed_turn`，保持 `BottomSpeed` 与 ratio 不变 | 「同一张表，换个速度跑」 |
`shape` | 只改 `SpurtAccRatio*` / `SpurtDecRatio*`，`TopSpeed` 不变 | 「同顶速，改加减速形状」 |
`patch` | 任意参数覆盖（`[{name, value}]`） | 重建验证、手工调参 |

`anchor` 的语义边界必须写在 UI 上：**`BottomSpeed` 不缩放**，所以 `v_set < 95` 时曲线会撞到平台——这是真实现象，不是 bug。

### 6.3 扫描网格

- 主轴：`v_set ∈ {60, 80, 100, 140, 180, 240, 320, 400}`（8 点，覆盖平台区/线性区/钳位区）
- 次轴：`dis ∈ {180, 254（45°斜穿）, 360}`、档位 `{LOW, MED, HIGH}`
- 默认产出 `8 × 3 × 3 = 72` 条 trace，`1 ms` 步长、单条 < 1200 点，总计 < 1 MB

### 6.4 缩放律检验（V4）

理想模型下 `anchor` 缩放 `k` 倍且不发生钳位时，巡航段时间应严格按 `1/k` 缩放。
这是一个**不依赖实测**的自洽性检查：偏差 > 2 ms 说明参数被某个 phase 覆写了（`spurt()` 里 `LTopSpeed` 不缩放就是典型）。

---

## 7. HTML 可视化

### 7.1 形式

- **单文件** `out/report.html`，`file://` 直接打开，无 CDN、无字体、无网络请求；
- 数据内嵌 `<script id="data" type="application/json">…</script>`；
- 图表用 Canvas 2D 自绘（约 600 行 JS），不引第三方库；
- 首屏 < 1 s，交互滑块用 `requestAnimationFrame` 节流到 60 fps。

### 7.2 页面结构

```
┌───────────────────────────────────────────────────────────┐
│ paramtwin · stage1     [表: params.competition.v3.json ▾]  │
│                        速度 v_set ──●──── 240 cm/s  ▣ 联动 │
├──────────┬────────────────────────────────────────────────┤
│ ① 概览   │  KPI 卡：v_set 240 │ v_peak 240 │ T_cell 111.5  │
│ ② 速度扫描│         v_avg 161 │ v_jump 58.7 │ θ_rt +2.0°   │
│ ③ 直行剖面│  ───────────────────────────────────────────   │
│ ④ 转向回放│  [ 主图区：随 Tab 切换 ]                        │
│ ⑤ 表健康 │                                                 │
│ ⑥ 参数图 │  ───────────────────────────────────────────   │
│          │  告警条：RV135L 越界 2 项 · HIGH 档不可达        │
└──────────┴────────────────────────────────────────────────┘
```

### 7.3 六视图与九张图

| # | 视图 | 图表 | 回答什么问题 |
|---|---|---|---|
| 1 | 概览 | KPI 卡 + 告警条 | 这张表整体是什么水平、有没有硬伤 |
| 2 | 速度扫描 | ① `v_avg`–`v_set`（含平台/线性/钳位三段着色） | 提速还有没有收益、拐点在哪 |
| 2 | 速度扫描 | ② `T_cell`–`v_set` | 单格时间对速度的弹性 |
| 3 | 直行剖面 | ③ `v`–`s` 折线（标出 `s*` 与阶跃） | 阶跃有多大、钳位占多少行程 |
| 3 | 直行剖面 | ④ `v`–`t` + `a`–`t` 双轴 | M1 与 M2 的差在哪一段 |
| 4 | 转向回放 | ⑤ `θ`–`t`、`ω`–`t`（表驱动） | 转了多少度、快慢分布 |
| 4 | 转向回放 | ⑥ 俯视轨迹 + 逐帧回放（可拖时间轴） | 转弯半径、路径形状 |
| 5 | 表健康 | ⑦ 各动作 `Δθ` 条形图（左右并排） | 不对称有多严重 |
| 5 | 表健康 | ⑧ 表长/窗口/越界 热力条 | 哪张表有结构缺陷 |
| 6 | 参数图 | ⑨ phase × 参数覆写矩阵 | 谁覆盖了谁、哪些是死参数 |

### 7.4 交互约定

- 顶部一个全局 `v_set` 滑块（60–400），影响 ②③④；
- 视图 3 加 `dis` 滑块（90–360）、档位下拉（LOW/MED/HIGH）；
- 视图 4 加动作下拉（LT90 / RT90 / L180 / LT45 / LT135…）；
- 悬停任意曲线显示 `(t, v, s)` 十字线读数；
- 导出：`features.json`、当前视图 PNG、当前 trace CSV。

### 7.5 JS 与 Python 的双实现对拍

JS 只实现 M0/M1（解析式，无状态），Python 是唯一事实源。防漂移手段：

- 共享 `model_spec.json`：常量 + 方程版本号；
- 构建时跑 12 个 golden case（3 档 × 2 种 dis × 2 个边界），JS 输出与 Python 逐点比对，误差 > 1e-9 则 `--strict` 构建失败；
- HTML 内嵌 `spec_version`，与 Python 不一致时页面顶部红条提示。

---

## 8. 目录与 CLI（下一轮实施时建）

```
paramtwin/
  stage1/
    ir/        params.py  tables.py  units.py
    features/  fa_speed.py  fb_timing.py  fc_turn.py  fd_ctrl.py  fe_health.py  ff_struct.py
    model/     m0_playback.py  m1_profile.py  m2_dynamics.py  quantize.py
    report/    build.py  assets/plot.js  templates/report.html
    cli.py
  schema/      paramir.tableir.scenario.trace.features.json
  out/         paramir.json tableir.json features.json traces/ report.html
  tests/       golden/  test_theta.py  test_speed.py  test_js_parity.py
```

```bash
python -m stage1.cli extract   --params tools/params/params.competition.v3.json \
                               --src code_competition --out out/
python -m stage1.cli features  --ir out/ --out out/features.json
python -m stage1.cli sim       --ir out/ --scenario scenarios/sweep_speed.json --out out/traces/
python -m stage1.cli report    --all out/ --out out/report.html --strict
```

四条命令可合成 `python -m stage1.cli all`。所有子命令默认 dry-run，只有 `--commit` 才写 `out/` 以外。

---

## 9. 里程碑与验收

| 里程碑 | 交付 | 验收 |
|---|---|---|
M1.1 | `ParamIR` / `TableIR` + `extract` | 72 参数 / 6 组表全部解析；`sha16` 与源一致 |
M1.2 | F-A ~ F-F + `features.json` | 本文 §1.4 / §1.5 的数字能被脚本自动复现 |
M1.3 | M0/M1/M2 + 扫描 trace | `T_cell` 解析值与 1 ms 积分差 < 0.5 ms |
M1.4 | `report.html` 六视图 | 离线打开无报错、滑块联动 < 16 ms/帧 |

**V1** 表积分回归：`LT 90.0 / RT 92.0 / 180 186.0` 三项锁定，改动即失败。
**V2** 轮距自洽：`b_est ∈ [7.2, 8.0] cm`，且 LT 与 180 的差 < 5%。
**V3** 速度三段性：扫描曲线必须出现「平台（`v_set<95`）→ 线性 → 钳位」三段，缺一即模型错。
**V4** 缩放律：`anchor` 下巡航段按 `1/k` 缩放，偏差 < 2 ms。
**V5** 越界检测：`RV135L` 报 `overrun=2`，其余动作 `overrun=0`。
**V6** JS/Python 对拍：12 个 golden case 全通过。

---

## 10. 风险与开放问题

| 风险 | 影响 | 处置 |
|---|---|---|
单位歧义（mm vs cm/s 混用） | 所有速度特征整体错 10 倍 | 用 `b_est` 闭式校验兜底；若 `b_est` 不在 [7.2,8.0] 则整链重标 |
`τ_m` 无实测 | M2 绝对时间不可信 | 默认 10 ms 并标 `assumed`；UI 灰显；后续用一次定速直行标定 |
表与固件版本不绑定 | 表被改过则特征失真 | `TableIR` 存 `sha256_16`，与 `paramir_sha` 一起钉住 |
JS/Python 双实现漂移 | 页面数字与 CLI 不一致 | `model_spec.json` + golden 对拍 + `--strict` |
`spurt()` 覆写不还原 | `anchor` 缩放后行为不一致 | V4 缩放律检验会直接暴露 |

**待确认（需真机一次标定，不阻塞本阶段）：**
1. `τ_m` 与 `K_m` 的实际值；
2. 物理轮距是否等于 `b_est ≈ 7.6 cm`；
3. `LTopSpeed` 的单位按 cm/s 解释是否正确（`b_est` 支持这一解释，但缺直接证据）。

---

## 11. 一页速览

- **输入**：一份参数表 + 六组查表；**输出**：一份 `features.json`、一批 trace、一个 `report.html`。
- **核心洞察**：速度是规划器输出不是参数；剖面是「双斜率 + 钳位」且切换点有阶跃；左右转向在表里就不对称（90 vs 92）。
- **理想模型的边界**：M0/M1 可精确复刻代码，M2 含一个待标定常数，仅作趋势。
- **速度可变**：`v_set` 作为情景轴，三种注入策略，8 点扫描，三段性必须出现。
- **可视化**：单文件离线 HTML，六视图九张图，一个全局速度滑块驱动三个视图。
