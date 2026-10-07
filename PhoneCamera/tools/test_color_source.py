#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P16  实时道颜色来源旁路（离线）：`color_source` 二分实验的判据

────────────────────────────────────────────────────────────────────────
背景（第 29 轮）
────────────────────────────────────────────────────────────────────────
现场 live 画面**偏绿**，而同场景的**相机原生照片颜色正常**。要定位"偏色是谁的锅"，
必须有一条**不由我们算颜色**的对照路：

    yuv_bt601（本进程读 Y/U/V，自己按 BT.601 有限范围换算）← 现状，偏绿
    rgba_hal （ImageFormat.RGBA_8888，YUV→RGB 由相机 HAL 完成）← 新增

★ 这是**判决性实验**：两条路必有一条偏，责任归属即唯一确定。
  · `rgba_hal` **也偏绿** ⇒ 我们的 BT.601 换算**是清白的**（问题在 HAL 或显示链路）
  · 只有 `yuv_bt601` 偏绿 ⇒ 问题在我们的换算里

⚠⚠ 本判据存在的理由不是"检查代码写得像不像"，而是钉住**三个会让这次二分悄悄失效**的坑：

  ① **降级静默**：设备不支持 `RGBA_8888` 时会退回 `yuv_bt601` —— 此时两条路
     是**同一条**，画面照样出得来、照样能截图，**看起来完全正常**，而结论整个是假的。
  ② **通道顺序**：RGBA 的字节序若写成 B,G,R,A，得到的是"红蓝颠倒"。
     它与"偏绿"是**两个不同故障**，混淆会把排查带偏一整条街。
  ③ **假旁路**：用 `RGBA_8888` 的 reader 却仍在本地读 Y 平面（`encodeColorJpeg`
     不分派）⇒ 实验归零，而且**不报错**，只是偏色依旧。

判据项（含反例，见 `main()`）：
  · P16a  Kotlin 工具链可用（**缺编译器判 FAIL，不静默跳过**）
  · P16b  真的从源码切出了三个被测函数体（防止"整文件搜关键词 = 恒真"）
  · P16c  源码锚定：格式常量必须是 `FLEX_RGBA_8888`
                （⚠ `ImageFormat.RGBA_8888` 在 Camera2 里是**隐藏常量**，
                  `android.graphics.ImageFormat` **没有导出**它 ⇒ 写它编译直接失败）
  · P16d  源码锚定：`encodeColorJpeg` **按 `img.format` 分派**（不是按 color_source 字面）
  · P16e  源码锚定：降级必须**大声报出**且把生效值写进 `liveStatusJson`
  · P16f  行为：RGBA 夹具**逐字节**取回，且通道顺序为 **R,G,B**
  · P16g  行为：RGBA 档**不做任何 YUV→RGB 算术**（HAL 已经算好了，
                再算一遍就是把两个来源混起来 —— 那正是"假旁路"）
  · P16h  **反例**：把通道顺序改成 B,G,R，同一套判据必须判否
  · P16i  **反例**：去掉格式分派（`encodeColorJpeg` 不看 `img.format`）必须判否
  · P16j  **反例**：把降级写成静默（不报、不上报生效值）必须判否
  · P16k  PC 侧：非法取值 **400**；deliver + rgba_hal **400**（不静默放行）
  · P16l  PC 侧：`COLOR_SOURCES` 与 App 侧**取值集合一致**（跨进程漂移会静默降级）
  · P16m  PC 侧：面板 `<option>` 集合与 `COLOR_SOURCES` **交叉校验**（从集合自身推导）
  · P16n  PC 侧：summary **同时**给请求值与实际生效值（二分结论的前置条件）
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KT_SRC = (ROOT / "android" / "m0probe" / "app" / "src" / "main" / "java"
          / "com" / "mouse" / "phoneprobe" / "DeliverySession.kt")
SERVER = ROOT / "pc" / "server.py"

_RES: list[tuple[bool, str]] = []
_TODO: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    _RES.append((bool(cond), name))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))
    return bool(cond)


def note(line: str) -> None:
    print(f"         · {line}")


# ────────────────────────────── 与 P15 共用的工具（刻意复制而非 import）

def _find_jar(pattern: str):
    # ⚠ 不能用 `pathlib.Path.glob`：**Windows 上不支持绝对 pattern**
    #   （抛 NotImplementedError: Non-relative patterns are unsupported）。
    #   这不是"环境差异"，是判据自己会崩 ⇒ 一换机器就红。
    # ⚠ 取**最新**（按 mtime）而不是第一个：Gradle 缓存里常并存多个版本，
    #   随手取一个可能拿到与 stdlib 不匹配的编译器 ⇒ 报"Kotlin 反射缺失"，
    #   而真正原因只是版本配错（报错位置离原因很远）。
    import glob as _g
    hits = [Path(h) for h in _g.glob(os.path.expanduser(pattern), recursive=True)
            if Path(h).is_file()]
    hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return str(hits[0]) if hits else None


