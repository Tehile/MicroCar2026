/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: path.c —— 最短路路径生成(8方向斜切/4方向垂直/回程)
*********************************************************************************************************
*/

#include "path.h"
/**
 * @brief 8方向45度斜切冲刺路径生成
 * @param GX 目标横坐标
 * @param GY 目标纵坐标
 * @note 基于 mapStepClose 距离图生成含45度斜切的8方向最短动作序列, 存入 fastwayAction;
 *       方向编码 mouseDirection×2(8方向), 动作算术 (8+cDirTemp-mouseDir)%8;
 *       斜切是缩短路径的关键, 区别于 PATH_Fastway4Dir(4方向90度)
 */
void PATH_Fastway8Dir(uchar GX, uchar GY)
{
	char i, j, cX, cY;
	uchar ucStep;
	uchar cDirTemp;
	uchar wayDir;
	uchar goDir;
	// 8方向编码: 绝对方向×2(0=上/2=右/4=下/6=左, 奇数位为45度斜向)
	uchar mouseDir = mazeState.mouseDirection * 2;
	cX = mazeState.mousePos.cX;
	cY = mazeState.mousePos.cY;

	for (i = 0; i < 16; i++)
	{
		for (j = 0; j < 16; j++)
		{
			mazeState.mapFastway[i][j] = 0xff;
		}
	}
	mazeState.fastwayCount = 0;

	FLOODFILL_Close(GX, GY);
	ucStep = mazeState.mapStepClose[cX][cY];

	if ((mazeState.mapWall[cX][cY] & WALL_UP_OPEN) && (mazeState.mapStepClose[cX][cY + 1] < ucStep))
	{
		cDirTemp = 0;
		if (mouseDir == cDirTemp)
		{
			mazeState.mapFastway[cX][cY] = cDirTemp;
			wayDir = cDirTemp;
			mazeState.fastwayAction[mazeState.fastwayCount].action = 0;
			mazeState.fastwayAction[mazeState.fastwayCount].cx = cX;
			mazeState.fastwayAction[mazeState.fastwayCount].cy = cY;
			mazeState.fastwayCount++;
			cY++;
			goto sec;
		}
	}
	if ((mazeState.mapWall[cX][cY] & WALL_RIGHT_OPEN) && (mazeState.mapStepClose[cX + 1][cY] < ucStep))
	{
		cDirTemp = 2;
		if (mouseDir == cDirTemp)
		{
			mazeState.mapFastway[cX][cY] = cDirTemp;
			wayDir = cDirTemp;
			mazeState.fastwayAction[mazeState.fastwayCount].action = 0;
			mazeState.fastwayAction[mazeState.fastwayCount].cx = cX;
			mazeState.fastwayAction[mazeState.fastwayCount].cy = cY;
			mazeState.fastwayCount++;
			cX++;
			goto sec;
		}
	}
	if ((mazeState.mapWall[cX][cY] & WALL_DOWN_OPEN) && (mazeState.mapStepClose[cX][cY - 1] < ucStep))
	{
		cDirTemp = 4;
		if (mouseDir == cDirTemp)
		{
			mazeState.mapFastway[cX][cY] = cDirTemp;
			wayDir = cDirTemp;
			mazeState.fastwayAction[mazeState.fastwayCount].action = 0;
			mazeState.fastwayAction[mazeState.fastwayCount].cx = cX;
			mazeState.fastwayAction[mazeState.fastwayCount].cy = cY;
			mazeState.fastwayCount++;
			cY--;
			goto sec;
		}
	}
	if ((mazeState.mapWall[cX][cY] & WALL_LEFT_OPEN) && (mazeState.mapStepClose[cX - 1][cY] < ucStep))
	{
		cDirTemp = 6;
		if (mouseDir == cDirTemp)
		{
			mazeState.mapFastway[cX][cY] = cDirTemp;
			wayDir = cDirTemp;
			mazeState.fastwayAction[mazeState.fastwayCount].action = 0;
			mazeState.fastwayAction[mazeState.fastwayCount].cx = cX;
			mazeState.fastwayAction[mazeState.fastwayCount].cy = cY;
			mazeState.fastwayCount++;
			cX--;
			goto sec;
		}
	}

	// 计算相对转向量: (目标8向方向-当前方向)模8, 0为直行
	mazeState.fastwayAction[mazeState.fastwayCount].action = (8 + cDirTemp - mouseDir) % 8;
	mazeState.fastwayAction[mazeState.fastwayCount].cx = cX;
	mazeState.fastwayAction[mazeState.fastwayCount].cy = cY;
	mazeState.fastwayCount++;

	mazeState.mapFastway[cX][cY] = cDirTemp;
	wayDir = cDirTemp;
	mouseDir = cDirTemp;
	switch (cDirTemp)
	{
	case 0:
		cY++;
		break;
	case 2:
		cX++;
		break;
	case 4:
		cY--;
		break;
	case 6:
		cX--;
		break;
	default:
		break;
	}

sec:
	// 沿等高值递减方向推进, 直至到达目标格
	while ((cX != GX) || (cY != GY))
	{
		ucStep = mazeState.mapStepClose[cX][cY];
		mouseDir = cDirTemp;
		if ((mazeState.mapWall[cX][cY] & WALL_UP_OPEN) && (mazeState.mapStepClose[cX][cY + 1] < ucStep))
		{
			goDir = 0;
			if (wayDir == 0)
			{
				cDirTemp = 0;
			}
			if (wayDir == 2)
			{
				cDirTemp = 1;
			}
			if (wayDir == 6)
			{
				cDirTemp = 7;
			}
			if (mouseDir == cDirTemp)
			{
				mazeState.mapFastway[cX][cY] = cDirTemp;
				wayDir = goDir;
				mazeState.fastwayAction[mazeState.fastwayCount].action = 0;
				mazeState.fastwayAction[mazeState.fastwayCount].cx = cX;
				mazeState.fastwayAction[mazeState.fastwayCount].cy = cY;
				mazeState.fastwayCount++;
				cY++;
				continue;
			}
		}
		if ((mazeState.mapWall[cX][cY] & WALL_RIGHT_OPEN) && (mazeState.mapStepClose[cX + 1][cY] < ucStep))
		{
			goDir = 2;
			if (wayDir == 0)
			{
				cDirTemp = 1;
			}
			if (wayDir == 2)
			{
				cDirTemp = 2;
			}
			if (wayDir == 4)
			{
				cDirTemp = 3;
			}
			if (mouseDir == cDirTemp)
			{
				mazeState.mapFastway[cX][cY] = cDirTemp;
				wayDir = goDir;
				mazeState.fastwayAction[mazeState.fastwayCount].action = 0;
				mazeState.fastwayAction[mazeState.fastwayCount].cx = cX;
				mazeState.fastwayAction[mazeState.fastwayCount].cy = cY;
				mazeState.fastwayCount++;
				cX++;
				continue;
			}
		}
		if ((mazeState.mapWall[cX][cY] & WALL_DOWN_OPEN) && (mazeState.mapStepClose[cX][cY - 1] < ucStep))
		{
			goDir = 4;
			if (wayDir == 2)
			{
				cDirTemp = 3;
			}
			if (wayDir == 4)
			{
				cDirTemp = 4;
			}
			if (wayDir == 6)
			{
				cDirTemp = 5;
			}
			if (mouseDir == cDirTemp)
			{
				mazeState.mapFastway[cX][cY] = cDirTemp;
				wayDir = goDir;
				mazeState.fastwayAction[mazeState.fastwayCount].action = 0;
				mazeState.fastwayAction[mazeState.fastwayCount].cx = cX;
				mazeState.fastwayAction[mazeState.fastwayCount].cy = cY;
				mazeState.fastwayCount++;
				cY--;
				continue;
			}
		}
		if ((mazeState.mapWall[cX][cY] & WALL_LEFT_OPEN) && (mazeState.mapStepClose[cX - 1][cY] < ucStep))
		{
			goDir = 6;
			if (wayDir == 0)
			{
				cDirTemp = 7;
			}
			if (wayDir == 4)
			{
				cDirTemp = 5;
			}
			if (wayDir == 6)
			{
				cDirTemp = 6;
			}
			if (mouseDir == cDirTemp)
			{
				mazeState.mapFastway[cX][cY] = cDirTemp;
				wayDir = goDir;
				mazeState.fastwayAction[mazeState.fastwayCount].action = 0;
				mazeState.fastwayAction[mazeState.fastwayCount].cx = cX;
				mazeState.fastwayAction[mazeState.fastwayCount].cy = cY;
				mazeState.fastwayCount++;
				cX--;
				continue;
			}
		}

		mazeState.mapFastway[cX][cY] = cDirTemp;

		mazeState.fastwayAction[mazeState.fastwayCount].action = (8 + mazeState.mapFastway[cX][cY] - mouseDir) % 8;
		mazeState.fastwayAction[mazeState.fastwayCount].cx = cX;
		mazeState.fastwayAction[mazeState.fastwayCount].cy = cY;
		mazeState.fastwayCount++;

		mouseDir = cDirTemp;
		wayDir = goDir;

		switch (goDir)
		{
		case 0:
			cY++;
			break;
		case 2:
			cX++;
			break;
		case 4:
			cY--;
			break;
		case 6:
			cX--;
			break;
		default:
			break;
		}
	}

	mazeState.fastwayAction[mazeState.fastwayCount].action = (8 + wayDir - mouseDir) % 8;
	mazeState.fastwayAction[mazeState.fastwayCount].cx = cX;
	mazeState.fastwayAction[mazeState.fastwayCount].cy = cY;
	mazeState.fastwayCount++;
}

