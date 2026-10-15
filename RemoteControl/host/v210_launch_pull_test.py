"""v2.10 真机探针: **拉取中执行命令(发车)** —— 「发车回 ERR_BUSY」复现/定位。

现场报告(下游 Control): 上位机在**持续拉取遥测的同时**下发 `MAZE RUN`(发车),
界面显示 BUSY。本脚本用同一套 `MouseClient` 门面把这条路径单独跑一遍, 逐轮记录:

  · `maze.run()` 的 `ok / status / attempts / 耗时` —— attempts>1 说明首条 ACK 没回来
  · 发车**是否真的生效** —— 帧里 `mazeTask` 是否离开 `WAIT`、电机是否真的转
  · 拉取器在发车窗口内的帧数 / 丢批 / 断连事件
  · 收车(`MAZE STOP`)后状态**是否真的复位** —— 没复位就是下一次 `ERR_BUSY` 的直接原因

三种拉取工况各跑 N 轮:
  · `off`   不发拉取(对照组; 观察窗口内按需 `TELEM` 取帧)
  · `telem` 停等(1 命令 1 帧, 约 1/RTT Hz)
  · `pace`  批式 10Hz(`RATE PULL 3 100`, v2.9/v2.10 默认工作档)

判据来源(**不写死**, 与固件同源): `link.c` 里 `MAZE RUN` 的忙闸是
`linkMazeReq || linkMazeRun` ⇒ `ERR_BUSY` 只可能出自两种状态:
  ① `linkMazeRun=1` —— 车**真的在跑迷宫**(收车没复位 / 重复发车);
  ② `linkMazeReq=1` —— 发车请求已被受理但主循环(1ms 内)尚未消费。
另有 `ERR_ESTOP`(急停锁存)与 `ERR_LOWBATT`(上位机低电闸, 压根没发链路)两种,
本脚本一并区分, 不许混成一句。

⚠️ 安全纪律(沿用本项目真机脚本惯例):
  · `--motor` 才真的发车; **电脑鼠必须悬空**或场地空旷。不加 `--motor` 只做状态审计。
  · 发车前打印风险并**倒数 3 秒**; 全程不主动发任何运动命令(发车本身除外)。
  · `--force` 把上位机低电闸(`registry.LAUNCH_MIN_MV`)临时置 0 —— 与逃生舱
    `send_raw("MAZE RUN")` 等价, **只绕上位机、不绕固件**; 供电不足时电机行为不确定,
    所以它只在"电脑鼠已悬空"的前提下有意义。默认关闭。

★ 硬保证 —— **只要用了 `--motor`, 脚本退出时一定是"待命"态, 不留运行中的车**:
  · 收车分级升级, 每步都拿**新鲜帧**验证(不是发完就算):
      `MAZE STOP` → `ESTOP`(立刻砍电机) → `MCU RESET`(整机软复位)。
  · 只要发过任何收车命令, 最后**必补一次 `MCU RESET`** —— 因为 `MAZE STOP`/`ESTOP`
    会置 `linkMazeAbort`, 而该位**全固件无处清除**(见 link.c 只有 3 处 `= 1`),
    不复位则下一次 `LESSON_Blind8()` 进门第一次检查就返回(车"发车 OK 却不动")。
    `MCU RESET` 是唯一能把"停车 + 清 abort + 复位发车状态"一次做干净的手段。
  · 触发点覆盖**所有退出路径**: 正常结束、抛异常、Ctrl-C / SIGTERM(已装信号处理),
    以及 `S0b` 自检结束 —— 任何一条路径都走同一个安全出口。
  · 万一连 `MCU RESET` 都确认不了待命, 脚本会**大字警告并要求人工断电**。
  · 只想发车不想复位? `--no-reset`(不推荐, 那就只剩 STOP/ESTOP 这种不可靠手段)。
  · 紧急情况(车在乱跑、脚本已退出): 单跑 `python mouse_stop.py COM7` 立刻收车。

★ 待命判据的加固(2026-10-16 第二轮真机):
  · **空帧 ≠ 待命**: 链路不通时取不到帧, 绝不能当成"车已停" —— 否则安全出口会
    **谎报"已确认待命"**(真机踩到: 设备掉线时却报了成功)。读不到状态一律按"可能在跑"
    处理, 最终诚实报"未能确认待命, 请人工断电"。
  · **刚发过车 ⇒ `assume_busy=True`**: 跳过"本来就待命"这条会**跳过全部收车命令**的
    快捷路径, 无条件走分级(宁可多复位一次, 绝不赌)。S1 轮内/工况收尾/退出兜底/S0b[5] 全启用。
  · "本来就待命"快捷路径本身也要求**连续 2 帧非空空闲帧**(单帧过渡态会误判)。
  · 收车每一步各自 try/except —— 单点命令异常(含 core 让路异常)不得中断分级收车。
  · `MCU RESET` 后若 `mode != "off"` 会**按原工况重新拉起拉取**, 否则下一轮变成
    "无拉取发车", 被测场景就不成立了。

用法(在 host/ 目录下):
    python v210_launch_pull_test.py COM7                    # 只做状态审计(不发车)
    python v210_launch_pull_test.py COM7 --motor            # 三工况 × 5 轮发车
    python v210_launch_pull_test.py COM7 --motor --force    # 低电时仍发(悬空前提)
    python v210_launch_pull_test.py COM7 --motor --modes pace --cycles 8
    python v210_launch_pull_test.py COM7 --motor --double-probe   # 追加"连发两车"探针
    python v210_launch_pull_test.py COM7 --motor --busy-probe      # 先跑 S0b 请求消费自检
    python v210_launch_pull_test.py COM7 --motor --show-cmdlog    # 附原始命令/应答日志
    python v210_launch_pull_test.py COM7 --motor --no-reset       # 禁止 MCU RESET 兜底(不推荐)

----------------------------------------------------------------------------
2026-10-16 真机修正记录(三件事, 都是本脚本自己的毛病):

  ① 判据读的是**陈旧帧**: 旧 `poll_frame` 调 `mc.cmd.telem(wait=True, ...)`, 而真签名是
     `telem(timeout=)` —— 关键字不存在, 抛的异常被 `except Exception: pass` **静默吞掉**,
     于是 `off` 工况下 `latest()` 永远停在上一次的值上 ⇒ 车明明在跑, 却被判成"没跑"。
     现在: 先记帧计数, 按需发一条 `TELEM`(一次一条不重发), **等计数真的变了**才算新鲜帧。

  ② 退出时**没有可靠收车**: 旧 `finally` 只调一次 `ensure_idle(..., 4.0)`, 而它的判据同样
     读陈旧帧 ⇒ 要么误判"本来就空闲"而**根本不发收车命令**, 要么判据永不满足、超时放弃
     ⇒ 车一直转到脚本结束后(真机踩到)。现在改为分级升级 + 每步新鲜帧验证 + 硬复位兜底。

  ③ **`MAZE STOP` 在"阻塞等待"里本就不可靠**(registry `maze.stop` 的原话: "常见现象是回 OK
     但 task 仍在 SEARCH; 真正可靠的收车是 `system.reset`")。迷宫主循环里是**非协作式阻塞
     等待**(等编码器里程/传感器), 退出标志常常轮不到; 而一旦用 `ESTOP` 把电机砍停, "等里程"
     的条件反而永不成立 ⇒ 程序永久卡住。**所以可靠收车只有 `MCU RESET`**, 前面两步只是
     "先把车物理停住"的减损动作。
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from host.api import MouseClient  # noqa: E402
from host.api.events import Event  # noqa: E402

#: 固件回"已在跑迷宫"的拒绝码(见 link.c: linkMazeReq || linkMazeRun)
BUSY = "ERR_BUSY"


def hr(title: str = "") -> None:
    print("\n" + "-" * 74)
    if title:
        print(title)
        print("-" * 74)


class ErrWatch:
    """订阅 `Event.ERROR`, 分门别类记账(断连是重点)。"""

    def __init__(self, mc: MouseClient) -> None:
        self.kinds: List[str] = []
        self.link_lost: List[Any] = []
        self._off = mc.on(Event.ERROR, self._on)

    def _on(self, m) -> None:
        d = getattr(m, "data", None) or {}
        k = str(d.get("kind", "?"))
        self.kinds.append(k)
        if k == "link_lost":
            self.link_lost.append(m)

    def since(self, n: int) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for k in self.kinds[n:]:
            out[k] = out.get(k, 0) + 1
        return out

    def close(self) -> None:
        try:
            self._off()
        except Exception:
            pass


# ================================================================ 观测辅助

def fmt_state(mc: MouseClient) -> str:
    s = mc.maze.state() or {}
    f = mc.telemetry.latest() or {}
    return (f"maze={s.get('taskName', '?'):<7} motion={s.get('motionName', '?'):<9} "
            f"road={s.get('roadName', '?'):<6} pos=({s.get('posX')},{s.get('posY')}) "
            f"spd=({f.get('speedL', 0):+.0f},{f.get('speedR', 0):+.0f}) "
            f"estop={bool(f.get('estop_lock'))} batt={f.get('battMv', 0)}mV")


# ============================================================ 新鲜帧 / 待命判据

def frame_total(mc: MouseClient) -> int:
    """已收到的统一帧**累计条数** —— 用它判"手上这一帧是不是新的"。"""
    try:
        return int(mc.store.unified.total)
    except Exception:
        try:
            return int(mc.telemetry.samples())
        except Exception:
            return -1


def fresh_frame(mc: MouseClient, mode: str, timeout: float = 2.0) -> Dict[str, Any]:
    """确保拿到**新**帧后返回它(扁平 dict)。读不到时返回 `{}`。

    ⚠️ 为什么非要"新": `latest()` 只是最近一帧的**缓存**, 链路一停它就永远停在上一次
    的值上。老实现用 `mc.cmd.telem(wait=True, ...)` 取帧 —— 该关键字并不存在(真签名是
    `telem(timeout=)`), 抛的异常被静默吞掉 ⇒ 判据一直读**陈旧帧** ⇒ "车在跑"被误判成
    "没跑"(2026-10-16 真机踩到)。这里改成: 记下帧计数 → 按需发一条 `TELEM`(停等式最小
    形态, 一次一条、不重发) → **等计数真的变了**才算数。
    """
    n0 = frame_total(mc)
    deadline = time.time() + max(0.3, timeout)
    while time.time() < deadline:
        if mode == "off":
            try:
                mc.cmd.telem(timeout=min(1.5, max(0.3, timeout)))
            except Exception:
                pass
        if frame_total(mc) != n0 and mc.telemetry.latest():
            break
        time.sleep(0.03)
    return mc.telemetry.latest() or {}


def running_now(f: Dict[str, Any]) -> bool:
    """由**帧**判"车在跑": 迷宫任务机 / 运动机任一非空闲, 或轮速非零。

    码值来源(不写死, 与固件同源): `MAZE_TASK_NAMES[0]="WAIT"`、`MOTION_TASK_NAMES[0]="NULL"`。
    """
    if not f:
        return False
    return (int(f.get("mazeTask", 0) or 0) != 0
            or int(f.get("motionTask", 0) or 0) != 0
            or abs(float(f.get("speedL", 0) or 0)) > 3.0
            or abs(float(f.get("speedR", 0) or 0)) > 3.0)


def poll_frame(mc: MouseClient, mode: str) -> None:
    """兼容旧调用点: 刷新一帧(内部即 `fresh_frame`)。"""
    fresh_frame(mc, mode, 1.2)


def set_pull(mc: MouseClient, mode: str) -> str:
    if mode == "off":
        r = mc.stream.off()
    elif mode == "telem":
        r = mc.stream.on()
    elif mode == "pace":
        r = mc.stream.pace()
    else:
        raise ValueError(f"未知工况: {mode!r}")
    time.sleep(1.5)
    return f"{str(r.status)} -> mode={mc.stream.mode}"


# ============================================================ 收车(安全关键)

def _wait_idle(mc: MouseClient, mode: str, seconds: float) -> bool:
    """在**新鲜帧**上反复确认"车已停" —— 拿到一次**非空**空闲帧即返回 True。

    ⚠️ 空帧(`fresh_frame` 读不到, 例如链路断了/设备掉线)**不算待命** —— 否则链路
    不通会被误判成"车已停", 收车会**谎报成功**(2026-10-16 真机踩到: 设备掉线时
    安全出口却打印"已确认待命")。读不到状态就必须按"可能在跑"处理。
    """
    t0 = time.time()
    while time.time() - t0 < max(0.0, seconds):
        try:
            f = fresh_frame(mc, mode, 1.2)
            if f and not running_now(f):
                return True
        except Exception:               # 判据不得因单帧异常而崩掉收车
            pass
        time.sleep(0.15)
    return False


def _confirm_idle(mc: MouseClient, mode: str, need: int = 2,
                  gap: float = 0.30) -> bool:
    """**连续 `need` 帧**都空闲才算待命(每帧都要求是新鲜的**非空**帧)。

    ⚠️ 为什么要连续两帧: "本来就待命"这条快捷路径会**跳过全部收车命令**。单帧
    过渡态(例如发车瞬间刚复位完、任务机还没起来)就足以把它误判成空闲 ⇒ 车在跑
    却不发收车命令(2026-10-16 真机事故的形态)。两帧 + 间隔能把误判压到可忽略。
    ⚠️ 空帧(链路不通)一律**不算待命** —— 读不到就不能说"已停"。
    """
    for k in range(max(1, need)):
        try:
            f = fresh_frame(mc, mode, 1.5)
        except Exception:
            return False
        if not f or running_now(f):
            return False
        if k + 1 < need:
            time.sleep(gap)
    return True


def _hard_reset(mc: MouseClient, mode: str, verbose: bool = True) -> bool:
    """`MCU RESET`: 整机软复位 —— 唯一能把"停车 + 清 `linkMazeAbort` + 复位发车状态"
    一次做干净的手段(复位后程序从 `main()` 重跑: `freemode=1` 电机恒 0、`mazeTask=WAIT`)。

    ⚠️ 这条命令**可能"抛异常但已生效"**: 固件先 ACK 再复位, 而 `send_cmd` 收尾的
    让路恢复(重发 `RATE PULL`)会撞上固件 ~3.2s 的重启窗口。所以异常一律**按已生效
    处理、继续往下验证待命**, 绝不因此中断收车。
    """
    try:
        r = mc.send_cmd("system.reset", timeout=6.0)
        status = str(r.status)
    except Exception as e:
        status = f"EXC:{type(e).__name__}(按已生效处理)"
    if verbose:
        print(f"      ③ MCU RESET -> {status}  (复位后首帧约 3.2s)")
    time.sleep(5.0)
    ok = _wait_idle(mc, mode, 6.0)
    if mode != "off":
        # 复位打断了拉取(固件重启, resume 未必赶上), 按原工况重新拉起 ——
        # 否则下一轮就成了"无拉取发车", 被测场景不成立。
        try:
            set_pull(mc, mode)
        except Exception:
            pass
    return ok


def force_idle(mc: MouseClient, mode: str = "off", *,
               stop_timeout: float = 3.5, estop_timeout: float = 3.0,
               allow_reset: bool = True, assume_busy: bool = False,
               verbose: bool = True
               ) -> Tuple[bool, str]:
    """**无条件**把电脑鼠带回待命, 返回 `(是否已确认待命, 说明)`。

    分级升级, 每一步都用**新鲜帧**验证(不是发完就算):
      ① `MAZE STOP` —— 优雅收车(顺带解锁急停)。⚠️ **在阻塞等待里不可靠**:
         迷宫主循环是非协作式阻塞(等编码器里程/传感器), 退出标志常常轮不到
         (registry `maze.stop` 的原话: "回 OK 但 task 仍在 SEARCH")。
      ② `ESTOP` —— 立刻把电机砍到 0(置 `freemode`)并锁存, 先把车**物理**停住。
         ⚠️ 副作用: 电机一停, "等里程"的条件**永不成立** ⇒ 程序可能永久卡住。
      ③ `MCU RESET` —— 整机软复位。因为 ①/② 置的 `linkMazeAbort` **全固件无处清除**
         (link.c 只有 3 处 `= 1`、无清除点), **只要发过 ①/② 就必补 ③** —— 否则下一次
         `LESSON_Blind8()` 进门第一次检查就返回(车"发车 OK 却不动")。

    本来就待命时**不发任何命令**(避免用 `MAZE STOP` 白白污染 abort 位)。

    `assume_busy=True` 时**跳过**上面这条快捷路径, 无条件走分级收车 —— 用于
    "本脚本刚发过车"的场合: 宁可多花一次复位, 也绝不冒"车在跑却被判成待命"的险。
    """
    if not assume_busy and _confirm_idle(mc, mode, 2, 0.30):
        return True, "本来就待命(连续 2 帧空闲, 未发任何收车命令)"
    if verbose:
        print("      (未确认待命 —— 按「可能在跑」处理, 开始分级收车)")

    tried: List[str] = []

    # ① MAZE STOP
    try:
        r = mc.maze.stop()
        tag = f"MAZE STOP[{r.status}]"
    except Exception as e:            # 单点异常**不得**中断分级收车
        tag = f"MAZE STOP[EXC:{type(e).__name__}]"
    tried.append(tag)
    if verbose:
        print(f"      ① {tag}")
    if _wait_idle(mc, mode, stop_timeout):
        if allow_reset and _hard_reset(mc, mode, verbose):
            return True, "MAZE STOP 停住 + MCU RESET 清场; " + " ".join(tried)
        return True, "MAZE STOP 生效; " + " ".join(tried)

    # ② ESTOP —— 先把电机砍停(物理减损)
    if verbose:
        print(f"      (MAZE STOP 未能在 {stop_timeout:.1f}s 内停住 —— 升级 ESTOP)")
    try:
        r = mc.send_cmd("estop")
        tag = f"ESTOP[{r.status}]"
    except Exception as e:
        tag = f"ESTOP[EXC:{type(e).__name__}]"
    tried.append(tag)
    if verbose:
        print(f"      ② {tag}  (电机立即置 0)")
    _wait_idle(mc, mode, estop_timeout)

    # ③ MCU RESET —— 必做(①/② 已置粘滞的 linkMazeAbort)
    if allow_reset:
        if _hard_reset(mc, mode, verbose):
            return True, "ESTOP 停车 + MCU RESET 复位; " + " ".join(tried)
        return False, "**未能确认待命** —— 请立即人工断电! " + " ".join(tried)
    f = fresh_frame(mc, mode, 1.5)
    still = (not f) or running_now(f)     # 读不到状态 ⇒ 按"可能在跑"处理
    return (not still), ("已发收车命令但**未复位**(--no-reset); " + " ".join(tried)
                         + ("  ⚠️ 可能仍卡在阻塞等待里" if still else ""))


def ensure_idle(mc: MouseClient, mode: str, timeout: float,
                assume_busy: bool = False) -> Tuple[bool, float, str]:
    """S1 矩阵的**发车前清场**(保留旧三元组签名, 安全语义同 `force_idle`)。

    ⚠️ 这里**必须**能真的把车停住 —— 上一版靠陈旧帧判断, 误判"本来就空闲"于是**根本没发
    收车命令**, 直接导致"脚本结束后车还在转"。现在一律交给 `force_idle`。

    `timeout` 形参保留只为兼容调用点(实际用 `force_idle` 内部的分级时限)。
    """
    t0 = time.time()
    ok, note = force_idle(mc, mode, allow_reset=True,
                          assume_busy=assume_busy, verbose=False)
    return ok, time.time() - t0, note


def cmdlog_window(mc: MouseClient, t0: float, keys=("MAZE", "ACK")) -> List[str]:
    out: List[str] = []
    for ts, d, txt in list(mc.cmd.log):
        if ts >= t0 and any(k in txt for k in keys):
            out.append(f"      {d} {txt.strip()}")
    return out


# ================================================================ S0b 请求消费自检

def busy_probe(mc: MouseClient, mode: str, observe: float) -> Dict[str, Any]:
    """分步分离「回 OK 却没跑」与「ERR_BUSY」—— 判 `MAZE RUN` 请求到底被谁消费。

    背景与结论(2026-10-16, 两轮真机):

      · **缺陷真实存在**(v2.7.0 `cf2fce2` 起): `USER/main.c` 值守循环的按键分支把
        `BUTTON1` 极性写反。真值 **idle=0 / 按下=1**, 三方佐证(缺一不可):
          - `menu.c:33` `if (BUTTON1 == 0 && ispressed == 0)` 是"**4s 未按键自动进主流程**"
            的倒计时(keydelay 数到 200×20ms=4s 才 break) ⇒ 只有 idle=0 才成立;
          - `menu.c:58/105` `if (BUTTON1)` / `while (BUTTON1);` = 按下 / 等松开 ⇒ 按下=1;
          - `calibrate.c:190` `BUTTON1 == 1 // 键被按下`、`lesson.c:217` 同。
        原代码写 `if (BUTTON1 == 0) /*按下*/ ... while (BUTTON1 == 0) /*等松开*/`
        ⇒ **空闲时即进分支并在内层永久自旋** ⇒ main 永不执行 `LINK_MazeStarted()`。
        其父提交(v2.6)的循环只有 `while (!LINK_MazeRequested()) DELAY_Ms(1);`。

      · **修复 = 三处 `== 0` 改 `== 1`**(全在 `#ifdef MOUSE_REMOTE` 内 ⇒ 比赛档 hex 不变)。
        真机验证(2026-10-16, fw 222): **车真的跑起来了** —— 这同时**反证 idle=0**
        (若 idle=1, 新代码的 `if (BUTTON1 == 1)` 在空闲时恒真、又会自旋, 车不可能跑)。

      · ⚠️ **本函数第一版踩了坑**: 判据读的是**陈旧帧**(原因见 `fresh_frame` 的说明),
        于是"车在跑"被误判成"没跑"、`finally` 也据此跳过了收车 ⇒ 车转到脚本结束后。
        看本函数输出请以 `task=` / `motion=` / `maxSpd=` 三项**新鲜帧**读数为准。

    故障指纹(`ERR_BUSY` 的**两种**来源, 必须分开):
      ① `linkMazeRun=1` —— 车**真的在跑**(正常拒绝; 想再发车得先收车);
      ② `linkMazeReq=1` —— 请求已受理但 main 没消费(缺陷状态)。

    只跑它不发运动命令(除发车本身), 用来给"修复前/修复后"留一对可比的证据。
    """
    print("\n" + "-" * 74)
    print("S0b  发车请求消费自检（分离「ERR_BUSY」与「回 OK 却没跑」）")
    print("-" * 74)

    def obs(secs: float) -> bool:
        seen: List[str] = []
        mseen: List[str] = []
        mx = 0.0
        ran = False
        t = time.time()
        while time.time() - t < secs:
            f = fresh_frame(mc, mode, 1.5)      # ★ 必须是**新**帧(旧版读缓存 ⇒ 误判)
            s = mc.maze.state() or {}
            if s:
                seen.append(str(s.get("taskName")))
                mseen.append(str(s.get("motionName")))
            mx = max(mx, abs(float(f.get("speedL", 0) or 0)),
                     abs(float(f.get("speedR", 0) or 0)))
            if running_now(f):
                ran = True
            time.sleep(0.2)
        ran = ran or any(x not in ("WAIT", "?", "") for x in seen) or mx > 3.0
        if not seen and mx <= 3.0:
            print("        → ⚠️ 期间**读不到遥测帧**(链路不通?) —— 无法判定, 不能算'没跑'")
            return False
        print(f"        → mazeTask 见 {sorted(set(seen))}  motion 见 {sorted(set(mseen))}"
              f"  maxSpd={mx:.0f}  判定: {'**跑了**' if ran else '**没跑**'}")
        return ran

    def step(name: str, fn):
        t0 = time.time()
        r = fn()
        d = r.data if isinstance(r.data, dict) else {}
        print(f"  {name}: ok={r.ok} status={r.status} "
              f"attempts={d.get('attempts')} t={(time.time()-t0)*1000:.0f}ms")
        return r

    print("  [1] 先 `MCU RESET` 清掉 BSS 里的发车标志, 再发车")
    step("MCU RESET", lambda: mc.send_cmd("system.reset", timeout=6.0))
    time.sleep(5.0)
    poll_frame(mc, mode)
    r1 = step("MAZE RUN", lambda: mc.maze.run("BLIND"))
    ran1 = obs(2.5)

    print("  [2] 立刻再发一车（若 [1] 真在跑, 这里应当 ERR_BUSY）")
    r2 = step("MAZE RUN", lambda: mc.maze.run("BLIND"))
    obs(1.0)

    print("  [3] 收车（MAZE STOP）后再发车")
    step("MAZE STOP", lambda: mc.maze.stop())
    time.sleep(1.5)
    r3 = step("MAZE RUN", lambda: mc.maze.run("BLIND"))
    ran3 = obs(2.5)

    print("  [4] 再 `MCU RESET` 后发车（与 [1] 对照, 验证「只有复位能恢复」）")
    step("MCU RESET", lambda: mc.send_cmd("system.reset", timeout=6.0))
    time.sleep(5.0)
    poll_frame(mc, mode)
    r4 = step("MAZE RUN", lambda: mc.maze.run("BLIND"))
    ran4 = obs(2.5)

    # ---------------- 判定 ----------------
    accepted_no_run = bool(r1.ok and not ran1)
    stuck = bool(accepted_no_run and r2.status == BUSY)
    print("\n  ── 判定 ──")
    if ran1:
        print("  · 首次发车**真的跑起来了** ⇒ `main()` 在正常消费发车请求"
              "(`LINK_MazeStarted()` 执行了) ⇒ 发车通路是通的。")
        if r2.status == BUSY:
            print("  · 再发一车回 **ERR_BUSY** ⇒ 这是**正确**的拒绝: 车确实还在跑"
                  "(`linkMazeRun=1`), 忙闸按设计生效 —— 不是故障。")
        print("  ⚠️ 所以『发车时显示 BUSY』要**分两种**看: 车已在跑时 BUSY 是正确应答;")
        print("     只有『车没跑、还回 BUSY』(=`linkMazeReq` 挂在原地)才是缺陷。")
    elif accepted_no_run:
        print("  · 首次发车 **回 OK 却没跑** ⇒ 发车请求被链路层受理(`linkMazeReq=1`)"
              "但**没被 main 消费** —— main 没走到 `while (!LINK_MazeRequested())`。")
        print("  ⇒ 定位方向: `USER/main.c` 值守循环(按键分支)或其之前的初始化 ——"
              " 见该文件 v2.7.1 注释里的三方极性佐证。修好后本函数 [1] 应变成「跑了」。")
    else:
        print("  · 首次发车未通过其他判据 —— 看上面每步的 status。")
    if r3.ok and not ran3:
        print("  ⚠️ 收车后发车 **回 OK 却没跑** ⇒ `linkMazeAbort` 粘滞: `MAZE STOP`/`ESTOP`"
              " 会把它置 1, 而**全固件无处清除**(link.c 只有 3 处 `= 1`) ⇒ 新发车被"
              " `LESSON_Blind8()` 进门第一次检查就返回。⇒ 反复发车时中间**必须**"
              " `MCU RESET` 清掉它。")
    if r4.ok and not ran4:
        print("  ⚠️ 复位后发车依旧不跑 ⇒ 与 [1] 同因。")

    # ★ 安全出口(自检内): [4] 已经发出去的车必须停干净 —— 等级同主流程
    print("\n  [5] 自检收车(分级: MAZE STOP → ESTOP → MCU RESET)")
    # [4] 刚发过车 ⇒ assume_busy: 无条件分级收车, 绝不赌"本来就待命"
    guard_ok, note = force_idle(mc, mode, allow_reset=True,
                                assume_busy=True, verbose=True)
    print(f"      → {'已确认待命' if guard_ok else '**未确认待命**'}: {note}")
    return {"accepted_no_run": accepted_no_run, "stuck": stuck,
            "ran1": ran1, "ran3": ran3, "ran4": ran4, "guard_ok": guard_ok}


# ================================================================ 单轮发车

def launch_once(mc: MouseClient, mode: str, observe: float,
                show_log: bool) -> Dict[str, Any]:
    poll_frame(mc, mode)                     # 刷新一帧(precheck 要读 battMv)
    b = mc.stream.stats()
    b_frames = int(b.get("frames", 0) or 0)
    b_lostb = int(b.get("lost_batches", 0) or 0)

    t0 = time.time()
    r = mc.maze.run("BLIND")
    dt = time.time() - t0

    # ---- 观察窗口: 发车到底有没有真的生效(必须用**新鲜帧**) ----
    seen: List[str] = []
    mseen: List[str] = []
    max_spd = 0.0
    ran_flag = False
    t1 = time.time()
    while time.time() - t1 < observe:
        f = fresh_frame(mc, mode, 1.5)
        s = mc.maze.state() or {}
        if s:
            seen.append(str(s.get("taskName", "?")))
            mseen.append(str(s.get("motionName", "?")))
        max_spd = max(max_spd, abs(float(f.get("speedL", 0) or 0)),
                      abs(float(f.get("speedR", 0) or 0)))
        if running_now(f):
            ran_flag = True
        time.sleep(0.2)

    a = mc.stream.stats()
    d = r.data if isinstance(r.data, dict) else {}
    out = dict(
        ok=bool(r.ok), status=str(r.status),
        attempts=int(d.get("attempts", 0) or 0),
        blocked=bool(d.get("blocked")),
        batt=int((mc.telemetry.latest() or {}).get("battMv", 0) or 0),
        dt=dt,
        launched=(ran_flag or any(x not in ("WAIT", "?", "") for x in seen)
                  or max_spd > 3.0),
        no_telem=(not seen and max_spd <= 3.0),
        seen=sorted(set(seen)), motion_seen=sorted(set(mseen)), max_spd=max_spd,
        frames_delta=int(a.get("frames", 0) or 0) - b_frames,
        lostb_delta=int(a.get("lost_batches", 0) or 0) - b_lostb,
        hz=a.get("hz"), rtt=a.get("rtt_ms"), inflight=a.get("inflight"),
        dead=a.get("dead"), mode=mc.stream.mode,
    )
    out["log"] = cmdlog_window(mc, t0) if show_log else []
    return out


def row_str(i: int, m: Dict[str, Any]) -> str:
    flag = "BUSY" if m["status"] == BUSY else ("OK  " if m["ok"] else "FAIL")
    warn = "  ⚠️无遥测(链路?)" if m.get("no_telem") else ""
    return (f"  #{i}  {flag} {m['status']:<11} attempts={m['attempts']} "
            f"blocked={int(m['blocked'])} t={m['dt']*1000:5.0f}ms  "
            f"真的发车={int(m['launched'])} maxSpd={m['max_spd']:4.0f} "
            f"batt={m['batt']}mV  "
            f"frames+{m['frames_delta']:<4} lostb+{m['lostb_delta']} "
            f"task={m['seen']} motion={m['motion_seen']}{warn}")


# ================================================================ 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="拉取中执行命令(发车)真机探针")
    ap.add_argument("port", help="串口, 如 COM7")
    ap.add_argument("--motor", action="store_true",
                    help="真的发车(电脑鼠必须悬空/场地空旷); 不加只做状态审计")
    ap.add_argument("--force", action="store_true",
                    help="临时把上位机低电闸 LAUNCH_MIN_MV 置 0(等价 send_raw 逃生舱)")
    ap.add_argument("--modes", default="off,telem,pace",
                    help="工况列表(逗号分隔), 默认 off,telem,pace")
    ap.add_argument("--cycles", type=int, default=5, help="每工况发车轮数, 默认 5")
    ap.add_argument("--observe", type=float, default=1.6,
                    help="每次发车后的观察窗口秒数, 默认 1.6")
    ap.add_argument("--stop-timeout", type=float, default=6.0,
                    help="收车等待上限秒数, 默认 6.0")
    ap.add_argument("--double-probe", action="store_true",
                    help="追加探针: 车已在跑时再发一车(预期 ERR_BUSY)")
    ap.add_argument("--busy-probe", action="store_true",
                    help="先跑 S0b 请求消费自检(复位/再发/收车/复位 四步), 分离"
                         "「回 OK 却没跑」与「ERR_BUSY」, 定位发车请求被谁消费")
    ap.add_argument("--show-cmdlog", action="store_true",
                    help="打印每次发车窗口内的原始命令/应答日志")
    ap.add_argument("--no-reset", action="store_true",
                    help="禁止用 MCU RESET 兜底(收车只到 STOP/ESTOP)。⚠️ 不推荐: "
                         "MAZE STOP 在阻塞等待里不可靠, 且会置粘滞的 linkMazeAbort, "
                         "关掉它就无法保证退出时是待命态。")
    args = ap.parse_args()

    # ★ 安全前置: 让 Ctrl-C / SIGTERM 也走 `finally` 的安全出口(默认 SIGINT 会抛
    #   KeyboardInterrupt → finally 执行; SIGTERM 默认直接杀进程 → 这里改成抛异常)。
    def _sig(signum, _frame):
        raise KeyboardInterrupt(f"收到信号 {signum} —— 走安全出口收车")
    for _s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(_s, _sig)
        except Exception:
            pass
    allow_reset = not args.no_reset

    modes = [x.strip().lower() for x in args.modes.split(",") if x.strip()]
    bad = [m for m in modes if m not in ("off", "telem", "pace")]
    if bad:
        print(f"未知工况: {bad} (只支持 off/telem/pace)")
        return 2

    mc = MouseClient(auto_session=False, auto_stream=True)
    if not mc.connect(args.port):
        print(f"连接 {args.port} 失败: {mc.state_detail}")
        return 1
    watch = ErrWatch(mc)
    rows: List[Dict[str, Any]] = []
    try:
        if not mc.telemetry.wait_first_frame(timeout=8.0):
            print("等首帧超时 —— 链路没起来, 中止")
            return 1

        # ---------------- S0 状态审计(只读) ----------------
        hr("S0  连接与当前状态审计(只读; 每次都先看这个)")
        dev = mc.device or {}
        print(f"  设备指纹 : proto={dev.get('proto')} fw={dev.get('fw')} "
              f"mazetype={dev.get('mazetype', dev.get('mazeType'))} "
              f"txbuf={dev.get('txbuf')} rst={mc.reset_cause}")
        print(f"  当前状态 : {fmt_state(mc)}")
        print(f"  拉取器   : mode={mc.stream.mode} {mc.stream.stats()}")
        s0 = mc.maze.state() or {}
        already = int(s0.get("task", 0)) != 0 or int(s0.get("motion", 0)) != 0
        print(f"  发车状态 : mazeTask={s0.get('taskName')} "
              f"motion={s0.get('motionName')}")
        if already:
            print("  ⚠️ 车**当前已在跑**(mazeTask/motion 非空闲) —— 此时任何 `MAZE RUN`")
            print("     都会（正确地）回 ERR_BUSY。先收车/复位再谈其他。")
        else:
            print("  车处于空闲等待态(WAIT) ⇒ 现在发车应当被接受。")
        from host import registry as _reg0
        _mv = int((mc.telemetry.latest() or {}).get("battMv", 0) or 0)
        print(f"  发车低电闸: battMv={_mv} mV / 下限 LAUNCH_MIN_MV={_reg0.LAUNCH_MIN_MV} mV"
              f" -> {'**会被拦(ERR_LOWBATT)**' if _mv and _mv < _reg0.LAUNCH_MIN_MV else '放行'}"
              f"   (--force 可临时置 0)")

        if not args.motor:
            print("\n[跳过] 发车测试(未加 --motor)。仅审计完毕。")
            return 0

        if args.force:
            # 与逃生舱 `send_raw("MAZE RUN")` 等价: 只绕**上位机**低电闸, 不绕固件。
            from host import registry as _reg
            _reg.LAUNCH_MIN_MV = 0
            print(f"\n  ⚠️ --force: 上位机低电闸已临时置 0"
                  f"(实读 {dev.get('battMv') or (mc.telemetry.latest() or {}).get('battMv')} mV)"
                  f" —— 仅绕上位机, 固件行为不变; 低电时电机行为不确定,"
                  f" 因此只在电脑鼠已悬空时可接受。")

        print("\n" + "!" * 74)
        print("即将真实发车(MAZE RUN) —— 请确认【电脑鼠悬空】或场地空旷。")
        print("!" * 74)
        for k in (3, 2, 1):
            print(f"    {k} ...")
            time.sleep(1.0)

        # ---------------- S0b 请求消费自检(可选) ----------------
        if args.busy_probe:
            probe_mode = modes[0]
            set_pull(mc, probe_mode)
            r = busy_probe(mc, probe_mode, args.observe)
            hr(f"S0b 小结 (工况={probe_mode})")
            print(f"  [1] 首次发车真的跑起来 : {r['ran1']}")
            print(f"  首次发车回 OK 却没跑   : {r['accepted_no_run']}")
            print(f"  再发必 BUSY(请求挂住)  : {r['stuck']}")
            print(f"  [3] 收车后仍没跑       : {(not r['ran3'])}")
            print(f"  [4] 复位后仍没跑       : {(not r['ran4'])}")
            print(f"  [5] 结束收车已确认待命 : {r['guard_ok']}")
            print("  (S0b 只做定性定位; 下面的 S1 矩阵给逐轮定量证据)")

        # ---------------- S1 拉取中的发车矩阵 ----------------
        for mode in modes:
            hr(f"S1[{mode}]  拉取工况 = {mode}   ({args.cycles} 轮发车)")
            r = set_pull(mc, mode)
            print(f"  拉取设置 : {r}")
            if mode != "off":
                time.sleep(1.5)
                st = mc.stream.stats()
                print(f"  拉取基线 : frames={st.get('frames')} "
                      f"batches={st.get('batches')} hz={st.get('hz')} "
                      f"rtt={st.get('rtt_ms')}ms inflight={st.get('inflight')}")

            for i in range(1, args.cycles + 1):
                n_before = len(watch.kinds)
                # assume_busy: 上一轮/上一工况刚发过车 ⇒ 无条件分级收车, 不赌"本来就待命"
                ok_idle, t_stop, note = ensure_idle(mc, mode, args.stop_timeout,
                                                    assume_busy=True)
                if not ok_idle:
                    print(f"  ⚠️ 清场失败({note}) —— 此时再发车必然 ERR_BUSY, "
                          f"本轮数据不可用, 记为「环境未清」")
                m = launch_once(mc, mode, args.observe, args.show_cmdlog)
                m["mode_name"] = mode
                m["clean"] = ok_idle
                m["t_stop"] = t_stop
                m["stop_note"] = note
                m["errs"] = watch.since(n_before)
                rows.append(m)
                print(row_str(i, m))
                print(f"       收车: {note}  用时 {t_stop:.2f}s   "
                      f"错误事件={m['errs'] or '无'}")
                for line in m["log"]:
                    print(line)

                # 连发两车探针: 车**正在**跑时再发一车 —— 预期 ERR_BUSY
                if args.double_probe and i == 1:
                    p = mc.maze.run("BLIND")
                    print(f"       [double-probe] 车在跑时再发一车 -> "
                          f"ok={p.ok} status={p.status} "
                          f"attempts={(p.data or {}).get('attempts')}")

            # 该工况收尾
            ok_idle, t_stop, note = ensure_idle(mc, mode, args.stop_timeout,
                                                assume_busy=True)
            print(f"  工况收尾 : {note} ({t_stop:.2f}s)")

        # ---------------- S2 汇总 ----------------
        hr("S2  汇总")
        n = len(rows)
        busy = [r for r in rows if r["status"] == BUSY]
        clean = [r for r in rows if r["clean"]]
        retried = [r for r in rows if r["attempts"] > 1]
        faked = [r for r in rows if r["ok"] and not r["launched"]]
        ghost = [r for r in rows if (not r["ok"]) and r["launched"]]
        print(f"  发车轮数             : {n}")
        print(f"  环境已清(可发)轮数   : {len(clean)}")
        print(f"  回 ERR_BUSY 轮数     : {len(busy)}"
              + (f"  <- {[r['mode_name'] for r in busy]}" if busy else ""))
        print(f"  需要重发的轮数(att>1): {len(retried)}"
              + (f"  <- {[r['mode_name'] for r in retried]}" if retried else ""))
        print(f"  回 OK 但实际没跑     : {len(faked)}"
              + (f"  <- {[r['mode_name'] for r in faked]}" if faked else ""))
        print(f"  回拒绝但实际在跑     : {len(ghost)}"
              + (f"  <- {[r['mode_name'] for r in ghost]}" if ghost else ""))
        for mode in modes:
            sub = [r for r in rows if r["mode_name"] == mode]
            if not sub:
                continue
            hz = [r["hz"] for r in sub if r["hz"]]
            stop = [r["t_stop"] for r in sub]
            print(f"  [{mode:<5}] 帧率中位≈{sorted(hz)[len(hz)//2] if hz else 0:.1f}Hz  "
                  f"发车耗时 max={max(r['dt'] for r in sub)*1000:.0f}ms  "
                  f"收车用时 max={max(stop):.2f}s  "
                  f"BUSY={sum(1 for r in sub if r['status'] == BUSY)}/{len(sub)}")

        verdict = ("未复现 ERR_BUSY（本机路径在拉取中发车全过）"
                   if not busy else "复现了 ERR_BUSY —— 见上表定位")
        print(f"\n  结论: {verdict}")
        print(f"  期间断连事件: {len(watch.link_lost)} 次 link_lost")
        if faked or ghost:
            print("  ⚠️ 存在「应答与真实状态不一致」的轮次 —— 这比 BUSY 本身更该先修。")
        return 0
    finally:
        # ★★ 安全出口 —— 只要用过 `--motor`, 无论正常结束 / 抛异常 / Ctrl-C,
        #    都必须把电脑鼠带回**待命**态(分级: MAZE STOP → ESTOP → MCU RESET)。
        try:
            mc.stream.off()      # 先停拉取器, 让链路安静下来给收车命令
        except Exception:
            pass
        if args.motor:
            try:
                ok, note = force_idle(mc, "off", allow_reset=allow_reset,
                                      assume_busy=True, verbose=True)
                print(f"\n[安全出口] {'已确认待命' if ok else '**未确认待命**'}: {note}")
                if not ok:
                    print("!" * 74)
                    print("⚠️ 无法确认电脑鼠已停车 —— **请立即人工断电**; 或另开一个终端跑")
                    print("   `python mouse_stop.py COM7` 再试一次收车。")
                    print("!" * 74)
            except Exception as e:
                print(f"\n[安全出口] 收车过程出错: {type(e).__name__}: {e}"
                      f" —— **请人工断电确认**!")
        else:
            print("\n[安全出口] 未用 --motor(只读审计), 不发任何收车命令。")
        watch.close()
        try:
            mc.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
