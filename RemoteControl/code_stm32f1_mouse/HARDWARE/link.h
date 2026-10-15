/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: link.h —— 蓝牙链路(遥测上行/命令下行/标定服务/参数持久化)
*       ------------------------------------------------------------------------------------------------
*               设计要点(细节见 docs/传输模式重构设计方案.md):
*                 - 本模块为**纯新增**, 不改动任何现有业务逻辑与红线数值
*                 - 上行: 二进制帧 AA 55 | LEN(2) | TYPE | SEQ | PAYLOAD | CRC16(2); MODBUS 多项式
*                 - 下行: ASCII 文本行 <CMD> [args] [#seq] *CRC16, 应答亦为文本行
*                 - 与 printf 共存: printf 只产出 0x09-0x7E, 永不产出 0xAA, 故可分流
*                 - 发送搬运走 DMA1_Channel4 (USART1_TX), 零 CPU 占用
*
*               v2.1 拉取模式(2026-10-05, 真机实测后的修正):
*                 - **遥测改为请求-响应**: 上位机发 `TELEM`, 固件回一帧统一帧 + 带时基的 ACK
*                 - **废除 v2.0 的自时钟持续流** —— 实测该链路(蓝牙透传)只能承载约 190 B/s,
*                   而自时钟会把 4.3 KB/s 持续压上去, 模块缓冲瞬间灌满并**停止转发且不自恢复**
*                 - 因此删除 `STREAM ON` / `STREAM OFF` 两条命令与固件侧的开流状态
*                 - 时间锚改由 `TELEM` 的应答携带(**每请求一次就刷新一次**, 比开流时给一次更稳)
*
*               v2.0 传输模式重构(2026-10-05):
*                 - **只有一种周期帧**: LINK_TYPE_UNIFIED(45B payload)
*                 - 取消双流: SCOPE 本地 1kHz 录制 + DUMP 整块回传全部删除
*                 - 取消 SNAP: 可测量量已全在统一帧里, 配置量走参数命令
*                 - 取消 MAZE EV 事件流: 迷宫状态量已进统一帧, 边沿由上位机差分取得
*                 - 上行只剩两个事件行: `STOPPED <reason> #n` / `ESTOP TRIP #n`
*********************************************************************************************************
*/

#ifndef _LINK_H_
#define _LINK_H_
#include "main.h"
/*-----------------------------------------  I N C L U D E S  -----------------------------------------*/

/*---------------------------------------  D E F I N I T I O N  ---------------------------------------*/

/* ================= 速率族命令(v2.6, 远程档常态) =================
 * 目标: 提高遥测速率, 以捕获 6.5 Hz 停等拉取漏掉的关键帧。
 * 做法: 固件**按 PC 指定的节拍自时钟发帧**, 由 `RATE` 命令族控制 ——
 *         `RATE FIX <hz>`         固定节拍(MTBF 压测的通信基准)
 *         `RATE PULL <n> <t_ms>`  PC 主导的批量节拍(发 n 帧、间隔 t ms、新 PULL 打断)
 *         `RATE OFF`              关掉自时钟, 回到纯 `TELEM` 拉取
 * ⚠️ **统一帧长度未变**(49B 载荷 / 57B 帧)。v2.6-exp 曾在帧尾追加 2B 目标帧率
 *    供 AIMD 反馈读取, 而 AIMD 已被"事件触发式拉取(周期 ≡ RTT)"取代、该字段
 *    零消费者, 故连同 AIMD 命令一并删除 —— 线上帧格式与 v2.5 完全一致,
 *    只有命令集增加。
 * ⚠️ 整段只在 MOUSE_REMOTE(远程档)下编译; 按键比赛档不含自时钟 ⇒ 比赛 hex 不变。 */

/* ---- 协议版本(v2.5 起按档位取值) ----
 * ⚠️ 两档编译**同一份** link.c/link.h: 共享宏不门控必改比赛档产物。
 *     故 P_NUM/PROTO_VER/FW_VER 三个宏都按 MOUSE_REMOTE 取值 ——
 *     比赛档保持 v2.4 语义与**逐字节不变**的产物, 远程档承载新协议。 */
#ifdef MOUSE_REMOTE
#define LINK_PROTO_VER 8 /* v2.7: `CAL` 家族扩展 —— 新增 6 个子命令(ERASEIR/IRSELF/
                          *        SENSOR/IRCAP/BLOCK/ERASEALL), 把按键菜单 2/4/5/6/7/8
                          *        项的能力远程化。**帧格式与参数表均未变**, 进位只因
                          *        命令集变化(先例: v2.4 的 `MAZE RUN`、v2.6 的 RATE 族)。 */
