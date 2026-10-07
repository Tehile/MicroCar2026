#ifndef _SYS_H_
#define _SYS_H_

/*-----------------------------------------  I N C L U D E S  -----------------------------------------*/
#include "main.h"
/*---------------------------------------  D E F I N I T I O N  ---------------------------------------*/
/* ---- Ex_NVIC_Config 参数: GPIO 端口号 ---- */
#define GPIO_A 0
#define GPIO_B 1
#define GPIO_C 2
#define GPIO_D 3
#define GPIO_E 4
#define GPIO_F 5
#define GPIO_G 6
/* ---- Ex_NVIC_Config 参数: 触发方式 ---- */
#define FTIR 1 // 下降沿触发
#define RTIR 2 // 上升沿触发
/* ---- JTAG_Set 参数: 调试口模式 ---- */
#define JTAG_SWD_DISABLE 0X02 /* 全关闭 */
#define SWD_ENABLE 0X01       /* 仅使能SWD */
#define JTAG_SWD_ENABLE 0X00  /* 全使能 */

/*-------------------------------------  F U N C T I O N S  -------------------------------------*/
void SYS_ClockInit(void);                                                                           // 系统时钟初始化(8MHz×9=72MHz)
void Sys_Soft_Reset(void);                                                                          // 系统软复位
void Sys_Standby(void);                                                                             // 待机模式
void MY_NVIC_SetVectorTable(u32 NVIC_VectTab, u32 Offset);                                          // 设置偏移地址
void MY_NVIC_PriorityGroupConfig(u8 NVIC_Group);                                                    // 设置NVIC分组
void MY_NVIC_Init(u8 NVIC_PreemptionPriority, u8 NVIC_SubPriority, u8 NVIC_Channel, u8 NVIC_Group); // 设置中断
void Ex_NVIC_Config(u8 GPIOx, u8 BITx, u8 TRIM);                                                    // 外部中断配置函数(只对GPIOA~G)
void JTAG_Set(u8 mode);

#endif
