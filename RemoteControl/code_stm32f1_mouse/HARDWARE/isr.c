/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: isr.c —— 运动控制中断核心(SysTick 1ms: 状态机+数据采集+电机PID+安全)
*********************************************************************************************************
*/
#include "isr.h"
#define SHOWCPU   // 开启显示时钟利用率
#define FLASHSAVE // 运行数据存入Flash

/* ===== 内部全局定义 ===== */

/* 系统配置常量(8个初值由 ISR_Init 逐字段赋值) */
SysConfig sysCfg; /* 系统配置常量(初值由 ISR_Init 赋值) */
/* 电机测速相关变量定义在 motor.c */
u8 straightCells = 1; // 连续直线走过的格子数

/**
 * @brief 大于零则返回原值, 否则返回0
 * @param t 待判断数值
 * @return 非负原值, 负数返回0
 */
static int abovezero(int t)
{
	if (t >= 0)
		return t;
	else
		return 0;
}

static u8 lastWallSide = LEFT;   /* 上一次贴墙侧(LEFT/RIGHT): 懒惰模式据此优先沿用同侧墙 */
volatile BattMonitor battMon;    /* 电池监控 */
volatile EmergMonitor emergMon;  /* 紧急监控(初值由 ISR_Init 赋值, enabled=1) */
volatile CpuMonitor cpuMon;      /* CPU占用监控 */

/**
 * @brief SysTick 1ms 中断: 运动控制主循环
 * @param none
 * @note 分段: 陀螺积分 -> 编码器里程 -> IR墙判 -> 任务状态机switch -> PID算速 -> PWM输出 -> 安全监控
 */
/* ===== motionCtrl.task 各 case 抽出的任务函数 ===== */

/**
 * @brief 加速状态: 目标速度随剩余格内里程线性变化, 叠加角度PD矫正
 * @param none
 * @note 位置要求按格内里程加速; 角度要求按航向角偏差PD矫正左右轮差
 */
static void doTaskAcc(void)
{
	motionCtrl.pidErrPrev = motionCtrl.pidErr;
	motionCtrl.pidErr = gyroStr.angle;
	// 位置要求: 距离越近目标速度越低
	speedSt.speedTargetL = pidGain.accRatio * odomStr.cellOdom + ACCV0;
	speedSt.speedTargetR = speedSt.speedTargetL;
	// 角度要求: 按航向角偏差 PD 矫正左右轮差
	speedSt.speedTargetL -= motionCtrl.pidErr * ACC_PID_KP + (motionCtrl.pidErr - motionCtrl.pidErrPrev) * ACC_PID_KD;
	speedSt.speedTargetR += motionCtrl.pidErr * ACC_PID_KP + (motionCtrl.pidErr - motionCtrl.pidErrPrev) * ACC_PID_KD;
}

/**
 * @brief 停止状态: 目标速度随剩余格内里程递减至0, 叠加角度PD矫正
 * @param none
 * @note 位置要求按格内里程制动; 停稳后 motionCtrl.tick 计时
 */
static void doTaskStop(void)
{
	motionCtrl.pidErrPrev = motionCtrl.pidErr;
	motionCtrl.pidErr = gyroStr.angle;
	// 位置要求: 距目标越近制动越大
	speedSt.speedTargetL = -pidGain.decRatio * odomStr.cellOdom;
	speedSt.speedTargetR = speedSt.speedTargetL;
	// 角度要求: 按航向角偏差 PD 矫正左右轮差
	speedSt.speedTargetL -= motionCtrl.pidErr * ACC_PID_KP + (motionCtrl.pidErr - motionCtrl.pidErrPrev) * ACC_PID_KD;
	speedSt.speedTargetR += motionCtrl.pidErr * ACC_PID_KP + (motionCtrl.pidErr - motionCtrl.pidErrPrev) * ACC_PID_KD;
	if (odomStr.cellOdom < 2 && odomStr.cellOdom > -2 && speedSt.speedL < 2 && speedSt.speedR < 2 && speedSt.speedL > -2 && speedSt.speedR > -2)
		motionCtrl.tick++; // 已停稳则计时
	else
		motionCtrl.tick = 0;
	speedSt.stoptick++;
}

/**
 * @brief 减速状态: 每周期目标速度递减
 * @param none
 * @note 每周期递减4, 保证30mm内速度由150降到70
 */
static void doTaskSlowDown(void)
{
	motionCtrl.pidErrPrev = motionCtrl.pidErr;
	motionCtrl.pidErr = gyroStr.angle;
	motionCtrl.tick++;
	{
		speedSt.speedTargetL -= 4; // 减速度略大于加速度, 保证30mm内速度由150降到70
		speedSt.speedTargetR = speedSt.speedTargetL;
	}
}

/**
 * @brief 提速状态: 每周期目标速度递增
 * @param none
 * @note 每周期递增1
 */
static void doTaskSpeedUp(void)
{
	motionCtrl.pidErrPrev = motionCtrl.pidErr;
	motionCtrl.pidErr = gyroStr.angle;
	motionCtrl.tick++;
	{
		speedSt.speedTargetL += 1;
		speedSt.speedTargetR = speedSt.speedTargetL;
	}
}

/**
 * @brief 转弯状态: 按转弯查表给定目标角与左右轮速, 角度误差PD矫正
 * @param none
 * @note 转弯表 gyro/vl/vr 为逐 tick 前馈(见 turnTbl), 表值为整定常量
 */
