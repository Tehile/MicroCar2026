/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: spurt.c —— 冲刺动作生成与执行(45/90度冲刺与回坐标)
*********************************************************************************************************
*/
/* 本文件在算法中位置: 执行层--冲刺速度档/动作序列生成, 向运动控制层输出目标动作。 */

#include "spurt.h"
/* actionsequence/actionCorrSeq 仅本模块使用, 为文件级 static */
static u8 actionsequence[200] = {0};
static u8 actionCorrSeq[200] = {0};

/**
 * @brief 设置冲刺PID增益(按速度档位)
 * @param mode 速度档位(SPURT_SPEED_45_LOW/MEDIUM/HIGH)
 * @note 同步生成并执行对应档位的冲刺动作序列
 */
void SPURT_GainSet(u8 mode)
{
	if (mode == SPURT_SPEED_45_LOW)
	{
		spurtCfg.irKp1 = 0.25;
		spurtCfg.irKp2 = 0.15;
		spurtCfg.irKd = 0;
		spurtCfg.gyroKp = 0.3;
		spurtCfg.gyroKd = 0;
		pidGain.straight45P = 0.07;
		pidGain.straight45D = 0.03;
		pidGain.turnP = 8;
		pidGain.turnD = 5;
	}
	else if (mode == SPURT_SPEED_MEDIUM)
	{
		spurtCfg.irKp1 = 0.25;
		spurtCfg.irKp2 = 0.15;
		spurtCfg.irKd = 0;
		spurtCfg.gyroKp = 0.3;
		spurtCfg.gyroKd = 0;
		pidGain.straight45P = 0.1;
		pidGain.straight45D = 0.1;
		pidGain.turnP = 8;
		pidGain.turnD = 5;
	}
	else if (mode == SPURT_SPEED_HIGH)
	{
		spurtCfg.irKp1 = 0.3;
		spurtCfg.irKp2 = 0.15;
		spurtCfg.irKd = 0;
		spurtCfg.gyroKp = 0.3;
		spurtCfg.gyroKd = 0;
		pidGain.straight45P = 0.1;
		pidGain.straight45D = 0.1;
		pidGain.turnP = 10;
		pidGain.turnD = 7;
	}

	SPURT_ActionGenerate(mode);
	SPURT_Action(mode);
}

/**
 * @brief 设置90度冲刺PID增益
 * @param mode 速度档位(SPURT_SPEED_90_LOW)
 * @note 同时设定转弯延时/冲刺速度并生成执行90度冲刺动作序列
 */
void SPURT_GainSet90(u8 mode)
{
	spurtCfg.irKp1 = 0.2; // high
	spurtCfg.irKp2 = 0.18;
	spurtCfg.irKd = 0.20;
	spurtCfg.gyroKp = 0.25;
	spurtCfg.gyroKd = 0.08;
	pidGain.turnP = 4; // 目前看是转弯还需要加快
	pidGain.turnD = 4; // D不知道怎么调

	turnDelay = 14; // 转弯快一些

	/* 电机PID增益由ISR每周期设为常量(MOTOR_KP_L 等), 此处无需再设 */

	speedCfg.turn = 80;
	speedCfg.fastSprint = 80;

	SPURT_ActionGenerate90(mode);
	SPURT_Action90(mode);
}

SpurtSpeedConfig spurtSpd; /* 初值由 SPURT_Init 逐字段赋值 */

/**
 * @brief 冲刺速度/比例配置逐字段赋值(结构定义见 spurt.h SpurtSpeedConfig)
 * @param none
 */
void SPURT_Init(void)
{
	spurtSpd.l45TopSpeed = 160;
	spurtSpd.l45BottomSpeed = 100;
	spurtSpd.objGoToTopSpd = 70;
	spurtSpd.objGoToBtmSpd = 70;
	spurtSpd.lTopSpeed = 70;
	spurtSpd.lBottomSpeed = 70;
	spurtSpd.l90TopSpeed = 150;
	spurtSpd.l90BottomSpeed = 120;
	spurtSpd.backTopSpeed = 70;
	spurtSpd.backBottomSpeed = 70;
	spurtSpd.topSpeed = 0;
	spurtSpd.bottomSpeed = 0;
	spurtSpd.spurtAccRatio45 = 0.6f;
	spurtSpd.spurtDecRatio45 = 0.5f;
	spurtSpd.spurtAccRatio90 = 0.8f;
	spurtSpd.spurtDecRatio90 = 0.5f;
	spurtSpd.spurtAccRatio = 0;
	spurtSpd.spurtDecRatio = 0;
	spurtSpd.initialSpeed = 0;
}

