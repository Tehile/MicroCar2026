/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: sys.c —— 系统时钟、NVIC 中断优先级与低功耗控制
*********************************************************************************************************
*/

/**
 * @brief 设置 NVIC 向量表偏移地址
 * @param NVIC_VectTab 向量表基址(CODE区或RAM区)
 * @param Offset 偏移量(与基址对齐)
 */
#include "sys.h"

void MY_NVIC_SetVectorTable(u32 NVIC_VectTab, u32 Offset)
{
	SCB->VTOR = NVIC_VectTab | (Offset & (u32)0x1FFFFF80); // 设置NVIC的向量表偏移寄存器
	// 用于标识向量表是在CODE区还是在RAM区
}
/**
 * @brief 设置 NVIC 优先级分组
 * @param NVIC_Group 分组号 0~4, 决定抢占/响应优先级位数划分
 */
void MY_NVIC_PriorityGroupConfig(u8 NVIC_Group)
{
	u32 temp, temp1;
	temp1 = (~NVIC_Group) & 0x07; // 取后三位
	temp1 <<= 8;
	temp = SCB->AIRCR;	// 读取先前的设置
	temp &= 0X0000F8FF; // 清空先前分组
	temp |= 0X05FA0000; // 写入钥匙
	temp |= temp1;
	SCB->AIRCR = temp; // 设置分组
}
/**
 * @brief 初始化 NVIC 中断: 设定优先级并使能中断通道
 * @param NVIC_PreemptionPriority 抢占优先级(数值越小越优先)
 * @param NVIC_SubPriority 响应优先级(数值越小越优先)
 * @param NVIC_Channel 中断编号
 * @param NVIC_Group 分组 0~4, 抢占/响应位数分别为 0/4,1/3,2/2,3/1,4/0
 * @note 优先级不能超过分组限定的位数
 */
void MY_NVIC_Init(u8 NVIC_PreemptionPriority, u8 NVIC_SubPriority, u8 NVIC_Channel, u8 NVIC_Group)
{
	u32 temp;
	u8 IPRADDR = NVIC_Channel / 4;			 // 每组只能存4个,得到组地址
	u8 IPROFFSET = NVIC_Channel % 4;		 // 在组内的偏移
	IPROFFSET = IPROFFSET * 8 + 4;			 // 得到偏移的确切位置
	MY_NVIC_PriorityGroupConfig(NVIC_Group); // 设置分组
	temp = NVIC_PreemptionPriority << (4 - NVIC_Group);
	temp |= NVIC_SubPriority & (0x0f >> NVIC_Group);
	temp &= 0xf; // 取低四位

	if (NVIC_Channel < 32)
		NVIC->ISER[0] |= 1 << NVIC_Channel; // 使能中断位(要清除的话,相反操作就OK)
	else
		NVIC->ISER[1] |= 1 << (NVIC_Channel - 32);
	NVIC->IP[IPRADDR] |= temp << IPROFFSET; // 设置响应优先级和抢断优先级
}

/**
 * @brief 配置外部中断 EXTI: 将 GPIO 引脚映射到 EXTI 并设定触发方式
 * @param GPIOx GPIO端口号 0~6(对应 GPIOA~G)
 * @param BITx 需要使能的引脚位
 * @param TRIM 触发模式: bit0=下降沿, bit1=上升沿, 可组合
 * @note 一次仅配置1个IO口, 多IO口需多次调用; 自动使能对应中断; 不含PVD/RTC/USB唤醒
 */
void Ex_NVIC_Config(u8 GPIOx, u8 BITx, u8 TRIM)
{
	u8 EXTADDR;
	u8 EXTOFFSET;
	EXTADDR = BITx / 4; // 得到中断寄存器组的编号
	EXTOFFSET = (BITx % 4) * 4;

	RCC->APB2ENR |= 0x01; // 使能io复用时钟

	AFIO->EXTICR[EXTADDR] &= ~(0x000F << EXTOFFSET); // 清除原来设置！！！
	AFIO->EXTICR[EXTADDR] |= GPIOx << EXTOFFSET;	 // EXTI.BITx映射到GPIOx.BITx

	// 自动设置
	EXTI->IMR |= 1 << BITx; // 开启line BITx上的中断(EMR事件线不屏蔽, 软件仿真时无法进入中断)
	if (TRIM & 0x01)
		EXTI->FTSR |= 1 << BITx; // line BITx上事件下降沿触发
	if (TRIM & 0x02)
		EXTI->RTSR |= 1 << BITx; // line BITx上事件上升降沿触发
}

