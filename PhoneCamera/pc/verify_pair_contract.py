#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""跨端配对契约验证：**PC 生成 → App 解码 → App 解析**，全都跑真代码。

三步：
  1. 用服务端生成器 `pair_uri()` 产出配对码（不是手写一串常量）；
  2. 把该码渲染成无损 PGM，交给 **App 里那个 zxing jar**（`app/libs/core-3.5.3.jar`）解码
     —— 证明"PC 生成的二维码 == App 的解码器能解出的码"，而且是同一个 jar；
  3. 用 JVM 直接跑 **App 的 `PairCode.parse()`**（纯 Kotlin、不依赖 Android 类）
     —— 证明"解出来的串能被 App 的解析器正确解析"，并顺带跑它内建的自检向量。

⚠ **诚实边界（不许把这一步说成真机验证）**：
   它排除了"格式不匹配 / 解析器写错 / 解码器解不出这个码"这三类问题；
   **不覆盖**"手机拍屏幕"的现实因素（对焦、反光、摩尔纹、距离、屏幕亮度）——
   那些只能在现场验（App 里长按「扫码配对」可跑解析自检，但相机部分仍需人眼确认）。

跑法：`python pc/verify_pair_contract.py`（需 JDK；先跑过一次 gradle assembleDebug）
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
APP = ROOT / "android" / "m0probe" / "app"
ZXING = APP / "libs" / "core-3.5.3.jar"
KT_CLASSES = APP / "build" / "tmp" / "kotlin-classes" / "debug"
JVMCHECK = HERE / "jvmcheck"


def find_tool(name: str) -> str | None:
    """找 java/javac：先看 PATH，再回退到本机已知的 JDK 目录。"""
    p = shutil.which(name)
    if p:
        return p
    for pat in (f"C:/Program Files/Microsoft/jdk*/bin/{name}.exe",
                f"C:/Program Files/Java/jdk*/bin/{name}.exe",
                f"C:/Program Files/Eclipse Adoptium/jdk*/bin/{name}.exe"):
        hit = sorted(glob.glob(pat))
        if hit:
            return hit[-1]
    return None


def find_kotlin_stdlib() -> str | None:
    hits = glob.glob(os.path.expanduser(
        "~/.gradle/caches/modules-2/files-2.1/org.jetbrains.kotlin/kotlin-stdlib/*/*/kotlin-stdlib-*.jar"))
    hits = [h for h in hits if "sources" not in h and "common" not in h]
    return sorted(hits)[-1] if hits else None


def qr_pgm(text: str, scale: int = 8, border: int = 2) -> str:
    """把文本渲染成 **P2 PGM**（ASCII 灰度，零依赖、可肉眼查）。

    放大 `scale` 倍：真实相机看到的码至少有几十像素/模块，
    1 像素/模块的图等于"理想化到不存在"，验证就失去意义了。
    """
    import qrcode  # type: ignore
    qr = qrcode.QRCode(border=border, error_correction=qrcode.constants.ERROR_CORRECT_Q)
    qr.add_data(text)
    qr.make(fit=True)
    m = qr.get_matrix()
    n = len(m)
    side = n * scale
    rows = []
    for r in range(n):
        # ⚠ 放大必须**逐模块**重复，不能 `整行 * scale`：
        #    `"a b c " * 3` 得到的是 `a b c a b c a b c`（**横向平铺三份图案**），
        #    不是 `a a a b b b c c c`（真正的放大）。
        #    第一版就是这么写错的，图完全不是二维码 —— 被 cv2 与 zxing 两个解码器当场抓住。
        line = "".join(("0 " if m[r][c] else "255 ") * scale for c in range(n)).strip()
        rows.extend([line] * scale)                        # 纵向放大（整行重复是对的）
    body = "\n".join(rows) + "\n"
    return f"P2\n{side} {side}\n255\n" + body


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    # `-Dstdout.encoding/-Dfile.encoding`：Windows 上 JVM 默认按 GBK 输出，
    # 中文错误信息会变成乱码 —— 判据的输出是要给人看的。
    if cmd and cmd[0].endswith(("java", "java.exe")):
        cmd = [cmd[0], "-Dstdout.encoding=UTF-8", "-Dstderr.encoding=UTF-8"] + cmd[1:]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", **kw)


