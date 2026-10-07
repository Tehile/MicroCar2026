"""Control —— 电脑鼠整合上位机。

只消费三个上游模块，不改它们的源码：
    A) RemoteControl/host   —— 鼠直控与遥测（host.api.MouseClient）
    B) PhoneCamera/pc       —— 手机俯拍采集（hostlink.HostLink，独立服务 8765）
    C) Auto Locate          —— ArUco 场地定位（aruco_locator）

本包**不 import PySide6 / pyqtgraph**（判据 I1）：Web 形态是刻意的选择，
它同时免掉了与 Auto Locate 的环境冲突。
"""

__version__ = "0.1.0"
#: 与上游 API 的对应关系见 docs/接口对照表.md
API_VERSION = "0.1"
