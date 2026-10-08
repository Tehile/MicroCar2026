/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: state.c —— 任务状态机
*********************************************************************************************************
*/

/* 搜索状态机跨周期持久状态 */
#include "state.h"
static int lTopSpeedH, L45TopSpeedh, BackTopSpeedh; /* 返回起点速度暂存 */
static uchar spurtmode;								/* 冲刺速度档, 跨准备段/主任务状态机共享 */
/* 搜索状态机跨周期状态封装(static, 仅本模块可见) */
typedef struct
{
	uchar roadCount;	/* 可前进支路数(EXPLORE_CrosswayCheck 返回值) */
	uchar crossIdx;		/* 支点栈索引 */
	uchar shortWayX;	/* 最短路径支点 X */
	uchar shortWayY;	/* 最短路径支点 Y */
	uchar stepMax;		/* 当前步数阈值 */
	uchar stepMaxLimit; /* 步数阈值上限 */
	char tempWayX;		/* 临时支点坐标 X */
	char tempWayY;		/* 临时支点坐标 Y */
	uchar goalGet;		/* 是否已到达过终点标志, 跨MAZESEARCH各周期持久 */
} SearchStateMachine;
static SearchStateMachine searchSm; /* 初值由 STATE_InitSearchSm 逐字段赋值 */

/**
 * @brief  搜索状态机逐字段赋值
 * @param  none
 * @note   初值: stepMax/stepMaxLimit=12, 其余字段为 0
 */
void STATE_InitSearchSm(void)
{
	searchSm.roadCount = 0;
	searchSm.crossIdx = 0;
	searchSm.shortWayX = 0;
	searchSm.shortWayY = 0;
	searchSm.stepMax = 12;
	searchSm.stepMaxLimit = 12;
	searchSm.tempWayX = 0;
	searchSm.tempWayY = 0;
	searchSm.goalGet = 0;
}

/**
 * @brief  冲刺方向判定: 根据前向红外传感器决定冲刺前行格数
 * @param  none
 * @note   非 45/90 冲刺任务时等待前向红外遮挡: f2 遮挡判为 2 格, f1 遮挡判为 1 格,
 *         判定后 LED 提示
 */
void STATE_DetermineGoaheadNum(void)
{
	if (mazeState.mouseTask != SPURT_45 && mazeState.mouseTask != SPURT_90)
	{
		while (1)
		{
			if (irDist.f2 < 20)
			{
				taskSt.goaheadnum = 2;
				break;
			}
			if (irDist.f1 < 20)
			{
				taskSt.goaheadnum = 1;
				break;
			}
		}
		LED_On(6);
		DELAY_Ms(1000);
		LED_Off(0);
	}
}

/**
 * @brief  读取Flash中保存的迷宫地图与起终点, 并按前方红外选择冲刺速度档
 * @param  none
 * @note   仅 Flash 存在有效地图(flashaddrmax!=65535)时执行: 载入墙图/起终点,
 *         按前向红外遮挡选择冲刺方向(SPURT_45/SPURT_90)与速度档, 设置 taskSt.goaheadnum
 */
void STATE_LoadMapAndSelectSpeed(void)
{
	u16 flashaddrmax;
	u8 blockx, blocky; /* 迷宫行列循环变量 */
	flashaddr = 0;
	flashaddrmax = FLASH_ReadHalfWord(FLASH_ADDR_BLOCK);
	if (flashaddrmax != 65535)
	{
		flashaddr = 0;
		for (blockx = 0; blockx < 16; blockx++)
		{
			for (blocky = 0; blocky < 16; blocky++)
			{
				mazeState.mapWall[blockx][blocky] = FLASH_ReadHalfWord(FLASH_ADDR_BLOCK + flashaddr);
				flashaddr += 2;
			}
		}

		flashaddr = 0;
		mazeState.xStart = FLASH_ReadHalfWord(FLASH_ADDR_BLOCK_POINT + flashaddr);
		flashaddr += 2;
		mazeState.yStart = FLASH_ReadHalfWord(FLASH_ADDR_BLOCK_POINT + flashaddr);
		flashaddr += 2;
		mazeState.goalX = FLASH_ReadHalfWord(FLASH_ADDR_BLOCK_POINT + flashaddr);
		flashaddr += 2;
		mazeState.goalY = FLASH_ReadHalfWord(FLASH_ADDR_BLOCK_POINT + flashaddr);
		flashaddr += 2;
		searchSt.goalCorrectDir = FLASH_ReadHalfWord(FLASH_ADDR_BLOCK_POINT + flashaddr);
		flashaddr += 2;
		taskSt.spurtFlag = FLASH_ReadHalfWord(FLASH_ADDR_BLOCK_POINT + flashaddr);
		flashaddr += 2;

		mazeState.mousePos.cX = mazeState.xStart;
		mazeState.mousePos.cY = mazeState.yStart;

		DELAY_Ms(100);
		LED_On(1);
		DELAY_Ms(120);
		LED_Off(0);
		DELAY_Ms(120);
		LED_On(1);
		DELAY_Ms(120);
		LED_Off(0);
		DELAY_Ms(120);
		while (1)
		{
			if (irDist.f1 < 20) // 斜线冲刺
			{
				mazeState.mouseTask = SPURT_45;
				spurtmode = SPURT_SPEED_45_LOW;
				LED_On(1);
				LED_On(2);
				DELAY_Ms(1000);
				LED_Off(0);
				DELAY_Ms(500);
				while (1)
				{
					if (irDist.f1 < 20) // 低速
					{
						spurtSpd.lTopSpeed = 200;
						spurtSpd.l45TopSpeed = 160;
						spurtSpd.backTopSpeed = 200;
						LED_On(1);
						LED_On(2);
						LED_On(3);
						LED_On(4);
						DELAY_Ms(1000);
						LED_Off(0);
						break;
					}
					if (irDist.f2 < 20) // 高速
					{
						spurtSpd.lTopSpeed = 240;
						spurtSpd.l45TopSpeed = 180;
						spurtSpd.backTopSpeed = 240;
						LED_On(5);
						LED_On(6);
						LED_On(7);
						LED_On(8);
						DELAY_Ms(1000);
						LED_Off(0);
						break;
					}
				}
				break;
			}
			if (irDist.f2 < 20)
			{
				mazeState.mouseTask = SPURT_90;
				spurtmode = SPURT_SPEED_90_LOW;
				LED_On(7);
				LED_On(8);
				DELAY_Ms(1000);
				LED_Off(0);
				DELAY_Ms(500);
				while (1)
				{
					if (irDist.f1 < 20) // 低速
					{
						spurtSpd.l90TopSpeed = 70;
						spurtSpd.backTopSpeed = 70;
						LED_On(1);
						LED_On(2);
						LED_On(3);
						LED_On(4);
						DELAY_Ms(1000);
						LED_Off(0);
						break;
					}
					if (irDist.f2 < 20) // 高速
					{
						spurtSpd.l90TopSpeed = 240;
						spurtSpd.backTopSpeed = 220;
						LED_On(5);
						LED_On(6);
						LED_On(7);
						LED_On(8);
						DELAY_Ms(1000);
						LED_Off(0);
						break;
					}
				}
				taskSt.goaheadnum = 0;
				break;
			}
		}
	}
}

