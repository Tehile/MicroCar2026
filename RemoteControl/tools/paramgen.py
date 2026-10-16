#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""paramgen —— 「固件源码 ⇄ 参数 JSON ⇄ 释义表 ⇄ 上位机注册表」的唯一 CLI 入口。

子命令
------
    extract   固件 → JSON      刷新 fw 快照, 保留 store/doc/history
    apply     JSON → 固件      只改写"值域", 默认 dry-run, --commit 才落盘
    check     多向对账          差异按 policy 分级, 有 error 时非零退出
    audit     只读审计          A/B/C 仲裁 + 覆写点 + 影子清单
    render    JSON → Markdown  生成参数总表(默认打印; --write 注入标记区)

用法(在 RemoteControl/ 下):
    python tools/paramgen.py extract --accept
    python tools/paramgen.py audit
    python tools/paramgen.py check
    python tools/paramgen.py apply                  # 预演
    python tools/paramgen.py apply --commit --name TURNP
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from paramkit import audit as A                      # noqa: E402
from paramkit import meta as M                       # noqa: E402
from paramkit import patch as P                      # noqa: E402
from paramkit import render as RD                    # noqa: E402
from paramkit.bytesio import sha256                  # noqa: E402
from paramkit.pipeline import (GROUPS, SCHEMA_VERSION, build_records)  # noqa: E402
from paramkit.registry import Registry               # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REDLINE = "68ad6be2272818269d6aac78e5356d9bb91036ddeaaa82c4ec3754a8a0948141"

PROFILES = {
    "competition": {"dir": "code_competition", "fw_ver": 300, "proto_ver": 8, "mazetype": 16},
    "legacy": {"dir": "code_stm32f1_mouse", "fw_ver": 222, "proto_ver": 8, "mazetype": 7},
}


def json_path(profile: str) -> str:
    return os.path.join(HERE, "params", "params.%s.json" % profile)


def load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def git_rev(root: str) -> str:
    try:
        import subprocess
        return subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def clean(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if not k.startswith("_")}


# --------------------------------------------------------------------------- extract
def fw_root(args) -> str:
    """固件根目录; `--fw-dir` 用于指到临时副本上做无风险验证。"""
    if getattr(args, "fw_dir", None):
        return os.path.abspath(args.fw_dir)
    return os.path.join(ROOT, PROFILES[args.profile]["dir"])


def cmd_extract(args):
    prof = PROFILES[args.profile]
    reg = Registry(fw_root(args))
    path = json_path(args.profile)
    cur = load_json(path)
    recs = build_records(reg, cur, M.host_meta(os.path.join(ROOT, "host")),
                         M.doc_table(os.path.join(ROOT, "docs")), git_rev(ROOT),
                         host_ids=host_params(os.path.join(ROOT, "host")))
    data = {
        "schema_version": SCHEMA_VERSION,
        "profile": args.profile,
        "generated": {
            "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "by": "tools/paramgen.py extract",
            "git_rev": git_rev(ROOT),
            "firmware_dir": prof["dir"],
        },
        "firmware": {**prof, "link_p_num": reg.link_p_num()},
        "wire": {"encoding": "Q16.16", "scale": 65536, "page": 115,
                 "page_addr": "0x0801CC00", "clamp_default": [0, 6553600]},
        "redline": {"hex_sha256": REDLINE, "rom": 82040, "ram": 20480,
                    "policy": "只在**故意**改参数值/新增参数时更新, 并同写 changelog"},
        "groups": GROUPS,
        "params": [clean(r) for r in recs],
        "gaps": [{"param": r["name"], **g} for r in recs for g in r["_gaps"]],
        "changelog": cur.get("changelog", []),
    }
    if not args.accept:
        print("== extract 预演(未写盘) ==")
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print("已写入 %s" % os.path.relpath(path, ROOT))
    print("条目 %d 项  |  A=%d B=%d C=%d  |  gaps=%d" % (
        len(recs),
        sum(1 for r in recs if r["arbitration"]["class"] == "A"),
        sum(1 for r in recs if r["arbitration"]["class"] == "B"),
        sum(1 for r in recs if r["arbitration"]["class"] == "C"),
        len(data["gaps"])))
    for r in recs:
        for g in r["_gaps"]:
            print("  [%s] %-12s %-18s %s" % (g["level"], r["name"], g["kind"], g["msg"][:80]))
    return 0


