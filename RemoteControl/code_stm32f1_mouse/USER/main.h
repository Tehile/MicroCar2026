#ifndef _MAIN_H_
#define _MAIN_H_

/* 系统层 */
#include "stm32f10x.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ==========================================================================================
 *  测试模式开关 —— 五选一(最多定义一个; 五个都不定义 = 比赛固件)
 * ------------------------------------------------------------------------------------------
 *
 *  [1] MOUSE_TEST_NO_MOTOR —— 安全测试模式(数据回传 / 标定阶段)
 *      电机与运动策略被彻底停用, 电脑鼠在任何情况下都不可能自行启动:
 *        - MOTOR_PwmInit() 不启用 TIM4 PWM, 只把 PB6..PB9 配成推挽输出并保持低电平
 *        - MOTOR_LoopControl() 恒输出 0, 任何路径算出的 PWM 都到不了电机
 *        - SysTick 内的运动状态机(doTask*)与课程实验(LESSON_ISR)整体跳过
 *        - main() 跳过按键菜单与 STATE_MainLoop, 直接进入"链路 + 传感器"值守循环
 *      仍然保留: 陀螺 / 编码器 / 红外 / ADC 采集, 蓝牙遥测上行, 下行命令与远程标定服务。
 *
 *  [2] MOUSE_TEST_MOTOR_LAB —— 电机远程控制实验模式(轮子悬空时使用)
 *      电机**可控**, 但仍然没有任何"自己跑起来"的路径:
 *        - 菜单 / STATE_MainLoop / 课程实验 / 运动状态机(doTask*)全部不编进去,
 *          因此唯一能改变速度目标的地方就是下行命令;
 *        - 只有 `MOVE RUN <spL> <spR> [ms] [ir_mm]` 能让轮子转, 它自带**双护栏**:
 *              时间护栏: 到时(默认 2000 ms)无条件停
 *              红外护栏: 任一红外读数 < 阈值(默认 10 mm)立即停
 *          护栏在 1ms ISR 里判定, 不依赖上位机在线;
 *        - `ESTOP` 是锁存急停: 停车并锁死, 之后的 MOVE/CAL 一律回 ERR_ESTOP,
 *          必须显式 `STOP` 才解锁 —— 操作者始终握有最终控制权;
 *        - 运动过程中的全部观测量由**统一帧**(LINK_TYPE_UNIFIED)自时钟上行,
 *          约 217 Hz; 不需要上位机配合, 也没有"停车后回传日志"这一步
 *          (v2.0 删除了 SCOPE/DUMP 双流, 理由与替代方案见 HARDWARE/link.h)。
 *
 *  [3] MOUSE_LESSON_LAB —— 课程调参实验模式(参数设定 + 实验启停 + 数据回传)
 *      面向课件实验三~七的"调参工作台"。与 [2] 一样电机可控且无自走策略, 另有:
 *        - 一套**可运行时改**的关键参数(电机 PID 六增益、红外/陀螺 P/D、
 *          目标速度/距离、传感器方案), 由上位机 SETQ 下发、GET 回读、SAVE 落盘;
 *        - 一套**实验引擎**(见 APP/lesson_lab.c): 上位机 `LESSON START <exp>` 启动,
 *          固件按实验剖面驱动电机并在结束时自动停车、回 `LESSON DONE`;
 *        - 课程参数存放在**独立的 Flash 页(page 116)**, 与生产标定参数(page 115)互不干扰;
 *        - 全部代码在 `#ifdef MOUSE_LESSON_LAB` 之下 —— **正式固件里一个字节都不占用**。
 *      这一档的存在就是"实验代码与最终版代码分开"这条要求的落点:
 *      要交比赛固件时, 什么都不定义即可, 实验层自动消失。
 *
 *  [4] MOUSE_DATA_LOG —— 数据收集档(无记忆走迷宫 + 运动控制, 远程发车)
 *      这一档**要跑自走策略**, 因此与 [1][2][3] 不同, 它**不定义 MOUSE_TEST_MODE**:
 *        - 菜单 / 运动状态机(doTask*) / 课程 ISR 全部保留编译;
 *        - 跳过按键菜单, 改为**等上位机发车**: 收到 `ARM`(武装计时 + 点亮 1Hz 基准灯)
 *          与 `MAZE RUN`(请固件开始跑) 后进入 LESSON_Blind8() 无记忆走迷宫;
 *        - 于是"运动控制(加减速/转向/遇障转向/不可避免时停车)"整套都在数据流之下,
 *          而遥测与命令由 SysTick 里的 LINK_Tick() 持续服务 —— 主循环卡在迷宫的
 *          while(1) 里也照样收数据、收命令, 这一点是数据完整性的前提。
 *      时间同步(ms 时基 / ARM / 1Hz 基准灯 / TIME 帧)本身**不依赖本档**,
 *      它在 link.c 里无条件编译, 生产固件同样可用; 本档只是把它与自走策略
 *      组合成"可远程发车、可完整采集"的采集平台。
 *
 *  [5] 全部注释掉 = 原比赛固件(自走 / 按键菜单 / 课程实验)。
 *
 *  改回安全模式或比赛固件: 只改下面这一段宏定义即可, 其余代码一行都不用动。
 * ========================================================================================== */
