#include "timer.h"
#include "sys.h"
#include "usart.h"	
#include "utility.h"
#ifndef MOUSE_REMOTE
#include "search_guard_v5.h"
#endif
#include "stratagy.h"
#include "led.h"
#include "stmflash.h"

#ifndef MOUSE_REMOTE
#define FLASHSAVE /* remote build: flight recorder OFF - TELEM streams live data to host instead; frees ~9.5KB RAM for the link layer */
#endif
u8 block_write_flag=0;
extern u8 task;
extern u8 goal_keep_flag;
extern u8 blockx,blocky; 
extern uchar    GucMapBlock[MAZETYPE][MAZETYPE]; 
extern uchar    GucXStart;                 
extern uchar    GucYStart;                 
extern  char GoalX;
extern char GoalY;
extern  uchar n;
extern uchar k;
extern MAZECOOR GmcCrossway[MAZETYPE * MAZETYPE];

#ifndef FLASHSAVE
const u16 save_num=1;
const u8 per_timer=2;
#endif
 
#ifdef FLASHSAVE
const u16 save_num=1136; /* V5: reserve 256-byte snapshots and 256 extra stack bytes. */
volatile V5_STOP_DIAGNOSTICS v5_diag={0};
const u8 per_timer=2;
#endif
 
extern int flashaddr;
extern MAZECOOR GmcMouse;    
extern long disf1,disf2,disf,disl,disr;
extern long track;
extern int gangle,setgangle;
extern int speedl,speedr;  
extern long setvl,setvr; 

 
void STMFLASH_Unlock(void)
{ FLASH->KEYR=FLASH_KEY1; 
  FLASH->KEYR=FLASH_KEY2;
}
 
void STMFLASH_Lock(void)
{
  FLASH->CR|=1<<7; 
}
 
u8 STMFLASH_GetStatus(void)
{	
	u32 res;		
	res=FLASH->SR; 
	if(res&(1<<0))return 1;		     
	else if(res&(1<<2))return 2;	 
	else if(res&(1<<4))return 3;	 
	return 0;						 
}
 
 
 
u8 STMFLASH_WaitDone(u16 time)
{
	u8 res;
	do
	{
		res=STMFLASH_GetStatus();
		if(res!=1)break; 
		delay_us(1);
		time--;
	 }while(time);
	 if(time==0)res=0xff; 
	 return res;
}
 
 
 
u8 STMFLASH_ErasePage(u32 paddr)
{
	u8 res=0;
	res=STMFLASH_WaitDone(0X5FFF); 
	if(res==0)
	{ 
		FLASH->CR|=1<<1; 
		FLASH->AR=paddr; 
		FLASH->CR|=1<<6; 
		res=STMFLASH_WaitDone(0X5FFF); 
		if(res!=1) 
		{
			FLASH->CR&=~(1<<1); 
		}
	}
	return res;
}
 
 
 
 
u8 STMFLASH_WriteHalfWord(u32 faddr, int dat)
{
	u8 res;	   	    
	res=STMFLASH_WaitDone(0XFF);
	if(res==0) 
	{
		FLASH->CR|=1<<0; 
		*(vs16*)faddr=dat; 
		res=STMFLASH_WaitDone(0XFF); 
		if(res!=1) 
		{
			FLASH->CR&=~(1<<0); 
		}
	} 
	return res;
} 
 
 
 
int STMFLASH_ReadHalfWord(u32 faddr)
{	
	return *(vs16*)faddr; 
}
	 			  	
 
 
 
 
 
				
 






 
RUN_DATA run_data[save_num];

void write_data(u16 num)
{
 
 
	run_data[num].gangle=gangle;
	run_data[num].task=task;
 
 
 
 
 
 
 
	run_data[num].speedl=speedl;
	run_data[num].speedr=speedr;
 
}

void save_data(u16 num)
{
 
 
 
 
 
 
 
	STMFLASH_Unlock();STMFLASH_WriteHalfWord(FLASH_ADDR_DATA+flashaddr,run_data[num].task);flashaddr+=2;
	STMFLASH_Unlock();STMFLASH_WriteHalfWord(FLASH_ADDR_DATA+flashaddr,run_data[num].gangle);flashaddr+=2;
 
 
 
	STMFLASH_Unlock();STMFLASH_WriteHalfWord(FLASH_ADDR_DATA+flashaddr,run_data[num].speedl);flashaddr+=2;
	STMFLASH_Unlock();STMFLASH_WriteHalfWord(FLASH_ADDR_DATA+flashaddr,run_data[num].speedr);	
}

