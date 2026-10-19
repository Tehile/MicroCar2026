#include <stm32f10x.h>
#include "sys.h"
#include "usart.h"		
#include "delay.h"	
#include "timer.h"
#include "adc.h"
#include "utility.h"
#include "stratagy.h"
#include "led.h"
#ifdef MOUSE_REMOTE
#include "link_port.h" /* P2: 协作式中止查询 LP_AbortRequested() */
#endif

 
 
u8 spurtTestFlag=0;
uchar spurtTestAction[200]={STRAIGHT90L,STRAIGHT90L,STRAIGHT90L,R180L}; 
uchar SpurtTest_FastwayAction[MAZETYPE * MAZETYPE]={0};
uchar FastwayCount; 
 

 
 
uchar    GucXStart                           = 0;                 
uchar    GucYStart                           = 0;                 

uchar    GucXGoal0                           = XDST0;             
uchar    GucXGoal1                           = XDST1;
uchar    GucYGoal0                           = YDST0;             
uchar    GucYGoal1                           = YDST1;

uchar    GucMouseTask                        = WAIT;              

char GoalX=7;
char GoalY=7;

uchar    GucMapStep[MAZETYPE][MAZETYPE]      = {0xff};            
uchar    GucMapStepC[MAZETYPE][MAZETYPE]      = {0xff};            
uchar    GucMapStepS[MAZETYPE][MAZETYPE]      = {0xff};            
uchar    GucMapStepSC[MAZETYPE][MAZETYPE]      = {0xff};            

MAZECOOR GmcStack[MAZETYPE * MAZETYPE]       = {0};               
MAZECOOR GmcStackS1[MAZETYPE * MAZETYPE]       = {0}; 
MAZECOOR GmcStackS2[MAZETYPE * MAZETYPE]       = {0}; 
MAZECOOR GmcCrossway[MAZETYPE * MAZETYPE]    = {0};               

MAZECOOR GmcMouse= {0,0};                                                
                                                                         
                                                                        
uchar    GucMouseDir=UP;                                             
uchar    GucMapBlock[MAZETYPE][MAZETYPE]={0};                       
                                                                         
                                                                        
                                                                        
uchar    GucMapGet[MAZETYPE][MAZETYPE]={0}; 
#ifndef MOUSE_REMOTE
extern int setvl,setvr;
#endif
extern u8 task;
extern uchar    GucMouseTask;

uchar	Mapfastway[MAZETYPE][MAZETYPE]= {0xff};

 
FASTWAYACTION	FastwayAction[MAZETYPE * MAZETYPE]={0}; 
 


uchar	Mapfastway_90[MAZETYPE][MAZETYPE]= {0xff};
FASTWAYACTION	FastwayAction_90[MAZETYPE * MAZETYPE]={0}; 
uchar	FastwayCount_90; 

uchar	FastwayActionTemp[MAZETYPE * MAZETYPE];
u8 ObjectGoTo_Flag=0;

void stop()
{
	task=NULL;GucMouseTask=99;setvl=0;setvr=0;
#ifdef MOUSE_REMOTE
	/* P2: 中止来自 MAZE STOP/ESTOP 时不堕入死循环, 返回让调用方逐层 unwind */
	if (LP_AbortRequested()) return;
#endif
	while(1);
}

																	   
 
