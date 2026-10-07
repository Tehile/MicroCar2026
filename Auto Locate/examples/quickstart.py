"""aruco-locator 使用示例（只用到公开接口）。

运行::

    python examples/quickstart.py --image 路径/照片.jpg --out out/example
    python examples/quickstart.py --source camera:0            # 实时（按 q 退出）
    python examples/quickstart.py --source 路径/视频.mp4
    python examples/quickstart.py --params                     # 只看参数目录

需要该目录的上一级是本项目根目录（即 `import aruco_locator` 能找到）。
"""

from __future__ import annotations

import argparse
import os
import sys

import cv2
import numpy as np

# 允许直接从仓库根目录之外运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aruco_locator import (  # noqa: E402
    create_locator,
    describe_profiles,
    flatten_config,
    locate,
    parameter_catalog,
)


# --------------------------------------------------------------------------- #
def demo_params() -> None:
    """参数目录与档位：把可调空间打印出来。"""
    cat = parameter_catalog()
    print(f"可调参数共 {len(cat)} 个，分组：{sorted({r['group'] for r in cat})}")
    print("\n—— 参数目录样例（前 6 条）——")
    for r in cat[:6]:
        rng = f"  范围 {r['range']}" if r["range"] else ""
        print(f"  {r['name']:48s} {r['type']:18s} 默认={r['default']!r}{rng}")

    print("\n—— 预设档位 ——")
    for p in describe_profiles():
        print(f"  {p['name']:<14} {p['desc']}")
        for k, v in p["params"].items():
            print(f"      {k} = {v!r}")
    print(f"\n（完整手册：python -m aruco_locator.docgen -o docs/参数参考.md）")


# --------------------------------------------------------------------------- #
def demo_single_image(path: str, out_dir: str | None) -> None:
    """单图：默认参数 vs 档位 vs 临时改参数。"""
    buf = np.fromfile(path, dtype=np.uint8)          # 中文路径必须这样读
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        print(f"读不到图片：{path}")
        return
    print(f"\n图像 {img.shape[1]}x{img.shape[0]}\n")

    # ① 一次性调用，全部默认参数
    res = locate(img)
    print("① 默认参数     :", res.summary())
    if not res.valid:
        print("   → 无效原因：", res.reason)

    # ② 套用档位 + 临时覆盖一个参数
    res2 = locate(img, profile="accurate", edge_search_px=3.5)
    print("② accurate 档位 :", res2.summary())

    # ③ 常驻实例：改参数后复用（视频场景就是这么用）
    loc = create_locator(profile="video", params={"output.smooth": "ema"})
    for i in range(3):
        res3 = loc.locate(img, frame_index=i, timestamp=i * 0.04)
    print("③ 常驻+滤波     :", res3.summary(), " flags=", res3.flags)

    # ④ 结果里到底有什么
    d = res.to_dict()
    print("\n④ to_dict() 关键字段：")
    for k in ("valid", "x", "y", "theta_deg", "dir_x", "dir_y",
              "corner_ids", "origin_id", "rms_field", "flags"):
        print(f"   {k:14s} = {d[k]!r}")
    print("   （写 CSV 用 res.to_row()，多值字段会压成分号字符串）")

    # ⑤ 落盘：结果 + 叠加图
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as fp:
            fp.write(res.to_json())
        vis = loc.annotate(img, res)
        ok, enc = cv2.imencode(".png", vis)
        enc.tofile(os.path.join(out_dir, "annotated.png"))
        print(f"\n⑤ 已写出 {out_dir}/result.json 与 annotated.png")


# --------------------------------------------------------------------------- #
def demo_source(source: str, out_dir: str | None, max_frames: int | None) -> None:
    """目录 / 视频 / 摄像头：统一走 iter_locations，逐帧消费（生成器，内存友好）。

    注意 iter_locations 只吐结果不吐画面；要在视频上叠加请用 CLI：::

        python -m aruco_locator.cli --source 视频.mp4 --out out --save-video out/ov.mp4
    """
    from aruco_locator import iter_locations

    print(f"\n逐帧定位：{source}")
    n = valid = 0
    for res in iter_locations(source, None, max_frames=max_frames):
        n += 1
        valid += bool(res.valid)
        if n <= 3 or n % 50 == 0:
            print(f"  {res.summary()}")
    print(f"\n共 {n} 帧，有效 {valid}（{valid / max(n, 1):.1%}）")
    if out_dir and n:
        print("  （如需叠加视频/图片，请用 CLI 的 --save-video / --save-annotated）")


# --------------------------------------------------------------------------- #
def demo_config_roundtrip() -> None:
    """配置的读写与导出：改了一堆参数后，把最终生效值存下来。"""
    cfg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml"
    )
    loc = create_locator(cfg_path if os.path.isfile(cfg_path) else None, profile="video")
    loc.cfg.camera.parallax.camera_height_mm = 2000.0      # 现场实测后填进去
    flat = flatten_config(loc)
    print("\n当前生效的非默认参数：")
    from aruco_locator import LocatorConfig

    base = flatten_config(LocatorConfig())
    for k, v in flat.items():
        if v != base.get(k):
            print(f"  {k} = {v!r}   (默认 {base.get(k)!r})")


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="aruco-locator 示例")
    ap.add_argument("--image", default=None, help="单张图片路径")
    ap.add_argument("--source", default=None, help="目录 / 视频 / camera:0")
    ap.add_argument("--out", default=None, help="输出目录")
    ap.add_argument("--max-frames", type=int, default=None)
    ap.add_argument("--params", action="store_true", help="只打印参数目录与档位")
    args = ap.parse_args(argv)

    if args.params or (not args.image and not args.source):
        demo_params()
    if args.image:
        demo_single_image(args.image, args.out)
    if args.source:
        demo_source(args.source, args.out, args.max_frames)
    demo_config_roundtrip()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
