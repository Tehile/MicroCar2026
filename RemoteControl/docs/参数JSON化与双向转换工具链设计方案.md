# 参数 JSON 化与「固件源码 ⇄ 参数表」双向转换工具链 —— 设计方案 **v2.0**

> 状态：**设计稿 + 可运行只读/写回原型（`tools/paramkit/` + `tools/paramgen.py`）**
> 适用基线：`code_competition/`（新基线，fw 300 / MAZETYPE 16）+ `host/` v2.11.0
> 取数基线：`git rev-parse --short HEAD` = **`1ea6e40`**（本轮未改任何固件字节）
> 关联文档：`docs/参数名称-功能-调整表.md`、`docs/比赛固件远程化移植方案.md`、`docs/算法详解.md` §4
> v1 存档：本文档的上一版（纯设计稿，无实现、无实测数据）

---

## 0. 本轮相对 v1 的四处收敛

| # | v1 的写法 | v2 的本轮依据 | 收敛结果 |
|---|---|---|---|
| 1 | "JSON 与固件**只允许相等**，不相等就报错" | 用户：**存储数据和固件中不一定是一致的** | 引入 `fw`（固件只读快照）/ `store`（人工受管值）/ `policy`（三方策略）**三件套**；分歧不再当错误，而是**被分级管理的状态**（见 §3.2） |
| 2 | `csyntax.py`：写一个"无依赖的 C 词法扫描器" | 用户：**建议正则表达式**；且 14 个文件是 GBK+CRLF | 改为 **ASCII 字节正则库**（`regexlib.py`，9 条正则），在 `bytes` 上直接匹配，**全流程不解码** ⇒ 编码/行尾天然零风险（见 §4） |
| 3 | 参数默认值取"文档/注册表"，固件值只做对账 | 用户：**数据以现在新固件为准** | `fw.value` 一律由**当前固件源码**抽出（受限求值 + 取整还原），文档值降级为 `doc` 参考；实测与文档的偏差全部落 `latent[]`/`notes[]`（见 §9/§10） |
| 4 | v1 只有方案，落地路线 G0–G4 待批 | 本轮要求"写代码用于读写" | **已实现并跑通** `extract / apply / check / audit / render` 五个子命令；幂等、写回、定点三关全绿（见 §1/§7） |

另外 v1 的 4 个待拍板点已由本轮规则蕴含（D1 双向可写、D2 写值域不写结构、D3 只登记不改、D4 版本 v2.0）。

---

## 1. 结论先行：本轮已经跑通的四组证据

| 证据 | 命令 | 结果 |
|---|---|---|
| **① 抽取完整** | `python tools/paramgen.py extract --accept` | **41 项**（= 固件 `LINK_P_NUM` 远程档 41），无一漏一项 |
| **② 自动仲裁** | `python tools/paramgen.py audit` | **A 固件赢 15 项 / B 镜像赢 8 项 / C 无人读 18 项**（15+8+18 = 41）<br>*（2026-10-16 修正：`IRKALSW` 由 B 改 A，原为 14/9/18；见 §2.1 脚注）* |
| **③ 幂等** | `apply`（JSON 与固件一致的场景） | **将改 0 行 / 0 文件** —— 写回是"定点"的 |
| **④ 写回 + 定点** | 改 JSON 的 4 个值 → `apply --commit`（对临时副本） | 只动 **3 行 / 3 文件**，且只替换"值"：`double turn_kp=4,turn_delta…` → `double turn_kp=4.5,turn_kd=3.5;`（注释/tab/CRLF 原样）；重新 `extract` 后 4/4 项 `fw.value == store.value` ✅ |
| **⑤ 零副作用** | `git status --porcelain -- code_competition host` | **无任何改动**（写回实验全部在 `%TEMP%` 的副本上做） |
| **⑥ 对账** | `python tools/paramgen.py check` | `error=0 warn=0 info=0`，**退出码 0** |

> ③ ④ 的细节：`apply` **默认 `--dry-run`**，落盘必须显式 `--commit`；落盘走"先写 `.tmp` 再 `os.replace`"；
> 写前校验"锚点行指纹"，写后只改值域切片。所以"能不能安全地反向写固件"这个问题，本轮已经从**论证**变成**实测**。

---

## 2. 本轮审计的硬结论

### 2.1 参数其实分三类，只有一类"改了有用"

开机顺序（`REMOTE/link.c:3315-3319`）是固定的四步：

```
LP_SyncIn()          固件全局量 → 镜像        （只有 A 类字段会被这一拍刷新）
link_ParamFromSys()  镜像      → linkParam[]  （上表槽）
link_CalibLoad()     Flash     → linkParam[] → linkParamApplyOne() → 镜像（magic/ver/cnt/crc 全合法才认）
LP_SyncOut()         镜像      → 固件全局量    （**每 tick 都跑**）
```

于是每条参数有一个**启动仲裁结果**，可由"赋值边可达闭包"自动判定（§5）：

| 类 | 数量 | 含义 | 改谁才有用 | 例 |
|---|---:|---|---|---|
| **A 固件赢** | **15** | 镜像被 `LP_SyncIn` 从固件刷新，再被推回 | **改固件常量**（或 `SETQ`/`SAVE` 走 Flash） | `TRACKL/R`、`SPEEDL/R`、`GYRORATIO`、`ENCANGLE`、`WALLDIST`、`IRTHRWALL`、`TURNDELAY`、`SPDSEARCH`、`SPDTURN`、`GSKP`、`TURNP`、`TURND`、**`IRKALSW`** |
| **B 镜像赢** | **8** | 只有"镜像 → 固件"出边：固件侧同名常量**每 tick 被覆盖** | **改 `link.c:119-127` 的镜像初值**；改固件常量**无效** | `IRKALQ`、`IRKALP`、`MKPL/MKIL/MKDL/MKPR/MKIR/MKDR` |
| **C 无人读** | **18** | 两侧都没有：可 `GET`/`SETQ`/`SAVE`，但**本固件无人读** | 谁都不用改（要改就得先"接线"） | `IRTHR45/CRIT/FLO/MID/HI/FNEAR`、`IRFRONTMODE`、`BATT*`、`TRACKCOORD`、`DISF60`、`PTKP/KI/KD` |

