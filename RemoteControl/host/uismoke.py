"""GUI 冒烟测试: 离屏(offscreen)构建主窗口, 接模拟器跑一段, 并截图。

用途: 在没有硬件、甚至没有显示器的情况下验证界面能真实构建并跑通,
并产出可查看的截图作为回归基线。

v2.1: 上行只有统一帧一路; 遥测由**主机按需拉取**(TELEM), 连接后自动开始(见 MainWindow._pull_tick)。

    python uismoke.py     # 产出 docs/ui_live.png / ui_fetch.png / ui_params.png / ui_calib.png
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication  # noqa: E402

from host.transport.link import Target  # noqa: E402
from host.ui.main_window import MainWindow  # noqa: E402
from run_host import apply_dark  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs")


def pump(app: QApplication, seconds: float) -> None:
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        time.sleep(0.005)


def free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main() -> int:
    port = free_port()
    sim = subprocess.Popen(
        [sys.executable, "-m", "host.sim.mouse_sim", "--port", str(port)],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ok = True
    try:
        app = QApplication(sys.argv)
        apply_dark(app)
        w = MainWindow()
        w.resize(1600, 1000)
        w.show()
        pump(app, 0.4)

        w.link.open(Target(kind="tcp", host="127.0.0.1", tcp_port=port))
        w.btn_connect.setText("断开")
        pump(app, 0.5)

        print(f"链路状态: {w.link.state} {w.link.state_detail}")
        if not w.link.is_open:
            print("!! 链路未打开")
            ok = False

        # v2.1: 连接后 MainWindow 已自动开始**停等拉取**(TELEM), 不需要再开流。
        # 这里只补一条墙图请求, 再让模拟器探索一会儿产生统一帧。
        w.client.send("MAP DUMP")
        pump(app, 4.0)

        st = w.link.stats()
        print(f"帧统计: 统一帧={st['unified']} 墙图={st['map']} 文本={st['text']} 事件={st['event']}")
        print(f"吞吐: {st['rx_bps']:.0f} B/s")
        print(f"迷宫: {'已收到' if w.store.get_map() else '未收到'}")
        if st["unified"] < 100:
            print("!! 统一帧过少")
            ok = False
        if not w.store.get_map():
            print("!! 未收到墙图")
            ok = False

        os.makedirs(OUT_DIR, exist_ok=True)
        p1 = os.path.join(OUT_DIR, "ui_live.png")
        w.grab().save(p1)
        print(f"已截图 {p1}")

        # ---- 统一帧查看/导出页 ----
        w.right_tabs.setCurrentWidget(w.fetch)
        pump(app, 0.6)
        f = w.store.latest()
        key = ("speedL", "speedR", "gyroAngle", "battMv")
        nz = sum(1 for k in key if f.get(k))
        print(f"最新帧: {len(f)} 个字段, 关键通道有值 {nz}/{len(key)}")
        if not f:
            print("!! 无最新帧")
            ok = False
        p2 = os.path.join(OUT_DIR, "ui_fetch.png")
        w.grab().save(p2)
        print(f"已截图 {p2}")

        # ---- 参数面板 ----
        w.client.get_all()
        pump(app, 0.6)
        w.right_tabs.setCurrentWidget(w.params)
        pump(app, 0.4)
        p3 = os.path.join(OUT_DIR, "ui_params.png")
        w.grab().save(p3)
        print(f"已截图 {p3}")

        # ---- 标定向导 ----
        w.right_tabs.setCurrentWidget(w.wizard)
        pump(app, 0.4)
        p4 = os.path.join(OUT_DIR, "ui_calib.png")
        w.grab().save(p4)
        print(f"已截图 {p4}")

        w.link.close()
        pump(app, 0.2)
    finally:
        try:
            sim.terminate()
            sim.wait(timeout=3)
        except Exception:
            sim.kill()

    print("GUI 冒烟: " + ("通过" if ok else "失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
