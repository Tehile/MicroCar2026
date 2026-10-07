# 迷宫算法详解

本文档详解电脑鼠的**迷宫模型、搜索、最短路径、冲刺与运动控制算法**,与代码实现逐条对应。四个核心机制分别在 §3(搜索)、§4(最短路径)、§6(冲刺优化)、§7(45° 冲刺)详述。

> 注:算法按**全迷宫 16×16** 设计,但当前课程化构建以 `MAZETYPE=7`(maze.h:65,四分之一迷宫 7×7,终点 (6,6))编译;`MAZETYPE` 注释表明可改 8 或 16。文档按通用机制描述,具体数值以代码宏为准。

## 1. 算法总览:搜索 → 最短路 → 冲刺

```
┌────────────────────────────────────────────────────────────┐
│  阶段一  迷宫探索(未知迷宫)                                   │
│    贴墙法则(6种)+ 中心法则 → 走遍迷宫                         │
│    MAZE_WallCheck 实时建墙图 mapWall                          │
│    岔路决策/死路剪枝 → 用全开图距离图选最短方向               │
└──────────────────────────┬─────────────────────────────────┘
                           ▼
┌────────────────────────────────────────────────────────────┐
│  阶段二  路径规划(已建图)                                     │
│    洪水填充: 全闭图/全开图距离图(等高值)                     │
│    最短路:   8方向(允许45°斜切)/ 4方向 生成转向动作序列        │
└──────────────────────────┬─────────────────────────────────┘
                           ▼
┌────────────────────────────────────────────────────────────┐
│  阶段三  高速冲刺(45°/90°)                                   │
│    动作合并 → 直行合并 → 转弯查表前馈                          │
│    睁眼/闭眼切换, L/M/H 三档速度与 PID 增益                    │
└────────────────────────────────────────────────────────────┘
```

## 2. 迷宫模型与墙位编码

### 2.1 坐标系与目标

- 迷宫 `MAZETYPE × MAZETYPE` 格(设计 16×16,当前构建 7×7),坐标 `(x, y)`,`mousePos.cX` 横轴、`.cY` 纵轴。
- 起点默认 (0,0),由起始出口判定可能镜像到另一侧。
- 终点为中心区四格(设计 `x∈{7,8}, y∈{7,8}`;当前构建单格 (6,6))。

### 2.2 墙位字节编码

`mapWall[x][y]` 一个字节同时保存**本格四个方向的通行性**与**四个邻居看回本格的通行性(镜像)**。

```
mapWall[x][y] 字节布局
┌──────────────────┬──────────────────┐
│  低 四位 本格通行 │  高 四位 镜像位  │
│  bit0 上  0x01     │  bit4 上  0x10    │
│  bit1 右  0x02     │  bit5 右  0x20    │
│  bit2 下  0x04     │  bit6 下  0x40    │
│  bit3 左  0x08     │  bit7 左  0x80    │
└──────────────────┴──────────────────┘
位 = 1 表示"开口/可通行"; 位 = 0 表示"有墙"
```

> 注意与直觉相反:**位为 1 表示此方向开口**。低 四位 用于本格判墙(全闭图),高 四位 供邻居同步与全开图判墙。

> **双地图语义(全开/全闭)**:未访问格初始化为 `0xf0` —— 低 四位=0(全闭图:未知按有墙),高 四位=0xF(全开图:未知按无墙)。`FLOODFILL_Close` 读低 四位得到**已探明路径上界**,`FLOODFILL_Open` 读高 四位得到**理论最短下界**;两图在起点相等即已找到最优解(联动 §3.4)。

### 2.3 方向编码与相对方向宏

绝对方向:`UP=0, RIGHT=1, DOWN=2, LEFT=3`。`(1 << 绝对方向)` 即得对应墙位宏。

相对方向宏依赖当前朝向 `mazeState.mouseDirection`:

```
MOUSEWAY_F = 1 << ((mouseDirection + 0) % 4)   // 前 = 朝向
MOUSEWAY_R = 1 << ((mouseDirection + 1) % 4)   // 右 = 朝向 + 1
MOUSEWAY_B = 1 << ((mouseDirection + 2) % 4)   // 后 = 朝向 + 2
MOUSEWAY_L = 1 << ((mouseDirection + 3) % 4)   // 左 = 朝向 + 3
```

### 2.4 墙检测与双向同步(`MAZE_WallCheck`)