> ⚠️ **2026-10-16 修正**：`IRKALSW` 原列 B 类，**有误**。它虽然走 `irCfg.kalOn` 这个影子字段，
> 但真正的固件量 `kalmanswitch` 被 `LP_SyncIn:91` **回读**（`LP_SyncOut:226` 写、下一拍读回，
> 自洽回环）⇒ 改固件常量有效、`SETQ` 也有效 ⇒ 应为 **A 类**。修正后：**41 = A15 + B8 + C18**。
> 根因：仲裁只检查了"镜像符号是否被 SyncIn 写"，没检查"sink 是否被 SyncIn 回读"（核查报告 I4）。

> **B 类是本方案存在的最大理由。** 它不报错、不告警，只是"你在 `timer.c:75` 把电机 PID 从 3 改成 5，
> 编译通过、烧录成功、真机行为一个字节都没变"。

### 2.2 三个**行为级**发现（本轮新查，均未改固件）

> ⚠️ **2026-10-16 修正**：本节"发现 1"原称 *`kalmanswitch` 被置 0*，**该句有误**，已按源码更正（见下）。
> 另 `IRKALSW` 的仲裁分类由 **B 改为 A**（修正后 41 = A15 + B8 + C18）。详见 `docs/参数JSON化-一致性核查报告.md`。

#### ★ 发现 1：远程档的卡尔曼滤波被"静默短路"（Q/P 被清零，**开关本身正常**）

```
adc.c:144-147   double Qf1=0.01, Pf1=0.2;   ← 比赛档的开机值
adc.c:151       u8 kalmanswitch=1;          ← 比赛档的开关开机值
link_port.c:40  IrCfg irCfg;                ← 镜像结构体在 BSS, 初值全 0
link_port.c:227 Qf1 = Qf2 = Ql = Qr = (double)irCfg.kalQ / 10000.0;   ← 每 tick 跑
link_port.c:228 Pf1 = Pf2 = Pl = Pr = (double)irCfg.kalP / 10000.0;   ← 每 tick 跑
link_port.c:91  irCalib.kalmanswitch = kalmanswitch;                  ← 每 tick 跑(固件→镜像)
link_port.c:226 kalmanswitch = irCalib.kalmanswitch;                  ← 每 tick 跑(镜像→固件)
link.c:732      irCalib.kalmanswitch = irCfg.kalOn;                   ← 仅 SETQ/CalibLoad 路径
```

- `irCfg.kalQ/kalP` **没有任何初始化式**（`link_port.c:40-50` 全是裸定义），
  `LP_SyncIn` 也**不刷新**它们 ⇒ 恒为 **0** ⇒ 每 tick 把 `Qf1..Qr = 0`、`Pf1..Pr = 0`，
  而卡尔曼增益 `kf = cov/(cov+P)`，`Q=P=0` 时 `cov` 不变、`kf ≡ 1` —— **滤波器退化成直通**。
- 但 `kalmanswitch` **恒为 1，并没有被关掉**：`LP_SyncOut:226` 写的值下一拍会被 `LP_SyncIn:91`
  原样读回（自洽回环）⇒ 改 `adc.c:151` 有效、`SETQ IRKALSW` 也有效。
  `irCfg.kalOn` 只是 `SETQ` 的临时入口，下一拍就被覆盖。

> **同一台车：烧比赛档 = 卡尔曼滤波（Q=0.01/P=0.2），烧远程档 = 滤波开但退化成直通（Q=P=0）。**
> 真机可验（不需要发车）：`GET IRKALQ` 在远程档应回 **0.0**（文档写 100）。
> ⚠️ `GET IRKALSW` 也会回 **0**，但那只是**参数表**的值（取自 `irCfg.kalOn` 这个 BSS 影子），
> **不代表固件开关被关**（固件实际是 `adc.c:151` 的 1）—— 这本身是一条独立的不一致（见核查报告 I5）。
> 这条已写进 JSON 的 `notes[]`（`IRKALQ/IRKALP/IRKALSW`）与 `latent[]`。

#### ★ 发现 2：远程档的电机 PID 与比赛档**是两套值**，且比赛档的整定被吃掉

```
HARDWARE/TIMER/timer.c:75   double kpl=3,kil=2,kdl=0.1,kpr=3,kir=2,kdr=0;   ← 菜单前的默认
USER/user.c:80-81           parameter_search(): kpl=13,kil=0.74,kdl=0.9 …   ← 真正跑的搜索期整定
USER/user.c:91-92           parameter_spurt():  kpl=13,kil=0.7,kdl=0.8 …    ← 冲刺期整定
REMOTE/link.c:124-125       linkMtrKpL=10.75, linkMtrKiL=0.55 …             ← 远程档镜像初值
```

- **比赛档**：`test.c:717 parameter_search()` / `parameter_spurt()` 决定电机 PID（搜索期 13/0.74/0.9）；
- **远程档**：`LP_SyncOut` 每 tick 把 `kpl..kdr` 写成镜像值 `10.75/0.55/0.55`，
  ⇒ **`parameter_search()/parameter_spurt()` 设的电机 PID 被立刻覆盖**，两档性格不同。

#### ★ 发现 3：`TRACKCOORD` 是"同名不同物"

```
REMOTE/link.h:364   #define LINK_P_TRACKCOORD 39   /* 默认 145 */
REMOTE/link_port.h:264   u8 trackCoord;            ← 镜像字段（影子，无消费者）
HARDWARE/TIMER/timer.c:52  const u8 trackcoor=127;  ← 固件真正在用的常量
HARDWARE/TIMER/timer.c:885/886/909/910   settrack(trackcoor);   ← 真实消费点
```

链路参数（`sysCfg.trackCoord`）与固件常量（`trackcoor`）**只差一个字母**，
但**互不相通**：`SETQ TRACKCOORD` 改了前者，后者恒 127。

