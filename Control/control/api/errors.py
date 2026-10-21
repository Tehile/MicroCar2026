"""上游异常 → HTTP 状态码。

规矩：**错误信息必须能照着做**。所以这里不吞消息，只加状态码；而且上游的原始
异常类型会出现在 `error.type` 里 —— 现场排查时"是哪一层抛的"比"抛了什么"更重要。
"""

from __future__ import annotations

import asyncio
import json
import traceback
from typing import Any, Dict

from aiohttp import web

from ..adapters.mouse import MouseError
from ..config import ConfigError
from ..ctx import SessionError
from ..video_io import VideoBusy, VideoError, VideoNotFound


class ApiError(Exception):
    """本层自己的错误（带明确状态码）。"""

    def __init__(self, status: int, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def status_of(e: BaseException) -> int:
    if isinstance(e, ApiError):
        return e.status
    if isinstance(e, KeyError):
        return 404
    if isinstance(e, FileNotFoundError):
        return 404
    if isinstance(e, (ValueError, ConfigError, TypeError)):
        return 400            # 请求本身写错了
    if isinstance(e, SessionError):
        # 会话编排失败**从来不是**"请求写错了"，而是"当前状态不具备执行条件"：
        # 要么已经有一个会话开着，要么鼠那一路此刻开不起来。
        # 归到 400 会让前端把"鼠标没连"显示成"你参数传错了"，是误导。
        return 409
    if isinstance(e, asyncio.TimeoutError):
        return 504
    # 视频会话（改版二十四）：三种要分开 ——
    #   产物不存在=404（"这个会话没有"）、已在跑=409（"等它结束"）、
    #   其余（路径错/依赖缺/产物畸形）=409：都不是"请求语法错了"，
    #   而是"当前状态不具备执行条件"，归 400 会显示成"你参数写错了"，是误导。
    if isinstance(e, VideoNotFound):
        return 404
    if isinstance(e, VideoBusy):
        return 409
    if isinstance(e, VideoError):
        return 409
    if isinstance(e, MouseError):
        msg = str(e)
        if "不可用" in msg:
            return 503            # 上游模块不可用 ⇒ 依赖没装好，不是请求错了
        return 409            # 设备当前不具备执行条件（没连、在忙）
    return 500


@web.middleware
async def error_middleware(request: web.Request, handler) -> web.StreamResponse:
    try:
        return await handler(request)
    except web.HTTPException as e:
        raise
    except Exception as e:                                    # noqa: BLE001
        status = status_of(e)
        body: Dict[str, Any] = {
            "ok": False,
            "error": {
                "type": type(e).__name__,
                "message": str(e) or "（异常没有消息 —— 这本身就是个问题）",
                "status": status,
            },
        }
        if isinstance(e, ApiError):
            body["error"].update(e.extra)
        if status >= 500:
            body["error"]["traceback"] = traceback.format_exc()[-2000:]
        return web.json_response(body, status=status, dumps=_dumps)


def _dumps(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False, default=str)


def ok(data: Any = None, **extra: Any) -> web.Response:
    body: Dict[str, Any] = {"ok": True}
    if data is not None:
        body["data"] = data
    body.update(extra)
    return web.json_response(body, dumps=_dumps)


async def body_json(request: web.Request, required: bool = False) -> dict:
    try:
        raw = await request.text()
    except Exception:                                        # noqa: BLE001
        raw = ""
    if not raw.strip():
        if required:
            raise ApiError(400, "这个端点需要 JSON 请求体（POST 一个 {} 也行）")
        return {}
    try:
        d = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ApiError(400, f"请求体不是合法 JSON：{e}") from e
    if not isinstance(d, dict):
        raise ApiError(400, "请求体必须是 JSON 对象")
    return d
