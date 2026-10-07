/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: menu.c —— 按键UI菜单
*********************************************************************************************************
*/

#include "menu.h"

#define FLASHSAVE

/**
 * @brief  按键菜单选择与分发
 * @param  none
 * @note   短按切换功能档(LED 显示档位), 长按确认。档位 0/1 返回继续主流程,
 *         档位 2-8/default 进入不返回的功能循环
 */
void MENU_Run(void)
{
	u8 keyfunc = 0, keytmp = 0, c = 0;
	u16 keydelay = 0, ispressed = 0;

	LED_Off(0);
	LED_On(6);
	while (1) /* 菜单选择循环, 选定后break出switch进主任务 */
	{
		if (BUTTON1 == 0 && ispressed == 0)
		{
			DELAY_Ms(20);
			keydelay++;
			if (keydelay == 50)
			{
				LED_Off(0);
				LED_On(5);
			}
			if (keydelay == 100)
			{
				LED_Off(0);
				LED_On(4);
			}
			if (keydelay == 150)
			{
				LED_Off(0);
				LED_On(3);
			}
			if (keydelay == 200)
			{
				LED_Off(0);
				break;
			}
		}
		if (BUTTON1)
		{
			speedSt.freemode = 1;
			if (ispressed == 0)
			{
				LED_Off(0);
				ispressed = 1;
			}
			DELAY_Ms(5);
			if (BUTTON1)
			{
				while (BUTTON1)
				{
					keytmp++;
					DELAY_Ms(10);
					if (keytmp > 60)
						break;
				}

				if (keytmp < 60)
				{
					keytmp = 0;
					LED_Off(keyfunc);
					keyfunc++;
					if (keyfunc == 9)
						keyfunc = 1;
					LED_On(keyfunc);
				}
				else
				{
					keytmp = 0;
					LED_Off(keyfunc);
					DELAY_Ms(80);
					LED_On(keyfunc);
					DELAY_Ms(80);
					LED_Off(keyfunc);
					DELAY_Ms(80);
					LED_On(keyfunc);
					DELAY_Ms(500);
					LED_Off(keyfunc);
					break;
				}
			}
			else
				;
		}
	}
	while (BUTTON1)
		;

	speedSt.freemode = 0;

	switch (keyfunc)
	{
	case 0:
		return;

	case 1:
		return;

	case 2: // 擦除红外校准数据
		CALIB_MenuEraseIrData(); /* 进入不返回 */
	case 3: // 自动校准陀螺仪零点
		CALIB_MenuCalibGyro(); /* 进入不返回 */
		break;
	case 4: // 红外线自检
		CALIB_MenuIrSelfTest(); /* 进入不返回 */
	case 5: // 显示传感器数据
		CALIB_MenuShowSensor(); /* 进入不返回 */
	case 6: // 计算红外线传感器数据
		CALIB_MenuCalcIr(); /* 进入不返回 */
	case 7:
		CALIB_MenuShowBlockData(); /* 进入不返回 */

	case 8: // 擦除全部运行数据后休眠

		speedSt.freemode = 1;
#ifdef FLASHSAVE
		FLASH_EraseRunData();
#endif
		FLASH_Unlock();
		FLASH_ErasePage(FLASH_ADDR_BLOCK);
		FLASH_Unlock();
		FLASH_ErasePage(FLASH_ADDR_BLOCK_POINT);
		FLASH_Unlock();
		FLASH_ErasePage(FLASH_ADDR_GOPATH);
		FLASH_Unlock();
		FLASH_ErasePage(FLASH_ADDR_BACKPATH);
		FLASH_Unlock();
		FLASH_ErasePage(FLASH_ADDR_GOPATH_90);
		FLASH_Unlock();
		FLASH_ErasePage(FLASH_ADDR_BACKPATH_90);
		while (1)
		{
			speedSt.sleepmode = 1; // 关闭时钟周期的全部函数，节约电量
			LED_Off(0);
			LED_On(c);
			DELAY_Ms(1000);
			c++;
			if (c == 9)
				c = 1;
		}
	default: // 兜底, 休眠
		while (1)
		{
			speedSt.sleepmode = 1; // 关闭时钟周期的全部函数，节约电量
			LED_Off(0);
			LED_On(c);
			DELAY_Ms(1000);
			c++;
			if (c == 9)
				c = 1;
		}
	}
}