走满一格后由 `MAZE_CoorUpdate` 调用:

```
① 本格标记已访问: mapVisited[本格] = 1
② 组装 ucMap: 初始 = MOUSEWAY_B(后方恒有路)
   左/前/右传感器有墙 → 清对应 MOUSEWAY_L/F/R 位;无墙 → 置位
③ 整理: ucMap &= 0x0F(取低四位)
        ucMap |= ucMap << 4(低四位复制到高四位)
④ 本格首次写入(低四位 == 0):
   mapWall[本格] = ucMap
   向"有墙"方向的邻居清对应镜像位
```

"本格低 四位 + 邻居镜像位"双向同步,保证全开图等后续判墙一致。

## 3. 机制一:搜索过程怎么进行

搜索的目标是在未知迷宫中**边走边建图**,直到地图足够完整、路径足够短,才转入冲刺。整个过程由前台状态机驱动,真正的走格交给后台 ISR。

### 3.1 状态机驱动

- **STATE_Start**(起点判定):先走一格探测左/右出口,判起点在迷宫哪侧;若在右端,把墙图**水平镜像翻转**,使内部逻辑统一按"起点在左侧"工作,并登记首个岔路支点。
- **STATE_MazeSearch**(探索核心):每到一个决策格,分两种情况处理——已到终点区则记录终点并判转冲刺;未到终点则数可前进支路,做死路剪枝与岔路决策,走一格继续。
- **STATE_ReturnSearch**(返回途中搜索):已到过终点后,利用岔路栈 DFS 回溯把剩下没走的分支补搜完;每步重算连通性,一旦发现"补搜也改善不了路径"就提前收工。

### 3.2 单格走格(`SEARCH_Search`)

"走一格"实际是**走到下一个需要决策的格**:

```
motionCtrl.task = SMARTSTRAIGHT;
while(1){
  if (odomStr.cellOdom >= 180) {       // 到达逻辑节点(格中心)
    MAZE_CoorUpdate();                 // 坐标+1格, 并探测该格墙
    if (MAZE_BreakOrNot()==1) break;   // 是岔路/死路/拐角 → 在此决策
  }
  if (有侧墙) SEARCH_HasWall();        // 睁眼走(侧墙距离矫正)
  else        SEARCH_NoWall();         // 闭眼/沿柱走
}
```

- **`MAZE_BreakOrNot`** 判定"是否该在此停下做决策":前墙有墙(撞墙/终点)或左/右侧无墙(岔路/拐角)都返回 1;笔直走廊则继续走。
- 转弯后 `MOTION_MouseTurnRight/Left` 用 `MOTION_SetTrack(180 - 偏移)` **人为制造 cellOdom≈180**,让 `SEARCH_Search` 一进来就命中节点判断,补全转弯遗留路程。

### 3.3 探索决策(岔路 / 死路 / 回退)

| 函数 | 干什么 |
|------|--------|
| `EXPLORE_CrosswayCheck` | 数当前格"有路且未访问"的支路数 roadCount |
| `EXPLORE_DeadwayCheck` | **死路剪枝**:在图上临时封掉某支路重算全开图最短路,若终点变不可达则识别为必死分支,返回应回退的支点 |
| `EXPLORE_CrosswayChoice` | **岔路决策**:以终点为源重跑全开图最短路,取前/左/右邻居步数 sF/sL/sR 排序,选步数最小的贴墙法则(最短优先) |
| `EXPLORE_BackPointChoice` | 回退时优先选"未访问且仍朝目标推进"的岔路继续补搜 |

死路剪枝的巧妙处:**不必真的走过去**,在图上预演"封掉这条支路后还能不能到终点",识别必死分支后直接 `SPURT_ObjectGoTo` 跳回更早的安全支点,减少冗余物理探索。

### 3.4 何时转冲刺(搜索价值收敛判据)

搜索不是漫无目的走遍全图,而是用距离图判断"继续搜还有没有价值":

```
补搜价值: mapStepCloseCopy[支点] + mapStepOpen[支点] < mapStepClose[起点]
```

- `mapStepCloseCopy[支点]` = 全闭图等高图(沿已探索树到该支点的实际步数,上界);
- `mapStepOpen[支点]` = 全开图最短距离图(该支点到终点的理论最短步数,下界);
- `mapStepClose[起点]` = 当前已知的起点→终点路径长度。

