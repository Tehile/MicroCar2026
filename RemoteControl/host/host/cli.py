"""上位机命令行入口 —— CLI 本身即机器可调用的接口。

用法(在 host/ 目录下):
    python -m host.cli ports --json
    python -m host.cli monitor COM7 --expect-safe     # 实时数据监视器(本阶段主用)
    python -m host.cli connect COM7 --stream --record
    python -m host.cli get TRACKL --json --target COM7
    python -m host.cli params set TRACKL 53.2015 --save --target COM7
    python -m host.cli calib gyro --target COM7
    python -m host.cli calib walldist --target COM7 && python -m host.cli save --target COM7
    python -m host.cli calib irdata --target COM7     # 4 个摆位逐点采(交互)
    python -m host.cli calib ir-self --target COM7    # v2.7 菜单 4: 红外自检(仅传感器段)
    python -m host.cli calib sensor --target COM7     # v2.7 菜单 5: 传感器 4 行
    python -m host.cli calib ircap --target COM7      # v2.7 菜单 6: 红外 8 值分步标定
    python -m host.cli calib block --target COM7      # v2.7 菜单 7: 块数据概览
    python -m host.cli calib erase-ir --confirm --target COM7   # v2.7 菜单 2(不可逆)
    python -m host.cli calib erase-all --confirm --target COM7  # v2.7 菜单 8(不可逆)
    python -m host.cli maze --target COM7             # 墙图(默认动作 map)
    python -m host.cli maze state --target COM7       # 迷宫状态(从统一帧读)
    python -m host.cli diagnose COM7
    python -m host.cli scan-baud COM7
    python -m host.cli replay sessions/2026-09-27_0151 --analyze

v2.0(2026-10-05): 传输协议整体替换 —— `SNAP`/`SCOPE` 命令与 `scope`/`snap`
子命令整套删除; 上行只剩统一帧(`TYPE_UNIFIED`)与墙图帧(`TYPE_MAP`)两种。
每个参数类命令都有 `params <动作>` 入口(与 `registry.COMMANDS` 的 `params.*`
逐条对齐), 顶层 `get/get-all/set/save` 是同一批命令的便捷写法。

约定: 每个子命令 = 一次 API 调用; `--json` 输出结构化结果(可被 jq 解析)。
不指定 --target 时自动挑第一个可用串口, 没有串口则回退到模拟器 TCP(5555)。
"""

from __future__ import annotations

import argparse
import json
import sys


# ---------------------------------------------------------------- 输出辅助

