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


def head(t: str) -> None:
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

    # 确定的起点：主动断一次，等 connected 连续两次为 False。
    # 不这么做的话，"上一轮模拟器被杀、服务端还认为连着"那一小段窗口里，
    # 页面的门控会按"已连"渲染，而检查器稍后读到"未连" ⇒ 断言随机红。
    head("0a. 确定起点")
    stable, broke = False, False
    try:
        _req(base + "/api/v1/mouse/disconnect", {})
        prev = None
        for _ in range(15):
            cur = bool((read_health(base).get("mouse") or {}).get("connected"))
            if cur is False and prev is False:
                stable = True
                break
            prev = cur
            await asyncio.sleep(0.4)
    except Exception as e:                                    # noqa: BLE001
        broke = True
        record("S0e", "SKIP", f"清理残留连接失败（不影响后续）：{e}")
    if not broke:
        record("S0e", "PASS" if stable else "SKIP",
               "鼠标已断开且状态稳定" if stable else "6 s 内未稳定 —— 连接类断言可能不准")

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
        profile = ROOT / ".tmp" / f"bcheck-{int(time.time())}"
        profile.mkdir(parents=True, exist_ok=True)
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
                elif args.sim:
                    ok5 = (api_mouse.get("connected") is True and ui.get("conn") is True
                           and chip_ok)
                    record("V5", "PASS" if ok5 else "FAIL",
                           f"应连上：后端={api_mouse.get('connected')}、"
                           f"界面={ui.get('conn')}、设备片 {ui.get('chipTxt')!r}")
                else:
                    ok5 = (api_mouse.get("connected") is False and ui.get("conn") is False
                           and not chip_ok)
                    record("V5", "PASS" if ok5 else "FAIL",
                           f"**应未连**：后端={api_mouse.get('connected')}、"
                           f"界面={ui.get('conn')}、设备片 {ui.get('chipTxt')!r}"
                           f"（假'已连接'比不显示更坏）")

                # ---------------- V6 接缝（核心）
                head("8. 接缝：每批帧数 N 的请求真的发出去了")
                if not ui.get("live"):
                    record("V6", "SKIP",
                           "前端没进真数据模式（LIVE.on=false）⇒ 接缝无从验起")
                else:
                    await cdp.eval("window.__calls.length=0")
                    await cdp.eval(
                        "(()=>{const i=document.getElementById('lv-n');"
                        "if(i){i.value='3';}"
                        "const b=document.getElementById('lv-n-apply');if(b){b.click();}})()")
                    await asyncio.sleep(1.5)
                    calls = await cdp.eval("window.__calls||[]") or []
                    hit = [c for c in calls
                           if str(c.get("u", "")).endswith("/api/v1/mouse/stream/batch")]
                    body_ok = False
                    if hit:
                        try:
                            body_ok = int(json.loads(hit[-1].get("b") or "{}").get("n")) == 3
                        except Exception:                     # noqa: BLE001
                            body_ok = False
                    pull = await cdp.eval("window.C.S.pullN")
                    record("V6", "PASS" if (hit and body_ok and pull == 3) else "FAIL",
                           f"点【应用】n=3 ⇒ 抓到 {len(hit)} 条 `POST /mouse/stream/batch`、"
                           f"body.n 正确={body_ok}、界面 S.pullN={pull}" if hit else
                           f"**点了【应用】但请求没发出去**（这一轮别的请求 "
                           f"{[c.get('u') for c in calls][:4]}）—— 界面会显示成功，网络里什么都没有")
                    # 还原成 1：别把环境留在批式档
                    try:
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

                # ---------------- V10 连接按钮必须真的去连（本版新增，专治"连上又断开"）
                head("11. 连接按钮：点一个不存在的端口，界面不许说「已连接」")
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
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        if profile is not None:
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--sim", action="store_true",
                    help="先起上游鼠标模拟器并让服务端连上（页面才有真数据）")
    ap.add_argument("--sim-port", type=int, default=5555)
    ap.add_argument("--wait", type=float, default=4.0,
                    help="首页打开后等多久（等 WS + 首轮 health）")
    ap.add_argument("--shots", default="")
    ap.add_argument("--width", type=int, default=1680)
    ap.add_argument("--height", type=int, default=1000)
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:                                         # noqa: BLE001
        pass
    print("=" * 74)
    print("Control 前端无头实测（第三版：九页面 + 接缝）")
    print("=" * 74)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
