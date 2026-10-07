#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""交付链路吞吐探针（回环 / 指定地址皆可）。

**为什么需要它**：`--max-mbps` 与协议形状（每帧 init → N 个分片 PUT → complete，
单连接串行）都是"可能成为天花板"的候选。现场实测只有 5.5 MB/s（44 Mbps）时，
必须回答：这是**链路**（WiFi）、**手机**（相机+重排+SHA-256 抢 CPU）、
还是**协议/服务端**的问题？—— 三者处置完全不同。

本脚本用与 App **逐字段相同**的请求序列在**回环**上打，得到"协议+服务端"的
天花板。若回环上是几百 MB/s，就说明 5.5 MB/s 与协议无关，瓶颈在链路或手机端。

用法：
    python pc/probe_throughput.py                 # 自动起服务（127.0.0.1，不限速）
    python pc/probe_throughput.py --max-mbps 40   # 带上现场默认的限速
    python pc/probe_throughput.py --frames 60 --size 2764800 --chunk 1048576 --parallel 1
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
PY = sys.executable
TOKEN = "probe"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Client:
    def __init__(self, base: str, chunk: int):
        self.base = base
        self.chunk = chunk
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600))
        return self

    async def __aexit__(self, *a):
        await self.session.close()

    async def one(self, seq: int, payload: bytes, sid: str, serial: str) -> int:
        """一帧：init → 分片 → complete。返回服务端确认的字节数。"""
        sha = hashlib.sha256(payload).hexdigest()
        meta = {"seq": seq, "calib_id": "calib_probe", "capture_path_id": "stream_deliver_yuv",
                "capture_path_rev": 1, "focus_distance_diopter": 1.001001, "zoom_ratio": 1.0,
                "y_stride": 1920}
        body = {"session_id": sid, "device_serial": serial, "seq": seq,
                "name": f"frame_{seq:06d}.yuv", "size": len(payload), "sha256": sha, "meta": meta}
        async with self.session.post(self.base + "/api/v1/files/init", json=body) as r:
            j = await r.json()
            if r.status != 200:
                raise RuntimeError(f"init {r.status} {j}")
        fid, off = j["file_id"], j["resume_offset"]
        while off < len(payload):
            part = payload[off:off + self.chunk]
            h = {"Content-Range": f"bytes {off}-{off + len(part) - 1}/{len(payload)}",
                 "X-Chunk-Crc32": f"{zlib.crc32(part) & 0xffffffff:08x}"}
            async with self.session.put(f"{self.base}/api/v1/files/{fid}", data=part, headers=h) as r:
                jj = await r.json()
                if r.status != 200:
                    raise RuntimeError(f"put {r.status} {jj}")
                off = jj["received"]
        async with self.session.post(f"{self.base}/api/v1/files/{fid}/complete",
                                     json={"sha256": sha}) as r:
            jc = await r.json()
            if r.status != 200:
                raise RuntimeError(f"complete {r.status} {jc}")
        return len(payload)


async def run(base: str, frames: int, size: int, chunk: int, parallel: int,
              sid: str) -> tuple[float, float]:
    payload = os.urandom(size)
    t0 = time.perf_counter()
    if parallel <= 1:
        async with Client(base, chunk) as c:
            for i in range(frames):
                await c.one(i + 1, payload, sid, "PROBE-1")
    else:
        # 只探测**不同帧并行**（协议要求同一帧的分片必须顺序到达）
        async def worker(c: Client, idx: int):
            for i in range(idx, frames, parallel):
                await c.one(i + 1, payload, sid, "PROBE-1")
        async with Client(base, chunk) as c:
            await asyncio.gather(*[worker(c, k) for k in range(parallel)])
    dt = time.perf_counter() - t0
    total = frames * size
    return total / dt / 1024 / 1024, frames / dt


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=None, help="已有服务地址（默认自动起一个回环服务）")
    ap.add_argument("--frames", type=int, default=40)
    ap.add_argument("--size", type=int, default=2764800, help="默认 1920x1440 的 Y 平面")
    ap.add_argument("--chunk", type=int, default=1048576)
    ap.add_argument("--parallel", type=int, default=1, help="并行上传的帧数")
    ap.add_argument("--max-mbps", type=float, default=0.0, help="服务端限速（0=不限，默认）")
    ap.add_argument("--lan", action="store_true",
                    help="用本机局域网地址而非回环（测真实 WiFi 上限；服务仍在本机）")
    args = ap.parse_args()

    import server as srv                       # 同目录

    tmp = Path(tempfile.mkdtemp(prefix="probe_tp_"))
    proc = None
    port = free_port()
    base = args.base
    if base is None:
        host = "0.0.0.0" if args.lan else "127.0.0.1"
        env = dict(os.environ)
        proc = subprocess.Popen([PY, "-u", str(HERE / "server.py"), "--host", host,
                                 "--port", str(port), "--data-dir", str(tmp),
                                 "--token", TOKEN, "--max-mbps", str(args.max_mbps),
                                 "--profile-id", "probe"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, env=env)
        addrs = srv.lan_addresses(None)
        ip = addrs[0][0] if (args.lan and addrs) else "127.0.0.1"
        base = f"http://{ip}:{port}"

    # 等就绪
    async with aiohttp.ClientSession() as s:
        for _ in range(60):
            try:
                await s.get(base + "/api/v1/sessions")
                break
            except Exception:
                await asyncio.sleep(0.2)

    sid = "probe_" + time.strftime("%H%M%S")
    print("=" * 72)
    print(f"交付链路吞吐探针")
    print("=" * 72)
    print(f"  目标      : {base}")
    print(f"  帧数      : {args.frames}  帧大小: {args.size/1024/1024:.2f} MB "
          f"(= {args.size/2764800:.2f} × 1920x1440 的 Y 平面)")
    print(f"  分片      : {args.chunk/1024/1024:.2f} MB   并行帧数: {args.parallel}")
    print(f"  服务端限速: {'不限' if args.max_mbps <= 0 else f'{args.max_mbps} MB/s'}")
    print("-" * 72)
    try:
        mbps, fps = await run(base, args.frames, args.size, args.chunk, args.parallel, sid)
        print(f"  实测      : {mbps:7.2f} MB/s  ({mbps*8*1.048576:6.1f} Mbps)   "
              f"{fps:6.2f} 帧/s（该帧大小下）")
        if args.size == 2764800:
            print(f"  折算      : 1920x1440 交付 {fps:.2f} fps")
        print("-" * 72)
        print("  解读：这是**协议+服务端+本机网络栈**的天花板。")
        print("        · 若它 ≫ 现场实测（约 5.5 MB/s）⇒ 瓶颈在 WiFi 链路或手机端，")
        print("          与协议形状无关，改分片/并行都不会有效；")
        print("        · 若它 ≈ 现场实测 ⇒ 协议或服务端是限制，值得优化。")
    finally:
        if proc:
            proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
