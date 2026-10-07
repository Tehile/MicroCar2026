"""应用上下文：把配置、时钟、总线、会话库、三个适配器、场地几何装在一起。

「一次开始记录」的原子性编排也在这里（设计方案 §5.3）——这是整个整合层最容易
出错、也最值得单独放一处的地方：**三路必须同生同死**，否则会产出"看着正常、
其实缺一路"的半个会话。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .adapters.camera import CameraAdapter, CameraError
from .adapters.locator import LocatorAdapter, LocatorError
from .adapters.mouse import MouseAdapter, MouseError
from .arena import Arena, from_config
from .bus import Bus
from .clock import Clock, now_iso, now_ms
from .launcher import ServiceLauncher
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
            get_channels=lambda: list(getattr(self.mouse, "_unified_channels", []) or []))
        # 决策差分的**码表**（上游的路口分类器与名字表）—— 从鼠适配器取，不在这里
        # import 上游（路径注入、能力探测都在适配器里，这一层不该再摸一遍）。
        # 适配器那时**可能还没装载完**（`ctx` 先于 `_load()` 的某些时机）⇒ 取不到就不注入，
        # 由适配器的 `_attach()` 在装载成功后补一次（`set_codes`）。
        self.maze_rec.set_codes(self.mouse.maze_codes())
        # 记录器的日志钩子：差分失败要**进日志**（否则右栏"悄悄变空"，看的人以为本来就没决策）
        self.maze_rec._log = self.bus.log
        self.mouse.maze_recorder = self.maze_rec
        self.camera = CameraAdapter(cfg, self.bus, self.store)
        # 上游相机服务的代启器（用户要求：配对时不必手动启动上游）。
        # 它与 camera 适配器分工明确：这里只管**进程**，通不通由 camera 探。
        self.launcher = ServiceLauncher(cfg, self.bus, self.store)
        # 把主时钟注入相机适配器 —— 它要靠它把帧的**拍摄时刻**（手机 epoch ns）
        # 换算成 t_pc_ms，而不是拿"帧到达主机的时刻"当时间（两者可能差秒级）。
        self.camera.clock = self.clock
        self.locator = LocatorAdapter(cfg, self.bus, self.store)
        self.started_ms = now_ms()
        self._tasks: List[asyncio.Task] = []
        #: 最近一次定位结果 / 最近一帧遥测 —— 场地页的 Δpos 读数靠它（不需要前端自己攒）
        self.last_loc: Optional[dict] = None
        self.last_telemetry: Dict[str, Any] = {}
        self.last_annotated: Dict[int, bytes] = {}          # seq -> jpeg（小缓存）
        # 离线回放也走同一条帧路径 ⇒ 不需要为"离线"写第二套代码
        self.camera.add_frame_listener(self.locator.submit_frame)

    # ------------------------------------------------------------ 后台任务
    async def start_background(self) -> None:
        self.bus.bind_loop(asyncio.get_running_loop())
        self.camera.start_events(asyncio.get_running_loop())
        # 相机健康必须由后台线程维护：同步探一次要 ~2 s（本机对"被拒绝"的连接就是这么慢），
        # `/health` 自己探就必然违反判据 I12。
        self.camera.start_probe_loop()
        self.locator.start_online()
        self._tasks.append(asyncio.create_task(self.mouse.sampler_loop(),
                                               name="mouse-sampler"))
        self._tasks.append(asyncio.create_task(self._delivery_watcher(),
                                               name="delivery-watcher"))
        self._tasks.append(asyncio.create_task(self._bus_tap(), name="bus-tap"))

    async def _bus_tap(self) -> None:
        """把"最新的两路状态"留在 ctx 里，供场地页的 Δpos / Δθ / Δt 读数直接取用。

        为什么不在前端攒：前端要算 Δpos 就得同时持有"最新定位"与"最新固件位置"
        并且知道它们是**哪一刻**的 —— 那是时间对齐问题，属于服务端该负责的事。
        """
        q = self.bus.subscribe()
        try:
            while True:
                msg = await q.get()
                t = msg.get("type")
                if t == "locate.result":
                    self.last_loc = msg
                elif t == "mouse.telemetry":
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
        self.bus.log("info", "正在收尾：停后台任务 → 停相机事件线程 → 关适配器")
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):      # noqa: BLE001
                pass
        self._tasks.clear()
        self.camera.stop_events()
        self.camera.stop_probe_loop()
        self.camera.stop_offline_replay()
        self.launcher.shutdown()      # 默认只**报告**它还在跑；stop_on_exit=true 才停
        self.locator.stop_online()
        try:
            if self.store.is_open:
                self.store.rollback()
        except Exception:                                    # noqa: BLE001
            pass
        self.mouse.close()

    async def _delivery_watcher(self) -> None:
        """等 `delivery_done` → 自动触发**赛后批量定位**（权威数据那条）。

        为什么必须"等排空"：批量定位要读全量帧，而帧是**分两步落**的
        （先 `.yuv` 后 `.json`）。早一步开始就会把"还没落完"的帧判成坏帧。
        """
        q = self.bus.subscribe()
        try:
            while True:
                msg = await q.get()
                if msg.get("type") != "camera.delivery_done":
                    continue
                if not bool(self.cfg.get("locator.offline.enabled", True)):
                    continue
                if not bool(self.cfg.get("locator.offline.auto_run_after_delivery", True)):
                    continue
                sid = msg.get("session_id")
                await asyncio.sleep(0.5)                     # 给最后几帧落盘留一点时间
                try:
                    self.trigger_batch(sid)
                except (CameraError, LocatorError) as e:
                    self.bus.log("warn", f"自动赛后复算没能启动：{e}")
        except asyncio.CancelledError:
            raise
        finally:
            self.bus.unsubscribe(q)

    # ------------------------------------------------------------ 健康 / 能力
    def health(self) -> dict:
        m = self.mouse.health()
        c = self.camera.health()
        # 相机健康里附一份**代启器**现状（纯内存 + 一次 poll，零网络、不读日志文件）：
        # 界面要靠它决定"配对时要不要自动代启"、以及【启动/停止】能不能按。
        # 显式 deep=False —— `/health` 每 2.5 s 走一次，日志尾巴归 GET /camera/service。
        c["launcher"] = self.launcher.status(deep=False)
        loc = self.locator.health()
        cl = self.clock.snapshot()
        ok_all = bool(m.get("connected")) and bool(c.get("reachable")) and bool(loc.get("ready"))
        return {
            "ok": True,                       # 判据 I2：任何一路挂了，这个端点仍必须 200
            "all_ready": ok_all,
            # 「先连接、再测试」的清单。纯内存计算，**一个网络请求都不发**（判据 I12）。
            "readiness": self.readiness(m, c, cl),
            "mouse": m,
            "camera": c,
            "locator": loc,
            "clock": cl,
            "arena": self.arena.to_dict(),
            "session": (self.store.current.to_dict() if self.store.current else None),
            "host_version": __version__,
            "uptime_s": round((now_ms() - self.started_ms) / 1000.0, 1),
            "unavailable": [k for k, v in (("mouse", m), ("camera", c), ("locator", loc))
                            if not (v.get("available"))],
        }

    def readiness(self, m: Optional[dict] = None, c: Optional[dict] = None,
                  cl: Optional[dict] = None) -> dict:
        """「准备清单」—— **先手动连接、再测试** 这条纪律的机器可读版本。**零网络。**

        为什么值得单独算：三路"能不能用"本来是分散在三个 health 里的，
        而现场真正要回答的是一个**连起来的问题** ——「现在能开拍了吗？不能的话缺哪一步、
        那一步要动手做什么」。把它一次性算好，界面就不用自己拼，
        也不会在七个地方各写一遍判断（写岔了就会出现"某页说能跑、某页说不能"）。

        `manual` 是**物理前提**（要动手的），`action` 是**点哪里**。两者分开写，
        因为"插好 USB"和"点连接按钮"是两件事，混成一句就没人知道该先干哪个。

        相机这一项**刻意把"服务可达"和"手机在线"分开判**：服务在跑但手机没连时，
        采集会得到一个空会话（上游回 `sent_to=0`），此时说"相机准备好了"是错的。
        """
        # 三个入参是为了让 `/health` 不必把同一份快照算两遍（它自己已经调过了）。
        m = self.mouse.health() if m is None else m
        c = self.camera.health() if c is None else c
        cl = self.clock.snapshot() if cl is None else cl
        ph = c.get("phone") or {}
        offline = bool(c.get("offline"))
        clients = ph.get("clients")
        cam_reach = bool(c.get("reachable"))
        # 手机在线：>0 一定好；==0 一定不好；None（判不了）**不当成不好**
        # —— 三态纪律，不知道就不能算拦住（否则一次读不到字段就永远开不了拍）。
        phone_ok = offline or (clients is None) or (isinstance(clients, int) and clients > 0)
        cam_ok = cam_reach and phone_ok

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
                "key": "camera", "name": "手机相机", "blocking": True,
                "ok": cam_ok,
                # state 一句里**同时说两件事**：服务通不通 + 手机在不在。
                # 只写"服务可达"会让 ok=False 看起来自相矛盾（正是要避免的误读）。
                "state": (
                    "离线：" + str((c.get("offline") or {}).get("session_id")) if offline
                    else ("服务可达 · 手机已连 %d 个" % clients
                          if isinstance(clients, int) and clients > 0
                          else "服务可达 · 手机未连" if cam_reach and clients == 0
                          else "服务可达 · 手机在线数判不了" if cam_reach and clients is None
                          else "服务可达" if cam_reach
                          else "探测中…" if c.get("reachable") is None
                          else "不可达")),
                "manual": "手机与电脑接同一个 WiFi；手机 App 打开后扫码配对。"
                          "（相机服务跑在电脑上，手机只是它的客户端）",
                "action": "点顶栏【相机】配对",
                "phone": ph, "clients": clients,
                "detail": (c.get("detail") or "").split("\n")[0],
                "sub": (None if offline else
                        ("手机已连 " + str(clients) + " 个" if isinstance(clients, int) and clients > 0
                         else "服务可达，手机未连"
                         if clients == 0 else
                         "手机在线数：判不了" if cam_reach else "")),
            },
            {
                # 时钟**不拦**开会话：时间偏移没标定只会让跨机对齐全靠 assumed，
                # 数据照样是有效的（每条记录仍然盖了 PC 墙钟）。标成 blocking=False
                # 是有意的 —— 把"没标定"升级成"不许开拍"会挡住合法的快速试验。
                "key": "clock", "name": "时间基", "blocking": False,
                "ok": bool(cl.get("calibrated")) and bool((m.get("timesync") or {}).get("armed")),
                "state": ("手机偏移已标定 ±" + str(cl.get("uncertainty_ms")) + "ms"
                          if cl.get("calibrated") else "手机偏移未标定"),
                "manual": "手机与电脑的墙钟偏移要标一次；设备时基在发车时对齐（TimeSync）",
                "action": "④ 标定 · ⑤ 发车 ARM",
                "detail": "",
            },
        ]
        blockers = [f"{i['name']}：{i['action']}" for i in items
                    if i["blocking"] and not i["ok"]]
        connected = [i for i in items if i["blocking"] and i["ok"]]
        return {
            "items": items,
            "ready": not blockers,
            # 「能不能开记录」= 至少有一路真的连上了。实测中确实存在"今天只测场地"
            # 或"今天只调参数"的用法，硬要求三路全齐会把这两种挡掉。
            "can_record": bool(connected),
            "blockers": blockers,
            "connected": [i["key"] for i in connected],
        }

    def caps(self) -> dict:
        return {
            "host_version": __version__,
            "mouse": self.mouse.caps(),
            "camera": _safe(lambda: self.camera.capabilities()),
            "locator": self.locator.caps(),
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
            "phonecamera_pc": self.cfg.get("paths.phonecamera_pc"),
            "autolocate": self.cfg.get("paths.autolocate"),
            "locator_config": self.cfg.get("locator.config"),
        }

    def upstream(self) -> dict:
        """三个上游的版本 / commit / 工作区是否洁净（§5.4 要求记进 meta）。"""
        p = self.paths
        rc = git_info(p["remotecontrol_host"])
        al = git_info(p["autolocate"])
        ph = git_info(p["phonecamera_pc"])
        try:
            cam = self.camera.capabilities()
        except CameraError:
            cam = {}
        return {
            "remotecontrol": {**rc, "api_version": self.mouse.caps().get("api_version")},
            "phonecamera": {**ph, "server_version": cam.get("server_version")
                            or cam.get("version"),
                            "protocol_version": cam.get("protocol_version")},
            "autolocate": {**al, "version": self.locator.caps().get("version")},
        }

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
        rd_warn: List[str] = []
        for it in rd["items"]:
            if it["blocking"] and not it["ok"]:
                rd_warn.append(f"{it['name']}没连上（{it['state']}）：{it['action']}")
            elif it.get("sub") and it["key"] == "camera" and "手机未连" in str(it.get("sub")):
                rd_warn.append(str(it["sub"]))
        done: List[str] = []
        snapshot = {
            "clock": self.clock.snapshot(),
            # 固件毫秒时基 → 本机单调秒 的这一段是**上游**的结论（mc.timesync），
            # 与上面"手机侧跨机偏移"是两回事 —— 分开记，不许合成一个数（见 clock.py）。
            "device": self.mouse.timesync_snapshot(),
            "mouse_features": dict(getattr(self.mouse, "_features", {}) or {}),
            "arena": self.arena.to_dict(),
            "locator": {"profile": self.cfg.get("locator.profile"),
                        "config": self.locator.fingerprint() if self.locator.available else None},
            "upstream": self.upstream(),
            "paths": self.paths,
        }
        s = self.store.open(session_id=session_id, notes=notes or [], snapshot=snapshot)
        sid = s.session_id
        self.bus.publish("session.phase", {"session_id": sid, "phase": s.phase,
                                           "op": "open", "step": "created"})
        self.bus.log("info", f"会话 {sid} 已建目录：{s.path}")
        try:
            # ① 鼠会话（本地目录，失败代价≈0 ⇒ 放最前，因为它最便宜）
            if self.mouse.available:
                try:
                    path = self.mouse.open_session(f"{sid}/mouse")
                    s.mouse_path = path
                    s.phase["mouse"] = "open"
                    done.append("mouse")
                    self.store.mark("mouse", "session.open", "", f"path={path}")
                except MouseError as e:
                    # 鼠没连上不该阻断整场记录：场地/采集/定位都是独立有用的。
                    s.phase["mouse"] = "failed"
                    s.detail["mouse"] = str(e)
                    self.bus.log("warn", f"鼠会话没开成（不阻断其余两路）：{e}")
            else:
                s.phase["mouse"] = "failed"
                s.detail["mouse"] = self.mouse.import_error or "模块不可用"

            # ② 手机采集（物理副作用：连拍 + 写盘 ⇒ 放在鼠之后）
            if self.camera.offline is not None:
                # 离线模式：不调 start，直接复用已导入的会话 + 回放
                sid_cam = self.camera.offline.get("session_id")
                s.phase["camera"] = "open"
                self.store.set_camera_ref({
                    "session_id": sid_cam, "data_dir": self.camera.offline.get("root"),
                    "offline": True, "store": None,
                    "note": "离线导入的本地会话（没有真实采集，为了演练）"})
                self.store.mark("camera", "offline_attached", str(sid_cam),
                                "离线会话导入：不调 hostlink.start()")
                self.camera.start_offline_replay(sid_cam,
                                                 rate_hz=float(self.cfg.get(
                                                     "camera.offline_rate_hz", 30.0)))
                done.append("camera")
            elif self.camera.available:
                try:
                    res = self.camera.start(
                        session_id="",
                        device_serial=str(self.cfg.get("camera.device_serial", "unknown")),
                        plan=dict(self.cfg.get("camera.default_plan") or {}))
                    sid_cam = res.get("session_id")
                    s.phase["camera"] = "open"
                    if int(res.get("sent_to") or 0) == 0:
                        self.bus.log("warn",
                                     "采集 start 已发出但无手机接收（sent_to=0）："
                                     "手机会话为空。请检查手机 App 是否在线、"
                                     "配对网址与 token 是否一致")
                    self.store.set_camera_ref({
                        "session_id": sid_cam, "data_dir": str(self.camera.data_dir() or ""),
                        "plan": res.get("plan"), "sent_to": res.get("sent_to"),
                        "store": None})
                    self.store.mark("camera", "session_started", str(sid_cam),
                                    json.dumps(res.get("plan") or {}, ensure_ascii=False))
                    done.append("camera")
                except CameraError as e:
                    s.phase["camera"] = "failed"
                    s.detail["camera"] = str(e)
                    raise
            else:
                s.phase["camera"] = "failed"
                s.detail["camera"] = self.camera.import_error or "模块不可用"

            # ③ 定位在线段
            if self.locator.available:
                self.locator.set_online(True)
                self.locator.start_online()
                s.phase["locate"] = "open"
                done.append("locate")
                self.store.mark("locate", "pipeline.ready", "",
                                f"profile={self.cfg.get('locator.profile')} "
                                f"queue={self.cfg.get('locator.online.queue_size')}")
            else:
                s.phase["locate"] = "failed"
                s.detail["locate"] = self.locator.import_error or "模块不可用"
        except Exception as e:                                # noqa: BLE001
            # ---- 回滚：把已成功的步骤逐个撤掉，然后**删掉会话目录**
            rolled: List[str] = []
            if "camera" in done:
                try:
                    self.camera.stop(s.get("_cam_sid") or "")
                    rolled.append("camera")
                except CameraError:
                    self.bus.log("warn", "回滚时停采集失败 —— 手机侧可能还在录，"
                                         "请手动在【采集】页点停止")
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
            self.locator.set_online(False)
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

    async def session_close(self, wait_delivery_s: float = 25.0,
                            auto_rerun: bool = True) -> dict:
        """收一次会话。顺序与开相反：**先停采集 → 等排空 → 再收鼠会话**（§5.3）。"""
        s = self.store.current
        if s is None:
            raise SessionError("当前无进行中的会话")
        warning: List[str] = []

        # ① 停采集
        if s.phase.get("camera") == "open":
            if self.camera.offline is not None:
                self.camera.stop_offline_replay()
                self.store.mark("camera", "offline_stopped", "", "离线回放停止")
            elif self.camera.available:
                try:
                    self.camera.stop(s.camera_ref.get("session_id") if s.camera_ref else "")
                except CameraError as e:
                    warning.append(f"停采集失败：{e}")
            s.phase["camera"] = "closed"

        # ② 等 delivery_done / 排空（超时也要继续，但**必须显式说出来**）
        if self.camera.offline is None and self.camera.available and s.camera_ref:
            sid_cam = s.camera_ref.get("session_id") or ""
            t0 = time.time()
            while time.time() - t0 < float(wait_delivery_s):
                try:
                    led = self.camera.ledger(sid_cam)
                except CameraError:
                    break
                if led.get("state") in ("stopped", "closed") or led.get("complete") is True:
                    break
                await asyncio.sleep(0.5)
            else:
                warning.append(
                    f"等待 {wait_delivery_s:.0f} s 未等到采集排空（账本 state 仍非 stopped）："
                    f"帧可能仍在传输，此时复算会把未落盘的帧判为坏帧。"
                    f"可稍后手动执行【复算本会话】。")
            try:
                led = self.camera.ledger(sid_cam)
                ref = dict(s.camera_ref or {})
                ref["store"] = led
                ref["frames_verified_on_pc"] = led.get("frames_verified_on_pc")
                s.camera_ref = ref
                self.store.set_camera_ref(ref)
            except CameraError as e:
                warning.append(f"读留存账本失败：{e}")

        # ③ 收鼠会话（它承载 mouse/ 与 locate/，所以必须最后收）
        if s.phase.get("mouse") == "open":
            try:
                self.mouse.close_session({"control_session": s.session_id})
            except MouseError as e:
                warning.append(f"收鼠会话失败：{e}")
            s.phase["mouse"] = "closed"

        # ④ 行数守恒自检（判据 I6）—— 对不上就当场说，不要等事后翻日志
        n_tl = self.store.count_rows("camera", "frame_verified")
        n_led = (s.camera_ref or {}).get("frames_verified_on_pc")
        if isinstance(n_led, int) and n_tl != n_led:
            warning.append(
                f"行数不守恒：timeline.csv 中 frame_verified {n_tl} 行，"
                f"相机账本报告 PC 校验 {n_led} 帧。差 {abs(n_tl - n_led)} 条，"
                f"说明有事件未记入时间轴（判据 I6）。")
        if self.locator.available:
            st = self.locator.stats()
            if st.get("dropped"):
                warning.append(
                    f"在线定位丢弃 {st['dropped']} 帧（有界队列满时丢最旧，不阻塞事件消费）。"
                    f"全量数据请用赛后批量。")

        self.locator.set_online(False)
        s.phase["locate"] = "closed"
        self.store.mark("session", "session.close", s.session_id,
                        "; ".join(warning) if warning else "无条件")
        closed = self.store.close(extra_meta={"warnings": warning} if warning else None)

        out: Dict[str, Any] = {"session": closed.to_dict() if closed else None,
                               "warnings": warning}
        if auto_rerun and closed is not None:
            try:
                # ⚠️ out_dir 必须显式传会话的 locate/：此刻 store 已经收掉了，
                # 不传就会落到 _orphan 里去（那份结果本该属于这个会话）。
                out["batch"] = self.trigger_batch(
                    closed.camera_ref.get("session_id") if closed.camera_ref else None,
                    out_dir=str(closed.locate_dir))
            except (CameraError, LocatorError) as e:
                warning.append(f"没启动赛后复算：{e}")
        self.bus.publish("session.phase", {"session_id": s.session_id,
                                           "phase": s.phase, "op": "close",
                                           "warnings": warning})
        return out

    # ------------------------------------------------------------ 赛后批量
    def trigger_batch(self, camera_session_id: Optional[str],
                      out_dir: Optional[str] = None) -> dict:
        """对某个手机会话跑全量复算（权威数据）。

        `out_dir` 必须由调用方在**会话已经收掉之后**显式传入（收会话的流程里我们会
        把 `closed.locate_dir` 传进来）—— 否则 `store.current` 已经是 None，
        结果会落到 `_orphan/` 里去，而那份结果**本该属于那个会话**。
        """
        if not self.locator.available:
            raise LocatorError(self.locator.import_error or "定位模块不可用")
        if not camera_session_id:
            raise CameraError("没有相机 session_id，不知道该复算哪个会话")
        root = self.camera.data_dir()
        if root is None:
            raise CameraError("拿不到相机 data_dir（相机服务不可用且没有导入离线会话）")
        src = root / str(camera_session_id)
        if not (src / "frames").is_dir():
            raise CameraError(f"本地找不到这个手机会话的帧目录：{src / 'frames'}")
        if out_dir is None:
            cur = self.store.current
            out_dir = str(cur.locate_dir) if cur is not None else None
        rid = self.locator.start_batch(str(src), out_dir=out_dir)
        self.store.mark("locate", "batch.start", camera_session_id,
                        f"run={rid} src={src} out={out_dir}")
        return {"run_id": rid, "src": str(src), "out": out_dir}

    # ------------------------------------------------------------ 场地标定落盘
    def save_arena(self, *, y_flip: bool, x_flip: bool, marker_offset_mm: List[float],
                   theta_offset_deg: float, camera_height_mm: Optional[float],
                   nadir_xy: Optional[List[float]], calibrated: bool,
                   note: str = "") -> dict:
        """把场地标定写回 **Control 自己的** config.yaml（逐行改，保注释）。

        ⚠️ **绝不写 `Auto Locate/config.yaml`**：那是上游仓库，改了 `git status` 就脏，
        判据 I9 会判否。相机高度与天底点改成**运行时注入**（`apply_params`），
        持久化放在我们自己的配置里。
        """
        self.arena.fw_to_arena.y_flip = bool(y_flip)
        self.arena.fw_to_arena.x_flip = bool(x_flip)
        self.arena.fw_to_arena.marker_offset_mm = (float(marker_offset_mm[0]),
                                                   float(marker_offset_mm[1]))
        self.arena.fw_to_arena.theta_offset_deg = float(theta_offset_deg)
        self.arena.calibrated = bool(calibrated)
        if camera_height_mm:
            self.arena.camera_height_mm = float(camera_height_mm)
        if nadir_xy:
            self.arena.nadir_xy = (float(nadir_xy[0]), float(nadir_xy[1]))

        vals = {
            "arena.fw_to_arena.y_flip": _y(self.arena.fw_to_arena.y_flip),
            "arena.fw_to_arena.x_flip": _y(self.arena.fw_to_arena.x_flip),
            "arena.fw_to_arena.marker_offset_mm":
                f"[{self.arena.fw_to_arena.marker_offset_mm[0]}, "
                f"{self.arena.fw_to_arena.marker_offset_mm[1]}]",
            "arena.calibrated": _y(self.arena.calibrated),
            "arena.camera_height_mm": (f"{self.arena.camera_height_mm}"
                                       if self.arena.camera_height_mm else "null"),
            "arena.nadir_xy": f"[{self.arena.nadir_xy[0]}, {self.arena.nadir_xy[1]}]",
        }
        res = self.cfg.patch(vals)
        # 相机高度/天底点**必须同时注入定位器**（运行时），否则标定了但不生效
        injected: List[str] = []
        if self.locator.available:
            try:
                self.locator.set_params({
                    "camera.parallax.camera_height_mm": self.arena.camera_height_mm,
                    "camera.parallax.nadir_xy": list(self.arena.nadir_xy),
                })
                injected = ["camera.parallax.camera_height_mm", "camera.parallax.nadir_xy"]
            except LocatorError as e:
                note = (note + f"\n注入定位器失败：{e}").strip()
        self.store.mark("arena", "calibrate", "", f"y_flip={y_flip} x_flip={x_flip} "
                                                  f"calibrated={calibrated} {note}")
        self.bus.publish("arena.changed", self.arena.to_dict())
        self.bus.log("info", f"场地标定已写回 config.yaml：{res}"
                             f"（注入定位器：{injected or '无'}）")
        return {"arena": self.arena.to_dict(), "patch": res, "injected": injected,
                "note": note}

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
    except (CameraError, LocatorError, MouseError) as e:
        return {"available": False, "error": str(e)}


def _y(v: Any) -> str:
    return "true" if v else "false"
