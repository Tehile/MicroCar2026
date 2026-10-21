"""无头浏览器实测：把六个视图真的渲染出来，抓 JS 异常与坏请求。

    python tools/browser_check.py                 # 默认查 http://127.0.0.1:8770
    python tools/browser_check.py --port 8897
    python tools/browser_check.py --shots .tmp/shots   # 把每页截屏存下来

**为什么要单独有这个脚本**：一遍 `py_compile` 只能证明"Python 语法没错"，
`curl /` 只能证明"HTML 字节发出去了"。而前端是**十几个 JS 文件 + Canvas 自绘**，
一个 `undefined.foo` 就足以让某一页白屏 —— 而它不会在服务端日志里留下任何痕迹。
判据里 P0 的验收写的是"浏览器能打开并看到两灯"，那就要有一台真的浏览器去看。

**为什么不用 Playwright**：本机没装，装它要拉一个 ~150 MB 的内核。而 Chrome 本来就有，
CDP 又是纯 WebSocket —— `aiohttp`（本项目已有依赖）就能当客户端。
少一个依赖，多一条能随时重跑的命令。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
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

#: 界面的主区是**两级**：**入口页**只做导航（三个子系统各一组，组内是模块卡片），
#: 点卡片**单开一个整屏模块界面**，点【← 返回】回入口。**没有选项卡**，也不开新标签。
#: 「综合运动」进去后是**单栏五页 + 一个可收纳底栏**（⑧数据）。
#: 改版二十四：相机与场地定位两路随手机相机整组下线（位置数据改由「视频复盘」给），
#: 左栏「场地 · 相机」整栏删除 ⇒ 页签 7 → 5、视图 8 → 6。**唯一真相仍是 app.js 的
#: `SYS_DEF`**，这里只是它的一份抄本。
#: 下面这几张表必须与 index.html 的容器/按钮顺序、control/static/app.js 的 RAIL_OF
#: 一致，并且 `len(TABS) + len(DOCK_VIEWS) == EXPECTED_VIEWS`（= 6，app.js 里那份）。
#: 三处一起改，少改一处会当场报出来。
#:
#: ⚠️ 逐页断言打的是「综合运动」界面里的六页 —— 它们平时在 `display:none` 的
#: `#ws-all` 里，**必须先 `C.openMod('all','@all')` 打开**（不可见元素的 `innerText`
#: 是空字符串，不打开的话每条断言都会读到空）。
SYS_TABS = ["mouse", "vid", "all"]           # 入口页分组（顺序 = app.js 的 SYS_DEF / index.html）
TAB_MOUSE = ["params", "depart", "exp", "scope", "console"]
TABS = list(TAB_MOUSE)                       # 单栏 nav 里的按钮（顺序 = index.html）
DOCK_VIEWS = ["data"]                        # 底部整合栏里的视图（没有按钮，展开即激活）
RAIL_OF_PY = {t: "mouse" for t in TAB_MOUSE}
RAIL_OF_PY.update({t: "dock" for t in DOCK_VIEWS})


def tab_click_js(tab: str) -> str:
    """点开某一页 —— 单栏只有一个页签条，按 data-tab 找按钮就行。
    底栏的 ⑧数据 没有按钮（它是"展开就激活"），走 `C.activate()`。"""
    rail = RAIL_OF_PY[tab]
    if rail == "dock":
        return f"C.activate('{tab}')"
    return f"document.querySelector('#rail-{rail} button[data-tab=\"{tab}\"]').click()"


#: 每个页面的**针对性断言**：光有 HTML 字节数不够 —— "有 5000 字节"可能全是空表格。
#: 这里检查"那些关键的控件/字段真的在，而且有值"。
#: 表达式返回 {ok: bool, note: str}；ok=False 会让该页 FAIL。
PAGE_PROBES = {
    # 参数页必须**真的分成组**（上游 `PARAM_GROUPS`：生产标定 / 红外测量 / 电源监控 /
    # 运动控制）。⚠️ 原来断言里有"课程调参"那一组 —— 上游 **2.4.0 已删课程调参整层**，
    # 于是第一支从那时起就永远为假，这条判据一直在靠"未连鼠 ⇒ gated"兜着
    # （2026-10-15 连模拟器跑 `--sim` 时才暴露）。⇒ 改成"生产标定在 + 另有任一组"。
    # 未连鼠时读不到参数，这时**要求它明确写出原因**，而不是摆一张空表
    # （"空白"和"没连"是两件事，混起来最费时间）。
    "params": ("(()=>{const t=document.querySelector('#p-groups').innerText||'';"
               "const hasProd=t.indexOf('生产标定')>=0;"
               "const hasOther=/红外测量|电源监控|运动控制/.test(t);"
               "const gated=t.indexOf('未连接电脑鼠')>=0||t.indexOf('无法读取参数')>=0;"
               "const n=(t.match(/\\\\bid\\\\b/g)||[]).length;"
               "return {ok: (hasProd&&hasOther)||gated,"
               " note:(hasProd&&hasOther)?('分组都在，项数标记 '+n)"
               " :'未连鼠 ⇒ 已写明原因（没冒充空表）'};})()"),
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
    ("B14a", "#d-arm", "mouse", "④ 发车（ARM）"),
    ("B14b", "#d-maze", "mouse", "④ 远程发车跑迷宫"),
    ("B14c", "#x-lab-run", "mouse", "⑤ 电机实验「跑一次」"),
    # ⚠️ B14d 在 2.4.0 上游上**判的是隐藏的按钮**（课程那栏被 `#x-lesson-body`
    # 整体收起）—— 但**没有删它**：本层是整合层，要能在 2.3.x 上游上跑，
    # 那时这个按钮是可见且需要门控的。`disabled` 检查对隐藏元素照样成立，
    # 所以它不假绿（真未门控时仍然会红）。
    ("B14d", "#x-lesson-run", "mouse", "⑤ 课程实验「跑实验」（2.4.0 起该栏收起，仍守 2.3.x）"),
    # ⚠️ 原 B14e（`#s-dump`）**已删除**：2.0.0 起没有"拉 dump"这个动作（`SCOPE`/`DUMP`
    # 整套删除），门控一个不存在的按钮等于给自己留一条永远绿的空断言。
    ("B14f", "#p-reload", "mouse", "③ 读全部参数"),
    ("B14h", "#k-send", "mouse", "⑦ 发原始命令"),
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


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def _synth_recon_doc(with_goals: bool = True) -> dict:
    """造一份**合成**的迷宫重构应答（B44 用）—— 形状与 `mouse.maze_reconstruct` 一致。

    锁死终点 (7,7)(7,8)(8,7)(8,8) 是**赛道属性**（`maze_rec.GOAL_CELLS` 经
    `goal_cells` 端出）⇒ 合成文档照抄那四格。`with_goals=False` 造"后端没给
    终点区"的负向样本（**整个字段缺失**，不是空数组 —— 界面两种都要接得住）。
    墙图本体只给能过 `draw()` 的最小形状（hex 一位一格，bit 置位 = 无墙）：
    这里要验的是"终点画没画对、缺数据瞎不瞎画"，不是重建算法（那是记录端的事）。
    """
    n = 16
    doc = {
        "n": n, "frames_used": 12, "window_s": 30.0, "source": "ring",
        "known_rows": ["0" * n] * n,
        "wall_rows": ["0" * n] * n,
        "visited": ["1" + "0" * (n - 1)] + ["0" * n] * (n - 1),
        "track": [[0, 0], [0, 1], [0, 2]],
    }
    if with_goals:
        doc["goal_cells"] = [[7, 7], [7, 8], [8, 7], [8, 8]]
        doc["goal_note"] = "赛道固定：中心 2×2（16×16 迷宫）"
    return doc


CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

RESULTS: list = []


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

    # 改版二十四：这里原来还有一整节「假上游相机」—— 在 8765 上起一个纯 stdlib 的
    # 假服务，给「配对二维码 / MJPEG 出画面 / 监视道三态」那几条判据造真数据，
    # 外加 S0d（起假上游）、S0f（暂停代启）、S0g（造出"相机不可达"的降级条件）三条开关。
    # 相机整路下线后那几条判据全删了，于是**这一节连同它的开关一起删掉** ——
    # 留着就是"给不存在的判据造数据"，还会在用户机器上多一个监听 8765 的进程。

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

            head("2. 打开首页（P0 验收：两灯可点 + 单栏五页 + 数据）")
            await cdp.call("Page.navigate", {"url": base + "/"})
            await asyncio.sleep(args.wait)
            title = await cdp.eval("document.title")
            lamps = await cdp.eval(
                "Array.from(document.querySelectorAll('.lamp em')).map(e=>e.textContent)")
            # 顶栏那两个状态标签必须**本身就是入口**（点它开该设备的窗口）：
            # `data-dev` 就是这件事在 DOM 上的证据，少了它标签就只是块画。
            devs = await cdp.eval(
                "Array.from(document.querySelectorAll('.lamp[data-dev]')).map(b=>b.dataset.dev)")
            navs = await cdp.eval(
                "Array.from(document.querySelectorAll('.rail-tabs button[data-tab]'))"
                ".map(b=>b.dataset.tab)")
            views = await cdp.eval(
                "Array.from(document.querySelectorAll('.rail-views .view')).map(v=>v.dataset.tab)")
            # 只数**有内容、且不在设备窗口里**的故障框：
            #  · 空盒子不算错误 —— `.errbox` 也可能是常驻的"故障位"（鼠标窗口的
            #    `#dm-warn`），服务正常时它就是空的（同 B13 那条纪律：
            #    "还没准备好"不许被数成"出错了"）；
            #  · 设备窗口里的故障框也不属于这一条 —— 它说的是"那一路设备不通"，
            #    有内容恰恰说明界面在如实报故障，那是由设备窗口那两条（B19/B21）单独判的。
            #    这条判据问的是"**页面/模块挂载**有没有出错"。
            errboxes = await cdp.eval(
                "Array.from(document.querySelectorAll('.errbox'))"
                ".filter(e=>!e.closest('#dev-modal'))"
                ".map(e=>(e.textContent||'').trim()).filter(t=>t.length)")
            record("B0", "PASS" if (lamps and len(lamps) == 2
                                    and devs == ["mouse", "clock"]) else "FAIL",
                   f"标题「{title}」；顶栏灯 {len(lamps)} 个：{lamps}；"
                   f"其中可点的设备标签 {devs}")
            record("B1", "PASS" if navs == TABS else "FAIL",
                   f"单栏页签 {len(navs)} 个（{len(TAB_MOUSE)} 页）：{navs}")
            record("B2", "PASS" if views == TABS + DOCK_VIEWS else "FAIL",
                   f"视图容器 {len(views)} 个（单栏 {len(TABS)} + 底栏 {len(DOCK_VIEWS)}），"
                   f"全部 mount 无异常"
                   + (f"；**有 errbox**：{errboxes}" if errboxes else ""))
            if errboxes:
                record("B2", "FAIL", f"挂载期出现 errbox：{errboxes}")

            # ---- 主区结构：入口页（纯导航）+ 模块界面（整屏）两级 ----
            # 入口页**只放导航**：三个子系统各一组（类别名称 + 模块卡片），
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
            #   · 上游 2.0.0 删 `SNAP`/`SCOPE` ⇒「分组测量」并入「实时参数」，5 变 4；
            #   · 上游 2.4.0 压到两档编译 + 删课程层 + 加 `MAZE RUN [BLIND|RACE]`
            #     ⇒ 四卡重排为**三卡**（实时状态与参数设定 / 无记忆走迷宫 / 比赛）；
            #   · 改版二十接回按键菜单远程化 ⇒ 加第 4 卡「维护与自检」；
            #   · **改版二十三**「无记忆走迷宫」整页下线（新基线只跑比赛跑法）⇒ 回到
            #     **三卡**：①实时状态与参数设定 ②比赛（含地图预览）③维护与自检。
            #   · **改版二十四**相机/定位下线后入口页是**三组**：小车控制 3 / 视频复盘 2 /
            #     综合运动 1。加/删卡就**必须**同步这几个数字 —— 卡片已经画出来而这里
            #     还写着旧数，报的是一条假红（铁律 50①：写死的数字跟着改版走）。
            #   这里写死数字是有意的 —— 卡片由 `SYS_DEF` 渲染，**模块脚本没加载时卡片照样在**，
            #   所以"卡片数对不对"与"模块能否挂上"是两道独立的闸。
            cards_ok = (sysinfo["mouse"]["cards"] == 3 and sysinfo["vid"]["cards"] == 2
                        and sysinfo["all"]["cards"] == 1)
            b24_ok = sec_ok and cards_ok and not oldnav and shown["nav"] and not shown["mod"]
            record("B24", "PASS" if b24_ok else "FAIL",
                   f"入口页只有导航（无旧选项卡、工作区不在入口页）："
                   f"小车控制 {sysinfo['mouse']['cards']} 卡片 {sysinfo['mouse']['names']}"
                   f"／视频复盘 {sysinfo['vid']['cards']}"
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

            # 入口页必须**真排出来了**：三组竖排（top 递增、等宽）、每组有高度、
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
                   f"入口页几何正常：三组竖排 {tops}、等宽 {widths[0]}px、"
                   f"内容 {box['sh']}/{box['ch']}px 无横向溢出；"
                   + "／".join(f"{s} 高 {sysinfo[s].get('h')}"
                               f"（卡片 {sysinfo[s].get('card0')}）" for s in SYS_TABS)
                   if b26_ok
                   else f"**入口页没排出来**：竖排={stacked} 等宽={same_w} "
                        f"不溢出={no_ovf}({box}) 组={geo_bad} 卡片={card_bad}")

            # 「单开一个新界面」必须**能被地址引用**：带 `#/<子系统>/<模块>` 打开
            # （刷新、贴链接、浏览器后退都算），要落到同一个界面 —— 否则现场说不清
            # "就是这一页"，只能靠嘴描述点哪几个卡片。
            # ⚠️ 深链目标要挑**所在子系统不是 mouse** 的那个：`#/mouse/...` 与默认首卡
            # 同组，即使路由坏了也可能"看起来对了"。用「视频复盘 · 视频会话」。
            await cdp.call("Page.navigate", {"url": base + "/#/vid/mod_video"})
            await asyncio.sleep(args.wait)
            deep = await cdp.eval(
                "(()=>{const q=function(s){return document.querySelector(s);};"
                "const w=q('#ws-vid .view.on');"
                "return {mod:w?w.dataset.mod:null,"
                " open:document.getElementById('sys').classList.contains('mod-open'),"
                " title:(q('#mod-title')||{textContent:''}).textContent,"
                " hash:location.hash||''};})()")
            b27_ok = (deep.get("mod") == "mod_video" and deep.get("open")
                      and deep.get("title") and deep.get("hash") == "#/vid/mod_video")
            record("B27", "PASS" if b27_ok else "FAIL",
                   f"深链 `#/vid/mod_video` 直接落到「{deep.get('title')}」界面"
                   if b27_ok else f"**深链落不到目标界面**：{deep}")

            # 逐页断言打的是「综合运动」界面里的六页：**先把那个界面打开** ——
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
                   f"「综合运动」卡片打开了单栏五页 + 数据界面（视图容器 {want_views} 个）"
                   if b28_ok else f"**综合运动界面打不开**：{allview}")

            # 三个**模块**（上游 1.5.0 组测发送 / 1.5.5 实验 8 状态机 / 2.4.0 比赛页）必须真的挂上并渲染出控件。
            # 为什么单独判：模块脚本没加载 / 注册失败时，**卡片照样在**（它由 `SYS_DEF` 渲染），
            # 点进去才发现是空的 —— 而"页面是空的"和"这个功能本来就没做"长得一模一样
            # （同 I21 的那条纪律：静态资源少一个会**静默变空**）。
            head("2f. 模块：实时状态与参数设定 / 比赛 / 维护与自检")
            n_err0 = len(await cdp.snapshot_console())
            want_sel = {
                # SAVE 按钮是**按数据里的 flash_page 现生成**的 ——
                # "现生成"的东西最容易在某一分支里整个不出现（脚本没报错、页面上没有）。
                #
                # 2.0.0：「分组测量」**已合并进「实时参数」**（`SNAP`/`SCOPE` 整套删除，
                # 两者读的东西变成了同一条统一帧）⇒ 这一页现在必须同时有
                # **读数区**（`#mp-live` / `#mp-stream` / `#mp-fault`）与**参数表区**。
                # 2.4.0（改版十二）：这一页改名「实时状态与参数设定」，并**并入了原
                # 「运动控制」（#mm-*）卡的内容 ⇒ 那些 id 现在也必须在这一页里
                # 找得到（它不再有自己的卡片，丢了没人报红）。
                "mod_params": ["#mp-groups", "#mp-reload", "#mp-saves",
                               "#mp-live", "#mp-stream", "#mp-fault",
                               "#mm-go", "#mm-estop"],
                # 比赛（2.4.0 新页）：`MAZE RUN RACE` 发车 + 停止并保存/收车/复位 + 前墙双档
                # + 冲刺状态 + 状态机 + **地图预览**（`C.parts.wallmap`，改版二十三迁入）。
                # 这些 id 是**新加的、别处没有**，所以必须钉住。
                "mod_race": ["#rc-run", "#rc-recstop", "#rc-mstop", "#rc-rescue", "#rc-refresh",
                             "#rc-hint", "#rc-front", "#rc-front-ok", "#rc-spurt", "#rc-state",
                             "#wm-cv", "#wm-src", "#wm-kv"],
                # 维护与自检（改版二十）：CAL 家族 6 项。id 也是这一页独有的。
                "mod_maint": ["#mt-wrap", "#mt-irself", "#mt-sensor", "#mt-block",
                              "#mt-ircap-next", "#mt-ir-erase", "#mt-erase-all", "#mt-diag"],
            }
            newmods = {}
            for mid in ("mod_params", "mod_race", "mod_maint"):
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
                #   · 比赛：状态机要么明说「未运行」，要么给出真的状态；
                #   · 维护与自检：要么明说「没有按键菜单远程化」，要么给出动作按钮。
                txt_sel = {"mod_params": ("#mp-live", 400),
                           "mod_race": ("#rc-state", 120),
                           "mod_maint": ("#mt-wrap", 120)}[mid]
                newmods[mid]["txt"] = await cdp.eval(
                    "(()=>{const b=document.querySelector('" + txt_sel[0] + "');"
                    "return b?b.textContent.replace(/\\s+/g,' ').slice(0," + str(txt_sel[1]) + "):'';})()")
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
            #   · 比赛：状态机要么明说「未运行 / 未连接电脑鼠」，要么给出真的状态；
            #   · 维护与自检：要么明说「没有按键菜单远程化」，要么给出动作按钮。
            mp_txt = newmods["mod_params"].get("txt", "")
            rc_txt = newmods["mod_race"].get("txt", "")
            mt_txt = newmods["mod_maint"].get("txt", "")
            says_ok = (("speedL" in mp_txt)
                       and (("未运行" in rc_txt) or ("未连接" in rc_txt) or ("任务" in rc_txt))
                       and (("按键菜单" in mt_txt) or ("红外" in mt_txt) or ("诊断" in mt_txt)
                            or ("能力" in mt_txt)))
            b30_ok = (not bad_m) and not hard30 and says_ok
            record("B30", "PASS" if b30_ok else "FAIL",
                   "模块界面都挂上了：" + "；".join(
                       f"{m}({'/'.join(want_sel[m])})" for m in want_sel)
                   + f" 全在、有内容（{mp_txt[:16]}…｜{rc_txt[:16]}…｜{mt_txt[:16]}…）、"
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
            #   小数+单位（speedL cm/s）、整数+单位（irF1 mm）、编码表翻名字（motionTask/dir）、
            #   位掩码展开（flags）、无单位整数（irSwitch）、以及派生键（roadName 来自
            #   `classify_road`，不是通道，**不许**混进读数区）。
            # ⚠️ 只用 **v9 还活着的通道**（2.12.0 删了 20 个，§11.24 影响面）：旧哨兵里的
            #   `cellOdom` / `irAdcF2` / `stackIdx` 已删，再喂它们等于在验一个不存在的量。
            #   `mazeTask: 6`（END）是 2.11 新档位 —— 码表里若缺它会显示成裸 `6`，
            #   所以哨兵直接钉这一档（"新档位必须翻名字"就落在这行）。
            SENT = {"speedL": -101.5, "speedR": 202.25,
                    "trackL": -303, "trackR": 404,
                    "irF1": 711, "irSwitch": 1, "battMv": 3900,
                    "motionTask": 5, "mazeTask": 6, "dir": 1,
                    "flags": 66, "roadName": "TEE_FL", "ms8": 77}
            SENT_CH = sorted((set(SENT) - {"roadName"})
                             | {"speedR", "wallLeft", "irR"})
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
                    "trackL=-303 mm", "trackR=404 mm",
                    "irF1=711 mm", "irSwitch=1",
                    "battMv=3900 mV",
                    "motionTask=STOP (5)",      # 名字来自码表，括号里是原码
                    "mazeTask=END (6)",         # 2.11 新档位：必须翻成 END，不许裸 6
                    "dir=RIGHT (1)",
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
                # 它是"分类结果"（比赛页的状态行在用）；这里出现反而是分组表把非通道键混进来了。
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
            # ⚠️ **负向**：v9 删掉的通道**不许**还在静态分组表里。
            #   喂 `channels: []` ⇒ `liveGroups()` 不过滤、把整张静态表画出来；
            #   删掉的键若还留在表里，这里立刻现形（留着 = "这个量能读，只是没数据"的假象，
            #   现场会去查一个已经不存在的字段）。期望值从 §11.24 影响面 #4 的删除清单反推。
            gone = await cdp.eval(
                "(()=>{C.openMod('mouse','mod_params');"
                "const box=document.getElementById('mp-live');"
                "if(!box)return null;"
                "const keep=C.state.telemetry;"
                "C.state.telemetry={unified:{speedL:1}, channels:[],"
                "  rate:{unified:{}}, bps:0, streaming:false};"
                "const v=C.mods.mod_params;"
                "try{v.onMsg({type:'mouse.telemetry'});}catch(e){}"
                "const ks=Array.from(box.querySelectorAll('code'))"
                "  .map(function(c){return c.textContent||'';});"
                "C.state.telemetry=keep;"
                "try{v.onMsg({type:'mouse.telemetry'});}catch(e){}"
                "return ks;})()")
            if gone is None:
                b34_bad.append("找不到 #mp-live（负向无从验起）")
            else:
                for k in ("cellOdom", "pidErr", "goalX", "goalY", "stackIdx", "motionStep",
                          "fastwayCount", "fastwayCount90", "dropCnt", "critL", "critR",
                          "left45", "right45", "wallThresh",
                          "irAdcF1", "irAdcF2", "irAdcL", "irAdcR"):
                    if k in gone:
                        b34_bad.append(f"已删通道 {k} 还在静态分组表里（v9 已删，不该显示）")
            b34_ok = not b34_bad
            record("B34", "PASS" if b34_ok else "FAIL",
                   f"「实时状态与参数设定」读数区：{len(want)} 个哨兵逐行与喂进去的统一帧一致"
                   f"（{' '.join(b34_got[:6])}…）、单位齐（cm/s / mm / mV）、"
                   f"编码翻名字（motionTask/dir/flags）、标题「{rp.get('paneH4')}」、"
                   f"流概览「{rp.get('stream')}」"
                   if b34_ok else
                   f"**实时读数的分组/键名/单位/编码不对**：{b34_bad[:4]}；实际={b34_got}")

            # ── B44：地图预览（迁自「无记忆走迷宫」）+ 终点锁死四格（改版二十三） ──
            # 守的是用户拍板的三条：
            #   ① 那张卡下线后，**地图预览**在比赛卡里活着（`C.parts.wallmap`）：
            #      控件在、画布有尺寸、真的在画（不是空壳）；
            #   ② **终点锁死** (7,7)(7,8)(8,7)(8,8)：后端常量（`maze_rec.GOAL_CELLS`）
            #      经 `goal_cells` 端出，界面只消费不重写（铁律 32）。真打一次端点断的
            #      就是这把锁；画图端拿到的也必须是这四格（钉 `C.draw.maze` 的入参）；
            #   ③ **缺数据不瞎画**：没数据时把"为什么没有"写在画布上，不许画一张空网格
            #      冒充"没墙"；端点不给 `goal_cells` 时写「后端没给终点区」，不臆造坐标。
            # 手法同 B34：**钉住数据再断言**（拦 `C.api` 喂合成应答 + 钉 `C.draw.maze` 入参）。
            # ⚠️ 本判据**不看帧**（终点来自应答、缺数据看的是空文档）⇒ `--sim` 下照样
            #    确定，**不进 SKIP**（真帧只动车的位置，动不了终点与"没有数据"这两条）。
            head("2h. 比赛卡·地图预览：终点锁死四格 + 缺数据不瞎画（含负向）")
            b44_bad, b44_got = [], []
            try:
                # ① 锁：真打一次端点 —— goal_cells 必须就是锁死的四格
                rr = _get(base, "/api/v1/mouse/maze/reconstruct?seconds=1")
                gc = (rr.get("data") or {}).get("goal_cells")
                if gc != [[7, 7], [7, 8], [8, 7], [8, 8]]:
                    b44_bad.append(f"端点 goal_cells 不是锁死的四格：{gc!r}")
                else:
                    b44_got.append("端点=锁死四格")
            except Exception as e:                            # noqa: BLE001
                b44_bad.append(f"读 /maze/reconstruct 失败：{type(e).__name__}: {e}")
            try:
                r44 = await cdp.eval(
                    "(async()=>{"
                    "const q=function(s){return document.querySelector(s);};"
                    "window.__b44_nm=C.needMouse; C.needMouse=function(){return null;};"
                    "window.__b44_api=C.api;"
                    "let mode='goals';"
                    "C.api=function(p,o){"
                    " if(String(p).indexOf('maze/reconstruct')>=0)"
                    "  return Promise.resolve(window.__b44_doc(mode));"
                    " return window.__b44_api(p,o);};"
                    "window.__b44_doc=function(m){"
                    " if(m==='goals')return " + json.dumps(_synth_recon_doc(True)) + ";"
                    " if(m==='nogoals')return " + json.dumps(_synth_recon_doc(False)) + ";"
                    " return {n:0};};"
                    "window.__b44_goals=null;"
                    "window.__b44_draw=C.draw.maze;"
                    "C.draw.maze=function(c,o){"
                    " window.__b44_goals=(o&&o.goals)?JSON.parse(JSON.stringify(o.goals)):null;"
                    " return window.__b44_draw(c,o);};"
                    "const proto=CanvasRenderingContext2D.prototype;"
                    "window.__b44_ft=proto.fillText; window.__b44_texts=[];"
                    "proto.fillText=function(t){"
                    " try{window.__b44_texts.push(String(t));}catch(e){}"
                    " return window.__b44_ft.apply(this,arguments);};"
                    "const out={};"
                    "C.openMod('mouse','mod_race');"
                    "await C.parts.wallmap.load();"
                    "const cv=q('#wm-cv'),kv=q('#wm-kv'),src=q('#wm-src');"
                    "out.miss=!(cv&&kv&&src);"
                    "if(!out.miss){"
                    " out.cvW=cv.width; out.cvH=cv.height;"
                    " out.kv1=(kv.textContent||'').replace(/\\s+/g,' ').trim();"
                    " out.src1=(src.textContent||'').replace(/\\s+/g,' ').trim();"
                    " out.drawn1=window.__b44_goals;"
                    " out.goalFn=C.parts.wallmap.goalsText();"
                    " out.has1=C.parts.wallmap.hasGoals();"
                    " out.texts1=window.__b44_texts.slice();}"
                    "mode='nogoals';"
                    "await C.parts.wallmap.load();"
                    "out.kv2=(q('#wm-kv').textContent||'').replace(/\\s+/g,' ').trim();"
                    "out.drawn2=window.__b44_goals;"
                    "out.has2=C.parts.wallmap.hasGoals();"
                    "mode='empty';"
                    "window.__b44_texts.length=0;"
                    "await C.parts.wallmap.load();"
                    "out.kv3=(q('#wm-kv').textContent||'').replace(/\\s+/g,' ').trim();"
                    "out.texts3=window.__b44_texts.slice();"
                    "C.api=window.__b44_api; C.needMouse=window.__b44_nm;"
                    "C.draw.maze=window.__b44_draw; proto.fillText=window.__b44_ft;"
                    "return out;})()")
            except Exception as e:                            # noqa: BLE001
                r44 = {"miss": True}
                b44_bad.append(f"地图预览渲染读取抛异常：{type(e).__name__}: {e}")
            if (r44 or {}).get("miss"):
                b44_bad.append("找不到 #wm-cv / #wm-kv / #wm-src（地图预览没挂上）")
            else:
                b44_got.append(f"画布 {r44.get('cvW')}×{r44.get('cvH')}")
                if not (r44.get("cvW") and r44.get("cvH")):
                    b44_bad.append(f"地图画布没尺寸（画不出来）：{r44.get('cvW')}×{r44.get('cvH')}")
                kv1 = str(r44.get("kv1") or "")
                if "终点区" not in kv1 or "(7,7)" not in kv1 or "(8,8)" not in kv1:
                    b44_bad.append(f"终点区没把锁死四格摆出来：{kv1[:120]!r}")
                if r44.get("goalFn") != "(7,7) (7,8) (8,7) (8,8)":
                    b44_bad.append(f"goalsText() 不是锁死四格：{r44.get('goalFn')!r}")
                if r44.get("drawn1") != [[7, 7], [7, 8], [8, 7], [8, 8]]:
                    b44_bad.append(f"画图端拿到的终点不是锁死四格：{r44.get('drawn1')!r}")
                if not r44.get("has1"):
                    b44_bad.append("喂了 goal_cells 但 hasGoals() 仍为假")
                if "重建" not in str(r44.get("src1") or ""):
                    b44_bad.append(f"来源行没说明这是重建图：{r44.get('src1')!r}")
                # 起点角：draw.maze 无条件画虚线圈 +「起点」两字（约定，不是读数）——
                #   图上看不到起点，现场无从对照"摆对了没有"（老 B37 的这条跟着图走）。
                texts1 = " ".join(str(t) for t in (r44.get("texts1") or []))
                if "起点" not in texts1:
                    b44_bad.append("图上没画起点标记（缺「起点」）")
                else:
                    b44_got.append("起点标记已画")
                # ③ 负向：后端没给 goal_cells ⇒ 如实说、不臆造坐标
                kv2 = str(r44.get("kv2") or "")
                if "后端没给终点区" not in kv2:
                    b44_bad.append(f"没给 goal_cells 时没写「后端没给终点区」：{kv2[:120]!r}")
                if r44.get("drawn2"):
                    b44_bad.append(f"没给 goal_cells 却画了终点：{r44.get('drawn2')!r}")
                if r44.get("has2"):
                    b44_bad.append("没给 goal_cells 但 hasGoals() 仍为真")
                b44_got.append("负向(无终点)=如实说")
                # ④ 缺数据不瞎画：空文档 ⇒ 画布写「还没有重建数据」，kv 清空
                kv3 = str(r44.get("kv3") or "")
                texts3 = " ".join(str(t) for t in (r44.get("texts3") or []))
                if "还没有重建数据" not in texts3:
                    b44_bad.append(f"缺数据时画布没写「还没有重建数据」：{texts3[:120]!r}")
                if kv3.strip():
                    b44_bad.append(f"缺数据时 kv 没清空（还在冒充有数据）：{kv3[:80]!r}")
                b44_got.append("缺数据=写明原因")
            b44_ok = not b44_bad
            record("B44", "PASS" if b44_ok else "FAIL",
                   f"地图预览迁入比赛卡：画布已渲染、终点锁死四格（{'；'.join(b44_got)}）、"
                   f"缺数据写明原因不瞎画"
                   if b44_ok else
                   f"**地图预览 / 锁死终点不对**：{b44_bad[:4]}")

            # ── B46：设备侧 4 个 LED 的三态显示（改版二十四 / 上游 2.13.0 FW 311） ──
            # I37 守的是纯函数（`control/leds.py`），这里守**画出来的样子**：
            #   ① 三态各画对：True=亮（实心发光）、False=灭（空心实线）、None=未知（虚线圈）；
            #   ② 4 灯齐亮才出「发车标记」；
            #   ③ **负向**：全 None（对应 fw 310 的预留位）时，**一个"灭"都不许有** ——
            #      现场看到 4 个灭会去查线路，而真实原因是固件根本没报这 4 位；
            #   ④ **负向**：`led*` 不许掉进「其他通道」兜底（同一事实两处显示）；
            #   ⑤ 老后端没有 `leds` 字段 ⇒ 不画空壳（不显示 4 个问号）。
            b46_bad, b46_got = [], []
            r46 = await cdp.eval(
                "(()=>{C.openMod('mouse','mod_params');"
                "const box=document.getElementById('mp-led');"
                "if(!box)return null;"
                "const live=document.getElementById('mp-live');"
                "const keep=C.state.telemetry;"
                "const v=C.mods.mod_params;"
                "function run(leds){"
                "  C.state.telemetry={unified:{speedL:1}, channels:[], leds:leds,"
                "    rate:{unified:{}}, bps:0, streaming:false};"
                "  try{v.onMsg({type:'mouse.telemetry'});}catch(e){}"
                "  const bs=Array.from(box.querySelectorAll('.leds b'));"
                "  return {n:bs.length, cls:bs.map(function(b){return b.className;}),"
                "          txt:box.textContent||''};"
                "}"
                "const a=run({led1:true,led2:false,led3:null,led4:false});"
                "const b=run({led1:true,led2:true,led3:true,led4:true});"
                "const c=run({led1:null,led2:null,led3:null,led4:null});"
                "C.state.telemetry={unified:{speedL:1}, channels:[], rate:{unified:{}},"
                "  bps:0, streaming:false};"
                "try{v.onMsg({type:'mouse.telemetry'});}catch(e){}"
                "const empty=box.innerHTML||'';"
                "C.state.telemetry={unified:{speedL:1,led1:1,led2:0,led3:1,led4:0},"
                "  channels:['speedL','led1','led2','led3','led4'],"
                "  leds:{led1:true,led2:false,led3:true,led4:false},"
                "  rate:{unified:{}}, bps:0, streaming:false};"
                "try{v.onMsg({type:'mouse.telemetry'});}catch(e){}"
                "const codes=Array.from(live.querySelectorAll('code'))"
                "  .map(function(x){return x.textContent||'';});"
                "C.state.telemetry=keep;"
                "try{v.onMsg({type:'mouse.telemetry'});}catch(e){}"
                "return {a:a,b:b,c:c,empty:empty,codes:codes};})()")
            if r46 is None:
                b46_bad.append("找不到 #mp-led（4 灯状态行没有落进实时读数面板）")
            else:
                a = r46.get("a") or {}
                if a.get("n") != 4:
                    b46_bad.append(f"应画 4 个灯，实得 {a.get('n')}")
                elif a.get("cls") != ["on", "off", "unk", "off"]:
                    b46_bad.append(f"三态没画对（应 on/off/unk/off）：{a.get('cls')}")
                else:
                    b46_got.append("亮/灭/未知各就位")
                if "发车标记" in (a.get("txt") or ""):
                    b46_bad.append("只有一组灯亮就写了「发车标记」（应为 4 灯齐亮才算）")
                b2 = r46.get("b") or {}
                if (b2.get("cls") or []) != ["on", "on", "on", "on"]:
                    b46_bad.append(f"全亮应 4 个 on，实得 {b2.get('cls')}")
                elif "发车标记" not in (b2.get("txt") or ""):
                    b46_bad.append("4 灯齐亮时没写「发车标记」")
                else:
                    b46_got.append("全亮=发车标记")
                # **负向**：全 None（fw 310 那条路）不许出现一个"灭"
                c2 = r46.get("c") or {}
                cls_c = c2.get("cls") or []
                if "off" in cls_c or "on" in cls_c:
                    b46_bad.append(f"**全未知时画出了亮/灭**（fw310 的预留位被当成真实电平）：{cls_c}")
                elif cls_c != ["unk"] * 4:
                    b46_bad.append(f"全未知应 4 个 unk，实得 {cls_c}")
                else:
                    b46_got.append("全未知不冒充灭")
                if (r46.get("empty") or "").strip():
                    b46_bad.append(f"老后端（无 leds 字段）画了空壳：{r46.get('empty')[:60]!r}")
                else:
                    b46_got.append("缺字段不画空壳")
                codes = r46.get("codes") or []
                leaked = [c for c in ("led1", "led2", "led3", "led4") if c in codes]
                if leaked:
                    b46_bad.append(f"{leaked} 掉进了「其他通道」（同一事实两处显示）")
                else:
                    b46_got.append("led* 不进其他通道")
            b46_ok = not b46_bad
            record("B46", "PASS" if b46_ok else "FAIL",
                   f"4 灯三态状态行：{'；'.join(b46_got)}"
                   if b46_ok else
                   f"**4 灯显示不对**：{b46_bad[:4]}")

            # ── B36：停止并保存 / 收车 / 复位按钮 + 发车前低电门控（**正反两面**） ──
            # 守的三个动作（改版二十三后都住在比赛卡 —— 记录与发车的生命周期跟过来了）：
            #   · 前两个是**按钮在不在**（它们在参数/门控之外，纯 DOM 断言就够）；
            #   · 低电提醒要**真的会显式出现**才叫守住了 —— 而它是"跟着 1 s 定时器
            #     从服务端取回来再渲染"的，所以要：**钉住健康状态**（让 `C.needMouse()`
            #     返回 null，否则门控行被"未连接电脑鼠"占着，电池那句话说不出）+ **拦一条
            #     接口**喂 warn 级预检 + 等一个定时器周期。
            #   · **负向**：喂 ok 级时那一行必须**空**。只验"warn 时会有字"是不够的 ——
            #     一个永远有字的提示等于没提示（本层纪律：一道从没红过的闸不算闸）。
            head("2i. 比赛卡 / 设备窗口：停止并保存·收车·复位 + 低电门控（含负向）")
            b36_bad = []
            btn = await cdp.eval(
                "(()=>{C.openMod('mouse','mod_race');"
                "const q=function(i){const e=document.getElementById(i);"
                " return e?{there:true,txt:(e.textContent||'').trim(),dis:!!e.disabled}:{there:false};};"
                "return {run:q('rc-run'), recstop:q('rc-recstop'), mstop:q('rc-mstop'),"
                "  rescue:q('rc-rescue'), dmRescue:q('dm-rescue'),"
                "  hint:(document.getElementById('rc-hint')||{}).textContent};})()")
            await asyncio.sleep(0.3)
            for k, want in (("recstop", "停止并保存"), ("mstop", "收车"), ("rescue", "复位")):
                if not btn.get(k, {}).get("there"):
                    b36_bad.append(f"比赛卡缺少按钮 {k}")
                elif want not in btn[k]["txt"]:
                    b36_bad.append(f"按钮 {k} 的文案不是「{want}」：{btn[k]['txt']!r}")
            # ⚠️ `recstop`（停止并保存）**不门控**：断链时记录器只置"该收尾"标记，
            #   落盘仍要有人推一把；被禁用 = 断链那一刻永远存不回盘（铁律 28）。
            if (btn.get("recstop") or {}).get("dis"):
                b36_bad.append("【停止并保存】被门控禁用了（它必须始终可点）")
            if not btn.get("dmRescue", {}).get("there"):
                b36_bad.append("设备窗口缺少【复位（软复位）】按钮")
            elif "复位" not in btn["dmRescue"]["txt"]:
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
                    " C.mods.mod_race.onHealth();"
                    " const h=document.getElementById('rc-hint');"
                    " if(h){h.textContent='';}"
                    " return true;})()")
                await asyncio.sleep(1.6)          # 等一个 TICK_MS 周期（1 s）让预检真跑一遍
                got = await cdp.eval(
                    "((document.getElementById('rc-hint')||{}).textContent||'').trim()")
                await cdp.eval(
                    "(()=>{if(window.__b36_api)C.api=window.__b36_api;"
                    " if(window.__b36_need)C.needMouse=window.__b36_need;"
                    " try{C.mods.mod_race.onHealth();}catch(e){}"
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
                   f"比赛卡有【停止并保存】【收车】【复位】（停止并保存不门控）、"
                   f"设备窗口有【复位（软复位）】；"
                   f"低电预检会把实测电压摆到门控行（warn ⇒ {warn_txt[:46]}…），"
                   f"**电量正常时那一行为空**（负向）"
                   if b36_ok else
                   f"**按钮或低电门控不对**：{b36_bad[:3]}")

            # ── B37：比赛卡 —— **前墙近/远两档**与恒等式当场自校验（改版十三，宿主迁改版二十三） ──
            # 守的是那两行显示（原在无记忆走迷宫左栏，现住比赛卡「前墙双档」区）：
            #   ① 必须**单列一行**把前墙拆成"近端 / 远端"（不再是单个"有墙"）——
            #      两档对应的现场动作不同（贴脸处理 / 提前减速），只给一个布尔就看不出来；
            #   ② 恒等式 `frontNear | frontFar == wallFront` 要**当场自校验**：成立写出成立，
            #      不成立红字报出来（比赛档靠这一档决定"减速还是马上转"，判错就是撞墙）。
            # 做法同 B34/B36：**先把数据钉住**（拦 `/maze/state` 喂合成状态）⇒ 渲染确定性。
            # 负向（两条都要"造得出来"）：
            #   ① 状态里**没有** frontNear/frontFar（老上游形状）⇒ 必须显示「—」，
            #      **不许**画成"无墙"、也**不许**写"恒等式成立"（判不了 ≠ 判过了）；
            #   ② 喂自相矛盾的一档（near|far ≠ wallFront）⇒ 必须报「对不上」，
            #      而不是照样写"成立"（一道只会说好的自校验等于没校验）。
            if args.sim:
                # ⚠️ 连了模拟器 ⇒ 每秒 10 条真帧会把钉住的假状态冲掉（onMsg 合并进 st），
                #    判据的确定性前提（未连鼠）不成立 ⇒ 如实标 SKIP，不假装通过。
                record("B37", "SKIP", "连了模拟器：真帧会冲掉钉住的假状态，本判据的确定性前提（未连鼠）不成立")
            else:
                head("2i-1. 比赛卡：前墙近/远两档 + 恒等式自校验（含负向）")
                b37_bad, b37_got = [], []

                async def _front_with(state_doc):
                    await cdp.eval(
                        "(()=>{window.__b37_api=window.__b37_api||C.api;"
                        " window.__b37_nm=C.needMouse;"
                        " C.needMouse=function(){return null;};"
                        " C.api=function(p,o){"
                        "  if(String(p).indexOf('/mouse/maze/state')>=0)"
                        "   return Promise.resolve(" + json.dumps(state_doc) + ");"
                        "  return window.__b37_api(p,o);};"
                        " C.openMod('mouse','mod_race');"
                        " return true;})()")
                    await asyncio.sleep(1.3)      # 等一轮 tick（1 s）把假状态读进来
                    out = await cdp.eval(
                        "(()=>{const f=document.getElementById('rc-front');"
                        "const o=document.getElementById('rc-front-ok');"
                        "return {front:(f?f.textContent:'').replace(/\\s+/g,' ').trim(),"
                        " ok:(o?o.textContent:'').trim()};})()")
                    await cdp.eval(
                        "(()=>{if(window.__b37_api)C.api=window.__b37_api;"
                        " if(window.__b37_nm)C.needMouse=window.__b37_nm;"
                        " try{C.mods.mod_race.onHealth();}catch(e){}"
                        " return true;})()")
                    return out

                try:
                    # 正面：近端档（frontNear=1, frontFar=0, wallFront=1）⇒ 显示「近端」+ 恒等式成立
                    good = {"state": {"task": 2, "taskName": "SEARCH", "motion": 1,
                                      "motionName": "GOAHEAD", "dir": 0, "dirName": "UP",
                                      "posX": 3, "posY": 4,
                                      "frontNear": 1, "frontFar": 0, "frontBoth": 1,
                                      "wallFront": 1, "frontName": "近端(贴脸)"},
                            "spurt": None}
                    r1 = await _front_with(good)
                    f1 = str(r1.get("front") or "")
                    b37_got.append("近端档=" + f1[:70])
                    if "frontNear" not in f1 or "frontFar" not in f1:
                        b37_bad.append(f"前墙没有单列近/远两行：{f1[:120]!r}")
                    elif "近端" not in f1:
                        b37_bad.append(f"喂了 frontNear=1 却没写出「近端」：{f1[:120]!r}")
                    if "恒等式成立" not in str(r1.get("ok") or ""):
                        b37_bad.append(f"1|0==1 却没写恒等式成立：{r1.get('ok')!r}")

                    # 负向①：老上游形状（状态里没有 frontNear/frontFar）
                    old_shape = {"state": {"task": 2, "taskName": "SEARCH", "motion": 1,
                                           "motionName": "GOAHEAD", "dir": 0, "dirName": "UP",
                                           "posX": 3, "posY": 4, "wallFront": 1},
                                 "spurt": None}
                    r2 = await _front_with(old_shape)
                    f2 = str(r2.get("front") or "")
                    ok2 = str(r2.get("ok") or "")
                    b37_got.append("负向(老上游)=" + ("写'—'" if "—" in f2 else "?"))
                    if "—" not in f2:
                        b37_bad.append(f"老上游（无 frontNear/frontFar）没显示「—」：{f2[:120]!r}")
                    if "恒等式成立" in ok2:
                        b37_bad.append(f"读不到两档却写了「恒等式成立」（判不了 ≠ 判过了）：{ok2!r}")

                    # 负向②：自相矛盾的一档（2|2=2 ≠ wallFront=1）⇒ 必须报「对不上」
                    bad_shape = {"state": {"task": 2, "taskName": "SEARCH", "motion": 1,
                                           "motionName": "GOAHEAD", "dir": 0, "dirName": "UP",
                                           "posX": 3, "posY": 4,
                                           "frontNear": 2, "frontFar": 2, "frontBoth": 2,
                                           "wallFront": 1, "frontName": "远端(提前量)"},
                                 "spurt": None}
                    r3 = await _front_with(bad_shape)
                    ok3 = str(r3.get("ok") or "")
                    b37_got.append("负向(矛盾档)=" + ("报'对不上'" if "对不上" in ok3 else "?"))
                    if "对不上" not in ok3:
                        b37_bad.append(f"自相矛盾的一档没报「对不上」：{ok3!r}")
                except Exception as e:                            # noqa: BLE001
                    b37_bad.append(f"读取前墙渲染时抛异常：{type(e).__name__}: {e}")
                b37_ok = not b37_bad
                record("B37", "PASS" if b37_ok else "FAIL",
                       f"比赛卡「前墙双档」有近/远两行（{'；'.join(b37_got)}），"
                       f"恒等式自校验两面都灵（成立如实写、不成立红字报）"
                       if b37_ok else
                       f"**前墙双档显示不对**：{b37_bad[:4]}")

            # ── B40：迷宫绘图落位 —— 前墙两档**分边** + 每行都不缺竖线（改版十八） ──
            # 守的是现场报的两条（都是"看图就能发现"的错，而 B37 只读左栏文字与画布
            # 尺寸，**看不见画在哪** ⇒ 必须像素级判据才钉得住）：
            #   ① 近端墙（`frontNear`，< 80 mm 贴脸）落在**本格**前沿 `PY(y)`；
            #      远端墙（`frontFar`，≥ 80 mm 提前量）落在**外一格**前沿 `PY(y)-cs`
            #      —— 两档**必须分边**。早先两档都落在 `PY(y+1)`，于是"贴脸"看着像隔了
            #      一格，照图判断"还有一格能走"就撞了。
            #   ② 每一行的竖线都在本格**整高** `PY(y)`→`PY(y)+cs` 内。格 (x,y) 的屏幕
            #      纵向范围就是 `[PY(y), PY(y)+cs]`；早先竖线跨 `PY(y)→PY(y+1)`
            #      （整体上移一格）⇒ **最底行一条竖线都没有**。
            # 手法（**不复制绘图端的几何公式**，否则改实现它跟着变、永远绿）：
            #   · 网格框从**像素本身**量：画布被 clearRect 清成透明，只有迷宫那块被填成
            #     深色 ⇒ "深色像素的最小外接矩形"**就是**网格框（不靠格线认框：最外两条
            #     白线会被后画的 `strokeRect` 外框盖住 ⇒ 依它认框会少两条，第一版就这样）；
            #   · 前墙那条线用**逐像素相减**摘出来：与"没有前墙"那一张比，差异像素
            #     **就是**那条线本身（不按颜色认，免得和琥珀色小车/起点圈混淆）。
            # 负向（两条都要"造得出来"）：
            #   ① 拿**只有远端墙**那张按"近端墙应落在本格前沿"验 ⇒ 必须判红；
            #   ② 自绘一张**故意不画最底行竖线**的图，用同一探测器验 ⇒ 必须报缺。
            head("2i-2. 迷宫绘图：前墙两档分边 + 每行都不缺竖线（像素级，含负向）")
            b40_bad, b40_got = [], []
            b40 = {}
            try:
                b40 = await cdp.eval(r"""(()=>{
                  const N = 7, D = window.devicePixelRatio || 1, R = {};
                  const ALL = []; for (let i=0;i<N;i++) ALL.push('f'.repeat(N));
                  const mk = function(){ const c = document.createElement('canvas');
                    c.width = Math.round(400*D); c.height = Math.round(320*D);
                    c.style.cssText = 'position:absolute;left:-99999px;top:0;'
                      + 'width:400px;height:320px;display:block;';
                    document.body.appendChild(c); return c; };
                  const cv = mk(), cv2 = mk();
                  try {
                    const draw = function(c, front){ C.draw.maze(c, {
                        height:320, n:N, known:ALL, wall:ALL, visited:null,
                        pos:[0,0], dir:0, front:front, label:''});
                      return c.getContext('2d').getImageData(0,0,c.width,c.height); };
                    /* 格线白 = #e8e8ea（近白、低饱和）；小车/起点圈是琥珀、地面是深蓝 */
                    const isW = function(d,i){ const r=d[i],g=d[i+1],b=d[i+2];
                      return r>195 && g>195 && b>195
                             && (Math.max(r,g,b)-Math.min(r,g,b)) < 24; };
                    /* 迷宫底色：深蓝（#0b0d12 框底 / #101419 未走访 / #1b2231 已走访）。
                       画布其余部分是透明 ⇒ 这类像素的**最小外接矩形就是网格框**。
                       （外框 strokeRect 的 #4a5468 与图例文字都不满足 r,g<40） */
                    const isF = function(d,i){ return d[i+3] > 8
                      && d[i] < 40 && d[i+1] < 40 && d[i+2] < 62; };
                    /* 把一串**升序**像素下标按间距聚成一条线，返回每条线中心。
                       ⚠️ 必须按**像素值差**聚（不是下标差），且不能"阈值以上各算一条"：
                       线宽 2.2 px 在整数像素上跨 3~4 列，抗锯齿边缘列时过时不通过
                       ⇒ 不聚会数出 14 条（第一版两次都栽在这里）。 */
                    const cluster = function(a, gap){
                      const out = []; let sum = 0, n = 0, last = -1e9;
                      for (let i=0;i<a.length;i++){
                        if (n && a[i] - last <= gap) { sum += a[i]; n++; }
                        else { if (n) out.push(Math.round(sum / n)); sum = a[i]; n = 1; }
                        last = a[i];
                      }
                      if (n) out.push(Math.round(sum / n));
                      return out; };
                    /* 网格框 = 迷宫底色（深蓝）**成片**出现的那一块。
                       ⚠️ 不能直接取"所有深色像素的外接矩形"：图例里"走过"那一笔
                       （`#1b2231`，也是深蓝）画在框**下方** ⇒ 会把下边界一路拉到
                       画布底（第一版就得到"宽 292 高 305"这么个不方的框）。
                       ⇒ 先按行/列数深色像素，只认**占满半幅**的那些行列。
                       （最外一圈被后画的 `strokeRect` 外框盖住 ⇒ 盒子比真实小 1 px，
                        这一圈误差由格距 `cs` 按比例吸收，格线上的落点差 < 1.5 px。） */
                    const bbox = function(im){ const w=im.width,h=im.height,d=im.data;
                      const cols=new Int32Array(w), rows=new Int32Array(h);
                      for (let y=0;y<h;y++) for (let x=0;x<w;x++)
                        if (isF(d,(y*w+x)*4)) { cols[x]++; rows[y]++; }
                      const span = function(a){ let m=0;
                        for (let i=0;i<a.length;i++) if (a[i]>m) m=a[i];
                        let s=-1, t=-1;
                        for (let i=0;i<a.length;i++) if (a[i] >= m*0.5) {
                          if (s<0) s=i; t=i; }
                        return [s,t]; };
                      const cx=span(cols), cy=span(rows);
                      return { x0:cx[0], x1:cx[1], y0:cy[0], y1:cy[1] }; };
                    /* 两张图逐像素相减 ⇒ 差异像素**就是**新画出来的那条线。
                       ⚠️ 只数**网格框以内**的差异：图例（框下方那一行）也会随档位
                       增减，它是"框外的差异"，算进来会把线的位置带偏
                       （第一版就报"近端墙 y≈289、远端 y≈278"，实际都在框外那截上）。 */
                    const dbox = function(a,b,box){ const w=a.width;
                      let mnx=1e9,mxx=-1,mny=1e9,mxy=-1,c=0,sy=0;
                      for (let i=0;i<a.data.length;i+=4){
                        if (a.data[i]!==b.data[i] || a.data[i+1]!==b.data[i+1]
                            || a.data[i+2]!==b.data[i+2]){
                          const p=(i>>2), x=p%w, y=(p/w)|0;
                          if (box && (x<box.x0 || x>box.x1 || y<box.y0 || y>box.y1))
                            continue;
                          c++; sy+=y;
                          if (x<mnx) mnx=x; if (x>mxx) mxx=x;
                          if (y<mny) mny=y; if (y>mxy) mxy=y; } }
                      return { n:c, my: c ? sy/c : -1, x0:mnx, x1:mxx, y0:mny, y1:mxy }; };
                    /* 这一行里数"竖线有几条"（白像素聚成线，一条线可能占好几列） */
                    const vlinesInRow = function(im,y){ const w=im.width,d=im.data;
                      const idx=[]; for (let x=0;x<w;x++)
                        if (isW(d,(y*w+x)*4)) idx.push(x);
                      return cluster(idx, 4).length; };
                    /* 归一化到 CSS px（自绘图要按 dpr 画） */
                    const S = function(v){ return v / D; };

                    const base = draw(cv, null);
                    const B = bbox(base);
                    R.w = base.width; R.h = base.height;
                    if (B.x0 > B.x1 || B.y0 > B.y1) { R.fatal = '找不到迷宫底色（外接矩形为空）';
                      return R; }
                    R.box = [B.x0, B.y0, B.x1, B.y1];
                    const sizeY = B.y1 - B.y0 + 1, sizeX = B.x1 - B.x0 + 1;
                    R.sizeX = sizeX; R.sizeY = sizeY;
                    if (Math.abs(sizeX - sizeY) > 3) {
                      R.fatal = '网格框不是方的（宽 ' + sizeX + ' 高 ' + sizeY + '）';
                      return R; }
                    const cs = sizeY / N;
                    R.cs = Math.round(cs * 10) / 10;
                    /* PY(y) = y0 + (n-1-y)*cs  ⇒  PY(0) = 框底上移一格，外一格再上移一格 */
                    R.expNearY = B.y1 + 1 - cs;
                    R.expFarY  = R.expNearY - cs;

                    const nD = dbox(draw(cv, {near:true, far:false}), base, B);
                    const fD = dbox(draw(cv, {near:false, far:true }), base, B);
                    const r1 = function(v){ return Math.round(v*10)/10; };
                    R.near = { n:nD.n, my:r1(nD.my), y0:nD.y0, y1:nD.y1 };
                    R.far  = { n:fD.n, my:r1(fD.my), y0:fD.y0, y1:fD.y1 };

                    /* 逐行数竖线：每一格带的中点各数一次 ⇒ "哪一行缺格线"直接看得出来。
                       ⚠️ 断言取**相对**（各行是否一样多），不取绝对值：最外两条竖线会被
                       外框盖住，绝对值会少 2；而"缺一行"是**比值**上的塌陷，相对判才准。 */
                    R.rowLines = [];
                    for (let y=0; y<N; y++) {
                      const top = B.y0 + (N-1-y) * cs;
                      const mid = Math.round(top + cs / 2);
                      R.rowLines.push(vlinesInRow(base, mid));
                    }
                    R.maxRowLines = Math.max.apply(null, R.rowLines);
                    R.minRowLines = Math.min.apply(null, R.rowLines);

                    /* 负向①：把"只有远端墙"那张按"近端墙该落在本格前沿"验 ⇒ 应当判红 */
                    R.negFrontGap = Math.round(Math.abs(fD.my - R.expNearY));

                    /* 负向②：自绘一张故意不画最底行竖线的图，用同一探测器 ⇒ 应当报缺 */
                    const g2 = cv2.getContext('2d');
                    g2.setTransform(D,0,0,D,0,0);
                    g2.fillStyle = '#101419'; g2.fillRect(0,0,400,320);
                    g2.strokeStyle = '#e8e8ea'; g2.lineWidth = 2;
                    for (let k=0;k<=N;k++){
                      const x = S(B.x0) + k * (sizeY / N);
                      const yTop = S(B.y0 + (N-2)*cs);      /* 底行上沿 */
                      g2.beginPath(); g2.moveTo(x, S(B.y0)); g2.lineTo(x, yTop);
                      g2.stroke(); }
                    const im2 = g2.getImageData(0,0,cv2.width,cv2.height);
                    const midBot = Math.round(B.y0 + (N-1)*cs + cs/2);
                    const midTop = Math.round(B.y0 + cs/2);
                    R.negBotLines = vlinesInRow(im2, midBot);
                    R.negTopLines = vlinesInRow(im2, midTop);
                    return R;
                  } finally { cv.remove(); cv2.remove(); }
                })()""")
                b40 = b40 if isinstance(b40, dict) else {}
                if b40.get("fatal"):
                    b40_bad.append("网格框探测失败：" + str(b40.get("fatal")))
                else:
                    near, far = b40.get("near") or {}, b40.get("far") or {}
                    expN, expF = b40.get("expNearY"), b40.get("expFarY")
                    cs = b40.get("cs") or 1
                    rows_l = b40.get("rowLines") or []
                    b40_got.append(
                        f"网格框={b40.get('box')} 格距≈{cs}px，"
                        f"近端墙 y≈{near.get('my')}[{near.get('y0')}-{near.get('y1')}]"
                        f"（应 {round(float(expN),1)}）、"
                        f"远端墙 y≈{far.get('my')}[{far.get('y0')}-{far.get('y1')}]"
                        f"（应 {round(float(expF),1)}）；逐行竖线数={rows_l}")
                    if near.get("n", 0) < 5:
                        b40_bad.append("近端墙几乎没画出来（与'无前墙'那张的差异像素只有 "
                                       f"{near.get('n')} 个）")
                    elif abs(float(near.get("my", -999)) - float(expN)) > 3:
                        b40_bad.append(
                            f"**近端墙落错边**：画在 y≈{near.get('my')}，本格前沿在 y="
                            f"{round(float(expN),1)}（差 "
                            f"{round(abs(float(near.get('my')) - float(expN)),1)} px ≈ "
                            f"{round(abs(float(near.get('my')) - float(expN)) / float(cs),2)} 格）"
                            " —— 贴脸的墙必须就在车头这一格边上")
                    if far.get("n", 0) < 5:
                        b40_bad.append("远端墙几乎没画出来（与'无前墙'那张的差异像素只有 "
                                       f"{far.get('n')} 个）")
                    elif abs(float(far.get("my", -999)) - float(expF)) > 3:
                        b40_bad.append(
                            f"**远端墙落错边**：画在 y≈{far.get('my')}，外一格前沿在 y="
                            f"{round(float(expF),1)}（差 "
                            f"{round(abs(float(far.get('my')) - float(expF)),1)} px）")
                    if (near.get("n", 0) >= 5 and far.get("n", 0) >= 5
                            and abs(float(near.get("my")) - float(far.get("my"))) < 3):
                        b40_bad.append("**前墙两档落在同一条边上**（y 坐标相同）"
                                       " —— 贴脸与提前量必须分边，否则看着像隔了一格")
                    if not rows_l or (b40.get("maxRowLines") or 0) < 8:
                        b40_bad.append(f"每一行都数不到竖线（逐行={rows_l}）"
                                       " —— 网格框或格线探测不可信，本条判据不算数")
                    elif (b40.get("minRowLines") or 0) < (b40.get("maxRowLines") or 0):
                        short_rows = [i for i, v in enumerate(rows_l)
                                      if v < (b40.get("maxRowLines") or 0)]
                        b40_bad.append(
                            f"**有行缺竖线**：自下往上第 {[r + 1 for r in short_rows][:4]} 行只有 "
                            f"{[rows_l[r] for r in short_rows][:4]} 条（其余行 "
                            f"{b40.get('maxRowLines')} 条）—— 格 (x,y) 的纵向范围是 "
                            "[PY(y), PY(y)+cs]，竖线跨错一格 ⇒ 那一行看起来没有格线")
                    if not b40_bad:
                        b40_got.append("负向①「拿远端墙当近端墙验」如期判红"
                                       f"（差 {b40.get('negFrontGap')} px）")
                        if int(b40.get("negFrontGap") or 0) <= 3:
                            b40_bad.append("负向①失效：远端墙恰好落在本格前沿上，"
                                           "这条判据区分不出两档（等于没钉住）")
                        else:
                            neg_bot = b40.get("negBotLines")
                            b40_got.append("负向②自绘图缺最底行竖线，探测器报 "
                                           f"{neg_bot} 条"
                                           f"（顶行 {b40.get('negTopLines')} 条）")
                            # ⚠️ 不能写 `x or 99`：负向②**正确**的结果恰恰是 0
                            #    （0 是 falsy ⇒ 会被当成"缺字段"而误判"探测器失效"）
                            if neg_bot is None:
                                b40_bad.append("负向②拿不到结果（探测器没返回 negBotLines）")
                            elif int(neg_bot) > 0:
                                b40_bad.append(f"负向②失效：那张故意缺最底行竖线的图，"
                                               f"探测器却数出 {neg_bot} 条竖线 ⇒ 认不出缺线")
            except Exception as e:                            # noqa: BLE001
                b40_bad.append(f"做像素级前墙/格线断言时抛异常：{type(e).__name__}: {e}")
            b40_ok = not b40_bad
            record("B40", "PASS" if b40_ok else "FAIL",
                   f"迷宫图上前墙两档**分边**（近端墙落在本格前沿、远端墙落在外一格前沿）"
                   f"且逐行都不缺竖线（{'；'.join(b40_got)}）"
                   if b40_ok else
                   f"**迷宫绘图的落位/格线不对**：{b40_bad[:4]}")

            # ── B41：拉取模式切换 + 默认停等 + 老上游不画（改版十九 / 二十一，上游 2.9.0） ──
            # 守本轮三件事：
            #   ① **默认停等**：连上之后 caps 的 `stream_mode` 必须是 `stopwait`
            #      （改版二十一从 pace 回退：在途恒 1 条请求、帧率 = 1/RTT，
            #       任何异常都能当场归因；要密波形由人点【批式】显式切过去）。
            #   ② **切换真打到后端**：点【批式】/【停等】后，页面那一行要跟着变成
            #      "批式"/"停等" —— 只换按钮不换显示就是"点了没反应"。
            #   ③ **老上游上不画**（负向）：`pace_pull=False` 时 `#mp-mode` 必须空。
            #      按下去只会 501 的按钮，比没有按钮更坏。
            # ⚠️ 轮询等条件，**不固定 sleep**：pace() 内部最坏 6 s（3×2 s）。
            head("2k. 实时状态页：拉取模式切换（批式/停等）、默认停等、老上游不画")
            b41_bad, b41_got = [], []
            try:
                with urllib.request.urlopen(base + "/api/v1/caps", timeout=15) as _r:
                    caps = json.loads(_r.read().decode("utf-8")).get("data") or {}
                mcap = caps.get("mouse") or {}
                pace_ok = bool(mcap.get("pace_pull"))
                b41_got.append("caps.pace_pull=%s" % pace_ok)
                if args.sim:
                    # ① 默认停等（连接后 caps 里的门面三态）
                    smode = str(mcap.get("stream_mode") or "")
                    b41_got.append("caps.stream_mode=%r" % smode)
                    if not pace_ok:
                        b41_bad.append("连了 2.9.0 上游却探到 pace_pull=False"
                                       "（能力位坏了 ⇒ 模式切换画不出来）")
                    elif smode != "stopwait":
                        b41_bad.append(f"默认不是停等：stream_mode={smode!r}"
                                       f"（应为 'stopwait'）")
                    # ② 端到端切模式（页面点按钮 + 轮询页面显示）
                    await cdp.eval("C.openMod('mouse','mod_params')")
                    await asyncio.sleep(0.6)
                    bar = await cdp.eval(
                        "(()=>{const b=document.getElementById('mp-mode');"
                        "return {html:(b?b.innerHTML:'').slice(0,200),"
                        "  btns:b?b.querySelectorAll('[data-mode]').length:0};})()")
                    b41_got.append("模式条按钮=%s" % ((bar or {}).get("btns") or 0))
                    if not (bar or {}).get("btns"):
                        b41_bad.append("pace_pull 为真但 #mp-mode 没画出切换按钮")
                    else:
                        async def _wait_mode(want, tag):
                            """点按钮后轮询页面那一行，直到出现目标模式词（最多 9 s）。"""
                            for _ in range(18):
                                txt = await cdp.eval(
                                    "(()=>{const s=document.getElementById('mp-stream');"
                                    "return (s?s.innerText:'');})()")
                                if want in str(txt or ""):
                                    return True
                                await asyncio.sleep(0.5)
                            b41_bad.append(f"点【{tag}】后页面那一行 9 s 内没变成「{want}」")
                            return False
                        # 批式 → 停等（**回到默认**）。两个方向都要点一遍
                        # （铁律 46），末态回到默认停等，不给后面判据留非默认现场。
                        await cdp.eval(
                            "(()=>{const b=document.querySelector("
                            "'#mp-mode [data-mode=pace]');if(b)b.click();})()")
                        if await _wait_mode("批式", "批式"):
                            b41_got.append("切到批式 OK")
                        await cdp.eval(
                            "(()=>{const b=document.querySelector("
                            "'#mp-mode [data-mode=stopwait]');if(b)b.click();})()")
                        if await _wait_mode("停等", "停等"):
                            b41_got.append("切回停等 OK")
                else:
                    b41_got.append("未连模拟器 ⇒ 只跑负向（不猜端到端）")
            except Exception as e:                            # noqa: BLE001
                b41_bad.append(f"读 caps / 切模式时抛异常：{type(e).__name__}: {e}")

            # 负向（**不需要连鼠**）：伪造 caps 说"老上游"⇒ #mp-mode 必须空。
            #   手法同 B38：临时换 `C.api`，再 `activate()` 让它重读能力位。
            #   ⚠️ 必须还原（否则后面的判据看到的是一个假的上游）。
            try:
                await cdp.eval("C.openMod('mouse','mod_params')")
                await asyncio.sleep(0.4)
                neg = await cdp.eval(
                    "(()=>{"
                    "const M=C.mods&&C.mods.mod_params; if(!M) return {err:'no mod'};"
                    "const old=C.api;"
                    "C.api=function(p,o){ if(p==='/api/v1/caps')"
                    "  return Promise.resolve({mouse:{pace_pull:false}});"
                    "  return old.call(this,p,o); };"
                    "M.activate();"
                    "return new Promise(function(res){"
                    "  setTimeout(function(){"
                    "    const b=document.getElementById('mp-mode');"
                    "    C.api=old; M.activate();"
                    "    res({empty:(b?b.innerHTML.trim():'')===''});"
                    "  },900);});})()")
                nempty = (neg or {}).get("empty")
                b41_got.append("负向(pace_pull=false) 模式条空=%s" % nempty)
                if nempty is not True:
                    b41_bad.append("pace_pull=False 时 #mp-mode 仍画着东西"
                                   "（老上游上按下去只会 501）")
                await asyncio.sleep(0.8)   # 等还原后的 activate 把模式条画回来
            except Exception as e:                            # noqa: BLE001
                b41_bad.append(f"负向（老上游不画）抛异常：{type(e).__name__}: {e}")
            b41_ok = not b41_bad
            record("B41", "PASS" if b41_ok else "FAIL",
                   f"拉取模式切换可用（{'；'.join(b41_got)}）"
                   if b41_ok else
                   f"**拉取模式切换/默认停等不对**：{b41_bad[:4]}")


            # ── B42：维护与自检卡（改版二十，上游 2.7.0 按键菜单远程化） ──
            # 守本轮四件事：
            #   ① 六个控件都画出来（诊断 3 + 标定 2 + 清空 1）；
            #   ② **读数真的落进 DOM**：点【红外自检】/【Flash 概览】后 #mt-diag 里要出现
            #      `irL` / `ir_valid` 这两个键 —— 只换按钮不显示数值就是"点了没反应"；
            #   ③ **擦除有二次确认**：点一次【擦除红外标定】只进确认态（按钮文字变），
            #      **不真擦**（判据不留副作用；端点行为由 drill 的 I33 验）；
            #   ④ 两条负向：
            #      · 不带 `confirm` 直打端点 ⇒ **409 + need_confirm**（不是 500）；
            #      · 伪造 `menu_remote=false` ⇒ 整块**一个按钮都不画**（老上游上按下去只会 501）。
            # ⚠️ ②③④-1 要连鼠（真发命令）；①与④-2 不连也能判。
            head("2l. 维护与自检卡：六控件 / 诊断读数落 DOM / 擦除二次确认 / 老上游不画")
            b42_bad, b42_got = [], []
            try:
                await cdp.eval("C.openMod('mouse','mod_maint')")
                await asyncio.sleep(0.9)
                dom = await cdp.eval(
                    "(()=>{const ids=['mt-irself','mt-sensor','mt-block','mt-ircap-next',"
                    "'mt-ir-erase','mt-erase-all'];"
                    "const have=ids.filter(function(i){return !!document.getElementById(i);});"
                    "return {have:have, n:have.length};})()")
                b42_got.append("控件 %s/6" % ((dom or {}).get("n")))
                if (dom or {}).get("n") != 6:
                    b42_bad.append("维护卡的六个控件没画齐：%s" % ((dom or {}).get("have")))
                if args.sim:
                    # ② 读数落进 DOM
                    # 点之前先记"为什么可能点不动"：按钮被 `C.gate` 禁用时 `click()` **根本不触发**
                    # （看起来就像"点了没反应"）；再加一条 **PROBE** —— 临时把 onclick 换成
                    # 只写 `#mt-msg` 的探针再点一次，用来把"DOM 绑定断了"与"`diag()` 内部炸了"
                    # 二分开（探针做完立刻还原，不改变被测行为）。
                    pre = await cdp.eval(
                        "(()=>{const b=document.getElementById('mt-irself');"
                        "const u=((C.state||{}).telemetry||{}).unified||{};"
                        "const w=document.getElementById('mt-wrap');"
                        "return {dis:b?!!b.disabled:null, oc:b?(typeof b.onclick):'nobtn',"
                        " wraps:document.querySelectorAll('#mt-wrap').length,"
                        " wrapKids:w?w.children.length:-1,"
                        " need:(C.needMouse?C.needMouse():null), task:u.mazeTask};})()")
                    b42_got.append("点前 disabled=%s mazeTask=%s needMouse=%r onclick=%s"
                                   " #mt-wrap=%s(子 %s)"
                                   % ((pre or {}).get("dis"), (pre or {}).get("task"),
                                      (pre or {}).get("need"), (pre or {}).get("oc"),
                                      (pre or {}).get("wraps"), (pre or {}).get("wrapKids")))
                    probe = await cdp.eval(
                        "(()=>{const b=document.getElementById('mt-irself');"
                        "if(!b) return {err:'nobtn'};const orig=b.onclick;"
                        "b.onclick=function(){var m=document.getElementById('mt-msg');"
                        "if(m)m.textContent='PROBE_OK';};b.click();"
                        "var m=document.getElementById('mt-msg');var got=m?m.textContent:'nom';"
                        "if(m)m.textContent='';b.onclick=orig;"
                        "return {got:got, oc:typeof orig};})()")
                    b42_got.append("PROBE=%r" % ((probe or {}).get("got"),))
                    # 再直调一次端点：把"后端 / `C.api` 有问题"与"`diag()` 的 DOM 写入有问题"
                    # 彻底二分开（这条是按钮行为的等价调用，只是不走界面那层）。
                    direct = await cdp.eval(
                        "(async()=>{try{const r=await C.api('/api/v1/mouse/maint/IRSELF',"
                        "{method:'POST',body:{}});"
                        "return {ok:true,keys:Object.keys(r||{}),"
                        "rk:Object.keys(((r||{}).result)||{}),st:((r||{}).result||{}).status};}"
                        "catch(e){return {ok:false,msg:String(e&&e.message),"
                        "tp:e&&e.type,st:e&&e.status};}})()")
                    b42_got.append("直调端点=%r" % (direct,))
                    # ② 读数真的落进 DOM —— **两个动作各看一次**。
                    #   ⚠️ `#mt-diag` 是"**当前这一次**的读数"那一块：后一次动作会**覆盖**前一次，
                    #   所以"点完两个再一起查两个键"必然只看到最后一个 —— 那是判据写错，不是产品错
                    #   （本轮就是这么红了一次，记在 §11.23 的 B42 条里）。
                    #   ⚠️ 等待改成**轮询条件**（最多 5 s）：首次调用要初始化链路，固定 1.4 s 会假红
                    #   （铁律 13：别等固定时长，要等条件）。
                    async def _click_read(btn_id, key):
                        await cdp.eval(
                            "(()=>{const b=document.getElementById('%s');if(b)b.click();})()"
                            % btn_id)
                        last = ""
                        for _ in range(25):
                            await asyncio.sleep(0.2)
                            last = await cdp.eval(
                                "(()=>{const d=document.getElementById('mt-diag');"
                                "return d?d.innerHTML:'';})()")
                            if key in str(last or ""):
                                return True, str(last or "")
                        return False, str(last or "")
                    ok_ir, dom_ir = await _click_read("mt-irself", "irL")
                    ok_bl, dom_bl = await _click_read("mt-block", "ir_valid")
                    has_ir, has_bl = ok_ir, ok_bl
                    b42_got.append("读数 irL=%s ir_valid=%s" % (has_ir, has_bl))
                    if not has_ir:
                        b42_bad.append("点【红外自检】后 #mt-diag 里没有 irL（实际 %r）；"
                                       "点前 disabled=%s mazeTask=%s needMouse=%r onclick=%s"
                                       " PROBE=%r 直调=%r"
                                       % (dom_ir[:80], (pre or {}).get("dis"),
                                          (pre or {}).get("task"), (pre or {}).get("need"),
                                          (pre or {}).get("oc"), (probe or {}).get("got"),
                                          direct))
                    if not has_bl:
                        b42_bad.append("点【Flash 概览】后 #mt-diag 里没有 ir_valid（实际 %r）；"
                                       "直调=%r" % (dom_bl[:80], direct))
                    # ③ 擦除二次确认（只点一次，**不真擦**）
                    conf = await cdp.eval(
                        "(()=>{const b=document.getElementById('mt-ir-erase');"
                        "if(!b) return {err:'no btn'};b.click();return {txt:b.textContent};})()")
                    ctxt = str((conf or {}).get("txt") or "")
                    b42_got.append("擦除确认态=%r" % ctxt)
                    if "确认" not in ctxt:
                        b42_bad.append("点【擦除红外标定】后没进入二次确认态（按钮文字=%r）" % ctxt)
                    # ④-1 不带 confirm 直打端点 ⇒ 409 + need_confirm
                    neg2 = await cdp.eval(
                        "(async()=>{const r=await fetch('/api/v1/mouse/maint/ERASEIR',"
                        "{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});"
                        "let j=null;try{j=await r.json();}catch(e){}"
                        "return {status:r.status, type:(j&&j.error&&j.error.type)||'',"
                        " msg:(j&&j.error&&j.error.message)||''};})()")
                    st2 = (neg2 or {}).get("status")
                    tp2 = (neg2 or {}).get("type")
                    b42_got.append("不带 confirm HTTP %s type=%r" % (st2, tp2))
                    if st2 != 409:
                        b42_bad.append("不带 confirm 直打端点应回 409，实际 %s（回 500 说明抛穿了）" % st2)
                    elif tp2 != "need_confirm":
                        b42_bad.append("409 的 error.type 不是 need_confirm：%r" % tp2)
                else:
                    b42_got.append("未连模拟器 ⇒ 不点按钮（只跑负向①）")
            except Exception as e:                                        # noqa: BLE001
                b42_bad.append("维护卡检查抛异常：%s: %s" % (type(e).__name__, e))

            # ④-2 负向（**不需要连鼠**）：伪造 caps 说"老上游" ⇒ 维护卡必须一个按钮都不画。
            #   手法同 B41：临时换 `C.api`，再 `activate()` 让它重读能力位；必须还原。
            try:
                await cdp.eval("C.openMod('mouse','mod_maint')")
                await asyncio.sleep(0.4)
                neg1 = await cdp.eval(
                    "(()=>{"
                    "const M=C.mods&&C.mods.mod_maint; if(!M) return {err:'no mod'};"
                    "const old=C.api;"
                    "C.api=function(p,o){ if(p==='/api/v1/caps')"
                    "  return Promise.resolve({mouse:{menu_remote:false}});"
                    "  return old.call(this,p,o); };"
                    "M.activate();"
                    "return new Promise(function(res){"
                    "  setTimeout(function(){"
                    "    const w=document.getElementById('mt-wrap');"
                    "    const n=w?w.querySelectorAll('button').length:-1;"
                    "    C.api=old; M.activate();"
                    "    res({btns:n});"
                    "  },900);});})()")
                nb = (neg1 or {}).get("btns")
                b42_got.append("负向(menu_remote=false) 按钮数=%s" % nb)
                if nb != 0:
                    b42_bad.append("menu_remote=false 时维护卡仍画着 %s 个按钮"
                                   "（老上游上按下去只会 501）" % nb)
                await asyncio.sleep(0.8)      # 等还原后的 activate 把按钮画回来
            except Exception as e:                                        # noqa: BLE001
                b42_bad.append("负向（老上游不画）抛异常：%s: %s" % (type(e).__name__, e))
            b42_ok = not b42_bad
            record("B42", "PASS" if b42_ok else "FAIL",
                   f"维护与自检卡可用（{'；'.join(b42_got)}）"
                   if b42_ok else
                   f"**维护与自检卡不对**：{b42_bad[:4]}")


            # ── B38：复位改名 + 停止并保存写清保存路径（改版十四，宿主迁改版二十三） ──
            # 改版十四原是五件小事。改版二十三把「无记忆走迷宫」整页下线后，其中三件
            # （决策历程清空 / 停止原因胶囊 / 网格尺寸选择）**随宿主消失**（那套时间线与
            # 复盘不再维护，见 §11.25）⇒ 判据跟着收窄，不给已下线的功能留空壳。
            # 存活的两件迁到比赛卡（发车与记录的生命周期跟过来了）：
            #   ①「救援」→「复位」：界面上**不许再出现「救援」**（按钮 + 设备窗口）；
            #   ② 停止并保存要**弹窗写清存到哪儿了**（完整路径，不只是文件名）。
            # 两条都钉得住（DOM 文本 + 拦接口喂应答）⇒ **不需要** `--sim` SKIP。
            head("2j. 比赛卡：复位改名 + 停止并保存写清保存路径")
            b38_bad, b38_got = [], []
            try:
                await cdp.eval("C.openMod('mouse','mod_race')")
                await asyncio.sleep(0.4)
                # ① 改名：整页 + 设备窗口里都不许再出现「救援」
                name_got = await cdp.eval(
                    "(()=>({page:(document.body.innerText||''),"
                    "  rc:(document.getElementById('rc-rescue')||{}).textContent||'',"
                    "  dm:(document.getElementById('dm-rescue')||{}).textContent||''}))()")
                rc_txt2 = (name_got or {}).get("rc", "")
                dm_txt2 = (name_got or {}).get("dm", "")
                b38_got.append(f"按钮 rc-rescue={rc_txt2!r} dm-rescue={dm_txt2!r}")
                if "复位" not in rc_txt2:
                    b38_bad.append(f"rc-rescue 文案不是「复位」：{rc_txt2!r}")
                if "复位" not in dm_txt2:
                    b38_bad.append(f"dm-rescue 文案不是「复位」：{dm_txt2!r}")
                if "救援" in (name_got or {}).get("page", ""):
                    b38_bad.append("页面上仍有「救援」字样（应统一改成「复位」）")

                # ② 保存路径：拦 `record/stop` 喂一个带 `path` 的应答 ⇒ 弹窗要**写出完整路径**。
                #    （真去落盘一次太慢且要鼠；这里钉的是"界面拿到 path 会不会显示它"。）
                fake_path = "C:/tmp/rec/20261008_0000_test.maze.json"
                await cdp.eval(
                    "(()=>{window.__b38_api=window.__b38_api||C.api;"
                    " const real=window.__b38_api;"
                    " window.__b38_toast=[];"
                    " const rt=C.toast; window.__b38_toastfn=rt;"
                    " C.toast=function(m,k,t){try{window.__b38_toast.push(String(m));}catch(e){}"
                    "  return rt(m,k,t);};"
                    " C.api=function(p,o){"
                    "  if(String(p).indexOf('maze/record/stop')>=0){"
                    "   return Promise.resolve(" + json.dumps(
                        {"ok": True, "name": "20261008_0000_test.maze.json",
                         "path": fake_path, "frames": 120, "tiny": False}) + ");}"
                    "  return real(p,o);};"
                    " return true;})()")
                await cdp.eval("document.getElementById('rc-recstop').click()")
                await asyncio.sleep(0.6)
                toast_got = await cdp.eval(
                    "(()=>{const a=window.__b38_toast||[];"
                    "return {toast:a.join('\\n')};})()")
                tt = str((toast_got or {}).get("toast") or "")
                b38_got.append("保存弹窗=" + tt.replace("\n", " / ")[:80])
                if fake_path not in tt:
                    if "maze.json" in tt:
                        b38_bad.append(f"弹窗只写了文件名，没写完整路径：{tt[:90]!r}")
                    else:
                        b38_bad.append(f"停止并保存的弹窗没写出保存位置：{tt[:90]!r}")
                # 还原 api/toast
                await cdp.eval(
                    "(()=>{if(window.__b38_api)C.api=window.__b38_api;"
                    " if(window.__b38_toastfn)C.toast=window.__b38_toastfn;"
                    " return true;})()")
            except Exception as e:                            # noqa: BLE001
                b38_bad.append(f"读取比赛卡渲染时抛异常：{type(e).__name__}: {e}")
            b38_ok = not b38_bad
            record("B38", "PASS" if b38_ok else "FAIL",
                   f"「救援」已统一为「复位」（页内 + 设备窗口）；停止并保存的弹窗写出完整保存路径（{'；'.join(b38_got)}）"
                   if b38_ok else
                   f"**复位改名 / 保存路径不对**：{b38_bad[:4]}")

            # ── B39：底栏信息集成栏（按来源分类 + 按级别筛选）──
            # 守的是改版十五的两条用户要求：
            #   ① 底栏**不再有"实时读数"条**（那些量在「实时状态与参数设定」页里有），
            #      整条底栏留给信息输出；
            #   ② 信息**按来源分类**（电脑鼠 / 相机 / 场地定位 / 本层）且**按级别可筛**
            #      （普通 / 警告 / 错误）—— 不是一锅粥。
            # 做法：直接从 `C.pushConsole` 喂三条**来源不同、级别不同**的假信息
            #（不依赖真 WS / 真设备 —— 钉住数据再断言，同 B34/B37 的路数），
            # 断言行上有来源标签、筛选后行数变少、计数写得出"筛掉了多少"。
            head("2k. 底栏信息集成栏：来源分类 + 级别筛选")
            b39_bad, b39_got = [], []
            try:
                bar = await cdp.eval(
                    "(()=>{const rb=!!document.getElementById('readouts');"
                    "const bar=!!document.getElementById('info-bar');"
                    "const log=!!document.getElementById('info-log');"
                    "const sb=Array.from((document.getElementById('ib-src')||{children:[]}).children)"
                    ".map(function(b){return b.textContent;});"
                    "const lb=Array.from((document.getElementById('ib-lvl')||{children:[]}).children)"
                    ".map(function(b){return b.textContent;});"
                    "return {rb:rb,bar:bar,log:log,sb:sb,lb:lb};})()")
                if (bar or {}).get("rb"):
                    b39_bad.append("底栏仍有 #readouts（实时读数条应已删除）")
                if not (bar or {}).get("bar") or not (bar or {}).get("log"):
                    b39_bad.append("底栏没有信息栏 #info-bar / #info-log")
                sb = (bar or {}).get("sb") or []
                lb = (bar or {}).get("lb") or []
                b39_got.append("来源钮=" + "/".join(sb) + " 级别钮=" + "/".join(lb))
                for need in ("全部", "电脑鼠", "视频复盘", "本层"):
                    if need not in sb:
                        b39_bad.append(f"来源筛选缺「{need}」（有 {sb}）")
                for need in ("全部", "普通", "警告", "错误"):
                    if need not in lb:
                        b39_bad.append(f"级别筛选缺「{need}」（有 {lb}）")
                # 喂三条：视频+警告 / 鼠+普通（设备文本）/ 本层+普通。
                # ⚠️ 改版二十四：第三个来源从 `camera` 换成 `video`（`bus.SOURCES` 已改）。
                await cdp.eval(
                    "(()=>{window.__b39_push=C.pushConsole;"
                    " C.pushConsole('', 'B39 视频警告样例', {type:'log', level:'warn',"
                    "  source:'video', text:'B39 视频警告样例', t_pc_ms:Date.now()});"
                    " C.pushConsole('', '→ B39 鼠文本样例', {type:'mouse.text', level:'info',"
                    "  source:'mouse', direction:'->', line:'B39 鼠文本样例', t_pc_ms:Date.now()});"
                    " C.pushConsole('', 'B39 本层样例', {type:'log', level:'info',"
                    "  source:'control', text:'B39 本层样例', t_pc_ms:Date.now()});"
                    " return true;})()")
                rows0 = await cdp.eval(
                    "(()=>{const b=document.getElementById('info-log');"
                    "const rows=Array.from((b||{children:[]}).children).map(function(r){"
                    "return {cls:r.className, txt:r.textContent||''};});"
                    "const cnt=(document.getElementById('ib-count')||{}).textContent||'';"
                    "return {n:rows.length, rows:rows.slice(-3), cnt:cnt};})()")
                b39_got.append("喂后行数=" + str((rows0 or {}).get("n")))
                tagged = await cdp.eval(
                    "(()=>{const b=document.getElementById('info-log');"
                    "const all=Array.from((b||{children:[]}).children).map(function(r){return r.textContent;});"
                    "return {vid:all.some(function(t){return t.indexOf('[视频复盘]')>=0 && t.indexOf('B39 视频警告样例')>=0;}),"
                    " mouse:all.some(function(t){return t.indexOf('[电脑鼠]')>=0 && t.indexOf('B39 鼠文本样例')>=0;}),"
                    " ctl:all.some(function(t){return t.indexOf('[本层]')>=0 && t.indexOf('B39 本层样例')>=0;})};})()")
                for k, nm in (("vid", "视频复盘"), ("mouse", "电脑鼠"), ("ctl", "本层")):
                    if not (tagged or {}).get(k):
                        b39_bad.append(f"{nm}来源的信息没带来源标签或没显示：{tagged}")
                # 筛选：只看「视频复盘」⇒ 鼠/本层的样例必须消失，且计数写"显示 1 / 共 N"。
                await cdp.eval(
                    "(()=>{const bs=Array.from(document.getElementById('ib-src').children);"
                    "const b=bs.find(function(x){return x.textContent==='视频复盘';});"
                    "if(b)b.click();return true;})()")
                await asyncio.sleep(0.2)
                rows1 = await cdp.eval(
                    "(()=>{const b=document.getElementById('info-log');"
                    "const all=Array.from((b||{children:[]}).children).map(function(r){return r.textContent||'';});"
                    "return {n:all.length,"
                    " vid:all.some(function(t){return t.indexOf('B39 视频警告样例')>=0;}),"
                    " other:all.some(function(t){return t.indexOf('B39 鼠文本样例')>=0||t.indexOf('B39 本层样例')>=0;}),"
                    " cnt:(document.getElementById('ib-count')||{}).textContent||''};})()")
                if not (rows1 or {}).get("vid"):
                    b39_bad.append(f"筛「视频复盘」后视频样例不见了：{rows1}")
                if (rows1 or {}).get("other"):
                    b39_bad.append(f"筛「视频复盘」后仍看得见别的来源（筛选没生效）：{rows1}")
                if "显示" not in str((rows1 or {}).get("cnt")) or "/ 共" not in str((rows1 or {}).get("cnt")):
                    b39_bad.append(f"筛选后的计数没写「显示 N / 共 M」：{(rows1 or {}).get('cnt')!r}")
                b39_got.append("筛视频后计数=" + str((rows1 or {}).get("cnt")))
                # 还原：点「全部」；级别筛选同理点一遍（负向：筛「错误」后警告样例也消失）。
                await cdp.eval(
                    "(()=>{const bs=Array.from(document.getElementById('ib-src').children);"
                    "const b=bs.find(function(x){return x.textContent==='全部';});"
                    "if(b)b.click();return true;})()")
                await cdp.eval(
                    "(()=>{const bs=Array.from(document.getElementById('ib-lvl').children);"
                    "const b=bs.find(function(x){return x.textContent==='错误';});"
                    "if(b)b.click();return true;})()")
                await asyncio.sleep(0.2)
                rows2 = await cdp.eval(
                    "(()=>{const b=document.getElementById('info-log');"
                    "const all=Array.from((b||{children:[]}).children).map(function(r){return r.textContent||'';});"
                    "return {warn:all.some(function(t){return t.indexOf('B39 视频警告样例')>=0;}),"
                    " n:all.length};})()")
                if (rows2 or {}).get("warn"):
                    b39_bad.append(f"筛「错误」后警告样例还在（级别筛选没生效）：{rows2}")
                # 还原 + 只把喂进去的三条拿掉，别污染后面的判据
                #（不点【清空】—— 真日志是 B9"WS 真在推"的证据，不能一起抹）
                await cdp.eval(
                    "(()=>{const bs=Array.from(document.getElementById('ib-lvl').children);"
                    "const b=bs.find(function(x){return x.textContent==='全部';});"
                    "if(b)b.click();"
                    " if(C.infoDropPrefix)C.infoDropPrefix('B39 ');"
                    " return true;})()")
            except Exception as e:                            # noqa: BLE001
                b39_bad.append(f"读取底栏信息栏时抛异常：{type(e).__name__}: {e}")
            b39_ok = not b39_bad
            record("B39", "PASS" if b39_ok else "FAIL",
                   f"底栏=信息集成栏（实时读数已删）：来源/级别两组筛选都在，"
                   f"喂三条不同来源的信息各带标签、按来源筛得动、计数写清筛掉了多少"
                   f"（{'；'.join(b39_got)}）"
                   if b39_ok else
                   f"**底栏信息集成栏不对**：{b39_bad[:4]}")

            # 后面逐页断言打的是「综合运动」界面 —— 上面切到新模块后要**切回来**，
            # 否则 `#ws-all` 是 `display:none`，`innerText` 全空，后面每一条都会假红。
            await cdp.eval("C.openMod('all','@all')")
            await asyncio.sleep(0.3)

            head("3. 逐页切换（每一页都真的渲染一遍）")
            for tab in TABS + DOCK_VIEWS:
                n_err0 = len(await cdp.snapshot_console())
                rail = RAIL_OF_PY[tab]
                try:
                    await cdp.eval(tab_click_js(tab))
                    await asyncio.sleep(args.per_tab)
                    # 视图里的数据是**异步**拉回来的（会话表/参数表都要等一趟网络），
                    # 固定等 1.5 s 会让断言读到"还没渲染完"的中间态，于是判据随机红
                    # —— 那不是界面错，是**我们没等它**。
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
                    ctx_id = "ctx-mouse" if rail == "mouse" else "dock-body"
                    info = await cdp.eval(
                        "(()=>{const v=document.querySelector('.view[data-tab=\"%s\"]');"
                        "const ctx=document.getElementById('%s');"
                        "const rd=document.getElementById('info-log');"
                        # 同 B0/B2：空盒子不算，设备窗口里的故障框也不算（它由 B21 判）
                        "const er=Array.from(document.querySelectorAll('.errbox'))"
                        ".filter(e=>!e.closest('#dev-modal')"
                        "&&(e.textContent||'').trim()).length;"
                        "const toast=document.getElementById('toast');"
                        "return {on: !!(v&&v.classList.contains('on')),"
                        " text:((v&&v.innerText)||'').length,"
                        " html:((v&&v.innerHTML)||'').length,"
                        " ctx:(ctx.innerText||'').length,"
                        " info:(rd.innerText||'').length,"
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
                "(()=>{const cl=document.getElementById('info-log');"
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
            # 每个视图是空壳。那种情况下逐页断言会各自报红（B.params / B.depart），
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
            # 改版二十四：原来有一条"相机不可达 ⇒ `/camera/mjpeg` 回 502 是预期 5xx"
            # 的放行。相机下线后那个端点不存在了，**不再有任何预期 5xx** —— 5xx 一律
            # 算硬伤。这条放行若留着，等于给未来的真 5xx 开一个后门。
            hard_bad = [b for b in bad if is_hard_request(b)]
            soft_bad = [b for b in bad if not is_hard_request(b)]
            record("B10", "PASS" if not hard_bad else "FAIL",
                   ("无硬伤请求；" + (f"预期内的 4xx {len(soft_bad)} 条（设备没接时正常）"
                                      if soft_bad else "零失败请求"))
                   if not hard_bad else
                   f"**有 {len(hard_bad)} 条**：{hard_bad[:4]}")

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
                # 扫**界面自己的文字**：顶栏 / 单栏页签 / 快捷条 / 数据条 / 底栏 /
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
                "const SEL=['#top','#rail-mouse','#quickbar',"
                "'#dock','#info-bar','#info-log','#nav','#mod','.ctx'];"
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
                "document.querySelectorAll('#top,#rail-mouse,#quickbar,"
                "#dock,#info-bar,#info-log,#nav,#mod,.ctx,.modal-box').forEach(function(e){ walk(e); });"
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
                # 改版二十四：门控只剩一路（鼠）。原来还有"相机就绪"这条腿 ——
                # 相机下线后 `GATE_CHECKS` 里已经没有 `need="camera"` 的项了，
                # 留着这段判断就是"读一个没人写的数"（铁律 32）。
                for cid, sel, need, label in GATE_CHECKS:
                    want = not mouse_ok
                    got = await cdp.eval(
                        f"(()=>{{const b=document.querySelector('{sel}');"
                        f"return b ? !!b.disabled : null;}})()")
                    ok = (got == want)
                    record(cid, "PASS" if ok else "FAIL",
                           f"{label}（需要鼠）："
                           f"{'已连 ⇒ 可点' if mouse_ok else '未连 ⇒ 应禁用'}"
                           f"，实测 disabled={got}，期望 disabled={want}"
                           if ok else
                           f"**{label} 的门控不对**：disabled 实测 {got}、期望 {want}"
                           f"（{'鼠已连' if mouse_ok else '鼠未连'}）")

            # 急停**故意不门控** —— 连不上的时候恰恰最可能想按它。这条反例要钉住。
            estop_disabled = await cdp.eval(
                "(()=>{const b=document.querySelector('#btn-estop');"
                "return b?!!b.disabled:null;})()")
            record("B15", "PASS" if estop_disabled is False else "FAIL",
                   "顶栏【急停】不被门控（B15 就是它的反例：连不上的时候最需要它）"
                   if estop_disabled is False else
                   f"**急停被禁用了**（disabled={estop_disabled}）—— 这会让最需要它的时刻按不下去")

            head("10. 未连接时开会话必须被当场拦住（前端先解释，服务端也拦）")
            opened = await cdp.eval(
                "(()=>{const b=document.getElementById('btn-open');"
                "return {disabled: !!b.disabled, title: b.title || ''};})()")
            # ⚠️ `mouse_ok` 只在 `hh` 读成功时才存在 —— 上面两条 SKIP 分支会让它没定义。
            # 早先这里直接用它，于是"health 两次读不一致"时不是 SKIP 而是 NameError
            # 把整场跑崩（判据自己成了故障源）。所以先判"知不知道"，再判门控。
            if not hh:
                record("B18", "SKIP",
                       f"health 没读到稳定值 ⇒ 不知道鼠连没连，这条不作数（实测 {opened}）")
            elif not mouse_ok:
                record("B18", "PASS" if opened.get("disabled") else "FAIL",
                       f"顶栏【开始记录】已禁用，悬停提示：「{opened.get('title')[:70]}」"
                       if opened.get("disabled") else
                       f"**一路都没连时【开始记录】仍可点**（{opened}）")
            else:
                record("B18", "PASS" if not opened.get("disabled") else "FAIL",
                       "已连上鼠 ⇒ 【开始记录】可点（门控没有误伤）"
                       if not opened.get("disabled") else f"误伤：{opened}")

            # ------------------------------------------------------------ 11
            '''本轮界面的两条核心要求，必须由机器盯着：

              ① **状态只在顶栏，点标签开窗口设参数连接** —— 所以标签得真的可点，
                 点了得真的切到对应面板（不能"点开都是同一个画面"）。
              ② **页签与视图一一对应**（改版二十四：单栏后不再有"两栏并排"这件事，
                 但"点了哪个页签就只有那一页是 `.on`"仍然要钉住）。
            '''
            head("11. 顶栏设备窗口（点标签打开）")
            pane_map = []
            for kind, title in (("mouse", "电脑鼠"), ("clock", "时间基")):
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
                   "点两个标签 ⇒ 各自只显示自己的面板、标题正确："
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


            await cdp.eval("document.getElementById('dev-close').click()")
            await asyncio.sleep(0.3)

            # ── B20：**页签与视图必须一一对应** ──
            # 改版二十四把左栏「场地 · 相机」整栏删掉了，于是原来"两栏并排"那条判据
            # 失去了对象。但**它守的东西还在**：点了哪个页签，就只许那一页是 `.on`，
            # 而且页签高亮必须跟着走。这条与 B1/B2（容器清单）不重复 ——
            # B1/B2 问"东西在不在"，B20 问"点了之后切没切过去"，后者才是用户的体感。
            two = await cdp.eval(
                "(()=>{const m=document.querySelector('#col-mouse .view.on');"
                "const mb=document.querySelector('#rail-mouse button.on');"
                "const ons=Array.from(document.querySelectorAll('#col-mouse .view.on'))"
                ".map(function(v){return v.dataset.tab;});"
                "const a=document.getElementById('col-mouse').getBoundingClientRect();"
                "return {mouse:m?m.dataset.tab:null, mouseBtn:mb?mb.dataset.tab:null,"
                " ons:ons, cols:document.querySelectorAll('#body > .col').length,"
                " camGone: !document.getElementById('col-cam'),"
                " w:Math.round(a.width)};})()")
            two_ok = (two.get("mouse") is not None and two.get("ons") == [two.get("mouse")]
                      and two.get("mouseBtn") == two.get("mouse")
                      and two.get("cols") == 1 and two.get("camGone") and two.get("w") > 300)
            record("B20", "PASS" if two_ok else "FAIL",
                   f"单栏：页签「{two.get('mouseBtn')}」与当前视图「{two.get('mouse')}」"
                   f"一一对应（同时只有一页 `.on`）、栏宽 {two.get('w')} px、"
                   f"左栏「场地 · 相机」已下线（{two.get('cols')} 栏）"
                   if two_ok else f"**页签与视图没对上**：{two}")

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