/**
 * @brief 冲刺入口: 对终点四个坐标制作等高图, 选离起点最近的可达终点
 * @param none
 * @note 选定目标坐标后由 SPURT_ObjectGoTo 逐格走格子到达
 */
void SPURT_MouseSpurt(void)
{
	uchar ucTemp = 0xff;
	char cXdst = 0, cYdst = 0;
	/*
	 *  对终点的四个坐标分别制作等高图
	 *  取离起点最近的一个点作为目标点
	 */
	if (mazeState.mapWall[mazeState.xGoal0][mazeState.yGoal0] & (WALL_LEFT_OPEN | WALL_DOWN_OPEN))
	{													   /*  判断该终点坐标是否有出口    */
		FLOODFILL_Close(mazeState.xGoal0, mazeState.yGoal0); /*  制作等高图                  */
		if (ucTemp > mazeState.mapStepClose[mazeState.xStart][mazeState.yStart])
		{ /*  保存离起点最近的坐标        */
			cXdst = mazeState.xGoal0;
			cYdst = mazeState.yGoal0;
			ucTemp = mazeState.mapStepClose[mazeState.xStart][mazeState.yStart];
		}
	}
	if (mazeState.mapWall[mazeState.xGoal0][mazeState.yGoal1] & (WALL_LEFT_OPEN | WALL_UP_OPEN))
	{													   /*  判断该终点坐标是否有出口    */
		FLOODFILL_Close(mazeState.xGoal0, mazeState.yGoal1); /*  制作等高图                  */
		if (ucTemp > mazeState.mapStepClose[mazeState.xStart][mazeState.yStart])
		{ /*  保存离起点最近的坐标        */
			cXdst = mazeState.xGoal0;
			cYdst = mazeState.yGoal1;
			ucTemp = mazeState.mapStepClose[mazeState.xStart][mazeState.yStart];
		}
	}
	if (mazeState.mapWall[mazeState.xGoal1][mazeState.yGoal0] & (WALL_RIGHT_OPEN | WALL_DOWN_OPEN))
	{													   /*  判断该终点坐标是否有出口    */
		FLOODFILL_Close(mazeState.xGoal1, mazeState.yGoal0); /*  制作等高图                  */
		if (ucTemp > mazeState.mapStepClose[mazeState.xStart][mazeState.yStart])
		{ /*  保存离起点最近的坐标        */
			cXdst = mazeState.xGoal1;
			cYdst = mazeState.yGoal0;
			ucTemp = mazeState.mapStepClose[mazeState.xStart][mazeState.yStart];
		}
	}
	if (mazeState.mapWall[mazeState.xGoal1][mazeState.yGoal1] & (WALL_UP_OPEN | WALL_RIGHT_OPEN))
	{													   /*  判断该终点坐标是否有出口    */
		FLOODFILL_Close(mazeState.xGoal1, mazeState.yGoal1); /*  制作等高图                  */
		if (ucTemp > mazeState.mapStepClose[mazeState.xStart][mazeState.yStart])
		{ /*  保存离起点最近的坐标        */
			cXdst = mazeState.xGoal1;
			cYdst = mazeState.yGoal1;
			ucTemp = mazeState.mapStepClose[mazeState.xStart][mazeState.yStart];
		}
	}
	SPURT_ObjectGoTo(cXdst, cYdst); /*  运行到指定目标点            */
}
/**
 * @brief 使电脑鼠运动到指定坐标(按等高图逐格前进, 优先直行, 必要时转向)
 * @param cXdst 目的地的横坐标
 * @param cYdst 目的地的纵坐标
 */
