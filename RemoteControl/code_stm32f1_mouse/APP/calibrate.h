#ifndef _CALIBRATE_H_
#define _CALIBRATE_H_
#include "main.h"

/**
 * @brief  应用红外校准数据到传感器校正曲线(启动时执行一次)
 */
void CALIB_InitIr(void);

/**
 * @brief  菜单功能2: 擦除红外校准数据(进入不返回)
 */
void CALIB_MenuEraseIrData(void);

/**
 * @brief  菜单功能3: 自动校准陀螺仪零点(进入不返回)
 */
void CALIB_MenuCalibGyro(void);

/**
 * @brief  菜单功能4: 红外线自检(进入不返回)
 */
void CALIB_MenuIrSelfTest(void);

/**
 * @brief  菜单功能5: 显示传感器数据(进入不返回)
 */
void CALIB_MenuShowSensor(void);

/**
 * @brief  菜单功能6: 计算红外线传感器数据(标定)(进入不返回)
 */
void CALIB_MenuCalcIr(void);

/**
 * @brief  菜单功能7: 打印迷宫墙/路径/红外数据(进入不返回)
 */
void CALIB_MenuShowBlockData(void);

#endif