/* #define MOUSE_TEST_NO_MOTOR 1 */ /* [1] 安全测试模式(电机禁用) */
/* #define MOUSE_TEST_MOTOR_LAB 1 */ /* [2] 电机远程控制实验(电机可控 + 双护栏) */
/* #define MOUSE_LESSON_LAB 1 */ /* [3] 课程调参实验(参数设定 + 实验启停 + 算参) */
#define MOUSE_DATA_LOG 1 /* [4] 数据收集档(无记忆迷宫 + 运动控制 + 远程发车) */

#if (defined(MOUSE_TEST_NO_MOTOR) ? 1 : 0) + (defined(MOUSE_TEST_MOTOR_LAB) ? 1 : 0) + \
        (defined(MOUSE_LESSON_LAB) ? 1 : 0) + (defined(MOUSE_DATA_LOG) ? 1 : 0) > 1
#error "MOUSE_TEST_NO_MOTOR / MOUSE_TEST_MOTOR_LAB / MOUSE_LESSON_LAB / MOUSE_DATA_LOG 四者互斥, 最多定义一个"
#endif

/* 三个*测试*模式的共同行为: 不跑菜单/状态机(自走)/课程示例, 只做采集 + 通信 + 显式命令。
 * 注意 MOUSE_DATA_LOG **不在其中** —— 数据收集档就是要跑自走策略。 */
#if defined(MOUSE_TEST_NO_MOTOR) || defined(MOUSE_TEST_MOTOR_LAB) || defined(MOUSE_LESSON_LAB)
#define MOUSE_TEST_MODE 1
#endif

/* 急停可恢复(实验/采集档的共同行为):
 *   1) 远程 `STOP` 能解除急停锁存(link.c 的 STOP 分支);
 *   2) 急停后**不进入 sleepmode**(isr.c 的 emergMon 分支)。
 * 只有比赛固件保留原语义: 急停即休眠、不接受远程复位(比赛规则要求, 且此时人在现场)。
 *
 * ⚠️ 为什么 MOUSE_DATA_LOG 也必须在这里(2026-10-04 真机实测的缺陷):
 *   它刻意不定义 MOUSE_TEST_MODE(因为要跑 doTask 系列 与 LESSON_ISR 自走), 但上面两条
 *   当时写成裸 `#ifdef MOUSE_TEST_MODE`, 于是数据收集档**继承了比赛固件的急停语义**:
 *     · `STOP` 回 OK 但 linkEStop 不复位 -> 之后的 MOVE/CAL 全部 ERR_ESTOP,
 *       只能断电重启, 一次急停就废掉一整场采集;
 *     · 急停 200ms 后 sleepmode=1 -> isr.c 跳过全部采样与 SAFETY_Monitor,
 *       而 LINK_Tick() 在门外照发帧 => **链路活着、数据冻结**(upMs 在涨,
 *       cpu/adcBatt/battMv 逐字节不变), 上位机看不出异常。
 *   教训: "会动的实验档"应该用**具名派生宏**表达, 不要在各处重复裸 #ifdef ——
 *   漏一处不会有编译错误, 只有在真机上才表现为行为不一致。 */
#if defined(MOUSE_TEST_MODE) || defined(MOUSE_DATA_LOG)
#define MOUSE_ESTOP_RECOVERABLE 1
#endif

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
#include "lesson_lab.h" /* 课程调参实验服务(仅 MOUSE_LESSON_LAB 下参与编译)

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