/**
 * @brief  冲刺调试模式: 执行 spurtTestAction 序列并保存运行数据
 * @param  none
 * @note   由 main() 在 spurtTestFlag==1 时调用, 末尾死循环不返回。
 *         整定常量: 墙检测阈值 140、转向 110、加速/减速距离 205/110、直道距离 180
 */
void STATE_SpurtTest(void)
{
	u16 spurtTestNum1 = 0, spurtTestNum2 = 0, spurtTestNum3 = 0;
	flashSt.flashenable = 1;
	wallSns.wallThresh = 140;
	LED_Off(0);
	LED_On(4);
	LED_On(5);
	speedCfg.turn = 110;
	searchSt.accdistance = 205;
	MOTION_Acc();
	MOTION_SetTrack(180);
	searchSt.speedmode = SPURT_SPEED_45_LOW;
	MOTION_Straight90(180 * 1);
	for (spurtTestNum3 = 0; spurtTestNum3 < 1; spurtTestNum3++)
		for (spurtTestNum1 = 0; spurtTestNum1 < 5;)
		{
			if (mazeState.spurtTestAction[spurtTestNum1] == STRAIGHT45L)
			{
				spurtTestNum2 = 0;
				while (mazeState.spurtTestAction[spurtTestNum1] == STRAIGHT45L)
				{
					spurtTestNum2++;
					spurtTestNum1++;
				}
				MOTION_Straight45(DIAGONAL_HALF_CELL_STEPS * spurtTestNum2);
			}
			else if (mazeState.spurtTestAction[spurtTestNum1] == STRAIGHT90L)
			{
				spurtTestNum2 = 0;
				while (mazeState.spurtTestAction[spurtTestNum1] == STRAIGHT90L)
				{
					spurtTestNum2++;
					spurtTestNum1++;
				}
				MOTION_Straight90(180 * spurtTestNum2);
			}
			else
			{
				searchSt.spurt45EndFlag = 0;
				MOTION_MouseTurn(mazeState.spurtTestAction[spurtTestNum1]);
				spurtTestNum1++;
			}
		}
	searchSt.decdistance = 110;
	MOTION_Stop();
	speedSt.freemode = 1;
	speedSt.sleepmode = 1;
	flashSt.flashenable = 0;
	LED_On(1);
	LED_On(3);
	LED_On(5);
	LED_On(7);
	FLASH_EraseRunData();
	LED_Off(0);
	LED_On(2);
	LED_On(4);
	LED_On(6);
	LED_On(8);
	flashaddr = 0;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_DATA_NUM + flashaddr, flashSt.saveDataTime);
	flashaddr += 2;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_DATA_NUM + flashaddr, flashSt.saveDataNum);
	flashaddr += 2;
	flashaddr = 0;
	for (flashSt.saveDataNum = 0; flashSt.saveDataNum < saveNum; flashSt.saveDataNum++)
	{
		FLASH_SaveData(flashSt.saveDataNum);
		flashaddr += 2;
	}
	LED_Off(0);
	DELAY_Ms(1000);
	LED_On(0);
	while (1)
		speedSt.sleepmode = 1;
}

/**
 * @brief  已到达过终点后的返回途中搜索
 * @param  none
 * @note   由搜索/冲刺状态在 taskSt.backSearch==1 时调用。起点在步数阈值内或已无
 *         未走支路时回起点转 SPURT; 否则按支点栈回溯(右手法则)继续探索, 支路走完回起点。
 */
