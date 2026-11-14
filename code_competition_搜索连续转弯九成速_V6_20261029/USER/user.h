#ifndef __USER_H
#define __USER_H
#include "sys.h"



//搜索-连转90度
extern const short vouter_search[195];
extern const short vinner_search[195];
extern const short gyro_l_search[195];
extern const short gyro_r_search[195];

//搜索/冲刺-回转180度
extern const short vl_around[628];
extern const short vr_around[628];
extern const short gyro_around[628];

//搜索/冲刺-停转90度
extern const short gyro_l_point[232];
extern const short gyro_r_point[232];
extern const short vouter_point[232];
extern const short vinner_point[232];

//冲刺-连转90度
extern const short vouter_spurt[1];
extern const short vinner_spurt[1];
extern const short gyro_l_spurt[1];
extern const short gyro_r_spurt[1];



extern u16 speed_ss,speed_turn, speed_fastss;

#ifdef MOUSE_REMOTE
/* 远程档: 可被链路层在线改写, 与 user.c 的定义门控成对(见 §4.3 参数可变性) */
extern u16 angleratio;
extern u16 gangleratio;
extern float TrackRatioL;
extern float TrackRatioR;
extern double SpeedRatioL;
extern double SpeedRatioR;
extern u8 dis2wall;
#else
extern const u16 angleratio;
extern const u16 gangleratio;
extern const float TrackRatioL;
extern const float TrackRatioR;
extern const double SpeedRatioL;
extern const double SpeedRatioR;
extern const u8 dis2wall;
#endif



void parameter_search(void);
void motor_PID_init(void);
void parameter_spurt(void);

#endif




