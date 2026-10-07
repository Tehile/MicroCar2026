"""连接管理: 端口枚举、连接/断开、链路统计与自检、单个端口诊断。

对应功能清单的 A 组与 L 组。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ..transport.link import LinkState, Target
from ..transport.ports import PortInfo, find_bluetooth_port, list_ports
from .errors import Result, clamp_timeout

if TYPE_CHECKING:  # pragma: no cover
    from .client import MouseClient

# 蓝牙透传链路的可持续吞吐实测只有约 150 B/s(见 docs 附录 F),
# 因此"有没有数据"不能用字节数粗判, 而要看能不能解析出合法帧。


class ConnAPI:
    """连接相关。多数方法在 `MouseClient` 上也有同名快捷入口。"""

    def __init__(self, mc: "MouseClient") -> None:
        self._mc = mc

    # ------------------------------------------------------------ 端口

    def ports(self) -> List[PortInfo]:
        """枚举串口。返回值已按『可用 > 有线 > 蓝牙传出 > 蓝牙传入』排序。"""
        return list_ports()

    def port_labels(self) -> List[str]:
        return [p.label() for p in self.ports()]

    def bluetooth_port(self) -> str:
        """猜一个蓝牙端口名(优先传出端口), 没有则返回空串。"""
        return find_bluetooth_port()

    def targets(self) -> List[Target]:
        """候选连接目标: 已排序的串口 + 两个模拟器端口。"""
        out = [Target(kind="serial", port=p.device, baud=115200) for p in self.ports()]
        out.append(Target(kind="tcp", host="127.0.0.1", tcp_port=5555))
        out.append(Target(kind="tcp", host="127.0.0.1", tcp_port=5556))
        return out

    def probe(self, port: str, baud: int = 115200, seconds: float = 2.0) -> Dict[str, Any]:
        """诊断单个串口: 能否打开、有没有数据、是否像遥测帧、是否像文本。

        直接开一个**临时**串口, 因此要求本客户端当前没有连着同一个口。
        这是把 diag_port.py 的能力内化进来, 便于脚本化排查。
        """
        cur = self._mc.link.target
        if self._mc.is_connected and cur is not None and cur.port == port:
            return {"ok": False, "error": "该端口已被本客户端占用, 请先 disconnect()"}
        try:
            import serial

            s = serial.Serial(port, baud, timeout=0.2)
        except Exception as e:
            return {"ok": False, "port": port, "baud": baud, "error": f"{type(e).__name__}: {e}"}
        try:
            s.reset_input_buffer()
            t0 = time.time()
            data = b""
            while time.time() - t0 < clamp_timeout(seconds, 2.0):
                d = s.read(4096)
                if d:
                    data += d
                if len(data) > 16384:
                    break
            from ..protocol.codec import FrameStreamParser, split_items

            p = FrameStreamParser()
            frames, lines = split_items(p.feed(data))
            printable = sum(1 for b in data if 0x20 <= b <= 0x7E or b in (9, 10, 13))
            return {
                "ok": True,
                "port": port,
                "baud": baud,
                "bytes": len(data),
                "bps": len(data) / max(clamp_timeout(seconds, 2.0), 1e-3),
                "frames": len(frames),
                "text_lines": len(lines),
                "bad_crc": p.stats["bad_crc"],
                "bad_sync": p.stats["bad_sync"],
                "printable_ratio": (printable / len(data)) if data else 0.0,
                "looks_like_telemetry": len(frames) > 0,
                "looks_like_text": len(lines) > 0 and len(frames) == 0,
                "head": data[:48].hex(" "),
            }
        finally:
            try:
                s.close()
            except Exception:
                pass

    def scan_baud(
        self, port: str, bauds: Optional[List[int]] = None, seconds: float = 1.0
    ) -> List[Dict[str, Any]]:
        """逐个波特率试读。用于确认电脑侧蓝牙模块的 UART 速率。

        判据(重要): **0 字节 ≠ 乱码**。
        波特率不对时会收到**乱码字节**(数据在流, 只是切错位);
        收到 0 字节说明对端根本没在发 —— 那是链路没建立, 不是速率问题。
        """
        rates = bauds or [115200, 9600, 38400, 57600, 19200, 230400, 4800, 1200]
        return [self.probe(port, b, seconds) for b in rates]

    # ------------------------------------------------------------ 连接

    def connect(self, target: Any = None, **kw) -> bool:
        return self._mc.connect(target, **kw)

    def disconnect(self) -> None:
        self._mc.disconnect()

    def wait_open(self, timeout: float = 3.0) -> bool:
        """等到链路真的可用(read 线程已起来)。"""
        return self._mc.wait(lambda: self._mc.link.is_open, timeout=timeout)

    @property
    def is_connected(self) -> bool:
        return self._mc.link.is_open

    @property
    def state(self) -> str:
        return self._mc.link.state

    @property
    def state_detail(self) -> str:
        return self._mc.state_detail

    @property
    def target(self) -> Optional[Target]:
        return self._mc.link.target

    # ------------------------------------------------------------ 统计

    def stats(self) -> Dict[str, Any]:
        return self._mc.link.stats()

    def self_check(self) -> Dict[str, Any]:
        """链路自检: {need_bps, got_bps, level, suggest_rate}。"""
        return self._mc.link.self_check()

    def is_degraded(self) -> bool:
        return self.self_check().get("level") == "degraded"

    def reconnect(self, delay: float = 0.5) -> bool:
        """断开后用同一目标重连(蓝牙掉线后的恢复路径)。"""
        t = self._mc.link.target
        if t is None:
            return False
        self._mc.disconnect()
        time.sleep(clamp_timeout(delay, 0.5))
        return self._mc.connect(t)

    # ------------------------------------------------------------ 救援

    def soft_reset(self, verify: bool = True, timeout: float = 8.0) -> Result:
        """**整机软复位**(发 `MCU RESET` → 固件 `Sys_Soft_Reset()`) —— 卡死救援口。

        为什么需要它: 走迷宫的主循环里有大量**非协作式阻塞等待**
        (`MOTION/motion.c` 的 `while (odomStr.cellOdom < dis) ;` 等), 它们等的是
        物理进度(编码器里程/传感器)。而 `STOP` 会把 `freemode` 置 1 ⇒ 电机 PWM 恒 0
        ⇒ 轮子永不再转 ⇒ **那些条件永远不成立**。结果是"越停越死": 命令通道照常回 OK,
        但程序卡在函数内部, `LESSON_Blind8()` 顶部的退出检查根本轮不到 ⇒ 只能断电。
        本方法提供一个不必断电的出口; 复位后 [4] 档自然回到发车等待循环。

        ⚠️ 与 `mc.params.reset()` **不是一回事**: 那个发的是 `RESET`, 只擦参数页、
        不重启; 本方法发的是 `MCU RESET`, 整机重启。名字像, 作用完全不同。

        ⚠️ 会**打断任何进行中的运动**(复位后电机回到自由模式), 仅 [1][2][3][4] 档支持
        (比赛固件回 `ERR_VALUE`)。别名 `rescue()`。

        参数:
          verify  : 复位后是否自动 PING 复核(推荐 True)。成功判据是固件上报的
                    复位原因变成 `SW`(软复位), 可与掉电(`POR`)区分开。
          timeout : 复核等待上限(秒)。

        返回 `Result`; 其 `data` 含:
          accepted : ACK 是否收到(**超时也可能是成功的** —— 复位可能在 ACK 出链路前发生)
          rst      : 复核到的复位原因(`SW` = 成功)
          verified : `rst == "SW"`
        """
        mc = self._mc
        mc._check_reentrancy("conn.soft_reset()")
        t0 = time.time()
        r = mc.send_cmd("system.reset", timeout=6.0)
        # 超时**不代表失败**: 固件先回 ACK 再延迟 ~150ms 复位, 但若 ACK 与复位
        # 抢在一起, ACK 可能没出得去。所以真正的判据只能靠复核。
        accepted = r.ok
        if not verify:
            return Result(True, "SENT", {"accepted": accepted, "cmd": r.status})

        # 复位需要一点时间, 之后再等设备"回来"(串口是常开的, 不重新连接)
        time.sleep(0.4)
        deadline = time.time() + clamp_timeout(timeout, 8.0)
        rst = None
        while time.time() < deadline:
            rr = mc.send_cmd("ping", timeout=3.0)
            if rr.ok:
                rst = mc.device.get("rst")
                break
            time.sleep(0.2)
        verified = (rst == "SW")
        detail = {
            "accepted": accepted,
            "rst": rst,
            "verified": verified,
            "elapsed_s": round(time.time() - t0, 2),
        }
        if verified:
            return Result(True, "RESET", detail)
        if rst is None:
            return Result(False, "NO_REPLY", detail)
        return Result(False, f"RST={rst}", detail)

    def rescue(self, **kw: Any) -> Result:
        """`soft_reset()` 的别名 —— 语义更直白: 把卡死的设备救回来。"""
        return self.soft_reset(**kw)
