/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: flash.c —— STM32内部Flash读写与运行数据存储
*********************************************************************************************************
*/

#include "flash.h"
#define FLASHSAVE /* 使能运行数据存入 Flash */

#ifndef FLASHSAVE /* 调试模式: 仅存1组 */
const u16 saveNum = 1;
const u8 perTimer = 2;
#endif
/* 存储运行数据参数: saveNum=保存条数上限, perTimer=存储间隔(定时周期数) */
#ifdef FLASHSAVE
const u16 saveNum = 1200;
const u8 perTimer = 2;
#endif
/* FLASH 保存地址约束:
 * 必须为偶数, 且大于本代码占用 FLASH 大小 + 0X08000000;
 * 最大 0X0801FFFF(存储空间为 0X08020000 - 保存地址 之间);
 * 每次写入16字节, 迷宫256格+路径数组共几项就用几个单元, 不要浪费 */

/* FLASH 存储区规划:
 * STM32F103 中容量 FLASH 共128K, 128页×1024字节, 每元素16位(2字节), 每页512元素;
 * 用户区分为 UART 与迷宫两部分: 程序占40K, 迷宫占1K, 留7K机动, 剩80K存UART数据
 * (80×1024/2=40960元素; 每1ms存4元素则10秒存40000元素);
 * 页划分: 0-39程序, 40-120 UART, 126-127 迷宫;
 * 注意: 每次写/擦除页前都必须先 FLASH_Unlock */
RunData run_data[saveNum]; /* 运行数据缓冲 */

/*-------------------------------------  F L A S H  D R I V E R  -------------------------------------*/
/**
 * @brief FLASH 解锁(写/擦除前必须调用)
 * @param none
 */
void FLASH_Unlock(void)
{
	FLASH->KEYR = FLASH_KEY1; // 写入解锁序列.
	FLASH->KEYR = FLASH_KEY2;
}
/**
 * @brief FLASH 上锁
 * @param none
 */
void FLASH_Lock(void)
{
	FLASH->CR |= 1 << 7; // 上锁
}
/**
 * @brief 获取 FLASH 操作状态
 * @param none
 * @return 0=完成, 1=忙, 2=编程错误, 3=写保护错误
 */
u8 FLASH_GetStatus(void)
{
	u32 res;
	res = FLASH->SR;
	if (res & (1 << 0))
		return 1; // 忙
	else if (res & (1 << 2))
		return 2; // 编程错误
	else if (res & (1 << 4))
		return 3; // 写保护错误
	return 0;	  // 操作完成
}
/**
 * @brief 等待 FLASH 操作完成
 * @param time 超时上限(每1us递减)
 * @return 操作状态(见 FLASH_GetStatus), 超时返回0xff
 */
u8 FLASH_WaitDone(u16 time)
{
	u8 res;
	do
	{
		res = FLASH_GetStatus();
		if (res != 1)
			break; // 非忙,无需等待了,直接退出.
		DELAY_Us(1);
		time--;
	} while (time);
	if (time == 0)
		res = 0xff; // TIMEOUT
	return res;
}
/**
 * @brief 擦除 FLASH 指定页
 * @param paddr 页地址
 * @return 0=成功, 其他见 FLASH_GetStatus
 */
u8 FLASH_ErasePage(u32 paddr)
{
	u8 res = 0;
	res = FLASH_WaitDone(0X5FFF); // 等待上次操作结束,>20ms
	if (res == 0)
	{
		FLASH->CR |= 1 << 1;		  // 页擦除
		FLASH->AR = paddr;			  // 设置页地址
		FLASH->CR |= 1 << 6;		  // 开始擦除
		res = FLASH_WaitDone(0X5FFF); // 等待操作结束,>20ms
		if (res != 1)				  // 非忙
		{
			FLASH->CR &= ~(1 << 1); // 清除页擦除标志.
		}
	}
	return res;
}
/**
 * @brief 写半字(16位)到 FLASH 指定地址
 * @param faddr 目标地址(必须为2的倍数)
 * @param dat 待写入数据
 * @return 0=成功, 其他见 FLASH_GetStatus
 */
