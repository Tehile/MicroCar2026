#ifndef _MAIN_H_
#define _MAIN_H_

/* 系统层 */
#include "stm32f10x.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* 基础层 */
#include "sys.h"
#include "delay.h"
#include "usart.h"

/* 基础类型 */
/* 基础整型别名（项目历史沿用；与 ST 库的 u8/u16/u32 同宽度但为独立 typedef） */
typedef unsigned char uchar;   // Unsigned  8 bit quantity  无符号 8位整型变量
typedef unsigned short ushort; // Unsigned 16 bit quantity  无符号16位整型变量
typedef unsigned int uint;     // Unsigned 32 bit quantity  无符号32位整型变量

/* 布尔与空指针宏（项目历史沿用） */
#define TRUE 1
#define FALSE 0
#define NULL 0

/* 硬件驱动层 */
#include "adc.h"
#include "irs.h"
#include "led.h"
#include "flash.h"
#include "motor.h"

/* 核心控制层(含 GyroStruct/MotionCtrl/OdomStruct 等结构体定义) */
#include "core.h"

/* ISR 数据采集与安全监控 */
#include "isr.h"
#include "gyro.h"
#include "odom.h"
#include "safety.h"

/* 策略/算法层 */
#include "maze.h"
#include "floodfill.h"
#include "path.h"
#include "explore.h"

/* 工具层 */
#include "speedtable.h"
#include "motion.h"
#include "spurt.h"
#include "search.h"

/* 应用层 */
#include "state.h"
#include "menu.h"
#include "calibrate.h"
#include "lesson.h"

/* 位带操作(GPIO 单引脚读写基础设施)
 * 供各硬件模块头定义引脚别名: irs.h(INFR_*) / led.h(LED1-8,BUTTON1) / core.h(RIGHTDIR,LEFTDIR)
 * 这些别名为延迟展开宏, 在 .c 实际使用时才展开, 故本块位于 include 之后仍安全。
 */
#define BITBAND(addr, bitnum) ((addr & 0xF0000000) + 0x2000000 + ((addr & 0xFFFFF) << 5) + (bitnum << 2))
#define MEM_ADDR(addr) *((volatile unsigned long *)(addr))
#define BIT_ADDR(addr, bitnum) MEM_ADDR(BITBAND(addr, bitnum))
// IO口地址映射
#define GPIOA_ODR_Addr (GPIOA_BASE + 12) // 0x4001080C
#define GPIOB_ODR_Addr (GPIOB_BASE + 12) // 0x40010C0C
#define GPIOC_ODR_Addr (GPIOC_BASE + 12) // 0x4001100C
#define GPIOD_ODR_Addr (GPIOD_BASE + 12) // 0x4001140C
#define GPIOE_ODR_Addr (GPIOE_BASE + 12) // 0x4001180C
#define GPIOF_ODR_Addr (GPIOF_BASE + 12) // 0x40011A0C
#define GPIOG_ODR_Addr (GPIOG_BASE + 12) // 0x40011E0C

#define GPIOA_IDR_Addr (GPIOA_BASE + 8) // 0x40010808
#define GPIOB_IDR_Addr (GPIOB_BASE + 8) // 0x40010C08
#define GPIOC_IDR_Addr (GPIOC_BASE + 8) // 0x40011008
#define GPIOD_IDR_Addr (GPIOD_BASE + 8) // 0x40011408
#define GPIOE_IDR_Addr (GPIOE_BASE + 8) // 0x40011808
#define GPIOF_IDR_Addr (GPIOF_BASE + 8) // 0x40011A08
#define GPIOG_IDR_Addr (GPIOG_BASE + 8) // 0x40011E08

// IO口操作,只对单一的IO口!
// 确保n的值小于16!
#define PAout(n) BIT_ADDR(GPIOA_ODR_Addr, n) // 输出
#define PAin(n) BIT_ADDR(GPIOA_IDR_Addr, n)  // 输入

#define PBout(n) BIT_ADDR(GPIOB_ODR_Addr, n) // 输出
#define PBin(n) BIT_ADDR(GPIOB_IDR_Addr, n)  // 输入

#define PCout(n) BIT_ADDR(GPIOC_ODR_Addr, n) // 输出
#define PCin(n) BIT_ADDR(GPIOC_IDR_Addr, n)  // 输入

#define PDout(n) BIT_ADDR(GPIOD_ODR_Addr, n) // 输出
#define PDin(n) BIT_ADDR(GPIOD_IDR_Addr, n)  // 输入

#define PEout(n) BIT_ADDR(GPIOE_ODR_Addr, n) // 输出
#define PEin(n) BIT_ADDR(GPIOE_IDR_Addr, n)  // 输入

#define PFout(n) BIT_ADDR(GPIOF_ODR_Addr, n) // 输出
#define PFin(n) BIT_ADDR(GPIOF_IDR_Addr, n)  // 输入

#define PGout(n) BIT_ADDR(GPIOG_ODR_Addr, n) // 输出
#define PGin(n) BIT_ADDR(GPIOG_IDR_Addr, n)  // 输入

/* main.c 专属全局(多模块共享) */
/* ---- 任务状态 ---- */
typedef struct
{
    u8 goaheadnum; /* 冲刺前行格数 */
    u8 backSearch; /* 返回搜索标志 */
    u8 spurtFlag;  /* 冲刺标志 */
    u8 goOrBack;   /* 去/回标志 */
} TaskState;
extern TaskState taskSt;
extern u16 flashaddr;

#endif
