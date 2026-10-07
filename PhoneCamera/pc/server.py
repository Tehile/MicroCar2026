#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PC 端接收服务（M1 最小链路，走 D24：连续流 + 按 sensor_timestamp 抽帧交付）。

设计依据：`手机相机采集与回传_方案设计.md`
  · §4.1 双车道（D6）：WS = 控制 + 监视（缩略图/状态），HTTP = 交付（全质量图像分片）
  · §6.1 技术选型（D8）：aiohttp 单进程（HTTP 分片 + WS + 静态面板）
  · §6.2 发现与配对（D9）：动态枚举网卡 → `http://<ip>:<port>/?token=…` → 终端二维码
  · §6.3 落盘规范：sessions/<session_id>/{manifest.json, frames/, thumbs/, incoming/, logs/}
  · §7.1 WS 消息：hello / config / start / stop / status / thumb / ack / error
  · §7.2 HTTP 接口：files/init · files/{id} (PUT 分片) · files/{id}/complete · sessions
  · §5.6 待传队列与断网容错（D13/D14）：幂等键 = session_id + device_serial + seq；
         只有服务端校验通过（分片完整 + 整文件 sha256）才置 verified 并回 ACK

校验策略（§7.2）：分片级 CRC32 快速失败 + 文件级 SHA-256 终检。
限速：交付道限速，避免打满 Wi-Fi 影响控制道与监视道。

**为什么把"未验证"单独表示**：本项目的判据体系里"没跑"≠"没问题"
（见 `tools/m0_evaluate.py` 的 INCOMPLETE 与 R16）。服务端同理 ——
文件状态只有 partial / verified / failed 三种，**没有"看起来传完了"这一档**；
`verified` 只由整文件 SHA-256 通过产生。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from aiohttp import WSMsgType, web

MAX_THUMBS = 200        # 监视道每会话保留的最新缩略图数（可随时清理的一类数据）

PROTOCOL_VERSION = 1
DEFAULT_PORT = 8765
CHUNK_MAX = 4 * 1024 * 1024          # 单片上限（App 侧建议 1 MiB）
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_\-. ]{1,80}$")

# --------------------------------------------------- 对外接口面（被整体架构调用）
#
# 依据 docs/对外接口与单一交付道.md。要点：本项目从"一个自己用的调试程序"变成
# "整体架构里的采集组件" ⇒ 需要一层**给别的程序用的**契约（版本声明、机器可读状态、
# 事件广播、实时流），并且这层契约**必须与设备侧接口分开保护**（见下面的 loopback 门控）。
# 版本纪律：**只要对外行为变了就 +1**（哪怕只改一行）。1.14.0 相对 1.13.1 是纯新增
# （pair 三形态、capabilities.console/routes、hostlink 五个便利方法），向后兼容；
# **1.15.0** 新增 `capture_mode=live`（会话级模式）与 `POST /api/v1/live/frame`
# （设备面实时道），并给 `session` / `host/status` 增加字段 —— 老调用方不受影响，
# 但**行为语义**多了一种（同一个会话可能"没有留存"），所以 minor +1。
SERVER_VERSION = "1.15.0"

# 两条载荷性质完全不同的道。**不许互相借道**（详见文档 §1）：
#   · 监视道 = 缩略图 / MJPEG，实时、**可丢**；
#   · 交付道 = **每一帧**的原始 Y 平面（不抽帧、不压缩），一帧不落。
#
# ⚠ 第 24 轮**合并**：以前还有第三条"归档道（每一帧、采集结束后回传）"。用户口径
# 明确为"每一帧都留"之后，交付道本身就是那条道 —— 于是 `kind` 只剩一个合法值。
# 保留 `KIND_DELIVERY` 与 `kind` 字段的**校验**（见 h_init）是为了让老客户端
# （≤1.12.0，会带 `kind="archive"`）**显式报错**，而不是静默写进交付帧的 seq 空间。
KIND_DELIVERY = "frame"

# ---- 会话级采集模式（第 27 轮新增）----
#
# `deliver`（默认，现状）：交付道 = 每一帧原始 Y 先落手机本地 → 边采边传 → 停采排空。
# `live`（新增）：交付道**整体关闭**（不落盘 / 不排空 / 不回传），只留**实时道**，
#   允许**彩色**；实时道走独立 HTTP 二进制（`POST /api/v1/live/frame`，JPEG），
#   App 侧是**串行闭环**（发完一帧才取下一帧）⇒ 延迟不累积。
#
# ⚠ 这个模式**跑完不留任何东西**（R38）：`store.applicable=false`、
# `delivery_complete` 判 **`null`（不适用）而非 `false`**。前者是本道没有这个概念，
# 后者是"判了、且没传完"——把两者混为一谈，现场会去"重传"一份根本不存在的留存。
MODE_DELIVER = "deliver"
MODE_LIVE = "live"
MODES = (MODE_DELIVER, MODE_LIVE)
# ---- 颜色来源（第 29 轮）----
# `yuv_bt601` = 本进程读 Y/U/V 平面、自己按 BT.601 有限范围换算（现状）；
# `rgba_hal`  = 用 `ImageFormat.RGBA_8888` 让**相机 HAL** 做 YUV→RGB，我们只下采样+编码。
#
# ★ 它的用途是**判决性实验**：现场 `yuv_bt601` 档偏绿，而同场景相机原生照片正常。
#   若 `rgba_hal` **也偏绿** ⇒ 我们的 BT.601 换算是清白的（责任在 HAL 或显示链路）；
#   若只有 `yuv_bt601` 偏绿 ⇒ 责任在我们的换算里。两条路必有一条绿 ⇒ 归属唯一确定。
#
# ⚠ 与 `MODES` 同一纪律：非法取值**显式 400**，因为它是**结论**的输入 ——
#   静默退回默认会让"我请求了 A"与"我跑的是 B"看起来一样，而结论会整个反过来。
COLOR_SRC_YUV_BT601 = "yuv_bt601"
COLOR_SRC_RGBA_HAL = "rgba_hal"
COLOR_SOURCES = (COLOR_SRC_YUV_BT601, COLOR_SRC_RGBA_HAL)
# live 帧必须是 JPEG（SOI 标记）：本道只承载"看得见"的画面，不接受原始平面。
# 这条校验不是洁癖 —— 没有它，有人把 2.76 MB 的原始 Y 灌进来也能"成功"，
# 而那份数据既不是完整的帧（没有元数据）也不是可解码的图（全是垃圾）。
JPEG_SOI = b"\xff\xd8"
LIVE_FRAME_MAX = 4 * 1024 * 1024     # 单帧上限（960×720 q70 ≈ 119 KB，留足余量）
# 实时道的**采集路径标识**：产出物（彩色 JPEG）与 `stream_deliver_yuv`（原始 Y）
# 根本不是同一种东西 ⇒ 必须是一条**独立的路径**，不能用同一 id 的 rev 表达。
# 它与 App 侧 `DeliverySession.LIVE_PATH_ID` 和 `tools/capture_path_versions.json`
# 三处必须一致（判据 A9l 交叉检查）。
LIVE_PATH_ID = "live_stream_color"
# ⚠ v1.15.0：1 → 2（与 App 侧 `DeliverySession.LIVE_PATH_REV` 和登记表**三处一致**，
#   判据交叉检查）。新增 `color_source=rgba_hal`：YUV→RGB 改由相机 HAL 完成。
#   ⚠ 交付道的 `stream_deliver_yuv` 仍为 3 —— **一个字节都没动**。
LIVE_PATH_REV = 2


def norm_mode(v) -> str:
    """把外部输入归一成合法采集模式（非法 ⇒ `deliver`）。

    ⚠ 归一**不许静默改意图**：老客户端不带这个字段是"不知道有模式"（退回现状正确），
    但**带了一个拼错的值**是"用户想要另一种行为" —— 后者必须能看见。
    所以调用方在 `v` 非空且非法时要记一笔（见 h_ws 的 session_begin / hello）。
    """
    m = str(v or "").strip().lower()
    return m if m in MODES else MODE_DELIVER

MJPEG_BOUNDARY = "phonecamera-frame"
MJPEG_QUEUE = 2          # 每个 MJPEG 订阅者的队列长度：满则丢最旧（监视道可丢）
HOST_EVENT_QUEUE = 512   # 每个事件订阅者的队列长度：满则丢最旧，但**必须计数**

# 只对本机（loopback）开放的路径。**只列新增接口** ——
# 既有的 `/api/v1/sessions`、缩略图、面板都是已经写在排查手册里的调试入口，
# 手机浏览器打开面板本身就是文档里的"5 秒判定法"，不能被这层门控误伤。
LOCAL_ONLY_PREFIXES = ("/api/v1/host/", "/stream/")


def is_local_only(path: str) -> bool:
    """该路径是否只允许本机访问。

    ⚠ 按**对端地址**判、不按 `Host` 头判：`Host` 是客户端可任意伪造的，
    按它判等于没判 —— 而这里要防的恰恰是"同一局域网里另一台机器来读状态/看画面"。
    """
    return path.startswith(LOCAL_ONLY_PREFIXES)


def is_loopback_addr(host: str) -> bool:
    h = (host or "").strip()
    if h.startswith("::ffff:"):          # IPv4-mapped IPv6
        h = h[7:]
    if h in ("::1", "localhost"):
        return True
    return h.startswith("127.")


def peer_is_local(req: web.Request) -> bool:
    tr = req.transport
    peer = tr.get_extra_info("peername") if tr is not None else None
    if not peer:
        # 拿不到对端地址 ⇒ **不当作本机**（fail closed）。
        # 反过来写（拿不到就放行）会让这层门控在某种部署下静默失效。
        return False
    host = peer[0] if isinstance(peer, (tuple, list)) and peer else str(peer)
    return is_loopback_addr(str(host))

# 帧级元数据里必须逐帧一致（"参数漂移"检查的默认字段集）。
# 依据 §5.4：profile 记的是**回读值**；这些量一旦中途变化，内参就不再是同一套。
DRIFT_FIELDS = ("focus_distance_diopter", "zoom_ratio", "af_mode", "ois_mode", "eis_mode",
                "edge_mode", "noise_reduction_mode", "exposure_time_ns", "iso",
                "sensor_pixel_mode", "active_physical_id", "output_size")


# --------------------------------------------------------------- 枚举比对（写法归一）
#
# **为什么需要这一层**（第 20 轮真机实测踩到，代价是整条流被误停）：
# profile 里 ois/eis 写的是**名字**（`"OFF"`），而 App 报的是**名字+码**（`"OFF(0)"`）。
# 门控原来直接 `str(got).upper() == str(want).upper()` ⇒ `"OFF(0)" != "OFF"` ⇒
# 判成"硬项不一致" ⇒ 自动停流。用户看到的现象就是"点了开始采集，画面刷一下就再也不动了"。
#
# 这类缺陷的性质与 G18/G24/G26 相同：**判据把"写法差异"当成了"事实差异"**。
# 修法必须落在判据侧（而不是让 App 改写法），因为写法本身是自描述的优点 ——
# `"OFF(0)"` 同时给出名字与原始码，比裸 `"OFF"` 信息更多。
#
# 三条纪律：
#   · 名字优先：两边都有名字就比名字（`"OFF(0)"` vs `"OFF"` ⇒ 一致）；
#   · 其次比码：都没有名字（或都有码）就比整数码（`0` vs `"OFF(0)"` ⇒ 一致）；
#   · **认不出且对不上 ⇒ 判不一致**（保守：宁可误停也不放过真的配置偏移）。
_ENUM_RE = re.compile(r"^\s*(?P<name>[A-Za-z][A-Za-z0-9_]*)\s*\(\s*(?P<code>-?\d+)\s*\)\s*$")

# 只在"一侧只有名字、另一侧只有码"时用来补齐（例：`--profile` 里写 ois=0）。
# ⚠ 同一个码在不同枚举里含义不同（2 对 AF 是 AUTO、对 EIS 是 PREVIEW_STABILIZATION），
# 所以这张表**只在必要时**查，且一旦两边都带名字就根本不查它。
_ENUM_CODES = {
    "OFF": 0, "ON": 1, "AUTO": 2, "INACTIVE": 3, "ACTIVE": 4,
    "ZERO_SHUTTER_LAG": 5, "HIGH_QUALITY": 6, "FAST": 7,
    "PREVIEW_STABILIZATION": 2,
}


def _enum_parts(v) -> tuple[Optional[str], Optional[int]]:
    """把"回读值/请求值"拆成 (名字大写, 整数码)，认三种写法：`"OFF"` / `"OFF(0)"` / `0`。"""
    if v is None:
        return None, None
    if isinstance(v, bool):
        return None, int(v)
    if isinstance(v, (int, float)):
        f = float(v)
        return (None, int(f)) if f.is_integer() else (None, None)
    s = str(v).strip()
    m = _ENUM_RE.match(s)
    if m:
        return m.group("name").upper(), int(m.group("code"))
    if s.lstrip("-").isdigit():
        return None, int(s)
    return s.upper(), None


def enum_eq(want, got) -> bool:
    """枚举语义相等（跨"名字/名字+码/裸码"三种写法）。见本段顶部三条纪律。

    ⚠ 两边**都带码**时码也必须相等：`"OFF(2)"` 与 `"OFF(0)"` 判**不一致**。
    名字优先不等于忽略码 —— 名字与码自相矛盾时，那是数据有毛病，
    应该让人看见（保守方向：宁可误停，也不把自相矛盾的记录当成"一致"）。
    """
    wn, wc = _enum_parts(want)
    gn, gc = _enum_parts(got)
    if wn is not None and gn is not None:
        if wn != gn:
            return False
        return True if (wc is None or gc is None) else wc == gc
    if wc is not None and gc is not None:
        return wc == gc
    if wn is not None and gc is not None and wn in _ENUM_CODES:
        return _ENUM_CODES[wn] == gc
    if gn is not None and wc is not None and gn in _ENUM_CODES:
        return _ENUM_CODES[gn] == wc
    return False


# --------------------------------------------------------------------------- 工具

# 虚拟网卡的名字里常见这些词（VMware / VirtualBox / Hyper-V / WSL / Wi-Fi Direct…）。
# 它们**永远连不上手机**，但会和真实网卡一起出现在候选里 —— 实测就踩过：
# 台机上 `192.168.221.1`(VMnet1) 与 `192.168.18.1`(VMnet8) 都在，而真实局域网是 `10.205.x.x`。
VIRTUAL_HINTS = ("vmware", "virtualbox", "hyper-v", "vethernet", "wsl", "loopback",
                 "wi-fi direct", "bluetooth", "tap-", "tun", "npcap", "virtual",
                 "docker", "radmin", "zerotier")


def egress_ip() -> Optional[str]:
    """拿"系统真正会用哪张网卡出门"的那张卡的地址（不真的发包）。

    它比"枚举所有地址"强：**有默认路由的那张卡才是手机能到的那个网段**。
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def adapter_map() -> dict[str, str]:
    """IPv4 → 网卡别名（借 PowerShell 的 `Get-NetIPAddress`，Windows 自带）。

    拿不到就返回空表：地址仍然列得出来，只是少了"这张是什么卡"的注解 ——
    排查时那句注解往往正是关键，所以值得多花一次调用。
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-NetIPAddress -AddressFamily IPv4 | "
             "Select-Object IPAddress,InterfaceAlias | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=8, encoding="utf-8", errors="replace")
        data = json.loads((out.stdout or "[]").strip() or "[]")
        if isinstance(data, dict):
            data = [data]
        return {d["IPAddress"]: str(d.get("InterfaceAlias") or "")
                for d in data if isinstance(d, dict) and d.get("IPAddress")}
    except Exception:
        return {}


def lan_addresses(prefer: Optional[str] = None) -> list[tuple[str, str, bool]]:
    """枚举本机地址，并按"手机最可能连得上"排序（D9：不硬编码 IP）。

    返回 `[(ip, 网卡别名, 是否虚拟网卡)]`，顺序即推荐顺序：
      1. `--lan-ip` 指定者（现场兜底，排第一）
      2. **出口 IP**（有默认路由的那张卡 —— 手机在同一 Wi-Fi 下必能到）
      3. 其它非虚拟地址
      4. 虚拟网卡（VMware/VirtualBox/… 排在最后，并明确标注）
    """
    names = adapter_map()
    seen: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in seen and not ip.startswith("127.") \
                    and not ip.startswith("169.254."):     # 169.254 = 没拿到 DHCP
                seen.append(ip)
    except Exception:
        pass
    eg = egress_ip()
    if eg and eg not in seen and not eg.startswith("127."):
        seen.insert(0, eg)
    if prefer and prefer not in seen:
        seen.insert(0, prefer)

    def is_virtual(ip: str) -> bool:
        n = (names.get(ip) or "").lower()
        return any(h in n for h in VIRTUAL_HINTS)

    out: list[tuple[str, str, bool]] = []
    for ip in sorted(seen, key=lambda a: (0 if a == prefer else 1,
                                          0 if a == eg else 1,
                                          1 if is_virtual(a) else 0, a)):
        out.append((ip, names.get(ip, "?"), is_virtual(ip)))
    return out


def err(status: int, code: str, message: str) -> web.Response:
    """**错误也必须是 JSON**：App 端统一按 JSON 解析响应体，
    若错误走 text/plain，客户端会拿到解析异常而不是错误码 —— 它就没法按 code 决定
    "重传这一片"还是"从头传"，只能一律当网络故障重试。契约要一致。"""
    return web.json_response({"error": code, "message": message}, status=status)


@web.middleware
async def loopback_only_middleware(req: web.Request, handler):
    """本机接口的隔离层（见 docs/对外接口与单一交付道.md §2.1）。

    设备侧接口（`/api/v1/files/*`、`/ws`）**天生必须监听 `0.0.0.0`**（手机要连进来），
    于是本机接口与设备接口**共用同一个端口**：不加这一层，"读状态 / 看实时画面"就
    顺带暴露给了整个局域网。处置是按**对端地址**判（`peer_is_local`），
    **不是**按 `Host` 头判 —— 后者可伪造。
    """
    if is_local_only(req.path) and not peer_is_local(req):
        return err(403, "not_local",
                   f"{req.path} 只对本机（loopback）开放：它读的是本机状态/画面，"
                   f"不对外提供服务。若确实要远程访问，请在本机做转发（本项目不支持反向代理）。")
    return await handler(req)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def make_session_id() -> str:
    return time.strftime("%Y-%m-%d_%H%M") + "_" + secrets.token_hex(2)


def print_qr(url: str) -> None:
    """终端二维码（D9 第 2 步）。qrcode 缺失时**降级但不失败** —— 手输 IP 是兜底。"""
    try:
        import qrcode  # type: ignore
    except Exception:
        print("  ⚠ 未安装 qrcode（`pip install qrcode`）—— 请在 App 里手输上面的地址与 token")
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    m = qr.get_matrix()
    # 用半块字符把两行压成一行，手机屏幕好扫
    for r in range(0, len(m), 2):
        line = ""
        for c in range(len(m[0])):
            top = m[r][c]
            bot = m[r + 1][c] if r + 1 < len(m) else False
            line += "█" if top and bot else ("▀" if top else ("▄" if bot else " "))
        print("  " + line)


# --------------------------------------------------------------------------- 配对码（扫码连接）
#
# 契约：**格式由 App 侧锚定**。`android/.../PairCode.kt` 里的
# SCHEME / ACTION / KEY_HOST / KEY_PORT / KEY_TOKEN / DEF_PORT 常量与 SELFTEST 第 1 条
# 会被 `pc/test_server.py` 第 26 组读出来，反算一遍与这里比对
# ⇒ 这里改格式会立刻弄红测试，而不是等到现场"扫码没反应"。
#
# 为什么用自定义 scheme 而不是直接放面板 URL：
#   ① **可判别**：扫到"不是配对码"的东西（微信号、文章链接）时能明确拒绝并说原因，
#      而不是把 `https://mp.weixin.qq.com/...` 填进地址框然后显示"连接失败"；
#   ② 更短 ⇒ 二维码模块更少 ⇒ 拍屏幕更容易解出；
#   ③ 与面板 URL 解耦：以后换 UI 路由不影响配对。

PAIR_SCHEME = "m0probe"
PAIR_ACTION = "pair"
PAIR_KEY_HOST = "h"
PAIR_KEY_PORT = "p"
PAIR_KEY_TOKEN = "t"
PAIR_DEF_PORT = 8765
# 与 App 的 PairCode.DEF_PORT 必须一致（`--port` 的默认值就是它）


def pair_uri(host: str, port: int, token: str) -> str:
    """规范配对码。手机扫码 / 面板复制 / 手抄都用这一串。"""
    return (f"{PAIR_SCHEME}://{PAIR_ACTION}?{PAIR_KEY_HOST}={host}"
            f"&{PAIR_KEY_PORT}={port}&{PAIR_KEY_TOKEN}={token}")


QR_SCALE = 6          # SVG 里每模块多少像素（矢量，放大不糊，这只是缺省视口大小）
QR_QUIET = 2          # **静区**：二维码四周必须留的空白模块数。少了扫码率会明显下降。


def qr_modules(text: str, quiet: int = QR_QUIET) -> Optional[list]:
    """二维码的**模块矩阵**（`True` = 黑）。

    为什么把这一步单独抽出来：同一个码要能同时以 **SVG（面板）/ PNG（存盘、贴文档）/
    矩阵（任何语言的调用方自己画）** 三种形态给出去。三份实现各算一次，就会出现
    "面板上的码能扫、别的程序画出来的不能扫" —— 而且**看起来都对**（都是二维码）。
    所以三者**必须**共用这一个矩阵（判据 `A8f` 逐格比对）。

    纠错等级取 **Q(25%)**：拍屏幕有反光/摩尔纹，多花点模块换抗误码是划算的
    （配对码只有几十字符，版本不会大）。qrcode 库缺失时返回 None，
    **降级为手输配对码，不算失败**。
    """
    try:
        import qrcode  # type: ignore
    except Exception:
        return None
    try:
        qr = qrcode.QRCode(border=quiet, error_correction=qrcode.constants.ERROR_CORRECT_Q)
        qr.add_data(text)
        qr.make(fit=True)
        return qr.get_matrix()
    except Exception:
        return None


def qr_matrix_json(text: str, quiet: int = QR_QUIET) -> Optional[dict]:
    """二维码的**语言无关**表示：每行一个 `0/1` 字符串。

    为什么要有这一形态：SVG 只有浏览器/矢量渲染器能画，PNG 要 Pillow。
    而调用方可能是个 C++/Go/嵌入式上位机 —— 给它 0/1 矩阵，任何语言都能自己画。

    ⚠ **必须把静区一起说出来**（`border` 字段）：只给黑模块的边界而不说四周要留几格白，
    调用方照着画就会把静区画没，码**看着完全正常但扫不出来**（这是二维码最隐蔽的坑）。
    `size` 是含静区的总模块数（`rows` 里每行的字符数 = `size`）。
    """
    m = qr_modules(text, quiet)
    if not m:
        return None
    return {
        "size": len(m), "border": quiet,
        "bit": "1=黑（模块）0=白（背景）；rows 含静区，直接按 size×size 画即可",
        "rows": ["".join("1" if v else "0" for v in row) for row in m],
    }


def qr_png_b64(text: str, scale: int = 8, quiet: int = QR_QUIET) -> Optional[str]:
    """二维码的 **PNG（base64）**：给"要存盘 / 贴进文档 / 丢给别的工具"的调用方。

    base64 而不是二进制流：这样它能和其他配对信息一起放在**同一个 JSON**里，
    调用方一次请求就拿到全部（不必再为图片单独开一条请求、也不必把 token 拼进图片 URL
    ——把凭据放进日志里最常见的路径就是"图片 URL 带参数"）。

    ⚠ `scale=8`（每模块 8 像素）：扫码是**光学**过程，1 像素/模块的图是"理想化到不存在"，
    打印或屏幕缩放后直接不可解。宁可大一点。
    qrcode/Pillow 任一缺失时返回 None（**降级为 SVG 或手输，不算失败**）。
    """
    m = qr_modules(text, quiet)
    if not m:
        return None
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return None
    try:
        n = len(m)
        img = Image.new("1", (n * scale, n * scale), 1)        # 1 = 白底
        px = img.load()
        for r in range(n):
            for c in range(n):
                if m[r][c]:
                    for dy in range(scale):
                        for dx in range(scale):
                            px[c * scale + dx, r * scale + dy] = 0
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def qr_formats(text: str) -> dict:
    """三形态**各自可用性**的显式声明。

    为什么要把"不可用"也说出来：`qr_svg: null` 单看只像"没生成"，
    调用方无从知道是"依赖没装"还是"这串码编不出来"⇒ 于是不知道该重试、该装包、
    还是该退回手输。**缺席必须有解释**（本项目一贯做法：判不了 ≠ 没问题）。
    """
    have_qr = qr_modules(text) is not None
    svg = qr_svg(text)
    mat = qr_matrix_json(text)
    png = qr_png_b64(text)
    missing = []
    if not have_qr:
        missing.append("qrcode（`pip install qrcode`）")
    if have_qr and png is None:
        missing.append("Pillow（`pip install pillow`，只有 PNG 形态需要）")
    return {
        "svg": svg is not None, "matrix": mat is not None, "png": png is not None,
        "error_correction": "Q", "quiet_zone": QR_QUIET,
        "missing_deps": missing,
        "fallback": ("三形态都不可用时**不算失败**：按 `pair_uri` / `url` 手输一样能连"
                     "（App 支持手输与粘贴）。"),
    }


def qr_svg(text: str, scale: int = QR_SCALE, quiet: int = QR_QUIET) -> Optional[str]:
    """把文本渲染成**内联 SVG**（面板直接塞进 DOM），供手机对着屏幕扫。

    为什么默认是 SVG：面板里就是塞进 DOM，SVG 是矢量、放大不糊，且**不必传二进制**
    （面板 HTML 里内联进去，零额外请求）。调用方要存盘/贴文档时用 `qr_png_b64`。

    qrcode 库缺失时返回 None：**降级为手输配对码，不算失败**。
    """
    m = qr_modules(text, quiet)
    if not m:
        return None
    n = len(m)
    side = n * scale
    # 一条 path 画完所有黑模块，比 N 个 <rect> 小一个数量级
    cmds = []
    for r in range(n):
        for c in range(n):
            if m[r][c]:
                cmds.append(f"M{c * scale} {r * scale}h{scale}v{scale}h-{scale}z")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{side}" height="{side}" '
            f'viewBox="0 0 {side} {side}" shape-rendering="crispEdges" '
            f'style="width:100%;height:auto;display:block" '
            f'role="img" aria-label="扫码配对（含 token）">'
            f'<rect width="{side}" height="{side}" fill="#ffffff"/>'
            f'<path d="{"".join(cmds)}" fill="#000000"/></svg>')


