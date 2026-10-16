"""源码登记簿: 把"参数落地链"抽成结构化数据。

链路(全部由正则得到, 无手工表)
------------------------------
    link.h  `#define LINK_P_X N`              → 参数宏 + id
    link.c  `linkParam[i].id/.q = ...`        → id → 表槽 → **镜像端符号**
    link_port.c `LP_SyncIn()`  固件 → 镜像   → A 类判据(镜像被固件刷新)
    link_port.c `LP_SyncOut()` 镜像 → 固件   → 出边(会被推给固件)
    link.c  `link_ParamApplyOne()`           → SETQ / Flash 载入的镜像自写
    固件文件 带初值声明                        → 开机默认值 + 写回锚点

仲裁 A/B/C 用**赋值边可达闭包**判:
    A 固件赢 : 既有"固件→镜像"入边(能被 SyncIn 刷新), 又有"镜像→固件"出边
    B 镜像赢 : 只有出边 ⇒ 固件常量每 tick 被镜像覆盖 (改固件常量**无效**)
    C 无人读 : 两侧都没有 ⇒ 真·影子(可 GET/SETQ/SAVE, 无消费者)
"""
from __future__ import annotations

import os
import re
from collections import defaultdict, deque

from . import regexlib as R
from .bytesio import read_bytes, split_lines

FW_FILES = (
    "USER/user.c",
    "HARDWARE/TIMER/timer.c",
    "UTILITY/utility.c",
    "HARDWARE/ADC/adc.c",
    "HARDWARE/LED/led.c",
    "HARDWARE/STMFLASH/stmflash.c",
    "USER/test.c",
)
REMOTE_FILES = ("REMOTE/link.c", "REMOTE/link_port.c")
ALL_FILES = FW_FILES + REMOTE_FILES

_KEYWORDS = {
    "u8", "u16", "u32", "s8", "s16", "s32", "int", "long", "float", "double", "char",
    "void", "unsigned", "const", "static", "if", "else", "for", "while", "return",
    "sizeof", "switch", "case", "break", "default", "goto", "do",
}


def D(b) -> str:
    return b.decode("gbk", "replace")


