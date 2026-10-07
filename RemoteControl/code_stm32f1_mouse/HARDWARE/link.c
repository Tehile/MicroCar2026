/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: link.c —— 蓝牙链路(遥测上行/命令下行/标定服务/参数持久化)
*       ------------------------------------------------------------------------------------------------
*               红线声明: 本文件不修改任何既有逻辑与红线数值。它只**读取**既有全局结构体, 并在
*                         方案约定的白名单内写入运行时变量(sysCfg / speedSt / irCalib / gyroStr /
*                         motionCtrl.task / odomStr.angleBase 经 MOTION_SetAngle)。
*
*               三条实现级关键约束(详见 docs/蓝牙上位机设计方案.md §4):
*                 1) RX 优先级必须高于 SysTick —— 否则 1ms 中断期间 USART 的 DR 未被读走,
*                    后续字节全部触发 ORE 被成片丢弃(一条 30B 命令丢 4-6B, 几乎必然损坏)。
*                 2) TX 必须走 DMA —— TXE 每 87us 置位一次, 而本模块的 Tick 每 1ms 才执行一次,
*                    若在 Tick 里轮询写 DR, 实际吞吐上限只有约 1000B/s(而非 11520B/s), 预算崩 10 倍。
*                 3) 1kHz 环形缓冲复用 flash.c 的 run_data[](9600B, 主流程闲置) —— 新增 RAM 0 字节。
*********************************************************************************************************
*/

#include "link.h"

/*-------------------------------------  内部常量  -------------------------------------*/
#define LINK_MOVE_SPEED_DEF 40 /* MOVE 默认速度(cm/s) */
#define LINK_SRV_SPEED 25      /* 角度基准扫描速度(cm/s) */
#define LINK_SRV_GUARD_MS 250  /* 起步保护(等速度环跟上) */
#define LINK_SRV_RISE_MS 120   /* 离开极小判据: 连续 N ms 高于当前极小 */
#define LINK_SRV_REPS 10       /* 往复次数(每次 90 度, 合计 900 度) */
#define LINK_SRV_MAXMS 30000   /* 超时保护 */
#define LINK_IR_SAMPLE_N 20    /* 单点采样平均点数 */
#define LINK_CAL_TIMEOUT 10000 /* 标定项超时(ms) */

/*-------------------------------------  内部类型  -------------------------------------*/
typedef struct
{
	u16 id;
	s32 q; /* Q16.16 */
} LinkParam;

typedef struct
{
	u8 state;  /* LINK_MOVE_* */
	u16 speed; /* cm/s */
	u32 target;
	u32 elapsed;
	u32 limit;
	long pStartL, pStartR;
	/* ---- LINK_MOVE_RUN(电机远程控制实验)专用 ----
	 * v2.0 说明: 原先还用 irMin/irWhich/vmaxL/vmaxR/runStartL/runStartR 攒一条
	 * "MOVEDONE RUN <...8 个字段>" 明细行。现在明细取消(运动过程已由统一帧
	 * 217Hz 连续采样), 这些只为那条明细存在的字段一并删除, 省 RAM 也省每毫秒的
	 * 比较/开方式统计。 */
	u32 irMm; /* 红外停车阈值(mm); 0 = 关闭红外护栏 */
} LinkMoveState;

typedef struct
{
	u8 state;
	u16 guard;
	u16 rise;
	u32 elapsed;
	long bestV;
	s32 bestAcc;
	s32 bestEnc;
	s32 prevAcc, prevEnc;
	u8 havePrev;
	u8 reps;
	s32 sumAcc;
	s32 sumEnc;
	s16 dir;
} LinkServoState;

typedef struct
{
	u8 item;
	u8 phase;
	u8 active;
	u8 needSample; /* 1=正在采样 */
	u8 samples;
	long acc;
	long acc2;
	u16 timeout;
	/* v2.0 请求-响应标定: 该步完成时要回的 ACK 序号(-1 = 不回)。
	 * 由命令处理填好, 由 link_CalibService 在采集完成的那一刻消费 ——
	 * 于是"发 CAL -> 等 -> 收 ACK"成为一条可靠的一问一答(见设计方案 §9)。 */
	s32 ackSeq;
} LinkCalState;

/*-------------------------------------  前向声明  -------------------------------------*/
static void link_Ack(s32 seq, const char *status);
/* ⚠️ link_AckI3 的实现远在文件末尾("应答"一节), 而 link_CalibService(在它之前)
 * 就会调用它 —— C90 下缺这条原型会让编译器**隐式声明**成 `int link_AckI3()`,
 * 随后与真正的 `void` 定义冲突, 报 #159 "declaration is incompatible"。
 * 这不是告警级别的问题: 它会让整个工程编译失败。 */
static void link_AckI3(s32 seq, const char *status, s32 a, s32 b, s32 c);
static void link_CalAck(u8 ok); /* 标定一步完成/失败的应答(实现见"应答"一节) */
static u8 link_CalibService(u8 item, u8 act);

/*-------------------------------------  内部状态  -------------------------------------*/
/* TX 环形缓冲与状态仅在本文件(且仅由 LINK_Tick)访问, 故无需 volatile */
static u8 linkTxBuf[LINK_TX_BUF_SIZE];
static u16 linkTxHead;
static u16 linkTxTail;
static u16 linkTxInflight;

static LinkMoveState linkMove;
static LinkServoState linkSrv;
static LinkCalState linkCal;
static LinkParam linkParam[LINK_P_NUM];

#ifdef MOUSE_REMOTE
/* ---- v2.5: id 39-55 新参数的宿主变量 ----
 * 静态初值必须 == 各编译期常量: LINK_Init 之前 ISR 就可能引用
 * (电机环每周期读 MOTOR_KP_*, 原地转读 TURN_PID_*), 不能暴露未初始化值。 */
double linkPtKp = 2.5, linkPtKi = 0.01, linkPtKd = 0.1;       /* == TURN_PID_KP/KI/KD */
double linkMtrKpL = 10.75, linkMtrKiL = 0.55, linkMtrKdL = 0.55; /* == MOTOR_KP/KI/KD_L */
double linkMtrKpR = 10.20, linkMtrKiR = 0.5, linkMtrKdR = 0.5;   /* == MOTOR_KP/KI/KD_R */
u8 linkDisF60 = 60;                                            /* == isr.c 原字面量 */
#endif

static u16 linkTick;
static u8 linkSeq;
static u16 linkDrop;
static u32 linkFrames;

/**
 * @brief 是否有任何"正在动 / 正在占硬件"的活动 —— 所有会动或会碰硬件的命令共用这一个判据
 * @param none
 * @return 1=忙(调用者应回 ERR_BUSY) 0=空闲
 *
 * **为什么必须看四处**（这是真机实测揪出的缺陷, 2026-10-04）:
 *   `MOVE FWD / MOVE ROT / MOVE RUN` 在启动时都会把 `motionCtrl.task` 置成
 *   MOTION_TASK_NULL —— 它们由 `link_MoveService()` 驱动, 不走任务状态机。
 *   于是**只看 `motionCtrl.task` 是查不出"正在跑一次 MOVE"的**, 第二条 MOVE 命令
 *   会畅通无阻地覆盖 `linkMove` 状态, 把 `elapsed` 清零、`limit` 改写 ——
 *   表现为**时间护栏被重置**: 连续发 `MOVE RUN` 就能把电机的运行时间无限延长,
 *   护栏形同虚设(真机实测: 首条 1500ms + 0.48s 处再发一条, 实际跑到 ~1.98s)。
 *   同理 `SAVE` 也会在运动中间去擦写 Flash, 而擦写停顿会打断 1kHz 控制环。
 *
 * 四处状态的含义:
 *   motionCtrl.task —— 任务状态机驱动(自走 / 课程实验)
 *   linkMove.state  —— 显式 MOVE FWD/ROT/RUN(启动时把 task 置 NULL, 故单看 task 查不出)
 *   linkSrv.state   —— 角度扫描服务(自己驱动电机)
 *   linkCal.active  —— 标定服务(可能驱动电机或写 Flash)
 *
 * 统一成一个函数而不是四处各写一遍: 之前正是因为"四处各写一遍"才漏掉了 linkMove。
 */
static u8 link_MotionBusy(void)
{
	if (motionCtrl.task != MOTION_TASK_NULL)
		return 1;
	if (linkMove.state != LINK_MOVE_NONE)
		return 1;
	if (linkSrv.state != LINK_SRV_NONE)
		return 1;
	if (linkCal.active)
		return 1;
	return 0;
}

/* ---- 急停锁存(电机远程控制实验) ---- */
static u8 linkEStop;   /* 1=急停锁存中: MOVE/CAL 被拒, 须 STOP 解锁 */
/* v2.0: 因 TX 空间不足被丢弃的事件行数。非 0 即置 flags 的 EVQ 位 ——
 * 事件只加序号不做重发, 所以"丢没丢"必须让主机看得见(设计方案 §8.3)。 */
static u16 linkEvDrop;
/* v2.0: 一次上电周期内读到的 RCC->CSR 复位原因(PING 应答上报, 见 LINK_Init)。 */
static u8 linkResetCause;
#ifdef MOUSE_REMOTE
/* 远程软复位的**倒计时**(ms, 0 = 无待办)。见 link_HandleLine 的 `MCU RESET` 分支。
 * 为什么要倒计时而不是立刻复位: 要先让 `ACK OK` 有机会离开 TX 环/蓝牙模块,
 * 否则上位机什么都收不到, 只能看到超时, 无从判断到底成没成。
 * 复位后 PING 的 rst 会变成 `SW` —— 那才是最终的成功凭据。 */
static u16 linkMcuResetDelay;
#endif

/* ---- 时间同步 / 发车(独立于校准与各测试档; 见 link.h 与 docs/数据收集与时间同步.md) ----
 * 这一组**不由任何测试档包住**: 生产固件同样编译、同样可用。
 * 全部只在 1ms 中断上下文(LINK_Tick / 命令处理)里读写, 与主循环并发,
 * 所以跑迷宫(主循环 while(1))期间计时照走、灯照闪、帧照发。 */
static u32 linkTimeMs;  /* 上电以来的毫秒时基。u32 可走 49 天不回绕 */
static u32 linkArmMs;   /* 发车(ARM)时刻在 linkTimeMs 上的读数; 0 = 未武装 */
static u8 linkArmed;    /* 1 = 已收到 ARM */
static u8 linkTimeLed;  /* 时间基准灯当前电平(0/1) */
static u8 linkMazeReq;   /* 1 = 已收到 MAZE RUN。**边沿信号**: main() 消费后由
                          *     LINK_MazeStarted() 清零 —— 见那里的说明。 */
static u8 linkMazeRun;   /* 1 = main() 已真的进入迷宫(退出后由 LINK_MazeFinished 清) */
#ifdef MOUSE_REMOTE
static u8 linkMazeAbort; /* 1 = 收到"退出迷宫"请求, 请 LESSON_Blind8() 返回。
                          *     只有远程档会用: MAZE STOP 与 ESTOP 置位。
                          *     门控在 MOUSE_REMOTE 之下, 使按键比赛档的 RAM/ROM 不受影响。 */
static u8 linkMazeTarget; /* 本次发车目标(LINK_MAZE_BLIND / LINK_MAZE_RACE)。
                           *    由 `MAZE RUN [BLIND|RACE]` 写, 由 main() 经
                           *    LINK_MazeTarget() 读 —— 只在远程档存在。 */
#endif

static s32 linkLastSeq;
static u16 linkLastCrc;
static u8 linkLastStatus[14];

static long linkEncPrevL, linkEncPrevR;
static long linkEncDiffAcc;

/*-------------------------------------  C R C 1 6 (MODBUS)  -------------------------------------*/
/* 半字节查表: tab[n] = 从初值 n 出发连续做 4 次"右移/条件异或 0xA001"的结果。
 * 于是一个字节由"内层循环 8 次"降为"两条语句", 函数体是直线代码(无分支循环)。
 *
 * 收益是**时间**而不是体积: 实测(-O0) 查表版 9 条指令/字节, 逐位版函数体 14 条但
 * 内含执行 8 次的循环, 动态约 94 条/字节, 相差约 10 倍。换算到 482 字节的 dump 块:
 *   逐位版 ~0.79ms, 查表版 ~0.075ms。
 * 这点很关键 —— dump 块的 CRC 是在 1ms 的 SysTick 里算的, 0.79ms 会吃掉近 80% 预算。
 * 代价: RO 增加 32 字节(16 项 u16 表), 整机 ROM 由 91556B 变为 91588B。
 *
 * 等价性已在 Python 上穷举验证: 256 个字节值 x 1024 个 CRC 状态(262144 组)零差异,
 * 另有 3000 组 0~600 字节随机缓冲零差异, 且 "123456789" -> 0x4B37 与标准向量一致。 */
static const u16 linkCrcTab[16] = {
	0x0000, 0xCC01, 0xD801, 0x1400,
	0xF001, 0x3C00, 0x2800, 0xE401,
	0xA001, 0x6C00, 0x7800, 0xB401,
	0x5000, 0x9C01, 0x8801, 0x4400};

/**
 * @brief  CRC16 单字节推进(多项式 0xA001, 初值 0xFFFF)
 * @param  crc 当前值
 * @param  data 字节
 * @return 新值
 * @note   与上位机 host/host/protocol/codec.py 的输出逐位一致;
 *         校验向量 "123456789" -> 0x4B37, 已在 host/selftest.py 中断言。
 */
static u16 link_CrcByte(u16 crc, u8 data)
{
	crc ^= (u16)data;
	crc = (u16)((crc >> 4) ^ linkCrcTab[crc & 0x0F]);
	crc = (u16)((crc >> 4) ^ linkCrcTab[crc & 0x0F]);
	return crc;
}

static u16 link_Crc16(const u8 *p, u16 n)
{
	u16 crc = 0xFFFF;
	while (n--)
		crc = link_CrcByte(crc, *p++);
	return crc;
}

/*-------------------------------------  T X : DMA  -------------------------------------*/
/**
 * @brief  DMA 初始化(USART1_TX 固定映射 DMA1_Channel4)
 * @param  none
 * @return none
 * @note   本项目全工程零 DMA 使用(ADC 为软件触发单次阻塞读), 故 Channel4 可独占。
 */
static void link_DmaInit(void)
{
	RCC->AHBENR |= 1 << 0; /* DMA1 时钟 */
	USART1->CR3 |= 1 << 7; /* DMAT: USART1 发送由 DMA 供数 */
	DMA1_Channel4->CCR = 0;
}

/**
 * @brief  查询 DMA 是否空闲, 并在完成时推进 TX 记账
 * @param  none
 * @return 1=空闲 0=忙
 * @note   tail 只在传输完成后推进, 因此生产者看到的空间是保守值, 绝不会覆写在途数据
 */
static u8 link_DmaIdle(void)
{
	if (DMA1_Channel4->CNDTR != 0)
		return 0;
	if (linkTxInflight)
	{
		linkTxTail = (u16)(linkTxTail + linkTxInflight);
		linkTxInflight = 0;
	}
	return 1;
}

/**
 * @brief  启动一次 DMA 传输(内存 -> USART1->DR)
 * @param  src 源地址(须连续)
 * @param  n   字节数(非 0)
 * @return none
 */
static void link_DmaStart(const u8 *src, u16 n)
{
	DMA1_Channel4->CCR = 0; /* EN=0 之后才允许改写 CMAR/CPAR/CNDTR */
	DMA1_Channel4->CPAR = (u32)(&USART1->DR);
	DMA1_Channel4->CMAR = (u32)src;
	DMA1_Channel4->CNDTR = n;
	/* EN | DIR(内存->外设) | MINC(内存地址递增), PSIZE/MSIZE 均 8bit, 低优先级 */
	DMA1_Channel4->CCR = (1 << 0) | (1 << 4) | (1 << 7);
}

/*-------------------------------------  T X : 环缓冲  -------------------------------------*/
static u16 link_TxUsed(void)
{
	return (u16)(linkTxHead - linkTxTail);
}

static u16 link_TxSpace(void)
{
	return (u16)(LINK_TX_BUF_SIZE - link_TxUsed());
}

/**
 * @brief  向 TX 环缓冲写一个字节(内部使用)
 * @param  b 字节
 * @return none
 * @note   用 PRIMASK 保护"读 head -> 写 buf -> 自增 head"三步。
 *         必要性: LINK_Tick 在 SysTick 上下文入队, 而 printf 经 LINK_PutChar
 *         可能在更高优先级的中断里入队; 若两者交错, 会写同一个槽位并丢失计数。
 *         保护段极短(约几十纳秒), 不会影响 USART1 接收中断的响应。
 */
