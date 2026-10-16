/*
*********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*       ------------------------------------------------------------------------------------------------
*               Function: link_port.c —— 平台适配层实现(比赛固件远程化移植 P1, 新增文件)
*       ------------------------------------------------------------------------------------------------
*               本文件是 link.c(协议层, 原样移植)与新比赛固件之间唯一的地板:
*                 - 定义 link.c 引用的全部镜像结构体实例(远程档独有, 比赛档不存在);
*                 - LP_SyncIn/LP_SyncOut 完成"镜像 <-> 新固件裸全局量"的单向同步;
*                 - LP_* 动作钩子把链路层的停车/复位/标记灯落到新固件引脚与全局量上。
*
*               ◆ 映射总表(新固件全局 -> 镜像字段), 按"固件里有没有对应量"分三档:
*                 A. 有对应量: LP_SyncIn 每 tick 刷新(测量量), LP_SyncOut 按需推回(参数);
*                 B. 无对应量但新固件有**字面量**: 影子参数(irCfg.thrFront 各档/powerCfg 各字段),
*                    可读可写可持久化, 不驱动固件决策;
*                 C. 无对应量且无消费者: stackIdx/battMon.adc 等, 恒 0(仅占位)。
*********************************************************************************************************
*/

#include "link_port.h"

/* ※ 整文件门控: 本文件**只存在于远程档**。
 * 原因(实测 2026-10-16): Keil `UV4 -b -t "<target>"` 会把工程里的 .c 编进**每一个**
 * Target —— 只登记在 Target_Remote 组里的 link_port.c, 编 Target 1 时也被编译了。
 * 而比赛档下 user.c 的 TrackRatioL 等是 const(不可赋值)、镜像实例也是纯增开销,
 * 故用 #ifdef 把整文件变成空翻译单元, 与工程铁律"两档共用代码必须门控"一致。
 * (P2 移植 link.c 时同样处理: 所有 REMOTE/ 下的 .c 都要整文件门控。) */
#ifdef MOUSE_REMOTE

/*------------------------------------  镜像实例定义  ------------------------------------*/
/* link.c 直接引用这些全局名(与旧工程同名), 故必须在此定义且**不加 static**。 */

MazeState mazeState;
IrCalib irCalib;
KalmanState kalmanSt;
IrDist irDist;
IrCfg irCfg;
SysConfig sysCfg;
SpeedConfig speedCfg;
PidGainConfig pidGain;
SpeedState speedSt;
GyroStruct gyroStr;
OdomStruct odomStr;
MotionCtrl motionCtrl;
WallSensor wallSns;
BattMonitor battMon;
PowerCfg powerCfg;
TaskState taskSt;
EmergMonitor emergMon;
uchar spurtmode;
float turnDelay;

/* 协作式中止标志(见 link_port.h 的说明) */
u8 linkRunAbort = 0;

/* ledBeaconLock 影子(新固件无对应物, 详见 link_port.h 注释): link.c 会写,
 * 新固件无人读; 定义在远程档, 比赛档不占 RAM。 */
u8 ledBeaconLock = 0;

/*------------------------------------  同步: 新固件 -> 镜像  ------------------------------------*/
/**
 * @brief  每 tick 把新固件的裸全局量刷新进 link.c 的只读镜像
 * @note   调用点: LINK_Tick() 开头(SysTick 1ms 上下文, 与固件其余部分同上下文, 无需 volatile)
 *         ◆ 只刷新"有固件来源"的字段; 影子参数(irCfg.thrFront 各档/powerCfg 各字段/sysCfg.trackCoord)
 *           一律不碰, 否则 SETQ 改的值会被下一秒的刷新打回源码默认值。
 *         ◆ 墙图/访问图(512B)不在此拷贝 —— MAP DUMP 前调 LP_SyncMap() 按需拷。
 */
