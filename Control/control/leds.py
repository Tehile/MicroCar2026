"""状态B1 的 4 个 LED 位 → 三态（改版二十四，上游 2.13.0 / FW 311）。

**为什么要有这个模块，而不是让前端自己拼"未知"**：

* 三态判定只能有一处（铁律 32：一元素一条真相）。散在 JS 里就意味着
  "旧固件读成灭"这个错会在每个用到的地方各犯一次；
* `0` 与 `未知` **不是同一件事**：FW 310 及以下这 4 位是预留位、恒 0，
  读出来是 0 只说明"固件不报"，不代表灯灭了。现场看到 4 个"灭"会去查线路，
  而真正的原因是固件没实现 —— 这正是要避免的静默错。

**能力判据不看版本号（铁律 6）**：主判据是通道在不在 `channels` 里；
`fw` 只用来识别"通道在、但设备不会填这个位"的那一段（fw < 311）。
两者任一不成立就是 `None`（未知），**不猜 0**。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

#: 4 个灯在统一帧里的通道名（上游 `UNIFIED_CHANNELS` 末尾，顺序固定）。
LED_KEYS = ("led1", "led2", "led3", "led4")

#: 这 4 位从哪个固件起才真的在报（2.13.0 行：FW 310 → 311）。
#: 低于它时位是预留的**恒 0**，读到的 0 没有语义。
LED_MIN_FW = 311


def _fw_int(fw: Any) -> Optional[int]:
    """把 `device["fw"]` 转成 int。取不到/不认识 ⇒ None（**不是 0**）。

    上游 `PING` 回的 `fw` 目前是字符串（"311"），但也见过数字；
    两种都收，认不出就回 None —— 认不出时把灯判成"未知"，比判成"灭"安全。
    """
    if fw is None or isinstance(fw, bool):
        return None
    try:
        return int(str(fw).strip())
    except (TypeError, ValueError):
        return None


def led_states(rec: Optional[Dict[str, Any]],
               channels: Optional[Iterable[str]] = None,
               fw: Any = None) -> Dict[str, Optional[bool]]:
    """一帧遥测 → `{"led1": True|False|None, ...}`。

    参数
    ----
    rec
        `mc.telemetry.latest()` 的那份 dict（键是通道名）。
    channels
        这条流**实际有**的通道名（`caps.unified_channels`）。
        `None` = 调用方没有清单，此时**只按 `rec` 里有没有这个键**判断；
        给空列表 = 明确"这条流没有这些通道" ⇒ 全未知。
    fw
        `device["fw"]`，只用于识别"预留位"那一段。

    返回值的三种含义
    ----------------
    `True`  读到位且为 1（亮）；
    `False` 读到位且为 0（灭）—— **只在 fw ≥ 311 且通道在清单里时才会出现**；
    `None`  未知：没连接 / 通道不存在 / 固件还没实现这 4 位 / 该帧缺这个键。

    ⚠️ 绝对不要在这里做"全亮 = 发车"之类的业务判断：那是显示层的事，
    而且 300 ms 的窗口在本层 4~6 Hz 的帧率下本来就只能命中 0~2 帧。
    """
    out: Dict[str, Optional[bool]] = {k: None for k in LED_KEYS}

    # ① 固件会不会填这 4 位。判不出来（None）就不往下走 —— 判不了 ≠ 灭。
    fw_n = _fw_int(fw)
    if fw_n is None or fw_n < LED_MIN_FW:
        return out

    # ② 这条流有没有这 4 个通道。`channels=None` 表示调用方没给清单，
    #    退化成"rec 里有键就认"（老上游宿主库上 `rec` 里根本没有 led*）。
    have = None if channels is None else set(channels)

    if not isinstance(rec, dict) or not rec:
        return out

    for k in LED_KEYS:
        if have is not None and k not in have:
            continue                      # 通道不在清单里 ⇒ 保持 None
        v = rec.get(k)
        if v is None or isinstance(v, (str, bool)):
            # 字符串/布尔不是解码器的合法输出；判不了就未知（布尔 true/false
            # 看似能用，但它会掩盖"上游换了类型"这件事）。
            continue
        try:
            out[k] = bool(int(v))
        except (TypeError, ValueError):
            continue
    return out
