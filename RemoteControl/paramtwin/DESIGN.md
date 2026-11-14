# paramtwin —— 参数表运行时刻仿真与结果建模（设计方案 v0.1）

> 状态：**设计稿，未实现代码**。本轮只交付方案，不修改 `code_competition/`、`host/`、`tools/` 任何文件。
> 目标读者：本项目后续维护者、赛前整定人员。
> 相关既有文档：`docs/转向查表算法.md`（表机制）、`docs/算法详解.md`（控制链路）、`tools/params/params.competition.v3.json`（参数表真源）。

---

## 0. 这个工程要解决什么

现有参数链是**单向**的：

```
固件源码 --extract3--> params.competition.v3.json --render3--> 参数表文档
                      ^
                      └──── apply3（人工改值写回源码）
```

能回答"参数现在是多少"，不能回答两个真问题：

1. **"改成这个值，车会怎样？"** —— 需要运行时刻仿真：把参数表喂进一个 1 ms 步进的固件孪生体 + 物理对象模型，跑出速度/角度/时序/能耗曲线。
2. **"我想要这个表现，参数该填多少？"** —— 需要结果建模 + 反问题求解：先建 "参数 → 行为特征" 的代理模型，再在模型上反解。

`paramtwin` 就是补这两个方向，并且**只产出 JSON 建议值**，绝不直接改固件。

```
                 ┌──────────── 正问题（仿真） ────────────┐
 参数表 JSON ──▶ ParamIR ──▶ FirmwareTwin × Plant ──▶ Trace ──▶ FeatureVector
      ▲                                                              │
      │                                                              ▼
      └──── Proposal JSON ◀── Inverse ◀── SurrogateModel ◀── DOE/拟合 ┘
                 └──────────── 反问题（重建） ────────────┘
```

一句话定位：**不是"再写一个仿真器"，而是给参数表装一台可复现的行为试验台 + 一台可反解的建模机。**

---

## 1. 现状考察：七条决定设计的事实

这一节不是背景介绍，而是"如果不知道就会做错"的硬事实。每条后面直接给出它约束了设计中的哪一部分。

### 1.1 参数表不是字典，是"赋值事件序列"

`params.competition.v3.json`：`schema_version=3`、**72 个参数 / 7 组 / 49 个 phase / 40 条 derived / 40 条 dropped**。
但参数条目的主体是 `sets[]`，每个 set 是"**在某个 phase（源码位置）对某个变量赋一次值**"：

```json
{"name":"speed_ss", "sets":[
  {"phase":"init",     "kind":"decl",   "value":40,  "file":"HARDWARE/TIMER/timer.c"},
  {"phase":"search",   "kind":"assign", "value":80,  "file":"USER/user.c",        "fn":"parameter_search"},
  {"phase":"spurt",    "kind":"assign", "value":100, "file":"USER/user.c",        "fn":"parameter_spurt"},
  {"phase":"s90#4",    "kind":"assign", "value":10,  "file":"UTILITY/utility.c",  "context":"if(speed_ss<10)"},
  {"phase":"wall45#1", "kind":"assign", "value":100, "file":"UTILITY/utility.c",  "fn":"beforemouseturn"},
  ...
  {"phase":"mouse",    "kind":"assign", "value":70,  "file":"UTILITY/utility.c",  "fn":"mouseturn"}
], "default_phase":"...", "default_value":...}
```

分布很不均匀：**43 个参数只有 1 套，10 个有 3 套，6 个有 8 套，`speed_ss` 有 13 套**。

更关键的是 `speed_ss` 在冲刺路径里根本不是常量：

```c
// utility.c · taskstraight90()
if(track < BottomSpeed/(SpurtAccRatio+0.2))            speed_ss=(SpurtAccRatio+0.2)*track;
else if(track < ...)                                   speed_ss=SpurtAccRatio*(track-...)+BottomSpeed;
else                                                   speed_ss=SpurtDecRatio*(dis-track)+BottomSpeed;
if(speed_ss<10) speed_ss=10;
if(speed_ss>TopSpeed) speed_ss=TopSpeed;
```

⇒ **它是梯形速度规划的输出，每毫秒重算**。`LTopSpeed / LBottomSpeed / SpurtAccRatio45 / SpurtDecRatio45` 不是"速度"，而是"这条梯形的四个形状参数"。

**约束 D1**：`ParamIR` 必须建模成 `(name, phase) → value` 的**偏序赋值图**，并能回答"在时刻 t / 在任务 X 中，这个变量的值是多少、由谁写入、能否恢复"。任何"读一遍 JSON 拿 default_value 当运行值"的做法，都会在 `wall45`、`s90`、`spurt` 三条路径上算错 2~3 倍。

### 1.2 一个参数有三套口径，必须分开输出

| 口径 | 来源 | 例子 |
|---|---|---|
| 编译期默认 | `kind=decl` 的 `first` 项 | `turn_kp=4`（`timer.c`） |
| 运行时初值 | `parameter_search/parameter_spurt` 等 phase 赋值 | `speed_ss=80`（`user.c`） |
| **路径内覆写（不可恢复）** | `spurt()` / `beforemouseturn()` / `ObjectGoTo` 分支 | `turn_kp/turn_kd` 按档改成 4/2、8/5、10/7，**永不还原默认的 4/4**；`SPURT90IR_kp1` 在 `ObjectGoTo` 分支被改 0.5 后不还原 |

第三条是调参最大的陷阱：竞速段跑过一次之后，`turn_kp` 就已经不是 4 了。`docs/参数名称-功能-调整表.md` 里已列了覆写警示，但那是散文，仿真器需要把它变成**机器可判定的副作用表**。

**约束 D2**：`FeatureVector` 每个特征都带 `phase` 与 `param_origin`（default / init / override），否则"改了参数没效果"和"改了参数被覆写"无法区分。

### 1.3 第二类数据资产：查表（turn tables）

参数表之外还有 4 组硬编码的 1 ms 轨迹表（详见 `docs/转向查表算法.md`）：

