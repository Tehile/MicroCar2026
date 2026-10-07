"""门面层的公共类型: 结果对象与误用异常。

单独成文件是为了让各个子 API(api/params.py 等)能在不 import client.py 的情况下
拿到 `Result` —— 否则会形成循环导入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class ReentrancyError(RuntimeError):
    """在调度线程的订阅回调里调用了会阻塞等待设备的方法。

    这类误用会与调度线程自身死锁(回调在等一个只有同一线程才能推进的状态),
    所以宁可立刻报错, 并由门面转成一个 ERROR 事件送到事件流里。
    """


@dataclass
class Result:
    """一次命令执行的结果。

    ok     : 是否成功(设备回了 `ACK ... OK`)
    status : 失败原因, 如 `ERR_NAME` / `ERR_CRC` / `TIMEOUT`
    data   : 附加信息(回读值、尝试次数等)
    """

    ok: bool
    status: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:
        return f"Result(ok={self.ok}, status={self.status!r}, data={self.data!r})"


def clamp_timeout(t: Optional[float], default: float) -> float:
    return default if t is None or t <= 0 else float(t)
