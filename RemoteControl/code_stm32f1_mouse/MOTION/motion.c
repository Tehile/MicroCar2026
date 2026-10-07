/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: motion.c —— 低级运动控制(直线/转弯/原地转/姿态调整)
*********************************************************************************************************
*/
#include "motion.h"
/**
 * @brief  45度直行指定距离
 * @param  dis
 */
void MOTION_Straight45(u16 dis)
{
	MOTION_SetTrack(0);
	motionCtrl.pidErrPrev = 0;
	motionCtrl.pidErr = 0;
	gyroStr.anglePrev = 0;
	gyroStr.anglePrev2 = 0;
	gyroStr.anglePeak = 0;
	gyroStr.anglePeakPrev = 0;
	gyroStr.angleAcc = 0;
	motionCtrl.task = STRAIGHT45;
	while (odomStr.cellOdom < dis)
	{
		if (searchSt.speedmode == SPURT_SPEED_45_LOW)
		{
			if (dis > 180)
			{
				if (odomStr.cellOdom < 90)
					speedCfg.fastSprint = (spurtSpd.l45TopSpeed - spurtSpd.l45BottomSpeed) * odomStr.cellOdom / 90 + spurtSpd.l45BottomSpeed;
				else if (odomStr.cellOdom < dis - 90)
					speedCfg.fastSprint = spurtSpd.l45TopSpeed;
				else if (odomStr.cellOdom < dis)
					speedCfg.fastSprint = (spurtSpd.l45TopSpeed - spurtSpd.l45BottomSpeed) * (dis - odomStr.cellOdom) / 90 + spurtSpd.l45BottomSpeed;
			}
			else
				speedCfg.fastSprint = spurtSpd.l45BottomSpeed;
		}
	}
}

/**
 * @brief 90度直线冲刺: 按档位/方向设置冲刺PID增益与顶底速
 * @note  根据 speedmode/taskSt.goOrBack/objectGoToFlag 选择 起步/回坐标/返回/前进 四档
 *        增益与速度参数; 参数为整定常量不可改
 */
static void MOTION_Straight90SetGainAndParam(void)
{
	if (spurtSpd.initialSpeed < 50) // 起步
	{
		spurtCfg.irKp1 = 0.31;
		spurtCfg.irKp2 = 0.21;
		spurtCfg.irKd = 0.1;
		spurtCfg.gyroKp = 0.28;
		spurtCfg.gyroKd = 0.10;
		if (taskSt.goOrBack == 1) // 返回起步
		{
			spurtSpd.topSpeed = spurtSpd.backTopSpeed;
			spurtSpd.bottomSpeed = spurtSpd.backBottomSpeed;
			spurtSpd.spurtAccRatio = spurtSpd.spurtAccRatio90;
			spurtSpd.spurtDecRatio = spurtSpd.spurtDecRatio90;
		}
		else
		{
			if (searchSt.speedmode == SPURT_SPEED_90_LOW) // 直线起步
			{
				spurtSpd.topSpeed = spurtSpd.l90TopSpeed;
				spurtSpd.bottomSpeed = spurtSpd.l90BottomSpeed;
				spurtSpd.spurtAccRatio = spurtSpd.spurtAccRatio90;
				spurtSpd.spurtDecRatio = spurtSpd.spurtDecRatio90;
			}

			if (searchSt.speedmode == SPURT_SPEED_45_LOW) // 斜线起步
			{
				spurtSpd.topSpeed = spurtSpd.lTopSpeed;
				spurtSpd.bottomSpeed = spurtSpd.lBottomSpeed;
				spurtSpd.spurtAccRatio = spurtSpd.spurtAccRatio45;
				spurtSpd.spurtDecRatio = spurtSpd.spurtDecRatio45;
			}
		}
	}
	else
	{
		if (mazeState.objectGoToFlag == 1) // 搜索返回指定坐标
		{
			spurtCfg.irKp1 = 0.5;
			spurtCfg.irKp2 = 0.35;
			spurtCfg.irKd = 2;
			spurtCfg.gyroKp = 0.4;
			spurtCfg.gyroKd = 0.4;
		}
		else
		{
			if (taskSt.goOrBack == 1) // 返回
			{
				spurtCfg.irKp1 = 0.5;
				spurtCfg.irKp2 = 0.35;
				spurtCfg.irKd = 1;
				spurtCfg.gyroKp = 0.25;
				spurtCfg.gyroKd = 0.08;
				spurtSpd.topSpeed = spurtSpd.backTopSpeed;
				spurtSpd.bottomSpeed = spurtSpd.backBottomSpeed;
				spurtSpd.spurtAccRatio = spurtSpd.spurtAccRatio90;
				spurtSpd.spurtDecRatio = spurtSpd.spurtDecRatio90;
			}
			else
			{
				if (searchSt.speedmode == SPURT_SPEED_90_LOW) // 直线冲刺
				{
					spurtCfg.irKp1 = 0.4;
					spurtCfg.irKp2 = 0.3;
					spurtCfg.irKd = 0.3;
					spurtCfg.gyroKp = 0.25;
					spurtCfg.gyroKd = 0.08;
					spurtSpd.topSpeed = spurtSpd.l90TopSpeed;
					spurtSpd.bottomSpeed = spurtSpd.l90BottomSpeed;
					spurtSpd.spurtAccRatio = spurtSpd.spurtAccRatio90;
					spurtSpd.spurtDecRatio = spurtSpd.spurtDecRatio90;
				}

				if (searchSt.speedmode == SPURT_SPEED_45_LOW) // 斜线冲刺
				{
					spurtCfg.irKp1 = 0.4;
					spurtCfg.irKp2 = 0.3;
					spurtCfg.irKd = 0.3;
					spurtCfg.gyroKp = 0.4;
					spurtCfg.gyroKd = 0.4;
					spurtSpd.topSpeed = spurtSpd.lTopSpeed;
					spurtSpd.bottomSpeed = spurtSpd.lBottomSpeed;
					spurtSpd.spurtAccRatio = spurtSpd.spurtAccRatio45;
					spurtSpd.spurtDecRatio = spurtSpd.spurtDecRatio45;
				}
			}
		}
	}
}

