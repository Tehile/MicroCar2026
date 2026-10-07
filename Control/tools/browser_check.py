"""无头浏览器实测：把七个页面真的渲染出来，抓 JS 异常与坏请求。

    python tools/browser_check.py                 # 默认查 http://127.0.0.1:8770
    python tools/browser_check.py --port 8897
    python tools/browser_check.py --shots .tmp/shots   # 把每页截屏存下来

**为什么要单独有这个脚本**：一遍 `py_compile` 只能证明"Python 语法没错"，
`curl /` 只能证明"HTML 字节发出去了"。而前端是 **7 个 JS 文件 + Canvas 自绘**，
一个 `undefined.foo` 就足以让某一页白屏 —— 而它不会在服务端日志里留下任何痕迹。
判据里 P0 的验收写的是"浏览器能打开并看到三灯"，那就要有一台真的浏览器去看。

**为什么不用 Playwright**：本机没装，装它要拉一个 ~150 MB 的内核。而 Chrome 本来就有，
CDP 又是纯 WebSocket —— `aiohttp`（本项目已有依赖）就能当客户端。
少一个依赖，多一条能随时重跑的命令。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from control.store import _rmtree_quiet      # noqa: E402  「绝不抛异常」的删除器

#: 界面的主区是**两级**：**入口页**只做导航（四个子系统各一组，组内是模块卡片），
#: 点卡片**单开一个整屏模块界面**，点【← 返回】回入口。**没有选项卡**，也不开新标签。
#: 「综合运动」进去后是**两栏 + 一个可收纳底栏**：左栏相机、右栏小车、底栏整合（⑨数据）。
#: 下面这几张表必须与 index.html 的容器/按钮顺序、control/static/app.js 的 RAIL_OF
#: 一致，并且 `len(TABS) + len(DOCK_VIEWS) == EXPECTED_VIEWS`（= 9，app.js 里那份）。
#: 三处一起改，少改一处会当场报出来。
#:
#: ⚠️ 逐页断言打的是「综合运动」界面里的九页 —— 它们平时在 `display:none` 的
#: `#ws-all` 里，**必须先 `C.openMod('all','@all')` 打开**（不可见元素的 `innerText`
#: 是空字符串，不打开的话每条断言都会读到空）。
SYS_TABS = ["mouse", "cam", "loc", "all"]    # 入口页分组（顺序 = app.js 的 SYS_DEF / index.html）
TAB_CAM = ["arena", "capture"]
TAB_MOUSE = ["params", "calib", "depart", "exp", "scope", "console"]
TABS = TAB_CAM + TAB_MOUSE                   # 两栏 nav 里的按钮（顺序 = index.html）
DOCK_VIEWS = ["data"]                        # 底部整合栏里的视图（没有按钮，展开即激活）
RAIL_OF_PY = {t: "cam" for t in TAB_CAM}
RAIL_OF_PY.update({t: "mouse" for t in TAB_MOUSE})
RAIL_OF_PY.update({t: "dock" for t in DOCK_VIEWS})


def tab_click_js(tab: str) -> str:
    """点开某一页 —— 两栏各自有自己的页签，所以要先按归属栏找到那个按钮。
    底栏的 ⑨数据 没有按钮（它是"展开就激活"），走 `C.activate()`。"""
    rail = RAIL_OF_PY[tab]
    if rail == "dock":
        return f"C.activate('{tab}')"
    return f"document.querySelector('#rail-{rail} button[data-tab=\"{tab}\"]').click()"


#: 每个页面的**针对性断言**：光有 HTML 字节数不够 —— "有 5000 字节"可能全是空表格。
#: 这里检查"那些关键的控件/字段真的在，而且有值"。
#: 表达式返回 {ok: bool, note: str}；ok=False 会让该页 FAIL。
PAGE_PROBES = {
    "arena": ("(()=>{const w=document.querySelector('#a-watermark');"
              "return {ok: !!w, note: '标定水印=' + (w?w.textContent:'缺失')};})()"),
    # 采集页：账本里几个数必须**各占一行**（合成一个数现场会选错处置），
    # 而"没有账本数据"要显示成一句话而不是一张空表。
    # 相机不可达时**不发请求**是本页的设计（每个端点要干等 2 s），那时只要
    # 它写明了原因、并给出「重探」按钮，就算合格 —— 空表格才是问题。
    "capture": ("(()=>{const ov=document.querySelector('#cg-ov');"
                "const sg=document.querySelector('#cg-status');"
                "const lg=document.querySelector('#cg-ledger');"
                "const t=lg?(lg.innerText||'').trim():'';"
                "const o=ov?(ov.innerText||''):'';"
                "const gated=o.indexOf('相机服务不可达')>=0;"
                "const rows=sg?sg.querySelectorAll('b').length:0;"
                "return {ok: !!ov && !!sg && (!!t || gated || rows>0),"
                " note:'ov='+(ov?1:0)+' sg='+(sg?1:0)+' gated='+(gated?1:0)"
                "  +' 状态行='+rows+' 账本='+t.length+'字 环境行=「'+o.slice(0,28)+'」'};})()"),
    # 参数页必须**真的分成两组**（生产标定 7 / 课程调参 14）—— 这是跟上游更新的要点。
    # 但未连鼠时读不到参数，这时**要求它明确写出原因**，而不是摆一张空表
    # （"空白"和"没连"是两件事，混起来最费时间）。
    "params": ("(()=>{const t=document.querySelector('#p-groups').innerText||'';"
               "const hasProd=t.indexOf('生产标定')>=0, hasLes=t.indexOf('课程调参')>=0;"
               "const gated=t.indexOf('未连接电脑鼠')>=0||t.indexOf('无法读取参数')>=0;"
               "const n=(t.match(/\\\\bid\\\\b/g)||[]).length;"
               "return {ok: (hasProd&&hasLes)||gated,"
               " note:(hasProd&&hasLes)?('分组都在，项数标记 '+n)"
               " :'未连鼠 ⇒ 已写明原因（没冒充空表）'};})()"),
    "calib": ("(()=>{const t=document.querySelector('.view[data-tab=\\\"calib\\\"]').innerText||'';"
              "return {ok: t.indexOf('flip')>=0||t.length>400, note:'标定向导 '+t.length+' 字'};})()"),
    # 发车页：时间同步面板 + 链路完整性面板都必须有行；
    # 未连鼠时本页会**早退**（时间同步要读设备的帧），那就要求它把原因写在 gatehint 里。
    "depart": ("(()=>{const s=document.querySelector('#d-sync'),"
               "l=document.querySelector('#d-link'),e=document.querySelector('#d-explain'),"
               "h=document.querySelector('#d-sync-hint');"
               "const rows=(n)=>n?n.querySelectorAll('b').length:0;"
               "const gated=h?((h.innerText||'').indexOf('未连接电脑鼠')>=0):false;"
               "return {ok: (rows(s)>=8 && rows(l)>=5) || gated,"
               " note:'同步字段 '+rows(s)+' / 链路字段 '+rows(l)+' / explain='+(e?e.textContent.length:0)+'字'"
               " +' / hint='+(h?('「'+(h.innerText||'').slice(0,24)+'」'):'缺')"
               " +(gated?'（未连鼠，已写明原因）':'')};})()"),
    # 实验页：状态行有值 + 实验下拉里真的有上游给的清单（来自 LESSON_EXPS）
    # 实验页：**清单与理论值来自上游的静态表，不依赖连接**，所以那部分必须始终在
    # （这正是"没插硬件也能把页面看全"的要求）；而"前置状态"未连时可以只写一句原因。
    # ⚠️ **2.4.0 上游已删除课程调参整层**（`LESSON_EXPS` 不复存在）⇒ 这一页只剩电机实验：
    #    此时课程那栏被换成 `#x-lesson-gone` 的一句说明、下拉本身收起（0 个选项）。
    #    ⇒ 判据改**两支**：课程层在 ⇒ 下拉 ≥4 项；课程层已删 ⇒ `#x-lesson-gone` 有文字
    #      **且点明"2.4.0 已删除"**（否则就是把一块空白的栏当成了正常）。理论值同理随层删。
    "exp": ("(()=>{const st=document.querySelector('#x-status'),"
            "sel=document.querySelector('#x-exp'),gone=document.querySelector('#x-lesson-gone');"
            "const opts=sel?Array.from(sel.options).map(o=>o.value).filter(v=>v!==''):[];"
            "const th=(document.querySelector('#x-theory')||{}).textContent||'';"
            "const nb=st?st.querySelectorAll('b').length:0;"
            "const gated=st?((st.innerText||'').indexOf('需连接电脑鼠')>=0):false;"
            "const gtxt=gone?(gone.innerText||''):'';"
            "const goneShown=!!(gone&&gone.offsetParent!==null&&gtxt.indexOf('2.4.0')>=0);"
            "const course=opts.length>=4;"
            "return {ok: goneShown || (course && (nb>=5||gated)),"
            " note:(goneShown?'课程层已删（2.4.0）：'+gtxt.slice(0,40)+'…':'实验 '+opts.length+' 个')"
            "+' / 状态字段 '+nb+(gated?'（未连鼠，已写明）':'')"
            "+' / 理论值'+(th.indexOf('62.22')>=0?'有':(goneShown?'随层删':'缺'))};})()"),
    # 波形页：2.0.0 只剩**一条统一帧**，dump 那半块已随 `SCOPE`/`DUMP` 一起删除。
    # 所以这里判的是"实时画布 + 信息行 + 游标区"三件东西在不在（而不是旧的信息行）。
    "scope": ("(()=>{const cv=document.querySelector('#s-live'),"
              "info=document.querySelector('#s-info'),"
              "cur=document.querySelector('#s-cur');"
              "return {ok: !!(cv&&info&&cur),"
              " note:'实时画布'+((cv)?'在':'缺')+' / 信息行'+((info)?'在':'缺')"
              "+' / 游标区'+((cur)?'在':'缺')};})()"),
    "console": ("(()=>{const a=document.querySelector('#k-device'),"
                "b=document.querySelector('#k-service');"
                "return {ok: !!a&&!!b, note:'文本流/服务日志区都在'};})()"),
    # 数据页：时间轴表头必须带上新增的两列
    "data": ("(()=>{const h=document.querySelector('#d-timeline');"
              "const t=h?h.innerText:'';"
              "return {ok: !!h, note:'时间轴表在'};})()"),
}

#: 门控核对表：(代号, CSS 选择器, 需要哪一路, 人话名字)。
#: 这是一张**判据表**，不是待办清单 —— 以后新加一个"要连设备才能用"的按钮，
#: 如果忘了在视图里门控它，这里就会红。所以宁可写全一点。
GATE_CHECKS = [
    ("B14a", "#d-arm", "mouse", "⑤ 发车（ARM）"),
    ("B14b", "#d-maze", "mouse", "⑤ 远程发车跑迷宫"),
    ("B14c", "#x-lab-run", "mouse", "⑥ 电机实验「跑一次」"),
    # ⚠️ B14d 在 2.4.0 上游上**判的是隐藏的按钮**（课程那栏被 `#x-lesson-body`
    # 整体收起）—— 但**没有删它**：本层是整合层，要能在 2.3.x 上游上跑，
    # 那时这个按钮是可见且需要门控的。`disabled` 检查对隐藏元素照样成立，
    # 所以它不假绿（真未门控时仍然会红）。
    ("B14d", "#x-lesson-run", "mouse", "⑥ 课程实验「跑实验」（2.4.0 起该栏收起，仍守 2.3.x）"),
    # ⚠️ 原 B14e（`#s-dump`）**已删除**：2.0.0 起没有"拉 dump"这个动作（`SCOPE`/`DUMP`
    # 整套删除），门控一个不存在的按钮等于给自己留一条永远绿的空断言。
    ("B14f", "#p-reload", "mouse", "③ 读全部参数"),
    ("B14g", "#c-addobs", "mouse", "④ 记一次观测"),
    ("B14h", "#k-send", "mouse", "⑧ 发原始命令"),
    ("B14i", "#cg-start", "camera", "② 采集 start"),
    ("B14j", "#cg-stop", "camera", "② 采集 stop"),
]

def read_health(base: str) -> dict:
    with urllib.request.urlopen(base + "/api/v1/health", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))["data"]


def _post(base: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(base + path, method="POST",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def _synth_maze_doc() -> dict:
    """造一份**合成**的迷宫记录（B35 用）—— 形状与 `control/maze_rec.py` 的落盘一致。

    为什么要造的而不是用真跑出来的：这一条要验的是"导入 → 复盘 → 游标回放"这条**链路**，
    不是"模拟器跑得像不像"。造的数据能把关键点**钉住**：
      · `posX` **逐帧变化** ⇒ 拖游标时左栏读数必须跟着变（否则那条断言是空验证）；
      · `mazeTask` 中间变一次 ⇒ 时间线上必须出现一条迁移；
      · `wall*` 三面随位置不同 ⇒ 中栏那张图必须真的被重建出来。
    """
    channels = ["posX", "posY", "dir", "wallLeft", "wallFront", "wallRight",
                "mazeTask", "road", "speedL", "speedR", "irF1", "irL", "irR",
                "gyroAngle", "encAngle", "goalX", "goalY", "motionTask", "battMv"]
    frames, t_ms = [], []
    for i in range(20):
        x = min(3, i // 5)               # 0,0,0,0,0,1,1,… → 位置会变
        y = i % 5
        task = 2 if i < 12 else 3        # 中间变一次 ⇒ 时间线要有一条迁移
        row = {c: 0 for c in channels}
        row.update({
            "posX": x, "posY": y, "dir": (1 if i >= 8 else 0),
            "wallLeft": 1 if (i % 3 == 0) else 0,
            "wallFront": 1 if (i % 4 == 0) else 0,
            "wallRight": 1 if (i % 5 == 0) else 0,
            "mazeTask": task, "road": 1 + (i % 7),
            "speedL": 30 + i, "speedR": 31 + i,
            "irF1": 100 + i * 3, "irL": 200 - i, "irR": 180 + i,
            "gyroAngle": i * 2, "encAngle": -i * 3,
            "goalX": 6, "goalY": 6, "motionTask": 1, "battMv": 3900,
        })
        frames.append([row[c] for c in channels])
        t_ms.append(i * 200)
    return {
        "format": "control.maze-run/1",
        "created_ms": int(time.time() * 1000),
        "start_ms": int(time.time() * 1000) - 4000,
        "end_ms": int(time.time() * 1000),
        "duration_ms": 3800,
        "why": "b35-synth", "end_why": "b35",
        "mazetype": 7,
        "channels": channels,
        "t_ms": t_ms,
        "frames": frames,
        "changes": [{"t_ms": int(time.time() * 1000) - 4000 + 2400, "from": 2, "to": 3}],
        "events": [{"t_ms": int(time.time() * 1000) - 500, "event": "STOPPED",
                    "reason": "END", "seq": 7, "status": "END"}],
        "integrity": {"frames": len(frames), "t0_pc_ms": int(time.time() * 1000) - 4000,
                      "evq_seen": False, "drop_seen": False, "tiny": False},
        "wall": {"n": 7,
                 "known_rows": ["8880000"] * 7,
                 "wall_rows": ["8000000"] * 7,
                 "visited": ["1000000", "1000000", "1000000", "1000000",
                             "1000000", "1000000", "1000000"],
                 "track": [[0, 0], [0, 1], [0, 2]]},
    }


def _svc_state(base: str) -> dict:
    """读代启器现状（零副作用）。失败返回空 dict —— 调用方按"不知道"处理。"""
    try:
        with urllib.request.urlopen(base + "/api/v1/camera/service", timeout=8) as r:
            return (json.loads(r.read().decode("utf-8")) or {}).get("data") or {}
    except Exception:                                         # noqa: BLE001
        return {}


def _port_open(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.4)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _fake_camera_upstream(port: int = 8765):
    """起一个**假的上游相机服务**（纯 stdlib），只为一件事：让配对二维码真画一遍。

    为什么必须在检查器里起它：`/camera/pair` 的内容是**整包**来自上游相机服务的
    （hostlink 只是转发 `/api/v1/pair`）。没有服务时前端只能走"不可达"的降级分支，
    于是「二维码真的画出来了」这条判据永远验不到 —— 而它恰恰是最容易悄悄坏掉的
    那类东西（Canvas 尺寸、矩阵行长度、静区，任何一处错了都只是"扫不出来"）。

    监听 `8765`（= config.yaml 里 `camera.base` 的默认地址）⇒ **不需要改任何配置**，
    也就不会把现场配置改坏。若 8765 上已经有真服务，直接用它（真的更好）。

    返回 `(server | None, 说明)`。
    """
    import http.server
    import threading

    if _port_open(port):
        return None, f"{port} 上已有相机服务，直接用真的"
    try:
        import qrcode                                    # 上游同款，生成真矩阵
    except Exception:                                    # noqa: BLE001
        qrcode = None

    url = f"http://127.0.0.1:{port}/?token=FAKE-TOKEN"
    if qrcode is not None:
        q = qrcode.QRCode(border=4, box_size=1)           # border=4 就是静区，与上游口径一致
        q.add_data(url)
        q.make(fit=True)
        mtx = q.get_matrix()
        rows = ["".join("1" if c else "0" for c in row) for row in mtx]
    else:                                                 # 兜底：只验绘制通路，不冒充真码
        n = 25
        rows = ["0" * n] * 4 + [
            "0" * 4 + "".join("1" if ((x + y) % 3 == 0) else "0" for x in range(n - 8)) + "0" * 4
            for y in range(n - 8)] + ["0" * n] * 4

    pair_body = {
        "url": url,
        "pair_uri": f"mousecam://pair?token=FAKE-TOKEN&host=127.0.0.1&port={port}",
        "token": "FAKE-TOKEN",
        "port": port,
        "hosts": [{"ip": "127.0.0.1", "port": port, "recommended": True,
                   "adapter": "fake", "virtual": False}],
        "qr_matrix": {"size": len(rows), "border": 4, "bit": 1, "rows": rows},
    }
    caps = {"server_version": "fake-1.0", "protocol_version": 1,
            "console": {"features": ["pair", "status"]}}
    status_body = {
        "ok": True, "state": "idle", "server_version": "fake-1.0",
        "protocol_version": 1,
        "device": {"clients": 1, "connected": True, "model": "FakePhone",
                   "app_version": "0.1-fake", "serial": "FAKE"},
        "session": None,
        "store": {"frames_expected": 0, "frames_on_device": 0,
                  "frames_verified_on_pc": 0, "gaps_on_device": 0,
                  "upload_gaps": 0, "every_frame": True, "complete": None},
    }
    sessions_body = {"sessions": [], "profile_id": "fake", "data_dir": "(fake)",
                     "ws_clients": 1}
    # `monitor` 段（判据 B33 要靠它）：上游用它区分"我推没推过画面"。
    # 做成可切换的，好让判据**两个方向都验**：推过 / 从没推过。
    mon_holder = {"m": {"subscribers": 0, "panel_subscribers": 0, "thumbs": 3,
                        "interval_ms": 1000,
                        "last_thumb": {"seq": 3, "session_id": "fake-sess",
                                       "bytes": 2048, "lane": "thumb",
                                       "color": False, "at": "2026-10-04T00:00:00"}}}
    mon_never = {"subscribers": 0, "panel_subscribers": 0, "thumbs": 0,
                 "interval_ms": None, "last_thumb": None}

    class _Srv(http.server.ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            # 客户端（Control 的 WS 事件订阅 / MJPEG 代理探针）中途断掉是**常态**，
            # socketserver 默认会把 traceback 打到检查器屏幕上，把结论淹掉。
            pass

    class _H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):                        # 静音，别刷检查器的屏
            pass

        def _json(self, obj, code=200):
            b = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _mjpeg(self):
            """发几帧真 JPEG（boundary 故意用**真上游那个非常规值**）。

            为什么这条要造出来：判据 B32 问的是"浏览器**真的把画面渲染出来了**吗"，
            而那正是现场报过的问题（手机在发、反代在转、预览窗口空白）。
            用一个写死的 `boundary=frame` 就永远测不到 —— 必须让假上游用
            `phonecamera-frame`，与真上游一致。
            """
            self.send_response(200)
            self.send_header(
                "Content-Type", f"multipart/x-mixed-replace; boundary={_MJPEG_BOUNDARY}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                for i in range(40):              # 够长：浏览器要来得及渲染
                    head = (f"--{_MJPEG_BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                            f"Content-Length: {len(_TINY_JPEG)}\r\nX-Seq: {i}\r\n\r\n"
                            ).encode("ascii")
                    self.wfile.write(head + _TINY_JPEG + b"\r\n")
                    self.wfile.flush()
                    time.sleep(0.2)
            except Exception:                    # noqa: BLE001  消费方走了是正常事件
                pass

        def do_GET(self):
            p = self.path.split("?")[0]
            if p == "/api/v1/capabilities":
                return self._json(caps)
            if p == "/api/v1/host/status":
                body = dict(status_body)
                # `subscribers` 在真上游是"当前有几个 MJPEG 订阅者"，
                # 这里跟着"有没有人在看"模拟：判据切换 never 时置 1（= 有人在订、上游却不推）。
                m = dict(mon_holder["m"])
                m["subscribers"] = 1 if "?s=1" in self.path or m.get("last_thumb") else 1
                body["monitor"] = m
                return self._json(body)
            if p == "/__monitor":
                mon_holder["m"] = dict(mon_never if "never=1" in self.path
                                       else {"subscribers": 1, "panel_subscribers": 0,
                                             "thumbs": 3, "interval_ms": 1000,
                                             "last_thumb": {"seq": 3, "session_id": "fake",
                                                            "bytes": 2048, "lane": "thumb",
                                                            "color": False,
                                                            "at": "2026-10-04T00:00:00"}})
                return self._json({"ok": True, "monitor": mon_holder["m"]})
            if p == "/api/v1/sessions":
                return self._json(sessions_body)
            if p == "/api/v1/pair":
                return self._json(pair_body)
            if p == "/stream/mjpeg":
                return self._mjpeg()
            # 未知路径一律 200 + {}：检查器把 5xx 当硬伤，
            # 而这里缺哪个端点**不是被测对象的问题**，别让它污染结论。
            return self._json({"ok": True, "fake": True})

        def do_POST(self):
            return self._json({"ok": True, "fake": True, "sent_to": 1})

    srv = _Srv(("127.0.0.1", port), _H)
    threading.Thread(target=srv.serve_forever, daemon=True, name="fake-cam").start()
    return srv, f"已起假上游相机 127.0.0.1:{port}（{len(rows)}×{len(rows)} 矩阵，供绘制通路验证）"


CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

RESULTS: list = []

#: 判据 B32 用的 1×1 真 JPEG，以及**故意的非常规 boundary**（= 真上游 `server.py`
#: 里的 `MJPEG_BOUNDARY`）。boundary 用非常规值才有意义：反代一旦把它写死成别的
#: （比如 `frame`），浏览器就**一帧都切不出来** —— 而这正是现场踩过的那个 bug。
_MJPEG_BOUNDARY = "phonecamera-frame"
_TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==")

#: 只有**未捕获的 JS 异常**才算硬伤。而 `console.error` / 4xx 在"什么都没插"的
#: 时候是**预期行为** —— 这个工具是给现场用的，它必须能分清"坏了"和"就是没插硬件"，
#: 否则它永远红着，也就没人看了。
SOFT_API_PREFIX = "/api/v1/"


def is_hard_request(bad: dict) -> bool:
    kind = str(bad.get("kind") or "")
    url = str(bad.get("url") or "")
    if kind == "loadingFailed":
        return True                       # 资源压根没下来
    if kind.startswith("HTTP"):
        try:
            code = int(kind.split()[1])
        except Exception:                                 # noqa: BLE001
            return True
        if code >= 500:
            return True
        if code == 404:
            # 静态资源 404 是硬伤（JS 少了就是白屏）；API 404 也可能是真问题，
            # 但"没插硬件"不会导致 404（那会是 409/503），所以一律算硬伤。
            return True
        # 4xx：API 的 409/503 在"设备没接"时是预期，静态资源 4xx 才是硬伤
        # （url 是**完整** URL，所以这里只能用 `in`，不能拿它去比前缀）
        return SOFT_API_PREFIX not in url
    return False


def record(cid: str, verdict: str, note: str) -> None:
    RESULTS.append((cid, verdict, note))
    tag = {"PASS": "[PASS]", "FAIL": "[FAIL]", "SKIP": "[SKIP]", "INFO": "[INFO]"}[verdict]
    print(f"  {tag} {cid:6s} {note}")


def head(t: str) -> None:
    print(f"\n── {t} " + "─" * max(0, 68 - len(t)))


def find_browser() -> str:
    for p in CHROME_CANDIDATES:
        if Path(p).is_file():
            return p
    found = shutil.which("chrome") or shutil.which("msedge")
    if found:
        return found
    raise SystemExit("找不到 Chrome/Edge —— 这个脚本必须有真浏览器才能跑")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---------------------------------------------------------------- CDP 客户端

class CDP:
    """极简 CDP 客户端：发命令 / 收事件。

    为什么要一个专用 reader：CDP 的**响应**和**事件**走在同一条 WS 上，如果只在
    "发完命令后读一条"里做同步等待，就会在事件插进来时错位。所以必须有一个持续的
    读取循环，把响应按 id 分发给 future、把事件攒进列表。
    """

    def __init__(self, session) -> None:
        self.session = session
        self._id = 0
        self._pending: dict = {}
        self.events: list = []
        self._reader_task = None

    async def start(self) -> None:
        self._reader_task = asyncio.create_task(self._reader())

    async def _reader(self) -> None:
        try:
            async for msg in self.session:
                if msg.type.name == "CLOSE":
                    break
                try:
                    d = json.loads(msg.data)
                except Exception:                             # noqa: BLE001
                    continue
                if "id" in d and d["id"] in self._pending:
                    fut = self._pending.pop(d["id"])
                    if not fut.done():
                        fut.set_result(d)
                else:
                    self.events.append(d)
        except Exception:                                     # noqa: BLE001
            pass

    async def call(self, method: str, params: dict = None, timeout: float = 30.0):
        self._id += 1
        i = self._id
        fut = asyncio.get_running_loop().create_future()
        self._pending[i] = fut
        await self.session.send_json({"id": i, "method": method, "params": params or {}})
        d = await asyncio.wait_for(fut, timeout)
        if "error" in d:
            raise RuntimeError(f"{method} 失败：{d['error']}")
        return d.get("result")

    async def eval(self, expr: str, timeout: float = 30.0):
        r = await self.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True,
        }, timeout)
        res = r.get("result") or {}
        if r.get("exceptionDetails"):
            raise RuntimeError(json.dumps(r["exceptionDetails"], ensure_ascii=False)[:400])
        return res.get("value")

    async def snapshot_console(self) -> list:
        """取当前累计的异常/错误（取完不清理，调用方自己比较长度）。"""
        out = []
        for e in self.events:
            m = e.get("method")
            p = e.get("params") or {}
            if m == "Runtime.exceptionThrown":
                d = p.get("exceptionDetails") or {}
                out.append({"kind": "exception",
                            "text": (d.get("exception") or {}).get("description")
                            or d.get("text") or "未知异常",
                            "url": d.get("url"), "line": d.get("lineNumber")})
            elif m == "Runtime.consoleAPICalled" and p.get("type") in ("error", "assert"):
                out.append({"kind": "console." + str(p.get("type")),
                            "text": " ".join(str((a or {}).get("value", (a or {}).get("description", "")))
                                             for a in (p.get("args") or [])),
                            "url": None, "line": None})
            elif m == "Log.entryAdded":
                en = p.get("entry") or {}
                if en.get("level") == "error":
                    out.append({"kind": "log." + str(en.get("source")),
                                "text": en.get("text"), "url": en.get("url"),
                                "line": en.get("lineNumber")})
        return out

    async def bad_requests(self) -> list:
        """收集 failed / 4xx5xx 的请求 —— JS 文件 404 会在这里现形。"""
        out = []
        for e in self.events:
            m = e.get("method")
            p = e.get("params") or {}
            if m == "Network.loadingFailed":
                out.append({"kind": "loadingFailed", "text": p.get("errorText"),
                            "url": (p.get("request") or {}).get("url")})
            elif m == "Network.responseReceived":
                r = p.get("response") or {}
                if int(r.get("status") or 200) >= 400:
                    out.append({"kind": "HTTP " + str(r.get("status")),
                                "url": r.get("url"), "text": ""})
        return out


# ---------------------------------------------------------------- 主流程

async def run(args) -> int:
    import aiohttp

    base = f"http://127.0.0.1:{args.port}"
    head("0. 目标服务")
    try:
        with urllib.request.urlopen(base + "/api/v1/health", timeout=5) as r:
            record("S0", "PASS" if r.status == 200 else "FAIL", f"{base}/api/v1/health → {r.status}")
    except Exception as e:                                    # noqa: BLE001
        record("S0", "FAIL", f"{base} 不可达：{e}（先 python app.py）")
        return 1

    # ---------------------------------------------------------------- 确定的起点
    '''⚠️ 这一节不是客套，它是**消灭 flaky 的那一刀**。

    场景：上一轮 `--sim` 收尾时模拟器被 terminate，但服务端的连接是**被动断开**的 ——
    `connected` 要等下一次 I/O 真的失败才翻过来。在那之前有一小段"服务端说已连、
    但实际已经死了"的窗口。若浏览器恰好在那段窗口里启动，页面的门控会按"已连"渲染，
    而检查器稍后读到的 health 是"未连" ⇒ 逐页断言与门控断言全变成随机结果。

    修法不是把断言放宽，而是**先给这一轮一个确定的起点**：主动断一次，再等
    `mouse.connected` 连续两次读到同一个值。'''
    head("0c. 确定起点（断开残留连接并等状态稳定）")
    try:
        _post(base, "/api/v1/mouse/disconnect", {})
        prev = None
        stable = False
        for _ in range(15):
            cur = bool((read_health(base).get("mouse") or {}).get("connected"))
            if cur is False and prev is False:
                stable = True
                break
            prev = cur
            await asyncio.sleep(0.4)
        record("S0e", "PASS" if stable else "SKIP",
               "鼠标已断开且状态稳定（这一轮从确定状态开始）"
               if stable else "鼠标状态未在 6 s 内稳定 —— 后续门控断言可能不准，建议重跑")
    except Exception as e:                                    # noqa: BLE001
        record("S0e", "SKIP", f"清理残留连接失败（不影响后续）：{e}")

    # ---------------------------------------------------------------- 假上游相机
    fake_cam, cam_note = None, "未启用"
    # `--no-fake-cam` 跑的是**"相机不可达"那条降级路**。而本层的代启功能一旦生效，
    # 那条路当场就没了（界面会自己去启动上游），而且会在**用户机器上留下一个上游进程**
    # —— 测试不该有这种副作用（真发生过：跑完 --no-fake-cam，8765 上多了一个 server.py）。
    # 所以这一轮**先暂停代启**（`/camera/service/pause`，只影响本次运行、不写 config.yaml），
    # 收尾时恢复。
    launcher_paused = False
    stopped_by_us = False            # 为了让降级条件成立，我们停掉了本层代启的上游
    if not args.fake_cam:
        try:
            _post(base, "/api/v1/camera/service/pause", {"on": True})
            launcher_paused = True
            record("S0f", "PASS", "本轮已暂停本层代启（降级路不该被自动修好、也不该留下进程）")
        except Exception as e:                                # noqa: BLE001
            record("S0f", "SKIP", f"暂停代启失败（降级路可能被自动修好）：{e}")
        # ⚠️ **只暂停代启还不够**：8765 上可能**已经有服务在跑**（本层先前起的，或别人起的），
        # 那样"相机不可达"这条路根本走不到 —— 而这一轮的全部意义就是走它。
        # 不处理的话判据会**假绿**（B16 画出真二维码、B29 走可达分支），
        # 而报告上看起来"降级路验过了"（真踩过：48 PASS 里没有一条在测降级）。
        try:
            cam = read_health(base).get("camera") or {}
            if cam.get("reachable") is not True:
                record("S0g", "PASS", "相机不可达 ⇒ 降级条件成立（这一轮测的就是它）")
            else:
                svc = _svc_state(base)
                if svc.get("by_us"):
                    # 本层代启的 = 我们自己的进程，停掉它是安全的（收尾会起回来）
                    _post(base, "/api/v1/camera/service/stop", {})
                    stopped_by_us = True
                    try:
                        _post(base, "/api/v1/camera/probe", {})   # 同步重探，别等 4 s 周期
                    except Exception:                         # noqa: BLE001
                        pass
                    time.sleep(0.8)
                    cam = read_health(base).get("camera") or {}
                    if cam.get("reachable") is not True:
                        record("S0g", "PASS",
                               "已停掉本层代启的上游服务 ⇒ 降级条件成立（收尾会起回来）")
                    else:
                        record("S0g", "SKIP",
                               f"停了本层代启的服务但仍可达（{cam.get('base')}）"
                               f"⇒ 降级路**本轮没验到**")
                else:
                    record("S0g", "SKIP",
                           f"**降级路本轮没被验到**：{cam.get('base')} 上的服务"
                           f"不是本层起的，测试不去停它（那是别人的进程）。"
                           f"想看降级结果，先手动停掉 8765 上的服务再跑 --no-fake-cam")
        except Exception as e:                                # noqa: BLE001
            record("S0g", "SKIP", f"检查降级条件失败（不影响其余判据）：{e}")
    if args.fake_cam:
        fake_cam, cam_note = _fake_camera_upstream(args.fake_cam_port)
        if fake_cam is not None:
            try:   # 让 Control 立刻探一次，而不是等它那个后台探测周期（默认 5 s）
                urllib.request.urlopen(urllib.request.Request(
                    base + "/api/v1/camera/probe", method="POST", data=b"{}",
                    headers={"Content-Type": "application/json"}), timeout=25).read()
            except Exception:                                 # noqa: BLE001
                pass
        record("S0d", "PASS" if fake_cam is not None else "SKIP", f"假上游相机：{cam_note}")
        # ⚠️ **"8765 上已有服务"不等于"这个服务是我们能用的那个"。**
        # 真发生过（2026-10-06）：8765 上跑着一个真 `pc/server.py`，但它的 **token 与
        # `config.yaml` 不符** ⇒ `/camera/pair` 读不到二维码、而端口又是通的。
        # 于是 B16 与 B29 **两侧分支都落不进去**（既不是"可达"也不是"不可达"），
        # 报出来是"二维码没画出来 / 不可达时没给下一步"—— 从现象完全看不出根因是 token。
        # 所以这里先探一次，把根因**当场写进报告**。
        if fake_cam is None:
            try:
                cam0 = read_health(base).get("camera") or {}
                if cam0.get("reachable") is not True:
                    d0 = str(cam0.get("detail") or "").splitlines()
                    extra = ('；' + d0[1].strip()[:90]) if len(d0) > 1 else ''
                    record("S0e", "INFO",
                           '8765 上那个服务**不认我们**'
                           f'（reachable={cam0.get("reachable")}）⇒ B16 / B29 会落到'
                           '「既非可达、也非不可达」的中间态而红，'
                           f'**根因在相机侧、不在本轮改动**：'
                           f'{(d0[0][:110] if d0 else "(没有 detail)")}{extra}')
                else:
                    record("S0e", "PASS", "8765 上的服务认我们的 token（可达）⇒ 用它验真数据")
            except Exception as e:                            # noqa: BLE001
                record("S0e", "SKIP", f"探测 8765 上的服务失败（不影响其余判据）：{e}")

    # ---------------------------------------------------------------- 接模拟器
    sim_proc = None
    if args.sim:
        head("0b. 起鼠标模拟器（让页面有真数据可渲染）")
        host_dir = ROOT.parent / "RemoteControl" / "host"
        if not (host_dir / "host" / "sim" / "mouse_sim.py").is_file():
            record("S0b", "SKIP", f"上游模拟器不在 {host_dir}")
        else:
            sim_proc = subprocess.Popen(
                [sys.executable, "-u", "-m", "host.sim.mouse_sim", "--port", str(args.sim_port)],
                cwd=str(host_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace")
            up = False
            t0 = time.time()
            # ⚠️ **不要连端口来判就绪**：模拟器是单客户端的，探针连上又立刻关，
            # 会让它把死连接当成"正在服务的客户端"，真正的客户端就连不上了
            # （表现是 TCP 通、但没有响应 ⇒ 上层超时，原因看着跟模拟器无关）。
            # 改成读它的启动输出，零交互。
            while time.time() - t0 < 25:
                line = sim_proc.stdout.readline()
                if not line:
                    if sim_proc.poll() is not None:
                        break
                    continue
                if "监听" in line or "listening" in line.lower():
                    up = True
                    break
            if not up:
                record("S0b", "FAIL", f"模拟器 25 s 内没监听 {args.sim_port}")
            else:
                record("S0b", "PASS", f"模拟器已监听 127.0.0.1:{args.sim_port}")
                try:
                    r = urllib.request.urlopen(urllib.request.Request(
                        base + "/api/v1/mouse/connect", method="POST",
                        data=json.dumps({"target": f"tcp://127.0.0.1:{args.sim_port}"}).encode(),
                        headers={"Content-Type": "application/json"}), timeout=30)
                    d = json.loads(r.read().decode("utf-8"))
                    record("S0c", "PASS", f"服务端已连上模拟器：{d.get('data')}")
                except Exception as e:                        # noqa: BLE001
                    body = ""
                    if hasattr(e, "read"):
                        try:
                            body = e.read().decode("utf-8", "replace")[:300]
                        except Exception:                     # noqa: BLE001
                            pass
                    record("S0c", "FAIL", f"连模拟器失败：{e} {body}")

    head("1. 起无头浏览器")
    exe = find_browser()
    dbg = free_port()
    profile = Path(tempfile.mkdtemp(prefix="ctl-chrome-"))
    proc = subprocess.Popen([
        exe, "--headless=new", f"--remote-debugging-port={dbg}",
        f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        "--disable-extensions", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=1", f"--window-size={args.width},{args.height}",
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ws_url = None
    t0 = time.time()
    while time.time() - t0 < 30:
        try:
            raw = urllib.request.urlopen(f"http://127.0.0.1:{dbg}/json/list", timeout=3).read()
            for t in json.loads(raw):
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    ws_url = t["webSocketDebuggerUrl"]
                    break
            if ws_url:
                break
        except Exception:                                     # noqa: BLE001
            pass
        time.sleep(0.4)
    if not ws_url:
        record("S1", "FAIL", "浏览器 30 s 内没给出 CDP 端点")
        proc.terminate()
        return 1
    record("S1", "PASS", f"{Path(exe).name} 无头已起（CDP {dbg}）")

    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(ws_url, max_msg_size=0) as ws:
            cdp = CDP(ws)
            await cdp.start()
            for dom in ("Page", "Runtime", "Log", "Network"):
                await cdp.call(f"{dom}.enable")
            await cdp.call("Emulation.setDeviceMetricsOverride",
                           {"width": args.width, "height": args.height,
                            "deviceScaleFactor": 1, "mobile": False})

            head("2. 打开首页（P0 验收：四灯可点 + 两栏 + 九页）")
            await cdp.call("Page.navigate", {"url": base + "/"})
            await asyncio.sleep(args.wait)
            title = await cdp.eval("document.title")
            lamps = await cdp.eval(
                "Array.from(document.querySelectorAll('.lamp em')).map(e=>e.textContent)")
            # 顶栏那四个状态标签必须**本身就是入口**（点它开该设备的窗口）：
            # `data-dev` 就是这件事在 DOM 上的证据，少了它标签就只是块画。
            devs = await cdp.eval(
                "Array.from(document.querySelectorAll('.lamp[data-dev]')).map(b=>b.dataset.dev)")
            navs = await cdp.eval(
                "Array.from(document.querySelectorAll('.rail-tabs button[data-tab]'))"
                ".map(b=>b.dataset.tab)")
            views = await cdp.eval(
                "Array.from(document.querySelectorAll('.rail-views .view')).map(v=>v.dataset.tab)")
            # 只数**有内容、且不在设备窗口里**的故障框：
            #  · 空盒子不算错误 —— `.errbox` 也可能是常驻的"故障位"（相机窗口的
            #    `#dc-fault`），服务正常时它就是空的（同 B13 那条纪律：
            #    "还没准备好"不许被数成"出错了"）；
            #  · 设备窗口里的故障框也不属于这一条 —— 它说的是"某一路设备不通"，
            #    有内容恰恰说明界面在如实报故障，那是由 **B29** 专门判的。
            #    这条判据问的是"**页面/模块挂载**有没有出错"。
            errboxes = await cdp.eval(
                "Array.from(document.querySelectorAll('.errbox'))"
                ".filter(e=>!e.closest('#dev-modal'))"
                ".map(e=>(e.textContent||'').trim()).filter(t=>t.length)")
            record("B0", "PASS" if (lamps and len(lamps) == 4
                                    and devs == ["mouse", "camera", "locator", "clock"]) else "FAIL",
                   f"标题「{title}」；顶栏灯 {len(lamps)} 个：{lamps}；"
                   f"其中可点的设备标签 {devs}")
            record("B1", "PASS" if navs == TABS else "FAIL",
                   f"两栏页签 {len(navs)} 个（左栏 {len(TAB_CAM)} / 右栏 {len(TAB_MOUSE)}）：{navs}")
            record("B2", "PASS" if views == TABS + DOCK_VIEWS else "FAIL",
                   f"视图容器 {len(views)} 个（两栏 {len(TABS)} + 底栏 {len(DOCK_VIEWS)}），"
                   f"全部 mount 无异常"
                   + (f"；**有 errbox**：{errboxes}" if errboxes else ""))
            if errboxes:
                record("B2", "FAIL", f"挂载期出现 errbox：{errboxes}")

            # ---- 主区结构：入口页（纯导航）+ 模块界面（整屏）两级 ----
            # 入口页**只放导航**：四个子系统各一组（类别名称 + 模块卡片），
            # 不放控件、不放工作区 —— 功能控件与控制对象混在一屏就是"显得乱"的来源。
            head("2b. 入口页（子系统分组 + 模块卡片）")
            sysinfo = await cdp.eval(
                "(()=>{const out={};"
                "document.querySelectorAll('.nav-sec[data-sys]').forEach(function(s){"
                " const cards=Array.from(s.querySelectorAll('.mod-card'));"
                " const r=s.getBoundingClientRect();"
                " const c0=cards.length?cards[0].getBoundingClientRect():null;"
                " out[s.dataset.sys]={on:true,"
                "  name:(s.querySelector('.sec-head b')||{textContent:''}).textContent,"
                "  sub:(s.querySelector('.sec-head .sec-sub')||{textContent:''}).textContent,"
                "  cards:cards.length,"
                "  h:Math.round(r.height), w:Math.round(r.width), top:Math.round(r.top),"
                "  card0:c0?Math.round(c0.height):0,"
                "  names:cards.map(function(c){return c.querySelector('b').textContent;})};});"
                "return out;})()")
            # 旧选项卡必须**彻底不在**：入口页本身就是导航，再来一排选项卡就是两套入口。
            oldnav = await cdp.eval("!!document.querySelector('#nav-sys')")
            shown = await cdp.eval(
                "(()=>{const n=document.getElementById('nav'),m=document.getElementById('mod');"
                "return {nav:n?getComputedStyle(n).display!=='none':false,"
                " mod:m?getComputedStyle(m).display!=='none':false,"
                " open:document.getElementById('sys').classList.contains('mod-open'),"
                " hash:location.hash||''};})()")
            sec_ok = (sorted(sysinfo.keys()) == sorted(SYS_TABS)
                      and all(sysinfo[s]["name"] for s in SYS_TABS))
            # ⚠️ 「小车控制」的卡片数是 **3**：
            #   · 上游 2.0.0 删了 `SNAP`/`SCOPE` ⇒「分组测量」并入「实时参数」，5 变 4；
            #   · 上游 2.4.0 压到两档编译 + 删课程层 + 加 `MAZE RUN [BLIND|RACE]`
            #     ⇒ 四卡重排为**三卡**：①实时状态与参数设定（原「实时参数」+「运动控制」
            #     +「校准例程」合成一页）②无记忆走迷宫（原「实验 8」改名）③比赛（新）。
            #   这里写死数字是有意的 —— 卡片由 `SYS_DEF` 渲染，**模块脚本没加载时卡片照样在**，
            #   所以"卡片数对不对"与"模块能否挂上"是两道独立的闸。
            cards_ok = (sysinfo["mouse"]["cards"] == 3 and sysinfo["cam"]["cards"] == 1
                        and sysinfo["loc"]["cards"] == 2 and sysinfo["all"]["cards"] == 1)
            b24_ok = sec_ok and cards_ok and not oldnav and shown["nav"] and not shown["mod"]
            record("B24", "PASS" if b24_ok else "FAIL",
                   f"入口页只有导航（无旧选项卡、工作区不在入口页）："
                   f"小车控制 {sysinfo['mouse']['cards']} 卡片 {sysinfo['mouse']['names']}"
                   f"／相机 {sysinfo['cam']['cards']}／定位 {sysinfo['loc']['cards']}"
                   f"／综合运动 {sysinfo['all']['cards']}"
                   if b24_ok
                   else f"**入口页结构不对**：旧选项卡={oldnav} 显示={shown} {sysinfo}")

            # 点卡片 ⇒ **单开一个整屏模块界面**：入口页收起、工作区整屏、头部有返回，
            # 且**别的子系统的工作区一个都不许露出来** —— 那才叫"单开一个新界面"。
            head("2c. 模块界面：点卡片单开、点返回回入口")
            drill = await cdp.eval(
                "(()=>{const q=function(s){return document.querySelector(s);};"
                "q('.nav-sec[data-sys=\"mouse\"] .mod-card').click();"
                "const sy=q('#sys'),nav=q('#nav'),mod=q('#mod'),ws=q('#ws-mouse');"
                "const on=ws?ws.querySelector('.view.on'):null;"
                "const extra=Array.from(document.querySelectorAll('#mod-body > .ws'))"
                ".filter(function(w){return w!==ws&&w.classList.contains('on');}).length;"
                "const r=mod.getBoundingClientRect(), sr=sy.getBoundingClientRect();"
                "return {mod:on?on.dataset.mod:null,"
                " open:sy.classList.contains('mod-open'),"
                " navHidden:getComputedStyle(nav).display==='none',"
                " modShown:getComputedStyle(mod).display!=='none',"
                " title:(q('#mod-title')||{textContent:''}).textContent,"
                " sub:(q('#mod-sub')||{textContent:''}).textContent,"
                " back:!!q('#mod-head [data-back]'),"
                " w:Math.round(r.width), sw:Math.round(sr.width),"
                " extra:extra, hash:location.hash||''};})()")
            await asyncio.sleep(0.3)
            back_ret = await cdp.eval(
                "(()=>{document.querySelector('#mod-head [data-back]').click();"
                "const n=document.getElementById('nav'),m=document.getElementById('mod');"
                "return {nav:getComputedStyle(n).display!=='none',"
                " mod:getComputedStyle(m).display!=='none',"
                " open:document.getElementById('sys').classList.contains('mod-open'),"
                " hash:location.hash||''};})()")
            b25_ok = (drill.get("mod") == "mod_params" and drill.get("open")
                      and drill.get("navHidden") and drill.get("modShown")
                      and drill.get("title") and drill.get("back")
                      and abs(drill.get("w", 0) - drill.get("sw", 0)) <= 2
                      and not drill.get("extra")
                      and drill.get("hash") == "#/mouse/mod_params"
                      and back_ret.get("nav") and not back_ret.get("mod")
                      and not back_ret.get("open") and back_ret.get("hash") == "")
            record("B25", "PASS" if b25_ok else "FAIL",
                   f"点卡片「{drill.get('title')}」⇒ 入口页收起、工作区整屏 "
                   f"({drill.get('w')}px)、其它工作区未露、URL={drill.get('hash')}；"
                   f"点返回 ⇒ 回入口页、URL 清空"
                   if b25_ok else f"**模块界面开合不对**：{drill} / 返回后 {back_ret}")

            # 入口页必须**真排出来了**：四组竖排（top 递增、等宽）、每组有高度、
            # 卡片有高度、横向不溢出。面板塌成 0 时 B24 仍可能全绿（卡片 DOM 在），
            # 只有几何能看出来 —— 这就是 B26 存在的理由。
            # ⚠️ 刻意**不要求可滚**：入口页只有卡片，一屏放得下才是常态
            #（原来"一页到底"那句要求的是长页，现在不成立了）。
            tops = [sysinfo[s]["top"] for s in SYS_TABS]
            widths = [sysinfo[s]["w"] for s in SYS_TABS]
            stacked = all(tops[i] < tops[i + 1] for i in range(len(tops) - 1))
            same_w = max(widths) - min(widths) <= 2
            box = await cdp.eval(
                "(()=>{const s=document.getElementById('sys');"
                "return {sw:s.scrollWidth, cw:s.clientWidth, sh:s.scrollHeight,"
                " ch:s.clientHeight, ov:getComputedStyle(s).overflowY};})()")
            no_ovf = box["sw"] <= box["cw"] + 2
            geo_bad = [s for s in SYS_TABS
                       if not (sysinfo[s].get("h", 0) > 40 and sysinfo[s].get("w", 0) > 300)]
            card_bad = [s for s in SYS_TABS if not sysinfo[s].get("card0", 0) > 30]
            b26_ok = stacked and same_w and no_ovf and not geo_bad and not card_bad
            record("B26", "PASS" if b26_ok else "FAIL",
                   f"入口页几何正常：四组竖排 {tops}、等宽 {widths[0]}px、"
                   f"内容 {box['sh']}/{box['ch']}px 无横向溢出；"
                   + "／".join(f"{s} 高 {sysinfo[s].get('h')}"
                               f"（卡片 {sysinfo[s].get('card0')}）" for s in SYS_TABS)
                   if b26_ok
                   else f"**入口页没排出来**：竖排={stacked} 等宽={same_w} "
                        f"不溢出={no_ovf}({box}) 组={geo_bad} 卡片={card_bad}")

            # 「单开一个新界面」必须**能被地址引用**：带 `#/<子系统>/<模块>` 打开
            # （刷新、贴链接、浏览器后退都算），要落到同一个界面 —— 否则现场说不清
            # "就是这一页"，只能靠嘴描述点哪几个卡片。
            await cdp.call("Page.navigate", {"url": base + "/#/loc/mod_maze"})
            await asyncio.sleep(args.wait)
            deep = await cdp.eval(
                "(()=>{const q=function(s){return document.querySelector(s);};"
                "const w=q('#ws-loc .view.on');"
                "return {mod:w?w.dataset.mod:null,"
                " open:document.getElementById('sys').classList.contains('mod-open'),"
                " title:(q('#mod-title')||{textContent:''}).textContent,"
                " hash:location.hash||''};})()")
            b27_ok = (deep.get("mod") == "mod_maze" and deep.get("open")
                      and deep.get("title") and deep.get("hash") == "#/loc/mod_maze")
            record("B27", "PASS" if b27_ok else "FAIL",
                   f"深链 `#/loc/mod_maze` 直接落到「{deep.get('title')}」界面"
                   if b27_ok else f"**深链落不到目标界面**：{deep}")

            # 逐页断言打的是「综合运动」界面里的九页：**先把那个界面打开** ——
            # 它在 `#ws-all` 里，入口页模式下是 `display:none`，而不可见元素的
            # `innerText` 是空字符串，不先打开，后面每一条都会读到空。
            allview = await cdp.eval(
                "(()=>{C.openMod('all','@all');"
                "const w=document.getElementById('ws-all');"
                "return {on:w.classList.contains('on'),"
                " vis:getComputedStyle(w).display!=='none',"
                " railViews:document.querySelectorAll('.rail-views .view').length,"
                " hash:location.hash||''};})()")
            await asyncio.sleep(0.4)
            want_views = len(TABS) + len(DOCK_VIEWS)
            b28_ok = (allview.get("on") and allview.get("vis")
                      and allview.get("railViews") == want_views
                      and allview.get("hash") == "#/all/@all")
            record("B28", "PASS" if b28_ok else "FAIL",
                   f"「综合运动」卡片打开了两栏九页界面（视图容器 {want_views} 个）"
                   if b28_ok else f"**综合运动界面打不开**：{allview}")

            # 三个**模块**（上游 1.5.0 组测发送 / 1.5.5 实验 8 状态机 / 2.4.0 比赛页）必须真的挂上并渲染出控件。
            # 为什么单独判：模块脚本没加载 / 注册失败时，**卡片照样在**（它由 `SYS_DEF` 渲染），
            # 点进去才发现是空的 —— 而"页面是空的"和"这个功能本来就没做"长得一模一样
            # （同 I21 的那条纪律：静态资源少一个会**静默变空**）。
            head("2f. 模块：实时状态与参数设定 / 无记忆走迷宫 / 比赛")
            n_err0 = len(await cdp.snapshot_console())
            want_sel = {
                # SAVE 按钮是**按数据里的 flash_page 现生成**的 ——
                # "现生成"的东西最容易在某一分支里整个不出现（脚本没报错、页面上没有）。
                #
                # 2.0.0：「分组测量」**已合并进「实时参数」**（`SNAP`/`SCOPE` 整套删除，
                # 两者读的东西变成了同一条统一帧）⇒ 这一页现在必须同时有
                # **读数区**（`#mp-live` / `#mp-stream` / `#mp-fault`）与**参数表区**。
                # 2.4.0（改版十二）：这一页改名「实时状态与参数设定」，并**并入了原
                # 「运动控制」（#mm-*）与「校准例程」（#mc-*）两张卡的内容 ⇒ 那些 id
                # 现在也必须在这一页里找得到（它们不再有自己的卡片，丢了没人报红）。
                "mod_params": ["#mp-groups", "#mp-reload", "#mp-saves",
                               "#mp-live", "#mp-stream", "#mp-fault",
                               "#mm-go", "#mm-estop", "#mc-items", "#mc-refresh"],
                # 无记忆走迷宫（原「实验 8」）改成**三栏**（状态 / 重构 / 历程）
                # + 记录与回放 ⇒ 断言跟着换：三栏的容器与关键 id 一个都不能少，
                # 而且**记录/回放那几个控件也要在**（它们不在任何别的清单里，
                # 少一个就没人为它报红 —— 见 §11.10 的教训）。
                # 2026-10-06 追加：右栏顶上那套"运行状态胶囊 + 时间线进度条 + 进入/退出回放"
                # 是本轮新加的，所以一并钉在这里。
                "mod_maze8": ["#m8-run", "#m8-mstop", "#m8-rescue", "#m8-stop",
                              "#m8-kv", "#m8-sens",
                              "#m8-cv", "#m8-tl", "#m8-evfault",
                              "#m8-reclist", "#m8-import", "#m8-recbox",
                              "#m8-runpill", "#m8-tlsld", "#m8-rep-enter",
                              "#m8-repctl", "#m8-rep-exit"],
                # 比赛（2.4.0 新页）：`MAZE RUN RACE` 发车 + 收车/救援 + 前墙双档
                # + 冲刺状态 + 状态机。这些 id 是**新加的、别处没有**，所以必须钉住。
                "mod_race": ["#rc-run", "#rc-mstop", "#rc-rescue", "#rc-refresh",
                             "#rc-front", "#rc-front-ok", "#rc-spurt", "#rc-state"],
            }
            newmods = {}
            for mid in ("mod_params", "mod_maze8", "mod_race"):
                newmods[mid] = await cdp.eval(
                    "(()=>{C.openMod('mouse','" + mid + "');"
                    "const v=document.querySelector('#ws-mouse .view.on');"
                    "const miss=" + json.dumps(want_sel[mid]) + ".filter(function(s){"
                    "  return !document.querySelector(s);});"
                    "const eb=Array.from(document.querySelectorAll('#ws-mouse .errbox'))"
                    "  .filter(function(e){return (e.textContent||'').trim();});"
                    "return {mod:v?v.dataset.mod:null, miss:miss,"
                    "  err:eb.map(function(e){return e.textContent.slice(0,60);})};})()")
                await asyncio.sleep(0.4)
                # 未连接时**也要画得出"要读什么"**：码表是静态的，不该等设备。
                #   · 实时状态与参数设定：读数区要按 `meas/meta` 的通道名把清单列出来；
                #   · 无记忆走迷宫：要么明说「未运行」，要么给出真的状态机读数；
                #   · 比赛：状态机要么明说「未运行」，要么给出真的状态。
                newmods[mid]["txt"] = await cdp.eval(
                    "(()=>{const b=document.querySelector('#mp-live');"
                    "return b?b.textContent.replace(/\\s+/g,' ').slice(0,400):'';})()"
                    if mid == "mod_params" else
                    ("(()=>{const b=document.querySelector('#m8-kv');"
                     "return b?b.textContent.replace(/\\s+/g,' ').slice(0,120):'';})()"
                     if mid == "mod_maze8" else
                     "(()=>{const b=document.querySelector('#rc-state');"
                     "return b?b.textContent.replace(/\\s+/g,' ').slice(0,120):'';})()"))
                if shots:
                    s = await cdp.call("Page.captureScreenshot", {"format": "png"})
                    (shots / f"{mid}.png").write_bytes(base64.b64decode(s["data"]))
            await asyncio.sleep(0.5)
            errs30 = await cdp.snapshot_console()
            # 只有**未捕获异常**算硬伤：没插硬件时 4xx / console.error 是预期行为
            #（与逐页断言同一口径，见下面的 `hard` / `soft`）。
            hard30 = [e for e in errs30[n_err0:] if e.get("kind") == "exception"]
            soft30 = [e for e in errs30[n_err0:] if e.get("kind") != "exception"]
            bad_m = [m for m, r in newmods.items()
                     if r.get("mod") != m or r.get("miss") or r.get("err")]
            # 内容也要**有话说**（表还在、字是空的 = 只有壳）：
            #   · 实时状态与参数设定：未连接时唯一能画的就是**通道清单**（静态码表）—— 要有 `speedL`；
            #   · 无记忆走迷宫：要么明说「未运行」，要么给出真的状态机读数；
            #   · 比赛：同上（未连接时应写「未连接电脑鼠」或「未运行」）。
            mp_txt = newmods["mod_params"].get("txt", "")
            m8_txt = newmods["mod_maze8"].get("txt", "")
            rc_txt = newmods["mod_race"].get("txt", "")
            says_ok = (("speedL" in mp_txt)
                       and (("未运行" in m8_txt) or ("任务" in m8_txt))
                       and (("未运行" in rc_txt) or ("未连接" in rc_txt) or ("任务" in rc_txt)))
            b30_ok = (not bad_m) and not hard30 and says_ok
            record("B30", "PASS" if b30_ok else "FAIL",
                   "模块界面都挂上了：" + "；".join(
                       f"{m}({'/'.join(want_sel[m])})" for m in want_sel)
                   + f" 全在、有内容（{mp_txt[:16]}…｜{m8_txt[:16]}…｜{rc_txt[:16]}…）、"
                   + "挂载无未捕获异常"
                   + (f"（另有 {len(soft30)} 条非异常日志）" if soft30 else "")
                   if b30_ok else
                   f"**模块没挂上或渲染失败**：{newmods}；"
                   f"未捕获异常 {hard30[:2]}；内容={'ok' if says_ok else '空'}")

            # 「实时状态与参数设定」那一页把**唯一那条统一帧**按语义分组画出来。只判"行在不在"不够
            # —— 键名读错时行照样在、值永远显示 `—`，而且脚本不报错。
            # ⚠️ 不能拿"当前遥测值"当期望：统一帧 ≈217 Hz 一直在走，读完 DOM 再读遥测，
            # 两边本来就差几拍（旧版第一次就是这么假红的）。
            # ⇒ 喂一份**已知的**帧进去重渲染，逐行比对；比对完立刻还原。
            head("2g. 实时状态与参数设定：界面读数 vs 统一帧（分组表逐行）")
            # 哨兵值：每组各挑一个，覆盖四种渲染路径 ——
            #   小数+单位（speedL mm/s）、整数+单位（irF1 mm）、编码表翻名字（motionTask）、
            #   位掩码展开（flags）、以及派生键（roadName 来自 `classify_road`，不是通道）。
            SENT = {"speedL": -101.5, "speedR": 202.25,
                    "trackL": -303, "cellOdom": 404,
                    "irF1": 711, "irAdcF2": 812, "battMv": 3900,
                    "motionTask": 5, "mazeTask": 3, "dir": 1,
                    "flags": 66, "roadName": "TEE_FL", "ms8": 77}
            SENT_CH = sorted((set(SENT) - {"roadName"})
                             | {"speedR", "wallLeft", "stackIdx"})
            rp = await cdp.eval(
                "(()=>{C.openMod('mouse','mod_params');"
                "const box=document.getElementById('mp-live');"
                "if(!box)return {miss:true};"
                "const keep=C.state.telemetry;"
                "const SENT=" + json.dumps(SENT) + ","
                "  CH=" + json.dumps(SENT_CH) + ";"
                "C.state.telemetry={unified:SENT, channels:CH,"
                "  rate:{unified:{hz:214.5, nominal:217, age_ms:12, samples:900, stalled:false}},"
                "  bps:1470, streaming:true};"
                "const v=C.mods.mod_params;"
                "v.onMsg({type:'mouse.telemetry'});"
                "const bs=Array.from(box.querySelectorAll('b'));"
                "const sp=Array.from(box.querySelectorAll('span'));"
                "const rows=bs.map(function(b,i){return {"
                "  key:(b.querySelector('code')||{}).textContent||'',"
                "  label:(b.textContent||'').replace(/\\s+/g,' ').trim(),"
                "  val:((sp[i]||{}).textContent||'').trim()};});"
                "const clean=function(x){return String(x||'').replace(/\\s+/g,' ').trim();};"
                "const out={miss:false, rows:rows,"
                "  title:clean((document.getElementById('mod-head')||{}).textContent),"
                "  paneH4:clean(((box.parentNode||document).querySelector('h4')||{}).textContent),"
                "  stream:clean((document.getElementById('mp-stream')||{}).textContent)};"
                # 立刻还原，并用真值重渲染一次（中间那一瞬的"哨兵值"不能留在屏幕上）
                "C.state.telemetry=keep;"
                "try{v.onMsg({type:'mouse.telemetry'});}catch(e){}"
                "return out;})()")
            # 期望值**写死字符串**是有意的 —— 这几行正是"分组 / 键名 / 单位 / 编码"四件事的答案。
            want = ["speedL=-101.5 cm/s", "speedR=202.3 cm/s",
                    "trackL=-303 mm", "cellOdom=404 mm",
                    "irF1=711 mm", "irAdcF2=812",
                    "battMv=3900 mV",
                    "motionTask=STOP (5)",      # 名字来自码表，括号里是原码
                    "mazeTask=SPURT (3)", "dir=RIGHT (1)",
                    "flags=休眠 + 急停锁存 (66)",
                    "ms8=77 ms"]
            b34_bad, b34_got = [], []
            if rp.get("miss"):
                b34_bad.append("找不到 #mp-live")
            else:
                got = {r.get("key"): r.get("val") for r in (rp.get("rows") or [])}
                for k in sorted(SENT):
                    b34_got.append(f"{k}={got.get(k)}")
                for w in want:
                    k, _, exp = w.partition("=")
                    if got.get(k) != exp:
                        b34_bad.append(f"{k}：界面「{got.get(k)}」≠ 应为「{exp}」")
                # 哨兵里给的通道**每一个都要有行**（少一个 = 分组表漏掉了它）
                for k in SENT_CH:
                    if k not in got:
                        b34_bad.append(f"通道 {k} 没有出现在读数区")
                # 单位必须真的画出来（少了单位 = 一列纯数字，读者不知道哪个是 mm）
                allv = " ".join(str(r.get("val")) for r in (rp.get("rows") or []))
                for u in (" cm/s", " mm", " mV"):
                    if u not in allv:
                        b34_bad.append(f"读数区没有出现单位「{u}」")
                # 派生键（`roadName`）**不在**通道清单里，也不在分组里 ——
                # 它是"分类结果"，属于「无记忆走迷宫」那一页；这里出现反而是分组表把非通道键混进来了。
                if "roadName" in got:
                    b34_bad.append("roadName 混进了读数区（它是分类结果，不是帧通道）")
                if "实时状态与参数设定" not in rp.get("title", ""):
                    b34_bad.append(f"模块名不是「实时状态与参数设定」：{rp.get('title')!r}")
                if "实时读数" not in rp.get("paneH4", ""):
                    b34_bad.append(f"读数区标题不是「实时读数」：{rp.get('paneH4')!r}")
                # 流概览行要说清"这是哪条流、多大、多久以前"
                for w in ("统一帧", "实测", "最新一帧"):
                    if w not in rp.get("stream", ""):
                        b34_bad.append(f"流概览行缺少「{w}」：{rp.get('stream')!r}")
                # ⚠️ **负向**：协议常量必须标成「协议常量」，**不许**标成「标称」。
                # 为什么值得单列一条：2.1.0 起真机实测帧率只有协议常量的约 1/50
                # （4.4~6.5 Hz vs 217 Hz）。把两者写成"实测 X（标称 Y）"，
                # 读者会以为"只差一点"，而那正好掩盖了这一页最要紧的事实。
                # 这是一条会被误读的文案，所以它值得一道闸。
                if "标称" in rp.get("stream", ""):
                    b34_bad.append(f"协议常量被标成「标称」（会掩盖 50 倍落差）：{rp.get('stream')!r}")
                if "协议常量" not in rp.get("stream", ""):
                    b34_bad.append(f"流概览行没写明这是「协议常量」：{rp.get('stream')!r}")
            b34_ok = not b34_bad
            record("B34", "PASS" if b34_ok else "FAIL",
                   f"「实时状态与参数设定」读数区：{len(want)} 个哨兵逐行与喂进去的统一帧一致"
                   f"（{' '.join(b34_got[:6])}…）、单位齐（cm/s / mm / mV）、"
                   f"编码翻名字（motionTask/dir/flags）、标题「{rp.get('paneH4')}」、"
                   f"流概览「{rp.get('stream')}」"
                   if b34_ok else
                   f"**实时读数的分组/键名/单位/编码不对**：{b34_bad[:4]}；实际={b34_got}")

            # ── B35：无记忆走迷宫的**三栏几何 + 记录/复盘往返** ──
            # 这一条守的是本轮新加的那套东西。它比"控件在不在"深一层：
            #   · 三栏必须**真的并排在同一行**（三列竖排也能"三块都在"，那是错的）；
            #   · 复盘必须**真的在回放** —— 拖游标后左栏读数**跟着变**。
            #     只判"点了一下有个复盘条"是查不出"游标没接线"的。
            head("2h. 无记忆走迷宫：三栏几何 + 记录/复盘往返（拖游标读数要跟着变）")
            geo = await cdp.eval(
                "(()=>{C.openMod('mouse','mod_maze8');"
                "const box=document.querySelector('.mz3');"
                "if(!box)return {miss:true};"
                "const ps=Array.from(box.children);"
                "const rs=ps.map(function(p){const r=p.getBoundingClientRect();"
                " return {w:Math.round(r.width), h:Math.round(r.height), top:Math.round(r.top)};});"
                "const wr=box.getBoundingClientRect();"
                "return {miss:false, n:ps.length, rects:rs, wrapW:Math.round(wr.width),"
                "  wrapH:Math.round(wr.height),"
                "  sameRow: rs.length===3 && Math.abs(rs[0].top-rs[1].top)<=1"
                "           && Math.abs(rs[1].top-rs[2].top)<=1,"
                "  sumW: rs.reduce(function(a,r){return a+r.w;},0),"
                "  hasCanvas: !!document.getElementById('m8-cv')};})()")
            await asyncio.sleep(0.3)

            # 造一份**合成记录**并导入（走服务端，顺带把导入校验也验了）。
            # 帧里的 `posX` 故意**随帧变化** —— 否则拖游标时左栏不会变，
            # 那条"复盘真的在回放"的断言就成了空验证。
            synth = _synth_maze_doc()
            b35_bad, b35_got = [], []
            imp = None
            try:
                imp = _post(base, "/api/v1/mouse/maze/records/import",
                            {"doc": synth, "name": "b35_synth.maze.json"})
            except Exception as e:                            # noqa: BLE001
                b35_bad.append(f"导入合成记录失败：{type(e).__name__}: {e}")
            name = ((imp or {}).get("data") or {}).get("name") if imp else None

            if not name:
                if not b35_bad:
                    b35_bad.append(f"导入没返回文件名：{imp}")
            else:
                # 进复盘：走**用户真走的那条路**（点【记录列表】→ 点那一份），
                # 不直接调内部函数 —— 那样测不到列表渲染与点击绑定。
                rp2 = await cdp.eval(
                    "(()=>{const b=document.getElementById('m8-reclist');"
                    "if(!b)return {miss:'no reclist'};"
                    "b.click();return {clicked:true};})()")
                await asyncio.sleep(0.8)
                lst = await cdp.eval(
                    "(()=>{const box=document.getElementById('m8-recbox');"
                    "if(!box)return {miss:'no recbox'};"
                    "const btns=Array.from(box.querySelectorAll('[data-rec]'));"
                    "return {n:btns.length,"
                    "  names:btns.map(function(x){return x.dataset.rec;})};})()")
                want_name = name
                if not (lst.get("names") or []):
                    b35_bad.append(f"记录列表没渲染出条目：{lst}")
                else:
                    if not any(want_name in str(x) for x in lst["names"]):
                        b35_bad.append(f"列表里没有刚导入的那份（{want_name}）：{lst['names'][:3]}")
                    # 点它
                    await cdp.eval(
                        "(()=>{const box=document.getElementById('m8-recbox');"
                        "const b=Array.from(box.querySelectorAll('[data-rec]'))"
                        "  .filter(function(x){return x.dataset.rec.indexOf("
                        + json.dumps(want_name) + ")>=0;})[0];"
                        "if(b)b.click();})()")
                    await asyncio.sleep(0.8)
                    r1 = await cdp.eval(
                        "(()=>{const bar=document.getElementById('m8-repctl');"
                        "const kv=document.getElementById('m8-kv');"
                        "const t=document.getElementById('m8-rep-t');"
                        "const cv=document.getElementById('m8-cv');"
                        "const sld=document.getElementById('m8-tlsld');"
                        "return {barShown: !!bar && bar.style.display!=='none',"
                        "  kv:(kv?kv.textContent:'').replace(/\\s+/g,' ').trim().slice(0,120),"
                        "  t:(t?t.textContent:'').trim(),"
                        "  cvW: cv?cv.width:0, cvH: cv?cv.height:0,"
                        "  sldMax: sld?Number(sld.max):0,"
                        "  pill:(document.getElementById('m8-runpill')||{}).className};})()")
                    b35_got.append("进入回放=" + str(r1.get("barShown")))
                    if not r1.get("barShown"):
                        b35_bad.append(f"点记录后回放控制条没出来：{r1}")
                    if not (r1.get("cvW") and r1.get("cvH")):
                        b35_bad.append(f"回放时中栏画布没尺寸：{r1}")
                    # 进度条在回放态必须**可拖且量程 = 这一段的时间长度**
                    # （不是帧号：帧在时间上不均匀，"拖到中间"必须落在时间的中点上）
                    if not r1.get("sldMax"):
                        b35_bad.append(f"回放态进度条量程为 0（拖不动）：{r1}")
                    if "replay" not in str(r1.get("pill")):
                        b35_bad.append(f"回放态状态胶囊没变成「回放中」：{r1.get('pill')!r}")
                    # 拖进度条到**时间的中点**（不是帧号中点），左栏读数必须跟着变
                    await cdp.eval(
                        "(()=>{const s=document.getElementById('m8-tlsld');"
                        "s.value=String(Math.floor(Number(s.max)*0.5));"
                        "s.dispatchEvent(new Event('input'));})()")
                    await asyncio.sleep(0.5)
                    r0 = await cdp.eval(
                        "(()=>{const kv=document.getElementById('m8-kv');"
                        "const s=document.getElementById('m8-tlsld');"
                        "return {kv:(kv?kv.textContent:'').replace(/\\s+/g,' ').trim().slice(0,120),"
                        "  t:(document.getElementById('m8-rep-t')||{}).textContent,"
                        "  val:s?Number(s.value):-1, max:s?Number(s.max):0};})()")
                    moved = (r0.get("kv") != r1.get("kv"))
                    # 拖到 50% 之后，游标时刻应该在**时间的中点附近**（允许 ±20% 容差：
                    # 帧在时间上不均匀，二分会落到最近的那一帧上）
                    mid_ok = (r0.get("max") and
                              0.3 * r0["max"] <= r0.get("val", -1) <= 0.7 * r0["max"])
                    b35_got.append("拖到 50% 后读数变了=" + str(moved) +
                                   " 落在时间中点=" + str(mid_ok))
                    if not moved:
                        b35_bad.append("拖进度条后左栏读数**没变**（回放没在动）："
                                       f"尾={r1.get('kv')!r} 中={r0.get('kv')!r}")
                    if not mid_ok:
                        b35_bad.append(f"进度条按**时间**定位不成立：拖到 50% 落在 "
                                       f"{r0.get('val')}/{r0.get('max')}")
                    # 退出回放
                    await cdp.eval("document.getElementById('m8-rep-exit').click()")
                    await asyncio.sleep(0.5)
                    ex = await cdp.eval(
                        "(()=>{const bar=document.getElementById('m8-repctl');"
                        "const sld=document.getElementById('m8-tlsld');"
                        "return {hidden: !!bar && bar.style.display==='none',"
                        "  kv:!!document.getElementById('m8-kv'),"
                        "  atEnd: !!sld && Number(sld.value)===Number(sld.max)};})()")
                    if not ex.get("hidden"):
                        b35_bad.append(f"退出回放后控制条没收起：{ex}")
                    # 退出后进度条必须**回到最右**（"现在"）—— 这是"所有面板显示最新画面"
                    # 在进度条上的体现；留在中间会让人以为还在回放。
                    if not ex.get("atEnd"):
                        b35_bad.append(f"退出回放后进度条没回到最右：{ex}")
                    b35_got.append("退出后收起=" + str(ex.get("hidden")) +
                                   " 进度条回最右=" + str(ex.get("atEnd")))

            # 三栏几何
            if geo.get("miss"):
                b35_bad.append("找不到 .mz3（三栏容器）")
            else:
                b35_got.append(f"三栏 n={geo.get('n')} sameRow={geo.get('sameRow')}")
                if geo.get("n") != 3:
                    b35_bad.append(f"三栏不是 3 块：{geo.get('n')}")
                if not geo.get("sameRow"):
                    b35_bad.append(f"三栏没并排在同一行（竖排也不算三栏）：{geo.get('rects')}")
                for i, r in enumerate(geo.get("rects") or []):
                    if r["w"] < 120 or r["h"] < 120:
                        b35_bad.append(f"第 {i + 1} 栏太小：{r}")
                # 三栏总宽不许超过容器（否则横向溢出 —— 窄屏上最常见的坏法）
                if geo.get("wrapW") and geo.get("sumW", 0) > geo["wrapW"] + 4:
                    b35_bad.append(f"三栏横向溢出：{geo['sumW']} > {geo['wrapW']}")
                if not geo.get("hasCanvas"):
                    b35_bad.append("中栏没有画布 #m8-cv")
            b35_ok = not b35_bad
            record("B35", "PASS" if b35_ok else "FAIL",
                   f"无记忆走迷宫三栏并排（{'/'.join(str(r['w']) for r in geo.get('rects') or [])}px，"
                   f"不溢出 {geo.get('wrapW')}px）+ 记录→列表→复盘→拖游标→退出 全通（"
                   f"{'；'.join(b35_got)}）"
                   if b35_ok else
                   f"**无记忆走迷宫三栏 / 复盘往返不对**：{b35_bad[:4]}")

            # ── B36：收车 / 救援按钮 + 发车前低电门控（**正反两面**） ──
            # 守的是上游 2.1.1~2.2.1 新接进来的三个动作：收车、救援、低电提醒。
            #   · 前两个是**按钮在不在**（它们在参数/门控之外，纯 DOM 断言就够）；
            #   · 低电提醒要**真的会显式出现**才叫守住了 —— 而它是"跟着 1 s 定时器
            #     从服务端取回来再渲染"的，所以要：**钉住健康状态**（让 `C.needMouse()`
            #     返回 null，否则门控行被"未连接电脑鼠"占着，电池那句话说不出）+ **拦一条
            #     接口**喂 warn 级预检 + 等一个定时器周期。
            #   · **负向**：喂 ok 级时那一行必须**空**。只验"warn 时会有字"是不够的 ——
            #     一个永远有字的提示等于没提示（本层纪律：一道从没红过的闸不算闸）。
            head("2i. 无记忆走迷宫 / 设备窗口：收车与救援按钮 + 低电门控（含负向）")
            b36_bad = []
            btn = await cdp.eval(
                "(()=>{C.openMod('mouse','mod_maze8');"
                "const q=function(i){const e=document.getElementById(i);"
                " return e?{there:true,txt:(e.textContent||'').trim()}:{there:false};};"
                "return {run:q('m8-run'), mstop:q('m8-mstop'), rescue:q('m8-rescue'),"
                "  dmRescue:q('dm-rescue'), hint:(document.getElementById('m8-hint')||{}).textContent};})()")
            await asyncio.sleep(0.3)
            for k, want in (("mstop", "收车"), ("rescue", "救援")):
                if not btn.get(k, {}).get("there"):
                    b36_bad.append(f"无记忆走迷宫缺少按钮 {k}")
                elif want not in btn[k]["txt"]:
                    b36_bad.append(f"按钮 {k} 的文案不是「{want}」：{btn[k]['txt']!r}")
            if not btn.get("dmRescue", {}).get("there"):
                b36_bad.append("设备窗口缺少【救援（软复位）】按钮")
            elif "救援" not in btn["dmRescue"]["txt"]:
                b36_bad.append(f"dm-rescue 文案可疑：{btn['dmRescue']['txt']!r}")

            # 低电门控：钉住"有没有连上"这个**判定入口** + 拦一条接口，等一个定时器周期。
            # ⚠️ 钉的是 `C.needMouse` **而不是** `C.state.health`：后者会被 2.5 s 的
            #    `refreshHealth` 覆盖回去 ⇒ 结果取决于"刷新有没有落进这 1.6 s 窗口"，
            #    **必然 flaky**（实测：`--sim` 绿而 plain 红，同一个判据两种结果）。
            #    钉判定函数才是确定性的 —— 与 B34 钉 `C.state.telemetry` 是同一个道理：
            #    **要让渲染确定，就钉住它的输入**，而不是钉住输入的来源。
            async def _hint_with(level, note, mv):
                await cdp.eval(
                    "(()=>{const real=window.__b36_api||C.api; window.__b36_api=real;"
                    " window.__b36_need=C.needMouse;"
                    " const realApi=real;"
                    " C.api=function(p,o){"
                    "  if(String(p).indexOf('/mouse/launch_precheck')>=0){"
                    "   return Promise.resolve(" + json.dumps(
                        {"level": level, "batt_mv": mv, "min_mv": 7000, "note": note}) + ");}"
                    "  return realApi(p,o);};"
                    " C.needMouse=function(){return null;};"
                    " C.mods.mod_maze8.onHealth();"
                    " const h=document.getElementById('m8-hint');"
                    " if(h){h.textContent='';}"
                    " return true;})()")
                await asyncio.sleep(1.6)          # 等一个 TICK_MS 周期（1 s）让预检真跑一遍
                got = await cdp.eval(
                    "((document.getElementById('m8-hint')||{}).textContent||'').trim()")
                await cdp.eval(
                    "(()=>{if(window.__b36_api)C.api=window.__b36_api;"
                    " if(window.__b36_need)C.needMouse=window.__b36_need;"
                    " try{C.mods.mod_maze8.onHealth();}catch(e){}"
                    " return true;})()")
                return got

            low_note = ("电池只有 4260 mV，低于发车下限 7000 mV，发车会被拦下。"
                        "低电时红外发射管变暗、墙检测会失效。请充电或换电池后重试。")
            warn_txt = await _hint_with("warn", low_note, 4260)
            ok_txt = await _hint_with("ok", "", 7680)
            # 正：warn 时那句话要**真的出现在界面上**，而且要带上实测电压
            if "4260" not in warn_txt:
                b36_bad.append(f"低电预检没有把实测电压摆到界面上：{warn_txt!r}")
            if "7000" not in warn_txt:
                b36_bad.append(f"低电警告里没有下限：{warn_txt!r}")
            # 负：ok 时那一行必须是空的（"永远有字的提示 = 没提示"）
            if ok_txt:
                b36_bad.append(f"电量正常时门控行不该有字，却有：{ok_txt!r}")
            b36_ok = not b36_bad
            record("B36", "PASS" if b36_ok else "FAIL",
                   f"无记忆走迷宫有【收车】【救援】、设备窗口有【救援（软复位）】；"
                   f"低电预检会把实测电压摆到门控行（warn ⇒ {warn_txt[:46]}…），"
                   f"**电量正常时那一行为空**（负向）"
                   if b36_ok else
                   f"**收车/救援按钮或低电门控不对**：{b36_bad[:3]}")

            # 后面逐页断言打的是「综合运动」界面 —— 上面切到新模块后要**切回来**，
            # 否则 `#ws-all` 是 `display:none`，`innerText` 全空，后面每一条都会假红。
            await cdp.eval("C.openMod('all','@all')")
            await asyncio.sleep(0.3)

            head("3. 逐页切换（两栏各自真的渲染一遍）")
            for tab in TABS + DOCK_VIEWS:
                n_err0 = len(await cdp.snapshot_console())
                rail = RAIL_OF_PY[tab]
                try:
                    await cdp.eval(tab_click_js(tab))
                    await asyncio.sleep(args.per_tab)
                    # 视图里的数据是**异步**拉回来的（相机端点尤其慢：探不到服务时
                    # 每个要干等 ~2 s），固定等 1.5 s 会让断言读到"还没渲染完"的中间态，
                    # 于是判据随机红 —— 那不是界面错，是**我们没等它**。
                    # 所以这里等到 innerHTML 长度连续两次读数不变（或超时）再断言。
                    last_n = -1
                    for _ in range(12):
                        n = await cdp.eval(
                            "(()=>{const v=document.querySelector('.view[data-tab=\"%s\"]');"
                            "return v ? (v.innerHTML||'').length : -1;})()" % tab)
                        if n == last_n:
                            break
                        last_n = n
                        await asyncio.sleep(0.55)
                    ctx_id = ("ctx-" + rail) if rail in ("cam", "mouse") else "dock-body"
                    info = await cdp.eval(
                        "(()=>{const v=document.querySelector('.view[data-tab=\"%s\"]');"
                        "const ctx=document.getElementById('%s');"
                        "const rd=document.getElementById('readouts');"
                        # 同 B0/B2：空盒子不算，设备窗口里的故障框也不算（它由 B29 判）
                        "const er=Array.from(document.querySelectorAll('.errbox'))"
                        ".filter(e=>!e.closest('#dev-modal')"
                        "&&(e.textContent||'').trim()).length;"
                        "const toast=document.getElementById('toast');"
                        "return {on: !!(v&&v.classList.contains('on')),"
                        " text:((v&&v.innerText)||'').length,"
                        " html:((v&&v.innerHTML)||'').length,"
                        " ctx:(ctx.innerText||'').length,"
                        " readouts:(rd.innerText||'').length,"
                        " errbox:er,"
                        " toast:(toast.innerText||'').trim()};})()" % (tab, ctx_id))
                except Exception as e:                        # noqa: BLE001
                    # 单页炸了也要继续查后面几页 —— 否则第一个 bug 会把剩下的全遮住
                    record("B." + tab, "FAIL", f"切换/读取该页时抛异常：{e}")
                    if shots:
                        try:
                            s = await cdp.call("Page.captureScreenshot", {"format": "png"})
                            (shots / f"{tab}.png").write_bytes(base64.b64decode(s["data"]))
                        except Exception:                     # noqa: BLE001
                            pass
                    continue
                errs = await cdp.snapshot_console()
                # **只有未捕获异常算这一页的硬伤**；4xx/console.error 在"没插硬件"时
                # 是预期行为，单列在 B10/B11 里，不混进逐页判定。
                hard = [e for e in errs[n_err0:] if e.get("kind") == "exception"]
                soft = [e for e in errs[n_err0:] if e.get("kind") != "exception"]
                # 针对性断言：这一页的**关键控件**真的在，而且有值
                probe, probe_err = None, None
                expr = PAGE_PROBES.get(tab)
                if expr:
                    try:
                        probe = await cdp.eval(expr)
                    except Exception as e:                    # noqa: BLE001
                        probe_err = f"{type(e).__name__}: {e}"
                if shots:
                    s = await cdp.call("Page.captureScreenshot", {"format": "png"})
                    (shots / f"{tab}.png").write_bytes(base64.b64decode(s["data"]))
                probe_ok = True
                if probe_err:
                    probe_ok = False
                elif isinstance(probe, dict):
                    probe_ok = bool(probe.get("ok"))
                ok = info["on"] and info["html"] > 200 and not hard \
                    and info["errbox"] == 0 and probe_ok
                record("B." + tab, "PASS" if ok else "FAIL",
                       f"内容 {info['html']} B / 文本 {info['text']} 字 / 上下文 {info['ctx']} 字"
                       f" / errbox {info['errbox']}"
                       + (f" / 断言 {probe.get('note')}" if isinstance(probe, dict) else "")
                       + (f" / **断言失败**：{probe_err}" if probe_err else "")
                       + (f" / **未捕获异常 {len(hard)} 条**：{hard[:2]}" if hard else "")
                       + (f" / 预算内错误 {len(soft)} 条" if soft else ""))

            head("4. WS 实时通道")
            ws_state = await cdp.eval(
                "(()=>{const w=document.getElementById('ws-dot');"
                "return {dot: w ? w.className : null,"
                " lamp: (document.getElementById('lamp-mouse')||{}).className};})()")
            got_hello = await cdp.eval(
                "(()=>{const cl=document.getElementById('console-line');"
                "return (cl.innerText||'').length;})()")
            dot_ok = "live" in str(ws_state.get("dot") or "")
            record("B9", "PASS" if dot_ok else "FAIL",
                   f"WS 指示「{ws_state.get('dot')}」；底部文本台已收到 {got_hello} 字"
                   f"（WS 真在推，不是只有 HTTP 轮询）")

            # ⚠️ **页面还活着吗** —— 这条判据是本轮最贵的一次教训换来的。
            # `/health` 里的 `uptime_s` 每份都不一样，所以"它在涨"就等于
            # "页面又拿到一份新 health"，也就是"那条 2.5 s 心跳真的在跑"。
            #
            # 为什么必须单独钉一条：心跳原先写在启动链的**最后**，于是前面任何一步
            # 变慢（实测是 `/caps` 把服务端事件循环占住十几秒），`setInterval` 就永远
            # 装不上 —— 页面**不报任何错**，只是静静地停在启动那一刻：顶栏是旧状态、
            # 每个视图是空壳。那种情况下逐页断言会各自报红（B.capture / B.depart），
            # 而红灯会把人引到"这一页坏了"，真相却是"整页都死了"。
            # 判据的职责就是能说出这是哪一类。
            up1 = await cdp.eval("((C.state.health||{}).uptime_s)")
            await asyncio.sleep(3.2)
            up2 = await cdp.eval("((C.state.health||{}).uptime_s)")
            hb_ok = (isinstance(up1, (int, float)) and isinstance(up2, (int, float))
                     and up2 > up1)
            record("B22", "PASS" if hb_ok else "FAIL",
                   f"心跳在跑：3.2 s 内 health.uptime_s 从 {up1} 涨到 {up2}"
                   f"（不是停在启动那一刻的死页面）"
                   if hb_ok else
                   f"**页面不再刷新 health**：3.2 s 内 uptime_s 停在 {up1}"
                   f"⇒ 2.5 s 那条心跳没装上（启动链里任何一步变慢都会这样，"
                   f"而页面自己不会报错）")

            head("5. 坏请求（JS/CSS 404、5xx 会在这里现形）")
            bad = await cdp.bad_requests()
            # ⚠️ **5xx 里有一类是本轮的预期**：`--no-fake-cam` 跑的是"相机不可达"，
            # 此时 `/api/v1/camera/mjpeg` 会如实回 502（反代连不上上游）。
            # 那不是硬伤 —— 恰恰相反：**静默回一个永远空白的 200 流**才是更坏的行为
            # （前端连"失败了"都看不出来）。所以这里按**本轮的状态**放行它，并如实计数，
            # 而不是把 5xx 一律算红（那样会让"如实报错"被惩罚）。
            cam_now = (read_health(base).get("camera") or {})
            cam_down = (cam_now.get("reachable") is False
                        or cam_now.get("available") is False)
            expected_5xx = [b for b in bad
                            if cam_down and "/api/v1/camera/" in str(b.get("url") or "")
                            and str(b.get("kind") or "").startswith("HTTP 5")]
            rest = [b for b in bad if b not in expected_5xx]
            hard_bad = [b for b in rest if is_hard_request(b)]
            soft_bad = [b for b in rest if not is_hard_request(b)]
            note_extra = (f"；另有 {len(expected_5xx)} 条**预期的** 5xx"
                          f"（相机不可达 ⇒ 反代如实回 502）" if expected_5xx else "")
            record("B10", "PASS" if not hard_bad else "FAIL",
                   ("无硬伤请求；" + (f"预期内的 4xx {len(soft_bad)} 条（设备没接时正常）"
                                      if soft_bad else "零失败请求") + note_extra)
                   if not hard_bad else
                   f"**有 {len(hard_bad)} 条**：{hard_bad[:4]}{note_extra}")

            head("6. 未捕获 JS 异常汇总（`console.error` 不计，那是 C.guard 的正常日志）")
            errs = await cdp.snapshot_console()
            hard_exc = [e for e in errs if e.get("kind") == "exception"]
            soft_exc = [e for e in errs if e.get("kind") != "exception"]
            record("B11", "PASS" if not hard_exc else "FAIL",
                   f"整场零未捕获异常；另有 {len(soft_exc)} 条 console/log 级别的预期错误"
                   if not hard_exc else
                   f"**{len(hard_exc)} 条**：" + "; ".join(
                       f"{(e.get('text') or '')[:120]}" for e in hard_exc[:3]))

            # ------------------------------------------------------------ 7
            '''「先手动连接、再测试」这套流程的验证。
            要点：**不靠肉眼看截图**（截图只能说"有东西"，说不出"按钮到底能不能点"），
            全部用 DOM 断言 —— 而"二维码真的画出了非空白像素"这种，
            只有把 Canvas 的像素读出来数一遍才算数。'''
            head("7. 准备清单（先连好、再测试）")
            # 清单在**底部整合栏**里（它是三路合起来的结论，不属于任何一栏），
            # 所以先展开它 —— 收起状态下 display:none，innerText 读出来是空串。
            await cdp.eval(
                "(()=>{const d=document.getElementById('dock');"
                "if(d&&!d.classList.contains('on'))"
                "  document.getElementById('dock-head').click();"
                "return 1;})()")
            await asyncio.sleep(0.8)
            prep = await cdp.eval(
                "(()=>{const l=document.getElementById('prep-list');"
                "if(!l) return {err:'没有 #prep-list'};"
                "const its=Array.from(l.querySelectorAll('.it'));"
                "const bad=its.filter(e=>!e.classList.contains('ok'));"
                "const noact=bad.filter(e=>!((e.querySelector('.ac')||{}).textContent||'').trim());"
                "const sum=document.getElementById('prep-summary');"
                "return {n:its.length, bad:bad.length, noact:noact.length,"
                " sum:(sum?sum.textContent:''),"
                " first:(its[0]?(its[0].innerText||'').split('\\n')[0]:'')};})()")
            prep_ok = (isinstance(prep, dict) and prep.get("n", 0) >= 3
                       and prep.get("noact") == 0)
            record("B12", "PASS" if prep_ok else "FAIL",
                   f"清单 {prep.get('n')} 项、其中未就绪 {prep.get('bad')} 项，"
                   f"每一项都写了「去做」（缺 {prep.get('noact')} 项）；"
                   f"汇总标记「{prep.get('sum')}」；第一项「{prep.get('first')}」"
                   if prep_ok else f"清单没渲染好：{prep}")

            # 界面只放**状态 / 数值 / 控件 / 必需的下一步**，不放解释性文字。
            # 这条用标记来判：说明性文本必然带 Markdown 残留或判据编号，
            # 它们出现在界面上一定是"写文档的手感漏进了界面"。
            # 之所以要有这条自动判据：清理完靠肉眼看很容易漏，而漏下的那几句
            # 恰恰是最显眼的（用户第一眼就会看到）。
            cmt = await cdp.eval(
                # 扫**界面自己的文字**：顶栏 / 两栏页签 / 快捷条 / 每栏数据条 / 底栏 /
                # 读数 / 当前可见的视图。
                # **跳过 `[data-log]` 容器**（底部控制台、设备文本流、服务日志）—— 那三处是
                # 原样落下来的文本流，里面出现任何字符都正常；把它们算进来，这条判据就会被
                # 日志刷成假红（真踩过：一条日志里的破折号让 B13 连红三轮）。
                # **同样跳过 `[data-raw]`**（`#d-explain` / `#x-lab-out` / `#x-lesson-out`）：
                # 那几处原样显示**上游返回的** `explain`，上游自己会写破折号 ——
                # 那是数据，不是我们写的界面文案。判据只管我们写的那部分。
                # 实现上**逐文本节点走 DOM**、而不是读 innerText：innerText 对
                # `display:none` 的元素会退化成 textContent，反而会把想排除的东西带回来。
                "(()=>{const out=[],where=[];"
                "const walk=function(n,tag){"
                "  Array.prototype.forEach.call(n.childNodes,function(c){"
                "    if(c.nodeType===3){out.push(c.nodeValue);where.push(tag);}"
                "    else if(c.nodeType===1){"
                "      if(c.hasAttribute('data-log')||c.hasAttribute('data-raw')) return;"
                "      walk(c,tag);}});};"
                "const SEL=['#top','#rail-cam','#rail-mouse','#quickbar',"
                "'#dock','#readouts','#nav','#mod','.ctx'];"
                "SEL.forEach(function(s){document.querySelectorAll(s).forEach(function(e){"
                " walk(e,s);});});"
                "let txt=out.join('\\n');"
                "const marks=[['**','Markdown 星号'],['`','反引号'],"
                " ['判据 I','判据编号'],['§','章节号'],['——','解释性破折号']];"
                "const hit=[];const bad=[];"
                "marks.forEach(function(p){const c=txt.split(p[0]).length-1;"
                " if(c) hit.push(p[1]+' ×'+c);});"
                "const lines=(txt.split('\\n')||[]).map(function(s){return s.trim();})"
                ".filter(function(s){return s.length>0;});"
                "let mx='';lines.forEach(function(s){if(s.length>mx.length)mx=s;});"
                # 命中的**在哪一块、原文是什么**也要报出来 —— 只说"有破折号"的话，
                # 面对一屏数据得自己去翻是哪一条（这里真翻过）。
                "out.forEach(function(t,i){marks.forEach(function(p){"
                " if(t.indexOf(p[0])>=0)"
                "  bad.push(where[i]+' › '+t.trim().slice(0,60));});});"
                "return {ok: !hit.length, hit:hit.join('、'), bad:bad.slice(0,3),"
                " nlines:lines.length, longest:mx.slice(0,80), maxlen:mx.length};})()")
            record("B13", "PASS" if cmt.get("ok") else "FAIL",
                   f"界面无说明性标记（可见文本 {cmt.get('nlines')} 行，"
                   f"最长一行 {cmt.get('maxlen')} 字）"
                   if cmt.get("ok") else
                   f"**界面里还残留说明性文本**：{cmt.get('hit')}"
                   f"　命中：{cmt.get('bad')}")

            # B23：界面文案纪律 —— 只放状态/数值/控件/下一步，不许出现口语词与图形标记。
            # 与 B13 同一套 DOM 走法（逐文本节点、跳过 `[data-log]` 与 `[data-raw]`），
            # 但两者判的是两件事：
            # B13 抓"写文档的手感漏进界面"（Markdown 残留、判据编号），
            # B23 抓"写聊天的手感漏进界面"（口语词、表情符号）。
            # 误报防护：词表只用**多字词 + 极低频单字语气词**，不含"别/你"这类
            # 会藏进常用词的单字（"识别""迷你"）；符号只认象形 emoji 与勾叉警示符，
            # **不碰** `▸ ● — ← →` 这些界面上有意使用的几何/箭头字形。
            kw = await cdp.eval(
                "(()=>{const out=[];"
                "const walk=function(n){"
                "  Array.prototype.forEach.call(n.childNodes,function(c){"
                "    if(c.nodeType===3){out.push(c.nodeValue);}"
                "    else if(c.nodeType===1){"
                "      if(c.hasAttribute('data-log')||c.hasAttribute('data-raw')) return;"
                "      walk(c);}});};"
                "document.querySelectorAll('#top,#rail-cam,#rail-mouse,#quickbar,"
                "#dock,#readouts,#nav,#mod,.ctx,.modal-box').forEach(function(e){ walk(e); });"
                "const lines=(out.join('\\n').split('\\n')||[])"
                "  .map(function(s){return s.trim();}).filter(function(s){return s.length;});"
                "const words=['咱们','我们','白等','白跑','刷屏','干等','搞定','弄好','顺手',"
                " '其实','居然','对吧','对不对','跑一遍','看一眼','点一下','才行','就行了',"
                " '没事','挺好','怎么样','干啥','吗','嘛','啦','哦','呀','喽','呗'];"
                "const emo=/[\\u{1F000}-\\u{1FAFF}\\u{2600}-\\u{27BF}\\u{2B00}-\\u{2BFF}\\u{FE0F}]/u;"
                "const hit=[];"
                "words.forEach(function(w){const c=lines.filter(function(s){"
                "  return s.indexOf(w)>=0;}).length;"
                "  if(c) hit.push(w+' ×'+c);});"
                "const el=lines.filter(function(s){return emo.test(s);}).length;"
                "if(el) hit.push('图形标记 ×'+el);"
                "let bad='';"
                "if(hit.length){const h=hit.join(' ');"
                "  for(let i=0;i<lines.length;i++){let s=lines[i];"
                "    if(words.some(function(w){return s.indexOf(w)>=0;})||emo.test(s)){"
                "      bad=s.slice(0,80);break;}}}"
                "return {ok: !hit.length, hit:hit.join('、'), bad:bad,"
                " nlines:lines.length};})()")
            record("B23", "PASS" if kw.get("ok") else "FAIL",
                   f"界面文案无口语词与图形标记（可见文本 {kw.get('nlines')} 行）"
                   if kw.get("ok") else
                   f"**界面文案出现口语词或图形标记**：{kw.get('hit')}"
                   f"　例：「{kw.get('bad')}」")

            head("8. 门控：没连上的模块，对应按钮必须点不动")
            # 先读一次 health，按它算出"每个按钮**应该**是什么状态"，再去 DOM 里核对。
            # 这样无论这次跑有没有接模拟器，断言都成立（连上就该能点，没连就该灰）。
            #
            # ⚠️ 核对之前必须先让**页面与服务端落到同一时刻**。设备刚断开时（最典型的
            # 场景：上一轮 `--sim` 的模拟器刚被收掉），服务端的 `connected` 要等下一次
            # I/O 失败才翻过来，中间有一小段"页面说已连、health 说未连"的窗口 ——
            # 在那段窗口里核对门控必然 flaky，而那不是界面错，是状态还在动。
            # 所以：先催页面追平，再连读两次；两次不一致就**如实 SKIP**，不给假红。
            def _read_health():
                return read_health(base)

            try:
                await cdp.eval("C.refreshHealthNow()")
                await asyncio.sleep(1.0)
                hh = _read_health()
                await asyncio.sleep(1.3)
                hh2 = _read_health()
                if (bool((hh.get("mouse") or {}).get("connected"))
                        != bool((hh2.get("mouse") or {}).get("connected"))):
                    record("B14", "SKIP",
                           "鼠标连接状态在核对期间还在变（设备刚接上/刚断开）"
                           "⇒ 这一节不作数；隔几秒重跑一次即可")
                    hh = {}
                else:
                    hh = hh2                                  # 用较新的那一份
            except Exception as e:                            # noqa: BLE001
                hh = {}
                record("B14", "SKIP", f"读 health 失败：{e}")
            if hh:
                mouse_ok = bool((hh.get("mouse") or {}).get("connected"))
                cam = hh.get("camera") or {}
                ph = cam.get("phone") or {}
                cam_ok = bool(cam.get("offline")) or (
                    bool(cam.get("reachable")) and ph.get("clients") != 0)
                for cid, sel, need, label in GATE_CHECKS:
                    want = not (mouse_ok if need == "mouse" else cam_ok)
                    got = await cdp.eval(
                        f"(()=>{{const b=document.querySelector('{sel}');"
                        f"return b ? !!b.disabled : null;}})()")
                    ok = (got == want)
                    record(cid, "PASS" if ok else "FAIL",
                           f"{label}（需要{'鼠' if need == 'mouse' else '相机'}）"
                           f"：{'已连 ⇒ 可点' if (need == 'mouse' and mouse_ok) or (need == 'camera' and cam_ok) else '未连 ⇒ 应禁用'}"
                           f"，实测 disabled={got}，期望 disabled={want}"
                           if ok else
                           f"**{label} 的门控不对**：disabled 实测 {got}、期望 {want}"
                           f"（{'鼠已连' if mouse_ok else '鼠未连'} / "
                           f"{'相机就绪' if cam_ok else '相机未就绪'}）")

            # 急停**故意不门控** —— 连不上的时候恰恰最可能想按它。这条反例要钉住。
            estop_disabled = await cdp.eval(
                "(()=>{const b=document.querySelector('#btn-estop');"
                "return b?!!b.disabled:null;})()")
            record("B15", "PASS" if estop_disabled is False else "FAIL",
                   "顶栏【急停】不被门控（B15 就是它的反例：连不上的时候最需要它）"
                   if estop_disabled is False else
                   f"**急停被禁用了**（disabled={estop_disabled}）—— 这会让最需要它的时刻按不下去")

            head("9. 配对二维码（手机连的就是它，现在在顶栏【相机】窗口里）")
            pair = await cdp.eval(
                "(()=>{document.querySelector('.lamp[data-dev=\"camera\"]').click();"
                "return 1;})()")
            await asyncio.sleep(1.2)
            pinfo = await cdp.eval(
                "(()=>{const m=document.getElementById('dev-modal');"
                "const cv=document.getElementById('dc-qr');"
                "const inf=document.getElementById('dc-pair-info');"
                "const fb=document.getElementById('dc-pair-fallback');"
                "const pane=document.querySelector('.dev-pane[data-pane=\"camera\"]');"
                "let black=-1;"
                "if(cv.width>0&&cv.height>0){"
                "  const d=cv.getContext('2d').getImageData(0,0,cv.width,cv.height).data;"
                "  black=0; for(let i=0;i<d.length;i+=4){ if(d[i]<128) black++; } }"
                "return {open:m.classList.contains('on'),"
                " paneOn: !!(pane&&pane.classList.contains('on')),"
                " w:cv.width, h:cv.height,"
                " black:black, infolen:(inf.innerText||'').length,"
                " hasurl:((inf.innerText||'').indexOf('token=')>=0),"
                " fb:(fb.innerText||'').slice(0,90)};})()")
            drew = (pinfo.get("w", 0) > 0 and pinfo.get("black", 0) > 50)
            panel_ok = bool(pinfo.get("open") and pinfo.get("paneOn"))
            if panel_ok and drew:
                verdict, note = "PASS", (
                    f"点顶栏【相机】⇒ 窗口已开、camera 面板在前；Canvas "
                    f"{pinfo.get('w')}×{pinfo.get('h')}、"
                    f"黑色模块像素 {pinfo.get('black')} 个（**真画出来了**，不是一张空白画布）；"
                    f"信息区 {pinfo.get('infolen')} 字、含配对网址={pinfo.get('hasurl')}")
            elif panel_ok and pinfo.get("fb"):
                # 没有相机服务时，这里要验的是**降级路径**（窗口说清为什么看不了），
                # 而不是硬要一张根本画不出来的画布 —— 判不了 ≠ 不通过。
                verdict, note = "SKIP", f"相机服务不可达，只验证到降级提示：{pinfo.get('fb')}"
            else:
                verdict, note = "FAIL", f"**二维码没画出来**：{pinfo}"
            record("B16", verdict, note)
            if pinfo.get("fb"):
                print(f"         降级说明：{pinfo['fb']}")

            # 服务不可达时，这一屏必须**给得出可照做的下一步** —— 而这一步在
            # `c.detail` 的**第 2 行起**（第 1 行是上游那串 WinError 10061）；
            # 早先界面只取第 1 行 ⇒ 现场看到一串报错、却不知道要去启动上游服务（真踩过）。
            # 反过来，服务可达时**不许留着上一次的故障**：陈旧故障比没有故障更误导。
            # 分支键取 `hasurl`（配对内容里有没有 token）而不是某句文案 ——
            # "拿到真数据"是结构事实，"写着不可达"是会变的自然语言（纪律 29）。
            fault = await cdp.eval(
                "(()=>{const f=document.getElementById('dc-fault');"
                "const g=function(i){const b=document.getElementById(i);"
                "return b?!!b.disabled:null;};"
                "return {txt:f?(f.innerText||'').trim():'MISSING',"
                " png:g('dc-pair-png'), copy:g('dc-pair-copy'),"
                " conn:g('dc-pair-then-connect'), refresh:g('dc-pair-refresh')};})()")
            actions = [ln.strip() for ln in fault["txt"].splitlines()
                       if ln.strip().startswith("·")]
            if pinfo.get("hasurl"):
                b29_ok = (fault["txt"] == "" and fault["png"] is False
                          and fault["copy"] is False and fault["conn"] is False
                          and fault["refresh"] is False)
                record("B29", "PASS" if b29_ok else "FAIL",
                       "相机服务可达：这一屏不留陈旧故障，配对四个按钮都可用"
                       if b29_ok else f"**可达时状态不对**：故障框=「{fault['txt'][:60]}」{fault}")
            else:
                b29_ok = (len(actions) >= 1 and fault["png"] and fault["copy"]
                          and fault["conn"] and fault["refresh"] is False)
                record("B29", "PASS" if b29_ok else "FAIL",
                       f"相机服务不可达：写出可照做的 {len(actions)} 条、"
                       f"禁用三个配对按钮（保留【刷新】）"
                       if b29_ok else f"**不可达时没给出下一步**：{fault}")

            if shots:
                s = await cdp.call("Page.captureScreenshot", {"format": "png"})
                (shots / "_cam_modal.png").write_bytes(base64.b64decode(s["data"]))

            # ── B32：**浏览器真的把 MJPEG 画面渲染出来了**（用户报过的那一条） ──
            # 现场现象：手机在发、服务在转、预览窗口**永远空白**，而服务端日志看不出问题。
            # 根因是反代把 `Content-Type` 的 boundary 写死了（与上游不一致）⇒
            # 浏览器按错误的分界切分 ⇒ 一帧也解不出来。
            # 所以这条判据必须落到**浏览器**上：`naturalWidth > 0` 才算"真的解出了一帧"，
            # 光看服务端返回 200 是不够的（写错 boundary 时它照样 200、照样在转发字节）。
            head("7c. 判据 B32：图像回传测试真的出画面（浏览器解出了帧）")
            cam_up_before = (read_health(base).get("camera") or {}).get("reachable")
            if fake_cam is None:
                record("B32", "SKIP",
                       "本轮用的是**真上游**相机服务（没有假 MJPEG 源）"
                       "⇒ 画面能不能出来取决于相机会话，判不了")
            elif cam_up_before is False:
                record("B32", "SKIP", "相机不可达（这一轮测的是降级路，不测画面）")
            else:
                base_before = (read_health(base).get("camera") or {}).get("base")
                try:
                    _post(base, "/api/v1/camera/base",
                          {"base": f"http://127.0.0.1:{args.fake_cam_port}"})
                    _post(base, "/api/v1/camera/probe", {})
                    await asyncio.sleep(0.5)
                    await cdp.eval("C.openMod('cam','mod_cam')")
                    await asyncio.sleep(0.4)
                    await cdp.eval(
                        "(()=>{const b=document.getElementById('mcam-gray');"
                        "if(b&&!b.disabled)b.click();return 1;})()")
                    # 上游那条约 1 Hz；假上游 0.2 s 一帧 ⇒ 3 s 足够
                    await asyncio.sleep(3.0)
                    mj = await cdp.eval(
                        "(()=>{const i=document.getElementById('mcam-img');"
                        "const n=document.getElementById('mcam-ph');"
                        "if(!i)return {miss:true};"
                        "return {miss:false, complete:!!i.complete,"
                        " nw:i.naturalWidth||0, nh:i.naturalHeight||0,"
                        " vis:getComputedStyle(i).display!=='none',"
                        " src:(i.getAttribute('src')||'').slice(0,40),"
                        # 读 `<span>` 的**可见性**才是"提示有没有被清掉"的证据；
                        # textContent 即使 display:none 也照样有内容（会自欺）。
                        " noteVis:!!(n&&getComputedStyle(n).display!=='none'),"
                        " note:n?(n.textContent||'').slice(0,40):''};})()")
                    if shots:
                        s = await cdp.call("Page.captureScreenshot", {"format": "png"})
                        (shots / "mod_cam.png").write_bytes(base64.b64decode(s["data"]))
                    drew = bool(mj.get("nw", 0) > 0 and mj.get("nh", 0) > 0)
                    record("B32", "PASS" if drew else "FAIL",
                           f"点【灰度】⇒ `<img>` 解出画面 {mj.get('nw')}×{mj.get('nh')}"
                           f"（naturalWidth>0 ⇒ **浏览器真的按 boundary 切出了帧**）、"
                           f"「等待第一帧」提示已自动隐藏（{mj.get('noteVis')}）"
                           if drew else
                           f"**画面没出来**：{mj}\n"
                           f"      ⇒ 反代的 boundary 与上游不一致时就是这个现象："
                           f"服务端 200、字节也在转发，浏览器却一帧都切不出来")
                except Exception as e:                        # noqa: BLE001
                    record("B32", "FAIL", f"验证画面时抛异常：{type(e).__name__}: {e}")
                finally:
                    if base_before:
                        try:
                            _post(base, "/api/v1/camera/base", {"base": base_before})
                            _post(base, "/api/v1/camera/probe", {})
                        except Exception:                     # noqa: BLE001
                            pass
                    try:
                        await cdp.eval(
                            "(()=>{const b=document.getElementById('mcam-stop');"
                            "if(b)b.click();return 1;})()")
                    except Exception:                         # noqa: BLE001
                        pass
                    # ⚠️ **必须切回综合运动台**：上面为了点【灰度】打开了模块界面，
                    # 而后面 B18/B19/B20/B21 判的是「综合运动台」那两栏 ——
                    # 不切回去它们会读到 `display:none`（宽度 0），全部假红（踩过）。
                    try:
                        await cdp.eval("C.openMod('all','@all')")
                        await asyncio.sleep(0.4)
                    except Exception:                         # noqa: BLE001
                        pass

            # ── B31：上游程序的设置面在窗口里真的画出来了 ──
            # 用户要求"设置里能填上游程序 + 配对时自动代启"。这一块全是**界面**：
            # 服务端那半边由 drill 的 I24/I25 判，这里判"用户能不能看到、能不能按"。
            # 三条：控件在 → 【启动】在不可达时**必须可用**（它就是出路）→
            # 自动启动开关的勾选状态跟着服务端的设置走（不是一个摆设）。
            lb = await cdp.eval(
                "(()=>{const q=function(i){return document.getElementById(i);};"
                "const box=q('dc-bin-kv');"
                "const start=q('dc-start'), stop=q('dc-stop'), chk=q('dc-autostart');"
                "const chkExit=q('dc-stoponexit');"
                "return {pane:!!document.querySelector('.dev-pane[data-pane=\"camera\"]'),"
                " hasBox:!!box, hasInput:!!q('dc-bin'), hasSave:!!q('dc-bin-set'),"
                " hasStart:!!start, hasStop:!!stop, hasChk:!!chk, hasChkExit:!!chkExit,"
                " txt:box?(box.innerText||'').replace(/\\s+/g,' ').slice(0,180):'',"
                " startOff:start?start.disabled:null, stopOff:stop?stop.disabled:null,"
                " chk:chk?chk.checked:null, chkExit:chkExit?chkExit.checked:null,"
                " enabled:!!((C.state.health||{}).camera||{}).launcher&&"
                "   ((C.state.health||{}).camera.launcher.enabled===true),"
                " paused:((C.state.health||{}).camera.launcher||{}).paused===true};})()")
            present = all(lb.get(k) for k in ("pane", "hasBox", "hasInput", "hasSave",
                                              "hasStart", "hasStop", "hasChk", "hasChkExit"))
            # 读数区必须**说得出收工策略** —— 否则"Ctrl+C 之后还剩个上游进程"
            # 在界面上是个无法解释的现象（用户专门问过这一条）。
            says = (("程序" in lb.get("txt", "")) and ("端口" in lb.get("txt", ""))
                    and ("收工" in lb.get("txt", "")))
            # 【启动】该不该能按，**要看三种情况**，不是"不可达就必须能按"：
            #   服务已在跑        → 禁用（点了只会回"已在运行"）
            #   不可达 + 允许代启  → **能用**（这就是它存在的唯一理由）
            #   不可达 + 已暂停/关掉 → 禁用（代启根本不会动手，能按才是骗人）
            want_start = ((not pinfo.get("hasurl")) and lb.get("enabled") is True
                          and lb.get("paused") is False)
            b31_ok = (present and says and lb.get("startOff") == (not want_start))
            record("B31", "PASS" if b31_ok else "FAIL",
                   f"相机窗口有「上游程序」设置：路径输入 + 保存 + 【启动上游服务】"
                   f"(禁用={lb.get('startOff')}，该按={want_start}) + 【停止】"
                   f"(禁用={lb.get('stopOff')}) + 开关(自动启动={lb.get('chk')}"
                   f"／收工停止={lb.get('chkExit')})；读数：{lb.get('txt')[:70]}"
                   if b31_ok else f"**上游程序设置块不对**：{lb}")

            # 关掉之后画布要清零 —— token 留在屏幕上没有任何好处
            await cdp.eval("document.getElementById('dev-close').click()")
            await asyncio.sleep(0.4)
            closed = await cdp.eval(
                "(()=>{const m=document.getElementById('dev-modal');"
                "const cv=document.getElementById('dc-qr');"
                "return {open:m.classList.contains('on'), w:cv.width};})()")
            record("B17", "PASS" if (not closed.get("open") and closed.get("w") == 0) else "FAIL",
                   "已关；画布清空（token 不留在屏幕上）"
                   if (not closed.get("open") and closed.get("w") == 0)
                   else f"关掉后仍有残留：{closed}")

            head("10. 未连接时开会话必须被当场拦住（前端先解释，服务端也拦）")
            opened = await cdp.eval(
                "(()=>{const b=document.getElementById('btn-open');"
                "return {disabled: !!b.disabled, title: b.title || ''};})()")
            if not mouse_ok and not cam_ok:
                record("B18", "PASS" if opened.get("disabled") else "FAIL",
                       f"顶栏【开始记录】已禁用，悬停提示：「{opened.get('title')[:70]}」"
                       if opened.get("disabled") else
                       f"**一路都没连时【开始记录】仍可点**（{opened}）")
            else:
                record("B18", "PASS" if not opened.get("disabled") else "FAIL",
                       "已连上至少一路 ⇒ 【开始记录】可点（门控没有误伤）"
                       if not opened.get("disabled") else f"误伤：{opened}")

            # ------------------------------------------------------------ 11
            '''本轮界面的两条核心要求，必须由机器盯着：

              ① **状态只在顶栏，点标签开窗口设参数连接** —— 所以标签得真的可点，
                 点了得真的切到对应面板（不能"点开都是同一个画面"）。
              ② **相机控制与小车控制并排、同时可见可操作** —— 所以两栏各自的 active
                 视图必须同时是 `.on`，而且两栏在几何上真是左右并排（不是叠在一起）。
            '''
            head("11. 顶栏设备窗口（点标签打开）+ 两栏同时可见")
            pane_map = []
            for kind, title in (("mouse", "电脑鼠"), ("camera", "手机相机"),
                                ("locator", "场地定位"), ("clock", "时间基")):
                r = await cdp.eval(
                    "(()=>{document.querySelector('.lamp[data-dev=\"%s\"]').click();"
                    "const m=document.getElementById('dev-modal');"
                    "const p=document.querySelector('.dev-pane[data-pane=\"%s\"]');"
                    "const ons=Array.from(document.querySelectorAll('.dev-pane.on'))"
                    ".map(function(e){return e.dataset.pane;});"
                    "return {open:m.classList.contains('on'),"
                    " title:(document.getElementById('dev-title')||{}).textContent,"
                    " paneOn:!!(p&&p.classList.contains('on')), ons:ons,"
                    " txt:((p&&p.innerText)||'').length};})()" % (kind, kind))
                await asyncio.sleep(0.35)
                ok = (r.get("open") and r.get("paneOn") and r.get("ons") == [kind]
                      and r.get("title") == title)
                pane_map.append((kind, title, ok, r))
            bad = [f"{t}（{r}）" for _, t, ok, r in pane_map if not ok]
            record("B19", "PASS" if not bad else "FAIL",
                   "点四个标签 ⇒ 各自只显示自己的面板、标题正确："
                   + "、".join(f"{t} {r.get('txt')} 字" for _, t, _, r in pane_map)
                   if not bad else f"**面板切换不对**：{bad}")
            if shots:
                s = await cdp.call("Page.captureScreenshot", {"format": "png"})
                (shots / "_dev_modal.png").write_bytes(base64.b64decode(s["data"]))

            # 设备窗口里同样不许出现解释性文字（B13 只管了常驻区域，弹窗要单独扫）。
            mtxt = await cdp.eval(
                "(()=>{const p=document.querySelector('.dev-pane.on');"
                "const t=(p&&p.innerText)||'';"
                "const marks=[['**','Markdown 星号'],['`','反引号'],"
                " ['判据 I','判据编号'],['§','章节号'],['——','解释性破折号']];"
                "const hit=[];marks.forEach(function(q){"
                " if(t.split(q[0]).length-1) hit.push(q[1]);});"
                "return {ok:!hit.length, hit:hit.join('、'), len:t.length,"
                " pane:(p?p.dataset.pane:'')};})()")
            record("B21", "PASS" if mtxt.get("ok") else "FAIL",
                   f"设备窗口（当前 {mtxt.get('pane')} 面板，{mtxt.get('len')} 字）无说明性标记"
                   if mtxt.get("ok") else f"**设备窗口里残留说明性文本**：{mtxt.get('hit')}")

            # ── B33：**"画面为什么没出来"必须能当场答出来** ──
            # 现场问得最多的一句是"是上位机的问题还是上游的问题"。上游 `monitor.last_thumb`
            # 就是那个分界（它到底推没推过画面），所以界面上必须**有这一格**。
            # 判的是**结构**而不是某句文案：相机窗口的 KV 里要有一行"监视道"，
            # 且它的文字能落进三类之一（已收到 / 上游未推过 / 未订阅）——
            # 三类都是"能照做"的，模糊的"等待中"不算。
            mon_txt = await cdp.eval(
                "(()=>{const b=document.getElementById('dc-state');"
                "if(!b)return {miss:true};"
                # KV 是并排的 `<b>名</b><span>值</span>`，innerText **不换行** ⇒
                # 取整行再截断会看不到关键词，报告会误导人。所以从"监视道"起截。
                "const t=b.innerText||'';"
                "const i=t.indexOf('监视道');"
                "const line=(i>=0)?t.slice(i, i+40):'';"
                "return {miss:false, line:line, has:!!line,"
                " kind: (line.indexOf('已收到')>=0) ? 'pushed'"
                "   : (line.indexOf('未推过')>=0) ? 'never'"
                "   : (line.indexOf('未订阅')>=0) ? 'nosub'"
                "   : (line.indexOf('判不了')>=0) ? 'unknown' : 'none'};})()")
            # 相机**不可达**时读不到上游 monitor 是应该的（判不了 ≠ 不通过）——
            # 那时只要求"这一行还在"（界面结构没缺），不要求它给得出结论。
            cam_up = bool(pinfo.get("hasurl"))
            b33_ok = (bool(mon_txt.get("has"))
                      and (mon_txt.get("kind") in ("pushed", "never", "nosub")
                           if cam_up else True))
            # 光"有一行字"不够 —— 还得**三种状态都给出可照做的答案**，其中就包括
            # 现场最常见的那一种（上游一次都没推过画面 ⇒ 该去手机端开预览）。
            # `C.monLine` 是纯函数，直接拿构造的 health 喂它，不必去凑真数据。
            mons = await cdp.eval(
                "(()=>{const f=function(o){try{return (C.monLine(o).text||'');}"
                "catch(e){return 'ERR:'+e.message;}};"
                "return {never:f({status:{monitor:{subscribers:1,last_thumb:null}}}),"
                " pushed:f({status:{monitor:{subscribers:1,last_thumb:{seq:7,bytes:2048}}}}),"
                " nosub:f({status:{monitor:{subscribers:0,last_thumb:null}}}),"
                " unknown:f({status:{}})};})()")
            three_ok = (("未推过" in mons.get("never", ""))
                        and ("已收到" in mons.get("pushed", "")
                             and "seq 7" in mons.get("pushed", ""))
                        and ("未订阅" in mons.get("nosub", ""))
                        and ("判不了" in mons.get("unknown", "")))
            # **两个方向都验**：切到"上游从没推过画面"（用户遇到的那种），
            # 这一格必须变成可照做的那句话 —— 而不是只验"有数据"那一路。
            never_line = ""
            if fake_cam is not None:
                try:
                    with urllib.request.urlopen(
                            f"http://127.0.0.1:{args.fake_cam_port}/__monitor?never=1",
                            timeout=6) as r:
                        r.read()
                    _post(base, "/api/v1/camera/probe", {})
                    await asyncio.sleep(0.6)
                    await cdp.eval("(()=>{C.refreshHealthNow&&C.refreshHealthNow();"
                                   "return 1;})()")
                    await asyncio.sleep(1.2)
                    never_line = await cdp.eval(
                        "(()=>{const b=document.getElementById('dc-state');"
                        "if(!b)return '';const t=b.innerText||'';"
                        "const i=t.indexOf('监视道');"
                        "return (i>=0)?t.slice(i, i+40):'';})()")
                    with urllib.request.urlopen(
                            f"http://127.0.0.1:{args.fake_cam_port}/__monitor",
                            timeout=6) as r:
                        r.read()
                    _post(base, "/api/v1/camera/probe", {})
                except Exception as e:                        # noqa: BLE001
                    never_line = f"ERR:{type(e).__name__}"
            never_ok = ("未推过" in never_line) if fake_cam is not None else True
            b33_ok = b33_ok and three_ok and never_ok
            record("B33", "PASS" if b33_ok else "FAIL",
                   f"相机窗口写着「{mon_txt.get('line', '').strip()[:60]}」；"
                   f"四种情况都答得出（没推过/推了/没订阅/读不到）；"
                   f"切到「上游从没推过」时显示「{never_line.strip()[:60]}」"
                   if b33_ok else
                   f"**这一屏答不出「画面为什么没出来」**：行={mon_txt} / 四种={mons} "
                   f"/ 切到 never 后={never_line!r}（要含「未推过」）")

            await cdp.eval("document.getElementById('dev-close').click()")
            await asyncio.sleep(0.3)

            two = await cdp.eval(
                "(()=>{const c=document.querySelector('#col-cam .view.on');"
                "const m=document.querySelector('#col-mouse .view.on');"
                "const cb=document.querySelector('#rail-cam button.on');"
                "const mb=document.querySelector('#rail-mouse button.on');"
                "const a=document.getElementById('col-cam').getBoundingClientRect();"
                "const b=document.getElementById('col-mouse').getBoundingClientRect();"
                "return {cam:c?c.dataset.tab:null, mouse:m?m.dataset.tab:null,"
                " camBtn:cb?cb.dataset.tab:null, mouseBtn:mb?mb.dataset.tab:null,"
                " twoVisible: !!(c&&m)&&c.classList.contains('on')&&m.classList.contains('on'),"
                " sideBySide: (a.width>200 && b.width>200 && a.right <= b.left + 2),"
                " w:Math.round(a.width)+'/'+Math.round(b.width)};})()")
            two_ok = (two.get("twoVisible") and two.get("sideBySide")
                      and two.get("camBtn") == two.get("cam")
                      and two.get("mouseBtn") == two.get("mouse"))
            record("B20", "PASS" if two_ok else "FAIL",
                   f"两栏同时在屏：左栏「{two.get('cam')}」({two.get('camBtn')}) + "
                   f"右栏「{two.get('mouse')}」({two.get('mouseBtn')})，"
                   f"并排宽度 {two.get('w')} px（同时可见、同时可操作）"
                   if two_ok else f"**两栏没有同时呈现**：{two}")

            # 汇总一下"预期内"的那批，方便判断是不是全是同一类（重复报同一条 = 噪声）
            if soft_bad:
                from collections import Counter
                cnt = Counter()
                for b in soft_bad:
                    cnt[str(b.get("url") or "").split("/api/v1/")[-1]] += 1
                print(f"         预期内错误分布：{dict(cnt)}")

    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
    if launcher_paused:
        # 复原：暂停只是"这一轮先别代启"，不能带出这一轮
        try:
            _post(base, "/api/v1/camera/service/pause", {"on": False})
        except Exception:                                     # noqa: BLE001
            pass
    if stopped_by_us:
        # 为了跑降级路，我们停掉了**本层代启的**那个上游服务。收尾要把它起回来 ——
        # 测试不该让"用户原本跑着的服务"少一个（虽然它是我们起的，但用户正用它）。
        try:
            _post(base, "/api/v1/camera/service/start", {})
        except Exception:                                     # noqa: BLE001
            pass
    if fake_cam is not None:
        fake_cam.shutdown()
        fake_cam.server_close()
    _rmtree_quiet(profile)
    if sim_proc is not None:
        sim_proc.terminate()
        try:
            sim_proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            sim_proc.kill()
        # 顺手把服务端的链路断开，免得留一个连着的模拟器
        try:
            urllib.request.urlopen(urllib.request.Request(
                base + "/api/v1/mouse/disconnect", method="POST", data=b"{}",
                headers={"Content-Type": "application/json"}), timeout=15)
        except Exception:                                     # noqa: BLE001
            pass

    head("汇总")
    n_pass = sum(1 for _, v, _ in RESULTS if v == "PASS")
    n_fail = sum(1 for _, v, _ in RESULTS if v == "FAIL")
    print(f"  PASS {n_pass}   FAIL {n_fail}")
    if n_fail:
        print("\n  没过的：")
        for cid, v, note in RESULTS:
            if v == "FAIL":
                print(f"    · {cid}: {note}")
    if shots:
        print(f"\n  截图：{shots.resolve()}")
    return 1 if n_fail else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--sim", action="store_true",
                    help="先起上游鼠标模拟器并让服务端连上（页面才有真数据）")
    ap.add_argument("--sim-port", type=int, default=5555)
    ap.add_argument("--fake-cam", action=argparse.BooleanOptionalAction, default=True,
                    help="8765 没有真相机服务时，起一个假上游 —— 好让「配对二维码」"
                         "那条判据真跑一遍（--no-fake-cam 关掉）")
    ap.add_argument("--fake-cam-port", type=int, default=8765)
    ap.add_argument("--wait", type=float, default=4.0, help="首页打开后等多久（等 WS+首轮 health）")
    ap.add_argument("--per-tab", type=float, default=1.5, help="每页停多久")
    ap.add_argument("--shots", default="")
    ap.add_argument("--width", type=int, default=1680)
    ap.add_argument("--height", type=int, default=1000)
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:                                         # noqa: BLE001
        pass
    print("=" * 74)
    print("Control 前端无头实测（CDP 直连，不依赖 Playwright）")
    print("=" * 74)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
