"""正则库: 本工具链**唯一**的语法知识来源, 全部为 ASCII 模式 (可直接作用于 bytes)。

设计原则
--------
1. **不做 C 解析器**。只认 4 类"行形态": 预处理条件行、带初值的声明行、赋值语句行、宏定义行。
   认不出的形态 → 记进 `gaps[]` 报错, **绝不猜、绝不改**。
2. **锚点必须唯一**。任何写回锚点都要求"全文件匹配次数 == 1", 否则报错退出。
3. **值域替换而非整行替换**。所有写回都靠"前置组 + 值组 + 剩余组"三段式,
   只替换"值组" ⇒ 制表符缩进 / 行尾注释 / 门控结构天然保留。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- 词法片段
CAST = rb"\(\s*(?:u8|u16|u32|s8|s16|s32|int|long|float|double|char|unsigned)\s*\*?\s*\)"
CTYPE = rb"u8|u16|u32|s8|s16|s32|int|long|float|double"
IDENT = rb"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?"
NUMERIC_TYPE = ("u8", "u16", "u32", "s8", "s16", "s32")

# ---------------------------------------------------------------- R1 预处理条件
RE_COND = re.compile(rb"^[ \t]*#[ \t]*(if|ifdef|ifndef|else|elif|endif)\b(.*)$")

# ---------------------------------------------------------------- R2 宏定义 (link.h)
RE_PM_DEFINE = re.compile(
    rb"^[ \t]*#[ \t]*define[ \t]+LINK_P_([A-Z0-9_]+)[ \t]+(\d+)[ \t]*(?:/\*(.*?)\*/)?[ \t\r]*$")
RE_PM_NUM = re.compile(rb"^[ \t]*#[ \t]*define[ \t]+LINK_P_NUM[ \t]+(\d+)")

# ---------------------------------------------------------------- R3 带初值的声明
RE_DECL_LINE = re.compile(
    rb"^[ \t]*(const[ \t]+)?(" + CTYPE + rb")[ \t]+([^;]+);")
RE_DECLARATOR = re.compile(rb"(?:^|,)[ \t]*([A-Za-z_]\w*)[ \t]*=[ \t]*([^,;]+)")

# ---------------------------------------------------------------- R4 函数签名 / 赋值
RE_FUNC_SIG = re.compile(rb"^[ \t]*(?:static[ \t]+)?[A-Za-z_][\w \t\*]*\b(%s)[ \t]*\([^;]*\)[ \t\r]*$")
RE_ASSIGN_SPLIT = re.compile(rb"(?<![=!<>+\-*/])=(?!=)")

# ---------------------------------------------------------------- R7 link.c 搬运表
RE_SLOT_ID = re.compile(rb"^[ \t]*linkParam\[(\d+)\]\.id[ \t]*=[ \t]*(LINK_P_[A-Z0-9_]+)[ \t]*;")
RE_SLOT_Q = re.compile(rb"^[ \t]*linkParam\[(\d+)\]\.q[ \t]*=[ \t]*(.+?);")
RE_CASE_PM = re.compile(rb"^[ \t]*case[ \t]+(LINK_P_[A-Z0-9_]+)[ \t]*:")

# ---------------------------------------------------------------- 受限求值
RE_SAFE_EXPR = re.compile(r"^[0-9+\-*/(). \t]+$")
RE_IDENT_ONLY = re.compile(IDENT)


def func_sig(name: str) -> "re.Pattern":
    return re.compile(RE_FUNC_SIG.pattern % name.encode())


def line_anchor(symbol: str, ctype: str | None = None) -> "re.Pattern":
    """写回锚点: 三段式 —— (前置) (值) (剩余)。

    - `ctype` 给定时按"声明行首"匹配(最严格, 用于文件级定义);
    - 不给定时按"`符号 =`"匹配(用于同一行的第 2..n 个声明符, 以及镜像初值)。
    """
    sym = re.escape(symbol.encode())
    if ctype:
        return re.compile(
            rb"^([ \t]*(?:const[ \t]+)?" + ctype.encode() + rb"[ \t]+" + sym +
            rb"[ \t]*=[ \t]*)([^,;\r\n]+)(.*)$")
    return re.compile(rb"(?<![A-Za-z0-9_.])(" + sym + rb"[ \t]*=[ \t]*)([^,;\r\n]+)")


def value_span(symbol: str) -> "re.Pattern":
    """只匹配值域(用于行内已有锚点行的二次定位)。"""
    sym = re.escape(symbol.encode())
    return re.compile(rb"(?<![A-Za-z0-9_.])" + sym + rb"[ \t]*=[ \t]*([^,;\r\n]+)")
