"""代启上游程序（PhoneCamera 的 PC 接收服务 `pc/server.py`）。

## 为什么需要它

之前要配对就得**手动**双击 `PhoneCamera/pc/start_service.cmd`：现场少点一下、或者把那个
控制台窗口关掉了，界面上就只剩一句"相机服务不可达"。用户明确要求把这步收进本层。

## 为什么不直接调那个 `.cmd`

`start_service.cmd` 会**开一个控制台窗口**、结尾还 `pause`（退出后等你按键）。从服务里
代启它等于凭空多一个窗口，而且进程树里隔了一层 `cmd.exe`，**Control 停不掉它**。
所以这里直接起 `python server.py`，参数从配置推导，stdout/stderr **落文件**
（纪律：子进程输出不许 PIPE 后不读 —— 管道写满会把子进程阻塞在自己日志上）。

## 三条硬纪律

1. **绝不在 `/health` 与后台探测里动手。** 探测线程只读快照；启动只能由**人按出来**
   （点【配对】/【连接】/【重探】/【启动上游服务】）。否则一个每 2.5 s 刷新的页面
   会不停拉起进程 —— 这类"看起来在工作，其实在制造垃圾"的行为最难发现。
2. **幂等**：已经可达就不起；本层起的那个还在跑就不重复起；端口被别的程序占了就**如实报**
   （不是"启动失败"，是"这儿已经有人了"，两者的下一步完全不同）。
3. **只停自己起的那个**（按 Popen 句柄校验）。别人起的服务不动它 —— 我们没有它的父进程关系，
   杀错了就是把人家的现场弄断。

## 与"文档里那些常量"的关系

上游 `start_service.cmd` 顶部写死了四个变量（PY / PORT / TOKEN / DATADIR / MAXMBPS）。
本模块**不读那个 .cmd**（它是给人看的），而是把同样的四项放进 `config.yaml` 的
`camera.launcher`，默认值对齐上游（端口从 `camera.base` 解析、token 沿用 `camera.token`、
数据目录取 `paths.phonecamera_pc/data`）。这样两边各自可读，且改一处不用去猜另一处。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .clock import now_ms

#: 上游 `server.py` 的默认值（`--max-mbps`；与 start_service.cmd 一致）
DEFAULT_MAX_MBPS = 40.0
#: 代启后等它可用的上限（秒）。真机实测 server.py 起来到能答 capabilities 是 1~3 s，
#: 留足余量；等不到就带着日志尾巴如实报失败，不假装成功。
DEFAULT_WAIT_S = 15.0
#: 轮询间隔（秒）
POLL_S = 0.4


def _tail(path: Path, n: int = 8) -> str:
    """读日志尾巴（失败时当证据用）。

    ⚠️ **两次解码尝试**：我们给子进程设了 `PYTHONIOENCODING=utf-8`，所以新日志是 UTF-8；
    但"用户手动双击 `.cmd` 起的那个"日志可能是 GBK。先严格 UTF-8、失败退 GBK、
    再不行才 `replace` —— 直接 `errors="replace"` 会把 GBK 内容读成一串 `����`，
    而**日志尾巴正是失败时唯一的证据**，读成乱码等于没有证据（真踩过）。
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4096))
            raw = f.read()
    except OSError:
        return ""
    text = ""
    for enc in ("utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", "replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


#: 子进程**必须**带上的环境变量。三个都各有一条"为什么"：
#:
#: · `PYTHONIOENCODING=utf-8` —— **这条是真机踩出来的**：上游 `pc/server.py` 的启动横幅里
#:   有 `▶ (U+25B6)` / `● / ▲` 这类字符，而 Windows 下 Python 的 stdout 在**重定向到文件**时
#:   用 locale 编码（简体中文机器就是 GBK）⇒ `print()` 当场抛 `UnicodeEncodeError`，
#:   进程 `exit 1`、服务根本没起来。表象是"代启了但没等到它可用：进程已退出（返回码 1）"，
#:   而真凶只在那行 Traceback 里（**上游源码不改**，那是它的 banner）。
#: · `PYTHONUTF8=1` —— 同一件事的更彻底版本（连文件系统编码一起）。
#: · `PYTHONUNBUFFERED=1` —— 否则子进程 print 的内容留在用户态缓冲里，
#:   日志文件是**空的** ⇒ 排查时"什么都没有"，而程序其实正在正常启动。
_CHILD_ENV = {
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
    "PYTHONUNBUFFERED": "1",
}


def _port_busy(host: str, port: int) -> bool:
    """端口上是否已经有东西在听。

    ⚠️ 只用来**区分两种失败**：服务没起（可以去起）vs 端口被别人占了（起了也白起）。
    所以它必须便宜且不依赖网络协议 —— 一次 `bind` 试探即可。
    对 `0.0.0.0` 的绑定测试在 Windows 上也能正确报 WSAEADDRINUSE。
    """
    s = socket.socket()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        s.bind((host or "127.0.0.1", int(port)))
        return False
    except OSError:
        return True
    finally:
        s.close()


class ServiceLauncher:
    """上游相机服务的代启器。**所有方法都可能阻塞** —— 调用方走线程池。"""

    def __init__(self, cfg: Any, bus: Any, store: Any) -> None:
        self.cfg = cfg
        self.bus = bus
        self.store = store
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._started_ms: Optional[int] = None
        self._start_error: str = ""
        #: **本次运行内**暂停代启（不写配置）。两种场景要它：
        #:   · 现场调试时"我自己管进程"，不想让 Control 插手；
        #:   · 自动化判据要跑"服务不可达"那条降级路 —— 而代启一旦生效，
        #:     那条路当场就没了，还会在**用户机器上留下一个进程**（真发生过）。
        self._paused: bool = False
        self.reload()

    def pause(self, on: bool) -> dict:
        """本次运行内暂停 / 恢复代启。**不改进 config.yaml** —— 它是临时状态。"""
        self._paused = bool(on)
        self.bus.log("info", f"上游相机程序的代启已{'暂停' if self._paused else '恢复'}"
                             f"（本次运行内，未改设置）")
        return {"paused": self._paused}

    # ------------------------------------------------------------ 规格
    def reload(self) -> None:
        """重新解析启动参数。改设置后调用（启动参数只在这里算一次，health 只读它）。"""
        c = self.cfg
        g = lambda k, d: c.get(f"camera.launcher.{k}", d)  # noqa: E731
        pc_dir = Path(str(c.get("paths.phonecamera_pc", "")))
        script = g("script", "") or str(pc_dir / "server.py")     # ←「上游程序」
        py = g("python", "") or sys.executable
        base = str(c.get("camera.base", "http://127.0.0.1:8765"))
        port = int(g("port", 0) or 0) or self.parse_port(base)
        token = str(g("token", "") or c.get("camera.token", "") or "")
        data_dir = g("data_dir", "") or str(pc_dir / "data")
        log = g("log", "") or ".tmp/phonecamera-service.log"
        host = str(g("host", "") or "")            # ""=用上游默认(0.0.0.0，手机要连得到)

        argv = [str(py), str(script), "--port", str(port)]
        if token:
            argv += ["--token", token]
        argv += ["--data-dir", str(data_dir)]
        argv += ["--max-mbps", str(float(g("max_mbps", DEFAULT_MAX_MBPS) or DEFAULT_MAX_MBPS))]
        if host:
            argv += ["--host", host]

        self._spec: Dict[str, Any] = {
            "enabled": bool(g("enabled", True)),
            "autostart": bool(g("autostart", True)),
            #: Control 收工时要不要**顺手把上游也停掉**。默认 false（见 `shutdown()`）。
            "stop_on_exit": bool(g("stop_on_exit", False)),
            "python": str(py),
            # `script` 是**配置里写的那个值**（空 = 用默认），给设置框回显用；
            # 真正要执行的是 `script_abs`。两者分开，免得"看一眼就点保存"把
            # 推导出来的绝对路径写进 config.yaml（那会让以后换机器全对不上）。
            "script": str(g("script", "") or ""),
            "script_abs": str(self._abs(script)),
            "script_ok": self._abs(script).is_file(),
            "python_ok": bool(shutil.which(str(py)) or self._abs(py).exists()),
            "port": port,
            "host": host or "0.0.0.0",
            "token_set": bool(token),
            "data_dir": str(data_dir),
            "max_mbps": float(g("max_mbps", DEFAULT_MAX_MBPS) or DEFAULT_MAX_MBPS),
            "log": str(self._abs(log)),
            "cwd": str(pc_dir if pc_dir.is_dir() else self._abs(script).parent),
            #: 给子进程注入的环境变量（界面/判据都要看得到"我们保证了什么"）。
            #: 尤其是 `PYTHONIOENCODING` —— 少了它，上游在 GBK 机器上**根本起不来**。
            "child_env": dict(_CHILD_ENV),
            "argv": argv,
        }

    @staticmethod
    def parse_port(base: str) -> int:
        """从 `http://host:8765` 里取出端口。取不到落回 8765（上游默认）。"""
        try:
            tail = str(base).split("//", 1)[-1]
            tail = tail.split("/", 1)[0]
            if ":" in tail:
                return int(tail.rsplit(":", 1)[1] or 8765)
        except (ValueError, IndexError):
            pass
        return 8765

    def _abs(self, p: str) -> Path:
        """程序/日志路径：相对则相对 Control/ 解析（与 `paths.*` 同一套规矩）。"""
        q = Path(str(p))
        if q.is_absolute():
            return q
        root = getattr(self.cfg, "root", None) or Path.cwd()
        return (Path(root) / q).resolve()

    def _argv_public(self) -> list:
        """给界面看的启动行 —— **token 用占位符**（凭据不进前端，R10）。"""
        out = []
        skip = False
        for a in self._spec["argv"]:
            if skip:
                out.append("••••")
                skip = False
                continue
            out.append(a)
            if a == "--token":
                skip = True
        return out

    # ------------------------------------------------------------ 状态（零副作用）
    def status(self, deep: bool = False) -> dict:
        """**不启动、不联网**地报告代启器现状。界面据此画开关与按钮。

        `deep=False`（`/health` 用）**连日志都不读**：那条路每 2.5 s 走一次，
        而 `/health` 的契约是"读快照"。要日志尾巴请走 `GET /camera/service`。
        """
        s = dict(self._spec)
        alive = self._proc is not None and self._proc.poll() is None
        by_us = alive
        s.update({
            "running": by_us,
            "by_us": by_us,
            "paused": self._paused,
            "pid": (self._proc.pid if by_us else None),
            "started_ms": (self._started_ms if by_us else None),
            "running_ms": ((now_ms() - self._started_ms) if by_us and self._started_ms else None),
            "last_error": self._start_error,
            "can_start": bool(s["enabled"] and s["script_ok"]),
            "can_stop": by_us,
        })
        if deep:
            s["log_exists"] = Path(s["log"]).is_file()
            s["log_tail"] = _tail(Path(s["log"]), 6)
            s["argv_display"] = self._argv_public()
        s.pop("argv", None)          # 真 argv 带 token：不进任何响应
        return s

    # ------------------------------------------------------------ 代启
    def start(self, is_up: Callable[[], bool], wait_s: float = DEFAULT_WAIT_S) -> dict:
        """必要时代启上游服务，并**等到它真的能答话**（或如实报失败）。

        `is_up` 由调用方给：它必须是**此刻的真探**（不是 health 快照）——
        快照最多旧 8 s，拿它做闸会重复起进程。
        """
        with self._lock:
            s = self._spec
            if self._paused:
                return self._fail(f"代启已暂停（本次运行内）。\n"
                                  f"  · 要恢复：POST /api/v1/camera/service/pause {{\"on\": false}}\n"
                                  f"  · 或手动启动 PhoneCamera/pc/start_service.cmd", s)
            if not s["enabled"]:
                return self._fail(f"自动启动已在设置里关闭。\n"
                                  f"  · 手动启动 PhoneCamera/pc/start_service.cmd\n"
                                  f"  · 或到相机窗口勾上「配对时自动启动」", s)
            if is_up():
                return {"ok": True, "started": False, "reason": "服务已在运行",
                        "pid": None, "waited_ms": 0, "log": s["log"], "port": s["port"]}
            if self._proc is not None and self._proc.poll() is None:
                # 本层起的那个还在启动中：不重复起，直接接着等
                proc, spawned = self._proc, False
            else:
                if not s["script_ok"]:
                    return self._fail(
                        f"找不到上游程序：{s['script_abs']}\n"
                        f"  · 到相机窗口的「上游程序」里填上 server.py 的路径\n"
                        f"  · 或修 config.yaml 的 camera.launcher.script", s)
                if _port_busy("127.0.0.1", s["port"]):
                    return self._fail(
                        f"端口 {s['port']} 已被别的程序占用（它不是相机服务）。\n"
                        f"  · 关掉占用它的程序，或把地址与端口改成另一对\n"
                        f"  · 也可手动启动上游服务并确认它监听的是同一端口", s)
                try:
                    proc = self._spawn()
                except Exception as e:                            # noqa: BLE001
                    return self._fail(f"代启失败：{type(e).__name__}: {e}", s)
                spawned = True
                self._proc = proc
                self._started_ms = now_ms()
            pid = proc.pid

        # 等待在锁外做：它可能持续十几秒，不该把别的话语全堵住
        t0 = time.monotonic()
        ok, why = self._wait_ready(proc, is_up, wait_s)
        waited = int((time.monotonic() - t0) * 1000)
        log = self._spec["log"]
        if not ok:
            msg = (f"代启了 {Path(self._spec['script_abs']).name}（PID {pid}）但没等到它可用：{why}")
            tail = _tail(Path(log), 8)
            alive = proc.poll() is None
            extra = ""
            if not alive:
                # 进程**自己退了**：上面那段是它的原始报错，不是我们的。
                # 说清楚"谁的问题"很重要 —— 否则现场会以为代启功能坏了。
                extra = ("\n  · 上游进程自己退出了：上面那段是它的原始报错（本层只负责起它）\n"
                         "  · 常见原因：它需要命令行参数/环境变量、依赖没装、端口被占")
                if "UnicodeEncodeError" in tail:
                    # 我们已给它设了 UTF-8，正常不该再出现；留着这条是为了自证"我们做了什么"。
                    extra += ("\n  · 若报的是 UnicodeEncodeError：上游在打印横幅时被控制台编码打断。"
                              "本层已为子进程设 PYTHONIOENCODING=utf-8 / PYTHONUTF8=1")
            elif not tail:
                # 子进程还活着、日志却是空的 —— 别把它当成"什么都没输出"：
                # Python 写文件是块缓冲（本层已加 PYTHONUNBUFFERED，这里只是兜底），
                # 也可能是"它监听的端口/地址与 camera.base 不一致"。
                extra = (f"\n  · 子进程仍在运行（日志为空多半是缓冲，不是没输出）\n"
                         f"  · 确认它的监听端口 {self._spec['port']} 与地址 "
                         f"{self.cfg.get('camera.base')} 一致\n"
                         f"  · 端口被别人先占了也会这样：先关掉占用者再试")
            self._start_error = msg + (f"\n{tail}" if tail else extra)
            self.bus.log("warn", msg)
            return {"ok": False, "started": spawned, "pid": pid, "reason": why,
                    "waited_ms": waited, "log": log, "port": self._spec["port"],
                    "tail": tail,
                    "detail": f"{msg}\n" + (f"{extra}\n" if extra else "")
                              + (f"  · 日志尾部：\n{tail}\n" if tail
                                 else f"  · 看日志：{log}\n")}
        self._start_error = ""
        note = (f"PID {pid} · 端口 {self._spec['port']} · {waited} ms 就绪")
        self.bus.log("info", f"已代启上游相机服务（{note}）")
        self.store.mark("camera", "camera.service.start", str(self._spec["port"]), note)
        return {"ok": True, "started": spawned, "pid": pid,
                "reason": "已启动" if spawned else "已在启动中（本次等到就绪）",
                "waited_ms": waited, "log": log, "port": self._spec["port"]}

    def _spawn(self) -> subprocess.Popen:
        s = self._spec
        log = Path(s["log"])
        log.parent.mkdir(parents=True, exist_ok=True)
        f = open(log, "ab")                     # 落文件：PIPE 不读会把子进程阻塞住
        try:
            kw: Dict[str, Any] = {}
            if os.name == "nt":
                # 不要弹控制台窗口（代启是后台行为，不是"给你开个终端"）
                kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            # **显式覆盖编码环境**：父进程（Control）可能跑在 GBK locale 下，
            # 子进程继承过去就会在打印自己的 banner 时崩掉（见 `_CHILD_ENV` 的说明）。
            # 这里是"覆盖"而不是"缺省才设" —— 上游那个 banner 是它自己的东西，我们不改，
            # 但可以保证它运行在一个能打印 UTF-8 的环境里。
            env = os.environ.copy()
            env.update(_CHILD_ENV)
            return subprocess.Popen(s["argv"], cwd=s["cwd"], env=env,
                                    stdin=subprocess.DEVNULL,
                                    stdout=f, stderr=subprocess.STDOUT, close_fds=True, **kw)
        finally:
            f.close()                           # 句柄已在子进程里复制一份，这里可以关

    def _wait_ready(self, proc: subprocess.Popen, is_up: Callable[[], bool],
                    wait_s: float) -> tuple:
        t0 = time.monotonic()
        while True:
            if is_up():
                return True, ""
            rc = proc.poll()
            if rc is not None:
                return False, f"进程已退出（返回码 {rc}）"
            if time.monotonic() - t0 >= wait_s:
                return False, f"{wait_s:.0f} s 内没起来"
            time.sleep(POLL_S)

    def _fail(self, msg: str, spec: dict) -> dict:
        self._start_error = msg
        self.bus.log("warn", f"代启上游相机服务未成：{msg.splitlines()[0]}")
        return {"ok": False, "started": False, "pid": None, "reason": msg.splitlines()[0],
                "detail": msg, "waited_ms": 0, "log": spec["log"], "port": spec["port"]}

    # ------------------------------------------------------------ 停止
    def stop(self, wait_s: float = 6.0) -> dict:
        """停掉**本层起的**那个。别处启动的服务不动（见模块 docstring 纪律 3）。"""
        with self._lock:
            p = self._proc
            if p is None or p.poll() is not None:
                self._proc = None
                return {"ok": False, "stopped": False, "pid": None,
                        "reason": "本层没有在跑的上游服务（可能是在别处启动的）",
                        "detail": ("本层只停自己起的进程。这个服务是别处启动的，"
                                   "请到它的窗口里停，或用任务管理器结束。")}
            pid = p.pid
            try:
                p.terminate()
                try:
                    p.wait(timeout=wait_s)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=3.0)
            except Exception as e:                                # noqa: BLE001
                return {"ok": False, "stopped": False, "pid": pid,
                        "reason": f"停止失败：{type(e).__name__}: {e}"}
            alive = p.poll() is None
            if alive:
                return {"ok": False, "stopped": False, "pid": pid,
                        "reason": f"发了停止信号但 PID {pid} 还活着",
                        "detail": f"请用任务管理器结束 PID {pid}。"}
            self._proc = None
            self._started_ms = None
            self.bus.log("info", f"已停止上游相机服务（PID {pid}）")
            self.store.mark("camera", "camera.service.stop", str(pid), "ok")
            return {"ok": True, "stopped": True, "pid": pid, "reason": "已停止"}

    # ------------------------------------------------------------ 收工
    def shutdown(self, stop: Optional[bool] = None) -> dict:
        """Control 收工（Ctrl+C / 关窗口）时，上游服务怎么处理。

        **默认留下不杀**（`camera.launcher.stop_on_exit: false`）。理由：手机连的是
        那个服务，而"服务"这个语义本身就要求它比控制端活得久 —— 现场经常是 Control
        重启一次、手机不该跟着断。杀了等于"我下班顺手把人家电源拔了"。

        但**留下必须说清楚**（谁起的、PID、怎么停、怎么改成自动停）：否则现场看到
        "Ctrl+C 之后还有个 python 在跑"，合理怀疑是进程泄漏 —— 那正是这条日志存在的理由。

        想让它跟着一起关：`camera.launcher.stop_on_exit: true`（或 `stop=True` 显式要求）。
        无论哪种，都**只关本层起的那个**；别处启动的服务一律不动。
        """
        do_stop = bool(self._spec.get("stop_on_exit", False)) if stop is None else bool(stop)
        with self._lock:
            p = self._proc
            if p is None or p.poll() is not None:
                return {"stopped": False, "pid": None, "by_setting": do_stop}
        if do_stop:
            r = self.stop()
            if r.get("ok"):
                self.bus.log("info", f"收尾：已按设置停掉本层代启的上游服务"
                                     f"（PID {r.get('pid')}，stop_on_exit=true）")
            return {"stopped": bool(r.get("ok")), "pid": r.get("pid"),
                    "by_setting": True, "detail": r}
        self.bus.log("info",
                     f"上游相机服务仍在运行（PID {p.pid}，由 Control 代启）—— "
                     f"手机还连着它，所以默认不停。要停：点相机窗口的【停止】"
                     f"（或结束该 PID）；想让它随 Control 一起停，把 "
                     f"camera.launcher.stop_on_exit 设为 true。")
        return {"stopped": False, "pid": p.pid, "by_setting": False}


def yaml_scalar(v: Any) -> str:
    """把设置值渲染成 YAML 标量。

    字符串一律用**双引号 + `json.dumps`**：Windows 路径里全是反斜杠，
    裸写会被 YAML 当成转义序列（`C:\\Users` 里的 `\\U` 直接报错）。
    双引号风格与 JSON 的转义规则一致，往返无损。
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return json.dumps(str(v), ensure_ascii=False)