| 组 | 表长（项=ms） | 播放窗口 | 备注 |
|---|---|---|---|
| 探索 90° `*_search` | 195 | 189 / 193 | 与 `*_t90_low` **逐元素相同**（两份副本） |
| 掉头 180° `*_around` | 628 | 200 + P 收口 | |
| 定点 90° `*_point` | 232 | 30 + P 收口 | |
| 竞速 45/90/135/180 `*_low` | 152 / 171 / 187 / 195 / 294 / 302 / 391 | 手工裁剪 −8/−4/−16/−14/+8 | `RV135L` 判据 296 > 表长 294，**越界读 2 项** |

表长 = 播放毫秒数，裁剪量写死在 `taskturn()` 的 `case` 里（不在参数表里）。改表长会**静默**改变转角——没有编译期断言。

**约束 D3**：`TableIR` 是独立于 `ParamIR` 的一等公民，要导入、校验（长度/窗口/越界/表首是否等于 `setangle` 目标/表尾是否归零/两份副本是否同步），并作为仿真的输入。表**不参与重建**（表头明示"赛场整定数据，勿改动"），只参与仿真与告警。

### 1.4 控制内核是"1 kHz 单中断 + 前台阻塞 while + 共享 tick"

```
SysTick(1ms):  传感器 → gangle/track/speed → switch(task) 算 setvl/setvr → 轮速 PI → PWM
前台(阻塞):    while(track<dis) task=GOSTRAIGHT;   // 只是反复写 task，真正执行在 ISR
```

`tick` 是**全局单例**，被 `TURN / KEEPWALLDISTANCE / BEFORE180TURN / taskstop` 共用。
`taskstop()` 用 `tick=0` 重置，`taskturn()` 也重置；任何"进表前忘写 `tick=0`"的路径都会**穿透等待**（`test.c` 有 3 处为此手工补写）。

远程档靠**篡改判据**解阻塞：`task=0; step=1; tick=60000; track=0x7FFFFFFF;`（`timer.c` `#ifdef MOUSE_REMOTE` 段）。急停 `isemergency=1` 只写 `task=NULL` 不写 `tick` ⇒ `tick` 停止自增 ⇒ 前台永久挂死。

**约束 D4**：仿真器的前台必须是**每毫秒让出一次的协程**，且 `tick` 必须是全局共享状态。若把 `while` 翻译成"循环直到条件满足"，就会同时丢掉：`turn_delay` 的过冲语义、`tick` 穿透缺陷、abort 的可退出性——而这三样恰好是赛前最常踩的坑。

### 1.5 数值类型本身就是行为的一部分

固件是 `int / long / double / short` 混算，且**截断点固定**：

```c
gdvl = (long)((double)setvl * SpeedRatioL);   // 向零截断，cm/s → pulse/ms
errl = (int)gdvl - gdspeedl;                  // 整型误差
errls += errl;  if(errls>1200) errls=1200;     // 积分限幅 ±1200
pwml = errl*kpl + errls*kil + (errl-errlh)*kdl;
if(pwml>350) pwml=350;                         // SPEED_HAHA=350，而 ARR=1500 ⇒ 占空比上限 23%
speedl = gdspeedl / SpeedRatioL;               // double，用于判静止
turn_gyro 是 const short ⇒ 表值被截断成整数度
```

⇒ 参数灵敏度会被**量化台阶**吃掉：`turn_kp` 从 4.00 改到 4.05，在 int 域可能完全同效。

**约束 D5**：仿真器必须逐点复刻"在哪一步转成什么类型"，并提供一个显式的 `quantize.json`（变量 → 类型 → 舍入方向）。否则"特征对参数的灵敏度"会是伪信号，反解会收敛到错误值。

### 1.6 现有 `host/host/sim/mouse_sim.py` 不能直接当运行时仿真用

它是**协议级**仿真：`speed_l += (target-speed_l)*0.22`（一阶惯性，τ≈4 ms），没有轮速 PID、没有查表、不读参数表、没有 `gangle` 积分链。它服务的是"上位机全链路 + 契约测试"，不是"参数行为"。

但它有两块可以直接复用：
- 迷宫/墙图/红外几何（`build_maze` / `_update_ir`）；
- 统一帧与命令的编解码（`_unified_frame` / `handle_line`），保证仿真结果能喂给既有分析工具（`host/analysis/metrics.py`）。

**约束 D6**：不重复造协议层与迷宫层，只新造 `Plant + FirmwareTwin + Feature/Model` 三层；输出尽量对齐统一帧字段，以便复用 `metrics.py`。

### 1.7 已有可用的实测基准

`host/sessions/*/stream_unified.csv` 是 33 通道统一帧（速度、航向、任务码、`dev_ms`、IR ADC、电池…），`host/host/analysis/metrics.py` 已实现阶跃指标与跟踪误差。

**约束 D7**：模型不许"凭感觉"。`plant.json` 必须由实测 CSV 系统辨识得到（`calibrate` 子命令），并对拍验收（同一动作的稳态速度 / 角速度平台 / 动作耗时，误差阈值可配）。

---

## 2. 总体架构

### 2.1 五层 + 两条总线

```
┌──────────────────────────────────────────────────────────────────────┐
│  L5  Inverse        target feature → 参数表提案（带约束/置信度）        │
├──────────────────────────────────────────────────────────────────────┤
│  L4  Model          代理模型 f: 参数 → 特征；灵敏度 / 可辨识性          │
├──────────────────────────────────────────────────────────────────────┤
│  L3  Feature        静态特征（从 ParamIR）+ 动态特征（从 Trace）        │
├──────────────────────────────────────────────────────────────────────┤
│  L2  Twin           FirmwareTwin（1ms 控制内核 + 任务协程）             │
│      Plant          电机/轮地/车体/电池/陀螺/红外/编码器                │
├──────────────────────────────────────────────────────────────────────┤
│  L1  IR             ParamIR · TableIR · PlantIR · Scenario             │
└──────────────────────────────────────────────────────────────────────┘
        ▲ 时间总线：唯一 1 ms 节拍（plant 可在内部子步）
        ▲ 参数总线：read(name, phase) → 值 + 来源 + 可写性 + 审计
```

