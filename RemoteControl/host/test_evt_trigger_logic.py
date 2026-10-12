# -*- coding: utf-8 -*-
"""事件触发式拉取的**离线逻辑自测**(不连设备, 不发任何命令)。

测 `link_pull_evt.classify_gap` 这条判据 + 由它闭出的自锁环路, 覆盖:

 1. **自锁速度律**: 每周期实发帧数 N = ceil(RTT/t), 到达速率 = N/RTT ≈ 1/t + 1/RTT;
    每个周期**恰好**在批起点检出一次边界, 且检出值恒 = RTT − (N−1)·t = RTT mod t。
 2. **退化死角**: RTT ≡ 0 (mod t) 时批起点间隔恰好 = t ⇒ 帧判据**零次触发**, 环路停摆;
    ACK 判据对同一批参数全部正常 ⇒ 这就是默认判据取 auto(ACK 为主) 的理由。
 3. **丢帧不误判**: 批内丢一帧 ⇒ ms8 间隔变长(2t) ⇒ 必须归 "loss" 而非批起点(否则误发命令);
    若丢的正好是紧邻批起点之前那帧, 批起点也变成 2t ⇒ 该周期按设计漏触发(靠 --max-wait-ms 兜底)。

正确的批结构(第一版仿真在这里错过, 记下来免得重犯):
    边界帧 = **下一批的第 1 帧**, 不是本批内的帧; 本批发满 N 帧后才轮到它。
    T 时刻发命令 -> 到达单片机 T+d -> 立即发 F1 -> F1 到达 PC = T+2d = T+RTT
    => PC 在收到 F1 的那一刻发下一条命令, 于是**周期恒 = RTT**(自锁)。

用法: python test_evt_trigger_logic.py        (退出码 0 = 全通过)
"""
from __future__ import annotations

import collections
import math
import statistics
import sys

from link_pull_evt import classify_gap, unwrap_rtt, RTT_QUANT

TOL = 0.75


# --------------------------------------------------------------- 判据单元测试
def test_classify() -> None:
    cases = [
        # (m8, prev_m8, t, 期望 kind, 期望 d, 说明)
        (200, 117, 83, "in", 83.0, "批内: 精确 t"),
        (10, 183, 83, "in", 83.0, "批内: 183+83=266 -> 10 (ms8 折回)"),
        (27, 200, 83, "in", 83.0, "批内: 200+83=283 -> 27 (ms8 折回)"),
        (0, 200, 83, "bound", 56.0, "批起点: 56ms 短间隔(200+56=256 -> 0)"),
        (100, 100, 83, "bound", 0.0, "批起点: 间隔 0(命令与上批末帧同刻到达)"),
        (160, 78, 83, "bound", 82.0, "批起点: 82ms, 比 t 只短 1ms —— 仍须检出"),
    ]
    for m8, p, t, want_kind, want_d, note in cases:
        kind, d = classify_gap(m8, p, t, TOL)
        assert kind == want_kind, (m8, p, t, kind, want_kind, note)
        assert abs(d - want_d) < 0.6, (m8, p, d, want_d, note)
    print("  ✓ 6 项判据用例全通过(含两种 ms8 折回、间隔 0、比 t 只短 1ms)")

    kind, d = classify_gap(117, 34, 83, TOL)          # 34 -> 117 恰为 t
    assert kind == "in" and abs(d - 83) < 0.6, (kind, d)
    print("  ✓ 容差方向: 34->117 恰为 83ms, 判为 in(不会误触发)")

    kind, d = classify_gap(200, 34, 83, TOL)          # 34 -> 200 = 166 = 2t
    assert kind == "loss" and abs(d - 166) < 0.6, (kind, d)
    print("  ✓ 34->200 = 166ms = 2t 判为 loss(丢帧), 不是批起点")


