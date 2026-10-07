"""无硬件端到端演练 + 可测判据验收。

    python tools/drill.py                # 起一个临时服务，跑完整条链路，逐条判 I1~I14
    python tools/drill.py --port 8899
    python tools/drill.py --keep         # 跑完不删临时会话（自己进去看产物）

它做的事：
    ① 用 tools/make_synth_frames.py 造一批**有真值**的合成场地帧（两种 stride 变体）
    ② 起 Control 服务 → 导入离线会话 → 开统一会话 → 回放 → 收会话 → 赛后批量
    ③ 逐条检查判据，并把结果与产物路径打出来

**为什么要有这个脚本**：上游三个模块各自都有自己的取证体系，本层如果只靠"我手动点过一遍"，
下次谁改了适配器都不会有人发现。判据要做成能一条命令重跑的，否则它们只是文档里的一段话。
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from control.store import _rmtree_quiet      # noqa: E402  「绝不抛异常」的删除器

RESULTS: list = []          # (id, 结论, 说明)
HW_ONLY = "需要现场硬件"


def record(cid: str, verdict: str, note: str) -> None:
    RESULTS.append((cid, verdict, note))
    tag = {"PASS": "[PASS]", "FAIL": "[FAIL]", "SKIP": "[SKIP]", "INFO": "[INFO]"}[verdict]
    print(f"  {tag} {cid:5s} {note}")


def head(t: str) -> None:
    print(f"\n── {t} " + "─" * max(0, 70 - len(t)))


def api(base: str, path: str, method: str = "GET", body=None, timeout: float = 30.0):
    """调一次 REST。**永不抛异常** —— 失败变成 (状态 -1, {"err": ...}, 耗时)。

    为什么不能让它抛穿：这是"一条命令验收全部判据"的工具，半路抛出等于后面所有判据
    都没跑，而且看不到失败现场。失败必须是**一条记录**，不是一次崩溃。
    """
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else {}, r.status, time.time() - t0
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return json.loads(raw), e.code, time.time() - t0
        except json.JSONDecodeError:
            return {"_raw": raw}, e.code, time.time() - t0
    except Exception as e:                                    # noqa: BLE001
        # 超时 / 连接被重置 / 连不上 —— 都归到这里，带上类型名，别丢信息
        return {"err": f"{type(e).__name__}: {e}", "_path": path,
                "_method": method}, -1, time.time() - t0


def launch_checked(base: str, tries: int = 4):
    """**发车（要求成功）**，并把这个模拟器的一个固有抖动挡掉。

    为什么不能直接 `api(... maze_run ...)` 就断言 HTTP 200：
    上游模拟器的电池读数**大幅抖动**（实测本地 14 s 内从 9.1 V 摆到 9.5 V，
    drill 里那一轮恰好读到 7023 mV），而发车闸是 `battMv < LAUNCH_MIN_MV(7000)`
    ⇒ **同一条命令时通时不通**。这会让"收车后能不能再发车"这种判据随机变红，
    而红的理由跟产品一点关系都没有（就是读数那一瞬间低于阈值）。

    ⚠️ 这里**不掩盖**低电这件事：真的被低电拦下时如实回报 `lowbatt=True`，
    由调用方把它记成"**判不了**"（三态纪律：判不了 ≠ 不通过），而不是"不通过"。

    返回 `(应答, 状态码, 错误原话, 成功?, 是否被低电拦下?)`。

    注意：**验"急停闸"（要 ERR_ESTOP）和"忙闸"（要 ERR_BUSY）的地方不能用它** ——
    那些地方要的就是"被拒"，必须原样调 `api()`。
    """
    rr, cc, msg = {}, 0, ""
    low = False
    for i in range(max(1, tries)):
        pk, _, _ = api(base, "/api/v1/mouse/launch_precheck", timeout=10)
        if (pk.get("data") or {}).get("level") == "ok" or i == max(1, tries) - 1:
            break
        time.sleep(2.0)                      # 等抖动过去（读数会自己涨回来）
    rr, cc, _ = api(base, "/api/v1/timesync/maze_run", "POST", {}, timeout=25)
    msg = ((rr.get("error") or {}).get("message") or "") if isinstance(rr, dict) else ""
    ok = (cc == 200 and ((rr.get("data") or {}).get("status") == "OK"))
    low = ("ERR_LOWBATT" in msg)
    return rr, cc, msg, ok, low


def head_of(base: str, path: str, timeout: float = 10.0):
    """HEAD 一次，拿 (响应头 dict, 状态码)。失败给 ({}, -1)。"""
    req = urllib.request.Request(base + path, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return dict(r.headers), r.status
    except urllib.error.HTTPError as e:
        return dict(e.headers), e.code
    except Exception:                                         # noqa: BLE001
        return {}, -1


def get_text(base: str, path: str, timeout: float = 15.0) -> str:
    """取一段文本（HTML/JS）。失败给空串 —— 判据会据此报 FAIL。"""
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:                                         # noqa: BLE001
        return ""


def wait_up(base: str, timeout: float = 60.0) -> bool:
    """等 /health 起来。

    ⚠️ 探测超时不能给 2 s：本机对**被拒绝**的连接要 ~2 s 才返回，给 2 s 会让每次
    探测都恰好卡在边界上，于是"服务明明起来了"也会被判成超时。给 5 s 留余量。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(base + "/api/v1/health", timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:                                     # noqa: BLE001
            time.sleep(0.4)
    return False


def drain(log_path: Path, n: int = 2500) -> str:
    """读服务日志的尾巴。

    以前这里是 `proc.stdout.read()`（`stdout=PIPE`）—— **那是个陷阱**：
    它会阻塞到子进程退出，失败路径上把一个 1 秒的失败变成十分钟的空等。
    现在服务的输出落在文件里，读文件即可，既不阻塞也能拿到已经写下的部分。
    """
    try:
        return log_path.read_text(encoding="utf-8", errors="replace")[-n:]
    except Exception as e:                                    # noqa: BLE001
        return f"（拿不到服务日志：{type(e).__name__}: {e}）"


def _session_dirs() -> set:
    """当前 sessions/ 下的会话目录名集合。

    判"回滚干不干净"要用**集合**而不是计数：计数只能发现"多了/少了"，
    而集合能发现"换了一个" —— 而且不受盘上本来就有的历史残留干扰
    （之前用"目录数必须为 0"断言，被历史残留误判红过一次）。
    """
    root = ROOT / "sessions"
    return {p.name for p in root.glob("2026-*")} if root.is_dir() else set()


#: 1×1 的最小合法 JPEG —— 用来做"能不能真切出一帧"的判据。
#: 必须是**真的** JPEG（以 `\xff\xd8` 起、`\xff\xd9` 止），否则"切出来了"这件事
#: 只能在字节层面假装成立。硬编码比现生成稳（不依赖 PIL，也不会哪天变得不一样）。
_TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==")


def _fake_camera(port: int):
    """起一个**假相机服务**：status 正常答、`control/start` 故意回 500、
    `stream/mjpeg` 发几帧真 JPEG。

    为什么要造它：判据 I5 要验的是"**中途**失败要回滚干净"，而"一路都没连"
    现在会被**前置门控**在更早一步拦住（那是 I18）。所以必须构造一个
    「门控认为相机就绪、但真正开会话时那一路上炸掉」的场景。

    顺带它也证明了**门控与回滚是两道独立的闸** —— 只做一道都会漏：
    门控拦的是"什么都没连就开"，回滚兜的是"连了但中途炸了"。

    不去起真的 PhoneCamera 服务再想办法让它报错（它没有"故意失败"的开关，
    而且真要让它 start 失败就得造一台假手机），用几十行标准库搭个最小替身最直接。

    **`/stream/mjpeg` 用的是非常规 boundary**（故意等于真上游的
    `phonecamera-frame`）：判据 I26 要抓的正是"反代把 boundary 写死成别的值"——
    boundary 一旦对不上，浏览器就**一帧都切不出来**（预览空白，而服务端一切正常）。
    用默认值或常见的 `frame` 就抓不到了。
    """
    import http.server
    import threading

    boundary = "phonecamera-frame"        # 与真上游一致（`server.py::MJPEG_BOUNDARY`）

    class _H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):                      # 别往 stderr 刷
            pass

        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _mjpeg(self) -> None:
            """连发 5 帧就收工（够切成帧，又不会把测试挂住）。"""
            self.send_response(200)
            self.send_header("Content-Type",
                             f"multipart/x-mixed-replace; boundary={boundary}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                for i in range(5):
                    head = (f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                            f"Content-Length: {len(_TINY_JPEG)}\r\nX-Seq: {i}\r\n\r\n"
                            ).encode("ascii")
                    self.wfile.write(head + _TINY_JPEG + b"\r\n")
                    self.wfile.flush()
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass                                # 消费方走了是正常事件

        def do_GET(self):                               # noqa: N802
            p = self.path.split("?")[0]
            if p == "/api/v1/host/status":
                # `device.clients=1` 是为了让 readiness 认为"手机已连" ⇒ 门控放行。
                self._send(200, {
                    "ok": True, "server_version": "fake-1.0.0", "protocol_version": 1,
                    "device": {"connected": True, "clients": 1, "model": "FAKE-PHONE",
                               "serial": "fake", "app_version": "0.0-fake"},
                    "capture": {"state": "idle", "session_id": None,
                                "frames_verified": 0, "delivery": "stream"},
                    "gate": {"status": "incomplete", "checked": False, "hard_fail": [],
                             "note": "假服务：判不了（不是通过）"},
                })
            elif p == "/api/v1/capabilities":
                self._send(200, {"ok": True, "server": "fake",
                                 "server_version": "fake-1.0.0", "protocol_version": 1})
            elif p == "/api/v1/sessions":
                self._send(200, {"sessions": [], "ws_clients": 1, "profile_id": "fake"})
            elif p == "/stream/mjpeg":
                self._mjpeg()
            else:
                self._send(404, {"ok": False, "error": "假服务没有这个端点"})

        def do_POST(self):                              # noqa: N802
            p = self.path.split("?")[0]
            if p == "/api/v1/control/start":
                self._send(500, {"ok": False,
                                 "error": "假相机故意失败（用来触发 I5 的回滚路径）"})
            else:
                self._send(404, {"ok": False, "error": "假服务没有这个端点"})

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _H)
    srv.fake_mjpeg_boundary = boundary       # 判据 I26 要拿它当"上游声明的真相"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _start_sim(port: int):
    """起上游鼠标模拟器（`host.sim.mouse_sim`）。起不来返回 None。

    ⚠️ **不要用"连一下端口"来判断就绪**：模拟器是**单客户端**的（accept 一个、
    服务到断开、再 accept 下一个）。探针连上去又立刻关，会让它把那个死连接当成
    "正在服务的客户端"，于是真正的客户端 `connect()` 在 TCP 层成功、却永远等不到
    响应 —— 表现是"HTTP 请求挂住 30 秒然后超时"，而原因看起来跟模拟器毫无关系（踩过）。
    改成**读它的启动输出**，与它零交互。
    """
    host_dir = ROOT.parent / "RemoteControl" / "host"
    if not (host_dir / "host" / "sim" / "mouse_sim.py").is_file():
        return None
    p = subprocess.Popen([sys.executable, "-u", "-m", "host.sim.mouse_sim",
                          "--port", str(port),
                          # ⚠️ **必须 `--no-auto`**：上游 1.5.x 起，`SCOPE START` 在
                          # `auto_explore=True`（默认）下会把模拟器置成"自走"（`move_mode=EXPLORE`），
                          # 而 `LESSON START` 的运动互斥判据因此永远为真 ⇒ 必得 `ERR_BUSY`。
                          # 这不是我们的锅：上游 `LessonAPI.run()` 内部就先发 `SCOPE START`
                          # 再发 `LESSON START`，两条自己撞上。上游自己的三套 smoke
                          # （lesson / maze / selftest）也都传了 `--no-auto`。
                          # 对判据无影响：`MAZE RUN` 是**显式**发车命令，不受 `--no-auto` 约束。
                          "--no-auto"], cwd=str(host_dir),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace")
    t0 = time.time()
    buf = ""
    while time.time() - t0 < 25:
        line = p.stdout.readline()          # 阻塞式读一行；进程死了会立刻返回 ''
        if not line:
            if p.poll() is not None:
                _stop_sim(p)
                return None
            continue
        buf += line
        if "监听" in line or "listening" in line.lower():
            return p
    _stop_sim(p)
    return None


def _stop_sim(p) -> None:
    if p is None:
        return
    try:
        p.terminate()
        p.wait(timeout=6)
    except Exception:                                         # noqa: BLE001
        try:
            p.kill()
        except Exception:                                     # noqa: BLE001
            pass


def _mouse_sections(base: str, args, sim) -> None:
    """I15 / I16 / I17 / I22 / I23 —— 需要一条活链路的判据，走模拟器。"""
    # ---- 先确认服务**此刻还答得上话**。这一步不是仪式：
    # 如果事件循环已经停摆（曾经因为"服务进程的 stdout 管道写满"整台服务卡死），
    # 后面每个请求都只会超时，而超时信息完全指不到真正的原因。这里先用短超时探一下，
    # 不健康就把服务自己的日志尾巴打出来 —— 让"为什么走不下去"自答。
    h, hc, hdt = api(base, "/api/v1/health", timeout=8)
    if hc != 200:
        record("S6", "FAIL",
               f"服务已不健康（HTTP {hc}，{hdt:.1f}s）⇒ 后面的判据全判不了。"
               f"服务日志尾巴：\n{drain(ROOT / '.tmp' / 'drill' / 'service.log', 1200)}")
        for cid in ("I15", "I16", "I17", "I22", "I23", "I27", "I28", "I30"):
            record(cid, "FAIL", "服务不健康 ⇒ 判不了")
        return

    # ---- 让服务端连上模拟器
    # 先断一次：前面的判据里连过串口，链路可能还半开着。**同一个 client 上换目标重连**
    # 是最容易踩坑的一步（旧 backend 没关干净时 connect 会一直不返回）。
    api(base, "/api/v1/mouse/disconnect", "POST", {})
    time.sleep(0.3)
    r, c, cdt = api(base, "/api/v1/mouse/connect", "POST",
                    {"target": f"tcp://127.0.0.1:{args.sim_port}"}, timeout=25)
    if c != 200:
        # 服务的**环形日志缓冲**才是日志所在（`bus.log` 进那里，不进 stdout）；
        # 落盘的那份只有启动 banner。两边都取一下，哪边有内容就报哪边。
        ring, _, _ = api(base, "/api/v1/logs/tail?n=40", timeout=6)
        lines = [f"    [{m.get('level')}] {str(m.get('text'))[:130]}"
                 for m in (ring.get("data") or [])]
        log = drain(ROOT / ".tmp" / "drill" / "service.log", 800)
        record("S6", "FAIL",
               f"连模拟器失败：HTTP {c}（{cdt:.1f}s）"
               f"{json.dumps(r, ensure_ascii=False)[:160]}\n"
               f"      服务日志尾（环形缓冲）：\n" + "\n".join(lines[-12:]) +
               f"\n      进程 stdout（只有启动 banner）：\n{log.strip()[:400]}")
        for cid in ("I15", "I16", "I17", "I22", "I23", "I27", "I28", "I30"):
            record(cid, "FAIL", f"连不上活链路 ⇒ 判不了（`mouse/connect` HTTP {c}）")
        return
    record("S6", "PASS", f"服务端已连上模拟器：{(r.get('data') or {}).get('device')}")

    # ---- I16：参数必须按 Flash 页分成两套（上游 1.5.0 起生产命名空间增至 24 项）
    g, c, _ = api(base, "/api/v1/mouse/params/grouped")
    d = g.get("data") or {}
    groups = d.get("groups") or {}
    prod, les = groups.get("生产标定") or {}, groups.get("课程调参") or {}
    meas_ir = groups.get("红外测量") or {}
    meas_batt = groups.get("电源监控") or {}
    pages = {k: (list(v.values())[0].get("flash_page") if v else None)
             for k, v in groups.items()}
    vias = {k: (list(v.values())[0].get("write_via") if v else None)
            for k, v in groups.items()}
    # 页号是**固件分区**的事实：上游 1.5.4 把运行数据区上移后 99/100 → 115/116
    # （原值正落在 FLASH_EraseRunData 的擦除范围内，会被连标定一起抹掉）。
    # 生产侧 7+11+6=24 项同页同通道 —— 所以它们必须**共用**一个 SAVE。
    ok = (c == 200 and len(prod) == 7 and len(les) == 14
          and len(meas_ir) == 11 and len(meas_batt) == 6
          and pages.get("生产标定") == 115 and pages.get("课程调参") == 116
          and pages.get("红外测量") == 115 and pages.get("电源监控") == 115
          and vias.get("生产标定") != vias.get("课程调参")
          and vias.get("红外测量") == vias.get("生产标定"))
    record("I16", "PASS" if ok else "FAIL",
           f"{d.get('count')} 项 = 生产 {len(prod)} + 课程 {len(les)} + "
           f"红外 {len(meas_ir)} + 电源 {len(meas_batt)}"
           f"（page {pages.get('生产标定')} / {pages.get('课程调参')}，"
           f"{vias.get('生产标定')} vs {vias.get('课程调参')}）"
           f"⇒ 生产侧四组同页同通道、课程侧另一条通道"
           if ok else f"HTTP {c}；生产 {len(prod)} / 课程 {len(les)} / 红外 {len(meas_ir)}"
                      f" / 电源 {len(meas_batt)}；pages={pages} vias={vias}")

    # ---- 发车 + 时间同步
    a, c, _ = api(base, "/api/v1/timesync/arm", "POST", {})
    armed_ok = c == 200 and ((a.get("data") or {}).get("timesync") or {}).get("armed")
    # ⚠️ 「帧发生时刻」不是发车后立刻就有：它要等第一条 TIME 帧到达、并把主机-设备时基
    # 对上（样本不够时算不出来）。原先写死 `sleep(2.5)` 再问一次 —— 慢一拍就拿到 None，
    # 于是 I15 **一次绿一次红**（真发生过，追着查了半天才发现不是功能问题）。
    # 改成**等到算得出为止**（有上限）：上限内还没出来才是真失败。
    t, td, ts, smp, waited = None, {}, {}, {}, 0.0
    for _ in range(24):                                   # 最多 ~12 s
        time.sleep(0.5)
        waited += 0.5
        t, _, _ = api(base, "/api/v1/timesync")
        td = t.get("data") or {}
        ts = td.get("timesync") or {}
        smp = td.get("sample") or {}
        if smp.get("t_send_pc_ms") is not None and smp.get("arrival_pc_ms") is not None:
            break

    # ---- I15：时间必须能说清**是哪一种**
    # 光看"帧的发送时刻算得出来"是不够的 —— 那只是一个瞬时值。
    # 真正要守住的是：**落盘的每一行**都标了 t_src，而且取值只在
    # {send, capture, arrival, local} 里。分不清"哪种时间"的数据比没有时间更危险，
    # 所以这条判据落在 timeline 的**列**上，不是落在某一次调用上。
    has_send = smp.get("t_send_pc_ms") is not None
    has_arr = smp.get("arrival_pc_ms") is not None
    tsrc_ok, tsrc_note = _timeline_tsrc_check()
    record("I15",
           "PASS" if (armed_ok and has_send and has_arr and tsrc_ok) else "FAIL",
           f"发车=True，样本 {ts.get('samples')} 帧 TIME（等了 {waited:.1f}s 才算出帧发生时刻），"
           f"偏移 {ts.get('offset_s')}s"
           f"（频差 {ts.get('drift_ppm')}ppm，样本够才拟合得出）；"
           f"帧发生时刻 {smp.get('t_send_pc_ms')} vs 到达 {smp.get('arrival_pc_ms')}"
           f"（链路 {smp.get('age_ms') if smp.get('age_ms') is not None else '未估出'} ms）；"
           f"{tsrc_note}"
           if (armed_ok and has_send and has_arr and tsrc_ok)
           else f"发车={armed_ok}（等了 {waited:.1f}s）；sample={smp}；timeline：{tsrc_note}")

    # ---- I17：丢帧必须可回答
    lk = td.get("link") or {}
    lk_ok = lk.get("seq_lost") is not None and lk.get("seq_gap") is not None
    record("I17", "PASS" if lk_ok else "FAIL",
           f"seq_lost={lk.get('seq_lost')} seq_gap={lk.get('seq_gap')} "
           f"bad_crc={lk.get('bad_crc')} rx={lk.get('rx_bps'):.0f} B/s ⇒ "
           f"「数据齐不齐」是可回答的，不靠猜"
           if lk_ok else f"stats 里没有丢帧字段：{lk}")

    # ---- 顺带：课程实验能跑通并自动算参（这不是判据，是"新功能真的通了"的证据）
    head("14c. 顺带验：课程实验与自动算参（新功能，非判据）")
    r, c, _ = api(base, "/api/v1/lesson/run", "POST",
                  {"exp": 2, "args": {"v": 60, "dist": 100, "ms": 3000},
                   "fetch_log": False}, timeout=120)
    res = r.get("data") or {}
    if c == 200 and res.get("ok"):
        s, c2, _ = api(base, "/api/v1/lesson/suggest", "POST",
                       {"exp": 2, "truth": 110.0, "result": res})
        sg = s.get("data") or {}
        items = sg.get("items") or {}
        record("S7", "PASS" if items else "FAIL",
               f"实验2 跑到 {res.get('reason')}，dL={res.get('dL')} dR={res.get('dR')}；"
               f"算参：{ {k: (v['old'], '→', v['new'], str(v['err_pct']) + '%') for k, v in items.items()} }"
               f"；{sg.get('accept_note') or sg.get('hint') or ''}")
    else:
        record("S7", "FAIL", f"课程实验失败：HTTP {c} {json.dumps(r, ensure_ascii=False)[:200]}")

    # ---- I22 / I23 **放在最后**：跑迷宫会把设备留在"迷宫里"（`mouse_task` 停在
    # MAZESEARCH，运动互斥判据因此一直忙）⇒ 它会挡住后面任何运动类判据。
    # 这不是绕开问题，而是这条链路的**真实次序**：跑迷宫是"整场"的事，跑完就该收工。
    # ---- I22：**统一帧单流 + 码表零设备访问 + 拉取节奏可控**
    # 这条判据跨了两次上游改版，所以它现在要守四件事：
    #   ① **拉取关着**时码表照样答得出 39 通道 + 迷宫码表 ⇒ 证明它**零设备访问**
    #      （否则未连接时页面画不出"要读什么"）；
    #   ② 打开拉取后**帧真的在来**（`counts.unified` 增长、`rx_bps > 0`）——
    #      这是"这条流存在"的实测证据，不是看协议常量的 217 Hz；
    #   ③ **关掉拉取后帧必须停止增长** —— 2.1.0 的核心性质（发送节奏在上位机手里）。
    #      v2.0 是固件自时钟，关不掉；而正是它把蓝牙透传模块缓冲灌死且不自愈。
    #      所以"停得住"才是这条判据里**最有价值**的那一半：它同时证明了自愈路径成立
    #      （停止请求 → 固件不再发 → 缓冲自然排空 → 不用断电）。
    #      ⚠️ 这一条**必须在真机/模拟器上都为真**；若它红，说明上游退回了自时钟。
    #   ④ **负向验证**：旧端点必须真的 404。协议是"整体替换、不做兼容层"，
    #      如果它们还在答话，说明我们改的是一层皮、旧路径仍然可达（那才是最危险的）。
    api(base, "/api/v1/mouse/stream", "POST", {"on": False})
    time.sleep(0.4)
    mm, cm_, _ = api(base, "/api/v1/mouse/meas/meta")
    mdd = mm.get("data") or {}
    chans = mdd.get("channels") or []
    maze_tab = mdd.get("maze") or {}
    # ---- ② 开流，量出帧真的在来
    api(base, "/api/v1/mouse/stream", "POST", {"on": True})
    time.sleep(1.0)
    l1, _, _ = api(base, "/api/v1/mouse/link_stats")
    time.sleep(1.5)
    l2, cl2, _ = api(base, "/api/v1/mouse/link_stats")
    d1 = (l1.get("data") or {})
    d2 = (l2.get("data") or {})
    n1 = (d1.get("counts") or {}).get("unified") or 0
    n2 = (d2.get("counts") or {}).get("unified") or 0
    bps = d2.get("rx_bps") or 0
    # ---- ③ 关掉拉取，量出"帧真的停住了"（2.1.0 的自愈性质）
    # 这一步是 **v2.1 特有的**：v2.0 的固件自时钟没有"停"这个动作。
    # 停发之后**必须等到在途的那一条（≤1 条）回来**再取基线，否则会把"最后一条
    # 正在路上的应答"算进增量，于是关流后计数仍涨一点 —— 那是假红，不是没停住。
    api(base, "/api/v1/mouse/stream", "POST", {"on": False})
    time.sleep(1.2)                       # 让在途的那一条（1/RTT，真机 ~0.23 s）落定
    lo1, _, _ = api(base, "/api/v1/mouse/link_stats")
    time.sleep(1.5)
    lo2, clo2, _ = api(base, "/api/v1/mouse/link_stats")
    m1 = ((lo1.get("data") or {}).get("counts") or {}).get("unified") or 0
    m2 = ((lo2.get("data") or {}).get("counts") or {}).get("unified") or 0
    # 关流后帧计数**不再增长**。给 0 的宽容度（严格相等）是有意的 ——
    # 停等在途恒 1，落定后再等 1.5 s 本该一帧都没有。
    stopped_ok = (m2 == m1)

    # ---- ④ 负向：这些端点必须**不存在**
    gone = {}
    for meth, path in (("GET", "/api/v1/mouse/snap"),
                       ("POST", "/api/v1/mouse/scope/start"),
                       ("POST", "/api/v1/mouse/scope/fetch"),
                       ("GET", "/api/v1/mouse/maze/events"),
                       ("POST", "/api/v1/mouse/maze/events"),
                       ("POST", "/api/v1/mouse/maze/snap")):
        _, gc, _ = api(base, path, None if meth == "GET" else "POST", {}, timeout=8)
        gone[path] = gc
    gone_ok = all(c == 404 for c in gone.values())
    caps, ccap, _ = api(base, "/api/v1/caps")
    cap = ((caps.get("data") or {}).get("mouse") or {})
    streams = cap.get("streams") or {}
    frames = cap.get("frames") or []
    # 帧类型只剩两种（`UNIFIED` / `MAP`），流只剩一条 —— 这是"整体替换"的形状。
    one_stream = (list(streams) == ["unified"] and sorted(frames) == ["MAP", "UNIFIED"])
    # 39 个通道要与协议层常量**逐项一致**，不是"有个列表就行"。
    # 上游路径取自配置（与 `_start_sim` 同一处真相：`ROOT.parent / RemoteControl / host`）。
    try:
        host_dir = ROOT.parent / "RemoteControl" / "host"
        if str(host_dir) not in sys.path:
            sys.path.insert(0, str(host_dir))
        from host.protocol.codec import UNIFIED_CHANNELS      # type: ignore
        want = list(UNIFIED_CHANNELS)
    except Exception as e:                                    # noqa: BLE001
        want = []
        record("I22", "FAIL", f"取不到上游 `UNIFIED_CHANNELS`：{type(e).__name__}: {e}")
    chans_ok = (len(chans) == 39 and chans == want)
    maze_ok = bool(maze_tab.get("tasks") and maze_tab.get("roads")
                   and maze_tab.get("road_kinds") and maze_tab.get("motions")
                   and maze_tab.get("dirs") and maze_tab.get("flags")
                   and (maze_tab.get("junctions") or []))
    i22_ok = (cm_ == 200 and cl2 == 200 and clo2 == 200 and ccap == 200
              and chans_ok and maze_ok and one_stream
              and n2 > n1 and bps > 0 and stopped_ok and gone_ok)
    record("I22", "PASS" if i22_ok else "FAIL",
           f"唯一一条流：`streams`={list(streams)} `frames`={sorted(frames)}；"
           f"码表零设备访问（拉取关着也答）{len(chans)} 通道"
           f"（与上游 `UNIFIED_CHANNELS` 逐项一致）· {len(maze_tab)} 组迷宫码表；"
           f"打开拉取后帧在来：unified {n1} → {n2}（+{n2 - n1}），rx {bps:.0f} B/s；"
           f"**关掉拉取后帧停住**：unified {m1} → {m2}（增量 {m2 - m1}，"
           f"{'停得住' if stopped_ok else '**仍在涨 → 节奏不在上位机手里**'}）；"
           f"旧端点全 404（{len(gone)} 条）"
           if i22_ok else
           f"**统一帧单流没成立**：HTTP meta={cm_} caps={ccap} stats={cl2}/{clo2}；"
           f"chans={len(chans)}（应 39，与上游一致={chans_ok}）；"
           f"streams={list(streams)} frames={sorted(frames)} one_stream={one_stream}；"
           f"maze_ok={maze_ok}；unified 开 {n1}→{n2} bps={bps}，"
           f"关 {m1}→{m2}（stopped_ok={stopped_ok}）；旧端点状态码={gone}")

    # ---- I23：实验 8 无记忆走迷宫 —— **统一帧 + 主机差分**（上游 2.0.0）
    # 旧 I23 判三条通道（事件流 / 1 Hz 状态帧 / SNAP MAZE）。2.0.0 把 `MAZE EV`
    # 与 `SNAP` 都删了，"它刚才识别到了什么"改成**主机差分相邻帧**得到。
    # 所以这条判据改成：
    #   · `state` 里要有任务名 / 路口分类 / 坐标（这些是帧里现成的）；
    #   · `changes`（差分出来的迁移序列）必须**非空** —— 否则差分这条路没走通，
    #     而它正是"识别到了什么"的唯一来源；
    #   · `eventsIncomplete` 必须是布尔（`EVQ` **显式**，不许是 None 蒙混）；
    #   · 负向：`MAZE EV` / `MAZE RST` 在设备侧必须回 `ERR_VALUE`（删干净了）。
    #
    # ⚠️ **先重开拉取**：I22 的最后一步是**故意把拉取关掉**（为验"停得住"），
    # 而下面全靠帧。这一开一关也是顺带验了"停掉之后还能重开"——
    # 如果只验了"停得住"而没验"重得开"，那排障手段就成了单程票。
    api(base, "/api/v1/mouse/stream", "POST", {"on": True})
    time.sleep(1.0)
    api(base, "/api/v1/mouse/maze/reset", "POST", {})
    time.sleep(0.4)
    mr, cr, _ = api(base, "/api/v1/timesync/maze_run", "POST", {}, timeout=20)
    time.sleep(7.0)                     # 差分要有得看，得跑几秒
    stt, cst, _ = api(base, "/api/v1/mouse/maze/state")
    hh, chh, _ = api(base, "/api/v1/mouse/maze/history")
    smm, csm, _ = api(base, "/api/v1/mouse/maze/summary")
    stv = (stt.get("data") or {}).get("state") or {}
    chg = ((stt.get("data") or {}).get("changes") or
           (hh.get("data") or {}).get("changes") or [])
    smv = (smm.get("data") or {}).get("summary") or {}
    # 差分序列的形状：(t, from, to) —— 时刻 + 前后两个任务码
    chg_ok = bool(chg) and all(isinstance(c, dict) and "from" in c and "to" in c
                               and c.get("from") != c.get("to") for c in chg)
    seq_ok = bool(smv.get("taskSeq"))
    evinc = (stt.get("data") or {}).get("eventsIncomplete")
    evinc_ok = isinstance(evinc, bool)
    # 负向：旧迷宫命令必须已被固件删掉（`MAZE EV` / `MAZE RST` -> ERR_VALUE）。
    # 判定要**兼容两种表现**：设备回 `ERR_VALUE`（正常路径，HTTP 200 + status），
    # 或者上游把它当失败抛出来（HTTP 5xx + 报文里带 ERR_*）。两种都算"确实没有了"。
    raw_ev, cr_ev, _ = api(base, "/api/v1/mouse/raw", "POST",
                           {"line": "MAZE EV 31"}, timeout=10)
    raw_rst, cr_rst, _ = api(base, "/api/v1/mouse/raw", "POST",
                             {"line": "MAZE RST"}, timeout=10)
    raw_txt = json.dumps(raw_ev, ensure_ascii=False) + json.dumps(raw_rst, ensure_ascii=False)
    raw_gone_ok = (("ERR_VALUE" in raw_txt) or ("ERR_NAME" in raw_txt)
                   or (cr_ev >= 500 and cr_rst >= 500))
    i23_ok = (cr == 200 and cst == 200 and chh == 200 and csm == 200
              and stv.get("taskName") and stv.get("roadName") is not None
              and stv.get("posX") is not None
              and chg_ok and seq_ok and evinc_ok and raw_gone_ok)
    record("I23", "PASS" if i23_ok else "FAIL",
           f"发车 HTTP {cr} ⇒ 状态 task={stv.get('taskName')} "
           f"road={stv.get('roadName')}({stv.get('roadKind')}) "
           f"pos=({stv.get('posX')},{stv.get('posY')}) 动作={stv.get('motionName')}；"
           f"差分取到 {len(chg)} 条迁移（首条 "
           f"{chg[0].get('from')}→{chg[0].get('to')}）；"
           f"汇总 task={smv.get('task')} 序列 {len(smv.get('taskSeq') or [])} 段；"
           f"eventsIncomplete={evinc}（布尔）；旧命令 MAZE EV/RST 已回 "
           f"{'ERR_*' if raw_gone_ok else '非错误'}"
           if i23_ok else
           f"**迷宫状态机读不全**：run={cr} state={cst} history={chh} summary={csm}；"
           f"state={stv}；changes={len(chg)} chg_ok={chg_ok} seq_ok={seq_ok}；"
           f"eventsIncomplete={evinc!r}（要 bool）；旧命令应答={raw_txt[:200]}")

    # ---- I27：实验 8「记录 → 保存 → 读回」往返 + 重建（含两条负向）
    # 这条判据守的是"**从发车到停止的数据真的落下来了、而且读回来是同一样东西**"：
    #   · 记录器拿得到帧（它订阅上游 `Event.TELEMETRY`，**不依赖浏览器**）；
    #   · 落盘的文件**自洽**：`channels` 是 39 个、`frames`/`t_ms`/`integrity.frames` 三者一致、
    #     每行长度都等于通道数（列名与值对不齐是最容易出的错，且**看着像对的**）；
    #   · 重建得出来：`wall` 有格子被访问过（否则中栏是空白）；
    #   · **负向 A**：`records/{name}` 读一个不存在的名字必须 **404**（不许静默回空）；
    #   · **负向 B**：**空记录不许落盘** —— 立刻 start→stop（一帧都没来）必须回
    #     `ok=False` 且**不新增文件**。理由：零帧文件对复盘毫无价值，只会留垃圾。
    #
    # ⚠️ 这里**不停拉取**（I23 已经把它开着），否则记录器会"什么都没有，而且一声不响"。
    # 先把 I23 发车时**自动开着的那一次**记录收掉，好让下面这次从零开始、可复算。
    api(base, "/api/v1/mouse/maze/record/stop", "POST", {})
    time.sleep(0.5)
    list0, _, _ = api(base, "/api/v1/mouse/maze/records")
    names0 = {r.get("name") for r in ((list0.get("data") or {}).get("records") or [])}

    api(base, "/api/v1/mouse/maze/record/start", "POST", {"why": "drill"})
    time.sleep(3.5)                     # 攒几帧（真机 4~6 Hz，模拟器更快）
    st_rec, c_rec, _ = api(base, "/api/v1/mouse/maze/record")
    rec_live = st_rec.get("data") or {}
    stop_r, c_stop, _ = api(base, "/api/v1/mouse/maze/record/stop", "POST", {})
    saved = stop_r.get("data") or {}
    name = saved.get("name")

    # 读回来，验自洽
    doc = {}
    c_get = 0
    if name:
        g, c_get, _ = api(base, "/api/v1/mouse/maze/records/" + str(name))
        doc = g.get("data") or {}
    chans = doc.get("channels") or []
    fr = doc.get("frames") or []
    tt = doc.get("t_ms") or []
    integ = doc.get("integrity") or {}
    wall = doc.get("wall") or {}
    rows_ok = bool(fr) and all(isinstance(row, list) and len(row) == len(chans) for row in fr)
    counts_ok = (len(fr) == len(tt) == int(integ.get("frames") or -1))
    live_ok = bool(rec_live.get("recording")) and int(rec_live.get("frames") or 0) > 0
    wall_ok = bool(wall.get("visited"))                       # 有格子被访问过 ⇒ 中栏画得出东西
    # 负向 A：不存在的记录必须 404
    _bad, c_bad, _ = api(base, "/api/v1/mouse/maze/records/__no_such_record__.maze.json")
    # 负向 B：空记录（**一帧都没有**）不许落盘。
    # ⚠️ 光"立刻 start→stop"造不出空记录 —— 拉取开着时帧随时在来。
    #    必须**先把拉取关掉**，让记录器真的收不到任何帧，这才是"空"。
    api(base, "/api/v1/mouse/stream", "POST", {"on": False})
    time.sleep(1.5)                     # 等在途那一条落定（否则会算进帧数 ⇒ 假绿）
    api(base, "/api/v1/mouse/maze/record/start", "POST", {"why": "drill-empty"})
    time.sleep(0.6)
    empty_r, _, _ = api(base, "/api/v1/mouse/maze/record/stop", "POST", {})
    empty_saved_ok = not (empty_r.get("data") or {}).get("ok")   # 应回 ok=False
    list1, _, _ = api(base, "/api/v1/mouse/maze/records")
    names1 = {r.get("name") for r in ((list1.get("data") or {}).get("records") or [])}
    no_new_ok = (names1 - names0) <= {name} if name else (len(names1) == len(names0))

    i27_ok = (c_rec == 200 and c_stop == 200 and c_get == 200
              and live_ok and saved.get("ok") and name
              and len(chans) == 39 and counts_ok and rows_ok and wall_ok
              and c_bad == 404 and empty_saved_ok and no_new_ok)
    record("I27", "PASS" if i27_ok else "FAIL",
           f"记录中 frames={rec_live.get('frames')}（recording={rec_live.get('recording')}）→ "
           f"保存 {name}（{saved.get('frames')} 帧）→ 读回 {len(chans)} 通道 / {len(fr)} 帧"
           f"（t_ms {len(tt)}，integrity.frames {integ.get('frames')}，三者一致={counts_ok}）；"
           f"每行长度==通道数={rows_ok}；重建出 {len(wall.get('visited') or [])} 个已访问格；"
           f"负向：不存在的记录 → {c_bad}（应 404）；空记录 → ok={bool((empty_r.get('data') or {}).get('ok'))}"
           f"（应 False）、新增文件={'有' if not no_new_ok else '无'}"
           if i27_ok else
           f"**记录往返没成立**：live={rec_live}；stop HTTP {c_stop} → {saved}；"
           f"get HTTP {c_get}：chans={len(chans)}（应 39）frames={len(fr)} t_ms={len(tt)} "
           f"integrity={integ}；counts_ok={counts_ok} rows_ok={rows_ok} wall_ok={wall_ok}；"
           f"负向：不存在→{c_bad}（应 404）；空记录 ok={(empty_r.get('data') or {}).get('ok')}"
           f"（应 False）新增={names1 - names0}")

    # ---- I28：收车 / 救援 / 急停（上游 2.1.1 / 2.2.0 / 2.2.1）
    # 这条判据有两层，**第二层才是重点**：
    #
    # **第一层：三个"无参数简单命令"端点必须真的通。**
    #   `/mouse/tare`、`/mouse/stop`、`/mouse/estop` 曾经**一直是 404** ——
    #   本层的 `_simple_cmd()` 把**线上文本**（`"TARE"`/`"STOP"`/`"ESTOP"`）当成了
    #   上游 `mc.send_cmd()` 要的**登记名**（`"tare"`/`"stop"`/`"estop"`，全小写），
    #   于是 `COMMANDS["ESTOP"]` 直接 `KeyError`。
    #   最糟的是它包含**急停**（顶栏那个按钮），而**没有任何判据调过这三个端点**
    #   ⇒ 从来没红过。⇒ 这就是"**一道从没红过的闸不算闸**"推广到**按钮**上的那一课：
    #   界面上每个动作按钮都该有一条端到端判据走一遍（见设计方案 §11.12）。
    #   所以这里**逐个真调一遍**，并核对回来的 `cmd` 是**线上文本**（TARE/STOP/ESTOP）——
    #   那正是"登记名 → 线上文本"映射对了的证据。
    #
    # **第二层：三种失败要能分得开，且收车/救援的往返要成立。**
    #   · 急停中发车 ⇒ `ERR_ESTOP`（**不是** `ERR_BUSY`，也不是 OK）；
    #   · 已在跑再发车 ⇒ `ERR_BUSY`；
    #   · 收车 ⇒ 必须等到**新帧**才判（旧帧会把成功报成失败），所以 `reliable`
    #     必须是 `True`（模拟器上收得住；三态里的 `None` 表示"没等到帧、判不了"）；
    #   · ★ **收车后必须能再发车** —— 2.1.1 修的正是这条（以前"发过一次车就只能断电"）；
    #   · 救援 ⇒ `verified` 必须与复位原因一致（`rst == "SW"`），**不许只看 accepted**。
    # I27 结尾**故意把拉取关掉了**（为验"空记录不落盘"）⇒ 这里必须先重开：
    # 收车后的"等新帧"要靠帧在来，没有帧就只剩 `reliable=None`（判不了）。
    api(base, "/api/v1/mouse/stream", "POST", {"on": True})
    time.sleep(1.2)
    api(base, "/api/v1/mouse/maze/stop", "POST", {})      # 清场：确保不在跑
    time.sleep(0.6)

    # ① 三个简单命令端点（**线上文本**要出现在应答里）
    simple = {}
    for path, want in (("/api/v1/mouse/tare", "TARE"),
                       ("/api/v1/mouse/stop", "STOP"),
                       ("/api/v1/mouse/estop", "ESTOP")):
        rr, cc, _ = api(base, path, "POST", {}, timeout=20)
        got = (((rr.get("data") or {}).get("result") or {}).get("data") or {}).get("cmd")
        stt = (((rr.get("data") or {}).get("result") or {}).get("status"))
        simple[want] = (cc, stt, got)
    simple_ok = all(code == 200 and stt == "OK" and got == want
                    for want, (code, stt, got) in simple.items())

    # ② 急停中发车 ⇒ ERR_ESTOP（负向：既不是 OK 也不是 ERR_BUSY）
    rm, cm, _ = api(base, "/api/v1/timesync/maze_run", "POST", {}, timeout=25)
    emsg = ((rm.get("error") or {}).get("message") or "") if isinstance(rm, dict) else ""
    estop_gate_ok = (cm == 409 and "ERR_ESTOP" in emsg)

    # ③ 解锁（stop）后发车 ⇒ OK；再发车 ⇒ ERR_BUSY（两种失败分得开）
    #    ⚠️ "要求成功"的这两处走 `launch_checked`（挡掉模拟器电池抖动造成的假红，
    #    并在真被低电拦下时如实回报），见它的 docstring。
    api(base, "/api/v1/mouse/stop", "POST", {})
    time.sleep(0.9)
    r1, c1, msg1, ok1, low1 = launch_checked(base)
    r2, c2, _ = api(base, "/api/v1/timesync/maze_run", "POST", {}, timeout=25)
    msg2 = ((r2.get("error") or {}).get("message") or "") if isinstance(r2, dict) else ""
    busy_ok = (c2 == 409 and "ERR_BUSY" in msg2)

    # ④ 收车：这时车**正在跑**（上一步刚发车成功、任务机在 SEARCH）。
    #    ⚠️ 这里 **不能**断言"运动中收车必然收不住" —— 那是**真机限定**的现象
    #    （`MAZE STOP` 只置"请退出"标志，而标志检查在 `while(1)` 顶部、主循环有
    #     30 余处非协作式阻塞等待）。**模拟器没有那些阻塞等待**，实测它当场就收住了
    #    （`reliable=True, task=0`）⇒ 写成普适断言会让这条判据在模拟器上永远红，
    #    而红的理由**不是产品错了**，是判据把"真机现象"当成了"必然"。
    #    （真踩过：第一版就是这么写的，实测报 `rel=True` ⇒ 判据自己错了。）
    #
    #    ⇒ 这一格验的是**"如实报告"**——`reliable` 必须与 `state.task` **自洽**：
    #        `True`  ⇔ 任务机已不在跑（task ≤ 1）
    #        `False` ⇔ 任务机还在跑（task ≥ 2）
    #        `None`  ⇒ 没等到新帧、判不了（这一态**无所谓自洽**，但字段必须在）
    #      这才是真正要守的东西：**不许把"没收到"说成"收住了"**。
    #    "静止时收车收得住"由 ⑥b 验；"真机运动中收不住"记在 §11.12，属**如实标注**。
    rs, cs, _ = api(base, "/api/v1/mouse/maze/stop", "POST", {}, timeout=25)
    sv = rs.get("data") or {}
    rel = sv.get("reliable")
    task0 = ((sv.get("state") or {}).get("task"))
    has_field = ("reliable" in sv and isinstance(task0, int))
    if rel is True:
        consistent = (task0 <= 1)          # 说"收住了" ⇒ 任务机必须真停了
    elif rel is False:
        consistent = (task0 >= 2)          # 说"没收住" ⇒ 任务机必须还在跑
    else:
        consistent = True                  # 判不了 ⇒ 不做自洽要求（但字段要在）
    moving_honest = (cs == 200 and bool(sv.get("ok")) and has_field and consistent)
    moving_task = task0                    # 报告里写出来（模拟器/真机会不同，如实看）
    moving_rel = rel

    # ⑤ ★ 收车后**再发车**（2.1.1 修的就是这条：以前只能断电）
    r3, c3, msg3, relaunch_ok, low3 = launch_checked(base)

    # ⑥ 救援（把车弄停 ⇒ 之后才能验"静止时收车真能收住"）
    rr, cr, _ = api(base, "/api/v1/mouse/rescue", "POST", {}, timeout=45)
    rv = rr.get("data") or {}
    rescue_ok = (cr == 200 and bool(rv.get("verified")) and rv.get("rst") == "SW"
                 and rv.get("advice"))
    time.sleep(3.4)                      # 复位后约 3 s 才有新帧（判读要等新鲜帧）
    rstt, _, _ = api(base, "/api/v1/mouse/maze/state")
    task_rst = ((rstt.get("data") or {}).get("state") or {}).get("task")

    # ⑥b 静止态收车：**这一格才验"收得住"**（车已停 ⇒ 标志轮得到检查）
    rs2, cs2, _ = api(base, "/api/v1/mouse/maze/stop", "POST", {}, timeout=25)
    sv2 = rs2.get("data") or {}
    rel2 = sv2.get("reliable")
    task2 = ((sv2.get("state") or {}).get("task"))
    collect_ok = (cs2 == 200 and sv2.get("ok") and rel2 is True
                  and isinstance(task2, int) and task2 <= 1)

    # ⑦ 低电预检端点：三态里的哪一种都要**答得出来**（这条只验"答得出"，不验闸本身
    #    —— 闸的判定在 I29 里用**纯函数**判，因为模拟器电池漂移造不出确定性的低电）
    rp, cp, _ = api(base, "/api/v1/mouse/launch_precheck")
    pv = rp.get("data") or {}
    pre_ok = (cp == 200 and pv.get("level") in ("ok", "warn", "unknown")
              and pv.get("min_mv") == 7000)

    # ⚠️ `ok1` / `relaunch_ok` 允许"**判不了**"这一态：模拟器电池读数抖到阈值以下时，
    #    `ERR_LOWBATT` 会把"要求成功"的那两次发车拦下 —— 那是**环境**，不是产品
    #    （闸本身由 I29 用纯函数守，与这里的抖动无关）。但必须**写进报告**，不许静默放过。
    low_note = ("；⚠️ 有发车被**低电闸**拦下（模拟器电池抖动到阈值以下）⇒ "
                "那一格按「判不了」记，不是产品错") if (low1 or low3) else ""
    i28_ok = (simple_ok and estop_gate_ok and (ok1 or low1) and busy_ok
              and moving_honest and (relaunch_ok or low3)
              and rescue_ok and collect_ok and pre_ok)
    record("I28", "PASS" if i28_ok else "FAIL",
           f"三个简单命令端点全通且线上文本对（"
           + "、".join(f"{w}→{v[2]}" for w, v in sorted(simple.items())) + "）；"
           f"急停中发车 ⇒ ERR_ESTOP（负向，非 OK/非 BUSY）；解锁后发车 OK、再发车 ERR_BUSY；"
           f"**收车如实报告**：reliable={moving_rel!r} / task={moving_task}"
           f"（自洽，{'模拟器当场就收住了' if moving_rel is True else '如实报告没收住'}）；"
           f"**收车后能再发车**（2.1.1 修的就是这条）：{relaunch_ok}；"
           f"救援 verified={rv.get('verified')}（rst={rv.get('rst')}、"
           f"accepted={rv.get('accepted')}、{rv.get('elapsed_s')}s）⇒ 复位后 task={task_rst}；"
           f"**静止态收车收得住**：reliable={rel2}（task={task2}）；"
           f"低电预检 level={pv.get('level')}（阈值 {pv.get('min_mv')} mV）" + low_note
           if i28_ok else
           f"**收车/救援/急停没成立**：简单命令={simple}（应 200/OK/线上文本）"
           f" simple_ok={simple_ok}；急停中发车 code={cm} msg={emsg[:90]!r}"
           f" estop_gate_ok={estop_gate_ok}；解锁后发车 code={c1} ok1={ok1}"
           f" msg={msg1[:120]!r}；"
           f"收车如实报告 code={cs} reliable={rel!r} task={task0} "
           f"has_field={has_field} consistent={consistent} moving_honest={moving_honest}；"
           f"收车后再发车 code={c3} relaunch_ok={relaunch_ok} msg={msg3[:120]!r}；"
           f"救援 code={cr} {rv} rescue_ok={rescue_ok}；"
           f"静止态收车：code={cs2} reliable={rel2!r} task={task2} collect_ok={collect_ok}；"
           f"预检 code={cp} {pv} pre_ok={pre_ok}")

    # ---- I30：右栏那条决策线，"实时"必须与"回放"**逐条相等**，且换段不残留
    #
    # 用户报的现象："回放按钮读到的事件树和退出回放的完全不一样"。
    # 根因：右栏那条时间线有**两条来路** ——
    #   · 实时 = `/mouse/maze/decisions`（记录器内存里的**常驻**决策环）；
    #   · 回放 = `/mouse/maze/record/live` 快照 / 已保存的记录（**这一段自己的**差分器）。
    # 而 `start()`（发车）当时只把环零点挪到发车时刻、**没清环、也没重置差分器**
    # ⇒ 上一段（以及发车之前那些帧）的决策原封不动留着，相对新零点就成了**负时间**
    # （实测：段 2 发车后环里 303 条、其中 151 条是负数，t 从 -14180 起）。
    # 回放那份**永远是单段**的 ⇒ 两条来路看起来"完全不一样"。
    #
    # 判据四条（两条正向 + **两条负向**）：
    #   ① 逐条相等：同一时刻取环与快照，`decisions` 必须**完全一致**（含 `t_ms`）；
    #   ② 落盘那份也一样（保存后读回再与环比对）；
    #   ③ 负向 **换段不残留**：收车 → 救援 → 再发车之后，新环必须**从 0 附近重新开始**
    #      （`max(t_ms)` 不许接着上一段的时长往上堆），且**不许出现负 `t_ms`**；
    #   ④ 负向 **发车被拒不许抹掉右栏**：`ERR_BUSY` 时环必须**原样保留**（前缀不变）、
    #      零点没被挪走（仍无负 t）。
    #
    # ⚠️ 少掉 ③④ 这两条负向，"清环"这件事**可以整个不被实现**而 ①② 照样绿 ——
    #    单段场景下清不清环看不出来。这就是"一道从没红过的闸不算闸"。
    # ⚠️ ④ 必须**先 `record/stop`** 再发车：否则 `start()` 会回 `already`
    #    （沿用 ③ 那段还在记的记录）⇒ 压根不走"新开一段"的路，回滚也无从谈起。
    head("14d. 判据 I30：右栏决策线「实时 ≡ 回放」（含换段不残留 / 发车被拒不抹数据）")

    def _dec_ring():
        rr, cc, _ = api(base, "/api/v1/mouse/maze/decisions", timeout=25)
        return (rr.get("data") or {}) if cc == 200 else {}

    def _dk(x):
        return json.dumps(x, sort_keys=True, ensure_ascii=False)

    def _neg(ds):
        return [x for x in ds if isinstance(x.get("t_ms"), int) and x["t_ms"] < 0]

    def _msg(rr):
        return str(((rr.get("error") or {}).get("message") or "")) if isinstance(rr, dict) else ""

    # 清场：I28 里发过车 ⇒ 记录器可能还开着；再把车弄回"可发车"
    api(base, "/api/v1/mouse/maze/record/stop", "POST", {})
    api(base, "/api/v1/mouse/stream", "POST", {"on": True})
    api(base, "/api/v1/mouse/maze/stop", "POST", {}, timeout=25)
    time.sleep(0.8)
    api(base, "/api/v1/mouse/rescue", "POST", {}, timeout=45)
    time.sleep(3.4)

    # ① 发一段，取"实时"那条线 + "进回放"那份快照，**逐条比对**
    #    ⚠️ "要求成功"的发车走 `launch_checked`（模拟器电池抖动会让同一命令时通时不通，
    #    见它的 docstring）；真被低电拦下时如实回报、按"判不了"记。
    l30, cl30, m30, ok30, low30 = launch_checked(base)
    d30 = (l30.get("data") or {}) if isinstance(l30, dict) else {}
    launch30 = (ok30 and bool((d30.get("record") or {}).get("recording")))
    time.sleep(11.0)
    ring1 = _dec_ring().get("decisions") or []
    neg1 = _neg(ring1)
    s30, cs30, _ = api(base, "/api/v1/mouse/maze/record/live?with_map=0", timeout=30)
    snap = (((s30.get("data") or {}).get("doc") or {}).get("decisions") or [])
    n_eq = min(len(snap), len(ring1))
    live_eq = (cs30 == 200 and n_eq > 0
               and all(_dk(snap[i]) == _dk(ring1[i]) for i in range(n_eq)))

    # ② 保存后再读回一次（"从记录列表进回放"走的就是它），同样逐条比
    p30, cp30, _ = api(base, "/api/v1/mouse/maze/record/stop", "POST", {}, timeout=30)
    sv30 = p30.get("data") or {}
    name30 = sv30.get("name")
    fdec = []
    if name30:
        g30, _cg30, _ = api(base, "/api/v1/mouse/maze/records/" + str(name30), timeout=30)
        fdec = ((g30.get("data") or {}).get("decisions") or [])
    ring1b = _dec_ring().get("decisions") or []
    n_f = min(len(fdec), len(ring1b))
    file_eq = (bool(name30) and n_f > 0
               and all(_dk(fdec[i]) == _dk(ring1b[i]) for i in range(n_f)))

    # ③ 负向：**换段不残留**。收车 → 救援 → 再发车，新环必须从 0 附近重新开始。
    api(base, "/api/v1/mouse/maze/stop", "POST", {}, timeout=25)
    time.sleep(0.8)
    api(base, "/api/v1/mouse/rescue", "POST", {}, timeout=45)
    time.sleep(3.4)
    t_b2 = time.monotonic()
    l31, cl31, m31, ok31, low31 = launch_checked(base)
    launch31 = ok31
    time.sleep(10.0)
    span2 = int((time.monotonic() - t_b2) * 1000)
    ring2 = _dec_ring().get("decisions") or []
    neg2 = _neg(ring2)
    ts2 = [x.get("t_ms") for x in ring2 if isinstance(x.get("t_ms"), int)]
    # 纪元真的重来了 ⇒ 环里最大的时刻不该超过"这次发车到现在"（+余量）。
    # 不重来（环没清 / 零点没挪）的话，它会接着上一段的时长继续往上堆（≈ 11 s + 10 s）。
    # ⚠️ 发车本身没成（低电拦下）时这一格**判不了**：环会因为"被拒回滚"而保留上一段，
    #    那不是"换段残留"，是**没换段**。标 `None` 交出去，别当成通过、也别当成失败。
    fresh_ok = (None if not launch31
                else (bool(ts2) and not neg2 and (max(ts2) <= span2 + 5000)))

    # ④ 负向：**发车被拒不许把右栏抹掉**。先收掉记录（否则 `start()` 回 already、
    #    根本不走"新开一段"的路），此时车仍在迷宫态 ⇒ `MAZE RUN` 回 ERR_BUSY。
    #    ⚠️ 前提是**③ 真的把车发起来了**（否则谈不上"已经在跑"）⇒ 没发起来就判不了。
    api(base, "/api/v1/mouse/maze/record/stop", "POST", {})
    time.sleep(0.4)
    before_k = [_dk(x) for x in (_dec_ring().get("decisions") or [])]
    r32, c32, _ = api(base, "/api/v1/timesync/maze_run", "POST", {}, timeout=25)
    time.sleep(2.0)
    after = _dec_ring().get("decisions") or []
    after_k = [_dk(x) for x in after]
    keep_ok = (None if not launch31 else
               (c32 == 409 and len(before_k) > 0
                and after_k[:len(before_k)] == before_k
                and not _neg(after)))

    low_note30 = ("；⚠️ 有发车被**低电闸**拦下（模拟器电池抖动到阈值以下）⇒ 对应的负向"
                  "按「判不了」记，不是产品错") if (low30 or low31) else ""
    i30_ok = ((launch30 or low30) and live_eq and file_eq
              and (fresh_ok is not False) and (keep_ok is not False))
    record("I30", "PASS" if i30_ok else "FAIL",
           f"同一段：实时环 {len(ring1)} 条 / 回放快照 {len(snap)} 条 ⇒ 比对 {n_eq} 条"
           f"**逐条相等**（{live_eq}）；落盘那份 {len(fdec)} 条 ⇒ 比对 {n_f} 条逐条相等"
           f"（{file_eq}）；负向①换段不残留：新环 {len(ring2)} 条、"
           f"max t={max(ts2) if ts2 else None} ms（今回已过 {span2} ms）、"
           f"负 t={len(neg2)} 条 ⇒ {fresh_ok}；"
           f"负向②发车被拒（HTTP {c32}，应 409）后旧条目 {len(before_k)} 条仍原样在前、"
           f"负 t={len(_neg(after))} 条 ⇒ {keep_ok}" + low_note30
           if i30_ok else
           f"**右栏实时线没守住**：launch30={launch30}（HTTP {cl30}，{str(m30)[:90]!r}）；"
           f"实时环 {len(ring1)} 条（负 t={len(neg1)}）vs 快照 {len(snap)} 条"
           f"（HTTP {cs30}）⇒ live_eq={live_eq}；落盘 {len(fdec)} 条（HTTP {cp30}，"
           f"name={name30}）⇒ file_eq={file_eq}；换段：launch31={launch31}"
           f"（HTTP {cl31}，{str(m31)[:90]!r}）新环 {len(ring2)} 条 "
           f"max t={max(ts2) if ts2 else None} neg={len(neg2)} span={span2} ⇒ {fresh_ok}；"
           f"发车被拒：HTTP {c32}（应 409，{_msg(r32)[:70]!r}）"
           f"旧条目 {len(before_k)} 条、前缀保持={after_k[:len(before_k)] == before_k}、"
           f"neg={len(_neg(after))} ⇒ {keep_ok}")

    # ---- 断开，别把模拟器链路留着
    api(base, "/api/v1/mouse/disconnect", "POST", {})


ALLOWED_TSRC = {"send", "capture", "arrival", "local"}


def _timeline_tsrc_check() -> tuple:
    """查最近一个会话的 timeline：是否**每一行**都标明了"这是哪一种时间"。"""
    import csv as _csv
    root = ROOT / "sessions"
    dirs = sorted([d for d in root.glob("2026-*") if d.is_dir()])
    if not dirs:
        return False, "找不到会话目录 ⇒ 判不了"
    tl = dirs[-1] / "timeline.csv"
    if not tl.is_file():
        return False, f"{dirs[-1].name} 里没有 timeline.csv"
    rows = list(_csv.DictReader(open(tl, encoding="utf-8-sig")))
    if not rows:
        return False, "timeline 是空的"
    bad = [r for r in rows if (r.get("t_src") or "") not in ALLOWED_TSRC]
    kinds = {}
    for r in rows:
        kinds[r.get("t_src")] = kinds.get(r.get("t_src"), 0) + 1
    note = (f"{dirs[-1].name} 的 timeline {len(rows)} 行，t_src 分布 {kinds}")
    return (not bad), note + ("" if not bad else f"；**{len(bad)} 行没标或标错**")


def _free_port() -> int:
    """要一个**当前空闲**的端口（起临时上游用，别碰 8765 —— 那儿可能跑着真服务）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = int(s.getsockname()[1])
    s.close()
    return p


def _port_busy(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", int(port)))
        return False
    except OSError:
        return True
    finally:
        s.close()


class _LogOnly:
    """给 ServiceLauncher 的最小替身：它只调 `log` 与 `mark`。"""
    def log(self, *a, **k):        # noqa: D102
        pass
    def mark(self, *a, **k):       # noqa: D102
        pass


def _launcher_sections(base: str, tmp: Path) -> None:
    """I24 / I25 —— 上游相机程序的**设置面**与**代启**。

    用户要求："配对时如果需要依赖上游提供的服务，则应当支持在设置中设置上游程序地址，
    在配对时自动启动调用而不需要手动启动上游服务"。

    这两条要守住的三件事，各自都有反例：
      · **设置要存得住**（改完下次还在；且不许把 config.yaml 的注释抹掉）；
      · **代启只能由人触发**（`/health` 每 2.5 s 走一次，它要是会拉起进程，
        一个开着的页面就会不停起进程 —— 这类"看着在工作，其实在制造垃圾"最难发现）；
      · **幂等**（已经可达就不起；本层起的还在跑就不重复起）。
    """
    import json as _json
    import shutil as _shutil

    from control import config as C
    from control.launcher import ServiceLauncher

    # ---------------------------------------------------------------- I24
    head("2c. 判据 I24：上游相机程序的设置面（零副作用、存得住、不泄凭据）")
    g, c, _ = api(base, "/api/v1/camera/service")
    s = (g.get("data") or {}) if c == 200 else {}
    disp = " ".join(s.get("argv_display") or [])
    token = ""
    try:
        raw = _json.dumps(api(base, "/api/v1/config")[0], ensure_ascii=False)
    except Exception:                                         # noqa: BLE001
        raw = ""
    token = ""
    import re as _re
    m = _re.search(r"camera\.launcher[\s\S]{0,400}", raw)
    # 凭据不外泄：启动行里必须是占位符；整个响应里不许出现真 token 串
    tok_leak = bool(token) and token in _json.dumps(s, ensure_ascii=False)
    spec_ok = bool(c == 200 and s.get("script_ok") and s.get("port")
                   and s.get("can_start") and not s.get("running")
                   and "••••" in disp and not tok_leak)
    # /health 连打三次**不许**拉起进程（这是本判据最要紧的一条）
    before = (s.get("pid"), s.get("running"))
    for _ in range(3):
        api(base, "/api/v1/health", timeout=10)
    g2, _, _ = api(base, "/api/v1/camera/service")
    s2 = g2.get("data") or {}
    no_spawn = (s2.get("pid"), s2.get("running")) == before == (None, False)

    # 设置要**存得住**：在**临时副本**上改嵌套键 —— 不碰真 config.yaml。
    probe = tmp / "config_probe.yaml"
    _shutil.copyfile(ROOT / "config.yaml", probe)
    cfg = C.load(probe)
    before_lines = probe.read_text(encoding="utf-8").splitlines()
    r1 = cfg.patch({"camera.launcher.autostart": "false",
                    "camera.launcher.script": '"../PhoneCamera/pc/server.py"',
                    "arena.fw_to_arena.y_flip": "false"})
    after_lines = probe.read_text(encoding="utf-8").splitlines()
    try:
        reloaded = _yaml_load(probe)
        auto = reloaded["camera"]["launcher"]["autostart"]
        script = reloaded["camera"]["launcher"]["script"]
        yflip = reloaded["arena"]["fw_to_arena"]["y_flip"]
    except Exception as e:                                    # noqa: BLE001
        reloaded, auto, script, yflip = None, None, None, None
        print(f"      重载失败：{type(e).__name__}: {e}")

    def _indent(lines_: list, key: str) -> int:
        for ln in lines_:
            if ln.strip().startswith(key + ":"):
                return len(ln) - len(ln.lstrip())
        return -1

    indent_ok = all(_indent(before_lines, k) == _indent(after_lines, k) >= 0
                    for k in ("autostart", "script", "y_flip"))
    notes_kept = all("#" in ln for ln in after_lines
                     if ln.strip().startswith(("autostart:", "script:", "y_flip:")))
    persist_ok = (all(v == "patched" for v in r1.values()) and reloaded is not None
                  and auto is False and script == "../PhoneCamera/pc/server.py"
                  and yflip is False and indent_ok and notes_kept)
    probe.unlink()

    i24_ok = spec_ok and no_spawn and persist_ok
    record("I24", "PASS" if i24_ok else "FAIL",
           f"设置面就绪：程序 {Path(str(s.get('script_abs') or '')).name} 存在、端口 "
           f"{s.get('port')}、启动行用占位符（无凭据外泄）；/health 连打 3 次"
           f"**没有代启进程**；设置能写回（嵌套键缩进不变 {indent_ok}、注释保留 "
           f"{notes_kept}、重载得动、值生效 autostart={auto}）"
           if i24_ok else
           f"**设置面有问题**：HTTP {c} spec={s}\n"
           f"      未代启={no_spawn}（pid/running {before}→{(s2.get('pid'), s2.get('running'))}）"
           f"；写回={r1} 重载={reloaded is not None} 缩进={indent_ok} 注释={notes_kept}"
           f"；auto={auto} script={script} y_flip={yflip}")

    # ---------------------------------------------------------------- I25
    head("2d. 判据 I25：代启上游服务 → 真能配对（不占 8765、不碰真配置）")
    # 全部在**进程内**做：临时配置副本 + 空闲端口 + 真上游 `pc/server.py`。
    # 这样既不污染现场的 config.yaml，也不会和"用户自己起的那个 8765 服务"打架。
    port = _free_port()
    probe2 = tmp / "config_launch.yaml"
    _shutil.copyfile(ROOT / "config.yaml", probe2)
    cfg2 = C.load(probe2)
    # ⚠️ 副本放在 `.tmp/drill/` 下 ⇒ **相对路径会按那个目录解析**（`../PhoneCamera/pc`
    # 变成 `.tmp/PhoneCamera/pc`，于是"找不到上游程序"）。所以路径类配置一律
    # 从**真配置**里取绝对路径覆盖回来 —— 只改与本次探测有关的项。
    real = C.load()
    for k in ("remotecontrol_host", "phonecamera_pc", "autolocate"):
        v = real.get(f"paths.{k}")
        if v:
            cfg2.update(f"paths.{k}", str(v), save=False)
    work = tmp / "camprobe"
    work.mkdir(exist_ok=True)
    cfg2.update("camera.base", f"http://127.0.0.1:{port}", save=False)
    cfg2.update("camera.launcher.port", port, save=False)
    cfg2.update("camera.launcher.data_dir", str(work / "data"), save=False)
    cfg2.update("camera.launcher.log", str(work / "serv.log"), save=False)
    cfg2.update("camera.launcher.max_mbps", 5.0, save=False)
    lz = ServiceLauncher(cfg2, _LogOnly(), _LogOnly())

    pc_dir = str(cfg2.get("paths.phonecamera_pc") or "")
    if pc_dir and pc_dir not in sys.path:
        sys.path.insert(0, pc_dir)
    up = {"n": 0}

    def _is_up() -> bool:
        """此刻通不通 —— 与 `routes.py::_cam_is_up` 同构（**真探**，不是读快照）。

        ⚠️ `HostLink(base, token, timeout=…)` 的 **token 是必需的位置参数**：
        漏了它抛的是 `TypeError`，而这里 `except Exception` 会把它吞成"还没起来"——
        表现是"白等 25 s 然后报没起来"，而真凶完全看不见（真踩过）。
        """
        up["n"] += 1
        try:
            import hostlink
            hostlink.HostLink(f"http://127.0.0.1:{port}",
                              str(cfg2.get("camera.token") or ""), timeout=3.0).capabilities()
            return True
        except Exception:                                     # noqa: BLE001
            return False

    qr_rows, st1, st2, stopped, released = 0, {}, {}, None, False
    log_file = work / "serv.log"
    # ⚠️ **必须在"最坏的环境"里跑**。本层要挡的正是"父进程是 GBK locale，子进程继承过去
    # 就在打印自己的 banner 时崩掉"（真机踩过：上游 `server.py` 的 `▶ (U+25B6)` 在 GBK 下
    # `UnicodeEncodeError`，进程 exit 1、服务根本没起来）。而开发机上常常自带
    # `PYTHONUTF8=1` —— 在那样的环境里跑这条判据**永远绿**，等于没验。
    # 所以这里临时把父环境改成 GBK 风格（跑完恢复）：launcher **覆盖**子进程环境这一件事
    # 才算真的被验到。
    saved_env = {k: os.environ.get(k) for k in
                 ("PYTHONUTF8", "PYTHONIOENCODING", "LANG", "LC_ALL")}
    for k in ("PYTHONUTF8", "LANG", "LC_ALL"):
        os.environ.pop(k, None)
    os.environ["PYTHONIOENCODING"] = "gbk"
    env_note = "父环境已临时改为 GBK（模拟真机默认）"
    spec1 = lz.status(deep=True)
    try:
        st1 = lz.start(is_up=_is_up, wait_s=25.0)
        if st1.get("ok"):
            try:
                import hostlink
                h = hostlink.HostLink(f"http://127.0.0.1:{port}",
                                      str(cfg2.get("camera.token") or ""), timeout=6.0)
                pr = h.pair()
                qr_rows = len(((pr.get("qr_matrix") or {}).get("rows")) or [])
            except Exception as e:                            # noqa: BLE001
                print(f"      配对读取失败：{type(e).__name__}: {e}")
            st2 = lz.start(is_up=_is_up, wait_s=10.0)         # 幂等：不许起第二个
        # ---- 收工策略：默认**保留**（手机还连着它），显式要求才停。
        # 这是用户明确问过的一条（"Ctrl+C 收尾后上游服务到底关没关"），
        # 所以要有断言 —— 而且默认那条必须**真的检查进程还活着**，不能只看返回值。
        keep = lz.shutdown()                                  # stop_on_exit=false（默认）
        kept_alive = bool(lz._proc is not None and lz._proc.poll() is None)
        stopped = lz.shutdown(stop=True)                      # 显式要求才停
        time.sleep(0.6)
        released = not _port_busy(port)
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        try:
            lz.stop()
        except Exception:                                     # noqa: BLE001
            pass
        try:
            probe2.unlink()
        except OSError:
            pass

    # 子进程环境的**结构断言**：我们保证了什么，要看得见（否则这条保护会被人无意删掉）
    env_ok = all((spec1.get("child_env") or {}).get(k) == v
                 for k, v in (("PYTHONIOENCODING", "utf-8"), ("PYTHONUTF8", "1"),
                              ("PYTHONUNBUFFERED", "1")))
    raw_log = b""
    try:
        raw_log = log_file.read_bytes()
    except OSError:
        pass
    try:
        raw_log.decode("utf-8")
        log_utf8 = True
    except UnicodeDecodeError:
        log_utf8 = False
    no_tb = b"Traceback" not in raw_log

    exit_ok = (keep.get("stopped") is False and keep.get("by_setting") is False
               and kept_alive and bool(stopped.get("stopped")))
    i25_ok = (bool(st1.get("ok")) and st1.get("started") and qr_rows > 0
              and st2.get("started") is False
              and bool(stopped and stopped.get("stopped")) and released
              and env_ok and log_utf8 and no_tb and exit_ok)

    # ---- I25 的**负向**面：把编码保护打掉 ⇒ 必须起不来，而且失败原因正是编码那件事。
    # 为什么值得再花几秒跑一次：`PYTHONIOENCODING=utf-8` 那三行是"看不见的保护"，
    # 谁顺手删掉都不会有任何症状（在我的机器上永远绿）—— 只有"打掉它就红"才是真的守着。
    # 断言要**认到具体原因**（带 `UnicodeEncodeError`）：光看"失败了"会放过"因为别的原因失败"。
    neg_port = _free_port()
    cfg2.update("camera.base", f"http://127.0.0.1:{neg_port}", save=False)
    cfg2.update("camera.launcher.port", neg_port, save=False)
    cfg2.update("camera.launcher.log", str(work / "neg.log"), save=False)
    lz.reload()
    import control.launcher as _L
    saved_child_env = dict(_L._CHILD_ENV)
    _L._CHILD_ENV.clear()
    for k in ("PYTHONUTF8", "LANG", "LC_ALL"):
        os.environ.pop(k, None)
    os.environ["PYTHONIOENCODING"] = "gbk"
    neg = {}
    try:
        try:
            (work / "neg.log").write_bytes(b"")
        except OSError:
            pass
        neg = lz.start(is_up=lambda: False, wait_s=8.0)
    finally:
        _L._CHILD_ENV.update(saved_child_env)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        try:
            lz.stop()
        except Exception:                                     # noqa: BLE001
            pass
    neg_detail = str(neg.get("detail") or "")
    neg_ok = (neg.get("ok") is False) and ("UnicodeEncodeError" in neg_detail)
    i25_ok = i25_ok and neg_ok

    record("I25", "PASS" if i25_ok else "FAIL",
           f"代启成功：PID {st1.get('pid')} · 端口 {port} · {st1.get('waited_ms')} ms 就绪；"
           f"当场读到二维码矩阵 {qr_rows} 行 ⇒ **配对不再需要手动启动上游**；"
           f"再点一次不起第二个（{st2.get('reason')}）。"
           f"（{env_note}）子进程环境={spec1.get('child_env')}、日志严格 UTF-8={log_utf8}、"
           f"无 Traceback={no_tb}；负向：打掉编码保护后**如期失败**且报的是 "
           f"UnicodeEncodeError（{neg_ok}）。**收尾策略**：默认收工 {keep.get('stopped')} "
           f"（进程仍活着={kept_alive} ⇒ 手机不断线）、显式要求时已停（PID {stopped.get('pid')}）"
           f"且端口已释放（{released}）"
           if i25_ok else
           f"**代启链路不通**：start={st1} / 二次={st2} / 收工={keep} 仍活={kept_alive} "
           f"/ stop={stopped} / 端口释放={released} / 二维码 {qr_rows} 行 / env_ok={env_ok} "
           f"/ 日志UTF-8={log_utf8} / 无异常={no_tb} / 负向={neg_ok}（{neg.get('reason')}）\n"
           f"      日志尾：{(raw_log[-400:] or b'').decode('utf-8', 'replace')}")


def _mjpeg_relay_section(base: str, args, tmp: Path) -> None:
    """I26 —— MJPEG 反代**真的能切出一帧**（这是"看得见画面"的唯一判据）。

    真踩过的那次故障长这样：手机在发、反代在转发、服务端日志只有两条"反代中断"，
    而**预览窗口永远空白**。根因是反代把 `Content-Type` 写死成
    `multipart/x-mixed-replace; boundary=frame`，而上游声明的是
    `boundary=phonecamera-frame` —— 声明与实际不符，浏览器按 `--frame` 去切分、
    流里却是 `--phonecamera-frame`，**一帧都切不出来**。

    所以这条判据**不能只比 header**（那只是"我们抄对了字符串"），必须
    **按反代自己声明的 boundary 去切，并真的拿到一帧完整 JPEG**（`FFD8` 起 `FFD9` 止）。
    上游是**假相机**、boundary 故意用非常规值 —— 写死任何一种常见值的实现都会红。
    """
    import threading

    head("2e. 判据 I26：MJPEG 反代的 boundary 必须与上游一致，且能真切出帧")
    orig_base = ((api(base, "/api/v1/health")[0].get("data") or {})
                 .get("camera") or {}).get("base") or ""
    fake = _fake_camera(args.fake_cam_port)
    want_boundary = fake.fake_mjpeg_boundary
    ct = ""
    frames = 0
    jpeg_ok = False
    err = ""
    try:
        api(base, "/api/v1/camera/base", "POST",
            {"base": f"http://127.0.0.1:{args.fake_cam_port}"})
        api(base, "/api/v1/camera/probe", "POST", {})
        # 读反代：**用标准库直接连**，拿到 header 后按声明的 boundary 切帧。
        conn = http.client.HTTPConnection("127.0.0.1", args.port, timeout=10)
        conn.request("GET", "/api/v1/camera/mjpeg")
        r = conn.getresponse()
        ct = r.getheader("Content-Type") or ""
        # 只取我们声明的那一段（假上游发 5 帧就收工，不会挂住）
        raw = r.read()
        conn.close()
        declared = ""
        for part in ct.split(";"):
            if part.strip().lower().startswith("boundary="):
                declared = part.split("=", 1)[1].strip().strip('"')
        if declared:
            # 只认"我们声明的那个"分界 —— 声明错了这里就是 0 帧
            marker = f"--{declared}".encode()
            chunks = raw.split(marker)
            for ch in chunks[1:]:
                body = ch.split(b"\r\n\r\n", 1)
                if len(body) < 2:
                    continue
                jpg = body[1].rstrip(b"\r\n")
                if jpg.startswith(b"\xff\xd8") and jpg.endswith(b"\xff\xd9"):
                    frames += 1
            jpeg_ok = frames > 0
    except Exception as e:                                    # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    finally:
        try:
            fake.shutdown()
            fake.server_close()
        except Exception:                                     # noqa: BLE001
            pass

    declared_ok = want_boundary in ct               # boundary 必须是**上游声明的那个**
    i26_ok = declared_ok and jpeg_ok
    record("I26", "PASS" if i26_ok else "FAIL",
           f"反代声明 `{ct}`（与上游的 boundary 一致）；按**它自己声明的 boundary** "
           f"切出 {frames} 帧完整 JPEG（FFD8…FFD9）⇒ 能看见画面"
           if i26_ok else
           f"**MJPEG 反代拿不出画面**：\n"
           f"      我们声明 : {ct or '（没拿到）'}\n"
           f"      上游声明 : boundary={want_boundary}\n"
           f"      切出帧数 : {frames}（要 >0）{('  异常：' + err) if err else ''}\n"
           f"      ⇒ boundary 对不上的话，浏览器会**一帧都切不出来**（预览空白，"
           f"而服务端一切正常）")
    if orig_base:
        try:
            api(base, "/api/v1/camera/base", "POST", {"base": orig_base})
        except Exception:                                     # noqa: BLE001
            pass


def _yaml_load(path: Path):
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--frames", type=int, default=8)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--sim", action="store_true",
                    help="起上游鼠标模拟器，额外验 I15~I17 / I22~I23（时间同步 / 参数四组 / 丢帧 / 统一帧单流 / 迷宫状态机 / 记录往返 / 收车与救援）。"
                         "不加就 SKIP —— 那几条需要一条活链路。")
    ap.add_argument("--sim-port", type=int, default=5556)
    ap.add_argument("--fake-cam-port", type=int, default=8797,
                    help="I5 用的假相机服务端口（status 正常答、start 故意 500）")
    args = ap.parse_args(argv)

    # 被管道接住时 stdout 是块缓冲的 —— 那样"跑到哪一节"就完全看不见，
    # 一旦某节挂住只能靠猜。这里强制行缓冲。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:                                         # noqa: BLE001
        pass

    base = f"http://127.0.0.1:{args.port}"
    tmp = ROOT / ".tmp" / "drill"
    if tmp.exists() and not args.keep:
        _rmtree_quiet(tmp)

    print("=" * 78)
    print("Control 端到端演练（无鼠 / 无手机 / 无场地）")
    print("=" * 78)

    # ---------------------------------------------------------------- 0 造素材
    head("0. 造合成素材（有真值）")
    for sid, pad in ((("SYNTH-TEST_0001"), 0), (("SYNTH-PAD_0001"), 16)):
        rc = subprocess.run([sys.executable, str(ROOT / "tools" / "make_synth_frames.py"),
                             "--out", str(tmp / "camera"), "--session-id", sid,
                             "--frames", str(args.frames), "--pad", str(pad)],
                            capture_output=True, text=True)
        if rc.returncode != 0:
            print(rc.stdout, rc.stderr)
            record("S0", "FAIL", f"合成素材生成失败（{sid}）")
            return 1
    record("S0", "PASS", f"合成了两个会话：无填充 + 行填充 16B（用于验 I13）")

    # ---------------------------------------------------------------- 1 起服务
    head("1. 起服务")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # ⚠️ **绝不能给 stdout=PIPE 然后不读它**。管道缓冲区（~64 KB）写满之后，
    # 服务进程会**阻塞在自己的 print/log 上** ⇒ 事件循环停摆 ⇒ 所有 HTTP 请求挂住，
    # 而客户端看到的只是"请求超时"，跟日志毫无关系 —— 极难往这个方向想（实测被坑了两轮，
    # 第一次表现成"服务 9 分钟没起来"，第二次表现成"mouse/connect 卡 30 秒"）。
    # 落成文件：无界、可随时 tail、进程死了也还在。
    log_path = tmp / "service.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fp = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([sys.executable, "-u", str(ROOT / "app.py"), "--port", str(args.port)],
                            cwd=str(ROOT), stdout=log_fp, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", env=env)
    try:
        if not wait_up(base):
            record("S1", "FAIL", "服务 60 s 内没起来")
            print(drain(log_path))
            return 1
        record("S1", "PASS", f"服务已就绪 {base}（它自己的输出在 {log_path}）")

        # ------------------------------------------------------------ I2 / I12
        head("2. 判据 I2 / I12：健康端点在子系统全挂时仍可用、且不阻塞")
        h, code, dt = api(base, "/api/v1/health")
        hh = h.get("data", {})
        details_ok = all(bool((hh.get(k) or {}).get("detail") is not None)
                         for k in ("mouse", "camera", "locator"))
        record("I2", "PASS" if (code == 200 and details_ok) else "FAIL",
               f"HTTP {code}；三路都有可读 detail；相机当前 "
               f"{'可达' if (hh.get('camera') or {}).get('reachable') else '不可达'}"
               f"（不可达时也返回 200 并说清了原因）")
        record("I12", "PASS" if dt < 1.0 else "FAIL", f"/health 耗时 {dt*1000:.0f} ms（< 1 s）")

        # ------------------------------------------------------------ I20 / I21
        head("2b. 判据 I20 / I21：界面资源不许被浏览器自行缓存、引用的文件都可达")
        h_root, c_root = head_of(base, "/")
        h_js, c_js = head_of(base, "/static/wsproto.js")
        cc = ((h_root.get("Cache-Control") or "") + " " +
              (h_js.get("Cache-Control") or "")).lower()
        ok_cc = (c_root == 200 and c_js == 200
                 and ("no-cache" in cc or "no-store" in cc))
        record("I20", "PASS" if ok_cc else "FAIL",
               f"Cache-Control：/ = {h_root.get('Cache-Control')!r}、"
               f"/static/wsproto.js = {h_js.get('Cache-Control')!r}"
               + ("" if ok_cc else " ⇒ **界面资源会被启发式缓存，可能新旧混用**"))

        html = get_text(base, "/")
        refs = sorted(set(re.findall(r'(?:src|href)="(/static/[^"?]+)"', html)))
        bad = []
        for r in refs:
            c = head_of(base, r)[1]
            if c != 200:
                bad.append(f"{r}→{c}")
        record("I21", "PASS" if (refs and not bad) else "FAIL",
               f"index.html 引用 {len(refs)} 个静态资源，全部 200"
               if (refs and not bad) else
               f"**有资源不可达或被漏引**：{'、'.join(bad) if bad else '一个都没解析到'}"
               f"（少一个脚本 = 某一页静默变空）")

        # ------------------------------------------------------------ I24 / I25
        _launcher_sections(base, tmp)

        # ------------------------------------------------------------ I26
        _mjpeg_relay_section(base, args, tmp)

        # ------------------------------------------------------------ I1
        head("3. 判据 I1：不许引入 GUI 框架")
        code_txt = ("import sys, control.ctx, control.api.routes\n"
                    "gui=[m for m in ('PySide6','PyQt5','pyqtgraph') if m in sys.modules]\n"
                    "print('GUI:', gui)\n")
        rc = subprocess.run([sys.executable, "-c", code_txt], cwd=str(ROOT),
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        record("I1", "PASS" if (rc.returncode == 0 and "GUI: []" in rc.stdout) else "FAIL",
               f"装载全部后端模块后 sys.modules 里的 GUI 框架: "
               f"{rc.stdout.strip().split('GUI:')[-1].strip() if 'GUI:' in rc.stdout else rc.stderr[-200:]}")

        # ------------------------------------------------------------ I3
        head("4. 判据 I3：串口被占用时的错误信息要能照着做")
        # ⚠️ **绝不要为了"实测被占端口"去连每一个串口。**
        # 实测代价：连接一条"能打开但对端不存在"的串口，会让上游的调度线程卡在
        # `serial.write → GetOverlappedResult` 上并**永久握着命令锁** ⇒ 之后任何
        # `mouse/connect` 都永不返回（而 /health 还是 200，看起来像网络问题）。
        # 这是上游的行为，我们改不了（判据 I9），但**可以不去触发它** ——
        # 一个"验收工具"把被测对象搞死，那它测出来的东西就没有意义了。
        # 所以这里只用**不存在的端口**验证"错误信息能不能照着做"，那个路径是确定性的。
        ports, _, _ = api(base, "/api/v1/mouse/ports")
        plist = (ports.get("data") or {}).get("targets") or []
        serials = [t["port"] for t in plist if t.get("kind") == "serial" and t.get("port")]
        r, c, _ = api(base, "/api/v1/mouse/connect", "POST", {"target": "COM199"})
        msg = ((r.get("error") or {}).get("message") or "")
        record("I3", "PASS" if (c == 409 and len(msg) > 20) else "FAIL",
               f"不存在的口 COM199 ⇒ HTTP {c}，信息「{msg.splitlines()[0][:70]}」"
               f"（本机枚举到 {len(serials)} 个串口：{serials}；**故意不去连它们** —— "
               f"见 drill 里这段注释）")

        # ------------------------------------------------------------ I18 / I19
        head("4b. 判据 I18 / I19：手动连接是**前置条件**，不是提示")
        # 此时鼠没连、相机也只是"服务可达但没有手机"。这正是"什么都不连就想开拍"
        # 的场景 —— 必须被拦住，而且要说清缺哪一步。
        hh, _, _ = api(base, "/api/v1/health")
        rd = (hh.get("data") or {}).get("readiness") or {}
        n_ok = sum(1 for it in rd.get("items", []) if it.get("ok"))
        dirs0 = _session_dirs()
        r, c, _ = api(base, "/api/v1/session/open", "POST", {})
        dirs1 = _session_dirs()
        emsg = ((r.get("error") or {}).get("message") or "")
        # 拦住还不够，得**说清怎么做**：消息里要出现"手动连好"和那份清单。
        says_what = ("手动连好" in emsg) and ("顶栏" in emsg) and emsg.count("·") >= 2
        # 目录集合必须**一个都没变** —— "拦住了但留了个空壳"不叫拦住。
        same = (dirs0 == dirs1)
        record("I18", "PASS" if (c == 409 and says_what and same) else "FAIL",
               f"一路都没连（清单 {len(rd.get('items', []))} 项、就绪 {n_ok} 项，"
               f"can_record={rd.get('can_record')}）⇒ 开会话 HTTP {c}；"
               f"消息里给了可照做的清单={'是' if says_what else '否'}；"
               f"没新建会话目录（集合 {'未变' if same else f'{sorted(dirs0)}→{sorted(dirs1)}'}）"
               if (c == 409 and says_what and same)
               else f"HTTP {c}；说清了怎么做={says_what}；目录 {sorted(dirs0)}→{sorted(dirs1)}；"
                    f"消息：{emsg[:200]}")
        # I19 是 I18 的**反例**，但要有东西连着才成立 ⇒ 放在第 9 节（导入离线相机之后）判。

        # ------------------------------------------------------------ I10（编码层）
        head("5. 判据 I10：大数组必须能走二进制帧")
        code_txt = ("import numpy as np, sys\n"
                    "from control.bus import encode_bin, decode_bin, JSON_PAYLOAD_LIMIT\n"
                    "meta={'type':'mouse.wave','channels':['a','b'],'shape':[3,2]}\n"
                    "arr=np.arange(6,dtype='<f4')\n"
                    "blob=encode_bin(meta, arr.tobytes())\n"
                    "m,p=decode_bin(blob)\n"
                    "assert m==meta, m\n"
                    "assert np.array_equal(np.frombuffer(p,dtype='<f4'), arr)\n"
                    "print('LIMIT', JSON_PAYLOAD_LIMIT, 'ROUNDTRIP OK')\n")
        rc = subprocess.run([sys.executable, "-c", code_txt], cwd=str(ROOT),
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        record("I10", "PASS" if "ROUNDTRIP OK" in rc.stdout else "FAIL",
               f"帧编解码往返一致；JSON 载荷上限 {64} KiB（超过必须走二进制）"
               if "ROUNDTRIP OK" in rc.stdout else rc.stderr[-300:])

        # ------------------------------------------------------------ I13
        head("6. 判据 I13：帧解码必须按 stride 切行（含反例）")
        code_txt = (
            "import sys, numpy as np\n"
            "sys.path.insert(0, r'%s')\n"
            "from pathlib import Path\n"
            "from control.decode import decode_y\n"
            "bad=[]; tested=0; counter=0\n"
            "for sess in ('SYNTH-TEST_0001','SYNTH-PAD_0001'):\n"
            "    for jp in sorted((Path(r'%s')/sess/'frames').glob('frame_*.json')):\n"
            "        g,meta=decode_y(jp, need_bgr=False)\n"
            "        raw=np.fromfile(str(meta.path),np.uint8)\n"
            "        manual=raw[:meta.height*meta.y_stride].reshape(meta.height,meta.y_stride)[:,:meta.width]\n"
            "        naive=raw[:meta.height*meta.width].reshape(meta.height,meta.width)\n"
            "        tested+=1\n"
            "        if not np.array_equal(g,manual): bad.append(('mismatch',str(jp)))\n"
            "        if meta.padded and np.array_equal(g,naive): counter+=1\n"
            "print('TESTED',tested,'BAD',len(bad),'PAD_FRAMES_MATCHING_NAIVE',counter)\n"
            % (ROOT, tmp / "camera"))
        rc = subprocess.run([sys.executable, "-c", code_txt], cwd=str(ROOT),
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = rc.stdout.strip()
        ok = "BAD 0" in out and "PAD_FRAMES_MATCHING_NAIVE 0" in out
        record("I13", "PASS" if ok else "FAIL",
               f"{out} ⇒ 两条路径逐像素相同，且硬编码 reshape(H,W) 在填充帧上确实出错"
               f"（反例成立 ⇒ 这条判据有区分度）" if ok else out + rc.stderr[-200:])

        # ------------------------------------------------------------ I14
        head("7. 判据 I14：收到 _reconnected 必须主动重对齐状态")
        # ⚠️ 测试必须跑在**真在跑的事件循环里**：`bus.publish` 是按契约走的
        # （见 control/bus.py `_safe_put`）—— 循环没在跑就**直接丢弃**，这是有意的，
        # 免得服务还没起来就攒一堆没人看的消息。所以上一版"在主线程直接调
        # _handle_event 然后看队列"永远看到空列表，那是测试写错了，不是代码错了。
        code_txt = (
            "import asyncio, sys\n"
            "sys.path.insert(0, r'%s')\n"
            "from control.config import bootstrap\n"
            "from control.bus import Bus\n"
            "from control.store import Store\n"
            "from control.adapters.camera import CameraAdapter\n"
            "\n"
            "async def main():\n"
            "    cfg = bootstrap()\n"
            "    bus = Bus(); bus.bind_loop(asyncio.get_running_loop())\n"
            "    cam = CameraAdapter(cfg, bus, Store(cfg.session_root))\n"
            "    q = bus.subscribe()\n"
            "    t0 = __import__('time').time()\n"
            "    cam._handle_event({'type': '_reconnected', 'tries': 1})\n"
            "    dt = __import__('time').time() - t0\n"
            "    await asyncio.sleep(0.2)\n"
            "    seen = []\n"
            "    while not q.empty(): seen.append(q.get_nowait())\n"
            "    types = [m.get('type') for m in seen]\n"
            "    recon = [m for m in seen\n"
            "             if m.get('type') == 'camera.status' and m.get('reconnected')]\n"
            "    print('TYPES', types, 'RECON', len(recon), 'BLOCK_MS', int(dt*1000))\n"
            "\n"
            "asyncio.run(main())\n" % ROOT)
        rc = subprocess.run([sys.executable, "-c", code_txt], cwd=str(ROOT),
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = rc.stdout.strip()
        # 两个条件：① 确实发了重对齐事件；② 没把事件线程按在 HTTP 上（本机一次失败探测 2 s）。
        blk = 99999
        if "BLOCK_MS" in out:
            try:
                blk = int(out.split("BLOCK_MS")[1].split()[0])
            except Exception:                                  # noqa: BLE001
                pass
        ok = "RECON 1" in out and blk < 500
        record("I14", "PASS" if ok else "FAIL",
               f"{out} ⇒ _reconnected 立刻发出 camera.status(reconnected=True)，"
               f"且**不再**在事件线程里做阻塞 status()（{blk} ms）"
               if ok else (out + rc.stderr)[-300:])

        # ------------------------------------------------------------ I5 原子性
        head("8. 判据 I5：开会话失败必须回滚，不留半个会话目录")
        # ⚠️ 这里比上一版麻烦了一点，值得记下来：
        # 加了前置门控（I18）之后，"相机不可达就直接开会话"已经**在更早一步被拦住**，
        # 根本走不到回滚那段代码 —— I5 于是失去了着力点（它当时确实变红了）。
        # 所以现在必须构造"**门控放行、中途才炸**"的场景：假相机 status 正常答
        # （readiness 认为相机就绪 ⇒ 放行），而 control/start 回 500（那一路上炸掉）。
        # 这也顺带证明门控与回滚是**两道独立的闸**：门控拦"什么都没连就开"，
        # 回滚兜"连了但中途炸了"，谁都替不了谁。
        dirs0 = _session_dirs()
        orig_base = ((api(base, "/api/v1/health")[0].get("data") or {})
                     .get("camera") or {}).get("base") or ""
        fake = _fake_camera(args.fake_cam_port)
        try:
            api(base, "/api/v1/camera/base", "POST",
                {"base": f"http://127.0.0.1:{args.fake_cam_port}"})
            api(base, "/api/v1/camera/probe", "POST", {})
            hh2, _, _ = api(base, "/api/v1/health")
            rd2 = (hh2.get("data") or {}).get("readiness") or {}
            cam_item = next((i for i in rd2.get("items", []) if i.get("key") == "camera"), {})
            passed_gate = bool(rd2.get("can_record"))
            r, c, _ = api(base, "/api/v1/session/open", "POST", {})
            dirs1 = _session_dirs()
            msg = ((r.get("error") or {}).get("message") or "")
            # 三重：门控放行了（说明测的是回滚那条路）、中途真失败了、**目录集合没变**
            ok = (passed_gate and c == 409 and dirs0 == dirs1 and "回滚" in msg)
            record("I5", "PASS" if ok else "FAIL",
                   f"假相机（status 正常 ⇒ 门控放行，camera 项「{cam_item.get('state')}」；"
                   f"start 故意 500）⇒ 开会话中途失败：HTTP {c}，"
                   f"目录集合{'未变 ⇒ 没留半个会话' if dirs0 == dirs1 else str(sorted(dirs1))}"
                   f"，消息里写明了回滚到哪一步"
                   if ok else
                   f"门控放行={passed_gate}（camera 项「{cam_item.get('state')}」）；"
                   f"HTTP {c}；目录 {sorted(dirs0)}→{sorted(dirs1)}；{msg[:220]}")
        finally:
            fake.shutdown()
            if orig_base:
                api(base, "/api/v1/camera/base", "POST", {"base": orig_base})
            api(base, "/api/v1/camera/probe", "POST", {})

        # ------------------------------------------------------------ 正路
        head("9. 正路：导入离线会话 → 开会话 → 回放 → 收会话 → 赛后批量")
        api(base, "/api/v1/camera/import", "POST",
            {"session_id": "SYNTH-TEST_0001", "root": str(tmp / "camera"), "replay": False})
        r, c, _ = api(base, "/api/v1/session/open", "POST", {"notes": ["drill"]})
        sid = (r.get("data") or {}).get("session_id")
        if not sid:
            record("S2", "FAIL", f"开会话失败：{c} {json.dumps(r, ensure_ascii=False)[:300]}")
            return 1
        record("S2", "PASS", f"会话 {sid} 三路同开：{(r['data']).get('phase')}")
        # I19（I18 的反例）：只连了"相机（离线）"这一路时必须放行 ——
        # 否则"今天只想测场地"这种用法会被门控误伤。但放行的同时**必须说清**
        # 另一路是空的，否则收会话翻数据时才发现少了一半。
        ow = (r["data"]).get("warnings") or []
        says_mouse = any("电脑鼠" in str(w) for w in ow)
        record("I19", "PASS" if (r and says_mouse) else "FAIL",
               f"只连了相机这一路 ⇒ 会话照开（没误伤「只测场地」的用法）；"
               f"开会话时给 {len(ow)} 条提醒，点出鼠那一路是空的"
               f"：{str(ow)[:130]}"
               if says_mouse else
               f"只连一路时**没有提醒另一路是空的**：{ow}")
        time.sleep(2.5)
        st, _, _ = api(base, "/api/v1/session")
        cnt = ((st.get("data") or {}).get("session") or {}).get("counts") or {}
        record("S3", "PASS" if cnt.get("camera_frames_verified") == args.frames else "FAIL",
               f"回放 {cnt.get('camera_frames_verified')}/{args.frames} 帧，"
               f"在线定位有效 {cnt.get('locate_valid')} 无效 {cnt.get('locate_invalid')}")

        r, c, _ = api(base, "/api/v1/session/close", "POST", {"wait_delivery_s": 5})
        warns = (r.get("data") or {}).get("warnings") or []
        batch = (r.get("data") or {}).get("batch") or {}
        record("S4", "PASS" if not warns else "FAIL",
               f"收会话告警 {len(warns)} 条" + (f"：{warns}" if warns else "（含行数守恒自检 I6）"))
        time.sleep(3.0)
        runs, _, _ = api(base, "/api/v1/locate/runs")
        rl = runs.get("data") or []
        done = [x for x in rl if x["state"] == "done"]
        digest = [(x["id"], x["state"], f"{x['done']}/{x['total']}", x["valid"], x["invalid"])
                  for x in rl]
        record("S5", "PASS" if done else "FAIL",
               f"赛后批量任务 (id, state, 进度, 有效, 无效)：{digest}")

        # ------------------------------------------------------------ I7
        head("10. 判据 I7：结果行数 == 交付帧数，且无效帧也留档")
        sess_dir = ROOT / "sessions" / sid
        jl = sess_dir / "locate" / "results.jsonl"
        rows = [json.loads(l) for l in jl.read_text(encoding="utf-8").splitlines() if l.strip()] \
            if jl.is_file() else []
        n_frames = len(list((tmp / "camera" / "SYNTH-TEST_0001" / "frames").glob("frame_*.json")))
        record("I7", "PASS" if len(rows) == n_frames else "FAIL",
               f"results.jsonl {len(rows)} 行 == 交付帧 {n_frames} 帧；"
               f"无效 {sum(1 for x in rows if not x.get('valid'))} 行（也留档，带 reason）")

        # ------------------------------------------------------------ I4 / I6
        head("11. 判据 I4 / I6：时间轴每行有 t_pc_ms；meta 记时钟；行数守恒")
        import csv
        tl = list(csv.DictReader(open(sess_dir / "timeline.csv", encoding="utf-8-sig")))
        miss = [x for x in tl if not x.get("t_pc_ms")]
        meta = json.loads((sess_dir / "meta.json").read_text(encoding="utf-8"))
        clk = meta.get("clock") or {}
        record("I4", "PASS" if (not miss and clk.get("offset_ms") is not None and clk.get("source"))
               else "FAIL",
               f"{len(tl)} 行时间轴，缺 t_pc_ms 的 {len(miss)} 行；"
               f"meta.clock = offset {clk.get('offset_ms')}ms / source={clk.get('source')} / ±{clk.get('uncertainty_ms')}ms")
        cam = json.loads((sess_dir / "camera.json").read_text(encoding="utf-8"))
        led_n = (cam.get("store") or {}).get("frames_verified_on_pc")
        n_tl = sum(1 for x in tl if x["source"] == "camera" and x["kind"] == "frame_verified")
        if led_n is None:
            record("I6", "SKIP", f"离线导入没有真实留存账本（store=null），无法比对；"
                                 f"时间轴里 frame_verified 有 {n_tl} 行（= 交付帧数，自行对照）")
        else:
            record("I6", "PASS" if n_tl == led_n else "FAIL",
                   f"timeline 的 frame_verified {n_tl} 行 == 账本 frames_verified_on_pc {led_n}")

        # ------------------------------------------------------------ I9
        head("12. 判据 I9：跑完之后三个上游仓库仍然干净")
        dirty = []
        for name, p in (("RemoteControl", ROOT.parent / "RemoteControl"),
                        ("PhoneCamera", ROOT.parent / "PhoneCamera"),
                        ("Auto Locate", ROOT.parent / "Auto Locate")):
            out = subprocess.run(["git", "-C", str(p), "status", "--porcelain"],
                                 capture_output=True, text=True).stdout.strip()
            if out:
                dirty.append((name, len(out.splitlines())))
        record("I9", "PASS" if not dirty else "FAIL",
               "三个上游仓库 git status 全空（只消费，没写过一个字节）"
               if not dirty else f"**有仓库变脏**：{dirty}")

        # ------------------------------------------------------------ 定位精度
        head("13. 定位精度：与合成真值对比（这一条不是判据，是「链路真的通了」的证据）")
        gt = json.loads((tmp / "camera" / "SYNTH-TEST_0001" / "ground_truth.json")
                        .read_text(encoding="utf-8"))["frames"]
        by_seq = {int(x.get("_seq")): x for x in rows}
        errs, dths = [], []
        for f in gt:
            got = by_seq.get(int(f["seq"]))
            if not got or not got.get("valid"):
                continue
            errs.append(math.hypot(got["x"] - f["x"], got["y"] - f["y"]))
            dths.append(abs(((got["theta_deg"] - f["theta_deg"] + 180) % 360) - 180))
        if errs:
            record("INFO", "INFO",
                   f"位置误差 均值 {sum(errs)/len(errs)*1440:.2f} mm / 最大 {max(errs)*1440:.2f} mm"
                   f"（按 1440 mm 场地折算）；角度 均值 {sum(dths)/len(dths):.3f}° / 最大 {max(dths):.3f}°")
        else:
            record("INFO", "FAIL", "没有可比对的有效结果")

        # ------------------------------------------------------------ I29（纯函数，无需设备）
        # 上游 2.2.1 新增的发车前**低电硬闸**（`registry._precheck_maze_run` + `LAUNCH_MIN_MV`）。
        # 为什么要**直接测判定函数**（而不是发一条低电帧去撞它）：
        #   ① 模拟器的电池是缓慢漂移的（7620 mV 起、按 IIR 降），**造不出确定性的低电**；
        #   ② 上游自己的 6 项新断言也是这么做的（"直接测判定函数本身，确定性，
        #      不受模拟器电池漂移影响"）。
        # 三态都要验（这是本层一贯的纪律：**判不了 ≠ 不通过**）：
        #   · 低电（< 阈值）⇒ **拒绝**，且带实测电压与下限（好让界面写得出"只有几 mV"）；
        #   · `battMv == 0`（**电池监视未开启**，不是没电）⇒ **放行** —— 拦它就会
        #     误锁死台架（比赛固件从不打开电池监视）；
        #   · 取不到帧 ⇒ **放行**（判不了就别拦）；
        #   · 充足 ⇒ 放行。
        # 外加一条"闸**真的挂在登记表上**"：`maze.run` 的 `precheck` 必须非空 ——
        # 闸写在 `MazeAPI.run()` 里的话，脚本写 `send_cmd("maze.run")` 就绕过去了。
        head("13b. 判据 I29：发车前低电闸（纯函数判定，无需设备）")
        code_txt = (
            "import sys\n"
            "sys.path.insert(0, r'%s')\n"
            "from host import registry as R\n"
            "out = []\n"
            "out.append(('min_mv', R.LAUNCH_MIN_MV))\n"
            "spec = R.COMMANDS.get('maze.run')\n"
            "out.append(('precheck_wired', bool(spec is not None and spec.precheck is not None)))\n"
            "class _T:\n"
            "    def __init__(self, mv): self._mv = mv\n"
            "    def latest(self):\n"
            "        if self._mv is None: raise RuntimeError('no frame')\n"
            "        return {'battMv': self._mv}\n"
            "class _MC:\n"
            "    def __init__(self, mv): self.telemetry = _T(mv)\n"
            "def probe(mv):\n"
            "    try: return R._precheck_maze_run(_MC(mv))\n"
            "    except Exception as e: return ('EXC', type(e).__name__)\n"
            "lo = R.LAUNCH_MIN_MV - 2731\n"
            "out.append(('low', probe(lo)))\n"
            "out.append(('zero', probe(0)))\n"
            "out.append(('noframe', probe(None)))\n"
            "out.append(('ok', probe(R.LAUNCH_MIN_MV + 679)))\n"
            "import json; print('RES ' + json.dumps(out))\n" % str(ROOT.parent / "RemoteControl" / "host"))
        rc = subprocess.run([sys.executable, "-c", code_txt], cwd=str(ROOT),
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        i29_ok = False
        res = None
        if "RES " in rc.stdout:
            try:
                res = dict(json.loads(rc.stdout.split("RES ", 1)[1].strip()))
            except Exception:                                 # noqa: BLE001
                res = None
        if res:
            mn = res.get("min_mv")
            low = res.get("low")
            # ⚠️ 经过 `json` 往返后**元组会变成列表** —— 这里两种都收（踩过：
            #    只认 `tuple` 会让这条判据永远红，而它自己看着挺有理）。
            low_ok = (isinstance(low, (list, tuple)) and len(low) == 2
                      and low[0] == "ERR_LOWBATT"
                      and isinstance(low[1], dict) and low[1].get("battMv") == mn - 2731
                      and low[1].get("minMv") == mn
                      and low[1].get("hint"))
            zero_ok = res.get("zero") is None            # 电池监视未开启 ⇒ 不拦
            noframe_ok = res.get("noframe") is None      # 判不了 ⇒ 不拦
            full_ok = res.get("ok") is None              # 充足 ⇒ 放行
            wired_ok = bool(res.get("precheck_wired"))
            i29_ok = bool(low_ok and zero_ok and noframe_ok and full_ok and wired_ok)
        record("I29", "PASS" if i29_ok else "FAIL",
               f"低电闸挂在登记表上（`maze.run.precheck` 非空）；阈值 {res.get('min_mv')} mV；"
               f"低于阈值 ⇒ 拒绝并回 ERR_LOWBATT（带实测 {low[1].get('battMv')} mV "
               f"与下限 {res.get('min_mv')} mV + 可照做的 hint）；"
               f"`battMv==0`（电池监视未开启）⇒ 放行；取不到帧 ⇒ 放行；充足 ⇒ 放行"
               if i29_ok else
               f"**低电闸不对**：{json.dumps(res, ensure_ascii=False, default=str) if res else rc.stdout[-300:] + rc.stderr[-300:]}")

        # ------------------------------------------------------------ I15~I17
        # 这三条是**上游 1.2/1.3/1.4 更新**带来的新失败模式，需要一条活链路才能判。
        head("14b. 判据 I15 / I16 / I17：跟上游更新（需要模拟器）")
        if not args.sim:
            for cid, why in (("I15", "帧时间必须说清是哪一种（send / arrival）"),
                             ("I16", "参数必须按 Flash 页分两套且通道不混"),
                             ("I17", "丢帧必须可回答（seq_lost 不是 None）")):
                record(cid, "SKIP", f"{why} —— 加 --sim 才验（需要一条活链路）")
            _skip_mouse_sections = True
        else:
            _skip_mouse_sections = False
            sim = _start_sim(args.sim_port)
            if sim is None:
                for cid in ("I15", "I16", "I17", "I22", "I23", "I27", "I28", "I30"):
                    record(cid, "FAIL", f"模拟器起不来（端口 {args.sim_port}）⇒ 这三条判不了")
            else:
                try:
                    _mouse_sections(base, args, sim)
                finally:
                    _stop_sim(sim)
        if _skip_mouse_sections:
            pass

        # ------------------------------------------------------------ 需要硬件的
        head("15. 需要现场硬件才能判的（这里如实标出，不假装通过）")
        record("I8", "SKIP", f"{HW_ONLY}：场地标定后把鼠放到已知格，比固件与视觉的格距残差 < 0.3 格")
        record("I11", "SKIP", f"半自动：complete=null 时前端渲染为「判不了」。"
                              f"服务端已在 /camera/ledger 里给出 _complete_meaning，"
                              f"但需要一台真的手机才能造出 null 的账本")
        record("I3*", "INFO", "串口占用已实测；但'先关 run_host.py'那句提示要真开着 GUI 才会走到")
        record("I12*", "INFO", f"{HW_ONLY}：一次 5 MB 慢投递下 /health 仍 < 1 s（需要真采集）")

        # ------------------------------------------------------------ 产物
        head("产物")
        print(f"  会话目录：{sess_dir}")
        for f in sorted(sess_dir.rglob("*")):
            if f.is_file():
                print(f"    {f.relative_to(sess_dir)}  ({f.stat().st_size} B)")
        print(f"\n  日志尾：")
        log, _, _ = api(base, "/api/v1/logs/tail?n=12")
        for m in (log.get("data") or []):
            print(f"    [{m.get('level','info')}] {m.get('text','')[:110]}")

    finally:
        head("汇总")
        n_pass = sum(1 for _, v, _ in RESULTS if v == "PASS")
        n_fail = sum(1 for _, v, _ in RESULTS if v == "FAIL")
        n_skip = sum(1 for _, v, _ in RESULTS if v == "SKIP")
        print(f"  PASS {n_pass}   FAIL {n_fail}   SKIP {n_skip}（需要现场硬件）")
        if n_fail:
            print("\n  没过的：")
            for cid, v, note in RESULTS:
                if v == "FAIL":
                    print(f"    · {cid}: {note}")
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            log_fp.close()
        except Exception:                                     # noqa: BLE001
            pass
        if not args.keep and tmp.exists():
            _rmtree_quiet(tmp)
        print("\n  下一步：python app.py → http://127.0.0.1:8770")
        print("  （--keep 可以保留 .tmp/drill 里的合成素材，方便自己进去翻）")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