**为什么是"两条总线"而不是"一个大循环"**：
- 时间总线保证"固件看到的世界"只有 1 ms 粒度——这与真机一致，也是所有时序特征的唯一时基；
- 参数总线让"这个值从哪来"成为一等查询，直接支撑 1.2 的三套口径与覆写审计，而不需要在每个公式里手写 `if phase==...`。

### 2.2 数据流（一次完整仿真）

```
params.v3.json ──extract──▶ ParamIR ─┐
user.c/utility.c 表 ──tables──▶ TableIR ├──▶ Scenario ──▶ Twin.run(max_ms) ──▶ Trace
plant.json ────────load────▶ PlantIR ─┘                                      │
                                                                             ▼
                                              Feature.extract(trace, paramIR)
                                                                             │
                                                        ┌────────────────────┴───────────┐
                                                        ▼                                ▼
                                                 features.json                    DOE 样本集 → Model
```

---

## 3. 数据契约

所有契约都是 JSON + JSON Schema，放在 `paramtwin/contracts/`。**任何跨模块传递只走契约，不允许直接传 Python 对象**（保证可序列化、可回归、可跨版本对拍）。

### 3.1 `ParamIR`

```json
{
  "schema": "paramtwin/paramir@1",
  "source": {"file":"tools/params/params.competition.v3.json","sha256":"...","profile":"competition"},
  "params": {
    "turn_kp": {
      "ctype": "double", "group": "motion", "unit": "1",
      "value_scale": 1.0,
      "sites": [
        {"phase":"init","kind":"decl","value":4.0,"file":"HARDWARE/TIMER/timer.c","line":61,
         "gate":"", "writable":true, "recoverable":true},
        {"phase":"spurt_medium","kind":"assign","value":8.0,"file":"UTILITY/utility.c","line":1174,
         "gate":"LOW_ENABLE", "recoverable":false}
      ],
      "default_phase": "init",
      "derived_from": [],
      "consumers": [{"task":"TURN","law":"turn_pd","symbol":"turn_kp"}]
    }
  },
  "phases": [{"id":"init","kind":"boot"},{"id":"spurt_medium","kind":"runtime","trigger":"spurt(MEDIUM)"}],
  "couplings": [{"lhs":"SpeedRatioL","rhs":"TrackRatioL/100","kind":"identity","enforced":false}]
}
```

要点：
- `sites` 保留原始 `file/line/sha16/gate`（**溯源到源码行**，便于 `apply3` 反向定位）；
- `recoverable=false` 直接来自 `docs/参数名称-功能-调整表.md` 的覆写警示 + 源码审计，是**机器可读的覆写表**；
- `consumers` 是"参数 → 控制律符号"的边，由 `extract` 阶段用 `timer.c`/`utility.c` 的引用点自动生成，是灵敏度分析与反解分组的基础；
- `couplings` 记录恒等式（`SpeedRatioL = TrackRatioL/100`、`SpurAccRatio→TopSpeed` 选择），重建时必须同时满足。

### 3.2 `TableIR`

```json
{
  "schema": "paramtwin/tableir@1",
  "tables": {
    "gyro_r_search": {"len":195, "unit":"deg", "dtype":"s16", "sha256":"...",
                      "head":-90, "tail":3, "monotonic":"increasing"},
    "vouter_search": {"len":195, "unit":"cm/s", "plateau":[70,109,109,70], "..." : "..."}
  },
  "windows": {"RT90SEARCH": {"table":"gyro_r_search","expr":"sizeof(x)","play_ms":193},
              "RV135L":   {"table":"gyro_rv135_low","expr":"sizeof(x)+8","play_ms":296}},
  "checks": [{"id":"RV135_OVERREAD","severity":"error","detail":"play_ms 296 > len 294"},
             {"id":"SEARCH_EQ_T90LOW","severity":"warn","detail":"两份副本逐元素相同"}]
}
```

`checks` 不是可选项——`RV135_OVERREAD` 一旦发生，仿真必须**显式建模"读到相邻常量"的行为**（用 `.map` 的邻接符号值填充），否则会假装没这回事。

### 3.3 `PlantIR`

与参数表**严格分离**。参数表是"固件以为的世界"，`PlantIR` 是"真实的世界"：

```json
{
  "schema": "paramtwin/plantir@1",
  "mechanics": {"wheel_diameter_mm":22.0, "track_width_mm":153.0, "mass_g":180, "inertia_kgm2":1.2e-6},
  "motor": {"R_ohm":5.0, "Kt":0.0105, "Ke":0.0105, "tau_e_ms":0.6, "tau_m_ms":8.0,
            "deadband_pwm":12, "efficiency":0.72, "asym": {"L":1.00, "R":0.97}},
  "tire": {"mu_static":0.9, "mu_kinetic":0.65, "slip_curve":[[0,0],[0.05,0.6],[0.1,0.95],[1,1.0]]},
  "battery": {"ocv_mv":7900, "r_int_mohm":420, "capacity_mah":300, "sag_mv_per_a":420},
  "gyro": {"sensitivity_mv_per_dps":0.67, "bias_lsb":0, "noise_lsb":1.2, "tempco":0.02},
  "ir": {"curve":{"a":4200,"b":-1.35}, "noise_mm":1.5, "saturation_mm":[0,249], "led_sag_v_per_a":0.0},
  "encoder": {"ppr":4300, "quantize":"floor"}
}
```

关键点：`TrackRatioL=55.09` 与 `plant.mechanics` 推出的**真值**（≈62.2 pulse/mm × 滑移修正）之间的比值，就是"标定误差"——这才是"改系数会不会让车走短"的可仿真来源。

### 3.4 `Scenario`

```json
{
  "schema": "paramtwin/scenario@1",
  "maze": {"n":16, "walls":"builtin:20260925", "start":[0,0,"UP"]},
  "actions": [
    {"op":"settle",  "ms":300},
    {"op":"turn",    "pattern":"RT90SEARCH", "pre":"before90turn", "last_action":"Straight"},
    {"op":"straight","mm":180},
    {"op":"turn",    "pattern":"R180L"},
    {"op":"run",     "segments":[{"op":"straight","mm":180},{"op":"turn","pattern":"LT45L"}]}
  ],
  "env": {"vbat_mv":7680, "floor":"mat_a", "temp_c":25, "disturbance":{"wall_offset_mm":0,"yaw0_deg":0.5}},
  "seed": 20261104
}
```

