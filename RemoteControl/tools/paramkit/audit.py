"""静态审计: 把"这条参数到底生不生效"变成机器结论。"""
from __future__ import annotations

import re

from . import regexlib as R
from .registry import D, FW_FILES


def enclosing_func(lines, idx):
    for j in range(idx, -1, -1):
        m = re.match(rb"^[ \t]*(?:static[ \t]+)?[A-Za-z_][\w \t\*]*\b([A-Za-z_]\w*)[ \t]*\([^;]*\)[ \t]*$",
                     lines[j])
        if m:
            return D(m.group(1)), j + 1
    return "?", 0


def overwrites(reg, symbol: str, limit: int = 8) -> list:
    """固件侧运行期覆写点: **声明行之外**的 `symbol =` 赋值。"""
    pat = re.compile(rb"(?<![A-Za-z0-9_.])" + re.escape(symbol.encode()) + rb"[ \t]*=[^=]")
    out = []
    for rel in FW_FILES:
        if not reg.exists(rel):
            continue
        lines = reg.lines(rel)
        for i, line in enumerate(lines):
            if reg.is_decl_line(rel, i, symbol):
                continue
            body = re.sub(rb"//.*$", b"", line)
            if pat.search(body):
                fn, fl = enclosing_func(lines, i)
                out.append({"file": rel, "line": i + 1, "func": fn, "func_line": fl,
                            "text": D(line).strip()[:100]})
                if len(out) >= limit:
                    return out
    return out


def arbitration_table(reg, params) -> list:
    rows = []
    for p in params:
        a = reg.arbitrate(p["mirror"])
        rows.append({"name": p["name"], "id": p["id"], "mirror": p["mirror"], **a})
    return rows


def gaps_for(reg, p) -> list:
    """单参数的风险项。level: error | warn | info"""
    out = []
    cls = p.get("arbitration", {}).get("class")
    if cls == "B" and not p.get("latent"):
        out.append({"level": "warn", "kind": "shadowed_default",
                    "msg": "B 类(镜像初值赢): 固件侧同名常量的改动**不生效**; "
                           "应登记 latent[] 说明被覆盖的来源"})
    if cls == "C" and not p.get("latent"):
        out.append({"level": "info", "kind": "no_host",
                    "msg": "C 类(无固件宿主): SETQ/SAVE 值无人消费; 应登记 latent[] 字面量出处"})
    if not p.get("consumers") and cls == "A":
        out.append({"level": "warn", "kind": "a_without_consumer",
                    "msg": "判为 A 类但未找到固件消费点(可能是测量量而非参数)"})
    if p.get("fw", {}).get("value") is None:
        out.append({"level": "error", "kind": "unparsable_init",
                    "msg": "初值表达式无法受限求值: %r" % p.get("fw", {}).get("init")})
    return out
