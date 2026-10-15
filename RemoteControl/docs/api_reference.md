# 上位机可编程接口参考（API Reference）

> 由 `tools/gen_api_reference.py` 自动生成，勿手改。生成时间：2026-10-16 01:32:12
> API 版本：`2.10.1`　|　包：`host`

> 这是「让上位机做一件事 = 调一个方法」的完整接口清单。
> 每个方法都会等待设备应答（默认 `wait=True` 且带超时），脚本能写直线代码。

## 1. 快速开始

```python
from host.api import MouseClient, Event

with MouseClient.connect_to("COM7") as mc:        # 退出自动断开+收会话
    mc.telemetry.wait_first_frame(timeout=5)        # 等首帧
    print(mc.device)                                # 设备自述(proto/fw/mazetype)
    mc.params.set("TRACKL", 53.2015)                # 写参数(ACK+回读校验)
    h = mc.telemetry.history_dict(seconds=10)       # 统一帧历史(拉取式, 帧率实测)
    print(mc.analyze.explain(h))                    # 按动作段给出指标
```

无硬件时用 `mc.connect("tcp://127.0.0.1:5555")` 连模拟器，或 `replay://<会话目录>` 回放。

## 2. 线程契约

| 线程 | 职责 |
|---|---|
| 读取线程 | 读字节 → 解析 → 写 store / 回调里只入队 |
| 调度线程 | `poll()` / 事件派发 / 订阅式落盘 / 周期任务；**订阅回调在此线程执行** |
| 调用者线程 | 调 API；`wait=True` 时阻塞等待 |

> 事件订阅回调里**不得**调用会阻塞等待设备的方法（会自锁），误用会抛 `ReentrancyError`。

## 3. 命令登记表（`registry.COMMANDS`）

新增功能只改这一张表。

| name | 线上示例 | args | since | 说明 |
|---|---|---|---|---|
| `arm` | `ARM` | `—` | 1.0 | 发车: 固件记下发车时刻 + 点亮 1Hz 时间基准灯(50% 占空比), 并回 `ACK <seq> OK <armMs>`(发车时基随 ACK 回, 不再单发 ARMED 行) |
| `calib.cal` | `CAL GYRO START` | `item, act` | 1.0 | 标定项控制。**请求-响应五项**(采完才回 ACK): item ∈ {GYRO,WALLDIST,IRDATA,GYRORATIO,ENCANGLE}, act ∈ {START,ABORT} 或 IRDATA 的相位号 0..3; 最坏可能数秒, 故超时 10s 且不重发。**v2.7 菜单远程化六项**(立即 ACK, act=None 省略 act 词): ERASEIR=擦红外标定页(菜单2) / IRSELF=红外自检仅传感器段(菜单4) / SENSOR=显示传感器数据(菜单5) / IRCAP <1..4>=红外 8 值分步标定, 须从 1 开始逐步调且第 1 步前页内须为全 FF(菜单6) / BLOCK=块数据概览, 墙图仍走 maze.dump(菜单7) / ERASEALL=擦迷宫路径 6 页后回值守不休眠(菜单8)。⚠️ ERASEIR / ERASEALL 不可逆, 须显式 confirm=True(上位机 precheck); 全部六项仅远程档支持, 均在运动中回 ERR_BUSY。结果值用 `GET` 回读 |
| `estop` | `ESTOP` | `—` | 1.0 | 急停(置 freemode, 并锁存直到 STOP) |
| `estop.clear` | `STOP` | `—` | 1.0 | `stop` 的同义入口(语义即「停车 + 解急停锁存」), 保留以便书写直观 |
| `maze.dump` | `MAP DUMP` | `—` | 1.0 | 墙图整块回传(98B) |
| `maze.run` | `MAZE RUN` | `target` | 1.0 | 远程发车(**仅远程档 MOUSE_REMOTE**; 比赛固件回 ERR_VALUE)。 target ∈ {BLIND, RACE}: BLIND=无记忆走迷宫(实验八剖面, 默认), RACE=正式比赛代码(先读图选档再发车)。 固件只置请求位, 真正的 while(1) 迷宫由主循环执行 —— 所以下达后命令通道与遥测都不会被阻塞。 ⚠️ 已在跑时回 `ERR_BUSY`, 急停锁存中回 `ERR_ESTOP`; 收车用 `maze.stop`(或 `system.reset`), 之后才能再次发车。 v2.8 起(远程档): 命令被接受的**同一时刻固件点亮全部 4 个 LED**, 保持 `LINK_LAUNCH_MARK_MS`(300ms)后释放 —— 录像时以「4 灯齐亮」那一帧对齐发车时刻; 窗口过后 LED 行为与从前一致(本命令的协议文本/应答均未变) |
| `maze.stop` | `MAZE STOP` | `—` | 1.0 | **把电脑鼠带出迷宫**: 停车 + 复位发车状态 + 解锁急停(**仅远程档 MOUSE_REMOTE**; 比赛固件回 ERR_VALUE)。 发车后固件主循环停在 LESSON_Blind8() 的 while(1) 里, 光靠 ESTOP/STOP 停不掉那个循环、发车状态无法复位。 ⚠️ 主循环里有大量**非协作式阻塞等待**, 标志位常常轮不到 ⇒ 现场常见现象是回 OK 但 task 仍在 SEARCH; 真正可靠的收车是 `system.reset` |
| `move.fwd` | `MOVE FWD 3000 40` | `pulses, speed` | 1.0 | 固定脉冲直线(开环) |
| `move.rot` | `MOVE ROT 2000 100` | `pwm, ms` | 1.0 | 固定 PWM 定长原地转(开环) |
| `move.run` | `MOVE RUN 40 40 100 10` | `sp_l, sp_r, ms, ir_mm` | 1.0 | 受双护栏约束的远程运动: 左右轮速度(cm/s) + 最长运行时间(默认2000ms) + 红外停车阈值(默认10mm, 负值=关闭)。停止后上行 `STOPPED <REASON> #n` |
| `params.get` | `GET TRACKL` | `name` | 1.0 | 回读单个参数(Q16.16) |
| `params.get_all` | `GET ALL` | `—` | 1.0 | 回读全部生产参数(远程档 41 项) |
| `params.reset` | `RESET` | `—` | 1.0 | 擦参数页并复位为源码默认值 |
| `params.save` | `SAVE` | `—` | 1.0 | 参数表写入 Flash |
| `params.set` | `SET TRACKL 53.2` | `name, v` | 1.0 | 写十进制(精度低, 仅供手工调试) |
| `params.setq` | `SETQ TRACKL 3486515` | `name, q` | 1.0 | 写精确 Q16.16 定点(推荐路径) |
| `ping` | `PING` | `—` | 1.0 | 心跳; 回 `PONG <proto> <fw> <mazetype> <txbuf> <rst>`, rst ∈ {POR,SW,IWDG,PIN} 是**上次复位原因**(连上后第一条就该发它) |
| `rate.fix` | `RATE FIX 0` | `hz` | 1.0 | **固定节拍压测**: 固件按恒定 hz(1..30) 自时钟持续发帧, 不看门狗、不降速、不停发, 直到 `rate.off`(或改下 `rate.pull`)。用途: 以固定 FPS 为通信基准做连续通信 / MTBF 压测, 便于跨环境比对断连时刻分布。⚠️ 跑完**必须** `rate.off` 收尾, 否则帧会一直发。 |
| `rate.off` | `RATE OFF` | `—` | 1.0 | 关掉自时钟, 回到纯 `telem` 拉取(v2.5 行为)。压测/拉取收尾时下发。 |
| `rate.pull` | `RATE PULL 0 0` | `n, t_ms` | 1.0 | **批量节拍(PC 主导)**: 固件按 t_ms 间隔连发 n 帧(立即发第一帧)后静默, 等下一条 `rate.pull`; 期间收到新的 PULL 立即打断重排(旧批次剩余帧作废, **不计入丢帧**)。n ∈ [1,16], t_ms ∈ [1,5000]。这是「事件触发式拉取(周期 ≡ RTT)」的载体 —— 上位机收到本周期第 1 帧就发下一条。 |
| `stop` | `STOP` | `—` | 1.0 | 正常停车 + **解急停锁存**(远程档); 这是重新获得控制权的唯一入口 |
| `system.reset` | `MCU RESET` | `—` | 1.0 | **整机软复位**(`Sys_Soft_Reset`, SYSRESETREQ): 程序从 main() 重跑, 用于把设备从**卡死**中救回来 —— 走迷宫的主循环里有大量非协作式阻塞等待(等编码器里程/传感器), 而 `STOP` 会让电机永不再转, 那些条件永远不成立, 于是程序卡在函数内部、连 `MAZE STOP` 的退出检查都轮不到(真机实测)。 ⚠️ 与 `params.reset` (发的是 `RESET`, 只擦参数不重启) **不是一回事**。 ACK 先回, 约 150ms 后才真正复位; 成功判据是**复位后 PING 报 `rst=SW`**。 仅**远程档**(MOUSE_REMOTE)支持, 比赛固件回 ERR_VALUE, 且会打断任何进行中的运动 |
| `tare` | `TARE` | `—` | 1.0 | 陀螺角累加器与里程基准清零 |
| `telem` | `TELEM` | `—` | 1.0 | **拉取一帧遥测**: 回一帧统一帧(49B 载荷/57B 帧) + `ACK <seq> OK <upMs>`(带时间锚)。v2.1 起遥测是**请求-响应**式的 —— 固件不再自时钟发送, 发多少帧完全由上位机决定。⚠️ 必须**一次只发一条**(严格停等): 固件每 1ms tick 只消费一行命令, 成串发必丢(实测回收率: 背靠背 2 条仅 50%)。上位机的 `api/stream.py` 已按停等实现，会自动调用它。不重发: 每次请求都必须真的产生一帧, 重发只会多要一帧。 |

