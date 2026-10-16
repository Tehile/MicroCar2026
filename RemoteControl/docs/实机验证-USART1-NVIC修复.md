# USART1 NVIC 未使能 —— 实机诊断与修复记录

> 日期：2026-10-16　目标：`RemoteControl` 远程档（`Target_Remote`，fw 300 / MAZETYPE 16）
> 结论：**一行代码的缺失**，导致上位机与电脑鼠之间的整条字节链路完全静默。
> 状态：已修复、已重编、已复测通过；比赛档红线逐字节不变。

---

## 1. 故障现象

远程化移植写完、编译无错、烧录成功，但上位机**收不到任何字节**：

| 尝试 | 结果 |
|---|---|
| `front_check.py COM7 -n 3`（PING + TELEM） | 0 字节，端口正常打开 |
| 原始字节探测（自行拼 CRC 发 `PING`/`TELEM`） | 静默期 0B，发后仍 0B |
| 换波特率（9600/57600/230400）、换 DTR/RTS 组合 | 全部 0B |
| 换端口（COM4/COM5 蓝牙 SPP） | 无设备 |

关键点：**Tx 侧完全正常**——板子能收到、能进 RXNE，只是数据永远到不了 CPU。

## 2. 诊断过程（只读）

### 2.1 先确认"是哪个档"

J-Link 读 Flash 首字（栈顶）：

| 档位 | 首字 | 板上实测 |
|---|---|---|
| `Target 1`（比赛档） | `0x20005000` | — |
| `Target_Remote`（远程档） | `0x200034F8` | **`200034F8`** ✅ 远程档烧对了 |

（烧错档的典型症状同样是"发 PING 无 PONG"，因为比赛档根本没有 `LINK_Init`。）

### 2.2 MCU 是活的，只是不收串口

| 观测 | 值 | 判读 |
|---|---|---|
| `PC` | `0x080063D4`（落在 `delay_ms`，`main` 调用内） | 不在复位向量，程序正常跑 |
| `gyro0` | `1811`（≠ `-1`） | 陀螺零偏标定已完成 |
| `linkTimeMs` | `312100` ms 且持续递增 | SysTick + `LINK_Tick` 一直在跑 |
| `USART1_BRR` | `0x0271` → USARTDIV 39.0625 | **115200 正确** |

### 2.3 ★ 决定性证据：寄存器对比

```
USART1_SR   = 0x000000F8   RXNE=1 且 ORE=1   ← 数据到了，没人读，已溢出
USART1_DR   = 0x50         = 字符 'P'        ← 正是上位机 PING 的首字节！
USART1_CR1  = 0x212C       UE|RE|TE|RXNEIE=1 ← USART 侧中断已开
USART1_BRR  = 0x0271       115200
GPIOA_CRH  = 0x888448B3   PA9=AF_PP(TX)、PA10=浮空输入(RX) ✅
SysTick_CTRL= 0x00010003   ENABLE|TICKINT ✅
NVIC_ISER0  = 0x00000000
NVIC_ISER1  = 0x00000000   ← USART1 的使能位是 0！
```

**链路是通的，数据真的到了 USART1；卡在 NVIC 层。**

### 2.4 反向证明

用 J-Link 手工向 `NVIC_ISER1` 写 `0x20`（IRQn 37 → bit5），端口立刻回：

```
PONG 8 300 16 1024 POR
```

⇒ **唯一缺口就是这个使能位。**（该写入在复位后失效，反证固件里确实没有它。）

## 3. 根因

`SYSTEM/usart/usart.c:111` 的注释写明：

> USART1 RX 的 **NVIC 使能与优先级统一由 `LINK_Init()` 配置**

而 `uart_init()` 只开了 USART 侧的 `RXNEIE`（`CR1` bit5）。这条"外包"在 `LINK_Init()` 里**只接了一半**：

```c
NVIC->IP[USART1_IRQn] = 0x00;   /* 优先级：写了 */
/* NVIC 使能位：漏了 */
```

于是：RXNE 在 USART 里挂着 → 中断不进 CPU → DR 里的字节无人读 → 后续字节全部触发 ORE 被丢弃。

> 补充：`MY_NVIC_Init()`（`SYSTEM/sys/sys.c:63`）其实是可用的，但全工程无人调用；且它会顺带改写 `SCB->AIRCR` 的全局优先级分组，与 `LINK_Init` 显式设 `IP` 的风格冲突，故不采用。

## 4. 修复

文件：`code_competition/REMOTE/link.c`，函数 `LINK_Init()`，`NVIC->IP[USART1_IRQn] = 0x00;` 之后。

```c
NVIC->ISER[USART1_IRQn >> 5] |= (u32)1 << (USART1_IRQn & 0x1F);
```

两处刻意的写法选择：

- **`|=` 而非 `=`**：`ISER` 一个字覆盖 IRQn 32..63，直接写会波及同字内其它中断。
- **直接写寄存器而非 `MY_NVIC_Init()`**：与上一行 `IP` 赋值同风格，也不改全局优先级分组。

紧随其后补一段"丢弃上电残缺字节"（清 `USART_RX_STA` + 读 SR/DR 复位 ORE），避免 `uart_init()` 到使能之间残留的半截命令让行解析错位。