/**
 * @brief 复位 RCC 时钟寄存器并配置向量表
 * @param none
 * @note 不能在此执行外设复位(否则串口不工作)
 */
void MYRCC_DeInit(void)
{
	RCC->APB1RSTR = 0x00000000; // 复位结束
	RCC->APB2RSTR = 0x00000000;

	RCC->AHBENR = 0x00000014;  // 睡眠模式闪存和SRAM时钟使能.其他关闭.
	RCC->APB2ENR = 0x00000000; // 外设时钟关闭.
	RCC->APB1ENR = 0x00000000;
	RCC->CR |= 0x00000001;	 // 使能内部高速时钟HSION
	RCC->CFGR &= 0xF8FF0000; // 复位SW[1:0],HPRE[3:0],PPRE1[2:0],PPRE2[2:0],ADCPRE[1:0],MCO[2:0]
	RCC->CR &= 0xFEF6FFFF;	 // 复位HSEON,CSSON,PLLON
	RCC->CR &= 0xFFFBFFFF;	 // 复位HSEBYP
	RCC->CFGR &= 0xFF80FFFF; // 复位PLLSRC, PLLXTPRE, PLLMUL[3:0] and USBPRE
	RCC->CIR = 0x00000000;	 // 关闭所有中断
	// 配置向量表
#ifdef VECT_TAB_RAM
	MY_NVIC_SetVectorTable(NVIC_VectTab_RAM, 0x0);
#else
	MY_NVIC_SetVectorTable(100 /*NVIC_VectTab_FLASH*/, 0x0);
#endif
}
/**
 * @brief 执行 WFI 等待中断指令
 * @param none
 * @note THUMB 不支持内联汇编, 使用 CMSIS __WFI() 实现
 */
void WFI_SET(void)
{
	__WFI();
}
/**
 * @brief 进入待机模式
 * @param none
 * @note 需先使能电源时钟; WKUP 引脚可唤醒
 */
void Sys_Standby(void)
{
	SCB->SCR |= 1 << 2;		 // 使能SLEEPDEEP位 (SYS->CTRL)
	RCC->APB1ENR |= 1 << 28; // 使能电源时钟
	PWR->CSR |= 1 << 8;		 // 设置WKUP用于唤醒
	PWR->CR |= 1 << 2;		 // 清除Wake-up 标志
	PWR->CR |= 1 << 1;		 // PDDS置位
	WFI_SET();				 // 执行WFI指令
}

/**
 * @brief 系统软复位
 * @param none
 */
void Sys_Soft_Reset(void)
{
	SCB->AIRCR = 0X05FA0000 | (u32)0x04;
}

/**
 * @brief 设置 JTAG/SWD 调试口模式
 * @param mode 模式: 0x00 全使能, 0x01 仅使能SWD, 0x02 全关闭
 */
void JTAG_Set(u8 mode)
{
	u32 temp;
	temp = mode;
	temp <<= 25;
	RCC->APB2ENR |= 1 << 0;	  // 开启辅助时钟
	AFIO->MAPR &= 0XF8FFFFFF; // 清除MAPR的[26:24]
	AFIO->MAPR |= temp;		  // 设置jtag模式
}
/**
 * @brief 系统时钟初始化: 外部8MHz晶振 PLL×9 = 72MHz
 * @param none
 * @note 倍频系数为本硬件固定值9(寄存器写 9-2=7, PLLMUL编码从2起算);
 *       APB1=36MHz(DIV2), APB2=72MHz(DIV1), AHB=72MHz(DIV1)
 */
void SYS_ClockInit(void)
{
	unsigned char temp = 0;
	MYRCC_DeInit();		   // 复位并配置向量表
	RCC->CR |= 0x00010000; // 外部高速时钟使能HSEON
	while (!(RCC->CR >> 17))
		;					// 等待外部时钟就绪
	RCC->CFGR = 0X00000400; // APB1=DIV2;APB2=DIV1;AHB=DIV1;
	RCC->CFGR |= 7 << 18;	// PLL×9 (8MHz×9=72MHz), 编码值=倍频数-2
	RCC->CFGR |= 1 << 16;	// PLLSRC ON
	FLASH->ACR |= 0x32;		// FLASH 2个延时周期

	RCC->CR |= 0x01000000; // PLLON
	while (!(RCC->CR >> 25))
		;					 // 等待PLL锁定
	RCC->CFGR |= 0x00000002; // PLL作为系统时钟
	while (temp != 0x02)	 // 等待PLL作为系统时钟设置成功
	{
		temp = RCC->CFGR >> 2;
		temp &= 0x03;
	}
}