class Registry:
    """一次构建, 多处查询。所有源码只读一遍, 保存在内存里。"""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self._raw: dict = {}
        self._lines: dict = {}
        self._decls: dict = {}
        self._edges: dict = None
        self._macros: list = None
        self._slots: dict = None

    # ------------------------------------------------------------- 基础
    def path(self, rel: str) -> str:
        return os.path.join(self.root, rel.replace("/", os.sep))

    def raw(self, rel: str) -> bytes:
        if rel not in self._raw:
            self._raw[rel] = read_bytes(self.path(rel))
        return self._raw[rel]

    def lines(self, rel: str) -> list:
        if rel not in self._lines:
            self._lines[rel] = split_lines(self.raw(rel))
        return self._lines[rel]

    def exists(self, rel: str) -> bool:
        return os.path.exists(self.path(rel))

    # ------------------------------------------------------------- R1 宏块
    def macros(self) -> list:
        """link.h 里落在 `#ifdef MOUSE_REMOTE` 区域内的 LINK_P_* 宏。"""
        if self._macros is not None:
            return self._macros
        out, depth = [], []
        for i, line in enumerate(self.lines("REMOTE/link.h")):
            m = R.RE_COND.match(line)
            if m:
                kind, rest = m.group(1), m.group(2)
                if kind in (b"if", b"ifdef", b"ifndef"):
                    depth.append(rest.strip())
                elif kind == b"endif" and depth:
                    depth.pop()
                continue
            m = R.RE_PM_DEFINE.match(line)
            if m and m.group(1) != b"NUM":   # LINK_P_NUM 是条目数, 不是参数
                out.append({
                    "name": D(m.group(1)), "id": int(m.group(2)),
                    "macro": "LINK_P_" + D(m.group(1)),
                    "comment": D(m.group(3) or b"").strip(),
                    "line": i + 1,
                    "in_remote": b"MOUSE_REMOTE" in b"|".join(depth),
                })
        self._macros = out
        return out

    def link_p_num(self) -> dict:
        """两档的 `LINK_P_NUM`(按门控区域取首次匹配)。"""
        out, depth = {}, []
        for line in self.lines("REMOTE/link.h"):
            m = R.RE_COND.match(line)
            if m:
                kind, rest = m.group(1), m.group(2).strip()
                if kind in (b"if", b"ifdef", b"ifndef"):
                    depth.append((b"!" if kind == b"ifndef" else b"") + rest)
                elif kind in (b"else", b"elif"):
                    if depth:
                        depth[-1] = b"!" + depth[-1].lstrip(b"!")
                elif kind == b"endif" and depth:
                    depth.pop()
                continue
            m = R.RE_PM_NUM.match(line)
            if m:
                # 取**最内层**的 MOUSE_REMOTE 项判档:
                # `!MOUSE_REMOTE` 里也含子串 `MOUSE_REMOTE`, 不能用 `in`;
                # 用最内层而不是 any(), 是因为 link.h:1 还有一层整文件 `#ifdef MOUSE_REMOTE`。
                ms = [t for t in depth if t in (b"MOUSE_REMOTE", b"!MOUSE_REMOTE")]
                key = "remote" if (ms and ms[-1] == b"MOUSE_REMOTE") else "competition"
                out.setdefault(key, int(m.group(1)))
        return out

    # ------------------------------------------------------------- R7 搬运表
    def slots(self) -> dict:
        """macro → {slot, qexpr, mirror, qline}"""
        if self._slots is not None:
            return self._slots
        out = {}
        lines = self.lines("REMOTE/link.c")
        for i, line in enumerate(lines):
            m = R.RE_SLOT_ID.match(line)
            if not m:
                continue
            macro = D(m.group(2))
            for j in range(i + 1, min(i + 5, len(lines))):
                m2 = R.RE_SLOT_Q.match(lines[j])
                if m2:
                    qexpr = D(m2.group(2)).strip()
                    out[macro] = {"slot": int(m2.group(1)), "qexpr": qexpr,
                                  "mirror": mirror_of(qexpr), "qline": j + 1}
                    break
        self._slots = out
        return out

    def case_macros(self) -> set:
        """link_ParamApplyOne 里有 case 分支的宏(防"加了 id 忘了搬")。"""
        return set(D(m.group(1)) for m in
                   (R.RE_CASE_PM.match(l) for l in self.lines("REMOTE/link.c")) if m)

    def func_stmts(self, rel: str, fname: str) -> list:
        """函数体内所有赋值语句 → [(src, dest, line), ...]"""
        lines = self.lines(rel)
        sig = R.func_sig(fname)
        start = next((i for i, l in enumerate(lines) if sig.match(l)), None)
        if start is None:
            return []
        out = []
        for i in range(start + 1, len(lines)):
            if lines[i].rstrip() == b"}":
                break
            s = re.sub(rb"//.*$", b"", re.sub(rb"/\*.*?$", b"", lines[i])).strip()
            if not s.endswith(b";") or re.match(rb"^(if|for|while|return|else|switch)", s):
                continue
            parts = R.RE_ASSIGN_SPLIT.split(s[:-1])
            if len(parts) < 2:
                continue
            rhs = re.sub(R.CAST, b"", parts[-1])
            for dest in _pick(parts[0]):
                for src in _pick(rhs):
                    out.append((src, dest, i + 1))
        return out

    def edges(self) -> dict:
        if self._edges is None:
            e = defaultdict(set)
            for rel, fn in (("REMOTE/link_port.c", "LP_SyncIn"),
                            ("REMOTE/link_port.c", "LP_SyncOut"),
                            ("REMOTE/link.c", "link_ParamFromSys"),
                            ("REMOTE/link.c", "link_ParamApplyOne")):
                for src, dest, ln in self.func_stmts(rel, fn):
                    e[src].add((dest, rel, ln))
            self._edges = e
        return self._edges

    def closure(self, start: str, hops: int = 8) -> list:
        e, seen, q = self.edges(), set(), deque([(start, 0)])
        out = []
        while q:
            n, d = q.popleft()
            if d >= hops:
                continue
            for dest, rel, ln in e.get(n, ()):
                if dest in seen:
                    continue
                seen.add(dest)
                out.append((dest, rel, ln))
                q.append((dest, d + 1))
        return out

    # ------------------------------------------------------------- R3 声明
    def decls(self) -> dict:
        """symbol → {file, line, type, const, init, value} (首次带初值的定义)"""
        if self._decls:
            return self._decls
        out = {}
        for rel in ALL_FILES:
            if not self.exists(rel):
                continue
            for i, line in enumerate(self.lines(rel)):
                m = R.RE_DECL_LINE.match(line)
                if not m:
                    continue
                ctype = D(m.group(2))
                is_const = bool(m.group(1))
                for d in R.RE_DECLARATOR.finditer(m.group(3)):
                    name = D(d.group(1))
                    if name in out:
                        continue
                    expr = D(d.group(2)).strip()
                    out[name] = {
                        "file": rel, "line": i + 1, "type": ctype, "const": is_const,
                        "init": expr, "text": D(line), "value": eval_expr(expr),
                    }
        self._decls = out
        return out

    def fw_symbols(self) -> set:
        return set(self.decls())

    def decl_sites(self, symbol: str) -> list:
        """该符号的**全部**带初值声明行(典型: 双档 `#ifdef/#else` 成对声明)。"""
        out = []
        pat = re.compile(rb"(?<![A-Za-z0-9_])" + re.escape(symbol.encode()) + rb"[ \t]*=")
        for rel in ALL_FILES:
            if not self.exists(rel):
                continue
            lines = self.lines(rel)
            regions = self.region_map(rel)
            for i, line in enumerate(lines):
                if not R.RE_DECL_LINE.match(line) or not pat.search(line):
                    continue
                first = True
                for d in R.RE_DECLARATOR.finditer(R.RE_DECL_LINE.match(line).group(3)):
                    if D(d.group(1)) == symbol:
                        break
                    first = False
                out.append({"file": rel, "line": i + 1, "gate": regions[i],
                            "first_declarator": first, "text": D(line).strip()[:120]})
        return out

    def region_map(self, rel: str) -> list:
        """逐行的活动门控条件(`#ifdef A && !B` 之类的拼接)。"""
        key = "__region__" + rel
        if key in self._raw:
            return self._raw[key]
        out, depth = [], []
        for line in self.lines(rel):
            m = R.RE_COND.match(line)
            if m:
                kind, rest = m.group(1), m.group(2).strip()
                if kind in (b"if", b"ifdef", b"ifndef"):
                    out.append(_uniq(depth))
                    depth.append((b"!" if kind == b"ifndef" else b"") + rest)
                elif kind in (b"else", b"elif"):
                    if depth:
                        depth[-1] = b"!" + depth[-1].lstrip(b"!")
                    out.append(_uniq(depth))
                elif kind == b"endif":
                    if depth:
                        depth.pop()
                    out.append(_uniq(depth))
                continue
            out.append(_uniq(depth))
        self._raw[key] = out
        return out

    def is_decl_line(self, rel: str, idx0: int, symbol: str) -> bool:
        pat = re.compile(rb"(?<![A-Za-z0-9_])" + re.escape(symbol.encode()) + rb"[ \t]*=")
        line = self.lines(rel)[idx0]
        return bool(R.RE_DECL_LINE.match(line) and pat.search(line))

    def consumers(self, symbol: str, limit: int = 6) -> list:
        """固件侧(REMOTE/ 之外)真实引用点 —— 排除**所有**声明行。"""
        pat = re.compile(rb"(?<![A-Za-z0-9_])" + re.escape(symbol.encode()) + rb"(?![A-Za-z0-9_])")
        out = []
        for rel in FW_FILES:
            if not self.exists(rel):
                continue
            for i, line in enumerate(self.lines(rel)):
                if self.is_decl_line(rel, i, symbol):
                    continue
                if pat.search(line):
                    out.append({"file": rel, "line": i + 1, "text": D(line).strip()[:100]})
                    if len(out) >= limit:
                        return out
        return out

    # ------------------------------------------------------------- 仲裁
    def arbitrate(self, mirror: str) -> dict:
        """返回 {class, evidence}"""
        down = [(d, f, l) for d, f, l in self.closure(mirror) if d in self.fw_symbols()]
        up = [(d, f, l) for d, f, l in self.closure(mirror) if d in {"LP_SyncIn"}]
        refreshed = _reachable_from_fw(self, mirror)
        if down and refreshed:
            cls = "A"
        elif down:
            cls = "B"
        else:
            cls = "C"
        return {"class": cls,
                "sink": down[0][0] if down else None,
                "evidence": ("%s←%s:%d" % (down[0][0], down[0][1], down[0][2])) if down else "无出边(影子)"}


