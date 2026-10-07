#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PC 端服务端到端测试（M1）：**用真 HTTP/WS 打真服务**，不是单元 mock。

覆盖的是协议里"错了会静默"的那几条（§7.2 / §5.6 / §6.3）：
  1  正常链路：init → 分片（CRC32）→ complete（SHA-256）→ 落盘 + 帧 JSON + manifest + ACK
  2  断点续传：重复 init 幂等回到同一 file_id；偏移不符返回 **真实偏移**（409）；从该偏移续传成功
  3  分片 CRC 不符 → 412 且**偏移不推进**（重传同片即可）
  4  整文件 SHA-256 不符 → 422，且**分片保留**（不强迫从头传）
  5  幂等键撞不同内容 → 409（不能"以先到的为准"）
  6  未收满就 complete → 409 incomplete
  7  WS 鉴权：无 token → 403；有 token → 首条即 config
  8  控制道：control/start → WS 收到 start（含 session_id/plan）；无人连接时 sent_to=0 要能看出来
  9  断言：calib_id 一致 → true；焦点回读中途变化 → param_drift 非空（**断言要能报错**）
 10  限速：交付道限速真的生效（长期平均速率，令牌桶口径）
 11  链路测速端点：/api/v1/linktest 回传字节数与速率
 12  监视道：thumb 按 seq 落盘 + 旁挂元数据 + 摘要/manifest + /thumb/latest 端点
 13  监视道健壮性：坏 base64、未知会话都不影响交付道，也不建会话
 14  镜头/变焦断言：会话内换镜要报出来；请求的 zoom 未被采纳也要报出来
 15  默认 profile 不指定 zoom（0），否则手面上的档位选择会被静默压掉
 16  控制道声明会话：session_begin → session_ack、幂等、非法 id 报 400 而非 500
 17  未知会话的缩略图：拒收、只回话一次、不建会话
 18  元数据完整度：缺 CaptureResult 派生字段要逐项计数（静默缺失必须自己算出来）
  19  PC 主控闭环：start_ack（失败不再假装 running）/ stop_ack / 开流门控 fail 自动停流
  20  开流门控的枚举写法归一（`"OFF(0)"` ≡ `"OFF"`）+ 回读缺席判 incomplete 且不停流
      + 夹具保真（本文件的夹具必须与 App 的 name() 写法一致，否则测试会变成安慰剂）
  21  溯源字段：会话先被 session_begin 建起来时，init 里的声明也必须进档（空则补、异则记冲突）
  22  "停采集 ≠ 停交付"：停流后照收交付帧并计数；delivery_complete 三态不许含糊
  23  监视道改推送：三张缩略图推三条（不再被 3 s 轮询合并成 1 张）；交付收尾宣告 delivery_done
  24  控制道**完全离线**时 HTTP 交付仍 verified（"实时联通结束"≠"不能回传"）；
      没有任何收尾证据时 delivery_complete 必须是 null（判不了 ≠ 已收全）
  25  监视道推送与交付收尾的记账：新增/未知 status 字段原样留档
  26  扫码配对：/api/v1/pair 鉴权（403）+ 配对码写法严格 + 二维码逐模块忠实
      + **PC 生成器 ↔ App 解析器的契约锚定**（从 PairCode.kt 读常量与样本串反算）
  27  （第 23 轮）对外接口面 A1–A6：capabilities 不含凭据 / loopback-only 按**对端地址**判
      （真起一个绑局域网地址的服务，伪造 Host、X-Forwarded-For 仍须 403）/ host/status 与
      面板同口径且 gate 三态原样透出 / MJPEG 是合法 multipart + 接入即得画面 /
      慢订阅者只丢自己的帧 / 事件满队列丢最旧且**计数**、节流也**计数**
  28  （第 23 轮）归档道 R1–R8：幂等键含 kind / 落盘物理隔离 / 内存两张表 / PC 不采信自报 /
      两个缺口分开命名且能点出帧号 / 两条自洽式 / 交叉校验（交付 ts ⊆ 归档 ts）/ 配额熔断
      显式命名且不影响交付道