### 2.3 文档/注册表 vs 固件：实测偏差（`fw` 与 `doc` 的差值）

| 参数 | 文档/`link.h` 注释/host 注册表 | **固件实测（本轮为准）** | 值来源 |
|---|---|---|---|
| `TRACKL` | 53.2015 | **55.0855624** | `USER/user.c:18` `54.9208*1.003` |
| `TRACKR` | 53.278 | **50.9356** | `USER/user.c:18` `55.1848*0.923` |
| `SPEEDL` | 0.532013（文档称 ≡ TRACKL/100） | **0.56169** | `USER/user.c:20` `0.6241*0.9`（**派生关系不成立，差 2%**） |
| `SPEEDR` | 0.532776 | **0.56439** | `USER/user.c:20` |
| `ENCANGLE` | 67 | **73**（表达式 73.71，`u16` 截断，−0.96%） | `USER/user.c:16` `81*0.91` |
| `GYRORATIO` | 399 | **399**（399.727 截断） | `USER/user.c:17` |
| `WALLDIST` | 42 | **40** | `USER/user.c:21` |
| `IRTHRWALL` | 160 | **130** | `HARDWARE/TIMER/timer.c:50` |
| `IRTHRFNEAR` | 80 | **0**（镜像 BSS）／字面量 **65** | `timer.c:503` |
| `TURNDELAY` | 16.5 | **5** | `UTILITY/utility.c:21`（`u8`） |
| `SPDSEARCH` | 95 | **40** | `HARDWARE/TIMER/timer.c:54` |
| `SPDTURN` | 60 | **30** | `HARDWARE/TIMER/timer.c:55` |
| `TURNP` / `TURND` | 5.0 / 4.5 | **4 / 4** | `HARDWARE/TIMER/timer.c:61` |
| `MKPL..MKDR` | 10.75/0.55/0.55/10.20/0.5/0.5 | **同左** —— 但**固件常量是 3/2/0.1/3/2/0**（B 类覆盖） | `REMOTE/link.c:124-125` vs `timer.c:75` |
| `PTKP/KI/KD` | 2.5 / 0.01 / 0.1 | **同左，但无消费者** | `REMOTE/link.c:123` |

> **不改这些值**——它们多半就是赛场整定的结果。工具的作用是**让偏差可见、可 diff、可追溯**。

---

## 3. 数据模型 v2：`tools/params/params.competition.json`

### 3.1 顶层

```jsonc
{
  "schema_version": 2,
  "profile": "competition",
  "generated": { "at": "...", "by": "tools/paramgen.py extract", "git_rev": "1ea6e40",
                 "firmware_dir": "code_competition" },
  "firmware": { "dir": "code_competition", "fw_ver": 300, "proto_ver": 8, "mazetype": 16,
                "link_p_num": { "remote": 41, "competition": 24 } },
  "wire":  { "encoding": "Q16.16", "scale": 65536, "page": 115, "page_addr": "0x0801CC00" },
  "redline": { "hex_sha256": "68ad6be2…8141", "rom": 82040, "ram": 20480 },
  "groups": [ /* calib | ir | power | motion */ ],
  "params": [ /* §3.2 */ ],
  "gaps":   [ /* 工具没能定性的问题(现在只剩 4 条 info) */ ],
  "changelog": [ /* 每次有意的改值必须留痕 */ ]
}
```

### 3.2 单条记录：`fw` / `store` / `policy` 三件套（本版核心）

```jsonc
{
  "name": "TURNP", "id": 45, "macro": "LINK_P_TURNP", "macro_suffix": "TURNP",
  "group": "motion", "kind": "int",

  "wire": { "slot": 30, "qexpr": "link_D2Q(pidGain.turnP)",
            "mirror": "pidGain.turnP", "case_present": true },

  "arbitration": { "class": "A", "sink": "turn_kp",
                   "evidence": "turn_kp←REMOTE/link_port.c:251" },

  "fw": {                       // ← 固件只读快照。extract 整体重写, 人工不许改
    "value": 4, "init": "4", "raw_value": 4, "quantize": "none", "loss": 0.0,
    "source": "HARDWARE/TIMER/timer.c",
    "decl": { "file": "HARDWARE/TIMER/timer.c", "line": 61, "type": "double",
              "const": false, "sha16": "…" }
  },

  "anchors": [                   // ← 写回锚点(§4.3)
    { "kind": "effective", "style": "member", "file": "HARDWARE/TIMER/timer.c",
      "line": 61, "symbol": "turn_kp", "ctype": null, "sha16": "…",
      "note": "开机生效值 = 此声明" }
  ],
  "latent": [                    // ← 人工: "看起来该改但改了没用"的地方
    { "file": "UTILITY/utility.c", "line": 1162, "symbol": "turn_kp", "value": "8",
      "effect": "SPURT_GainSet* 冲刺路径覆写且不恢复(远程档不进冲刺)" }
  ],

  "decls_all": [                 // ← 双档门控成对声明(自动发现)
    { "file": "USER/user.c", "line": 16, "gate": "MOUSE_REMOTE",  "first_declarator": true },
    { "file": "USER/user.c", "line": 23, "gate": "!MOUSE_REMOTE", "first_declarator": true }
  ],
  "consumers":  [ /* 真实消费点(排除所有声明行) */ ],
  "overwrites": [ /* 运行期覆写点(自动扫出) */ ],

  "store":  { "value": 4, "expr": "4", "state": "in-sync", "why": "", "since": "" },
  "policy": "apply",             // pinned | apply | report

  "doc": { "unit": "", "decimals": 3, "doc": "转弯P增益; …", "origin": "24",
           "func": "转弯角度矫正 P 参数", "tune": "PID 调试方法", "macro_comment": "pidGain.turnP 转弯P增益 默认 5.0" },
  "notes": [], "history": []
}
```

**`policy` 三态（取代 v1 的"必须相等"）**