void SPURT_ObjectGoTo(char cXdst, char cYdst)
{
	uchar ucStep = 1;
	char cNBlock = 0, cDirTemp;
	char cX, cY;
	LED_On(4);
	LED_On(5);
	mazeState.objectGoToFlag = 1;
	cX = mazeState.mousePos.cX;
	cY = mazeState.mousePos.cY;
	FLOODFILL_Close(cXdst, cYdst); /*  制作等高图                  */
	/*
	 *  根据等高值向目标点运动，直到达到目的地
	 */
	while ((cX != cXdst) || (cY != cYdst))
	{
		ucStep = mazeState.mapStepClose[cX][cY];
		/*
		 *  任选一个等高值比当前自身等高值小的方向前进
		 */
		if ((mazeState.mapWall[cX][cY] & WALL_UP_OPEN) && /*  上方有路                    */
			(mazeState.mapStepClose[cX][cY + 1] < ucStep))
		{				   /*  上方等高值较小              */
			cDirTemp = UP; /*  记录方向                    */
			if (cDirTemp == mazeState.mouseDirection)
			{			   /*  优先选择不需要转弯的方向    */
				cNBlock++; /*  前进一个方格                */
				cY++;
				continue; /*  跳过本次循环                */
			}
		}
		if ((mazeState.mapWall[cX][cY] & WALL_RIGHT_OPEN) && /*  右方有路                    */
			(mazeState.mapStepClose[cX + 1][cY] < ucStep))
		{					  /*  右方等高值较小              */
			cDirTemp = RIGHT; /*  记录方向                    */
			if (cDirTemp == mazeState.mouseDirection)
			{			   /*  优先选择不需要转弯的方向    */
				cNBlock++; /*  前进一个方格                */
				cX++;
				continue; /*  跳过本次循环                */
			}
		}
		if ((mazeState.mapWall[cX][cY] & WALL_DOWN_OPEN) && /*  下方有路                    */
			(mazeState.mapStepClose[cX][cY - 1] < ucStep))
		{					 /*  下方等高值较小              */
			cDirTemp = DOWN; /*  记录方向                    */
			if (cDirTemp == mazeState.mouseDirection)
			{			   /*  优先选择不需要转弯的方向    */
				cNBlock++; /*  前进一个方格                */
				cY--;
				continue; /*  跳过本次循环                */
			}
		}
		if ((mazeState.mapWall[cX][cY] & WALL_LEFT_OPEN) && /*  左方有路                    */
			(mazeState.mapStepClose[cX - 1][cY] < ucStep))
		{					 /*  左方等高值较小              */
			cDirTemp = LEFT; /*  记录方向                    */
			if (cDirTemp == mazeState.mouseDirection)
			{			   /*  优先选择不需要转弯的方向    */
				cNBlock++; /*  前进一个方格                */
				cX--;
				continue; /*  跳过本次循环                */
			}
		}
		cDirTemp = (cDirTemp + 4 - mazeState.mouseDirection) % 4; /*  计算方向偏移量              */

		if (cNBlock)
		{
			MOTION_MouseGoahead(cNBlock);
		}
		cNBlock = 0; /*  任务清零                    */

		/*
		 *  控制电脑鼠转弯
		 */
		switch (cDirTemp)
		{

		case 1:
			MOTION_MouseTurnRight();
			break;

		case 2:
			MOTION_MouseTurnBack();
			break;

		case 3:
			MOTION_MouseTurnLeft();
			break;

		default:
			break;
		}

		cX = mazeState.mousePos.cX;
		cY = mazeState.mousePos.cY;
	}
	/*
	 *  判断任务是否完成，否则继续前进
	 */

	if (cNBlock)
	{
		MOTION_MouseGoahead(cNBlock);
	}
	mazeState.objectGoToFlag = 0;
}

// fastwayAction 转弯序列动作码: 0=直走, 1=右转45度, 2=右转90度, 7=左转45度, 6=左转90度

/**
 * @brief 求动作码累加和(方向判别)
 * @param num 取动作序列前 num 个动作求和
 * @return 累加和(用于判别正/斜方向)
 */
int SPURT_GetSum(int num)
{
	int ret = 0, i = 0;
	for (i = 0; i <= num; i++)
		ret += mazeState.fastwayAction[i].action;
	return ret;
}

/**
 * @brief 求动作序列执行后的最终方向
 * @return 方向值(累加后对8取模, 0-7)
 */
int SPURT_GetFinalDir()
{
	int ret = 0, i = 0;
	for (i = 0; i < mazeState.fastwayCount; i++)
	{
		if (mazeState.fastwayAction[i].action == 7)
			ret += -1;
		else if (mazeState.fastwayAction[i].action == 6)
			ret += -2;
		else
			ret += mazeState.fastwayAction[i].action;
	}
	return ret % 8;
}

