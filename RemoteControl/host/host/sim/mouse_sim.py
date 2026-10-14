"""电脑鼠设备模拟器 —— 在没有硬件时让上位机完整跑通。

它按与固件 HARDWARE/link.c **完全相同的字节级协议**回话:
    - 1kHz 内部节拍; 收到 `TELEM` 就回一帧统一帧(LINK_TYPE_UNIFIED, 45B payload)
    - 响应全部文本命令(ACK/GET/SET/SETQ/SAVE/RESET/TARE/STOP/ESTOP/TELEM/MAP/MOVE/CAL/ARM/MAZE)
    - 上行异常事件只有两条: `STOPPED <reason> #n` / `ESTOP TRIP #n`
    - 复刻 Flash 参数页的 magic/version/CRC 校验与"校验失败即忽略"的降级行为

v2.1(2026-10-05, **真机实测后**) 与 v2.0 的关键差别:
    * 遥测由**固件自时钟持续流**改为**PC 拉取**(`TELEM` 请求-响应) ——
      真机实测那条蓝牙链路只能承载约 100~200 B/s, 自时钟的 4.3 KB/s 会把
      模块缓冲灌死且不自恢复; 拉取模式下主机一停请求就自然排空;
    * `STREAM ON` / `STREAM OFF` 与固件侧开流状态**删除**;
    * 时间锚改由 `TELEM` 的应答携带(每次拉取都刷新)。

v2.0(2026-10-05) 与旧版的关键差别(见《传输模式重构设计方案》):
    * 快/中/慢三帧 + TIME 帧 + MAZE 帧 **合并为一种**统一帧;
    * `SNAP` / `SCOPE` / `RATE` / `VER` / `MAZE EV` / `MAZE RST` **全部删除**;
    * 标定改**请求-响应**: `CAL <item> START` 不复立刻回 ACK, 而是等这一步
      采集完成才回(ACK 即"请摆下一个位置");
    * `TELEM` / `ARM` 的应答**带附加数值**(时间锚 / 发车时基);
    * 帧与事件**共用**一个 1 字节上行序号, 主机据此检测丢失。

用途有二:
    1. 上位机开发/演示(现在没有硬件也能验证全链路)
    2. 协议回归: selftest.py 可以把它拉起来做端到端断言

用法:
    python -m host.sim.mouse_sim               # 默认监听 127.0.0.1:5555
    python -m host.sim.mouse_sim --port 5556 --no-auto
    python -m host.sim.mouse_sim --rtt-ms 300  # 注入 ~300ms 往返延迟, 逼近真机蓝牙链路
"""

from __future__ import annotations

import argparse
import random
import socket
import struct
import threading
import time
from typing import Dict, List, Tuple

from ..protocol.codec import (
    FLAG_EMERG,
    FLAG_ESTOP_LOCK,
    FLAG_EVQ,
    FLAG_FREEMODE,
    FLAG_MOTOR_OFF,
    FLAG_PULLF1,
    FLAG_SLEEP,
    MAZE_TASK_NAMES,
    ROAD_NAMES,
    TYPE_MAP,
    TYPE_UNIFIED,
    UNIFIED_LEN,
    crc16_modbus,
    pack_frame,
    pack_unified,
)

MAZETYPE = 7
PROTO_VER = 8  # v2.7: CAL 家族新增 6 个菜单远程化子命令(帧格式与参数表未变); v2.8 亦未变
FW_VER = 221  # v2.8: 远程档 MAZE RUN 瞬间点亮全部 4 个 LED(LINK_LAUNCH_MARK_MS=300ms)后释放

# 参数表(与固件 id 对齐); 初值 = 固件 ISR_Init 里的源码默认值
PARAM_DEFAULTS: Dict[int, float] = {
    # ---- 生产标定(1..7, 任何固件都有) ----
    1: 62.59 * 0.85,  # TRACKL
    2: 62.68 * 0.85,  # TRACKR
    3: 0.6259 * 0.85,  # SPEEDL
    4: 0.6268 * 0.85,  # SPEEDR
    5: 399.0,  # GYRORATIO (u16 截断后的值)
    6: 67.0,  # ENCANGLE  (u16)(75*0.90)
    7: 42.0,  # WALLDIST
    # ---- 红外测量与阈值(22-32, 生产命名空间; 默认值 = 固件 core.h 的原宏值) ----
    22: 160.0,  # IRTHRWALL  (WALL_SIDE_THRESHOLD)
    23: 150.0,  # IRTHR45    (WALL_45_THRESHOLD)
    24: 60.0,   # IRTHRCRIT  (WALL_CRITICAL_THRESHOLD)
    25: 230.0,  # IRTHRFLO   (WALL_FRONT_FAR_LO)
    26: 240.0,  # IRTHRFMID  (WALL_FRONT_FAR_MID)
    27: 245.0,  # IRTHRFHI   (WALL_FRONT_FAR_HI)
    28: 80.0,   # IRTHRFNEAR (WALL_FRONT_NEAR)
    29: 100.0,  # IRKALQ     (0.0100 ×10000)
    30: 2000.0,  # IRKALP    (0.2000 ×10000)
    31: 1.0,    # IRKALSW      (卡尔曼开)
    32: 0.0,    # IRFRONTMODE  (0=均值)
    # ---- 电源监控(33-38; 默认值 = 固件 safety.c 原写死值的等价表示) ----
    33: 2417.0,  # BATTKV     (µV/LSB = 3.3V×3÷4096×1000)
    34: 7000.0,  # BATTLOWMV
    35: 7960.0,  # BATTHIGHMV (7000 + 8×120mV, 与原式 (volt-700)/12 一致)
    36: 100.0,  # BATTALPHA   (0.100 ×1000)
    37: 8.0,    # BATTMAXPCT
    38: 1.0,    # BATTMINPCT
    # ---- 运动/控制参数(39-55, v2.5; 默认值 = 各编译期常量) ----
    39: 145.0,  # TRACKCOORD (sysCfg.trackCoord)
    40: 16.5,  # TURNDELAY  (turnDelay)
    41: 95.0,  # SPDSEARCH  (speedCfg.search)
    42: 60.0,  # SPDTURN    (speedCfg.turn)
    43: 60.0,  # DISF60     (原 isr.c 字面量 60)
    44: 0.4,   # GSKP       (pidGain.goStraight)
    45: 5.0,   # TURNP      (pidGain.turnP)
    46: 4.5,   # TURND      (pidGain.turnD)
    47: 2.5,   # PTKP       (TURN_PID_KP)
    48: 0.01,  # PTKI       (TURN_PID_KI)
    49: 0.1,   # PTKD       (TURN_PID_KD)
    50: 10.75,  # MKPL      (MOTOR_KP_L)
    51: 0.55,  # MKIL       (MOTOR_KI_L)
    52: 0.55,  # MKDL       (MOTOR_KD_L)
    53: 10.20,  # MKPR      (MOTOR_KP_R)
    54: 0.5,   # MKIR       (MOTOR_KI_R)
    55: 0.5,   # MKDR       (MOTOR_KD_R)
}
PARAM_NAMES = {
    1: "TRACKL",
    2: "TRACKR",
    3: "SPEEDL",
    4: "SPEEDR",
    5: "GYRORATIO",
    6: "ENCANGLE",
    7: "WALLDIST",
    22: "IRTHRWALL",
    23: "IRTHR45",
    24: "IRTHRCRIT",
    25: "IRTHRFLO",
    26: "IRTHRFMID",
    27: "IRTHRFHI",
    28: "IRTHRFNEAR",
    29: "IRKALQ",
    30: "IRKALP",
    31: "IRKALSW",
    32: "IRFRONTMODE",
    33: "BATTKV",
    34: "BATTLOWMV",
    35: "BATTHIGHMV",
    36: "BATTALPHA",
    37: "BATTMAXPCT",
    38: "BATTMINPCT",
    39: "TRACKCOORD",
    40: "TURNDELAY",
    41: "SPDSEARCH",
    42: "SPDTURN",
    43: "DISF60",
    44: "GSKP",
    45: "TURNP",
    46: "TURND",
    47: "PTKP",
    48: "PTKI",
    49: "PTKD",
    50: "MKPL",
    51: "MKIL",
    52: "MKDL",
    53: "MKPR",
    54: "MKIR",
    55: "MKDR",
}
NAME_IDS = {v: k for k, v in PARAM_NAMES.items()}

