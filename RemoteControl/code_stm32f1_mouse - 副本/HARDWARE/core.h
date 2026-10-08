#ifndef _CORE_H_
#define _CORE_H_
#include "main.h"
/*-----------------------------------------  I N C L U D E S  -----------------------------------------*/
#include "sys.h"

/*---------------------------------------  D E F I N I T I O N  ---------------------------------------*/

#define ACCV0 10 /* 加速起始速度(cm/s) */

/* 红外测距墙判定阈值 */
#define WALL_SIDE_THRESHOLD 160    /* 侧墙阈值: irDist.l/irDist.r 小于此值判定有墙 */
#define WALL_45_THRESHOLD 150      /* 45度侧墙阈值(斜线矫正用) */
#define WALL_CRITICAL_THRESHOLD 60 /* 临界墙阈值(斜线转弯起始点, 低于此值禁动) */
#define WALL_FRONT_FAR_LO 230      /* 前墙远距阈值-低(双传感器确认有墙) */
#define WALL_FRONT_FAR_MID 240     /* 前墙远距阈值-中(双传感器确认有墙) */
#define WALL_FRONT_FAR_HI 245      /* 前墙远距阈值-高(双传感器确认有墙) */
#define WALL_FRONT_NEAR 80         /* 前墙近距阈值(单传感器即判定有墙) */

/* 编码器/运动几何常量 */
#define ENCODER_ARR_VALUE 10000           /* 编码器定时器ARR值/计数量程(0~9999, 溢出回绕模数) */
#define ENCODER_WRAP_THRESHOLD 9000       /* 编码器前后差值越界判定阈值(90%量程, 超过判定为回绕) */
#define ENCODER_SPEED_SIGN_THRESHOLD 5000 /* 编码器速度正负号判定阈值(50%量程) */
#define DIAGONAL_HALF_CELL_STEPS 127      /* 45度斜行半格编码器步数(180/sqrt(2)) */

/* 速度PID增益(轮速闭环 kp/ki/kd) */
#define MOTOR_PID_KP_L 12.0  /* 左轮比例增益(初始值, freemode=0时覆盖为10.75) */
#define MOTOR_PID_KI_L 1.0   /* 左轮积分增益 */
#define MOTOR_PID_KD_L 0.75  /* 左轮微分增益 */
#define MOTOR_PID_KP_R 12.01 /* 右轮比例增益(初始值, freemode=0时覆盖为10.20) */
#define MOTOR_PID_KI_R 1.0   /* 右轮积分增益 */
#define MOTOR_PID_KD_R 0.75  /* 右轮微分增益 */

/* 姿态/转弯PD增益(角度误差闭环) */
#define TURN_PID_KP 2.5        /* 转弯比例增益(TURNAROUND/POINTTURN90共用) */
#define TURN_PID_KD 0.1        /* 转弯微分增益 */
#define TURN_PID_KI 0.01       /* 转弯积分增益(仅POINTTURN90用) */
#define ACC_PID_KP 2.5         /* 加速/停止比例增益(ACC/STOP状态用) */
#define ACC_PID_KD 1.6         /* 加速/停止微分增益(ACC/STOP状态用) */
#define STRAIGHT45_PID_KP 0.12 /* 45度直行比例增益 */
#define STRAIGHT45_PID_KD 0.1  /* 45度直行微分增益 */

/* 直行/转弯PD增益初始值(运行时可由 SPURT_GainSet 按档位覆盖) */
#define GOSTRAIGHT_KP_INIT 0.4                  /* 盲走直行P增益初值 */
#define SMARTSTRAIGHTFAST_BIGERR_KP_INIT 0.5    /* 智能直行(快速)大误差P增益初值 */
#define SMARTSTRAIGHTFAST_SMALLERR_KP_INIT 0.35 /* 智能直行(快速)小误差P增益初值 */
#define TURN_KP_INIT 5.0                        /* 转弯P增益初值(运行时被SPURT_GainSet覆盖) */
#define TURN_KD_INIT 4.5                        /* 转弯D增益初值(运行时被SPURT_GainSet覆盖) */

/* 电机PWM与方向引脚(位带操作, TIM4) */
#define RIGHTPWM TIM4->CCR3
#define LEFTPWM TIM4->CCR1
#define RIGHTDIR PBout(9)
#define LEFTDIR PBout(7)

