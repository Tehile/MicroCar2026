/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: lesson.h -- 课程应用实验
*********************************************************************************************************
*/

#ifndef _LESSON_H_
#define _LESSON_H_

#include "main.h"

/* ---- 速度采样缓冲区长度 ---- */
#define LESSON_DATA_LEN  100

/**
 * @brief 课程实验状态结构体
 * @note  成员类型/顺序经 lesson.o 的 DWARF 调试信息核对, 与原始定义一致
 */
typedef struct
{
    int   isTest;                          /* 实验运行标志(1=进行中) */
    int   speedSampleIdx;                  /* 速度采样缓冲区当前下标 */
    int   speedLeftSee[LESSON_DATA_LEN];   /* 左轮速度采样数组 */
    int   speedRightSee[LESSON_DATA_LEN];  /* 右轮速度采样数组 */
    int   allDis;                          /* 累计行驶距离 */
    int   blockCount;                      /* 前方障碍连续检测计数 */
    int   speedNeed;                       /* 目标速度 */
    int   irSwitch;                        /* 红外纠偏使能(1=开) */
    int   wallMode;                        /* 贴墙模式(1=左墙, 2=右墙) */
    long  irErrPrev;                       /* 红外误差(上周期) */
    long  irErr;                           /* 红外误差(当前) */
    float irLeftP;                         /* 左红外P增益 */
    float irRightP;                        /* 右红外P增益 */
    int   gyroSwitch;                      /* 陀螺仪纠偏使能(1=开) */
    long  gyroErrPrev;                     /* 陀螺误差(上周期) */
    long  gyroErr;                         /* 陀螺误差(当前) */
    float gyroP;                           /* 陀螺P增益 */
    float gyroD;                           /* 陀螺D增益 */
    u8    tempPos;                         /* 迷宫列遍历临时下标 */
    u8    roadStat;                        /* 岔路口计数 */
} LessonStruct;

/* ---- 课程实验状态实例(定义于 lesson.c, 零初始化) ---- */
extern LessonStruct lessonStr;

/**
 * @brief  实验初始化
 * @note   为课程代码运行配置环境
 */
void LESSON_Init(void);

/**
 * @brief  实验中断处理
 * @note   为课程代码设置后台运行程序
 */
void LESSON_ISR(void);

/**
 * @brief  实验1：电机驱动实验
 */
void LESSON_Motor1(void);

/**
 * @brief  实验2：编码器测距实验
 */
void LESSON_Enc2(void);

/**
 * @brief  实验3：匀变速直线控制
 */
void LESSON_Pid3(void);

/**
 * @brief  实验4：红外测距实验
 */
void LESSON_Irs4(void);

/**
 * @brief  实验5：标定与校准
 */
void LESSON_Cali5(void);

/**
 * @brief  实验6：多传感器校准与直行
 */
void LESSON_Multi6(void);

/**
 * @brief  实验7：转向控制实验
 */
void LESSON_Turn7(void);

/**
 * @brief  实验8：无记忆走迷宫
 */
void LESSON_Blind8(void);

#endif
