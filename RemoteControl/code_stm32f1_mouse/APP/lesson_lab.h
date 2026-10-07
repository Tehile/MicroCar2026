/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: lesson_lab.h —— 课程调参实验服务(实验代码与最终版代码分离的落点)
*       ------------------------------------------------------------------------------------------------
*               本模块**只在 MOUSE_LESSON_LAB 下参与编译**(见 USER/main.h 的模式开关)。
*               正式比赛固件里它一个字节都不占 —— 这就是"实验代码与最终版代码分开"。
*
*               它给上位机提供两样东西:
*                 1) 一组可运行时改的**关键调参量**(电机 PID / 红外与陀螺增益 / 目标速度与距离 /
*                    传感器方案), 由链路层 SETQ 下发、GET 回读、SAVE 落到独立 Flash 页;
*                 2) 一个**实验引擎**: `LESSON START <exp>` 启动, 固件按实验剖面驱动电机,
*                    结束时自动停车并回 `LESSON DONE ...`。
*
*               与 APP/lesson.c 的分工:
*                 lesson.c  —— **原始课件实现**(按键驱动、printf 回传), 保持原样供对照学习;
*                 lesson_lab.c —— **上位机驱动的可调参版本**, 同样的物理量, 改由命令控制。
*               两者互不影响: lesson.c 的函数在测试模式下不会被调用(见 isr.c)。
*********************************************************************************************************
*/

#ifndef _LESSON_LAB_H_
#define _LESSON_LAB_H_

#include "main.h"

#ifdef MOUSE_LESSON_LAB

/*---------------------------------------  D E F I N I T I O N  ---------------------------------------*/

/* ---- 课程调参量 id ----
 * 与上位机 host/host/protocol/commands.py 的 LESSON_PARAM_IDS **必须一致**。
 * 刻意从 8 开始: 1-7 是生产标定参数(LINK_P_*), 两者命名空间不重叠。 */
#define LL_P_KPL 8        /* 左轮速度环 Kp (默认 = 红线值 MOTOR_KP_L) */
#define LL_P_KIL 9        /* 左轮速度环 Ki */
#define LL_P_KDL 10       /* 左轮速度环 Kd */
#define LL_P_KPR 11       /* 右轮速度环 Kp */
#define LL_P_KIR 12       /* 右轮速度环 Ki */
#define LL_P_KDR 13       /* 右轮速度环 Kd */
#define LL_P_IRPL 14      /* 红外纠偏 左轮 P (lessonStr.irLeftP) */
#define LL_P_IRPR 15      /* 红外纠偏 右轮 P (lessonStr.irRightP) */
#define LL_P_GYROP 16     /* 陀螺纠偏 P (lessonStr.gyroP) */
#define LL_P_GYROD 17     /* 陀螺纠偏 D (lessonStr.gyroD) */
#define LL_P_SPEEDNEED 18 /* 实验目标速度 cm/s (lessonStr.speedNeed) */
#define LL_P_ALLDIS 19    /* 实验目标距离 mm (lessonStr.allDis) */
#define LL_P_SENSORMODE 20 /* 传感器方案: 0=全关 1=仅红外 2=仅陀螺 3=红外+陀螺 */
#define LL_P_WALLMODE 21  /* 贴墙方向: 1=靠左 2=靠右 */
#define LL_P_NUM 14       /* 课程调参量个数 */

/* ---- 传感器方案(与课件 multi_sensor 的 KEINE/INFRAROT/GYROSKOP/ALLE 对应) ---- */
#define LL_SENSOR_NONE 0
#define LL_SENSOR_IR 1
#define LL_SENSOR_GYRO 2
#define LL_SENSOR_BOTH 3

/* ---- 实验编号(与课件一致, 便于对照) ---- */
#define LL_EXP_NONE 0
#define LL_EXP_ENCODER 2 /* 实验三  编码器测距        */
#define LL_EXP_PID 3     /* 实验四  匀变速直线控制    */
#define LL_EXP_IR 4      /* 实验五  红外标定(用既有 CAL IRDATA) */
#define LL_EXP_GYRO 5    /* 实验六  陀螺仪标定与校准  */
#define LL_EXP_MULTI 6   /* 实验七  多传感器校准直行  */
#define LL_EXP_TURN 7    /* 实验八  转向控制(预留)    */

/* ---- 实验结束原因(与链路事件行 `STOPPED <REASON> #n` 的 REASON 风格一致) ---- */
#define LL_REASON_DONE "DONE"   /* 剖面走完(正常结束) */
#define LL_REASON_TIME "TIME"   /* 时间护栏到时 */
#define LL_REASON_DIST "DIST"   /* 到达目标里程 */
#define LL_REASON_ANGLE "ANGLE" /* 到达目标角度 */
#define LL_REASON_IR "IR"       /* 前向红外触发(撞墙保护) */
#define LL_REASON_ESTOP "ESTOP" /* 急停 */
#define LL_REASON_STOP "STOP"   /* 上位机主动停止 */