# --------------------------------------------------------------------------- audit
def cmd_audit(args):
    prof = PROFILES[args.profile]
    reg = Registry(fw_root(args))
    data = load_json(json_path(args.profile))
    params = data.get("params")
    if not params:
        print("请先跑 extract --accept")
        return 1
    print("%-12s %3s %-21s %-9s %-28s %s" % ("name", "id", "镜像端", "仲裁", "证据", "覆写点"))
    print("-" * 132)
    for p in sorted(params, key=lambda x: x["id"]):
        a = reg.arbitrate(p["wire"]["mirror"])
        ow = ",".join(sorted({"%s:%d" % (o["file"].split("/")[-1], o["line"])
                              for o in p.get("overwrites", [])})) or "-"
        print("%-12s %3d %-21s %-9s %-28s %s" % (
            p["name"], p["id"], p["wire"]["mirror"],
            {"A": "A 固件赢", "B": "B 镜像赢", "C": "C 无人读"}[a["class"]],
            a["evidence"], ow))
    print()
    print("== 影子/空转清单(可 GET/SETQ/SAVE 但无消费者) ==")
    for p in params:
        if p["arbitration"]["class"] == "C":
            print("   %-12s id=%-3d mirror=%s" % (p["name"], p["id"], p["wire"]["mirror"]))
    return 0


# --------------------------------------------------------------------------- check
RE_HOST_DICT = re.compile(r"^PARAM_IDS_(\w+)\s*:\s*Dict\[str,\s*int\]\s*=\s*\{(.*?)^\}", re.M | re.S)
RE_HOST_ITEM = re.compile(r'"([A-Z0-9_]+)"\s*:\s*(\d+)')


def host_params(host_dir: str) -> dict:
    path = os.path.join(host_dir, "host", "protocol", "commands.py")
    if not os.path.exists(path):
        return {}
    txt = open(path, encoding="utf-8").read()
    out = {}
    for _, body in RE_HOST_DICT.findall(txt):
        for name, pid in RE_HOST_ITEM.findall(body):
            out[name] = int(pid)
    return out


def cmd_check(args):
    prof = PROFILES[args.profile]
    reg = Registry(os.path.join(ROOT, prof["dir"]))
    data = load_json(json_path(args.profile))
    if not data:
        print("请先跑 extract --accept")
        return 1
    params = data["params"]
    errors, warns, infos = [], [], []

    macros = {m["macro"] for m in reg.macros() if m["in_remote"]}
    # C1 宏集合
    jm = {p["macro"] for p in params}
    for x in sorted(macros - jm):
        errors.append("C1 固件有宏 %s 但 JSON 缺条目" % x)
    for x in sorted(jm - macros):
        errors.append("C1 JSON 有 %s 但固件无此宏" % x)
    # C11 id 唯一
    ids = [p["id"] for p in params]
    for i in set(ids):
        if ids.count(i) > 1:
            errors.append("C11 id %d 重复" % i)
    # C4 LINK_P_NUM
    num = reg.link_p_num().get("remote" if args.profile == "competition" else "competition")
    if num is not None and num != len(params):
        errors.append("C4 LINK_P_NUM=%s 但 JSON 条目 %d" % (num, len(params)))
    # C12 case 分支
    cases = reg.case_macros()
    for p in params:
        if p["macro"] not in cases:
            errors.append("C12 %s 在 link_ParamApplyOne 里没有 case 分支" % p["macro"])
        if p["wire"].get("slot") is None:
            errors.append("C12 %s 在 link_ParamFromSys 里没有搬运槽" % p["macro"])
    # C2 host id 集合
    hp = host_params(os.path.join(ROOT, "host"))
    if hp:
        for n, i in sorted(hp.items()):
            j = next((x for x in params if x["name"] == n), None)
            if not j:
                errors.append("C2 host 有参数 %s 但 JSON 无此名" % n)
            elif j["id"] != i:
                errors.append("C2 %s id: host=%d JSON=%d" % (n, i, j["id"]))
        for p in params:
            if p["name"] not in hp:
                warns.append("C2 %s 只在固件侧, host 注册表没有" % p["name"])
    # C3 fw 快照
    for p in params:
        mirror = p["wire"]["mirror"]
        a = reg.arbitrate(mirror)
        if a["class"] != p["arbitration"]["class"]:
            errors.append("C10 %s 仲裁漂移: JSON=%s 现场=%s"
                          % (p["name"], p["arbitration"]["class"], a["class"]))
        anchor = next((x for x in p.get("anchors", []) if x.get("kind") == "effective"), None)
        if anchor:
            d = reg.decls().get(anchor["symbol"])
            if d and p["fw"].get("init") is not None and d["init"] != p["fw"]["init"]:
                errors.append("C3 %s 初值已变: %r → %r (%s:%d)"
                              % (p["name"], p["fw"]["init"], d["init"],
                                 anchor["file"], anchor["line"]))
            elif sha256(reg.lines(anchor["file"])[int(anchor["line"]) - 1])[:16] != anchor.get("sha16"):
                errors.append("C3 %s 锚点行指纹已变: %s:%d"
                              % (p["name"], anchor["file"], anchor["line"]))
    # C13 分歧策略
    for p in params:
        fw, st, pol = p["fw"].get("value"), p["store"].get("value"), p["policy"]
        same = (fw == st) or (fw is None and st is None)
        if same:
            continue
        if pol == "pinned":
            errors.append("C13 %s policy=pinned 但 store(%s) != fw(%s)" % (p["name"], st, fw))
        elif pol == "report":
            if not p["store"].get("why"):
                errors.append("C13 %s policy=report 且与固件不一致, 但 store.why 为空" % p["name"])
            else:
                infos.append("C13 %s 已登记分歧: fw=%s store=%s (%s)"
                             % (p["name"], fw, st, p["store"]["why"]))
        else:
            infos.append("C13 %s 待写回: fw=%s → store=%s" % (p["name"], fw, st))
    # C14 policy=apply 但无有效锚点(改了也写不进去)
    for p in params:
        has = any(a.get("kind") == "effective" for a in p.get("anchors", []))
        if p["policy"] == "apply" and not has:
            errors.append("C14 %s policy=apply 但没有 effective 锚点 ⇒ 写不进去" % p["name"])
    # C5 host 元数据
    hm = M.host_meta(os.path.join(ROOT, "host"))
    for p in params:
        h = hm.get(p["name"])
        if not h:
            continue
        if h["unit"] != p["doc"].get("unit") or h["decimals"] != p["doc"].get("decimals"):
            warns.append("C5 %s 单位/小数位: host=(%s,%s) JSON=(%s,%s)"
                         % (p["name"], h["unit"], h["decimals"],
                            p["doc"].get("unit"), p["doc"].get("decimals")))

    for x in errors:
        print("ERROR  " + x)
    for x in warns:
        print("WARN   " + x)
    for x in infos:
        print("INFO   " + x)
    print("\n统计: error=%d warn=%d info=%d" % (len(errors), len(warns), len(infos)))
    if args.strict:
        return 1 if (errors or warns) else 0
    return 1 if errors else 0


