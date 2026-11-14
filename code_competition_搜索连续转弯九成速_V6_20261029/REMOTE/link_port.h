#ifndef _LINK_PORT_H_
#define _LINK_PORT_H_

/*
*********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*       ------------------------------------------------------------------------------------------------
*               Function: link_port.h —— 平台适配层(比赛固件远程化移植 P1, 新增文件)
*       ------------------------------------------------------------------------------------------------
*               职责: 把旧工程 link.c 依赖的"课程固件平台面"适配到新比赛固件(code_competition):
*                 1) 镜像结构体 —— link.c 引用的 mazeState/irCalib/irDist/... 在本文件定义
*                    (仅远程档编译), 内容由 LP_SyncIn() 每 tick 从新固件裸全局量刷新;
*                 2) 动作钩子   —— LP_StopMotion / LP_SoftReset / LP_LedMark / LP_SyncOut;
*                 3) 协作式中止 —— linkRunAbort: 新固件的运动原语全是忙等(while(track<180)
*                    之类), 置"请退出"标志永远不会被检查到, 必须由 SysTick 顶部**强制满足
*                    退出条件**(task=NULL / track=0x7FFFFFFF / tick=60000 / setvl=setvr=0),
*                    配合 freemode=1(电机总开关)实现 1ms 内停车 + 忙等立即返回;
*                 4) 常量换表   —— FLASH_* -> STMFLASH_*, 新增 FLASH_ADDR_SYSCALIB 页,
*                    MOTION_TASK_* / SPURT_SPEED_* 旧名 -> 新固件同值宏。
*
*               ◆ 数据流方向约定(单向, 避免回写打环):
*                 LP_SyncIn  : 新固件全局 --> 镜像   (每 tick; 只刷新"有固件来源"的字段)
*                 LP_SyncOut : 镜像 --> 新固件全局   (SETQ/SAVE 之后; 只推参数与控制量)
*                 镜像中没有固件来源的字段(irCfg.thrFront 各档/powerCfg 各字段/sysCfg.trackCoord 等)
*                 即"链路层影子参数": 上位机可读可写、随 SAVE 持久化, 但不驱动新固件决策
*                 (新固件这些阈值是代码字面量) —— 见 link_port.c 顶部的映射总表。
*
*               ※ 编码纪律: REMOTE/ 下所有文件均为 **GBK + CRLF** —— 因为它们要被
*                  timer.c/test.c 等 GBK 文件 include。勿用 UTF-8 工具整文件回写。
*********************************************************************************************************
*/

#include <stm32f10x.h>
#include "sys.h"
#include "timer.h"
#include "stratagy.h"
#include "utility.h"
#include "user.h"
#include "adc.h"
#include "stmflash.h"
#include "led.h"
#include "usart.h"

/* 以下 4 个标准头原在 REMOTE/main.h 垫片中(link.c 的 printf/strcmp/math 需要);
   2026-10-16 移除垫片后并入本文件, 使 link.h 只需 include 本文件一处。 */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/*------------------------------------  常量换表  ------------------------------------*/

/* ---- Flash 访问: 旧 flash.h -> 新 stmflash.h(函数签名逐一致, 直接别名) ----
 * 旧 link.c 在每次写前都自己调 FLASH_Unlock(), 新固件的 STMFLASH_WriteHalfWord 同样
 * 不解锁(由调用方负责), 故别名后行为一致。 */
#define FLASH_Unlock STMFLASH_Unlock
#define FLASH_Lock STMFLASH_Lock
#define FLASH_ReadHalfWord STMFLASH_ReadHalfWord
#define FLASH_WriteHalfWord STMFLASH_WriteHalfWord
#define FLASH_ErasePage STMFLASH_ErasePage

/* ---- Flash 页地址 ----
 * link.c 用到的 9 个 FLASH_ADDR_* 中, 8 个在新固件 stmflash.h 里**同名同值**
 * (BLOCK_STACK/BLOCK_POINT/BLOCK/GOPATH_90/BACKPATH_90/IRDATA/GOPATH/BACKPATH/GYRO0),
 * 无需别名; 唯独 SYSCALIB(链路层 41 参数持久化页)是新固件没有的:
 *   旧工程放 page115(0x0801CC00); 新固件已用页为 88/89(飞行记录)与 118-127
 *   (迷宫/路径/红外/陀螺), 0x08016800~0x0801D7FF(page90-117)整段空闲。
 *   取 page114(0x0801C800) 作参数页 —— 远离两端已用区, 预留 page90-113 给未来扩展。
 * ※ 与旧工程的 page115 数据**不兼容**(参数表语义已随宿主更换), 旧页数据须重 SAVE。 */
