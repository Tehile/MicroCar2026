# 系统架构设计

本文档描述 `code_stm32f1_mouse`(STM32F103RB 电脑鼠)的**当前**分层架构、前后台协作机制与模块职责。代码组织为五个目录,遵循"算法决策与实时执行分离"的**前后台系统**设计:前台主循环负责"想清楚下一步做什么",后台 SysTick 1ms 中断负责"把这 1ms 的运动做出来"。

## 1. 分层架构总览

```mermaid
graph TD
    A["应用层 APP<br/>state / menu / calibrate"]
    B["迷宫算法层 STRATEGY<br/>maze / floodfill / path / explore"]
    C["运动执行层 MOTION<br/>motion / spurt / search / speedtable"]
    D["硬件驱动与实时控制 HARDWARE<br/>core / isr / motor / adc / irs / led / flash / gyro / odom / safety / sys / delay / usart"]
    A -->|"主状态机调用算法"| B
    B -->|"生成动作/更新墙图"| A
    B -->|"算法结果交执行层"| C
    A -->|"调用运动封装"| C
    C -->|"写 motionCtrl.task, ISR 执行"| D
    A --> D
    B --> D
```

调用方向:应用层决策 → 算法层计算(建图/最短路/探索法则)→ 运动执行层封装 → 硬件层(ISR)实时执行。

## 2. 目录结构与模块职责

### APP/ 应用层

| 文件 | 承载内容 |
|------|----------|
| state.c / state.h | 主任务状态机 `STATE_MainLoop`、`STATE_Start/MazeSearch/ReturnSearch/Spurt/Spurt45/Spurt90`、赛前准备 `STATE_LoadMapAndSelectSpeed`、`STATE_DetermineGoaheadNum` |
| menu.c / menu.h | `MENU_Run` 按键菜单(0–8 档) |
| calibrate.c / calibrate.h | `CALIB_*` 红外/陀螺/传感器标定 |

### STRATEGY/ 迷宫算法层

| 文件 | 承载内容 |
|------|----------|
| maze.c / maze.h | 迷宫状态 `mazeState`(坐标/方向/任务码/墙图/栈数组)、`MAZE_Init`、`MAZE_CoorUpdate/Fast`、`MAZE_WallCheck` 墙检测与镜像同步、`MAZE_BreakOrNot`、墙位/方向编码宏 |
| floodfill.c / floodfill.h | `FLOODFILL_Close/Open/CloseCopy/OpenCopy` 洪水填充距离图(全闭图等高值/全开图最短路) |
| path.c / path.h | `PATH_Fastway8Dir/4Dir/BackPath` 8/4 方向最短路路径生成 |
| explore.c / explore.h | `EXPLORE_*` 六种贴墙法则、中心法则、岔路决策、死路剪枝、回退点选择 |

### MOTION/ 运动执行层

| 文件 | 承载内容 |
|------|----------|
| motion.c / motion.h | `MOTION_*` 直线/转弯/原地转向/加减速、转弯参数表 `turnParamTable` |
| spurt.c / spurt.h | `SPURT_*` 冲刺动作序列生成与执行、档位增益、`SpurtAction_t`/`SpurtSpeed_t` |
| search.c / search.h | `SEARCH_Search` 搜索走格、`SEARCH_HasWall/NoWall` 格内驱动方式 |
| speedtable.c / speedtable.h | 391 点转弯速度查找表(前馈 gyro/vl/vr) |

### HARDWARE/ 硬件驱动与实时控制

| 文件 | 承载内容 |
|------|----------|
| core.c / core.h | 核心类型容器(全部结构体类型与全局实例定义)、`CORE_Init`、PID 增益/墙判阈值宏 |
| isr.c / isr.h | `ISR_Init`、`SysTick_Handler`(1ms 调度)、21 个 `doTask*` 运动函数、`doWallDetect` |
| motor.c / motor.h | `MOTOR_*` 电机 PWM、编码器采样、速度环 PID |
| adc.c / adc.h | `ADC_*` 采样 |
| irs.c / irs.h | `IR_*` 红外测距与标定 |
| led.c / led.h | `LED_*` 显示、按键 |
| flash.c / flash.h | Flash 底层驱动 `FLASH_*` 与运行数据读写 |
| gyro.c / gyro.h | `GYRO_Init`、`GYRO_Sample` 陀螺角速率积分 |
| odom.c / odom.h | `ODOM_Sample` 编码器里程计 |
| safety.c / safety.h | `SAFETY_Monitor` 急停/电池/CPU 监控 |
| sys.c / sys.h | `SYS_ClockInit` 时钟初始化、位带操作宏 |
| delay.c / delay.h | `DELAY_*` 微秒/毫秒延时 |
| usart.c / usart.h | `USART_*` 串口、printf 重定向 |