void STATE_ReturnSearch(void)
{
	// 重算起点为源(含副本)/终点为源双距离图: mapStepClose[起点]=已探明路径(全闭上界), mapStepOpen[起点]=理论最短(全开下界)
	FLOODFILL_Open(mazeState.xStart, mazeState.yStart);
	FLOODFILL_OpenCopy();
	FLOODFILL_Close(mazeState.xStart, mazeState.yStart);
	FLOODFILL_CloseCopy();
	FLOODFILL_Open(mazeState.goalX, mazeState.goalY);
	FLOODFILL_Close(mazeState.goalX, mazeState.goalY);

	searchSm.roadCount = EXPLORE_CrosswayCheck(mazeState.mousePos.cX, mazeState.mousePos.cY); /* 统计可前进的支路数 */

	if (searchSm.roadCount /* 有可前进方向 */
		&& (mazeState.mapStepCloseCopy[searchSm.shortWayX][searchSm.shortWayY] + mazeState.mapStepOpen[searchSm.shortWayX][searchSm.shortWayY] < mazeState.mapStepClose[mazeState.xStart][mazeState.yStart]))
	{
		if (searchSm.roadCount > 1)
		{ /* 有多条可前进方向, 保存坐标 */
			mazeState.crosswayBuf[searchSm.crossIdx].cX = mazeState.mousePos.cX;
			mazeState.crosswayBuf[searchSm.crossIdx].cY = mazeState.mousePos.cY;
			searchSm.crossIdx++;
		}
		// 转冲刺: 起点实际路径≤阈值(stepMax), 或 全闭上界==全开下界(已找到最优解)
		if ((mazeState.mapStepClose[mazeState.xStart][mazeState.yStart] <= searchSm.stepMax) || (mazeState.mapStepClose[mazeState.xStart][mazeState.yStart] - mazeState.mapStepOpen[mazeState.xStart][mazeState.yStart] == 0))
		{
			FLASH_BlockWrite();
			MOTION_MouseTurnBack();
			SPURT_ObjectGoTo(mazeState.xStart, mazeState.yStart);
			MOTION_MouseTurnBackSpecial();
			mazeState.mouseTask = SPURT;
			return;
		}
		EXPLORE_CrosswayChoice(); /* 用右手法则搜索选择前进方向 */
		SEARCH_Search();		  /* 前进一格 */
	}
	else
	{
		// 转冲刺: 起点实际路径≤阈值(stepMax), 或 全闭上界==全开下界(已找到最优解)
		if ((mazeState.mapStepClose[mazeState.xStart][mazeState.yStart] <= searchSm.stepMax) || (mazeState.mapStepClose[mazeState.xStart][mazeState.yStart] - mazeState.mapStepOpen[mazeState.xStart][mazeState.yStart] == 0))
		{
			FLASH_BlockWrite();
			MOTION_MouseTurnBack();
			SPURT_ObjectGoTo(mazeState.xStart, mazeState.yStart);
			MOTION_MouseTurnBackSpecial();
			mazeState.mouseTask = SPURT;
			return;
		}
		else
		{
			if ((mazeState.mapStepClose[searchSm.shortWayX][searchSm.shortWayY] - mazeState.mapStepOpen[searchSm.shortWayX][searchSm.shortWayY] > 0) && (mazeState.mapStepCloseCopy[searchSm.shortWayX][searchSm.shortWayY] + mazeState.mapStepOpen[searchSm.shortWayX][searchSm.shortWayY] < mazeState.mapStepClose[mazeState.xStart][mazeState.yStart]))
			{
				while (searchSm.crossIdx > mazeState.stackIdx)
				{
					searchSm.crossIdx--;
					searchSm.roadCount = EXPLORE_CrosswayCheck(mazeState.crosswayBuf[searchSm.crossIdx].cX, mazeState.crosswayBuf[searchSm.crossIdx].cY);
					searchSm.tempWayX = mazeState.crosswayBuf[searchSm.crossIdx].cX;
					searchSm.tempWayY = mazeState.crosswayBuf[searchSm.crossIdx].cY;
					if (searchSm.roadCount)
					{
						if ((mazeState.mapStepClose[searchSm.tempWayX][searchSm.tempWayY] - mazeState.mapStepOpen[searchSm.tempWayX][searchSm.tempWayY] > 0) && (mazeState.mapStepCloseCopy[searchSm.tempWayX][searchSm.tempWayY] + mazeState.mapStepOpen[searchSm.tempWayX][searchSm.tempWayY] < mazeState.mapStepClose[mazeState.xStart][mazeState.yStart]))
						{
							SPURT_ObjectGoTo(mazeState.crosswayBuf[searchSm.crossIdx].cX, mazeState.crosswayBuf[searchSm.crossIdx].cY);
							if (searchSm.roadCount > 1)
							{
								searchSm.crossIdx++;
							}
							FLOODFILL_Close(mazeState.goalX, mazeState.goalY);
							EXPLORE_BackPointChoice(searchSm.tempWayX, searchSm.tempWayY);
							SEARCH_Search();
							return; /* 结束本次返回搜索 */
						}
					}
				}
				if (searchSm.crossIdx == 0)
				{ 	/* 走完了所有的支路, 回到起点 */
					SPURT_ObjectGoTo(mazeState.crosswayBuf[0].cX, mazeState.crosswayBuf[0].cY);
					MOTION_MouseTurnBackSpecial();
					mazeState.mouseTask = SPURT; /* 电脑鼠将开始冲刺状态 */
					return;
				}
			}
			while (--mazeState.stackIdx)
			{
				searchSm.roadCount = EXPLORE_CrosswayCheck(mazeState.crosswayBuf[mazeState.stackIdx].cX, mazeState.crosswayBuf[mazeState.stackIdx].cY);
				searchSm.tempWayX = mazeState.crosswayBuf[mazeState.stackIdx].cX;
				searchSm.tempWayY = mazeState.crosswayBuf[mazeState.stackIdx].cY;
				if (searchSm.roadCount)
				{
					if (mazeState.mapStepCloseCopy[searchSm.tempWayX][searchSm.tempWayY] + mazeState.mapStepOpen[searchSm.tempWayX][searchSm.tempWayY] < mazeState.mapStepClose[mazeState.xStart][mazeState.yStart])
					{
						if ((mazeState.mapStepClose[searchSm.tempWayX][searchSm.tempWayY] - mazeState.mapStepOpen[searchSm.tempWayX][searchSm.tempWayY] > 0))
						{
							SPURT_ObjectGoTo(mazeState.crosswayBuf[mazeState.stackIdx].cX, mazeState.crosswayBuf[mazeState.stackIdx].cY); // 返回点待改进
							searchSm.shortWayX = mazeState.crosswayBuf[mazeState.stackIdx].cX;
							searchSm.shortWayY = mazeState.crosswayBuf[mazeState.stackIdx].cY;
							searchSm.crossIdx = mazeState.stackIdx;
							if (searchSm.roadCount > 1)
							{
								searchSm.crossIdx++;
							}
							FLOODFILL_Close(mazeState.goalX, mazeState.goalY);
							EXPLORE_BackPointChoice(searchSm.tempWayX, searchSm.tempWayY);
							SEARCH_Search();
							break; /* 跳出支点回溯循环 */
						}
					}
				}
			}
			if (mazeState.stackIdx == 0)
			{
				SPURT_ObjectGoTo(mazeState.crosswayBuf[0].cX, mazeState.crosswayBuf[0].cY);
				MOTION_MouseTurnBackSpecial();
				mazeState.mouseTask = SPURT; /* 电脑鼠将开始冲刺状态 */
				return;
			}
		}
		if (searchSm.stepMax < searchSm.stepMaxLimit)
		{
			searchSm.stepMax++;
		}
	}
}

