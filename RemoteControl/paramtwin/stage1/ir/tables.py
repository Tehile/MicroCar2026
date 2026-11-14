"""TableIR: parse the `const short` turn tables and the actions that play them.

Two things are extracted straight from C source, never hand-maintained:

  1. every `const short name[N] = {...};` array (1 ms trajectory samples)
  2. every action in `taskturn()` (pattern -> setangle / tables / play window)
     plus the three standalone actions: TURNAROUND, POINTTURN90_L/R

The play window matters as much as the table: `length = sizeof(tbl) - 8` is
*bytes*, and the loop runs while `tick < length/2 - 2`, so the number of
samples actually played is `(2n + delta)/2 - 2`. A positive delta overruns the
array (RV135L reads 2 elements past the end).
"""

import hashlib
import math
import os
import re

from . import preproc

SCHEMA_VERSION = "1.0"

TBL_RE = re.compile(
    r"(?:static\s+)?const\s+short\s+(\w+)\s*\[\s*(\d+)\s*\]\s*=\s*\{([^}]*)\}\s*;")
CASE_RE = re.compile(r"case\s+(\w+)\s*:")

SIZE_RE = re.compile(
    r"length\s*=\s*sizeof\s*\(\s*(\w+)\s*\)\s*(?:([+-])\s*(\d+))?")

STANDALONE = (
    ("taskturnaround", "TURNAROUND"),
    ("taskpointturnleft", "POINTTURN90_L"),
    ("taskpointturnright", "POINTTURN90_R"),
)


def _read(path):
    with open(path, "rb") as fh:
        return fh.read().decode("gbk", errors="replace")


def _clean(path):
    """Source with comments removed and inactive #ifdef blocks blanked."""
    return preproc.preprocess(_read(path))[0]


def _line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def _brace_body(text, open_pos):
    depth = 0
    for i in range(open_pos, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_pos + 1:i]
    return text[open_pos + 1:]


def parse_tables(root):
    """Return (tables, warnings) for every active short[] literal under `root`."""
    tables, warnings = {}, []
    for dirpath, _dirs, names in os.walk(root):
        if any(part in dirpath for part in ("sessions", ".git", "Objects")):
            continue
        for name in sorted(names):
            if not name.endswith((".c", ".h")):
                continue
            path = os.path.join(dirpath, name)
            text = _clean(path)
            rel = os.path.relpath(path, root).replace("\\", "/")
            for lineno, line in enumerate(text.split("\n"), 1):
                for m in TBL_RE.finditer(line):
                    tname = m.group(1)
                    declared = int(m.group(2))
                    vals = [int(x) for x in re.findall(r"-?\d+", m.group(3))]
                    if len(vals) != declared:
                        warnings.append({
                            "code": "DECL_MISMATCH", "table": tname,
                            "declared": declared, "parsed": len(vals), "file": rel,
                        })
                    if tname in tables:
                        warnings.append({
                            "code": "DUPLICATE_TABLE", "table": tname,
                            "kept": "%s:%d" % (rel, lineno),
                            "dropped": tables[tname]["file"],
                        })
                    tables[tname] = {
                        "name": tname,
                        "n": len(vals),
                        "declared": declared,
                        "dtype": "s16",
                        "unit": "deg" if tname.startswith("gyro") else "cm/s",
                        "first": vals[0] if vals else 0,
                        "last": vals[-1] if vals else 0,
                        "min": min(vals) if vals else 0,
                        "max": max(vals) if vals else 0,
                        "sha16": hashlib.sha256(
                            ",".join(map(str, vals)).encode()).hexdigest()[:16],
                        "file": "%s:%d" % (rel, lineno),
                        "vals": vals,
                    }
    return tables, warnings


def _fields(block):
    out = {}
    m = re.search(r"setangle\s*\(\s*(-?\d+)\s*\)", block)
    if m:
        out["nominal_deg"] = int(m.group(1))
    m = re.search(r"turn_gyro\s*=\s*(\w+)", block)
    if m:
        out["gyro"] = m.group(1)
    m = re.search(r"turn_vl\s*=\s*(\w+)\s*;\s*turn_vr\s*=\s*(\w+)", block)
    if m:
        out["v_inner"], out["v_outer"] = m.group(1), m.group(2)
    m = SIZE_RE.search(block)
    if m:
        delta = 0
        if m.group(3):
            delta = int(m.group(3)) * (1 if m.group(2) == "+" else -1)
        out["length_expr"] = m.group(0).split("=", 1)[1].strip()
        out["size_delta"] = delta
    return out


def action_windows(root):
    """Hard play windows live in timer.c `case TURNAROUND/POINTTURN90`, not in
    utility.c (utility.c only waits for step==1 afterwards)."""
    path = os.path.join(root, "HARDWARE", "TIMER", "timer.c")
    if not os.path.exists(path):
        return {}
    text = _clean(path)
    out = {}
    for name in ("TURNAROUND", "POINTTURN90"):
        m = re.search(r"case\s+%s\s*:" % name, text)
        if not m:
            continue
        w = re.search(r"if\s*\(\s*tick\s*>\s*(\d+)\s*\)", text[m.end():m.end() + 1500])
        if w:
            out[name] = int(w.group(1))
    return out


