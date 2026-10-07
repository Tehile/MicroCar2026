"""生成**合成场地帧**，用来在没有手机、没有相机、没有场地时正向验证整条定位链路。

为什么不直接用 `Auto Locate/selftest_synthetic.py` 的输出当输入：
它 `--out` 存的是**叠加调试图** —— 角标记框、场地四边形、左上角的文字图例都画在上面了。
那些绿色/青色线正好压在标记的黑色边框上，cv2 的检测器于是**一个都认不出来**。
拿叠加图当定位输入，方法上就是错的（相当于把答案本身当成题目）。

所以这个脚本自己画干净的场地图：
    · 4 个角标记（ID 1~4）＋ 1 个目标标记（ID 0），全部用 cv2 的官方生成器画；
    · 施加一次**已知**的透视变换模拟相机（可关掉）；
    · 写出**真实交付格式**的 `frame_XXXXXX.yuv` + `.json`（裸 Y 平面 + 元数据），
      可按需求带上行填充（`--pad`），用来验证 decode 的 stride 处理（判据 I13）；
    · 同时写出 `ground_truth.json`：目标是"链路的产物对不对"，没有真值就没法判。

用法：
    python tools/make_synth_frames.py                      # 默认 6 帧到 .tmp/fake_camera
    python tools/make_synth_frames.py --out D:/x --frames 20
    python tools/make_synth_frames.py --pad 16             # 每帧行尾加 16 字节填充
    python tools/make_synth_frames.py --no-warp            # 理想俯视（验证几何本身）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DICT = cv2.aruco.DICT_4X4_1000
CORNER_IDS = [1, 2, 3, 4]
TARGET_ID = 0


def marker_bitmap(mid: int, px: int) -> np.ndarray:
    """官方生成器画标记。px 是**整块**边长（含黑框）。"""
    return cv2.aruco.generateImageMarker(
        cv2.aruco.getPredefinedDictionary(DICT), int(mid), int(px))


def build_field(size: int, marker_px: int, target_xy, target_theta_deg: float,
                rng: np.random.Generator):
    """画一张干净的俯视场地图。返回 (灰度图, 场地正方形的像素范围)。

    ⚠️ **坐标真值必须按"角标记内顶点构成的正方形"算，不是整幅图** ——
    这是 Auto Locate 的契约：场地坐标系是 [0,1]²，而它的边界由四个角标记的
    **内顶点**决定。按整幅图归一化，算出来的位置会系统性地差一个
    `(inset+marker_px)/size` 的偏移，且**看起来很像算法有偏**。
    （这个坑我是踩过一次才写下的：第一版真值差了 2.6% 场地边长，一度以为是定位器的问题。）

    `target_xy` 是**归一化** [0,1]² 位置（原点在左下角，与契约一致）。
    """
    img = np.full((size, size), 235, np.uint8)
    m = marker_px
    inset = int(size * 0.02)
    lo = inset + m                 # 场地正方形在该图的像素边界
    hi = size - inset - m
    span = hi - lo

    # 四个角标记：ID1 左上 / ID2 右上 / ID3 右下 / ID4 左下。
    # 上游明说"朝向与 ID 排列顺序均不固定"，所以排放顺序其实无所谓 ——
    # 坐标系的定义（原点在哪、x/y 朝哪）由定位器自己推，我们只提供一张能被检出的图。
    places = {
        1: (inset, inset),
        2: (hi, inset),
        3: (hi, hi),
        4: (inset, hi),
    }
    for mid, (x, y) in places.items():
        img[y:y + m, x:x + m] = marker_bitmap(mid, m)

    # 目标标记：可旋转
    bmp = marker_bitmap(TARGET_ID, m)
    cx = target_xy[0] * size
    cy = (1.0 - target_xy[1]) * size           # 归一化原点在左下 ⇒ 图上 y 要翻
    pad = int(m * 1.5)
    canvas = np.full((m + 2 * pad, m + 2 * pad), 235, np.uint8)
    canvas[pad:pad + m, pad:pad + m] = bmp
    M = cv2.getRotationMatrix2D((canvas.shape[1] / 2, canvas.shape[0] / 2),
                                target_theta_deg, 1.0)
    rot = cv2.warpAffine(canvas, M, (canvas.shape[1], canvas.shape[0]),
                         flags=cv2.INTER_NEAREST, borderValue=235)
    x0 = int(cx - rot.shape[1] / 2)
    y0 = int(cy - rot.shape[0] / 2)
    if x0 < 0 or y0 < 0 or x0 + rot.shape[1] > size or y0 + rot.shape[0] > size:
        raise SystemExit(
            f"目标标记被摆到图外了（cx,cy=({cx:.0f},{cy:.0f})）——"
            f"target_xy 请留在 [0.15, 0.85] 之内，合成图的边界不代表场地边界。")
    img[y0:y0 + rot.shape[0], x0:x0 + rot.shape[1]] = rot[:, :]

    cv2.rectangle(img, (lo, lo), (hi, hi), 200, 2)
    return img, (lo, hi, span)


def to_field_xy(cx_px: float, cy_px: float, box) -> tuple:
    """图像像素 → 场地归一化（原点左下）。这才是与 Auto Locate 可比的那套坐标。"""
    lo, hi, span = box
    x = (cx_px - lo) / span
    y = 1.0 - (cy_px - lo) / span
    return x, y


def warp_view(gray: np.ndarray, rng: np.random.Generator, amount: float):
    """施加一次透视变换，模拟"相机没完全正对"的俯拍。

    **返回 (图, 单应矩阵)** —— 把矩阵交出来是刻意的：真值必须用**同一个**变换
    去投影，而不是"再跑一遍随机数希望得到同样的角点"。第一版就是这么写的，
    结果是真值随机地对不上，而那种"误差"看起来完全像是算法的问题。
    """
    if amount <= 0:
        return gray, None
    h, w = gray.shape
    j = amount * min(h, w)
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([
        [rng.uniform(0, j), rng.uniform(0, j)],
        [w - rng.uniform(0, j), rng.uniform(0, j)],
        [w - rng.uniform(0, j), h - rng.uniform(0, j)],
        [rng.uniform(0, j), h - rng.uniform(0, j)],
    ])
    M = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(gray, M, (w, h), borderValue=235), M


def field_xy_after_warp(cx_px: float, cy_px: float, box, M=None) -> tuple:
    """把"画图时的像素点"换算成场地归一化坐标。

    ⚠️ **M 默认必须是 None —— 真值不要再过一次透视变换。**

    这一点反直觉但很关键：相机俯拍是**平面单应**，而定位器是把检出的角点
    用单应**反投**回场地坐标再取中心。于是"绘画时画的中心"与"反投回来的中心"
    是同一个点（精确相等），而"把中心正投到拍摄图上再取"是**另一个点** ——
    二者在透视畸变下会差百分之几（离场边越近差得越多）。

    第一版真值就是多投了这一次，结果 seq1 的 y 差了 0.022 场地边，
    而那个误差**看起来完全像是定位器有偏差**。（另一个更早的坑：按整幅图
    归一化而不是按角标记内顶点围成的正方形，那会差一个固定的 ~2.6%。）
    """
    lo, hi, span = box
    corners = np.float32([[[lo, lo]], [[hi, lo]], [[hi, hi]], [[lo, hi]]])
    pt = np.float32([[[cx_px, cy_px]]])
    if M is not None:
        corners = cv2.perspectiveTransform(corners, M)
        pt = cv2.perspectiveTransform(pt, M)
    c = corners.reshape(4, 2)
    lo_w = float(c[:, 0].min()); hi_w = float(c[:, 0].max())
    lo_h = float(c[:, 1].min()); hi_h = float(c[:, 1].max())
    span_w = hi_w - lo_w
    p = pt.reshape(2)
    return (p[0] - lo_w) / span_w, 1.0 - (p[1] - lo_h) / span_w


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / ".tmp" / "fake_camera"))
    ap.add_argument("--session-id", default="SYNTH-TEST_0001")
    ap.add_argument("--frames", type=int, default=6)
    ap.add_argument("--size", type=int, default=960)
    ap.add_argument("--marker-px", type=int, default=80)
    ap.add_argument("--pad", type=int, default=0, help="每行尾部填充字节数（验证 stride 处理）")
    ap.add_argument("--warp", type=float, default=0.06, help="透视抖动比例（0=理想俯视）")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)

    rng = np.random.default_rng(args.seed)
    root = Path(args.out)
    fdir = root / args.session_id / "frames"
    fdir.mkdir(parents=True, exist_ok=True)

    truth = []
    for i in range(args.frames):
        tx = 0.40 + 0.02 * i          # 目标在**场地**里的归一化位置
        ty = 0.45 + 0.015 * i
        tth = -150.0 + 38.0 * i
        g, box = build_field(args.size, args.marker_px, (tx, ty), tth, rng)
        v, Mw = warp_view(g, rng, args.warp)
        # 真值不过透视：单应把平面点映得精确，见 field_xy_after_warp 的说明
        gx, gy = field_xy_after_warp(tx * args.size, (1 - ty) * args.size, box)
        assert abs(gx - tx) < 0.5 and abs(gy - ty) < 0.5

        seq = i + 1
        W = v.shape[1]
        stride = W + args.pad
        buf = np.zeros((v.shape[0], stride), np.uint8)
        buf[:, :W] = v
        blob = buf.tobytes()
        yuv = fdir / f"frame_{seq:06d}.yuv"
        yuv.write_bytes(blob)
        meta = {
            "seq": seq,
            "session_id": args.session_id,
            "format": "YUV_Y",
            "width": W, "height": int(v.shape[0]),
            "y_stride": stride, "source_row_stride": W,
            # repacked=True 表示"已去填充"（与上游元数据的语义一致）
            "repacked": bool(stride == W),
            "size_bytes": len(blob),
            "wall_clock_ns": int(time.time() * 1e9) + seq * 33_000_000,
            "sensor_timestamp_ns": int(time.time() * 1e9) + seq * 33_000_000,
            "capture_path_id": "stream_deliver_yuv",
            "capture_path_rev": 3,
            "sha256": hashlib.sha256(blob).hexdigest(),
            "file": yuv.name,
            "_synthetic": True,
        }
        (fdir / f"frame_{seq:06d}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        truth.append({"seq": seq, "x": round(float(gx), 4), "y": round(float(gy), 4),
                      "theta_deg": round(float(tth), 2),
                      "note": ("真值 = 目标标记中心在**角标记内顶点构成的场地正方形**里的"
                               "归一化坐标（原点左下、逆时针为正），已计入透视抖动")})

    (root / args.session_id / "ground_truth.json").write_text(
        json.dumps({"session_id": args.session_id, "frames": truth,
                    "field_note": "px 尺度是任意的，判定的是**归一化**坐标"},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"合成会话写到：{root / args.session_id}")
    print(f"  帧数 {args.frames}，{args.size}×{args.size}，marker {args.marker_px}px，"
          f"行填充 {args.pad} 字节，透视抖动 {args.warp}")
    print(f"  真值见 ground_truth.json —— 判定链路的产物对不对，没有真值就没法判")
    print(f"  在 Control 里导入：POST /api/v1/camera/import "
          f'{{"session_id":"{args.session_id}","root":"{root}"}}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