/**
 * @brief  迷宫搜索状态处理
 * @param  none
 * @note   洪水填充搜索状态机核心, 分三种情况:
 *         到达终点区: 存图并按 goalCorrectDir 矫正掉头, backSearch==0 时以低速(70/140)
 *         返回起点转 SPURT; 未到过终点(goalGet==0): 支点栈深度优先探索;
 *         返回途中(goalGet==1): 调 STATE_ReturnSearch() 回溯
 */
void STATE_MazeSearch(void)
{
	uchar m = 0;
	uchar l = 0;
	char tempCX1, tempCX2, tempCY1, tempCY2, gox, goy;

	if (((mazeState.mousePos.cX == mazeState.xGoal0) || (mazeState.mousePos.cX == mazeState.xGoal1)) && ((mazeState.mousePos.cY == mazeState.yGoal0) || (mazeState.mousePos.cY == mazeState.yGoal1)))
	{
		if ((mazeState.mousePos.cX + mazeState.mousePos.cY + mazeState.mouseDirection) % 2 == 0)
			searchSt.goalCorrectDir = 1; // 左矫正
		else
			searchSt.goalCorrectDir = 2;
		searchSm.goalGet = 1;
		mazeState.goalX = mazeState.mousePos.cX;
		mazeState.goalY = mazeState.mousePos.cY;
		EXPLORE_GoalWallChange(mazeState.goalX, mazeState.goalY);

		// 重算起点为源(含副本)/终点为源双距离图: mapStepClose[起点]=已探明路径(全闭上界), mapStepOpen[起点]=理论最短(全开下界)
		FLOODFILL_Open(mazeState.xStart, mazeState.yStart);
		FLOODFILL_OpenCopy();
		FLOODFILL_Close(mazeState.xStart, mazeState.yStart);
		FLOODFILL_CloseCopy();
		FLOODFILL_Open(mazeState.goalX, mazeState.goalY);
		FLOODFILL_Close(mazeState.goalX, mazeState.goalY);

		FLASH_BlockWrite();

		if (taskSt.backSearch == 0)
		{

			motionCtrl.tick = 0;
			speedSt.turnaroundTick = 0;
			motionCtrl.task = KEEPWALLDISTANCE;
			while (motionCtrl.tick < 50)
				;
			// 矫正开始
			if (searchSt.goalCorrectDir != 0)
			{ /* 按 goalCorrectDir 执行左/右矫正掉头 */
				if (searchSt.goalCorrectDir == 1)
				{
					MOTION_Turn(POINTL90);
					motionCtrl.tick = 0;
					speedSt.turnaroundTick = 0;
					motionCtrl.task = KEEPWALLDISTANCE;
					while (motionCtrl.tick < 50)
						;
					MOTION_PointTurnLeft();
				}
				else if (searchSt.goalCorrectDir == 2)
				{
					MOTION_Turn(POINTR90);
					motionCtrl.tick = 0;
					speedSt.turnaroundTick = 0;
					motionCtrl.task = KEEPWALLDISTANCE;
					while (motionCtrl.tick < 50)
						;
					MOTION_PointTurnRight();
				}
			}
			else
			{
				MOTION_MouseTurnBackSpecial();
			}
			// 矫正结束
			lTopSpeedH = spurtSpd.lTopSpeed;
			L45TopSpeedh = spurtSpd.l45TopSpeed;
			BackTopSpeedh = spurtSpd.backTopSpeed;
			spurtSpd.lTopSpeed = 70;
			spurtSpd.l45TopSpeed = 140;
			spurtSpd.backTopSpeed = 70;
			PATH_Fastway4Dir(mazeState.xStart, mazeState.yStart);
			SPURT_GainSet90(SPURT_SPEED_90_LOW);
			spurtSpd.lTopSpeed = lTopSpeedH;
			spurtSpd.l45TopSpeed = L45TopSpeedh;
			spurtSpd.backTopSpeed = BackTopSpeedh;
			// 返回起点结束
			MOTION_MouseTurnBackSpecial();
			mazeState.mousePos.cX = mazeState.xStart;
			mazeState.mousePos.cY = mazeState.yStart;
			mazeState.mouseDirection = 0;

			mazeState.mouseTask = SPURT; /* 电脑鼠将开始冲刺状态 */
			return;
		}

		// 转冲刺: 起点实际路径≤阈值(stepMax), 或 全闭上界==全开下界(已找到最优解)
		if ((mazeState.mapStepClose[mazeState.xStart][mazeState.yStart] <= searchSm.stepMax) || (mazeState.mapStepClose[mazeState.xStart][mazeState.yStart] - mazeState.mapStepOpen[mazeState.xStart][mazeState.yStart] == 0))
		{
			SPURT_ObjectGoTo(mazeState.xStart, mazeState.yStart);
			MOTION_MouseTurnBackSpecial();
			mazeState.mouseTask = SPURT;
			return;
		}
		else
		{
			while (--mazeState.stackIdx)
			{
				searchSm.roadCount = EXPLORE_CrosswayCheck(mazeState.crosswayBuf[mazeState.stackIdx].cX, mazeState.crosswayBuf[mazeState.stackIdx].cY);
				searchSm.tempWayX = mazeState.crosswayBuf[mazeState.stackIdx].cX;
				searchSm.tempWayY = mazeState.crosswayBuf[mazeState.stackIdx].cY;

				if (searchSm.roadCount)
				{
					if (mazeState.mapStepCloseCopy[searchSm.tempWayX][searchSm.tempWayY] + mazeState.mapStepOpen[searchSm.tempWayX][searchSm.tempWayY] < mazeState.mapStepClose[mazeState.xStart][mazeState.yStart])
					{
						SPURT_ObjectGoTo(mazeState.crosswayBuf[mazeState.stackIdx].cX, mazeState.crosswayBuf[mazeState.stackIdx].cY); // 返回点待改进
						searchSm.shortWayX = mazeState.crosswayBuf[mazeState.stackIdx].cX;
						searchSm.shortWayY = mazeState.crosswayBuf[mazeState.stackIdx].cY;
						searchSm.crossIdx = mazeState.stackIdx;
						if (searchSm.roadCount > 1)
						{
							searchSm.crossIdx++;
						}
						FLOODFILL_Close(mazeState.goalX, mazeState.goalY);
						EXPLORE_BackPointChoice(searchSm.tempWayX, searchSm.tempWayY);
						SEARCH_Search();
						break; /* 跳出支点回溯循环 */
					}
				}
			}
			if (mazeState.stackIdx == 0)
			{
				SPURT_ObjectGoTo(mazeState.crosswayBuf[0].cX, mazeState.crosswayBuf[0].cY);
				MOTION_MouseTurnBackSpecial();
				mazeState.mouseTask = SPURT; /* 电脑鼠将开始冲刺状态 */
				return;
			}
		}
	}
	/* 电脑鼠不在终点 */
	else
	{
		/* 未到达过终点 */
		if (searchSm.goalGet == 0)
		{
			searchSm.roadCount = EXPLORE_CrosswayCheck(mazeState.mousePos.cX, mazeState.mousePos.cY); /* 统计可前进的支路数 */
			gox = mazeState.mousePos.cX;
			goy = mazeState.mousePos.cY;
			if (searchSm.roadCount)
			{ /* 有可前进方向 */
				m = EXPLORE_DeadwayCheck(mazeState.stackIdx);
				if (m == mazeState.stackIdx)
				{
					if (searchSm.roadCount > 1)
					{ /* 有多条可前进方向, 保存坐标 */
						mazeState.crosswayBuf[mazeState.stackIdx].cX = mazeState.mousePos.cX;
						mazeState.crosswayBuf[mazeState.stackIdx].cY = mazeState.mousePos.cY;
						mazeState.stackIdx++;
					}
				}
				else
				{
					gox = mazeState.crosswayBuf[m].cX;
					goy = mazeState.crosswayBuf[m].cY;
					mazeState.stackIdx = m + 1;
					SPURT_ObjectGoTo(gox, goy);
				}
				EXPLORE_CrosswayChoice();
				SEARCH_Search(); /* 前进一格 */
			}
			else
			{ /* 没有可前进方向, 回到最近支路 */
				m = EXPLORE_DeadwayCheck(mazeState.stackIdx);
				if (m < mazeState.stackIdx)
					mazeState.stackIdx = m + 1;
				while (--mazeState.stackIdx)
				{
					searchSm.roadCount = EXPLORE_CrosswayCheck(mazeState.crosswayBuf[mazeState.stackIdx].cX,
															   mazeState.crosswayBuf[mazeState.stackIdx].cY);
					/* 统计最近支点未走过的方向数 */

					/*
					 *  若存在未走过的路，则前往该支点，并跳出循环
					 *  否则继续查找还有未走过的支路。
					 */
					if (searchSm.roadCount)
					{
						m = mazeState.stackIdx;
						FLOODFILL_Open(mazeState.goalX, mazeState.goalY);
						if (m > 0)
						{
							tempCX1 = mazeState.crosswayBuf[m].cX;
							tempCY1 = mazeState.crosswayBuf[m].cY;
							l = m;
							tempCX2 = mazeState.crosswayBuf[m].cX;
							tempCY2 = mazeState.crosswayBuf[m].cY;
							gox = tempCX1;
							goy = tempCY1;
							while (l)
							{
								l--;
								searchSm.roadCount = EXPLORE_CrosswayCheck(mazeState.crosswayBuf[l].cX, mazeState.crosswayBuf[l].cY);
								if (searchSm.roadCount)
								{
									tempCX2 = mazeState.crosswayBuf[l].cX;
									tempCY2 = mazeState.crosswayBuf[l].cY;
									if (mazeState.mapStepOpen[tempCX1][tempCY1] > mazeState.mapStepOpen[tempCX2][tempCY2])
									{
										m = l;
										tempCX1 = mazeState.crosswayBuf[m].cX;
										tempCY1 = mazeState.crosswayBuf[m].cY;
										gox = mazeState.crosswayBuf[m].cX;
										goy = mazeState.crosswayBuf[m].cY;
										continue;
									}
									else
									{
										break;
									}
								}
							}
						}

						SPURT_ObjectGoTo(gox, goy);
						searchSm.roadCount = EXPLORE_CrosswayCheck(gox, goy);
						mazeState.stackIdx++;
						if (searchSm.roadCount > 1)
						{
							mazeState.crosswayBuf[mazeState.stackIdx].cX = mazeState.crosswayBuf[m].cX;
							mazeState.crosswayBuf[mazeState.stackIdx].cY = mazeState.crosswayBuf[m].cY;
							mazeState.stackIdx++;
						}
						EXPLORE_CrosswayChoice();
						SEARCH_Search();
						break;
					}
				}
				if (mazeState.stackIdx == 0)
				{ /* 走完了所有的支路, 回到起点 */
					SPURT_ObjectGoTo(mazeState.crosswayBuf[0].cX, mazeState.crosswayBuf[0].cY);
					MOTION_MouseTurnBackSpecial();
					mazeState.mouseTask = SPURT; /* 电脑鼠将开始冲刺状态 */
					return;
				}
			}
		}
		/* 已到达过终点的返回途中 */
		else
		{
			STATE_ReturnSearch(); /* 返回途中搜索逻辑 */
		}
	}
}