void LP_SyncIn(void)
{
    /* ---- 红外: 距离(mm)与原始 ADC ---- */
    irDist.f1 = disf1;
    irDist.f2 = disf2;
    irDist.f = disf;
    irDist.l = disl;
    irDist.r = disr;
    irCalib.rawf1 = rawf1;
    irCalib.rawf2 = rawf2;
    irCalib.rawl = rawl;
    irCalib.rawr = rawr;
    irCalib.rawf1avrgnear = rawf1avrgnear;
    irCalib.rawf2avrgnear = rawf2avrgnear;
    irCalib.rawlavrgnear = rawlavrgnear;
    irCalib.rawravrgnear = rawravrgnear;
    irCalib.rawf1avrgfar = rawf1avrgfar;
    irCalib.rawf2avrgfar = rawf2avrgfar;
    irCalib.rawlavrgfar = rawlavrgfar;
    irCalib.rawravrgfar = rawravrgfar;
    irCalib.kalmanswitch = kalmanswitch;
    irCalib.calibMode = biaodingmode;
    kalmanSt.Qf1 = Qf1;
    kalmanSt.Qf2 = Qf2;
    kalmanSt.Ql = Ql;
    kalmanSt.Qr = Qr;
    kalmanSt.Pf1 = Pf1;
    kalmanSt.Pf2 = Pf2;
    kalmanSt.Pl = Pl;
    kalmanSt.Pr = Pr;

    /* ---- 电池 ----
     * 新固件 battary 的单位是 "电压x100"(8.00V -> 800), 旧 battMon.mv 是 mV(8000),
     * 故 x10 换算。※ 新固件只在 showbattery==1 时更新 battary(P2 门控插入解除),
     * 解除前 battary 恒 0 => 遥测 battMv 恒 0(已知限制, 不影响其他字段)。 */
    battMon.voltage = battary;
    battMon.mv = (u16)(battary * 10.0);
    battMon.percent = battarypercent;
    battMon.adc = 0; /* 新固件不缓存电池 ADC 原值(C 档: 无消费者) */

    /* ---- 速度/电机 ---- */
    speedSt.speedL = speedl;
    speedSt.speedR = speedr;
    speedSt.freemode = freemode;
    speedSt.sleepmode = sleepmode;

    /* ---- 陀螺 ---- */
    gyroStr.raw = gyro;
    gyroStr.bias = gyro0;
    gyroStr.angle = gangle;
    gyroStr.angleAcc = ganglet;
    gyroStr.adjFlag = gyro_adj;
    gyroStr.adjCounter = angle_counter;
    gyroStr.correctionEnable = (u8)gyroswitch;

    /* ---- 编码器/里程 ---- */
    odomStr.encL = leftb;
    odomStr.encR = rightb;
    odomStr.distL = leftd;
    odomStr.distR = rightd;
    odomStr.distLCycle = leftcycle;
    odomStr.distRCycle = rightcycle;
    odomStr.trackAbsL = (long)absolutetrackl;
    odomStr.trackAbsR = (long)absolutetrackr;
    odomStr.trackL = trackl;
    odomStr.trackR = trackr;
    odomStr.cellOdom = track;
    odomStr.angle = angle;
    odomStr.angleBase = baseangle;

    /* ---- 运动状态机 ---- */
    motionCtrl.task = task;
    motionCtrl.step = step;
    motionCtrl.tick = tick;
    motionCtrl.pidErr = e;

    /* ---- 墙检测 ---- */
    wallSns.left = lefthaswall;
    wallSns.right = righthaswall;
    wallSns.front = forwardhaswall;
    wallSns.left45 = lefthaswall45;
    wallSns.right45 = righthaswall45;
    wallSns.critL = leftwallcritical;
    wallSns.critR = rightwallcritical;
    wallSns.irSwitch = irswitch;
    wallSns.wallThresh = sidewall_threshold;

    /* ---- 迷宫状态 ---- */
    mazeState.mousePos.cX = GmcMouse.cX;
    mazeState.mousePos.cY = GmcMouse.cY;
    mazeState.mouseDirection = GucMouseDir;
    mazeState.mouseTask = GucMouseTask;
    mazeState.xStart = GucXStart;
    mazeState.yStart = GucYStart;
    mazeState.goalX = GoalX;
    mazeState.goalY = GoalY;
    mazeState.fastwayCount = FastwayCount;
    mazeState.fastwayCount90 = FastwayCount_90;
    mazeState.spurtTestFlag = spurtTestFlag;
    mazeState.objectGoToFlag = ObjectGoTo_Flag;
    mazeState.stackIdx = 0; /* 新固件的栈索引是 mazeSearch 的局部量, 无全局可映射 */

    /* ---- 冲刺/任务标志 ---- */
    taskSt.spurtFlag = (u8)spurt_flag;
    taskSt.goOrBack = GoOrBackFlag;
    taskSt.goaheadnum = goaheadnum;
    spurtmode = speedmode; /* 档位码 60/61/62/63 两套固件一致, 无需换算 */

    /* ---- 急停 ---- */
    emergMon.active = isemergency;
    emergMon.delay = emergency_delay;

    /* ---- 系统配置(参数表 id 1-7 的宿主; 只读刷新, 写在 LP_SyncOut) ---- */
    sysCfg.trackRatioL = TrackRatioL;
    sysCfg.trackRatioR = TrackRatioR;
    sysCfg.speedRatioL = SpeedRatioL;
    sysCfg.speedRatioR = SpeedRatioR;
    sysCfg.gyroRatio = (u16)gangleratio;
    sysCfg.encAngleRatio = (u16)angleratio;
    sysCfg.distToWall = dis2wall;

    /* ---- 速度/增益配置(id 41/42/44/45/46 的宿主) ---- */
    speedCfg.search = speed_ss;
    speedCfg.turn = speed_turn;
    pidGain.goStraight = GOSTRAIGHT_kp;
    pidGain.turnP = turn_kp;
    pidGain.turnD = turn_kd;
    turnDelay = (float)turn_delay; /* 新固件是 u8(格), 旧参数是 float; 取整推送见 LP_SyncOut */
}

