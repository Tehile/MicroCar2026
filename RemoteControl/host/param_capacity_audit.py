"""容量复测：`GET ALL` 41 项 × 固件编译期真值（远程档口径）逐项对账。

判据与口径
----------
* **真源** = `tools/params/params.competition.v3.json`（由 `paramgen extract3`
  从比赛档源码生成，hex 红线 `68ad6be2…8141` 锁定）。
* 本机跑的是**远程档**（`MOUSE_REMOTE`）固件，故期望值按 `sets[*].gate` 挑：
  有 `gate` 含 `MOUSE_REMOTE`（且非 `!MOUSE_REMOTE`）的 init 声明就用它，
  否则用无门控 / `!MOUSE_REMOTE` 的那条。
* 在线参数名（`TRACKL`）与 JSON 参数名（`TrackRatioL`）的映射取自
  `params.competition.json` 的 `arbitration.sink`。

判定分档
--------
| 标记 | 含义 |
|---|---|
| `=` | 与编译期真值一致（相对误差 < 1e-6 或恰好相等） |
| `B` | 仲裁 class B：Flash `store` 覆盖固件默认，**允许**与编译期值不同 |
| `C` | 仲裁 class C：该参数在远程档 `dropped`，永不写回，**允许**不同 |
| `!A` | class A（固件为真源，线上值应镜像固件变量）却与真值不符 ⇒ **需查** |

用法::

    python param_capacity_audit.py COM7
    python param_capacity_audit.py COM7 --json out.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, ".")

ROOT = pathlib.Path(__file__).resolve().parent.parent
V3 = ROOT / "tools" / "params" / "params.competition.v3.json"
V2 = ROOT / "tools" / "params" / "params.competition.json"

REL_TOL = 1e-6


def load_v3() -> dict[str, dict]:
    d = json.loads(V3.read_text(encoding="utf-8"))
    return {p["name"].lower(): p for p in d["params"]}


def pick_expected(p: dict, remote: bool = True) -> tuple[float | None, str, str]:
    """从 v3 参数条目里挑出当前档位的 init 值。返回 (value, expr, 说明)。"""
    cands = [s for s in p.get("sets", []) if s.get("phase") == "init"]
    if not cands:
        return None, "", "无 init 声明"

    def gate_of(s: dict) -> str:
        return str(s.get("gate") or "")

    want, other = ("MOUSE_REMOTE", "!MOUSE_REMOTE") if remote else ("!MOUSE_REMOTE", "MOUSE_REMOTE")
    mine = [s for s in cands if want in gate_of(s) and other not in gate_of(s)]
    if mine:
        s = mine[0]
        return s.get("value"), s.get("expr") or "", f"gate={want} {s.get('file','')}"
    rest = [s for s in cands if other not in gate_of(s)]
    if rest:
        s = rest[0]
        return s.get("value"), s.get("expr") or "", f"无门控 {s.get('file','')}"
    s = cands[0]
    return s.get("value"), s.get("expr") or "", "仅对档门控"


def close(a: float, b: float) -> bool:
    if a is None or b is None:
        return False
    if a == b:
        return True
    den = max(abs(a), abs(b), 1e-12)
    return abs(a - b) / den < REL_TOL


def main() -> int:
    ap = argparse.ArgumentParser(prog="param_capacity_audit.py")
    ap.add_argument("port", nargs="?", default="COM7")
    ap.add_argument("--json", dest="dump", default="", help="把对账结果写成 JSON")
    args = ap.parse_args()

    from host.api import MouseClient
    from host.protocol.commands import PARAM_IDS

    v3 = load_v3()
    v2 = {p["name"]: p for p in json.loads(V2.read_text(encoding="utf-8"))["params"]}

    mc = MouseClient(auto_session=False, auto_stream=False)
    if not mc.connect(args.port):
        print("连接失败:", mc.state_detail)
        return 1
    try:
        mc.send_cmd("ping", timeout=5.0)
        print(f"设备: proto={mc.device.get('proto')} fw={mc.device.get('fw')} "
              f"mazetype={mc.device.get('mazetype')} rst={mc.reset_cause}")
        live = mc.params.get_all()
        if len(live) < len(PARAM_IDS):
            print(f"⚠ GET ALL 只回 {len(live)}/{len(PARAM_IDS)} 项")
        print(f"GET ALL 返回 {len(live)} 项\n")

        rows, bad, allow = [], [], []
        hdr = f"{'id':>3} {'name':<11}{'live':>15}{'expect(remote)':>16}{'Δ':>12}  arb  判定"
        print(hdr)
        print("-" * len(hdr))
        for name in sorted(PARAM_IDS, key=lambda x: PARAM_IDS[x]):
            pid = PARAM_IDS[name]
            cur = live.get(name)
            meta = v2.get(name, {})
            sink = (meta.get("arbitration") or {}).get("sink") or ""
            klass = (meta.get("arbitration") or {}).get("class") or "?"
            p3 = v3.get(sink.lower()) if sink else None

            exp, expr, why = (None, "", "v3 无此参数")
            if p3:
                exp, expr, why = pick_expected(p3, remote=True)
            if exp is None:
                exp = (meta.get("fw") or {}).get("value")

            delta = None if (cur is None or exp is None) else cur - exp
            if cur is None:
                tag, judge = "!", "线上缺失"
            elif close(cur, exp):
                tag, judge = "=", "一致"
            elif klass.upper() == "B":
                tag, judge = "B", "store 覆盖(允许)"
                allow.append(name)
            elif klass.upper() == "C":
                tag, judge = "C", "远程档 dropped(允许)"
                allow.append(name)
            else:
                tag, judge = "!A", "class A 与真值不符 ⇒ 需查"
                bad.append(name)

            ds = "-" if delta is None else f"{delta:+.6f}"
            es = "-" if exp is None else f"{exp:.6f}"
            cs = "-" if cur is None else f"{cur:.6f}"
            print(f"{pid:>3} {name:<11}{cs:>15}{es:>16}{ds:>12}  {klass:<4} {tag} {judge}")

        print("\n" + "=" * 72)
        print(f"合计 {len(PARAM_IDS)} 项：一致 {len(PARAM_IDS)-len(allow)-len(bad)} / "
              f"允许偏差 {len(allow)} / 需查 {len(bad)}")
        if allow:
            print("允许偏差:", ", ".join(allow))
            for n in allow:
                meta = v2.get(n, {})
                sink = (meta.get("arbitration") or {}).get("sink") or ""
                p3 = v3.get(sink.lower())
                exp, expr, why = pick_expected(p3, remote=True) if p3 else (None, "", "")
                print(f"   {n:<11} live={live.get(n)} expect={exp} expr={expr} ({why})")
        if bad:
            print("⚠ 需查:", ", ".join(bad))
        else:
            print("✅ 无 class A 偏差")

        if args.dump:
            pathlib.Path(args.dump).write_text(
                json.dumps({"device": mc.device, "live": live,
                            "need_check": bad, "allowed": allow},
                           ensure_ascii=False, indent=1), encoding="utf-8")
            print("已写出", args.dump)
        return 0
    finally:
        try:
            mc.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