`actions` 是**动作级**剧本，不是"跑整个迷宫"。理由：单动作是参数可辨识性最好的切片（见 §5.2），且复现成本低。迷宫级只在 L5 的可选评估里用。

### 3.5 `Trace`

对齐统一帧字段（便于 `host/analysis/metrics.py` 直接消费）+ 仿真专有列：

| 列 | 单位 | 说明 |
|---|---|---|
| `t_ms` | ms | 全局节拍（唯一时基） |
| `task`, `step`, `tick` | – | 任务状态机内部态 |
| `setvl, setvr` | cm/s | 内环给定（**特征提取的主输入**） |
| `speedl, speedr` | cm/s | 编码器解算速度 |
| `pwml, pwmr` | – | PWM（含限幅后） |
| `gangle, setgangle` | ° | 航向与参考 |
| `xe, ye, yaw` | mm, ° | **真实位姿**（固件看不到，仅用于评估） |
| `track, absolutetrack` | mm | 里程 |
| `disl, disr, disf1, disf2` | mm | 红外 |
| `vbat_mv` | mV | 电池 |
| `flags` | – | sat / estop / slip / abort |

`Trace` 落 CSV（人看）+ Parquet/`.npz`（算）。每份 trace 附 `run.json`：`git_rev / paramir_sha / plant_sha / scenario_sha / twin_version / seed`，保证"同一份输入必然同一份输出"。

### 3.6 `FeatureVector`

见 §5，独立成契约 `feature.schema.json`，是**正向输出与反向输入的公共货币**。

### 3.7 `Proposal`

```json
{
  "schema": "paramtwin/proposal@1",
  "base": {"file":"params.competition.v3.json","sha256":"..."},
  "target": {"v_straight":110,"omega_turn":0.52,"t_action":185,"overshoot_pct":8},
  "changes": [{"name":"kpl","from":13,"to":14,"site":"init","reason":"velocity loop bandwidth",
               "confidence":0.82,"identifiable":true}],
  "locked": ["TrackRatioL","SpeedRatioL"],
  "residual": {"v_straight":1.8,"omega_turn":-0.7,"t_action":2.1,"overshoot_pct":-11},
  "pareto": [{"id":"A","time_s":12.4,"err_mm":3.1},{"id":"B","time_s":11.9,"err_mm":5.6}],
  "warnings": ["overshoot 与 turn_kp/turn_kd 强耦合，二者不可独立辨识"],
  "apply_hint": "python tools/paramgen.py apply3 --name kpl --value 14  # 需人工审核后 --commit"
}
```

`Proposal` **不写源码**，只描述意图 + 给出 CLI 提示。这是红线（§10）。

---

## 4. 仿真内核

### 4.1 时间与调度

- 主节拍固定 **1 ms**，与 `SysTick` 同频；`t` 从 0 单调递增，不回绕（`link_time_ms` 另算）。
- `Plant` 内部可在 1 ms 内做 **N 子步**（默认 4×0.25 ms）以稳定积分电机电流环；子步对外不可见。
- 调度顺序**必须**与固件一致：

```
1. 传感采样（陀螺 → gangle；编码器 → track/speed；红外 → dis*；电池 → batt）
2. MOUSE_REMOTE abort 注入（若启用）
3. 阈值判定（lefthaswall / critical / forwardhaswall）
4. switch(task) 算 setvl/setvr
5. isemergency 判据
6. 轮速 PI → PWM → Plant 推进
7. 前台协程让出一次（执行一条语句，可能改 task/tick）
8. 观测量记录
```

顺序错了会改变"这一毫秒用新任务还是旧任务"的语义，直接导致 `turn_delay` 类参数偏移 1~2 mm。

### 4.2 参数总线

```python
class ParamBus:
    def read(self, name, *, task=None, phase=None) -> float
    def write(self, name, value, *, site, recoverable) -> None
    def origin(self, name) -> "init|runtime|override"
    def audit(self) -> list[WriteEvent]     # (t_ms, name, old, new, site)
```

规则：
- `read` 返回值的同时在 `trace` 里打点（只在"值发生变化"时写一行 `param_event`，避免 trace 爆炸）；
- `write` 的 `recoverable=False` 记入 `run.json.overwrites`，用于"这次仿真里哪些参数被踩过"的可解释性；
- `ParamIR` 里 `writable=false` 的项（如 `const u8 trackcoor`）拒绝 `write`，直接抛错——这是防止仿真器"顺手改了不该改的东西"。

### 4.3 控制内核

按 `timer.c` 的 `switch(task)` 复刻，但**用声明式方式挂在 law 注册表**上，而不是逐行移植：

```python
@law("GOSTRAIGHT")
def gostraight(p, s, t):
    e = s.gangle
    return p.speed_ss - e * p.GOSTRAIGHT_kp, p.speed_ss + e * p.GOSTRAIGHT_kp

@law("TURN")
def turn(p, s, t):
    ref = s.table_gyro[t.tick]
    e   = s.gangle - ref
    de  = e - s.e_prev
    vl  = s.table_vl[t.tick] - e*p.turn_kp - de*p.turn_kd
    vr  = s.table_vr[t.tick] + e*p.turn_kp + de*p.turn_kd
    t.tick += 1                      # 注意：TURN 先取值后自增
    return vl, vr
```

每个 `law` 必须附 **golden vector**：`(输入状态 → setvl/setvr)` 的手算样本，测的是"公式没被改坏"，不是"结果好看"。这是与 C 代码保持一致的唯一低成本手段（见 §9 V1）。

必须复刻的 6 个细节（都是真实缺陷/陷阱）：
1. `TURN` 先取值后自增，`TURNAROUND/POINTTURN90` 先自增后取值；
2. `TURNAROUND` 只播 200 ms、`POINTTURN90` 只播 30 ms，之后纯 P（2.5 / 1.0）收口，退出判据是**角速度**不是角度；
3. `taskstop()` 的 `settrack(-decdistance)` 负起点 + `stoptick>30` 兜底；
4. `BEFORE90TURN` 有前墙用 `disf2-disf1`、无前墙用 `gangle`；
5. `SMARTSTRAIGHTFAST` 的 `|e|>10` 分支切换 kp（0.5 / 0.35）；
6. `SPURT90IR` 的 `|e|>65 → e=0` 死区（**这是防打墙保护，不是笔误**）。

