# 上位机 API 变更记录（CHANGELOG）

> 记录 `host.api` 门面层的每次接口变更。
> 由接口化改造（`docs/上位机接口化设计方案.md`）引入。
>
> **2.0.0 是这条约定的一次显式破例**：本次是协议的**整体替换**，不做
> `_v2` 名称 + 弃用期，理由是"兼容层本身就是要删掉的那个东西"（用户决定，见
> `docs/传输模式重构设计方案.md` §1）。因此下面把**删除**与**新增**并列写清楚。

---

## [2.2.1] — 2026-10-05

### 安全修正：`MCU RESET` 复位前必须先停车（真机事故的直接响应）

**事故经过**：v2.2.0 的 `MCU RESET` 在真机上使用后，现场出现
**电机持续旋转、且对红外障碍物不再响应**，操作者被迫强行断电。

**归因（两层，都要修）**：

1. **`MCU RESET` 自身没停车。** 首版实现只置倒计时就 ACK，没有停电机 ——
   于是 ACK 延迟 ~150ms + 复位生效窗口这段时间里，电机仍在 PWM 驱动下转，
   机器人**带着动力滑行**。修复：复位前先走 `link_StopMotion()`
   （`freemode=1` ⇒ `MOTOR_LoopControl()` 输出恒 0，见 `motor.c`），
   让倒计时在**静态**下度过。
2. **复位只复位固件，不复位物理位置 —— 而调用方紧接着就发车了。**
   复位后 `mousePos`/`mapWall`/`stackIdx` 全部回到 BSS 零值，但车还在迷宫原处。
   此时发 `MAZE RUN`，探索会按"**我在起点 (0,0)**"的错误假设规划路径；
   而墙检测只用来**更新地图**、不是硬停车条件 ⇒ 车既按错图走、又不因遇墙而停。
   这解释了"一直转 + 对红外不敏感"。

**批量电池读数印证了更根本的诱因**：同一次现场，`battMv` 一路
**7681 → 6747 → 3962 → 4270 mV**（松载回升，典型带载跌落）。而固件
`safety.c` 只算出 `battMon.low` 供**显示**（LED/遥测），**没有任何低电保护动作**。
低电时红外发射管变暗 ⇒ 回波微弱 ⇒ 墙检测失效；电机驱动在跌落电压下行为亦不确定。

### 新增：发车前的低电硬闸（`ERR_LOWBATT`）

- `registry` 新增 `LAUNCH_MIN_MV = 7000`（与固件 `BATTLOWMV` 默认值一致，
  但这是**上位机的独立防线**，改设备参数不会放宽它）。
- `CommandSpec` 新增 **`precheck`** 字段：发送前的现场安全检查，返回非 `None` 即
  拒绝发送（不碰链路）。挂在 `maze.run` 上。
- **为什么放在登记表层而不是某个 API 方法里**：先前 `MazeAPI.run()` 里也能检查，
  但只要有人写 `mc.send_cmd("maze.run")` 就绕过去了 —— 而现场脚本恰恰这么写。
  放在 `send_cmd` 里执行，脚本 / CLI / `datalog` / 将来 JSON-RPC **一律绕不开**。
  明确的绕过通道是 `send_raw("MAZE RUN")`（逃生舱，一眼可见）。
- `battMv == 0`（电池监视未开启）或取不到帧时**不拦** —— 避免误锁死台架。

### 调用方修正（我自己引入的缺陷）

`host/maze_relaunch_repro.py` 原先在救援成功后**自动再发车**（`MAZE RUN #3`），
正是上面第 2 条的触发路径。现改为**默认只报告就绪状态、不发车**，
并打印"请先把电脑鼠放回起点角"；确要让脚本发需显式 `--relaunch`
（带 5 秒可中止倒计时）。

### 接口变更

- `registry.API_VERSION` 2.2.0 → **2.2.1**。
- 固件 `LINK_FW_VER` 213 → **214**（**线格式未变**，`LINK_PROTO_VER` 仍为 3）。
- 新增返回状态 `ERR_LOWBATT`（**上位机产生，不来自设备**；`data.blocked=True`）。

### 影响面

- `MCU RESET` 的改动在 `#ifdef MOUSE_ESTOP_RECOVERABLE`（=[1][2][3][4]）之内：
  `[5]` 比赛固件 ROM **90872 B，与改动前逐字节同尺寸**。
- `[4]` 档 ROM 75652 → **75660**（+8 B，一次 `link_StopMotion()` 调用）。

### 验证

- 九套件 **553 项全绿**（`maze_smoke` 42 → **48**，新增 6 项钉死低电闸：
  前置校验已挂 / 低电拒绝且带实测电压 / 充足放行 / `battMv==0` 不误拦 / 无帧不误拦）。
  ⚠️ 这些断言直接测判定函数本身 —— **确定性**，不受模拟器电池漂移影响。
- **真机验证**（电池恰好处于低电，是个活案例）：
  `battMv = 4270 mV` → `maze.run` 回 **`ERR_LOWBATT` / `blocked=True`**，
  之后 `mazeTask` **仍 `WAIT`、speed 0/0** ⇒ **车没有被发出去**，闸门生效。

### 真机验收（fw=214，换新电池后完整重跑）

| 项 | 实测 |
|---|---|
| 指纹 | `PONG 3 214 7 256 POR` |
| 电池 / 低电闸 | `7679~7724 mV` → 判定 **放行**（`None`） |
| `MCU RESET`（静止） | `rst=SW`、`freemode=True`、`speed=0/0`、`task=WAIT` |
| **`MCU RESET`（运动中）★** | 车原地转 `speed=35/-35` 时发复位 → 复位后连续 6 帧**全部 `speed=0/0`、`motion=NULL`** ⇒ **没有继续转**（停车修正生效） |
| 发车 | `MAZE RUN → OK`，`WAIT → START → SEARCH`，(0,0)→(6,7)，速度出现 69~120 ⇒ **真的走起来了** |
| `MAZE STOP`（车在运动时） | 回 `OK` 但**停在 `SEARCH`**，`MAZE RUN` 仍 `ERR_BUSY` ⇒ **该路径在运动中无效**（见下） |
| **救援闭环 ★** | 卡住 → `MCU RESET` → `WAIT`（**1064 ms，无需断电**）→ **`MAZE RUN → OK`** |

**由此确定一条操作事实（已写进 `host/README.md` 与脚本抬头）**：

> **`MAZE STOP` 在车运动时不可靠；可靠收车手段是 `MCU RESET`。**
> `MAZE STOP` 只置一个"请退出"标志（`linkMazeAbort`），而主循环有 30 余处
> **非协作式阻塞等待**。车在运动时程序正卡在那些循环**内部**，而标志检查在
> `LESSON_Blind8()` 的 while(1) **顶部** —— 根本轮不到。`MCU RESET` 整机软复位
> （BSS 清零）则总能收车。

**另一条操作要点（实测）**：`MCU RESET` 之后**第一帧新遥测要约 3.2 s 才到**
（实测 `t=0.22 s` 仍是复位前的旧帧 `seq=10`，`t=3.23 s` 才是复位后首帧 `seq=0`）。
且 `main()` 执行到 `speedSt.freemode = 1` 之前发出的帧带着 **BSS 初值**
（`freemode=False`）。⇒ **复位后判读状态必须轮询等到新鲜帧**，不能读一次就下结论。
（`maze_launch_probe.py` 已按此实现。）

`host/maze_launch_probe.py --cycle` 也据此改写：`MAZE STOP` 降为"参考项"
（如实报告是否真的收车了），**`MCU RESET` 成为必过的收车路径**。

### 仍未做

### 仍未做

- **卡死/失控自动探测**（第二步）：固件检测到"声称在动但编码器无增量"时**只上报 STOP**，
  是否 `MCU RESET` 由上位机决定。判据须用**进度**（现成的 `link_EncAccumulate()`）
  而非心跳 —— `LINK_RUN_MS_MAX = 60000` > IWDG 上限 ≈ 26 s，改主循环喂狗会误杀合法长运动。
- 给 30 余处阻塞等待加终止判据 / 把迷宫状态机改成非阻塞步进（治本）。
- 固件侧低电保护动作（目前完全依赖上位机闸门）。

---

## [2.2.0] — 2026-10-05

### 新增：`MCU RESET` —— 远程软复位（卡死救援口）

**动机（真机复现 2026-10-05）**：例程8 发车后停止，再发 `MAZE RUN` 会回 `ERR_BUSY`，
**只能断电**。根因不是标志位逻辑，而是**主循环里的非协作式阻塞等待**：

- `MOTION/motion.c` 有 30 余处 `while (<物理进度>) ;` —— `while (odomStr.cellOdom < dis)`
  (10 处)、`while (motionCtrl.tick < N)` (16 处)、`while (1) if (<传感器/step>) break;` (10 处)，
  **除 `MOTION_Stop()` 一处有 1s 超时外，全部无超时、无退出条件**。
- 而 `STOP` / `MAZE STOP` 会把 `speedSt.freemode` 置 1 ⇒ `MOTOR_LoopControl()` 的
  `if (freemode == 0)` 不成立 ⇒ **PWM 恒 0 ⇒ 轮子永不再转** ⇒
  那些条件**永远不可能满足** ⇒ "越停越死"。
- 命令通道照常回 `OK`，但程序卡在函数内部，`LESSON_Blind8()` **顶部的退出检查根本轮不到**
  —— 协作式"请退出"标志抢占不了非协作式阻塞循环。
- 实测旁证：卡住时 `cellOdom` 完全冻结、`motionCtrl.task == NULL`（运动状态机空闲）、
  `MOVE RUN` 仍回 `OK`（⇒ 卡的是纯计算）、`rst` 仍为 `POR`（⇒ 看门狗也不会介入，
  因为喂狗在 `LINK_Tick()` 里、而它由 SysTick 中断调用，**主循环卡死对看门狗不可见**）。

**做法**：`HARDWARE/sys.c` 里**早已存在**的 `Sys_Soft_Reset()`
(`SCB->AIRCR = 0x05FA0000 | 0x04`，SYSRESETREQ) **此前全仓从未被调用** ——
本版把它接上，新增命令 `MCU RESET`：

- 先回 `ACK OK`，再由 `LINK_Tick()` 倒计时 **150 ms** 后复位
  （150 ms 远小于上位机 2.5s 命令超时，又留足蓝牙模块转发时间；
  命令处理跑在 SysTick 里，立刻复位会让 ACK 一起消失，上位机只能看到超时）。
- **成功判据**：复位后 `PING` 报 `rst=SW` —— 复位原因名表早已就绪
  (`IWDG`/`SW`/`PIN`/`POR`)，因此可与掉电（`POR`）区分、可自动复核。
- 复位后 `[4]` 档自然回到发车等待循环，**不必断电**。

### ⚠️ 命令名易混（已在文档中显式区分）

| 命令 | 线上文本 | 作用 |
|---|---|---|
| `params.reset` | `RESET` | **只擦参数页**，让参数回落源码默认值（不重启） |
| **`system.reset`** | **`MCU RESET`** | **整机软复位**，程序从 `main()` 重跑 |

### 接口变更

- 命令 23 → **24**：新增 `system.reset`（标记 `urgent`，发送前让拉取器让路）。
- `host.api.conn` 新增 `ConnAPI.soft_reset(verify=True, timeout=8.0)` 与别名 `rescue()`；
  返回 `data` 含 `accepted` / `rst` / `verified` / `elapsed_s`。
  ⚠️ **`accepted=False` 不代表失败** —— ACK 可能与复位抢在一起；真正判据是复核 `rst == "SW"`。
- CLI 新增 `rescue` 子命令（`python -m host.cli rescue --target COM7`）。
- 固件 `LINK_FW_VER` 212 → **213**（**线格式未变**，`LINK_PROTO_VER` 仍为 3）。
- `API_VERSION` 2.1.2 → **2.2.0**。

### 影响面（对比赛固件零影响，已双重核实）

`MCU RESET` 整段门控在 `#ifdef MOUSE_ESTOP_RECOVERABLE`（= `[1][2][3][4]`）之下：

- `[5]` 比赛固件 ROM **90872 B，与改动前逐字节同尺寸**；
- **向后兼容**：旧固件（fw=212）收到 `MCU RESET` 回 `ERR_CMD`，且复位原因**保持 `POR`**
  —— 已真机实测，不会误触发。

### 验证

- 九套件 **547 项全绿**（`contract_test` 210 → **213**；`facade_smoke` 36 → **46**，
  新增 `[K] 卡死救援` 十项：命令表登记 / 线上文本 / `urgent` 标记 /
  与 `params.reset` 不混淆 / 救援前非空闲 / `soft_reset` 复核通过 /
  复核到 `rst=SW` / 复位后回 `WAIT` / 复位后链路仍可用 / `rescue()` 别名等价）。
