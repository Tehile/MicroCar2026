"""REST 路由表（设计方案 §6.2 的逐条落地）。

原则：
  · 全部绑 `127.0.0.1`（本机自用）。要开 `0.0.0.0` 必须先补鉴权 —— 不在这里偷偷开。
  · **token 只在服务端**：前端拿到的相机地址永远是 `/api/v1/camera/mjpeg`，
    真 token 由本进程在反代时补上。
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
from ..arena import CalibObservation, solve_flips
from ..clock import anchor_snapshot, mono_to_ms, now_ms
from ..decode import decode_y
from ..store import new_session_id
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
    r.add_post("/api/v1/clock/autofit", _clock_autofit)

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
    r.add_get("/api/v1/mouse/maze", _mouse_maze)
    # 迷宫（实验 8 无记忆走迷宫，上游 2.0.0）。**状态从统一帧读**，所以只剩
    # "读状态 + 读迁移序列 + 汇总 + 清本层差分缓冲"四条读的路，**没有事件流端点**。
    r.add_get("/api/v1/mouse/maze/state", _maze_state)
    r.add_get("/api/v1/mouse/maze/history", _maze_history)
    r.add_get("/api/v1/mouse/maze/summary", _maze_summary)
    r.add_post("/api/v1/mouse/maze/reset", _maze_reset)
    # 上游 2.1.1 新能力：**收车**（`MAZE STOP`）。v2.0 时代"发过一次车就只能断电"，
    # 现在有一个明确的收车口 —— 但它**车在运动时不可靠**，所以旁边还要有【复位】。
    r.add_post("/api/v1/mouse/maze/stop", _maze_stop)
    # 上游 2.2.0 新能力：**复位**（整机软复位 `MCU RESET`）。固件主循环有 30 余处
    # 非协作式阻塞等待，`STOP` 会让它们永远等不到 ⇒ "越停越死"，软复位是唯一出口。
    r.add_post("/api/v1/mouse/rescue", _mouse_rescue)
    # 发车前**低电预检**（纯读帧、不发命令）：上游 2.2.1 有硬闸，这里只是让它**提前可见**。
    r.add_get("/api/v1/mouse/launch_precheck", _mouse_launch_precheck)
    # 实验 8 的「从发车到停止」记录与复盘（详见 control/maze_rec.py 的模块 docstring）
    r.add_get("/api/v1/mouse/maze/reconstruct", _maze_reconstruct)
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
    # ⚠️ 静态路径必须**先**注册：aiohttp 按注册顺序匹配，{item} 会吞掉 "report"
    r.add_post("/api/v1/mouse/calib/report", _mouse_calib_report)
    r.add_post("/api/v1/mouse/calib/{item}", _mouse_calib)
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

    # ---- 相机
    r.add_get("/api/v1/camera/status", _cam_status)
    r.add_get("/api/v1/camera/ledger", _cam_ledger)
    r.add_get("/api/v1/camera/sessions", _cam_sessions)
    r.add_get("/api/v1/camera/overview", _cam_overview)
    r.add_get("/api/v1/camera/pair", _cam_pair)
    r.add_get("/api/v1/camera/mjpeg", _cam_mjpeg)
    r.add_get("/api/v1/camera/thumb", _cam_thumb)
    r.add_get("/api/v1/camera/frames", _cam_frames)
    r.add_get("/api/v1/camera/offline/sessions", _cam_offline_sessions)
    r.add_post("/api/v1/camera/start", _cam_start)
    r.add_post("/api/v1/camera/probe", _cam_probe)
    r.add_post("/api/v1/camera/connect", _cam_connect)
    r.add_post("/api/v1/camera/stop", _cam_stop)
    r.add_post("/api/v1/camera/base", _cam_base)
    # 上游程序：设置（可存）+ 代启 / 停止（都由人点，后台探测绝不动手）
    r.add_post("/api/v1/camera/settings", _cam_settings)
    r.add_get("/api/v1/camera/service", _cam_service_get)
    r.add_post("/api/v1/camera/service/start", _cam_service_start)
    r.add_post("/api/v1/camera/service/stop", _cam_service_stop)
    r.add_post("/api/v1/camera/service/pause", _cam_service_pause)
    r.add_post("/api/v1/camera/import", _cam_import)
    r.add_post("/api/v1/camera/exit_offline", _cam_exit_offline)

    # ---- 定位
    r.add_get("/api/v1/locate/catalog", _loc_catalog)
    r.add_get("/api/v1/locate/params", _loc_params_get)
    r.add_post("/api/v1/locate/params", _loc_params_set)
    r.add_get("/api/v1/locate/profiles", _loc_profiles)
    r.add_post("/api/v1/locate/profile", _loc_profile)
    r.add_get("/api/v1/locate/preview", _loc_preview)
    r.add_get("/api/v1/locate/annotated/{seq}.jpg", _loc_annotated)
    r.add_get("/api/v1/locate/results", _loc_results)
    r.add_get("/api/v1/locate/track", _loc_track)
    r.add_get("/api/v1/locate/runs", _loc_runs)
    r.add_post("/api/v1/locate/run", _loc_run)
    r.add_get("/api/v1/locate/run/{rid}", _loc_run_status)
    r.add_post("/api/v1/locate/run/{rid}/abort", _loc_run_abort)
    r.add_post("/api/v1/locate/online", _loc_online)

    # ---- 会话 / 数据
    r.add_post("/api/v1/session/open", _session_open)
    r.add_post("/api/v1/session/close", _session_close)
    r.add_get("/api/v1/session", _session_get)
    r.add_get("/api/v1/sessions", _sessions)
    r.add_get("/api/v1/session/timeline", _session_timeline)
    r.add_post("/api/v1/session/note", _session_note)
    r.add_get("/api/v1/session/export", _session_export)
    r.add_get("/api/v1/session/file", _session_file)

    # ---- 场地
    r.add_get("/api/v1/arena", _arena_get)
    r.add_post("/api/v1/arena/observations", _arena_obs_post)
    r.add_get("/api/v1/arena/observations", _arena_obs_get)
    r.add_post("/api/v1/arena/solve", _arena_solve)
    r.add_post("/api/v1/arena/calibrate", _arena_calibrate)

    r.add_get("/ws", ws_handler)


# ================================================================ 通用

async def _health(request: web.Request) -> web.Response:
    # 判据 I2：三路全挂也必须 200，且每路都有**可读的** detail
    return ok(request.app["ctx"].health())


async def _caps(request: web.Request) -> web.Response:
    return ok(request.app["ctx"].caps())


async def _config(request: web.Request) -> web.Response:
    cfg = request.app["ctx"].cfg
    raw = cfg.raw()
    # 凭据不进前端（判据 R10）—— 这里返回的是"有没有配"，不是"配的是什么"
    if "camera" in raw:
        raw["camera"]["token"] = "***（服务端持有，前端不显示）" if raw["camera"].get("token") else ""
    return ok(raw)


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


async def _clock_autofit(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    d = await body_json(request)
    sid = d.get("session_id") or (ctx.camera.offline or {}).get("session_id")
    frames = ctx.camera.frames_of(str(sid)) if sid else []
    pairs = []
    for f in frames:
        wc = f.get("wall_clock_ns")
        if not wc:
            continue
        try:
            st = Path(str(f["json"])).stat()
        except OSError:
            continue
        pairs.append((int(st.st_mtime * 1000), float(wc) / 1e6))
    if len(pairs) < 20:
        raise ApiError(409,
                       f"可用帧只有 {len(pairs)} 帧，需 ≥20 且带 wall_clock_ns")
    snap = ctx.clock.fit_min_latency(pairs)
    ctx.save_clock()
    return ok({**snap, "pairs": len(pairs),
               "caveat": "以文件 mtime 作到达时刻：偏上界，非无偏估计"})


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
    out: Dict[str, Any] = {"health": ctx.mouse.health()}
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
    但**两边都没有"可下发的速率"**，所以它仍然只接受 `on`（或省略 = 取反），
    带 `hz` 会明确报错，免得现场以为"降速了"其实什么都没发生。

    ⚠️ 关掉它不只是"停止显示"：v2.1 的 `off()` **立刻丢弃在途请求**，
    这是让蓝牙透传模块缓冲排空的唯一手段（2.0.0 的自时钟曾把那个缓冲灌死、
    且**不会自愈**，必须断电）。所以这个按钮在真机上是**排障手段**，不是省流量开关。
    """
    ctx = request.app["ctx"]
    mc = _need_mouse(ctx)
    d = await body_json(request)
    loop = asyncio.get_running_loop()
    if "hz" in d:
        raise ApiError(400, "没有可下发的速率（`RATE` 已删除）。"
                            "2.1.0 起遥测由上位机逐条拉取，帧率由链路往返决定；"
                            "只支持 {\"on\": true|false}。")
    if "on" in d:
        r = await loop.run_in_executor(None, lambda: mc.stream.toggle(bool(d["on"])))
    else:
        r = await loop.run_in_executor(None, mc.stream.toggle, not mc.stream.is_on)
    ctx.store.mark("mouse", "mouse.stream", "", f"on={mc.stream.is_on} ok={r.ok}")
    # `hz` 回的是**实测**帧率（上游 2.1 起 `telemetry.hz()` 已按到达间隔实算）；
    # 同时把拉取器自己的状态一并端出去（在途数 / 模式 / 丢了几条）——
    # 真机排障时"在途恒 1"是判断停等有没有被破坏的直接证据。
    return ok({"result": _res(r), "on": bool(mc.stream.is_on),
               "hz": mc.telemetry.hz(),
               "pull": _pull_stats(mc)})


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
    return ok(await loop.run_in_executor(None,
                                         lambda: ctx.mouse.maze_reconstruct(sec)))


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
        run = int(task) >= 2             # 2=SEARCH 起才算"在跑"（0 WAIT / 1 START 是待命）
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
    mazetype = d.get("mazetype")
    out = ctx.maze_rec.start(why=why, mazetype=mazetype)
    if out.get("ok"):
        ctx.store.mark("mouse", "maze.record.start", "", f"why={why}")
    return ok(out)


