# 参数 JSON 化 · 「两边同改」一致性核查报告

> 日期：**2026-10-16** ｜ 基线：`code_competition/`（fw 300 / MAZETYPE 16）@ `cb112ee`
> 本轮**未改任何代码、JSON、既有文档**；只做静态核查。
> 触发：用户提出 "B 类应当**同时修改固件侧与镜像侧的量**，确保固件中的值与导出的参数完全一致，保可重复性"。
> 本报告核对该原则在现有代码上的落地条件，并列出不一致项 **I1–I7** 与决策点 **E1–E4**。

---

## 0. 用户原则的复述（待确认）

| # | 原则 | 我的理解 |
|---|---|---|
| P-1 | B 类的成因 | 值写在固件里，但**为了让上位机可控**，镜像每 tick 把它覆盖掉 |
| P-2 | 比赛档不存在此问题 | 比赛档无远程层 ⇒ 无覆盖 ⇒ 固件值即真值 |
| P-3 | 工具与参数表是**通用**的（不分档） | 因此写回时**两边同改**（固件侧 + 镜像侧），使"固件源码里读到的值" == "导出的参数值"，保可重复性 |
| P-4 | 版本号 | 参数表直接上 **v3.0**（当前是 v2.5） |
| P-5 | 真机写回（`apply --commit` 上真树） | **等方案确定**，本轮不做 |

P-2 已核实为真：`REMOTE/link_port.c:31` 起整文件 `#ifdef MOUSE_REMOTE` 门控，`Target 1` 不含该宏 ⇒ 比赛档里 `LP_SyncIn/SyncOut` 根本不参与编译。✅

---

## 1. 关键前提：「固件中的量」其实是**三层**，不是一个

核查中发现，一条参数的"固件侧值"在源码里可以落在**三个互不相同的层**：

| 层 | 名字 | 例子（电机 P，左轮） | 生效窗口 |
|---|---|---|---|
| **L1** | 编译期初值 | `HARDWARE/TIMER/timer.c:75` `double kpl=3,...` | 上电 → 第一次 `parameter_search()` |
| **L2** | 运行期整定（固件自己写的） | `USER/user.c:80` `kpl=13;`（搜索期）<br>`USER/user.c:91` `kpl=13;`（冲刺期） | 进入对应阶段后，**比赛档全程** |
| **L3** | 镜像初值（远程层） | `REMOTE/link.c:124` `linkMtrKpL = 10.75` | 远程档**全程**（每 tick 压回 L1/L2） |

**同一台车，烧比赛档 kpl=13，烧远程档 kpl=10.75。** 三层同时存在，且当前**三个值互不相等**。

> 这就是"存储数据与固件中不一定一致"的物理来源：不是谁写错了，而是**三层语义本来就不同**。
> 所以"同时改两个量"必须先把"两个"指认为哪两层，否则改完仍然不一致。

---

## 2. 逐类核查：B 类重新清点

### 2.1 B 类 = **8 项**（不是 9 项，见 I4）

| 链路参数 | 镜像字段 | L1 编译期初值（锚点） | L2 运行期写者 | L3 镜像初值（锚点） | 需改处数 |
|---|---|---|---|---|---|
| `MKPL`/`MKIL`/`MKDL` | `linkMtrKpL/KiL/KdL` | `timer.c:75` `kpl=3,kil=2,kdl=0.1` | `user.c:80`(13/0.74/0.9)、`user.c:91`(13/0.7/0.8) | `link.c:124` 10.75/0.55/0.55 | **4** |
| `MKPR`/`MKIR`/`MKDR` | `linkMtrKpR/KiR/KdR` | `timer.c:75` `kpr=3,kir=2,kdr=0` | 同上 | `link.c:125` 10.20/0.5/0.5 | **4** |
| `IRKALQ` | `irCfg.kalQ` | `adc.c:144-147` `Qf1=Qf2=Ql=Qr=0.01`（**4 个变量**） | — | **不存在**（`link_port.c:40` 是 BSS 裸定义） | **1**（且要同时写 4 个变量） |
| `IRKALP` | `irCfg.kalP` | `adc.c:144-147` `Pf1=Pf2=Pl=Pr=0.2` | — | **不存在** | **1** |

### 2.2 A 类应从 14 → **15**：`IRKALSW` 归错了

`IRKALSW` 的链路镜像字段是 `irCfg.kalOn`，但**真正生效的固件量是 `kalmanswitch`**，而它是**自洽回环**：

