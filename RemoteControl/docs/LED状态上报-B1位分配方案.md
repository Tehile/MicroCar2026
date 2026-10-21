# LED 状态上报方案（B1 预留位分配）

> 需求：把统一帧状态字节 **B1** 的预留位分配给 4 个 LED，远程档 LED 状态一变就在帧里同步带上。
> 状态：**方案待拍板，未改任何代码**。

## 一、先纠正一处事实

B1（载荷偏移 25）现在的预留**不是 4 位，是 6 位**：

```
偏移 25 状态B1:  bit0 spurtFlag   bit1 goOrBack   bit2-7 预留（6 bit）
```

所以分配 4 位给 LED 之后还剩 2 位可留，不需要从别处挤。

**建议分配**（低位连续、留高位给将来扩展）：

| 位 | 掩码 | 含义 |
|---|---|---|
| bit2 | `0x04` | LED1（PA8） |
| bit3 | `0x08` | LED2（PC9） |
| bit4 | `0x10` | LED3（PC8） |
| bit5 | `0x20` | LED4（PC7） |
| bit6–7 | — | 继续预留 |

语义定为 **1 = 点亮**，与硬件电平解耦（板子是低电平点亮，上位机不该关心这个）。

## 二、LED 在固件里的实际形态（决定了方案怎么选）

`HARDWARE/LED/led.h`：

```c
#define LED1  PAout(8)    #define LED2  PCout(9)
#define LED3  PCout(8)    #define LED4  PCout(7)
#define LED5  PAout(8)    #define LED6  PCout(9)   /* 5-8 只是 1-4 的别名 */
```

- 物理上只有 **4 颗灯**，LED5–8 与 LED1–4 同引脚。
- **低电平点亮**：`ledon(n)` 写 0，`ledoff(n)` 写 1（`led.c:129-161`）。
- **没有任何软件影子变量** —— 全工程都是直接写引脚。

远程档现存的 LED 写点共 6 处：

| 位置 | 作用 |
|---|---|
| `link.c:2793-2796` | `MAZE RUN` 发车瞬间 4 灯齐亮 |
| `link.c:3403/3405` | 时间基准灯（LED4 按 `LINK_TIME_DIV=1000` 做 1 Hz 方波） |
| `link.c:3421-3434` | 发车标记窗口保持 + 释放（`LINK_LAUNCH_MARK_MS=300`） |
| `link_port.c:345-355` | `LP_LedMark()` 同功能封装 |
| `led.c:129-161` | `ledon/ledoff`（比赛档菜单/状态灯走这里） |
| `adc.c:260` | 全灭初值 |

## 三、数据来源：三个候选

| 方案 | 做法 | 侵入面 | 评价 |
|---|---|---|---|
| **A. 读 GPIO ODR**（推荐） | 组帧时读 `GPIOA->ODR` / `GPIOC->ODR`，取反得"亮/灭" | **零**（只改 `link_SendUnified` 一处） | 6 个写点全部自动覆盖，包括将来新增的写点 |
| B. 影子变量 | 加 `u8 ledShadow`，所有写点同步维护 | 改 6 处，其中 `led.c` 是**两档共享文件** | 侵入大、易漏，且会改到比赛档编译单元 |
| C. 新增 `LED` 查询命令 | 上位机轮询 | 小 | 不是"同步传输"，多一轮往返 |

**推荐 A**。理由不只是省事：

- 读引脚反映的是**真实亮灭**，影子变量只反映"代码以为的亮灭"，两者冲突时（比如别处直接写引脚）影子变量会撒谎；
- 工程里绕过 `ledon()` 直接写引脚的地方至少 3 处（发车标记、基准灯、`LP_LedMark`），影子变量方案必须逐个补，漏一个就长期错报；
- 读 ODR 只是一次 APB 读，4 次合计几十 ns，在 1 ms tick 里完全无感。

组帧写法（`link.c` 的 `sb1` 处）：

```c
#ifdef MOUSE_REMOTE
	/* LED1-4 实时状态 -> B1 bit2-5(1=亮)。低电平点亮 ⇒ ODR 位取反。
	 * 读引脚而不设影子变量: 全工程有 6 处直接写这 4 个引脚(见方案文档),
	 * 其中 led.c 还是两档共享文件; 读 ODR 零侵入且反映真实亮灭。 */
	sb1 |= (u8)(((u8)(~(GPIOA->ODR >> 8) & 0x01u) << 2)   /* LED1 = PA8  */
	          | ((u8)(~(GPIOC->ODR >> 9) & 0x01u) << 3)   /* LED2 = PC9  */
	          | ((u8)(~(GPIOC->ODR >> 8) & 0x01u) << 4)   /* LED3 = PC8  */
	          | ((u8)(~(GPIOC->ODR >> 7) & 0x01u) << 5)); /* LED4 = PC7  */
#endif
```