#define LINK_FW_VER 222  /* 2.22 —— v2.10.1: 修正远程档值守循环按键分支的 `BUTTON1` 判定
                          *        极性 —— 原写 `if (BUTTON1 == 0)` 当"按下"是**反的**
                          *        (真值 idle=0 / 按下=1, 见 menu.c:33 的"4s 未按键自动
                          *        进主流程"倒计时 + calibrate.c:190 `BUTTON1 == 1`)。该缺陷
                          *        使空闲时 `while (BUTTON1 == 0)` **永久自旋** ⇒ main 永不
                          *        执行 `LINK_MazeStarted()`: `MAZE RUN` 回 ACK OK 却**永不
                          *        发车**、随后恒 ERR_BUSY 且 `MAZE STOP` 救不回。详见
                          *        USER/main.c 值守循环。**帧格式/命令集/参数表全未变**
                          *        (49B/57B/41 项) ⇒ 协议号仍 8。 */
#else
#define LINK_PROTO_VER 5 /* 比赛档: 与 v2.4.0 完全一致(无 39-55 新参数, 表容量 24)。 */
#define LINK_FW_VER 216  /* 比赛档: 产物逐字节不变(基线 hex sha256 f0a5f863...)。 */
#endif
/* 历史版本记录:
 *  222 = v2.10.1: **远程档发车请求消费修复** —— `USER/main.c` 值守循环(等 `MAZE RUN`)
 *        里那条"按键进菜单"分支把 `BUTTON1` 极性写反(原 `if (BUTTON1 == 0)` 当按下;
 *        真值 **idle=0 / 按下=1**, 见 menu.c:33 的"4s 未按键自动进主流程"倒计时与
 *        calibrate.c:190 `BUTTON1 == 1`)。空闲时 `while (BUTTON1 == 0)` **永久自旋** ⇒
 *        main 永不执行 `LINK_MazeStarted()`: `MAZE RUN` 回 **ACK OK 却永不发车**、随后
 *        恒 `ERR_BUSY` 且 `MAZE STOP` 救不回(真机复现 2026-10-16, 见 host/
 *        v210_launch_pull_test.py)。缺陷由 v2.7(220) 引入(其父版本循环仅 `DELAY_Ms(1)`);
 *        v2.8 的"发车标记灯"验收只确认了命令层点的 4 颗灯、未验车辆真动, 故漏网。
 *        修正只在 `#ifdef MOUSE_REMOTE` 段内 ⇒ 比赛档 hex 逐字节不变;
 *        协议/命令/参数表零改动 ⇒ `LINK_PROTO_VER` 仍 8。
 *  221 = v2.8: **发车标记灯** —— 远程档 `MAZE RUN` 被接受的那一刻直接写引脚点亮
 *        LED1-4, 并保持 `LINK_LAUNCH_MARK_MS`(300ms)后释放。动机: 录像时需要一个
 *        无歧义的发车时刻标定(对轴)。实现刻意不加固件侧新状态机: 只在 link.c 加
 *        一个 `linkLaunchMarkEnd` 截止时刻, 窗口内靠 LINK_Tick 每 1ms 重写引脚维持,
 *        窗口一过即释放 —— 基准灯与常规状态灯的行为**此后一字未改**。
 *        **协议文本/命令集/参数表均未变**(命令仍 24 条) ⇒ `LINK_PROTO_VER` 停在 8,
 *        靠 `PONG` 的 fw 号(220->221)区分。
 *  220 = v2.7: `CAL` 家族扩展 + 远程档按键进菜单。新增子命令(均立即 ACK, 无采集等待):
 *        `CAL ERASEIR`(擦红外标定页, 菜单 2) / `CAL IRSELF`(红外自检仅传感器段, 菜单 4) /
 *        `CAL SENSOR`(传感器 4 行, 菜单 5) / `CAL IRCAP <1..4>`(红外 8 值分步标定,
 *        菜单 6) / `CAL BLOCK`(块数据概览, 菜单 7, 墙图仍走 MAP DUMP) /
 *        `CAL ERASEALL`(擦迷宫/路径 6 页, 菜单 8, **不休眠**否则远程失联)。
 *        另: 远程档值守期间短按 BUTTON1 进 `MENU_Run()`(菜单 0/1 项返回后恢复值守态)。
 *  219 = v2.6.1: flags bit3(0x08) 由恒 0 的 SCOPING 改为 `LINK_FLAG_PULLF1` ——
 *        "本帧是收到 PULL 后发出的第一帧"。PULL 分支在组帧前置位、`link_SendUnified`
 *        消费后立刻清(只标一帧)。**帧长与参数表一字未改** ⇒ 旧上位机仍能解全部既有字段,
 *        只是 bit3 会由"恒 0"变成"批首帧"语义; 新上位机据此把批起点判据从"推算"改成
 *        "读事实"(见 host/link_pull_evt.py 与 docs/链路MTBF压测方法.md §14.15)。
 *  218 = v2.6.0: `RATE` 命令族转正 —— 固件按 PC 指定节拍自时钟发帧, 作 MTBF 压测
 *        (`RATE FIX`) 与"事件触发式拉取(周期 ≡ RTT, `RATE PULL`)"的载体。
 *        **统一帧仍是 49B / 57B**: v2.6-exp 的帧尾 2B 目标帧率随 AIMD(ON/UP/KEEP/DOWN
 *        + 看门狗)一并删除 —— 该字段唯一消费者是已废弃的 AIMD 反馈。
 *  217 = v2.5.0: 参数表扩容(id 39-55) + IRTHRWALL 覆写恢复点同步。
 *  216 = v2.4.0: 档位开关由五档压成两档(见 USER/main.h)。
 *        · 远程档 `MOUSE_REMOTE`: 合并原 [2] 的远程运动、[3] 的参数读写、[4] 的远程发车与监视;
 *          去掉课程实验层(APP/lesson_lab.c 整层删除, 含 page116 与 LESSON 命令)。
 *        · 不定义 = 按键比赛档: **编译产物与 v2.3.0 逐字节一致**。
 *        另: `LINK_P_NUM` 仍为 24(生产表未动), 但课程表(8-21)连同 page116 一并消失。
 *  215 = v2.3.0: 前墙拆"近端/远端"两档 + 比赛状态量回传。
 *  214 = v2.2.1: `MCU RESET` **复位前先停车**(安全修正)。
 *  协议号: 8 = v2.7 CAL 家族扩展; 7 = v2.6 RATE 命令族; 6 = v2.5 参数表 24->41;
 *          5 = v2.4 `MAZE RUN` 可选目标词 BLIND|RACE; 4 = v2.3 统一帧 45->49B; 3 = v2.1 TELEM。 */

/* ---- 帧同步头与开销 ----
 * 布局: AA 55 | LEN(2,LE) | TYPE | SEQ | PAYLOAD(LEN) | CRC16(2,LE)
 * 开销 = 同步2 + 长度2 + 类型1 + 序号1 + CRC2 = 8 字节(不是 7!) */
#define LINK_SYNC0 0xAA
#define LINK_SYNC1 0x55
#define LINK_FRAME_OVERHEAD 8

/* ---- 上行帧类型(二进制) ----
 * v2.0 只留两种: 统一遥测帧 + 墙图整块。
 * 旧 FAST 0x01 / MID 0x02 / SLOW 0x03 / DUMP_HDR 0x04 / DUMP_BLK 0x05 / DUMP_END 0x06 /
 * TIME 0x08 / MAZE 0x09 **全部删除** —— 周期量统一进 LINK_TYPE_UNIFIED;
 * 时间同步不再单开帧(统一帧带 ms8, `TELEM` 应答带完整 u32 锚)。 */
#define LINK_TYPE_MAP 0x07 /* 墙图整块(静态数据, 按需拉取) */
#define LINK_TYPE_UNIFIED 0x0A

/* 墙图整块: cX,cY,dir,task + mapWall[MAZETYPE^2] + mapVisited[MAZETYPE^2]
 * 注意 MAZETYPE 定义在 STRATEGY/maze.h。宏体在**使用点**才展开, 而 link.c 使用时
 * main.h 已完整展开到 maze.h, 故此处取值正确(当前 MAZETYPE=7 -> 102 字节)。
 * 若将来把 MAZETYPE 改成 16, 本帧自动变为 516 字节, 单帧仍可发送。 */
#define LINK_MAP_LEN (4 + MAZETYPE * MAZETYPE * 2)

/* ================= 统一帧 =================
 * 见 docs/传输模式重构设计方案.md §4 / docs/传输模式接口设计.md §1。
 * 形态: AA 55 | LEN(2,LE) | TYPE | SEQ | PAYLOAD(49) | CRC16(2)  ->  57 B
 *
 * **拉取式(v2.1)**: 收到一条 `TELEM` 就发一帧, 没有任何周期任务、没有开流开关。
 *   于是设备端**不存在"自己发多了"的可能** —— 上限完全由上位机的请求窗口决定。
 *   过载保护在 link_SendFrame: TX 环空间不足就整帧丢弃并计数(linkDrop, 随帧上报),
 *   上位机据此收窄窗口。**丢弃而不阻塞**是必须的 —— 命令处理跑在 1ms 中断里。
 *
 * ⚠️ 为什么不做自时钟(v2.0 的做法, 已废除): 本链路经过一对蓝牙透传模块,
 *   其内部缓冲一旦被灌满就**停止转发且不会自行恢复**(需要模块重新上电)。
 *   自时钟会把 53B/帧 连续压到 100% 线速(≈4.3 KB/s), 远超实测的 ≈190 B/s 承载能力
 *   => 缓冲灌死, 从此连 PING 都收不到。拉取模式下上位机一停请求, 缓冲即自然排空。
 *
 * **时间锚**: 每帧带 ms8(时基低 8 位); `TELEM` 的应答另带完整 u32 时基,
 *   上位机以"锚 + 单调时钟粗估"做同余还原(resolve_ms8)。因为**每次拉取都刷新锚**,
 *   锚不会陈旧, 失锁能立刻自愈。
 *   ⚠️ `TELEM` **不参与幂等重放簿记**(既不查也不写 linkLastSeq) —— 它是持续流量,
 *   若参与就会把待重放的真实命令(如 MOVE/CAL)冲掉, 使重发变成重复执行。见 link.c。 */
#define LINK_UNIFIED_LEN 49

/* ---- 统一帧长度 ----
 * v2.6 起就是 LINK_UNIFIED_LEN 这一个值: v2.6-exp 曾在帧尾追加 2B 目标帧率,
 * 随 AIMD 一并删除后已无第二个长度。此处不再保留 LINK_UNIFIED_LEN_EXP 别名。 */

/* 载荷偏移(与上位机 codec.UNIFIED_CHANNELS 逐字段对齐; 改动须同步) */
#define LINK_U_MS8 0       /* u8     设备毫秒时基低 8 位        */
#define LINK_U_IRADC 1     /* u16×4  原始 ADC: F1 F2 L R        */
#define LINK_U_IRMM 9      /* u16×4  距离 mm: F1 F2 L R         */
#define LINK_U_BATTMV 17   /* u16    电池 mV(IIR 后)            */
#define LINK_U_SPEEDL 19   /* s16    左轮速度 cm/s              */
#define LINK_U_SPEEDR 21   /* s16    右轮速度 cm/s              */
#define LINK_U_GYRO 23     /* s16    陀螺积分角度               */
#define LINK_U_ENCANG 25   /* s16    编码器角度(与陀螺角不同物理量) */
#define LINK_U_TRACKL 27   /* s16    左轮相对里程 **mm**(非 pulse)  */
#define LINK_U_TRACKR 29   /* s16    右轮相对里程 mm            */
#define LINK_U_CELLODOM 31 /* s16    格内里程 mm                */
#define LINK_U_PIDERR 33   /* s16    控制误差(调参核心信号)     */
#define LINK_U_STATE_A 35  /* 位打包: 见下                      */
#define LINK_U_STATE_B 36  /* 位打包: 见下                      */
#define LINK_U_STATE_C 37  /* 位打包: 见下                      */
#define LINK_U_FLAGS 38    /* u8     LINK_FLAG_*                */
#define LINK_U_POS 39      /* 位打包: posX(4) | posY(4)         */
#define LINK_U_GOAL 40     /* 位打包: goalX(4) | goalY(4)       */
#define LINK_U_WALLTHR 41  /* u8     **运行时**侧墙阈值(非参数值)  */
#define LINK_U_STACKIDX 42 /* u8     岔路栈深度                 */
#define LINK_U_FASTWAY 43  /* u8     路径动作计数               */
#define LINK_U_DROP 44     /* u8     累计丢帧低 8 位            */
/* ---- v2.3 新增(纯只读监视, 不改任何决策) ---- */
#define LINK_U_FRONT 45    /* 位打包: 前墙双档 + 双路互证(见下) */
#define LINK_U_SPURT 46    /* 位打包: 冲刺档位与标志(见下)      */
#define LINK_U_GOAHEAD 47  /* u8     冲刺前行格数(taskSt.goaheadnum) */
#define LINK_U_FASTWAY90 48 /* u8    90°路径动作计数(mazeState.fastwayCount90) */

/* 位打包明细(偏移 35/36/37/39/40/45/46) */
/*  35 状态A: bit0-1 dir(0=UP 1=RIGHT 2=DOWN 3=LEFT)
 *            bit2-4 mazeTask(0=WAIT 1=START 2=SEARCH 3=SPURT 4=SPURT45 5=SPURT90)
 *            bit5   wallLeft   bit6 wallFront   bit7 wallRight        */
/*  36 状态B: bit0-6 motionTask(0..107, 见 core.h)   bit7 irSwitch      */
/*  37 状态C: bit0-3 motionStep   bit4 critL   bit5 critR
 *            bit6   left45                       bit7 right45          */
/*  39        bit0-3 posX(0-6)                     bit4-7 posY(0-6)     */
/*  40        bit0-3 goalX                         bit4-7 goalY         */
/*  45 前墙  : bit0 frontNear  bit1 frontFar  bit2 frontBoth  bit3-7 预留
 *            ⚠️ **派生量**, 由 wallSns.front + irCfg.thrFrontNear 在组帧时算出;
 *              恒等式 `frontNear | frontFar == wallSns.front` 且两者互斥 ⇒
 *              "墙落在哪一档"可直接读, 而 wallSns.front 的既有消费点一字未改。 */
/*  46 冲刺  : bit0-2 spurtMode(0=未选 1=90LOW 2=45LOW 3=MED 4=HIGH)
 *            bit3 spurtFlag  bit4 goOrBack  bit5 spurtTestFlag
 *            bit6 objectGoToFlag   bit7 预留                          */

/* 位宽与值域上限(供组帧裁剪与上位机静态断言共用) */
#define LINK_UB_DIR_W 2
#define LINK_UB_MAZETASK_W 3
#define LINK_UB_MAZETASK_MAX 5
#define LINK_UB_MOTIONTASK_W 7
#define LINK_UB_MOTIONTASK_MAX 107 /* 见未决项: 枚举再扩张就会溢出, 须加静态断言 */
#define LINK_UB_MOTIONSTEP_W 4
#define LINK_UB_POS_W 4
#define LINK_UB_GOAL_W 4

/* ---- v2.3 新增位定义 ----
 * 前墙双档(偏移 45): 三者互斥性由构造保证 —— near/far 二选一(有墙时),
 * both 是"原双传感器互证式成立"(与 near/far 正交, 供调试与后继决策)。 */
#define LINK_FRONT_NEAR 0x01 /* 贴脸: 任一路 < irCfg.thrFrontNear */
#define LINK_FRONT_FAR 0x02  /* 提前: 有墙但两路都还没进近距档 */
#define LINK_FRONT_BOTH 0x04 /* 原式条件①/②(双传感器互证)成立 */
/* 冲刺档位与标志(偏移 46) */
#define LINK_UB_SPURTMODE_W 3
#define LINK_UB_SPURTMODE_MAX 4 /* 见 spurt.h SpurtSpeed_t(60..63 -> 1..4) */
#define LINK_SPURTBF_FLAG 0x08   /* taskSt.spurtFlag */
#define LINK_SPURTBF_GOBACK 0x10 /* taskSt.goOrBack(去/回) */
#define LINK_SPURTBF_TEST 0x20   /* mazeState.spurtTestFlag */
#define LINK_SPURTBF_OBJECT 0x40 /* mazeState.objectGoToFlag(回坐标中) */

/* ---- 时间同步基准灯(相对独立于校准与各测试档) ----
 * 毫秒时基 linkTimeMs 由 SysTick 每 1ms 自增(u32, 49 天不绕), 上电起走。
 * 收到 ARM 命令即"发车": 记下基准 armMs 并点亮 1Hz 时间基准灯(50% 占空比),
 * armMs 由 `ARM` 的应答直接带回(v2.0: 不再单发 ARMED 行, 也不再单发 TIME 帧)。 */
#define LINK_TIME_LED 4    /* 时间基准灯 = LED4(PC7)。见 led.h 的 ledBeaconLock */
#define LINK_TIME_DIV 1000 /* 基准灯周期(ms) = 1Hz */

/* ---- 发车标记灯(v2.8, 仅远程档) ----
 * 远程档收到 `MAZE RUN` 的**同一时刻**点亮全部 4 个 LED(LED1-4, 低电平点亮,
 * 直接写引脚), 并保持本值毫秒后释放 —— 供录像/外部设备无歧义地对齐发车时刻
 * (LED 由暗变全亮的那一帧就是发车帧)。窗口结束后一切照旧(基准灯/状态灯原行为),
 * 即"此后的 LED 功能不变"。
 * ⚠️ 窗口内 LINK_Tick 每 1ms 重写一次引脚, 故窗口期间**基准灯(LED4)与迷宫状态
 *    灯都会被压住**; 若把本值取大, 会让外部看到的 1Hz 方波出现一段缺口。
 *    v2.8 取 300ms(= 1Hz 基准灯半周期的 0.6 倍), 亮 4 灯 vs 基准灯仅亮 1 灯,
 *    在录像里两种图案可区分; 要"更醒目"就调大, 要"更贴近瞬时"就调小。 */
#define LINK_LAUNCH_MARK_MS 300

/* ---- 统一帧 flags 位 ----
 * ⚠️ DUMPING(0x10) 在 v2.0 **恒为 0**: 它描述的是已删除的 dump 机制。
 *    位定义保留, 以免上位机的位解释漂移。
 *    bit7 由已废弃的 RUNLOG 改为 EVQ —— 见设计方案 §8.3 的"状态帧 EVQ 位"。 */
#define LINK_FLAG_FREEMODE 0x01 /* 自由模式 freemode==1: 电机输出被强制为 0(不是"解锁"!) */
#define LINK_FLAG_SLEEP 0x02    /* 休眠(sleepmode==1) */
#define LINK_FLAG_EMERG 0x04    /* 急停生效(本次上电以来触发过一次, 由 emergMon.active 镜像) */
#define LINK_FLAG_PULLF1 0x08   /* v2.6.1: **本帧 = 收到 PULL 后发出的第一帧**(批首帧)。
                                 *  远程档专属: 由 link_RateService 的 PULL 分支在组帧前置位,
                                 *  只标一帧; 比赛档没有 PULL ⇒ 恒 0。
                                 *  占用原 SCOPING(0x08) 位 —— 那套示波器机制 v2.0 已删除,
                                 *  该位自 v2.0 起恒 0、全树无置位点、无消费者, 故可安全复用。
                                 *  动机: 上位机(host/link_pull_evt.py)"批起点"判据原先只能靠
                                 *  ms8 帧间隙**推算**, 四类误判(归档错/折回/换档/1ms 刀锋)都
                                 *  会让它**提前触发** ⇒ 多发命令 ⇒ 速率顶过链路容量。现在
                                 *  判据直接读这一位 = 读事实。见 docs/链路MTBF压测方法.md §14.15。 */
#define LINK_FLAG_DUMPING 0x10  /* [v2.0 恒 0] dump 传输中 */
#define LINK_FLAG_MOTOR_OFF 0x20 /* [保留恒 0] 曾用于"安全测试模式(电机禁用)"; v2.4 取消该档后无发送方 */
#define LINK_FLAG_ESTOP_LOCK 0x40 /* 急停**锁存**中: 新的 MOVE/CAL 会被拒(ERR_ESTOP), 须 STOP 解锁 */
#define LINK_FLAG_EVQ 0x80        /* v2.0(替代 RUNLOG): 有事件行因 TX 空间不足被丢弃 → 事件流不完整 */

/* ---- 缓冲区尺寸(TX 环为 2 的幂, 便于位运算取模) ----
 * v2.0: SNAP 删除后不再有"一条 250B 文本必须整体入队"的需求,
 * 256B 足够(一帧 57B + printf 文本), 省 256B RAM。
 *
 * v2.4: **远程档扩到 512B** —— `GET ALL` 要一次输出 24 行 Q16.16 文本
 *       (最长 10 位整数), 合计约 343B, **超过 256B**。而内部 link_TxPut()
 *       不做空间检查(见 link.c) ⇒ 环满时 head 越过 tail, **回绕覆盖尚未
 *       被 DMA 发出的字节**。2026-10-07 真机实测: `GET ALL` 只回
 *       `P 22..38` 共 17 行, 前 7 项标定参数(TRACKL..WALLDIST)被吃掉;
 *       而单项 `GET TRACKL` 却完全正常 —— 极易误判成"标定页失效/未标定"。
 *       比赛档不跑上位机 `GET ALL`, 故保持 256B(产物逐字节中性)。
 * ⚠️ PONG 的第 4 个字段就是本值 ⇒ 远程档指纹变为 `PONG 5 216 7 512 <rst>`
 *    (这反而有用: 一眼能看出板上烧的是不是带本次修复的固件)。 */
#ifdef MOUSE_REMOTE
#define LINK_TX_BUF_SIZE 1024 /* TX 环形缓冲(必须是 2 的幂)。
                               * v2.5: 参数表 41 项 ⇒ GET ALL 41 行 ≈ 700B, 512B 已不够
                               * (会触发"环满丢弃"兜底丢行), 故扩到 1024。
                               * RAM 核对: 远程档 17.6KB/20KB(F103RB), 余量健康。 */
#else
#define LINK_TX_BUF_SIZE 256 /* TX 环形缓冲(必须是 2 的幂) */
#endif

/* ---- 事件行(上行异常) ----
 * 只有两条: `STOPPED <reason> #n` / `ESTOP TRIP #n`。
 * 序号 `#n` 是 1 字节循环, 与遥测帧的 SEQ **共用同一计数器空间**(linkSeq) ——
 * 于是主机既能检出事件丢失(step>=2), 也能判定帧与事件的相对顺序。
 * 发送前要求 TX 空闲 >= LINK_EV_NEED_SPACE, 不足则丢弃并计数(linkEvDrop)。 */
#define LINK_EV_NEED_SPACE 64u

/* ---- Flash 标定参数页 ----
 * 页规划（与 flash.h 同步, 2026-10-04 调整; v2.4 起 page 116 转为备用）:
 *   page 0..100   程序区(101 KB, IROM1 上限 0x19400)
 *   page 101      运行数据计数
 *   page 102..114 运行数据录制区（擦除范围正好 13 页）
 *   page 115      本模块的生产标定页 ← 原为 page 99
 *   page 116      备用(原"课程调参参数页", 随课程实验层在 v2.4 一并删除)
 *   page 117      备用
 *   page 118..127 迷宫/路径/红外/陀螺零偏
 *
 * ⚠️ 为什么从 99/100 上移到 115/116:
 *   运行数据区的擦除范围是 `saveNum/100+1 = 13 页`, 即 **page 89..101**。
 *   原先取 page 99/100 时, 两个标定页都落在该范围内 —— 一次 FLASH_EraseRunData()
 *   就会把生产标定一起擦掉。上移后录制区为 102..114, 不再相交。
 *   **后续若再改运行数据区或 saveNum, 必须重新核对本页是否落在擦除范围内。** */
#define FLASH_ADDR_SYSCALIB 0x0801CC00   /* page 115 */
#define LINK_CALIB_MAGIC 0x4D435331 /* 'MCS1' */
#define LINK_CALIB_VER 1
#define LINK_CALIB_PAYLOAD_OFF 16   /* 头 12B + 4B 对齐预留 */

/* ---- 标定参数 id ----
 * 分段约定(与上位机 protocol/commands.py 的 PARAM_IDS 必须逐项一致):
 *   1-7   生产标定     落 Flash page115
 *   22-38 红外测量+电源 落 Flash page115 (本文件下方)
 * ⚠️ v2.4: 原"8-21 课程调参(独立 page116)"整段删除 —— 课程实验层已移除,
 *   生产表条目数 `LINK_P_NUM` 保持 24 不变(因此 page115 的旧数据仍有效)。
 */
#define LINK_P_TRACKL 1   /* sysCfg.trackRatioL   里程脉冲/mm */
#define LINK_P_TRACKR 2   /* sysCfg.trackRatioR */
#define LINK_P_SPEEDL 3   /* sysCfg.speedRatioL   (pulse/ms)/(cm/s) = TRACKL/100 */
#define LINK_P_SPEEDR 4   /* sysCfg.speedRatioR */
#define LINK_P_GYRO 5     /* sysCfg.gyroRatio */
#define LINK_P_ENCANG 6   /* sysCfg.encAngleRatio */
#define LINK_P_WALLDIST 7 /* sysCfg.distToWall */

/* ---- 红外测量与阈值(22-32): 全部映射到 IrCfg, 默认值 = 原 WALL_* 宏值 ---- */
#define LINK_P_IRTHRWALL 22   /* irCfg.thrWall      侧墙阈值 mm       */
#define LINK_P_IRTHR45 23     /* irCfg.thr45        45 度阈值 mm      */
#define LINK_P_IRTHRCRIT 24   /* irCfg.thrCritical  临界墙阈值 mm     */
#define LINK_P_IRTHRFLO 25    /* irCfg.thrFrontLo   前墙远距-低 mm    */
#define LINK_P_IRTHRFMID 26   /* irCfg.thrFrontMid  前墙远距-中 mm    */
#define LINK_P_IRTHRFHI 27    /* irCfg.thrFrontHi   前墙远距-高 mm    */
#define LINK_P_IRTHRFNEAR 28  /* irCfg.thrFrontNear 前墙近距 mm       */
#define LINK_P_IRKALQ 29      /* irCfg.kalQ         卡尔曼 Q ×10000   */
#define LINK_P_IRKALP 30      /* irCfg.kalP         卡尔曼 P ×10000   */
#define LINK_P_IRKALSW 31     /* irCfg.kalOn        卡尔曼开关 0/1    */
#define LINK_P_IRFRONTMODE 32 /* irCfg.frontMode    前向综合 0/1/2    */

/* ---- 电源监控(33-38): 全部映射到 PowerCfg ---- */
#define LINK_P_BATTKV 33     /* powerCfg.battKv    分压系数 µV/LSB   */
#define LINK_P_BATTLOWMV 34  /* powerCfg.battLowMv 低电 mV           */
#define LINK_P_BATTHIGHMV 35 /* powerCfg.battHighMv 满电 mV          */
#define LINK_P_BATTALPHA 36  /* powerCfg.battAlpha IIR ×1000         */
#define LINK_P_BATTMAXPCT 37 /* powerCfg.battMaxPct 档位上限         */
#define LINK_P_BATTMINPCT 38 /* powerCfg.battMinPct 档位下限         */

/* ---- 运动/控制参数(39-55): 课件《电脑鼠参数调整表》全量入表(v2.5) ----
 * 宿主变量即运行时配置本体: 远程档经 link_ParamApplyOne 写入,
 * 比赛档不用这些 id(保持编译期常量初始化, 产物逐字节不变)。
 * 默认值全部 == 各编译期常量(下表注释), 因此"未 SETQ 时行为逐位不变"。 */
#define LINK_P_TRACKCOORD 39 /* sysCfg.trackCoord   纵向矫正灵敏度(格)   默认 145  */
#define LINK_P_TURNDELAY 40  /* turnDelay(motion.c) 转弯延迟格数(float)   默认 16.5 */
#define LINK_P_SPDSEARCH 41  /* speedCfg.search     搜索直线速度          默认 95   */
#define LINK_P_SPDTURN 42    /* speedCfg.turn       转弯速度              默认 60   */
#define LINK_P_DISF60 43     /* linkDisF60          180°前对正阈值 mm     默认 60   */
#define LINK_P_GSKP 44       /* pidGain.goStraight  盲走直行P增益         默认 0.4  */
#define LINK_P_TURNP 45      /* pidGain.turnP       转弯P增益             默认 5.0  */
#define LINK_P_TURND 46      /* pidGain.turnD       转弯D增益             默认 4.5  */
#define LINK_P_PTKP 47       /* linkPtKp            原地转P增益           默认 2.5  */
#define LINK_P_PTKI 48       /* linkPtKi            原地转I增益           默认 0.01 */
#define LINK_P_PTKD 49       /* linkPtKd            原地转D增益           默认 0.1  */
#define LINK_P_MKPL 50       /* linkMtrKpL          左轮电机P             默认 10.75 */
#define LINK_P_MKIL 51       /* linkMtrKiL          左轮电机I             默认 0.55 */
#define LINK_P_MKDL 52       /* linkMtrKdL          左轮电机D             默认 0.55 */
#define LINK_P_MKPR 53       /* linkMtrKpR          右轮电机P             默认 10.20 */
#define LINK_P_MKIR 54       /* linkMtrKiR          右轮电机I             默认 0.5  */
#define LINK_P_MKDR 55       /* linkMtrKdR          右轮电机D             默认 0.5  */

#ifdef MOUSE_REMOTE
#define LINK_P_NUM 41 /* 远程档生产表条目数 = 1-7 + 22-38 + 39-55 */
#else
#define LINK_P_NUM 24 /* 比赛档保持 v2.4 表容量(产物逐字节不变) */
#endif
/* (39-55 的宿主变量 extern 与消费侧门控宏放 core.h/motor.h ——
 *  isr.c/motion.c/motor.c 的 include 链只达 core.h/motor.h, 不达本文件。) */

/* ================= RATE 命令族常量(远程档) =================
 * 固件按 PC 指定的节拍自时钟发帧。三种模式**互斥**, 共用 link_RateService:
 *   FIX  —— 固定节拍: 自时钟按**恒定** F 持续发帧, 不看门狗、不降速、不停发,
 *           直到 RATE OFF。用途: 以固定 FPS 为通信基准做连续通信 / MTBF 压测,
 *           便于在不同环境下重复测试、比对断连时刻分布。
 *   PULL —— PC 主导的批量节拍(见下), 事件触发式拉取(周期 ≡ RTT)的载体。
 *   OFF  —— 关掉自时钟, 回到纯 `TELEM` 拉取(v2.5 行为)。
 *
 * 为什么不做 AIMD 自适应(v2.6-exp 的做法, 已删除): 那套"帧尾广播 F + PC 按实收
 * 速率回 UP/KEEP/DOWN + 固件看门狗静默停发"是为**探索链路容量上限**服务的;
 * 容量已量清(24~30 Hz 之间), 而实际主力是事件触发式拉取 —— 速率由 `t` 直接算出、
 * 不需要任何反馈, 于是 ON/UP/KEEP/DOWN 与帧尾广播字段全部成为零消费者, 一并删除。 */
#ifdef MOUSE_REMOTE
#define LINK_RATE_FIX_HZ_MIN 1   /* RATE FIX 频率下限(整数 Hz) */
#define LINK_RATE_FIX_HZ_MAX 30  /* RATE FIX 频率上限(整数 Hz) */
/* ---- PULL 批量节拍模式(PC 主导的信用/节拍) ----
 * `PULL <n> <t_ms>`: 单片机按 t_ms 间隔连续发 n 帧, 发完即静默等待下一条 PULL;
 * 期间收到**新的 PULL** 立即打断, 按新参数重排(旧批次作废)。
 * 用途: 让上位机直接指定"每次拉取发几帧、间隔多少", 从而
 *   ① 批量摊薄命令/应答往返(RTT); ② 用 n 与 t 组合制造"突发+静默"工况,
 *   检验给透传模块留空闲时间能否延长 MTBF; ③ 上位机按实收情况闭环调 t。
 * 与 RATE FIX 互斥(置 PULL 即退出固定节拍, 反之亦然)。
 * ★ v2.6.1: 每批的**第 1 帧**带 `LINK_FLAG_PULLF1`(统一帧 flags bit3) —— 上位机的
 *   "批起点"判据直接读这一位(读事实), 不再靠 ms8 帧间隙推算(见 link.h 的 flags 位说明)。 */
#define LINK_PULL_N_MAX 16     /* 每次拉取最多帧数 */
#define LINK_PULL_T_MS_MIN 1   /* 批内帧间隔下限(ms) */
#define LINK_PULL_T_MS_MAX 5000 /* 批内帧间隔上限(ms) */
/* 组帧节拍累加器: 每 ms 累加 F(Q8.8), 满 1 帧(256*1000)就发一帧。
 * 校验: 每 ms 加 F*256, 1000ms 共加 F*256000 => F 帧。 */
#define LINK_RATE_FRAME_UNIT 256000u
#endif

/* 参数值域钳制(link_ParamApplyOne 用): 防止写进离谱值把判定打穿 */
#define LINK_IR_THR_MAX 1000u /* 红外阈值上限 mm(>表长即等价关闭) */
#define LINK_IR_KAL_MAX 60000u /* 卡尔曼 Q/P 上限 ×10000 */
#define LINK_BATT_KV_MIN 1u    /* 分压系数下限 µV/LSB */
#define LINK_BATT_KV_MAX 20000u

/* ---- 标定动作 ----
 * v2.0: 标定改为**请求-响应**。`CAL <item> START` 不再立即 ACK,
 * 而是等该步采集完成才回 ACK(见 link_CalibService 的 ackSeq 机制)。
 * IRDATA 的"相位"由 `CAL IRDATA <n>`(n=0..3) 表达, 每步完成回一次 ACK。 */
#define LINK_ACT_START 0
#define LINK_ACT_STEP 1
#define LINK_ACT_DONE 2
#define LINK_ACT_ABORT 3
#define LINK_ACT_SAMPLE 4 /* 内部: 触发一次采样(IRDATA 逐点采集用) */

/* ---- 标定项(LINK_CAL_*, 对应命令 CAL <item> <act>) ---- */
#define LINK_CAL_GYRO 1      /* 陀螺零偏 -> 既有 page127 */
#define LINK_CAL_WALLDIST 2  /* 车心到墙距离 */
#define LINK_CAL_IRDATA 3    /* 红外 8 值 -> 既有 page123 */
#define LINK_CAL_GYRORATIO 4 /* 陀螺角度系数(以红外极值为物理基准) */
#define LINK_CAL_ENCANGLE 5  /* 编码器角度系数(同一基准) */

/* ---- v2.7: 按键菜单功能的远程化(仅远程档识别, 比赛档回 ERR_NAME) ----
 * 动机: 远程档跳过 `MENU_Run()` 后, 原先靠按键菜单做的维护动作(擦红外标定/红外自检/
 *       显示传感器/计算红外/看块数据/擦运行数据)就没有入口了。这里把它们挂到既有
 *       `CAL` 命令上, **命名与语义沿用请求-响应模型**, 上位机零新概念。
 * 实现约束(见 link.c `link_CalMenuRemote`):
 *   1) 全部**立即 ACK** —— 没有"采完才回"的等待, 也不碰 linkCal 挂起机制;
 *   2) 全部**不阻塞**(无 DELAY_Ms / 无 while 等按键) —— 命令执行在 SysTick 上下文,
 *      阻塞会拖垮 1ms 节拍; 原菜单里"等按键确认"的步骤由上位机分条命令替代;
 *   3) 擦除类**不加固件侧安全门** —— 确认弹窗由上位机做(registry precheck +
 *      api/calib.py 的 confirm 参数), 与 `maze.run` 的低电闸同一思路。
 * ⚠️ 这几个宏本身不生成代码, 故无需门控; 但**识别它们的 strcmp 分支**在
 *     `link_CalItemToId` 里必须门控, 否则比赛档 hex 会变。 */
#define LINK_CAL_ERASEIR 6  /* 擦除红外标定页(page123) —— 菜单 2 */
#define LINK_CAL_IRSELF 7   /* 红外自检(**仅传感器段**) —— 菜单 4;
                             *   原菜单后半段"前向开电机扫 FORWARDIRTEST"不远程化:
                             *   远程已有 MOVE RUN 双护栏, 没必要复刻一套无护栏动作 */
#define LINK_CAL_SENSOR 8   /* 显示传感器数据(固定 4 行) —— 菜单 5 */
#define LINK_CAL_IRCAP 9    /* 红外 8 值**分步**标定, act = 步号 1..4 —— 菜单 6;
                             *   每步采当前摆位下的传感器值, 第 4 步写 Flash */
#define LINK_CAL_BLOCK 10   /* 块数据概览: Flash 重载 + 45/90 路径计数 + 红外 8 值
                             *   —— 菜单 7; 墙图本身仍走 `MAP DUMP`(98B 帧) */
#define LINK_CAL_ERASEALL 11 /* 擦除迷宫/路径 6 页(BLOCK/BLOCK_POINT/GOPATH/BACKPATH/
                             *   GOPATH_90/BACKPATH_90) —— 菜单 8;
                             *   ⚠️ **不休眠**: 原菜单 8 擦完进流水灯死循环, 远程档
                             *      睡下去就失联了, 必须返回值守态 */