/* 运动状态机任务码(motionTask 取值, SysTick 调度) */
typedef enum
{
    MOTION_TASK_NULL = 0, /* 空闲/禁用 */
    /* === 基础运动 1-18 === */
    GOSTRAIGHT = 1,       /* 直线行驶 */
    TURN = 2,             /* 转弯 */
    TURNAROUND = 4,       /* 180度掉头 */
    STOP = 5,             /* 停车 */
    SUPERSTRAIGHT = 12,   /* 超高速直线 */
    INFRSTRAIGHT = 13,    /* 红外对正直线 */
    TURNLEFTFAST = 14,    /* 快速左转 */
    TURNRIGHTFAST = 15,   /* 快速右转 */
    EMERGENCY = 16,       /* 急停 */
    STRAIGHT45 = 17,      /* 45度斜行 */
    FASTSTRAIGHT = 18,    /* 快速直线 */
    /* === 转弯前后 27-29 === */
    BEFORE90TURN = 27,    /* 90度转弯前对正 */
    BEFORE180TURN = 28,   /* 180度转弯前对正 */
    AFTER180TURN = 29,    /* 180度转弯后对正 */
    /* === 智能直行/变速 30-34 === */
    SMARTSTRAIGHT = 30,          /* 智能直线(贴墙矫正) */
    CHANGESPEED = 31,            /* 变速 */
    SMARTSTRAIGHTFAST = 32,      /* 智能直线(快速档) */
    STRAIGHTEYECLOSECOORADJ = 33,/* 闭眼直线坐标矫正 */
    STRAIGHTPOLE = 34,           /* 沿杆直线 */
    /* === 加减速/贴墙/点转/边线 35-41 === */
    ACC = 35,              /* 加速 */
    SLOWDOWN = 36,         /* 减速 */
    SPEEDUP = 37,          /* 提速 */
    KEEPWALLDISTANCE = 38, /* 保持贴墙距离 */
    POINTTURN90 = 39,      /* 原地90度点转 */
    EDGESTRAIGHT = 40,     /* 沿边线直线 */
    FORWARDIRTEST = 41,    /* 前向红外测试 */
    /* === 冲刺90/智能冲刺 42-45 === */
    SPURT90BLIND = 42,        /* 90度盲冲 */
    SPURT90IR = 43,           /* 90度红外矫正冲刺 */
    SMARTSTRAIGHTSPURT = 44,  /* 智能直线冲刺 */
    SMARTSTRAIGHTSPURTL = 45, /* 智能直线冲刺(贴左) */
    /* === 搜索/冲刺任务 101-107 === */
    LT90SEARCH = 101,   /* 左转90搜索 */
    RT90SEARCH = 102,   /* 右转90搜索 */
    POINTR90 = 103,     /* 原地右转90 */
    POINTL90 = 104,     /* 原地左转90 */
    STRAIGHT_spurt_90 = 105, /* 90直线冲刺 */
    LT90SPURT = 106,    /* 左转90冲刺 */
    RT90SPURT = 107     /* 右转90冲刺 */
} MotionTask_t;

/* ---- 编码器/里程计 ---- */
typedef struct
{
    long encL;       /* 左编码器捕获值 */
    long encR;       /* 右编码器捕获值 */
    long encLPrev;   /* 左编码器上周期值 */
    long encRPrev;   /* 右编码器上周期值 */
    long encWrapCnt; /* 编码器溢出回绕计数 */
    long angleAbs;   /* 编码器绝对角度 */
    long angleBase;  /* 编码器角度基准 */
    long distL;      /* 左轮距离原始值 */
    long distR;      /* 右轮距离原始值 */
    long distLPrev;  /* 左轮上周期距离 */
    long distRPrev;  /* 右轮上周期距离 */
    long distLCycle; /* 左轮距离溢出计数 */
    long distRCycle; /* 右轮距离溢出计数 */
    long trackAbs;   /* 绝对里程 */
    float trackAbsL;  /* 左轮绝对里程 */
    float trackAbsR;  /* 右轮绝对里程 */
    long trackBase;  /* 里程基准 */
    long trackBaseL; /* 左轮里程基准 */
    long trackBaseR; /* 右轮里程基准 */
    long trackL;     /* 左轮相对里程 */
    long trackR;     /* 右轮相对里程 */
    long cellOdom;   /* 格内里程(当前格已走距离) */
    long angle;      /* 编码器相对角度(绝对-基准) */
    long angleSkip;  /* 角度跳跃量 */
    long anglehis[50]; /* 编码器角度历史缓冲(循环) */
    long newindex;   /* anglehis 当前写下标 */
    long anglehismax;/* anglehis 窗口最大值 */
    long anglehismin;/* anglehis 窗口最小值 */
} OdomStruct;
extern volatile OdomStruct odomStr;