static void doTaskTurn(void)
{
	gyroStr.target = *(turnTbl.gyro + motionCtrl.tick); // 查表取当前tick目标角度
	motionCtrl.pidErrPrev = motionCtrl.pidErr;
	motionCtrl.pidErr = gyroStr.angle - gyroStr.target;
	motionCtrl.tick++;
	speedSt.speedTargetL = *(turnTbl.vl + motionCtrl.tick) - motionCtrl.pidErr * pidGain.turnP - (motionCtrl.pidErr - motionCtrl.pidErrPrev) * pidGain.turnD; // 8,5	12,15  -20
	speedSt.speedTargetR = *(turnTbl.vr + motionCtrl.tick) + motionCtrl.pidErr * pidGain.turnP + (motionCtrl.pidErr - motionCtrl.pidErrPrev) * pidGain.turnD; //-20
}

/**
 * @brief 原地掉头180度
 * @param none
 * @note 两阶段: 查表前馈转向(step0) -> 陀螺对正停转(step1)
 */
static void doTaskTurnAround(void)
{
	if (motionCtrl.step == 0) // 阶段1: 查表前馈转向
	{
		motionCtrl.tick++;
		gyroStr.target = *(turnTbl.gyro + motionCtrl.tick);
		motionCtrl.pidErrPrev = motionCtrl.pidErr;
		motionCtrl.pidErr = gyroStr.angle - gyroStr.target;

		motionCtrl.pidErrInteg += motionCtrl.pidErr; // PID积分消残差
		speedSt.speedTargetL = *(turnTbl.vl + motionCtrl.tick) - motionCtrl.pidErr * TURN_PID_KP - motionCtrl.pidErrInteg * TURN_PID_KI - (motionCtrl.pidErr - motionCtrl.pidErrPrev) * TURN_PID_KD;
		speedSt.speedTargetR = *(turnTbl.vr + motionCtrl.tick) + motionCtrl.pidErr * TURN_PID_KP + motionCtrl.pidErrInteg * TURN_PID_KI + (motionCtrl.pidErr - motionCtrl.pidErrPrev) * TURN_PID_KD;
		if (motionCtrl.tick > 267) // 267 与180度回转查表长度一致
		{
			motionCtrl.step = 1;
			motionCtrl.tick = 0;
		}
		speedSt.turnaroundTick = 0;
	}
	else if (motionCtrl.step == 1) // 阶段2: 陀螺角度对正, 停稳后结束
	{
		motionCtrl.pidErrPrev = motionCtrl.pidErr;
		motionCtrl.pidErr = gyroStr.angle;
		speedSt.speedTargetL = -motionCtrl.pidErr * TURN_PID_KP - (motionCtrl.pidErr - motionCtrl.pidErrPrev) * TURN_PID_KD;
		speedSt.speedTargetR = motionCtrl.pidErr * TURN_PID_KP + (motionCtrl.pidErr - motionCtrl.pidErrPrev) * TURN_PID_KD;
		if (speedSt.speedL < 2 && speedSt.speedR < 2 && speedSt.speedL > -2 && speedSt.speedR > -2)
			motionCtrl.tick++;
		else
			motionCtrl.tick = 0;
	}
}

/**
 * @brief 原地点转90度
 * @param none
 * @note 两阶段: 查表前馈转向(step0) -> 陀螺对正停转(step1)
 */
static void doTaskPointTurn90(void)
{
	if (motionCtrl.step == 0) // 阶段1: 查表前馈转向
	{
		motionCtrl.tick++;
		gyroStr.target = *(turnTbl.gyro + motionCtrl.tick);
		motionCtrl.pidErrPrev = motionCtrl.pidErr;
		motionCtrl.pidErr = gyroStr.angle - gyroStr.target;
		motionCtrl.pidErrInteg += motionCtrl.pidErr;
		speedSt.speedTargetL = *(turnTbl.vl + motionCtrl.tick) - motionCtrl.pidErr * TURN_PID_KP - motionCtrl.pidErrInteg * TURN_PID_KI - (motionCtrl.pidErr - motionCtrl.pidErrPrev) * TURN_PID_KD; // 8/0.1/5
		speedSt.speedTargetR = *(turnTbl.vr + motionCtrl.tick) + motionCtrl.pidErr * TURN_PID_KP + motionCtrl.pidErrInteg * TURN_PID_KI + (motionCtrl.pidErr - motionCtrl.pidErrPrev) * TURN_PID_KD;
		if (motionCtrl.tick > 232) // 232 与90度点转查表长度一致
		{
			motionCtrl.step = 1;
			motionCtrl.tick = 0;
		}
		speedSt.turnaroundTick = 0;
	}
	else if (motionCtrl.step == 1) // 阶段2: 陀螺对正, 停稳后结束
	{
		motionCtrl.pidErr = gyroStr.angle;
		speedSt.speedTargetL = motionCtrl.pidErr * (-1);
		speedSt.speedTargetR = motionCtrl.pidErr * 1;
		if (speedSt.speedL < 15 && speedSt.speedR < 15 && speedSt.speedL > -15 && speedSt.speedR > -15)
			motionCtrl.tick++;
		else
			motionCtrl.tick = 0;
	}
}

/**
 * @brief 90度转弯前对正
 * @param none
 * @note 前方有墙按前墙距离差矫正, 无墙按陀螺角矫正
 */