/**
 * @brief 90度直线冲刺速度计算: 按档位/方向生成速度曲线并限幅
 * @param dis 目标距离(用于减速段计算)
 * @note  计算结果写 speedCfg.search; 曲线参数为整定常量不可改
 */
static void MOTION_Straight90CalcSpeed(u16 dis)
{
	if (spurtSpd.initialSpeed < 50)
	{
		if (odomStr.cellOdom < spurtSpd.bottomSpeed / (spurtSpd.spurtAccRatio + 0.2))
			speedCfg.search = (spurtSpd.spurtAccRatio + 0.2) * odomStr.cellOdom;
		else if (odomStr.cellOdom < (dis - (spurtSpd.bottomSpeed / (spurtSpd.spurtAccRatio + 0.2))) * (spurtSpd.spurtDecRatio / (spurtSpd.spurtAccRatio + spurtSpd.spurtDecRatio)) + (spurtSpd.bottomSpeed / (spurtSpd.spurtAccRatio + 0.2)))
			speedCfg.search = spurtSpd.spurtAccRatio * (odomStr.cellOdom - (spurtSpd.bottomSpeed / (spurtSpd.spurtAccRatio + 0.2))) + spurtSpd.bottomSpeed;
		else
			speedCfg.search = spurtSpd.spurtDecRatio * (dis - odomStr.cellOdom) + spurtSpd.bottomSpeed;
		if (speedCfg.search < 10)
			speedCfg.search = 10;
		if (speedCfg.search > spurtSpd.topSpeed)
			speedCfg.search = spurtSpd.topSpeed;
	}
	else
	{
		if (mazeState.objectGoToFlag == 1)
		{
			if (dis > 180)
			{
				if (odomStr.cellOdom < 100)
					speedCfg.search = (spurtSpd.objGoToTopSpd - spurtSpd.objGoToBtmSpd) * odomStr.cellOdom / 100 + spurtSpd.objGoToBtmSpd;
				else if (odomStr.cellOdom > dis - 80 && odomStr.cellOdom < dis)
					speedCfg.search = (spurtSpd.objGoToTopSpd - spurtSpd.objGoToBtmSpd) * (dis - odomStr.cellOdom) / 80 + spurtSpd.objGoToBtmSpd;
			}
			else
				speedCfg.search = spurtSpd.objGoToBtmSpd;
		}
		else
		{
			if (odomStr.cellOdom < dis * (spurtSpd.spurtDecRatio / (spurtSpd.spurtAccRatio + spurtSpd.spurtDecRatio)))
				speedCfg.search = spurtSpd.spurtAccRatio * odomStr.cellOdom + spurtSpd.bottomSpeed;
			else
				speedCfg.search = spurtSpd.spurtDecRatio * (dis - odomStr.cellOdom) + spurtSpd.bottomSpeed;
			if (speedCfg.search > spurtSpd.topSpeed)
				speedCfg.search = spurtSpd.topSpeed;
		}
	}
}

/**
 * @brief 90度直线冲刺(90度冲刺/斜线冲刺/回坐标共用)
 * @param dis 目标距离(mm, 冲刺格数换算)
 */
void MOTION_Straight90(u16 dis)
{
	searchSt.turnOffset = 0;

	motionCtrl.pidErrPrev = 0;
	motionCtrl.pidErr = 0;
	MOTION_SetTrack(0);
	spurtSpd.initialSpeed = speedSt.avgSpeed;
	while (odomStr.cellOdom < dis) // 到达目标距离前循环: 逐周期设增益/算速度/选任务
	{
		MOTION_Straight90SetGainAndParam();
		MOTION_Straight90CalcSpeed(dis);
		if (odomStr.cellOdom % 180 > 20 && odomStr.cellOdom % 180 < 100)
			motionCtrl.task = SPURT90BLIND; // 闭眼走
		else
		{
			if (wallSns.left == 1 || wallSns.right == 1)
				motionCtrl.task = SPURT90IR; // 睁眼走
			else
				motionCtrl.task = SPURT90BLIND; // 闭眼走
		}
		if (irDist.f < 20)
			emergMon.active = 1;
	}
}