```
LP_SyncIn  :91   irCalib.kalmanswitch = kalmanswitch;   ← 固件 → 镜像（回读）
LP_SyncOut :226  kalmanswitch        = irCalib.kalmanswitch;  ← 镜像 → 固件
link.c     :732  irCalib.kalmanswitch = irCfg.kalOn;    ← 仅 SETQ / CalibLoad 路径
```

⇒ SyncOut 写进 `kalmanswitch` 的值，下一拍会被 SyncIn 原样读回 ⇒ **改固件常量有效、`SETQ` 也有效且持久**。
⇒ 判定为 **A 类（固件赢）**，不是 B。

修正后：**41 = A 15 + B 8 + C 18**。

---

## 3. 不一致清单

| # | 级别 | 不一致 | 证据 | 后果 |
|---|---|---|---|---|
| **I1** | ★★★ | **"两边相等"的契约只写在注释里，且已被违反** | `link.c:120-125` 声明 "静态初值必须 == 各编译期常量 `MOTOR_KP_*` / `TURN_PID_*`"；但这两个宏**全工程不存在**（只有这条注释提到）；实际 `linkMtrKpL=10.75 ≠ kpl=3`、`linkPtKp=2.5 ≠ turn_kp=4` | 你要的"可重复性"目前**没有可依赖的锚** |
| **I2** | ★★★ | **B 类的固件侧真值根本没进 JSON** | `MKPL.fw.value = 10.75 @ REMOTE/link.c:124`（工具把**镜像初值**当成了固件值）；`kpl=3 @ HARDWARE/TIMER/timer.c:75` 不在 JSON 任何字段 | `fw` 字段名不副实；按"以固件为准"它给出的就是错的 |
| **I3** | ★★★ | **镜像端是 BSS 结构体的参数，`fw` 被记成 0** | `IRKALQ/IRKALP/IRTHRFNEAR/TRACKCOORD` → `fw={value:0, source:"bss-zero", anchors:[]}`；而固件里都有确定值：`adc.c:144-147`(0.01/0.2)、`timer.c:52 trackcoor=127`、`timer.c:503` 字面量 `65` | 导出值与固件真值直接矛盾，且 schema 里**没有位置**放固件真值 |
| **I4** | ★★ | **仲裁误判 `IRKALSW` 为 B（应为 A）** | 判据 `registry._reachable_from_fw()` 只检查"镜像符号 `irCfg.kalOn` 是否被 `LP_SyncIn` 写"，没检查"sink 是否被 `LP_SyncIn` **回读**" | B 类清点错误 ⇒ 决策建立在错的名单上 |
| **I5** | ★★ | **`GET IRKALSW` 与真实开关不一致**（真机可验，不用发车） | `link.c:589 linkParam[16].q = irCfg.kalOn`（BSS=0），而固件 `kalmanswitch` 实际是 `adc.c:151 = 1` | 参数表读回 **0**，固件实际 **1**；同族 `IRKALQ/IRKALP/IRTHRFNEAR/TRACKCOORD` 一律读回 0 |
| **I6** | ★★ | **上一轮文档 §2.2 发现 1 归因错了一处** | 原文称"`kalmanswitch` 被置 0"。实际 `kalmanswitch` 恒为 1；真正被清零的是 `Qf1..Qr`/`Pf1..Pr`（因 `irCfg.kalQ/kalP` 无初值） | 结论方向（远程档滤波退化）不变，但**真机判据要改**：`GET IRKALSW`→0 是"参数表的值"，不是固件开关 |
| **I7** | ★ | **C 类不是铁板一块，需分 C1/C2** | `irCfg.thrFrontNear` 虽不被比赛固件读，但**远程层自己**读它（`link.c:1261`、`link.c:1266-1267`）⇒ `SETQ IRTHRFNEAR` 会改变**远程层行为**；而 `sysCfg.trackCoord` 无人读（真 C） | "C 类 = 改了没用"这句话对 C2 组不成立 |

---

## 4. 「两边同改」落到 B 类上的两个结构性障碍

### 障碍 1：`MKP*` 的"另一个量"不是一个，而是**两个**，且其中一个是比赛档真值

```
timer.c:75   double kpl=3,kil=2,kdl=0.1,kpr=3,kir=2,kdr=0;   ← L1
user.c:80-81 kpl=13;kil=0.74;kdl=0.9; ...                    ← L2（比赛档搜索期真值）
user.c:91-92 kpl=13;kil=0.7; kdl=0.8; ...                    ← L2（比赛档冲刺期真值）
link.c:124   linkMtrKpL=10.75,linkMtrKiL=0.55,...            ← L3
```

