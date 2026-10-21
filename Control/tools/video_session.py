"""视频会话薄壳：把 Auto Locate 的 `run_session` 接上**项目配置**（改版二十四）。

为什么需要这一层（不是重复上游的 CLI）：

    `python -m aruco_locator.session` 只收 `--source/--out/--start-s/--end-s/--stride`，
    **不收 `--config`**（`session.py` 的 `__main__` 段）。直接用会拿内置默认配置跑：
    720p 必需的 `detection.scales=[1,2,3]`、内参、视差全部不生效 ⇒ 结果是一堆无效帧，
    然后被误判成"这个视频不行"。这个坑一旦踩上，排查方向会完全错。

所以本壳只做三件事：① 加 `sys.path` 指到 Auto Locate；② 把 `config` 路径传进去；
③ 打印一行 JSON 摘要给上层（上层读 `session.json` 为准，这行只是方便看日志）。

用法（由 `control/adapters/video.py` 起子进程调用，也可以手工跑）：

    python tools/video_session.py --source ..\\video\\run.mp4 --out ..\\video\\out\\20261022_x \\
        --config "..\\Auto Locate\\config.yaml" [--start-s 1.0] [--end-s 30] \\
        [--stride 1] [--no-maze] [--max-frames 100]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="video_session", description=__doc__)
    ap.add_argument("--source", required=True, help="视频文件路径")
    ap.add_argument("--out", required=True, help="会话产物目录")
    ap.add_argument("--config", default="", help="Auto Locate 的 config.yaml（**别省**）")
    ap.add_argument("--autolocate", default="", help="Auto Locate 根目录（默认从配置/相对路径推）")
    ap.add_argument("--start-s", type=float, default=None)
    ap.add_argument("--end-s", type=float, default=None)
    ap.add_argument("--stride", type=int, default=1)
    ap.add_argument("--max-frames", type=int, default=None)
    ap.add_argument("--no-maze", action="store_true", help="不做多帧迷宫融合")
    args = ap.parse_args(argv)

    # ---- 上游根目录：参数 > 配置 > 默认相对位置
    al_root = args.autolocate
    if not al_root:
        al_root = str((ROOT.parent / "Auto Locate").resolve())
    al = Path(al_root)
    if not (al / "aruco_locator").is_dir():
        print(json.dumps({"ok": False, "error": f"找不到 Auto Locate 包：{al}"},
                         ensure_ascii=False))
        return 2
    sys.path.insert(0, str(al))

    try:
        from aruco_locator.session import run_session
    except Exception as e:                                    # noqa: BLE001
        print(json.dumps({"ok": False,
                          "error": f"导入 aruco_locator.session 失败：{type(e).__name__}: {e}"},
                         ensure_ascii=False))
        return 2

    src = Path(args.source)
    if not src.is_file():
        print(json.dumps({"ok": False, "error": f"视频文件不存在：{src}"}, ensure_ascii=False))
        return 2
    cfg = args.config or ""
    if cfg and not Path(cfg).is_file():
        # 配置给了却不存在 —— **明确报错**，不悄悄落回默认配置（那正是本壳要避免的坑）
        print(json.dumps({"ok": False, "error": f"配置不存在：{cfg}"}, ensure_ascii=False))
        return 2

    try:
        res = run_session(
            str(src),
            config=(cfg or None),
            out_dir=args.out,
            start_s=args.start_s,
            end_s=args.end_s,
            stride=max(1, int(args.stride or 1)),
            max_frames=args.max_frames,
            maze=not args.no_maze,
        )
    except NotImplementedError as e:
        print(json.dumps({"ok": False, "error": f"{e}"}, ensure_ascii=False))
        return 2
    except Exception as e:                                    # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return 1

    out = {
        "ok": True,
        "n_total": int(getattr(res.frames, "n_total", 0)),
        "n_valid": int(getattr(res.frames, "n_valid", 0)),
        "valid_ratio": float(getattr(res.frames, "valid_ratio", 0.0) or 0.0),
        "fps": float(getattr(res.video, "fps_declared", 0.0) or 0.0),
        "frames": int(getattr(res.video, "frames", 0) or 0),
        "vfr_suspected": bool(getattr(res.video, "vfr_suspected", False)),
        "flags": list(res.flags or []),
        "out": str(args.out),
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