**为什么安全**：`link.c` 是整文件 `#ifdef MOUSE_REMOTE` 门控，`Target 1`（比赛档）根本不编译它。

## 5. 验证结果

### 5.1 编译

```
双档全量重编（build_competition_modes.py）
  Target 1        0 Error  ROM 82040  RAM 20480
  Target_Remote   0 Error  ROM 96512(+40B)  RAM 13560
  比赛档 hex = 68ad6be2272818269d6aac78e5356d9bb91036ddeaaa82c4ec3754a8a0948141
  → 红线 PASS（与修复前逐字节相同）
```

远程档 ROM +40B 恰为新增指令与注释剥离后的合理增量。

### 5.2 复测（冷上电，不经 J-Link）

重烧后**直接**探活，串口自愈：

```
PONG 8 300 16 1024 POR
TELEM 稳定回帧，周期约 8 ms
```

首帧 RTT ≈ 286~330 ms（含开串口 + `request_sample` 建链的一次性开销，与 `front_check` 历史行为一致，非本次引入）。

### 5.3 自动化冒烟测试

`host/basic_cmd_smoke.py`（本轮新增），覆盖 T1–T7，**全程不发车**：

| 段 | 内容 |
|---|---|
| T1 | `PING` 指纹：proto=8 / fw=300 / mazetype=16 / txbuf / rst |
| T2 | `TELEM` 5/5 收帧 + 稳态 RTT + 49 字节 payload + 65 字段完整性 + **车辆静止断言** |
| T3 | `GET ALL` 全量参数回读，校验 id 全在 `PARAM_IDS` 内、生产命名空间 24 项齐 |
| T4 | `GET <name>` 单点抽查（TRACKL / GYRORATIO / ENCANGLE） |
| T5 | `SETQ` + 回读校验 + **恢复原值**（`IRTHRFNEAR`，`link_ParamApplyOne` 有真实落点） |
| T6 | 错误注入：未知参数必须回 `ERR_NAME`，且不污染其它参数 |
| T7 | `MCU RESET` 软复位后链路复通（`--reset` 启用） |

安全护栏：`FORBIDDEN = {MOVE, MAZE, CAL, ARM, ESTOP, STOP, PARAMS, SAVE}`，
`build()` 逐条 `assert`，运动/擦写类命令**构造不出报文**。

### 5.4 离线自检（不接设备）

`python basic_cmd_smoke.py --selftest` → **PASS 24 / FAIL 0**，验证：

- 下行报文格式与固件 `link_HandleLine` 一致（`CMD #seq*XXXX`，`*` 后 4 位 hex，CRC 自洽）；
- 上行正则吃得下固件真实拼装的 `PONG` / `ACK <seq> <status> [a b c]` / `P <id> <q>`；
- Q16.16 往返无损（53.2015 / 123.0 / 0.01 / 0.2 / −3.5）；
- 安全护栏拦住 9/9 运动类命令，7 条白名单命令正常构造；
- `decode_unified()` 65 字段、`UNIFIED_LEN = 49`。

> 这个模式的价值：设备不在手上时也能证明"脚本没写错"，避免插上后把**脚本 bug** 误判成**固件 bug**。开发过程中它确实抓出了两处我自己的断言错误。

## 6. 结论与教训

1. **故障定位的关键一跃**：从"串口没反应"到"NVIC 使能位为 0"，靠的是**读寄存器而不是猜**。`USART1_DR` 里停着上位机发来的 `'P'`，这一个字节就锁定了"数据到了但没人读"。
2. **注释里的"外包契约"必须有实现兜底**。`usart.c:111` 把 NVIC 配置责任推给 `LINK_Init()`，后者只做了优先级——这类"半实现的外包"最难查，因为两边代码单独看都合理。
3. **"完全静默"往往不是配置错，而是中断没进 CPU**。这类问题的判据是 `RXNE=1` 且 `DR` 有残留字节。
4. **反向证明是铁证**：手工置位使能位后串口立刻复活，比任何推理都有力。
5. **门控设计的价值兑现**：`link.c` 整文件 `#ifdef MOUSE_REMOTE`，改远程档功能而比赛档红线逐字节不变——这正是单工程双 Target 方案的价值。

## 7. 关联文件

| 文件 | 说明 |
|---|---|
| `code_competition/REMOTE/link.c` | 修复点（`LINK_Init`，本轮唯一固件改动） |
| `code_competition/USER/Remote/TEST.hex` | 远程档产物（fw 300） |
| `code_competition/USER/TEST.hex` | 比赛档产物，红线 `68ad6be2…8141` |
| `host/basic_cmd_smoke.py` | 实机基础指令冒烟测试（本轮新增） |
| `build_competition_modes.py` | 双档全量重编 + 红线校验 |

## 8. 尚未验证（需设备回来）

- T3–T7 需要真机串口；本轮仅离线自检通过。
- 陀螺零偏标定要求**上电时车绝对静止**（`hardware_init()` 是阻塞死循环），否则串口全程静默——与本次故障无关，但症状相同，容易混淆。