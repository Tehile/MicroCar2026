#ifndef _LED_H_
#define _LED_H_
#include "main.h"
/*-----------------------------------------  I N C L U D E S  -----------------------------------------*/
/* 位带宏 PAout/PCout/PCin 定义于 main.h */
/*---------------------------------------  D E F I N I T I O N  ---------------------------------------*/
/* LED1-4 显示X坐标, LED5-8 显示Y坐标; 1-4 与 5-8 同引脚复用(如 LED1=LED5=PA8) */
#define LED1 PAout(8)
#define LED2 PCout(9)
#define LED3 PCout(8)
#define LED4 PCout(7)
#define LED5 PAout(8)
#define LED6 PCout(9)
#define LED7 PCout(8)
#define LED8 PCout(7)

#define BUTTON1 PCin(10) /* 按键输入 */

/* ---- 时间基准灯锁定(数据收集档专用) ----
 * 由 link.c 维护: 收到 ARM 命令(发车)后置 1, 之后 LED_On/LED_Off 全部直接返回,
 * 使 LED 完全归"1Hz 时间基准灯"独占 —— 否则迷宫/菜单的状态灯会把这个
 * 供外部(摄像头/光电)做时间对齐用的方波搅浑, 而方波一旦不干净, 时间同步就废了。
 * 其余模式(含比赛固件)恒为 0, LED 行为一字不变。 */
extern u8 ledBeaconLock;

/*-------------------------------------  F U N C T I O N S  -------------------------------------*/
void LED_Init(void);        /* LED 引脚初始化 */
void LED_On(u8 n);          /* 点亮指定 LED */
void LED_Off(u8 n);         /* 熄灭指定 LED */
void LED_ShowMouseXY(void); /* LED 显示鼠标坐标 */
void LED_ShowBlock(void);   /* 打印迷宫墙地图 */
#endif
