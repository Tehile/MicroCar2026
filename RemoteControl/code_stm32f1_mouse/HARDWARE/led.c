/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: led.c —— LED指示驱动与坐标显示
*********************************************************************************************************
*/

/**
 * @brief 用8个LED显示电脑鼠坐标
 * @param none
 * @note 1~4灯显示X坐标, 5~8灯显示Y坐标(按位点亮); 显示前全部熄灭
 */
#include "led.h"
void LED_ShowMouseXY()
{
	LED_Off(0);
	switch (mazeState.mousePos.cX)
	{
	case 0:
		break;
	case 1:
		LED_On(4);
		break;
	case 2:
		LED_On(3);
		break;
	case 3:
		LED_On(3);
		LED_On(4);
		break;
	case 4:
		LED_On(2);
		break;
	case 5:
		LED_On(2);
		LED_On(4);
		break;
	case 6:
		LED_On(2);
		LED_On(3);
		break;
	case 7:
		LED_On(2);
		LED_On(3);
		LED_On(4);
		break;
	case 8:
		LED_On(1);
		break;
	case 9:
		LED_On(1);
		LED_On(4);
		break;
	case 10:
		LED_On(1);
		LED_On(3);
		break;
	case 11:
		LED_On(1);
		LED_On(3);
		LED_On(4);
		break;
	case 12:
		LED_On(1);
		LED_On(2);
		break;
	case 13:
		LED_On(1);
		LED_On(2);
		LED_On(4);
		break;
	case 14:
		LED_On(1);
		LED_On(2);
		LED_On(3);
		break;
	case 15:
		LED_On(1);
		LED_On(2);
		LED_On(3);
		LED_On(4);
		break;
	default:
		break;
	}
	switch (mazeState.mousePos.cY)
	{
	case 0:
		break;
	case 1:
		LED_On(8);
		break;
	case 2:
		LED_On(7);
		break;
	case 3:
		LED_On(7);
		LED_On(8);
		break;
	case 4:
		LED_On(6);
		break;
	case 5:
		LED_On(6);
		LED_On(8);
		break;
	case 6:
		LED_On(6);
		LED_On(7);
		break;
	case 7:
		LED_On(6);
		LED_On(7);
		LED_On(8);
		break;
	case 8:
		LED_On(5);
		break;
	case 9:
		LED_On(5);
		LED_On(8);
		break;
	case 10:
		LED_On(5);
		LED_On(7);
		break;
	case 11:
		LED_On(5);
		LED_On(7);
		LED_On(8);
		break;
	case 12:
		LED_On(5);
		LED_On(6);
		break;
	case 13:
		LED_On(5);
		LED_On(6);
		LED_On(8);
		break;
	case 14:
		LED_On(5);
		LED_On(6);
		LED_On(7);
		break;
	case 15:
		LED_On(5);
		LED_On(6);
		LED_On(7);
		LED_On(8);
		break;
	default:
		break;
	}
}

/**
 * @brief LED 引脚初始化: PA8/PC7/PC8/PC9 推挽输出, PC10 输入
 * @param none
 */
/* 时间基准灯锁定标志(默认 0 = 不锁定)。仅数据收集档在发车后置 1, 见 led.h。 */
u8 ledBeaconLock = 0;

void LED_Init()
{
	RCC->APB2ENR |= 1 << 2; // 使能PORTA时钟
	GPIOA->CRH &= 0XFFFFFFF0;
	GPIOA->CRH |= 0X00000003; // PA8推挽输出

	// RCC->APB2ENR|=1<<3;	   //PORTB时钟使能
	// GPIOB->CRL&=0XFFFFFFF0;
	// GPIOB->CRL|=0X00000003;//PB0推挽输出
	// GPIOB->CRH&=0XFFFF00FF;
	// GPIOB->CRH|=0X00003300;//PB10,11推挽输出

	RCC->APB2ENR |= 1 << 4; // 使能PORTC口时钟
	GPIOC->CRL &= 0XFFFFFFFF;
	GPIOC->CRL |= 0X30000000; // PC7推挽输出
	GPIOC->CRH &= 0XFFFFFF00;
	GPIOC->CRH |= 0X00000033; // PC8,9推挽输出

	GPIOC->CRH &= 0XFFFFF0FF;
	GPIOC->CRH |= 0X00000800; // PC10输入

	LED_Off(0);
}