#: **全部参数** —— 与固件 `linkParam[LINK_P_NUM]`(远程档=41)一一对应。
#: ⚠️ 这不是"随便挑一段": 固件的 `GET ALL` 只遍历这张表, `GET <name>` 也只查它。
#: v2.4: 课程参数(8..21)随课程层删除; v2.5: 运动控制段(39..55)入表。
PRODUCTION_IDS: Tuple[int, ...] = (
    tuple(range(1, 8)) + tuple(range(22, 39)) + tuple(range(39, 56))
)

UP, RIGHT, DOWN, LEFT = 0, 1, 2, 3
DIR_DELTA = {UP: (0, 1), RIGHT: (1, 0), DOWN: (0, -1), LEFT: (-1, 0)}


def _q(v: float) -> int:
    s = v * 65536.0
    return int(s + 0.5) if s >= 0 else -int(-s + 0.5)


def _i16(v) -> int:
    """钳位到 int16 —— 真实固件字段是 s16, 越界会截断/回绕而非抛异常。

    模拟器必须同样防御: 否则一次异常输入(如过大的转速)会让 struct.pack 抛错,
    导致连接处理被中断、后续命令全部得不到应答(实机上表现为"突然全超时")。
    """
    v = int(v)
    if v > 32767:
        return 32767
    if v < -32768:
        return -32768
    return v


# ---------------------------------------------------------------- 迷宫

def build_maze(n: int = MAZETYPE, seed: int = 20260925) -> Tuple[List[List[int]], List[List[int]]]:
    """返回 (vwall, hwall)。vwall[x][y] = (x,y) 与 (x+1,y) 之间有墙; hwall 同理纵向。"""
    rnd = random.Random(seed)
    vwall = [[1 for _ in range(n)] for _ in range(n - 1)]
    hwall = [[1 for _ in range(n - 1)] for _ in range(n)]
    # 用随机 DFS 生成完美迷宫, 再随机打通一些墙制造环路与岔路
    visited = [[False] * n for _ in range(n)]
    stack = [(0, 0)]
    visited[0][0] = True
    while stack:
        x, y = stack[-1]
        nbrs = []
        for d in (UP, RIGHT, DOWN, LEFT):
            dx, dy = DIR_DELTA[d]
            nx, ny = x + dx, y + dy
            if 0 <= nx < n and 0 <= ny < n and not visited[nx][ny]:
                nbrs.append((d, nx, ny))
        if not nbrs:
            stack.pop()
            continue
        d, nx, ny = rnd.choice(nbrs)
        if d in (UP, DOWN):
            yy = min(y, ny)
            hwall[x][yy] = 0
        else:
            xx = min(x, nx)
            vwall[xx][y] = 0
        visited[nx][ny] = True
        stack.append((nx, ny))
    for _ in range(6):  # 打通若干墙形成环路
        if rnd.random() < 0.5 and n > 1:
            x = rnd.randrange(n - 1)
            y = rnd.randrange(n)
            vwall[x][y] = 0
        else:
            x = rnd.randrange(n)
            y = rnd.randrange(n - 1)
            hwall[x][y] = 0
    return vwall, hwall


def wall_byte(vwall, hwall, x: int, y: int, n: int) -> int:
    b = 0
    if y + 1 < n and not hwall[x][y]:
        b |= 0x01  # UP 可通行
    if x + 1 < n and not vwall[x][y]:
        b |= 0x02  # RIGHT
    if y - 1 >= 0 and not hwall[x][y - 1]:
        b |= 0x04  # DOWN
    if x - 1 >= 0 and not vwall[x - 1][y]:
        b |= 0x08  # LEFT
    return b


# ---------------------------------------------------------------- 模拟器

