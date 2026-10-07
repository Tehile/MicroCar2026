"""上位机入口。

用法:
    python run_host.py

无硬件时的完整演练:
    终端 A:  python -m host.sim.mouse_sim            # 模拟器, 监听 127.0.0.1:5555
    终端 B:  python run_host.py                       # 上位机, 连接里选“[模拟器] TCP 127.0.0.1:5555”

有硬件时:
    先与蓝牙 SPP 设备配对(Windows 会生成一个“传出”COM 口),
    然后在上位机里点“刷新”, 选带 [蓝牙] 标记的那个口, 点“连接”。
"""

from __future__ import annotations

import os
import sys

# 允许从任意工作目录启动
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QPalette, QColor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from host.ui.main_window import MainWindow  # noqa: E402


def apply_dark(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor("#1e1e1e"))
    pal.setColor(QPalette.WindowText, QColor("#e0e0e0"))
    pal.setColor(QPalette.Base, QColor("#242424"))
    pal.setColor(QPalette.AlternateBase, QColor("#2b2b2b"))
    pal.setColor(QPalette.Text, QColor("#e0e0e0"))
    pal.setColor(QPalette.Button, QColor("#333333"))
    pal.setColor(QPalette.ButtonText, QColor("#e0e0e0"))
    pal.setColor(QPalette.Highlight, QColor("#1565c0"))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ToolTipBase, QColor("#2b2b2b"))
    pal.setColor(QPalette.ToolTipText, QColor("#e0e0e0"))
    app.setPalette(pal)


def main() -> int:
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    apply_dark(app)
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