/**
 * @brief 4方向90度冲刺路径生成
 * @param GX 目标横坐标
 * @param GY 目标纵坐标
 * @note 基于 mapStepClose 生成4方向(90度)动作序列, 存入 fastwayAction90;
 *       动作算术 (4+cDirTemp-mouseDir)%4, 无斜切
 */
void PATH_Fastway4Dir(uchar GX, uchar GY)
{
	char i, j, cX, cY;
	uchar ucStep;
	uchar cDirTemp;
	uchar wayDir;
	// 4方向编码: 绝对方向(0=上/1=右/2=下/3=左)
	uchar mouseDir = mazeState.mouseDirection;
	cX = mazeState.mousePos.cX;
	cY = mazeState.mousePos.cY;

	for (i = 0; i < 16; i++)
	{
		for (j = 0; j < 16; j++)
		{
			mazeState.mapFastway90[i][j] = 0xff;
		}
	}
	mazeState.fastwayCount90 = 0;

	FLOODFILL_Close(GX, GY);
	ucStep = mazeState.mapStepClose[cX][cY];
	if ((mazeState.mapWall[cX][cY] & WALL_UP_OPEN) && (mazeState.mapStepClose[cX][cY + 1] < ucStep))
	{
		cDirTemp = 0;
		if (mouseDir == cDirTemp)
		{
			mazeState.mapFastway90[cX][cY] = cDirTemp;
			wayDir = cDirTemp;
			mazeState.fastwayAction90[mazeState.fastwayCount90].action = 0;
			mazeState.fastwayAction90[mazeState.fastwayCount90].cx = cX;
			mazeState.fastwayAction90[mazeState.fastwayCount90].cy = cY;
			mazeState.fastwayCount90++;
			cY++;
			goto sec;
		}
	}
	if ((mazeState.mapWall[cX][cY] & WALL_RIGHT_OPEN) && (mazeState.mapStepClose[cX + 1][cY] < ucStep))
	{
		cDirTemp = 1;
		if (mouseDir == cDirTemp)
		{
			mazeState.mapFastway90[cX][cY] = cDirTemp;
			wayDir = cDirTemp;
			mazeState.fastwayAction90[mazeState.fastwayCount90].action = 0;
			mazeState.fastwayAction90[mazeState.fastwayCount90].cx = cX;
			mazeState.fastwayAction90[mazeState.fastwayCount90].cy = cY;
			mazeState.fastwayCount90++;
			cX++;
			goto sec;
		}
	}
	if ((mazeState.mapWall[cX][cY] & WALL_DOWN_OPEN) && (mazeState.mapStepClose[cX][cY - 1] < ucStep))
	{
		cDirTemp = 2;
		if (mouseDir == cDirTemp)
		{
			mazeState.mapFastway90[cX][cY] = cDirTemp;
			wayDir = cDirTemp;
			mazeState.fastwayAction90[mazeState.fastwayCount90].action = 0;
			mazeState.fastwayAction90[mazeState.fastwayCount90].cx = cX;
			mazeState.fastwayAction90[mazeState.fastwayCount90].cy = cY;
			mazeState.fastwayCount90++;
			cY--;
			goto sec;
		}
	}
	if ((mazeState.mapWall[cX][cY] & WALL_LEFT_OPEN) && (mazeState.mapStepClose[cX - 1][cY] < ucStep))
	{
		cDirTemp = 3;
		if (mouseDir == cDirTemp)
		{
			mazeState.mapFastway90[cX][cY] = cDirTemp;
			wayDir = cDirTemp;
			mazeState.fastwayAction90[mazeState.fastwayCount90].action = 0;
			mazeState.fastwayAction90[mazeState.fastwayCount90].cx = cX;
			mazeState.fastwayAction90[mazeState.fastwayCount90].cy = cY;
			mazeState.fastwayCount90++;
			cX--;
			goto sec;
		}
	}

	// 计算相对转向量: (目标4向方向-当前方向)模4
	mazeState.fastwayAction90[mazeState.fastwayCount90].action = (4 + cDirTemp - mouseDir) % 4;
	mazeState.fastwayAction90[mazeState.fastwayCount90].cx = cX;
	mazeState.fastwayAction90[mazeState.fastwayCount90].cy = cY;
	mazeState.fastwayCount90++;

	mazeState.mapFastway90[cX][cY] = cDirTemp;
	wayDir = cDirTemp;
	mouseDir = cDirTemp;
	switch (cDirTemp)
	{
	case 0:
		cY++;
		break;
	case 1:
		cX++;
		break;
	case 2:
		cY--;
		break;
	case 3:
		cX--;
		break;
	default:
		break;
	}

sec:
	// 沿等高值递减推进, 直至到达目标格
	while ((cX != GX) || (cY != GY))
	{
		ucStep = mazeState.mapStepClose[cX][cY];
		mouseDir = cDirTemp;
		if ((mazeState.mapWall[cX][cY] & WALL_UP_OPEN) && (mazeState.mapStepClose[cX][cY + 1] < ucStep))
		{
			cDirTemp = 0;
			if (mouseDir == cDirTemp)
			{
				mazeState.mapFastway90[cX][cY] = cDirTemp;
				wayDir = cDirTemp;
				mazeState.fastwayAction90[mazeState.fastwayCount90].action = 0;
				mazeState.fastwayAction90[mazeState.fastwayCount90].cx = cX;
				mazeState.fastwayAction90[mazeState.fastwayCount90].cy = cY;
				mazeState.fastwayCount90++;
				cY++;
				continue;
			}
		}
		if ((mazeState.mapWall[cX][cY] & WALL_RIGHT_OPEN) && (mazeState.mapStepClose[cX + 1][cY] < ucStep))
		{
			cDirTemp = 1;
			if (mouseDir == cDirTemp)
			{
				mazeState.mapFastway90[cX][cY] = cDirTemp;
				wayDir = cDirTemp;
				mazeState.fastwayAction90[mazeState.fastwayCount90].action = 0;
				mazeState.fastwayAction90[mazeState.fastwayCount90].cx = cX;
				mazeState.fastwayAction90[mazeState.fastwayCount90].cy = cY;
				mazeState.fastwayCount90++;
				cX++;
				continue;
			}
		}
		if ((mazeState.mapWall[cX][cY] & WALL_DOWN_OPEN) && (mazeState.mapStepClose[cX][cY - 1] < ucStep))
		{
			cDirTemp = 2;
			if (mouseDir == cDirTemp)
			{
				mazeState.mapFastway90[cX][cY] = cDirTemp;
				wayDir = cDirTemp;
				mazeState.fastwayAction90[mazeState.fastwayCount90].action = 0;
				mazeState.fastwayAction90[mazeState.fastwayCount90].cx = cX;
				mazeState.fastwayAction90[mazeState.fastwayCount90].cy = cY;
				mazeState.fastwayCount90++;
				cY--;
				continue;
			}
		}
		if ((mazeState.mapWall[cX][cY] & WALL_LEFT_OPEN) && (mazeState.mapStepClose[cX - 1][cY] < ucStep))
		{
			cDirTemp = 3;
			if (mouseDir == cDirTemp)
			{
				mazeState.mapFastway90[cX][cY] = cDirTemp;
				wayDir = cDirTemp;
				mazeState.fastwayAction90[mazeState.fastwayCount90].action = 0;
				mazeState.fastwayAction90[mazeState.fastwayCount90].cx = cX;
				mazeState.fastwayAction90[mazeState.fastwayCount90].cy = cY;
				mazeState.fastwayCount90++;
				cX--;
				continue;
			}
		}
		mazeState.fastwayAction90[mazeState.fastwayCount90].action = (4 + cDirTemp - mouseDir) % 4;
		mazeState.fastwayAction90[mazeState.fastwayCount90].cx = cX;
		mazeState.fastwayAction90[mazeState.fastwayCount90].cy = cY;
		mazeState.fastwayCount90++;
		mazeState.mapFastway90[cX][cY] = cDirTemp;
		wayDir = cDirTemp;
		mouseDir = cDirTemp;

		switch (cDirTemp)
		{
		case 0:
			cY++;
			break;
		case 1:
			cX++;
			break;
		case 2:
			cY--;
			break;
		case 3:
			cX--;
			break;
		default:
			break;
		}
	}
	mazeState.fastwayAction90[mazeState.fastwayCount90].action = (4 + wayDir - mouseDir) % 4;
	mazeState.fastwayAction90[mazeState.fastwayCount90].cx = cX;
	mazeState.fastwayAction90[mazeState.fastwayCount90].cy = cY;
	mazeState.fastwayCount90++;
}

