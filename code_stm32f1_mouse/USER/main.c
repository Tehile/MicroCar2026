/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: main.c —— 主程序入口
*********************************************************************************************************
*/

#include "main.h"
#define FLASHSAVE // 运行数据存入Flash
TaskState taskSt; /* 初值由 TASK_Init 逐字段赋值 */

u16 flashaddr = 0;
/* Flash 读写偏移地址(各模块共享) */

/**
 * @brief  任务状态逐字段赋值
 * @param  none
 * @note   初值: backSearch=3, 其余字段为 0
 */
static void TASK_Init(void)
{
	taskSt.goaheadnum = 0;
	taskSt.backSearch = 3;
	taskSt.spurtFlag = 0;
	taskSt.goOrBack = 0;
}

/**
 * @brief  电脑鼠应用初始化
 * @param  none
 * @note   依次初始化迷宫/搜索/冲刺/任务状态
 */
void MOUSE_Init()
{
	MAZE_Init();   /* 迷宫状态初始化 */
	SEARCH_Init(); /* 搜索运动状态 */
	SPURT_Init();  /* 冲刺速度/比例配置 */
	TASK_Init();	   /* 任务状态 */
}

/**
 * @brief  硬件初始化
 * @param  none
 * @note   ISR_Init 须置于所有外设初始化之后(先开中断而外设未就绪会异常:
 *         SysTick 1ms 中断内 IR_GetDistance 依赖 TIM1/ADC/编码器)
 */
void HARDWARE_Init()
{
	SYS_ClockInit();
	USART_Init();
	DELAY_Init(); /* TIM1 延时基准, 须早于任何 DELAY_Us 使用者 */
	ADC_Init();
	IR_Init();
	MOTOR_PwmInit();
	LED_Init();
	MOTOR_EncLeftInit();  /* 左轮编码器 TIM2 */
	MOTOR_EncRightInit(); /* 右轮编码器 TIM3 */
	CORE_Init(); /* 速度/PID增益/墙检测配置(ISR 每周期读, 须在 ISR_Init 前) */
	ISR_Init();	 /* ← 最后开启 SysTick 控制中断(全部外设已就绪) */
	GYRO_Init(); /* 陀螺零点校准(须在 ISR_Init 之后, 依赖 ISR 递增 gyroStr.adjCounter) */
}

/**
 * @brief  主函数: 启动流程编排
 * @param  none
 * @note   顺序: 硬件初始化→鼠应用初始化→红外标定载入→按键菜单→读图选速→
 *         冲刺方向判定→(可选)冲刺调试→进入主状态机(不返回)
 */
int main()
{
	HARDWARE_Init();				//硬件初始化
	MOUSE_Init();					//任务初始化
	CALIB_InitIr();					//红外初始化
	MENU_Run();						//按键选择
	
	// LESSON_Motor1(); // 实验：电机驱动实验
	// LESSON_Enc2(); // 实验：编码器测距实验
	// LESSON_Pid3(); // 实验：匀变速直线控制
	LESSON_Cali5(); // 实验：标定与校准
	
	
	STATE_LoadMapAndSelectSpeed();	//读取地图信息
	STATE_DetermineGoaheadNum();	//发车判断（90冲刺或45冲刺）
	flashSt.flashenable = 0;
	wallSns.irSwitch = 1;
	irCalib.calibMode = 0; 			// 关闭红外标定模式
	MOTION_SetAngle(0);				//设定状态初值
	taskSt.backSearch = 1;			
	
	
	// LESSON_Irs4(); // 实验：红外测距实验
	// LESSON_Multi6(); // 实验：多传感器校准与直行
	// LESSON_Turn7(); // 实验：转向控制实验
	// LESSON_Blind8(); // 实验：无记忆走迷宫
	
	
	if (mazeState.spurtTestFlag == 1)
		STATE_SpurtTest();
	STATE_MainLoop();				//冲刺状态机
}