## 4. 帧登记表（`registry.FRAMES`）

| type | 名称 | 流 | since |
|---|---|---|---|
| 0x07 | MAP | map | 1.0 |
| 0x0A | UNIFIED | unified | 1.0 |

## 5. 参数表（`PARAM_IDS`）

| name | id | 单位 | 小数位 | 说明 |
|---|---|---|---|---|
| `TRACKL` | 1 | pulse/mm | 4 | 左轮里程系数; 走一格判据的直接因子, 最值得标定 |
| `TRACKR` | 2 | pulse/mm | 4 | 右轮里程系数 |
| `SPEEDL` | 3 | (pulse/ms)/(cm/s) | 6 | 左轮速度换算; 恒 = TRACKL/100, 由上位机派生 |
| `SPEEDR` | 4 | (pulse/ms)/(cm/s) | 6 | 右轮速度换算; 恒 = TRACKR/100, 由上位机派生 |
| `GYRORATIO` | 5 | LSB/度 | 0 | 陀螺角速率积分系数 |
| `ENCANGLE` | 6 | pulse/度 | 0 | 左右轮脉冲差 -> 角度; 固件为 u16, 取整 |
| `WALLDIST` | 7 | mm | 0 | 车中心到墙距离; 固件为 u8, 取整 |
| `IRTHRWALL` | 22 | mm | 0 | 侧墙判定阈值; 唯一真源 = wallSns.wallThresh, 默认 160 |
| `IRTHR45` | 23 | mm | 0 | 45 度侧墙阈值(斜线矫正用); 默认 150 |
| `IRTHRCRIT` | 24 | mm | 0 | 临界墙阈值(斜线转弯起始点, 低于此值禁动); 默认 60 |
| `IRTHRFLO` | 25 | mm | 0 | 前墙远距-低(双传感器确认有墙); 默认 230 |
| `IRTHRFMID` | 26 | mm | 0 | 前墙远距-中; 默认 240 |
| `IRTHRFHI` | 27 | mm | 0 | 前墙远距-高; 默认 245 |
| `IRTHRFNEAR` | 28 | mm | 0 | 前墙近距(单传感器即判定有墙); 默认 80 |
| `IRKALQ` | 29 | 1e-4 | 0 | 卡尔曼过程噪声 ×10000(越小越信任预测); 默认 100 = 0.0100 |
| `IRKALP` | 30 | 1e-4 | 0 | 卡尔曼测量噪声 ×10000(越大越平滑但迟钝); 默认 2000 = 0.2000 |
| `IRKALSW` | 31 | 0|1 | 0 | 卡尔曼滤波开关; 默认 1 |
| `IRFRONTMODE` | 32 | 0-2 | 0 | 前向综合距离: 0=均值(现状) 1=取小(更保守) 2=加权; 默认 0 |
| `BATTKV` | 33 | µV/LSB | 3 | 分压系数: mV = adcBatt × BATTKV / 1000; 默认 2417 (3.3V×3÷4096×1000) |
| `BATTLOWMV` | 34 | mV | 0 | 电量档下限(低于此值报低电); 默认 7000 |
| `BATTHIGHMV` | 35 | mV | 0 | 满电参考; step = (HIGH-LOW)/MAXPCT; 默认 7960 (=8×120mV 步进) |
| `BATTALPHA` | 36 | 1e-3 | 0 | 电压一阶 IIR 系数 ×1000; 0=不滤波, 1000=完全跟随; 默认 100 |
| `BATTMAXPCT` | 37 | 1-8 | 0 | 电量档上限; 默认 8 |
| `BATTMINPCT` | 38 | 0-8 | 0 | 电量档下限; 默认 1 |
| `TRACKCOORD` | 39 | 格 | 0 | 纵向矫正灵敏度(有墙->无墙时重置格内里程); 默认 145 |
| `TURNDELAY` | 40 | 格 | 1 | 转弯延迟格数(浮点); ⚠️ 转弯后回落点固定 70 与冲刺路径会覆写, 见调整表 |
| `SPDSEARCH` | 41 | cm/s | 0 | 搜索直线速度初值; ⚠️ 转弯后回落固定 70、运动中按工况重算 |
| `SPDTURN` | 42 | cm/s | 0 | 转弯速度; 冲刺路径会覆写为 80(远程档不进冲刺) |
| `DISF60` | 43 | mm | 0 | 180°前对正判据阈值(前墙距离>此值才对正); 默认 60 |
| `GSKP` | 44 |  | 3 | 盲走直行P增益; 默认 0.4 |
| `TURNP` | 45 |  | 3 | 转弯P增益; ⚠️ 冲刺路径会覆写(8/10/4)且不恢复, 远程档不进冲刺 |
| `TURND` | 46 |  | 3 | 转弯D增益; ⚠️ 同 TURNP |
| `PTKP` | 47 |  | 3 | 原地转P增益(POINTTURN90/TURNAROUND); 默认 2.5 |
| `PTKI` | 48 |  | 3 | 原地转I增益(仅 POINTTURN90); 默认 0.01 |
| `PTKD` | 49 |  | 3 | 原地转D增益; 默认 0.1 |
| `MKPL` | 50 |  | 3 | 左轮电机P(freemode=0 每周期生效); 默认 10.75 |
| `MKIL` | 51 |  | 3 | 左轮电机I; 默认 0.55 |
| `MKDL` | 52 |  | 3 | 左轮电机D; 默认 0.55 |
| `MKPR` | 53 |  | 3 | 右轮电机P; 默认 10.20 |
| `MKIR` | 54 |  | 3 | 右轮电机I; 默认 0.5 |
| `MKDR` | 55 |  | 3 | 右轮电机D; 默认 0.5 |

