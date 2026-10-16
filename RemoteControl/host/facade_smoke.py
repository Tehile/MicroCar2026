"""门面(MouseClient)冒烟测试 —— P1 验收基线。

验证 N1–N4 与功能组 A–J:
    N1 headless:  import host.api 不引入 Qt
    N2 线程安全:  多线程并发调用不异常、不死锁
    N3 push/pull:  on() 订阅回调 与 events_queue() 拉取 双模式
    N4 可阻塞:    wait() / wait_event() / 各 wait=True 方法

v2.0(2026-10-05): `SNAP`/`SCOPE`/`RATE`/`DUMP` 已随协议精简删除 ——
    · "三帧(fast/mid/slow)" 改为单路统一帧(`telemetry.latest()` 返回单个 dict);
    · "1kHz 波形回传" 改为直接从统一帧环形缓冲取(`telemetry.history_dict()`);
    · `RATE` 删除, 改为验证自时钟(无需调档)与时间锚;
    · `calib.gyro()` 的零偏不再回读(写入 Flash, 非参数), 原断言已删。

用法:
    python facade_smoke.py               # 自动拉起模拟器
    python facade_smoke.py --port 5556   # 连已运行的模拟器
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import threading
import time


from host.sim.mouse_sim import MAZETYPE as FW_N  # 迷宫尺寸跟固件档位(见 host/fwprofile.py)
PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_n1_headless() -> None:
    print("\n[N1] headless(不 import Qt)")
    import host.api  # noqa: F401

    qt = [m for m in sys.modules if "PySide" in m or "pyqtgraph" in m]
    check("import host.api 后无 PySide6/pyqtgraph", not qt, str(qt))


def _run_impl(port: int) -> None:
    from host.api import Event, MouseClient
    from host.protocol.commands import PARAM_IDS

    # ------------------------------------------------------------ 连接(A)
    print("\n[A] 连接管理 + 遥测(B)")
    with MouseClient.connect_to(f"tcp://127.0.0.1:{port}") as mc:
        check("连接成功(is_connected)", mc.is_connected, mc.state_detail)
        check(f"设备自述 mazetype=={FW_N}", mc.device.get("mazetype") == FW_N, str(mc.device))
        check("设备自述含 proto", "proto" in mc.device, str(mc.device))

        check("等待首帧(N4)", mc.telemetry.wait_first_frame(timeout=5.0))
        f = mc.telemetry.latest()
        check("统一帧有数据", bool(f), str(f))
        check("统一帧含左右轮速", "speedL" in f and "speedR" in f, str(f))

        # ------------------------------------------------------------ 事件(N3 push)
        print("\n[N3] 事件 push/pull")
        seen: list = []
        off = mc.on(Event.TELEMETRY, lambda m: seen.append(m))
        q = mc.events_queue()
        mc.wait(lambda: len(seen) > 0, timeout=3.0)
        check("on() 订阅回调收到 TELEMETRY", len(seen) > 0)
        off()
        time.sleep(0.3)
        # pull 模式: 队列里应已积累事件
        try:
            q.get_nowait()
            pulled = True
        except Exception:
            pulled = False
        check("events_queue() 拉取到事件", pulled)

        # ------------------------------------------------------------ 参数(E/D)
        print("\n[E] 参数 + [D] 可靠传输(回读校验)")
        got = mc.params.get_all()
        # 7 标定 + 17 红外/电源 + 17 运动控制 = 41 项(v2.5)。课程命名空间已删除,
        # 这就是远程档固件 `linkParam[LINK_P_NUM]` 的全部内容, 也是 `GET ALL` 的全部输出。
        check("GET ALL 回读 {} 项生产参数".format(len(PARAM_IDS)), len(got) == len(PARAM_IDS), str(len(got)))
        check("生产命名空间 {} 项齐备".format(len(PARAM_IDS)), set(mc.params.production_names()) <= set(got),
              str(sorted(set(mc.params.production_names()) - set(got))))
        r = mc.params.set("TRACKL", 53.2015)
        check("set() ACK+回读校验通过", r.ok, r.status)
        check("回读值一致", abs(mc.params.get("TRACKL") - 53.2015) < 1e-4,
              str(mc.params.get("TRACKL")))
        # 派生: speedRatio = trackRatio / 100
        rs = mc.params.set_many({"TRACKL": 53.2, "TRACKR": 53.2}, derive_speed=True)
        check("set_many+derive_speed 两项成功", all(x.ok for x in rs), str([x.status for x in rs]))
        check("SPEEDL 已派生为 TRACKL/100", abs(mc.params.get("SPEEDL") - 0.532) < 1e-3,
              str(mc.params.get("SPEEDL")))
        bad = mc.params.set("NOPE", 1.0)
        check("非法参数名返回 ERR_NAME", not bad.ok and bad.status == "ERR_NAME", bad.status)

        # ------------------------------------------------------------ 流控
        print("\n[C] 遥测拉取(stream)")
        r = mc.stream.on()
        check("stream.on -> OK", r.ok, r.status)
        # v2.1: 遥测是**拉取式**的 —— stream.on() 启动本机循环请求 TELEM,
        # 每条应答都带**完整 u32 时基**(时间锚), 供逐帧同余还原。
        check("stream.on 建立时间锚", mc.stream.anchor() is not None, str(mc.stream.anchor()))
        check("按'最近有帧'判定流已开启", mc.stream.is_on, str(mc.stream.is_on))

        # ------------------------------------------------------------ 标定(F)
        print("\n[F] 标定(阻塞式)")
        r = mc.calib.gyro()
        check("calib.gyro() 完成(请求-响应: ACK 即已落盘)", r.ok, r.status)
        # v2.0 起零偏写在 FLASH_ADDR_GYRO0(**不是参数**), 没有回读 —— 原
        # "gyro 有零偏值" 断言失去目标, 统一帧里也没有该量, 故删除。

        r = mc.calib.wall_distance()
        check("calib.wall_distance() 完成", r.ok, r.status)

        # IRDATA 逐点: 4 个摆位各发一条 `CAL IRDATA <i>`, 固件采完才回 ACK。
        # (v1.5.5 靠 `CALDONE`/`CALSTEP` 事件行, 那些行会被 TX 空间门静默丢弃。)
        r = mc.calib.ir_data()
        check("calib.ir_data() 采满 4 点", r.ok and r.data.get("points") == 4, str(r.data))
        check("ir_data 未降级回退(无 note 告警)", "note" not in r.data, str(r.data))

        # ------------------------------------------------------------ 动作 + 运动波形(G)
        print("\n[G] 动作 + 统一帧运动波形(≈217Hz 自时钟)")
        import numpy as np

        r = mc.move.forward(pulses=20000, speed=40)
        check("move.forward 回报 STOPPED", r.ok, f"{r.status} {r.data}")
        check("forward 带回停止原因", r.data.get("reason") is not None, str(r.data))
        # v2.0: 运动结束只剩 `STOPPED <reason> #n` 一条事件行; 波形不再靠 SCOPE
        # "停车后整块回传", 而是直接从统一帧环形缓冲取 —— 运动期间本就全速在流。
        h = mc.telemetry.history_dict()
        t_ms = h.get("t", [])
        check("统一帧历史非空", len(t_ms) > 0, str(len(t_ms)))
        nonzero = int((np.asarray(h.get("speedL", [])) != 0).sum())
        check("波形含非零轮速", nonzero > 0, f"nonzero={nonzero}")

        # ------------------------------------------------------------ 迷宫(H)
        print("\n[H] 迷宫")
        m = mc.maze.fetch(timeout=10.0)
        check("maze.fetch 返回墙图", bool(m), str(type(m).__name__))
        if m:
            check(f"墙图 {FW_N}x{FW_N}", m.get("mazetype") == FW_N and len(m["mapWall"]) == FW_N,
                  str(m.get("mazetype")))
            check("position 返回坐标", mc.maze.position() is not None, str(mc.maze.position()))

        # ------------------------------------------------------------ 分析(J)
        print("\n[J] 离线分析")
        # ⚠️ 单位约定: `analyze` 的分段阈值按**毫秒**轴设计(见
        # `analysis/metrics.segment_by_task` 的 duration_ms>=5 过滤), 而
        # `telemetry.history_dict()["t"]` 是宿主的 **epoch 秒**轴 —— 故这里把
        # 时间轴换算成"相对毫秒"再喂给它, 否则几秒的会话会被整段过滤掉。
        h_an = dict(h)
        h_an["t"] = (np.asarray(h["t"]) - h["t"][0]) * 1000.0
        segs = mc.analyze.summary(h_an)
        check("analyze.summary 产出动作段", len(segs) > 0, str(len(segs)))
        check("explain() 返回人可读文本", bool(mc.analyze.explain(h_an)), mc.analyze.explain(h_an))

        # ------------------------------------------------------------ 会话(I)
        print("\n[I] 会话记录")
        check("会话已自动开启", mc.session.is_open)
        mc.session.note("门面冒烟测试")
        c = mc.session.counts()
        check("会话计数含 unified", c.get("unified", 0) > 0, str(c))

        # ------------------------------------------------------------ N2 线程安全
        print("\n[N2] 线程安全(并发调用)")
        errors: list = []

        def worker(i: int) -> None:
            try:
                for _ in range(20):
                    mc.telemetry.latest()
                    mc.params.snapshot()
                    mc.stats()
                if i == 0:
                    mc.params.set("WALLDIST", 42.0, readback=False)
            except Exception as e:  # pragma: no cover
                errors.append(f"{type(e).__name__}: {e}")

        ths = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        check("3 线程并发调用无异常", not errors, str(errors))

        check("断开前状态可读(state_detail)", isinstance(mc.state_detail, str))

        # ------------------------------------------------------------ [K] 卡死救援
        print("\n[K] 卡死救援: conn.soft_reset()（`MCU RESET`）")
        # 背景(真机实测 2026-10-05): 走迷宫的主循环里有大量**非协作式阻塞等待**
        # (等编码器里程/传感器), 而 STOP 会让电机永不再转 ⇒ 那些条件永远不成立
        # ⇒ 程序卡在函数内部, 连 `MAZE STOP` 的退出检查都轮不到 ⇒ 只能断电。
        # `MCU RESET` 是那个"不必断电"的出口: 整机软复位, 程序从 main() 重跑。
        from host.registry import COMMANDS as _C, command_line, spec_of
        check("命令表登记了 system.reset", "system.reset" in _C, str(sorted(_C)[:5]))
        check("system.reset 发的是 'MCU RESET'",
              command_line("system.reset") == "MCU RESET",
              command_line("system.reset"))
        check("system.reset 标记为 urgent(发送前让拉取器让路)",
              bool(spec_of("system.reset").urgent))
        # ⚠️ 与 params.reset(发 `RESET`, 只擦参数) 是两回事 —— 名字像, 作用完全不同
        check("与 params.reset 不是同一命令",
              command_line("params.reset") == "RESET"
              and command_line("system.reset") == "MCU RESET")

        # 模拟一个"卡死现场": 让它在迷宫里跑起来, 然后执行救援
        mc.maze.run()
        # ⚠️ 不能写死 `sleep(0.3)` 后就断言: 慢链路(`--rtt-ms 300`)下主机读到的
        #    最新帧本身带 ~300ms 延迟, 那一刻仍是发车前的 WAIT。改成轮询等状态翻转。
        deadline = time.time() + 5.0
        task = None
        while time.time() < deadline:
            task = (mc.maze.state() or {}).get("taskName")
            if task not in (None, "WAIT"):
                break
            time.sleep(0.05)
        check("救援前: 设备处于非空闲态", task not in (None, "WAIT"), str(task))

        rr = mc.conn.soft_reset(verify=True, timeout=6.0)
        check("soft_reset 复核通过", rr.ok, f"{rr.status} {rr.data}")
        check("复核到的复位原因是 SW(软复位, 区别于掉电 POR)",
              rr.data.get("rst") == "SW", str(rr.data))
        check("复位后设备回到空闲(WAIT)",
              (mc.maze.state() or {}).get("taskName") == "WAIT",
              str((mc.maze.state() or {}).get("taskName")))
        check("复位后链路仍可用(PING OK)", mc.send_cmd("ping", timeout=4).ok)
        # 别名应等价
        rb = mc.conn.rescue(verify=True, timeout=6.0)
        check("rescue() 别名等价可用", rb.ok, f"{rb.status} {rb.data}")
    # with 退出自动断开 + 收会话


def main() -> int:
    ap = argparse.ArgumentParser(description="MouseClient 门面冒烟测试")
    ap.add_argument("--port", type=int, default=0, help="已运行模拟器的端口(默认自动拉起)")
    ap.add_argument("--rtt-ms", type=float, default=0.0,
                    help="给模拟器注入应答往返延迟(ms)。默认 0(亚毫秒) —— "
                         "但真机蓝牙链路实测 RTT≈300ms, 用 --rtt-ms 300 才能复现"
                         "'按快链路写的时序假设'类缺陷(如连接握手的 50ms 等待)")
    args = ap.parse_args()

    print("=" * 66)
    print("电脑鼠上位机 门面冒烟测试")
    if args.rtt_ms > 0:
        print(f"  ⚠ 慢链路模式: 模拟器应答延迟 {args.rtt_ms:.0f} ms(逼近真机蓝牙)")
    print("=" * 66)
    test_n1_headless()

    port = args.port or _free_port()
    proc = None
    if not args.port:
        sim_cmd = [sys.executable, "-m", "host.sim.mouse_sim", "--port", str(port), "--no-auto"]
        if args.rtt_ms > 0:
            sim_cmd += ["--rtt-ms", str(args.rtt_ms)]
        proc = subprocess.Popen(
            sim_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1.2)

    try:
        _run_impl(port)
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
    sys.exit(main())
