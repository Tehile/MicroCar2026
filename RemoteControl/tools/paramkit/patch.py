"""写回: JSON → 固件。**只比值域, 不碰其余字节。**

安全设计(五条, 缺一不可)
------------------------
1. **值域替换而非整行替换**: 只在"代码段"里定位 `符号 = <值>`, 用切片拼接换掉值,
   制表符缩进 / 行尾中文注释 / `#ifdef` 门控 / CRLF 全部原样保留(全流程 bytes)。
2. **注释区不参与匹配**: `TrackRatioL=...,TrackRatioR=...;  //  TrackRatioL=...,` 这种
   "行尾注释里复述了一遍声明"的写法很常见, 所以只在**第一个 `//` 之前**的代码段里定位。
3. **单行唯一**: 代码段里 `符号 =` 必须恰好出现 1 次, 否则报错(不猜)。
4. **行指纹护栏**: 锚点记录 `sha16`(该行字节 sha256 前 16 位)。写前必须相等,
   否则报"锚点失配" —— 防止"文件被改动后仍按旧行号写回"。
5. **默认 dry-run**: 只有 `--commit` 才落盘; 落盘走"先 tmp 再 os.replace"。
"""
from __future__ import annotations

import re

from . import regexlib as R
from .bytesio import sha256, split_lines, write_atomic
from .registry import Registry


def fmt_value(v, expr=None) -> str | None:
    if expr:
        return expr
    if v is None:
        return None
    if isinstance(v, float):
        return str(int(v)) if v == int(v) else ("%.10g" % v)
    return str(v)


def code_span(line: bytes) -> int:
    """代码段终点(第一个 `//` 的位置; 没有则到行尾, 含 \\r)。"""
    idx = line.find(b"//")
    if idx < 0:
        return len(line)
    return idx


def set_value(line: bytes, symbol: str, value: str):
    """把代码段里的 `symbol = <值>` 换成 `symbol = value`。歧义/找不到 → None。"""
    end = code_span(line)
    code = line[:end]
    pat = R.value_span(symbol)
    m = pat.search(code)
    if not m:
        return None
    if pat.search(code, m.end()):        # 代码段内不唯一
        return None
    return code[:m.start(1)] + value.encode() + line[m.end(1):]


def plan(reg: Registry, params, names=None, policies=("apply",)):
    """→ (edits, problems); `edits` = {(file, line): {symbol: new_text}}"""
    edits, problems = {}, []
    for p in params:
        if names and p["name"] not in names:
            continue
        if p["policy"] not in policies:
            continue
        anchor = next((a for a in p.get("anchors", []) if a.get("kind") == "effective"), None)
        if not anchor:
            problems.append((p["name"], "no-anchor", "无 effective 锚点(纯影子 / BSS 字段)"))
            continue
        new = fmt_value(p.get("store", {}).get("value"), p.get("store", {}).get("expr"))
        if new is None:
            problems.append((p["name"], "no-value", "store 无 value/expr"))
            continue
        rel, lineno, sym = anchor["file"], int(anchor["line"]), anchor["symbol"]
        if not reg.exists(rel):
            problems.append((p["name"], "no-file", rel))
            continue
        lines = reg.lines(rel)
        if not (1 <= lineno <= len(lines)):
            problems.append((p["name"], "bad-line", "%s:%d 越界" % (rel, lineno)))
            continue
        line = lines[lineno - 1]
        got = sha256(line)[:16]
        if anchor.get("sha16") and got != anchor["sha16"]:
            problems.append((p["name"], "anchor-drift",
                             "%s:%d 行指纹 %s != 记录 %s" % (rel, lineno, got, anchor["sha16"])))
            continue
        if anchor.get("style") == "decl":
            shape = R.line_anchor(sym, anchor.get("ctype"))
            if not shape.match(line):
                problems.append((p["name"], "decl-shape",
                                 "%s:%d 不是 `%s %s = ...;` 形态" % (rel, lineno,
                                                                    anchor.get("ctype"), sym)))
                continue
        if len(R.value_span(sym).findall(line[:code_span(line)])) != 1:
            problems.append((p["name"], "line-not-unique",
                             "%s:%d 代码段内 `%s =` 不是唯一" % (rel, lineno, sym)))
            continue
        edits.setdefault((rel, lineno), {})[sym] = new
    return edits, problems


def render_edits(reg: Registry, edits: dict) -> list:
    """→ [(file, line, before, after)]; 不改盘。"""
    out = []
    for (rel, lineno), subs in sorted(edits.items()):
        line = reg.lines(rel)[lineno - 1]
        new = line
        for sym, val in subs.items():
            cand = set_value(new, sym, val)
            if cand is not None:
                new = cand
        if new != line:
            out.append((rel, lineno, line, new))
    return out


def commit(reg: Registry, edits: dict) -> list:
    """落盘: 按文件聚合 → bytes 级重建 → 原子写。"""
    touched, by_file = [], {}
    for (rel, lineno), subs in edits.items():
        by_file.setdefault(rel, {})[lineno] = subs
    for rel, linesubs in by_file.items():
        raw = reg.raw(rel)
        lines = list(split_lines(raw))
        for lineno, subs in linesubs.items():
            line = lines[lineno - 1]
            for sym, val in subs.items():
                cand = set_value(line, sym, val)
                if cand is not None:
                    line = cand
            lines[lineno - 1] = line
        new = b"\n".join(lines)
        if new == raw:          # 无实际改动: 不落盘、不计入 touched
            continue
        write_atomic(reg.path(rel), new)
        touched.append(rel)
    return touched