#define FLASH_ADDR_SYSCALIB 0x0801CC00 /* page115: 链路层校准页(远程档专用)。必须与 link.h 逐 token 同值(含不写 u 后缀): 值/写法不同都会触发 #47-D 宏重定义告警 */

/* ---- 编码器常量(旧 core.h 的值; 新固件用字面量 10000/9000) ---- */
#define ENCODER_ARR_VALUE 10000           /* 编码器计数量程(0~9999, 溢出回绕模数) */
#define ENCODER_WRAP_THRESHOLD 9000       /* 前后差越界判定阈值(90%量程) */
#define ENCODER_SPEED_SIGN_THRESHOLD 5000 /* 速度正负号判定阈值(50%量程) */

/* ---- 运动任务名(旧 core.h 的 MotionTask_t 枚举名 -> 新 timer.h 的同值宏) ----
 * link.c 只用到 MOTION_TASK_NULL(停车/让出任务状态机); 其余旧枚举名在此补齐,
 * 便于阅读与将来的 link.c 移植。新固件 timer.h 已有同名同值宏(NULL/GOSTRAIGHT/...),
 * 但旧名是 MOTION_TASK_ 前缀, 故需显式映射。 */
#define MOTION_TASK_NULL 0

/* ---- 冲刺速度档(旧 spurt.h 的 SpurtSpeed_t; 数值与新 utility.h 的 LOW_90/LOW/
 * MEDIUM/HIGH **完全一致**(60/61/62/63) —— 故 spurtmode 可直接镜像到新固件的
 * speedmode, 上位机的档位码无需换算。 */
#define SPURT_SPEED_90_LOW 60
#define SPURT_SPEED_45_LOW 61
#define SPURT_SPEED_MEDIUM 62
#define SPURT_SPEED_HIGH 63

/*------------------------------------  新固件未导出的全局量  ------------------------------------*/
/* 以下全局量在新固件各 .c 里是文件级定义但未进头文件, link_port 需要显式 extern。
 * (timer.h/stratagy.h 已导出的 leftb/angle/kpl 等不重复声明。) */

/* adc.c: 卡尔曼噪声参数(IRKALQ/IRKALP 两个参数的实际生效值) */
extern double Qf1, Qf2, Ql, Qr;
extern double Pf1, Pf2, Pl, Pr;
/* adc.c: 红外原始 ADC 与滤波开关/标定模式 */
extern int rawf1, rawf2, rawl, rawr;
extern u8 kalmanswitch, biaodingmode;
/* test.c: 红外标定滑动平均(近/远 x 4 路) */
extern int rawf1avrgnear, rawf2avrgnear, rawlavrgnear, rawravrgnear;
extern int rawf1avrgfar, rawf2avrgfar, rawlavrgfar, rawravrgfar;
/* timer.c: 运动状态机 / 里程 / 电机目标 */
extern u8 task, step, freemode, sleepmode;
extern u16 tick, turnaround_tick;
extern long setvl, setvr;
extern int speedl, speedr, speed;
extern long e;
extern long leftb, rightb, leftd, rightd, leftcycle, rightcycle;
extern long absolutetrack, absolutetrackl, absolutetrackr, basetrack;
extern long track, trackl, trackr;
extern long angle, baseangle, loopahead;
extern int gyro, gyro0, gyroswitch, ganglet, gangle, gdiff;
extern u16 angle_counter;
extern u8 gyro_adj;
extern long disf1, disf2, disf, disl, disr, disl45, disr45;
extern u8 lefthaswall, righthaswall, forwardhaswall;
extern u8 lefthaswall45, righthaswall45;
extern u8 leftwallcritical, rightwallcritical;
extern u8 sidewall_threshold, irswitch;
extern double battary;
extern u8 battarypercent;
extern u16 isemergency, emergency_delay;
/* stratagy.c: 迷宫状态/地图 */
extern MAZECOOR GmcMouse;
extern uchar GucMouseTask, GucMouseDir;
extern uchar GucXStart, GucYStart;
extern char GoalX, GoalY;
extern uchar GucMapBlock[MAZETYPE][MAZETYPE];
extern uchar GucMapGet[MAZETYPE][MAZETYPE];
extern uchar FastwayCount, FastwayCount_90;
/* stratagy.c: 冲刺测试/回坐标标志(未进 stratagy.h) */
extern u8 spurtTestFlag, ObjectGoTo_Flag;
/* test.c: 冲刺标志族 */
extern u8 GoOrBackFlag, goaheadnum;
extern int spurt_flag; /* test.c:121 定义为 int(非 u8!); 与定义同型, 否则 test.c 编译期冲突 */
/* timer.c: 直行/转弯 P/D 增益(未进 timer.h) */
extern double GOSTRAIGHT_kp, turn_kp, turn_kd;
/* utility.c: 冲刺档位 / 转弯延迟 */
extern u8 speedmode, turn_delay;

