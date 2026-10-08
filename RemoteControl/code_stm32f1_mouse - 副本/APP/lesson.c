/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: lesson.c —— 课程应用实验
*********************************************************************************************************
*/

#include "lesson.h"

/* ---- 课程实验状态实例(定义于本文件, 零初始化; 类型见 lesson.h) ---- */
LessonStruct lessonStr;

/**
 * @brief  实验初始化
 * @param  none
 * @note   为课程代码运行配置环境
 */
void LESSON_Init(void)
{

}

/**
 * @brief  实验中断处理
 * @param  none
 * @note   为课程代码设置后台运行程序
 */
void LESSON_ISR(void)
{
	if (lessonStr.isTest)
	{
		// 匀速直线速度
		speedSt.speedTargetL = lessonStr.speedNeed;
		speedSt.speedTargetR = lessonStr.speedNeed;
		
		
		// 采样运动数据(带越界保护: 写满缓冲区后不再写入, 防止越界覆盖 allDis)
		if(lessonStr.speedSampleIdx % 20 == 0 && (lessonStr.speedSampleIdx / 20) < LESSON_DATA_LEN)
		{
			lessonStr.speedLeftSee[lessonStr.speedSampleIdx/20] = speedSt.speedL;
			lessonStr.speedRightSee[lessonStr.speedSampleIdx/20] = speedSt.speedR;
		}
		lessonStr.speedSampleIdx++;
		// 数据超出上限
		if(lessonStr.speedSampleIdx > LESSON_DATA_LEN * 100)
		{	
			lessonStr.isTest = 0;
			speedSt.freemode = 1;
			LED_On(2);
		}
		
		// 设定最大运行距离
		if(lessonStr.allDis)
			if(odomStr.trackAbsR > lessonStr.allDis || odomStr.trackAbsL > lessonStr.allDis)
			{
				lessonStr.isTest = 0;
				speedSt.freemode = 0;
				LED_On(1);
				speedSt.speedTargetL = 0;
				speedSt.speedTargetR = 0;
			}

		// 红外校准速度
		if (lessonStr.irSwitch)
		{
			if (lessonStr.wallMode == 1)
			{
				lessonStr.irErrPrev = lessonStr.irErr; //
				lessonStr.irErr = irDist.l - sysCfg.distToWall;
				speedSt.speedTargetL = speedSt.speedTargetL - lessonStr.irErr * lessonStr.irLeftP;
				speedSt.speedTargetR = speedSt.speedTargetR + lessonStr.irErr * lessonStr.irRightP;
			}
			else if (lessonStr.wallMode == 2)
			{
				lessonStr.irErrPrev = lessonStr.irErr;
				lessonStr.irErr = sysCfg.distToWall - irDist.r;
				speedSt.speedTargetL = speedSt.speedTargetL - lessonStr.irErr * lessonStr.irLeftP;
				speedSt.speedTargetR = speedSt.speedTargetR + lessonStr.irErr * lessonStr.irRightP;
			}
		}

		// 陀螺仪校准速度
		if (lessonStr.gyroSwitch)
		{

			lessonStr.gyroErrPrev = lessonStr.gyroErr;
			lessonStr.gyroErr = gyroStr.angle;

			// 陀螺仪PD控制
			speedSt.speedTargetL -= lessonStr.gyroErr * lessonStr.gyroP + (lessonStr.gyroErr - lessonStr.gyroErrPrev) * lessonStr.gyroD;
			speedSt.speedTargetR += lessonStr.gyroErr * lessonStr.gyroP + (lessonStr.gyroErr - lessonStr.gyroErrPrev) * lessonStr.gyroD;
		}
	}
}

/**
 * @brief  实验1：电机驱动实验
 * @param  none
 * @note
 */
