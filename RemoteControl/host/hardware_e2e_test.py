"""真机 GUI 端到端验证: 用真实 MainWindow 连 COM7, 走完整 UI 流程。

验证点:
  1) 端口列表里 COM7 排在最前且默认选中
  2) 点"连接"后 open 成功, 按钮变"断开", 波特率下拉被禁用
  3) 自动开始拉取遥测(TELEM 请求-响应)
  4) 遥测真的进来了(帧计数增长)
  5) 断开时自动停止拉取
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from host.ui.main_window import MainWindow  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def pump(app, sec: float) -> None:
    t0 = time.time()
    while time.time() - t0 < sec:
        app.processEvents()
        time.sleep(0.01)


def console_text(w: MainWindow) -> str:
    """ConsoleWidget 的文本实际存在内部的 .view(QPlainTextEdit) 里。"""
    view = getattr(w.console, "view", None)
    if view is not None and hasattr(view, "toPlainText"):
        try:
            return view.toPlainText()
        except Exception:
            pass
    for attr in ("toPlainText", "text"):
        f = getattr(w.console, attr, None)
        if callable(f):
            try:
                return f()
            except Exception:
                pass
    return ""


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    w = MainWindow()
    print("=" * 72)
    print("真机 GUI 端到端验证 (COM7)")
    print("=" * 72)

    # 1) 端口默认选择
    cur = w.cb_target.currentText()
    print(f"  下拉框当前项: {cur!r}")
    check("默认选中的是 COM7", "COM7" in cur, cur)

    # 2) 点连接
    w._toggle_connect()
    pump(app, 1.0)
    print(f"  按钮文本: {w.btn_connect.text()!r}   状态: {w.link.state}")
    check("连接成功(按钮变'断开')", w.btn_connect.text() == "断开")
    check("波特率下拉已禁用", not w.cb_baud.isEnabled())
    check("已创建会话目录", w.session is not None)

    txt = console_text(w)
    check("日志里有 TELEM 记录", "TELEM" in txt, txt[-120:])

    # 3) 静置排空, 再等遥测
    print("  静置 8 秒让模块缓冲排空 ...")
    pump(app, 8.0)
    print("  已开始拉取, 采集 20 秒 ...")
    t0 = time.time()
    while time.time() - t0 < 20.0:
        app.processEvents()
        time.sleep(0.02)
    st = w.link.stats()
    print(f"  帧统计: 统一 {st['unified']}  墙图 {st['map']}  文本 {st['text']}  事件 {st['event']}")
    print(f"  带宽: {st['rx_bps']:.0f} B/s   坏CRC {st['bad_crc']}  失步 {st['bad_sync']}  "
          f"丢序 {st['seq_lost']}")
    check("收到统一帧", st["unified"] > 200, str(st["unified"]))
    check("零坏 CRC", st["bad_crc"] == 0, str(st["bad_crc"]))
    check("零失步", st["bad_sync"] == 0, str(st["bad_sync"]))

    f = w.store.latest()
    if f:
        print(f"  统一帧样本: speedL={f.get('speedL')} speedR={f.get('speedR')} "
              f"gyro={f.get('gyroAngle')} task={f.get('motionTaskName')} "
              f"batt={f.get('battMv')}mV irL={f.get('irL')} irR={f.get('irR')}")
    check("统一帧数据非空", bool(f))

    chk = w.link.self_check()
    print(f"  链路自检: {chk}")
    check("链路自检判定为 ok", chk["level"] == "ok", str(chk))

    # 4) 断开
    w.btn_connect.setText("断开")
    w._toggle_connect()
    pump(app, 0.8)
    check("断开后按钮回到'连接'", w.btn_connect.text() == "连接")
    check("断开后波特率下拉恢复", w.cb_baud.isEnabled())

    print("\n" + "=" * 72)
    print(f"通过 {PASS} / 失败 {FAIL}")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