### 4.4 任务协程层

前台 `utility.c` 的阻塞函数，一对一映射成生成器：

```python
def taskturn(self, pattern):
    self.setangle(TABLE_SPEC[pattern].sign * TABLE_SPEC[pattern].deg)
    self.tick = 0; self.step = 0; self.e = self.eh = self.esum = 0
    self.task = Task.TURN
    yield from self.wait_while(lambda: self.tick < TABLE_SPEC[pattern].play_ms)
    self.settrack(0)
```

`wait_while` 每毫秒检查一次并返回控制权给主循环：

```python
def wait_while(self, cond):
    while cond():
        if self.abort and self.remote:      # 复刻 timer.c 的判据篡改
            self.task = 0; self.step = 1; self.tick = 60000
            self.track = 0x7FFFFFFF
        yield
```

这样三件事自动成立：
- `tick` 的共享语义（全局变量，任何任务写它都影响别人）；
- abort 的"粘滞退出"（每毫秒被重写 60000）；
- 急停挂死（`isemergency` 只写 `task=NULL`，不写 `tick` ⇒ 生成器永不返回）——这**是特性不是 bug**，必须能复现，否则无法解释真机"停了但卡住"。

协程调度器是**单线程**的：一个 `while t < max_ms: step_all() ; coro.send(None)`。不要用线程（会引入真实并发，与 1 ms 单中断语义不符）。

### 4.5 数值纪律

`quantize.json` 逐变量声明：

| 变量 | 固件类型 | 舍入 |
|---|---|---|
| `setvl/setvr` | `long`（32 位；`timer.c` 定义 `long`，其余文件 `extern int`） | 每毫秒赋值即截断 |
| `gdvl` | `long` | `int()` 截断 |
| `errl/errls` | `int` | 截断 + 限幅 1200 |
| `pwml` | `int` | 截断 + 限幅 350 |
| `track/absolutetrack` | `long` | 截断 |
| `gangle` | `int`（`ganglet/gangleratio`） | C 整数除（向零） |
| `turn_gyro[t]` | `short` | 表值本身是整数 |
| `speedl` | `double` | 全精度 |

实现上统一走一个 `firmware_cast(value, kind)` 原语，**禁止**在 law 里裸用 `int()`。这样"改 kp 0.05 没效果"能被复现出来，并在特征里显式标注 `quantized_flat=true`。

> 跨平台陷阱：`setvl/setvr` 在 `timer.c` 里定义成 `long`，在 `utility.c/stratagy.c/test.c` 里 `extern int`。
> ARM 上两者都是 32 位，所以固件无歧义；但若仿真内核改用 C 在 x86_64 上编译，`long` 是 64 位，
> 截断行为会变。**Python 实现按 32 位有符号处理，不跟随平台**。

### 4.6 Plant 方程

电机（单轮，含电压降与反电动势）：

```
V_app = Vbat * (pwm/1500) * sign            # 最大占空比 23%（SPEED_HAHA/ARR）
i     = (V_app - Ke*ω) / R                  # 一阶电气，τ_e
T     = Kt * i * η(sign) - T_friction(ω) - T_load
J dω/dt = T                                 # τ_m 由 J/(Kt*Ke/R) 决定
```

轮地滑移（简化为稳态 slip 曲线 + 一阶松弛）：

```
s_slip = clamp((F_demand / (μ*N)) , 0, 1)      # 需求摩擦 / 可用摩擦
v_wheel_eff = v_wheel * (1 - slip_gain(s_slip))
```

车体差速运动学：

```
v   = (vL + vR)/2
ω_yaw = (vR - vL)/track_width
x += v*cos(yaw), y += v*sin(yaw), yaw += ω_yaw
```

电池：

```
V_terminal = OCV(SOC) - i_total * R_int        # 启动瞬间的 sag 会拉低红外发射管电流 ⇒ 墙检测失效
SOC -= ∫i dt / capacity
```

红外：

```
d_true → ADC = a*d^b + noise → 一阶卡尔曼（Qf/Pf 参数）→ 查表 clamp[0,249] → dis*
```

陀螺：

```
ADC = bias + ω*0.4155 + noise  →  gdiff=(ADC-gyro0)/2  →  ganglet += (gdiff+gdiffh)/2  →  gangle = -ganglet/gangleratio
```

**注意**：`gangle` 不是"真实航向"。它是"固件以为的航向"。真实航向 `yaw` 由 plant 积分。二者的差就是**角度估计误差**——这本身是一个有价值的特征（`yaw_est_err`），也是"gangleratio 调错会怎样"的答案。

---

## 5. 特征体系

特征体系是正问题与反问题的**公共货币**。设计原则：每个特征必须同时满足 ①可从 trace 算出 ②有物理含义 ③标注了对哪些参数敏感 ④标注是否可解析反解。

### 5.1 五族特征

**F1 运动学（稳态，可解析反解）**

| id | 定义 | 单位 | 主要敏感参数 |
|---|---|---|---|
| `v_ss` | 直行段末 20% 的 `mean(speedl,speedr)` | cm/s | `speed_ss`（或 `LTopSpeed`）、`SpeedRatio*`、`kpl/kpr` |
| `omega_turn` | 转向中段（20%~80%）`d(gangle)/dt` 中位数 | °/ms | 表 `vouter/vinner`、`track_width` |
| `v_peak` | 段内最大速度 | cm/s | `LTopSpeed`、`accdistance` |
| `a_acc` | 加速段线性拟合斜率 | cm/s² | `SpurtAccRatio`、`accdistance` |
| `R_turn` | `v_ss / omega_turn` | mm | 表差速 |

**F2 动态（二阶等效，用于 PID 反解）**

对每个"给定阶跃"（`setvl` 从 A 跳到 B）拟合：

```
G(s) = K / (τ²s² + 2ζτs + 1)
```