u16 printf_line=1;
void print_data ()
{   int data_temp=0,data_temp1=0,repeat_time=0;
		flashaddr=0;
		data_temp=STMFLASH_ReadHalfWord(FLASH_ADDR_DATA_NUM+flashaddr);flashaddr+=2;printf("The Time Has Saved:%d\r\n",data_temp);
		data_temp1=STMFLASH_ReadHalfWord(FLASH_ADDR_DATA_NUM+flashaddr);flashaddr+=2;printf("The Number of The Current Time:%d\r\n",data_temp1);
		if(data_temp==1){ repeat_time=data_temp1+1;} else repeat_time=save_num+1;
 
		printf("task,track,speedl,speedr,\r\n");
	
	  flashaddr=0;
		printf("task:\r\n");
		for(printf_line=1;printf_line<repeat_time;printf_line++)
		{
			data_temp=STMFLASH_ReadHalfWord(FLASH_ADDR_DATA+flashaddr);flashaddr+=2*4;printf("%d,",data_temp);
		}
		printf("\r\n");
		
			  flashaddr=2;
		printf("gangle:\r\n");
		for(printf_line=1;printf_line<repeat_time;printf_line++)
		{
			data_temp=STMFLASH_ReadHalfWord(FLASH_ADDR_DATA+flashaddr);flashaddr+=2*4;printf("%d,",data_temp);
		}
		printf("\r\n");
		
			  flashaddr=4;
		printf("speedl:\r\n");
		for(printf_line=1;printf_line<repeat_time;printf_line++)
		{
			data_temp=STMFLASH_ReadHalfWord(FLASH_ADDR_DATA+flashaddr);flashaddr+=2*4;printf("%d,",data_temp);
		}
		printf("\r\n");
		
			  flashaddr=6;
		printf("speedr:\r\n");
		for(printf_line=1;printf_line<repeat_time;printf_line++)
		{
			data_temp=STMFLASH_ReadHalfWord(FLASH_ADDR_DATA+flashaddr);flashaddr+=2*4;printf("%d,",data_temp);
		}
		printf("\r\n");
		
 
 
}

void erase_run_data()
{
		for(flashaddr=0;flashaddr<(save_num/100)+1;flashaddr++)
		{STMFLASH_Unlock();STMFLASH_ErasePage(FLASH_ADDR_DATA+flashaddr*0x400);}
 		STMFLASH_Unlock();STMFLASH_ErasePage(FLASH_ADDR_DATA_NUM);
}

extern u8 SearchOrSpurt_Flag;
void SaveBlockData()
{
	STMFLASH_Unlock();STMFLASH_ErasePage(FLASH_ADDR_BLOCK_POINT);
    STMFLASH_Unlock();STMFLASH_ErasePage(FLASH_ADDR_BLOCK);
			flashaddr=0;
		for(blockx=0;blockx<16;blockx++)
		{for(blocky=0;blocky<16;blocky++)
			{
				STMFLASH_Unlock();STMFLASH_WriteHalfWord(FLASH_ADDR_BLOCK+flashaddr,GucMapBlock[blockx][blocky]);flashaddr+=2;
			}}
			
			flashaddr=0;
		for(blockx=0;blockx<16;blockx++)
		{for(blocky=0;blocky<16;blocky++)
			{
				GucMapBlock[blockx][blocky]=STMFLASH_ReadHalfWord(FLASH_ADDR_BLOCK+flashaddr);flashaddr+=2;
			}}	
		flashaddr=0;
    STMFLASH_Unlock();STMFLASH_WriteHalfWord(FLASH_ADDR_BLOCK_POINT+flashaddr,GucXStart);flashaddr+=2;
		STMFLASH_Unlock();STMFLASH_WriteHalfWord(FLASH_ADDR_BLOCK_POINT+flashaddr,GucYStart);flashaddr+=2;
		STMFLASH_Unlock();STMFLASH_WriteHalfWord(FLASH_ADDR_BLOCK_POINT+flashaddr,GoalX);flashaddr+=2;
		STMFLASH_Unlock();STMFLASH_WriteHalfWord(FLASH_ADDR_BLOCK_POINT+flashaddr,GoalY);flashaddr+=2;
		STMFLASH_Unlock();STMFLASH_WriteHalfWord(FLASH_ADDR_BLOCK_POINT+flashaddr,goal_keep_flag);flashaddr+=2;
		STMFLASH_Unlock();STMFLASH_WriteHalfWord(FLASH_ADDR_BLOCK_POINT+flashaddr,SearchOrSpurt_Flag);flashaddr+=2;

}
		
void SaveSearch_Stack()
		{ u8 temp_num;
			STMFLASH_Unlock();STMFLASH_ErasePage(FLASH_ADDR_BLOCK_STACK);
			flashaddr=0;
		 for(temp_num=0;temp_num<n+1;temp_num++)
			{STMFLASH_Unlock();STMFLASH_WriteHalfWord(FLASH_ADDR_BLOCK_STACK+flashaddr,GmcCrossway[temp_num].cX);flashaddr+=2;
			 STMFLASH_Unlock();STMFLASH_WriteHalfWord(FLASH_ADDR_BLOCK_STACK+flashaddr,GmcCrossway[temp_num].cY);flashaddr+=2;}
    }
		
void Block_Write()
{
	block_write_flag=1;
}












