"""量化 v2.1 停等拉取下, 课程实验能采到多少帧 —— 判断"实验波形"这条链路是否还够用。

背景: 实验三(编码器测距)跑 100mm @ 50cm/s ≈ 0.2s。
v2.0 设想统一帧 217 Hz -> 0.2s 内约 43 点(够算阶跃指标 σ%/ts/tp)。
v2.1 真机停等实测 4.4 Hz  -> 0.2s 内约 1 点。
本脚本在模拟器上量出"实验时长 vs 采样点数"的实际关系(模拟器 RTT 远小于真机,
所以这里给出的是**乐观上界**; 真机只会更差)。

只跑课程实验(会驱动电机, 但只在模拟器上, 无实物风险)。
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main() -> int:
    from host.api import MouseClient

    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "host.sim.mouse_sim", "--port", str(port), "--no-auto"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.2)
    try:
        mc = MouseClient(auto_session=False, auto_stream=True)
        if not mc.connect(f"tcp://127.0.0.1:{port}"):
            print("连接失败:", mc.state_detail)
            return 1
        mc.telemetry.wait_first_frame(timeout=5.0)
        time.sleep(1.0)
        print("=" * 76)
        print(f"拉取实测帧率 = {mc.telemetry.hz():.1f} Hz (模拟器; 真机约 4.4 Hz)")
        print("=" * 76)
        print(f"{'实验':>6} {'距离mm':>8} {'时长ms':>8} {'采样点':>7} {'等效Hz':>8}  够算阶跃指标?")
        print("-" * 76)
        for dist in (100, 300, 600):
            res = mc.lesson.run(2, v=50, dist=dist, ms=6000)
            dur = res.elapsed_ms or 0
            pts = res.log_points or 0
            eff = (pts / (dur / 1000.0)) if dur else 0
            enough = "是" if pts >= 20 else ("勉强" if pts >= 8 else "**否**")
            print(f"{'三':>6} {dist:>8} {dur:>8} {pts:>7} {eff:>8.1f}  {enough}")
            time.sleep(0.5)

        # 真机外推
        print("\n真机外推(按 4.4 Hz):")
        for dist in (100, 300, 600, 1000):
            dur_ms = dist / 10.0 / 50.0 * 1000  # mm / (cm/s -> mm/s) -> s -> ms
            pts = 4.4 * dur_ms / 1000.0
            print(f"  dist={dist:5d}mm  时长≈{dur_ms:6.0f}ms  ->  约 {pts:5.1f} 个点")
        mc.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("\n判读: 若短实验只有个位数个点, 则'实验波形算参'在真机上不可用 ——")
    print("      这正是 v1.5.5 用 SCOPE(1kHz 本地录制 + 停车后回传)要解决的问题。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