## 6. 事件（`Event`）

| 事件 | 载荷 |
|---|---|
| `state` | 链路状态变更 {state, detail} |
| `error` | 打开/读/解码错误 {kind, msg} |
| `telemetry` | 一帧遥测 {stream, rec} |
| `text` | 设备文本(含 printf) {line, direction} |
| `param` | 参数回读 {id, name, q} |
| `calib` | 标定消息 {kind, fields, raw}(v2.0 起 CAL 为请求-响应, 一般经 ACK 返回) |
| `move` | 运动结束消息 {kind, fields, raw}(v2.0 起经 EVENT 派发 `STOPPED`) |
| `estop` | 急停触发 {kind, fields, raw} |
| `event` | 统一上行事件 {event, reason, seq, text, raw} (STOPPED / ESTOP TRIP) |
| `timesync` | 时间同步消息 {kind, fields, raw} (kind=anchor/armed/lost) |
| `map` | 墙图 {mazetype, posX, posY, ...} |
| `advice` | 链路诊断 {level, got_bps, need_bps, note} |
| `session` | 会话开关/落盘 {op, path} |

## 7. 门面与子 API 方法

### 7.1 `MouseClient` — 门面本身

| 成员 | 签名 | 说明 |
|---|---|---|
| `close` | `(self) -> 'None'` | 收尾: 断开 + 关会话 + 停调度线程。 |
| `close_session` | `(self, extra: 'Optional[dict]' = None) -> 'None'` |  |
| `connect` | `(self, target: "'Any'" = None, baud: 'Optional[int]' = None, **kw) -> 'bool'` | 打开链路。返回是否成功 —— 失败时**不建会话、不开流**，原因见事件流。 |
| `connect_to` | `(target: 'Any' = None, **kwargs)` | `with MouseClient.connect_to("COM7") as mc:` —— 退出时自动断开并收会话。 |
| `device` | `(属性)` | 设备自述信息(PING 的回话): proto / fw / mazetype / txbuf / rst。 |
| `disconnect` | `(self) -> 'None'` | 断开链路。**先停拉取** —— 这是让蓝牙模块缓冲排空的唯一手段。 |
| `estop_locked` | `(属性)` | 设备是否处于『急停锁存』状态(帧 flags bit6)。 |
| `event_health` | `(self) -> 'Dict[str, Any]'` | 上行消息序号连续性的累计统计(丢帧/丢事件的可检测证据)。 |
| `events_incomplete` | `(属性)` | 事件流是否出现过丢弃(帧 flags bit7 = EVQ)。None = 还没收到帧。 |
| `events_queue` | `(self, maxsize: 'Optional[int]' = None)` | 拿到一个线程安全队列, 所有事件都会投递进去。 |
| `is_connected` | `(属性)` | 链路是否已连通(`LinkState.OPEN` 且后端已建立)。 |
| `motor_disabled` | `(属性)` | 设备是否处于『电机已禁用』安全测试模式。 |
| `motor_guard` | `(self, what: 'str') -> "'Optional[Result]'"` | 需要电机转动的前置检查。 |
| `on` | `(self, kind: "'Event | str'", cb: 'Callable[[EventMsg], None]') -> 'Callable[[], None]'` | 订阅事件。回调在**调度线程**执行(见模块 docstring 的线程契约)。 |
| `open_session` | `(self, name: 'Optional[str]' = None) -> 'Optional[Session]'` |  |
| `reanchor` | `(self, timeout: 'float' = 3.0) -> 'Result'` | 重新索要时间锚(注入一条 `TELEM`, 应答即带完整 u32 时基)。 |
| `recent_events` | `(self, n: 'int' = 50) -> 'List[EventMsg]'` |  |
| `reset_cause` | `(属性)` | 上次复位原因(POR/SW/IWDG/PIN), 来自 PING 应答。未知时 None。 |
| `send_async` | `(self, cmd: 'str', **kw: 'Any')` | 发送但不等待（返回 Pending）。需要自己 `wait(lambda: p.done)`。 |
| `send_cmd` | `(self, cmd: 'str', timeout: 'Optional[float]' = None, **kw: 'Any') -> 'Result'` | 按 `registry.COMMANDS` 里的名字执行一条登记命令，并等待应答。 |
| `send_raw` | `(self, line: 'str', timeout: 'float' = 3.0) -> 'Result'` | 逃生舱: 直接发一条原始命令（不经过登记表）。 |
| `session_obj` | `(属性)` | 当前会话对象(`Session`); 未开会话时为 `None`。 |
| `state` | `(属性)` | 链路状态字符串(`LinkState`): `closed`/`connecting`/`open`/`error`。 |
| `state_detail` | `(属性)` | 状态的人读补充: 失败原因/最近一次连接错误; 为空串表示无附加信息。 |
| `stats` | `(self) -> 'Dict[str, Any]'` |  |
| `wait` | `(self, pred: 'Callable[[], Any]', timeout: 'float' = 10.0, interval: 'float' = 0.01) -> 'bool'` | 阻塞直到 `pred()` 为真或超时。脚本写同步代码用。 |
| `wait_event` | `(self, kind: "'Event | str'", pred: 'Optional[Callable[[EventMsg], bool]]' = None, timeout: 'float' = 10.0, since: 'Optional[float]' = None) -> 'Optional[EventMsg]'` | 阻塞直到出现满足条件的事件, 返回它; 超时返回 None。 |