- 只改 **L1 + L3** 对齐 ⇒ **不改比赛档行为**（L2 会覆盖 L1），但**"参数表说 10.75、比赛档跑 13"仍然不一致**，只是不一致点从源码文本移到了运行期；
- 连带 **L2 一起改** ⇒ **改掉赛场调出来的搜索期/冲刺期整定**，比赛档行为变化 ⇒ **撞红线语义**。

**⇒ 单纯对齐 L1/L3 达成的是"上电初值一致"，不是"两档行为一致"。** 这一点必须先讲清，否则"完全一致"是达不到的。

### 障碍 2：`IRKAL*` 的"镜像初值"**在源码里不存在**

`link_port.c:40` `IrCfg irCfg;` 是 BSS 裸定义 ⇒ **没有初始化式可写**。写回无处落笔，除非：
- (i) 给结构体定义加初始化式（**改结构**，不是"只切值域"，与现有写回三护栏冲突）；或
- (ii) 改 `link_port.c:227-228` 的常量除子（`/10000.0`），语义错；或
- (iii) 接受 `IRKAL*` 只能单边（只改固件 L1），并在表里注明"远程档由 SETQ/SAVE 覆盖"。

⇒ **"两边同改"对 `IRKALQ/IRKALP` 只有 1 边。** 这是与 `MKP*` 不同的一族。

---

## 5. 需要拍板的决策点

| # | 决策 | 选项 | 备注 |
|---|---|---|---|
| **E1** | **"两个量"指哪两层？** | **甲**：只对齐 **L1(`timer.c:75`) ↔ L3(`link.c:124-125`)**；L2(`user.c` 整定)**不改**，作为独立运行时参数另行登记<br>**乙**：连 **L2 也改**，全档同一物理值（⚠ **改比赛档行为**）<br>**丙**：都不改，只在 JSON/表里**并列记录 L1/L2/L3 三值 + 分歧标记** | 甲最接近你的原意且不动红线语义；乙需要你明确许可 |
| **E2** | `fw` 字段是否拆成三层 | 建议拆：`fw.l1`（编译期初值）/ `fw.l2[]`（运行期覆盖者列表）/ `mirror`（镜像初值） | 直接回应 I2/I3 |
| **E3** | I4 的仲裁 bug 是否修 | 建议修：把判据从"镜像符号是否被 SyncIn 写"改为"sink 是否被 SyncIn 回读" | 修完 B 类 9→8、A 14→15 |
| **E4** | I3 的"BSS 结构体参数固件真值"如何入 JSON | 需要新增一个概念位（如 `fw.l1` 允许指向**不含该符号**的宿主文件 + 变量列表，如 `adc.c:144-147 → Qf1/Qf2/Ql/Qr`） | 否则这几项永远 `fw=0` |
| **D1** | `latent[]` 所有权 | (a) 只活在 JSON<br>(b) 另立 seed 文件 | 上一轮遗留，仍待定 |
| **D3** | `apply --commit` 上真树 | 已按你说的**挂起**，等方案定 | 当前仍只在 `%TEMP%` 副本上验证 |
| **D4** | 参数表版本号 | **v3.0** ✅（已收到） | 现文件标题为 `（v2.5）`，LF 换行 |

---

## 6. 附录：本报告的全部证据行

```
REMOTE/link.c:120-126        镜像初值 + "必须 == 编译期常量" 注释
REMOTE/link.c:569-591        link_ParamFromSys 的 IR 段（镜像 → linkParam）
REMOTE/link.c:615            linkParam[24].q = sysCfg.trackCoord
REMOTE/link.c:688-736        link_ParamApplyOne 的 IR 段（linkParam → 镜像）
REMOTE/link.c:723-724        irCfg.kalQ + kalmanSt.Qf1..Qr 直写
REMOTE/link.c:731-732        irCfg.kalOn → irCalib.kalmanswitch
REMOTE/link.c:763            sysCfg.trackCoord ←（唯一写者）
REMOTE/link.c:1261,1266-1267 远程层自己读 irCfg.thrFrontNear
REMOTE/link.c:3337,3435      LINK_Tick: LP_SyncIn 在头、LP_SyncOut 在尾
REMOTE/link_port.c:31        整文件 #ifdef MOUSE_REMOTE
REMOTE/link_port.c:37-54     镜像实例定义（全部 BSS 裸定义）
REMOTE/link_port.c:71-199    LP_SyncIn（含 :91 kalmanswitch、:156 wallThresh、:196-197 turn_kp/kd）
REMOTE/link_port.c:210-269   LP_SyncOut（:226 kalmanswitch、:227-228 Q/P、:253-258 kpl..kdr）
HARDWARE/TIMER/timer.c:50-61 sidewall_threshold / trackcoor / speed_ss / turn_kp
HARDWARE/TIMER/timer.c:75    double kpl=3,kil=2,kdl=0.1,kpr=3,kir=2,kdr=0;
HARDWARE/TIMER/timer.c:885,886,909,910  settrack(trackcoor)
HARDWARE/TIMER/timer.c:503   (disf1<65)||(disf2<65)   ← IRTHRFNEAR 的字面量
HARDWARE/ADC/adc.c:144-147   Qf1=0.01,Pf1=0.2, ...  Qr=0.01,Pr=0.2
HARDWARE/ADC/adc.c:151       u8 kalmanswitch=1;
USER/user.c:16-28            双档 5 标定声明（远程档去 const）
USER/user.c:78-97            parameter_search / parameter_spurt
UTILITY/utility.c:21         u8 turn_delay = 5;
```