static void link_TxPut(u8 b)
{
	u32 pm = __get_PRIMASK();
	__disable_irq();
#ifdef MOUSE_REMOTE
	/* v2.4 兜底: 环满时**丢弃该字节**而不是回绕覆盖。
	   本函数是 link_TxStr/TxInt 的内部出口, 原先无条件写入。一旦某条命令的
	   输出量超过环容量(如 `GET ALL` 一次 24 行 ≈343B > 256B), head 会越过
	   tail, 把**正在被 DMA 读取或尚未发出**的数据抹掉 —— 现场表现是
	   "输出从中间某行开始, 前面几行凭空消失", 且不定量、难复现、更难定位。
	   根治手段是扩容(见 link.h: 远程档 512B); 这条检查保证即使将来又出现
	   超长输出, 最坏也只是丢尾部若干字节(linkDrop 计数随帧上报),
	   绝不会破坏已经排好队的字节。 */
	if (link_TxSpace() == 0)
	{
		linkDrop++;
		__set_PRIMASK(pm);
		return;
	}
#endif
	linkTxBuf[linkTxHead & (LINK_TX_BUF_SIZE - 1)] = b;
	linkTxHead = (u16)(linkTxHead + 1);
	__set_PRIMASK(pm);
}

static void link_TxStr(const char *s)
{
	while (*s)
		link_TxPut((u8)(*s++));
}

static void link_TxInt(s32 v)
{
	char b[12];
	u8 n = 0;
	if (v < 0)
	{
		link_TxPut('-');
		v = -v;
	}
	if (v == 0)
		b[n++] = '0';
	while (v > 0)
	{
		b[n++] = (char)('0' + (v % 10));
		v /= 10;
	}
	while (n)
		link_TxPut((u8)b[--n]);
}

/**
 * @brief  把环缓冲的连续段交给 DMA
 * @param  none
 * @return none
 * @note   每次只搬"到缓冲末尾"的一段; 回绕时下一毫秒自动续上
 */
static void link_TxDrain(void)
{
	u16 used, n, idx;
	if (!link_DmaIdle())
		return;
	used = link_TxUsed();
	if (used == 0)
		return;
	idx = (u16)(linkTxTail & (LINK_TX_BUF_SIZE - 1));
	n = (u16)(LINK_TX_BUF_SIZE - idx);
	if (n > used)
		n = used;
	linkTxInflight = n;
	link_DmaStart(&linkTxBuf[idx], n);
}

/**
 * @brief  向 TX 环缓冲写一个字节 —— printf 的统一出口
 * @param  b 字节
 * @return 1=已入队, 0=环满已丢弃
 * @note   供 usart.c 的 fputc() 调用, 让 printf 与遥测共用同一条发送队列。
 *         这是必需的而非优化: DMA1_Channel4 已接管 USART1->DR, 若 printf 仍然
 *         直接写 DR, 会有两个后果 ——
 *           1) 与 DMA 争抢 DR, 把调试文本插进遥测帧中间, 主机侧该帧 CRC 必然失败;
 *           2) 原 fputc 用"等 TC 置位"来阻塞发送, 而 DMA 连续供数时 TC 几乎不置位,
 *              会把调用者(包括在 ISR 中的 printf)长时间卡住。
 *         写入用 PRIMASK 保护: 本函数可能在任意上下文被调用, 而 LINK_Tick
 *         也在 SysTick 上下文修改同一个 head。
 *         环满时直接丢弃该字节 —— 宁可丢调试文本, 也不阻塞系统。
 */
u8 LINK_PutChar(u8 b)
{
	u32 pm = __get_PRIMASK();
	__disable_irq();
	if ((u16)(LINK_TX_BUF_SIZE - (u16)(linkTxHead - linkTxTail)) == 0)
	{
		linkDrop++; /* 环满: 记一笔后丢弃, 绝不阻塞调用者 */
		__set_PRIMASK(pm);
		return 0;
	}
	linkTxBuf[linkTxHead & (LINK_TX_BUF_SIZE - 1)] = b;
	linkTxHead = (u16)(linkTxHead + 1);
	__set_PRIMASK(pm);
	return 1;
}

/*-------------------------------------  组帧  -------------------------------------*/
/**
 * @brief  发送一个遥测帧(经 TX 环缓冲)
 * @param  type 帧类型
 * @param  payload 载荷
 * @param  len 载荷长度
 * @return none
 * @note   空间不足时**整帧丢弃**并计数, 绝不部分写入(部分写入会造成主机永久错帧)
 */
static void link_SendFrame(u8 type, const u8 *payload, u16 len)
{
	u16 i;
	u16 crc = 0xFFFF;
	/* 预留 32B 余量: printf 路径(LINK_PutChar)与组帧可能在不同上下文并发,
	 * 若不在"检查空间"与"写入"之间留出余量, 极端情况下 head 会越过
	 * tail + LINK_TX_BUF_SIZE, 使 link_TxSpace() 的无符号减法下溢成巨大值。 */
	if (link_TxSpace() < (u16)(len + LINK_FRAME_OVERHEAD + 32))
	{
		linkDrop++;
		return;
	}
	link_TxPut(LINK_SYNC0);
	link_TxPut(LINK_SYNC1);
	link_TxPut((u8)(len & 0xFF));
	link_TxPut((u8)(len >> 8));
	link_TxPut(type);
	link_TxPut(linkSeq);
	crc = link_CrcByte(crc, type);
	crc = link_CrcByte(crc, linkSeq);
	for (i = 0; i < len; i++)
	{
		link_TxPut(payload[i]);
		crc = link_CrcByte(crc, payload[i]);
	}
	link_TxPut((u8)(crc & 0xFF));
	link_TxPut((u8)(crc >> 8));
	linkSeq++;
	linkFrames++;
}

/**
 * @brief  发一条上行事件行(带与遥测帧**共用**的序号)
 * @param  head 事件头文本(如 "STOPPED TIME" / "ESTOP TRIP")
 * @return 1=已发出 0=TX 空间不足, 已丢弃并计数(linkEvDrop)
 * @note   产出形态: `<head> #<seq>\r\n`, 其中 seq 取自 linkSeq, 发完自增。
 *
 *   - **序号与帧共用同一计数器**(设计方案 §8.3): 主机按 `step=(n-prev)&0xFF`
 *     判定 —— `step>=2` 即"中间少了消息", 既可检出事件丢失, 也能判定
 *     帧与事件的相对顺序。每发一条事件消耗一个序号, 所以帧号会跳 1 是正常的。
 *   - **只加序号、不重发**: 停等重发被 230ms 往返压在约 4 条/s, 跟不上
 *     5~6 条/s 的事件产生速度。序号保证的是"可检测", 不是"可恢复"。
 *   - **空间不足宁可丢弃**: 事件与遥测帧共用同一条 TX 环, 硬写会回绕
 *     覆盖尚未发出的数据(那会污染整段输出, 远比丢一条事件严重)。
 */
static u8 link_EvLine(const char *head)
{
	if (link_TxSpace() < LINK_EV_NEED_SPACE)
	{
		if (linkEvDrop < 0xFFFFu)
			linkEvDrop++;
		return 0u;
	}
	link_TxStr(head);
	link_TxPut(' ');
	link_TxPut('#');
	link_TxInt((s32)linkSeq);
	link_TxStr("\r\n");
	linkSeq++;
	return 1u;
}

/*-------------------------------------  数值工具  -------------------------------------*/
static s16 link_Sat16(long v)
{
	if (v > 32767L)
		return 32767;
	if (v < -32768L)
		return -32768;
	return (s16)v;
}

static void link_Put16(u8 *p, s16 v)
{
	p[0] = (u8)((u16)v & 0xFF);
	p[1] = (u8)(((u16)v >> 8) & 0xFF);
}

/*-------------------------------------  标定参数表  -------------------------------------*/
static s32 link_F2Q(float f)
{
	return (s32)((f >= 0.0f) ? (f * 65536.0f + 0.5f) : (f * 65536.0f - 0.5f));
}

static s32 link_D2Q(double d)
{
	return (s32)((d >= 0.0) ? (d * 65536.0 + 0.5) : (d * 65536.0 - 0.5));
}

static float link_Q2F(s32 q)
{
	return (float)q / 65536.0f;
}

static s32 link_Q2I(s32 q)
{
	if (q >= 0)
		return (q + 32768) >> 16;
	return -(((-q) + 32768) >> 16);
}

static void link_ParamFromSys(void)
{
	linkParam[0].id = LINK_P_TRACKL;
	linkParam[0].q = link_F2Q(sysCfg.trackRatioL);
	linkParam[1].id = LINK_P_TRACKR;
	linkParam[1].q = link_F2Q(sysCfg.trackRatioR);
	linkParam[2].id = LINK_P_SPEEDL;
	linkParam[2].q = link_D2Q(sysCfg.speedRatioL);
	linkParam[3].id = LINK_P_SPEEDR;
	linkParam[3].q = link_D2Q(sysCfg.speedRatioR);
	linkParam[4].id = LINK_P_GYRO;
	linkParam[4].q = (s32)sysCfg.gyroRatio << 16;
	linkParam[5].id = LINK_P_ENCANG;
	linkParam[5].q = (s32)sysCfg.encAngleRatio << 16;
	linkParam[6].id = LINK_P_WALLDIST;
	linkParam[6].q = (s32)sysCfg.distToWall << 16;

	/* ---- 红外测量与阈值(22-32): 初值取自 CORE_Init 设好的 irCfg / wallSns ---- */
	linkParam[7].id = LINK_P_IRTHRWALL;
	linkParam[7].q = (s32)wallSns.wallThresh << 16; /* 唯一真源 = wallSns.wallThresh */
	linkParam[8].id = LINK_P_IRTHR45;
	linkParam[8].q = (s32)irCfg.thr45 << 16;
	linkParam[9].id = LINK_P_IRTHRCRIT;
	linkParam[9].q = (s32)irCfg.thrCritical << 16;
	linkParam[10].id = LINK_P_IRTHRFLO;
	linkParam[10].q = (s32)irCfg.thrFrontLo << 16;
	linkParam[11].id = LINK_P_IRTHRFMID;
	linkParam[11].q = (s32)irCfg.thrFrontMid << 16;
	linkParam[12].id = LINK_P_IRTHRFHI;
	linkParam[12].q = (s32)irCfg.thrFrontHi << 16;
	linkParam[13].id = LINK_P_IRTHRFNEAR;
	linkParam[13].q = (s32)irCfg.thrFrontNear << 16;
	linkParam[14].id = LINK_P_IRKALQ;
	linkParam[14].q = (s32)irCfg.kalQ << 16;
	linkParam[15].id = LINK_P_IRKALP;
	linkParam[15].q = (s32)irCfg.kalP << 16;
	linkParam[16].id = LINK_P_IRKALSW;
	linkParam[16].q = (s32)irCfg.kalOn << 16;
	linkParam[17].id = LINK_P_IRFRONTMODE;
	linkParam[17].q = (s32)irCfg.frontMode << 16;

	/* ---- 电源监控(33-38): 初值取自 powerCfg ---- */
	linkParam[18].id = LINK_P_BATTKV;
	linkParam[18].q = (s32)((u32)powerCfg.battKv << 16);
	linkParam[19].id = LINK_P_BATTLOWMV;
	linkParam[19].q = (s32)powerCfg.battLowMv << 16;
	linkParam[20].id = LINK_P_BATTHIGHMV;
	linkParam[20].q = (s32)powerCfg.battHighMv << 16;
	linkParam[21].id = LINK_P_BATTALPHA;
	linkParam[21].q = (s32)powerCfg.battAlpha << 16;
	linkParam[22].id = LINK_P_BATTMAXPCT;
	linkParam[22].q = (s32)powerCfg.battMaxPct << 16;
	linkParam[23].id = LINK_P_BATTMINPCT;
	linkParam[23].q = (s32)powerCfg.battMinPct << 16;

#ifdef MOUSE_REMOTE
	/* ---- 运动/控制参数(39-55): 初值取自 ISR_Init/CORE_Init 设好的变量与编译期常量 ----
	 * 本函数在 LINK_Init 内调用, 晚于 CORE_Init/ISR_Init(main.c: 64/65 vs 81), 变量已就位。 */
	{
		extern float turnDelay; /* motion.c; 转弯后回落点(motion.c:601=70)与冲刺路径
		                         * (spurt.c GainSet90=14)会覆写, 见参数调整表文档 */

		linkParam[24].id = LINK_P_TRACKCOORD;
		linkParam[24].q = (s32)sysCfg.trackCoord << 16;
		linkParam[25].id = LINK_P_TURNDELAY;
		linkParam[25].q = link_F2Q(turnDelay);
		linkParam[26].id = LINK_P_SPDSEARCH;
		linkParam[26].q = (s32)speedCfg.search << 16;
		linkParam[27].id = LINK_P_SPDTURN;
		linkParam[27].q = (s32)speedCfg.turn << 16;
		linkParam[28].id = LINK_P_DISF60;
		linkParam[28].q = (s32)linkDisF60 << 16;
		linkParam[29].id = LINK_P_GSKP;
		linkParam[29].q = link_D2Q(pidGain.goStraight);
		linkParam[30].id = LINK_P_TURNP;
		linkParam[30].q = link_D2Q(pidGain.turnP);
		linkParam[31].id = LINK_P_TURND;
		linkParam[31].q = link_D2Q(pidGain.turnD);
		linkParam[32].id = LINK_P_PTKP;
		linkParam[32].q = link_D2Q(linkPtKp);
		linkParam[33].id = LINK_P_PTKI;
		linkParam[33].q = link_D2Q(linkPtKi);
		linkParam[34].id = LINK_P_PTKD;
		linkParam[34].q = link_D2Q(linkPtKd);
		linkParam[35].id = LINK_P_MKPL;
		linkParam[35].q = link_D2Q(linkMtrKpL);
		linkParam[36].id = LINK_P_MKIL;
		linkParam[36].q = link_D2Q(linkMtrKiL);
		linkParam[37].id = LINK_P_MKDL;
		linkParam[37].q = link_D2Q(linkMtrKdL);
		linkParam[38].id = LINK_P_MKPR;
		linkParam[38].q = link_D2Q(linkMtrKpR);
		linkParam[39].id = LINK_P_MKIR;
		linkParam[39].q = link_D2Q(linkMtrKiR);
		linkParam[40].id = LINK_P_MKDR;
		linkParam[40].q = link_D2Q(linkMtrKdR);
	}
#endif
}

/** @brief 把 s32 参数值限制在 [lo, hi](link_ParamApplyOne 用, 防止写进离谱值) */
static s32 link_ClampQ(s32 q, s32 lo, s32 hi)
{
	if (q < lo)
		return lo;
	if (q > hi)
		return hi;
	return q;
}

/**
 * @brief  把单个标定参数应用到运行时配置
 * @param  id 参数 id
 * @param  q  Q16.16
 * @return 1=成功 0=id 非法
 * @note   只转换被改动的那一项, 避免无谓的软件浮点开销(F1 无 FPU)。
 *         gyroRatio/encAngleRatio/distToWall 在 core.h 中为整型, 故取整写入;
 *         其量化粒度已在方案文档 §6.3 论证无害(三者均不在控制环内)。
 */