void mapStepEdit (char  cX, char  cY)//洪水填充算法实现  步数地图生成
{
    uchar n         = 0;                                                 
    uchar ucStep    = 0;                                                 
    uchar ucStat    = 0;                                                 
    uchar i,j;
    
    GmcStack[n].cX  = cX;                                                
    GmcStack[n].cY  = cY;                                                
    n++;
     
    for (i = 0; i < MAZETYPE; i++)
	{
        for (j = 0; j < MAZETYPE; j++)
		{
            GucMapStep[i][j] = 0xff;
        }
    }
     
    while (n)
	{
        GucMapStep[cX][cY] = ucStep++;                                   

         
        ucStat = 0;
        if ((GucMapBlock[cX][cY] & 0x01) &&                              
            (GucMapStep[cX][cY + 1] > (ucStep))) {                       
            ucStat++; // 上右下左                                                   
        }
        if ((GucMapBlock[cX][cY] & 0x02) &&                              
            (GucMapStep[cX + 1][cY] > (ucStep))) {                       
            ucStat++;                                                    
        }
        if ((GucMapBlock[cX][cY] & 0x04) &&
            (GucMapStep[cX][cY - 1] > (ucStep))) {
            ucStat++;                                                    
        }
        if ((GucMapBlock[cX][cY] & 0x08) &&
            (GucMapStep[cX - 1][cY] > (ucStep))) {
            ucStat++;                                                    
        }
         
        if (ucStat == 0) {
          //回溯 如果是死路 
					n--;
            cX = GmcStack[n].cX;
            cY = GmcStack[n].cY;
            ucStep = GucMapStep[cX][cY];
        } else {
					  //岔路口保存
            if (ucStat > 1) {                                            
                GmcStack[n].cX = cX;                                     
                GmcStack[n].cY = cY;                                     
                n++;
            }
             
            if ((GucMapBlock[cX][cY] & 0x01) &&                          
                (GucMapStep[cX][cY + 1] > (ucStep))) {                   
                cY++;                                                    
                continue;
            }
            if ((GucMapBlock[cX][cY] & 0x02) &&                          
                (GucMapStep[cX + 1][cY] > (ucStep))) {                   
                cX++;                                                    
                continue;
            }
            if ((GucMapBlock[cX][cY] & 0x04) &&                          
                (GucMapStep[cX][cY - 1] > (ucStep))) {                   
                cY--;                                                    
                continue;
            }
            if ((GucMapBlock[cX][cY] & 0x08) &&                          
                (GucMapStep[cX - 1][cY] > (ucStep))) {                   
                cX--;                                                    
                continue;
            }
        }
    }
}
//虚拟
void mapStepEditS (char  X, char  Y)
{
	uchar n         = 0;
	uchar m         = 0;
	uchar ucStep    = 0;
	uchar i,j;
	char cX=X;
	char cY=Y;
		
	for (i = 0; i < MAZETYPE; i++)
	{
        for (j = 0; j < MAZETYPE; j++) 
		{
            GucMapStepS[i][j] = 0xff;
        }
    }
	
	GucMapStepS[cX][cY]=ucStep;
	
	GmcStackS1[n].cX  = cX;                                                
    GmcStackS1[n].cY  = cY;
	n++;
	
	while(n)
	{
		ucStep++;
		while(n)
		{
			cX=GmcStackS1[n-1].cX;
			cY=GmcStackS1[n-1].cY;
			if( cY < MAZETYPE-1 ) 
			{
				if ((GucMapBlock[cX][cY] & 0x10) &&                              
					(GucMapStepS[cX][cY + 1] > (ucStep))) {                       
					GucMapStepS[cX][cY + 1]=ucStep;
					GmcStackS2[m].cX  = cX;
					GmcStackS2[m].cY  = cY+1;
					m++;
				}
			}
			if( cX < MAZETYPE-1 )
			{
				if ((GucMapBlock[cX][cY] & 0x20) &&                              
					(GucMapStepS[cX + 1][cY] > (ucStep))) {                       
					GucMapStepS[cX + 1][cY]=ucStep;
					GmcStackS2[m].cX  = cX+1;
					GmcStackS2[m].cY  = cY;
					m++;
				}
			}
			if( cY >0 )
			{
				if ((GucMapBlock[cX][cY] & 0x40) &&
					(GucMapStepS[cX][cY - 1] > (ucStep))) {
					GucMapStepS[cX][cY - 1]=ucStep;
					GmcStackS2[m].cX  = cX;
					GmcStackS2[m].cY  = cY-1;
					m++;
				}
			}
			if( cX >0 )
			{
				if ((GucMapBlock[cX][cY] & 0x80) &&
					(GucMapStepS[cX - 1][cY] > (ucStep))) {
					GucMapStepS[cX - 1][cY]=ucStep;
					GmcStackS2[m].cX  = cX-1;
					GmcStackS2[m].cY  = cY;
					m++;
				}
			}
			n--;
		}
		for(i=0;i<m;i++)
		{
			GmcStackS1[i].cX  = GmcStackS2[i].cX;
			GmcStackS1[i].cY  = GmcStackS2[i].cY;
		}
		n=m;
		m=0;
	}
}

void mapStepEditC (void)
{
	uchar i,j;
		
	for (i = 0; i < MAZETYPE; i++)
	{
        for (j = 0; j < MAZETYPE; j++) 
		{
            GucMapStepC[i][j] = GucMapStep[i][j];
        }
    }	
}

 
void mapStepEditSC (void)
{
	uchar i,j;
		
	for (i = 0; i < MAZETYPE; i++)
	{
        for (j = 0; j < MAZETYPE; j++) 
		{
            GucMapStepSC[i][j] = GucMapStepS[i][j];
        }
    }	
}
 //冲刺
/*
void mouseSpurt (void)
{
    uchar ucTemp = 0xff;
    char cXdst = 0,cYdst = 0;
       // 检查四个目标点，选择步数最少的可达目标
    if (GucMapBlock[GucXGoal0][GucYGoal0] & 0x0c) {                      
        mapStepEdit(GucXGoal0,GucYGoal0); //计算步数地图                                                   
        if (ucTemp > GucMapStep[GucXStart][GucYStart]) {                 
            cXdst  = GucXGoal0;
            cYdst  = GucYGoal0;
            ucTemp = GucMapStep[GucXStart][GucYStart];
        }
    }
    if (GucMapBlock[GucXGoal0][GucYGoal1] & 0x09) {                      
        mapStepEdit(GucXGoal0,GucYGoal1);                                
        if (ucTemp > GucMapStep[GucXStart][GucYStart]) {                 
            cXdst  = GucXGoal0;
            cYdst  = GucYGoal1;
            ucTemp = GucMapStep[GucXStart][GucYStart];
        }
    }
    if (GucMapBlock[GucXGoal1][GucYGoal0] & 0x06) {                      
        mapStepEdit(GucXGoal1,GucYGoal0);                                
        if (ucTemp > GucMapStep[GucXStart][GucYStart]) {                 
            cXdst  = GucXGoal1;
            cYdst  = GucYGoal0;
            ucTemp = GucMapStep[GucXStart][GucYStart];
        }
    }
    if (GucMapBlock[GucXGoal1][GucYGoal1] & 0x03) {                      
        mapStepEdit(GucXGoal1,GucYGoal1);                                
        if (ucTemp > GucMapStep[GucXStart][GucYStart]) {                 
            cXdst  = GucXGoal1;
            cYdst  = GucYGoal1;
            ucTemp = GucMapStep[GucXStart][GucYStart];
        }
    }
    objectGoTo(cXdst,cYdst);                                             
}
*/
 //移动控制
