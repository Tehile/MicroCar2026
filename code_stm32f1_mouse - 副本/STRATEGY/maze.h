#ifndef _MAZE_H_
#define _MAZE_H_
#include "main.h"
/*-----------------------------------------  I N C L U D E S  -----------------------------------------*/
#include "sys.h"
/*---------------------------------------  D E F I N I T I O N  ---------------------------------------*/

/* 常量宏定义 -- 搜索法则 */
#define RIGHTMETHOD 0
#define LEFTMETHOD 1
#define CENTRALMETHOD 2
#define FRONTRIGHTMETHOD 3
#define FRONTLEFTMETHOD 4

/* 常量宏定义 -- 电脑鼠运行绝对方向 */
#define UP 0
#define RIGHT 1
#define DOWN 2
#define LEFT 3

/* 常量宏定义 -- 电脑鼠运行相对方向 */
#define MOUSELEFT 0
#define MOUSEFRONT 1
#define MOUSERIGHT 2

/* 常量宏定义 -- 用绝对方向表示相对方向上的资料表位置(带状态宏)
   注意: F=前/R=右/B=后/L=左(相对鼠朝向), 值=(1 << ((mouseDirection+偏移)%4)),
   调用时 mouseDirection 须已正确设置, 朝向不同结果不同; 绝对墙壁位见 WALL_UP_OPEN 等(0x01..0x08) */
#define MOUSEWAY_F (1 << mazeState.mouseDirection)
#define MOUSEWAY_R (1 << ((mazeState.mouseDirection + 1) % 4))
#define MOUSEWAY_B (1 << ((mazeState.mouseDirection + 2) % 4))
#define MOUSEWAY_L (1 << ((mazeState.mouseDirection + 3) % 4))

/* 墙壁位编码宏
 * mapWall[x][y] 字节编码: 低四位=本格通行(bit0=上,1=右,2=下,3=左, 1=无墙/0=有墙),
 *                        高四位=镜像格(邻居同步)。详见 MAZE_WallCheck。数值不可更改
 *
 * 双地图语义: 未访问格 低四位=0(全闭图: 未知按有墙, 已探明路径上界),
 *            高四位=0xF(全开图: 未知按无墙, 理论最短下界);
 *            两图在起点相等(mapStepClose[起点]-mapStepOpen[起点]==0)即已找到最优解
 */
#define WALL_UP_OPEN 0x01      /* 本格-上 无墙 */
#define WALL_RIGHT_OPEN 0x02   /* 本格-右 无墙 */
#define WALL_DOWN_OPEN 0x04    /* 本格-下 无墙 */
#define WALL_LEFT_OPEN 0x08    /* 本格-左 无墙 */
#define WALL_LOW_NIBBLE 0x0F   /* 本格全部方向掩码 */
#define WALL_UP_MIRROR 0x10    /* 镜像格-上 */
#define WALL_RIGHT_MIRROR 0x20 /* 镜像格-右 */
#define WALL_DOWN_MIRROR 0x40  /* 镜像格-下 */
#define WALL_LEFT_MIRROR 0x80  /* 镜像格-左 */

/* 定义结构体类型 */
struct mazecoor
{
  char cX; /* X坐标 */
  char cY; /* Y坐标 */
};
typedef struct mazecoor MazeCoor;

struct fastwayaction
{
  char cx; /* 目标X坐标 */
  char cy; /* 目标Y坐标 */
  char action; /* 动作编码(8方向/4方向共用) */
};
typedef struct fastwayaction FastwayAction;

/* 常量宏定义 -- 迷宫类型 */
#define MAZETYPE 7 /*  8: 四分之一迷宫；16: 全迷宫 */

#if MAZETYPE == 7
#define XDST0 6
#define XDST1 6
#define YDST0 6
#define YDST1 6
#endif

/* 常量宏定义 -- 四分之一迷宫终点坐标 */
#if MAZETYPE == 8
#define XDST0 6
#define XDST1 7
#define YDST0 6
#define YDST1 7
#endif

/* 常量宏定义 -- 全迷宫终点坐标 */
#if MAZETYPE == 16
#define XDST0 7
#define XDST1 8
#define YDST0 7
#define YDST1 8
#endif

/* 常量宏定义 -- 迷宫搜索法则(RIGHT/LEFT/CENTRAL/FRONTRIGHT/FRONTLEFT: 右/左/中心/中右/中左) */
#define SEARCHMETHOD CENTRALMETHOD /* 设置迷宫搜索法则为中心法则 */

