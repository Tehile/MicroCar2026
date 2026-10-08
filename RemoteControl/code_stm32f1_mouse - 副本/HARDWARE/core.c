/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: core.c —— 实时控制核心初始化(速度/PID/墙检测配置)
*********************************************************************************************************
*/
/* 本文件在算法中位置: 实时控制层--全局配置实例定义与初始化, SysTick 中断实现见 isr.c */

#include "core.h"
// 常量定义
#define COORADJUST // 是否开启纵向矫正
#define SHOWCPU	   // 是否开启显示时钟利用率
#define FLASHSAVE // 运行数据存入Flash
/* 速度配置(初值由 CORE_Init 逐字段赋值) */
SpeedConfig speedCfg;
/* PID增益配置(初值由 CORE_Init 逐字段赋值) */
PidGainConfig pidGain;
SpurtConfig spurtCfg; /* 冲刺PID参数 */
// 变量定义
// 编码器测速和PID相关
volatile SpeedState speedSt; /* 零初始化, 运行时ISR/主循环读写 */
// 编码器角度相关
/* 编码器/里程计 */
volatile OdomStruct odomStr;
/* 运动PID控制 */
volatile MotionCtrl motionCtrl;
/* Flash数据保存状态 */
volatile FlashState flashSt;
// 红外测距和矫正相关
/* 红外距离(含历史) */
volatile IrDist irDist; /* 全部零初始化 */
/* 墙检测+红外控制(初值由 CORE_Init 逐字段赋值) */
volatile WallSensor wallSns;
/* 转弯表指针 */
TurnTable turnTbl;

/**
 * @brief 核心数据初始化: 速度/PID增益/墙检测配置逐字段赋值
 * @param none
 * @note 须在 ISR_Init 之前调用(SysTick_Handler 每周期读这些配置)
 */
void CORE_Init(void)
{
	/* 速度配置 */
	speedCfg.search = 95;
	speedCfg.turn = 60;
	speedCfg.fastSprint = 75;
	speedCfg.forwardIr = 0;

	/* PID增益配置(accRatio/decRatio 运行时由 MOTION_Acc/Stop 设) */
	pidGain.goStraight = GOSTRAIGHT_KP_INIT;
	pidGain.smartFastBig = SMARTSTRAIGHTFAST_BIGERR_KP_INIT;
	pidGain.smartFastSm = SMARTSTRAIGHTFAST_SMALLERR_KP_INIT;
	pidGain.straight45P = STRAIGHT45_PID_KP;
	pidGain.straight45D = STRAIGHT45_PID_KD;
	pidGain.turnP = TURN_KP_INIT;
	pidGain.turnD = TURN_KD_INIT;

	/* 墙检测+红外控制 */
	wallSns.left = 1;
	wallSns.right = 1;
	wallSns.front = 0;
	wallSns.leftPrev = 1;
	wallSns.rightPrev = 1;
	wallSns.left45 = 0;
	wallSns.right45 = 0;
	wallSns.left45Prev = 0;
	wallSns.right45Prev = 0;
	wallSns.critL = 0;
	wallSns.critR = 0;
	wallSns.critL1 = 0;
	wallSns.critR1 = 0;
	wallSns.critL2 = 0;
	wallSns.critR2 = 0;
	wallSns.critL3 = 0;
	wallSns.critR3 = 0;
	wallSns.peakL = 100;
	wallSns.peakR = 100;
	wallSns.peakTrkL = 0;
	wallSns.peakTrkR = 0;
	wallSns.irSwitch = 1;
	wallSns.wallThresh = WALL_SIDE_THRESHOLD;
}