/* ---- 移动/标定子状态 ---- */
#define LINK_MOVE_NONE 0
#define LINK_MOVE_FWD 1  /* 固定脉冲直线 */
#define LINK_MOVE_ROT 2  /* 固定 PWM 定长原地转 */
#define LINK_MOVE_RUN 3  /* 受"时间 + 红外"双护栏约束的远程运动(电机远程控制实验) */
#define LINK_SRV_NONE 0
#define LINK_SRV_SWEEP 1 /* 全周扫描找最小, 记录基准 */
#define LINK_SRV_PARK 2  /* 停到最近极小 */

/* ---- 电机远程控制实验: MOVE RUN 的护栏常量 ----
 * 为什么护栏要放在固件里(而不是上位机定时发 STOP):
 *   上位机可能卡死、蓝牙可能断、操作者可能离开 —— 而这条链路是不做配对的一次性透传。
 *   把"到时必停 / 贴脸必停"放在 1ms ISR 里判定, 停止就不再依赖任何外部条件。 */
#define LINK_RUN_MS_DEF 2000   /* 默认最长运行时间(ms)。2 秒是实验建议值 */
#define LINK_RUN_MS_MAX 60000  /* 上限, 防止误敲出天量数字把护栏架空 */
#define LINK_RUN_IR_DEF 10     /* 默认红外停车阈值(mm) = 1cm */
#define LINK_RUN_IR_MAX 3000   /* 阈值上限(大于红外量程即等于关闭) */
#define LINK_RUN_SPEED_MAX 300 /* 速度绝对值上限(cm/s)。超限直接 ERR_VALUE, 不再靠饱和 */