async def _maze_record_stop(request: web.Request) -> web.Response:
    """停止并保存（**人工兜底**）。

    为什么必须有这个按钮：固件**没有"迷宫结束"事件**（`MAZE RUN` 之后就一直处于
    迷宫态）⇒ 自动收尾那几个判据都是**弱判据**，只有跑完那一刻碰巧命中。
    没有人工兜底，模拟器上根本收不了尾。

    ⚠️ 2.1.1 起"收尾"这件事**多了两个正经动作**（本题只负责存盘，不负责动车）：
      · 【收车】`MAZE STOP` —— 停车 + 复位发车状态 + 解锁急停（**车在运动时不可靠**）；
      · 【复位】`MCU RESET` —— 整机软复位（可靠，但会把车的位置假设也清掉）。
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

    ⚠️ 但它**车在运动时不可靠**（`linkMazeAbort` 标志的检查在 while(1) 顶部，
    而主循环有 30 余处非协作式阻塞等待，运动时程序正卡在那些循环内部）⇒
    真机实测：回 `OK` 但 `mazeTask` 仍 `SEARCH`。
    ⇒ 所以应答里**带实测状态**（`state` / `reliable`），让界面当场判"到底收住没有"；
    界面据此提示"没用就用【复位】"。**不许只看 `OK` 就宣布收车成功。**
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


async def _mouse_calib(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    mc = _need_mouse(ctx)
    item = str(request.match_info["item"])
    d = await body_json(request)
    loop = asyncio.get_running_loop()
    acts = {"gyro": mc.calib.gyro, "wall_distance": mc.calib.wall_distance,
            "ir_data": lambda **kw: mc.calib.ir_data(**kw),
            "angle": lambda **kw: mc.calib.angle(**kw),
            "track": lambda **kw: mc.calib.track(**kw),
            "step": lambda **kw: mc.calib.step(item, str(d.get("act") or "START"), **kw)}
    fn = acts.get(item)
    if fn is None:
        raise ApiError(404, f"没有这个标定项 {item!r}。可选：{sorted(acts)}（+ step）")
    kwargs = {k: v for k, v in d.items() if k not in ("act",)}
    try:
        r = await loop.run_in_executor(None, lambda: fn(**kwargs))
    except TypeError as e:
        raise ApiError(400, f"标定项 {item} 的参数不对：{e}") from e
    if isinstance(r, list):
        out = [_res(x) for x in r]
    elif hasattr(r, "ok"):
        out = _res(r)
    else:
        out = r
    ctx.store.mark("mouse", f"calib.{item}", "", json.dumps(d, ensure_ascii=False))
    return ok({"result": out, "items": mc.calib.items(),
               "note": "结果写入会话的 mouse/calib/report.md"})


async def _mouse_calib_report(request: web.Request) -> web.Response:
    """把标定报告写进会话的 `mouse/calib/report.md`（上游 `calib.report`）。"""
    ctx = request.app["ctx"]
    mc = _need_mouse(ctx)
    d = await body_json(request, required=True)
    lines = d.get("lines")
    if isinstance(lines, str):
        lines = [x for x in lines.splitlines() if x.strip()]
    if not lines:
        raise ApiError(400, "lines 是空的")
    p = await asyncio.get_running_loop().run_in_executor(
        None, lambda: mc.calib.report(list(lines)))
    ctx.store.mark("mouse", "calib.report", "", f"{len(lines)} 行 → {p}")
    return ok({"path": p or None,
               "note": "报告落在 mouse/calib/report.md"})


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
    rec_note = ""
    #: 这一次调用**新开了一段记录吗**（`start()` 的 `fresh_epoch`）。
    #: 它是下面"发车被拒时怎么收场"的唯一依据：只有新开的那一段才该被丢掉。
    fresh = False
    try:
        out = ctx.maze_rec.start(why="maze_run",
                                 mazetype=(ctx.mouse.mc.maze.mazetype()
                                           if ctx.mouse.mc is not None else None))
        if out.get("already"):
            rec_note = "（已在记录中，沿用上一段）"
        else:
            fresh = bool(out.get("fresh_epoch"))
            ctx.store.mark("mouse", "maze.record.start", "", "why=maze_run")
    except Exception as e:                                    # noqa: BLE001
        rec_note = f"（记录未能开始：{type(e).__name__}）"
        ctx.bus.log("warn", f"实验 8 记录没能开始：{type(e).__name__}: {e}")
    try:
        res = await loop.run_in_executor(None, lambda: ctx.mouse.maze_run(target))
    except Exception:
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
    return ok({**res, "precheck": precheck,
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


# ================================================================ 相机

async def _cam_status(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(None, ctx.camera.status))


async def _cam_probe(request: web.Request) -> web.Response:
    """同步强制探一次相机（配对页/现场排查）。

    为什么要有它：`/health` 为了让判据 I12 成立，只读后台维护的快照，可能落后若干秒。
    用户按"重探"时他要的是**此刻**的真相，所以这里显式走一次阻塞探测 —— 放在
    executor 里，不占事件循环。
    """
    ctx = request.app["ctx"]
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(None, ctx.camera.probe_now))


async def _cam_connect(request: web.Request) -> web.Response:
    """**用户主动点的「连接相机」**：同步确认 + 回一份"确认单"。

    与 `/camera/probe` 的区别：probe 只回答"通不通"，这里要回答
    **"连上的是谁、手机在不在"** —— 也就是"手动连接后确认状态"这句话里
    "确认"两个字对应的东西。`base` 传了就顺手切过去（现场 IP 会变，
    改地址和连一次本来就是同一个动作的两半）。

    连不上抛 `CameraError` ⇒ 503，附一段可以照着做的排查清单；
    **绝不返回一个看起来成功的空壳**。
    """
    ctx = request.app["ctx"]
    d = await body_json(request, required=False)
    base = str((d or {}).get("base") or "").strip() or None
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(None, lambda: ctx.camera.connect(base)))


async def _cam_ledger(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    sid = request.query.get("session_id") or ""
    loop = asyncio.get_running_loop()
    led = await loop.run_in_executor(None, lambda: ctx.camera.ledger(sid))
    # 三态原样透出（判据 I11）：complete 为 null 时**必须**能被前端判成"判不了"
    led["_complete_meaning"] = (
        {True: "通过", False: "不通过", None: "判不了（App 未宣告应有帧数）"}
        .get(led.get("complete"), "未知"))
    return ok(led)


async def _cam_sessions(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(None, ctx.camera.sessions))


async def _cam_overview(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(None, ctx.camera.overview))


async def _cam_pair(request: web.Request) -> web.Response:
    """⚠️ 响应**含 token**。只在配对页显式请求时返回，且**不写日志**（R10）。"""
    ctx = request.app["ctx"]
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, ctx.camera.pair)
    return ok({**data, "_warning": "本响应含 token（等同凭据）：勿投屏、勿外传"})


async def _cam_mjpeg(request: web.Request) -> web.StreamResponse:
    """反向代理 MJPEG。**前端永远看不到 token**；而且这条流只进 `<img>`，
    绝不参与任何数值路径（R4：看得见 ≠ 算得准）。

    ⚠️ **`Content-Type` 必须原样透传上游的**（含它自己的 `boundary`）。
    真踩过：这里原先写死 `boundary=frame`，而上游是 `boundary=phonecamera-frame`
    —— 声明与实际不符，浏览器按 `--frame` 去切分、流里却是 `--phonecamera-frame`，
    **一帧都切不出来** ⇒ 预览窗口永远空白，而服务端一切正常（手机在发、反代在转发）。
    boundary 是**上游对它的流说了算**，本层不猜：猜错的代价是一整条链路看着通、却没画面。
    """
    ctx = request.app["ctx"]
    url = ctx.camera.mjpeg_url()
    if not url:
        raise ApiError(503, "拿不到 MJPEG 地址 —— 相机服务没在跑，或已在离线模式。"
                            "离线模式下没有实时画面，请用【数据】页看历史帧。")
    session = request.app["http"]
    try:
        upstream = await session.get(
            url, timeout=aiohttp.ClientTimeout(total=None, sock_read=30))
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        ctx.bus.log("warn", f"MJPEG 连不上上游：{type(e).__name__}: {e}")
        raise ApiError(502, f"连不上相机服务 {url.split('?')[0]}：{type(e).__name__}") from e

    async with upstream:
        if upstream.status != 200:
            raise ApiError(502, f"相机服务回 {upstream.status}："
                                f"{(await upstream.text())[:200]}")
        ct = upstream.headers.get("Content-Type") or "multipart/x-mixed-replace"
        resp = web.StreamResponse(status=200, headers={
            "Content-Type": ct,
            # 不缓存：否则中间层会把第一帧缓存住，画面永远冻在第一张（上游同一条理由）
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        })
        await resp.prepare(request)
        try:
            while True:
                try:
                    chunk = await upstream.content.readany()
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    # 读**上游**失败 = 真故障，值得记一条
                    ctx.bus.log("warn", f"MJPEG 上游读取中断：{type(e).__name__}: {e}")
                    break
                if not chunk:
                    break
                try:
                    await resp.write(chunk)
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                        *((_CCRE,) if _CCRE else ())) as e:
                    # 写**浏览器**失败 = 它把这一路关了（切页 / 刷新 / 停止预览）。
                    # **这是正常事件**，不能记 warn：现场切一次页面就多一条"故障"，
                    # 真故障会被自己的噪声淹掉（原先就是这行把日志刷成了两条 warn）。
                    ctx.bus.log("info", f"MJPEG 订阅者断开（{type(e).__name__}）")
                    break
        except asyncio.CancelledError:
            raise
        finally:
            try:
                await resp.write_eof()
            except Exception:                                    # noqa: BLE001
                pass
    return resp


async def _cam_thumb(request: web.Request) -> web.StreamResponse:
    ctx = request.app["ctx"]
    sid = request.query.get("session_id") or ""
    url = ctx.camera.thumb_url(sid)
    if not url:
        raise ApiError(503, "拿不到缩略图地址（相机服务不可用）")
    session = request.app["http"]
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
        if r.status != 200:
            raise ApiError(502, f"相机服务回 {r.status}")
        return web.Response(body=await r.read(),
                            content_type=r.headers.get("Content-Type", "image/jpeg"),
                            headers={"Cache-Control": "no-store"})


async def _cam_frames(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    sid = request.query.get("session_id")
    if not sid and ctx.store.current and ctx.store.current.camera_ref:
        sid = ctx.store.current.camera_ref.get("session_id")
    if not sid and ctx.camera.offline:
        sid = ctx.camera.offline.get("session_id")
    if not sid:
        raise ApiError(400, "要给 session_id（或先开一个会话 / 导入一个离线会话）")
    limit = _int(request, "limit", 0)
    loop = asyncio.get_running_loop()
    frames = await loop.run_in_executor(None, lambda: ctx.camera.frames_of(str(sid), limit))
    return ok({"session_id": sid, "count": len(frames), "frames": frames})


async def _cam_offline_sessions(request: web.Request) -> web.Response:
    """列本地已有的手机会话（无手机时的演练素材 —— 已经有 14 个真实会话）。

    ⚠️ 整段扫盘**必须在线程池里跑**。这不是洁癖：`frames/` 下是成千上万个
    `frame_*.json`，`glob` + 递归算占用是纯同步的文件系统遍历，而它以前直接跑在
    aiohttp 的事件循环上 —— 实测线程栈连续两个快照都停在 `pathlib.glob`（≥2 s），
    期间**整个服务不回答任何请求**（连纯内存的 `/health` 都超时），浏览器那边表现成
    "页面一直卡在启动中"。同文件里的 `/camera/frames` 本来就是这么做的。
    """
    ctx = request.app["ctx"]
    root = Path(str(ctx.cfg.get("camera.offline_import_root")))
    if not root.is_dir():
        return ok({"root": str(root), "sessions": [],
                   "note": f"目录不存在：{root}"})
    out = await asyncio.get_running_loop().run_in_executor(
        None, lambda: _scan_offline_sessions(root))
    return ok({"root": str(root), "sessions": out, "current": ctx.camera.offline})


def _scan_offline_sessions(root: Path) -> List[dict]:
    """同步扫盘：**只能在事件循环之外调用**（理由见 `_cam_offline_sessions`）。"""
    out: List[dict] = []
    for d in sorted(root.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        fdir = d / "frames"
        n = len(list(fdir.glob("frame_*.json"))) if fdir.is_dir() else 0
        man = d / "manifest.json"
        item: Dict[str, Any] = {"session_id": d.name, "frames": n,
                                "path": str(d), "bytes": _dir_bytes(d)}
        if man.is_file():
            try:
                m = json.loads(man.read_text(encoding="utf-8"))
                item["state"] = m.get("state")
                item["profile_id"] = m.get("profile_id")
                item["device_serial"] = m.get("device_serial")
            except (OSError, json.JSONDecodeError):
                pass
        out.append(item)
    return out


async def _cam_start(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    d = await body_json(request)
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, lambda: ctx.camera.start(
        session_id=str(d.get("session_id") or ""),
        device_serial=str(d.get("device_serial") or "unknown"),
        plan=d.get("plan"), capture_mode=str(d.get("capture_mode") or "")))
    return ok(res)


async def _cam_stop(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    d = await body_json(request)
    loop = asyncio.get_running_loop()
    return ok(await loop.run_in_executor(
        None, lambda: ctx.camera.stop(str(d.get("session_id") or ""))))


async def _cam_base(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    d = await body_json(request, required=True)
    base = str(d.get("base") or "").strip()
    if not base.startswith("http"):
        raise ApiError(400, "base 要写成 http://host:port 的形式")
    ctx.camera.set_base(base, d.get("token"))
    return ok({"base": ctx.camera.base, "note": "地址已改（本次运行有效；要长期生效用 /camera/settings）"})


# ---------------------------------------------------------------- 上游程序的设置与代启
# 用户要求：配对时若依赖上游服务，应能在设置里给出上游程序，并由本层代启，
# 不必手动双击 start_service.cmd。
#
# ⚠️ 三处分工必须清楚：
#   `settings`  —— **存**（写回 config.yaml，保留注释）
#   `service`   —— **看**（零网络、零副作用，纯内存）
#   `service/start|stop` —— **动手**（只有人能触发；后台探测与 /health 永不调用）

#: 允许在界面上改的 launcher 键 -> (类型, 说明)。类型用来做**入参校验**：
#: patch 写的是 yaml 文本，写进去一个类型不对的值（比如 bool 写成字符串）
#: 不会当场报错，只会在下次启动时以奇怪的方式失败。
_LAUNCH_KEYS = {
    "script": str, "python": str, "data_dir": str, "token": str,
    "log": str, "host": str,
    "enabled": bool, "autostart": bool, "stop_on_exit": bool,
    "port": int, "max_mbps": float,
}


async def _cam_settings(request: web.Request) -> web.Response:
    """把相机设置**写回 config.yaml**（保留注释）并当场生效。

    为什么需要它：地址和上游程序都属于"现场会变、且下次还要用"的东西。
    原先把地址只改在内存里 —— 重启一次就回到旧值，现场会以为"改了没用"。
    """
    from ..launcher import yaml_scalar
    ctx = request.app["ctx"]
    d = await body_json(request, required=True)
    render: Dict[str, str] = {}
    applied: Dict[str, Any] = {}

    base = d.get("base")
    if base is not None:
        base = str(base).strip()
        if not base.startswith("http"):
            raise ApiError(400, "地址要写成 http://host:port 的形式")
        render["camera.base"] = yaml_scalar(base)
        applied["base"] = base

    for k, v in (d.get("launcher") or {}).items():
        if k not in _LAUNCH_KEYS:
            raise ApiError(400, f"没有这个设置项：launcher.{k}；可选 {sorted(_LAUNCH_KEYS)}")
        want = _LAUNCH_KEYS[k]
        try:
            if want is bool:
                if isinstance(v, str):
                    v = v.strip().lower() in ("1", "true", "yes", "on")
                v = bool(v)
            elif want is int:
                v = int(v)
            elif want is float:
                v = float(v)
            else:
                v = str(v if v is not None else "")
        except (TypeError, ValueError):
            raise ApiError(400, f"launcher.{k} 的类型不对（要 {want.__name__}）") from None
        render[f"camera.launcher.{k}"] = yaml_scalar(v)
        applied[f"launcher.{k}"] = v

    if not render:
        raise ApiError(400, "没有任何要改的设置（base 或 launcher.*）")

    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, lambda: ctx.cfg.patch(render))
    not_found = [k for k, v in res.items() if v != "patched"]
    if not_found:
        # 目标行不存在（旧配置没这一段）：整份重写会**丢掉全部注释**，所以只告警不硬来。
        # 让用户自己去 config.yaml 补上那几行 —— 比我们悄悄抹掉文档便宜得多。
        ctx.bus.log("warn", f"设置没能写回（config.yaml 里没有这些行）：{not_found}")
    if "base" in applied:
        ctx.camera.set_base(str(applied["base"]))
    ctx.launcher.reload()          # 改完立刻按新参数算（下一次启动就用它）
    ctx.store.mark("camera", "camera.settings", ",".join(sorted(applied)),
                   f"written={len(render) - len(not_found)}/{len(render)}")
    return ok({"applied": applied, "written": render.keys(),
               "not_written": not_found,
               "note": ("已写入 config.yaml" if not not_found
                        else "config.yaml 里缺少对应行，尚未落盘（其余项已生效）")})


async def _cam_service_get(request: web.Request) -> web.Response:
    """代启器现状 + 启动行。**零网络、零副作用** —— 界面刷新随便调。"""
    ctx = request.app["ctx"]
    loop = asyncio.get_running_loop()
    st = await loop.run_in_executor(None, lambda: ctx.launcher.status(deep=True))
    return ok(st)


def _cam_is_up(ctx: Any):
    """给代启器用的"此刻通不通" —— **必须真探**，不能拿 health 快照。

    快照最多旧 8 s：拿它做闸会在"其实已经起来了"的时候再起一个进程。
    """
    def _f() -> bool:
        try:
            ctx.camera.capabilities(refresh=True)
            return True
        except Exception:                                     # noqa: BLE001
            return False
    return _f


async def _cam_service_start(request: web.Request) -> web.Response:
    """代启上游相机服务，并等到它真能答话（或带着日志尾巴如实报失败）。

    这条路由**只由人触发**。后台探测与 `/health` 绝不调它 ——
    否则一个 2.5 s 刷新的页面会不停拉起进程（最难发现的资源泄漏就是这种）。
    """
    ctx = request.app["ctx"]
    d = await body_json(request)
    wait_s = float(d.get("wait_s") or 0) or 15.0
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(
        None, lambda: ctx.launcher.start(is_up=_cam_is_up(ctx), wait_s=wait_s))
    ctx.camera.kick_probe()        # 起完立刻重探：别让界面还显示"不可达"（快照可能旧 8 s）
    if res.get("ok"):
        return ok(res)
    # 起不来是**故障**：409 + 可照做的清单（与 camera 侧的故障口径一致）
    raise ApiError(409, res.get("detail") or res.get("reason") or "代启上游服务失败")


async def _cam_service_stop(request: web.Request) -> web.Response:
    """停掉**本层起的**上游服务。别处启动的不动（没有它的父进程关系，杀错就是断人现场）。"""
    ctx = request.app["ctx"]
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, ctx.launcher.stop)
    ctx.camera.kick_probe()
    if res.get("ok"):
        return ok(res)
    raise ApiError(409, res.get("detail") or res.get("reason") or "停止上游服务失败")


async def _cam_service_pause(request: web.Request) -> web.Response:
    """**本次运行内**暂停 / 恢复代启（`{"on": true}`）。不写 config.yaml。

    存在的理由（两条，都不是"为测试开的后门"）：
      · 现场调试时"我自己管进程"，不想让 Control 插手；
      · 判据要跑"服务不可达"那条降级路 —— 而代启一旦生效，那条路当场就没了，
        还会在**用户机器上留下一个上游进程**（真发生过：`--no-fake-cam` 那一轮）。

    与 `camera.launcher.enabled`（配置项，长期）分工：那个是"要不要这个能力"，
    这个是"这一次先别动手"。
    """
    ctx = request.app["ctx"]
    d = await body_json(request, required=True)
    if "on" not in d:
        raise ApiError(400, '要写 {"on": true} 或 {"on": false}')
    return ok(ctx.launcher.pause(bool(d["on"])))


async def _cam_import(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    d = await body_json(request, required=True)
    sid = str(d.get("session_id") or "")
    if not sid:
        raise ApiError(400, "要指定 session_id（见 GET /camera/offline/sessions）")
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, lambda: ctx.camera.import_local_session(
        sid, root=d.get("root"), rate_hz=float(d.get("rate_hz") or 30.0),
        replay=bool(d.get("replay", True))))
    if ctx.store.is_open:
        ctx.store.set_camera_ref({"session_id": sid, "data_dir": str(ctx.camera.data_dir()),
                                  "offline": True, "store": None,
                                  "note": "离线导入"})
    return ok(res)


async def _cam_exit_offline(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    ctx.camera.exit_offline()
    return ok({"offline": None})


def _dir_bytes(p: Path) -> int:
    """会话目录的占用（**不含 frames/**：交付帧是 GB 级，单列）。
    用 os.walk 而不是 rglob —— rglob 产出的是 Path，不是 (dirpath, dirnames, files)。"""
    import os
    total = 0
    for dp, _dn, fns in os.walk(p):
        if Path(dp).name == "frames":
            _dn[:] = []                     # 就地清空才会真的剪枝
            continue
        for fn in fns:
            try:
                total += (Path(dp) / fn).stat().st_size
            except OSError:
                pass
    return total


# ================================================================ 定位

async def _loc_catalog(request: web.Request) -> web.Response:
    return ok(request.app["ctx"].locator.catalog())


async def _loc_params_get(request: web.Request) -> web.Response:
    return ok(request.app["ctx"].locator.params())


async def _loc_params_set(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    d = await body_json(request, required=True)
    params = d.get("params") if "params" in d else d
    return ok(ctx.locator.set_params(params))


async def _loc_profiles(request: web.Request) -> web.Response:
    return ok(request.app["ctx"].locator.profiles())


async def _loc_profile(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    d = await body_json(request, required=True)
    name = str(d.get("name") or "")
    if not name:
        raise ApiError(400, "要给 name（见 GET /locate/profiles）")
    return ok(ctx.locator.set_profile(name))


async def _loc_preview(request: web.Request) -> web.Response:
    """对**单帧**实时试解 —— 调参时改一个值马上看这一帧的结果。

    这是把"调参"从"跑一遍全量再对比"变成"改一下看一眼"的关键，
    否则每次试参数都要等几分钟。
    """
    ctx = request.app["ctx"]
    path = _resolve_frame(ctx, request)
    seq = _int(request, "seq", 0)
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, lambda: ctx.locator.submit_path(path, seq=seq))
    annotated = bool(request.query.get("annotate", "1") not in ("0", "false", ""))
    if annotated:
        try:
            jpg, _ = await loop.run_in_executor(None, lambda: ctx.locator.annotate_file(path, seq))
            ctx.last_annotated[seq] = jpg
            if len(ctx.last_annotated) > 40:
                ctx.last_annotated.pop(next(iter(ctx.last_annotated)))
        except Exception as e:                               # noqa: BLE001
            res["_annotate_error"] = f"{type(e).__name__}: {e}"
    res["_has_annotated"] = annotated
    return ok(res)


async def _loc_annotated(request: web.Request) -> web.Response:
    """叠加图（定位器自己画的角标记框 / 场地四边形 / 0 号位姿）。"""
    ctx = request.app["ctx"]
    seq = int(request.match_info["seq"])
    cached = ctx.last_annotated.get(seq)
    if cached is None:
        path = _resolve_frame(ctx, request, seq=seq)
        loop = asyncio.get_running_loop()
        cached, _ = await loop.run_in_executor(None, lambda: ctx.locator.annotate_file(path, seq))
        ctx.last_annotated[seq] = cached
    return web.Response(body=cached, content_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


def _resolve_frame(ctx: Any, request: web.Request, seq: Optional[int] = None) -> Path:
    sid = request.query.get("session_id") or (ctx.camera.offline or {}).get("session_id")
    if not sid and ctx.store.current and ctx.store.current.camera_ref:
        sid = ctx.store.current.camera_ref.get("session_id")
    if not sid:
        raise ApiError(400, "不知道是哪个会话 —— 给 session_id（或先导入一个离线会话）")
    frames = ctx.camera.frames_of(str(sid))
    if not frames:
        raise ApiError(404, f"会话 {sid} 里一帧都没有（或 data_dir 解析不到）")
    want = seq if seq is not None else _int(request, "seq", frames[0]["seq"])
    for f in frames:
        if int(f["seq"]) == int(want):
            return Path(f["path"])
    raise ApiError(404, f"会话 {sid} 里没有 seq={want} 这一帧"
                        f"（该会话共 {len(frames)} 帧）")


async def _loc_results(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    return ok(ctx.locator.results(limit=_int(request, "limit", 200),
                                  offset=_int(request, "offset", 0)))


async def _loc_track(request: web.Request) -> web.Response:
    """轨迹点数组。量小就直接 JSON；要全量请让前端订阅 WS 的 `locate.track`。"""
    ctx = request.app["ctx"]
    pts = ctx.locator.track()
    if len(pts) > 20000:
        import numpy as np
        arr = np.asarray([[p["t_pc_ms"] or 0, p["x"] or 0, p["y"] or 0,
                           p["theta_deg"] or 0, 1 if p["valid"] else 0] for p in pts],
                         dtype="<f4")
        ctx.bus.publish_bin({"type": "locate.track", "shape": list(arr.shape),
                             "columns": ["t_pc_ms", "x", "y", "theta_deg", "valid"],
                             "n": len(pts)}, arr.tobytes())
        return ok({"count": len(pts), "pushed": "ws:locate.track",
                   "note": "点数超过 2 万，改走二进制帧"})
    return ok({"count": len(pts), "points": pts})


async def _loc_runs(request: web.Request) -> web.Response:
    return ok(request.app["ctx"].locator.runs())


async def _loc_run(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    d = await body_json(request)
    sid = d.get("session_id") or ((ctx.store.current.camera_ref or {}).get("session_id")
                                  if ctx.store.current and ctx.store.current.camera_ref else None)
    if d.get("src"):
        src = str(d["src"])
    else:
        root = ctx.camera.data_dir()
        if root is None or not sid:
            raise ApiError(400, "给 session_id，或用 src 直接指定帧目录")
        src = str(Path(root) / str(sid))
        if not (Path(src) / "frames").is_dir():
            raise ApiError(404, f"本地没有这个会话的帧目录：{src}/frames")
    rid = ctx.locator.start_batch(src, out_dir=d.get("out_dir"),
                                  limit=int(d.get("limit") or 0))
    ctx.store.mark("locate", "batch.start", str(sid or src), f"run={rid}")
    return ok({"run_id": rid, "src": src})


async def _loc_run_status(request: web.Request) -> web.Response:
    return ok(request.app["ctx"].locator.run_status(request.match_info["rid"]))


async def _loc_run_abort(request: web.Request) -> web.Response:
    return ok(request.app["ctx"].locator.abort_run(request.match_info["rid"]))


async def _loc_online(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    d = await body_json(request)
    on = bool(d.get("on", True))
    ctx.locator.set_online(on)
    if on:
        ctx.locator.start_online()
    return ok({"online": on, "stats": ctx.locator.stats()})


# ================================================================ 会话

async def _session_open(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    d = await body_json(request)
    sid = str(d.get("session_id") or "").strip() or None
    return ok(await ctx.session_open(session_id=sid, notes=d.get("notes")))


async def _session_close(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    d = await body_json(request)
    return ok(await ctx.session_close(
        wait_delivery_s=float(d.get("wait_delivery_s") or 25.0),
        auto_rerun=bool(d.get("auto_rerun", True))))


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


# ================================================================ 场地

async def _arena_get(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    loc = ctx.last_loc or {}
    fw = None
    mc = ctx.mouse.mc
    if mc is not None and mc.is_connected:
        try:
            fw = mc.maze.position()
        except Exception:                                    # noqa: BLE001
            fw = None
    delta = None
    if fw and loc.get("valid") and loc.get("x") is not None:
        dx, dy, d = ctx.arena.residual_cells((int(fw[0]), int(fw[1])),
                                             (float(loc["x"]), float(loc["y"])))
        t_fw = ctx.last_telemetry.get("t_pc_ms")
        t_vis = loc.get("_t_pc_ms") or loc.get("t_pc_ms")
        delta = {"dx_cells": round(dx, 3), "dy_cells": round(dy, 3),
                 "rms_cells": round(d, 3),
                 "dtheta_deg": None, "dt_ms": (int(t_vis) - int(t_fw))
                 if (t_vis and t_fw) else None,
                 "note": "Δpos 随速度系统性变大"}
        if loc.get("theta_deg") is not None and fw[2] is not None:
            delta["dtheta_deg"] = round(float(loc["theta_deg"]) - float(fw[2]), 2)
    return ok({"arena": ctx.arena.to_dict(), "grid_lines": [
        [[a[0], a[1]], [b[0], b[1]]] for a, b in ctx.arena.grid_lines()],
        "fw_position": fw, "vis": loc or None, "delta": delta,
        "clock": ctx.clock.snapshot()})


async def _arena_obs_post(request: web.Request) -> web.Response:
    """加一个标定观测点："把鼠放在某个已知格"，然后取当前的固件格号 + 视觉位置。

    `fw_xy` 不给就自动从固件读（鼠必须连着）；`vis_xy` 不给就取最近一次定位结果。
    """
    ctx = request.app["ctx"]
    d = await body_json(request)
    fw = d.get("fw_xy")
    if fw is None:
        mc = ctx.mouse.mc
        if mc is None or not mc.is_connected:
            raise ApiError(409, "未连接电脑鼠：请连接或手动传入 fw_xy=[x,y]")
        pos = mc.maze.position()
        if pos is None:
            raise ApiError(409, "固件尚未上报位置")
        fw = [int(pos[0]), int(pos[1])]
    vis = d.get("vis_xy")
    if vis is None:
        loc = ctx.last_loc
        if not loc or loc.get("x") is None:
            raise ApiError(409, "无有效定位结果：请先启动在线定位")
        vis = [float(loc["x"]), float(loc["y"])]
    obs = getattr(ctx, "_calib_obs", [])
    obs.append({"fw_xy": [int(fw[0]), int(fw[1])], "vis_xy": [float(vis[0]), float(vis[1])],
                "note": str(d.get("note") or "")})
    ctx._calib_obs = obs
    return ok({"observations": obs, "n": len(obs)})


async def _arena_obs_get(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    return ok({"observations": getattr(ctx, "_calib_obs", [])})


async def _arena_solve(request: web.Request) -> web.Response:
    """穷举 4 种 flip 组合，挑残差最小的那种（§7.4 Step 3）。"""
    ctx = request.app["ctx"]
    d = await body_json(request)
    raw = d.get("observations") or getattr(ctx, "_calib_obs", [])
    if not raw:
        raise ApiError(400, "无观测点：至少需要 2 个")
    obs = [CalibObservation(fw_xy=(int(o["fw_xy"][0]), int(o["fw_xy"][1])),
                            vis_xy=(float(o["vis_xy"][0]), float(o["vis_xy"][1])))
           for o in raw]
    sol = solve_flips(obs, grid=ctx.arena.grid)
    if d.get("apply") and sol["confident"]:
        b = sol["best"]
        ctx.save_arena(y_flip=b["y_flip"], x_flip=b["x_flip"],
                       marker_offset_mm=list(ctx.arena.fw_to_arena.marker_offset_mm),
                       theta_offset_deg=ctx.arena.fw_to_arena.theta_offset_deg,
                       camera_height_mm=ctx.arena.camera_height_mm,
                       nadir_xy=list(ctx.arena.nadir_xy), calibrated=True,
                       note=f"由 {sol['n_points']} 个观测点解出，margin={sol['margin_cells']} 格")
        sol["applied"] = True
    elif d.get("apply"):
        sol["applied"] = False
        sol["apply_refused"] = "区分度不足，拒绝落盘（落错不报错，只会导致整图镜像）"
    return ok(sol)


async def _arena_calibrate(request: web.Request) -> web.Response:
    """直接落盘一组标定值（界面上的向导表单提交到这里）。"""
    ctx = request.app["ctx"]
    d = await body_json(request, required=True)
    need = ("y_flip", "x_flip")
    if not all(k in d for k in need):
        raise ApiError(400, f"至少要给 {need}（其余可选）")
    res = ctx.save_arena(
        y_flip=bool(d["y_flip"]), x_flip=bool(d["x_flip"]),
        marker_offset_mm=list(d.get("marker_offset_mm")
                              or ctx.arena.fw_to_arena.marker_offset_mm),
        theta_offset_deg=float(d.get("theta_offset_deg",
                                     ctx.arena.fw_to_arena.theta_offset_deg)),
        camera_height_mm=_f(d, "camera_height_mm"),
        nadir_xy=d.get("nadir_xy"),
        calibrated=bool(d.get("calibrated", True)),
        note=str(d.get("note") or ""))
    return ok(res)
