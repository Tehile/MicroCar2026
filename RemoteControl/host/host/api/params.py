"""参数管理: 读写、回读校验、物理量派生、持久化与备份。

对应功能清单的 E 组。

两条与固件语义紧密相关的规则:

  1. 写参数走 `SETQ`(Q16.16 定点), 不走 `SET`(十进制) —— 后者精度不够,
     且 `atof` 在 F1 上是软件浮点, 固件侧要额外花时间。
  2. `speedRatio ≡ trackRatio / 100`, 是同一物理量的两种表达(实测
     53.2015/100 = 0.532015 分毫不差)。若各自独立标定, 会出现"里程准了
     但速度指令偏高 3%"这种隐性不一致, PID 的 ref(脉冲/ms) 与实际脉宽脱节。
     所以**一个物理量一个来源**: 标定 trackRatio 后由主机强制派生 speedRatio。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from ..protocol.codec import float_to_q1616, q1616_to_float
from ..protocol.commands import (
    PARAM_ENUM,
    PARAM_GROUPS,
    PARAM_IDS,
    PARAM_META,
    PARAM_NAMES,
    PRODUCTION_PARAM_IDS,
)
from .errors import Result

if TYPE_CHECKING:  # pragma: no cover
    from .client import MouseClient

#: trackRatio -> speedRatio 的派生关系。
SPEED_FROM_TRACK_DIVISOR = 100.0

DERIVE_PAIRS: Tuple[Tuple[str, str], ...] = (("TRACKL", "SPEEDL"), ("TRACKR", "SPEEDR"))


class ParamsAPI:
    """参数读写。全部方法会等待设备应答(ACK + 可选回读校验)。"""

    def __init__(self, mc: "MouseClient") -> None:
        self._mc = mc

    # ------------------------------------------------------------ 元数据

    def names(self) -> List[str]:
        return sorted(PARAM_IDS)

    def meta(self, name: str) -> Optional[Tuple[str, int, str]]:
        """(单位, 小数位, 说明)"""
        return PARAM_META.get(name)

    def is_valid(self, name: str) -> bool:
        return name in PARAM_IDS

    # ------------------------------------------------------------ 读

    def snapshot(self) -> Dict[str, float]:
        """**本地缓存**的参数值(由历次 GET / GET ALL 的回话填充)。

        注意这里是缓存而不是实时值: 要强制刷新请先调 `get_all()`。
        """
        out: Dict[str, float] = {}
        for nid, q in self._mc.cmd.params.items():
            n = PARAM_NAMES.get(nid)
            if n:
                out[n] = q1616_to_float(q)
        return out

    def get_all(self, timeout: float = 3.0, refresh: bool = True) -> Dict[str, float]:
        """回读全部参数。refresh=True 时先发 `GET ALL`。

        **只等生产命名空间**(1-7 + 22-38 = 24 项): 固件 `GET ALL` 的循环上界是
        `LINK_P_NUM`。v2.4 起课程命名空间已删除, 于是 `PRODUCTION_PARAM_IDS`
        与 `PARAM_IDS`**完全重合**(都是 24 项), 等待集合不再有歧义。
        """
        if refresh:
            r = self._mc.send_cmd("params.get_all", timeout=timeout)
            if not r.ok:
                return {}
            want = set(PRODUCTION_PARAM_IDS.values())
            self._mc.wait(lambda: want.issubset(set(self._mc.cmd.params)), timeout=1.0)
        return self.snapshot()

    def production_names(self) -> List[str]:
        """生产命名空间的参数名(任何固件都有, 且 `GET ALL` 能一次全回)。"""
        return sorted(PRODUCTION_PARAM_IDS)

    def get_raw(self, name: str, timeout: float = 3.0) -> Optional[int]:
        """回读单个参数的 Q16.16 原始值。失败返回 None。"""
        if name not in PARAM_IDS:
            return None
        nid = PARAM_IDS[name]
        with self._mc.cmd._lock:
            self._mc.cmd.params.pop(nid, None)  # 避免回读到上一次的缓存
        r = self._mc.send_cmd("params.get", name=name, timeout=timeout)
        if not r.ok:
            return None
        self._mc.wait(lambda: nid in self._mc.cmd.params, timeout=0.5)
        return self._mc.cmd.params.get(nid)

    def get(self, name: str, timeout: float = 3.0) -> Optional[float]:
        q = self.get_raw(name, timeout=timeout)
        return None if q is None else q1616_to_float(q)

    # ------------------------------------------------------------ 写

    def set_raw(
        self, name: str, q: int, readback: bool = True, timeout: float = 3.0
    ) -> Result:
        """写 Q16.16 原始值(不做换算)。"""
        if name not in PARAM_IDS:
            return Result(False, "ERR_NAME", {"name": name})
        q = int(q)
        r = self._mc.send_cmd("params.setq", name=name, q=q, timeout=timeout)
        if not r.ok:
            return r
        info: Dict[str, Any] = {"name": name, "q": q, "value": q1616_to_float(q)}
        if not readback:
            return Result(True, "OK", info)
        got = self.get_raw(name, timeout=timeout)
        if got is None:
            return Result(False, "READBACK_TIMEOUT", info)
        if got != q:
            return Result(False, f"READBACK_MISMATCH(got={got}, want={q})", {**info, "readback": got})
        return Result(True, "OK", {**info, "readback": got})

    def set(self, name: str, value: float, readback: bool = True, timeout: float = 3.0) -> Result:
        """写物理值。内部转 Q16.16 下发, 默认做回读校验(可靠性 L4 层)。"""
        if name not in PARAM_IDS:
            return Result(False, "ERR_NAME", {"name": name})
        return self.set_raw(name, float_to_q1616(float(value)), readback=readback, timeout=timeout)

    def set_many(
        self,
        mapping: Dict[str, float],
        readback: bool = True,
        derive_speed: bool = False,
        timeout: float = 3.0,
    ) -> List[Result]:
        """批量写。derive_speed=True 时对 TRACK* 自动派生对应的 SPEED*。"""
        plan: Dict[str, float] = dict(mapping)
        if derive_speed:
            for tr, sp in DERIVE_PAIRS:
                if tr in plan and sp not in plan:
                    plan[sp] = plan[tr] / SPEED_FROM_TRACK_DIVISOR
        return [
            self.set(n, v, readback=readback, timeout=timeout) for n, v in plan.items()
        ]

    def derive_speed_from_track(self, readback: bool = True, timeout: float = 3.0) -> List[Result]:
        """按 `speedRatio = trackRatio / 100` 重新派生速度系数。

        标定 trackRatio 之后必须调用(或在 set_many 时用 derive_speed=True),
        否则里程与速度指令会来自两个互相独立的来源, 迟早不一致。
        """
        cur = self.snapshot()
        out: List[Result] = []
        for tr, sp in DERIVE_PAIRS:
            if tr not in cur:
                continue
            out.append(self.set(sp, cur[tr] / SPEED_FROM_TRACK_DIVISOR, readback=readback, timeout=timeout))
        return out

    # ------------------------------------------------------------ 持久化

    def save(self, timeout: float = 5.0) -> Result:
        """把当前参数表写入固件 Flash。

        门面会在写入前自动导出一份旧值到会话目录(`calib/params_before_*.csv`)。
        """
        self.backup()
        return self._mc.send_cmd("params.save", timeout=timeout)

    def reset(self, timeout: float = 5.0) -> Result:
        """擦除参数页并恢复源码默认值(重启后生效)。"""
        return self._mc.send_cmd("params.reset", timeout=timeout)

    def backup(self) -> Optional[str]:
        """把当前参数导出为 CSV 留档。没有会话目录时返回 None。"""
        sess = self._mc.session_obj
        if sess is None:
            return None
        rows: List[List[str]] = []
        cur = self.snapshot()
        for n in self.names():
            q = self._mc.cmd.params.get(PARAM_IDS[n])
            rows.append([n, "" if q is None else str(q), f"{cur.get(n, float('nan')):.6f}"])
        try:
            return sess.backup_params(rows)
        except Exception:
            return None

    # ------------------------------------------------------------ 展示

    def table(self) -> str:
        """人可读的参数表(CLI 用)。按"生产标定 / 红外测量 / 电源监控"分节。

        v2.4: 原先的"课程调参"一节(14 项, 只有 `MOUSE_LESSON_LAB` 固件才有)随课程层
        一并删除 —— 现在**任何固件都是同一张 24 项表**。"""
        cur = self.snapshot()
        lines: List[str] = []
        last: Optional[str] = None
        for n in sorted(self.names(), key=lambda x: PARAM_IDS[x]):
            group = PARAM_GROUPS.get(n, "")
            if group != last:
                lines.append(f"── {group} ──")
                last = group
            unit, dec, note = PARAM_META.get(n, ("", 4, ""))
            v = cur.get(n)
            sv = "—" if v is None else f"{v:.{dec}f}"
            enum = PARAM_ENUM.get(n)
            if enum is not None and v is not None:
                sv = f"{sv} ({enum.get(int(round(v)), '?')})"
            lines.append(f"{n:<10}{sv:>16}{unit:<20}{note}")
        return "\n".join(lines)
