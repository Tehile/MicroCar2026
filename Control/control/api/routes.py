"""REST 路由表（设计方案 §6.2 的逐条落地）。

原则：
  · 全部绑 `127.0.0.1`（本机自用）。要开 `0.0.0.0` 必须先补鉴权 —— 不在这里偷偷开。
  · 上游凭据（如 token）**只在服务端**：前端永远只看本层的端点，拿不到真凭据。
    （改版二十四：唯一的凭据持有者 PhoneCamera 随相机下线，这条纪律本身不变。）
  · 大数组走 WS 二进制（这里只负责"触发"与"给元信息"）。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
from aiohttp import web

from .. import __version__
from ..adapters.mouse import PULL_N_MAX, PULL_N_MIN
from ..clock import anchor_snapshot, mono_to_ms, now_ms
from ..maze_rec import GOAL_CELLS
from ..store import new_session_id
from ..video_io import list_sessions, list_videos
from .errors import ApiError, body_json, ok
from .ws import ws_handler

#: 服务端**写响应**时"对端已经走了"的那几个异常。aiohttp 3.9+ 有专门的
#: `ClientConnectionResetError`（继承自 ClientError），旧版没有 ⇒ 取不到就不列，
#: 免得 `except` 里出现一个不存在的名字。
_CCRE = getattr(aiohttp, "ClientConnectionResetError", None)


def register(app: web.Application, ctx: Any) -> None:
    r = app.router
    r.add_get("/api/v1/health", _health)
    r.add_get("/api/v1/caps", _caps)
    r.add_get("/api/v1/config", _config)
    r.add_get("/api/v1/paths", _paths)
    r.add_get("/api/v1/upstream", _upstream)
    r.add_get("/api/v1/logs/tail", _logs_tail)
    r.add_get("/api/v1/clock", _clock_get)
    r.add_post("/api/v1/clock/offset", _clock_offset)
    r.add_post("/api/v1/clock/mark", _clock_mark)
    r.add_post("/api/v1/clock/fit", _clock_fit)

    # ---- 鼠
    r.add_get("/api/v1/mouse/ports", _mouse_ports)
    r.add_post("/api/v1/mouse/connect", _mouse_connect)
    r.add_post("/api/v1/mouse/disconnect", _mouse_disconnect)
    r.add_get("/api/v1/mouse/state", _mouse_state)
    r.add_get("/api/v1/mouse/params", _mouse_params_get)
    r.add_post("/api/v1/mouse/params", _mouse_params_set)
    r.add_post("/api/v1/mouse/params/save", _mouse_params_save)
    r.add_post("/api/v1/mouse/params/reset", _mouse_params_reset)
    r.add_post("/api/v1/mouse/params/backup", _mouse_params_backup)
    r.add_post("/api/v1/mouse/stream", _mouse_stream)
    # 上游 2.9.0：**拉取模式切换**（批式 pace / 停等 stopwait / 全关 off）。
    # 与 /stream 分开是因为两者语义不同：/stream 是"流开不开"（人工刹车），
    # /stream/mode 是"怎么拉"（速率形态）。合并会让"关流"和"停等"变成一件事。
    r.add_post("/api/v1/mouse/stream/mode", _mouse_stream_mode)
    # 每批帧数 n（改版二十六）。与 `/stream/mode` 分开的**唯一**理由：
    # `/stream/mode` 是"开/关/哪种"三态，`n` 是"一次往返取几帧"的**连续量**；
    # 合成一条会让"只想调 n"的人不得不重发一遍 mode，等于一次请求改两件事。
    r.add_post("/api/v1/mouse/stream/batch", _mouse_stream_batch)
    # 维护与自检：v2.7 按键菜单 2/4/5/6/7/8 的远程化（一条端点对上游一个 CAL 命令族）
    r.add_post("/api/v1/mouse/maint/{item}", _mouse_maint)
    r.add_get("/api/v1/mouse/maze", _mouse_maze)
    # 迷宫（实验 8 无记忆走迷宫，上游 2.0.0）。**状态从统一帧读**，所以只剩
    # "读状态 + 读迁移序列 + 汇总 + 清本层差分缓冲"四条读的路，**没有事件流端点**。
    r.add_get("/api/v1/mouse/maze/state", _maze_state)
    r.add_get("/api/v1/mouse/maze/history", _maze_history)
    r.add_get("/api/v1/mouse/maze/summary", _maze_summary)
    r.add_post("/api/v1/mouse/maze/reset", _maze_reset)
    # 上游 2.1.1 新能力：**收车**（`MAZE STOP`）。v2.0 时代"发过一次车就只能断电"，
    # 现在有一个明确的收车口。2026-10-16 上游补了三个中止出口 + `LINK_Tick` 2 s
    # 兜底软复位 ⇒ 它现在**正常 0.2~1 s 就退出**，不必再"改为复位"。
    # ⚠️ 【复位】仍保留：语义不同（整机重来 vs 正常退出），且连不上时只有它能用。
    r.add_post("/api/v1/mouse/maze/stop", _maze_stop)
    # 上游 2.2.0 新能力：**复位**（整机软复位 `MCU RESET`）。固件主循环有 30 余处
    # 非协作式阻塞等待，`STOP` 会让它们永远等不到 ⇒ "越停越死"，软复位是唯一出口。
    r.add_post("/api/v1/mouse/rescue", _mouse_rescue)
    # 发车前**低电预检**（纯读帧、不发命令）：上游 2.2.1 有硬闸，这里只是让它**提前可见**。
    r.add_get("/api/v1/mouse/launch_precheck", _mouse_launch_precheck)
    # 实验 8 的「从发车到停止」记录与复盘（详见 control/maze_rec.py 的模块 docstring）
    r.add_get("/api/v1/mouse/maze/reconstruct", _maze_reconstruct)
    r.add_post("/api/v1/mouse/maze/reconstruct/clear", _maze_reconstruct_clear)
    r.add_get("/api/v1/mouse/maze/record", _maze_record_status)
    # 实时决策流 + 实时快照：右栏时间线与"边跑边回放"各自的数据源。
    # 两者都**零设备访问**（只读记录器内存），所以可以跟着界面刷新走。
    r.add_get("/api/v1/mouse/maze/decisions", _maze_decisions)
    r.add_post("/api/v1/mouse/maze/decisions/clear", _maze_decisions_clear)
    r.add_get("/api/v1/mouse/maze/record/live", _maze_record_live)
    r.add_post("/api/v1/mouse/maze/record/start", _maze_record_start)
    r.add_post("/api/v1/mouse/maze/record/stop", _maze_record_stop)
    r.add_get("/api/v1/mouse/maze/records", _maze_records)
    r.add_get("/api/v1/mouse/maze/records/{name}", _maze_record_get)
    r.add_post("/api/v1/mouse/maze/records/import", _maze_record_import)
    # 统一帧的**静态码表**（通道清单 + 迷宫码表）：零设备访问，未连接也答得出。
    r.add_get("/api/v1/mouse/meas/meta", _mouse_meas_meta)
    r.add_post("/api/v1/mouse/move", _mouse_move)
    r.add_post("/api/v1/mouse/tare", _mouse_tare)
    r.add_post("/api/v1/mouse/stop", _mouse_stop)
    r.add_post("/api/v1/mouse/estop", _mouse_estop)
    r.add_post("/api/v1/mouse/raw", _mouse_raw)
    # ---- 时间同步 / 发车（上游 1.4.0）
    r.add_get("/api/v1/timesync", _timesync)
    r.add_post("/api/v1/timesync/arm", _timesync_arm)
    r.add_post("/api/v1/timesync/maze_run", _timesync_maze_run)
    r.add_get("/api/v1/mouse/link_stats", _mouse_link_stats)
    # ---- 参数：两套（生产标定 page99 / 课程调参 page100）
    r.add_get("/api/v1/mouse/params/grouped", _mouse_params_grouped)
    r.add_post("/api/v1/mouse/params/grouped", _mouse_params_grouped_set)
    r.add_post("/api/v1/mouse/params/save_group", _mouse_params_save_group)
    # ---- 电机实验（上游 1.2.0）
    r.add_get("/api/v1/lab/status", _lab_status)
    r.add_post("/api/v1/lab/run", _lab_run)
    r.add_post("/api/v1/lab/log", _lab_log)
    r.add_post("/api/v1/lab/estop", _lab_estop)
    r.add_post("/api/v1/lab/resume", _lab_resume)
    # ---- 课程调参实验（上游 1.3.0 引入，**2.4.0 整层删除**）
    #
    # ⚠️ **这七条路由保留，但语义变了**：上游把课程层删了（编译档位 5 → 2，
    #    `MOUSE_LESSON_LAB` 连同 `lesson_lab.c` 751 行一并移除），所以它们**只能**
    #    回一条解释得清楚的错。为什么不干脆删掉路由：
    #      · 旧前端/旧脚本/判据调它们时，**404 说不出为什么** —— 现场会以为
    #        "本层忘了实现"，而不是"上游删了"；
    #      · 保留一条到 `calc` 的口（`/lesson/suggest`）—— **算参能力保留**，
    #        它算的是生产参数（TRACK*/GYRORATIO），与课程层无关。
    #    所以：`/lesson/suggest` **仍然可用**（走 calc 纯函数），其余六条明说已删除。
    r.add_get("/api/v1/lesson/list", _lesson_list)
    r.add_get("/api/v1/lesson/status", _lesson_status)
    r.add_post("/api/v1/lesson/run", _lesson_run)
    r.add_post("/api/v1/lesson/stop", _lesson_stop)
    r.add_post("/api/v1/lesson/log", _lesson_log)
    r.add_post("/api/v1/lesson/suggest", _lesson_suggest)
    r.add_post("/api/v1/lesson/apply", _lesson_apply)

    # ---- 视频复盘（改版二十四，Auto Locate 会话解析）
    # 位置数据的**唯一来路**。7 条全部是"起/停/查状态/读产物"：
    #   · `list`/`sessions`/`session`/`trajectory` 要扫盘读文件 ⇒ executor；
    #   · `status` **零 IO**（纯内存）—— 它被界面每秒轮询，摸盘就会拖住事件循环。
    # 没有 `align`：拍板③"前端不处理对轴"，时间轴由识别侧保证，本层只校验（§11.26 §五）。
    r.add_get("/api/v1/video/list", _video_list)
    r.add_post("/api/v1/video/run", _video_run)
    r.add_get("/api/v1/video/status", _video_status)
    r.add_post("/api/v1/video/cancel", _video_cancel)
    r.add_get("/api/v1/video/sessions", _video_sessions)
    r.add_get("/api/v1/video/session/{sid}", _video_session)
    r.add_get("/api/v1/video/trajectory/{sid}", _video_trajectory)

    # ---- 会话 / 数据
    r.add_post("/api/v1/session/open", _session_open)
    r.add_post("/api/v1/session/close", _session_close)
    r.add_get("/api/v1/session", _session_get)
    r.add_get("/api/v1/sessions", _sessions)
    r.add_get("/api/v1/session/timeline", _session_timeline)
    r.add_post("/api/v1/session/note", _session_note)
    r.add_get("/api/v1/session/export", _session_export)
    r.add_get("/api/v1/session/file", _session_file)

    # ---- 比赛记录摘要 + 视频关联（改版二十七，界面「比赛记录」页的数据源）
    # 为什么单开 `/records` 而不复用 `/mouse/maze/records`：
    # 那条只回 `{name,size,mtime_ms}`（**故意**轻，谁都能调），而界面那张表要 9 列。
    # 在旧端点上加列会让"只想知道有几条"的调用也跟着读整份 JSON。
    r.add_get("/api/v1/records", _records)
    r.add_get("/api/v1/records/{name}/replay", _records_replay)
    r.add_delete("/api/v1/records/{name}", _records_delete)
    r.add_post("/api/v1/records/{name}/video", _records_link)

    r.add_get("/ws", ws_handler)


# ================================================================ 通用

async def _health(request: web.Request) -> web.Response:
    # 判据 I2：三路全挂也必须 200，且每路都有**可读的** detail
    return ok(request.app["ctx"].health())


async def _caps(request: web.Request) -> web.Response:
    return ok(request.app["ctx"].caps())


async def _config(request: web.Request) -> web.Response:
    # 判据 R10：凭据**绝不进前端**。
    # ⚠️ 改版二十四：唯一的凭据持有者（PhoneCamera 的 `camera.token`）随相机下线，
    # 现在整份配置里没有任何密文字段 ⇒ 这里不再需要遮蔽。纪律本身不变：
    # 将来若再加带凭据的段，必须在这里遮蔽成"有没有配"，而不是"配的是什么"。
    return ok(request.app["ctx"].cfg.raw())


async def _paths(request: web.Request) -> web.Response:
    return ok(request.app["ctx"].paths)


async def _upstream(request: web.Request) -> web.Response:
    return ok(request.app["ctx"].upstream())


async def _logs_tail(request: web.Request) -> web.Response:
    n = _int(request, "n", 200)
    return ok(request.app["ctx"].bus.tail(n))


def _int(request: web.Request, key: str, default: int = 0) -> int:
    try:
        return int(request.query.get(key, default))
    except (TypeError, ValueError):
        raise ApiError(400, f"查询参数 {key} 必须是整数") from None


def _float(request: web.Request, key: str, default: Optional[float] = None) -> Optional[float]:
    v = request.query.get(key)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except ValueError:
        raise ApiError(400, f"查询参数 {key} 必须是数") from None


# ================================================================ 时钟

async def _clock_get(request: web.Request) -> web.Response:
    return ok(request.app["ctx"].clock.snapshot())


async def _clock_offset(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    d = await body_json(request, required=True)
    snap = ctx.save_clock(offset_ms=_f(d, "offset_ms"), uncertainty_ms=_f(d, "uncertainty_ms"),
                          source=str(d.get("source") or "manual"))
    return ok(snap)


async def _clock_mark(request: web.Request) -> web.Response:
    """记一个"人工同步脉冲"标记：用户在**同时被两路观测到**的动作发生的瞬间按一下。

    ⚠️ 这个标记的精度上限就是人的反应时间（~150 ms），所以它只够粗对齐。
    """
    ctx = request.app["ctx"]
    d = await body_json(request)
    t = now_ms()
    ctx._marks = getattr(ctx, "_marks", [])
    ctx._marks.append({"t_pc_ms": t, "label": str(d.get("label") or ""),
                       "device_ms": _f(d, "device_ms")})
    if d.get("device_ms") is not None:
        ctx.clock.add_sample(t, float(d["device_ms"]))
    return ok({"marks": ctx._marks[-20:], "n_samples": len(ctx.clock._samples),
               "note": "样本 ≥3 才能拟合"})


async def _clock_fit(request: web.Request) -> web.Response:
    snap = request.app["ctx"].clock.fit_sync_pulse()
    request.app["ctx"].save_clock()
    return ok(snap)


def _f(d: dict, k: str) -> Optional[float]:
    v = d.get(k)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ApiError(400, f"字段 {k} 必须是数，收到 {v!r}") from None


# ================================================================ 鼠

async def _mouse_ports(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    return ok(await asyncio.get_running_loop().run_in_executor(None, ctx.mouse.ports))


async def _mouse_connect(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    d = await body_json(request)
    target = d.get("target") or request.app["ctx"].cfg.get("mouse.target") or None
    baud = d.get("baud")
    res = await asyncio.get_running_loop().run_in_executor(
        None, lambda: ctx.mouse.connect(target, int(baud) if baud else None))
    ctx.store.mark("mouse", "link.connect", str(target), json.dumps(
        {"device": res.get("device")}, ensure_ascii=False))
    return ok(res)


async def _mouse_disconnect(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    return ok(await asyncio.get_running_loop().run_in_executor(None, ctx.mouse.disconnect))


async def _mouse_state(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    mc = ctx.mouse.mc
    out: Dict[str, Any] = {"health": ctx.mouse.health(),
                           # 最近一次采样链的 rate 快照（含逐批明细 `batches`）。
                           # **纯读**，不在端点里重算 —— 见 `MouseAdapter.rate_last()`。
                           "rate": ctx.mouse.rate_last()}
    if mc is not None and mc.is_connected:
        loop = asyncio.get_running_loop()
        out["link"] = await loop.run_in_executor(None, lambda: mc.link.self_check())
        out["summary"] = mc.telemetry.summary_text()
        out["session"] = {"is_open": mc.session.is_open, "path": mc.session.path,
                          "counts": mc.session.counts()}
    return ok(out)


async def _mouse_params_get(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    mc = _need_mouse(ctx)
    loop = asyncio.get_running_loop()
    vals = await loop.run_in_executor(None, mc.params.get_all)
    meta: Dict[str, Any] = {}
    for n in list(vals)[:400]:
        m = mc.params.meta(n)
        if m:
            meta[n] = {"type": m[0], "index": m[1], "note": m[2]}
    return ok({"values": vals, "meta": meta,
               "raw": {n: mc.params.get_raw(n) for n in list(vals)[:400]},
               "note": "Q16.16 原值 · speed* ≡ track*/100"})


async def _mouse_params_set(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    mc = _need_mouse(ctx)
    d = await body_json(request, required=True)
    values = d.get("values") or {}
    if not values:
        raise ApiError(400, "values 是空的 —— 没有要写的东西")
    readback = bool(d.get("readback", True))
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(
        None, lambda: mc.params.set_many(values, readback=readback))
    out = [_res(r) for r in (res if isinstance(res, (list, tuple)) else [res])]
    if d.get("derive_speed"):
        dr = await loop.run_in_executor(None, mc.params.derive_speed_from_track)
        out += [_res(r) for r in dr]
    ctx.store.mark("mouse", "mouse.param.write", ",".join(values.keys()),
                   f"ok={all(o['ok'] for o in out)}")
    return ok({"results": out,
               "note": "status=OK 且 readback_ok 才算写入"})


async def _mouse_params_save(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    mc = _need_mouse(ctx)
    loop = asyncio.get_running_loop()
    backup = await loop.run_in_executor(None, mc.params.backup)
    r = await loop.run_in_executor(None, mc.params.save)
    ctx.store.mark("mouse", "mouse.param.save", "", f"backup={backup} ok={r.ok}")
    return ok({"result": _res(r), "backup": backup,
               "note": "已自动备份"})


async def _mouse_params_reset(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    mc = _need_mouse(ctx)
    r = await asyncio.get_running_loop().run_in_executor(None, mc.params.reset)
    ctx.store.mark("mouse", "mouse.param.reset", "", f"ok={r.ok}")
    return ok({"result": _res(r), "note": "已擦除参数页，重启后生效"})


async def _mouse_params_backup(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    mc = _need_mouse(ctx)
    p = await asyncio.get_running_loop().run_in_executor(None, mc.params.backup)
    return ok({"backup": p})


async def _mouse_stream(request: web.Request) -> web.Response:
    """遥测流开关（**唯一的人工刹车**）。

    上游 2.0.0 删除了 `RATE`；2.1.0 又把"固件自时钟推流"改成"**上位机逐条拉取**"
    （`TELEM`）。所以这个端点现在的语义是"**启停本机拉取器**"，不是"命令设备开流"——
    但**这个端点不接收速率**（可调速率走 `/mouse/stream/mode`，上游 v2.9 才有），
    带 `hz` 会明确报错，免得现场以为"在这里降速了"其实什么都没发生。

    ⚠️ 关掉它不只是"停止显示"：v2.1 的 `off()` **立刻丢弃在途请求**，
    这是让蓝牙透传模块缓冲排空的唯一手段（2.0.0 的自时钟曾把那个缓冲灌死、
    且**不会自愈**，必须断电）。所以这个按钮在真机上是**排障手段**，不是省流量开关。
    """
    ctx = request.app["ctx"]
    mc = _need_mouse(ctx)
    d = await body_json(request)
    loop = asyncio.get_running_loop()
    if "hz" in d:
        raise ApiError(400, "这个端点不调速率。v2.9 起可调速率在 "
                            "/api/v1/mouse/stream/mode（{\"mode\":\"pace\"} 为批式，"
                            "{\"mode\":\"stopwait\"} 为停等）；"
                            "帧率由链路往返决定，不能任意指定。")
    # `auto=True` = "页面切走顺带停"。这时**正在跑车就扣留**（见 `pull_hold_reason`）；
    # 人工点【结束拉取】不带 auto，照停 —— 那是排障手段（`off()` 会丢弃在途请求，
    # 是排空蓝牙缓冲的唯一办法），不能替用户判断"能不能停"。
    auto = bool(d.get("auto"))
    want = bool(d["on"]) if "on" in d else (not mc.stream.is_on)
    # ⚠️ **记录中不许自动停**：切页把拉取关掉 = 这一跑的遥测被砍，
    #    而现场事后只会觉得"记录怎么是空的"。记录状态只有 ctx 知道，
    #    跑车状态由适配器看任务机 —— 两处各管一件，都在后端，界面不用判断。
    if auto and not want and ctx.store.current is not None:
        r = {"ok": True, "on": True, "kept": True, "reason": "recording",
             "n": None, "applied_n": None}
    else:
        r = await loop.run_in_executor(None, lambda: ctx.mouse.set_stream(want, auto=auto))
    ctx.store.mark("mouse", "mouse.stream", "", json.dumps(
        {"on": want, "auto": auto, **{k: r.get(k) for k in ("on", "kept", "reason")}},
        ensure_ascii=False))
    # `hz` 回的是**实测**帧率（上游 2.1 起 `telemetry.hz()` 已按到达间隔实算）；
    # 同时把拉取器自己的状态一并端出去（在途数 / 模式 / 丢了几条）——
    # 真机排障时"在途恒 1"是判断停等有没有被破坏的直接证据。
    return ok({"result": {"ok": r.get("ok"), "status": r.get("status")},
               "on": bool(mc.stream.is_on),
               # `kept=True` = **要停但没停**（车在跑）。必须回出去，否则界面
               # 会显示"已结束"而设备还在发帧 —— 又是"界面说一套、设备做一套"。
               "kept": bool(r.get("kept")), "reason": r.get("reason"),
               # `applied_n` = 这次开流**真的**带下去的档（None = 按停等起的）
               "n": r.get("n"), "applied_n": r.get("applied_n"),
               "hz": mc.telemetry.hz(),
               "pull": _pull_stats(mc)})


async def _mouse_stream_mode(request: web.Request) -> web.Response:
    """**拉取模式切换**（上游 v2.9.0 的批式拉取 `mc.stream.pace()`）。

    三种模式，一个端点一条真相来路：
      · `pace`     批式（`RATE PULL n t`：稀疏基频命令 + 每批 n 帧，默认 n=3/t=100ms = 10 Hz）
      · `stopwait` 停等（一条一答，帧率 = 1/RTT，真机 4.4~6.5 Hz）
      · `off`      全关（立刻停发，排空蓝牙缓冲）

    ⚠️ **必须走 `run_in_executor`**：`pace()` 内部 `cmd.send(wait=True, timeout=2.0)`
    最多试 3 次，最坏 6 秒级；`on()` 也会等在途请求收尾（≤0.6 s）。
    放在事件循环里会冻住整个服务（端点零同步设备的反面教材）。

    ⚠️ `mode` 回的是**门面三态**（off/stopwait/pace）而不是 `stats()["mode"]`——
    后者是拉取器自己的字段，停等式恒返回 `"stop-and-wait"`，**门面全关时也返回它**，
    照它写会做出"没开流却显示批式信息"的 bug。
    """
    ctx = request.app["ctx"]
    mc = _need_mouse(ctx)
    d = await body_json(request)
    mode = str(d.get("mode", "")).strip().lower()
    if mode not in ("pace", "stopwait", "off"):
        raise ApiError(400, "mode 只接受 pace / stopwait / off。")
    # 能力位：老上游没有 pace 门面三件套 ⇒ 明确报错，不让现场以为"切成功了"。
    if mode == "pace" and not ctx.mouse.features.get("pace_pull"):
        raise ApiError(501, "当前上游不支持批式拉取（没有 mc.stream.pace）。"
                            "换停等模式，或升级上游。")
    n = d.get("n")
    t_ms = d.get("t_ms")
    if n is not None:
        n = int(n)
        # ⚠️ 区间与 `POST /mouse/stream/batch` **同源**（`adapters/mouse.py` 的
        # `PULL_N_MIN/PULL_N_MAX` = 上游 `configure()` 的 1..16）。这里原来写 1~8，
        # 结果"同一条链路两个上限"：n=10 走这条被拒、走 batch 端点能过。
        if not (PULL_N_MIN <= n <= PULL_N_MAX):
            raise ApiError(400, f"每批帧数 n 只接受 {PULL_N_MIN}~{PULL_N_MAX}"
                                f"（固件 RATE PULL 的上限）。")
    if t_ms is not None:
        t_ms = int(t_ms)
        if not (50 <= t_ms <= 1000):
            raise ApiError(400, "批间隔 t_ms 只接受 50~1000（上游 75 ms 量化）。")
    loop = asyncio.get_running_loop()

    def _apply():
        if mode == "pace":
            return mc.stream.pace(n=n, t_ms=t_ms)
        if mode == "stopwait":
            return mc.stream.on()
        return mc.stream.off()

    r = await loop.run_in_executor(None, _apply)
    ctx.store.mark("mouse", "mouse.stream.mode", mode,
                   f"n={n} t_ms={t_ms} ok={r.ok}")
    try:
        now_mode = str(mc.stream.mode or "off")
    except Exception:                                     # noqa: BLE001
        now_mode = "off"
    return ok({"result": _res(r), "mode": now_mode,
               "pull": _pull_stats(mc)})


async def _mouse_stream_batch(request: web.Request) -> web.Response:
    """**每批帧数 n**（改版二十六）—— 「拉取」这条链上**唯一**的档位来路。

    body：`{"n": 1..16, "t_ms": 100}`（`t_ms` 可省，省了用协议默认 100 ms）。

    ⚠️ **n 不是"批内帧数"的修饰符，它就是拉取档**（上游 `TelemetryPuller` 的
    `kind` 由它决定）：`n == 1` ⇒ 停等（`TELEM`，`t_ms` 被忽略）；`n ≥ 2` ⇒ 批式
    （`RATE PULL n t`）。所以"把 n 设成 1"= 回到停等，**不是**"批式但每批 1 帧"。
    界面上不写清这一条，"设了还是那么慢"就会被当成没生效。

    ⚠️ 区间 **1~16 照抄上游** `configure()` 的校验（固件 `RATE PULL` 上限）。
    越界**必须 400**，不许在这里夹到边界：夹了之后界面显示"设置成功"、
    而设备按别的值跑，是最难查的一类静默错。

    ⚠️ 必须 `run_in_executor`：`tune()` 在 kind 变化时会 stop + start，
    `start()` 里要 `cmd.send(wait=True, timeout=2.0)`（试 3 次 ⇒ 最坏 6 s级）。

    ⚠️ 返回里的 `effective` 说"**这一刻**设备真的在用这个 n 吗"：流没开时 `tune`
    只把档位记进上游 `_pace_n`，设备侧还没变 ⇒ `false`。这一位不给，
    "改了没反应"就无从解释（三态纪律：判不了/没生效 ≠ 成功）。

    ⚠️ 本层**自己记着**这个 n（`MouseAdapter._pull_n`），并在 `connect()` 里重放：
    `mc.stream.on()` 每次都 `configure(1, t_ms)`，不重放的话掉一次线就悄悄回停等。
    """
    ctx = request.app["ctx"]
    d = await body_json(request)
    # ⚠️ 校验次序是有意的：**400（请求本身不对）→ 501（这份上游根本做不到）
    #    → 409（现在做不了）**。反过来先判 `_need_mouse` 的话，一条写错的 body
    #    会被报成"未连接电脑鼠"，现场会去查串口 —— 查一辈子也查不出来。
    if "n" not in d:
        raise ApiError(400, "要指定每批帧数 n（1~16；n=1 就是停等）。")
    try:
        n = int(d["n"])
    except (TypeError, ValueError):
        raise ApiError(400, f"每批帧数 n 必须是整数，收到 {d.get('n')!r}。")
    if not (PULL_N_MIN <= n <= PULL_N_MAX):
        raise ApiError(400, f"每批帧数 n 只接受 {PULL_N_MIN}~{PULL_N_MAX}"
                            f"（固件 RATE PULL 的上限），收到 {n}。n=1 就是停等，不必设 0。")
    t_ms = d.get("t_ms")
    if t_ms is not None:
        try:
            t_ms = int(t_ms)
        except (TypeError, ValueError):
            raise ApiError(400, f"批内节拍 t_ms 必须是整数，收到 {d.get('t_ms')!r}。")
        if t_ms < 1:
            raise ApiError(400, f"批内节拍 t_ms 必须 ≥1（毫秒），收到 {t_ms}。")
    # 能力位：老上游没有 `mc.stream.tune` ⇒ 明说改不了，不让现场以为"设成功了"。
    if not ctx.mouse.features.get("pace_pull"):
        raise ApiError(501, "当前上游没有批式拉取（`mc.stream.tune` 不存在）⇒ "
                            "每批帧数改不了。换停等模式，或升级上游。")
    _need_mouse(ctx)
    loop = asyncio.get_running_loop()
    r = await loop.run_in_executor(None, lambda: ctx.mouse.apply_pull_n(n, t_ms))
    ctx.store.mark("mouse", "mouse.stream.batch", str(n),
                   f"t_ms={r.get('t_ms')} mode={r.get('mode')} "
                   f"effective={r.get('effective')}")
    return ok(r)


async def _mouse_maint(request: web.Request) -> web.Response:
    """**维护与自检**：v2.7 按键菜单 2/4/5/6/7/8 的远程化（上游 `CAL` 家族）。

    一条端点对上游**一个命令族**（`{item}` 就是 `CAL <item>` 的那个词）——
    与线上"界面一个元素只许一条真相来路"同一条纪律，不做六个端点。

    `{item}` 取自上游 `CalibAPI.MENU_ITEMS`（见 `GET /api/v1/caps` 的 `maint_items`，
    界面按它画按钮）。body：`{"step": 1..4}` 仅红外分步标定要；`{"confirm": true}`
    擦除类必带（上游 `CAL ERASEIR` / `CAL ERASEALL`）。

    ⚠️ **本层不判 confirm 够不够**：原样透传给上游，由它的登记表 precheck 决定。
    不传时上游**完全不碰链路**、回 `NEED_CONFIRM` ⇒ 这里翻成 **409**
    （`error.type = "need_confirm"`），界面据此弹二次确认再带 `confirm` 重发。

    ⚠️ 必须 `run_in_executor`：`send_cmd` 是同步等 ACK 的（擦除 120~240 ms、
    超时 8~12 s），放在事件循环上会把整个服务冻住。

    ⚠️ 上游的拒绝码**原样透出**（`ERR_BUSY` / `ERR_VALUE` / `NEED_CONFIRM`），
    同时给**可照做**的 `hint`（见适配器 `_maint_annotate`）—— 协议词现场照不了。
    """
    ctx = request.app["ctx"]
    _need_mouse(ctx)
    item = str(request.match_info.get("item", "")).strip().upper()
    if not ctx.mouse.features.get("menu_remote"):
        raise ApiError(501, "这份上游没有按键菜单远程化（需要 RemoteControl 2.7.0 以上）。")
    if item not in ctx.mouse.maint_items:
        raise ApiError(400, "不是可远程的菜单项：" + (item or "(空)")
                            + "。可用项见 /api/v1/caps 的 maint_items。")
    d = await body_json(request)
    step = d.get("step")
    if item == "IRCAP":
        try:
            step = int(step)
        except (TypeError, ValueError):
            raise ApiError(400, "红外分步标定要给步号 step=1~4。")
        if not (1 <= step <= 4):
            raise ApiError(400, "step 只接受 1~4。")
    loop = asyncio.get_running_loop()
    r = await loop.run_in_executor(
        None, lambda: ctx.mouse.maint(item, step=step, confirm=bool(d.get("confirm"))))
    res = _res(r)
    data = res.get("data") or {}
    ctx.store.mark("mouse", "mouse.maint." + item.lower(), item,
                   "ok=%s status=%s" % (res["ok"], res["status"]))
    if not res["ok"] and res["status"] == "NEED_CONFIRM":
        raise ApiError(409, data.get("hint") or "这是不可逆操作，需要确认。",
                       type="need_confirm", item=item)
    return ok({"item": item, "step": step, "result": res})


def _pull_stats(mc: Any) -> Optional[dict]:
    """拉取器状态快照（2.1.0 才有；老上游返回 None 而不是 {}）。"""
    try:
        return dict(mc.stream.stats())
    except Exception:                                         # noqa: BLE001
        return None


async def _mouse_maze(request: web.Request) -> web.Response:
    """拉一次**固件自述的墙图**（`TYPE_MAP`，整块）。

    ⚠️ 这是一条**命令往返**（真机 ≈1.21 s），跑动中拉会占掉一次拉取的机会。
    所以界面上它是"按需点一下"，不是心跳。

    顺带把它**记进当前记录**（若正在记）：固件这份是**对账基准** ——
    界面上那张图是主机按帧重建的，两者对不上就说明重建逻辑有问题（真机要验的一条）。
    """
    ctx = request.app["ctx"]
    mc = _need_mouse(ctx)
    loop = asyncio.get_running_loop()
    m = await loop.run_in_executor(None, mc.maze.fetch)
    if m is not None:
        try:
            ctx.maze_rec.note_map(m)
        except Exception:                                     # noqa: BLE001
            pass      # 记不上不该让"读墙图"失败
    return ok({"map": m, "grid": mc.maze.grid(), "visited": mc.maze.visited(),
               "position": mc.maze.position(), "mazetype": mc.maze.mazetype(),
               "text": mc.maze.to_text()})


# ---------------------------------------------------------------- 统一帧静态码表
# 2.0.0 起**没有 SNAP**：全部测量量都在唯一的那条统一帧里（39 个扁平通道）。
# 这条端点因此变成"这条流里有些什么、迷宫码表长什么样"的纯元数据 —— 零设备访问。
#
# ⚠️ 下面这几个处理函数里 `_need_mouse(ctx)` 只当**闸门**用（它是统一的 409 文案），
# 真正干活的必须是 `ctx.mouse`（本层适配器）—— `_need_mouse` 返回的是**上游**
# MouseClient，而这些方法定义在适配器上。混用会得到
# `'MouseClient' object has no attribute 'maze_view'`（一条 500，看着像上游的锅）。

async def _mouse_meas_meta(request: web.Request) -> web.Response:
    """统一帧的通道清单 + 迷宫静态码表（通道名 / 路口类型 / 任务名）。

    **一次设备都不碰** —— 界面在未连接时也要能把"这条流里有哪些量"画出来，
    否则这一页在连上之前是纯空白（而它最该被用的时候恰恰是"准备阶段"）。
    """
    ctx = request.app["ctx"]
    return ok(ctx.mouse.meas_meta())


# ---------------------------------------------------------------- 迷宫（实验 8）
# 2.0.0 起**状态从统一帧读**，没有事件流，所以是**三条读的路**：
#   state   —— 最新状态 + 位置 + 迁移序列（界面心跳，**不发命令**）
#   history —— 只用任务迁移序列（复盘用）
#   summary —— 汇总 + 人话版


async def _maze_state(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    _need_mouse(ctx)
    return ok(await asyncio.get_running_loop().run_in_executor(None, ctx.mouse.maze_view))


async def _maze_history(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    _need_mouse(ctx)
    n = _int(request, "n", 0) or None
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(None, lambda: ctx.mouse.maze_history(n)))


async def _maze_summary(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    _need_mouse(ctx)
    return ok(await asyncio.get_running_loop().run_in_executor(None, ctx.mouse.maze_summary))


async def _maze_reset(request: web.Request) -> web.Response:
    """清**本层**的任务迁移差分缓冲。2.0.0 无 `MAZE RST`（固件侧统计已无上报通道）。"""
    ctx = request.app["ctx"]
    _need_mouse(ctx)
    return ok(await asyncio.get_running_loop().run_in_executor(None, ctx.mouse.maze_reset))


# ---------------------------------------------------------------- 实验 8：记录与复盘
# 设计理由（为什么记录器订阅上游 TELEMETRY、为什么墙图不存而是重建、
# 为什么自动收尾是**弱判据**）全部写在 `control/maze_rec.py` 的模块 docstring 里。

async def _maze_record_status(request: web.Request) -> web.Response:
    """记录状态一行（在记吗 / 多少帧 / 多久了 / 上次保存的摘要）。**零设备访问。**"""
    return ok(request.app["ctx"].maze_rec.status())


async def _maze_reconstruct(request: web.Request) -> web.Response:
    """**实时**的墙图重建（界面中栏的"重构示意"）—— 从最近一段帧算出来。

    为什么不复用 `GET /mouse/maze`（固件那份墙图）：那是**命令往返**（真机 ≈1.21 s），
    跑动中拉会占掉一次拉取的机会；而这一条是纯内存读（上游环形缓冲，默认 30 s），
    可以跟着界面刷新走。固件那份作为**对账基准**一并返回（`firmware`），不发命令。
    """
    ctx = request.app["ctx"]
    sec = _float(request, "seconds", 30.0) or 30.0
    loop = asyncio.get_running_loop()
    out = await loop.run_in_executor(None,
                                     lambda: ctx.mouse.maze_reconstruct(sec))
    # 终点区：**赛道属性**，不是设备状态（v9 删了 `goalX/goalY`，帧里没有这个信号）。
    # 常量只有一处真相（`maze_rec.GOAL_CELLS`）⇒ 由后端端给界面，
    # 前端**不再自己写一份**（铁律 32：一个元素只许一条真相来路）。
    out["goal_cells"] = [list(c) for c in GOAL_CELLS]
    out["goal_note"] = "赛道固定：中心 2×2（16×16 迷宫）"
    return ok(out)


async def _maze_reconstruct_clear(request: web.Request) -> web.Response:
    """**清空比赛页的实时墙图**（改版三十四）—— 从这一刻重新累积。

    为什么需要它：墙图是**本层增量累积**的（改版三十三），一旦攒进去就会一直留着。
    换了一盘迷宫、或者图上明显被某一帧的错误值弄脏了，人需要能说"从这里重新来"。
    没有这个按钮，唯一的办法是**拔掉重连**（把设备状态也一起丢了）。

    ⚠️ 只清**显示用的那张图**：不碰记录、不碰设备、不等价于"重连"。
    ⚠️ 清零基线是**现在**（不是 0）：否则下一轮会把环形缓冲里**上一盘**的 30 秒
       全喂进来，图上又是一张"看着正常、其实来自上一盘"的图。
    """
    ctx = request.app["ctx"]
    ctx.mouse.reset_live_map("界面清空")
    ctx.store.mark("mouse", "maze.live.clear", "", "界面清空")
    return ok({"cleared": True,
               "note": "实时墙图已清空，从下一帧重新累积（记录与设备都没动）"})


async def _maze_decisions(request: web.Response) -> web.Response:
    """**实时决策流**（右栏那条时间线）+ 运行状态。

    **零设备访问**（只读记录器内存里的常驻差分器）⇒ 可以跟着 1 s 的界面刷新走。

    为什么要单独一条端点而不是复用 `/mouse/maze/state`：
    `state` 给的是**此刻的快照**（任务/路口/位置各一个值），而"决策历程"是一条
    **序列** —— "它刚才识别到岔路口了"这句话在快照里根本表达不出来（快照只有"现在"）。
    上游 v2.0 删掉了 `MAZE EV` 事件流，这条序列现在**只能由主机差分**（见
    `control/maze_rec.py` 的 `MazeDecisions`，每一类的判据与上游依据都在它的 docstring 里）。

    应答里带的东西，都是界面上要用的：
      · `decisions`      —— 决策序列（`t_ms` 已相对环零点）；
      · `events`         —— **上行事件**（`STOPPED` / `ESTOP TRIP`，同一零点）。
        `reason` 从帧里推不出来，只能靠这条线；由后端一起给，前端不必自己攒
        （前端自己攒的话，刷新一次就丢一半，且与 decisions 的零点对不齐）。
      · `duration_ms`    —— 环零点到现在的长度（进度条按它画比例）；
      · `running`/`task` —— **运行状态**（顶栏那个胶囊要"醒目显示现在处于运行中"）。

    ⚠️ 零点口径：`start()`（发车）会把常驻环清成新纪元，零点由**发车后的第一帧**钉住
    ——与落盘/快照那份的 `t0` 是同一个数。所以这条线与你点【进入回放】看到的那条
    **逐条数值相等**（这正是本轮修的 bug：以前只挪零点不清环，上一段的决策以
    **负时间**残留，两条来路看起来完全不一样）。
    """
    ctx = request.app["ctx"]
    out = ctx.maze_rec.live_decisions()
    # 运行状态从**最新一帧**读（零设备访问）。三态：跑 / 停 / 判不了（没有帧）。
    st = None
    task = None
    try:
        if ctx.mouse.mc is not None:
            st = ctx.mouse.mc.maze.state()
            task = (st or {}).get("task")
    except Exception:                                         # noqa: BLE001
        st = None
    if task is None:
        run = None                       # 判不了 ≠ 停止（三态纪律）
    else:
        # 在跑 = **2..5**（SEARCH / SPURT / SPURT45 / SPURT90）。
        # 两个边界都要看：
        #   · 下界不含 0=WAIT / 1=START（START 只是发车那一瞬的过渡态）；
        #   · 上界不含 6=END（上游 2.11 换基线新增的**跑位收尾态**，固件已进 maze_done，
        #     车不动了）。只写 `>= 2` 会把"跑完待收尾"报成"还在跑"。
        # ⚠️ 与 `mod_maint.busyWhy()`、`mod_race.renderState()` 是**同一个判据**，
        #    改一处要一起改（铁律 50④）。
        run = 2 <= int(task) <= 5
    out["running"] = run
    out["task"] = task
    out["taskName"] = (st or {}).get("taskName")
    out["motionName"] = (st or {}).get("motionName")
    out["recording"] = bool(ctx.maze_rec.recording)
    return ok(out)


async def _maze_decisions_clear(request: web.Request) -> web.Response:
    """**清空决策历程**（右栏那条线上的【清空】按钮）。

    为什么需要它：决策与上行事件都由记录器那个**常驻环**供货（`live_decisions()`），
    前端每 1 s 全量重拉 ⇒ "只抹前端那几行 DOM"在下一次刷新就全回来了。
    用户要的是"这一段到此为止、下一段从零开始"（方便下一次发车的记录），
    所以正确的动作是让记录器**开一个新纪元**（清环 + 换差分器），
    而不是在浏览器里清显示 —— 那是上一轮踩过的坑（旧【清空显示】点了没反应）。

    ⚠️ 与发车那条路（`_timesync_maze_run` 里 `start()` 的 `_reset_epoch("launch")`）
    的区别：那里**带 stash**（发车失败要能整体放回去），这里**不带** ——
    用户手工清过之后，别处无权再"放回来"。**不影响正在记录的那一段**（只是清显示环）。

    **零设备访问**：纯内存操作，不发命令、不碰链路。
    """
    ctx = request.app["ctx"]
    out = ctx.maze_rec.clear_decisions()
    ctx.store.mark("mouse", "maze.decisions.clear", "", out.get("note") or "")
    return ok(out)

async def _maze_record_live(request: web.Request) -> web.Response:
    """**当前这一段**的完整快照（与落盘那份同形状）—— 供"边跑边回放"。

    `?with_map=0` 可跳过墙图（墙图能从帧重放重建，见 `maze_rec` 的模块 docstring）。

    ⚠️ **零设备访问、也不落盘、不改状态**：它就是"把此刻这一段组一份 doc 出来"。
    拿到的是**调用那一刻的快照**，之后新帧不会进来 —— 冻结才叫回放；想回到最新就退出回放。
    没在记录时 `doc` 为 `null`（那时没有"这一段"可回放）。
    """
    ctx = request.app["ctx"]
    loop = asyncio.get_running_loop()
    doc = await loop.run_in_executor(None, ctx.maze_rec.snapshot)
    if doc is None:
        return ok({"doc": None, "note": "当前没有在记录，没有可回放的这一段"})
    if not _int(request, "with_map", 1):
        doc.pop("wall", None)
    return ok({"doc": doc, "live": True})


async def _maze_record_start(request: web.Request) -> web.Response:
    """开始记录。**由【发车】自动触发**（见 `_timesync_maze_run`），也可单独调。

    ⚠️ **不要求已连接** —— 这是有意的：真机上"连接"可能因为蓝牙被灌满而暂时不可用，
    而那时恰恰最需要把**已经收到的东西**记下来（记录器只在收到帧时才有内容，
    没帧就是空记录，空记录不会被写成文件）。
    """
    ctx = request.app["ctx"]
    d = await body_json(request) or {}
    why = str(d.get("why") or "manual")
    # 没显式给就用**设备报的**尺寸（不是上游 ring 那个恒为 7 的字段，见 adapter.mazetype()）
    mazetype = d.get("mazetype") or ctx.mouse.mazetype()
    # ⚠️ **先保证有帧**。记录器只在收到帧时才写文件（空记录不落盘），所以"拉了但流没开"
    #    的结果是：界面说已开始记录、跑完一条记录也没有。这个坑与 `session_open`
    #    同源（改版三十一在那里补了兜底，这里漏了）。开流失败不阻断开始记录 ——
    #    但要如实告诉调用方"现在没有帧"，否则空记录无从解释。
    loop = asyncio.get_running_loop()
    pull_note = ""
    # `auto_pull: false` = **别动链路**（调用方自己已经安排好，或刻意要造一段没有帧的记录）。
    # 默认 true：人点【开始记录】时不该得到一条空记录。
    want_pull = d.get("auto_pull", True) is not False
    if not want_pull:
        pull_note = "按调用方要求未动拉取"
    elif ctx.mouse.connected and not ctx.mouse.stream_on:
        try:
            r = await loop.run_in_executor(None, lambda: ctx.mouse.set_stream(True))
            applied = (r or {}).get("applied_n")
            pull_note = f"已自动开流（档 n={applied}）"
            ctx.bus.log("info", f"开始记录前自动开流：{pull_note}", source="mouse")
        except Exception as e:                                # noqa: BLE001
            pull_note = f"拉取没能开起来：{type(e).__name__}: {e}"
            ctx.bus.log("warn", f"{pull_note} ⇒ 这条记录可能没有帧", source="mouse")
    elif not ctx.mouse.connected:
        pull_note = "未连接设备：这条记录不会有帧"
    out = ctx.maze_rec.start(why=why, mazetype=mazetype)
    if out.get("ok"):
        ctx.store.mark("mouse", "maze.record.start", "", f"why={why}")
    if pull_note:
        out["pull_note"] = pull_note
    return ok(out)


async def _maze_record_stop(request: web.Request) -> web.Response:
    """停止并保存（**人工兜底**）。

    为什么必须有这个按钮：固件**没有"迷宫结束"事件**（`MAZE RUN` 之后就一直处于
    迷宫态）⇒ 自动收尾那几个判据都是**弱判据**，只有跑完那一刻碰巧命中。
    没有人工兜底，模拟器上根本收不了尾。

    ⚠️ 2.1.1 起"收尾"这件事**多了两个正经动作**（本题只负责存盘，不负责动车）：
      · 【收车】`MAZE STOP` —— 停车 + 复位发车状态 + 解锁急停
        （正常 0.2~1 s 退出；爬不出来则固件 2 s 后自己软复位）；
      · 【复位】`MCU RESET` —— 整机软复位（连不上、卡死时用它；不复位物理位置）。
    ⇒ 收车与存盘是**两件事**，界面上也是两个按钮：先收车（让车停下来、让曲线有意义），
    再【停止并保存】。把两者混成一个按钮会让"只存盘不收车"变得不可能，
    而复盘恰恰需要后者（车还在跑时也想把已收到的先存下来）。
    """
    ctx = request.app["ctx"]
    loop = asyncio.get_running_loop()
    out = await loop.run_in_executor(None, lambda: ctx.maze_rec.stop(why="manual"))
    if out.get("ok"):
        ctx.store.mark("mouse", "maze.record.saved", str(out.get("name") or ""),
                       f"frames={out.get('frames')} why=manual")
    return ok(out)


async def _maze_records(request: web.Request) -> web.Response:
    """列出已有记录（按时间倒序）。只读文件名与大小。"""
    ctx = request.app["ctx"]
    n = _int(request, "n", 50) or 50
    loop = asyncio.get_running_loop()
    return ok({"records": await loop.run_in_executor(None, lambda: ctx.maze_rec.records(n))})


async def _maze_record_get(request: web.Request) -> web.Response:
    """读一条记录（复盘的数据源）。`?with_map=0` 可跳过墙图。

    ⚠️ 记录里含**全量 39 通道每帧**，所以它比其它端点大得多（几百 KB 量级）。
    判据 I10 只管 **WS 上的**载荷，HTTP 响应不受它约束 —— 但仍走 JSON（不用二进制），
    因为复盘是"人点一下、等一等"的动作，不是连续流。
    """
    ctx = request.app["ctx"]
    name = request.match_info.get("name") or ""
    wm = _int(request, "with_map", 1)
    loop = asyncio.get_running_loop()

    def _load() -> dict:
        try:
            return ctx.maze_rec.load(name, with_map=bool(wm))
        except ValueError as e:
            raise ApiError(404, str(e)) from e
        except Exception as e:                                # noqa: BLE001
            raise ApiError(500, f"读记录失败：{type(e).__name__}: {e}") from e

    return ok(await loop.run_in_executor(None, _load))


async def _maze_record_import(request: web.Request) -> web.Response:
    """导入一份**外部**记录（前端把文件内容 POST 上来），校验后落进记录目录。"""
    ctx = request.app["ctx"]
    d = await body_json(request, required=True)
    doc = d.get("doc") if isinstance(d.get("doc"), dict) else d
    name = str(d.get("name") or "")
    loop = asyncio.get_running_loop()
    try:
        out = await loop.run_in_executor(None, lambda: ctx.maze_rec.import_doc(doc, name))
    except ValueError as e:
        raise ApiError(400, str(e)) from e
    except Exception as e:                                    # noqa: BLE001
        raise ApiError(500, f"导入失败：{type(e).__name__}: {e}") from e
    return ok(out)


async def _maze_stop(request: web.Request) -> web.Response:
    """**收车**（上游 2.1.1 的 `MAZE STOP`）：停车 + 复位发车状态 + 解锁急停。

    ⚠️ 这是**推翻 v2.0 一条结论**的能力。以前本层的说法是"没有'停止跑迷宫'，
    跑完只能复位或重连"，因为 `MAZE RUN` 之后固件主循环停在 `LESSON_Blind8()` 的
    `while(1)` 里、发车状态永不复位 ⇒ 再发车永远 `ERR_BUSY`。

    ⚠️ 2026-10-16 之前它**车在运动时不可靠**（`linkMazeAbort` 标志的检查在 while(1)
    顶部，而主循环有 30 余处非协作式阻塞等待，运动时程序正卡在那些循环内部 ⇒
    回 `OK` 但 `mazeTask` 仍 `SEARCH`）。上游为此补了三个中止出口 + `LINK_Tick`
    **2 s 兜底软复位** ⇒ 现在正常 **0.2~1 s** 退出；极端情况 2 s 后固件自己软复位
    （`PING` 的 `rst` 由 `POR` 变 `SW`）。
    ⇒ 应答里仍**带实测状态**（`state` / `reliable`），让界面当场判"到底收住没有"。
       **不许只看 `OK` 就宣布收车成功**（`OK` 只表示命令被接受）。
    """
    ctx = request.app["ctx"]
    _need_mouse(ctx)
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(None, ctx.mouse.maze_stop))


async def _mouse_rescue(request: web.Request) -> web.Response:
    """**复位**（上游 2.2.0 的 `MCU RESET`）：整机软复位 —— 卡死复位口。

    `?verify=0` 可跳过复位后的 `PING` 复核（默认复核，**强烈建议复核**：
    `accepted=False` 不代表失败，真正的判据是复位原因变成 `SW`）。

    ⚠️ 因为它是**整机复位**，本层把两条后果写进应答，交给界面显示：
      ① 复位后**约 3 s 才有新帧**（此前可能是复位前的旧帧）⇒ 别急着判读状态；
      ② 复位**不复位物理位置** ⇒ 必须人工把电脑鼠放回起点角（见适配器 `rescue()`）。
    """
    ctx = request.app["ctx"]
    _need_mouse(ctx)
    verify = _int(request, "verify", 1)
    loop = asyncio.get_running_loop()
    out = await loop.run_in_executor(None, lambda: ctx.mouse.rescue(verify=bool(verify)))
    out["advice"] = ("复位后约 3 s 才有新遥测帧，判读状态请等新鲜帧；"
                     "复位不复位物理位置，发车前必须把电脑鼠放回起点角。")
    return ok(out)


async def _mouse_launch_precheck(request: web.Request) -> web.Response:
    """发车前**低电预检**（上游 2.2.1 那道硬闸的"提前可见"版）。

    **零设备访问**（只读内存里的最新帧）⇒ 可以在界面上跟着心跳刷新。
    返回 `level` = `ok` / `warn` / `unknown`：**`unknown` 不是通过也不是失败**
    （未连接 / 没帧 / 电池监视未开启），三态纪律，界面不许涂红。
    """
    ctx = request.app["ctx"]
    _need_mouse(ctx)
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(None, ctx.mouse.launch_precheck))


async def _mouse_move(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    mc = _need_mouse(ctx)
    d = await body_json(request, required=True)
    kind = str(d.get("kind") or "").lower()
    loop = asyncio.get_running_loop()
    if kind in ("forward", "fwd"):
        fn = lambda: mc.move.forward(int(d["pulses"]), speed=d.get("speed"),
                                     wait_done=bool(d.get("wait_done", False)))
    elif kind in ("backward", "back", "bwd"):
        # 上游无 rear：后退用**负速度**的 run（`sp_l/sp_r` 可正可负）。
        fn = lambda: mc.move.run(-abs(int(d.get("speed") or 40)),
                                 -abs(int(d.get("speed") or 40)),
                                 int(d.get("ms") or 2000), int(d.get("ir_mm", 10)),
                                 wait_done=bool(d.get("wait_done", False)))
    elif kind in ("rotate", "rot", "turn"):
        # `pwm` 实为旋转速度(cm/s)；兼容旧字段名 `pulses`。
        fn = lambda: mc.move.rotate(int(d.get("pwm", d.get("pulses", 40))),
                                    int(d.get("ms") or 0),
                                    wait_done=bool(d.get("wait_done", False)))
    elif kind in ("run", "drive"):
        # 按时长驱动：左/右轮速度(cm/s) + 毫秒。前进=两侧同号，转向=两侧异号。
        fn = lambda: mc.move.run(int(d.get("sp_l", 40)), int(d.get("sp_r", 40)),
                                 int(d.get("ms") or 2000), int(d.get("ir_mm", 10)),
                                 wait_done=bool(d.get("wait_done", False)))
    elif kind in ("servo", "sweep"):
        fn = lambda: mc.move.servo(str(d.get("act") or "SWEEP"),
                                   wait_done=bool(d.get("wait_done", False)))
    else:
        raise ApiError(400, "kind 只能是 forward / backward / rotate / run / servo")
    r = await loop.run_in_executor(None, fn)
    ctx.store.mark("mouse", f"move.{kind}", "", json.dumps(d, ensure_ascii=False))
    return ok({"result": _res(r), "task": mc.move.current_task()})


async def _session_note(request: web.Request) -> web.Response:
    """给会话写一条备注（进 notes.md + timeline）。"""
    ctx = request.app["ctx"]
    d = await body_json(request, required=True)
    text = str(d.get("text") or "").strip()
    if not text:
        raise ApiError(400, "text 是空的")
    ctx.store.note(text)
    ctx.store.mark("session", "note", "", text[:200])
    return ok({"text": text})


async def _mouse_tare(request: web.Request) -> web.Response:
    """清零累加量（陀螺角 / 里程基准）。上游没有对应的门面方法，直接用**登记名**。"""
    return await _simple_cmd(request, "tare")


async def _mouse_stop(request: web.Request) -> web.Response:
    """正常停车 + **解急停锁存**（`stop`，线上文本 `STOP`）。

    这是"急停之后重新拿回控制权"的入口 —— 急停是**锁存**的，不解锁的话
    之后的运动/发车都会被拒（固件收下请求再被 ISR 每帧清零速度，现场看起来
    就是"发了车却不动"）。
    """
    return await _simple_cmd(request, "stop")


async def _mouse_estop(request: web.Request) -> web.Response:
    """急停（锁存）。**这是安全功能**，见 `_simple_cmd` 的缺陷说明。"""
    return await _simple_cmd(request, "estop")


async def _simple_cmd(request: web.Request, name: str) -> web.Response:
    """发一条"无参数、只看成功与否"的登记命令。

    ⚠️ **`name` 必须是 `registry.COMMANDS` 的键（登记名，全小写），不是线上文本。**

    这里曾经传的是**线上文本**（`"TARE"` / `"STOP"` / `"ESTOP"`），而上游
    `mc.send_cmd()` 第一件事就是 `COMMANDS[name]` ⇒ `KeyError: 'ESTOP'` ⇒
    **`/mouse/tare`、`/mouse/stop`、`/mouse/estop` 三个端点一直是 404**。
    最糟的是它包含了**急停**（顶栏那个按钮），而**没有任何判据调过这三个端点**
    ⇒ 从来没红过（2026-10-05 接 2.2.1 的"急停锁存中发车回 ERR_ESTOP"这条路径时才发现，
    见设计方案 §11.12）。
    ⇒ 由此得到一条纪律的推广：**"一道从没红过的闸不算闸"要用在按钮上**，
    不只是用在判据上 —— 界面上的每个动作按钮都该有一条端到端判据走一遍。

    这里先按登记表校验一遍名字，好让"名字写错"变成一句**能读懂的话**（而不是 404 KeyError）：
    登记名写错是**本层的编码错**，不是现场问题，所以报 500 并把可选项列出来。
    """
    ctx = request.app["ctx"]
    mc = _need_mouse(ctx)
    reg = getattr(ctx.mouse, "_registry", None) or getattr(mc, "registry", None)
    known = set(getattr(reg, "COMMANDS", {}) or {})
    if known and name not in known:
        near = sorted(k for k in known if k.startswith(name[:3].lower()))
        raise ApiError(500, f"本层登记名写错了：{name!r} 不在上游命令表里"
                            f"（这是本层的编码错，不是设备问题）。"
                            f"相近的登记名：{near[:6]}")
    loop = asyncio.get_running_loop()
    r = await loop.run_in_executor(None, lambda: mc.send_cmd(name, timeout=3.0))
    ctx.store.mark("mouse", f"cmd.{name.replace('.', '_')}", str(r.status), f"ok={r.ok}")
    return ok({"result": _res(r)})


async def _mouse_raw(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    d = await body_json(request, required=True)
    line = str(d.get("line") or d.get("cmd") or "").strip()
    if not line:
        raise ApiError(400, "line 是空的")
    res = await asyncio.get_running_loop().run_in_executor(
        None, ctx.mouse_send_raw, line)
    return ok(res)


def _need_mouse(ctx: Any) -> Any:
    mc = ctx.mouse.mc
    if mc is None or not mc.is_connected:
        raise ApiError(409, "未连接电脑鼠")
    return mc


def _res(r: Any) -> dict:
    return {"ok": bool(getattr(r, "ok", False)), "status": getattr(r, "status", ""),
            "data": getattr(r, "data", {})}


# ================================================================ 时间同步 / 发车
# 上游 1.4.0 起，固件毫秒时基 → 本机单调秒 由 `mc.timesync` 负责（min-delay + ppm）。
# 这里只做透传与换算，**不在本层重算**（见 control/clock.py 的模块 docstring）。

async def _timesync(request: web.Request) -> web.Response:
    """发车与时间同步的总览。

    包含三样东西：
      · `timesync` —— 上游的同步状态（offset / drift_ppm / one_way_ms / 样本数 / 基准灯）
      · `link`     —— 含**丢帧**（seq_lost / seq_gap）
      · `sample`   —— 用最新一帧实际演示"帧发送时刻 vs 到达时刻"差多少，
                     让"这次更新到底改善了啥"在界面上看得见
    """
    ctx = request.app["ctx"]
    loop = asyncio.get_running_loop()
    out = {
        "timesync": ctx.mouse.timesync_snapshot(),
        "link": await loop.run_in_executor(None, ctx.mouse.link_stats),
        "anchor": anchor_snapshot(),
        "clock": ctx.clock.snapshot(),
    }
    mc = ctx.mouse.mc
    if mc is not None and mc.is_connected:
        def _demo() -> Optional[dict]:
            """用**最新一帧**演示"发送时刻 vs 到达时刻"差多少。

            ⚠️ 2.0.0 起 `telemetry.latest()` 返回的是**一个扁平 dict**（不再是
            `(fast, mid, slow, ...)` 那样的三元组）—— 老代码写的 `latest()[0]`
            现在是 `dict[0]` ⇒ `KeyError` ⇒ 被这里吞成 `None` ⇒ 端点从此**永远不回
            `sample`**，而界面只会看到"延迟那一栏是空的"。这类"静默降级"比报错难查得多。
            """
            try:
                rec = mc.telemetry.latest() or {}
            except Exception:                                 # noqa: BLE001
                return None
            if not isinstance(rec, dict) or not rec:
                return None
            t_send = ctx.mouse.rec_time_ms(rec)
            age = ctx.mouse.rec_age_ms(rec)
            return {
                "dev_ms": ctx.mouse.rec_dev_ms(rec),
                "ms8": rec.get("ms8"),
                "t_send_pc_ms": t_send,
                "arrival_pc_ms": (None if t_send is None or age is None
                                  else round(t_send + age, 1)),
                "age_ms": (None if age is None else round(age, 1)),
                "note": "t_send_pc_ms 为帧发生时刻，arrival 为主机收到时刻",
            }
        out["sample"] = await loop.run_in_executor(None, _demo)
    return ok(out)


async def _timesync_arm(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    _need_mouse(ctx)
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(None, ctx.mouse.arm))


async def _timesync_maze_run(request: web.Request) -> web.Response:
    """远程发车跑迷宫。**顺带自动开始记录**（"从发车到停止"的"发车"就是这里）。

    ⚠️ **顺序是先开记录再发车**，不能反过来：发车后最初几帧正是"它一开始看到了什么"，
    那一小段恰恰是复盘时最想看的（起点朝向、第一面墙）。反过来就会丢开头。

    ⚠️ 记录**失败不影响发车**：车该走还是要走。记录失败只记一行日志 ——
    把"记不上"升级成"发不了车"是本末倒置（而且现场很可能就是因为记录写不进磁盘
    才出问题，那时更该让车照常跑）。

    ## 2.4.0：`target` 决定跑哪套剖面（**这是「比赛」页与「实验 8」页的分岔口**）

    | target | 固件路径 | 界面上的入口 |
    |---|---|---|
    | `BLIND`（默认） | `LESSON_Blind8()` 无记忆走迷宫 | 「无记忆走迷宫」页 |
    | `RACE` | 先读 Flash 地图选档/定冲刺，再进主循环 | 「比赛」页 |

    ⚠️ **`target` 只在 request body 里给**，不给就是 `BLIND` —— 与上游"默认省略目标词"
    同构，于是 v2.3 时代的调用点与日志格式**原样保留**（纪律 26：越"零改动跟上"越要
    逐条读文案，这里我们反而是**故意**让旧路径一字不改）。

    ⚠️ **不许把 `RACE` 拼给不认识它的上游**：老上游的 `MazeAPI.run()` 是零参的，
    传 `target=` 会 `TypeError` ⇒ 冒成 500，看着像"本层拼错了请求"。
    所以这里先看能力探测（`maze_target`），不认就**当场 400 说清楚**。
    """
    ctx = request.app["ctx"]
    _need_mouse(ctx)
    d = await body_json(request, required=False)
    target = str((d or {}).get("target") or "BLIND").strip().upper() or "BLIND"
    if target not in ("BLIND", "RACE"):
        raise ApiError(400, f"target 只能是 BLIND 或 RACE，收到 {target!r}")
    if target == "RACE" and not ctx.mouse.caps().get("maze_target"):
        raise ApiError(
            400,
            "这台设备上的上游不支持 RACE 发车（`MazeAPI.run()` 没有 target 形参）。\n"
            "  · RACE 是 RemoteControl 2.4.0 新增的（`MAZE RUN [BLIND|RACE]`）；\n"
            "  · 2.3.0 及更早只有无记忆走迷宫，也就是 BLIND。")
    loop = asyncio.get_running_loop()
    # 先取一份**低电预检**随应答带回去（纯读帧）。它**不是闸** —— 真闸在上游
    # `send_cmd` 的登记表层（脚本/CLI 也绕不开）；这里只是让"电池不够"这件事
    # 在界面上**提前**可见，而不是等人点了发车才吃一个 ERR_LOWBATT。
    precheck = await loop.run_in_executor(None, ctx.mouse.launch_precheck)
    # ⚠️ **先拉取，再开记录，最后发车**（改版三十七）—— 顺序不能换：
    #    ① `ensure_pull()`：确认拉取在跑并**等到一帧新帧**。发车时若拉取本来没在跑，
    #       上游 `suspend()` 会直接返回、`resume()` 成空操作 ⇒ 发完车仍然不拉，
    #       只能等切页（前端 `syncPull()` 只在切页/启动调用）——现场就是
    #       "发完车过一会儿数据才跟上"。等不到就 409 拒绝发车（车不动）。
    #    ② `start()`：记录从"链路已经在出帧"的状态开始，帧 0/1 不再是待机空档。
    #    ③ `maze_run()`：命令本身会让拉取让路 0.9~2.4 s（上游 urgent 语义），
    #       这段缺口消不掉 —— 但**两端都已有锚点**（见下面 sent/ack 两条 mark）。
    pull = await loop.run_in_executor(None, ctx.mouse.ensure_pull)
    rec_note = ""
    #: 这一次调用**新开了一段记录吗**（`start()` 的 `fresh_epoch`）。
    #: 它是下面"发车被拒时怎么收场"的唯一依据：只有新开的那一段才该被丢掉。
    fresh = False
    try:
        out = ctx.maze_rec.start(why="maze_run", mazetype=ctx.mouse.mazetype())
        if out.get("already"):
            rec_note = "（已在记录中，沿用上一段）"
        else:
            fresh = bool(out.get("fresh_epoch"))
            ctx.store.mark("mouse", "maze.record.start", "", "why=maze_run")
            # 新一跑 ⇒ 比赛页的墙图从零攒。上一跑探到的墙若留在图上，
            # 那一片"看起来有墙"其实来自别的跑法（静默错，图上无从分辨）。
            ctx.mouse.reset_live_map("发车")
    except Exception as e:                                    # noqa: BLE001
        rec_note = f"（记录未能开始：{type(e).__name__}）"
        ctx.bus.log("warn", f"实验 8 记录没能开始：{type(e).__name__}: {e}", source="mouse")
    # 发车命令的两端锚点：`sent` 是命令发出的墙钟，`ack` 是设备应答回来。
    # 为什么必须记：命令期间拉取让路 ⇒ 记录里必有一段无采样；这两条锚点让
    # "缺口有多长、车在这段里跑了多远"以后能算（对轴用），而不是靠猜。
    t_sent = time.time()
    # 先把 `sent` 锚点交给记录器**再发命令**：缺口是在"下一帧到达时"才被记下来的，
    # 而那一帧可能比下面 `note_launch(ack)` 还早到。先给它划一个兜底窗口（5 s），
    # 那道缺口才不会被标成"原因不明"。
    try:
        ctx.maze_rec.note_launch(int(t_sent * 1000))
    except Exception:                                      # noqa: BLE001
        pass
    ctx.store.mark("mouse", "mouse.maze_run.sent", target,
                   f"pull_n={pull.get('n')} pull_started={pull.get('started')} "
                   f"frame_age_ms={pull.get('frame_age_ms')}")
    try:
        res = await loop.run_in_executor(None, lambda: ctx.mouse.maze_run(target))
    except Exception:
        ctx.store.mark("mouse", "mouse.maze_run.fail", target,
                       f"耗时 {int((time.time() - t_sent) * 1000)} ms（这段拉取是停的）")
        # 发车被拒（`ERR_LOWBATT` / `ERR_ESTOP` / `ERR_BUSY` / 其他）⇒ 这一段根本没开始。
        #
        # ⚠️ 判据是 **`fresh`（这一请求新开的段）**，不是"收到几帧"。
        #    以前写成"`frames == 0` 才收掉"，那是个**看运气**的判据：`start()` 之后命令
        #    往返要 1~3 s，期间待机帧照样进来 ⇒ 同一段代码有时收有时不收，
        #    而"收不收"还决定了常驻决策环要不要回滚 ⇒ 界面上的表现会**时好时坏**。
        #    改用 `fresh` 之后它是确定的：
        #      · `fresh=True` —— 这条记录是**本次刚建的**，发车又被拒 ⇒ 它必然只是
        #        "被拒之后那些待机帧"，对复盘毫无价值，收掉；常驻环也**放回上一段**
        #        （一次没开成的发车不该把右栏正在看的那一段抹成空的）；
        #      · `fresh=False` —— 本次是"沿用已在记的那一段"（`start()` 回了 already），
        #        那一段可能是真在跑的数据 ⇒ **原样不动**，收掉反而把它截断。
        try:
            if fresh:
                ctx.maze_rec.stop(why="launch_failed")
                ctx.maze_rec.restore_epoch()
                rec_note = (rec_note + "；发车被拒，已收掉这次记录").strip("；")
        except Exception:                                     # noqa: BLE001
            pass
        raise
    t_ack = time.time()
    try:
        # 补齐 `ack`：窗口收紧到"命令真正往返的那一段"，缺口据此标 `why=launch_cmd`。
        ctx.maze_rec.note_launch(int(t_sent * 1000), int(t_ack * 1000))
    except Exception:                                      # noqa: BLE001
        pass
    ctx.store.mark("mouse", "mouse.maze_run.ack", target,
                   f"耗时 {int((t_ack - t_sent) * 1000)} ms（这段拉取是停的，"
                   f"记录里必然有一个缺口）")
    return ok({**res, "precheck": precheck, "pull": pull,
               "record": ctx.maze_rec.status(), "record_note": rec_note})


async def _mouse_link_stats(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(None, ctx.mouse.link_stats))


# ================================================================ 参数（两套）
# ⚠️ 生产标定（id 1–7，Flash page 99）与课程调参（id 8–21，Flash page 100）是**两套**，
# 写入通道也不同。混用会失败，所以分开暴露、并且分组写回时自动选通道。

async def _mouse_params_grouped(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    _need_mouse(ctx)
    loop = asyncio.get_running_loop()
    refresh = request.query.get("refresh", "1") not in ("0", "false", "no")
    return ok(await loop.run_in_executor(None, lambda: ctx.mouse.params_grouped(refresh)))


async def _mouse_params_grouped_set(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    _need_mouse(ctx)
    d = await body_json(request, required=True)
    values = d.get("values") or {}
    if not values:
        raise ApiError(400, "values 是空的 —— 没有要写的东西")
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(
        None, lambda: ctx.mouse.set_params_grouped(
            {k: float(v) for k, v in values.items()},
            readback=bool(d.get("readback", True)), save=bool(d.get("save", False)))))


async def _mouse_params_save_group(request: web.Request) -> web.Response:
    """把某一组参数落 Flash。

    ⚠️ 课程组（page 116）在 2.0.0 必须**带值**：固件 `GET` 只查生产表，课程项读不回来，
    所以"读出当前值再落盘"这条路已经不存在 —— 值从请求体 `values` 里来
    （界面上就是那几个输入框）。不带给值会明确报错，而不是发一次空写假装成功。
    """
    ctx = request.app["ctx"]
    _need_mouse(ctx)
    d = await body_json(request) or {}
    group = str(d.get("group") or "生产标定")
    vals = {k: float(v) for k, v in (d.get("values") or {}).items()}
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(
        None, lambda: ctx.mouse.save_params(group, values=vals or None)))


# ================================================================ 电机实验（mc.lab）
# ⚠️ 轮子必须悬空。护栏在固件 1 ms 中断里，上位机被 kill 也不会失控。

def _lab_progress(ctx: Any, what: str):
    """把上游的进度回调接到总线上，让⑨实验页能实时看到。"""
    def _cb(frac: float, got: Optional[float] = None, total: Optional[float] = None) -> None:
        ctx.bus.publish("lab.progress", {
            "what": what, "frac": round(float(frac or 0.0), 4),
            "got": got, "total": total})
    return _cb


def _lab_sample(ctx: Any, what: str):
    def _cb(rec: dict) -> None:
        ctx.bus.publish("lab.sample", {"what": what, "sample": rec}, log=False)
    return _cb


async def _lab_status(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    if ctx.mouse.mc is None or not ctx.mouse.mc.is_connected:
        return ok({"connected": False,
                   "hint": "电脑鼠未连接"})
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(None, ctx.mouse.lab_status))


async def _lab_run(request: web.Request) -> web.Response:
    """跑一次带双护栏的远程运动，并**同时**采下运动期间的统一帧样本。

    2.0.0 起没有"停止后整块回传"（`SCOPE`/`DUMP` 已删），所以只接受 `sample_hz`
    （从最新快照里每秒抄几个点），不再有 `fetch_log` / `realtime_hz`。
    """
    ctx = request.app["ctx"]
    _need_mouse(ctx)
    d = await body_json(request) or {}
    args = dict(
        sp_l=int(d.get("sp_l", 40)), sp_r=int(d.get("sp_r", 40)),
        ms=int(d.get("ms", 2000)), ir_mm=int(d.get("ir_mm", 10)),
        sample_hz=float(d.get("sample_hz", 50.0)),
    )
    loop = asyncio.get_running_loop()
    smp = _lab_sample(ctx, "lab")
    res = await loop.run_in_executor(
        None, lambda: ctx.mouse.lab_run(on_sample=smp, **args))
    return ok(res)


async def _lab_log(request: web.Request) -> web.Response:
    """按频率采下**最新快照**若干秒（旧 fetch_log 的替代，见 `lab_fetch_log`）。"""
    ctx = request.app["ctx"]
    _need_mouse(ctx)
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(None, ctx.mouse.lab_fetch_log))


async def _lab_estop(request: web.Request) -> web.Response:
    """急停。**不过 motor_guard** —— 急停必须随时可用。"""
    ctx = request.app["ctx"]
    _need_mouse(ctx)
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(None, ctx.mouse.lab_estop))


async def _lab_resume(request: web.Request) -> web.Response:
    """解急停锁存。**这是重新获得控制权的唯一入口。**"""
    ctx = request.app["ctx"]
    _need_mouse(ctx)
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(None, ctx.mouse.lab_resume))


# ================================================================ 课程实验（mc.lesson）
#
# ⚠️ **上游 2.4.0 已删除课程调参整层**。下面这一组里：
#   · `/lesson/list`、`/lesson/status` → 照常 200，但 `available=false` + 一段说明。
#     **不返回 404**：界面要能画出"这一块已经不存在了，原因是……"，
#     而不是让"少了一半界面"变成"网络出错了"。
#   · `/lesson/run`、`/lesson/stop`、`/lesson/log` → **409 明确拒绝**（这些是动作，
#     假装成功比拒绝更坏）。`/lesson/log` 本来就一直是"明确失败"（见它的 docstring）。
#   · `/lesson/suggest`、`/lesson/apply` → **照常可用**：算参纯函数已迁到
#     `host/api/calc.py`，算出来的 TRACKL/TRACKR/GYRORATIO **全是生产参数**。

def _lesson_available(ctx: Any) -> bool:
    """上游还有没有课程层。**能力探测**，不看版本号。"""
    return bool((getattr(ctx.mouse, "_features", {}) or {}).get("lesson"))


_lesson_gone_msg = (
    "上游 RemoteControl 2.4.0 已删除课程调参实验整层（编译档位 5 → 2）。\n"
    "  · lesson.start / lesson.stop / lesson.status 三条命令已退出登记表；\n"
    "  · 课程 14 项参数（id 8–21）与 Flash page 116 一并释放；\n"
    "  · 曲线数据在旧版是实验期间就地捕获的，现在没有这条通道了。\n"
    "  · 算参仍然可用：POST /api/v1/lesson/suggest 算的是生产参数。")


async def _lesson_list(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    return ok({"exps": ctx.mouse.lesson_exps(),
               "theory": ctx.mouse.lesson_theory(),
               "available": _lesson_available(ctx),
               "note": ("" if _lesson_available(ctx) else _lesson_gone_msg),
               "features": dict(getattr(ctx.mouse, "_features", {}) or {})})


async def _lesson_status(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    if not _lesson_available(ctx):
        # 不 404：界面靠这一条把"这块没了 + 为什么"画出来。
        return ok({"connected": ctx.mouse.mc is not None and ctx.mouse.mc.is_connected,
                   "available": False, "note": _lesson_gone_msg})
    if ctx.mouse.mc is None or not ctx.mouse.mc.is_connected:
        return ok({"connected": False})
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(None, ctx.mouse.lesson_status))


async def _lesson_run(request: web.Request) -> web.Response:
    """跑一次课程实验：启动 → 等 DONE → **实验期间本地捕获统一帧** → 自动算参。

    ⚠️ 2.4.0 起这条路对上游不存在了 ⇒ **409 明确拒绝**（不返回空壳结果）。
    """
    ctx = request.app["ctx"]
    _need_mouse(ctx)
    if not _lesson_available(ctx):
        raise ApiError(409, _lesson_gone_msg)
    d = await body_json(request, required=True)
    if "exp" not in d:
        raise ApiError(400, "要给 exp（实验号）。可用的见 GET /api/v1/lesson/list")
    exp = int(d["exp"])
    exp_args = {k: v for k, v in (d.get("args") or {}).items()}
    run_opts = {}
    for k in ("capture", "sample_hz"):
        if k in d:
            run_opts[k] = d[k]
    # 只把该实验接受的形参喂进去 —— 上游的 pack() 对未知键会抛，早报早好
    spec = {e["no"]: e for e in ctx.mouse.lesson_exps()}.get(exp)
    if spec:
        allowed = set(spec["args"]) | {"capture", "sample_hz"}
        bad = sorted(set(exp_args) - allowed)
        if bad:
            raise ApiError(400, f"实验 {exp}（{spec['name']}）不接受这些参数：{bad}；"
                                f"它接受 {spec['args']}（默认 {spec['defaults']}）")
    loop = asyncio.get_running_loop()
    cb = _lab_progress(ctx, f"lesson{exp}")
    smp = _lab_sample(ctx, f"lesson{exp}")

    def _plan(ms: int) -> None:
        ctx.bus.publish("lesson.plan", {"exp": exp, "budget_ms": ms})

    res = await loop.run_in_executor(
        None, lambda: ctx.mouse.lesson_run(exp, on_progress=cb, on_sample=smp,
                                           on_timeout_plan=_plan, **run_opts, **exp_args))
    return ok(res)


async def _lesson_stop(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    _need_mouse(ctx)
    if not _lesson_available(ctx):
        raise ApiError(409, _lesson_gone_msg)
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(None, ctx.mouse.lesson_stop))


async def _lesson_log(request: web.Request) -> web.Response:
    """旧接口。2.0.0 没有"事后回传课程日志"这条通道 —— 它会**明确报错**，
    而不是返回一个空数组假装成功（空数组会被界面画成"零个点"）。
    2.4.0 更进一步：连课程层本身都没了。"""
    ctx = request.app["ctx"]
    _need_mouse(ctx)
    if not _lesson_available(ctx):
        raise ApiError(409, _lesson_gone_msg)
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(None, ctx.mouse.lesson_fetch_log))


async def _lesson_suggest(request: web.Request) -> web.Response:
    """**自动算参**：给实测真值，算出该写回的系数（纯计算，不改设备）。

    ⚠️ **2.4.0 之后这条仍然可用，而且更要紧**：它算的是 `TRACKL`/`TRACKR`/`GYRORATIO`，
    **全部属生产标定参数**（走 `mc.params`），与课程层毫无关系 —— 上游把算参函数
    从 `lesson.py` 专门迁到 `api/calc.py` 就是为了保住这条能力。
    所以这里**不判课程层在不在**，只判算参纯函数在不在。
    """
    ctx = request.app["ctx"]
    _need_mouse(ctx)
    d = await body_json(request, required=True)
    if "exp" not in d or "truth" not in d:
        raise ApiError(400, "要给 exp 与 truth（实测真值：实验2 = 实际走了多少 mm，"
                            "实验5 = 实际转了多少度）")
    res = d.get("result") or {}
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(
        None, lambda: ctx.mouse.lesson_suggest(int(d["exp"]), res, float(d["truth"]))))


async def _lesson_apply(request: web.Request) -> web.Response:
    """把算出的建议值写回设备。**按参数所在组自动选通道**。

    ⚠️ 与 `/lesson/suggest` 同理：算参算出来的全是**生产参数**（TRACK*/GYRORATIO），
    走 `mc.params` ⇒ **2.4.0 之后照常可用**，不判课程层在不在。
    历史注：上游 CHANGELOG 曾专门提过"`mc.lesson.set_params()` 只接受课程命名空间" ——
    那正是为什么本层这里走的是 `mc.params`。
    """
    ctx = request.app["ctx"]
    _need_mouse(ctx)
    d = await body_json(request, required=True)
    sug = d.get("suggestion")
    if not isinstance(sug, dict) or not sug.get("items"):
        raise ApiError(400, "要给 suggestion（就是 /lesson/suggest 返回的那个对象）")
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(
        None, lambda: ctx.mouse.lesson_apply(sug, save=bool(d.get("save", False)))))


# ================================================================ 视频复盘（改版二十四）

async def _video_list(request: web.Request) -> web.Response:
    """列 `video.root` 下的视频。扫盘 ⇒ 进 executor（铁律 3）。"""
    ctx = request.app["ctx"]
    loop = asyncio.get_running_loop()
    items = await loop.run_in_executor(None, list_videos, ctx.video.root)
    return ok({"root": str(ctx.video.root), "videos": items})


async def _video_sessions(request: web.Request) -> web.Response:
    """列已有的会话产物（`video.out_root`）。扫盘 ⇒ executor。"""
    ctx = request.app["ctx"]
    loop = asyncio.get_running_loop()
    items = await loop.run_in_executor(None, list_sessions, ctx.video.out_root)
    return ok({"root": str(ctx.video.out_root), "sessions": items})


async def _video_status(request: web.Request) -> web.Response:
    """**零 IO**：只读内存里的那一份任务状态（界面每秒轮询它）。"""
    return ok(request.app["ctx"].video.status())


async def _video_run(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    d = await body_json(request, required=True)

    def _opt_f(key: str, label: str) -> Optional[float]:
        """可选数字。给了就必须是数 —— 空串/None 当"没给"，**不猜 0**。

        ⚠️ `float("")` 会抛 ValueError，`float("abc")` 也是；两者都要变成
        能看懂的一句"X 必须是数字（秒）"，而不是 500 里的英文 traceback。
        """
        v = d.get(key)
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            raise ApiError(400, f"{label} 必须是数字（秒）：{v!r}") from None

    stride = d.get("stride")
    if stride in (None, ""):
        stride = None
    else:
        try:
            stride = max(1, int(stride))
        except (TypeError, ValueError):
            raise ApiError(400, f"stride 必须是整数：{stride!r}") from None

    job = ctx.video.run(
        str(d.get("source") or ""),
        start_s=_opt_f("start_s", "start_s"),
        end_s=_opt_f("end_s", "end_s"),
        stride=stride,
        maze=(None if d.get("maze") is None else bool(d.get("maze"))),
    )
    return ok(job)


async def _video_cancel(request: web.Request) -> web.Response:
    return ok(request.app["ctx"].video.cancel())


async def _video_session(request: web.Request) -> web.Response:
    """读 `session.json` 摘要（含时间轴自洽校验）。读文件 ⇒ executor。"""
    ctx = request.app["ctx"]
    sid = request.match_info["sid"]
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(None, ctx.video.session, sid))


async def _video_trajectory(request: web.Request) -> web.Response:
    """读 `trajectory.csv` → 轨迹点（抽样上限 2000）。读文件 ⇒ executor。"""
    ctx = request.app["ctx"]
    sid = request.match_info["sid"]
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(None, ctx.video.trajectory, sid))


# ================================================================ 会话

async def _session_open(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    d = await body_json(request)
    sid = str(d.get("session_id") or "").strip() or None
    return ok(await ctx.session_open(session_id=sid, notes=d.get("notes")))


async def _session_close(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    # 改版二十四：`wait_delivery_s` / `auto_rerun` 已随相机下线。
    # 仍然**收下并忽略**这两个键（旧脚本/旧判据会带），而不是 400 ——
    # 报 400 会让人以为是"参数写错了"，其实是"这条能力已经不存在了"。
    await body_json(request)
    return ok(await ctx.session_close())


async def _records(request: web.Request) -> web.Response:
    """**比赛记录摘要**（改版二十七）—— 界面「比赛记录」那张表的数据源。

    每行 9 列 + 几个辅助字段，口径逐条写在 `MazeRecorder.summaries()` 的 docstring 里。
    这里只说三件与端点有关的：

    ⚠️ **必须走 `run_in_executor`**：读盘 + 逐份 `json.loads`。默认 `n=50`，
    实际记录平均 ~28 KB、整个目录 1.9 MB ⇒ 50 条约几十毫秒；但记录攒到几百条之后
    就不该再放在事件循环上（这条端点是"点开记录页"触发的，不是心跳）。

    ⚠️ **上限 500**：不设上限的话，一条 `?n=100000` 就能把服务拖死几百毫秒。
    超了不报错，直接按 500 取 —— 但**响应里回实际条数**（`n`），别让调用方以为拿全了。

    ⚠️ `ledger` / `maze_seed` 是**本层口径**、且**可能为 `None`**：
    前者是"我们怎么算账本平"，后者记录里根本没有种子。界面必须能显示"判不了"。
    """
    ctx = request.app["ctx"]
    n = _int(request, "n", 50) or 50
    n = max(1, min(500, n))
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, lambda: ctx.maze_rec.summaries(n))
    return ok({"root": str(ctx.maze_rec.root), "records": rows, "n": len(rows)})


async def _records_delete(request: web.Request) -> web.Response:
    """**删除一条比赛记录**（改版三十二）。**不可逆**。

    `name` 是记录文件名（`*.maze.json`，路由里要 URL 编码）。

    三条纪律：
    * **只删本层产物**：`<名>.maze.json` + `<名>.link.json`。**不碰视频产物** ——
      "记录 → 视频"是单向关联，删记录顺手把视频删了，那是替人做决定。
    * 不存在 ⇒ **404**（`exists()` 先判），不是 409：换个名字就行。
    * 写盘操作走 executor；失败把 `OSError` 的原文报出来（"删不掉"必须能追到原因）。
    """
    ctx = request.app["ctx"]
    name = request.match_info.get("name") or ""
    if not ctx.maze_rec.exists(name):
        raise ApiError(404, f"没有这条记录：{name}（刷新一下列表，可能已经被删了）。")
    loop = asyncio.get_running_loop()
    try:
        out = await loop.run_in_executor(None, lambda: ctx.maze_rec.delete(name))
    except ValueError as e:
        raise ApiError(409, f"删不掉：{e}")
    ctx.store.mark("control", "record.delete", name,
                   json.dumps({"removed": out.get("removed")}, ensure_ascii=False))
    return ok(out)


async def _records_replay(request: web.Request) -> web.Response:
    """**一条记录的回放视图**（改版二十九）—— 「回放」页一次取全，本地播放。

    `name` 是记录文件名（`*.maze.json`，路由里要 URL 编码）。

    为什么一次给全、而不是"拖动时按需取一帧"：
    回放是**离线**看的（人拖轴、4 步/秒），逐帧取会把一次拖动变成几十个请求，
    而且拖回去还要重取。一次取全（默认上限 1200 个样本）之后，播放纯在客户端跑。

    ⚠️ **抽样必须自报**：`sampled` = 每隔多少帧取一个。`sampled > 1` 时
    `samples` 的帧号是**跳着**的，界面若拿"相邻样本 = 相邻帧"去算速度就是错数。

    ⚠️ `params` 可能为 `null`：只有改版二十八之后落的记录才有发车前参数快照。
    老记录**如实给 null**，界面显示「未记录」—— 拿当前参数顶上就是造假。

    读盘 + 逐帧重放建图 ⇒ 走 executor。
    """
    ctx = request.app["ctx"]
    name = request.match_info.get("name") or ""
    # 404 与 409 必须分开：名字不对/文件没了是 404（**换个名字就行**），
    # 文件在但读不出来是 409（**重试也没用，得去看文件**）。混成一个号，
    # 现场就会对着一条打不开的记录反复点。
    if not ctx.maze_rec.exists(name):
        raise ApiError(404, f"没有这条记录：{name}。"
                            f"（记录目录 {ctx.maze_rec.root}；到「比赛记录」页刷新看看有哪些。）")
    loop = asyncio.get_running_loop()

    def _load() -> dict:
        return ctx.maze_rec.replay_view(name)

    try:
        data = await loop.run_in_executor(None, _load)
    except (ValueError, OSError, json.JSONDecodeError) as e:
        raise ApiError(409, f"这条记录读不出来：{type(e).__name__}: {e}")
    return ok(data)


async def _records_link(request: web.Request) -> web.Response:
    """**建立 / 解除「记录 ↔ 视频会话」关联**（改版二十七）。

    body：`{"video_sid": "<视频会话 id>"}`；解除关联给 `{"video_sid": null}`。
    `name` 是记录文件名（`*.maze.json`，路由里要 URL 编码）。

    校验次序：**400（body 错）→ 404（对象不存在）→ 写**。
    * `video_sid` 不是"可选"：不带这个键就是**没说清要干什么**，不能当成"解除" ——
      那样一次手滑的空 POST 就会把已有关联删掉。
    * `video_sid` 指向的会话目录必须**真的存在**：关联到不存在的会话，
      界面上就是一条点不开、也删不掉的死链。

    关联落点：`records/maze/<记录名>.link.json`（**不写回 `.maze.json` 本体** ——
    那份是产物，要和上游/别的工具对得上；"关联哪个视频"是本层的判断）。
    同步写小文件 ⇒ 走 executor。
    """
    ctx = request.app["ctx"]
    name = request.match_info.get("name") or ""
    d = await body_json(request)
    if "video_sid" not in d:
        raise ApiError(400, '要指定 video_sid；**解除关联请显式给 null**'
                            '（不带这个键不会被当成"解除"）。')
    sid = d.get("video_sid")
    if sid:
        sid = str(sid).strip()
        out_root = Path(str(ctx.cfg.get("video.out_root") or ""))
        vd = out_root / sid
        # 判"是不是一个**真会话**"，判据与 `list_sessions()` 完全一致：
        # 光有目录不算 —— 一个空目录（解析到一半被杀、或人手建的）关联上去，
        # 界面上就是一条点开没内容、也没法删的死链。
        if not vd.is_dir() or not ((vd / "session.json").is_file()
                                   or (vd / "trajectory.csv").is_file()):
            raise ApiError(404, f"没有这个视频会话：{sid}。"
                                f"（要求 {vd} 下真有 session.json 或 trajectory.csv；"
                                f"先解析一段视频，或到「视频会话」页刷新列表看看有哪些。）")
    else:
        sid = None
    loop = asyncio.get_running_loop()

    def _do() -> dict:
        try:
            return ctx.maze_rec.write_link(name, sid)
        except ValueError as e:                     # 记录名不合法/不存在
            raise ApiError(404, str(e)) from e

    r = await loop.run_in_executor(None, _do)
    ctx.store.mark("control", "records.link", r.get("name", name),
                   f"video_sid={sid}")
    return ok(r)


async def _session_get(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    s = ctx.store.current
    return ok({"open": ctx.store.is_open,
               "session": s.to_dict() if s else None,
               "half_open": ctx.store.abort_if_half_open()})


async def _sessions(request: web.Request) -> web.Response:
    """会话列表。

    ⚠️ 必须走线程池：`store.list_sessions()` 对**每个**会话目录读 `meta.json`、
    **逐行数** `timeline.csv`、再递归算整个目录的占用。这是同步的文件系统遍历，
    而它跑在事件循环上 —— 会话一多就是整服务卡住，而本端点还在页面启动路径上
    （⑨数据页与底栏都要它）。同文件的 `/session/export`、`/camera/frames` 都走线程池。
    """
    ctx = request.app["ctx"]
    return ok(await asyncio.get_running_loop().run_in_executor(
        None, ctx.store.list_sessions))


async def _session_timeline(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    return ok(ctx.store.read_timeline(session_id=request.query.get("session_id"),
                                      since_ms=_int(request, "since", 0) or None,
                                      limit=_int(request, "limit", 20000)))


async def _session_export(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    sid = request.query.get("session_id") or (
        ctx.store.current.session_id if ctx.store.current else "")
    kind = request.query.get("kind") or "zip"
    if not sid:
        raise ApiError(400, "要指定 session_id")
    loop = asyncio.get_running_loop()
    p = await loop.run_in_executor(None, lambda: ctx.store.export(str(sid), str(kind)))
    rel = p.name if p.parent == ctx.cfg.session_root else str(p)
    return ok({"file": str(p), "name": p.name,
               "download": f"/api/v1/session/file?name={p.name}"})


async def _session_file(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    name = request.query.get("name") or ""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ApiError(400, "name 不合法（只接受会话根目录下的单个文件名）")
    p = (ctx.cfg.session_root / name).resolve()
    if p.parent != ctx.cfg.session_root.resolve() or not p.is_file():
        raise ApiError(404, f"没有这个导出文件：{name}")
    return web.FileResponse(p)

# ⚠️ 改版二十四：`GET /api/v1/arena` 已删。它读的是 `ctx.last_loc`（在线定位的最新结果，
# 随相机下线）与固件位置做 Δ 对比 —— 两路里有一路已经不存在，这个端点的语义就没了。
# `ctx.arena` 本身留着：8×8 场地的几何仍是**画轨迹的背景网格**（`/health`、`/caps` 里那份）。

