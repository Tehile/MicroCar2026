"""急停/停止的实际生效延迟测量 —— 重点验证"拉取器是否与急停抢链路"。

## 为什么怀疑拉取器
固件 `link.c` 明确: **每 1ms tick 只消费一行命令**(`if (USART_RX_STA & 0x80) {...}`;
bit7 未清时 `usart.c` 把后续到达的字节**直接丢弃**)。而 v2.1 的拉取器
(`PullRequester`)是**持续**发 `TELEM` 的 —— 于是同时下发的 `ESTOP` 只要与某条
`TELEM` 落进同一个 1ms 窗口, 就会被**丢掉**; 主机要等 `TIMEOUT_S`(2.5s) 才重发,
现场表现就是"**急停不立即生效**"。

固件侧本身极快: `isr.c` 里 `emergMon.active==1` 时**每个 1ms 周期**都把
`speedTargetL/R` 清零并置 `freemode=1`。所以"不快"只可能来自链路。

## 测什么
| 组 | 条件 | 目的 |
|---|---|---|
| A | 拉取器**关** | 基线: 命令通道独占链路时的往返 |
| B | 拉取器**开** | 争用: 与 `TELEM` 抢同一 tick 的概率与后果 |
| C | 拉取器**开** | 转圈中的车, 发 `STOP` → 测 T_ack 与 T_speed0 |
| D | 拉取器**关** | 同上, 作对照 |

同时覆盖用户问过的"**重复指令能否接收**": **在拉取器开着**的情况下连发命令。

## 安全
- 一律让车**原地转**(`MOVE RUN +v -v`), **不位移** —— 避免撞墙, 也避免红外护栏
  提前触发干扰测量(红外护栏显式关闭: 末参 `-1`)。
- 车速适中(±40), 时长 3s 兜底; 每轮结束显式 `STOP` 解锁。
- **会动车**, 必须显式 `--motor`。

用法:
    python estop_latency.py COM7               # 只测 A/B（命令通道, 不动车）
    python estop_latency.py COM7 --motor -n 5  # 追加 C/D（会原地转）
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time

sys.path.insert(0, ".")


def pct(vals, p):
    if not vals:
        return float("nan")
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p))]


def line(label, vals, unit="ms"):
    if not vals:
        print(f"    {label:34s} 无数据")
        return
    print(f"    {label:34s} min {min(vals):6.0f} / 中位 {statistics.median(vals):6.0f} "
          f"/ P90 {pct(vals,.9):6.0f} / max {max(vals):6.0f} {unit}   (n={len(vals)})")


def probe_commands(mc, n: int, tag: str) -> list[float]:
    """连发 n 条 PING, 返回每条从调用到拿到应答的耗时(ms)。"""
    print(f"\n  [{tag}] 连发 {n} 条 PING —— 测命令通道的往返与丢失")
    rtts: list[float] = []
    lost = 0
    for i in range(1, n + 1):
        t0 = time.time()
        r = mc.send_cmd("ping", timeout=6.0)
        dt = (time.time() - t0) * 1000
        if r.ok:
            rtts.append(dt)
            flag = "" if dt < 800 else "   <-- 偏慢"
            print(f"      #{i:<2d} OK   {dt:7.0f} ms   attempts={r.data.get('attempts')}{flag}")
        else:
            lost += 1
            print(f"      #{i:<2d} **{r.status}**  {dt:7.0f} ms   attempts={r.data.get('attempts')}")
        time.sleep(0.15)
    print(f"      --> 成功 {len(rtts)}/{n}, 失败 {lost}")
    line("命令往返", rtts)
    return rtts


def stop_latency(mc, tag: str, trials: int, pull: bool, use_estop: bool,
                 quiesce: bool = False) -> dict:
    """原地转圈 → 发 STOP/ESTOP → 测 T_ack 与 T_speed0。

    `quiesce=True` 时在发停止命令**之前先关掉拉取器**（让命令通道独占链路）——
    用来验证"急停被 TELEM 抢链路"这一假设。
    """
    kind = "ESTOP" if use_estop else "STOP"
    print(f"\n  [{tag}] 原地转圈 + 发 {kind}"
          f"（拉取器{'开' if pull else '关'}"
          f"{' →发命令前先静默' if quiesce else ''}）")
    if pull:
        mc.stream.on()
        mc.telemetry.wait_first_frame(timeout=6.0)
    else:
        mc.stream.off()

    acks: list[float] = []
    to_zero: list[float] = []
    retries: list[int] = []
    for k in range(1, trials + 1):
        # 清场, 确保可动
        mc.send_cmd("stop", timeout=6.0)
        time.sleep(0.2)
        if pull and not quiesce:
            mc.stream.on()
        elif pull:
            mc.stream.off()
            time.sleep(0.25)   # 让在途 TELEM 排空

        # 原地转: 两轮反向 -> 不位移; ir_mm=-1 关红外护栏, ms=4000 兜底
        t_start = time.time()
        r = mc.send_cmd("move.run", sp_l=40, sp_r=-40, ms=4000, ir_mm=-1, timeout=8.0)
        if not r.ok:
            print(f"      #{k} 起转被拒: {r.status}  (跳过)")
            continue
        ack_start = (time.time() - t_start) * 1000

        # 等它转稳(同时确认真在动)
        moving = False
        t_wait = time.time()
        while time.time() - t_wait < 1.5:
            if not (pull and not quiesce):
                time.sleep(1.0)
                moving = True
                break
            f = mc.telemetry.latest()
            if f and ((f.get("speedL") or 0) or (f.get("speedR") or 0)):
                moving = True
                break
            time.sleep(0.02)

        # ---- 关键测量 ----
        if quiesce:
            # ★ 修复假设: 急停前先让拉取器静默, 命令独占链路
            mc.stream.off()
            time.sleep(0.25)
        t0 = time.time()
        rr = mc.send_cmd("estop" if use_estop else "estop.clear", timeout=8.0)
        t_ack = (time.time() - t0) * 1000
        att = int(rr.data.get("attempts") or 1)
        retries.append(att)

        # 再测"转速读到 0"的时刻(仅在拉取器开着时能看到帧)
        t_zero = None
        if pull and not quiesce:
            deadline = time.time() + 5.0
            while time.time() < deadline:
                f = mc.telemetry.latest()
                if f and (f.get("speedL") or 0) == 0 and (f.get("speedR") or 0) == 0:
                    t_zero = (time.time() - t0) * 1000
                    break
                time.sleep(0.01)
        if t_zero is None and quiesce:
            # 静默期间看不到帧, 改为重新开拉取器后读第一帧
            mc.stream.on()
            mc.telemetry.wait_first_frame(timeout=4.0)
            f = mc.telemetry.latest()
            if f and (f.get("speedL") or 0) == 0 and (f.get("speedR") or 0) == 0:
                t_zero = (time.time() - t0) * 1000

        acks.append(t_ack)
        if t_zero is not None:
            to_zero.append(t_zero)
        warn = "   <-- 偏慢!" if t_ack > 800 else ""
        retry_warn = f"  重发{att-1}次!" if att > 1 else ""
        print(f"      #{k}  起转应答 {ack_start:5.0f} ms | "
              f"{kind} 应答 {t_ack:6.0f} ms (attempts={att}){warn}{retry_warn}"
              f"{f' | 转速归 0 {t_zero:6.0f} ms' if t_zero is not None else ''}")

        mc.send_cmd("stop", timeout=6.0)   # 解锁, 进入下一轮
        time.sleep(0.2)

    print(f"      --> 起转{'未确认在动' if not moving else '正常'}")
    line(f"{kind} 应答延迟", acks)
    if to_zero:
        line("转速归 0 用时", to_zero)
    if retries:
        n_retry = sum(1 for x in retries if x > 1)
        print(f"    {kind} 需要重发的次数: {n_retry}/{len(retries)}"
              f"{'   <-- 说明命令/ACK 在链路上丢了' if n_retry else '   (无重发)'}")
    mc.stream.off()
    return {"ack": acks, "to_zero": to_zero, "retries": retries}


def main() -> int:
    ap = argparse.ArgumentParser(prog="estop_latency.py")
    ap.add_argument("port", nargs="?", default="COM7")
    ap.add_argument("-n", type=int, default=8, help="每组试验次数")
    ap.add_argument("--motor", action="store_true", help="追加 C/D（会让车原地转!）")
    args = ap.parse_args()

    from host.api import MouseClient

    print("=" * 78)
    print(f"急停/停止 生效延迟测量   端口={args.port}"
          f"{'  【会原地转!】' if args.motor else '  (不动车)'}")
    print("=" * 78)

    mc = MouseClient(auto_session=False, auto_stream=False)
    if not mc.connect(args.port):
        print("连接失败:", mc.state_detail)
        return 1

    try:
        r = mc.send_cmd("ping", timeout=5)
        print(f"\n[0] PING {r.ok}  {mc.device}")
        if not r.ok:
            return 1
        info = mc.stream.stats()
        print(f"    拉取器: {info}")

        # ---------------- A/B: 命令通道在拉取器开/关下的差异 ----------------
        mc.stream.off()
        time.sleep(0.3)
        a = probe_commands(mc, args.n, "A 拉取器关")

        mc.stream.on()
        mc.telemetry.wait_first_frame(timeout=6.0)
        st = mc.stream.stats()
        print(f"    （拉取器已开: {st}）")
        b = probe_commands(mc, args.n, "B 拉取器开")
        mc.stream.off()
        time.sleep(0.3)

        # ---------------- C/D: 转圈中停止的延迟 ----------------
        c = d = f_ = e = {}
        if args.motor:
            print(f"\n[3] 转圈中停止 —— 对照实验")
            d = stop_latency(mc, "D 拉取器关", args.n, pull=False, use_estop=False)
            c = stop_latency(mc, "C 拉取器开", args.n, pull=True, use_estop=False)
            f_ = stop_latency(mc, "F 拉取器开+发命令前静默", args.n,
                              pull=True, use_estop=False, quiesce=True)
            e = stop_latency(mc, "E 拉取器开+ESTOP+静默", max(2, args.n // 2),
                             pull=True, use_estop=True, quiesce=True)

        # ---------------- 汇总 ----------------
        print("\n" + "=" * 78)
        print("结论")
        print("=" * 78)
        def med(v):
            return statistics.median(v) if v else float("nan")
        if a and b:
            print(f"    命令往返中位:  拉取器关 {med(a):.0f} ms   拉取器开 {med(b):.0f} ms")
            print(f"    成功条数:      拉取器关 {len(a)}/{args.n}   拉取器开 {len(b)}/{args.n}")
            slow = [x for x in b if x > 800]
            if slow or len(b) < args.n:
                print(f"    ⚠️ 拉取器开着时有 {len(slow)} 条明显变慢 / {args.n-len(b)} 条失败")
                print("       => 疑似 `TELEM` 与命令撞进同一 1ms tick 而被丢弃(见文件头注释)")
            else:
                print("    ✅ 拉取器未对命令通道造成可观测影响")
        if d.get("ack") and c.get("ack"):
            print(f"    STOP 应答中位: 拉取器关 {med(d['ack']):.0f} ms   "
                  f"拉取器开 {med(c['ack']):.0f} ms"
                  f"   开+静默 {med(f_['ack']) if f_.get('ack') else float('nan'):.0f} ms")
        if c.get("to_zero"):
            print(f"    ★ 从发 STOP 到**实测转速归 0**: 中位 {med(c['to_zero']):.0f} ms"
                  f"  (P90 {pct(c['to_zero'],.9):.0f} ms)")
            print("      注: 固件侧 <=1ms 就清零了目标速度, 所以这些时间全是链路 + 电机惯性;")
            print("          **若接近 3000/4000 ms = 命令没生效, 车是撞时间护栏停的**")
        if f_.get("ack"):
            print(f"    ★ 发命令前先静默拉取器 → STOP 应答中位 {med(f_['ack']):.0f} ms"
                  f"  (对照'拉取器开' {med(c['ack']):.0f} ms)")
        for tag, r in (("拉取器关", d), ("拉取器开", c), ("开+静默", f_), ("ESTOP+静默", e)):
            rt = r.get("retries") or []
            if rt:
                print(f"    重发情况 [{tag}]: {sum(1 for x in rt if x > 1)}/{len(rt)} 次需要重发")
        return 0
    finally:
        try:
            mc.send_cmd("stop", timeout=4.0)
        except Exception:
            pass
        try:
            mc.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
