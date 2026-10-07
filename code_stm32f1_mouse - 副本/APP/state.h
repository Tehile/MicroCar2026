#ifndef _STATE_H_
#define _STATE_H_
#include "main.h"

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
