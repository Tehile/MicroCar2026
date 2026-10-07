"""串口枚举与蓝牙 SPP 口识别。

Windows 上配对蓝牙 SPP 设备后会生成"传出"COM 口, 出现在串口列表里。
识别依据: 端口描述/硬件 ID 里含 Bluetooth 相关字样。
不同系统的描述文本不同, 所以按关键字表匹配, 命中不了就标记为普通串口。

重要(实机踩坑): Windows 还会给同一条 SPP UUID 生成一个"传入"端口,
其硬件 ID 形如 ..._LOCALMFG&0000\\...\\...&000000000000_00000000。
这种端口**能正常打开**但永远不会收到任何数据 —— 它只是本地的空壳,
等着对端设备主动连进来。用它是排查"程序连上了却读不到数据"的头号陷阱,
所以这里单独标出方向并把它排到最后、在界面上明确标注不可用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

try:
    from serial.tools import list_ports as _list_ports
    _HAS_PYSERIAL = True
except Exception:  # pragma: no cover - 缺少 pyserial 时仍可枚举 TCP
    _HAS_PYSERIAL = False


# 关键字来自 Windows/Linux/macOS 常见驱动描述
_BT_KEYWORDS = (
    "bluetooth",
    "蓝牙",
    "bluetooth link",
    "bluetooth serial",
    "standard serial over bluetooth",
    "rfcomm",
    "spp",
    "bth",
    "csr",
    "bluegiga",
)

# 方向常量
DIR_USB = "usb"            # 普通/有线串口
DIR_OUTGOING = "outgoing"  # 蓝牙"传出"端口: 绑定了对端 MAC, 主动去连它
DIR_INCOMING = "incoming"  # 蓝牙"传入"端口: 本地空壳, 打开也永远没数据


def classify_direction(desc: str, hwid: str) -> str:
    """按硬件 ID 判断端口方向。"""
    h = (hwid or "").upper()
    if "BTHENUM" not in h:
        return DIR_USB
    # 传入端口特征: 本地厂商占位 + 全零对端地址
    if "LOCALMFG" in h or "000000000000" in (hwid or ""):
        return DIR_INCOMING
    if "VID&" in h:
        return DIR_OUTGOING
    return DIR_OUTGOING


def remote_mac(hwid: str) -> str:
    """从 BTHENUM 硬件 ID 里取出对端蓝牙 MAC, 取不到返回空串。"""
    h = (hwid or "").upper()
    if "BTHENUM" not in h:
        return ""
    for part in (hwid or "").split("\\"):
        tail = part[-12:]
        if len(part) >= 12 and all(c in "0123456789ABCDEFabcdef" for c in tail):
            if tail.upper() != "000000000000":
                m = tail.upper()
                return ":".join(m[i:i + 2] for i in range(0, 12, 2))
    return ""


@dataclass
class PortInfo:
    device: str
    description: str
    hwid: str
    is_bluetooth: bool
    direction: str = DIR_USB

    def label(self) -> str:
        if self.direction == DIR_INCOMING:
            return f"[蓝牙·传入·收不到数据] {self.device}  {self.description}"
        if self.direction == DIR_OUTGOING:
            mac = remote_mac(self.hwid)
            extra = f" -> {mac}" if mac else ""
            return f"[蓝牙·传出{extra}] {self.device}  {self.description}"
        tag = "蓝牙" if self.is_bluetooth else "串口"
        return f"[{tag}] {self.device}  {self.description}"

    @property
    def usable(self) -> bool:
        """传入端口不适合作为连接目标。"""
        return self.direction != DIR_INCOMING


def list_ports() -> List[PortInfo]:
    """返回本机串口列表; 可用的排前, 有线串口优先于蓝牙, 传入端口排最后。"""
    out: List[PortInfo] = []
    if _HAS_PYSERIAL:
        for p in _list_ports.comports():
            desc = (p.description or "").strip()
            hwid = (p.hwid or "").strip()
            blob = f"{desc} {hwid} {p.name or ''}".lower()
            is_bt = any(k in blob for k in _BT_KEYWORDS)
            direction = classify_direction(desc, hwid)
            out.append(PortInfo(device=p.device, description=desc, hwid=hwid,
                                is_bluetooth=is_bt, direction=direction))

    # 排序: 可用优先 -> 有线优先于蓝牙 -> 端口名数字序
    def key(x: PortInfo):
        return (0 if x.usable else 1,
                0 if x.direction == DIR_USB else 1,
                int("".join(ch for ch in x.device if ch.isdigit()) or 0))

    out.sort(key=key)
    return out


def find_bluetooth_port() -> str:
    """返回第一个可用的蓝牙 SPP 口(传出优先), 没有则返回空串。"""
    cands = [p for p in list_ports() if p.is_bluetooth and p.usable]
    for p in cands:
        if p.direction == DIR_OUTGOING:
            return p.device
    return cands[0].device if cands else ""
