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
/* 红外测量配置(链路参数 id 22-32; 默认值 = 原 WALL_* 宏值) */
IrCfg irCfg;
/* 电源监控配置(链路参数 id 33-38; 默认值 = 原 safety.c 写死值) */
PowerCfg powerCfg;
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

	/* ---- 红外测量配置(参数 id 22-32 的默认值 = 原宏值, 行为逐位不变) ---- */
	/* 侧墙阈值(参数 IRTHRWALL)的唯一真源就是上面那行 wallSns.wallThresh。 */
	irCfg.thr45 = WALL_45_THRESHOLD;
	irCfg.thrCritical = WALL_CRITICAL_THRESHOLD;
	irCfg.thrFrontLo = WALL_FRONT_FAR_LO;
	irCfg.thrFrontMid = WALL_FRONT_FAR_MID;
	irCfg.thrFrontHi = WALL_FRONT_FAR_HI;
	irCfg.thrFrontNear = WALL_FRONT_NEAR;
	/* 卡尔曼噪声: 与 IR_Init() 里 kalmanSt 的初值同源(Q=0.01, P=0.2)
	 * 单位是 1e-4, 故 100/2000。irCfg 是"参数表里的值", kalmanSt 是"生效值"。 */
	irCfg.kalQ = 100;
	irCfg.kalP = 2000;
	irCfg.kalOn = 1;
	irCfg.frontMode = 0; /* 0 = 均值 (f1+f2)/2, 与 isr.c 原式一致 */

	/* ---- 电源监控配置(参数 id 33-38 的默认值 = 原 safety.c 写死值) ----
	 * 原式: voltage = ADC * 3.3 * 3 / 4096 * 100  (即 mV = ADC * 3300*3/4096)
	 *       3300 * 3 / 4096 = 2.41699... mV/LSB, 取 µV/LSB 记 2417。 */
	powerCfg.battKv = 2417u;
	powerCfg.battLowMv = 7000;
	/* 满电 mV 取 7960 而不是 8000 才与原来的 (voltage-700)/12 完全一致:
	 * step = (7960-7000)/8 = 120mV, 正好等于原式的 12 (V×100)。 */
	powerCfg.battHighMv = 7960;
	powerCfg.battAlpha = 100; /* 0.100 -> 原式 (new*1 + prev*9)/10 的等价一阶 IIR */
	powerCfg.battMaxPct = 8;
	powerCfg.battMinPct = 1;
}
