"""HostLink —— 给"整体架构里的其它程序"用的最小客户端库。

设计目标（docs/对外接口与单一交付道.md §2）：**三行接入**，且**只用标准库** ——
调用方往往是个临时分析脚本，"先装个 websocket 包"会把三行接入变成三分钟折腾。

    from hostlink import HostLink
    h = HostLink("http://127.0.0.1:8765", token="ab12cd34")
    h.start()                        # 等价于面板点「开始采集」
    print(h.status()["capture"])     # 机器可读状态

    for ev in h.events():            # 订阅广播（生成器）
        print(ev["type"], ev.get("seq"))

实时画面不必用本库（标准 MJPEG，任何能读 HTTP 的东西都能消费）：

    import cv2
    cap = cv2.VideoCapture(h.mjpeg_url())

**面板（控制台）能做的，这个库都能做** —— 一一对应关系由服务端
`/api/v1/capabilities` 的 `console.features` 声明，并由判据 `A8` 自动核对
（声明里的 `hostlink` 字段列的每个方法名都必须在本文件里真的定义）：

| 面板上做的事 | 本库 |
|---|---|
| 顶部环境行 + 会话表（3 秒刷新） | `overview()` / `sessions()` |
| ▶ 开始采集 / ■ 停止 | `start()` / `stop()` |
| 指令回执、状态变化的即时刷新 | `events()` |
| 监视道缩略图（实时画面） | `mjpeg_url()` / `thumb_url()` |
| 扫码配对（二维码 + 配对码 + 地址 + token） | `pair()` / `pair_url()` / `qr_png()` |
| 整机状态（面板的口径超集） | `status()` / `ledger()` |

⚠ 纪律（与接口本身同等重要）：
  · **实时画面是"看得见"，不是"算得准"** —— 有损 + 可丢。测量必须取交付道（`frames/`，**每一帧都留**、逐帧 sha256 已校验）；
  · `status()` 是**状态快照**，`events()` 是**通知**。真相以 `status()` 与落盘为准
    （订阅队列满会丢最旧，并回发 `events_dropped` 计数）；
  · 服务端可能比脚本老（缺某个接口）⇒ 先用 `capabilities()` 判断，不要硬编码假设。
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterator, Optional


class HostLinkError(RuntimeError):
    pass


# ---------------------------------------------------------------- 代理必须绕过
#
# 本库只用于访问**本机 / 局域网**上的这一个服务（接口面本身就是 loopback-only 的）。
# 但 `urllib` 默认会读 `HTTP_PROXY` / `http_proxy` / `HTTPS_PROXY` 环境变量 ——
# 现场机器上只要装了代理客户端（Clash / v2ray / 公司代理…），
# 对 `127.0.0.1:8765` 的请求就会**被塞进代理**，于是拿到一个和真实原因毫无关系的结果：
#
#     HTTP 502: upstream connect failed: 由于目标计算机积极拒绝，无法连接。(os error 10061)
#
# 服务其实好端端在跑。**排查方向会被彻底带偏**（看起来像"服务挂了/端口错了"）。
# 本项目实测踩到过（第 28 轮：`test_interop.py` 因 shell 里被注入了 `HTTP_PROXY`
# 而从 42/42 掉到 38/40，两条 FAIL 都指向代理返回的 502）。
# ⇒ 显式装一个**空代理表**的 opener：**行为只取决于地址，不取决于这台机器上装了什么软件**。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# --------------------------------------------------------------------- 最小 WS 客户端
#
# 为什么手写而不用 websocket-client：见模块 docstring。范围故意压到最小 ——
# 只做**客户端**、只读**文本帧**、不做分片重组（本项目的事件都是小 JSON，
# 远不到一帧装不下；真装不下时服务端会拆帧，而我们**必须报错而不是静默丢**）。
class _MiniWS:
    _OP_CONT, _OP_TEXT, _OP_BIN, _OP_CLOSE, _OP_PING, _OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA

    def __init__(self, url: str, timeout: float = 30.0, connect_timeout: float = 10.0):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("ws", "http", "https", "wss"):
            raise HostLinkError(f"只支持 ws:// 或 http://（收到 {u.scheme}://）")
        host = u.hostname or "127.0.0.1"
        port = u.port or 80
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        self.sock = socket.create_connection((host, port), timeout=connect_timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\n"
               f"Host: {host}:{port}\r\n"
               f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode("ascii"))
        # 读到 \r\n\r\n 为止（响应头之后**可能已经跟着帧数据**，所以要保留残包）
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise HostLinkError("握手时连接被关闭")
            buf += chunk
        head, self._rest = buf.split(b"\r\n\r\n", 1)
        first = head.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in first:
            raise HostLinkError(f"WS 握手失败：{first}")
        self._buf = self._rest
        self.closed = False

    # ---- 字节层 ----
    def _read_exact(self, n: int) -> bytes:
        out = self._buf
        self._buf = b""
        while len(out) < n:
            chunk = self.sock.recv(max(4096, n - len(out)))
            if not chunk:
                raise HostLinkError("连接已关闭")
            out += chunk
        out, self._buf = out[:n], out[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        # 客户端→服务端**必须**加掩码（RFC6455 5.3），否则服务端应关闭连接
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < (1 << 16):
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + masked)

    def recv_text(self) -> Optional[str]:
        """读一条文本消息；返回 None 表示对端正常关闭。"""
        while True:
            b0, b1 = self._read_exact(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else None
            payload = self._read_exact(n) if n else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == self._OP_CLOSE:
                self.closed = True
                return None
            if opcode == self._OP_PING:
                self._send_frame(self._OP_PONG, payload)
                continue
            if opcode == self._OP_PONG:
                continue
            if opcode == self._OP_CONT:
                # 分片消息：本项目的事件永远不会这么大。**显式报错**，不静默丢弃。
                raise HostLinkError("收到分片消息（CONT 帧）：本客户端只支持单帧文本消息")
            if not fin:
                raise HostLinkError("收到未结束（FIN=0）的文本帧：本客户端不支持分片")
            return payload.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self._send_frame(self._OP_CLOSE)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
        self.closed = True


class HostLink:
    def __init__(self, base: str, token: str, timeout: float = 10.0):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    # ---------------------------------------------------------------- 底层
    def _url(self, path: str, with_token: bool = True, force_scheme: str | None = None) -> str:
        url = self.base + path
        if force_scheme:
            url = force_scheme + "://" + url.split("://", 1)[1]
        if with_token:
            sep = "&" if "?" in url else "?"
            url += f"{sep}token={urllib.parse.quote(self.token)}"
        return url

    def _get_json(self, path: str, with_token: bool = True) -> dict:
        try:
            # ⚠ 用 `_OPENER`（空代理表）而不是 `urllib.request.urlopen`。
            # 原因见模块顶部 `_OPENER` 的注释：本库只服务**本机/局域网**上的这个服务，
            # 而 urllib 默认会吃 `HTTP_PROXY` 环境变量 ⇒ 装了代理/VPN 客户端的机器上，
            # `127.0.0.1:8765` 会被塞进代理，报一个和真实原因毫无关系的
            # `HTTP 502 upstream connect failed`。**行为只该取决于地址，不该取决于
            # 这台机器上装了什么软件。**
            with _OPENER.open(self._url(path, with_token), timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                j = json.loads(body)
                raise HostLinkError(f"HTTP {e.code} {j.get('error')}: {j.get('message')}")
            except json.JSONDecodeError:
                raise HostLinkError(f"HTTP {e.code}: {body[:200]}")
        except urllib.error.URLError as e:
            raise HostLinkError(f"连不上 {self.base}：{e.reason}（服务在跑吗？）")

    def _post_json(self, path: str, payload: Optional[dict] = None) -> dict:
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(self._url(path), data=data, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "X-Token": self.token})
        try:
            with _OPENER.open(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise HostLinkError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
        except urllib.error.URLError as e:
            raise HostLinkError(f"连不上 {self.base}：{e.reason}")

    # ---------------------------------------------------------------- 读
    def capabilities(self) -> dict:
        """能力声明。**无需 token**，且不含凭据 —— 启动时先读它。"""
        return self._get_json("/api/v1/capabilities", with_token=False)

    def status(self) -> dict:
        """机器可读状态快照。"""
        return self._get_json("/api/v1/host/status")

    def sessions(self) -> list:
        return self._get_json("/api/v1/sessions").get("sessions", [])

    def overview(self) -> dict:
        """面板**顶部环境行**的等价物（与面板同一次请求的数据，只是去掉会话数组）。

        返回 `ws_clients` / `profile_id` / `profile` / `data_dir` / `server_started_at`。

        为什么要单独留这个方法：`sessions()` 只取会话数组、把外层信封丢了，于是
        "面板顶部能看到的 profile 与数据目录，程序却拿不到" —— 而它俩恰恰是排查
        "我调的是不是同一台服务、同一份 profile"最直接的两个字段。
        """
        d = self._get_json("/api/v1/sessions")
        return {k: v for k, v in d.items() if k != "sessions"}

    def pair(self, host: str = "", port: int = 0) -> dict:
        """**配对的全部内容** —— 面板顶部"扫码配对"那一块的等价物。

        一次拿到三件套：**网址**（`url` + `hosts[]` 全部网卡候选）、**token**、
        **二维码**（`qr_svg` / `qr_matrix` / `qr_png_b64` 三形态，逐格一致）。

        ⚠ 返回体**含 token**（等同凭据）⇒ 别写进日志、别贴进 issue、别投屏。
        要落盘的场景用 `qr_png()` 而不是自己 `json.dump` 整个响应。

        `host` / `port` 不传时用服务端启动时钉住的**首选**地址；
        多网卡机器上首选未必对（VPN / 虚拟网卡），换一个试就传 `host=`。
        `hosts[]` 里 `recommended=True` 的那条与 `pair_uri` 一致。

        ⚠ 本接口需要 token —— 它返回的正是 token 本身。这是**唯一**要求 token 的读接口。
        """
        q = {}
        if host:
            q["host"] = host
        if port:
            q["port"] = str(int(port))
        path = "/api/v1/pair" + (("?" + urllib.parse.urlencode(q)) if q else "")
        return self._get_json(path)

    def pair_url(self) -> str:
        """**整行可粘贴的配对 URL**（`http://<ip>:<port>/?token=…`）。

        最常用的一个值：手机 App 的地址框、现场自检的浏览器地址栏用的都是它。
        ⚠ 与 `pair()['url']` 是同一个串，含 token —— 别当普通链接转出去。
        """
        return self.pair()["url"]

    def qr_png(self, path: str, host: str = "", port: int = 0) -> str:
        """把二维码存成 PNG 文件，返回落盘路径（供显示/打印/贴文档）。

        为什么要这个方法而不是让调用方自己解 base64：解码 + 写盘是每个调用方
        都要写一遍的三行样板，而且很容易漏掉"服务端可能给不出 PNG"
        （缺 Pillow）—— 那时 `qr_png_b64` 是 `None`，直接 `b64decode(None)` 会抛一个
        **与真实原因无关**的 TypeError。这里把它换成一条说清原因的 `HostLinkError`。

        落盘用途自己想清楚：**这张图里有 token**，等同于凭据。
        """
        d = self.pair(host, port)
        b64 = d.get("qr_png_b64")
        if not b64:
            fmts = d.get("qr_formats") or {}
            raise HostLinkError(
                "服务端没给 PNG 形态的二维码（miss={}）；"
                "可用 qr_svg / qr_matrix，或直接手输 pair_url()".format(
                    fmts.get("missing_deps") or "未知原因"))
        p = Path(path)
        p.write_bytes(base64.b64decode(b64))
        return str(p)

    def mjpeg_url(self) -> str:
        """实时画面地址（可直接喂给 cv2.VideoCapture / ffmpeg / <img>）。"""
        return self._url("/stream/mjpeg")

    def thumb_url(self, session_id: str = "", with_token: bool = False) -> str:
        """监视道**最新一张**（JPEG）的地址 —— 与面板轮询兜底用的是**同一条** URL。

        `session_id` 留空时自动用**最新**的那个会话（`sessions()` 已按时间倒序）；
        一个会话都没有时抛 `HostLinkError`（而不是编一个 URL 出来 —— 编出来的 URL
        看着正常、请求时 404，排查方向会被带偏）。

        `with_token=False` 是**故意的**：这个端点当前既不校验 token 也不限本机
        （见 capabilities.console.note 的已知开口），加上 token 只会让凭据扩散到更多
        日志里。要发给别的工具时可以传 `with_token=True`。
        """
        if not session_id:
            ss = self.sessions()
            if not ss:
                raise HostLinkError("还没有任何会话 ⇒ 没有缩略图可看；先 start() 或传 session_id")
            session_id = ss[0]["session_id"]
        return self._url(f"/api/v1/sessions/{urllib.parse.quote(session_id)}/thumb/latest",
                         with_token=with_token)

    def ledger(self, session_id: str = "") -> dict:
        """留存的完整性账本（**每一帧都留**：应有/实存/两个缺口/按 seq 区间点缺口）。

        ⚠ 第 24 轮**合并**后不再有独立的"归档清单"接口 —— 账本就在
        `/api/v1/host/status` 的 `store` 段里，`sessions()[i]["store"]` 也是同一份
        （同一个口径，避免"面板说好、接口说不清"）。

        返回**账本本身**（含 `complete` / `gaps_on_device` / `upload_gaps` /
        `missing_seqs`），不是外层信封 —— 调用方要的是这几个数。

        ⚠ `complete` 是**三态**：`null` = **判不了**（App 还没宣告应有帧数），
        不是通过。这是本项目的铁律：判不了 ≠ 没问题。
        """
        if session_id:
            for s in self.sessions():
                if s.get("session_id") == session_id:
                    return s.get("store") or {}
            return {}
        return self.status().get("store") or {}

    # ---------------------------------------------------------------- 控
    def start(self, session_id: str = "", device_serial: str = "unknown",
              plan: Optional[dict] = None, capture_mode: str = "") -> dict:
        """开始采集。`plan` 缺省 = **不抽帧**（第 24 轮语义：每一帧都留）。

        ⚠ 这里的默认值曾经是 `interval_ms: 200`（抽帧）—— 那是合并前的旧语义。
        本库**显式下发 plan**，所以服务端的默认值兜不住它：调用方用这个便利方法
        就会静默拿到"只留 1/6 的帧"，而 `status()` 里 `every_frame` 变 false。
        ⇒ 默认必须与 §capabilities 声明一致（`interval_ms: 0`），要降载自己传。

        `capture_mode`（第 27 轮）：留空 = 服务端默认 `deliver`（每一帧都留）；
        传 `"live"` 开**只看不留**模式（只实时回传、设备零留存、允许彩色）。
        ⚠ 面板上暂时**没有**这个开关 —— 它是"接口能做的、界面做不到"的那一类
        （见 `capabilities.console.beyond_console` 的 `live_mode`），
        所以这里显式给一个参数，别让调用方去手拼请求体。
        ⚠ `live` 跑完**不留任何东西**，误用不可恢复 ⇒ 传参前请确认这就是你要的。
        """
        body = {"session_id": session_id, "device_serial": device_serial,
                "plan": plan or {"mode": "interval", "interval_ms": 0, "count": 0}}
        if capture_mode:
            body["capture_mode"] = capture_mode
        return self._post_json("/api/v1/control/start", body)

    def stop(self, session_id: str = "") -> dict:
        return self._post_json("/api/v1/control/stop", {"session_id": session_id})

    # ---------------------------------------------------------------- 订阅
    def events(self, types: Optional[set] = None, reconnect: bool = True,
               reconnect_delay: float = 2.0, max_reconnects: int = 100
               ) -> Iterator[dict]:
        """订阅事件广播（生成器）。`types` 非空时只 yield 这些类型。

        **自动重连**：采集是长任务，WS 会因为 PC 重启 / 网络抖动断开。但重连**找不回**
        断开期间的事件 —— 所以断线后应当先 `status()` 对齐状态，再继续消费事件
        （事件是通知，状态才是真相）。重连时会 yield 一条
        `{"type": "_reconnected"}` 提醒调用方去对齐。
        """
        tries = 0
        while True:
            ws = None
            try:
                ws = _MiniWS(self._url("/api/v1/host/events", force_scheme="ws"),
                             timeout=max(30.0, self.timeout))
                tries = 0
                while True:
                    txt = ws.recv_text()
                    if txt is None:
                        break
                    try:
                        ev = json.loads(txt)
                    except json.JSONDecodeError:
                        continue          # 非法 JSON：跳过并继续（不是致命错误）
                    if types and ev.get("type") not in types:
                        continue
                    yield ev
            except HostLinkError:
                if not reconnect:
                    raise
            finally:
                if ws is not None:
                    ws.close()
            if not reconnect:
                return
            tries += 1
            if tries > max_reconnects:
                raise HostLinkError(f"事件订阅连续重连 {tries} 次失败，放弃")
            time.sleep(reconnect_delay)
            yield {"type": "_reconnected", "tries": tries,
                   "hint": "断线期间的事件找不回：请先调用 status() 对齐状态"}