void LESSON_Motor1(void)
{
	lessonStr.speedNeed = 50; 	//固定速度
	lessonStr.allDis = 1000; 	//固定前进距离
	while (1)
	{
		speedSt.speedTargetL = 0;
		speedSt.speedTargetR = 0;
		speedSt.freemode = 1;

		if (BUTTON1)
		{
			DELAY_Ms(5);

			if (BUTTON1)
			{
				LED_On(0);
				speedSt.freemode = 0;
				DELAY_Ms(2000);
				LED_Off(0);
				odomStr.trackAbsL = odomStr.trackAbsR = 0; 				// 里程清零, 从发车时刻起算
				odomStr.distLCycle = odomStr.distL = odomStr.distLPrev = odomStr.distRPrev = odomStr.distR = odomStr.distRCycle = 0;
				TIM2->CNT = 0;
				TIM3->CNT = 0;
				lessonStr.isTest = 1;
				break;
			}
		}
	}
	while (1)
	{
		if (BUTTON1)
		{
			DELAY_Ms(5);

			if (BUTTON1) // 消抖
			{
				LED_On(0);
				printf("\r\nleft:\r\n");
				lessonStr.speedSampleIdx = 0;
				for (lessonStr.speedSampleIdx = 0; lessonStr.speedSampleIdx < LESSON_DATA_LEN; lessonStr.speedSampleIdx++)
				{
					printf("%d\r\n", lessonStr.speedLeftSee[lessonStr.speedSampleIdx]);
				}
				printf("\r\nright:\r\n");
				lessonStr.speedSampleIdx = 0;
				for (lessonStr.speedSampleIdx = 0; lessonStr.speedSampleIdx < LESSON_DATA_LEN; lessonStr.speedSampleIdx++)
				{
					printf("%d\r\n", lessonStr.speedRightSee[lessonStr.speedSampleIdx]);
				}
				LED_Off(0);
				DELAY_Ms(500);
			}
		}
	}
}

/**
 * @brief  实验1：編码器测距实验
 * @param  none
 * @note
 */
void LESSON_Enc2(void)
{
	speedSt.freemode = 1;
	while (1)
	{
		LED_On(1);
		printf("trackAbsL: %f\t", odomStr.trackAbsL);
		printf("trackAbsR: %f\r\n", odomStr.trackAbsR);
		DELAY_Ms(500);
		if (BUTTON1)
		{
			DELAY_Ms(5);
			if (BUTTON1)
			{
				odomStr.trackAbsL = odomStr.trackAbsR = 0;
				odomStr.distLCycle = odomStr.distL = odomStr.distLPrev = odomStr.distRPrev = odomStr.distR = odomStr.distRCycle = 0;
				TIM2->CNT = 0;
				TIM3->CNT = 0;
				LED_On(0);
				DELAY_Ms(1000);
				LED_Off(0);
			}
		}
	}
}

/**
 * @brief  实验3：匀变速直线控制
 * @param  none
 * @note
 */
