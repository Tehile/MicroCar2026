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

#ifdef MOUSE_TEST_MODE
/**
 * @brief  测试模式值守循环(不返回)
 * @param  none
 * @note   两个测试模式共用: 让板子活着(LED 心跳) + 让链路与传感器一直跑。
 *         采样/组帧/命令解析/标定服务全部由 SysTick 里的 LINK_Tick() 完成,
 *         所以这里空转即可, 不做任何会阻塞的事。
 *         安全模式(MOUSE_TEST_NO_MOTOR)下电机已物理禁用;
 *         电机实验模式(MOUSE_TEST_MOTOR_LAB)下电机只受下行命令控制。
 */
static void STATE_TestLoop(void)
{
	u8 led = 1;
	while (1)
	{
		LED_Off(0);  /* 先全灭 */
		LED_On(led); /* 再点亮 LED1..3 循环, 1Hz 慢闪表示"运行中" */
		led++;
		if (led > 3)
			led = 1;
		DELAY_Ms(500);
	}
}
#endif

/**
 * @brief  主函数: 启动流程编排
 * @param  none
 * @note   顺序: 硬件初始化→鼠应用初始化→红外标定载入→按键菜单→读图选速→
 *         冲刺方向判定→(可选)冲刺调试→进入主状态机(不返回)
 * @note   定义 MOUSE_TEST_MODE(见 main.h; 由 MOUSE_TEST_NO_MOTOR 或
 *         MOUSE_TEST_MOTOR_LAB 派生)时走测试分支: 不按键、不载图、不跑状态机,
 *         只保留链路与传感器。
 *           - 安全模式          : 电机恒停, 只做采集与通信
 *           - 电机实验模式      : 电机只受下行 `MOVE RUN`(双护栏)控制, ESTOP 锁存
 */