- 五档 0 Error：`[1]27832 [2]28384 [3]32904 [4]75652 [5]90872`。

### 仍未做（下一步）

- **卡死自动探测**：本版只给了"手动救援"。自动探测（`linkMazeRun==1` 且
  `motionCtrl.task != NULL` 且**编码器 N 秒无增量** → 先置 abort，再超时则软复位）
  尚未实现。注意不能用"心跳"判据：`LINK_RUN_MS_MAX = 60000`（单条 `MOVE RUN`
  合法阻塞可达 60 s）而 IWDG 上限只 ≈ 26 s，改成"主循环喂狗"会误杀。
- 给 30 余处阻塞等待加终止判据 / 把迷宫状态机改成非阻塞步进（治本）。

---

## [2.1.2] — 2026-10-05

### 缺陷修复：急停/停止"不立即生效"（真机实测定位到链路层争用）

**用户报告**：急停（含 `STOP`）发出去以后，电脑鼠**没有立即停止**。

**实测数据（真机 COM7，原地转圈中发 `STOP`，关红外护栏，各 5 次）**：

| 场景 | 需要重发 | 从发命令到**实测转速归 0** |
|---|---|---|
| 修复前（拉取器开着） | **1/5** | **3220 ms** —— 正好等于 `MOVE RUN` 的 3000 ms 时间护栏 |
| 修复后 | **0/5** | **594 ms** |

`3220 ms` 这个数字是关键证据：**它不是"慢"，而是"根本没生效"** —— 车是被
`MOVE RUN` 自己的时间护栏停下的。另一轮还抓到 `attempts=3`（同一条 `ESTOP`
被重发 2 次，耗时 5238 ms）。

**根因**：固件 `link.c` **每 1 ms tick 只消费一行命令**（`if (USART_RX_STA & 0x80) {...}`；
bit7 未清时 `usart.c` 把后续到达的字节**直接丢弃**）。而 v2.1 的拉取器是**持续**发
`TELEM` 的 —— 于是同时下发的 `STOP`/`ESTOP` 只要与某条 `TELEM` 落进同一个 1 ms 窗口就
被丢掉；上位机只能等 `TIMEOUT_S`(2.5 s) 超时重发，最坏情况是一直没生效。
**固件侧本身无问题**：`isr.c` 在 `emergMon.active==1` 时每 1 ms 就把速度目标清零，
响应 ≤1 ms。

**修复（纯主机侧，固件一行未动）**：给 `PullRequester` 增加 `suspend()` / `resume()`，
并让 `MouseClient.send_cmd()` / `send_raw()` 在发送**时间敏感命令**前让拉取器让路，
使命令独占链路。

- `CommandSpec` 新增 `urgent: bool`；已标记 10 条：`estop` / `estop.clear` /
  `move.run` / `move.fwd` / `move.rot` / `maze.run` / `maze.stop` / `calib.cal` /
  `params.save` / `params.reset`。
- **普通读写命令照旧走快路径**（实测在拉取器开着时 8/8 成功、有重发兜底）——
  让路是有代价的（要等在途 `TELEM` 收尾），只给"慢一拍就出事"的命令打开。
- 让路实现要点：**等在途 `TELEM` 收到应答**即证明固件已消费完那一行、接收端空闲，
  因此无需额外静默；只有应答丢失时才补一小段保守静默。
- `stream.suspend()`/`resume()` 用嵌套计数保护，可由用户直接调用。

### 接口变更

- `registry.API_VERSION` 2.1.1 → **2.1.2**。固件未动（`LINK_FW_VER` 仍 212）。

### 验证

- 九套件 **534 项全绿**（`pull_smoke` 23 → **41**，新增 18 项专门钉死"让路"机制：
  suspend 必须真停发且在途清空、期间不收帧、让路期间命令仍可用、resume 恢复、
  **嵌套 suspend 只有最外层生效**、以及**拉取期间发 urgent 命令无重发**）。
- 真机复测：`STOP` 应答中位 299 ms、**转速归 0 中位 594 ms**、**0/5 重发**；
  普通命令仍走快路径（`PING` 中位 295 ms，未被拖慢）。
- 新增真机测量脚本 `host/estop_latency.py`（A/B 命令通道对照 + C/D/E/F 转圈中停止对照）。

---

## [2.1.1] — 2026-10-05

### 缺陷修复：发车/急停之后"繁忙"，无法再下达指令

**真机复现（2026-10-05，[4] 数据收集档）**：发过一次车之后，`MAZE RUN` 每次都回
`ERR_BUSY`，**连 `STOP` 也救不回来** —— 现场只能断电重启。用户报告的现象是
"测试中发车和急停可能会出现繁忙而无法下达指令"。

**根因（两个条件叠加）：**

1. `MAZE RUN` 的忙判据是 `linkMazeReq || linkMazeRun`，而 `linkMazeReq` **置位后
   只有上电（`LINK_Init`）才清零** —— `LINK_MazeStarted()` 当时只置 `linkMazeRun`，
   没有消费这个请求位。"请求"在语义上应当是**边沿信号**（main 消费即清零）；
   把它做成电平信号，就把"有一个待处理的发车请求"和"正在跑迷宫"两件事
   混进了同一个变量，是这个缺陷的根源。
2. `LESSON_Blind8()` 是 `while(1)` **永不返回**，所以发车之后**没有任何路径**能复位
   发车状态。急停只能停电机（ISR 每 1 ms 强制清速度目标），**停不掉那个循环** ——
   于是出现"车停了，但程序还在迷宫里"，后续指令继续被 busy / estop 挡回。

**修复（三处，全部门控在 `MOUSE_DATA_LOG` 之下）：**

| 改动 | 说明 |
|---|---|
| `linkMazeReq` 改在 `LINK_MazeStarted()` 里清零 | 请求位变成边沿信号（main 消费即清） |
| **新增 `MAZE STOP`** | 带出迷宫 + 复位发车状态 + **解锁急停**；`STOP` / `ESTOP` 同样会带出迷宫 |
| `LESSON_Blind8()` 加退出检查、`main()` 等待循环改为可重复 | 让"发车 → 收车 → 再发车"成立，不必断电 |

退出迷宫前会把停车动作做干净（速度目标归零、回到自由模式、清运动任务、
`mazeState.mouseTask` 复位到 `WAIT`），不让半途的动作残留到下一轮。

**比赛固件（[5]）不受影响（已核）**：新增代码全部在 `#ifdef MOUSE_DATA_LOG` 之下，
`[5]` 档 ROM **90872 B，与改动前逐字节同尺寸**，且 `fromelf` 符号表里
`LINK_MazeFinished` / `LINK_MazeAbortRequested` / `linkMazeAbort` **一个都不存在**。

### 接口变更

- **命令 22 → 23**：新增 `maze.stop`（固件关键字 `MAZE STOP`，**仅 `MOUSE_DATA_LOG` 档**；
  其余档回 `ERR_VALUE`，与"从未有过这条命令"完全等价）。
- `maze.run` 的失败语义明确化：已在跑时回 `ERR_BUSY`；**急停锁存中回 `ERR_ESTOP`**
  （原先会默默接受，然后被 ISR 每帧清零速度，现场表现为"发了车却不动"）。
- `host.api.maze` 新增 `MazeAPI.stop()`；CLI 新增 `maze stop` 动作（⚠️ 会停车）。
- 固件 `LINK_FW_VER` 210 → **211**（**线格式未变**，故 `LINK_PROTO_VER` 仍为 3；
  只有命令集扩充，用 FW 号区分新旧 hex）。
- **后续迭代：`LINK_FW_VER` → 212**。真机实测发现“繁忙”的**真正根因**比上面更靠前：
  `main()` 的 [4] 分支在**发车等待循环之前**调用 `STATE_LoadMapAndSelectSpeed()` 与
  `STATE_DetermineGoaheadNum()`，两者各有一个 `while(1)` 等“前方红外 <20mm”（比赛时电脑鼠
  鼻尖顶墙才算就位）。远程发车场景下前方没有近物 ⇒ **主循环永久卡死** ⇒ `MAZE RUN` 在
  链路层被接受（`linkMazeReq=1`）却**永不被消费** ⇒ 之后每次 `MAZE RUN` 都 `ERR_BUSY`、
  且 `STOP` 救不回。另有第三个死锁：无有效 Flash 地图时起点格是 `MAZE_Init()` 的默认值
  `0x11`，`START` 的两个方向位（`0x08`/`0x02`）都不成立，`mouseTask` 永远停在 `START`，
  且 `stackIdx` 停在 0（`while (--stackIdx)` 会下溢越界）。
  修复：三处，全部 `#ifdef MOUSE_DATA_LOG` 门控（`[5]` ROM 90872 B 逐次重编不变）。
- `API_VERSION` 2.1.0 → **2.1.1**。

### 验证

- 九套件 **516 项全绿**（`contract_test` 207 → 210；`maze_smoke` 34 → 42）。
  `maze_smoke` 新增的 8 项专门钉死这个缺陷，含"**收车后能再次发车**"与
  "**急停解锁后能再次发车**"两条 —— 它们在修复前必然失败。
- 五档 **0 Error**；`[5]` ROM 与改动前一致。
- 真机验收脚本：`host/maze_launch_probe.py --launch --cycle`。

---

## [2.1.0] — 2026-10-05

### 破坏性变更：遥测改上位机拉取（真机证伪"自时钟"后的修正）

设计依据：`docs/传输模式重构设计方案.md` §6.1（事故与整改）、`docs/传输模式接口设计.md` §3.5.1（拉取器）。

**本版是 v2.0 上真机后被推翻、再改正的一次修正。** v2.0 的"固件自时钟、无 Hz 上限持续流"
在真机上把蓝牙透传模块的内部缓冲灌死，且**不会自行恢复**。**协议的主线原则不变**（单流、请求-响应、
上位机能算的不传），**改的只有"谁控制发送节奏"这一条**。

#### 真机实测教训（本轮最重要的技术结论）

| 项 | 实测值 |
|---|---|
| 这条链路的**端到端持续吞吐** | **≈100~200 B/s** |
| 单条命令应答往返（`MAP DUMP` 120 B） | **≈1.21 s**（不是早期记录的 230 ms） |
| v2.0 自时钟压上去的速率 | 53 B × ≈217 Hz ≈ **4.3 KB/s** |
| 超链路能力 | **约 20 倍** |

- **后果**：蓝牙透传模块内部缓冲被灌满后**停止转发，且不会自行恢复**（必须给模块**重新上电**）。
  表现极有辨识度：`PING`/`ARM`/`GET ALL`/`MAP DUMP` 这些**小的、一次性的突发全部正常**，
  但一经 `STREAM ON` 开始持续流就**立刻 0 字节**，之后**连 `PING` 都不应答**，**只有断电重上才恢复**。
- **根因**：固件把 TX 环当背压信号，但**蓝牙模块的内部缓冲对 MCU 完全不可见** ——
  在 MCU 一侧"背压会收敛"的假设，跨过蓝牙透传就不成立。
- **关键证据（历史）**：**v1.5.5 的 `link.c` 里本来就写着这条约束**（"其内部缓冲一旦被灌满就会停止转发，
  且不会自行恢复（需要模块重新上电）"），并因此把常驻速率**刻意压到 152 B/s**；
  **v2.0 重写时把这条注释连同分频一起删了** —— 这是本轮事故的直接原因。
- **排查中已排除**（都不要再去查）：`link_AckU` 坏（`ARM` 同函数、正常）、TX 环/DMA 坏
  （`MAP DUMP` 120 B 突发正常）、MCU 卡死（IWDG 已启用，卡死应 2 s 内复位，但 20 s 不恢复）、
  端口掉线（`list_ports()` 每 0.25 s 采样 15 s，COM7 始终在）。

#### 拉取器：停等，而非滑动窗口（真机修正）

最初 v2.1 把上位机拉取器实现成"**滑动窗口 + AIMD**"（窗口 1..16 自适应）。
**真机实测证明这个设计是错的**，已改为**严格停等**（在途恒 1）。

- **根因**：固件每 1 ms tick 只消费一行命令 —— `HARDWARE/link.c` 的 `USART_RX_STA & 0x80` 分支，
  bit7 未清时 `usart.c` 会**直接丢弃**后续字节 ⇒ **同一时刻只能有 1 条命令在飞**。
- **背靠背回收率（真机，COM7）**：发 1 条 **100%**；**发 2 条 50%**；3 条 33%；4 条 25%；8 条 12%；
  **间隔 300 ms 逐条发 100%**。→ "**发 2 条就丢一半**"是这套设计的直接死因。
