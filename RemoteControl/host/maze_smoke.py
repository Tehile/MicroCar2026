"""迷宫状态机回传(实验8 无记忆走迷宫) 冒烟测试。

v2.0(2026-10-05) 的重大简化 —— 迷宫状态**全部搬到统一帧**里:
    · `MAZE EV` 事件流 / `LINK_TYPE_MAZE` 1Hz 状态帧 / `SNAP MAZE` 点查 **三条全删**;
    · 状态量(mazeTask / dir / posX,posY / wallLeft,Front,Right+frontFar /
      motionTask)本来就在 30B 统一帧里(≈217Hz);
    · 8 类路口的分类**不再由固件上报**, 改由主机侧 `codec.classify_road()` 用三位
      墙原语复刻(固件 `link_RoadKind()` 的逐分支镜像);
    · "任务迁移序列"不再由固件推事件, 由主机差分相邻帧得到(`maze.task_changes()`)。

所以本套件覆盖的不再是"事件/状态帧解码", 而是:
  1. **纯函数层**: `classify_road()` 对 8 种墙组合的分类(1=有墙, 与固件 wallSns 同义);
  2. **协议层**: 统一帧携带迷宫状态位并能往返; FRAMES 只剩 2 种帧; 命令表精简;
  3. **端到端**: 起模拟器 → MAZE RUN → 统一帧里任务从 WAIT 迁到 SEARCH →
     主机差分出迁移序列 → 墙图拉取与渲染。

设计要点(针对本项目已经踩过的坑):
  · 固件的"路口类型"判据必须与 `wallSns` 同源, 否则主机侧的分类与代码里决策的不是
    一回事 —— 故这里把 8 种组合做成**互不重叠**的全覆盖断言;
  · 墙位语义是 **1 = 有墙(封死)**, 与旧 `_ROAD_CASES` 的直觉相反, 这里显式标注。

用法: python maze_smoke.py [--port N]
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time

sys.path.insert(0, ".")

from host.api.client import MouseClient
from host.protocol.codec import (
    ROAD_KINDS,
    ROAD_NAMES,
    TYPE_MAP,
    TYPE_UNIFIED,
    UNIFIED_CHANNELS,
    classify_road,
    decode_unified,
    pack_unified,
)
from host.registry import COMMANDS, FRAMES, command_line, spec_of
from host.sim.mouse_sim import MAZETYPE as FW_N  # 迷宫尺寸跟固件档位(见 host/fwprofile.py)

PASS = 0
FAIL = 0

#: `classify_road(wall_left, wall_front, wall_right)` 的判据表 —— 与固件
#: `link_RoadKind()` 逐分支一致。**墙位 1 = 有墙(封死)**, 0 = 开口。
_ROAD_CASES = [
    # (wall_left, wall_front, wall_right, 期望码)
    (1, 1, 1, 8),   # DEAD     三面封死
    (1, 0, 1, 1),   # THRU     仅前方开口
    (0, 1, 1, 2),   # CORNER_L 仅左方开口(左弯)
    (1, 1, 0, 3),   # CORNER_R 仅右方开口(右弯)
    (0, 0, 1, 4),   # TEE_FL   前&左开口, 右封
    (1, 0, 0, 5),   # TEE_FR   前&右开口, 左封
    (0, 1, 0, 6),   # TEE_LR   左&右开口, 前封
    (0, 0, 0, 7),   # CROSS    三向全开
]


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [ok]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")
    return bool(ok)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _blank() -> dict:
    return {c: 0 for c in UNIFIED_CHANNELS}


# ---------------------------------------------------------------- 离线

def test_offline() -> None:
    print("\n[1] 离线: 路口判据 / 统一帧迷宫字段 / 协议登记")

    ok = all(classify_road(l, f, r) == want for l, f, r, want in _ROAD_CASES)
    check("classify_road 8 种墙组合全覆盖", ok, str(_ROAD_CASES))
    codes = [classify_road(l, f, r) for l, f, r, _ in _ROAD_CASES]
    check("八类路口码互不重复(判据不重叠)", len(set(codes)) == 8, str(codes))
    check("路口名表 0..8 齐备", set(ROAD_NAMES) == set(range(9)), str(sorted(ROAD_NAMES)))
    check("每个路口名都有中文大类", all(n in ROAD_KINDS for n in ROAD_NAMES.values()))

    # 统一帧承载迷宫状态(取代已删除的 MAZE 事件/状态帧/SNAP)
    d = _blank()
    d.update({"mazeTask": 2, "dir": 1, "posX": 3, "posY": 4,
              "wallLeft": 1, "wallFront": 0, "wallRight": 1,
              "motionTask": 32, "frontFar": 0})
    rec = decode_unified(pack_unified(d))
    check("统一帧解出迷宫状态机字段",
          rec["mazeTask"] == 2 and rec["dir"] == 1 and (rec["posX"], rec["posY"]) == (3, 4),
          str({k: rec[k] for k in ("mazeTask", "dir", "posX", "posY")}))
    check("统一帧解出三面墙原语(1=有墙)",
          (rec["wallLeft"], rec["wallFront"], rec["wallRight"]) == (1, 0, 1),
          str((rec["wallLeft"], rec["wallFront"], rec["wallRight"])))
    check("统一帧派生路口分类(仅前方开口 -> THRU)",
          rec["road"] == 1 and rec["roadName"] == "THRU", f"{rec['road']} {rec['roadName']}")
    # v9: goal 已出帧(静态决策结果), 这里只验任务名/动作名解析
    check("统一帧解出任务名/动作名",
          rec["mazeTaskName"] == "SEARCH"
          and rec["motionTaskName"] == "SMARTSTRAIGHTFAST",
          str({k: rec[k] for k in ("mazeTaskName", "motionTaskName")}))
    check("迷宫字段 pack/decode 往返一致", decode_unified(pack_unified(rec)) == rec)

    # 协议登记: v2.0 只有 2 种帧; 迷宫不再有专用帧/专用命令
    check("FRAMES 只有 UNIFIED/MAP 两种帧", set(FRAMES) == {TYPE_UNIFIED, TYPE_MAP},
          str(sorted(FRAMES)))
    check("命令表有 maze.run / maze.dump / maze.stop",
          "maze.run" in COMMANDS and "maze.dump" in COMMANDS and "maze.stop" in COMMANDS)
    check("MAZE EV / MAZE RST 已删除(v2.0 精简)",
          "maze.ev" not in COMMANDS and "maze.rst" not in COMMANDS)
    check("maze.dump = MAP DUMP", command_line("maze.dump") == "MAP DUMP")
    check("maze.dump 不重发(整块回传占用链路)", spec_of("maze.dump").no_retry)

    # ---- 发车前的低电闸(v2.2.1, 真机事故 2026-10-05 的固化) ----
    # 事故: battMv 掉到 3962 mV 时发车, 表现为"电机一直转 + 对红外障碍物不敏感"
    # (低电 ⇒ 红外发射变暗 ⇒ 墙检测失效)。固件对低电没有保护动作, 故由上位机拦。
    # 这里只测判定函数本身 —— 确定性, 不受模拟器电池漂移影响。
    from host.registry import LAUNCH_MIN_MV, _precheck_maze_run

    class _T:
        def __init__(self, d): self._d = d
        def latest(self): return self._d

    class _M:
        def __init__(self, d): self.telemetry = _T(d)

    check("maze.run 挂了前置校验(登记表层, 绕不过)",
          spec_of("maze.run").precheck is _precheck_maze_run)
    r_low = _precheck_maze_run(_M({"battMv": LAUNCH_MIN_MV - 1}))
    check("★ 低电时拒绝发车(ERR_LOWBATT)",
          r_low is not None and r_low[0] == "ERR_LOWBATT", str(r_low))
    check("低电拒绝时带实测电压与阈值",
          bool(r_low) and r_low[1].get("battMv") == LAUNCH_MIN_MV - 1
          and r_low[1].get("minMv") == LAUNCH_MIN_MV, str(r_low))
    check("电池充足时放行", _precheck_maze_run(_M({"battMv": LAUNCH_MIN_MV})) is None)
    check("battMv==0(监视未开)不误拦", _precheck_maze_run(_M({"battMv": 0})) is None)
    check("无帧时不误拦", _precheck_maze_run(_M(None)) is None)


# ---------------------------------------------------------------- 端到端

def test_e2e(port: int) -> None:
    print("\n[2] 端到端: 发车 → 统一帧里的迷宫状态机 → 墙图")

    mc = MouseClient(auto_stream=True)
    if not mc.connect(f"tcp://127.0.0.1:{port}"):
        check("连接模拟器", False, mc.state_detail)
        return
    try:
        mc.telemetry.wait_first_frame(timeout=5.0)

        # 空闲(未发车): 固件上电 mouseTask=WAIT(0), 状态直接从统一帧读
        st0 = mc.maze.state()
        check("未发车时状态可读(来自统一帧)", st0 is not None, str(st0))
        if st0:
            check("未发车: 任务 = WAIT", st0["taskName"] == "WAIT", str(st0["taskName"]))

        # clear_changes() 会挂上 TELEMETRY 订阅, 之后由主机差分任务迁移
        # (`_prev_task` 基线)。订阅是**异步**的(调度线程 50Hz), 必须先让基线
        # 落在当前的 WAIT 上 —— 否则若在首帧被 pump 之前就发车, 基线直接落到
        # SEARCH, 0→SEARCH 那一跳就丢了。
        mc.maze.clear_changes()
        time.sleep(0.3)
        r = mc.maze.run()
        check("MAZE RUN 被接受", r.ok, r.status)

        # 等状态机离开 WAIT(进入搜索/冲刺等任务态)
        deadline = time.time() + 8.0
        entered = False
        while time.time() < deadline:
            st = mc.maze.state()
            if st and st["taskName"] not in ("WAIT", "?"):
                entered = True
                break
            time.sleep(0.05)
        check("进入迷宫任务态(不再是 WAIT)", entered, str(mc.maze.state()))

        st = mc.maze.state()
        check(f"状态: 位置是合法 {FW_N}x{FW_N} 格",
              bool(st) and 0 <= st["posX"] < FW_N and 0 <= st["posY"] < FW_N,
              f"({st['posX']},{st['posY']})" if st else "None")
        check("状态: 朝向 0..3", bool(st) and 0 <= st["dir"] <= 3,
              str(st["dir"]) if st else "None")
        check("状态: 路口分类已复刻到主机侧",
              bool(st) and st["roadName"] in ROAD_NAMES.values(),
              str(st["roadName"]) if st else "None")

        # "任务迁移序列"由主机差分相邻统一帧得到(固件不再推事件)
        mc.wait(lambda: len(mc.maze.task_changes()) >= 1, timeout=8.0)
        check("主机侧差分出任务迁移序列",
              len(mc.maze.task_changes()) >= 1, str(mc.maze.task_changes()))

        # summary / explain
        s = mc.maze.summary()
        check("summary.running 为真", s["running"], str(s))
        check("summary.task 非空", bool(s["task"]), str(s["task"]))
        check("summary: 开口三元组齐备", s["opens"] is not None and len(s["opens"]) == 3,
              str(s["opens"]))
        check("summary 未误报事件丢弃", not s["evDropped"], str(s["evDropped"]))
        lines = mc.maze.explain()
        check("explain 输出多行且含路口", len(lines) >= 4 and any("路口" in ln for ln in lines),
              str(lines))

        # 路口访问(主机侧纯计算)
        check("road() 落在 0..8", 0 <= mc.maze.road() <= 8, str(mc.maze.road()))
        check("road_debounced 返回合法路口名",
              mc.maze.road_debounced(samples=2, gap_s=0.01) in ROAD_NAMES.values(),
              mc.maze.road_name())

        # 墙图(静态数据, 按需拉取)
        m = mc.maze.fetch(timeout=10.0)
        check(f"墙图已收到且为 {FW_N}x{FW_N}", bool(m) and len(m["mapWall"]) == FW_N,
              str(type(m).__name__))
        check(f"mazetype = {FW_N}", mc.maze.mazetype() == FW_N, str(mc.maze.mazetype()))
        check("position 返回坐标", mc.maze.position() is not None, str(mc.maze.position()))
        check("cell() 读出某格通行情况",
              isinstance(mc.maze.cell(0, 0), dict), str(mc.maze.cell(0, 0)))
        txt = mc.maze.to_text()
        check("to_text 渲染出方格图", "+" in txt and txt.count("\n") >= 7, txt[:60])

        # ---- 收车 → 复位发车状态 → **再次发车**(v2.1 真机缺陷的回归防线) ----
        # 修复前: `MAZE RUN` 只置 linkMazeReq 而不消费它, 加上 LESSON_Blind8() 是
        # while(1) 不返回 —— 发过一次车之后 `MAZE RUN` 永远回 ERR_BUSY, 只能断电
        # (真机复现 2026-10-05)。下面这三段把这个缺陷钉死。
        r2 = mc.maze.run()
        check("已在跑时再发车被拒(ERR_BUSY)", not r2.ok and "BUSY" in r2.status, r2.status)

        rs = mc.maze.stop()
        check("maze.stop 收车成功", rs.ok, rs.status)
        mc.wait(lambda: (mc.maze.state() or {}).get("taskName") == "WAIT", timeout=6.0)
        check("收车后回到 WAIT 态",
              (mc.maze.state() or {}).get("taskName") == "WAIT", str(mc.maze.state()))

        r3 = mc.maze.run()
        check("★ 收车后可以再次发车(修复前此处永久 ERR_BUSY)", r3.ok, r3.status)

        # ---- 急停: 带出迷宫 + 锁存; 解锁后可再发 ----
        re_ = mc.move.estop()
        check("ESTOP 被接受", re_.ok, re_.status)
        r4 = mc.maze.run()
        check("急停锁存中发车被拒(ERR_ESTOP)",
              not r4.ok and "ESTOP" in r4.status, r4.status)
        rr = mc.move.resume()
        check("STOP 解锁急停", rr.ok, rr.status)
        r5 = mc.maze.run()
        check("★ 急停解锁后可再次发车", r5.ok, r5.status)

        # 收尾: 把设备留在空闲态, 不影响后续用例
        mc.maze.stop()
    finally:
        mc.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0, help="已有模拟器端口(省略则自己起一个)")
    args = ap.parse_args()

    test_offline()

    port = args.port or _free_port()
    proc = None
    if not args.port:
        proc = subprocess.Popen(
            [sys.executable, "-m", "host.sim.mouse_sim", "--port", str(port), "--no-auto"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1.2)
    try:
        test_e2e(port)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    print("\n" + "=" * 66)
    print(f"通过 {PASS} / 失败 {FAIL}")
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