只要"绕道补搜该支路可能得到的新路径"仍短于当前已知最优,就值得继续探索;一旦没有任何支点满足该不等式,继续搜已无优化空间 → 回起点冲刺。

两个直接出口条件:

```
mapStepClose[起点] <= stepMax(12)               // 起点到终点已足够短
|| mapStepClose[起点] - mapStepOpen[起点] == 0   // 全闭上界 == 全开下界:已探路线就是最优
```

第二个条件的物理意义:`mapStepClose[起点]` 是全闭图(未知按有墙)算出的**已探明路径长度上界**,`mapStepOpen[起点]` 是全开图(未知按无墙)算出的**理论最短下界**。上界 == 下界,说明当前已探明的路径就是理论最短,再无捷径可挖。

`taskSt.backSearch` 是"返回搜索/冲刺排程"标志:`backSearch==0` 直接冲刺(已知地图);非 0 则探完再冲,并支持"冲一程→回来补搜"的交替模式。

## 4. 机制二:最短路径如何处理

最短路径的核心是**双距离图 + 沿等高值递减回溯**。

### 4.1 双距离图(洪水填充)

| 距离图 | 生成函数 | 数据结构 | 等高值含义 | 判墙位 |
|--------|----------|----------|-----------|--------|
| mapStepClose | `FLOODFILL_Close` | 单栈 stackClose | 已填格数(贪心发现序) | 低 四位(OPEN)=全闭地图,已探明路径上界 |
| mapStepOpen | `FLOODFILL_Open` | 双栈 stackOpenNow/Next | 严格最短步数 | 高 四位(MIRROR)=全开地图,理论最短下界 |

- **全闭图等高图(`FLOODFILL_Close`)**:从目标格洪泛,每填一格 +1;死胡同弹栈回退,岔路入栈记录。判墙读低 四位(本格位),未访问格低 四位=0(未知按有墙),等高图仅沿已探明走廊扩展——它是"已探明路径长度(上界)"的度量,供 8 方向最短路回溯。
- **全开图最短距离图(`FLOODFILL_Open`)**:双栈逐层扩散,每层 +1。判墙读高 四位(镜像位),未访问格高 四位=0xF(未知按无墙),可借道未探索格——它是"理论最短(下界)"的度量,供岔路决策、死路剪枝、冲刺判据使用。

### 4.2 距离图副本的用途

搜索状态机反复以不同目标(起点/终点)调用 FloodFill 会**覆盖**距离图。`FLOODFILL_CloseCopy/OpenCopy` 把"以起点为目标"的结果备份到 `mapStepCloseCopy/mapStepOpenCopy`,供跨周期判定(§3.4 的补搜价值判据)。

### 4.3 8 方向最短路(`PATH_Fastway8Dir`)

允许 45° 斜切以缩短总里程:

- **方向放大 2 倍**做 8 方向:`mouseDir = mouseDirection * 2`(0=上/2=右/4=下/6=左,奇数 1/3/5/7 为斜向)。
- 从当前位置**沿等高值递减方向**回溯到目标,输出一串相对转向动作 `fastwayAction[]`。
- 相对转向公式:`action = (8 + 目标朝向 - 当前朝向) % 8`。

**action 物理含义(8 方向)**:`0=直行, 1=右转45°, 2=右转90°, 3=右转135°, 4=掉头180°, 5=左转135°, 6=左转90°, 7=左转45°`。

### 4.4 4 方向最短路(`PATH_Fastway4Dir`)

与 8 方向同构,但方向**不放大**(0/1/2/3),`action = (4 + 目标 - 当前) % 4`(`0=直行, 1=右转90°, 2=掉头, 3=左转90°`)。产物存 `fastwayAction90[]`,供 90° 冲刺。

### 4.5 回程路径

`PATH_BackPath`(去程倒序 + 左右镜像)是保留的参考实现,**当前无调用**;实际回程由 `STATE_Spurt45/90` 用 `PATH_Fastway4Dir(xStart, yStart)` 在终点重新计算。

## 5. 探索法则(贴墙 / 岔路 / 死路)

### 5.1 六种贴墙法则

公共助手 `EXPLORE_WallFollowStep`:若该方向**有路且未走过**则按 turnCode 转向。六个法则只是三方向**检查优先级**不同:

