/**
 * @file    delay.c
 * @brief   TIM1 硬件延时(微秒/毫秒级)
 * @author  赛曙开发组
 * @date    2026-08-01
 * @note    TIM1 配置为 1MHz 自由计数器(72分频), 延时通过轮询 TIM1->CNT 实现,
 */

#include "delay.h"

/**
 * @brief  延时基准初始化: TIM1 配置为 1MHz 自由计数器
 * @date    2026-08-01
 * @author  赛曙开发组
 */
void DELAY_Init(void)
{
	RCC->APB2ENR |= 1 << 11;	   // TIM1时钟使能
	TIM1->ARR = ENCODER_ARR_VALUE; // 设定计数器自动重装值
	TIM1->PSC = 71;				   // 72分频 1MHz
	TIM1->CR1 |= 1 << 0;		   // 使能定时器1
}

/**
 * @brief  微秒级延时
 * @param  us 延时微秒数(最多允许跨界一次, 即最长 65535us)
 * @date    2026-07-24
 * @author  赛曙开发组
 */
void DELAY_Us(u16 us)
{
	u16 start;
	u8 cross = 0; // 是否跨界

	start = TIM1->CNT;
	while (1)
	{
		if (cross == 0)
		{
			if (TIM1->CNT < start)
				cross = 1;
			if (TIM1->CNT - start >= us)
				break;
		}
		else if (cross == 1)
		{
			if (TIM1->CNT + (ENCODER_ARR_VALUE - start) >= us)
				break;
		}
	}
}

/**
 * @brief  毫秒级延时
 * @param  ms 延时毫秒数
 * @date    2026-07-24
 * @author  赛曙开发组
 */
void DELAY_Ms(u16 ms)
{
	u16 start, t = 0;
	u8 cross = 0; // 是否跨界

	while (t < ms)
	{
		t++;
		cross = 0;
		start = TIM1->CNT;
		while (1)
		{
			if (cross == 0)
			{
				if (TIM1->CNT < start)
					cross = 1;
				if (TIM1->CNT - start >= 1000)
					break;
			}
			else if (cross == 1)
			{
				if (TIM1->CNT + (ENCODER_ARR_VALUE - start) >= 1000)
					break;
			}
		}
	}
}
