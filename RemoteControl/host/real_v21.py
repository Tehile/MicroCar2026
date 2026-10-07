"""真机 v2.1 验证（用**正式上位机 API**，即 pull_smoke 验证过的同一条代码路径）。

只发非运动命令（PING / TELEM），内部有运动命令黑名单。

阶段:
  V1 指纹        —— PING 应回 `PONG 3 21x 7 256 <rst>`(v2.1 系列)
  V2 手动一帧    —— TELEM 应回 一帧统一帧 + `ACK <seq> OK <upMs>`
  V3 持续拉取    —— 用 PullRequester 拉 N 秒, 记录帧率/窗口/丢帧
  V4 **自愈验证** —— 停发后静默, 看设备能否不加电恢复（用户提出的假设）
  V5 复拉        —— 恢复后再拉一小段, 确认可重复

用法: python host/real_v21.py COM7 [拉取秒数]
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

MOTOR_CMDS = ("MOVE", "MAZE RUN", "MCU RESET", "CAL", "TARE")


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "COM7"
    pull_s = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0

    from host.api import MouseClient
    from host.protocol.codec import UNIFIED_LEN

    print("=" * 74)
    print(f"真机 v2.1 验证  端口={port}   (只发 PING / TELEM, 不驱动电机)")
    print("=" * 74)

    # ---------------------------------------------------------------- V1
    print("\n[V1] 连接 + 指纹")
    # 先不开自动拉取, 逐个阶段手动控制
    mc = MouseClient(auto_session=False, auto_stream=False)
    if not mc.connect(port):
        print("  连接失败:", mc.state_detail)
        return 1
    r = mc.send_cmd("ping", timeout=5.0)
    print(f"  PING -> ok={r.ok} status={r.status!r}")
    dev = mc.device
    print(f"  指纹 = {dev}")
    ok_v1 = dev.get("proto") == 3 and (dev.get("fw") or 0) >= 210
    print(f"  {'OK ' if ok_v1 else '!! '}期望 proto=3 / fw>=210 (v2.1 系列; 210=初版 211=含 MAZE STOP)")

    # ---------------------------------------------------------------- V2
    print("\n[V2] 手动拉一帧 (TELEM)")
    n0 = mc.stats().get("unified", 0)
    t0 = time.time()
    r = mc.send_cmd("telem", timeout=6.0)
    dt = (time.time() - t0) * 1000
    n1 = mc.stats().get("unified", 0)
    print(f"  TELEM -> ok={r.ok} status={r.status!r}  往返 {dt:.0f} ms")
    print(f"  帧计数 {n0} -> {n1}  (期望 +1)")
    print(f"  时间锚 = {mc.store.get_anchor()}")
    f = mc.telemetry.latest()
    print(f"  通道数 = {len(f)}   帧长应 = {UNIFIED_LEN}+8")
    if f:
        print("  关键字段:", {k: f.get(k) for k in
                           ("ms8", "speedL", "speedR", "irF1", "irL", "battMv",
                            "wallLeft", "wallFront", "wallRight", "mazeTask", "flags")})

    # ---------------------------------------------------------------- V3
    print(f"\n[V3] 持续拉取 {pull_s:.0f}s (用正式 PullRequester)")
    a0 = mc.stats().get("unified", 0)
    mc.stream.on()
    print(f"  拉取器 = {mc.stream.stats()}")
    t0 = time.time()
    last = t0
    marks = []
    while time.time() - t0 < pull_s:
        time.sleep(1.0)
        st = mc.stream.stats()
        sc = mc.stats()
        el = time.time() - t0
        print(f"    t={el:5.1f}s  帧 {sc.get('unified',0)-a0:5d} "
              f"({(sc.get('unified',0)-a0)/el:6.2f} Hz)  在途 {st['inflight']:2d}  "
              f"丢 {st['lost']:3d}  bad_crc {sc.get('bad_crc',0)}")
        marks.append((el, sc.get("unified", 0) - a0, st["inflight"], st["lost"]))
    a1 = mc.stats().get("unified", 0)
    el = time.time() - t0
    st = mc.stream.stats()
    hz = (a1 - a0) / el
    print(f"  合计 {a1-a0} 帧 / {el:.1f}s = {hz:.2f} Hz  (≈ {hz*53:.0f} B/s 载荷)")
    print(f"  最终 模式={st.get('mode')} 在途={st['inflight']} sent={st['sent']} "
          f"done={st['done']} lost={st['lost']} 实测Hz={st['hz']}")

    # ---------------------------------------------------------------- V4
    print("\n[V4] **自愈验证**: 停拉取, 静默让缓冲排空, 再探活")
    mc.stream.off()
    print(f"  已停拉取 (拉取器 running={mc.stream.is_running if hasattr(mc.stream,'is_running') else mc.stream.is_on})")
    time.sleep(0.5)
    n_stop = mc.stats().get("unified", 0)
    for w in (2.0, 3.0, 5.0):
        time.sleep(w)
        n_now = mc.stats().get("unified", 0)
        print(f"    静默 +{w:.0f}s: 帧计数 {n_stop} -> {n_now} "
              f"({'OK 确实停了' if n_now == n_stop else '!! 仍在来帧'})")
    print("  现在发 PING 探活(不发 TELEM):")
    healed = False
    for i in range(3):
        r = mc.send_cmd("ping", timeout=6.0)
        print(f"    第 {i+1} 次 PING -> ok={r.ok} status={r.status!r}")
        if r.ok:
            healed = True
            break
    print(f"  >>> {'**自愈成功**: 停发后设备恢复应答, 无需断电' if healed else '停发后仍未恢复'}")

    # ---------------------------------------------------------------- V5
    print("\n[V5] 复拉 5s (确认可重复)")
    if healed:
        b0 = mc.stats().get("unified", 0)
        mc.stream.on()
        t0 = time.time()
        while time.time() - t0 < 5.0:
            time.sleep(1.0)
            print(f"    t={time.time()-t0:4.1f}s  帧 {mc.stats().get('unified',0)-b0}  "
                  f"在途 {mc.stream.stats()['inflight']}  丢 {mc.stream.stats()['lost']}")
        st = mc.stream.stats()
        el = time.time() - t0
        print(f"  复拉 {(mc.stats().get('unified',0)-b0)} 帧 / {el:.1f}s "
              f"= {(mc.stats().get('unified',0)-b0)/el:.2f} Hz  丢 {st['lost']}")
        mc.stream.off()
    else:
        print("  (跳过: 未自愈)")

    mc.close()
    print("\n" + "=" * 74)
    print("结论速览")
    print(f"  V1 指纹 proto=3/fw>=210 : {'OK' if ok_v1 else '不符(固件是否已烧 v2.1?)'}")
    print(f"  V2 单帧往返           : {dt:.0f} ms")
    print(f"  V3 持续帧率           : {hz:.2f} Hz (停等, 在途 {st['inflight']}, 丢 {st['lost']})")
    print(f"  V4 停发自愈           : {'成功' if healed else '**未恢复**'}")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
