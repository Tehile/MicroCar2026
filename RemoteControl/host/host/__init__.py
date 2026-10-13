"""电脑鼠蓝牙上位机。

与固件 HARDWARE/link.c 配对使用; 协议定义见 docs/通信协议规格.md, 算法见 docs/算法详解.md。
可编程接口(门面)见 `host.api`; 接口参考见 docs/api_reference.md。

⚠️ 下面两个版本号必须与实现同步, 否则窗口标题会显示错误版本:
   `__version__`  —— 上位机产品版本, 与 `registry.API_VERSION` 一致;
   `PROTO_VER`    —— 线上协议号, 与固件 `link.h: LINK_PROTO_VER` 一致
                     (v2.5 因参数表扩容 5->6; v2.6 因新增 RATE 命令族 6->7,
                      v2.6.1 只改统一帧 flags 的 bit3 语义, 命令集与帧格式都没动
                      ⇒ **协议号停在 7**, 靠 PONG 的 fw 号(218->219)区分)。
"""

__version__ = "2.6.1"
PROTO_VER = 7

from .registry import API_VERSION  # noqa: E402  (语义化 API 版本, 与固件 proto 分开)

__all__ = ["__version__", "PROTO_VER", "API_VERSION"]