/**
 * @brief 冲刺动作序列生成: 由左/右转命令序列转化为运动动作序列
 * @param mode 速度档位(SPURT_SPEED_45_LOW/MEDIUM/HIGH)
 * @note 由 SPURT_GetSum(i)%2 判别正/斜方向, 合并连续转向为单动作码。
 *       L/M/H 三档判断逻辑相同, 仅动作码档位偏移不同(L档1-16, M档=+20, H档=+40),
 *       合并为单循环, 动作码统一写为「L档基准码 + speedBase」。
 */
void SPURT_ActionGenerate(u8 mode)
{
	int current = 0;
	u16 i = 0, j = 0;
	u8 speedBase; /* 速度档基址偏移: 0=L档 / 20=M档 / 40=H档, 动作码 enum 等差步长为 20 */
	for (i = 0; i < 200; i++)
	{
		actionsequence[i] = 0;
		actionCorrSeq[i] = 0;
	}

	if (mode == SPURT_SPEED_45_LOW)
		speedBase = 0; /* L档 1-16  */
	else if (mode == SPURT_SPEED_MEDIUM)
		speedBase = 20; /* M档 21-36 (L+20) */
	else if (mode == SPURT_SPEED_HIGH)
		speedBase = 40; /* H档 41-56 (L+40) */
	else
		return; /* 其余档位(含90_LOW=60): 原三块均不命中, 等价空操作 */

	for (i = 0; i < mazeState.fastwayCount - 1; i++)
	{
		if (i < current)
			continue;

		if (mazeState.fastwayAction[i + 1].action == 1 && mazeState.fastwayAction[i + 2].action == 2 && mazeState.fastwayAction[i + 3].action == 1 && SPURT_GetSum(i) % 2 == 0)
		{
			actionsequence[j++] = R180L + speedBase;
			current += 3;
		}
		else if (mazeState.fastwayAction[i + 1].action == 7 && mazeState.fastwayAction[i + 2].action == 6 && mazeState.fastwayAction[i + 3].action == 7 && SPURT_GetSum(i) % 2 == 0)
		{
			actionsequence[j++] = L180L + speedBase;
			current += 3;
		}
		else if (mazeState.fastwayAction[i + 1].action == 2 && mazeState.fastwayAction[i + 2].action == 1 && SPURT_GetSum(i) % 2 == 1)
		{
			actionsequence[j++] = RV135L + speedBase;
			current += 2;
		}
		else if (mazeState.fastwayAction[i + 1].action == 6 && mazeState.fastwayAction[i + 2].action == 7 && SPURT_GetSum(i) % 2 == 1)
		{
			actionsequence[j++] = LV135L + speedBase;
			current += 2;
		}
		else if (mazeState.fastwayAction[i + 1].action == 1 && mazeState.fastwayAction[i + 2].action == 2 && SPURT_GetSum(i) % 2 == 0)
		{
			actionsequence[j++] = RT135L + speedBase;
			current += 3;
		}
		else if (mazeState.fastwayAction[i + 1].action == 7 && mazeState.fastwayAction[i + 2].action == 6 && SPURT_GetSum(i) % 2 == 0)
		{
			actionsequence[j++] = LT135L + speedBase;
			current += 3;
		}
		else if (mazeState.fastwayAction[i + 1].action == 7 && mazeState.fastwayAction[i + 2].action == 7 && SPURT_GetSum(i) % 2 == 0)
		{
			actionsequence[j++] = LT90L + speedBase;
			current += 2;
		}
		else if (mazeState.fastwayAction[i + 1].action == 1 && mazeState.fastwayAction[i + 2].action == 1 && SPURT_GetSum(i) % 2 == 0)
		{
			actionsequence[j++] = RT90L + speedBase;
			current += 2;
		}
		else if (mazeState.fastwayAction[i + 1].action == 2 && SPURT_GetSum(i) % 2 == 1)
		{
			actionsequence[j++] = RV90L + speedBase;
			current += 2;
		}
		else if (mazeState.fastwayAction[i + 1].action == 6 && SPURT_GetSum(i) % 2 == 1)
		{
			actionsequence[j++] = LV90L + speedBase;
			current += 2;
		}
		else if (mazeState.fastwayAction[i + 1].action == 1 && SPURT_GetSum(i) % 2 == 0)
		{
			actionsequence[j++] = RT45L + speedBase;
			current += 2;
		}
		else if (mazeState.fastwayAction[i + 1].action == 7 && SPURT_GetSum(i) % 2 == 0)
		{
			actionsequence[j++] = LT45L + speedBase;
			current += 2;
		}
		else if (mazeState.fastwayAction[i + 1].action == 1 && SPURT_GetSum(i) % 2 == 1)
		{
			actionsequence[j++] = RV45L + speedBase;
			current += 1;
		}
		else if (mazeState.fastwayAction[i + 1].action == 7 && SPURT_GetSum(i) % 2 == 1)
		{
			actionsequence[j++] = LV45L + speedBase;
			current += 1;
		}
		else if (mazeState.fastwayAction[i + 1].action == 0 && SPURT_GetSum(i) % 2 == 0)
		{
			// 斜线冲刺: 直行90度
			actionsequence[j++] = STRAIGHT90L + speedBase;
			current += 1;
			// 直线校正(长直线冲刺时依两侧墙保持车身水平): 与斜向冲刺冲突, 已停用
		}
		else if (mazeState.fastwayAction[i + 1].action == 0 && SPURT_GetSum(i) % 2 == 1)
		{
			actionsequence[j++] = STRAIGHT45L + speedBase;
			current += 1;
		}
	}
}