/**
 * @brief  鼠级右转(含矫正)
 */
void MOTION_MouseTurnRight()
{
	searchSt.skipCoorUpdate = 0;
	MOTION_Before90Turn(TurnRight);
	searchSt.lastSearchAct = TurnRight;
	if (wallSns.left == 1)
	{
		searchSt.turnRightOffset = sysCfg.distToWall - ((irDist.l + irDist.lPrev + irDist.lPrev2) / 3);
		searchSt.turnOffset = (irDist.r + irDist.rPrev + irDist.rPrev2) / 3 - sysCfg.distToWall;
	}
	else
	{
		searchSt.turnRightOffset = 0;
		searchSt.turnOffset = 0;
	}
	if (irDist.r < 50) // 明明有墙却还命令转弯，则报错
	{
		flashSt.flashwrite = 0;
		speedSt.speedTargetL = 0;
		speedSt.speedTargetR = 0;
		motionCtrl.task = NULL;
		DELAY_Ms(300);
		speedSt.sleepmode = 1;
		while (1)
		{
			speedSt.sleepmode = 1;
			LED_Off(0);
			DELAY_Ms(500);
			LED_On(7);
			LED_On(8);
			DELAY_Ms(500);
		}
	}
	MOTION_Turn(RT90SEARCH);
	motionCtrl.task = SMARTSTRAIGHT;
	mazeState.mouseDirection = (mazeState.mouseDirection + 1) % 4;
	MOTION_SetTrack(180 - searchSt.turnRightOffset); // 转弯之后进SEARCH_Search(),需要立即进入判断，满足的条件是cellOdom>=180，在此人为制造该条件。
	// 若右转之前车偏左，则o<0，因需要在SEARCH_Search()中多走完差的距离再进入判断，因此将cellOdom设置为小于180，即basetrack前移一点
}

/**
 * @brief  鼠级左转(含矫正)
 */
void MOTION_MouseTurnLeft()
{
	searchSt.skipCoorUpdate = 0;
	MOTION_Before90Turn(TurnLeft);
	searchSt.lastSearchAct = TurnLeft;
	if (wallSns.right == 1)
	{
		searchSt.turnLeftOffset = (irDist.r + irDist.rPrev + irDist.rPrev2) / 3 - sysCfg.distToWall;
		searchSt.turnOffset = (irDist.r + irDist.rPrev + irDist.rPrev2) / 3 - sysCfg.distToWall;
	}
	else
	{
		searchSt.turnLeftOffset = 0;
		searchSt.turnOffset = 0;
	}
	if (irDist.l < 50) // 明明有墙却还命令转弯，则报错
	{
		flashSt.flashwrite = 0;
		speedSt.speedTargetL = 0;
		speedSt.speedTargetR = 0;
		motionCtrl.task = NULL;
		DELAY_Ms(300);
		speedSt.sleepmode = 1;
		while (1)
		{
			speedSt.sleepmode = 1;
			LED_Off(0);
			DELAY_Ms(500);
			LED_On(5);
			LED_On(6);
			DELAY_Ms(500);
		}
	}
	MOTION_Turn(LT90SEARCH);
	motionCtrl.task = SMARTSTRAIGHT;
	mazeState.mouseDirection = (mazeState.mouseDirection + 3) % 4;
	MOTION_SetTrack(180 + searchSt.turnLeftOffset);
}

/**
 * @brief  鼠级回转180度
 */
void MOTION_MouseTurnBack()
{
	searchSt.skipCoorUpdate = 0;
	MOTION_Before180Turn();
	searchSt.lastSearchAct = Straight;
	MOTION_TurnAround();
	if (flashSt.blockWriteFlag == 1)
	{
		FLASH_SaveBlockData(); // FLASH_SaveSearchStack();
		flashSt.blockWriteFlag = 0;
	}
	MOTION_After180Turn();
	mazeState.mouseDirection = (mazeState.mouseDirection + 2) % 4;
	MAZE_CoorUpdate();
	MOTION_SetTrack(0);
}

/**
 * @brief  鼠级特殊回转
 */
void MOTION_MouseTurnBackSpecial()
{
	searchSt.skipCoorUpdate = 0;
	MOTION_Before180Turn();
	MOTION_TurnAround();
	mazeState.mouseDirection = (mazeState.mouseDirection + 2) % 4;
	MOTION_SetTrack(0);
}

/**
 * @brief  鼠级高速直行(按距离划分控制)
 * @param  cNBlock
 */
