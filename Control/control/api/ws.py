"""WS 上游通道：JSON + 二进制两条（设计方案 §6.2）。

为什么必须分成两条：**payload > 64 KB 一律走二进制**（判据 I10）。
250 Hz × 9 通道的波形用 JSON 推，光字符串化就能把浏览器卡死；二进制帧让前端
`new Float32Array(buf)` 零解析开销 —— 这不是优化，是它能不能跑起来的前提。
"""

from __future__ import annotations

import asyncio
import json
import struct
from typing import Any, Dict, Optional

from aiohttp import WSMsgType, web

from ..bus import JSON_PAYLOAD_LIMIT, Bus, encode_bin
from .errors import ApiError

_HEADER_LEN = struct.Struct("<I")


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ctx = request.app["ctx"]
    bus: Bus = ctx.bus
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=8 * 1024 * 1024)
    await ws.prepare(request)

    qj = bus.subscribe()
    qb = bus.subscribe_bin()
    subs = {"health", "mouse.state", "mouse.text", "mouse.event", "mouse.upevent",
            "mouse.error", "mouse.advice", "mouse.telemetry", "mouse.maze",
            "camera.event", "camera.frame", "camera.status", "camera.delivery_done",
            "locate.result", "locate.batch", "session.phase", "log", "arena.changed",
            "clock.changed", "mouse.param", "mouse.session"}
    only: Optional[set] = None

    async def pump_json() -> None:
        while True:
            msg = await qj.get()
            typ = msg.get("type")
            if only is not None and typ not in only:
                continue
            if not _fits(msg):
                # 判据 I10 的另一半：JSON 帧的载荷有上限。真超了就是上游 bug，
                # **必须显式报出来**，而不是悄悄发出去把前端卡住。
                await ws.send_json({"type": "protocol.error", "t_pc_ms": msg.get("t_pc_ms"),
                                    "about": typ,
                                    "message": f"{typ} 的载荷超过 {JSON_PAYLOAD_LIMIT} 字节"
                                               f"却走了 JSON 通道 —— 应当改走二进制帧"})
                continue
            await ws.send_str(json.dumps(msg, ensure_ascii=False, default=str))

    async def pump_bin() -> None:
        while True:
            meta, payload = await qb.get()
            await ws.send_bytes(encode_bin(meta, payload))

    async def recv() -> None:
        nonlocal only
        async for m in ws:
            if m.type == WSMsgType.TEXT:
                try:
                    d = json.loads(m.data)
                except json.JSONDecodeError:
                    continue
                typ = d.get("type")
                if typ == "ping":
                    await ws.send_str(json.dumps({"type": "pong", "t_pc_ms": d.get("t_pc_ms")},
                                                 ensure_ascii=False))
                elif typ == "sub":
                    item = d.get("types")
                    if item is None:
                        only = None
                    elif isinstance(item, list):
                        only = set(str(x) for x in item)
                    elif isinstance(item, str):
                        only = {item}
                    await ws.send_str(json.dumps({"type": "sub.ack", "only": sorted(only)
                                                  if only else None}, ensure_ascii=False))
                elif typ == "mouse.raw":
                    # 逃生舱：原始命令。**同步执行**（要等设备回），所以放到线程里，
                    # 绝不在事件循环里阻塞。
                    line = str(d.get("line") or d.get("cmd") or "").strip()
                    if not line:
                        await ws.send_str(json.dumps(
                            {"type": "mouse.raw.result", "ok": False,
                             "message": "命令是空的"}, ensure_ascii=False))
                        continue
                    res = await asyncio.get_running_loop().run_in_executor(
                        None, ctx.mouse_send_raw, line)
                    await ws.send_str(json.dumps({"type": "mouse.raw.result",
                                                  **res}, ensure_ascii=False, default=str))
            elif m.type == WSMsgType.ERROR:
                break

    tasks = [asyncio.create_task(t) for t in (pump_json(), pump_bin(), recv())]
    # 握手：先把现状推一次，前端不必再轮询
    try:
        await ws.send_str(json.dumps({"type": "hello", "health": ctx.health()},
                                     ensure_ascii=False, default=str))
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in list(done) + list(pending):
            try:
                await t
            except (asyncio.CancelledError, Exception):      # noqa: BLE001
                pass
    finally:
        bus.unsubscribe(qj)
        bus.unsubscribe(qb)
    return ws


def _fits(msg: Dict[str, Any]) -> bool:
    try:
        return len(json.dumps(msg, ensure_ascii=False, default=str)) <= JSON_PAYLOAD_LIMIT
    except (TypeError, ValueError):
        return True