def parse_actions(root, tables):
    actions, warnings = [], []
    rel = "UTILITY/utility.c"
    path = os.path.join(root, *rel.split("/"))
    if not os.path.exists(path):
        return actions, [{"code": "SOURCE_MISSING", "file": rel}]

    windows = action_windows(root)
    text = _clean(path)

    m = re.search(r"void\s+taskturn\s*\(\s*u8\s+pattern\s*\)\s*\{", text)
    if m:
        body = _brace_body(text, text.index("{", m.start()))
        base = _line_of(text, m.start())
        marks = [(c.start(), c.group(1)) for c in CASE_RE.finditer(body)]
        for i, (pos, cid) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(body)
            f = _fields(body[pos:end])
            if not f.get("gyro"):
                continue
            line = base + body.count("\n", 0, pos)
            actions.append(_make_action(
                cid, f, tables, "%s:%d" % (rel, line), "switch", warnings))

    for fn, aid in STANDALONE:
        m2 = re.search(r"void\s+%s\s*\(\s*\)\s*\{" % fn, text)
        if not m2:
            continue
        f = _fields(_brace_body(text, text.index("{", m2.start())))
        if not f.get("gyro"):
            continue
        key = "TURNAROUND" if aid == "TURNAROUND" else "POINTTURN90"
        f["hard_window"] = windows.get(key, 0)
        actions.append(_make_action(
            aid, f, tables, "%s:%d" % (rel, _line_of(text, m2.start())),
            "hard_stop", warnings))

    return actions, warnings


def _make_action(cid, f, tables, source, kind, warnings):
    g = tables.get(f["gyro"], {})
    vo = tables.get(f.get("v_outer", ""), {})
    vi = tables.get(f.get("v_inner", ""), {})
    n = g.get("n", 0)
    nominal = abs(f.get("nominal_deg", 0))

    if kind == "hard_stop":
        tick_max = f.get("hard_window") or 0
        length_expr = "step0: tick>%d" % tick_max
    else:
        delta = f.get("size_delta", 0)
        length_expr = f.get("length_expr", "")
        tick_max = max(0, (2 * n + delta) // 2 - 2)

    overrun = max(0, tick_max - (n - 1)) if n else 0
    idx = min(max(tick_max, 0), n - 1) if n else 0

    theta_tab = abs(g["vals"][0] - g["vals"][idx]) if n else 0.0
    omega_avg = theta_tab / tick_max if tick_max else 0.0

    b_est = None
    if n and vo.get("n") and vi.get("n") and theta_tab > 0:
        m = min(idx, vo["n"] - 1, vi["n"] - 1)
        acc = 0.0
        for i in range(m):
            d0 = abs(vo["vals"][i] - vi["vals"][i])
            d1 = abs(vo["vals"][i + 1] - vi["vals"][i + 1])
            acc += (d0 + d1) / 2.0
        b_est = (acc / 1000.0) / math.radians(theta_tab)

    if overrun:
        warnings.append({"code": "TABLE_OVERRUN", "action": cid,
                         "detail": "tick_max %d > n-1 %d" % (tick_max, n - 1)})
    if n and theta_tab == 0:
        warnings.append({"code": "EMPTY_TABLE", "action": cid,
                         "detail": "table %s carries no angular content" % f["gyro"]})

    return {
        "id": cid,
        "kind": kind,
        "nominal_deg": nominal,
        "gyro": f["gyro"],
        "v_outer": f.get("v_outer", ""),
        "v_inner": f.get("v_inner", ""),
        "n": n,
        "length_expr": length_expr,
        "tick_max": tick_max,
        "t_turn_ms": tick_max,
        "theta_tab": round(theta_tab, 2),
        "delta_theta": round(theta_tab - nominal, 2),
        "omega_avg": round(omega_avg, 2),
        "b_est_cm": round(b_est, 3) if b_est else None,
        "residual_deg": g["vals"][idx] if n else 0,
        "overrun": overrun,
        "empty": theta_tab == 0,
        "source": source,
    }


def load(root):
    tables, w1 = parse_tables(root)
    actions, w2 = parse_actions(root, tables)
    return {
        "schema_version": SCHEMA_VERSION,
        "root": os.path.basename(os.path.normpath(root)),
        "tables": list(tables.values()),
        "actions": actions,
        "warnings": w1 + w2,
    }


def table_map(tir):
    return {t["name"]: t for t in tir["tables"]}


def action_map(tir):
    return {a["id"]: a for a in tir["actions"]}


def summary(tir):
    acts = tir["actions"]
    d90 = [a["delta_theta"] for a in acts
           if a["nominal_deg"] == 90 and not a["empty"] and a["kind"] == "switch"]
    d180 = [a["delta_theta"] for a in acts
            if a["nominal_deg"] == 180 and not a["empty"] and a["kind"] == "switch"]
    return {
        "n_tables": len(tir["tables"]),
        "n_actions": len(acts),
        "n_overrun": sum(1 for a in acts if a["overrun"]),
        "n_empty": sum(1 for a in acts if a["empty"]),
        "asym_90": round(max(d90, default=0) - min(d90, default=0), 2),
        "max_delta_180": round(max(d180, default=0), 2),
    }
