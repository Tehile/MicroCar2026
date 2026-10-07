/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: search.c —— 搜索走格(前进/转向/回转)与搜索状态管理
*********************************************************************************************************
*/
/* 本文件在算法中位置: 执行层--搜索走格控制, 驱动一次走格直到到达逻辑节点。 */

#include "search.h"

/* 搜索运动状态集(定义见 search.h SearchState); 初值由 SEARCH_Init 逐字段赋值 */
SearchState searchSt;

/**
 * @brief 搜索运动状态逐字段赋值(结构定义见 search.h SearchState)
 * @param none
 */
void SEARCH_Init(void)
{
	searchSt.lastSearchAct = Straight;
	searchSt.turnRightOffset = 0;
	searchSt.turnLeftOffset = 0;
	searchSt.turnOffset = 0;
	searchSt.spurt45EndFlag = 0;
	searchSt.spurtActionNum = 0;
	searchSt.afterTurnSide = 0;
	searchSt.goalCorrectDir = 0;
	searchSt.back90Flag = 0;
	searchSt.lpeakfindenable = 0;
	searchSt.rpeakfindenable = 0;
	searchSt.skipCoorUpdate = 0;
	searchSt.speedmode = 0;
	searchSt.accdistance = 40;
	searchSt.decdistance = 40;
}
/**
 * @brief 搜索走格: 坐标更新/调度, 补全转弯后可能出现的路程
 * @param none
 */
void SEARCH_Search()
{
	if (searchSt.skipCoorUpdate == 1)
		searchSt.skipCoorUpdate = 0;
	motionCtrl.pidErrPrev = 0;
	motionCtrl.pidErr = 0;
	gyroStr.anglePrev = 0;
	gyroStr.anglePrev2 = 0;
	gyroStr.anglePeak = 0;
	gyroStr.anglePeakPrev = 0;
	motionCtrl.task = SMARTSTRAIGHT;
	// 有运动任务执行, 一定是转弯后有路程需要补全; 依有无墙自动选择矫正方式
	while (1)
	{
		if (odomStr.cellOdom >= 180) // 到达逻辑节点: 无论直走还是转弯, 走完一格都应满足此条件
		{
			straightCells++; // 连续直线格数
			// 原 basetrack+=180 曾用于基准跟进, 因数值异常已停用
			MOTION_SetTrack(0);
			if (searchSt.skipCoorUpdate == 0)
				MAZE_CoorUpdate();
			if (MAZE_BreakOrNot() == 1)
			{
				straightCells = 1;
				break;
			}
		}
		if (odomStr.cellOdom > 90 && odomStr.cellOdom < 180)
			; // 直线-直线/直线-转弯均满足; 转弯-转弯时可能需要补全路程
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
 * @brief 走有墙格(前半段红外、后半段盲走, 死循环)
 * @param none
 * @note 若下一格无墙, 末段进行纵向矫正; 转弯后调用需加速(闭眼走, 不依赖角度)
 */
void SEARCH_HasWall()
{
	searchSt.turnOffset = 0;
	if (irDist.f < 50) // 明明有墙却还命令直走，则报错
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
			LED_On(1);
			LED_On(2);
			DELAY_Ms(500);
		}
	}

	if (searchSt.lastSearchAct != Straight)
		MOTION_SetTrack(odomStr.cellOdom + 20); // 转弯接直线 格子初始点后移20mm
	searchSt.lastSearchAct = Straight;
	while (odomStr.cellOdom < 120 + 8)
		motionCtrl.task = SMARTSTRAIGHTFAST; // 睁眼走直线
	motionCtrl.pidErrPrev = 0;
	motionCtrl.pidErr = 0;
	while (odomStr.cellOdom < 180)
		motionCtrl.task = STRAIGHTEYECLOSECOORADJ; // 闭眼走直线，若遇到有墙-无墙则进行纵向矫正
}

/**
 * @brief 走无墙格(末段利用柱子校正姿态, 死循环)
 * @param none
 * @note 若柱子之后仍无墙(排骨情况), 进行纵向矫正; 转弯后调用需加速
 */
void SEARCH_NoWall()
{
	searchSt.turnOffset = 0;
	if (irDist.f < 50) // 明明有墙却还命令直走，则报错
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
			LED_On(3);
			LED_On(4);
			DELAY_Ms(500);
		}
	}
	if (searchSt.lastSearchAct != Straight)
		MOTION_SetTrack(odomStr.cellOdom + 20);
	searchSt.lastSearchAct = 1;
	while (odomStr.cellOdom < 120 + 8)
		motionCtrl.task = GOSTRAIGHT;

	wallSns.peakL = 200;
	wallSns.peakR = 200;
	searchSt.lpeakfindenable = 0;
	searchSt.rpeakfindenable = 0;
	wallSns.peakTrkL = 0;
	wallSns.peakTrkR = 0;
	motionCtrl.tick = 0;
	motionCtrl.task = STRAIGHTPOLE;
	while (odomStr.cellOdom < 180)
		; // 利用柱子校正姿态，若柱子之后无墙，则进行纵向矫正
}