static void doTaskBefore90Turn(void)
{
	if (wallSns.front == 1) // 前方有墙: 按前两传感器距离差对正
	{
		motionCtrl.pidErr = irDist.f2 - irDist.f1;
		speedSt.speedTargetL = speedCfg.turn - motionCtrl.pidErr * 0.1;
		speedSt.speedTargetR = speedCfg.turn + motionCtrl.pidErr * 0.1;
	}
	else // 无前墙: 按陀螺航向角对正
	{
		motionCtrl.pidErr = gyroStr.angle;
		speedSt.speedTargetL = speedCfg.turn - motionCtrl.pidErr * 0.6; // 1
		speedSt.speedTargetR = speedCfg.turn + motionCtrl.pidErr * 0.6;
	}
}

/**
 * @brief 180度转弯前对正
 * @param none
 * @note 按侧墙/前墙距离差矫正到车道中线
 */
static void doTaskBefore180Turn(void)
{
	if (wallSns.right == 1 && wallSns.left == 0 && irDist.f > 60) // 仅右侧有墙
	{
		speedSt.speedTargetL = speedCfg.turn + (irDist.r - sysCfg.distToWall);
		speedSt.speedTargetR = speedCfg.turn - (irDist.r - sysCfg.distToWall);
	}
	else if (wallSns.right == 0 && wallSns.left == 1 && irDist.f > 60) // 仅左侧有墙
	{
		speedSt.speedTargetL = speedCfg.turn - (irDist.l - sysCfg.distToWall);
		speedSt.speedTargetR = speedCfg.turn + (irDist.l - sysCfg.distToWall);
	}
	else if (wallSns.right == 1 && wallSns.left == 1 && irDist.f > 60) // 两侧都有墙: 取左右差
	{
		speedSt.speedTargetL = speedCfg.turn + (irDist.r - irDist.l);
		speedSt.speedTargetR = speedCfg.turn - (irDist.r - irDist.l);
	}
	else // 无侧墙: 按前墙距离调整到30mm
	{
		speedSt.speedTargetL = (irDist.f - 20) * 0.5 + (irDist.f1 - 20) * 1;
		speedSt.speedTargetR = (irDist.f - 20) * 0.5 + (irDist.f2 - 20) * 1;
		if (speedSt.speedTargetL > speedCfg.turn)
			speedSt.speedTargetL = speedCfg.turn;
		if (speedSt.speedTargetL < -speedCfg.turn)
			speedSt.speedTargetL = -speedCfg.turn;
		if (speedSt.speedTargetR > speedCfg.turn)
			speedSt.speedTargetR = speedCfg.turn;
		if (speedSt.speedTargetR < -speedCfg.turn)
			speedSt.speedTargetR = -speedCfg.turn;
		if ((irDist.f - 20) < -20 && abs(irDist.f1 - irDist.f2) < 100) // 太远且角度偏差不大, 直走
		{
			speedSt.speedTargetL = -speedCfg.turn;
			speedSt.speedTargetR = -speedCfg.turn;
		}
		if ((irDist.f - 20) > 20 && abs(irDist.f1 - irDist.f2) < 100) // 太近且角度偏差不大, 后退
		{
			speedSt.speedTargetL = speedCfg.turn;
			speedSt.speedTargetR = speedCfg.turn;
		}
		if (speedSt.speedL < 2 && speedSt.speedR < 2 && speedSt.speedL > -2 && speedSt.speedR > -2)
			motionCtrl.tick++;
		else
			motionCtrl.tick = 0;
		speedSt.turnaroundTick++;
		if (speedSt.turnaroundTick > 100)
			motionCtrl.tick = 100; // 防止抖动卡死
	}
}

/**
 * @brief 前向红外测试: 对正前方传感器参考距离
 * @param none
 * @note 按前方距离差线性矫正, 限幅±50
 */
static void doTaskForwardIrTest(void)
{
	speedSt.speedTargetL = (irDist.f - speedCfg.forwardIr) * 0.5 + (irDist.f1 - speedCfg.forwardIr) * 1;
	speedSt.speedTargetR = (irDist.f - speedCfg.forwardIr) * 0.5 + (irDist.f2 - speedCfg.forwardIr) * 1;
	if (speedSt.speedTargetL > 50)
		speedSt.speedTargetL = 50;
	if (speedSt.speedTargetL < -50)
		speedSt.speedTargetL = -50;
	if (speedSt.speedTargetR > 50)
		speedSt.speedTargetR = 50;
	if (speedSt.speedTargetR < -50)
		speedSt.speedTargetR = -50;
	if (speedSt.speedTargetL < 2 && speedSt.speedTargetL > -2)
		speedSt.speedTargetL = 0;
	if (speedSt.speedTargetR < 2 && speedSt.speedTargetR > -2)
		speedSt.speedTargetR = 0;
	if ((irDist.f - speedCfg.forwardIr) < -20 && abs(irDist.f1 - irDist.f2) < 100) // 太远且角度偏差不大, 直走
	{
		speedSt.speedTargetL = -50;
		speedSt.speedTargetR = -50;
	}
	if ((irDist.f - speedCfg.forwardIr) > 20 && abs(irDist.f1 - irDist.f2) < 100) // 太近, 后退
	{
		speedSt.speedTargetL = 50;
		speedSt.speedTargetR = 50;
	}
}

/**
 * @brief 保持贴墙距离
 * @param none
 * @note 按前墙距离差矫正到14mm参考位, 限幅±50
 */