void objectGoTo (char  cXdst, char  cYdst)
{		
    uchar ucStep = 1;
    char cNBlock = 0, cDirTemp;
    char cX,cY;
	ledon(4);ledon(5);
	ObjectGoTo_Flag=1;
    cX = GmcMouse.cX;
    cY = GmcMouse.cY;
    mapStepEdit(cXdst,cYdst);                                            
     
    while ((cX != cXdst) || (cY != cYdst))
	{
#ifdef MOUSE_REMOTE
        /* 远程档协作式中止(2026-10-20 真机): 本循环靠 mouseGoahead/mouseTurn*
         * 让车真的走, 再由末尾 cX = GmcMouse.cX 读回坐标来推进。中止后
         * setvl/setvr 已被 SysTick 归零、各运动原语 1ms 内返回, 坐标永远不变
         * => 外层 while 变成纯空转: 不动、不打印、不响应, 主循环回不到
         * test.c 的状态机顶部, 于是 mazeTask 恒 SEARCH、LINK_MazeFinished
         * 永不执行、下一次 MAZE RUN 恒 ERR_BUSY(真机实测)。这里直接退出,
         * 由调用方一路 unwind 回 maze_done。 */
        if (LP_AbortRequested()) { ObjectGoTo_Flag=0; return; }
#endif
        ucStep = GucMapStep[cX][cY];
         
        if ((GucMapBlock[cX][cY] & 0x01) &&                              
            (GucMapStep[cX][cY + 1] < ucStep)) {                         
            cDirTemp = UP;                                               
            if (cDirTemp == GucMouseDir) {                               
                cNBlock++;                                               
                cY++;
                continue;                                                
            }
        }
        if ((GucMapBlock[cX][cY] & 0x02) &&                              
            (GucMapStep[cX + 1][cY] < ucStep)) {                         
            cDirTemp = RIGHT;                                            
            if (cDirTemp == GucMouseDir) {                               
                cNBlock++;                                               
                cX++;
                continue;                                                
            }
        }
        if ((GucMapBlock[cX][cY] & 0x04) &&                              
            (GucMapStep[cX][cY - 1] < ucStep)) {                         
            cDirTemp = DOWN;                                             
            if (cDirTemp == GucMouseDir) {                               
                cNBlock++;                                               
                cY--;
                continue;                                                
            }
        }
        if ((GucMapBlock[cX][cY] & 0x08) &&                              
            (GucMapStep[cX - 1][cY] < ucStep)) {                         
            cDirTemp = LEFT;                                             
            if (cDirTemp == GucMouseDir) {                               
                cNBlock++;                                               
                cX--;
                continue;                                                
            }
        }
        cDirTemp = (cDirTemp + 4 - GucMouseDir)%4;                       
        
        if (cNBlock)
		{
			mouseGoahead(cNBlock);
        }        
        cNBlock = 0;                                                     
        
         
        switch (cDirTemp) {

        case 1:
            mouseTurnright();
            break;

        case 2:
            mouseTurnback();
            break;

        case 3:
            mouseTurnleft();
            break;

        default:
            break;
        }

    	cX = GmcMouse.cX;
    	cY = GmcMouse.cY;
    }
     
	
    if (cNBlock)
	{	
        mouseGoahead(cNBlock);
    }
	ObjectGoTo_Flag=0;
}
 
/*
uchar mazeBlockDataGet (uchar  ucDirTemp)
{
    char cX = 0,cY = 0;
    
     
    switch (ucDirTemp) {

    case MOUSEFRONT:
        ucDirTemp = GucMouseDir;
        break;

    case MOUSELEFT:
        ucDirTemp = (GucMouseDir + 3) % 4;
        break;

    case MOUSERIGHT:
        ucDirTemp = (GucMouseDir + 1) % 4;
        break;

    default:
        break;
    }
    
     
    switch (ucDirTemp) {

    case 0:
        cX = GmcMouse.cX;
        cY = GmcMouse.cY + 1;
        break;
        
    case 1:
        cX = GmcMouse.cX + 1;
        cY = GmcMouse.cY;
        break;
        
    case 2:
        cX = GmcMouse.cX;
        cY = GmcMouse.cY - 1;
        break;
        
    case 3:
        cX = GmcMouse.cX - 1;
        cY = GmcMouse.cY;
        break;
        
    default:
        break;
    }
    
    return(GucMapBlock[cX][cY]);                                         
}
*/
uchar mazeGetDataGet (uchar  ucDirTemp)
{
    char cX = 0,cY = 0;
    
     
    switch (ucDirTemp) {

    case MOUSEFRONT:
        ucDirTemp = GucMouseDir;
        break;

    case MOUSELEFT:
        ucDirTemp = (GucMouseDir + 3) % 4;
        break;

    case MOUSERIGHT:
        ucDirTemp = (GucMouseDir + 1) % 4;
        break;

    default:
        break;
    }
    
     
    switch (ucDirTemp) {

    case 0:
        cX = GmcMouse.cX;
        cY = GmcMouse.cY + 1;
        break;
        
    case 1:
        cX = GmcMouse.cX + 1;
        cY = GmcMouse.cY;
        break;
        
    case 2:
        cX = GmcMouse.cX;
        cY = GmcMouse.cY - 1;
        break;
        
    case 3:
        cX = GmcMouse.cX - 1;
        cY = GmcMouse.cY;
        break;
        
    default:
        break;
    }
    
    return(GucMapGet[cX][cY]);                                         
}
 
void rightMethod (void)// 墙壁探索
{
    if ((GucMapBlock[GmcMouse.cX][GmcMouse.cY] & MOUSEWAY_R) &&          
        (mazeGetDataGet(MOUSERIGHT)== 0)) {                        
        mouseTurnright();                                                
        return;//优先右侧
    }
    if ((GucMapBlock[GmcMouse.cX][GmcMouse.cY] & MOUSEWAY_F) &&          
        (mazeGetDataGet(MOUSEFRONT)== 0)) {                        
        return; //直行                                                       
    }
    if ((GucMapBlock[GmcMouse.cX][GmcMouse.cY] & MOUSEWAY_L) &&          
        (mazeGetDataGet(MOUSELEFT )== 0)) {                        
        mouseTurnleft();                                                 
        return;//左侧
    }
}