### USER/ 入口层

| 文件 | 承载内容 |
|------|----------|
| main.c / main.h | `main()` 启动流程、`MOUSE_Init`/`HARDWARE_Init`/`TASK_Init`;聚合头 main.h 统一包含各模块头 |

## 3. 前后台系统(两张详图)

电脑鼠是典型的**前后台系统**:主循环(前台)是"指挥官",SysTick 1ms 中断(后台)是"执行者"。两者之间没有消息队列或事件回调,靠**共享变量握手**(见 §3.3)。

### 3.1 图A —— 前台系统:主循环状态机干什么

前台的全部工作就是 `STATE_MainLoop` 里的 `while(1) switch(mazeState.mouseTask)`:按当前阶段码把控制权交给对应的 `STATE_*` 阶段函数。每个阶段函数"拥有"一段流程,决定做什么、何时结束、下一阶段交给谁。

```mermaid
flowchart TD
    S0["STATE_MainLoop<br/>while(1) switch(mouseTask)"]

    S0 --> W["WAIT<br/>MOTION_Acc + SetTrack(100)<br/>起步等待"]
    W --> ST["START<br/>STATE_Start: 走一格探测左/右出口<br/>判起点在迷宫哪侧, 必要时镜像翻转墙图,<br/>登记首个岔路支点"]
    ST --> MS["MAZESEARCH<br/>STATE_MazeSearch 探索核心"]

    MS --> MS1{"当前格在<br/>终点区?"}
    MS1 -- "是" --> E1["记录到达终点<br/>算矫正方向 goalCorrectDir,<br/>EXPLORE_GoalWallChange 打通终点墙,<br/>重算起点/终点双距离图, 存图"]
    E1 --> E2{"taskSt.backSearch == 0?"}
    E2 -- "是(不补搜)" --> SP["SPURT<br/>低速回起点直接冲刺"]
    E2 -- "否(继续补搜)" --> E3{"起点已满足<br/>stepMax 或最优?"}
    E3 -- "是" --> SP
    E3 -- "否" --> RS["STATE_ReturnSearch<br/>返回途中搜索"]

    MS1 -- "否" --> MS2{"goalGet == 0?<br/>(未到过终点)"}
    MS2 -- "是(探索主干)" --> MS3{"EXPLORE_CrosswayCheck<br/>数当前格可前进支路 roadCount"}
    MS3 -- ">0 有路" --> MS4["EXPLORE_DeadwayCheck 死路剪枝<br/>EXPLORE_CrosswayChoice 岔路决策(最短优先)"]
    MS4 --> MS5["SEARCH_Search 走一格<br/>→写 motionCtrl.task → ISR 执行<br/>→轮询 odomStr.cellOdom 完成信号"]
    MS5 --> MS1
    MS3 -- "0 死路" --> MS6["沿支点栈回退到最近有路支点<br/>SPURT_ObjectGoTo 快速跳到"]
    MS6 --> MS1

    RS --> RS1{"补搜仍有优化价值?<br/>mapStepCloseCopy[支点]+mapStepOpen[支点]<br/>&lt; mapStepClose[起点]"}
    RS1 -- "是" --> MS4
    RS1 -- "否" --> RS2{"起点已满足<br/>stepMax 或最优?"}
    RS2 -- "是" --> SP
    RS2 -- "否" --> RS3["回溯支点栈找更近支路<br/>继续补搜"]
    RS3 --> MS4

    SP["SPURT<br/>STATE_Spurt 存墙图/起终点<br/>决定 45°/90° 冲刺"]
    SP --> SP1{"goaheadnum 冲刺型"}
    SP1 --> SP45["SPURT_45<br/>PATH_Fastway8Dir 斜切路径<br/>SPURT_ActionGenerate + SPURT_Action"]
    SP1 --> SP90["SPURT_90<br/>PATH_Fastway4Dir<br/>SPURT_ActionGenerate90 + SPURT_Action90"]
```

> 关键点:前台**从不亲自驱动电机**。它只决策,然后调用运动封装函数(走一格/转弯/冲刺直行),封装函数写 `motionCtrl.task` 交给 ISR,自己忙等完成信号,封装返回"做完了"才推进下一步。

### 3.2 图B —— 后台系统:SysTick 1ms 中断干什么

后台的工作在 `SysTick_Handler` 里,每个 1ms 按固定的"感知 → 决策 → 行动 → 安全"流水线执行一次:

