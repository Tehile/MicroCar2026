#ifndef _FLASH_H_
#define _FLASH_H_
#include "main.h"
/*-----------------------------------------  I N C L U D E S  -----------------------------------------*/
#include "sys.h"
/*---------------------------------------  D E F I N I T I O N  ---------------------------------------*/

/* Flash 底层配置 */
#define STM32_FLASH_SIZE 128		// 所选STM32的FLASH容量大小(单位为K)
#define STM32_FLASH_WREN 1			// 使能FLASH写入(0=不使能, 1=使能)
#define STM32_FLASH_BASE 0x08000000 // STM32 FLASH的起始地址
// FLASH解锁键值
#define FLASH_KEY1 0X45670123
#define FLASH_KEY2 0XCDEF89AB

/* 存储数据结构体类型 */
struct run_data
{
	u8 motionTask;           /* 运动状态机任务码 */
	s16 gangle, speedl, speedr; /* 航向角, 左轮速度, 右轮速度 */
};
typedef struct run_data RunData;

/* FLASH 存储区地址规划 */
#define FLASH_ADDR_DATA_NUM 0X08016000 // page 88
#define FLASH_ADDR_DATA 0X08016400	   // page 89

#define FLASH_ADDR_BLOCK_STACK 0X0801D800 // page 118
#define FLASH_ADDR_BLOCK_POINT 0X0801DC00 // page 119
#define FLASH_ADDR_BLOCK 0X0801E000		  // page 120
#define FLASH_ADDR_GOPATH_90 0X0801E400	  // page 121
#define FLASH_ADDR_BACKPATH_90 0X0801E800 // page 122

#define FLASH_ADDR_IRDATA 0X0801EC00   // page 123
#define FLASH_ADDR_GOPATH 0X0801F000   // page 124
#define FLASH_ADDR_BACKPATH 0X0801F800 // page 126
#define FLASH_ADDR_GYRO0 0X0801FC00	   // page 127

/* ---- extern 声明(定义见 flash.c) ---- */
extern const u16 saveNum; /* 运行数据存储上限(FLASHSAVE=1200) */
extern const u8 perTimer; /* 每次存储间隔(定时周期数) */

/*-------------------------------------  F U N C T I O N S  -------------------------------------*/
/* ---- Flash 底层驱动 ---- */
void FLASH_Unlock(void);					// FLASH解锁
void FLASH_Lock(void);						// FLASH上锁
u8 FLASH_GetStatus(void);					// 获得状态
u8 FLASH_WaitDone(u16 time);				// 等待操作结束
u8 FLASH_ErasePage(u32 paddr);				// 擦除页
u8 FLASH_WriteHalfWord(u32 faddr, int dat); // 写入半字
int FLASH_ReadHalfWord(u32 faddr);			// 读出半字

/* ---- 迷宫/运行数据存储业务 ---- */
void FLASH_EraseRunData(void);   /* 擦除运行数据区 */
void FLASH_WriteData(u16 num);   /* 写入运行数据缓冲 */
void FLASH_SaveData(u16 num);    /* 保存运行数据到Flash */
void FLASH_PrintData(void);      /* 打印Flash中运行数据 */
void FLASH_SaveBlockData(void);  /* 保存迷宫墙信息 */
void FLASH_SaveSearchStack(void);/* 保存搜索交叉点栈 */
void FLASH_BlockWrite(void);     /* 置位块写入标志 */

#endif