void rightleftMethod (void)
{
	if ((GucMapBlock[GmcMouse.cX][GmcMouse.cY] & MOUSEWAY_R) &&          
		(mazeGetDataGet(MOUSERIGHT)== 0)) {                        
		mouseTurnright();                                                
		return;
	}
    if ((GucMapBlock[GmcMouse.cX][GmcMouse.cY] & MOUSEWAY_L) &&          
        (mazeGetDataGet(MOUSELEFT )== 0)) {                        
        mouseTurnleft();                                                 
        return;
    }
	if ((GucMapBlock[GmcMouse.cX][GmcMouse.cY] & MOUSEWAY_F) &&          
        (mazeGetDataGet(MOUSEFRONT)== 0)) {                        
        return;                                                          
    }
}
 
void leftMethod (void)
{
    if ((GucMapBlock[GmcMouse.cX][GmcMouse.cY] & MOUSEWAY_L) &&          
        (mazeGetDataGet(MOUSELEFT )== 0)) {                        
        mouseTurnleft();                                                 
        return;
    }
    if ((GucMapBlock[GmcMouse.cX][GmcMouse.cY] & MOUSEWAY_F) &&          
        (mazeGetDataGet(MOUSEFRONT)== 0)) {                        
        return;                                                          
    }
	if ((GucMapBlock[GmcMouse.cX][GmcMouse.cY] & MOUSEWAY_R) &&          
        (mazeGetDataGet(MOUSERIGHT)== 0)) {                        
        mouseTurnright();                                                
        return;
    }    
}

void leftrightMethod (void)
{
    if ((GucMapBlock[GmcMouse.cX][GmcMouse.cY] & MOUSEWAY_L) &&          
        (mazeGetDataGet(MOUSELEFT )== 0)) {                        
        mouseTurnleft();                                                 
        return;
    }
	if ((GucMapBlock[GmcMouse.cX][GmcMouse.cY] & MOUSEWAY_R) &&          
        (mazeGetDataGet(MOUSERIGHT)== 0)) {                        
        mouseTurnright();                                                
        return;
    }
	if ((GucMapBlock[GmcMouse.cX][GmcMouse.cY] & MOUSEWAY_F) &&          
        (mazeGetDataGet(MOUSEFRONT)== 0)) {                        
        return;                                                          
    }   
}
 
void frontRightMethod (void)
{    
    if ((GucMapBlock[GmcMouse.cX][GmcMouse.cY] & MOUSEWAY_F) &&          
        (mazeGetDataGet(MOUSEFRONT)== 0)) {                        
        return;                                                          
    }
	if ((GucMapBlock[GmcMouse.cX][GmcMouse.cY] & MOUSEWAY_R) &&          
        (mazeGetDataGet(MOUSERIGHT)== 0)) {                        
        mouseTurnright();                                                
        return;
    }
	if ((GucMapBlock[GmcMouse.cX][GmcMouse.cY] & MOUSEWAY_L) &&          
        (mazeGetDataGet(MOUSELEFT )== 0)) {                        
        mouseTurnleft();                                                 
        return;
    }   
}
 
void frontLeftMethod (void)
{
    if ((GucMapBlock[GmcMouse.cX][GmcMouse.cY] & MOUSEWAY_F) &&          
        (mazeGetDataGet(MOUSEFRONT)== 0)) {                        
        return;                                                          
    }
	if ((GucMapBlock[GmcMouse.cX][GmcMouse.cY] & MOUSEWAY_L) &&          
        (mazeGetDataGet(MOUSELEFT )== 0)) {                        
        mouseTurnleft();                                                 
        return;
    }    
	if ((GucMapBlock[GmcMouse.cX][GmcMouse.cY] & MOUSEWAY_R) &&          
        (mazeGetDataGet(MOUSERIGHT)== 0)) {                        
        mouseTurnright();                                                
        return;
    }   
}
 
void centralMethod (void)
{
    if (GmcMouse.cX & 0x08) {
        if (GmcMouse.cY & 0x08) {

              
            switch (GucMouseDir) {
                
            case UP:                                                     
                leftMethod();                                            
                break;

            case RIGHT:                                                  
                rightMethod();                                           
                break;

            case DOWN:                                                   
				if(GmcMouse.cX<=GmcMouse.cY)
                frontRightMethod();                                      
				else
				rightMethod();
                break;

            case LEFT:                                                   
				if(GmcMouse.cX>=GmcMouse.cY)
                frontLeftMethod();                                       
				else
				leftMethod();
                break;

            default:
                break;
            }
        } else {

                 
            switch (GucMouseDir) {
                
            case UP:                                                     
				if(GmcMouse.cX-7<=8-GmcMouse.cY)
                frontLeftMethod();                                       
				else
				leftMethod();
                break;

            case RIGHT:                                                  
                leftMethod();                                            
                break;

            case DOWN:                                                   
                rightMethod();                                           
                break;

            case LEFT:                                                   
				if(GmcMouse.cX-7>=8-GmcMouse.cY)
                frontRightMethod();                                      
				else
				rightMethod();
                break;

            default:
                break;
            }
        }
    } else {
        if (GmcMouse.cY & 0x08) {

                 
            switch (GucMouseDir) {
                
            case UP:                                                     
                rightMethod();                                           
                break;

            case RIGHT:                                                  
				if(8-GmcMouse.cX>=GmcMouse.cY-7)
                frontRightMethod();                                      
				else
				rightMethod();
                break;

            case DOWN:                                                   
				if(8-GmcMouse.cX<=GmcMouse.cY-7)
                frontLeftMethod();                                       
				else
				leftMethod();
                break;

            case LEFT:                                                   
                leftMethod();                                            
                break;

            default:
                break;
            }
        } else {

                 
            switch (GucMouseDir) {
                
            case UP:                                                     
				if(8-GmcMouse.cX<=8-GmcMouse.cY)
                frontRightMethod();                                      
				else
				rightMethod();
                break;

            case RIGHT:                                                  
				if(8-GmcMouse.cX>=8-GmcMouse.cY)
                frontLeftMethod();                                       
				else
				leftMethod();
                break;

            case DOWN:                                                   
                leftMethod();                                            
                break;

            case LEFT:                                                   
                rightMethod();                                           
                break;

            default:
                break;
            }
        }
    }
}
 