- **固定窗口扫描**（每档 6 s）：窗口 1 已到 **6.50 Hz**（RTT 153 ms），窗口 2 为 6.49，窗口 4/6 为 7.15 ——
  加大窗口只多约 10%，却开始成串丢失。
- **停等版真机复测**：12 秒 53 帧 = **4.41 Hz**（≈234 B/s 载荷），**在途恒 1、丢 0、`bad_crc` 0**；单帧往返 157~234 ms。
- **结论**：停等**不是软件限速**，而是协议层物理约束（可达帧率 = 1/RTT ≈ **4.4~6.5 Hz**），
  且**比堆在途请求更快**（零浪费、零丢失）。`PullRequester` 不再有
  `MIN_WINDOW` / `MAX_WINDOW` / `FAST_START_WINDOW` / `ADAPT_INTERVAL_S` 与 `_adapt()`；
  `snapshot()` 去掉 `window`、新增 `mode: "stop-and-wait"`；`tune()` 只剩 `request_timeout`。

#### 破坏性变更

- **`STREAM ON` / `STREAM OFF` 删除**，**新增 `TELEM`**：`PC -> TELEM`，设备回
  **一帧统一帧（53 B）+ `ACK <seq> OK <upMs>`**。固件**删除**开流状态与自时钟组帧，
  **收到一条 `TELEM` 才发一帧**（`link_SendUnified()`）。
- **命令 23 → 22 条**（删 2 加 1）。`registry.COMMANDS` 的 `stream.on` / `stream.off` 消失、`telem` 出现。
- 测试：新增 `pull_smoke.py`（23 项，专测拉取链路与「停流后彻底不再收帧」）；
  九套件 **505 项全绿**（`contract_test` 因 `TELEM` 用例数为 207）。
- **时间锚来源变化**：由 `STREAM ON` 应答"开流时给一次" → **每次 `TELEM` 应答都带完整 u32 时基**
  （**每次拉取都刷新锚**，失锁能立刻自愈）。帧内仍只带 `ms8` 低 8 位。
- **固件版本号**：`LINK_PROTO_VER` **2 → 3**、`LINK_FW_VER` **200 → 210**（`PING` 的 `PONG <proto> <fw> ...` 相应变化）。
- **`mc.stream` 语义变化**：不再是"向固件开关流"，而是"**启停本机拉取器**"；
  `stream.off()` **立即丢弃所有在途请求**（这是让蓝牙模块缓冲排空的唯一手段）。

#### 新增

- **停等拉取器** `host/host/api/stream.py` 的 `PullRequester`：**任何时刻在途 ≤ 1**，
  收到应答才发下一条（`_refill()` 仅在 `self._out` 为空时补发）；单请求超时 3 s；
  **没有任何软件 Hz 上限** —— 可达帧率 = 1/RTT，由链路往返决定。
  （真机实测后由"滑动窗口 + AIMD"改为此设计，见上节。）
- **`pull_smoke.py` 测试套件（23 项）**：`TELEM` 静态断言 / 连接即拉取并等到首帧 / 锚由 `TELEM` 应答建立 /
  持续拉取 3 s 帧数递增且**模式为 stop-and-wait、在途恒不超过 1 条** / 拉取期间命令通道可用 /
  **停流后彻底不再收帧** / 再开流恢复 / 断开自动停拉取 / 反例（`auto_stream=False` 时一帧都不来）。
- **自愈性质（本版的设计收益，真机已验证）**：PC 停止请求 → 固件不再发送 → 蓝牙缓冲**自然排空** →
  **无需断电即可恢复**（真机：停发 10 s 后帧计数不再增长，再发 `PING` **立刻应答**）。

#### 修正

- **`CommandClient.TIMEOUT_S` 由 0.50 → 2.5 s**：真机往返实测 ≈1.21 s，取约两倍余量。
  历史上这个值取过 0.20 s / 0.50 s，都**低于**现在的实测往返，会让"设备其实马上要回、主机已经重发"
  甚至直接报 TIMEOUT。（`TELEM` 与 `CAL` 等请求-响应/整块命令由 `CommandSpec.timeout` / `no_retry` 单独指定。）

#### 验证结果

- 五档固件重编 **0 Error**；**各档 ROM 比 v2.0 略小**（省下自时钟那套代码，见 `release/归档说明.md` §2.3）。
- 上位机：`pull_smoke.py` 全绿；既有套件按"拉取模式"对齐（锚机制改走 `TELEM` 应答）。
- **真机验证（停等拉取）**：12 s 得 **53 帧 = 4.41 Hz**、丢 0、`bad_crc` 0、**在途恒 1**；
  停发 10 s 后发 `PING` **立刻应答（无需断电即自愈）**。
  **仅发了非运动命令 `PING`/`TELEM`；`MOVE`/`ARM`/迷宫等运动相关仍待验。**

#### 明确不做

- 不改帧格式（统一帧仍是 53 B / `TYPE=0x0A`）。
- 不恢复 `STREAM ON/OFF`（"固件持续发流"这件事本身已被证明不安全）。
- 波特率提升仍不动（115200-8-N-1）。⚠️ **原判断「提高波特率会自动反映为更高帧率」已被真机证伪**：
  RTT 实测 ≈224 ms，而一帧 53 B @115200 在串口上只占 4.6 ms —— **瓶颈是蓝牙透传模块的转发延迟**，
  与串口速率无关。要真正提速得**换掉蓝牙透传**（或改有线），不是提波特率。


#### 工作区精简（2026-10-05，非协议变更）

v2.1 落地后做了一次工作区瘦身（78 MB → 73 MB），并留下可重复执行的清理工具：

- **新增 `cleanup_workspace.py`**（仓库根）：默认**预览**，`--apply` 才执行；
  清理 `host/sessions/`（保留最新 N 个，供 `monitor_smoke` 的 M4 回放用例）、
  `__pycache__/`、遗留 `*.log`、历史上的一次性调试脚本。
  **刻意保留** Keil 中间产物（删了要全量重编）、上游固件副本、`release/*.zip`。
- **`build_all_modes.py` 不再往仓库根写日志**：改为写系统临时目录
  （先前每跑一次五档就留下 5 个 `build_*.log`，反复跑会越积越多）。
- **删除 13 个过期/一次性脚本**：前提被真机证伪（`_stability.py`）、已被别的套件覆盖
  （`link_downlink_test.py`）、能力已内化（`diag_port.py` / `connectfail_test.py`）、
  以及 8 个一次性排查脚本（`real_probe` / `real_timeline` / `diff_link` / `real_link_drop` /
  `real_window_sweep` / `real_tick_latency` / `real_latency_breakdown` / `real_pull`，
  结论均已写进文档）。**保留**有复跑价值的 `real_v21` / `real_burst` / `real_discriminate` /
  `real_throughput` / `real_lesson_sampling`。
- **删除 2 张过期 GUI 截图**（`ui_dump.png` / `ui_status.png`：描述已删除的 1kHz DUMP 功能，
  且 `uismoke.py` 早已不再产出它们）。
- **修掉一批「文档说是这样、代码其实是那样」的不一致**（其中两条曾是**假绿**）：
  - `host/ui/main_window.py` 的「开流/停流」按钮**在发已删除的 `STREAM ON/OFF`**
    （会收到 `ERR_CMD`）→ 改为按停等纪律的按需拉取；
  - `CommandClient.stream_on()/stream_off()`（死方法）→ 换为 `telem()`；
  - `selftest.py --e2e` 里那条 `stream_on()` 断言**一直静默失败**（该分支只在 `--e2e` 下跑，
    而默认 `selftest.py` 的 45 项不含它）→ 改走 `TELEM` 并真正验证锚与连续拉取；
  - `host/__init__.py` 的 `__version__` 还停在 **1.1.0**（GUI 标题会显示错版本）、
    `PROTO_VER` 停在 1 → 改为 `2.1.0` / `3`。

---

## [2.0.0] — 2026-10-05

### 破坏性变更：传输模式重构（单流 + 自时钟 + 请求-响应）

设计依据：`docs/传输模式重构设计方案.md`（协议机制）、`docs/传输模式接口设计.md`（接口与字段迁移）。

**三条原则**：① 上行只有"持续数据流 + 异常事件"；② 下行一律请求-响应；③ 上位机能算的不传。

#### 新增

- **统一遥测帧** `LINK_TYPE_UNIFIED = 0x0A`（45 B 载荷，扁平展开 39 个通道）。
  一份帧就是全量快照：测量量 35 B + 状态机位打包 3 B + 坐标/决策 7 B。
  `codec.decode_unified()` / `codec.pack_unified()`（互为对偶，`contract_test [S3]` 做全字段往返断言）。
- **自时钟发送**：固件每 1 ms 检查 TX 环与 DMA 是否空闲，够就组"当前最新值"一帧。
  **无 Hz 上限**，帧率由物理链路决定（53 B ÷ 11520 B/s ≈ 217 Hz）。
- **时间锚**：`STREAM ON` 的应答携带完整 u32 时基（`ACK <seq> OK <upMs>`）；
  每帧只带 `ms8`（低 8 位），主机用 `timesync.resolve_ms8()` 同余还原。
- **上行事件序号**：`STOPPED <reason> #n` / `ESTOP TRIP #n` 带序号，
  与遥测帧**共用**同一个 1 字节计数器。不做 ACK、不重发 —— 序号保证的是"丢失可检测"。
  主机侧 `TelemetryStore.event_health()` 给出 `up_gaps/up_lost/ev_gaps/ev_lost`。
- **IWDG 看门狗**（LSI/64 = 625 Hz，RLR=1249 ≈ 2.0 s，喂狗在 `LINK_Tick()`）+
  `PING` 上报上次复位原因：`PONG <proto> <fw> <mazetype> <txbuf> <rst>`，`rst ∈ {POR,SW,IWDG,PIN}`。
  主机侧 `mc.reset_cause`。
- **标定改请求-响应**：`CAL <item> {START|SAMPLE|ABORT}` 采完才回 `ACK`
  （ACK 即"这一步完成，请摆下一个位置"）。角度标定结果放在 `ACK <seq> OK <sumAcc> <sumEnc> <deg>`。
  `CAL` 归入 `no_retry`、主机超时 10 s。
- **`ARM` / `MAZE RUN` 的应答带值**：`ACK <seq> OK <armMs>`（取代单发的 `ARMED` 行）。
- **`mc.lab.capture()`**：按下最新快照采样若干秒（取代"停车后整块回传"）。
- **`mc.lesson.run(capture=...)`**：实验曲线在**实验期间本地捕获**（实验结束不再有回传往返）。
- **CLI/帧分派保序**：`FrameStreamParser.feed()` 返回**有序列表**（`Frame` 与文本行混排），
  因为帧与事件共用序号 —— **交错顺序本身就是协议**。需要拆两类时用 `codec.split_items()`。

#### 删除

- **双流**：`SCOPE`（固件侧 1 kHz 本地录制）+ `DUMP`（整块回传）**整套删除**。
  其唯一依据"可持续吞吐只有 152 B/s"**已在真机证伪**（`SCOPE DUMP` 实测 1.38 KB/s 未堵死），
  且单流全速上行后数据本就在主机内存里。**1 kHz 红外采样本身保留**（`doWallDetect()`，比赛算法依赖）。
- **命令 41 → 18 条**：删 `SNAP`（全部域）、`SCOPE ×3`、`RATE`、`VER`（并入 `PING`）、
  `MAZE EV`、`MAZE RST`。现存 18 个关键字对应 23 个 API 名（`stop`/`estop.clear` 同字面量）。
- **帧 9 种 → 2 种**：删 `fast`/`mid`/`slow`/`time`/`maze`/`dump_hdr`/`dump_blk`/`dump_end`。
- **帧类型常量**：`TYPE_FAST` `TYPE_MID` `TYPE_SLOW` `TYPE_TIME` `TYPE_MAZE` `TYPE_DUMP_*` 及其解码器。
- **事件行**：`MOVEDONE ...`（只剩 `STOPPED <reason>`）、`ARMED <ms>`、`CALSTEP`/`CALDONE`/`CALERR`、
  `MAZE EV ...`（整条事件流；迷宫状态量已在统一帧里，边沿由主机差分取得）。
- **子 API**：`mc.snap`（`api/snap.py`）、`mc.scope`（`api/scope.py`）整个模块删除。
- **CLI 子命令**：`snap`、`scope`；`maze` 的动作缩减为 `map|state|summary`。
- **会话落盘**：`dump_1khz_*.csv` 不再产生（改由 `stream_unified.csv` 承担）。
- **`Link.self_check()` 的"建议降级速率"**：自时钟发送下主机无从也不能下发速率。

#### 修正