# --------------------------------------------------------------------------- apply
def cmd_apply(args):
    prof = PROFILES[args.profile]
    reg = Registry(fw_root(args))
    data = load_json(json_path(args.profile))
    if not data:
        print("请先跑 extract --accept")
        return 1
    only = set(args.name) if args.name else None
    edits, problems = P.plan(reg, data["params"], names=only)
    rows = P.render_edits(reg, edits)
    print("== apply %s ==" % ("落盘" if args.commit else "预演(--dry-run)"))
    for rel, ln, before, after in rows:
        print("  %s:%d" % (rel, ln))
        print("    - %s" % before.decode("gbk", "replace").rstrip())
        print("    + %s" % after.decode("gbk", "replace").rstrip())
    for name, kind, msg in problems:
        if kind != "skip":
            print("  [%s] %-12s %s" % (kind, name, msg))
    print("将改 %d 行 / %d 文件" % (len(rows), len({r[0] for r in rows})))
    if not args.commit:
        print("(预演结束; 加 --commit 才写盘)")
        return 0
    touched = P.commit(reg, edits)
    print("已写: %s" % ", ".join(touched))
    return 0


# --------------------------------------------------------------------------- render
def cmd_render(args):
    data = load_json(json_path(args.profile))
    if not data:
        print("请先跑 extract --accept")
        return 1
    blocks = {"param-table": RD.markdown_table(data["params"])}
    if args.write:
        doc = os.path.join(ROOT, "docs", "参数名称-功能-调整表.md")
        txt = RD.inject(doc, blocks)
        with open(doc, "w", encoding="utf-8", newline="") as f:
            f.write(txt)
        print("已注入 %s 的标记区" % doc)
    else:
        print(blocks["param-table"])
    return 0


def main():
    ap = argparse.ArgumentParser(prog="paramgen", description="参数落地链读写工具")
    ap.add_argument("--profile", default="competition", choices=sorted(PROFILES))
    ap.add_argument("--fw-dir", help="固件根目录(默认 code_competition/; 可指到临时副本验证)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--fw-dir", help="固件根目录(默认 code_competition/; 可指到临时副本验证)")

    p = sub.add_parser("extract", parents=[common], help="固件 → JSON")
    p.add_argument("--accept", action="store_true", help="写回 JSON(默认只预演)")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("audit", parents=[common], help="只读审计")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("check", parents=[common], help="多向对账")
    p.add_argument("--strict", action="store_true", help="warn 也算失败")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("apply", parents=[common], help="JSON → 固件")
    p.add_argument("--commit", action="store_true", help="真正落盘(默认 pre演)")
    p.add_argument("--name", action="append", help="只处理指定参数(可多次)")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("render", parents=[common], help="JSON → Markdown 生成区")
    p.add_argument("--write", action="store_true", help="注入 docs 的标记区")
    p.set_defaults(func=cmd_render)

    args = ap.parse_args()
    try:
        sys.exit(args.func(args))
    except BrokenPipeError:
        sys.exit(0)


if __name__ == "__main__":
    main()
