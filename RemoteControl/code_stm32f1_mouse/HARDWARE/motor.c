/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: motor.c —— 电机驱动(初始化/速度PID/闭环控制)
*********************************************************************************************************
*/
/* 本文件在算法中位置: 执行层--电机速度闭环, 将运动状态机算出的目标速度转为PWM输出。 */

#include "motor.h"

MotorVars motor;	 /* 电机测速状态 */
PidStruct motorPidL; /* 左轮速度PID */
PidStruct motorPidR; /* 右轮速度PID */

/**
 * @brief 电机 PWM 初始化(TIM4, PB6-9 复用推挽输出)
 * @param none
 * @note 安全测试模式(MOUSE_TEST_NO_MOTOR)下不启用 TIM4, 只把 PB6-9 拉成推挽低电平。
 *       电机实验模式(MOUSE_TEST_MOTOR_LAB)下走正常 PWM 初始化 —— 该模式不再定义
 *       MOUSE_TEST_NO_MOTOR, 因此本函数自动落到 #else 分支, 无需改动。
 */
void MOTOR_PwmInit(void)
{
#ifdef MOUSE_TEST_NO_MOTOR
	/* ---- 安全测试模式 ----
	 * 这里**刻意不启用 TIM4 的 PWM 输出**: 只把 PB6..PB9 配成普通推挽输出并拉到低电平,
	 * 且不配置 CCMR/CCER/CR1 —— 所以任何对 TIM4->CCRx 的写入都不会在引脚上产生电平跳变。
	 * 保留 TIM4 时钟使能只是为了"写 CCR 时有合法外设可写", 避免时钟未开时访问寄存器。
	 * 代价: 无法用 PWM 驱动电机 —— 这正是本阶段要的。 */
	RCC->APB1ENR |= 1 << 2; // TIM4 Clock Enable(仅为寄存器写入合法, 不使能 PWM)
	RCC->APB2ENR |= 1 << 3; // GPIOB Clock Enable

	GPIOB->CRL &= 0X00FFFFFF;
	GPIOB->CRL |= 0X33000000; // PB6 7 通用推挽输出 50MHz
	GPIOB->CRH &= 0XFFFFFF00;
	GPIOB->CRH |= 0X00000033; // PB8 9 通用推挽输出 50MHz

	GPIOB->BRR = (1 << 6) | (1 << 7) | (1 << 8) | (1 << 9); // 四路全部输出低电平(电机停)
	return;
#else
	// 主电机
	RCC->APB1ENR |= 1 << 2; // TIM4 Clock Enable
	RCC->APB2ENR |= 1 << 3; // GPIOB Clock Enable

	GPIOB->CRL &= 0X00FFFFFF;
	GPIOB->CRL |= 0XBB000000; // PB6 7复用推挽输出
	GPIOB->CRH &= 0XFFFFFF00;
	GPIOB->CRH |= 0X000000BB; // PB8 9复用推挽输出

	TIM4->ARR = MOTOR_PWM_MAX; // Period
	TIM4->PSC = 1;			   // 2-Divided 36MHz

	TIM4->CCMR1 &= 0x0C0C;	// CH1 CH2 Output Compare
	TIM4->CCMR1 |= 6 << 4;	// CH1 PWM1 MODEL
	TIM4->CCMR1 |= 6 << 12; // CH2 PWM1 MODEL

	TIM4->CCMR2 &= 0x0C0C;	// CH3 CH4 Output Compare
	TIM4->CCMR2 |= 6 << 4;	// CH3 PWM1 MODEL
	TIM4->CCMR2 |= 6 << 12; // CH4 PWM1 MODEL

	TIM4->CCMR1 |= 1 << 3;	// CH1 Preload Enable
	TIM4->CCMR1 |= 1 << 11; // CH2 Preload Enable
	TIM4->CCMR2 |= 1 << 3;	// CH3 Preload Enable
	TIM4->CCMR2 |= 1 << 11; // CH4 Preload Enable

	TIM4->CCER |= 1 << 0;  // OC1 Output Enable
	TIM4->CCER |= 1 << 4;  // OC2 Output Enable
	TIM4->CCER |= 1 << 8;  // OC3 Output Enable
	TIM4->CCER |= 1 << 12; // OC4 Output Enable

	TIM4->CR1 |= 1 << 7; // Auto-reload Preload Enable
	TIM4->CR1 |= 1 << 0; // TIM4 Enable
#endif /* MOUSE_TEST_NO_MOTOR */
}