- 丢帧统计口径：v1.5.5 按"相邻**帧**序号差"判定，在 v2.0 会把它正常的"帧/事件交错"
  误判成丢帧（实测一次 `STOPPED` 报 3 个缺口、256 条丢失）。现改为**按相邻上行消息
  （帧或事件）**判定，且唯一真源在 `TelemetryStore`，`Link.counts` 只做抄写。
- 课程参数（id 8–21）**读侧不对称**：固件 `GET ALL` 与 `GET <name>` 只查生产表
  （`linkParam[LINK_P_NUM]`，24 项），课程 14 项一律回 `ERR_NAME`；写侧（`SET`/`SETQ`）是通的。
  模拟器现已**原样复刻**该不对称（此前回全部 38 项，把真机上的 `ERR_NAME` 掩盖成了"随机故障"），
  并由 `contract_test [D2]` 分别断言两段行为。
- `motionCtrl.task` 与"正在跑 `MOVE`"不同源：`MOVE FWD/ROT/RUN` 启动时会把 `motionCtrl.task`
  置 `NULL`，故"只看 task"的 busy 检查查不出正在运动 —— 曾导致第二条 `MOVE RUN`
  覆盖 `linkMove.elapsed/limit`、**把时间护栏重置**。现统一走 `link_MotionBusy()`
  （task + `linkMove.state` + `linkSrv.state` + `linkCal.active`）；模拟器同步对齐，
  `contract_test [D4]` 用**墙钟**验证护栏未被重置。
- 档位门控改用具名派生宏：`MOUSE_TEST_MODE`（[1][2][3]）与
  `MOUSE_ESTOP_RECOVERABLE`（[1][2][3]+[4]）。裸 `#ifdef MOUSE_TEST_MODE` 漏一处不会有编译错误，
  曾在 [4] 档造成"急停锁死只能断电"+"链路活着但数据冻结"。`contract_test [S5]` 静态钉死。

#### 空间与验证

- Flash 分区重排以容纳 [5] 比赛固件：程序区 0–100（101 KB）、录制区 102–114、
  标定页 **115**、课程页 **116**、迷宫页 118–127。
  顺带修掉一处真冲突：标定页原在 99/100，正落在 `FLASH_EraseRunData` 的擦除范围
  （89..101）内 —— 一次运行数据擦除就会抹掉生产标定与课程参数。`contract_test [S5]` 有 4 条断言。
- 新增 `contract_test [S6]`：主机侧协议常量（`U_*` 字段偏移、`FLAG_*` 位定义、`TYPE_*`、`UNIFIED_LEN`）
  逐项比对固件 `link.h` 的 `LINK_U_*` / `LINK_FLAG_*` —— 两处实现同一份协议，任一侧单独改动
  只会在真机上表现为"字段整体错位/标志位恒假"，必须有静态防线。

#### 明确不做

- 兼容层（双协议共存、版本协商）：**不做**，直接替换。
- `MAZE EV <mask>`：不保留（无事件行可配）。
- 波特率提升：本次不动（115200-8-N-1），自时钟发送使日后提升**自动适配**。

---

## [1.5.5] — 2026-10-04

### 新增：迷宫状态机回传（实验 8 无记忆走迷宫）

**诉求**：为实验 8 无记忆走迷宫加"状态机回传"—— 把**代码里真实存在的状态**（弯道/岔路口/死路/终点…）
实时报给上位机。原先慢帧只给 `mouseTask` + 坐标，看不出"它刚才识别到了什么"。

**设计原则：只读观测，不埋点。** 观测层整个放在 `HARDWARE/link.c` 里，
**不在 maze/state/lesson/motion 里插一行钩子**，只读已公开的 `mazeState` / `wallSns` / `motionCtrl.task`
（它们本来就驱动决策，所以"回传的状态"与"代码里的状态"同源），观测点在 SysTick 1 ms 上下文
→ 迷宫的 `while(1)` 挡不住它。

**三个通道**

| 通道 | 内容 |
|---|---|
| **事件流** `MAZE EV <EVENT> ...` | **迁移驱动**，只在变化时发：`BEGIN`/`EXIT`/`TASK <from> <to>`/`ROAD <KIND> <code>`/`JUNC <stack>`/`TURN <MOTION> <code>`/`GOAL <x> <y>`/`CELL`(默认关) |
| **1 Hz 状态帧** `LINK_TYPE_MAZE (0x09)` | 16 B 载荷：task/road/opens/motion/pos/dir/stack/flags/evMask/evCount/juncCnt/turnCnt/deadCnt。**仅在迷宫态发送**（空闲零增量），且与其它周期帧一致受 `STREAM ON` 约束 |
| **`SNAP MAZE`** | 阻塞点查；命令在中断里处理，**迷宫跑着也能问**。额外给出 `evDrop`(丢弃计数) 与 `cellCnt`(格数) |

**路口类型 8 种**（由 `wallSns` 前/左/右开口相对朝向分类，与 `LESSON_Blind8` 数 `roadStat` 同源）：
`THRU` 直道 / `CORNER_L`·`CORNER_R` 弯道 / `TEE_FL`·`TEE_FR`·`TEE_LR` 岔路口 / `CROSS` 十字 / `DEAD` 死路。
**终点判据**与 `state.c` 的 `STATE_MazeSearch()` "到达终点区"完全一致（只读复制，不改 state.c）。

**新命令**：`MAZE EV [mask|OFF|DEF|ALL]`（默认 31 = TASK\|ROAD\|JUNC\|TURN\|GOAL，`CELL` 默认关）、`MAZE RST`。
**新帧类型**：`0x09`（FRAMES 8 → **9**；命令数 +2）。
**上位机**：`mc.maze` 新增 `state()/history()/snap()/events()/watch()/on_event()/summary()/explain()/set_events()/reset()/monitor_view()`；
`Event.MAZE`；`monitor` 多一节"迷宫状态机"面板；`cli maze` 扩为 8 个动作。

**带宽**：事件实测 ≈5~6 条/s（约 150 B/s）且只在跑迷宫时才有；状态帧 24 B/s 同样只在迷宫态；
空闲零增量。发事件前要求 TX 环空闲 ≥64 B，不足则**丢弃并计数**（`evDrop`）并在状态帧置
`EVQ` 标志 —— 宁可少发，也不让长文本在 dump 期间回绕覆盖未发出的数据。

**顺带修掉的三个 bug（都做成了回归）**

1. **BEGIN 被无限重发**（模拟器侧，实测 6 秒 299 条）：用 `maze_flags` 高位记"上一拍在迷宫态"，
   而 `reset()` 会清 `flags` → 边沿每拍都成立。改用独立字段（与固件同构）。
2. **进入迷宫的 `TASK` 迁移被 reset 吞掉**：`reset()` 把 `task_prev` 设成当前值，正好吃掉 `WAIT → SEARCH`。
   改为进入迷宫时显式退回 `WAIT`（手动 `MAZE RST` 不走这条路径，故不误报）。
3. **`push_time` 的 `self._notify("time", rec)` 被挤进新函数**（上位机 `ring.py`）：
   插 `push_maze` 时锚点选在 `push_time` 最后一行之前，那行被留在了新函数末尾 →
   **TIME 帧不再通知**（时间同步样本恒 0，`timesync_smoke` 掉 5 项），而迷宫帧冒名通知成 `"time"`。
   **编译与 import 都不报错**，只有跑起来才现形。修法：那行放回 `push_time`，新函数自己 `_notify("maze", rec)`。

**新增回归**
- `host/maze_smoke.py`（**58 项**）：离线（路口判据 8 组合全覆盖、事件解析容错、16 B 帧解码含 u16 字段）+
  端到端（发车→事件流→1Hz 帧→SNAP→summary/explain）+ 不变量（`BEGIN` 只 1 条、`ROAD` 相邻不重复、
  掩码 OFF 真的静默、空闲无状态帧）。
- `contract_test [S5]` 新增两条静态断言：**源码"注释提前闭合"扫描**（跨行注释内的 `*/` 之后不得再有内容 ——
  这个坑本项目踩过两次：`main.h` 869 error、`link.c` 的 "struct has no field"）；
  **每个帧 sink 只 notify 自己的 stream**（防上面第 3 类"插错位置"）。
- 模拟器保真度修正：上电 `mouse_task` 由 2 改为 **0 (WAIT)**，与固件一致（原来上电就"在迷宫里"，
  会让"进入迷宫态"的边沿永不出现）。

**结果**：九套件全绿 **667 项**（原 600）：selftest 59 / contract **239** / **maze 58** / snap 117 /
motor_lab 44 / lesson 38 / timesync 46 / facade 38 / monitor 28。
五档编译（**[5] 比赛固件 ROM 96728 B，余 6696 B**）：
[1] 33576 / [2] 34128 / [3] 38640 / [4] 81704 / [5] 96728。

**真机未验证**（6 项待办见 `docs/迷宫状态机回传.md` §9）：重点是"路口判定与现场一致"与"`EVQ` 为 0"。

---

## [1.5.4] — 2026-10-04

### 修复（Flash 布局，**未改一行程序逻辑**）：[5] 比赛固件空间不足 → 运行数据区上移

[1.5.3] 核实了 **[5] 比赛固件链接失败**（`L6406E/L6407E`）。本轮定位到具体原因并修复。

**诊断（逐对象实测，方案文档见 `docs/比赛固件空间不足诊断与瘦身方案.md`）**

- 这是 **ROM** 问题，**与 RAM 缓冲区无关**。对照实验：把 `saveNum` 1200→900（省 2400 B RAM）后**仍失败**；
  把程序区放宽到 `0x20000` 后即 **0 error**。
- 全工程最大单一 ROM 消耗是 **`MOTION/speedtable.c`：43 张 `const short` 查表 = 20266 B（19.8 KB），
  而该文件代码只有 14 B**，占程序区 22.5%。
- 其次 `link.o` = 14298 B（PC 链路模块）。
- **C 库 printf/浮点全家桶只有 ≈1.9 KB** —— 过去容易怀疑的对象，实际没有杠杆。
- 缺口 = `94112 − 90112` = **4000 B**。

**采取的做法**：`FLASH_EraseRunData()` 按 `saveNum/100+1 = **13** 页` 擦除，而运行数据区
**预留了 29 页（page 89..117）** —— **16 页是虚设的**。把这 16 页还给程序区，
同时把链路模块的标定页从"落在擦除范围内"的位置挪出来：

| 页 | 原 | 现 |
|---|---|---|
| 程序区 | 0..87（88 KB，IROM1 `0x16000`） | **0..100（101 KB，IROM1 `0x19400`）** |
| 运行数据计数 | 88 | **101** `0x08019400` |
| 运行数据录制区 | 89..117（预留 29 页） | **102..114** `0x08019800`（擦除 13 页） |
| 生产标定页 | 99 ← **在擦除范围内** | **115** `0x0801CC00` |
| 课程参数页 | 100 ← **在擦除范围内** | **116** `0x0801D000` |
| 备用 | — | 117 |
| 迷宫/路径/红外/陀螺 | 118..127 | **原样不动** |

**改动清单（5 处，全是地址常量 + 1 个项目设置）**

| 文件 | 改动 |
|---|---|
| `HARDWARE/flash.h` | `FLASH_ADDR_DATA_NUM` 0x08016000→0x08019400；`FLASH_ADDR_DATA` 0x08016400→0x08019800 |
| `HARDWARE/link.h` | `FLASH_ADDR_SYSCALIB` 0x08018C00→0x0801CC00；`FLASH_ADDR_LESSONPARAM` 0x08019000→0x0801D000 |
| `USER/Mouse.uvprojx` | IROM1 `<Size>` 0x16000 → 0x19400 |

**结果（五档一次跑完，全部 0 Error）**

| 档位 | Code | RO-data | RW | **ROM** | RAM | 余量 |
|---|---|---|---|---|---|---|
| [1] 安全测试 | 28384 | 456 | 2136 | 30976 | 16744 | 72448 |
| [2] 电机远程 | 28936 | 456 | 2136 | 31528 | 16744 | 71896 |
| [3] 课程调参 | 33464 | 456 | 2136 | 36056 | 17912 | 67368 |
| [4] 数据收集 | 55740 | 21204 | 2160 | 79104 | 17648 | 24320 |
| **[5] 比赛固件** | 70652 | 21300 | 2160 | **94112** | 17648 | **9312** ✅ |

**顺带修掉一个真实缺陷**：`FLASH_EraseRunData()` 被 `calibrate.c:71` / `menu.c:136` / `state.c:274`
三处调用，擦除 **page 89..101**；而链路模块的标定页/课程参数页原先取 **page 99 / 100**，
**正落在该范围内** —— 触发一次擦除就会把生产标定与课程参数一起抹掉。
上移后录制区为 102..114，与 115/116 不再相交。

**不受影响 / 须知**

