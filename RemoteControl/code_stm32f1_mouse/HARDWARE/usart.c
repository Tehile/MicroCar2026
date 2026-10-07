/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: usart.c —— 串口初始化与printf重定向
*********************************************************************************************************
*/

/* 支持 printf 重定向, 无需选择 Use MicroLIB */
#include "usart.h"
#if 1
/* ARMCC v6: semihosting disabled via __use_no_semihosting_swi */
#if __ARMCC_VERSION >= 6000000
/* __FILE is already defined in stdio.h for ARMCC v6 */
#else
#pragma import(__use_no_semihosting)
struct __FILE
{
	int handle;
};
#endif
FILE __stdout;
void _sys_exit(int x) { x = x; }
// 重定义fputc函数
/* 注意: DMA1_Channel4 已接管 USART1 的发送(见 link.c)，因此这里不能再直接写 DR ——
 *   1) 会与 DMA 争抢数据寄存器，把调试文本插进遥测帧中间(主机侧该帧 CRC 必然失败)；
 *   2) 原来的"等 TC 置位再写 DR"在 DMA 连续供数时几乎等不到 (写 DR 会清 TC)，
 *      会把调用者(包括处于中断里的 printf)长时间卡住。
 * 改为统一入队到 link.c 的 TX 环缓冲，由 LINK_Tick 每毫秒经 DMA 送出。
 * 环满时该字节被丢弃并计入 linkDrop —— 宁可少几行调试打印，也不阻塞系统。 */
int fputc(int ch, FILE *f)
{
	LINK_PutChar((u8)ch);
	return ch;
}
#endif
#ifdef EN_USART1_RX // 如果使能了接收
u8 USART_RX_BUF[64]; // 接收缓冲, 最大64个字节
// 接收状态位: bit7=接收完成, bit6=收到0x0d, bit5~0=有效字节数
u8 USART_RX_STA = 0; // 接收状态标记

/**
 * @brief 串口1接收中断: 以 CR+LF 结束判定一帧接收完成
 * @param none
 * @note 读取 USARTx->SR 可避免莫名错误
 */
void USART1_IRQHandler(void)
{
	u8 res;
	if (USART1->SR & (1 << 5)) // 接收到数据
	{
		res = USART1->DR;
		if ((USART_RX_STA & 0x80) == 0) // 接收未完成
		{
			if (USART_RX_STA & 0x40) // 接收到了0x0d
			{
				if (res != 0x0a)
					USART_RX_STA = 0; // 接收错误,重新开始
				else
					USART_RX_STA |= 0x80; // 接收完成了
			}
			else // 还没收到0X0D
			{
				if (res == 0x0d)
					USART_RX_STA |= 0x40;
				else
				{
					USART_RX_BUF[USART_RX_STA & 0X3F] = res;
					USART_RX_STA++;
					if (USART_RX_STA > 63)
						USART_RX_STA = 0; // 接收数据错误,重新开始接收
				}
			}
		}
	}
}
#endif
/**
 * @brief 串口1初始化: PA9(TX)/PA10(RX), 115200-8-N-1
 * @param none
 * @note BRR=0x271: USARTDIV=72MHz/(115200×16)=39.0625,
 *       整数39<<4 + 小数0.0625×16=1 → 625=0x271
 */
void USART_Init(void)
{
	RCC->APB2ENR |= 1 << 2;	 // 使能PORTA口时钟
	RCC->APB2ENR |= 1 << 14; // 使能串口时钟
	GPIOA->CRH &= 0XFFFFF00F;
	GPIOA->CRH |= 0X000008B0; // IO状态设置

	RCC->APB2RSTR |= 1 << 14;	 // 复位串口1
	RCC->APB2RSTR &= ~(1 << 14); // 停止复位
	// 波特率设置
	USART1->BRR = 0x271;   // 115200 @72MHz (USARTDIV=39.0625)
	USART1->CR1 |= 0X200C; // 1位停止,无校验位.
#ifdef EN_USART1_RX		   // 如果使能了接收
	// 使能接收中断
	USART1->CR1 |= 1 << 8;					  // PE中断使能
	USART1->CR1 |= 1 << 5;					  // 接收缓冲区非空中断使能
	/* 注1: 旧版标准库符号名 USART1_IRQChannel 在 V3.x 库中已更名为 USART1_IRQn。
	 *      本行位于 #ifdef EN_USART1_RX 内, 在使能接收之前从未被编译, 故一直未暴露。
	 * 注2: 本项目的 MY_NVIC_Init 用 NVIC->IP[IPRADDR] |= temp << IPROFFSET 写优先级,
	 *      而 CMSIS 的 NVIC->IP 是 uint8_t[240], 移位 12 位后被截断为 0 ——
	 *      故这里的 3/3 参数实际是空操作, 只起到"使能该中断"的作用。
	 *      抢占优先级由 LINK_Init() 统一确定: SysTick 降为 1、USART1 为 0,
	 *      从而让 RX ISR 能抢占 1ms 的 SysTick(否则接收会成片丢字节)。 */
	MY_NVIC_Init(3, 3, USART1_IRQn, 2); // 组2，最低优先级
#endif
}