static u8 link_ParamApplyOne(u16 id, s32 q)
{
	switch (id)
	{
	case LINK_P_TRACKL:
		sysCfg.trackRatioL = link_Q2F(q);
		break;
	case LINK_P_TRACKR:
		sysCfg.trackRatioR = link_Q2F(q);
		break;
	case LINK_P_SPEEDL:
		sysCfg.speedRatioL = (double)q / 65536.0;
		break;
	case LINK_P_SPEEDR:
		sysCfg.speedRatioR = (double)q / 65536.0;
		break;
	case LINK_P_GYRO:
		sysCfg.gyroRatio = (u16)link_Q2I(q);
		break;
	case LINK_P_ENCANG:
		sysCfg.encAngleRatio = (u16)link_Q2I(q);
		break;
	case LINK_P_WALLDIST:
		sysCfg.distToWall = (u8)link_Q2I(q);
		break;

	/* ---- 红外测量与阈值(22-32) ----
	 * 全部整数语义: Q16.16 -> 就近取整 -> 钳制到合法区间。
	 * 注意 IRTHRWALL 写的是 wallSns.wallThresh(u8): 它与 wallSns 共用同一份,
	 * 这样 APP/state.c 在比赛流程里改成 140 仍然有效(红线行为不变)。 */
	case LINK_P_IRTHRWALL:
		wallSns.wallThresh = (u8)link_ClampQ(link_Q2I(q), 1, 255);
		break;
	case LINK_P_IRTHR45:
		irCfg.thr45 = (u16)link_ClampQ(link_Q2I(q), 0, (s32)LINK_IR_THR_MAX);
		break;
	case LINK_P_IRTHRCRIT:
		irCfg.thrCritical = (u16)link_ClampQ(link_Q2I(q), 0, (s32)LINK_IR_THR_MAX);
		break;
	case LINK_P_IRTHRFLO:
		irCfg.thrFrontLo = (u16)link_ClampQ(link_Q2I(q), 0, (s32)LINK_IR_THR_MAX);
		break;
	case LINK_P_IRTHRFMID:
		irCfg.thrFrontMid = (u16)link_ClampQ(link_Q2I(q), 0, (s32)LINK_IR_THR_MAX);
		break;
	case LINK_P_IRTHRFHI:
		irCfg.thrFrontHi = (u16)link_ClampQ(link_Q2I(q), 0, (s32)LINK_IR_THR_MAX);
		break;
	case LINK_P_IRTHRFNEAR:
		irCfg.thrFrontNear = (u16)link_ClampQ(link_Q2I(q), 0, (s32)LINK_IR_THR_MAX);
		break;
	case LINK_P_IRKALQ:
		irCfg.kalQ = (u16)link_ClampQ(link_Q2I(q), 0, (s32)LINK_IR_KAL_MAX);
		kalmanSt.Qf1 = kalmanSt.Qf2 = kalmanSt.Ql = kalmanSt.Qr = (double)irCfg.kalQ / 10000.0;
		break;
	case LINK_P_IRKALP:
		irCfg.kalP = (u16)link_ClampQ(link_Q2I(q), 1, (s32)LINK_IR_KAL_MAX);
		kalmanSt.Pf1 = kalmanSt.Pf2 = kalmanSt.Pl = kalmanSt.Pr = (double)irCfg.kalP / 10000.0;
		break;
	case LINK_P_IRKALSW:
		irCfg.kalOn = (link_Q2I(q) != 0) ? 1u : 0u;
		irCalib.kalmanswitch = irCfg.kalOn;
		break;
	case LINK_P_IRFRONTMODE:
		irCfg.frontMode = (u8)link_ClampQ(link_Q2I(q), 0, 2);
		break;

	/* ---- 电源监控(33-38) ---- */
	case LINK_P_BATTKV:
		powerCfg.battKv = (u32)link_ClampQ(link_Q2I(q), (s32)LINK_BATT_KV_MIN, (s32)LINK_BATT_KV_MAX);
		break;
	case LINK_P_BATTLOWMV:
		powerCfg.battLowMv = (u16)link_ClampQ(link_Q2I(q), 0, 20000);
		break;
	case LINK_P_BATTHIGHMV:
		powerCfg.battHighMv = (u16)link_ClampQ(link_Q2I(q), 0, 20000);
		break;
	case LINK_P_BATTALPHA:
		powerCfg.battAlpha = (u16)link_ClampQ(link_Q2I(q), 0, 1000);
		break;
	case LINK_P_BATTMAXPCT:
		powerCfg.battMaxPct = (u8)link_ClampQ(link_Q2I(q), 1, 100);
		break;
	case LINK_P_BATTMINPCT:
		powerCfg.battMinPct = (u8)link_ClampQ(link_Q2I(q), 0, 100);
		break;

#ifdef MOUSE_REMOTE
	/* ---- 运动/控制参数(39-55): 宿主即运行时配置本体 ----
	 * 增益类统一钳制 [0, 100.0]; TURNDELAY 钳制 [1.0, 200.0] 格;
	 * 速度/阈值类钳制到整数语义域。 */
	case LINK_P_TRACKCOORD:
		sysCfg.trackCoord = (u8)link_ClampQ(link_Q2I(q), 0, 255);
		break;
	case LINK_P_TURNDELAY:
	{
		extern float turnDelay; /* motion.c */
		turnDelay = link_Q2F(link_ClampQ(q, 65536, 13107200));
	}
	break;
	case LINK_P_SPDSEARCH:
		speedCfg.search = (u16)link_ClampQ(link_Q2I(q), 0, 1000);
		break;
	case LINK_P_SPDTURN:
		speedCfg.turn = (u16)link_ClampQ(link_Q2I(q), 0, 1000);
		break;
	case LINK_P_DISF60:
		linkDisF60 = (u8)link_ClampQ(link_Q2I(q), 0, 255);
		break;
	case LINK_P_GSKP:
		pidGain.goStraight = link_Q2F(link_ClampQ(q, 0, 6553600));
		break;
	case LINK_P_TURNP:
		pidGain.turnP = link_Q2F(link_ClampQ(q, 0, 6553600));
		break;
	case LINK_P_TURND:
		pidGain.turnD = link_Q2F(link_ClampQ(q, 0, 6553600));
		break;
	case LINK_P_PTKP:
		linkPtKp = link_Q2F(link_ClampQ(q, 0, 6553600));
		break;
	case LINK_P_PTKI:
		linkPtKi = link_Q2F(link_ClampQ(q, 0, 6553600));
		break;
	case LINK_P_PTKD:
		linkPtKd = link_Q2F(link_ClampQ(q, 0, 6553600));
		break;
	case LINK_P_MKPL:
		linkMtrKpL = link_Q2F(link_ClampQ(q, 0, 6553600));
		break;
	case LINK_P_MKIL:
		linkMtrKiL = link_Q2F(link_ClampQ(q, 0, 6553600));
		break;
	case LINK_P_MKDL:
		linkMtrKdL = link_Q2F(link_ClampQ(q, 0, 6553600));
		break;
	case LINK_P_MKPR:
		linkMtrKpR = link_Q2F(link_ClampQ(q, 0, 6553600));
		break;
	case LINK_P_MKIR:
		linkMtrKiR = link_Q2F(link_ClampQ(q, 0, 6553600));
		break;
	case LINK_P_MKDR:
		linkMtrKdR = link_Q2F(link_ClampQ(q, 0, 6553600));
		break;
#endif
	default:
		return 0;
	}
	return 1;
}

static u8 link_ParamFind(u16 id, u16 *idx)
{
	u16 i;
	for (i = 0; i < LINK_P_NUM; i++)
	{
		if (linkParam[i].id == id)
		{
			*idx = i;
			return 1;
		}
	}
	return 0;
}

static u8 link_ParamSet(u16 id, s32 q)
{
	u16 i;
	if (!link_ParamFind(id, &i))
		return 0;
	linkParam[i].q = q;
	return link_ParamApplyOne(id, q);
}

#ifdef MOUSE_REMOTE
/**
 * @brief  IRTHRWALL 覆写分支结束后的"恢复值"(motion.c 智能冲刺路径 ×2)
 * @return 参数表当前 IRTHRWALL 值(钳制 1-255, 与 link_ParamApplyOne 同界)
 * @note   原代码此处硬编码 160 —— SETQ 改 IRTHRWALL 后, 只要跑过一次 45° 弯/
 *         智能冲刺就会被改回 160(覆写点 motion.c:418/436 -> 110, 恢复点 -> 160)。
 *         v2.5 起恢复点改读参数表: SETQ 的值不再被吃掉; 覆写点(45° 弯收紧到 110)
 *         保留 —— 那是功能行为不是漂移。比赛档不调用本函数(宏展开回字面量 160)。
 */
u8 LINK_WallThrDefault(void)
{
	u16 idx;
	if (link_ParamFind(LINK_P_IRTHRWALL, &idx))
		return (u8)link_ClampQ(link_Q2I(linkParam[idx].q), 1, 255);
	return 160; /* 表项缺失的兜底(理论上不可达) */
}
#endif

static u8 link_NameToId(const char *s, u16 *id)
{
	if (!strcmp(s, "TRACKL"))
		*id = LINK_P_TRACKL;
	else if (!strcmp(s, "TRACKR"))
		*id = LINK_P_TRACKR;
	else if (!strcmp(s, "SPEEDL"))
		*id = LINK_P_SPEEDL;
	else if (!strcmp(s, "SPEEDR"))
		*id = LINK_P_SPEEDR;
	else if (!strcmp(s, "GYRORATIO"))
		*id = LINK_P_GYRO;
	else if (!strcmp(s, "ENCANGLE"))
		*id = LINK_P_ENCANG;
	else if (!strcmp(s, "WALLDIST"))
		*id = LINK_P_WALLDIST;
	/* ---- 红外测量与阈值(22-32): 生产命名空间, 任何固件都认得 ---- */
	else if (!strcmp(s, "IRTHRWALL"))
		*id = LINK_P_IRTHRWALL;
	else if (!strcmp(s, "IRTHR45"))
		*id = LINK_P_IRTHR45;
	else if (!strcmp(s, "IRTHRCRIT"))
		*id = LINK_P_IRTHRCRIT;
	else if (!strcmp(s, "IRTHRFLO"))
		*id = LINK_P_IRTHRFLO;
	else if (!strcmp(s, "IRTHRFMID"))
		*id = LINK_P_IRTHRFMID;
	else if (!strcmp(s, "IRTHRFHI"))
		*id = LINK_P_IRTHRFHI;
	else if (!strcmp(s, "IRTHRFNEAR"))
		*id = LINK_P_IRTHRFNEAR;
	else if (!strcmp(s, "IRKALQ"))
		*id = LINK_P_IRKALQ;
	else if (!strcmp(s, "IRKALP"))
		*id = LINK_P_IRKALP;
	else if (!strcmp(s, "IRKALSW"))
		*id = LINK_P_IRKALSW;
	else if (!strcmp(s, "IRFRONTMODE"))
		*id = LINK_P_IRFRONTMODE;
	/* ---- 电源监控(33-38) ---- */
	else if (!strcmp(s, "BATTKV"))
		*id = LINK_P_BATTKV;
	else if (!strcmp(s, "BATTLOWMV"))
		*id = LINK_P_BATTLOWMV;
	else if (!strcmp(s, "BATTHIGHMV"))
		*id = LINK_P_BATTHIGHMV;
	else if (!strcmp(s, "BATTALPHA"))
		*id = LINK_P_BATTALPHA;
	else if (!strcmp(s, "BATTMAXPCT"))
		*id = LINK_P_BATTMAXPCT;
	else if (!strcmp(s, "BATTMINPCT"))
		*id = LINK_P_BATTMINPCT;
#ifdef MOUSE_REMOTE
	else if (!strcmp(s, "TRACKCOORD"))
		*id = LINK_P_TRACKCOORD;
	else if (!strcmp(s, "TURNDELAY"))
		*id = LINK_P_TURNDELAY;
	else if (!strcmp(s, "SPDSEARCH"))
		*id = LINK_P_SPDSEARCH;
	else if (!strcmp(s, "SPDTURN"))
		*id = LINK_P_SPDTURN;
	else if (!strcmp(s, "DISF60"))
		*id = LINK_P_DISF60;
	else if (!strcmp(s, "GSKP"))
		*id = LINK_P_GSKP;
	else if (!strcmp(s, "TURNP"))
		*id = LINK_P_TURNP;
	else if (!strcmp(s, "TURND"))
		*id = LINK_P_TURND;
	else if (!strcmp(s, "PTKP"))
		*id = LINK_P_PTKP;
	else if (!strcmp(s, "PTKI"))
		*id = LINK_P_PTKI;
	else if (!strcmp(s, "PTKD"))
		*id = LINK_P_PTKD;
	else if (!strcmp(s, "MKPL"))
		*id = LINK_P_MKPL;
	else if (!strcmp(s, "MKIL"))
		*id = LINK_P_MKIL;
	else if (!strcmp(s, "MKDL"))
		*id = LINK_P_MKDL;
	else if (!strcmp(s, "MKPR"))
		*id = LINK_P_MKPR;
	else if (!strcmp(s, "MKIR"))
		*id = LINK_P_MKIR;
	else if (!strcmp(s, "MKDR"))
		*id = LINK_P_MKDR;
#endif
	else
		return 0;
	return 1;
}

/*-------------------------------------  Flash 持久化  -------------------------------------*/
/**
 * @brief  擦除并写入标定参数页(page 99)
 * @param  none
 * @return none
 * @note   页布局: magic(u32) ver(u16) cnt(u16) crc(u16) rsvd(u16) | payload(每项 id u16 + q s32)
 *         写 Flash 期间 CPU 取指停顿, 故调用方应保证电机已停(标定流程本身 freemode=1)。
 */
static void link_CalibSave(void)
{
	u32 addr;
	u16 i;
	u16 crc = 0xFFFF;

	FLASH_Unlock();
	FLASH_ErasePage(FLASH_ADDR_SYSCALIB);

	addr = FLASH_ADDR_SYSCALIB;
	FLASH_Unlock();
	FLASH_WriteHalfWord(addr, (int)(LINK_CALIB_MAGIC & 0xFFFF));
	addr += 2;
	FLASH_Unlock();
	FLASH_WriteHalfWord(addr, (int)((LINK_CALIB_MAGIC >> 16) & 0xFFFF));
	addr += 2;
	FLASH_Unlock();
	FLASH_WriteHalfWord(addr, LINK_CALIB_VER);
	addr += 2;
	FLASH_Unlock();
	FLASH_WriteHalfWord(addr, LINK_P_NUM);
	addr += 2;
#ifdef MOUSE_REMOTE
	/* ⚠️ crc 槽(+8) 必须保持**擦除态 0xFFFF**, 只在本函数末尾写一次真 CRC。
	 * 真机实测 2026-10-07: 原文在此先写个 0 当"占位", 结果那个 CRC 永远写不进去 ——
	 * F1 的 Flash 编程只能把位从 1 清成 0(flash.c 的 FLASH_WriteHalfWord 是直接把半字
	 * 写到目标地址, 没有"目标非 0xFFFF 就跳过/先擦"的保护), 于是 +8 先变成 0x0000,
	 * 末尾那次写 crc 的结果只剩 "0x0000 AND crc" = 0。
	 * ⇒ 存进 Flash 的 CRC 恒为 0 ⇒ link_CalibLoad 的 crcF != crcC 永远成立、整页被判
	 * 无效 ⇒ 参数每次上电都回落 link_ParamFromSys 的源码默认值, SAVE 形同虚设
	 * (改完参数一复位就没了)。擦除后该半字本就是 0xFFFF, 无需占位。
	 * ⚠️ 也不能"末尾再写一遍 0xFFFF 补救" —— 那同样要 0→1, 同样写不进去。
	 * 门控在 MOUSE_REMOTE 之下: 比赛流程不使用 SAVE, 比赛档产物因此逐字节不变。 */
	addr += 2; /* 跳过 crc(+8), 留到本函数末尾一次写入 */
#else
	FLASH_Unlock();
	FLASH_WriteHalfWord(addr, 0); /* crc 占位(旧行为; 见上面 MOUSE_REMOTE 分支的说明) */
	addr += 2;
#endif
	FLASH_Unlock();
	FLASH_WriteHalfWord(addr, 0); /* rsvd(+10) */
	addr += 2;

	addr = FLASH_ADDR_SYSCALIB + LINK_CALIB_PAYLOAD_OFF;
	for (i = 0; i < LINK_P_NUM; i++)
	{
		FLASH_Unlock();
		FLASH_WriteHalfWord(addr, (int)linkParam[i].id);
		crc = link_CrcByte(crc, (u8)(linkParam[i].id & 0xFF));
		crc = link_CrcByte(crc, (u8)((linkParam[i].id >> 8) & 0xFF));
		addr += 2;
		FLASH_Unlock();
		FLASH_WriteHalfWord(addr, (int)((u32)linkParam[i].q & 0xFFFF));
		crc = link_CrcByte(crc, (u8)((u32)linkParam[i].q & 0xFF));
		crc = link_CrcByte(crc, (u8)(((u32)linkParam[i].q >> 8) & 0xFF));
		addr += 2;
		FLASH_Unlock();
		FLASH_WriteHalfWord(addr, (int)(((u32)linkParam[i].q >> 16) & 0xFFFF));
		crc = link_CrcByte(crc, (u8)(((u32)linkParam[i].q >> 16) & 0xFF));
		crc = link_CrcByte(crc, (u8)(((u32)linkParam[i].q >> 24) & 0xFF));
		addr += 2;
	}

	FLASH_Unlock();
	FLASH_WriteHalfWord(FLASH_ADDR_SYSCALIB + 8, (int)crc);
	FLASH_Lock();
}

/**
 * @brief  从 Flash 载入标定参数并应用
 * @param  none
 * @return 1=载入成功 0=无有效参数
 * @note   任何一项校验失败都静默返回 0: 参数页损坏等价于"未标定", 而不是加载一个错的值。
 */
static u8 link_CalibLoad(void)
{
	u32 addr;
	u32 magic;
	u16 i, cnt;
	u16 crcF, crcC = 0xFFFF;

	magic = (u32)(FLASH_ReadHalfWord(FLASH_ADDR_SYSCALIB) & 0xFFFF);
	magic |= (u32)(FLASH_ReadHalfWord(FLASH_ADDR_SYSCALIB + 2) & 0xFFFF) << 16;
	if (magic != LINK_CALIB_MAGIC)
		return 0;
	if ((u16)FLASH_ReadHalfWord(FLASH_ADDR_SYSCALIB + 4) != LINK_CALIB_VER)
		return 0;
	cnt = (u16)FLASH_ReadHalfWord(FLASH_ADDR_SYSCALIB + 6);
	if (cnt != LINK_P_NUM)
		return 0;
	crcF = (u16)FLASH_ReadHalfWord(FLASH_ADDR_SYSCALIB + 8);

	addr = FLASH_ADDR_SYSCALIB + LINK_CALIB_PAYLOAD_OFF;
	for (i = 0; i < LINK_P_NUM; i++)
	{
		linkParam[i].id = (u16)FLASH_ReadHalfWord(addr);
		crcC = link_CrcByte(crcC, (u8)(linkParam[i].id & 0xFF));
		crcC = link_CrcByte(crcC, (u8)((linkParam[i].id >> 8) & 0xFF));
		addr += 2;
		linkParam[i].q = (s32)((u32)FLASH_ReadHalfWord(addr) & 0xFFFF);
		crcC = link_CrcByte(crcC, (u8)((u32)linkParam[i].q & 0xFF));
		crcC = link_CrcByte(crcC, (u8)(((u32)linkParam[i].q >> 8) & 0xFF));
		addr += 2;
		linkParam[i].q |= (s32)(((u32)FLASH_ReadHalfWord(addr) & 0xFFFF) << 16);
		crcC = link_CrcByte(crcC, (u8)(((u32)linkParam[i].q >> 16) & 0xFF));
		crcC = link_CrcByte(crcC, (u8)(((u32)linkParam[i].q >> 24) & 0xFF));
		addr += 2;
	}
	if (crcF != crcC)
		return 0;

	for (i = 0; i < LINK_P_NUM; i++)
		link_ParamApplyOne(linkParam[i].id, linkParam[i].q);
	return 1;
}

