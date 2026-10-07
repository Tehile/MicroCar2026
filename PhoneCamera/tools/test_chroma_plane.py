"""判据 P15：设备端**色度链路**的正确性（离线，不需真机，真编译真执行）。

════════════════════════════════════════════════════════════════════
【第 30 轮现场】

用户看 live 画面报「明显解码和编码不是同一套」。从截图反解出的硬事实：

| 测量 | 数值 |
|---|---|
| 页面暗背景 vs CSS `#151a21` | `(21,26,33)` 一致 ⇒ 截图/浏览器**没有**偏色 |
| 由显示像素反解的 U/V 均值 | `115.1 / 119.7`（两者**都**低于中性 128） |
| 色度标准差 `Cbstd/Crstd` | `6.94 / 5.72` |

双向负偏移在 BT.601 下展开：`G += -100U - 208V` 变大、`R += 409V` 变小、
`B += 516U` 变小最多 ⇒ **偏绿**，与现场一致。

════════════════════════════════════════════════════════════════════
【本轮真正查清的两件事：一件是代码缺陷，一件是我自己上一轮的推断缺陷】

### (1) 代码缺陷（已修）：`planeToBytes` 的快路径依赖**可变 buffer 状态**

```kotlin
if (pixelStride == 1 && rowStride == w) buf.get(out, 0, w * h)   // ← 相对 position
else if (pixelStride == 1) { buf.position(r * rowStride); ... } // ← 改 position
```

三条分支里只有快路径**不设定** position 就相对读 ⇒ 它的正确性依赖一个
**本函数既不建立也不检查**的外部不变量（`Image.Plane.buffer` 的初始 position）。
两条路都合法，代码却只在一条路上自洽。

⚠ **诚实标注**：本条判据能证明"这是个真实潜伏缺陷"，**不能**证明
"它就是截图中偏绿的成因"—— 见 (2)。没有真机，因果链断在最后一步。

### (2) 我上一轮的推断缺陷（本条判据必须自己先钉死，否则等于拿着错前提修代码）

我曾断言"色度动态范围塌到 6.94 ⇒ 色度字节取错位置"。**这个断言未经检验**：
截图里的 `Cbstd` 是在 **手机 JPEG q70 → 浏览器降采样显示**之后测的，
而 JPEG 4:2:0 q70 的色度 DCT 量化**本来就会压低色度标准差**。
⇒ "6.94 算不算低"必须先有**基线**，否则整个推理建在流沙上。

`P15k` 就是这个基线：拿**判据自己认为正确**的输出，过一遍同款 JPEG q70 +
同款降采样，再测一次色度标准差。**若 6.94 落在基线允许的范围内，
那么"动态范围塌陷"就不是证据，我上一轮的因果推断必须撤回。**

同理，"BT.601/709 矩阵用错被排除"的理由（中性灰在两套矩阵下相同）也只
适用于**低饱和**内容；`P15i` 用**高饱和**夹具证明本判据对矩阵系数是敏感的
—— 但"这台手机相机吐的是 601 还是 709"是**设备事实、不是代码事实**，
离线判不了，只能上真机比对已知色卡。

════════════════════════════════════════════════════════════════════
【P13c 的盲区 —— 本条存在的理由】

`test_interop.py` 的 P13c 用 **cv2 在 PC 侧合成**左红右蓝的 JPEG 推上去，
验的是"PC 转发链路没把颜色弄坏"——**出问题的代码根本不在这条路径上**
（YUV→RGB 发生在手机 App 里）。⇒ P13c **结构上不可能**发现 App 侧色度错误。
**"判据只覆盖了被观测对象的一半"。**

════════════════════════════════════════════════════════════════════
【本条怎么避免自欺】

1. **真编译真执行**，且**不改被测表达式**：从 `DeliverySession.kt` 切出
   `planeToBytes` 与 `encodeColorJpeg` 的**真实源码**贴进 harness；只替换
   `Bitmap/JPEG` 这两个**被测对象之外**的调用（并在 `P15s` 里断言色度数学
   那几行**逐字节未被改动**）—— 抄一份 Python 等价物会与真机代码悄悄分叉。
2. **基准自检先跑**（`P15z`）：中性灰必须出 `R=G=B`；`P15z` 不过则后续数字全作废。
3. **反例必须判否**（`P15g/h/i`），否则 P15 只是"跑了个空"。
4. **缺 Kotlin 编译器 ⇒ 判 FAIL，不静默跳过**（沿用 A9m4 纪律）。
"""

from __future__ import annotations

import glob
import io
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KOTLIN_SRC = (ROOT / "android" / "m0probe" / "app" / "src" / "main" / "java"
              / "com" / "mouse" / "phoneprobe" / "DeliverySession.kt")

_fail: list[str] = []
_ok = 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global _ok
    if cond:
        _ok += 1
        print(f"  [PASS] {name}")
        return True
    _fail.append(name)
    print(f"  [FAIL] {name}\n         {detail}")
    return False


def note(line: str) -> None:
    print(f"  [INFO] {line}")


# ═══════════════════════════════════════════════════════════ 工具链

def _find_jar(pattern: str):
    hits = glob.glob(os.path.expanduser(pattern), recursive=True)
    return hits[0] if hits else None


def kotlin_toolchain():
    """Kotlin 编译器 + 依赖 jar。**找不到就返回 None ⇒ 判 FAIL，不跳过。**"""
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
    # ⚠ annotations 不是可选的：编译器**自己**在 codegen 阶段要读
    #   org.jetbrains.annotations.NotNull，缺了会在写 class 文件时炸
    #   NoClassDefFoundError（而不是报"源码有问题"—— 报错位置离原因很远）。
    annot = _find_jar(f"{home}/org.jetbrains/annotations/*/*/annotations-*.jar")
    if not (compiler and stdlib and script_rt and trove and daemon and annot):
        return None
    java = shutil.which("java")
    if not java:
        return None
    return {"java": java, "compiler": compiler, "stdlib": stdlib,
            "reflect": reflect, "script_rt": script_rt,
            "trove": trove, "daemon": daemon, "annot": annot}


def _cut_braced(src: str, from_idx: int):
    """从 `from_idx` 起按花括号配对切出整块（含签名行）。"""
    i = src.index("{", from_idx)
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[from_idx:j + 1]
        j += 1
    return None


def extract_function(src: str, name: str):
    """切出 `private fun <name>(...) { ... }` 的**真实函数体**（含签名）。

    ⚠ 必须真的切出函数体 —— 整文件搜关键词会命中别的调用点，
    那样判据就是**恒真**的（记忆第 6 条铁律）。
    """
    pat = re.compile(r"private\s+fun\s+" + re.escape(name) + r"\s*\(")
    m = pat.search(src)
    if not m:
        return None
    return _cut_braced(src, m.start())


