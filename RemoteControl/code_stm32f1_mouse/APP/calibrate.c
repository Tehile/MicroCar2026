/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: calibrate.c —— 红外标定与传感器调试菜单
*********************************************************************************************************
*/

#include "calibrate.h"
#define FLASHSAVE

/**
 * @brief  应用红外校准数据到传感器校正曲线
 * @param  none
 * @note   上电即调用一次(先于菜单), 使传感器读数不受是否进入标定菜单影响;
 *         Flash 中无有效标定值(0xFFFF)时使用内置默认近/远距离电压
 */
void CALIB_InitIr(void)
{
	u16 flashreadtemp;
	flashreadtemp = FLASH_ReadHalfWord(FLASH_ADDR_IRDATA);
	if (flashreadtemp != 65535)
	{
		flashaddr = 0;
		irCalib.rawf1avrgnear = FLASH_ReadHalfWord(FLASH_ADDR_IRDATA + flashaddr);
		flashaddr += 2;
		irCalib.rawf1avrgfar = FLASH_ReadHalfWord(FLASH_ADDR_IRDATA + flashaddr);
		flashaddr += 2;
		irCalib.rawf2avrgnear = FLASH_ReadHalfWord(FLASH_ADDR_IRDATA + flashaddr);
		flashaddr += 2;
		irCalib.rawf2avrgfar = FLASH_ReadHalfWord(FLASH_ADDR_IRDATA + flashaddr);
		flashaddr += 2;
		irCalib.rawlavrgnear = FLASH_ReadHalfWord(FLASH_ADDR_IRDATA + flashaddr);
		flashaddr += 2;
		irCalib.rawlavrgfar = FLASH_ReadHalfWord(FLASH_ADDR_IRDATA + flashaddr);
		flashaddr += 2;
		irCalib.rawravrgnear = FLASH_ReadHalfWord(FLASH_ADDR_IRDATA + flashaddr);
		flashaddr += 2;
		irCalib.rawravrgfar = FLASH_ReadHalfWord(FLASH_ADDR_IRDATA + flashaddr);
		flashaddr += 2;

		IR_InfCorrect(irCalib.rawlavrgnear, 33, irCalib.rawlavrgfar, 94, 250, datal, L);
		IR_InfCorrect(irCalib.rawravrgnear, 33, irCalib.rawravrgfar, 95, 250, datar, R);
		IR_InfCorrect(irCalib.rawf1avrgnear, 32, irCalib.rawf1avrgfar, 213, 250, dataf1, F1); // 31-214
		IR_InfCorrect(irCalib.rawf2avrgnear, 32, irCalib.rawf2avrgfar, 213, 250, dataf2, F2);

		LED_On(8);
		DELAY_Ms(120);
		LED_Off(0);
		DELAY_Ms(120);
		LED_On(8);
		DELAY_Ms(120);
		LED_Off(0);
		DELAY_Ms(120);
	}
	else
	{
		IR_InfCorrect(1450, 33, 371, 94, 250, datal, L);
		IR_InfCorrect(1439, 35, 366, 94, 250, datar, R);
		IR_InfCorrect(1880, 32, 145, 212, 250, dataf1, F1); // 31-214
		IR_InfCorrect(2166, 32, 142, 212, 250, dataf2, F2);
	}

	speedSt.freemode = 1;
#ifdef FLASHSAVE
	FLASH_EraseRunData();
#endif

	FLASH_Unlock();
	FLASH_ErasePage(FLASH_ADDR_BLOCK);
	FLASH_Unlock();
	FLASH_ErasePage(FLASH_ADDR_BLOCK_POINT);
	FLASH_Unlock();
	FLASH_ErasePage(FLASH_ADDR_GOPATH);
	FLASH_Unlock();
	FLASH_ErasePage(FLASH_ADDR_BACKPATH);
	FLASH_Unlock();
	FLASH_ErasePage(FLASH_ADDR_GOPATH_90);
	FLASH_Unlock();
	FLASH_ErasePage(FLASH_ADDR_BACKPATH_90);
	speedSt.freemode = 0;
}

/**
 * @brief  菜单功能2: 擦除红外校准数据
 * @param  none
 * @note   擦除后进入 LED 流水灯死循环, 不返回, 靠电源复位退出
 */
