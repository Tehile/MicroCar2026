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
	/* 电池电压检测(参数化: 分压系数/滤波系数/档位边界全部来自 powerCfg)
	 * 原式 voltage = ADC*3.3*3/4096*100 (即 V×100) 与现在的
	 *   mv = ADC * battKv / 1000  (battKv=2417 µV/LSB), voltage = mv/10
	 * 逐位等价; 滤波从"V×100 域一阶 IIR"改为"mV 域一阶 IIR", 系数同为 0.1,
	 * 只是量化粒度从 10mV 细化到 1mV。 */
	if (battMon.enabled == 1)
	{
		u16 adc = ADC_Get(ADC_BATTARY);
		u32 mv;
		u16 span, step;
		u8 pct;

		battMon.adc = adc;
		mv = ((u32)adc * powerCfg.battKv) / 1000u;

		if (powerCfg.battAlpha >= 1000u)
			battMon.mv = (u16)mv; /* alpha=1: 不滤波 */
		else
			battMon.mv = (u16)(((u32)mv * powerCfg.battAlpha +
			                    (u32)battMon.mv * (1000u - powerCfg.battAlpha)) / 1000u);

		battMon.voltagePrev = battMon.voltage;
		battMon.voltage = (double)battMon.mv / 10.0; /* 既有帧语义保持不变: V×100 */

		/* 电量档: pct = (mv - low) / step, step = (high-low)/maxPct。
		 * 默认 low=7000mV, high=7960mV, maxPct=8 -> step=120mV,
		 * 与原来的 (voltage-700)/12 完全一致(volt@796 -> 档位 8)。 */
		span = (u16)(powerCfg.battHighMv - powerCfg.battLowMv);
		step = (powerCfg.battMaxPct > 0u) ? (u16)(span / powerCfg.battMaxPct) : 120u;
		if (step == 0u)
			step = 1u;
		pct = (battMon.mv > powerCfg.battLowMv)
		          ? (u8)((battMon.mv - powerCfg.battLowMv) / step)
		          : 0u;
		if (pct < powerCfg.battMinPct)
			pct = powerCfg.battMinPct;
		if (pct > powerCfg.battMaxPct)
			pct = powerCfg.battMaxPct;
		battMon.percent = pct;
		battMon.low = (battMon.mv < powerCfg.battLowMv) ? 1u : 0u;
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
