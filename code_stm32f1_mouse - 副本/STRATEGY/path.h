#ifndef _PATH_H_
#define _PATH_H_
#include "main.h"

void PATH_Fastway8Dir(uchar GX, uchar GY);
void PATH_Fastway4Dir(uchar GX, uchar GY);
/* FastwayAction 完整定义在 maze.h(struct fastwayaction); 此处仅前置声明, 指针形参无需完整类型 */
struct fastwayaction;
void PATH_BackPath(struct fastwayaction *fastwayAction, uchar fastwayCount);

#endif
