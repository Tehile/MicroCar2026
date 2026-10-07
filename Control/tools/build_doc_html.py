#!/usr/bin/env python
"""把 docs/*.md 渲染成自包含的暗色 HTML（无外部依赖、无 CDN、可离线打开）。

用法:
    python tools/build_doc_html.py                      # 渲染 docs/ 下全部 .md
    python tools/build_doc_html.py docs/xxx.md          # 渲染指定文件

为什么单独写一个脚本而不是手写 HTML：
    设计方案会被反复改。「手抄一份 HTML」必然与 md 分叉，而分叉之后
    没人知道哪份是真相。⇒ md 是唯一真相来源，HTML 每次由它生成。
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

CSS = """
:root{
  --bg:#14161a; --bg2:#1a1d23; --bg3:#20242c; --line:#2b3038;
  --fg:#dfe3ea; --fg2:#9aa3b0; --fg3:#6b7480;
  --accent:#5aa9e6; --accent2:#7bd88f; --warn:#e5b567; --danger:#e06c75;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Cascadia Mono",monospace;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
}
#wrap{display:flex; align-items:flex-start; max-width:1500px; margin:0 auto}
/* ---------- 侧栏 ---------- */
#toc{
  position:sticky; top:0; flex:0 0 300px; height:100vh; overflow-y:auto;
  padding:22px 16px 60px; border-right:1px solid var(--line); background:var(--bg2);
}
#toc .brand{font-size:13px; color:var(--fg3); letter-spacing:.08em; margin-bottom:14px; text-transform:uppercase}
#toc ul{list-style:none; margin:0; padding-left:0}
#toc ul ul{padding-left:14px}
#toc li{margin:1px 0}
#toc a{display:block; padding:4px 8px; border-radius:6px; color:var(--fg2); text-decoration:none;
        font-size:13px; line-height:1.45; border-left:2px solid transparent}
#toc a:hover{background:var(--bg3); color:var(--fg)}
#toc>ul>li>a{color:var(--fg); font-weight:600}
/* ---------- 正文 ---------- */
main{flex:1 1 auto; min-width:0; padding:34px 46px 120px}
h1,h2,h3,h4{line-height:1.35; margin:1.9em 0 .6em; scroll-margin-top:16px}
h1{font-size:27px; margin-top:0; padding-bottom:.45em; border-bottom:2px solid var(--line)}
h2{font-size:21px; padding-bottom:.35em; border-bottom:1px solid var(--line); color:#fff}
h3{font-size:17px; color:var(--accent)}
h4{font-size:15px; color:var(--fg2)}
p{margin:.75em 0}
a{color:var(--accent)}
strong{color:#fff}
hr{border:0; border-top:1px solid var(--line); margin:2.2em 0}
/* ---------- 表格 ---------- */
table{border-collapse:collapse; width:100%; margin:1.1em 0; font-size:13.5px; display:block; overflow-x:auto}
th,td{border:1px solid var(--line); padding:7px 11px; text-align:left; vertical-align:top}
th{background:var(--bg3); color:#fff; font-weight:600; white-space:nowrap}
tr:nth-child(even) td{background:rgba(255,255,255,.014)}
td code{white-space:nowrap}
/* ---------- 代码 ---------- */
code{background:var(--bg3); padding:1px 5px; border-radius:4px; font-family:var(--mono);
     font-size:12.8px; color:#e8c07d}
pre{background:#0f1115; border:1px solid var(--line); border-radius:8px; padding:14px 16px;
    overflow-x:auto; margin:1.1em 0}
pre code{background:none; padding:0; color:#c8d0da; font-size:12.6px; line-height:1.65; white-space:pre}
/* ---------- 引用（本方案里全是纪律/警告） ---------- */
blockquote{margin:1.1em 0; padding:10px 16px; border-left:3px solid var(--warn);
           background:rgba(229,181,103,.07); border-radius:0 8px 8px 0; color:#f0e0c0}
blockquote p{margin:.3em 0}
blockquote code{background:rgba(0,0,0,.35)}
ul,ol{padding-left:1.6em}
li{margin:.28em 0}
@media (max-width:1080px){
  #toc{display:none}
  main{padding:20px 18px 80px}
}
"""


def build_one(md_path: Path) -> Path:
    text = md_path.read_text(encoding="utf-8")
    md = markdown.Markdown(
        extensions=["tables", "fenced_code", "toc", "sane_lists", "attr_list"],
        extension_configs={"toc": {"toc_depth": "1-3", "permalink": False}},
    )
    body = md.convert(text)
    toc = md.toc

    title = md_path.stem
    m = re.search(r"^#\s+(.+)$", text, re.M)
    if m:
        title = m.group(1).strip()

    page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>{CSS}</style>
</head>
<body>
<div id="wrap">
  <nav id="toc">
    <div class="brand">Control · 整合上位机</div>
    {toc}
  </nav>
  <main>
{body}
  </main>
</div>
</body>
</html>
"""
    out = md_path.with_suffix(".html")
    out.write_text(page, encoding="utf-8")
    return out


def main() -> int:
    targets = [Path(a) for a in sys.argv[1:]] or sorted(DOCS.glob("*.md"))
    if not targets:
        print(f"no .md under {DOCS}")
        return 1
    for p in targets:
        if not p.is_absolute():
            p = (ROOT / p) if (ROOT / p).exists() else p
        out = build_one(p)
        size = out.stat().st_size
        print(f"OK  {p.name}  ->  {out.name}  ({size/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