### 7.2 `mc.conn` — 连接管理

连接管理: 端口枚举、连接/断开、链路统计与自检、单个端口诊断。

| 方法 | 签名 | 说明 |
|---|---|---|
| `bluetooth_port` | `(self) -> 'str'` | 猜一个蓝牙端口名(优先传出端口), 没有则返回空串。 |
| `connect` | `(self, target: 'Any' = None, **kw) -> 'bool'` |  |
| `disconnect` | `(self) -> 'None'` |  |
| `is_connected` | `(属性)` | 链路是否已连通(同 `MouseClient.is_connected`)。 |
| `is_degraded` | `(self) -> 'bool'` |  |
| `port_labels` | `(self) -> 'List[str]'` |  |
| `ports` | `(self) -> 'List[PortInfo]'` | 枚举串口。返回值已按『可用 > 有线 > 蓝牙传出 > 蓝牙传入』排序。 |
| `probe` | `(self, port: 'str', baud: 'int' = 115200, seconds: 'float' = 2.0) -> 'Dict[str, Any]'` | 诊断单个串口: 能否打开、有没有数据、是否像遥测帧、是否像文本。 |
| `reconnect` | `(self, delay: 'float' = 0.5) -> 'bool'` | 断开后用同一目标重连(蓝牙掉线后的恢复路径)。 |
| `rescue` | `(self, **kw: 'Any') -> 'Result'` | `soft_reset()` 的别名 —— 语义更直白: 把卡死的设备救回来。 |
| `scan_baud` | `(self, port: 'str', bauds: 'Optional[List[int]]' = None, seconds: 'float' = 1.0) -> 'List[Dict[str, Any]]'` | 逐个波特率试读。用于确认电脑侧蓝牙模块的 UART 速率。 |
| `self_check` | `(self) -> 'Dict[str, Any]'` | 链路自检: {need_bps, got_bps, level, suggest_rate}。 |
| `soft_reset` | `(self, verify: 'bool' = True, timeout: 'float' = 8.0) -> 'Result'` | **整机软复位**(发 `MCU RESET` → 固件 `Sys_Soft_Reset()`) —— 卡死救援口。 |
| `state` | `(属性)` | 链路状态字符串: `closed`/`connecting`/`open`/`error`。 |
| `state_detail` | `(属性)` | 状态的人读补充(失败原因); 为空串表示无附加信息。 |
| `stats` | `(self) -> 'Dict[str, Any]'` |  |
| `target` | `(属性)` | 当前连接的 `Target`(端口/地址信息); 未连接时为 `None`。 |
| `targets` | `(self) -> 'List[Target]'` | 候选连接目标: 已排序的串口 + 两个模拟器端口。 |
| `wait_open` | `(self, timeout: 'float' = 3.0) -> 'bool'` | 等到链路真的可用(read 线程已起来)。 |

### 7.3 `mc.params` — 参数

参数管理: 读写、回读校验、物理量派生、持久化与备份。

| 方法 | 签名 | 说明 |
|---|---|---|
| `backup` | `(self) -> 'Optional[str]'` | 把当前参数导出为 CSV 留档。没有会话目录时返回 None。 |
| `derive_speed_from_track` | `(self, readback: 'bool' = True, timeout: 'float' = 3.0) -> 'List[Result]'` | 按 `speedRatio = trackRatio / 100` 重新派生速度系数。 |
| `get` | `(self, name: 'str', timeout: 'float' = 3.0) -> 'Optional[float]'` |  |
| `get_all` | `(self, timeout: 'float' = 3.0, refresh: 'bool' = True) -> 'Dict[str, float]'` | 回读全部参数。refresh=True 时先发 `GET ALL`。 |
| `get_raw` | `(self, name: 'str', timeout: 'float' = 3.0) -> 'Optional[int]'` | 回读单个参数的 Q16.16 原始值。失败返回 None。 |
| `is_valid` | `(self, name: 'str') -> 'bool'` |  |
| `meta` | `(self, name: 'str') -> 'Optional[Tuple[str, int, str]]'` | (单位, 小数位, 说明) |
| `names` | `(self) -> 'List[str]'` |  |
| `production_names` | `(self) -> 'List[str]'` | 生产命名空间的参数名(任何固件都有, 且 `GET ALL` 能一次全回)。 |
| `reset` | `(self, timeout: 'float' = 5.0) -> 'Result'` | 擦除参数页并恢复源码默认值(重启后生效)。 |
| `save` | `(self, timeout: 'float' = 5.0) -> 'Result'` | 把当前参数表写入固件 Flash。 |
| `set` | `(self, name: 'str', value: 'float', readback: 'bool' = True, timeout: 'float' = 3.0) -> 'Result'` | 写物理值。内部转 Q16.16 下发, 默认做回读校验(可靠性 L4 层)。 |
| `set_many` | `(self, mapping: 'Dict[str, float]', readback: 'bool' = True, derive_speed: 'bool' = False, timeout: 'float' = 3.0) -> 'List[Result]'` | 批量写。derive_speed=True 时对 TRACK* 自动派生对应的 SPEED*。 |
| `set_raw` | `(self, name: 'str', q: 'int', readback: 'bool' = True, timeout: 'float' = 3.0) -> 'Result'` | 写 Q16.16 原始值(不做换算)。 |
| `snapshot` | `(self) -> 'Dict[str, float]'` | **本地缓存**的参数值(由历次 GET / GET ALL 的回话填充)。 |
| `table` | `(self) -> 'str'` | 人可读的参数表(CLI 用)。按"生产标定 / 红外测量 / 电源监控"分节。 |

### 7.4 `mc.telemetry` — 遥测

遥测读取: 最新快照、环形历史、吞吐统计。

