# -*- coding: utf-8 -*-
"""I30 的**变异验证**（纪律 12：一道从没红过的闸不算闸）。

守的是"两条来路同一个纪元"：把修复撤掉必须**变红**，否则这道闸就是摆设。
保留在仓库里 —— 差分/纪元那部分逻辑再动时，重跑一次就能知道闸还有没有牙。

做法：同一个场景跑两遍 ——
  · A（现状）：`_reset_epoch()` 清环 + 换差分器 + 零点交给发车后第一帧；
  · B（旧行为）：只挪零点，**不清环也不换差分器**（就是本轮修掉的那个 bug）。

要看的就一件事：B 里环里会不会出现**负 t_ms**（= 上一段/发车前的决策残留在新纪元里），
而 A 里不会。若两者一样，说明这道闸**根本没咬住**。

用法：`python tools/neg_i30.py`（纯内存，不连设备、不起服务）。实测 A 0 条负 t / B 11 条。
"""
import sys
import textwrap
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from control.maze_rec import MazeRecorder          # noqa: E402

CH = ["mazeTask", "posX", "posY", "dir", "motionTask", "stackIdx",
      "wallLeft", "wallFront", "wallRight", "goalX", "goalY"]


def _f(task=0, x=0, y=0, d=0, mo=0, st=0):
    return {"mazeTask": task, "posX": x, "posY": y, "dir": d, "motionTask": mo,
            "stackIdx": st, "wallLeft": 0, "wallFront": 1, "wallRight": 0,
            "goalX": 5, "goalY": 5}


def scenario(rec, pre_s, post_s):
    """发车前喂一段（会产生决策），再 start()，再喂一段。返回 (负条数, 总条数, 最大 t)。"""
    for i in range(12):
        rec.feed(_f(task=0, x=0, y=0, d=i % 4), None)     # dir 变 ⇒ HEAD 决策
        time.sleep(pre_s / 12.0)
    rec.start(why="neg-test")                            # ← 发车（新纪元）
    for i in range(12):
        rec.feed(_f(task=2, x=i % 3, y=0, d=(i + 1) % 4, mo=1), None)
        time.sleep(post_s / 12.0)
    d = rec.live_decisions()
    ds = d["decisions"]
    ts = [x["t_ms"] for x in ds]
    neg = [t for t in ts if t < 0]
    return len(neg), len(ds), (max(ts) if ts else None), d["t0_kind"]


OLD_BODY = """
    def _reset_epoch(self, kind="launch", stash=True):
        self._dec_t0 = None            # 旧行为：只挪零点，不清环、不换差分器
        self._dec_t0_kind = str(kind)
"""

ok = True
for tag, revert in (("A 现状（已修）", False), ("B 撤掉修复（旧行为）", True)):
    rec = MazeRecorder(Path("records/maze"), get_channels=lambda: list(CH))
    if revert:
        # 直接在实例所属的类上换掉 `_reset_epoch`（模拟"修之前"的实现）
        ns = {}
        exec(textwrap.dedent(OLD_BODY), {}, ns)           # noqa: S102
        rec._reset_epoch = ns["_reset_epoch"].__get__(rec, MazeRecorder)
    neg, n, mx, kind = scenario(rec, pre_s=1.2, post_s=1.2)
    verdict = "绿（无负 t）" if neg == 0 else "**红**（有负 t）"
    print("%-22s 环 %3d 条 · 负 t %2d 条 · max t=%s · kind=%s ⇒ %s"
          % (tag, n, neg, mx, kind, verdict))
    if revert:
        ok = ok and (neg > 0)          # 撤掉修复**必须**变红，否则这道闸是假的
    else:
        ok = ok and (neg == 0)         # 修复在时不许有负 t

print("\n负向验证", "通过：撤掉修复后确实变红 ⇒ 这道闸咬得住" if ok else "**失败：闸咬不住**")
sys.exit(0 if ok else 1)