static void link_CalibErase(void)
{
	FLASH_Unlock();
	FLASH_ErasePage(FLASH_ADDR_SYSCALIB);
	FLASH_Lock();
}


/*-------------------------------------  红外查表重建  -------------------------------------*/
/**
 * @brief  用当前 irCalib 的 8 个标定值重建四张查表
 * @param  none
 * @return none
 * @note   调用参数与 APP/calibrate.c:47-50 逐项一致(近/远距离常量 33/94/95/32/213)。
 *         刻意**不复用** CALIB_InitIr(): 后者会擦除迷宫与路径页(BLOCK/GOPATH/BACKPATH),
 *         属于破坏性副作用——标定不应该把已探索的迷宫清掉。
 *         若将来 calibrate.c 修改了这些距离常量, 此处必须同步。
 */
static void link_IrRebuild(void)
{
	IR_InfCorrect(irCalib.rawlavrgnear, 33, irCalib.rawlavrgfar, 94, 250, datal, L);
	IR_InfCorrect(irCalib.rawravrgnear, 33, irCalib.rawravrgfar, 95, 250, datar, R);
	IR_InfCorrect(irCalib.rawf1avrgnear, 32, irCalib.rawf1avrgfar, 213, 250, dataf1, F1);
	IR_InfCorrect(irCalib.rawf2avrgnear, 32, irCalib.rawf2avrgfar, 213, 250, dataf2, F2);
}

/*-------------------------------------  原始轮差累计  -------------------------------------*/
/**
 * @brief  累计 (左编码器 - 右编码器) 的原始脉冲数
 * @param  none
 * @return none
 * @note   独立于 sysCfg.encAngleRatio, 因此可作为 encAngleRatio 的标定基准,
 *         不会形成"用编码器角标编码器"的循环依赖。
 */
static void link_EncAccumulate(void)
{
	long dl = odomStr.encL - linkEncPrevL;
	long dr = odomStr.encR - linkEncPrevR;
	if (dl < -ENCODER_WRAP_THRESHOLD)
		dl += ENCODER_ARR_VALUE;
	else if (dl > ENCODER_WRAP_THRESHOLD)
		dl -= ENCODER_ARR_VALUE;
	if (dr < -ENCODER_WRAP_THRESHOLD)
		dr += ENCODER_ARR_VALUE;
	else if (dr > ENCODER_WRAP_THRESHOLD)
		dr -= ENCODER_ARR_VALUE;
	linkEncPrevL = odomStr.encL;
	linkEncPrevR = odomStr.encR;
	linkEncDiffAcc += (dl - dr);
}

/*-------------------------------------  遥测组帧  -------------------------------------*/
static u8 link_Flags(void)
{
	u8 f = 0;
	if (speedSt.freemode == 1)
		f |= LINK_FLAG_FREEMODE;
	if (speedSt.sleepmode == 1)
		f |= LINK_FLAG_SLEEP;
	if (emergMon.active == 1)
		f |= LINK_FLAG_EMERG;
	/* v2.4: LINK_FLAG_MOTOR_OFF 不再有发送方 —— "电机物理禁用"的安全档已随双档简化取消。 */
	/* 急停锁存是独立于 emergMon 的**链路层**事实: 表示"必须显式 STOP 才能再动"。 */
	if (linkEStop)
		f |= LINK_FLAG_ESTOP_LOCK;
	/* v2.0: 事件行若因 TX 空间不足被丢弃, 置 EVQ 让主机知道"事件流不完整"
	 * (设计方案 §8.3)。丢帧本身另有 dropCnt 上报。 */
	if (linkEvDrop)
		f |= LINK_FLAG_EVQ;
	/* FLAG_SCOPING/FLAG_DUMPING 在 v2.0 恒为 0 —— SCOPE 与 dump 机制已删除。 */
	return f;
}

/**
 * @brief  立即组一帧统一帧并压入 TX 环(供 `TELEM` 命令按需调用)
 * @param  none
 * @return none
 * @note   v2.1 拉取模式: **没有**"自时钟"、**没有** Hz 上限、**没有**开流开关。
 *         完全由上位机请求驱动 —— 收到一条 `TELEM` 就发一帧。
 *
 *         为什么不在这里做"链路空闲才发"的门控(那是 v2.0 的做法, 已废除):
 *         门控解决的是"固件自己决定何时发"带来的过载, 而拉取模式下**发送时机
 *         本来就由上位机掌握**, 固件只需要如实响应。真正的过载保护在 `link_SendFrame`
 *         里 —— 它发现 TX 环空间不足就**整帧丢弃并计数**(linkDrop, 随帧上报),
 *         于是上位机能在帧里看到丢弃, 自行收窄请求窗口。这一条"丢弃而不是阻塞"
 *         是必须的: 阻塞会让命令处理停下, 而命令处理在 1ms 中断里。
 */
static void link_SendUnified(void)
{
	u8 pl[LINK_UNIFIED_LEN];
	u8 sa, sb, sc;
	u8 fr, sp, sm;
	long dmin;

	/* ---- 测量量 ----
	 * 时间戳取"组帧这一刻"的毫秒时基低 8 位。因为现在是请求驱动, 帧的发送时刻
	 * 与请求到达时刻只差一个 tick, 所以 ms8 与同一次 `TELEM` 应答里的完整 upMs
	 * 天然对齐 —— 上位机拿 ACK 的锚还原绝对时刻即可。 */
	pl[LINK_U_MS8] = (u8)(linkTimeMs & 0xFFu);

	link_Put16(&pl[LINK_U_IRADC + 0], link_Sat16((long)irCalib.rawf1));
	link_Put16(&pl[LINK_U_IRADC + 2], link_Sat16((long)irCalib.rawf2));
	link_Put16(&pl[LINK_U_IRADC + 4], link_Sat16((long)irCalib.rawl));
	link_Put16(&pl[LINK_U_IRADC + 6], link_Sat16((long)irCalib.rawr));

	link_Put16(&pl[LINK_U_IRMM + 0], link_Sat16(irDist.f1));
	link_Put16(&pl[LINK_U_IRMM + 2], link_Sat16(irDist.f2));
	link_Put16(&pl[LINK_U_IRMM + 4], link_Sat16(irDist.l));
	link_Put16(&pl[LINK_U_IRMM + 6], link_Sat16(irDist.r));

	link_Put16(&pl[LINK_U_BATTMV], link_Sat16((long)battMon.mv));
	link_Put16(&pl[LINK_U_SPEEDL], link_Sat16(speedSt.speedL));
	link_Put16(&pl[LINK_U_SPEEDR], link_Sat16(speedSt.speedR));
	link_Put16(&pl[LINK_U_GYRO], link_Sat16(gyroStr.angle));
	link_Put16(&pl[LINK_U_ENCANG], link_Sat16(odomStr.angle));
	link_Put16(&pl[LINK_U_TRACKL], link_Sat16(odomStr.trackL));
	link_Put16(&pl[LINK_U_TRACKR], link_Sat16(odomStr.trackR));
	link_Put16(&pl[LINK_U_CELLODOM], link_Sat16(odomStr.cellOdom));
	link_Put16(&pl[LINK_U_PIDERR], link_Sat16(motionCtrl.pidErr));

	/* ---- 状态机(位打包) ----
	 * 注: motionTask 与 mazeTask 是**两个不同**的状态机 ——
	 *     前者是"正在执行的运动动作"(core.h 的 MOTION_TASK_*), 后者是迷宫任务机。
	 * motionStep 裁到 4 bit(0..15), 超出截断 —— 见设计方案 §13.2 未决项。 */
	sa = (u8)(((u8)mazeState.mouseDirection & 0x03u)
	          | (((u8)mazeState.mouseTask & 0x07u) << 2)
	          | (wallSns.left ? 0x20u : 0u)
	          | (wallSns.front ? 0x40u : 0u)
	          | (wallSns.right ? 0x80u : 0u));
	sb = (u8)(((u8)motionCtrl.task & 0x7Fu) | (wallSns.irSwitch ? 0x80u : 0u));
	sc = (u8)(((u8)motionCtrl.step & 0x0Fu)
	          | (wallSns.critL ? 0x10u : 0u)
	          | (wallSns.critR ? 0x20u : 0u)
	          | (wallSns.left45 ? 0x40u : 0u)
	          | (wallSns.right45 ? 0x80u : 0u));

	pl[LINK_U_STATE_A] = sa;
	pl[LINK_U_STATE_B] = sb;
	pl[LINK_U_STATE_C] = sc;
	pl[LINK_U_FLAGS] = link_Flags();
	/* posX/posY 各 4 bit; goalX/goalY 同为 char(初值 7), 4 bit 覆盖 0..15 */
	pl[LINK_U_POS] = (u8)(((u8)mazeState.mousePos.cX & 0x0Fu)
	                      | (((u8)mazeState.mousePos.cY & 0x0Fu) << 4));
	pl[LINK_U_GOAL] = (u8)(((u8)mazeState.goalX & 0x0Fu)
	                       | (((u8)mazeState.goalY & 0x0Fu) << 4));
	/* 运行时侧墙阈值: 被 state.c:225(->140) 与 motion.c:418(->110) 改写,
	 * 与参数 IRTHRWALL(160) 不是一回事 —— 不传就无法解释侧墙判定, 故必须进帧。 */
	pl[LINK_U_WALLTHR] = wallSns.wallThresh;
	pl[LINK_U_STACKIDX] = mazeState.stackIdx;
	pl[LINK_U_FASTWAY] = mazeState.fastwayCount;
	pl[LINK_U_DROP] = (u8)(linkDrop & 0xFFu);

	/* ---- v2.3 新增: 纯只读监视量 ----
	 * 这一段**不改变任何决策**, 只把固件内部已经能区分、但接口上看不到的量搬出来。 */

	/* 前墙双档(*派生量*): 原 `wallSns.front` 由 ISR 按"4 个条件取或"写死,
	 * 这里不再碰它, 只用同一组阈值把结果**拆成近端/远端**:
	 *     frontNear = 有墙 且 min(f1,f2) < thrFrontNear   (贴脸, 必须处理)
	 *     frontFar  = 有墙 且 min(f1,f2) >= thrFrontNear  (提前量, 可用于提前决策)
	 * 恒等式 frontNear | frontFar == wallSns.front(两者互斥), 因此
	 * maze.c/isr.c/motion.c/lesson.c 里 5 个读 `wallSns.front` 的点**一行都不用改**。
	 * 另把原式①②的"双传感器互证"单独记一位(frontBoth), 便于上位机区分
	 * "两路都看到墙"与"单路贴脸兜底"。 */
	dmin = (irDist.f1 < irDist.f2) ? irDist.f1 : irDist.f2;
	fr = 0u;
	if (wallSns.front)
	{
		if (dmin < (long)irCfg.thrFrontNear)
			fr |= LINK_FRONT_NEAR;
		else
			fr |= LINK_FRONT_FAR;
	}
	if ((irDist.f1 < (long)irCfg.thrFrontLo && irDist.f2 < (long)irCfg.thrFrontHi)
	    || (irDist.f2 < (long)irCfg.thrFrontMid && irDist.f1 < (long)irCfg.thrFrontHi))
		fr |= LINK_FRONT_BOTH;
	pl[LINK_U_FRONT] = fr;

	/* 比赛冲刺选档/状态(只读镜像; 这些量在比赛固件 [5] 里才有意义,
	 * 测试档不跑冲刺时恒为 0/未选, 上位机据此判断"现在是不是在冲刺、用哪一档")。 */
	sm = 0u;
	if (spurtmode == SPURT_SPEED_90_LOW)
		sm = 1u;
	else if (spurtmode == SPURT_SPEED_45_LOW)
		sm = 2u;
	else if (spurtmode == SPURT_SPEED_MEDIUM)
		sm = 3u;
	else if (spurtmode == SPURT_SPEED_HIGH)
		sm = 4u;
	sp = (u8)(sm & 0x07u);
	if (taskSt.spurtFlag)
		sp |= LINK_SPURTBF_FLAG;
	if (taskSt.goOrBack)
		sp |= LINK_SPURTBF_GOBACK;
	if (mazeState.spurtTestFlag)
		sp |= LINK_SPURTBF_TEST;
	if (mazeState.objectGoToFlag)
		sp |= LINK_SPURTBF_OBJECT;
	pl[LINK_U_SPURT] = sp;

	pl[LINK_U_GOAHEAD] = taskSt.goaheadnum;
	pl[LINK_U_FASTWAY90] = mazeState.fastwayCount90;

	link_SendFrame(LINK_TYPE_UNIFIED, pl, LINK_UNIFIED_LEN);
}

/*-------------------------------------  运动服务  -------------------------------------*/
/**
 * @brief  取四路红外中的最小读数
 * @return 最小读数(mm, 已钳到 >= 0)
 * @note   四路一起看而不是只看前方两路: 轮子悬空或侧向贴障碍时侧红外同样能反映
 *         "快贴上了"; 本实验要证明的正是"任何一路触发都能停"。
 *         (v2.0: 原先还要输出"哪一路最小"用于明细行, 明细取消后不再需要。)
 */
static long link_MinIr(void)
{
	long v = irDist.f1;
	long t;

	t = irDist.f2;
	if (t < v)
		v = t;
	t = irDist.l;
	if (t < v)
		v = t;
	t = irDist.r;
	if (t < v)
		v = t;
	if (v < 0)
		v = 0;
	return v;
}

/**
 * @brief  结束一次 MOVE RUN: 停车 + 上报停止原因
 * @param  reason  停止原因(LINK_RUN_REASON_*)
 * @return none
 * @note   v2.0 起只上报"**为什么停**"这一条信息, 形态是事件行 `STOPPED <reason> #n`。
 *           为什么只剩 reason: "时间到"与"红外触发"在统一帧的 flags/状态位里
 *           **区分不出**(两者停下后都表现为目标速度归零), 所以必须靠事件通知;
 *           而 elapsed / ir_min / dL / dR 这些量在 217Hz 的连续帧序列里都能读到,
 *           再塞进一条不可靠的长文本只是浪费带宽。
 *         停车动作完全不依赖上位机: 目标速度归零 + freemode=1 在 1ms ISR 里完成。
 */
static void link_MoveFinishRun(const char *reason)
{
	char head[24];
	u8 i = 0;
	const char *t;

	speedSt.speedTargetL = 0;
	speedSt.speedTargetR = 0;
	speedSt.freemode = 1;
	linkMove.state = LINK_MOVE_NONE;

	t = "STOPPED ";
	while (*t && i < (u8)(sizeof(head) - 1))
		head[i++] = *t++;
	t = reason;
	while (*t && i < (u8)(sizeof(head) - 1))
		head[i++] = *t++;
	head[i] = '\0';
	link_EvLine(head);
}

/**
 * @brief  停车并清掉所有运动/标定子状态(STOP 与 ESTOP 共用)
 * @param  runReason 若当时正在 MOVE RUN, 用它上报停止原因
 * @return none
 */
static void link_StopMotion(const char *runReason)
{
	if (linkMove.state == LINK_MOVE_RUN)
		link_MoveFinishRun(runReason);

	speedSt.speedTargetL = 0;
	speedSt.speedTargetR = 0;
	speedSt.freemode = 1;
	motionCtrl.task = MOTION_TASK_NULL;
	linkMove.state = LINK_MOVE_NONE;
	linkSrv.state = LINK_SRV_NONE;
	linkCal.active = 0;
	linkCal.needSample = 0;
	irCalib.calibMode = 0;

}

/**
 * @brief  固定脉冲直线 / 定长原地转 / 受双护栏约束的远程运动 的状态机
 * @param  none
 * @return none
 * @note   写 speedSt.speedTargetL/R 之前必须把 motionCtrl.task 置 MOTION_TASK_NULL,
 *         否则各 doTask* 会在每个 1ms 周期覆盖目标速度。
 */