| 方法 | 签名 | 说明 |
|---|---|---|
| `buffer` | `(self, name: 'str' = 'unified') -> 'RingBuffer'` |  |
| `bytes_per_second` | `(self) -> 'float'` |  |
| `channels` | `(self, name: 'str' = 'unified') -> 'List[str]'` |  |
| `event_health` | `(self) -> 'Dict[str, Any]'` | 上行事件流的完整性统计(序号缺口)。 |
| `flags` | `(self) -> 'Dict[str, bool]'` | 统一帧里的状态位。 |
| `history` | `(self, name: 'str' = 'unified', seconds: 'Optional[float]' = None)` | 返回 `(t_seconds_epoch, data[行=样本, 列=通道])`。 |
| `history_dict` | `(self, name: 'str' = 'unified', seconds: 'Optional[float]' = None) -> 'Dict[str, np.ndarray]'` | 把历史拆成 `{通道名: 一维数组}`。 |
| `hz` | `(self, name: 'str' = 'unified') -> 'float'` | **实测**帧率(Hz)。 |
| `is_streaming` | `(self, max_age: 'float' = 2.5) -> 'bool'` | 是否正在收到数据。 |
| `last_frame_age` | `(self, name: 'str' = 'unified') -> 'Optional[float]'` | 最后一帧距今多少秒。None = 从未收到过。 |
| `latest` | `(self) -> 'Dict[str, Any]'` | 统一帧的最新值(扁平 dict)。没有数据时为空 dict。 |
| `live` | `(self, seconds: 'float' = 5.0, interval: 'float' = 0.5, printer=<built-in function print>) -> 'None'` | 在终端里持续打印状态行(CLI/调试用)。 |
| `samples` | `(self, name: 'str' = 'unified') -> 'int'` |  |
| `self_check` | `(self) -> 'Dict[str, Any]'` |  |
| `stats` | `(self) -> 'Dict[str, Any]'` |  |
| `summary_text` | `(self) -> 'str'` | 一行摘要(CLI 与日志用)。 |
| `wait_first_frame` | `(self, timeout: 'float' = 5.0) -> 'bool'` | 等第一帧统一帧到达(连上后最常用的同步点)。 |

### 7.5 `mc.stream` — 遥测流控制

遥测拉取控制: 统一拉取器 `TelemetryPuller` —— 一条命令取回 n 帧, `n=1` 退化为停等(`TELEM` 回复门控), `n≥2` 为批式(`RATE PULL n t` 网格调度)。

| 方法 | 签名 | 说明 |
|---|---|---|
| `anchor` | `(self)` | 当前时间锚 `(本机单调秒, 设备 ms)`; 未建立时 None。 |
| `configure` | `(self, n: 'int', t_ms: 'int') -> 'Dict[str, Any]'` | 纯设参(不启动): 记下批式档 `(n, t_ms)` 并校验(v2.10 新增)。 |
| `is_on` | `(属性)` | 拉取器是否在跑(明确的内部状态, 不再靠"最近有没有帧"猜)。 |
| `mode` | `(属性)` | 当前拉取模式: `"off"` / `"stopwait"` / `"pace"`。 |
| `off` | `(self, timeout: 'float' = 3.0) -> 'Result'` | 停止拉取。**立刻停发** —— 这是让蓝牙模块缓冲排空的唯一手段。 |
| `on` | `(self, timeout: 'float' = 3.0) -> 'Result'` | 开始**停等式**拉取遥测(首条应答即建立时间锚)。非阻塞。 |
| `pace` | `(self, n: 'Optional[int]' = None, t_ms: 'Optional[int]' = None, timeout: 'float' = 3.0) -> 'Result'` | 切换到**批式**拉取(`RATE PULL n t`, 默认 n=3/t=100ms = 10Hz)。 |
| `pace_off` | `(self, timeout: 'float' = 3.0) -> 'Result'` | 退出批式拉取(固件侧发 `RATE OFF` 停发)。 |
| `refresh_anchor` | `(self, timeout: 'float' = 3.0) -> 'Result'` | 立即索要一条时间锚(注入 `TELEM` 等回复)。`mc.reanchor()` 调它。 |
| `resume` | `(self) -> 'None'` | 恢复被 `suspend()` 暂停的拉取(仅当它原本在跑时才恢复)。 |
| `stats` | `(self) -> 'Dict[str, Any]'` | 拉取器状态: 在途数/收发计数/实测帧率 + 门面层模式。 |
| `suspend` | `(self) -> 'None'` | **给命令通道让路**: 暂停拉取并等线上静默(由 `resume()` 恢复)。 |
| `tick` | `(self) -> 'None'` | 调度线程 50Hz 调用: 委派给唯一的拉取器。 |
| `toggle` | `(self, on: 'bool', timeout: 'float' = 3.0) -> 'Result'` |  |
| `tune` | `(self, request_timeout: 'Optional[float]' = None, n: 'Optional[int]' = None, t_ms: 'Optional[int]' = None) -> 'Dict[str, Any]'` | 运行期调参(立即生效)。 |

### 7.6 `mc.calib` — 标定

标定: 5 项物理系数(**请求-响应** 交互模型)。