/* ---- 冲刺PID参数 ---- */
typedef struct
{
    float irKp1;  /* 90度冲刺红外P增益(误差大时) */
    float irKp2;  /* 90度冲刺红外P增益(误差小时) */
    float irKd;   /* 90度冲刺红外D增益 */
    float gyroKp; /* 90度冲刺陀螺P增益 */
    float gyroKd; /* 90度冲刺陀螺D增益 */
} SpurtConfig;
extern SpurtConfig spurtCfg;
extern u8 straightCells; /* 连续直线走过的格子数 */

/* ---- 运动PID控制 ---- */
typedef struct
{
    long pidErr;      /* 当前PID误差 */
    long pidErrPrev;  /* 上周期PID误差(微分用) */
    long pidErrInteg; /* PID误差积分 */
    u16 tick;         /* ISR节拍计数 */
    u8 task;          /* 运动状态机当前任务 */
    u8 step;          /* 运动子步骤 */
} MotionCtrl;
extern volatile MotionCtrl motionCtrl;

/* ---- 红外距离(ISR共享, 需volatile) ---- */
typedef struct
{
    long f1;       /* 前左传感器距离 */
    long f2;       /* 前右传感器距离 */
    long f;        /* 前方综合距离 */
    long l;        /* 左侧距离 */
    long r;        /* 右侧距离 */
    long l45;      /* 左45度距离 */
    long r45;      /* 右45度距离 */
    long lPrev;    /* 左侧上一周期 */
    long rPrev;    /* 右侧上一周期 */
    long lPrev2;   /* 左侧上上周期 */
    long rPrev2;   /* 右侧上上周期 */
    long l45Prev;  /* 左45度上一周期 */
    long r45Prev;  /* 右45度上一周期 */
    long l45Prev2; /* 左45度上上周期 */
    long r45Prev2; /* 右45度上上周期 */
} IrDist;
extern volatile IrDist irDist;

/* ---- 陀螺仪状态(ISR共享, 需volatile) ---- */
typedef struct
{
    int raw;             /* 陀螺ADC原始值 */
    int bias;            /* 陀螺零点偏置 */
    int diff;            /* 角速度差分(滤波后) */
    int diffPrev;        /* 上周期差分 */
    int angle;           /* 当前航向角 */
    int angleAcc;        /* 角度梯形积分累加器 */
    int angleShort;      /* 上周期角度(int型) */
    long anglePrev;      /* 上周期角度 */
    long anglePrev2;     /* 上上周期角度 */
    long anglePeak;      /* 角度峰值 */
    long anglePeakPrev;  /* 上周期峰值 */
    int initFlag;        /* 初始化完成标志 */
    int switchFlag;      /* 采集开关标志 */
    u8 adjFlag;          /* 零点自动校正使能 */
    int target;          /* 目标角度(转弯设定值) */
    u16 adjCounter;      /* 零点校正计数 */
    u8 correctionEnable; /* 陀螺零漂校正使能 */
} GyroStruct;
extern volatile GyroStruct gyroStr;

/* ---- 系统配置常量 ---- */
typedef struct
{
    u16 gyroRatio;      /* 陀螺角度系数 */
    u8 distToWall;      /* 车中心到墙距离 */
    u16 encAngleRatio;  /* 编码器角度系数 */
    float trackRatioL;  /* 左轮距离系数 */
    float trackRatioR;  /* 右轮距离系数 */
    double speedRatioL; /* 左轮速度系数 */
    double speedRatioR; /* 右轮速度系数 */
    u8 trackCoord;      /* 纵向矫正灵敏度 */
} SysConfig;
extern SysConfig sysCfg;

/* ---- 速度配置(非ISR共享) ---- */
typedef struct
{
    u16 search;     /* 搜索直线速度 */
    u16 turn;       /* 转弯速度 */
    u16 fastSprint; /* 冲刺最大速度 */
    int forwardIr;  /* 前向红外参考距离 */
} SpeedConfig;
extern SpeedConfig speedCfg;