static void doTaskKeepWallDistance(void)
{
	speedSt.speedTargetL = (irDist.f - 14) * 0.5 + (irDist.f1 - 14) * 1;
	speedSt.speedTargetR = (irDist.f - 14) * 0.5 + (irDist.f2 - 14) * 1;
	if (speedSt.speedTargetL > 50)
		speedSt.speedTargetL = 50;
	if (speedSt.speedTargetL < -50)
		speedSt.speedTargetL = -50;
	if (speedSt.speedTargetR > 50)
		speedSt.speedTargetR = 50;
	if (speedSt.speedTargetR < -50)
		speedSt.speedTargetR = -50;
	if ((irDist.f - 14) < -20 && abs(irDist.f1 - irDist.f2) < 100) // 太远, 直走
	{
		speedSt.speedTargetL = -50;
		speedSt.speedTargetR = -50;
	}
	if ((irDist.f - 14) > 20 && abs(irDist.f1 - irDist.f2) < 100) // 太近, 后退
	{
		speedSt.speedTargetL = 50;
		speedSt.speedTargetR = 50;
	}
	if (speedSt.speedL < 2 && speedSt.speedR < 2 && speedSt.speedL > -2 && speedSt.speedR > -2)
		motionCtrl.tick++;
	else
		motionCtrl.tick = 0;
	speedSt.turnaroundTick++;
	if (speedSt.turnaroundTick > 500)
		motionCtrl.tick = 100; // 防止抖动卡死
}

/**
 * @brief 闭眼直线
 * @param none
 * @note 按陀螺航向角PD矫正, 保持直线
 */
static void doTaskGoStraight(void)
{
	motionCtrl.pidErrPrev = motionCtrl.pidErr;
	motionCtrl.pidErr = gyroStr.angle;
	speedSt.speedTargetL = speedCfg.search - motionCtrl.pidErr * pidGain.goStraight; // 0.4;
	speedSt.speedTargetR = speedCfg.search + motionCtrl.pidErr * pidGain.goStraight; // 0.4;
}

/**
 * @brief 智能直线
 * @param none
 * @note 有墙侧按墙距差矫正, 无墙按陀螺角矫正
 */
static void doTaskSmartStraight(void)
{
	if (wallSns.left == 0 && wallSns.right == 0) // 两侧无墙, 闭眼走直线
	{
		motionCtrl.pidErrPrev = motionCtrl.pidErr;
		motionCtrl.pidErr = gyroStr.angle;
		speedSt.speedTargetL = speedCfg.turn - motionCtrl.pidErr * 1; // 1
		speedSt.speedTargetR = speedCfg.turn + motionCtrl.pidErr * 1;
	}
	else if (wallSns.left == 1) // 左侧有墙: 按左墙距差矫正
	{
		motionCtrl.pidErrPrev = motionCtrl.pidErr;
		motionCtrl.pidErr = irDist.l - sysCfg.distToWall;
		speedSt.speedTargetL = speedCfg.turn - motionCtrl.pidErr * 0.1;
		speedSt.speedTargetR = speedCfg.turn + motionCtrl.pidErr * 0.1;
	}
	else if (wallSns.right == 1) // 右侧有墙: 按右墙距差矫正
	{
		motionCtrl.pidErrPrev = motionCtrl.pidErr;
		motionCtrl.pidErr = sysCfg.distToWall - irDist.r;
		speedSt.speedTargetL = speedCfg.turn - motionCtrl.pidErr * 0.1;
		speedSt.speedTargetR = speedCfg.turn + motionCtrl.pidErr * 0.1;
	}
}

/**
 * @brief 智能直线(快速档)
 * @param none
 * @note 懒惰模式沿用上次贴墙侧, 按墙距差大小切换 PD 增益档
 */
static void doTaskSmartStraightFast(void)
{
	if (wallSns.left == 0 && wallSns.right == 0) // 闭眼走直线
	{
		motionCtrl.pidErrPrev = motionCtrl.pidErr;
		motionCtrl.pidErr = gyroStr.angle;
		speedSt.speedTargetL = speedCfg.search - motionCtrl.pidErr * 1;
		speedSt.speedTargetR = speedCfg.search + motionCtrl.pidErr * 1;
	}
	else
	{
		if ((lastWallSide == LEFT && wallSns.left == 1) || (lastWallSide == RIGHT && wallSns.right == 0)) // 懒惰模式：上一次用的哪边墙，这次只要那边有墙就还用那边
		{
			lastWallSide = LEFT;
			motionCtrl.pidErrPrev = motionCtrl.pidErr;
			motionCtrl.pidErr = irDist.l - sysCfg.distToWall;
			if (abs(motionCtrl.pidErr) > 10)
			{
				speedSt.speedTargetL = speedCfg.search - motionCtrl.pidErr * pidGain.smartFastBig - (motionCtrl.pidErr - motionCtrl.pidErrPrev) * 0.16; // 0.2;
				speedSt.speedTargetR = speedCfg.search + motionCtrl.pidErr * pidGain.smartFastBig + (motionCtrl.pidErr - motionCtrl.pidErrPrev) * 0.16; // 0.2;
			}
			else
			{
				speedSt.speedTargetL = speedCfg.search - motionCtrl.pidErr * pidGain.smartFastSm - (motionCtrl.pidErr - motionCtrl.pidErrPrev) * 0.1; // 0.15;
				speedSt.speedTargetR = speedCfg.search + motionCtrl.pidErr * pidGain.smartFastSm + (motionCtrl.pidErr - motionCtrl.pidErrPrev) * 0.1; // 0.15;
			}
		}
		else
		{
			lastWallSide = RIGHT;
			motionCtrl.pidErrPrev = motionCtrl.pidErr;
			motionCtrl.pidErr = sysCfg.distToWall - irDist.r;
			if (abs(motionCtrl.pidErr) > 10)
			{
				speedSt.speedTargetL = speedCfg.search - motionCtrl.pidErr * pidGain.smartFastBig - (motionCtrl.pidErr - motionCtrl.pidErrPrev) * 0.16; // 0.2;
				speedSt.speedTargetR = speedCfg.search + motionCtrl.pidErr * pidGain.smartFastBig + (motionCtrl.pidErr - motionCtrl.pidErrPrev) * 0.16; // 0.2;
			}
			else
			{
				speedSt.speedTargetL = speedCfg.search - motionCtrl.pidErr * pidGain.smartFastSm - (motionCtrl.pidErr - motionCtrl.pidErrPrev) * 0.1; // 0.15;
				speedSt.speedTargetR = speedCfg.search + motionCtrl.pidErr * pidGain.smartFastSm + (motionCtrl.pidErr - motionCtrl.pidErrPrev) * 0.1; // 0.15;
			}
		}
	}
	// MOTION_SetAngle(0);
}

