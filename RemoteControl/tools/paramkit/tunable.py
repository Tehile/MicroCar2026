"""全量整定常量扫描器 (v3) —— 「多套(sets)」模型。

把每个**整定常量**抽成「同一逻辑参数 + 不同时期的多套值」, 每套带独立锚点
(`file` / `line` / `sha16` / `gate`), 写回按套进行。

边界 / 命名 / 数据模型见 `docs/参数全量整定常量化-方案v3.0.md` §1–§4。

三个硬规则(对应用户拍板)
--------------------------
1. **多套各自保留、各自写回** —— 不统一各套的值(§6-Q1 读法甲)。
2. **远程档与比赛档冲突时, 保留比赛档、舍弃远程档** ——
   远程专属门控(`#ifdef MOUSE_REMOTE`)与 `REMOTE/link.c` 镜像不进 `sets`,
   只进 `dropped[]` 留痕, **永不写回**。
3. **只切值域** —— 写回复用 `patch.set_value` 的三段式切片, 行指纹 `sha16` 护栏。
"""
from __future__ import annotations

import re
from collections import OrderedDict

from . import regexlib as R
from . import patch as P
from .bytesio import sha256, split_lines, write_atomic
from .registry import Registry, D

# ---------------------------------------------------------------- 白名单
# 由 docs/参数全量整定常量化-方案v3.0.md §1.2 枚举而来; 不在此表 = 不是整定量。
# (name, group, note)
TUNABLES: "list[tuple[str,str,str]]" = [
    # -- 标定系数 (USER/user.c, 双档两份, 取 #else 比赛档那份)
    ("angleratio", "calib", "编码器角度系数"),
    ("gangleratio", "calib", "陀螺仪角度系数"),
    ("TrackRatioL", "calib", "左轮编码器距离系数"),
    ("TrackRatioR", "calib", "右轮编码器距离系数"),
    ("SpeedRatioL", "calib", "左轮编码器速度系数"),
    ("SpeedRatioR", "calib", "右轮编码器速度系数"),
    ("dis2wall", "calib", "车居中时传感器测得的墙距"),
    # -- 门限 / 坐标
    ("sidewall_threshold", "threshold", "侧墙阈值"),
    ("trackcoor", "threshold", "settrack 坐标系数"),
    # -- 速度 / 里程整定
    ("keep_dis", "motion", "保持距离"),
    ("speed_ss", "motion", "直行速度"),
    ("speed_turn", "motion", "转弯速度"),
    ("speed_fastss", "motion", "快速直行速度"),
    ("accdistance", "motion", "加速距离"),
    ("decdistance", "motion", "减速距离"),
    ("turn_delay", "motion", "转弯延时"),
    # -- 直行 / 转弯 PID
    ("GOSTRAIGHT_kp", "motion", "gostraight 比例"),
    ("SMARTSTRAIGHTFAST_bigerror_kp", "motion", "SMART 直行大误差比例"),
    ("SMARTSTRAIGHTFAST_smallerror_kp", "motion", "SMART 直行小误差比例"),
    ("STRAIGHT45_kp", "motion", "45 度直行比例"),
    ("STRAIGHT45_kd", "motion", "45 度直行微分"),
    ("turn_kp", "motion", "转弯比例"),
    ("turn_kd", "motion", "转弯微分"),
    # -- 冲刺 90 / 陀螺
    ("SPURT90IR_kp1", "motion", "冲刺90 IR 比例1"),
    ("SPURT90IR_kp2", "motion", "冲刺90 IR 比例2"),
    ("SPURT90IR_kd", "motion", "冲刺90 IR 微分"),
    ("SPURT90GYRO_kp", "motion", "冲刺90 陀螺比例"),
    ("SPURT90GYRO_kd", "motion", "冲刺90 陀螺微分"),
    # -- 电机 PID (L1=timer.c:75 / L2=user.c / L3=link.c 镜像)
    ("kpl", "motor_pid", "左轮比例"),
    ("kil", "motor_pid", "左轮积分"),
    ("kdl", "motor_pid", "左轮微分"),
    ("kpr", "motor_pid", "右轮比例"),
    ("kir", "motor_pid", "右轮积分"),
    ("kdr", "motor_pid", "右轮微分"),
    # -- 速度剖面 (UTILITY/utility.c)
    ("L45TopSpeed", "speed_profile", "45 度冲刺顶速"),
    ("L45BottomSpeed", "speed_profile", "45 度冲刺底速"),
    ("LTopSpeed", "speed_profile", "直行顶速"),
    ("LBottomSpeed", "speed_profile", "直行底速"),
    ("L90TopSpeed", "speed_profile", "90 度冲刺顶速"),
    ("L90BottomSpeed", "speed_profile", "90 度冲刺底速"),
    ("BackTopSpeed", "speed_profile", "回程顶速"),
    ("BackBottomSpeed", "speed_profile", "回程底速"),
    ("ObjectGoTo_TopSpeed", "speed_profile", "去目标顶速"),
    ("ObjectGoTo_BottomSpeed", "speed_profile", "去目标底速"),
    ("SpurtAccRatio45", "speed_profile", "45 度加速比"),
    ("SpurtDecRatio45", "speed_profile", "45 度减速比"),
    ("SpurtAccRatio90", "speed_profile", "90 度加速比"),
    ("SpurtDecRatio90", "speed_profile", "90 度减速比"),
    # -- 红外卡尔曼 (HARDWARE/ADC/adc.c)
    ("Qf1", "kalman", "f1 过程方差"),
    ("Pf1", "kalman", "f1 测量方差"),
    ("Qf2", "kalman", "f2 过程方差"),
    ("Pf2", "kalman", "f2 测量方差"),
    ("Ql", "kalman", "左过程方差"),
    ("Pl", "kalman", "左测量方差"),
    ("Qr", "kalman", "右过程方差"),
    ("Pr", "kalman", "右测量方差"),
    ("f1", "kalman", "f1 观测初值"),
    ("covf1", "kalman", "f1 协方差初值"),
    ("f2", "kalman", "f2 观测初值"),
    ("covf2", "kalman", "f2 协方差初值"),
    ("l", "kalman", "左观测初值"),
    ("covl", "kalman", "左协方差初值"),
    ("r", "kalman", "右观测初值"),
    ("covr", "kalman", "右协方差初值"),
    # -- 陀螺卡尔曼 (HARDWARE/TIMER/timer.c:85)
    ("g", "kalman", "陀螺观测初值"),
    ("covg", "kalman", "陀螺协方差初值"),
    ("Q", "kalman", "陀螺过程方差"),
    ("P", "kalman", "陀螺测量方差"),
    # -- 开关型 (Q3: 纳入, 单列 group)
    ("kalmanswitch", "switch", "红外卡尔曼总开关"),
    ("EMERGENCYON", "switch", "急停开关"),
    ("irswitch", "switch", "红外开关"),
    ("gyroswitch", "switch", "陀螺开关"),
]

