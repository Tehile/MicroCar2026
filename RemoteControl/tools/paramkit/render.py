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
    """把 {id: text} 写进文档的对应标记区。文档里必须先有 `<!-- GEN:BEGIN id=... -->`。"""
    txt = open(doc_path, encoding="utf-8").read()
    for bid, body in blocks.items():
        lines = txt.splitlines(True)
        out, inside, found = [], False, False
        for ln in lines:
            if ln.strip().startswith("<!-- GEN:BEGIN id=%s" % bid):
                out.append(ln)
                out.append(BEGIN % (bid, sha256(body.encode()).hexdigest()[:8]) + "\n")
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
