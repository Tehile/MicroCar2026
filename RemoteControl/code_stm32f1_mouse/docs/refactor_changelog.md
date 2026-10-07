# 代码标准化改造记录

> 本文档汇总 `code_stm32f1_mouse` 从原始工程到标准化发布版的**改造历程、成果指标与红线遵守情况**,供汇报与复盘使用。完整施工明细见 `../施工进度记录.md`,发布前收尾优化见 `../优化进度记录.md`。
>
> **注意**:本文为改造期间的历史记录,文中出现的文件名/函数名(如 `control_isr.c`、`stmflash`、`UTILITY/`、`SYSTEM/` 等)是**改造过程各阶段的命名**,与当前代码不符;当前命名见 `architecture.md` 与 `rename_map.md`。

## 1. 改造背景与目标

| 项目 | 说明 |
|------|------|
| 改造对象 | `code_stm32f1_mouse`(STM32F103RB,Keil MDK + ARMCC v5) |
| 改造依据 | 改造计划 P0–P8(9 阶段 57 项)+ 编码规范 `coding-standard-cpp.md` |
| 参考标杆 | `标杆代码/`(STM32F103RB 模块化参考) |
| 核心原则 | **只改外壳不动内核**;13 项红线仅可加注释 / 提具名宏(值不变),**禁改逻辑与数值** |
| 完成状态 | P0–P8、U1–U13、阶段①–④ 全部完成并赛场验证通过,已合并远端 `main`(squash `9eda18d`) |

## 2. 改造时间线

| 阶段 | 主题 | 摘要 |
|------|------|------|
| P0 | 编码与文件头基础设施 | GBK→UTF-8(BOM);19 文件补 Doxygen 五字段头;include guard 统一 |
| P1 | 命名规范化 | 目录/拼写修正;Guc/Gmc 变量去前缀语义化;~88 个核心函数统一 MODULE_Action |
| P2 | 魔数治理(全红线) | 墙位掩码/PID 增益/几何/速度常量提具名宏,数值零改动 |
| P3 | 注释体系 | 全函数 Doxygen 五字段;核心算法加详注 |
| P4 | 头文件与接口整理 | .h 四段式 banner;extern 归位;comm_types.h 基础类型 |
| P5 | 函数拆分与去重 | SysTick 拆 21 个 doTask;main 拆子函数;贴墙/冲刺系列参数化去重 |
| P6 | 文件拆分与模块化 | 4 大文件拆分:utility 2178→5 模块、timer→6 文件、strategy→4 文件、main 1676→30 行 |
| P6.5 | extern 归属治理 | 散落 extern 106→0;退役 utility_globals.h |
| P7 | 全局变量治理 | 经 U 系列与阶段①–④ 分步封装入结构体 |
| P8 | 项目文档 | 本文档体系:README + docs/(架构/算法/改造记录) |
| U1–U13 | 结构封装深化 | 死代码清理/单点局部化/结构体封装/main.h 统一聚合(9 完成,4 项并入阶段①–④) |
| 阶段①–④ | 散落全局治理 | 清场→去重局部化→非 ISR 封装→ISR 共享封装+volatile 收尾 |
| O1–O7 | 发布前优化 | 施工注释清理/职责错位修正/模块拆分/位带宏迁移/参数固化/motor 整合 |

## 3. 量化对比指标

| 指标 | 改造前 | 改造后 |
|------|--------|--------|
| 源码编码 | GBK | UTF-8(BOM) |
| 源文件 .c/.h | 25 | 51 |
| 函数命名 MODULE_Action | 0 | ~88(P1-10 四批次) |
| 命名宏(魔数治理) | 0 | ~40+(P2 八项) |
| 散落 extern | 106 | 0(P6.5 归零) |
| 结构体类型 | 0 | 20+ |
| main.c 行数 | 1676 | 30(<50 达标) |
| 注释 | 少量 | 全函数 Doxygen 五字段 + 红线/算法详注 |
| ROM 占用 | — | 74.59 KB |
| RAM 占用 | — | 19.56 KB |
| 红线数值改动 | — | **0 / 13** |