void CALIB_MenuEraseIrData(void)
{
	u16 tmptt;
	u8 c = 0;
	speedSt.freemode = 1;
	flashaddr = 0;
	for (tmptt = 0; tmptt < 8; tmptt++)
	{
		FLASH_Unlock();
		FLASH_ErasePage(FLASH_ADDR_IRDATA + flashaddr);
		flashaddr += 2;
	}
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

/**
 * @brief  菜单功能3: 自动校准陀螺仪零点
 * @param  none
 * @note   置 adjFlag 启动 ISR 内零点校准, 采样 adjCounter>1500 后将 bias 写入
 *         Flash, 随后进入流水灯死循环不返回
 */
void CALIB_MenuCalibGyro(void)
{
	u8 c = 0;
	gyroStr.adjCounter = 0;
	speedSt.freemode = 1;
	gyroStr.adjFlag = 1;
	while (1)
	{
		if (gyroStr.angle >= 0)
		{
			LED_Off(0);
			LED_On(gyroStr.angle % 8 + 1);
		}
		else if (gyroStr.angle < 0)
		{
			LED_Off(0);
			LED_On(gyroStr.angle % 8 + 8);
		}

		if (gyroStr.adjCounter > 1500)
			break;
	}
	gyroStr.adjFlag = 0;
	FLASH_Unlock();
	FLASH_ErasePage(FLASH_ADDR_GYRO0);
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_GYRO0, gyroStr.bias);
	speedSt.sleepmode = 1;
	LED_On(0);
	DELAY_Ms(100);
	LED_Off(0);
	c = 1;

	while (1)
	{
		printf("GYRO0:%d,", gyroStr.bias);
		if (c == 9 || c == 0)
			c = 1;
		LED_Off(0);
		LED_On(c);
		DELAY_Ms(500);
		c++;
	}
}

/**
 * @brief  菜单功能4: 红外线自检
 * @param  none
 * @note   依次自检左/右红外: 距离偏差映射 LED 亮灯数(1~8), 按键确认后进入
 *         前向红外 FORWARDIRTEST 测试, 最后流水灯死循环不返回
 */
void CALIB_MenuIrSelfTest(void)
{
	u8 c = 0;
	int irselftestdiff; /* 距离偏差(映射 LED 档位) */
	speedSt.freemode = 1;

	while (1)
	{
		irselftestdiff = irDist.l - sysCfg.distToWall + 4;
		if (irselftestdiff > 8)
			irselftestdiff = 8;
		if (irselftestdiff < 1)
			irselftestdiff = 1;
		LED_Off(0);
		LED_On(irselftestdiff);
		if (BUTTON1 == 1) // 键被按下，停止倒计时。
		{
			DELAY_Ms(5); // 消抖
			if (BUTTON1 == 1)
				break;
		}
		printf("irDist.l:%d,irDist.r:%d \r\n", irDist.l, irDist.r);
		DELAY_Ms(100);
	}
	LED_On(0);
	DELAY_Ms(2000);
	LED_Off(0);
	while (1)
	{
		irselftestdiff = irDist.r - sysCfg.distToWall + 4;
		if (irselftestdiff > 8)
			irselftestdiff = 8;
		if (irselftestdiff < 1)
			irselftestdiff = 1;
		LED_Off(0);
		LED_On(irselftestdiff);
		if (BUTTON1 == 1)
		{
			DELAY_Ms(5);
			if (BUTTON1 == 1)
				break;
		}
		printf("irDist.l:%d,irDist.r:%d \r\n", irDist.l, irDist.r);
		DELAY_Ms(100);
	}

	LED_Off(0);
	LED_On(4);
	DELAY_Ms(1000);
	LED_Off(0);
	LED_On(3);
	DELAY_Ms(1000);
	LED_Off(0);
	LED_On(2);
	DELAY_Ms(1000);
	LED_Off(0);
	LED_On(1);
	DELAY_Ms(1000);

	speedSt.freemode = 0;
	motionCtrl.task = FORWARDIRTEST;
	speedCfg.forwardIr = 100;
	DELAY_Ms(5000);
	speedCfg.forwardIr = 50;
	DELAY_Ms(5000);
	speedCfg.forwardIr = 17;
	DELAY_Ms(5000);
	motionCtrl.task = NULL;
	while (1)
	{
		speedSt.freemode = 1;
		if (c == 9 || c == 0)
			c = 1;
		LED_Off(0);
		LED_On(c);
		DELAY_Ms(1000);
		c++;
	}
}