/**
 * @brief 左轮编码器初始化(TIM2, PA15/PB3 两相输入)
 * @param none
 */
void MOTOR_EncLeftInit(void)
{
	RCC->APB2ENR |= 3 << 2; // GPIOA B Clock Enable
	GPIOA->CRH &= 0x0FFFFFFF;
	GPIOA->CRH |= 0x80000000;
	GPIOB->CRL &= 0xFFFF0FFF;
	GPIOB->CRL |= 0x00008000; // PA15 PB3弱下拉输入

	RCC->APB2ENR |= 1 << 0; // Alternate Function I/O Clock Enable
	RCC->APB1ENR |= 1 << 0; // TIM2 Clock Enable
	AFIO->MAPR &= 0xF8FFFCFF;
	AFIO->MAPR |= 0x02000000; // JTAG-DP SW-DP Clock Disable
	AFIO->MAPR &= 0xFFFFFCFF;
	AFIO->MAPR |= 1 << 8; // TIM2 Partial Remap1

	TIM2->ARR = ENCODER_ARR_VALUE - 1; // top
	TIM2->PSC = 0;					   // 1-Divided
	TIM2->CCER |= 0 << 1;			   // CC1反相
	TIM2->CCMR1 |= 1 << 9;			   // IC2->TI2
	TIM2->CCMR1 |= 1 << 1;			   // IC1->TI1
	TIM2->SMCR |= 0x0003;			   // 编码器模式3
	TIM2->SR = 0x0000;				   // 清中断
	TIM2->CNT = 0x0000;
	TIM2->CR1 |= 1 << 0; // 使能定时器2
}

/**
 * @brief 右轮编码器初始化(TIM3, PB4/PB5 两相输入)
 * @param none
 */
void MOTOR_EncRightInit(void)
{
	RCC->APB2ENR |= 1 << 3; // GPIOB Clock Enable
	GPIOB->CRL &= 0xFF00FFFF;
	GPIOB->CRL |= 0x00880000; // PB4 PB5弱下拉输入

	RCC->APB1ENR |= 1 << 1; // TIM3 Clock Enable
	AFIO->MAPR &= 0XFFFFF3FF;
	AFIO->MAPR |= 1 << 11; // TIM3 Partial Remap1

	TIM3->ARR = ENCODER_ARR_VALUE - 1; // top
	TIM3->PSC = 0;					   // 1-Divided
	TIM3->CCER |= 1 << 1;			   // CC1反相
	TIM3->CCMR1 |= 1 << 8;
	TIM3->CCMR1 |= 1 << 0;
	TIM3->SMCR |= 0x0003; // 编码器模式3
	TIM3->SR = 0x0000;	  // 清中断
	TIM3->CNT = 0x0000;
	TIM3->CR1 |= 1 << 0; // 使能定时器3
}

/**
 * @brief 电机PID误差+积分更新(不算输出)
 * @param pp PID结构体指针(调用前须填好 ref/feedBack/integLimit)
 * @note 每ISR无条件执行: errPrev=err; err=ref-feedBack; errInteg+=err(带±integLimit anti-windup)
 */
void MOTOR_PidUpdateErr(PidStruct *pp)
{
	pp->errPrev = pp->err;
	pp->err = pp->ref - pp->feedBack;
	pp->errInteg += pp->err;
	if (pp->errInteg > pp->integLimit)
		pp->errInteg = pp->integLimit; /* Anti-windup */
	if (pp->errInteg < -pp->integLimit)
		pp->errInteg = -pp->integLimit;
}

