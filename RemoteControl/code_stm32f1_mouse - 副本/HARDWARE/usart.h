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
#define EN_USART3_RX // 使能串口3接收
/*-------------------------------------  F U N C T I O N S  -------------------------------------*/
void USART_Init(void); // 串口1初始化(115200-8-N-1 @72MHz)

#endif