/**
 * @brief 点亮指定 LED
 * @param n LED编号 1~8; 0=全部点亮
 * @note LED 为低电平点亮
 * @note 时间基准灯锁定期间(数据收集档发车后)直接返回, 见 led.h 的 ledBeaconLock
 */
void LED_On(u8 n)
{
	if (ledBeaconLock)
		return; /* 时间基准灯独占: 不让状态灯污染 1Hz 方波 */
	switch (n)
	{
	case 1:
		LED1 = 0;
		break;
	case 2:
		LED2 = 0;
		break;
	case 3:
		LED3 = 0;
		break;
	case 4:
		LED4 = 0;
		break;
	case 5:
		LED5 = 0;
		break;
	case 6:
		LED6 = 0;
		break;
	case 7:
		LED7 = 0;
		break;
	case 8:
		LED8 = 0;
		break;
	case 0:
		LED1 = 0;
		LED2 = 0;
		LED3 = 0;
		LED4 = 0;
		LED5 = 0;
		LED6 = 0;
		LED7 = 0;
		LED8 = 0;
		break;
	default:
		break;
	}
}

/**
 * @brief 熄灭指定 LED
 * @param n LED编号 1~8; 0=全部熄灭
 * @note LED 低电平点亮, 熄灭即输出高电平
 */
void LED_Off(u8 n)
{
	if (ledBeaconLock)
		return; /* 时间基准灯独占: 不让状态灯污染 1Hz 方波 */
	switch (n)
	{
	case 1:
		LED1 = 1;
		break;
	case 2:
		LED2 = 1;
		break;
	case 3:
		LED3 = 1;
		break;
	case 4:
		LED4 = 1;
		break;
	case 5:
		LED5 = 1;
		break;
	case 6:
		LED6 = 1;
		break;
	case 7:
		LED7 = 1;
		break;
	case 8:
		LED8 = 1;
		break;
	case 0:
		LED1 = 1;
		LED2 = 1;
		LED3 = 1;
		LED4 = 1;
		LED5 = 1;
		LED6 = 1;
		LED7 = 1;
		LED8 = 1;
		break;
	default:
		break;
	}
}

/**
 * @brief 用 ASCII 字符打印迷宫墙地图
 * @param none
 * @note $ 标记起点与终点(终点格输出坐标), 行首字符表示左侧墙
 */
void LED_ShowBlock()
{
	u8 blockx, blocky;
	blockx = 0;
	blocky = 0;
	blocky = 15;
	printf("+");
	for (blockx = 0; blockx < 16; blockx++)
	{
		if (mazeState.mapWall[blockx][blocky] & 0x01)
			printf("   +");
		else
			printf("---+");
	}
	printf("\r\n");

	for (blocky = 16; blocky > 0;)
	{
		blocky--;
		if (mazeState.mapWall[0][blocky] & 0x08)
			printf(" ");
		else
			printf("|");
		for (blockx = 0; blockx < 16; blockx++)
		{
			if (((blockx == mazeState.xStart) && (blocky == 0)) || ((blockx == 7 || blockx == 8) && (blocky == 7 || blocky == 8)))
			{
				if ((blockx == mazeState.goalX) && (blocky == mazeState.goalY))
				{
					if (mazeState.mapWall[blockx][blocky] & 0x02)
						printf("%d,%d ", blockx, blocky);
					else
						printf("%d,%d|", blockx, blocky);
				}
				else if (mazeState.mapWall[blockx][blocky] & 0x02)
					printf(" $  ");
				else
					printf(" $ |");
			}
			else if (mazeState.mapWall[blockx][blocky] & 0x02)
				printf("    ");
			else
				printf("   |");
		}
		printf("\r\n");
		printf("+");
		for (blockx = 0; blockx < 16; blockx++)
		{
			if (mazeState.mapWall[blockx][blocky] & 0x04)
				printf("   +");
			else
				printf("---+");
		}
		printf("\r\n");
	}
	printf("(%d,0) Is The Start_Point", mazeState.xStart);
	printf("\r\n");
}
