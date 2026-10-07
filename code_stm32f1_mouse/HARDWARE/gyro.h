#ifndef _GYRO_H_
#define _GYRO_H_
#include "main.h"

void GYRO_Init(void);   /* 陀螺仪初始化: 读零点偏置 + 首次自动校准 */
void GYRO_Sample(void); /* 陀螺仪角度采集 */

#endif