| 函数 | 优先级 | 语义 |
|------|--------|------|
| EXPLORE_WallFollowRight | 右 → 前 → 左 | 右手法则 |
| EXPLORE_WallFollowRightLeft | 右 → 左 → 前 | 右左 |
| EXPLORE_WallFollowLeft | 左 → 前 → 右 | 左手法则 |
| EXPLORE_WallFollowLeftRight | 左 → 右 → 前 | 左右 |
| EXPLORE_WallFollowFrontRight | 前 → 右 → 左 | 中右法则 |
| EXPLORE_WallFollowFrontLeft | 前 → 左 → 右 | 中左法则 |

### 5.2 中心法则(`EXPLORE_CentralMethod`)

按鼠所在象限 × 当前朝向,从六个法则中选一,让探索轨迹形成**螺旋贴墙**,始终贴着外侧墙走,减少迷宫中央绕圈,使全闭图等高图更规整。当前默认 `SEARCHMETHOD = CENTRALMETHOD`。

### 5.3 岔路决策(`EXPLORE_CrosswayChoice`)

以目标格为源重跑全开图最短路,取前/左/右三邻格步数 sF/sL/sR,按大小关系落入六法则(§3.3 表),即"**优先朝步数最小方向走**",让探索兼顾"尽快接近终点"与"贴墙法则的确定性"。

### 5.4 死路剪枝(`EXPLORE_DeadwayCheck`)

见 §3.3——图上封路预演识别必死分支,返回更早的安全支点,减少冗余探索。

## 6. 机制三:冲刺做了哪些优化保证冲刺速度

冲刺阶段目标是**在已建图上用最短时间跑完去程/回程**,为此做了层层优化(规划 → 执行 → 控制 → 速度):

### 6.1 规划层:斜切 + 动作合并

- **8 方向斜切**(`PATH_Fastway8Dir`):允许 45° 对角穿越,比 4 方向直角绕行**总里程更短**(规划层节时)。
- **动作合并**(`SPURT_ActionGenerate`):把 8 方向 fastway 的连续小转角在规划阶段就合并成一次大角度转弯,减少转弯次数与转弯间的加减速停顿。用 `SPURT_GetSum(i) % 2` 判当前朝向奇偶(基本方向/斜向)决定合并方式:

| fastway 动作窗口 | 朝向奇偶 | 输出动作码 | 说明 |
|-----------------|----------|-----------|------|
| 1, 2, 1 | 偶 | R180L | 一次原地回转代替三次分步转向 |
| 7, 6, 7 | 偶 | L180L | 同上 |
| 2, 1 | 奇 | RV135L | 斜向大角 |
| 1, 2 | 偶 | RT135L | 基本方向大角 |
| 1, 1 / 7, 7 | 偶 | RT90L / LT90L | 两次 45° 合并为 90° |
| 1 / 7 | 偶/奇 | RT45L / RV45L 等 | 单步小角 |
| 0 | 偶/奇 | STRAIGHT90L / STRAIGHT45L | 直行分类(基本/斜向) |

### 6.2 执行层:直行合并 + 转弯查表前馈

- **连续直行合并**(`SPURT_Action`):整段直道只在进入 `MOTION_Straight90(180 * num)` / `MOTION_Straight45(127 * num)` 时启动一次任务,`while(cellOdom < dis)` 内持续高速跑,**不在每个格子边界停车判墙**,消除搜索模式的"每格决策开销"。
- **转弯查表前馈**(`MOTION_Turn` + `turnParamTable`):转弯不再靠反馈慢慢逼近目标角,而是用**赛场预整定的前馈曲线**(speedtable.c 的逐 tick `gyro/vl/vr` 表)直接"给定"全程角度与轮速轨迹,只留 PD 小闭环修残余误差。转弯时间压到查表长度级(0.18–0.39s)。
- **冲刺专用转弯表**:90° 冲刺用 `LT90SPURT/RT90SPURT` 映射 `gyroLSpurt/gyroRSpurt/vouterSpurt/vinnerSpurt`,为冲刺速度单独整定,比搜索转弯表更快。

### 6.3 直线控制:睁眼 / 闭眼自适应切换

`MOTION_Straight90` 在每个 `cellOdom % 180` 周期内:

- 格中间(20–100 段):**闭眼**(`SPURT90BLIND`),只靠陀螺角 PD,避开传感器响应时延与近墙干扰,全速盲走;
- 墙沿附近:开红外(**睁眼** `SPURT90IR`),懒惰贴墙侧 + 大小误差分档增益(irKp1/irKp2),只在需要对齐时才矫正。

