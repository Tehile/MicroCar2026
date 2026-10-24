"""Control —— 电脑鼠整合上位机。

只消费**两个**上游模块，不改它们的源码：
    A) RemoteControl/host   —— 鼠直控与遥测（host.api.MouseClient），**同进程 import**
    B) Auto Locate          —— ArUco 场地定位（`python -m aruco_locator.session`），
                              **起子进程**（本层薄壳 `tools/video_session.py` 转接配置）

⚠️ **改版二十四：手机相机那一路已整块下线** —— `PhoneCamera/pc`、`hostlink.HostLink`、
独立服务 8765、在线帧流定位全部移除。位置数据的唯一来路是"一段视频 → Auto Locate
会话解析 → 轨迹"（`control/adapters/video.py`），**不是**实时帧流。
（本文件的旧版 docstring 曾把它列为上游 B，与 `ctx.py` 冲突，已在本次修正。）

本包**不 import PySide6 / pyqtgraph**（判据 I1）：Web 形态是刻意的选择，
它同时免掉了与 Auto Locate 的环境冲突。

模块组成与依赖见 [`docs/模块组成总览.md`](../docs/模块组成总览.md)。
"""

__version__ = "0.1.0"
#: 与上游 API 的对应关系见 docs/接口对照表.md
API_VERSION = "0.1"