/**
 * @brief  斜线冲刺状态处理
 * @param  none
 * @note   生成 8 方向去程+4 方向回程路径存入 Flash, 掉头冲刺;
 *         按 backSearch/goaheadnum 决定返回搜索或分档再冲(速度整定值 240/180/70)
 */
void STATE_Spurt45(void)
{
	u8 c = 0;
	u16 tmptt;
	speedSt.sleepmode = 1;

	PATH_Fastway8Dir(mazeState.goalX, mazeState.goalY);
	flashaddr = 0;
	FLASH_Unlock();
	FLASH_ErasePage(FLASH_ADDR_GOPATH);
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_GOPATH + flashaddr, mazeState.fastwayCount);
	flashaddr += 2;
	for (tmptt = 0; tmptt < 200; tmptt++)
	{
		FLASH_Unlock();
		FLASH_WriteHalfWord(FLASH_ADDR_GOPATH + flashaddr, mazeState.fastwayAction[tmptt].action);
		flashaddr += 2;
	}

	mazeState.mousePos.cX = mazeState.goalX;
	mazeState.mousePos.cY = mazeState.goalY;
	mazeState.mouseDirection = (SPURT_GetFinalDir() / 2 + 2) % 4;

	PATH_Fastway4Dir(mazeState.xStart, mazeState.yStart);
	flashaddr = 0;
	FLASH_Unlock();
	FLASH_ErasePage(FLASH_ADDR_BACKPATH_90);
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_BACKPATH_90 + flashaddr, mazeState.fastwayCount90);
	flashaddr += 2;
	for (tmptt = 0; tmptt < 200; tmptt++)
	{
		FLASH_Unlock();
		FLASH_WriteHalfWord(FLASH_ADDR_BACKPATH_90 + flashaddr, mazeState.fastwayAction90[tmptt].action);
		flashaddr += 2;
	}

	speedSt.sleepmode = 0;
	MOTION_Turn(POINTL90);
	motionCtrl.tick = 0;
	speedSt.turnaroundTick = 0;
	motionCtrl.task = KEEPWALLDISTANCE;
	while (motionCtrl.tick < 50)
		;
	MOTION_PointTurnRight();
	flashaddr = 0;
	mazeState.fastwayCount = FLASH_ReadHalfWord(FLASH_ADDR_GOPATH + flashaddr);
	flashaddr += 2;
	for (tmptt = 0; tmptt < 200; tmptt++)
	{
		mazeState.fastwayAction[tmptt].action = FLASH_ReadHalfWord(FLASH_ADDR_GOPATH + flashaddr);
		flashaddr += 2;
	}
	searchSt.spurt45EndFlag = 1;
	SPURT_GainSet(spurtmode);
	searchSt.spurt45EndFlag = 0;
	LED_On(5);
	LED_On(6);
	LED_On(7);
	LED_On(8);
	if (searchSt.goalCorrectDir != 0)
	{
		if (searchSt.goalCorrectDir == 1)
		{
			MOTION_Turn(POINTL90);
			motionCtrl.tick = 0;
			speedSt.turnaroundTick = 0;
			motionCtrl.task = KEEPWALLDISTANCE;
			while (motionCtrl.tick < 50)
				;
			MOTION_PointTurnLeft();
		}
		else if (searchSt.goalCorrectDir == 2)
		{
			MOTION_Turn(POINTR90);
			motionCtrl.tick = 0;
			speedSt.turnaroundTick = 0;
			motionCtrl.task = KEEPWALLDISTANCE;
			while (motionCtrl.tick < 50)
				;
			MOTION_PointTurnRight();
		}
	}

	if (taskSt.backSearch == 1)
	{
		LED_Off(0);
		searchSt.accdistance = 232;
		MOTION_Acc();
		searchSt.accdistance = 40;
		mazeState.mousePos.cX = mazeState.goalX;
		mazeState.mousePos.cY = mazeState.goalY;
		MOTION_SetTrack(0);
		MAZE_CoorUpdate();
		taskSt.backSearch += 1;
		mazeState.mouseTask = MAZESEARCH; /* 转入返回途中搜索 */
		STATE_ReturnSearch();			  /* 返回途中搜索, 重新调度 */
		return;
	}

	flashaddr = 0;
	mazeState.fastwayCount90 = FLASH_ReadHalfWord(FLASH_ADDR_BACKPATH_90 + flashaddr);
	flashaddr += 2;
	for (tmptt = 0; tmptt < 200; tmptt++)
	{
		mazeState.fastwayAction90[tmptt].action = FLASH_ReadHalfWord(FLASH_ADDR_BACKPATH_90 + flashaddr);
		flashaddr += 2;
	}
	taskSt.goOrBack = 1;
	searchSt.back90Flag = 1;
	SPURT_GainSet90(SPURT_SPEED_90_LOW);
	searchSt.back90Flag = 0;
	taskSt.goOrBack = 0;
	SPURT_BackTurn();

	if (taskSt.goaheadnum == 1 || taskSt.goaheadnum == 2)
	{
		taskSt.goaheadnum = 3; // 高速斜线冲
		spurtSpd.lTopSpeed = 240;
		spurtSpd.l45TopSpeed = 180;
		spurtSpd.backTopSpeed = 240;
		LED_On(5);
		LED_On(6);
		LED_On(7);
		LED_On(8);
		DELAY_Ms(500);
		LED_Off(0);
		mazeState.mousePos.cX = mazeState.xStart;
		mazeState.mousePos.cY = mazeState.yStart;
		mazeState.mouseDirection = 0;
		mazeState.mouseTask = SPURT_45;
	}
	if (taskSt.goaheadnum == 3)
	{
		taskSt.goaheadnum = 4; // 超高速斜线冲
		spurtSpd.lTopSpeed = 240;
		spurtSpd.l45TopSpeed = 180;
		spurtSpd.backTopSpeed = 240;
		LED_On(5);
		LED_On(6);
		LED_On(7);
		LED_On(8);
		DELAY_Ms(500);
		LED_Off(0);
		mazeState.mousePos.cX = mazeState.xStart;
		mazeState.mousePos.cY = mazeState.yStart;
		mazeState.mouseDirection = 0;
		mazeState.mouseTask = SPURT_45;
	}
	else if (taskSt.goaheadnum == 4 || taskSt.goaheadnum == 0)
		while (1)
		{
			{
				speedSt.sleepmode = 1;
				if (c == 9 || c == 0)
					c = 1;
				LED_Off(0);
				LED_On(c);
				DELAY_Ms(1000);
				c++;
			}
		}
}

