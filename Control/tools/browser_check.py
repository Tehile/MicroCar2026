"""无头浏览器实测（第三版）：九个页面真渲染 + 接缝真的接上了。

    python tools/browser_check.py                 # 默认查 http://127.0.0.1:8770
    python tools/browser_check.py --sim           # 先起上游模拟器并连上（有真数据）
    python tools/browser_check.py --shots .tmp/shots

## 这一版为什么整体重写

改版二十五把前端整体换了（`18c180a`）：旧前端 14 个文件删除，换成
**左轨导航 + 九页面**（`assets/{lib,shell,draw}.js` + `assets/views/*.js`）。
第一版 47 条判据钉在旧 DOM（`#nav-sys` / `#rail-mouse` / `#wm-kv` / `#dev-modal` …）
与旧全局（`C.mods` / `C.parts` / `C.openMod` …）上，**一个都不存在了**，
所以那时它被停用（文件保留，取证方式留在 git 历史里）。这一版按新 DOM 重新落点。

## 判据落点换了（这是重点，不只是换个选择器）

第一版验的是"**界面把后端数据显示对了**"——那个年代前端直连后端。
中间那一段（改版二十五~二十七）前端是**纯原型**，验"数据对不对"没有对象。
接线后（改版二十八 P4）要验的是"**接缝真的接上了**"：
点了按钮，请求到底有没有发出去、发的是不是那一条、参数对不对。

所以这一版的核心是 **V6**：在页面里注入 `fetch` 记录器，点【应用】把每批帧数
设成 3，然后**去请求记录里找** `POST /api/v1/mouse/stream/batch`。
找不到就是没接上——而界面上一句"已下发"是本地 toast，跟网络毫无关系。

## 负向判据（V5）

**设备没连上时，界面不许说连上了。** 假"已连接"会让人以为可以去发车。
`--sim` 与不带 `--sim` 两种情况**断言方向相反**——双向都点才叫判据。

## 为什么不用 Playwright

本机没装，装它要拉一个 ~150 MB 的内核。而 Chrome 本来就有，CDP 又是纯 WebSocket
——`aiohttp`（本项目已有依赖）就能当客户端。少一个依赖，多一条能随时重跑的命令。
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
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from control.store import _rmtree_quiet      # noqa: E402

#: 九个页面。唯一真相在前端 `assets/lib.js` 的 `NAV`；这里是判据里的**期望值**，
#: 不一致就该有人来解释（见 V2）。
VIEWS = ["overview", "live", "params", "race", "maint", "video", "track", "data", "replay"]
#: 左轨四组（同上）
NAV_GROUPS = [
    ("", ["overview"]),
    ("小车控制", ["live", "params", "race", "maint"]),
    ("视频复盘", ["video", "track"]),
    ("记录", ["data", "replay"]),
]

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

RESULTS: list = []


def record(cid: str, verdict: str, note: str) -> None:
    RESULTS.append((cid, verdict, note))
    print(f"  [{verdict:<4}] {cid:<4} {note}")


#: 当前跑到哪一段（全局超时时用它指出**卡在哪**）。见 `main()` 的 wait_for。
CUR = {"head": "(还没开始)", "t": 0.0}


def head(t: str) -> None:
    CUR["head"] = t
    CUR["t"] = time.time()
    print(f"\n── {t} " + "─" * max(4, 74 - len(t)))


# ---------------------------------------------------------------- HTTP 小工具

def _req(url: str, data=None, timeout: float = 10.0):
    body = None if data is None else json.dumps(data).encode()
    r = urllib.request.Request(url, data=body,
                               headers={"Content-Type": "application/json"},
                               method="GET" if data is None else "POST")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}"), resp.status
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8") or "{}"), e.code
        except Exception:                                     # noqa: BLE001
            return {}, e.code


def read_health(base: str) -> dict:
    d, c = _req(base + "/api/v1/health", timeout=8)
    return (d.get("data") or {}) if c == 200 else {}


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

    为什么要一个专用 reader：CDP 的**响应**和**事件**走同一条 WS，只在
    "发完命令后读一条"里做同步等待，就会在事件插进来时错位。所以必须有一个持续的
    读取循环，把响应按 id 分发给 future、把事件攒进列表。
    """

    def __init__(self, session) -> None:
        self.session = session
        self._id = 0
        self._pending: dict = {}
        self.events: list = []

    async def start(self) -> None:
        asyncio.create_task(self._reader())

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

    async def call(self, method: str, params=None, timeout: float = 30.0):
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
        if r.get("exceptionDetails"):
            raise RuntimeError(json.dumps(r["exceptionDetails"], ensure_ascii=False)[:400])
        return (r.get("result") or {}).get("value")

    def exceptions(self) -> list:
        out = []
        for e in self.events:
            if e.get("method") == "Runtime.exceptionThrown":
                d = e.get("params", {}).get("exceptionDetails") or {}
                out.append(((d.get("exception") or {}).get("description")
                            or d.get("text") or "未知异常")[:300])
        return out


#: 注入页面的探针：记录**所有** fetch 与未捕获错误。
#: 判"接缝有没有接上"只能看"请求有没有真的发出去"——界面上一句"已下发"是本地 toast。
PROBE = """
window.__calls = [];
(function () {
  const f = window.fetch;
  if (!f) return;
  window.fetch = function (u, o) {
    try {
      window.__calls.push({ u: String(u), m: ((o && o.method) || 'GET').toUpperCase(),
                            b: (o && o.body) || '' });
    } catch (e) { /* 记录失败不能影响真实请求 */ }
    return f.apply(this, arguments);
  };
})();
window.__errs = [];
window.addEventListener('error', function (e) {
  window.__errs.push(String((e && e.message) || e));
});
"""


