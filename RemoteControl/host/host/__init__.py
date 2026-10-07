"""电脑鼠蓝牙上位机。

与固件 HARDWARE/link.c 配对使用; 协议定义见 docs/蓝牙上位机设计方案.md。
可编程接口(门面)见 `host.api`; 接口参考见 docs/api_reference.md。

⚠️ 下面两个版本号必须与实现同步, 否则窗口标题会显示错误版本:
   `__version__`  —— 上位机产品版本, 与 `registry.API_VERSION` 一致;
   `PROTO_VER`    —— 线上协议号, 与固件 `link.h: LINK_PROTO_VER` 一致
                     (v2.1 因命令集变更由 2 进到 3)。
"""

__version__ = "2.1.0"
PROTO_VER = 3

from .registry import API_VERSION  # noqa: E402  (语义化 API 版本, 与固件 proto 分开)

__all__ = ["__version__", "PROTO_VER", "API_VERSION"]