/**
 * @brief 回程路径生成: 倒序取反动作序列
 * @param FastwayAction_1 待转换的动作序列(原地修改)
 * @param FastwayCount_2 动作数
 * @note 将斜切方向取反(1↔7, 2↔6), 使正向动作序列镜像为反向回程
 */
void PATH_BackPath(FastwayAction *FastwayAction_1, uchar FastwayCount_2)
{
	int i;
	for (i = 0; i < FastwayCount_2; i++)
	{
		mazeState.fastwayActionTemp[i] = FastwayAction_1[FastwayCount_2 - i - 1].action;
	}
	for (i = 0; i < FastwayCount_2; i++)
	{
		// 8方向动作取反镜像: 1↔7, 2↔6(斜切方向反向)
		switch (mazeState.fastwayActionTemp[i])
		{
		case 1:
			mazeState.fastwayActionTemp[i] = 7;
			break;
		case 2:
			mazeState.fastwayActionTemp[i] = 6;
			break;
		case 6:
			mazeState.fastwayActionTemp[i] = 2;
			break;
		case 7:
			mazeState.fastwayActionTemp[i] = 1;
			break;
		}
	}
	FastwayAction_1[0].action = 0;
	for (i = 1; i < FastwayCount_2; i++)
	{
		FastwayAction_1[i].action = mazeState.fastwayActionTemp[i - 1];
	}
}