class MouseSim:
    def __init__(self, auto_explore: bool = True, seed: int = 20260925,
                 safe: bool = False, rtt_ms: float = 0.0) -> None:
        self.n = MAZETYPE
        self.vwall, self.hwall = build_maze(self.n, seed)
        self.auto_explore = auto_explore
        #: 安全测试模式: 模拟"固件已禁用电机"的板子(flags 置 MOTOR_OFF, 速度恒 0)
        self.safe = safe
        #: 命令→应答的整趟往返延迟(秒)。模拟真机的蓝牙/CH340 转发 —— 默认 0(亚毫秒)。
        #: 真机实测 RTT ≈ 300 ms; 上位机里"按快链路写的时序假设"(例如连接握手只
        #: sleep 50 ms 就取设备指纹)只有在注入这个延迟后才能离线复现,
        #: 见 `facade_smoke.py --rtt-ms`。
        self.rtt_s = max(0.0, float(rtt_ms) / 1000.0)

        # 运行时参数
        self.params: Dict[int, float] = dict(PARAM_DEFAULTS)
        self.flash_valid = False  # 出厂未标定 -> 参数页 magic 不匹配

        # 运动状态
        self.speed_target_l = 0.0
        self.speed_target_r = 0.0
        self.speed_l = 0.0
        self.speed_r = 0.0
        self.gyro = 0.0  # deg
        self.angle_acc = 0.0
        self.dist_l_mm = 0.0
        self.dist_r_mm = 0.0
        self.run_dist_l = 0.0
        self.run_dist_r = 0.0
        self.enc_l = 0.0
        self.enc_r = 0.0
        self.enc_diff_acc = 0.0
        self.cell_odom = 0.0
        self.motion_task = 0
        self.motion_step = 0
        self.pid_err = 0.0
        self.volt = 7.62
        self.batt_mv = 7620
        self.cpu = 18
        self.emerg = False
        self.freemode = 0
        self.sleepmode = 0

        # 迷宫位置
        self.pos = (0, 0)
        self.dir = UP
        # 上电处于 WAIT(=0), 与固件一致 —— 固件在 main() 里才把 mouseTask 推到
        # START/MAZESEARCH; 这样"进入迷宫态"的边沿才会出现。
        self.mouse_task = 0  # WAIT
        self.goal = (7, 7)  # 未设定(与固件 goalX/goalY 初值 7 同语义)
        self.visited = [[False] * self.n for _ in range(self.n)]
        self.visited[0][0] = True
        self.wall_known = [[False] * self.n for _ in range(self.n)]
        self.map_wall = [[0] * self.n for _ in range(self.n)]

        # 红外的黑盒探针(用墙距生成, 让波形可信)
        self.ir = [230.0, 230.0, 230.0, 230.0]  # F1 F2 L R
        self.wall_bits = 0  # 位: 1=左墙 2=右墙 4=前墙(与固件 wallSns 同语义)
        self.wall_thresh = int(PARAM_DEFAULTS[22])

        # 命令状态
        self.last_seq = -1
        self.last_crc = 0
        self.last_status = "OK"
        # (v2.1 起没有"开流"状态 —— 遥测完全由 TELEM 请求驱动, 见 handle_line)
        #: TELEM 的二进制帧旁路(与 map_reply 同构): 主循环会先发它再发文本 ACK。
        self.telem_reply: bytes | None = None
        #: 速率族(v2.6)自时钟产生的待发帧(与 telem_reply 同构, 由服务循环搬走)。
        self.rate_frames: List[bytes] = []
        self.rate_on = False   # 收到 RATE FIX / RATE PULL 后为真
        self.rate_fix_hz = 0   # RATE FIX 的目标帧率(整数 Hz)
        self.rate_acc = 0      # 固定节拍的组帧累加器(Q8.8, 每 ms 加 hz*256)
        self.pull_on = False   # PULL 批量节拍模式
        self.pull_count = 0    # 本批剩余待发帧数
        self.pull_period = 0   # 批内帧间隔(ms)
        self.pull_timer = 0    # 距下一帧的倒计时(ms)
        self.pull_f1 = False   # v2.6.1: 正在组的这一帧是否为"收到 PULL 后发出的第一帧"
        self.tick = 0
        self.txbuf = 1024  # v2.5: 41 项参数的 GET ALL ≈ 700B, 512 已不够(与固件远程档同步)

        #: 上行序号 —— **帧与事件共用**(与固件 linkSeq 同语义), 1 字节循环。
        #: 主机比较相邻两条上行消息的序号差即可检测丢失; 不做 ACK、不重发。
        self.up_seq = 0
        self.ev_drop = 0  # 因空间不足被丢弃的事件条数 -> flags 的 EVQ

        # 运动驱动
        self.move_mode = "NONE"  # NONE / FWD / ROT / RUN / SERVO / EXPLORE
        self.move_pulses = 0
        self.move_start_l = 0.0
        self.move_start_r = 0.0
        self.move_until = 0.0
        self.rot_target = 0.0

        #: 待补发的事件行(在 1ms 节拍里产生, 由服务循环搬到 tx)。
        self.ev_out: List[str] = []

        # ---- MOVE RUN: 时间 + 红外 双护栏(与固件 link_MoveService 对齐) ----
        self.run_limit = 0        # 最长运行时间(ms)
        self.run_elapsed = 0      # 已运行(ms)
        self.run_ir_mm = 0        # 红外停车阈值(mm); 0 = 关闭
        self.run_ir_min = 1 << 30  # 本次最小红外(初值取哨兵, 第一个 tick 就会覆盖)
        self.run_ir_which = 0     # 0=F1 1=F2 2=L 3=R
        self.run_start_l = 0.0
        self.run_start_r = 0.0
        self.run_vmax_l = 0
        self.run_vmax_r = 0
        self.estop_lock = False   # 与固件 linkEStop 对齐

        # 探索驱动
        self.phase = "CELL"
        self.phase_dist = 0.0
        self.turn_target_dir = UP

        # ---- 时间同步 / 发车(与固件 link.c 的 ARM / MAZE RUN 对齐) ----
        self.link_time_ms = 0     # 上电以来的毫秒时基(u32)
        self.arm_ms = 0           # 发车基准; 0 = 未武装
        self.armed = False
        self.time_led = 0         # 1Hz 时间基准灯电平
        self.maze_running = False
        self.maze_requested = False
        #: 发车目标(`MAZE RUN [BLIND|RACE]`)—— v2.4 新增, 与固件 linkMazeTarget 对齐
        self.maze_target = "BLIND"

        # ---- 标定(请求-响应, 与固件 LinkCalState 对齐) ----
        #: 挂起的命令 seq; -1 = 无。命令到达时不立刻 ACK, 采集完成才由
        #: `_cal_finish()` 回一次(见《方案》§9)。角度标定带 3 个附加数值。
        self.cal_ack_seq = -1
        self.cal_item = None
        self.cal_phase = 0
        self.cal_ticks = 0
        self.srv_start = 0.0

        # ---- v2.7 菜单远程化(立即 ACK, 与固件 link_CalMenuRemote 对齐) ----
        #: IRCAP 序列进度(0 = 未开始/已完成); ERASEIR 与第 4 步完成时清零
        self.ir_cap_step = 0
        #: IRDATA 页 8 个半字(擦除态全 0xFFFF; 固件上电默认即此)
        self.ir_page = [0xFFFF] * 8
        #: 去/回路径 4 页(GOPATH/BACKPATH/GOPATH_90/BACKPATH_90)的有效项数,
        #: 只服务 `CAL BLOCK` 的概览(ERASEALL 清零)
        self.path_counts = [0, 0, 0, 0]

        # 复位原因(由 PING 上报; 模拟器固定 POR —— 真机上可能被 IWDG/SW 改写)
        self.reset_cause = "POR"

        self.lock = threading.Lock()
        self.map_reply: "bytes | None" = None

    # ---------------------------------------------------------- 物理步进

    def step(self) -> None:
        with self.lock:
            self._step_locked()

    def _step_locked(self) -> None:
        self.tick += 1
        self.link_time_ms += 1  # 毫秒时基(与固件 linkTimeMs 同语义: u32, 不回绕)
        # 1Hz 时间基准灯: 50% 占空比(与固件 link_TimeLed 一致)
        if self.armed:
            self.time_led = 1 if ((self.link_time_ms - self.arm_ms) % 1000) < 500 else 0

        if self.emerg:
            self.speed_target_l = self.speed_target_r = 0.0

        if self.safe:
            # 安全测试模式: 固件电机被物理禁用 —— 速度目标归零、电机处于自由模式,
            # 板子不动但传感器数据(陀螺零偏/红外/电压)照常刷新。
            self.speed_target_l = self.speed_target_r = 0.0
            self.freemode = 1

        # 一阶惯性逼近目标(时间常数约 4ms, 与实测曲线形态接近)
        self.speed_l += (self.speed_target_l - self.speed_l) * 0.22
        self.speed_r += (self.speed_target_r - self.speed_r) * 0.22

        # 陀螺: 顺时针为正(右轮前进为正)
        dt_deg = (self.speed_r - self.speed_l) * 0.00716
        self.gyro += dt_deg
        self.angle_acc += dt_deg * self.params[5]  # angleAcc = 角度 * gyroRatio
        # 编码器
        dl = self.speed_l / 100.0  # cm/s -> mm/ms
        dr = self.speed_r / 100.0
        self.dist_l_mm += dl
        self.dist_r_mm += dr
        self.run_dist_l += dl
        self.run_dist_r += dr
        self.enc_l += dl * self.params[1]
        self.enc_r += dr * self.params[2]
        self.enc_diff_acc += (dl - dr) * self.params[6]

        # 电压缓慢下降 + 纹波; 经一阶 IIR 得 battMv(与固件 safety.c 同构)
        self.volt = max(6.9, self.volt - 1e-6) + random.uniform(-0.003, 0.003)
        alpha = max(0.0, min(1000.0, self.params[36])) / 1000.0
        self.batt_mv = int(round(self.batt_mv * (1.0 - alpha) + self.volt * 1000.0 * alpha))
        self.cpu = int(max(8, min(60, self.cpu + random.uniform(-1.5, 1.5))))

        # 标签里程(相对)
        if self.phase == "CELL":
            self.cell_odom = self.phase_dist
        else:
            self.cell_odom = 0.0

        self._drive()
        self._update_ir()
        self._cal_tick()
        self._rate_tick()

    # ---------------------------------------------------------- 运动驱动

    def _rate_tick(self) -> None:
        """速率族的每 1ms 服务 —— 与固件 `link_RateService` 同构。

        未开流时为空操作。PULL 按 PC 给的 `n` / `t_ms` 连发后静默等下一条;
        FIX 按恒定 F 连续发。v2.1 起"自时钟"只在 PC 显式下发 RATE FIX / PULL 之后
        才存在(平时遥测仍完全由 TELEM 请求驱动)—— 这正是 MTBF 压测与事件触发式
        拉取需要的那条路径, 因此这里不能省: 否则模拟器上无法验证 `rate.*` 三条
        登记命令(契约要求"每个登记项都必须能被模拟器实际执行")。
        """
        if not self.rate_on:
            return
        if self.pull_on:
            if self.pull_count <= 0:
                return  # 本批已发完: 静默等下一条 PULL
            # 批首帧判据 —— 与固件 link_RateService 逐步同构: link_PullSet 把 timer 置 0,
            # 而每发一帧都会重置为 pull_period(>=1) ⇒ "带 timer==0 进来"当且仅当本批第 1 帧。
            # ⚠️ 必须在**倒计时之前**取值(批内末帧的 timer 恰好减到 0, 事后再看就分不清了)。
            first_frame = self.pull_timer == 0
            if self.pull_timer > 0:
                self.pull_timer -= 1
                if self.pull_timer > 0:
                    return
            self.pull_f1 = first_frame          # 只影响本帧组帧, 组完立刻清(与固件同序)
            self.rate_frames.append(self._unified_frame())
            self.pull_f1 = False
            self.up_seq = (self.up_seq + 1) & 0xFF
            self.pull_count -= 1
            self.pull_timer = self.pull_period
            return
        self.rate_acc += self.rate_fix_hz * 256
        if self.rate_acc >= 256000:  # 满刻度 = 1 帧/ms(与固件 LINK_RATE_FRAME_UNIT 同口径)
            self.rate_acc -= 256000
            self.rate_frames.append(self._unified_frame())
            self.up_seq = (self.up_seq + 1) & 0xFF

    def _drive(self) -> None:
        if self.move_mode == "FWD":
            d_l = self.dist_l_mm - self.move_start_l
            d_r = self.dist_r_mm - self.move_start_r
            if abs(d_l) >= self.move_pulses / self.params[1] or abs(d_r) >= self.move_pulses / self.params[2]:
                self.speed_target_l = self.speed_target_r = 0.0
                self.move_mode = "NONE"
                self.freemode = 1
                self.motion_task = 0
                self._ev("STOPPED END")  # v2.0: 取代 `MOVEDONE FWD ...`
            return

        if self.move_mode == "ROT":
            if time.time() >= self.move_until:
                self.speed_target_l = self.speed_target_r = 0.0
                self.move_mode = "NONE"
                self.freemode = 1
                self.motion_task = 0
                self._ev("STOPPED END")  # v2.0: 取代 `MOVEDONE ROT`
            return

        if self.move_mode == "RUN":
            # ---- 与固件 link_MoveService 的 RUN 分支逐条对齐 ----
            self.run_elapsed += 1

            ir, which = self._min_ir()
            if self.run_ir_min == 0 or ir < self.run_ir_min:
                self.run_ir_min = ir
                self.run_ir_which = which
            self.run_vmax_l = max(self.run_vmax_l, int(abs(self.speed_l)))
            self.run_vmax_r = max(self.run_vmax_r, int(abs(self.speed_r)))

            # 护栏 1: 红外贴脸必停
            if self.run_ir_mm and ir < self.run_ir_mm:
                self._finish_run("IR")
                return
            # 护栏 2: 到时必停
            if self.run_elapsed >= self.run_limit:
                self._finish_run("TIME")
                return
            return

        if self.move_mode == "SERVO":
            # 模拟角度基准扫描: 往复若干次 x 90 度, 约 6 秒后回报累计量。
            # v2.0: 结果不再走 `CALDONE GYRORATIO ...` 事件行, 而是进挂起命令的
            # ACK 附加字段(见 _cal_tick)。
            self.motion_task = 2
            if time.time() - self.srv_start > 6.0:
                deg = 900.0
                sum_acc = int(deg * self.params[5])
                sum_enc = int(deg * self.params[6])
                self.speed_target_l = self.speed_target_r = 0.0
                self.move_mode = "NONE"
                self.motion_task = 0
                self.freemode = 1
                self.gyro = 0.0
                self._cal_finish(True, (sum_acc, sum_enc, int(deg)))
            return

        if self.move_mode != "EXPLORE":
            self.motion_task = 0
            return
        # ---- 右手法则探索: 每格 180mm, 遇墙转弯 ----
        if self.phase == "CELL":
            self.motion_task = 32  # SMARTSTRAIGHTFAST
            self.pid_err = self.gyro
            self.speed_target_l = 68.0
            self.speed_target_r = 68.0
            self.phase_dist += (self.speed_l + self.speed_r) / 2.0 / 100.0
            if self.phase_dist >= 180.0:
                self.phase_dist = 0.0
                self._arrive_cell()
        else:  # TURN
            self.motion_task = 2  # TURN
            ang = abs(self.gyro)
            err = 90.0 - ang
            if err < 2.0:
                self.speed_target_l = self.speed_target_r = 0.0
                self.gyro = 0.0
                self.dir = self.turn_target_dir
                self.phase = "CELL"
                self.pid_err = 0.0
            else:
                v = min(45.0, 12.0 + err * 0.55)
                # 目标方向相对当前朝向的符号决定转向
                delta = (self.turn_target_dir - self.dir) % 4
                sign = 1.0 if delta == 1 else -1.0
                self.speed_target_l = sign * v
                self.speed_target_r = -sign * v
                self.pid_err = self.gyro

    def _motion_busy(self) -> bool:
        """是否有任何"正在动"的活动 —— 对应固件 `link_MotionBusy()`。

        **为什么需要它**（真机实测揪出的缺陷, 2026-10-04）:
        `MOVE FWD/ROT/RUN` 启动时都会把 `motionCtrl.task` 置 NULL(它们由
        `link_MoveService` 驱动, 不走任务状态机), 所以"只看 task"是查不出正在跑 MOVE 的。
        当时固件四处 busy 检查都漏了 `linkMove.state`, 后果是第二条 `MOVE RUN` 会被接受
        并**覆盖 `linkMove.elapsed/limit` —— 时间护栏被重置**, 连续发就能无限延长运行。
        模拟器当时也没查, 所以整套测试盖不住这个问题; 这里补上, 让
        "第二条 MOVE 必须被 ERR_BUSY 拒掉" 成为可回归的断言。
        """
        return ((self.move_mode != "NONE") or self.cal_ack_seq >= 0)

    def _flags(self) -> int:
        """统一帧 flags 字节 —— 与固件 `link_Flags()` 位定义逐位一致。

        单一来源: 遥测帧与(曾经的)SNAP 各算一遍会让两侧不一致, 故抽成方法。
        v2.0: bit4(DUMPING)恒 0 —— dump 机制已删除;
        bit7 由 RUNLOG 改为 **EVQ**(有事件行被丢弃 -> 事件流不完整)。
        ⚠️ 本方法**只读状态镜像**, 不碰 PULLF1 —— 那一位是**按帧**的(只有批首帧带),
           由 `_rate_tick` 在组帧前后置/清(见那里的注释), 与固件的门控 OR 同构。
        """
        f = 0
        if self.freemode:
            f |= FLAG_FREEMODE
        if self.sleepmode:
            f |= FLAG_SLEEP
        if self.emerg:
            f |= FLAG_EMERG
        if self.safe:
            f |= FLAG_MOTOR_OFF
        if self.estop_lock:
            f |= FLAG_ESTOP_LOCK
        if self.ev_drop:
            f |= FLAG_EVQ
        if self.pull_f1:
            f |= FLAG_PULLF1   # v2.6.1: 批首帧(仅 PULL 批的第 1 帧)
        return f

    # ---------------------------------------------------------- 上行事件

    def _ev(self, head: str) -> None:
        """产生一条上行事件行 —— 与固件 `link_EvLine()` 同构。

        形态: `<head> #<seq>\\r\\n`, seq 取自**与帧共用**的 1 字节计数器。
        固件在 TX 空间不足时会丢弃并置 flags 的 EVQ(不做 ACK、不重发);
        模拟器没有真实 TX 环, 事件恒有空间 —— 想回归"丢弃"那条路径,
        手动把 `self.ev_drop` 置非零即可(flags 立刻带 EVQ)。
        """
        self.ev_out.append(f"{head} #{self.up_seq}")
        self.up_seq = (self.up_seq + 1) & 0xFF

    # ---------------------------------------------------------- 标定(请求-响应)

    def _cal_start(self, item: str, phase: int) -> None:
        """进入一步标定采集。**不**在这里回 ACK —— 采集完成由 _cal_tick 回。"""
        self.cal_item = item
        self.cal_phase = phase
        self.cal_ticks = 0
        self.speed_target_l = self.speed_target_r = 0.0
        self.motion_task = 0
        if item == "GYRORATIO" or item == "ENCANGLE":
            self.freemode = 0
            self.srv_start = time.time()
            self.move_mode = "SERVO"
            self.speed_target_l, self.speed_target_r = 25.0, -25.0
        else:
            self.freemode = 1
            self.move_mode = "NONE"

    def _cal_finish(self, ok: bool, extra: "tuple | None" = None) -> None:
        """消费挂起的标定应答(见固件 `link_CalAck` / `link_AckI3`)。"""
        if self.cal_ack_seq < 0:
            return
        seq = self.cal_ack_seq
        self.cal_ack_seq = -1
        self.cal_item = None
        if ok and extra:
            self.ev_out.append(f"ACK {seq} OK {extra[0]} {extra[1]} {extra[2]}")
        else:
            self.ev_out.append(f"ACK {seq} {'OK' if ok else 'ERR_TIMEOUT'}")

    def _cal_tick(self) -> None:
        """1ms 节拍: 推进静态三项的采集并回挂起的 ACK。

        GYRO 需要等陀螺零偏自算稳定(固件是 adjCounter>1500 -> 1.5s),
        这里按 300 拍(0.3s)模拟 —— 仍明显长于"立刻回", 足以验证请求-响应语义。
        """
        if self.cal_item is None or self.cal_ack_seq < 0:
            return
        if self.cal_item in ("GYRORATIO", "ENCANGLE"):
            return  # 由 _drive 的 SERVO 分支收尾
        self.cal_ticks += 1
        need = 300 if self.cal_item == "GYRO" else 60
        if self.cal_ticks < need:
            return
        if self.cal_item == "GYRO":
            self.params[5] = float(random.randint(2020, 2060))  # 仅用于让 GET 有变化
            self._cal_finish(True)
        elif self.cal_item == "WALLDIST":
            self.params[7] = float(int(self.ir[2]) if self.ir[2] < 200 else 42)
            self._cal_finish(True)
        else:  # IRDATA: 一个相位 = 一条命令, 采够就 ACK(第 4 相位的 ACK 即"整套完成")
            self._cal_finish(True)

    # ---------------------------------------------------------- v2.7 菜单远程化

    #: v2.7 由按键菜单远程化而来的 6 个子命令(与固件 link_CalItemToId 新分支对应)
    _CAL_MENU_ITEMS = ("ERASEIR", "IRSELF", "SENSOR", "IRCAP", "BLOCK", "ERASEALL")

    def _cal_menu_remote(self, item: str, t: List[str], seq: int) -> List[str]:
        """v2.7: 按键菜单 2/4/5/6/7/8 的远程化(全部**立即 ACK**)。

        与固件 `link_CalMenuRemote` 逐条对齐, 含它的两条闸:
        运动中/标定中一律 `ERR_BUSY`; `IRCAP` 必须从 1 顺序调, 且第 1 步前
        IRDATA 页必须全 FF(远程版新增的安全闸, 菜单 6 本身没有这条)。
        """

        def ack(s: str) -> str:
            self.last_status = s
            return f"ACK {seq} {s}\r\n"

        if self._motion_busy():
            return [ack("ERR_BUSY")]

        if item == "ERASEIR":
            self.ir_page = [0xFFFF] * 8
            self.ir_cap_step = 0
            return [ack("OK")]

        if item == "IRSELF":
            # 固件报 irDist.l/r(mm) 与 distToWall; 模拟器没有距离模型,
            # 用左右红外通道原值顶位(协议形状一致, 数值不作为判据)。
            return [ack(f"OK {int(self.ir[2])} {int(self.ir[3])} {int(self.params[7])}")]

        if item == "SENSOR":
            # 固件打 4 行 printf(值不在 ACK 里), 这里只回 ACK
            return [ack("OK")]

        if item == "IRCAP":
            if len(t) < 3:
                return [ack("ERR_VALUE")]
            try:
                step = int(t[2])
            except ValueError:
                return [ack("ERR_VALUE")]
            if step < 1 or step > 4:
                return [ack("ERR_VALUE")]
            if step != 1 and step != self.ir_cap_step + 1:
                return [ack("ERR_VALUE")]
            if step == 1 and any(v != 0xFFFF for v in self.ir_page):
                return [ack("ERR_BUSY")]
            # 采样配对与固件一致: 1/2 步取左右通道, 3/4 步取前向两路
            a = int(self.ir[2]) if step <= 2 else int(self.ir[0])
            b = int(self.ir[3]) if step <= 2 else int(self.ir[1])
            self.ir_cap_step = step
            if step < 4:
                return [ack(f"OK {a} {b} {step}")]
            # 第 4 步: 擦页并按固件写序落 8 个半字, 序列复位
            self.ir_page = [a, b, a, b, a, b, a, b]
            self.ir_cap_step = 0
            return [ack(f"OK {a} {b} 4")]

        if item == "BLOCK":
            p45 = self.path_counts[0] + self.path_counts[1]
            p90 = self.path_counts[2] + self.path_counts[3]
            ir_valid = sum(1 for v in self.ir_page if v != 0xFFFF)
            return [ack(f"OK {p45} {p90} {ir_valid}")]

        # ERASEALL: 擦迷宫/路径 6 页(**不含** IRDATA 页, 与固件一致);
        # 固件随后进流水灯死循环, 远程版返回值守 —— 模拟器只做状态清零。
        self.path_counts = [0, 0, 0, 0]
        return [ack("OK")]

    # ---------------------------------------------------------- MOVE RUN

    def _min_ir(self) -> Tuple[int, int]:
        """(最小红外读数, 通道号)。通道 0=F1 1=F2 2=L 3=R, 与固件 link_MinIr 一致。"""
        vals = [int(self.ir[0]), int(self.ir[1]), int(self.ir[2]), int(self.ir[3])]
        i = min(range(4), key=lambda k: vals[k])
        return vals[i], i

    def _finish_run(self, reason: str) -> None:
        """停车 + 上报停止原因(与固件 link_MoveFinishRun 对齐)。

        v2.0 只上报 **为什么停** —— elapsed / ir_min / dL / dR 这些量在 217Hz 的
        连续帧序列里都能读到, 再塞进一条不可靠的长文本只是浪费带宽。
        """
        self.speed_target_l = self.speed_target_r = 0.0
        self.freemode = 1
        self.move_mode = "NONE"
        self.motion_task = 0
        self._ev(f"STOPPED {reason}")

    def _arrive_cell(self) -> None:
        x, y = self.pos
        self._mark_walls(x, y)
        # 右手法则: 右 -> 前 -> 左 -> 后
        for d in ((self.dir + 1) % 4, self.dir, (self.dir + 3) % 4, (self.dir + 2) % 4):
            if self._can_go(x, y, d):
                nx, ny = x + DIR_DELTA[d][0], y + DIR_DELTA[d][1]
                self.pos = (nx, ny)
                if nx < self.n and ny < self.n:
                    self.visited[nx][ny] = True
                self.turn_target_dir = d
                self.phase = "TURN"
                return
        self.turn_target_dir = (self.dir + 2) % 4
        self.phase = "TURN"

    def _mark_walls(self, x: int, y: int) -> None:
        self.wall_known[x][y] = True
        self.map_wall[x][y] = wall_byte(self.vwall, self.hwall, x, y, self.n)

    def _can_go(self, x: int, y: int, d: int) -> bool:
        dx, dy = DIR_DELTA[d]
        nx, ny = x + dx, y + dy
        if not (0 <= nx < self.n and 0 <= ny < self.n):
            return False
        b = wall_byte(self.vwall, self.hwall, x, y, self.n)
        return bool(b & (1 << d))

    def _update_ir(self) -> None:
        x, y = self.pos
        b = wall_byte(self.vwall, self.hwall, x, y, self.n) if 0 <= x < self.n and 0 <= y < self.n else 0
        # 相对朝向的 前/左/右 距离
        f_open = bool(b & (1 << self.dir))
        l_open = bool(b & (1 << ((self.dir + 3) % 4)))
        r_open = bool(b & (1 << ((self.dir + 1) % 4)))
        base = 34.0 if self.phase == "TURN" else 42.0
        f = 232.0 if f_open else max(28.0, base + random.uniform(-2, 2))
        # 前进时前向距离随 cellOdom 减小
        if not f_open:
            f = max(26.0, 180.0 - self.cell_odom + random.uniform(-2, 2))
            f = min(f, 235.0)
        self.ir[0] = f
        self.ir[1] = f + random.uniform(-3, 3)
        self.ir[2] = 236.0 if l_open else max(28.0, 42.0 + random.uniform(-2, 2))
        self.ir[3] = 236.0 if r_open else max(28.0, 42.0 + random.uniform(-2, 2))
        self.wall_bits = 0
        if not l_open:
            self.wall_bits |= 0x01
        if not r_open:
            self.wall_bits |= 0x02
        if not f_open:
            self.wall_bits |= 0x04
        # 运行时侧墙阈值: 与固件一样**不是**参数值 —— state.c 比赛流程改写为 140,
        # motion.c 运动控制中改写为 110。参数 IRTHRWALL(160) 只是初值。
        if self.move_mode != "NONE":
            self.wall_thresh = 110
        elif 1 <= self.mouse_task <= 5:
            self.wall_thresh = 140
        else:
            self.wall_thresh = int(self.params[22])

    # ---------------------------------------------------------- 组帧

    def _ir_adc(self, k: int) -> int:
        """由 mm 反推原始 ADC(仅用于让"测距/ADC"自洽)。"""
        return max(0, min(4095, 2990 - int(self.ir[k]) * 12))

    def _unified_frame(self) -> bytes:
        """组一帧统一帧 —— 与固件 `link_UnifiedFrame()` 逐字段镜像。"""
        # v2.3: 前墙双档是**派生量**(恒等式 frontNear|frontFar == wallFront)。
        # 模拟器按与固件 link.c 相同的规则算: 有墙 且 min(f1,f2) < thrFrontNear -> 近端。
        near_thr = int(self.params[28])          # 28 = IRTHRFNEAR
        fmin = min(self.ir[0], self.ir[1])
        wf = (self.wall_bits >> 2) & 0x01
        front_near = 1 if (wf and fmin < near_thr) else 0
        front_far = 1 if (wf and not front_near) else 0
        d = {
            "ms8": self.link_time_ms & 0xFF,
            "irAdcF1": self._ir_adc(0), "irAdcF2": self._ir_adc(1),
            "irAdcL": self._ir_adc(2), "irAdcR": self._ir_adc(3),
            "irF1": _i16(self.ir[0]), "irF2": _i16(self.ir[1]),
            "irL": _i16(self.ir[2]), "irR": _i16(self.ir[3]),
            "battMv": self.batt_mv,
            "speedL": _i16(self.speed_l), "speedR": _i16(self.speed_r),
            "gyroAngle": _i16(self.gyro),
            "encAngle": _i16(self.enc_diff_acc / max(1.0, self.params[6])),
            "trackL": _i16(self.dist_l_mm), "trackR": _i16(self.dist_r_mm),
            "cellOdom": _i16(self.cell_odom), "pidErr": _i16(self.pid_err),
            "dir": self.dir,
            "mazeTask": self.mouse_task,
            "wallLeft": self.wall_bits & 0x01,
            "wallRight": (self.wall_bits >> 1) & 0x01,
            "wallFront": (self.wall_bits >> 2) & 0x01,
            "motionTask": self.motion_task & 0x7F,
            "irSwitch": 0,
            "motionStep": self.motion_step & 0x0F,
            "critL": 1 if (self.wall_bits & 0x01) and self.ir[2] < self.params[24] else 0,
            "critR": 1 if (self.wall_bits & 0x02) and self.ir[3] < self.params[24] else 0,
            "left45": 0, "right45": 0,
            "flags": self._flags(),
            "posX": self.pos[0], "posY": self.pos[1],
            "goalX": self.goal[0], "goalY": self.goal[1],
            "wallThresh": self.wall_thresh,
            "stackIdx": 0,
            "fastwayCount": 0,
            "dropCnt": 0,
            # ---- v2.3: 前墙双档 / 冲刺状态(模拟器不跑冲刺, 全 0) ----
            "frontNear": front_near,
            "frontFar": front_far,
            "frontBoth": front_far,   # 模拟器近似: 远端档即"两路都看到"
            "spurtMode": 0, "spurtFlag": 0, "goOrBack": 0,
            "spurtTestFlag": 0, "objectGoToFlag": 0,
            "goaheadnum": 0, "fastwayCount90": 0,
        }
        pl = pack_unified(d)
        assert len(pl) == UNIFIED_LEN, len(pl)
        return pack_frame(TYPE_UNIFIED, pl, self.up_seq)

    def _frame_map(self) -> bytes:
        wall = bytearray()
        for x in range(self.n):
            for y in range(self.n):
                wall.append(self.map_wall[x][y] if self.wall_known[x][y] else 0)
        vis = bytearray()
        for x in range(self.n):
            for y in range(self.n):
                vis.append(1 if self.visited[x][y] else 0)
        pl = struct.pack("<bbBB", self.pos[0], self.pos[1], self.dir, self.mouse_task) + bytes(wall) + bytes(vis)
        return pack_frame(TYPE_MAP, pl, self.up_seq)

    # ---------------------------------------------------------- 命令处理

    def handle_line(self, line: str) -> List[bytes]:
        """返回待发送的文本应答(不含遥测帧)。"""
        out: List[str] = []
        try:
            star = line.rfind("*")
            if star < 0 or len(line) - star < 5:
                return [b"ACK -1 ERR_NOCRC\r\n"]
            body = line[:star]
            crc_rx = int(line[star + 1 : star + 5], 16)
            if crc16_modbus(body.encode("ascii")) != crc_rx:
                return [b"ACK -1 ERR_CRC\r\n"]
            seq = -1
            if "#" in body:
                body, seq_s = body.split("#", 1)
                seq = int(seq_s)
            body_s = body.strip()
            # ---- TELEM: 纯拉取命令, **不参与幂等重放簿记**(与固件 link_HandleLine 一致) ----
            # 必须在这里提前返回: 拉取器是持续发 TELEM 的, 一旦让它写 last_seq/last_crc,
            # 就会把"上一条待重放的真实命令"(MOVE/CAL)冲掉 —— 那条命令的 ACK 一旦丢失,
            # 上位机按同 seq 重发时不会被重放, 而是**重新执行**(MOVE 重新起步 / CAL 重采)。
            if seq >= 0 and (body_s == "TELEM" or body_s.startswith("TELEM ")):
                self.telem_reply = self._unified_frame()
                self.up_seq = (self.up_seq + 1) & 0xFF
                return [f"ACK {seq} OK {self.link_time_ms}\r\n".encode()]
            # ---- RATE: 自时钟节拍控制, **同样不参与幂等重放簿记**(与固件一致) ----
            # 事件触发式拉取每周期下发一条 RATE; 若让它写 last_seq/last_crc, 就会把
            # 待重放的真实命令冲掉 —— 那条命令的 ACK 一丢, 上位机重发时不会被重放,
            # 而是**重新执行**。
            if seq >= 0 and (body_s == "RATE" or body_s.startswith("RATE ")):
                return [r.encode() for r in self._exec(body_s, seq)]
            if seq >= 0 and seq == self.last_seq and crc_rx == self.last_crc:
                return [f"ACK {seq} {self.last_status}\r\n".encode()]
            self.last_seq, self.last_crc = seq, crc_rx
            replies = self._exec(body_s, seq)
        except Exception as e:  # 模拟器自身异常不应中断服务
            replies = [f"ACK -1 ERR_SIM({e})\r\n"]
        return [r.encode() for r in replies]

    def _exec(self, cmd: str, seq: int) -> List[str]:
        t = cmd.split()
        if not t:
            return [f"ACK {seq} ERR_CMD\r\n"]

        def ack(s: str) -> str:
            self.last_status = s
            return f"ACK {seq} {s}\r\n"

        def ack_u(s: str, v: int) -> str:
            """带一个附加整数的应答(`ACK <seq> OK <v>`) —— 对应固件 link_AckU。

            ⚠️ 固件端这类应答**不进 linkLastStatus**(它是"当下的值", 缓存重放就是
            撒谎), 并会清 linkLastSeq。这里同样只回不缓存。"""
            return f"ACK {seq} {s} {int(v)}\r\n"

        c = t[0].upper()

        if c == "PING":
            # v2.0: VER 已并入 PING; 后两字段 = TX 环水位 + 上次复位原因名。
            return [
                f"PONG {PROTO_VER} {FW_VER} {self.n} {self.txbuf} {self.reset_cause}\r\n",
                ack("OK"),
            ]
        if c == "GET":
            if len(t) >= 2 and t[1].upper() == "ALL":
                # 只回 `linkParam[LINK_P_NUM]` 那张表(远程档 = 41 项, 与固件 `GET ALL` 同构)。
                # v2.4: 课程参数随课程层删除; v2.5: 运动控制段(39-55)入表, 这张表就是**全部**。
                lines = [f"P {i} {_q(self.params[i])}\r\n" for i in PRODUCTION_IDS]
                return lines + [ack("OK")]
            if len(t) >= 2 and t[1].upper() in NAME_IDS:
                i = NAME_IDS[t[1].upper()]
                return [f"P {i} {_q(self.params[i])}\r\n", ack("OK")]
            # 不在册的名字一律 ERR_NAME(与固件 `link_NameToId` 的失败路径一致)
            return [ack("ERR_NAME")]
        if c == "SET":
            if len(t) < 3 or t[1].upper() not in NAME_IDS:
                return [ack("ERR_NAME")]
            try:
                v = float(t[2])
            except ValueError:
                return [ack("ERR_VALUE")]
            self.params[NAME_IDS[t[1].upper()]] = v
            return [ack("OK")]
        if c == "SETQ":
            if len(t) < 3 or t[1].upper() not in NAME_IDS:
                return [ack("ERR_NAME")]
            try:
                q = int(t[2])
            except ValueError:
                return [ack("ERR_VALUE")]
            self.params[NAME_IDS[t[1].upper()]] = q / 65536.0
            return [ack("OK")]
        if c == "SAVE":
            # 运动/实验期间擦写 Flash 的取指停顿会打断 1kHz 控制环 -> 与固件一致地拒绝
            if self._motion_busy():
                return [ack("ERR_BUSY")]
            self.flash_valid = True
            return [ack("OK")]
        if c == "RESET":
            self.flash_valid = False
            return [ack("OK")]
        if c == "MCU":
            # `MCU RESET` = 整机软复位(固件 Sys_Soft_Reset)。与上面的 `RESET`
            # (只擦参数页) 完全不同。模拟器不重启进程, 只把"复位原因"置成 SW ——
            # 这样上位机的复核判据(rst=="SW")在模拟器上也能走通。
            if len(t) >= 2 and t[1].upper() == "RESET":
                self._halt()
                self.maze_requested = False
                self.maze_running = False
                self.mouse_task = 0          # WAIT
                self.estop_lock = False
                self.emerg = False
                self.sleepmode = 0
                self.freemode = 1
                self.reset_cause = "SW"
                return [ack("OK")]
            return [ack("ERR_VALUE")]
        if c == "TARE":
            self.gyro = 0.0
            self.enc_diff_acc = 0.0
            return [ack("OK")]
        if c == "STOP":
            # 与固件一致: 若正在 MOVE RUN, 先按 RUN 的格式上报一次 `STOPPED USER`
            was_run = self.move_mode == "RUN"
            self._halt()
            if was_run:
                self._ev("STOPPED USER")
            # 测试/采集档: STOP 兼作"解急停", 并让板子保持在线
            self.estop_lock = False
            self.emerg = False
            self.sleepmode = 0
            # [4] 档语义: STOP 把电脑鼠带出迷宫循环并复位发车状态
            self._maze_release()
            return [ack("OK")]
        if c == "ESTOP":
            was_run = self.move_mode == "RUN"
            self._halt()
            if was_run:
                self._ev("STOPPED ESTOP")
            self.emerg = True
            self.estop_lock = True
            # [4] 档语义: 急停同样把电脑鼠带出迷宫循环(否则状态无法复位)
            self._maze_release()
            # 注意: 这里**不**发 `ESTOP TRIP` —— 固件的该事件只在"硬件急停通路"
            # 首次触发时发, 而 ESTOP 命令已先行置位 linkEStop, 于是
            # LINK_EStopTrip() 直接返回(见 link.c 的注释)。
            return [ack("OK")]
        if c == "TELEM":
            # 正常情况下到不了这里 —— TELEM 已由 handle_line **提前拦截**(它必须绕开
            # 幂等重放簿记)。保留这一支只为防御: 若真的走到, 也绝不碰 last_seq。
            self.telem_reply = self._unified_frame()
            self.up_seq = (self.up_seq + 1) & 0xFF
            return [ack_u("OK", self.link_time_ms)]
        if c == "MAP":
            if len(t) >= 2 and t[1].upper() == "DUMP":
                self.map_reply = self._frame_map()
                return [ack("OK")]
            return [ack("ERR_VALUE")]
        if c == "MOVE":
            if len(t) < 2:
                return [ack("ERR_VALUE")]
            a = t[1].upper()
            if a == "RUN":
                # MOVE RUN <spL> <spR> [ms] [ir_mm]
                if len(t) < 4:
                    return [ack("ERR_VALUE")]
                try:
                    sp_l = int(float(t[2]))
                    sp_r = int(float(t[3]))
                    ms = int(float(t[4])) if len(t) >= 5 else 0
                    ir = int(float(t[5])) if len(t) >= 6 else 0
                except ValueError:
                    return [ack("ERR_VALUE")]
                if self.estop_lock:
                    return [ack("ERR_ESTOP")]
                if self._motion_busy():
                    # 关键: 必须包含 move_mode != NONE。否则第二条 RUN 会覆盖
                    # run_elapsed/run_limit, 把时间护栏重置掉(真机曾如此)。
                    return [ack("ERR_BUSY")]
                if sp_l == 0 and sp_r == 0:
                    return [ack("ERR_VALUE")]
                if ms <= 0:
                    ms = 2000
                if ir < 0:
                    ir = 0            # 负值 = 关闭红外护栏
                elif ir == 0:
                    ir = 10           # 省略 = 默认 10mm
                self._halt()
                self.run_limit = ms
                self.run_elapsed = 0
                self.run_ir_mm = ir
                self.run_ir_min = 1 << 30
                self.run_ir_which = 0
                self.run_vmax_l = self.run_vmax_r = 0
                self.run_start_l = self.dist_l_mm
                self.run_start_r = self.dist_r_mm
                self.freemode = 0
                self.move_mode = "RUN"
                self.speed_target_l, self.speed_target_r = float(sp_l), float(sp_r)
                return [ack("OK")]
            if a == "FWD" and len(t) >= 3:
                if self.estop_lock:
                    return [ack("ERR_ESTOP")]
                if self._motion_busy():
                    return [ack("ERR_BUSY")]
                self._halt()
                self.move_pulses = int(float(t[2]))
                self.move_start_l = self.dist_l_mm
                self.move_start_r = self.dist_r_mm
                speed = float(t[3]) if len(t) >= 4 else 40.0
                self.freemode = 0
                self.move_mode = "FWD"
                self.speed_target_l = self.speed_target_r = speed
                return [ack("OK")]
            if a == "ROT" and len(t) >= 3:
                if self.estop_lock:
                    return [ack("ERR_ESTOP")]
                if self._motion_busy():
                    return [ack("ERR_BUSY")]
                self._halt()
                pwm = float(t[2])
                ms = int(float(t[3])) if len(t) >= 4 else 500
                self.freemode = 0
                self.move_mode = "ROT"
                self.move_until = time.time() + ms / 1000.0
                self.speed_target_l, self.speed_target_r = pwm, -pwm
                return [ack("OK")]
            return [ack("ERR_VALUE")]
        if c == "ARM":
            # 发车: 记下发车时刻 + 点亮 1Hz 时间基准灯; 发车时基随 ACK 回
            self.armed = True
            self.arm_ms = self.link_time_ms
            self.time_led = 1
            return [ack_u("OK", self.arm_ms)]
        if c == "MAZE":
            if len(t) >= 2 and t[1].upper() == "RUN":
                # MAZE RUN [BLIND|RACE] —— v2.4 起带可选目标词。
                # RACE 在固件里会先跑 STATE_LoadMapAndSelectSpeed() /
                # STATE_DetermineGoaheadNum()(读 Flash 地图选档+定冲刺)再进主循环;
                # 模拟器没有 Flash 地图, 因此两者都落到同一套探索驱动, 只记录目标。
                goal = t[2].upper() if len(t) >= 3 else "BLIND"
                if goal not in ("BLIND", "RACE"):
                    return [ack("ERR_VALUE")]
                if self.maze_requested or self.maze_running:
                    return [ack("ERR_BUSY")]
                if self.estop_lock:
                    # 与固件一致: 急停锁存中直接发车会被 ISR 每周期清零速度,
                    # 与其让主机看到"发了车却不动", 不如明确回 ERR_ESTOP
                    return [ack("ERR_ESTOP")]
                self.maze_target = goal
                self.maze_requested = True
                self.maze_running = True   # 模拟器没有"主循环等待"一说, 直接开跑
                # v2.8: 真固件在此刻还会**点亮全部 4 个 LED**(LINK_LAUNCH_MARK_MS)
                # 作为录像对轴用的发车标记。模拟器没有 LED 引脚, 故不外显 ——
                # 这里只留注释, 使"固件有、模拟器无"的差异是有意且可查的。
                self.mouse_task = 2        # MAZESEARCH
                # MAZE RUN 是**显式**发车命令, 因此不受 --no-auto 影响
                self.move_mode = "EXPLORE"
                self.freemode = 0
                return [ack("OK")]
            if len(t) >= 2 and t[1].upper() == "STOP":
                # MAZE STOP(v2.1 新增): 带出迷宫 + 复位发车状态 + 解锁急停
                self._halt()
                self.estop_lock = False
                self.emerg = False
                self.sleepmode = 0
                self._maze_release()
                return [ack("OK")]
            # MAZE EV / MAZE RST 已删除(v2.0) —— 与固件一样回 ERR_VALUE
            return [ack("ERR_VALUE")]
        if c == "CAL":
            # CAL <item> [<act>] —— 与固件 link.c 的 CAL 分支同构, 两类子命令:
            #  * 旧五项请求-响应: act = START/ABORT 或 IRDATA 相位 0..3, 采完才 ACK;
            #  * v2.7 六项菜单远程化: act 省略, 或 IRCAP 的步号 1..4 —— **立即 ACK**,
            #    走 _cal_menu_remote(顺序在固件里也是先解析 item 再分流)。
            if len(t) < 2:
                return [ack("ERR_VALUE")]
            item = t[1].upper()
            if item in self._CAL_MENU_ITEMS:
                return self._cal_menu_remote(item, t, seq)
            # 旧五项: 未知 item 一律 ERR_NAME(与固件 link_CalItemToId 的失败路径一致)
            if len(t) < 3:
                return [ack("ERR_VALUE")]
            act = t[2].upper()
            if item not in ("GYRO", "WALLDIST", "IRDATA", "GYRORATIO", "ENCANGLE"):
                return [ack("ERR_NAME")]
            if act == "ABORT":
                self.cal_ack_seq = -1
                self.cal_item = None
                self._halt()
                return [ack("OK")]
            phase = 0
            if act != "START":
                if item != "IRDATA":
                    return [ack("ERR_VALUE")]
                try:
                    phase = int(act)
                except ValueError:
                    return [ack("ERR_VALUE")]
                if phase < 0 or phase > 3:
                    return [ack("ERR_VALUE")]
            if self.estop_lock and item in ("GYRORATIO", "ENCANGLE"):
                return [ack("ERR_ESTOP")]
            if self._motion_busy() and not (self.cal_item == item and self.cal_ack_seq >= 0):
                return [ack("ERR_BUSY")]
            self.cal_ack_seq = seq
            self.last_seq = -1  # 防止"重发同一条命令"命中缓存里的旧 ACK
            self._cal_start(item, phase)
            return []  # 不立刻回 ACK —— 采集完成由 _cal_tick / _drive 回
        if c == "RATE":
            # 速率族(v2.6): OFF / FIX <hz> / PULL <n> <t_ms>。与固件 link.c 的 RATE
            # 分支逐词对齐; 三个子命令都只做"节拍设置", 实际发帧由 _rate_tick 完成。
            sub = t[1].upper() if len(t) >= 2 else ""
            if sub == "OFF":
                self.rate_on = False
                self.pull_on = False
                self.pull_count = 0
                self.pull_f1 = False
                self.rate_acc = 0
                return [ack("OK")]
            if sub == "FIX":
                try:
                    hz = int(float(t[2])) if len(t) >= 3 else 2
                except ValueError:
                    return [ack("ERR_VALUE")]
                self.rate_fix_hz = max(1, min(30, hz))
                self.rate_acc = 0
                self.rate_on = True
                self.pull_on = False
                return [ack("OK")]
            if sub == "PULL":
                try:
                    n = int(t[2]) if len(t) >= 3 else 1
                    tms = int(t[3]) if len(t) >= 4 else 100
                except ValueError:
                    return [ack("ERR_VALUE")]
                self.pull_count = max(1, min(16, n))
                self.pull_period = max(1, min(5000, tms))
                self.pull_timer = 0  # 立即发第一帧
                self.rate_on = True
                self.pull_on = True
                return [ack("OK")]
            return [ack("ERR_NAME")]
        return [ack("ERR_CMD")]

    def _halt(self) -> None:
        """停车 + 清运动/标定子状态(与固件 `link_StopMotion()` 对齐)。"""
        self.speed_target_l = self.speed_target_r = 0.0
        self.move_mode = "NONE"
        self.freemode = 1
        self.motion_task = 0
        self.phase = "CELL"
        self.phase_dist = 0.0
        self.cal_item = None
        self.cal_ack_seq = -1

    def _maze_release(self) -> None:
        """把电脑鼠**带出迷宫循环**并复位发车状态。

        与固件 [4] 档的 `MAZE STOP` / `STOP` / `ESTOP` 对齐 —— 这三条命令在固件里
        都会置 `linkMazeAbort`, 让 `LESSON_Blind8()` 的 while(1) 返回、main() 随即
        `LINK_MazeFinished()` 复位发车状态。

        为什么模拟器也要做: 固件只置 `linkMazeReq` 而不消费它, 加上 while(1) 不返回,
        导致**发过一次车后 `MAZE RUN` 永久 ERR_BUSY**(真机复现 2026-10-05)。
        模拟器若不复位, 契约测试就盖不住这条真实缺陷 —— 本方法就是那条防线。
        """
        self.maze_requested = False
        self.maze_running = False
        self.mouse_task = 0  # WAIT

    # ---------------------------------------------------------- 服务端

    def run(self, host: str, port: int) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(1)
        print(f"[sim] 监听 {host}:{port}（Ctrl+C 退出）")
        while True:
            conn, addr = srv.accept()
            print(f"[sim] 客户端接入 {addr}")
            try:
                self._serve_one(conn)
            except Exception as e:
                print(f"[sim] 客户端异常: {e}")
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
            print("[sim] 客户端断开, 等待下一个连接")

    def _serve_one(self, conn: socket.socket) -> None:
        conn.settimeout(None)
        rx = bytearray()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        next_tick = time.perf_counter()
        tx = bytearray()
        last_flush = time.perf_counter()
        #: 待释放的应答 [(到期时刻, 字节)] —— 仅在 `rtt_s > 0` 时使用(按到期时刻递增)。
        delayed: List[Tuple[float, bytes]] = []

        while True:
            # 本轮新产生的全部字节: 命令应答 + 事件行 + 自时钟帧。
            # 先攒进 pending, 再按 `rtt_s` 决定直接入 tx 还是延迟释放 ——
            # 这样"链路往返延迟"对**所有**下行命令的应答一致生效。
            pending = bytearray()
            # ---- 收 ----
            conn.settimeout(0.0)
            try:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                rx += chunk
            except BlockingIOError:
                pass
            except OSError:
                return

            # 文本命令按 CR/LF 分帧(与固件 usart.c 的语义一致)
            while True:
                nl = rx.find(b"\n")
                if nl < 0:
                    break
                raw = bytes(rx[: nl + 1])
                del rx[: nl + 1]
                line = raw.decode("ascii", "replace").strip()
                if line:
                    reps = self.handle_line(line)
                    # TELEM: 先发帧、再发 ACK(与固件的入队顺序一致)
                    tr = self.telem_reply
                    if tr is not None:
                        pending += tr
                        self.telem_reply = None
                    for r in reps:
                        pending += r

            # ---- 物理与遥测 ----
            now = time.perf_counter()
            while now >= next_tick:
                self.step()
                next_tick += 0.001
                if next_tick < now - 0.25:  # 落后太多则重新对齐, 避免追帧风暴
                    next_tick = now + 0.001

            # 事件行 / 自时钟统一帧都在 1ms 节拍里产生, 由服务循环搬到 pending
            with self.lock:
                for ev in self.ev_out:
                    pending += (ev + "\r\n").encode()
                self.ev_out.clear()

                # (v2.6: 自时钟帧只在 PC 显式下发 RATE FIX / RATE PULL 后才产生;
                #  平时的遥测仍完全由上位机的 TELEM 请求驱动 —— 见 handle_line 的 TELEM 分支)
                for fr in self.rate_frames:
                    pending += fr
                self.rate_frames.clear()

            mr = self.map_reply
            if mr is not None:
                pending += mr
                self.map_reply = None

            # ---- 链路往返延迟(`--rtt-ms`): 模拟蓝牙/CH340 转发 ----
            if pending:
                if self.rtt_s > 0.0:
                    delayed.append((time.perf_counter() + self.rtt_s, bytes(pending)))
                else:
                    tx += pending
            if delayed:
                t_now = time.perf_counter()
                if delayed[0][0] <= t_now:  # 队首已到期才扫描, 常态零开销
                    rest = []
                    for rel, data in delayed:
                        if rel <= t_now:
                            tx += data
                        else:
                            rest.append((rel, data))
                    delayed = rest

            # ---- 发送(约 5ms 一批, 降低 syscall 开销) ----
            if tx and (time.perf_counter() - last_flush > 0.005 or len(tx) > 4096):
                try:
                    conn.sendall(bytes(tx))
                except OSError:
                    return
                tx.clear()
                last_flush = time.perf_counter()

            time.sleep(0.0005)


def main() -> None:
    ap = argparse.ArgumentParser(description="电脑鼠设备模拟器(协议与固件一致)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("--no-auto", action="store_true", help="不自动跑探索, 需手动 MAZE RUN")
    ap.add_argument("--safe", action="store_true",
                    help="安全测试模式: 置'电机禁用'标志且速度恒 0(对齐 MOUSE_TEST_NO_MOTOR 固件)")
    ap.add_argument("--seed", type=int, default=20260925)
    ap.add_argument("--rtt-ms", type=float, default=0.0,
                    help="给每条命令的应答注入整趟往返延迟(ms), 模拟真机的蓝牙/CH340 转发; "
                         "默认 0(亚毫秒)。真机实测约 300 —— 用它可以离线复现"
                         "'按快链路写的时序假设'类缺陷(如连接握手只等 50 ms)")
    args = ap.parse_args()
    sim = MouseSim(auto_explore=not args.no_auto, seed=args.seed, safe=args.safe,
                   rtt_ms=args.rtt_ms)
    try:
        sim.run(args.host, args.port)
    except KeyboardInterrupt:
        print("\n[sim] 退出")


if __name__ == "__main__":
    main()