/**
 * @brief  菜单功能5: 显示传感器数据
 * @param  none
 * @note   分两屏打印编码器/红外/陀螺仪读数, 按键切换, 最后流水灯死循环不返回
 */
void CALIB_MenuShowSensor(void)
{
	u8 c = 0;
	wallSns.irSwitch = 1;
	speedSt.freemode = 1;

	gyroStr.correctionEnable = 1;

	while (1)
	{
		printf("\n");
		printf("odomStr.trackAbsL:%f\t", odomStr.trackAbsL);
		printf("odomStr.trackAbsR:%f", odomStr.trackAbsR);
		if (BUTTON1 == 1)
		{
			DELAY_Ms(5);
			if (BUTTON1)
				break;
		}
		DELAY_Ms(300);
	}
	LED_On(0);
	DELAY_Ms(1000);
	LED_Off(0);
	while (1)
	{
		printf("adcf1:%d\t adcf2:%d\t adcl:%d\t adcr:%d\t \r\ngangle_Rate:%d\t  gyroStr.angle:%d\t  angle_code:%ld \r\n", irCalib.rawf1, irCalib.rawf2, irCalib.rawl, irCalib.rawr, gyroStr.raw, gyroStr.angle, odomStr.angle);
		if (BUTTON1 == 1)
		{
			DELAY_Ms(5);
			if (BUTTON1)
				break;
		}
		DELAY_Ms(300);
	}

	while (1)
	{
		speedSt.sleepmode = 1; // 关闭时钟周期的全部函数，节约电量
		if (c == 9 || c == 0)
			c = 1;
		LED_Off(0);
		LED_On(c);
		DELAY_Ms(1000);
		c++;
	}
}

/**
 * @brief  菜单功能6: 计算红外线传感器数据(标定)
 * @param  none
 * @note   按 LED 提示分 4 步采样近/远墙电压: 左右近墙(1/2)→左右远墙(7/8)→
 *         前近(4/5)→前远(3/6), 每步按键确认, 完成后写入 Flash 并经流水灯结束
 */
