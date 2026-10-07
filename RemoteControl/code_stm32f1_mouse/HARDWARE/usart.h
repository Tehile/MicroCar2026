#ifndef _USART_H_
#define _USART_H_
#include "main.h"
/*-----------------------------------------  I N C L U D E S  -----------------------------------------*/
#include <stm32f10x.h>
#include "stdio.h"
/*---------------------------------------  D E F I N I T I O N  ---------------------------------------*/
extern u8 USART_RX_BUF[64]; // 接收缓冲, 最大64个字节
extern u8 USART_RX_STA;     // 接收状态标记

// 如果想串口中断接收，请不要注释以下宏定义
/* 蓝牙链路: 使能串口1接收。定义此宏即可点亮 usart.c 内现成的
 * 64B 接收缓冲 + CR/LF 分帧 + USART1_IRQHandler + 接收中断使能,
 * usart.c 本身一行都不需要改。下行命令复用该 CR/LF 文本分帧。 */
#define EN_USART1_RX
#define EN_USART3_RX // 使能串口3接收(模板残留, 全工程无 USART3 实现)
/*-------------------------------------  F U N C T I O N S  -------------------------------------*/
void USART_Init(void); // 串口1初始化(115200-8-N-1 @72MHz)

#endif