| `policy` | `check` 遇到 `store ≠ fw` 时 | `apply` 行为 | 用途 |
|---|---|---|---|
| `pinned` | **error** | 拒绝写 | 红线相关、绝不从 JSON 侧改的值 |
| `apply` | `INFO`（"待写回"） | **会写**（这是唯一的写入口） | 正常的可调参数 |
| `report` | `INFO`（需带 `store.why`，否则 error） | 跳过 | C 类影子 / 无锚点的 B 类：**允许长期不一致**，只做登记 |

`store.expr` 保留**源码原表达式**（`54.9208*1.003`）。这是幂等的关键：
`apply` 写回的是"原样的表达式"而不是算出来的 `55.0855624`，所以**一份未修改的 JSON 写回去 = 0 字节变化**。

### 3.3 字段所有权（谁写谁，铁律）

| 字段 | `extract` | 人工 | 说明 |
|---|---|---|---|
| `fw` / `arbitration` / `wire` / `anchors(effective)` / `decls_all` / `consumers` / `overwrites` / `gaps` | **重写** | ✗ 不许改 | 全自动，改了下次 extract 就没了 |
| `store` / `policy` / `latent` / `notes` / `history` / `doc` | **保留** | ✔ 主战场 | 调参、写"为什么"、钉策略都在这里 |

---

## 4. 正则库：为什么"正则够了"

### 4.1 前提：只在 **bytes** 上匹配

`code_competition/` 里 14 个文件是 **GBK+CRLF**，其余是 UTF-8/ASCII。
本工具**所有正则都是纯 ASCII 模式**，直接作用在 `bytes` 上 ⇒
**不需要编码探测、不可能写坏中文注释、CRLF 天然保留**（`split(b"\n")` 保留行内 `\r`）。

只有"给人看"的时刻（打印 diff / 落 JSON / 生成表格）才 `decode('gbk','replace')`。

### 4.2 九条正则（`tools/paramkit/regexlib.py`）

| # | 名字 | 模式（示意） | 用途 |
|---|---|---|---|
| R1 | `RE_COND` | `^[ \t]*#[ \t]*(if\|ifdef\|ifndef\|else\|elif\|endif)\b(.*)$` | 预处理区域跟踪（判 `MOUSE_REMOTE` 分支） |
| R2 | `RE_PM_DEFINE` | `^[ \t]*#[ \t]*define[ \t]+LINK_P_([A-Z0-9_]+)[ \t]+(\d+)[ \t]*(?:/\*(.*?)\*/)?[ \t\r]*$` | 参数宏 + id + 行尾注释 |
| R2' | `RE_PM_NUM` | `…#[ \t]*define[ \t]+LINK_P_NUM[ \t]+(\d+)` | 两档表容量 |
| R3 | `RE_DECL_LINE` | `^[ \t]*(const[ \t]+)?(u8\|u16\|…\|double)[ \t]+([^;]+);` | 找带初值的声明行 |
| R3' | `RE_DECLARATOR` | `(?:^\|,)[ \t]*([A-Za-z_]\w*)[ \t]*=[ \t]*([^,;]+)` | **一行多声明符**（`double turn_kp=4,turn_kd=4;`） |
| R4 | `RE_FUNC_SIG` | `^[ \t]*(?:static[ \t]+)?[A-Za-z_][\w \t\*]*\b(NAME)[ \t]*\([^;]*\)[ \t\r]*$` | 定位函数体（`LP_SyncIn` / `LP_SyncOut` / `link_ParamApplyOne`） |
| R5 | `RE_ASSIGN_SPLIT` | `(?<![=!<>+\-*/])=(?!=)` | 切赋值（**避开 `==` `!=` `<=` `>=`**） |
| R7 | `RE_SLOT_ID` / `RE_SLOT_Q` | `linkParam\[(\d+)\]\.id[ \t]*=[ \t]*(LINK_P_…);` / `…\.q[ \t]*=[ \t]*(.+?);` | 表槽 ↔ 宏 ↔ 镜像端 |
| R7' | `RE_CASE_PM` | `^[ \t]*case[ \t]+(LINK_P_…)[ \t]*:` | 反查"加了 id 忘了搬" |
| R8 | `value_span(sym)` | `(?<![A-Za-z0-9_.])SYM[ \t]*=[ \t]*([^,;\r\n]+)` | **写回用**：定位"值" |
| R9 | `line_anchor(sym,ctype)` | `^([ \t]*(?:const[ \t]+)?TYPE[ \t]+SYM[ \t]*=[ \t]*)([^,;\r\n]+)(.*)$` | **写回用**：验证"这是声明行" |

### 4.3 写回策略：行指纹 + 值域切片（三条护栏）

```
1) 行指纹护栏   锚点存 sha16 = sha256(该行原始字节)[:16]
                写前必须相等 ⇒ "文件已被人改过还按老行号写" 直接报错
2) 单行唯一     value_span(sym) 在**该行代码段**内必须恰好命中 1 次
3) 只切值域     用切片拼接: code[:m.start(1)] + 新值 + line[m.end(1):]
                ⇒ 缩进 tab / 行尾中文注释 / CRLF 一个字节都不动
```

还有一条实测踩出来的细节：`USER/user.c:18` 的行尾注释里**把整条声明又抄了一遍**
（`…; //编码器距离系数  //  TrackRatioL=54.9208*1.003,TrackRatioR=…`），
所以匹配前必须**先截掉第一个 `//` 之后的注释区**（`code_span()`），否则"唯一性"永远不成立。

### 4.4 能力边界（认不出就不动）

**不做**：宏展开、`#if` 表达式求值、跨行表达式、函数指针/数组下标里的间接消费者。

**做不了时**：写进 `gaps[]`（当前只剩 4 条 info）或标 `latent[]`，**绝不猜、绝不改**。

---

## 5. 仲裁判定：赋值边 + 可达闭包（约 40 行）

