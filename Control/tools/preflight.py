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
    # ⚠️ 改版二十四：`PhoneCamera/pc` 整行删除（手机相机下线）。
    # 这里只放**上游仓库**；视频素材/产物目录不是仓库，单独在下面第 3b 节看。
    paths = {
        "RemoteControl/host": cfg.get("paths.remotecontrol_host"),
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
    # 上游是**平级目录**，不是包 —— 必须把它们各自加进 sys.path
    # （整合层里这件事由 control/adapters/__init__.ensure_path 统一负责）
    from control.adapters import ensure_path
    for p in paths.values():
        ensure_path(str(p or ""))
    for mod, label in (("host.api", "鼠 host.api"), ("aruco_locator", "视频 aruco_locator")):
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
    head("6. 判据 I9：上游仓库的工作区必须干净")
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
            # ⚠️ `-- .` 不可省：上游各目录其实是**同一个仓库**的子目录（单仓库 + 上游子目录）。
            # 不加 pathspec 时报的是整个仓库的改动 —— 包括 Control 自己正在改的文件，
            # 于是"上游脏了"这条警告会一直亮着、指错方向。
            out = subprocess.run(["git", "-C", str(Path(str(p))), "status", "--porcelain", "--", "."],
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

    # ---------------------------------------------------------- 8 视频复盘
    if not args.quick:
        head("8. 视频复盘（离线：视频文件 → Auto Locate 会话解析）")
        # 三条都要查，理由是它们各自会让"点开始解析"直接失败，而且失败信息都在子进程里：
        #   ① 素材目录：不存在 ⇒ 界面上选不到视频（但手填绝对路径仍可用 ⇒ WARN 不是 FAIL）；
        #   ② 上游配置：**必须给**。`python -m aruco_locator.session` 不收 `--config`，
        #      缺了它 720p 的 scales/内参全部不生效 ⇒ 一堆无效帧，看起来像"这个视频不行"；
        #   ③ 上游会话模块：`aruco_locator/session.py` 在不在（**只看文件，不 import** ——
        #      import 会把 cv2 拉进这个自检进程，而 preflight 本身不该依赖视觉栈）。
        vroot = cfg.get("video.root")
        vcfg = cfg.get("video.config")
        vout = cfg.get("video.out_root")
        if vroot and Path(str(vroot)).is_dir():
            n_vid = len([q for q in Path(str(vroot)).iterdir()
                         if q.is_file() and q.suffix.lower() in (".mp4", ".mov", ".mkv", ".avi")])
            ok(f"素材目录 {vroot}（{n_vid} 个视频）")
        else:
            warn(f"素材目录不存在：{vroot}",
                 "建一个空目录即可（界面里也能直接手填视频的绝对路径）："
                 f"mkdir \"{vroot}\"")
        if vcfg and Path(str(vcfg)).is_file():
            ok(f"上游配置 {vcfg}")
        else:
            fail(f"上游配置不存在：{vcfg}",
                 "config.yaml 的 video.config 必须指向 Auto Locate/config.yaml。"
                 "缺了它 720p 的检测尺度/内参全部不生效 ⇒ 全是无效帧。")
        al = cfg.get("paths.autolocate")
        sess = Path(str(al or "")) / "aruco_locator" / "session.py"
        if sess.is_file():
            ok(f"上游会话模块在：{sess.name}（不 import，只看文件）")
        else:
            fail(f"上游会话模块缺失：{sess}",
                 "Auto Locate 没有 run_session 就没法解析视频。检查 paths.autolocate。")
        if vout:
            try:
                Path(str(vout)).mkdir(parents=True, exist_ok=True)
                pr = Path(str(vout)) / ".preflight_write_test"
                pr.write_text("x", encoding="utf-8")
                pr.unlink()
                ok(f"产物根可写：{vout}")
            except OSError as e:
                fail(f"产物根不可写：{vout}（{e}）", "解析出来的会话会落不下去。")

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
        "默认批式",                            #   （十九①：当时的决定，开篇历史记录留档，不改）
        "默认停等",                            #   （二十一：默认由 pace 回退 stopwait；config.yaml 与代码兜底同源）
        "门面三态",                            #   （十九②：判模式只看 mc.stream.mode，不看 stats()["mode"]）
        "只读不给调",                          #   （十九③：n/t 不开放，10 Hz 是压测出的安全档）
        # 2026-10-15 改版（二十）：接回上游 v2.7 的按键菜单远程化（「维护与自检」卡）。
        # 布局与模块清单**变了**（小车控制三卡 → 四卡），锚点取"新版形态"的主题与两件要害：
        "按键菜单远程化",                       #   （二十①：CAL 家族 6 项回到界面，含翻改版十七的账）
        "二次确认",                            #   （二十②：擦除类不可逆 ⇒ 界面就地二次确认）
        "没有镜像",                            #   （二十③：模拟器没镜像 BLOCK 的墙图重载 ⇒ 留真机验）
        # 2026-10-20 改版（二十二）：跟上游 2.10~2.12（协议 v9 统一帧 49→30 B）。
        "统一帧 49 → 30 B",                 #   （二十二①：PROTO 9 帧重排，20 个通道消失）
        "静默失效",                          #   （二十二②：GOAL/JUNC 靠已删字段 ⇒ 不报错、只是少记录）
        "那个事件没有落地",                  #   （二十二③：PATHPLAN 只在方案里，fastwayCount 真拿不到）
        "兜底软复位",                        #   （二十二④：MAZE STOP 2 s 兜底，rst POR→SW）
        "整文件已删",                        #   （二十二⑤：权威变更记录改指 通信协议规格.md §9）
        # 2026-10-20 改版（二十三）：「无记忆走迷宫」下线 + 地图预览迁比赛 + 终点锁死。
        # 布局与模块清单**又变了**（四卡 → 三卡），锚点取"新版形态"的三句拍板与两条退化：
        "「无记忆走迷宫」整页下线",           #   （二十三①：新基线只跑比赛跑法，mod_maze8 删）
        "地图预览迁入比赛卡",                 #   （二十三②：唯一还有独立价值的中栏那张图搬家）
        "终点位置锁死",                       #   （二十三③：GOAL_CELLS 常量，不从帧读 goalX/goalY）
        "I36",                              #   （完整性 drop_seen 退化为本机 seq_lost 差量）
        # 2026-10-21 改版（二十四）：手机摄像头整块下线 + 视频复盘 + 上游 2.13.0 四灯。
        # 布局与模块清单**又变了**（页签 7→5、视图 8→6、三卡 → 三组 3/2/1），
        # 锚点取"新版形态"的四条拍板与两条自省（判据账、工具瘦身）：
        "整组删掉",                          #   （拍板①：相机与两张调试卡一起下线，不留"读产物"页）
        "视频入口只认服务器路径",             #   （拍板②：video.root 列表 + 绝对路径，不做网页上传）
        "对轴归识别侧",                       #   （拍板③：撤销人工指帧与 aligned.json，本层只做自洽校验）
        "4 灯先放在实时状态卡",               #   （拍板④：led1..led4 进状态行，不进读数表）
        "判据的账要平",                       #   （删六条相机判据 + I25/I26，必须同时说清增改）
        "验收工具也要跟着瘦",                 #   （不给已删除的功能留空壳判据 —— 留着只会一直 SKIP）
        # 2026-10-22 改版（二十五）：前端整体重设计（外部提交 18c180a）。后端一行未改，
        # 所以锚点取"边界"与"断点"两类：边界是这一版最重要的产物，断点是唯一的真损伤。
        "前端整体重设计",                     #   （旧前端 14 文件 + app.js + wsproto.js 整体删）
        "纯前端原型",                        #   （fetch 零出现、零 WebSocket；数字来自 250 ms 模拟 tick）
        "前端不再由本层维护",                  #   （control/static/** 只读不改，改行为走"提契约需求"）
        "四处接缝",                          #   （act/pullBatch/videoStart/videoCancel，页面层不用动）
        "停用并加硬闸",                       #   （browser_check 2471 行全钉旧 DOM；停用留证据+无 --force 就 exit 3）
        "I20 必然 FAIL",                #   （drill I20 探的 /static/wsproto.js 已删 ⇒ 实测 404）
        # 2026-10-22 改版（二十六）：批式 N 打通（用户拍板：N 要调用、且所有拉取界面共用）。
        # 锚点取"口径"与"两条坑"——这一版最容易在半年后被改错的正是这三件：
        "批式 N 打通",                       #   （新增 POST /mouse/stream/batch，n 统一 1~16）
        "N 就是拉取档",                      #   （n=1 停等 / ≥2 批式，kind 由上游按 n 决定）
        "不夹值",                            #   （越界 400；夹值＝界面说成功、设备按别的跑）
        "重连会把 N 吃掉",                    #   （stream.on() 每次 configure(1,…) ⇒ connect 里重放）
        # 布局方向：总纲里"入口页只做导航 / 单开一个整屏模块 / 不设选项卡"那几条**属于改版
        # 一到二十四**，改版二十五换了前端之后它们只剩史实价值。**故意不删**（删了就是篡改
        # 历史），而是补一条现状锚点，让"两个时代"并存、各自可查。
        "现行布局（改版二十五起）",            #   （左轨 + 九页面，单页 hash 路由）
        # 2026-10-22 改版（二十七）：P2 后端契约补齐。锚点取"口径"与"那个静默错"——
        # 半年后最容易被改回去的正是这两处：
        "P2 后端契约补齐",                     #   （caps/rate/sessions/records 五处纯增量）
        "本层说的，不是上游事实",               #   （launch/launch_at_s/ledger/maze_seed/created 都是本层口径）
        "实测率不该取决于有没有人在看",          #   （rate 从 _bin_subs 闸后面提到前面）
        "解除 = 删文件",                       #   （关联的"空值"是删文件，不是写 null）
        # 2026-10-22 改版（二十八）：P4 接线 + P5 判据重写。锚点取"判据的落点"——
        # 这一版最容易被后来的人改回去的，是把判据又写成"看界面文字对不对"：
        "P4 接线 + P5 判据重写",                #   （LIVE.on 一处开关 + browser_check 第三版）
        "接缝真的接上了",                       #   （判据要抓真请求，不看 toast）
        "点【应用】n=3",                       #   （V6 的具体做法：注入 fetch 记录器）
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
    print("    · 视频 → 把一个 mp4 丢进 video/，再进【视频复盘】→【视频会话】点开始解析")
    print("      （没有视频也能验界面：那一页会把\"目录里没有视频\"如实写出来）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