def kotlin_toolchain():
    """Kotlin 编译器 + 依赖 jar。**找不到就返回 None ⇒ 判 FAIL，不跳过。**
    ⚠ 路径必须带 `modules-2/files-2.1` 两层 —— Gradle 的依赖 jar 不在 `caches/`
       根下，而在 `caches/modules-2/files-2.1/<组>/<名>/<版本>/<sha>/`。漏掉这两层
       ⇒ glob 全 0 ⇒ 判据报"工具链不可用"，而机器上其实装着（**误报比没有更坏**）。
    """
    home = os.path.expanduser("~/.gradle/caches/modules-2/files-2.1")
    compiler = _find_jar(f"{home}/org.jetbrains.kotlin/kotlin-compiler-embeddable/*/*/"
                         f"kotlin-compiler-embeddable-*.jar")
    stdlib = _find_jar(f"{home}/org.jetbrains.kotlin/kotlin-stdlib/*/*/kotlin-stdlib-*.jar")
    reflect = _find_jar(f"{home}/org.jetbrains.kotlin/kotlin-reflect/*/*/kotlin-reflect-*.jar")
    script_rt = _find_jar(f"{home}/org.jetbrains.kotlin/kotlin-script-runtime/*/*/"
                          f"kotlin-script-runtime-*.jar")
    trove = _find_jar(f"{home}/org.jetbrains.intellij.deps/trove4j/*/*/trove4j-*.jar")
    daemon = _find_jar(f"{home}/org.jetbrains.kotlin/kotlin-daemon-embeddable/*/*/"
                        f"kotlin-daemon-embeddable-*.jar")
    annot = _find_jar(f"{home}/org.jetbrains/annotations/*/*/annotations-*.jar")
    java = shutil.which("java")
    # ⚠ `annot` 不是可选的：编译器**自己**在 codegen 阶段要读
    #   org.jetbrains.annotations.NotNull，缺了会在写 class 文件时抛
    #   NoClassDefFoundError —— 而报错指向"编译失败"，**离原因很远**。
    #   也不许在这里少检一项然后让后面某条判据报出莫名其妙的错。
    if not all([compiler, stdlib, script_rt, trove, daemon, annot, java]):
        return None
    return {"java": java, "compiler": compiler, "stdlib": stdlib, "reflect": reflect,
            "script_rt": script_rt, "trove": trove, "daemon": daemon, "annot": annot}


def _cut_braced(src: str, from_idx: int):
    i = src.find("{", from_idx)
    if i < 0:
        return None
    depth = 0
    j = i
    while j < len(src):
        c = src[j]
        if c == '"':                                   # 跳过字符串字面量
            j += 1
            while j < len(src) and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[from_idx:j + 1]
        j += 1
    return None


def extract_function(src: str, name: str):
    """切出 `private fun <name>(...) { ... }` 的**真实函数体**（含签名）。

    ⚠ 必须真的切出函数体 —— 整文件搜关键词会命中别的调用点，
    那样判据就是**恒真**的（第 6 条铁律）。
    """
    pat = re.compile(r"(?:private\s+)?fun\s+" + re.escape(name) + r"\s*\(")
    m = pat.search(src)
    if not m:
        return None
    return _cut_braced(src, m.start())


def strip_kotlin_comments(src: str) -> str:
    """剥掉 `//` 与 `/* */` 注释。

    ⚠ 不剥会踩这一脚：源码锚定会命中**我自己在注释里写的解释文字**
    （例如"这里不能用 `buf.get(out,0,...)`"），判据当场变红，
    而代码其实是对的 —— 更坏的是它会逼我把正确的注释改掉。
    """
    s = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    return re.sub(r"//[^\n]*", " ", s)


def _find_sdk_android_jar():
    """找本机 Android SDK 的 `android.jar`（找得到才做格式值交叉校验）。"""
    roots = [Path(os.environ.get("ANDROID_HOME", "")),
             Path(os.environ.get("ANDROID_SDK_ROOT", "")),
             Path(os.path.expanduser("~/AppData/Local/Android/Sdk")),
             Path("C:/PersonalData/Program/Others/android-sdk")]
    for r in roots:
        if not r or not r.exists():
            continue
        hits = sorted((r / "platforms").glob("*/android.jar"),
                      key=lambda p: p.name, reverse=True)
        if hits:
            return str(hits[0])
    return None


def _read_java_int_constant(class_bytes: bytes, name: str):
    """确认 .class 的常量池里**存在**名为 `name` 的 Utf8 条目。

    ⚠ 只判"符号存在"，不解析它的**数值** —— 数值用 [flex_rgba_value] 从真机
       `javap` 输出里取。
    ⚠ 为什么用自己解析常量池而不用反射/javap：判据要能在任何装了 SDK 的机器上跑。
       class 文件的常量池格式是确定的（tag + 长度/索引），按标签逐条跳过即可。
    """
    if not class_bytes.startswith(b"\xca\xfe\xba\xbe"):
        return False
    cp_count = int.from_bytes(class_bytes[8:10], "big")
    p, i = 10, 1
    while i < cp_count and p < len(class_bytes):
        tag = class_bytes[p]
        p += 1
        if tag == 1:
            ln = int.from_bytes(class_bytes[p:p + 2], "big")
            if class_bytes[p + 2:p + 2 + ln].decode("utf-8", "replace") == name:
                return True
            p += 2 + ln
        elif tag in (7, 8, 16, 19, 20):
            p += 2
        elif tag == 15:
            p += 3
        elif tag in (3, 4, 9, 10, 11, 12, 17, 18):
            p += 4
        elif tag in (5, 6):
            p += 8
            i += 1
        else:
            return False
        i += 1
    return False


