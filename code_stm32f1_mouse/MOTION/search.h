#ifndef _SEARCH_H_
#define _SEARCH_H_
#include "main.h"
#include "sys.h"

void SEARCH_Init(void); /* 搜索运动状态逐字段赋值 */
void SEARCH_Search(void);
void SEARCH_HasWall(void);
void SEARCH_NoWall(void);

#define Straight 0
#define TurnLeft 1
#define TurnRight 2

/**
 * @brief 搜索运动状态集合(实例 searchSt 定义在 search.c)
 * @note  每个动作执行一次走格所需的状态; accdistance/decdistance 为加速/减速距离
 */
typedef struct
{
    u8 lastSearchAct;       /* 上次搜索动作(初值Straight) */
    int turnRightOffset;    /* 右转偏移量: 墙距差换算, 用于MOTION_SetTrack转弯后补正 */
    int turnLeftOffset;     /* 左转偏移量: 墙距差换算 */
    int turnOffset;         /* 通用转弯偏移量: 墙距差换算 */
    u8 spurt45EndFlag;      /* 45度冲刺结束标志 */
    u8 spurtActionNum;      /* 冲刺动作计数 */
    u8 afterTurnSide;       /* 转弯后贴墙侧: 0=无墙不矫正, 1=左墙, 2=右墙 */
    u8 goalCorrectDir;      /* 终点矫正方向: 0=不矫正, 1=左矫正, 2=右矫正 */
    u8 back90Flag;          /* 后退90标志 */
    u8 lpeakfindenable;     /* 左峰值查找使能 */
    u8 rpeakfindenable;     /* 右峰值查找使能 */
    u8 skipCoorUpdate;      /* 跳过坐标更新 */
    SpurtSpeed_t speedmode; /* 冲刺速度档(执行层) */
    u16 accdistance;        /* 加速距离 */
    u16 decdistance;        /* 减速距离 */
} SearchState;
extern SearchState searchSt;

#endif
