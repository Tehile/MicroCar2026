/**
 * @file    isr.h
 * @brief   系统中断初始化与 SysTick 1ms 控制中断
 * @author  赛曙开发组
 * @date    2026-08-10
 * @note    ISR_Init 由 main() 在全部外设初始化完成后调用(初始化顺序红线, 见 main.c);
 *          SysTick_Handler 为 1ms 控制中断入口, 内部按 GYRO->ODOM->墙判->doTask*->
 *          电机PID->SAFETY 流水线执行(具体实现见 isr.c)。
 */
#ifndef _ISR_H_
#define _ISR_H_
#include "main.h"

void ISR_Init(void);        /* SysTick 1ms控制中断初始化 */
void SysTick_Handler(void); /* 1ms Timer Interruption */

#endif