---

# 附篇（同日追加）：「多套参数」的完整清单

> 触发：用户要求"对搜索期/冲刺期这样不同时期不同参数的量**拆分成多套**"。
> 核查后发现：**"多套"远不止 `MKP*`**，且**绝大多数多套量根本不在参数表里**。
> 本节只列事实与锚点，不含方案。

## A. 「时期 / 模式」维度的实际枚举

固件里的"时期"不是两个，至少 **7 类**：

| 时期 | 触发点 | 文件 |
|---|---|---|
| ① 上电初值 | 全局初始化式 | `timer.c:45-63`、`utility.c:21`、`utility.c:1260-1268` |
| ② 搜索期 | `parameter_search()` | `user.c:78-86` |
| ③ 冲刺期 | `parameter_spurt()` | `user.c:89-97` |
| ④ LOW / MEDIUM / HIGH | `spurt(mode)` | `utility.c:1151-1192` |
| ⑤ 冲刺 90°（按入速分段） | `taskstraight90()` | `utility.c:1280-1286`、`:1327-1331`、`:1345-1349`、`:1359-1363`、`:1372-1376` |
| ⑥ 去程 / 回程 | `GoOrBackFlag` | `utility.c:1287-1293`（`BackTopSpeed`/`BackBottomSpeed`） |
| ⑦ 按键测试菜单 | `test_spurt90(...)`（10 个入参直接赋给全局） | `utility.c:1211-1222` |

## B. 多套量清单（★ = 当前**不在**参数表、无链路 id）

| 变量 | 上电初值 | 各套值（出处） | 套数 | 有链路 id？ |
|---|---|---|---|---|
| `kpl/kil/kdl` | `timer.c:75` 3/2/0.1 | 13/0.74/0.9 `user.c:80`；13/0.7/0.8 `user.c:91`；10.75/0.55/0.55 `link.c:124` | 4 | ✅ MKPL/KIL/KDL |
| `kpr/kir/kdr` | `timer.c:75` 3/2/0 | 13/0.74/0.9；13/0.7/0.8；10.20/0.5/0.5 `link.c:125` | 4 | ✅ MKPR/KIR/KDR |
| `turn_kp/turn_kd` | `timer.c:61` 4/4 | 4/2 `utility.c:1162-1163`；8/5 `:1174-1175`；10/7 `:1186-1187` | **4** | ✅ TURNP/TURND（现被当成"1 套"） |
| `SPURT90IR_kp1/kp2/kd` | **无（BSS 0）** ★ | 0.17/0.1/0 `:1155-1157`；0.25/0.15/0 `:1167-1169`；0.3/0.15/0 `:1179-1181`；0.31/0.21/0.1 `:1282-1284`；0.5/0.35/2 `:1327-1329`；0.5/0.35/1 `:1345-1347`；0.4/0.3/0.3 `:1359-1361`；0.3/0.3/0.3 `:1372-1374` | **8** | ❌ 无 |
| `SPURT90GYRO_kp/kd` | **无（BSS 0）** ★ | 0.3/0 `:1158-1159`；0.3/0 `:1170-1171`；0.3/0 `:1182-1183`；0.28/0.10 `:1285-1286`；0.4/0.4 `:1330-1331`；0.25/0.08 `:1348-1349`；0.25/0.08 `:1362-1363`；0.4/0.4 `:1375-1376` | **8** | ❌ 无 |
| `STRAIGHT45_kp/kd` | `timer.c:60` 0.12/0.1 | 0.07/0.05 `:1160-1161`；0.1/0.1 `:1172-1173`；0.1/0.1 `:1184-1185` | 4 | ❌ 无 |
| `sidewall_threshold` | `timer.c:50` 130 | **110** `utility.c:1417`、`:1423`（LT45L/RT45L 分支） | 2 | ✅ IRTHRWALL |
| `speed_ss` | `timer.c:54` 40 | 80 `user.c:82`；100 `user.c:93`；70 `:1418/:1424`；100 `:1416/:1419/:1420/:1422/:1425/:1426`；动态公式 `:1313-1339`；`utility.c:1522` 70 | 多 | ✅ SPDSEARCH |
| `speed_turn` | `timer.c:55` 30 | 70 `user.c:94`；`spd_turn` `utility.c:1221` | 3 | ✅ SPDTURN |
| `turn_delay` | `utility.c:21` 5 | 9 `user.c:85`；0 `user.c:96` | 3 | ✅ TURNDELAY |
| `keep_dis` | `timer.c:45` 14 | 12 `user.c:84`、`user.c:95` | 2 | ❌ 无 |
| `speed_fastss` | `timer.c:56` 30 | 100 `utility.c:1416/1419/1420/1422/1425/1426`、`:1409-1414`；动态 `:1249-1255` | 多 | ❌ 无 |
| `SMARTSTRAIGHTFAST_bigerror_kp` / `_smallerror_kp` | `timer.c:58/59` 0.5/0.35 | 无其它写者 | 1 | ❌ 无 |
| `LTopSpeed/LBottomSpeed/L90TopSpeed/L90BottomSpeed/BackTopSpeed/BackBottomSpeed/ObjectGoTo_TopSpeed/ObjectGoTo_BottomSpeed` | `utility.c:1260-1263` | 多处动态选择 | 多 | ❌ 无 |
| `SpurtAccRatio45/SpurtDecRatio45/SpurtAccRatio90/SpurtDecRatio90/SpurtAccRatio/SpurtDecRatio` | `utility.c:1265-1267` | 多处动态选择 | 多 | ❌ 无 |
| `beforeturntrack` | 分支内赋 0/3/7/13/14/60/65 | `utility.c:1409-1435` | 多 | ❌ 无 |