void MOTION_MouseGoahead(char cNBlock) // 更高速地走直线，利用已知的有无墙情况(初始化时处理一下)，直接按照距离划分控制方式，而非按照格子。如：前360mm用左侧矫正，后180mm用右侧矫正。类似的命令方式。另外要加上转弯后加速。
{
	u8 n = 0;
	searchSt.skipCoorUpdate = 1;
	motionCtrl.pidErrPrev = 0;
	motionCtrl.pidErr = 0;
	gyroStr.anglePrev = 0;
	gyroStr.anglePrev2 = 0;
	gyroStr.anglePeak = 0;
	gyroStr.anglePeakPrev = 0;
	motionCtrl.task = SMARTSTRAIGHT;
	while (1)
	{
		if (odomStr.cellOdom >= 180) // 到达逻辑节点。注意无论直走还是转弯，一格之后都应满足该条件！!!!!!!!!!之后将cellOdom-180
		{
			straightCells++; // 连续直线格数
			MOTION_SetTrack(0);
			MAZE_CoorUpdate();
			n++;
			if (n == cNBlock)
			{
				straightCells = 1;
				break;
			}
		}
		if (odomStr.cellOdom > 90 && odomStr.cellOdom < 180)
			; // 直线-直线时到判断条件时必满足。直线-跳出-直线？。直线-转弯时到判断条件时必满足。转弯-转弯时可能需要补全路程。
		else
		{
			if (wallSns.left == 1 || wallSns.right == 1)
				SEARCH_HasWall(); // 相当于在调用SEARCH_Search()的时刻读一次墙，作为该格子睁眼还是闭眼的依据，可能出现问题！
			else if (wallSns.left == 0 && wallSns.right == 0)
				SEARCH_NoWall();
		}
	}
}

/**
 * @brief 鼠级转弯前准备
 * @param pattern 动作码
 */
void MOTION_BeforeMouseTurn(u8 pattern)
{
	int beforeturntrack;
	motionCtrl.pidErr = 0;
	motionCtrl.pidErrPrev = 0;
	switch (pattern)
	{
	// low speed
	case LV45L:
		beforeturntrack = 3;
		speedCfg.fastSprint = 100;
		goto BFLV;
	case LV90L:
		beforeturntrack = 7;
		speedCfg.fastSprint = 100;
		goto BFLV;
	case LV135L:
		beforeturntrack = 9;
		speedCfg.fastSprint = 100;
		goto BFLV;
	case RV45L:
		beforeturntrack = 3;
		speedCfg.fastSprint = 100;
		goto BFRV;
	case RV90L:
		beforeturntrack = 7;
		speedCfg.fastSprint = 100;
		goto BFRV;
	case RV135L:
		beforeturntrack = 9;
		speedCfg.fastSprint = 100;
		goto BFRV;

	case LT45L:
		beforeturntrack = 0;
		speedCfg.search = 100;
		wallSns.wallThresh = 110;
		goto BFL;
	case LT90L:
		beforeturntrack = 10;
		speedCfg.search = 70;
		goto BFL;
	case LT135L:
		beforeturntrack = 14;
		speedCfg.search = 100;
		goto BFL;
	case L180L:
		beforeturntrack = 0;
		speedCfg.search = 100;
		goto BFL;

	case RT45L:
		beforeturntrack = 0;
		speedCfg.search = 100;
		wallSns.wallThresh = 110;
		goto BFR;
	case RT90L:
		beforeturntrack = 10;
		speedCfg.search = 70;
		goto BFR;
	case RT135L:
		beforeturntrack = 14;
		speedCfg.search = 100;
		goto BFR;
	case R180L:
		beforeturntrack = 0;
		speedCfg.search = 100;
		goto BFR;

	// medium speed
	case LV45M:
		beforeturntrack = 7;
		goto BFLV;
	case LV90M:
		beforeturntrack = 7;
		goto BFLV;
	case LV135M:
		beforeturntrack = 7;
		goto BFLV;
	case RV45M:
		beforeturntrack = 7;
		goto BFRV;
	case RV90M:
		beforeturntrack = 7;
		goto BFRV;
	case RV135M:
		beforeturntrack = 7;
		goto BFRV;

	case LT45M:
		beforeturntrack = 0;
		goto BFL;
	case LT90M:
		beforeturntrack = 23;
		goto BFL;
	case LT135M:
		beforeturntrack = 23;
		goto BFL;
	case L180M:
		beforeturntrack = 0;
		goto BFL;

	case RT45M:
		beforeturntrack = 0;
		goto BFR;
	case RT90M:
		beforeturntrack = 23;
		goto BFR;
	case RT135M:
		beforeturntrack = 23;
		goto BFR;
	case R180M:
		beforeturntrack = 0;
		goto BFR;

	// SPURT_SPEED_HIGH SPEED
	case LV45H:
		beforeturntrack = 0;
		goto BFLV;
	case LV90H:
		beforeturntrack = 0;
		goto BFLV;
	case LV135H:
		beforeturntrack = 0;
		goto BFLV;
	case RV45H:
		beforeturntrack = 0;
		goto BFRV;
	case RV90H:
		beforeturntrack = 0;
		goto BFRV;
	case RV135H:
		beforeturntrack = 0;
		goto BFRV;

	case LT45H:
		beforeturntrack = 0;
		goto BFL;
	case LT90H:
		beforeturntrack = 0;
		goto BFL;
	case LT135H:
		beforeturntrack = 0;
		goto BFL;
	case L180H:
		beforeturntrack = 0;
		goto BFL;

	case RT45H:
		beforeturntrack = 0;
		goto BFR;
	case RT90H:
		beforeturntrack = 0;
		goto BFR;
	case RT135H:
		beforeturntrack = 0;
		goto BFR;
	case R180H:
		beforeturntrack = 0;
		goto BFR;

	BFLV:
		while (1)
		{
			motionCtrl.task = STRAIGHT45;
			if (wallSns.critL3 == 1 && wallSns.critL2 == 1 && wallSns.critL1 == 1 && wallSns.critL == 0)
				break;
		}
		MOTION_SetTrack(0);
		while (odomStr.cellOdom < beforeturntrack)
			motionCtrl.task = STRAIGHT45;
		break;
	BFRV:
		while (1)
		{
			motionCtrl.task = STRAIGHT45;
			if (wallSns.critR3 == 1 && wallSns.critR2 == 1 && wallSns.critR1 == 0 && wallSns.critR == 0)
				break;
		}
		MOTION_SetTrack(0);
		while (odomStr.cellOdom < beforeturntrack)
			motionCtrl.task = STRAIGHT45;
		break;
	BFL:
		while (1)
		{
			motionCtrl.task = SMARTSTRAIGHTSPURTL;
			if (wallSns.leftPrev == 1 && wallSns.left == 0)
				break;
		}
		wallSns.wallThresh = 160;
		MOTION_SetTrack(0);
		while (odomStr.cellOdom < beforeturntrack)
			motionCtrl.task = SMARTSTRAIGHTSPURTL;
		break;
	BFR:
		while (1)
		{
			motionCtrl.task = SMARTSTRAIGHTSPURTL;
			if (wallSns.rightPrev == 1 && wallSns.right == 0)
				break;
		}
		wallSns.wallThresh = 160;
		MOTION_SetTrack(0);
		while (odomStr.cellOdom < beforeturntrack)
			motionCtrl.task = SMARTSTRAIGHTSPURTL;
		break;
	}
}