u8 FLASH_WriteHalfWord(u32 faddr, int dat)
{
	u8 res;
	res = FLASH_WaitDone(0XFF);
	if (res == 0) // OK
	{
		FLASH->CR |= 1 << 0;		// 编程使能
		*(vs16 *)faddr = dat;		// 写入数据
		res = FLASH_WaitDone(0XFF); // 等待操作完成
		if (res != 1)				// 操作成功
		{
			FLASH->CR &= ~(1 << 0); // 清除PG位
		}
	}
	return res;
}
/**
 * @brief 读取指定地址半字(16位数据)
 * @param faddr 读地址
 * @return 对应数据
 */
int FLASH_ReadHalfWord(u32 faddr)
{
	return *(vs16 *)faddr;
}

/**
 * @brief 将当前运行数据写入内存缓冲
 * @param num 缓冲序号
 * @note 仅写缓冲, 实际落盘由 FLASH_SaveData 完成
 */
void FLASH_WriteData(u16 num)
{
	run_data[num].gangle = gyroStr.angle;
	run_data[num].motionTask = motionCtrl.task;
	run_data[num].speedl = speedSt.speedL;
	run_data[num].speedr = speedSt.speedR;
}

/**
 * @brief 将第 num 组运行数据写入 Flash
 * @param num 缓冲序号
 * @note 每组数据占4个半字, 地址由全局 flashaddr 推进
 */
void FLASH_SaveData(u16 num)
{
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_DATA + flashaddr, run_data[num].motionTask);
	flashaddr += 2;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_DATA + flashaddr, run_data[num].gangle);
	flashaddr += 2;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_DATA + flashaddr, run_data[num].speedl);
	flashaddr += 2;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_DATA + flashaddr, run_data[num].speedr);
}

static u16 printfLine = 1; /* 仅FLASH_PrintData使用 */
/**
 * @brief 打印 Flash 中保存的运行数据
 * @param none
 * @note 依次打印保存次数、当前记录数与各通道(task/gangle/speedL/speedR)序列
 */
void FLASH_PrintData()
{
	int data_temp = 0, data_temp1 = 0, repeat_time = 0;
	flashaddr = 0;
	data_temp = FLASH_ReadHalfWord(FLASH_ADDR_DATA_NUM + flashaddr);
	flashaddr += 2;
	printf("The Time Has Saved:%d\r\n", data_temp);
	data_temp1 = FLASH_ReadHalfWord(FLASH_ADDR_DATA_NUM + flashaddr);
	flashaddr += 2;
	printf("The Number of The Current Time:%d\r\n", data_temp1);
	if (data_temp == 1)
	{
		repeat_time = data_temp1 + 1;
	}
	else
		repeat_time = saveNum + 1;
	printf("motionCtrl.task,odomStr.cellOdom,speedL,speedR,\r\n");

	flashaddr = 0;
	printf("motionCtrl.task:\r\n");
	for (printfLine = 1; printfLine < repeat_time; printfLine++)
	{
		data_temp = FLASH_ReadHalfWord(FLASH_ADDR_DATA + flashaddr);
		flashaddr += 2 * 4;
		printf("%d,", data_temp);
	}
	printf("\r\n");

	flashaddr = 2;
	printf("gangle:\r\n");
	for (printfLine = 1; printfLine < repeat_time; printfLine++)
	{
		data_temp = FLASH_ReadHalfWord(FLASH_ADDR_DATA + flashaddr);
		flashaddr += 2 * 4;
		printf("%d,", data_temp);
	}
	printf("\r\n");

	flashaddr = 4;
	printf("speedL:\r\n");
	for (printfLine = 1; printfLine < repeat_time; printfLine++)
	{
		data_temp = FLASH_ReadHalfWord(FLASH_ADDR_DATA + flashaddr);
		flashaddr += 2 * 4;
		printf("%d,", data_temp);
	}
	printf("\r\n");

	flashaddr = 6;
	printf("speedR:\r\n");
	for (printfLine = 1; printfLine < repeat_time; printfLine++)
	{
		data_temp = FLASH_ReadHalfWord(FLASH_ADDR_DATA + flashaddr);
		flashaddr += 2 * 4;
		printf("%d,", data_temp);
	}
	printf("\r\n");
}