/**
 * @brief 闭眼直线+坐标矫正
 * @param none
 * @note 陀螺PD直线; 有墙→无墙变化时纵向矫正
 */
static void doTaskStraightEyeCloseCoorAdj(void)
{
	motionCtrl.pidErrPrev = motionCtrl.pidErr;
	motionCtrl.pidErr = gyroStr.angle;
	speedSt.speedTargetL = speedCfg.search - motionCtrl.pidErr * 0.25;
	speedSt.speedTargetR = speedCfg.search + motionCtrl.pidErr * 0.25;
#ifdef COORADJUST

	{
		if (wallSns.leftPrev == 1 && wallSns.left == 0)
		{
			MOTION_SetTrack(sysCfg.trackCoord);
		}
		if (wallSns.rightPrev == 1 && wallSns.right == 0)
		{
			MOTION_SetTrack(sysCfg.trackCoord);
		}
	}
#endif
}

/**
 * @brief 沿杆直线
 * @param none
 * @note 陀螺PD直线; 有墙→无墙变化时纵向矫正
 */
static void doTaskStraightPole(void)
{
	motionCtrl.pidErr = gyroStr.angle;
	speedSt.speedTargetL = speedCfg.search - motionCtrl.pidErr * 0.5;
	speedSt.speedTargetR = speedCfg.search + motionCtrl.pidErr * 0.5;

#ifdef COORADJUST
	{
		if (wallSns.leftPrev == 1 && wallSns.left == 0)
			MOTION_SetTrack(sysCfg.trackCoord);
		if (wallSns.rightPrev == 1 && wallSns.right == 0)
			MOTION_SetTrack(sysCfg.trackCoord);
	}
#endif
}

/**
 * @brief 冲刺前智能直线(转弯前补全)
 * @param none
 * @note 有墙按墙距差小增益矫正
 */
static void doTaskSmartStraightSpurt(void)
{
	if (wallSns.left == 0 && wallSns.right == 0) // 闭眼走直线
	{
		motionCtrl.pidErrPrev = motionCtrl.pidErr;
		motionCtrl.pidErr = gyroStr.angle;
		speedSt.speedTargetL = speedCfg.search - motionCtrl.pidErr * 1;
		speedSt.speedTargetR = speedCfg.search + motionCtrl.pidErr * 1;
	}
	else if (wallSns.left == 1)
	{
		motionCtrl.pidErrPrev = motionCtrl.pidErr;
		motionCtrl.pidErr = irDist.l - sysCfg.distToWall;
		speedSt.speedTargetL = speedCfg.search - motionCtrl.pidErr * 0.03;
		speedSt.speedTargetR = speedCfg.search + motionCtrl.pidErr * 0.03;
	}
	else if (wallSns.right == 1)
	{
		motionCtrl.pidErrPrev = motionCtrl.pidErr;
		motionCtrl.pidErr = sysCfg.distToWall - irDist.r;
		speedSt.speedTargetL = speedCfg.search - motionCtrl.pidErr * 0.03;
		speedSt.speedTargetR = speedCfg.search + motionCtrl.pidErr * 0.03;
	}
}

/**
 * @brief 冲刺前智能直线(贴左)
 * @param none
 * @note 有墙按墙距差最小增益矫正
 */
static void doTaskSmartStraightSpurtL(void)
{
	if (wallSns.left == 0 && wallSns.right == 0) // 闭眼走直线
	{
		motionCtrl.pidErrPrev = motionCtrl.pidErr;
		motionCtrl.pidErr = gyroStr.angle;
		speedSt.speedTargetL = speedCfg.search - motionCtrl.pidErr * 1;
		speedSt.speedTargetR = speedCfg.search + motionCtrl.pidErr * 1;
	}
	else if (wallSns.left == 1)
	{
		motionCtrl.pidErrPrev = motionCtrl.pidErr;
		motionCtrl.pidErr = irDist.l - sysCfg.distToWall;
		speedSt.speedTargetL = speedCfg.search - motionCtrl.pidErr * 0.01;
		speedSt.speedTargetR = speedCfg.search + motionCtrl.pidErr * 0.01;
	}
	else if (wallSns.right == 1)
	{
		motionCtrl.pidErrPrev = motionCtrl.pidErr;
		motionCtrl.pidErr = sysCfg.distToWall - irDist.r;
		speedSt.speedTargetL = speedCfg.search - motionCtrl.pidErr * 0.01;
		speedSt.speedTargetR = speedCfg.search + motionCtrl.pidErr * 0.01;
	}
}