## C. 由此暴露的**新增**不一致

| # | 级别 | 不一致 |
|---|---|---|
| **I8** ★★★ | `SPURT90IR_*` / `SPURT90GYRO_*` **5 个变量在 `timer.c:62` 无初始化式（BSS 0）**，却有 **8 套**分时期赋值；且**完全没有链路参数**（不在 41 项内）。⇒ "参数表 = 固件可调量的真源"这个前提目前不成立 |
| **I9** ★★★ | 多套量里**绝大多数没有链路 id**（`keep_dis`/`speed_fastss`/`SMARTSTRAIGHTFAST_*`/`TopSpeed 家族`/`SpurtAccRatio 家族`/`beforeturntrack`/`SPURT90*`/`STRAIGHT45_*`）。⇒ "拆分成多套"若要求"上位机可调"，必须扩链路协议；若不扩，它们只能是**编译期层**参数 |
| **I10** ★★ | `TURNP/TURND` 现被当"1 套"，实际 `spurt()` 写了 3 套（4/2、8/5、10/7）+ 初值 4/4 ⇒ **现有模型缺"多套"维度，A/B/C 三分类不足以描述** |
| **I11** ★★ | `IRTHRWALL`（`sidewall_threshold`）在 `utility.c:1417/1423` 被写成 **110** ⇒ 也是多套，与"v2.5 起改读参数表"的说明冲突 |
| **I12** ★★ | 三分类只回答"**改固件常量**会不会被打回"，没回答"**`SETQ` 会不会被固件运行期写者覆盖**"。例：`SETQ TURNP 10` 在下次 `spurt()` 被改回 8/5/7 ⇒ **两个问题必须分开建模** |
| **I13** ★ | `kiturn`（`timer.c:63`）**全工程只有定义、无任何读写** ⇒ 死变量 |

## D. 协议侧硬约束（决定了"多套"能怎么落地）

```
REMOTE/link.h:325  分段约定"与上位机 protocol/commands.py 的 PARAM_IDS 必须逐项一致"
REMOTE/link.h:382-385
  #ifdef MOUSE_REMOTE  #define LINK_P_NUM 41
  #else                #define LINK_P_NUM 24   /* 比赛档保持 v2.4 容量, 产物逐字节不变 */
```

- id 空间 **1-7 / 22-38 / 39-55**；比赛档 `LINK_P_NUM` 必须**保持 24**（否则 page115 旧数据失效 + 红线变化）。
- ⇒ **新增"套"不能简单占用比赛档 id 空间**；`PARAM_IDS` 改动还会牵动 `host/contract_test.py`（262 项）。