/* ---- 启动/停止返回码 ---- */
#define LL_OK 0
#define LL_ERR_BUSY 1  /* 已有实验/运动在跑 */
#define LL_ERR_VALUE 2 /* 参数非法 */
#define LL_ERR_ESTOP 3 /* 急停锁存中, 须先 STOP 解锁 */
#define LL_ERR_EXP 4   /* 未知实验号 */

/* ---- PID 剖面实验(实验四)的阶段 ---- */
#define LL_PH_IDLE 0
#define LL_PH_ACC 1
#define LL_PH_CRUISE 2
#define LL_PH_DEC 3
#define LL_PH_STOP 4

/*----------------------------------------  T Y P E S  ----------------------------------------*/

/**
 * @brief 课程调参量(运行时可改)
 * @note  电机六增益的初值**严格等于 motor.h 的红线常量** —— 只是变得可改, 数值未被改动。
 *        未进入本模式时这些字段根本不存在, 生产固件仍直接用 MOTOR_KP_L 等字面量。
 */
typedef struct
{
    float kpL, kiL, kdL; /* 左轮速度环 PID */
    float kpR, kiR, kdR; /* 右轮速度环 PID */
} LessonGain;

/**
 * @brief 课程实验引擎状态
 */
typedef struct
{
    u16 exp;       /* 当前实验号(LL_EXP_*) */
    u16 phase;     /* 阶段(LL_PH_*) */
    u16 elapsed;   /* 已运行毫秒 */
    u16 limit;     /* 时间护栏(ms) */
    u8 running;    /* 1=实验中 */
    u8 doneFlag;   /* 1=有未上报的 DONE 行 */
    s32 vCruise;   /* 巡航速度 cm/s */
    u16 tAcc;      /* 加速段时长 ms */
    u16 tCruise;   /* 匀速段时长 ms */
    u16 tDec;      /* 减速段时长 ms */
    s32 distTarget; /* 目标里程 mm(实验三/七) */
    s32 angleTarget; /* 目标角度 度(实验六) */
    s32 vStartL, vStartR; /* 起始里程快照(实验三/七) */
    s32 dL, dR;    /* 实测位移(脉冲) */
    s32 vPeakL, vPeakR; /* 实测速度峰值 cm/s */
    s32 angFinal;  /* 结束时航向角 */
    const char *reason; /* 结束原因(LL_REASON_*) */
} LessonLabState;

/*---------------------------------------  V A R I A B L E S  ---------------------------------------*/
extern LessonGain lessonGain;   /* 课程可调增益 */
extern LessonLabState lessonLab; /* 实验引擎状态 */

/*-------------------------------------  F U N C T I O N S  -------------------------------------*/

/** @brief 初始化: 把增益初值置为红线常量, 引擎置空闲(LINK_Init 内调用) */
void LESSONLAB_Init(void);

/** @brief 1ms 引擎: 由 LINK_Tick() 调用(受 sleepmode / 急停约束) */
void LESSONLAB_Tick(void);

/**
 * @brief  启动一个实验
 * @param  exp 实验号(LL_EXP_*)
 * @param  a,b,c,d 实验参数(含义随实验而定, 见 lesson_lab.c)
 * @return LL_OK / LL_ERR_*
 */
u8 LESSONLAB_Start(u16 exp, s32 a, s32 b, s32 c, s32 d);

/** @brief 主动停止实验(置 freemode, 回目标速度归零) */
u8 LESSONLAB_Stop(void);

/**
 * @brief  由链路层在 STOP / ESTOP 时调用, 带上真正的原因
 * @param  reason LL_REASON_STOP / LL_REASON_ESTOP
 */
void LESSONLAB_Abort(const char *reason);

/** @brief 是否有实验在跑 */
u8 LESSONLAB_Running(void);

/** @brief 当前实验号 */
u16 LESSONLAB_Exp(void);

/**
 * @brief  取走一条待上报的 DONE 行(形如 "LESSON DONE 3 DONE 4200 ...", 不含 CRLF)
 * @return 1=已填充 buf, 0=无待报内容
 */
u8 LESSONLAB_TakeDone(char *buf, u16 cap);

/** @brief 课程调参量读(值以 Q16.16 表达, 与链路层一致) */
u8 LESSONLAB_ParamGet(u16 id, s32 *q);

/** @brief 课程调参量写(Q16.16) */
u8 LESSONLAB_ParamSet(u16 id, s32 q);

/** @brief 该 id 是否属于课程调参量 */
u8 LESSONLAB_ParamIdIs(u16 id);

#endif /* MOUSE_LESSON_LAB */

#endif