/**
 * @brief  电机PID输出计算(用已更新的err/errInteg/errPrev算out)
 * @param  pp PID结构体指针(kp/ki/kd须先填好, err须已由 MOTOR_PidUpdateErr 更新)
 * @note   out = err*kp + errInteg*ki + (err-errPrev)*kd
 */
void MOTOR_PidCalcOut(PidStruct *pp)
{
	pp->out = pp->err * pp->kp + pp->errInteg * pp->ki + (pp->err - pp->errPrev) * pp->kd;
}

/**
 * @brief  电机速度PID计算(误差更新+输出一次算完)
 * @param  pp PID结构体指针(调用前须填好 ref/feedBack/kp/ki/kd/integLimit)
 */
void MOTOR_PidControl(PidStruct *pp)
{
	MOTOR_PidUpdateErr(pp);
	MOTOR_PidCalcOut(pp);
}

/**
 * @brief 编码器采样 + 测速 + PID误差更新
 * @param none
 * @note 含编码器溢出补偿(±ENCODER_SPEED_SIGN_THRESHOLD); 误差更新无条件执行(含 freemode)
 */
void MOTOR_EncSample(void)
{
	// Left Motor
	motor.targetL = speedSt.speedTargetL * sysCfg.speedRatioL; // cm/s -> Pulse/ms 系数越大走得越快
	motor.wheelAngleL = MOTOR_ENCL_VALUE;
	motor.speedL = motor.wheelAngleL - motor.whAngPrevL;
	if (motor.speedL < -ENCODER_SPEED_SIGN_THRESHOLD)
		motor.speedL += ENCODER_ARR_VALUE; // Positive Spillover
	if (motor.speedL > ENCODER_SPEED_SIGN_THRESHOLD)
		motor.speedL -= ENCODER_ARR_VALUE; // Negative Spillover
	motor.whAngPrevL = motor.wheelAngleL;
	motorPidL.ref = (int)motor.targetL;
	motorPidL.feedBack = motor.speedL;
	motorPidL.integLimit = MOTOR_INTEG_LIMIT;
	MOTOR_PidUpdateErr(&motorPidL);

	// Right Motor
	motor.targetR = speedSt.speedTargetR * sysCfg.speedRatioR; // cm/s -> Pulse/ms
	motor.wheelAngleR = MOTOR_ENCR_VALUE;
	motor.speedR = motor.wheelAngleR - motor.whAngPrevR;
	if (motor.speedR < -ENCODER_SPEED_SIGN_THRESHOLD)
		motor.speedR += ENCODER_ARR_VALUE;
	if (motor.speedR > ENCODER_SPEED_SIGN_THRESHOLD)
		motor.speedR -= ENCODER_ARR_VALUE;
	motor.whAngPrevR = motor.wheelAngleR;
	motorPidR.ref = (int)motor.targetR;
	motorPidR.feedBack = motor.speedR;
	motorPidR.integLimit = MOTOR_INTEG_LIMIT;
	MOTOR_PidUpdateErr(&motorPidR);

	speedSt.speedL = motor.speedL / sysCfg.speedRatioL;
	speedSt.speedR = motor.speedR / sysCfg.speedRatioR;
	speedSt.avgSpeed = (speedSt.speedL + speedSt.speedR) / 2;
}

/**
 * @brief 左轮PWM方向输出(含死区)
 * @param pwm PID输出值(已限幅)
 * @note 死区 ±MOTOR_DEADZONE_L(=1)
 */
void MOTOR_LeftPwm(double pwm)
{
	if (pwm >= MOTOR_DEADZONE_L) // 前进-35
	{
		MOTOR_LPWML = 0;
		MOTOR_LPWMH = pwm;
	}
	else if (pwm <= -MOTOR_DEADZONE_L) // 后退
	{
		MOTOR_LPWML = -pwm;
		MOTOR_LPWMH = 0;
	}
	else
	{
		MOTOR_LPWML = 0;
		MOTOR_LPWMH = 0;
	}
}

/**
 * @brief 右轮PWM方向输出(含死区)
 * @param pwm PID输出值(已限幅)
 * @note 死区 ±MOTOR_DEADZONE_R(=35), 与左轮不对称
 */
