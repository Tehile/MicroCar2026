"""遥测拉取控制: PC 侧**停等**拉取器(`TELEM` 请求-响应)。

v2.0(2026-10-05) 曾是"固件自时钟持续流, 无 Hz 上限"。
v2.1(2026-10-05, **真机实测后**)改为**请求-响应**:

    PC  ->  TELEM
    设备 ->  一帧统一帧(53 B) + `ACK <seq> OK <upMs>`(带时间锚)

为什么必须改 —— 真机证据链:
  · 这条链路经过一对蓝牙透传模块, 其内部缓冲一旦被灌满就**停止转发且不会自行恢复**
    (必须给模块重新上电)。v1.5.5 的 `link.c` 里就写明了这一点, 并因此把常驻速率
    刻意压到 152 B/s。
  · v2.0 把这条约束当成"待重新验证的旧结论"丢掉了, 结果实测: 一收 `STREAM ON`
    就把 ≈4.3 KB/s 的全量帧持续压上去, 而这条链路实测只能承载约 100~200 B/s
    => 模块缓冲瞬间灌死, 从此连 `PING` 都收不到(只有断电才恢复)。
  · 改成拉取后: PC 不再请求 => 固件不再发送 => 缓冲自然排空, **无需断电即可自愈**。
    这正是"把发送节奏交给上位机"最实际的收益。

速率策略: **严格停等(stop-and-wait)** —— 一次只放一条 `TELEM`, 应答回来才发下一条。
  ⚠️ 这不是"软件限速", 而是**协议层的物理约束**, 且停等实测**比堆在途请求更快**:

  (a) 固件**每 1ms tick 只消费一行命令**(`link.c`: `if (USART_RX_STA & 0x80) {...}`;
      未清 bit7 时 `usart.c` 把后续字节直接丢弃)—— 所以成串发必丢。
      真机实测背靠背发 N 条 `TELEM` 的回收率:
          N=1 → 100%   N=2 → 50%   N=3 → 33%   N=4 → 25%   N=8 → 12%
      而**间隔 300ms 逐条发** → 100%。唯一变量就是"同一时刻有几条在飞"。
  (b) 固定窗口扫描(每档 6s, COM7):
          窗口 1 → 6.50 Hz (RTT 153ms) | 窗口 2 → 6.49 | 窗口 4 → 7.15 | 窗口 6 → 7.15
      窗口 1 已经拿到 **1/RTT 的满速**; 加到 6 只多 10%, 却开始成串丢失。

  所以"吃满物理带宽"的正解是**不要堆请求**: 停等的吞吐就是这条链路的上限。
  早期版本用 AIMD 自适应窗口(1→16)是**基于错误假设**(以为能流水线), 已在真机实测后删除。

时间锚: 每次 `TELEM` 的应答都带完整 u32 时基, 所以**锚随每一次拉取刷新** ——
  比 v2.0"开流时给一次"更稳, 失锁也能立刻自愈(见 `api/timesync.py`)。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .errors import Result

if TYPE_CHECKING:  # pragma: no cover
    from .client import MouseClient


class PullRequester:
    """**停等式**拉取器: 一次只放一条 `TELEM`, 应答回来才发下一条。

    线程契约: `tick()` 由 `MouseClient` 的调度线程周期调用(50 Hz);
    `start()` / `stop()` 由调用者线程调用。内部只用简单的标量, 不做加锁 ——
    与 `CommandClient` 一致(它自己加锁保护 `_pending`)。
    """

    #: 单条请求的等待上限。实测 RTT ≈150 ms, 故给 3 s 的宽余量。
    REQUEST_TIMEOUT_S = 3.0

    def __init__(self, mc: "MouseClient",
                 request_timeout: Optional[float] = None) -> None:
        self._mc = mc
        if request_timeout is not None:
            self.REQUEST_TIMEOUT_S = float(request_timeout)

        self.running = False
        self._out: List[Any] = []          # 在途请求(恒 ≤ 1 条)
        self._sent = 0
        self._done = 0
        self._lost = 0
        self._done_ts: List[float] = []    # 最近若干次完成的时刻(算实测 Hz)
        self.hz = 0.0
        self.best_hz = 0.0
        self._last_tick = 0.0
        #: 让路(suspend/resume)的嵌套计数与"原本是否在跑"的记忆 —— 见 suspend()。
        self._suspends = 0
        self._was_running = False

    # ------------------------------------------------------------ 启停

    def start(self) -> Result:
        """开始拉取。**非阻塞** —— 首发一条请求就返回, 锚在应答到达时自动入账。"""
        if self.running:
            return Result(True, "OK", {"inflight": len(self._out)})
        self.running = True
        self._done_ts = []
        self._refill()
        return Result(True, "OK", {"inflight": len(self._out)})

    #: `stop()` 时**等在途 `TELEM` 收尾**的上限(秒)。取 ~2× 实测 RTT(≈300 ms)。
    #: 见 `stop()` 里"为什么要等"的说明。
    STOP_QUIESCE_MAX_S = 0.60

    def stop(self) -> Result:
        """停止拉取。**先等最后一条在途 `TELEM` 收尾**, 再丢弃它。

        立刻停发是关键: 模块缓冲一旦积压, 只有"彻底不再发"才会排空。

        ⚠️ 为什么要"等"而不是直接丢(2026-10-07 真机实测, COM7/蓝牙透传):
          `TELEM` 一旦写出串口就**已经在线上**了 —— 固件要等下一个 1 ms tick 才
          消费它(`link.c` 每 tick 只吃一行), 而 `usart.c` 在 `USART_RX_STA & 0x80`
          未清期间**直接丢弃**随后到达的字节。于是
              `mc.stream.off()` → 紧接着 `send_cmd(...)`
          (也就是最自然的"先停流、再发车 / 再发急停"写法)会**撞上那个 1 ms 窗口**:
          实测 4 次 PING = 223 / **2702** / **2700** / **2705** ms, 即 **3/4 首发被丢**,
          只能干等 `TIMEOUT_S`(2.5 s) 超时重发。
          改为"先等在途那条 `TELEM` 拿到应答(应答到达 ⇒ 固件必然已消费完那一行)再返回"
          之后, 同一时序实测 **0/8** 丢首发(168/182/167 ms ...)。

          等待期间**不发任何字节**, 因此不会加重蓝牙模块缓冲积压 ——
          与"立刻停发"的设计初衷并不冲突(停发指的是不再产生**新**请求)。

        ⚠️ 仍保留 `abandon` 兜底: 上限挡住"应答真的丢了"的情况, 不让 `off()` 挂死。
        """
        was = self.running
        self.running = False  # 先关闸: tick() 从此不再补发新请求
        deadline = time.time() + self.STOP_QUIESCE_MAX_S
        out, self._out = self._out, []
        for p in out:
            while not p.done and time.time() < deadline:
                time.sleep(0.002)
            if not p.done:
                try:
                    self._mc.cmd.abandon(p, "STOPPED")
                except Exception:
                    pass
        return Result(True, "OK" if was else "ALREADY_OFF", {"inflight": 0})

    # ------------------------------------------------------------ 让路

    #: 让路时**等在途请求收尾**的上限(秒)。在途的那条 `TELEM` 一旦收尾, 线上就没人
    #: 抢 tick 了; 所以等到它 done 即可, 用上限兜住"应答丢了"的情况。
    QUIESCE_MAX_S = 0.30
    #: **应答没等到**时才用的保守静默(秒)。为什么只在超时时需要: 若在途的 `TELEM`
    #: 正常收到了应答, 就**证明固件已经把那一行消费完**(它先消费、才回帧),
    #: 接收端此刻必然空闲, 于是不需要再额外等; 反之若应答丢了, 就无法断定接收端
    #: 是否还咬着半行数据, 这时留一小段完全静默来兜底。
    QUIESCE_TAIL_S = 0.12

    def suspend(self) -> None:
        """**给命令通道让路**: 暂停拉取并等线上静默, 之后由调用方 `resume()`。

        ⚠️ 为什么必须这样做(真机实测 2026-10-05) —— 这是"急停/停止不立即生效"的根因:
          固件 `link.c` **每 1ms tick 只消费一行命令**(`if (USART_RX_STA & 0x80) {...}`;
          bit7 未清时 `usart.c` 把后续到达的字节**直接丢弃**)。而拉取器是**持续**发
          `TELEM` 的, 于是同时下发的 `STOP`/`ESTOP` 只要与某条 `TELEM` 落进同一个
          1ms 窗口就会被**丢掉**:
            · 上位机要等 `TIMEOUT_S`(2.5s) 超时才重发 -> 现场"按了急停半天才停";
            · 最坏情况是命令一直没生效, **车是被 MOVE 的时间护栏停下的**(真机实测:
              `转速归 0 用时 3220 ms`, 而护栏正是 3000 ms)。
          实测对照(原地转圈中发 STOP):
            · 拉取器开(修复前):     1/5 需重发, 转速归 0 中位 **3220 ms**(= 没生效)
            · 拉取器开 + 先让路:    **0/5 重发**, 转速归 0 中位 **751 ms**

        幂等/可嵌套: 用计数保护, 只有最外层真正暂停/恢复 —— 这样 `send_cmd` 里
        嵌套调用 `send_raw` 之类不会把自己提前恢复掉。
        """
        self._suspends += 1
        if self._suspends > 1:
            return
        if not self.running:
            return
        self._was_running = True
        self.running = False
        out, self._out = self._out, []
        deadline = time.time() + self.QUIESCE_MAX_S
        settled = True
        for p in out:
            while not p.done and time.time() < deadline:
                time.sleep(0.002)
            if not p.done:
                settled = False
                try:
                    self._mc.cmd.abandon(p, "SUSPEND")
                except Exception:
                    pass
        # 收到应答 => 固件已消费完那一行 => 接收端必然空闲, 无需额外等待。
        if not settled:
            time.sleep(self.QUIESCE_TAIL_S)

    def resume(self) -> None:
        """恢复 `suspend()` 暂停的拉取(仅当它原本在跑时才恢复)。"""
        self._suspends = max(0, self._suspends - 1)
        if self._suspends > 0:
            return
        if not self._was_running:
            return
        self._was_running = False
        self.running = True
        self._refill()

    # ------------------------------------------------------------ 每拍

    def tick(self) -> None:
        """由调度线程周期调用: 收割在途请求 -> 补发下一条 -> 更新统计。"""
        if not self.running:
            return
        now = time.time()

        self._last_tick = now

        keep: List[Any] = []
        for p in self._out:
            if p.done:
                if p.ok:
                    self._done += 1
                    self._done_ts.append(now)
                else:
                    self._lost += 1
                continue
            if now - p.sent_at > self.REQUEST_TIMEOUT_S:
                try:
                    self._mc.cmd.abandon(p, "TIMEOUT")
                except Exception:
                    pass
                self._lost += 1
                continue
            keep.append(p)
        self._out = keep

        self._refill()
        self._update_hz(now)

    def _refill(self) -> None:
        """**停等**: 只有在途为空时才发下一条 —— 绝不一次发多条。

        ⚠️ 这是本类最要紧的不变量。固件每 1 ms tick 只消费一行命令
        (见模块 docstring 里的实测回收率), 一次发两条就丢第二条。
        """
        if not self.running or self._out:
            return
        p = self._mc.cmd.send("TELEM", no_retry=True)
        # 每条应答都带完整时基 -> 逐条刷新时间锚
        p.callbacks.append(self._mc._absorb_anchor)
        self._out.append(p)
        self._sent += 1

    def _update_hz(self, now: float) -> None:
        """按最近 2 秒内完成的请求数算实测帧率。"""
        cut = now - 2.0
        while self._done_ts and self._done_ts[0] < cut:
            self._done_ts.pop(0)
        self.hz = len(self._done_ts) / 2.0
        self.best_hz = max(self.best_hz, self.hz)

    # ------------------------------------------------------------ 视图

    def snapshot(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "mode": "stop-and-wait",
            "inflight": len(self._out),
            "sent": self._sent,
            "done": self._done,
            "lost": self._lost,
            "hz": round(self.hz, 2),
            "best_hz": round(self.best_hz, 2),
        }


class StreamAPI:
    """遥测拉取开关(门面 `mc.stream`)。

    沿用 v1.5.5 的"显式开关"语义: 连上之后要 `mc.stream.on()` 才开始收遥测;
    `mc.stream.off()` 停止拉取。区别只在于开流不再是"命令固件持续发",
    而是"本机开始循环请求"。
    """

    def __init__(self, mc: "MouseClient") -> None:
        self._mc = mc
        self._req = PullRequester(mc)

    # ------------------------------------------------------------ 开关

    def on(self, timeout: float = 3.0) -> Result:
        """开始拉取遥测(首条应答即建立时间锚)。非阻塞。"""
        return self._req.start()

    def off(self, timeout: float = 3.0) -> Result:
        """停止拉取。**立刻停发** —— 这是让蓝牙模块缓冲排空的唯一手段。

        ⚠️ 会先花 ≤0.6 s **等在途的那条 `TELEM` 收尾**再返回(不额外发字节), 否则
        紧接着发来的命令有 ~3/4 概率撞上固件的 1 ms 收行窗口而被丢弃 —— 详见
        `PullRequester.stop()`。
        """
        return self._req.stop()

    def toggle(self, on: bool, timeout: float = 3.0) -> Result:
        return self.on(timeout=timeout) if on else self.off(timeout=timeout)

    @property
    def is_on(self) -> bool:
        """拉取器是否在跑(明确的内部状态, 不再靠"最近有没有帧"猜)。"""
        return self._req.running

    # ------------------------------------------------------------ 让路

    def suspend(self) -> None:
        """**给命令通道让路**: 暂停拉取并等线上静默(由 `resume()` 恢复)。

        供 `MouseClient.send_cmd()` / `send_raw()` 在发命令前后调用。
        直接调用者也可用: 例如"运动期间不想让遥测抢占链路"的实验。
        必须与 `resume()` 成对 —— 内部有嵌套计数保护。
        """
        self._req.suspend()

    def resume(self) -> None:
        """恢复被 `suspend()` 暂停的拉取(仅当它原本在跑时才恢复)。"""
        self._req.resume()

    # ------------------------------------------------------------ 视图/调参

    def stats(self) -> Dict[str, Any]:
        """拉取器状态: 在途数/收发计数/实测帧率。"""
        return self._req.snapshot()

    def tune(self, request_timeout: Optional[float] = None) -> Dict[str, Any]:
        """运行期调整超时(仅供实验; 会立即生效)。

        注意: **没有"窗口"可调** —— 拉取器是严格停等的, 见模块 docstring。
        """
        r = self._req
        if request_timeout is not None:
            r.REQUEST_TIMEOUT_S = max(0.2, float(request_timeout))
        return {"mode": "stop-and-wait", "request_timeout": r.REQUEST_TIMEOUT_S}

    # ------------------------------------------------------------ 时间锚

    def _absorb_anchor(self, ok: bool, status: str) -> Optional[int]:
        """从 `ACK <seq> OK <upMs>` 的状态串里取锚并写入 store。"""
        if not ok:
            return None
        parts = str(status or "").split()
        if len(parts) >= 2 and parts[0].upper() == "OK":
            try:
                up = int(parts[1])
            except ValueError:
                return None
            self._mc.store.set_anchor(up)
            return up
        return None

    def anchor(self):
        """当前时间锚 `(本机单调秒, 设备 ms)`; 未建立时 None。"""
        return self._mc.store.get_anchor()