/**
 * @brief  垂直冲刺状态处理
 * @param  none
 * @note   生成 4 方向去/回程路径存入 Flash, 掉头冲刺;
 *         按 backSearch/goaheadnum 决定返回搜索或进入结束流水灯
 */
void STATE_Spurt90(void)
{
	u8 c = 0;
	u16 tmptt;

	PATH_Fastway4Dir(mazeState.goalX, mazeState.goalY);
	flashaddr = 0;
	FLASH_Unlock();
	FLASH_ErasePage(FLASH_ADDR_GOPATH_90);
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_GOPATH_90 + flashaddr, mazeState.fastwayCount90);
	flashaddr += 2;
	for (tmptt = 0; tmptt < 200; tmptt++)
	{
		FLASH_Unlock();
		FLASH_WriteHalfWord(FLASH_ADDR_GOPATH_90 + flashaddr, mazeState.fastwayAction90[tmptt].action);
		flashaddr += 2;
	}

	mazeState.mousePos.cX = mazeState.goalX;
	mazeState.mousePos.cY = mazeState.goalY;
	mazeState.mouseDirection = (SPURT_GetFinalDir90() + 2) % 4;

	PATH_Fastway4Dir(mazeState.xStart, mazeState.yStart);

	flashaddr = 0;
	FLASH_Unlock();
	FLASH_ErasePage(FLASH_ADDR_BACKPATH_90);
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_BACKPATH_90 + flashaddr, mazeState.fastwayCount90);
	flashaddr += 2;
	for (tmptt = 0; tmptt < 200; tmptt++)
	{
		FLASH_Unlock();
		FLASH_WriteHalfWord(FLASH_ADDR_BACKPATH_90 + flashaddr, mazeState.fastwayAction90[tmptt].action);
		flashaddr += 2;
	}

	MOTION_Turn(POINTL90);
	motionCtrl.tick = 0;
	speedSt.turnaroundTick = 0;
	motionCtrl.task = KEEPWALLDISTANCE;
	while (motionCtrl.tick < 50)
		;
	MOTION_PointTurnRight();

	flashaddr = 0;
	mazeState.fastwayCount90 = FLASH_ReadHalfWord(FLASH_ADDR_GOPATH_90 + flashaddr);
	flashaddr += 2;
	for (tmptt = 0; tmptt < 200; tmptt++)
	{
		mazeState.fastwayAction90[tmptt].action = FLASH_ReadHalfWord(FLASH_ADDR_GOPATH_90 + flashaddr);
		flashaddr += 2;
	}

	searchSt.spurt45EndFlag = 1;
	SPURT_GainSet90(SPURT_SPEED_90_LOW);
	searchSt.spurt45EndFlag = 0;

	if (searchSt.goalCorrectDir != 0)
	{
		if (searchSt.goalCorrectDir == 1)
		{
			MOTION_Turn(POINTL90);
			motionCtrl.tick = 0;
			speedSt.turnaroundTick = 0;
			motionCtrl.task = KEEPWALLDISTANCE;
			while (motionCtrl.tick < 50)
				;
			MOTION_PointTurnLeft();
		}
		else if (searchSt.goalCorrectDir == 2)
		{
			MOTION_Turn(POINTR90);
			motionCtrl.tick = 0;
			speedSt.turnaroundTick = 0;
			motionCtrl.task = KEEPWALLDISTANCE;
			while (motionCtrl.tick < 50)
				;
			MOTION_PointTurnRight();
		}
	}

	if (taskSt.backSearch == 1)
	{
		LED_Off(0);
		searchSt.accdistance = 232;
		MOTION_Acc();
		searchSt.accdistance = 40;
		mazeState.mousePos.cX = mazeState.goalX;
		mazeState.mousePos.cY = mazeState.goalY;
		MOTION_SetTrack(0);
		MAZE_CoorUpdate();
		taskSt.backSearch += 1;
		mazeState.mouseTask = MAZESEARCH; /* 转入返回途中搜索 */
		STATE_ReturnSearch();			  /* 返回途中搜索, 重新调度 */
		return;
	}

	flashaddr = 0;
	mazeState.fastwayCount90 = FLASH_ReadHalfWord(FLASH_ADDR_BACKPATH_90 + flashaddr);
	flashaddr += 2;
	for (tmptt = 0; tmptt < 200; tmptt++)
	{
		mazeState.fastwayAction90[tmptt].action = FLASH_ReadHalfWord(FLASH_ADDR_BACKPATH_90 + flashaddr);
		flashaddr += 2;
	}
	taskSt.goOrBack = 1;
	searchSt.back90Flag = 1;
	SPURT_GainSet90(SPURT_SPEED_90_LOW);
	searchSt.back90Flag = 0;
	taskSt.goOrBack = 0;
	SPURT_BackTurn();

	if (taskSt.goaheadnum == 2)
	{

		while (1)
		{
			speedSt.sleepmode = 1;
			if (c == 9 || c == 0)
				c = 1;
			LED_Off(0);
			LED_On(c);
			DELAY_Ms(1000);
			c++;
		}
	}
	else if (taskSt.goaheadnum == 0)
	{
		while (1)
		{
			speedSt.sleepmode = 1;
			if (c == 9 || c == 0)
				c = 1;
			LED_Off(0);
			LED_On(c);
			DELAY_Ms(1000);
			c++;
		}
	}
}