# 扫描范围: 比赛侧(含 test.c 的 main 状态机里的临时覆盖)。
SCAN_FILES = (
    "USER/user.c",
    "USER/test.c",
    "HARDWARE/TIMER/timer.c",
    "UTILITY/utility.c",
    "HARDWARE/ADC/adc.c",
    "STRATAGY/stratagy.c",
)
# 远程侧只读登记, 不写回(Q1: 冲突时舍弃远程档)。
REMOTE_FILES = ("REMOTE/link.c", "REMOTE/link_port.c")

#: 函数名 → 时期前缀(其余用函数名本身)
FN_PREFIX = {
    "parameter_search": None,      # 见 PHASE_SIMPLE
    "parameter_spurt": None,
    "spurt": None,
    "test_spurt90": None,
    "mouseturn": None,
    "taskstraight90": "s90",
    "taskstraight45": "s45",
    "beforemouseturn": "wall45",
    "before180turn": "t180",
    "main": "main",
}
PHASE_SIMPLE = {
    "parameter_search": "search",
    "parameter_spurt": "spurt",
    "test_spurt90": "menu",
    "mouseturn": "mouse",
}
#: default_phase 的挑选优先级(方案 §4.2: 默认 L2=搜索/冲刺期, 退化到 L1=init)
DEFAULT_PRIORITY = ("search", "spurt", "init")