- 迷宫墙、起终点、90° 路径、红外标定、常规路径、陀螺零偏（page 118..127）地址**未变** → **不需要重新标定**。
- **录制容量未削减**（仍 1200 组 / 13 页擦除）—— 这不是"把数据改短"，而是**把虚设的预留页还给程序区**。
- 唯一须知：链路模块自己的标定页搬了家，板上旧值读不到 → 回落源码默认值，重做 `SAVE` 即恢复
  （与既有 D1 决策一致）。

**新增回归**：`contract_test [S5]` 增加 4 条页布局断言 ——
程序区上界必须严格等于 `FLASH_ADDR_DATA_NUM`；运行数据擦除范围必须为 13 页；
`FLASH_ADDR_SYSCALIB` / `FLASH_ADDR_LESSONPARAM` 必须**位于数据区、1 KB 对齐、不越过迷宫页、
且不在擦除范围内**。日后改 `saveNum` 或这些页地址会立刻报红。

**数字勘误**：先前多处把 [4] 的 ROM 写成 **77104 B**，实为 **79104 B**（55740+21204+2160）。
已在本文档、`docs/安全测试模式与实时监视.md`、`docs/比赛固件空间不足诊断与瘦身方案.md` 与项目记忆中更正。

**未采用的备选**（保留在方案文档中备用）
- **A 查表元素收窄到 1 字节**（省 8488 B）：需动 `speedtable.c/h` 与 `motion.c`
  （含 `TurnParam_t.length = sizeof(表)` 而使用处 `tick < length/2-2` 的 `sizeof` 陷阱）。
- **B 仿射去冗余**（省 7654 B）：16 张表满足精确仿射关系（`gyroR* = −gyroL*`、`vinner* = −vouter* + c`），
  但需给 `isr.c` 的实时解引用加 negate/offset。
- **C `link.c` 按档裁剪**（4.6~9 KB）：[5] 会失去 `MOVE`/`SNAP`/`SCOPE`。
- **E 提升优化等级**（10~17 KB）：需全量真机复测。
- **明确排除**：RLE/分段编码查表（实测仅 4145 个变化点、平均游程 2.4，分段描述≈16.6 KB，只省 3.7 KB）；
  把查表搬进 RAM（需 20266 B，[5] RAM 只余 2863 B）。
- **用户提出的"只保留课程 1~8 实验、不要最终比赛运行代码"** 未采用（会让 [5] 失去比赛能力）。

---

## [1.5.3] — 2026-10-04

### 背景

上一条修复（运动互斥，见 [1.5.2]）烧录后复测。互斥四项（`MOVE RUN`/`MOVE FWD`/`MOVE ROT`/`SAVE`）
全部按预期回 `ERR_BUSY`，护栏时长不再被重置（`elapsed=2500`，结束于 2.796 s = 0.30 收到 + 2.50 运行）。
**但顺带测急停时发现 [4] 数据收集档的急停语义整个是错的** —— 而这一点档位无关的模拟器盖不住。

### 修复 1（固件，[4] 档）：急停锁存无法用 `STOP` 解除

**现象（真机 COM7）**：

```
起始一次 move.run  ->  ERR_ESTOP          ← 上一轮的锁存跨连接保留
...运动中发 ESTOP  ->  OK
锁存中 move.run    ->  ERR_ESTOP
estop.clear(STOP)  ->  OK                  ← 回 OK!
解锁后 move.run    ->  ERR_ESTOP           ← 仍然被拒
SNAP flags = 0xC7  (FREEMODE|SLEEP|EMERG|ESTOP_LOCK|RUNLOG)
```

**根因**：`MOUSE_TEST_MODE` 只由 [1][2][3] 派生，**[4] `MOUSE_DATA_LOG` 刻意不派生**
（它要跑 `doTask*`/`LESSON_ISR` 自走，见 `USER/main.h`）。而"STOP 兼作解急停"整段被包在
`#ifdef MOUSE_TEST_MODE` 里（`link.c` 的 STOP 分支）→ **[4] 继承比赛固件的"急停一次即锁死、只认断电"语义**。
对数据收集档这意味着**一次急停就废掉一整场采集**。

### 修复 2（固件，[4] 档）：急停后进入 sleepmode → **链路活着，数据冻结**

同一处门控还漏了 `isr.c` 的 `#ifndef MOUSE_TEST_MODE`：它守着"急停 200 ms 后 `sleepmode=1`"。
[4] 档因此会休眠，而 `isr.c` 的 `sleepmode` 分支**跳过全部采样与 `SAFETY_Monitor()`**
（陀螺/里程/墙检测/电池全停），偏偏 `LINK_Tick()` 被刻意放在该分支之外 —— 于是**帧照发，数据是死的**：

```
4 次 SNAP 间隔 0.9s:
  upMs  = 199430 → 200630 → 201754 → 202879   ← 时基在涨, 链路活着
  cpu   = 62 62 62 62                          ← 逐字节不变
  adcBatt = 2905 2905 2905 2905                ← 逐字节不变
  battMv  = 7015 7015 7015 7015                ← 逐字节不变
快帧 ms8 在变(帧在发) —— 这是最坏的失效模式: 现场看不出来, 采集记录全是冻结值。
```

**修法（两处同源，一并消除）**：新增**具名派生宏** `MOUSE_ESTOP_RECOVERABLE`
（`USER/main.h`）：`MOUSE_TEST_MODE || MOUSE_DATA_LOG`。
`link.c` 的 STOP 解锁块改用 `#ifdef MOUSE_ESTOP_RECOVERABLE`；
`isr.c` 的急停休眠块改用 `#ifndef MOUSE_ESTOP_RECOVERABLE`。
**比赛固件（五档全不定义）保持原语义：急停即休眠、不接受远程复位。**

> 教训：这次不是"漏了一处判断"，而是**同一语义在四处各写一遍裸 `#ifdef`** —— 漏一处不会有编译错误，
> 只在真机上表现为行为不一致。故改用**具名派生宏表达"档位共同行为"**，并加静态断言钉死（见下）。
> 同时清掉 `USER/main.h` 里重复的 [1]/[2] 两行注释。

### 修复 3（固件）：`LESSON START` 的互斥判据与其余四处不一致

`link.c` 的 `LESSON START` 只查 `linkMove.state != LINK_MOVE_NONE`，未复用
[1.5.2] 引入的统一判据 `link_MotionBusy()`（后者还含 `motionCtrl.task`/`linkSrv`/`linkCal`）。
改为 `link_MotionBusy()`，与 `SAVE`/`MOVE RUN`/`MOVE FWD`/`MOVE ROT` 完全对齐，
模拟器的 `_motion_busy()` 亦已一致。

### 修复 4（模拟器）：`SNAP MOTION` 的 `flags` 只算了一位

`mouse_sim.py` 里快帧与 `SNAP MOTION` **各算一遍 flags**，结果 SNAP 那份只算了
`MOTOR_OFF`，漏掉 `estop_lock`/`freemode`/`emerg`/`sleep`/`scoping`/`dumping`/`runlog`；
而真机 `SNAP MOTION` 回的是完整字节（实测 `0xC7`）——**两侧不一致**，
导致"急停是否锁存"这类断言在模拟器上失效。
抽成单一来源 `MouseSimState._flags()`（与固件 `link_Flags()` 位定义逐位一致），快帧与 SNAP 共用。

### 新增回归（把这次的两类缺陷钉住）

| 用例 | 覆盖 |
|---|---|
| `contract_test [S5] 固件档位门控一致性` | 静态查源码：① `main.h` 的 `MOUSE_ESTOP_RECOVERABLE` 派生式；② `link.c` 顶层 STOP 块与 `isr.c` 急停块**必须走具名宏、不得残留裸 `MOUSE_TEST_MODE`**；③ 残留裸门控**清点基线**（`link.c` 0 处 / `isr.c` 3 处），将来新加一处立刻报红；④ **程序区上界必须等于 `FLASH_ADDR_DATA_NUM`**（越界会静默覆盖标定/迷宫数据） |
| `contract_test [D5] 急停可恢复` | 端到端：`ESTOP` → 锁存拒动 + `flags` 置 `ESTOP_LOCK(0x40)` → `STOP` 解锁 → `flags` 清零 → 运动恢复 |

> `[S5]` 存在的理由值得记一笔：[1.5.2] 的运动互斥、本条的档位门控，**都是"固件与模拟器一起错"**，
> 所以整套 500+ 项跑全绿也盖不住。**能查源码的地方不要只靠模拟器。**

### 核实（**既有问题，非本次引入**）：[5] 比赛固件超出程序区，无法链接

编译五档时发现 **[5]（四宏全注释）链接失败**：

```
L6406E: No space in execution regions with .ANY selector ...
L6407E: Sections of aggregate size 0x6c0 bytes could not fit into .ANY selector(s).
".\Object\Mouse.axf" - 69 Error(s), 114 Warning(s).
```

| 项 | 值 |
|---|---|
| 芯片 | STM32F103RB：Flash 128 KB / RAM 20 KB |
| **程序区上限** | uvprojx IROM1 = `0x16000` = **90112 B**（88 KB） |
| 该上限的来由 | `FLASH_ADDR_DATA_NUM = 0x08016000`（第 88 页）—— 上面 40 KB 是比赛用持久数据区（迷宫/路径/`IRDATA`/陀螺零点），**程序区不得越界，这是设计约束** |
| [5] 实际占用 | Code 70652 + RO-data 21300 + RW-data 2160 = **94112 B** |
| [4] 实际占用 | Code 55740 + RO-data 21204 + RW-data 2160 = **79104 B**（余 11 KB） |

**超出约 3.9 KB**。把程序区临时放宽到 `0x20000` 后 [5] 即 0 error，确认是 ROM 容量问题
（把 `saveNum` 从 1200 降到 900、省 2400 B RAM 无效，可排除 RAM）。

判为既有问题：把 TX 环回退到 256 B（即撤回本会话唯一的 RAM 增量）后仍失败。
主因是 `link.c`（上位机链路模块，为此新增的功能，错误清单里 `link.o` 出现 30+ 次）
叠加在原本就接近上限的比赛固件上。

**未做修改**：这需要重新划分 Flash 分区（动 `FLASH_ADDR_*` 与比赛数据布局），
属比赛关键决策，留待确认。**当前 [1]/[2]/[3]/[4] 四档均可正常编译（0 Error）。**

### 编译与测试

| 档位 | Code | RO-data | RW | ZI | Error |
|---|---|---|---|---|---|
| [4] 数据收集 | 55740 | 21204 | 2160 | 15488 | **0** |
| [3] 课程调参 | 33464 | 456 | 2136 | 15776 | **0** |
| [2] 电机远程 | 28936 | 456 | 2136 | 14608 | **0** |
| [1] 安全测试 | 28384 | 456 | 2136 | 14608 | **0** |
| [5] 比赛固件 | 70652 | 21300 | 2160 | 15488 | ❌ 链接失败（见上） |

八套件全绿 **587 项**（原 568）：selftest 59 / contract **217** / snap 117 / motor_lab 44 /
lesson 38 / timesync 46 / facade 38 / monitor 28。

---

## [1.5.2] — 2026-10-04

### 修复：`MOVE RUN/FWD/ROT` 的运动互斥漏检 —— **时间护栏可被重置**

按"测试连续的运动控制命令，怀疑重复执行指令时会存在问题"在真机上实测，
**"重复执行"本身没问题，但顺手揪出一个更严重的既有缺陷。**

#### 实测结论（真机 COM7）

| 场景 | 结果 |
|---|---|
| 同一份字节（同 seq 同 CRC）连发 3 次 | **MOVEDONE 只有 1 条** → 重放应答、**不重执行** ✅ 幂等正确 |
| 相同文本、不同 seq，间隔 1 s 各发一次 | **2 条 MOVEDONE**，`elapsed=1000ms` 各一次，`dL/dR` 两轮差 < 0.2% ✅ |
| 第 1 条 `ms=1500` **仍在跑**时发第 2 条 | ⚠️ **第 2 条返回 `OK`（应为 `ERR_BUSY`），且 MOVEDONE 出现于 1.993 s** —— 不是 1.7 s |

#### 根因

`MOVE FWD / MOVE ROT / MOVE RUN` 在启动时都会把 `motionCtrl.task` 置成
`MOTION_TASK_NULL`（它们由 `link_MoveService()` 驱动，**不走任务状态机**）。
而四处 busy 检查只查 `motionCtrl.task`，**全部漏了 `linkMove.state`**：

| 位置 | 原判据 |
|---|---|
| `SAVE` | `motionCtrl.task \|\| linkSrv.state` |
| `MOVE RUN` | `motionCtrl.task \|\| linkSrv.state \|\| linkCal.active` |
| `MOVE FWD` / `MOVE ROT` | `motionCtrl.task` |

