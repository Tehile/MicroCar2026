"""遥测拉取控制: **停等**(`TELEM` 请求-响应, ~4Hz) 与 **批式**(`RATE PULL n t`, 默认 10Hz) 两种互斥模式。

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

v2.9(2026-10-15) 在停等之外加了**批式拉取**(`PacePull`, 见本模块的第二个类):
  · 形态 = **稀疏基频命令 + 每批多帧**: PC 每 T=n·t_ms 才发一条 `RATE PULL n t`,
    固件在这段时间里按 t_ms 间隔连发 n 帧。命令频率只有 1/T(默认 3.3Hz),
    传输量却等于 n/T(默认 10Hz) —— **命令密度与帧率解耦**;
  · 为什么不全甩给 `RATE FIX`: 2026-10-15 八组真机样本显示链路有 **~150ms 转发量子**
    (MCU 侧严格 t 一拍, PC 侧却两帧 <1ms 成对到达), 真实容量 ≈ **2 帧/150ms = 13.3Hz**;
    26Hz 级三组 15.5/95.2/113.3s 全断连, ≤13.3Hz 四样本均值 245.9s。详见
    `docs/链路MTBF压测方法.md` §16。默认档 `PULL 3 100`(10Hz, 容量 75%)留有 20% 余量;
  · 两种模式**互斥**: `mc.stream.pace()` 会先停掉停等式, 反之亦然。

时间锚: 每次 `TELEM` 的应答都带完整 u32 时基, 所以**锚随每一次拉取刷新** ——
  比 v2.0"开流时给一次"更稳, 失锁也能立刻自愈(见 `api/timesync.py`)。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .errors import Result
from .events import Event

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

    @property
    def paused(self) -> bool:
        """是否处于 `suspend()` 暂停中(嵌套计数 > 0)。v2.9 供门面路由用。"""
        return self._suspends > 0

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


class PacePull:
    """**批式拉取**(v2.9): 稀疏基频命令 + 每批多帧 —— `RATE PULL <n> <t>`。

    与 `PullRequester`(停等单条 `TELEM`)**互斥**, 由 `StreamAPI` 二选一管理。
    默认 `n=3, t_ms=100`(基频 3.33 Hz + 每批 3 帧 = 10 Hz 到达帧率)。

    为什么需要它(2026-10-15 真机压测全记录, COM7/蓝牙透传/fw 221, 详见
    `docs/链路MTBF压测方法.md` §13-15):
      停等式单条请求受 1/RTT 限制只有 ~4Hz。直接提速会死, 因为这条链路有
      **~150ms 的转发量子**: MCU 侧帧内 ms8 严格 t 一拍, PC 侧却两帧在 <1ms
      内成对到达 ⇒ 模块把帧攒批转发, 真实容量 ≈ 2 帧/150ms ≈ 13.3Hz。实测:
        · 26Hz 级(FIX / evt / PULL 密集)三组 15.5 / 95.2 / 113.3 s 全部断连;
        · 13.3Hz 顶格(PULL 3 75) 176.3s; 12Hz 无命令(FIX) 300 / 143s;
        · **10Hz 留余量(PULL 3 100) 300s / 240.9s**, 交付率 99.9% / 98.6%。
      ⇒ "稀疏命令 + 每批多帧"在容量 ~75% 处运行: 传输量 2.5 倍(4→10Hz),
         断连概率大幅低于顶格/超容。

    工作方式: PC 每 `T = n·t` 毫秒发一条 `RATE PULL n t`(幂等; 丢一条下一条
    自然补上 —— 这正是"基频+多帧"的自愈性), 固件收到后以 t 为节拍连发 n 帧。
    PC **不发** `TELEM`, 只做三件事:
      1. 到点发命令(提前 RTT 发, 让批首帧落在网格上);
      2. 每批首帧到达后更新 RTT 估计(整趟 = 命令下行 + 首帧上行, IIR 0.25);
      3. 连续 `GAP_TIMEOUT_S` 无帧 ⇒ 判链路死亡, 发一次 `Event.ERROR`
         (kind="link_lost") 提示**物理断电复位** —— 真机实测断连不会自愈,
         "9 分钟自行恢复"假说已于 2026-10-15 证伪。

    线程契约: `tick()` 由 `MouseClient` 调度线程 50Hz 调用(**非阻塞**一步);
    `start()/stop()/suspend()/resume()` 由调用者线程调用。内部只用标量,
    读侧只经 `TelemetryAPI` 的原子访问器, 与 `PullRequester` 一致。

    参数约束: `n`∈[1,16](固件 `RATE PULL` 上限), `t_ms`≥1; `T=n·t` **建议取
    75ms 整数倍** —— 下行命令通路按 75ms 量化, 非整数倍会看到批起点被推后
    (实测 paceB T=150ms 仍有 44 次 293ms 边界; T=300ms 的 E 组边界 282~290ms)。
    """

    #: 断连判定: 连续无帧上限(s)。E 组实测正常最大帧间隔 236ms; 3s 无帧 =
    #: 模块停转发(真机 8 组样本全是这个死法, 且必须物理断电才能恢复)。
    GAP_TIMEOUT_S = 3.0
    #: RTT 估计的 IIR 增益(新样本权重)。0.25 = 4 批收敛, 兼顾响应与平滑。
    RTT_GAIN = 0.25
    #: RTT 初值(s): 命令下行 + 首帧上行。链路实测 57~137ms, 取中偏保守。
    RTT_WARMUP_S = 0.10
    #: `start()`/`resume()` 时 `RATE PULL` 命令的重试次数(连上后首条常丢)。
    CMD_TRIES = 3
    #: `suspend()` 时等线上静默的上限(s): 发 `RATE OFF` 后固件立即丢弃本批
    #: 剩余帧, 等一个 RTT 让最后的帧到达即可, 上限兜底"帧真丢了"。
    QUIESCE_MAX_S = 0.30

    def __init__(self, mc: "MouseClient", n: int = 3, t_ms: int = 100) -> None:
        self._mc = mc
        self.running = False
        self.n = 1
        self.t_ms = 100
        self.T_s = 0.3
        self.fps = 10.0
        self.configure(n, t_ms)
        self._t0 = 0.0            # 启动时刻(墙钟, 与 last_frame_age 同基准)
        self._due_mono = 0.0      # 下一批首帧应到时刻(单调钟)
        self._rtt = self.RTT_WARMUP_S
        self._pending = None      # (send_wall, frames_before) 待反馈批次
        self._frames0 = 0         # 启动时的累计帧数(算增量)
        self.batches = 0
        self.lost_batches = 0     # 整批没到的次数(命令丢失/被抢)
        self.dead = False         # 已判链路死亡(只发一次事件)
        self._suspends = 0
        self._was_running = False

    @property
    def paused(self) -> bool:
        """是否处于 `suspend()` 暂停中(嵌套计数 > 0)。供门面路由用。"""
        return self._suspends > 0

    # ------------------------------------------------------------ 参数

    def configure(self, n: int, t_ms: int) -> Dict[str, Any]:
        """设置每批帧数 n 与帧内节拍 t_ms(运行中调用会随下一条命令生效)。"""
        n, t_ms = int(n), int(t_ms)
        if not 1 <= n <= 16:
            raise ValueError(f"n 须在 1..16(固件 RATE PULL 上限), 收到 {n}")
        if t_ms < 1:
            raise ValueError(f"t_ms 须 ≥1, 收到 {t_ms}")
        self.n, self.t_ms = n, t_ms
        self.T_s = n * t_ms / 1000.0
        self.fps = n / self.T_s
        return {"n": n, "t_ms": t_ms, "T_ms": n * t_ms,
                "fps": round(self.fps, 2)}

    # ------------------------------------------------------------ 命令

    def _rate_word(self) -> str:
        return "RATE PULL %d %d" % (self.n, self.t_ms)

    def _send_rate(self) -> Result:
        return self._mc.cmd.send(self._rate_word(), wait=True,
                                 timeout=2.0, no_retry=True)

    # ------------------------------------------------------------ 启停

    def start(self) -> Result:
        """进入批式拉取。非阻塞 —— 发一条 `RATE PULL` 就返回。"""
        if self.running:
            return Result(True, "OK", {"n": self.n, "t_ms": self.t_ms})
        last = "?"
        for _ in range(self.CMD_TRIES):
            r = self._send_rate()
            last = str(r.status)
            if r.ok:
                break
            time.sleep(0.15)
        else:
            return Result(False, "RATE_PULL_FAILED",
                          {"word": self._rate_word(), "status": last})
        self.running = True
        self.dead = False
        self.batches = 0
        self.lost_batches = 0
        self._rtt = self.RTT_WARMUP_S
        self._t0 = time.time()
        self._frames0 = self._mc.telemetry.samples()
        # 网格: 首批提前 RTT 发, 首帧落在 now+0.05(给固件反应时间)
        self._due_mono = time.monotonic() + 0.05
        self._pending = None
        return Result(True, "OK", {"n": self.n, "t_ms": self.t_ms,
                                   "T_ms": self.n * self.t_ms})

    def stop(self) -> Result:
        """退出批式拉取: 发 `RATE OFF` 让固件也停(立刻停发是关键)。"""
        was = self.running
        self.running = False
        self._pending = None
        if was:
            try:
                self._mc.cmd.send("RATE OFF", wait=True, timeout=2.0,
                                  no_retry=True)
            except Exception:
                pass
        return Result(True, "OK" if was else "ALREADY_OFF",
                      {"frames": self.frames()})

    # ------------------------------------------------------------ 让路

    def suspend(self) -> None:
        """给命令通道让路: 停发 `RATE PULL` 并发 `RATE OFF`, 等线上静默。

        契约与 `PullRequester.suspend()` 相同( `send_cmd` 的 urgent 命令与
        `send_raw` 都会调它): 固件每 1ms 只消费一行命令, 本类 3.3Hz 的周期
        命令虽稀, 仍可能撞窗口 —— 让路期间彻底不发, 恢复时重新进入 PULL 模式。
        幂等/可嵌套: 只有最外层真正暂停/恢复。
        """
        self._suspends += 1
        if self._suspends > 1 or not self.running:
            return
        self._was_running = True
        self.running = False
        self._pending = None
        try:
            self._mc.cmd.send("RATE OFF", wait=True, timeout=2.0, no_retry=True)
        except Exception:
            pass
        deadline = time.monotonic() + self.QUIESCE_MAX_S
        while time.monotonic() < deadline:
            age = self._mc.telemetry.last_frame_age()
            if age is not None and age < 0.02:
                break
            time.sleep(0.005)

    def resume(self) -> None:
        """恢复被 `suspend()` 暂停的批式拉取(仅当它原本在跑且链路未死)。"""
        if self._suspends > 0:
            self._suspends -= 1
        if self._suspends > 0 or not self._was_running:
            return
        self._was_running = False
        if self.dead or self.running:
            return
        for _ in range(self.CMD_TRIES):
            r = self._send_rate()
            if r.ok:
                self.running = True
                self._due_mono = time.monotonic() + 0.05
                self._pending = None
                return
            time.sleep(0.15)

    # ------------------------------------------------------------ 调度

    def tick(self) -> None:
        """调度线程 50Hz 调用。**非阻塞**: 没到点就返回。"""
        if not self.running:
            return
        now_mono = time.monotonic()
        now_wall = time.time()

        # 1) 断连检测(墙钟, 与 last_frame_age 同基准)。只判一次并发事件 ——
        #    真机实测: 断连后必须物理断电复位, 上位机重连/软复位都救不回来。
        age = self._mc.telemetry.last_frame_age()
        if age is not None and age > self.GAP_TIMEOUT_S:
            if not self.dead:
                self.dead = True
                self.running = False
                self._pending = None
                self._mc.bus.emit(Event.ERROR, {
                    "kind": "link_lost",
                    "msg": "连续 %.1fs 无遥测帧: 链路已断(实测不自愈)。"
                           "请给电脑鼠/蓝牙模块断电 3~5s 再重连, 然后"
                           " mc.stream.pace() 恢复。" % age})
            return

        # 2) 上一批反馈: 首帧到了就算 RTT(整趟); 超 T+1s 没到算整批丢失。
        if self._pending is not None:
            send_wall, idx0 = self._pending
            if self._mc.telemetry.samples() > idx0:
                rtt = now_wall - send_wall
                if 0.005 < rtt < 2.0:
                    self._rtt = (1.0 - self.RTT_GAIN) * self._rtt + \
                                self.RTT_GAIN * rtt
                self._pending = None
            elif now_wall - send_wall > self.T_s + 1.0:
                self.lost_batches += 1
                self._pending = None

        # 3) 到点发命令(提前 RTT, 让批首帧落在网格上)。网格**恒定推进**,
        #    不锁实测首帧 —— 锁实测会把 RTT 抖动写进网格累积成速率误差
        #    (link_pull_pace.py 实测: 锁实测被压到 10.4Hz/目标 12.05)。
        if now_mono >= self._due_mono - self._rtt:
            idx = self._mc.telemetry.samples()
            self._mc.cmd.send(self._rate_word(), wait=False, no_retry=True)
            self.batches += 1
            self._pending = (now_wall, idx)
            self._due_mono += self.T_s

    # ------------------------------------------------------------ 视图

    def frames(self) -> int:
        base = self._mc.telemetry.samples() - self._frames0
        return max(0, base)

    def snapshot(self) -> Dict[str, Any]:
        frames = self.frames()
        el = time.time() - self._t0 if self._t0 else 0.0
        return {
            "mode": "pace",
            "running": self.running,
            "n": self.n,
            "t_ms": self.t_ms,
            "T_ms": self.n * self.t_ms,
            "fps_target": round(self.fps, 2),
            "fps_obs": round(frames / el, 2) if el > 1.0 else 0.0,
            "batches": self.batches,
            "frames": frames,
            "lost_batches": self.lost_batches,
            "rtt_ms": round(self._rtt * 1000.0, 1),
            "dead": self.dead,
            "suspends": self._suspends,
        }


class StreamAPI:
    """遥测拉取开关(门面 `mc.stream`)。

    两种互斥的拉取模式(v2.9):
      · `on()` / `off()` —— **停等式** `PullRequester`(单条 `TELEM` 请求-响应,
        ~4Hz, 久经考验的默认; 时间锚随每次应答刷新);
      · `pace()` / `pace_off()` —— **批式** `PacePull`(`RATE PULL n t`,
        稀疏命令+每批多帧, 默认 10Hz; 2026-10-15 真机压测的 2.5 倍传输量档)。
    `suspend()/resume()` 自动路由到当前活跃的那个, 急停让路契约不变。

    两种模式都沿用 v1.5.5 的"显式开关"语义: 连上之后要显式开流才开始收遥测
    (`mc.stream.on()` 停等式 / `mc.stream.pace()` 批式), 关流(`off()` /
    `pace_off()`) 即彻底停发 —— 区别只在于开流不再是"命令固件持续发",
    而是"本机开始循环请求"(停等式)或"本机按基频命令、固件批量发"(批式)。
    """

    def __init__(self, mc: "MouseClient") -> None:
        self._mc = mc
        self._req = PullRequester(mc)
        # v2.9: 批式拉取器(默认 10Hz 档; 与 _req 互斥, 见 PacePull docstring)
        self._pace = PacePull(mc)
        # suspend() 期间记住被暂停的是哪个模式(两者互斥, 恢复时路由回去)
        self._suspended_kind: Optional[str] = None

    # ------------------------------------------------------------ 开关

    def on(self, timeout: float = 3.0) -> Result:
        """开始**停等式**拉取遥测(首条应答即建立时间锚)。非阻塞。

        若批式正在跑会先 `pace_off()` 停掉(两者互斥, 见类 docstring)。
        """
        if self._pace.running:
            self._pace.stop()
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

    # ------------------------------------------------------------ 批式(v2.9)

    def pace(self, n: Optional[int] = None, t_ms: Optional[int] = None,
             timeout: float = 3.0) -> Result:
        """切换到**批式**拉取(`RATE PULL n t`, 默认 n=3/t=100ms = 10Hz)。

        若停等式正在跑会先 `off()` 停掉(两者互斥)。`n`/`t_ms` 省略则用当前值
        (可用于"断连复位后原样恢复")。参数约束与 75ms 量化建议见 `PacePull`。

        真机依据(2026-10-15, COM7): 10Hz 档两组 300s/240.9s、交付率
        99.9%/98.6%, 是停等式 ~4Hz 的 2.5 倍; 顶格 13.3Hz 与 26Hz 级都会
        在 100~300s 内断连(模块缓冲灌满, 须物理断电)。
        """
        if self._req.running:
            self._req.stop()
        if n is not None or t_ms is not None:
            self._pace.configure(n if n is not None else self._pace.n,
                                 t_ms if t_ms is not None else self._pace.t_ms)
        return self._pace.start()

    def pace_off(self, timeout: float = 3.0) -> Result:
        """退出批式拉取(固件侧发 `RATE OFF` 停发)。"""
        return self._pace.stop()

    @property
    def is_on(self) -> bool:
        """拉取器是否在跑(明确的内部状态, 不再靠"最近有没有帧"猜)。"""
        return self._req.running or self._pace.running

    @property
    def mode(self) -> str:
        """当前拉取模式: `"off"` / `"stopwait"` / `"pace"`。"""
        if self._pace.running:
            return "pace"
        if self._req.running:
            return "stopwait"
        return "off"

    # ------------------------------------------------------------ 让路

    def suspend(self) -> None:
        """**给命令通道让路**: 暂停拉取并等线上静默(由 `resume()` 恢复)。

        供 `MouseClient.send_cmd()` / `send_raw()` 在发命令前后调用。
        直接调用者也可用: 例如"运动期间不想让遥测抢占链路"的实验。
        必须与 `resume()` 成对 —— 内部有嵌套计数保护。
        自动路由到当前活跃的模式(pace / stopwait)。

        ⚠️ 路由按"谁被暂停过"而不是"谁在跑": 外层已暂停时运行中的
        `send_cmd`(urgent)还会再 suspend/resume 一次, 若按 running 路由会把这层
        内层让路当成"无可停"而**清掉外层记忆**, 导致外层 resume 变空操作
        (2026-10-15 pull_smoke 实测这个 bug: 3 项失败)。按 `_suspended_kind`
        记忆路由则内层让路落进同一个拉取器的嵌套计数, 由它自己防提前恢复。
        """
        if self._pace.running:
            self._suspended_kind = "pace"
        elif self._req.running:
            self._suspended_kind = "stopwait"
        elif self._suspended_kind is None:
            return          # 什么都没在跑、也没暂停过: 无可让路
        # 已在暂停中(内层让路): 保持 kind 不变, 落进同一拉取器的嵌套计数
        if self._suspended_kind == "pace":
            self._pace.suspend()
        else:
            self._req.suspend()

    def resume(self) -> None:
        """恢复被 `suspend()` 暂停的拉取(仅当它原本在跑时才恢复)。"""
        kind = self._suspended_kind
        if kind is None:
            return
        r = self._pace if kind == "pace" else self._req
        r.resume()
        if not r.paused:     # 嵌套计数归零 = 暂停真正结束, 清掉路由记忆
            self._suspended_kind = None

    # ------------------------------------------------------------ 调度

    def tick(self) -> None:
        """调度线程 50Hz 调用: 路由到当前活跃的拉取器(两者互斥)。"""
        if self._pace.running:
            self._pace.tick()
        else:
            self._req.tick()

    # ------------------------------------------------------------ 视图/调参

    def stats(self) -> Dict[str, Any]:
        """拉取器状态: 在途数/收发计数/实测帧率 + 门面层模式。

        注意: `mode` 字段由拉取器自己给(停等式 = `"stop-and-wait"`, 批式 =
        `"pace"`), 这是 v2.1 起的既有契约(测试断言它); 门面层的三态
        (off/stopwait/pace)在 `stream_mode` 字段, 与 `mc.stream.mode` 一致。
        """
        if self._pace.running or self._pace.dead:
            out = dict(self._pace.snapshot())
        else:
            out = dict(self._req.snapshot())
        out["stream_mode"] = self.mode
        return out

    def tune(self, request_timeout: Optional[float] = None,
             n: Optional[int] = None, t_ms: Optional[int] = None) -> Dict[str, Any]:
        """运行期调参(立即生效)。

        `request_timeout` 只影响停等式; `n`/`t_ms` 只影响批式(随下一条
        `RATE PULL` 生效)。两种模式的说明见 `PacePull` / `PullRequester`。
        """
        r = self._req
        if request_timeout is not None:
            r.REQUEST_TIMEOUT_S = max(0.2, float(request_timeout))
        pace_cfg = self._pace.configure(n if n is not None else self._pace.n,
                                        t_ms if t_ms is not None else self._pace.t_ms)
        return {"mode": self.mode, "stopwait_request_timeout": r.REQUEST_TIMEOUT_S,
                "pace": pace_cfg}

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