/* 常量宏定义 -- 电机运行速度 */
#define MAXSPEED 150   /* 全速上限(编码器步/控制周期≈PWM占空比‰; 冲刺高挡/急加速用, 对应约1.5m/s) */
#define SEARCHSPEED 68 /* 搜索巡航速度(迷宫探索与低速转弯用, 对应约0.68m/s) */

/* 常量宏定义 -- 前进一个迷宫格电机所需步数 */
#define ONEBLOCK 125 /* 单格直行编码器目标步数(180mm/格÷编码器分辨率≈1.44mm/步) */

#define WAIT 0       /*  等待状态                    */
#define START 1      /*  启动状态                    */
#define MAZESEARCH 2 /*  搜寻状态                    */
#define SPURT 3      /*  冲刺状态                    */
#define SPURT_45 4   /*  斜线冲刺状态                */
#define SPURT_90 5   /*  垂直冲刺状态                */

/* ---- extern 声明 ---- */
/* ---- 迷宫状态(定义在 maze.c) ---- */
typedef struct
{
  MazeCoor mousePos;    /* 当前坐标: .cX横, .cY纵 */
  uchar mouseDirection; /* 前进方向(UP/RIGHT/DOWN/LEFT=0/1/2/3) */
  uchar mouseTask;      /* 任务状态机(WAIT/START/MAZESEARCH/SPURT...) */
  uchar xStart;         /* 起点X */
  uchar yStart;         /* 起点Y */
  uchar xGoal0;         /* 终点X(值1) */
  uchar xGoal1;         /* 终点X(值2) */
  uchar yGoal0;         /* 终点Y(值1) */
  uchar yGoal1;         /* 终点Y(值2) */
  char goalX;           /* 目标X(初值7) */
  char goalY;           /* 目标Y(初值7) */
  uchar fastwayCount;   /* 路径动作计数 */
  uchar fastwayCount90; /* 90度路径动作计数 */
  u8 spurtTestFlag;     /* 冲刺测试标志 */
  u8 objectGoToFlag;    /* 回坐标标志 */
  /* ---- 迷宫数组 ---- */
  uchar mapWall[MAZETYPE][MAZETYPE];                  /* 墙信息图(低四位本格,高四位镜像格) */
  uchar mapVisited[MAZETYPE][MAZETYPE];               /* 访问标记图 */
  uchar mapFastway[MAZETYPE][MAZETYPE];               /* 8方向路径方向图 */
  uchar mapFastway90[MAZETYPE][MAZETYPE];             /* 4方向路径方向图 */
  uchar mapStepClose[MAZETYPE][MAZETYPE];               /* 全闭图距离图 */
  uchar mapStepCloseCopy[MAZETYPE][MAZETYPE];           /* 全闭图距离图副本 */
  uchar mapStepOpen[MAZETYPE][MAZETYPE];               /* 全开图距离图 */
  uchar mapStepOpenCopy[MAZETYPE][MAZETYPE];           /* 全开图距离图副本 */
  MazeCoor stackClose[MAZETYPE * MAZETYPE];             /* 全闭图填充栈 */
  MazeCoor stackOpenNow[MAZETYPE * MAZETYPE];          /* 全开图当前层栈 */
  MazeCoor stackOpenNext[MAZETYPE * MAZETYPE];         /* 全开图下一层栈 */
  MazeCoor crosswayBuf[MAZETYPE * MAZETYPE];          /* 支路坐标缓冲 */
  FastwayAction fastwayAction[MAZETYPE * MAZETYPE];   /* 路径动作序列 */
  FastwayAction fastwayAction90[MAZETYPE * MAZETYPE]; /* 90度路径动作序列 */
  uchar fastwayActionTemp[MAZETYPE * MAZETYPE];       /* 路径反转缓冲 */
  uchar spurtTestAction[200];                         /* 冲刺测试动作序列 */
  u8 stackIdx;                                        /* 岔路栈索引 */
} MazeState;
extern MazeState mazeState;

/*-------------------------------------  F U N C T I O N S  -------------------------------------*/
uchar MAZE_WallDataGet(uchar ucDirTemp);
uchar MAZE_VisitedDataGet(uchar ucDirTemp);
void MAZE_Init(void);
void MAZE_CoorUpdate(void);
void MAZE_CoorUpdateFast(u8 num);
void MAZE_WallCheck(void);
u8 MAZE_BreakOrNot(void);

#endif
