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
 * @note   两个档位(见 main.h):
 *           - MOUSE_REMOTE —— 远程档: 跳过按键菜单, 等上位机 `MAZE RUN [BLIND|RACE]` 发车,
 *             可反复"发车 → 收车 → 再发车"; 值守期间参数读写 / 标定 / 手动 MOVE / 遥测全可用。
 *           - 不定义      —— 按键比赛档: 与原始比赛固件逐字节一致(按键菜单 → 读图选档 →
 *             发车判断 → 无记忆走迷宫 → 冲刺状态机)。
 */
int main()
{
	HARDWARE_Init();				//硬件初始化
	LINK_Init();					//蓝牙链路初始化(RX优先级/DMA/标定参数载入; 须在HARDWARE_Init之后)
	MOUSE_Init();					//任务初始化
	CALIB_InitIr();					//红外初始化

#ifdef MOUSE_REMOTE
	/* ================= 远程档: 值守态 + 远程发车(可重复) =================
	 * 与按键比赛档的差别只有入口与"是否接受远程命令", 自走策略完全相同:
	 *   1) 跳过按键菜单(改由上位机 `MAZE RUN` 发车);
	 *   2) 发车前在下面空转等待 —— 而**采集与命令全都由 SysTick 的 LINK_Tick() 完成**,
	 *      所以等待期间遥测照发、命令照收, 上位机正是靠它把时间基准对齐。
	 * 之后进入 LESSON_Blind8(): 它是 while(1) 阻塞式自走, 主循环不再返回,
	 * 但通信依旧由中断维持 —— 这是"运动期间也能完整收数据"的关键。 */
	speedSt.freemode = 1;	/* 未发车前: 电机输出恒 0 */
	speedSt.sleepmode = 0;	/* 不休眠, 保证传感器/链路持续工作 */
	wallSns.irSwitch = 1;	/* 打开红外发射 */
	irCalib.calibMode = 0;	/* 关闭红外标定模式 */
	flashSt.flashenable = 0;/* 运行期间不写 Flash, 避免覆盖比赛运行数据 */
	battMon.enabled = 1;	/* 盯电池状态(只读 ADC) */
	emergMon.enabled = 1;	/* 过流/超速急停监控(第三道保险, MOVE RUN 双护栏之外) */

	STATE_LoadMapAndSelectSpeed();	/* 读地图信息(远程档: 到冲刺选档段前即返回, 见 state.c) */
	STATE_DetermineGoaheadNum();	/* 远程档: 直接返回, 不做"鼻尖顶墙"式冲刺选档 */

	/* ---- 等上位机发车（**可重复**）----
	 * 上位机: 先 ARM(武装计时 + 点亮 1Hz 基准灯 + 记发车时刻), 再 `MAZE RUN [BLIND|RACE]`。
	 * 这里只等 MAZE RUN; ARM 只影响计时, 不影响是否出发。
	 * 用 DELAY_Ms(1) 让出时间片, SysTick 里的采样/组帧/命令解析照常跑。
	 *
	 * 外层再套一层循环: LESSON_Blind8() 在收到 `MAZE STOP` / `ESTOP` 时会返回
	 * (见 APP/lesson.c 的退出检查), 这里随即 LINK_MazeFinished() 复位发车状态, 回到等待。
	 * 于是"发车 → 急停 → 收车 → 再发车"不必断电; 修复前发过一次车后
	 * `MAZE RUN` 会永远回 ERR_BUSY(真机复现 2026-10-05)。 */
	while (1)
	{
		while (!LINK_MazeRequested())
			DELAY_Ms(1);

		LINK_MazeStarted(); /* 消费发车请求位 + 置"已在跑" */

		if (LINK_MazeTarget() == LINK_MAZE_RACE)
		{
			/* ---- 正式比赛入口序列 ----
			 * 每次发车重载一次 Flash 地图/起终点/目标方向 (§STATE_LoadMapAndSelectSpeed 的
			 * 非阻塞读图段), 再走一次发车判断。冲刺选档的两个 while(1) 在远程档被整段跳过,
			 * 因此这里**不会**产生冲刺档位 —— 见 main.h 的说明。
			 * 目标为 BLIND(默认)时跳过本段, 直接用内存里的地图探索。 */
			STATE_LoadMapAndSelectSpeed();
			STATE_DetermineGoaheadNum();
		}

		MOTION_SetAngle(0); /* 每次发车都从零角度起 —— 这是"可重复发车"的一半 */
		taskSt.backSearch = 1;

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
