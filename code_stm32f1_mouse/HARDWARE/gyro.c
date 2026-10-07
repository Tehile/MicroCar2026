/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: gyro.c —— 陀螺仪初始化与角度采集
*********************************************************************************************************
*/

#include "gyro.h"
volatile GyroStruct gyroStr; /* 陀螺状态(ISR+主循环共享, 全部零初始化) */

/**
 * @brief 陀螺仪初始化: 读零点偏置, 无有效值时上电自动校准并存入 FLASH
 * @param none
 * @note 须在 ISR_Init 之后调用(自动校准依赖 SysTick 递增 gyroStr.adjCounter)
 */
void GYRO_Init(void)
{
	gyroStr.bias = FLASH_ReadHalfWord(FLASH_ADDR_GYRO0);
	if (gyroStr.bias == -1)
	{
		u8 ledswitch = 0;
		gyroStr.bias = 2044;
		speedSt.freemode = 1;
		gyroStr.adjCounter = 0;
		gyroStr.adjFlag = 1;
		while (1)
		{
			if (gyroStr.angle >= 0)
			{
				LED_Off(0);
				LED_On(gyroStr.angle % 8 + 1);
			}
			else if (gyroStr.angle < 0)
			{
				LED_Off(0);
				LED_On(gyroStr.angle % 8 + 8);
			}

			if (gyroStr.adjCounter > 1000)
				break;
		}
		gyroStr.adjFlag = 0;
		FLASH_Unlock();
		FLASH_ErasePage(FLASH_ADDR_GYRO0);
		FLASH_Unlock();
		FLASH_WriteHalfWord(FLASH_ADDR_GYRO0, gyroStr.bias);
		speedSt.sleepmode = 1;
		LED_On(0);
		DELAY_Ms(100);
		LED_Off(0);
		ledswitch = 1;
		while (1)
		{
			LED_Off(0);
			LED_On(ledswitch);
			DELAY_Ms(200);
			ledswitch++;
			if (ledswitch == 9)
				break;
		}
		LED_On(0);
		DELAY_Ms(500);
		LED_Off(0);
		speedSt.sleepmode = 0;
	}
}

/**
 * @brief ISR 陀螺仪角度采集(每1ms由 SysTick_Handler 调用)
 * @param none
 * @note 梯形积分累加 angleAcc; adjFlag 置位时按角度回摆方向修正零点偏置
 */
void GYRO_Sample(void)
{
	gyroStr.raw = ADC_Get(ADC_GYRO);

	gyroStr.diffPrev = gyroStr.diff;
	gyroStr.diff = (int)(gyroStr.raw - gyroStr.bias) / 2;
	if (gyroStr.diff < 0)
		gyroStr.diff--;
	gyroStr.angleAcc += (gyroStr.diff + gyroStr.diffPrev) / 2; // 梯形积分, 顺时针方向角度为正; 清零角度只需置 angle=0
	gyroStr.angleShort = gyroStr.angle;
	gyroStr.angle = gyroStr.angleAcc / sysCfg.gyroRatio * (-1);

	// 零点校准: 角度持续同向漂移则反向微调 bias
	if (gyroStr.adjFlag == 1)
	{
		gyroStr.adjCounter++;
		if (gyroStr.angle > gyroStr.angleShort)
		{
			gyroStr.bias--;
			gyroStr.adjCounter = 0;
		}
		else if (gyroStr.angle < gyroStr.angleShort)
		{
			gyroStr.bias++;
			gyroStr.adjCounter = 0;
		}
	}
}