_RE_DECL = re.compile(rb"^[ \t]*(?:const[ \t]+)?(?:unsigned[ \t]+|signed[ \t]+)?"
                      rb"(u8|u16|u32|s8|s16|s32|int|long|short|char|float|double)"
                      rb"[ \t]+([^;]+);[ \t\r]*$")
_RE_DECLATOR = re.compile(rb"(?:^|,)[ \t]*([A-Za-z_]\w*)[ \t]*(?:=[ \t]*([^,;]+))?")
_RE_CTRL = re.compile(rb"^[ \t]*(if|else|for|while|switch|case|do|default)\b")
#: 函数签名(捕获函数名)。注意 regexlib.RE_FUNC_SIG 是 `%s` 模板, 不能直接用。
_RE_FUNC = re.compile(rb"^[ \t]*(?:static[ \t]+)?[A-Za-z_][\w \t\*]*\b"
                      rb"([A-Za-z_]\w*)[ \t]*\([^;]*\)[ \t\r]*$")


# ================================================================ 工具
def _norm(b: bytes) -> str:
    return re.sub(rb"\s+", b"", b).decode("gbk", "replace")


def _code(line: bytes) -> bytes:
    """代码段 = 去掉 `//` 行注释与 `/*...*/` 块注释之后的部分(供匹配用)。"""
    i = line.find(b"//")
    s = line if i < 0 else line[:i]
    return re.sub(rb"/\*.*?\*/", b"", s)


def _is_const_expr(expr: str) -> bool:
    return bool(R.RE_SAFE_EXPR.match(expr.strip()))


def assign_pat(symbol: str):
    return re.compile(rb"(?<![A-Za-z0-9_.])" + re.escape(symbol.encode()) +
                      rb"[ \t]*=(?!=)[ \t]*([^,;\r\n]+)")


# ================================================================ 结构索引
class Index:
    """每文件一次: 括号深度 / 函数区间 / 门控。"""

    def __init__(self, reg: Registry):
        self.reg = reg
        self._fns: dict = {}
        self._scope: dict = {}
        self._ctrl: dict = {}

    def functions(self, rel: str) -> list:
        """[(start0, end0, name)] —— start0 是签名行索引, end0 是收尾 `}` 索引。"""
        if rel in self._fns:
            return self._fns[rel]
        out, cur, depth = [], None, 0
        for i, lb in enumerate(self.reg.lines(rel)):
            code = _code(lb)
            if cur is None:
                m = _RE_FUNC.match(code)
                if m and not code.rstrip().endswith(b";") and b"=" not in code.split(m.group(1))[0]:
                    cur = (i, D(m.group(1)))
                    depth = 0
            if cur is not None:
                depth += code.count(b"{") - code.count(b"}")
                if depth <= 0 and i > cur[0]:
                    out.append((cur[0], i, cur[1]))
                    cur = None
        self._fns[rel] = out
        return out

    def file_scope(self, rel: str) -> list:
        """逐行 bool: 是否在文件作用域(深度 0)。"""
        if rel in self._scope:
            return self._scope[rel]
        out, depth = [], 0
        for lb in self.reg.lines(rel):
            code = re.sub(rb"/\*.*?\*/", b"", _code(lb))
            out.append(depth == 0)
            depth += code.count(b"{") - code.count(b"}")
            if depth < 0:
                depth = 0
        self._scope[rel] = out
        return out

    def func_of(self, rel: str, line0: int) -> str:
        for s, e, n in self.functions(rel):
            if s <= line0 - 1 <= e:
                return n
        return "<file>"

    def ctrl_of(self, rel: str, line0: int) -> str:
        """最近的上文控制语句(取自 code 段, 便于人读时期)。"""
        if rel not in self._ctrl:
            self._ctrl[rel] = [i + 1 for i, l in enumerate(self.reg.lines(rel))
                               if _RE_CTRL.match(_code(l))]
        prev = [n for n in self._ctrl[rel] if n < line0]
        if not prev:
            return ""
        return _code(self.reg.lines(rel)[prev[-1] - 1]).strip().decode("gbk", "replace")[:80]

    def gate_of(self, rel: str, line0: int) -> str:
        if rel.startswith("REMOTE/"):
            return "MOUSE_REMOTE"
        return self.reg.region_map(rel)[line0 - 1]

    def gate_kind(self, rel: str, line0: int) -> str:
        if rel.startswith("REMOTE/"):
            return "remote"
        terms = [t.strip() for t in self.gate_of(rel, line0).split("&&") if t.strip()]
        if any(t == "!MOUSE_REMOTE" for t in terms):
            return "competition"
        if any(t == "MOUSE_REMOTE" for t in terms):
            return "remote"
        return "both"


