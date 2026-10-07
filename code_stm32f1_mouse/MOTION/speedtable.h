#ifndef _SPEEDTABLE_H_
#define _SPEEDTABLE_H_
#include "main.h"
#include "sys.h"

/* 速度档位配置开关 */
#define search_speed_70

/* 转弯速度查找表(定义于 speedtable.c): 前馈陀螺角度+左右轮速度, 391 点数组;
   表值为赛场整定数据, 勿改动。带尺寸 extern 供 motion.c 的 turnParamTable 用 sizeof 取长 */
extern const short vouterSpurt[182];
extern const short vinnerSpurt[182];
extern const short gyroLSpurt[182];
extern const short gyroRSpurt[182];
extern const short vouterSearch[227];
extern const short vinnerSearch[227];
extern const short gyroLSearch[227];
extern const short gyroRSearch[227];
extern const short gyroAround[267];
extern const short vlAround[267];
extern const short vrAround[267];
extern const short gyroLPoint[232];
extern const short gyroRPoint[232];
extern const short vouterPoint[232];
extern const short vinnerPoint[232];
extern const short vouterT90Low[195];
extern const short vinnerT90Low[195];
extern const short gyroLt90Low[195];
extern const short gyroRt90Low[195];
extern const short gyroLv90Low[187];
extern const short gyroRv90Low[187];
extern const short vouterV90Low[187];
extern const short vinnerV90Low[187];
extern const short gyroLt45Low[152];
extern const short gyroRt45Low[152];
extern const short vouterT45Low[152];
extern const short vinnerT45Low[152];
extern const short gyroLv45Low[171];
extern const short gyroRv45Low[171];
extern const short vouterV45Low[171];
extern const short vinnerV45Low[171];
extern const short gyroLt135Low[302];
extern const short gyroRt135Low[302];
extern const short vouterT135Low[302];
extern const short vinnerT135Low[302];
extern const short gyroLv135Low[294];
extern const short gyroRv135Low[294];
extern const short vouterV135Low[294];
extern const short vinnerV135Low[294];
extern const short vouter180Low[391];
extern const short vinner180Low[391];
extern const short gyroL180Low[391];
extern const short gyroR180Low[391];

#endif