/* adc.c 的红外查表数组(link.c 的 link_IrRebuild 要用; adc.h 未导出它们) */
extern u16 dataf1[250], dataf2[250], datal[250], datar[250];
/* ledBeaconLock 影子变量(移植新增): 旧工程 led.c 的"基准灯独占"标志,
 * 新固件没有对应物。link.c 会写它(ARM 时置 1 / LINK_Init 清 0),
 * 新固件无人读它 —— 保留只为让 link.c 原样编译; 发车标记灯走
 * LP_LedMark 直写引脚, 不经过它。定义在 link_port.c(仅远程档)。 */
extern u8 ledBeaconLock;

/* sys.c 实现了整机软复位但 sys.h 没声明(新固件无人调用它)。
 * 声明放这里而不是改 sys.h —— 不动比赛固件的任何文件。 */
void Sys_Soft_Reset(void);

/*------------------------------------  协作式中止  ------------------------------------*/
/**
 * linkRunAbort —— "请退出当前运动/迷宫流程"的全局中止标志(远程档)
 *
 * 为什么需要它: 新固件所有运动原语都是**忙等**(mouseGoahead 的 while(1) 里等
 * track>=180、taskturn 里等 tick), 置一个"请退出"标志**永远不会被检查到**;
 * 而 freemode=1 虽能立刻停电机(timer.c 的 PWM 输出段), 却会让 while(track<180)
 * 因 track 不再增长而**永久挂死** —— 表现为"命令照回 OK, 车停了, 但程序再也
 * 不出来, 只能断电"(旧工程 2026-10-05 踩过的同一个坑)。
 *
 * 解法(分两级, 都在 SysTick 1ms 上下文):
 *   ① timer.c 的 SysTick 顶部(门控插入)见 linkRunAbort==1 就**强制满足退出条件**:
 *      task=NULL / track=0x7FFFFFFF / tick=60000 / setvl=setvr=0 => 所有底层忙等
 *      在这一拍立即返回;
 *   ② utility.c/test.c 的 6-8 处顶层 while(1) 另加 break 检查(它们不 poll track)。
 * 停车由 LP_StopMotion() 同步完成(freemode=1 => PWM 恒 0)。
 *
 * 生命周期: MAZE STOP / ESTOP 置 1(link.c 调 LP_AbortRequest); LINK_MazeStarted()
 * 清 0(可再次发车); MCU RESET 后自然清零。
 */
extern u8 linkRunAbort;
void LP_AbortRequest(void); /* 请求协作式中止(linkRunAbort=1) */
void LP_AbortClear(void);   /* 清除中止标志(再次发车前) */
u8 LP_AbortRequested(void); /* 读中止标志(timer.c 的 SysTick 顶部用) */

/*------------------------------------  镜像结构体  ------------------------------------*/
/* 以下类型/字段**只收 link.c 实际用到的部分**(逐一核对过 link.c 的引用), 不是旧固件
 * 结构体的完整拷贝 —— 目的是让远程档 RAM 增量最小化。字段名/类型与旧固件一致,
 * 故 link.c 的表达式一行都不用改。 */

typedef struct
{
    char cX; /* 当前坐标 X */
    char cY; /* 当前坐标 Y */
} LPMazeCoor;