static void link_MoveService(void)
{
	long dL, dR;
	long ir;

	if (linkMove.state == LINK_MOVE_NONE)
		return;

	/* ================= MOVE RUN: 时间 + 红外 双护栏 ================= */
	if (linkMove.state == LINK_MOVE_RUN)
	{
		linkMove.elapsed++;
		ir = link_MinIr();

		/* ---- 护栏 1: 红外贴脸必停 ---- */
		if (linkMove.irMm && (u32)ir < linkMove.irMm)
		{
			link_MoveFinishRun(LINK_RUN_REASON_IR);
			return;
		}

		/* ---- 护栏 2: 到时必停 ---- */
		if (linkMove.elapsed >= linkMove.limit)
		{
			link_MoveFinishRun(LINK_RUN_REASON_TIME);
			return;
		}
		return;
	}

	if (linkMove.state == LINK_MOVE_FWD)
	{
		dL = odomStr.distL + odomStr.distLCycle * ENCODER_ARR_VALUE - linkMove.pStartL;
		dR = odomStr.distR + odomStr.distRCycle * ENCODER_ARR_VALUE - linkMove.pStartR;
		if (dL < 0)
			dL = -dL;
		if (dR < 0)
			dR = -dR;
		if (dL >= (long)linkMove.target || dR >= (long)linkMove.target)
		{
			speedSt.speedTargetL = 0;
			speedSt.speedTargetR = 0;
			speedSt.freemode = 1;
			linkMove.state = LINK_MOVE_NONE;
			link_EvLine("STOPPED END");
		}
	}
	else
	{
		linkMove.elapsed++;
		if (linkMove.elapsed >= linkMove.limit)
		{
			speedSt.speedTargetL = 0;
			speedSt.speedTargetR = 0;
			speedSt.freemode = 1;
			linkMove.state = LINK_MOVE_NONE;
			link_EvLine("STOPPED END");
		}
	}
}

/**
 * @brief  角度基准扫描: 用"前向红外正对墙"的极小值作为物理角度基准
 * @param  none
 * @return none
 * @note   这是**非循环依赖**的关键: 极小值由红外几何决定, 与陀螺系数/编码器系数无关,
 *         因此可用它标定 gyroRatio 与 encAngleRatio(方案文档 §6.3)。
 *         相邻两个极小相差 90 度; 往复 LINK_SRV_REPS 次, 累计 900 度, 把 ±2 度的
 *         极值定位误差均摊到约 0.2%。
 *         要求放在迷宫角格(相邻两面互相垂直的墙), 否则找不到两个相邻极小。
 */
static void link_ServoService(void)
{
	long v;
	s32 dAcc, dEnc;

	if (linkSrv.state == LINK_SRV_NONE)
		return;

	linkSrv.elapsed++;
	if (linkSrv.elapsed > LINK_SRV_MAXMS)
	{
		speedSt.speedTargetL = 0;
		speedSt.speedTargetR = 0;
		speedSt.freemode = 1;
		linkSrv.state = LINK_SRV_NONE;
		/* v2.0: 不再上行 `CALERR GYRORATIO TIMEOUT` 事件行; 由 link_CalibService
		 * 在下一拍看到 state==NONE 时以 link_CalAck(0) 回 `ERR_TIMEOUT`。 */
		return;
	}

	if (linkSrv.guard)
	{
		linkSrv.guard--;
		return;
	}

	v = irDist.f1 + irDist.f2;
	if (v < linkSrv.bestV)
	{
		linkSrv.bestV = v;
		linkSrv.bestAcc = gyroStr.angleAcc;
		linkSrv.bestEnc = linkEncDiffAcc;
		linkSrv.rise = 0;
	}
	else
	{
		linkSrv.rise++;
	}

	if (linkSrv.rise < LINK_SRV_RISE_MS)
		return;

	/* ---- 找到一个极小 ---- */
	if (linkSrv.havePrev)
	{
		dAcc = linkSrv.bestAcc - linkSrv.prevAcc;
		dEnc = linkSrv.bestEnc - linkSrv.prevEnc;
		if (dAcc < 0)
			dAcc = -dAcc;
		if (dEnc < 0)
			dEnc = -dEnc;
		linkSrv.sumAcc += dAcc;
		linkSrv.sumEnc += dEnc;
		linkSrv.reps++;
	}
	linkSrv.prevAcc = linkSrv.bestAcc;
	linkSrv.prevEnc = linkSrv.bestEnc;
	linkSrv.havePrev = 1;
	linkSrv.bestV = 0x7FFFFFFFL;
	linkSrv.rise = 0;
	linkSrv.guard = LINK_SRV_GUARD_MS;

	if (linkSrv.reps >= LINK_SRV_REPS)
	{
		speedSt.speedTargetL = 0;
		speedSt.speedTargetR = 0;
		speedSt.freemode = 1;
		linkSrv.state = LINK_SRV_NONE;
		/* v2.0: 取代 `CALDONE GYRORATIO <sumAcc> <sumEnc> <deg>` 事件行 ——
		 * 三个结果值直接进 `CAL GYRORATIO START` 的挂起应答(ACK 附加字段)。
		 * 上位机据此算 gyroRatio = sumAcc/deg、encAngleRatio = sumEnc/deg。 */
		if (linkCal.ackSeq >= 0)
		{
			link_AckI3(linkCal.ackSeq, "OK", linkSrv.sumAcc, linkSrv.sumEnc,
			           (s32)LINK_SRV_REPS * 90);
			linkCal.ackSeq = -1;
		}
		linkCal.active = 0;
		return;
	}

	/* 反向继续找下一个极小(往复) */
	linkSrv.dir = (s16)(-linkSrv.dir);
	speedSt.speedTargetL = linkSrv.dir * LINK_SRV_SPEED;
	speedSt.speedTargetR = -(linkSrv.dir * LINK_SRV_SPEED);
}

/*-------------------------------------  标定服务  -------------------------------------*/
/**
 * @brief  标定项的统一非阻塞状态机(蓝牙链路与按键菜单可共用)
 * @param  item 标定项(LINK_CAL_*)
 * @param  act  LINK_ACT_START/STEP/DONE/ABORT
 * @return 1=该项已完成 0=进行中
 * @note   IRDATA 必须由上位机逐点触发: START 后先发 SAMPLE 采第 1 点, 摆好位置再发 SAMPLE
 *         采下一点, 共 4 点。其余各项 START 后自动采集。
 */
static u8 link_CalibService(u8 item, u8 act)
{
	if (act == LINK_ACT_ABORT)
	{
		linkCal.active = 0;
		linkCal.needSample = 0;
		speedSt.speedTargetL = 0;
		speedSt.speedTargetR = 0;
		speedSt.freemode = 1;
		linkSrv.state = LINK_SRV_NONE;
		linkMove.state = LINK_MOVE_NONE;
		irCalib.calibMode = 0;
		return 1;
	}

	if (act == LINK_ACT_START)
	{
		linkCal.item = item;
		/* IRDATA 的相位由命令 `CAL IRDATA <n>` 给定, 在这里**不能**清零;
		 * 其余四项只有一个相位(start=0)。 */
		if (item != LINK_CAL_IRDATA)
			linkCal.phase = 0;
		linkCal.samples = 0;
		linkCal.acc = 0;
		linkCal.acc2 = 0;
		linkCal.timeout = 0;
		linkCal.active = 1;
		linkCal.needSample = 1;
		motionCtrl.task = MOTION_TASK_NULL;
		speedSt.speedTargetL = 0;
		speedSt.speedTargetR = 0;
		wallSns.irSwitch = 1;

		switch (item)
		{
		case LINK_CAL_GYRO:
			gyroStr.correctionEnable = 1; /* 关闭"编码器角度稳定则清零陀螺"的干扰 */
			gyroStr.adjCounter = 0;
			gyroStr.adjFlag = 1;
			speedSt.freemode = 1;
			break;
		case LINK_CAL_WALLDIST:
			speedSt.freemode = 1;
			break;
		case LINK_CAL_IRDATA:
			irCalib.calibMode = 1; /* 滑动平均而非查表 */
			speedSt.freemode = 1;
			/* v2.0: 不再"等上位机逐点 SAMPLE" —— 收到 `CAL IRDATA <n>` 即开始采这一相位,
			 * 采够 LINK_IR_SAMPLE_N 个点回一次 ACK(见下方 IRDATA 分支)。 */
			linkCal.needSample = 1;
			break;
		case LINK_CAL_GYRORATIO:
		case LINK_CAL_ENCANGLE:
			gyroStr.correctionEnable = 1;
			gyroStr.adjFlag = 0;
			speedSt.freemode = 0;
			linkSrv.state = LINK_SRV_SWEEP;
			linkSrv.guard = LINK_SRV_GUARD_MS;
			linkSrv.rise = 0;
			linkSrv.elapsed = 0;
			linkSrv.bestV = 0x7FFFFFFFL;
			linkSrv.havePrev = 0;
			linkSrv.reps = 0;
			linkSrv.sumAcc = 0;
			linkSrv.sumEnc = 0;
			linkSrv.dir = 1;
			speedSt.speedTargetL = LINK_SRV_SPEED;
			speedSt.speedTargetR = -LINK_SRV_SPEED;
			break;
		default:
			speedSt.freemode = 1;
			break;
		}
		return 0;
	}

	/* ---- 显式采样触发(IRDATA 逐点采集) ---- */
	if (act == LINK_ACT_SAMPLE)
	{
		linkCal.samples = 0;
		linkCal.acc = 0;
		linkCal.acc2 = 0;
		linkCal.needSample = 1;
		return 0;
	}

	if (!linkCal.active)
		return 1;

	if (linkCal.item != LINK_CAL_GYRORATIO && linkCal.item != LINK_CAL_ENCANGLE)
	{
		linkCal.timeout++;
		if (linkCal.timeout > LINK_CAL_TIMEOUT)
		{
			linkCal.active = 0;
			linkCal.needSample = 0;
			speedSt.freemode = 1;
			irCalib.calibMode = 0;
			/* 取代旧的 `CALERR <item> TIMEOUT` 事件行 ——
			 * ACK 有序号 + 重发机制, 不会被 TX 空间门悄悄丢掉。 */
			link_CalAck(0);
			return 1;
		}
	}
	else
	{
		if (linkSrv.state == LINK_SRV_NONE)
		{
			/* 成功路径已由 link_ServoService 以 link_AckI3 回掉(ackSeq 置 -1),
			 * 故这里的 CalAck(0) 只会覆盖"扫描超时"那条路径 -> `ERR_TIMEOUT`。 */
			link_CalAck(0);
			linkCal.active = 0;
			return 1;
		}
		return 0;
	}

	/* ---- 陀螺零偏: 由 GYRO_Sample 内部自算, 这里只等它就绪并落盘 ---- */
	if (linkCal.item == LINK_CAL_GYRO)
	{
		if (gyroStr.adjCounter > 1500)
		{
			gyroStr.adjFlag = 0;
			gyroStr.correctionEnable = 0;
			FLASH_Unlock();
			FLASH_ErasePage(FLASH_ADDR_GYRO0);
			FLASH_Unlock();
			FLASH_WriteHalfWord(FLASH_ADDR_GYRO0, gyroStr.bias);
			linkCal.active = 0;
			link_CalAck(1); /* 取代 `CALDONE GYRO <bias>`; 结果用 GET 读 */
			return 1;
		}
		return 0;
	}

	/* ---- 车心到墙距离: 静止时取左红外稳定均值 ---- */
	if (linkCal.item == LINK_CAL_WALLDIST)
	{
		if (!linkCal.needSample)
			return 0;
		linkCal.acc += irDist.l;
		linkCal.samples++;
		if (linkCal.samples >= LINK_IR_SAMPLE_N)
		{
			s32 avg = linkCal.acc / (long)linkCal.samples;
			link_ParamSet(LINK_P_WALLDIST, avg << 16);
			linkCal.active = 0;
			linkCal.needSample = 0;
			link_CalAck(1); /* 取代 `CALDONE WALLDIST <avg>`; 结果用 GET 读 */
			return 1;
		}
		return 0;
	}

	/* ---- 红外 8 值: 4 个位置逐点采样 ---- */
	if (linkCal.item == LINK_CAL_IRDATA)
	{
		if (!linkCal.needSample)
			return 0;
		switch (linkCal.phase)
		{
		case 0: /* 左近墙 */
			linkCal.acc += irCalib.rawl;
			break;
		case 1: /* 左远墙 */
			linkCal.acc += irCalib.rawl;
			break;
		case 2: /* 前近墙 */
			linkCal.acc += irCalib.rawf1;
			linkCal.acc2 += irCalib.rawf2;
			break;
		default: /* 前远墙 */
			linkCal.acc += irCalib.rawf1;
			linkCal.acc2 += irCalib.rawf2;
			break;
		}
		linkCal.samples++;
		if (linkCal.samples < LINK_IR_SAMPLE_N)
			return 0;

		switch (linkCal.phase)
		{
		case 0:
			irCalib.rawlavrgnear = (int)(linkCal.acc / (long)linkCal.samples);
			irCalib.rawravrgfar = irCalib.rawr; /* 与 calibrate.c 步骤1 的配对方式一致 */
			break;
		case 1:
			irCalib.rawlavrgfar = (int)(linkCal.acc / (long)linkCal.samples);
			irCalib.rawravrgnear = irCalib.rawr;
			break;
		case 2:
			irCalib.rawf1avrgnear = (int)(linkCal.acc / (long)linkCal.samples);
			irCalib.rawf2avrgnear = (int)(linkCal.acc2 / (long)linkCal.samples);
			break;
		default:
			irCalib.rawf1avrgfar = (int)(linkCal.acc / (long)linkCal.samples);
			irCalib.rawf2avrgfar = (int)(linkCal.acc2 / (long)linkCal.samples);
			break;
		}
		linkCal.samples = 0;
		linkCal.acc = 0;
		linkCal.acc2 = 0;
		linkCal.needSample = 0;
		/* v2.0: 一个相位 = 一条命令。采够就回 ACK(取代 `CALSTEP IRDATA <n>`),
		 * 上位机收到 ACK 再摆下一个位置并发下一条 `CAL IRDATA <n+1>`。 */
		if (linkCal.phase < 3)
		{
			linkCal.active = 0;
			link_CalAck(1);
			return 1;
		}

		/* ---- 最后一个相位: 4 点齐了, 重建查表 + 写既有 page123 ---- */
		link_IrRebuild();
		irCalib.calibMode = 0;
		{
			u32 a = FLASH_ADDR_IRDATA;
			FLASH_Unlock();
			FLASH_ErasePage(FLASH_ADDR_IRDATA);
			FLASH_Unlock();
			FLASH_WriteHalfWord(a, irCalib.rawf1avrgnear);
			a += 2;
			FLASH_Unlock();
			FLASH_WriteHalfWord(a, irCalib.rawf1avrgfar);
			a += 2;
			FLASH_Unlock();
			FLASH_WriteHalfWord(a, irCalib.rawf2avrgnear);
			a += 2;
			FLASH_Unlock();
			FLASH_WriteHalfWord(a, irCalib.rawf2avrgfar);
			a += 2;
			FLASH_Unlock();
			FLASH_WriteHalfWord(a, irCalib.rawlavrgnear);
			a += 2;
			FLASH_Unlock();
			FLASH_WriteHalfWord(a, irCalib.rawlavrgfar);
			a += 2;
			FLASH_Unlock();
			FLASH_WriteHalfWord(a, irCalib.rawravrgnear);
			a += 2;
			FLASH_Unlock();
			FLASH_WriteHalfWord(a, irCalib.rawravrgfar);
		}
		linkCal.active = 0;
		link_CalAck(1); /* 最后一步的 ACK 即"整套完成" */
		return 1;
	}

	return 0;
}

/*-------------------------------------  墙图回传  -------------------------------------*/
static void link_SendMap(void)
{
	u8 pl[LINK_MAP_LEN];
	u16 o = 0;
	u8 x, y;
	pl[o++] = (u8)mazeState.mousePos.cX;
	pl[o++] = (u8)mazeState.mousePos.cY;
	pl[o++] = mazeState.mouseDirection;
	pl[o++] = mazeState.mouseTask;
	for (x = 0; x < MAZETYPE; x++)
		for (y = 0; y < MAZETYPE; y++)
			pl[o++] = mazeState.mapWall[x][y];
	for (x = 0; x < MAZETYPE; x++)
		for (y = 0; y < MAZETYPE; y++)
			pl[o++] = mazeState.mapVisited[x][y];
	link_SendFrame(LINK_TYPE_MAP, pl, LINK_MAP_LEN);
}

/*-------------------------------------  命令解析工具  -------------------------------------*/
static char *link_SkipSp(char *p)
{
	while (*p == ' ' || *p == '\t')
		p++;
	return p;
}

/**
 * @brief  匹配并消费一个 token(以空格/制表/结束为界)
 * @param  p 指针的指针
 * @param  tok 待匹配 token
 * @return 1=匹配 0=不匹配(不推进指针)
 */
static u8 link_TokIs(char **p, const char *tok)
{
	u8 n = 0;
	char *q;
	*p = link_SkipSp(*p);
	q = *p;
	while (tok[n])
	{
		if (q[n] != tok[n])
			return 0;
		n++;
	}
	if (q[n] != ' ' && q[n] != '\t' && q[n] != '\0')
		return 0;
	*p = q + n;
	return 1;
}