/*------------------------------------  同步: 镜像 -> 新固件  ------------------------------------*/
/**
 * @brief  把镜像中的可调参数/控制量推给新固件全局量
 * @note   调用点: LINK_Tick() 末尾(SETQ/SAVE/MOVE 命令处理之后, 同拍写入、下一 tick 生效)
 *         ◆ 只推"有固件宿主"的字段; 影子参数(irCfg.thrFront 各档/powerCfg 各字段/sysCfg.trackCoord/
 *           linkPtKp 系列/linkDisF60)不推 —— 推了也没有接收者。
 *         ◆ setvl/setvr 的单位换算与旧工程 motor.c:162 一致:
 *           target = speedTarget(cm/s) x SpeedRatio => Pulse/ms。
 */
void LP_SyncOut(void)
{
    /* ---- 控制量 ---- */
    task = motionCtrl.task;
    /* ※ 不推 setvl/setvr: 新固件里它们是电机控制环在 task 开关里每 tick 算出的
     * 输出量(ACC/STOP/TURN/... 各 case 都会写), 回写必与 task 开关打架。
     * 远程 MOVE/CAL 的直驱改由 timer.c PID 段的覆盖分支实现:
     *   task==0 && freemode==0 时, gdvl/gdvr 取 speedSt.speedTargetL/R x SpeedRatio
     * (条件里的 task==0 即"固件任务状态机没在跑", 与 link.c 把 task 置 NULL 的
     *  旧语义一致; freemode==0 即 link.c 声明的"电机使能"。) */
    freemode = speedSt.freemode;
    sleepmode = speedSt.sleepmode;
    isemergency = emergMon.active;

    /* ---- 红外 ---- */
    sidewall_threshold = wallSns.wallThresh;
    kalmanswitch = irCalib.kalmanswitch;
    Qf1 = Qf2 = Ql = Qr = (double)irCfg.kalQ / 10000.0;
    Pf1 = Pf2 = Pl = Pr = (double)irCfg.kalP / 10000.0;

    /* ---- 速度/增益 ---- */
    speed_ss = speedCfg.search;
    speed_turn = speedCfg.turn;
    GOSTRAIGHT_kp = pidGain.goStraight;
    turn_kp = pidGain.turnP;
    turn_kd = pidGain.turnD;
    kpl = linkMtrKpL;
    kil = linkMtrKiL;
    kdl = linkMtrKdL;
    kpr = linkMtrKpR;
    kir = linkMtrKiR;
    kdr = linkMtrKdR;
    turn_delay = (u8)(turnDelay + 0.5f);

    /* ---- 系统配置(远程档 user.c 的 const 已门控解除, 见 §4.3) ---- */
    TrackRatioL = sysCfg.trackRatioL;
    TrackRatioR = sysCfg.trackRatioR;
    SpeedRatioL = sysCfg.speedRatioL;
    SpeedRatioR = sysCfg.speedRatioR;
    gangleratio = sysCfg.gyroRatio;
    angleratio = sysCfg.encAngleRatio;
    dis2wall = sysCfg.distToWall;
}

