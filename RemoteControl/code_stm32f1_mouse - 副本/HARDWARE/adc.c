/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: adc.c —— ADC 硬件驱动(ADC1 + GPIO 配置, 通道采样)
*********************************************************************************************************
*/
/* 本文件在算法中位置: 硬件层--模拟量采集统一入口, 为红外/陀螺/电池提供 ADC 读数。 */

#include "adc.h"
/**
 * @brief ADC 初始化(采样通道 GPIO + ADC1 配置 + 校准)
 * @param none
 * @note 分频6分频使ADC时钟为12M(不能超14M); 执行复位校准与校准并等待完成
 */
void ADC_Init(void)
{
	// 先初始化IO口
	RCC->APB2ENR |= 1 << 2;	  // 使能PORTA时钟
	GPIOA->CRL &= 0XFFFFFF00; // PA0-0通道，PA1-1通道 Anolog输入

	RCC->APB2ENR |= 1 << 4;	  // 使能PORTC口时钟
	GPIOC->CRL &= 0XF0FF0000; // PC0,1,2,3 Anolog输入 通道10,11,12,13；PC6推挽输出
	GPIOC->CRL |= 0X03000000; // PC6推挽输出

	RCC->APB2ENR |= 1 << 3;	  // 使能PORTB口时钟
	GPIOB->CRH &= 0X000FFFFF; // 清除原始设置
	GPIOB->CRH |= 0X33300000; // PB13,14,15推挽输出

	// 通道10/11设置
	RCC->APB2ENR |= 1 << 9;		// ADC1时钟使能
	RCC->APB2RSTR |= 1 << 9;	// ADC1复位
	RCC->APB2RSTR &= ~(1 << 9); // 复位结束
	RCC->CFGR &= ~(3 << 14);	// 分频因子清零
	// SYSCLK/DIV2=12M ADC时钟设置为12M,ADC最大时钟不能超过14M!
	// 否则将导致ADC准确度下降!
	RCC->CFGR |= 2 << 14; // 6分频

	ADC1->CR1 &= 0X00F0FFFF; // 工作模式清零
	ADC1->CR1 |= 0 << 16;	 // 独立工作模式
	ADC1->CR1 &= ~(1 << 8);	 // 非扫描模式
	ADC1->CR2 &= ~(1 << 1);	 // 单次转换模式
	ADC1->CR2 |= 7 << 17;	 // 软件控制转换
	ADC1->CR2 |= 1 << 20;	 // 使用外部触发(SWSTART)!!! 必须用一个事件来触发
	ADC1->CR2 &= ~(1 << 11); // 右对齐

	ADC1->SQR1 &= ~(0XF << 20); // 1个转换在规则序列

	// 设置采样时间
	ADC1->SMPR1 &= 0XFFFFF000; // 通道10,11,12,13采样时间清空
	ADC1->SMPR2 &= 0XFFFFFFC0;
	ADC1->SMPR1 |= 7 << 9; // 通道13  239.5周期,提高采样时间可以提高精确度
	ADC1->SMPR1 |= 7 << 6; // 通道12  239.5周期,提高采样时间可以提高精确度
	ADC1->SMPR1 |= 7 << 3; // 通道11  239.5周期,提高采样时间可以提高精确度
	ADC1->SMPR1 |= 7 << 0; // 通道10  239.5周期,提高采样时间可以提高精确度
	ADC1->SMPR2 |= 7 << 3; // 通道1  239.5周期,提高采样时间可以提高精确度
	ADC1->SMPR2 |= 7 << 0; // 通道0  239.5周期,提高采样时间可以提高精确度

	ADC1->CR2 |= 1 << 0; // 开启AD转换器
	ADC1->CR2 |= 1 << 3; // 使能复位校准
	while (ADC1->CR2 & 1 << 3)
		; // 等待校准结束
	// 该位由软件设置并由硬件清除。在校准寄存器被初始化后该位将被清除。
	ADC1->CR2 |= 1 << 2; // 开启AD校准
	while (ADC1->CR2 & 1 << 2)
		; // 等待校准结束
	// 该位由软件设置以开始校准，并在校准结束时由硬件清除
}

/**
 * @brief 获取指定通道 ADC 转换值(单次转换)
 * @param ch ADC 通道号(ADC_RF/ADC_R/ADC_L/ADC_LF/ADC_GYRO/ADC_BATTARY)
 * @return 该通道 12 位转换结果
 * @note 阻塞等待转换结束
 */
u16 ADC_Get(u8 ch)
{
	// 设置转换序列
	ADC1->SQR3 &= 0XFFFFFFE0; // 规则序列1 通道ch
	ADC1->SQR3 |= ch;
	ADC1->CR2 |= 1 << 22; // 启动规则转换通道
	while (!(ADC1->SR & 1 << 1))
		;			 // 等待转换结束
	return ADC1->DR; // 返回adc值
}
