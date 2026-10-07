/**
 * @file    motion.h
 * @brief   低级运动控制接口
 * @author  赛曙开发组
 * @date    2026-07-28
 * @note    含设定/加减速/转弯(查 turnParamTable)/原地转/转弯前后姿态调整
 */
#ifndef _MOTION_H_
#define _MOTION_H_
#include "main.h"
#include "sys.h"

/* 运动/速度档位配置开关(原 config.h 迁入, 宏值不变) */
#define LOW_ENABLE    // 低速 所有转弯和直线都90

/* accdistance/decdistance 已封装入 searchSt (见 search.h SearchState);
   interCurveDist 改 motion.c static, 不再 extern */

extern float turnDelay;

void MOTION_SetTrack(int t);
void MOTION_SetAngle(int a);
void MOTION_Acc(void);
void MOTION_Stop(void);
void MOTION_GoStraight(u16 dis);
void MOTION_SlowDown(u16 dis);
void MOTION_SpeedUp(u16 dis);
void MOTION_Turn(u8 pattern);
void MOTION_TurnAround(void);
void MOTION_PointTurnRight(void);
void MOTION_PointTurnLeft(void);
void MOTION_Before90Turn(u8 Direction);
void MOTION_Before180Turn(void);
void MOTION_After180Turn(void);
void MOTION_Straight45(u16 dis);
void MOTION_Straight90(u16 dis);
void MOTION_MouseTurnRight(void);
void MOTION_MouseTurnLeft(void);
void MOTION_MouseTurnBack(void);
void MOTION_MouseTurnBackSpecial(void);
void MOTION_MouseGoahead(char cNBlock);
void MOTION_BeforeMouseTurn(u8 pattern);
void MOTION_MouseTurn(u8 pattern);

#endif
