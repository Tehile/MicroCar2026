"""渲染: JSON → Markdown 生成区(只写标记区, 区外逐字节保留)。"""
from __future__ import annotations

from .bytesio import sha256

BEGIN = "<!-- GEN:BEGIN id=%s sha=%s -->"
END = "<!-- GEN:END -->"

ARB_LABEL = {"A": "固件赢", "B": "⚠镜像赢", "C": "⚠空转"}


def markdown_table(params, title="参数总表") -> str:
    head = ("| id | 链路名 | 宏 | 分组 | 类型 | 开机生效值 | 仲裁 | 释义 | 生效源 |\n"
            "|---:|---|---|---|---|---:|---|---|---|\n")
    rows = []
    for p in sorted(params, key=lambda x: x["id"]):
        fw = p.get("fw", {})
        v = fw.get("value")
        vs = "—" if v is None else ("%.6g" % v)
        src = (fw.get("decl") or {}).get("file", "")
        ln = (fw.get("decl") or {}).get("line")
        doc = (p.get("doc") or {}).get("doc") or (p.get("doc") or {}).get("func") or ""
        doc = doc.replace("|", "/")
        if len(doc) > 58:
            doc = doc[:57] + "…"
        where = ("%s:%s" % (src, ln)) if ln else (src or "(BSS 无初值)")
        rows.append("| %d | %s | `%s` | %s | %s | %s | %s | %s | %s |" % (
            p["id"], p["name"], p["macro"], p["group"], p["kind"], vs,
            ARB_LABEL.get(p.get("arbitration", {}).get("class"), "?"), doc, where))
    return head + "\n".join(rows) + "\n"


def markdown_v3(data) -> str:
    """v3 多套参数表: 总览(每参数一行) + 套明细(每套一行)。

    `data` = `tools/params/params.competition.v3.json` (schema 3)。
    总览列 = 默认值(default_phase 指向的那一套) + 各类站点计数;
    套明细列 = `参数@时期 = 表达式 @ 文件:行` + 门控 + 可写标志。
    """
    params = data.get("params", [])
    order = [g["id"] for g in data.get("groups", [])]

    def _key(p):
        try:
            return (order.index(p["group"]), p["name"])
        except ValueError:
            return (len(order), p["name"])

    params = sorted(params, key=_key)
    out = []
    # ---- 总览 ----
    out.append("**总览（%d 项）** —— 默认值 = `default_phase` 指向的那一套"
               "（挑选优先级 search→spurt→init）；「套」= 常量赋值点数，"
               "`derived` = 公式赋值点（只登记、不写回），"
               "`dropped` = 远程档专属套（永不写回）。" % len(params))
    out.append("")
    out.append("| 参数 | 分组 | 类型 | 默认值 | 时期 | 套 | derived | dropped | 释义 |")
    out.append("|---|---|---|---|---|---:|---:|---:|---|")
    for p in params:
        ctype = next((s.get("ctype") for s in p.get("sets", []) if s.get("ctype")), None) or "—"
        de = p.get("default_expr")
        dv = "—" if de in (None, "") else "`%s`" % str(de).replace("|", "/")
        note = (p.get("note") or "").replace("|", "/")
        out.append("| %s | %s | %s | %s | %s | %d | %d | %d | %s |" % (
            p["name"], p["group"], ctype, dv, p.get("default_phase") or "—",
            len(p.get("sets", [])), len(p.get("derived", [])),
            len(p.get("dropped", [])), note))
    out.append("")
    # ---- 套明细 ----
    total = sum(len(p.get("sets", [])) for p in params)
    out.append("**套明细（%d 套）** —— `参数@时期 = 源码表达式 @ 锚点`；"
               "写回列 ✗ = 该套所在行内同符号多次赋值，需人工拆行后才能写"
               "（自动标 `writable:false`，`apply3` 跳过并登记）。" % total)
    out.append("")
    out.append("| 参数 | 时期 | 值 | 锚点 | 门控 | 写回 |")
    out.append("|---|---|---|---|---|---|")
    for p in params:
        for s in sorted(p.get("sets", []), key=lambda x: (x["file"], int(x["line"]))):
            gate = s.get("gate") or "—"
            wr = "✓" if s.get("writable", True) else "✗"
            out.append("| %s | %s | `%s` | %s:%s | %s | %s |" % (
                p["name"], s.get("phase", "?"), str(s.get("expr", "")).replace("|", "/"),
                s.get("file", "?"), s.get("line", "?"), gate, wr))
    out.append("")
    return "\n".join(out) + "\n"


def gap_table(params) -> str:
    rows = []
    for p in params:
        for g in p.get("_gaps", []) + p.get("gaps", []):
            rows.append("| %s | %s | %s | %s |" % (p["name"], g.get("level"),
                                                   g.get("kind"), g.get("msg", "")))
    if not rows:
        return "(无)\n"
    return "| 参数 | 级别 | 类别 | 说明 |\n|---|---|---|---|\n" + "\n".join(rows) + "\n"


def inject(doc_path: str, blocks: dict) -> str:
    """把 {id: text} 写进文档的对应标记区。文档里必须先有 `<!-- GEN:BEGIN id=... -->`。

    幂等: 旧 BEGIN 行(无论占位符还是带 sha 的旧标记)整体被替换为
    「新 sha 标记 + 新 body」, 不会累积重复标记行。
    """
    txt = open(doc_path, encoding="utf-8").read()
    for bid, body in blocks.items():
        lines = txt.splitlines(True)
        out, inside, found = [], False, False
        for ln in lines:
            if ln.strip().startswith("<!-- GEN:BEGIN id=%s" % bid):
                out.append(BEGIN % (bid, sha256(body.encode())[:8]) + "\n")
                out.append(body if body.endswith("\n") else body + "\n")
                inside = True
                found = True
                continue
            if inside:
                if ln.strip() == END:
                    out.append(ln)
                    inside = False
                continue
            out.append(ln)
        if not found:
            raise SystemExit("docs/%s 里找不到标记区 id=%s；请先手工加：\n  %s\n  %s"
                             % (doc_path, bid, BEGIN % (bid, "........"), END))
        txt = "".join(out)
    return txt
