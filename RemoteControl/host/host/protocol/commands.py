"""下行命令层: 构造文本命令、ACK 状态机、超时重发、幂等重放、参数回读。

可靠性四层(与方案文档 §5.7 对应):
  L1 命令行带 CRC16, 固件校验失败回 ERR_CRC
  L2 固件执行后回 `ACK <seq> <OK|ERR_xxx>`
  L3 超时未收 ACK 则重发(最多 MAX_ATTEMPTS 次)
  L4 写参数后由上层调用 readback() 做 GET 回读校验

幂等: 重发时**字节完全不变**(同 seq 同 CRC), 固件据此判定为重复并重放上次应答,
      不会重复执行副作用(重复写参数、重复触发标定动作)。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .codec import crc16_modbus, float_to_q1616, q1616_to_float

# ---- 生产标定参数(与固件 HARDWARE/link.h 的 LINK_P_* 一致; **任何**固件都有) ----
PARAM_IDS_BASE: Dict[str, int] = {
    "TRACKL": 1,
    "TRACKR": 2,
    "SPEEDL": 3,
    "SPEEDR": 4,
    "GYRORATIO": 5,
    "ENCANGLE": 6,
    "WALLDIST": 7,
}

# ---- 红外测量与阈值(22-32) / 电源监控(33-38) ----
# 与固件 HARDWARE/link.h 的 LINK_P_IR* / LINK_P_BATT* **必须逐项一致**。
# 这一段属**生产命名空间**(任何固件都有), 因此落在固件的 linkParam[] 里,
# `GET ALL` / `GET <name>` / `SETQ` / `SAVE` 四条路径自动覆盖, 不需要额外开关。
# 默认值 = 原来写死在 core.h / safety.c 里的常量, 所以不写参数时行为逐位不变。
PARAM_IDS_MEAS: Dict[str, int] = {
    # 红外阈值与滤波(取代 core.h 的 WALL_* 宏)
    "IRTHRWALL": 22,  # 侧墙阈值(唯一真源 = wallSns.wallThresh)
    "IRTHR45": 23,  # 45 度阈值
    "IRTHRCRIT": 24,  # 临界墙阈值
    "IRTHRFLO": 25,  # 前墙远距-低
    "IRTHRFMID": 26,  # 前墙远距-中
    "IRTHRFHI": 27,  # 前墙远距-高
    "IRTHRFNEAR": 28,  # 前墙近距
    "IRKALQ": 29,  # 卡尔曼过程噪声 ×10000
    "IRKALP": 30,  # 卡尔曼测量噪声 ×10000
    "IRKALSW": 31,  # 卡尔曼开关 0/1
    "IRFRONTMODE": 32,  # 前向综合 0=均值 1=取小 2=加权
    # 电源监控(取代 safety.c 里写死的分压比/低电/步进/档位)
    "BATTKV": 33,  # 分压系数 µV/LSB
    "BATTLOWMV": 34,  # 电量档下限 mV
    "BATTHIGHMV": 35,  # 满电 mV
    "BATTALPHA": 36,  # IIR 系数 ×1000
    "BATTMAXPCT": 37,  # 档位上限
    "BATTMINPCT": 38,  # 档位下限
}

#: 名字 <-> id 的唯一来源。**v2.4 起只剩生产命名空间** —— 课程调参量(8..21)已随
#: 课程层一并删除, 因此这里的条目数与固件 `LINK_P_NUM`(=24)严格相等。
PARAM_IDS: Dict[str, int] = {**PARAM_IDS_BASE, **PARAM_IDS_MEAS}
PARAM_NAMES: Dict[int, str] = {v: k for k, v in PARAM_IDS.items()}

#: 生产命名空间(任何固件都有, 落 Flash page115) —— **`GET ALL` 只回这些**。
#: v2.4 起与 PARAM_IDS 完全重合; 保留这个别名是因为 `ParamsAPI.get_all()` 用它
#: 决定"要等哪些 id 到齐", 以及契约测试拿它做 ARG 表一致性断言。
PRODUCTION_PARAM_IDS: Dict[str, int] = dict(PARAM_IDS)

#: 分组标签, 仅供 CLI / 文档分节显示
PARAM_GROUPS: Dict[str, str] = {
    **{n: "生产标定" for n in PARAM_IDS_BASE},
    **{n: "红外测量" for n in PARAM_IDS_MEAS if n.startswith("IR")},
    **{n: "电源监控" for n in PARAM_IDS_MEAS if n.startswith("BATT")},
}

#: 枚举型参数的取值含义(0/1 类开关、模式号)。CLI/文档用来把数字翻成人话。
PARAM_ENUM: Dict[str, Dict[int, str]] = {
    "IRKALSW": {0: "关闭卡尔曼", 1: "启用卡尔曼"},
    "IRFRONTMODE": {0: "均值(现状)", 1: "取小(更保守)", 2: "加权"},
}

# (单位, 小数位, 说明)
PARAM_META: Dict[str, Tuple[str, int, str]] = {
    "TRACKL": ("pulse/mm", 4, "左轮里程系数; 走一格判据的直接因子, 最值得标定"),
    "TRACKR": ("pulse/mm", 4, "右轮里程系数"),
    "SPEEDL": ("(pulse/ms)/(cm/s)", 6, "左轮速度换算; 恒 = TRACKL/100, 由上位机派生"),
    "SPEEDR": ("(pulse/ms)/(cm/s)", 6, "右轮速度换算; 恒 = TRACKR/100, 由上位机派生"),
    "GYRORATIO": ("LSB/度", 0, "陀螺角速率积分系数"),
    "ENCANGLE": ("pulse/度", 0, "左右轮脉冲差 -> 角度; 固件为 u16, 取整"),
    "WALLDIST": ("mm", 0, "车中心到墙距离; 固件为 u8, 取整"),
    # ---- 红外测量与阈值(22-32) ----
    "IRTHRWALL": ("mm", 0, "侧墙判定阈值; 唯一真源 = wallSns.wallThresh, 默认 160"),
    "IRTHR45": ("mm", 0, "45 度侧墙阈值(斜线矫正用); 默认 150"),
    "IRTHRCRIT": ("mm", 0, "临界墙阈值(斜线转弯起始点, 低于此值禁动); 默认 60"),
    "IRTHRFLO": ("mm", 0, "前墙远距-低(双传感器确认有墙); 默认 230"),
    "IRTHRFMID": ("mm", 0, "前墙远距-中; 默认 240"),
    "IRTHRFHI": ("mm", 0, "前墙远距-高; 默认 245"),
    "IRTHRFNEAR": ("mm", 0, "前墙近距(单传感器即判定有墙); 默认 80"),
    "IRKALQ": ("1e-4", 0, "卡尔曼过程噪声 ×10000(越小越信任预测); 默认 100 = 0.0100"),
    "IRKALP": ("1e-4", 0, "卡尔曼测量噪声 ×10000(越大越平滑但迟钝); 默认 2000 = 0.2000"),
    "IRKALSW": ("0|1", 0, "卡尔曼滤波开关; 默认 1"),
    "IRFRONTMODE": ("0-2", 0, "前向综合距离: 0=均值(现状) 1=取小(更保守) 2=加权; 默认 0"),
    # ---- 电源监控(33-38) ----
    "BATTKV": ("µV/LSB", 3, "分压系数: mV = adcBatt × BATTKV / 1000; 默认 2417 (3.3V×3÷4096×1000)"),
    "BATTLOWMV": ("mV", 0, "电量档下限(低于此值报低电); 默认 7000"),
    "BATTHIGHMV": ("mV", 0, "满电参考; step = (HIGH-LOW)/MAXPCT; 默认 7960 (=8×120mV 步进)"),
    "BATTALPHA": ("1e-3", 0, "电压一阶 IIR 系数 ×1000; 0=不滤波, 1000=完全跟随; 默认 100"),
    "BATTMAXPCT": ("1-8", 0, "电量档上限; 默认 8"),
    "BATTMINPCT": ("0-8", 0, "电量档下限; 默认 1"),
}

# 标定项名 -> 固件 CAL 子命令名
CAL_ITEMS = ["GYRO", "WALLDIST", "IRDATA", "GYRORATIO", "ENCANGLE"]


class CommandError(Exception):
    pass


@dataclass
class Pending:
    seq: int
    line: bytes
    cmd: str
    attempts: int = 0
    sent_at: float = 0.0
    done: bool = False
    ok: bool = False
    status: str = ""
    #: 超时后是否重发。默认为真(幂等重发是命令通道可靠性的基石);
    #: 对"一发就已产生持续性副作用"的命令必须关掉 —— v2.0 里只有 `CAL`
    #: (请求-响应: 采完才 ACK, 可能数秒)与 `MAP DUMP`(整块回传占用链路)。
    #: 重发不会让它们更快, 反而往正在进行的流程里插命令, 且把等待时间重置。
    retry: bool = True
    callbacks: List[Callable[[bool, str], None]] = field(default_factory=list)


class CommandClient:
    """命令客户端。线程安全: 发送可能在 UI 线程, 收行在读取线程。"""

    #: 单次尝试的等待上限。
    #: **真机实测(2026-10-05, COM7/CH340 + 蓝牙透传)**: 单条命令的应答往返
    #: 实测稳定在 **≈1.21 s**(120 B 应答的 MAP DUMP), 而不是更早记录的 221 ms。
    #: 也就是说这条链路本体只有约 100~200 B/s 的有效吞吐, 往返时间主要花在
    #: 蓝牙模块的缓冲/转发上, 与应答长度基本成正比。
    #: 取 2.5 s: 覆盖实测 RTT 的两倍余量。
    #: ⚠️ 历史上这个值取过 0.20 s(那时实测 221 ms)与 0.50 s —— 都**低于**现在的
    #: 实测 RTT, 会让"设备其实马上要回, 主机已经重发"甚至直接报 TIMEOUT。
    #: 改这条值必须以真机实测为准, 不要照抄历史数字。
    #: 幂等重发是安全的(字节不变 → 固件按 seq 重放上次应答)。
    #: 例外: 请求-响应式命令(CAL)与整块回传(MAP DUMP)、以及拉取用的 TELEM
    #: 由 CommandSpec.timeout / no_retry 单独指定, 不走这里。
    TIMEOUT_S = 2.5
    MAX_ATTEMPTS = 5

    def __init__(self, send_bytes: Callable[[bytes], None]) -> None:
        self._send = send_bytes
        self._lock = threading.RLock()
        self._seq = 0
        self._pending: Dict[int, Pending] = {}
        self.params: Dict[int, int] = {}  # id -> Q16.16
        self.params_dirty = True
        self.device: Dict[str, object] = {}
        self.log: List[Tuple[float, str, str]] = []  # (ts, dir, text) dir in {"->", "<-"}
        self.stats = {"sent": 0, "retries": 0, "timeouts": 0, "err": 0, "ok": 0}
        self.events: List[Tuple[float, str, str]] = []  # 非 ACK 的异步消息
        self.on_async: Optional[Callable[[str], None]] = None
        self._log_limit = 2000

    # ------------------------------------------------------------ 发送

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFF
        return self._seq

    def _build(self, cmd: str, seq: int) -> bytes:
        body = f"{cmd} #{seq}".encode("ascii")
        return body + f"*{crc16_modbus(body):04X}".encode("ascii") + b"\r\n"

    def send(self, cmd: str, wait: bool = False, timeout: float = 3.0,
             no_retry: bool = False) -> Pending:
        """发一条命令。wait=True 时阻塞等待应答(仅用于批处理, 不要在 UI 线程用)。

        `no_retry=True` 用于"副作用已经产生、重发无益"的命令(见 Pending.retry),
        与 `registry.CommandSpec.no_retry` 同名同义 —— `registry.run()` 直接转传。
        """
        seq = self._next_seq()
        line = self._build(cmd, seq)
        p = Pending(seq=seq, line=line, cmd=cmd, retry=not no_retry)
        with self._lock:
            self._pending[seq] = p
            self._append_log("->", cmd)
        self._send(line)
        p.attempts = 1
        p.sent_at = time.time()
        with self._lock:
            self.stats["sent"] += 1
        if wait:
            deadline = time.time() + timeout
            while time.time() < deadline:
                if p.done:
                    break
                time.sleep(0.01)
            if not p.done:
                raise CommandError(f"timeout waiting for: {cmd}")
            if not p.ok:
                raise CommandError(f"device rejected: {cmd} -> {p.status}")
        return p

    def send_many(self, cmds: List[str], stop_on_error: bool = True) -> List[Pending]:
        out = []
        for c in cmds:
            p = self.send(c, wait=True)
            out.append(p)
            if stop_on_error and not p.ok:
                break
        return out

    # ------------------------------------------------------------ 重发/超时

    def poll(self) -> None:
        """由定时器周期调用(建议 20ms): 处理超时与重发。"""
        now = time.time()
        expired: List[Pending] = []
        with self._lock:
            for p in list(self._pending.values()):
                if p.done:
                    continue
                if not p.retry:
                    # 不重发的命令**不在此处判超时** —— 它的到期应由调用者给的
                    # timeout 决定(`MouseClient._wait_pending` 会调 abandon 收尾)。
                    # 否则 `send_cmd("calib.cal", timeout=10.0)` 会在 0.5s 就被判死,
                    # 平白计一次 timeout, 而标定其实正在采集。
                    continue
                if now - p.sent_at < self.TIMEOUT_S:
                    continue
                if p.attempts >= self.MAX_ATTEMPTS:
                    # 不重发(副作用已产生) 或 重发次数用尽 -> 判超时, 交给调用者决定
                    p.done = True
                    p.ok = False
                    p.status = "TIMEOUT"
                    self.stats["timeouts"] += 1
                    expired.append(p)
                    continue
                p.attempts += 1
                p.sent_at = now
                self.stats["retries"] += 1
                self._append_log("->", p.cmd + f"  (重发 #{p.attempts})")
                self._send(p.line)  # 字节完全不变 -> 固件幂等重放
        for p in expired:
            self._finish(p)

    def abandon(self, p: Pending, status: str = "TIMEOUT") -> None:
        """调用者放弃等待时收尾: 标记挂起项并**从表中摘除**。

        必要性: `poll()` 对 `retry=False` 的命令不再自行到期(它的到期由调用者的
        timeout 决定), 所以必须由调用者负责摘除, 否则 Pending 会永久留在
        `_pending` 里造成泄漏, 而且 seq 复用时会与之冲突。
        """
        with self._lock:
            if p.done:
                return
            p.done = True
            p.ok = False
            p.status = status
            self._pending.pop(p.seq, None)
            self.stats["timeouts"] += 1

    # ------------------------------------------------------------ 接收

    def feed_line(self, line: str) -> None:
        """在读取线程中调用, 处理一行文本(应答或异步消息)。"""
        with self._lock:
            self._append_log("<-", line)
        if line.startswith("ACK "):
            parts = line.split(None, 2)
            if len(parts) >= 3:
                try:
                    seq = int(parts[1])
                except ValueError:
                    return
                status = parts[2].strip()
                with self._lock:
                    p = self._pending.get(seq)
                    if p is None or p.done:
                        return
                    p.done = True
                    # 状态串可能带附加字段(`OK <upMs>` / `OK <a> <b> <c>`),
                    # 因此用前缀判定成功, 而不是全等。
                    head = status.split(None, 1)[0].upper() if status else ""
                    p.ok = head == "OK"
                    p.status = status
                    if p.ok:
                        self.stats["ok"] += 1
                    else:
                        self.stats["err"] += 1
                self._finish(p)
            return
        self._handle_async(line)

    def _finish(self, p: Pending) -> None:
        with self._lock:
            self._pending.pop(p.seq, None)
            cbs = list(p.callbacks)
        for cb in cbs:
            try:
                cb(p.ok, p.status)
            except Exception:
                pass

    def _handle_async(self, line: str) -> None:
        with self._lock:
            if line.startswith("PONG "):
                # `PONG <proto> <fw> <mazetype> <txbuf> <rst>`
                # v2.0: VER 已并入 PING, 后两字段新增(txbuf = TX 环水位,
                # rst = 上次复位原因名, 见《方案》§10)。
                bits = line.split()[1:]
                if len(bits) >= 3:
                    self.device["proto"] = int(bits[0])
                    self.device["fw"] = int(bits[1])
                    self.device["mazetype"] = int(bits[2])
                if len(bits) >= 4:
                    try:
                        self.device["txbuf"] = int(bits[3])
                    except ValueError:
                        pass
                if len(bits) >= 5:
                    self.device["rst"] = bits[4]
            elif line.startswith("P "):
                parts = line.split()
                if len(parts) == 3:
                    try:
                        self.params[int(parts[1])] = int(parts[2])
                        self.params_dirty = True
                    except ValueError:
                        pass
            self.events.append((time.time(), "async", line))
        if self.on_async is not None:
            try:
                self.on_async(line)
            except Exception:
                pass

    def drain_events(self) -> List[str]:
        """取走并清空异步消息的文本部分。由 UI 定时器在**主线程**调用,
        这样标定向导等界面组件无需处理跨线程回调。"""
        with self._lock:
            out = [t for _ts, _k, t in self.events]
            self.events = []
        return out

    # ------------------------------------------------------------ 日志

    def _append_log(self, direction: str, text: str) -> None:
        self.log.append((time.time(), direction, text))
        if len(self.log) > self._log_limit:
            del self.log[: self._log_limit // 2]

    # ------------------------------------------------------------ 便捷命令

    def ping(self) -> Pending:
        return self.send("PING", wait=True)

    def get_all(self) -> Pending:
        return self.send("GET ALL", wait=True)

    def readback(self, name: str) -> Optional[int]:
        """回读单个参数(Q16.16), 超时返回 None。"""
        p = self.send(f"GET {name}", wait=True)
        if not p.ok:
            return None
        return self.params.get(PARAM_IDS[name])

    def set_param(self, name: str, value: float, readback: bool = True) -> Tuple[bool, str]:
        """写参数: 用 SETQ 下发精确 Q16.16, 并默认做回读校验(L4)。"""
        if name not in PARAM_IDS:
            return False, "ERR_NAME"
        q = float_to_q1616(value)
        p = self.send(f"SETQ {name} {q}", wait=True)
        if not p.ok:
            return False, p.status
        if readback:
            rb = self.readback(name)
            if rb is None:
                return False, "READBACK_TIMEOUT"
            if rb != q:
                return False, f"READBACK_MISMATCH(got {rb}, want {q})"
        return True, "OK"

    def save(self) -> Tuple[bool, str]:
        p = self.send("SAVE", wait=True)
        return p.ok, p.status

    def reset(self) -> Tuple[bool, str]:
        p = self.send("RESET", wait=True)
        return p.ok, p.status

    def map_dump(self, timeout: float = 5.0) -> Tuple[bool, str]:
        # 墙图整块回传(98B): 占用链路, 不重发。
        p = self.send("MAP DUMP", wait=True, timeout=timeout, no_retry=True)
        return p.ok, p.status

    def move_fwd(self, pulses: int, speed: int = 0) -> Tuple[bool, str]:
        cmd = f"MOVE FWD {int(pulses)}" + (f" {int(speed)}" if speed else "")
        p = self.send(cmd, wait=True)
        return p.ok, p.status

    def move_rot(self, pwm: int, ms: int = 0) -> Tuple[bool, str]:
        cmd = f"MOVE ROT {int(pwm)}" + (f" {int(ms)}" if ms else "")
        p = self.send(cmd, wait=True)
        return p.ok, p.status

    def stop(self) -> Tuple[bool, str]:
        p = self.send("STOP", wait=True)
        return p.ok, p.status

    def estop(self) -> Tuple[bool, str]:
        p = self.send("ESTOP", wait=True)
        return p.ok, p.status

    def tare(self) -> Tuple[bool, str]:
        p = self.send("TARE", wait=True)
        return p.ok, p.status

    def cal(self, item: str, act="START", timeout: float = 10.0) -> Tuple[bool, str]:
        """标定控制(**请求-响应**)。

        act ∈ {START, ABORT}, 或 IRDATA 的相位号 0..3。
        v2.0: `CAL` 不立刻回 ACK, 而是**等这一步采集完成才回** ——
        所以超时给到 10 s 且**不重发**(重发会往正在进行的采集里插命令)。
        ACK 到达即表示"这一步完成, 请摆下一个位置"; 结果值用 `GET` 回读。
        见《传输模式重构设计方案》§9。
        """
        p = self.send(f"CAL {str(item).upper()} {act}", wait=True,
                      timeout=timeout, no_retry=True)
        return p.ok, p.status

    def telem(self, timeout: float = 3.0) -> Pending:
        """拉取一帧遥测(v2.1)。应答体是 `ACK <seq> OK <upMs>`, 带着**完整 u32 时基**。

        这是命令层的裸入口 —— 面向用户的自动化请用门面的 `mc.stream.on()`
        (停等拉取循环, 见 `host/api/stream.py`)。

        ⚠️ 两条纪律, 违反会静默丢帧:
          1. **一次只发一条**(严格停等): 固件每 1ms tick 只消费一行命令, 成串发会丢
             (实测回收率: 背靠背 2 条仅 50%)。
          2. **不重发**(`no_retry`): 每次请求都必须真的产生一帧, 重发只会多要一帧。
        """
        return self.send("TELEM", wait=True, timeout=timeout, no_retry=True)


def q1616_to_display(q: int, name: str) -> str:
    """按参数元数据格式化显示值。"""
    decimals = PARAM_META.get(name, ("", 4, ""))[1]
    return f"{q1616_to_float(q):.{decimals}f}"
