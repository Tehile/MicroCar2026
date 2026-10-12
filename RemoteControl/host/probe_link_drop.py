# -*- coding: utf-8 -*-
"""判定「谁在丢帧」：单片机 TX 环 vs 链路中段（模块/蓝牙/CH340）。

原理
----
固件在统一帧 offset 44（`LINK_U_DROP`）上报 `linkDrop` —— 自 `link_RateReset`
以来**因 TX 环空间不足而整帧丢弃**的累计计数（link.c:1220）。
host 侧从未解析该字段；本探针在运行时包装 `registry.FRAMES` 的 decoder 取出它，
不改动被测代码，同时统计 PC 端实际收到的帧数。

判据（`RATE FIX <fps>` 恒定发送期间）
------------------------------------
* **MCU-drop 不涨，但 PC 实收 < 目标**  ⇒ 帧已成功交给模块，丢在**链路里**
  （模块 / 蓝牙 / CH340 的缓冲区）。→ 支持"链路中段有隐形缓冲上限"。
* **MCU-drop 涨速 ≈ (目标 − 实收)**    ⇒ 丢在**单片机侧**，即 TX 环满，
  说明模块的输入侧喂不进（模块 UART 波特率/流控）。
* **两者都不涨但 PC 实收 = 目标**      ⇒ 本档位健康。

用法
----
    python probe_link_drop.py COM7 --fps 30 --duration 120
    python probe_link_drop.py COM7 --fps 12 --duration 120 --out drop12.csv
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import sys
import time

sys.path.insert(0, ".")

import host.registry as registry
from host.registry import TYPE_UNIFIED

# ---------------------------------------------------------------- 取出 drop 字段
# FrameSpec 是 frozen dataclass ⇒ 用 dataclasses.replace 造一个 decoder 被包装的副本，
# 替换进 registry.FRAMES 表(仅本进程内生效，不改动被测代码)。
_orig = registry.FRAMES[TYPE_UNIFIED]
_orig_decoder = _orig.decoder


def _decoder_with_drop(payload: bytes) -> dict:
    """在 registry 解码器之上补一个 `mcuDrop`(offset 44, linkDrop 低 8 位)。

    ⚠️ v2.6 起不再补 `fpsBroadcast`(帧尾 2 B 目标帧率随 AIMD 删除, 载荷恒 49 B)。
    """
    d = _orig_decoder(payload)
    if len(payload) > 44:
        d["mcuDrop"] = payload[44]          # u8, linkDrop 低 8 位
    return d


registry.FRAMES[TYPE_UNIFIED] = dataclasses.replace(_orig, decoder=_decoder_with_drop)

from host.api import MouseClient  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("port")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--duration", type=float, default=120.0)
    ap.add_argument("--gap-timeout", type=float, default=3.0)
    ap.add_argument("--out", default=None, help="逐秒 CSV 输出路径")
    a = ap.parse_args()

    mc = MouseClient(auto_session=False, auto_stream=False)
    if not mc.connect(a.port):
        print("连接失败:", mc.state_detail)
        return 2
    time.sleep(0.3)

    cnt = {"n": 0, "drop": 0, "last_rx": None}
    prev = mc.store.on_frame

    def hook(stream, rec):
        if stream == "unified":
            cnt["n"] += 1
            cnt["last_rx"] = float(rec.get("_rx_mono") or time.monotonic())
            if "mcuDrop" in rec:
                cnt["drop"] = rec["mcuDrop"]
        if prev:
            prev(stream, rec)

    mc.store.on_frame = hook

    ok = False
    for i in range(6):
        try:
            p = mc.cmd.send("RATE FIX %d" % a.fps, wait=True, timeout=2.0, no_retry=True)
            print("RATE FIX %d -> %s (第 %d 次, %s)" % (a.fps, p.ok, i + 1, p.status))
            if p.ok:
                ok = True
                break
        except Exception as e:
            print("RATE FIX 异常:", type(e).__name__)
        time.sleep(0.3)
    if not ok:
        print("无法进入固定节拍模式，退出")
        return 3

    fh = None
    w = None
    if a.out:
        fh = open(a.out, "w", newline="", encoding="utf-8-sig")
        w = csv.writer(fh)
        w.writerow(["t_s", "frames", "mcu_drop_total", "mcu_drop_delta"])
        fh.flush()

    t0 = time.monotonic()
    last_t = t0
    last_n = 0
    last_drop = None
    drop_acc = 0
    print("")
    print("%7s %7s %9s %11s %12s" % ("t_s", "实收/s", "累计收帧", "MCU丢帧累计", "MCU丢帧增量"))
    while True:
        now = time.monotonic()
        el = now - t0
        if el >= a.duration:
            print("\n到达时长上限（未断连）")
            break
        time.sleep(0.25)

        # 断连判定
        last_rx = cnt["last_rx"]
        if last_rx is not None and (now - last_rx) > a.gap_timeout:
            print("\n✗✗ 断连：连续 %.1fs 无帧 (t=%.1fs)" % (now - last_rx, el))
            break

        if now - last_t >= 1.0:
            dn = cnt["n"] - last_n
            dd = None
            if cnt["drop"] is not None and last_drop is not None:
                dd = (cnt["drop"] - last_drop) & 0xFF      # u8 回绕安全
                drop_acc += dd
            last_drop = cnt["drop"]
            print("%7.1f %7d %9d %11s %12s" % (
                el, dn, cnt["n"],
                cnt["drop"] if cnt["drop"] is not None else "-",
                dd if dd is not None else "-"))
            if w:
                w.writerow(["%.1f" % el, dn, cnt["drop"], dd if dd is not None else ""])
                fh.flush()
            last_t = now
            last_n = cnt["n"]

    el = time.monotonic() - t0
    print("")
    print("=" * 64)
    print("汇总: 时长 %.1fs  目标 %d Hz  PC 实收 %d 帧 (%.2f Hz, 达成率 %.1f%%)"
          % (el, a.fps, cnt["n"], cnt["n"] / el, 100.0 * (cnt["n"] / el) / a.fps))
    print("      MCU 侧因 TX 环满丢弃(增量累计) = %d 帧" % drop_acc)
    miss = a.fps * el - cnt["n"]
    print("      PC 端缺口 = %.0f 帧" % miss)
    if drop_acc == 0 and miss > 5:
        print("  ⇒ **MCU 没有丢帧**，帧已交给模块 ⇒ 丢失发生在【链路中段】")
    elif drop_acc > 0 and abs(drop_acc - miss) <= max(3, 0.3 * miss):
        print("  ⇒ MCU 丢帧数 ≈ PC 缺口 ⇒ 丢失发生在【单片机 TX 环】(模块喂不进)")
    elif drop_acc > 0:
        print("  ⇒ 两侧都在丢: MCU %d 帧 + 链路 %.0f 帧" % (drop_acc, miss - drop_acc))
    else:
        print("  ⇒ 未观察到明显丢帧")
    print("=" * 64)
    if fh:
        fh.close()
        print("逐秒数据:", a.out)
    try:
        mc.disconnect()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
