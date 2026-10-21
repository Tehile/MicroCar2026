"""电脑鼠蓝牙上位机。

与固件 HARDWARE/link.c 配对使用; 协议定义见 docs/通信协议规格.md, 算法见 docs/算法详解.md。
可编程接口(门面)见 `host.api`; 接口参考见 docs/api_reference.md。

⚠️ 下面两个版本号必须与实现同步, 否则窗口标题会显示错误版本:
   `__version__`  —— 上位机产品版本, 与 `registry.API_VERSION` 一致;
   `PROTO_VER`    —— 线上协议号, 与固件 `link.h: LINK_PROTO_VER` 一致
                     (v2.5 因参数表扩容 5->6; v2.6 因新增 RATE 命令族 6->7;
                      v2.6.1 只改统一帧 flags 的 bit3 语义 ⇒ 协议号停 7;
                      v2.7 因 CAL 家族新增 6 个菜单远程化子命令 7->8,
                      帧格式与参数表仍未变;
                      v2.8 只改**发车时 LED 的点亮行为**(远程档 MAZE RUN 瞬间
                      全 4 灯亮 LINK_LAUNCH_MARK_MS 后释放), 命令集/帧格式/
                      参数表一字未动 ⇒ 协议号仍停 8, 靠 PONG 的 fw 号区分)。
                     v2.9 纯上位机: 新增批式拉取 `mc.stream.pace()`(`RATE PULL n t`
                      稀疏命令+每批多帧, 默认 10Hz; 真机压测的 2.5 倍传输量档)与
                      断连自动检测 —— 未新增任何命令/字段 ⇒ 协议号仍停 8。
                     v2.10 纯上位机: **拉取器统一** —— 停等式(`TELEM` 回复门控)与
                       批式(`RATE PULL n t`)合并为唯一的 `TelemetryPuller`
                       (剖面 n: n=1 停等 / n≥2 批式, "后者是前者的拓展"在调度器
                       层面成立); 顺带补两个缺口: 批中点注入 `TELEM` 刷时间锚
                       (每 30s)与停等式的断连判定(7s)。命令集/帧格式/参数表
                       一字未动 ⇒ 协议号仍停 8。
                     v2.10.1 固件侧修复(**host 零改动**): `USER/main.c` 值守循环按键
                       分支的 `BUTTON1` 极性写反(真值 idle=0 / 按下=1), 空闲时
                       `while (BUTTON1 == 0)` 永久自旋 ⇒ main 永不消费 `MAZE RUN`
                       (`MAZE RUN` 回 ACK OK 却永不发车、随后恒 ERR_BUSY)。
                       命令集/帧格式/参数表一字未动 ⇒ 协议号仍停 8, fw 221→222。
                     v2.12 **统一帧重排 49→30 B**(v9): 删原始 ADC / 格内里程 / 控制误差 /
                     原始 goal / 运行时墙阈 / 岔路栈 / 快路径计数 / 丢帧计数 / motionStep /
                     critL,critR / left45,right45 / frontNear; 状态B 拆 B0/B1; 前墙远近
                     并入状态C; 冲刺档位与 goaheadnum 合并为"冲刺上下文"。
                     帧布局不兼容 ⇒ 协议号 8→9, fw 300→310。
"""

__version__ = "2.13.0"
PROTO_VER = 9

from .registry import API_VERSION  # noqa: E402  (语义化 API 版本, 与固件 proto 分开)

__all__ = ["__version__", "PROTO_VER", "API_VERSION"]