# ================================================================ 时期
class Phaser:
    """给 (文件, 行, 函数, 符号) 定一个时期名。同函数多处按符号内序号区分。"""

    def __init__(self, idx: Index):
        self.idx = idx
        self._counter: dict = {}          # (rel, fn, sym) -> 下一个序号

    def phase(self, rel: str, line0: int, fn: str, sym: str) -> str:
        if fn == "<file>":
            return "init"
        if fn == "spurt":
            for j in range(line0 - 1, max(0, line0 - 40), -1):
                t = _norm(self.idx.reg.lines(rel)[j])
                if "mode==LOW" in t or "speedmode==LOW" in t:
                    return "spurt.low"
                if "mode==MEDIUM" in t or "speedmode==MEDIUM" in t:
                    return "spurt.medium"
                if "mode==HIGH" in t or "speedmode==HIGH" in t:
                    return "spurt.high"
            return "spurt"
        if fn in PHASE_SIMPLE:
            return PHASE_SIMPLE[fn]
        prefix = FN_PREFIX.get(fn, fn)
        if prefix is None:
            prefix = fn
        key = (rel, fn, sym)
        n = self._counter.get(key, 0) + 1
        self._counter[key] = n
        return "%s#%d" % (prefix, n)


# ================================================================ 构建
def build(root: str) -> dict:
    reg = Registry(root)
    idx = Index(reg)
    ph = Phaser(idx)
    _GROUP_DESC = {
        "calib": "标定系数（编码器 / 陀螺 / 距离）",
        "threshold": "门限 / 坐标",
        "motion": "运动整定（速度 / PID / 距离）",
        "motor_pid": "电机 PID（L1 timer.c / L2 user.c）",
        "speed_profile": "速度剖面（顶速 / 底速 / 加减速比）",
        "kalman": "红外 + 陀螺卡尔曼",
        "switch": "开关型",
    }
    groups: "OrderedDict" = OrderedDict()
    for _n, _g, _d in TUNABLES:
        groups.setdefault(_g, []).append(_n)
    params, gaps = [], []

    for name, group, note in TUNABLES:
        rec = {
            "name": name, "group": group, "note": note,
            "sets": [], "derived": [], "dropped": [], "assigners": [],
        }
        seen_sites = set()
        # ---- 声明点(init) ----
        for rel in SCAN_FILES + REMOTE_FILES:
            if not reg.exists(rel):
                continue
            lines = reg.lines(rel)
            scope = idx.file_scope(rel)
            for i, lb in enumerate(lines):
                if not scope[i]:
                    continue
                m = _RE_DECL.match(_code(lb))
                if not m:
                    continue
                decls = list(_RE_DECLATOR.finditer(m.group(2)))
                hit = [d for d in decls if D(d.group(1)) == name]
                if not hit:
                    continue
                d = hit[0]
                expr = (D(d.group(2)).strip() if d.group(2) else "")
                line0 = i + 1
                seen_sites.add((rel, line0))
                kind = idx.gate_kind(rel, line0)
                ctype = D(m.group(1))
                entry = {
                    "phase": "init", "kind": "decl" if expr else "bss",
                    "value": _num(expr), "expr": expr,
                    "file": rel, "line": line0,
                    "sha16": sha256(lb)[:16],
                    "gate": idx.gate_of(rel, line0), "ctype": ctype,
                    # 一行多声明符: 只有首个带类型前缀(`double a=1,b=2;`)
                    "first": (d.start() == decls[0].start()),
                    "writable": bool(expr),
                }
                if kind == "remote":
                    entry["reason"] = "profile: remote-only, keep competition"
                    rec["dropped"].append(entry)
                elif expr:
                    rec["sets"].append(entry)
                else:
                    # BSS 无初值: 没有可写锚点, 只登记(方案 §2.3「无 init」)
                    rec.setdefault("bss", []).append({"file": rel, "line": line0,
                                                      "gate": idx.gate_of(rel, line0)})
        # ---- 赋值点 ----
        pat = assign_pat(name)
        for rel in SCAN_FILES + REMOTE_FILES:
            if not reg.exists(rel):
                continue
            lines = reg.lines(rel)
            for i, lb in enumerate(lines):
                line0 = i + 1
                if (rel, line0) in seen_sites:
                    continue
                code = _code(lb)
                if not pat.search(code):
                    continue
                if reg.is_decl_line(rel, i, name):
                    continue
                # 声明行但符号无初值(如 `float A,B;` 里 B 出现在别处) —— 交给上面
                ms = list(pat.finditer(code))
                if not ms:
                    continue
                expr = D(ms[0].group(1)).strip()
                fn = idx.func_of(rel, line0)
                phase = ph.phase(rel, line0, fn, name)
                const = _is_const_expr(expr)
                kind = idx.gate_kind(rel, line0)
                entry = {
                    "phase": phase,
                    "kind": ("assign" if const else "derived"),
                    "value": _num(expr) if const else None,
                    "expr": expr,
                    "file": rel, "line": line0,
                    "sha16": sha256(lb)[:16],
                    "gate": idx.gate_of(rel, line0),
                    "fn": fn,
                    "context": idx.ctrl_of(rel, line0),
                    "writable": const and len(ms) == 1,
                }
                if const and len(ms) != 1:
                    entry["why_not"] = "line-not-unique: 该行 `%s =` 出现 %d 次" % (name, len(ms))
                if fn not in rec["assigners"]:
                    rec["assigners"].append(fn)
                if kind == "remote":
                    entry["reason"] = "profile: remote-only, keep competition"
                    rec["dropped"].append(entry)
                elif const:
                    rec["sets"].append(entry)
                else:
                    rec["derived"].append(entry)

        rec["sets"].sort(key=lambda s: (s["file"], s["line"]))
        rec["derived"].sort(key=lambda s: (s["file"], s["line"]))
        rec["dropped"].sort(key=lambda s: (s["file"], s["line"]))
        rec["default_phase"] = _pick_default(rec["sets"])
        dp = next((s for s in rec["sets"] if s["phase"] == rec["default_phase"]), None)
        rec["default_value"] = dp["value"] if dp else None
        rec["default_expr"] = dp["expr"] if dp else None
        rec["status"] = "ok" if rec["sets"] else "no-set"
        if not rec["sets"]:
            gaps.append({"name": name, "kind": "no-const-set",
                         "detail": "没有可写常量套(全是 derived / bss / dropped)"})
        params.append(rec)

    phases = sorted({s["phase"] for p in params for s in p["sets"]} |
                    {d["phase"] for p in params for d in p["derived"]})
    return {
        "schema_version": 3,
        "profile": "competition",
        "firmware": {
            "root": "code_competition",
            "redline_sha256": REDLINE,
            "link_p_num": reg.link_p_num(),
        },
        "phases": [{"id": p, "desc": PHASE_DESC.get(p, p)} for p in phases],
        "groups": [{"id": g, "name": g, "desc": _GROUP_DESC.get(g, g), "params": v}
                   for g, v in groups.items()],
        "params": params,
        "gaps": gaps,
    }


