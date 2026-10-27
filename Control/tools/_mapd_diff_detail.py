"""一次性诊断：把 map_live_check 落盘的逐帧 CSV 重放成两张图，逐面列出差异。

用途：`L7` 报出不一致面时，判断是「固件 open4 错」还是「主机推断错」。
不参与正式验收，纯排查用。

    python tools/_mapd_diff_detail.py <frames.csv>
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from control.map_live import LiveWallMap          # noqa: E402
from control.maze_rec import MazeWallMap          # noqa: E402

DIR_NAME = {0: "+Y", 1: "+X", 2: "-Y", 3: "-X"}


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: _mapd_diff_detail.py <frames.csv>")
        return 2
    lm = LiveWallMap(16)
    wm = MazeWallMap(16)
    rows = list(csv.DictReader(open(sys.argv[1], encoding="utf-8")))

    readings = []
    for r in rows:
        ents = []
        for part in (r["entries"] or "").split(";"):
            if not part:
                continue
            ex, ey, o4 = part.split(":")
            ents.append({"x": int(ex), "y": int(ey), "open4": int(o4, 16)})
        rec = {
            "seq": int(r["seq"]), "_seq": int(r["seq"]),
            "posX": int(r["posX"]), "posY": int(r["posY"]), "dir": int(r["dir"]),
            "wallLeft": int(r["wallL"]), "wallFront": int(r["wallF"]),
            "wallRight": int(r["wallR"]), "frontFar": int(r["frontFar"]),
            "mapd": {"count": int(r["count"]), "lossy": int(r["lossy"]),
                     "resync": int(r["resync"]), "entries": ents},
        }
        lm.feed(rec)
        wm.feed(rec)
        readings.append((int(r["posX"]), int(r["posY"]), int(r["dir"]),
                         int(r["wallL"]), int(r["wallF"]), int(r["wallR"]),
                         int(r["frontFar"]), int(r["frontBoth"])))

    print("=== 每格读数（每格取第一次） ===")
    shown = set()
    for px, py, d, wl, wf, wr, far, both in readings:
        if (px, py) in shown:
            continue
        shown.add((px, py))
        print(f"  ({px:2d},{py:2d}) dir={DIR_NAME[d]:2s} L={wl} F={wf} R={wr}"
              f" far={far} both={both}")

    print("\n=== 面差异（0=可通 1=有墙；只列两边都知道的） ===")
    bad = 0
    for y in range(16):
        for x in range(16):
            # fv/fh 都是 n×(n-1)：x 最大 n-2（n-1 是外框，`draw.maze` 单独画）
            if x >= 15:
                continue
            for kind, mine, theirs, known in (
                ("h", lm.fh[y][x], int(wm.wall[x][y][0]), wm.known[x][y][0]),
                ("v", lm.fv[y][x], int(wm.wall[x][y][1]), wm.known[x][y][1]),
            ):
                if mine == 2 or not known:
                    continue
                if mine != theirs:
                    bad += 1
                    print(f"  {'+' if kind == 'h' else '|'} ({x:2d},{y:2d})"
                          f" {kind}面  mapd={mine} host={theirs}"
                          f"  [该格 open4={lm.open4[x][y]:x}]")
    print(f"\n差异合计 {bad} 面")
    return 0


if __name__ == "__main__":
    sys.exit(main())