## 四、红线保障（这是本方案最关键的一条）

`link_SendUnified()` 是**两档共用**函数，比赛档 PROTO 5 / FW 216、远程档 PROTO 9 / FW 310。
不加门控就会改比赛档产物 ⇒ 红线 hex `68ad6be2…8141` 直接破。

所以：

- 新增代码**全部裹在 `#ifdef MOUSE_REMOTE` 里**；
- **不碰** `led.c` / `led.h` / `USER/` / `timer.c` / `adc.c`；
- 交付前必须重编比赛档，`sha256` 逐字节比对红线。

## 五、版本号

| 宏 | 现值 | 建议 | 理由 |
|---|---|---|---|
| `LINK_PROTO_VER`（远程） | 9 | **保持 9** | 帧长 30 B、所有偏移都不变；旧上位机只是把 bit2-5 当恒 0 的预留位，仍能解全部旧字段（与 v9 把 SCOPING 位改成 PULLF1 时的处理一致） |
| `LINK_FW_VER`（远程） | 310 | **311** | 位语义变了，上位机据此决定"这 4 位能不能读"；fw<311 时 LED 位按"未知"处理，不显示成全灭 |
| 比赛档 `PROTO/FW` | 5 / 216 | **不动** | 零改动 |

## 六、需要修改的内容（逐文件）

### 固件（仅远程档受影响）

| 文件 | 改动 |
|---|---|
| `REMOTE/link.h` | ① 位打包注释（200-205 行）改成 `bit2-5 LED1..LED4(1=亮) bit6-7 预留`；② 新增 `LINK_B1_LED1 0x04` … `LINK_B1_LED4 0x20` 与 `LINK_B1_LED_SHIFT 2`、`LINK_B1_LED_MASK 0x3C`；③ `LINK_FW_VER 310→311` + 历史记录补一行 |
| `REMOTE/link.c` | `link_SendUnified()` 的 `sb1` 组帧后追加上述门控段（约 8 行）。**其余一行不动** |
| `REMOTE/link_port.c` / `led.c` / `led.h` | **不改** |

### 上位机

| 文件 | 改动 |
|---|---|
| `host/protocol/codec.py` | ① 新增 `STATE_B1_LED1..LED4`；② `UNIFIED_CHANNELS` **末尾追加** `led1..led4`（规则：新增字段一律追加末尾，否则 RingBuffer 历史数据错列）；③ `decode_unified` 加 4 个字段；④ `pack_unified` 同步；⑤ `assert len == 29` → `33` |
| `host/fwprofile.py` | 档位表与 label 里 `310` → `311` |
| `host/contract_test.py` | 位宏对照表加 4 项；往返用例加 `led1..4` |
| `host/v9_pull_verify.py` | ① S0 加"B1 LED 4 位一致"检查；② 201 / 350 行 `29 项` → `33 项` |
| `host/ui/status_panel.py` | 可选：4 个小圆点指示 LED 状态 |

> 注意：`UNIFIED_LEN` 仍是 **30 B**，线上帧长 **38 B** 不变。变的只是 B1 那 6 个预留位里用掉 4 个。

### 文档

- `docs/通信协议规格.md`：B1 位表补 4 行；
- `docs/通信协议v9-统一帧重排方案.md`：新增小节说明 B1 预留位的落位；
- 本文件归档。

## 七、验证计划（真机，全部零风险）

| 步骤 | 操作 | 预期 |
|---|---|---|
| V1 | 重编比赛档 | hex `sha256` 仍是 `68ad6be2…8141`（**不过就不许提交**） |
| V2 | 烧远程档，`TELEM` | 载荷 30 B；33 通道全解出；`led1..4` 存在 |
| V3 | 待命态连取 10 帧 | 4 灯位稳定（读数应等于肉眼所见，人工比对一次） |
| V4 | `ARM` | `led4` 以 1 Hz 翻转（基准灯 `LINK_TIME_DIV=1000`） |
| V5 | `MAZE RUN` | 300 ms 窗口内 **4 位同时变 1**（发车标记），随后回落到常规态 |
| V6 | `MAZE STOP` | 状态随之变化，无残亮 |
| V7 | 掉电重启后 `PING` | fw 显示 311 |

V5 是最有说服力的一条：它把"发车时刻 4 灯齐亮"这个已有物理行为，变成了帧里可读出的事实。

## 八、待拍板

1. **位分配**：按 bit2–5（低位连续、bit6–7 留空）可以吗？还是想用 bit4–7？
2. **数据源**：A 读 GPIO ODR（推荐，零侵入）还是 B 影子变量？
3. **版本号**：`FW 310→311`、`PROTO 保持 9`？
4. **是否要上位机可视化**（`status_panel` 加 4 个灯指示）？

