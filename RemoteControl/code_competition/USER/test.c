#include <stm32f10x.h>
#include "sys.h"
#include "usart.h"		
#include "delay.h"	
#include "timer.h"
#include "adc.h"
#include "utility.h"
#include "stratagy.h"
#include "led.h"
#include "stmflash.h"
#include "user.h"
#ifdef MOUSE_REMOTE
#include "link.h" /* 移植 P2: 链路层(LINK_Init/Tick + MAZE 值守 API) */
#endif
int testi = 0;
extern uchar spurtTestAction[200]; 
extern u8 sidewall_threshold;
#ifndef MOUSE_REMOTE
#define FLASHSAVE /* remote build: flight recorder OFF - TELEM streams live data to host instead; frees ~9.5KB RAM for the link layer */
#endif
extern u16 save_data_num,save_num;
extern u8 save_data_time,per_timer;
u16 Test_Time_Flag=0;extern int cpu;
extern u8 biaodingmode;
extern uchar 	SpurtTest_FastwayAction[MAZETYPE * MAZETYPE];
extern u16 accdistance;
extern u8 speedmode;
extern int TIM2CNT;
extern u8	spurtTestFlag;
extern u8 back90_flag;
extern u16 totaltrack;
extern u16 decdistance;
extern u8 spurt_45_end_flag;
int LTopSpeedh,L45TopSpeedh,BackTopSpeedh;
u16 ledcontrol=0;
u8 turndir=0;
u8 goaheadnum=0;
u8 BackSearchFlag=3; 
extern long angle;
extern u8 spurtTestFlag;
u8 blockx,blocky; 
u8 SearchOrSpurt_Flag=0;
u8 GoOrBackFlag=0;
uchar n          = 0;                                                
extern u8 goal_keep_flag; 
extern uchar	Mapfastway[MAZETYPE][MAZETYPE]      ;
extern FASTWAYACTION	FastwayAction[MAZETYPE * MAZETYPE];               
extern uchar	FastwayCount;
extern uchar	Mapfastway_90[MAZETYPE][MAZETYPE]      ;
extern FASTWAYACTION	FastwayAction_90[MAZETYPE * MAZETYPE];               
extern uchar	FastwayCount_90;
extern uchar	actionsequence[];
extern uchar    GucXStart;                 
extern uchar    GucYStart;                 
extern uchar    GucXGoal0;             
extern uchar    GucXGoal1;
extern uchar    GucYGoal0;             
extern uchar    GucYGoal1;
u8 flashirerror = 0;
extern uchar    GucMouseTask;              

extern char GoalX;
extern char GoalY;

extern uchar    GucMapStep[MAZETYPE][MAZETYPE];            
extern uchar    GucMapStepS[MAZETYPE][MAZETYPE];            
extern uchar    GucMapStepC[MAZETYPE][MAZETYPE];            
extern uchar    GucMapStepSC[MAZETYPE][MAZETYPE];            

extern MAZECOOR GmcStack[MAZETYPE * MAZETYPE];               
extern MAZECOOR GmcStackS1[MAZETYPE * MAZETYPE]; 
extern MAZECOOR GmcStackS2[MAZETYPE * MAZETYPE]; 
extern MAZECOOR GmcCrossway[MAZETYPE * MAZETYPE];               

extern MAZECOOR GmcMouse;                                                
                                                                         
                                                                        
extern uchar    GucMouseDir;                                             
extern uchar    GucMapBlock[MAZETYPE][MAZETYPE];                         
                                                                         
                                                                        
                                                                        
extern uchar	GucMapGet[MAZETYPE][MAZETYPE];

#ifndef MOUSE_REMOTE
extern u16 disf1,disf2,disf,disl,disr,disl45,disr45,disl45h,disr45h,disl45hh,disr45hh;
#endif
extern u8 leftwallcritical,rightwallcritical,leftwallcriticalh,rightwallcriticalh;
extern u8 righthaswall;
#ifndef MOUSE_REMOTE
extern int setvl,setvr;
#endif
extern int gyroinit,ganglet;
extern double g;
extern u8 task;
extern int gangle,gangleh,ggangleh;
extern int gyro,gyro0;
extern int pwml,pwmr;
extern u8 battarypercent;
 
extern u8 angleterm,gyro_adj;
extern u16 angle_counter;
extern u8 freemode;
extern u8 sleepmode;
extern long track,trackl,trackr;
extern u8 irswitch;
extern u8 lefthaswall,righthaswall,forwardhaswall,lefthaswallh,righthaswallh;
extern u16 tick,turnaround_tick;
 
extern u16 speed_ss,speed_turn,speed_fastss;
extern u8 kalmanswitch;

#ifndef MOUSE_REMOTE
extern u16 dataf1[240],dataf2[240],datal[220],datar[220];
#endif
extern int rawf1,rawf2,rawl,rawr,rawf1h,rawf2h,rawlh,rawrh;
int rawf1avrgnear,rawf2avrgnear,rawlavrgnear,rawravrgnear,rawf1avrgfar,rawf2avrgfar,rawlavrgfar,rawravrgfar;
int iii;
extern int forwardirdis;
#ifdef MOUSE_REMOTE
int showbattery=1;	/* remote profile: battery monitor ON (2026-10-16); competition keeps 0 */
#else
int showbattery=0;
#endif
extern int LTopSpeed,L45TopSpeed,L90TopSpeed,BackTopSpeed;
extern long absolutetrackl,absolutetrackr,absolutetrack;
 