/**
 * @brief 执行冲刺动作序列(按档位依次执行直行/转弯动作)
 * @param mode 速度档位(SPURT_SPEED_45_LOW/MEDIUM/HIGH)
 * @note 连续相同直行动作合并为一段执行; 执行完按档位设定减速距离
 */
void SPURT_Action(u8 mode)
{
	u16 i = 0, num = 0;

	searchSt.speedmode = (SpurtSpeed_t)mode;

	if (mode == SPURT_SPEED_45_LOW)
	{
#ifndef LOW_ENABLE
		while (1)
		{
			LED_Off(0);
			LED_On(1);
			LED_On(8);
			DELAY_Ms(500);
			LED_Off(0);
			LED_On(3);
			LED_On(6);
			DELAY_Ms(500);
		}
#endif
		while (actionsequence[i] != 0)
		{
			if (actionsequence[i] == STRAIGHT45L)
			{
				num = 0;
				while (actionsequence[i] == STRAIGHT45L)
				{
					num++;
					i++;
					searchSt.spurtActionNum++;
				}
				MOTION_Straight45(DIAGONAL_HALF_CELL_STEPS * num);
			}
			else if (actionsequence[i] == STRAIGHT90L)
			{
				num = 0;
				while (actionsequence[i] == STRAIGHT90L)
				{
					num++;
					i++;
					searchSt.spurtActionNum++;
				}
				MOTION_Straight90(180 * num);
			}
			else
			{
				searchSt.spurt45EndFlag = 0;
				MOTION_MouseTurn(actionsequence[i]);
				i++;
				searchSt.spurtActionNum++;
			}
		}
		LED_Off(0);
		LED_On(1);
		LED_On(2);
		LED_On(3);
		LED_On(4);
	}
	if (mode == SPURT_SPEED_MEDIUM)
	{
#ifndef MEDIUM_ENABLE
		while (1)
		{
			LED_Off(0);
			LED_On(1);
			LED_On(8);
			DELAY_Ms(500);
			LED_Off(0);
			LED_On(3);
			LED_On(6);
			DELAY_Ms(500);
		}
#endif
		while (actionsequence[i] != 0)
		{
			if (actionsequence[i] == STRAIGHT45M)
			{
				num = 0;
				while (actionsequence[i] == STRAIGHT45M)
				{
					num++;
					i++;
				}
				MOTION_Straight45(DIAGONAL_HALF_CELL_STEPS * num); // 需要改成中速的直线
			}
			else if (actionsequence[i] == STRAIGHT90M)
			{
				num = 0;
				while (actionsequence[i] == STRAIGHT90M)
				{
					num++;
					i++;
				}
				MOTION_Straight90(180 * num); // 需要改成中速的直线
			}
			else
			{
				MOTION_MouseTurn(actionsequence[i]);
				i++;
			}
		}
		searchSt.decdistance = 120;
		MOTION_Stop();
	}
	if (mode == SPURT_SPEED_HIGH)
	{
#ifndef HIGH_ENABLE
		while (1)
		{
			LED_Off(0);
			LED_On(1);
			LED_On(8);
			DELAY_Ms(500);
			LED_Off(0);
			LED_On(3);
			LED_On(6);
			DELAY_Ms(500);
		}
#endif
		while (actionsequence[i] != 0)
		{
			if (actionsequence[i] == STRAIGHT45H)
			{
				num = 0;
				while (actionsequence[i] == STRAIGHT45H)
				{
					num++;
					i++;
				}
				MOTION_Straight45(DIAGONAL_HALF_CELL_STEPS * num); // 需要改成高速的直线
			}
			else if (actionsequence[i] == STRAIGHT90H)
			{
				num = 0;
				while (actionsequence[i] == STRAIGHT90H)
				{
					num++;
					i++;
				}
				MOTION_Straight90(180 * num); // 需要改成高速的直线
			}
			else
			{
				MOTION_MouseTurn(actionsequence[i]);
				i++;
			}
		}
		searchSt.decdistance = 120;
		MOTION_Stop();
	}
}

