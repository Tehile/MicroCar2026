"""Control 启动器。

    python app.py                 # 默认读同目录 config.yaml
    python app.py --port 8771
    python app.py --print-paths   # 只打印解析后的路径（排查路径问题用）

⚠️ 只绑 127.0.0.1（单机自用）。**要开 0.0.0.0 必须先补鉴权** ——
这个服务能直接操作电脑鼠、能读相机凭据，暴露到局域网等于把控制权交出去。
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

import aiohttp
from aiohttp import web

from control import __version__
from control.api import errors as api_errors
from control.api import routes
from control.config import ConfigError, bootstrap
from control.ctx import Ctx

HERE = Path(__file__).resolve().parent


@web.middleware
async def ui_no_cache(request: web.Request, handler):
    """界面资源一律 `no-cache`：**允许缓存，但每次都必须回源校验**。

    不写这个头时，浏览器会按 `Last-Modified` 自行做启发式缓存，而且**每个文件
    独立**决定要不要复用 —— 于是很容易拿到新旧混用的组合。实测过的那种：
    `index.html` 已经是新的（有 `#nav-sys`、会加载 `mod_*.js`），而 `wsproto.js`
    还是缓存里的旧版（命名空间里没有 `mods`）⇒ 六个模块脚本全在
    `C.mods.mod_xxx = ...` 上抛错、一个都没注册，启动链随即在 `mountViews`
    抛出 `Cannot read properties of undefined (reading 'mod_params')`，
    整页只剩空壳。**这类故障在界面上看不出跟缓存有关**，所以从源头掐掉。

    代价是一次 304（本机毫秒级）；API 响应不在这里，另有自己的 `no-store`。
    """
    resp = await handler(request)
    if request.path == "/" or request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def build_app(cfg, http: aiohttp.ClientSession) -> web.Application:
    app = web.Application(middlewares=[ui_no_cache, api_errors.error_middleware],
                          client_max_size=8 << 20)
    ctx = Ctx(cfg)
    app["ctx"] = ctx
    app["http"] = http
    routes.register(app, ctx)

    static_dir = HERE / "control" / "static"
    app.router.add_get("/", lambda r: web.FileResponse(static_dir / "index.html"))
    app.router.add_static("/static/", static_dir, name="static")

    async def _on_startup(a: web.Application) -> None:
        await ctx.start_background()
        ctx.bus.log("info", f"Control v{__version__} 起来了："
                            f"http://{cfg.get('server.host')}:{cfg.get('server.port')}")
        if not ctx.mouse.available:
            ctx.bus.log("warn", "鼠模块不可用 —— 波形/参数/标定/调试台会打不开")
        if not ctx.camera.available:
            ctx.bus.log("warn", "相机模块不可用 —— 采集页会打不开")
        if not ctx.locator.available:
            ctx.bus.log("warn", "定位模块不可用 —— 场地页只有固件位置，没有视觉位置")

    async def _on_cleanup(a: web.Application) -> None:
        await ctx.shutdown()

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


async def main(args: argparse.Namespace) -> int:
    try:
        cfg = bootstrap(args.config)
    except ConfigError as e:
        print(f"配置有问题：\n{e}", file=sys.stderr)
        return 2

    if args.print_paths:
        print(f"control      : {cfg.root}")
        print(f"sessions     : {cfg.session_root}")
        for k in ("remotecontrol_host", "phonecamera_pc", "autolocate", "locator.config"):
            v = cfg.get(f"paths.{k}") if k != "locator.config" else cfg.get("locator.config")
            if v:
                print(f"{k:13s}: {v}  {'OK' if Path(str(v)).exists() else '**不存在**'}")
        return 0

    host = args.host or str(cfg.get("server.host"))
    port = int(args.port or cfg.get("server.port"))
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"⚠️ 你把服务绑到了 {host} —— 这个服务能直接控鼠并持有相机凭据，"
              f"暴露到局域网前**必须先补鉴权**。继续请显式确认。", file=sys.stderr)
        if not args.i_know_what_im_doing:
            print("（加 --i-know-what-im-doing 才会真的绑上去）", file=sys.stderr)
            return 3

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=5.0)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        app = build_app(cfg, http)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        url = f"http://{host}:{port}"
        print(f"\n  Control v{__version__}  →  {url}")
        print(f"  会话目录：{cfg.session_root}")
        print(f"  环境提示：本整合层固定用 general_env 跑"
              f"（C:\\Users\\liuru\\.conda\\envs\\general_env\\python.exe）")
        print(f"  按 Ctrl+C 收工\n")

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            await stop.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        print("\n正在收尾…")
        await runner.cleanup()
    return 0


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="app.py", description="电脑鼠整合上位机")
    p.add_argument("--config", "-c", default=None, help="配置文件（默认 ./config.yaml）")
    p.add_argument("--host", default=None, help="绑定地址（默认取配置；**默认只绑本机**）")
    p.add_argument("--port", type=int, default=None, help="端口（默认取配置，8770）")
    p.add_argument("--print-paths", action="store_true", help="打印解析后的上游路径后退出")
    p.add_argument("--i-know-what-im-doing", action="store_true",
                   help="允许绑到非 127.0.0.1（**先想清楚鉴权**）")
    return p.parse_args(argv)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main(parse_args())))
    except KeyboardInterrupt:
        raise SystemExit(130)
