"""手机相机（模块 B）适配器 —— 包 `hostlink.HostLink`。

三个上游给我们的硬约束（读自 `PhoneCamera/pc/hostlink.py` 与 `server.py`）：

1. **`hostlink` 是同步阻塞实现**（`socket` + `urllib`，刻意只用标准库）。
   ⇒ 不要为了"优雅"去用 `run_in_executor` 包整个生成器；用**一个专用线程**跑
   `events()`，把事件投进线程安全队列，是最简单且不会踩坑的做法（§7.3）。

2. **`events()` 断线重连后找不回断开期间的事件**，会 yield 一条 `_reconnected`。
   ⇒ 收到它**必须调一次 `status()` 重新对齐**，否则界面会显示过期状态（判据 I14）。

3. **token 是凭据**：只存服务端，**不进前端、不进日志**。
   `/camera/pair` 的响应含 token，所以它**不写日志**、只在配对页显式请求时返回（R10）。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..bus import Bus
from ..clock import now_ms
from . import probe_module

#: 这些事件**必须立即透出**（稀疏、语义重、丢一条就少一个事实）
IMMEDIATE = {
    "session_started", "delivery_done", "aborted", "gate", "error", "warn",
    "reconnected", "_reconnected", "session_stopped", "control",
}
#: 这些事件来自 App 的**高频**上行，节流后再给界面（但**不节流地**参与计数与落盘）
THROTTLED = {"frame_verified", "upload_progress", "delivery_progress", "chunk", "ack"}
#: 节流窗口（秒）：高频类事件最多这么频繁地推给界面
THROTTLE_S = 0.2


class CameraError(RuntimeError):
    """相机侧操作失败。"""


class CameraAdapter:
    """相机侧统一入口。**前端永远看不到 token。**"""

    def __init__(self, cfg: Any, bus: Bus, store: Any) -> None:
        self.cfg = cfg
        self.bus = bus
        self.store = store
        self.base = str(cfg.get("camera.base", "http://127.0.0.1:8765")).rstrip("/")
        self._token = str(cfg.get("camera.token", ""))
        self._timeout = float(cfg.get("camera.timeout", 10.0))
        self._host = None
        self._lock = threading.RLock()
        self._import_error: Optional[str] = None
        self._caps: Optional[dict] = None
        self._last_status: Optional[dict] = None
        self._last_ok_ms: Optional[int] = None

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        #: 健康快照（**后台探测**产出）。`health()` 只读它 —— 判据 I12 要求 /health < 1 s，
        #: 而本机对 `127.0.0.1:8765` 一次"被拒绝"的连接要 ~2.0 s 才返回（系统层行为）。
        #: 所以同步探测这条路从根上就走不通，只能提前探好、读的时候零成本。
        self._probe_thread: Optional[threading.Thread] = None
        self._probe_stop = threading.Event()
        self._probe_wake = threading.Event()        # 用来打断等待（离线进出、改地址）
        self._probe: Optional[dict] = None          # {reachable, ts, dur_ms, err, status, ver}
        self._probe_guard = threading.Lock()        # 同一时刻只允许一次在途探测
        self._probe_prev: Optional[bool] = None     # 上一次的可达性（用来判"翻转"）
        self._probe_interval_ok = float(cfg.get("camera.probe_interval_s", 8.0))
        self._probe_interval_bad = float(cfg.get("camera.probe_interval_bad_s", 4.0))

        #: 主时钟（由 `Ctx` 注入）。用来把帧的**拍摄时刻**（手机 epoch ns）换算成 `t_pc_ms`。
        #: 为 None 或未标定时，timeline 如实退回用"到达时刻"并把 t_src 标成 arrival。
        self.clock = None

        #: 帧监听者（定位在线段挂在这里）
        self._frame_listeners: List[Callable[[dict, Optional[Path]], None]] = []
        self._throttle: Dict[str, float] = {}

        #: 离线（无手机）模式：把一个**已有的真实会话目录**当采集源（§8.3）
        self._offline: Optional[dict] = None
        self._offline_stop = threading.Event()
        self._offline_thread: Optional[threading.Thread] = None
        self._data_dir_cache: Optional[Path] = None
        self._data_dir_at: int = 0

        self._load()

    # ------------------------------------------------------------ 装载
    def _load(self) -> None:
        paths = self.cfg.get("paths.phonecamera_pc", "")
        mod, err = probe_module("hostlink", paths)
        if err:
            self._import_error = err
            self.bus.log("warn", f"相机模块不可用：{err.splitlines()[0]}", source='camera')
            return
        try:
            from hostlink import HostLink, HostLinkError       # noqa: F401
            self._HostLink = HostLink
            self._HostLinkError = HostLinkError
        except Exception as e:                                 # noqa: BLE001
            self._import_error = f"hostlink 可导入但取不到 HostLink：{type(e).__name__}: {e}"
            return
        self._import_error = None

    @property
    def available(self) -> bool:
        return self._import_error is None

    @property
    def import_error(self) -> Optional[str]:
        return self._import_error

    def _ensure(self) -> Any:
        if not self.available:
            raise CameraError(f"相机模块不可用。\n{self._import_error}")
        with self._lock:
            if self._host is None:
                self._host = self._HostLink(self.base, self._token, timeout=self._timeout)
            return self._host

    def set_base(self, base: str, token: Optional[str] = None) -> None:
        """改服务地址（现场 IP 会变 —— 这正是需要一个界面的原因之一）。"""
        with self._lock:
            self.base = str(base).rstrip("/")
            if token is not None:
                self._token = str(token)
            self._host = None
            self._caps = None
            self._last_status = None
            self._data_dir_cache = None
        self.kick_probe()       # 换地址了，旧快照说的是旧服务，必须立刻作废

    @property
    def reachable(self) -> bool:
        return self._last_ok_ms is not None and (now_ms() - self._last_ok_ms) < 15000

    # ------------------------------------------------------------ 读
    def capabilities(self, refresh: bool = False) -> dict:
        if not self.available:
            raise CameraError(self._import_error or "相机模块不可用")
        if self._caps is None or refresh:
            try:
                self._caps = self._ensure().capabilities()      # 无需 token
                self._last_ok_ms = now_ms()
            except Exception as e:                              # noqa: BLE001
                raise CameraError(self._explain("读 capabilities", e)) from e
        return self._caps

    def status(self, refresh: bool = True) -> dict:
        if not self.available:
            raise CameraError(self._import_error or "相机模块不可用")
        if not refresh and self._last_status is not None:
            return self._last_status
        try:
            st = self._ensure().status()
            self._last_status = st
            self._last_ok_ms = now_ms()
            return st
        except Exception as e:                                  # noqa: BLE001
            raise CameraError(self._explain("读 status", e)) from e

    def overview(self) -> dict:
        return self._call("读 overview", lambda h: h.overview())

    def sessions(self) -> list:
        return self._call("读会话表", lambda h: h.sessions())

    def ledger(self, session_id: str = "") -> dict:
        return self._call("读留存账本", lambda h: h.ledger(session_id))

    def pair(self) -> dict:
        """⚠️ 响应**含 token**。调用方负责：不写日志、只在配对页显式请求时返回。"""
        return self._call("读配对信息", lambda h: h.pair(), log=False)

    def mjpeg_url(self) -> Optional[str]:
        """带 token 的 MJPEG 地址 —— **只在服务端使用**（反向代理用）。"""
        if not self.available:
            return None
        try:
            return self._ensure().mjpeg_url()
        except Exception:                                       # noqa: BLE001
            return None

    def thumb_url(self, session_id: str = "") -> Optional[str]:
        if not self.available:
            return None
        try:
            return self._ensure().thumb_url(session_id=session_id)
        except Exception:                                       # noqa: BLE001
            return None

    # ------------------------------------------------------------ 控制
    def start(self, session_id: str = "", device_serial: str = "unknown",
              plan: Optional[dict] = None, capture_mode: str = "") -> dict:
        """开始采集。`plan` 缺省 = 不抽帧（每一帧都留）。

        `capture_mode`（上游 1.15.0）：留空 = `deliver`（每帧都留）；传 `"live"` 开
        **只看不留**的实时道（实时回传彩色、设备零留存、**跑完不留任何东西**）。
        ⚠️ 它是 `HostLink.start()` 的**顶层参数**，**不从 `plan` 里读** —— 传 `plan` 没用。
        """
        plan = plan or dict(self.cfg.get("camera.default_plan") or
                            {"mode": "interval", "interval_ms": 0, "count": 0})
        if int(plan.get("interval_ms") or 0) != 0:
            # 上游把这个默认值改过一次（旧语义是 200 ms 抽帧），所以这里必须
            # **显式提示语义变化**：every_frame 会变 false，账本的口径随之改变。
            self.bus.log("warn",
                         f"plan.interval_ms={plan.get('interval_ms')} ≠ 0："
                         f"此为抽帧，`every_frame` 将变为 false，"
                         f"账本中 frames_expected 的含义随之改变（并非每帧都留存）", source='camera')
        return self._call("start 采集", lambda h: h.start(
            session_id=session_id, device_serial=device_serial, plan=plan,
            capture_mode=capture_mode or ""))

    def stop(self, session_id: str = "") -> dict:
        return self._call("stop 采集", lambda h: h.stop(session_id=session_id))

    def _call(self, what: str, fn: Callable[[Any], Any], log: bool = True) -> Any:
        try:
            out = fn(self._ensure())
            self._last_ok_ms = now_ms()
            return out
        except Exception as e:                                  # noqa: BLE001
            msg = self._explain(what, e)
            if log:
                self.bus.log("warn", msg, source='camera')
            raise CameraError(msg) from e

    #: 认得出"压根没谈上话"的几种说法。**越宽越好**，因为反例的代价是
    #: "错误信息给不出下一步"（判据 B29 要的正是那几条 `·`）——
    #: 而"认不出来"的原因往往跟我们无关：本机快照是中文 `连不上`，
    #: 但如果机器上有个 HTTP 代理（企业网、抓包工具），urlopen 会先撞代理，
    #: 拿回来的是英文的 `HTTP 502: upstream connect failed`；网络超时又是另一种措辞。
    #: 这些全都指向同一件事：**服务没在答话**，所以下一步也是同一套。
    _UNREACHED = ("连不上", "connectionrefused", "connection refused", "refused",
                  "urlerror", "10061", "upstream connect failed", "502",
                  "timed out", "timeout", "unreachable")

    def _explain(self, what: str, e: Exception) -> str:
        raw = f"{type(e).__name__}: {e}"
        low = raw.lower()
        hint = ""
        if "403" in raw or "bad_token" in raw:
            hint = "\n  · token 不符：核对 config.yaml 的 camera.token 与手机 App 是否一致"
        elif (any(k in low for k in self._UNREACHED) or "HostLinkError" in raw):
            # 这段文字会整段显示在相机窗口的故障位里（`#dc-fault`），
            # 所以它是**可照做的清单**，不是解释。第一条是首选动作：
            # 本层能代启上游程序，省掉手动双击 start_service.cmd。
            hint = (f"\n  · 点【启动上游服务】代启 pc/server.py（默认 {self.base}）\n"
                    f"  · 或手动启动 PhoneCamera/pc/start_service.cmd\n"
                    f"  · 地址不对就到上面「地址」里改（现场 IP 会变）\n"
                    f"  · 若这台机器走 HTTP 代理，代理会让本机回环连接也失败")
        return f"{what}失败：{raw}{hint}"

    # ------------------------------------------------------------ 事件消费线程
    def start_events(self, loop: asyncio.AbstractEventLoop) -> None:
        """起一个**专用线程**跑 `hostlink.events()`。

        为什么不用 asyncio：上游是同步阻塞的（见模块 docstring #1）。
        """
        if not self.available:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._loop = loop
            self._stop.clear()
            self._thread = threading.Thread(target=self._events_loop, name="cam-events",
                                            daemon=True)
            self._thread.start()

    def stop_events(self) -> None:
        self._stop.set()

    def _events_loop(self) -> None:
        """消费 `hostlink.events()`。断线自动重连由上游负责，我们负责**重对齐**。

        日志纪律（踩过）：相机没在跑是**常态**，而这一层是 2 s 一轮的重试。
        如果每轮都 `bus.log("warn", ..., source='camera')`，800 行的日志环会在半小时内被同一条刷满，
        真正的现场事件全被挤出去 —— 对一个"靠日志排查"的工具来说等于没有日志。
        所以：只报**这一次故障的第一条**，之后每退避到第 N 次报一条心跳，恢复时再报一条。
        """
        fails = 0
        t_first = 0.0
        while not self._stop.is_set():
            h = self._ensure()
            try:
                for ev in h.events(reconnect=True):
                    if self._stop.is_set():
                        return
                    if fails:
                        self.bus.log("info", f"相机事件订阅恢复（此前连续失败 {fails} 次）", source='camera')
                        fails = 0
                    self._handle_event(ev)
            except Exception as e:                              # noqa: BLE001
                if self._stop.is_set():
                    return
                fails += 1
                self._reached(False)
                if fails == 1:
                    t_first = time.time()
                    self.bus.log("warn", f"相机事件订阅断了：{type(e).__name__}: {e}"
                                         f"；改为退避重试（2→4→8→15 s 封顶），恢复时另报一条", source='camera')
                elif fails % 15 == 0:
                    self.bus.log("warn", f"相机事件订阅仍连不上（已失败 {fails} 次，"
                                         f"约 {time.time() - t_first:.0f} s）", source='camera')
                # 退避：2 / 4 / 8 / 15 封顶。固定 2 s 会在服务真的没起时把机器烧得没必要。
                self._stop.wait(min(15.0, 2.0 * (2 ** min(fails - 1, 3))))

    def _reached(self, ok: bool) -> None:
        if ok:
            self._last_ok_ms = now_ms()

    def _handle_event(self, ev: dict) -> None:
        typ = str(ev.get("type") or "")
        if typ == "_reconnected":
            # 判据 I14：断线期间的事件**找不回**，所以"当前状态"才是唯一的真相，
            # 必须主动告诉客户端"你手上那份可能是过期的，重新对齐"。
            #
            # ⚠️ 这里**不能**再同步调 status()：那样会把事件线程按在 HTTP 上（本机一次
            # 失败探测 2 s），断线时反而更卡、更容易堆积。改成：① 立刻作废快照并踢一次
            # 重探；② 用现有快照发一条把 reconnected 标出来的事件。
            # ⚠️ 这条是**进底栏的界面文案**：不许带破折号（铁律 2，B13 守着）。
            self.bus.log("info", "相机事件流重连：已作废快照并主动重探，请重新对齐", source='camera')
            self.kick_probe()
            self.bus.publish("camera.status", {"reconnected": True, "health": self.health()})
            self._reached(True)
            return

        self._reached(True)

        if typ == "frame_verified":
            self._on_frame_verified(ev)
            return

        if typ in ("session_started", "delivery_done", "session_stopped", "aborted", "gate"):
            self.store.mark("camera", typ, str(ev.get("session_id") or ""),
                            _brief(ev))
            if typ == "delivery_done":
                self.bus.publish("camera.delivery_done", ev)

        if typ in IMMEDIATE or (typ not in THROTTLED and typ):
            self.bus.publish("camera.event", ev)
        elif typ in THROTTLED:
            last = self._throttle.get(typ, 0.0)
            now = time.time()
            if now - last >= THROTTLE_S:
                self._throttle[typ] = now
                self.bus.publish("camera.event", {**ev, "_throttled": True})

    def _on_frame_verified(self, ev: dict) -> None:
        """**每一帧都要走到这里** —— 判据 I6 的行数守恒靠它。

        注意：界面侧的推送是**节流**的，但这一条**不节流**：timeline 行数与
        locator 入队都必须在每一帧上发生，否则"漏记事件"就真的发生了。

        ⚠️ 时间用**拍摄时刻**，不是到达时刻。帧到达 PC 要经过手机编码 + WiFi 上传 +
        落盘 + 校验，这几步加起来可能是**秒级**的；拿到达时刻当"这一帧发生在什么时候"，
        与鼠侧遥测对齐时会整体错开。手机在帧 meta 里给了 `wall_clock_ns`（epoch 纳秒），
        配上本层标定出来的偏移就能换算成 `t_pc_ms`。

        拿不到就**如实退回到达时刻**并把 `t_src` 标成 `arrival` —— 分不清"哪种时间"的
        数据比没有时间更危险。
        """
        seq = ev.get("seq")
        path = self.frame_path(ev.get("session_id"), ev.get("file"))
        t_pc, t_src = None, "arrival"
        caps_ns = _wall_clock_ns(path)
        clk = getattr(self, "clock", None)
        if caps_ns is not None and clk is not None and clk.calibrated:
            v = clk.to_pc_ms_from_ns(caps_ns)
            if v is not None:
                t_pc, t_src = int(v), "capture"
        self.store.mark("camera", "frame_verified", f"frame_{int(seq or 0):06d}",
                        f"bytes={ev.get('bytes')}"
                        + (f" file={ev.get('file')}" if ev.get("file") else "")
                        + ("" if t_src == "capture" else "  ⚠时间=到达时刻（未标定手机偏移）"),
                        t_pc_ms=t_pc, t_src=t_src)
        sess = self.store.current
        if sess is not None:
            sess.counts["camera_frames_verified"] += 1
        for cb in list(self._frame_listeners):
            try:
                cb(ev, path)
            except Exception as e:                              # noqa: BLE001
                self.bus.log("warn", f"帧监听者抛异常（已隔离）：{type(e).__name__}: {e}", source='camera')
        last = self._throttle.get("frame_verified_ui", 0.0)
        if time.time() - last >= THROTTLE_S:
            self._throttle["frame_verified_ui"] = time.time()
            self.bus.publish("camera.frame", {"seq": seq, "file": ev.get("file"),
                                              "bytes": ev.get("bytes"),
                                              "path": str(path) if path else None})

    def add_frame_listener(self, cb: Callable[[dict, Optional[Path]], None]) -> None:
        self._frame_listeners.append(cb)

    # ------------------------------------------------------------ 帧路径解析
    def data_dir(self) -> Optional[Path]:
        """帧文件所在的**会话根目录**。

        三级解析（顺序是有意的）：
          ① 离线导入模式 → 用它指定的根；
          ② 相机服务在线 → 用 `overview()['data_dir']`（这是权威答案：服务自己说它的
             数据目录在哪，而不是我们猜它跟 config 一致）；
          ③ 服务不可达 → **回落到 config 里的 `camera.offline_import_root`**。
             这一步让"没手机也能看历史会话"不需要先点一次导入 ——
             而它之所以安全，是因为回落的目录是我们自己配置里写明的那个，
             不是猜出来的。
        """
        if self._offline is not None:
            return Path(self._offline["root"])
        # ⚠️ 这个函数会被**每一帧**调用（frame_path 里），所以不能每次都发 HTTP：
        # 缓存 10 s，既跟得上现场改 data_dir，又不会把事件线程变成 HTTP 客户端的奴隶。
        if self._data_dir_cache is not None and (now_ms() - self._data_dir_at) < 10000:
            return self._data_dir_cache
        try:
            ov = self.overview()
            dd = ov.get("data_dir")
            if dd:
                self._data_dir_cache = Path(dd)
                self._data_dir_at = now_ms()
                return self._data_dir_cache
        except CameraError:
            pass
        fb = self.cfg.get("camera.offline_import_root")
        if fb and Path(str(fb)).is_dir():
            self._data_dir_cache = Path(str(fb))
            self._data_dir_at = now_ms()
            return self._data_dir_cache
        return None

    def frame_path(self, session_id: Optional[str], file: Optional[str]) -> Optional[Path]:
        """把 `frame_verified` 事件里的 `file` 解析成本地绝对路径。

        ⚠️ 事件里**没有**完整路径（只有 `session_id` + `file`），所以这里依赖
        `data_dir()`。解析不出来就返回 None —— **绝不猜路径**，猜错了会把别的
        会话的帧拿去定位。
        """
        if not session_id or not file:
            return None
        root = self.data_dir()
        if root is None:
            return None
        p = root / str(session_id) / "frames" / str(file)
        return p if p.is_file() else None

    def frames_of(self, session_id: str, limit: int = 0) -> List[dict]:
        """列出一个会话里**已在场的**帧（本地扫盘，不依赖 App 自报）。"""
        root = self.data_dir()
        if root is None:
            return []
        fdir = root / str(session_id) / "frames"
        if not fdir.is_dir():
            return []
        out: List[dict] = []
        for jp in sorted(fdir.glob("frame_*.json")):
            yuv = jp.with_suffix(".yuv")
            if not yuv.is_file():
                yuv = jp.with_suffix(".raw")
            if not yuv.is_file():
                continue                     # 只认"元数据与数据都在场"的帧
            try:
                meta = json.loads(jp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out.append({"seq": meta.get("seq"), "file": yuv.name, "json": str(jp),
                        "path": str(yuv), "width": meta.get("width"),
                        "height": meta.get("height"), "y_stride": meta.get("y_stride"),
                        "repacked": meta.get("repacked"),
                        "wall_clock_ns": meta.get("wall_clock_ns"),
                        "sha256": meta.get("sha256"),
                        "size_bytes": meta.get("size_bytes")})
            if limit and len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------ 离线会话导入（§8.3）
    def import_local_session(self, session_id: str, root: Optional[str] = None,
                             rate_hz: float = 30.0, replay: bool = True) -> dict:
        """把一个**已有的真实会话目录**当成采集源接进来 —— 无手机时的演练通道。

        为什么这是必须做的功能：`PhoneCamera/pc/data/sessions/` 下已经有 14 个真实
        会话（含 149 帧真实 Y 平面），把它们接进 Control 就能在**没有手机、没有鼠、
        没有网络**的条件下把场地页与定位链路端到端跑通。
        """
        base = Path(root) if root else Path(str(self.cfg.get("camera.offline_import_root")))
        sdir = base / str(session_id)
        if not (sdir / "frames").is_dir():
            raise CameraError(
                f"本地无此会话：{sdir}\n  · 离线导入根目录为 "
                f"{base}（config.yaml 的 camera.offline_import_root）")
        frames = self._scan(sdir) if (sdir / "frames").is_dir() else []
        self._offline = {"session_id": str(session_id), "root": str(base)}
        self._offline_frames = frames
        self._data_dir_cache = None                 # 缓存失效：根目录变了
        self.kick_probe()                           # 进离线 ⇒ 快照立刻翻成「离线可达」
        self.bus.log("info", f"已导入本地相机会话 {session_id}："
                             f"{len(self._offline_frames)} 帧（离线模式，不连服务）", source='camera')
        if replay:
            self.start_offline_replay(session_id, rate_hz=rate_hz)
        return {"session_id": session_id, "root": str(base),
                "frames": len(self._offline_frames),
                "replay": bool(replay), "rate_hz": rate_hz}

    @staticmethod
    def _scan(sdir: Path) -> List[Path]:
        out = []
        for jp in sorted((sdir / "frames").glob("frame_*.json")):
            yuv = jp.with_suffix(".yuv")
            if not yuv.is_file():
                yuv = jp.with_suffix(".raw")
            if yuv.is_file():
                out.append(jp)
        return out

    @property
    def offline(self) -> Optional[dict]:
        return self._offline

    def exit_offline(self) -> None:
        self.stop_offline_replay()
        self._offline = None
        self._data_dir_cache = None
        self.kick_probe()       # 退出离线 ⇒ 立刻回去探真服务，别让旧快照说"可达"

    def offline_frames(self) -> List[Path]:
        return list(getattr(self, "_offline_frames", []))

    def start_offline_replay(self, session_id: str = "", rate_hz: float = 30.0) -> dict:
        """把离线会话的帧按给定速率**喂进同一条事件路径**。

        这样做的意义：场地页、定位在线段、时间轴**都不需要为"离线"写第二套代码**。
        离线导入只是一条**来源不同、形状相同**的帧流。
        """
        self.stop_offline_replay()
        self._offline_stop.clear()
        frames = self.offline_frames()
        sid = session_id or (self._offline or {}).get("session_id", "offline")

        def _run() -> None:
            interval = 1.0 / max(0.1, float(rate_hz))
            for jp in frames:
                if self._offline_stop.is_set():
                    return
                t0 = time.time()
                try:
                    meta = json.loads(jp.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                ev = {"type": "frame_verified", "session_id": sid,
                      "seq": meta.get("seq"), "bytes": meta.get("size_bytes"),
                      "file": jp.with_suffix(".yuv").name, "sha256": meta.get("sha256"),
                      "_offline": True,
                      "_wall_clock_ns": meta.get("wall_clock_ns")}
                self._handle_event(ev)
                dt = interval - (time.time() - t0)
                if dt > 0:
                    time.sleep(dt)
            self.bus.publish("camera.delivery_done", {
                "session_id": sid, "_offline": True,
                "note": "离线回放放完"})

        self._offline_thread = threading.Thread(target=_run, name="cam-offline", daemon=True)
        self._offline_thread.start()
        return {"session_id": sid, "frames": len(frames), "rate_hz": rate_hz}

    def stop_offline_replay(self) -> None:
        self._offline_stop.set()
        t = self._offline_thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._offline_thread = None

    @property
    def offline_playing(self) -> bool:
        t = self._offline_thread
        return bool(t is not None and t.is_alive())

    # ------------------------------------------------------------ 健康
    def health(self) -> dict:
        """**零网络**地返回相机侧状态（判据 I12）。

        真相由 `_probe_loop()` 在后台维护。这里只做两件事：读快照、标出快照多旧。
        `reachable` 是**三态**：`True` 可达 / `False` 明确不可达 / `None` 还没探过
        —— 判据 I11 的纪律，`None` 绝不许被渲染成"通过"。
        """
        if not self.available:
            return {"reachable": False, "server_version": None, "detail": self._import_error,
                    "available": False, "offline": None, "probe_age_ms": None, "probe_ms": None}
        if self._offline is not None:
            return {"available": True, "reachable": True, "server_version": "离线会话",
                    "detail": f"离线模式：{self._offline['session_id']}",
                    "offline": self._offline, "playing": self.offline_playing,
                    "base": self.base, "probe_age_ms": 0, "probe_ms": None,
                    "phone": {"clients": None, "offline": True}}

        p = self._probe
        if p is None:
            return {"available": True, "reachable": None, "server_version": None,
                    "detail": "尚未探测",
                    "base": self.base, "offline": None,
                    "probe_age_ms": None, "probe_ms": None, "phone": None}
        out = {
            "available": True,
            "reachable": bool(p.get("reachable")),
            "server_version": p.get("ver"),
            "detail": p.get("err") or "",
            "base": self.base,
            "offline": None,
            "probe_age_ms": max(0, now_ms() - int(p.get("ts") or 0)),
            "probe_ms": round(float(p.get("dur_ms") or 0.0), 1),
            # 手机在线数：`None` = 判不了（探测那次没读到会话表），不是 0 个。
            "phone": p.get("phone"),
        }
        if p.get("reachable"):
            out["status"] = p.get("status") or {}
        return out

    def probe_now(self) -> dict:
        """同步强制探一次（配对页/现场排查用）。**别拿它当 /health 的实现。**

        "强制"是字面意思：等后台那次探完再探，绝不用旧快照糊弄 ——
        调用方（点了按钮的人）要的是此刻的真相。
        """
        self._probe_once(wait_guard=True)
        return self.health()

    def connect(self, base: Optional[str] = None) -> dict:
        """**用户主动点的「连接」**：同步确认一次，并带回一份"确认单"。

        为什么需要一个和 `/health` 分开的同步接口：
          · `/health` 必须**零网络**（判据 I12），所以它只能报后台快照，会有几秒延迟；
          · 而"我点了连接按钮"这个动作**应该等**，且必须回答"到底连上没有、
            连上的是谁"—— 这正是"手动连接后确认状态"的落点。
        代价：不可达时这里会实打实等 ~2 s（本机对"被拒绝"的连接就是这么慢）。
        用户主动点的，等得起；`/health` 等不起，两者不能混。

        `base` 传了就顺手切过去（现场 IP 会变，这是同一个动作的两半）。
        连不上**抛 CameraError**，不返回"看起来成功"的空壳。
        """
        if not self.available:
            raise CameraError(self._import_error or "相机模块不可用")
        if base:
            self.set_base(base)
        st = self.probe_now()
        if not st.get("reachable"):
            raise CameraError(
                f"连不上相机服务 {self.base}\n"
                f"{st.get('detail') or ''}\n"
                f"  · 点【启动上游服务】代启 pc/server.py\n"
                f"  · 或手动启动 PhoneCamera/pc/start_service.cmd\n"
                f"  · 地址不对就到上面「地址」里改；手机 App 也要打开")
        status = st.get("status") or {}
        ph = st.get("phone") or {}
        n = ph.get("clients")
        if n == 0:
            note = "服务可达 · 手机未连"
        elif isinstance(n, int):
            who = " ".join(x for x in (ph.get("model"), ph.get("app_version")) if x)
            note = f"手机已连 {n} 个" + (f" · {who}" if who else "")
        else:
            note = "判不了"
        self.bus.log("info", f"相机已连接 {self.base}"
                             f"（{st.get('server_version') or '版本未知'}）；{note}", source='camera')
        self.store.mark("camera", "camera.connect", str(self.base), note)
        return {"ok": True, "base": self.base, "reachable": True,
                "server_version": st.get("server_version"),
                "protocol_version": status.get("protocol_version"),
                "probe_ms": st.get("probe_ms"),
                "phone": st.get("phone"), "phone_note": note,
                "capture": status.get("capture") or {},
                "gate": status.get("gate") or {}}

    # ------------------------------------------------------------ 健康探测线程
    def start_probe_loop(self) -> None:
        """起后台线程定期探相机。**/health 的真相来源。**

        为什么不能就地在 `/health` 里探：本机实测
        `socket.create_connection(("127.0.0.1", 8765))` 在服务没跑时也要 **2.047 s**
        才抛出 10061（系统层行为，换 `urllib` 一样），于是：
          · `/health` 会稳定花掉 2 s ⇒ 直接违反判据 I12；
          · 前端每秒刷一次健康就会积压请求；
          · 任何"等服务起来"的脚本（含 `tools/drill.py`）都会假超时。
        离线模式（`_offline` 非空）下不探 —— 那时"相机"就是本地目录。
        """
        if not self.available:
            return
        with self._lock:
            if self._probe_thread is not None and self._probe_thread.is_alive():
                return
            self._probe_stop.clear()
            self._probe_thread = threading.Thread(target=self._probe_loop, name="cam-probe",
                                                  daemon=True)
            self._probe_thread.start()

    def stop_probe_loop(self) -> None:
        self._probe_stop.set()
        self._probe_wake.set()

    def kick_probe(self) -> None:
        """丢掉当前快照并立刻重探（改地址、离线进出后用）。"""
        self._probe = None
        self._probe_wake.set()

    def _probe_loop(self) -> None:
        # 起手立刻探一次，免得界面前几百毫秒看到 reachable=None。
        while not self._probe_stop.is_set():
            t0 = time.perf_counter()
            wait_s = self._probe_once()
            # 配置写的是**周期**不是"探测完再等"，所以要扣掉探测自己花掉的时间
            # （一次失败探测本机要 2 s，不扣的话周期会变成 2+4=6 s）。
            spent = time.perf_counter() - t0
            self._probe_wake.wait(max(0.5, wait_s - spent))
            self._probe_wake.clear()

    def _probe_once(self, wait_guard: bool = False) -> float:
        """探一次，写进 `self._probe`；返回**下一次该等多久**（秒）。

        `wait_guard=False`（后台周期线程用）：已有在途探测就**跳过**，
        反正那一次的结果马上就写进快照了。

        `wait_guard=True`（用户点「连接 / 重探」时用）：**必须等闸**再探。
        为什么：闸被后台那一次占着时直接返回的话，`probe_now()` 会拿着一个
        **还没更新的空快照**回去 —— 表现就是"点了重探，界面上啥也没变"，
        而这恰恰是现场排查最需要它管用的时候（真踩过：I5 因此拿到"探测中…"）。
        """
        got = (self._probe_guard.acquire() if wait_guard
               else self._probe_guard.acquire(blocking=False))
        if not got:                                            # 已有在途探测就跳过
            return self._probe_interval_ok
        t0 = time.perf_counter()
        try:
            if self._offline is not None:
                # 离线模式下"相机"是本地目录，不占网络，也不必频繁重估
                self._probe = {"reachable": True, "ts": now_ms(), "dur_ms": 0.0,
                               "err": None, "status": {"offline": True},
                               "ver": "离线会话"}
                self._announce_flip(True)
                return self._probe_interval_ok
            try:
                st = self._ensure().status()
            except Exception as e:                              # noqa: BLE001
                dur = (time.perf_counter() - t0) * 1000.0
                self._probe = {"reachable": False, "ts": now_ms(), "dur_ms": dur,
                               "err": self._explain("读 status", e), "status": None,
                               "ver": None}
                self._reached(False)
                self._announce_flip(False)
                return self._probe_interval_bad
            dur = (time.perf_counter() - t0) * 1000.0
            with self._lock:
                self._last_status = st
            self._reached(True)
            self._probe = {"reachable": True, "ts": now_ms(), "dur_ms": dur, "err": None,
                           "status": st, "phone": self._phone_of(st),
                           "ver": st.get("server_version") or st.get("version")}
            self._announce_flip(True)
            return self._probe_interval_ok
        finally:
            self._probe_guard.release()

    @staticmethod
    def _phone_of(st: Any) -> Optional[dict]:
        """从 `status()` 里解出「手机（App）连上没有」。**服务可达 ≠ 手机在线。**

        为什么这个区分值得单独做：`pc/server.py` 在跑只说明"电脑这边准备好了"；
        手机上那个采集 App 完全可能没打开。这时相机灯是绿的、采集指令也发得出去，
        但**没有任何手机接收** ⇒ `sent_to=0` ⇒ 手机会话是空的。现场最容易在这里白跑。

        `status()['device']` 里本来就有 `connected`/`clients`，所以这一条是**顺带得到**的，
        不需要额外的网络请求（探测线程已经在调 status 了）。

        ⚠️ 字段缺失就返回 `None`（**判不了**），绝不猜成 0 个 ——
        "一个客户端都没有"和"不知道有几个客户端"是两回事（三态纪律 I11）。
        """
        if not isinstance(st, dict):
            return None
        dev = st.get("device")
        if not isinstance(dev, dict):
            return None
        try:
            n = int(dev.get("clients"))
        except (TypeError, ValueError):
            return None
        return {"clients": n,
                "connected": bool(dev.get("connected")),
                "model": dev.get("model") or "",
                "app_version": dev.get("app_version") or "",
                "serial": dev.get("serial") or ""}

    def _announce_flip(self, reachable: bool) -> None:
        """可达性**翻转**时主动推一条事件。

        为什么不只靠前端轮询 `/health`：轮询周期是 2.5 s，现场"插上网线/手机掉线"
        这类翻转你会晚 2.5 s 才知道。翻转是**稀疏且语义重**的事件，值得立刻说。
        """
        prev = self._probe_prev
        self._probe_prev = reachable
        if prev is None or prev == reachable:
            return
        self.bus.log("info", f"相机可达性变为 {'可达' if reachable else '不可达'}", source='camera')
        self.bus.publish("camera.status", {"reachable": reachable, "flipped": True,
                                           "health": self.health()})


def _wall_clock_ns(path: Optional[Path]) -> Optional[int]:
    """从帧的 meta JSON 里取**拍摄时刻**（手机 epoch 纳秒）。

    ⚠️ 要 `wall_clock_ns`（epoch），**不是** `sensor_timestamp_ns` —— 后者是手机开机
    后的单调计数，跨机不可比（设计方案 §2.2 专门列了这条）。
    事件里只带 `sensor_timestamp_ns`，所以这里必须去读盘的 meta。
    文件不存在/字段缺失一律返回 None，由调用方**如实退回**到到达时刻。
    """
    if path is None:
        return None
    try:
        from ..decode import load_meta
        ns = getattr(load_meta(path), "wall_clock_ns", None)
        return None if ns is None else int(ns)
    except Exception:                                          # noqa: BLE001
        return None


def _brief(ev: dict, n: int = 140) -> str:
    keep = {k: v for k, v in ev.items() if k != "type"}
    s = json.dumps(keep, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + "…"