// 垂直冲刺(90度)

/**
 * @brief 90度冲刺动作序列生成(由左/右转命令序列转化为动作序列)
 * @param mode 速度档位(SPURT_SPEED_90_LOW)
 * @note 生成动作序列的同时计算直线校正侧(BOTHSIDE/LEFTSIDE/RIGHTSIDE/NOSIDE)
 */
void SPURT_ActionGenerate90(u8 mode)
{
	int current = 0;
	u16 i = 0, j = 0;
	uchar k = 0, dirtemp = 0, leftdir = 0, rightdir = 0;
	for (i = 0; i < 200; i++)
	{
		actionsequence[i] = 0;
		actionCorrSeq[i] = 0;
	}
	if (mode == SPURT_SPEED_90_LOW)
	{
		for (i = 0; i < mazeState.fastwayCount90 - 1; i++)
		{
			if (i < current)
				continue;
			if (mazeState.fastwayAction90[i + 1].action == 1)
			{
				actionsequence[j++] = RT90SEARCH;
				current += 1;
			}
			if (mazeState.fastwayAction90[i + 1].action == 3)
			{
				actionsequence[j++] = LT90SEARCH;
				current += 1;
			}
			if (mazeState.fastwayAction90[i + 1].action == 0)
			{
				actionsequence[j] = STRAIGHT_spurt_90;
				dirtemp = mazeState.mapFastway[mazeState.fastwayAction[i + 1].cx][mazeState.fastwayAction[i + 1].cy];
				leftdir = (dirtemp + 3) % 4;
				rightdir = (dirtemp + 1) % 4;

				if ((mazeState.mapWall[mazeState.fastwayAction[i + 1].cx][mazeState.fastwayAction[i + 1].cy] & (1 << leftdir)) && (mazeState.mapWall[mazeState.fastwayAction[i + 1].cx][mazeState.fastwayAction[i + 1].cy] & (1 << rightdir)))
				{
					actionCorrSeq[j++] = BOTHSIDE;
					if (actionCorrSeq[j - 2] == LEFTSIDE || actionCorrSeq[j - 1] == RIGHTSIDE)
						actionCorrSeq[j - 1] = actionCorrSeq[j - 2];
				}
				else if (mazeState.mapWall[mazeState.fastwayAction[i + 1].cx][mazeState.fastwayAction[i + 1].cy] & (1 << leftdir))
				{
					actionCorrSeq[j++] = LEFTSIDE;
					for (k = j - 2; actionCorrSeq[k] == BOTHSIDE; k--)
						actionCorrSeq[k] = LEFTSIDE;
				}
				else if (mazeState.mapWall[mazeState.fastwayAction[i + 1].cx][mazeState.fastwayAction[i + 1].cy] & (1 << rightdir))
				{
					actionCorrSeq[j++] = RIGHTSIDE;
					for (k = j - 2; actionCorrSeq[k] == BOTHSIDE; k--)
						actionCorrSeq[k] = RIGHTSIDE;
				}
				else
					actionCorrSeq[j++] = NOSIDE;
				current += 1;
			}
		}
	}
}