def _reachable_from_fw(reg: Registry, mirror: str) -> bool:
    """镜像端是否被 LP_SyncIn 从固件刷新(只走 SyncIn 的边)。"""
    for src, dest, _ in reg.func_stmts("REMOTE/link_port.c", "LP_SyncIn"):
        if dest == mirror:
            return True
    # 一跳中转: SyncIn 写 A, 别处再 A -> mirror
    for src, dest, _ in reg.func_stmts("REMOTE/link_port.c", "LP_SyncIn"):
        for d2, _, _ in reg.edges().get(dest, ()):
            if d2 == mirror:
                return True
    return False


def _uniq(depth) -> str:
    seen, out = set(), []
    for x in depth:
        k = D(x)
        if k not in seen:
            seen.add(k)
            out.append(k)
    return " && ".join(out)


def _pick(chunk: bytes) -> list:
    """取表达式里的符号节点: 剔关键字/函数名/linkParam 枢纽/点号成员/数组下标。"""
    out = []
    for x in re.finditer(R.IDENT, chunk):
        name, end = D(x.group(0)), x.end()
        prev = chunk[x.start() - 1:x.start()]
        if name in _KEYWORDS or name == "linkParam" or prev in (b"[", b"."):
            continue
        if chunk[end:end + 1] == b"(":
            continue
        out.append(name)
    return out


def mirror_of(qexpr: str) -> str:
    """从 `linkParam[i].q` 右值认出镜像端符号。"""
    b = qexpr.encode()
    m = re.search(rb"^[ \t]*" + R.CAST + rb"[ \t]*(?:" + R.CAST + rb"[ \t]*)*(" + R.IDENT + rb")", b)
    if m and b"." in m.group(1):
        return D(m.group(1))
    m = re.search(rb"([A-Za-z_]\w*)[ \t]*\([ \t]*(" + R.IDENT + rb")[ \t]*\)", b)
    if m:
        return D(m.group(2))
    b2 = re.sub(R.CAST, b"", b)
    m = re.search(R.IDENT, b2)
    return D(m.group(0)) if m else "?"


def eval_expr(expr: str):
    """受限求值: 只允许数字与 + - * / ( )。不 eval 任意串。"""
    if not R.RE_SAFE_EXPR.match(expr):
        return None
    try:
        return eval(expr, {"__builtins__": {}}, {})  # noqa: S307 - 已用白名单正则约束
    except Exception:
        return None
