"""释义元数据汇聚: 从三处**只读**来源取"人话", 不产出真值。

来源
----
① `REMOTE/link.h` 宏块行尾注释      → 一句话身份
② `host/host/protocol/commands.py`  → 单位 / 小数位 / 说明(PARAM_META)
③ `docs/参数名称-功能-调整表.md`      → 课件编号 / 调整技巧 / 备注

② 与 ③ 用正则解析源码文本, **不 import host 包**(避免拖起串口依赖)。
"""
from __future__ import annotations

import os
import re

__all__ = ["host_meta", "doc_table"]

RE_META_ROW = re.compile(r'^\s*"([A-Z0-9_]+)"\s*:\s*\(\s*"([^"]*)"\s*,\s*(\d+)\s*,\s*"((?:[^"\\]|\\.)*)"\s*\)', re.M)
RE_MD_ROW = re.compile(r"^\|\s*([0-9/\-]+?)\s*\|(.+?)\|\s*$", re.M)
RE_BOLD = re.compile(r"\*\*([A-Z0-9_]+)\*\*")


def host_meta(host_dir: str) -> dict:
    """name → {unit, decimals, desc}"""
    path = os.path.join(host_dir, "host", "protocol", "commands.py")
    if not os.path.exists(path):
        return {}
    txt = open(path, encoding="utf-8").read()
    out = {}
    for name, unit, dec, desc in RE_META_ROW.findall(txt):
        out[name] = {"unit": unit, "decimals": int(dec), "desc": desc}
    return out


def _cells(line: str) -> list:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def doc_table(docs_dir: str) -> dict:
    """name → {origin, tune, note, func}"""
    path = os.path.join(docs_dir, "参数名称-功能-调整表.md")
    if not os.path.exists(path):
        return {}
    out = {}
    for line in open(path, encoding="utf-8").read().splitlines():
        if not line.startswith("|") or set(line) <= set("|-: "):
            continue
        cols = _cells(line)
        if len(cols) < 7:
            continue
        m = RE_BOLD.search(cols[2])
        if not m:
            continue
        out[m.group(1)] = {
            "origin": cols[0],
            "func": cols[3],
            "tune": cols[4],
            "site": cols[5],
            "note": cols[7] if len(cols) > 7 else "",
        }
    return out