/**
 * @brief  起点判定
 * @param  none
 * @note   向前搜索一格, 据左/右出口判定起点在迷宫左侧还是右侧; 若在右侧,
 *         镜像翻转已记录墙壁(墙掩码 0x08/0x02/0xf0/0xD0), 登记首个支点后转入 MAZESEARCH
 */
void STATE_Start(void)
{
	uchar ucTemp = 0;
	SEARCH_Search(); /* 向前搜索 */
	if (mazeState.mapWall[mazeState.mousePos.cX][mazeState.mousePos.cY] & 0x08)
	{										  /* 判断电老鼠左边是否存在出口 */
		mazeState.xStart = MAZETYPE - 1;	  /* 修改电脑鼠起点的横坐标 */
		mazeState.mousePos.cX = MAZETYPE - 1; /* 修改电脑鼠当前位置的横坐标 */
		/*
		 *  由于默认的起点为(0,0)，现在需要把已记录的墙壁资料转换过来
		 */

		if (mazeState.mousePos.cY < MAZETYPE - 1)
		{
			mazeState.mapWall[MAZETYPE - 1][mazeState.mousePos.cY + 1] = mazeState.mapWall[0][mazeState.mousePos.cY + 1];
			mazeState.mapWall[0][mazeState.mousePos.cY + 1] = 0xf0;
		}
		ucTemp = mazeState.mousePos.cY;
		do
		{
			mazeState.mapWall[MAZETYPE - 1][ucTemp] = mazeState.mapWall[0][ucTemp];
			mazeState.mapWall[MAZETYPE - 2][ucTemp] = 0xD0;
			mazeState.mapWall[0][ucTemp] = 0xf0;
			mazeState.mapWall[1][ucTemp] = 0xf0;
		} while (ucTemp--);
		mazeState.mapWall[MAZETYPE - 2][mazeState.mousePos.cY] = 0xf0;
		mazeState.goalX = 8;

		mazeState.crosswayBuf[mazeState.stackIdx].cX = MAZETYPE - 1;
		mazeState.crosswayBuf[mazeState.stackIdx].cY = 0;
		mazeState.stackIdx++;
		mazeState.mouseTask = MAZESEARCH; /* 状态转换为搜寻状态 */
	}
	else if (mazeState.mapWall[mazeState.mousePos.cX][mazeState.mousePos.cY] & 0x02)
	{ /* 判断电老鼠右边是否存在出口 */

		mazeState.crosswayBuf[mazeState.stackIdx].cX = 0;
		mazeState.crosswayBuf[mazeState.stackIdx].cY = 0;
		mazeState.stackIdx++;
		mazeState.mouseTask = MAZESEARCH; /* 状态转换为搜寻状态 */
	}
}