| 方法 | 签名 | 说明 |
|---|---|---|
| `angle` | `(self, timeout: 'float' = 120.0, apply: 'bool' = True, readback: 'bool' = True) -> 'Result'` | 角度系数 GYRORATIO / ENCANGLE。 |
| `apply_angle` | `(self, gyro_ratio: 'float', enc_angle: 'float', readback: 'bool' = True) -> 'List[Result]'` | 写回两个角度系数。 |
| `apply_angle_and_track` | `(self, gyro_ratio: 'float', enc_angle: 'float', dL: 'int', dR: 'int', measured_mm: 'float') -> 'List[Result]'` | 把角度与里程两项一次写回(界面上"应用"按钮的语义)。 |
| `apply_track` | `(self, dL: 'int', dR: 'int', measured_mm: 'float', derive_speed: 'bool' = True, readback: 'bool' = True) -> 'List[Result]'` | 应用里程系数, 默认同时派生 SPEEDL/R = TRACKL/R ÷ 100。 |
| `block` | `(self, timeout: 'float' = 8.0) -> 'Result'` | 块数据概览 —— 按键菜单 7 的远程化(`CAL BLOCK`)。 |
| `compute_track` | `(dL: 'int', dR: 'int', measured_mm: 'float') -> 'Tuple[float, float]'` | 由『实际跑出的脉冲数』与『尺量距离』算里程系数: 新系数 = 脉冲 / 毫米。 |
| `erase_all` | `(self, confirm: 'bool' = False, timeout: 'float' = 12.0) -> 'Result'` | 擦除迷宫/路径 6 页 —— 按键菜单 8 的远程化(`CAL ERASEALL`)。 |
| `erase_ir` | `(self, confirm: 'bool' = False, timeout: 'float' = 8.0) -> 'Result'` | 擦除红外标定页(page123) —— 按键菜单 2 的远程化(`CAL ERASEIR`)。 |
| `gyro` | `(self, timeout: 'float' = 25.0) -> 'Result'` | 陀螺零偏。要求电脑鼠**静止**; 固件自行采样约 1.5s 并写入既有 Flash 页。 |
| `ir_cap` | `(self, step: 'int', timeout: 'float' = 12.0) -> 'Result'` | 红外 8 值分步标定的第 `step` 步(1..4) —— 按键菜单 6 的远程化。 |
| `ir_cap_all` | `(self, on_prompt: 'Optional[Callable[[int, str, str], bool]]' = None, erase_first: 'bool' = False, timeout_per_step: 'float' = 12.0) -> 'Result'` | 4 步一次跑完(`ir_cap` 的顺序封装)。 |
| `ir_data` | `(self, on_prompt: 'Optional[Callable[[int, str, str], bool]]' = None, timeout_per_point: 'float' = 30.0) -> 'Result'` | 红外 8 值(4 个摆位各采一点)。 |
| `ir_sample` | `(self, phase: 'int') -> 'Result'` | 采集相位 `phase` ∈ 0..3(每一相位 = 一条命令, 采完才 ACK)。 |
| `ir_self` | `(self, timeout: 'float' = 8.0) -> 'Result'` | 红外自检(**仅传感器段**) —— 按键菜单 4 的远程化(`CAL IRSELF`)。 |
| `ir_start` | `(self) -> 'Result'` | 开始红外逐点: 采第 0 个相位(左近墙)。 |
| `items` | `(self) -> 'List[str]'` |  |
| `report` | `(self, lines: 'Sequence[str]') -> 'Optional[str]'` | 把标定过程写入 `calib/report.md`(会话未开则为 None)。 |
| `run_straight` | `(self, pulses: 'int' = 20000, speed: 'int' = 40, timeout: 'Optional[float]' = None) -> 'Result'` | 开环跑一段固定脉冲的直线, 返回停止原因(见 `codec.RUN_REASON_NAMES`)。 |
| `sensor` | `(self, timeout: 'float' = 8.0) -> 'Result'` | 显示传感器数据(固定 4 行) —— 按键菜单 5 的远程化(`CAL SENSOR`)。 |
| `step` | `(self, item: 'str', act: 'Any' = 'START', timeout: 'float' = 12.0) -> 'Result'` | 手工单步: `CAL <item> <act>`。 |
| `track` | `(self, pulses: 'int' = 20000, speed: 'int' = 40, measured_mm: 'Optional[float]' = None, dL: 'Optional[int]' = None, dR: 'Optional[int]' = None, derive_speed: 'bool' = True, timeout: 'Optional[float]' = None) -> 'Result'` | 一步到位: 跑直线 → (给了实测距离与里程差就) 计算并应用。 |
| `wall_distance` | `(self, timeout: 'float' = 25.0) -> 'Result'` | 车心到墙距离。要求电脑鼠**平行贴住左墙**。结果写 WALLDIST 参数, 回读之。 |

### 7.7 `mc.move` — 开环移动

开环移动(标定辅助)与远程双护栏运动。

| 方法 | 签名 | 说明 |
|---|---|---|
| `current_task` | `(self) -> 'Optional[str]'` | 当前运动任务名(来自统一帧), 便于判断"是否已经停下来"。 |
| `estop` | `(self, timeout: 'float' = 3.0) -> 'Result'` | 急停(锁存)。立即停车 + 置 `estop_lock`, 之后的 MOVE/CAL 会被拒。 |
| `forward` | `(self, pulses: 'int' = 20000, speed: 'int' = 0, wait_done: 'bool' = True, timeout: 'Optional[float]' = None) -> 'Result'` | 固定脉冲直线(开环)。`wait_done=True` 时等 `STOPPED`。 |
| `resume` | `(self, timeout: 'float' = 3.0) -> 'Result'` | 正常停车并**解除急停锁存**(固件侧就是 `STOP`)。 |
| `rotate` | `(self, pwm: 'int' = 40, ms: 'int' = 0, wait_done: 'bool' = True, timeout: 'Optional[float]' = None) -> 'Result'` | 固定速度定长原地转(开环)。`pwm` 实为旋转速度(cm/s), 与前进速度同量级。 |
| `run` | `(self, sp_l: 'int' = 40, sp_r: 'int' = 40, ms: 'int' = 2000, ir_mm: 'int' = 10, wait_done: 'bool' = True, timeout: 'Optional[float]' = None) -> 'Result'` | 受**时间 + 红外**双护栏约束的远程运动(电机远程控制实验主入口)。 |
| `servo` | `(self, act: 'str' = 'SWEEP', wait_done: 'bool' = False, timeout: 'Optional[float]' = None) -> 'Result'` | 前向红外极值扫描(角度基准)。 |
| `stop` | `(self, timeout: 'float' = 3.0) -> 'Result'` | 正常停车(等价于 `resume`, 保留更常用的名字)。 |
| `wait_done` | `(self, what: 'str' = 'FWD', timeout: 'float' = 60.0, since: 'Optional[float]' = None)` |  |
| `wait_idle` | `(self, timeout: 'float' = 30.0, idle_speed: 'float' = 2.0) -> 'bool'` | 等到左右轮速都接近 0 —— "车停了"。 |
| `wait_stopped` | `(self, reason: 'Optional[str]' = None, timeout: 'float' = 60.0, since: 'Optional[float]' = None)` | 等待 `STOPPED <reason> #n` 事件。返回 EventMsg 或 None。 |

### 7.8 `mc.lab` — 电机实验(远程运动)

电机远程控制实验(单流)。

| 方法 | 签名 | 说明 |
|---|---|---|
| `capture` | `(self, seconds: 'float' = 2.0, hz: 'float' = 50.0) -> 'List[Dict[str, Any]]'` | 按固定频率采下"最新快照"若干秒(替代旧的 fetch_log), 便于事后画图。 |
| `estop` | `(self, timeout: 'float' = 3.0) -> 'Result'` | 急停(锁存)。**随时可用**, 不依赖任何先决条件。 |
| `explain` | `(self, res: 'RunResult') -> 'str'` | 把一次实验结果整理成几行人能读的结论。 |
| `resume` | `(self, timeout: 'float' = 3.0) -> 'Result'` | 解除急停锁存并正常停车(固件侧 = `STOP`)。 |
| `run` | `(self, sp_l: 'int' = 40, sp_r: 'int' = 40, ms: 'int' = 2000, ir_mm: 'int' = 10, sample_hz: 'float' = 50.0, on_sample: 'Optional[Dict[str, Any]]' = None) -> 'RunResult'` | 跑一次带双护栏的远程运动, 并采下运动期间的统一帧样本。 |
| `status` | `(self) -> 'Dict[str, Any]'` | 一眼看清"现在能不能动"。 |