def _emit(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    elif isinstance(obj, str):
        print(obj)
    else:
        for line in _flatten(obj):
            print(line)


def _flatten(obj) -> list:
    """把 dict/list 转成简洁的 key: value 行。"""
    if isinstance(obj, dict):
        return [f"{k}: {v}" for k, v in obj.items()]
    if isinstance(obj, (list, tuple)):
        return [str(x) for x in obj]
    return [str(obj)]


# ---------------------------------------------------------------- 连接辅助

def _open_client(target: str, **ctor_kw):
    from host.api import MouseClient

    mc = MouseClient(**ctor_kw)
    if not mc.connect(target):
        detail = mc.state_detail or "未知原因"
        mc.close()
        raise SystemExit(f"连接失败: {detail}")
    return mc


def _resolve_target(args) -> str:
    t = getattr(args, "target", None)
    if t:
        return t
    # 自动: 第一个可用串口, 否则模拟器 TCP
    from host.api import coerce_target

    return coerce_target(None)


# ---------------------------------------------------------------- 子命令

def cmd_monitor(args) -> None:
    """实时数据监视器(终端仪表盘) —— 串到 host.monitor。"""
    from host import monitor

    argv = []
    if args.target:
        argv.append(args.target)
    argv += ["--baud", str(args.baud), "--hz", str(args.hz), "--width", str(args.width),
             "--timeout", str(args.timeout)]
    if args.seconds:
        argv += ["--seconds", str(args.seconds)]
    for flag in ("once", "json", "no_color", "no_clear", "no_stream", "record", "expect_safe"):
        if getattr(args, flag.replace("-", "_")):
            argv.append("--" + flag.replace("_", "-"))
    rc = monitor.main(argv)
    if rc:
        raise SystemExit(rc)


def cmd_motor_test(args) -> None:
    """电机远程控制实验(双流) —— 串到 host.motor_test。"""
    from host import motor_test

    argv = []
    if args.target:
        argv.append(args.target)
    argv += ["--baud", str(args.baud), "--sp", *[str(x) for x in args.sp],
             "--time", str(args.time), "--ir", str(args.ir), "--hz", str(args.hz)]
    if args.out:
        argv += ["--out", args.out]
    for flag in ("no_analyze", "status", "estop", "resume", "expect_safe",
                 "json", "no_color"):
        if getattr(args, flag):
            argv.append("--" + flag.replace("_", "-"))
    raise SystemExit(motor_test.main(argv))


def cmd_datalog(args) -> None:
    """数据收集台(远程发车 + 时间同步 + 完整采集) —— 串到 host.datalog。"""
    from host import datalog

    argv = []
    if args.target:
        argv.append(args.target)
    argv += ["--baud", str(args.baud), "--seconds", str(args.seconds), "--hz", str(args.hz)]
    if args.out:
        argv += ["--out", args.out]
    for flag in ("no_maze", "no_arm", "force", "arm_only", "maze_only",
                 "status", "json", "no_color"):
        if getattr(args, flag):
            argv.append("--" + flag.replace("_", "-"))
    raise SystemExit(datalog.main(argv))


def cmd_estop(args) -> None:
    """一键急停(随时可用, 不需要其它前置条件)。"""
    from host import motor_test

    argv = []
    if args.target:
        argv.append(args.target)
    argv += ["--baud", str(args.baud)]
    argv.append("--resume" if args.resume else "--estop")
    if args.json:
        argv.append("--json")
    raise SystemExit(motor_test.main(argv))


def cmd_rescue(args) -> None:
    """卡死救援: 发 `MCU RESET` 让设备整机软复位(不必断电)。

    用于"命令回 OK 但设备再也不响应"的情形 —— 走迷宫的主循环里有大量非协作式
    阻塞等待在等物理进度, 而 `STOP` 会让电机永不再转, 那些条件永远不成立,
    于是程序卡在函数内部、连退出检查都轮不到(真机实测 2026-10-05)。
    ⚠️ 会打断任何进行中的运动。与 `cli params --reset`(只擦参数不重启)不同。
    """
    from host.api import MouseClient

    mc = MouseClient(auto_session=False, auto_stream=False)
    if not mc.connect(_resolve_target(args)):
        raise SystemExit(f"连接失败: {mc.state_detail}")
    try:
        r = mc.conn.soft_reset(verify=not args.no_verify, timeout=args.timeout)
        if getattr(args, "json", False):
            _emit({"ok": r.ok, "status": r.status, "data": r.data}, True)
        else:
            print(f"MCU RESET -> {r.status}   {r.data}")
            if r.ok:
                print("  设备已软复位(rst=SW), 回到空闲态, 可重新发车。")
            else:
                print("  ⚠️ 复核未通过 —— 可再试一次, 或断电重启。")
        if not r.ok:
            raise SystemExit(1)
    finally:
        mc.close()


def cmd_ports(args) -> None:
    from host.api import MouseClient

    mc = MouseClient(auto_session=False, auto_stream=False)
    try:
        ports = mc.conn.ports()
        labels = mc.conn.port_labels()
    finally:
        mc.close()
    if getattr(args, "json", False):
        _emit(
            [
                {"device": p.device, "description": p.description, "direction": p.direction,
                 "is_bluetooth": p.is_bluetooth, "usable": p.usable}
                for p in ports
            ],
            True,
        )
    else:
        print("\n".join(labels) if labels else "（未发现串口）")


def cmd_connect(args) -> None:
    from host.api import MouseClient

    mc = MouseClient(auto_session=args.record, auto_stream=args.stream)
    if not mc.connect(args.target, baud=args.baud):
        raise SystemExit(f"连接失败: {mc.state_detail}")
    print(f"已连接: {mc.state_detail}")
    print(f"设备: {mc.device}")
    if args.stream:
        print("等待遥测首帧…")
        if mc.telemetry.wait_first_frame(timeout=5):
            print("遥测已到达。Ctrl+C 退出。")
            try:
                mc.telemetry.live(seconds=args.seconds)
            except KeyboardInterrupt:
                pass
        else:
            print("未收到遥测(是否已开始拉取? 见 mc.stream.on())")
    elif args.seconds:
        try:
            mc.telemetry.live(seconds=args.seconds)
        except KeyboardInterrupt:
            pass
    print("会话目录:", mc.session.path if args.record else "(未开启)")
    mc.close()


def cmd_get(args) -> None:
    mc = _open_client(_resolve_target(args))
    try:
        val = mc.params.get(args.param)
        if val is None:
            raise SystemExit(f"回读失败: {args.param}")
        _emit({"param": args.param, "value": val}, getattr(args, "json", False))
    finally:
        mc.close()


def cmd_get_all(args) -> None:
    mc = _open_client(_resolve_target(args))
    try:
        got = mc.params.get_all()
        if getattr(args, "json", False):
            _emit(got, True)
        else:
            print(mc.params.table())
    finally:
        mc.close()


def cmd_params(args) -> None:
    """参数读写族 —— 与 `registry.COMMANDS` 的 `params.*` 逐条对齐。

    动作 get / get-all / set / setq / save / reset 各对应一条登记命令;
    顶层 `get/get-all/set/save` 是同一批命令的便捷入口, 保留不变。

    `setq` 收 Q16.16 原始定点(与 `params.setq` 的 `q` 同义), 是**无精度损失**的
    写入路径; `set` 收十进制物理值(内部换算, 精度略低, 供手工调试)。
    """
    act = args.action
    mc = _open_client(_resolve_target(args))
    try:
        if act == "get":
            val = mc.params.get(args.name)
            if val is None:
                raise SystemExit(f"回读失败: {args.name}")
            _emit({"param": args.name, "value": val}, getattr(args, "json", False))
            return
        if act == "get-all":
            got = mc.params.get_all()
            if getattr(args, "json", False):
                _emit(got, True)
            else:
                print(mc.params.table())
            return
        if act == "set":
            r = mc.params.set(args.name, args.value)
            if not r.ok:
                raise SystemExit(f"写入失败: {r.status}")
            if args.save and not mc.params.save().ok:
                raise SystemExit("保存失败")
            _emit({"param": args.name, "value": args.value, "ok": True,
                   "readback": r.data.get("readback"), "saved": args.save},
                  getattr(args, "json", False))
            return
        if act == "setq":
            r = mc.params.set_raw(args.name, int(args.q))
            if not r.ok:
                raise SystemExit(f"写入失败: {r.status}")
            if args.save and not mc.params.save().ok:
                raise SystemExit("保存失败")
            _emit({"param": args.name, "q": int(args.q), "ok": True,
                   "readback": r.data.get("readback"), "saved": args.save},
                  getattr(args, "json", False))
            return
        if act == "save":
            r = mc.params.save(timeout=args.timeout)
            _emit({"ok": r.ok, "status": r.status}, getattr(args, "json", False))
            if not r.ok:
                raise SystemExit(f"SAVE 失败: {r.status}")
            return
        if act == "reset":
            r = mc.params.reset(timeout=args.timeout)
            _emit({"ok": r.ok, "status": r.status}, getattr(args, "json", False))
            if not r.ok:
                raise SystemExit(f"RESET 失败: {r.status}")
            return
        raise SystemExit(f"未知 params 动作: {act}")
    finally:
        mc.close()


def cmd_set(args) -> None:
    mc = _open_client(_resolve_target(args))
    try:
        r = mc.params.set(args.param, args.value)
        if not r.ok:
            raise SystemExit(f"写入失败: {r.status}")
        if args.save:
            s = mc.params.save()
            if not s.ok:
                raise SystemExit(f"保存失败: {s.status}")
        _emit({"param": args.param, "value": args.value, "ok": True,
               "readback": r.data.get("readback"), "saved": args.save},
              getattr(args, "json", False))
    finally:
        mc.close()


#: v2.7 菜单远程化动作(立即 ACK 的维护动作, 与"跑一项标定"不同流)
_MENU_ITEMS = ("ERASE-IR", "IR-SELF", "SENSOR", "IRCAP", "BLOCK", "ERASE-ALL")


def _run_menu_action(mc, item: str, args) -> "object":
    """v2.7: 按键菜单 2/4/5/6/7/8 的远程化(全部立即 ACK)。

    擦除两类(ERASE-IR / ERASE-ALL)必须显式 `--confirm` —— 固件侧没有二次
    确认, 这道闸在 registry precheck 上(不发确认就地返回 NEED_CONFIRM)。
    """
    if item == "ERASE-IR":
        return mc.calib.erase_ir(confirm=args.confirm)
    if item == "ERASE-ALL":
        return mc.calib.erase_all(confirm=args.confirm)
    if item == "IR-SELF":
        return mc.calib.ir_self()
    if item == "SENSOR":
        return mc.calib.sensor()
    if item == "BLOCK":
        return mc.calib.block()
    # IRCAP: 给了 --step 就单步, 否则 4 步连跑(每步前等回车摆位)
    if getattr(args, "step", 0):
        return mc.calib.ir_cap(args.step)

    def _ask(i: int, name: str, desc: str) -> bool:
        print(f"\n第 {i + 1}/4 点 —— {name}: {desc}")
        if args.yes:
            return True
        ans = input("  摆好后回车采集 (输入 c 取消): ").strip().lower()
        return ans != "c"

    return mc.calib.ir_cap_all(on_prompt=_ask, erase_first=args.erase_first)


def cmd_calib(args) -> None:
    from host.api import MouseClient

    mc = _open_client(_resolve_target(args))
    try:
        item = args.item.upper().replace("_", "-")
        if item == "IRDATA":
            # 4 个摆位逐点采: 每点等操作者摆好再采(与固件的 CAL IRDATA SAMPLE 一致)
            def _ask(i: int, name: str, desc: str) -> bool:
                print(f"\n第 {i + 1}/4 点 —— {name}: {desc}")
                if args.yes:
                    return True
                ans = input("  摆好后回车采集 (输入 c 取消): ").strip().lower()
                return ans != "c"

            r = mc.calib.ir_data(on_prompt=_ask)
        elif item in _MENU_ITEMS:
            r = _run_menu_action(mc, item, args)
        else:
            fn = {
                "GYRO": mc.calib.gyro,
                "WALLDIST": mc.calib.wall_distance,
                "ANGLE": mc.calib.angle,
                "TRACK": mc.calib.track,
            }.get(item)
            if fn is None:
                raise SystemExit(
                    "未知标定项: {} (可选 GYRO/WALLDIST/IRDATA/ANGLE/TRACK 或 "
                    "菜单远程化 {})".format(item, "/".join(i.lower() for i in _MENU_ITEMS))
                )
            r = fn() if item in ("GYRO", "WALLDIST", "ANGLE") else fn(
                pulses=args.pulses, speed=args.speed
            )
        _emit({"item": item, "ok": r.ok, "status": r.status, "data": r.data},
              getattr(args, "json", False))
        if not r.ok and r.status == "MOTOR_DISABLED":
            print(f"\n提示: {r.data.get('hint', '')}", file=sys.stderr)
        if not r.ok and r.status == "NEED_CONFIRM":
            print(f"\n⚠️ {r.data.get('hint', '该操作不可逆, 需要确认')}\n"
                  f"   确认无误请加 --confirm 重跑。", file=sys.stderr)
            raise SystemExit(2)
        if not r.ok and item == "IRCAP" and r.status == "ERR_BUSY":
            print("\n提示: IRDATA 页非空(已标定过) —— 远程版要求先擦页再重标: "
                  "`calib erase-ir --confirm`(不可逆)或 `calib ircap --erase-first`。",
                  file=sys.stderr)
    finally:
        mc.close()


def cmd_save(args) -> None:
    """把参数表写入 Flash(page 115)。WALLDIST / 角度系数 / 里程系数改完 RAM 后需要它。"""
    from host.api import MouseClient

    mc = _open_client(_resolve_target(args))
    try:
        r = mc.params.save(timeout=args.timeout)
        _emit({"ok": r.ok, "status": r.status}, getattr(args, "json", False))
        if not r.ok:
            raise SystemExit(f"SAVE 失败: {r.status}")
    finally:
        mc.close()


def cmd_maze(args) -> None:
    """迷宫: 墙图 + **状态(从统一帧读取)**(实验8 无记忆走迷宫)。

    `action` 省略时等价 `map`。

    v2.0: 迷宫状态量已全部在统一帧里(≈217 Hz), 原先的 `MAZE EV` 事件流、
    1Hz 状态帧、`SNAP MAZE` 三条通道整体删除 —— 于是动作只剩
    "拉墙图 / 读当前状态 / 看汇总"。
    """
    action = getattr(args, "action", None) or "map"
    mc = _open_client(_resolve_target(args))
    try:
        if action == "map":
            m = mc.maze.fetch()
            if not m:
                raise SystemExit("未获取到墙图")
            pos = mc.maze.position()
            print(mc.maze.to_text(mark=None if pos is None else pos[:2]))
            if args.out:
                with open(args.out, "w", encoding="utf-8") as f:
                    json.dump({"mazetype": m["mazetype"], "mapWall": m["mapWall"],
                               "mapVisited": m["mapVisited"]}, f, ensure_ascii=False)
            _emit({"mazetype": m["mazetype"], "out": args.out}, getattr(args, "json", False))
            return

        if action == "state":
            st = mc.maze.state()
            if getattr(args, "json", False):
                _emit(st or {}, True)
                return
            if st is None:
                print("（还没收到统一帧。未在拉取? 迷宫没在跑?）")
                return
            print(f"任务 {st['taskName']}  动作 {st['motionName']}  步 {st['motionStep']}  "
                  f"路口 {st['roadKind']}({st['roadName']})  "
                  f"开口(L,F,R)=({st['wallLeft']},{st['wallFront']},{st['wallRight']})")
            print(f"前墙 {st['frontName']}  "
                  f"(近端 {st['frontNear']} / 远端 {st['frontFar']} / 双路互证 {st['frontBoth']})")
            print(f"位置 ({st['posX']},{st['posY']}) 朝向 {st['dirName']}  "
                  f"终点 ({st['goalX']},{st['goalY']})  岔路栈 {st['stack']}  "
                  f"快路径计数 {st['fastwayCount']}")
            if st["evDropped"]:
                print("⚠️ 上行事件有丢弃(链路拥塞): 事件流不完整")
            return

        if action == "spurt":
            sp_ = mc.maze.spurt()
            if getattr(args, "json", False):
                _emit(sp_ or {}, True)
                return
            if sp_ is None:
                print("（还没收到统一帧。未在拉取?）")
                return
            print(f"冲刺档 {sp_['modeName']}({sp_['mode']})  冲刺标志 {sp_['spurtFlag']}  "
                  f"去/回 {sp_['goOrBack']}")
            print(f"冲刺前行格数 {sp_['goaheadnum']}  90°路径计数 {sp_['fastwayCount90']}  "
                  f"冲刺测试 {sp_['spurtTestFlag']}  回坐标中 {sp_['objectGoToFlag']}")
            print("（只读监视: 冲刺入口由比赛固件里的人工贴墙选档决定, 上位机不能设置）")
            return

        if action == "summary":
            s = mc.maze.summary()
            if getattr(args, "json", False):
                _emit(s, True)
            else:
                for line in mc.maze.explain():
                    print(line)
            return

        if action == "run":
            # ⚠️ 会发车: 先过登记表的低电闸(registry._precheck_maze_run)。
            goal = (getattr(args, "goal", None) or "blind").upper()
            r = mc.maze.run(target=goal)
            print(f"MAZE RUN {goal} -> {r.status}")
            if not r.ok:
                if r.status == "ERR_LOWBATT":
                    print(f"  拒绝发车: {r.data.get('hint', '')}")
                raise SystemExit(1)
            return

        if action == "stop":
            # ⚠️ 会停车: 把电脑鼠带出迷宫 + 复位发车状态 + 解锁急停。
            r = mc.maze.stop()
            print(f"MAZE STOP -> {r.status}")
            if not r.ok:
                raise SystemExit(1)
            return

        raise SystemExit(f"未知 maze 动作: {action}")
    finally:
        mc.close()


def cmd_diagnose(args) -> None:
    from host.api import MouseClient

    mc = MouseClient(auto_session=False, auto_stream=False)
    try:
        r = mc.conn.probe(args.port, args.baud, args.seconds)
    finally:
        mc.close()
    _emit(r, getattr(args, "json", False))


def cmd_scan_baud(args) -> None:
    from host.api import MouseClient

    mc = MouseClient(auto_session=False, auto_stream=False)
    try:
        results = mc.conn.scan_baud(args.port, args.bauds, args.seconds)
    finally:
        mc.close()
    if getattr(args, "json", False):
        _emit(results, True)
    else:
        for r in results:
            print(f"{r.get('baud', '?')}: {r.get('bytes', 0)}B  "
                  f"frames={r.get('frames', 0)} lines={r.get('text_lines', 0)} "
                  f"crc={r.get('bad_crc', 0)}")


def cmd_replay(args) -> None:
    from host.api import MouseClient

    mc = MouseClient(auto_session=False, auto_stream=False)
    mc.connect("replay://" + args.session)
    try:
        print(f"回放: {args.session}")
        mc.telemetry.wait_first_frame(timeout=10)
        print(mc.telemetry.summary_text())
        st = mc.stats()
        print(f"帧: {st.get('unified', 0)}  字节: {st.get('rx_bps', 0):.0f} B/s")
        if args.analyze:
            # v2.0: 分析输入改为**统一帧历史**(1kHz dump 文件已不再产生)。
            # 回放时 raw.bin 由新解析器重跑, 统一帧序列就在本地环形缓冲里。
            print("—— 动作段分析 ——")
            print(mc.analyze.explain(mc.telemetry.history_dict()))
        if getattr(args, "json", False):
            _emit({"frames": st.get("unified"), "rx_bps": st.get("rx_bps")}, True)
    finally:
        mc.close()


# ---------------------------------------------------------------- 参数解析

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="host.cli", description="电脑鼠上位机命令行接口")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("ports", help="枚举串口")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("connect", help="连接(可选开流+录会话+实时状态)")
    sp.add_argument("target")
    sp.add_argument("--baud", type=int, default=115200)
    sp.add_argument("--stream", action="store_true", help="连接后开始拉取遥测(mc.stream.on)")
    sp.add_argument("--record", action="store_true", help="开启会话落盘")
    sp.add_argument("--seconds", type=float, default=0.0, help="实时状态持续时间(0=直到 Ctrl+C)")

    sp = sub.add_parser("get", help="读单个参数")
    sp.add_argument("param")
    sp.add_argument("--target")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("get-all", help="读全部参数")
    sp.add_argument("--target")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("set", help="写参数(可 --save 落 Flash)")
    sp.add_argument("param")
    sp.add_argument("value", type=float)
    sp.add_argument("--save", action="store_true")
    sp.add_argument("--target")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("rescue",
                        help="卡死救援: 整机软复位(`MCU RESET`, 不必断电; 会打断运动)")
    sp.add_argument("--target")
    sp.add_argument("--timeout", type=float, default=8.0, help="复位后复核等待上限(秒)")
    sp.add_argument("--no-verify", action="store_true", help="只发命令, 不复核 rst")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("calib", help="跑一项标定 / v2.7 起兼管按键菜单远程化动作")
    sp.add_argument("item",
                    help="GYRO/WALLDIST/IRDATA/ANGLE/TRACK; v2.7 菜单远程化: "
                         "erase-ir/ir-self/sensor/ircap/block/erase-all(下划线可写连字符)")
    sp.add_argument("--pulses", type=int, default=20000)
    sp.add_argument("--speed", type=int, default=40)
    sp.add_argument("--yes", action="store_true",
                    help="IRDATA/IRCAP: 不逐点等回车, 连续采 4 点(自动化用)")
    sp.add_argument("--step", type=int, default=0, metavar="N",
                    help="IRCAP 单步(1..4); 省略则 4 步连跑")
    sp.add_argument("--erase-first", action="store_true",
                    help="IRCAP: 先擦红外页再采(不可逆, 页非空时必需)")
    sp.add_argument("--confirm", action="store_true",
                    help="erase-ir/erase-all: 确认执行不可逆擦除(否则就地拒绝)")
    sp.add_argument("--target")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("save", help="把参数表写入 Flash(page 115)")
    sp.add_argument("--timeout", type=float, default=5.0)
    sp.add_argument("--target")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("params", help="参数读写族(与 registry 的 params.* 逐条对齐)")
    sp.add_argument("action", choices=["get", "get-all", "set", "setq", "save", "reset"],
                    help="get/get-all/set/setq/save/reset")
    sp.add_argument("name", nargs="?", help="参数名(get/set/setq 需要)")
    sp.add_argument("value", nargs="?", type=float, help="物理值(set)")
    sp.add_argument("q", nargs="?", type=int, help="Q16.16 原始值(setq)")
    sp.add_argument("--save", action="store_true", help="set/setq 后落 Flash")
    sp.add_argument("--timeout", type=float, default=5.0, help="save/reset 超时(秒)")
    sp.add_argument("--target")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("maze", help="迷宫: 墙图 + 状态(从统一帧读取) + 发车/收车")
    sp.add_argument("action", nargs="?", default="map",
                    choices=["map", "state", "spurt", "summary", "run", "stop"],
                    help="map=拉墙图(默认) state=当前迷宫状态 spurt=比赛冲刺状态(只读) "
                         "summary=汇总 run=远程发车(会动! 见 --goal) "
                         "stop=收车(会停车! 带出迷宫+复位发车状态+解锁急停)")
    sp.add_argument("--goal", choices=["blind", "race"], default="blind",
                    help="run 的目标: blind=无记忆走迷宫(默认) race=正式比赛代码(先读图选档)")
    sp.add_argument("--out", help="map: 导出 map.json")
    sp.add_argument("--target")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("monitor", help="实时数据监视器(终端仪表盘)")
    sp.add_argument("target", nargs="?", default=None,
                    help="串口/TCP/回放目标; 省略则自动选择")
    sp.add_argument("--baud", type=int, default=115200)
    sp.add_argument("--hz", type=float, default=5.0)
    sp.add_argument("--width", type=int, default=78)
    sp.add_argument("--seconds", type=float, default=0.0)
    sp.add_argument("--timeout", type=float, default=5.0)
    sp.add_argument("--once", action="store_true", help="打印一次快照后退出")
    sp.add_argument("--json", action="store_true", help="每行一条 JSON")
    sp.add_argument("--no-color", action="store_true")
    sp.add_argument("--no-clear", action="store_true")
    sp.add_argument("--no-stream", action="store_true")
    sp.add_argument("--record", action="store_true")
    sp.add_argument("--expect-safe", action="store_true",
                    help="期望固件处于安全测试模式(电机禁用)")

    sp = sub.add_parser("motor-test", help="电机远程控制实验(运动期间采样统一帧)")
    sp.add_argument("target", nargs="?", default=None, help="串口/TCP; 省略则自动选择")
    sp.add_argument("--baud", type=int, default=115200)
    sp.add_argument("--sp", type=int, nargs="+", default=[40],
                    help="目标速度 cm/s: 一个值=左右相同, 两个值=左 右")
    sp.add_argument("--time", type=float, default=2.0, help="最长运行时间(秒), 默认 2.0")
    sp.add_argument("--ir", type=int, default=10,
                    help="红外停车阈值(mm), 默认 10; 负值=关闭红外护栏")
    sp.add_argument("--hz", type=float, default=5.0, help="采样显示频率")
    sp.add_argument("--out", help="把运动期间的采样导出为 CSV")
    sp.add_argument("--no-analyze", action="store_true")
    sp.add_argument("--status", action="store_true", help="只打印状态")
    sp.add_argument("--estop", action="store_true", help="只发急停")
    sp.add_argument("--resume", action="store_true", help="只发 STOP(解急停)")
    sp.add_argument("--expect-safe", action="store_true")

    sp = sub.add_parser("datalog", help="数据收集台(远程发车 + 时间同步 + 完整采集)")
    sp.add_argument("target", nargs="?", default=None, help="串口/TCP; 省略则自动选择")
    sp.add_argument("--baud", type=int, default=115200)
    sp.add_argument("--seconds", type=float, default=10.0, help="采集时长(秒)")
    sp.add_argument("--hz", type=float, default=5.0, help="实时流显示频率")
    sp.add_argument("--no-maze", dest="no_maze", action="store_true",
                    help="不远程发车跑迷宫, 只采集")
    sp.add_argument("--no-arm", dest="no_arm", action="store_true",
                    help="不发车(不点基准灯、数据不带 dev_ms)")
    sp.add_argument("--force", action="store_true", help="MAZE RUN 被拒时也继续")
    sp.add_argument("--arm-only", dest="arm_only", action="store_true")
    sp.add_argument("--maze-only", dest="maze_only", action="store_true")
    sp.add_argument("--status", action="store_true", help="只打印时间同步状态")
    sp.add_argument("--out", help="把采集期间捕获的统一帧导出为 CSV")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--no-color", dest="no_color", action="store_true")

    sp = sub.add_parser("estop", help="一键急停 / --resume 解锁")
    sp.add_argument("--target")
    sp.add_argument("--baud", type=int, default=115200)
    sp.add_argument("--resume", action="store_true", help="改为发 STOP(正常停车并解急停锁存)")

    sp = sub.add_parser("diagnose", help="诊断单个串口")
    sp.add_argument("port")
    sp.add_argument("--baud", type=int, default=115200)
    sp.add_argument("--seconds", type=float, default=2.0)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("scan-baud", help="扫描波特率")
    sp.add_argument("port")
    sp.add_argument("--bauds", type=int, nargs="*")
    sp.add_argument("--seconds", type=float, default=1.0)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("replay", help="回放历史会话")
    sp.add_argument("session")
    sp.add_argument("--analyze", action="store_true")
    sp.add_argument("--json", action="store_true")

    return p


_DISPATCH = {
    "ports": cmd_ports,
    "monitor": cmd_monitor,
    "connect": cmd_connect,
    "get": cmd_get,
    "get-all": cmd_get_all,
    "params": cmd_params,
    "set": cmd_set,
    "calib": cmd_calib,
    "save": cmd_save,
    "maze": cmd_maze,
    "rescue": cmd_rescue,
    "motor-test": cmd_motor_test,
    "datalog": cmd_datalog,
    "estop": cmd_estop,
    "diagnose": cmd_diagnose,
    "scan-baud": cmd_scan_baud,
    "replay": cmd_replay,
}


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        _DISPATCH[args.cmd](args)
    except SystemExit as e:
        # SystemExit 的 code 可能是字符串(raise SystemExit("原因"))或 None。
        # 字符串要打出来再返回非 0 —— 直接 int() 会抛 ValueError, 变成 traceback。
        if e.code is None:
            return 0
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            return 2
        return int(e.code)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