void CALIB_MenuCalcIr(void)
{
	u16 tmptt;
	u16 flashreadtemp;
	u8 c = 0;
	speedSt.freemode = 1;
	flashreadtemp = FLASH_ReadHalfWord(FLASH_ADDR_IRDATA);
	if (flashreadtemp != 65535)
	{
		flashaddr = 0;
		irCalib.rawf1avrgnear = FLASH_ReadHalfWord(FLASH_ADDR_IRDATA + flashaddr);
		flashaddr += 2;
		irCalib.rawf1avrgfar = FLASH_ReadHalfWord(FLASH_ADDR_IRDATA + flashaddr);
		flashaddr += 2;
		irCalib.rawf2avrgnear = FLASH_ReadHalfWord(FLASH_ADDR_IRDATA + flashaddr);
		flashaddr += 2;
		irCalib.rawf2avrgfar = FLASH_ReadHalfWord(FLASH_ADDR_IRDATA + flashaddr);
		flashaddr += 2;
		irCalib.rawlavrgnear = FLASH_ReadHalfWord(FLASH_ADDR_IRDATA + flashaddr);
		flashaddr += 2;
		irCalib.rawlavrgfar = FLASH_ReadHalfWord(FLASH_ADDR_IRDATA + flashaddr);
		flashaddr += 2;
		irCalib.rawravrgnear = FLASH_ReadHalfWord(FLASH_ADDR_IRDATA + flashaddr);
		flashaddr += 2;
		irCalib.rawravrgfar = FLASH_ReadHalfWord(FLASH_ADDR_IRDATA + flashaddr);
		flashaddr += 2;

		printf("rawLFavrgnear:%d,rawLFavrgfar:%d \r\n", irCalib.rawf1avrgnear, irCalib.rawf1avrgfar);
		printf("rawLavrgnear:%d,rawLavrgfar:%d \r\n", irCalib.rawlavrgnear, irCalib.rawlavrgfar);
		printf("rawRavrgnear:%d,rawRavrgfar:%d \r\n", irCalib.rawravrgnear, irCalib.rawravrgfar);
		printf("rawRFavrgnear:%d,rawRFavrgnear:%d \r\n", irCalib.rawf2avrgnear, irCalib.rawf2avrgfar);

		while (1)
		{
			speedSt.sleepmode = 1; // 关闭时钟周期的全部函数，节约电量
			if (c == 9 || c == 0)
				c = 1;
			LED_Off(0);
			LED_On(c);
			DELAY_Ms(1000);
			c++;
		}
	}

	irCalib.calibMode = 1;
	LED_Off(0);
	while (1)
	{
		LED_On(1);
		LED_On(2);
		if (BUTTON1 == 1)
		{
			DELAY_Ms(5); // 消抖
			if (BUTTON1 == 1)
				break;
		}
	}
	LED_Off(0);
	LED_On(4);
	DELAY_Ms(1000);
	LED_Off(0);
	LED_On(3);
	DELAY_Ms(1000);
	LED_Off(0);
	LED_On(2);
	DELAY_Ms(1000);
	LED_Off(0);
	LED_On(1);
	DELAY_Ms(1000);
	/* 步骤1: 左近墙采样(1/2 灯提示) */
	irCalib.rawlavrgnear = irCalib.rawl; /* 左传感器近墙电压 */
	irCalib.rawravrgfar = irCalib.rawr;

	LED_Off(0);
	DELAY_Ms(500);
	while (1)
	{
		LED_On(7);
		LED_On(8);
		if (BUTTON1 == 1)
		{
			DELAY_Ms(5);
			if (BUTTON1 == 1)
				break;
		}
	}
	LED_Off(0);
	LED_On(4);
	DELAY_Ms(1000);
	LED_Off(0);
	LED_On(3);
	DELAY_Ms(1000);
	LED_Off(0);
	LED_On(2);
	DELAY_Ms(1000);
	LED_Off(0);
	LED_On(1);
	DELAY_Ms(1000);
	/* 步骤2: 左远墙采样(7/8 灯提示) */
	irCalib.rawlavrgfar = irCalib.rawl;
	irCalib.rawravrgnear = irCalib.rawr;

	LED_Off(0);
	DELAY_Ms(500);
	while (1)
	{
		LED_On(4);
		LED_On(5);
		if (BUTTON1 == 1)
		{
			DELAY_Ms(5);
			if (BUTTON1 == 1)
				break;
		}
	}
	LED_Off(0);
	LED_On(4);
	DELAY_Ms(1000);
	LED_Off(0);
	LED_On(3);
	DELAY_Ms(1000);
	LED_Off(0);
	LED_On(2);
	DELAY_Ms(1000);
	LED_Off(0);
	LED_On(1);
	DELAY_Ms(1000);
	/* 步骤3: 前传感器近墙采样(4/5 灯提示) */
	irCalib.rawf1avrgnear = irCalib.rawf1;
	irCalib.rawf2avrgnear = irCalib.rawf2;

	LED_Off(0);
	DELAY_Ms(500);
	while (1)
	{
		LED_On(3);
		LED_On(6);
		if (BUTTON1 == 1)
		{
			DELAY_Ms(5);
			if (BUTTON1 == 1)
				break;
		}
	}
	LED_Off(0);
	LED_On(4);
	DELAY_Ms(1000);
	LED_Off(0);
	LED_On(3);
	DELAY_Ms(1000);
	LED_Off(0);
	LED_On(2);
	DELAY_Ms(1000);
	LED_Off(0);
	LED_On(1);
	DELAY_Ms(1000);
	/* 步骤4: 前传感器远墙采样(3/6 灯提示) */
	irCalib.rawf1avrgfar = irCalib.rawf1;
	irCalib.rawf2avrgfar = irCalib.rawf2;

	flashaddr = 0;
	for (tmptt = 0; tmptt < 8; tmptt++)
	{
		FLASH_Unlock();
		FLASH_ErasePage(FLASH_ADDR_IRDATA + flashaddr);
		flashaddr += 2;
	}
	flashaddr = 0;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_IRDATA + flashaddr, irCalib.rawf1avrgnear);
	flashaddr += 2;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_IRDATA + flashaddr, irCalib.rawf1avrgfar);
	flashaddr += 2;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_IRDATA + flashaddr, irCalib.rawf2avrgnear);
	flashaddr += 2;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_IRDATA + flashaddr, irCalib.rawf2avrgfar);
	flashaddr += 2;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_IRDATA + flashaddr, irCalib.rawlavrgnear);
	flashaddr += 2;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_IRDATA + flashaddr, irCalib.rawlavrgfar);
	flashaddr += 2;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_IRDATA + flashaddr, irCalib.rawravrgnear);
	flashaddr += 2;
	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_IRDATA + flashaddr, irCalib.rawravrgfar);
	flashaddr += 2;

	LED_On(0);
	DELAY_Ms(300);
	LED_Off(0);
	DELAY_Ms(300);
	irCalib.calibMode = 0;
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