于是第二条 MOVE 畅通无阻地**覆盖 `linkMove` 状态**：

```c
linkMove.state   = LINK_MOVE_RUN;
linkMove.elapsed = 0;          /* ← 计时器清零 */
linkMove.limit   = (u32)ms;    /* ← 护栏被改写 */
```

→ **时间护栏被重置**：连续发 `MOVE RUN` 就能把电机运行时间无限延长，
而时间护栏是 `MOVE RUN` 除急停之外**最主要的安全兜底**。
实测：首条 1500 ms，在 0.48 s 处再发一条，实际跑到 **~1.98 s**。
同理 `SAVE` 也会在运动中间去擦写 Flash，而擦写停顿会打断 1 kHz 控制环（本仓库文档自己写明了这点）。

#### 修复

固件新增统一判据 `link_MotionBusy()`（`link.c`），四处共用：

```c
static u8 link_MotionBusy(void)
{
    if (motionCtrl.task != MOTION_TASK_NULL) return 1; /* 任务状态机(自走/课程) */
    if (linkMove.state != LINK_MOVE_NONE)    return 1; /* 显式 MOVE(启动时把 task 置 NULL!) */
    if (linkSrv.state != LINK_SRV_NONE)      return 1; /* 角度扫描 */
    if (linkCal.active)                      return 1; /* 标定 */
    return 0;
}
```

统一成一个函数而不是四处各写一遍 —— **之前正是"四处各写一遍"才漏掉了 `linkMove`。**

#### 为什么整套测试盖不住

**模拟器当时也没查**（`MOVE RUN` 直接 `_halt()` 然后覆盖状态），
所以固件与模拟器"一起错"，`contract_test` 的 144+ 项全部通过。
现在模拟器同步实现了 `_motion_busy()`，并新增两条回归用例：

- `[D3] 幂等`：同字节 ×3 → 3 条 ACK / 1 条 MOVEDONE；不同 seq ×2 → 2 条 MOVEDONE
- `[D4] 运动互斥`：运动中的第 2 条 `MOVE RUN`/`MOVE ROT`/`MOVE FWD`/`SAVE` 必须回 `ERR_BUSY`，
  **且 `MOVEDONE` 的护栏时长不被重置**（断言 `... TIME 1200 ...` 仍是第 1 条的值）

`[D1]` 顺序表同步加了"每条运动命令前先 `stop`"——以前的顺序能过，
**恰恰是因为设备错误地接受了重叠命令**。

九套件 **568 项**全绿（原 553）。

---

## [1.5.1] — 2026-10-04

### 真机联调（COM7 / CH340 + 蓝牙透传）暴露的两个问题 + 修复

首次把 v1.5.0 接到真机做参数回传测试，**全部读数正常**（见下），但联调过程暴露两个
**模拟器不会复现**的问题，其中一个会让 `SCOPE DUMP` 看起来"失败"（其实 dump 跑完了）。

#### 现象与证据（原始字节流 + 命令层时刻戳）

```
+1.222s -> SCOPE STOP
+1.443s <- ACK 4 OK         ← 命令发出后 221 ms 才回；而主机超时是 200 ms
+1.649s -> SCOPE DUMP
+1.658s <- ACK 4 OK         ← STOP 的迟到应答（seq 不匹配，主机继续等）
+1.869s -> SCOPE DUMP (重发 #2)      … 连续重发到第 5 次
+2.663s <- ACK 5 OK         ← dump 把 ACK 挤后了约 1.0 秒
+0.164s <- ACK -1 ERR_CRC   ← 链路上偶发命令行 CRC 错（幂等重发已能自愈）
```

#### 修复 1（上位机）：命令超时 0.20s → **0.50s**

`CommandClient.TIMEOUT_S` 原来取 0.20 s，而**实测链路单程+应答往返可达 221 ms** ——
正好卡在阈值上，于是"设备其实马上要回，主机已经重发"，表现为每条命令重发 1~4 次
（`PING` 3 次、`SCOPE STOP` 2 次、`SCOPE DUMP` 5 次）；运气差时 5 次用尽直接报
`TIMEOUT`，**即使设备已正确执行**。改为 0.50 s（实测 RTT 的两倍余量），
最坏耗时 5×0.5 = 2.5 s 仍可接受。修复后 `PING` **attempts = 1**。

#### 修复 2（上位机）：`ScopeAPI.fetch()` 不再因 ACK 超时而放弃

`SCOPE DUMP` 一启动，固件就连发 20 块 × 490 B 的 dump 帧，而 ACK 文本与这些帧
**共用同一条 TX 环 + DMA1_Channel4** → ACK 被挤后约 1 s，超过命令层重发窗口 →
`send_cmd("scope.dump")` 报 `TIMEOUT` → 旧代码 `if not r.ok: return None`，
**明明 dump 已经跑完却返回失败**。

现在只有"设备明确拒绝"（`ERR_BUSY` 等）才提前放弃；`TIMEOUT` 一律继续等
`DUMP_DONE` —— 那才是"数据到了没有"的唯一判据。

#### 修复 3（固件，**已烧录并验证**）：`SCOPE DUMP` 先回 ACK 再启动 dump

`link_StartDump()` 与 `link_Ack()` 调换顺序：先把应答文本入环，再启动 dump 流。
原始字节流已证实生效 —— `ACK n OK` 出现在第一个 dump 帧**之前**：

```
b'PONG 1 100 7\r\nACK 1 OK\r\nACK 2 OK\r\nACK 3 OK\r\nACK 4 OK\r\n\xaaU\x0c\x00\x04...'
                                                                  ^^^^^^^^^^^^^^^^ DUMP_HDR
```

#### 修复 4（上位机）：`scope.dump` 声明为**不重发**命令

烧录后复测发现 dump 的 ACK 仍有约 1 s 延迟（dump 一启动就全速占用无线电链路，
模块缓冲区里 ACK 可能排在 dump 数据之后 —— 这是模块行为，**不可依赖**）。
但 `SCOPE DUMP` 重发毫无意义（副作用已产生），反而往全速的 dump 流里插命令。

于是给 `CommandSpec` 增加 `no_retry` 字段（仅 `scope.dump` 置真），并解决随之而来的
**到期归属**问题：不重发的命令不再由 `poll()` 按 `TIMEOUT_S`(0.5s) 判死，
而由**调用者给的 timeout** 决定；`MouseClient._wait_pending()` 超时时调
`CommandClient.abandon()` 摘除挂起项（否则 Pending 会永久残留在 `_pending` 里）。

**真机复测（烧录后）：`sent 51 / retries 0 / timeouts 0 / ok 51`，残留挂起项 0；
30 次 SNAP 问答全部成功，5 个剖面全部成功。** 修前是 `retries 10 / timeouts 5`。

### 真机读数（v1.5.0 实测，COM7 @115200）

| 项 | 实测值 |
|---|---|
| `VER` | `proto=1 fw=100 mazetype=7 **txbuf=512** scopedepth=1200`（确认新固件） |
| `GET ALL` | **24 项**全部回读，17 项新参数默认值逐项正确 |
| 参数写+回读 | `BATTLOWMV/BATTALPHA/IRKALP/IRTHRWALL/IRFRONTMODE` 写→回读一致，还原后一致（未落 Flash） |
| `SNAP` | 5 域齐备；电池 `adcBatt=2909 → 7023 mV`（7.02 V）；`irSat` 正确标出前向两路**远端超量程** |
| SNAP 轮询 | 30 次问答 30/30 成功，0 重发 0 超时 |
| 遥测流 | 2 s 内 fast 11 / mid 2 / slow 1 / time 2，**bad_crc=0, seq_lost=0**，~185 B/s |
| 剖面 A | 675~750 点（空闲，全 0） |
| 剖面 B | 749 点 · `irF1=249 irF2=249 irL=26..28 irR=34` —— 与 `SNAP` 一致（交叉验证） |
| 剖面 C | 675 点 · `adcF1=2..5 adcF2=2..4 adcL=161..163 adcR=121..123`（与 sat=2 自洽） |
| 剖面 D | 674 点 · `battMv=7010..7013 adcBatt=2898..2906`；`distF=249` = (f1+f2)/2 ✓ |
| 剖面 E | 600 点 · `battMv=7009..7013`、双路红外、任务码 0 |
| dump 吞吐 | ~8.2 s/次（含采集与命令往返），`timeouts=0` |

### 真机链路特征（本项目新的长期事实）

- **稳态命令往返 ≈230 ms**（20 次 SNAP 问答 4.6 s），`TIMEOUT_S=0.5 s` 有 2.3× 余量；
- **连接后第一条命令**可能慢到 ~750 ms（模块唤醒）；
- **链路上偶发命令行位错** → 设备回 `ACK -1 ERR_CRC`（主机不匹配任何挂起项，幂等重发自愈）；
- **dump 期间的 ACK 不可依赖**（可能被 dump 数据挤后 ~1 s）→ 一律以 `DUMP_DONE` 为判据。

九套件仍全绿 **553 项**。

---

## [1.5.0] — 2026-10-04

### 新增：红外测距与电源电平的测量回传（阻塞式 `SNAP` + `SCOPE` 录制剖面）

按"把红外测距与电源电平变成**可确认、可标定、可记录、可分析**的量"的要求实施
（设计出处：`docs/红外与电源测量回传设计方案.md`）。三条已确认的设计决策：

- **D1 不做参数升级兼容**：旧 Flash page99 自动作废、回落源码默认值。
- **D2 实时确认走阻塞问答**：新增 `SNAP` 命令，**不新增帧类型、不新增周期任务、常驻带宽零增量**。
- **D3 高分辨率记录沿用双流式**：只给 `SCOPE` 加**录制剖面**，零新增 RAM。

#### 固件

- **`SNAP` 阻塞式测量快照**（`HARDWARE/link.c`）——一问一答地回**全部可测量量**，分 5 个数据域：
  - `SNAP MOTION` 转速/目标/pidErr/PWM/角度/任务/flags（对应快帧）
  - `SNAP ODOM` 里程/编码器角/陀螺原始/零偏/绝对角（对应中帧）
  - `SNAP SENSE` **四路红外原始 ADC + 四路 mm + 前向综合 + 饱和掩码** + 墙位/坐标/任务/红外开关/侧墙阈值（对应慢帧 **+ 新增 ADC 与饱和**）
  - `SNAP POWER` **电池原始 ADC / mV / 电量档 / 低电标志 / 采样使能** + CPU/丢包/过流（**全新**）
  - `SNAP TIME` 设备时基/发车时刻/已武装/基准灯/遥测流开关
  语法 `SNAP [MOTION|ODOM|SENSE|POWER|TIME|ALL]`，无参数=ALL；被问一次回若干行 + `ACK`。
  **字段名与上位机 `decode_fast/mid/slow` 的字典键同名**，主机侧零分叉。
- **`SNAP` 不依赖 `STREAM ON`**——走命令/应答通道，因此**上电默认静默时也能问**，
  且不占任何常驻遥测带宽（"确认实时值"不再需要先开流）。
- **TX 环缓冲 256 → 512 B**（`link.h`）：一条完整快照 ≈250B 必须能整体入队；
  配套加了**空间门** `link_TxSpace() < 320 → ERR_BUSY`（`link_TxStr` 不做空间检查，
  dump 期间强行写长文本会回绕覆盖尚未发出的数据）。
- **`SCOPE` 录制剖面 A–E**（`link.h` / `link_ScopeSample()`）——同一块 9600B 环
  （`run_data[]`，1200 点 × 8B，1 kHz）换一种"记录内容"，**零新增 RAM**：
  `A` 运动学（默认，逐位等于旧行为）/ `B` 四路红外 mm / `C` 四路红外原始 ADC /
  `D` 前向综合 + 电池 mV/ADC / `E` 前右前左 + 电池 mV + 任务码。
  `DUMP_HDR.channels` 字节兼作剖面标识（**A 仍报 4**，B..E 报 ASCII `'B'`..`'E'`），
  于是旧的 `channels==4` 解析路径一行都不用改。
  `SCOPE START [A|B|C|D|E]`，不带参数 = A（旧语义一字不变）。