/**
 * @brief  鼠级转弯(不用已知墙矫正)
 * @param  pattern
 */
void MOTION_MouseTurn(u8 pattern)
{
	MOTION_BeforeMouseTurn(pattern);
	MOTION_Turn(pattern);
	LED_Off(0);
	speedCfg.search = 70;
	speedCfg.fastSprint = 100;
}

float turnDelay = 16.5; /* 转弯延迟格数(float: motion.c 有 turnDelay-3.5 浮点比较) */

/**
 * @brief  设置格内里程基准(track)
 * @param  t
 */
void MOTION_SetTrack(int t) // 修正相对坐标
{
	odomStr.trackBase = odomStr.trackAbs - t;
	odomStr.cellOdom = t;
}

/**
 * @brief  设置角度基准
 * @param  a
 */
void MOTION_SetAngle(int a) // 把角度基准逆时针(-)或顺时针(+)转几度
{
	gyroStr.angleAcc = -a * sysCfg.gyroRatio * (-1);
	odomStr.angleBase = odomStr.angleAbs + a;
}

////////////////////////////////////////////////////////////////////////////////////////
/**
 * @brief  加速任务
 */
void MOTION_Acc()
{
	MOTION_SetTrack(0);
	motionCtrl.pidErr = 0;
	motionCtrl.pidErrPrev = 0;
	pidGain.accRatio = (double)(speedCfg.turn - ACCV0) / searchSt.accdistance; // 10是初始阶跃速度
	while (odomStr.cellOdom < searchSt.accdistance)
		motionCtrl.task = ACC;
}

/**
 * @brief  停止任务
 */
void MOTION_Stop()
{
	MOTION_SetTrack(-searchSt.decdistance);
	motionCtrl.pidErrPrev = 0;
	motionCtrl.pidErr = 0;
	motionCtrl.tick = 0;
	speedSt.stoptick = 0;
	pidGain.decRatio = (double)speedSt.avgSpeed / searchSt.decdistance;
	while (motionCtrl.tick < 5)
	{
		motionCtrl.task = STOP;
		if (speedSt.stoptick > 1000)
			break; // 1s超时
	}
	MOTION_SetTrack(0);
}

/**
 * @brief  直行指定距离
 * @param  dis
 */
void MOTION_GoStraight(u16 dis)
{
	motionCtrl.pidErrPrev = 0;
	motionCtrl.pidErr = 0;
	while (odomStr.cellOdom < dis)
		motionCtrl.task = GOSTRAIGHT;
}

/**
 * @brief  减速指定距离
 * @param  dis
 */
void MOTION_SlowDown(u16 dis)
{
	long begintrack;
	motionCtrl.pidErr = 0;
	motionCtrl.pidErrPrev = 0;
	motionCtrl.tick = 0;
	begintrack = odomStr.trackAbs;
	while (speedSt.avgSpeed > speedCfg.turn && odomStr.trackAbs - begintrack < dis)
		motionCtrl.task = SLOWDOWN;
	while (odomStr.trackAbs - begintrack < dis)
		motionCtrl.task = GOSTRAIGHT;
}