def main() -> int:
    sys.path.insert(0, str(HERE))
    import server as srv                                   # noqa: E402

    host, port, token = "192.168.1.23", 8765, "a3f9c1d2"
    uri = srv.pair_uri(host, port, token)
    print(f"[1/3] PC 生成配对码（服务端生成器）：{uri}")
    if uri != f"m0probe://pair?h={host}&p={port}&t={token}":
        print("✗ 生成器产出的写法与契约不符")
        return 2

    java = find_tool("java")
    javac = find_tool("javac")
    if not java or not javac:
        print("⚠ 找不到 JDK（java/javac）⇒ 本验证无法进行（**不是通过**）。")
        return 3
    if not ZXING.exists():
        print(f"⚠ 缺 {ZXING} ⇒ 无法解码（不是通过）")
        return 3

    with tempfile.TemporaryDirectory(prefix="pairchk_") as td:
        tdp = Path(td)
        pgm = tdp / "pair.pgm"
        pgm.write_text(qr_pgm(uri), encoding="utf-8")
        txt = tdp / "pair.txt"
        txt.write_text(uri, encoding="utf-8")

        # ---- 2) 用 App 那个 zxing jar 解码 ----
        # 两个验证器**分开编译**：QrDecode 只要 zxing；PairCheck 还依赖 App 的 class
        # 与 kotlin-stdlib。分开编译的好处是缺 Kotlin 产物时第 2 步仍能跑，
        # 并明确说"第 3 步未验证" —— 而不是整体失败让人以为扫码链路坏了。
        std = find_kotlin_stdlib()
        cp_parts = [str(ZXING)]
        if KT_CLASSES.exists():
            cp_parts.append(str(KT_CLASSES))
        if std:
            cp_parts.append(std)
        cp_src = os.pathsep.join(cp_parts)
        out = tdp / "classes"
        out.mkdir()
        r = run([javac, "-nowarn", "-cp", cp_src, "-d", str(out),
                 str(JVMCHECK / "QrDecode.java")])
        if r.returncode != 0:
            print("编译 QrDecode 失败：\n" + (r.stderr or r.stdout))
            return 2
        if KT_CLASSES.exists():
            r2 = run([javac, "-nowarn", "-cp", cp_src, "-d", str(out),
                      str(JVMCHECK / "PairCheck.java")])
            if r2.returncode != 0:
                print("编译 PairCheck 失败：\n" + (r2.stderr or r2.stdout))
                return 2
        cp = os.pathsep.join([str(out), cp_src])

        d = run([java, "-cp", cp, "QrDecode", str(pgm)])
        if d.returncode != 0:
            print("✗ zxing 解码失败：" + (d.stderr or "").strip()[:200])
            return 2
        decoded = d.stdout.strip()
        good = decoded == uri
        print(f"[2/3] zxing（App 的 jar）解码：{'OK' if good else '不符'}"
              f"{'' if good else '  解出：' + repr(decoded)}")
        if not good:
            return 2

        # ---- 3) 用 JVM 跑 App 的解析器 ----
        if not KT_CLASSES.exists():
            print(f"[3/3] ⚠ 缺 {KT_CLASSES} ⇒ **未验证** App 的解析器"
                  f"（先跑一次 `gradle assembleDebug`）；此验证**未完成**，不算通过。")
            return 3
        p = run([java, "-cp", cp, "PairCheck", str(txt), f"{host}:{port}|{token}"])
        print(f"[3/3] App 的 PairCode.parse()（扫码那条路径）：{p.stdout.strip() or p.stderr.strip()}")
        if p.returncode != 0:
            print(f"✗ App 解析器未通过（退出码 {p.returncode}）")
            return 2

        # 3b) **旧写法**（终端也打印的整行 URL）必须解析出同一结果：
        #     用户完全可能手抄/粘贴那一行，而不是扫码 —— 两条路都得能走。
        url_file = tdp / "pair_url.txt"
        url_file.write_text(f"http://{host}:{port}/?token={token}", encoding="utf-8")
        p2 = run([java, "-cp", cp, "PairCheck", str(url_file), f"{host}:{port}|{token}"])
        print(f"[3b] 旧写法（整行 URL）：{p2.stdout.strip() or p2.stderr.strip()}")
        if p2.returncode != 0:
            print(f"✗ 整行 URL 写法未被正确解析（退出码 {p2.returncode}）")
            return 2

        # 3c) **误扫防线**：扫到无关二维码必须被明确拒绝，且原因指名道姓。
        #     这一条是"扫码"这个功能最容易被忽略、又最容易在现场变成"没反应"的地方。
        #     关键词在 Python 侧检查（命令行里传中文受平台编码影响，不能当判据）。
        bad_file = tdp / "bad.txt"
        bad_file.write_text("https://mp.weixin.qq.com/s/abcdefg", encoding="utf-8")
        p3 = run([java, "-cp", cp, "PairCheck", str(bad_file), "--expect-fail"])
        o3 = (p3.stdout or p3.stderr).strip()
        print(f"[3c] 误扫（公众号链接）：{o3}")
        if p3.returncode != 0 or "REJECTED-AS-EXPECTED" not in o3 or "网页链接" not in o3:
            print("✗ 误扫未被拒绝，或拒绝原因没说清（应为「网页链接，不是配对码…」）")
            return 2

    print("结论：跨端契约通过 —— PC 生成 → zxing 解出 → App 解析器解析，全链路一致。")
    print("      ⚠ 仍未覆盖「手机拍屏幕」（对焦/反光/摩尔纹），那一步只能现场验。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