void MOTOR_RightPwm(double pwm)
{
	if (pwm >= MOTOR_DEADZONE_R)
	{
		MOTOR_RPWML = 0;
		MOTOR_RPWMH = pwm;
	}
	else if (pwm <= -MOTOR_DEADZONE_R)
	{
		MOTOR_RPWML = -pwm;
		MOTOR_RPWMH = 0;
	}
	else
	{
		MOTOR_RPWML = 0;
		MOTOR_RPWMH = 0;
	}
}

/**
 * @brief 速度闭环控制: PID输出 -> 限幅 -> PWM方向输出
 * @param none
 * @note freemode!=0 时输出清零(自由模式/急停); 增益为整定常量
 * @note 安全测试模式(MOUSE_TEST_NO_MOTOR)下恒输出 0 并直接返回 —— 电机绝不动。
 *       编码器采样与测速仍在 MOTOR_EncSample() 中照常进行, 遥测数据不受影响。
 */
void MOTOR_LoopControl(void)
{
#ifdef MOUSE_TEST_NO_MOTOR
	/* ---- 安全测试模式: 电机恒停 ---- */
	speedSt.pwmMotorL = 0;
	speedSt.pwmMotorR = 0;
	MOTOR_LeftPwm(0);
	MOTOR_RightPwm(0);
	return;
#else
	if (speedSt.freemode == 0)
	{ // 30-1-2
#ifdef MOUSE_LESSON_LAB
		/* ---- 课程调参模式: 六增益改为"运行时可改" ----
		 * 红线#6 说的是**数值**不可改, 这里没有改任何数值: 初值仍由 LESSONLAB_Init()
		 * 从 MOTOR_KP_L 等同一个宏赋入, 只是允许上位机在实验中临时覆盖。
		 * 非本模式时走下面 #else 分支, 与原文逐字一致。 */
		motorPidL.kp = lessonGain.kpL;
		motorPidL.ki = lessonGain.kiL;
		motorPidL.kd = lessonGain.kdL;
		MOTOR_PidCalcOut(&motorPidL);
		speedSt.pwmMotorL = motorPidL.out;
		motorPidR.kp = lessonGain.kpR;
		motorPidR.ki = lessonGain.kiR;
		motorPidR.kd = lessonGain.kdR;
		MOTOR_PidCalcOut(&motorPidR);
		speedSt.pwmMotorR = motorPidR.out;
#else
		motorPidL.kp = MOTOR_KP_L;
		motorPidL.ki = MOTOR_KI_L;
		motorPidL.kd = MOTOR_KD_L; // 也可适当调大，太低了对应60+的搜索速度会抽风， 11.21-11.20对应低电量跑的还行
		MOTOR_PidCalcOut(&motorPidL);
		speedSt.pwmMotorL = motorPidL.out;
		motorPidR.kp = MOTOR_KP_R;
		motorPidR.ki = MOTOR_KI_R;
		motorPidR.kd = MOTOR_KD_R;
		MOTOR_PidCalcOut(&motorPidR);
		speedSt.pwmMotorR = motorPidR.out;
#endif
	}
	else
	{
		speedSt.pwmMotorL = 0;
		speedSt.pwmMotorR = 0;
	}

	if (speedSt.pwmMotorL > MOTOR_PWM_LIMIT)
		speedSt.pwmMotorL = MOTOR_PWM_LIMIT;
	if (speedSt.pwmMotorL < -MOTOR_PWM_LIMIT)
		speedSt.pwmMotorL = -MOTOR_PWM_LIMIT;
	MOTOR_LeftPwm(speedSt.pwmMotorL);

	if (speedSt.pwmMotorR > MOTOR_PWM_LIMIT)
		speedSt.pwmMotorR = MOTOR_PWM_LIMIT;
	if (speedSt.pwmMotorR < -MOTOR_PWM_LIMIT)
		speedSt.pwmMotorR = -MOTOR_PWM_LIMIT;
	MOTOR_RightPwm(speedSt.pwmMotorR);
#endif /* MOUSE_TEST_NO_MOTOR */
}
