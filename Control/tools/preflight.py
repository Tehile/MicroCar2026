"""启动前自检。**按顺序把"会让人白折腾半小时"的事先排掉。**

    python tools/preflight.py            # 全量自检
    python tools/preflight.py --quick    # 跳过网络与 git（离线环境）

每一项都输出三段：**检查了什么 / 结论 / 不对时怎么改**。
退出码：0 全过，1 有 FAIL（有 WARN 仍返回 0 —— 警告不是拦路石）。
"""

from __future__ import annotations

import argparse
import importlib
import os
import socket
import subprocess
import sys
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAIL: list = []
WARN: list = []
OK: list = []


def head(t: str) -> None:
    print(f"\n── {t} " + "─" * max(0, 66 - len(t)))


def ok(t: str) -> None:
    OK.append(t)
    print(f"  [ OK ] {t}")


def warn(t: str, fix: str = "") -> None:
    WARN.append(t)
    print(f"  [WARN] {t}")
    if fix:
        print(f"         → {fix}")


def fail(t: str, fix: str = "") -> None:
    FAIL.append(t)
    print(f"  [FAIL] {t}")
    if fix:
        print(f"         → {fix}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="跳过网络与 git 检查")
    args = ap.parse_args(argv)

    print(f"Control 自检 · 解释器 {sys.executable}")
    print(f"Python {sys.version.split()[0]}  ·  项目 {ROOT}")

    # ---------------------------------------------------------- 1 配置
    head("1. 配置")
    cfg = None
    try:
        from control.config import bootstrap
        cfg = bootstrap()
        ok(f"config.yaml 载入通过：{cfg.path}")
    except Exception as e:                                   # noqa: BLE001
        fail(f"config.yaml 载不进来：{type(e).__name__}: {e}",
             "配置里出现未知键会**直接报错**（不静默忽略）。照提示改。")
        return _report()

    # ---------------------------------------------------------- 2 依赖
    head("2. 依赖（整合层固定用 general_env）")
    need = {"aiohttp": None, "numpy": None, "yaml": "pyyaml", "cv2": "opencv-contrib-python",
            "serial": "pyserial"}
    for mod, pkg in need.items():
        try:
            m = importlib.import_module(mod)
            v = getattr(m, "__version__", "?")
            ok(f"{mod:9s} {v}")
        except Exception as e:                               # noqa: BLE001
            fail(f"{mod} 导不进来：{type(e).__name__}",
                 f"pip install {pkg}（**只在这个环境里装**："
                 f"C:\\Users\\liuru\\.conda\\envs\\general_env\\python.exe）")
    if "general_env" not in sys.executable:
        warn(f"当前不是 general_env（{sys.executable}）",
             "本整合层固定用 C:\\Users\\liuru\\.conda\\envs\\general_env\\python.exe。"
             "Auto Locate 明令不得在该环境外 pip install。")

    # ---------------------------------------------------------- 3 上游路径
    head("3. 上游仓库路径")
    paths = {
        "RemoteControl/host": cfg.get("paths.remotecontrol_host"),
        "PhoneCamera/pc": cfg.get("paths.phonecamera_pc"),
        "Auto Locate": cfg.get("paths.autolocate"),
    }
    for name, p in paths.items():
        if p and Path(str(p)).is_dir():
            ok(f"{name} → {p}")
        else:
            fail(f"{name} 路径不存在：{p}",
                 "相对路径是相对 Control/ 解析的。检查 config.yaml 的 paths 段。")

    # ---------------------------------------------------------- 4 上游可导入
    head("4. 上游可 import（**只 import，不改它们**）")
    # 上游是三个**平级目录**，不是包 —— 必须把它们各自加进 sys.path
    # （整合层里这件事由 control/adapters/__init__.ensure_path 统一负责）
    from control.adapters import ensure_path
    for p in paths.values():
        ensure_path(str(p or ""))
    for mod, label in (("host.api", "鼠 host.api"), ("hostlink", "相机 hostlink"),
                       ("aruco_locator", "定位 aruco_locator")):
        try:
            m = importlib.import_module(mod)
            v = getattr(m, "__version__", None) or getattr(m, "API_VERSION", None) or ""
            ok(f"{label} 可导入 {v}")
        except Exception as e:                               # noqa: BLE001
            fail(f"{label} 导不进来：{type(e).__name__}: {e}",
                 "路径已确认存在 ⇒ 大概率是依赖缺失。跑一遍上面第 2 节。")

    # ---------------------------------------------------------- 5 I1：不许引入 GUI
    head("5. 判据 I1：不许引入 PySide6 / pyqtgraph")
    gui = [m for m in ("PySide6", "PyQt5", "pyqtgraph") if m in sys.modules]
    if gui:
        fail(f"sys.modules 里出现了 GUI 框架：{gui}",
             "Web 形态的目的之一就是**不引入 PySide6**，从而不依赖 stmlink、"
             "不碰 Auto Locate 的环境纪律。有任何一处 import 都会让这条判据判否。")
    else:
        ok("没有 GUI 框架被导入（Web 版不需要它们）")

    # ---------------------------------------------------------- 6 I9：上游不许脏
    head("6. 判据 I9：三个上游仓库的工作区必须干净")
    for name, p in paths.items():
        if not (p and Path(str(p)).is_dir()):
            continue
        repo = Path(str(p))
        for _ in range(4):
            if (repo / ".git").exists():
                break
            if repo.parent == repo:
                repo = None
                break
            repo = repo.parent
        if repo is None:
            warn(f"{name} 不是一个 git 仓库（记录 commit 的能力会缺失）")
            continue
        try:
            out = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
            if out:
                n = len(out.splitlines())
                warn(f"{name} 工作区**有 {n} 处改动** ⇒ meta.json 里记下的前提"
                     f"不是任何已发布的版本",
                     "取证体系的结论绑定在「当前源码」上。要么提交，要么明确知道自己在测什么。")
            else:
                ok(f"{name} 干净")
        except (OSError, subprocess.SubprocessError) as e:
            warn(f"{name} git 查询失败：{e}")

    # ---------------------------------------------------------- 7 串口
    head("7. 串口（**串口是独占的**，被占就没有第二个能连）")
    try:
        from host.transport.ports import list_ports
        ports = list_ports()
        if not ports:
            warn("没有枚举到任何串口",
                 "没有硬件时可以直接连模拟器：先起 RemoteControl/host/sim/mouse_sim.py，"
                 "再连 tcp://127.0.0.1:5555")
        for pi in ports:
            tag = "可用" if getattr(pi, "usable", False) else "**不可用/被占**"
            print(f"         {pi.device:8s} {getattr(pi, 'desc', '')[:36]:38s} {tag}")
        ok(f"枚举到 {len(ports)} 个串口")
        bt = [p for p in ports if "blue" in (getattr(p, "desc", "") or "").lower()]
        if bt:
            warn("检测到蓝牙串口：蓝牙链路的可持续吞吐实测只有约 150 B/s",
                 "所以实时只能看状态，1 kHz dump 要停车后拉（约 60 s）。")
    except Exception as e:                                   # noqa: BLE001
        warn(f"串口枚举失败：{type(e).__name__}: {e}")

    # ---------------------------------------------------------- 8 相机服务
    if not args.quick:
        head("8. 相机服务（独立进程，默认 8765）")
        try:
            from control.adapters.camera import CameraAdapter
            from control.bus import Bus
            from control.store import Store
            bus = Bus()
            cam = CameraAdapter(cfg, bus, Store(cfg.session_root))
            if not cam.available:
                warn(f"hostlink 不可用：{cam.import_error}")
            else:
                try:
                    caps = cam.capabilities()
                    ok(f"相机服务可达：{cam.base}  版本 {caps.get('server_version') or caps.get('version')}")
                    print(f"         注意：这台服务只允许 loopback 读（/host/、/stream/）——"
                          f"Control 与它同机，所以没问题。")
                except Exception as e:                       # noqa: BLE001
                    warn(f"相机服务不可达：{str(e).splitlines()[0]}",
                         "启动 PhoneCamera/pc/start_service.cmd。"
                         "**没有手机也能继续**：去【采集】页用「离线会话导入」把已有会话接进来。")
        except Exception as e:                               # noqa: BLE001
            warn(f"相机自检跳过：{type(e).__name__}: {e}")

    # ---------------------------------------------------------- 9 可写性
    head("9. 会话根目录可写")
    root = cfg.session_root
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".preflight_write_test"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        ok(f"{root} 可写")
    except OSError as e:
        fail(f"{root} 不可写：{e}", "会话全都会开不出来。检查路径与权限。")

    # ---------------------------------------------------------- 10 端口占用
    head("10. Control 自己的端口")
    port = int(cfg.get("server.port", 8770))
    s = socket.socket()
    try:
        s.bind((str(cfg.get("server.host", "127.0.0.1")), port))
        ok(f"{cfg.get('server.host')}:{port} 空闲")
    except OSError as e:
        warn(f"端口 {port} 被占：{e}", "改 config.yaml 的 server.port（或先关掉上一个 Control）")
    finally:
        s.close()

    # ---------------------------------------------------------- 11 大方向冻结
    head("11. 大方向冻结（宗旨与布局不得改动）")
    # 总方向已由用户确认。这一节把"方向还在不在"变成一条可复跑的检查 ——
    # 检查的是**具体锚点**而不只是文件存在：文档被整体重写、宗旨被删、布局被改
    # 时这里必须红，否则"冻结"只是文档里的一句空话。
    doc = ROOT / "docs" / "上位机整合设计方案.md"
    anchors = [
        "总纲（冻结）",                       # 章节还在
        "本项目是整合层，不是替代层",          # 宗旨一
        "人先连、后测",                       # 宗旨二
        "文本是唯一真相",                     # 宗旨三
        "布局方案（冻结）",                    # 布局
        "子系统子页面",                        # 2026-10-04 改版（一）的布局形态
        "入口页只做导航",                      # 2026-10-04 改版（三）的布局形态
        "单开一个整屏模块界面",                 # 入口页 → 模块界面的开法
        "不设选项卡、不切页",                   # 硬规则：换界面 ≠ 换页，也不开新标签
        "子系统与模块（冻结）",                 # 子系统 / 模块清单
        "分组测量",                            # 2026-10-04 改版（四）：小车控制新增的两个模块
        "实验 8 · 无记忆走迷宫",                #   （上游 1.5.0 组测发送 / 1.5.5 迷宫状态机）
        "实时参数",                            # 2026-10-04 改版（五）：参数回传 → 实时参数
        "四路红外 / 电压",                      #   （红外/电压与里程并进同一张读数表）
        # 2026-10-05 改版（六）：上游 2.0.0「传输模式重构」—— 协议整体替换、不做兼容层。
        # 「分组测量」这个**模块**没了（它并入「实时参数」），但那个词仍出现在改版记录里，
        # 所以上面那条锚点保留（规则：改总纲开篇要保留全部锚点串）。
        # 下面两条是**新版形态**的锚点 —— 少任何一条，这次改版就等于没落到文档里：
        "分组测量并入实时参数",                 #   （两个取参数的子功能合并成一页）
        "SNAP / SCOPE / RATE / VER 已全部删除", #   （协议整体替换的直接证据）
        # 2026-10-05 改版（七）：上游 2.1.0「拉取模式」—— v2.0 的自时钟被真机推翻。
        # 锚点是**新版形态**的表述；布局与模块清单都不变，所以只加这两条：
        "拉取模式",                            #   （谁控制发送节奏：改成上位机逐条拉）
        "帧率不再是 217 Hz",                    #   （真机 4.4~6.5 Hz，差约 50 倍）
        # 2026-10-05 改版（八）：实验 8 改三栏 + 从发车到停止的记录与复盘。
        # 布局与模块清单都不变（仍是入口页 + 整屏模块），所以只加"新版形态"的两条：
        "重构示意",                            #   （中栏：从帧重建"它眼里的迷宫"）
        "从发车到停止",                         #   （记录自动圈定这一段）
        # 2026-10-05 改版（九）：上游 2.1.1~2.2.1（真机事故驱动）。这一轮**推翻了一条结论**
        # 并**抓出一个一直没红过的真缺陷**，所以锚点取的是那两件事的新形态：
        "能收回来",                            #   （主题：发出去的车要能收回来）
        "MAZE STOP",                          #   （2.1.1 收车命令；旧结论"没有停止跑迷宫"已废）
        "MCU RESET",                          #   （2.2.0 可靠收车手段：整机软复位）
        "ERR_LOWBATT",                        #   （2.2.1 发车前低电硬闸）
        "都要有一条端到端判据真的走一遍",        #   （真缺陷的教训升级成纪律：动作按钮也要有闸）
        # 2026-10-06 改版（十）：实验 8 右栏重做（进度条 / 回放 / 8 类决策）。
        # 布局与模块清单都不变，所以只加"新版形态"的三条：
        "时间线进度条",                        #   （右栏最顶上那个，驱动的是一整个"此刻"）
        "进入回放",                            #   （按键 + 拖进度条两种入口）
        "决策差分",                            #   （8 类决策必须主机差分：MAZE EV 已被上游删除）
        # 2026-10-07 改版（十一）：实验 8 的"回放/实时两条来路"统一到同一个纪元。
        # 布局与模块清单都不变，所以只加"新版形态"的三条：
        "开新纪元",                            #   （发车 = 清常驻环 + 换差分器，不只是挪零点）
        "两条来路必然对不上",                   #   （根因：旧实现把上一段的决策留在环里，成了负时间）
        "后端能算的，不要前端自己攒",            #   （上行事件并进常驻环，与决策共用同一零点）
        # 2026-10-07 改版（十二）：上游 2.4.0（压档 + 删课程层 + `MAZE RUN [BLIND|RACE]`）。
        # 布局规则不变（仍是入口页 + 整屏模块），变的是**小车控制的模块清单**（四卡 → 三卡），
        # 所以锚点取"新版形态"的模块名与两件新事：
        "实时状态与参数设定",                   #   （原「实时参数」改名 + 并入运动/标定）
        "无记忆走迷宫",                         #   （原「实验 8」改名，内部三栏不变）
        "比赛",                                #   （新页：读地图选档发车 + 冲刺监视）
        "MAZE RUN",                           #   （2.4.0 的发车命令，带目标词）
        "RACE",                               #   （比赛目标；缺一档就是"两种跑法"没写出来）
        "取消独立参数标定",                      #   （用户的说法 → 指的是课程层，不是标定例程）
        "前任统一帧通道数写死",                  #   （真缺陷：能力位静默 False，见 §11.15）
        # 2026-10-08 改版（十三/十四）：无记忆走迷宫的两轮现场小改。布局与模块清单都不变，
        # 所以只加"新版形态"的锚点串：
        "前墙双档显示",                        #   （十三①：远端墙/近端墙两档分开画）
        "起点必须",                            #   （十三②：左下角、车头朝上，图上要画得出来）
        "决策历程要能",                        #   （十四③：清空 = 后端开新纪元，不是抹 DOM）
        "推不出来",                            #   （十四④：停止原因只能靠上行事件，帧里同形）
        "只影响显示",                          #   （十四⑤：网格尺寸选择不传后端/不改记录）
        "底栏重做",                            #   （十五：删实时读数条，底栏只留信息输出）
        "按\"来源\"分类",                      #   （十五②：bus.log 带 source，不许按文案猜）
        "删解释性 / 补充性文字",                #   （十五③：界面只留状态 + 可照做的下一步）
        "运动控制",                            #   （十六①：上游 2.5.0 参数表 +第四组 id 39–55）
        "说明列",                              #   （十六②：上游 PARAM_META 功能短句上界面，清洗不改写）
        "与上游现算比对",                       #   （十六③：I16 不写死 24 项三组，逐组逐项对）
        "删除「标定例程」",                      #   （十七①：鼠侧五项 + 场地几何 + 报告整块下线）
        "页签重排",                            #   （十七②：④标定删，发车⑤→④ … 数据⑨→⑧，EXPECTED_VIEWS 9→8）
        "静态配置",                            #   （十七③：flip 值改 config.yaml，不再自动求解）
        "前墙双档必须",                         #   （十八①：近端画本格前沿、远端画外一格前沿 —— 分边）
        "整体上移一格",                         #   （十八②：PY(y) 是上沿，早先当成下一格的上沿）
        "格子的屏幕范围",                       #   （十八③：判据要按"格子的屏幕区间"对位）
        "默认批式",                            #   （十九①：mouse.stream_mode 默认 pace，调试阶段）
        "门面三态",                            #   （十九②：判模式只看 mc.stream.mode，不看 stats()["mode"]）
        "只读不给调",                          #   （十九③：n/t 不开放，10 Hz 是压测出的安全档）
        "此后所有任务不得改变这里的方向",       # 显式约束
    ]
    try:
        text = doc.read_text(encoding="utf-8")
    except OSError as e:
        text = ""
        fail(f"方案文档读不到：{e}", f"应有：{doc}")
    if text:
        missing = [a for a in anchors if a not in text]
        if missing:
            fail(f"方案文档缺少冻结锚点 {missing}",
                 "把这些写回 docs/上位机整合设计方案.md 开篇的「总纲（冻结）」一节；"
                 "改完跑 python tools/build_doc_html.py 重生成 HTML。")
        else:
            ok("方案文档含全部冻结锚点（宗旨三条 + 布局 + 显式约束）")
        readme = ROOT / "README.md"
        try:
            rt = readme.read_text(encoding="utf-8")
        except OSError:
            rt = ""
        if "总纲（冻结）" in rt:
            ok("README 已指向冻结章节")
        else:
            warn("README 没有指向「总纲（冻结）」",
                 "在 README 顶部加一句指针，免得后来者只读 README 就改布局。")
        gen = ROOT / "docs" / "上位机整合设计方案.html"
        if gen.exists():
            try:
                gt = gen.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                gt = ""
            if "总纲（冻结）" in gt:
                ok("生成的 HTML 与冻结章节同步")
            else:
                warn("HTML 里没有冻结章节（md 改了但没重生成？）",
                     "跑 python tools/build_doc_html.py。**不要手改 HTML。**")

    return _report()


def _report() -> int:
    head("结论")
    print(f"  通过 {len(OK)}  警告 {len(WARN)}  失败 {len(FAIL)}")
    if FAIL:
        print("\n  必须先修掉这些（FAIL）：")
        for t in FAIL:
            print(f"    · {t}")
        return 1
    if WARN:
        print("\n  可以开工，但这些要知道：")
        for t in WARN:
            print(f"    · {t}")
    print("\n  下一步：python app.py  然后打开 http://127.0.0.1:8770")
    print("  没有硬件也能验证全部功能：")
    print("    · 鼠 → 先起 RemoteControl/host/sim/mouse_sim.py，连 tcp://127.0.0.1:5555")
    print("    · 相机 → 【采集】页「离线会话导入」，用已有那 14 个真实会话")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