/**
 * @brief 90度冲刺睁眼直线
 * @param none
 * @note 懒惰模式沿用上次贴墙侧, 红外PD矫正
 */
static void doTaskSpurt90Ir(void)
{
	if (wallSns.left == 0 && wallSns.right == 0) // 闭眼走直线
	{
		motionCtrl.pidErrPrev = motionCtrl.pidErr;
		motionCtrl.pidErr = gyroStr.angle;
		speedSt.speedTargetL = speedCfg.search - motionCtrl.pidErr * 1;
		speedSt.speedTargetR = speedCfg.search + motionCtrl.pidErr * 1;
	}
	else
	{
		if ((lastWallSide == LEFT && wallSns.left == 1) || (lastWallSide == RIGHT && wallSns.right == 0)) // 懒惰模式：上一次用的哪边墙，这次只要那边有墙就还用那边
		{
			lastWallSide = LEFT;
			motionCtrl.pidErrPrev = motionCtrl.pidErr;
			motionCtrl.pidErr = irDist.l - sysCfg.distToWall;
			if (abs(motionCtrl.pidErr) > 10)
			{
				speedSt.speedTargetL = speedCfg.search - motionCtrl.pidErr * spurtCfg.irKp1; // 0.3;
				speedSt.speedTargetR = speedCfg.search + motionCtrl.pidErr * spurtCfg.irKp1;
			}
			else
			{
				speedSt.speedTargetL = speedCfg.search - motionCtrl.pidErr * spurtCfg.irKp2 - (motionCtrl.pidErr - motionCtrl.pidErrPrev) * spurtCfg.irKd;
				speedSt.speedTargetR = speedCfg.search + motionCtrl.pidErr * spurtCfg.irKp2 + (motionCtrl.pidErr - motionCtrl.pidErrPrev) * spurtCfg.irKd;
			}
		}
		else
		{
			lastWallSide = RIGHT;
			motionCtrl.pidErrPrev = motionCtrl.pidErr;
			motionCtrl.pidErr = sysCfg.distToWall - irDist.r;
			if (abs(motionCtrl.pidErr) > 10)
			{
				speedSt.speedTargetL = speedCfg.search - motionCtrl.pidErr * spurtCfg.irKp1;
				speedSt.speedTargetR = speedCfg.search + motionCtrl.pidErr * spurtCfg.irKp1;
			}
			else
			{
				speedSt.speedTargetL = speedCfg.search - motionCtrl.pidErr * spurtCfg.irKp2 - (motionCtrl.pidErr - motionCtrl.pidErrPrev) * spurtCfg.irKd;
				speedSt.speedTargetR = speedCfg.search + motionCtrl.pidErr * spurtCfg.irKp2 + (motionCtrl.pidErr - motionCtrl.pidErrPrev) * spurtCfg.irKd;
			}
		}
	}
}

/**
 * @brief 90度冲刺闭眼直线
 * @param none
 * @note 陀螺PD矫正
 */
static void doTaskSpurt90Blind(void)
{
	motionCtrl.pidErrPrev = motionCtrl.pidErr;
	motionCtrl.pidErr = gyroStr.angle;
	speedSt.speedTargetL = speedCfg.search - motionCtrl.pidErr * spurtCfg.gyroKp - (motionCtrl.pidErr - motionCtrl.pidErrPrev) * spurtCfg.gyroKd;
	speedSt.speedTargetR = speedCfg.search + motionCtrl.pidErr * spurtCfg.gyroKp + (motionCtrl.pidErr - motionCtrl.pidErrPrev) * spurtCfg.gyroKd;

	//	#ifdef COORADJUST
	//		if(lefthaswallh==1&&lefthaswall==0){MOTION_SetTrack(trackcoor-90);}
	//		if(righthaswallh==1&&righthaswall==0){MOTION_SetTrack(trackcoor-90);}
	//	#endif
}

/**
 * @brief 45度斜线冲刺
 * @param none
 * @note 按侧墙/前墙距离差计算偏航误差并PD矫正
 */