/**
 * @brief 执行90度冲刺动作序列(含搜索转向/直线校正/回程逻辑)
 * @param mode 速度档位(SPURT_SPEED_90_LOW)
 * @note 转弯前按 RT90SEARCH/LT90SEARCH 触发搜索转向; 结束后按回程状态设减速距离
 */
void SPURT_Action90(u8 mode)
{
	u16 i = 0, num = 0;

	searchSt.speedmode = (SpurtSpeed_t)mode;

	if (mode == SPURT_SPEED_90_LOW)
	{
#ifndef LOW_90_ENABLE
		while (1)
		{
			LED_Off(0);
			LED_On(1);
			LED_On(8);
			DELAY_Ms(500);
			LED_Off(0);
			LED_On(3);
			LED_On(6);
			DELAY_Ms(500);
		}
#endif
		if ((actionsequence[0] == RT90SEARCH) || actionsequence[0] == LT90SEARCH)
		{
			if (actionsequence[i] == RT90SEARCH)
			{
				motionCtrl.task = SMARTSTRAIGHT;
				while (1)
					if (wallSns.rightPrev == 1 && wallSns.right == 0)
						break;
				MOTION_SetTrack(0);
				while (1)
					if (odomStr.cellOdom >= 36)
						break;
			}
			if (actionsequence[i] == LT90SEARCH)
			{
				motionCtrl.task = SMARTSTRAIGHT;
				while (1)
					if (wallSns.leftPrev == 1 && wallSns.left == 0)
						break;
				MOTION_SetTrack(0);
				while (1)
					if (odomStr.cellOdom >= 36)
						break;
			}
		}
		while (actionsequence[i] != 0)
		{
			if (actionsequence[i] == STRAIGHT_spurt_90)
			{
				num = 0;
				while (actionsequence[i] == STRAIGHT_spurt_90)
				{
					num++;
					i++;
				}
				if (taskSt.goOrBack == 1 && searchSt.back90Flag == 1 && actionsequence[0] == STRAIGHT_spurt_90)
				{
					if (mazeState.mouseTask == SPURT_45)
					{
						if (num == 1)
						{
							MOTION_Straight90(80 * num + 80);
							MOTION_SetTrack(0);
						}
						else
							MOTION_Straight90(180 * num - 100 + 80);
					}
					else if (mazeState.mouseTask == SPURT_90)
					{
						if (num == 1)
						{
							MOTION_Straight90(80 * num + 80);
							MOTION_SetTrack(0);
						}
						else
							MOTION_Straight90(180 * num - 100 + 80);
					}
					searchSt.back90Flag = 0;
				}
				else if (num == 1)
				{
					MOTION_Straight90(80 * num);
					MOTION_SetTrack(0);
				}
				else
					MOTION_Straight90(180 * num - 100); // 小于180 65=175-110
				if (actionsequence[i] == RT90SEARCH)
				{
					motionCtrl.task = SMARTSTRAIGHT;
					while (1)
						if (wallSns.rightPrev == 1 && wallSns.right == 0)
							break;
					MOTION_SetTrack(0);
					while (1)
						if (odomStr.cellOdom >= 36)
							break;
				}
				if (actionsequence[i] == LT90SEARCH)
				{
					motionCtrl.task = SMARTSTRAIGHT;
					while (1)
						if (wallSns.leftPrev == 1 && wallSns.left == 0)
							break;
					MOTION_SetTrack(0);
					while (1)
						if (odomStr.cellOdom >= 36)
							break;
				}
			}
			else
			{
				searchSt.spurt45EndFlag = 0;
				if (actionsequence[i] == RT90SEARCH)
					SPURT_TurnRight();
				if (actionsequence[i] == LT90SEARCH)
					SPURT_TurnLeft();
				i++;
			}
		}
		if (taskSt.goOrBack == 1)
			searchSt.decdistance = 30;
		else if (taskSt.backSearch == 0)
			searchSt.decdistance = 30;
		else if (taskSt.backSearch == 1)
			searchSt.decdistance = 30 + 150;
		else
			searchSt.decdistance = 30;
		MOTION_Stop();

		if (taskSt.backSearch == 1)
		{
			motionCtrl.tick = 0;
			speedSt.turnaroundTick = 0;
			motionCtrl.task = KEEPWALLDISTANCE;
			while (motionCtrl.tick < 50)
				;
		}
	}
}

