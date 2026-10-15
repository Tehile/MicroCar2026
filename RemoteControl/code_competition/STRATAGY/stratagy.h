#ifndef __STRATAGY_H
#define __STRATAGY_H
#include "sys.h"

 
#define RIGHTMETHOD         0
#define LEFTMETHOD          1
#define CENTRALMETHOD       2
#define FRONTRIGHTMETHOD    3
#define FRONTLEFTMETHOD     4


 
#define UP                  0
#define RIGHT               1
#define DOWN                2
#define LEFT                3
#define CENTER               4


 
#define MOUSELEFT           0
#define MOUSEFRONT          1
#define MOUSERIGHT          2


 
#define MOUSEWAY_F          (1 <<   GucMouseDir)
#define MOUSEWAY_R          (1 << ((GucMouseDir + 1) % 4))
#define MOUSEWAY_B          (1 << ((GucMouseDir + 2) % 4))
#define MOUSEWAY_L          (1 << ((GucMouseDir + 3) % 4))


 
struct mazecoor
{
	char cX;
	char cY;
};
typedef struct mazecoor MAZECOOR;

struct fastwayaction
{
	char cx;
	char cy;
	char action;
};
typedef struct fastwayaction FASTWAYACTION;

 
typedef unsigned char  uchar;         
typedef unsigned short ushort;        
typedef unsigned int   uint;          

#define TRUE  1
#define FALSE 0
#define NULL  0


 
#define MAZETYPE        16                                                


 
#if MAZETYPE == 8  //8
#define XDST0           6
#define XDST1           7
#define YDST0           6
#define YDST1           7
#endif


 
#if MAZETYPE == 16
#define XDST0           7
#define XDST1           8
#define YDST0           7
#define YDST1           8
#endif


 
#define SEARCHMETHOD    CENTRALMETHOD                                      


 
#define MAXSPEED        150                                              
#define SEARCHSPEED     68                                               


 
#define ONEBLOCK        125

#define  WAIT           0                                                
#define  START          1                                                
#define  MAZESEARCH     2                                                
#define  SPURT          3                                                
#define  SPURT_45       4                                                
#define  SPURT_90       5      
#define  END						6



void mapStepEdit (char  cX, char  cY);
void mapStepEditC (void);
void mapStepEditS (char  X, char  Y);
void mapStepEditSC (void);
void mouseSpurt (void);
void objectGoTo (char  cXdst, char  cYdst);
uchar mazeBlockDataGet (uchar  ucDirTemp);
uchar mazeGetDataGet (uchar  ucDirTemp);
void rightMethod (void);
void leftMethod (void);
void frontRightMethod (void);
void frontLeftMethod (void);
void centralMethod (void);
uchar crosswayCheck (char  cX, char  cY);
void crosswayChoice (void);
void goalWallchange(char GoalX,char GoalY);
void backpointwaychoice(char cX,char cY);
uchar sStepGet (uchar  ucDirTemp);
void fastway(uchar GX,uchar GY);

void fastway_90(uchar GX,uchar GY);

uchar deadwaycheck(uchar cross);

void backPath(FASTWAYACTION *FastwayAction, uchar FastwayCount);

#endif
