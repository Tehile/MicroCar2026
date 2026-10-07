/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: safety.c —— 安全监控(急停/电池电压/运行数据保存/CPU占用)
*********************************************************************************************************
*/

#include "safety.h"
#define SHOWCPU
// #define SHOWVOLTAGE
#define FLASHSAVE

/**
 * @brief 安全监控(每1ms由 SysTick_Handler 调用)
 * @param none
 * @note 依次执行: 过流急停判定 -> 电池电压采样 -> 运行数据FLASH保存 -> CPU占用显示
 */
void SAFETY_Monitor(void)
{
	static u8 saveDataTemp = 0; /* 仅本函数使用 */
	if (emergMon.enabled == 1)
	{
		if (speedSt.pwmMotorL > MOTOR_PWM_LIMIT || speedSt.pwmMotorR > MOTOR_PWM_LIMIT || speedSt.pwmMotorL < -MOTOR_PWM_LIMIT || speedSt.pwmMotorR < -MOTOR_PWM_LIMIT)
			emergMon.counter++;
		else
			emergMon.counter = 0;
		if (emergMon.counter > 200 || speedSt.speedTargetL > 2000 || speedSt.speedTargetR > 2000 || speedSt.speedTargetL < -2000 || speedSt.speedTargetR < -2000)
			emergMon.active = 1; // 连续超限200周期或目标速度超2000 触发急停
	}
	/* 电池电压检测 */
	if (battMon.enabled == 1)
	{
		battMon.voltagePrev = battMon.voltage;
		battMon.voltage = ADC_Get(ADC_BATTARY) * 3.3 * 3 / 4096 * 100; // 最后乘100转换为整数。设置8V-7V为电源范围
		battMon.voltage = (battMon.voltage * 1 + battMon.voltagePrev * 9) / 10;
		battMon.percent = (battMon.voltage - 700) / 12;
		if (battMon.percent == 0)
			battMon.percent = 1;
		if (battMon.percent > 8)
			battMon.percent = 8;
#ifdef SHOWVOLTAGE
		LED_Off(0);
		LED_On(battMon.percent);
#endif
	}

#ifdef FLASHSAVE
	if (flashSt.flashenable == 1)
	{
		if (flashSt.flashwrite == 1)
		{
			saveDataTemp++;
			if (saveDataTemp > (perTimer - 1))
			{
				FLASH_WriteData(flashSt.saveDataNum);
				flashSt.saveDataNum++;
				saveDataTemp = 0;
				if (flashSt.saveDataNum >= saveNum - 1)
				{
					flashSt.saveDataNum = 0;
					flashSt.saveDataTime++;
				}
			}
		}
	}
#endif

#ifdef SHOWCPU
	cpuMon.ticks = TIM1->CNT - cpuMon.ticksPrev;
	if (cpuMon.ticks < 0)
		cpuMon.ticks = cpuMon.ticks + ENCODER_ARR_VALUE;
	cpuMon.ticks = cpuMon.ticks / 10; // 转换成百分制
	if (flashSt.flashenable == 1)
	{
		if (cpuMon.ticks > 95) // 时钟周期占用率高于95%
		{
			LED_On(1);
			LED_On(7);
		}
		else
			LED_Off(0);
	}
#endif

	if (emergMon.active == 1)
		LED_On(0);
}