```mermaid
flowchart LR
    T["SysTick 1ms 中断"] --> G["① 感知 GYRO_Sample<br/>陀螺角速率梯形积分 → 航向角"]
    G --> O["① 感知 ODOM_Sample<br/>编码器里程/角度(含溢出回绕)"]
    O --> W["① 感知 doWallDetect<br/>红外测距 → 侧/前/45°/临界墙判定"]
    W --> D["② 决策 switch(motionCtrl.task)<br/>执行对应 doTask* → 算左右目标速度<br/>speedSt.speedTargetL/R"]
    D --> M["③ 行动 MOTOR_EncSample<br/>编码器测速 + 速度环 PID 误差更新"]
    M --> L["③ 行动 MOTOR_LoopControl<br/>PID 输出 → 限幅 → PWM 方向输出"]
    L --> S["④ 安全 SAFETY_Monitor<br/>急停 / 电池 / 数据保存 / CPU 占用"]
```

**① 感知** —— 采集本毫秒的"现实":陀螺航向角、编码器里程/角度、红外四方向距离,并判定墙位。

**② 决策** —— `switch(motionCtrl.task)` 执行对应的 `doTask*`:每个 doTask 是一套"这 1ms 该怎么开"的算式,依据刚采到的角度/墙距/里程算出左右轮**目标速度**。21 个 doTask 与任务码的映射:

| 分类 | 任务码 → doTask | 职责 |
|------|-----------------|------|
| 基础运动 | GOSTRAIGHT=1 → doTaskGoStraight | 闭眼直线:陀螺角 PD |
| | TURN=2 → doTaskTurn | 转弯:查前馈表 + 角度 PD |
| | TURNAROUND=4 → doTaskTurnAround | 原地 180°:两阶段(查表转向→陀螺对正) |
| | STOP=5 → doTaskStop | 刹车:负斜坡减速 |
| | STRAIGHT45=17 → doTaskStraight45 | 45° 斜线冲刺:侧/前红外双项误差 |
| 转弯前后 | BEFORE90TURN=27 / BEFORE180TURN=28 → doTaskBefore90/180Turn | 转弯前对正与定位 |
| 智能直行 | SMARTSTRAIGHT=30 → doTaskSmartStraight | 智能直线:有墙用红外/无墙用陀螺 |
| | SMARTSTRAIGHTFAST=32 → doTaskSmartStraightFast | 搜索直线:懒惰贴墙侧 + 分档增益 |
| | STRAIGHTEYECLOSECOORADJ=33 / STRAIGHTPOLE=34 | 闭眼+纵向矫正 / 沿杆直线 |
| 加减速/贴墙 | ACC=35 / SLOWDOWN=36 / SPEEDUP=37 | 加速/减速/提速 |
| | KEEPWALLDISTANCE=38 / POINTTURN90=39 / FORWARDIRTEST=41 | 贴墙/原地90/前向对正 |
| 冲刺 | SPURT90BLIND=42 → doTaskSpurt90Blind | 90° 冲刺闭眼:陀螺 PD |
| | SPURT90IR=43 → doTaskSpurt90Ir | 90° 冲刺睁眼:红外 PD |
| | SMARTSTRAIGHTSPURT=44 / SMARTSTRAIGHTSPURTL=45 | 转弯前补全直线 |

**③ 行动** —— `MOTOR_EncSample` 测实时速度并更新 PID 误差,`MOTOR_LoopControl` 把"目标速度 vs 实时速度"跑 PID 闭环,输出 PWM 驱动电机。

**④ 安全** —— `SAFETY_Monitor` 检查急停/电池/运行数据保存/CPU 占用。

### 3.3 握手:命令下行,完成信号上行

运动封装函数与 ISR 之间靠**共享变量**完成握手:

1. **命令下行**:封装函数写运动任务码 `motionCtrl.task`,告诉 ISR"接下来执行哪种运动"。
2. **完成信号上行**:封装函数随后**忙等轮询** ISR 每个毫秒回写的状态量,条件满足即返回:

| 共享变量 | 方向 | 语义 | 典型等待条件 |
|----------|------|------|--------------|
| `motionCtrl.task` | 前台 → ISR | 运动任务码(MotionTask_t) | 赋值即开始执行 |
| `odomStr.cellOdom` | ISR → 前台 | 格内已走里程(0→180mm) | `while(cellOdom < 180)` 走满一格 |
| `motionCtrl.tick` | ISR → 前台 | 动作节拍计数 | `while(tick < 50)` 保持 50ms |
| `wallSns.left/right/front` | ISR → 前台 | 侧/前墙有无判定 | 转弯前确认侧墙 |
| `speedSt.avgSpeed` | ISR → 前台 | 实时平均速度 | 加减速到位判断 |

