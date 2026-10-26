"""v10 建图增量通道 —— **真机**端到端验收（需要现场硬件）。

    python tools/map_live_check.py COM7                  # 发车跑 BLIND，走到 12 格或 25 s
    python tools/map_live_check.py COM7 --cells 20 --max-s 40
    python tools/map_live_check.py COM7 --dry            # 不发车，只验"静止零状态"
    python tools/map_live_check.py COM7 --json out.json

**为什么必须有这个脚本**：`drill.py` 全离线可跑，而 v10 的增量通道只有
`contract_test` 的 S10（模拟器对拍）覆盖 —— 那只证明"host 解自己造的帧没解错"。
真机上还有三件事它证明不了：

  ① 固件到底有没有在走格时把 `GucMapBlock` 真的压进环（`link_MapSample` 挂在
     `LINK_Tick` 里，坐标变化的判据是 `GmcMouse` 比对 —— 写错就恒不触发）；
  ② `open4` 的位序与固件 `__wallCheck` 写进去的是不是同一套（低位=可通）；
  ③ 环的推进时机（`linkDrop` 快照比对）在连续走格时会不掉条目。

判据（每条都实判，不打"看起来对"）:

    R0  发车前 `MCU RESET` 成功（`rst=SW`）—— 不清掉上一轮的位姿与增量环，
        本轮的第一条增量会从旧位置接着走，且 L3/L4 查不出来（图静默错位）
    E1  发车前 `CAL ERASEALL` 擦掉 6 页迷宫存档。**只复位不够**：实测复位后
        发车，固件仍会从存档恢复位置与整张图（`GmcMouse` 从 (0,0) 跳到上次
        中断处），增量环被上一轮的格子灌满
    R1  复位后位姿归零 `pos == (0,0)`
    Z1  静止时 `mapd.count == 0`（环空），`entries` 全 0
    L1  发车 `MAZE RUN` 拿到 OK
    L2  车动了（`pos` 变化次数 > 0）
    L3  条目序列**连续**：相邻条目曼哈顿距离恒为 1（总步长 == 条数-1）。
        这是判「没丢条目 / 没被软件搬坐标污染」的正确位置 —— **不能**用 `pos`
        判：`pos` 只是帧里的低频镜像（本轮实测 ~4 Hz），两帧之间走过两格就漏
        中间点，(0,2)→(2,2) 看着像跳变而条目里明明有 (1,2)
    L4  条目坐标在界内，且首条紧接起点（起点格按设计永不进流）
    L5  每条条目的 `open4 != 0`（`__wallCheck` 必置来路位 ⇒ 写过的格恒非 0）
    L6  `lossy == 0`（短跑不该溢出 32 条环）
    L9  **位序硬判**（零噪声）：首访格的 open4 在「来路方向」那一位必须为 1。
        `__wallCheck()` 里 `ucMap |= MOUSEWAY_B` 是无条件置位（utility.c:423），
        所以这条是死约束；位序写反/错位会成片报错（实测：循环左移 1 位 → 9/12
        步报错，四向全取反 → 12/12 步报错）。覆盖率 = 首访步数，输出里会打出来
    L9b **走出去方向**的位（软判）：那一位取自跨格那一瞬的红外，而格边界处前向
        红外会毛刺翻转一次，盖章又是一次性的（utility.c:444 的 `==0x00` 闸门）
        ⇒ 毛刺会被永久烙进图。实测 run4/run5 同一路径只有 (0,1) 一面不同，
        故只报数，>3 面才升 FAIL
    L7  与主机推断图（`MazeWallMap` 走 wallLeft/Front/Right + dir）逐面对账 ——
        **只报数不判**：`wallL/F/R` 是跨格途中的瞬时读数，与 `__wallCheck` 在
        跨格瞬间写 `GucMapBlock` 的判据不同时，天然有差异（实测 1~6 面）
    L8  收车后任务机回到 WAIT（`MAZE STOP` 回 OK 不等于停住了）

安全
----
* 只发 `MCU RESET` / `MAZE RUN` / `MAZE STOP` / `PING` / `TELEM`，不发任何
  裸轮速/标定命令。**发车前会先复位**（`--no-reset` 可跳过，仅上电首跑该用）
* 发车前检 `estop_locked` / 电池；`MAZE STOP` **一定**在 `finally` 里发
* Ctrl-C 也走 finally ⇒ 不会把车留在跑道的 while(1) 里
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from control.adapters import ensure_path            # noqa: E402
from control.map_live import LiveWallMap, MapdRows  # noqa: E402
from control.maze_rec import MazeWallMap            # noqa: E402

_host_dir = ROOT.parent / "RemoteControl" / "host"
ensure_path(str(_host_dir))

from host.api import MouseClient, Event             # noqa: E402

RESULTS: List[tuple] = []


def record(cid: str, verdict: str, note: str, detail: str = "") -> None:
    RESULTS.append((cid, verdict, note, detail))
    tag = {"PASS": "[PASS]", "FAIL": "[FAIL]", "SKIP": "[SKIP]", "INFO": "[INFO]"}[verdict]
    print(f"  {tag} {cid:4s} {note}" + (f"   {detail}" if detail else ""))


FRAME_CSV_HEADER = ["ts", "seq", "posX", "posY", "dir",
                    "wallL", "wallF", "wallR", "frontFar", "frontBoth",
                    "count", "lossy", "resync", "entries"]


def dump_artifacts(json_path: str, out: Dict[str, Any],
                   rows: List[tuple]) -> None:
    """把结果 JSON 与逐帧 CSV 落盘。

    ★ 单独成函数是为了**离线可测**：这段原先内联在 `main()` 末尾，只能靠一次
      真机完整跑才走到，于是 `--json` 一直是坏的而没人发现（本轮实测：全部判据
      PASS 之后在这一行崩 `'tuple' object has no attribute 'port'`，因为 L9 的
      循环变量 `a` 把 argparse 的 Namespace 覆盖了）。
    """
    p = Path(json_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_p = p.with_suffix(".frames.csv")
    with csv_p.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(FRAME_CSV_HEADER)
        w.writerows(rows)
    print(f"\n  结果写入 {json_path}\n  逐帧写入 {csv_p}（{len(rows)} 帧）")


DIRV = {(0, 1): 0, (1, 0): 1, (0, -1): 2, (-1, 0): 3}


def judge_bits(entries: List[tuple]):
    """open4 位序判据。返回 (back_bad, fwd_bad, n_chk, n_skip)。

    硬判（back_bad）：**首次进入**某格那一步，该格记录值里的「来路位」必须为 1。
    `__wallCheck()` 的低半字节是 `B|L|F|R`，其中 B 由 `ucMap |= MOUSEWAY_B`
    **无条件**置（utility.c:423），所以首访格记录下来的 open4 必然含"从哪进来的"
    那一位 —— 这是死约束，位序写反/错位会成片报错。

    软判（fwd_bad）：走出去方向那一位取自跨格那一瞬的红外，格边界处前向红外会
    毛刺翻转一次（实测 run4/run5 同一路径只有 (0,1) 一面不同），而盖章是一次性的
    （utility.c:444 的 `==0x00` 闸门），毛刺会被永久烙进图 ⇒ 只计数，不硬判。

    ★ "首访"必须**按流顺序增量判断**（`k not in visits`），不能先统计全流次数再
    按 `count == 1` 过滤：那样"进入过一次、后来又回访"的格子连它**第一次进入**
    那一步也会被整体跳过。实测代价很大 —— run6 是走到 (7,8) 后原路退回 (0,0)，
    36 条 / 19 格，用全流计数只剩 3 步可判（覆盖率 8%），改成增量判断后覆盖到
    每一个新格的首次进入。

    为什么重访格那一侧不能判：同一个一次性闸门使得重访不再更新，记录值仍是
    **第一次**进入时的快照，按新方向判必假阳性。
    """
    visits: Dict[tuple, int] = {}

    def _bump(k: tuple) -> None:
        visits[k] = visits.get(k, 0) + 1

    if entries:
        _bump((entries[0][0], entries[0][1]))   # 首条已进过的格先登记

    back_bad: List[str] = []
    fwd_bad: List[str] = []
    n_chk = n_skip = 0
    # ★ 循环变量别叫 a/b：`a` 是 argparse 的 Namespace，被覆盖后末尾写 JSON 时
    #   会 `'tuple' object has no attribute 'port'`（本轮实测踩到）。
    for ea, eb in zip(entries, entries[1:]):
        d = DIRV.get((eb[0] - ea[0], eb[1] - ea[1]))
        if d is None:
            continue                          # 非相邻格：由 L3 单独报
        kb = (eb[0], eb[1])
        if kb not in visits:                  # 首次进入该格
            n_chk += 1
            if not (eb[2] >> (d ^ 2)) & 1:
                back_bad.append(
                    f"({eb[0]},{eb[1]}) 首次从 {d} 方向进入，来路位={d ^ 2} 却是 0"
                    f"（open4={eb[2]:x}）")
        else:
            n_skip += 1                       # 重访：记录值是上次的快照，不可判
        _bump(kb)
        # ea 这一次是它的**第一次**出现 ⇒ 这一份记录就是盖章快照，走出去方向的
        # 那一位可判；若是重访后再离开，值已陈旧，跳过。
        ka = (ea[0], ea[1])
        if visits.get(ka, 0) == 1 and not (ea[2] >> d) & 1:
            fwd_bad.append(
                f"({ea[0]},{ea[1]}) 往 {d} 方向走了出去，该位却是 0"
                f"（open4={ea[2]:x}）")
    return back_bad, fwd_bad, n_chk, n_skip


class Acc:
    """帧级累积器。运行在 host 的**读取线程**里，只做 append。"""

    def __init__(self, n: int) -> None:
        self.lock = threading.Lock()
        self.n = n
        self.lm = LiveWallMap(n)
        self.wm = MazeWallMap(n)
        self.frames = 0
        self.md_frames = 0            # 带 count>0 的帧
        self.entries: List[tuple] = []  # (x, y, open4, seq)
        self.track: List[tuple] = []    # 坐标**变化**序列
        self.raw_pos: List[tuple] = []  # 每帧坐标（含重复）
        self.lossy = 0
        self.resync = 0
        self.last_pos: Optional[tuple] = None
        self.bad_open = []              # open4 == 0 的条目
        self.oob = []                   # 越界条目
        #: 逐帧原始观测 —— 对账定性靠它（远墙粒度差异 vs 真错必须能分辨）
        self.rows: List[tuple] = []

    def on_frame(self, msg: Any) -> None:
        # 回调收到的是 EventMsg（不是 dict）；`EventMsg.get()` 直接转发到 .data
        rec = msg.get("rec") if hasattr(msg, "get") else None
        if not rec:
            return
        md = rec.get("mapd") or {}
        x, y = rec.get("posX", 0), rec.get("posY", 0)
        with self.lock:
            self.frames += 1
            self.raw_pos.append((x, y))
            self.rows.append((
                round(float(msg.ts) if hasattr(msg, "ts") else 0.0, 3),
                int(rec.get("_seq") or -1), x, y,
                int(rec.get("dir") or 0),
                int(rec.get("wallLeft") or 0), int(rec.get("wallFront") or 0),
                int(rec.get("wallRight") or 0), int(rec.get("frontFar") or 0),
                int(rec.get("frontBoth") or 0),
                int(md.get("count") or 0), int(md.get("lossy") or 0),
                int(md.get("resync") or 0),
                ";".join("%d:%d:%x" % (int(e["x"]), int(e["y"]), int(e["open4"]))
                         for e in (md.get("entries") or [])),
            ))
            if self.last_pos is None or (x, y) != self.last_pos:
                self.track.append((x, y))
                self.last_pos = (x, y)
            if md.get("resync"):
                self.resync += 1
            if md.get("lossy"):
                self.lossy += 1
            if md.get("count"):
                self.md_frames += 1
            for e in md.get("entries") or []:
                ex, ey, o4 = int(e["x"]), int(e["y"]), int(e["open4"])
                self.entries.append((ex, ey, o4, rec.get("_seq")))
                if o4 == 0:
                    self.bad_open.append((ex, ey))
                if not (0 <= ex < self.n and 0 <= ey < self.n):
                    self.oob.append((ex, ey))
            # 两张图都喂 —— lm 是界面口径（三态），wm 是主机推断（对账）
            try:
                self.lm.feed(rec)
            except Exception:                              # noqa: BLE001
                pass
            try:
                self.wm.feed(rec)
            except Exception:                              # noqa: BLE001
                pass

    def snap(self):
        with self.lock:
            return (self.frames, self.md_frames, list(self.track),
                    list(self.entries), self.lossy, self.resync)


def wait_idle(mc: MouseClient, timeout: float = 5.0) -> Dict[str, Any]:
    """等车真的停住：最新帧的 `mazeTask`/`motionTask` **都归零**。

    ⚠️ 不要用 `mc.maze.state()` —— 它要"新鲜帧"，而 `MAZE STOP` 自己会占住
    「严格停等、在途 ≤1」的那个槽位，拉取器要等它超时才恢复（实测 5 s 内
    一直拿不到 state，于是被误判成"没停住"）。直接读 `telemetry.latest()`
    并检查帧龄，才是"车现在动没动"的真答案。
    """
    t0 = time.monotonic()
    last: Dict[str, Any] = {}
    while time.monotonic() - t0 < timeout:
        f = mc.telemetry.latest() or {}
        age = mc.telemetry.last_frame_age()
        if f and age is not None and age < 1.2:
            last = f
            if int(f.get("mazeTask") or 0) == 0 and int(f.get("motionTask") or 0) == 0:
                return {"ok": True, "frame": f, "elapsed": time.monotonic() - t0}
        time.sleep(0.2)
    return {"ok": False, "frame": last, "elapsed": time.monotonic() - t0}


def render_rows(lm: LiveWallMap, n: int) -> List[str]:
    """文本矩阵。每格一个十六进制 `open4`（低 4 位＝四向可通位，1=通），
    `-` = 这一格从来没被报过。**行序反转**成 Y 向上（与界面一致）。"""
    return list(reversed(MapdRows.dump(lm, n)["rows"]))


def main() -> int:
    ap = argparse.ArgumentParser(prog="map_live_check.py")
    ap.add_argument("port", nargs="?", default="COM7")
    ap.add_argument("--dry", action="store_true", help="不发车，只验静止零状态")
    ap.add_argument("--cells", type=int, default=12, help="走到多少格就收车(默认 12)")
    ap.add_argument("--max-s", type=float, default=25.0, help="最长跑多少秒(默认 25)")
    ap.add_argument("--json", default=None, help="把结果写成 JSON")
    ap.add_argument("--no-countdown", action="store_true", help="跳过发车倒计时")
    ap.add_argument("--no-reset", action="store_true",
                    help="发车前不做 MCU RESET（只在上电后第一次跑时该用）")
    ap.add_argument("--no-erase", action="store_true",
                    help="发车前不擦迷宫存档（保留断点续跑，验收基本不该用）")
    a = ap.parse_args()
    if a.dry:
        a.no_reset = True        # dry 的契约是"零副作用"，不复位

    print("=" * 78)
    print(f"v10 建图增量通道 真机验收 —— {a.port}"
          + ("  [dry: 不发车]" if a.dry else ""))
    print("=" * 78)

    mc = MouseClient()
    if not mc.connect(a.port):
        print("  [FAIL] 串口打不开：%s\n   %s" % (a.port, mc.state_detail))
        return 2
    try:
        # ★ 指纹要在**停流**的状态下取。
        #   `connect()` 里那次 PING 可能被拉取器的首条 TELEM 抢先而丢失（上游对此
        #   是"不阻断连接、静默放过"），于是 `device` 为空。此时若在**开流状态下**
        #   补 PING，会撞上"严格停等、在途 ≤1"：PING 占住槽位 ⇒ TELEM 补不进来 ⇒
        #   Z1 数到 0 帧（实测踩到）。所以必须先 off 再 PING 再 on。
        dev = mc.device
        if not dev.get("proto"):
            mc.stream.off()
            for _ in range(3):
                try:
                    mc.send_cmd("ping", timeout=3.0)
                except Exception:                          # noqa: BLE001
                    pass
                dev = mc.device
                if dev.get("proto"):
                    break
                time.sleep(0.3)
            mc.stream.on()

        if not mc.telemetry.wait_first_frame(timeout=6.0):
            print("  [FAIL] 6 s 内没收到统一帧（板子没在跑 / 烧错档）")
            return 2

        proto, fw = dev.get("proto"), dev.get("fw")
        mt = dev.get("mazetype") or 16
        n = int(mt)
        print(f"  设备: PROTO {proto} / FW {fw} / MAZETYPE {n} / rst {dev.get('rst')}")
        if proto != 10:
            print(f"  [FAIL] 板子里不是 v10 固件（PONG proto={proto}），先烧 Remote/TEST.hex")
            return 2
        record("D1", "PASS" if proto == 10 and fw == 314 else "FAIL",
               f"指纹 proto={proto} fw={fw}", f"mazetype={n}")

        # ---- R0 复位（清掉上一轮的位姿与建图环） ---------------------------
        # **必须在发车前做**。增量环是"从本轮起点开始的一条序列"：上一轮跑完车
        # 停在半路，固件里的 GmcMouse 也还在那儿。不复位就发车，本轮第一条增量
        # 会从旧位置接着走，而 host 以为起点是 (0,0) ⇒ 整张图静默错位，且
        # L3（条目数 1:1）/L4（坐标在轨迹里）都会把"接着走"当成正常而查不出来。
        #
        # 副作用正好是我们要的：`LINK_Init()` 里的 `link_MapReset()` 置 resync
        # ⇒ host 侧清图重来；`GmcMouse`/`GucMapBlock` 回到上电初值 ⇒ 起点格
        # 也回到"未探明"，与物理上的起点角对齐。
        if not a.no_reset:
            mc.stream.off()        # 复位期间停流：PING 与 TELEM 会互抢在途槽位

            # ① 先擦迷宫存档 6 页（BLOCK / BLOCK_POINT / GOPATH / BACKPATH /
            #    GOPATH_90 / BACKPATH_90）。
            #    **只发 MCU RESET 不够**：`maze_run` 里虽然自己会擦这 6 页，但实测
            #    擦完仍能从存档恢复出整张图和位置（见 R2/L9 的实测记录），于是
            #    `GmcMouse` 从 (0,0) 跳到存档位置、`GucMapBlock` 带着上一轮墙值 ⇒
            #    增量环立刻被"上一轮的格子"灌满。必须显式擦一次并复核 ACK。
            if not a.no_erase:
                er = mc.calib.erase_all(confirm=True, timeout=12.0)
                record("E1", "PASS" if er.ok else "FAIL",
                       f"CAL ERASEALL -> {er.status}", "擦 6 页迷宫存档(不可逆)")

            # ② 再整机复位，让 GucMapBlock / GmcMouse / 增量环都回上电初值
            rr = mc.conn.soft_reset(verify=True, timeout=8.0)
            rd = rr.data or {}
            record("R0", "PASS" if rd.get("verified") else "FAIL",
                   f"MCU RESET -> {rr.status}，rst={rd.get('rst')}",
                   f"{rd.get('elapsed_s')}s（rst=SW 才是软复位，POR 是掉电）")
            mc.stream.on()
            if not mc.telemetry.wait_first_frame(timeout=8.0):
                print("  [FAIL] 复位后 8 s 内没恢复统一帧（复位没起来 / 串口断了）")
                return 2
            dev = mc.device
            if dev.get("proto") != 10:
                print(f"  [FAIL] 复位后指纹不对：proto={dev.get('proto')} fw={dev.get('fw')}")
                return 2
            # 复位后固件要先跑完**阻塞式**陀螺标定（车必须静止）。标定期间 PING/TELEM
            # 能应答，但主循环还没起来 ⇒ 这时候发的 MAZE RUN 只会被 ISR 收下排队。
            # 等任务机连续稳定在 0 再往下走，否则发车时刻不可控。
            settle = wait_idle(mc, 6.0)
            print(f"  复位后稳定耗时 {settle['elapsed']:.1f}s"
                  f"（mazeTask={settle['frame'].get('mazeTaskName')}）")
            time.sleep(1.0)

        acc = Acc(n)
        off = mc.on(Event.TELEMETRY, acc.on_frame)

        # ---- Z1 静止零状态 -------------------------------------------------
        time.sleep(1.2)
        f0, md0, tr0, en0, _, rs0 = acc.snap()
        record("Z1", "PASS" if (f0 > 0 and not en0) else "FAIL",
               f"静止 {f0} 帧内增量条目 {len(en0)} 条（应为 0）",
               f"resync 首帧计数={rs0}")

        # ---- R1 复位后位姿归零 ---------------------------------------------
        # 不复位就会带着上一轮的坐标发车（见 R0 的注释）。这一条专防那种情况。
        r1x, r1y = acc.last_pos or (0, 0)
        if not a.no_reset:
            record("R1", "PASS" if (r1x, r1y) == (0, 0) else "FAIL",
                   f"复位后位姿归零 pos=({r1x},{r1y})",
                   f"resync 帧={rs0}（每个复位周期应恰好 1 次）")

        if a.dry:
            off()
            return 0 if all(r[1] != "FAIL" for r in RESULTS) else 1

        # ---- 发车前检 ------------------------------------------------------
        estop = mc.estop_locked
        batt = int((mc.telemetry.latest() or {}).get("battMv") or 0)
        px, py = acc.last_pos or (0, 0)
        print(f"  起点 pos=({px},{py})  电池={batt} mV  急停锁存={estop}")
        if estop:
            record("L0", "FAIL", "急停锁存中，发车会被 ISR 每帧清零速度", "先解锁急停")
            return 1
        if batt and batt < 7000:
            record("L0", "FAIL", f"电池 {batt} mV < 7000，发车会被拦", "充电后重试")
            return 1
        if (px, py) != (0, 0):
            print(f"  ⚠ 起点不是 (0,0) 而是 ({px},{py}) —— 远程档的探索假设"
                  f"\"我在 (0,0)\"，物理位置与内部位姿不一致会按错图行驶。")

        if not a.no_countdown:
            for k in (3, 2, 1):
                print(f"  {k} 秒后发车 …（松手，确认车在起点角）")
                time.sleep(1.0)

        # ---- L1 发车 -------------------------------------------------------
        t_launch = time.monotonic()
        r = mc.maze.run("BLIND")
        record("L1", "PASS" if r.ok else "FAIL",
               f"MAZE RUN -> {r.status}",
               f"{int((time.monotonic() - t_launch) * 1000)} ms")
        if not r.ok:
            off()
            return 1

        # ---- 跑 ------------------------------------------------------------
        deadline = t_launch + a.max_s
        last_n = -1
        try:
            while time.monotonic() < deadline:
                time.sleep(0.25)
                _, _, track, entries, _, _ = acc.snap()
                steps = max(0, len(track) - 1)      # 首点是起点
                if steps != last_n:
                    print(f"    {time.monotonic() - t_launch:5.1f}s  pos={track[-1]}"
                          f"  走格={steps}  增量={len(entries)}")
                    last_n = steps
                if steps >= a.cells:
                    print(f"  达到 {steps} 格，收车")
                    break
        except KeyboardInterrupt:
            print("\n  Ctrl-C —— 立刻收车")
        finally:
            # ---- L8 收车（**必须**无条件发） -------------------------------
            t_stop = time.monotonic()
            rs = mc.maze.stop()
            idle = wait_idle(mc, 5.0)
            dt = time.monotonic() - t_stop
            fr = idle["frame"]
            record("L8", "PASS" if (rs.ok and idle["ok"]) else "FAIL",
                   f"MAZE STOP -> {rs.status}，停住={idle['ok']}",
                   f"{dt:.2f}s  mazeTask={fr.get('mazeTaskName')}"
                   f" motionTask={fr.get('motionTaskName')}"
                   f" speed=({fr.get('speedL')},{fr.get('speedR')})")
            off()

        # ---- 判定 ----------------------------------------------------------
        frames, md_frames, track, entries, lossy, resync = acc.snap()
        cells = [(e[0], e[1]) for e in entries]
        start = track[0] if track else None

        # **连续性一律用"条目序列"判，不用 `pos` 轨迹。**
        # 条目来自固件 1 kHz 采样（每格 129-450 ms，不可能漏），而 `pos` 只是帧里的
        # 低频镜像（本轮实测 ~4 Hz）⇒ 两帧之间走过两格就漏一个中间点。
        # 实测踩到：`pos` 显示 (0,2)→(2,2) 像跳变，而条目里明明有 (1,2) ——
        # 拿 `pos` 判"固件搬坐标"会把欠采样误判成缺陷。
        step_m = [abs(a[0] - b[0]) + abs(a[1] - b[1])
                  for a, b in zip(cells, cells[1:])]
        contiguous = all(d == 1 for d in step_m)
        pos_jumps = [(a, b) for a, b in zip(track, track[1:])
                     if abs(a[0] - b[0]) + abs(a[1] - b[1]) != 1]
        steps = max(0, len(track) - 1)
        print(f"\n  跑了 {frames} 帧 / pos 变化 {steps} / 条目 {len(entries)}"
              f"（带条目帧 {md_frames}）")
        print(f"  pos 相邻跳变 {len(pos_jumps)} 处（低频镜像，漏格属正常，不作判据）")

        record("L2", "PASS" if steps > 0 else "FAIL", f"车动了（pos 变化 {steps} 次）")
        record("L3", "PASS" if (cells and contiguous) else "FAIL",
               f"条目序列连续（{len(cells)} 条 / 无跳步）",
               ("总步长 %d，应 %d" % (sum(step_m), len(cells) - 1)) if cells else "无条目")
        bad_step = [(a, b) for a, b, d in zip(cells, cells[1:], step_m) if d != 1]
        if bad_step:
            record("L3b", "FAIL", f"条目序列有 {len(bad_step)} 处跳步",
                   str(bad_step[:4]))

        # 首条必须紧接起点（起点格按设计不进流，见 MouseSim.mapd_sent 的注释）
        first_ok = bool(cells) and start is not None and \
            abs(cells[0][0] - start[0]) + abs(cells[0][1] - start[1]) == 1
        oob = [c for c in cells if not (0 <= c[0] < n and 0 <= c[1] < n)]
        record("L4", "PASS" if (first_ok and not oob) else "FAIL",
               f"条目坐标合法且首条紧接起点(pos={start})",
               f"首条={cells[0] if cells else None}  越界={oob[:3]}")

        record("L5", "PASS" if not acc.bad_open else "FAIL",
               f"open4 != 0（写过的格必有来路位）",
               str(acc.bad_open[:5]) if acc.bad_open else "")
        record("L6", "PASS" if lossy == 0 else "FAIL",
               f"lossy 帧 {lossy}（短跑不该溢出 32 条环）", f"resync={resync}")

        # ---- L9a 位序硬判（零噪声，见 judge_bits 的注释） -------------------
        back_bad, fwd_bad, n_chk, n_skip = judge_bits(entries)
        record("L9", "PASS" if not back_bad else "FAIL",
               f"open4 来路位与进入方向自洽（位序硬判：{n_chk} 步，跳过重访 {n_skip} 步）",
               str(back_bad[:4]) if back_bad else "")

        # ---- L9b 走出去方向的位（软判，见 judge_bits 的注释） ---------------
        if not fwd_bad:
            record("L9b", "PASS", "走出去方向的位全为 1（无红外毛刺被烙进图）")
        else:
            record("L9b", "INFO" if len(fwd_bad) <= 3 else "FAIL",
                   f"走出去方向位缺失 {len(fwd_bad)} 面"
                   f"（一次性盖章烙进了跨格红外毛刺，非通道错误）",
                   str(fwd_bad[:3]))

        # L7 是**对账诊断**，不是判据 —— `MazeWallMap` 由 wallL/F/R（跨格途中的
        # 瞬时读数）反推，与 `__wallCheck()` 在跨格瞬间写 GucMapBlock 的判据
        # 本来就不完全同时刻，天然有差异（本轮实测 5/32）。位序正确性由 L9 硬判。
        diff = acc.lm.face_diff(acc.wm, n)
        record("L7", "INFO",
               f"与主机推断图逐面对账：共知 {diff['common']} 面 / 不一致 {diff['mismatch']}",
               "对账诊断不作判据（瞬时读数 vs 跨格落地）；位序由 L9 判")

        # ---- 图 ------------------------------------------------------------
        st = acc.lm.stats()
        print(f"\n  重建统计: {st}")
        rows = render_rows(acc.lm, n)
        print("\n  重建图（Y 向上，X 向右；每格=open4 十六进制，`-` 未报过）:")
        for i, line in enumerate(rows):
            print(f"    {n - 1 - i:2d} {line}")

        out = {
            "port": a.port, "proto": proto, "fw": fw, "n": n,
            "frames": frames, "steps": steps, "entries": len(entries),
            "contiguous": contiguous, "pos_jumps": pos_jumps,
            "lossy": lossy, "resync": resync,
            "face_diff": diff, "stats": st, "track": track,
            "entry_list": entries,
        }
        if a.json:
            dump_artifacts(a.json, out, acc.rows)

    finally:
        try:
            mc.disconnect()
        except Exception:                                  # noqa: BLE001
            pass

    npass = sum(1 for r in RESULTS if r[1] == "PASS")
    nfail = sum(1 for r in RESULTS if r[1] == "FAIL")
    print("\n" + "=" * 78)
    print(f"汇总: PASS {npass} / FAIL {nfail}")
    print("=" * 78)
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
