"""命令与帧的单一登记点(E1)。

扩展规约 —— 新增一项功能时只允许改本文件的表:

    新增一条下行命令  ->  COMMANDS 里加一个 CommandSpec
    新增一种上行帧    ->  FRAMES   里加一个 FrameSpec
    新增一个参数      ->  protocol/commands.py 的 PARAM_IDS / PARAM_META

两条由 contract_test.py 静态断言、由 CI 强制:

    a) transport/link.py 的帧分派必须走 FRAMES, 不得出现 `type == TYPE_*` 比较
       或 decode_* 直调(否则新增帧要改两处, 迟早漂移)
    b) 每个登记项都必须能被模拟器实际执行(契约测试逐条跑)

本模块只**描述**命令, 不实现传输。执行走 run(), 由调用者传入 CommandClient。

v2.0(2026-10-05): 命令集由 41 条精简到 18 条, 帧由 9 种精简到 2 种。
    删除: SNAP(全部域) / SCOPE(3 条) / RATE / VER / MAZE EV / MAZE RST。
    理由见《传输模式重构设计方案》§7.2 / §8.4。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Tuple

from .protocol.codec import (
    TYPE_MAP,
    TYPE_UNIFIED,
    decode_map,
    decode_unified,
)
from .protocol.commands import CommandClient

API_VERSION = "2.2.1"


# ---------------------------------------------------------------- 命令表

def _move_fwd(pulses: int, speed: int = 0) -> str:
    """speed 为 0 时省略 —— 固件据此走默认速度。与 commands.move_fwd 行为一致。"""
    return f"MOVE FWD {int(pulses)}" + (f" {int(speed)}" if speed else "")


def _move_rot(pwm: int, ms: int = 0) -> str:
    return f"MOVE ROT {int(pwm)}" + (f" {int(ms)}" if ms else "")


def _move_run(sp_l: int, sp_r: int, ms: int = 0, ir_mm: int = 0) -> str:
    """受"时间 + 红外"双护栏约束的远程运动(电机远程控制实验)。

    ms / ir_mm 为 0 时省略该参数, 由固件取默认值(2000 ms / 10 mm);
    ir_mm 传负值 (-1) 表示**关闭红外护栏**, 只验证时间护栏。
    """
    s = f"MOVE RUN {int(sp_l)} {int(sp_r)}"
    if ms or ir_mm:
        s += f" {int(ms)}"
        if ir_mm:
            s += f" {int(ir_mm)}"
    return s


def _cal(item: str, act: Any = "START") -> str:
    """`CAL <item> <act>` —— act 可为 START / ABORT, 或 IRDATA 的相位号(0..3)。

    v2.0: 标定是**请求-响应**的 —— GYRO/WALLDIST/IRDATA 的 START 不立刻回 ACK,
    而是等这一步采集完成才回(ACK 即"请摆下一个位置"), 见《方案》§9。
    """
    return f"CAL {str(item).upper()} {act}"


def _lesson_start(exp: int, a: int = 0, b: int = 0, c: int = 0, d: int = 0) -> str:
    """`LESSON START <exp> [a] [b] [c] [d]` —— 尾部为 0 的参数省略。

    固件用同一个整数解析器逐项取参, 取不到就按 0 处理, 因此"少写尾部 0"是安全的。
    参数含义随实验而定(见 host/host/api/lesson.py 的 LESSON_EXPS)。
    """
    vals = [int(a), int(b), int(c), int(d)]
    while vals and vals[-1] == 0:
        vals.pop()
    return " ".join([f"LESSON START {int(exp)}"] + [str(v) for v in vals])


@dataclass(frozen=True)
class CommandSpec:
    """一条下行命令。

    name  : API 名(点分层级), 也是 CLI 子命令与(将来)JSON-RPC 方法名的来源
    build : 由关键字参数拼出线上命令文本
    args  : build 接受的参数名(顺序即位置参数顺序), 供文档与参数校验使用
    ack   : 设备是否回 `ACK <seq> <status>`(全部为真, 保留字段以备将来单向命令)
    since : 引入该命令的 API 版本
    note  : 一句话说明, 会进自动生成的接口文档
    """

    name: str
    build: Callable[..., str]
    args: Tuple[str, ...] = ()
    ack: bool = True
    since: str = "1.0"
    note: str = ""
    #: 超时后是否重发。仅对"一发就已产生持续性副作用"的命令置 False:
    #: 重发不会让效果更快, 反而会往正在进行的流程里插命令。
    #: v2.0: `CAL` 整条归入 no_retry(采完才 ACK, 可能数秒) —— 见《方案》§9.3。
    no_retry: bool = False
    #: 该命令的应答等待时长(秒)。None = 用 CommandClient 的默认超时。
    timeout: float = None
    #: **时间敏感**: 发送前请遥测拉取器让路(`MouseClient.send_cmd` 会调
    #: `stream.suspend()`), 使命令独占链路。默认 False。
    #:
    #: ⚠️ 为什么需要(真机实测 2026-10-05): 固件**每 1ms tick 只消费一行命令**
    #: (`link.c`: `if (USART_RX_STA & 0x80) {...}`; bit7 未清时 `usart.c` 把后续
    #: 字节**直接丢弃**)。拉取器持续发 `TELEM` 时, 同一条 `STOP`/`ESTOP` 只要与
    #: 某条 `TELEM` 落进同一个 1ms 窗口就会被丢掉 —— 上位机要等 `TIMEOUT_S`(2.5s)
    #: 才重发, 现场就是"**按了急停半天不停**"; 最坏是命令一直没生效, 车被 MOVE 的
    #: 时间护栏停下(实测"转速归 0 用时 3220 ms"而护栏正是 3000 ms)。
    #: 让路是**有代价的**(要多等在途 `TELEM` 收尾 + 一小段静默, 约 0.2 s),
    #: 所以只给"慢一拍就出事"的命令打开, 普通读写命令照旧走快路径
    #: (实测普通命令在拉取器开着时 8/8 成功, 且有重发兜底)。
    urgent: bool = False
    #: 发送前的**现场安全检查**(可选)。签名 `fn(mc) -> Optional[Tuple[str, dict]]`:
    #: 返回 `None` = 放行; 返回 `(status, data)` = **拒绝本次发送**(直接把该结果当
    #: 返回值给调用者, **不碰链路**)。
    #:
    #: ⚠️ 为什么放在这里而不是某个 API 方法里(真机事故 2026-10-05):
    #: 原先 `MazeAPI.run()` 里也能做检查, 但只要有人写 `mc.send_cmd("maze.run")`
    #: 就绕过去了 —— 而现场脚本恰恰就是这么写的。放在登记表这一层, **所有**
    #: 走 `send_cmd` 的路径(脚本/CLI/datalog/未来 JSON-RPC)一律绕不开。
    #: 明确的绕过通道是 `send_raw("MAZE RUN")`(逃生舱, 一眼可见)。
    precheck: Callable[..., Any] = None


#: 发车(让电脑鼠自己跑)前的电池下限(mV)。低于它拒绝发车。
#: 默认 7000 与固件 `BATTLOWMV` 参数默认值一致 —— 但这是**上位机的独立防线**,
#: 改设备参数不会放宽它。
#: ⚠️ 依据(真机事故 2026-10-05): 当时 `battMv` 一路 7681 → 6747 → **3962 mV**,
#: 现场表现为"电机一直转、且对红外障碍物不再敏感"。低电时红外发射管变暗 ⇒
#: 回波微弱 ⇒ 墙检测失效; 电机驱动在跌落电压下行为也不再确定。而**固件对低电
#: 没有任何保护动作**(`safety.c` 只算出 `battMon.low` 供显示, 不会停车),
#: 所以这道闸只能由上位机把。
LAUNCH_MIN_MV = 7000


def _precheck_maze_run(mc: Any):
    """发车前检查电池。返回 None 放行, 或 (status, data) 拒绝。"""
    try:
        f = mc.telemetry.latest()
        mv = int((f or {}).get("battMv") or 0)
    except Exception:
        mv = 0
    # `mv == 0` 表示电池监视未开启(不是"没电"), 此时不拦 —— 否则会误锁死台架。
    if mv and mv < LAUNCH_MIN_MV:
        return (
            "ERR_LOWBATT",
            {
                "battMv": mv,
                "minMv": LAUNCH_MIN_MV,
                "hint": ("电池过低(远低于固件低电阈值), 低电会让红外墙检测与电机行为"
                         "异常, 发车可能失控; 请充电/换电后重试。确需发车请把电脑鼠"
                         "架离地面后用 send_raw('MAZE RUN')"),
            },
        )
    return None


COMMANDS: Dict[str, CommandSpec] = {
    # ---- 设备与协议 ----
    "ping": CommandSpec(
        "ping", lambda: "PING",
        note="心跳; 回 `PONG <proto> <fw> <mazetype> <txbuf> <rst>`, "
             "rst ∈ {POR,SW,IWDG,PIN} 是**上次复位原因**(连上后第一条就该发它)",
    ),
    # ---- 参数 ----
    "params.get": CommandSpec("params.get", lambda name: f"GET {name}", ("name",), note="回读单个参数(Q16.16)"),
    "params.get_all": CommandSpec("params.get_all", lambda: "GET ALL", note="回读全部生产参数(24 项)"),
    "params.setq": CommandSpec(
        "params.setq", lambda name, q: f"SETQ {name} {int(q)}", ("name", "q"),
        note="写精确 Q16.16 定点(推荐路径)",
    ),
    "params.set": CommandSpec(
        "params.set", lambda name, v: f"SET {name} {v}", ("name", "v"),
        note="写十进制(精度低, 仅供手工调试)",
    ),
    "params.save": CommandSpec("params.save", lambda: "SAVE", urgent=True,
                                      note="参数表写入 Flash"),
    "params.reset": CommandSpec("params.reset", lambda: "RESET", urgent=True,
                                        timeout=8.0, note="擦参数页并复位为源码默认值"),
    "system.reset": CommandSpec(
        "system.reset", lambda: "MCU RESET", urgent=True, timeout=6.0,
        note=(
            "**整机软复位**(`Sys_Soft_Reset`, SYSRESETREQ): 程序从 main() 重跑, "
            "用于把设备从**卡死**中救回来 —— 走迷宫的主循环里有大量非协作式阻塞等待"
            "(等编码器里程/传感器), 而 `STOP` 会让电机永不再转, 那些条件永远不成立, "
            "于是程序卡在函数内部、连 `MAZE STOP` 的退出检查都轮不到(真机实测)。 "
            "⚠️ 与 `params.reset` (发的是 `RESET`, 只擦参数不重启) **不是一回事**。 "
            "ACK 先回, 约 150ms 后才真正复位; 成功判据是**复位后 PING 报 `rst=SW`**。 "
            "仅 [1][2][3][4] 档支持(比赛固件回 ERR_VALUE), 且会打断任何进行中的运动"
        ),
    ),
    # ---- 运动控制 ----
    "tare": CommandSpec("tare", lambda: "TARE", note="陀螺角累加器与里程基准清零"),
    "stop": CommandSpec(
        "stop", lambda: "STOP",
        note="正常停车 + **解急停锁存**(测试/采集档); 这是重新获得控制权的唯一入口",
    ),
    "estop.clear": CommandSpec(
        "estop.clear", lambda: "STOP", urgent=True,
        note="`stop` 的同义入口(语义即「停车 + 解急停锁存」), 保留以便书写直观",
    ),
    "estop": CommandSpec("estop", lambda: "ESTOP", urgent=True,
                          note="急停(置 freemode, 并锁存直到 STOP)"),
    "move.fwd": CommandSpec("move.fwd", _move_fwd, ("pulses", "speed"), urgent=True, note="固定脉冲直线(开环)"),
    "move.rot": CommandSpec("move.rot", _move_rot, ("pwm", "ms"), urgent=True, note="固定 PWM 定长原地转(开环)"),
    "move.run": CommandSpec(
        "move.run", _move_run, ("sp_l", "sp_r", "ms", "ir_mm"), urgent=True,
        note=(
            "受双护栏约束的远程运动: 左右轮速度(cm/s) + 最长运行时间(默认2000ms) "
            "+ 红外停车阈值(默认10mm, 负值=关闭)。停止后上行 `STOPPED <REASON> #n`"
        ),
    ),
    # ---- 遥测(拉取式) ----
    "telem": CommandSpec(
        "telem", lambda: "TELEM", no_retry=True, timeout=3.0,
        note=(
            "**拉取一帧遥测**: 回一帧统一帧(53B) + `ACK <seq> OK <upMs>`(带时间锚)。"
            "v2.1 起遥测是**请求-响应**式的 —— 固件不再自时钟发送, 发多少帧完全由上位机决定。"
            "⚠️ 必须**一次只发一条**(严格停等): 固件每 1ms tick 只消费一行命令, 成串发必丢"
            "(实测回收率: 背靠背 2 条仅 50%)。上位机的 `api/stream.py` 已按停等实现，会自动调用它。"
            "不重发: 每次请求都必须真的产生一帧, 重发只会多要一帧。"
        ),
    ),
    # ---- 标定(请求-响应) ----
    "calib.cal": CommandSpec(
        "calib.cal", _cal, ("item", "act"), no_retry=True, timeout=10.0, urgent=True,
        note=(
            "标定项控制: item ∈ {GYRO,WALLDIST,IRDATA,GYRORATIO,ENCANGLE}。"
            "act ∈ {START,ABORT}, 或 IRDATA 的相位号 0..3。"
            "**采完才回 ACK**(ACK 即「这一步完成, 请摆下一个位置」); 最坏可能数秒, "
            "故超时 10s 且不重发。结果值用 `GET` 回读"
        ),
    ),
    # ---- 墙图 ----
    "maze.dump": CommandSpec("maze.dump", lambda: "MAP DUMP", no_retry=True, note="墙图整块回传(98B)"),
    # ---- 时间同步 / 数据收集 ----
    "arm": CommandSpec(
        "arm", lambda: "ARM",
        note=(
            "发车: 固件记下发车时刻 + 点亮 1Hz 时间基准灯(50% 占空比), "
            "并回 `ACK <seq> OK <armMs>`(发车时基随 ACK 回, 不再单发 ARMED 行)"
        ),
    ),
    "maze.run": CommandSpec(
        "maze.run", lambda: "MAZE RUN", urgent=True,
        precheck=_precheck_maze_run,
        note=(
            "远程发车跑无记忆走迷宫(**仅 MOUSE_DATA_LOG 档**; 其余固件回 ERR_VALUE)。 "
            "固件只置请求位, 真正的 while(1) 迷宫由主循环执行 —— "
            "所以下达后命令通道与遥测都不会被阻塞。 "
            "⚠️ 已在跑时回 `ERR_BUSY`, 急停锁存中回 `ERR_ESTOP`; "
            "收车用 `maze.stop`(或 `STOP`), 之后才能再次发车"
        ),
    ),
    "maze.stop": CommandSpec(
        "maze.stop", lambda: "MAZE STOP", urgent=True,
        note=(
            "**把电脑鼠带出迷宫**: 停车 + 复位发车状态 + 解锁急停"
            "(**仅 MOUSE_DATA_LOG 档**; 其余固件回 ERR_VALUE)。 "
            "发车后固件主循环停在 LESSON_Blind8() 的 while(1) 里, "
            "光靠 ESTOP/STOP 停不掉那个循环、发车状态无法复位 —— "
            "本命令让主循环回到等待态, 于是**不必断电就能重新发车**"
        ),
    ),
    # ---- 课程调参实验(仅 MOUSE_LESSON_LAB 固件; 生产固件回 ERR_VALUE) ----
    "lesson.start": CommandSpec(
        "lesson.start", _lesson_start, ("exp", "a", "b", "c", "d"),
        note=(
            "启动课程实验并自带时间护栏。exp: 2=编码器测距 3=匀变速直行 "
            "5=陀螺标定 6=多传感器直行; 结束时固件上行 `LESSON DONE ...`"
        ),
    ),
    "lesson.stop": CommandSpec(
        "lesson.stop", lambda: "LESSON STOP", note="中止课程实验(停车并回 LESSON DONE ... STOP)",
    ),
    "lesson.status": CommandSpec(
        "lesson.status", lambda: "LESSON STATUS",
        note="回一行 `LESSON STATUS <exp> <running>` 异步状态",
    ),
}


def run(client: CommandClient, cmd: str, wait: bool = True, **kw: Any):
    """按名字执行一条登记在册的命令。返回 Pending。

    这是"逃生舱"之上的**唯一**命令执行入口 —— CLI、契约测试、子 API 都走它,
    所以登记表就是命令的全集。
    """
    spec = COMMANDS.get(cmd)
    if spec is None:
        raise KeyError(f"未登记的命令: {cmd!r} (可用的见 registry.COMMANDS)")
    line = spec.build(**kw)
    to = {}
    if spec.timeout is not None:
        to["timeout"] = spec.timeout
    return client.send(line, wait=wait, no_retry=spec.no_retry, **to)


def command_line(cmd: str, **kw: Any) -> str:
    """只拼命令文本, 不发送(供文档、日志、测试比对)。"""
    spec = COMMANDS[cmd]
    return spec.build(**kw)


def spec_of(cmd: str) -> CommandSpec:
    return COMMANDS[cmd]


def names(prefix: str = "") -> list:
    return sorted(n for n in COMMANDS if n.startswith(prefix))


# ---------------------------------------------------------------- 帧表

def _decode_map_auto(p: bytes) -> dict:
    """墙图帧的 mazetype 由载荷长度反推(墙图 + 访问图各 n² 字节)。

    固件不重复发 mazetype, 所以这里必须自己算 —— 做成统一签名后, FRAMES 的
    decoder 全部是 `Callable[[bytes], dict]`, 分派代码不必为某一种帧开特例。
    """
    n = int(((len(p) - 4) // 2) ** 0.5 + 0.5)
    return decode_map(p, n)


@dataclass(frozen=True)
class FrameSpec:
    """一种上行帧。

    sink  : `TelemetryStore` 上的累积方法名。帧分派**完全由表驱动** ——
            新增一种帧不需要改 link.py 一行代码。
    count : 计入 `Link.counts` 的键名(None = 该帧不单独计数)
    """

    type: int
    name: str
    decoder: Callable[[bytes], dict]
    stream: str
    sink: str
    count: "str | None" = None
    since: str = "1.0"


FRAMES: Dict[int, FrameSpec] = {
    TYPE_UNIFIED: FrameSpec(TYPE_UNIFIED, "UNIFIED", decode_unified, "unified", "push_unified", "unified"),
    TYPE_MAP: FrameSpec(TYPE_MAP, "MAP", _decode_map_auto, "map", "push_map", "map"),
}


def frame_spec(ftype: int) -> "FrameSpec | None":
    return FRAMES.get(ftype)


# ---------------------------------------------------------------- 静态断言

#: link.py 里出现这些即说明帧分派被硬编码了 —— 新增帧就会漏改, 由契约测试报红。
FRAME_DISPATCH_FORBIDDEN = (
    "decode_unified(",
    "decode_map(",
)


def check_frame_dispatch_is_table_driven(link_source: str) -> list:
    """检查 link.py 的帧分派是否走表。返回违规行说明列表(空 = 通过)。

    允许 `import` 行出现这些名字吗? 不允许 —— 改表之后 link.py 根本不需要导入它们。
    """
    bad = []
    for i, line in enumerate(link_source.splitlines(), 1):
        s = line.strip()
        if s.startswith("#") or s.startswith("//"):
            continue
        for pat in FRAME_DISPATCH_FORBIDDEN:
            if pat in line:
                bad.append(f"link.py:{i}: 出现 {pat.rstrip('(')} —— 帧分派应走 registry.FRAMES")
    return bad
