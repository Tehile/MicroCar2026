"""Minimal C preprocessor: strip comments and evaluate #ifdef/#if 0 blocks.

The firmware sources keep several generations of the same table around, some
commented out with `//`, some inside `/* ... */`, some behind `#ifdef
search_speed_50`. A naive regex finds all of them and silently picks the wrong
one (e.g. gyro_l_search appears as 170, 201, 210, 222 and 195 elements).

This module keeps the line count identical so callers can still report
`file:line` accurately.
"""

import re

DIRECTIVE_RE = re.compile(r"^\s*#\s*(ifdef|ifndef|if|elif|else|endif|define)\b(.*)$")
DEFINE_NAME_RE = re.compile(r"^([A-Za-z_]\w*)")


def strip_comments(text):
    """Replace // and /* */ comment bodies with spaces, preserving newlines."""
    out = list(text)
    i, n = 0, len(text)
    state = None
    while i < n:
        c = text[i]
        if state is None:
            if c == "/" and i + 1 < n and text[i + 1] == "/":
                out[i] = out[i + 1] = " "
                i += 2
                state = "line"
                continue
            if c == "/" and i + 1 < n and text[i + 1] == "*":
                out[i] = out[i + 1] = " "
                i += 2
                state = "block"
                continue
            if c == '"':
                state = "str"
            elif c == "'":
                state = "chr"
            i += 1
            continue
        if state == "line":
            if c == "\n":
                state = None
            else:
                out[i] = " "
        elif state == "block":
            if c == "*" and i + 1 < n and text[i + 1] == "/":
                out[i] = out[i + 1] = " "
                i += 2
                state = None
                continue
            if c != "\n":
                out[i] = " "
        elif state in ("str", "chr"):
            if c == "\\":
                i += 2
                continue
            if (state == "str" and c == '"') or (state == "chr" and c == "'"):
                state = None
        i += 1
    return "".join(out)


def _eval_if(expr, defines):
    e = expr.strip()
    e = re.sub(r"defined\s*\(\s*(\w+)\s*\)",
               lambda m: "1" if m.group(1) in defines else "0", e)
    e = re.sub(r"defined\s+(\w+)",
               lambda m: "1" if m.group(1) in defines else "0", e)
    for name in set(re.findall(r"[A-Za-z_]\w*", e)):
        e = re.sub(r"\b%s\b" % re.escape(name),
                   "1" if name in defines else "0", e)
    e = e.replace("&&", " and ").replace("||", " or ").replace("!", " not ")
    e = re.sub(r"(?<![<>!=])=(?!=)", "==", e)
    try:
        return bool(eval(e, {"__builtins__": {}}, {}))  # noqa: S307 - numeric only
    except Exception:
        return True


def collect_defines(text, defines=None):
    defines = set(defines or ())
    for line in strip_comments(text).split("\n"):
        m = DIRECTIVE_RE.match(line)
        if m and m.group(1) == "define":
            nm = DEFINE_NAME_RE.match(m.group(2).strip())
            if nm:
                defines.add(nm.group(1))
    return defines


def preprocess(text, defines=None):
    """Return (clean_text, defines). Inactive lines become empty strings."""
    defines = collect_defines(text, defines)
    lines = strip_comments(text).split("\n")
    out, stack, active = [], [], True
    for line in lines:
        m = DIRECTIVE_RE.match(line)
        if not m:
            out.append(line if active else "")
            continue
        kind, rest = m.group(1), m.group(2).strip()
        if kind == "define":
            if active:
                nm = DEFINE_NAME_RE.match(rest)
                if nm:
                    defines.add(nm.group(1))
        elif kind == "ifdef":
            cond = rest.split()[0] in defines if rest else False
            stack.append((active, cond))
            active = active and cond
        elif kind == "ifndef":
            cond = rest.split()[0] not in defines if rest else True
            stack.append((active, cond))
            active = active and cond
        elif kind == "if":
            cond = _eval_if(rest, defines)
            stack.append((active, cond))
            active = active and cond
        elif kind == "elif":
            if stack:
                parent, cur = stack[-1]
                cond = (not cur) and _eval_if(rest, defines)
                stack[-1] = (parent, cur or cond)
                active = parent and (cur or cond)
        elif kind == "else":
            if stack:
                parent, cur = stack[-1]
                stack[-1] = (parent, not cur)
                active = parent and (not cur)
        elif kind == "endif":
            if stack:
                parent, _ = stack.pop()
                active = parent
        out.append("")
    return "\n".join(out), defines