typedef struct
{
    LPMazeCoor mousePos;      /* 当前坐标 <- GmcMouse */
    uchar mouseDirection;     /* 前进方向(UP/RIGHT/DOWN/LEFT=0/1/2/3) <- GucMouseDir */
    uchar mouseTask;          /* 任务状态机(WAIT/START/MAZESEARCH/SPURT.../END) <- GucMouseTask */
    uchar xStart;             /* 起点 X <- GucXStart */
    uchar yStart;             /* 起点 Y <- GucYStart */
    char goalX;               /* 目标 X <- GoalX */
    char goalY;               /* 目标 Y <- GoalY */
    uchar fastwayCount;       /* 路径动作计数 <- FastwayCount */
    uchar fastwayCount90;     /* 90 度路径动作计数 <- FastwayCount_90 */
    u8 spurtTestFlag;         /* 冲刺测试标志 <- spurtTestFlag */
    u8 objectGoToFlag;        /* 回坐标标志 <- ObjectGoTo_Flag */
    u8 stackIdx;              /* 岔路栈索引(新固件无对应全局, 恒 0) */
    uchar mapWall[MAZETYPE][MAZETYPE];    /* 墙信息图 <- GucMapBlock */
    uchar mapVisited[MAZETYPE][MAZETYPE]; /* 访问标记图 <- GucMapGet */
} MazeState;
extern MazeState mazeState;

typedef struct
{
    int rawf1, rawf2, rawl, rawr;                          /* 当前 ADC 原始值 <- adc.c */
    int rawf1avrgnear, rawf2avrgnear, rawlavrgnear, rawravrgnear; /* 标定平均-近 <- test.c */
    int rawf1avrgfar, rawf2avrgfar, rawlavrgfar, rawravrgfar;     /* 标定平均-远 <- test.c */
    u8 kalmanswitch; /* 卡尔曼开关 <- kalmanswitch */
    u8 calibMode;    /* 标定模式(1=滑动平均而非查表) <- biaodingmode */
} IrCalib;
extern IrCalib irCalib;

typedef struct
{
    double Qf1, Qf2, Ql, Qr; /* 过程噪声 <- adc.c */
    double Pf1, Pf2, Pl, Pr; /* 测量噪声 <- adc.c */
} KalmanState;
extern KalmanState kalmanSt;

typedef struct
{
    long f1;  /* 前左距离 <- disf1 */
    long f2;  /* 前右距离 <- disf2 */
    long f;   /* 前方综合 <- disf */
    long l;   /* 左侧距离 <- disl */
    long r;   /* 右侧距离 <- disr */
} IrDist;
extern IrDist irDist;

typedef struct
{
    u16 thr45;        /* 45 度阈值 mm(影子参数: 新固件用字面量 150) */
    u16 thrCritical;  /* 临界墙阈值 mm(影子参数: 字面量 60) */
    u16 thrFrontLo;   /* 前墙远距-低 mm(影子参数: 字面量 230) */
    u16 thrFrontMid;  /* 前墙远距-中 mm(影子参数: 字面量 240) */
    u16 thrFrontHi;   /* 前墙远距-高 mm(影子参数: 字面量 245) */
    u16 thrFrontNear; /* 前墙近距 mm(影子参数: 字面量 65) */
    u16 kalQ;         /* 卡尔曼 Q ×10000 -> Qf1/Qf2/Ql/Qr */
    u16 kalP;         /* 卡尔曼 P ×10000 -> Pf1/Pf2/Pl/Pr */
    u8 kalOn;         /* 卡尔曼开关 -> kalmanswitch */
    u8 frontMode;     /* 前向综合 0/1/2(影子参数: 新固件固定取均值) */
} IrCfg;
extern IrCfg irCfg;

typedef struct
{
    u16 gyroRatio;     /* 陀螺角度系数 <- gangleratio */
    u8 distToWall;     /* 车中心到墙距离 <- dis2wall */
    u16 encAngleRatio; /* 编码器角度系数 <- angleratio */
    float trackRatioL; /* 左轮距离系数 <- TrackRatioL */
    float trackRatioR; /* 右轮距离系数 <- TrackRatioR */
    double speedRatioL;/* 左轮速度系数 <- SpeedRatioL */
    double speedRatioR;/* 右轮速度系数 <- SpeedRatioR */
    u8 trackCoord;     /* 纵向矫正灵敏度(影子参数: 新固件 COORADJUST 未启用) */
} SysConfig;
extern SysConfig sysCfg;

