#ifndef _MAIN_H_
#define _MAIN_H_

/* 系统层 */
#include "stm32f10x.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ==========================================================================================
 *  档位开关 —— 二选一(不定义 = 按键比赛档)
 * ------------------------------------------------------------------------------------------
 *
 *  MOUSE_REMOTE —— 远程档(上位机接管)
 *      值守态(未发车): 统一帧遥测 + 参数读写(GET/SETQ/SET/SAVE/RESET) + 远程标定(CAL/TARE)
 *                      + 手动运动 MOVE FWD/ROT/RUN(自带"时间 + 红外"双护栏) + 急停/解锁
 *      发车态        : `MAZE RUN [BLIND|RACE]` —— 实验8(无记忆走迷宫) / 正式比赛入口序列
 *      收车          : `MAZE STOP` / `ESTOP` / `MCU RESET`(复位前先停车)
 *                      可反复"发车 → 收车 → 再发车", 不必断电。
 *
 *      ⚠️ 本档**刻意不复现**两件只有操作者在现场才能完成的事:
 *           1) 按键菜单 `MENU_Run()`;
 *           2) 把鼻尖顶墙来选冲刺方向与速度档(`STATE_LoadMapAndSelectSpeed` /
 *              `STATE_DetermineGoaheadNum` 里的两个 `while(1)`)。
 *         远程场景前方没有近物, 那两个循环会**永久卡死**(真机实测 2026-10-05),
 *         因此在本档整段跳过。代价: 本档**不产生冲刺选档量**(spurtmode / spurtSpd /
 *         goaheadnum 保持初值), 真跑起来后与真比赛的冲刺行为不同 —— 这是远程化的
 *         必然取舍, 不是缺陷。要恢复真冲刺行为, 必须回到按键比赛档。
 *
 *  不定义 —— 按键比赛档
 *      上电进按键菜单, 一切靠板上按键与物理摆放。
 *      本档的编译产物与 v2.3.0 的比赛固件**逐字节一致**(改动前后各编一次比 .axf 即可验)。
 *      **远程发车/收车命令在本档不编译**(回 ERR_VALUE) —— 避免"主机以为发车了, 但其实
 *      没有任何东西会跑"这种撒谎式应答; 比赛规则也要求人在现场。
 *
 *  ⚠️ 为什么远程档必须"急停可恢复": 若继承比赛档的急停语义, 急停 200ms 后进 sleepmode
 *     ⇒ isr.c 跳过全部采样与 SAFETY_Monitor, 而 LINK_Tick() 在门外照发帧
 *     ⇒ 表现为"链路活着、数据冻结"(upMs 在涨, 采样量逐字节不变), 现场极难察觉。
 *     所以远程档: `STOP` 能解急停锁存, 且急停后不休眠。
 *
 *  改档: 只改下面这一行宏定义即可, 其余代码一行都不用动。
 * ========================================================================================== */
#define MOUSE_REMOTE 1 /* 远程档(上位机接管: 监视 + 参数 + 手动运动 + 远程发车) */

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
/* 蓝牙链路(遥测/命令/标定/参数持久化): 纯新增模块, 须在 HARDWARE_Init 之后 LINK_Init */
#include "link.h"

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
#define GPIOF_ODR_Addr (GPIOF_BASE + 12) // 0x40011C0C
#define GPIOG_ODR_Addr (GPIOG_BASE + 12) // 0x4001200C

#define GPIOA_IDR_Addr (GPIOA_BASE + 8) // 0x40010808
#define GPIOB_IDR_Addr (GPIOB_BASE + 8) // 0x40010C08
#define GPIOC_IDR_Addr (GPIOC_BASE + 8) // 0x40011008
#define GPIOD_IDR_Addr (GPIOD_BASE + 8) // 0x40011408
#define GPIOE_IDR_Addr (GPIOE_BASE + 8) // 0x40011808
#define GPIOF_IDR_Addr (GPIOF_BASE + 8) // 0x40011C08
#define GPIOG_IDR_Addr (GPIOG_BASE + 8) // 0x40012008

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