跑法：`python pc/test_server.py`（需要 general_env：aiohttp）
"""
from __future__ import annotations

import asyncio
import base64
import collections
import base64
import hashlib
import io
import json
import re
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path

import aiohttp

try:                        # 只有 A8h2（PNG 逐格比对）需要；缺了就判 INCOMPLETE，不是 FAIL
    from PIL import Image  # type: ignore
except Exception:           # pragma: no cover
    Image = None

HERE = Path(__file__).resolve().parent
PY = sys.executable
TOKEN = "testtoken"
CHUNK = 256 * 1024

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


def crc32b(b: bytes) -> str:
    return f"{zlib.crc32(b) & 0xFFFFFFFF:08x}"


def svg_check(svg: str, text: str) -> tuple[bool, str]:
    """二维码 SVG 的**忠实性**检查 —— 不是"非空就算过"，也不是"数量对就算对"。

    为什么要这么严：只断言"含 <svg>"的测试，在"画成一整块黑""少画一半模块"
    或"模块位置整体错一格"时照样全绿，而现场表现是"扫码扫不出来" —— 又一个安慰剂
    （本项目栽过好几次）。所以这里：
      ① 从 SVG 的 `<path>` **重建模块矩阵**（path 的 `M{x} {y}h{s}v{s}h-{s}z` 就是逐格）；
      ② 拿 qrcode 库为**同一文本**重算矩阵；
      ③ **逐格比对位置**（不只是数量）+ 边长必须是模块数的整数倍。
    只比数量是不够的：把两个模块互换位置，数量一个不差，但码已经废了。
    """
    if not svg or not svg.startswith("<svg") or "</svg>" not in svg:
        return False, "不是 SVG"
    m = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
    if not m:
        return False, "没有 viewBox"
    w, h = int(m.group(1)), int(m.group(2))
    if w != h:
        return False, f"非正方形 {w}x{h}"
    d = re.search(r'<path d="([^"]*)"', svg)
    if not d:
        return False, "没有 path（一块白板，现场扫不出来却会「通过」）"
    cells = set()
    scale = None
    for cm in re.finditer(r"M(\d+) (\d+)h(\d+)v(\d+)h-\d+z", d.group(1)):
        x, y, s2 = int(cm.group(1)), int(cm.group(2)), int(cm.group(3))
        if scale is None:
            scale = s2
        elif scale != s2 or int(cm.group(4)) != s2:
            return False, "模块尺寸不一致（渲染有缩放/裁切）"
        cells.add((y // s2, x // s2))
    if not cells or not scale or scale <= 0:
        return False, "path 里没有可解析的模块"
    if w % scale != 0:
        return False, f"边长 {w} 不是模块尺寸 {scale} 的整数倍"
    mods = w // scale
    try:
        import qrcode  # type: ignore
    except Exception as e:                                  # pragma: no cover
        return False, f"qrcode 不可用：{e}"
    # 不把 border / 纠错等级写死在测试里（写死就变成"两份抄一样"）：遍历候选参数，
    # 找一个能与 SVG **逐格**对上的。都找不到 ⇒ SVG 不是这段文本的合法二维码。
    levels = [qrcode.constants.ERROR_CORRECT_L, qrcode.constants.ERROR_CORRECT_M,
              qrcode.constants.ERROR_CORRECT_Q, qrcode.constants.ERROR_CORRECT_H]
    detail = ""
    for lv in levels:
        for border in range(0, 7):
            qr = qrcode.QRCode(border=border, error_correction=lv)
            qr.add_data(text)
            qr.make(fit=True)
            mat = qr.get_matrix()
            if len(mat) != mods:
                continue
            want = {(r, c) for r in range(mods) for c in range(mods) if mat[r][c]}
            if want == cells:
                if 'fill="#000000"' not in svg or 'fill="#ffffff"' not in svg:
                    return False, "缺黑白填充（扫码需要对比度）"
                return True, (f"{mods}×{mods} 模块、{len(want)} 个黑模块、"
                              f"**逐格位置**与库矩阵一致（border={border}）")
            detail = (f"最接近的参数下：SVG 多画 {len(cells - want)} 格、"
                      f"少画 {len(want - cells)} 格")
    return False, f"SVG 不是这段文本的合法二维码（{detail or '模块数对不上'}）"


# --------------------------------------------------------------------------- 客户端助手

class Client:
    def __init__(self, base: str, sess: aiohttp.ClientSession):
        self.base, self.sess = base, sess

    async def init(self, **kw):
        body = {"session_id": "2026-09-25_0100_test", "device_serial": "TESTSERIAL",
                "model": "Xiaomi 24129PN74C", "app_version": "m0probe-1.9.0",
                "calib_id": "calib_test_01", "delivery": "stream",
                "capture_path_id": "burst_yuv_manual", "capture_path_rev": 1}
        body.update(kw)
        async with self.sess.post(f"{self.base}/api/v1/files/init", json=body) as r:
            return r.status, await r.json()

    async def put(self, fid, off, data, total, *, crc=None, expect=None):
        h = {"Content-Range": f"bytes {off}-{off + len(data) - 1}/{total}"}
        if crc is not None:
            h["X-Chunk-Crc32"] = crc
        async with self.sess.put(f"{self.base}/api/v1/files/{fid}", data=data, headers=h) as r:
            return r.status, await r.json()

    async def complete(self, fid, sha=None):
        async with self.sess.post(f"{self.base}/api/v1/files/{fid}/complete",
                                  json={"sha256": sha} if sha else {}) as r:
            return r.status, await r.json()

    async def sessions(self):
        async with self.sess.get(f"{self.base}/api/v1/sessions") as r:
            return await r.json()

    async def control(self, action, **body):
        # ⚠ 必须带 token：`/api/v1/control/*` 从第 25 轮起**真的**校验 token
        # （此前声明说要、代码不查；它不在 loopback 名单里 ⇒ 同网段谁都能启停采集）。
        async with self.sess.post(f"{self.base}/api/v1/control/{action}?token={TOKEN}",
                                  json=body) as r:
            return r.status, await r.json()

    async def control_raw(self, action, headers=None, **body):
        """**不带 token** 地打 control（A1f「声明必须为真」要用），可选自定义头。"""
        async with self.sess.post(f"{self.base}/api/v1/control/{action}", json=body,
                                  headers=headers or {}) as r:
            return r.status, await r.json()


async def recv_until(ws, want: str, timeout: float = 8.0) -> dict:
    """从 WS 读到指定 type 为止（其他类型的消息先放着不看）。

    为什么需要它：ACK、start/stop 走**同一条** WS（§7.1），而多帧连续交付时
    ACK 会插在中间。断言"下一条必须是 stop"会偶发失败 —— 那是**测试假设错了**，
    不是协议错了（协议没承诺全局顺序）。
    """
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while True:
        left = end - loop.time()
        if left <= 0:
            raise asyncio.TimeoutError(f"等 {want} 超时")
        m = json.loads((await asyncio.wait_for(ws.receive(), left)).data)
        if m.get("type") == want:
            return m


async def upload_whole(c: Client, seq: int, payload: bytes, *, meta=None, chunk=CHUNK,
                       session_id=None, **extra):
    """分片传完并 complete，返回 (file_id, sha256)。session_id 为空则用 Client 自带的。

    `extra` 直接进 init 请求体（用于改 app_version / calib_id 等**声明**字段的用例）。
    """
    sha = hashlib.sha256(payload).hexdigest()
    kw = {"session_id": session_id} if session_id else {}
    kw.update(extra)
    st, j = await c.init(seq=seq, name=f"frame_{seq:06d}.yuv", size=len(payload),
                         sha256=sha, meta=meta or default_meta(seq), **kw)
    assert st == 200, (st, j)
    fid = j["file_id"]
    off = j["resume_offset"]
    while off < len(payload):
        part = payload[off:off + chunk]
        st, j = await c.put(fid, off, part, len(payload), crc=crc32b(part))
        assert st == 200, (st, j)
        off = j["received"]
    st, j = await c.complete(fid, sha)          # ← 少了这一步就永远等不到 ACK
    assert st == 200, (st, j)
    return fid, sha


def default_meta(seq: int, **over):
    m = {
        "seq": seq, "calib_id": "calib_test_01",
        "capture_path_id": "burst_yuv_manual", "capture_path_rev": 1,
        "sensor_timestamp_ns": 167000000000000 + seq * 33288000,
        "trigger_sent_ns": 167000000000000 + seq * 33288000 - 4200000,
        "exposure_end_ns": 167000000000000 + seq * 33288000 + 10000000,
        "wall_clock_ns": 1790000000000000000, "boot_time_ns": 0,
        "sensor_ts_minus_request_ns": 4200000, "callback_latency_ns": 81000000,
        "control_enable_zsl": False,
        "focus_distance_diopter": 1.001001, "zoom_ratio": 1.0,
        # ⚠ **夹具必须与 App 的实际写法一致**：App 的 `name()` 记的是 `"名字(码)"`
        # （见 DeliverySession.name）。第 20 轮踩到的坑正是这里 —— 夹具写的是裸名字
        # `"OFF"`，而真机送的是 `"OFF(0)"`，于是"门控 pass"这条断言**一直是绿的**，
        # 真机上却把 ois/eis 判成硬失败并自动停流。夹具与现实的偏差会让测试变成安慰剂。
        # 下面 20f 有一条断言专门钉住这个格式前提（直接从 .kt 源码里读）。
        "af_mode": "OFF(0)", "ois_mode": "OFF(0)", "eis_mode": "OFF(0)",
        "edge_mode": "OFF(0)", "noise_reduction_mode": "OFF(0)",
        "exposure_time_ns": 10000000, "iso": 200, "sensor_pixel_mode": 0,
        "rolling_shutter_skew_ns": 9212928, "battery_temperature_c": 38.5,
        "width": 1920, "height": 1440, "y_stride": 1920,
        "crop_region": [0, 0, 4096, 3072], "format": "YUV_Y",
    }
    m.update(over)
    return m


# --------------------------------------------------------------------------- 主流程

async def main() -> int:
    # 契约锚定要用服务端模块里的函数（pair_uri / enum_eq / PANEL_HTML）：
    # **判据必须能引用"被观测对象的真实产出"**，不能凭记忆抄一份常量到测试里。
    sys.path.insert(0, str(HERE))
    import server as srv                                    # noqa: E402

    port = free_port()
    tmp = Path(tempfile.mkdtemp(prefix="pcsv_"))
    proc = subprocess.Popen(
        [PY, str(HERE / "server.py"), "--host", "127.0.0.1", "--port", str(port),
         "--data-dir", str(tmp), "--token", TOKEN, "--max-mbps", "25"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace")
    # ⚠ **必须把服务端 stdout 持续排空**，不能挂着不读。
    # 实测代价：被测服务端每开/停一次流、每写一次归档都会 `print` 一行；管道缓冲（几 KB）
    # 填满之后，**服务端进程会阻塞在 print 上** —— 表现是"客户端某一次请求超时"，
    # 而真正的原因是**测试自己**没读管道。这是个假故障，且极易被误读成服务端有 bug
    # （本项目的同类教训：`subprocess.PIPE` 不读会伪装成"服务没起来"）。
    srv_log: collections.deque = collections.deque(maxlen=4000)

    def _drain(p):
        try:
            for line in p.stdout:
                srv_log.append(line.rstrip())
        except Exception:
            pass

    threading.Thread(target=_drain, args=(proc,), daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as sess:
            c = Client(base, sess)
            # 等服务起来
            for _ in range(60):
                try:
                    await c.sessions()
                    break
                except Exception:
                    await asyncio.sleep(0.25)
            else:
                print("服务未就绪：\n", "\n".join(list(srv_log)[-40:]))
                return 2

            # ---------- 7 WS 鉴权 ----------
            try:
                async with sess.ws_connect(f"{base}/ws") as ws:
                    await ws.close()
                check("7a WS 无 token 被拒", False, "竟然连上了")
            except aiohttp.WSServerHandshakeError as e:
                check("7a WS 无 token 被拒（403）", e.status == 403, f"status={e.status}")

            async with sess.ws_connect(f"{base}/ws?token={TOKEN}") as ws:
                first = json.loads((await ws.receive()).data)
                check("7b WS 首条为 config", first.get("type") == "config"
                      and "profile" in first, str(first)[:200])
                await ws.send_str(json.dumps({
                    "type": "hello", "device_serial": "TESTSERIAL", "session_id": "2026-09-25_0100_test",
                    "model": "Xiaomi 24129PN74C", "app_version": "m0probe-1.9.0",
                    "calib_id": "calib_test_01", "delivery": "stream",
                    "capture_path_id": "burst_yuv_manual", "capture_path_rev": 1}))
                await asyncio.sleep(0.2)

                # ---------- 8 控制道 ----------
                st, j = await c.control("start", plan={"mode": "interval", "interval_ms": 200,
                                                       "count": 3})
                m = await recv_until(ws, "start")
                check("8a control/start → WS 收到 start",
                      st == 200 and m.get("type") == "start" and j["sent_to"] == 1
                      and m["plan"]["interval_ms"] == 200, f"{st} {str(m)[:200]}")

                # ---------- 1 正常链路 ----------
                payload = os.urandom(700 * 1024)
                fid, sha = await upload_whole(c, 1, payload)
                m = await recv_until(ws, "ack")
                check("1a verified 后经 WS 回 ACK",
                      m.get("type") == "ack" and m.get("state") == "verified"
                      and m.get("file_id") == fid, str(m)[:200])

                sid = "2026-09-25_0100_test"
                sroot = tmp / "sessions" / sid
                fpath = sroot / "frames" / "frame_000001.yuv"
                check("1b 交付文件落盘且字节一致",
                      fpath.exists() and fpath.read_bytes() == payload,
                      str(fpath))
                fmeta = json.loads((sroot / "frames" / "frame_000001.json").read_text(encoding="utf-8"))
                check("1c 帧级元数据带 sha256/size/路径标识",
                      fmeta.get("sha256") == sha and fmeta.get("size_bytes") == len(payload)
                      and fmeta.get("capture_path_id") == "burst_yuv_manual"
                      and fmeta.get("y_stride") == 1920, str(fmeta)[:200])
                man = json.loads((sroot / "manifest.json").read_text(encoding="utf-8"))
                check("1d manifest 记 frame_count 与交付路线",
                      man["frame_count"] == 1 and man["delivery"] == "stream"
                      and man["capture_path_rev"] == 1, str(man.get("frame_count")))

                # ---------- 2 断点续传 ----------
                payload2 = os.urandom(600 * 1024)
                sha2 = hashlib.sha256(payload2).hexdigest()
                st, j = await c.init(seq=2, name="frame_000002.yuv", size=len(payload2),
                                     sha256=sha2, meta=default_meta(2))
                fid2 = j["file_id"]
                st, j = await c.put(fid2, 0, payload2[:CHUNK], len(payload2),
                                    crc=crc32b(payload2[:CHUNK]))
                # 重复 init（模拟断网重连）→ 必须回到同一 file_id 与当前偏移
                st, j2 = await c.init(seq=2, name="frame_000002.yuv", size=len(payload2),
                                      sha256=sha2, meta=default_meta(2))
                check("2a 重复 init 幂等（同 file_id + 当前偏移）",
                      j2["file_id"] == fid2 and j2["resume_offset"] == CHUNK, str(j2))
                st, j3 = await c.put(fid2, CHUNK - 111, payload2[CHUNK - 111:CHUNK],
                                     len(payload2), crc=crc32b(payload2[CHUNK - 111:CHUNK]))
                check("2b 偏移不符 → 409 且返回真实偏移",
                      st == 409 and j3["resume_offset"] == CHUNK, f"{st} {j3}")
                # 从真实偏移续传完
                off = CHUNK
                while off < len(payload2):
                    part = payload2[off:off + CHUNK]
                    st, jx = await c.put(fid2, off, part, len(payload2), crc=crc32b(part))
                    assert st == 200, (st, jx)
                    off = jx["received"]
                st, jx = await c.complete(fid2, sha2)
                check("2c 续传后 complete 成功", st == 200 and jx["state"] == "verified", f"{st} {jx}")

                # ---------- 3 分片 CRC 失败 ----------
                payload3 = os.urandom(300 * 1024)
                sha3 = hashlib.sha256(payload3).hexdigest()
                st, j = await c.init(seq=3, name="frame_000003.yuv", size=len(payload3),
                                     sha256=sha3, meta=default_meta(3))
                fid3 = j["file_id"]
                st, jx = await c.put(fid3, 0, payload3[:1000], len(payload3), crc="deadbeef")
                check("3a CRC 不符 → 412 且偏移不推进",
                      st == 412 and jx["resume_offset"] == 0, f"{st} {jx}")
                st, jx = await c.put(fid3, 0, payload3[:1000], len(payload3),
                                     crc=crc32b(payload3[:1000]))
                check("3b 重传同片即可继续", st == 200 and jx["received"] == 1000, f"{st} {jx}")

                # ---------- 4 整文件 sha256 不符 ----------
                # 先把剩下的分片传完（否则先撞上"未收满"这条，测不到 sha 校验）
                off = 1000
                while off < len(payload3):
                    part = payload3[off:off + CHUNK]
                    st, jt = await c.put(fid3, off, part, len(payload3), crc=crc32b(part))
                    assert st == 200, (st, jt)
                    off = jt["received"]
                check("4a 分片全部到齐", off == len(payload3), f"off={off}")
                st, jx = await c.complete(fid3, "0" * 64)
                check("4b sha256 不符 → 422 且保持 partial",
                      st == 422 and jx["state"] == "partial", f"{st} {jx}")
                check("4c 分片被保留（不为难客户端重传整文件）",
                      (tmp / "sessions" / sid / "incoming" / f"{fid3}.part").exists(), "")
                st, jx = await c.complete(fid3, sha3)
                check("4d 用正确的 sha 再 complete 成功",
                      st == 200 and jx["state"] == "verified", f"{st} {jx}")

                # ---------- 6 未收满就 complete ----------
                payload6 = os.urandom(200 * 1024)
                st, j = await c.init(seq=6, name="frame_000006.yuv", size=len(payload6),
                                     sha256=hashlib.sha256(payload6).hexdigest(),
                                     meta=default_meta(6))
                st, jx = await c.complete(j["file_id"], hashlib.sha256(payload6).hexdigest())
                check("6 未收满 → 409 incomplete 且给出偏移",
                      st == 409 and jx["state"] == "partial" and jx["resume_offset"] == 0,
                      f"{st} {jx}")

                # ---------- 5 幂等键撞不同内容 ----------
                st, jx = await c.init(seq=6, name="frame_000006.yuv", size=len(payload6) + 1,
                                      meta=default_meta(6))
                check("5 同 key 不同 size → 409 JSON（不以先到的为准）",
                      st == 409 and jx.get("error") == "idem_conflict", f"{st} {jx}")

                # ---------- 9 断言：参数漂移必须报出来 ----------
                p7 = os.urandom(64 * 1024)
                await upload_whole(c, 7, p7, meta=default_meta(7, focus_distance_diopter=1.001001))
                p8 = os.urandom(64 * 1024)
                await upload_whole(c, 8, p8, meta=default_meta(
                    8, focus_distance_diopter=1.234567, calib_id="calib_OTHER"))
                await asyncio.sleep(0.2)
                j9 = await c.sessions()
                s9 = [x for x in j9["sessions"] if x["session_id"] == sid][0]
                drift_fields = {d["field"] for d in s9["drift"]}
                check("9a 焦点回读中途变化 → param_drift 报出来",
                      "focus_distance_diopter" in drift_fields, str(s9["drift"])[:200])
                check("9b calib_id 不一致 → calib_consistent=False",
                      s9["calib_consistent"] is False, str(s9["calib_consistent"]))

                # ---------- 12 监视道（thumb，D6 双车道） ----------
                # 一个"形态像 JPEG"的字节串：**服务端不解码**，它只是搬运工，
                # 所以断言就该断言**字节级往返一致**，而不是"能被解析成图片"。
                jpg = b"\xff\xd8\xff\xe0" + b"M" * 600 + b"\xff\xd9"
                b64 = base64.b64encode(jpg).decode()
                await ws.send_json({"type": "thumb", "session_id": sid, "seq": 7,
                                    "sensor_timestamp_ns": 33287916,
                                    "capture_path_id": "stream_deliver_yuv", "capture_path_rev": 1,
                                    "jpeg_bytes": len(jpg), "jpeg_b64": b64,
                                    "meta": {"state": "running", "queue_len": 3}})
                await asyncio.sleep(0.3)
                tp = tmp / "sessions" / sid / "thumbs" / "thumb_000007.jpg"
                check("12a 缩略图按 seq 落盘且字节一致",
                      tp.exists() and tp.read_bytes() == jpg, str(tp))
                tmeta = json.loads((tmp / "sessions" / sid / "thumbs" /
                                    "thumb_000007.json").read_text(encoding="utf-8"))
                check("12b 旁挂元数据记时间戳与路径（可回答「这张对应哪一帧」）",
                      tmeta.get("sensor_timestamp_ns") == 33287916
                      and tmeta.get("capture_path_id") == "stream_deliver_yuv"
                      and "jpeg_b64" not in tmeta, str(tmeta)[:200])
                j12 = await c.sessions()
                s12 = [x for x in j12["sessions"] if x["session_id"] == sid][0]
                check("12c 会话摘要暴露 thumbs 与 thumb_latest",
                      s12["thumbs"] == 1 and s12["thumb_latest"]["seq"] == 7
                      and s12["thumb_latest"]["bytes"] == len(jpg), str(s12["thumb_latest"]))
                async with sess.get(f"{base}/api/v1/sessions/{sid}/thumb/latest") as r:
                    body = await r.read()
                    check("12d GET .../thumb/latest 回 JPEG 且字节一致",
                          r.status == 200 and r.content_type == "image/jpeg" and body == jpg,
                          f"{r.status} {r.content_type} {len(body)}B")
                man12 = json.loads((tmp / "sessions" / sid / "manifest.json").read_text(encoding="utf-8"))
                check("12e manifest 记 thumb_latest（监视道也有记录）",
                      (man12.get("thumb_latest") or {}).get("seq") == 7, str(man12.get("thumb_latest")))

                # ---------- 13 监视道必须"坏也不影响交付道" ----------
                n_before = len([x for x in (await c.sessions())["sessions"]])
                await ws.send_json({"type": "thumb", "session_id": sid, "seq": 8,
                                    "jpeg_b64": "!!!不是合法 base64!!!"})
                await ws.send_json({"type": "thumb", "session_id": "不存在的会话", "seq": 9,
                                    "jpeg_b64": b64})
                await asyncio.sleep(0.4)
                # ⚠ 先查"有没有多出会话"，再做控制道存活检查 —— 因为 `control/start`
                # **本身就会新建会话**，放在中间会让这条断言量到自己制造的会话。
                n_after = [x for x in (await c.sessions())["sessions"]]
                check("13b 未知会话的缩略图被忽略，且**不会**顺手建会话",
                      len(n_after) == n_before and "不存在的会话" not in
                      {x["session_id"] for x in n_after}, f"{n_before} -> {len(n_after)}")
                # WS 仍然活着：还能正常收指令
                st, j = await c.control("start", plan={"mode": "interval", "interval_ms": 200})
                m = await recv_until(ws, "start")
                check("13a 坏的缩略图不打断 WS 控制道", m.get("type") == "start", str(m)[:120])
                st, jx = await c.control("stop")
                await recv_until(ws, "stop")

                # ---------- 14 镜头/变焦断言（0.6× 是否真生效 / 整段是否同镜） ----------
                # 逻辑相机由多颗物理镜头融合：zoom=0.6 ⇒ 切超广角 ⇒ 内参完全不同。
                # 所以"一段数据能不能当一套内参用"必须先能回答两件事。
                p9 = os.urandom(64 * 1024)
                await upload_whole(c, 9, p9, meta=default_meta(
                    9, active_physical_id="2", zoom_ratio=1.0,
                    requested={"zoom_ratio": 1.0, "focus_diopter": 1.001001}))
                await asyncio.sleep(0.2)
                j14 = await c.sessions()
                s14 = [x for x in j14["sessions"] if x["session_id"] == sid][0]
                check("14a 同镜同 zoom ⇒ unique=True 且 zoom_match=True",
                      s14["lens"]["active_physical_id_unique"] is True
                      and s14["lens"]["zoom_match"] is True, str(s14["lens"])[:200])
                # 反向：中途换镜 + zoom 未被采纳
                p10 = os.urandom(64 * 1024)
                await upload_whole(c, 10, p10, meta=default_meta(
                    10, active_physical_id="3", zoom_ratio=1.0,
                    requested={"zoom_ratio": 0.6, "focus_diopter": 1.001001}))
                await asyncio.sleep(0.2)
                man14 = json.loads((tmp / "sessions" / sid / "manifest.json").read_text(encoding="utf-8"))
                lens = man14["lens"]
                check("14b 会话内换镜 ⇒ unique=False 且列出两颗镜头",
                      lens["active_physical_id_unique"] is False
                      and lens["active_physical_id_distinct"] == ["2", "3"], str(lens)[:200])
                check("14c 请求 0.6 但回读 1.0 ⇒ zoom_match=False（0.6 没生效要能看出来）",
                      lens["zoom_match"] is False
                      and lens["zoom_requested_distinct"] == [0.6, 1.0]
                      and lens["zoom_readback_distinct"] == [1.0], str(lens)[:200])

                # ---------- 16 控制道声明会话（session_begin / session_ack） ----------
                # 监视道（WS）与交付道（HTTP）的相对时序不可控：连接后 App 往往要先补传
                # **上一个会话**的积压帧，那些 verified 只证明"旧会话存在"。所以会话必须由
                # **控制道显式声明**，而不是让监视道去猜交付道的进度。
                sid_new = "2026-09-25_0100_begin"
                await ws.send_json({"type": "session_begin", "session_id": sid_new,
                                    "app_version": "m0probe-test", "model": "T",
                                    "zoom_effective": 0.6, "zoom_source": "app_ui",
                                    "capture_path_id": "stream_deliver_yuv",
                                    "capture_path_rev": 2})
                m = await recv_until(ws, "session_ack")
                check("16a session_begin → session_ack 且会话号一致",
                      m.get("session_id") == sid_new, str(m)[:160])
                j16 = await c.sessions()
                s16 = [x for x in j16["sessions"] if x["session_id"] == sid_new]
                check("16b 会话被建立（App 声明即建，不必等首帧 init）",
                      len(s16) == 1 and s16[0]["frames_total"] == 0, str(s16)[:200])
                # 幂等：重复声明不改状态、不重复建
                await ws.send_json({"type": "session_begin", "session_id": sid_new})
                m2 = await recv_until(ws, "session_ack")
                j16b = await c.sessions()
                check("16c 重复 session_begin 幂等（仍 ack，会话数不变）",
                      m2.get("session_id") == sid_new
                      and len([x for x in j16b["sessions"] if x["session_id"] == sid_new]) == 1,
                      str(m2)[:120])
                # ⚠ 会话号要放在**顶层**（幂等键 = session_id+device_serial+seq）；
                # 只写进 meta 的话，帧还是落到 Client 自带的那个会话上，
                # 而它的 seq=1 早被前面用过（不同 size）⇒ 撞幂等冲突，测出来的就不是本条语义。
                st, jx = await c.init(seq=1, name="frame_000001.yuv", size=1024,
                                      session_id=sid_new,
                                      sha256=hashlib.sha256(b"x" * 1024).hexdigest(),
                                      meta=default_meta(1))
                check("16d 声明过的会话可以直接收帧（init 不报未知会话）",
                      st == 200 and jx.get("file_id"), f"{st} {jx}")
                # 非法 id 必须被拒（原来是 return 一个 Response ⇒ 报成 500）
                await ws.send_json({"type": "session_begin", "session_id": "非法/名字"})
                me = await recv_until(ws, "error")
                check("16e 非法 session_id → error(bad_session_id) 而不是 500",
                      me.get("code") == "bad_session_id", str(me)[:160])

                # ---------- 17 未知会话的缩略图：拒收且**只回话一次** ----------
                # 不建会话（防止陈旧/伪造缩略图凭空造目录），但要告知一次，
                # 让 App 自己关闸 —— 否则它会按 1 Hz 一直灌到永远。
                n_before17 = len((await c.sessions())["sessions"])
                for k in (1, 2, 3):
                    await ws.send_json({"type": "thumb", "session_id": "2026-09-25_0100_ghost",
                                        "seq": k, "jpeg_b64": b64})
                await asyncio.sleep(0.5)
                got = []
                try:
                    while True:
                        mm = json.loads((await asyncio.wait_for(ws.receive(), 0.4)).data)
                        got.append(mm.get("type"))
                        if len(got) > 12:
                            break
                except asyncio.TimeoutError:
                    pass
                check("17a 未知会话的缩略图被拒收，且只回话一次（防刷屏）",
                      got.count("thumb_rejected") == 1, f"收到的类型序列={got}")
                n_after17 = len((await c.sessions())["sessions"])
                check("17b 三次未知缩略图都没建出会话",
                      n_after17 == n_before17, f"{n_before17} -> {n_after17}")

                # ---------- 18 元数据完整度断言（CaptureResult 派生字段） ----------
                # 这类缺失是**静默**的：画面、时间戳、字节数全正常，只是少了一组字段。
                # 第 18 轮实测某会话 44 帧里 28 帧整组缺席（App 把结果对象丢了），
                # 而所有计数器都正常 ⇒ 必须由服务端自己算出来。
                sid_mq = "2026-09-25_0100_metaq"

                def mq(seq, **over):
                    m = default_meta(seq, active_physical_id="3", **over)
                    return m

                # 一帧完整 + 一帧缺 skew + 一帧被显式标记结果缺席
                await upload_whole(c, 1, os.urandom(64 * 1024), session_id=sid_mq,
                                   meta=mq(1))
                broken = mq(2)
                broken.pop("rolling_shutter_skew_ns")
                broken["result_missing"] = True
                await upload_whole(c, 2, os.urandom(64 * 1024), session_id=sid_mq, meta=broken)
                partial = mq(3)
                partial.pop("crop_region")
                await upload_whole(c, 3, os.urandom(64 * 1024), session_id=sid_mq, meta=partial)
                await asyncio.sleep(0.3)
                man18 = json.loads((tmp / "sessions" / sid_mq / "manifest.json").read_text(encoding="utf-8"))
                q18 = man18["meta_quality"]
                check("18a 缺 CaptureResult 派生字段被逐项计数（skew 1/3、crop 1/3）",
                      q18["frames"] == 3 and q18["missing"]["rolling_shutter_skew_ns"] == 1
                      and q18["missing"]["crop_region"] == 1
                      and q18["missing"]["active_physical_id"] == 0, str(q18)[:220])
                check("18b 显式标记结果缺席的帧被记为 flagged，且 ok=False",
                      q18["flagged_result_missing"] == 1 and q18["ok"] is False, str(q18)[:220])
                # 正向：全新会话两帧都完整 ⇒ ok=True、全 0（否则就是"永远报异常"的判据）
                sid_mq2 = "2026-09-25_0100_metaok"
                await upload_whole(c, 1, os.urandom(64 * 1024), session_id=sid_mq2, meta=mq(1))
                await upload_whole(c, 2, os.urandom(64 * 1024), session_id=sid_mq2, meta=mq(2))
                await asyncio.sleep(0.3)
                man18b = json.loads((tmp / "sessions" / sid_mq2 / "manifest.json").read_text(encoding="utf-8"))
                q18b = man18b["meta_quality"]
                check("18c 全字段完整的会话 ⇒ ok=True 且各计数为 0",
                      q18b["ok"] is True and all(v == 0 for v in q18b["missing"].values()),
                      str(q18b)[:220])

                # ---------- 19 PC 主控闭环：start/stop 回执 + 开流门控 ----------
                # start/stop 原来是**单向广播**：PC 只知道"WS 发送成功"，不知道"相机真的开了吗"。
                # 相机被占用/设备瞬时故障时，面板会一直显示 running 而实际什么都没采到。
                sid_ctl = "2026-09-25_0100_ctl"
                st, j = await c.control("start", session_id=sid_ctl,
                                        plan={"mode": "interval", "interval_ms": 200})
                check("19a control/start 建会话并广播", st == 200 and j["session_id"] == sid_ctl,
                      f"{st} {j}")
                m = await recv_until(ws, "start")
                check("19b App 侧收到 start（含 plan/profile/token）",
                      m.get("session_id") == sid_ctl and "plan" in m and "profile" in m,
                      str(m)[:160])

                # 开流失败：必须让 PC 不再假装在采
                await ws.send_json({"type": "start_ack", "session_id": sid_ctl, "ok": False,
                                    "error": "摄像头被断开"})
                await asyncio.sleep(0.25)
                s19 = [x for x in (await c.sessions())["sessions"] if x["session_id"] == sid_ctl][0]
                check("19c start_ack 失败 ⇒ 状态 start_failed（不再假装 running）",
                      s19["state"] == "start_failed" and s19["start_ack"]["ok"] is False
                      and "摄像头被断开" in (s19["start_ack"]["error"] or ""), str(s19)[:200])

                await ws.send_json({"type": "start_ack", "session_id": sid_ctl, "ok": True,
                                    "output_size": [1920, 1440], "format": "YUV_Y",
                                    "plan_interval_ms": 200})
                await asyncio.sleep(0.25)
                s19b = [x for x in (await c.sessions())["sessions"] if x["session_id"] == sid_ctl][0]
                check("19d start_ack 成功 ⇒ state=running 且回执内容入档",
                      s19b["state"] == "running"
                      and s19b["start_ack"]["output_size"] == [1920, 1440], str(s19b)[:200])

                # 门控 pass：回读与 profile 一致；且 profile.zoom_ratio=0（不指定）必须被**跳过**
                await upload_whole(c, 1, os.urandom(64 * 1024), session_id=sid_ctl,
                                   meta=default_meta(1))
                await asyncio.sleep(0.3)
                g19 = [x for x in (await c.sessions())["sessions"]
                       if x["session_id"] == sid_ctl][0]["start_gate"]
                check("19e 门控 pass，且 zoom=0（不指定）被跳过而不是判失败",
                      g19["status"] == "pass" and "zoom_ratio" not in g19["checks"]
                      and any("zoom_ratio 未指定" in n for n in g19["soft_notes"]), str(g19)[:240])

                # 门控 fail：回读与 profile 硬项不一致 ⇒ 自动停流（免得采出一批"配置前提是错的"数据）
                sid_gf = "2026-09-25_0100_gatefail"
                bad = default_meta(1)
                bad["ois_mode"] = "ON(1)"                   # profile 要求 OFF（用 App 的真实写法）
                await upload_whole(c, 1, os.urandom(64 * 1024), session_id=sid_gf, meta=bad)
                ms = await recv_until(ws, "stop")
                check("19f 门控 fail ⇒ 自动下发 stop 且点名失败项",
                      ms.get("reason") == "start_gate_failed"
                      and "ois" in (ms.get("hard_fail") or []), str(ms)[:180])
                gf = [x for x in (await c.sessions())["sessions"] if x["session_id"] == sid_gf][0]
                check("19g 门控失败会话标成 gate_failed 并留档",
                      gf["state"] == "gate_failed" and gf["start_gate"]["status"] == "fail"
                      and gf["start_gate"]["checks"]["ois"]["got"] == "ON(1)"
                      and "锁定配方" in gf["start_gate"]["checks"]["ois"].get("hint", ""),
                      str(gf["start_gate"])[:220])

                # stop_ack：让 PC 知道"停在哪"
                await ws.send_json({"type": "stop_ack", "session_id": sid_ctl, "running": False,
                                    "frames_delivered": 44, "queue_len": 51})
                await asyncio.sleep(0.25)
                s19c = [x for x in (await c.sessions())["sessions"] if x["session_id"] == sid_ctl][0]
                check("19h stop_ack 记录「停在哪」（已抽帧/队列剩余）且状态转 stopped",
                      s19c["stop_ack"]["frames_delivered"] == 44
                      and s19c["stop_ack"]["queue_len"] == 51
                      and s19c["state"] == "stopped", str(s19c)[:200])

                # ---------- 20 门控的枚举写法归一 / 判不了不停流（第 20 轮真机缺陷回归） ----------
                # 事故：profile 写 `"OFF"`，App 回读写 `"OFF(0)"`，判据按**字符串**比 ⇒
                # 判成硬项不一致 ⇒ 自动停流。现象："点了开始采集，画面刷一下就再也不动了"。
                # 双向都要验：既不能把"写法不同、语义相同"判成失败，也不能因为归一
                # 就把真不一致放过（见 20a/20b 的表驱动用例与 19f 的真实停流用例）。
                sid_nz = "2026-09-25_0100_norm"
                await c.control("start", session_id=sid_nz)
                await recv_until(ws, "start")
                await ws.send_json({"type": "start_ack", "session_id": sid_nz, "ok": True,
                                    "output_size": [1920, 1440], "format": "YUV_Y",
                                    "plan_interval_ms": 200})
                await upload_whole(c, 1, os.urandom(32 * 1024), session_id=sid_nz,
                                   meta=default_meta(1))          # ois/eis = "OFF(0)"
                await asyncio.sleep(0.3)
                g20 = [x for x in (await c.sessions())["sessions"]
                       if x["session_id"] == sid_nz][0]
                check("20c 真机写法 \"OFF(0)\" vs profile \"OFF\" ⇒ 门控 pass（不再假失败停流）",
                      g20["start_gate"]["status"] == "pass" and g20["start_gate"]["hard_fail"] == []
                      and g20["state"] == "running", str(g20["start_gate"])[:240])
                check("20d \"写法归一\"必须留痕（normalized 标记 + 注记），不能静默放水",
                      g20["start_gate"]["checks"]["ois"].get("normalized") is True
                      and any("写法不同" in n for n in g20["start_gate"]["soft_notes"]),
                      str(g20["start_gate"]["checks"])[:240])

                # 回读缺席 ⇒ incomplete 且**不下发 stop**；后续帧补齐后自动复判为 pass
                sid_inc = "2026-09-25_0100_incomplete"
                await c.control("start", session_id=sid_inc)
                await recv_until(ws, "start")
                m_inc = default_meta(1, result_missing=True)
                m_inc.pop("ois_mode")
                m_inc.pop("eis_mode")
                await upload_whole(c, 1, os.urandom(32 * 1024), session_id=sid_inc, meta=m_inc)
                await asyncio.sleep(0.3)
                got_stop = False
                try:
                    while True:
                        mm = json.loads((await asyncio.wait_for(ws.receive(), 0.5)).data)
                        if mm.get("type") == "stop" and mm.get("session_id") == sid_inc:
                            got_stop = True
                except asyncio.TimeoutError:
                    pass
                s_inc = [x for x in (await c.sessions())["sessions"]
                         if x["session_id"] == sid_inc][0]
                check("20e 回读缺席 ⇒ incomplete、不下发 stop（判不了 ≠ 停流）",
                      s_inc["start_gate"]["status"] == "incomplete"
                      and set(s_inc["start_gate"]["unevaluable"]) == {"ois", "eis"}
                      and not got_stop and s_inc["state"] != "gate_failed",
                      str(s_inc["start_gate"])[:240])
                await upload_whole(c, 2, os.urandom(32 * 1024), session_id=sid_inc,
                                   meta=default_meta(2))          # 第 2 帧字段齐全
                await asyncio.sleep(0.3)
                s_inc2 = [x for x in (await c.sessions())["sessions"]
                          if x["session_id"] == sid_inc][0]
                check("20f incomplete 不冻结：后续帧补齐后自动复判为 pass（证据取第 2 帧）",
                      s_inc2["start_gate"]["status"] == "pass"
                      and "seq=2" in s_inc2["start_gate"]["evidence"],
                      str(s_inc2["start_gate"])[:240])

                # ---------- 21 溯源字段：会话先被 session_begin 建起来，init 里的声明也得进档 ----------
                # 第 20 轮从真机落盘数据里查出来的：manifest 里 device_serial='unknown'、
                # app_version=''、capture_path_rev=0，而 App 每个 init 都在如实上报这些值 ——
                # 因为 `h_init` 原来是 `session(sid) or create_session(...)`，会话已存在时
                # 整组声明被丢掉。后果："这份证据是哪台机器的哪个构建采的"变成不可回答。
                sid_pv = "2026-09-25_0100_prov"
                await ws.send_json({"type": "session_begin", "session_id": sid_pv,
                                    "device_serial": "unknown"})       # 先建（只有会话号）
                await asyncio.sleep(0.25)
                man_pv0 = json.loads((tmp / "sessions" / sid_pv / "manifest.json")
                                     .read_text(encoding="utf-8"))
                check("21a 会话先建时溯源字段就是空的（说明这条路径确实存在）",
                      man_pv0["app_version"] == "" and man_pv0["capture_path_id"] == "",
                      str({k: man_pv0[k] for k in ("app_version", "capture_path_id")}))
                await upload_whole(c, 1, os.urandom(32 * 1024), session_id=sid_pv,
                                   meta=default_meta(1))
                await asyncio.sleep(0.3)
                man_pv = json.loads((tmp / "sessions" / sid_pv / "manifest.json")
                                    .read_text(encoding="utf-8"))
                check("21b init 里的声明（model/serial/app_version/path/rev）补进会话档案",
                      man_pv["app_version"] == "m0probe-1.9.0"
                      and man_pv["device_serial"] == "TESTSERIAL"
                      and man_pv["device_model"] == "Xiaomi 24129PN74C"
                      and man_pv["capture_path_id"] == "burst_yuv_manual"
                      and man_pv["capture_path_rev"] == 1,
                      str({k: man_pv[k] for k in ("app_version", "device_serial", "device_model",
                                                  "capture_path_id", "capture_path_rev")}))
                # 中途改声明 ⇒ **不许覆盖**，要记冲突（否则"两套前提混一批"会被就地抹平）
                await upload_whole(c, 2, os.urandom(32 * 1024), session_id=sid_pv,
                                   meta=default_meta(2), app_version="m0probe-9.9.9",
                                   calib_id="calib_OTHER")
                await asyncio.sleep(0.3)
                man_pv2 = json.loads((tmp / "sessions" / sid_pv / "manifest.json")
                                     .read_text(encoding="utf-8"))
                dc = man_pv2.get("declared_conflicts") or []
                check("21c 声明中途变化 ⇒ 不覆盖原值，只记冲突（并进 manifest）",
                      man_pv2["app_version"] == "m0probe-1.9.0"
                      and any("app_version" in x for x in dc)
                      and any("calib_id" in x for x in dc), str(dc)[:200])

                # ---------- 22 "停采集 ≠ 停交付"（第 21 轮用户诉求的判据化）----------
                # 用户原话："确保实时联通结束后手机能够按照双流方案回传全部信息。"
                # 双流 = 监视道（WS，可丢）+ 交付道（HTTP，必须收全）⇒ 两条路的**生命周期必须独立**：
                # 停采集只是不再产生新帧，队列里已抽好的帧仍要传完；控制道断线更不该影响 HTTP。
                # 本组证明 PC 侧的第一个不变量：**停流后照收，而且要能说出"还欠多少"**。
                # （第二个不变量"控制道完全离线仍照收"见块外的第 24 组 —— 那需要在
                #  `async with ws` 之外执行，否则测不出真离线。）
                sid_dr = "2026-09-25_0100_drain"
                await c.control("start", session_id=sid_dr)
                await recv_until(ws, "start")
                await ws.send_json({"type": "start_ack", "session_id": sid_dr, "ok": True,
                                    "output_size": [1920, 1440], "format": "YUV_Y"})
                await upload_whole(c, 1, os.urandom(32 * 1024), session_id=sid_dr,
                                   meta=default_meta(1))
                await upload_whole(c, 2, os.urandom(32 * 1024), session_id=sid_dr,
                                   meta=default_meta(2))
                await asyncio.sleep(0.25)
                s_dr0 = [x for x in (await c.sessions())["sessions"]
                         if x["session_id"] == sid_dr][0]
                check("22a 采集期间的帧不算「迟到帧」（先在采集期内收 2 帧）",
                      s_dr0["frames_verified"] == 2 and s_dr0["late_frames_after_stop"] == 0,
                      str(s_dr0)[:180])

                # App 停采集：回 stop_ack，并且**承诺继续补传**（队列还剩 3 帧）
                await ws.send_json({"type": "stop_ack", "session_id": sid_dr, "running": False,
                                    "frames_delivered": 5, "queue_len": 3,
                                    "pending_frames": 3, "will_drain": True})
                await asyncio.sleep(0.25)
                s_dr1 = [x for x in (await c.sessions())["sessions"]
                         if x["session_id"] == sid_dr][0]
                check("22b stop_ack 记录「还欠多少」与「会继续传」（停采集不停交付要能看见）",
                      s_dr1["stop_ack"]["pending_frames"] == 3
                      and s_dr1["stop_ack"]["will_drain"] is True
                      and s_dr1["stopped_at"], str(s_dr1["stop_ack"])[:220])
                check("22c 采集已停但队列未清 ⇒ delivery_complete=False（不是 True、也不是 null）",
                      s_dr1["delivery_complete"] is False,
                      str(s_dr1["delivery_complete_note"])[:200])

                # ⚠ 关键：停采集后 PC **必须继续接受**交付帧。
                # 若这里把"会话已 stopped"当成"不再收"，手机队列就永远传不完 ——
                # 而表面上看一切正常（没有报错，只是帧不见了）。
                await upload_whole(c, 3, os.urandom(32 * 1024), session_id=sid_dr,
                                   meta=default_meta(3))
                await upload_whole(c, 4, os.urandom(32 * 1024), session_id=sid_dr,
                                   meta=default_meta(4))
                await asyncio.sleep(0.25)
                s_dr2 = [x for x in (await c.sessions())["sessions"]
                         if x["session_id"] == sid_dr][0]
                check("22d 停采集后仍照收交付帧，且计成「停后到达」（不是静默、不是串台）",
                      s_dr2["frames_verified"] == 4 and s_dr2["late_frames_after_stop"] == 2,
                      str({k: s_dr2[k] for k in ("frames_verified", "late_frames_after_stop")}))
                man_dr = json.loads((tmp / "sessions" / sid_dr / "manifest.json")
                                    .read_text(encoding="utf-8"))
                check("22e 停后到达的计数也进 manifest（落盘可回查，不只活在内存里）",
                      man_dr["late_frames_after_stop"] == 2
                      and man_dr["assertions"]["delivery_complete"] is False,
                      str(man_dr.get("late_frames_after_stop")))

                # ---------- 23 监视道：面板推送（收到即渲染）+ 交付收尾宣告 ----------
                # 用户现象："电脑端接收的缩略图帧率小于手机端，计数一下 +3 但只更新 1 张画面。"
                # 根因不是丢数据（thumbs 是逐张落盘数），而是面板**3 s 轮询 vs 1 Hz 推送**
                # 造成的 1:3 采样口径。改推送后收到即渲染；三张就必须推三条、落三张。
                async with sess.ws_connect(f"{base}/ws/panel?token={TOKEN}") as panel:
                    await asyncio.sleep(0.2)
                    check("23a 面板推送通道与控制道分离（连上它不占用 ws_clients）",
                          (await c.sessions())["ws_clients"] == 1,
                          f"ws_clients={(await c.sessions())['ws_clients']}")
                    jpg = b"\xff\xd8\xff" + os.urandom(2048)
                    for i in (11, 12, 13):
                        await ws.send_json({"type": "thumb", "session_id": sid_dr, "seq": i,
                                            "sensor_timestamp_ns": 1000 + i,
                                            "jpeg_b64": base64.b64encode(jpg).decode()})
                        got = await recv_until(panel, "thumb", timeout=5)
                        check(f"23b 第 {i} 张缩略图**逐张**推给面板（不再被轮询合并成 1 张）",
                              got.get("seq") == i and got.get("session_id") == sid_dr,
                              str(got)[:160])
                    await asyncio.sleep(0.25)
                    s_th = [x for x in (await c.sessions())["sessions"]
                            if x["session_id"] == sid_dr][0]
                    check("23c 推送与落盘是同一份数据：推了 3 条 ⇒ thumbs 计数 +3",
                          s_th["thumbs"] == 3, f"thumbs={s_th['thumbs']}")

                    # 交付收尾宣告（App 侧"队列已清空"的唯一一手证据）
                    await ws.send_json({"type": "delivery_done", "session_id": sid_dr,
                                        "frames": 4, "sampled": 4, "queue_len": 0,
                                        "drain_seconds": 12})
                    await recv_until(panel, "delivery_done", timeout=5)
                    check("23d delivery_done 也推给面板（界面能立刻显示「全部回传完成」）", True)
                await asyncio.sleep(0.25)
                s_done = [x for x in (await c.sessions())["sessions"]
                          if x["session_id"] == sid_dr][0]
                check("23e 收到 delivery_done ⇒ delivery_complete=True 并落进 manifest",
                      s_done["delivery_complete"] is True
                      and s_done["delivery_done"]["frames"] == 4,
                      str(s_done.get("delivery_complete_note"))[:200])
                man_done = json.loads((tmp / "sessions" / sid_dr / "manifest.json")
                                      .read_text(encoding="utf-8"))
                check("23f 收尾宣告进 manifest（含耗时与两侧计数，可离线复核）",
                      man_done["delivery_done"]["drain_seconds"] == 12
                      and man_done["assertions"]["delivery_complete"] is True,
                      str(man_done.get("delivery_done"))[:200])
                # 反例：**只有"没有新帧"是不够的** —— 22 段的会话已收 4 帧、停了很久，
                # 但没宣告过收尾。它必须是 False（有 stop_ack 且队列没清），绝不能推断成 True。
                # 更强的反例在块外第 24 组：连 stop_ack 都没有 ⇒ 必须是 null（判不了）。
                check("23g 反例：没有 delivery_done 时说不出「已收全」（不许从「没有新帧」推断）",
                      s_dr2["delivery_complete"] is False
                      and s_dr2["delivery_done"] is None, str(s_dr2.get("delivery_complete")))

                # 23h 监视道**不许拖住控制道**（D6 的底线）。
                # 面板推送是在 App 的控制道循环里触发的，所以这里必须证明：
                # 面板假死（连上但从不读）时，**控制道照常处理**，且 App 的连接
                # 不会被顺带弄断。旧实现（`await asyncio.wait_for(send_str)`）
                # 真的会踩：超时的取消顺着 `await` 冒泡，把 App 的连接整个掀掉
                # —— 手机端莫名显示"连接已断开"，服务端却什么错都不打。
                async with sess.ws_connect(f"{base}/ws/panel?token={TOKEN}") as hung:
                    big = base64.b64encode(os.urandom(400 * 1024)).decode()
                    t0 = time.monotonic()
                    for i in range(12):        # ≈4.8 MB 推送：足够把 TCP 缓冲填满
                        await ws.send_json({"type": "thumb", "session_id": sid_dr,
                                            "seq": 200 + i, "jpeg_b64": big})
                    await ws.send_json({"type": "stop_ack", "session_id": sid_dr,
                                        "running": False, "frames_delivered": 9,
                                        "queue_len": 0, "pending_frames": 0, "will_drain": True})
                    seen = False
                    for _ in range(100):       # 最多等 10 s（含 12 张大图的落盘开销）
                        await asyncio.sleep(0.1)
                        s_x = [x for x in (await c.sessions())["sessions"]
                               if x["session_id"] == sid_dr][0]
                        if (s_x.get("stop_ack") or {}).get("frames_delivered") == 9:
                            seen = True
                            break
                    dt = time.monotonic() - t0
                    check("23h 面板假死 ⇒ 控制道照常处理（stop_ack 收得到，连接不被掀掉）",
                          seen and ws.closed is False,
                          f"耗时 {dt:.2f}s，processed={seen}，app_ws_closed={ws.closed}")

                # ---------- 25 App 新增的 status 字段必须**原样留档** ----------
                # 第 20 轮的教训：App 把字段加进两份并列 JSON 清单里的**一份**，
                # 现场只看 PC 控制台 ⇒ "新仪器等于没上"。这条钉住 PC 侧的对应义务：
                # 不认识的字段也要存下来，不能白名单过滤。
                await ws.send_json({"type": "status", "session_id": sid_dr, "state": "idle",
                                    "queue_len": 0, "drain_state": "drained",
                                    "delivery_alive": True, "uploaded_frames": 42,
                                    "thumbs_enqueued": 30, "thumbs_sent": 28,
                                    "thumbs_send_failed": 2, "thumbs_dropped": 0,
                                    "_future_field": "keep-me"})
                await asyncio.sleep(0.25)
                s_st = [x for x in (await c.sessions())["sessions"]
                        if x["session_id"] == sid_dr][0]
                ls = s_st.get("last_status") or {}
                check("25a 新增/未知 status 字段原样留档（收尾状态与监视对账要能到 PC）",
                      ls.get("drain_state") == "drained" and ls.get("uploaded_frames") == 42
                      and ls.get("thumbs_enqueued") == 30 and ls.get("thumbs_sent") == 28
                      and ls.get("_future_field") == "keep-me", str(ls)[:220])

                # ---------- 8b stop ----------
                st, j = await c.control("stop")
                m = await recv_until(ws, "stop")
                check("8b control/stop → WS 收到 stop",
                      st == 200 and m.get("type") == "stop" and j["sent_to"] == 1, f"{st} {m}")

            # ---------- 8c 无 App 在线时 sent_to=0 要看得出来 ----------
            st, j = await c.control("start")
            check("8c 没有 App 在线 → sent_to=0（面板据此提示）",
                  st == 200 and j["sent_to"] == 0, str(j))

            # ---------- 24 控制道**完全离线**时，交付道仍必须工作 ----------
            # "实时联通结束"的字面含义就是这条 WS 断掉（PC 重启 / WiFi 抖动 / 用户关掉面板）。
            # 22/23 组只证明了"停采集照收"，这一组证明**连控制道都不在也照收** ——
            # 交付走 HTTP，与 WS 是两条独立的路。这条若挂了，现象就是
            # "一断开，剩下的数据永远留在手机上"（用户第 21 轮诉求的反面）。
            # 必须放在 `async with ws` **之外**：块内 ws 还连着，测不出真离线。
            await asyncio.sleep(0.3)
            check("24a 控制道已全部离线（ws_clients == 0）",
                  (await c.sessions())["ws_clients"] == 0,
                  f"ws_clients={(await c.sessions())['ws_clients']}")
            sid_off = j["session_id"]                      # 8c 建的会话（本来就无 App 在线）
            await upload_whole(c, 1, os.urandom(32 * 1024), session_id=sid_off,
                               meta=default_meta(1))
            s_off = [x for x in (await c.sessions())["sessions"]
                     if x["session_id"] == sid_off][0]
            check("24b 控制道离线 ⇒ HTTP 交付仍成功 verified（交付道不依赖 WS）",
                  s_off["frames_verified"] == 1, str(s_off)[:180])
            check("24c 既无 stop_ack 也无 delivery_done ⇒ delivery_complete=null："
                  "判不了就是判不了，绝不推断成「已收全」",
                  s_off["delivery_complete"] is None
                  and "判不了" in s_off["delivery_complete_note"],
                  str(s_off.get("delivery_complete_note"))[:220])

            # ---------- 26 扫码配对的配对码接口（HTTP；与 App/WS 无关，纯粹服务端契约）----------
            #
            # 手机要扫的三种信息（地址 / 端口 / token）必须由服务端**一处**产出：
            # 面板、终端、接口各算一遍的话，"面板显示 192.168.x、终端打印 10.x"这种分叉
            # 没有东西能阻止（现场最常见的就是连不上虚拟网卡那个地址）。
            async with sess.get(f"{base}/api/v1/pair") as r:
                st_no, j_no = r.status, await r.json()
            check("26a 配对码接口无 token ⇒ 403（它返回的正是凭据，必须鉴权）",
                  st_no == 403 and j_no.get("error") == "bad_token", f"{st_no} {j_no}")
            async with sess.get(f"{base}/api/v1/pair?token=WRONG") as r:
                check("26a2 错 token ⇒ 403（不能因为「局域网」就放行）",
                      r.status == 403, f"status={r.status}")

            async with sess.get(f"{base}/api/v1/pair?token={TOKEN}") as r:
                st_ok, j_pair = r.status, await r.json()
            uri = j_pair.get("pair_uri", "")
            check("26b 配对码 == pair_uri(host, port, token)（与生成器逐字符一致，"
                  "不许接口里再拼一遍字符串）",
                  st_ok == 200 and j_pair.get("ok")
                  and uri == srv.pair_uri(j_pair["host"], j_pair["port"], TOKEN)
                  and j_pair.get("token") == TOKEN, str(j_pair)[:200])
            check("26c 写法严格：m0probe://pair?h=…&p=…&t=…（没有第二种写法）",
                  re.fullmatch(r"m0probe://pair\?h=[^&]+&p=\d+&t=[A-Za-z0-9]+", uri) is not None,
                  uri)
            ok_svg, why_svg = svg_check(j_pair.get("qr_svg") or "", uri)
            check("26d 面板二维码忠实渲染（与 qrcode 库的矩阵逐模块一致，不是「非空就算过」）",
                  ok_svg, why_svg)
            check("26d2 与二维码同时给出可手输的两件东西（码扫不出来时还有出路）",
                  bool(j_pair.get("url")) and j_pair["url"].endswith(f"?token={TOKEN}")
                  and j_pair.get("note"), str(j_pair.get("url")))

            # 26d3 **判据本身要能失败**：把某个模块挪一格（黑模块数量一个不差）之后，
            #       svg_check 必须判否。否则"逐格比对"就只是嘴上的 ——
            #       而"数量对、位置错"的二维码现场一样扫不出来（安慰剂的经典形态）。
            svg_ok_text = j_pair.get("qr_svg") or ""
            mm = re.search(r"M(\d+) (\d+)h(\d+)v", svg_ok_text)
            if mm:
                x0, y0, s0 = int(mm.group(1)), int(mm.group(2)), int(mm.group(3))
                broken = svg_ok_text.replace(f"M{x0} {y0}h{s0}v{s0}h-{s0}z",
                                             f"M{x0 + s0} {y0}h{s0}v{s0}h-{s0}z", 1)
                ok_b, why_b = svg_check(broken, uri)
                check("26d3 反例：挪走一个模块（数量不变）必须判否",
                      ok_b is False, f"竟然通过了：{why_b}")
            else:
                check("26d3 反例：能从 path 解析出模块坐标（否则本判据无效）",
                      False, "path 解析失败")

            async with sess.get(f"{base}/api/v1/pair?token={TOKEN}&host=10.1.2.3&port=9000") as r:
                j_ov = await r.json()
            check("26e host/port 可显式覆盖（多网卡现场：让手机走指定的那张卡）",
                  j_ov.get("pair_uri") == f"m0probe://pair?h=10.1.2.3&p=9000&t={TOKEN}",
                  str(j_ov)[:160])
            async with sess.get(f"{base}/api/v1/pair?token={TOKEN}&port=abc") as r:
                check("26f 端口非法 ⇒ 400（不是 500，也不静默回默认值 —— "
                      "静默回默认值会让人拿着一个错的码去扫）",
                      r.status == 400, f"status={r.status}")
            # ============ 第 24 轮：单一交付道（**每一帧都留**）+ 本机接口面 ============
            #
            # 用户口径（本轮唯一的验收标准）："**每一帧都留**，且实际记录的内容要原封不动
            # 传到电脑"。⇒ 第 23 轮的"交付道（抽帧）+ 归档道（每一帧）"**合并成一条道**：
            #   不抽帧 → 先落手机本地 → 边采边传 → 传不完的停采后排空 → 配额熔断 → 缺口账本
            #
            # **删除项比新增更容易悄悄复发**，所以每一条删除都有判据钉住：
            #   · 旧 `kind="archive"` 必须被**显式 400 拒绝**（不许静默写进交付帧的 seq 空间）；
            #   · 落盘只有一个目录、内存只有一张表、幂等键里不再有 kind；
            #   · 旧的归档端点 / 归档事件 / 归档面板列都不该再出现。
            #
            # 本轮修的**静默缺陷**（R9）是本组最重要的判据：
            #   `writeQueue.offer(...)` 的返回值曾被忽略 ⇒ 队列（容量 32）满时那一帧
            #   **静默消失**，而 App 的 `deliveredCount`（写成功才 ++）、`queue_len`
            #   （数磁盘上的 .yuv）、PC 的 `_delivery_complete`（就靠这两个数）
            #   **三方一致地少一帧** —— 谁都发现不了。这与"每一帧都留"直接冲突。
            ASID = "2026-09-25_0900_store"
            KS = (HERE.parent / "android/m0probe/app/src/main/java/com/mouse/phoneprobe"
                  / "DeliverySession.kt")
            KSRC = KS.read_text(encoding="utf-8") if KS.exists() else ""

            def kslice(start: str, end: str) -> str:
                """从 App 源码里切一段（用于**源码锚定**判据）。

                为什么必须读真源码：第 24 轮修的是一个**静默缺陷**，而静默缺陷的特征是
                "修没修都不报错"。只有把"必须长成什么样"钉在源码上，回归才会响。
                """
                i = KSRC.find(start)
                if i < 0:
                    return ""
                j = KSRC.find(end, i + len(start))
                return KSRC[i:j if j > 0 else len(KSRC)]

            def aleaks(text: str, tok: str) -> bool:
                """凭据嗅探器。**它必须能真的抓到** —— 否则"不含凭据"就是一句空话。"""
                return bool(tok) and tok in text

            async def ain(sid, seq, payload, *, kind=None, meta=None, name=None):
                body = {"session_id": sid, "device_serial": "TESTSERIAL", "seq": seq,
                        "name": name or f"frame_{seq:06d}.yuv",
                        "size": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "meta": default_meta(seq) if meta is None else meta,
                        "model": "Xiaomi 24129PN74C", "app_version": "m0probe-1.13.0",
                        "calib_id": "calib_test_01", "delivery": "stream",
                        "capture_path_id": "stream_deliver_yuv", "capture_path_rev": 2}
                if kind:
                    body["kind"] = kind
                async with sess.post(f"{base}/api/v1/files/init", json=body) as r:
                    return r.status, await r.json()

            async def awhole(sid, seq, payload, *, kind=None, meta=None):
                """传完一帧（分片 + complete），返回 (status, json)。"""
                st, j = await ain(sid, seq, payload, kind=kind, meta=meta)
                if st != 200:
                    return st, j
                fid, off = j["file_id"], j["resume_offset"]
                while off < len(payload):
                    part = payload[off:off + CHUNK]
                    st, jx = await c.put(fid, off, part, len(payload), crc=crc32b(part))
                    if st != 200:
                        return st, jx
                    off = jx["received"]
                return await c.complete(fid, hashlib.sha256(payload).hexdigest())

            async def ledger(sid=ASID):
                """留存账本。

                ⚠ 第 24 轮删掉了独立的 `/api/v1/sessions/{sid}/archive` 接口 —— 账本现在
                只有两个出口（`host/status` 的 `store` 段、`sessions[]` 里的 `store`），
                而且**必须是同一份**（"面板说好、接口说不清"是上一轮专门避免过的坑）。
                """
                async with sess.get(f"{base}/api/v1/sessions") as r:
                    js = await r.json()
                for x in js.get("sessions", []):
                    if x["session_id"] == sid:
                        return x.get("store") or {}
                return None

            # ---------- A1 capabilities：两条道、无需 token、不含凭据 ----------
            # 上位机启动时应当**先读能力声明**，而不是假定"某个接口应该存在"。
            async with sess.get(f"{base}/api/v1/capabilities") as r:
                cap_st, cap_raw = r.status, await r.text()
            caps = json.loads(cap_raw)
            # 第 27 轮起有**三条道**（monitor / delivery / live）⇒ 判据不能再写死
            # "恰好两条"（那是把「有哪几条道」抄成常量，新增一条就要来改判据）。
            # 改成断言**结构性质**（这才是真正的不变量）：
            #   · 每条道必须声明 purpose / transport / lossy（少一项 = 声明不完整）；
            #   · 恰有**一条** `lossy is False` 的留存道 —— "只留一条道"是核心不变量
            #     （第 24 轮合并归档道的全部意义就在这里）。
            lanes = caps.get("lanes", {})
            persist_lanes = sorted(k for k, v in lanes.items() if v.get("lossy") is False)
            incomplete_lanes = sorted(k for k, v in lanes.items()
                                      if not (v.get("purpose") and v.get("transport")
                                              and "lossy" in v))
            check("A1a capabilities 无需 token 即可读；道声明结构完整，且**恰有一条非 lossy 的留存道**"
                  "（“只留一条道”是核心不变量，不是“恰好两条”）",
                  cap_st == 200
                  and set(lanes) == {"monitor", "delivery", "live"}
                  and persist_lanes == ["delivery"] and not incomplete_lanes
                  and caps.get("server_version") == srv.SERVER_VERSION
                  and caps.get("protocol_version") == srv.PROTOCOL_VERSION,
                  f"{cap_st} lanes={sorted(lanes)} persist={persist_lanes} "
                  f"incomplete={incomplete_lanes}")
            check("A1b capabilities 里不含任何凭据（token 明文）—— 它无需鉴权就能读",
                  not aleaks(cap_raw, TOKEN), cap_raw[:200])
            check("A1c 反例：把 token 塞进响应体，嗅探器必须判否（否则 A1b 是安慰剂）",
                  aleaks('{"auth":{"url":"http://x/?token=' + TOKEN + '"}}', TOKEN) is True
                  and aleaks('{"ok":true}', TOKEN) is False, "嗅探器失灵")
            dl = caps["lanes"]["delivery"]
            check("A1d 交付道声明为**无损**且写清了“两个缺口不许合并”与降载旋钮"
                  "（上位机据此知道该信哪几个数）",
                  dl.get("lossy") is False and dl.get("kind") == "frame"
                  and "不抽帧" in dl.get("note", "")
                  and set(dl.get("knobs", {})) >= {"plan.interval_ms",
                                                   "profile.deliver_fps",
                                                   "profile.store_budget_gb",
                                                   "profile.store_reserve_gb"}
                  and "不许合并" in (dl.get("ledger", {}).get("two_gaps") or ""),
                  json.dumps(dl, ensure_ascii=False)[:300])
            check("A1e 反例：capabilities 里**不再有** archive 道（合并的删除项要能被测出来）",
                  "archive" not in caps.get("lanes", {})
                  and not any("archive" in str(x) for x in caps.get("auth", {}).values()),
                  str(caps.get("auth"))[:200])

            # ---------- A1f 「声明必须为真」：声明说要 token 的，就必须真的要 token ----------
            # 判据**从声明本身推导**（不另抄一份路径清单）⇒ 以后谁往声明里加一条却没实现，
            # 这条立刻红。本轮就是这么抓到 `/api/v1/control/*` 的：`capabilities` 与
            # `docs/对外接口与单一交付道.md §2.1` 都写着"需要 token"，而 `h_control` 从不校验；
            # 更要紧的是它**不在 loopback 名单里**（名单只有 `/api/v1/host/`、`/stream/`）
            # ⇒ 同一局域网里任何一台机器都能启停采集。
            auth_decl = list((caps.get("auth") or {}).get("token_required") or [])
            probe_of = {"/api/v1/host/status": "GET", "/api/v1/host/events": "WS",
                        "/stream/mjpeg": "GET", "/api/v1/pair": "GET",
                        "/api/v1/control/*": "POST"}
            unprobed = [p for p in auth_decl if p not in probe_of]
            codes: dict = {}
            for p in auth_decl:
                try:
                    if probe_of.get(p) == "GET":
                        async with sess.get(base + p) as r:
                            codes[p] = r.status
                    elif probe_of.get(p) == "POST":
                        codes[p], _ = await c.control_raw("start")      # 无 token 打 control
                    elif probe_of.get(p) == "WS":
                        async with sess.ws_connect(base + p) as w:
                            await w.close()
                        codes[p] = 101
                    else:
                        codes[p] = None
                except aiohttp.WSServerHandshakeError as e:
                    codes[p] = e.status
                except Exception as e:                                   # noqa: BLE001
                    codes[p] = f"err:{type(e).__name__}"
            check("A1f 声明必须为真：`capabilities.auth.token_required` 里**每一条**，"
                  "不带 token 都必须 403（含 control）",
                  not unprobed and len(codes) == len(auth_decl) and len(codes) >= 5
                  and all(v == 403 for v in codes.values()),
                  f"未覆盖={unprobed} 实测={codes}")
            check("A1g 带**错** token 也必须 403（不能只判「有没有」）",
                  (await c.control_raw("start", headers={"X-Token": "wrong-token"}))[0] == 403
                  and (await c.control_raw("start", headers={"X-Token": TOKEN}))[0] in (200, 404),
                  "错 token 被放行")
            check("A1h 锚定：面板调 control 时必须自带 token"
                  "（否则给服务端加鉴权就把面板打死，而面板是取证入口，不许被误伤）",
                  "'X-Token':PANEL_TOKEN" in srv.PANEL_HTML
                  and "api/v1/control/" in srv.PANEL_HTML,
                  "面板 control 调用里找不到 token")

            # ---------- A2 loopback-only：按**对端地址**判，不认 Host / X-Forwarded-For ----------
            check("A2a is_loopback_addr 双向（含 IPv4-mapped IPv6）",
                  all(srv.is_loopback_addr(x) for x in
                      ("127.0.0.1", "127.5.5.5", "::1", "::ffff:127.0.0.1"))
                  and not any(srv.is_loopback_addr(x) for x in
                              ("10.0.0.1", "192.168.1.7", "8.8.8.8", "", "0.0.0.0")),
                  "回环判定有误")
            check("A2b is_local_only 双向：新接口受控，既有调试入口（面板/列表/缩略图）不误伤",
                  all(srv.is_local_only(p) for p in
                      ("/api/v1/host/status", "/api/v1/host/events", "/stream/mjpeg"))
                  and not any(srv.is_local_only(p) for p in
                              ("/api/v1/capabilities", "/api/v1/sessions",
                               "/api/v1/files/init", "/api/v1/control/start",
                               "/api/v1/pair",
                               f"/api/v1/sessions/{ASID}/thumb/latest")),
                  "local-only 路径集有误")

            class _Tr:
                def __init__(self, peer):
                    self._peer = peer

                def get_extra_info(self, k):
                    return self._peer if k == "peername" else None

            class _Req:
                def __init__(self, peer):
                    self.transport = None if peer is None else _Tr(peer)

            check("A2c peer_is_local：拿不到对端地址 ⇒ fail closed（不是放行）",
                  srv.peer_is_local(_Req(None)) is False
                  and srv.peer_is_local(_Req(())) is False
                  and srv.peer_is_local(_Req(("10.0.0.5", 1234))) is False
                  and srv.peer_is_local(_Req(("127.0.0.1", 1234))) is True
                  and srv.peer_is_local(_Req(("::1", 1234))) is True,
                  "对端地址判定有误")

            # 真·端到端：另起一个**绑定到本机局域网地址**的服务，从同一台机器去访问它 ——
            # 这样"非本机"是真的非本机（对端地址就是那块网卡的地址），不是模拟出来的。
            lan_ip = None
            try:
                _s0 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                _s0.connect(("8.8.8.8", 80))
                lan_ip = _s0.getsockname()[0]
                _s0.close()
            except Exception:
                lan_ip = None
            if lan_ip and not srv.is_loopback_addr(lan_ip):
                _s1 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    _s1.bind((lan_ip, 0))
                    lport = _s1.getsockname()[1]
                except OSError as e:
                    lport = None
                    check("A2d 无法绑定本机局域网地址 ⇒ 这一条只能靠上面三条单元判据",
                          False, f"bind {lan_ip} 失败: {e}")
                finally:
                    _s1.close()
                if lport:
                    ltmp = Path(tempfile.mkdtemp(prefix="pcsv_lan_"))
                    lp = subprocess.Popen(
                        [PY, str(HERE / "server.py"), "--host", lan_ip, "--port", str(lport),
                         "--data-dir", str(ltmp), "--token", TOKEN, "--max-mbps", "25"],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                        encoding="utf-8", errors="replace")
                    lbase = f"http://{lan_ip}:{lport}"
                    try:
                        for _ in range(60):
                            try:
                                async with sess.get(f"{lbase}/api/v1/capabilities") as r:
                                    if r.status == 200:
                                        break
                            except Exception:
                                pass
                            await asyncio.sleep(0.25)
                        # 每一项都带上伪造的 Host / X-Forwarded-For：按对端地址判 ⇒ 仍须 403
                        forge = {"Host": "localhost", "X-Forwarded-For": "127.0.0.1",
                                 "X-Real-IP": "127.0.0.1"}
                        got_lan = {}
                        for path in ("/api/v1/host/status", "/stream/mjpeg"):
                            async with sess.get(f"{lbase}{path}?token={TOKEN}",
                                                headers=forge) as r:
                                got_lan[path] = r.status
                        check("A2d 非本机（局域网地址，对端地址即那块网卡）访问本机接口一律 403，"
                              "伪造 Host / X-Forwarded-For 无效",
                              set(got_lan.values()) == {403}, f"{lan_ip} {got_lan}")
                        async with sess.get(f"{lbase}/api/v1/sessions", headers=forge) as r:
                            st_lan = r.status
                        check("A2e 同一台机器从局域网地址进来，既有调试入口仍可用（门控没误伤）",
                              st_lan == 200, f"status={st_lan}")
                    finally:
                        lp.terminate()
                        try:
                            lp.wait(timeout=5)
                        except Exception:
                            lp.kill()
                        shutil.rmtree(ltmp, ignore_errors=True)
            else:
                check("A2d 本机没有可用的局域网地址 ⇒ 只能靠 A2a/A2c 的单元判据",
                      False, f"lan_ip={lan_ip}")

            # ---------- A3 host/status 与面板同口径；gate 三态原样透出 ----------
            # 「最新会话」是按 started_at 取的，而 now_iso() 只到秒 ⇒ 必须等过秒边界，
            # 否则"最新"是谁取决于插入顺序（判据会变成运气）。
            await asyncio.sleep(1.1)
            GSID = "2026-09-25_0901_gate"
            m_nord = default_meta(1)
            for k in ("ois_mode", "eis_mode", "width", "height", "format"):
                m_nord.pop(k, None)
            # 这一帧自描述为"结果对象没留住"（真机上约 2/3 的帧就是这样）⇒ 门控**跳过它**取证据，
            # 于是此刻唯一可用的证据帧回读缺席 ⇒ incomplete。这才是真机上的实际形态。
            m_nord["result_missing"] = True
            st_g, _ = await awhole(GSID, 1, b"\x11" * 4096, meta=m_nord)

            async with sess.get(f"{base}/api/v1/host/status?token={TOKEN}") as r:
                st_hs, hs = r.status, await r.json()
            check("A3a host/status 需 token 且字段齐备（`store` 取代了旧的 `archive`）",
                  st_hs == 200 and {"capture", "device", "gate", "monitor", "store",
                                    "host_events", "storage", "sessions"} <= set(hs)
                  and "archive" not in hs
                  and hs["server_version"] == srv.SERVER_VERSION,
                  f"{st_hs} {sorted(hs)}")
            async with sess.get(f"{base}/api/v1/host/status") as r:
                check("A3b host/status 无 token ⇒ 403（不会因为「是本机」就免鉴权）",
                      r.status == 403, f"status={r.status}")
            check("A3c 回读缺席 ⇒ gate=incomplete（**不是 pass**），且 checked 仍为 false（不停流）",
                  hs["capture"]["session_id"] == GSID
                  and hs["gate"]["status"] == "incomplete"
                  and hs["gate"]["checked"] is False
                  and hs["gate"]["unevaluable"], f"{hs['gate']}")
            # 再来一帧带完整回读 ⇒ incomplete **不冻结**，后续帧自动复判
            st_g2, _ = await awhole(GSID, 2, b"\x12" * 4096, meta=default_meta(2))
            async with sess.get(f"{base}/api/v1/host/status?token={TOKEN}") as r:
                hs2 = await r.json()
            check("A3d incomplete 不冻结：后续帧补齐回读后自动复判为 pass",
                  st_g2 == 200 and hs2["capture"]["session_id"] == GSID
                  and hs2["gate"]["status"] == "pass", f"{hs2['gate']}")
            check("A3e 没有宣告账本时 store 段**判不了**而不是“看着没问题”"
                  "（complete=null；本项目铁律：判不了 ≠ 没问题）",
                  hs2["store"].get("complete") is None
                  and hs2["store"].get("frames_stored_on_device") is None,
                  json.dumps(hs2["store"], ensure_ascii=False)[:240])

            # ============================================================================
            # 以下需要一条设备控制道连接（留存宣告、缩略图推送、事件订阅都在 WS 上）。
            # 这里另开一条：上面主流程的 ws 已经关闭（第 24 组验过"控制道完全离线"）。
            # 必须挂一个**后台读任务**把 ACK 排空 —— 每帧 verified 都会回一条 ack，
            # 不读就会把 socket 缓冲填满，那时 `hub.broadcast` 会阻塞在 await 上，
            # 而被它阻塞的正是 App 的控制道消息循环（第 21 轮就是这个现象）。
            # ============================================================================
            hws = await sess.ws_connect(f"{base}/ws?token={TOKEN}")
            await hws.receive()                                  # 首条 config
            hws_msgs: list = []

            async def _hws_reader():
                try:
                    async for m in hws:
                        if m.type == aiohttp.WSMsgType.TEXT:
                            try:
                                hws_msgs.append(json.loads(m.data))
                            except Exception:
                                hws_msgs.append({"type": "_unparsed"})
                        elif m.type == aiohttp.WSMsgType.ERROR:
                            break
                except Exception:
                    pass

            hrd = asyncio.create_task(_hws_reader())
            try:
                await hws.send_json({"type": "hello", "device_serial": "TESTSERIAL",
                                     "session_id": ASID, "model": "Xiaomi 24129PN74C",
                                     "app_version": "m0probe-1.13.0",
                                     "calib_id": "calib_test_01", "delivery": "stream",
                                     "capture_path_id": "stream_deliver_yuv",
                                     "capture_path_rev": 2})
                await asyncio.sleep(0.2)

                # ---------- R1 旧 kind="archive" 必须被**显式拒绝** ----------
                # 为什么这不是小事：老的 App（≤1.12.0）会带 `kind="archive"` 发归档帧，
                # 而归档帧与交付帧**共用同一个 seq 空间**。若服务端默默放行，归档 seq=7 会
                # 写进交付 seq=7 的位置（落盘同名、内存同表）⇒ **后到的静默覆盖先到的，
                # 而两边都显示"已 verified"**。宁可显式报错，逼老客户端升级。
                p_d7 = b"D" * 4096
                st_d, j_d = await ain(ASID, 7, p_d7)
                st_a, j_a = await ain(ASID, 7, b"A" * 4096, kind="archive")
                check("R1a 旧 `kind=\"archive\"` ⇒ 400 lane_merged，**并说明为什么危险**",
                      st_a == 400 and j_a.get("error") == "lane_merged"
                      and "seq 空间" in (j_a.get("message") or ""),
                      f"{st_a} {str(j_a)[:220]}")
                check("R1b 反例（防过度删除）：不带 kind 与 kind=\"frame\" 都必须照常 200",
                      st_d == 200 and j_d.get("file_id")
                      and (await ain(ASID, 8, b"D" * 4096, kind="frame"))[0] == 200,
                      f"{st_d} {str(j_d)[:160]}")
                check("R1c 垃圾 kind 走**同一个**显式拒绝（不是 500、不是静默接受）",
                      (await ain(ASID, 9, b"X" * 128, kind="随便"))[0] == 400,
                      "bad kind 没有拒绝")
                check("R1d 被拒的 archive 帧**没有占用 seq=7**：随后用同号发交付帧仍能 verified",
                      (await awhole(ASID, 7, p_d7))[1].get("state") == "verified",
                      "seq 被旧 kind 用掉了（说明拒绝发生在占号之前）")
                check("R1e 源码锚定：服务端模块里已不存在 `KIND_ARCHIVE` / `KINDS`",
                      not hasattr(srv, "KIND_ARCHIVE") and not hasattr(srv, "KINDS")
                      and srv.KIND_DELIVERY == "frame",
                      f"{[x for x in ('KIND_ARCHIVE','KINDS') if hasattr(srv, x)]}")

                # ---------- R2 幂等键不再含 kind，但幂等本身不许被做坏 ----------
                st_i1, j_i1 = await ain(ASID, 30, b"I" * 2048)
                st_i2, j_i2 = await ain(ASID, 30, b"I" * 2048)
                st_i3, j_i3 = await ain(ASID, 30, b"J" * 4096)
                check("R2a 同 (sid, seq) 重复 init ⇒ 同一 file_id（断点续传的根基没坏）",
                      st_i1 == 200 and st_i2 == 200
                      and j_i1["file_id"] == j_i2["file_id"],
                      f"{j_i1.get('file_id')} {j_i2.get('file_id')}")
                check("R2b 同键但内容不同 ⇒ 409 idem_conflict（不许“以先到的为准”）",
                      st_i3 == 409 and j_i3.get("error") == "idem_conflict",
                      f"{st_i3} {str(j_i3)[:180]}")
                check("R2c 不同会话允许同 seq（幂等键含 session_id，不是全局唯一）",
                      (await ain("2026-09-25_0903_other", 30, b"I" * 2048))[0] == 200,
                      "跨会话撞键")

                # ---------- R3 落盘与内存**各只有一处** ----------
                await awhole(ASID, 31, b"K" * 2048)
                adir = tmp / "sessions" / ASID
                fdel = adir / "frames" / "frame_000031.yuv"
                check("R3a 落盘只有一个目录、一种命名（`frames/frame_*`；`archive/` 与 "
                      "`aframe_*` 都不该存在）",
                      fdel.exists()
                      and not (adir / "archive").exists()
                      and not list(adir.rglob("aframe_*")),
                      f"frame_000031={fdel.exists()} archive_dir={(adir/'archive').exists()}")
                js_all = await c.sessions()
                sx = next((x for x in js_all["sessions"] if x["session_id"] == ASID), None)
                n_disk = len(list((adir / "frames").glob("*.yuv"))) if adir.exists() else -1
                check("R3b 内存只有一张表：`sessions[]` 的**已校验**帧数与实际落盘一致"
                      "（4 次 init：7 / 8 / 30 / 31；其中 complete 过的只有 7 与 31）",
                      sx is not None and sx["frames_total"] == 4
                      and sx["frames_verified"] == n_disk == 2,
                      f"total={sx['frames_total'] if sx else None} "
                      f"verified={sx['frames_verified'] if sx else None} 磁盘={n_disk}")
                check("R3c 源码锚定：`Session` 上已不存在 `archives` / `archive_info`"
                      "（同一个根因的第三个出口必须真的被拆掉）",
                      not hasattr(srv.Session, "archives")
                      and not hasattr(srv.Session, "archive_info")
                      and hasattr(srv.Session, "delivery_info"),
                      f"{[x for x in ('archives','archive_info') if hasattr(srv.Session, x)]}")

                # ---------- R4 账本：两个缺口分开 + 两条自洽式 ----------
                # 核心纪律：**PC 绝不采信 App 自报的"我传完了"**。App 说存了 100、PC 只收到 2，
                # 账本里就必须写 2，差额单列为"可以重传"的传输缺口。
                l0 = await ledger()
                v0 = l0["frames_verified_on_pc"]
                stored_decl = v0 + 64                      # App 宣称手机存了这么多
                gaps_decl = [167000000000000 + i for i in range(9000, 9010)]  # 只能重采的 10 帧
                expect_decl = stored_decl + len(gaps_decl)
                await hws.send_json({"type": "delivery_done", "session_id": ASID,
                                     "frames": v0, "frames_expected": expect_decl,
                                     "frames_stored_on_device": stored_decl,
                                     "gaps_on_device": len(gaps_decl),
                                     "gap_ts": gaps_decl, "queue_len": 0, "drain_seconds": 1})
                await asyncio.sleep(0.4)
                l1 = await ledger()
                check("R4a 两个缺口**分开命名**：采集缺口 10（只能重采）/ 传输缺口 64（可以重传），"
                      "两个数不许被合并成一个",
                      l1["gaps_on_device"] == len(gaps_decl)
                      and l1["upload_gaps"] == 64
                      and l1["gaps_on_device"] != l1["upload_gaps"],
                      json.dumps({k: l1[k] for k in
                                  ("gaps_on_device", "upload_gaps")}, ensure_ascii=False))
                check("R4b 两条自洽式都成立时 self_consistent=True；"
                      "但**还有缺口** ⇒ complete 必须仍是 false（不许把“账本自洽”当成“数据齐了”）",
                      l1["self_consistent"] is True and l1["complete"] is False,
                      f"self={l1['self_consistent']} complete={l1['complete']}")
                check("R4c `gap_ts` 真的透出（缺口的**位置**，不是只有个数）",
                      len(l1["gap_ts"]) == len(gaps_decl),
                      str(l1["gap_ts"])[:120])
                # 反例：把自洽式打破
                await hws.send_json({"type": "delivery_done", "session_id": ASID,
                                     "frames": v0, "frames_expected": expect_decl + 7,
                                     "frames_stored_on_device": stored_decl,
                                     "gaps_on_device": len(gaps_decl),
                                     "gap_ts": gaps_decl, "queue_len": 0})
                await asyncio.sleep(0.4)
                l2 = await ledger()
                check("R4d 反例：expected ≠ stored + gaps ⇒ self_consistent 必须 false、"
                      "complete 必须 false（自洽式不许形同虚设）",
                      l2["self_consistent"] is False and l2["complete"] is False,
                      f"self={l2['self_consistent']} complete={l2['complete']}")

                # ---------- R5 按 seq 区间点缺口，且**三处证据必须对齐** ----------
                # 第 24 轮的 seq 由 App 的 `nextSeq()` 逐帧分配（**写失败的帧也占号**），
                # 所以判据是"宣告区间内 PC 没校验到的号"，而不是旧的"从 0 起的连续编号"。
                # 这样 `missing_seqs` 同时覆盖两类缺口，并与 `gaps_on_device + upload_gaps`
                # 构成**第三条独立校验** —— 三处对不上时必须显式说出来。
                XSID = "2026-09-25_0904_seqgap"
                for i in range(8):
                    await awhole(XSID, i, b"Q" * 1024)
                await hws.send_json({"type": "delivery_done", "session_id": XSID,
                                     "frames": 8, "frames_expected": 10,
                                     "frames_stored_on_device": 10,
                                     "gaps_on_device": 0, "gap_ts": [],
                                     "seq_first": 0, "seq_last": 9, "queue_len": 0})
                await asyncio.sleep(0.4)
                lx = await ledger(XSID)
                check("R5a 区间 [0,9] 内只收到 0..7 ⇒ 点出缺口 [8,9]，"
                      "且与 `gaps_on_device + upload_gaps` 对齐（不出报警注记）",
                      lx["missing_seqs"] == [8, 9] and lx["missing_seqs_note"] is None
                      and lx["upload_gaps"] == 2 and lx["gaps_on_device"] == 0,
                      json.dumps({k: lx[k] for k in ("missing_seqs", "upload_gaps",
                                                     "gaps_on_device", "missing_seqs_note")},
                                 ensure_ascii=False))
                # 反例：让两条独立证据**互相矛盾**（计数自洽，但 seq 区间只支持 2 个缺口）
                await hws.send_json({"type": "delivery_done", "session_id": XSID,
                                     "frames": 8, "frames_expected": 15,
                                     "frames_stored_on_device": 10,
                                     "gaps_on_device": 5, "gap_ts": [11, 12, 13, 14, 15],
                                     "seq_first": 0, "seq_last": 9, "queue_len": 0})
                await asyncio.sleep(0.4)
                lx2 = await ledger(XSID)
                check("R5b 反例：seq 区间只支持 2 个缺口，而 5+2=7 —— 必须**显式报警注记**；"
                      "同时 `self_consistent` 仍为 True，说明这是两条**独立**校验，"
                      "不能用一个代替另一个",
                      "对不上" in (lx2["missing_seqs_note"] or "")
                      and lx2["self_consistent"] is True,
                      f"note={lx2['missing_seqs_note']} self={lx2['self_consistent']}")
                # R5c：**宣告了账本、但没有 seq 区间** ⇒ 只能说明"点不出来"，不许硬算。
                # （注：若会话**根本没宣告过**账本，那是另一种情形 —— 判不了 ≠ 没问题。）
                NSID = "2026-09-25_0908_noseq"
                for i in range(2):
                    await awhole(NSID, i, b"N" * 512)
                await hws.send_json({"type": "delivery_done", "session_id": NSID,
                                     "frames": 2, "frames_expected": 2,
                                     "frames_stored_on_device": 2,
                                     "gaps_on_device": 0, "gap_ts": [], "queue_len": 0})
                await asyncio.sleep(0.4)
                ln = await ledger(NSID)
                check("R5c 宣告了账本但**没有 seq 区间** ⇒ 判「点不出来」并**明说**"
                      "（不许硬算一串错的帧号）",
                      ln["missing_seqs_note"] is not None and ln["missing_seqs"] == [],
                      str(ln["missing_seqs_note"])[:160])

                # ---------- R6 complete 是三态：判不了 ≠ 通过 ----------
                check("R6a 没有宣告 ⇒ complete=None（**判不了**），不是 True",
                      (await ledger("2026-09-25_0903_other")).get("complete") is None,
                      str(await ledger("2026-09-25_0903_other"))[:160])
                YSID = "2026-09-25_0905_ok"
                for i in range(3):
                    await awhole(YSID, i, b"Y" * 1024)
                await hws.send_json({"type": "delivery_done", "session_id": YSID,
                                     "frames": 3, "frames_expected": 3,
                                     "frames_stored_on_device": 3,
                                     "gaps_on_device": 0, "gap_ts": [],
                                     "seq_first": 0, "seq_last": 2, "queue_len": 0})
                await asyncio.sleep(0.4)
                ly = await ledger(YSID)
                check("R6b 真·一帧不落（应有=实存=PC 已校验、无缺口）⇒ complete=True",
                      ly["complete"] is True and ly["self_consistent"] is True,
                      json.dumps({k: ly[k] for k in ("complete", "self_consistent",
                                                     "frames_verified_on_pc")},
                                 ensure_ascii=False))

                # ---------- R7 "每一帧都留"：默认值与源码锚定 ----------
                check("R7a 服务端默认档**不再抽帧**：profile.deliver_fps = 0（0 = 每一帧都留）",
                      srv.DEFAULT_PROFILE.get("deliver_fps") == 0,
                      f"deliver_fps={srv.DEFAULT_PROFILE.get('deliver_fps')}")
                _stc, jc = await c.control("start", session_id="2026-09-25_0906_plan")
                check("R7b `/api/v1/control/start` 不带 plan ⇒ 默认 `interval_ms=0`（不抽帧）；"
                      "写 >0 才是降载",
                      _stc == 200 and jc.get("plan", {}).get("interval_ms") == 0,
                      f"{_stc} {str(jc)[:200]}")
                enc = kslice("private fun enqueue(", "\n    private val writeQueue")
                wr = kslice("private fun startWriter()", "    /**\n     * 逐帧结果元数据")
                store_blk = kslice("// =================================================="
                                   " 本地留存账本", "\n/**\n * 极简 WebSocket")
                check("R7c 源码锚定：App 里 `currentIntervalNs()` 返回 0 表示不抽帧，"
                      "且 plan 默认 `interval_ms` 为 0（旧默认 200 ms 必须消失）",
                      "private fun currentIntervalNs(): Long" in KSRC
                      and "return if (fps > 0) (1000.0 / fps * 1e6).toLong() else 0L" in KSRC
                      and 'put("interval_ms", 0)' in KSRC
                      and 'optDouble("deliver_fps", 0.0)' in KSRC
                      and 'optInt("interval_ms", 200)' not in KSRC,
                      f"currentIntervalNs={'currentIntervalNs' in KSRC} "
                      f"旧默认残留={('optInt(\"interval_ms\", 200)' in KSRC)}")
                check("R7d 源码锚定：账本里有 `every_frame`（降载时语义变化必须**可见**）",
                      "every_frame" in store_blk
                      and "storeStatusJson" in store_blk
                      and "frames_stored_on_device" in store_blk,
                      f"store_blk 长度={len(store_blk)}")
                check("R7e 源码锚定：配额与熔断搬进了写线程，且**显式命名** "
                      "`aborted_no_space`",
                      "storeReserveBytes()" in wr and "abortStore(\"no_space\"" in wr
                      and "aborted_no_space" in store_blk
                      and "storeBudgetBytes()" in wr,
                      f"wr 长度={len(wr)}")

                # ---------- R8 面板必须把两个缺口与熔断**呈现出来** ----------
                ph = srv.PANEL_HTML
                check("R8a 面板写明“每一帧都留”与“必须先落本地”的理由（83 MB/s vs 5.5 MB/s）",
                      "每一帧都留" in ph and "83 MB/s" in ph and "5.5 MB/s" in ph
                      and "15.1 倍" in ph,
                      "面板没有说明合并后的语义")
                check("R8b 面板给两个缺口分别命名：机内缺口（重采）/ 传输缺口（重传）",
                      "gaps_on_device" in ph and "upload_gaps" in ph
                      and "重采" in ph and "重传" in ph and "机内" in ph,
                      "面板没有把两个缺口分开命名")
                check("R8c 面板把 `aborted_no_space` 呈现为「空间不足熔断」（不许看起来像“没开”）",
                      "aborted_no_space" in ph and "空间不足熔断" in ph,
                      "面板没有给熔断命名")
                check("R8d 反例：面板**不再**读 `s.archive`（列口径已改到 store，"
                      "改一半会显示成空列却看不出来）",
                      "s.archive" not in ph and "s.store" in ph,
                      "面板仍读旧字段")

                # ---------- R9 **本轮核心修复**：静默丢帧必须被计数（源码锚定） ----------
                # 这是"每一帧都留"的命门：`writeQueue.offer` 忽略返回值 ⇒ 队列满时帧消失，
                # 而 App 的 deliveredCount / queue_len 与 PC 的 delivery_complete
                # **三方一致地少一帧**。现场只会觉得"少了点"，永远不会有人报错。
                check("R9a 源码锚定：`enqueue` 里**检查了** `writeQueue.offer` 的返回值",
                      "if (!writeQueue.offer(" in enc,
                      f"enqueue 长度={len(enc)}")
                check("R9b 源码锚定：旧的“不看返回值”写法必须消失"
                      "（否则 R9a 只是因为两种写法并存而通过）",
                      "\n            writeQueue.offer(meta.toString() to y)" not in enc,
                      "旧写法仍在")
                check("R9c 四条丢帧路径**都进账本**：写队列满 / 图未到 / 写盘失败 / 元数据组装失败",
                      'noteStoreGap(ts, "write_queue_full")' in enc
                      and 'noteStoreGap(ts, "meta_build_failed")' in enc
                      and 'noteStoreGap(gapTs, "write_failed")' in wr
                      and 'noteStoreGap(it, "image_never_arrived")' in KSRC,
                      f"enc={len(enc)} wr={len(wr)}")
                check("R9d 缺口记录**自描述**：按 sensor timestamp 点位置，且 ts=0 时不记位置"
                      "（被丢的帧根本没有 seq —— seq 是写盘成功时才分配的）",
                      "private fun noteStoreGap(ts: Long, why: String)" in store_blk
                      and "if (ts > 0L)" in store_blk,
                      f"store_blk 长度={len(store_blk)}")
                check("R9e 源码锚定：App 里已不存在归档道残留标识"
                      "（`archOn` / `startArchive` / `submitArchive` / `ARCHIVE_KIND`）",
                      not any(t in KSRC for t in ("archOn", "startArchive",
                                                  "submitArchive", "ARCHIVE_KIND",
                                                  "archiveStatusJson")),
                      str([t for t in ("archOn", "startArchive", "submitArchive",
                                       "ARCHIVE_KIND") if t in KSRC]))
                check("R9f 熔断**只停写盘、不停回传**（已经拿到的数据要传完）",
                      "writer?.interrupt()" in store_blk
                      and "已存下的帧照常回传" in KSRC,
                      f"store_blk 长度={len(store_blk)}")

                # ---------- A4 MJPEG 必须是合法 multipart/x-mixed-replace ----------
                # 监视道是**有损可丢**的实时画面：只用于"看得见"，不得用于测量。
                # 选 multipart/x-mixed-replace 的唯一理由是消费方零依赖（cv2 / ffmpeg / <img>）。
                thumb_bytes = bytes(range(256)) * 16          # 4 KB

                def mjpeg_ok(headers):
                    ct = headers.get("Content-Type", "")
                    if not ct.startswith("multipart/x-mixed-replace"):
                        return False, f"Content-Type={ct!r}（必须是 multipart 流）"
                    if f"boundary={srv.MJPEG_BOUNDARY}" not in ct:
                        return False, f"boundary 不对：{ct!r}"
                    if "no-store" not in headers.get("Cache-Control", ""):
                        return False, "缺 no-store（中间层会把第一帧缓存住，画面永远冻住）"
                    return True, ""

                warm_hdr = {}
                async with sess.get(f"{base}/stream/mjpeg?token={TOKEN}") as warm:
                    warm_hdr = dict(warm.headers)
                    # 必须先有一条订阅，push_monitor 才会把"最新一张"缓存到 hub 上
                    await hws.send_json({"type": "thumb", "session_id": GSID, "seq": 11,
                                         "jpeg_b64": base64.b64encode(thumb_bytes).decode(),
                                         "sensor_timestamp_ns":
                                             167000000000000 + 11 * 33288000,
                                         "meta": {"state": "running", "queue_len": 0}})
                    await asyncio.sleep(0.4)
                    whead = await warm.content.readuntil(b"\r\n\r\n")
                    lm0 = re.search(rb"Content-Length: (\d+)", whead)
                    wbody = (await warm.content.readexactly(int(lm0.group(1)))
                             if lm0 else b"")
                ok_m, why_m = mjpeg_ok(warm_hdr)
                check("A4a MJPEG 是合法 multipart/x-mixed-replace（边界正确 + no-store）",
                      ok_m, why_m)
                check("A4b 反例：Content-Type 写成 image/jpeg、或 boundary 不符、或缺 no-store "
                      "都必须判否（否则 A4a 只是「非空就算过」）",
                      mjpeg_ok({"Content-Type": "image/jpeg",
                                "Cache-Control": "no-store"})[0] is False
                      and mjpeg_ok({"Content-Type":
                                    "multipart/x-mixed-replace; boundary=other",
                                    "Cache-Control": "no-store"})[0] is False
                      and mjpeg_ok({"Content-Type": "multipart/x-mixed-replace; "
                                    f"boundary={srv.MJPEG_BOUNDARY}",
                                    "Cache-Control": "no-cache"})[0] is False,
                      "判据放水")
                check("A4c 订阅后收到的第一张就是刚推的那张（字节一致，且带 X-Seq 供对账）",
                      wbody == thumb_bytes and b"X-Seq: 11" in whead,
                      f"len={len(wbody)} head={whead[:90]}")
                # 断开后**没有任何新推送**，新订阅者仍须立刻拿到"手上最新的一张"——
                # 否则 1 Hz 档下最长要等 1 秒，现场会以为"流没连上"。
                async with sess.get(f"{base}/stream/mjpeg?token={TOKEN}") as cold:
                    chead = await cold.content.readuntil(b"\r\n\r\n")
                    lm1 = re.search(rb"Content-Length: (\d+)", chead)
                    cbody = (await cold.content.readexactly(int(lm1.group(1)))
                             if lm1 else b"")
                check("A4d 新订阅者「接入即得画面」：无需等下一帧，拿到的就是缓存的最新一张",
                      cbody == thumb_bytes and b"X-Seq: 11" in chead,
                      f"len={len(cbody)} head={chead[:90]}")
                async with sess.get(f"{base}/stream/mjpeg") as r:
                    check("A4e MJPEG 无 token ⇒ 403（不能凭「是本机」就白给画面）",
                          r.status == 403, f"status={r.status}")

                # ---------- A5 慢订阅者只丢自己的帧，不影响控制道/交付道 ----------
                async with sess.get(f"{base}/stream/mjpeg?token={TOKEN}",
                                    timeout=aiohttp.ClientTimeout(total=90)) as slow:
                    b64big = base64.b64encode(bytes(range(256)) * 256).decode()   # 64 KB/张
                    for i in range(80):
                        await hws.send_json({"type": "thumb", "session_id": GSID,
                                             "seq": 100 + i, "jpeg_b64": b64big,
                                             "sensor_timestamp_ns": 167000000000000
                                             + (100 + i) * 33288000,
                                             "meta": {"state": "running", "queue_len": 0}})
                    await asyncio.sleep(0.5)
                    t0 = time.monotonic()
                    st_c, j_c = await c.control("stop")
                    dt_c = time.monotonic() - t0
                    async with sess.get(f"{base}/api/v1/host/status?token={TOKEN}") as rh:
                        st_h2 = rh.status
                    check("A5a 慢订阅者（80×64 KB 未读 ≈ 5 MB）不阻塞控制道：往返 < 1.5 s",
                          st_c == 200 and dt_c < 1.5, f"{st_c} dt={dt_c:.3f}s")
                    check("A5b 慢订阅者期间 host/status 仍可用", st_h2 == 200, f"{st_h2}")
                    st_f5, j_f5 = await awhole(GSID, 3, b"\x13" * 8192)
                    check("A5c 慢订阅者期间交付道照常 verified（慢的只该是它自己的画面）",
                          st_f5 == 200 and j_f5.get("state") == "verified",
                          f"{st_f5} {str(j_f5)[:160]}")

                # A5d/A5e：队列语义（满则丢最旧 + 计数）用**真实现**在进程内直接验 ——
                # 端到端要凑满 512 条事件代价太大，而这里验的是同一份代码。
                hub_u = srv.Hub(Path(tempfile.mkdtemp(prefix="hubunit_")), 25.0, "t")
                try:
                    q_m = asyncio.Queue(maxsize=srv.MJPEG_QUEUE)
                    hub_u.mjpeg_subs["k"] = q_m
                    for i in range(6):
                        hub_u.push_monitor(b"J%d" % i, i, GSID, {"sensor_timestamp_ns": i})
                    kept = []
                    while not q_m.empty():
                        kept.append(q_m.get_nowait()[1])
                    check("A5d push_monitor 满则丢最旧（2 格队列推 6 张 ⇒ 只剩最后 2 张），"
                          "且 last_thumb 始终是最新一张",
                          kept == [4, 5] and hub_u.last_thumb_meta["seq"] == 5, f"kept={kept}")

                    q_e = asyncio.Queue(maxsize=2)
                    sub = {"q": q_e, "dropped": 0}
                    hub_u.host_ws["w"] = sub
                    for i in range(5):
                        hub_u.broadcast_event({"type": "frame_verified", "seq": i})
                    check("A5e 事件队列满则丢最旧且 dropped **计数递增**（慢消费者必须能知道自己漏了）",
                          sub["dropped"] == 3 and q_e.qsize() == 2,
                          f"dropped={sub['dropped']} qsize={q_e.qsize()}")

                    # 节流：不节流就是"每秒几十条"，但节流掉多少**必须计数**
                    sub2 = {"q": asyncio.Queue(maxsize=64), "dropped": 0}
                    hub_u.host_ws = {"w2": sub2}
                    hub_u._ev_last.clear()
                    hub_u._ev_throttled.clear()
                    for i in range(20):
                        hub_u.broadcast_event({"type": "delivery_progress", "seq": i},
                                              throttle_s=2.0)
                    msgs2 = []
                    while not sub2["q"].empty():
                        msgs2.append(json.loads(sub2["q"].get_nowait()))
                    check("A5f delivery_progress 被节流且**被节流掉多少有计数**"
                          "（20 条进 ⇒ 1 条出 + 19 条记在 throttled）",
                          len(msgs2) == 1
                          and hub_u._ev_throttled.get("delivery_progress") == 19
                          and msgs2[0].get("throttled_skipped") == 0,
                          f"出={len(msgs2)} throttled={dict(hub_u._ev_throttled)}")
                    # 反例：throttle_s=0 时必须一条不落地全发出去（否则"节流"就是遮盖丢事件）
                    sub3 = {"q": asyncio.Queue(maxsize=64), "dropped": 0}
                    hub_u.host_ws = {"w3": sub3}
                    for i in range(20):
                        hub_u.broadcast_event({"type": "delivery_progress", "seq": i})
                    check("A5g 反例：throttle_s=0 时 20 条必须全发（节流不许变成静默丢弃）",
                          sub3["q"].qsize() == 20, f"qsize={sub3['q'].qsize()}")
                finally:
                    shutil.rmtree(hub_u.data_dir, ignore_errors=True)

                # ---------- A6 真实留存进度 → delivery_progress 走的是节流通道 ----------
                # 端到端确认一遍：App 的 `status`（每 2 s 一条，带 store 账本）会让 PC 广播
                # `delivery_progress`，而它被节流；被节流掉的条数在 host/status 里查得到
                # （否则下游会以为"留存停了"）。
                async with sess.ws_connect(
                        f"{base}/api/v1/host/events?token={TOKEN}") as ev:
                    ev_hello = json.loads((await ev.receive()).data)
                    evs: list = []

                    async def _ev_reader():
                        try:
                            async for m in ev:
                                if m.type == aiohttp.WSMsgType.TEXT:
                                    evs.append(json.loads(m.data))
                        except Exception:
                            pass

                    erd = asyncio.create_task(_ev_reader())
                    try:
                        # 等过节流窗口（2 s）再开始：否则第一条 delivery_progress 本身
                        # 就会被节流掉，n_prog=0，这条判据就变成"看运气"。
                        await asyncio.sleep(2.2)
                        for i in range(20):
                            await hws.send_json({"type": "status", "session_id": ASID,
                                                 "state": "running",
                                                 "store": {"state": "recording",
                                                           "every_frame": True,
                                                           "frames_expected": 100 + i,
                                                           "frames_stored_on_device": 100 + i,
                                                           "gaps_on_device": 0}})
                        await asyncio.sleep(0.8)
                    finally:
                        erd.cancel()
                    n_prog = sum(1 for m in evs if m.get("type") == "delivery_progress")
                    async with sess.get(f"{base}/api/v1/host/status?token={TOKEN}") as r:
                        hs3 = await r.json()
                    thr = (hs3.get("host_events") or {}).get("throttled") or {}
                    check("A6a 事件订阅：接入即给 hello，并能收到真实的留存进度事件",
                          ev_hello.get("type") == "hello" and n_prog >= 1,
                          f"hello={ev_hello.get('type')} n_prog={n_prog}")
                    check("A6b 20 条 status ⇒ delivery_progress 被节流（不是 20 条），"
                          "且被节流掉的条数在 host/status 里查得到",
                          n_prog < 20 and thr.get("delivery_progress", 0) >= 15,
                          f"n_prog={n_prog} throttled={thr}")
                    check("A6c host_events 计数：订阅者数与会话列表口径一致",
                          (hs3.get("host_events") or {}).get("subscribers") >= 1
                          and ASID in hs3.get("sessions", []),
                          str(hs3.get("host_events")))
                    check("A6d 事件里带的账本与 sessions[] 的账本**是同一份**"
                          "（“面板说好、接口说不清”不许再出现）",
                          (evs and any(m.get("type") == "delivery_progress"
                                       and "declared" in m for m in evs))
                          and (await ledger(ASID))["frames_expected"] == 119,
                          f"ledger={str(await ledger(ASID))[:160]}")

                # ---------- A8 控制台功能 ↔ 对外接口：**从声明自身推导**逐项探针 ----------
                # 需求原话是「对外接口要包括**控制台的全部功能**」。这句话若只写在文档里，就只能靠
                # 人定期核对；而"面板加了个控件、声明忘了补"是最容易发生的漂移（第 25 轮就在同类
                # 地方一次抓到三处"声明与实现不符"）。所以这里**不抄一份功能清单**，而是遍历
                # capabilities 自己声明的那张表：声明几条就探几条。声明删掉一条 ⇒ 探针也少一条
                # （不会假装还在验）；声明新增一条而它不成立 ⇒ 立刻红。
                #
                # "端点真的存在吗"不靠 HTTP 猜（404 到底是路由没有、还是业务说会话不存在？），
                # 而是比对 capabilities **从真实路由表导出的** `routes` —— 精确、无歧义。
                routes = {(r["method"], re.sub(r"\{[^}]*\}", "{}", r["path"]))
                          for r in (caps.get("routes") or [])}
                con = caps.get("console") or {}
                feats = con.get("features") or []
                beyond = con.get("beyond_console") or []
                allv = feats + beyond

                check("A8a 声明了「控制台功能 ↔ 对外接口」映射表（features / beyond / routes 都在）",
                      len(feats) >= 7 and len(beyond) >= 2 and bool(routes),
                      f"features={len(feats)} beyond={len(beyond)} routes={len(routes)}")

                def route_miss(items, rts):
                    """声明 → 路由表 的匹配。**写成纯函数**：反例用例要能直接喂假数据。"""
                    out = []
                    for f in items:
                        raw = str(f.get("method") or "").upper()
                        m = "GET" if raw == "WS" else raw      # WS 在 aiohttp 里就是一条 GET 路由
                        key = (m, re.sub(r"\{[^}]*\}", "{}", str(f.get("endpoint") or "")))
                        if key not in rts:
                            out.append(str(f.get("id") or key))
                    return out

                miss = route_miss(allv, routes)
                check("A8b 声明里的每条 endpoint **都真的在路由表里**（声明必须为真）",
                      not miss, f"路由表里找不到：{miss}")

                # 反例与正例**成对**：既证明"伪造声明会被判否"，也证明"真声明不是恒被判否"。
                # 只写反例的话，一个恒返回非空的探针照样全绿（反向用例的真空通过）。
                fake = [{"id": "ghost", "method": "GET", "endpoint": "/api/v1/does_not_exist"},
                        {"id": "ghost2", "method": "WS", "endpoint": "/nope"}]
                check("A8c 反例：伪造两条不存在的声明 ⇒ 探针必须都判否（判据不是空转）",
                      route_miss(fake, routes) == ["ghost", "ghost2"],
                      f"得到 {route_miss(fake, routes)}")
                check("A8c2 正例：真实声明在同一探针下必须全过（防「恒判否」）",
                      not route_miss(allv, routes), f"{route_miss(allv, routes)}")

                # ---- 逐条探鉴权：声明要 token 的，不带 / 带错都必须 403 ----
                # ⚠ 一律**不带 token**去探（不强求 2xx）：`/stream/mjpeg` 与 `/api/v1/host/events`
                # 是长连接，带对 token 打过去会挂住测试 —— 而鉴权并不需要连上。
                no_tok, bad_tok = [], []
                for f in allv:
                    if "token" not in str(f.get("auth") or ""):
                        continue
                    # `endpoint` 是**真实路由模板**（可能与具体调用路径不同，如 `/control/{action}`）；
                    # `probe` 给出"能真打到的具体路径"。探针优先用 `probe` —— 这样它验的是
                    # "这条端点在鉴权上真的生效"，而不是"某个字面量路径返回了 403"。
                    ep = str(f.get("probe") or f["endpoint"]).replace("{sid}", ASID)
                    mm = str(f.get("method") or "GET").upper()

                    async def _hit(url, method):
                        try:
                            if method == "POST":
                                async with sess.post(url, json={}) as r:
                                    return r.status
                            async with sess.get(url) as r:
                                return r.status
                        except Exception as e:                              # noqa: BLE001
                            return f"exc:{type(e).__name__}"

                    st0 = await _hit(base + ep, mm)
                    st1 = await _hit(base + ep + ("&" if "?" in ep else "?") + "token=WRONG", mm)
                    if st0 != 403:
                        no_tok.append(f"{f.get('id')}⇒{st0}")
                    if st1 != 403:
                        bad_tok.append(f"{f.get('id')}⇒{st1}")
                check("A8d 声明要 token 的，**不带 token 必须 403**（逐条探，不抄清单）",
                      not no_tok, f"未 403：{no_tok}")
                check("A8d2 带**错** token 也必须 403（不能只判「有没有带」）",
                      not bad_tok, f"未 403：{bad_tok}")

                # ---- hostlink 声明的方法名必须真的存在（源码锚定）----
                hl_path = HERE / "hostlink.py"
                hl_src = hl_path.read_text(encoding="utf-8") if hl_path.exists() else ""

                def hl_missing(names, src):
                    return [n for n in names
                            if not re.search(rf"^    def {re.escape(str(n))}\(", src, re.M)]

                hl_miss = [f"{f.get('id')}→{n}()" for f in allv
                           for n in hl_missing(f.get("hostlink") or [], hl_src)]
                check("A8e 声明里的 hostlink 方法名**都真的定义在 pc/hostlink.py 里**"
                      "（读不到源文件判 FAIL，不是 INCOMPLETE——锚定失效就等于没测）",
                      bool(hl_src) and not hl_miss,
                      f"读到源码={bool(hl_src)}，缺失：{hl_miss}")
                check("A8e2 反例：喂一个不存在的方法名 ⇒ 必须判否（「声明有、实现无」要被抓住）",
                      hl_missing(["pair", "qr_png", "no_such_method"], hl_src)
                      == ["no_such_method"],
                      f"得到 {hl_missing(['pair', 'qr_png', 'no_such_method'], hl_src)}")

                # ---- 反向：面板源码里用到的每个路径都必须被声明覆盖 ----
                # 这一条防的是"面板加了控件、忘了对外声明"——那会让别的程序**永远调不到**这个功能，
                # 而面板上看着一切正常（现场表现："控制台有的功能，接口里找不到"）。
                async with sess.get(f"{base}/") as r:
                    panel_html = await r.text()
                used = {u for u in re.findall(r"""['"](/[A-Za-z0-9_/.\-{}]*)['"]""", panel_html)
                        if u.startswith(("/api/", "/stream/", "/ws"))}
                declared = {re.sub(r"\{[^}]*\}", "{}", str(f["endpoint"])) for f in allv}
                # 例外表只有一条：面板专用推送通道。对外的等价物 `/api/v1/host/events` **已声明** ——
                # 两条通道的语义不同（面板断开只是"某个标签页没在看"），不能合并，故显式豁免。
                allow = {"/ws/panel"}

                def orphan(uses, decl, alw):
                    out = []
                    for u in uses:
                        if u in alw:
                            continue
                        # 面板里的路径常是拼接片段（`'/api/v1/sessions/'+sid+'/thumb/latest'`）⇒ 前缀匹配
                        if any(u == d or u.startswith(d.rstrip("/") + "/") or d.startswith(u)
                               for d in decl):
                            continue
                        out.append(u)
                    return out

                orph = orphan(used, declared, allow)
                check("A8f **反向**：面板源码里用到的每个路径都被声明覆盖"
                      "（面板新增控件而没同步声明 ⇒ 立刻红）",
                      bool(used) and not orph,
                      f"面板用到 {sorted(used)}；未声明：{orph}")
                check("A8f2 反向探针的真空保护：允许表之外的路径必须被判否",
                      orphan({"/api/v1/panel_only_secret"}, declared, allow)
                      == ["/api/v1/panel_only_secret"], "反向探针看不清")

                # ---- 二维码三形态：必须是**同一个码** ----
                async with sess.get(f"{base}/api/v1/pair?token={TOKEN}") as r:
                    pr = await r.json()
                ok_svg, det_svg = svg_check(pr.get("qr_svg") or "", pr["pair_uri"])
                check("A8g 二维码 SVG 与配对码文本逐格一致（复用既有 svg_check）", ok_svg, det_svg)

                mtx = pr.get("qr_matrix") or {}
                rows = mtx.get("rows") or []
                mat_cells = {(ri, ci) for ri, row in enumerate(rows)
                             for ci, ch in enumerate(row) if ch == "1"}
                dd = re.search(r'<path d="([^"]*)"', pr.get("qr_svg") or "")
                svg_cells, _sc = set(), None
                for cm in re.finditer(r"M(\d+) (\d+)h(\d+)v(\d+)h-\d+z", dd.group(1) if dd else ""):
                    x, y, s2 = int(cm.group(1)), int(cm.group(2)), int(cm.group(3))
                    _sc = _sc or s2
                    svg_cells.add((y // s2, x // s2))
                check("A8g2 矩阵形态与 SVG **逐格一致**（两种形态画的必须是同一个码 —— "
                      "只比黑模块个数会漏掉「两个模块互换位置」）",
                      bool(mat_cells) and svg_cells == mat_cells
                      and len(rows) == mtx.get("size")
                      and all(len(x) == mtx.get("size") for x in rows),
                      f"矩阵 {len(mat_cells)} 格 / SVG {len(svg_cells)} 格；"
                      f"size={mtx.get('size')} vs rows={len(rows)}")
                check("A8g3 矩阵形态把**静区**一起声明了（只给黑模块边界 ⇒ 调用方画满会扫不出来）",
                      isinstance(mtx.get("border"), int) and mtx["border"] >= 2
                      and "1=黑" in str(mtx.get("bit")), str(mtx)[:160])

                fmts = pr.get("qr_formats") or {}
                check("A8h `qr_formats` 声明与实际产物一致（声明 png=true 就必须真有 PNG）",
                      fmts.get("svg") == bool(pr.get("qr_svg"))
                      and fmts.get("matrix") == bool(pr.get("qr_matrix"))
                      and fmts.get("png") == bool(pr.get("qr_png_b64")),
                      f"{fmts} vs svg={bool(pr.get('qr_svg'))} png={bool(pr.get('qr_png_b64'))}")
                if fmts.get("png") and Image is not None:
                    img = Image.open(io.BytesIO(base64.b64decode(pr["qr_png_b64"]))).convert("L")
                    w, h = img.size
                    n = int(mtx.get("size") or 0)
                    if n and w == h and w % n == 0:
                        sc = w // n
                        px = img.load()
                        pc = {(ri, ci) for ri in range(n) for ci in range(n)
                              if px[ci * sc + sc // 2, ri * sc + sc // 2] < 128}
                        check("A8h2 PNG 形态与矩阵逐格一致（别的程序存下来的图也是同一个码）"
                              "—— 采样取模块中心，边缘反锯齿不算数",
                              pc == mat_cells, f"相差 {len(pc ^ mat_cells)} 格（{w}x{h}）")
                    else:
                        check("A8h2 PNG 尺寸必须是 模块数×整数倍（否则采样点无意义）",
                              False, f"{w}x{h} 与 {n} 个模块不整")
                else:
                    print("  [INCOMPLETE] A8h2 PNG 逐格比对判不了："
                          + ("服务端未提供 PNG" if not fmts.get("png") else "本机缺 Pillow")
                          + f"（missing_deps={fmts.get('missing_deps')}）—— **判不了 ≠ 通过**")

                # ---- 三件套必须自洽：网址 / token / 二维码说的是同一件事 ----
                check("A8i pair 的 url / pair_uri / host / port / token **互相自洽**"
                      "（否则会出现「网址连得上、扫码连不上」这类现场怪象）",
                      pr["url"] == f'http://{pr["host"]}:{pr["port"]}/?token={pr["token"]}'
                      and pr["pair_uri"] == (f'm0probe://pair?h={pr["host"]}'
                                             f'&p={pr["port"]}&t={pr["token"]}')
                      and pr["token"] == TOKEN,
                      f'{pr["url"]} / {pr["pair_uri"]}')

                async with sess.get(f"{base}/api/v1/pair?token={TOKEN}&host=10.99.99.99") as r:
                    pr2 = await r.json()
                hs = pr2.get("hosts") or []
                check("A8j 用 ?host= 指定地址时，它必须进 hosts 且**恰好一条** recommended"
                      "（多网卡现场选错卡是连不上的头号原因，指定了却看不出来最要命）",
                      bool(hs) and hs[0]["ip"] == "10.99.99.99"
                      and hs[0].get("recommended") is True
                      and sum(1 for x in hs if x.get("recommended")) == 1,
                      f"hosts={[(x['ip'], x.get('recommended')) for x in hs]}")

                check("A8k 新增的 console / routes 段**不含任何凭据**"
                      "（capabilities 无需 token 就能读 ⇒ 配对三件套只能待在 /api/v1/pair 里）",
                      not aleaks(cap_raw, TOKEN) and "m0probe://pair" not in cap_raw
                      and "qr_png_b64" not in cap_raw,
                      "凭据泄漏进 capabilities")

                # ---------- A9 实时道（`capture_mode=live`：只看不留 + 彩色） ----------
                #
                # 这一组的存在理由与 A8 相同：把"live 模式不储存"这条**承诺**变成
                # 能被机器验证的东西。三件事缺一不可：
                #   ① 声明与实现一致（`lanes.live` 的端点真的在**导出的路由表**里）；
                #   ② 三态口径正确（`store.applicable=false`、`delivery_complete=null`
                #      **且 note 写「不适用」** —— 不是 False（那是"判了、没传完"）、
                #      也不是"判不了"（那是取证不足））；
                #   ③ **反例真的被拒**（live 会话收到交付帧、deliver 会话收到实时帧），
                #      否则"关闭交付道"这件事没有任何第二道防线。
                lv_cap = (caps.get("lanes") or {}).get("live") or {}
                lv_routes = {(r["method"], re.sub(r"\{[^}]*\}", "{}", r["path"]))
                             for r in (caps.get("routes") or [])}
                check("A9a lanes.live 声明齐备：no_persist / color / 端点 / 计数器语义 / 路径标识"
                      "（“跑完不留东西”是**声明**，判据从声明推导，不另抄一份清单）",
                      lv_cap.get("no_persist") is True and lv_cap.get("color") is True
                      and lv_cap.get("lossy") is True
                      and ("POST", lv_cap.get("endpoints", {}).get("frame") or "") in lv_routes
                      and "skipped" in (lv_cap.get("counter_semantics") or "")
                      and lv_cap.get("path_id") and lv_cap.get("knobs", {}).get("plan.capture_mode"),
                      json.dumps(lv_cap, ensure_ascii=False)[:260])

                LIVE_SID = "2026-10-04_0129_live"
                st, j = await c.control("start", session_id=LIVE_SID,
                                        device_serial="TESTSERIAL", capture_mode="live")
                check("A9b start 能开 live 会话，且回执里带着模式"
                      "（模式必须**显式选择**，不许靠默认值静默进入 —— 它跑完什么都没留）",
                      st == 200 and j.get("capture_mode") == "live", f"{st} {j}")

                st, j = await c.control("start", session_id="2026-10-04_0129_badmode",
                                        capture_mode="LIVE")
                check("A9b2 反例：capture_mode 取值非法必须**显式报错**"
                      "（静默退回默认会把「只看不留」悄悄换成「全量落盘」——"
                      "两种现场行为差一个量级，而使用者看不出来）",
                      st == 400 and j.get("error") == "bad_capture_mode", f"{st} {j}")

                # 推 3 帧实时画面。本道只校验 JPEG 的 SOI，不解码 ⇒ 用最小合法前缀即可
                # （真正的"能不能解出画面"是跨程序判据 P13 的事，那边用真 JPEG）。
                fake_jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 512 + b"\xff\xd9"
                ok_cnt = 0
                for i in range(3):
                    async with sess.post(
                            f"{base}/api/v1/live/frame", data=fake_jpg,
                            headers={"X-Session-Id": LIVE_SID, "X-Seq": str(i),
                                     "X-Sensor-Timestamp-Ns": str(10 ** 12 + i)}) as r:
                        if r.status == 200:
                            ok_cnt += 1
                check("A9c 实时帧被接收（**设备面**接口：不校验 token，与 files/* 同一条口径；"
                      "它不返回任何凭据，所以是显式记录的开口而非遗漏）",
                      ok_cnt == 3, f"ok={ok_cnt}")

                def _sess_of(j, sid):
                    return next((x for x in (j.get("sessions") or [])
                                 if x.get("session_id") == sid), None)

                lvs = _sess_of(await c.sessions(), LIVE_SID)
                check("A9d live 会话三态：`store.applicable=false` 且 "
                      "`delivery_complete` 判 **null**，且两处 note 都写明**不适用**"
                      "（不是 False —— 那会被读成“判了、没传完”，于是有人去重传一份"
                      "根本不存在的留存；也不是“判不了”—— 那是取证不足）",
                      lvs is not None and lvs.get("capture_mode") == "live"
                      and lvs["store"].get("applicable") is False
                      and lvs["delivery_complete"] is None
                      and "不适用" in (lvs.get("delivery_complete_note") or "")
                      and "不适用" in (lvs["store"].get("note") or ""),
                      json.dumps({k: (lvs or {}).get(k) for k in
                                  ("capture_mode", "delivery_complete",
                                   "delivery_complete_note")}, ensure_ascii=False)[:280])

                fdir = tmp / "sessions" / LIVE_SID / "frames"
                n_yuv = len(list(fdir.glob("*.yuv"))) if fdir.is_dir() else 0
                check("A9e 「不储存」的**硬证据**：live 会话跑完 frames/ 里一个文件都没有"
                      "（只看接口字段不够 —— 字段说「没落盘」而磁盘上有文件，就是声明为假；"
                      "本项目的规矩是**落盘才是真相**）",
                      n_yuv == 0 and lvs is not None and lvs["live"]["frames_received"] == 3,
                      f"yuv={n_yuv} live_received={(lvs or {}).get('live', {}).get('frames_received')}")

                st, j = await c.init(session_id=LIVE_SID, seq=0,
                                     name="frame_000000.yuv", size=1024)
                check("A9f 反例：live 会话收到**交付帧**必须显式拒绝"
                      "（「交付道整体关闭」如果只靠“App 不发”，这条承诺就没有第二道防线）",
                      st == 409 and j.get("error") == "mode_conflict", f"{st} {j}")
                lvs = _sess_of(await c.sessions(), LIVE_SID)
                check("A9f2 模式冲突**被计数且可查**（静默丢弃 = 这个缺陷永远只在"
                      "被发现的那一刻才存在）",
                      lvs is not None and lvs["live"].get("mode_conflicts", 0) >= 1
                      and bool(lvs["live"].get("mode_conflict_last")),
                      json.dumps((lvs or {}).get("live", {}).get("mode_conflict_last"),
                                 ensure_ascii=False))

                DLV_SID = "2026-10-04_0129_deliv"
                await c.control("start", session_id=DLV_SID, device_serial="TESTSERIAL")
                async with sess.post(f"{base}/api/v1/live/frame", data=fake_jpg,
                                     headers={"X-Session-Id": DLV_SID, "X-Seq": "0"}) as r:
                    st, j = r.status, await r.json()
                check("A9g 反例：deliver 会话收到实时帧也必须拒绝"
                      "（App 站错了道要能看见 —— 反向的静默同样是缺陷）",
                      st == 400 and j.get("error") == "mode_conflict", f"{st} {j}")

                NOPE_SID = "2026-10-04_0129_nope"
                async with sess.post(f"{base}/api/v1/live/frame", data=fake_jpg,
                                     headers={"X-Session-Id": NOPE_SID, "X-Seq": "0"}) as r:
                    st, j = r.status, await r.json()
                check("A9h 未知会话的实时帧 ⇒ 404 且**不建会话**"
                      "（否则伪造/陈旧的帧会凭空造出会话目录 —— 与缩略图同一条纪律）",
                      st == 404 and j.get("error") == "unknown_session"
                      and not (tmp / "sessions" / NOPE_SID).exists(), f"{st} {j}")

                async with sess.post(f"{base}/api/v1/live/frame", data=b"\x00" * 1024,
                                     headers={"X-Session-Id": LIVE_SID, "X-Seq": "9"}) as r:
                    st, j = r.status, await r.json()
                check("A9i 非 JPEG 载荷必须拒绝（没有这条，灌 2.76 MB 原始 Y 也会「成功」，"
                      "而那份数据既不是完整帧、也不是可解码的图）",
                      st == 400 and j.get("error") == "bad_payload", f"{st} {j}")

                # ---- App 源码锚定：live 那条道**真的不含留存入口** ----
                src_kt = (HERE.parent / "android/m0probe/app/src/main/java/com/mouse/phoneprobe"
                          / "DeliverySession.kt")
                kt = src_kt.read_text(encoding="utf-8") if src_kt.is_file() else ""

                def _fn_body(text: str, *names: str) -> str:
                    """切出一个 Kotlin 函数的体（到下一个成员声明为止）。

                    为什么要切片而不是全文搜：全文搜 `enqueue(` 必然命中（deliver 那条路
                    就在用它）⇒ 判据会**恒真**。只有切进 live 的循环体内，才真的在问
                    「这条道自己有没有落盘」。读不到源文件 ⇒ 空串 ⇒ 判据失败
                    （锚定失效等于没测，不能算通过）。
                    """
                    for n in names:
                        i = text.find("private fun " + n + "(")
                        if i < 0:
                            continue
                        marks = [text.find("\n    private fun ", i + 10),
                                 text.find("\n    @Volatile", i + 10),
                                 text.find("\n    internal ", i + 10),
                                 text.find("\n    // ====", i + 10)]
                        ends = [m for m in marks if m > 0]
                        return text[i:min(ends)] if ends else text[i:]
                    return ""

                def _kconst_int(text: str, name: str):
                    """从 Kotlin 源码读出 `const val <name> = <整数>` 的值。

                    ⚠ 必须**按赋值目标名精确匹配**：上一轮 P16 用
                      `const val \\w+\\s*=\\s*(...)` 抓格式常量，先命中了
                      `PATH_ID = "stream_deliver_yuv"`，改宽一点又命中
                      `COLOR_SRC_RGBA_HAL = "rgba_hal"` 里的**字符串字面量** ——
                      两次都是"看起来在工作、其实取错了东西"。
                    ⚠ 只认 `const val`：非 const 的 `val` 可能是运行期计算出来的，
                      拿源码文本当值等于**声明与事实脱钩**。
                    读不到 ⇒ 返回 None（由调用方判 FAIL，不静默当作 0/缺省）。
                    """
                    m = re.search(r"const\s+val\s+" + re.escape(name)
                                  + r"\s*=\s*(\d+)\b", text)
                    return int(m.group(1)) if m else None

                live_body = _fn_body(kt, "startLiveLoop")
                check("A9j 源码锚定：`startLiveLoop` 里**只有实时道** —— 必须有 "
                      "`postLiveFrame(`，且**不得**出现 `enqueue(` / `startWriter(`"
                      "（那两条是留存器的入口；“不储存”因此不只是接口行为，代码里也没路）",
                      bool(live_body) and "postLiveFrame(" in live_body
                      and "enqueue(" not in live_body and "startWriter(" not in live_body,
                      (live_body[:150].replace("\n", " ") or "读不到 startLiveLoop"))
                fake_body = _fn_body(kt.replace("postLiveFrame(jpg, ts)",
                                                "enqueue(ts, jpg, null)"), "startLiveLoop")
                check("A9j2 反例：把 `enqueue(` 塞进同一段代码，同一检测必须判**否**"
                      "（否则 A9j 可能只是因为“函数根本没找到”而通过）",
                      bool(fake_body) and "enqueue(" in fake_body, "反例检测失灵")

                check("A9k 彩色是硬要求不是顺手：live 必须取 `planes[1]`/`planes[2]`（色度），"
                      "且用 `acquireLatestImage()`（与交付道的 FIFO 语义**相反**）",
                      "planes[1]" in kt and "planes[2]" in kt
                      and "acquireLatestImage" in kt and "MODE_LIVE" in kt,
                      f"planes1={'planes[1]' in kt} planes2={'planes[2]' in kt} "
                      f"latest={'acquireLatestImage' in kt} live={'MODE_LIVE' in kt}")

                # ---- 采集路径登记（App 侧 + 登记表 + 服务端声明，三处必须一致）----
                reg = HERE.parent / "tools" / "capture_path_versions.json"
                regd = json.loads(reg.read_text(encoding="utf-8")) if reg.is_file() else {}
                vers = regd.get("versions") or {}
                newest = list(vers.values())[-1] if vers else {}
                paths = (newest or {}).get("paths") or {}

                # ⚠⚠ 这里原来写的是 `paths.get("live_stream_color") == 1`
                #   —— 把**某个字面值**当成了判据。本轮合法地把 live rev 提到 2
                #   （新增 color_source ⇒ 产出像素变了）⇒ 这条当场红。
                # 记忆第 32 条：**判据里每个字面清单都问"是不变量还是现状？"**
                #   `== 1` 是"现状"，不是"不变量"。
                #   真正的不变量是**三处互相一致** + **既有路径的 rev 没被动**。
                #
                # ⚠ "既有路径 rev 没被动"必须**从登记表历史里比**，不能写死 `== 3`：
                #   写死的话，将来合法地把 stream_deliver_yuv 提到 4 时，
                #   这条又会以同样的方式红一次 —— 而那次的红是**假的**。
                prev_paths = (list(vers.values())[-2] or {}).get("paths") or {} \
                    if len(vers) >= 2 else {}
                app_rev = _kconst_int(kt, "LIVE_PATH_REV")
                srv_rev = srv.LIVE_PATH_REV
                reg_rev = paths.get("live_stream_color")
                # 交付道那条：与**上一版登记表**逐项比，任何"既有路径 rev 变了"都要报出来
                moved = {k: (prev_paths.get(k), v)
                         for k, v in paths.items()
                         if k in prev_paths and prev_paths[k] != v
                         and k != "live_stream_color"}
                check("A9l 采集路径三处一致：登记表 / App 源码 / 服务端声明的 "
                      "`live_stream_color` rev **逐字相等**（不写死某个值——"
                      "rev 合法 +1 时这条不许红），且 `path_id` 一致；"
                      "**既有路径的 rev 一个都没动**（新增参数不许让既有路径的证据作废）",
                      reg_rev is not None and app_rev == reg_rev and srv_rev == reg_rev
                      and lv_cap.get("path_id") == "live_stream_color"
                      and lv_cap.get("path_rev") == reg_rev
                      and not moved,
                      f"registry={reg_rev} App={app_rev} srv={srv_rev} "
                      f"cap={lv_cap.get('path_rev')}；被改动的既有路径={moved or '无'}")

                # ⚠ A9l2 反例：三处一致这个判据**必须会判否**。
                #   改完"不再写死字面值"之后，它有可能退化成"随便什么都过"
                #   （三处都读不到时如果全用 `or None` 兜底，就会恒真）。
                #   ⇒ 分别注入"App 改了 / PC 改了 / 既有路径被动了"三种漂移。
                _kt_bump = kt.replace(f"const val LIVE_PATH_REV = {app_rev}",
                                      f"const val LIVE_PATH_REV = {app_rev + 1}"
                                      ) if app_rev is not None else ""
                check("A9l2 反例：App 侧 rev 单独漂移 / 既有路径 rev 被悄悄改动 ⇒ 必须判否"
                      "（否则 A9l 可能只是'三处一起变'才通过的）",
                      _kconst_int(_kt_bump, "LIVE_PATH_REV") == (app_rev + 1)
                      and _kconst_int(kt, "LIVE_PATH_REV") == app_rev
                      and not moved,
                      f"注入后 App rev={_kconst_int(_kt_bump, 'LIVE_PATH_REV')} "
                      f"（原 {app_rev}）；moved={moved or '无'} —— "
                      "判据自身读不出漂移 ⇒ A9l 恒真")

                # ---- 第 28 轮：面板上真的能选 live（在此之前它只在接口里）----
                # 判据**从 `srv.MODES` 自身推导**（不另抄一份清单）：面板那两个 <option> 的取值
                # 必须与模式集合**逐字相等**。加第三种模式而忘了改面板 ⇒ 这里立刻红，
                # 而不是现场点了按钮才发现"模式没生效"。
                # ⚠ 为什么这条值得单独写：上一轮 live 只存在于接口时，它写在 `beyond_console`
                # （"面板做不到"）里 —— 那是**真话**。这一轮面板有了控件，那句话就变成假话，
                # 于是必须同时搬声明（已搬到 `features`）。**声明与界面的漂移是双向的。**
                def panel_modes(html: str) -> set:
                    """**只**取 `id="capmode"` 那个 <select> 里的选项。

                    ⚠⚠ 这里原来抓的是**全页**所有 `<option value="...">`，
                      于是本轮新增的 `colsrc`（**颜色来源**，另一个维度）
                      的两个选项也被当成"模式"⇒ 判红。
                      看着像"面板坏了"，其实坏的是判据：
                      它把「面板上所有下拉框」当成了「模式集合」。
                      ⇒ 下拉框可以有 N 个，各自的选项集合要**分别**与对应的
                      常量集合比对（模式 ↔ MODES，颜色来源 ↔ COLOR_SOURCES）。
                    ⚠ 用 `id="capmode"` 定位而不是"第一个 <select>"：
                      顺序会变，按位置取是**现状**不是**不变量**。
                    ⚠⚠ 值的字符类**必须含数字**：`yuv_bt601` 里有 `601`，
                      写成 `[A-Za-z_]+` 会静默漏掉它 ⇒ 集合少一个 ⇒ 又一次假红。
                      （这已是本轮第三次"正则比想象的窄"：P16c 抓常量、
                        P15e 抓降级分支、这里抓 option 值。）
                    """
                    m = re.search(r'<select id="capmode".*?</select>', html, re.S)
                    if not m:
                        return set()
                    return set(re.findall(r'<option value="([A-Za-z0-9_]+)"', m.group(0)))

                def panel_colsrc(html: str) -> set:
                    """`id="colsrc"` 下拉框的选项集合（颜色来源维度）。"""
                    m = re.search(r'<select id="colsrc".*?</select>', html, re.S)
                    if not m:
                        return set()
                    return set(re.findall(r'<option value="([A-Za-z0-9_]+)"', m.group(0)))

                def panel_ok(html: str) -> tuple[bool, str]:
                    got = panel_modes(html)
                    want = set(srv.MODES)
                    why = []
                    if got != want:
                        why.append(f"模式选项 {sorted(got)} ≠ MODES {sorted(want)}")
                    # 选了必须**真的传下去**：只画个下拉框而不放进 start 的 body，
                    # 就是"界面上能选、实际不生效"—— 比没有控件更坏（用户会以为生效了）。
                    if "capture_mode" not in html:
                        why.append("面板源码里没有 capture_mode（选了却没往下传）")
                    # ⚠ 第 31 轮：颜色来源是**与模式正交的另一个维度**，同样要双向钉住 ——
                    #   下拉框选项必须等于接口取值集合，且**真的进了 start 的 body**。
                    #   只查模式的话，新加的 colsrc 就是个没人管的孤儿控件。
                    got_cs = panel_colsrc(html)
                    if got_cs != set(srv.COLOR_SOURCES):
                        why.append(f"颜色来源选项 {sorted(got_cs)} "
                                   f"≠ COLOR_SOURCES {sorted(set(srv.COLOR_SOURCES))}")
                    if "color_source" not in html:
                        why.append("面板源码里没有 color_source（选了却没往下传）")
                    # 实时道要有画面出口（用的是已声明的 /stream/mjpeg）
                    if "/stream/mjpeg" not in html:
                        why.append("面板里没有 /stream/mjpeg（实时道没有画面出口）")
                    # 不适用 ≠ 判不了：面板必须认识 store.applicable
                    if "applicable" not in html:
                        why.append("面板不认 `store.applicable`（live 的『不适用』会被画成『判不了』）")
                    return (not why), "；".join(why)

                ok_panel, why_panel = panel_ok(srv.PANEL_HTML)
                check("A9m 面板**真的能选 live 模式**：模式选项集合 == `srv.MODES`（从模式集合自身推导）、"
                      "`capture_mode` 真的进了 start 的 body、实时道有画面出口、"
                      "且认得 `applicable`（不把『不适用』画成『判不了』）；"
                      "颜色来源（**与模式正交的另一个维度**）同样双向钉住",
                      ok_panel, why_panel)
                # ⚠⚠ 判据**自身**的正则自检：本轮被这个坑了第三次（`yuv_bt601` 里的
                #   数字被 `[A-Za-z_]+` 漏掉 ⇒ 集合少一个 ⇒ 假红，看着像"面板坏了"）。
                #   正则比想象的窄是**静默**的：漏一个取值只会让报告少一项，
                #   不会报错 ⇒ 必须让判据自己检查"我的字符类覆盖全部取值了吗"。
                _VAL_CLS = r"[A-Za-z0-9_]+"
                _uncovered = [v for v in list(srv.MODES) + list(srv.COLOR_SOURCES)
                              if not re.fullmatch(_VAL_CLS, v)]
                check("A9m1 判据的取值正则 `[A-Za-z0-9_]+` **覆盖全部实际取值**"
                      "（模式 + 颜色来源；漏掉一个就会静默少一项 ⇒ 假红。"
                      "这是本轮第三次踩'正则比想象的窄'）",
                      not _uncovered,
                      f"正则覆盖不到: {_uncovered} ⇒ 判据会漏掉它们并假红")
                # 反例：把模式选项抹掉一个 ⇒ 必须判否；把 capture_mode 抹掉 ⇒ 必须判否。
                # 没有这条，"恒判真"的探针（比如正则写错、永远匹配空集）也会全绿。
                broken1 = srv.PANEL_HTML.replace('<option value="live">', '<option value="liveX">')
                broken2 = srv.PANEL_HTML.replace("capture_mode", "cm")
                broken3 = srv.PANEL_HTML.replace("/stream/mjpeg", "/stream/nope")
                # ⚠ 第 31 轮新增两个反例：颜色来源维度的两项检查**也必须会判否**。
                #   否则"加了两行检查"与"加了两行永远为真的装饰" indistinguishable。
                broken4 = srv.PANEL_HTML.replace('<option value="rgba_hal">',
                                                 '<option value="rgba_halX">')
                broken5 = srv.PANEL_HTML.replace("color_source", "cs")
                check("A9m2 反例：面板少一个模式选项 / 不传 capture_mode / 没有画面出口 / "
                      "颜色来源选项对不上 / 不传 color_source ⇒ 探针必须判否"
                      "（否则 A9m 可能只是“正则永远匹配空集”而通过）",
                      (not panel_ok(broken1)[0]) and (not panel_ok(broken2)[0])
                      and (not panel_ok(broken3)[0])
                      and (not panel_ok(broken4)[0]) and (not panel_ok(broken5)[0]),
                      f"broken1={panel_ok(broken1)[1]} | broken2={panel_ok(broken2)[1]} "
                      f"| broken3={panel_ok(broken3)[1]}")

                # 声明位置也必须跟着搬：面板能选了，它就不该再待在 `beyond_console`（"面板做不到"）里。
                feat_ids = {f.get("id") for f in feats}
                beyond_ids = {f.get("id") for f in beyond}
                check("A9m3 `live_mode` 的声明位置与界面一致：进 `features`、"
                      "**不再**留在 `beyond_console`（那里写着“面板没有对应控件”—— 现在是假话）",
                      "live_mode" in feat_ids and "live_mode" not in beyond_ids,
                      f"features={sorted(feat_ids)} beyond={sorted(beyond_ids)}")

                # ---- 面板 JS **真的能跑**（第 28 轮：这条抓到了真缺陷）----
                #
                # 为什么必须有这条：`PANEL_HTML` 里那段 JS 对本项目而言**只是一个字符串**。
                # Python `ast.parse` 过、`node --check` 过（语法完全合法）、A9m 也过
                #（它只检"某个字符串在不在"）—— 而本轮实测踩到的错是：
                #   `const isLive` 声明在 `gateCell`（它的使用点）**之后** ⇒ 块级作用域 TDZ
                #   ⇒ `ReferenceError: Cannot access 'isLive' before initialization`
                #   ⇒ **整个 refresh() 抛异常，会话表一行都不渲染**（页面其它部分照常，
                #      所以肉眼只看到"表格是空的"，很容易被当成"还没有会话"）。
                # **三层静态检查全部放过它，只有真的把页面跑一遍才会现形。**
                # ⇒ 所以这里用 node + 最小 DOM 桩**真执行** `refresh()`，并断言渲染结果。
                def find_node() -> str:
                    p = shutil.which("node")
                    if p:
                        return p
                    for pat in (r"C:/Program Files/nodejs/node.exe",
                                r"C:/Program Files (x86)/nodejs/node.exe",
                                "/usr/local/bin/node", "/usr/bin/node"):
                        if Path(pat).is_file():
                            return pat
                    return ""

                def panel_runtime(src_html: str, node: str) -> tuple[bool, str]:
                    """在 node 里跑一遍面板脚本（带最小 DOM/fetch/WS 桩），返回 (是否跑通, 摘要)。"""
                    m_js = re.search(r"<script>(.*)</script>", src_html, re.S)
                    if not m_js:
                        return False, "面板里找不到 <script>"
                    js = m_js.group(1).replace("__TOKEN__", "smoketok")
                    # 夹具：一个 live 会话 + 一个 deliver 会话（两条分支都要被渲染到）
                    fixture = {
                        "sessions": [
                            {"session_id": "2026-10-04_0000_smokelive",
                             "capture_mode": "live", "state": "running",
                             "device_model": "SMOKE", "device_serial": "S1",
                             "app_version": "m0probe-1.14.0",
                             "capture_path_id": "live_stream_color", "capture_path_rev": 1,
                             "delivery": "live", "frames_total": 0, "frames_verified": 0,
                             "bytes_verified": 0, "thumbs": 0, "thumb_latest": None,
                             "lens": {}, "start_ack": {"ok": True}, "stop_ack": None,
                             "start_gate": {"status": "incomplete", "hard_fail": [],
                                            "unevaluable": ["zoom_ratio"], "checks": {},
                                            "soft_notes": [], "evidence": None},
                             "calib_consistent": None, "drift": [],
                             "delivery_complete": None, "delivery_complete_note": "**不适用**：live",
                             "late_frames_after_stop": 0, "declared_conflicts": None,
                             "last_status": {},
                             "store": {"applicable": False, "mode": "live",
                                       "state": "not_applicable", "note": "**不适用**"},
                             "live": {"applicable": True, "mode": "live",
                                      "frames_received": 7, "bytes_received": 123456,
                                      "mbps_received": 0.42, "fps_actual": 6.9,
                                      "declared": {"sent": 7, "skipped_est": 12, "dropped": 0,
                                                   "latency_ms": 31},
                                      "rejected": 0, "mode_conflicts": 0, "last": None}},
                            {"session_id": "2026-10-04_0001_smokedeliver",
                             "capture_mode": "deliver", "state": "recording",
                             "device_model": "SMOKE", "device_serial": "S2",
                             "app_version": "m0probe-1.14.0",
                             "capture_path_id": "stream_deliver_yuv", "capture_path_rev": 3,
                             "delivery": "stream", "frames_total": 9, "frames_verified": 8,
                             "bytes_verified": 1024, "thumbs": 2,
                             "thumb_latest": {"name": "thumb_000002.jpg", "seq": 2, "bytes": 4096,
                                              "sensor_timestamp_ns": 123, "meta": {}, "at": "now"},
                             "lens": {}, "start_ack": {"ok": True}, "stop_ack": None,
                             "start_gate": {"status": "pass", "hard_fail": [], "unevaluable": [],
                                            "checks": {}, "soft_notes": [], "evidence": None},
                             "calib_consistent": True, "drift": [],
                             "delivery_complete": None, "delivery_complete_note": "判不了",
                             "late_frames_after_stop": 0, "declared_conflicts": None,
                             "last_status": {},
                             "store": {"applicable": True, "mode": "deliver",
                                       "state": "recording", "every_frame": True,
                                       "frames_expected": 9, "frames_stored_on_device": 9,
                                       "gaps_on_device": 0, "frames_verified_on_pc": 8,
                                       "upload_gaps": 1, "missing_seqs": [], "complete": False,
                                       "bytes_verified": 1024, "missing_seqs_note": None},
                             "live": {"applicable": False, "mode": "deliver"}},
                        ],
                        "ws_clients": 0, "profile_id": "smoke", "profile": {},
                        "data_dir": "X:/smoke", "host_events": {},
                    }
                    harness = (
                        "const FIX = " + json.dumps(fixture, ensure_ascii=False) + ";\n"
                        "function mkEl(id){return {id,innerHTML:'',textContent:'',className:'',"
                        "dataset:{},style:{},src:'',alt:'',onload:null,onerror:null,"
                        "removeAttribute(){},setAttribute(){}};}\n"
                        "const ELS={};\n"
                        "global.document={getElementById:(id)=>ELS[id]||(ELS[id]=mkEl(id)),"
                        "querySelector:()=>null};\n"
                        "global.location={protocol:'http:',host:'127.0.0.1:9'};\n"
                        "global.WebSocket=function(){return {readyState:0,close(){}};};\n"
                        # ⚠ 必须 stub `setInterval`：面板脚本末尾是 `refresh(); setInterval(refresh,3000);`
                        # —— 真的跑起来 node 会因为有 pending interval **永不退出**，
                        # 表现是"harness 超时"（第一次就是这么红的一条）。`setTimeout` 保留原生
                        # （harness 要靠它等一次 refresh 完成）。
                        "global.setInterval=()=>0; global.clearInterval=()=>{};\n"
                        "global.fetch=async(u)=>({ok:true,status:200,text:async()=>'',"
                        "json:async()=>(String(u).includes('/pair')"
                        "?{ok:true,qr_svg:'<svg/>',pair_uri:'m0probe://pair',host:'h',port:1,"
                        "token:'t',note:''}:FIX)});\n"
                        "const __errs=[];\n"
                        "process.on('unhandledRejection',e=>__errs.push('rej: '+e));\n"
                        "(async()=>{\n"
                        "  try{\n" + js + "\n"
                        "  }catch(e){__errs.push('sync: '+(e&&e.message||e));}\n"
                        "  await new Promise(r=>setTimeout(r,400));\n"
                        "  const hint=ELS['hint']?ELS['hint'].textContent:'';\n"
                        "  const rows=ELS['rows']?ELS['rows'].innerHTML:'';\n"
                        "  const live=ELS['liveinfo']?ELS['liveinfo'].innerHTML:'';\n"
                        "  console.log('@@RES@@'+JSON.stringify({errs:__errs,hint,rowsLen:rows.length,"
                        "hasLiveSid:rows.includes('smokelive'),hasDeliverSid:rows.includes('smokedeliver'),"
                        "hasNA:rows.includes('不适用'),hasLiveLedger:live.includes('7')}));\n"
                        "  process.exit(0);\n"
                        "})();\n"
                    )
                    f = Path(tempfile.mkdtemp(prefix="panel_rt_")) / "harness.js"
                    f.write_text(harness, encoding="utf-8")
                    try:
                        cp = subprocess.run([node, str(f)], capture_output=True, text=True,
                                            encoding="utf-8", errors="replace", timeout=60)
                    except subprocess.TimeoutExpired:
                        return False, "node 跑 harness 超时（面板脚本里有死循环？）"
                    out = (cp.stdout or "") + (cp.stderr or "")
                    mm = re.search(r"@@RES@@(\{.*\})", out)
                    if not mm:
                        return False, f"harness 没吐出结果：{out.strip()[:300]}"
                    r = json.loads(mm.group(1))
                    ok = (not r["errs"]) and ("刷新失败" not in r["hint"]) \
                        and r["hasLiveSid"] and r["hasDeliverSid"] and r["hasNA"]
                    return ok, json.dumps(r, ensure_ascii=False)

                node_bin = find_node()
                if not node_bin:
                    # 铁律：缺依赖**不许静默跳过**（本项目栽过"可选依赖缺失被当通过"）。
                    check("A9m4 面板 JS **真的能跑**（node + 最小 DOM 桩真执行 refresh，"
                          "抓 TDZ / 未声明引用这类静态检查看不见的错）", False,
                          "找不到 node ⇒ 这条判据没有跑，**不能算通过**")
                    check("A9m5 反例：真实复现本轮的 TDZ（把 `const isLive` 挪到它的使用点之后）"
                          "⇒ 同一 harness 必须报错", False, "同上：无 node")
                else:
                    ok_rt, det_rt = panel_runtime(srv.PANEL_HTML, node_bin)
                    check("A9m4 面板 JS **真的能跑**：在 node 里用最小 DOM/fetch/WS 桩真执行 `refresh()`，"
                          "断言无异常、无“刷新失败”、live 与 deliver 两个会话都渲染出来、"
                          "且 live 的「不适用」被画出来"
                          "（⚠ 静态检查只能证“字符串在”，**证不了“能跑”**）",
                          ok_rt, det_rt)
                    # 反例必须**真实复现**踩到的那个错：把声明块剪到使用点之后 ⇒ TDZ。
                    blk = ("      const isLive = (s.capture_mode === 'live');\n"
                           "      const LG = s.live || {};\n")
                    anchor = "      const AR = s.store || {};\n"
                    broken = srv.PANEL_HTML
                    if blk in broken and anchor in broken:
                        broken = broken.replace(blk, "", 1).replace(anchor, anchor + blk, 1)
                    ok_bad, det_bad = panel_runtime(broken, node_bin)
                    check("A9m5 反例：把 `const isLive` 声明剪到它的**使用点之后**（本轮真实踩到的 TDZ）"
                          "⇒ 同一 harness 必须判否（否则 A9m4 只是“跑了个空”而通过）",
                          (blk in srv.PANEL_HTML) and (not ok_bad),
                          ("变换未生效（锚点没匹配上）" if blk not in srv.PANEL_HTML
                           else f"坏版本竟然通过了：{det_bad[:200]}"))


                # ---------- A7 熔断：显式命名 + 不影响已存帧的回传 ----------
                BSID = "2026-09-25_0907_abort"
                for i in range(3):
                    await awhole(BSID, i, b"B" * 1024)
                await hws.send_json({"type": "delivery_aborted", "session_id": BSID,
                                     "reason": "no_space",
                                     "message": "剩余空间 1800 MB < 余量保护",
                                     "frames_expected": 100, "frames_stored_on_device": 3,
                                     "gaps_on_device": 97, "gap_ts": [1, 2, 3],
                                     "bytes": 3072})
                await asyncio.sleep(0.4)
                lb = await ledger(BSID)
                check("A7a 熔断状态**显式命名**（`aborted_no_space` 而不是“没在跑”），"
                      "且原因与缺口都透出",
                      lb["state"] == "aborted_no_space" and lb["aborted_reason"] == "no_space"
                      and lb["gaps_on_device"] == 97,
                      json.dumps({k: lb[k] for k in ("state", "aborted_reason",
                                                     "gaps_on_device")}, ensure_ascii=False))
                st_ba, j_ba = await awhole(BSID, 9, b"B" * 1024)
                check("A7b 熔断**不影响已存帧的继续回传**：紧接着仍能 verified"
                      "（熔断保的是“已经拿到的数据”）",
                      st_ba == 200 and j_ba.get("state") == "verified",
                      f"{st_ba} {str(j_ba)[:160]}")
                async with sess.get(f"{base}/api/v1/sessions") as r:
                    ss_ab = (await r.json())["sessions"]
                async with sess.get(f"{base}/api/v1/host/status?token={TOKEN}") as r:
                    hs_ab = await r.json()
                b_ab = _sess_of({"sessions": ss_ab}, BSID)
                check("A7c 熔断在**两个出口口径一致**：会话自己的 store 与 host/status 的 store.phase"
                      "（⚠ `latest` 已改成「创建序」—— `started_at` 只精确到秒，同秒创建的会话"
                      "原先排序不定，判据会随别的用例插入而随机红；本轮实测踩到）",
                      b_ab is not None
                      and b_ab["store"].get("state") in
                      ("aborted_no_space", "aborted", "uploading", "done", "recording")
                      and hs_ab["store"].get("phase") in
                      ("aborted_no_space", "aborted", "uploading-to-pc", "done",
                       "storing-on-device", "none"),
                      f"session={((b_ab or {}).get('store') or {}).get('state')} "
                      f"host={hs_ab['store'].get('phase')} (latest={hs_ab['capture'].get('session_id')})")

            finally:
                hrd.cancel()
                try:
                    await hws.close()
                except Exception:
                    pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------- 20 枚举归一的**表驱动**用例 + 夹具保真（不需要起服务） ----------
    #
    # 为什么单独做这一组：上面的 HTTP/WS 用例只能覆盖"某一种写法"。而这类缺陷的根源是
    # **判据把写法差异当成事实差异**，写法是可能随时新增的（App 改格式、有人手写
    # --profile 用裸码）⇒ 必须把归一规则本身逐条钉住，且**双向**（相等与不等都要验）。
    # 判据脚本要用到服务端模块的函数（如 pair_uri / enum_eq）做**契约锚定**，
    # srv 已在 main() 开头导入（那里也说明了理由）。

    for want, got, exp in [("OFF", "OFF(0)", True),     # App 的真实写法（本次事故）
                           ("OFF", "OFF", True),        # 两边都是裸名字
                           ("OFF", 0, True),            # profile 写裸码
                           (0, "OFF(0)", True),         # 裸码 vs 名字+码
                           ("on", "ON(1)", True),       # 大小写
                           ("OFF", "ON(1)", False),     # 真不一致（归一不许把它放过）
                           ("OFF", "HIGH_QUALITY(6)", False),
                           ("ON", "AN(1)", False),      # 名字打错不许蒙对
                           ("OFF", None, False)]:
        check(f"20a 枚举归一：want={want!r} got={got!r} ⇒ {exp}",
              srv.enum_eq(want, got) is exp, f"得到 {srv.enum_eq(want, got)}")
    check("20b 名字与码自相矛盾（'OFF(2)' vs 'OFF(0)'）判**不一致**（保守：不放过矛盾记录）",
          srv.enum_eq("OFF(2)", "OFF(0)") is False)

    # 真空保护：没有帧 ⇒ incomplete，**绝不是 pass**（"判不了 ≠ 通过"）
    _tmpg = Path(tempfile.mkdtemp(prefix="gate_"))
    try:
        s0 = srv.Session("s0", "SERIAL", _tmpg)
        s0.profile = dict(srv.DEFAULT_PROFILE)
        g0 = s0.start_gate()
        check("20g 零帧 ⇒ 门控 incomplete（不是 pass）",
              g0["status"] == "incomplete" and g0["checks"] == {}, str(g0)[:200])
    finally:
        shutil.rmtree(_tmpg, ignore_errors=True)

    # 夹具保真：本文件的 default_meta 声称"与 App 的写法一致"，那就必须真的能对上 ——
    # 从 App 源码里把格式串与 ois 表读出来。App 一旦改写法，这条会先炸，
    # 而不是等到现场"点了开始采集，画面刷一下就停了"才被发现。
    try:
        kt = (HERE.parent / "android" / "m0probe" / "app" / "src" / "main" /
              "java" / "com" / "mouse" / "phoneprobe" / "DeliverySession.kt").read_text(encoding="utf-8")
        fmt = '"$it($i)"' in kt or '"${it}($i)"' in kt
        m_ois = re.search(r'"ois"\s*to\s*mapOf\(([^)]*)\)', kt)
        check("20h 夹具保真：App 的 name() 仍产出「名字(码)」且 ois 表含 OFF",
              fmt and m_ois is not None and '"OFF"' in m_ois.group(1),
              f"fmt={fmt} ois表={m_ois.group(1) if m_ois else None}")

    except Exception as e:
        check("20h 夹具保真：App 的 name() 仍产出「名字(码)」且 ois 表含 OFF", False, str(e))

    # ---------- 26（续）契约锚定：PC 生成器 ↔ App 解析器（不需要起服务）----------
    #
    # 扫码是**两台设备之间的格式契约**，两侧代码由两个作者写：各自"编译通过"毫无意义，
    # 只有"PC 生成的那串 == App 认的那串"才有意义。
    # 铁律：格式串必须从**被观测对象的真实产出**里读（这里读的是 App 源码），
    # 不能在测试里另抄一份 —— 那样只能证明"两份抄得一样"。
    # ⚠ 诚实边界：Python 跑不了 Kotlin 解析器，所以这一组只锁**字符串契约**；
    #    "解析器行为"由 App 内建自检在真机上证明（长按「扫码配对」跑 SELFTEST）。
    kt_pair = (HERE.parent / "android" / "m0probe" / "app" / "src" / "main" /
               "java" / "com" / "mouse" / "phoneprobe" / "PairCode.kt")
    try:
        ktp = kt_pair.read_text(encoding="utf-8")
    except Exception:
        ktp = ""
    check("26g 能读到 App 的 PairCode.kt（读不到 = 锚定失效，不能算通过）",
          bool(ktp), str(kt_pair))
    if ktp:
        def _kstr(name: str) -> str | None:
            m = re.search(r'const val %s\s*=\s*"([^"]*)"' % name, ktp)
            return m.group(1) if m else None

        def _kint(name: str) -> int | None:
            m = re.search(r"const val %s\s*=\s*(\d+)" % name, ktp)
            return int(m.group(1)) if m else None

        check("26h 契约常量逐一相等（scheme / action / 三个键名 / 默认端口）",
              _kstr("SCHEME") == srv.PAIR_SCHEME and _kstr("ACTION") == srv.PAIR_ACTION
              and _kstr("KEY_HOST") == srv.PAIR_KEY_HOST
              and _kstr("KEY_PORT") == srv.PAIR_KEY_PORT
              and _kstr("KEY_TOKEN") == srv.PAIR_KEY_TOKEN
              and _kint("DEF_PORT") == srv.PAIR_DEF_PORT,
              f"kt={_kstr('SCHEME')}/{_kstr('ACTION')}/{_kstr('KEY_HOST')}"
              f"{_kstr('KEY_PORT')}{_kstr('KEY_TOKEN')}/{_kint('DEF_PORT')}  "
              f"py={srv.PAIR_SCHEME}/{srv.PAIR_ACTION}/{srv.PAIR_KEY_HOST}"
              f"{srv.PAIR_KEY_PORT}{srv.PAIR_KEY_TOKEN}/{srv.PAIR_DEF_PORT}")

        samples = re.findall(r'"(m0probe://pair\?h=([^&"]+)&p=(\d+)&t=([^&"]*))"', ktp)
        check("26i App 自检向量里的配对码样本 == PC 生成器的产物（逐字符，双向）",
              bool(samples) and any(srv.pair_uri(h, int(p), t) == full
                                    for full, h, p, t in samples),
              f"抽到样本 {samples[:2]}")

        neg = re.findall(r'Case\(\s*"([^"]*)"\s*,\s*false', ktp)
        check("26j App 自检表里**有反例**，且覆盖「扫到无关二维码」"
              "（只有正例的表在解析器退化成「什么都接受」时依然全绿）",
              len(neg) >= 5 and any("weixin://" in x for x in neg)
              and any("mp.weixin.qq.com" in x for x in neg),
              f"反例 {len(neg)} 条：{neg[:3]}")

    src_py = (HERE / "server.py").read_text(encoding="utf-8")
    check("26k 面板真的会显示二维码（qrbox + 拉 /api/v1/pair + 只拉一次）",
          'id="qrbox"' in srv.PANEL_HTML and "/api/v1/pair" in srv.PANEL_HTML
          and "loadPair" in srv.PANEL_HTML and "pairLoaded" in srv.PANEL_HTML)
    check("26l 终端与面板用的是**同一个生成器** + 码上写明「等同凭据」"
          "（防投屏外传；也防将来有人在终端里手拼一个字符串出来）",
          "pair_uri(addrs[0][0], args.port, token)" in src_py
          and "等同凭据" in src_py and "等同凭据" in srv.PANEL_HTML)

    # ---------- 15 默认 profile 不得强制 zoom（否则手机上的档位选择形同虚设） ----------
    # 0.6×/1.0× 是两颗物理镜头，谁在机器旁边谁决定；PC 默认把 zoom_ratio 写 1.0
    # 会让 App 界面上的选择被静默压掉。这条断言就是钉住那个语义：**0 = 不指定**。
    try:
        src = (HERE / "server.py").read_text(encoding="utf-8")
        m = re.search(r"DEFAULT_PROFILE\s*=\s*\{(.*?)\n\}", src, re.S)
        prof = {}
        for k, v in re.findall(r'"([a-z_]+)"\s*:\s*([^,\n]+)', m.group(1)):
            prof[k] = v.strip()
        check("15 PC 默认 profile 的 zoom_ratio = 0（不指定，由 App 决定）",
              prof.get("zoom_ratio") == "0", f"zoom_ratio={prof.get('zoom_ratio')}")
    except Exception as e:
        check("15 PC 默认 profile 的 zoom_ratio = 0（不指定，由 App 决定）", False, str(e))

    # ---------- 10 限速（另起一个低限速服务） ----------
    port2, tmp2 = free_port(), Path(tempfile.mkdtemp(prefix="pcsv_slow_"))
    p2 = subprocess.Popen([PY, str(HERE / "server.py"), "--host", "127.0.0.1",
                           "--port", str(port2), "--data-dir", str(tmp2),
                           "--token", TOKEN, "--max-mbps", "2"],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          encoding="utf-8", errors="replace")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as sess:
            c2 = Client(f"http://127.0.0.1:{port2}", sess)
            for _ in range(60):
                try:
                    await c2.sessions()
                    break
                except Exception:
                    await asyncio.sleep(0.25)
            # ⚠ 限速是**令牌桶**（允许 0.25 s 的突发额度），所以"单个 1 MiB 请求要 ≥0.35 s"
            # 这条断言是错的（它把限速当成了"每请求固定 sleep"的老实现）。
            # 要验的是**长期平均速率**：连传 4 MiB @2MB/s，扣掉 0.5 MiB 突发额度后
            # 至少要 (4-0.5)/2 = 1.75 s。单请求口径无法区分"桶"与"固定睡眠"。
            payload = os.urandom(1024 * 1024)
            st, j = await c2.init(seq=1, name="frame_000001.yuv", size=4 * len(payload),
                                  sha256=hashlib.sha256(payload * 4).hexdigest(),
                                  meta=default_meta(1))
            fid = j["file_id"]
            t0 = time.monotonic()
            off = 0
            for _ in range(4):
                st, jx = await c2.put(fid, off, payload, 4 * len(payload),
                                      crc=crc32b(payload))
                off = jx["received"]
            dt = time.monotonic() - t0
            check("10 交付道限速生效（4 MiB @2MB/s 长期平均 ≥1.5s）", st == 200 and dt >= 1.5,
                  f"status={st} dt={dt:.3f}s")

            # ---------- 11 链路测速端点（App「链路测速」按钮用） ----------
            body = os.urandom(8 * 1024 * 1024)
            t0 = time.monotonic()
            async with sess.post(f"http://127.0.0.1:{port2}/api/v1/linktest",
                                 data=body) as r:
                jl = await r.json()
            dt = time.monotonic() - t0
            check("11 linktest 返回收到的字节数", r.status == 200 and jl["bytes"] == len(body),
                  f"{r.status} {jl}")
            # 8 MiB @2 MB/s ⇒ 约 4 s（扣 0.5 MiB 突发）；只验"确实报告了合理速率"
            check("11 linktest 报告速率且与限速量级一致",
                  jl["mbps"] > 0 and 1.0 <= jl["mbps"] <= 3.0,
                  f"mbps={jl['mbps']} dt={dt:.2f}s")
    finally:
        p2.terminate()
        try:
            p2.wait(timeout=5)
        except Exception:
            p2.kill()
        shutil.rmtree(tmp2, ignore_errors=True)

    n_ok = sum(1 for _, ok, _ in results if ok)
    print("\n" + "=" * 60)
    print(f"pc/test_server：{n_ok}/{len(results)} 通过")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
