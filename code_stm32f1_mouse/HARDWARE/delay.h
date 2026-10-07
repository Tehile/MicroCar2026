/**
 * @file    delay.h
 * @brief   TIM1 硬件延时接口
 * @author  赛曙开发组
 * @date    2026-08-01
 * @note    延时基于 TIM1 1MHz 自由计数器, 需先调用 DELAY_Init()。
 */

#ifndef _DELAY_H_
#define _DELAY_H_
/*-----------------------------------------  I N C L U D E S  -----------------------------------------*/
#include "main.h"
/*-------------------------------------  F U N C T I O N S  -------------------------------------*/
void DELAY_Init(void);
void DELAY_Us(u16 us);
void DELAY_Ms(u16 ms);

#endif