## 九、实施记录（2026-10-21 落地）

**拍板结果**：位分配 **bit2–5**、数据源 **A（读 GPIO ODR）**、版本 **FW 310→311 / PROTO 保持 9**、
上位机页面指示**本轮不做**（通道先上线，页面后续按需接）。

### 改了什么

| 文件 | 改动 |
|---|---|
| `code_competition/REMOTE/link.h` | `LINK_FW_VER 310→311`；位打包明细补 bit2–5；历史版本记录加 311 条；新增 `LINK_B1_LED1..4`（0x04/0x08/0x10/0x20） |
| `code_competition/REMOTE/link.c` | `link_SendUnified()` 里 `sb1` 组帧后加 **11 行 `#ifdef MOUSE_REMOTE` 门控段**，其余一行未动 |
| `host/host/protocol/codec.py` | 4 个位宏；`UNIFIED_CHANNELS` **末尾**追加 `led1..led4`，断言 29→**33**；`decode_unified` / `pack_unified` 同步 |
| `host/host/registry.py` · `host/host/__init__.py` | API 2.12.0→**2.13.0** |
| `host/host/fwprofile.py` | 档位标签 fw 310→311（版本号本身是从 `link.h` 现读的，无硬编码） |
| `host/contract_test.py` | 位宏对照表加 4 项；新增"LED 位不压 spurtFlag/goOrBack"断言；`_payload_unified` 用交替值走这 4 位 |
| `host/v9_pull_verify.py` | `EXPECT_FW=311`；S0 加状态B1 六位比对 + 通道表 33 项 + "LED 追加在末尾"；S2 验 `led*=0/1` |

固件实现（`link_SendUnified`）：

```c
#ifdef MOUSE_REMOTE
	sb1 |= (u8)(((u8)(~(GPIOA->ODR >> 8) & 0x01u) << 2)    /* LED1 = PA8 */
	          | ((u8)(~(GPIOC->ODR >> 9) & 0x01u) << 3)    /* LED2 = PC9 */
	          | ((u8)(~(GPIOC->ODR >> 8) & 0x01u) << 4)    /* LED3 = PC8 */
	          | ((u8)(~(GPIOC->ODR >> 7) & 0x01u) << 5));  /* LED4 = PC7 */
#endif
```

反汇编核对（`fromelf --text -c`，远程档 axf）——四位都在，移位正确：

```
0x0800390e  LDR  r0,[pc,#840] ; = 0x4001080c      ; GPIOA->ODR
0x08003910  LDR  r0,[r0]                          ; 读 ODR
0x08003914  BIC  r0,r1,r0,LSR #8                  ; ~(ODR>>8)&1  = PA8
0x08003918  LSLS r0,r0,#2                         ; -> bit2
0x0800391c  LDR  r1,[r1,#0xc]                     ; GPIOC->ODR(基址 0x40011000 + 0x0C)
0x08003920  BIC  r1,r2,r1,LSR #9  / ORR ...,LSL #3 ; PC9 -> bit3
0x0800392c  BIC  r1,r2,r1,LSR #8  / ORR ...,LSL #4 ; PC8 -> bit4
0x08003938  BIC  r1,r2,r1,LSR #7  / ORR ...,LSL #5 ; PC7 -> bit5
0x08003940  ORR  r8,r0,r8                          ; 并入 sb1
```

### 构建与离线验证

| 项 | 结果 |
|---|---|
| Target 1（比赛档） | 0 Error / 38 Warning，ROM 82040 / RAM 20480，hex `68ad6be2…8141` ⇒ **红线 PASS，逐字节相同** |
| Target_Remote | 0 Error / 27 Warning，ROM **96624 B**（96592 → +32）、RAM 13560 不变，hex `e784a8c4…` |
| `contract_test` | **263/0**（原 262，+1 为新增的 LED 位不重叠断言） |
| `v9_pull_verify --selftest` | **19/0**（含状态B1 六位一致、通道 33、LED 追加末尾、124 组往返） |
| 其余套件 | basic_cmd 26/0 · pull 42/0 · stream 78/0 · monitor 32/0 · selftest 45/0 · maze 48/0 · evt 判据全过 |

### 真机待办（对应 §七）

V1/V2 的离线部分已由上面两张表覆盖；剩下的都要上板：**重烧 `Target_Remote`** 后跑
`python host/v9_pull_verify.py COM7 --no-ladder`（S1 会因 fw 311 与旧片 310 区分开），
再按 V3–V5 人工比对：待命 4 位稳定 → `ARM` 后 led4 按 1 Hz 翻转 → `MAZE RUN` 的 300 ms
发车标记窗口内 4 位同时为 1。