typedef struct
{
    u16 search; /* 搜索直线速度 -> speed_ss */
    u16 turn;   /* 转弯速度 -> speed_turn */
} SpeedConfig;
extern SpeedConfig speedCfg;

typedef struct
{
    double goStraight; /* 直行 P 增益 -> GOSTRAIGHT_kp */
    double turnP;      /* 转弯 P 增益 -> turn_kp */
    double turnD;      /* 转弯 D 增益 -> turn_kd */
} PidGainConfig;
extern PidGainConfig pidGain;

typedef struct
{
    int speedL;        /* 左轮实时速度(cm/s) <- speedl */
    int speedR;        /* 右轮实时速度(cm/s) <- speedr */
    long speedTargetL; /* 左轮目标速度 -> setvl */
    long speedTargetR; /* 右轮目标速度 -> setvr */
    u8 freemode;       /* 自由模式(电机总开关) -> freemode */
    u8 sleepmode;      /* 睡眠模式 <- sleepmode */
} SpeedState;
extern SpeedState speedSt;

typedef struct
{
    int raw;             /* 陀螺 ADC 原始值 <- gyro */
    int bias;            /* 陀螺零点偏置 <- gyro0 */
    int angle;           /* 当前航向角 <- gangle */
    int angleAcc;        /* 角度积分累加器 <- ganglet */
    u8 adjFlag;          /* 零点自动校正使能 <- gyro_adj */
    u16 adjCounter;      /* 零点校正计数 <- angle_counter */
    u8 correctionEnable; /* 陀螺零漂校正使能 <- gyroswitch */
} GyroStruct;
extern GyroStruct gyroStr;

typedef struct
{
    long encL;       /* 左编码器捕获值 <- leftb */
    long encR;       /* 右编码器捕获值 <- rightb */
    long distL;      /* 左轮距离原始值 <- leftd */
    long distR;      /* 右轮距离原始值 <- rightd */
    long distLCycle; /* 左轮距离溢出计数 <- leftcycle */
    long distRCycle; /* 右轮距离溢出计数 <- rightcycle */
    long trackAbsL;  /* 左轮绝对里程 <- absolutetrackl */
    long trackAbsR;  /* 右轮绝对里程 <- absolutetrackr */
    long trackL;     /* 左轮相对里程 <- trackl */
    long trackR;     /* 右轮相对里程 <- trackr */
    long cellOdom;   /* 格内里程 <- track */
    long angle;      /* 编码器相对角度 <- angle */
    long angleBase;  /* 编码器角度基准 <- baseangle */
} OdomStruct;
extern OdomStruct odomStr;

typedef struct
{
    long pidErr; /* 当前 PID 误差 <- e */
    u16 tick;    /* ISR 节拍计数 <- tick */
    u8 task;     /* 运动状态机当前任务 <-> task(双向) */
    u8 step;     /* 运动子步骤 <- step */
} MotionCtrl;
extern MotionCtrl motionCtrl;

typedef struct
{
    u8 left;       /* 左侧有墙 <- lefthaswall */
    u8 right;      /* 右侧有墙 <- righthaswall */
    u8 front;      /* 前方有墙 <- forwardhaswall */
    u8 left45;     /* 左 45 度有墙 <- lefthaswall45 */
    u8 right45;    /* 右 45 度有墙 <- righthaswall45 */
    u8 critL;      /* 左临界墙 <- leftwallcritical */
    u8 critR;      /* 右临界墙 <- rightwallcritical */
    u8 irSwitch;   /* 红外传感器开关 <- irswitch */
    u8 wallThresh; /* 侧墙判定阈值 <-> sidewall_threshold(双向) */
} WallSensor;
extern WallSensor wallSns;

typedef struct
{
    u8 enabled;  /* 电压采样使能(影子参数) */
    double voltage; /* 当前电池电压 ×100 <- battary */
    u8 percent; /* 电量百分比 <- battarypercent */
    u16 adc;    /* 电池 ADC 原始值(新固件不缓存, 恒 0) */
    u16 mv;     /* 电压 mV(遥测 battMv 直接取它) = battary*10 */
    u8 low;     /* 低电标志(影子参数) */
} BattMonitor;
extern BattMonitor battMon;

