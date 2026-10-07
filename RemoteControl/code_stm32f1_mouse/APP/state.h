#ifndef _STATE_H_
#define _STATE_H_
#include "main.h"

/* v2.3: `state.c` 的冲刺速度档(SpurtSpeed_t 取值, 0 表示未选)。
 * 由 static 改为全局**仅为可见性** —— 链路层组帧时只读镜像, 不写。 */
extern uchar spurtmode;

void STATE_InitSearchSm(void);
void STATE_DetermineGoaheadNum(void);
void STATE_LoadMapAndSelectSpeed(void);
void STATE_SpurtTest(void);
void STATE_ReturnSearch(void);
void STATE_MazeSearch(void);
void STATE_Spurt45(void);
void STATE_Spurt90(void);
void STATE_Start(void);
void STATE_Spurt(void);
void STATE_MainLoop(void);

#endif