输出 `K, τ, ζ, overshoot_pct, rise_ms, settle_ms, sse`。
直接复用 `host/analysis/metrics.py` 的 `step_metrics`，保持与既有分析口径一致。

**F3 标定一致性（无量纲比值，乘性可反解）**

| id | 定义 | 用途 |
|---|---|---|
| `eps_d` | `(track - 真实里程)/真实里程` | 校准 `TrackRatioL/R` |
| `eps_theta` | `(gangle - yaw)/90` 在转向段 | 校准 `gangleratio` |
| `eps_enc_theta` | `(absoluteangle - yaw)` | 校准 `angleratio` |
| `slip_ratio` | `1 - v_wheel_eff/v_wheel` | 判断地面/加速是否过猛 |
| `batt_sag_mv` | 起步 20 ms 内 `vbat` 最大跌落 | 判断电池内阻是否已恶化 |

**F4 时序/安全（离散，规则反解）**

| id | 定义 | 用途 |
|---|---|---|
| `t_action` | 单动作 `ms` | 校 `turn_delay`、`accdistance`、`decdistance` |
| `tick_trace` | `tick` 的转折点序列 | 复现穿透/挂死 |
| `pwm_sat_pct` | `|pwm|>=350` 占比 | 判断增益过大/电压不足 |
| `estop_count` | `isemergency` 触发次数 | 硬约束（必须为 0） |
| `abort_exitable` | abort 注入后 `wait_while` 是否在 2 ms 内返回 | 远程档安全网 |
| `table_overread` | 是否读到表外索引 | 硬约束 |

**F5 迷宫级（可选，P2）**

`cell_time_ms` / `junction_time_ms` / `total_time_s` / `path_mm` / `wall_hit_count`。

### 5.2 静态特征提取（直接从参数表算，不需要仿真）

这是"对已有参数表的特征提取"的第一层，秒级完成，用于**筛表**：

| id | 定义 | 判据 |
|---|---|---|
| `speed_ladder` | 各 phase 下 `speed_ss/speed_fastss/speed_turn` 的取值序列 | 是否单调、是否有 phase 掉档 |
| `gain_margin` | 轮速环开环增益 `kpl + kil*N + kdl*N` 的量级估计 | 与推荐区间比 |
| `override_risk` | `recoverable=false` 的 set 数 / 该参数被消费的 task 数 | >0 则标红 |
| `param_coupling` | `couplings` 中未强制执行的恒等式（如 `SpeedRatioL ≠ TrackRatioL/100`） | 偏差 >2% 报警 |
| `unit_anomaly` | 值域 vs `unit` 的合理性（如 `turn_delay` 落在 0~40 mm） | 越界报警 |
| `table_table_sync` | `*_search` vs `*_t90_low` 是否逐元素相同 | 不同步报警 |
| `window_len_mismatch` | `play_ms` vs `len` | 越界/截断报警 |
| `identifiability` | 由 `consumers` 图算的"参数→特征"雅可比秩 | 秩亏则标注不可辨识 |

**为什么这层必须先做**：72 个参数里，一次整定真正能动的通常不超过 8 个。静态特征把"哪些能动、哪些一动就打架"先筛出来，动态仿真只跑有意义的子空间，DOE 的维度从 72 降到 5~10。

### 5.3 动态特征提取

```python
def extract(trace, param_ir, spec) -> FeatureVector:
    # 1. 按 task 切段（复用 host/analysis/metrics.segment_by_task 的思路）
    # 2. 段内算 F1/F2
    # 3. 用 trace 里的"真实位姿"算 F3
    # 4. 用状态机事件算 F4
    # 5. 打上 phase / param_origin / quantized_flat 标签
```

每个特征附 `{value, unit, ci, n_samples, valid, note}`。`ci` 由多次扰动（不同 seed / wall_offset）得到，没有 `ci` 的特征不允许进入反问题。

### 5.4 特征选择（决定反问题可解性）

反问题维度必须 ≤ 可辨识维数。做法：
1. 由 `consumers` 图得到候选参数集 P；
2. 对 P 做一次 DOE + 代理模型，算 Sobol 总效应；
3. 只保留 `S_T > 0.05` 且与目标特征单调相关的参数；
4. 对剩余参数算雅可比 `∂f/∂p` 的条件数；条件数 > 1e3 时**合并**成组合参数（如 `turn_kp/turn_kd` 合并为"等效阻尼比"）。

---

## 6. 结果建模

### 6.1 正问题流水线

```
doe.json ──┬─▶ Twin.run × N ──▶ traces ──▶ features ──▶ dataset.parquet
           │                                                  │
           └──────────── (并行，每样本独立 seed) ──────────────┘
                                                              ▼
                                                   fit → model.pkl + report.md
```

单次仿真预算：1 个动作 ≈ 0.3~1 s 仿真时间 ≈ 10~30 ms 墙钟（纯 Python，numpy 向量化 plant 子步）。**1000 样本 ≈ 30 s**，可接受。

### 6.2 DOE

- **筛选阶段**：Plackett-Burman 或 拉丁超立方（LHS）20 点 × 10 参数，只为排敏感度；
- **建模阶段**：LHS 200~500 点（含中心点重复 5 次估纯误差）；
- **验证阶段**：留出 20% + 边界点（参数上下界）单独跑；
- 离散参数（`u8/u16`、`turn_delay` 整数）用**分层抽样**，不用连续 LHS 后取整（会破坏空间填充性）。

### 6.3 代理模型选型

| 模型 | 何时用 | 输出 |
|---|---|---|
| 线性 / 对数线性 | 首轮、要解释 | 弹性系数 `∂ln y/∂ln x`，直接当"灵敏度" |
| 二次响应面（含交互） | 参数 3~8 个、有交互 | 显著项 + 等值线 |
| 高斯过程（GPR） | 样本 < 200、要不确定性 | `mean ± 2σ`，用于贝叶斯优化 |
| 梯度提升树 | 样本 > 500、强非线性 | 特征重要度（仅参考，不外推） |

选型不靠拍脑袋：用 **留一交叉验证的 RMSE / R²** 自动选，`R² < 0.85` 时**拒绝**进入反问题（并报"当前特征不可建模"）。这条比"选一个最先进的模型"重要得多。