static void doTaskStraight45(void)
{
	if (irDist.l < irCfg.thr45) // 左侧有墙
	{
		motionCtrl.pidErr = abovezero(irDist.l + 50 - irDist.f1) + abovezero(180 - irDist.f1); // pidErr越大，说明车越左偏。两项和，第一项代表角度偏差，第二项代表横向坐标偏差
		speedSt.speedTargetL = speedCfg.fastSprint + motionCtrl.pidErr * pidGain.straight45P + (motionCtrl.pidErr - motionCtrl.pidErrPrev) * pidGain.straight45D;
		speedSt.speedTargetR = speedCfg.fastSprint - motionCtrl.pidErr * pidGain.straight45P - (motionCtrl.pidErr - motionCtrl.pidErrPrev) * pidGain.straight45D;
	}
	else if (irDist.r < irCfg.thr45) // 右侧有墙
	{
		motionCtrl.pidErr = abovezero(irDist.r + 50 - irDist.f2) + abovezero(180 - irDist.f2); // pidErr越大，说明车越右偏
		speedSt.speedTargetL = speedCfg.fastSprint - motionCtrl.pidErr * pidGain.straight45P - (motionCtrl.pidErr - motionCtrl.pidErrPrev) * pidGain.straight45D;
		speedSt.speedTargetR = speedCfg.fastSprint + motionCtrl.pidErr * pidGain.straight45P + (motionCtrl.pidErr - motionCtrl.pidErrPrev) * pidGain.straight45D;
	}
	if (irDist.r > irCfg.thr45 && irDist.l > irCfg.thr45) // 两侧都没有墙
	{
		motionCtrl.pidErr = /*odomStr.angle+*/ abovezero(220 - irDist.f2) - abovezero(220 - irDist.f1);
		speedSt.speedTargetL = speedCfg.fastSprint - motionCtrl.pidErr * pidGain.straight45P;
		speedSt.speedTargetR = speedCfg.fastSprint + motionCtrl.pidErr * pidGain.straight45P;
	}
}

/**
 * @brief ISR 墙壁检测
 * @param none
 * @note 按传感器开关采集距离, 判定 侧墙/前墙/45度/临界墙
 */
static void doWallDetect(void)
{
	switch (wallSns.irSwitch) // 传感器开关。转弯时候关闭传感器以提高电机效率
	{
	case 1:
		irDist.lPrev2 = irDist.lPrev;
		irDist.rPrev2 = irDist.rPrev;
		irDist.lPrev = irDist.l;
		irDist.rPrev = irDist.r;
		irDist.l45Prev2 = irDist.l45Prev;
		irDist.r45Prev2 = irDist.r45Prev;
		irDist.l45Prev = irDist.l45;
		irDist.r45Prev = irDist.r45;
		IR_GetDistance();
		irDist.f = (irDist.f1 + irDist.f2) / 2;
		break;
	case 0:
		break; // 传感器关闭时不采集(转弯时关闭以提高电机效率)
	}
	// 90度直线: 左右墙判定
	wallSns.leftPrev = wallSns.left;
	wallSns.rightPrev = wallSns.right;
	if (irDist.l < wallSns.wallThresh)
		wallSns.left = 1;
	else
		wallSns.left = 0;
	if (irDist.r < wallSns.wallThresh)
		wallSns.right = 1;
	else
		wallSns.right = 0;
	// 前墙判定(双传感器远距或单传感器近距)
	if ((irDist.f1 < irCfg.thrFrontLo && irDist.f2 < irCfg.thrFrontHi) || (irDist.f2 < irCfg.thrFrontMid && irDist.f1 < irCfg.thrFrontHi) || (irDist.f1 < irCfg.thrFrontNear) || (irDist.f2 < irCfg.thrFrontNear))
		wallSns.front = 1;
	else
		wallSns.front = 0;

	// 45度直线矫正时的45度墙判定
	wallSns.left45Prev = wallSns.left45;
	wallSns.right45Prev = wallSns.right45;
	if (irDist.l < irCfg.thr45)
		wallSns.left45 = 1;
	else
		wallSns.left45 = 0;
	if (irDist.r < irCfg.thr45)
		wallSns.right45 = 1;
	else
		wallSns.right45 = 0;

	// 45度冲刺: 临界墙判定(决定斜线转弯起始点)
	wallSns.critL3 = wallSns.critL2;
	wallSns.critL2 = wallSns.critL1;
	wallSns.critL1 = wallSns.critL;
	wallSns.critR3 = wallSns.critR2;
	wallSns.critR2 = wallSns.critR1;
	wallSns.critR1 = wallSns.critR;
	if (irDist.l < irCfg.thrCritical)
		wallSns.critL = 1; // 临界墙阈值与斜线转弯起始点相关
	else
		wallSns.critL = 0;
	if (irDist.r < irCfg.thrCritical)
		wallSns.critR = 1;
	else
		wallSns.critR = 0;
}

/**
 * @brief 控制中断初始化: 配置 SysTick 为 1ms 周期中断(运动控制时基)
 * @param none
 * @note SysTick 时钟固定为 HCLK/8(72MHz); 真正的延时见 delay.c
 */
void ISR_Init(void)
{
	u8 facUs;  /* us 计数倍乘(SysTick = HCLK/8) */
	u16 facMs; /* ms 计数倍乘 */

	/* 系统配置常量逐字段赋值(须早于 SysTick 使能, ISR 每周期读 sysCfg/emergMon) */
	sysCfg.gyroRatio = 423.926 * 1.0286 * 0.9167;
	sysCfg.distToWall = 42;
	sysCfg.encAngleRatio = (u16)(75 * 0.90);
	sysCfg.trackRatioL = 62.59f * 0.85f;
	sysCfg.trackRatioR = 62.68f * 0.85f;
	sysCfg.speedRatioL = 0.6259 * 0.85;
	sysCfg.speedRatioR = 0.6268 * 0.85;
	sysCfg.trackCoord = 145;
	emergMon.enabled = 1;
	emergMon.active = 0;
	emergMon.counter = 0;
	emergMon.delay = 0;

	SysTick->CTRL &= 0xfffffffb; /* bit2清空, 选择外部时钟 HCLK/8 */
	facUs = 72 / 8;				 /* 72MHz 系统时钟 */
	facMs = (u16)facUs * 1000;

	SysTick->LOAD = (u32)1 * facMs;		/* 时间加载(SysTick->LOAD为24bit) 定时1ms */
	SysTick->VAL = 0x00;				/* 清空计数器 */
	SysTick->CTRL |= (1 << 0 | 1 << 1); /* 开始倒数, 使能中断 */
}