把"能闭眼就跑全速"与"需要墙就对齐"折中,最大化无传感器高速段。

### 6.4 速度:加减速斜坡 + 档位增益 + 高频闭环

- **加减速斜坡**:`MOTION_Straight90CalcSpeed` 梯形速度曲线(加速 `accRatio*里程`、巡航、减速 `decRatio*(dis-里程)+bottom`,限幅 `[10, topSpeed]`);`MOTION_Acc/Stop` 起步/刹车斜坡。让全速段占比最大化又不超电机能力。
- **三档速度与增益**(`SPURT_GainSet` L/M/H):高档转弯 P/D 更大、直行 45 P/D 更大,匹配更高车速的响应需求。
- **电机 1ms 轮速闭环**:`MOTOR_EncSample` + `MOTOR_LoopControl` 每 1ms 设固定增益跑 PID 到 PWM,保证高速稳定。
- **转弯前预对齐**:90° 冲刺首弯与每个直段后的 `SMARTSTRAIGHT` 预对齐;`BEFORE90TURN` 用 `turnDelay` 决定入弯点;`SPURT_TurnRight/Left` 依侧墙距差算入弯偏移,保证高速入弯后正对。

## 7. 机制四:45° 冲刺是如何实现的

45° 冲刺允许鼠**斜穿迷宫格**,斜格里程比直角绕行短,配合高速双项误差闭环,是缩短冲刺时间的关键。

### 7.1 斜格运动(`MOTION_Straight45`)

- 设任务码 `STRAIGHT45 = 17`,`while(odomStr.cellOdom < dis)` 持续跑。
- **每斜格里程常数 `DIAGONAL_HALF_CELL_STEPS = 127`**(= 180/√2,斜格半对角线),调用方传入 `127 * num`。
- 45° 档速度曲线:仅当 `speedmode == SPURT_SPEED_45_LOW` 且 `dis > 180` 时,按 `cellOdom` 三分段写 `speedCfg.fastSprint`:前 90 步线性升(`l45BottomSpeed→l45TopSpeed`)、中段保持顶速、末 90 步线性降;`dis <= 180` 恒为底速。

### 7.2 斜线误差闭环(`doTaskStraight45`)

45° 斜跑时侧向没有正交墙可依,普通侧墙矫正会失效。`doTaskStraight45` 每 1ms 把**斜向侧传感器与前方传感器**组合成双项误差:

- 左侧有墙:`pidErr = abovezero(irDist.l + 50 - irDist.f1) + abovezero(180 - irDist.f1)`
  - **第一项代表角度偏差,第二项代表横向(纵向)坐标偏差**;pidErr 越大说明车越左偏。
- 右侧有墙:镜像(用 `irDist.r` 与 `irDist.f2`)。
- 两侧都无墙:`pidErr = abovezero(220 - irDist.f2) - abovezero(220 - irDist.f1)`(前双传感器差对正)。

一次 PD 输出(`speedTargetL/R = fastSprint ∓ pidErr*straight45P ∓ ΔpidErr*straight45D`)同时纠正**航向和位置**,使鼠能以接近顶速斜穿而不必减速。

### 7.3 触发与档位

- `STATE_LoadMapAndSelectSpeed`:按前方红外遮挡选 45°(f1 遮挡)或 90°(f2 遮挡),并按二次遮挡选低/高速(45° 低 `lTopSpeed=200/l45TopSpeed=160`,高 `lTopSpeed=240/l45TopSpeed=180`)。
- `STATE_Spurt45`:用 `PATH_Fastway8Dir` 生成 8 方向斜切去程存入 Flash,`SPURT_GainSet(spurtmode)` 执行;`goaheadnum==3/4` 时抬速(240/180)触发超高速斜线冲。

## 8. 运动状态机(SysTick 1ms)

### 8.1 任务码分类(`MotionTask_t`)

