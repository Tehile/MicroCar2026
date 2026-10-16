"""字节级安全 IO: 全流程不解码源码, 只用 bytes 正则。

为什么不用文本模式:
  `code_competition` 里 14 个文件是 **GBK + CRLF**(timer.c / utility.c / user.c / test.c 等),
  其余是 UTF-8; 一解码一编码就可能把 BOM / 行尾 / 非法字节改掉。
  本工具所有正则都是 **纯 ASCII**, 因此可以在 bytes 上直接匹配 —— 天然兼容任何编码。
"""
from __future__ import annotations

import hashlib
import os

__all__ = ["read_bytes", "write_atomic", "sha256", "split_lines", "join_lines"]


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def write_atomic(path: str, data: bytes) -> None:
    """先写同目录 .tmp 再 os.replace, 避免半截文件。"""
    tmp = path + ".paramgen.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def split_lines(data: bytes) -> list:
    """按 \\n 切分并**保留行尾**(CRLF 的 \\r 留在行内), 保证回写逐字节一致。"""
    return data.split(b"\n")


def join_lines(lines) -> bytes:
    return b"\n".join(lines)