REDLINE = "68ad6be2272818269d6aac78e5356d9bb91036ddeaaa82c4ec3754a8a0948141"

PHASE_DESC = {
    "init": "上电初值（编译期初始化式）",
    "search": "搜索期 parameter_search()",
    "spurt": "冲刺/回程期 parameter_spurt()",
    "spurt.low": "spurt(LOW)",
    "spurt.medium": "spurt(MEDIUM)",
    "spurt.high": "spurt(HIGH)",
    "menu": "按键测试菜单 test_spurt90()",
    "mouse": "鼠转后 mouseturn()",
}


def _pick_default(sets: list) -> str | None:
    if not sets:
        return None
    by = {s["phase"] for s in sets}
    for p in DEFAULT_PRIORITY:
        if p in by:
            return p
    return sets[0]["phase"]


def _num(expr: str):
    from .registry import eval_expr
    if not expr:
        return None
    return eval_expr(expr)


# ================================================================ 写回
def plan(reg: Registry, params, names=None) -> tuple:
    """多套写回计划。→ (edits, problems); edits = {(file,line): {sym: new_text}}"""
    edits, problems = {}, []
    for p in params:
        if names and p["name"] not in names:
            continue
        for s in p["sets"]:
            if not s.get("writable", True):
                problems.append((p["name"], "skip",
                                 "%s %s:%d %s" % (s["phase"], s["file"], s["line"],
                                                  s.get("why_not", "不可写"))))
                continue
            new = s.get("expr") if s.get("expr") not in (None, "") else s.get("value")
            if new is None:
                problems.append((p["name"], "no-value", s["phase"]))
                continue
            rel, lineno = s["file"], int(s["line"])
            if not reg.exists(rel):
                problems.append((p["name"], "no-file", rel))
                continue
            lines = reg.lines(rel)
            if not (1 <= lineno <= len(lines)):
                problems.append((p["name"], "bad-line", "%s:%d" % (rel, lineno)))
                continue
            line = lines[lineno - 1]
            got = sha256(line)[:16]
            if s.get("sha16") and got != s["sha16"]:
                problems.append((p["name"], "anchor-drift",
                                 "%s:%d %s != %s" % (rel, lineno, got, s["sha16"])))
                continue
            sym = p["name"]
            code = P.code_span(line)
            if len(R.value_span(sym).findall(line[:code])) != 1:
                problems.append((p["name"], "line-not-unique",
                                 "%s:%d `%s =` 不唯一" % (rel, lineno, sym)))
                continue
            if s["kind"] == "decl" and s.get("first", True) and s.get("ctype"):
                if not R.line_anchor(sym, s["ctype"]).match(line):
                    problems.append((p["name"], "decl-shape",
                                     "%s:%d 不是 `%s %s = ...;`" % (rel, lineno,
                                                                    s["ctype"], sym)))
                    continue
            edits.setdefault((rel, lineno), {})[sym] = str(new)
    return edits, problems


def render_edits(reg: Registry, edits: dict) -> list:
    out = []
    for (rel, lineno), subs in sorted(edits.items()):
        line = reg.lines(rel)[lineno - 1]
        new = line
        for sym, val in subs.items():
            cand = P.set_value(new, sym, val)
            if cand is not None:
                new = cand
        if new != line:
            out.append((rel, lineno, line, new))
    return out


def commit(reg: Registry, edits: dict) -> list:
    touched, by_file = [], {}
    for (rel, lineno), subs in edits.items():
        by_file.setdefault(rel, {})[lineno] = subs
    for rel, linesubs in by_file.items():
        raw = reg.raw(rel)
        lines = list(split_lines(raw))
        for lineno, subs in linesubs.items():
            line = lines[lineno - 1]
            for sym, val in subs.items():
                cand = P.set_value(line, sym, val)
                if cand is not None:
                    line = cand
            lines[lineno - 1] = line
        new = b"\n".join(lines)
        if new == raw:
            continue
        write_atomic(reg.path(rel), new)
        touched.append(rel)
    return touched