/**
 * @brief  加速指定距离
 * @param  dis
 */
void MOTION_SpeedUp(u16 dis)
{
	long begintrack;
	motionCtrl.pidErr = 0;
	motionCtrl.pidErrPrev = 0;
	motionCtrl.tick = 0;
	begintrack = odomStr.trackAbs;
	while (speedSt.avgSpeed < speedCfg.search && odomStr.trackAbs - begintrack < dis)
		motionCtrl.task = SPEEDUP;
	while (odomStr.trackAbs - begintrack < dis)
	{
		speedSt.speedTargetL = speedCfg.search;
		speedSt.speedTargetR = speedCfg.search;
	}
}

/**
 * @brief 转弯参数查找表条目(MOTION_Turn 各转弯模式参数)
 * @note  每行对应一个动作码 pattern: 转向角 + 前馈陀螺/左右轮速表指针 + 表长度。
 *        表为整定常量仅引用; length = sizeof(gyro表) 编译期常量
 */
typedef struct
{
	u8 pattern;		   /* 动作码(枚举值, 匹配键) */
	int angle;		   /* MOTION_SetAngle 参数 */
	const short *gyro; /* 前馈陀螺角度表(turnTbl.gyro) */
	const short *vl;   /* 左轮速度表(turnTbl.vl) */
	const short *vr;   /* 右轮速度表(turnTbl.vr) */
	u16 length;		   /* = sizeof(gyro表), 用于 while(motionCtrl.tick<length/2-2) */
} TurnParam_t;

static const TurnParam_t turnParamTable[] = {
	{LT90SPURT, -90, gyroLSpurt, vinnerSpurt, vouterSpurt, sizeof(gyroLSpurt)},
	{RT90SPURT, 90, gyroRSpurt, vouterSpurt, vinnerSpurt, sizeof(gyroRSpurt)},
	{LT90SEARCH, -90, gyroLSearch, vinnerSearch, vouterSearch, sizeof(gyroLSearch)},
	{RT90SEARCH, 90, gyroRSearch, vouterSearch, vinnerSearch, sizeof(gyroRSearch)},
	{POINTL90, -90, gyroLPoint, vinnerPoint, vouterPoint, sizeof(gyroLPoint)},
	{POINTR90, 90, gyroRPoint, vouterPoint, vinnerPoint, sizeof(gyroRPoint)},
#ifdef LOW_ENABLE
	{LT90L, -90, gyroLt90Low, vinnerT90Low, vouterT90Low, sizeof(gyroLt90Low)},
	{RT90L, 90, gyroRt90Low, vouterT90Low, vinnerT90Low, sizeof(gyroRt90Low)},
	{LV90L, -90, gyroLv90Low, vinnerV90Low, vouterV90Low, sizeof(gyroLv90Low)},
	{RV90L, 90, gyroRv90Low, vouterV90Low, vinnerV90Low, sizeof(gyroRv90Low)},
	{LT45L, -45, gyroLt45Low, vinnerT45Low, vouterT45Low, sizeof(gyroLt45Low)},
	{RT45L, 45, gyroRt45Low, vouterT45Low, vinnerT45Low, sizeof(gyroRt45Low)},
	{LV45L, -45, gyroLv45Low, vinnerV45Low, vouterV45Low, sizeof(gyroLv45Low)},
	{RV45L, 45, gyroRv45Low, vouterV45Low, vinnerV45Low, sizeof(gyroRv45Low)},
	{LT135L, -135, gyroLt135Low, vinnerT135Low, vouterT135Low, sizeof(gyroLt135Low)},
	{RT135L, 135, gyroRt135Low, vouterT135Low, vinnerT135Low, sizeof(gyroRt135Low)},
	{LV135L, -135, gyroLv135Low, vinnerV135Low, vouterV135Low, sizeof(gyroLv135Low)},
	{RV135L, 135, gyroRv135Low, vouterV135Low, vinnerV135Low, sizeof(gyroRv135Low)},
	{L180L, -180, gyroL180Low, vinner180Low, vouter180Low, sizeof(gyroL180Low)},
	{R180L, 180, gyroR180Low, vouter180Low, vinner180Low, sizeof(gyroR180Low)},
#endif
};

/**
 * @brief 按动作码执行一次转弯(低级)
 * @param pattern 动作码, 查 turnParamTable 得角度与前馈/速度表
 * @note 命中行设角度+表指针+length 后走 TURN 状态机; 未命中则 length 保持0
 */