u16 flashaddr=0,flashaddrmax,flashreadtemp; 
u8 flashwrite=1,flashenable=0;
int irselftestdiff;
int spurt_flag=0;

 
void mouseInit()
{
	int i=0,j=0;

	for(i=0;i<MAZETYPE;i++)
	{
		for(j=0;j<MAZETYPE;j++)
		{
			GucMapBlock[i][j] = 0xf0; 
		}
	}
 
 
			
	GucMapBlock[0][0] = 0x11;
	GucMapBlock[1][0] = 0x70;
		
 
}
 
 
void hardware_init()
{
	Stm32_Clock_Init(9);	 
	delay_init(72);			 
	uart_init(72,115200);	 
	Adc_Init();				 
	PWM_motor_Init();
	Led_Init();
	Timer1_Init();
	Timer2_Init();
	Timer3_Init();
#ifdef MOUSE_REMOTE
	/* 2026-10-16 修: 链路初始化必须提到**陀螺零偏标定阻塞循环之前**。
	 * 下面 if(gyro0==-1) 的校准循环(行 164 起)在陀螺不静止时会**永久自旋**,
	 * 而 USART1 RX 的 NVIC 使能写在 LINK_Init 里 —— 放在 hardware_init 末尾
	 * 就等于"标定没完成之前整条链路静默"(实测: 悬挂工况 COM7 全程 0 字节)。
	 * 提前后: (1) 标定期间 PING/TELEM 仍可应答, 便于在线诊断;
	 *          (2) IWDG 提前配成 2s, 并由 SysTick 每 1ms 喂狗(见 link.c 喂狗说明)。
	 * 位置: 所有硬件 Init 之后、读标定 Flash 之前; 全局量已由 C 启动代码初始化,
	 * 故与"LINK_Init 必须在 HARDWARE_Init 之后"的原约束不冲突。 */
	LINK_Init();
#endif
	gyro0=STMFLASH_ReadHalfWord(FLASH_ADDR_GYRO0);		 
	 
	if(gyro0==-1)
	{
		u8 ledswitch=0;
		gyro0=2044;freemode=1;
		angle_counter=0;
		gyro_adj=1;
		while(1)
		{	
			if(gangle>=0)
			{ledoff(0);ledon(gangle%8+1);}
			else if(gangle<0)
			{ledoff(0);ledon(gangle%8+8);}

			if(angle_counter>1000)break;				 
		}
		gyro_adj=0;
		STMFLASH_Unlock();
		STMFLASH_ErasePage(FLASH_ADDR_GYRO0);
		STMFLASH_Unlock();
		STMFLASH_WriteHalfWord(FLASH_ADDR_GYRO0,gyro0);	 
		sleepmode=1;									 
		ledon(0);delay_ms(100);ledoff(0);
		ledswitch=1;
		while(1)
		{
			ledoff(0); ledon(ledswitch);
			delay_ms(200);
			ledswitch++;
			if(ledswitch==9) break;
		}
		ledon(0);
		delay_ms(500);
		ledoff(0);
		sleepmode=0;
	}
}

