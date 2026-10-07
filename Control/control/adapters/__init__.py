"""上游适配器。

**铁律：只 import，不改上游源码**（判据 I9）。
三个模块各有自己的取证体系（`selftest.py` / `test_server.py` / 上游合成自检），
一旦改了它们的源码，那些判据的结论就不再对当前代码成立 —— 整合层的正确做法是
**当消费者**，出了问题把上游的原始信息原样透出，而不是在中间层"修好它"。
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Optional


class UpstreamMissing(RuntimeError):
    """上游模块 import 失败。消息里必须带上"路径 + 怎么修"。"""


_import_lock = threading.Lock()


def ensure_path(p: str) -> bool:
    """把上游目录加进 sys.path（幂等）。返回目录是否存在。"""
    if not p:
        return False
    ap = str(Path(p).resolve())
    if not os.path.isdir(ap):
        return False
    with _import_lock:
        if ap not in sys.path:
            sys.path.insert(0, ap)
    return True


def probe_module(name: str, path: str):
    """尝试 import 一个上游模块，失败时给出可照做的信息。

    返回 `(module_or_None, error_str_or_None)`。
    """
    if not ensure_path(path):
        return None, (f"上游路径不存在：{path}\n"
                      f"  · 检查 Control/config.yaml 的 paths 段（相对路径以 Control/ 为基准）")
    try:
        return __import__(name), None
    except Exception as e:                       # noqa: BLE001 - 要把原始异常透出去
        return None, (f"import {name} 失败：{type(e).__name__}: {e}\n"
                      f"  · 上游路径已确认存在（{path}），因此多为依赖缺失\n"
                      f"  · 本整合层固定用 general_env 运行："
                      f"C:\\Users\\liuru\\.conda\\envs\\general_env\\python.exe\n"
                      f"  · 运行 python tools/preflight.py 可定位缺失项")