/* ---- 远程软复位(`MCU RESET`)----
 * 为什么要有它: 走迷宫的主循环里有 30 余处**非协作式阻塞等待**在等物理进度
 * (`while (odomStr.cellOdom < dis) ;` 之类), 而 `STOP` 会把 freemode 置 1、
 * 让电机永不再转 ⇒ 那些条件永远不成立 ⇒ 主循环卡在函数内部, `LESSON_Blind8()`
 * 顶部的退出检查轮不到。表现是"命令照回 OK, 但车再也不响应, 只能断电"。
 * 本命令给出一个**不必断电**的出口(真机实测 2026-10-05)。
 * 仅远程档(MOUSE_REMOTE)编译; 按键比赛档不含此命令 —— 比赛要求人在现场。 */
#define LINK_MCU_RESET_DELAY_MS 150 /* 先发 ACK, 延时这么多 ms 后再复位
                                     * (远小于上位机 2.5s 命令超时, 又留足蓝牙转发时间) */

/* ---- STOPPED 的停止原因(设计方案 §8.1) ----
 * 语义变化(v2.0): 旧的 `MOVEDONE RUN <REASON> <elapsed> <ir_min> ...` 只剩 reason,
 * 明细改由统一帧连续采样获得(见 link_MoveFinishRun)。 */