- **新增 17 项参数（id 22–38，生产命名空间，落 page99）**——`LINK_P_NUM` 7 → 24：
  - 红外 22–32：`IRTHRWALL`/`IRTHR45`/`IRTHRCRIT`/`IRTHRFLO`/`IRTHRFMID`/`IRTHRFHI`/
    `IRTHRFNEAR`（把 `core.h` 的 7 个阈值宏变成运行时可调）、`IRKALQ`/`IRKALP`（卡尔曼 Q/P，×1e-4）、
    `IRKALSW`（卡尔曼开关）、`IRFRONTMODE`（前向综合算法 0=均值/1=取小/2=加权）。
  - 电源 33–38：`BATTKV`（**分压标定** µV/LSB）、`BATTLOWMV`/`BATTHIGHMV`（档位边界）、
    `BATTALPHA`（IIR 系数 ×1e-3）、`BATTMAXPCT`/`BATTMINPCT`（档位上下限）。
  - **默认值一律等于原来的写死值**（`BATTHIGHMV=7960` 而非 8000，就是为了让
    `step=(high-low)/maxPct=120mV` 与旧式 `(volt-700)/12` **逐位一致**）；
    因此不写参数时行为逐位不变。
  - 落在固件 `linkParam[]` 里 → `GET ALL` / `GET <name>` / `SETQ` / `SAVE` 四条路径**自动全覆盖**。
- **`wallSns.wallThresh` 仍是侧墙阈值的唯一真源**：`IRTHRWALL` 直接读写它，
  因此 `APP/state.c` 在比赛流程里改成 140 的**红线行为不变**。
- **`safety.c` 电池计算参数化**：`mv = adc × BATTKV / 1000`（整数 mV 域滤波，比原来的
  V×100 域细 10 倍），档位由 `(mv-low)/step` 给出，新增 `battMon.adc/mv/low` 三个字段；
  `battMon.voltage`（既有帧用）语义不变。
- **比赛固件（第 5 档）也置 `battMon.enabled = 1`**——原来只有测试档/课程实验打开，
  于是比赛固件下电压恒 0.00V（是"没采"不是"没电"）。只读 ADC，不动任何执行器；
  `SHOWVOLTAGE` 未定义，因此不占 LED。

#### 上位机

- **新子 API `mc.snap`（`host/host/api/snap.py`）**：`read()`/`read_flat()`/`latest()`/`age_ms()`/
  `poll()`/`as_frames()`/`ir_channels()`/`battery()`/`check_battery()`/`explain()`。
  实测语义：`read()` 先清缓存再发命令、等读取线程填缓存（与 `params.get_raw()` 同构，
  保证拿到的是**本次**回话）；`poll()` 是**纯主机侧定时器**，固件无需知道。
- **红外合并视图 `ir_channels()`**：每路给 `{adc, mm, sat, sat_text, trustworthy}`，
  把"距离被标定表夹紧"这件事显式化成 `sat`（0 正常 / 1 近端饱和 / 2 远端超量程）。
  单看 mm 无法区分"真贴住了"与"近得查不出"，这是本次新增的关键信息。
- **`as_frames()`** 把快照映射成遥测帧视角的 dict（并补出 `volt` 派生键），
  于是 `monitor` / `analyze` 直接复用既有字段，没有为 SNAP 单开显示分支。
- **`mc.scope` 剖面接口**：`start(profile)`、`profile()`、`profile_channels()`、
  `last_data()`（按剖面取名取数组）、`fetch_data(profile, collect_s)`。
  剖面 A 的旧接口 `fetch()` / `dump_arrays()` **一字未改**（回归保护）。