def flex_rgba_value() -> int | None:
    """从真机 `javap` 输出里读 `FLEX_RGBA_8888` 的数值（不硬编码）。

    ⚠ 硬编码的后果：源码改了格式而判据仍按旧值造夹具 ⇒ "分派"那一项
       变成**自证的恒真**（它只验证"分派与我自己造的值一致"）。
    """
    sdk = _find_sdk_android_jar()
    if not sdk or not shutil.which("javap"):
        return None
    try:
        p = subprocess.run(["javap", "-constants", "-cp", sdk,
                            "android.graphics.ImageFormat"],
                           capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace")
        m = re.search(r"FLEX_RGBA_8888\s*=\s*(0x[0-9A-Fa-f]+|\d+)", p.stdout)
        if m:
            return int(m.group(1), 0)
    except Exception:
        pass
    return None


def run_kotlin(tc: dict, files: dict, tag: str) -> str:
    """真编译真执行，返回 stdout。编译/执行失败 ⇒ 抛异常（判 FAIL，不跳过）。"""
    d = Path(tempfile.mkdtemp(prefix=f"p16_{tag}_"))
    try:
        for name, body in files.items():
            (d / name).write_text(body, encoding="utf-8")
        cp = os.pathsep.join([tc["compiler"], tc["stdlib"], tc["script_rt"],
                              tc["trove"], tc["daemon"], tc["annot"]]
                             + ([tc["reflect"]] if tc["reflect"] else []))
        out_dir = d / "out"
        srcs = [str(d / n) for n in files]
        # ⚠ **不能**用 `-include-runtime` 打 jar：与 `-no-stdlib` 同时给时，
        #   jar 会**生成但不可运行**（"Invalid or corrupt jarfile"）——
        #   报错完全不提编译参数，看起来像文件损坏，排查方向被带偏。
        #   ⇒ 改成编译到**目录**，再用显式 `-cp` 起 java。
        # ⚠ errors="replace"：Kotlin 编译器把源码里的中文按本地代码页回显，
        #   不是合法 UTF-8 ⇒ 不设会让 subprocess 解码线程抛异常，
        #   **真正的编译错误反而被吃掉**。
        p = subprocess.run(
            [tc["java"], "-cp", cp, "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler",
             *srcs, "-no-stdlib",
             # ⚠ `-cp`（编译器进程的 classpath）**不等于** `-classpath`（被编译代码的）。
             #   少给后者 ⇒ 报 "Ensure that you have a dependency on the
             #   Kotlin standard library"，而错误点在 harness 末尾的 println，
             #   **离真正的原因（缺 stdlib）十万八千里**。
             "-classpath", os.pathsep.join(
                 [tc["stdlib"]] + ([tc["reflect"]] if tc["reflect"] else [])),
             "-nowarn", "-d", str(out_dir)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600)
        if not out_dir.exists() or not any(out_dir.rglob("*.class")):
            raise RuntimeError("Kotlin 编译失败:\n"
                               + (p.stdout or "")[-3000:] + (p.stderr or "")[-3000:])
        r = subprocess.run([tc["java"], "-cp",
                            os.pathsep.join([str(out_dir), tc["stdlib"]]), "ProbeKt"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300)
        if r.returncode != 0:
            raise RuntimeError("Kotlin 执行失败:\n" + r.stdout[-3000:] + r.stderr[-3000:])
        return r.stdout
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ────────────────────────────── RGBA 夹具

RGBA_STUB = r'''
package android.media

class Image(
    val width: Int,
    val height: Int,
    val format: Int,
    val planes: Array<Plane>
) {
    class Plane(
        val buffer: java.nio.ByteBuffer,
        val rowStride: Int,
        val pixelStride: Int
    )
}
'''

FAKE_STORE = r'''
object FakeStore {
    var px: IntArray? = null
    var tw = 0
    var th = 0
    fun create(a: IntArray, w: Int, h: Int): Any { px = a; tw = w; th = h; return Any() }
}

class FakeBitmap(val w: Int, val h: Int) {
    fun compressTo(bos: java.io.ByteArrayOutputStream): Boolean {
        val a = FakeStore.px!!
        val sb = StringBuilder()
        for (p in a) {
            val r = (p shr 16) and 0xFF
            val g = (p shr 8) and 0xFF
            val b = p and 0xFF
            if (sb.isNotEmpty()) sb.append(',')
            sb.append(r).append(' ').append(g).append(' ').append(b)
        }
        bos.write(sb.toString().toByteArray(Charsets.ISO_8859_1))
        bos.write('\n'.code)
        return true
    }
    fun recycle() {}
}
'''

# RGBA reader 的格式常量。
# ⚠ **数值不在这里定**：真值来自真机 SDK 的 `javap -constants`（`flex_rgba_value()`），
#   Android 各版本可能改。实测 API 34 = **42（0x2A）**，即 `PixelFormat.RGBA_8888`。
#   我第一版在这里写死 `0x7F420888`（想当然以为它等于 HAL 的四字符码）⇒
#   **被本判据的 P16c' 当场抓出来** —— 那正是"不硬编码常量"这条要求的价值：
#   写死的话，判据只会验证"分派与我造的值一致"，**恒真且毫无意义**。
RGBA_FMT_FALLBACK = 42


def rgba_fixture(w: int, h: int, row_pad: int = 0, pixel_stride: int = 4,
                 order: str = "RGBA", base_pos: int = 0) -> dict:
    """构造 RGBA 平面 + **每个输出像素的真值**（按 `order` 决定的字节序）。"""
    px_row = w * pixel_stride
    rs = px_row + row_pad
    # ⚠ 尾部余量必须**足够大**：真实 camera buffer 的容量通常大于有效区（对齐/池化），
    #   而 `base_pos>0`（position 标记偏移）会让最后一行的最后一个像素再往后 `base_pos` 字节。
    #   余量不足 ⇒ harness 抛 IndexOutOfBoundsException ⇒ 判据报"执行失败"，
    #   而**真正想验的行为根本没跑**（报错离原因很远，且会让人怀疑设备代码）。
    # ⇒ 给 `+4*row_pad + 256`，并让它与 `base_pos` 无关地宽裕。
    buf = bytearray(rs * h + 4 * row_pad + 256)
    truth = []
    for r in range(h):
        for c in range(w):
            R = (r * 7 + c * 13) % 256
            G = (r * 11 + c * 3) % 256
            B = (r * 5 + c * 17) % 256
            truth.append((R, G, B))
            # ⚠ 像素数据写在 `base_pos` **之后**：position 的语义是
            #   "有效区从第 base_pos 字节开始"，不是"每个像素再偏移 base_pos"。
            #   漏掉这一点 ⇒ 真值画在错误位置，而被测代码（正确的）
            #   反而"错" ⇒ **判据会把正确实现判成 FAIL**。
            o = base_pos + r * rs + c * pixel_stride
            if order == "RGBA":
                buf[o] = R & 0xFF
                buf[o + 1] = G & 0xFF
                buf[o + 2] = B & 0xFF
                buf[o + 3] = 255
            else:                              # 反例用：写反的 BGR
                buf[o] = B & 0xFF
                buf[o + 1] = G & 0xFF
                buf[o + 2] = R & 0xFF
                buf[o + 3] = 255
    return {"w": w, "h": h, "rowStride": rs, "pixelStride": pixel_stride,
            "bytes": list(buf), "truth": truth, "base_pos": base_pos}


def write_rgba_spec(fx: dict) -> str:
    """⚠ **字段顺序必须与 harness 的 `nxt()` 严格一致**：`w h rowStride pixelStride base_pos n`。

    本轮真实踩到：spec 里只写了 4 个头字段（缺 `w`/`h`），harness 读 6 个
    ⇒ 后面全部**错位一格**（`rs` 读到 0、`ps` 读到 1792、`n` 读到 0）
    ⇒ 报的是 `IndexOutOfBoundsException`，**离真正的原因（spec 与 harness 字段数不符）
    十万八千里**。
    ⇒ 两处都加了"自描述"：spec 里带 `w`/`h`，且 `_spec_fields()` 是**唯一**的字段定义处，
    harness 的读取顺序由它生成 —— 以后加字段不会再错位。
    """
    head = _spec_fields()
    toks = [str(fx[k]) for k in head[:-1]] + [str(len(fx["bytes"]))]
    return " ".join(toks + [str(b & 0xFF) for b in fx["bytes"]])


def _spec_fields() -> list:
    """RGBA spec 的字段顺序（**唯一**定义处）。"""
    return ["w", "h", "rowStride", "pixelStride", "base_pos", "n"]


def build_rgba_harness(color_fn: str, rgba_fn: str, spec_path: str,
                       fmt: int = RGBA_FMT_FALLBACK, helpers: str = "",
                       fmt_const: int = RGBA_FMT_FALLBACK) -> dict:
    # ⚠ 先把整段拼好、再统一替换占位符。写成
    #   `a + b + r'''...'''.replace(...)` 时，那个 `.replace` 只作用在
    #   **最后一段字面量上**（Python 的隐式字符串拼接优先级高于方法调用）
    #   ⇒ 前面几段里的占位符永远不被替换，而错误要等到 Kotlin 编译时才炸，
    #   报的是"unresolved reference: __FMT_CONST__"，
    #   **离真正的原因（拼接写法）很远** —— 这正是本轮真实踩到的那一脚。
    probe = ("import java.io.File\n"
             "import android.media.Image\n\n"
             "val liveQuality = 70\n"
             "// 常量值由真机 SDK 的 javap 推导后注入（见 flex_rgba_value），非硬编码。\n"
             "val IMG_FMT_RGBA = __FMT_CONST__\n\n"
             + color_fn + "\n\n" + rgba_fn + "\n\n" + helpers + "\n\n"
             + 'fun main() {\n'
               '    val toks = File("@@SPEC@@").readText().trim()\n'
               # ⚠ 分隔正则必须是 `\s+`（**恰好一个**反斜杠）——两种写法各错一半：
               #   · 普通字符串 "\\s+"  → Kotlin 源里是 `\s+` ⇒ Regex 匹配**字面反斜杠+s**
               #     ⇒ split 切不开 ⇒ 字段全错位 ⇒ 报错是后面的 IndexOutOfBounds，
               #       **离原因十万八千里**；
               #   · raw string "\\s+"  → Kotlin 源里是 `\s+` ⇒ 编译报
               #     illegal escape: '\s'。
               # ⇒ 正确写法：普通字符串 + **四个**反斜杠的 Python 字面量
               #   （即 Python 源码里写 "\\\\s+"）。
               '        .split(Regex(' + '"' + chr(92)*2 + 's+"' + '))\n'
               '    var t = 0\n'
               '    fun nxt(): Int = toks[t++].toInt()\n'
               '    val w = nxt(); val h = nxt()\n'
               '    val rs = nxt(); val ps = nxt(); val bp = nxt(); val n = nxt()\n'
               '    // ⚠ ByteArray 有符号：-124 & 0xFF == 132\n'
               '    val bytes = ByteArray(n) { toks[t++].toInt().toByte() }\n'
               '    val bb = java.nio.ByteBuffer.wrap(bytes)\n'
               '    if (bp != 0) bb.position(bp)\n'
               '    val img = Image(w, h, @@FMT@@, arrayOf(Image.Plane(bb, rs, ps)))\n'
               '    println("###RGB###")\n'
               '    val bos = java.io.ByteArrayOutputStream(1 shl 20)\n'
               '    val jpg = encodeColorJpeg(img, w, h)\n'
               '    if (jpg == null) println("NULL") else print(String(jpg))\n'
               '}\n')
    probe = (probe.replace("@@SPEC@@", spec_path)
                  .replace("@@FMT@@", str(fmt))
                  .replace("__FMT_CONST__", str(fmt_const)))
    # 兜底自检：占位符没替换干净就在**这里**炸，而不是等 Kotlin 编译时报
    # 一个离原因十万八千里的 "unresolved reference"。
    if "@@" in probe or "__FMT" in probe:
        raise RuntimeError("harness 占位符没替换干净:\n" + probe[:400])
    return {
        "Probe.kt": probe,
        "Image.kt": RGBA_STUB,
        "Fake.kt": FAKE_STORE,
    }

def parse_rgb(stdout: str):
    m = re.search(r"###RGB###\n(.*)", stdout, re.S)
    if not m:
        raise RuntimeError("harness 输出格式不符:\n" + stdout[:1500])
    body = m.group(1).strip()
    if body == "NULL":
        return []
    vals = [int(x) for x in body.replace(",", " ").split()]
    return [tuple(vals[i:i + 3]) for i in range(0, len(vals) - 2, 3)]


# ────────────────────────────── 反例：把通道顺序写反

def swapped_rgba_fn(rgba_fn: str) -> str:
    """把通道顺序改成 B,G,R（红蓝颠倒）。

    ⚠ 必须**按整行一次性替换**：先前一版先做 R→B、再做 B→R，
    第二步会把第一步刚写好的行又改回去 ⇒ 结果是两个 `val R`（编译报
    "conflicting declarations"）⇒ 判据变成"反例构造失败"，而不是"反例被判否"。
    **反例自己也必须先能编译**，否则"判否"分不清是"抓到了"还是"反例坏了"。
    """
    out = rgba_fn
    r_line = re.search(r"^(\s*)val R = buf\.get\(.*$", out, re.M)
    if not r_line:
        return out
    ind = r_line.group(1)
    old = (f"{ind}val R = buf.get(o).toInt() and 0xFF\n"
           f"{ind}val G = buf.get(o + 1).toInt() and 0xFF\n"
           f"{ind}val B = buf.get(o + 2).toInt() and 0xFF")
    new_block = (f"{ind}val B = buf.get(o).toInt() and 0xFF\n"
                 f"{ind}val G = buf.get(o + 1).toInt() and 0xFF\n"
                 f"{ind}val R = buf.get(o + 2).toInt() and 0xFF")
    if old not in out:
        return out                      # 行文本与预期不同 ⇒ 原样返回，让编译报错暴露它
    return out.replace(old, new_block)


def undispatched_color_fn(color_fn: str) -> str:
    """去掉格式分派 ⇒ RGBA reader 也走 YUV 路（这就是"假旁路"）。"""
    return re.sub(r"if \(img\.format == \w+\) \{\s*return \w+\(img, tw, th\)\s*\}",
                  "", color_fn)


# ────────────────────────────── 主流程

def main() -> int:
    print("P16  实时道颜色来源旁路（color_source）—— 二分绿偏的判决性实验")
    print("=" * 78)

    tc = kotlin_toolchain()
    if tc is None:
        check("P16a Kotlin 工具链可用（缺编译器必须判 FAIL，不静默跳过）", False,
              "**不许**因为跑不了就放过这一条（那正是上一轮的盲区）")
        return 1
    check("P16a Kotlin 工具链可用", True)

    src = KT_SRC.read_text(encoding="utf-8")
    py = SERVER.read_text(encoding="utf-8")

    # ---- P16b 真切函数体
    color_fn = extract_function(src, "encodeColorJpeg")
    rgba_fn = extract_function(src, "encodeRgbaJpeg")
    rr_fn = extract_function(src, "resolveReaderFormat")
    # ⚠ **共用函数也必须一并切出来**：`encodeColorJpeg` 与 `encodeRgbaJpeg`
    #   都调 `drawCrosshair` / `jpegEncode`。不切 ⇒ harness 编译报
    #   "unresolved reference"，而**真正的原因（缺了依赖函数）离报错点很远**
    #   —— 排查方向会被带偏（以为是代码写错了，其实是判据没给全依赖）。
    helpers = "\n\n".join(x for x in [
        extract_function(src, "drawCrosshair"),
        extract_function(src, "jpegEncode"),
        # ⚠ `planeToBytes` **必须**带上：`encodeColorJpeg` 的 YUV 分支引用它，
        #   即使本判据只走 RGBA 路，Kotlin 也要**编译通过**整段函数体。
        #   少给 ⇒ 报 "unresolved reference: planeToBytes"，
        #   而它离真正的问题（我以为只测一半）很远。
        extract_function(src, "planeToBytes"),
    ] if x)
    # `jpegEncode` 里是 `android.graphics.Bitmap.createBitmap(...)` / `.compress(...)`
    # —— 真实 Android SDK 不在编译 classpath 上（也没有实现），必须改写成替身。
    # ⚠ 改写范围**只限这两个调用**，色彩数学一行不动（P16g 会钉住这一点）。
    helpers = (helpers
               .replace("val bmp = android.graphics.Bitmap.createBitmap(px, tw, th,\n"
                        "            android.graphics.Bitmap.Config.ARGB_8888)",
                        "val bmp = FakeBitmap(tw, th).also { FakeStore.create(px, tw, th) }")
               .replace("bmp.compress(android.graphics.Bitmap.CompressFormat.JPEG, liveQuality, bos)",
                        "bmp.compressTo(bos)"))
    if "FakeBitmap" not in helpers:
        note("⚠ jpegEncode 未改写成功 ⇒ harness 会因缺 Bitmap 而编译失败")
    code_color = strip_kotlin_comments(color_fn or "")
    code_rgba = strip_kotlin_comments(rgba_fn or "")
    code_rr = strip_kotlin_comments(rr_fn or "")
    ok = check("P16b 真的切出了三个被测函数体（`encodeColorJpeg`/`encodeRgbaJpeg`/"
               "`resolveReaderFormat`）",
               bool(color_fn) and bool(rgba_fn) and bool(rr_fn),
               f"encodeColorJpeg={len(color_fn or '')}B "
               f"encodeRgbaJpeg={len(rgba_fn or '')}B "
               f"resolveReaderFormat={len(rr_fn or '')}B")
    if not ok:
        return 1

    # ---- P16c 格式常量锚定
    # ⚠ 必须**按赋值目标名精确匹配** `IMG_FMT_RGBA`：若用 `\w*RGBA\w*` 这类模糊模式，
    #   会先命中 `COLOR_SRC_RGBA_HAL = "rgba_hal"` —— 一个**字符串字面量**，
    #   于是判据一直在对着不相干的常量做检查（恒真且毫无意义）。
    #   这正是"判据里的字面清单要问是**不变量**还是**现状**"的又一个实例。
    const_line = re.search(r"const val IMG_FMT_RGBA\s*=\s*([^\n]+)", src)
    uses_flex = bool(const_line) and "FLEX_RGBA_8888" in const_line.group(1)
    # 数值从真机 SDK 的 javap 输出里取（不硬编码）—— 见 flex_rgba_value 的告警。
    flex_val = flex_rgba_value()
    flex_present = None
    sdk_jar = _find_sdk_android_jar()
    if sdk_jar and const_line:
        try:
            import zipfile
            with zipfile.ZipFile(sdk_jar) as z:
                flex_present = _read_java_int_constant(
                    z.read("android/graphics/ImageFormat.class"), "FLEX_RGBA_8888")
        except Exception:
            flex_present = None
    # 反向自检：`ImageFormat.RGBA_8888` 在 Camera2 里是隐藏常量，SDK 未导出 ⇒ 写了编译就失败。
    # 所以判据要确认**代码里**不把它当可用的符号（注释里提一句是允许的、且应该）。
    bad_symbol = re.search(r"ImageFormat\.RGBA_8888\s*[,)=]", strip_kotlin_comments(src))
    check("P16c 源码锚定：`IMG_FMT_RGBA` 用 `ImageFormat.FLEX_RGBA_8888`"
          "（⚠ `ImageFormat.RGBA_8888` 是 Camera2 隐藏常量，"
          "android.graphics.ImageFormat **没导出**它 ⇒ 写它编译直接失败）",
          uses_flex and not bad_symbol,
          f"IMG_FMT_RGBA = {(const_line.group(1).strip() if const_line else '**未找到该常量**')}"
          + (f"；⚠ 代码里仍出现 ImageFormat.RGBA_8888 于偏移 {bad_symbol.start()}"
             if bad_symbol else "")
          + (f"；SDK 中该符号存在={flex_present}" if flex_present is not None else ""))

    check("P16c' 能从真机 SDK 读到 `FLEX_RGBA_8888` 的数值"
          "（判据不硬编码它 —— 硬编码会让'分派'这项变成**自证的恒真**："
          "它只验证'分派与我自己造的值一致'）",
          flex_val == RGBA_FMT_FALLBACK,
          f"javap 读得 FLEX_RGBA_8888 = "
          + (hex(flex_val) if flex_val else "**读不到**（javap 或 android.jar 缺失）")
          + f"，判据默认夹具值 = {hex(RGBA_FMT_FALLBACK)}")
    if flex_val is None:
        # 读不到数值就**判 FAIL**，不许"用默认值凑合跑" —— 那样这项恒真。
        check("P16c'（缺 SDK 时必须判 FAIL，不许用硬编码值凑合）", False,
              "javap/android.jar 不可用 ⇒ 无法确认判据夹具与真机常量一致")

    # ---- P16d 格式分派
    disp = re.search(r"if \(img\.format\s*==\s*\w+\)\s*\{\s*return\s+\w+\(img,\s*tw,\s*th\)\s*\}",
                     code_color)
    check("P16d 源码锚定：`encodeColorJpeg` **按 `img.format` 分派**到 RGBA 路"
          "（按 color_source 字面分派是错的：HAL 不支持时两者会不一致 ⇒ 去读不存在的 Y 平面）",
          bool(disp),
          "没有按 img.format 分派 ⇒ rgba_hal 会走 YUV 路（假旁路，实验归零）")

    # ---- P16e 降级必须报出
    # ⚠ 检查范围是**降级分支自身**的切片，不是整文件 —— 整文件搜 log( 会命中
    #   别处的日志，于是"降级那行被删掉"也照样搜得到 ⇒ P16e 与 P16j 双双恒真。
    # ⚠ 降级分支：从 `if (avail.isNullOrEmpty()) {` 到该分支的 `return ImageFormat…`。
    # ⚠⚠ `isNullOrEmpty()` 后面那个 **`) {`** 的右括号**不能漏**：写成
    #   `isNullOrEmpty\(\)\s*\{` 会去匹配 `isNullOrEmpty` + `()` + `{`，
    #   而源码里 `()` 与 `{` 之间还夹着一个 `)`（来自 `if (...)`）⇒ 永远不匹配。
    #   症状是判据报"降级分支不存在"，**看起来像代码没写降级**，
    #   真实原因只是正则少了一个字符 —— 排查方向被带偏。
    _DEG_PAT = (r"avail\.isNullOrEmpty\(\)\)\s*\{"           # 降级分支起点（含 if 的右括号）
                r"(.*?)"                                    # 切片内容
                r"return\s+ImageFormat\s*\.?\s*YUV_420_888")  # 该分支的 return
    deg_m0 = re.search(_DEG_PAT, code_rr, re.S)
    deg_slice0 = deg_m0.group(1) if deg_m0 else ""
    degrade_reports = (re.search(r"\blog\s*\(", deg_slice0) is not None
                       and re.search(r"liveColorSource\s*=\s*COLOR_SRC_YUV_BT601",
                                     deg_slice0) is not None)
    up_reports = re.search(r'put\("color_source",\s*liveColorSource\)', src) is not None
    # ⚠ PC 侧是**普通字典键**，不是 `put(...)` —— 两端写法不同，
    #   判据要分别按各自的形态找，否则会误报"PC 没接收"。
    up_eff = ('"color_source_effective"' in py
              and re.search(r'lv\.get\("color_source"\)', py) is not None)
    up_req = '"color_source_requested"' in py
    check("P16e 降级**必须大声报出**，且实际生效值**必须上报**"
          "（静默降级 ⇒ 两条路是同一条，而画面照常出得来、**看起来完全正常**）",
          degrade_reports and up_reports and up_eff and up_req,
          f"App 日志={degrade_reports} App 上报生效值={up_reports} "
          f"PC 收生效值={up_eff} PC 留请求值={up_req}")

    # ---- P16f/g 行为：RGBA 逐字节 + 不做 YUV 算术
    W, H = 24, 16
    spec_path = tempfile.mktemp(prefix="p16_spec_", suffix=".txt").replace("\\", "/")
    fx = rgba_fixture(W, H, row_pad=0)
    Path(spec_path).write_text(write_rgba_spec(fx), encoding="utf-8")

    harness = build_rgba_harness(color_fn, rgba_fn, spec_path, helpers=helpers)
    try:
        got = parse_rgb(run_kotlin(tc, harness, "rgba"))
        # 十字准线会覆盖中心几个像素 ⇒ 逐字节比对时排除中心 ±arm
        arm = max(6, W // 20)
        cx0, cy0 = W // 2, H // 2
        skipped = set()
        for d in range(-arm, arm + 1):
            skipped.add(cy0 * W + min(max(cx0 + d, 0), W - 1))
            skipped.add(min(max(cy0 + d, 0), H - 1) * W + cx0)
        mism = []
        for idx, truth in enumerate(fx["truth"]):
            if idx in skipped:
                continue
            g = got[idx] if idx < len(got) else None
            if g != truth:
                mism.append((idx, g, truth))
        check("P16f RGBA 档**逐像素**等于夹具真值（pixelStride=4, 行不补齐）",
              not mism and len(got) == W * H,
              f"取到 {len(got)}/{W*H}；首个错位 {mism[0] if mism else '无'}")

        # 行补齐 + 非零 base position（HAL 用 position 标记偏移的常见做法）。
        # ⚠ 真值必须**只算一次**，且与 `base_pos` 无关 —— 因为像素数据就写在
        #   buffer 里 `base_pos` 之后的位置上（`base_pos=3` 表示"有效区从第 3 字节起"），
        #   不是"每个像素再往后挪 3 字节"。
        fx2 = rgba_fixture(W, H, row_pad=16, base_pos=3)
        Path(spec_path).write_text(write_rgba_spec(fx2), encoding="utf-8")
        got2 = parse_rgb(run_kotlin(tc, harness, "rgba_pad"))
        mism2 = [(i, g, t) for i, (g, t) in enumerate(zip(got2, fx2["truth"]))
                 if i not in skipped and g != t]
        check("P16f' 行补齐 16 B + position=3 时仍逐像素正确"
              "（基址取 `buf.position()` 而非写死 0）",
              not mism2 and len(got2) == W * H,
              f"首个错位 {mism2[0] if mism2 else '无'}")

        arith = re.findall(r"298\s*\*|\b409\b|\b516\b|bt601|BT\.601", code_rgba)
        check("P16g RGBA 档里**没有任何 YUV→RGB 算术**"
              "（HAL 已算好，再算一遍就是把两个来源混起来 = 假旁路）",
              not arith,
              f"仍找到 {arith[:5] if arith else '—'}")

        # ---- P16h 反例：通道顺序写反必须判否
        Path(spec_path).write_text(write_rgba_spec(fx), encoding="utf-8")
        h_bad = build_rgba_harness(color_fn, swapped_rgba_fn(rgba_fn), spec_path,
                                  helpers=helpers)
        got_bad = parse_rgb(run_kotlin(tc, h_bad, "swapped"))
        mism_bad = [(i, g, t) for i, (g, t) in enumerate(zip(got_bad, fx["truth"]))
                    if i not in skipped and g != t]
        check("P16h **反例**：把通道顺序改成 B,G,R 后，同一套判据必须判否"
              "（红蓝颠倒与偏绿是两个故障，混淆会把排查带偏一整条街）",
              bool(mism_bad),
              f"BGR 版错位像素数 = {len(mism_bad)}（应 > 0）")
        if mism_bad:
            i, g, t = mism_bad[0]
            note(f"反例首个错位：得到 {g}，真值 {t}（R↔B 互换）")

        # ---- P16i 反例：去掉格式分派必须判否
        undisp = undispatched_color_fn(color_fn)
        h_und = build_rgba_harness(undisp, rgba_fn, spec_path, helpers=helpers)
        failed = False
        try:
            g_und = parse_rgb(run_kotlin(tc, h_und, "undispatched"))
            failed = (g_und != got)
        except Exception as e:
            failed = True
            note(f"无分派版直接崩了（也算判否）：{str(e)[:110]}")
        check("P16i **反例**：去掉 `img.format` 分派后必须判否"
              "（RGBA reader 只有一个 plane，去读 Y 平面就是假旁路）",
              failed, "分派被去掉但结果不变 ⇒ P16d 是恒真的")

        # ---- P16j 反例：静默降级必须判否（源码级）
        #构造一个"静默降级版"的 resolveReaderFormat：把 log(...) 换成 noop(...)
        # 并去掉 `liveColorSource = COLOR_SRC_YUV_BT601`（不写生效值）。
        # 判定：**降级可观测性必须只由"降级分支自身"提供** ——
        # 若判据只搜"文件里有没有 log("，那么把降级那行删掉也照样搜得到别处的 log
        # ⇒ 判据恒真。所以这里直接在**降级分支的切片内**查。
        silent_rr = re.sub(r"log\(", "noop(", code_rr)
        silent_rr = re.sub(r"liveColorSource\s*=\s*COLOR_SRC_YUV_BT601;?", "", silent_rr)
        # 降级分支：从 "不支持" 到 return YUV 之间那一段（正则见 P16e 的 _DEG_PAT）
        deg_m = re.search(_DEG_PAT, silent_rr, re.S)
        deg_slice = deg_m.group(1) if deg_m else ""
        still_observable = bool(re.search(r"\b(log|noop)\s*\(", deg_slice)) and \
            bool(re.search(r"liveColorSource\s*=\s*COLOR_SRC_YUV_BT601", deg_slice))
        check("P16j **反例**：把降级改成静默（去掉日志 + 不写生效值）必须判否"
              "（检查范围必须是**降级分支自身**，不能是整文件 —— "
              "整文件搜 log 会命中别处，判据就成了恒真）",
              not still_observable,
              f"降级分支切片 = {(deg_slice.strip()[:90] + '…') if deg_slice else '**未找到该分支**'}")
        if not deg_slice:
            check("P16j' 降级分支能被切出来（否则上面的反例无意义）", False,
                  "resolveReaderFormat 里找不到 'avail.isNullOrEmpty()' 到 return YUV 的片段")
    finally:
        if os.path.exists(spec_path):
            os.remove(spec_path)

    # ---- P16k/l PC 侧：取值校验与跨进程一致
    py_code = strip_py_comments(py)
    bad_src = re.search(r'if src not in COLOR_SOURCES:.*?bad_color_source', py_code, re.S)
    not_allowed = re.search(r'if src == COLOR_SRC_RGBA_HAL and mode != MODE_LIVE:.*?'
                            r'color_source_not_allowed', py_code, re.S)
    check("P16k PC 侧：非法 `color_source` → 400；`deliver` + `rgba_hal` → 400"
          "（放行会让设备静默降级，而会话记着用户请求了 rgba_hal ⇒ 声明与实际不符）",
          bool(bad_src) and bool(not_allowed),
          f"非法校验={bool(bad_src)} 模式互斥={bool(not_allowed)}")

    py_vals = set(re.findall(r'"(yuv_bt601|rgba_hal)"', py))
    kt_vals = set(re.findall(r'const val COLOR_SRC_\w+ = "(\w+)"', src))
    check("P16l 跨进程取值集合一致（两端漂移 ⇒ 设备静默降级，**且没有任何报错**）",
          py_vals == kt_vals and len(kt_vals) == 2,
          f"PC={sorted(py_vals)} App={sorted(kt_vals)}")

    # ---- P16m 面板 option 交叉校验（从 COLOR_SOURCES 自身推导）
    py_cols = set(re.findall(r'"(yuv_bt601|rgba_hal)"', py))
    opt_vals = set(re.findall(r'<option value="(yuv_bt601|rgba_hal)"', py))
    check("P16m 面板 `<option>` 集合与 `COLOR_SOURCES` 一致"
          "（第 12 条铁律：界面能做的 ≠ 接口能做的；加控件忘接口是静默缺陷）",
          opt_vals == py_cols and len(opt_vals) == 2,
          f"面板={sorted(opt_vals)} 接口={sorted(py_cols)}")

    # ---- P16n summary 同时给两值
    check("P16n summary **同时**给请求值与实际生效值，并算出降级标志"
          "（二分结论的前置条件：只给请求值会让人以为在跑 HAL 档）",
          all(k in py for k in ('"color_source_requested"', '"color_source_effective"',
                                '"color_source_degraded"')),
          "缺字段 ⇒ 面板无法显示降级")

    # ─────────────── 汇总
    passed = sum(1 for ok, _ in _RES if ok)
    total = len(_RES)
    print("=" * 78)
    print(f"P16 汇总：{passed}/{total} 通过" + ("" if passed == total else "  ← 有 FAIL"))
    for ok, name in _RES:
        if not ok:
            print(f"   FAIL: {name}")
    return 0 if passed == total else 1


def strip_py_comments(src: str) -> str:
    """剥掉 Python 注释 —— 否则源码锚定会命中我自己在注释里写的解释文字
    （第 30 条铁律的变体：判据命中注释 ⇒ 逼人改掉正确的注释）。"""
    out = []
    for line in src.split("\n"):
        # 只剥整行注释；行尾注释在字符串里很难分辨，但本判据的锚点都不含 #
        s = re.sub(r"^\s*#.*$", "", line)
        out.append(s)
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(main())