void MOTION_Turn(u8 pattern)
{
	u16 length = 0;
	u8 i;
	motionCtrl.pidErrPrev = 0;
	motionCtrl.pidErr = 0;
	motionCtrl.pidErrInteg = 0;
	motionCtrl.tick = 0;
	for (i = 0; i < sizeof(turnParamTable) / sizeof(turnParamTable[0]); i++)
	{
		if (turnParamTable[i].pattern == pattern)
		{
			MOTION_SetAngle(turnParamTable[i].angle);
			turnTbl.gyro = turnParamTable[i].gyro;
			turnTbl.vl = turnParamTable[i].vl;
			turnTbl.vr = turnParamTable[i].vr;
			length = turnParamTable[i].length;
			break;
		}
	}
	motionCtrl.task = TURN;
	while (motionCtrl.tick < length / 2 - 2)
		;
	MOTION_SetTrack(0);
}

/**
 * @brief  回转180度
 */
void MOTION_TurnAround()
{
	MOTION_SetAngle(180);
	turnTbl.gyro = gyroAround;
	turnTbl.vl = vlAround;
	turnTbl.vr = vrAround;
	motionCtrl.pidErrPrev = 0;
	motionCtrl.pidErr = 0;
	motionCtrl.tick = 0;
	motionCtrl.step = 0;
	motionCtrl.task = TURNAROUND;
	while (1)
		if (motionCtrl.step == 1 && motionCtrl.tick > 10)
			break; // 10
	MOTION_SetTrack(0);
}

/**
 * @brief  原地右转90度
 */
/**
 * @brief  原地转90度公共执行体(PointTurnRight/Left 共用, 镜像参数化)
 * @param  dir  转向: TurnRight=右转(+90)/ TurnLeft=左转(-90)
 * @note   右/左两版为规则镜像: SetAngle 符号、gyro 目标、vl/vr 的 outer/inner 分配对调;
 *         其余(PID 清零/motionCtrl.tick/step/motionTask/等待/SetTrack)完全一致, 逻辑等价。
 */
static void MOTION_PointTurnExec(char dir)
{
	if (dir == TurnRight)
	{
		MOTION_SetAngle(90);
		turnTbl.gyro = gyroRPoint;
		turnTbl.vl = vouterPoint;
		turnTbl.vr = vinnerPoint;
	}
	else
	{
		MOTION_SetAngle(-90);
		turnTbl.gyro = gyroLPoint;
		turnTbl.vl = vinnerPoint;
		turnTbl.vr = vouterPoint;
	}
	motionCtrl.pidErrPrev = 0;
	motionCtrl.pidErr = 0;
	motionCtrl.tick = 0;
	motionCtrl.step = 0;
	motionCtrl.task = POINTTURN90;
	while (1)
		if (motionCtrl.step == 1 && motionCtrl.tick > 10)
			break;
	MOTION_SetTrack(0);
}

/**
 * @brief  原地右转90度
 */
void MOTION_PointTurnRight()
{
	MOTION_PointTurnExec(TurnRight);
}

/**
 * @brief  原地左转90度
 */
void MOTION_PointTurnLeft()
{
	MOTION_PointTurnExec(TurnLeft);
}

/**
 * @brief  90度转弯前减速与姿态调整
 * @param  Direction
 */
void MOTION_Before90Turn(u8 Direction) // 减速，利用前方传感器调整姿态，判断转弯开始点，获取矫正距离
{
	// 	int disltmp,disrtmp;
	// 	MOTION_SlowDown()				//需要能够修正纵向坐标！！！！！
	// 	if(speed>speedTurn+10)MOTION_SlowDown(50);			//若直线高速搜索首次切换到转弯，则这段距离只能用于减速不能矫正(事实上直线搜索之后角度误差也比较小无需矫正)。
	// 	else						//若处于连续转弯期间，则用这段距离来矫正姿态。
	// 	{
	motionCtrl.pidErr = 0;
	motionCtrl.pidErrPrev = 0;
	motionCtrl.task = BEFORE90TURN; // 根据前方是否有墙判断矫正方式，两种应当可以随意切换，因为前方墙在远距离时容易误判。
	while (1)
	{
		// 			if(cellOdom>=(44-turnOffset)) {turnOffset=0;break;}		//出弯到下一次入弯之间的直线距离
		if (searchSt.lastSearchAct == Straight) // 直线接转弯 多补全20-12mm
		{
			if (Direction == TurnLeft) // 左转前补全6mm
			{
				if (odomStr.cellOdom >= turnDelay + 3)
				{
					searchSt.turnOffset = 0;
					break;
				}
			}
			else
			{
				if (odomStr.cellOdom >= turnDelay - 3.5) // 右转
				{
					searchSt.turnOffset = 0;
					break;
				}
			}
		}
		else if (searchSt.lastSearchAct == TurnLeft)
		{
			if (Direction == TurnLeft) // 左转前补全6mm
			{
				if (odomStr.cellOdom >= turnDelay + 3)
				{
					searchSt.turnOffset = 0;
					break;
				}
			}
			else
			{
				if (odomStr.cellOdom >= turnDelay - 3.5) // 右转
				{
					searchSt.turnOffset = 0;
					break;
				}
			}
		}
		else if (searchSt.lastSearchAct == TurnRight)
		{
			if (Direction == TurnLeft) // 左转前补全6mm
			{
				if (odomStr.cellOdom >= turnDelay + 3)
				{
					searchSt.turnOffset = 0;
					break;
				}
			}
			else
			{
				if (odomStr.cellOdom >= turnDelay - 3.5) // 右转
				{
					searchSt.turnOffset = 0;
					break;
				}
			}
		}
	}
	// 	}
}

