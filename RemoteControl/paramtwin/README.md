# paramtwin —— 参数表运行时刻仿真与结果建模

> **阶段一已实施完成**（`stage1/`，28 个测试全绿）。产物：`out/report.html`（单文件，离线可开）。
> 实施记录见 [`STAGE1_IMPL.md`](STAGE1_IMPL.md)；方案见 [`STAGE1_DESIGN.md`](STAGE1_DESIGN.md)。

## 阶段导航

| 阶段 | 文档 | 内容 | 状态 |
|---|---|---|---|
| **一** | [`STAGE1_DESIGN.md`](STAGE1_DESIGN.md) → [`STAGE1_IMPL.md`](STAGE1_IMPL.md) | 参数表特征提取 + 理想建模（M0/M1/M2）+ 速度可变扫描 + 单文件 HTML 可视化 | **已实现，28 tests 全绿** |
| 二/三 | [`DESIGN.md`](DESIGN.md) | DOE、代理模型、灵敏度、参数表反解与重建 | 远期蓝图，未实施 |

> 阶段一砍掉了反解与真机闭环，只保留「读表 → 算特征 → 理想前向 → 画出来」这条单向链。
> 两文的术语（`ParamIR` / `TableIR` / `Trace` / 特征族）保持一致，阶段一可直接复用阶段二的契约。

## 三分钟上手

```bash
cd paramtwin
python -m stage1.cli all                    # 生成 out/paramir|tableir|features|traces|report.html
python -m unittest discover -s tests -v     # 28 tests：V1–V7 + 对拍 + DOM 冒烟
```

## 一句话

给 `tools/params/params.competition.v3.json` 装两台机器：

- **试验台**：把参数表 + 查表喂进 1 ms 步进的固件孪生体与物理对象模型，跑出轨迹、时序、误差、能耗；
- **建模机**：建 "参数 → 行为特征" 的代理模型，并反解 "目标特征 → 参数表提案"。

## 为什么放在这里

现有参数链是单向的（源码 → JSON → 文档 → 人工改值），只能回答"参数现在是多少"，不能回答：

- 改成这个值，车会怎样？
- 我想要这个表现，参数该填多少？
- 同一套参数换个电池（7.68 V → 7.30 V）还能不能跑？

这三个问题的试错成本在真机上极高，而它们的可仿真度也很高——因为控制逻辑全在 `timer.c` 的 1 kHz 中断里，是确定性的。

## 三条硬结论（决定了整个设计）

1. **参数是事件，不是常量。**
   `v3` 表里 72 个参数声明了 49 个 phase（其中 35 个被实际引用），`speed_ss` 有 13 套赋值，且在冲刺路径里是每毫秒重算的规划输出；
   `turn_kp/turn_kd`、`SPURT90IR_*` 会被冲刺路径覆写且**不恢复**。
   ⇒ 必须按 `(name, phase) → value` 建模，并区分「编译期默认 / 运行时初值 / 路径内覆写」三套口径。

2. **查表是独立的数据资产，而且左右不对称。**
   43 张 `const short[]`、21 个可用动作，播放窗口手工裁剪（`sizeof-8` / `-4` / `-16` / `-14` / `+8`），`RV135L` 判据 296 > 表长 294（越界读 3 项）。
   按窗口积分出来的角度是 **LT 90.0° / RT 92.0° / 180° 掉头 186.0°** —— 左右差 2° 是表里写死的，不是噪声。
   ⇒ 必须导入、校验、参与仿真；但不参与重建（表头明示"勿改动"）。

3. **速度不是参数，是规划器的输出；而且剖面不是梯形。**
   `utility.c:1383-1388` 是「双斜率折线 + 钳位」：切换点硬编码在 `s* = 0.4545·dis`，两侧斜率分别为
   `1.5·ratio` 与 `5·dec`，因此切换点存在**设定值阶跃**（LOW 档 `dis=180` 时 181 → 240 cm/s，钳位后仍跳 59）。
   `speed_ss` 两段都以 `+BottomSpeed` 收尾 ⇒ **速度下限被 `BottomSpeed=95` 锁死**，提速设 50 也不会更慢。
   ⇒ 特征必须区分 `v_set` / `v_peak` / `v_avg`，模型不能把顶速当常量。

4. **时序耦合必须靠协程复现，不能靠"循环到条件满足"。**
   `tick` 是全局单例，被 TURN / KEEPWALLDISTANCE / BEFORE180TURN / taskstop 共用；
   远程 abort 靠每毫秒重写 `tick=60000` 解阻塞；急停只写 `task=NULL` 导致前台永久挂死。
   ⇒ 前台阻塞循环必须逐毫秒让出，否则 `turn_delay` 类参数会算错，缺陷也无法复现。

## 目录

```
paramtwin/
  README.md  STAGE1_DESIGN.md  STAGE1_IMPL.md  DESIGN.md
  stage1/
    ir/          params.py tables.py preproc.py units.py     ParamIR / TableIR
    features/    speed turn ctrl health struct               F-A..F-F，51 项
    model/       playback(M0) profile(M1) dynamics(M2)
    report/      build.py + template.html + assets/{model,plot,app}.js
    cli.py
  tests/         test_stage1.py(28) parity.cjs dom_smoke.cjs gen_golden.py golden/
  out/           paramir.json tableir.json features.json traces/ report.html
```

阶段二/三扩展（暂不建）：`plant/`、`twin/`、`model/`（DOE/代理）、`inverse/`。

## 与现有工程的关系

| 现有资产 | 关系 |
|---|---|
| `tools/params/params.competition.v3.json` | **输入真源**，只读；`extract` 转成 `ParamIR` |
| `tools/paramgen.py apply3` | **出口**：`Proposal` 由人审核后走它落盘 |
| `code_competition/HARDWARE/TIMER/timer.c` | **被仿真的对象**，只读；控制律以它为准 |
| `code_competition/UTILITY/utility.c` | 任务状态机源头，只读 |
| `host/host/sim/mouse_sim.py` | **复用**迷宫/墙图/帧编解码；它的物理模型太简（一阶惯性，无 PID/查表），不能当运行时仿真 |
| `host/analysis/metrics.py` | **复用**阶跃指标，保证分析口径一致 |
| `host/sessions/*/stream_unified.csv` | **标定与对拍**的真机数据 |
| `docs/转向查表算法.md` | 表机制与缺陷清单，`TableIR` 校验项的来源 |

## 边界

- 不修改 `code_competition/`、`host/`、`tools/` 任何文件；红线用 `git status` 守。
- 不重建查表，只做一致性告警。
- 不仿真迷宫搜索决策（`stratagy.c`），只仿真运动执行层。
- 不承诺绝对成绩，只承诺相对排序与趋势。
- 一切输出只进 `out/`；本阶段没有任何写参数表的代码路径。

## 下一步

阶段一已闭环（见 `STAGE1_IMPL.md` §3 的 V1–V7）。后续按 `STAGE1_IMPL.md` §9：
用真机 `stream_unified.csv` 标定 `τ_m` / `headroom` → 把 `speed_ss` 逐毫秒规划并入 M1 → 多动作串联，验证 2°/6° 过转在格口序列上的累积。