static s32 link_TokNum(char **p)
{
	s32 v = 0;
	u8 neg = 0;
	*p = link_SkipSp(*p);
	if (**p == '-')
	{
		neg = 1;
		(*p)++;
	}
	while (**p >= '0' && **p <= '9')
	{
		v = v * 10 + (s32)(**p - '0');
		(*p)++;
	}
	return neg ? -v : v;
}

/**
 * @brief  解析 Q16.16 定点数, 支持 "53.2015" 这类十进制写法
 * @param  p 指针的指针
 * @param  out 输出 Q16.16
 * @return 1=成功 0=格式错误或超范围
 * @note   全程纯整数运算(禁止 atof: 在 F1 上是毫秒级软件浮点, 不能进 ISR)。
 *         小数部分最多 4 位, 保证 fp*65536 不溢出 u32(9999*65536≈6.55e8 < 4.29e9)。
 *         需要完整 6 位小数精度时请改用 SETQ 直接下发 Q16.16 整数。
 */
static u8 link_TokQ16(char **p, s32 *out)
{
	char *q;
	u8 fd = 0, neg = 0;
	u32 ip = 0, fp = 0, div = 1, mul;
	*p = link_SkipSp(*p);
	q = *p;
	if (*q == '-')
	{
		neg = 1;
		q++;
	}
	if (*q < '0' || *q > '9')
		return 0;
	while (*q >= '0' && *q <= '9')
	{
		ip = ip * 10u + (u8)(*q - '0');
		q++;
	}
	if (*q == '.')
	{
		q++;
		while (*q >= '0' && *q <= '9' && fd < 4)
		{
			fp = fp * 10u + (u8)(*q - '0');
			div *= 10u;
			fd++;
			q++;
		}
	}
	if (ip > 32767u)
		return 0;
	mul = ip << 16;
	if (div > 1u)
		mul += (fp * 65536u) / div;
	*out = neg ? -(s32)mul : (s32)mul;
	*p = q;
	return 1;
}

/**
 * @brief  取下一个空格分隔的 token(就地以 '\0' 结尾)
 * @param  p 指针的指针
 * @return token 起始地址
 */
static char *link_TokWord(char **p)
{
	char *q;
	*p = link_SkipSp(*p);
	q = *p;
	while (**p && **p != ' ' && **p != '\t')
		(*p)++;
	if (**p)
	{
		**p = '\0';
		(*p)++;
	}
	return q;
}

/*-------------------------------------  应答  -------------------------------------*/
/**
 * @brief  回复一条文本应答, 并记录状态供幂等重放
 * @param  seq 命令序号(可为负, 表示未提供)
 * @param  status 状态串(OK / ERR_xxx)
 * @return none
 * @note   记录状态是为了让"重发同一命令"时直接重放上次结果, 而不会重复执行副作用
 *         (例如重复写参数、重复触发标定动作)。
 */
static void link_Ack(s32 seq, const char *status)
{
	u8 i;
	link_TxStr("ACK ");
	link_TxInt(seq);
	link_TxPut(' ');
	link_TxStr(status);
	link_TxStr("\r\n");
	if (seq >= 0)
	{
		for (i = 0; i < 13 && status[i]; i++)
			linkLastStatus[i] = (u8)status[i];
		linkLastStatus[i] = 0;
	}
}

/**
 * @brief  回复一条"带一个整数值"的文本应答(格式: ACK <seq> <status> <value>)
 * @param  seq 命令序号(可为负, 表示未提供)
 * @param  status 状态串
 * @param  value 附加整数值
 * @return none
 * @note   用于两处带"时刻"的应答: `TELEM`(回时基作为时间锚)与 `ARM`(回发车时刻)。
 *         因为锚必须**每次新鲜**, 本函数不记录 linkLastStatus ——
 *         调用者须自行清 linkLastSeq 让幂等失效(见各自的调用点)。
 */
static void link_AckU(s32 seq, const char *status, s32 value)
{
	link_TxStr("ACK ");
	link_TxInt(seq);
	link_TxPut(' ');
	link_TxStr(status);
	link_TxPut(' ');
	link_TxInt(value);
	link_TxStr("\r\n");
}

/**
 * @brief  回复一条"带三个整数值"的文本应答(格式: ACK <seq> <status> <a> <b> <c>)
 * @param  seq 命令序号
 * @param  status 状态串
 * @param  a,b,c 三个附加整数值
 * @return none
 * @note   v2.0: 角度标定(GYRORATIO/ENCANGLE)的 sweep 结果(sumAcc/sumEnc/deg)原本
 *         走 `CALDONE GYRORATIO ...` 事件行; 事件行在 TX 空间不足时会被丢弃, 且与
 *         "标定改请求-响应"的原则冲突。改为直接进挂起命令的 ACK 附加字段。
 *         与 link_AckU 一样**不**记录 linkLastStatus(调用者需自清 linkLastSeq)。
 */
static void link_AckI3(s32 seq, const char *status, s32 a, s32 b, s32 c)
{
	link_TxStr("ACK ");
	link_TxInt(seq);
	link_TxPut(' ');
	link_TxStr(status);
	link_TxPut(' ');
	link_TxInt(a);
	link_TxPut(' ');
	link_TxInt(b);
	link_TxPut(' ');
	link_TxInt(c);
	link_TxStr("\r\n");
}

/**
 * @brief  消费挂起的标定应答(见 LinkCalState.ackSeq)
 * @param  ok 1 -> `OK`; 0 -> `ERR_TIMEOUT`
 * @return none
 * @note   v2.0 的请求-响应标定靠这一条闭合: 命令到达时不立刻 ACK,
 *         而是把 seq 存进 linkCal.ackSeq, 等采集真的完成/超时再由本函数回一次。
 */
static void link_CalAck(u8 ok)
{
	if (linkCal.ackSeq >= 0)
	{
		link_Ack(linkCal.ackSeq, ok ? "OK" : "ERR_TIMEOUT");
		linkCal.ackSeq = -1;
	}
}

/** @brief 本次上电之前的复位原因名(见 LINK_Init; 由 PING 上报) */
static const char *link_ResetCauseName(void)
{
	switch (linkResetCause)
	{
	case 2:
		return "IWDG";
	case 1:
		return "SW";
	case 3:
		return "PIN";
	default:
		return "POR";
	}
}

/*-------------------------------------  命令分发  -------------------------------------*/
static u8 link_CalItemToId(const char *s)
{
	if (!strcmp(s, "GYRO"))
		return LINK_CAL_GYRO;
	if (!strcmp(s, "WALLDIST"))
		return LINK_CAL_WALLDIST;
	if (!strcmp(s, "IRDATA"))
		return LINK_CAL_IRDATA;
	if (!strcmp(s, "GYRORATIO"))
		return LINK_CAL_GYRORATIO;
	if (!strcmp(s, "ENCANGLE"))
		return LINK_CAL_ENCANGLE;
	return 0;
}

/**
 * @brief  执行一条已通过 CRC 校验的命令
 * @param  line 命令部分(已去掉 *crc 与 #seq)
 * @param  seq  命令序号(无则 -1)
 * @return none
 * @note   所有应答都是文本行; 二进制只用于遥测上行, 两者互不干扰。
 */