def strip_kotlin_comments(src: str) -> str:
    """去掉 `//` 行注释与 `/* */` 块注释，**保留行数**（用空串替换）。

    ⚠⚠ 这条不是洁癖，是本判据自己踩到的：修复 `planeToBytes` 时我在注释里
    写了 `buf.get(out, 0, w * h)` 和 `buf.position(...)` 来解释**为什么**不再用它们，
    结果 P15c/P15d 立刻双双变红 —— **源码锚定命中了注释**。
    ⇒ 判据表达强于事实：它把"作者在解释这段代码"当成了"这段代码还在"。
    （与记忆第 5 条同族：**被测对象之外的编码不得进判据**，注释也是编码。）

    已知局限：不识别字符串字面量里的 `//`；被测的两个函数体内无字符串字面量，
    且 `P15s` 会断言色度数学那几行**逐字节未被改动**（等价于间接担保）。
    """
    src = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), src, flags=re.S)
    out = []
    for line in src.split("\n"):
        i = line.find("//")
        out.append(line if i < 0 else line[:i])
    return "\n".join(out)


def run_kotlin(tc: dict, files: dict, tag: str) -> str:
    """真编译真执行，返回 stdout。编译/执行失败 ⇒ 抛异常（判 FAIL，不跳过）。"""
    d = Path(tempfile.mkdtemp(prefix=f"p15_{tag}_"))
    try:
        for name, body in files.items():
            (d / name).write_text(body, encoding="utf-8")
        cp = os.pathsep.join([tc["compiler"], tc["stdlib"], tc["script_rt"],
                              tc["trove"], tc["daemon"], tc["annot"]]
                             + ([tc["reflect"]] if tc["reflect"] else []))
        out_dir = d / "classes"
        srcs = [str(d / n) for n in files]
        # ⚠ 三条踩过的坑，一起写下来免得下次重犯：
        #   ① `-cp`（编译器进程 classpath）≠ `-classpath`（被编译代码 classpath）；
        #      少后者 ⇒ "Ensure that you have a dependency on the Kotlin standard library"，
        #      而报错点在 harness 末尾的 println，离原因十万八千里。
        #   ② `-no-stdlib` 与 `-include-runtime` **互斥**：加了前者，jar 里就没有
        #      运行时 ⇒ `java -jar` 报 "Invalid or corrupt jarfile"（成功编译、假失败）。
        #      ⇒ 改为 `-d` 输出目录 + 显式 `java -cp classes;stdlib`。
        #   ③ 编译器 codegen 阶段自己要 `org.jetbrains.annotations.NotNull`，
        #      缺它 ⇒ 写 class 文件时 NoClassDefFoundError。
        p = subprocess.run(
            [tc["java"], "-cp", cp, "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler",
             *srcs, "-no-stdlib", "-nowarn",
             "-classpath", os.pathsep.join([tc["stdlib"]]
                                           + ([tc["reflect"]] if tc["reflect"] else [])),
             "-d", str(out_dir)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600)
        if not out_dir.exists() or not any(out_dir.rglob("*.class")):
            raise RuntimeError("Kotlin 编译失败:\n"
                               + (p.stdout or "")[-3000:] + (p.stderr or "")[-3000:])
        run_cp = os.pathsep.join([str(out_dir), tc["stdlib"]]
                                 + ([tc["reflect"]] if tc["reflect"] else []))
        # 文件名固定为 Main.kt ⇒ 顶层 main 的类名是 `MainKt`
        r = subprocess.run([tc["java"], "-cp", run_cp, "MainKt"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300)
        if r.returncode != 0:
            raise RuntimeError("Kotlin 执行失败:\n" + r.stdout[-3000:] + r.stderr[-3000:])
        return r.stdout
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ═══════════════════════════════════════════════════ 夹具（YUV420）

def yuv420_fixture(w: int, h: int, row_pad: int, pixel_stride: int,
                   init_pos=(0, 0, 0), pattern: str = "ramp",
                   shared_uv: bool = False, chroma_amp: int = 12) -> dict:
    """构造一份**自描述**的 YUV_420_888 布局 + 每个平面的**真值**。

    `pixel_stride=2` ⇒ NV12/NV21 交错 UV。
    ⚠ `shared_uv=True` 复现**真机布局**：NV12 的 U/V **共享同一个底层 buffer**，
      Camera2 靠 `planes[2].buffer` 的 `position=1` 标记 V 的字节偏移。
      ⇒ 判据必须覆盖这一种，否则会得出"绝对索引就万事大吉"的错误结论
      （本轮真机不存在时，这个坑是判据自己踩出来的）。
    """
    cw, ch = (w + 1) // 2, (h + 1) // 2
    if pattern == "neutral":
        Y = [[128] * w for _ in range(h)]
        U = [[128] * cw for _ in range(ch)]
        V = [[128] * cw for _ in range(ch)]
    elif pattern == "ramp":
        Y = [[(x * 255 // max(w - 1, 1)) for x in range(w)] for _ in range(h)]
        U = [[(40 + (x * 160 // max(cw - 1, 1))) for x in range(cw)] for _ in range(ch)]
        V = [[(200 - (x * 160 // max(cw - 1, 1))) for x in range(cw)] for _ in range(ch)]
    elif pattern == "scene":
        # 灰阶梯 + 左红块 + 右蓝块 + 上绿块 + 中性墙：
        # 低饱和与高饱和同时存在，用来分别检验"中性灰不变量"与"矩阵/顺序敏感性"。
        Y = [[200] * w for _ in range(h)]
        U = [[128] * cw for _ in range(ch)]
        V = [[128] * cw for _ in range(ch)]
        for r in range(h):
            for c in range(w):
                if c < w // 3:                        # 红块
                    Y[r][c] = 81
                    if r % 2 == 0 and c % 2 == 0:
                        U[r // 2][c // 2], V[r // 2][c // 2] = 90, 240
                elif c >= 2 * w // 3:                 # 蓝块
                    Y[r][c] = 41
                    if r % 2 == 0 and c % 2 == 0:
                        U[r // 2][c // 2], V[r // 2][c // 2] = 240, 110
                elif r < h // 3:                      # 绿块
                    Y[r][c] = 145
                    if r % 2 == 0 and c % 2 == 0:
                        U[r // 2][c // 2], V[r // 2][c // 2] = 54, 34
                elif r >= 2 * h // 3:                 # 灰阶梯（低饱和）
                    Y[r][c] = 40 + (c * 180 // max(w - 1, 1))
    elif pattern == "natural":
        Y = [[128] * w for _ in range(h)]
        U = [[128] * cw for _ in range(ch)]
        V = [[128] * cw for _ in range(ch)]
        # 接近真实场景：**低饱和**的渐变 + 确定性"噪声" + 一块中性墙。
        # ⚠ 饱和度必须低：第一版用纯色块（Cbstd≈80）时，JPEG q70 的色度 DCT
        #   几乎无损（压缩比 0.99）⇒ 基线毫无参考价值，会得出"链路几乎不压色度"
        #   的错误结论。真实室内画面的 Cbstd 大约 10~30，色度细节集中在**低幅**区域。
        import math as _m
        for r in range(h):
            for c in range(w):
                base = 70 + (c * 120 // max(w - 1, 1)) + (r * 40 // max(h - 1, 1))
                n = ((r * 37 + c * 17) % 11) - 5          # 确定性伪噪声
                Y[r][c] = max(0, min(255, base + n))
                # 色度：低幅摆动（±chroma_amp 左右），并让 V 略偏 ⇒ 模拟暖色室内光
                du = int(chroma_amp * 0.8 * _m.sin(c * 0.21 + r * 0.07)) + ((r + c) % 5) - 2
                dv = int(chroma_amp * _m.cos(c * 0.17 - r * 0.05)) + ((r * 2 + c) % 5) - 2
                U[r // 2][c // 2] = max(16, min(240, 128 + du))
                V[r // 2][c // 2] = max(16, min(240, 128 + dv))
        # 一块中性墙（用来验"中性不变量"在低饱和下依然成立）
        for r in range(h // 3, h // 3 + h // 4):
            for c in range(w // 3, w // 3 + w // 4):
                Y[r][c] = 190
                U[r // 2][c // 2] = 128
                V[r // 2][c // 2] = 128
    else:
        raise ValueError(pattern)

    return _pack(w, h, cw, ch, Y, U, V, init_pos,
                 row_pad, pixel_stride, shared_uv)


def _pack(w, h, cw, ch, Y, U, V, init_pos, row_pad, pixel_stride, shared_uv) -> dict:
    """把 Y/U/V 三个矩阵打包成**自描述**的平面布局（含 buffer 字节与初始 position）。"""
    # ⚠ 尾部补 256 字节（要**够大**）：旧实现在 NV12 上会越界读 V 平面。
    #   补少了 → BufferUnderflowException，**抛异常反而比静默读错更友好**，
    #   而现场的 bug 恰恰是"字节数对、位置错、**不抛任何异常**"。
    #   ⇒ 夹具必须让"读错位置"这件事**能够悄悄发生**，反例才有意义。
    TAIL = 256

    y_rs = w + row_pad
    # ⚠ 尾部补 256 字节（要**够大**）：旧实现在 NV12 上会越界读 V 平面。
    #   补少了 → BufferUnderflowException，**抛异常反而比静默读错更友好**，
    #   而现场的 bug 恰恰是"字节数对、位置错、**不抛任何异常**"。
    #   ⇒ 夹具必须让"读错位置"这件事**能够悄悄发生**，反例才有意义。
    TAIL = 256
    y_plane = bytearray(y_rs * h + TAIL)
    for r in range(h):
        for c in range(w):
            y_plane[r * y_rs + c] = Y[r][c] & 0xFF

    uv_rs = cw * pixel_stride + row_pad
    if pixel_stride == 1:                      # 平面式：U、V 各一个平面
        u_plane, v_plane = bytearray(uv_rs * ch + TAIL), bytearray(uv_rs * ch + TAIL)
        for r in range(ch):
            for c in range(cw):
                u_plane[r * uv_rs + c] = U[r][c] & 0xFF
                v_plane[r * uv_rs + c] = V[r][c] & 0xFF
        u_bytes, v_bytes, v_pos = list(u_plane), list(v_plane), 0
    elif shared_uv:                            # NV12：U 偶数位、V 奇数位，同一 buffer
        uv = bytearray(uv_rs * ch + TAIL)
        for r in range(ch):
            for c in range(cw):
                uv[r * uv_rs + c * 2] = U[r][c] & 0xFF
                uv[r * uv_rs + c * 2 + 1] = V[r][c] & 0xFF
        u_bytes = v_bytes = list(uv)
        v_pos = 1                              # ★ Camera2 就是这么标记 V 的
    else:                                      # 交错但两个独立 buffer（少见但合法）
        # ⚠ 每个 buffer **只放自己那一份**色度，仍按 pixelStride=2 隔位摆放
        #   （NV21 式的 V-first 交错）。若像早先那样往两个 buffer 里都写 U+V，
        #   V 平面读到的还是 U —— 那是**夹具自己错**，判据会把它误报成代码缺陷。
        u_plane, v_plane = bytearray(uv_rs * ch + TAIL), bytearray(uv_rs * ch + TAIL)
        for r in range(ch):
            for c in range(cw):
                u_plane[r * uv_rs + c * 2] = U[r][c] & 0xFF
                v_plane[r * uv_rs + c * 2] = V[r][c] & 0xFF
        u_bytes, v_bytes, v_pos = list(u_plane), list(v_plane), 0

    pos = list(init_pos)
    return {"w": w, "h": h, "cw": cw, "ch": ch, "Y": Y, "U": U, "V": V,
            "init_pos": [pos[0], pos[1], v_pos + pos[2]],
            "planes": [{"rowStride": y_rs, "pixelStride": 1, "bytes": list(y_plane)},
                       {"rowStride": uv_rs, "pixelStride": pixel_stride, "bytes": u_bytes},
                       {"rowStride": uv_rs, "pixelStride": pixel_stride, "bytes": v_bytes}]}


def write_spec(fx: dict) -> str:
    """纯空白分隔的 spec（不用 JSON —— 免去手写 JSON 解析器的分叉风险）。"""
    head = f"{fx['w']} {fx['h']} {fx['cw']} {fx['ch']}\n"
    body = ""
    for p, ip in zip(fx["planes"], fx["init_pos"]):
        body += f"{p['rowStride']} {p['pixelStride']} {ip} {len(p['bytes'])}\n"
        body += " ".join(str(b) for b in p["bytes"]) + "\n"
    with tempfile.NamedTemporaryFile("w", suffix=".spec", delete=False,
                                     encoding="utf-8") as f:
        f.write(head + body)
        return f.name


# ═══════════════════════════════════════════════════════ harness

HARNESS = r'''
import android.hardware.image.Image
import java.io.File

/** Bitmap/JPEG 的**替身**：只代替"被测对象之外"的编码环节，
 *  目的是把产物变成可逐像素断言的 ASCII，而不是压缩流。 */
class FakeBitmap {
    fun compressTo(bos: java.io.ByteArrayOutputStream): Boolean {
        val sb = StringBuilder()
        for (r in 0 until FakeStore.h) {
            if (r > 0) sb.append(';')
            for (c in 0 until FakeStore.w) {
                if (c > 0) sb.append(',')
                val p = FakeStore.px[r * FakeStore.w + c]
                sb.append((p ushr 16) and 0xFF).append(' ')
                  .append((p ushr 8) and 0xFF).append(' ')
                  .append(p and 0xFF)
            }
        }
        bos.write(sb.toString().toByteArray())
        return true
    }
    fun recycle() {}
}

object FakeStore {
    var w = 0; var h = 0; var px: IntArray = IntArray(0)
    fun create(px: IntArray, w: Int, h: Int): FakeBitmap {
        this.px = px; this.w = w; this.h = h
        return FakeBitmap()
    }
}

val liveQuality = 70

/**
 * ⚠ `encodeColorJpeg` 自 v1.15.0 起按 `img.format` 分派，需要这个常量才能编译。
 *   值 42 = `FLEX_RGBA_8888` 的**真值**（P16c' 用 `javap -constants` 从
 *   `android-34/android.jar` 读出并钉住；我第一版注释写成 0x7F420888，被它当场抓到）。
 *   本判据**不验**这个常量 —— 只用它让分派表达式编译得过；`Image.format` 默认 35
 *   ⇒ 运行时永远走不到这个分支。
 */
const val IMG_FMT_RGBA = 42

@@PLANE_FN@@

@@COLOR_FN@@

fun main() {
    val toks = File("@@SPEC@@").readText().trim().split(Regex("\\s+"))
    var t = 0
    fun nxt(): Int = toks[t++].toInt()
    val w = nxt(); val h = nxt(); val cw = nxt(); val ch = nxt()
    val sizes = listOf(w to h, cw to ch, cw to ch)

    // ⚠⚠ **每个"帧"必须新建一套 buffer**：真机每帧都是**新的 Image**（新 buffer），
    //   而 `buf.get(out,0,n)` 这类相对读会**推进 position**。
    //   我第一版 harness 把同一批 plane 对象先喂给 planeToBytes、再喂给
    //   encodeColorJpeg ⇒ 第二次 position 已被推走 ⇒ BufferUnderflowException。
    //   ⇒ 那个异常**是 harness 自己造的**，不是被测代码的问题。
    //   **差点又据此去"修"正确的代码**（现场坏、测试红 ⇒ 先比对夹具与真实调用方式）。
    fun newImage(): Image {
        var tt = 0
        fun n2(): Int = toks[tt++].toInt()
        n2(); n2(); n2(); n2()                       // w h cw ch（跳过）
        val ps3 = Array(3) {
            val rs = n2(); val ps = n2(); val ip = n2(); val n = n2()
            val b = ByteArray(n) { toks[tt++].toInt().toByte() }
            val bb = java.nio.ByteBuffer.wrap(b)
            if (ip != 0) bb.position(ip)               // ⚠ 合法的初始 position
            Image.Plane(bb, rs, ps)
        }
        return Image(w, h, ps3)
    }

    val sb = StringBuilder()
    val imgA = newImage()
    for (pi in 0 until 3) {
        val out = planeToBytes(imgA.planes[pi], sizes[pi].first, sizes[pi].second)
        sb.append(out.joinToString(",")).append('\n')
    }
    println("###PLANES###")
    print(sb)
    println("###RGB###")
    val jb = encodeColorJpeg(newImage(), w, h)          // ← 全新一帧
    println(if (jb == null) "NULL" else String(jb))
}
'''

IMAGE_STUB = r'''
package android.hardware.image

class Image(
    val width: Int,
    val height: Int,
    val planes: Array<Plane>,
    /**
     * ⚠ v1.15.0 起 `encodeColorJpeg` 按 **`img.format`** 分派（不按 color_source
     *   字面），所以替身**必须**有这个字段，否则 P15 会编译失败 ——
     *   而报错 "unresolved reference: format" 离真因（被测对象重构了、判据没跟上）
     *   很远，很容易被误读成"设备代码坏了"。
     *   ⚠ 默认值取 `YUV_420_888`（35）而不是 RGBA：本判据只验 **BT.601 那条路**，
     *   若默认成 RGBA，`encodeColorJpeg` 会直接转调 `encodeRgbaJpeg`（harness 里
     *   没有）⇒ 全部用例变成"跑的是另一条路"，而**输出看上去仍然正常**
     *   —— 那是**静默跑错被测分支**，最坏的一种假绿。
     */
    val format: Int = 35
) {
    /** 只带 `planeToBytes` / `encodeColorJpeg` 真正用到的三个成员。 */
    class Plane(
        val buffer: java.nio.ByteBuffer,
        val rowStride: Int,
        val pixelStride: Int
    )
}
'''

# 被测对象**之外**的两个调用（本判据唯一允许改写的地方，P15s 会钉住它）
REPLACEMENTS = [
    ("android.graphics.Bitmap.createBitmap(px, tw, th,\n"
     "            android.graphics.Bitmap.Config.ARGB_8888)",
     "FakeStore.create(px, tw, th)"),
    ("bmp.compress(android.graphics.Bitmap.CompressFormat.JPEG, liveQuality, bos)",
     "bmp.compressTo(bos)"),
]

# 色度数学的"指纹行"：改写后必须**逐字节**还在
COLOR_FINGERPRINT = [
    "val U = (u[cu * cw + cuv].toInt() and 0xFF) - 128",
    "val V = (v[cu * cw + cuv].toInt() and 0xFF) - 128",
    "val R = ((298 * yy + 409 * V + 128) shr 8).coerceIn(0, 255)",
    "val G = ((298 * yy - 100 * U - 208 * V + 128) shr 8).coerceIn(0, 255)",
    "val B = ((298 * yy + 516 * U + 128) shr 8).coerceIn(0, 255)",
]

OLD_PLANE_FN = r'''
private fun planeToBytes(p: Image.Plane, w: Int, h: Int): ByteArray {
    val buf = p.buffer
    val out = ByteArray(w * h)
    val rowStride = p.rowStride
    val pixelStride = p.pixelStride
    if (pixelStride == 1 && rowStride == w) {
        buf.get(out, 0, w * h)                        // ★ 反例：相对 position，未先设定
    } else if (pixelStride == 1) {
        for (r in 0 until h) { buf.position(r * rowStride); buf.get(out, r * w, w) }
    } else {
        for (r in 0 until h) {
            var idx = r * rowStride
            for (c in 0 until w) { out[r * w + c] = buf.get(idx); idx += pixelStride }
        }
    }
    return out
}
'''


def build_harness(plane_fn: str, color_fn: str, spec_path: str,
                  helpers: str = "") -> dict:
    cf = color_fn
    for a, b in REPLACEMENTS:
        cf = cf.replace(a, b)
    # ⚠ `encodeColorJpeg` 自 v1.15.0 起把「画准线」与「JPEG 编码」抽成了
    #   **共用私有函数** `drawCrosshair` / `jpegEncode`，并按 `img.format` 分派到
    #   `encodeRgbaJpeg`。harness 必须**一并切出**它们，否则编译报
    #   "unresolved reference: drawCrosshair" ——
    #   而**真正的原因**（被测对象重构了、判据没跟上）离那条报错很远，
    #   很容易被误当成"设备代码坏了"。
    #   ⚠ `REPLACEMENTS`（Bitmap 替身）也要作用在 helpers 上，
    #   否则 `jpegEncode` 会去调真实 Android SDK（不在 classpath 上）。
    hf = helpers
    for a, b in REPLACEMENTS:
        hf = hf.replace(a, b)
    src = (HARNESS.replace("@@PLANE_FN@@", plane_fn)
                  .replace("@@COLOR_FN@@", cf + "\n\n" + hf)
                  .replace("@@SPEC@@", spec_path.replace("\\", "\\\\").replace('"', '\\"')))
    return {"Main.kt": src, "Image.kt": IMAGE_STUB}


def patched(color_fn: str, helpers: str = "") -> str:
    """把 `REPLACEMENTS` 作用后的**全部**被测源码拼起来（供 P15s 检查改写面）。"""
    out = color_fn
    for a, b in REPLACEMENTS:
        out = out.replace(a, b)
    for a, b in REPLACEMENTS:
        helpers = helpers.replace(a, b)
    return out + "\n" + helpers


def parse_out(stdout: str):
    m = re.search(r"###PLANES###\n(.*?)\n###RGB###\n(.*)", stdout, re.S)
    if not m:
        raise RuntimeError("harness 输出格式不符:\n" + stdout[:1500])
    # ⚠⚠ Kotlin 的 ByteArray 是**有符号**的，joinToString 直接吐 -124；
    #    必须 & 0xFF 归一。漏这一步的后果特别阴：`-124 & 0xFF == 132` —— 
    #    数值其实**完全正确**，却因为 `got=-124 != want=132` 判成 FAIL。
    #    ⇒ 差点把正确的代码判成错的（"现场坏、测试红" ⇒ 先怀疑判据）。
    planes = [[int(x) & 0xFF for x in r.split(",")]
              for r in m.group(1).strip().split("\n") if r.strip()]
    body = m.group(2).strip()
    if body == "NULL":
        return planes, []
    rgb = []
    for row in body.rstrip(";").split(";"):
        vals = [int(x) for x in row.replace(",", " ").split()]
        rgb.append([tuple(vals[i:i + 3]) for i in range(0, len(vals), 3)])
    return planes, rgb


def flat(mat) -> list:
    return [b & 0xFF for row in mat for b in row]


# ═══════════════════════════════════════════════════════ 统计量

def std(xs, neutral: float = 0.0) -> float:
    if not xs:
        return 0.0
    mu = sum(xs) / len(xs)
    return math.sqrt(sum((x - mu) ** 2 for x in xs) / len(xs))


def mean(xs) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def rgb_to_cbcr(rgb):
    """与截图分析同一口径：由**显示像素**反解 Cb/Cr（128 为中性）。"""
    cbs, crs = [], []
    for row in rgb:
        for (r, g, b) in row:
            R, G, B = r / 255.0, g / 255.0, b / 255.0
            Y = 0.299 * R + 0.587 * G + 0.114 * B
            cbs.append((B - Y) * 0.564 * 255.0)
            crs.append((R - Y) * 0.713 * 255.0)
    return cbs, crs


def jpeg_downsample_baseline(rgb, quality: int, scale: int):
    """把 harness 的 RGB 产物过一遍 **JPEG q + 最近邻降采样**，再测色度统计。

    ⚠ 这一步在 **Python 侧**做（PIL），而 live 的真实链是 Android `Bitmap.compress`
    ⇒ 两者 JPEG 实现不同，**只能当量级参照，不能当等式**。

    ⚠⚠ **缺 PIL ⇒ 抛异常，不返回 None 静默降级。**
    本轮真踩到了：用裸 `python`（WorkBuddy 托管解释器，**没有 PIL**）跑，
    判据打印 4 行「测量链基线判不了」后**照样 RC=0 全绿** ——
    `P15k`/`P15k'` 两条**根本没跑**，而它们正是"现场 6.94 算不算低"的唯一依据
    （上一轮靠它撤回了我的因果推断）。
    ⇒ "跑绿了"与"跑全了"是两件事；**INCOMPLETE 必须变成 FAIL**，
    否则**换台机器/换个解释器就会静默少判两条**，而报告上看不出区别。
    这是记忆第 1 条铁律（"判不了 ≠ 没问题"）在**判据自身**上的应用。
    """
    try:
        from PIL import Image
    except Exception as e:
        raise RuntimeError(
            "PIL 不可用 ⇒ **P15k / P15k' 无法执行**，"
            "而它们是'现场色度标准差 6.94 算不算低'的**唯一**依据。"
            f"（原始错误：{e}）"
            "请用项目解释器跑：`C:\\Users\\liuru\\.conda\\envs\\general_env\\python.exe`"
            " —— 按纪律记 FAIL，不许静默跳过。")
    h, w = len(rgb), len(rgb[0])
    im = Image.new("RGB", (w, h))
    im.putdata([p for row in rgb for p in row])
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality)
    buf.seek(0)
    dec = Image.open(buf).convert("RGB").resize((w // scale, h // scale), Image.NEAREST)
    px = list(dec.getdata())
    small = [[px[r * (w // scale) + c] for c in range(w // scale)] for r in range(h // scale)]
    cb, cr = rgb_to_cbcr(small)
    return std(cb), std(cr), mean(cb), mean(cr)


# ═══════════════════════════════════════════════════════════ 主流程

def main() -> int:
    print("=" * 74)
    print("P15  设备端色度链路（离线：真编译真执行 DeliverySession.kt 的真实函数体）")
    print("=" * 74)

    if not check(f"源码存在 {KOTLIN_SRC.name}", KOTLIN_SRC.exists(), "文件不见了"):
        return 1
    src = KOTLIN_SRC.read_text(encoding="utf-8")

    tc = kotlin_toolchain()
    if not check("P15a Kotlin 工具链可用（缺编译器必须判 FAIL，不静默跳过）",
                 tc is not None,
                 "Gradle 缓存里找不到 kotlin-compiler-embeddable / stdlib / "
                 "script-runtime / trove4j / daemon-embeddable，或 java 不在 PATH —— "
                 "**不许**因为'跑不了'就放过这一条（那正是上一轮的盲区）"):
        return 1

    # ─────────────── 真的把函数体切出来
    plane_fn = extract_function(src, "planeToBytes")
    color_fn = extract_function(src, "encodeColorJpeg")
    ok = check("P15b 从 DeliverySession.kt **真的切出** `planeToBytes` 函数体"
               "（整文件搜关键词会命中调用点 ⇒ 判据恒真）",
               bool(plane_fn) and "Image.Plane" in (plane_fn or "")
               and len(plane_fn or "") > 200,
               f"长度={len(plane_fn) if plane_fn else 0}")
    ok &= check("P15b' 同样切出 `encodeColorJpeg`（色度数学本体）",
                bool(color_fn) and "planeToBytes(img.planes[1]" in (color_fn or ""),
                f"长度={len(color_fn) if color_fn else 0}")

    # ⚠ v1.15.0：`encodeColorJpeg` 已把准线与 JPEG 编码**抽成共用私有函数**，
    #   并按 `img.format` 分派到 `encodeRgbaJpeg`。三者缺一个都编译不过 ——
    #   而"unresolved reference: drawCrosshair"离真因（判据没跟上重构）很远。
    #   `encodeRgbaJpeg` 在本判据里**永远不被调用**（`Image.format` 默认 35），
    #   切它只为让分派表达式编译得过，避免**静默跑错被测分支**。
    helper_fns = {}
    for fname in ("drawCrosshair", "jpegEncode", "encodeRgbaJpeg"):
        helper_fns[fname] = extract_function(src, fname)
    ok &= check("P15b'' 一并切出 `drawCrosshair` / `jpegEncode` / `encodeRgbaJpeg`"
                "（v1.15.0 起 `encodeColorJpeg` 依赖它们；缺一个 ⇒ 编译失败，"
                "而报错离真因很远）",
                all(helper_fns.values()),
                f"缺少: {[k for k, v in helper_fns.items() if not v]}")
    if not ok:
        return 1
    helpers_src = "\n\n".join(v for v in helper_fns.values() if v)

    # ─────────────── 源码锚定（**先剥注释** —— 见 strip_kotlin_comments 的告警）
    plane_code = strip_kotlin_comments(plane_fn)
    check("P15c 源码锚定：`planeToBytes` 的**代码**里**不再有**相对 position 的批量读 "
          "`buf.get(out, 0, n)`（旧快路径的真因之一）",
          not re.search(r"\.get\(\s*out\s*,\s*0\s*,", plane_code),
          "仍找到相对 position 的批量读 —— 它不设定 position 就读，"
          "正确性依赖本函数既不建立也不检查的外部不变量")
    check("P15d 源码锚定：`planeToBytes` 的**代码**里**不再写** `buf.position(...)` 去**改**位置",
          not re.search(r"buf\.position\s*\(\s*[^)\s]", plane_code),
          "仍找到 `buf.position(x)` 赋值 —— 改 position 再相对读，顺序依赖调用次序。"
          "⚠ 注意与 `val base = buf.position()`（**零参读**）区分：后者是取基址，是必需的")
    # ★ 本轮最关键的一条锚定：NV12 的 V 平面靠 `buffer.position()==1` 标记字节偏移。
    #   绝对索引必须**从 position 起算**；硬写基址 0 ⇒ 把 U 当 V。
    m_base = re.search(r"val\s+(\w+)\s*=\s*buf\.position\s*\(\s*\)", plane_code)
    base_name = m_base.group(1) if m_base else None
    uses_base = bool(base_name) and bool(
        re.search(r"\b" + re.escape(base_name) + r"\s*\+", plane_code))
    check("P15d' 源码锚定：`planeToBytes` 的绝对索引**基址取自 `buf.position()`**"
          "（NV12 的 V 平面 position=1，硬编码基址 0 会把 U 读成 V）",
          uses_base,
          f"找到基址变量={base_name}；它是否真的参与索引计算={uses_base} —— "
          "V 平面的字节偏移会被忽略（U/V 读成同一个平面）")
    check("P15c' 反向自检：判据确实**能**在代码里认出旧的两种写法"
          "（否则 P15c/d 只是'搜了个永远搜不到的东西'）",
          bool(re.search(r"\.get\(\s*out\s*,\s*0\s*,", strip_kotlin_comments(OLD_PLANE_FN)))
          and bool(re.search(r"buf\.position\s*\(\s*[^)\s]", strip_kotlin_comments(OLD_PLANE_FN)))
          and not re.search(r"val\s+\w+\s*=\s*buf\.position\s*\(\s*\)",
                            strip_kotlin_comments(OLD_PLANE_FN)),
          "把旧实现喂进去竟然判不出来 ⇒ P15c/d 恒真")

    # ─────────────── harness 自身的改写面被钉死
    # ⚠ 检查范围必须含 `helpers`：`jpegEncode` 才是真正调 `Bitmap.createBitmap` /
    #   `compress` 的地方，`encodeColorJpeg` 里已经没有了。
    #   只查 color_fn 会得到 "FakeStore.create 不在里面" ⇒ **假红**，
    #   于是人会去"修"正确的代码（现场坏、测试红 ⇒ 先怀疑判据）。
    all_patched = patched(color_fn, helpers_src)
    lost = [f for f in COLOR_FINGERPRINT if f not in all_patched]
    check("P15s harness 只替换 `Bitmap/JPEG`（被测对象之外），"
          "**色度数学那几行逐字节未被改动**",
          (not lost) and "FakeStore.create(px, tw, th)" in all_patched
          and "android.graphics" not in all_patched,
          f"丢失/被改写的指纹行: {lost}")

    def run(plane_src, color_src, fx, tag, helpers=None):
        sp = write_spec(fx)
        try:
            files = build_harness(plane_src, color_src, sp,
                                  helpers_src if helpers is None else helpers)
            return parse_out(run_kotlin(tc, files, tag))
        finally:
            os.unlink(sp)

    W, H = 64, 48

    # ═════════════ 基准自检（先过，否则后面所有数字作废）
    fx_n = yuv420_fixture(W, H, 0, 1, pattern="neutral")
    _, rgb_n = run(plane_fn, color_fn, fx_n, "neutral")
    allpx = [p for row in rgb_n for p in row]
    uniq = set(allpx)
    neutral_px = [p for p in allpx if p[0] == p[1] == p[2]]
    # ⚠ 断言不能写 `len(set(allpx))==1`：`encodeColorJpeg` 会画一个**已知色十字准线**
    #   `0xFFFF3B30`，它本来就该是非中性 ⇒ 我第一版这么写，判据当场把**正确的代码**
    #   判成 FAIL。教训同族：**别把"输出里应当只有一种东西"当判据**，
    #   要判"该为中性的部分确实中性"（比例），并把准线单独立一条。
    if not check("P15z **基准自检**：中性灰 `U=V=128` ⇒ **非准线像素**全部 R=G=B"
                 "（不过则本条所有数字作废——上一轮就是检验台自身出 bug）",
                 bool(rgb_n) and len(neutral_px) >= 0.9 * len(allpx) and len(allpx) > 0,
                 f"中性像素 {len(neutral_px)}/{len(allpx)}；出现的颜色={sorted(uniq)[:4]}"):
        return 1
    bv = neutral_px[0][0]
    # 理论值必须**照抄代码的定点算式**（`(298*yy + 128) shr 8`，即除 256），
    # 不能写成 `(Y-16)*298/1024` —— 那是浮点推导，换算因子不同 ⇒ 32 vs 130。
    # 教训：**判据里的"期望值"要从被测代码的算式推，不能从教科书推。**
    theo = ((128 - 16) * 298 + 128) >> 8
    note(f"中性灰基准像素 = ({bv},{bv},{bv})；照抄代码定点算式 "
         f"((128-16)*298+128)>>8 = {theo}")
    check("P15z' 基准值与**代码算式**推出的理论值一致"
          "（证明 harness 里的色彩数学接对了线，不是碰巧 R=G=B）",
          abs(bv - theo) <= 1, f"got={bv} 理论={theo}（注意是 >>8 即 /256，不是 /1024）")
    check("P15z'' **已知色十字准线** `0xFFFF3B30` 在**精确像素**上存活"
          "（上一轮它只在被 JPEG+缩放的截图里测过，糊掉 unusable ⇒ 当时判为不可用；"
          "在 harness 里它是精确的，正式升为判据：能把'色度读错'与'下游编码错'分开）",
          (255, 59, 48) in uniq, f"出现的颜色={sorted(uniq)[:4]}")

    # ═════════════ P15e 行为：六种 stride 布局下色度必须逐字节取回
    #     ⚠ 其中 `shared_uv=True` 是**真机 NV12 的真实布局**（U/V 共用 buffer，
    #     靠 position=1 标记 V）—— 本轮判据自己踩出：没有它，"纯绝对索引"
    #     会被判为全对，而它在真机上会把 U 当 V 读。
    layouts = [(0, 1, False, "紧凑平面式 ps=1"),
               (16, 1, False, "带行补齐 ps=1"),
               (0, 2, False, "交错·独立buffer ps=2"),
               (16, 2, False, "交错+行补齐·独立buffer ps=2"),
               (0, 2, True, "**NV12 共享buffer ps=2（真机布局）**"),
               (16, 2, True, "**NV12 共享buffer+行补齐 ps=2（真机布局）**")]
    for pad, ps, shared, label in layouts:
        fx = yuv420_fixture(W, H, pad, ps, shared_uv=shared)
        got, _ = run(plane_fn, color_fn, fx, f"e{pad}_{ps}_{int(shared)}")
        if len(got) != 3:
            check(f"P15e/{label} 三个平面都能取回", False, f"只拿到 {len(got)} 个")
            continue
        u_want, v_want = flat(fx["U"]), flat(fx["V"])
        ub = next((i for i, (a, b) in enumerate(zip(got[1], u_want)) if a != b), None)
        vb = next((i for i, (a, b) in enumerate(zip(got[2], v_want)) if a != b), None)
        check(f"P15e/{label} U/V 平面**逐字节**等于真值"
              f"（rowStride={fx['planes'][1]['rowStride']}, pixelStride={ps}, "
              f"V.position={fx['init_pos'][2]}）",
              ub is None and vb is None and len(got[1]) == len(u_want),
              f"U 首个错位 idx={ub} (got={got[1][ub] if ub is not None else '-'} "
              f"want={u_want[ub] if ub is not None else '-'}) | "
              f"V 首个错位 idx={vb} (got={got[2][vb] if vb is not None else '-'} "
              f"want={v_want[vb] if vb is not None else '-'})")

    # ═════════════ P15f 动态范围（现场指纹）—— 用**真机 NV12 布局**
    fx = yuv420_fixture(W, H, 0, 2, shared_uv=True)
    got, _ = run(plane_fn, color_fn, fx, "std")
    su_got, sv_got = std([b - 128 for b in got[1]]), std([b - 128 for b in got[2]])
    su_w, sv_w = std([b - 128 for b in flat(fx["U"])]), std([b - 128 for b in flat(fx["V"])])
    check("P15f **色度动态范围被保住**：取回 U/V 的标准差 == 真值标准差",
          abs(su_got - su_w) < 0.5 and abs(sv_got - sv_w) < 0.5,
          f"U {su_got:.2f} vs {su_w:.2f} | V {sv_got:.2f} vs {sv_w:.2f}")

    # ═════════════ P15g 反例①：旧实现（相对 position + 基址写死）必须判否
    #     夹具用**真机 NV12 布局**（U/V 共享 buffer，V.position=1）⇒ 旧实现会把 U 当 V。
    #     这正是现场"亮度完全正常、只有颜色脏"的形态：Y 是独立平面 position=0，
    #     读对了；色度两个平面读成同一份字节 ⇒ **必然偏色，且不抛任何异常**。
    fx1 = yuv420_fixture(W, H, 0, 2, pattern="scene", shared_uv=True)
    got_old, rgb_old = run(OLD_PLANE_FN, color_fn, fx1, "old")
    luma_ok = got_old[0] == flat(fx1["Y"])
    u_wrong_ok = got_old[1] == flat(fx1["U"])
    v_wrong_ok = got_old[2] == flat(fx1["V"])
    check("P15g **反例①**：换回旧实现后，同一套判据必须判否"
          "（NV12 布局：Y 读对、U/V 读成同一份 ⇒ 精确复现'亮度正常、颜色脏'）",
          (not v_wrong_ok) and luma_ok,
          f"亮度是否仍正确={luma_ok}（应 True：反例要只脏色度）；"
          f"V 平面是否被误判为正确={v_wrong_ok}（应 False）")
    if rgb_old:
        cb, cr = rgb_to_cbcr(rgb_old)
        note(f"反例①的现场指纹：Cb偏={mean(cb):+.1f} Cr偏={mean(cr):+.1f} "
             f"Cbstd={std(cb):.2f} Crstd={std(cr):.2f} "
             f"（现场实测 −12.9/−8.3、6.94/5.72）")

    # ═════════════ P15g2 反例②：**只做绝对索引但基址写死 0** —— 本轮的真实教训
    #     我第一版修复就是"纯绝对索引、隐含基址 0"，在 NV12 上仍然把 U 当 V。
    #     ⇒ 必须有一条判据独立于源码锚定去钉住它。
    zero_base_fn = plane_fn.replace("val base = buf.position()", "val base = 0")
    if check("P15g2 反例准备：能在 `planeToBytes` 里定位到基址变量以便注入反例",
             "val base = buf.position()" in plane_fn,
             "源码形状变了，反例注入点失效 —— 判据本身要跟着更新，不能静默跳过"):
        got_zb, _ = run(zero_base_fn, color_fn, fx1, "zerobase")
        check("P15g2 **反例②**：把基址改成写死的 0（第一版修复的写法）后必须判否"
              "（NV12 的 V 平面 position=1，写死 0 ⇒ U 被当成 V）",
              got_zb[2] != flat(fx1["V"]),
              f"V 平面竟然判为正确（读出 {len(got_zb[2])} 字节）")

    # ═════════════ P15h 反例②：U/V 互换必须判否
    swapped = (color_fn
               .replace("planeToBytes(img.planes[1], cw, ch)",
                        "planeToBytes(img.planes[9], cw, ch)")
               .replace("planeToBytes(img.planes[2], cw, ch)",
                        "planeToBytes(img.planes[1], cw, ch)")
               .replace("planeToBytes(img.planes[9], cw, ch)",
                        "planeToBytes(img.planes[2], cw, ch)"))
    fx2 = yuv420_fixture(W, H, 0, 2, pattern="scene", shared_uv=True)
    _, rgb_ok = run(plane_fn, color_fn, fx2, "scene_ok")
    _, rgb_sw = run(plane_fn, swapped, fx2, "scene_sw")
    check("P15h **反例②**：把 U/V 平面互换后，同一套判据必须判否",
          bool(rgb_ok) and bool(rgb_sw) and rgb_ok != rgb_sw,
          f"反例输出与正例相同（{len(rgb_sw)} 行）⇒ 判据对 U/V 顺序不敏感")

    # ═════════════ P15i 反例③：矩阵换成 BT.709，在**高饱和**夹具上必须判否
    bt709 = color_fn
    for a, b in [("409 * V", "459 * V"), ("- 100 * U - 208 * V", "- 55 * U - 136 * V"),
                 ("516 * U", "454 * U")]:
        bt709 = bt709.replace(a, b)
    _, rgb_709 = run(plane_fn, bt709, fx2, "scene_709")
    check("P15i **反例③**：把 BT.601 系数换成 BT.709 后，在**高饱和**夹具上必须判否"
          "（矩阵系数被钉住；但'这台相机吐 601 还是 709'是**设备事实**，"
          "离线判不了，须上真机比对色卡）",
          bool(rgb_ok) and bool(rgb_709) and rgb_ok != rgb_709,
          "反例与正例输出相同 ⇒ 判据对色彩矩阵不敏感")

    # ═════════════ P15k 测量链基线：给现场 6.94 一个可比较的参照
    #  ⚠ 夹具必须**低饱和、接近真实场景**。第一版用 `scene`（纯色块），
    #    实测 Cbstd 高达 80，JPEG q70 的色度 DCT 对这种**大片纯色**几乎无损
    #    （压缩比 0.99）⇒ 拿它当基线毫无参考价值，会给出"链路几乎不压色度"的
    #    错误结论。⇒ 真实场景是**渐变+噪声+低饱和**，必须那样造。
    #  ⚠ 单个夹具也不足以定论 ⇒ 扫**四档饱和度**，报"正确输出过链路后的范围"。
    BW, BH = 160, 120
    rows, lo, hi = [], None, None
    for amp in (6, 10, 14, 20):
        fx3 = yuv420_fixture(BW, BH, 0, 2, pattern="natural", shared_uv=True, chroma_amp=amp)
        _, rgb_full = run(plane_fn, color_fn, fx3, f"chain{amp}")
        # ⚠ 四档**全部**必须成功。旧写法是 `if not X: continue` ⇒ 全部失败时
        #   `rows` 为空 ⇒ 整个 P15k 段被跳过 ⇒ 而 `return 1 if _fail else 0`
        #   照样给出 RC=0。**"全部用例失败"与"全部用例通过"在退出码上无法区分。**
        #   ⇒ 这里必须显式判 FAIL（P15k0），不能靠段落的 if。
        if not check(f"P15k0/{amp} 低饱和夹具（色度幅度±{amp}）跑出非空产物",
                     bool(rgb_full), "harness 输出为空 ⇒ 基线样本缺失"):
            return 1
        cb0, cr0 = rgb_to_cbcr(rgb_full)
        raw = (std(cb0), std(cr0), mean(cb0), mean(cr0))
        aft = jpeg_downsample_baseline(rgb_full, quality=70, scale=4)
        rows.append((amp, raw, aft))
        lo = aft[0] if lo is None else min(lo, aft[0])
        hi = aft[0] if hi is None else max(hi, aft[0])

    if check("P15k0' 四档基线样本**一个不缺**（否则下面的正常带是残缺的，"
             "而'6.94 在不在带内'会被**残缺带**判成'不在'）",
             len(rows) == 4, f"只有 {len(rows)}/4 档"):
        note("测量链基线（判据认为**正确**的输出，过 q70 JPEG + 4×降采样）：")
        for amp, raw, aft in rows:
            note(f"    夹具色度幅度±{amp:<3d} 原始 Cbstd={raw[0]:6.2f} "
                 f"→ 过链路后 {aft[0]:6.2f}（压缩比 {aft[0] / raw[0]:.2f}）")
        note(f"    ⇒ **正确输出**过完链路后的 Cbstd 落在 [{lo:.2f}, {hi:.2f}]")
        # ⚠ 这里原来写的是 `check("P15k ...", True)` —— **恒真**，等于没判。
        #   "基线已量化"必须是**可证伪**的性质：带宽、单调性、量级都对才算。
        #   （记忆铁律 2/32：判据里每个字面清单都问"是不变量还是现状"；
        #     `check(..., True)` 是把判据写成装饰的典型形态。）
        check("P15k 测量链基线**已量化且非退化**（四档带出确定的宽度与单调下降，"
              "否则'6.94 算不算低'没有答案，现场因果推断就是建在流沙上）",
              hi > lo and (hi - lo) > 1.0
              and all(rows[i][2][0] <= rows[i + 1][2][0] for i in range(len(rows) - 1)),
              f"正常带 [{lo:.2f}, {hi:.2f}]，宽度={hi - lo:.2f}；"
              f"逐档={[round(r[2][0], 2) for r in rows]}（应随夹具色度幅度**单调不增**，"
              "因为 JPEG q70 会压低色度标准差）")
        in_band = lo <= 6.94 <= hi
        check("P15k' **★ 自查结论（推翻上一轮的因果推断）**："
              "现场实测的 `Cbstd=6.94` **落在正确输出的正常带内** ⇒ "
              "「色度动态范围塌陷 ⇒ 色度字节取错位置」这条推理**不成立**，撤回",
              in_band,
              f"正常带 [{lo:.2f}, {hi:.2f}]，现场 6.94 "
              f"{'在带内 ⇒ 上一轮推断不成立' if in_band else '仍在带外'}")

    print("=" * 74)
    print(f"  P15 通过 {_ok} 条，失败 {len(_fail)} 条")
    for f_ in _fail:
        print(f"    - {f_}")
    print("=" * 74)
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