typedef struct
{
    u32 battKv;     /* 分压系数 uV/LSB(影子参数: 新固件电池式为固定 3.3*3/4096*100) */
    u16 battLowMv;  /* 低电 mV(影子参数) */
    u16 battHighMv; /* 满电 mV(影子参数) */
    u16 battAlpha;  /* IIR 系数 ×1000(影子参数) */
    u8 battMaxPct;  /* 档位上限(影子参数) */
    u8 battMinPct;  /* 档位下限(影子参数) */
} PowerCfg;
extern PowerCfg powerCfg;

typedef struct
{
    u8 goaheadnum; /* 冲刺前行格数 <- goaheadnum */
    u8 spurtFlag;  /* 冲刺标志 <- spurt_flag */
    u8 goOrBack;   /* 去/回标志 <- GoOrBackFlag */
} TaskState;
extern TaskState taskSt;

typedef struct
{
    u16 active;  /* 急停触发标志 <- isemergency */
    u16 counter; /* 过流计数(影子参数) */
    u16 delay;   /* 急停延时计数 <- emergency_delay */
} EmergMonitor;
extern EmergMonitor emergMon;

/* 冲刺速度档(旧 spurtmode): 上位机按 60/61/62/63 报送, 与新固件 speedmode 同值 */
extern uchar spurtmode;
/* 转弯延迟格数(旧 motion.c 的 float) -> 新固件 turn_delay(u8, 取整写入) */
extern float turnDelay;

/* link.c 定义的参数宿主变量(id 43-55), LP_SyncOut 要把它们推给新固件 */
extern double linkPtKp, linkPtKi, linkPtKd;                 /* 原地转 P/I/D(影子参数) */
extern double linkMtrKpL, linkMtrKiL, linkMtrKdL;           /* 左轮电机 P/I/D -> kpl/kil/kdl */
extern double linkMtrKpR, linkMtrKiR, linkMtrKdR;           /* 右轮电机 P/I/D -> kpr/kir/kdr */
extern u8 linkDisF60;                                       /* 180° 前对正阈值(影子参数) */

/*------------------------------------  动作钩子  ------------------------------------*/
/**
 * LP_SyncIn —— 每 tick 把新固件的裸全局量刷新进 link.c 的只读镜像
 * 调用点: LINK_Tick() 开头(1ms, SysTick 上下文)
 * 方向: 新固件 -> 镜像(单向)。镜像中没有固件来源的字段(**影子参数**)不在此刷新,
 *       否则 SETQ 改的值会被下一秒的刷新打回默认。
 */
void LP_SyncIn(void);

/**
 * LP_SyncOut —— 把镜像中的可调参数/控制量推给新固件全局量
 * 调用点: LINK_Tick() 末尾(SETQ/SAVE/MOVE 之后, 同拍生效于下一 tick)
 * 方向: 镜像 -> 新固件(单向)。只推"有固件宿主"的字段(见 link_port.c 映射总表)。
 */
void LP_SyncOut(void);

/**
 * LP_SyncMap —— 按需把墙图/访问图拷进镜像(MAP DUMP 前调用)
 * 为什么不每 tick 拷: mapWall+mapVisited 共 512B, 每 tick 白拷约 28us;
 *                       MAP DUMP 是低频按需命令, 按需拷更省。
 */
void LP_SyncMap(void);

/**
 * LP_StopMotion —— 立即停电机并请求协作式中止
 * 动作: freemode=1(电机总开关, PWM 恒 0) + task=NULL + setvl=setvr=0 + linkRunAbort=1
 * 与 link.c 的 link_StopMotion() 分工: 后者清链路层状态机(镜像), 本函数动固件。
 */
void LP_StopMotion(void);

/** LP_SoftReset —— 整机软复位(MCU RESET 的最终执行者) */
void LP_SoftReset(void);

/**
 * LP_LedMark —— 发车标记灯(v2.8 语义): on=1 点亮 LED1-4, on=0 熄灭
 * 直接写引脚而不走 ledon()/ledoff(): 后者在 ledBeaconLock 置位时会被挡掉,
 * 而发车标记必须无论如何都亮(与旧工程 link.c 的同名逻辑一致)。
 */
void LP_LedMark(u8 on);

#endif /* _LINK_PORT_H_ */