### 7.9 `mc.timesync` — 时间同步 / 数据收集

时间同步 —— 把"设备毫秒时基"映射到"本机单调时钟", 从而算出**每个接收帧的准确时间**。

| 方法 | 签名 | 说明 |
|---|---|---|
| `arm` | `(self, timeout: 'float' = 3.0) -> 'Result'` | **发车**: 让固件记下发车时刻并点亮 1Hz 时间基准灯。 |
| `dev_ms` | `(self, rec: 'dict') -> 'Optional[int]'` | 把一帧换算成"发车以来毫秒数"。未发车或取不到时基时返回 None。 |
| `dev_ms_from_recv` | `(self, rec: 'dict') -> 'Optional[int]'` | 给取不到毫秒时基的帧估算"发车以来毫秒数"(兜底, 仅为 CSV 有一列时间)。 |
| `drift_ppm` | `(self) -> 'Optional[float]'` |  |
| `explain` | `(self) -> 'str'` |  |
| `frame_time` | `(self, rec: 'dict') -> 'Optional[float]'` | 把一帧记录换算成**本机单调秒**(即"这一帧真正发生的时刻")。 |
| `frame_time_up` | `(self, rec: 'dict') -> 'Optional[int]'` | 把一帧记录换算成**设备毫秒时基**上的发送时刻。 |
| `maze_run` | `(self, target: 'str' = 'BLIND', timeout: 'float' = 3.0) -> 'Result'` | 远程发车(仅**远程档** `MOUSE_REMOTE`; 比赛固件回 ERR_VALUE)。 |
| `needs_anchor` | `(self) -> 'bool'` | 是否需要(重新)索要时间锚。 |
| `note_frame` | `(self, rec: 'dict') -> 'Optional[int]'` | 由读取线程喂入一帧统一帧记录(内部用, 也可手工喂)。 |
| `offset_s` | `(self) -> 'Optional[float]'` | 当前偏移估计(秒)。`t_pc_mono = offset + up_ms/1000`。 |
| `on_arm_ack` | `(self, up_ms: 'int') -> 'None'` | 处理 `ARM` 的应答(发车回执), 其状态串是 `OK <armMs>`。 |
| `reset` | `(self) -> 'None'` | 清空全部观测与发车状态(重连/换设备时必须调)。 |
| `snapshot` | `(self) -> 'TimeSyncState'` |  |
| `wait_sync` | `(self, samples: 'int' = 2, timeout: 'float' = 5.0) -> 'bool'` | 等至少 `samples` 个可用帧到达(建立可用映射)。返回是否达到。 |

### 7.10 `mc.maze` — 迷宫

迷宫: 墙图拉取/渲染 + **状态从统一帧读取**(实验8 无记忆走迷宫)。

| 方法 | 签名 | 说明 |
|---|---|---|
| `cell` | `(self, x: 'int', y: 'int') -> 'Optional[Dict[str, Any]]'` | 某一格的四方向通行情况。 |
| `clear_changes` | `(self) -> 'None'` |  |
| `describe` | `(self) -> 'str'` | 一行摘要: 尺寸、坐标、朝向、已探索格数。 |
| `explain` | `(self) -> 'List[str]'` |  |
| `fetch` | `(self, timeout: 'float' = 10.0) -> 'Optional[Dict[str, Any]]'` | 请求并等待墙图。返回 `decode_map` 的结果。 |
| `grid` | `(self) -> 'Optional[List[List[int]]]'` | 墙位二维数组, `grid[x][y]`。 |
| `last` | `(self) -> 'Optional[Dict[str, Any]]'` | 最近一次收到的墙图(不发命令)。 |
| `mazetype` | `(self) -> 'int'` |  |
| `position` | `(self) -> 'Optional[Tuple[int, int, int, int]]'` | (x, y, direction, mazeTask)。优先用墙图帧, 否则用统一帧。 |
| `road` | `(self) -> 'int'` | 当前路口类型码(由三位墙原语逐帧分类)。 |
| `road_debounced` | `(self, samples: 'int' = 2, gap_s: 'float' = 0.02) -> 'str'` | 连续 `samples` 次(每次间隔 `gap_s`)分类一致才认。复刻固件 20ms 去抖的意图。 |
| `road_name` | `(self) -> 'str'` |  |
| `run` | `(self, target: 'str' = 'BLIND', timeout: 'float' = 3.0)` | 远程发车(仅**远程档** `MOUSE_REMOTE`; 比赛固件回 ERR_VALUE)。 |
| `spurt` | `(self) -> 'Optional[Dict[str, Any]]'` | 比赛冲刺状态(v2.3, 只读)。无数据时 None。 |
| `state` | `(self) -> 'Optional[Dict[str, Any]]'` | 统一的迷宫视图(直接来自最新帧)。无数据时 None。 |
| `stop` | `(self, timeout: 'float' = 3.0)` | 把电脑鼠**带出迷宫**, 复位发车状态并解锁急停（`MAZE STOP`）。 |
| `summary` | `(self) -> 'Dict[str, Any]'` |  |
| `task_changes` | `(self, n: 'Optional[int]' = None) -> 'List[Tuple[float, int, int]]'` | 迷宫任务的迁移序列 `(时刻, from, to)` —— 主机侧差分得到。 |
| `to_text` | `(self, mark: 'Optional[Tuple[int, int]]' = None) -> 'str'` | 渲染成 ASCII 迷宫。 |
| `visited` | `(self) -> 'Optional[List[List[bool]]]'` |  |

### 7.11 `mc.analyze` — 离线分析

离线分析: 阶跃指标、跟踪误差、按运动任务分段。