static void link_ExecCmd(char *line, s32 seq)
{
	char *p = line;
	char *w;
	u16 id;
	s32 v;

	/* ================= PING(含协议/固件版本 + 复位原因) =================
	 * v2.0: 旧的独立 `VER` 命令删除 —— 它的字段与 PING 完全重复。
	 * 应答: `PONG <proto> <fw> <mazetype> <txbuf> <rst>`
	 *   rst ∈ {POR, SW, IWDG, PIN} —— 见设计方案 §10.2。PING 通常是连接后的
	 *   第一条命令, 所以上位机一连上就知道"设备刚复位过 / 是被看门狗复位过"。 */
	if (link_TokIs(&p, "PING"))
	{
		link_TxStr("PONG ");
		link_TxInt(LINK_PROTO_VER);
		link_TxPut(' ');
		link_TxInt(LINK_FW_VER);
		link_TxPut(' ');
		link_TxInt(MAZETYPE);
		link_TxPut(' ');
		link_TxInt(LINK_TX_BUF_SIZE);
		link_TxPut(' ');
		link_TxStr(link_ResetCauseName());
		link_TxStr("\r\n");
		link_Ack(seq, "OK");
		return;
	}

	/* ================= GET ================= */
	if (link_TokIs(&p, "GET"))
	{
		w = link_TokWord(&p);
		if (!strcmp(w, "ALL"))
		{
			u16 i;
			for (i = 0; i < LINK_P_NUM; i++)
			{
				link_TxStr("P ");
				link_TxInt(linkParam[i].id);
				link_TxPut(' ');
				link_TxInt(linkParam[i].q);
				link_TxStr("\r\n");
			}
			link_Ack(seq, "OK");
			return;
		}
		if (!link_NameToId(w, &id))
		{
			link_Ack(seq, "ERR_NAME");
			return;
		}
		{
			u16 idx;
			if (link_ParamFind(id, &idx))
			{
				link_TxStr("P ");
				link_TxInt(id);
				link_TxPut(' ');
				link_TxInt(linkParam[idx].q);
				link_TxStr("\r\n");
				link_Ack(seq, "OK");
			}
			else
			{
				link_Ack(seq, "ERR_NAME");
			}
		}
		return;
	}

	/* ================= SETQ: 直接下发 Q16.16 整数(上位机用, 无精度损失) ================= */
	if (link_TokIs(&p, "SETQ"))
	{
		w = link_TokWord(&p);
		if (!link_NameToId(w, &id))
		{
			link_Ack(seq, "ERR_NAME");
			return;
		}
		v = link_TokNum(&p);
		if (!link_ParamSet(id, v)) /* v 即 Q16.16 原始整数(如 53.2015 -> 3486565) */
		{
			link_Ack(seq, "ERR_NAME");
			return;
		}
		link_Ack(seq, "OK");
		return;
	}

	/* ================= SET: 十进制写法(人手敲用, 最多 4 位小数) =================
	 * 注意 link_TokIs 只在整个 token 匹配时才推进指针, 因此 "SETQ ..." 不会误入此分支。 */
	if (link_TokIs(&p, "SET"))
	{
		w = link_TokWord(&p);
		if (!link_NameToId(w, &id))
		{
			link_Ack(seq, "ERR_NAME");
			return;
		}
		if (!link_TokQ16(&p, &v))
		{
			link_Ack(seq, "ERR_VALUE");
			return;
		}
		if (!link_ParamSet(id, v))
		{
			link_Ack(seq, "ERR_NAME");
			return;
		}
		link_Ack(seq, "OK");
		return;
	}


	/* ================= SAVE / RESET ================= */
	if (link_TokIs(&p, "SAVE"))
	{
		if (link_MotionBusy())
		{
			link_Ack(seq, "ERR_BUSY");
			return;
		}
		link_CalibSave();
		link_Ack(seq, "OK");
		return;
	}

	if (link_TokIs(&p, "RESET"))
	{
		link_CalibErase();
		/* 只擦除参数页。RAM 中 sysCfg 保持当前值不动, 因此 RESET 的完整效果
		 * (回落到 ISR_Init 里的源码默认值)在下次上电由 LINK_Init 体现。
		 * 这样做的原因: 运行中把 sysCfg 改回默认值会让机器人的运动参数突变,
		 * 反而危险; 让它随重启自然生效更安全。 */
		link_Ack(seq, "OK");
		return;
	}

#ifdef MOUSE_REMOTE
	/* ================= MCU RESET —— 远程软复位(救援口) =================
	 * ⚠️ 与上面的 `RESET` **完全不同**, 别看错:
	 *      `RESET`      —— 擦除参数页, 让参数回落源码默认值(不重启)
	 *      `MCU RESET`  —— 整机软复位(`Sys_Soft_Reset()`), 程序从头开始跑
	 *
	 * 为什么需要它 —— 真机实测(2026-10-05)揪出的自锁死锁:
	 *   走迷宫的主循环里有大量**非协作式阻塞等待**(`MOTION/motion.c` 里
	 *   `while (odomStr.cellOdom < dis) ;` 之类共 30 余处), 它们等的都是物理进度
	 *   (编码器里程 / 传感器 / `motionCtrl.tick`)。而 `STOP` 会把 `freemode` 置 1
	 *   ⇒ 电机 PWM 恒 0 ⇒ 轮子永不再转 ⇒ **那些条件永远不可能满足**。
	 *   结果是"越停越死": 命令通道照常回 OK, 但主循环卡在函数内部,
	 *   `LESSON_Blind8()` 顶部的退出检查**根本轮不到** ⇒ 只能断电。
	 *   本命令提供一个**不必断电**的出口: 复位后远程档自然回到发车等待循环。
	 *
	 * 为什么只在远程档编译:
	 *   它和"急停可恢复"是同一类东西 —— 都是"操作者握着最终控制权"的手段。
	 *   按键比赛档不编译本段, 使那支固件的机器码**与 v2.3.0 逐字节一致**。
	 *
	 * 为什么延迟而不是立刻复位:
	 *   命令处理跑在 SysTick 里, 直接复位会让刚入队的 `ACK OK` 一起消失,
	 *   上位机只能看到超时。这里先 ACK, 再由 LINK_Tick() 倒计时 ~150ms 后复位
	 *   (150ms 远小于上位机 2.5s 的命令超时, 又留足了 BT 模块转发的时间)。
	 *   真正的成功判据是**复位后 PING 报 `rst=SW`**。
	 *
	 * ⚠️ 复位前**必须先停车**(真机事故 2026-10-05 的修正):
	 *   首版实现只置倒计时、没有停车, 结果现场出现"复位后电机继续转、
	 *   且对红外障碍物不再响应", 操作者被迫强行断电。原因有两层:
	 *     1) 本命令带 ~150ms 的 ACK 延迟, 加上复位生效窗口 —— 这期间若电机
	 *        仍在 PWM 驱动下转, 机器人会**带着动力滑行**;
	 *     2) 复位只复位**固件**, 不复位**物理位置** —— 内部位姿/地图清零而车
	 *        还在原处, 此时若立刻 `MAZE RUN`, 探索会按"我在起点"的错误假设
	 *        行驶: 既不认路也不认墙(墙检测只用来更新地图, 不是硬停车条件)。
	 *   因此这里先按 `STOP` 的语义把一切停干净(`freemode=1` ⇒ PWM 恒 0,
	 *   见 motor.c 的 MOTOR_LoopControl), 让 150ms 倒计时在**静态**下度过。
	 *   复位后由上位机决定下一步; **操作者须把电脑鼠放回起点角再发车**。
	 *   注: 若主循环正卡在阻塞等待里, 清 freemode 会让它更出不来 —— 但本命令
	 *   的意义正是"不靠它自己出来", 所以无害。 */
	if (link_TokIs(&p, "MCU"))
	{
		if (link_TokIs(&p, "RESET"))
		{
			link_StopMotion(LINK_RUN_REASON_USER); /* 先把车停干净, 再倒计时复位 */
			link_Ack(seq, "OK");
			linkMcuResetDelay = LINK_MCU_RESET_DELAY_MS;
			return;
		}
		link_Ack(seq, "ERR_VALUE");
		return;
	}
#endif /* MOUSE_REMOTE */

	/* ================= TARE / STOP / ESTOP ================= */
	if (link_TokIs(&p, "TARE"))
	{
		MOTION_SetAngle(0);
		linkEncDiffAcc = 0;
		link_Ack(seq, "OK");
		return;
	}

	if (link_TokIs(&p, "STOP"))
	{
		link_StopMotion(LINK_RUN_REASON_USER);
#ifdef MOUSE_REMOTE
		/* 远程档: `STOP` 的语义是"把控制权还给操作者"。两件事一起做:
		 *   1) 若电脑鼠正在迷宫里跑, 把程序带出迷宫循环 —— 否则车停了、程序仍在
		 *      while(1) 里空转, 发车状态无法复位(现场表现: 停了车也下不去新指令);
		 *   2) 兼作"解急停" —— 这是操作者重新取得控制权的唯一入口。同时把
		 *      sleepmode 清回 0, 使急停后板子仍在线(传感器/链路继续工作)。
		 * 按键比赛档保持原语义: 急停后必然休眠, 不接受远程复位。 */
		linkMazeAbort = 1;
		linkEStop = 0;
		emergMon.active = 0;
		emergMon.delay = 0;
		speedSt.sleepmode = 0;
#endif
		link_Ack(seq, "OK");
		return;
	}

	if (link_TokIs(&p, "ESTOP"))
	{
		/* 若正在 MOVE RUN, 先按 RUN 的格式上报一次, 让上位机拿到统一的明细行 */
		link_StopMotion(LINK_RUN_REASON_ESTOP);
#ifdef MOUSE_REMOTE
		/* 远程档: 急停的含义就是"把我从迷宫里带出来": 置退出请求, 让 LESSON_Blind8
		 * 返回, 主循环回到等待态(否则车停了、程序还在迷宫 while(1) 里, 状态无法复位)。 */
		linkMazeAbort = 1;
#endif
		emergMon.active = 1; /* 触发既有急停通路(按键比赛档会 200ms 后休眠; 远程档不休眠) */
		linkEStop = 1;       /* 锁存: 之后的 MOVE/CAL 一律 ERR_ESTOP, 直到显式 STOP */
		link_Ack(seq, "OK");
		return;
	}

	/* 注: `TELEM` 不在这里 —— 它由 link_HandleLine **提前拦截**, 因为它必须绕开
	 *     幂等重放簿记(理由见那里的注释)。此处留白以免误改回来。 */

	/* ================= MAP ================= */
	if (link_TokIs(&p, "MAP"))
	{
		if (link_TokIs(&p, "DUMP"))
		{
			link_SendMap();
			link_Ack(seq, "OK");
			return;
		}
		link_Ack(seq, "ERR_VALUE");
		return;
	}

	/* ================= ARM / MAZE —— 时间同步与远程发车 =================
	 * 时间同步这一块**不依赖任何测试档**: 生产固件也编译、也能用。
	 * 上位机流程通常是: ARM(武装计时 + 点亮 1Hz 基准灯 + 记发车时刻)
	 *                   -> MAZE RUN(让电脑鼠开始跑无记忆迷宫) 或 MOVE RUN 等。
	 * 两条分开是有意的: ARM 之后"任何动作"都被同一时间基准覆盖, 不限于跑迷宫。 */
	if (link_TokIs(&p, "ARM"))
	{
		linkArmed = 1;
		linkArmMs = linkTimeMs;
		linkTimeLed = 1;
		ledBeaconLock = 1; /* 基准灯独占 LED, 保证方波干净 */
		LED4 = 0;          /* 低电平点亮 */
		/* v2.0: 发车时刻**直接随 ACK 回**(`ACK <seq> OK <armMs>`), 不再单发一行
		 * `ARMED <ms>` —— 后者是一条没有序号、不可校验的异步文本, 而 ACK 有
		 * 序号 + 重发机制。同理删除 1Hz 的 TIME 帧: 统一帧每帧都带 ms8, 用
		 * `TELEM` 应答里的完整锚就能还原绝对时刻(见 link_SendUnified 的说明)。 */
		link_AckU(seq, "OK", (s32)linkArmMs);
		return;
	}

	if (link_TokIs(&p, "MAZE"))
	{
		/* MAZE RUN [BLIND|RACE] —— 请 main() 进入无记忆走迷宫(LESSON_Blind8)。
		 * 本命令只置请求位: 真正"跑"由主循环执行, 命令处理在中断里,
		 * 这里绝不能在 ISR 里跑那个 while(1)。
		 * 动作词**可选**: 不带 = BLIND, 因此旧上位机(只发 `MAZE RUN`)照常工作。 */
		if (link_TokIs(&p, "RUN"))
		{
#ifndef MOUSE_REMOTE
			/* 只有远程档的 main() 会等这条命令。按键比赛档若回 OK 就是撒谎
			 * (主机以为发车了, 其实没有任何东西会跑), 所以明确拒绝。 */
			link_Ack(seq, "ERR_VALUE");
			return;
#else
			/* 解析可选目标词。link_TokIs 不消耗未匹配的 token, 故两句顺序无关;
			 * 认不出的词一律按 BLIND 处理(向前兼容: 语义更保守, 不会突然跑比赛流程)。 */
			linkMazeTarget = LINK_MAZE_BLIND;
			if (link_TokIs(&p, "RACE"))
				linkMazeTarget = LINK_MAZE_RACE;
			else if (link_TokIs(&p, "BLIND"))
				linkMazeTarget = LINK_MAZE_BLIND;

			if (linkMazeReq || linkMazeRun)
			{
				link_Ack(seq, "ERR_BUSY");
				return;
			}
			if (linkEStop)
			{
				/* 急停锁存中直接发车会被 ISR 每周期清零速度 —— 与其让主机看到
				 * "发了车却不动", 不如明确回 ERR_ESTOP(先 STOP / MAZE STOP 解锁)。 */
				link_Ack(seq, "ERR_ESTOP");
				return;
			}
			linkMazeReq = 1;
			link_Ack(seq, "OK");
			return;
#endif
		}
#ifdef MOUSE_REMOTE
		if (link_TokIs(&p, "STOP"))
		{
			/* ---- MAZE STOP —— "把电脑鼠带出迷宫, 控制权还给操作者"(v2.1 新增) ----
			 * 与 ESTOP 的分工:
			 *   ESTOP     —— 立刻停车 + **锁存**, 之后的 MOVE/CAL 一律 ERR_ESTOP;
			 *   MAZE STOP —— 正常退出迷宫 + 复位发车状态 + **解锁急停**。
			 * 于是"发车 → 急停 → 收车 → 再发车"不必断电就能走完。
			 *
			 * 置 abort 让主循环从 LESSON_Blind8() 返回, main() 随即复位发车状态
			 * (LINK_MazeFinished), 下一次 `MAZE RUN` 才能被接受。
			 * 同时清急停锁存与 sleepmode: 这是操作者重新取得控制权的入口。
			 * 整段门控在 MOUSE_REMOTE 之下 —— 按键比赛档落到下面的 ERR_VALUE,
			 * 与"从未有过这条命令"完全等价。 */
			linkMazeAbort = 1;
			link_StopMotion(LINK_RUN_REASON_USER);
			linkEStop = 0;
			emergMon.active = 0;
			emergMon.delay = 0;
			speedSt.sleepmode = 0;
			link_Ack(seq, "OK");
			return;
		}
#endif /* MOUSE_REMOTE */
		/* MAZE EV / MAZE RST 已删除(v2.0):
		 *   - `MAZE EV <mask>` 与整条 MAZE EV 事件流一起取消(无事件行可配);
		 *   - `MAZE RST` 原先只清 linkMaze 里的统计(事件计数/岔路口/死路/转弯/格数),
		 *     而那些统计随事件流一并删除 —— 它们是**纯观测层**的计数, 现在由上位机
		 *     对统一帧差分自行维护, 不需要设备端再留一个空动作命令。 */
		link_Ack(seq, "ERR_VALUE");
		return;
	}

	/* ================= MOVE ================= */
	if (link_TokIs(&p, "MOVE"))
	{
		/* ---- MOVE RUN: 受"时间 + 红外"双护栏约束的远程运动(电机远程控制实验) ----
		 * 用法: MOVE RUN <spL> <spR> [<ms>] [<ir_mm>]
		 *   spL/spR : 左右轮目标速度(cm/s), 可正可负, 可为 0(单轮)；不能同时为 0
		 *   ms      : 最长运行时间, 省略=2000ms, 上限 LINK_RUN_MS_MAX
		 *   ir_mm   : 红外停车阈值(mm), 省略=10mm(=1cm)；**负值=关闭红外护栏**
		 * 完成回: 事件行 `STOPPED <REASON> #n`(v2.0 起只上报"为什么停";
		 *         elapsed / ir_min / dL / dR 这些量在 217 Hz 的统一帧序列里都能读到)
		 * 护栏在 1ms ISR 里判定, 不依赖上位机在线。 */
		if (link_TokIs(&p, "RUN"))
		{
			s32 spL = link_TokNum(&p);
			s32 spR = link_TokNum(&p);
			s32 ms = link_TokNum(&p);
			s32 ir = link_TokNum(&p);

			if (linkEStop)
			{
				link_Ack(seq, "ERR_ESTOP");
				return;
			}
			if (link_MotionBusy())
			{
				/* **关键**: 这里必须包含 linkMove.state。原来只查 motionCtrl.task,
				 * 而 MOVE RUN 自己会把 task 置 NULL -> 第二条 RUN 会在第一条仍在
				 * 运行时被接受, 覆盖 linkMove.elapsed/limit, 把时间护栏重置掉。 */
				link_Ack(seq, "ERR_BUSY");
				return;
			}
			if ((spL == 0 && spR == 0) || spL > LINK_RUN_SPEED_MAX || spL < -LINK_RUN_SPEED_MAX ||
			    spR > LINK_RUN_SPEED_MAX || spR < -LINK_RUN_SPEED_MAX)
			{
				link_Ack(seq, "ERR_VALUE");
				return;
			}
			if (ms <= 0)
				ms = LINK_RUN_MS_DEF;
			if (ms > LINK_RUN_MS_MAX)
				ms = LINK_RUN_MS_MAX;
			/* ir<0 = 显式关闭红外护栏(便于单独验证时间护栏); ir==0/省略 = 默认 10mm。
			 * 注意 link_TokNum 对"缺省"和"写 0"都返回 0, 所以要区分"关闭"只能用负值。 */
			if (ir < 0)
				ir = 0;
			else if (ir == 0)
				ir = LINK_RUN_IR_DEF;
			if (ir > LINK_RUN_IR_MAX)
				ir = LINK_RUN_IR_MAX;

			linkMove.state = LINK_MOVE_RUN;
			linkMove.elapsed = 0;
			linkMove.limit = (u32)ms;
			linkMove.irMm = (u32)ir;

			motionCtrl.task = MOTION_TASK_NULL;
			speedSt.freemode = 0;
			speedSt.speedTargetL = spL;
			speedSt.speedTargetR = spR;

			/* v2.0: 这里不再打开 1kHz 本地日志 —— SCOPE 与 DUMP 整套删除后,
			 * "运动时只跑实时流、停止后回传高分辨率日志"这条双流路径不存在了。
			 * 运动过程的波形改由统一帧(217Hz)直接给出: 4.6ms 一个点, 对
			 * "护栏是不是到点就停""电机有没有响应"这类判断已经足够。 */

			link_Ack(seq, "OK");
			return;
		}

		if (link_TokIs(&p, "FWD"))
		{
			u32 pulses = (u32)link_TokNum(&p);
			s32 sp = link_TokNum(&p);
			if (linkEStop)
			{
				link_Ack(seq, "ERR_ESTOP");
				return;
			}
			if (link_MotionBusy())
			{
				link_Ack(seq, "ERR_BUSY");
				return;
			}
			if (pulses == 0)
			{
				link_Ack(seq, "ERR_VALUE");
				return;
			}
			if (sp <= 0)
				sp = LINK_MOVE_SPEED_DEF;
			linkMove.state = LINK_MOVE_FWD;
			linkMove.target = pulses;
			linkMove.speed = (u16)sp;
			linkMove.pStartL = odomStr.distL + odomStr.distLCycle * ENCODER_ARR_VALUE;
			linkMove.pStartR = odomStr.distR + odomStr.distRCycle * ENCODER_ARR_VALUE;
			motionCtrl.task = MOTION_TASK_NULL;
			speedSt.freemode = 0;
			speedSt.speedTargetL = sp;
			speedSt.speedTargetR = sp;
			link_Ack(seq, "OK");
			return;
		}
		if (link_TokIs(&p, "ROT"))
		{
			s32 sp = link_TokNum(&p);
			s32 ms = link_TokNum(&p);
			if (linkEStop)
			{
				link_Ack(seq, "ERR_ESTOP");
				return;
			}
			if (link_MotionBusy())
			{
				link_Ack(seq, "ERR_BUSY");
				return;
			}
			if (sp == 0)
			{
				link_Ack(seq, "ERR_VALUE");
				return;
			}
			if (ms <= 0)
				ms = 500;
			linkMove.state = LINK_MOVE_ROT;
			linkMove.elapsed = 0;
			linkMove.limit = (u32)ms;
			motionCtrl.task = MOTION_TASK_NULL;
			speedSt.freemode = 0;
			speedSt.speedTargetL = sp;
			speedSt.speedTargetR = -sp;
			link_Ack(seq, "OK");
			return;
		}
		link_Ack(seq, "ERR_VALUE");
		return;
	}

	/* ================= CAL(请求-响应式标定, 设计方案 §9) =================
	 * 语法: CAL <item> START | CAL <item> ABORT | CAL IRDATA <phase 0..3>
	 *   GYRO / WALLDIST / IRDATA —— **采完才回 ACK**: 命令到达时不立刻应答,
	 *     而是把 seq 存进 linkCal.ackSeq, 等采集真的完成/超时再回一次。
	 *     ACK 本身即"这一步做完了, 请摆下一个位置"(取代旧的 CALSTEP/CALDONE 事件行)。
	 *   GYRORATIO / ENCANGLE —— 由电机做整周角度扫描, 最长可能 30s, 远超命令层的
	 *     10s 超时, 故仍**立即 ACK**(结果看 GET / 统一帧)。 */
	if (link_TokIs(&p, "CAL"))
	{
		u8 item;
		u8 phase = 0;
		u8 defer;
		w = link_TokWord(&p);
		item = link_CalItemToId(w);
		if (item == 0)
		{
			link_Ack(seq, "ERR_NAME");
			return;
		}
		if (link_TokIs(&p, "ABORT"))
		{
			linkCal.ackSeq = -1;
			link_CalibService(item, LINK_ACT_ABORT);
			link_Ack(seq, "OK");
			return;
		}
		/* 相位: IRDATA 用 0..3(`CAL IRDATA 2`); 其余项只接受 START, 相位恒为 0。 */
		if (!link_TokIs(&p, "START"))
		{
			if (item != LINK_CAL_IRDATA)
			{
				link_Ack(seq, "ERR_VALUE");
				return;
			}
			v = link_TokNum(&p);
			if (v < 0 || v > 3)
			{
				link_Ack(seq, "ERR_VALUE");
				return;
			}
			phase = (u8)v;
		}
		/* 需要电机转的两项在急停锁存期间必须一并拒绝, 否则"锁死"是假的 ——
		 * 静态三项(GYRO/WALLDIST/IRDATA)不涉及运动, 照常放行。 */
		if (linkEStop && (item == LINK_CAL_GYRORATIO || item == LINK_CAL_ENCANGLE))
		{
			link_Ack(seq, "ERR_ESTOP");
			return;
		}
		if (link_MotionBusy() && !(linkCal.active && linkCal.item == item))
		{
			link_Ack(seq, "ERR_BUSY");
			return;
		}
		defer = (u8)((item == LINK_CAL_GYRO || item == LINK_CAL_WALLDIST ||
		              item == LINK_CAL_IRDATA || item == LINK_CAL_GYRORATIO ||
		              item == LINK_CAL_ENCANGLE)
		                 ? 1u
		                 : 0u);
		linkCal.phase = phase;
		linkCal.ackSeq = defer ? seq : -1;
		if (defer)
			/* 防止"重发同一条命令"命中 linkLastStatus 的旧应答 ——
			 * 那时标定其实还在进行中, 回一个缓存的 "OK" 就是撒谎。 */
			linkLastSeq = -1;
		link_CalibService(item, LINK_ACT_START);
		if (!defer)
			link_Ack(seq, "OK");
		return;
	}

	link_Ack(seq, "ERR_CMD");
}

/*-------------------------------------  接收行处理  -------------------------------------*/
/**
 * @brief  处理一条完整接收行
 * @param  buf 行缓冲
 * @param  len 字节数(不含 CR/LF)
 * @return none
 * @note   行格式: <CMD> [args] [#seq] *<CRC16 HEX4>; CRC 覆盖 '*' 之前的全部字节。
 *         幂等: seq 与 CRC 都与上一条相同则不重复执行, 直接重放上次状态。
 */
static void link_HandleLine(char *buf, u8 len)
{
	char *star = 0;
	char *hash = 0;
	char *p;
	s32 seq = -1;
	u16 crcCalc, crcRecv = 0;
	u16 i, cmdLen;

	if (len == 0)
		return;
	if (len > 63)
		len = 63;
	buf[len] = '\0';
	cmdLen = (u16)len;

	/* ---- 取 '*' 后的 4 位十六进制 CRC ---- */
	for (i = 0; i < cmdLen; i++)
		if (buf[i] == '*')
			star = &buf[i];
	if (!star || (u16)(cmdLen - (u16)(star - buf)) < 5u)
	{
		link_Ack(-1, "ERR_NOCRC");
		return;
	}
	for (i = 1; i <= 4; i++)
	{
		u8 c = (u8)star[i];
		u8 v;
		if (c >= '0' && c <= '9')
			v = (u8)(c - '0');
		else if (c >= 'A' && c <= 'F')
			v = (u8)(c - 'A' + 10);
		else if (c >= 'a' && c <= 'f')
			v = (u8)(c - 'a' + 10);
		else
		{
			link_Ack(-1, "ERR_CRC");
			return;
		}
		crcRecv = (u16)((crcRecv << 4) | v);
	}
	crcCalc = link_Crc16((const u8 *)buf, (u16)(star - buf));
	*star = '\0';
	if (crcCalc != crcRecv)
	{
		link_Ack(-1, "ERR_CRC");
		return;
	}

	/* ---- 可选 '#seq' ---- */
	for (i = 0; i < (u16)strlen(buf); i++)
		if (buf[i] == '#')
			hash = &buf[i];
	if (hash)
	{
		*hash = '\0';
		p = hash + 1;
		seq = link_TokNum(&p);
	}

	/* ---- TELEM: 纯拉取命令, **不参与幂等重放簿记** ----
	 * 必须在这里提前返回, 不能放进 link_ExecCmd —— 因为下面那段会写
	 * `linkLastSeq/linkLastCrc`。而拉取器是**持续**发 TELEM 的, 一旦让它写这两个
	 * 变量, 就会把"上一条待重放的真实命令"(如 MOVE / CAL)冲掉:
	 * 于是那条命令的 ACK 一旦丢失, 上位机按同 seq 重发时**不会被重放, 而是重新执行**
	 * —— MOVE 会重新起步、CAL 会重新采一遍。这在真机上是真事故。
	 * 所以 TELEM 既不查也不写重放状态: 它每次都必须真的发一帧, 本身也无需重放保护
	 * (上位机对它 no_retry, 不会重发)。
	 * 判据用整词匹配, 避免把将来的 `TELEMX` 之类误判进来。 */
	if (seq >= 0 && strncmp(buf, "TELEM", 5) == 0
	    && (buf[5] == '\0' || buf[5] == ' '))
	{
		link_SendUnified();
		link_AckU(seq, "OK", (s32)linkTimeMs);
		return;
	}

	/* ---- 幂等: 相同 seq 且相同 CRC -> 重放上次应答 ---- */
	if (seq >= 0 && seq == linkLastSeq && crcRecv == linkLastCrc)
	{
		link_TxStr("ACK ");
		link_TxInt(seq);
		link_TxPut(' ');
		link_TxStr((const char *)linkLastStatus);
		link_TxStr("\r\n");
		return;
	}
	linkLastSeq = seq;
	linkLastCrc = crcRecv;

	link_ExecCmd(buf, seq);
}

