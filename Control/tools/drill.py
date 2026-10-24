"""无硬件端到端演练 + 可测判据验收。

    python tools/drill.py                # 起一个临时服务，跑完整条链路，逐条判自动判据
    python tools/drill.py --sim          # 额外起上游鼠标模拟器，跑需要活链路的那些
    python tools/drill.py --port 8899
    python tools/drill.py --keep         # 跑完不删临时会话（自己进去看产物）

它做的事：
    ① 起 Control 服务
    ② 逐条判：健康/缓存/窄门控/时序/回滚/时间轴守恒/4 灯三态/视频产物与时间轴 …
       （需要活链路的走 `--sim`，没有就 SKIP —— **不假装通过**）
    ③ 把结果与产物路径打出来

**为什么要有这个脚本**：上游两个模块各自都有自己的取证体系，本层如果只靠"我手动点过一遍"，
下次谁改了适配器都不会有人发现。判据要做成能一条命令重跑的，否则它们只是文档里的一段话。

⚠️ 改版二十四：手机相机整块下线，本文件里关于它的取证（假相机服务、代启、MJPEG 反代、
合成帧 + stride 切行、离线导入 + 赛后批量、定位精度）连同被测对象一起删了。
删掉的地方都留了一段注释说明"测的是什么、为什么没有对象了、守的东西现在钉在哪"。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from control.store import _rmtree_quiet      # noqa: E402  「绝不抛异常」的删除器
from control.maze_rec import FORMAT as REC_FORMAT   # noqa: E402  导入临时记录要用它

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

    ⚠️ 失败时**必须报出最后一次异常**。只写"服务没起来"的话，三种完全不同的
    故障长得一模一样：端口被第三方占（服务自己根本没绑上）、服务绑上了但
    `127.0.0.1` 连不通、以及**被代理接管**（`urlopen` 走了系统代理，回 403/407）。
    实测为了一个"S1 失败"排查了两轮，最后发现是端口被别人占着。
    """
    t0 = time.time()
    last = "(一次都没探到)"
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(base + "/api/v1/health", timeout=5) as r:
                if r.status == 200:
                    return True
                last = f"HTTP {r.status}"
        except Exception as e:                                # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        time.sleep(0.4)
    print(f"    [wait_up] {timeout:.0f}s 内没通：{base}/api/v1/health")
    print(f"              最后一次异常：{last}")
    print(f"              查端口占用：netstat -ano | findstr :{base.rsplit(':', 1)[-1]}")
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


#: 本层消费的两个上游目录（相对本工程根）。判据 I9 用。
#: 改版二十四：PhoneCamera 不再被消费（相机整块下线）⇒ 从这份清单里去掉。
UPSTREAMS = ("RemoteControl", "Auto Locate")


def _upstream_dirty() -> dict:
    """两个上游目录**各自范围内**的 git status 行集合（`-- .` 限定）。

    ⚠️ `-- .` 不可省：这两个目录**都不是独立仓库**（单仓库 + 上游子目录），
    不加 pathspec 时 `git status` 报的是整个仓库 —— 于是"上游脏了"常常是
    "我刚改了 Control"，这条判据就成了永远红、且指错方向的噪音。
    """
    out = {}
    for name in UPSTREAMS:
        p = ROOT.parent / name
        if not p.is_dir():
            out[name] = None                              # 不在 ⇒ 判不了，别当成"干净"
            continue
        r = subprocess.run(["git", "-C", str(p), "status", "--porcelain", "--", "."],
                           capture_output=True, text=True)
        out[name] = set(r.stdout.splitlines())
    return out