- **`mc.analyze` 新增 4 个分析**：`ir_saturation_stats()`（饱和占比/量程使用）、
  `ir_range_curve()`（由剖面 C 的 1kHz 原始 ADC 拟合 `ADC=a·d^b` 标定曲线，含 r²）、
  `batt_low_events()`（低电区间）、`batt_discharge()`（起止/总跌落/**最大瞬时跌落速率**——
  后者是内阻的间接指标，用 0.5Hz 的帧根本看不见）。
- **落盘按剖面走**：`session.open_dump()` 的表头由 `TelemetryStore.dump_profile()` 决定
  （A 仍是老 6 列，B..E 换成该剖面的通道名），dump CSV 文件名变为 `dump_meas_<剖面>_*.csv`。
  否则红外波形会被贴上 `speedl` 的表头 —— 数据看起来对、含义全错。
- **`params.get_all()` 的等待集合改为 `PRODUCTION_PARAM_IDS`**（1–7 + 22–38 = 24 项）：
  固件 `GET ALL` 的循环上界是 `LINK_P_NUM`，课程段 8–21 不参与；按全 38 项等会每次白等满超时。
- **`host.monitor` 新增 `--snap HZ`**：改走阻塞式快照刷新（**不需要 STREAM ON**），
  并额外显示红外原始 ADC/饱和、电池 mV/电量档/低电、设备时基。
  实测在"快0中0慢0 帧"（完全静默）下依然能显示红外与电源。
- **`host.cli` 新增 `snap` 子命令**（`snap` / `--dom` / `--poll` / `--json`），
  `scope fetch` 新增 `--profile`。
- **`PARAM_ENUM`**：新增枚举型参数的取值含义（`IRKALSW`/`IRFRONTMODE` 等），
  `params.table()` 会显示 `0 (均值(现状))` 这样的人话。
- `COMMANDS` 28 → 29（新增 `snap.get`，`scope.start` 增加可选 `prof`）；`FRAMES` 仍 8；
  `PARAM_IDS` 21 → 38；`API_VERSION` 1.4.0 → 1.5.0。

#### 测试

- **新增 `host/snap_smoke.py`（117 项）**：`parse_snap_line`/`decode_ir_sat` 离线语义、
  `SNAP_FIELDS` 与既有帧的字段覆盖关系、命令拼装、5 个剖面的通道表、`channels`→剖面映射、
  dump 块双解（`records` 旧语义 + `words` 原始 4×u16）、SNAP 五域端到端、
  派生视图（`as_frames`/`ir_channels`/`battery`/`check_battery`/`explain`）、
  `poll` 定时、`ERR_BUSY`/`ERR_VALUE` 容错、新增 17 项参数的写+回读。
- 九套件全绿 **553 项**（原 396）：selftest 59 / contract 183 / **snap 117** / motor_lab 44 /
  lesson 38 / timesync 46 / facade 38 / monitor 28。
- 固件三档（[1] 安全 / [3] 课程 / [4] 数据收集）均 **0 Error**，告警数不变；
  新增符号已用 `fromelf` 核查（`link_SnapLine` 922B / `link_SnapService` 90B / `link_IrSat` 28B /
  `linkParam` 192B=24×8B / `irCfg` 18B / `powerCfg` 12B），RAM 合计 ≈17.9KB / 20KB。

#### 未做（明确记录）

- **急停三阈值**（过流 PWM / 连续超限周期数 / 目标速度上限）**未参数化**：
  它们同时被 `motor.c` 的限幅路径消费，动它就要改红线文件，收益不抵风险。
- **`irDist.l45/r45` 等 12 个字段确认为死字段**（只做移位、从不计算；侧墙 45° 判定实际复用
  `irDist.l/r`）。本版**如实标注**，不虚构 45° 通道数据。
- **不做 1 kHz 全通道并行记录**：20KB RAM 装不下（4 通道 × 1200 点需 24KB），故用剖面切换。

---

## [1.4.0] — 2026-09-27

### 新增：数据收集档 + 时间同步（发车计时 / 1Hz 基准灯 / 逐帧准确时间）

按"检测运动特性并收集数据"的要求，补齐**远程发车 + 时间同步 + 完整采集**这条链路
（对应 `docs/数据收集与时间同步.md`）。

- **固件：新测试模式 `MOUSE_DATA_LOG`**（`USER/main.h`，第五档）——电机**可控**且
  **含自走策略**（无记忆走迷宫 + 运动控制）。与 [1][2][3] 的本质差别是**它不定义 `MOUSE_TEST_MODE`**
  （前三个测试档都在"关掉自己跑起来的口子"，而它恰恰要打开）。四个模式宏互斥（加上原比赛固件共五档）。
- **固件：时间同步（不依赖任何档，生产固件也编译）**——`linkTimeMs` 毫秒时基（SysTick 1ms 自增，
  u32 49 天不绕）、`ARM` 命令（记发车时刻 + 点亮 1Hz 基准灯 + 回 `ARMED <ms>`）、
  `MAZE RUN` 命令（远程发车跑无记忆迷宫；仅 [4] 档实现，其余回 `ERR_VALUE`）。
- **固件：新帧 `LINK_TYPE_TIME`(0x08) @1Hz**——payload 12B：`upMs`(u32) + `armMs`(u32) +
  `led`(u8) + `flags`(u8)。与基准灯**同拍**（都在 `linkTimeMs % 1000 == 0`）。
- **固件：快帧保留字节改为时基低 8 位**——`f[15] = linkTimeMs & 0xFF`，
  使**每个快帧**都能被定位到 256ms 窗口内；**帧长一个字节都没变**，零带宽代价。
- **固件：`DUMP_HDR` 8 → 12 字节**，追加 `startMs`(u32) = 回传数组中最早一点的绝对设备毫秒时基。
  于是 1kHz 数据也能被钉在绝对时间轴上。
- **固件：`ledBeaconLock`**（`led.c|h`）——发车后 `LED_On/LED_Off` 全部直接返回，
  基准灯**独占** LED4(PC7)。理由：给外部（摄像头/光电/人眼）做时间对齐用的方波一旦被状态灯搅浑，
  时间同步就废了。其余模式恒为 0，LED 行为一字不变。
- **上位机**：
  - 新模块 `host/host/clock.py`：`now_monotonic()` / `now_wall_ms()`。
    原工程**所有**时间戳都取自墙钟 `time.time()`，一次 NTP 回拨就会让时间映射倒挂。
    数据链路的时间计算改用单调时钟。
  - 注册表新增 `arm`、`maze.run` 两条命令（`COMMANDS` 26 → **28**）；
    `FRAMES` 新增 `TIME`（7 → **8** 种）。
  - 新子 API **`mc.timesync`（`TimeSyncAPI`）**：`arm()` / `maze_run()` / `wait_sync()` /
    `offset_s()` / `drift_ppm()` / `frame_time_up()` / `frame_time()` / `dev_ms()` /
    `frame_age_ms()` / `snapshot()` / `explain()`。
  - **算时间的纯函数**（可脱硬件单测）：`min_delay_offset`（min-delay 偏移估计）、
    `fit_clock` / `drift_ppm`（最小二乘频差）、`resolve_ms8`（8 位 → 完整时基）。
  - **丢帧检测**：帧头 SEQ 原来解析后即丢弃，现在在帧分派处启用 ——
    `stats()` 新增 `seq_gap`（间隙次数）与 `seq_lost`（丢失帧数）。
  - **落盘**：`stream_*.csv` 新增 `dev_ms`（发车以来毫秒）与 `seq` 两列；
    帧的"到达时刻"（`_rx_mono`）与序号跟着记录走，落盘与 UI 拿到同一时刻。
  - 新事件 `Event.TIMESYNC`。
  - 新终端入口 **`python -m host.datalog`**（`host.cli datalog` 等价）：
    `--arm-only` / `--maze-only` / `--status` / `--seconds` / `--no-maze` / `--out`，
    一次跑完"发车 → 远程发车 → 实时监视（带 dev_ms）→ 停车 → 1kHz 回传 → 完整性报告"。
  - 模拟器实现同一套：毫秒时基、`ARM`/`ARMED`、`MAZE RUN`、`TIME` 帧、快帧低 8 位、`DUMP_HDR.startMs`。

### 变更（行为）

- **dump 的 `t_ms` 语义变更**：现在是**绝对设备毫秒时基**（`t = startMs + i`），不再从 0 起。
  这样 1kHz 数据可直接与发车时刻/基准灯对齐；按"差值"使用它的分析代码不受影响。
- `motor_lab_smoke` 的"时间轴从 0 开始"断言随之更新为"等长且逐点 +1ms 且起点 > 0"。

### 测试

- 新增 `host/timesync_smoke.py`（T1–T7：纯函数 / 发车 / 逐帧时间 / dump 绝对时间轴 /
  丢帧检测 / 落盘完整性 / 远程发车）。
- `contract_test.py` 基线更新（28 命令 / 8 帧），D1 覆盖 `arm`/`maze.run`，
  帧往返新增 `TIME`，S5 CLI 子命令加入 `datalog`。
- 全绿 **396**：`selftest --e2e` 59 / `contract_test` **144** / `motor_lab_smoke` 44 /
  `lesson_smoke` 38 / **`timesync_smoke` 46** / `facade_smoke` 37 / `monitor_smoke` 28。

### 固件构建（`MOUSE_DATA_LOG`）

- `0 Error(s)`；Code 52416 + RO 21208 = **73624 B** ROM（自走策略已编入，故大于测试档）。
- 符号：`LINK_MazeRequested`/`LINK_MazeStarted`/`ledBeaconLock` 存在；
  自走路径 `LESSON_Blind8`/`doTaskTurn`/`MOTION_MouseTurnBack`/`EXPLORE_CrosswayChoice`/`SEARCH_HasWall` 全在。

---

## [1.3.0] — 2026-09-27

### 新增：课程调参实验 + 参数设定（实验三~八）

按课件要求补齐"参数设定功能"：上位机在 PC 上设定关键目标参数 → 传到电脑鼠 → 按课件剖面动作 →
回传过程数据 → 上位机**自动算出应写回的修正参数**（对应 `docs/课程调参实验与参数设定.md`）。

- **固件：新测试模式 `MOUSE_LESSON_LAB`**（`USER/main.h`）——第四档模式，电机**可控**、无自走策略、
  但多一层"按课件剖面自动跑"的引擎；与 `MOUSE_TEST_NO_MOTOR` / `MOUSE_TEST_MOTOR_LAB` **三者互斥**。
- **固件：新模块 `APP/lesson_lab.c|h`**——整体被 `#ifdef MOUSE_LESSON_LAB` 包住，
  生产固件里**一个字节都不占**（实验代码与最终版代码分开）。含 `LESSONLAB_Init/Tick/Start/Stop/
  Abort/Running/Exp/TakeDone/ParamGet/ParamSet`。
- **固件：课程参数表（id 8–21，14 项）**——`KPL/KIL/KDL/KPR/KIR/KDR`（PID，默认值取自 `motor.h` 红线常量）、
  `IRPL/IRPR/GYROP/GYROD`（纠偏增益）、`SPEEDNEED/ALLDIS`、`SENSORMODE`（派生：写它同改红外/陀螺开关）、`WALLMODE`。
  单独落 **Flash page100**，与生产标定页（99）互不覆盖。
- **固件：`LESSON` 命令组**——`START <exp> [a] [b] [c] [d]` / `STOP` / `STATUS`；
  拒绝码 `ERR_ESTOP` / `ERR_BUSY` / `ERR_VALUE` / `ERR_EXP`。`LESSON` 与 `MOVE` **互斥**。
- **固件：`LESSON DONE <exp> <REASON> <ms> <dL> <dR> <vpkL> <vpkR> <angle>`** 明细行；
  四个实验剖面（2 编码器测距 / 3 匀变速直线 / 5 陀螺标定 / 6 多传感器直行）全在 1 ms 引擎里推进，
  **每个实验都自带时间护栏**（上位机失联也不会失控）。
- **固件：`MOTOR_LoopControl` 在 `MOUSE_LESSON_LAB` 下改用 `lessonGain.*`**——
  电机六增益变成运行时变量；**红线常量数值一字未改**，只是从常量变成可改的变量。
- **上位机**：
  - `protocol/commands.py` 拆出 `PARAM_IDS_BASE`(1–7) + **`LESSON_PARAM_IDS`(8–21)**，加 `PARAM_GROUPS`；
    `PARAM_META` 补全 14 项课程参数（`PARAM_IDS` 7 → **21**）。
  - 注册表新增 `lesson.start` / `lesson.stop` / `lesson.status` 三条命令（`COMMANDS` 23 → **26**）。
  - 新子 API **`mc.lesson`（`LessonAPI`）**：`params()` / `param_meta()` / `set_params()` / `read_param()` /
    `status()` / `start()` / `stop()` / `run()`（阻塞闭环：采集→启动→等 DONE→冻结→回传→自动算参）/
    `fetch_log()` / `explain()`；返回 `LessonResult`。
  - `api/lesson.py` 内置**算参纯函数**（可脱离硬件单测）：`theory_track_ratio`（62.2267）、
    `theory_gyro_ratio`（415.8061）、`solve_ratio`（实验三/六共用）、`error_ratio`（≤3% 验收线）、
    `step_metrics`（σ%/ts/tp）、`straightness_metrics`、`ir_error_stats` / `pick_best_position`。
  - 新事件 `Event.LESSON`（`kind=DONE`/`STATUS`）。
  - 新终端实验台 **`python -m host.lesson`**（`host.cli lesson` 等价）：`--list` / `--theory` /
    `--params` / `--set` / `--start` / `--stop` / `--truth` / `--apply` / `--save`。
    其中 `--truth <实测真值>` + `--apply` 完成"回传数据 → 自动算参 → 写回设备"的闭环。
  - 模拟器实现同一套 `LESSON` 命令组、四个实验剖面、14 项课程参数与 `LESSON DONE` 回传。

### 修正

- `mc.lesson.set_params()` 只接受课程命名空间；生产标定（`TRACKL/TRACKR/GYRORATIO`）必须走 `mc.params.set()`。
  终端 `--apply` 已按命名空间自动分流（两套参数在不同的 Flash 页）。
- `facade_smoke` / `selftest` 的"GET ALL 回读 7 项"断言更新为 **21 项**（生产 7 + 课程 14）。

### 测试

- 新增 `host/lesson_smoke.py`（L1–L5：参数读写 / 启停 / 四个剖面 / 自动算参写回 / 安全固件立即拒绝）。
- `contract_test.py` 基线更新（26 命令 / 21 参数），D1 覆盖三个 LESSON 命令，D2 覆盖全部 21 个参数。
- 全绿：`selftest --e2e` **59/59**、`contract_test` **137/137**、`motor_lab_smoke` **43/43**、
  `lesson_smoke` **38/38**、`facade_smoke` **37/37**、`monitor_smoke` **28/28**。

### 固件构建（`MOUSE_LESSON_LAB`）

- `0 Error(s), 0 Warning(s)`；Code 29588 + RO 460 = **30048 B** ROM。
- 符号核查：`LESSONLAB_*`（10 个函数）、`lessonGain`、`lessonLab` 存在；
  自走路径 `MENU_Run`/`STATE_MainLoop`/`doTaskTurn`/`LESSON_Motor1` 全部为 0。

---

## [1.2.0] — 2026-09-27

### 新增：电机远程控制实验 + 双流数据传输

轮子悬空阶段的**远程电机测试**：一条命令让电机转一下，同时验证"运动时走实时流、
停止后回传高分辨率日志"的分时双流方案（对应 `docs/电机远程控制与双流传输.md`，
设计出处 `docs/蓝牙上位机设计方案.md` §5.5）。

- **固件：新测试模式 `MOUSE_TEST_MOTOR_LAB`**（`USER/main.h`）——电机**可控**，但不含自走策略；
  与 `MOUSE_TEST_NO_MOTOR` 互斥，派生 `MOUSE_TEST_MODE` 统一包住测试分支。
  `isr.c` 的急停分支在测试模式下不升睡眠、改为调 `LINK_EStopTrip()`。
- **固件：`MOVE RUN <spL> <spR> [ms] [ir_mm]`**——受**时间（默认 2 s）+ 红外（默认 1 cm，负值关闭）
  双护栏**约束的远程运动，护栏全在 1 ms ISR 判定，不依赖上位机在线。
- **固件：`ESTOP` 锁存**——随时可用的急停；锁存后一切 `MOVE`/`CAL` 返回 `ERR_ESTOP`，
  必须显式 `STOP` 解锁。急停触发上行一行 `ESTOP TRIP`（`isr.c` 按键急停同样调用）。
- **固件：`MOVEDONE RUN <REASON> <ms> <ir_min> <chan> <dL> <dR> <vpkL> <vpkR>`** 明细行；
  停止时冻结 1 kHz 环并置就绪位，供事后整块回传。
- **快帧 flags 新增两位**：`LINK_FLAG_ESTOP_LOCK`(0x40)、`LINK_FLAG_RUNLOG`(0x80)。
- **上位机**：
  - 注册表新增 `move.run`、`estop.clear` 两条命令（`COMMANDS` 21 → **23**）。
  - 新子 API **`mc.lab`（`MotorLabAPI`）**：`status()` / `run()`（双流编排 + 前置拦截）/
    `estop()` / `resume()` / `fetch_log()` / `explain()`；返回 `RunResult`。
  - `mc.move` 新增 `run()` / `estop()` / `resume()` / `stop()`；
    `mc.estop_locked` / `mc.log_ready` 两个属性。
  - 新事件 `Event.ESTOP`；监视器状态行新增"急停锁存" / "运动日志已就绪"。
  - 新终端实验入口 **`python -m host.motor_test`**（`host.cli motor-test` / `host.cli estop` 等价）。
  - 模拟器实现同一套 `MOVE RUN` 双护栏 / `ESTOP` 锁存 / `MOVEDONE RUN` / 两个状态位。

### 修正

- 修复 `host/cli.py` 中 `diagnose` 子命令的**语法错误**（两条语句挤在一行），
  该错误会让整个 CLI 无法导入；新增前它未被任何测试覆盖。
- `tools/gen_api_reference.py` 补上 `mc.lab` 子 API 段与 `estop` 事件说明。

### 测试

- 新增 `host/motor_lab_smoke.py`（L1–L5：时间护栏 / 红外护栏 / 急停锁存 / 双流 / 安全固件快速拒绝）。
- `contract_test.py` 新增静态检查 **S5**：CLI 必须可导入且子命令齐备（正是上面那个语法错误的回归护栏）。
- 全绿：`contract_test` **100/100**、`motor_lab_smoke` **43/43**、`selftest --e2e` **59/59**、
  `facade_smoke` **37/37**、`monitor_smoke` **28/28**。

### 真机验证（2026-09-27）

- 实验室固件经 J-Link 烧录，回读与 `Mouse.bin` **字节一致**（MD5 吻合）。
- 真机跑 `--sp 40 --time 2 --ir 10`：停止原因 **TIME @ 2000 ms**，实测峰值 L 46 / R 50 cm/s，
  坏 CRC 0，日志 1200 点回传成功。
- **吞吐模型修订**：一次 `SCOPE DUMP`（20 块 × 490 B ≈ 9.8 KB）真机耗时 **~7.1 s（≈1.38 KB/s）**，
  为 UART 理论上限（11520 B/s）的约 12%，即早期"152 B/s"是**常驻预算**而非突发上限。
  该耗时**与采集点数无关**（固件固定回传 20 块）。已同步修订 `docs/蓝牙上位机设计方案.md` §5.5
  与 `docs/电机远程控制与双流传输.md` §4.1、§7.4。

---

## [1.1.0] — 2026-09-27

### 新增：可编程接口门面（P1–P4 完成）

把原先散落在 `ui/main_window.py` 里的编排逻辑收口为 headless 门面，并建立
"新增功能只改一张表"的强制机制。

- **门面 `host.api.MouseClient`**
  - 连接生命周期：连接自动 `STREAM ON`、断开前 `STREAM OFF`、失败不建会话。
  - 20ms 调度线程：`poll()` / 事件派发 / 订阅式落盘（修复高采样率漏帧）/ 周期任务。
  - 事件总线（push/pull 双模式）、`wait()`/`wait_event()` 阻塞辅助。
  - 九个子 API：`conn` `params` `telemetry` `stream` `calib` `move` `scope` `maze` `analyze` `session`。
- **注册表 `host.registry`**（E1 单一登记点）
  - `COMMANDS`（21 条命令）/ `FRAMES`（7 种帧）登记表。
  - `link.py` 帧分派改为查表，新增帧不改 link.py。
- **契约测试 `contract_test.py`**（E3）：遍历登记表，静态断言 + 模拟器逐条 ACK + 参数往返。
- **CLI `host.cli`**（P3）：`ports/connect/get/set/calib/scope/maze/diagnose/scan-baud/replay`，`--json` 输出。
- **回放 `host.replay`**（P4）：`Target(kind="replay")` 按 `raw.ts` 原始节奏重放历史会话。
- **文档自动生成 `tools/gen_api_reference.py`**（E5）：`docs/api_reference.md` 由登记表+docstring 反射生成。

### 修正

- 删除固件不存在的幽灵命令：`text`（`TEXT ON/OFF`）、`move.servo`（`MOVE SERVO`）、
  `scope.arm`（`SCOPE ARM`）—— 登记表现与固件实际命令集逐条一致（21 条）。
- 修正 `RATE` 取值与固件分档一致：`{100, 50, 20, 10, 5, 2}` Hz。
- 修正 `send_cmd`/`command_line`/`registry.run` 的「命令名」形参 `name` 与参数命令
  的 `name` 参数撞名问题（形参统一改为 `cmd`）。
- 模拟器对齐固件：上电默认静默 + `STREAM ON/OFF` 门控遥测 + `RATE` 分档一致。

### 明确不做（用户确认）

- 固件匹配/跨语言同步脚本 `tools/sync_check.py`（E2）——不需要。
- 固件新增 `CAPS` 能力协商命令（E4）——不需要兼容性协商。

---

## [1.0.0] — 之前

- 协议层（帧编解码、CRC16、Q16.16、流式分流）、命令层（ACK/重发/幂等/回读）、
  传输层（串口/TCP 双模）、数据模型（环形缓冲/会话落盘）、分析指标。
- 实机联调：蓝牙透传链路吞吐实测 ~152 B/s 上限，默认静默 + STREAM 开关方案落地。