```mermaid
sequenceDiagram
    participant SM as 主状态机 STATE_*
    participant MO as 运动封装(MOTION_* / SEARCH_Search)
    participant ISR as SysTick ISR(1ms)
    SM->>MO: 阶段决策:下一步走 / 转 / 冲?
    MO->>ISR: ① 写命令 motionCtrl.task(如 GOSTRAIGHT / TURN)
    loop 每 1ms
        ISR->>ISR: 感知:采样陀螺 / 编码器 / 红外
        ISR->>ISR: 决策:switch(motionCtrl.task) 执行 doTask* → 算目标速度
        ISR->>ISR: 行动:电机 PID → PWM
        ISR-->>MO: ② 回写完成信号(cellOdom / motionCtrl.tick / wallSns)
    end
    MO-->>SM: ③ 轮询到条件满足,返回"动作完成"
    SM->>SM: 推进到下一格 / 下一阶段
```

## 4. 启动流程

```mermaid
graph LR
    START["main()"] --> H1["HARDWARE_Init<br/>时钟→串口→延时→ADC→红外→电机→LED→编码器<br/>→CORE_Init→ISR_Init→GYRO_Init"]
    H1 --> M1["MOUSE_Init<br/>MAZE_Init + SEARCH_Init + SPURT_Init + TASK_Init"]
    M1 --> C1["CALIB_InitIr<br/>红外标定查表"]
    C1 --> MN["MENU_Run<br/>按键选择功能档位"]
    MN --> LD["STATE_LoadMapAndSelectSpeed<br/>读 Flash 地图/选速度档"]
    LD --> DG["STATE_DetermineGoaheadNum<br/>前向红外定冲刺格数"]
    DG --> ML["STATE_MainLoop<br/>主任务状态机(不返回)"]
```

> 初始化顺序红线:`ISR_Init`(开启 SysTick 1ms)必须放在所有外设初始化之后;`CORE_Init` 的速度/PID/墙检配置须在 ISR_Init 之前(ISR 每周期读);`GYRO_Init` 的陀螺零点校准须在 ISR_Init 之后(依赖 ISR 递增校正计数)。

## 5. 全局状态组织

全局运行状态以结构体形式组织,类型集中定义于 `core.h`,实例定义于 `core.c` 及各模块 .c。

| 结构体 | 实例 | 定义位置 | 承载内容 |
|--------|------|----------|----------|
| MazeState | mazeState | maze.c | 迷宫坐标/方向/任务码/墙图/路径栈 |
| MotionCtrl | motionCtrl | core.c | 运动状态机:pidErr/任务码/步进/节拍 |
| OdomStruct | odomStr | core.c | 编码器里程与角度 |
| GyroStruct | gyroStr | gyro.c | 陀螺角速率/积分角度/零偏 |
| IrDist | irDist | core.c | 红外前/左/右距离 |
| SpeedConfig | speedCfg | core.c | 各场景速度配置 |
| SpeedState | speedSt | core.c | 左右目标速度/平均速度/PWM |
| PidGainConfig | pidGain | core.c | PID 增益集合 |
| TurnTable | turnTbl | core.c | 转弯查表指针 |
| WallSensor | wallSns | core.c | 墙检测状态与阈值 |
| EmergMonitor / BattMonitor / CpuMonitor | emergMon / battMon / cpuMon | core.c / isr.c | 急停/电池/CPU 监控 |
| SysConfig / FlashState | sysCfg / flashSt | isr.c / core.c | 系统配置常量 / Flash 状态 |
| TaskState | taskSt | main.c | 任务状态:goaheadnum/backSearch/spurtFlag |
| SpurtConfig | spurtCfg | core.c | 冲刺 PID 参数 |
| SpurtSpeedConfig | spurtSpd | spurt.c | 冲刺速度常量 |
| SearchState | searchSt | search.c | 搜索/转弯偏移/速度档 |
| SearchStateMachine | searchSm | state.c | 搜索状态机跨周期变量(static) |

## 6. 头文件聚合模式

各模块 `.c` 仅包含自己的 `.h`,各 `.h` 统一包含聚合头 `main.h`;`main.h` 按层依次包含系统/硬件/核心控制/算法/执行/应用各模块头,并定义位带操作宏。

```mermaid
graph BT
    A1["motion.c"] --> B1["motion.h"]
    A2["maze.c"] --> B2["maze.h"]
    A3["core.c"] --> B3["core.h"]
    B1 --> M["main.h"]
    B2 --> M
    B3 --> M
    M --> S["系统/硬件/算法/执行/应用 各模块头"]
```

该模式保证全项目符号可见性一致,新增模块只需在 `main.h` 登记即可全局访问。