#define LINK_RUN_REASON_TIME "TIME"   /* 时间护栏到 */
#define LINK_RUN_REASON_IR "IR"       /* 近墙保护触发 */
#define LINK_RUN_REASON_ESTOP "ESTOP" /* 急停 */
#define LINK_RUN_REASON_USER "USER"   /* STOP 命令 */
#define LINK_RUN_REASON_END "END"     /* 任务正常跑完(固定脉冲直线/定长转) */

/*-------------------------------------  F U N C T I O N S  -------------------------------------*/
void LINK_Init(void); /* 链路初始化: RX 优先级/DMA/标定参数载入(须在 HARDWARE_Init 之后调用) */
void LINK_Tick(void); /* 每 1ms 由 SysTick_Handler 调用(须置于 sleepmode 分支之外) */

/* 急停通知(由 isr.c 的 SysTick_Handler 在 emergMon.active==1 时调用)。
 * 作用: 把"急停已发生"这一事实锁存到链路层 ——
 *   1) 统一帧 flags 的 LINK_FLAG_ESTOP_LOCK 置位, 上位机一眼可见;
 *   2) 之后所有 MOVE/CAL 命令回 ERR_ESTOP, 直到显式 STOP 解锁;
 *   3) 上行一行 `ESTOP TRIP #n`(最低延迟, 不等任务收尾)。
 * 这样操作者在任何时候按下急停, 系统都会**停在锁死状态**而不是悄悄恢复。 */