```python
# 1) 从 4 个函数体里抽出所有赋值边:  src → dest
#    LP_SyncIn / LP_SyncOut (link_port.c), link_ParamFromSys / link_ParamApplyOne (link.c)
#    边方向 = 右值 → 左值; 剔掉关键字 / 函数名 / linkParam 枢纽 / 成员点 / 数组下标
E = {(src, dest, file, line) for f in (...4 个函数...) for ...}

# 2) 从镜像端出发做 BFS
out = closure(mirror)                       # 能走到哪些固件符号
up  = mirror 是否被 LP_SyncIn 直接/一跳写入  # 是否能被"固件 → 镜像"刷新

class = "A 固件赢" if (out hit firmware and up) else \
        "B 镜像赢" if (out hit firmware)       else "C 无人读"
```

**为什么这条规则对**：
- `A` 的两侧都有边 ⇒ 开关时镜像 = 固件常量（Flash 有效时 = Flash 值），改固件常量有效；
- `B` 只有出边 ⇒ 镜像的 BSS/静态初值每 tick 压过去，**改固件常量无效**；
- `C` 没有出边 ⇒ 值出不了 `linkParam[]`，是真空转。

`B` 的判定正好抓住了一个"两跳"的例子：`irCfg.kalOn --(link.c:732)--> irCalib.kalmanswitch --(link_port.c:226)--> kalmanswitch`。
两跳/三跳都由闭包统一处理，不需要人肉追。

---

## 6. 工具链：`tools/paramgen.py`

### 6.1 命令一览（已实现）

| 命令 | 方向 | 读 | 写 | 退出码 |
|---|---|---|---|---|
| `extract` | 固件 → JSON | 固件源码 / link.h / host 注册表 / docs | `tools/params/params.<profile>.json`（需 `--accept`，默认只预演） | 0 |
| `apply` | JSON → 固件 | JSON + 固件 | 固件（**默认 dry-run**；`--commit` 才落盘） | 0 成功 / 非 0 有阻断 |
| `check` | 多向对账 | 全部 | — | **0 一致；1 有 error**（`--strict` 连 warn 也失败） |
| `audit` | 只读 | 固件 | —（打印 A/B/C + 影子清单 + 覆写点） | 0 |
| `render` | JSON → Markdown | JSON | stdout；`--write` 注入 `docs/参数名称-功能-调整表.md` 的标记区 | 0 |

全局开关：`--profile {competition,legacy}`、`--fw-dir <目录>`（**指向临时副本做无风险验证**）。

### 6.2 常用姿势

```bash
cd "RemoteControl"
PY=C:/ProgramData/anaconda3/python.exe          # 项目约定解释器

$PY tools/paramgen.py extract --accept          # 把当前固件抽成 JSON
$PY tools/paramgen.py audit                     # A/B/C + 影子 + 覆写点
$PY tools/paramgen.py check                     # 对账(exit 0 才算过)
$PY tools/paramgen.py render                    # 看参数总表

# 改参: 只改 JSON 的 store.value / store.expr, 然后
$PY tools/paramgen.py apply                     # 预演(看 diff)
$PY tools/paramgen.py apply --commit --name TURNP
```

### 6.3 对账项（`check` 实做 14 条）

| # | 对账 | 级别 |
|---|---|---|
| C1 | 固件 `LINK_P_*` 宏集合 ↔ JSON `macro` | error |
| C2 | host `PARAM_IDS_*` 名字与 id ↔ JSON | error |
| C3 | JSON `fw.init` ↔ 固件实参 + **锚点行指纹** | error |
| C4 | `LINK_P_NUM` ↔ 条目数 | error |
| C5 | host `PARAM_META` 单位/小数位 ↔ JSON `doc` | warn |
| C10 | JSON `arbitration` ↔ 现场重算 | error |
| C11 | id 唯一 | error |
| C12 | 每个宏在 `link_ParamFromSys` 有槽 **且** 在 `link_ParamApplyOne` 有 `case` | error |
| C13 | 分歧策略（`pinned` 不等 → error；`report` 无 `why` → error） | error/info |
| C14 | `policy=apply` 但无 effective 锚点（**改了写不进去**） | error |
| C6/C8/C9 | `render` 产物、大表 sha256、红线 hex | 待 G1/G3（本轮 `--write` 未跑） |

> 本轮 `check` 结果：`error=0 warn=0 info=0`。**C12 是本方案最省事的一条**：
> 新增参数若只改了 `link.h` 忘了加搬运槽，`check` 立刻报错，不必等真机。

### 6.4 与在线调参的边界（不变）

- JSON / `apply` 管的是**编译期默认值（出厂值）**；
- `SETQ` / `SAVE` 管的是**运行期覆盖值（现场值）**，落 Flash page115；
- 二者单向：`JSON 生成出厂值 → 现场 SETQ 覆盖 → SAVE 落 Flash`。**不合并**。

---

## 7. 校验环与实测结果

```
   A. 幂等      apply(与固件一致的 JSON) → 0 字节                      ✅ 0 行 / 0 文件
   B. 定点      JSON.fw.init ↔ 固件实参 + 锚点行指纹(check C3)          ✅ 38/38(有锚点项)全等
   C. 反向定点  apply(extract(F)) → F 逐字节不变                       ✅ 实测
   D. 双向      store 改 4 项 → apply --commit → 重新 extract           ✅ 4/4 相等
   E. 零副作用  对临时副本操作 → 原树 git status                        ✅ 无改动
   F. 对账      paramgen check 退出码 0                                ✅
   G. 红线      双档重编 + 比赛档 sha256 == 68ad6be2…8141              ⏳ 待 G3 接进 apply
```

**E 的做法**：`cp -r code_competition %TEMP%/rc_apply_test` → `apply --commit --fw-dir <副本>` →
逐文件 sha256 对比。**固件源码在整个开发过程中一次都没被写过**（`git status` 为空即证）。
写回路径的"真落盘"要到 §11 的 G3 阶段、用户点头后才在真实树上跑一次。

---

## 8. 目录与文件（已落地）