int main()
 {  
	 
	u16 spurtTestNum1=0, spurtTestNum2=0, spurtTestNum3=0;
 
	uchar ucRoadStat = 0;                                                
	uchar ucTemp     = 0;
	uchar m=0;
	uchar l=0;
 	uchar k=0;
	
	u16 SpurtTest_Test=0;
	
	uchar shortwayX=0;
	uchar shortwayY=0;
	uchar shortwayN=0;
	
	uchar GoalGet=0;
	uchar stepMax=12;  //12
	uchar stepMaxLimit=12;
	
	uchar dstep=2;
	uchar dstepS=2;
	
	uchar tempBlock;

	
	 
	char tempX=0;
	char tempY=0;
	
	char tempCX1,tempCX2,tempCY1,tempCY2,gox,goy;

	 
	u8 keyfunc=0,keytmp=0,c=0;
#ifdef MOUSE_REMOTE
	u8 mazeBlind=0; /* 移植 P2: 本次发车目标(0=BLIND 只探索不冲刺, 1=RACE 全流程) */
#endif
	u32 ft=0;
	u16 keydelay=0,ispressed=0;
	long flashtemp;
	u16 tmptt;
	u8 spurtmode;
	
	hardware_init();
	mouseInit();
	 
	flashreadtemp=STMFLASH_ReadHalfWord(FLASH_ADDR_IRDATA);
	
	if(flashreadtemp!=65535)
	{
		flashaddr=0;
		rawf1avrgnear=STMFLASH_ReadHalfWord(FLASH_ADDR_IRDATA+flashaddr);flashaddr+=2;
		rawf1avrgfar=STMFLASH_ReadHalfWord(FLASH_ADDR_IRDATA+flashaddr);flashaddr+=2;
		rawf2avrgnear=STMFLASH_ReadHalfWord(FLASH_ADDR_IRDATA+flashaddr);flashaddr+=2;
		rawf2avrgfar=STMFLASH_ReadHalfWord(FLASH_ADDR_IRDATA+flashaddr);flashaddr+=2;
		rawlavrgnear=STMFLASH_ReadHalfWord(FLASH_ADDR_IRDATA+flashaddr);flashaddr+=2;
		rawlavrgfar=STMFLASH_ReadHalfWord(FLASH_ADDR_IRDATA+flashaddr);flashaddr+=2;
		rawravrgnear=STMFLASH_ReadHalfWord(FLASH_ADDR_IRDATA+flashaddr);flashaddr+=2;
		rawravrgfar=STMFLASH_ReadHalfWord(FLASH_ADDR_IRDATA+flashaddr);flashaddr+=2;
		
 
		InfCorrect(rawlavrgnear,33,rawlavrgfar,94,250,datal,L);
		InfCorrect(rawravrgnear,33,rawravrgfar,95,250,datar,R);
		InfCorrect(rawf1avrgnear,32,rawf1avrgfar,213,250,dataf1,F1); 
		InfCorrect(rawf2avrgnear,32,rawf2avrgfar,213,250,dataf2,F2);
		
		
    printf("L - rawlavrgnear: %d, rawlavrgfar: %d, datal: %d\n", rawlavrgnear, rawlavrgfar, datal);
    printf("R - rawravrgnear: %d, rawravrgfar: %d, datar: %d\n", rawravrgnear, rawravrgfar, datar);
    printf("F1 - rawf1avrgnear: %d, rawf1avrgfar: %d, dataf1: %d\n", rawf1avrgnear, rawf1avrgfar, dataf1);
    printf("F2 - rawf2avrgnear: %d, rawf2avrgfar: %d, dataf2: %d\n", rawf2avrgnear, rawf2avrgfar, dataf2);
	
		ledon(8);delay_ms(120);ledoff(0);delay_ms(120);ledon(8);delay_ms(120);ledoff(0);delay_ms(120);
	}
	else
	{
		
		flashirerror = 1;
	
		InfCorrect(1450,33,371,94,250,datal,L);
		InfCorrect(1439,35,366,94,250,datar,R);
		InfCorrect(1880,32,145,212,250,dataf1,F1); 
		InfCorrect(2166,32,142,212,250,dataf2,F2);
	}
	
	
fuction_Choose:
	ledoff(0);ledon(6);
#ifdef MOUSE_REMOTE
	/* ================= 远程档值守循环(移植 P2) =================
	 * 不等按键: 等上位机 `MAZE RUN` 命令。linkMazeReq 由 link.c 在 SysTick
	 * 上下文置位(边沿信号), LINK_MazeStarted() 消费它并清协作式中止标志。
	 * 值守态安全设置与旧工程 main.c 一致: 电机输出恒 0(freemode=1)、不休眠
	 * (sleepmode=0, 保证传感器与链路持续工作)、红外发射开。 */
	freemode=1;
	sleepmode=0;
	irswitch=1;
	while (!LINK_MazeRequested())
	{
		ledoff(0); ledon(6); delay_ms(250); ledoff(0); delay_ms(250);
	}
	LINK_MazeStarted();
	mazeBlind = (LINK_MazeTarget() == 0); /* LINK_MAZE_BLIND=0 / LINK_MAZE_RACE=1 */
	goto maze_run; /* 跳过按键菜单, 直接进比赛流程(见下方 maze_run 标签) */
#else
	while(1)				 
	{
		if(BUTTON1==0 && ispressed==0)					 
		{
			delay_ms(5);
			keydelay++;
			if(keydelay==50){ledoff(0);ledon(5);}
			if(keydelay==100){ledoff(0);ledon(4);}
			if(keydelay==150){ledoff(0);ledon(3);}
			if(keydelay==200){ledoff(0);break;}
		}
		if(BUTTON1)		 
		{
			freemode=1;		 
			if(ispressed==0){ledoff(0);ispressed=1;}	 
			delay_ms(5);	 
			if(BUTTON1)
			{
				while(BUTTON1)
				{
					keytmp++;
					delay_ms(10);
					if(keytmp>60) break;				 
				}

				if(keytmp<60)							 
				{
					keytmp=0;
					ledoff(keyfunc);
					keyfunc++;
					if(keyfunc==9)keyfunc=1;
					ledon(keyfunc);
				}
				else									 
				{	
					keytmp=0;
					ledoff(keyfunc);
					delay_ms(80);
					ledon(keyfunc);
					delay_ms(80);
					ledoff(keyfunc);
					delay_ms(80);
					ledon(keyfunc);
					delay_ms(500);
					ledoff(keyfunc);
					break;
				}
			}
			else;
		}
	}
	while(BUTTON1);		 
#endif
	
	freemode=0;
	
	switch(keyfunc)
	{
 
		case 0:				 
 
			break;
 
		case 1:				 

			while(1)
			{
				sleepmode=1; 
				if(c==9||c==0)c=1;
				ledoff(0);ledon(c);delay_ms(500);c++;
			}
 
		case 2:				 
 
		freemode = 1;	
		flashaddr=0;
			for(tmptt=0;tmptt<8;tmptt++)
			{
				STMFLASH_Unlock();STMFLASH_ErasePage(FLASH_ADDR_IRDATA+flashaddr);flashaddr+=2;
			}
			while(1)
			{
				sleepmode=1; 
				if(c==9||c==0)c=1;
				ledoff(0);ledon(c);delay_ms(1000);c++;
			}
 
		case 3:				 
 
			angle_counter=0;
		  freemode = 1;
			gyro_adj=1;
			while(1)
			{	
				if(gangle>=0)
				{ledoff(0);ledon(gangle%8+1);}
				else if(gangle<0)
				{ledoff(0);ledon(gangle%8+8);}

				if(angle_counter>1500)break; 
			}
			gyro_adj=0;
			STMFLASH_Unlock();
			STMFLASH_ErasePage(FLASH_ADDR_GYRO0);
			STMFLASH_Unlock();
			STMFLASH_WriteHalfWord(FLASH_ADDR_GYRO0,gyro0); 
			sleepmode=1; 
			ledon(0);delay_ms(100);ledoff(0);
			c=1;
				
			while(1)
			{
				printf("GYRO0:%d,",gyro0);
				if(c==9||c==0)c=1;
				ledoff(0);ledon(c);delay_ms(500);c++;
			}
			
			ledon(0);
			delay_ms(500);
			ledoff(0);
			sleepmode=0;
			break;
			break;
 
		case 4:				 
 
			freemode=1;
			while(1)
			{
				printf("disl:%d,disr:%d disf1:%d disf2:%d \r\n",disl,disr,disf1,disf2);
				delay_ms(100);
			}
			
		case 5:				 
 
			irswitch=1;
		freemode=1;
go_to_case_5:
			
			gangleSee = 1;
			
			while(1)
			{
				 
								printf("\n");
				printf("absolutetrackl:%ld",absolutetrackl);
				printf("absolutetrackr:%ld",absolutetrackr);
 
 
				if(BUTTON1==1)
				{
					delay_ms(5);
					if(BUTTON1) break;
				}
				delay_ms(300);
			}
			ledon(0);
			delay_ms(1000);
			ledoff(0);
			while(1)
			{
        printf("adcf1:%d adcf2:%d adcl:%d adcr:%d gangle_Rate:%d  gangle:%d  angle_code:%ld \r\n",rawf1,rawf2,rawl,rawr,gyro,gangle,angle);
				if(BUTTON1==1)
				{
					delay_ms(5);
					if(BUTTON1) break;
				}
				delay_ms(300);
			}
			
			while(1)
			{
				sleepmode=1; 
				if(c==9||c==0)c=1;
				ledoff(0);ledon(c);delay_ms(1000);c++;
				if(BUTTON1==1)
				{delay_ms(5);if(BUTTON1==1){ispressed=0; goto fuction_Choose;}}
			}
 
		case 6:				 
			
		freemode = 1;
		flashreadtemp=STMFLASH_ReadHalfWord(FLASH_ADDR_IRDATA);
			if(flashreadtemp!=65535)
			{
				flashaddr=0;
				rawf1avrgnear=STMFLASH_ReadHalfWord(FLASH_ADDR_IRDATA+flashaddr);flashaddr+=2;
				rawf1avrgfar=STMFLASH_ReadHalfWord(FLASH_ADDR_IRDATA+flashaddr);flashaddr+=2;
				rawf2avrgnear=STMFLASH_ReadHalfWord(FLASH_ADDR_IRDATA+flashaddr);flashaddr+=2;
				rawf2avrgfar=STMFLASH_ReadHalfWord(FLASH_ADDR_IRDATA+flashaddr);flashaddr+=2;
				rawlavrgnear=STMFLASH_ReadHalfWord(FLASH_ADDR_IRDATA+flashaddr);flashaddr+=2;
				rawlavrgfar=STMFLASH_ReadHalfWord(FLASH_ADDR_IRDATA+flashaddr);flashaddr+=2;
				rawravrgnear=STMFLASH_ReadHalfWord(FLASH_ADDR_IRDATA+flashaddr);flashaddr+=2;
				rawravrgfar=STMFLASH_ReadHalfWord(FLASH_ADDR_IRDATA+flashaddr);flashaddr+=2;
				
				printf("rawLFavrgnear:%d,rawLFavrgfar:%d \r\n",rawf1avrgnear,rawf1avrgfar);
				printf("rawLavrgnear:%d,rawLavrgfar:%d \r\n",rawlavrgnear,rawlavrgfar);
				printf("rawRavrgnear:%d,rawRavrgfar:%d \r\n",rawravrgnear,rawravrgfar);
				printf("rawRFavrgnear:%d,rawRFavrgnear:%d \r\n",rawf2avrgnear,rawf2avrgfar);
				
				while(1)
				{
					sleepmode=1; 
					if(c==9||c==0)c=1;
					ledoff(0);ledon(c);delay_ms(1000);c++;
				}
			}
	
			biaodingmode=1;
			ledoff(0);
			while(1)
			{
				ledon(1);ledon(2);
				if(BUTTON1==1)
				{
					delay_ms(5); 
					if(BUTTON1==1)break;
				}					
			}
			ledoff(0);ledon(4);delay_ms(1000);ledoff(0);ledon(3);delay_ms(1000);ledoff(0);ledon(2);delay_ms(1000);ledoff(0);ledon(1);delay_ms(1000);
			rawlavrgnear=rawl; 
			rawravrgfar=rawr; 
			 
			ledoff(0);
			delay_ms(500);
			while(1)
			{
				ledon(7);ledon(8);
				if(BUTTON1==1)
				{
					delay_ms(5); 
					if(BUTTON1==1)break;
				}					
			}
			ledoff(0);ledon(4);delay_ms(1000);ledoff(0);ledon(3);delay_ms(1000);ledoff(0);ledon(2);delay_ms(1000);ledoff(0);ledon(1);delay_ms(1000);
			rawlavrgfar=rawl;
			rawravrgnear=rawr;
			 
			
			ledoff(0);
			delay_ms(500);
			while(1)
			{
				ledon(4);ledon(5);
				if(BUTTON1==1)
				{
					delay_ms(5); 
					if(BUTTON1==1)break;
				}					
			}
			ledoff(0);ledon(4);delay_ms(1000);ledoff(0);ledon(3);delay_ms(1000);ledoff(0);ledon(2);delay_ms(1000);ledoff(0);ledon(1);delay_ms(1000);
			rawf1avrgnear=rawf1;
			rawf2avrgnear=rawf2;
			 
			
			ledoff(0);
			delay_ms(500);
			while(1)
			{
				ledon(3);ledon(6);
				if(BUTTON1==1)
				{
					delay_ms(5); 
					if(BUTTON1==1)break;
				}					
			}
			ledoff(0);ledon(4);delay_ms(1000);ledoff(0);ledon(3);delay_ms(1000);ledoff(0);ledon(2);delay_ms(1000);ledoff(0);ledon(1);delay_ms(1000);
			rawf1avrgfar=rawf1;
			rawf2avrgfar=rawf2;
			 
			
			flashaddr=0;
			for(tmptt=0;tmptt<8;tmptt++)
			{
				STMFLASH_Unlock();STMFLASH_ErasePage(FLASH_ADDR_IRDATA+flashaddr);flashaddr+=2;
			}
			flashaddr=0;
			STMFLASH_Unlock();STMFLASH_WriteHalfWord(FLASH_ADDR_IRDATA+flashaddr,rawf1avrgnear);flashaddr+=2;
			STMFLASH_Unlock();STMFLASH_WriteHalfWord(FLASH_ADDR_IRDATA+flashaddr,rawf1avrgfar);flashaddr+=2;
			STMFLASH_Unlock();STMFLASH_WriteHalfWord(FLASH_ADDR_IRDATA+flashaddr,rawf2avrgnear);flashaddr+=2;
			STMFLASH_Unlock();STMFLASH_WriteHalfWord(FLASH_ADDR_IRDATA+flashaddr,rawf2avrgfar);flashaddr+=2;
			STMFLASH_Unlock();STMFLASH_WriteHalfWord(FLASH_ADDR_IRDATA+flashaddr,rawlavrgnear);flashaddr+=2;
			STMFLASH_Unlock();STMFLASH_WriteHalfWord(FLASH_ADDR_IRDATA+flashaddr,rawlavrgfar);flashaddr+=2;
			STMFLASH_Unlock();STMFLASH_WriteHalfWord(FLASH_ADDR_IRDATA+flashaddr,rawravrgnear);flashaddr+=2;
			STMFLASH_Unlock();STMFLASH_WriteHalfWord(FLASH_ADDR_IRDATA+flashaddr,rawravrgfar);flashaddr+=2;
			
			ledon(0);delay_ms(300);ledoff(0);delay_ms(300);
			biaodingmode=0;
			while(1)
			{
				sleepmode=1; 
				if(c==9||c==0)c=1;
				ledoff(0);ledon(c);delay_ms(1000);c++;
			}
			break;
 
		case 7:				 
 
		freemode=1;	
		flashaddr=0;
			for(blockx=0;blockx<16;blockx++)
			{for(blocky=0;blocky<16;blocky++)
			{GucMapBlock[blockx][blocky]=STMFLASH_ReadHalfWord(FLASH_ADDR_BLOCK+flashaddr);flashaddr+=2;}}
			flashaddr=0;
			GucXStart=STMFLASH_ReadHalfWord(FLASH_ADDR_BLOCK_POINT+flashaddr);flashaddr+=2;
			GucYStart=STMFLASH_ReadHalfWord(FLASH_ADDR_BLOCK_POINT+flashaddr);flashaddr+=2;
			GoalX=STMFLASH_ReadHalfWord(FLASH_ADDR_BLOCK_POINT+flashaddr);flashaddr+=2;
			GoalY=STMFLASH_ReadHalfWord(FLASH_ADDR_BLOCK_POINT+flashaddr);flashaddr+=2;

			printf("block picture:\r\n");
			Show_Block();
			
//		
			while(1)
			{
				sleepmode=1; 
				if(c==9||c==0)c=1;
				ledoff(0);ledon(c);delay_ms(1000);c++;
			}
 
		case 8:				 
 
			while(1)
			{
				sleepmode=1; 
				if(c==9||c==0)c=1;
				ledoff(0);ledon(c);delay_ms(500);c++;
			}	   
		
		
		default:
			while(1)
			{
				sleepmode=1; 
				ledoff(0);ledon(c);delay_ms(1000);c++;
				if(c==9) c=1;
			}
	}
		
		
	
#ifdef MOUSE_REMOTE
maze_run: /* 远程档发车入口(值守循环 goto 到此; 每次发车重跑下面的
          * Flash 擦除/读图/parameter_search, 即"每次远程发车都是一轮新探索") */
    /* P2 重复发车状态复位: 以下均为全局量, 上次跑位后被改写, 不清则二次跑叠加/错乱 */
    n = 0;               /* 交叉点栈索引(全局, 搜索中递增) */
    GucMouseTask = WAIT; /* 上次跑位停在 END(=6), 不清则二次跑直接进 case END 空转 */
    spurt_flag = 0;      /* 上次跑位置 1, 不清则二次跑 sleepmode 不复位 => 冲刺趴窝 */
#endif
	#if 1
		freemode=1;	
		#ifdef FLASHSAVE
			erase_run_data();
		#endif
			
		STMFLASH_Unlock(); STMFLASH_ErasePage(FLASH_ADDR_BLOCK);
		STMFLASH_Unlock(); STMFLASH_ErasePage(FLASH_ADDR_BLOCK_POINT);
		STMFLASH_Unlock(); STMFLASH_ErasePage(FLASH_ADDR_GOPATH);
		STMFLASH_Unlock(); STMFLASH_ErasePage(FLASH_ADDR_BACKPATH);
		STMFLASH_Unlock(); STMFLASH_ErasePage(FLASH_ADDR_GOPATH_90);
		STMFLASH_Unlock(); STMFLASH_ErasePage(FLASH_ADDR_BACKPATH_90);
			
		freemode = 0;
	#endif
	
	flashaddr=0;
	flashaddrmax=STMFLASH_ReadHalfWord(FLASH_ADDR_BLOCK);
	if(flashaddrmax!=65535)
	{
		flashaddr=0;
		for(blockx=0;blockx<16;blockx++)
		{
			for(blocky=0;blocky<16;blocky++)
			{
				GucMapBlock[blockx][blocky]=STMFLASH_ReadHalfWord(FLASH_ADDR_BLOCK+flashaddr); flashaddr+=2;
			}
		}
		
		flashaddr=0;
		GucXStart=STMFLASH_ReadHalfWord(FLASH_ADDR_BLOCK_POINT+flashaddr); flashaddr+=2;
		GucYStart=STMFLASH_ReadHalfWord(FLASH_ADDR_BLOCK_POINT+flashaddr); flashaddr+=2;
		GoalX=STMFLASH_ReadHalfWord(FLASH_ADDR_BLOCK_POINT+flashaddr); flashaddr+=2;
		GoalY=STMFLASH_ReadHalfWord(FLASH_ADDR_BLOCK_POINT+flashaddr); flashaddr+=2;
		goal_keep_flag=STMFLASH_ReadHalfWord(FLASH_ADDR_BLOCK_POINT+flashaddr); flashaddr+=2;
		SearchOrSpurt_Flag=STMFLASH_ReadHalfWord(FLASH_ADDR_BLOCK_POINT+flashaddr); flashaddr+=2;
		
		GmcMouse.cX=GucXStart;
		GmcMouse.cY=GucYStart;
		 
		delay_ms(100);ledon(1);delay_ms(120);ledoff(0);delay_ms(120);ledon(1);delay_ms(120);ledoff(0);delay_ms(120);
	}
	
	
	
	

	
	
 
	parameter_search();
	
	if(GucMouseTask!=SPURT_45 && GucMouseTask!=SPURT_90)
	{
		while(1)
		{
			if(disf2<20){goaheadnum=2; break;} 
 
		}
		ledon(6);delay_ms(1000);ledoff(0);
	}
	flashenable=0;			 
	irswitch=1;
	biaodingmode=0;
	setangle(0);			 
	BackSearchFlag=1;

 
	while (1)
	{ 
		
#ifdef MOUSE_REMOTE
        /* P2 协作式中止: MAZE STOP/ESTOP/STOP 经 LP_StopMotion 置 linkRunAbort,
         * timer.c SysTick 已把 task/track/tick/setvl/setvr 强制清零(各类忙等循环
         * 随即退出); 此处退出状态机, 经 maze_done 复位链路状态回待命值守循环 */
        if (LP_AbortRequested()) goto maze_done;
#endif
        switch (GucMouseTask) {                                          
            
        case WAIT:
             
			taskacc();settrack(100);
            GucMouseTask = START;
            break;
            
        case START:                                                      
	        mazeSearch();                                                
            if (GucMapBlock[GmcMouse.cX][GmcMouse.cY] & 0x08) {          
                GucXStart   = MAZETYPE - 1;                              
                GmcMouse.cX = MAZETYPE - 1;                                  
                 
				 
						if(GmcMouse.cY<MAZETYPE - 1)
						{
							GucMapBlock[MAZETYPE - 1][GmcMouse.cY+1] = GucMapBlock[0][GmcMouse.cY+1];
							GucMapBlock[0][GmcMouse.cY+1] = 0xf0;
						}
                ucTemp = GmcMouse.cY;
                do {
                    GucMapBlock[MAZETYPE - 1][ucTemp] = GucMapBlock[0][ucTemp];
										GucMapBlock[MAZETYPE - 2][ucTemp] = 0xD0;
                    GucMapBlock[0 ][ucTemp] = 0xf0;
										GucMapBlock[1 ][ucTemp] = 0xf0;
                }while (ucTemp--);
								
							GucMapBlock[MAZETYPE - 2][GmcMouse.cY] = 0xf0;
							GoalX=8;
                 
                GmcCrossway[n].cX = MAZETYPE - 1;
                GmcCrossway[n].cY = 0;
                n++;
                GucMouseTask = MAZESEARCH;                               
            }
            else if (GucMapBlock[GmcMouse.cX][GmcMouse.cY] & 0x02) {          
                 
                GmcCrossway[n].cX = 0;
                GmcCrossway[n].cY = 0;
                n++;
                GucMouseTask = MAZESEARCH;                               
            }
            break;

 
        case MAZESEARCH:
      //分支A
			if(((GmcMouse.cX==GucXGoal0)||(GmcMouse.cX==GucXGoal1))&&((GmcMouse.cY==GucYGoal0)||(GmcMouse.cY==GucYGoal1))) //&&forwardhaswall==1
			{
				if((GmcMouse.cX+GmcMouse.cY+GucMouseDir)%2==0) goal_keep_flag=2;   
				else goal_keep_flag=1; 
						printf("goal_keep_flag=%d, BackSearchFlag =%d, \n  GmcMouse.cX==%d ,GmcMouse.cY==%d,GucMouseDir=%d\n forwardhaswall=%d", goal_keep_flag, BackSearchFlag,GmcMouse.cX,GmcMouse.cY,GucMouseDir,forwardhaswall);
				GoalGet=1;
				GoalX=GmcMouse.cX;
				GoalY=GmcMouse.cY;
				goalWallchange(GoalX,GoalY);
				
				mapStepEditS(GucXStart,GucYStart);
				mapStepEditSC();
				mapStepEdit(GucXStart,GucYStart);
				mapStepEditC();
				mapStepEditS(GoalX,GoalY);
				mapStepEdit(GoalX,GoalY);
				
				Block_Write();
 
 
 
			if(BackSearchFlag==0)
			{
				 
				 
			//	mouseGoahead(1);
						tick=0;
						turnaround_tick=0;
						task=KEEPWALLDISTANCE;
						while(tick<50)
										 
														if(goal_keep_flag!=0)
										{ 
											if(goal_keep_flag==1)
											{
												taskturn(POINTL90);
												tick=0;
												turnaround_tick=0;
												task=KEEPWALLDISTANCE;
												while(tick<50);
												taskpointturnleft();
											}
											else if(goal_keep_flag==2)
											{
												taskturn(POINTR90);
												tick=0;
												turnaround_tick=0;
												task=KEEPWALLDISTANCE;
												while(tick<50);
												taskpointturnright();
											}	
										}
										else
										{specialmouseTurnback();}
										 
				LTopSpeedh=LTopSpeed;L45TopSpeedh=L45TopSpeed;BackTopSpeedh=BackTopSpeed;
				LTopSpeed=70;L45TopSpeed=140;BackTopSpeed=70;
				fastway_90(GucXStart,GucYStart);
 
				//parameter_spurt(LOW_90);
				LTopSpeed=LTopSpeedh;L45TopSpeed=L45TopSpeedh;BackTopSpeed=BackTopSpeedh;
				 
				specialmouseTurnback();
											
					GmcMouse.cX=GucXStart;
					GmcMouse.cY=GucYStart;
					GucMouseDir=0;
										
				parameter_spurt();					
				GucMouseTask = SPURT;                                
				break;
			}
 
  printf("GucMapStep[GucXStart][GucYStart]=%d   GucMapStepS[GucXStart][GucYStart]=%d  \n",GucMapStep[GucXStart][GucYStart],GucMapStepS[GucXStart][GucYStart]);
				if((GucMapStep[GucXStart][GucYStart]<=stepMax)||(GucMapStep[GucXStart][GucYStart]-GucMapStepS[GucXStart][GucYStart]==0))
				{
					objectGoTo(GucXStart,GucYStart);
					specialmouseTurnback();
					printf("go to start\n");
					parameter_spurt();
					GucMouseTask = SPURT; 
					break;
				}
				else
				{
					printf("go to search\n");
					while (--n)
					{
						ucRoadStat = crosswayCheck(GmcCrossway[n].cX,GmcCrossway[n].cY);
						tempX=GmcCrossway[n].cX;
						tempY=GmcCrossway[n].cY;

						if (ucRoadStat)
						{
							if(GucMapStepC[tempX][tempY]+GucMapStepS[tempX][tempY]<GucMapStep[GucXStart][GucYStart])
							{
								objectGoTo(GmcCrossway[n].cX,GmcCrossway[n].cY);                       
								shortwayX=GmcCrossway[n].cX;
								shortwayY=GmcCrossway[n].cY;
								k=n;
								if (ucRoadStat > 1) 
								{
									k++;
								}
								mapStepEdit(GoalX,GoalY);
								backpointwaychoice(tempX,tempY);
								mazeSearch();
								break;
							}
						}
					}
					if (n == 0)
					{
						objectGoTo(GmcCrossway[0].cX, GmcCrossway[0].cY);
						specialmouseTurnback();
						parameter_spurt();		
						GucMouseTask = SPURT;                                
						break;
					}
				}
			}
       //分支B
			else
			{
         
				if(GoalGet==0)
				{
					ucRoadStat = crosswayCheck(GmcMouse.cX,GmcMouse.cY);         
					gox=GmcMouse.cX;
					goy=GmcMouse.cY;
					if (ucRoadStat) {                                            
						m=deadwaycheck(n);
						if(m==n)
						{
							if (ucRoadStat > 1) {                                    
								GmcCrossway[n].cX = GmcMouse.cX;
								GmcCrossway[n].cY = GmcMouse.cY;
								n++;
							}
						}
						else
						{
							gox=GmcCrossway[m].cX;
							goy=GmcCrossway[m].cY;
							n=m+1;
							objectGoTo(gox,goy);
						}
						crosswayChoice();
						mazeSearch();                                            
					} 
					else 
					{                                                     
						m=deadwaycheck(n);
						if(m<n) n=m+1;
						while (--n) {
							ucRoadStat = crosswayCheck(GmcCrossway[n].cX,
													   GmcCrossway[n].cY);
																				 
																				
							 
							if (ucRoadStat) {
								m=n;
								mapStepEditS(GoalX,GoalY);
								if(m>0)
								{
									tempCX1=GmcCrossway[m].cX;
									tempCY1=GmcCrossway[m].cY;
									l=m;
									tempCX2=GmcCrossway[m].cX;
									tempCY2=GmcCrossway[m].cY;
									gox=tempCX1;
									goy=tempCY1;
									while(l)
									{
										l--;
										ucRoadStat = crosswayCheck(GmcCrossway[l].cX,GmcCrossway[l].cY);
										if (ucRoadStat)
										{
											tempCX2=GmcCrossway[l].cX;
											tempCY2=GmcCrossway[l].cY;
											if(GucMapStepS[tempCX1][tempCY1]>GucMapStepS[tempCX2][tempCY2])
											{
												m=l;
												tempCX1=GmcCrossway[m].cX;
												tempCY1=GmcCrossway[m].cY;
												gox=GmcCrossway[m].cX;
												goy=GmcCrossway[m].cY;
												continue;
											}
											else
											{
												break;
											}
										}
									}									
								}

								objectGoTo(gox,goy); 
								ucRoadStat = crosswayCheck(gox,goy);
								n++;
								if (ucRoadStat > 1) {
									GmcCrossway[n].cX=GmcCrossway[m].cX;
									GmcCrossway[n].cY=GmcCrossway[m].cY;
									n++;
								}
								crosswayChoice();
								mazeSearch(); 
								break;
							}
						}
					    if (n == 0) {                                            
								objectGoTo(GmcCrossway[0].cX, GmcCrossway[0].cY);
								specialmouseTurnback();
								parameter_spurt();		
								GucMouseTask = SPURT;                                
								break;
						}
					}
				}
 
				else
				{
SearchJump:					mapStepEditS(GucXStart,GucYStart);
					mapStepEditSC();
					mapStepEdit(GucXStart,GucYStart);
					mapStepEditC();
					mapStepEditS(GoalX,GoalY);
					mapStepEdit(GoalX,GoalY);
					
					ucRoadStat = crosswayCheck(GmcMouse.cX,GmcMouse.cY);         
					
					if (ucRoadStat                                            
						&&(GucMapStepC[shortwayX][shortwayY]+GucMapStepS[shortwayX][shortwayY]<GucMapStep[GucXStart][GucYStart])) 
					{
						if (ucRoadStat > 1) {                                    
							GmcCrossway[k].cX = GmcMouse.cX;
							GmcCrossway[k].cY = GmcMouse.cY;
							k++;	
						}
						if((GucMapStep[GucXStart][GucYStart]<=stepMax)||(GucMapStep[GucXStart][GucYStart]-GucMapStepS[GucXStart][GucYStart]==0))
						{
							
							Block_Write();
							mouseTurnback();
							objectGoTo(GucXStart,GucYStart);
							specialmouseTurnback();
							parameter_spurt();		
							GucMouseTask = SPURT; 
							break;
						}
						crosswayChoice();                                        
						mazeSearch();                                            
					} 
					else
					{	
						if((GucMapStep[GucXStart][GucYStart]<=stepMax)||(GucMapStep[GucXStart][GucYStart]-GucMapStepS[GucXStart][GucYStart]==0))
						{
							Block_Write();
							mouseTurnback();
							objectGoTo(GucXStart,GucYStart);
							specialmouseTurnback();
							parameter_spurt();		
							GucMouseTask = SPURT; 
							break;
						}
						else
						{
							if((GucMapStep[shortwayX][shortwayY]-GucMapStepS[shortwayX][shortwayY]>0)
								&&(GucMapStepC[shortwayX][shortwayY]+GucMapStepS[shortwayX][shortwayY]<GucMapStep[GucXStart][GucYStart]))
							{
								while (k>n)
								{
									k--;
									ucRoadStat = crosswayCheck(GmcCrossway[k].cX,GmcCrossway[k].cY);
									tempX=GmcCrossway[k].cX;
									tempY=GmcCrossway[k].cY;
									if (ucRoadStat)
									{
										if((GucMapStep[tempX][tempY]-GucMapStepS[tempX][tempY]>0)
											&&(GucMapStepC[tempX][tempY]+GucMapStepS[tempX][tempY]<GucMapStep[GucXStart][GucYStart]))
										{
											objectGoTo(GmcCrossway[k].cX,GmcCrossway[k].cY);
											if (ucRoadStat > 1) 
											{
												k++;
											}
											mapStepEdit(GoalX,GoalY);
											backpointwaychoice(tempX,tempY);
											mazeSearch();
											goto next;
										}
									}
								}
								if (k == 0) {                                            
									objectGoTo(GmcCrossway[0].cX, GmcCrossway[0].cY);
									specialmouseTurnback();
									parameter_spurt();		
									GucMouseTask = SPURT;                                
									break;
								}
							}
							while (--n)
							{
								ucRoadStat = crosswayCheck(GmcCrossway[n].cX,GmcCrossway[n].cY);
								tempX=GmcCrossway[n].cX;
								tempY=GmcCrossway[n].cY;
								if (ucRoadStat)
								{
									if(GucMapStepC[tempX][tempY]+GucMapStepS[tempX][tempY]<GucMapStep[GucXStart][GucYStart])
									{
										if((GucMapStep[tempX][tempY]-GucMapStepS[tempX][tempY]>0))
										{
											objectGoTo(GmcCrossway[n].cX,GmcCrossway[n].cY);                       
											shortwayX=GmcCrossway[n].cX;
											shortwayY=GmcCrossway[n].cY;
											k=n;
											if (ucRoadStat > 1) 
											{
												k++;
											}
											mapStepEdit(GoalX,GoalY);
											backpointwaychoice(tempX,tempY);
											mazeSearch();
											break;
										}
									}
								}
							}
							if (n == 0)
							{
								
								objectGoTo(GmcCrossway[0].cX, GmcCrossway[0].cY);
								specialmouseTurnback();
								parameter_spurt();		
								GucMouseTask = SPURT;                                
								break;
							}
						}
						if(stepMax<stepMaxLimit)
						{
							stepMax++;
						}
					}
				}
			}
next:		break;

 
/*		case SPURT:
			printf("????: GoalX=%d, GoalY=%d\n", GoalX, GoalY);
	     printf("???1: GucMapStep[GucXStart][GucYStart]=%d, stepMax=%d, diff=%d\n", GucMapStep[GucXStart][GucYStart], stepMax, GucMapStep[GucXStart][GucYStart]-GucMapStepS[GucXStart][GucYStart]);
			settrack(120);
		
		//	objectGoTo(GoalX, GoalY);
		//taskturnaround();
		fastway(GoalX, GoalY);
		spurt(LOW);
speedmode=LOW;
		taskturn(RT45L);
		taskstraight45(254*6);
		taskturn(RT45L);
	
		
		//	mouseTurnback();
		//	objectGoTo(0, 0);
			//specialmouseTurnback();
	*/	
		//	GucMouseTask = END;                             
	//		break;
		
	//	case SPURT_45:
			case SPURT:
		
		printf("GoalX=%d, GoalY=%d, GucMouseDir=%d, FastwayCount=%d\n", GoalX, GoalY, GucMouseDir, FastwayCount);  //打印终点坐标和最短路径距离
	     printf(" GucMapStep[GucXStart][GucYStart]=%d, stepMax=%d, diff=%d\n", GucMapStep[GucXStart][GucYStart], stepMax, GucMapStep[GucXStart][GucYStart]-GucMapStepS[GucXStart][GucYStart]);
			sleepmode=1;
			fastway(GoalX,GoalY);
			flashaddr=0;
			STMFLASH_Unlock();STMFLASH_ErasePage(FLASH_ADDR_GOPATH);
			STMFLASH_Unlock();STMFLASH_WriteHalfWord(FLASH_ADDR_GOPATH+flashaddr,FastwayCount);flashaddr+=2;
			for(tmptt=0;tmptt<200;tmptt++)
			{
				STMFLASH_Unlock();STMFLASH_WriteHalfWord(FLASH_ADDR_GOPATH+flashaddr,FastwayAction[tmptt].action);flashaddr+=2;
			}
     printf("Start pose: (%d,%d) Dir=%d\n", GmcMouse.cX, GmcMouse.cY, GucMouseDir);   //打印起点和是否冲刺
#ifdef MOUSE_REMOTE
            /* BLIND: 路径已由上方 fastway 算好并写 Flash(GOPATH 写段), 跳过物理冲刺,
             * 直接进 END => maze_done 回待命态 */
            if (mazeBlind) { GucMouseTask = END; break; }
#endif
      		if(spurt_flag==0)
				{					sleepmode=0;		

				}
			spurt_45_end_flag=1;
			spurt(LOW);
			spurt_flag=1;
    for (tmptt=0;tmptt<FastwayCount;tmptt++){
       printf("act[%d]=%d   seq[%d]=%d  \n", tmptt, FastwayAction[tmptt].action, tmptt, actionsequence[tmptt]);     //打印生成45度冲刺的动作序列
		}	    
		
			spurt_45_end_flag=0;
			ledon(5);ledon(6);ledon(7);ledon(8);
		printf(" GucMouseTask=%d\n",GucMouseTask);
			break;
			

	/*		if(goal_keep_flag!=0)
			{
				if(goal_keep_flag==1)
				{
					taskturn(POINTL90);
					tick=0;
					turnaround_tick=0;
					task=KEEPWALLDISTANCE;
					while(tick<50);
					taskpointturnleft();
				}
				else if(goal_keep_flag==2)
				{
					taskturn(POINTR90);
					tick=0;
					turnaround_tick=0;
					task=KEEPWALLDISTANCE;
					while(tick<50);
					taskpointturnright();
				}	
			}

			if(BackSearchFlag==1)
			{
			ledoff(0);
			accdistance=232;
			taskacc();
			accdistance=40;
			GmcMouse.cX=GoalX;
			GmcMouse.cY=GoalY;
			settrack(0);
			__mouseCoorUpdate();
			BackSearchFlag+=1;
			GucMouseTask = MAZESEARCH;                               // 电脑鼠将开始冲刺状态        
			goto SearchJump;
			}

			flashaddr=0;
			FastwayCount_90=STMFLASH_ReadHalfWord(FLASH_ADDR_BACKPATH_90+flashaddr);flashaddr+=2;
			for(tmptt=0;tmptt<200;tmptt++)
			{
				FastwayAction_90[tmptt].action=STMFLASH_ReadHalfWord(FLASH_ADDR_BACKPATH_90+flashaddr);flashaddr+=2;
			}
			GoOrBackFla
			.
			.........................................
			.
			g=1;
			back90_flag=1;
			spurt_90(LOW_90);
			back90_flag=0;
			GoOrBackFlag=0;
			spurt_back_mouseTurnback();
			

				
			if(goaheadnum==1||goaheadnum==2)
			{
				goaheadnum=3;//高速斜线冲
				LTopSpeed=240;
				L45TopSpeed=180;
				BackTopSpeed=240;
				ledon(5);ledon(6);ledon(7);ledon(8); delay_ms(500);
				ledoff(0);
				GmcMouse.cX=GucXStart;
				GmcMouse.cY=GucYStart;
				GucMouseDir=0;
				GucMouseTask=SPURT_45;
			}
			if(goaheadnum==3)
			{
				goaheadnum=4;//超高速斜线冲
				LTopSpeed=240;
				L45TopSpeed=180;
				BackTopSpeed=240;
				ledon(5);ledon(6);ledon(7);ledon(8); delay_ms(500);
				ledoff(0);
				GmcMouse.cX=GucXStart;
				GmcMouse.cY=GucYStart;
				GucMouseDir=0;
				GucMouseTask=SPURT_45;
			}
			else if(goaheadnum==4||goaheadnum==0) 
			while(1)
			{{
				sleepmode=1;
				if(c==9||c==0)c=1;
				ledoff(0);ledon(c);delay_ms(1000);c++;
			}}
			
			break;
			
 */
		case SPURT_90:
			break;


#ifdef MOUSE_REMOTE
		case END:
			/* 远程档: 不进 LED 死循环, 退出状态机经 maze_done 回待命态(等下一次 MAZE RUN) */
			goto maze_done;
#else
		case END:
			sleepmode = 1;
			while (1)
			{
				if (c == 9 || c == 0)c = 1;
				ledoff(0); ledon(c); delay_ms(1000); c++;
			}
			break;
#endif
			
        default:
            break;
        }
    }
#ifdef MOUSE_REMOTE
maze_done:
    /* 一次跑位结束(正常到 END, 或被 STOP/ESTOP 中止跳出状态机):
     * 复位链路"跑位中"标志使下一次 MAZE RUN 可被接受, 回 fuction_Choose 待命值守 */
    LINK_MazeFinished();
    goto fuction_Choose;
#endif
}
		

