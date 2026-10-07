#ifndef _SPURT_H_
#define _SPURT_H_

/* ---- 类型定义前置: SpurtSpeed_t/SpurtAction_t 等类型被 search.h 依赖,
 *     需在 include main.h 之前定义, 避免循环 include 导致类型未定义 ----
 */
/* ---- 冲刺速度/比例配置结构体(实例 spurtSpd 定义在 spurt.c) ---- */
typedef struct
{
    int l45TopSpeed;       /* 45度直行顶速 */
    int l45BottomSpeed;    /* 45度直行底速 */
    int objGoToTopSpd;     /* 回坐标顶速 */
    int objGoToBtmSpd;     /* 回坐标底速 */
    int lTopSpeed;         /* 斜线冲刺顶速 */
    int lBottomSpeed;      /* 斜线冲刺底速 */
    int l90TopSpeed;       /* 90度直线顶速 */
    int l90BottomSpeed;    /* 90度直线底速 */
    int backTopSpeed;      /* 返回顶速 */
    int backBottomSpeed;   /* 返回底速 */
    int topSpeed;          /* 当前顶速(运行时) */
    int bottomSpeed;       /* 当前底速(运行时) */
    float spurtAccRatio45; /* 45度加速比 */
    float spurtDecRatio45; /* 45度减速比 */
    float spurtAccRatio90; /* 90度加速比 */
    float spurtDecRatio90; /* 90度减速比 */
    float spurtAccRatio;   /* 当前加速比(运行时) */
    float spurtDecRatio;   /* 当前减速比(运行时) */
    int initialSpeed;      /* 起步速度 */
} SpurtSpeedConfig;
extern SpurtSpeedConfig spurtSpd;

/**
 * @brief 冲刺动作码(SPURT_ActionGenerate 生成, 存于 fastwayAction.action 域)
 * @note  动作码数值为约定常量, 被路径生成/switch/查表索引依赖, 勿改动。
 *        命名: 首字母 L/R=左/右; 次字母 T/V=转弯类型; 90/45/135/180=角度, STRAIGHT=直行; 末字母 L/M/H=速度档
 */
typedef enum
{
    /* === L 档（低速）1-16 === */
    LT90L = 1,
    RT90L = 2,
    LV90L = 3,
    RV90L = 4, /* 90度 */
    LT45L = 5,
    RT45L = 6,
    LV45L = 7,
    RV45L = 8, /* 45度 */
    LT135L = 9,
    RT135L = 10,
    LV135L = 11,
    RV135L = 12, /* 135度 */
    L180L = 13,
    R180L = 14, /* 180度 */
    STRAIGHT90L = 15,
    STRAIGHT45L = 16, /* 直行 */
    /* === M 档（中速）21-36 === */
    LT90M = 21,
    RT90M = 22,
    LV90M = 23,
    RV90M = 24,
    LT45M = 25,
    RT45M = 26,
    LV45M = 27,
    RV45M = 28,
    LT135M = 29,
    RT135M = 30,
    LV135M = 31,
    RV135M = 32,
    L180M = 33,
    R180M = 34,
    STRAIGHT90M = 35,
    STRAIGHT45M = 36,
    /* === H 档（高速）41-56 === */
    LT90H = 41,
    RT90H = 42,
    LV90H = 43,
    RV90H = 44,
    LT45H = 45,
    RT45H = 46,
    LV45H = 47,
    RV45H = 48,
    LT135H = 49,
    RT135H = 50,
    LV135H = 51,
    RV135H = 52,
    L180H = 53,
    R180H = 54,
    STRAIGHT90H = 55,
    STRAIGHT45H = 56
} SpurtAction_t;

/**
 * @brief 冲刺速度档位(speedmode 取值, SPURT_ActionGenerate/SPURT_GainSet 用)
 * @note  枚举数值为约定常量, 动作码生成/速度表索引/增益切换依赖 60/61/62/63, 勿改动
 */
typedef enum
{
    SPURT_SPEED_90_LOW = 60, /* 直线冲刺低档起步 */
    SPURT_SPEED_45_LOW = 61, /* 斜线冲刺低档起步 */
    SPURT_SPEED_MEDIUM = 62, /* 中速档 */
    SPURT_SPEED_HIGH = 63    /* 高速档 */
} SpurtSpeed_t;

// 冲刺直线校正方法
#define BOTHSIDE 80
#define LEFTSIDE 81
#define RIGHTSIDE 82
#define NOSIDE 83

#include "main.h"
#include "sys.h"

/* 冲刺速度档位配置开关 */
#define LOW_90_ENABLE
#define MEDIUM_ENABLE // 中速 90t,180t,45v--110; 45t,90v,135t,135v---90; 直线斜线均90(最高150)

void SPURT_Init(void); /* 冲刺速度/比例配置逐字段赋值 */
void SPURT_MouseSpurt(void);
void SPURT_ObjectGoTo(char cXdst, char cYdst);
void SPURT_GainSet(u8 mode);
void SPURT_GainSet90(u8 mode);
int SPURT_GetSum(int num);
int SPURT_GetFinalDir(void);
void SPURT_ActionGenerate(u8 mode);
void SPURT_Action(u8 mode);
void SPURT_ActionGenerate90(u8 mode);
void SPURT_Action90(u8 mode);
int SPURT_GetFinalDir90(void);
void SPURT_TurnRight(void);
void SPURT_TurnLeft(void);
void SPURT_BeforeTurnL(void);
void SPURT_BeforeTurnR(void);
void SPURT_BackTurn(void);

#endif