## 4. 阶段成果摘要

### P0–P8

| 阶段 | 关键成果 | 红线 | 验证 |
|------|----------|------|------|
| P0 | 全项目编码转换与文件头补齐,编译零影响 | — | 无损校验 25/25 |
| P1 | 88 个函数 MODULE_Action 化 + 变量语义化 | — | diff 等量增删 + 引用计数核对 |
| P2 | 魔数全部提具名宏,数值逐项比对 | 全阶段红线 | 无裸掩码/裸增益残留 |
| P3 | 84 个函数补 Doxygen;核心算法详注 | 仅注释 | 零逻辑影响 |
| P4 | .h 模板统一;extern 按定义模块归位 | — | 符号可达性脚本校验 |
| P5 | SysTick 21 doTask + main 拆子函数 + 参数化去重 | 部分红线 | 代码行多重集一致 + Keil 回归 |
| P6 | 4 大文件拆分模块化,main 30 行 | — | 函数计数一致 + 多重集零丢失 |
| P6.5 | extern 106→0,退役脚手架头 | — | 每符号单次定义 + 消费者覆盖校验 |
| P7 | 全局变量分域封装入结构体 | — | Keil + 赛场回归 |
| P8 | 项目文档体系 | — | 本次交付 |

### U1–U13(结构封装深化)

13 项中 **9 项直接完成**(U1 死代码清理/U2 单点局部化/U3 循环计数器消除/U4 main.h 聚合/U5 main 逻辑提取/U7 SystemConfig/U10 TaskState/U11 SpurtConfig/U12 static 化);U6(MazeState)/U8(ISR 变量)/U9(FlashState)/U13(调试变量)并入阶段①–④ 治理。

### 阶段①–④(散落全局治理)

| 阶段 | 内容 | 状态 |
|------|------|------|
| ① 清场 | 删 10 个死/条件死代码;showbattery 封入 BattMonitor.enabled | ✅ |
| ② 去重+局部化 | flashreadtemp 局部化;actionsequence 移 static;删未用全局 | ✅ |
| ③ 非 ISR 共享封装 | MazeState(32 成员)/SpurtSpeedConfig/SearchState/SearchStateMachine 四大结构体 | ✅ |
| ④ ISR 共享封装 + volatile | motionTask/speed/angle/flash 等归入结构体并补 volatile;KalmanState/IrCalib;turnDelay 类型修正 | ✅ 已赛场回归 |

### O1–O7(发布前优化)

| 编号 | 内容 | 状态 |
|------|------|------|
| O1 | 施工注释清理(~280 处墓碑/阶段前缀),红线警告保留 | ✅ |
| O3 | delay/ISR 职责错位修正(DELAY_*/ISR_Init 归位) | ✅ |
| O4 | adc.c 拆分为 ADC 硬件层 + IR 应用层(irs.c) | ✅ |
| O5 | sys.h 位带宏迁移至 main.h(31 宏) | ✅ |
| O6 | 初始化函数参数固化(SYS_ClockInit/USART_Init/ISR_Init 无参) | ✅ |
| O7 | motor.c/h 整合电机功能(6 函数逐字符 IDENTICAL 搬移) | ✅ |
| O2 | 硬件模块文件重组(gyro/odom/motor/irs 独立 .h) | ⬜ 后续规划 |

## 5. 红线遵守总表(13 项)

