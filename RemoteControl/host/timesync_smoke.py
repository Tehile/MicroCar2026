"""数据收集与时间同步冒烟测试 —— 把"数据的完整性"和"时间的准确性"变成可断言的事实。

要回答七个问题, 每个都是现场真会出错的地方:

    T1 纯函数     偏移(min-delay)/频差(ppm)/8 位时间戳还原, 在已知真值上算得对
    T2 发车       `ARM` 被接受, 应答体带 `OK <armMs>`, 之后统一帧按期到达
    T3 逐帧时间   统一帧(带 8 位时间戳)能被还原成正确的完整设备时刻
    T4 时间轴     统一帧能还原到**本机单调时刻**, 且时间轴与真实时间同速、落在发车之后
    T5 丢帧检测   上行消息序号不连续能被数出来(gap 次数 + 丢失条数)
    T6 数据完整性 会话落盘齐全(raw.bin / raw.ts / 统一帧 CSV / events.log / text.log / meta.json), CSV 里有 dev_ms 列
    T7 远程发车   `MAZE RUN` 生效(统一帧 mazeTask=SEARCH)、重复发车被拒

v2.0(2026-10-05): 时间同步从"独立 1Hz 时间帧"改为"**时间锚 + 每帧 ms8**":
    - `TELEM` 的应答 `ACK <seq> OK <upMs>` 给出完整 u32 时基(锚, 每次拉取都刷新);
    - 统一帧每帧只带低 8 位 `ms8`, 主机用 `resolve_ms8(predicted, ms8)` 同余还原;
    - `ARM` 的发车时基随**同一条 ACK** 回(不再单发 `ARMED <ms>` 行);
    - 旧的 1kHz dump(`SCOPE`)与其绝对时间轴整体删除, 时间轴改由统一帧序列承载;
    - 帧与事件行**共用**同一个 1 字节上行序号, 故 T5 比较的是"相邻上行消息"。

用法(在 host/ 目录下):
    python timesync_smoke.py
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from typing import List

PASS = 0
FAIL = 0
FAILED: List[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f"  <- {detail}" if detail else ""))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _Sim:
    def __init__(self, *extra: str) -> None:
        self.port = _free_port()
        self.cmd = [sys.executable, "-m", "host.sim.mouse_sim",
                    "--port", str(self.port), "--no-auto", *extra]
        self.proc: subprocess.Popen | None = None

    @property
    def target(self) -> str:
        return f"tcp://127.0.0.1:{self.port}"

    def __enter__(self) -> "_Sim":
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        time.sleep(1.2)
        return self

    def __exit__(self, *exc) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def _connect(sim: "_Sim", **kw):
    from host.api import MouseClient

    mc = MouseClient(**kw)
    if not mc.connect(sim.target):
        raise RuntimeError(f"连接失败: {mc.state_detail}")
    mc.telemetry.wait_first_frame(timeout=5.0)
    return mc


# ---------------------------------------------------------------- T1 纯函数

def test_pure_functions() -> None:
    print("\n[T1] 时间同步纯函数(已知真值)")
    from host.api.timesync import (
        drift_ppm, fit_clock, min_delay_offset, resolve_ms8,
    )

    # 真值: 偏移 100.0s, 设备与 PC 同速, 单程延迟 3ms 恒定
    samples = [(100.003 + i, i * 1000) for i in range(5)]
    off = min_delay_offset(samples)
    check("min_delay_offset 还原偏移", off is not None and abs(off - 100.003) < 1e-6,
          str(off))

    # 设备比 PC 慢 2%(每 PC 秒只走 980 设备毫秒) -> ppm ≈ -20000
    slow = [(float(i), int(i * 980)) for i in range(10)]
    ppm = drift_ppm(slow)
    check("drift_ppm 识别 -2% 频差", ppm is not None and abs(ppm - (-20000)) < 100,
          str(ppm))
    fit = fit_clock(slow)
    check("fit_clock 斜率 ≈ 0.98", fit is not None and abs(fit[1] - 0.98) < 1e-6, str(fit))

    check("样本不足时返回 None", min_delay_offset([]) is None and fit_clock([(0.0, 0)]) is None)

    # 8 位还原(v2.0 参数顺序: (predicted_up_ms, ms8) —— 先有预测/锚再还原):
    #   预测 5000ms(误差 0) -> 低字节 0 应还原成同余类里最接近 5000 的 5120
    check("resolve_ms8 精确预测", resolve_ms8(5000, 0) == 5120, str(resolve_ms8(5000, 0)))
    check("resolve_ms8 预测偏 +127 仍正确", resolve_ms8(5120 + 127, 0) == 5120,
          str(resolve_ms8(5120 + 127, 0)))
    check("resolve_ms8 预测偏 -127 仍正确", resolve_ms8(5120 - 127, 0) == 5120,
          str(resolve_ms8(5120 - 127, 0)))
    # 低字节取自"完整时基的低 8 位": 大于 256 的设备时刻也能唯一还原
    check("resolve_ms8 任意同余值可还原",
          all(resolve_ms8(3000 + v, (3000 + v) & 0xFF) == 3000 + v
              for v in range(0, 5000, 371)))


# ---------------------------------------------------------------- T2 发车

def test_arm(sim: "_Sim") -> None:
    print("\n[T2] ARM 发车(应答带 armMs)")
    with _connect(sim) as mc:
        check("连接后未发车", mc.timesync.snapshot().armed is False)
        r = mc.timesync.arm()
        check("ARM ACK OK", r.ok, r.status)
        # v2.0: 发车时基随同一条 ACK 回到 `arm_up_ms`, 不再有独立的 ARMED 行。
        deadline = time.time() + 2.0
        while time.time() < deadline and mc.timesync.arm_up_ms is None:
            time.sleep(0.05)
        check("ACK 带发车时基并记录", mc.timesync.arm_up_ms is not None,
              str(mc.timesync.arm_up_ms))
        check("wait_sync 拿到足够样本", mc.timesync.wait_sync(samples=3, timeout=6.0))
        st = mc.timesync.snapshot()
        check("snapshot.armed 为真", st.armed)
        check("offset 已收敛", st.offset_s is not None)
        check("样本数 >= 3", st.samples >= 3, str(st.samples))


# ---------------------------------------------------------------- T3 逐帧时间

def test_per_frame_time(sim: "_Sim") -> None:
    print("\n[T3] 每个统一帧都能定位到准确时刻")
    from host.api import Event

    with _connect(sim) as mc:
        mc.timesync.arm()
        mc.timesync.wait_sync(samples=3, timeout=6.0)
        # 收集一组统一帧, 检查 ms8 -> 完整设备时刻的还原是否自洽
        got: List = []
        off = mc.on(Event.TELEMETRY,
                    lambda m: got.append(m.data["rec"])
                    if m.data.get("stream") == "unified" else None)
        try:
            time.sleep(1.5)
        finally:
            off()
        check("收到统一帧", len(got) >= 5, str(len(got)))
        ups = [mc.timesync.frame_time_up(r) for r in got]
        check("每帧都能还原出设备时刻", all(u is not None for u in ups), str(ups[:5]))
        if all(u is not None for u in ups):
            # 设备时基必须单调不减, 且相邻帧的间隔应是帧周期的整数倍(不会跳 256)
            steps = [ups[i + 1] - ups[i] for i in range(len(ups) - 1)]
            check("还原出的设备时刻单调不减", all(s >= 0 for s in steps), str(steps[:6]))
            check("相邻帧间隔不含 256 级跳变(未误配窗口)",
                  all(abs(s) < 250 for s in steps), str(steps[:6]))
            # ms8 必须与还原值同余
            ok = all((u & 0xFF) == r["ms8"] for u, r in zip(ups, got))
            check("还原值与 ms8 同余", ok)


# ---------------------------------------------------------------- T4 时间轴

def test_time_axis(sim: "_Sim") -> None:
    print("\n[T4] 统一帧时间轴: 还原到本机时刻且与真实时间同速")
    from host.api import Event

    with _connect(sim) as mc:
        mc.timesync.arm()
        mc.timesync.wait_sync(samples=2, timeout=6.0)
        check("时间锚已建立", mc.store.get_anchor() is not None)
        got: List = []
        off = mc.on(Event.TELEMETRY,
                    lambda m: got.append(m.data["rec"])
                    if m.data.get("stream") == "unified" else None)
        try:
            time.sleep(1.0)
        finally:
            off()
        check("收到统一帧", len(got) >= 30, str(len(got)))
        tps = [mc.timesync.frame_time(r) for r in got]
        check("每帧都能还原到本机单调时刻", all(x is not None for x in tps))
        if all(x is not None for x in tps) and len(tps) >= 2:
            # 时间轴跨度必须与真实流逝同速(差 <= 一个帧周期量级), 否则轴是错的
            rx = [float(r.get("_rx_mono") or 0.0) for r in got]
            span_t = tps[-1] - tps[0]
            span_rx = rx[-1] - rx[0]
            check("时间轴跨度与真实流逝同速", abs(span_t - span_rx) < 0.05,
                  f"span_t={span_t:.4f} span_rx={span_rx:.4f}")
            steps = [tps[i + 1] - tps[i] for i in range(len(tps) - 1)]
            check("本机时刻轴单调不减", all(s >= -1e-6 for s in steps), str(steps[:6]))
            ups = [mc.timesync.frame_time_up(r) for r in got]
            arm_ms = mc.timesync.arm_up_ms
            check("还原出的设备时刻落在发车之后",
                  all(u is not None and u >= arm_ms - 5 for u in ups),
                  f"min={min(u for u in ups if u is not None)} arm={arm_ms}")


# ---------------------------------------------------------------- T5 丢帧检测

def test_seq_gap_detection() -> None:
    print("\n[T5] 上行消息序号间隙 -> 丢帧计数(帧/事件共用计数器)")
    from host.model.ring import TelemetryStore
    from host.protocol.codec import TYPE_UNIFIED, Frame, pack_unified
    from host.protocol.commands import CommandClient
    from host.transport.link import Link

    store = TelemetryStore()
    cmd = CommandClient(send_bytes=lambda b: None)
    lnk = Link(store=store, client=cmd)
    blank = pack_unified({})

    def feed(seq: int) -> None:
        lnk._dispatch_frame(Frame(type=TYPE_UNIFIED, seq=seq, payload=blank))

    feed(10)
    check("首帧不计丢帧", lnk.counts["seq_lost"] == 0, str(lnk.counts["seq_lost"]))
    feed(11)
    check("连续帧不误报", lnk.counts["seq_lost"] == 0)
    feed(14)  # 跳了 12, 13 -> 丢 2 条
    check("间隙计入 gap 次数", lnk.counts["seq_gap"] == 1, str(lnk.counts["seq_gap"]))
    check("间隙计入丢失条数(=2)", lnk.counts["seq_lost"] == 2, str(lnk.counts["seq_lost"]))
    feed(15)
    check("恢复后继续正常", lnk.counts["seq_gap"] == 1)
    # 256 回绕: 255 -> 0 是连续的, 不该误报
    store.up_last_seq = 255
    feed(0)
    check("256 回绕不误报", lnk.counts["seq_gap"] == 1, str(lnk.counts["seq_gap"]))

    # 帧与事件**共用**同一 1 字节计数器: 帧号"跳 1"是正常的(中间插了一条事件行),
    # 只有逐条比较(而不是逐帧比较)才不会把它误判成丢帧 —— 这是 v2.0 的关键点。
    store2 = TelemetryStore()
    lnk2 = Link(store=store2, client=CommandClient(send_bytes=lambda b: None))

    def feed2(seq: int) -> None:
        lnk2._dispatch_frame(Frame(type=TYPE_UNIFIED, seq=seq, payload=blank))

    feed2(40)
    store2.note_event_seq(41)   # 事件行占用同一计数器
    lnk2._sync_seq_counts()
    feed2(42)
    check("帧与事件交替(帧号不连续)不误报",
          lnk2.counts["seq_gap"] == 0, str(lnk2.counts["seq_gap"]))


# ---------------------------------------------------------------- T6 数据完整性

def test_session_integrity(sim: "_Sim") -> None:
    print("\n[T6] 会话落盘完整性 + dev_ms 列")
    tmp = tempfile.mkdtemp(prefix="timesync_sess_")
    mc = None
    path = None
    try:
        mc = _connect(sim, session_dir=tmp)
        mc.timesync.arm()
        mc.timesync.wait_sync(samples=2, timeout=6.0)
        time.sleep(1.5)
        path = mc.session.path
        check("会话已打开", path is not None and os.path.isdir(path), str(path))
        counts = mc.session.counts()
        check("会话计数含统一帧", counts.get("unified", 0) > 0, str(counts))
        # v2.0: `ARM` 不再回一行 `ARMED <ms>` 文本, 而是把发车时基塞进 ACK
        # (`ACK <seq> OK <armMs>`) —— 所以"文本计数"不再是发车的判据,
        # 改为直接验证 ACK 里的时基被解析出来了(这才是 v2.0 的等价能力)。
        st = mc.timesync.snapshot()
        check("ARM 时基经 ACK 回来(armed + arm_up_ms)",
              st.armed and st.arm_up_ms is not None, str(st.to_dict()))
    finally:
        # meta.json 只在 close() 时写盘 —— 所以必须先收尾再检查文件
        if mc is not None:
            mc.close()

    if not (path and os.path.isdir(path)):
        check("会话目录存在", False, str(path))
        return
    files = set(os.listdir(path))
    check("raw.bin 已落盘", "raw.bin" in files, str(sorted(files)))
    check("raw.ts 已落盘", "raw.ts" in files)
    check("统一帧 CSV 已落盘", "stream_unified.csv" in files, str(sorted(files)))
    check("events.log 已落盘", "events.log" in files)
    check("text.log 已落盘", "text.log" in files)
    check("meta.json 已落盘", "meta.json" in files)

    csv_path = os.path.join(path, "stream_unified.csv")
    if os.path.exists(csv_path):
        with open(csv_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        check("CSV 有表头且有数据", len(lines) >= 2, str(len(lines)))
        if lines:
            cols = lines[0].split(",")
            check("CSV 含 dev_ms 列", "dev_ms" in cols, str(cols[:8]))
            check("CSV 含 seq 列", "seq" in cols, str(cols[:8]))
            if len(lines) > 1 and "dev_ms" in cols:
                i = cols.index("dev_ms")
                vals = [ln.split(",")[i] for ln in lines[1:] if ln]
                check("dev_ms 列非空(发车后应有值)",
                      any(v.strip() != "" for v in vals), str(vals[:5]))

    rb = os.path.join(path, "raw.bin")
    if os.path.exists(rb):
        check("raw.bin 非空", os.path.getsize(rb) > 0, str(os.path.getsize(rb)))


# ---------------------------------------------------------------- T7 远程发车

def test_remote_launch(sim: "_Sim") -> None:
    print("\n[T7] 远程发车 MAZE RUN")
    with _connect(sim) as mc:
        mc.timesync.arm()
        mc.timesync.wait_sync(samples=2, timeout=6.0)
        f0 = mc.telemetry.latest()
        check("发车前不在迷宫态", int(f0.get("mazeTask", -1)) == 0,
              str(f0.get("mazeTask")))
        r = mc.timesync.maze_run()
        check("MAZE RUN ACK OK", r.ok, r.status)
        # mazeTask 搭在统一帧上(每帧约 5ms), 几乎立刻可见 —— 用轮询而不是死等
        deadline = time.time() + 3.0
        f = {}
        while time.time() < deadline:
            f = mc.telemetry.latest()
            if int(f.get("mazeTask", -1)) == 2:
                break
            time.sleep(0.05)
        check("统一帧上报迷宫任务 = SEARCH(2)", int(f.get("mazeTask", -1)) == 2,
              str(f.get("mazeTask")))
        check("统一帧出现运动任务码(真的在跑)",
              int(f.get("motionTask", 0)) != 0, str(f.get("motionTask")))
        r2 = mc.timesync.maze_run()
        check("重复发车被拒(ERR_BUSY)", r2.status == "ERR_BUSY", r2.status)


# ---------------------------------------------------------------- 入口

def main() -> int:
    print("=" * 66)
    print("电脑鼠 数据收集与时间同步 冒烟测试")
    print("=" * 66)

    test_pure_functions()
    test_seq_gap_detection()

    with _Sim() as sim:
        test_arm(sim)
    with _Sim() as sim:
        test_per_frame_time(sim)
    with _Sim() as sim:
        test_time_axis(sim)
    with _Sim() as sim:
        test_session_integrity(sim)
    with _Sim() as sim:
        test_remote_launch(sim)

    print("\n" + "=" * 66)
    print(f"通过 {PASS} / 失败 {FAIL}")
    if FAILED:
        print(f"失败项: {FAILED}")
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