# --------------------------------------------------------------------------- 数据模型

@dataclass
class FileRecord:
    file_id: str
    session_id: str
    device_serial: str
    seq: int
    name: str
    size: int
    expect_sha256: Optional[str]
    meta: dict
    part: Path
    # ⚠ 第 24 轮删掉了 `kind` 字段：两条道合并成一条之后它只剩一个取值，
    # 留在幂等键里反而会让人以为"还有多条道的语义差异"。完整教训见
    # Session.store_decl 上方那段注释（将来再加道时**必须**把它加回去）。
    final: Optional[Path] = None
    received: int = 0
    verified: bool = False
    failed_reason: Optional[str] = None
    crc_failures: int = 0
    created_at: float = field(default_factory=time.time)

    @property
    def state(self) -> str:
        if self.verified:
            return "verified"
        return "failed" if self.failed_reason else "partial"


@dataclass
class Session:
    session_id: str
    device_serial: str
    root: Path
    model: str = ""
    app_version: str = ""
    calib_id: str = ""
    delivery: str = "stream"
    capture_path_id: str = ""
    capture_path_rev: int = 0
    profile: dict = field(default_factory=dict)
    profile_id: str = ""
    started_at: str = field(default_factory=now_iso)
    ended_at: Optional[str] = None
    plan: dict = field(default_factory=dict)
    state: str = "idle"                  # idle|armed|running|stopped
    frames: dict[int, FileRecord] = field(default_factory=dict)
    thumbs: int = 0
    logs: list[str] = field(default_factory=list)
    last_status: dict = field(default_factory=dict)
    # (monotonic, bytes) —— 每次 verified 追加一条，供 verify_rate_mbps 用
    verify_marks: list = field(default_factory=list)
    # 监视道最新一张的索引（名字/序号/字节/时间戳）—— 写进 manifest，面板也用它
    thumb_latest: dict = field(default_factory=dict)
    # PC 主控的闭环回执（§7.1）：start_ack 证明"相机真的开了"，stop_ack 说明"停在哪"
    start_ack: dict = field(default_factory=dict)
    stop_ack: dict = field(default_factory=dict)
    gate_checked: bool = False               # 只在**判出定论**（pass/fail）后置位
    gate_incomplete_logged: bool = False     # incomplete 只打印一次，免得逐帧刷屏
    start_gate_cache: dict = field(default_factory=dict)
    # App 声明与已记录值冲突的溯源字段（空则补、不同则记，见 absorb_declaration）
    decl_conflicts: list = field(default_factory=list)
    # ---- 交付收尾（"停采集 ≠ 停交付"）----
    # 双流方案里交付道（HTTP）**独立于**控制道（WS）：停采集只是不再产生新帧，
    # 队列里已经抽好的帧仍要传完。所以"停了以后还有帧到"是**预期行为**，必须计数，
    # 否则它看起来就像旧会话串台（第 21 轮把这条做成可观测）。
    stopped_at: Optional[str] = None               # 采集停止的时刻（收到 stop_ack / PC 发 stop）
    late_frames_after_stop: int = 0                # 停采集后仍到达的帧数（收尾的正常产物）
    delivery_done: dict = field(default_factory=dict)   # App 宣告"队列已清空、全部 verified"

    # ---- 本地留存账本（**每一帧都留**；第 24 轮合并后只有一条道）----
    # ⚠ 第 24 轮**合并**：以前"交付道（抽帧·实时）"与"归档道（每一帧·采集后回传）"
    # 是两条道，共用同一个 seq 空间 ⇒ 幂等键要含 `kind`、落盘要分目录、内存要分两张表
    # （同一个根因的三个出口，只改一处就是静默覆盖）。
    # 用户口径明确为"每一帧都留"之后两条道合成一条，三个出口**全部收敛回一处**：
    #   · 幂等键 = `sid|serial|seq`（不再有 kind）；
    #   · 落盘   = `frames/frame_%06d.yuv`（不再有 `archive/aframe_*`）；
    #   · 内存   = 只有 `s.frames`（不再有 `s.archives`）。
    # ⚠ 但那条教训**必须留在注释里**：将来**再出现两条共用 seq 空间的流**时，幂等键必须
    #   把"道的名字"加回去 —— 否则后到的会**静默覆盖**先到的，而两边都显示"已验证"。
    #
    # 这份 dict 由 App 的 `status.store`（每 2 s）与 `delivery_done` 更新，内容形如
    # `frames_expected / frames_stored_on_device / gaps_on_device / gap_ts /
    #  seq_first / seq_last / warmup_frames_skipped / result_missing / every_frame`。
    store_decl: dict = field(default_factory=dict)
    store_decl_at: float = 0.0     # 最近一次收到宣告的时刻（判"这个账本新不新"）

    # ---- 采集模式（第 27 轮）----
    # `deliver` = 每一帧都留（默认）；`live` = 只看不留（交付道整体关闭）。
    # ⚠ 它必须在**会话创建时**就定下来（来自 start 的 plan），不能事后改：
    # 中途切换会让同一个 session_id 下的数据一半有留存、一半没有。
    capture_mode: str = MODE_DELIVER
    # 颜色来源（第 29 轮）。⚠ **请求值与实际值必须分两个字段**：
    #   `color_source` 是"我请求的"，`color_source_effective` 是"设备实际跑的"
    #   （HAL 不支持 RGBA_8888 时会被降级）。只报请求值 ⇒ 面板显示"我在跑 rgba_hal"，
    #   而设备其实在跑 yuv_bt601 ⇒ **二分实验白做，且表面上看不出任何异常**。
    color_source: str = COLOR_SRC_YUV_BT601
    color_source_effective: str = ""      # 设备 status 上报的空串 = 尚未拿到
    # live 道的账本：**PC 自己数**的那一半（App 自报的在 `live_decl` 里）。
    # 两边都要有 —— 只看 App 自报等于采信被测程序；只看 PC 计数则看不出"App 想发多少"。
    live_frames: int = 0                                   # PC 收到的 live 帧数
    live_bytes: int = 0                                    # PC 收到的 live 字节数
    live_last: dict = field(default_factory=dict)          # 最近一帧的索引（seq/ts/bytes/at）
    live_rejected: int = 0                                 # 被拒的 live 帧（非 JPEG / 未知会话）
    live_decl: dict = field(default_factory=dict)          # App 通过 status/delivery_done 宣告的 live 账本
    live_decl_at: float = 0.0
    # **模式互斥的反例计数**（判据 A9d/A9e 就盯这一对）：
    # live 会话里收到交付帧（`/api/v1/files/*`）说明"关闭交付道"没做到；
    # deliver 会话里收到实时帧说明 App 站错了道。**两边都必须能看见**。
    mode_conflicts: int = 0
    mode_conflict_last: str = ""
    # (monotonic, bytes) —— 每收到一个 live 帧追加一条，供 live_rate_mbps 用
    live_marks: list = field(default_factory=list)

    # ---- 落盘 ----
    @property
    def frames_dir(self) -> Path:
        return self.root / "frames"

    # ---- 留存账本（见 docs/对外接口与单一交付道.md §5）----
    def delivery_info(self) -> dict:
        """留存的完整性账本（**每一帧都留**）。

        **PC 绝不采信 App 自报的"我传完了"**（本项目铁律：不采信被测程序自报的汇总结论）。
        所以这里有两类计数，来源不同、缺口的含义也不同：

          · App 宣告的：`frames_expected`（应留存的帧数）、`frames_stored_on_device`
            （写进手机本地的帧数）、`gaps_on_device`（**采集期没写进去**）；
          · **PC 自己数出来的**：`frames_verified_on_pc`、`upload_gaps`
            （**写进去了但没传到**）、`missing_seqs`（按 seq 区间点出具体缺了哪几帧）。

        ⚠ **两个缺口必须分开命名**：`gaps_on_device` 只能**重采**，`upload_gaps` 可以
        **重传**。合并成一个数，现场必然选错处置。

        ⚠ `missing_seqs` 的口径与第 23 轮不同（所以判据也重写了）：本道 seq 由 App 的
        `nextSeq()` 逐帧分配，**写失败的帧也占号** ⇒ 判据是"宣告的区间
        `[seq_first, seq_last]` 内 PC 没校验到的那些号"，而**不是**旧的"从 0 起的连续编号"。
        这样它同时覆盖两类缺口，并且能与 `gaps_on_device + upload_gaps` 构成
        **第三条独立校验**（三处证据对不上时必须显式说出来）。
        """
        if self.capture_mode == MODE_LIVE:
            # **`applicable=false`**：本模式里「留存」这个概念没有实例。
            # 保留同名字段（全为 None）是为了让消费方**不必改取值路径**就能显示；
            # 而 `None` 与 `0` 在 UI 上必须能区分 —— `0` 是"一帧都没有"（那是缺陷），
            # `None` 是"这个问题不存在"（那是设计）。两者混同，现场会把设计当故障去修。
            return {
                "applicable": False,
                "mode": MODE_LIVE,
                "state": "not_applicable",
                "every_frame": None,
                "interval_ms": None,
                "frames_expected": None,
                "frames_stored_on_device": None,
                "gaps_on_device": None,
                "gap_ts": [],
                "frames_verified_on_pc": 0,
                "upload_gaps": None,
                "missing_seqs": [],
                "missing_seqs_note": "不适用：live 模式不产生交付帧。",
                "seq_first": None,
                "seq_last": None,
                "warmup_frames_skipped": None,
                "result_missing": None,
                "aborted_reason": None,
                "bytes_verified": 0,
                "self_consistent": None,
                "complete": None,
                "declared_at_epoch": self.store_decl_at,
                "note": ("⚠ `applicable=false`：本会话是 live 模式（**只看不留**），"
                         "交付道整体关闭 ⇒ 不存在「应有多少帧」。`complete=null` 在这里是"
                         "**不适用**，既不是「判不了」也不是「通过」。实时道账本见 `live` 段。"),
            }
        verified = [k for k in sorted(self.frames) if self.frames[k].verified]
        verified_pc = len(verified)
        bytes_pc = sum(self.frames[k].size for k in verified)

        decl = self.store_decl or {}
        expected = decl.get("frames_expected")
        stored = decl.get("frames_stored_on_device")
        gaps_dev = decl.get("gaps_on_device")
        gap_ts = [int(x) for x in (decl.get("gap_ts") or []) if isinstance(x, (int, float))]

        upload_gaps: Optional[int] = None
        if isinstance(stored, int):
            upload_gaps = max(0, stored - verified_pc)

        # ---- 按 seq 区间点出缺口（**PC 自己算**，不依赖 App 报的缺口个数）----
        seq_first = decl.get("seq_first")
        seq_last = decl.get("seq_last")
        missing_seqs: list[int] = []
        missing_note: Optional[str] = None
        if isinstance(seq_first, int) and isinstance(seq_last, int) \
                and 0 <= seq_first <= seq_last:
            present = set(verified)
            missing_seqs = [s for s in range(seq_first, seq_last + 1) if s not in present]
            # 独立校验：点出来的总缺口 == 采集缺口 + 回传缺口（三处证据必须对齐）
            if isinstance(gaps_dev, int) and isinstance(upload_gaps, int):
                want = gaps_dev + upload_gaps
                if len(missing_seqs) != want:
                    missing_note = (
                        f"⚠ **三条证据对不上**：按 seq 区间点出 {len(missing_seqs)} 个缺口，"
                        f"而采集缺口 {gaps_dev} + 回传缺口 {upload_gaps} = {want}。"
                        "说明 App 的 seq 区间或缺口记账与实际落盘/上传不一致 —— "
                        "**不要照任何一方去补采**，先查日志。")
        elif decl:
            missing_note = ("App 还没宣告 seq 区间（[seq_first, seq_last]）⇒ 点不出具体缺了哪几帧；"
                            "`upload_gaps` 这个**个数差**不受影响。")

        # 自洽式（两条都要成立，否则 complete 不许为 true）
        self_ok: Optional[bool] = None
        if isinstance(expected, int) and isinstance(stored, int) and isinstance(gaps_dev, int):
            self_ok = (expected == stored + gaps_dev) and (stored == verified_pc + upload_gaps)

        complete: Optional[bool] = None
        if isinstance(expected, int) and isinstance(stored, int) and self_ok is not None:
            complete = bool(self_ok and expected == verified_pc
                            and not gap_ts and not missing_seqs)

        return {
            "applicable": True,
            "mode": self.capture_mode,
            "state": decl.get("state") or ("none" if not decl else "unknown"),
            "every_frame": decl.get("every_frame"),
            "interval_ms": decl.get("interval_ms"),
            "frames_expected": expected,
            "frames_stored_on_device": stored,
            "gaps_on_device": gaps_dev,
            "gap_ts": gap_ts[:2000],
            "frames_verified_on_pc": verified_pc,
            "upload_gaps": upload_gaps,
            "missing_seqs": missing_seqs[:2000],
            "missing_seqs_note": missing_note,
            "seq_first": seq_first,
            "seq_last": seq_last,
            "warmup_frames_skipped": decl.get("warmup_frames_skipped"),
            "result_missing": decl.get("result_missing"),
            "aborted_reason": decl.get("aborted_reason"),
            "bytes_verified": bytes_pc,
            "self_consistent": self_ok,
            "complete": complete,
            "declared_at_epoch": self.store_decl_at,
            "note": ("`gaps_on_device`（采集期没写进去）只能重采；`upload_gaps`（没传到 PC）"
                     "可以重传 —— 两者含义不同，不要合并。`complete=null` 表示**判不了**"
                     "（App 还没宣告应有帧数），不是通过。"),
        }

    def live_rate_mbps(self, window_s: float = 60.0) -> Optional[float]:
        """最近 window_s 秒内 live 帧的字节速率（MB/s）。与交付道同口径、**各算各的**。"""
        cut = time.monotonic() - window_s
        pts = [p for p in self.live_marks if p[0] >= cut]
        if len(pts) < 2:
            return None
        span = pts[-1][0] - pts[0][0]
        if span <= 0:
            return None
        return sum(n for _, n in pts) / span / 1024 / 1024

    def live_info(self) -> dict:
        """实时道账本（`capture_mode=live`，第 27 轮）。

        **两半计数必须分开摆**，理由与交付道完全相同（见 delivery_info）：
          · PC 自己数的 `frames_received` / `bytes_received` / `fps_actual`；
          · App 宣告的 `sent` / `skipped` / `dropped` / `latency_ms`。
        只看前者会漏掉"App 想发但发不出、或跳过了多少陈帧"；只看后者就是采信被测程序。

        ⚠ `skipped` 与 `dropped` **含义不同，不许合并**：
          · `skipped` = 取图时被跳过的陈帧 —— 这是 `acquireLatestImage()` 的固有行为，
            **是设计**（本道要"此刻最新"，不要积压的旧画面）；
          · `dropped` = 队列满 / 编码失败 等**异常**丢帧（真缺陷）。
        合并成一个数之后，"链路不够快"与"实现有 bug"就没法区分了。
        """
        now = time.monotonic()
        marks = [p for p in self.live_marks if p[0] >= now - 10.0]
        fps = None
        if len(marks) >= 2 and marks[-1][0] > marks[0][0]:
            fps = (len(marks) - 1) / (marks[-1][0] - marks[0][0])
        return {
            "applicable": self.capture_mode == MODE_LIVE,
            "mode": self.capture_mode,
            # 颜色来源（第 29 轮）：请求值与**实际生效值**分列。
            # ⚠ 两者不相等 ⇒ **本次绿偏二分实验不成立**（两条路退化成同一条）。
            #   只报请求值的话，PC 与面板都会显示"rgba_hal 正常"，
            #   而设备其实一直在跑 yuv_bt601 ⇒ 结论错得毫无征兆。
            "color_source_requested": self.color_source,
            "color_source_effective": self.color_source_effective or None,
            "color_source_degraded": bool(
                self.color_source_effective
                and self.color_source_effective != self.color_source),
            "rgba_supported": (self.live_decl or {}).get("rgba_supported"),
            "frames_received": self.live_frames,
            "bytes_received": self.live_bytes,
            "mbps_received": self.live_rate_mbps(),
            "fps_actual": fps,
            "last": self.live_last or None,
            "declared": dict(self.live_decl) or None,
            "declared_at_epoch": self.live_decl_at,
            "rejected": self.live_rejected,
            "mode_conflicts": self.mode_conflicts,
            "mode_conflict_last": self.mode_conflict_last or None,
            "note": ("**设备零留存**（用户口径）：本道不落盘、不停采排空、不回传。"
                     "`skipped` 是被跳过的陈帧（设计如此），`dropped` 才是异常丢帧；"
                     "两者**不许合并**。PC 侧的 `frames_received` 是**自己数的**，"
                     "与 `declared.sent` 是两条独立证据。"),
        }

    def absorb_declaration(self, **decl) -> list[str]:
        """把 App 在 init 里如实上报的**溯源字段**补进会话记录。

        为什么必须有这一步（第 20 轮从真机落盘数据里查出来的）：v1.10.5 起会话改由
        控制道 `session_begin` **先建**，而 `h_init` 原来是
        `hub.session(sid) or create_session(...)` —— 会话已存在时，init 里带的
        model / device_serial / app_version / calib_id / capture_path_id /
        capture_path_rev **整组被丢掉**。实测后果：manifest 里
        `device_serial='unknown'`、`app_version=''`、`capture_path_id=''`、
        `capture_path_rev=0`，而 App 每一个 init 都在如实上报这些值。
        这又是一次"字段在、值没了"的静默缺失 —— 它直接毁掉
        "这份证据是哪台机器的哪个构建采的"这个问题。

        规则：**空则补，非空且不同则记冲突**（不覆盖）。中途变化说明这批数据里
        混了两套前提（典型：换了标定、换了台机器），那是要人看见的问题，
        不是就地抹平的问题。
        """
        filled: list[str] = []
        conflicts: list[str] = []
        for k, v in decl.items():
            if v in (None, "", 0):
                continue
            cur = getattr(self, k, None)
            if cur in (None, "", 0) or (k == "device_serial" and cur == "unknown"):
                setattr(self, k, v)
                filled.append(k)
            elif cur != v:
                conflicts.append(f"{k}: 记录={cur!r} ≠ App声明={v!r}")
        if filled:
            self.log(f"溯源字段按 App 声明补齐：{filled}")
        if conflicts:
            self.decl_conflicts = list(self.decl_conflicts) + conflicts
            self.log(f"⚠ 溯源字段与 App 声明冲突（**不覆盖**，需人工核对）：{conflicts}")
        return conflicts

    def log(self, msg: str) -> None:
        line = f"{now_iso()} {msg}"
        self.logs.append(line)
        self.logs[:] = self.logs[-400:]
        try:
            (self.root / "logs" / "upload.log").open("a", encoding="utf-8").write(line + "\n")
        except Exception:
            pass

    def _drift(self) -> list[dict]:
        """逐帧扫"参数漂移"：同一字段出现不同取值即记下（§5.4 的断言之一）。"""
        seen: dict[str, set] = {}
        for seq in sorted(self.frames):
            meta = self.frames[seq].meta or {}
            for k in DRIFT_FIELDS:
                if k in meta and meta[k] is not None:
                    v = meta[k]
                    seen.setdefault(k, set()).add(json.dumps(v, sort_keys=True))
        return [{"field": k, "distinct_count": len(v), "values": sorted(v)}
                for k, v in seen.items() if len(v) > 1]

    def _temps(self) -> Optional[list[float]]:
        vals = [self.frames[s].meta.get("battery_temperature_c")
                for s in sorted(self.frames)]
        vals = [v for v in vals if isinstance(v, (int, float))]
        return [min(vals), max(vals)] if vals else None

    def _calib_consistent(self) -> Optional[bool]:
        got = {self.frames[s].meta.get("calib_id") for s in self.frames}
        got = {g for g in got if g}
        if not got:
            return None
        return len(got) == 1 and (not self.calib_id or got == {self.calib_id})

    def _delivery_complete(self) -> Optional[bool]:
        """App 侧队列是否已**全部**回传完。三态，`None` = 判不了。

        判据链（每一条都要能说出证据是哪几个字段）：

        · App 通过控制道宣告过 `delivery_done` ⇒ `True`。这是唯一的一手证据。
        · 没有宣告，但 `stop_ack` 里 App 自报"抽了 N 帧、队列剩 0"，而 PC 这边
          `verified` 的帧数 **≥ N** ⇒ `True`（两个来自不同端的计数互相印证）。
          注意 `stop_ack` 是**停的那一刻**的快照，若之后还有迟到帧，说明当时并未传完 ⇒ `False`。
        · 其余（控制道断开、从没收到 stop_ack、队列还有余量）⇒ `None`。

        **为什么不用"N 秒没有新帧"推断**：那正是让判据强于事实 —— 链路卡顿、
        旧会话串台、App 被系统冻结都会骗过它，而骗过的后果是"以为收全了"。
        """
        if self.capture_mode == MODE_LIVE:
            # **不适用 ≠ 判不了**。live 模式的定义就是"不留存"，所以这里返回 None
            # 的理由与上面那些"证据不足"完全不同，`_delivery_complete_note()` 会写清楚。
            # ⚠ 绝不返回 `False` —— `False` 的含义是"判了、没传完"，现场会去重传
            # 一份根本不存在的留存；也绝不返回 `True` —— 那等于宣称"收全了"。
            return None
        if self.delivery_done:
            return True
        sa = self.stop_ack or {}
        n_verified = sum(1 for f in self.frames.values() if f.verified)
        delivered = sa.get("frames_delivered")
        if delivered is None:
            return None
        if self.late_frames_after_stop > 0:
            # 停的那一刻之后还有帧到 ⇒ 当时队列并**不**是空的（或 App 的记账漏了）
            return False
        if sa.get("queue_len") == 0:
            return n_verified >= int(delivered)
        return False

    def _delivery_complete_note(self) -> str:
        """把三态判定的**依据**写出来。不写依据，null 会被读成"没问题"。"""
        if self.capture_mode == MODE_LIVE:
            # 这里必须用"不适用"这四个字，不能只说"判不了" —— 后者会被读成
            # "PC 没收到足够证据，可能还在传"，于是有人会去等、去重传。
            return ("**不适用**：本会话是 `capture_mode=live`（只看不留）—— "
                    "交付道整体关闭，设备与 PC 都**不保留任何帧**，"
                    "所以「收全了没有」这个问题在本模式里不存在。"
                    "要看的是实时道的 `live` 段（sent/skipped/dropped/fps_actual）。")
        if self.delivery_done:
            # 宣告是**一手证据**，所以结论照采信；但把两侧的计数摆出来供复核 ——
            # 判据可以信 App，观测者必须能自己核对。
            claim = self.delivery_done.get("frames")
            n = sum(1 for f in self.frames.values() if f.verified)
            extra = "" if claim == n else (
                f"（⚠ PC 侧 verified {n} 帧 ≠ 宣告 {claim} 帧 —— 该会话可能另有来源的帧，"
                f"或宣告早于最后一帧落盘；不影响「已收全」这个结论，但摆出来供复核）")
            return f"App 宣告交付收尾：{claim} 帧全部 verified，队列已清空{extra}"
        sa = self.stop_ack or {}
        delivered = sa.get("frames_delivered")
        n_verified = sum(1 for f in self.frames.values() if f.verified)
        if delivered is None:
            return ("判不了：从没收到 stop_ack / delivery_done（控制道可能已断开）"
                    "—— PC 侧无法区分「还在传」与「传完了」")
        if self.late_frames_after_stop > 0:
            return (f"未收完：stop_ack 说队列剩 {sa.get('queue_len')}，"
                    f"之后又有 {self.late_frames_after_stop} 帧到达 ⇒ 停时并未传完")
        if sa.get("queue_len") != 0:
            return f"未收完：stop_ack 说队列还剩 {sa.get('queue_len')} 帧"
        return (f"据 stop_ack 推断已收完：App 自报抽帧 {delivered}、队列 0，"
                f"PC 已 verified {n_verified}"
                + ("" if n_verified == int(delivered) else "（⚠ 两侧计数不等，见 late_frames）"))

    # 开流门控：哪些键必须"回读 == 请求"（硬），哪些只记不判（软）
    GATE_HARD = ("zoom_ratio", "ois", "eis", "format", "output_size")

    def start_gate(self) -> dict:
        """开流门控：把**第一帧的回读值**与 profile 逐项比对（§5.4 / M1 待办清单）。

        三条纪律（缺任何一条，门控自己就会变成"过度结论"）：

        · **profile 里写 0/None 的键 = "不指定"**：`zoom_ratio: 0` 的语义是"由 App 界面档位决定"
          ⇒ 必须**跳过**并记 notes，绝不判不一致（否则默认档位下门控永远失败）；
        · **软键只记不判**：对焦回读被 HAL 量化（G2：量化到 1 mm 距离）、曝光/ISO 也可能被
          HAL 覆盖 ⇒ 给容差、只出 notes，不参与 pass/fail；
        · **没有帧可看 ⇒ `incomplete`**（本项目铁律：判不了 ≠ 通过）；
        · **回读缺席 ⇒ 该项计入 `unevaluable`，整体 `incomplete`，不停流**：第 20 轮新增。
          "值对不上"（fail）与"值没看见"（incomplete）是两件事，前者的动作是停流，
          后者只是取证不足 —— 把它也停流，等于用"判不了"得出"有问题"的结论；
        · **枚举按语义比，不按字符串比**（`"OFF(0)"` ≡ `"OFF"`）：写法差异不是事实差异。
          第 20 轮真机实测：这条没做，ois/eis 被判假失败 ⇒ 自动停流 ⇒ 用户看到"刷一下就不动了"。

        为什么值得单独做：profile 是 PC 下发的**意图**，回读是设备的**事实**。
        两者不一致时（典型：zoom 被 HAL 拒、输出尺寸被改），数据照样在流，
        但"这批数据是按什么配置采的"就已经是错的了。
        """
        seqs = sorted(self.frames)
        if not seqs:
            return {"status": "incomplete", "checks": {}, "hard_fail": [], "unevaluable": [],
                    "soft_notes": [], "evidence": "",
                    "note": "还没有收到任何帧 ⇒ 无法判定 profile 是否真的生效"}
        prof = self.profile or {}
        # 取哪一帧作证据：**不是"第一帧"，是"第一帧带完整回读字段的帧"**。
        # 1.10.5 之前约 2/3 的帧因"结果先到、图未到"而整组 CaptureResult 派生字段缺席；
        # 拿第一帧硬判，会把一次**取证不足**判成**配置不对**，然后自动停流 ——
        # 问题就从"缺证据"升级成"采不了"。缺席的帧自描述 result_missing=true，跳过即可。
        ev_seq = next((q for q in seqs
                       if (self.frames[q].meta or {}).get("result_missing") is not True), seqs[0])
        meta = self.frames[ev_seq].meta or {}
        checks: dict = {}
        hard_fail: list = []
        unevaluable: list = []
        soft: list = []
        norm_notes: list = []

        def put(key: str, want, got, ok: bool) -> None:
            c = {"want": want, "got": got, "ok": ok}
            if not ok:
                hard_fail.append(key)
            elif str(want) != str(got):
                # 写法不同、语义相同 ⇒ **必须留痕**：否则"归一"自己又变成一个静默步骤，
                # 将来有人看到 `want="OFF"` 而 checks 里 `got="OFF(0)"` 会以为判据放水了。
                c["normalized"] = True
                norm_notes.append(f"{key}: 请求 {want!r} ≡ 回读 {got!r}（写法不同，语义相同）")
            checks[key] = c

        def uneval(key: str, want, why: str) -> None:
            """回读缺席 ⇒ 这一项**判不了**（不是"不一致"）：判不了 ≠ 有问题，也 ≠ 通过。"""
            checks[key] = {"want": want, "got": None, "ok": None, "why": why}
            unevaluable.append(key)

        pz = prof.get("zoom_ratio")
        if pz is None or float(pz or 0) == 0:
            soft.append("zoom_ratio 未指定（profile=0 ⇒ 由 App 界面档位决定，门控跳过）")
        else:
            got = meta.get("zoom_ratio")
            if got is None:
                uneval("zoom_ratio", pz, "回读缺席（该帧没带 CaptureResult 派生字段）")
            else:
                put("zoom_ratio", pz, got,
                    abs(float(got) - float(pz)) <= max(0.005 * float(pz), 1e-3))

        for key, mk in (("ois", "ois_mode"), ("eis", "eis_mode")):
            want = prof.get(key)
            if want in (None, ""):
                continue
            got = meta.get(mk)
            if got is None:
                uneval(key, want, "回读缺席（该帧没带 CaptureResult 派生字段）")
            else:
                ok = enum_eq(want, got)
                put(key, want, got, ok)
                # 这两个键**不是可调旋钮，是锁定配方的断言**：App 侧的锁定配方把 OIS/EIS
                # 钉死为 OFF（M0 ② 就是这么采的，内参可复现性依赖它），profile 里写
                # OFF/ON 只是把它**声明出来**好让门控能验。所以此项 fail 的含义是
                # "HAL 没有把稳定器关掉"或"有人把 profile 改成了锁定配方给不出的值"，
                # 而不是"这个旋钮没调对" —— 这句要留在证据里，不然下次又要重新推理一遍。
                if not ok:
                    checks[key]["hint"] = ("该键是**锁定配方断言**而非可调参数：App 侧请求里"
                                           "稳定器恒为 OFF。不一致 = HAL 拒绝了锁定请求，"
                                           "或 profile 被写成了非 OFF 值")

        want_size = prof.get("output_size")
        if want_size:
            got_size = [meta.get("width"), meta.get("height")]
            if got_size == [None, None]:
                uneval("output_size", list(want_size), "回读缺席（该帧没带宽高）")
            else:
                put("output_size", list(want_size), got_size, list(want_size) == got_size)

        want_fmt = prof.get("format")
        if want_fmt:
            if meta.get("format") is None:
                uneval("format", want_fmt, "回读缺席（该帧没带格式）")
            else:
                put("format", want_fmt, meta.get("format"), want_fmt == meta.get("format"))

        # ---- 软键：只记 ----
        fd = prof.get("focus_diopter")
        got_fd = meta.get("focus_distance_diopter")
        if fd is not None:
            if got_fd is None:
                soft.append("对焦回读缺失（该帧没带 CaptureResult 派生字段 —— 查 meta_quality）")
            elif abs(float(got_fd) - float(fd)) > 0.01:
                soft.append(f"对焦回读 {got_fd:.6f} ≠ 请求 {fd}（HAL 会量化，仅供参考）")
        for key, mk in (("exposure_time_ns", "exposure_time_ns"), ("iso", "iso")):
            want = prof.get(key)
            got = meta.get(mk)
            if want is not None and got is not None and float(got) != float(want):
                soft.append(f"{key} 回读 {got} ≠ 请求 {want}（该量可能被 HAL 覆盖，仅供参考）")

        # 三态：fail（真不一致 ⇒ 停流）/ incomplete（缺证据 ⇒ 只标记，不停流）/ pass
        if hard_fail:
            status = "fail"
        elif unevaluable:
            status = "incomplete"
        else:
            status = "pass"
        return {
            "status": status,
            "checks": checks,
            "hard_fail": hard_fail,
            "unevaluable": unevaluable,
            "soft_notes": norm_notes + soft,
            "evidence": f"取自该会话 seq={ev_seq} 的 CaptureResult 回读值"
                        "（按 seq 升序取第一个带完整回读字段的帧，跳过 result_missing 的帧）",
            "note": ("hard_fail 非空 ⇒ **下发的 profile 没有被真正采纳**，这批数据的配置前提不成立"
                     "（动作：自动停流）；unevaluable 非空 ⇒ 这几项的回读值缺席，**判不了**"
                     "（动作：只标记，不停流，等后续帧补齐再判）；status=incomplete 也可能是"
                     "还没有任何帧"),
        }

    # 来自 CaptureResult 的那组字段（App 侧"结果对象没留住"时会整组缺席）
    RESULT_DERIVED = ("rolling_shutter_skew_ns", "active_physical_id", "zoom_ratio",
                      "crop_region", "exposure_time_ns", "iso")

    def meta_quality(self) -> dict:
        """逐帧元数据完整度：有多少帧缺"来自 CaptureResult 的那组字段"。

        **为什么必须进 manifest**：这类缺失是**静默**的 —— 送达的帧画面正常、时间戳正常、
        字节数正常，只是元数据里少了一组字段；下游要么当成"该帧没有这个量"，
        要么在聚合时把它当 0/None 参与计算（例如"平均 skew"直接被拉偏）。
        实测（第 18 轮）：某会话 44 帧里 **28 帧**整组缺席，而所有计数器都是正常的。
        ⇒ 判据必须自己算出"缺了多少帧"，而不是等人去翻 JSON。
        """
        n = len(self.frames)
        miss = {k: 0 for k in self.RESULT_DERIVED}
        flagged = 0
        for seq in sorted(self.frames):
            meta = self.frames[seq].meta or {}
            if meta.get("result_missing") is True:
                flagged += 1
            for k in self.RESULT_DERIVED:
                if meta.get(k) is None:
                    miss[k] += 1
        # 判定口径：**以 skew 与镜头为关键**（几何/时序结论直接依赖它们）；
        # crop_region/zsl/曝光 缺席同样记账，但只作提示（不是每个版本都会记全）。
        ok = None if n == 0 else (miss["rolling_shutter_skew_ns"] == 0
                                  and miss["active_physical_id"] == 0)
        return {
            "frames": n,
            "missing": miss,
            "flagged_result_missing": flagged,
            "ok": ok,
            "note": ("ok=false ⇒ 有帧缺 CaptureResult 派生字段（画面与时间戳都正常，"
                     "**只能靠这里发现**）：下游不得把 None 当数值参与聚合；"
                     "若 flagged_result_missing>0，说明 App 侧确实丢了结果对象"),
        }

    def lens_info(self) -> dict:
        """生效物理镜头与变焦的逐帧汇总。

        **为什么必须进 manifest**：逻辑相机由多颗物理镜头融合，`CONTROL_ZOOM_RATIO`
        区间含 **0.6**（本机原始报告），而 **0.6× 不是"主摄缩小"，是切到超广角** ——
        不同传感器/像元/光圈 ⇒ **内参完全不同**。所以一段会话若要当"一套内参"用，
        必须先回答两件事：**整段是不是同一颗镜头**、**请求的 zoom 是否真的生效**。
        """
        ids, zreq, zrb, pairs, changes = [], [], [], [], 0
        for seq in sorted(self.frames):
            meta = self.frames[seq].meta or {}
            v = meta.get("active_physical_id")
            if v not in (None, ""):
                ids.append(str(v))
            zr = meta.get("zoom_ratio")
            if zr is not None:
                zrb.append(float(zr))
            req = meta.get("requested") if isinstance(meta.get("requested"), dict) else {}
            if req.get("zoom_ratio") is not None:
                zreq.append(float(req["zoom_ratio"]))
                # 只有"请求与回读都有"的帧才参与一致性判定：
                # 早期帧可能只带其中之一（不同版本字段不同），拿它们配对比是无意义的
                if zr is not None:
                    pairs.append((float(req["zoom_ratio"]), float(zr)))
            c = meta.get("lens_changes_so_far")
            if isinstance(c, int):
                changes = max(changes, c)
        uniq = sorted(set(ids))
        zoom_match = None
        if pairs:
            zoom_match = all(abs(b - a) <= max(0.005 * max(a, 1e-9), 1e-3) for a, b in pairs)
        return {
            "active_physical_id_distinct": uniq,
            "active_physical_id_unique": (len(uniq) <= 1) if uniq else None,
            "zoom_requested_distinct": sorted(set(zreq)),
            "zoom_readback_distinct": sorted(set(zrb)),
            "zoom_match": zoom_match,
            "lens_changes_peak": changes,
            "note": ("active_physical_id 不唯一 ⇒ **该段数据分属两套内参**，下游必须按镜头分段；"
                     "zoom_match=false ⇒ 请求的变焦没有被采纳（0.6× 是否真切到超广角要看这个）"),
        }

    def prune_thumbs(self) -> None:
        """监视道限量：只留最新 MAX_THUMBS 张（设计上"可随时清理"）。

        不设上限的后果与 R29 同类：一次长采集会悄悄把磁盘吃掉一部分，
        而且缩略图是**可丢的**——留最新若干张足够现场确认取景。
        """
        d = self.root / "thumbs"
        if not d.is_dir():
            return
        js = sorted(d.glob("thumb_*.jpg"))
        for old in js[:-MAX_THUMBS]:
            try:
                old.unlink()
                (d / (old.stem + ".json")).unlink(missing_ok=True)
            except Exception:
                pass

    def latest_thumb(self) -> Optional[Path]:
        d = self.root / "thumbs"
        if not d.is_dir():
            return None
        js = sorted(d.glob("thumb_*.jpg"))
        return js[-1] if js else None

    def verify_rate_mbps(self, window_s: float = 60.0) -> Optional[float]:
        """最近 window_s 秒内 verified 的字节速率（MB/s）。

        **为什么要在 PC 侧也算一遍**：App 报的是"我发出去了多少"，PC 报的是
        "我确认收到了多少"。两者若明显不等，说明有重传/失败重试；
        两者相等且都低于链路能力，才是真的链路受限。诊断必须能分开这两件事。
        """
        cut = time.monotonic() - window_s
        pts = [p for p in self.verify_marks if p[0] >= cut]
        if len(pts) < 2:
            return None
        span = pts[-1][0] - pts[0][0]
        if span <= 0:
            return None
        return sum(n for _, n in pts) / span / 1024 / 1024

    def write_manifest(self) -> None:
        verified = [s for s in sorted(self.frames) if self.frames[s].verified]
        man = {
            "session_id": self.session_id,
            "protocol_version": PROTOCOL_VERSION,
            "device_serial": self.device_serial,
            "device_model": self.model,
            "app_version": self.app_version,
            "calib_id": self.calib_id,
            "delivery": self.delivery,
            "capture_path_id": self.capture_path_id,
            "capture_path_rev": self.capture_path_rev,
            "profile_id": self.profile_id,
            "profile": self.profile,
            "plan": self.plan,
            # 采集模式随 manifest 落盘：**"这份数据是什么模式采的"是读数据的前提** ——
            # live 模式的会话本来就该"没有帧"，但只看 `frame_count: 0` 会以为采集失败了。
            "capture_mode": self.capture_mode,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "state": self.state,
            "frame_count": len(verified),
            "frames_declared": len(self.frames),
            "bytes_verified": sum(self.frames[s].size for s in verified),
            "verify_rate_mbps": self.verify_rate_mbps(),
            "thumbs": self.thumbs,
            "thumb_latest": self.thumb_latest or None,
            # --- 断言（§6.3）：每一条都要能回答"证据是哪几个字段" ---
            "lens": self.lens_info(),
            "meta_quality": self.meta_quality(),
            "start_ack": self.start_ack or None,
            "stop_ack": self.stop_ack or None,
            "start_gate": self.start_gate_cache or None,
            # 溯源字段与 App 声明冲突（非空且不同 ⇒ 不覆盖，只留痕）。null = 没有冲突，
            # 但**要先看上面几个溯源字段是否为空** —— 空字段意味着"没声明"，而不是"一致"。
            "declared_conflicts": self.decl_conflicts or None,
            # ---- 交付收尾：停采集之后队列是否已全部传完 ----
            "stopped_at": self.stopped_at,
            "late_frames_after_stop": self.late_frames_after_stop,
            "delivery_done": self.delivery_done or None,
            # 留存账本（**每一帧都留**）：应有/机内存/两个缺口/按 seq 区间点缺口
            "store": self.delivery_info(),
            # 实时道账本（`capture_mode=live` 时才有内容；deliver 下 applicable=false）
            "live": self.live_info(),
            "assertions": {
                "calib_id_consistent": self._calib_consistent(),
                "param_drift": self._drift(),
                "temp_range_c": self._temps(),
                "all_frames_verified": (len(verified) == len(self.frames)
                                        and len(self.frames) > 0),
                "delivery_complete": self._delivery_complete(),
                "delivery_complete_note": self._delivery_complete_note(),
                "note": ("calib_id_consistent / param_drift / delivery_complete 为 null 表示"
                         "**该批没有可判的字段**，不是「通过」（本项目判据体系里"
                         "「判不了」≠「没问题」）"),
            },
        }
        tmp = self.root / "manifest.json.tmp"
        tmp.write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.root / "manifest.json")   # 原子替换，避免读到半截 manifest

    def summary(self) -> dict:
        return {
            "session_id": self.session_id,
            "device_serial": self.device_serial,
            "device_model": self.model,
            "app_version": self.app_version,
            "calib_id": self.calib_id,
            "delivery": self.delivery,
            # 采集模式（`deliver` = 每一帧都留；`live` = 只看不留）。
            # 面板必须能一眼看出这个 —— `live` 跑完什么都没留，误用不可恢复（R38）。
            "capture_mode": self.capture_mode,
            "capture_path": f"{self.capture_path_id}/{self.capture_path_rev}",
            # 面板要能区分"声明了"与"没声明"（空字符串拼出来的 "/0" 看着像有值）
            "capture_path_id": self.capture_path_id,
            "capture_path_rev": self.capture_path_rev,
            "state": self.state,
            "started_at": self.started_at,
            "frames_total": len(self.frames),
            "frames_verified": sum(1 for s in self.frames.values() if s.verified),
            "frames_partial": sum(1 for s in self.frames.values() if not s.verified),
            "bytes_verified": sum(f.size for f in self.frames.values() if f.verified),
            "thumbs": self.thumbs,
            "thumb_latest": self.thumb_latest or None,
            "lens": self.lens_info(),
            "meta_quality": self.meta_quality(),
            "start_ack": self.start_ack or None,
            "stop_ack": self.stop_ack or None,
            "start_gate": (self.start_gate_cache or self.start_gate()),
            "declared_conflicts": self.decl_conflicts or None,
            "last_status": self.last_status,
            "drift": self._drift(),
            "calib_consistent": self._calib_consistent(),
            # 交付收尾：stop_ack 只是"停在哪"，delivery_complete 才回答"是否全收完"（三态）
            "stopped_at": self.stopped_at,
            "late_frames_after_stop": self.late_frames_after_stop,
            "delivery_done": self.delivery_done or None,
            "delivery_complete": self._delivery_complete(),
            "delivery_complete_note": self._delivery_complete_note(),
            # 留存账本：面板与 host/status 用同一个口径（避免"面板说好、接口说不清"）
            "store": self.delivery_info(),
            # 实时道账本（`live` 模式下才有内容；`deliver` 下 applicable=false）
            "live": self.live_info(),
        }


