"""抽取与合并: 固件 → JSON 记录。

**分工铁律**
    `fw`   —— 每次 extract **整体重写**, 是固件的只读快照, 人工不许改;
    `store`/`policy`/`doc`/`notes`/`history` —— 人工资产, extract **原样保留**。
两者允许不相等, 由 `check` 按 policy 分级报告(见 paramgen check)。
"""
from __future__ import annotations

import datetime
import hashlib
import os
import re

from . import audit
from .bytesio import sha256, split_lines
from .registry import D, Registry

SCHEMA_VERSION = 2

GROUPS = [
    {"key": "calib", "label": "生产标定", "order": 1},
    {"key": "ir", "label": "红外测量", "order": 2},
    {"key": "power", "label": "电源监控", "order": 3},
    {"key": "motion", "label": "运动控制", "order": 4},
]

GROUP_OF = {
    "calib": ("TRACKL", "TRACKR", "SPEEDL", "SPEEDR", "GYRORATIO", "ENCANGLE", "WALLDIST"),
    "ir": ("IRTHRWALL", "IRTHR45", "IRTHRCRIT", "IRTHRFLO", "IRTHRFMID", "IRTHRFHI",
           "IRTHRFNEAR", "IRKALQ", "IRKALP", "IRKALSW", "IRFRONTMODE"),
    "power": ("BATTKV", "BATTLOWMV", "BATTHIGHMV", "BATTALPHA", "BATTMAXPCT", "BATTMINPCT"),
}


def group_for(name: str) -> str:
    for k, names in GROUP_OF.items():
        if name in names:
            return k
    return "motion"


def kind_for(ctype: str, value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int) or ctype in ("u8", "u16", "u32", "s8", "s16", "s32", "int", "long"):
        return "int"
    return "float"


def quantize(value, ctype):
    """固件落地的取整: u16 angleratio=73.71 ⇒ 实际存 73。返回 (raw, effective, how)。"""
    raw = round(value, 12) if isinstance(value, float) else value
    if value is None:
        return None, None, "none"
    if ctype in ("u8", "u16", "u32", "s8", "s16", "s32"):
        return raw, int(value), "trunc"
    return raw, raw, "none"


def loss_pct(raw, eff):
    if raw in (None, 0) or eff is None or raw == eff:
        return 0.0
    return round((eff - raw) / raw * 100.0, 4)


def guess_unit(name: str, host_meta: dict) -> dict:
    m = host_meta.get(name, {})
    return {"unit": m.get("unit", ""), "decimals": m.get("decimals", 3),
            "doc": m.get("desc", "")}


def _mirror_decl(reg: Registry, mirror: str):
    """镜像端自己的声明(仅当它是裸符号时才有; 结构体字段落在 BSS)。"""
    if "." in mirror:
        return None
    return reg.decls().get(mirror)


def _hash_line(data: bytes, lineno: int) -> str:
    lines = split_lines(data)
    if 1 <= lineno <= len(lines):
        return sha256(lines[lineno - 1])[:16]
    return ""