| 方法 | 签名 | 说明 |
|---|---|---|
| `batt_discharge` | `(self, mv: 'Any', t_s: 'Any' = None) -> 'Dict[str, Any]'` | 电池跌落分析: 起止电压、总跌落、最大瞬时跌落速率(内阻的间接指标)。 |
| `batt_low_events` | `(self, mv: 'Any', low_mv: 'float' = 7000.0) -> 'List[Dict[str, Any]]'` | 从 mV 序列里找出**低电区间**(用于"跑到一半掉电"的复盘)。 |
| `channels` | `(self, h: 'Dict[str, np.ndarray]') -> 'Dict[str, Any]'` | 把 `history_dict()` 的输出整理成分析用的具名通道。 |
| `explain` | `(self, h: 'Dict[str, np.ndarray]') -> 'str'` | 把 summary 变成一段人可读的文本(CLI 与日志用)。 |
| `format` | `(self, m: 'Dict[str, float]') -> 'str'` |  |
| `history_channels` | `(self, seconds: 'Optional[float]' = None) -> 'Dict[str, np.ndarray]'` | 取统一帧历史(具名通道)。需要连接设备或回放。 |
| `history_segments` | `(self, h: 'Dict[str, np.ndarray]') -> 'List[Dict[str, Any]]'` | 把历史按运动任务切段(段内带各通道切片)。 |
| `ir_range_curve` | `(self, adc: 'Any', mm: 'Any', bins: 'int' = 24) -> 'Dict[str, Any]'` | 从"原始 ADC ↔ 距离"配对序列里拟合标定曲线(幂律 `ADC = a * d^b`)。 |
| `ir_saturation_stats` | `(self, series: 'Any' = None) -> 'Dict[str, Any]'` | 统计各通道的**饱和占比**与量程使用情况。 |
| `segments` | `(self, t_ms: 'Sequence[float]', task: 'Sequence[int]') -> 'List[Dict[str, Any]]'` | 按 `motionTask` 变化切段。一次动作就是一段。 |
| `step` | `(self, t: 'Sequence[float]', y: 'Sequence[float]', target: 'float', band: 'float' = 0.02) -> 'Dict[str, float]'` | 阶跃响应指标: rise_s / overshoot_pct / settle_s / sse / peak / final。 |
| `summary` | `(self, h: 'Dict[str, np.ndarray]', band: 'float' = 0.02) -> 'List[Dict[str, Any]]'` | 对每一段算左右轮速的阶跃指标。 |
| `task_name` | `(self, code: 'int') -> 'str'` |  |
| `tracking` | `(self, t: 'Sequence[float]', y: 'Sequence[float]', ref: 'Sequence[float]', tail_frac: 'float' = 0.2) -> 'Dict[str, float]'` | 跟踪误差: rms / maxabs / tail_mean / tail_rms。 |

### 7.12 `mc.session` — 会话记录

会话记录: 目录、落盘开关、备注、标定报告、历史会话列表。

| 方法 | 签名 | 说明 |
|---|---|---|
| `close` | `(self, extra: 'Optional[dict]' = None) -> 'Optional[str]'` |  |
| `counts` | `(self) -> 'Dict[str, int]'` |  |
| `is_open` | `(属性)` | 会话是否已打开(有 `Session` 对象即视为已开)。 |
| `list_sessions` | `(root: 'str' = 'sessions') -> 'List[Dict[str, Any]]'` | 列出历史会话目录及其 meta(按时间倒序)。供 CLI 与回放选择。 |
| `note` | `(self, text: 'str') -> 'None'` | 写一条备注。会话未开启时先暂存, 开启后补写。 |
| `open` | `(self, name: 'Optional[str]' = None) -> 'Optional[str]'` | 开启会话, 返回目录路径。已开启则直接返回当前路径。 |
| `path` | `(属性)` | 当前会话目录路径(帧/事件落盘处); 未开会话时为 `None`。 |
| `report` | `(self, lines: 'List[str]') -> 'Optional[str]'` | 追加写入 `calib/report.md`。 |

### 7.13 `host.api.calc` — 标定算参(纯函数)

不碰设备、不作副作用, 可脱离硬件单测; 也可用于离线回放。
v2.4 从课程层迁出后独立成模块 —— **能力保留, 与课程无关**。

| 函数 / 类型 | 作用 |
|---|---|
| `theory_track_ratio(...) -> float` | 里程系数理论值(由机械参数算, 默认 ≈62.24) |
| `theory_gyro_ratio(...) -> float` | 陀螺角速率系数理论值(由 ADC 刻度算, 默认 ≈415.8) |
| `solve_ratio(cur, reading, truth) -> float` | 线性标定: 新系数 = 旧系数 × 读数 / 真值 |
| `error_ratio(reading, truth) -> float` | 相对误差(%), 用于「误差 ≤3%」验收线 |
| `StepMetrics` / `step_metrics(t_ms, y, ...)` | 阶跃响应 σ% / ts / tp / rise |
| `StraightMetrics` / `straightness_metrics(t, sl, sr, gy)` | 直行质量: 航向漂移 + 左右轮不对称 |
| `IrErrorStats` / `ir_error_stats(measured, truth)` | 红外标定误差: max/mean/RMS/最差点 |
| `pick_best_position(stats_by_pos)` | 在若干标定位置里选平均误差最小的 |

## 8. 命令行工具

```bash
python -m host.cli <子命令>        # 每个子命令 = 一次 API 调用; --json 输出结构化结果
python -m host.monitor [目标]      # 实时数据仪表盘(标定/回传阶段主用)
```

| 子命令 | 说明 |
|---|---|
| `ports` | 枚举串口 |
| `connect` | 连接(可选开流+录会话+实时状态) |
| `get` | 读单个参数 |
| `get-all` | 读全部参数 |
| `set` | 写参数(可 --save 落 Flash) |
| `rescue` | 卡死救援: 整机软复位(`MCU RESET`, 不必断电; 会打断运动) |
| `calib` | 跑一项标定 / v2.7 起兼管按键菜单远程化动作 |
| `save` | 把参数表写入 Flash(page 115) |
| `params` | 参数读写族(与 registry 的 params.* 逐条对齐) |
| `maze` | 迷宫: 墙图 + 状态(从统一帧读取) + 发车/收车 |
| `monitor` | 实时数据监视器(终端仪表盘) |
| `motor-test` | 电机远程控制实验(运动期间采样统一帧) |
| `datalog` | 数据收集台(远程发车 + 时间同步 + 完整采集) |
| `estop` | 一键急停 / --resume 解锁 |
| `diagnose` | 诊断单个串口 |
| `scan-baud` | 扫描波特率 |
| `replay` | 回放历史会话 |

`host.monitor` 参数: `--baud`　`--hz` 刷新率(默认 10)　`--width` 面板宽度(字符)　`--seconds` 持续多少秒后自动退出(0=一直跑)　`--once` 只打印一次当前快照后退出(脚本用)　`--json` 每行输出一条 JSON, 不画面板　`--no-color` 关闭 ANSI 颜色　`--no-clear` 不清屏, 追加打印(便于留日志)　`--no-stream` 不自动开始拉取(mc.stream.on); 设备需已有人在拉　`--record` 同时开启会话落盘(sessions/)　`--timeout` 等待首帧的秒数　`--expect-safe` 期望设备处于安全测试模式(电机禁用); 缺失该标志时高亮告警