uchar crosswayCheck (char  cX, char  cY)
{
    uchar ucCt = 0;
    if ((GucMapBlock[cX][cY] & 0x01) &&                                  
        (GucMapGet[cX][cY + 1] == 0)) {                             
        ucCt++;                                                          
    }
    if ((GucMapBlock[cX][cY] & 0x02) &&                                  
        (GucMapGet[cX + 1][cY] == 0)) {                             
        ucCt++;                                                          
    }
    if ((GucMapBlock[cX][cY] & 0x04) &&                                  
        (GucMapGet[cX][cY - 1] == 0)) {                             
        ucCt++;                                                          
    }
    if ((GucMapBlock[cX][cY] & 0x08) &&                                  
        (GucMapGet[cX - 1][cY] == 0)) {                             
        ucCt++;                                                          
    }
    return ucCt;
}
 
void crosswayChoice (void)
{
    uchar sF,sL,sR;
	mapStepEditS(GoalX,GoalY);
	sF=sStepGet(MOUSEFRONT);
	sL=sStepGet(MOUSELEFT);
	sR=sStepGet(MOUSERIGHT);

	 
    if((sR<sF)&&(sF<=sL))
    {
        rightMethod();
    }
	else if((sR<sL)&&(sL<=sF))
	{
		rightleftMethod();
	}
    else if((sL<sF)&&(sF<=sR))
	{
        leftMethod();
    }
    else if((sL<sR)&&(sR<=sL))
	{
        leftrightMethod();
    }    
    else if((sF<sR)&&(sR<=sL))
	{
        frontRightMethod();
	}
    else if((sF<sL)&&(sL<=sR))
	{
        frontLeftMethod();
	}
	else
	{
		centralMethod();
	}
}
 
void goalWallchange(char GoalX,char GoalY)
{
	uchar temp,temp1;
	GucMapBlock[GucXGoal0][GucYGoal0]=((GucMapBlock[GucXGoal0][GucYGoal0])&0x0F);
	GucMapBlock[GucXGoal0][GucYGoal1]=((GucMapBlock[GucXGoal0][GucYGoal1])&0x0F);
	GucMapBlock[GucXGoal1][GucYGoal0]=((GucMapBlock[GucXGoal1][GucYGoal0])&0x0F);
	GucMapBlock[GucXGoal1][GucYGoal1]=((GucMapBlock[GucXGoal1][GucYGoal1])&0x0F);
	temp1=GucMapBlock[GoalX][GoalY];
	temp=(temp1<<4);
	GucMapBlock[GoalX][GoalY]=((GucMapBlock[GoalX][GoalY])|temp);
}

void backpointwaychoice(char cX,char cY)//返回路径
{
	if ((GucMapBlock[GmcMouse.cX][GmcMouse.cY] & MOUSEWAY_F) &&          
        (mazeGetDataGet(MOUSEFRONT)== 0)&&
		(sStepGet(MOUSEFRONT)<GucMapStepS[cX][cY])) {
        return;                                                          
    }
    if ((GucMapBlock[GmcMouse.cX][GmcMouse.cY] & MOUSEWAY_L) &&          
        (mazeGetDataGet(MOUSELEFT )== 0)&&
		(sStepGet(MOUSELEFT)<GucMapStepS[cX][cY])) {
        mouseTurnleft();                                                 
        return;
    }
    if ((GucMapBlock[GmcMouse.cX][GmcMouse.cY] & MOUSEWAY_R) &&          
        (mazeGetDataGet(MOUSERIGHT)== 0)&&
		(sStepGet(MOUSERIGHT)<GucMapStepS[cX][cY])) {
        mouseTurnright();                                                
        return;
    }
	if ((GucMapBlock[GmcMouse.cX][GmcMouse.cY] & MOUSEWAY_F) &&          
        (mazeGetDataGet(MOUSEFRONT)== 0)) {
        return;                                                          
    }
    if ((GucMapBlock[GmcMouse.cX][GmcMouse.cY] & MOUSEWAY_L) &&          
        (mazeGetDataGet(MOUSELEFT )== 0)) {
        mouseTurnleft();                                                 
        return;
    }
    if ((GucMapBlock[GmcMouse.cX][GmcMouse.cY] & MOUSEWAY_R) &&          
        (mazeGetDataGet(MOUSERIGHT)== 0)) {
        mouseTurnright();                                                
        return;
    }
}
  