### 6.4 灵敏度

- 全局：Sobol 一阶 `S_i` + 总效应 `S_Ti`（用 Saltelli 采样，样本 = N*(2d+2)）；
- 局部：在工作点求雅可比 `J = ∂f/∂p`，输出"每 +1 单位参数 → 特征变化"的表格；
- 交互：二阶 `S_ij`（只对 `S_Ti - S_i` 大的参数对算）。

输出 `sensitivity.md`，形式：

```
v_ss        ← speed_ss (S_T=0.71) , SpeedRatioL (0.14) , kpl (0.06)
omega_turn  ← vouter/vinner 表 (0.63) , track_width (0.21) , turn_kp (0.05)
t_action    ← play_ms (0.88) , turn_delay (0.07)
```

这张表**就是调参顺序**：先动 `S_T` 大的，别在噪声里调 `S_T<0.05` 的。

---

## 7. 参数表重建（反问题）

### 7.1 形式化

给定目标特征 `y*`（含容差 `tol`）、锁定集 `L`、可行域 `Ω`，求

```
min_p  Σ_i w_i * ((f_i(p) - y*_i) / tol_i)²  +  λ * Σ_j ((p_j - p_j^0)/σ_j)²
s.t.   p ∈ Ω,  p_L = p_L^0,  c(p) = 0（恒等式约束）,  p 的整型/量纲约束
```

第二项是**正则项**：偏好"离原表近的解"。没有它，反问题会给出数学上最优但现场不敢用的参数。

### 7.2 三阶段求解（先解析，再优化，最后全仿真精修）

**P1 解析反解（拿到可行起点）**

标定类与运动学类是**乘性**关系，可直接反解：

```
TrackRatioL_new = TrackRatioL_old * (track_measured / track_true)      # 与 api/calc.py 同一公式
gangleratio_new = gangleratio_old * (gangle_measured / yaw_true)
speed_ss_new    = speed_ss_old  * (v*_new / v_ss_old)                  # 一阶近似
```

这一步 0 次仿真，命中率已能到 70~85%。

**P2 代理模型上的约束优化（快）**

- 连续参数：CMA-ES（对病态、强耦合稳健）或 SLSQP（有等式约束时）；
- 离散参数：坐标下降 + 邻域枚举（`turn_delay` 只可能在 ±3 mm 内动）；
- 多目标：NSGA-II，输出 Pareto 前沿（时间 / 精度 / 稳定裕度）。

**P3 全仿真精修（准）**

取 P2 的 top-3 解，各跑 20 次全仿真（含随机扰动），按 `mean + 2σ` 选最优。这一步防"代理模型过拟合导致的假最优"。

### 7.3 约束

1. **值域**：来自 `ctype`（u8 0~255、s16 ±32767）+ 业务合理区间（`turn_delay ∈ [0,40]` mm、`kpl ∈ [1,30]`）。越界直接剪枝。
2. **恒等式**：`SpeedRatioL = TrackRatioL/100` 等由 `couplings` 给出；违反则**投影回可行面**而不是简单拒绝。
3. **不可写项**：`writable=false`（如 `const u8 trackcoor`）永远不出现在 `changes` 里。
4. **覆写项**：`recoverable=false` 的参数若被改，必须在 `warnings` 里写明"该值在 spurt 路径会被覆写为 X"。
5. **数量上限**：一次建议的改动 ≤ 6 项。超过则拒绝，要求分轮（现场可执行性 > 数学最优）。

### 7.4 三种重建模式

| 模式 | 输入 | 用途 |
|---|---|---|
| M1 全量重建 | 只给目标特征 | 从零拟一套表（换车/换场地后） |
| M2 局部重建 | 目标特征 + 锁定集 | 只调 3~6 个（赛前微调，最常用） |
| M3 迁移重建 | 原表 + 新环境（电压/地面/温度） | "同一套参数换个电池还能不能跑" |

M3 是"运行时刻"三个字最直接的体现：给定 `vbat=7300 mV`、地面 `μ` 下降 15%，反解出"要不要降 `LTopSpeed`、降多少"。这一问用真机试错成本极高，仿真刚好补位。

### 7.5 可辨识性诚实

反问题最危险的不是算不出，而是**算出一个看起来精确的错解**。所以 `Proposal` 必须带：

- 每个改动的 `identifiable: true/false`；
- 参数相关性矩阵（>0.9 的标"不可独立确定"）；
- 若目标特征数 < 待求参数数，直接拒绝并提示"需要补充目标特征 X/Y"。

---

## 8. 目录与 CLI

### 8.1 目录蓝图

```
paramtwin/
  README.md                  # 定位、蓝图、边界、里程碑（本文件的简版）
  DESIGN.md                  # 本方案
  contracts/                 # JSON Schema（paramir / tableir / plantir / scenario / trace / feature / proposal）
  paramtwin/
    ir/         extract.py  tables.py  plant.py
    plant/      motor.py  tire.py  battery.py  sensors.py  chassis.py
    twin/       kernel.py  laws.py  tasks.py  scheduler.py  cast.py
    feature/    static.py  dynamic.py  schema.py
    model/      doe.py  fit.py  sensitivity.py
    inverse/    analytic.py  optimize.py  propose.py
    cli.py
  data/
    tables.json              # 由 extract 生成（含 sha256）
    plant.json               # 由 calibrate 生成
    quantize.json
  tests/       golden/  regression/
  out/         runs/  models/  reports/       # gitignore
```

### 8.2 CLI

```bash
# 0. 一次性：抽取参数表 + 查表 + 静态特征
python -m paramtwin extract  --src code_competition --params tools/params/params.competition.v3.json
python -m paramtwin tables   --check                 # 长度/窗口/越界/副本同步
python -m paramtwin static   --out out/reports/table_health.md

# 1. 标定 plant（用实测 CSV）
python -m paramtwin calibrate --csv host/sessions/2026-10-27_0844/stream_unified.csv --out data/plant.json

# 2. 单次仿真
python -m paramtwin sim --scenario scenarios/turn90_right.json --trace out/runs/t1.csv
python -m paramtwin feature --trace out/runs/t1.csv --out out/runs/t1.feat.json

# 3. 建模
python -m paramtwin doe  --params 6 --n 300 --out out/doe.json
python -m paramtwin fit  --doe out/doe.json --model gpr --out out/models/m1.pkl
python -m paramtwin sensitivity --model out/models/m1.pkl --out out/reports/sens.md

# 4. 重建
python -m paramtwin synth --target targets/target_a.json --mode M2 \
       --lock TrackRatioL,SpeedRatioL,angleratio --out out/proposals/p1.json
python -m paramtwin verify --proposal out/proposals/p1.json --n 20
```