/**
 * @brief  菜单功能7: 打印迷宫墙/路径/红外数据
 * @param  none
 * @note   从 Flash 载入墙图/起终点, 依次打印墙图、45/90 去回路径、红外标定值与
 *         dataf1/f2/l/r 标定表, 最后流水灯死循环不返回
 */
void CALIB_MenuShowBlockData(void)
{
	u16 tmptt;
	u16 flashreadtemp;
	u8 c = 0, blockx, blocky; /* 迷宫行列循环变量 */
	speedSt.freemode = 1;
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

	printf("block picture:\r\n");
	LED_ShowBlock();

	printf("block data:\r\n");
	for (blockx = 0; blockx < 16; blockx++)
	{
		printf("{\r\n");
		for (blocky = 0; blocky < 16; blocky++)
			printf("%d,", mazeState.mapWall[blockx][blocky]);
		printf("},\r\n");
	}

	printf("\r\n");
	printf("45 SPURT_GainSet go path: ");
	flashaddr = 0;
	for (tmptt = 0; tmptt < 200; tmptt++)
	{
		mazeState.fastwayAction[tmptt].action = FLASH_ReadHalfWord(FLASH_ADDR_GOPATH + flashaddr);
		flashaddr += 2;
		printf("%d,", mazeState.fastwayAction[tmptt].action);
	}
	printf("        \r\nback path: ");
	flashaddr = 0;
	for (tmptt = 0; tmptt < 200; tmptt++)
	{
		mazeState.fastwayAction[tmptt].action = FLASH_ReadHalfWord(FLASH_ADDR_BACKPATH + flashaddr);
		flashaddr += 2;
		printf("%d,", mazeState.fastwayAction[tmptt].action);
	}
	printf("\r\n");
	printf("90 SPURT_GainSet go path: ");
	flashaddr = 0;
	for (tmptt = 0; tmptt < 200; tmptt++)
	{
		mazeState.fastwayAction[tmptt].action = FLASH_ReadHalfWord(FLASH_ADDR_GOPATH_90 + flashaddr);
		flashaddr += 2;
		printf("%d ", mazeState.fastwayAction[tmptt].action);
	}
	printf("        \r\nback path: ");
	flashaddr = 0;
	for (tmptt = 0; tmptt < 200; tmptt++)
	{
		mazeState.fastwayAction[tmptt].action = FLASH_ReadHalfWord(FLASH_ADDR_BACKPATH_90 + flashaddr);
		flashaddr += 2;
		printf("%d ", mazeState.fastwayAction[tmptt].action);
	}

	printf("       \r\nIR DATA: ");
	flashaddr = 0;
	for (tmptt = 0; tmptt < 8; tmptt++)
	{
		flashreadtemp = FLASH_ReadHalfWord(FLASH_ADDR_IRDATA + flashaddr);
		flashaddr += 2;
		printf("%d,", flashreadtemp);
	}
	printf("\r\nu16 dataf1[250]={");
	for (tmptt = 0; tmptt < 249; tmptt++)
		printf("%d,", dataf1[tmptt]);
	printf("0};");
	printf("\r\nu16 dataf2[250]={");
	for (tmptt = 0; tmptt < 249; tmptt++)
		printf("%d,", dataf2[tmptt]);
	printf("0};");
	printf("\r\nu16 datal[250]={");
	for (tmptt = 0; tmptt < 249; tmptt++)
		printf("%d,", datal[tmptt]);
	printf("0};");
	printf("\r\nu16 datar[250]={");
	for (tmptt = 0; tmptt < 249; tmptt++)
		printf("%d,", datar[tmptt]);
	printf("0};");
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