```
RemoteControl/
├── tools/                                    ← 新增（跨 固件/上位机/文档 三层，故放仓库根）
│   ├── paramgen.py                           ← 唯一 CLI 入口（5 个子命令）
│   ├── params/
│   │   └── params.competition.json           ← **真源**（fw 自动 + store/policy/latent 人工）
│   └── paramkit/
│       ├── __init__.py
│       ├── bytesio.py     字节级安全读写（探测/行尾/先 tmp 再 os.replace）
│       ├── regexlib.py    ★ 9 条 ASCII 正则（语法知识的唯一来源）
│       ├── registry.py    源码登记簿：宏/槽/声明/赋值边/可达闭包/仲裁
│       ├── audit.py       覆写点扫描 + 风险项(gaps)
│       ├── meta.py        从 link.h 注释 / host PARAM_META / docs 表汇聚"人话"
│       ├── pipeline.py    抽取 + 合并（fw 重写、人工资产保留）
│       ├── patch.py       ★ 写回：行指纹 + 值域切片（5 条护栏）
│       └── render.py      Markdown 生成区（`<!-- GEN:BEGIN id=… sha=… -->`）
└── docs/参数名称-功能-调整表.md              ← 将来由 render 注入标记区（本轮未跑 --write）
```

**为什么放仓库根**：它同时要读 `code_competition/`（GBK）、`host/`（UTF-8）、`docs/`（LF），
放在任何一侧都会造成不对称的相对路径。

---

## 9. 参数总表（当前固件实测，41 项）

> 由 `python tools/paramgen.py render` 生成，`fw.value` 全部取自 `code_competition/` 现源码。
> 「仲裁」列：`固件赢` = 改固件常量有效；`⚠镜像赢` = 改固件常量**无效**（要改 `link.c` 镜像初值）；`⚠空转` = 无人读。

| id | 链路名 | 宏 | 分组 | 类型 | 开机生效值 | 仲裁 | 释义 | 生效源 |
|---:|---|---|---|---|---:|---|---|---|
| 1 | TRACKL | `LINK_P_TRACKL` | calib | float | 55.0855624 | 固件赢 | 左轮里程系数 | USER/user.c:18 |
| 2 | TRACKR | `LINK_P_TRACKR` | calib | float | 50.9356 | 固件赢 | 右轮里程系数 | USER/user.c:18 |
| 3 | SPEEDL | `LINK_P_SPEEDL` | calib | float | 0.56169 | 固件赢 | 左轮速度换算 | USER/user.c:20 |
| 4 | SPEEDR | `LINK_P_SPEEDR` | calib | float | 0.56439 | 固件赢 | 右轮速度换算 | USER/user.c:20 |
| 5 | GYRORATIO | `LINK_P_GYRO` | calib | int | 399 | 固件赢 | 陀螺角度系数（399.727 截断） | USER/user.c:17 |
| 6 | ENCANGLE | `LINK_P_ENCANG` | calib | int | 73 | 固件赢 | 编码器角度系数（73.71 截断） | USER/user.c:16 |
| 7 | WALLDIST | `LINK_P_WALLDIST` | calib | int | 40 | 固件赢 | 车中心到墙距离 | USER/user.c:21 |
| 22 | IRTHRWALL | `LINK_P_IRTHRWALL` | ir | int | 130 | 固件赢 | 侧墙判定阈值 | HARDWARE/TIMER/timer.c:50 |
| 23 | IRTHR45 | `LINK_P_IRTHR45` | ir | int | 0 | ⚠空转 | 45° 侧墙阈值（**字面量 150**） | BSS 无初值 |
| 24 | IRTHRCRIT | `LINK_P_IRTHRCRIT` | ir | int | 0 | ⚠空转 | 临界墙阈值（**字面量 60**） | BSS 无初值 |
| 25 | IRTHRFLO | `LINK_P_IRTHRFLO` | ir | int | 0 | ⚠空转 | 前墙远距-低（**字面量 230**） | BSS 无初值 |
| 26 | IRTHRFMID | `LINK_P_IRTHRFMID` | ir | int | 0 | ⚠空转 | 前墙远距-中（**字面量 240**） | BSS 无初值 |
| 27 | IRTHRFHI | `LINK_P_IRTHRFHI` | ir | int | 0 | ⚠空转 | 前墙远距-高（**字面量 245**） | BSS 无初值 |
| 28 | IRTHRFNEAR | `LINK_P_IRTHRFNEAR` | ir | int | 0 | ⚠空转 | 前墙近距（**字面量 65**） | BSS 无初值 |
| 29 | IRKALQ | `LINK_P_IRKALQ` | ir | int | **0** | ⚠镜像赢 | 卡尔曼 Q ×10000 —— **★发现1** | BSS 无初值 |
| 30 | IRKALP | `LINK_P_IRKALP` | ir | int | **0** | ⚠镜像赢 | 卡尔曼 P ×10000 —— **★发现1** | BSS 无初值 |
| 31 | IRKALSW | `LINK_P_IRKALSW` | ir | int | **0** | ⚠镜像赢 | 卡尔曼开关 —— **★发现1** | BSS 无初值 |
| 32 | IRFRONTMODE | `LINK_P_IRFRONTMODE` | ir | int | 0 | ⚠空转 | 前向综合模式（无该分支） | BSS 无初值 |
| 33 | BATTKV | `LINK_P_BATTKV` | power | int | 0 | ⚠空转 | 分压系数（写死 3.3×3/4096×100） | BSS 无初值 |
| 34 | BATTLOWMV | `LINK_P_BATTLOWMV` | power | int | 0 | ⚠空转 | 档位下限（写死 700） | BSS 无初值 |
| 35 | BATTHIGHMV | `LINK_P_BATTHIGHMV` | power | int | 0 | ⚠空转 | 满电参考（写死步进 12） | BSS 无初值 |
| 36 | BATTALPHA | `LINK_P_BATTALPHA` | power | int | 0 | ⚠空转 | IIR 系数（写死 1:9） | BSS 无初值 |
| 37 | BATTMAXPCT | `LINK_P_BATTMAXPCT` | power | int | 0 | ⚠空转 | 档位上限（写死 8） | BSS 无初值 |
| 38 | BATTMINPCT | `LINK_P_BATTMINPCT` | power | int | 0 | ⚠空转 | 档位下限（写死 1） | BSS 无初值 |
| 39 | TRACKCOORD | `LINK_P_TRACKCOORD` | motion | int | 0 | ⚠空转 | 纵向矫正灵敏度 —— **★发现3** | BSS 无初值 |
| 40 | TURNDELAY | `LINK_P_TURNDELAY` | motion | int | 5 | 固件赢 | 转弯延迟格数（`u8`，文档 16.5 是旧的） | UTILITY/utility.c:21 |
| 41 | SPDSEARCH | `LINK_P_SPDSEARCH` | motion | int | 40 | 固件赢 | 搜索直线速度初值（文档 95 是旧的） | HARDWARE/TIMER/timer.c:54 |
| 42 | SPDTURN | `LINK_P_SPDTURN` | motion | int | 30 | 固件赢 | 转弯速度（文档 60 是旧的） | HARDWARE/TIMER/timer.c:55 |
| 43 | DISF60 | `LINK_P_DISF60` | motion | int | 60 | ⚠空转 | 180° 前对正阈值（字面量 60） | REMOTE/link.c:126 |
| 44 | GSKP | `LINK_P_GSKP` | motion | float | 0.4 | 固件赢 | 盲走直行 P | HARDWARE/TIMER/timer.c:57 |
| 45 | TURNP | `LINK_P_TURNP` | motion | int | 4 | 固件赢 | 转弯 P（文档 5.0 是旧的） | HARDWARE/TIMER/timer.c:61 |
| 46 | TURND | `LINK_P_TURND` | motion | int | 4 | 固件赢 | 转弯 D（文档 4.5 是旧的） | HARDWARE/TIMER/timer.c:61 |
| 47 | PTKP | `LINK_P_PTKP` | motion | float | 2.5 | ⚠空转 | 原地转 P（无消费者） | REMOTE/link.c:123 |
| 48 | PTKI | `LINK_P_PTKI` | motion | float | 0.01 | ⚠空转 | 原地转 I（无消费者） | REMOTE/link.c:123 |
| 49 | PTKD | `LINK_P_PTKD` | motion | float | 0.1 | ⚠空转 | 原地转 D（无消费者） | REMOTE/link.c:123 |
| 50 | MKPL | `LINK_P_MKPL` | motion | float | 10.75 | ⚠镜像赢 | 左轮电机 P —— **★发现2** | REMOTE/link.c:124 |
| 51 | MKIL | `LINK_P_MKIL` | motion | float | 0.55 | ⚠镜像赢 | 左轮电机 I | REMOTE/link.c:124 |
| 52 | MKDL | `LINK_P_MKDL` | motion | float | 0.55 | ⚠镜像赢 | 左轮电机 D | REMOTE/link.c:124 |
| 53 | MKPR | `LINK_P_MKPR` | motion | float | 10.2 | ⚠镜像赢 | 右轮电机 P | REMOTE/link.c:125 |
| 54 | MKIR | `LINK_P_MKIR` | motion | float | 0.5 | ⚠镜像赢 | 右轮电机 I | REMOTE/link.c:125 |
| 55 | MKDR | `LINK_P_MKDR` | motion | float | 0.5 | ⚠镜像赢 | 右轮电机 D | REMOTE/link.c:125 |