void SysTick_Handler() // 1ms Timer Interruption
{
	static u16 ledTest = 0; /* LED测试计时(仅ISR内部) */
	cpuMon.ticksPrev = TIM1->CNT;
	if (ledTest == 1000)
	{
		ledTest = 0;
	}
	ledTest++;

	if (speedSt.sleepmode == 1) // Sleep Mode. Close all sensors and set PWM to 0
	{
		MOTOR_LPWMH = 0;
		MOTOR_LPWML = 0;
		MOTOR_RPWMH = 0;
		MOTOR_RPWML = 0;
	}
	else
	{
		cpuMon.ticksPrev = TIM1->CNT;
		// 陀螺仪角度采集(左转为负, 右转为正)
		GYRO_Sample();
		// 编码器里程采集
		ODOM_Sample();

		doWallDetect();

		/* ---- 运动状态机 ----
		 * 两个档位共用同一套 doTask*: 远程档靠"未发车时 freemode=1 ⇒ PWM 恒 0"保证车
		 * 不会自己跑起来(MOVE RUN 另有时间 + 红外双护栏), 而不是靠不编译状态机。 */
		switch (motionCtrl.task)
		{
		case MOTION_TASK_NULL:
			break;

		case ACC: // 加速
			doTaskAcc();
			break;

		case STOP: // 刹车
			doTaskStop();
			break;

		case SLOWDOWN: // 非零速度之间减速
			doTaskSlowDown();
			break;

		case SPEEDUP: // 非零速度之间加速
			doTaskSpeedUp();
			break;

		case TURN: // 转弯
			doTaskTurn();
			break;

		case TURNAROUND: // 原地掉头180度
			doTaskTurnAround();
			break;

		case POINTTURN90: // 原地点转90度
			doTaskPointTurn90();
			break;

		case BEFORE90TURN: // 90度转弯前对正
			doTaskBefore90Turn();
			break;

		case BEFORE180TURN: // 180度转弯前对正
			doTaskBefore180Turn();
			break;

		case FORWARDIRTEST: // 前向红外测试对正
			doTaskForwardIrTest();
			break;

		case KEEPWALLDISTANCE: // 保持贴墙距离
			doTaskKeepWallDistance();
			break;

		// ===== 搜索直线 =====
		case GOSTRAIGHT: // 闭眼走直线
			doTaskGoStraight();
			break;

		case SMARTSTRAIGHT: // 转弯后补全
			doTaskSmartStraight();
			break;

		case SMARTSTRAIGHTFAST: // 搜索每格前半段, 按有无墙睁眼/闭眼跑
			doTaskSmartStraightFast();
			break;

		case STRAIGHTEYECLOSECOORADJ: // 有墙格子后半段, 遇有墙无墙变化时纵向矫正
			doTaskStraightEyeCloseCoorAdj();
			break;

		case STRAIGHTPOLE: // 沿杆直线
			doTaskStraightPole();
			break;

		case SMARTSTRAIGHTSPURT: // 冲刺前直线补全
			doTaskSmartStraightSpurt();
			break;

		case SMARTSTRAIGHTSPURTL: // 冲刺前直线补全(贴左)
			doTaskSmartStraightSpurtL();
			break;

		case SPURT90IR: // 90度冲刺睁眼直线
			doTaskSpurt90Ir();
			break;

		case SPURT90BLIND: // 90度冲刺闭眼直线
			doTaskSpurt90Blind();
			break;

		case STRAIGHT45: // 45度斜线冲刺(含纵向矫正)
			doTaskStraight45();
			break;

		default:
			LED_On(0);
			break;
		}

		if (emergMon.active == 1) // 急停触发: 停车并延时进入睡眠
		{
			speedSt.freemode = 1;
			LED_On(0);
			motionCtrl.task = MOTION_TASK_NULL;
			speedSt.speedTargetL = 0;
			speedSt.speedTargetR = 0;
#ifndef MOUSE_REMOTE
			/* 只有按键比赛档才在急停后进入休眠(见 main.h 的说明)。
			 * 远程档休眠会跳过下面全部采样与 SAFETY_Monitor, 而上位机侧
			 * LINK_Tick() 仍在门外照发帧 —— 表现为"链路活着但数据冻结"
			 * (upMs 在涨, cpu/adcBatt/battMv 逐字节不变), 且现场很难察觉。
			 * 远程阶段恰恰需要"急停之后仍然看得见", 故不睡眠。 */
			emergMon.delay++;
			if (emergMon.delay == 200)
				speedSt.sleepmode = 1;
#endif
			LINK_EStopTrip(); /* 通知链路层: 置急停锁存, 上位机据此显示并可回传现场日志 */
		}
		// 课程使用代码
		LESSON_ISR();
		// 电机速度闭环控制(编码器采样+测速+PID+PWM, 见 motor.c)
		MOTOR_EncSample();
		MOTOR_LoopControl();

		SAFETY_Monitor();
	}

	/* 蓝牙链路: 遥测组帧/命令解析/标定服务。
	 * 刻意放在 if(sleepmode)/else 之外 —— 休眠与标定死循环期间遥测仍然存活,
	 * 否则远程标定过程中上位机会失去数据。 */
	LINK_Tick();
}