约定：
- 所有子命令默认 **dry-run**（只打印，不写 `out/` 以外的地方）；
- 任何写 `params.*.json` 的行为都要 `--commit`，且写的是 `params.proposed.v3.json`，不是原表；
- `--seed` 固定，默认取 `paramir_sha[:8]` 派生，保证"同输入同输出"。

### 8.3 运行环境与依赖

- **解释器**：`C:/ProgramData/anaconda3/python.exe`（3.13.5）或 conda `general_env`（3.12.13）。
  workbuddy 托管的 python **没有 numpy**，直接跑会崩（本项目已踩过：`import v210_launch_pull_test`）。
- **硬依赖**：`numpy`（trace 向量化、DOE）。核心仿真只依赖标准库 + numpy。
- **软依赖**：`scipy`（SLSQP/插值）、`scikit-learn`（GPR/树，可选）、`pandas`（只用于报告）。
- **降级策略**：缺 `scikit-learn` 时，代理模型退化为"线性 + 二次响应面 + 自实现 GPR"（约 200 行），
  主流程不阻塞；缺 `scipy` 时用坐标下降 + 网格搜索替代 SLSQP。
- **不引入** matplotlib 硬依赖：报告出 Markdown 表格；需要图时由 `host/ui` 或 HTML 报告承担。

---

## 9. 验证与验收

| # | 项 | 方法 | 判据 |
|---|---|---|---|
| V1 | 控制律一致性 | 每个 `law` 的 golden vector（手算 8~15 组输入） | 逐点严格相等（含截断） |
| V2 | 表完整性 | `tables --check` | 长度/窗口/越界/副本同步 4 项全过；`RV135_OVERREAD` 必须**显式暴露**而非静默 |
| V3 | 实测对拍 | 用 `sessions/*/stream_unified.csv` 的同一动作段 | `v_ss` 误差 ≤ 8%，`omega_turn` ≤ 10%，`t_action` ≤ 10 ms |
| V4 | 灵敏度符号 | 单调性断言（加大 `speed_ss` ⇒ `v_ss` 上升；加大 `turn_kd` ⇒ 超调下降） | 100% 通过 |
| V5 | 反解闭环 | `synth` → `sim` → 重算特征 | 残差 ≤ 目标容差；不收敛时必须报"不可解"而非给近似解 |
| V6 | 可复现 | 同 seed 跑两次 | trace 的 sha256 完全一致 |
| V7 | 红线 | `git status --porcelain` | 只允许 `paramtwin/` 下新增；`code_competition/**` 与 `tools/**` 零改动 |

V3 是唯一"必须有真机数据"的一项；其余全部离线可跑。V3 不达标时，**允许**只交付"相对比较"能力（A 参数 vs B 参数谁更快），但必须在报告里降级标注。

---

## 10. 边界与风险

**不做的事**

1. 不改 `code_competition/` 任何文件（含 hex、含 Keil 工程）。参数只以 JSON 提案形式存在。
2. 不仿真 `stratagy.c` 的搜索决策（右手法则/回溯），只仿真"运动执行层"。迷宫级评估复用 `mouse_sim` 的墙图。
3. 不重建查表（表是赛场整定数据，改动风险远大于收益）；只做一致性告警。
4. 不做 GUI。报告出 Markdown + 可选 HTML（复用 `host/ui` 风格）。
5. 不追求"绝对成绩预测"。对外只承诺**相对排序**与**趋势**。

**主要风险**

| 风险 | 影响 | 缓解 |
|---|---|---|
| Plant 参数不可辨识（真机数据不足） | 绝对误差大 | 只输出相对结论；`calibrate` 报告置信区间 |
| 逐行复刻 C 造成"双份真源"漂移 | 仿真与固件不一致 | golden vector + 每次 `extract3` 后跑 V1；`sha16` 变化即告警 |
| 参数耦合导致反解不唯一 | 给出假精确解 | 相关矩阵 + `identifiable` 标记 + 特征数校验 |
| 离散/量化让梯度失效 | 优化不动 | `quantize.json` + 离散参数专门处理（坐标下降/枚举） |
| 仿真快感导致跳过真机验证 | 现场翻车 | V3 是发布门槛；`Proposal` 强制带 `apply_hint` 与人工审核 |

**里程碑**

| 阶段 | 交付 | 判据 |
|---|---|---|
| P0 | `extract/tables` + 单动作仿真骨架（TURN/GOSTRAIGHT） | 能跑出 `RT90SEARCH` 的 trace，`t_action≈193 ms` |
| P1 | 静态特征 + 动态特征 + DOE + 线性模型 | `sensitivity.md` 能指出 top-3 参数 |
| P2 | 反解（M2/M3）+ 实测标定 | V3/V5 通过 |
| P3 | 迷宫级 + Pareto + 报告 | 一次完整"给目标→出提案→验证"闭环 |

---

## 11. 一页速览

```
输入：一份参数表（72 参数 / 49 phase） + 一张查表（6 组 1 ms 轨迹） + 一个物理对象
过程：1 ms 步进的固件孪生（含 tick 共享、阻塞协程、类型截断） × 物理模型
输出：轨迹 → 五族特征（运动学/动态/标定/时序/迷宫） → 代理模型 → 参数提案
价值：把"改参数靠试"变成"改参数先算"，并且算出来的东西能被 trace 和实测双重证伪
```

三条不可动摇的原则：

1. **参数是事件，不是常量**（`(name, phase) → value`，覆写不可恢复）。
2. **表是数据资产，不是代码**（导入、校验、参与仿真，但不重建）。
3. **只产提案，不写固件**（红线靠 `git status` 守，不靠自觉）。