---

## 10. `latent[]`：本质无效的改动点（已灌 27 项）

`latent[]` 是**人工字段**，记录"看着像该改的地方，其实改了没用/被覆盖"。

| 参数 | `latent` 摘要（`file:line` → 值） |
|---|---|
| `IRKALQ` / `IRKALP` | `adc.c:144` `Qf1=0.01` / `Pf1=0.2` — 被 `link_port.c:227/228` 每 tick 写成 0 |
| `IRKALSW` | `adc.c:151` `kalmanswitch=1` — 被 `link_port.c:226` 写成 0 |
| `MKP×6` | `timer.c:75`（3/2/0.1/3/2/0）+ `user.c:80-81`（搜索期 13/0.74/0.9）+ `user.c:91-92`（冲刺期 13/0.7/0.8）——全部被 `link.c:124-125` 覆盖 |
| `IRTHR45` | `timer.c:509,511` 字面量 `150` |
| `IRTHRCRIT` | `timer.c:521,523` 字面量 `60` |
| `IRTHRFLO/MID/HI/NEAR` | `timer.c:503` 字面量 `230 / 240 / 245 / 65` |
| `TRACKCOORD` | `timer.c:52` `const u8 trackcoor=127`；消费点 `timer.c:885/886/909/910` |
| `DISF60` | `timer.c:695,700,705` `disf>60` |
| `BATTKV/LOWMV/HIGHMV/ALPHA/MAXPCT/MINPCT` | `timer.c:1283-1287` 写死的分压/档位算式 |
| `PTKP/KI/KD`、`IRFRONTMODE` | 空：新固件**没有对应消费形态**（点转走表驱动 + `turn_kp/turn_kd`） |

### 10.1 自动扫出的运行期覆写点（`overwrites[]`）

工具从固件源码自动发现以下几组覆写（与 v1 的手写矩阵一致，但现在是机器产出）：

| 参数 | 覆写点（自动扫出） |
|---|---|
| `TURNP` / `TURND` | `UTILITY/utility.c:1162,1174,1186,1219`（冲刺增益表，不恢复） |
| `TURNDELAY` | `USER/user.c:85`（搜索期 9）、`USER/user.c:96`（冲刺期 0） |
| `SPDSEARCH` | `USER/user.c:82,93`、`UTILITY/utility.c:1313…1335`（转弯后回落/分段重算） |
| `SPDTURN` | `USER/user.c:94`、`UTILITY/utility.c:1221` |
| `IRTHRWALL` | `UTILITY/utility.c:1417,1423,1497,1510`（智能冲刺收紧阈值） |

---

## 11. 未做项与待拍板