/* ---- 运行时速度/电机PWM(ISR写主循环读, 需volatile) ---- */
typedef struct
{
    int speedL;         /* 左轮实时速度(cm/s) */
    int speedR;         /* 右轮实时速度(cm/s) */
    int avgSpeed;       /* 平均速度(cm/s) */
    long speedTargetL;  /* 左轮目标速度 */
    long speedTargetR;  /* 右轮目标速度 */
    double pwmMotorL;   /* 左轮PWM输出 */
    double pwmMotorR;   /* 右轮PWM输出 */
    int stoptick;       /* 停止计时 */
    u16 turnaroundTick; /* 180度转弯计时 */
    u8 freemode;        /* 自由模式标志 */
    u8 sleepmode;       /* 睡眠模式标志 */
} SpeedState;
extern volatile SpeedState speedSt;

/* ---- Flash数据保存(ISR读主循环写, 需volatile) ---- */
typedef struct
{
    u8 flashwrite;     /* 写Flash标志(1=写入) */
    u8 flashenable;    /* Flash保存使能 */
    u16 saveDataNum;   /* 已保存数据计数 */
    u8 saveDataTime;   /* 当前循环号 */
    u8 blockWriteFlag; /* 块写入标志 */
} FlashState;
extern volatile FlashState flashSt;

/* ---- PID增益配置(非ISR共享) ---- */
typedef struct
{
    double goStraight;   /* 直行P增益 */
    double smartFastBig; /* 智能直行大误差P增益 */
    double smartFastSm;  /* 智能直行小误差P增益 */
    double straight45P;  /* 45度直行P增益 */
    double straight45D;  /* 45度直行D增益 */
    double turnP;        /* 转弯P增益 */
    double turnD;        /* 转弯D增益 */
    double accRatio;     /* 加速比 */
    double decRatio;     /* 减速比 */
} PidGainConfig;
extern PidGainConfig pidGain;

/* ---- 转弯表指针(非ISR共享) ---- */
typedef struct
{
    const short *gyro; /* 前馈陀螺角度表 */
    const short *vl;   /* 左轮速度表 */
    const short *vr;   /* 右轮速度表 */
} TurnTable;
extern TurnTable turnTbl;

/* ---- 墙检测+红外控制(ISR共享, 需volatile) ---- */
typedef struct
{
    u8 left;        /* 左侧有墙 */
    u8 right;       /* 右侧有墙 */
    u8 front;       /* 前方有墙 */
    u8 leftPrev;    /* 左侧墙历史 */
    u8 rightPrev;   /* 右侧墙历史 */
    u8 left45;      /* 左45度有墙 */
    u8 right45;     /* 右45度有墙 */
    u8 left45Prev;  /* 左45度墙历史 */
    u8 right45Prev; /* 右45度墙历史 */
    u8 critL;       /* 左临界墙 */
    u8 critR;       /* 右临界墙 */
    u8 critL1;      /* 左临界墙历史1 */
    u8 critR1;      /* 右临界墙历史1 */
    u8 critL2;      /* 左临界墙历史2 */
    u8 critR2;      /* 右临界墙历史2 */
    u8 critL3;      /* 左临界墙历史3 */
    u8 critR3;      /* 右临界墙历史3 */
    u16 peakL;      /* 左侧峰值距离 */
    u16 peakR;      /* 右侧峰值距离 */
    u16 peakTrkL;   /* 左侧峰值里程 */
    u16 peakTrkR;   /* 右侧峰值里程 */
    u8 irSwitch;    /* 红外传感器开关 */
    u8 wallThresh;  /* 侧墙判定阈值 */
} WallSensor;
extern volatile WallSensor wallSns;

/* ---- 紧急/安全监控 ---- */
typedef struct
{
    u8 enabled;  /* 急停使能(1=开启) */
    u16 active;  /* 急停触发标志 */
    u16 counter; /* 过流计数 */
    u16 delay;   /* 急停延时计数 */
} EmergMonitor;
extern volatile EmergMonitor emergMon;

/* ---- 电池监控 ---- */
typedef struct
{
    u8 enabled;         /* 电压采样使能(1=开启) */
    double voltage;     /* 当前电池电压×100 */
    double voltagePrev; /* 上一周期电压 */
    u8 percent;         /* 电量百分比(0-8) */
} BattMonitor;
extern volatile BattMonitor battMon;

/* ---- CPU占用监控 ---- */
typedef struct
{
    int ticks;     /* 当前周期CPU计数 */
    int ticksPrev; /* 上一周期CPU计数 */
} CpuMonitor;
extern volatile CpuMonitor cpuMon;

/*-------------------------------------  F U N C T I O N S  -------------------------------------*/

void CORE_Init(void); /* 核心数据初始化: 速度/PID增益/墙检测配置(ISR_Init 前调用) */

#endif
