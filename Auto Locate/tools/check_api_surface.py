"""API 表面检查 —— 防止"静默回退"再次打断会话级功能。

背景（2026-10-27）：`aruco_locator/api.py` 曾被回退到更早版本，
`ArucoLocator.field_frame_from_obs()` 消失，`session.py` 的迷宫融合直接
`AttributeError`，而**导入、单元自检、单帧定位全部正常** —— 这类回退很难被发现。
本脚本把"必须存在的名字"写成断言，一条命令即可验收。

用法::

    python tools/check_api_surface.py        # 全部通过 → 退出码 0

覆盖三层：
  1. v1.x 冻结契约（`__version__` / `__all__` / `Localization` 字段 / `locate`）
  2. v1.2 会话级接口（`aruco_locator.session` 的公开名字与 `run_session` 形参）
  3. 独立脚本 `maze_extract` 的被依赖符号（session 融合直接用到的）
"""

from __future__ import annotations

import inspect
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  ok   {msg}")
    else:
        print(f"  FAIL {msg}")
        FAILS.append(msg)


def main() -> int:
    import aruco_locator as al

    print("[1] v1.x 冻结契约")
    check(al.__version__.startswith("1."), f"__version__ = {al.__version__}")
    check(len(al.__all__) == 36, f"__all__ 数量 = {len(al.__all__)}（基线 36）")
    for name in ("create_locator", "locate", "ArucoLocator", "Localization", "iter_locations"):
        check(hasattr(al, name), f"aruco_locator.{name}")
    fields = set(inspect.signature(al.Localization).parameters) if hasattr(
        al.Localization, "__dataclass_fields__") else set(al.Localization.__dataclass_fields__)
    for f in ("x", "y", "theta_deg", "valid", "reason", "flags", "rms_field", "origin_id",
              "corner_ids", "x_raw", "y_raw"):
        check(f in al.Localization.__dataclass_fields__, f"Localization.{f}")

    print("[2] 会话级接口（v1.2 拟）")
    try:
        import aruco_locator.session as s

        for name in ("run_session", "preflight", "fuse_maze", "SessionResult", "VideoMeta",
                     "SyncResult", "Trajectory", "FrameStats", "render_trajectory"):
            check(hasattr(s, name), f"session.{name}")
        params = set(inspect.signature(s.run_session).parameters)
        for p in ("video_path", "config", "out_dir", "start_frame", "end_frame", "start_s",
                  "end_s", "stride", "sync_led", "max_frames", "maze", "maze_cfg",
                  "maze_stride", "maze_min_consistency"):
            check(p in params, f"run_session(…, {p})")
    except ImportError as exc:  # pragma: no cover
        check(False, f"导入 aruco_locator.session 失败：{exc}")

    print("[3] 四角求 H（迷宫融合依赖，最易被回退）")
    check(hasattr(al.ArucoLocator, "field_frame_from_obs"),
          "ArucoLocator.field_frame_from_obs")
    if hasattr(al.ArucoLocator, "field_frame_from_obs"):
        sig = inspect.signature(al.ArucoLocator.field_frame_from_obs)
        check(list(sig.parameters) == ["self", "obs", "image_shape"],
              f"field_frame_from_obs 形参 = {list(sig.parameters)}")

    print("[4] maze_extract 被依赖符号")
    try:
        import maze_extract as me

        for name in ("Maze", "MazeConfig", "extract_maze", "make_ortho", "measure_walls",
                     "render_structure", "render_maze", "_to_state", "UNKNOWN", "WALL", "OPEN"):
            check(hasattr(me, name), f"maze_extract.{name}")
        if hasattr(me, "Maze"):
            check(hasattr(me.Maze, "to_adjacency"), "Maze.to_adjacency")
            check(hasattr(me.Maze, "sanity"), "Maze.sanity")
    except ImportError as exc:  # pragma: no cover
        check(False, f"导入 maze_extract 失败：{exc}")

    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项 —— API 表面与契约不一致，请先修复再跑会话级功能。")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