async def run(args) -> int:
    import aiohttp

    base = f"http://127.0.0.1:{args.port}"
    head("0. 目标服务")
    try:
        _d, c = _req(base + "/api/v1/health", timeout=5)
        record("S0", "PASS" if c == 200 else "FAIL", f"{base}/api/v1/health → {c}")
        if c != 200:
            return 1
    except Exception as e:                                    # noqa: BLE001
        record("S0", "FAIL", f"{base} 不可达：{e}（先 python app.py）")
        return 1

    # 起点：**只记录**当前连接状态，不主动断开。
    # ⚠️ 以前这里无条件 `disconnect`，理由是要一个"确定起点"。代价是：
    #    所有需要活链路的判据（V9 参数 / V11 按页开门 / V12 实时图 / V13 回放）
    #    全都因为"没连设备"而 SKIP —— 看着像"功能没实现"，其实是检查器自己断的。
    #    现在只有真正需要"未连接"的那条（V10）在它自己那一段里断开。
    head("0a. 起点：记下当前连接状态")
    _hc = read_health(base).get("mouse") or {}
    conn0 = bool(_hc.get("connected"))
    record("S0e", "PASS",
           f"起点：connected={conn0}（{'活链路，需要设备的判据能跑' if conn0 else '未连接，需要设备的判据会 SKIP'}）")

    sim_proc = None
    if args.sim:
        head("0b. 起鼠标模拟器")
        host_dir = ROOT.parent / "RemoteControl" / "host"
        if not (host_dir / "host" / "sim" / "mouse_sim.py").is_file():
            record("S0b", "SKIP", f"上游模拟器不在 {host_dir}")
        else:
            sim_proc = subprocess.Popen(
                [sys.executable, "-u", "-m", "host.sim.mouse_sim",
                 "--port", str(args.sim_port), "--no-auto"],
                cwd=str(host_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace")
            up, t0 = False, time.time()
            # 不要连端口探活：模拟器是单客户端的，探针连上又关会占掉"正在服务的客户端"
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
                _d, c = _req(base + "/api/v1/mouse/connect",
                            {"target": f"tcp://127.0.0.1:{args.sim_port}"}, timeout=30)
                record("S0b", "PASS" if c == 200 else "FAIL",
                       f"模拟器已监听、服务端 connect → {c}")
                await asyncio.sleep(2.0)          # 让遥测先跑几帧（帧率/4 灯才有值）

    proc = None
    profile = None
    try:
        head("1. 起无头浏览器")
        browser = find_browser()
        port = free_port()
        # ⚠️ Chrome profile **不能建在项目里**（`.tmp/bcheck-*`）。
        #    一个 profile 约 283 个文件 / 5 MB，而这个脚本每次跑都新建一个；
        #    实测堆到 **32 个 / 1 万文件 / 262 MB** 之后，`rm -rf .tmp/*`
        #    在 Git Bash 上要 **4 分钟以上**（小文件单次开销大），
        #    于是"清理临时文件"这件事本身变成了一个超时源。
        #    改放系统临时目录：不污染仓库，系统磁盘清理也会收。
        profile = Path(tempfile.mkdtemp(prefix="bcheck-"))
        proc = subprocess.Popen(
            [browser, "--headless=new", f"--remote-debugging-port={port}",
             f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
             "--disable-gpu", "--disable-extensions",
             f"--window-size={args.width},{args.height}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ws_url = None
        for _ in range(60):
            try:
                # ⚠️ 必须取**页面级** target：`/json/version` 给的是**浏览器级** WS，
                # 在它上面 `Page.enable` / `Runtime.evaluate` 一律 -32601（"method not found"）
                # —— 那两个域挂在 page session 上。
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
                    tabs = json.loads(r.read().decode())
                page = next((t for t in tabs if t.get("type") == "page"), None)
                if page and page.get("webSocketDebuggerUrl"):
                    ws_url = page["webSocketDebuggerUrl"]
                    break
            except Exception:                                 # noqa: BLE001
                pass
            time.sleep(0.25)
        if not ws_url:
            record("S1", "FAIL", "浏览器没起来（CDP 连不上）")
            return 1
        record("S1", "PASS", f"浏览器已起（CDP :{port}）")

        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(ws_url, max_msg_size=0) as ws:
                cdp = CDP(ws)
                await cdp.start()
                await cdp.call("Page.enable")
                await cdp.call("Runtime.enable")
                await cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": PROBE})
                await cdp.call("Page.navigate", {"url": base + "/"})
                await asyncio.sleep(args.wait)

                shots = Path(args.shots) if args.shots else None
                if shots:
                    shots.mkdir(parents=True, exist_ok=True)

                # ---------------- V0 外壳
                head("2. 启动：外壳")
                boot = await cdp.eval(
                    "(()=>{const C=window.C,S=C&&C.S;"
                    "return {ok:!!C,live:!!(C&&C.LIVE&&C.LIVE.on),"
                    "route:S?S.route:null,"
                    "nav:document.querySelectorAll('#nav .nav-item').length,"
                    "rail:!!document.getElementById('rail'),"
                    "main:!!document.getElementById('main'),"
                    "dock:!!document.getElementById('dock'),"
                    "log:!!document.getElementById('log'),"
                    "drawer:!!document.getElementById('drawer')};})()")
                ok0 = all(boot.get(k) for k in ("ok", "rail", "main", "dock", "log", "drawer"))
                record("V0", "PASS" if ok0 else "FAIL",
                       f"外壳齐（rail/main/dock/log/drawer）；route={boot.get('route')!r}、"
                       f"nav {boot.get('nav')} 项、LIVE.on={boot.get('live')}")

                # ---------------- V1 九页逐页
                head("3. 九个页面逐页真渲染")
                bad = []
                for vid in VIEWS:
                    await cdp.eval(f"window.C.go('{vid}')")
                    await asyncio.sleep(0.35)
                    r = await cdp.eval(
                        "(()=>{const m=document.getElementById('main');"
                        "const on=document.querySelector('#nav .nav-item.on');"
                        "return {route:window.C.S.route,"
                        " kids:m?m.children.length:0,"
                        " len:m?(m.innerText||'').trim().length:0,"
                        " on:on?on.getAttribute('data-route'):null};})()")
                    ok = (r.get("route") == vid and (r.get("kids") or 0) > 0
                          and (r.get("len") or 0) > 20 and r.get("on") == vid)
                    if not ok:
                        bad.append(f"{vid}(kids={r.get('kids')},len={r.get('len')},"
                                   f"on={r.get('on')})")
                    if shots:
                        shot = await cdp.call("Page.captureScreenshot", {"format": "png"})
                        (shots / f"{vid}.png").write_bytes(
                            base64.b64decode(shot.get("data") or ""))
                record("V1", "PASS" if not bad else "FAIL",
                       "九页都挂上了（内容非空、页签高亮与路由一致）" if not bad
                       else f"这些页没挂上/高亮不对：{'、'.join(bad)}")

                # ---------------- V2 导航结构
                head("4. 导航结构（四组九项）")
                nav = await cdp.eval(
                    "(()=>{const g=Array.from(document.querySelectorAll('#nav .nav-group'));"
                    "return {groups:g.length,"
                    " items:g.map(x=>Array.from(x.querySelectorAll('.nav-item'))"
                    ".map(b=>b.getAttribute('data-route')))};})()")
                want = [g[1] for g in NAV_GROUPS]
                ok2 = nav.get("items") == want
                record("V2", "PASS" if ok2 else "FAIL",
                       f"四组九项与 `NAV` 一致（{nav.get('groups')} 组 / "
                       f"{sum(len(x) for x in (nav.get('items') or []))} 项）" if ok2
                       else f"导航与期望不一致：实际 {nav.get('items')}，期望 {want}")

                # ---------------- V3 未捕获异常
                head("5. 未捕获 JS 异常")
                await cdp.eval("window.C.go('live')")
                await asyncio.sleep(0.4)
                inj = await cdp.eval("window.__errs||[]")
                allerr = cdp.exceptions() + [str(x) for x in (inj or [])]
                record("V3", "PASS" if not allerr else "FAIL",
                       "整轮零未捕获异常" if not allerr
                       else f"{len(allerr)} 条：{allerr[:3]}")

                # ---------------- V4 实时状态页
                head("6. 实时状态页（4 灯 / 帧率 / 通道卡）")
                lv = await cdp.eval(
                    "(()=>{const leds=document.querySelectorAll('#lv-leds .led-s');"
                    "const cls=Array.from(leds).map(x=>x.className.replace('led-s','').trim());"
                    "const rate=document.getElementById('lv-rate');"
                    "return {leds:leds.length,cls:cls,rate:rate?rate.innerText:null,"
                    " chs:document.querySelectorAll('#lv-ch dd').length};})()")
                # 4 灯只判"有四格且标记合法"：亮/灭/未知都行（三态都得能显示），
                # 具体是哪一态取决于固件与通道，不是这一条要管的事。
                ok4 = (lv.get("leds") == 4
                       and all(x in ("on", "off", "unknown") for x in (lv.get("cls") or [])))
                record("V4", "PASS" if ok4 else "FAIL",
                       f"4 灯 {lv.get('leds')} 格、标记 {lv.get('cls')}；"
                       f"帧率大字 {lv.get('rate')!r}；通道卡 {lv.get('chs')} 行")

                # ---------------- V5 连接状态不许造假（负向/正向）
                head("7. 连接状态：界面与后端必须一致")
                api_mouse = read_health(base).get("mouse") or {}
                ui = await cdp.eval(
                    "(()=>{const S=window.C.S;"
                    "const chip=document.querySelector('.devchip[data-dev=mouse]');"
                    "return {conn:S.health.mouse.connected,live:window.C.LIVE.on,"
                    " chip:chip?chip.className:null,"
                    " chipTxt:chip?(chip.innerText||'').trim():null};})()")
                # 设备片的"已连接"标记是 `ok` 类（`devState()` 的约定：ok/warn/bad/unknown）。
                # ⚠️ 不是 `on` —— 一开始按 `on` 判，结果永远 false，判据自己变成假红。
                chip_ok = bool(ui.get("chip") and "ok" in str(ui.get("chip")).split())
                if not ui.get("live"):
                    # 原型模式下连接状态是**演示值**（初始 mock 就写着"已连接"），
                    # 拿它当"界面撒谎"是冤枉 —— 这条判据只对真数据模式有意义。
                    record("V5", "SKIP",
                           f"前端在原型模式（LIVE.on=false）⇒ 连接状态是演示值，不判；"
                           f"界面 conn={ui.get('conn')}、设备片 {ui.get('chipTxt')!r}")
                else:
                    # 判据是**两边一致**，不是"必须未连"。
                    # ⚠️ 以前这里是"起点一律未连"（脚本开头会强制 disconnect），
                    #    于是"连着设备"这一态被判成 FAIL —— 而"连着设备"恰恰是
                    #    现场最常见的一态。真正的静默错是**不一致**：
                    #    后端没连而界面说已连接（假绿灯），或反过来（假红灯）。
                    be = api_mouse.get("connected")
                    ok5 = (bool(ui.get("conn")) == bool(be)) and (chip_ok == bool(be))
                    record("V5", "PASS" if ok5 else "FAIL",
                           f"后端 connected={be}、界面={ui.get('conn')}、"
                           f"设备片标 ok={chip_ok}（{ui.get('chipTxt')!r}）⇒ "
                           f"两边{'一致' if ok5 else '**不一致**'}"
                           + ("" if ok5 else
                              "：假'已连接'比不显示更坏，假'未连接'会让人去查串口"))

                # ---------------- V6 接缝（核心）
                head("8. 接缝：改 N 之后拉取真的按新档开起来了")
                # 改版三十一之后的正确姿势（前端也是这么做的）：
                #   **结束拉取 → 改 N → 开始拉取**。
                # 为什么不能只改 N：拉取中 N 是禁用的，而且开流由后端带档（pace(n)），
                # 光改 N 不开流 = 界面上写着 n=3、设备一帧都不发。
                if not ui.get("live"):
                    record("V6", "SKIP",
                           "前端没进真数据模式（LIVE.on=false）⇒ 接缝无从验起")
                elif not api_mouse.get("connected"):
                    # 没设备时 `stream/batch` 与 `stream` 都会 409，验不出"开流有没有用上新档"
                    record("V6", "SKIP",
                           "没连电脑鼠 ⇒ 开流这一步必然 409，判不了接缝")
                else:
                    await cdp.eval("window.C.go('live')")
                    await asyncio.sleep(0.8)
                    # ① 先停（不管当前开没开，停一次都是幂等的）
                    await cdp.eval(
                        "(()=>{const b=document.getElementById('lv-pull-off');"
                        "if(b&&!b.disabled) b.click();})()")
                    await asyncio.sleep(1.2)
                    await cdp.eval("window.__calls.length=0")
                    # ② 改 N=3 再点【开始拉取】
                    await cdp.eval(
                        "(()=>{const i=document.getElementById('lv-n');"
                        "if(i){i.disabled=false;i.value='3';}"
                        "const b=document.getElementById('lv-pull-on');if(b){b.disabled=false;b.click();}})()")
                    await asyncio.sleep(2.5)
                    calls = await cdp.eval("window.__calls||[]") or []
                    hits_b = [c for c in calls
                              if str(c.get("u", "")).endswith("/api/v1/mouse/stream/batch")]
                    hits_on = [c for c in calls
                               if str(c.get("u", "")).endswith("/api/v1/mouse/stream")]
                    body_ok = False
                    if hits_b:
                        try:
                            body_ok = int(json.loads(hits_b[-1].get("b") or "{}").get("n")) == 3
                        except Exception:                     # noqa: BLE001
                            body_ok = False
                    st6 = await cdp.eval(
                        "(()=>{const S=window.C.S;"
                        "return {n:S.pullN, pulling:S.pulling};})()") or {}
                    # 后端到底开没开，也核一遍（"界面说开了"不算）
                    hh6 = read_health(base).get("mouse") or {}
                    stream_on = hh6.get("stream_on")
                    ok6 = (bool(hits_b) and body_ok and bool(hits_on)
                           and st6.get("n") == 3 and st6.get("pulling") is True
                           and stream_on is True)
                    record("V6", "PASS" if ok6 else "FAIL",
                           f"结束→改 n=3→开始：抓到 {len(hits_b)} 条 `stream/batch`"
                           f"（body.n 对={body_ok}）+ {len(hits_on)} 条 `stream`；"
                           f"界面 n={st6.get('n')} pulling={st6.get('pulling')}；"
                           f"后端 stream_on={stream_on}"
                           + ("" if ok6 else
                              " ⇒ **界面说开了但请求没发/后端没开**，"
                              "或者改 N 之后开流没用上新档（设备按 n=1 发、界面写 3）"))
                    # 还原：停掉，别把环境留在批式档
                    try:
                        _req(base + "/api/v1/mouse/stream", {"on": False}, timeout=20)
                        _req(base + "/api/v1/mouse/stream/batch", {"n": 1}, timeout=20)
                    except Exception:                         # noqa: BLE001
                        pass

                # ---------------- V7 记录页 / 回放页真的用上后端数据
                head("9. 记录与回放：后端有数据时，界面是不是真的用了它")
                rec_api, rc = _req(base + "/api/v1/records?n=50", timeout=15)
                api_rows = ((rec_api.get("data") or {}).get("records") or []) if rc == 200 else []
                api_rows = [r for r in api_rows if not r.get("error")]
                if not ui.get("live"):
                    record("V7", "SKIP", "前端没进真数据模式 ⇒ 记录页无从验起")
                elif not api_rows:
                    record("V7", "SKIP", "记录目录里一条记录都没有 ⇒ 无从验起（不是失败）")
                else:
                    await cdp.eval("window.C.loadRecords()")
                    await asyncio.sleep(1.2)
                    ui_rows = await cdp.eval(
                        "(()=>{const S=window.C.S;"
                        "return {n:S.records.length,"
                        " first:(S.records[0]||{}).id||'',"
                        " ledger0:(S.records[0]||{}).ledger,"
                        " hasNull:S.records.some(r=>r.ledger===null)};})()") or {}
                    # 回放：进页会自己取一次（`needPage`），等到样本到齐
                    await cdp.eval("window.C.go('replay')")
                    got = {}
                    for _ in range(12):
                        await asyncio.sleep(0.5)
                        got = await cdp.eval(
                            "(()=>{const r=window.C.record(window.C.S.recSel);"
                            "return r?{n:(r.samples||[]).length,"
                            " N:((r.maze||{}).N||0),"
                            " sampled:r.sampled||1,"
                            " params:(r.params?r.params.length:null)}:null;})()") or {}
                        if (got.get("n") or 0) > 0:
                            break
                    # 账本三态在界面上的样子（老记录 ledger=null 必须是「判不了」）
                    await cdp.eval("window.C.go('data')")
                    await asyncio.sleep(0.8)
                    led = await cdp.eval(
                        "(()=>{const t=document.getElementById('dt-rows');"
                        "if(!t) return {txt:'', hasBad:false};"
                        "const txt=t.innerText||'';"
                        "return {txt:txt, hasBad:txt.indexOf('判不了')>=0,"
                        " hasDiff:txt.indexOf('差 ')>=0};})()") or {}
                    n_ok = ui_rows.get("n") == len(api_rows)
                    id_ok = (ui_rows.get("first") == (api_rows[0].get("id") or ""))
                    rep_ok = (got.get("n") or 0) > 0 and (got.get("N") or 0) >= 2
                    # 若后端给了 null 账本，界面必须出现「判不了」而不是「差 N 帧」
                    led_ok = (not ui_rows.get("hasNull")) or bool(led.get("hasBad"))
                    ok7 = n_ok and id_ok and rep_ok and led_ok
                    record("V7", "PASS" if ok7 else "FAIL",
                           f"记录页 {ui_rows.get('n')} 行 / 后端 {len(api_rows)} 条"
                           f"（首行 {ui_rows.get('first')!r} 对齐={id_ok}）；"
                           f"回放取到 {got.get('n')} 个样本、迷宫 N={got.get('N')}"
                           f"（抽样间隔 {got.get('sampled')}）；"
                           f"老记录账本显示「判不了」={led.get('hasBad')}"
                           + ("" if ok7 else " ⇒ **界面没用上后端数据，或把'判不了'画成了'差 N 帧'**"))

                # ---------------- V9 参数页真的吃上了后端那 41 项
                head("10. 参数页：41 项四组，派生量只读")
                if not api_mouse.get("connected"):
                    record("V9", "SKIP", "电脑鼠没连 ⇒ 参数端点 409，界面理应没有参数表")
                else:
                    await cdp.eval("window.C.loadParams()")
                    await cdp.eval("window.C.go('params')")
                    got_p = {}
                    for _ in range(10):
                        await asyncio.sleep(0.4)
                        got_p = await cdp.eval(
                            "(()=>{const P=window.C.S.params||[];"
                            "const all=P.reduce((a,g)=>a+(g.items||[]).length,0);"
                            "const der=P.reduce((a,g)=>a+(g.items||[]).filter(i=>i.derived).length,0);"
                            "const sp=P[0]&&P[0].items?P[0].items.filter(i=>i.name==='SPEEDL')[0]:null;"
                            "const rows=document.querySelectorAll('#pp-rows tr').length;"
                            "return {g:P.length,all:all,der:der,rows:rows,"
                            " spVal:sp?sp.value:null,spDer:sp?!!sp.derived:null};})()") or {}
                        if (got_p.get("all") or 0) > 0:
                            break
                    ok9 = (got_p.get("g") == 4 and got_p.get("all") == 41
                           and got_p.get("der") == 2 and got_p.get("spDer") is True
                           and (got_p.get("rows") or 0) > 0)
                    record("V9", "PASS" if ok9 else "FAIL",
                           f"参数表 {got_p.get('g')} 组 / {got_p.get('all')} 项"
                           f"（后端 41 项四组）、派生 {got_p.get('der')} 项、"
                           f"SPEEDL 只读={got_p.get('spDer')} 值={got_p.get('spVal')}、"
                           f"渲染 {got_p.get('rows')} 行"
                           + ("" if ok9 else " ⇒ **参数页没吃上后端数据，或派生量被当成了可写项**"))

                # ---------------- V11 切页不许动流 + 手动停最高（改版四十七重写）
                head("11a. 拉取默认常开：切页不动流；手动停**粘住**（切页也不回弹）")
                # 改版三十一这里验的是"切到不需要帧的页面要停"——那套"按页开门"已经
                # 拆了（见设计方案 §11.47）。现在验**两个方向**，缺一不能算判据：
                #   ① auto 模式（默认）：切到只读盘的 data 页，流**仍要在**（切页不动流）；
                #   ② 人按过【结束】（`pullIntent='off'`）：切回 live 页，流**仍不许开**
                #      —— 手动是最高权限，任何"进页确保开"的逻辑都不许越过它。
                # 只验 ①：会放过"手动被自动盖回去"（正是这次要修的病）。
                # 只验 ②：会放过"切页还在关流"（旧病）。
                if not (ui.get("live") and api_mouse.get("connected")):
                    record("V11", "SKIP", "没连设备（或没进真数据模式）⇒ 拉取常开无从验起")
                else:
                    def _mhpull() -> dict:
                        return (read_health(base).get("mouse") or {})

                    async def _ensure_on(timeout: float = 8.0) -> bool:
                        """把"在拉"这个**前提**做出来（判据本身不依赖上一个组留下的状态）。

                        ⚠️ 为什么要这一步：V6 是"结束→改档→开始"，收尾是**开着**的；
                        但任何一条前面的判据都可能合法地停过流，那时 V11 的 ① 会红，
                        红的原因与本条要验的东西（切页是否关流）无关 —— 那是误报。
                        ⚠️ 而不是"点一下就断言"：开流是**异步**的（POST + 上游 ACK），
                        只等固定 1.5 s 会在慢链路下偶发红。等条件，不等时长。
                        """
                        if bool(_mhpull().get("stream_on")):
                            return True
                        await cdp.eval("window.C.setPull(true, window.C.S.pullN)")
                        t0 = time.monotonic()
                        while time.monotonic() - t0 < timeout:
                            await asyncio.sleep(0.5)
                            if bool(_mhpull().get("stream_on")):
                                return True
                        return False

                    ensured = await _ensure_on()
                    await cdp.eval("window.C.go('live')")
                    await asyncio.sleep(1.5)
                    h_live = _mhpull()
                    on_live = bool(h_live.get("stream_on"))
                    mode_live = str(h_live.get("pull_mode") or "")
                    # ① 切到 data（只读盘、一帧都不需要）⇒ 流不许关
                    await cdp.eval("window.C.go('data')")
                    await asyncio.sleep(1.8)
                    on_data = bool(_mhpull().get("stream_on"))
                    # 参数页顺带验"读表依赖拉取"：41 项是它真的读到上游的证据
                    await cdp.eval("window.C.go('params')")
                    await asyncio.sleep(2.0)
                    on_params = bool(_mhpull().get("stream_on"))
                    pcount = await cdp.eval(
                        "(()=>((window.C.S.params||[]).reduce((a,g)=>a+g.items.length,0)))()")
                    # ② 人工停（比赛页面板上的【结束】）⇒ 粘住
                    await cdp.eval("window.C.go('race')")
                    await asyncio.sleep(2.0)
                    # 面板的【结束】在"本来就没在拉"时是禁用的 ⇒ 先确保开着，
                    # 否则点了个 disabled 按钮，"停住了"这条就是假绿（本来就没在拉）。
                    if not bool(_mhpull().get("stream_on")):
                        await _ensure_on()
                    await cdp.eval("(()=>{const b=document.querySelector("
                                   "'#rc-pull [data-r=pp-off]');if(b&&!b.disabled)b.click();})()")
                    await asyncio.sleep(2.0)
                    h_off = _mhpull()
                    off_stop = bool(h_off.get("stream_on"))
                    mode_off = str(h_off.get("pull_mode") or "")
                    # 再切回 live（"进页确保开"的地方）⇒ 不许自己开回来
                    await cdp.eval("window.C.go('live')")
                    await asyncio.sleep(2.5)
                    h_back = _mhpull()
                    back_on = bool(h_back.get("stream_on"))
                    mode_back = str(h_back.get("pull_mode") or "")
                    ok11 = bool(ensured and on_live and on_data and on_params and (pcount == 41)
                                and (not off_stop) and (mode_off == "off")
                                and (not back_on) and (mode_back == "off"))
                    # 收尾：把人停的那次开回来（后面的判据要帧），并回 auto
                    await cdp.eval("window.C.setPull(true, window.C.S.pullN)")
                    await asyncio.sleep(2.0)
                    record("V11", "PASS" if ok11 else "FAIL",
                           f"先确保在拉={ensured}（mode={mode_live!r}）；"
                           f"live 拉={on_live} → data 拉={on_data}（应为 True，"
                           f"切页不许关流）→ params 拉={on_params}（参数 {pcount} 项）；"
                           f"人工停 ⇒ 拉={off_stop}（应 False）·后端 mode={mode_off!r}；"
                           f"再切回 live ⇒ 拉={back_on}（应 False）·mode={mode_back!r}"
                           + ("" if ok11 else
                              " ⇒ **要么切页还在关流（连上就断、数据要手动开），"
                              "要么手动停被自动逻辑盖回去了**"))

                # ---------------- V12 比赛页的图必须**跟着设备走**（改版三十三）
                head("11c. 比赛页：图是设备给的，而且会随帧增长")
                # 三十二之前这里画的是前端原型那盘静态迷宫：**一进页就有路径**，
                # 所以"图上有内容"根本不能判通过。要验的是：
                #   ① 尺寸等于设备报的 mazetype；② **一段时间后帧数在涨**。
                # 只验①会把"静态原型 + 正确尺寸"放过；只验②会把"每 1.2 s 闪成空白"放过
                # （空白时 frames_total 也是 0 → 增长也可能成立），所以两条一起验。
                if not pick("V12"):
                    record("V12", "SKIP", "--only 未选中")
                elif not (ui.get("live") and api_mouse.get("connected")):
                    record("V12", "SKIP", "没连设备 ⇒ 实时图无从验起")
                else:
                    await cdp.eval("window.C.go('race')")
                    await asyncio.sleep(3.0)
                    m1 = await cdp.eval(
                        "(()=>{const v=window.C.S.mazeView;const S=window.C.S;"
                        "return JSON.stringify({N:v?v.N:null,total:v?v.frames_total:0,"
                        "cov:v?v.covered_s:0,cells:v?Object.keys(v.visited||{}).length:0,"
                        "want:S.mazeN||null});})()")
                    j1 = json.loads(m1 or "{}")
                    await asyncio.sleep(4.0)
                    m2 = await cdp.eval(
                        "(()=>{const v=window.C.S.mazeView;"
                        "return JSON.stringify({N:v?v.N:null,total:v?v.frames_total:0,"
                        "cov:v?v.covered_s:0});})()")
                    j2 = json.loads(m2 or "{}")
                    n_ok = (j1.get("N") is not None and j1.get("N") == j1.get("want"))
                    grew = (j2.get("total") or 0) > (j1.get("total") or 0)
                    ok12 = bool(n_ok and grew)
                    record("V12", "PASS" if ok12 else "FAIL",
                           f"比赛页：N={j1.get('N')}（设备 {j1.get('want')}，一致={n_ok}）、"
                           f"4 s 内 frames_total {j1.get('total')}→{j2.get('total')}"
                           f"（在涨={grew}）、覆盖 {j2.get('cov')} s、已探明 {j1.get('cells')} 格"
                           + ("" if ok12 else
                              " ⇒ **要么画的是原型那盘图（不在跟设备），"
                              "要么帧根本没进来**"))

                # ---------------- V15 比赛页【清空】（改版三十四）
                head("11e. 比赛页【清空】：图归零，然后继续涨")
                # 为什么不能只验"归零"：`reset_live_map` 把清零基线设成**现在**
                # 才对。设成 0 的话，下一轮会把环形缓冲里**上一盘**的 30 秒全喂回来
                # —— 图立刻又满了，而内容来自别的跑法（看起来完全正常）。
                # 所以必须验两步：① 点完立刻是 0；② 再等几秒**能重新涨起来**。
                if not pick("V15"):
                    record("V15", "SKIP", "--only 未选中")
                elif not (ui.get("live") and api_mouse.get("connected")):
                    record("V15", "SKIP", "没连设备 ⇒ 清空无从验起")
                else:
                    await cdp.eval("window.C.go('race')")
                    await asyncio.sleep(2.5)
                    b4 = await cdp.eval(
                        "(()=>{const v=window.C.S.mazeView;return v?(v.frames_total||0):0;})()")
                    await cdp.eval("(()=>{const b=document.getElementById('rc-clear');"
                                   "if(b)b.click();})()")
                    await asyncio.sleep(1.5)
                    z = await cdp.eval(
                        "(()=>{const v=window.C.S.mazeView;"
                        "return JSON.stringify({tot:v?(v.frames_total||0):0,has:!!v});})()")
                    jz = json.loads(z or "{}")
                    await asyncio.sleep(3.5)
                    b5 = await cdp.eval(
                        "(()=>{const v=window.C.S.mazeView;return v?(v.frames_total||0):0;})()")
                    # ⚠️ 判据不能要求"立刻等于 0"：点按钮到读值之间隔了 1.5 s，
                    #    这期间的帧本来就该算进新图（10 Hz × 1.5 s ≈ 15 帧）。
                    #    真正要抓的是"**没清掉**（值还在原地涨）"与
                    #    "**清完立刻被上一盘的旧帧填满**（值回到 b4 附近）"。
                    tot_after = int(jz.get("tot") or 0)
                    zero_ok = (not jz.get("has")) or (
                        tot_after <= 60 and tot_after < max(1, int(b4 or 0)))
                    regrow = int(b5 or 0) > 0
                    ok15 = bool((b4 or 0) > 0 and zero_ok and regrow)
                    record("V15", "PASS" if ok15 else "FAIL",
                           f"清空前 frames_total={b4}；点【清空】后页面 "
                           f"{'无图' if not jz.get('has') else 'frames_total=' + str(jz.get('tot'))}"
                           f"（归零={zero_ok}）；再等 3.5 s 涨到 {b5}（重新累积={regrow}）"
                           + ("" if ok15 else
                              " ⇒ **要么没清掉，要么清完立刻又被上一盘的旧帧填满**"))

                # ---------------- V16 比赛页"已保存…"是**中间态**（改版四十一）
                head("11f. 比赛页：「已保存 · 名 · N 帧」4 s 后回落成「停止并保存」")
                # 为什么单列：原来那一行一旦成立就**永久**停在那里（按钮还是灰的）。
                # 下次要停记录时，按钮上写着上一份的名字 ⇒ 人只能猜"我还能不能停"。
                # 判据必须是两段：① 保存后**确实**出现过"已保存"；② 过 4 s 必须回落。
                # 只验①会把"卡死"放过；只验②会在"从来没显示过"时也通过（假绿）。
                if not pick("V16"):
                    record("V16", "SKIP", "--only 未选中")
                elif not (ui.get("live") and api_mouse.get("connected")):
                    record("V16", "SKIP", "没连设备 ⇒ 造不出记录，按钮状态无从验起")
                else:
                    try:
                        await cdp.eval("window.C.go('race')")
                        await asyncio.sleep(1.5)
                        _req(base + "/api/v1/mouse/maze/record/start",
                             {"why": "browser-check-v16"}, timeout=20)
                        await asyncio.sleep(2.0)
                        _req(base + "/api/v1/mouse/maze/record/stop", {}, timeout=20)
                        # 最多等 5 s 等"已保存"出现（前端 1 s 轮询 + 后端落盘）
                        seen_saved, seen_txt = False, ""
                        for _i in range(10):
                            await asyncio.sleep(0.5)
                            seen_txt = str(await cdp.eval(
                                "(()=>{const b=document.getElementById('rc-recstop');"
                                "return b?b.textContent.trim():'';})()") or "")
                            if "已保存" in seen_txt:
                                seen_saved = True
                                break
                        # 再等 5 s（> 4 s 窗）⇒ 必须回落
                        await asyncio.sleep(5.0)
                        back = json.loads(str(await cdp.eval(
                            "(()=>{const b=document.getElementById('rc-recstop');"
                            "return JSON.stringify({t:b?b.textContent.trim():'',"
                            "d:b?b.disabled:null});})()") or "{}"))
                        ok16 = bool(seen_saved and back.get("t") == "停止并保存")
                        record("V16", "PASS" if ok16 else "FAIL",
                               f"保存后按钮={seen_txt!r}（出现过「已保存」={seen_saved}）；"
                               f"5 s 后={back.get('t')!r}（disabled={back.get('d')}）"
                               + ("" if ok16 else
                                  " ⇒ 要么从没显示过（保存这一下没人知道），"
                                  "要么**卡在「已保存」回不来**：下次要用这个按钮，"
                                  "上面写着上一份的名字、还是灰的"))
                    except Exception as e:                        # noqa: BLE001
                        record("V16", "SKIP", f"造记录时出错（判不了）：{type(e).__name__}: {e}")

                # ---------------- V17 比赛页：手动停 = 最高权限（改版四十七改口径）
                head("11g. 比赛页：手动停之后**谁都不许**把它开回来（发车前后也一样）")
                # 这条守的仍是现场那个循环的另一半：以前"停车/复位后有人替你把流开回来"，
                # 于是 `n≥2` 开流重试 3×2 s 占住命令通道 ⇒ 发不出任何命令。
                # 改版四十七换掉了机制（前端看门狗删了，改成后端 `_pull_mode='off'` 粘住），
                # 判据跟着换**观测点**：不再看"看门狗补不补"，而是看
                #   ① 停了以后**后端 `pull_mode` 真是 'off'**（不是界面上自己写了个 off）；
                #   ② 停住的状态要**扛过发车那条命令链路**（`ensure_pull/suspend/resume`
                #      会碰拉取）—— 所以这里顺带把比赛页切进切出一次再等 ≥4.5 s。
                # ⚠️ 8 s 豁免、`LIVE.pullOffAt` 都已删除（粘住是永久的）⇒ 判据里也不许留。
                if not pick("V17"):
                    record("V17", "SKIP", "--only 未选中")
                elif not (ui.get("live") and api_mouse.get("connected")):
                    record("V17", "SKIP", "没连设备 ⇒ 拉取状态无从验起")
                else:
                    await cdp.eval("window.C.go('race')")
                    await asyncio.sleep(2.5)
                    j1 = json.loads(str(await cdp.eval(
                        "(()=>{const S=window.C.S;"
                        "const h=document.querySelector('#rc-pull .pl-hz b');"
                        "return JSON.stringify({pull:!!S.pulling,hz:h?h.textContent:'',"
                        "hasOn:!!document.querySelector('#rc-pull [data-r=pp-on]'),"
                        "hasOff:!!document.querySelector('#rc-pull [data-r=pp-off]')});})()")
                        or "{}"))
                    # 前面的组可能已经停掉 ⇒ 先手动开起来再验"停"
                    if not j1.get("pull"):
                        await cdp.eval("(()=>{const b=document.querySelector("
                                       "'#rc-pull [data-r=pp-on]');if(b&&!b.disabled)b.click();})()")
                        await asyncio.sleep(2.5)
                        j1["pull"] = bool(await cdp.eval("window.C.S.pulling"))
                    await cdp.eval("(()=>{const b=document.querySelector("
                                   "'#rc-pull [data-r=pp-off]');if(b&&!b.disabled)b.click();})()")
                    await asyncio.sleep(2.0)
                    st2 = bool(await cdp.eval("window.C.S.pulling"))
                    m2 = str(((read_health(base).get("mouse") or {}).get("pull_mode") or ""))
                    # 切走再回来（"进页确保开"最可能越权的地方）后仍要停着
                    await cdp.eval("window.C.go('live')")
                    await asyncio.sleep(1.5)
                    await cdp.eval("window.C.go('race')")
                    await asyncio.sleep(3.0)
                    st3 = bool(await cdp.eval("window.C.S.pulling"))
                    m3 = str(((read_health(base).get("mouse") or {}).get("pull_mode") or ""))
                    ok17 = bool(j1.get("hasOff") and j1.get("pull")
                                and st2 is False and st3 is False
                                and m2 == "off" and m3 == "off")
                    # 收尾：开回来（后面的组要帧），并把 `pull_mode` 回 AUTO
                    await cdp.eval("window.C.setPull(true, window.C.S.pullN)")
                    await asyncio.sleep(2.0)
                    record("V17", "PASS" if ok17 else "FAIL",
                           f"面板={'有' if j1.get('hasOn') and j1.get('hasOff') else '缺按钮'}"
                           f"·Hz={j1.get('hz')!r}；拉取 进页={j1.get('pull')} → "
                           f"手动停后={st2}（后端 mode={m2!r}）→ 切走再回比赛页等 3 s="
                           f"{st3}（mode={m3!r}）"
                           + ("" if ok17 else
                              " ⇒ **要么进页没开（比赛页整页读不到帧），"
                              "要么停不住：有谁又把拉取开回来了 / 后端根本没记住 `off`**"
                              " —— 正是现场那个"
                              "「不停弹开始拉流、发不出命令」的循环"))

                # ---------------- V18 发车状态：命令结果 vs 旧帧（改版四十二/四十三）
                head("11h. 发车状态：命令刚回来时不被旧帧推翻，窗口过后交回帧")
                # 为什么单列：这就是"收车成功 → 已发车 闪成 开始发车 → 又变回 已发车"。
                # 收车那一刻手上还压着**停车前采的帧**（动作码仍是"直行"），它一到
                # 就把按钮翻回去；而流随即停掉 ⇒ 再没有帧来纠正 ⇒ **永久停在错的**。
                # ⚠️ 改版四十三把判据从 `mazeTask` 换成了 `motionTask`（动作码）：
                #    迷宫流程态在车停稳后可能仍停在高位，用它判"在不在跑"会让发车键
                #    一直灰着（现场："任务机 WAIT 了，发车键还是灰的"）。
                #    这条判据两个方向都验：
                #      ① 钉住窗口内：旧帧（动作=直行）不许把 `running` 翻成 true；
                #      ② 窗口过后：同一帧说了算（必须 true，且钉已清）。
                #    只验①会放过"钉永远不会过期"（永远收不了车）；
                #    只验②会放过"根本没钉"（那正是当时的 bug）。
                if not pick("V18"):
                    record("V18", "SKIP", "--only 未选中")
                else:
                    try:
                        j18 = json.loads(str(await cdp.eval(
                            "(()=>{const C=window.C,S=C.S;"
                            "const bak={r:S.running,m:S.tel.motionTask,p:S.runPin};const o={};"
                            "C.pinRunning(false);S.tel.motionTask=1;C.applyRunning();"
                            "o.pinned=S.running;"
                            "S.runPin.at-=6000;C.applyRunning();"
                            "o.expired=S.running;o.cleared=(S.runPin===null);"
                            "S.running=bak.r;S.tel.motionTask=bak.m;S.runPin=bak.p;"
                            "return JSON.stringify(o);})()") or "{}"))
                        ok18 = (j18.get("pinned") is False and j18.get("expired") is True
                                and j18.get("cleared") is True)
                        record("V18", "PASS" if ok18 else "FAIL",
                               f"钉住期间旧帧(动作=直行)⇒running={j18.get('pinned')!r}"
                               f"（应为 False）；窗口过后⇒{j18.get('expired')!r}"
                               f"（应为 True）、钉已清={j18.get('cleared')}"
                               + ("" if ok18 else
                                  " ⇒ 要么没钉住（收车后会被停车前那一帧翻回「已发车」，"
                                  "而流停了再没人纠正）；要么钉不过期（永远收不了车）"))
                    except Exception as e:                        # noqa: BLE001
                        record("V18", "SKIP", f"驱动判定函数出错（判不了）：{type(e).__name__}: {e}")

                # ---------------- V19 收车/复位：点下去**当拍**要有反馈（改版四十三）
                head("11i. 收车：点击 300 ms 内进 pending，确认后收尾且**流一直开着**")
                # 为什么单列：探针实测（2026-10-26）旧版点击后 **2 s 内按钮文案、
                # `disabled`、hint 一个字都不变** —— 因为界面在等"命令 3 s + 等新帧
                # 2.5 s"那条最长 5.5 s 的静默链。现场就是"点了没反应，只好再点一遍"。
                # ⚠️ 这里**把 `C.act` 打桩成 1.2 s 后才 resolve**（只桩网络，不桩界面）：
                #    真机/模拟器上命令可能 30 ms 就回来，300 ms 时已经进观察窗 ——
                #    那样这条会飘。桩住之后"cmd 阶段"是确定的，验的是**界面自己**。
                # ⚠️ 两个方向都要：只验"出现过收车中…"会放过"永远停在那里"，
                #    所以后半段放开动作码，验它**会自己收尾**。
                # ⚠️ 改版四十七：**收车不再停流**（拉取默认常开）。所以后半段断言的
                #    是"流**还在**" —— 旧版这里断言 `pull is False`，那是旧机制的产物。
                #    这一位反过来也更有意义：收车要读任务机，帧先停了就永远读不到，
                #    "收完车顺手把流关掉"正是那个坑的入口（§11.42 的教训）。
                if not pick("V19"):
                    record("V19", "SKIP", "--only 未选中")
                elif not (ui.get("live") and api_mouse.get("connected")):
                    record("V19", "SKIP", "没连设备 ⇒ 比赛页按钮是禁用的，验不了")
                else:
                    try:
                        await cdp.eval("window.C.go('race')")
                        await asyncio.sleep(2.0)
                        # 先把流开起来（"确认后流仍在"要能观察到对象）
                        await cdp.eval("(()=>{const b=document.querySelector("
                                       "'#rc-pull [data-r=pp-on]');if(b&&!b.disabled)b.click();})()")
                        await asyncio.sleep(2.0)
                        await cdp.eval(
                            "(()=>{const C=window.C;window.__act=C.act;"
                            "C.act=function(n){if(n!=='rescue'&&n!=='mazeStop')return window.__act(n);"
                            "return new Promise(function(res){setTimeout(function(){"
                            "res({ok:true,status:'OK',reliable:null,waited:true,fresh:false});},1200);});};"
                            "document.querySelector('#rc-mstop').click();})()")
                        await asyncio.sleep(0.3)          # 只等 300 ms
                        j19a = json.loads(str(await cdp.eval(
                            "(()=>{const q=s=>document.querySelector(s);"
                            "const mb=q('#rc-mstop'),run=q('#rc-run'),h=q('#rc-hint');"
                            "return JSON.stringify({txt:mb?mb.textContent:'',dis:mb?mb.disabled:null,"
                            "runDis:run?run.disabled:null,hint:h?h.textContent:'',"
                            "pend:!!(window.C.VIEWS.race._pend)});})()") or "{}"))
                        # 放开判据：动作码报"空闲" ⇒ 观察窗应当确认收车并收尾（但**不停流**）
                        await cdp.eval("(()=>{window.C.S.tel.motionTask=0;})()")
                        await asyncio.sleep(2.4)
                        j19b = json.loads(str(await cdp.eval(
                            "(()=>{const C=window.C,q=s=>document.querySelector(s);"
                            "if(window.__act)C.act=window.__act;"
                            "return JSON.stringify({txt:q('#rc-mstop')?q('#rc-mstop').textContent:'',"
                            "pend:!!C.VIEWS.race._pend,pull:!!C.S.pulling});})()") or "{}"))
                        ok19 = (("收车中" in str(j19a.get("txt")))
                                and j19a.get("dis") is True and j19a.get("runDis") is True
                                and j19a.get("pend") is True
                                and ("收车中" not in str(j19b.get("txt")))
                                and j19b.get("pend") is False
                                and j19b.get("pull") is True)     # 四十七：不许停流
                        record("V19", "PASS" if ok19 else "FAIL",
                               f"300 ms 时：按钮={j19a.get('txt')!r}、disabled={j19a.get('dis')}、"
                               f"发车键 disabled={j19a.get('runDis')}、hint={j19a.get('hint')!r}；"
                               f"确认后：按钮={j19b.get('txt')!r}、pending={j19b.get('pend')}、"
                               f"拉取={j19b.get('pull')}（应 True：收车不停流）"
                               + ("" if ok19 else
                                  " ⇒ 要么点下去没有即时反馈（现场是「点了没反应」），"
                                  "要么确认之后不收尾（观察窗卡死），"
                                  "要么**收完车把流也关了**（下一帧永远读不到）"))
                    except Exception as e:                        # noqa: BLE001
                        record("V19", "SKIP", f"驱动收车界面出错（判不了）：{type(e).__name__}: {e}")

                # ---------------- V20 断开 ⇒ 读数归零（改版四十三）
                head("11j. 断开之后：帧率写 0、曲线清空，不许冻在最后一帧")
                # 为什么单列：实测（2026-10-26）停流后 `hz2` **恒等于 10.41 不动**，
                # 而帧龄从 406 ms 一路涨到 8069 ms —— 因为 `applyTelemetry` 只在有值时
                # 覆盖，`applyHealth` 又从不重置 ⇒ 读数**冻在最后一帧**上。
                # 现场看到的不是"过期"，而是"界面卡住了"。
                # ⚠️ 必须同时验两件事：`resetTelemetry` 清干净 + **下降沿真的会调它**
                #    （只验前者会放过"没人调"；只验后者会放过"少清几项"）。
                if not pick("V20"):
                    record("V20", "SKIP", "--only 未选中")
                elif not ui.get("live"):
                    record("V20", "SKIP", "没进真数据模式 ⇒ 无从验起")
                else:
                    try:
                        await cdp.eval("window.C.go('live')")
                        await asyncio.sleep(1.6)
                        j20 = json.loads(str(await cdp.eval(
                            "(()=>{const C=window.C,S=C.S;const o={};"
                            "o.before={hz2:S.tel.hz2,hist:S.hist.rate.length};"
                            # 先塞一张"上一段的迷宫图"，才**验得出**它会被清掉
                            # （本来就 null 的话，"清空"这条恒真 —— 假绿）
                            "S.mazeView={N:8};"
                            # 下降沿：喂一份"未连接"的 health（真值源就是它）
                            "C.applyHealth({mouse:{connected:false}});"
                            "o.after={hz2:S.tel.hz2,hist:S.hist.rate.length,"
                            "mt:S.tel.motionTask,running:S.running,maze:S.mazeView,"
                            "telRxAt:S.telRxAt,pos:S.tel.posX};"
                            "C.VIEWS.live.paint(true);"
                            "o.rateTxt=(document.querySelector('#lv-rate')||{}).textContent;"
                            "o.noteTxt=(document.querySelector('#lv-rate-note')||{}).textContent;"
                            "return JSON.stringify(o);})()") or "{}"))
                        b = j20.get("before") or {}
                        a = j20.get("after") or {}
                        if b.get("hz2") is None:
                            record("V20", "SKIP", "断开前就没有帧率（没在拉）⇒ 判不了清零")
                        else:
                            ok20 = (a.get("hz2") is None and a.get("hist") == 0
                                    and a.get("mt") == 0 and a.get("running") is False
                                    and a.get("maze") is None and a.get("telRxAt") == 0
                                    and a.get("pos") is None
                                    and str(j20.get("rateTxt")).strip() in ("0.0", "0"))
                            record("V20", "PASS" if ok20 else "FAIL",
                                   f"断开前 hz2={b.get('hz2')}（曲线 {b.get('hist')} 点）→ "
                                   f"断开后 hz2={a.get('hz2')}、曲线 {a.get('hist')} 点、"
                                   f"动作码={a.get('mt')}、running={a.get('running')}、"
                                   f"迷宫图={a.get('maze')}；界面帧率={j20.get('rateTxt')!r}、"
                                   f"脚注={j20.get('noteTxt')!r}"
                                   + ("" if ok20 else
                                      " ⇒ 断开之后读数还冻在最后一帧上（帧率不动、曲线还在），"
                                      "现场看起来像界面卡住而不是链路没了"))
                        # 收尾：把真 health 拉回来（下一条判据还要用）
                        await cdp.eval("fetch('/api/v1/health').then(r=>r.json())"
                                       ".then(j=>window.C.applyHealth(j.data))")
                        await asyncio.sleep(1.2)
                    except Exception as e:                        # noqa: BLE001
                        record("V20", "SKIP", f"驱动断开清零出错（判不了）：{type(e).__name__}: {e}")


                # ---------------- V13/V14 记录的回放与删除（改版三十三）
                head("11d. 记录：手动开的也能回放，删除点两次才删一条")
                # 造一条**手动开的**记录（`launch=false`）：以前前端拿 `launch` 当
                # "有没有位置"，这种记录回放按钮就是灰的。造完立刻验、验完就删。
                made = ""
                if not pick("V13", "V14"):
                    record("V13", "SKIP", "--only 未选中")
                    record("V14", "SKIP", "--only 未选中")
                elif not ui.get("live"):
                    record("V13", "SKIP", "没进真数据模式 ⇒ 记录页无从验起")
                    record("V14", "SKIP", "同上")
                else:
                    try:
                        # **不传 `auto_pull:false`**：让它自己把拉取开起来 ——
                        # 这正是人点【开始记录】的路径。传 false 的话流是关的，
                        # 0 帧 ⇒ 记录器不落盘 ⇒ 造不出记录，V13/V14 只能 SKIP
                        # （"没造出来"和"功能坏了"看起来一样，这是判据自己骗自己）。
                        _req(base + "/api/v1/mouse/maze/record/start",
                             {"why": "browser-check"}, timeout=20)
                        await asyncio.sleep(2.2)
                        # ⚠️ `_req` 回的是 **(dict, code) 二元组**，不是 dict。
                        #    当 dict 用会 AttributeError，被下面的 except 吞成
                        #    "没造出来" —— 于是 V13/V14 永远 SKIP，而人以为
                        #    "回放功能没实现"（判据自己把自己关掉了）。
                        sp, sp_code = _req(base + "/api/v1/mouse/maze/record/stop",
                                           {}, timeout=20)
                        made = str(((sp.get("data") or {}).get("name")) or "")
                        if sp_code != 200:
                            log_line = f"record/stop HTTP {sp_code}"
                            print("    " + log_line)
                    except Exception:                            # noqa: BLE001
                        made = ""
                    await cdp.eval("window.C.go('data')")
                    await asyncio.sleep(2.5)
                    has = await cdp.eval(
                        "(()=>{const S=window.C.S;const i=(S.records||[]).findIndex(r=>r.name==="
                        + json.dumps(made) + ");return i;})()")
                    if not made or has is None or has < 0:
                        record("V13", "SKIP", f"这条记录没造出来（{made!r}）⇒ 回放无从验起")
                        record("V14", "SKIP", "同上")
                    else:
                        await cdp.eval(f"(()=>{{const t=document.querySelectorAll('#view tbody tr')"
                                       f"[{has}];if(t)t.click();}})()")
                        await asyncio.sleep(1.0)
                        btn = await cdp.eval(
                            "(()=>{const b=document.querySelector('[data-rp=go]');"
                            "return JSON.stringify({has:!!b,dis:b?b.disabled:null});})()")
                        jb = json.loads(btn or "{}")
                        await cdp.eval("(()=>{const b=document.querySelector('[data-rp=go]');"
                                       "if(b&&!b.disabled)b.click();})()")
                        await asyncio.sleep(3.5)
                        rp = await cdp.eval(
                            "(()=>{const S=window.C.S;const r=window.C.record(S.recSel);"
                            "const cv=document.getElementById('rp-maze');let ink=0;"
                            "if(cv){const g=cv.getContext('2d');"
                            "const d=g.getImageData(0,0,cv.width,cv.height).data;"
                            "for(let i=0;i<d.length;i+=4){"
                            "if(d[i]<200||d[i+1]<200||d[i+2]<200)ink++;}}"
                            "return JSON.stringify({samples:r?(r.samples||[]).length:null,"
                            "N:r&&r.maze?r.maze.N:null,launch:r?r.launch:null,ink:ink,"
                            "banner:((document.querySelector('#view .banner'))||{}).textContent||''});})()")
                        jr = json.loads(rp or "{}")
                        ok13 = bool((jb.get("dis") is False) and (jr.get("samples") or 0) > 0
                                    and (jr.get("ink") or 0) > 0
                                    and not str(jr.get("banner") or "").strip())
                        record("V13", "PASS" if ok13 else "FAIL",
                               f"手动开的记录 {made}：回放按钮 disabled={jb.get('dis')}"
                               f"（应 False）、samples={jr.get('samples')}、N={jr.get('N')}、"
                               f"墨迹={jr.get('ink')} 像素、报错={jr.get('banner')!r}"
                               + ("" if ok13 else
                                  " ⇒ **要么按钮还是灰的（拿 launch 当门），"
                                  "要么回放页挂了**"))

                        # V14：删除的两步确认
                        await cdp.eval("window.C.go('data')")
                        await asyncio.sleep(2.2)
                        await cdp.eval(f"(()=>{{const t=document.querySelectorAll('#view tbody tr')"
                                       f"[{has}];if(t)t.click();}})()")
                        await asyncio.sleep(0.8)
                        first_before = await cdp.eval("(window.C.S.records[0]||{}).id")
                        await cdp.eval("window.__calls.length=0")
                        await cdp.eval("(()=>{const b=document.querySelector('[data-rp=del]');"
                                       "if(b)b.click();})()")
                        await asyncio.sleep(1.3)      # 跨过一次 1 s 轮询
                        s1 = await cdp.eval(
                            "(()=>{const b=document.querySelector('[data-rp=del]');"
                            "return JSON.stringify({txt:b?b.textContent:'',"
                            "calls:(window.__calls||[]).filter(c=>c.m==='DELETE').length});})()")
                        j1s = json.loads(s1 or "{}")
                        await cdp.eval("(()=>{const b=document.querySelector('[data-rp=del]');"
                                       "if(b)b.click();})()")
                        await asyncio.sleep(2.5)
                        dels = await cdp.eval(
                            "(window.__calls||[]).filter(c=>c.m==='DELETE').map(c=>c.u)") or []
                        gone = await cdp.eval(
                            "(()=>{const S=window.C.S;return (S.records||[]).some(r=>r.name==="
                            + json.dumps(made) + ");})()")
                        first_after = await cdp.eval("(window.C.S.records[0]||{}).id")
                        step1_ok = ("确认" in str(j1s.get("txt"))) and int(j1s.get("calls") or 0) == 0
                        ok14 = bool(step1_ok and len(dels) == 1 and (gone is False)
                                    and first_after != first_before)
                        record("V14", "PASS" if ok14 else "FAIL",
                               f"删除：点一次只有文案变化={step1_ok}"
                               f"（文本 {j1s.get('txt')!r}、DELETE {j1s.get('calls')} 条）；"
                               f"点两次发了 {len(dels)} 条 DELETE、列表里还在={gone}、"
                               f"首行 {first_before}→{first_after}"
                               + ("" if ok14 else
                                  " ⇒ **要么一次点击就删了（没确认），要么删完列表不更新**"))

                # ---------------- V22 回放的发车/收车标记（改版四十四）
                head("11e. 回放：发车/收车标在「车真的动/停」那一刻，判不了就不画")
                # 为什么单列：这两根刻度原来是**两个假量** ——
                #   发车取 `launch_at_s`（对发车自动开录的记录恒 0.0 ⇒ 永远贴最左端），
                #   收车硬编码 `p:1`（永远在末尾）。实测 `20261008_150828_mz7`：
                #   车 0.44 s 动、4.18 s 停住，记录却到 173.1 s ⇒ 收车刻度错了 169 s。
                # 两方向都验：
                #   ① 有运行区间 ⇒ 两根线**在**算出来的位置上，标签写「发车/收车 + 时刻」；
                #   ② 把 `phases` 摘掉（模拟没有动作码通道的老记录）⇒ 一根都不许画，
                #      且必须给出「判不了」。只验 ① 会放过「判不了时照样画两根假的」。
                if not pick("V22"):
                    record("V22", "SKIP", "--only 未选中")
                elif not ui.get("live"):
                    record("V22", "SKIP", "没进真数据模式 ⇒ 回放无从验起")
                else:
                    try:
                        await cdp.eval("window.C.go('data')")
                        await asyncio.sleep(1.4)
                        j22 = json.loads(str(await cdp.eval(
                            "(async()=>{const C=window.C,S=C.S;"
                            # 逐条取回放，找一条**真的动过**的：跑过才有运行区间可验。
                            # （随便挑第一条的话，读到一条「全程没动」的记录会误判成功能坏了）
                            "let id=null;"
                            "for(let k=0;k<8&&k<S.records.length;k++){"
                            "const r=S.records[k];if(!r)continue;"
                            "await C.loadReplay(r.id);"
                            "const rr=C.record(r.id);"
                            "if(rr&&rr.runFrom!=null&&rr.dur){id=rr.id;break;}}"
                            "if(!id)return JSON.stringify({none:true});"
                            "S.recSel=id;C.go('replay');"
                            "await new Promise(r=>setTimeout(r,1300));"
                            "const rec=C.record(S.recSel);"
                            "const st=document.querySelector('#rp-marks .mk-start');"
                            "const en=document.querySelector('#rp-marks .mk-end');"
                            "const o={id:rec.id,dur:rec.dur,runFrom:rec.runFrom,runTo:rec.runTo,"
                            "st:!!st,en:!!en,stLeft:st?st.style.left:'',enLeft:en?en.style.left:'',"
                            "stTxt:st?st.textContent:'',enTxt:en?en.textContent:'',"
                            "info:(document.querySelector('#rp-info')||{}).textContent||''};"
                            # 反向：摘掉 phases ⇒ 一根标记都不许留，且必须写「判不了」
                            # ⚠️ 直接调 `paintMarks()`，**不要**重挂页面：重挂会顺带
                            #    重新拉记录，刚摘掉的 phases 立刻被后端数据盖回来
                            #    （这条判据自己踩过一次 ⇒ 那时改成 C.go 之后恒红）。
                            "const saved=rec.phases;rec.phases=null;"
                            "C.VIEWS.replay.paintMarks();"
                            "o.unk=!!document.querySelector('#rp-marks .mk-unk');"
                            "o.st2=!!document.querySelector('#rp-marks .mk-start');"
                            "o.en2=!!document.querySelector('#rp-marks .mk-end');"
                            "rec.phases=saved;C.VIEWS.replay.paintMarks();"
                            "return JSON.stringify(o);})()") or "{}"))
                        if j22.get("none"):
                            record("V22", "SKIP", "前 8 条记录里没有一条动过 ⇒ 无运行区间可验")
                        else:
                            def _p(sv):
                                """'12.34%' → 12.34；取不到给 None（**不是 0**）"""
                                try:
                                    return float(str(sv).replace("%", ""))
                                except (TypeError, ValueError):
                                    return None
                            _dur = max(1e-6, j22.get("dur") or 1)
                            _to = (j22.get("runTo") if j22.get("runTo") is not None
                                   else j22.get("runFrom")) or 0
                            exp_a = (j22.get("runFrom") or 0) / _dur * 100
                            exp_b = _to / _dur * 100
                            pa, pb = _p(j22.get("stLeft")), _p(j22.get("enLeft"))
                            pos_ok = (pa is not None and pb is not None
                                      and abs(pa - exp_a) <= 1.0 and abs(pb - exp_b) <= 1.0)
                            txt_ok = (str(j22.get("stTxt") or "").startswith("发车")
                                      and str(j22.get("enTxt") or "").startswith("收车"))
                            info_ok = ("发车" in str(j22.get("info"))
                                       and "收车" in str(j22.get("info")))
                            unk_ok = bool(j22.get("unk") and not j22.get("st2")
                                          and not j22.get("en2"))
                            ok22 = bool(j22.get("st") and j22.get("en") and pos_ok
                                        and txt_ok and info_ok and unk_ok)
                            record("V22", "PASS" if ok22 else "FAIL",
                                   f"{j22.get('id')}（时长 {j22.get('dur')} s、发车 "
                                   f"{j22.get('runFrom')} → 收车 {j22.get('runTo')} s）："
                                   f"发车线在 {j22.get('stLeft')!r}（应 {exp_a:.2f}%）、"
                                   f"收车线在 {j22.get('enLeft')!r}（应 {exp_b:.2f}%）、"
                                   f"标签 {j22.get('stTxt')!r}/{j22.get('enTxt')!r}；"
                                   f"摘掉 phases ⇒ 剩余标记={j22.get('st2')}/{j22.get('en2')}"
                                   f"（应 False/False）、写了「判不了」={j22.get('unk')}"
                                   + ("" if ok22 else
                                      " ⇒ 要么刻度不在车真的动/停那一刻"
                                      "（旧实现：发车恒 0%、收车恒 100%），"
                                      "要么判不了的时候照样画了两根假刻度"))
                    except Exception as e:                        # noqa: BLE001
                        record("V22", "SKIP",
                               f"驱动回放标记出错（判不了）：{type(e).__name__}: {e}")

                # ---------------- V21 记录列表的滚动续载（改版四十五）
                head("11k. 记录列表：一屏 50 条，滚到底自己续，不许说「就这么多」")
                # 为什么单列：列表原来**恒取 50 条就到底** —— 目录里有 300 多条时，
                # 界面不会说"还有 273 条没显示"，用户会以为记录只有 50 份。
                # 改成滚动续载之后，新的假话换了个方向：**未载全时报"共 3 条发车"**
                # （那只数了 50 行）。所以这条判据四头都钉：
                #   ① 首屏 50 条 + 文案必须写"已载 50 / 共 N"（未载全就得出这两个数）；
                #   ② **搜索一条只在第 51 条之后的记录 ⇒ 自动载全并命中**
                #      （只在已载的 50 条里搜，"搜不到"会被读成"没有这条记录"——假否定）；
                #   ③ 滚到底 ⇒ 行数长到 N，且**没有重复 id**（翻页错位的唯一可见症状）；
                #   ④ 载全之后提示行清空（`#dt-more` 空文本 ⇒ CSS `:empty` 收掉）。
                # `N ≤ 50` 的库里验不到续载 ⇒ SKIP（如实标，不硬过）。
                if not pick("V21"):
                    record("V21", "SKIP", "--only 未选中")
                elif not ui.get("live"):
                    record("V21", "SKIP", "没进真数据模式 ⇒ 列表续载无从验起")
                else:
                    try:
                        rec_cnt, crc = _req(base + "/api/v1/records?n=1", timeout=20)
                        total = (rec_cnt.get("data") or {}).get("total") if crc == 200 else None
                        if not total or int(total) <= 50:
                            record("V21", "SKIP",
                                   f"全库 {total} 条（≤50）⇒ 一屏就装得下，验不到续载（不是失败）")
                        else:
                            tot = int(total)
                            # 取**全库最后一条**的 id 当搜索词：它一定不在首屏 50 条里。
                            # （绝不硬编一个名字：记录目录是活的，写死的词迟早不存在）
                            _lr, _lrc = _req(
                                base + f"/api/v1/records?n=1&offset={tot - 1}", timeout=20)
                            _rows = ((_lr.get("data") or {}).get("records") or []) if _lrc == 200 else []
                            kw = str((_rows[0] or {}).get("id") or "") if _rows else ""
                            if not kw:
                                kw = "zz_never_exists"
                            await cdp.eval("window.C.go('data')")
                            await asyncio.sleep(1.0)
                            j21 = json.loads(str(await cdp.eval(
                                "(async()=>{const C=window.C,S=C.S;const o={};"
                                # 复位成"刚进页"的样子：清空已载窗口再取一次（非续页）
                                # ⇒ 只拿一页（REC_PAGE）。
                                "const reset=async()=>{S.records.length=0;await C.loadRecords();"
                                "await new Promise(r=>setTimeout(r,600));};"
                                "await reset();"
                                "o.first=S.records.length;o.total=S.recTotal;"
                                "o.cnt=(document.querySelector('#dt-count')||{}).textContent||'';"
                                "o.more=(document.querySelector('#dt-more')||{}).textContent||'';"
                                "o.sel=(document.querySelector('#dt-sel')||{}).textContent||'';"
                                # ② 搜索：词只在第 51 条之后 ⇒ 必须先自动载全再筛。
                                # ⚠️ `loadRecords('all')` **不 emit('state')**（不整页重挂），
                                #    所以这期间 `#dt-q` 不会被重建成新元素 —— 若哪天改成 emit，
                                #    这里拿到的就是已经脱离文档的老 input（写进去界面没反应）。
                                "let qEl=document.querySelector('#dt-q');"
                                f"qEl.value={json.dumps(kw, ensure_ascii=False)};"
                                "qEl.dispatchEvent(new Event('input'));"
                                "await new Promise(r=>setTimeout(r,1200));"
                                "o.qLoaded=S.records.length;"
                                "const qr=document.querySelector('#dt-rows tr[data-id]');"
                                "o.qRows=document.querySelectorAll('#dt-rows tr[data-id]').length;"
                                "o.qHit=qr?qr.getAttribute('data-id'):'';"
                                "qEl=document.querySelector('#dt-q');"
                                "qEl.value='';qEl.dispatchEvent(new Event('input'));"
                                "await new Promise(r=>setTimeout(r,500));"
                                "await reset();"
                                "const sc=document.querySelector('.dt-list-scroll');"
                                "if(!sc)return JSON.stringify({noScroll:true});"
                                "let guard=0;"
                                "while(S.records.length<S.recTotal&&guard++<40){"
                                "  sc.scrollTop=sc.scrollHeight;"
                                # 派真事件而不是直接调 onScroll()：要验的是"滚动确实接上了"
                                # （setter 触发的原生事件不去重也会派一次，`_more` 闸挡住重入）
                                "  sc.dispatchEvent(new Event('scroll'));"
                                "  await new Promise(r=>setTimeout(r,450));}"
                                "o.loop=guard;"
                                "o.loaded=S.records.length;"
                                "o.uniq=new Set(S.records.map(r=>r.id)).size;"
                                "o.rows=document.querySelectorAll('#dt-rows tr[data-id]').length;"
                                "o.cnt2=(document.querySelector('#dt-count')||{}).textContent||'';"
                                "o.more2=(document.querySelector('#dt-more')||{}).textContent||'';"
                                "o.scrollH=sc.scrollHeight;o.top=sc.scrollTop;"
                                "return JSON.stringify(o);})()") or "{}"))
                            if j21.get("noScroll"):
                                record("V21", "SKIP", "记录页没有滚动容器（布局变了？）⇒ 判不了")
                            else:
                                if shots:
                                    # 留一张"列表末尾"的图：提示行与计数文案都在底部，
                                    # 静态看图才会发现"提示行被表格线切开""字和行挤在一起"。
                                    shot = await cdp.call("Page.captureScreenshot", {"format": "png"})
                                    (shots / "data_more.png").write_bytes(
                                        base64.b64decode(shot.get("data") or ""))
                                first_ok = (j21.get("first") == 50)
                                # 未载全 ⇒ 文案里必须同时有"已载"和"共"，且**不许**出现
                                # 发车/视频条数（那两个数此刻只覆盖已载那批）
                                txt_ok = ("已载" in str(j21.get("cnt"))
                                          and "共" in str(j21.get("cnt"))
                                          and "发车" not in str(j21.get("cnt")))
                                more_ok = ("已载" in str(j21.get("more"))
                                           and "滚" in str(j21.get("more")))
                                # ② 搜索只在第 51 条之后的记录 ⇒ 载全了，且正好命中那一条
                                q_ok = (j21.get("qLoaded") == tot
                                        and j21.get("qRows") == 1
                                        and str(j21.get("qHit")) == kw)
                                grow_ok = (j21.get("loaded") == tot
                                           and j21.get("rows") == tot
                                           and j21.get("uniq") == tot)
                                done_ok = (str(j21.get("more2")) == ""
                                           and "已载" not in str(j21.get("cnt2")))
                                ok21 = bool(first_ok and txt_ok and more_ok and q_ok
                                            and grow_ok and done_ok)
                                record("V21", "PASS" if ok21 else "FAIL",
                                       f"首屏 {j21.get('first')} 条（应 50）、全库 {tot} 条；"
                                       f"文案 {j21.get('cnt')!r}、提示 {j21.get('more')!r}；"
                                       f"搜 {kw!r}（只在第 51 条之后）⇒ 载入 "
                                       f"{j21.get('qLoaded')} 条 / 命中 {j21.get('qRows')} 行 "
                                       f"{j21.get('qHit')!r}"
                                       f"；滚 {j21.get('loop')} 次 ⇒ 已载 {j21.get('loaded')} / "
                                       f"唯一 {j21.get('uniq')} / 表格行 {j21.get('rows')}"
                                       f"（应都是 {tot}）；载全后提示 {j21.get('more2')!r}"
                                       f"（应为空）、文案 {j21.get('cnt2')!r}"
                                       + ("" if ok21 else
                                          " ⇒ 要么还是只给 50 条（用户会以为记录就这么点），"
                                          "要么未载全就搜（「搜不到」被读成「没有这条记录」），"
                                          "要么未载全就把「发车 N」当全库数，"
                                          "要么翻页重复/漏条"))
                    except Exception as e:                        # noqa: BLE001
                        record("V21", "SKIP",
                               f"驱动列表续载出错（判不了）：{type(e).__name__}: {e}")

                # ---------------- V23 回放的状态卡 == 实时通道卡（同一份信息量）
                head("11l. 回放：状态卡与实时通道卡同一张码表、同一份取值")
                # 为什么单列：**数据一直在记录里**（整行 32 列），而回放原来只透
                # 14 个字段（界面显示 11 项）⇒ 少一大截（四路红外 / 陀螺角 / 左右里程 /
                # 冲刺四项 / 前墙两档 / 帧时基）。用户看到的结论只能是"回放本来就没
                # 这些数据" —— 而这是**显示层把数据挡掉了**，不是设备没给。
                # 这条钉四件事：
                #   ① 两边渲染出的键集合**完全相同**（都来自 `CH.groups` 这张码表）；
                #   ② 顺序也一致（同一张表 ⇒ 不该出现"回放把红外排到前面"）；
                #   ③ 当前样本里**有值**的键，界面上不许显示 `—`（逐个比，不比"看着挺多"）；
                #   ④ 4 灯在（4 格三态）；把 `fieldPos` 摘掉 ⇒ **所有**值必须变 `—`
                #      （负向：证明这些数字真是从样本里取的，不是别处抄来的）。
                if not pick("V23"):
                    record("V23", "SKIP", "--only 未选中")
                elif not ui.get("live"):
                    record("V23", "SKIP", "没进真数据模式 ⇒ 通道卡无从比对")
                else:
                    try:
                        await cdp.eval("window.C.go('live')")
                        await asyncio.sleep(0.7)
                        live_keys = json.loads(str(await cdp.eval(
                            "JSON.stringify(Array.from("
                            "document.querySelectorAll('#lv-ch [data-k]'))"
                            ".map(e=>e.dataset.k))")) or "[]")
                        await cdp.eval("window.C.go('replay')")
                        await asyncio.sleep(2.0)
                        j23 = json.loads(str(await cdp.eval(
                            "(async()=>{const C=window.C,S=C.S;"
                            "const rec=C.record(S.recSel)||{};"
                            "if(!rec.samples||!rec.samples.length){await C.loadReplay(rec.id);"
                            "await new Promise(r=>setTimeout(r,900));}"
                            "const rec2=C.record(S.recSel)||{};"
                            "const o={id:rec2.id,samples:(rec2.samples||[]).length,"
                            "fields:(rec2.fields||[]).length};"
                            "const dds=Array.from(document.querySelectorAll('#rp-ch [data-k]'));"
                            "o.keys=dds.map(e=>e.dataset.k);"
                            "const vals=dds.map(e=>(e.textContent||'').trim());"
                            "o.dash=vals.filter(t=>t==='—').length;"
                            "const sm=(rec2.samples||[])[0]||{};"
                            "const fp=rec2.fieldPos||{};"
                            "const v=sm.v||[];"
                            "o.expect=o.keys.filter(k=>{const j=fp[k];"
                            "return j!=null&&v[j]!=null&&v[j]!==undefined;}).length;"
                            "o.shown=vals.filter(t=>t&&t!=='—').length;"
                            "o.leds=Array.from(document.querySelectorAll('#rp-leds .l-state'))"
                            ".map(e=>e.textContent);"
                            "o.run=(document.querySelector('#rp-run')||{}).textContent||'';"
                            # 负向：摘掉字段索引 ⇒ 界面上必须**一个值都不剩**
                            "const save=rec2.fieldPos;"
                            "rec2.fieldPos={};C.VIEWS.replay.paint(true);"
                            "o.dashAfter=Array.from(document.querySelectorAll('#rp-ch [data-k]'))"
                            ".filter(e=>(e.textContent||'').trim()==='—').length;"
                            "rec2.fieldPos=save;C.VIEWS.replay.paint(true);"
                            "o.restored=Array.from(document.querySelectorAll('#rp-ch [data-k]'))"
                            ".filter(e=>(e.textContent||'').trim()!=='—').length;"
                            "return JSON.stringify(o);})()") or "{}"))
                        rp_keys = j23.get("keys") or []
                        n_keys = len(rp_keys)
                        shown = int(j23.get("shown") or 0)
                        expect = int(j23.get("expect") or 0)
                        if not j23.get("samples"):
                            record("V23", "SKIP",
                                   f"记录 {j23.get('id')!r} 没取到回放样本 ⇒ 状态卡无从比对")
                        else:
                            same_ok = (rp_keys == live_keys)
                            many_ok = n_keys >= 28
                            # 「有值的键」逐个比：界面显示的非 `—` 数 == 样本里非 null 数
                            val_ok = (shown == expect and shown > 0)
                            leds_ok = (len(j23.get("leds") or []) == 4
                                       and all(t in ("亮", "灭", "未知")
                                               for t in (j23.get("leds") or [])))
                            neg_ok = (int(j23.get("dashAfter") or 0) == n_keys
                                      and int(j23.get("restored") or 0) == shown)
                            ok23 = bool(same_ok and many_ok and val_ok and leds_ok and neg_ok)
                            record("V23", "PASS" if ok23 else "FAIL",
                                   f"回放 {n_keys} 项 / 实时 {len(live_keys)} 项，"
                                   f"键集合与顺序一致={same_ok}、≥28 项={many_ok}；"
                                   f"当前样本有值的 {expect} 个键，界面显示 "
                                   f"{shown} 个（应相等）={val_ok}；"
                                   f"4 灯 {j23.get('leds')}={leds_ok}；运行={j23.get('run')!r}；"
                                   f"摘掉字段索引 ⇒ {j23.get('dashAfter')}/{n_keys} 项变「—」，"
                                   f"还原后有值 {j23.get('restored')} 项={neg_ok}"
                                   + ("" if ok23 else
                                      " ⇒ 回放的状态卡又比实时少了几项，"
                                      "或者那些数字不是从这条记录的样本里取的"))
                    except Exception as e:                        # noqa: BLE001
                        record("V23", "SKIP",
                               f"驱动回放状态卡出错（判不了）：{type(e).__name__}: {e}")

                # ---------------- V24 断链显示与"串口是否开着"分开（改版四十七新增）
                head('11n. 判死 ≠ 断开：三种状态三种说法，红只给"确认无应答"')
                # 为什么单列：现场那句"界面显示断开了，但内部收发一直是正常的"——
                # 显示层当时只有两个输入（`connected` + `link_dead`），于是
                # 「PING 不通但串口还开着」只能被画成"未连接/断开"，而上游其实
                # 一直在收帧。改版四十七把两种事实拆开：`connected` = 串口，
                # `link.state` = 链路（ok/paused/no_pull/waiting/suspect/dead）。
                #
                # 做法：**同一个 `connected:true` 喂三份 health**，看界面说的三句话
                # 是否各不相同、且红只出现在第三份（dead）——
                #   A 判死（串口仍开）：面板写"链路无应答（串口仍开）"、上红、顶栏"链路无应答"；
                #   B 帧回来（state=ok）：不写无应答、不红、顶栏回端口名；
                #   C 人停的（pull_mode=off）：写"已停（手动）"、**不红**（这不是故障）。
                # ⚠️ 三条腿缺一不可：只验 A 会放过"什么都涂红"（恐慌型假警报，
                #    现场会去拔线）；只验 C 会放过"手动停被当成故障"。
                # ⚠️ 这里**不伪造 `connected:false`**：那会走 `resetTelemetry()`
                #    下降沿，把后面判据要用的读数清掉（V20 专门验那条路径）。
                if not pick("V24"):
                    record("V24", "SKIP", "--only 未选中")
                elif not ui.get("live"):
                    record("V24", "SKIP", "前端没进真数据模式 ⇒ 面板与顶栏无从验起")
                else:
                    fake = {
                        "A": {"mouse": {"available": True, "connected": True,
                                        "port": "COM7", "stream_on": True,
                                        "pull_mode": "auto",
                                        "link": {"state": "dead",
                                                 "link_dead": {"at": 1, "rounds": 3},
                                                 "frame_age_ms": 12000,
                                                 "probe_fail_rounds": 3}}},
                        "B": {"mouse": {"available": True, "connected": True,
                                        "port": "COM7", "stream_on": True,
                                        "pull_mode": "auto",
                                        "link": {"state": "ok", "link_dead": None,
                                                 "frame_age_ms": 120,
                                                 "probe_fail_rounds": 0}}},
                        "D": {"mouse": {"available": True, "connected": True,
                                        "port": "COM7", "stream_on": False,
                                        "pull_mode": "off",
                                        "link": {"state": "paused", "link_dead": None,
                                                 "frame_age_ms": 12000,
                                                 "probe_fail_rounds": 0}}},
                    }
                    try:
                        j24 = json.loads(str(await cdp.eval(
                            "(()=>{const C=window.C;"
                            "let host=document.getElementById('v24-probe');"
                            "if(!host){host=document.createElement('div');"
                            "host.id='v24-probe';"
                            "host.style.cssText='position:fixed;left:-9999px;top:0';"
                            "document.body.appendChild(host);}"
                            "const pp=C.pullPanel(host);"
                            "const snap=()=>{const m=host.querySelector('.pl-meta');"
                            "const d=document.querySelector('#devices [data-dev=mouse] em');"
                            "return {pull:!!C.S.pulling,meta:m?m.textContent:'',"
                            "bad:m?m.classList.contains('bad'):null,"
                            "chip:d?d.textContent:'',"
                            "intent:C.S.health.mouse.pullIntent};};"
                            "const out={};"
                            f"C.applyHealth({json.dumps(fake['A'])});pp.paint();out.A=snap();"
                            f"C.applyHealth({json.dumps(fake['B'])});pp.paint();out.B=snap();"
                            f"C.applyHealth({json.dumps(fake['D'])});pp.paint();out.D=snap();"
                            "return JSON.stringify(out);})()") or "{}"))
                        a, b, d_ = (j24.get("A") or {}), (j24.get("B") or {}), (j24.get("D") or {})
                        a_ok = ("无应答" in (a.get("meta") or "")
                                and a.get("bad") is True
                                and a.get("chip") == "链路无应答"
                                and a.get("pull") is True)      # 判死不许把拉取指示清零
                        b_ok = ("无应答" not in (b.get("meta") or "")
                                and b.get("bad") is False
                                and a.get("chip") != b.get("chip")
                                and b.get("pull") is True)
                        d_ok = ("已停" in (d_.get("meta") or "")
                                and d_.get("bad") is False          # 手动停不涂红
                                and d_.get("pull") is False
                                and d_.get("intent") == "off")
                        ok24 = bool(a_ok and b_ok and d_ok)
                        record("V24", "PASS" if ok24 else "FAIL",
                               f"A 判死：面板={a.get('meta')!r}·红={a.get('bad')}"
                               f"·顶栏={a.get('chip')!r}；"
                               f"B 帧回来：面板={b.get('meta')!r}·红={b.get('bad')}"
                               f"·顶栏={b.get('chip')!r}；"
                               f"C 人停：面板={d_.get('meta')!r}·红={d_.get('bad')}"
                               f"·intent={d_.get('intent')!r}"
                               + ("" if ok24 else
                                  ' ⇒ **要么判死被画成了「未连接」（现场会去拔线），'
                                  '要么把「人停的」也涂红了**'))
                    except Exception as e:                        # noqa: BLE001
                        record("V24", "SKIP",
                               f"驱动面板/顶栏出错（判不了）：{type(e).__name__}: {e}")
                    # 收尾：把真 health 喂回去（上面三份是**伪造的显示状态**，
                    # 留着会让后面的组看到一台"判死了"的车）
                    try:
                        await cdp.eval("(()=>{const h=document.getElementById('v24-probe');"
                                       "if(h)h.remove();})()")
                        real = read_health(base)
                        if real:
                            await cdp.eval("(()=>{window.C.applyHealth("
                                           + json.dumps(real) + ");})()")
                    except Exception:                             # noqa: BLE001
                        pass


                # ⚠️ V10 会**把设备断开**（它要验"点不存在的端口不许说已连接"），
                #    所以必须排在所有"需要活链路"的判据（V9/V11/V12/V13/V14）**之后**。
                #    放在前面时它们会全 SKIP —— 看着像"没实现"，其实是被 V10 断了。
                head("11. 连接按钮：点一个不存在的端口，界面不许说「已连接」")
                # ⚠️ 这条**只在"本来就没连"时跑**（见下面的 `api_mouse.get("connected")`
                #    分支）：点连接会先断开再打开新端口，拿真机试错等于把现场拆了。
                #    所以这里**不主动断开** —— 断开的只有"全局开头那一次"，
                #    而那个也已经删了（就是它让 V9/V11/V12 全 SKIP 的）。
                # 只在**后端本来就没连**时跑：① 这正是 bug 复现的条件（界面自己编一个"已连接"，
                # 1 s 后 /health 把真话拉回来）；② 不去动用户已经连着的真机
                # （点连接会先断开再打开新端口，乱试等于把现场拆了）。
                if not ui.get("live"):
                    record("V10", "SKIP", "前端没进真数据模式 ⇒ 连接按钮无从验起")
                elif api_mouse.get("connected"):
                    record("V10", "SKIP",
                           "电脑鼠已连着（可能是真机）⇒ 不拿它去试错端口，免得把现场拆了")
                else:
                    await cdp.eval("window.__calls.length=0")
                    await cdp.eval(
                        "(()=>{const c=document.querySelector('.devchip[data-dev=mouse]');"
                        "if(c) c.click();})()")
                    await asyncio.sleep(0.7)
                    await cdp.eval(
                        "(()=>{const i=document.getElementById('dr-manual');"
                        "if(i) i.value='COM99';"
                        "const b=document.getElementById('dr-connect');if(b) b.click();})()")
                    err_txt = ""
                    # 失败要留够时间：串口打不开有时要等到系统超时
                    for _ in range(10):
                        await asyncio.sleep(0.5)
                        st = await cdp.eval(
                            "(()=>{const S=window.C.S;"
                            "const t=(document.getElementById('toasts')||{}).innerText||'';"
                            "return {conn:S.health.mouse.connected,toast:t};})()") or {}
                        err_txt = str(st.get("toast") or "")
                        if not st.get("conn") and err_txt:
                            break
                    calls = await cdp.eval("window.__calls||[]") or []
                    hit = [c for c in calls
                           if str(c.get("u", "")).endswith("/api/v1/mouse/connect")]
                    after = await cdp.eval(
                        "(()=>{const S=window.C.S;"
                        "const chip=document.querySelector('.devchip[data-dev=mouse]');"
                        "return {conn:S.health.mouse.connected,"
                        " chipTxt:chip?(chip.innerText||''):''};})()") or {}
                    # 三个都要成立：真的发了请求；状态**没有被本地改成已连接**；
                    # 界面上不许出现"已连接"这种话（只有后端说连上才允许说）
                    said_ok = ("已连接" in err_txt) or ("已连接" in str(after.get("chipTxt") or ""))
                    ok10 = bool(hit) and (after.get("conn") is False) and not said_ok
                    # 关抽屉，别把下一节留在遮挡层下面
                    await cdp.eval(
                        "(()=>{const b=document.getElementById('dr-close');if(b) b.click();"
                        "document.body.classList.remove('drawer-open');})()")
                    record("V10", "PASS" if ok10 else "FAIL",
                           f"点【连接】填 COM99 ⇒ 抓到 {len(hit)} 条 `POST /mouse/connect`；"
                           f"2 s 后界面 connected={after.get('conn')}"
                           f"（后端也没连）、设备片 {str(after.get('chipTxt'))[:24]!r}；"
                           f"界面上出现「已连接」={said_ok}"
                           + ("" if ok10 else
                              " ⇒ **按钮只改了本地状态、没真连**（现场表现就是「提示已连接、"
                              "1 秒后断开」），或者连不上时还敢说已连接"))

                # ---------------- V11 按页开关拉取（改版三十一）
                # ---------------- V8 界面文案纪律
                head("12. 主区文案：不放机制说明")
                txt = await cdp.eval(
                    "(()=>{const m=document.getElementById('main');"
                    "return m?(m.innerText||''):'';})()") or ""
                smells = [w for w in ("判据", "§", "铁律", "TODO", "FIXME") if w in txt]
                record("V8", "PASS" if not smells else "FAIL",
                       "主区没有机制/判据编号这类说明性文字" if not smells
                       else f"主区出现说明性文字：{smells}")

    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        if profile is not None:
            # 删掉**这一次**的 profile（在系统临时目录里，约 283 个文件 / 5 MB，
            # 实测 1~2 s，不会拖退出）。以前这里写的是 `pass` —— 于是 profile
            # 只增不减，堆到 32 个 / 1 万文件之后，"清理 .tmp"反而成了超时源
            # （Git Bash 的 rm 处理 1 万个小文件要 4 分钟）。
            # 教训：省一次 1 秒的删除，换来一个 4 分钟的删除，账是反的。
            _rmtree_quiet(profile)
        if sim_proc is not None:
            sim_proc.terminate()
            try:
                sim_proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                sim_proc.kill()
            try:
                _req(base + "/api/v1/mouse/disconnect", {}, timeout=15)
            except Exception:                                 # noqa: BLE001
                pass

    head("汇总")
    n_pass = sum(1 for _, v, _ in RESULTS if v == "PASS")
    n_fail = sum(1 for _, v, _ in RESULTS if v == "FAIL")
    n_skip = sum(1 for _, v, _ in RESULTS if v == "SKIP")
    print(f"  PASS {n_pass}   FAIL {n_fail}" + (f"   SKIP {n_skip}" if n_skip else ""))
    if n_fail:
        print("\n  没过的：")
        for cid, v, note in RESULTS:
            if v == "FAIL":
                print(f"    · {cid}: {note}")
    if args.shots:
        print(f"\n  截图：{Path(args.shots).resolve()}")
    return 1 if n_fail else 0


#: `--only` 选中的判据（空集 = 全跑）。见 `pick()`。
ONLY: set = set()


def pick(*ids: str) -> bool:
    """这一段要不要跑。`--only` 没给 ⇒ 全跑；给了 ⇒ 段内**任一条**被选中就跑。"""
    if not ONLY:
        return True
    return any(str(i).upper() in ONLY for i in ids)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--sim", action="store_true",
                    help="先起上游鼠标模拟器并让服务端连上（页面才有真数据）")
    ap.add_argument("--sim-port", type=int, default=5555)
    ap.add_argument("--wait", type=float, default=4.0,
                    help="首页打开后等多久（等 WS + 首轮 health）")
    ap.add_argument("--shots", default="")
    ap.add_argument("--timeout", type=float, default=150.0,
                    help="整轮上限（秒）。超时直接报**卡在哪一段**，别再干等。")
    ap.add_argument("--only", default="",
                    help="只跑这几条判据（逗号分隔，如 V12,V13）。"
                         "未选中的段**整段跳过**（连等待一起省），输出记 SKIP。"
                         "改一处前端就只跑相关的那几条，别再等全套。")
    ap.add_argument("--width", type=int, default=1680)
    ap.add_argument("--height", type=int, default=1000)
    args = ap.parse_args(argv)
    #: `--only` 的判据集合（空 = 全跑）。**段级**跳过：段内所有判据都没选中就连
    #: 等待一起不做 —— 光在输出上过滤等于白等（这工具一半时间在 sleep 上）。
    global ONLY
    ONLY = {x.strip().upper() for x in str(args.only or "").split(",") if x.strip()}
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:                                         # noqa: BLE001
        pass
    print("=" * 74)
    print("Control 前端无头实测（第三版：九页面 + 接缝）")
    print("=" * 74)
    # ⚠️ **超时必须是线程级的，不能用 `asyncio.wait_for`**。
    #    这个脚本里大量用的是**同步** `urllib`（`_req` / `read_health`）和
    #    `subprocess` 的 `readline`：它们阻塞时事件循环整个停住，
    #    `wait_for` 的计时器也跟着停摆 —— 于是"90 s 超时"能变成 3 分钟干等
    #    （实测踩过两次）。线程 + `os._exit` 不受事件循环影响，说 40 s 就 40 s。
    def _watchdog(sec: float) -> None:
        time.sleep(sec)
        try:
            import sys as _s
            _s.stdout.write(
                f"\n[超时] {sec:.0f} s 没跑完 —— **卡在**：{CUR['head']}"
                f"（该段已停留 {time.time() - CUR['t']:.0f} s）\n"
                "      先看这一段要等的东西在不在（服务 / 模拟器 / 浏览器 / 串口），"
                "或把 --timeout 调大。\n")
            _s.stdout.flush(); _s.stderr.flush()
        finally:
            os._exit(3)

    import threading as _th
    _th.Thread(target=_watchdog, args=(float(args.timeout),), daemon=True).start()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