static u16 interCurveDist; /* 弯间直线距离(原disbf180): 出弯到下一次入弯的直线距离(模块内 static) */
/**
 * @brief  180度转弯前刹车与定位
 */
void MOTION_Before180Turn() // 减速刹车，调整纵向距离，利用前方和左右方传感器调整水平位置到中间，角度正对墙面。
{
	MOTION_SetTrack(0);
	motionCtrl.task = SMARTSTRAIGHT;
	while (odomStr.cellOdom < 20)
		; // 一开始前方墙面容易误判，先走一段再判断
	if (wallSns.left == 1)
		searchSt.afterTurnSide = 1;
	else if (wallSns.right == 1)
		searchSt.afterTurnSide = 2;
	else
		searchSt.afterTurnSide = 0;
	while (odomStr.cellOdom < 40)
		;

	if (wallSns.front == 0) // 根据该点探测的墙信息选择接下来的动作:刹车或用前墙定位
	{
		if (searchSt.lastSearchAct == Straight)
			MOTION_GoStraight(70); // 之前是100,走到cellOdom=60，不是再走60
		else
			MOTION_GoStraight(50); // 之前是100，走到cellOdom=40，不是再走60
		interCurveDist = odomStr.trackAbs;
		searchSt.decdistance = 40;
		MOTION_Stop();
		searchSt.decdistance = 40;
		interCurveDist = odomStr.trackAbs - interCurveDist + 6; // 出弯到下一次入弯之间的直线距离
	}
	else if (wallSns.front == 1) // cjm
	{
		motionCtrl.tick = 0;
		speedSt.turnaroundTick = 0;
		motionCtrl.task = BEFORE180TURN;
		while (motionCtrl.tick < 50)
			;
		interCurveDist = 60 - 5;
	}

	MOTION_SetTrack(0);
	searchSt.lastSearchAct = Straight;
}

/**
 * @brief  180度转弯后加速启动
 */
void MOTION_After180Turn() // 加速启动
{
	//	if(((mousePos.cX==xGoal0)||(mousePos.cX==xGoal1))&&((mousePos.cY==yGoal0)||(mousePos.cY==yGoal0)))
	if ((wallSns.left == 1 && abs(irDist.l - sysCfg.distToWall) < 9) || (wallSns.right == 1 && abs(irDist.r - sysCfg.distToWall) < 9) || searchSt.afterTurnSide == 0) // 误差在正负10mm以内就不用矫正,2015年改成8。
	//	if (1)
	{
		MOTION_Acc();
		MOTION_GoStraight(interCurveDist - 35);
	}
	else if ((mazeState.mousePos.cX == mazeState.goalX) && (mazeState.mousePos.cY == mazeState.goalY))
	{
		if ((mazeState.mousePos.cX + mazeState.mousePos.cY + mazeState.mouseDirection) % 2 == 0)
		{
			MOTION_Turn(POINTR90);
			motionCtrl.tick = 0;
			speedSt.turnaroundTick = 0;
			motionCtrl.task = KEEPWALLDISTANCE;
			while (motionCtrl.tick < 50)
				;
			// MOTION_Turn(POINTL90);
			MOTION_PointTurnLeft();
			MOTION_Acc();
			MOTION_GoStraight(interCurveDist - 35);
		}
		else
		{
			MOTION_Turn(POINTL90);
			motionCtrl.tick = 0;
			speedSt.turnaroundTick = 0;
			motionCtrl.task = KEEPWALLDISTANCE;
			while (motionCtrl.tick < 50)
				;
			// MOTION_Turn(POINTR90);
			MOTION_PointTurnRight();
			MOTION_Acc();
			MOTION_GoStraight(interCurveDist - 35);
		}
	}
	else if (searchSt.afterTurnSide == 2)
	{
		MOTION_Turn(POINTL90);
		motionCtrl.tick = 0;
		speedSt.turnaroundTick = 0;
		motionCtrl.task = KEEPWALLDISTANCE;
		while (motionCtrl.tick < 50)
			;
		// MOTION_Turn(POINTR90);
		MOTION_PointTurnRight();
		MOTION_Acc();
		MOTION_GoStraight(interCurveDist - 35);
	}
	else if (searchSt.afterTurnSide == 1)
	{
		MOTION_Turn(POINTR90);
		motionCtrl.tick = 0;
		speedSt.turnaroundTick = 0;
		motionCtrl.task = KEEPWALLDISTANCE;
		while (motionCtrl.tick < 50)
			;
		// MOTION_Turn(POINTL90);
		MOTION_PointTurnLeft();
		MOTION_Acc();
		MOTION_GoStraight(interCurveDist - 35);
	}
	else // 两边无墙
	{
		MOTION_Acc();
		MOTION_GoStraight(interCurveDist - 35);
	}
	searchSt.afterTurnSide = 0;
}
