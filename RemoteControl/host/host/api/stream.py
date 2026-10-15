"""遥测拉取控制: 统一拉取器 `TelemetryPuller` —— 一条命令取回 n 帧, `n=1` 退化为停等(`TELEM` 回复门控), `n≥2` 为批式(`RATE PULL n t` 网格调度)。

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

v2.9(2026-10-15) 在停等之外加了**批式拉取**(`RATE PULL n t`):
  · 形态 = **稀疏基频命令 + 每批多帧**: PC 每 T=n·t_ms 才发一条 `RATE PULL n t`,
    固件在这段时间里按 t_ms 间隔连发 n 帧。命令频率只有 1/T(默认 3.3Hz),
    传输量却等于 n/T(默认 10Hz) —— **命令密度与帧率解耦**;
  · 为什么不全甩给 `RATE FIX`: 2026-10-15 八组真机样本显示链路有 **~150ms 转发量子**
    (MCU 侧严格 t 一拍, PC 侧却两帧 <1ms 成对到达), 真实容量 ≈ **2 帧/150ms = 13.3Hz**;
    26Hz 级三组 15.5/95.2/113.3s 全断连, ≤13.3Hz 四样本均值 245.9s。详见
    `docs/链路MTBF压测方法.md` §16。默认档 `PULL 3 100`(10Hz, 容量 75%)留有 20% 余量。

v2.10(2026-10-15) **统一**: 停等式与批式本是同一条链路的两个剖面 ——
"一条拉取命令的语义 = 取回 n 帧": n=1 用 `TELEM`(回复门控, 即停等), n≥2 用
`RATE PULL n t`(网格调度, 即批式)。两套类合并为唯一的 `TelemetryPuller`,
统一不变量只有一条: **任何时刻最多一条命令在等它的首帧**(n=1 时首帧≡回复≡停等)。
顺带补齐两个合并后才看得见的缺口: 批式模式从不刷时间锚(⇒ 批中点注入 `TELEM`,
每 30s 一次)与停等式没有断连判定(⇒ 统一检测器, telem 7s / pull 3s)。
设计与真机依据见 `docs/遥测拉取器统一设计方案.md`。

时间锚: 每次 `TELEM` 的应答都带完整 u32 时基, 所以停等式**锚随每一次拉取刷新** ——
  比 v2.0"开流时给一次"更稳, 失锁也能立刻自愈(见 `api/timesync.py`)。
  批式的 `RATE PULL` 应答是裸 `ACK OK`(不带时基, `link.c` 的 `link_Ack`),
  因此批式靠**批中点注入 `TELEM`** 周期性刷锚(见 `_maintain_anchor`)。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .errors import Result
from .events import Event

if TYPE_CHECKING:  # pragma: no cover
    from .client import MouseClient

__all__ = ["TelemetryPuller", "StreamAPI"]


class TelemetryPuller:
    """**统一遥测拉取器**(v2.10): 剖面 `(n, t_ms)`, 语义 = "一条命令取回 n 帧"。

    两个剖面(由 `start()` 时的 n 决定, 记在 `kind` 里):

    | kind | n | 命令词 | 调度 | 帧率 |
    |---|---|---|---|---|
    | `telem` | 1 | `TELEM` | 回复门控(在途清空才发下一条) | 1/RTT(真机 4.4~6.5Hz) |
    | `pull` | ≥2 | `RATE PULL n t` | 网格: 每 `T=n·t` 一条, 提前 RTT 估计发 | n/T(默认 3/100ms=10Hz) |

    `n=1` 时**保留 `TELEM` 而不用 `RATE PULL 1 t`**: 只有 `TELEM` 的应答带
    `OK <upMs>` 完整时基(`link.c` 里 RATE 只回裸 `OK`) —— 停等"失锁立刻自愈"
    全靠逐条刷锚。于是"后者是前者的拓展, n=1 退化为前者"在调度器层面精确成立。

    统一不变量(真机实测的根: 固件每 1ms tick 只消费一行命令, 背靠背 N 条回收率
    N=2→50%, 见模块 docstring):

    > **任何时刻, 最多一条命令在"等它的首帧"。**

    自节流把这条不变量做实: 下一条命令的发车时刻 =
    `max(网格点 − RTT估计, 上一条命令的 ACK 到达时刻)`。正常域(RTT<T, 已验证的 10Hz 档)
    与 v2.9 批式逐拍一致; 退化域(RTT>T, 模块热衰/链路变慢)自动降到
    `1/max(T, RTT)` 的命令节奏, 用帧率换完整批, 避免新 PULL 抢占旧批剩余帧。
    ⚠️ 自节流是唯一改动"批式正常域之外"行为的点, 需真机复核。

    ⚠️ 为什么用 **ACK 到达**而不是"首帧到达"来判定"上一条已启动"(v2.10 实测教训):
      ACK 按 seq 唯一归属到命令, 而**帧没有归属** —— 退化域里"上一条批的剩余帧"
      会在下一条命令发出之后才到, 把新命令的 pending 当成自己的首帧清掉:
      模拟器注入 500ms 延迟实测, 首帧法把 RTT 估成 53ms(真值 500ms), 节流从未生效。
      ACK 与批首帧本来就在同一 burst 里到达(固件先回 ACK 再发帧), 取 ACK 信号
      更干净。丢 ACK 的代价是最多等 `T+1s` 判整批丢失(帧仍会到, 只是计数+停一拍)。

    线程契约: `tick()` 由 `MouseClient` 的调度线程周期调用(50 Hz, **非阻塞**一步);
    `start()/stop()/suspend()/resume()/refresh_anchor()` 由调用者线程调用。
    内部只用简单的标量, 不做加锁 —— 与 `CommandClient` 一致(它自己加锁保护 `_pending`)。

    参数约束: `n`∈[1,16](固件 `RATE PULL` 上限), `t_ms`≥1; `T=n·t` **建议取
    75ms 整数倍** —— 下行命令通路按 75ms 量化, 非整数倍会看到批起点被推后
    (实测 paceB T=150ms 仍有 44 次 293ms 边界; T=300ms 的 E 组边界 282~290ms)。
    **`n=1` 时 `t_ms` 被忽略**(停等的周期就是 RTT, 不是 t) —— 下游 Control 的
    切换端点允许 `n=1`, 那里会字面退化为停等, 不要以为 `n=1,t=100` 是 10Hz。
    """

    #: 单条 `TELEM` 请求的等待上限。实测 RTT ≈150 ms, 故给 3 s 的宽余量。
    REQUEST_TIMEOUT_S = 3.0
    #: `stop()` 时**等在途 `TELEM` 收尾**的上限(秒)。取 ~2× 实测 RTT(≈300 ms)。
    #: 见 `stop()` 里"为什么要等"的说明。**契约测试 S8f 钉死 ≥0.3s**。
    STOP_QUIESCE_MAX_S = 0.60
    #: 让路时**等在途请求收尾**的上限(秒)。在途的那条 `TELEM` 一旦收尾, 线上就没人
    #: 抢 tick 了; 所以等到它 done 即可, 用上限兜住"应答丢了"的情况。
    QUIESCE_MAX_S = 0.30
    #: **应答没等到**时才用的保守静默(秒)。为什么只在超时时需要: 若在途的 `TELEM`
    #: 正常收到了应答, 就**证明固件已经把那一行消费完**(它先消费、才回帧),
    #: 接收端此刻必然空闲, 于是不需要再额外等; 反之若应答丢了, 就无法断定接收端
    #: 是否还咬着半行数据, 这时留一小段完全静默来兜底。
    QUIESCE_TAIL_S = 0.12
    #: 断连判定: 批式(pull)连续无帧上限(s)。E 组实测正常最大帧间隔 236ms;
    #: 3s 无帧 = 模块停转发(真机 8 组样本全是这个死法, 且必须物理断电才能恢复)。
    GAP_TIMEOUT_S = 3.0
    #: 断连判定: 停等式(telem)连续无帧上限(s) = 2×REQUEST_TIMEOUT_S + 1.0。
    #: 必须同时容得下两种正常长空档: ① 单条请求被提前放弃的最坏帧间隙
    #: ≈ 超时(3s)+RTT; ② **`MCU RESET` 后首帧遥测 ~3.2s**(收车/救援路径,
    #: Control 在用) —— 3.2s 再留一倍以上余量。v2.10 新增(停等式原来只能数 lost)。
    TELEM_GAP_TIMEOUT_S = 7.0
    #: RTT 估计的 IIR 增益(新样本权重)。0.25 = 4 批收敛, 兼顾响应与平滑。
    #: 两种模式同口径: telem 用"回复时刻−发送时刻", pull 用"首帧时刻−命令发送时刻"。
    RTT_GAIN = 0.25
    #: RTT 初值(s): 命令下行 + 首帧上行。链路实测 57~137ms, 取中偏保守。
    RTT_WARMUP_S = 0.10
    #: `start()`/`resume()` 时 `RATE PULL` 命令的重试次数(连上后首条常丢)。
    CMD_TRIES = 3
    #: 批式模式刷时间锚的周期(s)。`ANCHOR_MAX_AGE_S`=120s 的 1/4, 保守;
    #: 注入点在**批中点**(距上一条 PULL 与下一条 PULL 各 T/2, 绝不撞 1ms 收行窗口)。
    ANCHOR_REFRESH_S = 30.0
    #: 锚注入后等回复的上限(s): 超时兜底允许下一次注入(防"注入丢了"永久挡住刷锚)。
    ANCHOR_INJECT_TIMEOUT_S = 2.0

    def __init__(self, mc: "MouseClient",
                 n: int = 1, t_ms: int = 100,
                 request_timeout: Optional[float] = None) -> None:
        self._mc = mc
        if request_timeout is not None:
            self.REQUEST_TIMEOUT_S = float(request_timeout)

        # ---- 剖面(统一轴就是 n) ----
        self.n = 1
        self.t_ms = 100
        self.T_s = 0.3
        self.fps = 10.0
        self.configure(n, t_ms)
        #: 当前发射策略: `"telem"`(n=1 停等) / `"pull"`(n≥2 批式)。
        #: **由 start() 时的 n 决定**, 是"当前用哪套策略"的唯一真值 ——
        #: 不由 running 推导(暂停中也要知道原本是哪种)。
        self.kind = "telem" if self.n <= 1 else "pull"

        self.running = False
        self.dead = False
        self.hz = 0.0
        self.best_hz = 0.0

        # ---- telem 剖面状态 ----
        self._out: List[Any] = []          # 在途请求(恒 ≤ 1 条)
        self._sent = 0
        self._done = 0
        self._lost = 0
        self._last_tick = 0.0

        # ---- pull 剖面状态 ----
        self._t0 = 0.0            # 启动时刻(墙钟, 与 last_frame_age 同基准)
        self._due_mono = 0.0      # 下一批首帧应到时刻(单调钟)
        self._rtt = self.RTT_WARMUP_S
        self._pending = None      # (Pending, send_wall) 待反馈的批命令
        self._frames0 = 0         # 启动时的累计帧数(算增量)
        self.batches = 0
        self.lost_batches = 0     # 整批没到的次数(命令丢失/被抢)

        # ---- 帧到达统计(两种模式同一口径: 2s 窗口实算) ----
        self._frame_ts: List[float] = []
        self._samples_seen = 0

        # ---- 时间锚维护(批式专用; 停等式每条回复都刷, 不需要) ----
        self._anchor_refreshes = 0
        self._anchor_due = 0.0        # 下次允许注入的单调时刻
        self._anchor_mid = 0.0        # 下一个批中点的单调时刻
        self._anchor_outstanding = False
        self._anchor_sent_mono = 0.0

        #: 让路(suspend/resume)的嵌套计数与"原本是否在跑"的记忆 —— 见 suspend()。
        self._suspends = 0
        self._was_running = False

    @property
    def paused(self) -> bool:
        """是否处于 `suspend()` 暂停中(嵌套计数 > 0)。供门面查询用。"""
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

    def _rate_word(self) -> str:
        return "RATE PULL %d %d" % (self.n, self.t_ms)

    def _send_rate(self) -> Result:
        return self._mc.cmd.send(self._rate_word(), wait=True,
                                 timeout=2.0, no_retry=True)

    # ------------------------------------------------------------ 启停

    def start(self) -> Result:
        """开始拉取。**非阻塞** —— 发一条命令就返回, 锚在应答到达时自动入账。

        `kind` 按当前 n 决定(n==1 → 停等 `TELEM`; n≥2 → 批式 `RATE PULL`)。
        计数器每次启动归零( stats 描述"这一段运行")。
        """
        if self.running:
            return Result(True, "OK", self._start_info())
        if self._suspends > 0:
            # 让路中不抢跑: 等最外层 resume() 再启(否则暂停期会偷偷发命令)
            return Result(True, "OK", {"note": "suspended"})
        self.kind = "telem" if self.n <= 1 else "pull"
        self.dead = False
        self._sent = self._done = self._lost = 0
        self._frame_ts = []
        self._t0 = time.time()
        self._samples_seen = self._mc.telemetry.samples()
        self.running = True
        if self.kind == "telem":
            self._out = []
            self._refill()
            return Result(True, "OK", {"inflight": len(self._out)})
        # ---- 批式: 先把 RATE PULL 打到设备里(3 次重试), 再进入网格调度 ----
        last = "?"
        for _ in range(self.CMD_TRIES):
            r = self._send_rate()
            last = str(r.status)
            if r.ok:
                break
            time.sleep(0.15)
        else:
            self.running = False
            return Result(False, "RATE_PULL_FAILED",
                          {"word": self._rate_word(), "status": last})
        self.batches = 0
        self.lost_batches = 0
        self._rtt = self.RTT_WARMUP_S
        self._frames0 = self._mc.telemetry.samples()
        # 网格: 首批提前 RTT 发, 首帧落在 now+0.05(给固件反应时间)
        self._due_mono = time.monotonic() + 0.05
        self._drop_pending("RESTART")
        # 锚维护: 首拍即评估( freshly connected 时无锚 ⇒ 立即注入一条 TELEM)
        self._anchor_outstanding = False
        self._anchor_due = time.monotonic()
        self._anchor_mid = self._due_mono + self.T_s / 2.0
        return Result(True, "OK", {"n": self.n, "t_ms": self.t_ms,
                                   "T_ms": self.n * self.t_ms})

    def _start_info(self) -> Dict[str, Any]:
        if self.kind == "telem":
            return {"inflight": len(self._out)}
        return {"n": self.n, "t_ms": self.t_ms, "T_ms": self.n * self.t_ms}

    def stop(self) -> Result:
        """停止拉取。**立刻停发** —— 这是让蓝牙模块缓冲排空的唯一手段。

        拆teardown 按 `kind` 分派:
          · telem: **先等最后一条在途 `TELEM` 收尾**, 再丢弃它;
          · pull : 发 `RATE OFF` 让固件也停。

        ⚠️ telem 为什么要"等"而不是直接丢(2026-10-07 真机实测, COM7/蓝牙透传):
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
        if self.kind == "telem":
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
        # ---- 批式: 固件侧也要停(它按 PULL 的节拍自时钟发帧) ----
        self._drop_pending("STOPPED")
        self._anchor_outstanding = False
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
        """**给命令通道让路**: 暂停拉取并等线上静默, 之后由调用方 `resume()`。

        ⚠️ 为什么必须这样做(真机实测 2026-10-05) —— 这是"急停/停止不立即生效"的根因:
          固件 `link.c` **每 1ms tick 只消费一行命令**(`if (USART_RX_STA & 0x80) {...}`;
          bit7 未清时 `usart.c` 把后续到达的字节**直接丢弃**)。而拉取器是**持续**发
          命令的, 于是同时下发的 `STOP`/`ESTOP` 只要与某条拉取命令落进同一个
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
        if self._suspends > 1 or not self.running:
            return
        self._was_running = True
        self.running = False
        if self.kind == "telem":
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
            return
        # ---- 批式: 发 RATE OFF + 等最后的尾帧落地 ----
        self._drop_pending("SUSPEND")
        self._anchor_outstanding = False
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
        """恢复 `suspend()` 暂停的拉取(仅当它原本在跑时才恢复)。

        telem → 直接补发一条 `TELEM`; pull → 重发 `RATE PULL`(3 次重试,
        连上后首条常丢)。链路已判死(`dead`)则不恢复 —— 重新 `start()` 才行。
        """
        if self._suspends > 0:
            self._suspends -= 1
        if self._suspends > 0 or not self._was_running:
            return
        self._was_running = False
        if self.dead or self.running:
            return
        if self.kind == "telem":
            self.running = True
            self._refill()
            return
        for _ in range(self.CMD_TRIES):
            r = self._send_rate()
            if r.ok:
                self.running = True
                self._due_mono = time.monotonic() + 0.05
                self._drop_pending("RESUME")
                return
            time.sleep(0.15)

    # ------------------------------------------------------------ 每拍

    def tick(self) -> None:
        """由调度线程周期调用(50Hz, 非阻塞): 断连判定 -> 收割 -> 调度 -> 锚 -> 统计。"""
        if not self.running:
            return
        now_mono = time.monotonic()
        now_wall = time.time()
        self._last_tick = now_wall

        # 1) 断连判定(阈值分模式, 见类常量)。只判一次并发事件 ——
        #    真机实测: 断连后必须物理断电复位, 上位机重连/软复位都救不回来。
        if self._check_link(now_wall):
            return

        # 2) 收割在途(上一拍发出的命令)
        if self.kind == "telem":
            self._reap_telem(now_wall)
        else:
            self._reap_pull(now_wall)

        # 3) 调度下一条命令(自节流 + 网格恒进)
        if self.kind == "telem":
            self._refill()
        else:
            self._schedule_pull(now_mono, now_wall)

        # 4) 时间锚维护(仅批式: 它的命令应答不带时基)
        self._maintain_anchor(now_mono)

        # 5) 帧率统计(2s 窗口帧到达实算, 两种模式同口径)
        self._note_frames(now_wall)
        self._update_hz(now_wall)

    # ------------------------------------------------------------ telem 一拍

    def _reap_telem(self, now: float) -> None:
        """收割在途的 `TELEM`: 完成计 done(顺带 IIR 刷新 RTT), 超时 abandon 计 lost。"""
        keep: List[Any] = []
        for p in self._out:
            if p.done:
                if p.ok:
                    self._done += 1
                    rtt = now - p.sent_at
                    if 0.005 < rtt < 2.0:
                        self._rtt = (1.0 - self.RTT_GAIN) * self._rtt + \
                                    self.RTT_GAIN * rtt
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

    def _refill(self) -> None:
        """**停等**: 只有在途为空时才发下一条 —— 绝不一次发多条。

        ⚠️ 这是本类最要紧的不变量。固件每 1 ms tick 只消费一行命令
        (见模块 docstring 里的实测回收率), 一次发两条就丢第二条。
        """
        if not self.running or self._out:
            return
        p = self._mc.cmd.send("TELEM", no_retry=True)
        # 每条应答都带完整时基 -> 逐条刷新时间锚
        p.callbacks.append(self._on_anchor_reply)
        self._out.append(p)
        self._sent += 1

    # ------------------------------------------------------------ pull 一拍

    def _drop_pending(self, status: str = "STOPPED") -> None:
        """丢弃在途的批命令: 未收 ACK 的 abandon 兜底(否则 Pending 永远留在
        `cmd._pending` 里, 且 seq 复用时会冲突)。"""
        if self._pending is not None:
            p = self._pending[0]
            if not p.done:
                try:
                    self._mc.cmd.abandon(p, status)
                except Exception:
                    pass
            self._pending = None

    def _reap_pull(self, now_wall: float) -> None:
        """上一批命令的反馈: **ACK 到了**就算整趟 RTT; 超 T+1s 没 ACK 算整批丢失。

        为什么看 ACK 不看首帧: ACK 按 seq 唯一归属, 不受"上一条批的剩余帧"污染
        (实测 500ms 注入延迟下首帧法把 RTT 估成 53ms, 自节流被完全绕过 —— 见类
        docstring)。代价: ACK 真丢了要等 `T+1s` 才放行(帧仍到, 只计一次丢批)。
        """
        if self._pending is None:
            return
        p, send_wall = self._pending
        if p.done:
            if p.ok:
                rtt = now_wall - send_wall
                if 0.005 < rtt < 2.0:
                    self._rtt = (1.0 - self.RTT_GAIN) * self._rtt + \
                                self.RTT_GAIN * rtt
                self._done += 1
            else:
                self._lost += 1
            self._pending = None
        elif now_wall - send_wall > self.T_s + 1.0:
            self.lost_batches += 1
            self._lost += 1
            try:
                self._mc.cmd.abandon(p, "TIMEOUT")
            except Exception:
                pass
            self._pending = None

    def _schedule_pull(self, now_mono: float, now_wall: float) -> None:
        """到点发下一条 `RATE PULL`(提前 RTT, 让批首帧落在网格上)。

        **自节流**: `next_send = max(网格点 − RTT估计, 上一条命令的 ACK 到达)` ——
        上一条的 ACK 还没回就不发(正常域 RTT<T 时 ACK 必先到, 与 v2.9 逐拍一致;
        退化域 RTT>T 时自动降速, 不让新 PULL 抢占旧批剩余帧)。
        网格**恒定推进**, 不锁实测首帧 —— 锁实测会把 RTT 抖动写进网格累积成
        速率误差(`link_pull_pace.py` 实测: 锁实测被压到 10.4Hz/目标 12.05)。
        """
        if self._pending is not None:
            return                      # 上一条命令的 ACK 未回: 自节流, 让路
        if now_mono < self._due_mono - self._rtt:
            return                      # 没到网格点(提前量 = RTT 估计)
        p = self._mc.cmd.send(self._rate_word(), wait=False, no_retry=True)
        self.batches += 1
        self._sent += 1
        self._pending = (p, now_wall)
        self._due_mono += self.T_s
        # 下一个批中点 = 本次发车 + T/2(距上一条 PULL 与下一条 PULL 各 T/2)
        self._anchor_mid = now_mono + self.T_s / 2.0

    # ------------------------------------------------------------ 断连判定

    def _frame_gap(self, now_wall: float) -> float:
        """"连续多久没有新帧" —— 只认**本次运行期间**到达的帧。

        上一次运行(甚至上一次 pace)留下的旧帧不算数: 停流 5s 后再开流,
        首拍不能因为"距上一模式的最后一帧已 5s"就判死。所以取
        `min(距最后一帧, 距本次 start)`; 从未收到帧时用距 start。
        """
        age = self._mc.telemetry.last_frame_age()
        since_start = now_wall - self._t0 if self._t0 else 0.0
        if age is None:
            return since_start
        return min(age, since_start)

    def _check_link(self, now_wall: float) -> bool:
        """连续无帧超过阈值 ⇒ 判链路死亡(只发一次事件)。返回是否已死。"""
        if self.dead:
            return True
        limit = self.TELEM_GAP_TIMEOUT_S if self.kind == "telem" \
            else self.GAP_TIMEOUT_S
        if self._frame_gap(now_wall) <= limit:
            return False
        self.dead = True
        self.running = False
        self._drop_pending("LINK_LOST")
        self._anchor_outstanding = False
        for p in self._out:
            try:
                self._mc.cmd.abandon(p, "LINK_LOST")
            except Exception:
                pass
        self._out = []
        self._mc.bus.emit(Event.ERROR, {
            "kind": "link_lost",
            "msg": "连续 %.1fs 无遥测帧: 链路已断(实测不自愈)。"
                   "请给电脑鼠/蓝牙模块断电 3~5s 再重连, 然后"
                   " mc.stream.on() / mc.stream.pace() 恢复。" % limit})
        return True

    # ------------------------------------------------------------ 时间锚

    def _on_anchor_reply(self, ok: bool, status: str) -> None:
        """`TELEM` 应答回调(读取线程): 清注入标记 + 把 `OK <upMs>` 写入锚。"""
        self._anchor_outstanding = False
        try:
            self._mc._absorb_anchor(ok, status)
        except Exception:
            pass

    def _maintain_anchor(self, now_mono: float) -> None:
        """批式模式的时间锚维护(停等式不需要: 每条回复都刷)。

        `RATE PULL` 的 ACK 是裸 `OK`(不带时基) ⇒ 不注入的话锚会停在"最后一次
        `TELEM`"上; 设备一旦复位(`linkTimeMs` 归零), ms8 同余还原永久失锁且
        **没有任何恢复路径**。所以: 每 `ANCHOR_REFRESH_S` 在**批中点**注入一条
        `TELEM`(不等回复, 回调刷锚); `timesync.needs_anchor()` 为真时立即注入。
        代价: 3.3Hz 命令预算里多 0.03Hz; 固件对 `TELEM` 立即回一帧(不影响
        PULL 批计时, `linkPullTimer` 独立走时)。
        """
        if self.kind != "pull":
            return
        if self._anchor_outstanding:
            # 兜底: 注入的回复丢了(超 ANCHOR_INJECT_TIMEOUT_S)就允许下次再注
            if now_mono - self._anchor_sent_mono > self.ANCHOR_INJECT_TIMEOUT_S:
                self._anchor_outstanding = False
            return
        try:
            need = self._mc.timesync.needs_anchor()
        except Exception:
            need = False
        if not need and now_mono < self._anchor_due:
            return
        if now_mono < self._anchor_mid:
            return                      # 等批中点: 离两条 PULL 命令各 T/2
        self._inject_anchor(now_mono)
        self._anchor_due = now_mono + self.ANCHOR_REFRESH_S

    def _inject_anchor(self, now_mono: float) -> None:
        """注入一条 `TELEM` 刷锚(**不阻塞** tick: 不等回复, 回调入账)。"""
        p = self._mc.cmd.send("TELEM", wait=False, no_retry=True)
        p.callbacks.append(self._on_anchor_reply)
        self._anchor_outstanding = True
        self._anchor_sent_mono = now_mono
        self._anchor_refreshes += 1

    def refresh_anchor(self, timeout: float = 3.0) -> Result:
        """**立即**索要一条时间锚(注入 `TELEM` 并等回复)。`mc.reanchor()` 调它。

        停等式特殊处理: 每条回复都带时基, 所以在途那条的回复就是最新锚 ——
        等它即可, **绝不插第二条 `TELEM`**(在途 ≤1 不变量)。未在跑时直接发一条。
        """
        if self.kind == "telem" and self.running:
            if not self._out:
                self._refill()
            p = self._out[0] if self._out else None
            if p is not None:
                deadline = time.time() + timeout
                while not p.done and time.time() < deadline:
                    time.sleep(0.005)
                if p.done:
                    return Result(p.ok, p.status,
                                  {"cmd": "TELEM", "note": "在途请求的回复即新锚"})
                return Result(False, "TIMEOUT", {"cmd": "TELEM"})
        # pull 模式 / 未运行: 直接注入一条(回调刷锚)
        self._anchor_outstanding = False
        p = self._mc.cmd.send("TELEM", wait=False, no_retry=True)
        p.callbacks.append(self._on_anchor_reply)
        self._anchor_outstanding = True
        self._anchor_sent_mono = time.monotonic()
        self._anchor_refreshes += 1
        deadline = time.time() + timeout
        while not p.done and time.time() < deadline:
            time.sleep(0.005)
        if not p.done:
            try:
                self._mc.cmd.abandon(p, "TIMEOUT")
            except Exception:
                pass
            self._anchor_outstanding = False
            return Result(False, "TIMEOUT", {"cmd": "TELEM"})
        return Result(p.ok, p.status, {"cmd": "TELEM"})

    # ------------------------------------------------------------ 调参

    def tune(self, n: Optional[int] = None,
             t_ms: Optional[int] = None) -> Dict[str, Any]:
        """运行期设参; **n 的变化改变 kind 时自动重 arm**(立即生效)。

        同 kind 内改档(如 pull 3/100 → 2/150)不重启: 下一条命令自然带新参数
        (命令每 T 重发一遍, 幂等)。跨 kind(telem↔pull)则停旧启新。
        """
        new_n = self.n if n is None else int(n)
        new_t = self.t_ms if t_ms is None else int(t_ms)
        cfg = self.configure(new_n, new_t)
        if self.running and (new_n <= 1) != (self.kind == "telem"):
            self.stop()
            self.start()
        return cfg

    # ------------------------------------------------------------ 统计

    def _update_hz(self, now_wall: float) -> None:
        """按最近 2 秒内**到达的帧**算实测帧率(两种模式同口径)。"""
        cut = now_wall - 2.0
        ts = self._frame_ts
        while ts and ts[0] < cut:
            ts.pop(0)
        self.hz = len(ts) / 2.0
        self.best_hz = max(self.best_hz, self.hz)

    def _note_frames(self, now_wall: float) -> None:
        """把这一拍新到的帧记进 2s 窗口(一批多帧成对到达, 逐帧记)。"""
        n = self._mc.telemetry.samples()
        if n > self._samples_seen:
            for _ in range(n - self._samples_seen):
                self._frame_ts.append(now_wall)
            self._samples_seen = n

    def frames(self) -> int:
        """本次启动以来到达的帧数。"""
        base = self._mc.telemetry.samples() - self._frames0
        return max(0, base)

    def snapshot(self) -> Dict[str, Any]:
        """状态快照。字段是两种剖面的**并集**(旧读法都不破):

        `mode` = 拉取器自报(`"stop-and-wait"` / `"pace"`, v2.1 起的既有契约);
        `sent/done/lost` = **命令口径**(telem: TELEM 请求数; pull: RATE PULL 命令数);
        `frames` = **帧口径**(telem 时 = done, 1 命令 1 帧);
        `batches/lost_batches` = telem 时退化字面成立(每条 TELEM 就是一批 1 帧);
        `inflight` = 统一不变量"最多一条命令在等首帧"的可观测面;
        `fps_target` = telem 报 1/RTT 估计, pull 报 n/T;
        `anchor_refreshes` = 批式中注入 TELEM 刷锚的次数(telem 恒 0)。
        """
        telem = self.kind == "telem"
        frames = self.frames()
        el = time.time() - self._t0 if self._t0 else 0.0
        return {
            "mode": "stop-and-wait" if telem else "pace",
            "running": self.running,
            "n": self.n,
            "t_ms": self.t_ms,
            "T_ms": self.n * self.t_ms,
            "fps_target": round(1.0 / self._rtt, 2)
                          if (telem and self._rtt > 0) else round(self.fps, 2),
            "fps_obs": round(frames / el, 2) if el > 1.0 else 0.0,
            "sent": self._sent,
            "done": self._done,
            "lost": self._lost,
            "frames": frames,
            "batches": self._sent if telem else self.batches,
            "lost_batches": self._lost if telem else self.lost_batches,
            "inflight": len(self._out) if telem
                        else (1 if self._pending is not None else 0),
            "hz": round(self.hz, 2),
            "best_hz": round(self.best_hz, 2),
            "rtt_ms": round(self._rtt * 1000.0, 1),
            "dead": self.dead,
            "suspends": self._suspends,
            "anchor_refreshes": self._anchor_refreshes,
        }


class StreamAPI:
    """遥测拉取开关(门面 `mc.stream`) —— v2.10 起背后只有一个 `TelemetryPuller`。

    两个剖面(统一拉取器的 n):
      · `on()` / `off()` —— **停等式**(n=1): 单条 `TELEM` 请求-响应, ~4Hz,
        久经考验的默认; 时间锚随每次应答刷新;
      · `pace()` / `pace_off()` —— **批式**(n≥2, 默认 3/100ms = 10Hz):
        `RATE PULL n t` 稀疏命令+每批多帧, 2026-10-15 真机压测的 2.5 倍传输量档;
        `pace(1, t)` 字面退化为停等(t_ms 被忽略)。
    两种模式**互斥**(切换时先停旧再启新); `suspend()/resume()` 直接委派
    唯一的拉取器(急停让路契约不变, 内部不再有模式路由)。

    两种模式都沿用 v1.5.5 的"显式开关"语义: 连上之后要显式开流才开始收遥测
    (`mc.stream.on()` 停等式 / `mc.stream.pace()` 批式), 关流(`off()` /
    `pace_off()`) 即彻底停发 —— 区别只在于开流不再是"命令固件持续发",
    而是"本机开始循环请求"(停等式)或"本机按基频命令、固件批量发"(批式)。
    """

    def __init__(self, mc: "MouseClient") -> None:
        self._mc = mc
        # v2.10: 唯一拉取器(默认剖面 n=1 = 停等; pace() 切批式档)
        self._puller = TelemetryPuller(mc)
        # 批式档记忆: pace() 省略参数时用它("断连复位后原样恢复")
        self._pace_n, self._pace_t = 3, 100

    # ------------------------------------------------------------ 开关

    def on(self, timeout: float = 3.0) -> Result:
        """开始**停等式**拉取遥测(首条应答即建立时间锚)。非阻塞。

        若批式正在跑会先停掉(两者互斥, 见类 docstring)。
        """
        if self._puller.running:
            self._puller.stop()
        self._puller.configure(1, self._puller.t_ms)
        return self._puller.start()

    def off(self, timeout: float = 3.0) -> Result:
        """停止拉取。**立刻停发** —— 这是让蓝牙模块缓冲排空的唯一手段。

        ⚠️ 停等式会先花 ≤0.6 s **等在途的那条 `TELEM` 收尾**再返回(不额外发字节),
        否则紧接着发来的命令有 ~3/4 概率撞上固件的 1 ms 收行窗口而被丢弃 ——
        详见 `TelemetryPuller.stop()`。
        """
        return self._puller.stop()

    def toggle(self, on: bool, timeout: float = 3.0) -> Result:
        return self.on(timeout=timeout) if on else self.off(timeout=timeout)

    # ------------------------------------------------------------ 批式(v2.9)

    def pace(self, n: Optional[int] = None, t_ms: Optional[int] = None,
             timeout: float = 3.0) -> Result:
        """切换到**批式**拉取(`RATE PULL n t`, 默认 n=3/t=100ms = 10Hz)。

        若停等式正在跑会先停掉(两者互斥)。`n`/`t_ms` 省略则用当前批式档
        (可用于"断连复位后原样恢复")。参数约束与 75ms 量化建议见
        `TelemetryPuller`。**`n=1` 时字面退化为停等**(`t_ms` 被忽略)。

        真机依据(2026-10-15, COM7): 10Hz 档两组 300s/240.9s、交付率
        99.9%/98.6%, 是停等式 ~4Hz 的 2.5 倍; 顶格 13.3Hz 与 26Hz 级都会
        在 100~300s 内断连(模块缓冲灌满, 须物理断电)。
        """
        if n is not None:
            self._pace_n = int(n)
        if t_ms is not None:
            self._pace_t = int(t_ms)
        if self._puller.running:
            self._puller.stop()
        self._puller.configure(self._pace_n, self._pace_t)
        return self._puller.start()

    def pace_off(self, timeout: float = 3.0) -> Result:
        """退出批式拉取(固件侧发 `RATE OFF` 停发)。"""
        return self._puller.stop()

    def configure(self, n: int, t_ms: int) -> Dict[str, Any]:
        """纯设参(不启动): 记下批式档 `(n, t_ms)` 并校验(v2.10 新增)。"""
        cfg = self._puller.configure(int(n), int(t_ms))
        self._pace_n, self._pace_t = int(n), int(t_ms)
        return cfg

    @property
    def is_on(self) -> bool:
        """拉取器是否在跑(明确的内部状态, 不再靠"最近有没有帧"猜)。"""
        return self._puller.running

    @property
    def mode(self) -> str:
        """当前拉取模式: `"off"` / `"stopwait"` / `"pace"`。"""
        if not self._puller.running:
            return "off"
        return "stopwait" if self._puller.kind == "telem" else "pace"

    # ------------------------------------------------------------ 让路

    def suspend(self) -> None:
        """**给命令通道让路**: 暂停拉取并等线上静默(由 `resume()` 恢复)。

        供 `MouseClient.send_cmd()` / `send_raw()` 在发命令前后调用。
        直接调用者也可用: 例如"运动期间不想让遥测抢占链路"的实验。
        必须与 `resume()` 成对 —— 内部有嵌套计数保护。
        v2.10 起直接委派唯一的拉取器(不再有"暂停的是哪个模式"的路由 ——
        2026-10-15 实测: 按 running 路由会清掉外层暂停记忆, 导致外层 resume
        变空操作, pull_smoke 3 项失败; 单一拉取器后这类 bug 的根因被消除)。
        """
        self._puller.suspend()

    def resume(self) -> None:
        """恢复被 `suspend()` 暂停的拉取(仅当它原本在跑时才恢复)。"""
        self._puller.resume()

    # ------------------------------------------------------------ 调度

    def tick(self) -> None:
        """调度线程 50Hz 调用: 委派给唯一的拉取器。"""
        self._puller.tick()

    # ------------------------------------------------------------ 视图/调参

    def stats(self) -> Dict[str, Any]:
        """拉取器状态: 在途数/收发计数/实测帧率 + 门面层模式。

        注意: `mode` 字段由拉取器自己给(停等式 = `"stop-and-wait"`, 批式 =
        `"pace"`), 这是 v2.1 起的既有契约(测试断言它); 门面层的三态
        (off/stopwait/pace)在 `stream_mode` 字段, 与 `mc.stream.mode` 一致。
        """
        out = dict(self._puller.snapshot())
        out["stream_mode"] = self.mode
        return out

    def tune(self, request_timeout: Optional[float] = None,
             n: Optional[int] = None, t_ms: Optional[int] = None) -> Dict[str, Any]:
        """运行期调参(立即生效)。

        `request_timeout` 只影响停等式; `n`/`t_ms` 记进批式档并**立即生效** ——
        n 的变化若改变 kind(telem↔pull)会自动重 arm。不传 n/t 时不动档位
        (停等式在跑时不会被一声不响切走)。剖面说明见 `TelemetryPuller`。
        """
        if request_timeout is not None:
            self._puller.REQUEST_TIMEOUT_S = max(0.2, float(request_timeout))
        if n is None and t_ms is None:
            cfg = {"n": self._puller.n, "t_ms": self._puller.t_ms,
                   "T_ms": self._puller.n * self._puller.t_ms,
                   "fps": round(self._puller.fps, 2)}
            return {"mode": self.mode,
                    "stopwait_request_timeout": self._puller.REQUEST_TIMEOUT_S,
                    "pace": cfg}
        cand_n = self._pace_n if n is None else int(n)
        cand_t = self._pace_t if t_ms is None else int(t_ms)
        cfg = self._puller.tune(n=cand_n, t_ms=cand_t)   # 先校验(可能抛 ValueError)
        self._pace_n, self._pace_t = cand_n, cand_t     # 校验过了才落记忆
        return {"mode": self.mode,
                "stopwait_request_timeout": self._puller.REQUEST_TIMEOUT_S,
                "pace": cfg}

    # ------------------------------------------------------------ 时间锚

    def anchor(self):
        """当前时间锚 `(本机单调秒, 设备 ms)`; 未建立时 None。"""
        return self._mc.store.get_anchor()

    def refresh_anchor(self, timeout: float = 3.0) -> Result:
        """立即索要一条时间锚(注入 `TELEM` 等回复)。`mc.reanchor()` 调它。"""
        return self._puller.refresh_anchor(timeout=timeout)