void LESSON_Pid3(void)
{
	
	lessonStr.speedNeed = 50; 	//固定速度
	lessonStr.allDis = 1000; 	//固定前进距离
	while (1) // 等待进入实验模式
	{
		speedSt.speedTargetL = speedSt.speedTargetR = 0; // 初始化左右轮期望速度
		speedSt.freemode = 1;							 // 电机解锁

		if (BUTTON1) // 按下按键后进入实验模式
		{
			DELAY_Ms(5); // 延时5ms防止误触和抖动

			if (BUTTON1)
			{
				LED_On(0);			  // 亮全部LED灯
				speedSt.freemode = 0; // 电机加锁
				DELAY_Ms(2000);		  // 延时2000ms
				LED_Off(0);			  // 关闭全部LED灯
				odomStr.trackAbsL = odomStr.trackAbsR = 0; 			  // 里程清零, 从发车时刻起算
				odomStr.distLCycle = odomStr.distL = odomStr.distLPrev = odomStr.distRPrev = odomStr.distR = odomStr.distRCycle = 0;
				TIM2->CNT = 0;
				TIM3->CNT = 0;
				lessonStr.isTest = 1; // 实验模式标志位置1
				break;				  // 跳出while(1)
			}
		}
	}
	while (1) // 回传左右轮速度数据
	{
		if (BUTTON1) // 按下按键后进入数据回传模式
		{
			DELAY_Ms(5); // 延时5ms防止误触和抖动

			if (BUTTON1) // 按下按键后回传左右轮速度数据
			{
				LED_On(0); // 亮全部LED灯

				// 蓝牙回传左轮速度数据START//
				printf("\r\nleft:\r\n");
				lessonStr.speedSampleIdx = 0;
				for (lessonStr.speedSampleIdx = 0; lessonStr.speedSampleIdx < LESSON_DATA_LEN; lessonStr.speedSampleIdx++)
				{
					printf("%d\r\n", lessonStr.speedLeftSee[lessonStr.speedSampleIdx]);
				}
				// 蓝牙回传左轮速度数据FINISH//

				// 蓝牙回传右轮速度数据START//
				printf("\r\nright:\r\n");
				lessonStr.speedSampleIdx = 0;
				for (lessonStr.speedSampleIdx = 0; lessonStr.speedSampleIdx < LESSON_DATA_LEN; lessonStr.speedSampleIdx++)
				{
					printf("%d\n", lessonStr.speedRightSee[lessonStr.speedSampleIdx]);
				}
				// 蓝牙回传右轮速度数据FINISH//
				LED_Off(0); // 关闭全部LED灯
				DELAY_Ms(500);
			}
		}
	}
}

/**
 * @brief  实验4：红外测距实验
 * @param  none
 * @note
 */
void LESSON_Irs4(void)
{
	// 实验开始
	lessonStr.speedNeed = 50; 	//固定速度
	lessonStr.allDis = 1000; 	//固定前进距离
	lessonStr.irLeftP = 0.1;
	lessonStr.irRightP = 0.1;
	lessonStr.wallMode = 1;		//靠左边矫正
	speedSt.freemode = 0;
	lessonStr.blockCount = 0;
	odomStr.trackAbsL = odomStr.trackAbsR = 0; 		// 里程清零, 从发车时刻起算
	odomStr.distLCycle = odomStr.distL = odomStr.distLPrev = odomStr.distRPrev = odomStr.distR = odomStr.distRCycle = 0;
	TIM2->CNT = 0;
	TIM3->CNT = 0;
	lessonStr.isTest = 1;
	lessonStr.irSwitch = 1;

	while (1)
	{
		LED_On(3);
		if (irDist.f1 < 30 || irDist.f2 < 30)
		{
			lessonStr.blockCount++;
		}
		else
			lessonStr.blockCount = 0;

		if (lessonStr.blockCount >= 3)
		{
			lessonStr.isTest = 0;
			speedSt.speedTargetL = 0;
			speedSt.speedTargetR = 0;
			LED_Off(3);
			LED_On(1);
		}
		if (lessonStr.isTest == 0)
		{
			break;
		}
	}

	while (1)
	{
		if (BUTTON1)
		{
			DELAY_Ms(5);
			if (BUTTON1)
			{
				LED_On(0);

				printf("\nleft:\n");
				lessonStr.speedSampleIdx = 0;
				for (lessonStr.speedSampleIdx = 0; lessonStr.speedSampleIdx < LESSON_DATA_LEN; lessonStr.speedSampleIdx++)
				{
					printf("%d\n", lessonStr.speedLeftSee[lessonStr.speedSampleIdx]);
				}

				printf("\nright:\n");
				lessonStr.speedSampleIdx = 0;
				for (lessonStr.speedSampleIdx = 0; lessonStr.speedSampleIdx < LESSON_DATA_LEN; lessonStr.speedSampleIdx++)
				{
					printf("%d\n", lessonStr.speedRightSee[lessonStr.speedSampleIdx]);
				}

				LED_Off(0);
			}
		}
	}
}

/**
 * @brief  实验5：标定与校准
 * @param  none
 * @note
 */