# ⚠️ 改版二十四删掉的取证装置（不要把这段历史一起删）：
# `_TINY_JPEG` + `_fake_camera()` 曾在这里造一个"status 正常答、control/start 故意 500、
# `/stream/mjpeg` 发几帧真 JPEG"的假 PhoneCamera，用来给判据 I5（开会话中途失败要回滚）
# 和 I26（MJPEG 反代的 boundary 必须与上游一致）制造着力点。
# 手机相机整块下线后这两条判据**没有被测对象**了，装置也就一起删了 ——
# 把"给已删除功能准备的假上游"留在验收工具里，下一次改动会以为它还该被用。

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
    """I15 / I16 / I17 / I22 / I23 / I27 / I28 / I30~I33 —— 需要活链路的判据，走模拟器。"""
    # ---- 先确认服务**此刻还答得上话**。这一步不是仪式：
    # 如果事件循环已经停摆（曾经因为"服务进程的 stdout 管道写满"整台服务卡死），
    # 后面每个请求都只会超时，而超时信息完全指不到真正的原因。这里先用短超时探一下，
    # 不健康就把服务自己的日志尾巴打出来 —— 让"为什么走不下去"自答。
    h, hc, hdt = api(base, "/api/v1/health", timeout=8)
    if hc != 200:
        record("S6", "FAIL",
               f"服务已不健康（HTTP {hc}，{hdt:.1f}s）⇒ 后面的判据全判不了。"
               f"服务日志尾巴：\n{drain(ROOT / '.tmp' / 'drill' / 'service.log', 1200)}")
        for cid in ("I15", "I16", "I17", "I22", "I23", "I27", "I28", "I30", "I31", "I32", "I33", "I40", "I41", "I42", "I43", "I44", "I45"):
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
        for cid in ("I15", "I16", "I17", "I22", "I23", "I27", "I28", "I30", "I31", "I32", "I33", "I40", "I41", "I42", "I43", "I44", "I45"):
            record(cid, "FAIL", f"连不上活链路 ⇒ 判不了（`mouse/connect` HTTP {c}）")
        return
    record("S6", "PASS", f"服务端已连上模拟器：{(r.get('data') or {}).get('device')}")

    # ⚠️ 改版三十一：连上**不再自动拉取**（`auto_stream: false`），而下面这几条
    #    （I16 读参数、I15 看记录样本、I17 看丢帧计数）都**需要链路在跑**：
    #    停流时上游 `GET ALL` 会 3 s 超时 ⇒ 参数表空，看起来像"固件没参数"。
    #    所以这里显式开流，并把档设回停等（n=1）—— 与"页面按需拉取"不矛盾，
    #    这一段本来就是"跟上游更新"的联调段。
    api(base, "/api/v1/mouse/stream/batch", "POST", {"n": 1, "t_ms": 100})
    _rs0, _cs0, _ = api(base, "/api/v1/mouse/stream", "POST", {"on": True})
    record("S6b", "PASS" if _cs0 == 200 and (_rs0.get("data") or {}).get("on") else "FAIL",
           f"联调段显式开流（n=1 停等）⇒ HTTP {_cs0}、on={( _rs0.get('data') or {}).get('on')}")

    # ---- I16：参数分组必须与**上游 PARAM_GROUPS / PARAM_META 现算一致**（**不写死组数/项数**
    #          —— 2.4.0 是 24 项三组、2.5.0 起 41 项四组（+「运动控制」id 39–55，仅远程档
    #          生效）。组名、项数、参数 id 全部从上游 host.protocol.commands 现取，
    #          上游再扩容时这条判据不用动（I22 的同款约定）。
    g, c, _ = api(base, "/api/v1/mouse/params/grouped")
    d = g.get("data") or {}
    groups = d.get("groups") or {}
    les = groups.get("课程调参") or {}
    # 课程组（只老上游有）不参与比对；其余组与上游逐组逐项对。
    got = {k: v for k, v in groups.items() if k != "课程调参"}
    pages = {k: (list(v.values())[0].get("flash_page") if v else None)
             for k, v in got.items()}
    vias = {k: (list(v.values())[0].get("write_via") if v else None)
            for k, v in got.items()}
    # 上游静态表（`host_dir` 的取法与 I22 同一处真相）。
    want, want_ids, want_meta = None, {}, {}
    try:
        host_dir = ROOT.parent / "RemoteControl" / "host"
        if str(host_dir) not in sys.path:
            sys.path.insert(0, str(host_dir))
        from host.protocol.commands import (                 # type: ignore
            PARAM_GROUPS as _PG, PARAM_IDS as _PI, PARAM_META as _PM)
        want, want_ids, want_meta = {}, _PI, _PM
        for n, gn in _PG.items():
            want.setdefault(gn, []).append(n)
    except Exception as e:                                    # noqa: BLE001
        record("I16", "FAIL", f"取不到上游 `PARAM_GROUPS`：{type(e).__name__}: {e}")
    # 页号是**固件分区**的事实：生产侧 4 组同落 page 115（2.4.0 起单页）。
    # 说明列：上游 PARAM_META 的 desc 经后端清洗后**每项非空**、且不得残留
    # ⚠️ 警示符 / —— 破折号 / 反引号（它们会原样上界面，违反界面文案纪律）。
    bad_desc = [(n, it.get("desc") or "")
                for gn in got.values() for n, it in gn.items()
                if (not (it.get("desc") or "").strip()
                    or "⚠" in (it.get("desc") or "")
                    or "——" in (it.get("desc") or "")
                    or "`" in (it.get("desc") or ""))]
    ok = (c == 200 and want is not None and les == {})
    if want is not None:
        ok = (ok
              and set(got) == set(want)                       # 组名一致
              and all(set(got.get(gn) or {}) == set(ns) for gn, ns in want.items())
              and all((got.get(gn) or {}).get(n, {}).get("id") == want_ids[n]
                      for gn, ns in want.items() for n in ns)  # 参数 id 逐项一致
              and all(p == 115 for p in pages.values() if p is not None)
              and len(set(v for v in vias.values() if v)) <= 1  # 同一条写入通道
              and not bad_desc
              and d.get("count") == sum(len(ns) for ns in want.values()))
    parts = " + ".join(f"{gn} {len(got.get(gn) or {})}" for gn in (want or {}))
    record("I16", "PASS" if ok else "FAIL",
           f"{d.get('count')} 项 {len(got)} 组（{parts}），与上游 PARAM_GROUPS 逐组逐项一致；"
           f"page 115 同一条写入通道；说明列来自上游 PARAM_META 且无界面禁字符"
           if ok else
           f"HTTP {c}；实际组 {sorted(got)} / 期望组 {sorted(want or {})}；"
           f"各组项数 {{{', '.join(f'{k}: {len(v)}' for k, v in got.items())}}}；"
           f"pages={pages} vias={vias}"
           + (f"；说明列违规 {bad_desc[:3]}" if bad_desc else ""))

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
    head("14c. 顺带验：课程调参实验在本层已随上游删除（应为明确拒绝，非 5xx）")
    r, c, _ = api(base, "/api/v1/lesson/run", "POST",
                  {"exp": 2, "args": {"v": 60, "dist": 100, "ms": 3000},
                   "fetch_log": False}, timeout=120)
    res = r.get("data") or {}
    msg = json.dumps(r.get("error") or r, ensure_ascii=False)[:180]
    # 上游 2.4.0 已删除课程层 ⇒ 本层必须**明确拒绝**而不是照样答 200 假成功、也不能吐 500。
    # 允许 404（路由没了）或 409/4xx（路由在但明确说不支持）——总之要指出"上游已删"。
    gone = c in (404, 409, 410, 422) or (c == 400)
    says_deleted = any(k in msg for k in ("2.4.0", "删除", "课程", "lesson", "不存在", "不支持"))
    record("S7", "PASS" if (gone and says_deleted) else "FAIL",
           f"课程实验端点：HTTP {c} ⇒ " +
           (f"明确拒绝且指出了原因（{msg}）" if (gone and says_deleted)
            else f"应为「已删除」类拒绝，实际 {msg}"))

    # ---- I22 / I23 **放在最后**：跑迷宫会把设备留在"迷宫里"（`mouse_task` 停在
    # MAZESEARCH，运动互斥判据因此一直忙）⇒ 它会挡住后面任何运动类判据。
    # 这不是绕开问题，而是这条链路的**真实次序**：跑迷宫是"整场"的事，跑完就该收工。
    # I22：**统一帧单流 + 码表零设备访问 + 拉取节奏可控**
    # 这条判据跨了三次上游改版，所以它现在要守四件事：
    #   ① **拉取关着**时码表照样答得出全部通道 + 迷宫码表 ⇒ 证明它**零设备访问**
    #      （否则未连接时页面画不出"要读什么"）；
    # ⚠️ **通道数不许写死**：2.0.0 是 39、2.3.0 起是 49（前墙双档 3 + 冲刺状态 7）。
    #    这里原写成 `len(chans) == 39` ⇒ 上游一升到 2.3.0，这条判据就**假红**，而根因
    #    是"判据钉错了数"而不是产品错（纪律 13：判据红先怀疑判据）。改成**与上游
    #    `UNIFIED_CHANNELS` 逐项相等**（见下面的 `chans_ok`），数字由协议层自己说。
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
    # 通道要与协议层常量**逐项一致**，不是"有个列表就行"，也**不写死个数**。
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
    chans_ok = bool(want) and chans == want
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
           f"chans={len(chans)}（与上游 UNIFIED_CHANNELS({len(want)}) 一致={chans_ok}）；"
           f"streams={list(streams)} frames={sorted(frames)} one_stream={one_stream}；"
           f"maze_ok={maze_ok}；unified 开 {n1}→{n2} bps={bps}，"
           f"关 {m1}→{m2}（stopped_ok={stopped_ok}）；旧端点状态码={gone}")

    # ---- I23：无记忆走迷宫 —— **统一帧 + 主机差分**（上游 2.0.0）
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

    # ---- I27：无记忆走迷宫「记录 → 保存 → 读回」往返 + 重建（含两条负向）
    # 这条判据守的是"**从发车到停止的数据真的落下来了、而且读回来是同一样东西**"：
    #   · 记录器拿得到帧（它订阅上游 `Event.TELEMETRY`，**不依赖浏览器**）；
    #   · 落盘的文件**自洽**：`channels` 与上游 `UNIFIED_CHANNELS` 逐项相同、
    #     `frames`/`t_ms`/`integrity.frames` 三者一致、
    #     每行长度都等于通道数（列名与值对不齐是最容易出的错，且**看着像对的**）；
    #   · 重建得出来：`wall` 有格子被访问过（否则中栏是空白）；
    #   · **负向 A**：`records/{name}` 读一个不存在的名字必须 **404**（不许静默回空）；
    #   · **负向 B**：**空记录不许落盘** —— 立刻 start→stop（一帧都没来）必须回
    #     `ok=False` 且**不新增文件**。理由：零帧文件对复盘毫无价值，只会留垃圾。
    #
    # ⚠️ **通道数不许写死**：2.0.0 是 39、2.3.0 起是 49。改成与上游 `UNIFIED_CHANNELS`
    #    逐项相等（`chans` 由记录文件带来，`want_chans` 由协议层取），数字由上游自己说。
    #
    # ⚠️ 这里**不停拉取**（I23 已经把它开着），否则记录器会"什么都没有，而且一声不响"。
    # 先把 I23 发车时**自动开着的那一次**记录收掉，好让下面这次从零开始、可复算。
    try:
        want_chans = list(UNIFIED_CHANNELS)         # 复用 I22 取到的那份协议层清单
    except Exception:                               # noqa: BLE001
        want_chans = list(want)
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
    # ⚠️ 还必须 `auto_pull: false`（改版三十三）：`record/start` 默认会**自动开流**
    #    （否则人点【开始记录】得到一条空记录）。不显式关掉的话，这里刚关的流
    #    会被 start**重新打开**，0.6 s 里就有帧 ⇒ 这段变成"一条正常记录落盘"，
    #    而判据还写着"应 ok=False"。那不是产品坏了，是这段测试的前提被改了。
    api(base, "/api/v1/mouse/stream", "POST", {"on": False})
    time.sleep(1.5)                     # 等在途那一条落定（否则会算进帧数 ⇒ 假绿）
    api(base, "/api/v1/mouse/maze/record/start", "POST",
        {"why": "drill-empty", "auto_pull": False})
    time.sleep(0.6)
    empty_r, _, _ = api(base, "/api/v1/mouse/maze/record/stop", "POST", {})
    empty_saved_ok = not (empty_r.get("data") or {}).get("ok")   # 应回 ok=False
    list1, _, _ = api(base, "/api/v1/mouse/maze/records")
    names1 = {r.get("name") for r in ((list1.get("data") or {}).get("records") or [])}
    no_new_ok = (names1 - names0) <= {name} if name else (len(names1) == len(names0))

    i27_ok = (c_rec == 200 and c_stop == 200 and c_get == 200
              and live_ok and saved.get("ok") and name
              and bool(want_chans) and chans == want_chans
              and counts_ok and rows_ok and wall_ok
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
           f"get HTTP {c_get}：chans={len(chans)}（与上游 {(len(want_chans))} 一致={chans == want_chans}）"
           f"frames={len(fr)} t_ms={len(tt)} "
           f"integrity={integ}；counts_ok={counts_ok} rows_ok={rows_ok} wall_ok={wall_ok}；"
           f"负向：不存在→{c_bad}（应 404）；空记录 ok={(empty_r.get('data') or {}).get('ok')}"
           f"（应 False）新增={names1 - names0}")

    # ---- I28：收车 / 复位 / 急停（上游 2.1.1 / 2.2.0 / 2.2.1）
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
    # **第二层：三种失败要能分得开，且收车/复位的往返要成立。**
    #   · 急停中发车 ⇒ `ERR_ESTOP`（**不是** `ERR_BUSY`，也不是 OK）；
    #   · 已在跑再发车 ⇒ `ERR_BUSY`；
    #   · 收车 ⇒ 必须等到**新帧**才判（旧帧会把成功报成失败），所以 `reliable`
    #     必须是 `True`（模拟器上收得住；三态里的 `None` 表示"没等到帧、判不了"）；
    #   · ★ **收车后必须能再发车** —— 2.1.1 修的正是这条（以前"发过一次车就只能断电"）；
    #   · 复位 ⇒ `verified` 必须与复位原因一致（`rst == "SW"`），**不许只看 accepted**。
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

    # ⑥ 复位（把车弄停 ⇒ 之后才能验"静止时收车真能收住"）
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

    # ⑦b 拉取档 n（改版二十六）—— **正反两面都要点**（铁律 46）：
    #     合法值必须真的落到上游档位上；越界必须 **400**，**不许夹值**
    #     （夹了就是"界面说成功、设备按别的值跑"，比报错难查得多）。
    #     最后**恢复 n=1（停等）**：后面的判据看帧率相对量，留在批式档会把读数改掉。
    rb1, cb1, _ = api(base, "/api/v1/mouse/stream/batch", "POST", {"n": 3}, timeout=25)
    bv1 = rb1.get("data") or {}
    batch_pos = (cb1 == 200 and bv1.get("n") == 3 and bv1.get("mode") == "pace")
    rb2, cb2, _ = api(base, "/api/v1/mouse/stream/batch", "POST", {"n": 99}, timeout=15)
    batch_neg = (cb2 == 400)
    rb3, cb3, _ = api(base, "/api/v1/mouse/stream/batch", "POST", {"n": 1}, timeout=25)
    bv3 = rb3.get("data") or {}
    batch_back = (cb3 == 200 and bv3.get("n") == 1)
    # 再核一次"这个 n 是**唯一来路**"：`caps.mouse.pull` 要跟着变。
    # ⚠️ 读的是 `/api/v1/caps`（**不是** `/health`）：`pull` 是**能力/参数**，
    #    与 `firmware`/`unified_channels`/`stream_mode` 同一族，就住在 caps 里。
    #    先前把判据钉在 `/health` 上，实测拿到 None —— 那是判据写错了地方，不是产品没实现。
    cr, cc, _ = api(base, "/api/v1/caps")
    pull_now = (((cr.get("data") or {}).get("mouse") or {}).get("pull") or {})
    pull_aligned = (cc == 200 and pull_now.get("n") == 1 and pull_now.get("fps") is None)
    i40_ok = batch_pos and batch_neg and batch_back and pull_aligned
    record("I40", "PASS" if i40_ok else "FAIL",
           f"每批帧数 n：设 3 ⇒ HTTP {cb1}、档位 n={bv1.get('n')}、"
           f"门面 mode={bv1.get('mode')!r}（应为 pace）、fps={bv1.get('fps')!r}；"
           f"越界 n=99 ⇒ HTTP {cb2}（应为 **400**，不许夹到 16）；"
           f"恢复 n=1 ⇒ HTTP {cb3}、n={bv3.get('n')}、fps={bv3.get('fps')!r}"
           f"（停等 ⇒ fps 必须是 None，不能拿批式公式算出的 10 Hz 冒充）；"
           f"`caps.mouse.pull` = n={pull_now.get('n')!r} kind={pull_now.get('kind')!r}"
           f"（界面读的就是这一份）")
    # ⑦c 记录摘要 / 视频关联 / 逐批明细（改版二十七）—— **正反两面都要点**
    #     ① 摘要要有 9 列，且 `ledger`/`maze_seed` 必须守三态（老记录判不了就是 None）；
    #     ② 关联：合法会话 ⇒ 200 且列表里看得到；解除（显式 null）⇒ 干净删掉；
    #        坏 body ⇒ 400；不存在的会话 ⇒ 404。
    #     ③ 逐批明细：`/mouse/state` 的 `rate.unified.batches` 要有行（停等 n=1 也有 ——
    #        每条 TELEM 就是一批 1 帧）。
    rpaths, cpaths, _ = api(base, "/api/v1/paths")
    vroot = Path(str(((rpaths.get("data") or {}).get("video_out_root") or "")))
    rrec, crec, _ = api(base, "/api/v1/records?n=3")
    rec_rows = (rrec.get("data") or {}).get("records") or []
    rec_keys = {"id", "start", "dur_s", "frames", "launch", "launch_at_s",
                "ledger", "gap", "maze_seed", "dir", "video_sid"}
    rec_ok = (crec == 200 and bool(rec_rows)
              and rec_keys.issubset(set(rec_rows[0].keys())))
    # 三态：`ledger` 只许 True/False/None；`maze_seed` **恒 None**（记录里没有种子，
    # 而 0 是合法种子 ⇒ 不许拿 0 顶）
    tri_ok = (all(r.get("ledger") in (True, False, None) for r in rec_rows)
              and all(r.get("maze_seed") is None for r in rec_rows))
    rsta, csta, _ = api(base, "/api/v1/mouse/state")
    rate = ((rsta.get("data") or {}).get("rate") or {}).get("unified") or {}
    bts = rate.get("batches")
    batch_ok = (csta == 200 and isinstance(bts, list) and bool(bts)
                and {"seq", "n", "ms", "hz"}.issubset(set(bts[0].keys())))

    link_ok = link_del = link_400 = link_404 = False
    tmp_vid = None
    try:
        if rec_rows and vroot.is_dir():
            nm = str(rec_rows[0].get("name") or "")
            tmp_vid = vroot / "drill-link-tmp"
            tmp_vid.mkdir(parents=True, exist_ok=True)
            (tmp_vid / "session.json").write_text("{}", encoding="utf-8")
            rl, cl, _ = api(base, f"/api/v1/records/{nm}/video", "POST",
                            {"video_sid": "drill-link-tmp"})
            link_ok = (cl == 200 and (rl.get("data") or {}).get("video_sid") == "drill-link-tmp")
            r2, c2, _ = api(base, "/api/v1/records?n=3")
            row2 = next((x for x in ((r2.get("data") or {}).get("records") or [])
                         if x.get("name") == nm), {})
            link_ok = link_ok and row2.get("video_sid") == "drill-link-tmp"
            # 解除：显式 null
            r3, c3, _ = api(base, f"/api/v1/records/{nm}/video", "POST",
                            {"video_sid": None})
            r4, c4, _ = api(base, "/api/v1/records?n=3")
            row4 = next((x for x in ((r4.get("data") or {}).get("records") or [])
                         if x.get("name") == nm), {})
            link_del = (c3 == 200 and row4.get("video_sid") is None
                        and not (vroot.parent / "maze" / (nm[:-len(".maze.json")]
                                                          + ".link.json")).exists())
            # 负向两条
            _r, c5, _ = api(base, f"/api/v1/records/{nm}/video", "POST", {})
            link_400 = (c5 == 400)          # 不带 video_sid ≠ 解除
            _r, c6, _ = api(base, f"/api/v1/records/{nm}/video", "POST",
                            {"video_sid": "definitely-not-a-session"})
            link_404 = (c6 == 404)
    finally:
        if tmp_vid is not None:
            try:
                (tmp_vid / "session.json").unlink()
            except OSError:
                pass
            try:
                tmp_vid.rmdir()
            except OSError:
                pass          # 测试不留产物；删不掉也不让判据崩（会体现在下面的结果里）

    i41_ok = rec_ok and tri_ok and batch_ok and link_ok and link_del and link_400 and link_404
    record("I41", "PASS" if i41_ok else "FAIL",
           f"记录摘要：{len(rec_rows)} 行、9 列齐={rec_ok}、"
           f"三态（ledger∈{{T,F,None}} 且 maze_seed 恒 None）={tri_ok}；"
           f"关联：写={link_ok} 解除={link_del} 空 body⇒400={link_400} "
           f"不存在会话⇒404={link_404}；"
           f"逐批明细：batches={len(bts) if isinstance(bts, list) else bts!r} 行，"
           f"首行 n={bts[0].get('n') if bts else None} ms={bts[0].get('ms') if bts else None}"
           f" hz={bts[0].get('hz') if bts else None}")

    # ⑦d 回放视图 / 参数派生量（改版二十九）
    #     ① `GET /records/{name}/replay` 要能直接画：样本非空、迷宫 N≥2、
    #        `sampled` 自报（抽样了就说出来）、坐标是**格中心**口径（+0.5）；
    #     ② 不存在的记录 ⇒ **404**（不是 409：换个名字就行 vs 重试也没用）；
    #     ③ 参数派生量：SPEEDL 必须**恒等于** TRACKL/100，且 `readable=false`
    #        （让界面自己除就会出现"改了 TRACKL、SPEEDL 还是旧值"）。
    r_row = None
    if crec == 200:
        cand = [x for x in ((rrec.get("data") or {}).get("records") or []) if not x.get("error")]
        r_row = cand[0] if cand else None
    rp_ok = False
    rp_detail = "记录目录里没有可用记录 ⇒ 判不了"
    if r_row:
        nm = str(r_row.get("name") or "")
        rd, crd, _ = api(base, "/api/v1/records/" + quote(nm) + "/replay")
        rdd = rd.get("data") or {}
        smp = rdd.get("samples") or []
        mz = rdd.get("maze") or {}
        centered = all(
            (s.get("posX") is None) or (abs(float(s["posX"]) - round(float(s["posX"]) - 0.5) - 0.5) < 1e-6)
            for s in smp[:20])
        rp_ok = (crd == 200 and len(smp) > 0 and int(mz.get("N") or 0) >= 2
                 and int(rdd.get("sampled") or 0) >= 1 and centered)
        rp_detail = (f"回放：HTTP {crd}、样本 {len(smp)}、迷宫 N={mz.get('N')}、"
                     f"抽样间隔 {rdd.get('sampled')}、坐标格中心口径={centered}")
        _, c404, _ = api(base, "/api/v1/records/nope-not-exist.maze.json/replay")
        rp_ok = rp_ok and c404 == 404
        rp_detail += f"；不存在的记录 ⇒ HTTP {c404}（应为 404）"

    pd, cpd, _ = api(base, "/api/v1/mouse/params/grouped")
    pdd = pd.get("data") or {}
    pg = pdd.get("groups") or {}
    base_g = pg.get("生产标定") or {}
    tl, sl = base_g.get("TRACKL") or {}, base_g.get("SPEEDL") or {}
    der_ok = False
    if tl.get("value") is not None and sl.get("value") is not None:
        der_ok = (abs(float(sl["value"]) - float(tl["value"]) / 100.0) < 1e-9
                  and sl.get("derived") is True and sl.get("readable") is False
                  and sl.get("derived_from") == "TRACKL")
    meta_ok = sum(int((v or {}).get("count") or 0)
                  for v in (pdd.get("groups_meta") or {}).values()) == int(pdd.get("count") or -1)
    p_ok = (cpd == 200 and der_ok and meta_ok)
    record("I42", "PASS" if (rp_ok and p_ok) else "FAIL",
           f"{rp_detail}；参数派生量：SPEEDL={sl.get('value')} 应等于 TRACKL/100="
           f"{(float(tl['value']) / 100.0) if tl.get('value') is not None else None}"
           f"、只读={sl.get('readable') is False}、groups_meta 合计与 count 一致={meta_ok}")

    # ⑦e 按需拉取（改版三十一）—— **六段，正反都有**
    #     ① 连上后**不该**自动拉（`auto_stream: false`）；
    #     ② 关着的时候改 N 要能改、且如实说"还没生效"（`effective=false`）；
    #     ③ 再开流必须**按新档开**（`applied_n`，不是先按 1 开再改）；
    #     ④ 人工停（不带 auto）照停 —— 那是排障手段；
    #     ⑤ 记录中"自动停"要被**扣留**（否则切个页面就把这一跑的遥测砍了）；
    #     ⑥ 没有记录、车也不跑时，自动停要真停（不许一律扣留）。
    def _stream(on=None, auto=None):
        body = {}
        if on is not None:
            body["on"] = on
        if auto is not None:
            body["auto"] = auto
        r, c, _ = api(base, "/api/v1/mouse/stream", "POST", body)
        return (r.get("data") or {}), c

    def _stream_on() -> bool:
        _h, c_h, _ = api(base, "/api/v1/health")
        if c_h != 200:
            return False
        return bool((( _h.get("data") or {}).get("mouse") or {}).get("stream_on"))

    _rs, _cs, _ = api(base, "/api/v1/session")
    sess_now = ((_rs.get("data") or {}).get("session")) if _cs == 200 else None
    if sess_now:
        # 有会话开着就别在这里收（那是别人的状态）；⑤⑥ 两段记为判不了
        pass
    d_off, c_off = _stream(False)
    first_off = (c_off == 200 and d_off.get("on") is False and not _stream_on())
    d_n, c_n, _ = api(base, "/api/v1/mouse/stream/batch", "POST", {"n": 3, "t_ms": 100})
    n_off = d_n.get("data") or {}
    n_ok = (c_n == 200 and n_off.get("n") == 3 and n_off.get("effective") is False
            and str(n_off.get("mode")) in ("off", "stopwait", "telem"))
    d_on, c_on = _stream(True)
    on_ok = (c_on == 200 and d_on.get("on") is True and d_on.get("applied_n") == 3
             and _stream_on())
    caps_on, _c_caps, _ = api(base, "/api/v1/caps")
    kind_on = ((caps_on.get("data") or {}).get("mouse") or {}).get("pull") or {}
    pace_ok = (kind_on.get("kind") == "pace" and kind_on.get("n") == 3)
    d_manual, c_manual = _stream(False)
    manual_ok = (c_manual == 200 and d_manual.get("on") is False
                 and not d_manual.get("kept") and not _stream_on())
    # ⑤ 记录中自动停 ⇒ 扣留
    rec_kept = None
    d_k: dict = {}
    if not sess_now:
        _, cs_o, _ = api(base, "/api/v1/session/open", "POST", {})
        if cs_o == 200:
            _stream(True)                       # 开记录本身会兜底开流，这里显式再开一次
            d_k, c_k = _stream(False, auto=True)
            rec_kept = (c_k == 200 and d_k.get("kept") is True
                        and d_k.get("reason") == "recording" and _stream_on())
            api(base, "/api/v1/session/close", "POST", {})
    # ⑥ 无条件自动停（此时没有会话）
    if sess_now:
        idle_ok = None
    else:
        _stream(True)
        d_i, c_i = _stream(False, auto=True)
        idle_ok = (c_i == 200 and d_i.get("on") is False
                   and not d_i.get("kept") and not _stream_on())
    # 收尾：别把环境留在批式/开流状态
    _stream(False)
    api(base, "/api/v1/mouse/stream/batch", "POST", {"n": 1, "t_ms": 100})

    i43_ok = first_off and n_ok and on_ok and pace_ok and manual_ok and idle_ok is not False
    if rec_kept is not None:
        i43_ok = i43_ok and rec_kept
    record("I43", "PASS" if i43_ok else "FAIL",
           f"按需拉取：停流={first_off}、关流改 n=3（effective=False）={n_ok}、"
           f"带档开流 applied_n={d_on.get('applied_n')} on={d_on.get('on')}={on_ok}、"
           f"caps.pull={kind_on.get('kind')}/n={kind_on.get('n')}={pace_ok}；"
           f"人工停={manual_ok}；"
           f"记录中自动停被扣留={rec_kept}（reason={d_k.get('reason')}）；"
           f"空闲时自动停真停={idle_ok}"
           + ("" if i43_ok else " ⇒ **要么没按 N 开流，要么把'自动停'当成了'人工停'**"))

    # ---- I44：`record/start` 自己会把拉取开起来（改版三十三）
    # 为什么单列一条：记录器**只在收到帧时才写文件**，所以"界面说已开始记录、跑完一条
    # 记录也没有"这个现象，根因不在记录器，而在它前面少了一次开流。判据必须验
    # **两件事**：① 流本来是关的 ② start 之后真在跑、且真落了盘。
    # ⚠️ `stream_on` 在 **/health**（状态），不在 `/caps`（能力族）。
    #    钉错端点的后果是"恒 None"——看着像"功能没实现"，其实是判据自己读错了地方。
    api(base, "/api/v1/mouse/stream", "POST", {"on": False})
    time.sleep(1.2)
    off_before = not _stream_on()
    st_r, _, _ = api(base, "/api/v1/mouse/maze/record/start", "POST", {"why": "drill-auto"})
    st_d = st_r.get("data") or {}
    note_ok = "自动开流" in str(st_d.get("pull_note") or "")
    time.sleep(1.5)
    on_after = _stream_on()
    sp_r, _, _ = api(base, "/api/v1/mouse/maze/record/stop", "POST", {})
    sp_d = sp_r.get("data") or {}
    saved_frames = sp_d.get("frames")
    i44_ok = bool(off_before and note_ok and on_after
                  and sp_d.get("ok") and (saved_frames or 0) > 0)
    record("I44", "PASS" if i44_ok else "FAIL",
           f"开始记录前流是关的={off_before}；start 回 pull_note={st_d.get('pull_note')!r}"
           f"（含\"自动开流\"={note_ok}）；之后 stream_on={on_after}；"
           f"stop 落盘 {saved_frames} 帧 ok={sp_d.get('ok')}"
           + ("" if i44_ok else
              " ⇒ **点开始记录时没有帧 ⇒ 一条记录也存不出来**（改版三十三之前就是这样）"))

    # ---- I49：**发车前先把拉取拉起来，并等到一帧新帧**（改版三十七）
    # 真机实证（记录 20261022_162108 / 162357）：`maze.run` 在上游是 **urgent** 命令，
    # `send_cmd()` 在整段往返里 `suspend()`…`resume()`（`RATE OFF` 等应答 + `MAZE RUN`
    # 同步等应答 + `RATE PULL` 重发），实测 **0.9~2.4 s 零帧**，恢复时车已经在跑。
    # 更隐蔽的一种：**发车时拉取本来就没在跑**（`auto_stream:false`，拉取按页开门）
    # ⇒ `suspend()` 直接返回、`resume()` 成空操作 ⇒ 发完车仍然不拉，只能等切页。
    # 这正是 162357 开头的形状：帧 0/1 正常、帧 1→2 断 949 ms，两帧都还是待机值。
    #
    # 所以这条判据钉**两件事**（正反两面）：
    #   ① 先把流关掉（旧帧龄 ≥1.2 s）再发车 ⇒ 应答里 `pull.started=True` 且
    #      `frame_age_ms ≤ 350` —— **等的必须是新帧**：若实现拿旧帧充数，读数会是
    #      ≥1200 ms，这条立刻红（这就是负向）。
    #   ② 这一跑记录的**头两帧间隔 ≤250 ms**（= 开录时链路已经在出帧，
    #      不是"开录之后再慢慢把流拉起来"）。
    # ⚠️ 用 `launch_checked`：模拟器电池读数会抖，直接 `api()` 断言 200 会假红。
    api(base, "/api/v1/mouse/stream", "POST", {"on": False})
    time.sleep(1.2)
    off_before49 = not _stream_on()
    rr49, cc49, msg49, ok49, low49 = launch_checked(base)
    pv49 = ((rr49.get("data") or {}).get("pull") or {})
    age49 = pv49.get("frame_age_ms")
    started_ok = bool(pv49.get("started")) and isinstance(age49, int) and age49 <= 350
    name49 = ((rr49.get("data") or {}).get("record") or {}).get("name")
    head_gap49 = None
    # ⚠️ 记录**正在写**时磁盘上还没有文件（名字在 `stop()` 落盘时才生成），
    #    所以不能指望应答里带 name（`status()` 也没这个键）。这里先收车+存盘，
    #    再从"最新一条记录"读回头两帧 —— 这才是**真的**验记录开头。
    api(base, "/api/v1/mouse/maze/stop", "POST", {}, timeout=25)
    api(base, "/api/v1/mouse/maze/record/stop", "POST", {}, timeout=25)
    rl49, cl49, _ = api(base, "/api/v1/records?n=1")
    rows49 = ((rl49.get("data") or {}).get("records") or [])
    if cl49 == 200 and rows49:
        name49 = name49 or str(rows49[0].get("name") or "")
    if name49:
        rp49, _, _ = api(base, f"/api/v1/records/{quote(name49)}/replay", timeout=20)
        s49 = ((rp49.get("data") or {}).get("samples") or [])
        if (len(s49) >= 2 and isinstance(s49[0].get("t"), (int, float))
                and isinstance(s49[1].get("t"), (int, float))):
            head_gap49 = int(round((s49[1]["t"] - s49[0]["t"]) * 1000))
    # 把流开回来：后面 I45 要看实时帧。
    api(base, "/api/v1/mouse/stream", "POST", {"on": True})
    time.sleep(0.8)
    if low49 or not ok49:
        # 低电被拦 = 这一次**判不了**（不是不通过）；如实标 SKIP 并带上原话。
        record("I49", "SKIP", f"发车没成（{msg49 or cc49}）⇒ 发车前保活判不了")
    else:
        i49_ok = bool(off_before49 and started_ok
                      and head_gap49 is not None and head_gap49 <= 250)
        record("I49", "PASS" if i49_ok else "FAIL",
               f"发车前流是关的={off_before49}；应答 pull.started={pv49.get('started')!r}、"
               f"frame_age_ms={age49!r}（应 ≤350：**必须是新帧**，拿旧帧充数会 ≥1200）、"
               f"waited_ms={pv49.get('waited_ms')!r}；记录 {name49} 的头两帧间隔="
               f"{head_gap49!r} ms（应 ≤250）"
               + ("" if i49_ok else
                  " ⇒ 发车前没有确认拉取在跑：这一跑记录的开头会是缺口"
                  "（真机 162357 就是这样断 949 ms），且发完后可能一直不拉到切页为止"))

    # ---- I45：实时墙图**不依赖环形缓冲窗口**（改版三十三）
    # `?seconds=1` 连着调三次：如果实现是"每次重放最近 1 s"，
    # `covered_s` 永远 ≤1、`frames_total` 每次归零重算 ⇒ 这条会红。
    # 增量累积才对：`frames_total` 单调不减、`covered_s` 能超过 1 s。
    api(base, "/api/v1/mouse/stream", "POST", {"on": True})
    api(base, "/api/v1/mouse/maze/record/start", "POST", {"why": "drill-live", "auto_pull": False})
    seen = []
    for _k in range(4):
        time.sleep(1.2)
        rr, _, _ = api(base, "/api/v1/mouse/maze/reconstruct?seconds=1")
        dd = rr.get("data") or {}
        seen.append((int(dd.get("frames_total") or 0),
                     float(dd.get("covered_s") or 0.0),
                     int(dd.get("frames_used") or 0)))
    api(base, "/api/v1/mouse/maze/record/stop", "POST", {})
    totals = [x[0] for x in seen]
    covs = [x[1] for x in seen]
    grows = all(totals[i] >= totals[i - 1] for i in range(1, len(totals))) and \
        totals[-1] > totals[0]
    over_window = max(covs) > 1.2          # 窗口只有 1 s，超过它就证明是累积出来的
    i45_ok = bool(grows and over_window)
    record("I45", "PASS" if i45_ok else "FAIL",
           f"连续 ?seconds=1 取样：frames_total={totals}（单调增={grows}）、"
           f"covered_s={covs}（最大超过 1 s 窗口={over_window}）"
           + ("" if i45_ok else
              " ⇒ **每次都在重放滑动窗口**：跑过窗口长度后，早期探到的墙会消失"))

    # ---- I53：v10 建图增量**端到端**出图（改版三十九）
    # 为什么必须端到端：I51 只证明了那个类自己是对的，证明不了"它被接上了"。
    # 接线漏一处（没在 `on_telemetry` 里喂 / 喂了但 `maze_reconstruct` 没换源）
    # ⇒ 界面上仍是一张图，只是那张图来自**旧的主机推断**：看着有墙有路，
    # 但"未探明"这一态永远不存在（它把"没走到"当成"没有墙"）。
    head("14c. 判据 I53：比赛页那张图真的来自固件增量（三态齐 + 不是全场同值）")
    rr53, cc53, _ = api(base, "/api/v1/mouse/maze/reconstruct")
    d53 = (rr53.get("data") or {}) if cc53 == 200 else {}
    src53 = d53.get("source")
    vw53 = d53.get("view") or {}
    st53 = (vw53.get("stats") or ((d53.get("mapd") or {}).get("stats")) or {})
    # ⚠️ **车停在起点时增量通道天然是空的**：起点格永不入流（固件那边它也是 0）。
    #    这时判据只能验到"一张空图"，而空图既可能是"没接线"也可能是"没动过" ——
    #    两种结论完全不同。所以先看条目，必要时**真让它走一趟**再验。
    if src53 == "mapd" and not int((st53 or {}).get("entries") or 0):
        api(base, "/api/v1/mouse/stream", "POST", {"on": True})
        time.sleep(0.6)
        _rr_l, _cc_l, _msg_l, _ok_l, _low_l = launch_checked(base)
        if _ok_l:
            time.sleep(3.0)                  # 让它跨几格（每跨一格固件才推一条）
        api(base, "/api/v1/mouse/maze/stop", "POST", {}, timeout=25)
        api(base, "/api/v1/mouse/maze/record/stop", "POST", {}, timeout=25)
        rr53, cc53, _ = api(base, "/api/v1/mouse/maze/reconstruct")
        d53 = (rr53.get("data") or {}) if cc53 == 200 else {}
        src53 = d53.get("source")
        vw53 = d53.get("view") or {}
        st53 = (vw53.get("stats") or ((d53.get("mapd") or {}).get("stats")) or {})
    v53 = vw53.get("v") or []
    h53 = vw53.get("h") or []
    flat53 = [c for row in v53 for c in row] + [c for row in h53 for c in row]
    has_unknown = 2 in flat53
    has_known = (0 in flat53) or (1 in flat53)
    cells53 = int(st53.get("cells") or 0)
    total53 = int(vw53.get("cells_total") or 0)
    not_all = bool(total53) and cells53 < total53
    if src53 != "mapd":
        record("I53", "SKIP",
               f"这张图不是增量来源（source={src53!r}）⇒ 这台固件的帧里没有 "
               f"`mapd`（PROTO<10），端到端验不了；退路是主机推断那套"
               f"（`view.src='ring'`，已由 I45/I46 守着）")
    elif not int(st53.get("entries") or 0):
        # 走了那一趟还是一格都没有 ⇒ **判不了**，不是"通过"也不是"产品错"。
        # 分不清三种情况：发车被低电拦 / 车一步没跨 / 固件不推条目。
        record("I53", "SKIP",
               f"增量通道里一条都没有（带 mapd 的帧={st53.get('frames_mapd')}）"
               f"⇒ 车没跨过格（起点格永不入流）或发车没成，端到端这一格判不了；"
               f"接线本身由 I51/I52 守着")
    else:
        i53_ok = bool(st53.get("entries") and st53.get("frames_mapd")
                      and cells53 > 0 and has_unknown and has_known and not_all)
        record("I53", "PASS" if i53_ok else "FAIL",
               f"source={src53}、view.src={vw53.get('src')}、"
               f"条目={st53.get('entries')}、带 mapd 的帧={st53.get('frames_mapd')}、"
               f"已探明格={cells53}/{total53}；三态：未探明出现={has_unknown}、"
               f"已知面出现={has_known}；**不是全格同值**（cells<total={not_all}）"
               + ("" if i53_ok else
                  " ⇒ 图没有真的换成增量来源：`未探明` 不存在说明还是主机推断那套"
                  "（它把'没走到'当成'没有墙'），或者全场被填成同一个值（编的）"))

    # ---- I54 / I55：记录改名 + 批量删除（改版四十）
    #
    # 为什么这两条要单立：它们都是**改数据**的动作，做错了界面上都看不出异常 ——
    #   · 改名漏搬 `<名>.link.json` ⇒ 列表里新名字好端端躺着，而"记录 → 视频"
    #     的关联**静默丢了**（那份关联只存在于那个小文件里，记录本体里没有）；
    #   · 批量删若靠前端循环发 N 个 DELETE ⇒ 半途失败时"到底删了几条"只能靠数，
    #     而这件事**不可逆**。
    # 所以钉五件事（含两条负向）：
    #   ① 改名成功后列表里新旧名字一对一换掉，且 `.link.json` 跟着搬（有视频会话时）；
    #   ② **负向**：新名字已被占 ⇒ 409，且**被占的那条一个字节没动**（不许"先删后写"）；
    #   ③ 同名改名 ⇒ 200 但 `renamed:false`（幂等，不是错误）；
    #   ④ **负向**：空 `to` ⇒ 400、带路径分隔符 ⇒ 409、源不存在 ⇒ 404；
    #   ⑤ 批量删**逐条报**：真删掉 1 条 + 不存在的 1 条如实报失败，空数组 ⇒ 400。
    # ⚠️ 全程只用**自己导入的临时记录**，跑完删干净 —— 真记录一条都不碰。
    head("14g. 判据 I54/I55：记录改名（关联一起搬 / 409 不覆盖 / 幂等）+ 批量删（逐条报）")
    scratch: list = []

    def _imp(tag: str) -> str:
        """导入一条 1 帧的临时记录，返回落盘文件名（失败给空串）。

        ⚠️ `created_ms` **必须有**：导入不只是校验格式，还要用创建时刻生成文件名
           （`_name_for`）。少这个键 ⇒ `KeyError` 翻成 500。手写 doc 的判据最容易
           漏这一条，漏了会看到"导入失败"而不是"我少给了键"。
        """
        rr, cc, _ = api(base, "/api/v1/mouse/maze/records/import", "POST",
                        {"name": tag, "doc": {"format": REC_FORMAT,
                                              "channels": ["a", "b"],
                                              "frames": [[0, 0]], "t_ms": [0],
                                              # 两次导入必须落到**不同的**文件名，
                                              # 否则第二次会覆盖第一次，"重名冲突"就测不出来了。
                                              "created_ms": int(time.time() * 1000)
                                                            + len(scratch) * 1000}})
        nm = str((rr.get("data") or {}).get("name") or "")
        if cc == 200 and nm:
            scratch.append(nm)
        return nm if cc == 200 else ""

    def _names() -> set:
        rl, cl, _ = api(base, "/api/v1/records?n=500")
        return {str(x.get("name")) for x in ((rl.get("data") or {}).get("records") or [])}

    # ⚠️ 路径里的 `name`、列表里的 `name` 都是**带后缀的文件名**，而改名的 `to`
    #    要的是**不带后缀的名字**。第一版直接把 "X.maze.json" 拼成 "X.maze.json-rn"
    #    传进去 ⇒ 落盘成 "X.maze.json-rn.maze.json"（后缀被追加了一遍），
    #    列表里当然找不到新名字，后面几步全部 404。判据自己写错时，
    #    它报的"改名把东西搬坏了"是**假红**（铁律 52 的另一面：先看判据自己）。
    STEM = ".maze.json"

    def _stem(full: str) -> str:
        return full[:-len(STEM)] if full.endswith(STEM) else full

    n54a = _imp("drill-i54-a")
    n54b = _imp("drill-i54-b")
    # 有视频会话就把关联也钉上（没有就如实说"判不了"，不假装通过）
    vsid = ""
    rv, cvv, _ = api(base, "/api/v1/video/sessions")
    for s in ((rv.get("data") or {}).get("sessions") or []):
        if s.get("id"):
            vsid = str(s["id"])
            break
    if n54a and vsid:
        # 关联落盘成 `<名>.link.json`；改名时它必须一起搬，否则关联静默消失。
        api(base, f"/api/v1/records/{quote(n54a)}/video", "POST", {"video_sid": vsid})
    new_stem = _stem(n54a) + "-rn" if n54a else ""
    renamed = new_stem + STEM        # 改名成功后应有的**文件名**
    r1, c1, _ = api(base, f"/api/v1/records/{quote(n54a)}/rename", "POST", {"to": new_stem})
    d1 = r1.get("data") or {}
    names_after = _names()
    moved = bool(c1 == 200 and d1.get("renamed")) and (renamed in names_after) \
        and (n54a not in names_after)
    if moved:
        scratch.remove(n54a)
        scratch.append(renamed)
    # 关联跟着搬：`.link.json` 也得在
    link_moved = None
    if moved and vsid:
        rl2, _, _ = api(base, "/api/v1/records?n=500")
        for row in ((rl2.get("data") or {}).get("records") or []):
            if str(row.get("name")) == renamed:
                link_moved = (row.get("video_sid") == vsid)
                break
    # 同名 ⇒ 幂等（`to` 给不带后缀的名字）
    r2, c2, _ = api(base, f"/api/v1/records/{quote(renamed)}/rename", "POST",
                    {"to": new_stem})
    idem = (c2 == 200 and (r2.get("data") or {}).get("renamed") is False)
    # 负向：新名已被占 ⇒ 409，且被占的那条没动
    if n54b:
        r3, c3, _ = api(base, f"/api/v1/records/{quote(renamed)}/rename", "POST",
                        {"to": _stem(n54b)})
    else:
        r3, c3 = {}, -1
    conflict = (c3 == 409 and n54b in _names())
    # 负向：空 / 非法 / 源不存在
    _r, c_empty, _ = api(base, f"/api/v1/records/{quote(renamed)}/rename", "POST", {"to": "  "})
    _r, c_slash, _ = api(base, f"/api/v1/records/{quote(renamed)}/rename", "POST",
                         {"to": "../i54-escape"})
    _r, c_404, _ = api(base, "/api/v1/records/__no_such_i54__.maze.json/rename", "POST",
                       {"to": "whatever"})
    gates = (c_empty == 400 and c_slash == 409 and c_404 == 404)
    i54_ok = bool(n54a and n54b and moved and idem and conflict and gates
                  and (link_moved is not False))
    record("I54", "PASS" if i54_ok else "FAIL",
           f"改名 {n54a} → {renamed}：HTTP {c1}、renamed={d1.get('renamed')!r}、"
           f"列表里新旧一对一换掉={moved}；"
           f"关联搬迁：{'无视频会话 ⇒ 判不了' if not vsid else link_moved!r}；"
           f"同名幂等={idem}（200 且 renamed:false）；"
           f"负向：重名 409 且被占那条仍在={conflict}（实际 {c3}）、"
           f"空名 400={c_empty == 400}、带 `..` 的 409={c_slash}、源不存在 404={c_404 == 404}"
           + ("" if i54_ok else
              " ⇒ 改名把东西搬坏了：`.link.json` 没跟着搬（关联静默丢）、"
              "或者覆盖了已存在的记录（不可逆）、或者把非法名当成了合法名"))

    # I55：批量删 —— 一次请求、逐条结果
    _r, c_bulk0, _ = api(base, "/api/v1/records/bulk-delete", "POST", {"names": []})
    ghost = "__no_such_i55__.maze.json"
    r5, c5, _ = api(base, "/api/v1/records/bulk-delete", "POST",
                    {"names": [renamed, ghost]})
    d5 = r5.get("data") or {}
    res5 = {str(x.get("name")): bool(x.get("ok")) for x in (d5.get("results") or [])}
    left = _names()
    i55_ok = bool(c_bulk0 == 400 and c5 == 200
                  and d5.get("deleted") == 1 and d5.get("failed") == 1
                  and res5.get(renamed) is True and res5.get(ghost) is False
                  and renamed not in left)
    record("I55", "PASS" if i55_ok else "FAIL",
           f"空数组 ⇒ {c_bulk0}（应 400）；混着删 [存在, 不存在] ⇒ {c5}、"
           f"deleted={d5.get('deleted')} failed={d5.get('failed')}、"
           f"逐条结果={res5}、真删掉的那条已不在列表={renamed not in left}"
           + ("" if i55_ok else
              " ⇒ 批量删没有逐条报：前端拿不到'哪几条没删成'，"
              "而这件事不可逆，只能当成全成了"))
    # 清场：把剩下的临时记录删干净（**测试不留产物**）
    if n54b:
        api(base, "/api/v1/records/bulk-delete", "POST", {"names": [n54b]})
    for nm in list(scratch):
        if nm in _names():
            api(base, "/api/v1/records/bulk-delete", "POST", {"names": [nm]})
    scratch.clear()



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
           f"复位 verified={rv.get('verified')}（rst={rv.get('rst')}、"
           f"accepted={rv.get('accepted')}、{rv.get('elapsed_s')}s）⇒ 复位后 task={task_rst}；"
           f"**静止态收车收得住**：reliable={rel2}（task={task2}）；"
           f"低电预检 level={pv.get('level')}（阈值 {pv.get('min_mv')} mV）" + low_note
           if i28_ok else
           f"**收车/复位/急停没成立**：简单命令={simple}（应 200/OK/线上文本）"
           f" simple_ok={simple_ok}；急停中发车 code={cm} msg={emsg[:90]!r}"
           f" estop_gate_ok={estop_gate_ok}；解锁后发车 code={c1} ok1={ok1}"
           f" msg={msg1[:120]!r}；"
           f"收车如实报告 code={cs} reliable={rel!r} task={task0} "
           f"has_field={has_field} consistent={consistent} moving_honest={moving_honest}；"
           f"收车后再发车 code={c3} relaunch_ok={relaunch_ok} msg={msg3[:120]!r}；"
           f"复位 code={cr} {rv} rescue_ok={rescue_ok}；"
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
    #   ③ 负向 **换段不残留**：收车 → 复位 → 再发车之后，新环必须**从 0 附近重新开始**
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

    # ③ 负向：**换段不残留**。收车 → 复位 → 再发车，新环必须从 0 附近重新开始。
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

    # ---- I31：「比赛」页数据面 —— MAZE RUN RACE 用 target="RACE" 发车；
    #      meas/meta 答得出 spurt 两张码表；maze/state 的 spurt 块读得出；
    #      前墙双档恒等式 frontNear|frontFar == wallFront 且两档互斥。
    #      三态纪律：老上游无 spurt 时如实标"读不到"，**绝不涂成 0/未选**。
    head("14e. 判据 I31：「比赛」页数据面（RACE / 前墙双档 / 冲刺状态）")
    mt, cmt, _ = api(base, "/api/v1/mouse/meas/meta")
    mtd = mt.get("data") or {}
    sp_meta = mtd.get("spurt") or {}
    modes = sp_meta.get("modes") or {}
    front = sp_meta.get("front") or {}
    meta_ok = (cmt == 200 and bool(modes) and bool(front)
               and any("90" in str(v) for v in modes.values())
               and any("近端" in str(v) for v in front.values()))
    # 发车（活链路）——先确保不在运动态且**急停未锁存**：
    #   `/mouse/stop` 是"停车 + **解急停锁存**"（线上文本 STOP），`/mouse/maze/stop` 是收车。
    #   ⚠️ 别用 `/mouse/estop` 去"解锁" —— 它是**上锁**（线上文本 ESTOP）。踩过：那样 RACE 必回 ERR_ESTOP。
    api(base, "/api/v1/mouse/maze/stop", "POST", {}, timeout=25)
    time.sleep(0.6)
    api(base, "/api/v1/mouse/stop", "POST", {}, timeout=15)     # 解锁（STOP）
    time.sleep(0.9)
    rrc, crc, _ = api(base, "/api/v1/timesync/maze_run", "POST",
                      {"target": "RACE"}, timeout=25)
    time.sleep(1.2)
    # 状态面：spurt 块（冲刺）与 state 里的前墙双档是否读得出
    st, cst, _ = api(base, "/api/v1/mouse/maze/state")
    std = st.get("data") or {}
    sp = std.get("spurt") or {}
    ss = std.get("state") or {}
    race_ok = crc in (200, 400, 409, 503)   # 400=该档没这命令；409=BUSY；503=没连
    sp_ok = (cst == 200 and isinstance(sp, dict))
    # 恒等式（有帧才谈得上）：前墙双档在 **state** 里（`mc.maze.state()` 的
    # `frontNear`/`frontFar`/`wallFront`），**不在 spurt 里** —— 早先读错了地方，
    # 于是"恒等式判得了没有"永远是 None（看着像"老固件读不到"，其实是本判据读错了键）。
    ident = None
    if ss:
        fn = ss.get("frontNear"); ff = ss.get("frontFar"); wf = ss.get("wallFront")
        if None not in (fn, ff, wf):
            ident = ((fn | ff) == wf) and ((fn & ff) == 0)
    i31_ok = meta_ok and race_ok and sp_ok and (ident is not False)
    record("I31", "PASS" if i31_ok else "FAIL",
           f"码表：spurt.modes {len(modes)} 档（{list(modes.values())[:2]}）、"
           f"front {len(front)} 项（{list(front.values())[:2]}）；"
           f"`MAZE RUN RACE` HTTP {crc}（{_msg(rrc)[:60]!r}）；"
           f"maze/state.spurt 是 dict={isinstance(sp, dict)}"
           + (f"；前墙恒等式 {(ss.get('frontNear'), ss.get('frontFar'), ss.get('wallFront'))} ⇒ {ident}"
              if ident is not None else "；⚠️ 本次帧里没有前墙三项 ⇒ 恒等式判不了（老固件如实标'读不到'，不当失败）")
           if i31_ok else
           f"**比赛页数据面没守住**：meas/meta HTTP {cmt}（modes={modes} front={front}）"
           f"；RACE HTTP {crc}（{_msg(rrc)[:80]!r}）；state.spurt={sp!r}；恒等式={ident}")

    # ---- I32：「无记忆走迷宫」的前墙双档 + 起点角显示（**数据面**，纯读，不发车）
    #   ① `maze/state.state` 必须带 `frontNear`/`frontFar`（2.3.0 起）—— 界面靠它区分
    #      "近端墙（前方最近一个有墙，贴脸，必须处理）"与"远端墙（当前方向下一个判断有墙，提前量）"；
    #   ② 恒等式 `frontNear | frontFar == wallFront` 且两档互斥（固件组帧的自洽性）；
    #   ③ 起点约定：固件 `dir` 0..3 = UP/RIGHT/DOWN/LEFT，**发车要求 (0,0) 且朝上(dir=0)**；
    #      这一条在**纯数据面**只能验"能读到 dir 且码表把 0 叫 UP"，摆车是现场动作。
    #   ④ 码表 `meas/meta.maze.fronts` 必须零设备访问可用（未连接也能画名字）。
    head("14f. 判据 I32：无记忆走迷宫的前墙双档（近/远）与起点朝向码表")
    mz_meta = mtd.get("maze") or {}
    fronts_tab = mz_meta.get("fronts") or {}
    dirs_tab = mz_meta.get("dirs") or {}
    i32_meta_ok = (bool(fronts_tab) and any("近端" in str(v) for v in fronts_tab.values())
                   and any("远端" in str(v) for v in fronts_tab.values())
                   and str(dirs_tab.get("0", "")).upper() == "UP")
    # 数据面：state 里前墙三项与 dir 的可读性（有帧时）
    i32_state_ok = True
    note32 = "无帧 ⇒ 只看码表"
    if ss:
        has_front_keys = ("frontNear" in ss and "frontFar" in ss)
        dirv = ss.get("dir")
        dir_up = (dirv == 0)
        note32 = (f"state: frontNear={ss.get('frontNear')} frontFar={ss.get('frontFar')} "
                  f"wallFront={ss.get('wallFront')} dir={dirv}({ss.get('dirName')}) "
                  f"pos=({ss.get('posX')},{ss.get('posY')}) 恒等式={ident}")
        # 键必须在（2.3.0 起）；恒等式不得为 False
        i32_state_ok = has_front_keys and (ident is not False)
    i32_ok = i32_meta_ok and i32_state_ok
    record("I32", "PASS" if i32_ok else "FAIL",
           f"前墙码表 {len(fronts_tab)} 项（{list(fronts_tab.values())}）、"
           f"dirs[0]={dirs_tab.get('0')!r}；{note32}"
           if i32_ok else
           f"**前墙双档 / 起点朝向码表没守住**：maze.fronts={fronts_tab} dirs[0]={dirs_tab.get('0')!r}；{note32}")


    # ---- I33：v2.7 按键菜单远程化的六项（**链路级**，`--sim` 才跑）
    #   ① 能力位与清单：`menu_remote` 为真、可远程 6 项、摆位表 4 条且带要求原文；
    #   ② 顺序闸：跳步（直接发 step=3）⇒ 上游 `ERR_VALUE`；
    #   ③ 4 步顺序跑完 ⇒ 全 OK；跑完后重发第 1 步 ⇒ `ERR_BUSY`
    #      （页里已有值，上游 v2.7 新加的安全闸）；
    #   ④ 8 个半字真的写进去了：`BLOCK` 的 `ir_valid` 必须报 **8**；
    #   ⑤ `BLOCK` 的**副作用**必须被后端端出来（"迷宫页的图已按 Flash 重载"）
    #      ⚠️ 模拟器**没有镜像**这一步的墙图重载 ⇒ 那条副作用只验"声明在不在"，
    #         墙图真被换掉这件事留真机验（如实标注，不当成通过）；
    #   ⑥ **confirm 闸 + "真的没碰链路"**：不带 confirm 的擦除 ⇒ 409 + need_confirm，
    #      且**擦除后 `BLOCK` 的 `ir_valid` 必须还是 8** —— 这是"命令真没发出去"的
    #      可观测证据（拿命令计数当证据不行：拉取器在跑，`cmd.sent` 一直在涨）；
    #   ⑦ 运动中发菜单项 ⇒ `ERR_BUSY`（固件一律拒）；⑧ `ERASEALL` 带 confirm ⇒ 成功。
    #   ⚠️ 全部**发到本层端点**（`/mouse/maint/{item}`），不是直接打模拟器 —— 判据
    #      要验的是本层的门面。
    head("14g. 判据 I33：维护与自检六项（confirm 闸 / IRCAP 顺序 / 运动中 / BLOCK 概览）")

    def _maint(item, body=None, timeout=30.0):
        return api(base, "/api/v1/mouse/maint/" + item, "POST", body or {}, timeout=timeout)

    def _mres(rr):
        return ((rr.get("data") or {}).get("result") or {}) if isinstance(rr, dict) else {}

    def _mdata(rr):
        return (_mres(rr).get("data") or {})

    def _merr(rr):
        return ((rr.get("error") or {}) if isinstance(rr, dict) else {})

    caps33, _c33, _ = api(base, "/api/v1/caps", timeout=20)
    m33 = ((caps33.get("data") or {}).get("mouse") or {})
    items33 = m33.get("maint_items") or []
    steps33 = m33.get("maint_steps") or []
    i33_cap = (m33.get("menu_remote") is True and len(items33) == 6 and len(steps33) == 4
               and bool(str((steps33[0] or {}).get("desc", "")).strip()))

    # 清场：I30/I31 发过车（I32 只读），车可能**还在跑**。固件与模拟器对"运动中"一律回
    # `ERR_BUSY`（模拟器 `_motion_busy() = (move_mode != "NONE") or cal_ack_seq >= 0`）
    # ⇒ 先收车 + 复位，否则下面每一条都撞 `ERR_BUSY`，而那**不是**产品错，是判据没清场。
    #  ⚠️ 第一次就踩：I33 直接跟在"刚发过车"后面，六条全红在 `ERR_BUSY` 上。
    api(base, "/api/v1/mouse/maze/record/stop", "POST", {})
    api(base, "/api/v1/mouse/maze/stop", "POST", {}, timeout=25)
    time.sleep(0.8)
    api(base, "/api/v1/mouse/rescue", "POST", {}, timeout=45)
    time.sleep(3.4)

    # ② 跳步（step=3 直接发）⇒ 上游 ERR_VALUE
    r_sk, c_sk, _ = _maint("IRCAP", {"step": 3})
    i33_skip = (c_sk == 200 and _mres(r_sk).get("status") == "ERR_VALUE")

    # ③ 先清页（带 confirm），再顺序跑 4 步；然后重发第 1 步 ⇒ ERR_BUSY
    r_cl, c_cl, _ = _maint("ERASEIR", {"confirm": True})
    i33_erase = (c_cl == 200 and _mres(r_cl).get("ok") is True)
    seq_steps = []
    for st in (1, 2, 3, 4):
        rsq, csq, _ = _maint("IRCAP", {"step": st})
        seq_steps.append((st, csq, _mres(rsq).get("status"), _mres(rsq).get("ok")))
        if not (csq == 200 and _mres(rsq).get("ok") is True):
            break
    i33_seq = (len(seq_steps) == 4 and all(x[3] is True for x in seq_steps))
    r_ag, c_ag, _ = _maint("IRCAP", {"step": 1})
    i33_again = (c_ag == 200 and _mres(r_ag).get("status") == "ERR_BUSY")

    # ④ / ⑤ BLOCK：8 个半字写入 + 副作用声明
    r_bl, c_bl, _ = _maint("BLOCK", {})
    d_bl = _mdata(r_bl)
    i33_len8 = (c_bl == 200 and _mres(r_bl).get("ok") is True and d_bl.get("ir_valid") == 8)
    i33_note = ("Flash" in str(d_bl.get("note") or ""))

    # ⑥ confirm 闸 + 真的没碰链路（擦完 ir_valid 该还是 8）
    r_ec, c_ec, _ = _maint("ERASEIR", {})
    r_bl2, _c_bl2, _ = _maint("BLOCK", {})
    ir_after = _mdata(r_bl2).get("ir_valid")
    i33_gate = (c_ec == 409 and _merr(r_ec).get("type") == "need_confirm"
                and ir_after == 8)

    # ⑦ 运动中一律 ERR_BUSY（先收车复位把车弄回可发车，再发一次车）
    api(base, "/api/v1/mouse/maze/record/stop", "POST", {})
    api(base, "/api/v1/mouse/maze/stop", "POST", {}, timeout=25)
    time.sleep(0.8)
    api(base, "/api/v1/mouse/rescue", "POST", {}, timeout=45)
    time.sleep(3.4)
    _l33, _cl33, _m33, ok33, low33 = launch_checked(base)
    time.sleep(0.6)
    r_br, c_br, _ = _maint("IRSELF", {})
    i33_run = (None if not ok33
               else (c_br == 200 and _mres(r_br).get("status") == "ERR_BUSY"))
    # 收尾：别把车留在跑
    api(base, "/api/v1/mouse/maze/stop", "POST", {}, timeout=25)
    time.sleep(0.8)
    api(base, "/api/v1/mouse/rescue", "POST", {}, timeout=45)
    time.sleep(3.4)
    api(base, "/api/v1/mouse/maze/record/stop", "POST", {})

    # ⑧ ERASEALL 带 confirm ⇒ 成功（擦 6 页后返回值守）
    r_ea, c_ea, _ = _maint("ERASEALL", {"confirm": True}, timeout=35)
    i33_ea = (c_ea == 200 and _mres(r_ea).get("ok") is True)

    i33_ok = (i33_cap and i33_skip and i33_erase and i33_seq and i33_again and i33_len8
              and i33_note and i33_gate and (i33_run is not False) and i33_ea)
    record("I33", "PASS" if i33_ok else "FAIL",
           f"能力位 menu_remote={m33.get('menu_remote')}、可远程 {len(items33)} 项、"
           f"摆位表 {len(steps33)} 条（{str((steps33[0] or {}).get('name'))!r}）；"
           f"跳步 ⇒ {_mres(r_sk).get('status')}；擦除 ⇒ ok={_mres(r_cl).get('ok')}，"
           f"4 步 {[x[2] for x in seq_steps]}，重发第 1 步 ⇒ {_mres(r_ag).get('status')}；"
           f"BLOCK ir_valid={d_bl.get('ir_valid')}（应 8）副作用声明={i33_note}；"
           f"confirm 闸 HTTP {c_ec}（type={_merr(r_ec).get('type')!r}）擦后 ir_valid={ir_after}"
           f"（应仍 8 ⇒ 真没碰链路）；运动中 ⇒ {_mres(r_br).get('status')}"
           f"（launch_ok={ok33}）；ERASEALL ⇒ ok={_mres(r_ea).get('ok')}"
           + "；⚠️ 模拟器没镜像 BLOCK 的墙图重载，那一条留真机验（不当成通过）"
           + ("；⚠️ 发车被低电闸拦下过 ⇒ 运动中断言按「判不了」记" if low33 else "")
           if i33_ok else
           f"**维护与自检没守住**：caps menu_remote={m33.get('menu_remote')} "
           f"items={items33} steps={len(steps33)}；"
           f"跳步 {_mres(r_sk).get('status')}（HTTP {c_sk}）；"
           f"擦除 {_mres(r_cl).get('status')}（HTTP {c_cl}）；4 步 {seq_steps}；"
           f"重发 {_mres(r_ag).get('status')}（HTTP {c_ag}）；"
           f"BLOCK {_mres(r_bl).get('status')} ir_valid={d_bl.get('ir_valid')} "
           f"note={str(d_bl.get('note'))[:60]!r}；"
           f"闸 HTTP {c_ec} type={_merr(r_ec).get('type')!r} 擦后 ir_valid={ir_after}；"
           f"运动中 {_mres(r_br).get('status')}（HTTP {c_br}, launch_ok={ok33}）；"
           f"ERASEALL {_mres(r_ea).get('status')}（HTTP {c_ea}）")

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


