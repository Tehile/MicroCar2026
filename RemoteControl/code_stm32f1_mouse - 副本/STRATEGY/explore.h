#ifndef _EXPLORE_H_
#define _EXPLORE_H_
#include "main.h"

void EXPLORE_WallFollowRight(void);
void EXPLORE_WallFollowRightLeft(void);
void EXPLORE_WallFollowLeft(void);
void EXPLORE_WallFollowLeftRight(void);
void EXPLORE_WallFollowFrontRight(void);
void EXPLORE_WallFollowFrontLeft(void);
void EXPLORE_CentralMethod(void);
uchar EXPLORE_CrosswayCheck(char cX, char cY);
void EXPLORE_CrosswayChoice(void);
void EXPLORE_GoalWallChange(char goalX, char goalY);
void EXPLORE_BackPointChoice(char cX, char cY);
uchar EXPLORE_StepGet(uchar ucDirTemp);
uchar EXPLORE_DeadwayCheck(uchar cross);

#endif