| # | 红线内容 | 处置方式 | 状态 |
|---|----------|----------|------|
| 1 | 墙位编码写入与邻居同步(MAZE_WallCheck) | 提 WALL_* 宏 + 改名,数值零改 | ✅ |
| 2 | DFS 洪水填充栈操作与低 四位 判墙(MAZE_FloodFillDFS) | 改名 + 算法详注 | ✅ |
| 3 | BFS 洪水填充双栈与高 四位 判墙(MAZE_FloodFillBFS) | 改名 + 算法详注 | ✅ |
| 4 | 8 方向编码与 `(8+…)%8` 动作算术(PATH_Fastway8Dir) | 改名 + 注释 | ✅ |
| 5 | 391 点转弯查找表(speedtable.c) | 文件原样保留,零触碰 | ✅ |
| 6 | 内联 PID 增益常数 | 提 MOTOR_PID_*/TURN_PID_* 具名宏,值不变 | ✅ |
| 7 | crosswayChoice 距离比较与子方法选择 | 注释保留,逻辑零改 | ✅ |
| 8 | deadwaycheck 临时封路+连通性判定(MAZE_DeadwayCheck) | 改名 + 详注 | ✅ |
| 9 | spurt 三档 PID 增益数值(SPURT_GainSet) | 改名 + 注释,增益零改 | ✅ |
| 10 | mazeSearch 连续运动 180 阈值(MAZE_Search) | 改名 + 注释,阈值零改 | ✅ |
| 11 | goalWallchange 目标格 四位 操作(MAZE_GoalWallChange) | 改名 | ✅ |
| 12 | 速度常数与动态斜坡 | 提具名宏(MAXSPEED 等),值不变 | ✅ |
| 13 | 编码器溢出回绕与陀螺积分(GYRO_Sample/ODOM_Sample) | 零触碰,未改名 | ✅ |

**合计:13/13 零改动。**

## 6. 命名规范与取舍

| 维度 | 规范 | 说明 |
|------|------|------|
| 函数 | `MODULE_Action` | MAZE_/PATH_/SPURT_/MOTION_/LED_/FLASH_/IR_/ADC_/MOTOR_/TIMER_/STATE_/CALIB_ 等 |
| 变量 | lowerCamelCase(≤15 字符) | 全局不加 g_ 前缀(已封装入结构体) |
| 类型 | UpperCamelCase / XxxStruct | 20+ 个运行状态结构体 |
| 宏 | SCREAMING_SNAKE_CASE + 全模块名前缀 | 如 MAZE_WALL_UP_OPEN |
| 枚举 | TOPIC_VALUE | 如 MotionTask_t / SpurtAction_t / SpurtSpeed_t |
| 编码 | UTF-8(BOM) + CRLF | 全项目统一,Keil 兼容 |
| 注释 | Doxygen 五字段(@file/@brief/@author/@date/@note) | 全函数补齐 |

**取舍决策**:函数命名与宏前缀与标杆一致(赛曙标准同向);变量大小写遵循赛曙标准 lowerCamelCase(与标杆 PascalCase 有意偏离,标准优先)。

## 7. 验证机制

- **Keil 编译 0 error**:每项改造后全量编译通过。
- **reverse-map 字节比对**:改名/搬移函数,反汇编函数体与基线逐字节一致。
- **符号可达性脚本**:每个 extern 恰一次定义、消费者 include 全覆盖、无重复声明。
- **归一化比对**:搬移函数去空白/注释后逐字符一致(O7 六函数 IDENTICAL)。
- **赛场回归**:搜索/冲刺/回起点全流程性能验证(O7、阶段④ 等 ISR 热路径改动重点回归)。

## 8. 当前遗留事项

| 事项 | 说明 | 状态 |
|------|------|------|
| 函数归位与文件重组 | sprint→spurt、timer→core、maze/explore 子模块前缀等文件级整理 | 已确认,**暂不实施**(计划见 `../../函数归位与文件重组计划.md`) |
| O2 硬件模块文件重组 | gyro/odom/motor/irs 独立 .h,拆分 timer.h 万能头 | ⬜ 后续规划(O2-1~O2-5) |

以上均为**文件级结构调整**,不涉及算法与数值改动,实施前后按红线纪律做 reverse-map 与赛场回归即可。