void LINK_EStopTrip(void);

/* ---- 远程发车(见 docs/数据收集与时间同步.md) ----------------------------------------
 * 设计要点: **时基与基准灯都在 1ms 中断里维护**, 因此无论主循环在做什么
 * (包括卡在 LESSON_Blind8() 的 while(1) 里跑迷宫), 计时与灯都不受影响;
 * 反过来, 命令解析与遥测组帧也在同一个中断里完成 —— 所以跑迷宫期间
 * 上位机依然能收数据、发命令。数据收集能"搭"在阻塞式自走上, 靠的就是这一点。 */
u8 LINK_MazeRequested(void);  /* 是否已收到 MAZE RUN 请求 */
void LINK_MazeStarted(void);  /* 由 main() 在真正进入迷宫前调用: 消费请求位 + 置"已在跑" */
void LINK_MazeFinished(void); /* 由 main() 在退出迷宫后调用: 复位发车状态, 以允许再次发车 */

#ifdef MOUSE_REMOTE
/* ---- 远程发车目标(`MAZE RUN [BLIND|RACE]`) ----
 * 只有远程档编译这两条命令分支。**不带动作词时 = BLIND**, 因此 v2.3.0 的上位机
 * 不做任何修改也照常工作(它发 `MAZE RUN`, 固件按 BLIND 处理)。
 *   BLIND —— 实验8: 直接用内存里的地图做无记忆走迷宫;
 *   RACE  —— 正式比赛入口序列: 每次发车前重载 Flash 地图/起终点/目标方向, 再探索。
 *            (冲刺选档的两个 while(1) 在远程档被跳过, 见 APP/state.c。) */
#define LINK_MAZE_BLIND 0
#define LINK_MAZE_RACE 1
u8 LINK_MazeTarget(void); /* 取本次发车目标(在 LINK_MazeStarted() 之后有效) */

/* 是否有"退出迷宫"请求(LESSON_Blind8 的 while(1) 用; 见 APP/lesson.c)。
 * 只有 `MAZE STOP` 与 `ESTOP` 会置位。**按键比赛档不编译本声明与对应检查**,
 * 于是 LESSON_Blind8 的机器码与原来完全一致。 */
u8 LINK_MazeAbortRequested(void);
#endif /* MOUSE_REMOTE */

/* printf 出口: usart.c 的 fputc() 调用它, 使 printf 与遥测共用 TX 环缓冲。
 * 返回 1=已入队, 0=环满已丢弃(不阻塞)。 见 link.c 中的详细说明。 */
u8 LINK_PutChar(u8 b);

#endif
