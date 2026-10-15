"""v2.10.0 统一拉取器 **真机验收**（《遥测拉取器统一设计方案》§9.3 五项）。

对应设计文档 §9.3 的真机验收清单（本脚本逐项核对并给出 PASS/FAIL）：

    A1  默认停等连接收帧       on()/停等: 在途≤1、锚逐条刷新、帧率≈1/RTT
    A2  pace() 切 10Hz 长跑零丢批   默认 120s（对齐 v2.9 验收口径）
    A5  锚刷新注入期间帧率无损    批中点注入 TELEM; 对比注入前后 hz
    A4  MCU RESET 后首帧 ~3.2s 不误报断连   telem 阈值 7s 的关键验证
    A3  运动中 urgent 让路 5/5 无重发   **需 --motor 显式开启**(发运动命令)

⚠️ 安全纪律（沿用本项目真机脚本惯例）：
    · A1/A2/A4/A5 只发 `PING` / `TELEM` / `RATE`（A4 额外一条 `MCU RESET`），
      **绝不发运动命令**；
    · A3 会发 `MOVE RUN`（原地转，双护栏约束：时间 + 红外），**必须 --motor 显式开启**，
      且要求**车轮悬空**或场地空旷；开启后先打印风险再倒数 3 秒。

用法（在 host/ 目录下）：
    python v210_accept.py COM7                  # 只跑 A1 A2 A5 A4（不动电机）
    python v210_accept.py COM7 --pace-secs 120  # 指定 A2 时长（默认 120）
    python v210_accept.py COM7 --no-reset       # 跳过 A4（不做整机软复位）
    python v210_accept.py COM7 --motor          # 追加 A3（发运动命令，车轮须悬空）
    python v210_accept.py COM7 --only A3 --motor  # 只重跑某一项（逗号分隔多项）

判据来源：设计文档 §9.3；`TelemetryPuller` 的类常量见 `host/host/api/stream.py`。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from host.api import MouseClient  # noqa: E402
from host.api.events import Event  # noqa: E402
from host.api.stream import TelemetryPuller  # noqa: E402

PASS = 0
FAIL = 0
FAILED: List[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"    [OK] {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"    [FAIL] {name}" + (f"  <- {detail}" if detail else ""))
    return bool(cond)


def section(title: str) -> None:
    print("\n" + "-" * 68)
    print(title)
    print("-" * 68)


def fmt_stats(st: Dict[str, Any]) -> str:
    return (f"mode={st.get('mode')} n={st.get('n')} "
            f"frames={st.get('frames')} batches={st.get('batches')} "
            f"lost={st.get('lost')} lost_b={st.get('lost_batches')} "
            f"inflight={st.get('inflight')} hz={st.get('hz')} "
            f"rtt={st.get('rtt_ms')}ms anch={st.get('anchor_refreshes')}")


def _fw_expect():
    """从固件 `link.h` 现读 (LINK_PROTO_VER, LINK_FW_VER)，读不到返回 None。"""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                     "code_stm32f1_mouse", "HARDWARE", "link.h")
    try:
        h = open(p, encoding="utf-8").read()
        return (int(re.search(r"#define\s+LINK_PROTO_VER\s+(\d+)", h).group(1)),
                int(re.search(r"#define\s+LINK_FW_VER\s+(\d+)", h).group(1)))
    except Exception:
        return None


class LinkLostWatch:
    """后台订阅 `Event.ERROR(kind="link_lost")` —— 用于断言"没误报断连"。"""

    def __init__(self, mc: MouseClient) -> None:
        self.events: List[Any] = []
        self._off = mc.on(Event.ERROR, self._on)

    def _on(self, m) -> None:
        if (getattr(m, "data", None) or {}).get("kind") == "link_lost":
            self.events.append(m)

    def close(self) -> None:
        try:
            self._off()
        except Exception:
            pass


# ================================================================ A1 停等

def a1_stopwait(mc: MouseClient) -> None:
    section("A1  默认停等连接收帧（on()：在途≤1 / 锚逐条刷新 / 帧率≈1/RTT）")
    check("连接后自动进入停等(mode=stopwait)", mc.stream.mode == "stopwait",
          mc.stream.mode)
    check("等到首帧", mc.telemetry.wait_first_frame(timeout=8.0))

    lost_watch = LinkLostWatch(mc)
    try:
        t0 = time.time()
        while time.time() - t0 < 6.0:
            time.sleep(1.0)
            st = mc.stream.stats()
            print(f"      t={time.time()-t0:4.1f}s  {fmt_stats(st)}")
        st = mc.stream.stats()
        check("在途恒 ≤1（统一不变量）", st["inflight"] <= 1, str(st["inflight"]))
        check("请求三态自洽 sent=done+inflight 且无丢失",
              st["sent"] == st["done"] + st["inflight"] and st["lost"] == 0,
              f"sent={st['sent']} done={st['done']} "
              f"inflight={st['inflight']} lost={st['lost']}")
        check("帧数≈命令数（1 命令 1 帧）",
              st["done"] <= st["frames"] <= st["done"] + 1,
              f"done={st['done']} frames={st['frames']}")
        check("停等式不注入锚(anchor_refreshes=0)",
              st["anchor_refreshes"] == 0, str(st["anchor_refreshes"]))
        check("帧率在合理区间(1~25Hz)", 1.0 <= st["hz"] <= 25.0, str(st["hz"]))
        a = mc.stream.anchor()
        check("时间锚已建立", a is not None, str(a))
        snap = mc.timesync.snapshot()
        check("锚逐条刷新(年龄 <1.5s)", snap.anchor_age_s is not None
              and snap.anchor_age_s < 1.5, str(snap.anchor_age_s))
        check("A1 期间未误报断连", not lost_watch.events)
        check("链路未判死(dead=False)", st["dead"] is False, str(st["dead"]))
    finally:
        lost_watch.close()


# ================================================================ A2 pace 长跑

def a2_pace_long(mc: MouseClient, secs: float) -> None:
    section(f"A2  pace() 切 10Hz 长跑零丢批（{secs:.0f}s，默认 PULL 3 100）")
    lost_watch = LinkLostWatch(mc)
    try:
        r = mc.stream.pace()
        check("pace() 返回 OK", r.ok, str(r.status))
        check("mode=pace", mc.stream.mode == "pace", mc.stream.mode)
        check("批式档 n=3/t=100", (mc.stream.stats()["n"],
                                   mc.stream.stats()["t_ms"]) == (3, 100),
              fmt_stats(mc.stream.stats()))
        check("等到首帧", mc.telemetry.wait_first_frame(timeout=8.0))

        t0 = time.time()
        next_log = t0 + 10.0
        row = 0
        while time.time() - t0 < secs:
            time.sleep(0.5)
            if mc.stream.stats().get("dead"):
                print("      ⚠️ 链路判死(dead)——真机断连, 中止本项")
                break
            now = time.time()
            if now >= next_log:
                next_log = now + 10.0
                st = mc.stream.stats()
                el = now - t0
                print(f"      t={el:5.1f}s  frames={st['frames']} "
                      f"batches={st['batches']} lost_b={st['lost_batches']} "
                      f"hz={st['hz']} rtt={st['rtt_ms']}ms "
                      f"anch={st['anchor_refreshes']}")
                row += 1

        st = mc.stream.stats()
        el = time.time() - t0
        exp_frames = 10.0 * el
        print(f"      结束: {fmt_stats(st)}  实测 fps={st['frames']/el:.2f}")
        check("全程零丢批(lost_batches=0)", st["lost_batches"] == 0,
              str(st["lost_batches"]))
        check("帧数达目标 90% 以上",
              st["frames"] >= 0.90 * exp_frames,
              f"frames={st['frames']} 期望≈{exp_frames:.0f}")
        check("实测帧率 8.5~11 Hz", 8.5 <= st["frames"] / el <= 11.0,
              f"{st['frames']/el:.2f}")
        check("A2 期间未误报断连", not lost_watch.events,
              f"{len(lost_watch.events)} 次")
        check("链路未判死(dead=False)", st["dead"] is False, str(st["dead"]))
    finally:
        lost_watch.close()
        mc.stream.pace_off()


# ================================================================ A5 锚刷新无损

def a5_anchor_fps(mc: MouseClient, secs: float = 24.0) -> None:
    section(f"A5  锚刷新注入期间帧率无损（ANCHOR_REFRESH_S 调小到 3s，跑 {secs:.0f}s）")
    old = TelemetryPuller.ANCHOR_REFRESH_S
    TelemetryPuller.ANCHOR_REFRESH_S = 3.0
    lost_watch = LinkLostWatch(mc)
    try:
        mc.stream.pace()
        check("mode=pace", mc.stream.mode == "pace", mc.stream.mode)
        check("等到首帧", mc.telemetry.wait_first_frame(timeout=8.0))
        t0 = time.time()
        hz_samples: List[float] = []
        next_log = t0 + 3.0
        while time.time() - t0 < secs:
            time.sleep(0.5)
            if mc.stream.stats().get("dead"):
                print("      ⚠️ 链路判死, 中止本项")
                break
            now = time.time()
            if now >= next_log:
                next_log = now + 3.0
                st = mc.stream.stats()
                hz_samples.append(float(st["hz"]))
                print(f"      t={now-t0:5.1f}s  hz={st['hz']:5.2f}  "
                      f"anch={st['anchor_refreshes']}  frames={st['frames']}")
        st = mc.stream.stats()
        n_inj = st["anchor_refreshes"]
        check("确实发生了多次锚注入(>3)", n_inj > 3, str(n_inj))
        # 去掉前两拍(启动瞬态), 用稳态窗口判帧率
        steady = [h for h in hz_samples[2:]] or hz_samples or [0.0]
        mn, mx = min(steady), max(steady)
        print(f"      稳态 hz: min={mn:.2f} max={mx:.2f} 注入次数={n_inj}")
        check("注入期间帧率无塌陷(min≥6Hz)", mn >= 6.0, f"min={mn:.2f}")
        check("帧率维持在 10Hz 档附近(max≤11.5Hz)", mx <= 11.5, f"max={mx:.2f}")
        snap = mc.timesync.snapshot()
        check("同余还原未失锁", snap.locked is True, str(snap.lost))
        check("A5 期间未误报断连", not lost_watch.events)
    finally:
        lost_watch.close()
        mc.stream.pace_off()
        TelemetryPuller.ANCHOR_REFRESH_S = old


# ================================================================ A4 MCU RESET

def a4_mcu_reset(mc: MouseClient) -> None:
    section("A4  MCU RESET 后首帧 ~3.2s 不误报断连（telem 阈值 7s 的关键验证）")
    lost_watch = LinkLostWatch(mc)
    frame_ts: List[float] = []

    def _on_frame(msg) -> None:
        frame_ts.append(time.time())

    off_frame = mc.on(Event.TELEMETRY, _on_frame)
    try:
        mc.stream.on()
        check("切回停等 mode=stopwait", mc.stream.mode == "stopwait",
              mc.stream.mode)
        check("等到首帧", mc.telemetry.wait_first_frame(timeout=8.0))
        # 确认车轮静止再复位(整机软复位会打断任何进行中的运动)
        idle = mc.move.wait_idle(timeout=3.0)
        print(f"      复位前车轮静止: {idle}（speedL/R="
              f"{mc.telemetry.latest().get('speedL')}/"
              f"{mc.telemetry.latest().get('speedR')}）")

        frame_ts.clear()
        time.sleep(0.5)                     # 采一段正常帧当基准
        t_reset = time.time()
        print("      → 发送 MCU RESET（整机软复位，ACK 先回、约 150ms 后复位）")
        r = mc.send_cmd("system.reset")
        print(f"      MCU RESET 应答: ok={r.ok} status={r.status} "
              f"attempts={r.data.get('attempts')}")

        # 监听 ~9s: 记录帧间最大空档 + 是否恢复
        last_seen = time.time()
        max_gap = 0.0
        resumed_at = None
        deadline = time.time() + 9.0
        while time.time() < deadline:
            time.sleep(0.02)
            if frame_ts:
                t = frame_ts[-1]
                if t > last_seen:
                    gap = t - last_seen
                    max_gap = max(max_gap, gap)
                    last_seen = t
                    if resumed_at is None and t - t_reset > 0.5:
                        resumed_at = t
        st = mc.stream.stats()
        print(f"      复位后帧间最大空档={max_gap:.2f}s  "
              f"恢复用时={((resumed_at - t_reset) if resumed_at else float('nan')):.2f}s  "
              f"frames={st['frames']} lost={st['lost']}")

        check("复位后帧流恢复（有帧到达）", resumed_at is not None)
        check("首帧空档 < 7s（telem 断连阈值）", max_gap < 7.0,
              f"max_gap={max_gap:.2f}s")
        check("未误报 link_lost 事件", not lost_watch.events,
              f"{len(lost_watch.events)} 次")
        check("链路未判死(dead=False)", st["dead"] is False, str(st["dead"]))
        # 复位原因核对: 复位后 PING 应报 rst=SW
        mc.send_cmd("ping")
        time.sleep(0.3)
        rc = mc.reset_cause
        print(f"      复位后 PING rst={rc}")
        check("复位原因 = SW（软复位生效）", rc == "SW", str(rc))
    finally:
        off_frame()
        lost_watch.close()


# ================================================================ A3 运动中让路

def a3_urgent_yield(mc: MouseClient, trials: int = 5) -> None:
    section(f"A3  运动中 urgent 让路 {trials}/{trials} 无重发（**发运动命令**）")
    st = mc.lab.status()
    print(f"      设备状态: can_move={st['can_move']} "
          f"motor_disabled={st['motor_disabled']} "
          f"estop_locked={st['estop_locked']} ir_min={st['ir_min']}")
    if st["motor_disabled"] is True:
        check("设备可运动（非安全测试固件）", False,
              "固件为 MOUSE_TEST_NO_MOTOR，电机恒 0，A3 无意义")
        return
    if st["can_move"] is not True:
        check("设备可运动", False, f"status={st}")
        return

    lost_watch = LinkLostWatch(mc)
    try:
        r = mc.stream.pace()          # 拉取器开着 = 让路的最难工况
        check("拉取器已开(pace)", mc.stream.mode == "pace", mc.stream.mode)
        check("等到首帧", mc.telemetry.wait_first_frame(timeout=8.0))

        attempts_list: List[int] = []
        retried = 0
        moved_ok = 0
        for i in range(1, trials + 1):
            # 发运动: 原地转(左右反向)，时间护栏 1500ms，关闭红外护栏
            # （原地转不接近前墙；时间护栏保证有界）。
            ack = mc.send_cmd("move.run", sp_l=25, sp_r=-25, ms=1500, ir_mm=-1)
            if not ack.ok:
                print(f"      [{i}] MOVE RUN 被拒: {ack.status}")
                continue
            time.sleep(0.30)                     # 让它真的转起来
            f = mc.telemetry.latest()
            spd = (abs(float(f.get("speedL", 0))), abs(float(f.get("speedR", 0))))
            if max(spd) > 2.0:
                moved_ok += 1
            # 运动中下发 urgent STOP（estop.clear = STOP, urgent=True → 让路）
            s = mc.send_cmd("estop.clear")
            att = int(s.data.get("attempts", 0))
            attempts_list.append(att)
            if att > 1:
                retried += 1
            print(f"      [{i}] 转速={spd[0]:.0f}/{spd[1]:.0f}  "
                  f"STOP ok={s.ok} attempts={att}")
            time.sleep(1.0)                      # 等停稳再下一轮

        check("至少观察到车在动（速度>2cm/s）", moved_ok >= 1,
              f"{moved_ok}/{trials} 轮见到运动")
        check(f"每轮 STOP attempts 均为 1（无重发）", retried == 0,
              f"重发 {retried} 次; attempts={attempts_list}")
        check("STOP 全部成功", len(attempts_list) == trials
              and all(a >= 1 for a in attempts_list), str(attempts_list))
        st = mc.stream.stats()
        check("批式未判死", st["dead"] is False, str(st["dead"]))
        check("A3 期间未误报断连", not lost_watch.events)
    finally:
        lost_watch.close()
        mc.stream.pace_off()


# ================================================================ main

def main() -> int:
    ap = argparse.ArgumentParser(description="v2.10 统一拉取器真机验收（设计 §9.3）")
    ap.add_argument("port", nargs="?", default="COM7")
    ap.add_argument("--pace-secs", type=float, default=120.0,
                    help="A2 批式长跑时长(默认 120s)")
    ap.add_argument("--no-reset", action="store_true",
                    help="跳过 A4（不做 MCU RESET 整机软复位）")
    ap.add_argument("--motor", action="store_true",
                    help="开启 A3（发 MOVE RUN 运动命令；车轮须悬空/场地空旷）")
    ap.add_argument("--only", default="",
                    help="只跑指定项（逗号分隔，如 A3 或 A1,A2）；默认全跑")
    args = ap.parse_args()

    want = {s.strip().upper() for s in args.only.split(",") if s.strip()}

    def sel(tag: str) -> bool:
        return (tag in want) if want else True

    print("=" * 68)
    print(f"v2.10.0 统一拉取器真机验收   端口={args.port}")
    print("=" * 68)

    mc = MouseClient(auto_session=False, auto_stream=True)
    if not mc.connect(args.port):
        print(f"✗ 无法连接 {args.port}: {mc.state_detail}")
        mc.close()
        return 2

    try:
        # connect() 返回时 PING 的 PONG 可能还没被调度线程解析成 device, 这里补一次
        if not mc.device:
            mc.send_cmd("ping")
            time.sleep(0.3)
        dev = mc.device
        print(f"设备指纹: {dev}")
        exp = _fw_expect()
        if exp is not None and dev:
            got = (dev.get("proto"), dev.get("fw"))
            if got != exp:
                print(f"  ⚠️ 板上固件 {got} 与源码 link.h {exp} 不一致 —— "
                      f"下面的判据可能失真")
        if sel("A1"):
            a1_stopwait(mc)
        if sel("A2"):
            a2_pace_long(mc, args.pace_secs)
        if sel("A5"):
            a5_anchor_fps(mc)
        if sel("A4"):
            if args.no_reset:
                print("\n[跳过] A4（--no-reset）")
            else:
                a4_mcu_reset(mc)
        if sel("A3") and args.motor:
            print("\n" + "!" * 68)
            print("A3 即将发送 MOVE RUN 运动命令 —— 请确认【车轮悬空】或场地空旷。")
            print("!" * 68)
            for k in (3, 2, 1):
                print(f"    {k} ...")
                time.sleep(1.0)
            a3_urgent_yield(mc)
        elif sel("A3"):
            print("\n[跳过] A3（未加 --motor；运动中让路验收需显式开启）")
    except KeyboardInterrupt:
        print("\n用户中断。")
    finally:
        try:
            mc.stream.off()
        except Exception:
            pass
        mc.close()

    print("\n" + "=" * 68)
    print(f"验收结果: 通过 {PASS} / 失败 {FAIL}")
    if FAILED:
        print("失败项:")
        for f in FAILED:
            print(f"  - {f}")
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