# --------------------------------------------------------------- 闭环仿真
def simulate(rtt: int, t: int, n: int = 16, mode: str = "frame",
             ack_tx: int = 2, want_periods: int = 200, drop=None):
    """按正确的批结构复现闭环。

    每批恰发 N = min(n, ceil(RTT_eff/t)) 帧(超出部分被新命令抢占, **从未发出**),
    批内间隔恒 = t, 批起点间隔 = RTT_eff − (N−1)·t。
      * mode="frame": 以 ms8 间隙判据触发;
      * mode="ack"  : 以本周期 PULL 的 ACK 触发, 周期 = RTT + ack_tx。
    drop(period, j) -> True 表示该帧发出但丢失(不参与判据)。
    返回 dict(bounds, losses, stalls, periods, frames)。
    """
    d = rtt // 2
    eff = rtt + (ack_tx if mode == "ack" else 0)
    N = min(n, -(-eff // t))                      # ceil(eff/t)
    if N <= 0:
        return {"bounds": [], "losses": [], "stalls": 1, "periods": 0, "frames": 0}

    A = eff + d                                   # 第一条命令(0 时刻发)的到达 + 一次 bootstrap
    prev_m8 = None
    bounds: list[float] = []
    losses: list[float] = []
    frames = 0
    stalls = 0
    # p == 0 是**自举周期**: 它前面没有可比较的帧, 检不出边界, 必须靠外部起步
    # (真实工具用 --trigger frame 的 boot / 或 ACK 判据)。真正计时从 p >= 1 开始。
    for p in range(want_periods + 1):
        base = A
        A_next = None
        fired = (p == 0) or (mode == "ack")
        if mode == "ack" and p > 0:
            bounds.append(float("nan"))
        for j in range(N):
            e = base + j * t
            if drop is not None and drop(p, j):
                continue                          # 丢失: 不计入判据, 也不更新 prev_m8
            m8 = e & 0xFF
            frames += 1
            if mode == "frame" and prev_m8 is not None:
                kind, dv = classify_gap(m8, prev_m8, t, TOL)
                if kind == "bound":
                    if j == 0:
                        fired = True
                        bounds.append(dv)
                    else:
                        bounds.append(-abs(dv))   # 负数 = 非批起点处检出边界(应永不发生)
                    A_next = e + rtt              # 触发 -> 下一条命令在本帧到达 PC 后 RTT 到达
                elif kind == "loss":
                    losses.append(dv)
            prev_m8 = m8
        if not fired:
            stalls += 1
            break
        if mode == "ack":
            A = base + eff                        # ACK 判据: 周期 = RTT + ack_tx(自锁于另一常数)
        else:
            A = A_next if A_next is not None else base + rtt
    return {"bounds": bounds, "losses": losses, "stalls": stalls,
            "periods": len(bounds), "frames": frames}


def test_speed_law() -> None:
    print("自锁速度律自检 (N = ceil(RTT/t), 速率 = N/RTT;  每周期恰 1 次边界, 且只在批起点):")
    bad = 0
    for rtt in (100, 120, 140, 150, 166, 180, 225, 280):
        for t in (83, 75, 60, 45, 25):
            if rtt % t == 0:
                continue                          # 退化点由 test_degeneracy 覆盖
            r = simulate(rtt, t, mode="frame", want_periods=200)
            N_exp = math.ceil(rtt / t)
            gap_exp = rtt - (N_exp - 1) * t
            if r["stalls"]:
                print("  ✗ RTT=%d t=%d: 停摆" % (rtt, t)); bad += 1; continue
            uniq = sorted(set(round(b) for b in r["bounds"]))
            if uniq != [gap_exp] or r["losses"]:
                print("  ✗ RTT=%d t=%d: 边界值 %s ≠ %d, losses=%d"
                      % (rtt, t, uniq, gap_exp, len(r["losses"])))
                bad += 1
                continue
            rate = N_exp / (rtt / 1000.0)
            print("  ✓ RTT=%3d t=%2d: N=%2d  周期数=%3d  边界恒=%3d ms  ⇒ %5.2f Hz"
                  % (rtt, t, N_exp, r["periods"], gap_exp, rate))
    assert bad == 0, "速度律自检失败 %d 例" % bad


def test_degeneracy() -> None:
    print("退化死角自检 (RTT ≡ 0 mod t ⇒ 帧判据停摆; ACK 判据正常):")
    pairs = [(rtt, t) for rtt in (100, 150, 200, 225, 300) for t in (25, 50, 75, 100)
             if rtt % t == 0]
    assert pairs, "没有退化点?"
    for rtt, t in pairs:
        rf = simulate(rtt, t, mode="frame", want_periods=50)
        assert rf["stalls"] == 1 and rf["periods"] == 0, (rtt, t, rf)
        ra = simulate(rtt, t, mode="ack", want_periods=200)
        assert ra["stalls"] == 0 and ra["periods"] == 200, (rtt, t, ra)
        print("  ✓ RTT=%3d t=%2d: 帧判据 0 次触发(停摆) | ACK 判据 %d 周期正常"
              % (rtt, t, ra["periods"]))
    # ACK 判据对全矩阵都不停摆
    for rtt in (100, 150, 166, 225):
        for t in (83, 75, 45, 25):
            r = simulate(rtt, t, mode="ack", want_periods=200)
            assert r["stalls"] == 0 and r["periods"] == 200, (rtt, t, r)
    print("  ✓ ACK 判据对 (RTT,t) 全矩阵 16 组全部不停摆")


def test_loss() -> None:
    print("丢帧不误判自检 (取 RTT=150, t=45 ⇒ N=4, 舍入误差为零):")
    rtt, t = 150, 45
    assert math.ceil(rtt / t) == 4

    # 情形 1: 丢批中间的帧(j=1) —— 后面还有帧, 批起点仍是短间隔 ⇒ 环路照常
    r = simulate(rtt, t, mode="frame", want_periods=200,
                 drop=lambda p, j: (j == 1 and p % 2 == 0))
    assert r["stalls"] == 0 and r["periods"] == 200, r
    assert r["losses"] and all(abs(d - 2 * t) < 0.6 for d in r["losses"]), set(r["losses"])
    assert sorted(set(round(b) for b in r["bounds"])) == [rtt - 3 * t], set(r["bounds"])
    print("  ✓ 丢中间帧: 200 周期零停摆; 判为丢帧 %d 次(全 = 2t = %d ms); 批起点恒 = %d ms"
          % (len(r["losses"]), 2 * t, rtt - 3 * t))

    # 情形 2: 丢紧邻批起点之前那帧(j=N-1) —— 批起点变成长间隔 ⇒ 按设计漏触发
    r2 = simulate(rtt, t, mode="frame", want_periods=200,
                  drop=lambda p, j: j == math.ceil(rtt / t) - 1)
    assert r2["stalls"] == 1 and r2["periods"] == 0, r2
    print("  ✓ 丢批末帧: 批起点变成 2t 长间隔 ⇒ 判据**按设计**不触发(该周期靠 --max-wait-ms 兜底)")
    print("     ⇒ ACK 判据不受批内丢帧影响, 这是 auto 默认的理由之二")


def test_burst_settling() -> None:
    """回归测试: 触发后必须"结算"缓冲区, 否则一次延迟到达的突发会连发两条命令。

    真机现象(2026-10-13 首轮 auto/n=8/t=83): 逐周期表呈 `0.5ms → ~300ms → 0.5ms → …` 交替。
    机制: **ACK 触发不消耗帧**。ACK 与它对应的批起点帧常在同一次 USB 突发里(主机侧看几乎
    同时), 而"查 ACK"排在"扫帧"之前 ⇒ ACK 先触发、缓冲区里那帧还没被扫; 下一轮扫描又把
    **同一个**批起点算一次 ⇒ 0.5ms 内再发一条命令, 后者抢占前者, 周期被压塌。
    修法: 任何触发之后, 先把缓冲区里**已经发生**的帧结算掉(记账但不触发)。

    仿真: 发帧时刻为真值, 交付按 137ms 的块突发(与周期 300ms 不成整数比), 主机 1ms 轮询。
    """
    P, t = 300, 83
    N = math.ceil(P / t)                       # 4 帧/批; 批起点间隔 = 300-3*83 = 51
    emits = []
    for k in range(40):
        for j in range(N):
            emits.append(k * P + j * t)
    emits.sort()
    CHUNK = 137
    n_batches = len(emits) // N

    out = {}
    for settle in (False, True):
        prev, cur, seen = None, 0, 0
        trig = dbl = 0
        last_trig = None
        ack_avail = 0
        for ms in range(1, emits[-1] + CHUNK + 1):
            c = (ms // CHUNK) * CHUNK
            while seen < len(emits) and emits[seen] <= c:
                if emits[seen] % P == 0:
                    ack_avail += 1               # 该批的 ACK 与 F1 在同一突发里到达
                seen += 1
            # (1) 查 ACK —— 真机里排在扫帧之前
            if ack_avail:
                ack_avail -= 1
                trig += 1
                if last_trig is not None and ms - last_trig < 50:
                    dbl += 1
                last_trig = ms
                if settle:
                    while cur < seen:            # ★ 结算: 记账但不触发
                        prev, cur = emits[cur] & 0xFF, cur + 1
                continue
            # (2) 扫帧
            allow = True
            while cur < seen:
                m8 = emits[cur] & 0xFF
                if prev is None:
                    prev, cur = m8, cur + 1
                    continue
                kind, _d = classify_gap(m8, prev, t, TOL)
                prev, cur = m8, cur + 1
                if kind != "bound" or not allow:
                    continue
                trig += 1
                if last_trig is not None and ms - last_trig < 50:
                    dbl += 1
                last_trig = ms
                allow = False                    # 同一次扫描内只触发一次
                if not settle:
                    break                        # 不结算: 残留帧留给下一轮 ⇒ 同一批起点再触发
        out[settle] = (trig, dbl)

    trig_no, dbl_no = out[False]
    trig_yes, dbl_yes = out[True]
    print("  不结算: 触发 %d 次, 其中 %d 次是距上次<50ms 的重复触发(批数 %d)"
          % (trig_no, dbl_no, n_batches))
    print("  结算后: 触发 %d 次, 重复 %d 次" % (trig_yes, dbl_yes))
    assert dbl_no > 0, "没有复现出重复触发(检查仿真)"
    assert dbl_yes == 0 and trig_yes >= n_batches - 1, (trig_yes, dbl_yes)
    print("  ✓ 回归成立: 不结算必然重复触发; 结算后每批恰好 1 次、零重复 ⇒ 修的就是这个")


def test_rate_table() -> None:
    print("可操作档位(RTT 未知也不影响: 速率 = ceil(RTT/t)/RTT, t 越小越快越均匀):")
    print("      %6s" % "t→", "".join("%12s" % ("t=%d" % t) for t in (83, 75, 60, 45, 25)))
    for rtt in (120, 150, 200, 280):
        cells = []
        for t in (83, 75, 60, 45, 25):
            N = math.ceil(rtt / t)
            cells.append("%2d帧 %5.1fHz" % (N, N / (rtt / 1000.0)))
        print("  RTT=%3d" % rtt, "".join("%12s" % c for c in cells))
    print("  注: 均匀性由 RTT mod t 决定 —— 间隔序列 = t,t,…,t,(RTT mod t)。")


# --------------------------------------------------- 动态 t: t = floor(RTT/N)+1
def decide_t(rtt: float, N: int) -> int:
    """动态 t 的**唯一**正确公式(与 link_pull_evt 主循环里那一行逐字等价)。"""
    return max(1, min(127, int(math.floor(rtt / N)) + 1))


def firmware_behaviour(rtt: float, t: int):
    """固件的真实行为(不是"我们希望"的行为)。

    每批**固定发 n 帧**, 下一批 PULL 只在 RTT 之后到达并抢占剩余 ⇒
        **每周期实发帧数 N_eff = floor(RTT/t) + 1**
    间隔序列 = t, t, …, t, (RTT − (N_eff−1)·t); 最后一个就是"批起点间隔"。
    返回 (N_eff, gaps)。
    """
    N = int(math.floor(rtt / t)) + 1
    return N, [t] * (N - 1) + [rtt - (N - 1) * t]


def effective_N(rtt: float, want_N: int) -> int:
    """镜像主循环里的 N 下限保护: t 有硬上限 127ms ⇒ N > RTT/127。"""
    return max(want_N, int(math.floor(rtt / 127.0)) + 1)


def test_dynamic_t_inversion() -> None:
    """t = floor(RTT/N)+1 ⇒ 每周期**恰好** N 帧, 且间隔极差 ≤ N ms。

    推导一行版: 要 N_eff = N, 需 (N−1)·t < RTT 且 N·t > RTT ⇒ t = floor(RTT/N)+1。
    间隔极差 = t − (RTT − (N−1)t) = N − (RTT mod N) ∈ [1, N]。
    """
    print("动态 t 反演自检  (t = floor(RTT/N)+1 ⇒ N_eff 恰好 = N; 间隔极差 ∈ [1, N] ms):")
    bad, worst, cnt = 0, 0, 0
    for rtt in (120, 150, 200, 225, 280, 300, 301, 305, 350, 400, 456):
        for want in range(2, 9):
            N = effective_N(rtt, want)          # 太小会被主循环自动抬到下限
            t = decide_t(rtt, N)
            neff, gaps = firmware_behaviour(rtt, t)
            sp = max(gaps) - min(gaps)
            cnt += 1
            worst = max(worst, sp)
            in_ok = classify_gap(t, 0, t, TOL)[0] == "in"
            bd = int(round(gaps[-1]))
            bd_ok = (bd >= 1) and (classify_gap(bd, 0, t, TOL)[0] == "bound")
            if not (neff == N and 1 <= sp <= N and in_ok and bd_ok):
                bad += 1
                print("  ✗ RTT=%d want_N=%d ⇒ N=%d t=%d N_eff=%d 间隔 %s (in=%s bd=%s)"
                      % (rtt, want, N, t, neff, gaps, in_ok, bd_ok))
    assert bad == 0, "动态 t 反演失败 %d 例" % bad
    print("  ✓ %d 组 (RTT, want_N) 全部 N_eff == N(含 N 被自动抬高的情形); 最大极差 %d ms"
          % (cnt, worst))
    print("    ⚠️ 硬约束 1: t ≤ 127 ms(ms8 只有 8 位) ⇒ **N 有下限** N > RTT/127:")
    for rtt in (150, 300, 456):
        nm = int(math.floor(rtt / 127.0)) + 1
        print("       RTT=%3d ms ⇒ N 最小 %d(≈%.1f Hz); 想更慢只能换 --trigger ack 或固定 --t-ms"
              % (rtt, nm, nm / (rtt / 1000.0)))
    print("    ⚠️ 硬约束 2: tol 必须 < 1 ms —— 批起点间隔最多只比 t 短 (t − bd = N − (RTT mod N)),")
    print("       最坏恰好 = 1 ms; 一旦 tol ≥ 1, 边界就会被判成 'in' 而**漏触发**、环路停摆。")


def test_round_formula_is_a_trap() -> None:
    """把公式写成 round(RTT/N)(看上去更自然)会在 N | RTT 时翻车 —— 记下来, 别改回去。"""
    print("反例自检  (round(RTT/N) 在 N | RTT 时退化成 t = RTT/N ⇒ N_eff = N+1, 边界间隔塌成 0):")
    for rtt, N in ((300, 4), (300, 5), (300, 6), (240, 3), (240, 4), (360, 8)):
        t_r, t_g = int(round(rtt / N)), decide_t(rtt, N)
        n_r, g_r = firmware_behaviour(rtt, t_r)
        n_g, g_g = firmware_behaviour(rtt, t_g)
        sp_r, sp_g = max(g_r) - min(g_r), max(g_g) - min(g_g)
        print("  RTT=%-4d N=%d │ round ⇒ t=%-3d N_eff=%-2d 极差 %6.0f ms │"
              " floor+1 ⇒ t=%-3d N_eff=%-2d 极差 %2.0f ms"
              % (rtt, N, t_r, n_r, sp_r, t_g, n_g, sp_g))
        assert n_g == N and sp_g <= N, "floor+1 也不对了: RTT=%d N=%d" % (rtt, N)
        assert (n_r != N) or (sp_r > N), "RTT=%d N=%d: round 居然没翻车, 该用例无意义" % (rtt, N)
    print("  ✓ 6/6 例: round 全部翻车(N_eff 多一帧且边界间隔 = 0); floor+1 全部正好 N 帧")


def simulate_dynamic(rtt_series, N, window=8, host_jitter=0.0):
    """闭环: 只喂**真实 RTT 序列**, 每轮按实现里同一套流水线推 t, 返回逐轮明细。

    流水线(与 link_pull_evt.main 逐步对应):
      1. 参照 = median(host_hist)(仅当解缠的并列参照; host_jitter 建模其误差)
      2. 批起点 ms8 差按 **75ms 物理格点**解缠(`unwrap_rtt`, 与实现同一函数)
      3. 合理性闸: 60..900 ms 且(在格点上 或 离参照 ≤80) → 入 rtt_hist
      4. t = floor(median(rtt_hist)/N)+1, 再套 N 下限保护 (N > RTT/127)
    返回 [(t, N_eff, 间隔极差, 本轮是否被闸丢样本), ...]

    ⚠️ ms8 样本必须"解缠后的值", 不能拿真值直接喂 —— 否则测的就不是解缠逻辑了。
    """
    host: collections.deque = collections.deque(maxlen=8)
    rh: collections.deque = collections.deque(maxlen=window)
    ref, t = 300.0, 83
    out = []
    for i, rtt in enumerate(rtt_series):
        t_used = t                                 # 本周期实际下发给单片机的 t
        neff, gaps = firmware_behaviour(rtt, t_used)
        host.append(rtt + (host_jitter if i % 2 else -host_jitter))
        ref = statistics.median(host)
        raw = int(rtt) & 0xFF                      # ms8 只有 8 位(真实受采样量化影响)
        obs, on_lat = unwrap_rtt(raw, ref)         # ← 与实现同一个函数
        ok = (60.0 <= obs <= 900.0) and (on_lat or abs(obs - ref) <= 80.0)
        if ok:
            rh.append(obs)
        if rh:
            med = statistics.median(rh)
            Nt = effective_N(med, N)
            t = max(1, min(127, int(math.floor(med / Nt)) + 1))
        out.append((t_used, neff, max(gaps) - min(gaps), 0 if ok else 1))
    return out


def simulate_legacy_unwrap(rtt_series, N, window=8):
    """**旧**解缠(离参照最近 + ±80 闸)的对照实现 —— 只为证明它换档时会冻住, 已不在产品里。"""
    host: collections.deque = collections.deque(maxlen=8)
    rh: collections.deque = collections.deque(maxlen=window)
    ref, t = 300.0, 83
    out = []
    for rtt in rtt_series:
        t_used = t
        neff, gaps = firmware_behaviour(rtt, t_used)
        host.append(rtt)
        ref = statistics.median(host)
        raw = int(rtt) & 0xFF
        obs = float(min((raw, raw - 256, raw + 256), key=lambda v: abs(v - ref)))
        ok = (5.0 <= obs <= 5000.0) and abs(obs - ref) <= 80.0
        if ok:
            rh.append(obs)
        if rh:
            med = statistics.median(rh)
            Nt = effective_N(med, N)
            t = max(1, min(127, int(math.floor(med / Nt)) + 1))
        out.append((t_used, neff, max(gaps) - min(gaps), 0 if ok else 1))
    return out


def test_unwrap_rtt_lattice() -> None:
    """★ 75 ms 格点解缠: ±256 的错分支必被排除, 且**换档样本不再被误拒**。

    实测 RTT ∈ {150, 225, 300, 375}(=75×2/3/4/5); 8 位 ms8 的 ±256 分支把它们挪到
    256 mod 75 = 31 ms 的格点外 ⇒ "落在格点上"就是无歧义判据。
    """
    print("RTT 解缠: 75ms 物理格点 vs 旧的'离参照最近'  (真值 = 实测的 150/225/300/375):")
    truth = [150, 225, 300, 375, 450]
    ref = 300.0                                   # 旧档参照(换档前)
    bad_old = 0
    for v in truth:
        raw = v & 0xFF
        new, on_lat = unwrap_rtt(raw, ref)
        old = float(min((raw, raw - 256, raw + 256), key=lambda x: abs(x - ref)))
        # 旧闸: |old - ref| <= 80 ?
        old_ok = abs(old - ref) <= 80.0
        new_ok = on_lat or abs(new - ref) <= 80.0
        flag = "✓" if (new == v and new_ok) else "✗"
        if not (old == v and old_ok):
            bad_old += 1
        print("  真值 %3d  raw=%3d → 新 %3.0f(%s, 闸%s) | 旧 %3.0f(闸%s)  %s"
              % (v, raw, new, "格点内" if on_lat else "格点外",
                 "放行" if new_ok else "拒绝", old, "放行" if old_ok else "拒绝", flag))
        assert new == v, "格点解缠把 %d 解成了 %.0f" % (v, new)
        assert new_ok, "格点解缠的 %d 却被闸拒了" % v
    print("  ✓ 5 个真值全部被格点解缠正确还原且放行")
    print("  · 旧法(离参照 300 最近 + ±80 闸)在这些值上出错/误拒 %d 个 —— 其中 150 会被解成 406"
          % bad_old)
    assert bad_old >= 1, "旧法应当至少在换档值上出错 (否则本测试没有鉴别力)"
    # 格点外样本必须被标出来(体检指标)
    _, on_lat = unwrap_rtt(int(413) & 0xFF, 400.0)
    print("  ✓ 格点外样本(413 ms)被标记 on_lattice=%s ⇒ off_lattice 计数可当体检指标" % on_lat)


def test_rtt_regime_shift() -> None:
    """★ RTT 换档(300→150)时 t 必须跟手 —— 这是真机 140s 那次暴露的问题。

    真机第 101 周期链路停顿 547 ms 后 RTT 由 300 换到 150: 周期自锁是对的(n=2, 150ms),
    但旧解缠把 150 解成 406、又按旧档 300 拒掉 ⇒ t 冻在 76/102(正确值应是 38)。
    """
    print("RTT 换档 300→150 时 t 是否跟手  (N=4, 期望 t: 76 → 38):")
    N = 4
    seq = [300] * 20 + [150] * 20
    new = simulate_dynamic(seq, N)
    old = simulate_legacy_unwrap(seq, N)
    t_new_tail = sorted({r[0] for r in new[26:]})
    t_old_tail = sorted({r[0] for r in old[26:]})
    print("  新(格点解缠): 换档后稳态 t 取值 %s   (期望 {%d})" % (t_new_tail, decide_t(150, N)))
    print("  旧(离参照最近): 换档后稳态 t 取值 %s   (卡在旧档 ⇒ 不跟手)" % t_old_tail)
    assert t_new_tail == [decide_t(150, N)], "换档后 t 没跟手: %s" % t_new_tail
    assert t_old_tail != [decide_t(150, N)], "旧法居然跟手了? 那说明换档场景没建对: %s" % t_old_tail
    dropped_new = sum(r[3] for r in new[20:])
    print("  ✓ 新法换档后 0 个样本被闸误拒(实收 = %d 个丢弃); 旧法把换档样本拒掉 ⇒ 冻在旧档"
          % dropped_new)
    assert dropped_new == 0, "换档后的样本不该被拒"
    trail = [new[i][0] for i in (0, 15, 19, 20, 21, 22, 24, 30, 39)]
    print("    新法 t 轨迹(采样) = %s  ← 阶跃处 ~%d 个周期收敛" % (trail, 3))
    # 换档期的"均匀性": N_eff 必须恢复为 N
    assert new[-1][1] == N, "换档后 N_eff=%d ≠ N=%d" % (new[-1][1], N)
    print("  ✓ 换档后 N_eff 恢复 = %d, 间隔极差 = %d ms" % (new[-1][1], new[-1][2]))


def test_dynamic_t_closed_loop() -> None:
    """闭环收敛自检: RTT 阶跃 / 野值 下 t 能否自己走回来, 稳态 N_eff 是否恒 = N。"""
    print("动态 t 闭环收敛自检  (喂真 RTT 序列, 看 t 自己收敛、解缠会不会跑飞):")
    N = 4
    seq = [300] * 12 + [225] * 12 + [300] * 12
    res = simulate_dynamic(seq, N)
    for lo, hi, rtt in ((6, 12, 300), (18, 24, 225), (30, 36, 300)):
        for i in range(lo, hi):
            t, neff, sp, dr = res[i]
            assert t == decide_t(rtt, N) and neff == N, \
                "第 %d 轮未收敛: RTT=%d 得到 t=%d(期望 %d) N_eff=%d" \
                % (i, rtt, t, decide_t(rtt, N), neff)
    trail = [res[i][0] for i in (0, 2, 12, 16, 18, 24, 28, 30, 33)]
    dropped = sum(r[3] for r in res)
    print("  ✓ 300 → 225 → 300 两次阶跃全部收敛: t 轨迹(采样) %s" % trail)
    print("    稳态 t = %d(RTT=300) / %d(RTT=225); N_eff 恒 = %d; 合理性闸丢样本 %d 个"
          % (decide_t(300, 4), decide_t(225, 4), N, dropped))
    assert dropped == 0, "干净序列不该有样本被丢弃"
    # 野值不能动 t (中位数窗口天然抗离群; 注意 457 恰在 75 格点上 ⇒ 单靠格点筛挡不住它)
    seq2 = [300] * 30
    for k in (10, 17):
        seq2[k] = 457                              # 真机实测出现过的野值量级
    res2 = simulate_dynamic(seq2, N)
    ts2 = sorted({r[0] for r in res2[8:]})
    print("  ✓ 混入 2 个 457 ms 野值: 稳态 t 取值集合 = %s (必须仍只有 %d)"
          % (ts2, decide_t(300, N)))
    assert ts2 == [decide_t(300, N)], "野值把 t 带偏了: %s" % ts2



def test_fixed_t_is_a_lottery() -> None:
    """固定 t 的问题不是"不好", 而是**无法预知好不好** —— 均匀性 = RTT mod t, 毫无规律。

    同一个 RTT 下相邻的 t 可能一个极好、一个灾难; 而 RTT 本身还会变(实测轮间就可能不同)
    ⇒ **固定 t 无解**, 这才是"该不该给它一个定值"的真正答案。
    """
    print("固定 t 的「抽奖」自检  (RTT=300 ms 固定, 只扫 t):")
    rtt = 300
    rows = []
    for t in (70, 71, 72, 73, 74, 75, 76, 77, 80, 83, 100, 101, 150):
        n, g = firmware_behaviour(rtt, t)
        sp = max(g) - min(g)
        rows.append((t, n, sp))
        tag = "  ← 灾难(边界间隔塌成 0 附近)" if sp > 40 else ("  ← 很好" if sp <= 5 else "")
        print("  t=%-4d N_eff=%-2d 间隔极差 %5.0f ms%s" % (t, n, sp, tag))
    best = min(rows, key=lambda r: r[2])
    worst = max(rows, key=lambda r: r[2])
    print("  同一 RTT: 最好 t=%-4d 极差 %3.0f ms  vs  最坏 t=%-4d 极差 %3.0f ms —— 差 %.0f 倍"
          % (best[0], best[2], worst[0], worst[2], worst[2] / max(1.0, best[2])))
    print("  实测印证: 本轮 t=83 ⇒ 极差 32 ms(83,83,83,51); 动态 t=76 ⇒ 极差 4 ms(76,76,76,72)")
    lo = max(2, int(math.floor(rtt / 127.0)) + 1)
    print("  动态 t 在同 RTT 下的可用档位(N ≥ %d): %s"
          % (lo, "  ".join("N=%d→t=%d(%.1fHz)"
                           % (N, decide_t(rtt, N), N / (rtt / 1000.0))
                           for N in range(lo, 9))))
    assert any(sp > 40 for _, _, sp in rows), "应当存在灾难 t"
    assert worst[2] / max(1.0, best[2]) > 10, "固定 t 的抽奖幅度应当很大"


def bootstrap_n_plan(boot: int, n: int, n_periods: int):
    """镜像主循环的 n_used 序列(含起步那条 PULL)。

    批号规则: 起步那条 = 批 1; 循环里第 p 次发送 = 批 `p+2`(修掉了原来 `periods+1` 的
    off-by-one —— 那样起步批与第 1 次循环发送会共用批号 1)。
    `still_boot = boot > 0 and batch <= boot` ⇒ 头 boot 轮落到单帧拉取。
    """
    seq = [1 if boot > 0 else n]
    for p in range(n_periods):
        batch = p + 2
        seq.append(1 if (boot > 0 and batch <= boot) else n)
    return seq


def bootstrap_t_trace(boot: int, n_periods: int = 6,
                      RTT: float = 300.0, T_SEED: int = 83, want_N: int = 4):
    """镜像主循环的**每批实际用的 t**(None = 该批是起步单帧轮, t 无所谓)。

    模型由真机 `evt_dyn_n4` 标定(`t_ms` 列 = 83,83,83,76,76,76):

      * 单个 RTT 样本 = **两个**"批起点检测点"做差; 第 1 个检测点只当锚, 不产生样本;
      * n=1 批: 那一帧**当场**就是批起点检测点(间隔 = 周期 = RTT, 无歧义);
      * n>1 批: 批起点检测点(被抢占的那帧)要**等下一批命令到达**才出现 ⇒ 晚一期;
      * 一旦手里有 ≥1 个样本, t 立刻收敛到 t_true。
    """
    sends = bootstrap_n_plan(boot, 8, n_periods)        # 每元素 = 该批发的 n
    t_true = decide_t(RTT, effective_N(RTT, want_N))
    out, detections, pend, t_cur = [], 0, 0, T_SEED
    for nn in sends:
        out.append(None if nn == 1 else t_cur)
        detections += pend
        pend = 0
        if nn == 1:
            detections += 1                            # n=1: 本期帧当场就是检测点
        else:
            pend = 1                                   # n>1: 本期的检测点晚一期才看到
        if max(0, detections - 1) >= 1:
            t_cur = t_true
    return out


def test_bootstrap_phase() -> None:
    """起步单帧轮 `--bootstrap K`: 头 K 轮 n=1(= 老拉取模式**逐字节相同**), 并在这几轮里
    无歧义地测出 RTT(主机周期无歧义 + n=1 时每帧都是批起点) ⇒ 转 N 帧时 t 已收敛。

    对照 boot=0: 主循环只能拿种子 `--t-ms` 硬撑。真机 `evt_dyn_n4` 实测逐周期 `t_ms` 列
    = `83,83,83,76,76,76` —— **前 3 个周期都在用种子 83**, 正是本用例要消掉的东西。
    """
    print("起步单帧轮自检  (--bootstrap K: 头 K 轮 n=1 = 老模式逐字节相同; 顺便测准 RTT):")
    RTT, want_N, n, T_SEED = 300.0, 4, 8, 83
    t_true = decide_t(RTT, effective_N(RTT, want_N))
    assert t_true == 76 and t_true != T_SEED, (t_true, T_SEED)

    # ① n_used 计划(缺这一条就无法断定"头几轮真的只发一帧")
    for boot, exp in ((0, [n, n, n, n, n]),
                      (2, [1, 1, n, n, n]),
                      (3, [1, 1, 1, n, n])):
        got = bootstrap_n_plan(boot, n, 4)
        assert got == exp, "boot=%d ⇒ n 计划 %s ≠ %s" % (boot, got, exp)
        print("  boot=%d ⇒ 每批 n = %s" % (boot, got))
    print("  boot=1 ⇒ 主循环自动抬到 2(ms8 的 RTT 样本要两个批起点帧做差, 1 轮拿不到)")

    # ② "种子 t 过渡期"消失了几个周期 —— 这是起步单帧轮的直接收益
    #    模型输出与真机 boot=0 的 83,83,83,76,76,76 必须逐项吻合(否则模型是编的)。
    assert bootstrap_t_trace(0)[:6] == [83, 83, 83, 76, 76, 76], \
        "模型与真机不符: %s" % bootstrap_t_trace(0)
    print("  boot=0 的 t 轨迹 %s  ← 与真机 evt_dyn_n4 的 t_ms 列**逐项吻合**"
          % bootstrap_t_trace(0)[:6])
    for boot in (2, 3):
        tr = bootstrap_t_trace(boot)
        seed_n = sum(1 for v in tr if v == T_SEED)
        print("  boot=%d 的 t 轨迹 %s ⇒ 落在种子 t 上的周期数 %d → %d"
              % (boot, tr, 3, seed_n))
        assert seed_n == 0, "boot=%d 仍有 %d 个周期用种子 t" % (boot, seed_n)
        assert tr.count(1) == 0 and tr[0] is None and tr[boot - 1] is None
    print("  ✓ 起步 ≥2 轮 ⇒ 第一个 N 帧周期(第 %d 期)就直接用上 t=%d, 种子过渡期归零"
          % (3, t_true))

    # ③ 起步轮 n=1 的帧间隔 = 周期 = RTT, 但 ms8 只有 8 位 ⇒ **折回**, 分类结果会随 RTT 翻转
    #    (RTT=300 ⇒ 原始差 44 < t ⇒ 误判"批起点"; RTT=150 ⇒ 原始差 150 > t ⇒ 误判"丢帧")
    #    ⇒ 帧判据在 n=1 下根本不可用 ⇒ 起步批记 t_of_batch=0, scan_frames 跳过分类、只喂 RTT。
    for rtt in (300, 150):
        raw, kind, d = int(round(rtt)) % 256, None, None
        kind, d = classify_gap(raw, 0, t_true, TOL)
        print("  n=1 且 RTT=%-3d ⇒ ms8 原始差 %-3d ⇒ 判成 '%s'(间隔 %.0f) —— "
              "同一个物理间隔, 判据随折回翻转" % (rtt, raw, kind, d))
        assert kind in ("bound", "loss")
    neff, gaps = firmware_behaviour(RTT, t_true)
    print("  而 N 帧周期边界 = RTT mod t = %d ms ⇒ 判 '%s' ⇒ 判据照常有效"
          % (int(round(gaps[-1])), classify_gap(int(round(gaps[-1])), 0, t_true, TOL)[0]))
    print("  ✓ 全部符合预期")


# =====================================================================================
# 事件级仿真: 批次打标时机 / RTT 来源  →  动态 t 稳不稳
# =====================================================================================
def simulate_tagging(rtt: float = 225.0, want_N: int = 4, n: int = 8, boot: int = 0,
                     tag_mode: str = "ack", rtt_mode: str = "ack",
                     n_periods: int = 26, t_seed: int = 83,
                     ack_lead: float = 3.0, t_tol: float = TOL,
                     gate: float = 80.0, window: int = 8, min_period_ms: float = 30.0,
                     dbg_out: list = None):
    """事件级仿真 —— 忠实建模"帧属于哪一批"的打标时机, 以及 RTT 样本从哪来。

    模型要点(与固件/真机一致):
      * 命令 k 在 S_k 发出, 走 D 才到单片机 ⇒ **第 k 批从 S_k+D 才开始发射**; 于是上一批的
        尾帧会**晚于 S_k** 到达 PC —— 用"发送时刻"打标, 它们就被贴上**新批号**(竞态根源)。
      * 本批 F1 在 S_k+2D = S_k+RTT 到达; ACK 短得多, 线上排在本批 F1 之前。
      * 新 PULL 到达单片机时抢占: 上一批 arrive >= S_{k+1}+RTT 的帧不再发出。
      * ms8 = **发射时刻**的 1ms 计数器低 8 位 ⇒ MCU 时基, 天然不含 CH340/USB 抖动。
      * 触发后先"结算"缓冲区(记账不触发) —— 上一版修好的, 这里保留。

    tag_mode: "send" = 旧实现(发送时刻改 `fl.cur_batch`, 有竞态) | "ack" = 现实现(收 ACK 才改)
    rtt_mode: "gap"  = 旧实现(样本取自被判为批起点的帧; 解缠参照 = 触发周期中位数)
              "ack"  = 现实现(样本取自 ACK 标记的批首帧; 解缠参照 = 逐条命令 send→ACK 往返)

    返回 rows: 每周期 [周期ms, 该周期用的 t, 该周期实发帧数, 触发来源]
    """
    import heapq

    D = rtt / 2.0
    INF = float("inf")
    eq: list = []
    _o = [0]

    def push(at, kind, batch, m=-1):
        _o[0] += 1
        heapq.heappush(eq, (at, _o[0], kind, batch, m))

    log: list = []                  # (到达时刻, ms8, 批号标签)
    cutoff: dict = {}
    t_of_batch: dict = {}
    rtt_hist: collections.deque = collections.deque(maxlen=window)
    rtt_send: collections.deque = collections.deque(maxlen=window)
    per_hist: collections.deque = collections.deque(maxlen=8)
    ack_idx: list = []
    bound_ms8: list = []
    rows: list = []
    st = {"t": float(t_seed), "t_ref": float(t_seed) * max(1, want_N),
          "ack_mark": 0, "bm": 0, "last_ack_m8": None, "last_gap_m8": None, "bad": 0,
          "cur_batch": 1, "inflight": 1, "last_send": 0.0, "nframes": 0,
          "cursor": 0, "prev_m8": None, "prev_tag": None}

    def send(k, n_use, t_use, at):
        cutoff[k] = INF
        # ★ 单片机在**整数 tick** 上发射(1ms 定时器), 所以同一批内的 ms8 差是**精确整数**。
        #   若拿浮点时间直接取整, 0.5 的舍入会在相邻两帧间造出 ±1ms 的伪抖动, 而判据 tol=0.75ms
        #   本来就是为了咬住"精确等于 t"这件事 —— 会被这点伪抖动全部判成批起点(踩过一次)。
        base = int(math.floor(at + D))
        push(at + rtt - ack_lead, "ack", k)
        for m in range(n_use):
            emit = base + m * int(t_use)
            push(emit + D, "frame", k, emit)          # payload 槽 = 发射 tick(整数)

    def scan(allow_frame) -> bool:
        """走一遍新帧。allow_frame=True 时遇到第一个批起点就**记下并返回 True**(与工具同)。"""
        hit = False
        while st["cursor"] < len(log):
            _, m8, tag = log[st["cursor"]]
            if st["prev_m8"] is None:
                st["prev_m8"], st["prev_tag"], st["cursor"] = m8, tag, st["cursor"] + 1
                continue
            t_cls = t_of_batch.get(st["prev_tag"], st["t"])
            if t_cls <= 0:                      # 起步单帧轮: 没有"批内间隔"可判
                st["prev_m8"], st["prev_tag"], st["cursor"] = m8, tag, st["cursor"] + 1
                continue
            kind, _d = classify_gap(m8, st["prev_m8"], int(t_cls), t_tol)
            st["prev_m8"], st["prev_tag"], st["cursor"] = m8, tag, st["cursor"] + 1
            if kind != "bound":
                continue
            bound_ms8.append(m8)
            if allow_frame and not hit:
                hit = True
        return hit

    def refresh_t():
        """镜像主循环里"动态 t"的那一段, 按 rtt_mode 分两种口径。"""
        if not want_N:
            return
        if rtt_mode == "ack":
            if rtt_send:
                st["t_ref"] = statistics.median(rtt_send)
            avail = len(log)
            j = st["ack_mark"]
            while j < len(ack_idx) and ack_idx[j] < avail:
                mm = log[ack_idx[j]][1]
                j += 1
                if st["last_ack_m8"] is not None:
                    raw = (mm - st["last_ack_m8"]) & 0xFF
                    cand = float(min((raw, raw - 256, raw + 256),
                                     key=lambda v: abs(v - st["t_ref"])))
                    if 5.0 <= cand <= 5000.0 and abs(cand - st["t_ref"]) <= gate:
                        rtt_hist.append(cand)
                    else:
                        st["bad"] += 1
                st["last_ack_m8"] = mm
            st["ack_mark"] = j
        else:
            if per_hist:
                st["t_ref"] = statistics.median(per_hist)
            for mm in bound_ms8[st["bm"]:]:
                if st["last_gap_m8"] is not None:
                    raw = (mm - st["last_gap_m8"]) & 0xFF
                    cand = float(min((raw, raw - 256, raw + 256),
                                     key=lambda v: abs(v - st["t_ref"])))
                    if 5.0 <= cand <= 5000.0 and abs(cand - st["t_ref"]) <= gate:
                        rtt_hist.append(cand)
                    else:
                        st["bad"] += 1
                st["last_gap_m8"] = mm
            st["bm"] = len(bound_ms8)
        if rtt_hist:
            med = statistics.median(rtt_hist)
            st["t"] = float(decide_t(med, effective_N(med, want_N)))

    def do_trigger(src, at):
        dt = at - st["last_send"]                                # 本周期长度(抢在更新前取)
        at = max(at, st["last_send"] + min_period_ms)            # --min-period-ms 硬闸(ms)
        # ★ 与工具同序: **先**按本周期新到的样本更新 RTT/t, **再**发命令(t 当周期就生效)。
        #   反过来(先发再算)会让每个周期都用上一轮的 t, 收敛慢一轮、且掩盖过渡期的误判。
        refresh_t()
        k = st["inflight"] + 1
        still_boot = boot > 0 and k <= boot
        t_use = st["t"]
        t_of_batch[k] = 0.0 if still_boot else t_use
        n_use = 1 if still_boot else n
        rows.append([at - st["last_send"], t_use, st["nframes"], src])
        per_hist.append(at - st["last_send"])
        if dbg_out is not None:
            dbg_out.append({"src": src, "dt": dt, "t": st["t"], "t_ref": st["t_ref"],
                            "rtt_hist": list(rtt_hist), "bad": st["bad"],
                            "nlog": len(log), "ack_idx": list(ack_idx),
                            "ack_mark": st["ack_mark"], "nframe": st["nframes"]})
        cutoff[st["inflight"]] = int(math.floor(at + D))   # 抢占: 上一批更晚发射的帧不再发出
        send(k, n_use, t_use, at)
        st["inflight"], st["last_send"], st["nframes"] = k, at, 0

    t_of_batch[1] = 0.0 if boot > 0 else st["t"]
    n0 = 1 if boot > 0 else n
    st["nframes"] = 0
    send(1, n0, st["t"], 0.0)

    # ★ 攒包: 真机把 ACK(14B) 与它对应的 F1(59B) 攒进**同一批 USB 突发**一起释放, 读线程把
    #    整批按到达顺序处理完, 主循环才醒过来扫一遍 —— 这正是"触发后结算"能吃掉重复触发的
    #    原因。不建模攒包, ACK 与 F1 就成了两个独立事件, 会凭空多出一次 0.5ms 的重复触发。
    coalesce = 4.0
    while eq and len(rows) < n_periods:
        at, _o2, kind, kb, m0 = heapq.heappop(eq)
        chunk = [(at, kind, kb, m0)]
        while eq and eq[0][0] <= at + coalesce:
            a2, _o3, k2, kb2, m2 = heapq.heappop(eq)
            chunk.append((a2, k2, kb2, m2))
        ack_done = None
        for a2, k2, kb2, m2 in chunk:
            if k2 == "ack":
                if tag_mode == "ack":
                    st["cur_batch"] = kb2        # ★ 打标的正确时机
                ack_idx.append(len(log))         # 该批 F1 将来占的下标
                rtt_send.append(rtt)
                ack_done = kb2
            else:
                if m2 >= cutoff.get(kb2, INF):   # 被新 PULL 抢占, 这一帧从未发出
                    continue
                log.append((a2, m2 & 0xFF, st["cur_batch"]))
                st["nframes"] += 1
        # 结算后统一判据: 帧优先, 其次 ACK(与工具主循环同序)
        if scan(True):
            do_trigger("frame", chunk[-1][0])
        elif ack_done is not None and ack_done == st["inflight"]:
            do_trigger("ack", chunk[-1][0])
    return rows


def test_tagging_race() -> None:
    """批次打标时机 + RTT 来源  →  动态 t 会不会发散(2×2 对照)。

    2026-10-13 真机 `--frames 4 --bootstrap 2`(旧实现)实测: t 轨迹 83 → 57 → 62 → 68 → 38,
    周期 308 → 134 ms, 15 个周期内就崩 —— 本用例用事件级仿真定位到底是哪一步放大了误差。
    """
    RTT, WANT_N, SEED = 225.0, 4, 83
    t_ideal = decide_t(RTT, effective_N(RTT, WANT_N))
    assert t_ideal == 57 and t_ideal != SEED, (t_ideal, SEED)
    print("批次打标 / RTT 来源 对动态 t 的影响  (事件级仿真; RTT=%.0f ms, N=%d, "
          "种子 t=%d, 真值 t=%d):" % (RTT, WANT_N, SEED, t_ideal))
    print("  %-34s %-12s %-9s %s" % ("变体 (打标时机 / RTT 来源)", "稳态 t", "早触发", "判读"))
    print("  " + "-" * 78)

    res = {}
    for tag_mode in ("send", "ack"):
        for rtt_mode in ("gap", "ack"):
            rows = simulate_tagging(RTT, WANT_N, n=8, boot=0, tag_mode=tag_mode,
                                    rtt_mode=rtt_mode, n_periods=26, t_seed=SEED,
                                    ack_lead=0.5)
            tail = rows[len(rows) // 3:]                  # 丢掉起步段, 只看后半段
            ts = [r[1] for r in tail]
            # "早触发" = 周期明显短于 RTT 的次数。这是**环路被判据带偏**的直接证据, 比
            #  "最小周期"稳定(硬闸会把最极端的那种压到 30ms 整, 看起来反而像正常值)。
            early = sum(1 for r in rows if r[0] < 0.75 * RTT)
            res[(tag_mode, rtt_mode)] = (ts, early, rows)
            lab = ("%s / %s" % ("发送时刻" if tag_mode == "send" else "收到 ACK",
                                "间隙分类" if rtt_mode == "gap" else "ACK 标记"))
            ts_ok = all(int(v) == t_ideal for v in ts)
            if min(ts) < 0.75 * t_ideal:
                verdict = "✗ 发散(t 坍塌)"
            elif ts_ok and early == 0:
                verdict = "✓ 干净"
            elif early:
                verdict = "△ t 对了, 但环路仍被误触发"
            else:
                verdict = "△ 环路稳, 但 t 有偏(参照口径不对)"
            print("  %-34s %-12s %-9s %s"
                  % (lab, "{%s}" % ",".join(str(int(v)) for v in sorted(set(ts))[:4]),
                     "%d/%d" % (early, len(rows)), verdict))

    # ---- 旧实现 (send / gap): 必须复现"t 往下坍塌" ----
    ts_old, early_old, rows_old = res[("send", "gap")]
    assert min(ts_old) < 0.75 * t_ideal, \
        "旧实现竟然没发散? t=%s" % sorted({int(v) for v in ts_old})
    assert early_old >= len(rows_old) // 3, \
        "旧实现的早触发次数太少(%d/%d), 机制没复现" % (early_old, len(rows_old))
    print("  ✓ 旧实现(发送时刻打标 + 间隙分类当 RTT 源)复现发散: t 从 %d 塌到 %d, "
          "%d/%d 个周期是早触发" % (SEED, min(ts_old), early_old, len(rows_old)))
    print("    链条: 在途的上一批尾帧被贴**新批号** ⇒ 它的批内间隔(旧 t)按**新 t** 判"
          " ⇒ 判成批起点 ⇒ 多发命令")
    print("          ⇒ 周期腰斩 ⇒ 触发周期中位数塌一半 ⇒ 解缠参照塌 ⇒ 错样本反被放行 ⇒ 自锁发散。")

    # ---- 只修打标 (ack / gap): 不再误发命令, 但参照口径仍是"触发周期", t 仍会偏 ----
    ts_mid, early_mid, _ = res[("ack", "gap")]
    assert early_mid == 0, "打标修好后不该再有早触发, 却有 %d 次" % early_mid
    assert min(ts_mid) != t_ideal, "对照无意义: 旧参照竟然给出了正确的 t"
    print("  · 只修打标(收到 ACK 才打标): 早触发 0 次 ⇒ **环路不再被带偏**; 但 t 取值 "
          "%s ⇒ 仍有偏"
          % sorted({int(v) for v in ts_mid}))
    print("    (因为参照仍是「触发周期」这个由环路健康度决定的量; 环路一旦被带偏, 参照跟着塌。)")

    # ---- 现实现 (ack / ack): 必须完全干净 ----
    ts_new, early_new, rows_new = res[("ack", "ack")]
    assert all(int(v) == t_ideal for v in ts_new), \
        "现实现稳态 t 不为 %d: %s" % (t_ideal, sorted({int(v) for v in ts_new}))
    assert early_new == 0, "现实现仍有 %d 次早触发" % early_new
    assert min(r[0] for r in rows_new) > 0.85 * RTT, \
        "现实现仍有被压短的周期: %.0f" % min(r[0] for r in rows_new)
    print("  ✓ 现实现(收到 ACK 才打标 + RTT 取自 ACK 标记帧/逐条命令往返): "
          "稳态 t 恒 = %d, 早触发 0 次, 周期 %.0f~%.0f ms(≈RTT, 差 1ms 是 MCU tick 量化)"
          % (t_ideal, min(r[0] for r in rows_new), max(r[0] for r in rows_new)))

    ts_sw, early_sw, _ = res[("send", "ack")]
    assert all(int(v) == t_ideal for v in ts_sw), \
        "只换 RTT 来源时 t 竟然不稳: %s" % sorted({int(v) for v in ts_sw})
    print("  · 只换 RTT 来源(仍发送时刻打标): t 也对(%s), 但 %d/%d 个周期是早触发"
          % (sorted({int(v) for v in ts_sw}), early_sw, 26))
    print("    ⇒ 打标竞态**仍在误发命令**; 只是 RTT 估计被保护住了, 所以不再雪崩。")
    print("  ⇒ 结论: 两处都要改 ——")
    print("     ① **RTT 来源**(根治): 从「间隙分类 + 触发周期参照」换成「ACK 标记帧 + 逐条命令")
    print("        send→ACK 往返参照」。腰斩样本会在 ±80ms 一致性闸前被拒, 发散环断在这里。")
    print("     ② **打标时机**(防复发): t 会因 RTT 漂移而变(真机实测 150/225/300, 三值差 75ms),")
    print("        那时发送时刻打标就会在过渡期误判, 所以打标必须等 ACK。")


if __name__ == "__main__":
    test_classify()
    print()
    test_speed_law()
    print()
    test_degeneracy()
    print()
    test_loss()
    print()
    test_burst_settling()
    print()
    test_rate_table()
    print()
    test_dynamic_t_inversion()
    print()
    test_round_formula_is_a_trap()
    print()
    test_dynamic_t_closed_loop()
    print()
    test_unwrap_rtt_lattice()
    print()
    test_rtt_regime_shift()
    print()
    test_fixed_t_is_a_lottery()
    print()
    test_bootstrap_phase()
    print()
    test_tagging_race()
    print()
    print("全部通过 ✓")
    sys.exit(0)