void LESSON_Cali5(void)
{
	lessonStr.isTest = 0;
	gyroStr.correctionEnable = 1;
	speedSt.freemode = 1;
	LED_Off(0);

	battMon.enabled = 1;

	while (1)
	{
		LED_On(1);
		printf("bat: %.2f     gyroStr.bias:%d     gyroStr.raw:%d     gyroStr.angle:%d\n", battMon.voltage, gyroStr.bias, gyroStr.raw, gyroStr.angle);
		DELAY_Ms(500);

		if (BUTTON1)
		{
			DELAY_Ms(5);
			if (BUTTON1)
			{
				MOTION_SetAngle(0); // 姿态清零(等价原实验的 ganglet=0)
				LED_On(0);
				DELAY_Ms(1000);
				LED_Off(0);
			}
		}
	}
}

/**
 * @brief  实验6：多传感器校准与直行
 * @param  none
 * @note
 */
void LESSON_Multi6(void)
{
	// 实验开始
	lessonStr.speedNeed = 50; 	//固定速度
	lessonStr.allDis = 1000;
	lessonStr.irLeftP = 0.1;
	lessonStr.irRightP = 0.1;
	lessonStr.wallMode = 1;		//靠左边矫正
	lessonStr.gyroP = 2.5;
	lessonStr.gyroD = 1.6;
	lessonStr.isTest = 0;
	speedSt.freemode = 0;
	lessonStr.blockCount = 0;
	odomStr.trackAbsL = odomStr.trackAbsR = 0; 		// 里程清零, 从发车时刻起算
	odomStr.distLCycle = odomStr.distL = odomStr.distLPrev = odomStr.distRPrev = odomStr.distR = odomStr.distRCycle = 0;
	TIM2->CNT = 0;
	TIM3->CNT = 0;
	lessonStr.isTest = 1;
	lessonStr.irSwitch = 1;
	lessonStr.gyroSwitch = 1;
	// 运行到前方有墙自动停止
	while (1)
	{
		LED_On(3);
		if (irDist.f1 < 30 || irDist.f2 < 30)
		{
			lessonStr.blockCount++;
		}
		else
			lessonStr.blockCount = 0;

		if (lessonStr.blockCount >= 3)
		{
			lessonStr.isTest = 0;
			speedSt.speedTargetL = 0;
			speedSt.speedTargetR = 0;
			LED_Off(3);
			LED_On(1);
		}

		if (lessonStr.isTest == 0)
		{
			break;
		}
	}

	// 回传电脑鼠左右轮速度数据
	while (1)
	{
		if (BUTTON1)
		{
			DELAY_Ms(5);
			if (BUTTON1)
			{
				// CHULIHANSHU

				LED_On(0);

				printf("\nleft:\n");
				lessonStr.speedSampleIdx = 0;
				for (lessonStr.speedSampleIdx = 0; lessonStr.speedSampleIdx < LESSON_DATA_LEN; lessonStr.speedSampleIdx++)
				{
					printf("%d\n", lessonStr.speedLeftSee[lessonStr.speedSampleIdx]);
				}

				printf("\nright:\n");
				lessonStr.speedSampleIdx = 0;
				for (lessonStr.speedSampleIdx = 0; lessonStr.speedSampleIdx < LESSON_DATA_LEN; lessonStr.speedSampleIdx++)
				{
					printf("%d\n", lessonStr.speedRightSee[lessonStr.speedSampleIdx]);
				}

				LED_Off(0);

				break;
			}
			else
				continue;
		}
	}
}

/**
 * @brief  实验7：转向控制实验
 * @param  none
 * @note
 */
void LESSON_Turn7(void)
{
	// 前往floodfill.c 更改搜索转向相关数组
	// const short vouterT90Low[195]; 
	// const short vinnerT90Low[195];
	// const short gyroLt90Low[195];
	// const short gyroRt90Low[195];
	
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

		default:
			speedSt.freemode = 1;
			break;
		}
	}

}

/**
 * @brief  实验8：无记忆走迷宫
 * @param  none
 * @note
 */