/*------------------------------------  按需同步: 墙图  ------------------------------------*/
/**
 * @brief  把墙图/访问图拷进镜像(MAP DUMP 前调用)
 * @note   为什么不每 tick 拷: mapWall+mapVisited 共 512B, 每 tick 白拷约 28us
 *         (占 1ms 预算的 2.8%); MAP DUMP 是低频按需命令, 按需拷更省。
 *         编码说明: GucMapBlock 低四位本格墙、高四位镜像格墙, 与旧 mapWall 一致;
 *         GucMapGet 为访问标记(0xFF=未访问), 与旧 mapVisited 一致。
 */
void LP_SyncMap(void)
{
    u8 x, y;
    for (x = 0; x < MAZETYPE; x++)
    {
        for (y = 0; y < MAZETYPE; y++)
        {
            mazeState.mapWall[x][y] = GucMapBlock[x][y];
            mazeState.mapVisited[x][y] = GucMapGet[x][y];
        }
    }
}

/*------------------------------------  动作钩子  ------------------------------------*/
/**
 * @brief  立即停电机并请求协作式中止
 * @note   动作分两层, 都在 1ms 内生效:
 *           ① 直接写固件全局量(freemode=1/task=NULL/setvl=setvr=0) —— 本拍即停;
 *           ② linkRunAbort=1 —— 让 timer.c SysTick 顶部的门控段在后续每一拍
 *              强制满足退出条件, 把卡在 while(track<180) 里的底层忙等全部放出来。
 *         ※ 顺序: 先写全局量再置 abort, 避免 abort 段把刚写的目标速度又清一遍
 *            (虽结果相同, 但保持"停车动作只此一处"更易推理)。
 */
void LP_StopMotion(void)
{
    freemode = 1;
    task = 0; /* NULL */
    setvl = 0;
    setvr = 0;
    speedSt.freemode = 1;
    speedSt.speedTargetL = 0;
    speedSt.speedTargetR = 0;
    motionCtrl.task = 0;
    linkRunAbort = 1;
}

/** @brief 整机软复位(新固件 sys.c 已有实现, 直接调) */
void LP_SoftReset(void)
{
    Sys_Soft_Reset();
}

/**
 * @brief  发车标记灯: on=1 点亮 LED1-4, on=0 熄灭
 * @note   直接写引脚而不走 ledon()/ledoff(): 后者在 ledBeaconLock 置位时会被挡掉,
 *         而发车标记必须无论如何都亮(与旧工程 link.c 的 v2.8 逻辑一致)。
 *         新固件 4 颗灯是低电平点亮(LED1=PAout(8) 等), 故 on=1 写 0。
 */
void LP_LedMark(u8 on)
{
    if (on)
    {
        LED1 = 0;
        LED2 = 0;
        LED3 = 0;
        LED4 = 0;
    }
    else
    {
        LED1 = 1;
        LED2 = 1;
        LED3 = 1;
        LED4 = 1;
    }
}

/*------------------------------------  协作式中止  ------------------------------------*/
/** @brief 请求协作式中止(MAZE STOP / ESTOP 调; 见 link_port.h 的长说明) */
void LP_AbortRequest(void)
{
    linkRunAbort = 1;
}

/** @brief 清除中止标志(再次发车前由 LINK_MazeStarted 调, 或复位后自然为 0) */
void LP_AbortClear(void)
{
    linkRunAbort = 0;
}

/** @brief 读中止标志(timer.c 的 SysTick 顶部门控段用) */
u8 LP_AbortRequested(void)
{
    return linkRunAbort;
}

#endif /* MOUSE_REMOTE */