/**
 * @brief 擦除运行数据区
 * @param none
 * @note 按 saveNum/100+1 页循环擦除, 再擦除数据计数页
 */
void FLASH_EraseRunData()
{
	for (flashaddr = 0; flashaddr < (saveNum / 100) + 1; flashaddr++)
	{
		FLASH_Unlock();
		FLASH_ErasePage(FLASH_ADDR_DATA + flashaddr * 0x400);
	}
	FLASH_Unlock();
	FLASH_ErasePage(FLASH_ADDR_DATA_NUM);
}

/**
 * @brief 保存迷宫墙信息到 Flash
 * @param none
 * @note 写入 16×16 mapWall 后读回校验, 并保存起点/终点/搜索终点方向/冲刺标志
 */
void FLASH_SaveBlockData()
{
	u8 blockx, blocky; /* 行列索引 */
	FLASH_Unlock();
	FLASH_ErasePage(FLASH_ADDR_BLOCK_POINT);
	FLASH_Unlock();
	FLASH_ErasePage(FLASH_ADDR_BLOCK);
	flashaddr = 0;
	for (blockx = 0; blockx < 16; blockx++)
	{
		for (blocky = 0; blocky < 16; blocky++)
		{
			FLASH_Unlock();
			FLASH_WriteHalfWord(FLASH_ADDR_BLOCK + flashaddr, mazeState.mapWall[blockx][blocky]);
			flashaddr += 2;
		}
	}

	flashaddr = 0;
	for (blockx = 0; blockx < 16; blockx++)
	{
		for (blocky = 0; blocky < 16; blocky++)
		{
			mazeState.mapWall[blockx][blocky] = FLASH_ReadHalfWord(FLASH_ADDR_BLOCK + flashaddr);
			flashaddr += 2;
		}
	}
	flashaddr = 0;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_BLOCK_POINT + flashaddr, mazeState.xStart);
	flashaddr += 2;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_BLOCK_POINT + flashaddr, mazeState.yStart);
	flashaddr += 2;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_BLOCK_POINT + flashaddr, mazeState.goalX);
	flashaddr += 2;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_BLOCK_POINT + flashaddr, mazeState.goalY);
	flashaddr += 2;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_BLOCK_POINT + flashaddr, searchSt.goalCorrectDir);
	flashaddr += 2;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_BLOCK_POINT + flashaddr, taskSt.spurtFlag);
	flashaddr += 2;
}

/**
 * @brief 保存搜索交叉点栈到 Flash
 * @param none
 * @note 写入 crosswayBuf[0..stackIdx] 的 cX/cY
 */
void FLASH_SaveSearchStack()
{
	u8 temp_num;
	FLASH_Unlock();
	FLASH_ErasePage(FLASH_ADDR_BLOCK_STACK);
	flashaddr = 0;
	for (temp_num = 0; temp_num < mazeState.stackIdx + 1; temp_num++)
	{
		FLASH_Unlock();
		FLASH_WriteHalfWord(FLASH_ADDR_BLOCK_STACK + flashaddr, mazeState.crosswayBuf[temp_num].cX);
		flashaddr += 2;
		FLASH_Unlock();
		FLASH_WriteHalfWord(FLASH_ADDR_BLOCK_STACK + flashaddr, mazeState.crosswayBuf[temp_num].cY);
		flashaddr += 2;
	}
}

/**
 * @brief 置位块写入标志
 * @param none
 * @note 置位 flashSt.blockWriteFlag, 触发一次迷宫数据写入
 */
void FLASH_BlockWrite()
{
	flashSt.blockWriteFlag = 1;
}
