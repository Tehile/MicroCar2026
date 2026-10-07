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

/* FLASH 存储区地址规划（2026-10-04 调整, 见 docs/比赛固件空间不足诊断与瘦身方案.md）
 *   程序区 : page   0..100 → 101 KB（uvprojx IROM1 上限 0x19400, 与本文件 DATA_NUM 严格对齐）
 *   数据区 : page 101..127
 *     page 101      运行数据计数/条数
 *     page 102..114 运行数据录制区（**擦除范围正好 13 页** —— 由 FLASH_EraseRunData 的
 *                   saveNum/100+1 决定, saveNum=1200 → 13 页; 见 flash.c）
 *     page 115      PC 链路模块的生产标定页（见 link.h）
 *     page 116..117 备用（page 116 原为"课程参数页", 随课程实验层在 v2.4 一并删除）
 *     page 118..127 迷宫墙/起终点/90°路径/红外标定/常规路径/陀螺零偏（本次未动）
 *
 * 为什么把运行数据区从 page 88 上移到 101:
 *   1) 它原先预留了 page 89..117 共 29 页(29 KB), 但实际只需 13 页(擦除范围)。
 *      **虚设的 16 页还给了程序区**, 使比赛固件得以装下（原先超约 3.9 KB）。
 *   2) 修掉一处真实冲突: 链路模块的标定页/课程参数页原先取 page 99/100,
 *      **正落在本区的擦除范围(89..101)内** —— 只要触发一次 FLASH_EraseRunData()
 *      (calibrate.c / menu.c / state.c 共 3 处调用) 就会连标定一起擦掉。
 *      上移后本区擦除范围 102..114, 与 page 115 不再相交。
 *   3) 录制容量未削减（仍 1200 组 / 13 页擦除），迷宫/路径/红外/陀螺页地址全部不变,
 *      故**不需要重新标定**。 */
#define FLASH_ADDR_DATA_NUM 0X08019400 // page 101
#define FLASH_ADDR_DATA 0X08019800	   // page 102

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