int main()
{
	HARDWARE_Init();				//硬件初始化
	LINK_Init();					//蓝牙链路初始化(RX优先级/DMA/标定参数载入; 须在HARDWARE_Init之后)
	MOUSE_Init();					//任务初始化
	CALIB_InitIr();					//红外初始化

#ifdef MOUSE_TEST_MODE
	/* ================= 测试模式公用初始化 =================
	 * 电机侧: 安全模式在 motor.c 里被彻底停用; 实验模式则保持可控。
	 * 这里统一把流程侧"可能自己动起来"的口子关掉。 */
	speedSt.freemode = 1;	/* 自由模式: 空闲时电机输出恒 0(MOVE RUN 会显式清 0) */
	speedSt.sleepmode = 0;	/* 不休眠, 保证传感器/链路持续工作 */
	wallSns.irSwitch = 1;	/* 打开红外发射 */
	irCalib.calibMode = 0;	/* 关闭红外标定模式 */
	flashSt.flashenable = 0;/* 测试期间不写 Flash, 避免覆盖比赛运行数据 */

	/* 电池电压监视: 该开关原本只在 LESSON_Cali5() 里打开(教学实验),
	 * 正常启动流程从不打开 => battMon.voltage 恒为 0, 上位机实时面板会一直显示 0.00V。
	 * 测试阶段需要盯着电池状态, 故在此显式打开(只读 ADC, 不涉及任何执行器)。 */
	battMon.enabled = 1;

#if defined(MOUSE_TEST_MOTOR_LAB) || defined(MOUSE_LESSON_LAB)
	/* 电机实验模式 / 课程调参模式: 保留过流/超速急停监控 —— 这是除双护栏之外的第三道保险。
	 * 课程调参模式同样驱动电机, 因此急停监控必须一起打开。
	 * (急停后的"休眠"已在 isr.c 里按 MOUSE_TEST_MODE 关掉, 所以不会因此丢数据。) */
	emergMon.enabled = 1;
#else
	/* 安全模式: 无电机, 急停判定无意义。 */
	emergMon.enabled = 0;
#endif

	STATE_TestLoop();	/* 不返回 */
#elif defined(MOUSE_DATA_LOG)
	/* ================= 数据收集档: 远程发车 + 无记忆走迷宫 =================
	 * 与比赛固件的差别只有两点:
	 *   1) 跳过按键菜单(改由上位机 ARM/MAZE RUN 发车);
	 *   2) 发车前先在这里空转等待 —— 而**采集与命令全都由 SysTick 的
	 *      LINK_Tick() 完成**, 所以这段等待期间遥测照发、命令照收,
	 *      上位机正是靠它在发车前就看到数据流、并把时间基准对齐。
	 * 之后进入 LESSON_Blind8(): 它是 while(1) 阻塞式自走, 主循环不再返回,
	 * 但通信依旧由中断维持 —— 这是"运动期间也能完整收数据"的关键。 */
	speedSt.freemode = 1;	/* 未发车前: 电机输出恒 0 */
	speedSt.sleepmode = 0;	/* 不休眠, 保证传感器/链路持续工作 */
	wallSns.irSwitch = 1;	/* 打开红外发射 */
	irCalib.calibMode = 0;	/* 关闭红外标定模式 */
	flashSt.flashenable = 0;/* 采集期间不写 Flash, 避免覆盖比赛运行数据 */
	battMon.enabled = 1;	/* 盯电池状态 */
	emergMon.enabled = 1;	/* 过流/超速急停监控(第三道保险, 双护栏之外) */

	STATE_LoadMapAndSelectSpeed();	/* 读取地图信息(与比赛固件一致) */
	STATE_DetermineGoaheadNum();	/* 发车判断(90/45 冲刺) */
	MOTION_SetAngle(0);				/* 设定状态初值 */
	taskSt.backSearch = 1;

	/* ---- 等待上位机发车（**可重复**）----
	 * 上位机: 先 ARM(武装计时 + 点亮 1Hz 基准灯 + 记发车时刻), 再 MAZE RUN。
	 * 这里只等 MAZE RUN; ARM 只影响计时, 不影响是否出发。
	 * 用 DELAY_Ms(1) 让出时间片, SysTick 里的采样/组帧/命令解析照常跑。
	 *
	 * v2.1: 外层再套一层循环, 让这段**可以走第二遍**。
	 *   LESSON_Blind8() 在收到 `MAZE STOP` / `ESTOP` 时会返回(见 APP/lesson.c 的
	 *   退出检查), 这里随即 LINK_MazeFinished() 复位发车状态, 回到等待。
	 *   于是"发车 → 急停 → 收车 → 再发车"不必断电; 修复前发过一次车后
	 *   `MAZE RUN` 会永远回 ERR_BUSY(真机复现 2026-10-05)。 */
	while (1)
	{
		while (!LINK_MazeRequested())
			DELAY_Ms(1);

		LINK_MazeStarted(); /* 消费发车请求位 + 置"已在跑" */
		LESSON_Blind8();	/* 无记忆走迷宫(收到退出请求时返回) */
		LINK_MazeFinished();/* 复位发车状态, 允许再次发车 */
	}
#else
	MENU_Run();						//按键选择
	
	// LESSON_Motor1(); // 实验：电机驱动实验
	// LESSON_Enc2(); // 实验：编码器测距实验
	// LESSON_Pid3(); // 实验：匀变速直线控制
	// LESSON_Cali5(); // 实验：标定与校准
	
	
	STATE_LoadMapAndSelectSpeed();	//读取地图信息
	STATE_DetermineGoaheadNum();	//发车判断（90冲刺或45冲刺）
	flashSt.flashenable = 0;
	wallSns.irSwitch = 1;
	irCalib.calibMode = 0; 			// 关闭红外标定模式
	/* 电池电压监视: 原来只在 LESSON_Cali5() 里打开, 于是比赛固件下
	 * battMon.voltage 恒为 0 —— 上位机看到的是 0.00V, 不是"没电"而是"没采"。
	 * 这里显式打开, 使**统一帧的 battMv** 在比赛/常规固件下也是真实值。
	 * 只读 ADC, 不涉及任何执行器; SHOWVOLTAGE 未定义, 因此不占用 LED。 */
	battMon.enabled = 1;
	MOTION_SetAngle(0);				//设定状态初值
	taskSt.backSearch = 1;			
	
	
	// LESSON_Irs4(); // 实验：红外测距实验
	// LESSON_Multi6(); // 实验：多传感器校准与直行
	// LESSON_Turn7(); // 实验：转向控制实验
	LESSON_Blind8(); // 实验：无记忆走迷宫
	
	
	if (mazeState.spurtTestFlag == 1)
		STATE_SpurtTest();
	STATE_MainLoop();				//冲刺状态机
#endif
}