class Hub:
    """服务端内存态（落盘为真相，内存只作索引）。"""

    def __init__(self, data_dir: Path, max_mbps: float | None, token: str):
        self.data_dir = data_dir
        self.sessions_dir = data_dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.max_mbps = max_mbps
        self._tb_tokens = float(max_mbps * 1024 * 1024 * 0.25) if (max_mbps and max_mbps > 0) else 0.0
        self._tb_last = time.monotonic()
        self.token = token
        self.sessions: dict[str, Session] = {}
        self.files: dict[str, FileRecord] = {}
        self.idem: dict[str, str] = {}                 # (sid|serial|seq) -> file_id
        self.ws: set[web.WebSocketResponse] = set()
        # 面板（浏览器）的推送通道：`{websocket -> 发送队列}`。**必须是独立集合**：
        # App 那条 `/ws` 是控制道，断开/重连的语义完全不同，混在一起会让
        # "面板刷新"看起来像"设备掉线"。
        self.panel_ws: dict = {}
        self.profile: dict = {}
        self.profile_id: str = ""
        # 手机该连哪个地址/端口（启动时按 lan_addresses() 的首选填入）——
        # 配对码要用它，不能临时猜：多网卡机器上"猜错网卡"是最常见的连不上原因。
        self.lan_ip: str = ""
        # **全部候选**（启动时算一次并钉住）：多网卡机器上首选未必对（VPN / 虚拟网卡 /
        # 手机连的是另一张卡）⇒ 对外接口必须把候选**都**给出去，让调用方（或人）
        # 能换一个试。放在这里缓存而不是每次请求现算，是因为现算会走一次
        # getaddrinfo + 出口 IP 探测（几十 ms），而且**运行中网络变化会让两次请求
        # 给出不同的首选地址** —— 配对信息必须稳定。
        self.lan_candidates: list[tuple[str, str, bool]] = []
        self.port: int = PAIR_DEF_PORT
        self.started_at = now_iso()

        # ---- 本机上位机（整体架构里的其它程序）----
        # 与面板通道分开：面板是"人看的"，这一层是"程序调用的"，两者的队列长度与
        # 节流策略不同（事件流可以很长，MJPEG 只要最新）。
        self.mjpeg_subs: dict = {}       # sub_id -> asyncio.Queue[(jpeg_bytes, seq, sid)]
        self.host_ws: dict = {}          # websocket -> {"q": Queue, "dropped": int}
        self._ev_last: dict = {}         # 事件类型 -> 上次发出时刻（节流用）
        self._ev_throttled: dict = {}    # 事件类型 -> 被节流掉多少条（**必须能看见**）
        # 最近一张缩略图（**字节**）：新订阅的 MJPEG 客户端立刻能看到画面，
        # 而不是黑屏等到下一张（1 Hz 档下最长要等 1 秒，10 fps 档下 100 ms）
        self.last_thumb_jpeg: Optional[bytes] = None
        self.last_thumb_meta: dict = {}
        self.host_events_sent: int = 0
        # 实时道（`capture_mode=live`）打到了**未知会话**的次数（整个服务累计）。
        # 与「模式互斥」分开计数：前者是"会话不存在"，后者是"会话存在但站在另一条道上"——
        # 两种故障的处置完全不同（一个去查会话寿命，一个去查 App 的启动参数）。
        self.live_rejected_total: int = 0

    # ---- 会话 ----
    def session(self, session_id: str) -> Optional[Session]:
        return self.sessions.get(session_id)

    def sessions_newest_first(self) -> list:
        """会话按**创建顺序**倒序（最新在前）。

        ⚠ 为什么不用 `sorted(started_at, reverse=True)`：`started_at` 的格式只精确到**秒**，
        同一秒内创建的多个会话排序**不确定** ⇒ "最新会话"会随机指向另一个 ——
        而面板与 `host/status` 的 `capture`/`store`/`live` 都取"最新会话"。
        实测代价（第 27 轮）：新加的 A9 用例在同一秒里建了几个会话，A7c 拿到的
        就不是它自己那个 ⇒ 判据随机红，而红的理由看起来像"熔断没生效"。
        dict 保插入序 ⇒ 倒序遍历就是"创建序倒序"，没有同秒歧义。
        """
        return list(reversed(list(self.sessions.values())))

    def create_session(self, session_id: str, device_serial: str, **kw) -> Session:
        s = self.sessions.get(session_id)
        if s:
            return s
        if not SESSION_ID_RE.match(session_id):
            # ⚠ 这里**必须抛异常**，不能 `return err(...)` —— 调用方拿的是"Session"，
            # 一个 Response 对象会在下一行 `s.plan = ...` 处变成 AttributeError（500），
            # 把"客户端参数错"报成"服务端崩了"。调用方负责转成 400。
            raise ValueError(f"非法 session_id: {session_id!r}")
        root = self.sessions_dir / session_id
        for sub in ("frames", "thumbs", "incoming", "logs"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        s = Session(session_id=session_id, device_serial=device_serial, root=root,
                    profile=dict(self.profile), profile_id=self.profile_id, **kw)
        self.sessions[session_id] = s
        s.log(f"session created（device={device_serial}）")
        s.write_manifest()
        # 本机事件广播。⚠ 这里 `capture_path_id` 可能还是空的：PC 主控 start 时
        # 会话先建、路径由 App 随后的 init 补齐（`absorb_declaration`）。
        # 所以订阅者**不能**把这一条事件当成"路径已确定"，要看后续 frame_verified。
        self.broadcast_event({"type": "session_started", "session_id": session_id,
                              "device_serial": device_serial, "profile_id": self.profile_id,
                              "capture_path_id": s.capture_path_id,
                              "capture_path_rev": s.capture_path_rev,
                              "at": s.started_at})
        return s

    # ---- WS 广播 ----
    async def broadcast(self, payload: dict) -> int:
        """给所有已连接的 App 发消息；返回成功数（0 表示没有 App 在线）。

        为什么返回计数：面板要能显示"指令发出去了但没有 App 接收" ——
        否则点了"开始"什么也没发生，看起来像服务端坏了。
        """
        dead = []
        n = 0
        msg = json.dumps(payload, ensure_ascii=False)
        for ws in list(self.ws):
            try:
                await ws.send_str(msg)
                n += 1
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.ws.discard(ws)
        return n

    def broadcast_panel(self, payload: dict) -> int:
        """给已打开的面板推一条消息；返回投递数。**同步、非阻塞**。

        **为什么面板需要推送**：面板原来靠 3 s 轮询。App 的缩略图是 1 Hz，于是
        "3 s 收到 3 张、只画 1 张" —— 用户看到计数 +3 而画面只更新一次，
        合理地怀疑"丢了 2/3"（第 21 轮实测报障）。数据其实一张没丢：
        `thumbs` 计数是逐张落盘数。**是观测口径骗了人**，不是链路骗了人。

        **为什么必须是同步的**：这条推送是在 **App 的控制道消息循环里**被调用的
        （收到 thumb 就转发给面板）。任何 `await` 都意味着"面板的状态能决定
        控制道的快慢"。实测代价：用 `asyncio.wait_for` 给 `send_str` 加超时，
        超时的取消会顺着 `await` 一路冒泡，**把 App 的控制道连接整个掀掉**
        （现象：手机端莫名显示"连接已断开"，而服务端什么错都没打）。
        ⇒ 改为"投递到每连接的 2 格队列，由独立任务发送"：
        慢面板只会丢自己的帧，**碰不到控制道**。这与 App 端监视道的做法
        （`thumbQueue` 2 格、丢最旧）是同一条原则：**监视道可丢，控制道不可堵**。
        """
        msg = json.dumps(payload, ensure_ascii=False)
        n = 0
        for _ws, q in list(self.panel_ws.items()):
            try:
                q.put_nowait(msg)
                n += 1
            except asyncio.QueueFull:
                # 丢最旧、留最新 —— 监视道是"看到现在"，不是"一帧不落"
                try:
                    q.get_nowait()
                    q.put_nowait(msg)
                    n += 1
                except Exception:
                    pass
            except Exception:
                pass
        return n

    # ---- 本机接口面：监视道分发 + 事件广播 ----
    def push_monitor(self, jpeg: bytes, seq: int, sid: str, meta: dict) -> int:
        """把"最新一张缩略图"投递给所有 MJPEG 订阅者。**同步、非阻塞、满则丢最旧**。

        与面板推送同一条纪律（教训 15）：这个方法是在 **App 的控制道消息循环里**
        被调用的 ⇒ 任何 `await` 都意味着"某个下游消费者的快慢能决定控制道的生死"。
        所以只做 `put_nowait`：慢订阅者只丢自己的帧。

        为什么订阅者队列只有 2 格：MJPEG 的语义是"看到现在"，不是"一帧不落" ——
        积压的帧没有价值（画出来就是延迟）。这与交付道的要求正好相反（交付道**一帧不落**）。
        """
        self.last_thumb_jpeg = jpeg
        self.last_thumb_meta = {"seq": seq, "session_id": sid, "bytes": len(jpeg),
                                "sensor_timestamp_ns": meta.get("sensor_timestamp_ns"),
                                # 来自哪条道、是不是彩色：消费方必须能自己判断
                                # （把 live 帧当交付画面拿去做测量，就是静默用错数据）
                                "lane": meta.get("lane", "thumb"),
                                "color": bool(meta.get("color", False)),
                                "at": now_iso()}
        n = 0
        for q in list(self.mjpeg_subs.values()):
            try:
                q.put_nowait((jpeg, seq, sid))
                n += 1
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait((jpeg, seq, sid))
                    n += 1
                except Exception:
                    pass
            except Exception:
                pass
        return n

    def broadcast_event(self, payload: dict, throttle_s: float = 0.0) -> int:
        """给本机事件订阅者广播一条事件。同步、非阻塞、满则丢最旧 + **计数**。

        `throttle_s > 0` 时同类型事件在该间隔内只发一条（用于 `delivery_progress`：
        交付帧是逐帧验证的，不节流就是"每秒几十条事件"，订阅者一个慢就积压成灾）。
        **被节流掉多少条必须计数（`_ev_throttled`）** —— 否则"节流"本身又变成一个
        静默步骤，下游拿不到 `delivery_progress` 时会以为"留存停了"。
        """
        t = str(payload.get("type") or "")
        if throttle_s > 0:
            now = time.monotonic()
            if now - self._ev_last.get(t, 0.0) < throttle_s:
                self._ev_throttled[t] = self._ev_throttled.get(t, 0) + 1
                return 0
            self._ev_last[t] = now
            payload = dict(payload)
            payload["throttled_skipped"] = self._ev_throttled.pop(t, 0)
        msg = json.dumps(payload, ensure_ascii=False)
        n = 0
        for info in list(self.host_ws.values()):
            q = info["q"]
            try:
                q.put_nowait(msg)
                n += 1
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(msg)
                    info["dropped"] += 1
                    n += 1
                except Exception:
                    pass
            except Exception:
                pass
        self.host_events_sent += n
        return n

    def host_status(self) -> dict:
        """机器可读的整机状态（`GET /api/v1/host/status`）。

        字段含义必须与面板**完全一致**（否则会出现"面板说好、接口说不清"）。
        特别地：`gate.status` 三态原样透出 —— **`incomplete` 不是通过**。
        """
        latest = None
        for s in self.sessions_newest_first():
            latest = s
            break
        running = bool([s for s in self.sessions.values() if s.state == "running"])
        cap: dict = {"running": running, "session_id": None, "state": "idle",
                     "capture_mode": MODE_DELIVER,
                     "frames_verified": 0, "deliver_fps": None, "upload_mbps": None,
                     "profile_id": self.profile_id, "started_at": None}
        gate: dict = {"status": "incomplete", "checked": False, "hard_fail": [],
                      "note": "还没有会话 ⇒ 判不了（不是通过）"}
        store: dict = {"state": "none", "every_frame": None}
        live: dict = {"applicable": False, "mode": MODE_DELIVER,
                      "frames_received": 0, "bytes_received": 0}
        if latest is not None:
            verified = [k for k in latest.frames if latest.frames[k].verified]
            marks = [p for p in latest.verify_marks if p[0] >= time.monotonic() - 10.0]
            fps = None
            if len(marks) >= 2 and marks[-1][0] > marks[0][0]:
                fps = len(marks) / (marks[-1][0] - marks[0][0])
            cap.update({"session_id": latest.session_id, "state": latest.state,
                        "capture_mode": latest.capture_mode,
                        "frames_verified": len(verified), "deliver_fps": fps,
                        "upload_mbps": latest.verify_rate_mbps(),
                        "started_at": latest.started_at,
                        "profile_id": latest.profile_id or self.profile_id,
                        "bytes_verified": sum(latest.frames[k].size for k in verified),
                        "late_frames_after_stop": latest.late_frames_after_stop,
                        "delivery_complete": latest._delivery_complete()})
            g = latest.start_gate_cache or latest.start_gate()
            gate = {"status": g.get("status"), "checked": latest.gate_checked,
                    "hard_fail": g.get("hard_fail") or [],
                    "unevaluable": g.get("unevaluable") or [],
                    "note": g.get("note")}
            ai = latest.delivery_info()
            store = {k: v for k, v in ai.items() if k != "note"}
            store["phase"] = {"none": "none", "idle": "none",
                              "recording": "storing-on-device",
                              "uploading": "uploading-to-pc", "done": "done",
                              "aborted_no_space": "aborted_no_space",
                              "aborted": "aborted"}.get(ai["state"], ai["state"])
            store["note"] = ai.get("note")
            # 实时道账本：**与 store 并列摆出来** —— live 会话里 store 是"不适用"，
            # 真正要看的状态全在这里；deliver 会话里它是 applicable=false。
            live = latest.live_info()
        thumbs = latest.thumbs if latest is not None else 0
        return {
            "ok": True, "server_version": SERVER_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "capture": cap,
            "device": {"connected": len(self.ws) > 0,
                       "clients": len(self.ws),
                       "model": (latest.model if latest else ""),
                       "serial": (latest.device_serial if latest else ""),
                       "app_version": (latest.app_version if latest else "")},
            "gate": gate,
            "monitor": {"subscribers": len(self.mjpeg_subs),
                        "panel_subscribers": len(self.panel_ws),
                        "thumbs": thumbs,
                        "interval_ms": (latest.last_status or {}).get("thumb_interval_ms")
                                       if latest else None,
                        "last_thumb": self.last_thumb_meta or None},
            "store": store,
            "live": live,
            "host_events": {"subscribers": len(self.host_ws),
                            "sent": self.host_events_sent,
                            "throttled": dict(self._ev_throttled)},
            "storage": {"data_dir": str(self.data_dir)},
            "sessions": [s.session_id for s in self.sessions_newest_first()][:50],
        }

    # ---- 限速（真正的令牌桶，按字节） ----
    #
    # ⚠ 旧实现是 `sleep(nbytes / max_mbps)` —— **每个请求固定睡满该请求的"理论时长"**，
    # 于是"40 MB/s"在小分片上根本达不到：实测回环下 40 MB/s 的档位只有 **17.6 MB/s**
    # （1 MiB 分片要睡 26 ms，而该分片本身的处理还要额外时间，两者相加）。
    # 也就是说，那个数字印在启动横幅上是**误导**的，而且它可能成为看不见的天花板。
    # 改成累计额度的令牌桶后，长期平均速率才与 `--max-mbps` 一致。
    async def throttle(self, nbytes: int) -> None:
        if not self.max_mbps or self.max_mbps <= 0:
            return
        rate = self.max_mbps * 1024 * 1024          # B/s
        burst = rate * 0.25                          # 允许 0.25 s 的突发额度
        now = time.monotonic()
        self._tb_tokens = min(burst, self._tb_tokens + (now - self._tb_last) * rate)
        self._tb_tokens -= nbytes
        if self._tb_tokens < 0:
            await asyncio.sleep(-self._tb_tokens / rate)
            self._tb_tokens = 0.0
            # ⚠ 时间基准必须推到**睡醒之后**：这次睡眠已经把那段时间的额度用掉了；
            # 若仍以入睡前的时刻为基准，睡醒后会把同一段时间的额度**再补一遍**，
            # 稳态速率就会翻倍 —— 实测 2 MB/s 的档位跑出 3.88 MB/s，被测试抓住。
            self._tb_last = time.monotonic()
        else:
            self._tb_last = now


# --------------------------------------------------------------------------- HTTP：交付道

async def h_index(req: web.Request) -> web.Response:
    hub: Hub = req.app["hub"]
    return web.Response(text=PANEL_HTML.replace("__TOKEN__", hub.token),
                        content_type="text/html", charset="utf-8")


async def h_pair(req: web.Request) -> web.Response:
    """配对信息（`GET /api/v1/pair?token=…`）—— **面板顶部那一块的全部内容**。

    一次请求给出配对的**三件套**（供其它程序显示/转达给手机）：
      · **网址**：`url`（整行可粘贴）与 `hosts[]`（全部网卡候选，含虚拟网卡标注）；
      · **token**：`token`；
      · **二维码**：`qr_svg` / `qr_matrix` / `qr_png_b64` 三形态（逐格一致）。

    **这是唯一要求 token 的读接口**，因为它返回的正是 token 本身：
    不鉴权等于把凭据广播给同网段任何人。其它读接口（`/api/v1/sessions`）
    维持"局域网内不鉴权"的现状 —— 但那不返回凭据，性质不同。

    ⚠ 同理：这些内容**不得进 `capabilities`**（那个接口无需 token，见其 docstring）。

    `host` / `port` 可显式指定（多张网卡、手机要走另一张时用）；
    不指定时用启动时钉住的首选地址（`lan_addresses()` 的第 1 个）。
    """
    hub: Hub = req.app["hub"]
    if req.query.get("token") != hub.token:
        return err(403, "bad_token", "token 不符（此接口返回凭据，必须鉴权）")
    host = str(req.query.get("host") or hub.lan_ip or "127.0.0.1")
    try:
        port = int(req.query.get("port") or hub.port or PAIR_DEF_PORT)
    except ValueError:
        return err(400, "bad_port", "port 必须是整数")
    uri = pair_uri(host, port, hub.token)

    # 全部候选地址：多网卡机器上"首选"未必是手机能到的那张（VPN/虚拟网卡/双网段）。
    # 面板原来只显示首选，于是选错时只能靠人去猜 —— 这里把候选连同网卡名一起给出来，
    # 并**明确标注虚拟网卡**（手机连不上它），`recommended` 标出与当前配对码一致的那个。
    cands = []
    for ip, name, virt in (hub.lan_candidates or [(host, "?", False)]):
        cands.append({
            "ip": ip, "adapter": name, "virtual": virt, "recommended": ip == host,
            "url": f"http://{ip}:{port}/?token={hub.token}",
            "pair_uri": pair_uri(ip, port, hub.token),
        })
    if not any(c["ip"] == host for c in cands):
        # 调用方用 `?host=` 指定了一个不在候选里的地址（比如手机连的是同网段另一台机器的
        # 转发地址）⇒ 也要把它列进去，且标为 recommended，否则调用方会以为自己的选择没生效。
        cands.insert(0, {"ip": host, "adapter": "（调用方指定）", "virtual": False,
                         "recommended": True,
                         "url": f"http://{host}:{port}/?token={hub.token}",
                         "pair_uri": uri})

    return web.json_response({
        "ok": True,
        "pair_uri": uri,
        "host": host,
        "port": port,
        "token": hub.token,
        "url": f"http://{host}:{port}/?token={hub.token}",
        # 二维码**三形态**（共用同一个模块矩阵，逐格一致；判据 A8f）：
        #   svg    —— 面板内联进 DOM / 任何矢量渲染器
        #   matrix —— 0/1 字符串矩阵，**任何语言**都能自己画（含静区说明）
        #   png    —— base64 PNG，存盘/贴文档/丢给别的工具
        "qr_svg": qr_svg(uri),
        "qr_matrix": qr_matrix_json(uri),
        "qr_png_b64": qr_png_b64(uri),
        "qr_formats": qr_formats(uri),
        "hosts": cands,
        "note": ("扫这个码等于拿到地址与 token（<b>等同凭据</b>：别投屏、别外传）。"
                 "手机端 m0probe-1.11.1+：「扫码配对」→ 自动填入并连接。"
                 "若这里显示「未安装 qrcode 库」，按右边的地址 + token 手输一样能连。"),
    })


def _routes_of(app: web.Application) -> list:
    """导出**实际生效的**端点清单（`[{method, path}]`），供判据做精确的存在性比对。

    `path` 是路由模板（如 `/api/v1/sessions/{sid}/thumb/latest`），参数名是服务端内部的，
    判据比对时应当把 `{…}` 归一成 `{}` —— 声明里写 `{session_id}` 还是 `{sid}` 都对，
    但**路径结构**必须一致（少一段、多一段都不行）。
    """
    out = []
    for r in app.router.routes():
        m = getattr(r, "method", None)
        path = getattr(getattr(r, "resource", None), "canonical", None)
        if path and m and m not in ("HEAD", "OPTIONS"):
            out.append({"method": str(m).upper(), "path": str(path)})
    return sorted(out, key=lambda x: (x["path"], x["method"]))


async def h_sessions(req: web.Request) -> web.Response:
    hub: Hub = req.app["hub"]
    data = {
        "sessions": [s.summary() for s in hub.sessions_newest_first()],
        "ws_clients": len(hub.ws),
        "profile_id": hub.profile_id,
        "profile": hub.profile,
        "server_started_at": hub.started_at,
        "data_dir": str(hub.data_dir),
    }
    return web.json_response(data)


def _token_ok(req: web.Request, hub: Hub) -> bool:
    """token 鉴权：查询串或 `X-Token` 头。**两种都接受**是因为 MJPEG 的消费方
    （`cv2.VideoCapture` / `ffmpeg`）多半只会拼 URL，而普通程序更喜欢放请求头。"""
    if req.query.get("token") == hub.token:
        return True
    return bool(hub.token) and req.headers.get("X-Token") == hub.token


async def h_capabilities(req: web.Request) -> web.Response:
    """**能力与版本声明**（`GET /api/v1/capabilities`，无需 token）。

    上位机启动时应当**先读这个接口**，而不是假定"某个接口应该存在" ——
    这块组件会随需求演进，硬编码路径猜测迟早对不上。

    ⚠ 本接口**不含任何凭据**：它无需 token 就能读，所以绝不能把 token / 配对码放进来。
    """
    hub: Hub = req.app["hub"]
    return web.json_response({
        "ok": True,
        "server": "phonecamera-pc",
        "server_version": SERVER_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "lanes": {
            "monitor": {
                "purpose": "实时看画面（现场摆位/在线跟踪）",
                "transport": "ws(thumb) + http-mjpeg",
                "lossy": True,
                "note": ("**可丢**（不排队、不落盘）。只用于「看得见」，不得用于测量/分析。"
                         "`capture_mode=live` 时，实时道的彩色帧**也走这条管线**"
                         "（`POST /api/v1/live/frame` → 本 MJPEG）—— 于是「live 的画面是彩色」"
                         "这件事可以被同一条判据验出来（P13）。"),
                "endpoints": {"mjpeg": "/stream/mjpeg", "thumb_latest":
                              "/api/v1/sessions/{session_id}/thumb/latest"},
            },
            "delivery": {
                "purpose": "**每一帧**的原始 Y 平面（第 24 轮合并：交付道 = 唯一的留存道）",
                "transport": "先写手机本地 → http-chunked（分片 + 断点续传 + sha256）",
                "kind": KIND_DELIVERY, "lossy": False,
                "note": ("**不抽帧、不压缩**：sensor 输出多少帧就留多少帧"
                         "（跳过前 5 帧预热，跳过数进账本）。全帧率 83.0 MB/s "
                         "远超链路实测 5.5 MB/s ⇒ 采集期先落手机本地、边采边传，"
                         "传不完的停采后排空。可录时长 = 手机可用空间 ÷ 实测写入速率。"
                         "⚠ 默认不再抽帧 ⇒ 写盘量约 ×6，**必须做开/关 A/B 实测**。"),
                "knobs": {
                    "plan.interval_ms": ">0 时退回抽帧（**降载**：语义不再是\"每一帧都留\"）",
                    "profile.deliver_fps": "同上，作为 interval_ms 的备选来源",
                    "profile.store_budget_gb": "留存预算（默认 = 剩余空间 50%）",
                    "profile.store_reserve_gb": "余量保护（默认 2 GB，触线熔断）",
                },
                "ledger": {
                    "state": ["idle", "recording", "uploading", "done", "aborted_no_space"],
                    "two_gaps": ("gaps_on_device（采集期没写进本地，**只能重采**）vs "
                                 "upload_gaps（写进了但没传到，**可以重传**）—— 不许合并"),
                    "what": "见 /api/v1/host/status 的 `store` 段",
                },
            },
            "live": {
                "purpose": "**只看不留**：只实时回传，不储存、也不在事后回传额外的数据（第 27 轮）",
                "transport": "http 二进制（POST /api/v1/live/frame，JPEG）—— 避开 WS 文本帧的 base64 膨胀",
                "kind": MODE_LIVE, "lossy": True, "color": True,
                "mode": f"会话级 `capture_mode={MODE_LIVE}`（默认是 `{MODE_DELIVER}`）",
                "no_persist": True,
                "path_id": LIVE_PATH_ID, "path_rev": LIVE_PATH_REV,
                "note": ("⚠ **本道跑完不留任何东西**：没有 `frames/`、没有逐帧 sha256、"
                         "`store.applicable=false`、`delivery_complete` 判 **`null`（不适用）"
                         "而非 `false`** —— 后者会被读成「判了、没传完」，于是有人去重传一份"
                         "根本不存在的留存。设备零留存（用户口径），PC 侧只把帧推给监视道订阅者"
                         "（`/stream/mjpeg`）供现场回看，**不落盘**。"
                         "App 侧是**串行闭环**（`acquireLatestImage → 编码 → 阻塞 POST → 回到 acquire`）"
                         "⇒ 目标是**延迟不累积**（有队列时第 N 帧延迟 = 队列长 × 单帧时间，"
                         "会随积压线性增长），不是高 fps。"
                         "⚠ 彩色 JPEG **有损**：本道的画面**不得用于测量/定位**。"),
                "declared_counters": ["sent", "skipped", "dropped", "bytes", "mbps",
                                      "fps_actual", "latency_ms", "color", "width", "quality",
                                      "color_source", "color_source_requested",
                                      "rgba_supported", "reader_format"],
                "counter_semantics": ("`skipped` = 被跳过的陈帧（`acquireLatestImage` 的固有行为，"
                                      "**是设计**）；`dropped` = 队列满/编码失败等**异常**丢帧。"
                                      "**两者不许合并** —— 合并后「链路不够快」与「实现有 bug」"
                                      "就分不出来了。"),
                "knobs": {
                    "plan.capture_mode": f"`{MODE_LIVE}` 开本道；缺省 `{MODE_DELIVER}`",
                    "plan.live_width": "目标宽度（默认 960；高度按 4:3 推）",
                    "plan.live_quality": "JPEG 质量（默认 70）",
                    "plan.live_max_mbps": "安全旋钮：>0 时限流（默认 0 = 不限，符合「阻塞发送」口径）",
                    "plan.color_source": (
                        f"**颜色由谁算**：`{COLOR_SRC_YUV_BT601}` = 本进程读 Y/U/V 平面、"
                        f"自己按 BT.601 有限范围换算（现状）；`{COLOR_SRC_RGBA_HAL}` = 用"
                        f"`RGBA_8888` 让**相机 HAL** 做 YUV→RGB，本进程只下采样+编码。"
                        f"缺省 `{COLOR_SRC_YUV_BT601}`。**只在 `capture_mode={MODE_LIVE}` "
                        f"下有效**（交付道要原始 Y 平面，请求它会 400 "
                        f"`color_source_not_allowed`）；取值非法 ⇒ 400 `bad_color_source`。"
                        f"★ 用途是**判决性实验**：现场 live 画面偏绿而同场景相机原生照片正常，"
                        f"`{COLOR_SRC_RGBA_HAL}` **也偏绿** ⇒ 我们的换算清白（责任在 HAL/"
                        f"显示链路）；**只有** `{COLOR_SRC_YUV_BT601}` 偏绿 ⇒ 责任在我们的换算。"
                        f"⚠ 设备不支持 `RGBA_8888` 时会**降级**并在 "
                        f"`live.color_source_effective` 里体现 —— **那时二分不成立**"
                        f"（两条路是同一条），结论按未验证对待。"),
                },
                "endpoints": {"frame": "/api/v1/live/frame",
                              "mjpeg": "/stream/mjpeg"},
            },
        },
        # ---- 控制台（面板）功能 ↔ 对外接口的映射表 ----
        #
        # 这一段存在的理由：需求原话是"**对外接口要覆盖控制台的全部功能**"。这句话如果只写在
        # 文档里，就只能靠人定期核对 —— 而"面板加了个按钮、接口忘了补"正是最容易发生的漂移
        # （第 25 轮就在同类地方一次抓到三处"声明与实现不符"）。
        # 所以把它写成**声明**，再由判据 `A8` **从这份声明自身推导**逐项探针：
        #   · `endpoint` 必须真的在路由表里；
        #   · `auth` 声明要 token 的，**不带 token 必须 403**（并与 `auth.token_required` 交叉一致）；
        #   · `hostlink` 列出的方法名必须真的在 `pc/hostlink.py` 里定义（源码锚定）；
        #   · **反向**：面板源码里用到的每个路径都必须出现在本段里
        #     （新增控件忘了声明 ⇒ 判据立刻红，而不是等现场"这个功能别的程序调不到"）。
        "console": {
            "what": ("控制台（浏览器面板 `GET /`）能做的每一件事，与它的对外接口一一对应。"
                     "调用方要“用程序替代面板”时，照着 `features` 逐条调即可。"
                     "⚠ `endpoint` 是**真实路由模板**（如 `/api/v1/control/{action}`，"
                     "与 `capabilities.routes` 里的写法一致）；`probe` 是“能真打到的具体路径”，"
                     "判据用它验鉴权。`auth` 取值：`none` / `token` / `token+loopback`。"),
            "features": [
                {"id": "sessions_snapshot",
                 "label": "面板 3 秒刷新的全部内容（环境行 + 会话表 11 列）",
                 "panel": "顶部环境行 + 会话表", "method": "GET",
                 "endpoint": "/api/v1/sessions", "auth": "none",
                 "hostlink": ["sessions", "overview"],
                 "note": ("同一响应里既有会话数组（`sessions()`）又有 profile / 数据目录 / "
                          "WS 客户端数（`overview()`）——与面板**完全同口径**。")},
                {"id": "start", "label": "▶ 开始采集（PC→App start）",
                 "panel": "按钮", "method": "POST", "endpoint": "/api/v1/control/{action}",
                 "probe": "/api/v1/control/start",
                 "auth": "token", "hostlink": ["start"],
                 "note": ("`action=start`。**闭环**：App 回 `start_ack`（开流成功/失败原样透出），"
                          "下发的 profile 会被开流门控逐项比对，硬项不一致会**自动停流**。")},
                {"id": "stop", "label": "■ 停止",
                 "panel": "按钮", "method": "POST", "endpoint": "/api/v1/control/{action}",
                 "probe": "/api/v1/control/stop",
                 "auth": "token", "hostlink": ["stop"],
                 "note": ("`action=stop`。回 `stop_ack`（停在哪：已留存多少、队列剩多少）。"
                          "⚠ 停采集 ≠ 停回传：交付道的排空是**独立**的。")},
                {"id": "live_updates",
                 "label": "指令回执 / 状态变化的即时刷新（面板的推送通道）",
                 "panel": "面板 WebSocket", "method": "WS", "endpoint": "/api/v1/host/events",
                 "auth": "token+loopback", "hostlink": ["events"],
                 "note": ("面板那条 `/ws/panel` 是**面板专用**（缩略图 + 刷新通知；它断开只意味着"
                          "\"某个标签页没在看\"）；对外必须用 `/api/v1/host/events`（采集生命周期"
                          "事件）。两者**不许混用** —— 混了就会把“刷新页面”记成“设备掉线”。")},
                {"id": "monitor_stream", "label": "实时画面（监视道灰度缩略图 / live 模式的彩色帧，同一条管线）",
                 "panel": "监视道区块 + 实时道区块", "method": "GET", "endpoint": "/stream/mjpeg",
                 "auth": "token+loopback", "hostlink": ["mjpeg_url"],
                 "note": ("MJPEG，**有损且可丢**。任何测量/分析必须走交付道。"
                          "⚠ 两条道的画面**都从这里出去**（帧里带 `lane` / `color` 元数据）："
                          "`lane=thumb,color=false` 是监视道（灰度）；`lane=live,color=true` 是"
                          "实时道（彩色，`capture_mode=live` 时才有）。消费方**必须自己看这两个字段** —— "
                          "把 live 帧当成交付画面去做测量，就是静默用错数据。")},
                {"id": "live_mode",
                 "label": "实时模式：只看不留（彩色，设备零留存）",
                 "panel": "「开始采集」旁边的采集模式选择（deliver / live）",
                 "method": "POST", "endpoint": "/api/v1/control/{action}",
                 "probe": "/api/v1/control/start",
                 "auth": "token", "hostlink": ["start"],
                 "note": ("`capture_mode=live`：**交付道整体关闭**（不落盘、不起 writer、不排空、"
                          "App 侧连 WS `thumb` 也关），只跑实时道（彩色 JPEG，串行闭环，"
                          "延迟不累积）。取值非法 ⇒ **400 `bad_capture_mode`**"
                          "（不许静默退回默认 —— 那会把“只看不留”悄悄换成“全量落盘”）。"
                          "⚠ 面板上那两条 <option> 的取值与 `MODES` 的一致性由判据 **A9m** 盯着"
                          "（**从 `MODES` 自身推导**，加第三种模式而忘了改面板 ⇒ 立刻红）。")},
                {"id": "monitor_thumb_latest",
                 "label": "监视道最新一张（面板推送断线时的轮询兜底）",
                 "panel": "缩略图区", "method": "GET",
                 "endpoint": "/api/v1/sessions/{sid}/thumb/latest", "auth": "none",
                 "hostlink": ["thumb_url"],
                 "note": ("与面板用的是**同一条** URL。⚠ 它既不校验 token 也不限本机 —— "
                          "见下方 `console.note` 的已知开口说明。")},
                {"id": "pair", "label": "扫码配对（二维码 + 配对码 + 地址 + token）",
                 "panel": "配对区块", "method": "GET", "endpoint": "/api/v1/pair",
                 "auth": "token", "hostlink": ["pair"],
                 "note": ("**唯一要求 token 的读接口** —— 因为它返回的正是 token 本身。"
                          "二维码给三种形态（svg / matrix / png），共用同一个模块矩阵。")},
            ],
            "beyond_console": [
                {"id": "host_status", "method": "GET", "endpoint": "/api/v1/host/status",
                 "auth": "token+loopback", "hostlink": ["status"],
                 "why": ("面板没有对应控件，但它是面板数据的**超集**（同口径 + `gate` 三态原样"
                         "透出 + 交付收尾三态）。轮询状态优先用它。")},
                {"id": "capabilities", "method": "GET", "endpoint": "/api/v1/capabilities",
                 "auth": "none", "hostlink": ["capabilities"],
                 "why": ("启动时先读它。⚠ **不含凭据**，所以配对信息不在这里 —— 在 "
                         "`/api/v1/pair`（需 token）。")},
            ],
            "note": ("本段是“控制台能做的”**下界**：对外接口还另有 `beyond_console` 里那几条，"
                     "以及**设备面**接口（`/api/v1/files/*`、`/api/v1/live/frame`、`/ws` —— "
                     "那是手机 App 用的，不是给本机程序调用的）。"
                     "⚠ **已知开口（有意保留，见 docs 第 9 节）**：`/api/v1/sessions` 与 "
                     "`/api/v1/sessions/{sid}/thumb/latest` 既不校验 token、也不限本机 —— "
                     "这是为了保住“用手机浏览器打开面板做现场自检”这个手段（README 自检第 2 条）。"
                     "它们不含凭据，但**含画面**；要收紧就得放弃那个手段，故显式记录而非静默放过。"
                     "⚠ **第 28 轮：`live` 模式已升为面板控件**（`features` 里的 `live_mode`）—— "
                     "在此之前它只存在于接口，属 `beyond_console`；现在面板能选了，"
                     "**声明必须跟着搬**，否则这张表就说了一句假话。"),
        },
        "auth": {
            "token_query_or_header": "token（查询串 `?token=` 或请求头 `X-Token`）",
            "loopback_only": ["/api/v1/host/*", "/stream/*"],
            "token_required": ["/api/v1/host/status", "/api/v1/host/events",
                               "/stream/mjpeg",
                               "/api/v1/pair", "/api/v1/control/*"],
            # **显式记录"不校验 token"的那些**（不是遗漏，是有意开口）。
            #
            # 为什么不加上就完事：本项目已经吃过"声明为假"的亏（1.13.0 之前
            # `/api/v1/control/*` 文档写着要 token、代码从不校验）。反过来，
            # **"没有声明"也是一种静默**：将来有人看到这里能匿名访问，无法从代码里
            # 判断那是"设计如此"还是"忘了加"。写出来之后，判据 A9 可以交叉检查
            # "声明不校验 token 的，**不带 token 必须能通**"—— 于是这条开口被锁住形状：
            # 它只允许保持现状，不允许再悄悄扩大（新增一条会有人看见）。
            "no_token": ["/", "/ws", "/ws/panel",
                         "/api/v1/sessions", "/api/v1/sessions/{sid}/thumb/latest",
                         "/api/v1/capabilities",
                         "/api/v1/files/*", "/api/v1/linktest", "/api/v1/live/frame"],
            "no_token_note": ("`no_token` 里前四条是**设备面/调试入口**（手机 App 用，或"
                              "「用手机浏览器打开面板现场自检」这个手段）；后三条是**已知开口**，"
                              "见 docs §9 —— 其中 `/api/v1/live/frame` 是第 27 轮新增的实时道，"
                              "它不落盘、不返回凭据，但仍允许同网段塞画面。**要收紧必须上真机验证**，"
                              "故显式留档而非静默放过。"),
            "note": ("loopback_only 的接口**只对本机开放**（按对端地址判，不认 `Host` 头）。"
                     "设备侧接口（/api/v1/files/*、/ws）必须监听 0.0.0.0，两者共用端口，"
                     "所以这层隔离是必需的，不是可选的。"),
        },
        "limits": {"chunk_max": CHUNK_MAX, "mjpeg_queue": MJPEG_QUEUE,
                   "host_event_queue": HOST_EVENT_QUEUE, "max_thumbs_per_session": MAX_THUMBS},
        # 端点清单**从实际路由表导出**（不是手写清单）。
        #
        # 为什么要有这一项：本块里的 `console.features` 会声明"某个功能对应的端点是 X"，
        # 而"X 真的存在吗"如果只靠 HTTP 探针去猜（404 到底是路由没有、还是业务说会话不存在？），
        # 判据自己就不可靠。导出真实路由表之后，"声明 vs 存在"可以**精确比对**。
        # ⚠ 它是**实现的事实**，不是意图：`app.router.routes()` 拿不到的东西，
        # 就不可能出现在这里 —— 所以这一项永远不会与实现漂移。
        # ⚠ WS 端点在里面记作 `GET`（aiohttp 用一个 GET 路由承载升级）；判据比对时按此归一。
        "routes": _routes_of(req.app),
    })


async def h_host_status(req: web.Request) -> web.Response:
    """机器可读状态（`GET /api/v1/host/status`，需 token，仅本机）。"""
    hub: Hub = req.app["hub"]
    if not _token_ok(req, hub):
        return err(403, "bad_token", "token 不符（查询串 token= 或请求头 X-Token）")
    return web.json_response(hub.host_status())


async def _host_event_pump(ws: web.WebSocketResponse, info: dict) -> None:
    """把队列里的事件推给订阅者。

    **为什么要独立任务**：投递方（控制道消息循环）只 `put_nowait`，不 `await` ——
    否则"某个订阅者网络慢"就会顺着 `await` 把控制道拖住（第 21 轮实测代价：
    面板假死把设备连接整个掀掉）。
    """
    q: asyncio.Queue = info["q"]
    try:
        while True:
            msg = await q.get()
            if info["dropped"]:
                # 慢消费者**必须能知道自己漏了**（不许静默）。
                # 漏事件本身不致命（事件是"通知"，真相在落盘与状态接口里），
                # 但如果它不知道漏了，就会以为"留存一直没进度"。
                await ws.send_str(json.dumps(
                    {"type": "events_dropped", "dropped": info["dropped"],
                     "note": "订阅队列满，这些事件被丢弃（丢最旧）。状态以 host/status 为准。"},
                    ensure_ascii=False))
                info["dropped"] = 0
            await ws.send_str(msg)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


async def h_host_events(req: web.Request) -> web.WebSocketResponse:
    """事件广播（`GET /api/v1/host/events`，需 token，仅本机）。

    这是"广播"的实体：上位机订阅一次，之后**采集生命周期里的每个节点都会主动推给它**
    （会话开始 / 帧已验证 / 门控判决 / 停止 / 留存进度 / 交付收尾），
    不必轮询目录或状态接口。
    """
    hub: Hub = req.app["hub"]
    if not _token_ok(req, hub):
        return web.json_response({"error": "bad_token", "message": "token 不符"}, status=403)
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(req)
    q: asyncio.Queue = asyncio.Queue(maxsize=HOST_EVENT_QUEUE)
    info = {"q": q, "dropped": 0}
    hub.host_ws[ws] = info
    print(f"[host] events 订阅者接入（当前 {len(hub.host_ws)} 个）")
    await ws.send_str(json.dumps({
        "type": "hello", "server_version": SERVER_VERSION,
        "protocol_version": PROTOCOL_VERSION, "queue_size": HOST_EVENT_QUEUE,
        "note": "队列满则丢最旧并回发 events_dropped 计数；真相以 host/status 与落盘为准。",
    }, ensure_ascii=False))
    pump = asyncio.create_task(_host_event_pump(ws, info))
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                # 只支持一个客户端指令：主动要一次当前状态（等价于 host/status）
                try:
                    m = json.loads(msg.data)
                except Exception:
                    m = {}
                if m.get("type") == "status":
                    await ws.send_str(json.dumps({"type": "status",
                                                  "status": hub.host_status()},
                                                 ensure_ascii=False))
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        pump.cancel()
        hub.host_ws.pop(ws, None)
        print(f"[host] events 订阅者断开（剩 {len(hub.host_ws)} 个）")
    return ws


async def h_stream_mjpeg(req: web.Request) -> web.StreamResponse:
    """实时视频流（`GET /stream/mjpeg`，需 token，仅本机）。

    格式是 `multipart/x-mixed-replace` —— 选它的唯一理由是**消费方零依赖**：
    `cv2.VideoCapture("http://…/stream/mjpeg?token=…")`、`ffmpeg`、`<img src=…>`
    都能直接读，不需要在本项目里发 SDK。

    ⚠ **观感与分辨率由监视道决定**（帧率 = 缩略图节奏，默认 1 Hz；实时档可提到 ~10 fps）。
    它是**有损**且**会丢帧**的通道：**任何测量/分析都必须取交付道**（`frames/`）。
    """
    hub: Hub = req.app["hub"]
    if not _token_ok(req, hub):
        return err(403, "bad_token", "token 不符（查询串 token= 或请求头 X-Token）")
    resp = web.StreamResponse(status=200, headers={
        "Content-Type": f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
        # 不缓存：否则中间层会把第一帧缓存住，画面永远冻在第一张
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Connection": "close",
    })
    await resp.prepare(req)
    q: asyncio.Queue = asyncio.Queue(maxsize=MJPEG_QUEUE)
    key = id(q)
    hub.mjpeg_subs[key] = q
    print(f"[host] mjpeg 订阅者接入（当前 {len(hub.mjpeg_subs)} 个）")

    async def write_part(jpeg: bytes, seq: int, sid: str) -> None:
        head = (f"--{MJPEG_BOUNDARY}\r\n"
                f"Content-Type: image/jpeg\r\n"
                f"Content-Length: {len(jpeg)}\r\n"
                f"X-Seq: {seq}\r\nX-Session-Id: {sid}\r\n\r\n").encode("ascii")
        await resp.write(head + jpeg + b"\r\n")

    try:
        # 先把**手上最新的一张**发出去：否则订阅后要等到下一张才有画面
        # （1 Hz 档下最长等 1 秒，现场会以为"流没连上"）。
        if hub.last_thumb_jpeg is not None:
            m = hub.last_thumb_meta or {}
            await write_part(hub.last_thumb_jpeg, int(m.get("seq") or 0),
                             str(m.get("session_id") or ""))
        while True:
            jpeg, seq, sid = await q.get()
            await write_part(jpeg, seq, sid)
    except (asyncio.CancelledError, ConnectionResetError, ConnectionAbortedError):
        # 消费方断开是**正常事件**（比如程序重启）。这里必须只做清理，
        # 绝不向上抛成 500 —— 否则控制台会被"订阅者关页面"刷满假故障。
        pass
    finally:
        hub.mjpeg_subs.pop(key, None)
        print(f"[host] mjpeg 订阅者断开（剩 {len(hub.mjpeg_subs)} 个）")
    return resp


async def h_init(req: web.Request) -> web.Response:
    """声明一个文件。**幂等**（§5.6）：同一 (session_id, device_serial, seq) 重复 init
    返回同一个 file_id 与当前偏移 —— 断网重传因此不会产生重复文件。"""
    hub: Hub = req.app["hub"]
    try:
        body = await req.json()
    except Exception:
        return err(400, "bad_json", "请求体必须是 JSON")

    sid = str(body.get("session_id") or "")
    serial = str(body.get("device_serial") or "")
    seq = body.get("seq")
    name = str(body.get("name") or "")
    size = body.get("size")
    if not SESSION_ID_RE.match(sid):
        return err(400, "bad_session_id", "session_id 必须匹配 ^[A-Za-z0-9_-]{1,64}$")
    if not isinstance(seq, int) or seq < 0:
        return err(400, "bad_seq", "seq 必须是非负整数")
    if not NAME_RE.match(name):
        return err(400, "bad_name", "name 只允许字母/数字/下划线/连字符/点/空格")
    if not isinstance(size, int) or size <= 0:
        return err(400, "bad_size", "size 必须是正整数")

    meta = body.get("meta") or {}
    if not isinstance(meta, dict):
        return err(400, "bad_meta", "meta 必须是对象")

    # ---- `kind`：第 24 轮合并后**只有一个合法值** ----
    # ⚠ 这段校验是**防静默**的，不是装饰：老的 App（≤1.12.0）会带 `kind="archive"` 发归档帧，
    #   而归档帧与交付帧**共用同一个 seq 空间**（第 23 轮的教训）。若这里默默放行，
    #   归档 seq=7 会写进交付 seq=7 的位置（落盘同名 `frame_000007.yuv`、内存同表 `s.frames`）
    #   ⇒ **后到的静默覆盖先到的，而两边都显示"已 verified"**。
    #   宁可显式拒绝，让老客户端立刻报错、逼它升级。
    kind = str(body.get("kind") or KIND_DELIVERY)
    if kind != KIND_DELIVERY:
        return err(400, "lane_merged",
                   f"kind={kind!r} 已废弃：第 24 轮起归档道并入交付道（交付道 = 每一帧都留）。"
                   "请升级 App（≥ m0probe-1.13.0）；继续用旧版会与交付帧共用 seq 空间"
                   "而**静默互相覆盖**。")

    # 幂等键：合并后**不再含 kind**（只有一条道，没有再需要区分的东西）。
    # ⚠ 这条注释是留给人看的：将来**再出现两条共用 seq 空间的流**时，
    #   "道的名字"必须重新进这个键，否则后到的会静默覆盖先到的。
    key = f"{sid}|{serial}|{seq}"
    if key in hub.idem:
        fr = hub.files[hub.idem[key]]
        if fr.size != size or fr.name != name:
            # 幂等键撞上不同内容 —— 必须显式拒绝，不能"以先到的为准"
            return err(409, "idem_conflict",
                       "同一幂等键（session_id+device_serial+seq）声明了不同的 size/name")
        return web.json_response({"file_id": fr.file_id, "resume_offset": fr.received,
                                  "chunk_max": CHUNK_MAX, "state": fr.state,
                                  "protocol_version": PROTOCOL_VERSION})

    decl = {
        "model": str(body.get("model") or ""), "app_version": str(body.get("app_version") or ""),
        "calib_id": str(body.get("calib_id") or ""), "delivery": str(body.get("delivery") or ""),
        "capture_path_id": str(body.get("capture_path_id") or ""),
        "capture_path_rev": int(body.get("capture_path_rev") or 0),
    }
    s = hub.session(sid)
    if s is None:
        s = hub.create_session(sid, serial, **decl)
        s.absorb_declaration(device_serial=serial)
    else:
        # 会话已存在（控制道 session_begin 先建的）⇒ 把 init 里的溯源字段补进去。
        # 少了这一步，manifest 的溯源字段就永远是空的（第 20 轮实测踩到）。
        s.absorb_declaration(device_serial=serial, **decl)

    # ---- 模式互斥（第 27 轮）----
    # `live` 的定义就是"交付道整体关闭"（不落盘、不排空、不回传）。所以 live 会话里
    # 收到交付帧必须是**显式错误**：要么 App 的模式分叉没做到（它在偷跑交付道），
    # 要么调用方把两种模式搞混了。**不许静默丢弃** —— 静默的后果是有人以为
    # "这条路还在工作"，而实际上那份数据根本没有留存（R38 不可逆）。
    if s.capture_mode == MODE_LIVE:
        s.mode_conflicts += 1
        s.mode_conflict_last = (f"交付帧打到了 live 会话（seq={seq}, name={name}）"
                                "—— 「不储存」没做到")
        s.log("⚠ 模式互斥告警：" + s.mode_conflict_last)
        return err(409, "mode_conflict",
                   "本会话是 capture_mode=live（只看不留）：交付道整体关闭，不接受交付帧。"
                   "App 若在 live 模式下仍往 /api/v1/files/* 发帧，说明模式分叉没做到。")

    fid = secrets.token_hex(8)
    fr = FileRecord(file_id=fid, session_id=sid, device_serial=serial, seq=seq,
                    name=name, size=size,
                    expect_sha256=(body.get("sha256") or None), meta=meta,
                    part=s.root / "incoming" / f"{fid}.part")
    fr.part.touch()
    hub.files[fid] = fr
    hub.idem[key] = fid
    s.frames[seq] = fr
    # **"停采集 ≠ 停交付"**（双流方案的本意）：停流只是不再产生新帧，队列里已经抽好的
    # 帧仍要传完 ⇒ 停流后收到帧是**预期行为**，不是旧会话串台。但它必须**计数**，
    # 否则"明明停了怎么还在收"只能靠猜（第 21 轮把这条做成可观测）。
    if s.stopped_at:
        s.late_frames_after_stop += 1
        if s.late_frames_after_stop <= 2 or s.late_frames_after_stop % 50 == 0:
            s.log(f"收尾中：停采集后又到 1 帧（累计 {s.late_frames_after_stop}）seq={seq}"
                  f" —— 交付道本就独立于采集，这是预期产物")
    s.log(f"init seq={seq} name={name} size={size} file_id={fid}")
    s.write_manifest()
    return web.json_response({"file_id": fid, "resume_offset": 0, "chunk_max": CHUNK_MAX,
                              "state": "partial", "protocol_version": PROTOCOL_VERSION})


RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


async def h_put_chunk(req: web.Request) -> web.Response:
    """分片上传。带 `Content-Range: bytes a-b/total` 与 `X-Chunk-Crc32`。

    设计取舍（§7.2）：分片级 CRC32 只做"快速失败"；**文件的真相由 complete 的
    SHA-256 决定**。所以这里即使一切正常也只返回 partial（除了收满时的提示），
    绝不提前说"完成"。
    """
    hub: Hub = req.app["hub"]
    fid = req.match_info["file_id"]
    fr = hub.files.get(fid)
    if not fr:
        return err(404, "unknown_file", "file_id 未知（服务端可能重启过）")
    if fr.verified:
        return web.json_response({"state": "verified", "resume_offset": fr.received,
                                  "note": "already verified（幂等）"})

    cr = req.headers.get("Content-Range", "")
    m = RANGE_RE.match(cr)
    if not m:
        return err(400, "need_content_range", "缺少或无法解析 Content-Range: bytes a-b/total")
    start, end, total = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if total != fr.size:
        return err(400, "total_mismatch", "Content-Range 的 total 与 init 声明的 size 不符")
    if end < start:
        return err(400, "bad_range", "end < start")
    data = await req.read()
    if len(data) != end - start + 1:
        return err(400, "length_mismatch",
                          f"body 长度 {len(data)} != 区间长度 {end - start + 1}")

    if start != fr.received:
        # 断点续传入口：把**真实偏移**告诉 App，让它从这里继续（不猜、不补齐）
        return web.json_response({"state": "partial", "resume_offset": fr.received,
                                  "note": f"offset mismatch: have {fr.received}, got {start}"},
                                 status=409)

    want_crc = req.headers.get("X-Chunk-Crc32")
    if want_crc:
        got = f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"
        if got.lower() != want_crc.lower():
            fr.crc_failures += 1
            fr.meta.setdefault("crc_failures", 0)
            fr.meta["crc_failures"] = fr.crc_failures
            # 不推进偏移、不写盘 ⇒ App 重传同一片即可
            return web.json_response({"state": "partial", "resume_offset": fr.received,
                                      "note": f"chunk crc mismatch: want {want_crc}, got {got}"},
                                     status=412)

    with fr.part.open("ab") as f:
        f.write(data)
    fr.received += len(data)
    await hub.throttle(len(data))
    s = hub.sessions.get(fr.session_id)
    if s:
        s.log(f"chunk seq={fr.seq} {start}-{end} (+{len(data)}B) -> {fr.received}/{fr.size}")
        # 分片粒度不刷新 manifest（每片都写会拖慢）；完成时统一写
    return web.json_response({"state": "partial", "resume_offset": fr.received,
                              "received": fr.received, "size": fr.size})


async def h_thumb_latest(req: web.Request) -> web.Response:
    """面板的监视道：返回该会话最新一张缩略图（JPEG）。

    不加缓存头、且面板每次带 `?t=` 破缓存 —— 监视道要的是"秒级看到现在"，
    缓存住反而会让人以为画面卡住。
    """
    hub: Hub = req.app["hub"]
    s = hub.session(req.match_info["sid"])
    if not s:
        return err(404, "unknown_session", "会话不存在")
    f = s.latest_thumb()
    if not f:
        return err(404, "no_thumb", "该会话还没有收到缩略图（App 未开流或未连接）")
    return web.Response(body=f.read_bytes(), content_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


async def h_linktest(req: web.Request) -> web.Response:
    """链路测速入口（App 的「链路测速」按钮用）。

    为什么要专门一条：现场只有 5.5 MB/s，必须把**链路能力**与
    **采集循环的开销**分开。这条只做"收字节、丢弃"，是最纯的链路测量 ——
    相机不开、不落盘、不校验，测出来的就是这条 WiFi + 手机 HTTP 栈的上限。
    """
    hub: Hub = req.app["hub"]
    n = 0
    t0 = time.monotonic()
    async for chunk in req.content.iter_chunked(1 << 20):
        await hub.throttle(len(chunk))
        n += len(chunk)
    dt = max(time.monotonic() - t0, 1e-6)
    mbps = n / dt / 1024 / 1024
    print(f"[linktest] 收到 {n/1024/1024:.1f} MB 用时 {dt:.2f}s => "
          f"{mbps:.2f} MB/s ({mbps*8*1.048576:.0f} Mbps)")
    return web.json_response({"bytes": n, "seconds": round(dt, 3),
                              "mbps": round(mbps, 2),
                              "encoding": req.headers.get("Content-Encoding", "identity")})


async def h_live_frame(req: web.Request) -> web.Response:
    """实时道（`capture_mode=live`）：收一帧彩色 JPEG，**只给"看得见"，不落盘**。

    这是第 27 轮新增的**设备面**接口（手机 App 调）。设计要点逐条：

    · **为什么是 HTTP 二进制而不是复用 WS**：`live` 是"尽力发"（串行闭环，发完一帧才取
      下一帧）⇒ 它会把链路占满。同一条 WS 上跑这个量级，`start`/`stop` 的时延会被挤，
      而且 WS 走文本帧 + base64（**+33% 膨胀**）。HTTP 二进制两者都避开。
    · **串行闭环**在 App 侧实现（`acquireLatestImage → 编码 → 阻塞 POST → 回到 acquire`）：
      目标是**延迟不累积**，不是高 fps。所以这里**不排队、不缓冲**，收到就推给订阅者。
    · **不校验 token**：与 `/api/v1/files/*` 同属设备面（手机在局域网里直连），
      且它**不返回任何凭据**。⚠ 这条是**已知开口**（见 capabilities `auth.no_token`
      与 docs §9）：同网段的人可以往任意 session_id 塞画面。**必须写出来，不许静默。**
    · **不建会话**：未知 `session_id` 一律 404 —— 否则伪造/陈旧的帧会凭空造出会话目录
      （缩略图那条就是这么处理的，同一条纪律）。
    · **模式互斥**：`deliver` 会话收到实时帧 ⇒ 400 `mode_conflict` 并**计数**。
      这是判据 A9e 盯的反例 —— "App 站错了道"必须能看见，而不是静默被丢。
    """
    hub: Hub = req.app["hub"]
    sid = str(req.headers.get("X-Session-Id") or req.query.get("session_id") or "")
    body = await req.read()
    if not sid:
        return err(400, "bad_session_id", "缺 X-Session-Id 请求头")
    s = hub.session(sid)
    if s is None:
        # 与 thumb 一致：**不建会话**，但把话说明白（App 会据此重新声明会话）。
        hub.live_rejected_total += 1
        return err(404, "unknown_session",
                   "会话不存在（会话由 start / session_begin 建立）—— 本接口不建会话")
    if s.capture_mode != MODE_LIVE:
        s.mode_conflicts += 1
        s.mode_conflict_last = (f"live/frame 打到了 {s.capture_mode} 会话（seq="
                                f"{req.headers.get('X-Seq')}）—— App 站错了道")
        s.log("⚠ 模式互斥告警：" + s.mode_conflict_last)
        return err(400, "mode_conflict",
                   f"本会话是 capture_mode={s.capture_mode}；实时帧只属于 live 会话")
    if not body:
        s.live_rejected += 1
        return err(400, "empty_body", "帧体为空")
    if len(body) > LIVE_FRAME_MAX:
        s.live_rejected += 1
        return err(413, "frame_too_large", f"{len(body)} B 超过单帧上限 {LIVE_FRAME_MAX} B")
    if not body.startswith(JPEG_SOI):
        # ⚠ 必须有这条：没有它，把 2.76 MB 的原始 Y 灌进来也会"成功"，
        # 而那份数据既不是完整帧（无元数据）也不是可解码的图（全是垃圾）。
        s.live_rejected += 1
        return err(400, "bad_payload",
                   "实时道只接受 JPEG（首字节必须是 FFD8）；原始 Y 平面走交付道")

    seq = int(req.headers.get("X-Seq") or 0)
    ts = req.headers.get("X-Sensor-Timestamp-Ns")
    n = len(body)
    s.live_frames += 1
    s.live_bytes += n
    s.live_marks.append((time.monotonic(), n))
    while len(s.live_marks) > 4096:
        s.live_marks.pop(0)
    s.live_last = {"seq": seq, "bytes": n, "sensor_timestamp_ns": ts,
                   "color": True, "at": now_iso()}
    # 推给 MJPEG 订阅者（复用监视道那条管线 —— 语义完全一致："看到现在"，可丢）。
    hub.push_monitor(body, seq, sid,
                     {"sensor_timestamp_ns": ts, "lane": MODE_LIVE, "color": True})
    return web.json_response({"ok": True, "session_id": sid, "seq": seq,
                              "received": n, "live_frames": s.live_frames,
                              "lane": MODE_LIVE, "stored": False,
                              "note": "本道不落盘（设备零留存）"})


async def h_complete(req: web.Request) -> web.Response:
    """提交整文件 sha256 → **只有校验通过才 verified**（§7.2 / §5.6）。

    未通过时的处置是"保留已收分片、告诉 App 期望偏移"，而不是把文件删掉重传：
    重传整文件的代价与"继续传"完全一样（都是分片），但删掉会丢掉已经到手的字节。
    """
    hub: Hub = req.app["hub"]
    fr = hub.files.get(req.match_info["file_id"])
    if not fr:
        return err(404, "unknown_file", "file_id 未知（服务端可能重启过）")
    s = hub.sessions.get(fr.session_id)

    if fr.verified:                                  # 幂等：重复 complete 不重复触发
        if s:
            s.write_manifest()
        return web.json_response({"file_id": fr.file_id, "state": "verified",
                                  "sha256": fr.expect_sha256, "note": "already verified"})

    try:
        body = await req.json()
    except Exception:
        body = {}
    claim = str(body.get("sha256") or fr.expect_sha256 or "")

    if fr.received != fr.size:
        fr.failed_reason = f"incomplete: {fr.received}/{fr.size}"
        if s:
            s.log(f"complete REJECTED seq={fr.seq} {fr.failed_reason}")
            s.write_manifest()
        return web.json_response({"file_id": fr.file_id, "state": "partial",
                                  "resume_offset": fr.received,
                                  "note": fr.failed_reason}, status=409)

    got = sha256_file(fr.part)
    if claim and got.lower() != claim.lower():
        # 关键：**不删分片**。整文件校验失败可能是"传输错"或"App 侧算错"，
        # 两种都必须让 App 能重来；删掉等于强迫它从头传一遍。
        fr.failed_reason = "sha256 mismatch"
        if s:
            s.log(f"complete REJECTED seq={fr.seq} sha256 mismatch "
                  f"(claim={claim[:12]}…, got={got[:12]}…)")
            s.write_manifest()
        return web.json_response({"file_id": fr.file_id, "state": "partial",
                                  "resume_offset": 0,   # 从 0 重传（内容不可信）
                                  "note": "sha256 mismatch: 服务端保留分片，App 需从头重传"},
                                 status=422)

    seq = fr.seq
    ext = Path(fr.name).suffix or ".bin"
    # 落盘：合并后只有一条道 ⇒ 只有一个目录、一种命名。
    # ⚠ 第 23 轮这里曾经按 kind 分 `frames/` 与 `archive/`（两者都是 `.yuv`，
    #   不分就会静默覆盖）—— 现在"分"这件事本身消失了，因为**只有一条道**。
    out_dir = s.frames_dir
    stem = f"frame_{seq:06d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / f"{stem}{ext}"
    fr.part.replace(final)
    fr.final = final
    fr.verified = True
    fr.failed_reason = None
    fr.expect_sha256 = got

    # 帧级 JSON（§6.3）。**元数据来自 App，服务端只补它自己知道的量**：
    # sha256 / received_at / size —— 否则"校验过的字节"与"声明的元数据"之间就没有连接。
    fmeta = dict(fr.meta or {})
    fmeta.update({
        "seq": seq,
        "session_id": fr.session_id,
        "calib_id": fmeta.get("calib_id") or (s.calib_id if s else ""),
        "capture_path_id": fmeta.get("capture_path_id") or (s.capture_path_id if s else ""),
        "capture_path_rev": fmeta.get("capture_path_rev") or (s.capture_path_rev if s else 0),
        "file": final.name,
        "size_bytes": fr.size,
        "sha256": got,
        "verified_at": now_iso(),
        "server": {"protocol_version": PROTOCOL_VERSION, "chunk_crc_failures": fr.crc_failures},
    })
    (out_dir / f"{stem}.json").write_text(
        json.dumps(fmeta, ensure_ascii=False, indent=2), encoding="utf-8")

    s.verify_marks.append((time.monotonic(), fr.size))
    s.verify_marks[:] = s.verify_marks[-2000:]
    # ---- 开流门控：判到**定论**为止（pass/fail 冻结；incomplete 不冻结，等下一帧补齐）----
    # 为什么不能"只判第一帧就收工"：incomplete 是**取证不足**，随后的帧很可能就补齐了；
    # 冻结它等于把一次"还没看清"永久固定成"看不清"。但 fail 一旦成立就冻结并停流 ——
    # 配置前提错了，多采的每一帧都是按错配置采的。
    if not s.gate_checked:
        g = s.start_gate()
        s.start_gate_cache = g
        if g["status"] == "fail":
            s.gate_checked = True
            s.log(f"⚠ 开流门控失败：{g['hard_fail']} ⇒ 下发 profile 未被真正采纳，"
                  f"自动停流（这批数据不能按该配置使用）")
            print(f"[gate] session={s.session_id} FAIL hard_fail={g['hard_fail']} "
                  + json.dumps(g["checks"], ensure_ascii=False))
            await hub.broadcast({"type": "stop", "session_id": s.session_id,
                                 "reason": "start_gate_failed", "hard_fail": g["hard_fail"]})
            s.state = "gate_failed"
            hub.broadcast_event({"type": "gate", "session_id": s.session_id,
                                 "status": "fail", "hard_fail": g["hard_fail"]})
        elif g["status"] == "pass":
            s.gate_checked = True
            s.log(f"开流门控通过（{len(g['checks'])} 项硬检查）"
                  + (f"；{len(g['soft_notes'])} 条软注记" if g["soft_notes"] else ""))
            print(f"[gate] session={s.session_id} PASS checks={len(g['checks'])}"
                  + (f" 归一={len(g['soft_notes'])}" if g["soft_notes"] else ""))
            hub.broadcast_event({"type": "gate", "session_id": s.session_id,
                                 "status": "pass", "hard_fail": []})
        elif not s.gate_incomplete_logged:
            # 判不了 ⇒ **不停流**，但必须显式说出来（否则"没判"会被人当成"判过了"）
            s.gate_incomplete_logged = True
            s.log(f"开流门控判不了：{g['unevaluable']} 的回读值缺席 ⇒ 记为 incomplete，"
                  f"**不停流**（等后续帧补齐后再判；在此期间不要把这批数据当成已门控）")
            print(f"[gate] session={s.session_id} INCOMPLETE unevaluable={g['unevaluable']} "
                  f"（不停流）")
    rate = s.verify_rate_mbps()
    s.log(f"VERIFIED seq={seq} {final.name} {fr.size}B sha256={got[:12]}…"
          + (f"  交付速率={rate:.2f} MB/s" if rate else ""))
    s.write_manifest()

    # ACK 走 WS（§7.1）—— 只有 verified 才发；App 凭它删本地文件（§5.6）
    await hub.broadcast({"type": "ack", "file_id": fr.file_id, "session_id": fr.session_id,
                         "seq": seq, "state": "verified", "sha256": got})
    # 本机事件广播：上位机订阅一次就能收到"每来一帧"，不必轮询目录
    # （轮询会读到半写入的文件——帧级 json 与 .yuv 是分两步落的）。
    hub.broadcast_event({"type": "frame_verified", "session_id": fr.session_id, "seq": seq,
                         "bytes": fr.size, "sha256": got, "file": final.name,
                         "sensor_timestamp_ns": (fr.meta or {}).get("sensor_timestamp_ns"),
                         "at": now_iso()})
    return web.json_response({"file_id": fr.file_id, "state": "verified", "sha256": got,
                              "file": str(final)})


async def h_control(req: web.Request) -> web.Response:
    """面板按钮：start / stop（PC→App，§7.1）。

    ⚠ **必须校验 token**（第 25 轮补）。此前这里**没有任何鉴权**：
    `capabilities.auth.token_required` 与 `docs/对外接口与单一交付道.md §2.1` 都白纸黑字
    写着它需要 token，而代码不查 —— 又一条"声明为真"的欠账。后果不只是名不副实：
    `/api/v1/control/*` **不在 loopback_only 名单里**（名单只有 `/api/v1/host/`、`/stream/`），
    所以**同一局域网里任何一台机器都能启停采集**（让手机白录、写满存储、PC 建一堆空会话）。
    """
    hub: Hub = req.app["hub"]
    if not _token_ok(req, hub):
        return err(403, "bad_token", "token 不符（查询串 token= 或请求头 X-Token）")
    action = req.match_info["action"]
    if action not in ("start", "stop"):
        return err(404, "bad_action", "只支持 start / stop")
    try:
        body = await req.json()
    except Exception:
        body = {}
    sid = str(body.get("session_id") or "")
    if action == "start":
        sid = sid or make_session_id()
        serial = str(body.get("device_serial") or "unknown")
        try:
            s = hub.create_session(sid, serial)  # profile 由 hub 统一注入，保证与 config 一致
        except ValueError as e:
            return err(400, "bad_session_id", str(e))
        # ⚠ 默认 `interval_ms=0` = **不抽帧**（第 24 轮合并后的语义：每一帧都留）。
        # 写 >0 是**降载**，会让语义退回"只留一部分"，账本里 every_frame 会变 false。
        s.plan = body.get("plan") or {"mode": "interval", "interval_ms": 0, "count": 0}
        # ---- 采集模式（第 27 轮）----
        # `deliver`（默认）= 每一帧都留；`live` = 只看不留（交付道整体关闭、允许彩色）。
        # ⚠ 取值非法必须**显式报错**：静默退回默认会让「我要只实时回传」变成
        # 「结果没实时传、还全量落盘」，反向也一样 —— 两种现场行为差一个量级，
        # 靠默认值兜住等于把用户的意图换掉而他不知道。
        mode = str(body.get("capture_mode") or s.plan.get("capture_mode") or MODE_DELIVER)
        if mode not in MODES:
            return err(400, "bad_capture_mode",
                       f"capture_mode 只能是 {' | '.join(MODES)}，收到 {mode!r}")
        s.capture_mode = mode
        # ---- 颜色来源（第 29 轮：二分定位"绿偏"是谁的锅）----
        # `yuv_bt601` = 本进程按 BT.601 自己换算；`rgba_hal` = 相机 HAL 换算（我们只编码）。
        # ⚠ 非法取值**显式 400**，与 capture_mode 同一理由：这是一次**判决性实验**，
        #   静默退回默认会让"我请求了 A"与"我跑的是 B"看起来一模一样，
        #   结论（偏色归属）会整个反过来，而**面板上没有任何异常可看**。
        src = str(body.get("color_source") or s.plan.get("color_source")
                  or COLOR_SRC_YUV_BT601)
        if src not in COLOR_SOURCES:
            return err(400, "bad_color_source",
                       f"color_source 只能是 {' | '.join(COLOR_SOURCES)}，收到 {src!r}")
        # ⚠ 交付道请求 rgba_hal 必须**在这里就拒**：那条路要的是原始 Y 平面，
        #   RGBA reader 没有 Y 平面。若放行，设备端会静默退回 yuv_bt601，
        #   而会话记录里写着用户请求了 rgba_hal ⇒ 声明与实际不符。
        if src == COLOR_SRC_RGBA_HAL and mode != MODE_LIVE:
            return err(400, "color_source_not_allowed",
                       f"color_source={COLOR_SRC_RGBA_HAL} 只在 capture_mode={MODE_LIVE} 下"
                       f"有意义（交付道要的是原始 Y 平面，RGBA 没有）")
        s.color_source = src
        s.plan = dict(s.plan)
        s.plan["color_source"] = src
        s.state = "armed"
        s.write_manifest()
        n = await hub.broadcast({"type": "start", "session_id": sid, "plan": s.plan,
                                 "capture_mode": mode, "color_source": src,
                                 "profile": hub.profile, "profile_id": hub.profile_id,
                                 "token": hub.token})
        s.log(f"start 指令已发出（接收方 {n} 个）plan={s.plan} capture_mode={mode} "
              f"color_source={src}")
        s.write_manifest()
        return web.json_response({"session_id": sid, "sent_to": n, "plan": s.plan,
                                  "capture_mode": mode, "color_source": src})
    # stop
    s = hub.session(sid)
    if s:
        s.state = "stopped"
        s.ended_at = now_iso()
        s.write_manifest()
    n = await hub.broadcast({"type": "stop", "session_id": sid})
    if s:
        s.log(f"stop 指令已发出（接收方 {n} 个）")
    return web.json_response({"session_id": sid, "sent_to": n})


# --------------------------------------------------------------------------- WS：控制 + 监视

async def h_ws(req: web.Request) -> web.WebSocketResponse:
    hub: Hub = req.app["hub"]
    if req.query.get("token") != hub.token:
        return web.json_response({"error": "bad_token",
                                  "message": "token 不符（token 即轻量鉴权，防止同网段误连）"},
                                 status=403)
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(req)
    hub.ws.add(ws)
    peer = req.remote or "?"
    dev_serial = ""
    hub.broadcast_event({"type": "device_online", "peer": peer,
                         "clients": len(hub.ws), "at": now_iso()})
    # 已就"该会话未知"回过话的集合：同一条连接里每个未知会话只提醒一次（防刷屏）
    unknown_thumb_notified: set = set()
    # 第一条：把当前 profile 与 token 给 App（§7.1 `config`）
    await ws.send_str(json.dumps({
        "type": "config", "protocol_version": PROTOCOL_VERSION,
        "profile_id": hub.profile_id, "profile": hub.profile,
        "session_dir": str(hub.sessions_dir), "token": hub.token,
        "server_time": now_iso(),
    }, ensure_ascii=False))
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                m = json.loads(msg.data)
            except Exception:
                await ws.send_str(json.dumps({"type": "error", "code": "bad_json"}, ensure_ascii=False))
                continue
            t = m.get("type")
            if t == "hello":
                dev_serial = str(m.get("device_serial") or "")
                # 只有 hello 里**明确带了 session_id** 才建会话：正常流程下会话由
                # `start` 创建，App 的 hello 不带它（否则每次连接都会留一个空会话目录）。
                sid = str(m.get("session_id") or "")
                if sid and SESSION_ID_RE.match(sid):
                    s = hub.create_session(sid, dev_serial,
                                           model=str(m.get("model") or ""),
                                           app_version=str(m.get("app_version") or ""),
                                           calib_id=str(m.get("calib_id") or ""),
                                           delivery=str(m.get("delivery") or "stream"),
                                           capture_path_id=str(m.get("capture_path_id") or ""),
                                           capture_path_rev=int(m.get("capture_path_rev") or 0),
                                           capture_mode=norm_mode(m.get("capture_mode")))
                    s.state = "idle"
                    s.log(f"hello from {dev_serial}（{s.model} / {s.app_version}）")
                    s.write_manifest()
                print(f"[ws] hello from {dev_serial} @{peer}")
            elif t in ("start_ack", "stop_ack"):
                # **PC 主控的闭环回执**（§7.1）。原来 start/stop 是单向广播：
                # 面板只知道"WS 发送成功"，不知道"相机是否真的开起来了/真的停了"。
                # 相机打开失败（设备瞬时故障、被别的应用占用）时，界面会一直显示 running，
                # 而实际什么都没采到 —— 主控要成立，指令必须有回执。
                sid = str(m.get("session_id") or "")
                s = hub.session(sid)
                if not s:
                    print(f"[ws] {t} 未知会话 {sid}（忽略）")
                    continue
                if t == "start_ack":
                    ok = bool(m.get("ok"))
                    s.start_ack = {"ok": ok, "error": m.get("error"), "at": m.get("at_ms"),
                                   "output_size": m.get("output_size"), "format": m.get("format"),
                                   "plan_interval_ms": m.get("plan_interval_ms"),
                                   "zoom_requested": m.get("zoom_requested"), "server_at": now_iso()}
                    s.state = "running" if ok else "start_failed"
                    s.log(f"start_ack ok={ok}" + ("" if ok else f" error={m.get('error')}")
                          + f" output={m.get('output_size')} zoom={m.get('zoom_requested')}")
                    print(f"[ws] start_ack ok={ok} session={sid}"
                          + ("" if ok else f"  ✗ {m.get('error')}"))
                else:
                    s.stop_ack = {"running": m.get("running"), "frames_delivered": m.get("frames_delivered"),
                                  "queue_len": m.get("queue_len"),
                                  "meta_missing_result": m.get("meta_missing_result"),
                                  "pending_frames": m.get("pending_frames"),
                                  "will_drain": m.get("will_drain"),
                                  "server_at": now_iso()}
                    if s.state not in ("stopped", "gate_failed"):
                        s.state = "stopped"
                    if not s.stopped_at:
                        s.stopped_at = now_iso()
                    # `will_drain` 是 App 的**承诺**："我不再采了，但队列传完再收工"。
                    # 这句是收尾判定的依据 —— 少了它，面板就分不清
                    # "停了=结束了" 与 "停了但还在传"。
                    s.log(f"stop_ack：已抽帧 {m.get('frames_delivered')}，队列剩 {m.get('queue_len')}"
                          + ("；App 承诺继续补传（停采集不停交付）" if m.get("will_drain") else ""))
                    print(f"[ws] stop_ack session={sid} 队列剩 {m.get('queue_len')}"
                          f" 待补传 {m.get('pending_frames')}")
                    hub.broadcast_panel({"type": "stop_ack", "session_id": sid,
                                               "info": s.stop_ack})
                    hub.broadcast_event({"type": "stopped", "session_id": sid,
                                         "stopped_at": s.stopped_at,
                                         "frames_delivered": m.get("frames_delivered"),
                                         "queue_len": m.get("queue_len"),
                                         "will_drain": bool(m.get("will_drain"))})
                s.write_manifest()
            elif t == "session_begin":
                # **控制道显式声明"我要开始采这个会话了"**（§7.1 新增）。
                #
                # 为什么必须有这条：监视道（缩略图）走 WS，交付道（帧）走 HTTP，
                # 两者的相对时序**不可控** —— 实测踩到：连接后 App 先把**旧会话**的积压帧
                # 补传完（那些帧的 verified 只证明"旧会话存在"），而当前会话在 PC 端
                # 要等它自己的首帧 init 才建立 ⇒ 这中间的缩略图全被"未知会话"拒掉。
                # 让监视道去猜交付道的进度是错的；正确的做法是**控制道自己声明**：
                # 谁要开始采集，谁就在控制道上说一句，PC 立刻建会话并回 ack。
                sid = str(m.get("session_id") or "")
                if not sid:
                    await ws.send_str(json.dumps({"type": "error", "code": "bad_session_id",
                                                  "message": "session_begin 必须带 session_id"},
                                                 ensure_ascii=False))
                    continue
                try:
                    s = hub.create_session(
                        sid, dev_serial,
                        model=str(m.get("model") or ""),
                        app_version=str(m.get("app_version") or ""),
                        calib_id=str(m.get("calib_id") or ""),
                        delivery=str(m.get("delivery") or "stream"),
                        capture_path_id=str(m.get("capture_path_id") or ""),
                        capture_path_rev=int(m.get("capture_path_rev") or 0),
                        capture_mode=norm_mode(m.get("capture_mode")))
                except ValueError as e:
                    await ws.send_str(json.dumps({"type": "error", "code": "bad_session_id",
                                                  "message": str(e)}, ensure_ascii=False))
                    continue
                if str(m.get("capture_mode") or "") and \
                        norm_mode(m.get("capture_mode")) != str(m.get("capture_mode")).strip().lower():
                    s.log(f"⚠ App 报的 capture_mode={m.get('capture_mode')!r} 非法 ⇒ 按 deliver 处理")
                if s.state in ("armed", "running"):
                    pass                     # 幂等：重复声明不改状态
                else:
                    s.state = "armed"
                s.log(f"session_begin（App 本地开流；zoom={m.get('zoom_effective')} "
                      f"来源={m.get('zoom_source')}）⇒ 监视道可开始发送")
                s.write_manifest()
                print(f"[ws] session_begin {sid}（已建会话，监视道开闸）")
                await ws.send_str(json.dumps({"type": "session_ack", "session_id": sid},
                                             ensure_ascii=False))
            elif t == "status":
                sid = str(m.get("session_id") or "")
                s = hub.session(sid)
                if s:
                    s.last_status = {k: v for k, v in m.items() if k != "type"}
                    if s.state == "armed":
                        s.state = "running"
                    # 留存账本随 `status`（每 2 s）一直在更新 ⇒ 面板与 host/status
                    # **不必等采集结束**就能看到"应有多少 / 落盘多少 / 缺了多少"。
                    # 记 `store_decl_at` 是为了能回答"这份账本是不是过期的"。
                    st_store = m.get("store")
                    if isinstance(st_store, dict):
                        s.store_decl = st_store
                        s.store_decl_at = time.time()
                        # 上位机订阅一次就能看到进度，不必轮询（节流统一在 PC 侧做）。
                        # ⚠ `live` 模式下 store 是「不适用」：广播 `delivery_progress`
                        # 会让人以为"有一条留存道在慢慢攒帧"—— 而那条道根本不存在。
                        if st_store.get("applicable") is not False:
                            hub.broadcast_event(
                                {"type": "delivery_progress", "session_id": sid,
                                 "declared": st_store,
                                 "frames_verified_on_pc":
                                     len([k for k in s.frames if s.frames[k].verified])},
                                throttle_s=2.0)
                    # ---- 实时道（`capture_mode=live`）----
                    lv = m.get("live")
                    if isinstance(lv, dict):
                        s.live_decl = lv
                        s.live_decl_at = time.time()
                        # ★ 记下**设备实际生效**的颜色来源（第29 轮）。
                        #   为什么要与请求值分开：HAL 不支持 RGBA_8888 时设备会降级成
                        #   `yuv_bt601`，而会话里记着用户请求了 `rgba_hal` ⇒
                        #   **二分实验悄悄跑成了同一条路两次**，面板还一切正常。
                        eff = str(lv.get("color_source") or "")
                        if eff and eff != s.color_source_effective:
                            s.color_source_effective = eff
                            if eff != s.color_source:
                                s.log(f"⚠ 颜色来源被降级：请求 {s.color_source} ⇒ "
                                      f"设备实际 {eff}"
                                      f"（rgba_supported={lv.get('rgba_supported')}）"
                                      f"——**本次绿偏二分实验不成立**")
                        # live 会话的路径标识**只能**从 `status.live.path_id` 补进来：
                        # 它不发 `/api/v1/files/*`（没有 init 可以补路径），
                        # 而 manifest 里 `capture_path` 空着会让"这份数据是哪条路径产的"
                        # 无法回答 —— 登记表也就对不上了。
                        if lv.get("path_id") and not s.capture_path_id:
                            s.capture_path_id = str(lv["path_id"])
                            s.capture_path_rev = int(lv.get("path_rev") or 0)
                            s.log(f"路径标识由 status.live 补入："
                                  f"{s.capture_path_id}/{s.capture_path_rev}")
                    s.write_manifest()
                print(f"[ws] status {m}")
            elif t == "thumb":
                # 监视道（D6）：缩略图**可随时清理，不参与交付校验**。
                # 它存在的意义是"交付道堵住时仍能看到取景"，所以处理原则是
                # **绝不让它影响交付道**：解不开就记一笔、绝不抛；数量有上限。
                sid = str(m.get("session_id") or "")
                s = hub.session(sid)
                if s is None:
                    # **不建会话**（否则陈旧/伪造的缩略图会凭空造出会话目录），
                    # 但必须**回话一次**：App 收到 thumb_rejected 会自己关闸并记一笔，
                    # 而不是按 1 Hz 一直往这里灌（同时也避免控制台被刷屏）。
                    print(f"[ws] thumb 忽略：未知会话 {sid}（监视道不建会话）")
                    if sid not in unknown_thumb_notified:
                        unknown_thumb_notified.add(sid)
                        await ws.send_str(json.dumps(
                            {"type": "thumb_rejected", "session_id": sid,
                             "reason": "unknown_session",
                             "message": "PC 还不知道该会话（会话由 start 或 session_begin 建立）"},
                            ensure_ascii=False))
                elif not m.get("jpeg_b64"):
                    s.log("thumb 缺 jpeg_b64，已忽略")
                else:
                    try:
                        raw = base64.b64decode(m["jpeg_b64"])
                        seq = int(m.get("seq") or 0)
                        name = f"thumb_{seq:06d}.jpg"
                        (s.root / "thumbs" / name).write_bytes(raw)
                        # 元数据旁挂一份：监视道也要能回答"这张缩略图对应哪一帧"
                        (s.root / "thumbs" / f"thumb_{seq:06d}.json").write_text(
                            json.dumps({k: v for k, v in m.items() if k != "jpeg_b64"},
                                       ensure_ascii=False, indent=2), encoding="utf-8")
                        s.thumbs += 1
                        s.thumb_latest = {"name": name, "seq": seq, "bytes": len(raw),
                                          "sensor_timestamp_ns": m.get("sensor_timestamp_ns"),
                                          "meta": m.get("meta") or {}, "at": now_iso()}
                        s.prune_thumbs()
                        if s.thumbs % 10 == 1:      # 别刷屏：每 10 张记一次
                            s.log(f"thumb #{s.thumbs} {name} {len(raw)}B "
                                  f"(seq={seq}, ts={m.get('sensor_timestamp_ns')})")
                        s.write_manifest()
                        # 推给面板：**收到即渲染**。原来面板 3 s 轮询拉一张最新图，
                        # 而缩略图是 1 Hz ⇒ 用户看到"计数 +3、画面只更新 1 次"，
                        # 合理地怀疑丢了 2/3。其实一张没丢（`thumbs` 是逐张落盘数），
                        # 是**采样口径**骗了人。`panel_ws` 为空时不做无谓的编码开销。
                        if hub.panel_ws:
                            hub.broadcast_panel({
                                "type": "thumb", "session_id": sid, "seq": seq,
                                "bytes": len(raw), "at": s.thumb_latest["at"],
                                "sensor_timestamp_ns": m.get("sensor_timestamp_ns"),
                                "meta": m.get("meta") or {}, "jpeg_b64": m["jpeg_b64"]})
                        # 推给本机 MJPEG 订阅者（整体架构里的上位机）。
                        # 与面板推送同样必须**同步非阻塞**：这里仍在 App 的控制道
                        # 消息循环里，慢订阅者只能丢自己的帧。
                        if hub.mjpeg_subs:
                            hub.push_monitor(raw, seq, sid, m)
                    except Exception as e:
                        s.log(f"thumb 解码失败（监视道可丢，不影响交付）: {e}")
            elif t == "delivery_done":
                # **交付收尾宣告**（§7.1）：App 把队列里的帧全部传完、逐帧 verified 之后发这一条。
                #
                # 为什么必须有：`stop_ack` 只回答"停在哪"（抽了多少帧、队列剩几帧），
                # 回答不了"到底收全了没有"。而 PC 侧**没有任何可信办法**从"不再有新帧"
                # 推断出"传完了" —— 链路卡顿、App 被系统冻结、旧会话串台都能骗过这种推断，
                # 骗过的后果是"以为收全了"（判据强于事实）。收尾必须由 App **显式宣告**。
                sid = str(m.get("session_id") or "")
                s = hub.session(sid)
                if not s:
                    print(f"[ws] delivery_done 未知会话 {sid}（忽略，不建会话）")
                else:
                    s.delivery_done = {k: v for k, v in m.items() if k != "type"}
                    s.delivery_done["server_at"] = now_iso()
                    # 收尾宣告里带着**最终账本**（应有/实存/缺口/seq 区间）⇒ 用它落账，
                    # 于是账本不必依赖采集期那些每 2 s 的 `status`（最后几帧可能来不及上报）。
                    di = {k: v for k, v in m.items()
                          if k in ("frames_expected", "frames_stored_on_device",
                                   "gaps_on_device", "gap_ts", "seq_first", "seq_last",
                                   "warmup_frames_skipped", "result_missing")}
                    if di:
                        di["state"] = "done"
                        s.store_decl = {**(s.store_decl or {}), **di}
                        s.store_decl_at = time.time()
                    ai = s.delivery_info()
                    s.log(f"交付收尾：队列已清空，{m.get('frames')} 帧全部回传完成"
                          + (f"（收尾耗时 {m.get('drain_seconds')} s）"
                             if m.get("drain_seconds") is not None else ""))
                    s.log(f"留存账本：应有 {ai['frames_expected']} / 手机实存 "
                          f"{ai['frames_stored_on_device']} / PC 已校验 "
                          f"{ai['frames_verified_on_pc']}"
                          f"（**只能重采**的采集缺口 {ai['gaps_on_device']}，"
                          f"**可以重传**的传输缺口 {ai['upload_gaps']}）"
                          f"complete={ai['complete']}")
                    print(f"[ws] delivery_done session={sid} frames={m.get('frames')} "
                          f"queue={m.get('queue_len')} complete={ai['complete']}")
                    s.write_manifest()
                    hub.broadcast_panel({"type": "delivery_done", "session_id": sid,
                                               "info": s.delivery_done})
                    hub.broadcast_event({"type": "delivery_done", "session_id": sid,
                                         "info": s.delivery_done,
                                         "ledger": {k: v for k, v in ai.items()
                                                    if k != "note"}})
            elif t == "delivery_aborted":
                # 留存熔断（典型：剩余空间不足）。**必须显式命名状态** ——
                # 静默停止会让人以为"还在录"，而"以为还在录"的代价是**整段数据作废**。
                sid = str(m.get("session_id") or "")
                s = hub.session(sid)
                if not s:
                    print(f"[ws] delivery_aborted 未知会话 {sid}（忽略，不建会话）")
                else:
                    decl = {k: v for k, v in m.items() if k != "type"}
                    decl["state"] = ("aborted_no_space" if m.get("reason") == "no_space"
                                     else "aborted")
                    # ⚠ 键名归一（**在这里归一次，别让读侧各猜各的**）：App 发的是 `reason`，
                    # 而账本对外统一叫 `aborted_reason`。两处各写一份映射的话，
                    # 将来任一侧改名 ⇒ 熔断原因**静默变 None**（界面只会显示"没在跑"）。
                    decl["aborted_reason"] = m.get("reason")
                    decl["server_at"] = now_iso()
                    # **合并**而不是覆盖：熔断宣告里没有 seq 区间等字段，覆盖会把它们弄丢。
                    s.store_decl = {**(s.store_decl or {}), **decl}
                    s.store_decl_at = time.time()
                    s.log(f"⚠ 留存中止：{m.get('reason')} —— {m.get('message') or ''}"
                          f"（已存下的帧仍会回传完）")
                    print(f"[ws] delivery_aborted session={sid} reason={m.get('reason')}")
                    s.write_manifest()
                    hub.broadcast_event({"type": "delivery_aborted", "session_id": sid,
                                         "reason": m.get("reason"), "declared": decl})
            elif t == "error":
                sid = str(m.get("session_id") or "")
                s = hub.session(sid)
                if s:
                    s.log(f"[App error] {m.get('code')}: {m.get('message')}")
                print(f"[ws] App error {m.get('code')}: {m.get('message')}")
            else:
                await ws.send_str(json.dumps({"type": "error", "code": "unknown_type",
                                              "message": t}, ensure_ascii=False))
    finally:
        hub.ws.discard(ws)
        hub.broadcast_event({"type": "device_offline", "serial": dev_serial, "peer": peer,
                             "clients": len(hub.ws), "at": now_iso()})
        print(f"[ws] disconnected {dev_serial or peer}")
    return ws


async def h_panel_ws(req: web.Request) -> web.WebSocketResponse:
    """面板（浏览器）的推送通道：缩略图 / 指令回执 / 交付收尾。

    **为什么与 App 的 `/ws` 分开**：App 那条是**控制道** —— 它断开意味着设备掉线，
    要触发收尾状态机；面板这条断了只是"某个浏览器标签没在看"。混在一条里，
    "刷新页面"就会被记成"设备掉线"。两者的鉴权相同（都用 token），但语义必须分开。

    面板仍是**只读**的（所有动作走 HTTP `POST /api/v1/control/*`），
    所以这里收到什么都不处理 —— 但必须把消息**消费掉**，否则关闭/心跳帧不被处理。
    """
    hub: Hub = req.app["hub"]
    if req.query.get("token") != hub.token:
        return web.json_response({"error": "bad_token",
                                  "message": "token 不符（token 即轻量鉴权）"}, status=403)
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(req)
    # **每连接一个 2 格队列 + 独立发送任务**（见 broadcast_panel 的说明）：
    # 投递方（控制道循环）只做非阻塞的 `put_nowait`，慢面板只丢自己的帧。
    q: asyncio.Queue = asyncio.Queue(maxsize=2)

    async def _pump() -> None:
        try:
            while True:
                await ws.send_str(await q.get())
        except asyncio.CancelledError:
            raise
        except Exception:
            pass                      # 对端没了：连接自身的 async for 会退出并清理

    pump = asyncio.create_task(_pump())
    hub.panel_ws[ws] = q
    print(f"[panel] 推送通道已连接（共 {len(hub.panel_ws)} 个）")
    try:
        async for _msg in ws:
            continue
    finally:
        pump.cancel()
        hub.panel_ws.pop(ws, None)
        print(f"[panel] 推送通道断开（剩 {len(hub.panel_ws)} 个）")
    return ws


# --------------------------------------------------------------------------- 面板

PANEL_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>相机交付面板</title>
<style>
 body{font:14px/1.5 system-ui,"Segoe UI",sans-serif;margin:24px;background:#0f1115;color:#e6e6e6}
 h1{font-size:18px;margin:0 0 4px} .sub{color:#8b93a1;font-size:12px;margin-bottom:16px}
 table{border-collapse:collapse;width:100%;margin:8px 0 20px}
 th,td{border-bottom:1px solid #262b33;padding:6px 8px;text-align:left;font-size:13px}
 th{color:#8b93a1;font-weight:500}
 .ok{color:#4ade80}.warn{color:#fbbf24}.bad{color:#f87171}
 button{background:#2563eb;border:0;color:#fff;padding:8px 14px;border-radius:6px;cursor:pointer}
 button.sec{background:#374151} button:disabled{opacity:.5;cursor:not-allowed}
 .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:10px 0}
 code{background:#1a1f27;padding:2px 6px;border-radius:4px}
 .box{background:#151a21;border:1px solid #262b33;border-radius:8px;padding:12px;margin-bottom:16px}
</style></head><body>
<h1>相机交付面板</h1>
<div class="sub">token <code>__TOKEN__</code> ｜ 数据目录见「环境」 ｜ 3 秒自动刷新
 ｜ 三条道：监视道（<b>灰度</b>缩略图）+ 交付道（每帧原始 Y，不送画面）+ 实时道（<b>彩色</b>，只看不留）</div>

<div class="box">
  <div class="row">
    <button onclick="ctl('start')">▶ 开始采集（PC→App start）</button>
    <button class="sec" onclick="ctl('stop')">■ 停止</button>
    <span class="sub">采集模式</span>
    <!-- ⚠ 这两个 option 的取值必须与 server.py 的 `MODES` 一致，且**判据盯着**这一致性
         （A9m 从 `MODES` 自身推导）。加第三种模式而忘了这里 ⇒ 判据立刻红，
         而不是现场点了按钮才发现"模式没生效"。 -->
    <select id="capmode" title="capture_mode：决定这条会话是「每一帧都留」还是「只看不留」"
            style="background:#1a1f27;color:#e6e6e6;border:1px solid #262b33;border-radius:6px;padding:6px">
      <option value="deliver">deliver — 每一帧都留（灰度原始 Y，先落手机本地）</option>
      <option value="live">live — 只看不留（彩色 JPEG，串行闭环，零留存）</option>
    </select>
    <!-- ⚠ 第 29 轮：颜色来源。同样**必须与 `COLOR_SOURCES` 一致且被判据盯着** ——
         否则会重演"面板加了控件、接口没加/取值不一致"这类静默缺陷
         （第 12 条铁律：界面能做的 ≠ 接口能做的）。
         ⚠ 它**只在 live 下有意义**：交付道要的是原始 Y 平面，RGBA reader 没有 Y 平面
         ⇒ 选 live + 非 yuv_bt601 才会真正下发；deliver 下服务端会 400。 -->
    <select id="colsrc" title="color_source：颜色由谁算。只在 capture_mode=live 下有意义。"
            style="background:#1a1f27;color:#e6e6e6;border:1px solid #262b33;border-radius:6px;padding:6px">
      <option value="yuv_bt601">yuv_bt601 — 本进程按 BT.601 自己换算（现状）</option>
      <option value="rgba_hal">rgba_hal — 相机 HAL 换算（绿偏归属的二分实验）</option>
    </select>
    <span id="hint" class="sub"></span>
  </div>
  <div class="sub">⚠ <b>颜色来源是「绿偏归属」的判决性实验</b>：现场画面偏绿，而同场景的
    <b>相机原生照片正常</b>。<code>rgba_hal</code> 档让<b>相机 HAL</b> 做 YUV→RGB
    （厂商 ISP 那套色彩管线），本进程只下采样+编码。
    <b>若 <code>rgba_hal</code> 也偏绿</b> ⇒ 本进程的 BT.601 换算是清白的（问题在 HAL 或显示链路）；
    <b>若只有 <code>yuv_bt601</code> 偏绿</b> ⇒ 问题在我们的换算里。
    两条路必有一条偏，<b>责任归属即唯一确定</b>。
    <br>⚠ 若设备不支持 <code>RGBA_8888</code>，App 会<b>降级</b>成 <code>yuv_bt601</code>
    并在会话日志与下方「实时道」里显式标出——那时<b>本次二分不成立</b>
    （两条路退化成同一条），结论请按<b>未验证</b>对待。</div>
  <div class="sub">⚠ <b>模式是会话性质，不是画质档</b>：<code>live</code> 下<b>交付道整体关闭</b>
    （不落盘、不起 writer、不排空），跑完<b>什么都不剩</b>（设计如此，误用不可恢复）；
    而 <code>deliver</code> 下实时道不工作。两种模式<b>互斥</b>，由服务端判。
    <br>start 会新建一个 session；App 收到后按 <code>plan.interval_ms</code> 抽帧交付。
    <b>指令是闭环的</b>：App 开流成功/失败会回 <code>start_ack</code>（失败原因直接显示在「状态」列），
    停止会回 <code>stop_ack</code>（含"已抽帧多少、队列还剩多少"）。
    另：<b>开流门控</b>会把 App 的<b>回读值</b>与本 profile 逐项比对，硬项不一致（如 zoom 未被采纳）
    会<b>自动停流</b>并把会话标成 <code>gate_failed</code> —— 免得采出一批"配置前提是错的"数据。</div>
</div>

<div class="box">
  <div class="sub">扫码配对（手机 App →「扫码配对」按钮）
    ｜ <b>这个码里含 token，等同凭据</b>：别投屏、别外传</div>
  <div class="row">
    <div id="qrbox" style="background:#fff;padding:8px;border-radius:6px;width:216px;height:216px;
         display:flex;align-items:center;justify-content:center;flex:0 0 auto">
      <span class="sub" style="color:#333">加载中…</span></div>
    <div class="sub" style="min-width:280px">
      配对码 <code id="pairuri" style="word-break:break-all">—</code><br>
      地址 <code id="pairaddr">—</code> ｜ token <code id="pairtoken">—</code><br>
      <span id="pairnote"></span>
    </div>
  </div>
</div>

<div class="box">
  <div class="sub"><b>监视道</b>（<b>灰度</b>缩略图，走 WS；<b>与交付道独立</b> —— 交付积压时这里仍刷新）</div>
  <div class="sub" style="margin-bottom:6px">
    通道 <span id="thumbchan" class="warn">连接中…</span> ｜
    收到 <b id="thumbrx">0</b> 张 ｜ 渲染 <b id="thumbdrawn">0</b> 张
    <span id="thumbcoal"></span> ｜ 渲染帧率 <b id="thumbfps">0.0</b>/s（App 侧 1.0/s）
    ｜ 来源 <span id="thumplane" class="sub">—</span>
  </div>
  <div class="row">
    <img id="thumb" alt="还没有缩略图（App 未连接 / 未开流）"
         style="width:360px;height:270px;object-fit:contain;background:#0b0d11;
                border:1px solid #262b33;border-radius:6px">
    <div class="sub" id="thumbinfo" style="min-width:240px">—</div>
  </div>
  <div class="sub" id="thumbsync"></div>
</div>

<div class="box">
  <div class="sub"><b>实时道</b>（<b>彩色</b>，`capture_mode=live` 才有内容）——
    <b>只看不留</b>：设备不落盘、PC 也不落盘（这个会话连 <code>thumbs/</code> 都没有）。
    走 <code>/stream/mjpeg</code> 同一条管线，但帧带 <code>lane=live</code> / <code>color=true</code></div>
  <div class="sub" style="margin-bottom:6px" id="livehead">—</div>
  <div class="row">
    <img id="liveimg" alt="（实时道未连接 —— 只有当会话是 live 模式时这里才有画面）"
         style="width:360px;height:270px;object-fit:contain;background:#0b0d11;
                border:1px solid #262b33;border-radius:6px">
    <div class="sub" id="liveinfo" style="min-width:260px">—</div>
  </div>
  <div class="sub" id="livesync"></div>
  <div class="sub">⚠ 这条流需 token 且<b>仅本机可读</b>（<code>/stream/*</code> 在 loopback 名单里）——
    所以<b>用手机浏览器打开面板时它不会显示</b>（那时看上面的监视道即可）；
    本机 <code>cv2.VideoCapture</code> / <code>ffmpeg</code> 读它是没问题的。</div>
</div>

<div class="box">
  <div class="sub"><b>给本机其它程序用的接口</b>（只接受来自本机的请求）—— 供整体架构里的上位机 / 分析程序接入</div>
  <div class="sub" style="margin:6px 0">
    先读 <code>GET /api/v1/capabilities</code>（无需 token）拿清"有哪些接口、各条道是什么"，
    再 <code>GET /api/v1/host/status</code>（需 token）读整机状态。
    <br>实时画面 <code>GET /stream/mjpeg</code>（MJPEG，<b>可丢</b>，只用于"看得见"、不得用于测量）；
    事件广播 <code>ws /api/v1/host/events</code>（连接即得一份当前状态）。
    <br>Python 三行接入：<code>from hostlink import HostLink; h = HostLink("http://127.0.0.1:8765", token="…"); print(h.status())</code>
    <br>⚠ 上面这行是**真参数**（第一个位置参数是 URL，token 是关键字）—— 写成 <code>HostLink(port, token)</code>
    会直接报错，而"文档里的示例没人跑"是本项目吃过一次的亏（判据 P10 现在盯着 README 里的每个示例）。
    <br><b>这些接口按"对端地址是否为回环"判本机</b>（不认可伪造的 <code>Host</code> 头）；
    拿不到对端地址就<b>判不本机</b>（fail closed）—— 设备侧接口必须监听 <code>0.0.0.0</code>，所以不能靠端口区分。
  </div>
  <div class="sub"><b>交付道 = 每一帧都留</b>（第 24 轮合并：不再分"交付道 / 归档道"）：相机吐出的<b>每一帧</b>原始 Y 平面（不抽帧、不压缩）在采集期先落<b>手机本地</b>，同时边采边传；传不完的停采后排空。
    为什么必须先落本地：单帧 1920×1440 纯 Y ≈ 2.76 MB，30 fps ⇒ <b>83 MB/s</b>，而手机→PC 实测链路只有 <b>5.5 MB/s</b>（差 <b>15.1 倍</b>）—— 实时传完每一帧在物理上不可能。
    <br>⚠ 全帧率落盘很吃空间：手机 21 GB 可用空间约 <b>4.2 分钟</b>就写满；写满会连带拖死 App 与回传 ⇒ 有配额与<b>熔断</b>（<code>aborted_no_space</code>，显式命名，不许静默停止）。
    <br>「留存」列里两个缺口<b>必须分开看</b>：<b>机内缺口</b>（<code>gaps_on_device</code>：采集期没写进本地）只能<b>重采</b>；<b>传输缺口</b>（<code>upload_gaps</code>：写进去了但没传到 PC）可以<b>重传</b>。</div>
</div>

<div class="box" id="env"></div>

<table><thead><tr><th>会话</th><th>设备</th><th>App</th><th>路径</th><th>镜头/变焦</th><th>状态</th>
<th>帧（已验证/声明）</th><th>字节</th><th>缩略图</th><th>留存（每一帧）</th><th>断言</th></tr></thead>
<tbody id="rows"><tr><td colspan="11" class="sub">—</td></tr></tbody></table>

<div class="box"><div class="sub">最近事件（服务端日志见 sessions/&lt;id&gt;/logs/upload.log）</div>
<pre id="log" style="margin:6px 0 0;white-space:pre-wrap">—</pre></div>

<script>
// ---------------------------------------------------------------------------
// 实时道账本的**统一渲染**（会话表与实时道区块共用一份，避免两处口径分叉）
//
// 关键纪律：`applicable=false`（不适用）必须与 `0`（收不到）分开画。
// live 模式下没有"留存"这个机制，服务端判 `delivery_complete=null` + note「不适用」；
// 前端若把 null 一律当成"判不了"、把 0 当成"没收到"，就会显示成**永远失败的进度条**，
// 而其实没有问题 —— 是问题问错了（本项目铁律：不适用 ≠ 判不了 ≠ 0）。
// ---------------------------------------------------------------------------
function fmtMB(n){ return (n===null||n===undefined)? '—' : (n/1048576).toFixed(2)+' MB'; }
function fmtNum(v, d){ return (v===null||v===undefined)? '—' : (d===undefined? v : Number(v).toFixed(d)); }
function liveCell(LG){
  if (!LG || LG.applicable !== true){
    return '<span class="sub">不适用</span> <span class="sub">（本会话不是 live 模式 —— 留存机制在跑，见「留存」列）</span>';
  }
  const D = LG.declared || {};
  let h = 'PC 实收 <b>'+LG.frames_received+'</b> 帧 ｜ '+fmtMB(LG.bytes_received)
        + ' ｜ <b>'+fmtNum(LG.mbps_received, 2)+'</b> MB/s ｜ <b>'+fmtNum(LG.fps_actual, 1)+'</b> fps';
  h += '<br>App 宣告：发送 '+fmtNum(D.sent)+' ｜ 跳过 <b>'+fmtNum(D.skipped_est)
     + '</b>（陈帧·设计内）｜ 异常丢 '+fmtNum(D.dropped)
     + (D.latency_ms!=null? ' ｜ 延迟 '+fmtNum(D.latency_ms)+' ms':'');
  if (LG.rejected) h += ' <span class="warn">拒收 '+LG.rejected+'</span>';
  if (LG.mode_conflicts) h += ' <span class="bad">⚠ 模式冲突 '+LG.mode_conflicts+'</span>';
  // 两条独立证据摆在一起：PC 自己数的 vs App 宣告的。差得多说明"App 想发但发不出"。
  if (D.sent!=null && D.sent !== LG.frames_received){
    h += '<br><span class="warn">两条证据不等：App 说发送 '+D.sent+'、PC 实收 '
       + LG.frames_received + '（差额通常是链路/超时，不是"没发"）</span>';
  }
  return h;
}

async function refresh(){
  loadPair();
  try{
    const r = await fetch('/api/v1/sessions'); const j = await r.json();
    document.getElementById('env').innerHTML =
      '<span class="sub">WS 客户端 <b>' + j.ws_clients + '</b> 个 ｜ profile <code>' +
      (j.profile_id||'-') + '</code> = <code>' + JSON.stringify(j.profile) + '</code> ｜ 数据目录 <code>' +
      j.data_dir + '</code></span>';
    const rows = j.sessions.map(s=>{
      const calib = s.calib_consistent===null?'<span class="warn">判不了</span>':
                    (s.calib_consistent?'<span class="ok">一致</span>':'<span class="bad">不一致</span>');
      const drift = (s.drift&&s.drift.length)? ('<span class="bad">漂移 '+s.drift.map(d=>d.field).join(',')+'</span>')
                    : '<span class="ok">无</span>';
      const st = s.frames_partial>0? '<span class="warn">未完成</span>':'<span class="ok">完整</span>';
      // 采集模式：`live`（只看不留）必须**一眼可见** —— 它跑完什么都没留下（R38），
      // 误用的后果不可恢复。所以给一个显式徽标，而不是靠"帧数 0"去暗示
      //（"帧数 0"与"采集失败"在界面上长得一模一样，那是观测口径骗人）。
      //
      // ⚠ `isLive` / `LG` 必须在**这里**声明：本行的 `const` 是块级作用域 + TDZ，
      // 谁在上面用到它们就会抛 `ReferenceError: Cannot access 'isLive' before initialization`。
      // 实测代价（第 28 轮）：`gateCell` 用到了 `isLive`，而声明写在它下面 ⇒
      // **整个 `refresh()` 抛异常 ⇒ 会话表一行都不渲染**（表格是空的，页面其它部分照常）。
      // ⚠⚠ 这个错**三层检查全都抓不到**：Python `ast.parse` 过、`node --check` 过
      //（语法完全合法）、判据 A9m 也过（它只检字符串在不在）。**只有真的把页面渲染一遍**
      //（headless 浏览器抓 `hint` 里的异常）才会现形。
      // ⇒ 教训：面板这类"字符串里的代码"，静态检查只能证"存在"，不能证"能跑"。
      const isLive = (s.capture_mode === 'live');
      const LG = s.live || {};
      const modeBadge = isLive
        ? '<span class="bad">LIVE 无留存</span><br>' : '';
      // 镜头/变焦：换档=换镜头=换内参，所以这一列要能一眼看出"是不是同镜、请求生效没有"
      const L = s.lens || {};
      const ids = (L.active_physical_id_distinct||[]).join('→') || '—';
      const zoom = (L.zoom_readback_distinct||[]).join('/') || '—';
      const lensCell = '<code>id '+ids+'</code><br><span class="sub">zoom '+zoom+
        ((L.active_physical_id_unique===false)? ' <span class="bad">换过镜</span>':'') +
        ((L.zoom_match===false)? ' <span class="bad">zoom 未生效</span>':'') + '</span>';
      // PC 主控的闭环：start_ack 证明"相机真的开了"，stop_ack 说明"停在哪"
      const A = s.start_ack;
      const ackCell = (A? (A.ok? '<span class="ok">相机已开流</span>'
                                 : '<span class="bad">启动失败</span> <span class="sub">'+A.error+'</span>')
                        : '<span class="sub">未收到 start_ack</span>')
        + (s.stop_ack? '<br><span class="sub">已停：抽帧 '+s.stop_ack.frames_delivered
                        +'，队列剩 '+s.stop_ack.queue_len+'</span>' : '');
      // 门控：profile 是"意图"，回读是"事实"，两者不一致时数据照样在流但配置前提已错
      const G = s.start_gate || {};
      // 三态必须分开显示：fail = 配置真的没生效（已自动停流）；incomplete = 回读缺席、**判不了**
      // （没停流，等后续帧补齐）；pass = 已知的硬项全对得上。
      // ⚠ live 模式下必然是 `incomplete` —— 因为门控的**证据来源是交付帧的逐帧回读**，
      // 而 live 一条交付帧都没有。这时"判不了"是**诚实的**（取证不足），**不是**"不适用"
      // （问题本身是适用的：锁定配置有没有生效，对 live 一样有意义）。
      // 但光显示"判不了"会被读成"出问题了"⇒ 必须把**为什么没有证据**说出来。
      const nz = Object.keys(G.checks||{}).filter(k=>G.checks[k] && G.checks[k].normalized).length;
      const gateCell = (G.status==='pass'? '<span class="ok">门控 pass</span>'
                        : G.status==='fail'? '<span class="bad">门控 fail '+((G.hard_fail||[]).join(','))+'</span>'
                        : '<span class="warn">门控 判不了'+(((G.unevaluable||[]).length)? ' '+G.unevaluable.join(','):'')+'</span>')
        + ((isLive && G.status!=='pass' && G.status!=='fail')
             ? '<br><span class="sub">原因：live 不产交付帧 ⇒ 无逐帧回读来源（不是配置有问题）</span>' : '')
        + ((G.status!=='fail' && (G.soft_notes&&G.soft_notes.length))?
             '<br><span class="sub">'+G.soft_notes.length+' 条注记' + (nz? '（含 '+nz+' 项写法归一）':'') + '</span>' : '')
        + ((G.status&&G.evidence)? '<br><span class="sub">证据 '+((G.evidence.match(/seq=([0-9]+)/)||[])[1]||'-')+'</span>' : '');
      // 溯源字段**空 ≠ 一致**：空就显式写"未声明"。否则一行空白的 device 看起来像"正常"，
      // 而实际上"这份证据是哪台机器的哪个构建采的"是不可回答的（第 20 轮实测踩到）。
      const cell = (v,miss)=>(v? v : '<span class="warn">'+miss+'</span>');
      const DC = s.declared_conflicts;
      // 交付收尾三态：它回答的是**另一个问题** —— `stop_ack` 说"停在哪"，
      // 这条说"队列收全了没有"。null 必须显式写成"判不了"，
      // 否则单元格空着会被读成"没问题"（本项目铁律：判不了 ≠ 通过）。
      // ⚠ 但还有**第四种**必须分家的：**不适用** —— live 会话里"留存"这个机制根本不存在。
      // 服务端在 live 下判 `null` + 一句含「不适用」的 note。前端若把 null 一律画成
      // "判不了"，这一格就变成一个**永远失败的进度条**（而问题其实问错了：不是没收全，
      // 是本模式没有"收"这件事）。三态 → 四态，判据 A9d 两处都盯着。
      const dc = s.delivery_complete;
      const lateN = s.late_frames_after_stop || 0;
      const dcCell = '<br>' + (isLive
            ? '<span class="sub">交付收尾 <b>不适用</b>（live 零留存）</span>'
            : (dc===true? '<span class="ok">交付收尾 ✓</span>'
             : dc===false? '<span class="warn">交付未收完</span>'
             : '<span class="sub">交付收尾 判不了</span>'))
        + (lateN? ' <span class="sub">停采集后仍到 '+lateN+' 帧（交付道独立于采集，属预期）</span>' : '')
        + ((!isLive && s.delivery_complete_note)? '<br><span class="sub">'+s.delivery_complete_note+'</span>':'');
      // 留存：**每一帧**都留（先落手机本地，边采边传、停采后排空）。
      // ⚠ 两类计数来源不同、**绝不能合并**：
      //   · App 宣告 —— frames_expected / frames_stored_on_device / gaps_on_device；
      //   · PC 自数 —— frames_verified_on_pc / upload_gaps / missing_seqs（按 seq 点）。
      // `gaps_on_device`（采集期没写进去）**只能重采**；`upload_gaps`（没传到）**可以重传**。
      // `aborted_no_space` 是配额熔断（写满会连带拖死 App 与回传），必须显式呈现 ——
      // 不许让"留存停了"看起来像"没开留存"。
      // ⚠ **live 会话必须走另一个分支**：它 `applicable=false`，各计数是 `null`。
      // 若沿用下面那套渲染，会画出"应有 判不了 ｜ 机内存 判不了 ｜ 缺口 判不了"
      // —— 三处"判不了"把"本模式没有留存机制"说成了"取证不足"，是同一类错误的第三种形态。
      const AR = s.store || {};
      if (AR.applicable === false){
        return '<tr><td><code>'+s.session_id+'</code></td><td>'+cell(s.device_model,'型号未声明')+'<br><span class="sub">'+
          (s.device_serial||'-')+'</span></td><td>'+cell(s.app_version,'版本未声明')+'</td><td>'+
          (s.capture_path_id? '<code>'+s.capture_path_id+'/'+s.capture_path_rev+'</code>'
                             : '<span class="warn">路径未声明</span>')+'<br><span class="sub">'+
          s.delivery+'</span></td><td>'+lensCell+'</td><td>'+modeBadge+s.state+'<br>'+ackCell+'</td><td>'+
          '<span class="sub">—（不适用）</span></td><td><span class="sub">—</span></td><td>'+
          '<span class="sub">—（live 不落 thumbs）</span>'+
          '</td><td><span class="warn">不适用</span> <span class="sub">live 零留存</span><br>'+
          liveCell(LG)+'</td><td>'+gateCell+'<br>calib '+calib+'<br>'+drift+dcCell+
          (DC? '<br><span class="bad">溯源冲突 '+DC.length+'</span>':'')+'</td></tr>';
      }
      const arSt = {none:'<span class="sub">未开始</span>',
                     idle:'<span class="sub">未开始</span>',
                     recording:'<span class="ok">留存中</span>',
                     uploading:'<span class="ok">回传中</span>',
                     done:'<span class="ok">回传完成</span>',
                     aborted_no_space:'<span class="bad">空间不足熔断</span>'}[AR.state]
                 || ('<span class="sub">'+(AR.state||'-')+'</span>');
      const num = v=>(v===null||v===undefined)? '<span class="warn">判不了</span>' : v;
      const comp = AR.complete===true? '<span class="ok">账本自洽 ✓</span>'
                 : AR.complete===false? '<span class="bad">账本对不上</span>'
                 : '<span class="warn">判不了</span>';
      const gapCell = AR.missing_seqs_note
        ? '<br><span class="warn">'+AR.missing_seqs_note+'</span>'
        : (((AR.missing_seqs||[]).length)
            ? '<br><span class="bad">点出 '+AR.missing_seqs.length+' 个缺口帧号</span>'
            : '');
      const arCell = arSt
        + (AR.every_frame===false? ' <span class="warn">降载(抽帧)</span>' : '')
        + '<br><span class="sub">应有 '+num(AR.frames_expected)+' ｜ 机内存 '+num(AR.frames_stored_on_device)
        + ' ｜ PC 收 '+AR.frames_verified_on_pc+'</span>'
        + '<br><span class="sub">缺口：机内 <b>'+num(AR.gaps_on_device)+'</b>（重采）｜ 传输 <b>'+num(AR.upload_gaps)+'</b>（重传）</span>'
        + '<br>'+comp
        + ((AR.bytes_verified)? ' <span class="sub">'+(AR.bytes_verified/1048576).toFixed(1)+' MB</span>':'')
        + gapCell;
      return '<tr><td><code>'+s.session_id+'</code></td><td>'+cell(s.device_model,'型号未声明')+'<br><span class="sub">'+
        (s.device_serial||'-')+'</span></td><td>'+cell(s.app_version,'版本未声明')+'</td><td>'+
        (s.capture_path_id? '<code>'+s.capture_path_id+'/'+s.capture_path_rev+'</code>'
                           : '<span class="warn">路径未声明</span>')+'<br><span class="sub">'+
        s.delivery+'</span></td><td>'+lensCell+'</td><td>'+modeBadge+s.state+'<br>'+ackCell+'</td><td>'+s.frames_verified+'/'+s.frames_total+
        '</td><td>'+(s.bytes_verified/1048576).toFixed(2)+' MB</td><td>收到 '+s.thumbs+' 张'+
        '</td><td>'+arCell+'</td><td>'+gateCell+'<br>calib '+calib+'<br>'+drift+dcCell+
        (DC? '<br><span class="bad">溯源冲突 '+DC.length+'</span>':'')+'</td></tr>';
    });
    document.getElementById('rows').innerHTML = rows.length? rows.join('') :
      '<tr><td colspan="11" class="sub">还没有会话 — 点「开始采集」</td></tr>';
    // 监视道：推送通道在时**不要再动 img**（两路同时写 src 会互相顶替，
    // 把真实的"合并"数虚增 —— 那就又变成观测口径骗人了）。
    // 推送断线才退回"3 秒轮询拉一张"，并在通道状态里写明"轮询模式（帧率会被节流）"。
    if (j.sessions.length){
      const s0 = j.sessions[0];
      const tl = s0.thumb_latest;
      // 通道来源徽标：监视道送的是**灰度**（App 从 Y 平面现算，R=G=B），
      // 实时道送的是**彩色**。⚠ live 模式下 App 根本不再推 thumb —— 这一格必须写出来，
      // 否则"监视道不动了"会被读成"监视道坏了"，而其实是"本模式没有它"。
      document.getElementById('thumplane').innerHTML = (s0.capture_mode === 'live')
        ? '<b class="warn">live 模式下 App 不推 thumb</b>（监视道让位给实时道）'
        : '监视道 thumb（<b>灰度</b>）';
      if (tl){
        if (!panelWsLive())
          paintThumb('/api/v1/sessions/'+s0.session_id+'/thumb/latest?t='+Date.now(), false);
        document.getElementById('thumbinfo').innerHTML =
          '缩略图 <b>'+s0.thumbs+'</b> 张（只留最新 200）<br>最新：<code>'+tl.name+'</code><br>'+
          'seq '+tl.seq+' ｜ '+(tl.bytes/1024).toFixed(1)+' KB ｜ 帧时间戳 '+
          (tl.sensor_timestamp_ns||'-')+'<br>收到于 '+tl.at+
          (tl.meta&&tl.meta.state? ('<br>App 状态 '+tl.meta.state+' 队列 '+(tl.meta.queue_len??'-')):'') +
          // 档位来自 App 的 status（zoom_effective/zoom_source），生效镜头来自会话的 lens 汇总。
          // ⚠ 注意作用域：`L` 是上面 rows.map 里的局部变量，这里必须自己取一次。
          '<br>档位 zoom=' + (((s0.last_status||{}).zoom_effective) ?? '-') +
          '（' + (((s0.last_status||{}).zoom_source) ?? '-') + '）生效镜头 id ' +
          ((((s0.lens||{}).active_physical_id_distinct)||['-']).slice(-1)[0]);
      } else {
        document.getElementById('thumbinfo').textContent =
          (s0.capture_mode === 'live')
            ? '本会话是 live 模式 —— 监视道被有意关闭（App 不推 thumb），画面请看下面的「实时道」'
            : '该会话还没有缩略图 — App 需在线并开流（监视道只送画面，不影响交付）';
      }
    } else {
      const tp = document.getElementById('thumplane');
      if (tp) tp.textContent = '—';
    }
    // 实时道区块（`capture_mode=live` 才有内容）
    if (typeof paintLive === 'function') paintLive(j.sessions[0] || null);
    const logs = j.sessions.length? (j.sessions[0].last_status||{}) : {};
    document.getElementById('log').textContent = JSON.stringify(logs, null, 1);
  }catch(e){ document.getElementById('hint').textContent = '刷新失败: '+e; }
}

// ---------------------------------------------------------------------------
// 监视道：推送优先，轮询兜底
//
// 为什么要改：面板原来 3 s 轮询「拉一张最新图」，而 App 是 1 Hz 推缩略图
// ⇒ 用户看到"接收计数一下子 +3，画面只更新 1 张"，合理地怀疑丢了 2/3。
// **数据一张没丢**（`thumbs` 是逐张落盘数，PC 端每张都写了 thumbs/thumb_xxxxxx.jpg），
// 是渲染端的**采样口径**骗了人。所以这里做三件事：
//   ① 收到即渲染（帧率与 App 一致，不再是 1/3）；
//   ② "收到 / 渲染 / 合并"分列，差额显式解释 —— 不许让 3:1 这类比例无声出现；
//   ③ 推送断了退回轮询，并把"现在是轮询模式（帧率会被节流）"写在通道状态里。
// ---------------------------------------------------------------------------
const PANEL_TOKEN = '__TOKEN__';
let pws = null, pwsRetry = null;

// ---------------------------------------------------------------------------
// 扫码配对区块
//
// 二维码由**服务端**渲染成内联 SVG（`/api/v1/pair` 的 `qr_svg`）：
// 让前端自己画就得再引一个 JS 库，而本工程的约束是零前端依赖。
// 只拉一次（`pairLoaded`）：配对信息在服务进程生命周期内不变，
// 每 3 秒重画一遍 200×200 的 SVG 纯属烧 CPU。
// ---------------------------------------------------------------------------
let pairLoaded = false;
async function loadPair(){
  if (pairLoaded) return;
  try{
    const r = await fetch('/api/v1/pair?token=' + encodeURIComponent(PANEL_TOKEN));
    const j = await r.json();
    if (!j.ok) return;
    pairLoaded = true;
    const box = document.getElementById('qrbox');
    box.innerHTML = j.qr_svg ? j.qr_svg
      : '<span style="color:#b91c1c;font-size:12px;text-align:center">未安装 qrcode 库<br>' +
        '（pip install qrcode）<br>按右边地址 + token 手输也能连</span>';
    document.getElementById('pairuri').textContent = j.pair_uri;
    document.getElementById('pairaddr').textContent = j.host + ':' + j.port;
    document.getElementById('pairtoken').textContent = j.token;
    document.getElementById('pairnote').innerHTML = (j.note || '') +
      '<br>配对码格式：<code>m0probe://pair?h=&lt;ip&gt;&amp;p=&lt;port&gt;&amp;t=&lt;token&gt;</code>' +
      '<br>手输用的整行 URL：<code>' + j.url + '</code>';
  }catch(e){ /* 服务刚起来还没 ready 就下次刷新再试（不报错、不阻塞 refresh） */ }
}
let thumbRx = 0, thumbDrawn = 0, thumbCoal = 0, drawMarks = [];

function panelWsLive(){ return pws && pws.readyState === 1; }

function paintThumb(src, isPush){
  const img = document.getElementById('thumb');
  // 上一张的**解码还没完成**就被新图顶替 ⇒ 记一笔"合并"。
  // 监视道的语义是"看到现在"，所以在途的旧帧被丢弃是**设计**，不是缺陷；
  // 但它必须被计数，否则"收到 30 张只看见 28 张"永远只能靠猜。
  if (img.dataset.pending === '1') thumbCoal++;
  if (isPush) thumbRx++;
  img.dataset.pending = '1';
  img.onload = ()=>{ thumbDrawn++; drawMarks.push(Date.now()); img.dataset.pending='0'; updateThumbStat(); };
  img.onerror = ()=>{ img.dataset.pending = '0'; };
  img.src = src;
  updateThumbStat();
}

function updateThumbStat(){
  const now = Date.now();
  while (drawMarks.length && now - drawMarks[0] > 10000) drawMarks.shift();
  const el = (id)=>document.getElementById(id);
  if (!el('thumbrx')) return;
  el('thumbrx').textContent = thumbRx;
  el('thumbdrawn').textContent = thumbDrawn;
  el('thumbcoal').innerHTML = thumbCoal ? '｜ 合并 <b>'+thumbCoal+'</b> 张' : '';
  el('thumbfps').textContent = (drawMarks.length/10).toFixed(1);
  const gap = thumbRx - thumbDrawn - thumbCoal;
  el('thumbsync').innerHTML =
    '收到 '+thumbRx+' 张 / 渲染 '+thumbDrawn+' 张'+(thumbCoal? ' / 合并 '+thumbCoal+' 张':'')
    + (gap>0? '（还有 '+gap+' 张在解码路上）':'')
    + ' —— <b>差额不是丢帧</b>：每一张都已落盘到 <code>thumbs/</code>（会话目录里有对应 jpg+json），'
    + '监视道取"最新帧"语义，丢的只是**显示**；交付道走 HTTP，与这里完全无关。';
}

function setChan(live, why){
  const c = document.getElementById('thumbchan');
  if (!c) return;
  c.className = live? 'ok' : (why? 'bad' : 'warn');
  c.textContent = live? '推送（收到即渲染）' : (why? ('已断开：'+why+' → 轮询兜底') : '轮询模式（帧率会被节流）');
}

function connectPanelWs(){
  try{
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    pws = new WebSocket(proto + '://' + location.host + '/ws/panel?token=' + PANEL_TOKEN);
    pws.onopen  = ()=>{ setChan(true); if (pwsRetry) { clearInterval(pwsRetry); pwsRetry = null; } };
    pws.onerror = ()=>{};
    pws.onclose = ()=>{ setChan(false, '面板通道'); refresh();
                        if (!pwsRetry) pwsRetry = setInterval(()=>{ if(!panelWsLive()) connectPanelWs(); }, 3000); };
    pws.onmessage = (ev)=>{
      let m; try { m = JSON.parse(ev.data); } catch(e){ return; }
      if (m.type === 'thumb') paintThumb('data:image/jpeg;base64,' + m.jpeg_b64, true);
      else refresh();     // start_ack / stop_ack / delivery_done：立刻取一次快照
    };
  }catch(e){ setChan(false, '浏览器不支持 WS'); }
}

connectPanelWs();

// ---------------------------------------------------------------------------
// 实时道预览（`capture_mode=live`）
//
// 为什么用 `MJPEG <img>` 而不是复用面板那条 WS 推送：live 是**尽力发**（30 fps 量级），
// 走 WS 就得每帧 base64（**+33% 膨胀**）再在 JS 里解码；而 `<img src="/stream/mjpeg">`
// 是**浏览器原生**的 multipart 流 —— 零 JS 开销、自动丢旧帧，语义正好与"看到现在"一致
//（这与 App 侧"实时道走独立 HTTP 二进制"是同一条理由，只是换成了浏览器消费）。
//
// ⚠ 只在会话**确实是 live** 时才接上：挂着一个常开的 MJPEG 订阅会白占一个订阅位
//（`host/status` 里能看见），而且"没画面的黑框"会被误读成"链路坏了"。
// ---------------------------------------------------------------------------
let liveAttached = false;
function paintLive(s){
  const img  = document.getElementById('liveimg');
  const head = document.getElementById('livehead');
  const info = document.getElementById('liveinfo');
  const sync = document.getElementById('livesync');
  if (!img || !head) return;
  const isLive = !!(s && s.capture_mode === 'live');
  if (isLive && !liveAttached){
    img.src = '/stream/mjpeg?token=' + encodeURIComponent(PANEL_TOKEN) + '&t=' + Date.now();
    liveAttached = true;
  } else if (!isLive && liveAttached){
    img.removeAttribute('src');        // 断开这条 MJPEG 连接，恢复 alt 文案
    liveAttached = false;
  }
  if (!s){
    head.innerHTML = '<span class="sub">还没有会话 —— 点「开始采集」，模式选 <code>live</code></span>';
    info.textContent = '—'; if (sync) sync.textContent = '';
    return;
  }
  const LG = s.live || {};
  // ★ 第29 轮：**实际生效**的颜色来源必须显示出来，且降级时**显眼**。
  //   理由：二分实验的结论完全取决于"跑的是哪条路"。若设备不支持 RGBA_8888
  //   而被降级，画面照样出得来、照样能截图 —— **看起来完全正常**，
  //   但两条路是同一条，结论就是假的。所以这里不是"提示"，是**结论的前置条件**。
  const csEff = LG.color_source_effective, csReq = LG.color_source_requested;
  const csDegr = !!LG.color_source_degraded;
  const csLine = isLive
    ? (' <span class="' + (csDegr ? 'bad' : 'sub') + '" style="display:inline-block;'
       + 'border:1px solid ' + (csDegr ? '#a33' : '#262b33') + ';border-radius:4px;'
       + 'padding:1px 6px;margin-top:4px">颜色来源：<code>'
       + (csEff || '未上报') + '</code>'
       + (csDegr ? '（请求 <code>' + (csReq || '?') + '</code> 被降级：'
                   + 'rgba_supported=' + JSON.stringify(LG.rgba_supported)
                   + ' ⇒ <b>本次绿偏二分实验不成立</b>）' : '')
       + '</span>')
    : '';
  head.innerHTML = (isLive
    ? '<span class="ok">live 模式：画面实时，但<b>不留任何帧</b></span>'
      + ' <span class="sub">（设备不落盘；这个会话连 <code>thumbs/</code> 都不会生成）</span>'
    : '<span class="sub">当前会话 <code>' + (s.capture_mode || 'deliver')
      + '</code> —— 实时道 <b>不适用</b>（交付道在跑，画面见上面的监视道）</span>')
    + csLine;
  info.innerHTML = liveCell(LG);
  if (sync) sync.innerHTML = isLive
    ? '⚠ 这里<b>没有</b>“收全了没有”这个问题：实时道<b>可丢</b>且设备零留存 —— '
      + '<code>delivery_complete</code> 判 <b>null</b>，含义是<b>不适用</b>'
      + '（不是“判不了”，更不是“0 帧”；写成 <code>false</code> 会变成一个永远失败的进度条）。'
      + '要看的是「跳过（陈帧·设计内）」与「异常丢」<b>两个分开的数</b> —— '
      + '合并成一个就分不清“链路不够快”与“实现有 bug”。'
    : '';
}

async function ctl(action){
  const h=document.getElementById('hint');
  const sel = document.getElementById('capmode');
  const mode = (sel && sel.value) || 'deliver';
  // ⚠ 第 29 轮：颜色来源。**只在 start 时下发** —— 它是会话性质的一部分，
  //   中途切换会让同一个 session 里出现两种色彩处理的帧。
  const cs = document.getElementById('colsrc');
  const colsrc = (cs && cs.value) || 'yuv_bt601';
  try{
    const r = await fetch('/api/v1/control/'+action,{method:'POST',
      headers:{'Content-Type':'application/json','X-Token':PANEL_TOKEN},
      body: JSON.stringify(action==='start'? {capture_mode: mode, color_source: colsrc} : {})});
    const j = await r.json();
    if(!r.ok){ h.textContent = action+' 失败：HTTP '+r.status+' '+(j.message||j.error||'');
               return; }   // 不再把 403 显示成「session undefined」
    h.innerHTML = action+' → session '+j.session_id+'，模式 <code>'+(j.capture_mode||'-')+'</code>，'
      +'颜色来源 <code>'+(j.color_source||'-')+'</code>，'
      +'送达 '+j.sent_to+' 个 App'
      + (j.sent_to? '' : '（⚠ 没有 App 在线：先在手机 App 里连接）')
      + ((action==='start' && j.capture_mode==='live')
          ? ' <b class="bad">⚠ live：跑完不落盘、不留帧</b>' : '')
      + ((action==='start' && j.color_source==='rgba_hal')
          ? ' <b>正在跑绿偏二分实验的 HAL 档</b>：若设备不支持 RGBA_8888，'
            +'下方「实时道」会标出降级 ⇒ <b>本次二分不成立</b>' : '');
    refresh();
  }catch(e){ h.textContent = '失败: '+e; }
}
refresh(); setInterval(refresh, 3000);
</script></body></html>
"""


# --------------------------------------------------------------------------- 启动

DEFAULT_PROFILE = {
    # M1 默认档：1920×1440 / Y 平面 / **不抽帧**（每一帧都留）。
    # ⚠ 不抽帧意味着写盘与回传的负载是**全帧率**的：单帧 1920×1440 纯 Y = 2 764 800 B，
    # 30.04 fps ⇒ **83.0 MB/s**，而手机→PC 实测链路只有 5.5 MB/s（差 15.1 倍）
    # ⇒ 采集期必须先落手机本地、采集结束后排空回传。见 docs/对外接口与单一交付道.md。
    # 为什么不用推荐档 4096×3072：R19 尚未收口 —— 它的带宽是"算出来的"，等实测吞吐。
    "output_size": [1920, 1440],
    "format": "YUV_Y",              # 由 YUV_420_888 取 Y 平面（编码在采集之后，不算换采集路径）
    "focus_diopter": 1.001001,      # 工作距离 1 m（M0 实测回读值，非请求值）
    # 变焦：**0 = 不指定**（由 App 界面上的档位决定，定位默认 0.6× 超广角）。
    # 写非 0 值才会覆盖 App 的选择。为什么默认不指定：0.6× 与 1.0× 是**两颗物理镜头**，
    # 谁在机器旁边谁最清楚该用哪一颗；PC 默默压成 1.0 会让手机上的选择形同虚设。
    # ⚠ 不管谁定，生效值都会逐帧记进元数据（requested.zoom_ratio + zoom_source）。
    "zoom_ratio": 0,
    # OIS/EIS：**锁定配方的断言，不是可调旋钮** —— App 侧的请求里稳定器恒为 OFF
    # （M0 ② 的锁定配方，内参可复现性依赖它），这里写出来只是让门控能验"设备是否照做"。
    # ⚠ 别把这两个键当旋钮去写 ON：App 不会照办（它是锁定项），门控会判 fail 并停流。
    "ois": "OFF", "eis": "OFF",
    "exposure_time_ns": 10000000, "iso": 200,
    "frame_interval_ms": 0,         # 0 = 不限（按 sensor 节奏采）
    # ⚠ 第 24 轮起默认 **0 = 不抽帧 = "每一帧都留"**（交付道与归档道合并后的唯一语义）。
    # 设成 >0 才是**降载旋钮**（全帧率写不动/传不动时才用），账本里 `every_frame` 会变 false。
    "deliver_fps": 0,
    "crop_region": [0, 0, 4096, 3072],
    "delivery": "stream",           # D24：连续流 + 每一帧都留
}


def build_app(hub: Hub) -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024,
                          middlewares=[loopback_only_middleware])
    app["hub"] = hub
    app.router.add_get("/", h_index)
    app.router.add_get("/api/v1/sessions", h_sessions)
    app.router.add_get("/api/v1/pair", h_pair)
    # ---- 对外接口面（被整体架构/本机其它程序调用）----
    app.router.add_get("/api/v1/capabilities", h_capabilities)
    app.router.add_get("/api/v1/host/status", h_host_status)
    app.router.add_get("/api/v1/host/events", h_host_events)
    app.router.add_get("/stream/mjpeg", h_stream_mjpeg)
    app.router.add_post("/api/v1/files/init", h_init)
    app.router.add_put("/api/v1/files/{file_id}", h_put_chunk)
    app.router.add_post("/api/v1/files/{file_id}/complete", h_complete)
    app.router.add_post("/api/v1/control/{action}", h_control)
    app.router.add_post("/api/v1/linktest", h_linktest)
    # 实时道（`capture_mode=live`，设备面）：App 推彩色 JPEG，**不落盘**，只转发给监视道。
    app.router.add_post("/api/v1/live/frame", h_live_frame)
    app.router.add_get("/api/v1/sessions/{sid}/thumb/latest", h_thumb_latest)
    app.router.add_get("/ws", h_ws)
    app.router.add_get("/ws/panel", h_panel_ws)
    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="PC 端接收服务（M1）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--data-dir", default=str(Path(__file__).resolve().parent / "data"))
    ap.add_argument("--token", default=None, help="会话 token（默认随机）")
    ap.add_argument("--max-mbps", type=float, default=40.0,
                    help="交付道限速（MB/s）；0 = 不限速")
    ap.add_argument("--profile", default=None, help="profile JSON 文件（缺省用内置保守档）")
    ap.add_argument("--profile-id", default="m1-default")
    ap.add_argument("--lan-ip", default=None,
                    help="强制使用该地址生成二维码/URL（现场认准某张网卡时用）")
    args = ap.parse_args(argv)

    profile = dict(DEFAULT_PROFILE)
    if args.profile:
        profile.update(json.loads(Path(args.profile).read_text(encoding="utf-8")))
    token = args.token or secrets.token_hex(4)
    hub = Hub(Path(args.data_dir), args.max_mbps, token)
    hub.profile = profile
    hub.profile_id = args.profile_id

    print("=" * 72)
    print("相机采集 · PC 端接收服务（M1）")
    print("=" * 72)
    print(f"  数据目录 : {hub.data_dir}")
    print(f"  profile  : {args.profile_id} = {json.dumps(profile, ensure_ascii=False)}")
    print(f"  限速     : {'不限' if not args.max_mbps else f'{args.max_mbps} MB/s'}")
    addrs = lan_addresses(args.lan_ip)
    if not addrs:
        print("  ⚠ 未取到任何局域网地址：手机将无法连接。请用 --lan-ip <本机局域网IP> 指定。")
        addrs = [("127.0.0.1", "loopback", True)]
    # 配对码要用"手机该连的那个地址"：把它钉在 hub 上（面板与终端用的是同一个值），
    # 而不是各自再算一次 —— 两处各算一次就是"同一台机器给出两个地址"的来源。
    hub.lan_ip = addrs[0][0]
    # 候选**全部**留下来：`/api/v1/pair` 要把它们给出去（多网卡机器上首选未必对）。
    hub.lan_candidates = list(addrs)
    hub.port = args.port
    print("  PC 主控：面板「▶ 开始采集 / ■ 停止」下发 start/stop，App 回 start_ack/stop_ack（闭环）")
    print("  手机 App 连接方式（任选）：")
    print("    ① 扫码：App 里点「扫码配对」，对着下面这个二维码（面板顶部也有同一个）")
    print("    ② 粘贴：把下面【首选】那一行整行 URL 粘进地址框（token 会自动带上）")
    print("    ③ 手输：只填 ip:port，再单独填 token")
    for i, (ip, name, virt) in enumerate(addrs):
        tag = "【首选】" if i == 0 else "      "
        note = ("  ← ⚠ 虚拟网卡，手机连不上，别用" if virt else "")
        print(f"    {tag} http://{ip}:{args.port}/?token={token}   [{name}]{note}")
    uri = pair_uri(addrs[0][0], args.port, token)
    print(f"  配对码（含义：地址 {addrs[0][0]}:{args.port} + token，扫它等于拿到凭据，别外传）：")
    print_qr(uri)
    print(f"  配对码原文（设备不支持扫码时可手抄）：{uri}")
    print(f"  自检（在电脑上）：curl http://{addrs[0][0]}:{args.port}/api/v1/sessions")
    print("  自检（在手机上）：浏览器打开 http://%s:%d/?token=%s —— 能看到面板就说明网络通"
          % (addrs[0][0], args.port, token))
    print("-" * 72)
    print("  给**本机其它程序**用的接口（整体架构接入点，均需 token，且只允许本机访问）：")
    print(f"    能力声明 : http://127.0.0.1:{args.port}/api/v1/capabilities   （无需 token，不含凭据）")
    print(f"    状态     : http://127.0.0.1:{args.port}/api/v1/host/status?token={token}")
    print(f"    事件广播 : ws://127.0.0.1:{args.port}/api/v1/host/events?token={token}")
    print(f"    实时画面 : http://127.0.0.1:{args.port}/stream/mjpeg?token={token}   （MJPEG，可丢）")
    print("    三行接入 : python -c \"import sys;sys.path.insert(0,'pc');from hostlink import HostLink;"
          f"h=HostLink('http://127.0.0.1:{args.port}','{token}');print(h.status()['capture'])\"")
    print("  ⚠ 实时画面（监视道）是**有损且可丢**的：只用于「看得见」。")
    print("     测量/分析取交付道（frames/）：**每一帧**的原始 Y 平面，逐帧 sha256 已校验。")
    print("     账本（应有 / 机关内存 / 两个缺口）见 host/status 的 `store` 段或面板最右列。")
    print("=" * 72)
    try:
        web.run_app(build_app(hub), host=args.host, port=args.port, print=None,
                    access_log=None)
    except OSError as e:
        # 启动失败要给人话，不要一串 traceback 让人以为是代码坏了。
        # 这两种现场最常遇到，且处置完全不同。
        win = getattr(e, "winerror", None)
        if win == 10048 or e.errno == 98:
            print(f"\n✗ 端口 {args.port} 已被占用（可能上一次的服务没关干净）。")
            print(f"  处置：换端口 `--port {args.port + 1}`，或先结束占用进程：")
            print(f'    netstat -ano | findstr :{args.port}     # 看最后一列的 PID')
            print(f"    taskkill /PID <PID> /F")
        elif win == 10013 or e.errno == 13:
            print(f"\n✗ 无权绑定 {args.host}:{args.port}。")
            print("  处置：改用 `--host 0.0.0.0`（默认）或 `--host <本机局域网IP>`；")
            print("       若仍失败，换一个端口再试。")
        else:
            print(f"\n✗ 启动失败：{e}")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
