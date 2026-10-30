"""参数文档生成器。

用法::

    python -m aruco_locator.docgen                 # 打印到 stdout
    python -m aruco_locator.docgen -o docs/参数参考.md

参数表由 :mod:`aruco_locator.parameters` 的注册表派生，**不是手写的**，
因此不会出现"文档里有、代码里没有"的漂移。
"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .parameters import describe_profiles, parameter_catalog

GROUP_TITLE = {
    "detection": "detection —— 标记检测与角点精化",
    "geometry": "geometry —— 内顶点选择、坐标系与有效性判定",
    "refine": "refine —— 联合最小二乘精化",
    "output": "output —— 时序滤波",
    "camera": "camera —— 标定与差高视差修正",
    "grid": "grid —— 格点匹配第二通道",
}
GROUP_ORDER = ["detection", "geometry", "refine", "output", "camera", "grid"]


def _fmt_default(v) -> str:
    if isinstance(v, list):
        return "[" + ", ".join(str(x) for x in v) + "]"
    if v is None:
        return "null"
    return str(v)


def _param_table(group: str) -> str:
    rows = parameter_catalog(group=group)
    out = [
        "| 参数 | 类型 | 默认 | 单位 | 建议范围 | 说明 |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        desc = (r["desc"] or "—").replace("\n", " ")
        if r["tune_when"]:
            desc += f"<br>**何时调**：{r['tune_when']}"
        out.append(
            f"| `{r['name'].split('.', 1)[1]}` | `{r['type']}` | `{_fmt_default(r['default'])}` "
            f"| {r['unit'] or '—'} | {r['range'] or '—'} | {desc} |"
        )
    return "\n".join(out)


def _profile_table() -> str:
    out = ["| 档位 | 覆盖的参数 | 说明 |", "|---|---|---|"]
    for p in describe_profiles():
        if p["params"]:
            body = "<br>".join(f"`{k}` = `{_fmt_default(v)}`" for k, v in p["params"].items())
        else:
            body = "—（内置默认值）"
        out.append(f"| **`{p['name']}`** | {body} | {p['desc']} |")
    return "\n".join(out)


def build_markdown() -> str:
    total = len(parameter_catalog())
    parts = [
        "# 参数参考（自动生成）",
        "",
        f"> 由 `python -m aruco_locator.docgen` 生成，对应 aruco-locator **v{__version__}**。",
        "> 共 %d 个可调参数。**不要手改本文件** —— 改 `aruco_locator/parameters.py` 里的"
        "元数据后重新生成。" % total,
        "",
        "所有参数都可用点号路径读写，也可直接写叶子名（无歧义时）：",
        "",
        "```python",
        "from aruco_locator import create_locator, get_param, set_param, apply_params",
        "",
        "loc = create_locator('config.yaml', params={'detection.edge_search_px': 4.0})",
        "set_param(loc, 'geometry.vertex_mode', 'full')      # 点号路径",
        "set_param(loc, 'edge_refine', False)                 # 叶子名简写",
        "get_param(loc, 'camera.parallax.camera_height_mm')   # 读",
        "apply_params(loc, {'output': {'smooth': 'ema'}})     # 嵌套写法",
        "```",
        "",
        "写错参数名、类型不符或超出建议范围都会**直接抛异常**，不会被静默忽略。",
        "",
        "## 预设档位",
        "",
        _profile_table(),
        "",
        "```python",
        "loc = create_locator(profile='robust')                       # 套用档位",
        "loc = create_locator(profile='fast', edge_search_px=3.0)     # 档位之上再覆盖",
        "```",
        "",
        "---",
        "",
        "## 参数总表",
        "",
    ]
    for g in GROUP_ORDER:
        parts += [f"### {GROUP_TITLE.get(g, g)}", "", _param_table(g), ""]
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aruco_locator.docgen", description="生成参数参考文档")
    ap.add_argument("-o", "--out", default=None, help="输出文件；缺省打印到 stdout")
    args = ap.parse_args(argv)
    text = build_markdown()
    if args.out:
        d = os.path.dirname(os.path.abspath(args.out))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fp:
            fp.write(text + "\n")
        print(f"已写入 {args.out}（{len(text)} 字符）")
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