/**
 * @brief  搜索结束保存数据并转入冲刺
 * @param  none
 * @note   将迷宫墙图/起终点写入 Flash; goaheadnum==1/3 转 SPURT_45, ==2 转 SPURT_90,
 *         否则保持 SPURT(由主循环补做 SPURT_45)
 */
void STATE_Spurt(void)
{
	u8 blockx, blocky; /* 迷宫行列循环变量 */
	FLASH_Unlock();
	FLASH_ErasePage(FLASH_ADDR_BLOCK_POINT);
	FLASH_Unlock();
	FLASH_ErasePage(FLASH_ADDR_BLOCK);
	flashaddr = 0;
	for (blockx = 0; blockx < 16; blockx++)
	{
		for (blocky = 0; blocky < 16; blocky++)
		{
			FLASH_Unlock();
			FLASH_WriteHalfWord(FLASH_ADDR_BLOCK + flashaddr, mazeState.mapWall[blockx][blocky]);
			flashaddr += 2;
		}
	}
	flashaddr = 0;
	for (blockx = 0; blockx < 16; blockx++)
	{
		for (blocky = 0; blocky < 16; blocky++)
		{
			mazeState.mapWall[blockx][blocky] = FLASH_ReadHalfWord(FLASH_ADDR_BLOCK + flashaddr);
			flashaddr += 2;
		}
	}
	taskSt.spurtFlag = 1;
	flashaddr = 0;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_BLOCK_POINT + flashaddr, mazeState.xStart);
	flashaddr += 2;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_BLOCK_POINT + flashaddr, mazeState.yStart);
	flashaddr += 2;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_BLOCK_POINT + flashaddr, mazeState.goalX);
	flashaddr += 2;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_BLOCK_POINT + flashaddr, mazeState.goalY);
	flashaddr += 2;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_BLOCK_POINT + flashaddr, searchSt.goalCorrectDir);
	flashaddr += 2;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_BLOCK_POINT + flashaddr, taskSt.spurtFlag);
	flashaddr += 2;

	taskSt.backSearch += 1;

	DELAY_Ms(200);
	if (taskSt.goaheadnum == 1 || taskSt.goaheadnum == 3)
	{
		mazeState.mouseTask = SPURT_45;
		spurtmode = SPURT_SPEED_45_LOW;
		return;
	}
	if (taskSt.goaheadnum == 2)
	{
		mazeState.mouseTask = SPURT_90;
		spurtmode = SPURT_SPEED_90_LOW;
		return;
	}
}

/**
 * @brief  主任务状态机循环
 * @param  none
 * @note   while(1) 内按 mouseTask 分派: WAIT 启动/START 起点判定/MAZESEARCH 搜索/
 *         SPURT 数据保存/SPURT_45 斜线冲刺/SPURT_90 垂直冲刺, 不返回
 */
void STATE_MainLoop(void)
{
	STATE_InitSearchSm(); /* 搜索状态机初值(进入主循环前) */
	while (1)
	{
		switch (mazeState.mouseTask)
		{ /* 状态机处理 */

		case WAIT:

			MOTION_Acc();
			MOTION_SetTrack(100);
			mazeState.mouseTask = START;
			break;

		case START:		   /* 判断电脑鼠起点的横坐标 */
			STATE_Start();
			break;

			/* 搜索开始 */
		case MAZESEARCH:
			STATE_MazeSearch();
			break;

		case SPURT:
			STATE_Spurt();
			if (mazeState.mouseTask == SPURT)
				STATE_Spurt45(); /* goaheadnum 非 1/2/3 时保持 SPURT, 此处补做斜线冲刺 */
			break;

		case SPURT_45:
			STATE_Spurt45();
			break;

		case SPURT_90:
			STATE_Spurt90();
			break;

		default:
			break;
		}
	}
}
