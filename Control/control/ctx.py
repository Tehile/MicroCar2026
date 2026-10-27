"""应用上下文：把配置、时钟、总线、会话库、适配器、场地几何装在一起。

**改版二十四**：手机相机整块下线（PhoneCamera / 8765 / hostlink / 在线定位 / 代启），
位置数据的唯一来路改成**视频会话**（`video` 适配器 + Auto Locate 的 `run_session`）。
所以这一层现在只管两路：

    · **电脑鼠**（实时链路）—— 会话记录里唯一有"实时数据"的那一路；
    · **视频复盘**（离线批处理）—— 一段视频 → 轨迹，不进会话、不占实时通道。

「一次开始记录」的原子性编排仍在 `session_open`：现在只有鼠一路，但那条纪律不变
（**要么整份有效、要么整份不存在**，判据 I5）—— 半份会话比没有会话更糟。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .adapters.mouse import MouseAdapter, MouseError
from .adapters.video import VideoAdapter
from .arena import Arena, from_config
from .bus import Bus
from .clock import Clock, now_iso, now_ms
from .maze_rec import MazeRecorder
from .store import Store, git_info


class SessionError(RuntimeError):
    """会话编排失败。**必须已经回滚干净**，消息要说明回滚到哪一步。"""


class Ctx:
    def __init__(self, cfg: Any, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        self.cfg = cfg
        self.clock = Clock(offset_ms=float(cfg.get("clock.offset_ms", 0.0)),
                           uncertainty_ms=float(cfg.get("clock.uncertainty_ms", 250.0)),
                           source=str(cfg.get("clock.source", "assumed")))
        self.bus = Bus(loop=loop, log_ring=int(cfg.get("server.log_ring", 800)))
        self.store = Store(cfg.session_root)
        self.arena: Arena = from_config(cfg)
        self.mouse = MouseAdapter(cfg, self.bus, self.store)
        # 实验 8 的「从发车到停止」记录器。**它订阅的是上游的 `Event.TELEMETRY`**
        # （每帧一次），不是本层的 `mouse.telemetry`（那条只在有人看波形时才发）。
        # 详细理由写在 `maze_rec.py` 的模块 docstring 里 —— 那是个"会静默没数据"的坑。
        self.maze_rec = MazeRecorder(
            Path(str(cfg.get("records.maze_root", "records/maze"))),
            get_channels=lambda: list(getattr(self.mouse, "_unified_channels", []) or []),
            # 完整性差量的取数：本机链路统计（`seq_lost`/`bad_crc`）。
            # 设备侧 `dropCnt` v9 已删 ⇒ 只能退化为本机口径，见
            # `maze_rec._probe_losses`（不许在记录器里自己比 seq）。
            get_link_stats=self.mouse.link_stats,
            # 发车时的参数快照（改版二十八）。走 `refresh=False` ⇒ **纯读缓存、
            # 不碰设备**：开录这一步绝不能因为"读参数"而多一次串口往返（发车时序
            # 本来就在等一条命令）。缓存空着就回 None ⇒ 回放显示「未记录」。
            get_params=self._rec_params,
            # 开录时的设备信息（改版四十六）：只有 `fw`，给回放的 4 灯三态用。
            # 同样**纯读缓存**（`caps()` 不碰设备）：开录这一步多一次串口往返，
            # 就是在发车时序上多加一段。取不到 ⇒ None ⇒ 回放说"判不了"。
            get_device=self._rec_device)
        # 决策差分的**码表**（上游的路口分类器与名字表）—— 从鼠适配器取，不在这里
        # import 上游（路径注入、能力探测都在适配器里，这一层不该再摸一遍）。
        # 适配器那时**可能还没装载完**（`ctx` 先于 `_load()` 的某些时机）⇒ 取不到就不注入，
        # 由适配器的 `_attach()` 在装载成功后补一次（`set_codes`）。
        self.maze_rec.set_codes(self.mouse.maze_codes())
        # 记录器的日志钩子：差分失败要**进日志**（否则右栏"悄悄变空"，看的人以为本来就没决策）
        self.maze_rec._log = self.bus.log
        self.mouse.maze_recorder = self.maze_rec
        # 视频复盘：位置数据的**唯一来路**。它起子进程跑 Auto Locate 的会话解析，
        # 本进程只做"起/停/查状态/读产物"——与鼠链路完全解耦（删相机时这一路没改一行）。
        self.video = VideoAdapter(cfg, self.bus)
        self.started_ms = now_ms()
        self._tasks: List[asyncio.Task] = []
        #: 最近一帧遥测 —— 场地页的 Δ 读数靠它（不需要前端自己攒）
        self.last_telemetry: Dict[str, Any] = {}

    # ------------------------------------------------------------ 后台任务
    async def start_background(self) -> None:
        self.bus.bind_loop(asyncio.get_running_loop())
        self._tasks.append(asyncio.create_task(self.mouse.sampler_loop(),
                                               name="mouse-sampler"))
        self._tasks.append(asyncio.create_task(self._bus_tap(), name="bus-tap"))

    async def _bus_tap(self) -> None:
        """把"最新的遥测"留在 ctx 里，供场地页的 Δ 读数直接取用。

        为什么不在前端攒：前端要算 Δ 就得同时持有"最新定位"与"最新固件位置"
        并且知道它们是**哪一刻**的 —— 那是时间对齐问题，属于服务端该负责的事。
        ⚠️ 改版二十四：`locate.result` 那一路随相机下线，这里只剩遥测一路。
        """
        q = self.bus.subscribe()
        try:
            while True:
                msg = await q.get()
                if msg.get("type") == "mouse.telemetry":
                    self.last_telemetry = msg
        except asyncio.CancelledError:
            raise
        finally:
            self.bus.unsubscribe(q)

    # ------------------------------------------------------------ 逃生舱
    def mouse_send_raw(self, line: str) -> dict:
        """原始命令（**同步、要等设备回**）⇒ 调用方必须放到线程里跑。"""
        if self.mouse.mc is None:
            raise MouseError("未连接：原始命令通道需先建立链路")
        res = self.mouse.mc.send_raw(line, timeout=3.0)
        self.store.mark("mouse", "raw", line, f"ok={res.ok} status={res.status}")
        return {"ok": bool(res.ok), "status": res.status, "cmd": line,
                "data": res.data}

    async def shutdown(self) -> None:
        self.bus.log("info", "正在收尾：停后台任务 → 关适配器")
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):      # noqa: BLE001
                pass
        self._tasks.clear()
        self.video.shutdown()         # 只杀**本次会话起的那个子进程**（铁律 25）
        try:
            if self.store.is_open:
                self.store.rollback()
        except Exception:                                    # noqa: BLE001
            pass
        self.mouse.close()

    # ------------------------------------------------------------ 健康 / 能力
    def _rec_params(self) -> Optional[dict]:
        """给记录器用的**参数快照**（开录那一刻冻一次）。

        `refresh=False` ⇒ 只读适配器的缓存，**零设备访问**。为什么这么苛求：
        它是在"发车/开录"这条路径上被调用的，而那条路径正在等设备回话 ——
        多一次串口往返就等于把发车推迟一截，且失败时没人知道是被这个拖的。

        返回 `{groups, at_ms, source}`；缓存空着 ⇒ `None`（回放页显示「未记录」）。
        **绝不拿当前参数去补一份历史记录的快照** —— 那是造假。
        """
        try:
            g = self.mouse.params_grouped(refresh=False) or {}
        except Exception:                                    # noqa: BLE001
            return None
        groups = g.get("groups") or {}
        if not groups:
            return None
        return {"groups": groups, "at_ms": now_ms(), "source": "cache"}

    def _rec_device(self) -> Optional[dict]:
        """给记录器用的**设备信息快照**（开录那一刻冻一次）：目前只有 `fw`。

        为什么要它：4 灯的三态判据里有一半是"这 4 位固件到底会不会填"
        （`control/leds.py` 的 `LED_MIN_FW=311`）。记录里只有 `led1..4` 的 0/1 时，
        回放**分不出"真灭"与"预留恒 0"** —— 而把"没实现"画成"灭"正是那个模块
        要消灭的静默错。`caps()` 读的是缓存（不碰设备），`fw_int` 已经认不出给 None。
        """
        try:
            caps = self.mouse.caps() or {}
        except Exception:                                    # noqa: BLE001
            return None
        fw = caps.get("fw_int")
        return None if fw is None else {"fw": fw}

    def health(self) -> dict:
        m = self.mouse.health()
        cl = self.clock.snapshot()
        # 视频会话的状态是**纯内存**的（不扫盘、不读文件），可以放心放进 /health。
        # 但它**不参与** `all_ready`：视频是离线批处理，跟"现在能不能开拍"无关。
        v = self.video.status()
        return {
            "ok": True,                       # 判据 I2：任何一路挂了，这个端点仍必须 200
            "all_ready": bool(m.get("connected")) and bool(cl.get("calibrated")),
            # 「先连接、再测试」的清单。纯内存计算，**一个网络请求都不发**（判据 I12）。
            "readiness": self.readiness(m, cl),
            "mouse": m,
            "video": v,
            "clock": cl,
            "arena": self.arena.to_dict(),
            "session": (self.store.current.to_dict() if self.store.current else None),
            "host_version": __version__,
            "uptime_s": round((now_ms() - self.started_ms) / 1000.0, 1),
            "unavailable": [k for k, val in (("mouse", m),) if not val.get("available")],
        }

    def readiness(self, m: Optional[dict] = None, cl: Optional[dict] = None) -> dict:
        """「准备清单」—— **先手动连接、再测试** 这条纪律的机器可读版本。**零网络。**

        为什么值得单独算：现场真正要回答的是一个**连起来的问题** ——
        「现在能开拍了吗？不能的话缺哪一步、那一步要动手做什么」。
        把它一次性算好，界面就不用自己拼，也不会在几个地方各写一遍判断。

        `manual` 是**物理前提**（要动手的），`action` 是**点哪里**。两者分开写，
        因为"插好 USB"和"点连接按钮"是两件事，混成一句就没人知道该先干哪个。

        ⚠️ 改版二十四：相机与定位两项随相机下线（位置数据改视频会话，**它不拦开拍**：
        视频解析跟"现在能不能记录"没有关系）。所以清单只剩两条，而 `can_record`
        变成"**电脑鼠连上了**"—— 这条判据比原来更严格（以前只要有一路就算能记）。
        """
        m = self.mouse.health() if m is None else m
        cl = self.clock.snapshot() if cl is None else cl
        v = self.video.status()
        vst = v.get("state") or "idle"
        items = [
            {
                "key": "mouse", "name": "电脑鼠", "blocking": True,
                "ok": bool(m.get("connected")),
                "state": ("已连 " + str(m.get("port") or "")) if m.get("connected")
                         else ("模块缺失" if not m.get("available") else "未连接"),
                "manual": "USB 线插好；上游的 run_host.py 必须关掉（串口独占，"
                          "两个程序同时只会有一个连上）",
                "action": "点顶栏【电脑鼠】连接",
                "detail": (m.get("detail") or "").split("\n")[0],
            },
            {
                # 时钟**不拦**开会话：时间偏移没标定只会让跨机对齐全靠 assumed，
                # 数据照样是有效的（每条记录仍然盖了 PC 墙钟）。
                "key": "clock", "name": "时间基", "blocking": False,
                "ok": bool(cl.get("calibrated")) and bool((m.get("timesync") or {}).get("armed")),
                "state": ("偏移已标定 ±" + str(cl.get("uncertainty_ms")) + "ms"
                          if cl.get("calibrated") else "偏移未标定（手动填写）"),
                "manual": "跨机对齐要有一个偏移量；设备时基在发车时对齐（TimeSync）",
                "action": "点顶栏【时钟】填偏移；② 发车 ARM",
                "detail": "",
            },
            {
                # 视频是**离线**的：它既不拦开拍，也不需要"连接"。
                # 放在清单里只为一件事——让人一眼看到"上一次解析成没成"。
                "key": "video", "name": "视频复盘", "blocking": False,
                "ok": vst in ("done", "idle"),
                "state": {"running": "解析中", "done": "上次解析完成",
                          "failed": "上次解析失败", "cancelled": "上次已取消",
                          "idle": "还没有解析过"}.get(vst, vst),
                "manual": "视频放服务器目录（或直接填绝对路径），解析完才产出轨迹",
                "action": "入口页【视频复盘】→【视频会话】",
                "detail": (v.get("error") or "").split("\n")[0] if vst == "failed" else "",
            },
        ]
        blockers = [f"{i['name']}：{i['action']}" for i in items
                    if i["blocking"] and not i["ok"]]
        connected = [i for i in items if i["blocking"] and i["ok"]]
        return {
            "items": items,
            "ready": not blockers,
            # 「能不能开记录」= 有没有**真在出数据**的那一路。现在只剩电脑鼠：
            # 没有它，会话里就真的一条记录都不会有（以前相机能单独撑起一场空会话）。
            "can_record": bool(connected),
            "blockers": blockers,
            "connected": [i["key"] for i in connected],
        }

    def caps(self) -> dict:
        return {
            "host_version": __version__,
            "mouse": self.mouse.caps(),
            "video": self.video.caps(),
            "server": {"host": self.cfg.get("server.host"),
                       "port": self.cfg.get("server.port"),
                       "session_root": str(self.cfg.session_root)},
            "clock": self.clock.snapshot(),
            "arena": self.arena.to_dict(),
            "ws": {
                "json_limit_bytes": 64 * 1024,
                "bin_frame": "[4B uint32 header_len][JSON header][Float32Array payload]",
                "note": "payload > 64 KB 一律走二进制帧（判据 I10）",
            },
        }

    # ------------------------------------------------------------ 路径
    @property
    def paths(self) -> dict:
        return {
            "control": str(self.cfg.root),
            "sessions": str(self.cfg.session_root),
            "remotecontrol_host": self.cfg.get("paths.remotecontrol_host"),
            "autolocate": self.cfg.get("paths.autolocate"),
            "video_root": self.cfg.get("video.root"),
            "video_out_root": self.cfg.get("video.out_root"),
            "video_config": self.cfg.get("video.config"),
        }

    def upstream(self) -> dict:
        """两个上游的版本 / commit / 工作区是否洁净（§5.4 要求记进 meta）。

        改版二十四：PhoneCamera 整块下线 ⇒ 只剩 RemoteControl 与 Auto Locate。
        """
        p = self.paths
        rc = git_info(p["remotecontrol_host"])
        al = git_info(p["autolocate"])
        return {
            "remotecontrol": {**rc, "api_version": self.mouse.caps().get("api_version")},
            "autolocate": {**al, "version": self._autolocate_version()},
        }

    def _autolocate_version(self) -> Optional[str]:
        """读 Auto Locate 的版本号（读包内 `__init__.py` 的 `__version__`）。

        为什么不用 import：那会把整套视觉依赖（cv2/numpy）拉进**服务进程**，
        而我们只用它写一行元数据。读文件失败就回 None（**不编**）。
        """
        p = self.cfg.get("paths.autolocate")
        if not p:
            return None
        f = Path(str(p)) / "aruco_locator" / "__init__.py"
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.startswith("__version__"):
                    return line.split("=", 1)[1].strip().strip('"\'')
        except OSError:
            return None
        return None

    # ------------------------------------------------------------ 会话（原子开关）
    async def session_open(self, session_id: Optional[str] = None,
                           notes: Optional[List[str]] = None,
                           arena_note: bool = True) -> dict:
        """开一次统一会话。**任一步失败 ⇒ 回滚已成功的步骤**（判据 I5）。"""
        if self.store.is_open:
            raise SessionError(f"已经有一个会话开着：{self.store.current.session_id}。"
                               f"先停掉再开。")

        # ---- 先过「准备清单」：一路都没连就开拍，开出来的是一场空记录。
        # 这不是洁癖 —— 空会话会让人以为"记录下来了"，而事后翻 timeline 才发现
        # 里面什么设备事件都没有，那比当场被拦住糟得多。
        rd = self.readiness()
        if not rd["can_record"]:
            raise SessionError(
                "未连接任何设备，会话里不会有数据。先手动连好：\n"
                + "\n".join("  · " + b for b in rd["blockers"]))
        rd_warn: List[str] = [f"{it['name']}没连上（{it['state']}）：{it['action']}"
                              for it in rd["items"] if it["blocking"] and not it["ok"]]
        done: List[str] = []
        snapshot = {
            "clock": self.clock.snapshot(),
            # 固件毫秒时基 → 本机单调秒 的这一段是**上游**的结论（mc.timesync），
            # 与跨机偏移是两回事 —— 分开记，不许合成一个数（见 clock.py）。
            "device": self.mouse.timesync_snapshot(),
            "mouse_features": dict(getattr(self.mouse, "_features", {}) or {}),
            "arena": self.arena.to_dict(),
            "video": self.video.caps(),
            "upstream": self.upstream(),
            "paths": self.paths,
        }
        s = self.store.open(session_id=session_id, notes=notes or [], snapshot=snapshot)
        sid = s.session_id
        self.bus.publish("session.phase", {"session_id": sid, "phase": s.phase,
                                           "op": "open", "step": "created"})
        self.bus.log("info", f"会话 {sid} 已建目录：{s.path}")
        try:
            # ① 鼠会话（本地目录，失败代价≈0）
            if self.mouse.available:
                # ⚠️ **开记录必须先有帧**（改版三十一）：拉取改成"页面按需"之后，
                #    连上时可能是关着的。这里兜底开一次 —— 记录是"要落数据"的动作，
                #    不能靠"用户正好停在实时状态页"这种巧合。
                #    失败不阻断开会话（宁可记一个空会话，也要让现场看到报错）。
                try:
                    if not self.mouse.stream_on:
                        r = self.mouse.set_stream(True)
                        self.bus.log("info", "开记录顺带打开拉取（之前是关的）"
                                             f"· n={r.get('n')}"
                                             + ("（批式）" if r.get("applied_n") else "（停等）"),
                                     source="mouse")
                        self.store.mark("mouse", "stream.auto_on", "",
                                        f"why=session_open n={r.get('n')}")
                except Exception as e:                        # noqa: BLE001
                    self.bus.log("warn", f"开记录时没能打开拉取：{type(e).__name__}: {e}",
                                 source="mouse")
                try:
                    path = self.mouse.open_session(f"{sid}/mouse")
                    s.mouse_path = path
                    s.phase["mouse"] = "open"
                    done.append("mouse")
                    self.store.mark("mouse", "session.open", "", f"path={path}")
                except MouseError as e:
                    s.phase["mouse"] = "failed"
                    s.detail["mouse"] = str(e)
                    self.bus.log("warn", f"鼠会话没开成：{e}", source="mouse")
            else:
                s.phase["mouse"] = "failed"
                s.detail["mouse"] = self.mouse.import_error or "模块不可用"
        except Exception as e:                                # noqa: BLE001
            # ---- 回滚：把已成功的步骤逐个撤掉，然后**删掉会话目录**
            rolled: List[str] = []
            if "mouse" in done:
                try:
                    self.mouse.close_session({"rolled_back": True})
                    rolled.append("mouse")
                except MouseError:
                    pass
                mp = s.path / "mouse"
                if mp.is_dir():
                    # 用 store 里那个"绝不抛异常"的删除器 —— 回滚路径**不许**把服务搞崩
                    from .store import _rmtree_quiet
                    _rmtree_quiet(mp)
            spath = self.store.rollback().path
            # 回滚**必须说实话**：目录真删掉了才说删掉了。
            # `_rmtree_quiet` 按设计吞掉所有异常（不让清理失败把服务搞崩），
            # 但"吞掉异常"不等于"删成功" —— 环境限制（只读、占用、删除守卫）
            # 会让目录留在盘上。以前这里无条件写"会话目录已删除"，
            # 实测就出现过"报已回滚、目录却还在"（判据 I5 的检查因此变红）。
            gone = not spath.exists()
            raise SessionError(
                f"开会话失败：{type(e).__name__}: {e}\n"
                f"  · 已回滚：{rolled or '（无步骤成功）'}；"
                + ("会话目录已删除（判据 I5）" if gone
                   else f"目录未删除：{spath}。请手动删除，避免半个会话混入会话列表")) from e

        self.bus.publish("session.phase", {"session_id": sid, "phase": s.phase,
                                           "op": "open", "step": "ready"})
        out = s.to_dict()
        # 门控结果一并回给调用方：界面要能把"这一路是空的、因为没连"讲清楚，
        # 而不是等收会话时才发现少了一半数据。
        out["readiness"] = rd
        out["warnings"] = rd_warn
        return out

    async def session_close(self) -> dict:
        """收一次会话。

        改版二十四：相机下线后**没有"等采集排空"这一步**了（那是手机投递的账本），
        所以不再有 `wait_delivery_s` / `auto_rerun` / 赛后复算。只剩"收鼠会话 + 关账"。
        """
        s = self.store.current
        if s is None:
            raise SessionError("当前无进行中的会话")
        warning: List[str] = []

        # ① 收鼠会话
        if s.phase.get("mouse") == "open":
            try:
                self.mouse.close_session({"control_session": s.session_id})
            except MouseError as e:
                warning.append(f"收鼠会话失败：{e}")
            s.phase["mouse"] = "closed"

        self.store.mark("session", "session.close", s.session_id,
                        "; ".join(warning) if warning else "无条件")
        closed = self.store.close(extra_meta={"warnings": warning} if warning else None)

        out: Dict[str, Any] = {"session": closed.to_dict() if closed else None,
                               "warnings": warning}
        self.bus.publish("session.phase", {"session_id": s.session_id,
                                           "phase": s.phase, "op": "close",
                                           "warnings": warning})
        return out

    def save_clock(self, offset_ms: Optional[float] = None, uncertainty_ms: Optional[float] = None,
                   source: Optional[str] = None) -> dict:
        if offset_ms is not None or uncertainty_ms is not None or source is not None:
            self.clock.set_offset(
                self.clock.offset_ms if offset_ms is None else offset_ms,
                self.clock.uncertainty_ms if uncertainty_ms is None else uncertainty_ms,
                source or "manual", method="人工输入（/api/v1/clock/offset）")
        snap = self.clock.snapshot()
        self.cfg.patch({
            "clock.offset_ms": f"{snap['offset_ms']}",
            "clock.uncertainty_ms": f"{snap['uncertainty_ms']}",
            "clock.source": f"{snap['source']}",
        })
        self.bus.publish("clock.changed", snap)
        return snap


def _safe(fn) -> Any:
    try:
        return fn()
    except MouseError as e:
        return {"available": False, "error": str(e)}