uchar sStepGet (uchar  ucDirTemp)
{
    char cX = 0,cY = 0;
    switch (ucDirTemp) {
    case MOUSEFRONT:
        ucDirTemp = GucMouseDir;
        break;
    case MOUSELEFT:
        ucDirTemp = (GucMouseDir + 3) % 4;
        break;
    case MOUSERIGHT:
        ucDirTemp = (GucMouseDir + 1) % 4;
        break;
    default:
        break;
    }
    switch (ucDirTemp) 
	{
    case 0:
        cX = GmcMouse.cX;
        cY = GmcMouse.cY + 1;
        break;
    case 1:
        cX = GmcMouse.cX + 1;
        cY = GmcMouse.cY;
        break;
    case 2:
        cX = GmcMouse.cX;
        cY = GmcMouse.cY - 1;
        break;
    case 3:
        cX = GmcMouse.cX - 1;
        cY = GmcMouse.cY;
        break;
        
    default:
        break;
    }
	if((cX<=15)&&(cY<=15))
	{
		return(GucMapStepS[cX][cY]);
	}
	else
	{
		return 255;
	}
}

 
uchar deadwaycheck(uchar cross)
{						
	uchar backcross=cross;      
	uchar n=cross;
	uchar m;
	uchar bx=GmcMouse.cX;
	uchar by=GmcMouse.cY;
	uchar countmap=0;
	
	uchar tempRoadState;
	uchar tempx,tempy;
	uchar tempBlock;
	
	mapStepEditS(GoalX,GoalY);
	mapStepEditSC();
	
	while(--n)
	{
		tempRoadState=crosswayCheck(GmcCrossway[n].cX,GmcCrossway[n].cY);
		if(tempRoadState)
		{
			tempx=GmcCrossway[n].cX;
			tempy=GmcCrossway[n].cY;
			if(GucMapStepSC[tempx][tempy]<GucMapStepSC[bx][by])
			{
				tempBlock=GucMapBlock[tempx][tempy];
				GucMapBlock[tempx][tempy]=0x00;
				mapStepEditS(GmcMouse.cX,GmcMouse.cY);
				countmap++;
				GucMapBlock[tempx][tempy]=tempBlock;
				if(GucMapStepS[GoalX][GoalY]==0xff)
				{
					m=cross;
					while(--m>n)   
					{
						if(GucMapStepS[GmcCrossway[m].cX][GmcCrossway[m].cY]!=0xff)
						{
							continue;
						}
						else
						{
							goto enddead;
						}
					}
					bx=tempx;
					by=tempy;
					backcross=n;
				}
				if(countmap>=20)
				{
					break;
				}
			}
			else
			{
				continue;
			}
		}
		else
		{
			 
			 
			continue;
		}
	}
enddead:	
	return backcross;
}

 
void fastway(uchar GX,uchar GY)
{
	char i,j,cX,cY;
	uchar ucStep;
	uchar cDirTemp;
	uchar wayDir;      
	uchar goDir;
	uchar mouseDir=GucMouseDir*2;
    cX = GmcMouse.cX;
    cY = GmcMouse.cY;
		 	
	for(i=0;i<16;i++)
	{
		for(j=0;j<16;j++)
		{
			Mapfastway[i][j]=0xff;
		}
	}
	FastwayCount=0;

	mapStepEdit(GX,GY);
	ucStep=GucMapStep[cX][cY];

	if ((GucMapBlock[cX][cY] & 0x01) && (GucMapStep[cX][cY + 1] < ucStep))
	{	 
		cDirTemp = 0;
		if(mouseDir==cDirTemp)
		{
			Mapfastway[cX][cY]=cDirTemp;
			wayDir=cDirTemp;
			FastwayAction[FastwayCount].action=0;
			FastwayAction[FastwayCount].cx=cX;
			FastwayAction[FastwayCount].cy=cY;
			FastwayCount++;
			cY++;
			goto sec;
		}
	}
	if ((GucMapBlock[cX][cY] & 0x02) && (GucMapStep[cX + 1][cY] < ucStep))
	{	  
		cDirTemp = 2;
		if(mouseDir==cDirTemp)
		{
			Mapfastway[cX][cY]=cDirTemp;
			wayDir=cDirTemp;
			FastwayAction[FastwayCount].action=0;
			FastwayAction[FastwayCount].cx=cX;
			FastwayAction[FastwayCount].cy=cY;
			FastwayCount++;
			cX++;
			goto sec;
		}
	}
	if ((GucMapBlock[cX][cY] & 0x04) && (GucMapStep[cX][cY - 1] < ucStep))
	{	
		cDirTemp = 4;
		if(mouseDir==cDirTemp)
		{
			Mapfastway[cX][cY]=cDirTemp;
			wayDir=cDirTemp;
			FastwayAction[FastwayCount].action=0;
			FastwayAction[FastwayCount].cx=cX;
			FastwayAction[FastwayCount].cy=cY;
			FastwayCount++;
			cY--;
			goto sec;
		}
	}
	if ((GucMapBlock[cX][cY] & 0x08) && (GucMapStep[cX - 1][cY] < ucStep))
	{	
		cDirTemp = 6;
		if(mouseDir==cDirTemp)
		{
			Mapfastway[cX][cY]=cDirTemp;
			wayDir=cDirTemp;
			FastwayAction[FastwayCount].action=0;
			FastwayAction[FastwayCount].cx=cX;
			FastwayAction[FastwayCount].cy=cY;
			FastwayCount++;
			cX--;
			goto sec;
		}
	}
	
	FastwayAction[FastwayCount].action=(8+cDirTemp-mouseDir)%8;
	FastwayAction[FastwayCount].cx=cX;
	FastwayAction[FastwayCount].cy=cY;
	FastwayCount++;
	
	Mapfastway[cX][cY]=cDirTemp;
	wayDir=cDirTemp;
	mouseDir=cDirTemp;
	switch(cDirTemp)	 
	{
		case 0:cY++;
			   break;
		case 2:cX++;
			   break;
		case 4:cY--;
			   break;
		case 6:cX--;
			   break;
		default:break;
	}
		
sec:	
	while ((cX != GX) || (cY != GY))
	{
		ucStep = GucMapStep[cX][cY];
		mouseDir=cDirTemp;
		if ((GucMapBlock[cX][cY] & 0x01) && (GucMapStep[cX][cY + 1] < ucStep))	
		{
			goDir=0;
			if(wayDir==0)
			{
				cDirTemp = 0;
			}
			if(wayDir==2)
			{
				cDirTemp = 1;
			}
			if(wayDir==6)
			{
				cDirTemp = 7;
			}
			if(mouseDir==cDirTemp)
			{
				Mapfastway[cX][cY]=cDirTemp;
				wayDir=goDir;
				FastwayAction[FastwayCount].action=0;
				FastwayAction[FastwayCount].cx=cX;
				FastwayAction[FastwayCount].cy=cY;
				FastwayCount++;
				cY++;
				continue; 
			}
		}
		if ((GucMapBlock[cX][cY] & 0x02) && (GucMapStep[cX + 1][cY] < ucStep))
		{
			goDir=2;
			if(wayDir==0)
			{
				cDirTemp = 1;
			}
			if(wayDir==2)
			{
				cDirTemp = 2;
			}
			if(wayDir==4)
			{
				cDirTemp = 3;
			}
			if(mouseDir==cDirTemp)
			{
				Mapfastway[cX][cY]=cDirTemp;
				wayDir=goDir;
				FastwayAction[FastwayCount].action=0;
				FastwayAction[FastwayCount].cx=cX;
				FastwayAction[FastwayCount].cy=cY;
				FastwayCount++;
				cX++;
				continue; 
			}
		}
		if ((GucMapBlock[cX][cY] & 0x04) && (GucMapStep[cX][cY - 1] < ucStep))
		{
			goDir=4;
			if(wayDir==2)
			{
				cDirTemp = 3;
			}
			if(wayDir==4)
			{
				cDirTemp = 4;
			}
			if(wayDir==6)
			{
				cDirTemp = 5;
			}
			if(mouseDir==cDirTemp)
			{
				Mapfastway[cX][cY]=cDirTemp;
				wayDir=goDir;
				FastwayAction[FastwayCount].action=0;
				FastwayAction[FastwayCount].cx=cX;
				FastwayAction[FastwayCount].cy=cY;
				FastwayCount++;
				cY--;
				continue; 
			}
		}
		if ((GucMapBlock[cX][cY] & 0x08) && (GucMapStep[cX - 1][cY] < ucStep))
		{
			goDir=6;
			if(wayDir==0)
			{
				cDirTemp = 7;
			}
			if(wayDir==4)
			{
				cDirTemp = 5;
			}
			if(wayDir==6)
			{
				cDirTemp = 6;
			}
			if(mouseDir==cDirTemp)
			{
				Mapfastway[cX][cY]=cDirTemp;
				wayDir=goDir;
				FastwayAction[FastwayCount].action=0;
				FastwayAction[FastwayCount].cx=cX;
				FastwayAction[FastwayCount].cy=cY;
				FastwayCount++;
				cX--;
				continue; 
			}
		}
		
		Mapfastway[cX][cY]=cDirTemp;
		
		FastwayAction[FastwayCount].action=(8+Mapfastway[cX][cY]-mouseDir)%8;
		FastwayAction[FastwayCount].cx=cX;
		FastwayAction[FastwayCount].cy=cY;
		FastwayCount++;
		
		mouseDir=cDirTemp;
		wayDir=goDir;
		
		switch(goDir)
		{
			case 0:cY++;
				   break;
			case 2:cX++;
				   break;
			case 4:cY--;
				   break;
			case 6:cX--;
				   break;
			default:break;
		}
	}
	
	FastwayAction[FastwayCount].action=(8+wayDir-mouseDir)%8;
	FastwayAction[FastwayCount].cx=cX;
	FastwayAction[FastwayCount].cy=cY;
	FastwayCount++;
}


 
void fastway_90(uchar GX,uchar GY)
{
	char i,j,cX,cY;
	uchar ucStep;
	uchar cDirTemp;
	uchar wayDir;      
	uchar goDir;
	uchar mouseDir=GucMouseDir;
    cX = GmcMouse.cX;
    cY = GmcMouse.cY;
		 	
	for(i=0;i<16;i++)
	{
		for(j=0;j<16;j++)
		{
			Mapfastway_90[i][j]=0xff;
		}
	}
	FastwayCount_90=0;

	mapStepEdit(GX,GY);
	ucStep=GucMapStep[cX][cY];
	if ((GucMapBlock[cX][cY] & 0x01) && (GucMapStep[cX][cY + 1] < ucStep))
	{	 
		cDirTemp = 0;
		if(mouseDir==cDirTemp)
		{
			Mapfastway_90[cX][cY]=cDirTemp;
			wayDir=cDirTemp;
			FastwayAction_90[FastwayCount_90].action=0;
			FastwayAction_90[FastwayCount_90].cx=cX;
			FastwayAction_90[FastwayCount_90].cy=cY;
			FastwayCount_90++;
			cY++;
			goto sec;
		}
	}
	if ((GucMapBlock[cX][cY] & 0x02) && (GucMapStep[cX + 1][cY] < ucStep))
	{	  
		cDirTemp = 1;
		if(mouseDir==cDirTemp)
		{
			Mapfastway_90[cX][cY]=cDirTemp;
			wayDir=cDirTemp;
			FastwayAction_90[FastwayCount_90].action=0;
			FastwayAction_90[FastwayCount_90].cx=cX;
			FastwayAction_90[FastwayCount_90].cy=cY;
			FastwayCount_90++;
			cX++;
			goto sec;
		}
	}
	if ((GucMapBlock[cX][cY] & 0x04) && (GucMapStep[cX][cY - 1] < ucStep))
	{	
		cDirTemp = 2;
		if(mouseDir==cDirTemp)
		{
			Mapfastway_90[cX][cY]=cDirTemp;
			wayDir=cDirTemp;
			FastwayAction_90[FastwayCount_90].action=0;
			FastwayAction_90[FastwayCount_90].cx=cX;
			FastwayAction_90[FastwayCount_90].cy=cY;
			FastwayCount_90++;
			cY--;
			goto sec;
		}
	}
	if ((GucMapBlock[cX][cY] & 0x08) && (GucMapStep[cX - 1][cY] < ucStep))
	{	
		cDirTemp = 3;
		if(mouseDir==cDirTemp)
		{
			Mapfastway_90[cX][cY]=cDirTemp;
			wayDir=cDirTemp;
			FastwayAction_90[FastwayCount_90].action=0;
			FastwayAction_90[FastwayCount_90].cx=cX;
			FastwayAction_90[FastwayCount_90].cy=cY;			
			FastwayCount_90++;
			cX--;
			goto sec;
		}
	}
	
	FastwayAction_90[FastwayCount_90].action=(4+cDirTemp-mouseDir)%4;
	FastwayAction_90[FastwayCount_90].cx=cX;
	FastwayAction_90[FastwayCount_90].cy=cY;
	FastwayCount_90++;
	
	Mapfastway_90[cX][cY]=cDirTemp;
	wayDir=cDirTemp;
	mouseDir=cDirTemp;
	switch(cDirTemp)	 
	{
		case 0:cY++;
			   break;
		case 1:cX++;
			   break;
		case 2:cY--;
			   break;
		case 3:cX--;
			   break;
		default:break;
	}
		
sec:
	while ((cX != GX) || (cY != GY))
	{		
		ucStep = GucMapStep[cX][cY];
		mouseDir=cDirTemp;	
	if ((GucMapBlock[cX][cY] & 0x01) && (GucMapStep[cX][cY + 1] < ucStep))
	{	 
		cDirTemp = 0;
		if(mouseDir==cDirTemp)
		{
			Mapfastway_90[cX][cY]=cDirTemp;
			wayDir=cDirTemp;
			FastwayAction_90[FastwayCount_90].action=0;
			FastwayAction_90[FastwayCount_90].cx=cX;
			FastwayAction_90[FastwayCount_90].cy=cY;
			FastwayCount_90++;
			cY++;
			continue;
		}
	}
	if ((GucMapBlock[cX][cY] & 0x02) && (GucMapStep[cX + 1][cY] < ucStep))
	{	  
		cDirTemp = 1;
		if(mouseDir==cDirTemp)
		{
			Mapfastway_90[cX][cY]=cDirTemp;
			wayDir=cDirTemp;
			FastwayAction_90[FastwayCount_90].action=0;
			FastwayAction_90[FastwayCount_90].cx=cX;
			FastwayAction_90[FastwayCount_90].cy=cY;
			FastwayCount_90++;
			cX++;
			continue;
		}
	}
	if ((GucMapBlock[cX][cY] & 0x04) && (GucMapStep[cX][cY - 1] < ucStep))
	{	
		cDirTemp = 2;
		if(mouseDir==cDirTemp)
		{
			Mapfastway_90[cX][cY]=cDirTemp;
			wayDir=cDirTemp;
			FastwayAction_90[FastwayCount_90].action=0;
			FastwayAction_90[FastwayCount_90].cx=cX;
			FastwayAction_90[FastwayCount_90].cy=cY;
			FastwayCount_90++;
			cY--;
			continue;
		}
	}
	if ((GucMapBlock[cX][cY] & 0x08) && (GucMapStep[cX - 1][cY] < ucStep))
	{	
		cDirTemp = 3;
		if(mouseDir==cDirTemp)
		{
			Mapfastway_90[cX][cY]=cDirTemp;
			wayDir=cDirTemp;
			FastwayAction_90[FastwayCount_90].action=0;
			FastwayAction_90[FastwayCount_90].cx=cX;
			FastwayAction_90[FastwayCount_90].cy=cY;
			FastwayCount_90++;
			cX--;
			continue;
		}
	}			
		FastwayAction_90[FastwayCount_90].action=(4+cDirTemp-mouseDir)%4;
		FastwayAction_90[FastwayCount_90].cx=cX;
		FastwayAction_90[FastwayCount_90].cy=cY;
		FastwayCount_90++;
		Mapfastway_90[cX][cY]=cDirTemp;
		wayDir=cDirTemp;
		mouseDir=cDirTemp;
		
		switch(cDirTemp)
		{
			case 0:cY++;
				   break;
			case 1:cX++;
				   break;
			case 2:cY--;
				   break;
			case 3:cX--;
				   break;
			default:break;
		}
			}
	FastwayAction_90[FastwayCount_90].action=(4+wayDir-mouseDir)%4;
	FastwayAction_90[FastwayCount_90].cx=cX;
	FastwayAction_90[FastwayCount_90].cy=cY;
	FastwayCount_90++;
	}
	
	 
void backPath(FASTWAYACTION *FastwayAction_1, uchar FastwayCount_2)
{
	int i;
	for(i=0;i<FastwayCount_2;i++)
	{
		FastwayActionTemp[i]=FastwayAction_1[FastwayCount_2-i-1].action;
	}
	for(i=0;i<FastwayCount_2;i++)
	{
		switch(FastwayActionTemp[i])
		{
			case 1:FastwayActionTemp[i]=7;break;
			case 2:FastwayActionTemp[i]=6;break;
			case 6:FastwayActionTemp[i]=2;break;
			case 7:FastwayActionTemp[i]=1;break;
		}
	}	
	FastwayAction_1[0].action=0;
	for(i=1;i<FastwayCount_2;i++)
	{
		FastwayAction_1[i].action=FastwayActionTemp[i-1];
	}
}