def build_records(reg: Registry, existing: dict, host_meta: dict, doc_table: dict,
                  git_rev: str = "", host_ids: dict = None) -> list:
    old = {p["name"]: p for p in (existing or {}).get("params", [])}
    macros = [m for m in reg.macros() if m["in_remote"]]
    macros.sort(key=lambda m: m["id"])
    slots = reg.slots()
    cases = reg.case_macros()
    id2name = {int(v): k for k, v in (host_ids or {}).items()}
    out = []

    for m in macros:
        # 链路名以 **host 注册表** 为准(线上名), 宏名单独记; 二者不一致要记 gap
        name = id2name.get(m["id"], m["name"])
        macro = m["macro"]
        slot = slots.get(macro, {})
        mirror = slot.get("mirror", "?")
        arb = reg.arbitrate(mirror)
        cls = arb["class"]

        # ---- 有效默认值落在哪 ----
        if cls == "A":
            src_symbol, src_decl = arb["sink"], reg.decls().get(arb["sink"] or "")
            anchor_kind = "firmware-const"
        elif cls == "B":
            src_symbol, src_decl = mirror, _mirror_decl(reg, mirror)
            anchor_kind = "mirror-init"
        else:
            src_symbol, src_decl = mirror, _mirror_decl(reg, mirror)
            anchor_kind = "mirror-init" if src_decl else "none"

        fw = {"value": None, "init": None, "source": "unknown", "decl": None}
        if src_decl:
            raw_v, eff_v, q = quantize(src_decl["value"], src_decl["type"])
            fw = {"value": eff_v, "init": src_decl["init"], "source": src_decl["file"],
                  "quantize": q, "raw_value": raw_v, "loss": loss_pct(raw_v, eff_v),
                  "decl": {"file": src_decl["file"], "line": src_decl["line"],
                           "type": src_decl["type"], "const": src_decl["const"],
                           "sha16": _hash_line(reg.raw(src_decl["file"]), src_decl["line"])}}
        elif "." in mirror:
            # 结构体字段(BSS 零初值): irCfg.* / powerCfg.* / sysCfg.trackCoord
            host = reg.decls().get(mirror.split(".")[0])
            fw = {"value": 0, "init": None, "source": "bss-zero", "quantize": "none",
                  "raw_value": 0, "loss": 0.0,
                  "decl": {"file": host["file"] if host else "", "line": 0,
                           "type": "struct-field", "const": False, "sha16": "",
                           "note": "无初始化式 ⇒ 开机必为 0"}}

        # ---- 锚点(写回用) ----
        anchors = []
        if src_decl:
            sites = reg.decl_sites(src_symbol) if src_symbol else []
            first = next((x["first_declarator"] for x in sites
                          if x["file"] == src_decl["file"] and x["line"] == src_decl["line"]), True)
            anchors.append({
                "kind": "effective",
                "style": "decl" if (anchor_kind == "firmware-const" and first) else "member",
                "file": src_decl["file"], "line": src_decl["line"], "symbol": src_symbol,
                "ctype": (src_decl["type"]
                          if (anchor_kind == "firmware-const" and first) else None),
                "sha16": _hash_line(reg.raw(src_decl["file"]), src_decl["line"]),
                "note": ("开机生效值 = 此声明" if cls != "B"
                         else "开机生效值 = 此处镜像初值(固件侧同名常量每 tick 被覆盖)"),
            })

        prev = old.get(name, {})
        for a in prev.get("anchors", []):
            if a.get("kind") == "latent":
                anchors.append(a)   # latent 由人工维护, 保留

        store = prev.get("store") or {"value": fw["value"], "expr": fw["init"],
                                      "state": "in-sync", "why": "", "since": ""}
        has_anchor = any(a.get("kind") == "effective" for a in anchors)
        policy = prev.get("policy") or ("apply" if has_anchor and cls != "C" else "report")

        rec = {
            "name": name, "id": m["id"], "macro": macro,
            "macro_suffix": m["name"],
            "group": group_for(name),
            "kind": kind_for(src_decl["type"] if src_decl else "", fw["value"]),
            "wire": {"slot": slot.get("slot"), "qexpr": slot.get("qexpr"),
                     "mirror": mirror, "case_present": macro in cases},
            "arbitration": arb,
            "fw": fw,
            "anchors": anchors,
            "decls_all": reg.decl_sites(src_symbol) if src_symbol else [],
            "consumers": reg.consumers(src_symbol) if src_symbol and cls == "A" else [],
            "overwrites": audit.overwrites(reg, src_symbol) if src_symbol and cls == "A" else [],
            "store": store, "policy": policy,
            "doc": {**guess_unit(name, host_meta),
                    **{k: v for k, v in (doc_table.get(name) or {}).items()},
                    **{k: v for k, v in prev.get("doc", {}).items() if k in
                       ("cn", "func", "tune", "origin", "site")},
                    "macro_comment": m["comment"]},
            "latent": prev.get("latent", []),
            "notes": prev.get("notes", []),
            "history": prev.get("history", []),
        }
        if name != m["name"]:
            t = ("宏名 `%s` 与链路名 `%s` 不一致: 宏名去掉前缀后不是链路名, 以 host 注册表为准"
                 % (macro, name))
            if t not in rec["notes"]:
                rec["notes"].append(t)
        seen_note = set()
        rec["notes"] = [x for x in rec["notes"]
                        if not (x in seen_note or seen_note.add(x))]
        rec["_gaps"] = audit.gaps_for(reg, rec)
        out.append(rec)
    return out
