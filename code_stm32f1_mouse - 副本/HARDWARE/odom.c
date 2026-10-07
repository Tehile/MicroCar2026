/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: odom.c —— 编码器角度+里程采集
*********************************************************************************************************
*/

#include "odom.h"

/**
 * @brief ISR 编码器角度+里程采集(每1ms由 SysTick_Handler 调用)
 * @param none
 * @note 含编码器溢出回绕补偿(±ENCODER_WRAP_THRESHOLD)与陀螺角度校正; 结果写入 odomStr 由主循环读取
 */
void ODOM_Sample(void)
{
	odomStr.encLPrev = odomStr.encL;
	odomStr.encRPrev = odomStr.encR;
	odomStr.encL = TIM2->CNT;
	odomStr.encR = TIM3->CNT;
	if ((odomStr.encL - odomStr.encLPrev) < -ENCODER_WRAP_THRESHOLD)
		odomStr.encWrapCnt++; // 左轮前进跨界
	if ((odomStr.encR - odomStr.encRPrev) < -ENCODER_WRAP_THRESHOLD)
		odomStr.encWrapCnt--; // 右轮前进跨界
	if ((odomStr.encL - odomStr.encLPrev) > ENCODER_WRAP_THRESHOLD)
		odomStr.encWrapCnt--; // 左轮倒退跨界
	if ((odomStr.encR - odomStr.encRPrev) > ENCODER_WRAP_THRESHOLD)
		odomStr.encWrapCnt++;																				  // 右轮倒退跨界
	odomStr.angleAbs = (odomStr.encL - odomStr.encR + odomStr.encWrapCnt * ENCODER_ARR_VALUE) / sysCfg.encAngleRatio; // 绝对角度，不停累加
	odomStr.angle = odomStr.angleAbs - odomStr.angleBase;														  // 相对角度

	// 用编码器校正陀螺仪

	if (gyroStr.correctionEnable == 0)
	{

		odomStr.angleSkip++;
		if (odomStr.angleSkip == 5)
		{
			u8 angleIndex;
			odomStr.angleSkip = 0;
			odomStr.anglehis[odomStr.newindex] = odomStr.angle; // 循环数组保存最近40个编码器角度
			odomStr.newindex++;
			if (odomStr.newindex == 40)
				odomStr.newindex = 0;

			odomStr.anglehismin = 100;
			odomStr.anglehismax = -100;

			for (angleIndex = 0; angleIndex < 40; angleIndex++)
			{
				if (odomStr.anglehis[angleIndex] < odomStr.anglehismin)
					odomStr.anglehismin = odomStr.anglehis[angleIndex];
				if (odomStr.anglehis[angleIndex] > odomStr.anglehismax)
					odomStr.anglehismax = odomStr.anglehis[angleIndex];
			}
			if (gyroStr.adjFlag != 1) // 标定陀螺仪的时候不能清零
			{						  // 转弯状态(|angle|>20)时不应当清零陀螺仪
				if (abs(odomStr.anglehismax - odomStr.anglehismin) < 2 && abs(odomStr.angle) < 20)
					gyroStr.angleAcc = 0; // 只要编码器角度超过50个周期基本保持不变，就置零陀螺仪
			}
		}
	}

	if ((gyroStr.anglePrev2 - gyroStr.anglePrev) > 0 && (gyroStr.anglePrev - gyroStr.angle) < 0)
	{
		gyroStr.anglePeakPrev = gyroStr.anglePeak;
		gyroStr.anglePeak = gyroStr.angle;
	}
	if ((gyroStr.anglePrev2 - gyroStr.anglePrev) < 0 && (gyroStr.anglePrev - gyroStr.angle) > 0)
	{
		gyroStr.anglePeakPrev = gyroStr.anglePeak;
		gyroStr.anglePeak = gyroStr.angle;
	}
	gyroStr.anglePrev2 = gyroStr.anglePrev;
	gyroStr.anglePrev = gyroStr.angle;

	odomStr.distLPrev = odomStr.distL;
	odomStr.distRPrev = odomStr.distR;
	odomStr.distL = TIM2->CNT;
	odomStr.distR = TIM3->CNT;
	if ((odomStr.distL - odomStr.distLPrev) < -ENCODER_WRAP_THRESHOLD)
		odomStr.distLCycle++; // Left Coder Spillover Positively
	if ((odomStr.distR - odomStr.distRPrev) < -ENCODER_WRAP_THRESHOLD)
		odomStr.distRCycle++; // Right Coder Spillover Positively
	if ((odomStr.distL - odomStr.distLPrev) > ENCODER_WRAP_THRESHOLD)
		odomStr.distLCycle--; // Left Coder Spillover Negatively
	if ((odomStr.distR - odomStr.distRPrev) > ENCODER_WRAP_THRESHOLD)
		odomStr.distRCycle--; // Right Coder Spillover Negatively
	odomStr.trackAbsL = (float)(odomStr.distL + odomStr.distLCycle * ENCODER_ARR_VALUE) / sysCfg.trackRatioL;
	odomStr.trackAbsR = (float)(odomStr.distR + odomStr.distRCycle * ENCODER_ARR_VALUE) / sysCfg.trackRatioR;
	odomStr.trackAbs = (odomStr.trackAbsL + odomStr.trackAbsR) / 2;
	odomStr.trackL = odomStr.trackAbsL - odomStr.trackBaseL;
	odomStr.trackR = odomStr.trackAbsR - odomStr.trackBaseR;
	odomStr.cellOdom = odomStr.trackAbs - odomStr.trackBase;
}