/*-------------------------------------  初始化  -------------------------------------*/
void LINK_Init(void)
{
	linkTxHead = 0;
	linkTxTail = 0;
	linkTxInflight = 0;
	linkTick = 0;
	linkSeq = 0;
	/* v2.1 拉取模式: 固件**不再自时钟发帧**, 遥测一律由上位机的 `TELEM` 请求驱动。
	 * 于是这里没有任何"开流"状态要初始化 —— 上电即静默, 且**永远静默到被请求为止**。
	 * 为什么放弃自时钟(真机实测教训, 见设计方案 §6.1):
	 *   本链路经过一对蓝牙透传模块, 其内部缓冲一旦被灌满就**停止转发且不会自行恢复**
	 *   (必须给模块重新上电)。v2.0 的"链路空闲就发一帧"实测会把 4.3 KB/s 的全量帧
	 *   持续压上去, 而这条链路实测只能承载约 190 B/s —— 于是缓冲瞬间灌死,
	 *   从此连 `PING` 都收不到。改为拉取后, 上位机不再请求 => 固件不再发送 =>
	 *   缓冲自然排空, **不需要断电即可恢复**。 */
	linkDrop = 0;
	linkFrames = 0;
	linkLastSeq = -1;
	linkLastCrc = 0;
	linkLastStatus[0] = 0;
	linkMove.state = LINK_MOVE_NONE;
	linkSrv.state = LINK_SRV_NONE;
	linkCal.active = 0;
	linkCal.needSample = 0;
	linkCal.ackSeq = -1;
	linkEStop = 0;   /* 上电不锁存: 只有真正急停过才锁 */
	linkEvDrop = 0;
	linkResetCause = 0;
#ifdef MOUSE_REMOTE
	linkMcuResetDelay = 0; /* 上电不挂起远程软复位(见 `MCU RESET` 分支) */
#endif

	/* 时间同步: 上电时基从 0 起走; 未武装(等 ARM 命令发车) */
	linkTimeMs = 0;
	linkArmMs = 0;
	linkArmed = 0;
	linkTimeLed = 0;
	linkMazeReq = 0;
	linkMazeRun = 0;
	ledBeaconLock = 0; /* 未发车前 LED 照常充当状态灯 */

	/* ================= 复位原因(在设计方案 §10.2, 由 PING 上报) =================
	 * RCC->CSR 的各位是**边沿锁存**的: 只要发生过就一直是 1, 直到写 RMVF 清掉。
	 * 因此这里读一次、记下来、立刻清 —— 于是 PING 报的是"**本次上电之前**为什么复位",
	 * 而不会被更早的历史污染。
	 * 判定优先级按"信息量"排: IWDG(程序卡死过) > SW > POR > PIN。 */
	{
		u32 csr = RCC->CSR;
		if (csr & RCC_CSR_IWDGRSTF)
			linkResetCause = 2;
		else if (csr & RCC_CSR_SFTRSTF)
			linkResetCause = 1;
		else if (csr & RCC_CSR_PORRSTF)
			linkResetCause = 0;
		else if (csr & RCC_CSR_PINRSTF)
			linkResetCause = 3;
		else
			linkResetCause = 0;
		RCC->CSR |= RCC_CSR_RMVF; /* 清标志, 下次复位才重新置位 */
	}

	/* ================= IWDG 独立看门狗(设计方案 §10.1) =================
	 * 为什么用 IWDG 而不是 WWDG: IWDG 跑在 LSI 上, **不受主时钟/时钟失效影响**,
	 * 这正是"卡死"场景下唯一还活着的东西。
	 *
	 * 喂狗点: LINK_Tick()(SysTick 1ms 上下文) —— 而不是主循环。
	 *   因为主循环可能长期卡在 LESSON_Blind8() 的 while(1) 里跑迷宫, 那是**正常**
	 *   工作状态, 不是卡死; 而 SysTick 始终在跑, 所以"喂狗 = 中断系统还活着"。
	 *   (它也覆盖了"中断被关死后卡住"——那才是真需要复位的场景。)
	 *
	 * 超时选择: LSI 40kHz(30~60kHz) / 64 = 625Hz, 重装 1249 -> 约 2.0s
	 *   (最坏 1.3~2.7s)。留足 2s 是因为**Flash 擦除**会阻塞取指:
	 *   FLASH_EraseRunData() 一次擦 13 页, 每页约 20~40ms, 合计最坏约 0.5s,
	 *   期间 SysTick 会被推迟 —— 超时必须明显大于它, 否则一擦参数就误复位。
	 * 注意: 一旦启动就无法关闭; 用 SWD 打断点会让内核停住而看门狗照跑,
	 *   调试时被复位属预期现象。 */
	IWDG->KR = 0x5555; /* 允许写 PR/RLR */
	IWDG->PR = 0x04;   /* 预分频 /64 */
	IWDG->RLR = 1249;  /* 625Hz 下约 2.0s */
	IWDG->KR = 0xAAAA; /* 先喂一次, 装载 RLR */
	IWDG->KR = 0xCCCC; /* 启动 */

	/* ================= RX 优先级修正(关键) =================
	 * 现状: SysTick 占复位默认的抢占优先级 0(全工程无任何代码写 SCB->SHP),
	 *       而 USART1 RX 被 usart.c 的 MY_NVIC_Init(3, 3, ..., 2) 设为最低
	 *       => SysTick 可以抢占 RX, 反之不行。
	 *       若 SysTick 的 1ms 处理耗时 100-200us(F1 无 FPU, PID 全 double 软件浮点),
	 *       期间 RX 的 DR 未被读走, 后续到达的字节全部触发 ORE 被丢弃(成片丢, 非偶发)。
	 * 处理: 把 SysTick 的抢占降到 1, RX 提到 0, 让约 2.5us 的 RX ISR 可以抢占 SysTick。
	 * 说明: 中断分组为 2 位抢占 / 2 位子优先级(见 usart.c), 抢占位占优先级字节的 bit7:6,
	 *       故 SysTick 需要 0x40(抢占 1), USART1 需要 0x00(抢占 0)。
	 *       此处直接写寄存器而非 NVIC_SetPriority(), 以免 CMSIS 的移位约定产生歧义。 */
	SCB->SHP[11] = 0x40;          /* SysTick: 异常号 15 -> SHP[11], 抢占 1 / 子 0 */
	NVIC->IP[USART1_IRQn] = 0x00; /* USART1 RX: 抢占 0 / 子 0 */

	/* ================= TX DMA ================= */
	link_DmaInit();

	/* ================= 标定参数 =================
	 * 先取源码默认值, 再尝试用 Flash page99 覆盖。
	 * LINK_Init 在 HARDWARE_Init 之后调用, 此时 ISR_Init 已给 sysCfg 赋过默认值,
	 * 所以这里直接覆盖即可 —— ISR_Init 零改动。 */
	link_ParamFromSys();
	if (!link_CalibLoad())
		link_ParamFromSys(); /* 载入失败时确保参数表与源码默认值一致 */


	linkEncPrevL = odomStr.encL;
	linkEncPrevR = odomStr.encR;
	linkEncDiffAcc = 0;
}

/*-------------------------------------  每毫秒  -------------------------------------*/
void LINK_Tick(void)
{
	u16 len;

	linkTick++;
	linkTimeMs++; /* 全局毫秒时基(独立于任何测试档): 与 linkTick 不同, u32 不回绕 */

#ifdef MOUSE_REMOTE
	/* 0a. 远程软复位倒计时(见 `MCU RESET` 分支)。
	 * 必须放在**中断上下文**里递减 —— 这样即使主循环已经卡死在
	 * `MOTION/*.c` 的阻塞等待里, 这条救援路径依然能走到(这正是它的意义)。
	 * 倒计时结束时 `Sys_Soft_Reset()` 触发 SYSRESETREQ, 程序从 main() 重跑,
	 * 复位原因会被 LINK_Init 记成 SW, 下一次 PING 据此显示 `rst=SW`。 */
	if (linkMcuResetDelay)
	{
		linkMcuResetDelay--;
		if (linkMcuResetDelay == 0)
			Sys_Soft_Reset();
	}
#endif

	/* 0. 时间基准灯: 发车后以 1Hz(50% 占空比)闪烁, 供外部(摄像头/光电/人眼)
	 *    与回传的时间戳做互相印证。这里直接写引脚(不走 LED_On/Off),
	 *    因为 ledBeaconLock 已把所有状态灯的写入挡掉了 —— 保证方波干净。 */
	if (linkArmed)
	{
		u8 lv = (u8)(((linkTimeMs - linkArmMs) % (u32)LINK_TIME_DIV) < (u32)(LINK_TIME_DIV / 2));
		if (lv != linkTimeLed)
		{
			linkTimeLed = lv;
			if (lv)
				LED4 = 0; /* 低电平点亮 */
			else
				LED4 = 1;
		}
	}

	/* 1. 消费接收行。RX ISR 优先级低于 SysTick, 不会打断本段, 故读-解析-清空天然原子。
	 *    若忘记消费 USART_RX_STA 的 bit7, 一行收完后后续所有字节会被 usart.c 直接丢弃。 */
	if (USART_RX_STA & 0x80)
	{
		len = (u16)(USART_RX_STA & 0x3F);
		link_HandleLine((char *)USART_RX_BUF, (u8)len);
		USART_RX_STA = 0;
	}

	/* 2. 原始轮差累计(始终运行, 供角度标定使用) */
	link_EncAccumulate();

	/* 2b. 喂狗(IWDG, 见 LINK_Init 的说明)。
	 *     放在这里而不是主循环: 主循环可能正常地卡在迷宫的 while(1) 里,
	 *     而 SysTick 是否在跑才是"系统还活着"的真正判据。 */
	IWDG->KR = 0xAAAA;

	/* 3. 运动 / 角度扫描 / 标定状态机 */
	link_MoveService();

	link_ServoService();
	if (linkCal.active)
		link_CalibService(linkCal.item, LINK_ACT_STEP);

	/* 5. (v2.1 起不再有自时钟组帧 —— 遥测完全由上位机的 TELEM 请求驱动) */

	/* 6. TX 搬运 */
	link_TxDrain();
}

/*-------------------------------------  急停通知  -------------------------------------*/
/**
 * @brief  由 isr.c 在 emergMon.active==1 时调用: 把急停锁存到链路层
 * @param  none
 * @return none
 * @note   必须在 SysTick(1ms) 上下文调用, 且会被**每毫秒**调用一次 ——
 *         所以第一件事就是判断是否已经锁存并直接返回(否则 1ms 刷一行文本把链路灌死)。
 *         急停的"停车"动作由 ISR 自己完成(清速度目标 + 任务码), 这里只负责两件事:
 *           1) 锁存 -> 之后 MOVE/CAL 一律 ERR_ESTOP, 必须显式 STOP 才解锁;
 *           2) 主动上行一行 `ESTOP TRIP #n`, 让上位机不必等到下一帧遥测才知道出了事。
 *         为什么这一条**不做重发**: 它要求最低延迟, 而重发要等 230ms 往返。
 *         保证"不丢"靠的是它发得早(不等任务收尾) + 行短(远小于 LINK_EV_NEED_SPACE),
 *         而不是靠可靠通道 —— 序号让主机至少知道"丢过"。
 */
void LINK_EStopTrip(void)
{
	if (linkEStop)
		return; /* 已锁存: 1ms 一次的调用不能重复上报 */

	linkEStop = 1;
	linkMove.state = LINK_MOVE_NONE;
	linkSrv.state = LINK_SRV_NONE;
	linkCal.active = 0;
	linkCal.needSample = 0;

	link_EvLine("ESTOP TRIP");
}

/*-------------------------------------  远程发车  -------------------------------------*/
/**
 * @brief  是否有待处理的远程发车请求(main() 的等待循环用)
 * @param  none
 * @return 1=已收到 MAZE RUN 且尚未启动
 */
u8 LINK_MazeRequested(void)
{
	return linkMazeReq;
}

/**
 * @brief  标记"迷宫已真的开始跑"(由 main() 在进入迷宫前调用)
 * @param  none
 * @return none
 * @note   ⚠️ 这里**同时把 linkMazeReq 清零** —— 这是 v2.1 的一个真机缺陷修复。
 *
 *         原实现只置 linkMazeRun 而不清 linkMazeReq, 而 `MAZE RUN` 的忙判据是
 *         `linkMazeReq || linkMazeRun`、`LESSON_Blind8()` 又是 while(1) 不返回 ——
 *         于是 **发过一次车之后两个标志都恒为 1, `MAZE RUN` 永远回 ERR_BUSY,
 *         只能断电复位**(真机复现 2026-10-05: 首次 OK, 之后每次都 ERR_BUSY)。
 *
 *         "请求"在语义上是**边沿信号**: main() 一旦消费就该清零。把它做成电平信号
 *         是把"待处理请求"和"正在跑"两件事混在一个变量里, 是这个 bug 的根源。
 */
void LINK_MazeStarted(void)
{
#ifdef MOUSE_REMOTE
	/* 只有远程档的 main() 会消费这个请求位(按键比赛档的 main() 不调用本函数),
	 * 故一并门控起来 —— 保证按键比赛档(LESSON_Blind8 不可打断的那一档)的
	 * 机器码与 v2.3.0 **逐字节一致**。 */
	linkMazeReq = 0; /* 消费掉请求位 —— 不带这一行就会永久 ERR_BUSY */
#endif
	linkMazeRun = 1;
}

#ifdef MOUSE_REMOTE
/**
 * @brief  取本次发车目标(由 main() 在 LINK_MazeStarted() 之后调用)
 * @param  none
 * @return LINK_MAZE_BLIND / LINK_MAZE_RACE
 * @note   值由 `MAZE RUN [BLIND|RACE]` 的命令分支写入; 不带动作词即 BLIND。
 *         只有远程档存在这个函数 —— 按键比赛档的 main() 完全不看发车目标。
 */
u8 LINK_MazeTarget(void)
{
	return linkMazeTarget;
}

/**
 * @brief  标记"迷宫已结束/已退出"(由 main() 在 LESSON_Blind8() 返回后调用)
 * @param  none
 * @return none
 * @note   清掉 linkMazeRun, 使下一次 `MAZE RUN` 能被接受 ——
 *         这是"**可重复发车**"的另一半(另一半是 LESSON_Blind8 的退出检查)。
 *         只被远程档的 main() 调用, 故门控在 MOUSE_REMOTE 之下。
 */
void LINK_MazeFinished(void)
{
	linkMazeRun = 0;
}

/**
 * @brief  是否有"退出迷宫"请求(APP/lesson.c 的 LESSON_Blind8 每轮检查)
 * @param  none
 * @return 1=请退出迷宫循环
 * @note   只有 `MAZE STOP` 与 `ESTOP` 会置位 linkMazeAbort。
 *         为什么需要它: `LESSON_Blind8()` 是 while(1), 急停只能停电机、**停不掉
 *         那个循环**, 于是"车停了但程序还在迷宫里"—— 发车状态无法复位, 现场表现为
 *         "急停之后什么指令都下不去"。有了退出通道, main() 才能回到等待态。
 *
 *         **按键比赛档不调用本函数**: LESSON_Blind8 里的调用被 #ifdef MOUSE_REMOTE
 *         包住, 预处理阶段就消失, 因此那支固件的机器码一行都没变。
 */
u8 LINK_MazeAbortRequested(void)
{
	return linkMazeAbort;
}
#endif /* MOUSE_REMOTE */