| 分类 | 任务码 | 说明 |
|------|--------|------|
| 基础运动 | GOSTRAIGHT=1 / TURN=2 / TURNAROUND=4 / STOP=5 / STRAIGHT45=17 | 直线/转弯/原地180/刹车/45° |
| 转弯前后 | BEFORE90TURN=27 / BEFORE180TURN=28 | 转弯前接近与定位 |
| 智能直行/变速 | SMARTSTRAIGHT=30 / SMARTSTRAIGHTFAST=32 / STRAIGHTEYECLOSECOORADJ=33 / STRAIGHTPOLE=34 | 侧墙/陀螺纠偏、搜索直线、闭眼矫正、沿杆 |
| 加减速/贴墙/点转 | ACC=35 / SLOWDOWN=36 / SPEEDUP=37 / KEEPWALLDISTANCE=38 / POINTTURN90=39 / FORWARDIRTEST=41 | 起步/减速/提速/贴墙/原地90/前向对正 |
| 冲刺 | SPURT90BLIND=42 / SPURT90IR=43 / SMARTSTRAIGHTSPURT=44 / SMARTSTRAIGHTSPURTL=45 | 90°冲刺睁眼/闭眼、转弯前补全 |
| 搜索/冲刺查表模式码 | 101–107(LT90SEARCH / RT90SEARCH / POINTR90 / POINTL90 / STRAIGHT_spurt_90 / LT90SPURT / RT90SPURT) | **非 motionCtrl.task**,是转弯查表模式码 |

### 8.2 SysTick 1ms 调度流水线

```mermaid
graph LR
    S["SysTick 1ms"] --> G["GYRO_Sample 陀螺角度梯形积分"]
    G --> O["ODOM_Sample 里程/角度(编码器回绕)"]
    O --> W["doWallDetect 红外墙判"]
    W --> T["switch(motionCtrl.task) → doTask* 设目标速度"]
    T --> P["MOTOR_EncSample + 速度环 PID → PWM"]
    P --> F["SAFETY_Monitor 急停/电池/存储/CPU"]
```

### 8.3 主循环阻塞等待模式

运动层函数设任务码后自旋轮询完成信号,ISR 推进:

| 函数 | 等待条件 | 说明 |
|------|----------|------|
| MOTION_Acc | `while(odomStr.cellOdom < accdistance)` | 起步加速至目标距离 |
| MOTION_Stop | `while(motionCtrl.tick < 5)` | 刹车(超时 stoptick > 1000) |
| MOTION_Turn | `while(motionCtrl.tick < length/2 - 2)` | 表驱动转弯按节拍推进 |
| MOTION_TurnAround / PointTurnExec | `while(1) if(motionCtrl.step==1 && tick>10) break` | 原地转向两阶段 |
| SEARCH_Search | `while(1) if(odomStr.cellOdom>=180) {若 MAZE_BreakOrNot() 则 break}` | 走满一格(180mm)决策 |
| MOTION_Straight45/90 | `while(odomStr.cellOdom < dis)` | 每拍切换睁眼/闭眼直线 |

## 9. 关键常量速查

| 常量 | 值 | 含义 |
|------|-----|------|
| MAZETYPE | 7(设计 16) | 当前构建 7×7 四分之一迷宫 |
| DIAGONAL_HALF_CELL_STEPS | 127 | 斜格半对角线(45° 冲刺每格, 180/√2) |
| stepMax / stepMaxLimit | 12 / 12 | 搜索步数阈值(起点→终点够短可冲刺) |
| WALL_SIDE_THRESHOLD | 160 | 侧墙判定 |
| WALL_45_THRESHOLD | 150 | 45° 侧墙判定 |
| WALL_CRITICAL_THRESHOLD | 60 | 临界墙距(斜线转弯起始点) |
| WALL_FRONT_* | 230/240/245/80 | 前墙远/近判定 |
| spurtSpd.l45TopSpeed / l45BottomSpeed | 160 / 100 | 45° 冲刺顶/底速(低档) |
| spurtSpd.l90TopSpeed / l90BottomSpeed | 150 / 120 | 90° 冲刺顶/底速(低档) |

## 附录:已知代码事实

- **MAZETYPE=7**(maze.h:65):当前构建为 7×7 四分之一迷宫,终点 (6,6);算法按 16×16 设计,`MAZETYPE` 可改 8 或 16。
- **PATH_BackPath 为死代码**:当前无调用;实际回程由 `PATH_Fastway4Dir(xStart,yStart)` 重算。
- **SPURT_ActionGenerate90 混用数组**(spurt.c):其直行段读 `mazeState.mapFastway/fastwayAction`(8 向)而输入路径是 `fastwayAction90[]`(4 向),存在潜在不一致(已知问题,课程化构建下未触发错误)。