| # | 项 | 现状 | 建议 |
|---|---|---|---|
| **G1** | `render --write` 注入 `docs/参数名称-功能-调整表.md` 的标记区 | 未跑（`--stdout` 已可用）；C6 未启用 | 下一步做：先在该文档加 `<!-- GEN:BEGIN id=param-table -->`…`<!-- GEN:END -->` |
| **G2** | `emit-host`：JSON → `host/host/protocol/params_spec.py`（`commands.py` 改为 import） | 未做（`check C2/C5` 已能对账） | 等参数表稳定后再做，避免动 host 契约测试 |
| **G3** | `apply` 落地**红线自动校验**（`build_competition_modes.py` + 比赛档 sha256） | 未接；本轮靠"只在副本上写"保证零风险 | 接进来后才能在真树上 `apply --commit` |
| **G4** | `params.legacy.json`（课程固件，只 extract/render/check） | 未做 | P3 之后再排 |
| **P1** | 三个行为级发现是否**修**（B 类接线：让 `MKP*`/`IRKALQ` 能被 `SETQ` 生效） | 只登记，未改 | **建议先不修**：修了就改远程档行为，且改动碰 `link_port.c`/`timer.c`（红线相关）；先把"记录"做扎实 |
| **P2** | `link.h:364-380` 的宏注释里 `默认 145 / 16.5 / 95 / 60 / 5.0 / 4.5` 全是旧值 | 未改 | 下一轮可让 `render` 反向生成宏注释（或手工对齐 §9） |
| **P3** | 真机验证 §2.2 三条（`GET IRKALQ`→0.0、远程档电机 PID、`SETQ TRACKCOORD` 无效） | 待真机 | 与 P3 真机联调合并做，**不需要发车**：`GET` / `PONG` / 静置遥测即可 |

**待你拍板**：

| # | 决策 | 选项 | 我的建议 |
|---|---|---|---|
| D1 | `latent[]` 的所有权 | (a) 只活在 JSON（人工维护）<br>(b) 另立 `latent_seed.json` 由 extract 合并 | **(a)** —— 灌一次就固化，extract 已验证能原样保留；多一份 seed 文件只会再造一个"要同步的地方" |
| D2 | B 类（`MKP*` / `IRKALQ` / `IRKALP`）怎么处理 | (a) 只登记（现状）<br>(b) 把 `LP_SyncIn` 补上这些字段（**改固件**，接线）<br>(c) `policy` 全钉成 `pinned`，堵死 JSON 侧改写 | ~~(a)~~ **已被用户 2026-10-16 新指令覆盖** → 改为"**L1/L2/L3 同改，默认值取 L2**"，见核查报告 **E1**（待 grill 定稿） |
| D3 | `apply` 何时上真树 | (a) G3 接完红线校验后<br>(b) 现在就允许（靠人工先关 Keil） | **(a)** |
| D4 | 参数表版本号 | 沿用 v2.5 / 另起 v3.0 | **另起 v3.0**（competition 档），v2.5 冻结为 legacy 档历史 |

---

## 12. 风险与取舍

| 风险 | 缓解 |
|---|---|
| 工具误改固件 → 翻车 | `apply` 默认 dry-run；行指纹 + 单行唯一 + 只切值域；写前拒绝不猜；落盘先 tmp 再 replace；**本轮全部写在 `%TEMP%` 副本上** |
| 静态审计误判（宏/间接消费） | `arbitration` 由闭包算 + 人可在 `notes[]`/`latent[]` 里反驳；C10 只报 error 不自动改 |
| JSON 变成"第二个要同步的文档" | `check` 的 C1–C14 把它钉在"漏一处就报错"的位置；`extract` 自动刷 `fw`，人工只碰 `store/policy/latent` |
| GBK/CRLF 被写坏 | 全流程 bytes；正则纯 ASCII；写后可用 `git diff --stat` 一眼看是否只有目标行变化 |
| B 类"改了没用"被继续忽略 | `audit` 每次都把 B 类打上 `⚠镜像赢`；`render` 的表格里也有该列 |
| 与既有 `SETQ/SAVE` 流程重叠 | 边界写死：JSON = 编译期出厂值；`SETQ/SAVE` = 运行期现场值，单向覆盖 |

---

## 附录 A：本轮验证命令与输出（可复现）

```bash
cd "RemoteControl"
PY=C:/ProgramData/anaconda3/python.exe

$PY tools/paramgen.py extract --accept
#   已写入 tools\params\params.competition.json
#   条目 41 项 | A=14 B=9 C=18 | gaps=4

$PY tools/paramgen.py check ; echo $?
#   统计: error=0 warn=0 info=0
#   0

# 幂等(JSON 与固件一致)
cp -r code_competition "$TEMP/rc_apply_test"
$PY tools/paramgen.py apply --fw-dir "$TEMP/rc_apply_test"
#   将改 0 行 / 0 文件

# 写回 + 定点(把 TURNP/TURND/MKPL/ENCANGLE 的 store 改掉)
$PY tools/paramgen.py apply --commit --fw-dir "$TEMP/rc_apply_test"
#   HARDWARE/TIMER/timer.c:61   double turn_kp=4,turn_kd=4;  →  double turn_kp=4.5,turn_kd=3.5;
#   REMOTE/link.c:124           linkMtrKpL = 10.75, …        →  linkMtrKpL = 12, …
#   USER/user.c:16              u16 angleratio=81*0.91;      →  u16 angleratio=75;
#   将改 3 行 / 3 文件

git status --porcelain -- code_competition host
#   (空)   ← 原件零改动
```

## 附录 B：一句话总结

> **参数表不再是"文档"，而是可执行的真源**：`fw` 由固件自动抽取、`store` 由人管理、`policy` 决定分歧怎么算，
> 仲裁 A/B/C 由赋值边可达闭包算出，读写走 ASCII 正则 + 字节切片 ——
> 于是"改了参数但行为没变"这件事，第一次变成了 `audit` 里一行 `⚠镜像赢`，而不是赛后翻车。