void LESSON_Blind8(void)
{
	flashSt.flashenable = 0;
	irCalib.calibMode = 0;
	MOTION_SetAngle(0);
	taskSt.backSearch = 1;
	speedSt.freemode = 0;
	mazeState.mouseTask = WAIT;
	while (1)
	{
		switch (mazeState.mouseTask)
		{
		// 等待状态
		case WAIT:
			// 启动电脑鼠，将状态转换为开始
			LED_Off(0);
			LED_On(1);
			searchSt.accdistance = 40;
			MOTION_Acc();
			MOTION_SetTrack(55 + searchSt.accdistance);
			mazeState.mouseTask = START;
			break;

		// 开始状态
		case START:
			LED_Off(0);
			LED_On(2);

			// 开始搜索迷宫
			STATE_MazeSearch();

			// 根据迷宫第一个路口判断发车位置（↙ or ↗）
			// 右上角发车需要转换坐标，将状态转换为搜索
			if (mazeState.mapWall[mazeState.mousePos.cX][mazeState.mousePos.cY] & 0x08)
			{
				mazeState.xStart = MAZETYPE - 1;
				mazeState.mousePos.cX = MAZETYPE - 1;
				if (mazeState.mousePos.cY < MAZETYPE - 1)
				{
					mazeState.mapWall[MAZETYPE - 1][mazeState.mousePos.cY + 1] = mazeState.mapWall[0][mazeState.mousePos.cY + 1];
					mazeState.mapWall[0][mazeState.mousePos.cY + 1] = 0xf0;
				}
				lessonStr.tempPos = mazeState.mousePos.cY;
				do
				{
					mazeState.mapWall[MAZETYPE - 1][lessonStr.tempPos] = mazeState.mapWall[0][lessonStr.tempPos];
					mazeState.mapWall[MAZETYPE - 2][lessonStr.tempPos] = 0xD0;
					mazeState.mapWall[0][lessonStr.tempPos] = 0xf0;
					mazeState.mapWall[1][lessonStr.tempPos] = 0xf0;
				} while (lessonStr.tempPos--);
				mazeState.mapWall[MAZETYPE - 2][mazeState.mousePos.cY] = 0xf0;
				mazeState.goalX = 8;

				mazeState.crosswayBuf[mazeState.stackIdx].cX = MAZETYPE - 1;
				mazeState.crosswayBuf[mazeState.stackIdx].cY = 0;
				mazeState.stackIdx++;
				mazeState.mouseTask = MAZESEARCH;
			}

			// 左下角发车，将状态转换为搜索
			else if (mazeState.mapWall[mazeState.mousePos.cX][mazeState.mousePos.cY] & 0x02)
			{

				mazeState.crosswayBuf[mazeState.stackIdx].cX = 0;
				mazeState.crosswayBuf[mazeState.stackIdx].cY = 0;
				mazeState.stackIdx++;
				mazeState.mouseTask = MAZESEARCH;
			}
			break;

		// 搜索状态（采用右手法则）
		case MAZESEARCH:
			LED_Off(0);
			LED_On(3);

			lessonStr.roadStat = 0;
			// 统计岔路口数量

			if (wallSns.left == 0)
			{
				LED_Off(4);
				lessonStr.roadStat++;
			}
			else
			{
				LED_On(4);
			}
			if (wallSns.right == 0)
			{
				LED_Off(1);
				lessonStr.roadStat++;
			}
			else
			{
				LED_On(1);
			}

			if (wallSns.front == 0)
			{
				LED_Off(2);
				LED_Off(3);
				lessonStr.roadStat++;
			}
			else
			{
				LED_On(2);
				LED_On(3);
			}

			// 有未经过的岔路口则根据搜索法则选择前进方向
			if (lessonStr.roadStat)
			{
				EXPLORE_CrosswayChoice();
				STATE_MazeSearch();
			}
			// 无岔路口则返回
			else
				MOTION_MouseTurnBack();

			break;

		default:
			break;
		}
	}
}