# ⚠️ 改版二十四删掉的三大块取证（连同它们的全部辅助函数）：
#
#   `_launcher_sections()`  —— I24（上游相机程序的设置面：零副作用 / 存得住 / 不泄凭据）
#                           —— I25（代启上游服务 → 真能配对，含"编码保护打掉必须起不来"的负向）
#   `_mjpeg_relay_section()` —— I26（MJPEG 反代的 boundary 必须与上游声明一致）
#   `_free_port()` / `_port_busy()` / `_yaml_load()` / `_LogOnly` —— 只为上面两节服务
#
# 一起删的理由是**它们测的东西已经不存在了**：相机适配器、代启器（`launcher.py`）、
# MJPEG 反代、8765 上的上游服务，全随改版二十四下线。留着"跑起来必红"的判据，
# 只会让下一次改动学会忽略红色。
# 其中两件事**仍然要守，只是换了着力点**：
#   · I24 守的"改嵌套键不许写坏 config.yaml" ⇒ 现改在 `video.out_root` 上（见第 2c 节）；
#   · I25 守的"不许留下看不见的进程" ⇒ 现在只有视频子进程，钉在 `VideoAdapter.shutdown()`
#     只杀自己起的那一个（本文件 I39 与 `video.py` 的实现注释里各留了一笔）。


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--sim", action="store_true",
                    help="起上游鼠标模拟器，额外验 I15~I17 / I22~I23（时间同步 / 参数分组 / 丢帧 / 统一帧单流 / 迷宫状态机 / 记录往返 / 收车与复位）。"
                         "不加就 SKIP —— 那几条需要一条活链路。")
    ap.add_argument("--sim-port", type=int, default=5556)
    args = ap.parse_args(argv)

    # 被管道接住时 stdout 是块缓冲的 —— 那样"跑到哪一节"就完全看不见，
    # 一旦某节挂住只能靠猜。这里强制行缓冲。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:                                         # noqa: BLE001
        pass

    # ⚠️ **端口可能被别人占着**，而"服务起不来"的报错会把人引向完全错误的方向。
    #    实测：8899 被小米电脑管家的 `MiPCAudio.exe`（音频共享）占着，
    #    drill 报的却是"S1: 服务 60 s 内没起来" —— 于是去查 app.py、查代码、
    #    查了半天，真凶是一个音频服务。⇒ 起服务前先试绑一次，被占就换端口并说清。
    def _port_free(p: int) -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # ⚠️ 两处都不能省：
            #   · 绑 **0.0.0.0**（不是 127.0.0.1）—— app.py 绑的是全网卡，
            #     占端口的第三方程序多半也绑 0.0.0.0；只试 127.0.0.1 会漏判。
            #   · **不要 SO_REUSEADDR** —— Windows 上它允许"抢占式复用"，
            #     端口明明被监听也返回成功 ⇒ 判定恒为"空闲"，等于没做检查。
            s.bind(("0.0.0.0", p))
            return True
        except OSError:
            return False
        finally:
            s.close()

    if not _port_free(args.port):
        _old = args.port
        for _p in range(_old + 1, _old + 30):
            if _port_free(_p):
                args.port = _p
                break
        print(f"\n[warn] 端口 {_old} 被别的程序占着（不是本层的服务）⇒ 改用 {args.port}。"
              f"\n       查是谁占的：netstat -ano | findstr :{_old}，再用 tasklist 看那个 PID。"
              f"\n       （实测过一次：占 8899 的是小米电脑管家的 MiPCAudio.exe。）")

    base = f"http://127.0.0.1:{args.port}"
    tmp = ROOT / ".tmp" / "drill"
    if tmp.exists() and not args.keep:
        _rmtree_quiet(tmp)

    # 判据 I9 的**基线**：在干任何事之前先给上游拍一张"脏在哪"。
    # 为什么必须有基线：上游（尤其 Auto Locate）本身就是正在开发的东西，工作区里
    # 常有一堆未提交改动 —— 那些不是本层写的，把它们算成"本层弄脏了上游"就是一条
    # 永远红的假警报（真发生过：42 条上游自己的改动让 I9 连红）。
    # I9 真正要守的只有一件事：**跑完这一轮，上游没有比开跑前多出任何改动**。
    up_before = _upstream_dirty()

    print("=" * 78)
    print("Control 端到端演练（无鼠 / 无视频素材 / 无场地）")
    print("=" * 78)

    # ---------------------------------------------------------------- 0 纯本地：远近墙
    head("0. 墙图：近墙落本格、远墙落外一格（纯内存，不碰服务）")
    # 为什么单列一条**不依赖链路**的判据：模拟器的 `frontFar` 恒 0，
    # 所以"远墙画在一格之外"这件事在联调里**永远验不到**。而它错了也不会崩，
    # 只是图上少一格距离（照图判断"还有一格能走"就撞了）。
    try:
        from control.maze_rec import MazeWallMap
        # dir=0 是 +y（向上）。本格 (2,2) 前方 (2,3) 有远墙 ⇒
        #   本格 (2,2) 的 front 必须是 False（贴脸是空的）
        #   (2,3) 的 front 必须是 True （墙在再前一格的边上）
        m_far = MazeWallMap(16)
        m_far.feed({"posX": 2, "posY": 2, "dir": 0, "wallFront": 1, "frontFar": 1,
                    "wallLeft": 0, "wallRight": 0})
        far_ok = (m_far.wall[2][2][0] is False and m_far.wall[2][3][0] is True
                  and m_far.known[2][2][0] is True)
        # 负向：frontFar=0（近墙）⇒ 墙就在本格前沿，下一格必须是 False
        m_near = MazeWallMap(16)
        m_near.feed({"posX": 2, "posY": 2, "dir": 0, "wallFront": 1, "frontFar": 0,
                     "wallLeft": 0, "wallRight": 0})
        near_ok = (m_near.wall[2][2][0] is True and m_near.wall[2][3][0] is False)
        i46_ok = bool(far_ok and near_ok)
        record("I46", "PASS" if i46_ok else "FAIL",
               f"frontFar=1 ⇒ 本格 front={m_far.wall[2][2][0]}（应 False）、"
               f"下一格 front={m_far.wall[2][3][0]}（应 True）={far_ok}；"
               f"frontFar=0 ⇒ 本格={m_near.wall[2][2][0]}（应 True）、"
               f"下一格={m_near.wall[2][3][0]}（应 False）={near_ok}"
               + ("" if i46_ok else
                  " ⇒ **远墙被当成近墙画了**：图上少一格距离（照图判断会撞）"))
    except Exception as e:                                    # noqa: BLE001
        record("I46", "FAIL", f"墙图单测没跑起来：{type(e).__name__}: {e}")

    # ---------------------------------------------------------------- 0b 设备时基
    head("0b. 帧时刻：同批收到的两帧，时刻必须不同（纯内存）")
    # 这条是改版三十五的核心。**故意的负向**：同一毫秒「收到」两帧、
    # 而固件的 `ms8` 差 100（10 Hz 正常采样）—— 这正是实测的现场
    # （147 帧里 43 组撞戳）。修好之后这两帧的 `t_ms` 必须差 100，不是一个 0。
    try:
        from control.maze_rec import MazeRecorder
        _tmpdir = Path(".tmp")
        _tmpdir.mkdir(exist_ok=True)
        _chs = ["ms8", "posX", "posY", "dir", "wallLeft", "wallFront", "wallRight"]
        mr = MazeRecorder(root=_tmpdir, get_channels=lambda: list(_chs))
        mr.start(why="drill-clock")
        # ⚠️ 变量名**不能叫 `base`** —— 外层 `run()` 里 `base` 是服务 URL，
        #    这里一覆盖，后面的 `wait_up(base)` 拿到一个 dict，
        #    报的是"服务 60 s 内没起来"（真凶是变量遮蔽，排查了两轮）。
        frame0 = {"posX": 0, "posY": 0, "dir": 0, "wallLeft": 0, "wallFront": 0, "wallRight": 0}
        # 第 1 帧锚时基；第 2、3 帧模拟"同一批"：ms8 各差 100，但我们**立刻连着喂**
        # （主机侧到达时刻几乎相同，正是批式拉取的样子）。
        mr.feed(dict(frame0, ms8=52))
        mr.feed(dict(frame0, ms8=152))
        mr.feed(dict(frame0, ms8=252))
        time.sleep(0.25)
        mr.feed(dict(frame0, ms8=96))        # 256 回绕（252 → 96 是 +100）
        out = mr.stop(why="manual")
        doc = json.loads(Path(out["path"]).read_text(encoding="utf-8"))
        tt = doc.get("t_ms") or []
        rx = doc.get("rx_t_ms") or []
        dup_t = len(tt) - len(set(tt))
        # 时刻必须严格递增，且第 2、3 帧各差 100（设备确实每 100 ms 采一帧）
        inc = all(tt[i + 1] > tt[i] for i in range(len(tt) - 1))
        step_ok = (len(tt) >= 4 and tt[1] - tt[0] == 100 and tt[2] - tt[1] == 100
                   and tt[3] - tt[2] == 100)
        # 对照：**到达**时刻在同一批里应当是相同的（这正是不能用它的原因）
        rx_dup = len(rx) - len(set(rx))
        src = (doc.get("integrity") or {}).get("t_source")
        i47_ok = bool(dup_t == 0 and inc and step_ok and src == "dev")
        record("I47", "PASS" if i47_ok else "FAIL",
               f"t_ms={tt}（重复 {dup_t} 个、严格递增={inc}、逐帧 100 ms={step_ok}）；"
               f"rx_t_ms={rx}（同批重复 {rx_dup} 个 —— 这就是旧实现撞戳的来路）；"
               f"integrity.t_source={src!r}"
               + ("" if i47_ok else
                  " ⇒ **时间轴还在用到达时刻**：同批的几帧会挤进同一个毫秒，"
                  "回放时刻重叠、Δt=0"))
        try:
            Path(out["path"]).unlink()
        except OSError:
            pass
    except Exception as e:                                    # noqa: BLE001
        record("I47", "FAIL", f"时刻单测没跑起来：{type(e).__name__}: {e}")

    # ---------------------------------------------------------------- 0c 采样缺口
    head("0c. 采样缺口：只标注不补帧，且能认出「这段是发车命令占的」（纯内存）")
    # 为什么单列：真机两条记录里 `t_ms` 分别跳过 2398 / 949 ms，而
    # `integrity.seq_lost_delta` **都是 0** —— 协议层没丢序号（压根没请求），
    # 于是"缺了 24 帧"在账面上显示"完好"。缺口必须**单独记一条**，否则它就是静默的。
    #
    # 这条判据钉四件事：
    #   ① 缺口按 `t_ms` 判定（≥`GAP_MIN_MS`），`ms` 必须**等于** `t_ms` 的跳变（自洽）；
    #   ② 与发车窗口重叠的缺口标 `why="launch_cmd"`；
    #   ③ **窗口之后**的缺口 `why=None`（不许一律标成发车 —— 那是编原因）；
    #   ④ **帧数不多不少**（没人偷偷插值补帧），且 `status()["name"]` 开录时就有。
    try:
        from control.maze_rec import MazeRecorder as _MR
        _chs2 = ["ms8", "posX", "posY", "dir", "wallLeft", "wallFront", "wallRight",
                 "mazeTask", "motionTask"]
        _t2 = Path(".tmp")
        _t2.mkdir(exist_ok=True)
        mr2 = _MR(root=_t2, get_channels=lambda: list(_chs2))
        st2 = mr2.start(why="drill-gap")
        name_at_start = (mr2.status() or {}).get("name")
        base2 = {"posX": 0, "posY": 0, "dir": 0, "wallLeft": 0, "wallFront": 0,
                 "wallRight": 0, "mazeTask": 0, "motionTask": 0}
        mr2.feed(dict(base2, ms8=10))
        mr2.feed(dict(base2, ms8=110))
        sent2 = int(time.time() * 1000)
        mr2.note_launch(sent2)                      # 发车窗口从这里开始（ack 还没回来）
        time.sleep(1.20)                            # 模拟"命令独占链路"的那段
        # 这一段的车已经开始跑（mazeTask/motionTask 起来）⇒ 设备侧运动起点
        mr2.feed(dict(base2, ms8=110, mazeTask=2, motionTask=32))
        ack2 = int(time.time() * 1000)
        mr2.note_launch(sent2, ack2)
        mr2.feed(dict(base2, ms8=210, mazeTask=2, motionTask=32))
        time.sleep(1.20)                            # 窗口**之后**的一段（必须不标原因）
        mr2.feed(dict(base2, ms8=210, mazeTask=2, motionTask=32))
        out2 = mr2.stop(why="manual")
        doc2 = json.loads(Path(out2["path"]).read_text(encoding="utf-8"))
        tt2 = doc2.get("t_ms") or []
        integ2 = doc2.get("integrity") or {}
        gs2 = integ2.get("gaps") or []
        lc2 = integ2.get("launch") or {}
        # ① 缺口自洽：每条的 ms 必须等于 t_ms 上对应那一跳
        jumps = {tt2[i] - tt2[i - 1]: 0 for i in range(1, len(tt2))}
        consistent = bool(gs2) and all(
            any(abs(g["ms"] - (tt2[i] - tt2[i - 1])) < 2 for i in range(1, len(tt2)))
            for g in gs2)
        # ②③ 原因：第一条（与窗口重叠）必须是 launch_cmd，最后一条必须是 None
        why_ok = (len(gs2) >= 2 and gs2[0].get("why") == "launch_cmd"
                  and gs2[-1].get("why") is None)
        # ④ 不补帧（喂了 5 帧就必须正好 5 帧）+ 开录即有名
        no_fill = (len(tt2) == 5)
        name_ok = bool(name_at_start) and name_at_start == out2.get("name")
        run_ok = isinstance(lc2.get("first_run_t_ms"), int) and (
            lc2.get("run_lag_ms") is not None and lc2["run_lag_ms"] >= 0)
        i50_ok = bool(consistent and why_ok and no_fill and name_ok and run_ok)
        record("I50", "PASS" if i50_ok else "FAIL",
               f"缺口 {len(gs2)} 条：{[ (g['ms'], g['why']) for g in gs2 ]}"
               f"（`ms` 与 `t_ms` 跳变自洽={consistent}；首条 why=launch_cmd、"
               f"末条 why=None={why_ok}）；帧数 {len(tt2)}/5（**不补帧**={no_fill}）；"
               f"开录即有名={name_ok}（status.name={name_at_start!r}、落盘 {out2.get('name')!r}）；"
               f"发车锚点 ack={lc2.get('ack_ms')} ms、运动起点 run_lag={lc2.get('run_lag_ms')} ms"
               + ("" if i50_ok else
                  " ⇒ 缺口要么没记（账面上 'seq_lost=0' 会把'缺 24 帧'说成完好），"
                  "要么把窗口外的缺口也标成发车（编原因）"))
        try:
            Path(out2["path"]).unlink()
        except OSError:
            pass
    except Exception as e:                                    # noqa: BLE001
        record("I50", "FAIL", f"缺口单测没跑起来：{type(e).__name__}: {e}")

    # ---------------------------------------------------------------- 0d v10 建图
    head("0d. v10 建图增量：三态 / 幂等 / 冲突取最新 / 落盘往返（纯内存）")
    # 为什么单列：v10 换成"固件报事实"之后，图上多出**第三态「未探明」**。
    # 那一态是最容易做错、也最容易看着没错的 —— 固件里没走过的格低半字节就是 0，
    # 而 0 的字面意思恰好是"四面皆墙"。谁要是不加这一层，画出来就是一张
    # **满格实墙**的图：有内容、边界清楚、看着完全正常，但每一面都是编的。
    #
    # 这条判据钉五件事：
    #   ① 幂等：同一条重复 apply 不改变任何面（否则邻居清掉的位会自己长回来）；
    #   ② 没报过的格 = 未探明（2），**不是** 有墙（1）；
    #   ③ 坏条目（open4=0）只记"到过"，不写面；
    #   ④ 同一条边被两条报成不同结论 ⇒ **最后一次说了算**（用户 2026-10-22 裁决）
    #      且 `face_flip` 记数（"这条边被改过"本身是现场信息，不擦）；
    #   ⑤ 落盘行往返：`-` 必须还原成未探明，十六进制必须还原成同一张图。
    try:
        from control.map_live import LiveWallMap, MapdRows, UNKNOWN

        def _f(seq, x, y, o4, note_resync=0):
            return {"_seq": seq, "posX": x, "posY": y,
                    "mapd": {"count": 1, "lossy": 0, "resync": note_resync,
                             "entries": [{"x": x, "y": y, "open4": o4}]}}

        # open4 的第 d 位 = **1 可通 / 0 有墙**，d 与固件同序（0=+Y 1=+X 2=-Y 3=-X）
        lm1 = LiveWallMap()
        lm1.feed(_f(10, 0, 0, 0b0001))          # +Y 通 ⇒ fh[0][0]=0；+X 墙 ⇒ fv[0][0]=1
        first = (lm1.fh[0][0], lm1.fv[0][0], lm1.stats()["cells"])
        for _k in range(3):
            lm1.feed(_f(10, 0, 0, 0b0001))      # 同一条再喂三遍
        idem = (lm1.fh[0][0], lm1.fv[0][0], lm1.stats()["cells"]) == first \
            and lm1.stats()["cells"] == 1
        # ② 未探明：x=1 那一片从没被报过
        unk_ok = (lm1.fv[0][1] == UNKNOWN and lm1.fv[7][7] == UNKNOWN
                  and lm1.fv[0][0] == 1)
        # ③ 坏条目：open4=0 只记 visited
        lm1.feed(_f(11, 2, 2, 0))
        bad_ok = (lm1.open4[2][2] == 0 and lm1.visited[2][2] == 1
                  and lm1.fv[2][2] == UNKNOWN and lm1.fh[2][2] == UNKNOWN)
        # ④ 冲突取最新：(0,0) 说 +Y 通；从 (0,1) 看 -Y 说有墙 ⇒ 后者后到、说了算
        lm1.feed(_f(12, 0, 1, 0b0011))
        flip_n = lm1.stats()["face_flip"]
        latest_ok = (lm1.fh[0][0] == 1 and flip_n >= 1)
        # ⑤ 落盘往返
        dumped = MapdRows.dump(lm1, 16)
        back = MapdRows.view(dumped["rows"], 16, lm1.view(16)["trail"])
        memv = lm1.view(16)
        rt_ok = (back["v"] == memv["v"] and back["h"] == memv["h"]
                 and back["visited"] == memv["visited"]
                 and dumped["rows"][9][9] == MapdRows.UNKNOWN_CH)
        # ⑥ `坐标差`（pos_mismatch）口径：**重走老格不算错位**。
        #    `last_cell` 若只在"写新格"时更新（曾这么写），车回头走已经写过的格
        #    时会拿一个很旧的格子去比当前坐标 ⇒ 判据常亮（实测 762/1374，55%，
        #    警告行整场亮着，现场只会以为固件采样坏了）。正确口径是取**本帧最后
        #    一条条目**，与坐标差 ≥2 才算"有格子没报上来"。
        lm2 = LiveWallMap()
        lm2.feed(_f(30, 0, 0, 0b0001))        # 写 (0,0)
        lm2.feed(_f(31, 3, 3, 0b0001))        # 写 (3,3) ⇒ 最后条目跳到 (3,3)
        lm2.feed(_f(32, 0, 0, 0b0001))        # **回头重走** (0,0)：门控早退，但条目仍是 (0,0)
        pm_revisit = lm2.stats()["pos_mismatch"]
        lm2.feed({"_seq": 33, "posX": 9, "posY": 9,
                  "mapd": {"count": 1, "lossy": 0, "resync": 0,
                           "entries": [{"x": 0, "y": 0, "open4": 0b0001}]}})
        pm_bad = lm2.stats()["pos_mismatch"]
        pos_ok = (pm_revisit == 0 and pm_bad == 1)
        i51_ok = bool(idem and unk_ok and bad_ok and latest_ok and rt_ok and pos_ok)
        record("I51", "PASS" if i51_ok else "FAIL",
               f"幂等={idem}（重复 3 次后 fh/fv/cells 不变，cells={lm1.stats()['cells']}）；"
               f"未探明={(lm1.fv[0][1], lm1.fv[7][7])}（应都是 {UNKNOWN}）、"
               f"已探明面 fv[0][0]={lm1.fv[0][0]}（应为 1）={unk_ok}；"
               f"坏条目只记到过={bad_ok}；冲突取最新 fh[0][0]={lm1.fh[0][0]}（应为 1）、"
               f"face_flip={flip_n}={latest_ok}；落盘往返={rt_ok}；"
               f"坐标差口径：重走老格 {pm_revisit}（应为 0）、真错位 {pm_bad}（应为 1）={pos_ok}"
               + ("" if i51_ok else
                  " ⇒ 未探明被画成墙 / 重复 apply 改图 / 冲突擦掉前一次 / 落盘的 `-` 还原不回来 /"
                  " 坐标差把重走当错位：这五种都会让图或诊断**看着正常但内容是编的**"))
    except Exception as e:                                    # noqa: BLE001
        record("I51", "FAIL", f"建图三态单测没跑起来：{type(e).__name__}: {e}")

    # ---------------------------------------------------------------- 0e 帧号别名
    head("0e. 建图吃的是 `_seq`（不是 `seq`）：跳号必须数得出来（纯内存）")
    # 为什么单列：上游 `MapReplayer.apply()` 读的是 `frame["seq"]`，而遥测事件里的
    # 帧号键叫 **`_seq`**（`TelemetryStore` 加的）。直接把 `rec` 透传进去 ⇒ 它认为
    # "从来没有序号" ⇒ `holes`/`seq_gaps` **恒 0**，账面写"没丢批"而实际丢了。
    # 那是典型的**假绿**（比假红危险：没人会去查一个绿色的项）。
    # 判据必须**正反两向**：连续喂 ⇒ 0；跳一批 ⇒ 1。只看"有个数"是验不出来的。
    try:
        from control.map_live import LiveWallMap as _LM2

        def _f2(seq_key, seq, x=0, y=0):
            return {seq_key: seq, "posX": x, "posY": y,
                    "mapd": {"count": 1, "lossy": 0, "resync": 0,
                             "entries": [{"x": x, "y": y, "open4": 0b0001}]}}

        la = _LM2()
        la.feed(_f2("_seq", 10))
        la.feed(_f2("_seq", 11))            # 连续 ⇒ 0 次跳号
        la.feed(_f2("_seq", 13))            # 跳 1 批 ⇒ 1 次
        gaps_underscore = la.stats()["seq_gaps"]
        lb = _LM2()
        lb.feed(_f2("seq", 10))
        lb.feed(_f2("seq", 11))
        lb.feed(_f2("seq", 13))
        gaps_plain = lb.stats()["seq_gaps"]
        # 反向：只给 `seq`（上游原生键）也必须算得出来；两者结论必须一致
        i52_ok = (gaps_underscore == 1 and gaps_plain == 1)
        record("I52", "PASS" if i52_ok else "FAIL",
               f"只给 `_seq`（10,11,13）⇒ seq_gaps={gaps_underscore}（应为 1）；"
               f"只给 `seq`（10,11,13）⇒ seq_gaps={gaps_plain}（应为 1）"
               + ("" if i52_ok else
                  " ⇒ 有一路读错键：**这一路会恒报「没丢批」**，真丢了也看不出来"))
    except Exception as e:                                    # noqa: BLE001
        record("I52", "FAIL", f"帧号别名单测没跑起来：{type(e).__name__}: {e}")

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
        # 改版二十四：只剩两路（电脑鼠 + 视频复盘）。视频那一路是**离线**的，
        # 它的"detail 可读"= status() 里带着 state（哪怕 state 是 idle），
        # 而不是"有没有 error" —— 从没解析过不该算故障。
        mo = hh.get("mouse") or {}
        vd = hh.get("video") or {}
        details_ok = (mo.get("detail") is not None) and (vd.get("state") is not None)
        record("I2", "PASS" if (code == 200 and details_ok) else "FAIL",
               f"HTTP {code}；鼠标 detail 可读（当前"
               f"{'已连接' if mo.get('connected') else ('未连接' if mo.get('available') else '模块缺失')}）、"
               f"视频状态可读（state={vd.get('state')}）；两路都没连/没跑时也返回 200 并说清了原因")
        record("I12", "PASS" if dt < 1.0 else "FAIL", f"/health 耗时 {dt*1000:.0f} ms（< 1 s）")

        # ------------------------------------------------------------ I20 / I21
        head("2b. 判据 I20 / I21：界面资源不许被浏览器自行缓存、引用的文件都可达")
        h_root, c_root = head_of(base, "/")
        # ⚠️ 探的必须是**真实存在**的静态资源。原来写 `/static/wsproto.js` ——
        #    改版二十五前端重设计把这个文件删了，于是 c_js 恒 404、I20 恒 FAIL，
        #    而报出来的文案是"界面资源会被启发式缓存"（**假红 + 误导**）。
        #    换成一个一定在的文件：`assets/lib.js`（新前端的唯一数据层）。
        h_js, c_js = head_of(base, "/static/assets/lib.js")
        cc = ((h_root.get("Cache-Control") or "") + " " +
              (h_js.get("Cache-Control") or "")).lower()
        ok_cc = (c_root == 200 and c_js == 200
                 and ("no-cache" in cc or "no-store" in cc))
        record("I20", "PASS" if ok_cc else "FAIL",
               f"Cache-Control：/ = {h_root.get('Cache-Control')!r}、"
               f"/static/assets/lib.js = {h_js.get('Cache-Control')!r}"
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

        # ------------------------------------------------------------ I24（改版二十四重钉）
        # 原来这条钉的是 `camera.launcher.autostart`（两级嵌套）——那次真的把 4 空格
        # 写成 8 空格，config.yaml 下一次就 ParserError，服务起不来。
        # 相机下线后**这个键没了，但那一类错还在**：`patch()` 里 `head` 没 strip
        # 就会把缩进写两遍。现在拿 `video.out_root`（一级嵌套）继续钉同一件事 ——
        # 一级嵌套同样会现形（同一 mapping 里一行缩进多 2 格 ⇒ 结构变了）。
        head("2c. 判据 I24：改嵌套键不许写坏 config.yaml（缩进不许翻倍、注释要留、要能重新 load）")
        i24_bad, i24_got = [], []
        probe = tmp / "i24_config.yaml"
        try:
            from control.config import load as _cfg_load
            probe.parent.mkdir(parents=True, exist_ok=True)
            probe.write_text((ROOT / "config.yaml").read_text(encoding="utf-8"),
                             encoding="utf-8")

            def _indent_of(text: str, key: str) -> int:
                for ln in text.splitlines():
                    if ln.strip().startswith(key + ":"):
                        return len(ln) - len(ln.lstrip(" "))
                return -1

            ind0 = _indent_of(probe.read_text(encoding="utf-8"), "out_root")
            cfg2 = _cfg_load(probe)
            res = cfg2.patch({"video.out_root": '"records/video_i24"'})
            txt = probe.read_text(encoding="utf-8")
            ind1 = _indent_of(txt, "out_root")
            if res.get("video.out_root") != "patched":
                i24_bad.append(f"patch 没认到目标行：{res}")
            elif ind0 < 0 or ind1 != ind0:
                i24_bad.append(f"缩进被改写：{ind0} → {ind1} 空格（写坏 yaml 的典型形态）")
            else:
                i24_got.append(f"缩进不变（{ind1} 空格）")
            try:
                cfg3 = _cfg_load(probe)
                got = cfg3.get("video.out_root")
                if str(got).replace("\\", "/").endswith("records/video_i24"):
                    i24_got.append("值生效且能重新 load")
                else:
                    i24_bad.append(f"改完读回来的不是新值：{got!r}")
            except Exception as e:                            # noqa: BLE001
                i24_bad.append(f"改完 load 不起来了：{type(e).__name__}: {e}")
            # 注释必须还在（config.yaml 里那些"为什么是这个值"是最值钱的东西）
            if "# out_root: 会话产物根" in txt:
                i24_got.append("注释保住")
            else:
                i24_bad.append("注释被写没了（整份 safe_dump 的老毛病）")
        except Exception as e:                                # noqa: BLE001
            i24_bad.append(f"跑 I24 时抛异常：{type(e).__name__}: {e}")
        finally:
            _rmtree_quiet(probe)
        record("I24", "PASS" if not i24_bad else "FAIL",
               f"在临时副本上改 `video.out_root`：{'；'.join(i24_got)}"
               if not i24_bad else f"**配置改写不安全**：{i24_bad[:3]}")

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
        # ⚠️ 改版二十四：拦住的那一路从两条降到**一条**（只有电脑鼠拦记录），
        # 所以清单行的个数从 2 变 1 —— 这里钉的是"清单非空且带对勾分隔"，
        # 不是某个具体条数（条数会随准备清单增减而变，钉死它只会让判据假红）。
        says_what = ("手动连好" in emsg) and ("顶栏" in emsg) and emsg.count("·") >= 1
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
        # I19 是 I18 的**反例**，但要有东西连着才成立 ⇒ 放在第 9 节（连上鼠之后）判。

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

        # ⚠️ 改版二十四删掉两条判据（连同它们的取证装置）：
        #   I13「帧解码必须按 stride 切行（含反例）」—— 被测的 `control/decode.py`
        #       随相机下线一起删了；它守的"不许硬编码 reshape(H,W)"是**YUV 平面解码**
        #       的问题，现在视频解码在 Auto Locate 里，本层不再碰一帧原始字节；
        #   I14「收到 _reconnected 必须主动重对齐状态」—— 被测的 `CameraAdapter`
        #       也不在了（那条重连语义属于 hostlink 的长连接，本层已无长连接）。
        # 两条都不是"暂时不跑"，是**没有对象**了：留着 SKIP 会让人以为还能补测。

        # ------------------------------------------------------------ I5 原子性
        head("8. 判据 I5：开会话失败必须回滚，不留半个会话目录")
        # ⚠️ 改版二十四：这条**换了着力点，但一句都没软化**。
        # 原来的构造面是"假相机 status 正常（门控放行）、control/start 回 500（中途炸）"——
        # 相机下线后没有"第二路"可以在中途炸了，只剩鼠一路。
        # 但 I5 要守的是**编排代码的回滚段**（`ctx.session_open` 里的 except + rollback +
        # `_rmtree_quiet`），那段代码还在、还叫得动 ⇒ 用**注入**造两个场景，进程内直测：
        #   ① 意外异常（不是 MouseError）：必须抛 SessionError、**目录集合一个不变**、
        #      消息里写清"回滚到哪一步 + 目录删没删"；
        #   ② MouseError：鼠侧会话开不出来属于"那一路失败"，整场会话不许假装成功 ——
        #      `phase.mouse` 必须是 `failed`，且 `detail.mouse` 记着原因（能被界面上报出来）。
        # 为什么不 HTTP 造：服务端没有"故意失败"的开关，而为了这个去改生产代码是倒过来的。
        code_txt = (
            "import asyncio, json, shutil, sys\n"
            "sys.path.insert(0, r'%s')\n"
            "from control.config import bootstrap\n"
            "from control.ctx import Ctx, SessionError\n"
            "from control.adapters.mouse import MouseError\n"
            "\n"
            "cfg = bootstrap()\n"
            "root = cfg.session_root\n"
            "before = sorted(p.name for p in root.glob('20*') if p.is_dir())\n"
            # ⚠️ `before` **必须一起回传**：判据那侧要拿它比对"目录集合有没有变"。
            # 早先漏了这一行 ⇒ `i5.get('before')` 永远是 None ⇒ 两条比对全部失真，
            # 报出来的是"留下了半个会话目录 + 一长串历史目录名"，看着像真故障。
            "out = {'before': before}\n"
            "\n"
            "def fresh():\n"
            "    c = Ctx(cfg)\n"
            "    # 让门控放行（判据要走到编排段，不能被前置门控挡在门外）\n"
            "    c.mouse.health = lambda: {'available': True, 'connected': True,\n"
            "                              'port': 'INJECTED', 'detail': ''}\n"
            "    return c\n"
            "\n"
            "c1 = fresh()\n"
            "c1.mouse.open_session = lambda name: (_ for _ in ()).throw(\n"
            "    RuntimeError('人为意外失败（验回滚）'))\n"
            "try:\n"
            "    asyncio.run(c1.session_open(notes=['i5-a']))\n"
            "    out['a_raised'] = False\n"
            "except SessionError as e:\n"
            "    out['a_raised'] = True\n"
            "    out['a_msg'] = str(e)\n"
            "out['a_dirs'] = sorted(p.name for p in root.glob('20*') if p.is_dir())\n"
            "\n"
            "c2 = fresh()\n"
            "c2.mouse.open_session = lambda name: (_ for _ in ()).throw(\n"
            "    MouseError('人为鼠侧失败'))\n"
            "try:\n"
            "    d = asyncio.run(c2.session_open(notes=['i5-b']))\n"
            "    out['b'] = {'phase': d.get('phase'), 'detail': d.get('detail')}\n"
            "except SessionError as e:\n"
            "    out['b'] = {'raised': str(e)}\n"
            "c2.store.rollback()\n"
            "for p in root.glob('20*'):\n"
            "    if p.is_dir() and p.name not in before:\n"
            "        shutil.rmtree(p, ignore_errors=True)\n"
            "out['b_dirs'] = sorted(p.name for p in root.glob('20*') if p.is_dir())\n"
            "print('RES ' + json.dumps(out, ensure_ascii=False, default=str))\n"
            % ROOT)
        rc = subprocess.run([sys.executable, "-c", code_txt], cwd=str(ROOT),
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        i5 = None
        if "RES " in rc.stdout:
            try:
                i5 = json.loads(rc.stdout.split("RES ", 1)[1].strip())
            except Exception:                                  # noqa: BLE001
                i5 = None
        i5_bad = []
        if not i5:
            i5_bad.append(f"注入脚本没跑通：{(rc.stdout + rc.stderr)[-300:]}")
        else:
            if not i5.get("a_raised"):
                i5_bad.append("意外异常没有变成 SessionError（会话可能被当成开成功）")
            msg = str(i5.get("a_msg") or "")
            if "回滚" not in msg:
                i5_bad.append("失败消息没说回滚到哪一步")
            if "目录" not in msg:
                i5_bad.append("失败消息没交代会话目录删没删")
            if i5.get("a_dirs") != i5.get("before"):
                i5_bad.append(f"留下了半个会话目录：{i5.get('a_dirs')}")
            b = i5.get("b") or {}
            if (b.get("phase") or {}).get("mouse") != "failed":
                i5_bad.append(f"鼠侧开失败时 phase.mouse 不是 failed：{b.get('phase')}")
            if "人为鼠侧失败" not in str((b.get("detail") or {}).get("mouse") or ""):
                i5_bad.append(f"没把失败原因记进 detail：{b.get('detail')}")
            if i5.get("b_dirs") != i5.get("before"):
                i5_bad.append(f"清场后目录集合没恢复：{i5.get('b_dirs')}")
        record("I5", "PASS" if not i5_bad else "FAIL",
               f"注入意外异常 ⇒ SessionError（消息含回滚与目录交代）、目录集合"
               f"{'未变' if i5 and i5.get('a_dirs') == i5.get('before') else '变了'}；"
               f"注入 MouseError ⇒ 会话不假装成功：phase.mouse="
               f"{(i5 or {}).get('b', {}).get('phase', {}).get('mouse') if i5 else '?'}、"
               f"detail 记着原因"
               if not i5_bad else f"**回滚/失败语义不对**：{i5_bad[:4]}")

        # ------------------------------------------------------------ 正路
        head("9. 正路：连上电脑鼠 → 开会话 → 收会话 → 验产物")
        # ⚠️ 改版二十四：原来的正路是"导入离线相机会话 → 回放 → 收会话 → 赛后批量"。
        # 相机、离线导入、在线定位、赛后批量全下线 ⇒ 正路只剩**鼠这一条**：
        # 连上（模拟器）→ 开会话 → 让它真跑一会儿 → 收会话 → 拿目录对着查。
        # 没有 --sim 就没有活链路 ⇒ 这几条如实 SKIP，不假装通过。
        sess_dir = None
        sid = None
        if not args.sim:
            for cid, why in (("S2", "开会话/收会话的整条正路"),
                             ("I19", "不拦记录的项不许把记录拦住"),
                             ("S3", "会话里真的落了时间轴行"),
                             ("S4", "收会话不许带告警"),
                             ("I4", "时间轴每行都有 t_pc_ms"),
                             ("I6", "时间轴行数守恒")):
                record(cid, "SKIP", f"{why} —— 加 --sim 才验（需要一条活链路）")
        else:
            sim9 = _start_sim(args.sim_port)
            if sim9 is None:
                for cid in ("S2", "I19", "S3", "S4", "I4", "I6"):
                    record(cid, "FAIL", f"模拟器起不来（端口 {args.sim_port}）⇒ 判不了")
            else:
                try:
                    api(base, "/api/v1/mouse/disconnect", "POST", {})
                    time.sleep(0.3)
                    rc_, cc_, _ = api(base, "/api/v1/mouse/connect", "POST",
                                      {"target": f"tcp://127.0.0.1:{args.sim_port}"},
                                      timeout=25)
                    if cc_ != 200:
                        for cid in ("S2", "I19", "S3", "S4", "I4", "I6"):
                            record(cid, "FAIL", f"连不上活链路 ⇒ 判不了（HTTP {cc_}）")
                    else:
                        hh9, _, _ = api(base, "/api/v1/health")
                        rd9 = (hh9.get("data") or {}).get("readiness") or {}
                        r9, c9, _ = api(base, "/api/v1/session/open", "POST",
                                        {"notes": ["drill"]})
                        sid = (r9.get("data") or {}).get("session_id")
                        if not sid:
                            for cid in ("S2", "I19", "S3", "S4"):
                                record(cid, "FAIL",
                                       f"开会话失败：HTTP {c9} "
                                       f"{json.dumps(r9, ensure_ascii=False)[:200]}")
                        else:
                            record("S2", "PASS",
                                   f"会话 {sid} 已开，phase={(r9['data']).get('phase')}")
                            # I19（I18 的反例）：**只连电脑鼠就够开记录**。
                            # 现在只有它一路拦记录（时钟/视频都不拦），所以这条要守的是
                            # "非阻塞项即使没就绪，也不许把记录拦住" —— 否则"今天只想
                            # 调参数、时间基先不标定"这种合法用法会被误伤。
                            nonblock = [it for it in rd9.get("items", [])
                                        if not it.get("blocking")]
                            nb_not_ok = [it.get("key") for it in nonblock
                                         if not it.get("ok")]
                            i19_ok = bool(rd9.get("can_record")) and bool(sid)
                            record("I19", "PASS" if i19_ok else "FAIL",
                                   f"只连了电脑鼠（can_record={rd9.get('can_record')}）"
                                   f"⇒ 会话照开；清单里 {len(nonblock)} 项不拦记录，"
                                   f"其中此刻未就绪的有 {nb_not_ok or '（无）'}"
                                   f" —— 未就绪也不阻止记录（不误伤「先不标时间基」）"
                                   if i19_ok else
                                   f"只连一路就没放行：can_record={rd9.get('can_record')}、"
                                   f"sid={sid}")
                            # 让它真的过几拍：时间轴要有行，读数是**跑出来的**不是编的
                            time.sleep(2.5)
                            st9, _, _ = api(base, "/api/v1/session")
                            cnt = ((st9.get("data") or {}).get("session") or {}).get("counts") or {}
                            n_tl = int(cnt.get("timeline_rows") or 0)
                            record("S3", "PASS" if n_tl > 0 else "FAIL",
                                   f"2.5 s 内时间轴落了 {n_tl} 行（counts.timeline_rows）"
                                   if n_tl > 0 else
                                   f"时间轴一行都没有（counts={cnt}）⇒ 记录链路没通")
                            r9c, c9c, _ = api(base, "/api/v1/session/close", "POST", {})
                            warns9 = (r9c.get("data") or {}).get("warnings") or []
                            record("S4", "PASS" if (c9c == 200 and not warns9) else "FAIL",
                                   f"收会话 HTTP {c9c}，告警 {len(warns9)} 条"
                                   + (f"：{warns9}" if warns9 else "（干净收尾）"))
                            sess_dir = ROOT / "sessions" / str(sid)
                finally:
                    _stop_sim(sim9)
                    api(base, "/api/v1/mouse/disconnect", "POST", {})

        # ------------------------------------------------------------ I4 / I6
        head("10. 判据 I4 / I6：时间轴每行有 t_pc_ms；meta 记时钟；行数守恒")
        if sess_dir is None or not (sess_dir / "timeline.csv").is_file():
            for cid in ("I4", "I6"):
                record(cid, "SKIP", "没有可查的会话（正路没跑成：需要 --sim）")
        else:
            import csv
            tl = list(csv.DictReader(open(sess_dir / "timeline.csv", encoding="utf-8-sig")))
            miss = [x for x in tl if not x.get("t_pc_ms")]
            meta = json.loads((sess_dir / "meta.json").read_text(encoding="utf-8"))
            clk = meta.get("clock") or {}
            record("I4", "PASS" if (not miss and clk.get("offset_ms") is not None
                                    and clk.get("source")) else "FAIL",
                   f"{len(tl)} 行时间轴，缺 t_pc_ms 的 {len(miss)} 行；"
                   f"meta.clock = offset {clk.get('offset_ms')}ms / "
                   f"source={clk.get('source')} / ±{clk.get('uncertainty_ms')}ms")
            # ✅ I6（改版二十四重钉）：原来是"timeline 的 frame_verified 行数 == 相机账本
            #    frames_verified_on_pc"——相机账本没了。**守恒这件事本身还在**：
            #    一场会话在 timeline 里只能有 **1 行 open + 1 行 close**（多一行 = 重复记账、
            #    少一行 = 收了没记），且 counts.timeline_rows 必须等于文件里的真实行数
            #    （那是内存计数器，跟文件对不上就说明有行丢了/写重了）。
            # ⚠️ 行的**来源**别猜：`ctx.session_open` 里 open 那行是
            #    `store.mark("mouse", "session.open", ...)`（它是"鼠会话开成了"这件事），
            #    close 那行才是 `source="session"`。第一版把 open 也记成 `source="session"`，
            #    于是永远读到 0 行、报一条假红（铁律 50①：判据得跟真代码同源）。
            n_open = sum(1 for x in tl if x.get("kind") == "session.open")
            n_open_m = sum(1 for x in tl
                           if x.get("source") == "mouse" and x.get("kind") == "session.open")
            n_close = sum(1 for x in tl
                          if x.get("source") == "session" and x.get("kind") == "session.close")
            cnt_meta = (meta.get("counts") or {}).get("timeline_rows")
            i6_bad = []
            if n_open != 1:
                i6_bad.append(f"session.open {n_open} 行（应为 1）")
            if n_open_m != 1:
                i6_bad.append(f"来源=鼠的那行 session.open 是 {n_open_m} 行（应为 1）")
            if n_close != 1:
                i6_bad.append(f"session.close {n_close} 行（应为 1）")
            if cnt_meta is not None and int(cnt_meta) != len(tl):
                i6_bad.append(f"counts.timeline_rows={cnt_meta} ≠ 文件 {len(tl)} 行")
            record("I6", "PASS" if not i6_bad else "FAIL",
                   f"时间轴守恒：open {n_open} 行（来源=鼠）/ close {n_close} 行，"
                   f"内存计数 {cnt_meta} == 文件 {len(tl)} 行"
                   if not i6_bad else f"**记账不守恒**：{i6_bad}")

        # ------------------------------------------------------------ I9
        head("12. 判据 I9：跑完之后两个上游依然没被本层改过")
        # 改版二十四：PhoneCamera 不再是被消费的上游（相机整块下线）⇒ 从清单里去掉。
        # 这条判据的意思是"本层只消费、不改上游源码"，与本层删掉了哪些上游无关。
        #
        # ⚠️ 判的是**增量**而不是"全空"：上游本身就是正在开发的东西（Auto Locate 里
        # 常有几十条未提交的源码改动），要求"全空"等于要求先去提交别人的工作 ——
        # 那不是本层能管的事，只会变成一条永远红、且指错方向的假警报（真发生过）。
        # 所以：开跑前拍一张（`up_before`），收尾再拍一张，只问"**多出来什么**"。
        up_after = _upstream_dirty()
        dirty, pre_n, missing = [], [], []
        for name in UPSTREAMS:
            a, b = up_after.get(name), up_before.get(name)
            if a is None:
                missing.append(name)
                continue
            pre_n.append(f"{name} {len(b or ())} 条")
            new = sorted(a - (b or set()))
            if new:
                # 「脏了几条」不够，得说出**脏在哪** —— 只报个数等于让人自己去翻整个目录；
                # 但只给前 3 条，别把结论淹掉。
                dirty.append(f"{name} 新增 {len(new)} 条："
                             f"{' | '.join(x.strip() for x in new[:3])}")
        verdict9 = "FAIL" if dirty else ("SKIP" if missing else "PASS")
        record("I9", verdict9,
               f"跑完这一轮两个上游**没有新增改动**（开跑前已有的未提交改动："
               f"{'、'.join(pre_n) or '无'} —— 那是上游自己的活，不算本层写的）"
               if not dirty else f"**本层把上游写脏了**：{dirty}")
        if missing:
            print(f"         判不了：{missing} 不在本工程旁边")

        # ⚠️ 改版二十四删掉的一节：「13. 定位精度：与合成真值对比」。
        # 它拿合成帧的 ground_truth 与在线定位结果比毫米级误差 —— 合成帧生成器
        # （`tools/make_synth_frames.py`）、在线定位、真值文件全随相机下线。
        # 位置精度现在该由 **Auto Locate 自己的评估**回答（它有自己的合成自检），
        # 本层只负责把轨迹画出来 ⇒ 不再在这里重复一套精度取证。

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

        # ------------------------------------------------------------ I34 / I35（纯函数，无需设备）
        # ---- I34：终点锁死 (7,7)(7,8)(8,7)(8,8)（改版二十三）
        # 用户拍板"终点位置锁死 (7,7)(7,8)(8,7)(8,8)，不需要传输数据了"⇒
        # 判据就是那把锁本身，四面都要钉：
        #   ① 常量 `maze_rec.GOAL_CELLS` 必须就是这四格（改它 = 改赛道定义，必须先改判据）；
        #   ② `_GOAL_SET` 从同一常量派生（不许另写一份坐标表 —— 铁律 32）；
        #   ③ 端点端出的 `goal_cells` 与常量**同一个对象**（routes 只是搬运）；
        #   ④ GOAL 事件：走进终点区**首次**报一次；**负向**：一直不进 ⇒ 一条都没有。
        head("13c. 判据 I34：终点锁死四格（常量 / 派生集 / 端点同源 / GOAL 只报首次）")
        i34_bad, i34_got = [], []
        try:
            from control import maze_rec as MR
            from control.api import routes as RT
            cells = tuple(tuple(c) for c in MR.GOAL_CELLS)
            if cells != ((7, 7), (7, 8), (8, 7), (8, 8)):
                i34_bad.append(f"GOAL_CELLS 不是锁死的四格：{cells!r}")
            else:
                i34_got.append("常量=锁死四格")
            if MR._GOAL_SET != frozenset(cells):
                i34_bad.append(f"_GOAL_SET 与 GOAL_CELLS 不同源：{MR._GOAL_SET!r}")
            if RT.GOAL_CELLS is not MR.GOAL_CELLS:
                i34_bad.append("routes 的 GOAL_CELLS 不是 maze_rec 的同一个对象（两条来路）")
            else:
                i34_got.append("端点同源")

            def _fr34(x, y, task=2):
                return {"mazeTask": task, "posX": x, "posY": y,
                        "dir": 0, "motionTask": 1}

            # ④ 正面：从区外走进 (7,7)，再在区内移动 ⇒ GOAL 只报 1 条
            dec = MR.MazeDecisions(codes=None)
            evs = []
            evs += dec.feed(_fr34(0, 0, 0), 0)      # 第一帧：只播种（WAIT）
            evs += dec.feed(_fr34(1, 0, 2), 100)    # 离开 WAIT
            evs += dec.feed(_fr34(7, 7, 2), 200)    # 进终点区
            evs += dec.feed(_fr34(7, 8, 2), 300)    # 区内移动（不许再报）
            evs += dec.feed(_fr34(8, 8, 2), 400)
            goals = [e for e in evs if e.get("kind") == "GOAL"]
            if len(goals) != 1:
                i34_bad.append(f"进终点区应只报 1 条 GOAL，实得 {len(goals)}：{goals}")
            elif (goals[0].get("x"), goals[0].get("y")) != (7, 7):
                i34_bad.append(f"GOAL 的坐标不对：{goals[0]}")
            else:
                i34_got.append("GOAL 报 1 次")
            # 负向：一直在终点区外 ⇒ 一条 GOAL 都不许有（负向要造得出来 —— 铁律 12）
            dec2 = MR.MazeDecisions(codes=None)
            ev2 = []
            for i, (x, y) in enumerate([(0, 0), (1, 0), (2, 0), (6, 6), (0, 6)]):
                ev2 += dec2.feed(_fr34(x, y, 2 if i else 0), i * 100)
            if any(e.get("kind") == "GOAL" for e in ev2):
                i34_bad.append(f"没进终点区却报了 GOAL：{ev2}")
            else:
                i34_got.append("负向不误报")
        except Exception as e:                                    # noqa: BLE001
            i34_bad.append(f"跑 I34 时抛异常：{type(e).__name__}: {e}")
        i34_ok = not i34_bad
        record("I34", "PASS" if i34_ok else "FAIL",
               f"终点锁死 (7,7)(7,8)(8,7)(8,8)：{'；'.join(i34_got)}"
               if i34_ok else
               f"**终点锁死不对**：{i34_bad[:4]}")

        # ---- I35：JUNC 判据已下线（改版二十三）
        # `stackIdx` 上游换基线那轮就恒 0、v9 连字节都删了 ⇒ 原"岔路栈深度变化"的
        # JUNC 判据已下线（§11.25）。这条判据守两面：
        #   ① **不许再产出 JUNC**：喂岔路口形状的帧序列（有 ROAD 也没关系）⇒ 事件里
        #      没有 JUNC —— "判据还在但永不触发"是静默失效的温床，摘干净才算下线；
        #   ② **不许留残渣**：`maze_rec.py` 源码里不再出现 `stackIdx`（找不到数据来源
        #      的判据留着 = 有人以为它还在工作）。
        head("13d. 判据 I35：JUNC 判据已下线（不产出、源码无 stackIdx）")
        i35_bad, i35_got = [], []
        try:
            import inspect
            src = inspect.getsource(MR)
            # ⚠️ 只查**代码引用**（带引号的键名），不查全文：模块注释里本来就写着
            #   "`stackIdx` 已下线"的缘由 —— 全文匹配会把这段文档判成残渣（假红）。
            if ('"stackIdx"' in src) or ("'stackIdx'" in src):
                i35_bad.append("maze_rec.py 代码仍引用 stackIdx（该字段 v9 已删）")
            else:
                i35_got.append("代码无 stackIdx 引用")
            # 喂一段"有岔路"的帧序列（三面墙变化，路口形状）——
            # 即使有码表时 ROAD 会认出来，也**不许**冒出 JUNC
            dec3 = MR.MazeDecisions(codes=None)
            ev3 = []
            seq = [(0, 0, 0, 0, 0, 0), (1, 0, 0, 1, 1, 2), (2, 0, 1, 1, 0, 2),
                   (3, 0, 0, 1, 1, 2), (4, 0, 1, 0, 1, 2)]
            for i, (x, y, wl, wf, wr, task) in enumerate(seq):
                ev3 += dec3.feed({"mazeTask": task, "posX": x, "posY": y, "dir": 0,
                                  "motionTask": 1, "wallLeft": wl, "wallFront": wf,
                                  "wallRight": wr}, i * 100)
            junc = [e for e in ev3 if e.get("kind") == "JUNC"]
            if junc:
                i35_bad.append(f"JUNC 已下线却还在产出：{junc}")
            else:
                i35_got.append("不产出 JUNC")
            kinds = sorted({str(e.get("kind")) for e in ev3})
            i35_got.append("事件种类=" + ",".join(kinds))
        except Exception as e:                                    # noqa: BLE001
            i35_bad.append(f"跑 I35 时抛异常：{type(e).__name__}: {e}")
        i35_ok = not i35_bad
        record("I35", "PASS" if i35_ok else "FAIL",
               f"JUNC 已下线：{'；'.join(i35_got)}"
               if i35_ok else
               f"**JUNC 下线不干净**：{i35_bad[:4]}")

        # ---- I36：完整性 drop_seen 已退化为本机 seq_lost 差量（改版二十三）
        # 设备侧 `dropCnt` v9 已删（召回也没落地）⇒ `feed()` 里不许再读它；
        # 收尾时按 `stats()["seq_lost"]`/`bad_crc` 的**差量**判（§11.24 第二组 #3）。
        # 这条判据守三面：
        #   ① **不许留残渣**：源码里不许再有带引号的 `dropCnt`（注释里提到不算）；
        #   ② **差量要真的算出来**：钉假 stats 0→3 ⇒ drop_seen=True、delta=3；
        #      3→3（没丢）⇒ False、delta=0；
        #   ③ **三态不许抹平**：不注入取数 ⇒ delta=None（没探到 ≠ 没丢，不是 0）；
        #      计数器中途清零 5→2 ⇒ drop_seen=True（链路断过，数据不完整）。
        head("13e. 判据 I36：完整性按本机 seq_lost 差量（不读 dropCnt / 三态）")
        i36_bad, i36_got = [], []
        try:
            import inspect
            import tempfile
            src = inspect.getsource(MR)
            # ⚠️ 只查**代码引用**（带引号的键名）——注释里本来就写着下线缘由（I35 同款）
            if ('"dropCnt"' in src) or ("'dropCnt'" in src):
                i36_bad.append("maze_rec.py 代码仍引用 dropCnt（该字段 v9 已删）")
            else:
                i36_got.append("代码无 dropCnt 引用")

            def _run36(seq0, seq1, inject=True):
                """钉住假链路统计，跑一段最小记录，回 `stop()` 摘要（产物落临时目录）。"""
                tmp = Path(tempfile.mkdtemp(prefix="mz_i36_"))
                try:
                    state = {"seq_lost": int(seq0), "bad_crc": 0}
                    rec = MR.MazeRecorder(
                        tmp,
                        get_channels=lambda: ["mazeTask", "posX", "posY"],
                        get_link_stats=(lambda: dict(state)) if inject else None)
                    rec.start(why="i36")
                    rec.feed({"mazeTask": 2, "posX": 1, "posY": 1}, None)
                    state["seq_lost"] = int(seq1)
                    out = rec.stop(why="i36")
                    return (out.get("drop_seen"), out.get("seq_lost_delta"),
                            out.get("ok"))
                finally:
                    _rmtree_quiet(tmp)

            got_a = _run36(0, 3)
            if not (got_a[2] and got_a[0] is True and got_a[1] == 3):
                i36_bad.append(f"丢了 3 条应报 drop_seen=True/delta=3，实得 {got_a}")
            else:
                i36_got.append("0→3 报丢")
            got_b = _run36(3, 3)
            if not (got_b[0] is False and got_b[1] == 0):
                i36_bad.append(f"没丢应报 drop_seen=False/delta=0，实得 {got_b}")
            else:
                i36_got.append("3→3 不误报")
            got_c = _run36(0, 0, inject=False)
            if not (got_c[0] is False and got_c[1] is None):
                i36_bad.append(f"没探到应 delta=None（不是 0），实得 {got_c}")
            else:
                i36_got.append("没探到回 None")
            got_d = _run36(5, 2)
            if not (got_d[0] is True):
                i36_bad.append(f"计数器中途清零（链路断过）应 drop_seen=True，实得 {got_d}")
            else:
                i36_got.append("计数器清零也判不完整")
        except Exception as e:                                    # noqa: BLE001
            i36_bad.append(f"跑 I36 时抛异常：{type(e).__name__}: {e}")
        i36_ok = not i36_bad
        record("I36", "PASS" if i36_ok else "FAIL",
               f"完整性按本机 seq_lost 差量：{'；'.join(i36_got)}"
               if i36_ok else
               f"**完整性差量不对**：{i36_bad[:4]}")

        # ---- I37：4 灯三态（改版二十四 / 上游 2.13.0 FW 311）
        # 守的是**"未知不是灭"**：FW 310 及以下状态B1 的 bit2–5 是预留恒 0，
        # 读到的 0 只说明固件不报。判据全在纯函数 `control/leds.py` 上，不需要设备。
        #   ① 正常：fw 311 + 通道在 ⇒ 1→True / 0→False（真的解出两种）；
        #   ② 负向：fw 310 / fw 未知 / 通道不在清单 ⇒ **None**，一个 False 都不许有；
        #   ③ 类型异常：`"1"` 这种字符串不许猜成 True（上游换类型要有人知道）。
        head("13f. 判据 I37：4 灯三态（未知 ≠ 灭 / fw<311 不判）")
        i37_bad, i37_got = [], []
        try:
            from control.leds import led_states, LED_KEYS

            ch_all = list(LED_KEYS)
            rec_mix = {"led1": 1, "led2": 0, "led3": 1, "led4": 0}

            got = led_states(rec_mix, ch_all, "311")
            if got != {"led1": True, "led2": False, "led3": True, "led4": False}:
                i37_bad.append(f"fw311 正常解应 1/0/1/0，实得 {got}")
            else:
                i37_got.append("1/0 解出亮灭")

            got = led_states(rec_mix, ch_all, 310)
            if any(v is not None for v in got.values()):
                i37_bad.append(f"**fw310 把预留位读成了真实电平**：{got}")
            else:
                i37_got.append("fw310 全未知")

            got = led_states(rec_mix, ch_all, None)
            if any(v is not None for v in got.values()):
                i37_bad.append(f"fw 未知时应全 None，实得 {got}")
            else:
                i37_got.append("fw 未知不判")

            got = led_states(rec_mix, ["mazeTask", "posX"], 311)
            if any(v is not None for v in got.values()):
                i37_bad.append(f"通道不在清单里时应全 None，实得 {got}")
            else:
                i37_got.append("缺通道全未知")

            got = led_states({"led1": 1}, None, 311)
            if not (got["led1"] is True and got.get("led2") is None):
                i37_bad.append(f"单键帧应只判存在的那个，实得 {got}")
            else:
                i37_got.append("缺键只判存在的")

            # 负向：字符串不是解码器的合法输出，不许猜成 True（三态纪律）
            got = led_states({"led1": "1", "led2": "0"}, ch_all, 311)
            if got["led1"] is not None or got["led2"] is not None:
                i37_bad.append(f"字符串值不许猜成布尔，实得 {got}")
            else:
                i37_got.append("类型异常不猜")
        except Exception as e:                                    # noqa: BLE001
            i37_bad.append(f"跑 I37 时抛异常：{type(e).__name__}: {e}")
        i37_ok = not i37_bad
        record("I37", "PASS" if i37_ok else "FAIL",
               f"4 灯三态：{'；'.join(i37_got)}"
               if i37_ok else
               f"**4 灯三态不对**：{i37_bad[:4]}")

        # ---- I38：时间轴自洽校验（改版二十四，拍板③"识别侧保证一致、前端不处理对轴"）
        # 本层不再做人工对轴，但仍然要**验**这条轴：拿一条自相矛盾的轴去画"12.34 s"
        # 比不画更坏（看起来和真的一样准）。判据全在纯函数 `video_io.timebase_check` 上。
        #   ① 正常数据 ⇒ ok=True、trust="seconds"（**不许误报**）；
        #   ② 三种坏法各自判出，且 issues 里**指名**是哪一条（不是笼统"异常"）；
        #   ③ VFR ⇒ trust="frames"（只信帧号），即使没别的毛病；
        #   ④ 空轨迹 ⇒ no_rows，不许 ok=True。
        head("13g. 判据 I38：时间轴自洽（坏法指名 / VFR 只信帧号）")
        i38_bad, i38_got = [], []
        try:
            from control.video_io import timebase_check

            def _mk(n=100, fps=30.0, stride=1):
                return [{"frame": k * stride, "t_video_s": (k * stride) / fps,
                         "valid": True, "x": 0.5, "y": 0.5} for k in range(n)]

            good = _mk()
            tb = timebase_check(good, {"fps_declared": 30.0, "frames": 100}, stride=1)
            if not (tb["ok"] and tb["trust"] == "seconds" and not tb["issues"]):
                i38_bad.append(f"正常数据被判坏了：{tb}")
            else:
                i38_got.append("正常不误报")

            rows = _mk()
            rows[50]["frame"] = rows[49]["frame"]        # 帧号不递增
            tb = timebase_check(rows, {"fps_declared": 30.0})
            if "frame_gap" not in [i["code"] for i in tb["issues"]]:
                i38_bad.append(f"帧号断裂没判出：{tb['issues']}")
            else:
                i38_got.append("帧号断裂")

            rows = _mk()
            rows[60]["t_video_s"] = rows[59]["t_video_s"] - 0.1   # 时间戳回退
            tb = timebase_check(rows, {"fps_declared": 30.0})
            if "ts_backwards" not in [i["code"] for i in tb["issues"]]:
                i38_bad.append(f"时间戳回退没判出：{tb['issues']}")
            else:
                i38_got.append("时间戳回退")

            tb = timebase_check(_mk(), {"fps_declared": 30.0, "frames": 200})
            if "n_frames_mismatch" not in [i["code"] for i in tb["issues"]]:
                i38_bad.append(f"元数据帧数不符没判出：{tb['issues']}")
            else:
                i38_got.append("元数据不符")

            tb = timebase_check(_mk(), {"fps_declared": 30.0, "frames": 100,
                                       "vfr_suspected": True})
            if tb["trust"] != "frames":
                i38_bad.append(f"VFR 仍宣称精确秒（trust={tb['trust']}）")
            else:
                i38_got.append("VFR 只信帧号")

            tb = timebase_check([], {"fps_declared": 30.0})
            if tb["ok"] or "no_rows" not in [i["code"] for i in tb["issues"]]:
                i38_bad.append(f"空轨迹被当成正常：{tb}")
            else:
                i38_got.append("空轨迹不合规")
        except Exception as e:                                    # noqa: BLE001
            i38_bad.append(f"跑 I38 时抛异常：{type(e).__name__}: {e}")
        i38_ok = not i38_bad
        record("I38", "PASS" if i38_ok else "FAIL",
               f"时间轴自洽：{'；'.join(i38_got)}"
               if i38_ok else
               f"**时间轴校验不对**：{i38_bad[:4]}")

        # ---- I39：会话产物读取（改版二十四）
        # 守的是"缺列/畸形要明确报错，不静默出 0 点"：一张空轨迹图看起来像"车没动"，
        # 而真实原因往往是产物格式变了 —— 这是最贵的那种静默错。
        head("13h. 判据 I39：视频会话产物读取（缺列/畸形不静默出 0 点）")
        i39_bad, i39_got = [], []
        try:
            import csv as _csv
            import json as _json
            import tempfile
            from control.video_io import (VideoError, VideoNotFound, read_session,
                                          read_trajectory)

            _HDR = ("frame,t_video_s,t_rel_s,valid,x,y,theta_deg,dir_x,dir_y,"
                    "rms_field,origin_id,corner_ids,flags,reason").split(",")

            def _mk_case(*, sj=True, csv_text=None, header=None):
                d = Path(tempfile.mkdtemp(prefix="v_i39_"))
                if sj:
                    (d / "session.json").write_text(
                        _json.dumps({"video": {"fps_declared": 30.0, "frames": 3},
                                     "frames": {"n_total": 3, "n_valid": 2},
                                     "flags": []}), encoding="utf-8")
                if csv_text is not None:
                    (d / "trajectory.csv").write_text(csv_text, encoding="utf-8-sig")
                elif header is not None:
                    import io
                    buf = io.StringIO()
                    w = _csv.writer(buf)
                    w.writerow(header)
                    (d / "trajectory.csv").write_text(buf.getvalue(), encoding="utf-8")
                return d

            rows_txt = ("frame,t_video_s,t_rel_s,valid,x,y,theta_deg,dir_x,dir_y,rms_field,"
                        "origin_id,corner_ids,flags,reason\r\n"
                        "0,0.0,0.0,True,0.12,0.34,90,0,1,0.8,1,1;2;3;4,,\r\n"
                        "1,0.0333,0.0333,False,,,,,,,,,no_target\r\n"
                        "2,0.0667,0.0667,True,0.13,0.35,91,0,1,0.7,1,1;2;3;4,,\r\n")
            d0 = _mk_case(csv_text=rows_txt)
            try:
                sj0 = read_session(d0)
                tj0 = read_trajectory(d0)
                if sj0.get("video", {}).get("frames") != 3:
                    i39_bad.append(f"session.json 没读对：{sj0}")
                if not (tj0["n_total"] == 3 and tj0["rows"][0]["frame"] == 0
                        and tj0["rows"][0]["valid"] is True
                        and abs(tj0["rows"][0]["x"] - 0.12) < 1e-9
                        and tj0["rows"][1]["valid"] is False):
                    i39_bad.append(f"轨迹解析不对：n={tj0['n_total']} 首行={tj0['rows'][:2]}")
                else:
                    i39_got.append("正常产物可读")
            finally:
                _rmtree_quiet(d0)

            d1 = _mk_case(sj=False, csv_text=rows_txt)
            try:
                try:
                    read_session(d1)
                    i39_bad.append("缺 session.json 时没报错（会静默当成空会话）")
                except VideoNotFound:
                    i39_got.append("缺 session.json 报 404 类错")
            finally:
                _rmtree_quiet(d1)

            d2 = _mk_case(sj=True)
            try:
                try:
                    read_trajectory(d2)
                    i39_bad.append("缺 trajectory.csv 时没报错")
                except VideoNotFound:
                    i39_got.append("缺 trajectory.csv 报错")
            finally:
                _rmtree_quiet(d2)

            # 缺列：把 x 列去掉 ⇒ 必须说"缺列"，不许照样读成 0 点
            hdr_miss = [c for c in _HDR if c != "x"]
            d3 = _mk_case(sj=True, header=hdr_miss)
            try:
                try:
                    read_trajectory(d3)
                    i39_bad.append("缺 x 列没报错（会画出一堆无坐标点）")
                except VideoError as e:
                    if "缺列" not in str(e):
                        i39_bad.append(f"缺列报错没说清缺什么：{e}")
                    else:
                        i39_got.append("缺列指名")
            finally:
                _rmtree_quiet(d3)

            # 有行但帧号全空 ⇒ 不许静默 0 点
            d4 = _mk_case(sj=True, csv_text=(
                "frame,t_video_s,valid,x,y\r\n,,,,,\r\n,,,,,\r\n"))
            try:
                try:
                    tj = read_trajectory(d4)
                    i39_bad.append(f"帧号全空却返回了 {tj['n_total']} 点（静默出 0 点）")
                except VideoError:
                    i39_got.append("帧号全空报错")
            finally:
                _rmtree_quiet(d4)
        except Exception as e:                                    # noqa: BLE001
            i39_bad.append(f"跑 I39 时抛异常：{type(e).__name__}: {e}")
        i39_ok = not i39_bad
        record("I39", "PASS" if i39_ok else "FAIL",
               f"会话产物读取：{'；'.join(i39_got)}"
               if i39_ok else
               f"**产物读取不对**：{i39_bad[:4]}")

        # ------------------------------------------------------------ I15~I17
        # 这三条是**上游 1.2/1.3/1.4 更新**带来的新失败模式，需要一条活链路才能判。
        head("14b. 判据 I15 / I16 / I17：跟上游更新（需要模拟器）")
        if not args.sim:
            for cid, why in (("I15", "帧时间必须说清是哪一种（send / arrival）"),
                             ("I16", "参数分组必须与上游 PARAM_GROUPS 逐组逐项一致（2.5.0 起 41 项四组）"),
                             ("I17", "丢帧必须可回答（seq_lost 不是 None）")):
                record(cid, "SKIP", f"{why} —— 加 --sim 才验（需要一条活链路）")
            _skip_mouse_sections = True
        else:
            _skip_mouse_sections = False
            sim = _start_sim(args.sim_port)
            if sim is None:
                for cid in ("I15", "I16", "I17", "I22", "I23", "I27", "I28", "I30", "I31", "I32", "I33", "I40", "I41", "I42", "I43", "I44", "I45"):
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
        # ⚠️ 改版二十四：原来这里挂的是 I11（相机账本 `complete=null` 的三态渲染，
        # 需要一台真手机）—— 相机没了，那条判据的被测对象也没了。
        # 现在"必须真有东西"的只剩视频解析这**一整条外链**：
        record("V1", "SKIP", "真实视频 → Auto Locate 解析的**端到端**：需要一段真视频 + "
                              "上游视觉依赖（cv2/numpy）齐全。本文件只验**产物读取与三态逻辑**"
                              "（I38/I39）+ 子进程的起停路径；解析质量归上游自己的评估")
        record("I3*", "INFO", "串口占用已实测；但'先关 run_host.py'那句提示要真开着 GUI 才会走到")
        record("I12*", "INFO", f"{HW_ONLY}：真机 250 Hz 满流下 /health 仍 < 1 s（需要真设备）")

        # ------------------------------------------------------------ 产物
        head("产物")
        if sess_dir is None or not Path(sess_dir).is_dir():
            print("  （没有会话目录：正路需要 --sim 才跑得起来）")
        else:
            print(f"  会话目录：{sess_dir}")
            for f in sorted(Path(sess_dir).rglob("*")):
                if f.is_file():
                    print(f"    {f.relative_to(Path(sess_dir))}  ({f.stat().st_size} B)")
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
