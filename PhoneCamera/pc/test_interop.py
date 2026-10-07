#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""跨程序通信验证（P1–P13）：**真起服务，真用三套互不相关的消费端去读它**。

与 `pc/test_server.py` 的分工（两件事，不能互相代替）：
  · `test_server.py` 用 aiohttp **在同一个进程里**打服务 ⇒ 证明的是**协议正确**；
  · 本文件起**独立进程**的服务，再用**三套不同技术栈**的消费端去读 ⇒ 证明的是
    **D26 那句承诺本身**（"整体架构里的其它程序能直接消费"）。
  同进程里跑得通的客户端，可能暗地里依赖了只有本项目才有的东西（aiohttp 的 WS /
  multipart 解析、共享的 Python 对象）。**换一套栈还能用，才算数** —— 本项目反复栽在
  "测试全绿、现场不 work"上，所以这里刻意让消费端互不相关：

    ① `pc/hostlink.py`    —— 本项目自带的最小客户端库（**只用标准库**）
    ② `cv2.VideoCapture`  —— OpenCV（最常用的视觉入口）
    ③ `ffmpeg`            —— **另一个操作系统进程、非 Python**（"消费方零依赖"不是嘴上说说）

判据（每条都配一个"能判否"的理由）：
  P1 capabilities：无需 token 可读、**不含凭据**、道声明结构完整且**恰有一条非 lossy 的留存道**、无 archive
  P2 hostlink 三行接入：`status()` 有 `store` 无 `archive`；账本两个出口同一口径
  P3 hostlink 订阅：`events()` 收到 hello/session_started（广播能被第三方程序消费）
  P4 hostlink 控制：`start()` 闭环 + **默认 plan 不抽帧**（含源码锚定与反例）
  P5 cv2.VideoCapture 读到**真 JPEG**：尺寸对 + 内容与推过去的渐变一致（不是"有字节就算"）
  P6 ffmpeg（独立进程）解出 2 帧、尺寸与像素均值都对
  P7 隔离仍成立：无 token 的 `host/status`、`/stream/mjpeg` 都 403
  P8 鉴权仍成立：control 无 token / 错 token ⇒ 403，对 token ⇒ 200（跨进程视角）
  P9 第三方 DX：连不上时的报错必须**可读**（不是裸 URLError），否则调用方无从排查
  P10 文档里的接入示例必须**真的能跑**（README 的 `h.xxx` 都在真类上；构造函数关键字在签名里）
  P11 配对**三件套**（用户口径：网址 + token + 二维码）：`pair()` 齐备；二维码交给
      **另一个 JVM 进程**用 App 那个 zxing jar 解，逐字符比对；含"换个码必须解出不同"的反例
  P12 用 hostlink **代替面板**走一遍：overview / sessions / start / status / stop /
      ledger / thumb_url / mjpeg_url —— 面板能做的，第三方程序都能做
  P13 实时道（`capture_mode=live`，只看不留）：用**标准库**推彩色帧、再用 **cv2** 从
      MJPEG 把它**解出来比像素**（左半红 / 右半蓝）⇒ 证明这段画面**真的是彩色**；
      配反例：同一套判据用在**灰度**帧上必须判否（灰度 JPEG 也是 3 通道 ⇒ 只看通道数
      会恒判彩色）。另含跨进程的模式互斥反例（deliver 会话收到实时帧必须 400）