/**
 * @brief 求90度动作序列执行后的最终方向
 * @return 方向值(累加后对4取模, 0-3)
 */
int SPURT_GetFinalDir90()
{
	int ret = 0, i = 0;
	for (i = 0; i < mazeState.fastwayCount90; i++)
	{
		if (mazeState.fastwayAction90[i].action == 3)
			ret += -1;
		else
			ret += mazeState.fastwayAction90[i].action;
	}
	return ret % 4;
}

// 垂直冲刺动作(左右转90度/原地回转)
/**
 * @brief 右转90度冲刺(转弯前依左墙距计算入弯偏移)
 * @param none
 */
void SPURT_TurnRight()
{
	searchSt.skipCoorUpdate = 0;
	SPURT_BeforeTurnR();
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
	MOTION_Turn(RT90SPURT); // 对应拐弯前的cellOdom也要改为10*2.
	motionCtrl.task = SMARTSTRAIGHT;
	mazeState.mouseDirection = (mazeState.mouseDirection + 1) % 4;
	MOTION_SetTrack(180 - searchSt.turnRightOffset);
	motionCtrl.task = SMARTSTRAIGHT;
	while (odomStr.cellOdom <= 180)
		;
	MOTION_SetTrack(0);
}

/**
 * @brief 左转90度冲刺(转弯前依右墙距计算入弯偏移)
 * @param none
 */
void SPURT_TurnLeft()
{
	searchSt.skipCoorUpdate = 0;
	SPURT_BeforeTurnL();
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
	MOTION_Turn(LT90SPURT); // 对应拐弯前的cellOdom也要改为10*2.
	motionCtrl.task = SMARTSTRAIGHT;
	mazeState.mouseDirection = (mazeState.mouseDirection + 3) % 4;
	MOTION_SetTrack(180 + searchSt.turnLeftOffset);
	motionCtrl.task = SMARTSTRAIGHT;
	while (odomStr.cellOdom <= 180)
		;
	MOTION_SetTrack(0);
}

/**
 * @brief 转弯前减速: 依前方传感器调整姿态, 判断转弯开始点并获取矫正距离
 * @param none
 */
void SPURT_BeforeTurnL()
{

	{
		motionCtrl.pidErr = 0;
		motionCtrl.pidErrPrev = 0;

		MOTION_SetTrack(0);

		motionCtrl.task = BEFORE90TURN; // 根据前方是否有墙判断矫正方式，两种应当可以随意切换，因为前方墙在远距离时容易误判。
		while (1)
		{
				// 出弯到下一次入弯之间的直线距离
			if (odomStr.cellOdom >= 20)
			{
				searchSt.turnOffset = 0;
				break;
			}
			// 出弯到下一次入弯之间的直线距离(58-22=36, 44+36=80)
		}
	}
}

/**
 * @brief 转弯前减速: 依前方传感器调整姿态, 判断转弯开始点并获取矫正距离
 * @param none
 */
void SPURT_BeforeTurnR()
{

	{
		motionCtrl.pidErr = 0;
		motionCtrl.pidErrPrev = 0;

		MOTION_SetTrack(0);

		motionCtrl.task = BEFORE90TURN; // 根据前方是否有墙判断矫正方式，两种应当可以随意切换，因为前方墙在远距离时容易误判。
		while (1)
		{
			if (odomStr.cellOdom >= 20)
			{
				searchSt.turnOffset = 0;
				break;
			} // 出弯到下一次入弯之间的直线距离
		}
	}
}

/**
 * @brief 原地回转180度(冲刺结束返回起点用)
 * @param none
 */
void SPURT_BackTurn()
{
	searchSt.skipCoorUpdate = 0;
	MOTION_SetTrack(0);

	motionCtrl.task = SMARTSTRAIGHT;
	motionCtrl.tick = 0;
	speedSt.turnaroundTick = 0;
	motionCtrl.task = BEFORE180TURN;
	while (motionCtrl.tick < 50)
		;
	MOTION_SetTrack(0);

	MOTION_TurnAround();
	mazeState.mouseDirection = (mazeState.mouseDirection + 2) % 4;
	MOTION_SetTrack(0);
}
