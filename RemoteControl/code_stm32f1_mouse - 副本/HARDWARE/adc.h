#ifndef _ADC_H_
#define _ADC_H_
#include "main.h"
/*-----------------------------------------  I N C L U D E S  -----------------------------------------*/
#include "sys.h"

/*---------------------------------------  D E F I N I T I O N  ---------------------------------------*/
/* ADC1 通道号 */
#define ADC_RF 13     // PC3 13
#define ADC_R 12      // PC2 12
#define ADC_L 11      // PC1 11
#define ADC_LF 10     // PC0 10
#define ADC_GYRO 0    // PB1
#define ADC_BATTARY 1 // PA1
/* 红外前向通道别名(与 irs.h 的 F1/F2 编号对应) */
#define ADC_F2 ADC_RF
#define ADC_F1 ADC_LF

/*-------------------------------------  F U N C T I O N S  -------------------------------------*/
void ADC_Init(void); /* ADC1 + GPIO 配置(6通道: 4红外 + 陀螺 + 电池) */
u16 ADC_Get(u8 ch);  /* 单通道采样, ch 取 ADC_* 通道号 */

#endif