跑法：`python pc/test_interop.py`
依赖：general_env（aiohttp）+ cv2 + ffmpeg（找不到 ffmpeg 时 P6 判 FAIL 并说明，不静默跳过）
"""
from __future__ import annotations

import asyncio
import base64
import collections
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
PY = sys.executable
TOKEN = "interoptoken"
SID = "2026-09-27_0053_itop"
THUMB_W, THUMB_H = 480, 360
OLD_DEFAULT_LINE = '{"mode": "interval", "interval_ms": 200, "count": 0}'

FFMPEG = shutil.which("ffmpeg") or next(
    (p for p in (r"C:\PersonalData\Program\Others\ffpeg"
                 r"\ffmpeg-2024-09-05-git-3d0d0f68d5-full_build\bin\ffmpeg.exe",)
     if Path(p).exists()), "")

results: list[tuple[str, bool, str]] = []


def check(tag: str, ok: bool, detail: str = "") -> None:
    results.append((tag, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {tag}" + (f"  {detail}" if not ok and detail else ""))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_thumb() -> bytes:
    """一张**确定的**灰度渐变 JPEG：宽 480 高 360，左列黑、右列白、均值≈127.5。

    为什么不用随机图：判据要能验"**解出来的内容就是我发的那张**"。
    只验"收到了一个 JPEG"的话，通道串了、缓存串了、画面冻在上一帧上，都照样绿。
    """
    import cv2
    import numpy as np
    col = (np.arange(THUMB_W, dtype=np.int32) * 255 // (THUMB_W - 1)).astype(np.uint8)
    img = np.tile(col, (THUMB_H, 1))
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
    if not ok:
        raise RuntimeError("cv2.imencode 失败")
    return buf.tobytes()


def grad_score(frame) -> tuple:
    """(h, w, mean, 左列均值, 右列均值) —— 灰度或 BGR 都能喂。"""
    import numpy as np
    a = np.asarray(frame)
    if a.ndim == 3:
        a = a[:, :, 0]
    return (a.shape[0], a.shape[1], float(a.mean()),
            float(a[:, 0].mean()), float(a[:, -1].mean()))


def read_cv2(url: str, want: int = 1, timeout: float = 20.0):
    import cv2
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    t0 = time.time()
    frames = []
    try:
        while time.time() - t0 < timeout and len(frames) < want:
            ok, fr = cap.read()
            if ok and fr is not None:
                frames.append(fr)
            else:
                time.sleep(0.05)
    finally:
        cap.release()
    return frames


def read_ffmpeg(url: str, out: Path, n: int = 2, timeout: float = 30.0):
    """**另一个进程**去解 MJPEG。返回 (returncode, stderr, 解出的纯灰度字节)。"""
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "mpjpeg", "-i", url, "-frames:v", str(n),
           "-pix_fmt", "gray", "-f", "rawvideo", str(out)]
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    raw = out.read_bytes() if out.exists() else b""
    return p.returncode, p.stderr.decode("utf-8", "replace")[:400], raw


def scan_api(text: str, cls) -> tuple[list[str], list[str]]:
    """把文档里的接入示例当**代码**读：出现的 `h.xxx` / `HostLink.xxx` 必须在真类上存在，
    构造函数的关键字参数必须真的在签名里。只扫围栏 ```python 块（避免把散文里的
    反斜杠写法误当调用）。返回 (缺失的名字, 签名里没有的关键字)。"""
    import inspect
    import re
    blocks = re.findall(r"```python\n(.*?)```", text, re.S)
    code = "\n".join(blocks)
    missing = sorted({f"h.{n}" for n in re.findall(r"\bh\.(\w+)", code)
                      if not hasattr(cls, n)} |
                     {f"HostLink.{n}" for n in re.findall(r"\bHostLink\.(\w+)", code)
                      if not hasattr(cls, n)})
    params = set(inspect.signature(cls.__init__).parameters) - {"self"}
    bad_kw = sorted({kw for call in re.findall(r"HostLink\(([^)]*)\)", code)
                     for kw in re.findall(r"(\w+)\s*=(?!=)", call) if kw not in params})
    return missing, bad_kw


def find_tool(name: str) -> str:
    """找 java/javac：先 PATH，再回退到本机已知的 JDK 目录（与 verify_pair_contract 同策）。"""
    p = shutil.which(name)
    if p:
        return p
    for pat in (f"C:/Program Files/Microsoft/jdk*/bin/{name}.exe",
                f"C:/Program Files/Java/jdk*/bin/{name}.exe",
                f"C:/Program Files/Eclipse Adoptium/jdk*/bin/{name}.exe"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return ""


def zxing_classpath(tmp: Path) -> tuple[str, str]:
    """编译 JVM 侧解码器（`pc/jvmcheck/QrDecode.java`），返回 (classpath, 失败原因)。"""
    java, javac = find_tool("java"), find_tool("javac")
    zx = HERE.parent / "android" / "m0probe" / "app" / "libs" / "core-3.5.3.jar"
    if not java or not javac:
        return "", "找不到 JDK（java/javac）"
    if not zx.exists():
        return "", f"缺 {zx}（App 用的那个 zxing jar）"
    out = tmp / "cls"
    out.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([javac, "-nowarn", "-cp", str(zx), "-d", str(out),
                        str(HERE / "jvmcheck" / "QrDecode.java")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return "", f"javac 失败：{(r.stderr or r.stdout)[:200]}"
    return os.pathsep.join([str(out), str(zx)]), ""


def zxing_decode(cp: str, img: Path) -> tuple[bool, str]:
    d = subprocess.run([find_tool("java"), "-Djava.awt.headless=true",
                        "-Dstdout.encoding=UTF-8", "-cp", cp, "QrDecode", str(img)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if d.returncode != 0:
        return False, (d.stderr or "").strip()[:240]
    return True, d.stdout.strip()


async def main() -> int:
    sys.path.insert(0, str(HERE))
    from hostlink import HostLink, HostLinkError                     # noqa: E402

    port = free_port()
    tmp = Path(tempfile.mkdtemp(prefix="pcsv_iop_"))
    proc = subprocess.Popen(
        [PY, str(HERE / "server.py"), "--host", "127.0.0.1", "--port", str(port),
         "--data-dir", str(tmp), "--token", TOKEN, "--max-mbps", "25"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace")
    # ⚠ **必须持续排空子进程 stdout**（第 23 轮的代价：管道缓冲填满 ⇒ 被测进程阻塞在
    # print 上 ⇒ 表现成"某个请求莫名超时"，而根因是**测试自己**没读管道）。
    srv_log: collections.deque = collections.deque(maxlen=4000)
    threading.Thread(target=lambda p=proc: [srv_log.append(x.rstrip()) for x in p.stdout],
                     daemon=True).start()

    base = f"http://127.0.0.1:{port}"
    h = HostLink(base, token=TOKEN)
    print(f"服务进程 pid={proc.pid}  {base}")

    # ⚠ **先等就绪再开始探**。P1 是第一条判据，它上来就打 `/api/v1/capabilities`，
    # 而服务是**刚 fork 出来的** —— 机器负载高时（本轮实测）会撞上
    # `[WinError 10061] 目标计算机积极拒绝`，于是 P1 红。
    # 那时现象看着像"capabilities 接口坏了"，其实只是**服务还没 listen**。
    # ⇒ 判据要验的是**接口行为**，不是"这台机器启动一个 Python 服务要多久"。
    # 做法：显式轮询（有上限）。服务真起不来就判一条明确的 FAIL，
    # 而不是让后面每一条都跟着红（那样会淹没真正的原因）。
    ready, last_err, t_ready = False, "", time.time()
    while time.time() - t_ready < 25:
        try:
            h.capabilities()
            ready = True
            break
        except Exception as e:                                        # noqa: BLE001
            last_err = f"{type(e).__name__}: {str(e)[:110]}"
            time.sleep(0.3)
    check("服务就绪（等它 listen 成功再开始探 —— 别把启动竞态当成接口故障）",
          ready, f"{time.time() - t_ready:.1f}s {last_err}")

    events_seen: list = []
    ev_stop = threading.Event()

    def _events_probe():
        # ⚠ 类型名不要想当然：`start` / `stop` 是**设备道**（`/ws`）的广播类型；
        # 而**本机事件道**（`/api/v1/host/events`）用的是 `session_started` / `stopped` /
        # `frame_verified` / `delivery_*`。第一版这里按 `start` 过滤 ⇒ 只收到 `hello`，
        # 判据当场红 —— 这正是本项目那条老规矩：**断言失败先怀疑测试的假设**。
        try:
            for ev in h.events(types={"hello", "session_started", "stopped"}):
                events_seen.append(ev)
                if len(events_seen) >= 3 or ev_stop.is_set():
                    break
        except Exception as e:                                        # noqa: BLE001
            events_seen.append({"type": "_err", "msg": f"{type(e).__name__}: {e}"})

    try:
        async with aiohttp.ClientSession(
                # 显式写 `trust_env=False`（**aiohttp 3.14 的默认值本来就是 False**，
                #  已用 `inspect.signature` 核实过 —— 不写也安全）。
                # 写出来是为了把它**变成一条声明**：读代码的人一眼能看到"我们刻意不读代理环境"，
                # 而不是靠"某个库版本的默认值碰巧对我们有利"（本项目栽过同类：
                # 同一默认值写在两处必然分叉 / 依赖环境的现状）。
                #
                # ⚠ 真正的凶手是 **urllib**（`urllib.request.getproxies()` 会返回本机代理）——
                # 本轮实测：shell 里被注入 `HTTP_PROXY=http://127.0.0.1:50659` 后，
                # 本文件从 42/42 掉到 38/40，两条 FAIL（P1 / P9）**都走 hostlink（urllib）**，
                # 报的是代理回的 `HTTP 502: upstream connect failed ... (os error 10061)`，
                # 而服务好端端在跑 ⇒ 排查方向会被彻底带偏。
                # 修法见 `hostlink._OPENER`（空代理表）与下面的 `post_live`，
                # 并由 **P14a/P14b** 用注入式判据钉住。
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=60)) as device:
            # ---------- P1 capabilities（**无需 token**，第三方程序的第一件事） ----------
            try:
                caps = h.capabilities()
                # ⚠ 别把"道清单"抄成常量：A1a 已栽过一次 —— 新增 live 道后写死的
                #   {"monitor","delivery"} 必然红。这里断言的是**结构性质**：
                #   三条道齐备 + 每条道声明完整 + **恰有一条非 lossy 的留存道**
                #   （"只留一条道"才是核心不变量，"恰好两条"只是当时的现象）。
                lanes_i = caps.get("lanes", {}) or {}
                persist_i = sorted(k for k, v in lanes_i.items() if v.get("lossy") is False)
                incomplete_i = sorted(k for k, v in lanes_i.items()
                                      if not (v.get("purpose") and v.get("transport") and "lossy" in v))
                check("P1a capabilities 无需 token 即可读；道声明结构完整，且**恰有一条非 lossy 的留存道**"
                      "（“只留一条道”是核心不变量，不是“恰好两条”）",
                      set(lanes_i) == {"monitor", "delivery", "live"}
                      and persist_i == ["delivery"] and not incomplete_i,
                      f"lanes={sorted(lanes_i)} persist={persist_i} incomplete={incomplete_i}")
                raw = json.dumps(caps, ensure_ascii=False)
                check("P1b capabilities 里不含凭据（它无需鉴权就能读，放 token 等于广播）",
                      TOKEN not in raw, raw[:200])
                check("P1c 反例：给一个**含 token** 的合成响应，同一个嗅探器必须判否"
                      "（否则 P1b 是安慰剂）",
                      (TOKEN in json.dumps({"u": f"{base}/?token={TOKEN}"}))
                      and (TOKEN not in json.dumps({"lanes": {}})),
                      "嗅探器失灵")
            except HostLinkError as e:
                check("P1 capabilities 可读", False, f"{e}")

            # ---------- 设备侧 WS（模拟手机）：这里它只是"喂数据的人" ----------
            dws = await device.ws_connect(f"{base}/ws?token={TOKEN}")
            first = json.loads((await dws.receive()).data)
            check("P1d 设备侧 WS 首条是 config（设备面用同一个 token）",
                  first.get("type") == "config", str(first)[:160])

            # ---------- P4 hostlink 控制闭环 ----------
            threading.Thread(target=_events_probe, daemon=True).start()
            await asyncio.sleep(0.5)                     # 让 events 先连上，好收到 start
            j = h.start(session_id=SID, device_serial="IOPDEV")
            check("P4a hostlink.start() 三行接入即生效：送达设备 1 个 + 会话目录已建",
                  j.get("sent_to") == 1 and j.get("session_id") == SID
                  and (tmp / "sessions" / SID).exists(),
                  f"{str(j)[:160]} dir={(tmp / 'sessions' / SID).exists()}")
            plan_ms = (j.get("plan") or {}).get("interval_ms")
            check("P4b **默认 plan 不抽帧**（第 24 轮语义：每一帧都留）——"
                  " interval_ms 必须是 0，不是旧的 200",
                  plan_ms == 0, str(j.get("plan")))
            hsrc = (HERE / "hostlink.py").read_text(encoding="utf-8")
            has_old = '"interval_ms": 200' in hsrc
            has_new = '"interval_ms": 0' in hsrc
            check("P4c 源码锚定：hostlink 里旧的 200 ms 默认必须**消失**"
                  "（本库显式下发 plan，服务端默认兜不住它 ⇒ 调用方会静默拿到 1/6 的帧）",
                  has_new and not has_old and OLD_DEFAULT_LINE in "x = " + OLD_DEFAULT_LINE,
                  f"含 0={has_new} 含 200={has_old}")
            m = await asyncio.wait_for(dws.receive(), timeout=5)
            sm = json.loads(m.data)
            check("P4d 控制指令**真的到了设备**（不是「WS 发送成功」就算开了相机）",
                  sm.get("type") == "start" and sm.get("session_id") == SID,
                  str(sm)[:160])

            # ---------- 推缩略图（喂监视道） ----------
            thumb = make_thumb()
            thumb_b64 = base64.b64encode(thumb).decode()
            push_state = {"n": 0}

            async def pusher():
                i = 0
                try:
                    while True:
                        i += 1
                        push_state["n"] = i
                        await dws.send_str(json.dumps(
                            {"type": "thumb", "session_id": SID, "seq": 100 + i,
                             "sensor_timestamp_ns": 10_000_000 * i,
                             "jpeg_b64": thumb_b64}))
                        await asyncio.sleep(0.25)
                except Exception:                                    # noqa: BLE001
                    pass

            push_task = asyncio.create_task(pusher())
            for _ in range(20):
                if (await asyncio.to_thread(h.status))["monitor"].get("thumbs"):
                    break
                await asyncio.sleep(0.25)

            # ---------- P2 hostlink 三行接入 ----------
            st = h.status()
            check("P2a status() 里有 store 段、没有 archive 段（合并后的口径）",
                  "store" in st and "archive" not in st, str(list(st))[:200])
            srow = next((x for x in h.sessions() if x.get("session_id") == SID), None)
            led_a = h.ledger(SID)
            keys = {"state", "frames_expected", "gaps_on_device", "upload_gaps",
                    "missing_seqs", "complete"}
            check("P2b 账本只有两个出口（host/status.store 与 sessions[].store）且**同一口径**",
                  keys <= set(led_a) and keys <= set(st.get("store") or {})
                  and srow is not None and (srow.get("store") or {}).get("state")
                  == led_a.get("state"),
                  f"keys={sorted(led_a)}")
            check("P2c 两个缺口**分开命名**（现场据此选处置：重采 vs 重传）",
                  "gaps_on_device" in led_a and "upload_gaps" in led_a
                  and "complete" in led_a, str(led_a)[:200])

            # ---------- P5 cv2.VideoCapture（另一套栈） ----------
            mj = h.mjpeg_url()
            frames = await asyncio.to_thread(read_cv2, mj, 1, 20.0)
            if frames:
                hh, ww, mean, lft, rgt = grad_score(frames[0])
                check("P5 cv2.VideoCapture 直读实时流，且**解出来的内容就是发过去那张**"
                      "（尺寸 + 灰度渐变：左黑右白）",
                      (hh, ww) == (THUMB_H, THUMB_W) and abs(mean - 127.5) < 6
                      and lft < 25 and rgt > 230,
                      f"shape={hh}x{ww} mean={mean:.1f} left={lft:.0f} right={rgt:.0f}")
            else:
                check("P5 cv2.VideoCapture 直读实时流", False,
                      f"20 s 内没读到帧；推了 {push_state['n']} 张；"
                      f"服务端日志尾部={list(srv_log)[-6:]}")

            # ---------- P6 ffmpeg（**另一个进程**） ----------
            if not FFMPEG:
                check("P6 ffmpeg（独立进程）解 MJPEG", False, "本机 PATH 与已知路径都找不到 ffmpeg")
            else:
                out = tmp / "ff.raw"
                rc, err, rawb = await asyncio.to_thread(read_ffmpeg, mj, out, 2, 30.0)
                want = 2 * THUMB_W * THUMB_H
                if rc == 0 and len(rawb) == want:
                    import numpy as np
                    a = np.frombuffer(rawb, dtype=np.uint8).reshape(2, THUMB_H, THUMB_W)
                    gs = [grad_score(a[i]) for i in range(2)]
                    check("P6 ffmpeg（**独立进程、非 Python**）解出 2 帧且尺寸/灰度渐变都对"
                          "（这才叫「消费方零依赖」）",
                          all(s[0] == THUMB_H and s[1] == THUMB_W
                              and abs(s[2] - 127.5) < 6 and s[3] < 25 and s[4] > 230
                              for s in gs),
                          f"rc={rc} bytes={len(rawb)}/{want} 逐帧={gs}")
                else:
                    check("P6 ffmpeg（独立进程）解 MJPEG", False,
                          f"rc={rc} bytes={len(rawb)}/{want} stderr={err}")

            # ---------- P3 hostlink 订阅广播 ----------
            ev_stop.set()
            for _ in range(40):                          # 等订阅线程收齐（最多 8 s）
                if len(events_seen) >= 2:
                    break
                await asyncio.sleep(0.2)
            kinds = [e.get("type") for e in events_seen]
            check("P3 hostlink.events() 收到 hello（广播能被第三方程序消费）",
                  "hello" in kinds, f"收到={kinds[:6]}")
            check("P3b 采集生命周期事件走**同一条**订阅（`session_started`）—— "
                  "不是只有握手能收到",
                  "session_started" in kinds and len(kinds) >= 2, f"收到={kinds[:8]}")

            # ---------- P7 / P8 鉴权（跨进程视角复验一次） ----------
            async def raw_get(path):
                async with device.get(base + path) as r:
                    return r.status

            check("P7a 无 token 的 host/status ⇒ 403；带 token ⇒ 200",
                  await raw_get("/api/v1/host/status") == 403
                  and await raw_get(f"/api/v1/host/status?token={TOKEN}") == 200,
                  "host/status 鉴权失效")
            check("P7b 无 token 的 /stream/mjpeg ⇒ 403（不能凭「是本机」就白给画面）",
                  await raw_get("/stream/mjpeg") == 403, "MJPEG 无鉴权")

            async def raw_ctl(tok=""):
                url = f"{base}/api/v1/control/stop" + (f"?token={tok}" if tok else "")
                hd = {"Content-Type": "application/json"}
                if tok:
                    hd["X-Token"] = tok
                async with device.post(url, json={"session_id": SID}, headers=hd) as r:
                    return r.status

            check("P8a control 无 token ⇒ 403（它不在 loopback 名单里，不校验等于"
                  "同网段谁都能启停采集）",
                  await raw_ctl() == 403, "control 无鉴权")
            check("P8b control 带**错** token ⇒ 403；带对 token ⇒ 200",
                  await raw_ctl("wrong") == 403 and await raw_ctl(TOKEN) == 200,
                  "错 token 被放行")

            push_task.cancel()
            await dws.close()

        # ---------- P9 第三方 DX：连不上时必须是**可读**的错误 ----------
        bad = HostLink(f"http://127.0.0.1:{free_port()}", token=TOKEN, timeout=3.0)
        try:
            bad.status()
            check("P9 连不上时报错可读", False, "竟然连上了（端口被占用？）")
        except HostLinkError as e:
            check("P9 第三方程序连不上时拿到的是**可读的 HostLinkError**"
                  "（不是裸 URLError，调用方才有线索排查）",
                  "连不上" in str(e) and "服务在跑吗" in str(e), str(e)[:160])
        except Exception as e:                                       # noqa: BLE001
            check("P9 第三方程序连不上时报错可读", False,
                  f"抛的是 {type(e).__name__} 而不是 HostLinkError：{e}")

        # ---------- P10 文档里的接入示例必须**真的能跑** ----------
        # 上一版 README 写的是 `HostLink(port=8765, token=…)` 与 `HostLink.from_env()` ——
        # 两样都不存在，照抄就报错。**没人跑的文档示例等于没有**，所以把它变成判据。
        rd = (HERE / "README.md").read_text(encoding="utf-8")
        miss, bad = scan_api(rd, HostLink)
        check("P10 README 的 python 示例里，每个 `h.xxx` / `HostLink.xxx` 都真实存在、"
              "构造函数关键字都在签名里（照抄即可运行）",
              not miss and not bad and "```python" in rd,
              f"缺失={miss} 签名外关键字={bad}")
        fake = ("```python\n"
                "h = HostLink(port=8765, token=\"x\")\n"
                "print(h.status())\nprint(HostLink.from_env())\nprint(h.archive())\n"
                "```")
        miss2, bad2 = scan_api(fake, HostLink)
        check("P10b 反例：把旧的坏示例（`port=` 构造 + `from_env()` + 已删的 `archive()`）"
              "喂给同一个检查器，必须全部报出来（否则 P10 是安慰剂）",
              set(miss2) == {"HostLink.from_env", "h.archive"} and bad2 == ["port"],
              f"缺失={miss2} 签名外关键字={bad2}")

        # ---------- P11 配对的「网址 / token / 二维码」三件套 ----------
        # 用户口径：对外接口要能给出**配对的网址、token 和二维码**。前两样是字符串，比一下就
        # 完事；二维码是**图**，"给出来了"与"扫得出来"完全是两件事 —— 所以这一组要把它交给
        # **另一个进程、另一套实现**（App 里那个 zxing jar）真的解一遍。
        hcap = h.capabilities()
        con = hcap.get("console") or {}
        feats = con.get("features") or []
        beyond = con.get("beyond_console") or []

        check("P11a capabilities 声明了「控制台功能 ↔ 接口」映射表"
              "（features + beyond_console，第三方照它就能替代面板）",
              len(feats) >= 7 and len(beyond) >= 2 and bool(hcap.get("routes")),
              f"features={len(feats)} beyond={len(beyond)} routes={len(hcap.get('routes') or [])}")

        # 从**声明自身**推导：每个 `hostlink` 方法名都要能在**运行时实例**上找到。
        # （test_server 的 A8e 查的是源码；这条查实例 —— 两条一起才等于"照着声明就能调"。）
        hl_miss = sorted({f"{f.get('id')}→{n}()" for f in feats + beyond
                          for n in (f.get("hostlink") or []) if not hasattr(h, str(n))})
        check("P11b 声明里的 hostlink 方法名在**真实例**上都能找到"
              "（读不到 / 对不上 ⇒ 第三方照声明写代码就会 AttributeError）",
              not hl_miss, f"实例上缺失：{hl_miss}")
        check("P11c 反例：喂一个不存在的方法名 ⇒ 同一个探针必须判否（否则 P11b 是安慰剂）",
              [n for n in ("pair", "qr_png", "nope") if not hasattr(h, n)] == ["nope"],
              "反例探针看不清")

        pr = h.pair()
        check("P11d `pair()` 一次给全三件套：网址 + token + 二维码（token 与接线时一致）",
              pr.get("token") == TOKEN
              and pr.get("url", "").endswith(f"token={TOKEN}")
              and str(pr.get("pair_uri", "")).startswith("m0probe://pair?")
              and bool(pr.get("hosts")),
              json.dumps({k: pr.get(k) for k in ("url", "pair_uri", "hosts")},
                         ensure_ascii=False)[:280])

        qt = Path(tempfile.mkdtemp(prefix="qr_"))
        png, qr_err = qt / "pair.png", ""
        try:
            h.qr_png(str(png))
        except HostLinkError as e:
            qr_err = str(e)
        check("P11e `qr_png()` 把接口给的二维码落成 PNG 文件"
              "（服务端给不出时，错误信息要说清**为什么**，而不是抛一个无关的 TypeError）",
              png.exists() and png.stat().st_size > 100, qr_err or "文件没写出来")

        cp, why = zxing_classpath(qt)
        if cp:
            ok, got = zxing_decode(cp, png)
            check("P11f **服务端接口真实产出的 PNG** 能被 App 那个 zxing jar"
                  "（另一个 JVM 进程、另一套实现）解出，且逐字符等于接口给的配对码"
                  "（「给出来了」≠「扫得出来」）",
                  ok and got == pr.get("pair_uri"),
                  (f"解出 {got!r}，期望 {pr.get('pair_uri')!r}" if ok
                   else f"解码失败：{got}"))
            # 反例：换一个地址的码，解出来**必须不同** —— 证明解码器真的在读这张图，
            # 而不是读缓存 / 常量（否则 P11f 的比对是安慰剂）。
            alt = h.pair(host="10.1.1.1")
            alt_png = qt / "alt.png"
            try:
                h.qr_png(str(alt_png), host="10.1.1.1")
            except HostLinkError:
                alt_png = None
            if alt_png and alt_png.exists():
                ok2, got2 = zxing_decode(cp, alt_png)
                check("P11g 反例：换一个地址的码解出来必须**不同**"
                      "（否则 P11f 只是在比两个常量）",
                      ok2 and got2 == alt.get("pair_uri")
                      and got2 != pr.get("pair_uri"),
                      f"解出 {got2!r}，另一个码是 {alt.get('pair_uri')!r}")
            else:
                check("P11g 反例：换地址的码必须能生成", False, "生成失败")
        else:
            # 依赖缺失 ⇒ 判 FAIL 而**不是**静默跳过：本轮要证的正是"二维码能被扫"，
            # 证不了就不能算通过（与 P6 对 ffmpeg 的态度一致）。
            check("P11f 二维码 PNG 能否被 zxing 解出 —— **判不了**（依赖缺失）", False, why)

        # ---------- P12 用 hostlink「代替面板」走一遍 ----------
        # 需求原话："对外接口要包括**在现有项目控制台的全部功能**"。这一组就是把它当真：
        # 面板上每个动作，都用第三方程序（只用标准库的库）做一遍。
        ov = h.overview()
        check("P12a `overview()` 拿到面板**顶部环境行**"
              "（profile / 数据目录 / WS 客户端数 / 启动时刻）",
              bool(ov.get("data_dir")) and "profile" in ov and "ws_clients" in ov,
              json.dumps(ov, ensure_ascii=False)[:220])

        sid2 = "2026-09-27_0110_itop2"
        js = h.start(session_id=sid2, device_serial="IOP")
        check("P12b `start()` 等价于面板「▶ 开始采集」（会话已建、默认 plan 不抽帧）",
              js.get("session_id") == sid2
              and (js.get("plan") or {}).get("interval_ms") == 0,
              json.dumps(js, ensure_ascii=False)[:220])

        mine = next((x for x in h.sessions() if x.get("session_id") == sid2), None)
        check("P12c `sessions()` 拿到面板会话表的内容（含 store 账本 / 门控 / 镜头）"
              "—— 面板那 11 列数据第三方都能读",
              mine is not None and "store" in mine and "start_gate" in mine
              and "lens" in mine, str(mine)[:240] if mine else "会话不在列表里")

        st = h.status()
        check("P12d `status()` 是面板的**超集**（gate 三态 + store 账本 + capture 同口径）",
              {"gate", "store", "capture"} <= set(st),
              json.dumps(sorted(st), ensure_ascii=False)[:200])

        check("P12e `mjpeg_url()` / `thumb_url()` 都是可直接消费的 URL"
              "（前者交给 cv2/ffmpeg；后者与**面板轮询用的是同一条**）",
              h.mjpeg_url().startswith("http://") and "token=" in h.mjpeg_url()
              and "/thumb/latest" in h.thumb_url(sid2),
              f"{h.mjpeg_url()} / {h.thumb_url(sid2)}")

        js2 = h.stop(session_id=sid2)
        check("P12f `stop()` 等价于面板「■ 停止」", js2.get("session_id") == sid2,
              json.dumps(js2, ensure_ascii=False)[:160])

        led = h.ledger(sid2)
        check("P12g `ledger()` 与 `sessions()` / `status()` 是**同一份数据**"
              "（账本只允许有两个出口，且口径必须一致）",
              isinstance(led, dict) and "state" in led, str(led)[:200])

        # ---------- P13 实时道（`capture_mode=live`）：**跨进程验它是彩色** ----------
        #
        # 为什么必须换栈验：`test_server.py` 的 A9 只证明"接口收下了 JPEG 字节"。
        # 本轮的核心承诺却是 **这一段是彩色的** —— 只有把真产出的画面**解出来比像素**
        # 才算证到。而且必须配反例：同一套判据用在**灰度**帧上要判否，
        # 否则"判彩色"可能只是"任何 JPEG 都算彩色"。
        def colour_report(frame) -> dict:
            """(通道数, 三通道均值的极差, 左半 BGR 均值, 右半 BGR 均值)。

            ⚠ 判彩色**不能只看通道数**：灰度 JPEG 被 cv2 解出来**也是 3 通道**
            （三通道值相等）⇒ 必须比**通道之间的差异**。
            """
            import numpy as np
            a = np.asarray(frame)
            if a.ndim != 3 or a.shape[2] < 3:
                return {"ch": 1, "spread": 0.0, "left": None, "right": None}
            means = [float(a[:, :, i].mean()) for i in range(3)]
            hw = a.shape[1] // 2
            return {"ch": int(a.shape[2]), "spread": max(means) - min(means),
                    "left": [round(float(a[:, :hw, i].mean()), 1) for i in range(3)],
                    "right": [round(float(a[:, hw:, i].mean()), 1) for i in range(3)]}

        def make_colour() -> bytes:
            """一张**确定的**彩色夹具：左半纯红、右半纯蓝（BGR）。

            左右两半刻意选互补色相 —— "通道串了 / 只取了一个平面 / YUV 矩阵用错"
            这三种典型缺陷都会让它对不上，而不是"看着像彩色就算"。
            """
            import cv2
            import numpy as np
            img = np.zeros((THUMB_H, THUMB_W, 3), np.uint8)
            img[:, :THUMB_W // 2] = (0, 0, 255)       # 左：纯红（BGR）
            img[:, THUMB_W // 2:] = (255, 0, 0)       # 右：纯蓝
            ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            if not ok:
                raise RuntimeError("cv2.imencode 失败")
            return buf.tobytes()

        LIVE_SID = "2026-10-04_0140_iolive"
        js_live = h.start(session_id=LIVE_SID, device_serial="IOP", capture_mode="live")
        check("P13a `start(capture_mode='live')` 从第三方库也能开这个模式",
              js_live.get("capture_mode") == "live",
              json.dumps(js_live, ensure_ascii=False)[:200])

        col_jpg = make_colour()

        def post_live(sid: str, seq: int, blob: bytes) -> int:
            """用**标准库**推一帧实时画面，返回 HTTP 码（不抛异常）。

            ⚠ 走**空代理表 opener**：urllib 默认吃 `HTTP_PROXY`，会把 127.0.0.1 的请求
            塞进代理并回一个 502（本轮实测踩到）。判据要验的是这个接口，不是这台机器的代理设置。
            """
            import urllib.error
            import urllib.request
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            req = urllib.request.Request(
                f"{base}/api/v1/live/frame", data=blob, method="POST",
                headers={"Content-Type": "image/jpeg", "X-Session-Id": sid,
                         "X-Seq": str(seq), "X-Sensor-Timestamp-Ns": str(10 ** 12 + seq)})
            try:
                with opener.open(req, timeout=8) as resp:
                    return resp.status
            except urllib.error.HTTPError as e:
                return e.code
            except Exception:
                return 0

        posted = sum(1 for i in range(3) if post_live(LIVE_SID, i, col_jpg) == 200)
        check("P13b 用**标准库**（urllib）推 3 帧彩色画面：设备面接口不依赖任何第三方库"
              "（手机那一侧用的也是标准库 HTTP 栈）",
              posted == 3, f"posted={posted}")

        frames_c = []
        for _w in range(6):
            frames_c = read_cv2(h.mjpeg_url(), want=1, timeout=8)
            if frames_c:
                break
            time.sleep(0.4)
        rc = colour_report(frames_c[0]) if frames_c else {"ch": 1, "spread": 0.0,
                                                          "left": None, "right": None}
        check("P13c **live 通道里读到的画面确实是彩色**：通道间均值差显著，"
              "且左半偏红（R 高 B 低）、右半偏蓝（B 高 R 低）—— 与推过去的夹具一致"
              "（「能读到一个 JPEG」 ≠ 「读到的就是那张彩图」）",
              rc["ch"] == 3 and rc["spread"] > 25 and rc["left"] is not None
              and rc["left"][2] - rc["left"][0] > 60
              and rc["right"][0] - rc["right"][2] > 60,
              json.dumps(rc, ensure_ascii=False))

        import cv2 as _cv2
        import numpy as _np
        rg = colour_report(_cv2.imdecode(_np.frombuffer(make_thumb(), _np.uint8),
                                         _cv2.IMREAD_COLOR))
        check("P13d 反例：同一套判据用在**灰度**夹具上必须判否"
              "（灰度 JPEG 解出来**也是 3 通道** ⇒ 只看通道数会恒判彩色；"
              "这一条就是 P13c 的安慰剂检验）",
              rg["ch"] == 3 and rg["spread"] < 8, json.dumps(rg, ensure_ascii=False))

        mine_l = next((x for x in h.sessions() if x.get("session_id") == LIVE_SID), None)
        check("P13e 第三方库读到的 live 会话口径与 A9d 一致：`store.applicable=false`、"
              "`delivery_complete=null`（**不适用**）—— 换一套栈结论不变，才不是同进程的巧合",
              mine_l is not None and mine_l.get("capture_mode") == "live"
              and mine_l["store"].get("applicable") is False
              and mine_l["delivery_complete"] is None,
              json.dumps({k: (mine_l or {}).get(k) for k in
                          ("capture_mode", "delivery_complete")}, ensure_ascii=False))

        DLV_SID = "2026-10-04_0140_iodlv"
        h.start(session_id=DLV_SID, device_serial="IOP")
        code_x = post_live(DLV_SID, 0, col_jpg)
        check("P13f 反例：deliver 会话收到实时帧必须 400 `mode_conflict`"
              "（跨进程也要成立 —— 模式互斥是**服务端**的事，不是靠 App 自觉）",
              code_x == 400, f"HTTP {code_x}")

        # ---------- P14 环境里的代理不得影响接入（第 28 轮新增） ----------
        #
        # 为什么要有这条：本轮实测踩到 —— shell 里被注入 `HTTP_PROXY` 后，本文件从
        # **42/42 掉到 38/40**，两条 FAIL（P1 / P9）报的都是
        # `HTTP 502: upstream connect failed ... (os error 10061)`，
        # 而服务好端端在跑。
        # ⚠ **真凶是 urllib**（`getproxies()` 会读到本机代理），不是 aiohttp
        #（aiohttp 3.14 的 `trust_env` 默认是 `False`，已核实）。
        # 现场机器上装了 Clash / v2ray / 公司代理的人**必然**撞上，
        # 且**排查方向会被彻底带偏**（看起来像"服务挂了/端口错了"）。
        #
        # 做法：**自己把那条前提造出来**（注入式，本项目铁律）——
        # 注入一个"死代理"，然后要求接入路径照常工作。
        # ⚠ 反向用例**必须真判否**：用 `ProxyHandler()`（无参 = 读环境）现场新建 opener
        # 去请求同一地址，必须失败 —— 否则正向用例可能只是"注入根本没生效"而真空通过。
        # （不能用 `urllib.request.urlopen`：它内部的 opener 是模块级缓存的，
        #   早先的调用已经把当时的环境烘焙进去了，注入不进去 ⇒ 反向用例会静默变正例。）
        import urllib.error
        import urllib.request

        saved_env = {k: os.environ.get(k) for k in
                     ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy")}
        try:
            for k in saved_env:
                os.environ[k] = "http://127.0.0.1:1"      # 一个必然连不上的死代理
            # 反向：现场新建的默认 opener（读环境）必须被死代理带偏 ⇒ 必须失败
            try:
                bad_op = urllib.request.build_opener(urllib.request.ProxyHandler())
                with bad_op.open(f"{base}/api/v1/capabilities", timeout=6) as rr:
                    bad_ok = (rr.status == 200)
                bad_note = f"竟然通了（HTTP {bad_ok}）"
            except Exception as e:                                     # noqa: BLE001
                bad_ok = False
                bad_note = f"{type(e).__name__}: {str(e)[:80]}"
            check("P14b 反向：注入死代理后，**按默认方式**（读环境）建的 opener 必须被带偏 ⇒ 必须失败"
                  "（否则 P14a 可能只是「注入根本没生效」而真空通过）",
                  not bad_ok, bad_note)
            # 正向：真接入路径必须照常工作
            try:
                caps_p = h.capabilities()
                pos_ok = isinstance(caps_p, dict) and bool(caps_p.get("lanes"))
                pos_note = f"lanes={sorted((caps_p or {}).get('lanes', {}))}"
            except Exception as e:                                     # noqa: BLE001
                pos_ok = False
                pos_note = f"{type(e).__name__}: {str(e)[:120]}"
            check("P14a **注入死代理后，第三方库照样能接入**（`HostLink` 必须绕过代理）"
                  "—— 行为只该取决于地址，不该取决于这台机器上装了什么软件",
                  pos_ok, pos_note)
        finally:
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        h.stop(session_id=LIVE_SID)
    finally:
        ev_stop.set()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    n_pass = sum(1 for _, ok, _ in results if ok)
    print(f"\n跨程序通信验证：{n_pass}/{len(results)} 通过")
    if n_pass != len(results):
        print("--- 服务端日志尾部 ---")
        print("\n".join(list(srv_log)[-30:]))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
