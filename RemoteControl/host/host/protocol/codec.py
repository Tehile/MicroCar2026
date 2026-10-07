"""字节级编解码: CRC16 / Q16.16 / 上行二进制帧 / 流式分流解析器。

帧格式(与固件 link.c 完全一致):
    AA 55 | LEN(2, LE) | TYPE | SEQ | PAYLOAD(LEN) | CRC16(2, LE)

CRC16 覆盖 TYPE/SEQ/PAYLOAD, 采用 MODBUS 多项式 0xA001、初值 0xFFFF。
校验向量: crc16(b"123456789") == 0x4B37

与 printf 文本共存的依据:
    printf 只产出 0x09-0x7E + CR/LF, 永远不可能产出 0xAA,
    因此见到 0xAA 即尝试按帧解析, 校验失败则回退当作文本。

v2.0(2026-10-05): 周期帧只剩一种 —— `TYPE_UNIFIED`(45B payload)。
    旧的 fast/mid/slow/dump_*/time/maze 全部删除(见《传输模式重构设计方案》§4)。
    上行事件只剩两个文本行: `STOPPED <reason> #n` / `ESTOP TRIP #n`。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional

# ---------------------------------------------------------------- 常量

SYNC0 = 0xAA
SYNC1 = 0x55
FRAME_OVERHEAD = 8  # 同步2 + LEN2 + TYPE1 + SEQ1 + CRC2

TYPE_MAP = 0x07      # 墙图整块(静态数据, 按需拉取)
TYPE_UNIFIED = 0x0A  # 统一遥测帧(唯一周期帧)

UNIFIED_LEN = 45

#: 统一帧载荷里各字段的字节偏移(与固件 link.h 的 LINK_U_* 逐项对齐)。
#: **这是一处必须两侧同步的常量** —— 改动它等于改协议。
U_MS8 = 0
U_IRADC = 1
U_IRMM = 9
U_BATTMV = 17
U_SPEEDL = 19
U_SPEEDR = 21
U_GYRO = 23
U_ENCANG = 25
U_TRACKL = 27
U_TRACKR = 29
U_CELLODOM = 31
U_PIDERR = 33
U_STATE_A = 35
U_STATE_B = 36
U_STATE_C = 37
U_FLAGS = 38
U_POS = 39
U_GOAL = 40
U_WALLTHR = 41
U_STACKIDX = 42
U_FASTWAY = 43
U_DROP = 44

#: 统一帧的"扁平化"通道名 —— 位打包字段在解码时**直接展开**成独立键,
#: 于是 ring/monitor/analyze 看到的都是平铺字段, 不需要知道打包细节。
#: 顺序即 RingBuffer 的列顺序, 新增字段请追加在末尾(否则历史数据错列)。
UNIFIED_CHANNELS: List[str] = [
    # 测量量
    "ms8",
    "irAdcF1", "irAdcF2", "irAdcL", "irAdcR",
    "irF1", "irF2", "irL", "irR",
    "battMv",
    "speedL", "speedR",
    "gyroAngle", "encAngle",
    "trackL", "trackR", "cellOdom",
    "pidErr",
    # 状态机(由位打包解出)
    "dir", "mazeTask",
    "wallLeft", "wallFront", "wallRight",
    "motionTask", "irSwitch",
    "motionStep", "critL", "critR", "left45", "right45",
    "flags",
    "posX", "posY", "goalX", "goalY",
    "wallThresh", "stackIdx", "fastwayCount", "dropCnt",
]
#: 展平后共 39 项 —— 45 B 载荷里位打包的字节在这里被拆成独立键,
#: 所以列数(39) 小于字节数(45)。改动打包要同步这个数与 U_* 偏移。
assert len(UNIFIED_CHANNELS) == 39, len(UNIFIED_CHANNELS)

#: 统一帧的**额定上限**帧率(Hz) —— 53B/帧 ÷ 11.52 KB/s ≈ 217 Hz。
#: ⚠️ v2.1 起遥测是**拉取式**的, 实际帧率由链路能力与请求窗口决定(真机实测这条
#: 蓝牙链路只有约 100~200 B/s, 故实际只有个位数 Hz)。本值只用于**环形缓冲的容量
#: 估算**(按上限开, 保证任何速率下都能存满时间窗)与缺省显示, 不代表实际帧率。
#: 要实际帧率请用 `mc.telemetry.hz()`(按到达间隔实算)。
UNIFIED_HZ = 217.0

# 统一帧 flags 位
FLAG_FREEMODE = 0x01    # 自由模式 freemode==1: 电机输出被强制为 0(不是"解锁"!)
FLAG_SLEEP = 0x02       # 休眠
FLAG_EMERG = 0x04       # 急停生效
FLAG_SCOPING = 0x08     # [v2.0 恒 0] 已删除的 SCOPE 机制
FLAG_DUMPING = 0x10     # [v2.0 恒 0] 已删除的 dump 机制
FLAG_MOTOR_OFF = 0x20   # 固件安全测试模式: 电机被物理禁用
FLAG_ESTOP_LOCK = 0x40  # 急停**锁存**中: 新的 MOVE/CAL 会被拒, 须 STOP 解锁
FLAG_EVQ = 0x80         # v2.0(替代 RUNLOG): 有事件行被丢弃 → 事件流不完整

FLAG_NAMES: Dict[int, str] = {
    FLAG_FREEMODE: "自由模式(电机停)",
    FLAG_SLEEP: "休眠",
    FLAG_EMERG: "急停",
    FLAG_SCOPING: "采集中(已废弃)",
    FLAG_DUMPING: "回传中(已废弃)",
    FLAG_MOTOR_OFF: "安全模式(电机禁用)",
    FLAG_ESTOP_LOCK: "急停锁存",
    FLAG_EVQ: "事件有丢弃",
}

#: 迷宫朝向(mazeState.mouseDirection)
DIR_NAMES: Dict[int, str] = {0: "UP", 1: "RIGHT", 2: "DOWN", 3: "LEFT"}

#: 迷宫任务状态(mazeState.mouseTask) —— 与固件 STRATEGY/maze.h 的枚举一致
MAZE_TASK_NAMES: Dict[int, str] = {
    0: "WAIT", 1: "START", 2: "SEARCH", 3: "SPURT", 4: "SPURT45", 5: "SPURT90",
}

# 墙位编码(mapWall 低四位: bit0=上 bit1=右 bit2=下 bit3=左; 置位表示"无墙/可通行")
WALL_UP_OPEN = 0x01
WALL_RIGHT_OPEN = 0x02
WALL_DOWN_OPEN = 0x04
WALL_LEFT_OPEN = 0x08
WALL_LOW_NIBBLE = 0x0F

# 运动任务码(core.h MotionTask_t), 仅列常用的, 其余显示数字
MOTION_TASK_NAMES: Dict[int, str] = {
    0: "NULL",
    1: "GOSTRAIGHT",
    2: "TURN",
    4: "TURNAROUND",
    5: "STOP",
    17: "STRAIGHT45",
    27: "BEFORE90TURN",
    28: "BEFORE180TURN",
    30: "SMARTSTRAIGHT",
    32: "SMARTSTRAIGHTFAST",
    33: "STRAIGHTEYECLOSECOORADJ",
    34: "STRAIGHTPOLE",
    35: "ACC",
    36: "SLOWDOWN",
    37: "SPEEDUP",
    38: "KEEPWALLDISTANCE",
    39: "POINTTURN90",
    41: "FORWARDIRTEST",
    42: "SPURT90BLIND",
    43: "SPURT90IR",
    44: "SMARTSTRAIGHTSPURT",
    45: "SMARTSTRAIGHTSPURTL",
    46: "TURNLEFTFAST",
    47: "TURNRIGHTFAST",
    48: "EMERGENCY",
    49: "SUPERSTRAIGHT",
    50: "INFRSTRAIGHT",
    51: "FASTSTRAIGHT",
    52: "EDGESTRAIGHT",
    53: "CHANGESPEED",
    # ---- 搜索/冲刺转弯(与固件 core.h 的 MotionTask_t 101-107 一致) ----
    101: "LT90SEARCH",
    102: "RT90SEARCH",
    103: "POINTR90",
    104: "POINTL90",
    105: "STRAIGHT_SPURT_90",
    106: "LT90SPURT",
    107: "RT90SPURT",
}

#: 路口类型 —— 由 wallSns 的前/左/右开口相对当前朝向分类。
#: v2.0: 固件**不再上报**这个分类结果(MAZE EV 已删除), 改由本模块的
#: `classify_road()` 用统一帧里的三位原语在主机侧复刻(见设计方案 §8.4)。
ROAD_NAMES: Dict[int, str] = {
    0: "NONE", 1: "THRU", 2: "CORNER_L", 3: "CORNER_R",
    4: "TEE_FL", 5: "TEE_FR", 6: "TEE_LR", 7: "CROSS", 8: "DEAD",
}

ROAD_KINDS: Dict[str, str] = {
    "NONE": "未判定",
    "THRU": "直道",
    "CORNER_L": "左弯",
    "CORNER_R": "右弯",
    "TEE_FL": "岔路口(T型,缺右)",
    "TEE_FR": "岔路口(T型,缺左)",
    "TEE_LR": "岔路口(T型,前方封死)",
    "CROSS": "十字路口",
    "DEAD": "死路",
}


def classify_road(wall_left: int, wall_front: int, wall_right: int) -> int:
    """由三位墙原语复刻固件的路口分类(与 link.c 的 link_RoadKind 逐分支一致)。

    `wall_*` 语义与固件 wallSns 相同: **1 = 有墙(封死)**, 0 = 开口。
    返回 `ROAD_NAMES` 的键。固件端 20ms 去抖的规则由调用方自行决定是否复刻 ——
    这里的纯函数只做分类, 不做时序。
    """
    f = 0 if wall_front else 1
    l = 0 if wall_left else 1
    r = 0 if wall_right else 1
    if f and l and r:
        return 7
    if f and l:
        return 4
    if f and r:
        return 5
    if l and r:
        return 6
    if f:
        return 1
    if l:
        return 2
    if r:
        return 3
    return 8


# ---------------------------------------------------------------- CRC16

def _build_crc_table() -> List[int]:
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        table.append(crc)
    return table


_CRC_TABLE = _build_crc_table()


def crc16_modbus(data: bytes) -> int:
    """CRC16/MODBUS, 与固件 link_Crc16 输出逐位一致。"""
    crc = 0xFFFF
    for b in data:
        crc = (crc >> 8) ^ _CRC_TABLE[(crc ^ b) & 0xFF]
    return crc


# ---------------------------------------------------------------- Q16.16

def q1616_to_float(q: int) -> float:
    return q / 65536.0


def float_to_q1616(v: float) -> int:
    """四舍五入到最近的可表示值(与固件 link_F2Q/link_D2Q 的取整方向一致)。"""
    scaled = v * 65536.0
    return int(scaled + 0.5) if scaled >= 0 else -int(-scaled + 0.5)


def _sat16(v: int) -> int:
    """钳位到 int16 —— 与固件 `link_Sat16()` 同语义(越界截断而非抛异常)。"""
    v = int(v)
    if v > 32767:
        return 32767
    if v < -32768:
        return -32768
    return v


# ---------------------------------------------------------------- 帧

@dataclass
class Frame:
    type: int
    seq: int
    payload: bytes

    @property
    def type_name(self) -> str:
        return {
            TYPE_MAP: "MAP",
            TYPE_UNIFIED: "UNIFIED",
        }.get(self.type, f"0x{self.type:02X}")


def pack_frame(ftype: int, payload: bytes, seq: int = 0) -> bytes:
    """构造一个完整帧(固件与模拟器共用, 保证两侧字节级一致)。"""
    head = struct.pack("<BBHBB", SYNC0, SYNC1, len(payload), ftype, seq & 0xFF)
    body = head[4:] + payload  # TYPE/SEQ/PAYLOAD
    return head + payload + struct.pack("<H", crc16_modbus(body))


# ---------------------------------------------------------------- 载荷解码

def decode_unified(p: bytes) -> dict:
    """统一帧(45B) -> **扁平 dict**。

    位打包字段在这里**直接展开**成独立键(dir / mazeTask / wallLeft / ...),
    所以上层(ring / monitor / analyze)看到的是平铺字段, 不需要知道打包细节。

    字段与固件 `link_UnifiedFrame()` 的偏移逐字段对齐(见 U_* 常量);
    `contract_test` 会读固件源码比对, 防止两侧漂移。
    """
    b = p[:UNIFIED_LEN] + b"\x00" * max(0, UNIFIED_LEN - len(p))

    (ms8,) = struct.unpack_from("<B", b, U_MS8)
    adc_f1, adc_f2, adc_l, adc_r = struct.unpack_from("<HHHH", b, U_IRADC)
    ir_f1, ir_f2, ir_l, ir_r = struct.unpack_from("<hhhh", b, U_IRMM)
    (batt_mv,) = struct.unpack_from("<H", b, U_BATTMV)
    sp_l, sp_r, gyro, enc, tkl, tkr, cell, perr = struct.unpack_from("<hhhhhhhh", b, U_SPEEDL)

    sa = b[U_STATE_A]
    sb = b[U_STATE_B]
    sc = b[U_STATE_C]
    flags = b[U_FLAGS]
    pos = b[U_POS]
    goal = b[U_GOAL]

    d = {
        "ms8": ms8,
        "irAdcF1": adc_f1, "irAdcF2": adc_f2, "irAdcL": adc_l, "irAdcR": adc_r,
        "irF1": ir_f1, "irF2": ir_f2, "irL": ir_l, "irR": ir_r,
        "battMv": batt_mv,
        "speedL": sp_l, "speedR": sp_r,
        "gyroAngle": gyro, "encAngle": enc,
        "trackL": tkl, "trackR": tkr, "cellOdom": cell,
        "pidErr": perr,
        # -- 状态A --
        "dir": sa & 0x03,
        "mazeTask": (sa >> 2) & 0x07,
        "wallLeft": (sa >> 5) & 0x01,
        "wallFront": (sa >> 6) & 0x01,
        "wallRight": (sa >> 7) & 0x01,
        # -- 状态B --
        "motionTask": sb & 0x7F,
        "irSwitch": (sb >> 7) & 0x01,
        # -- 状态C --
        "motionStep": sc & 0x0F,
        "critL": (sc >> 4) & 0x01,
        "critR": (sc >> 5) & 0x01,
        "left45": (sc >> 6) & 0x01,
        "right45": (sc >> 7) & 0x01,
        "flags": flags,
        # -- 坐标/决策 --
        "posX": pos & 0x0F,
        "posY": (pos >> 4) & 0x0F,
        "goalX": goal & 0x0F,
        "goalY": (goal >> 4) & 0x0F,
        "wallThresh": b[U_WALLTHR],
        "stackIdx": b[U_STACKIDX],
        "fastwayCount": b[U_FASTWAY],
        "dropCnt": b[U_DROP],
    }
    # 派生的可读量(纯复算, 不占带宽)
    d["freemode"] = bool(flags & FLAG_FREEMODE)
    d["sleep"] = bool(flags & FLAG_SLEEP)
    d["emerg"] = bool(flags & FLAG_EMERG)
    d["scoping"] = bool(flags & FLAG_SCOPING)
    d["dumping"] = bool(flags & FLAG_DUMPING)
    d["motors_off"] = bool(flags & FLAG_MOTOR_OFF)
    d["estop_lock"] = bool(flags & FLAG_ESTOP_LOCK)
    d["evq"] = bool(flags & FLAG_EVQ)
    d["dirName"] = DIR_NAMES.get(d["dir"], str(d["dir"]))
    d["mazeTaskName"] = MAZE_TASK_NAMES.get(d["mazeTask"], str(d["mazeTask"]))
    d["motionTaskName"] = MOTION_TASK_NAMES.get(d["motionTask"], str(d["motionTask"]))
    d["road"] = classify_road(d["wallLeft"], d["wallFront"], d["wallRight"])
    d["roadName"] = ROAD_NAMES.get(d["road"], "NONE")
    return d


def pack_unified(d: dict) -> bytes:
    """扁平 dict -> 统一帧 45B 载荷。**decode_unified 的对偶**, 供模拟器与测试使用。

    ⚠️ 这是"协议常量"的第二处实现(第一处是固件 link_UnifiedFrame)。两侧靠
    `U_*` 偏移与位打包规则对齐; `contract_test` 会用
    `decode_unified(pack_unified(d)) == d` 做往返断言, 防止主机侧自身漂移;
    与**固件**的一致性则由刻意保持的逐字段镜像来保证(见《传输模式接口设计》§2)。

    缺字段按 0 处理(模拟器不必为每个派生字段都赋值), 越界整数按固件同样的
    s16/u16 规则钳位 —— 否则一次异常输入就会让 struct.pack 抛错,
    模拟器连接处理被中断, 实机上表现为"突然全部超时"。
    """
    def g(key: str) -> int:
        return int(d.get(key, 0) or 0)

    out = bytearray(UNIFIED_LEN)
    out[U_MS8] = g("ms8") & 0xFF

    struct.pack_into("<HHHH", out, U_IRADC,
                     _sat16(g("irAdcF1")) & 0xFFFF, _sat16(g("irAdcF2")) & 0xFFFF,
                     _sat16(g("irAdcL")) & 0xFFFF, _sat16(g("irAdcR")) & 0xFFFF)
    struct.pack_into("<hhhh", out, U_IRMM,
                     _sat16(g("irF1")), _sat16(g("irF2")),
                     _sat16(g("irL")), _sat16(g("irR")))
    struct.pack_into("<H", out, U_BATTMV, _sat16(g("battMv")) & 0xFFFF)
    struct.pack_into("<hhhhhhhh", out, U_SPEEDL,
                     _sat16(g("speedL")), _sat16(g("speedR")),
                     _sat16(g("gyroAngle")), _sat16(g("encAngle")),
                     _sat16(g("trackL")), _sat16(g("trackR")),
                     _sat16(g("cellOdom")), _sat16(g("pidErr")))

    out[U_STATE_A] = ((g("dir") & 0x03)
                      | ((g("mazeTask") & 0x07) << 2)
                      | ((g("wallLeft") & 0x01) << 5)
                      | ((g("wallFront") & 0x01) << 6)
                      | ((g("wallRight") & 0x01) << 7))
    out[U_STATE_B] = (g("motionTask") & 0x7F) | ((g("irSwitch") & 0x01) << 7)
    out[U_STATE_C] = ((g("motionStep") & 0x0F)
                      | ((g("critL") & 0x01) << 4)
                      | ((g("critR") & 0x01) << 5)
                      | ((g("left45") & 0x01) << 6)
                      | ((g("right45") & 0x01) << 7))
    out[U_FLAGS] = g("flags") & 0xFF
    out[U_POS] = (g("posX") & 0x0F) | ((g("posY") & 0x0F) << 4)
    out[U_GOAL] = (g("goalX") & 0x0F) | ((g("goalY") & 0x0F) << 4)
    out[U_WALLTHR] = g("wallThresh") & 0xFF
    out[U_STACKIDX] = g("stackIdx") & 0xFF
    out[U_FASTWAY] = g("fastwayCount") & 0xFF
    out[U_DROP] = g("dropCnt") & 0xFF
    return bytes(out)


def decode_map(p: bytes, mazetype: int) -> dict:
    """墙图帧: cX,cY,dir,task + mapWall[n*n] + mapVisited[n*n] (行主序 x 外层)。"""
    n = mazetype
    cx, cy, direction, task = struct.unpack("<bbBB", p[:4])
    wall = list(p[4 : 4 + n * n])
    visited = list(p[4 + n * n : 4 + 2 * n * n])
    grid = [[wall[x * n + y] for y in range(n)] for x in range(n)]
    vis = [[visited[x * n + y] for y in range(n)] for x in range(n)]
    return {
        "posX": cx,
        "posY": cy,
        "direction": direction,
        "mouseTask": task,
        "mapWall": grid,
        "mapVisited": vis,
        "mazetype": n,
    }


# ---------------------------------------------------------------- 上行事件行

#: `STOPPED` 的原因码 -> 人话(与固件 link.h 的 LINK_RUN_REASON_* 一致)
RUN_REASON_NAMES: Dict[str, str] = {
    "TIME": "时间护栏到",
    "IR": "近墙保护(红外护栏)",
    "ESTOP": "急停",
    "USER": "STOP 命令",
    "END": "任务正常跑完",
}

#: 上行事件名 -> 人话(v2.0 只有这两条)
EVENT_NAMES: Dict[str, str] = {
    "STOPPED": "任务结束",
    "ESTOP": "急停触发",
}

#: 红外通道代号 -> 人话
IR_CHANNEL_NAMES = {"F1": "前左", "F2": "前右", "L": "左", "R": "右"}


def _split_seq(parts: List[str]) -> "tuple[List[str], Optional[int]]":
    """把行尾的 `#n` 摘出来。返回 (其余字段, seq 或 None)。"""
    if parts and parts[-1].startswith("#"):
        try:
            return parts[:-1], int(parts[-1][1:])
        except ValueError:
            return parts, None
    return parts, None


def parse_event_line(line: str) -> Optional[dict]:
    """解析一条上行事件行 -> dict; 不是事件行返回 None。

    两种形态(见《传输模式重构设计方案》§8):
        `STOPPED <reason> #<n>`
        `ESTOP TRIP #<n>`

    容错策略与旧版一致: **短行不抛异常**, 缺的字段留空 ——
    固件版本可能领先/落后于上位机, 一条不认识的新事件不该让整条事件流断掉。
    """
    parts = line.split()
    if not parts:
        return None
    if parts[0].upper() == "STOPPED":
        rest, seq = _split_seq(parts[1:])
        reason = rest[0].upper() if rest else ""
        return {
            "event": "STOPPED",
            "reason": reason,
            "reason_text": RUN_REASON_NAMES.get(reason, reason),
            "text": f"任务结束: {RUN_REASON_NAMES.get(reason, reason or '?')}",
            "seq": seq,
            "raw": line,
        }
    if parts[0].upper() == "ESTOP" and len(parts) >= 2 and parts[1].upper() == "TRIP":
        _rest, seq = _split_seq(parts[2:])
        return {
            "event": "ESTOP TRIP",
            "reason": "ESTOP",
            "reason_text": RUN_REASON_NAMES["ESTOP"],
            "text": "急停触发",
            "seq": seq,
            "raw": line,
        }
    return None


# ---------------------------------------------------------------- 流式解析器

class FrameStreamParser:
    """把任意切分的字节流切分为二进制帧与文本行, **保持两者的先后顺序**。

    设计要点(与固件 link.h 的共存策略对应):
      见到 0xAA 才尝试按帧解析; 长度不足则等待更多数据;
      CRC 校验失败则把该 0xAA 当普通文本字节吐出并前移一位,
      这样即使偶发错帧也不会卡死解析器(自恢复)。

    ⚠️ **返回一个有序列表而不是 (frames, lines) 两个列表** —— 这一条是 v2.0 的
    硬要求。v1.5.5 里帧与事件行的序号各自独立, 谁先谁后无所谓; v2.0 起两者
    **共用**一个 1 字节序号(固件 linkSeq), 于是"同一块 recv 数据里
    [帧, 事件, 帧]" 一旦被拆成两个列表再分别处理, 顺序就变成了 [帧, 帧, 事件],
    主机侧会把这**正常的交错**误判成丢帧(实测: 一次 STOPPED 就报 3 个缺口、
    256 条丢失)。顺序即协议, 所以这里必须保序。
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._text = bytearray()
        self.stats = {"frames": 0, "bad_crc": 0, "bad_sync": 0, "text_lines": 0, "bytes": 0}

    def feed(self, data: bytes) -> List["Frame | str"]:
        """投喂字节, 按**到达顺序**返回 `Frame` 与文本行(`str`)混排的列表。"""
        self._buf += data
        self.stats["bytes"] += len(data)
        out: List["Frame | str"] = []

        while self._buf:
            aa = self._buf.find(SYNC0)
            if aa < 0:
                # 整段都是文本。必须在这里也排空行缓冲 —— 否则当数据被切成
                # 逐字节投喂时(串口就会这样), 纯文本段永远走不到 aa>0 分支,
                # 文本行会一直积压在 _text 里发不出来。
                self._text += self._buf
                self._buf.clear()
                self._drain_lines(out)
                break
            if aa > 0:
                self._text += self._buf[:aa]
                del self._buf[:aa]
                self._drain_lines(out)
                continue

            # aa == 0, 尝试解析帧
            if len(self._buf) < FRAME_OVERHEAD:
                break
            if self._buf[1] != SYNC1:
                self.stats["bad_sync"] += 1
                self._text.append(SYNC0)
                del self._buf[:1]
                self._drain_lines(out)
                continue
            ln = self._buf[2] | (self._buf[3] << 8)
            total = FRAME_OVERHEAD + ln
            if len(self._buf) < total:
                break  # 等更多数据
            body = bytes(self._buf[4 : 6 + ln])
            crc_rx = self._buf[6 + ln] | (self._buf[7 + ln] << 8)
            if crc16_modbus(body) != crc_rx:
                self.stats["bad_crc"] += 1
                self._text.append(SYNC0)
                del self._buf[:1]
                self._drain_lines(out)
                continue
            out.append(Frame(type=self._buf[4], seq=self._buf[5], payload=bytes(self._buf[6 : 6 + ln])))
            self.stats["frames"] += 1
            del self._buf[:total]

        return out

    def _drain_lines(self, out: List["Frame | str"]) -> None:
        while True:
            nl = self._text.find(b"\n")
            if nl < 0:
                break
            raw = bytes(self._text[:nl])
            del self._text[: nl + 1]
            line = raw.rstrip(b"\r").decode("utf-8", "replace")
            if line:
                out.append(line)
                self.stats["text_lines"] += 1


def split_items(items: List["Frame | str"]) -> "tuple[List[Frame], List[str]]":
    """把 `feed()` 的有序列表拆成 (frames, lines) —— 供不关心顺序的调用方使用。

    需要顺序的调用方(链路读取线程)必须自己按列表顺序处理, 不要用本函数。
    """
    frames = [i for i in items if isinstance(i, Frame)]
    lines = [i for i in items if isinstance(i, str)]
    return frames, lines
