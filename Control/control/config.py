"""配置载入与校验。

沿用上游风格：**未知键直接报错**，不静默忽略（静默忽略的配置项等于没有配置项，
现场会变成"我明明改了但它没生效"）。

相对路径一律相对于 config.yaml 所在目录（= Control 根目录）解析。
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import yaml

#: 允许出现的顶层键 —— 多一个就报错
TOP_KEYS = {
    "server", "mouse", "camera", "locator", "arena", "clock", "paths",
    # 实验 8 的迷宫记录目录（`records.maze_root`）。加这一段而不是塞进 `server`：
    # 它是**产物目录**，与"服务怎么跑"不是一类东西（将来会话/导出目录也会归这里）。
    "records",
}

#: 顶层键 -> 允许的子键。空集合表示"该段是自由格式"。
SCHEMA: Dict[str, Optional[set]] = {
    "server": {"host", "port", "session_root", "log_ring"},
    "mouse": {"target", "baud", "auto_stream", "scheduler_hz", "telemetry_window_s",
              "wave_window_ms", "auto_reconnect"},
    "camera": {"base", "token", "timeout", "default_plan", "offline_import_root",
               "probe_interval_s", "probe_interval_bad_s", "launcher"},
    "locator": {"config", "profile", "online", "offline"},
    "arena": {"grid", "pitch_mm", "field_mm", "fw_to_arena", "calibrated",
              "nadir_xy", "camera_height_mm"},
    "clock": {"offset_ms", "uncertainty_ms", "source"},
    "paths": {"remotecontrol_host", "autolocate", "phonecamera_pc"},
    "records": {"maze_root"},
}

#: 需要在装载后解析成绝对路径的键（点号路径）
_PATH_KEYS = {
    "paths.remotecontrol_host",
    "paths.autolocate",
    "paths.phonecamera_pc",
    "camera.offline_import_root",
    "locator.config",
    # 实验 8 的记录目录。**必须在这里**：不进来的话 `Path("records/maze")` 会相对
    # **当前工作目录**解析 —— 从别处起服务（`python Control/app.py`）就把记录写到
    # 别的地方去了，而现场表现是"记录列表是空的"（文件其实在，只是找不到）。
    "records.maze_root",
}


class ConfigError(ValueError):
    """配置不合法。消息必须能照着改。"""


def _find_scalar_line(lines: list, path: list) -> Optional[int]:
    """在 yaml 文本里按点号路径找**那一行**的行号（逐层缩进收窄搜索范围）。

    只做两件事：跳过空行与整行注释、按缩进判断层级。找不到返回 None
    （调用方据此决定是报错还是落回整份重写）。
    """
    start, end, indent = 0, len(lines), -1
    for depth, key in enumerate(path):
        target = indent + 2 if depth else 0
        found = None
        for i in range(start, end):
            line = lines[i]
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            ind = len(line) - len(line.lstrip(" "))
            if depth > 0 and ind <= indent:
                break
            if ind == target and line.lstrip().startswith(key + ":"):
                found = i
                break
        if found is None:
            return None
        if depth == len(path) - 1:
            return found
        indent = target
        start = found + 1
        end = len(lines)
    return None


class NS:
    """支持属性访问 + 点号路径读写的命名空间包装。

    ⚠️ 不要给这个类加 `__slots__`：它靠 `object.__setattr__` 把 yaml 的每个键
    变成实例属性，有 slots 时这些赋值会直接抛 AttributeError。
    """

    def __init__(self, d: Dict[str, Any]) -> None:
        object.__setattr__(self, "_d", d)
        for k, v in d.items():
            if isinstance(v, dict):
                object.__setattr__(self, k, NS(v))
            else:
                object.__setattr__(self, k, v)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"NS({self._d!r})"

    def __contains__(self, k: str) -> bool:
        return k in self._d

    def get(self, path: str, default: Any = None) -> Any:
        cur: Any = self._d
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur


def _validate(d: Dict[str, Any], where: str = "") -> None:
    for k, v in d.items():
        full = f"{where}{k}"
        if not where:
            if k not in TOP_KEYS:
                raise ConfigError(
                    f"配置里有未知的顶层段 `{k}`。允许的是：{sorted(TOP_KEYS)}。"
                    f"（改配置前先看 docs/上位机整合设计方案.md §7.1）")
            allowed = SCHEMA.get(k)
            if allowed is None:
                continue
            if not isinstance(v, dict):
                raise ConfigError(f"配置段 `{k}` 必须是映射，实际是 {type(v).__name__}")
            unknown = set(v) - allowed
            if unknown:
                raise ConfigError(
                    f"配置段 `{k}` 里有未知键 {sorted(unknown)}。允许的是：{sorted(allowed)}。")
        if isinstance(v, dict):
            _validate(v, full + ".")


class Config:
    """载入后的配置。`cfg.arena.grid` / `cfg.get("arena.grid")` 都可用。"""

    def __init__(self, raw: Dict[str, Any], path: Path) -> None:
        _validate(raw)
        self.path = Path(path).resolve()
        self.root = self.path.parent           # Control/
        self._raw = copy.deepcopy(raw)
        self._raw_path = path                  # 写回时的目标（保持原文件名）
        self.ns = NS(raw)

    # ---- 属性直通（便于 cfg.server.port 这种写法）
    def __getattr__(self, name: str) -> Any:
        try:
            return getattr(object.__getattribute__(self, "ns"), name)
        except AttributeError:
            raise AttributeError(name) from None

    def get(self, path: str, default: Any = None) -> Any:
        return self.ns.get(path, default)

    def raw(self) -> Dict[str, Any]:
        return copy.deepcopy(self._raw)

    # ---- 路径
    def abspath(self, p: str) -> Path:
        """相对路径一律相对 config.yaml 所在目录解析。"""
        q = Path(p)
        return q if q.is_absolute() else (self.root / q).resolve()

    def resolve_paths(self) -> None:
        for dotted in sorted(_PATH_KEYS):
            cur = self._raw
            parts = dotted.split(".")
            for part in parts[:-1]:
                cur = cur.get(part) if isinstance(cur, dict) else None
                if cur is None:
                    break
            if isinstance(cur, dict) and isinstance(cur.get(parts[-1]), str) \
                    and cur[parts[-1]]:
                cur[parts[-1]] = str(self.abspath(cur[parts[-1]]))
        self.ns = NS(self._raw)

    # ---- 写回（场地标定 / 时钟标定用）
    def update(self, path: str, value: Any, *, save: bool = True) -> None:
        """按点号路径写一个值，可选立刻落盘。"""
        parts = path.split(".")
        cur = self._raw
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
            if not isinstance(cur, dict):
                raise ConfigError(f"写配置 {path!r} 时路径被非映射值挡住：{part}")
        cur[parts[-1]] = value
        self.ns = NS(self._raw)
        if save:
            self.save()

    def save(self) -> None:
        """整份重写（**会丢注释**）。只在 `patch()` 找不到目标行时才该用它。"""
        text = yaml.safe_dump(self._raw, allow_unicode=True, sort_keys=False, width=100)
        tmp = self._raw_path.with_suffix(".yaml.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self._raw_path)

    # ---- 外科式改写：只动目标行，保住注释
    def patch(self, values: Dict[str, str], *, update_memory: bool = True) -> Dict[str, object]:
        """按点号路径逐行改 yaml 文本里的**标量值**，保留全部注释与排版。

        为什么不用 `safe_dump` 整份重写：config.yaml 里写满了"为什么是这个值"的注释，
        那些注释是现场排查时最值钱的东西。整份重写会把它们全抹掉 —— 那就等于
        用"改一个数"的代价换走了整份文档。

        `values` 的值是**已经渲染好的 YAML 片段**（如 `"true"`、`"[0.0, 0.0]"`、`"1650.0"`）。
        返回 `{dotted: "patched" | "not_found"}`；调用方对 `not_found` 要自己决定
        （通常落回 `save()` 并告警）。
        """
        lines = self._raw_path.read_text(encoding="utf-8").splitlines(keepends=True)
        result: Dict[str, object] = {}
        for dotted, rendered in values.items():
            idx = _find_scalar_line(lines, dotted.split("."))
            if idx is None:
                result[dotted] = "not_found"
                continue
            raw = lines[idx]
            stripped = raw.rstrip("\n")
            body, sep, comment = stripped.partition("#")
            if not sep:
                comment, sep = "", ""
            # ⚠️ `head` **必须 strip**：`body` 是整行（含前面的缩进），
            # 而下面又要再拼一次 `indent` ⇒ 不 strip 就会把缩进写两遍。
            # 这个 bug 只在**嵌套键**上现形（缩进 0 的顶层键看不出来）：
            # 曾经把 `camera.launcher.autostart` 从 4 空格写成 8 空格，
            # 于是 config.yaml **下一次就解析不了**（ParserError）—— 服务直接起不来。
            # 判据：`tools/drill.py` 的 I24（在临时副本上改嵌套键，必须重新 load 得动、
            # 缩进不变、注释还在）。
            head = body.split(":", 1)[0].strip()
            indent = body[:len(body) - len(body.lstrip(" "))]
            newline = "\n" if raw.endswith("\n") else ""
            middle = f"{indent}{head}: {rendered}"
            if sep:
                middle += "   " + sep + comment
            lines[idx] = middle + newline
            result[dotted] = "patched"
        if any(v == "patched" for v in result.values()):
            # ⚠️ **落盘前先自己解析一遍**。这不是洁癖：config.yaml 是"唯一真相"，
            # 写坏它等于下次启动直接 ParserError（服务起不来），而现场手上没有编辑器时
            # 只能靠改回来的路径。宁可这一项不生效并告警，也不留下一个读不动的配置。
            new_text = "".join(lines)
            try:
                yaml.safe_load(new_text)
            except yaml.YAMLError as e:
                bad = [k for k, v in result.items() if v == "patched"]
                for k in bad:
                    result[k] = "bad_render"
                return result
            tmp = self._raw_path.with_suffix(".yaml.tmp")
            tmp.write_text(new_text, encoding="utf-8")
            os.replace(tmp, self._raw_path)
        if update_memory:
            for dotted, rendered in values.items():
                if result.get(dotted) != "patched":
                    continue
                try:
                    parsed = yaml.safe_load(rendered)
                except yaml.YAMLError:
                    parsed = rendered
                parts = dotted.split(".")
                cur = self._raw
                for p in parts[:-1]:
                    cur = cur.setdefault(p, {})
                    if not isinstance(cur, dict):
                        break
                else:
                    cur[parts[-1]] = parsed
            self.ns = NS(self._raw)
        return result

    # ---- 常用的几个派生量
    @property
    def session_root(self) -> Path:
        p = Path(self.get("server.session_root", "sessions"))
        return p if p.is_absolute() else (self.root / p).resolve()


def load(path: Optional[str | os.PathLike] = None) -> Config:
    here = Path(__file__).resolve().parent.parent      # Control/
    p = Path(path) if path else (here / "config.yaml")
    if not p.is_file():
        raise ConfigError(f"找不到配置文件：{p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"config.yaml 不是合法 YAML：{e}") from e
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml 的顶层必须是映射")
    cfg = Config(raw, p)
    cfg.resolve_paths()
    return cfg


#: 进程级单例（app.py 启动时装载；工具脚本可自己 load）
CFG: Optional[Config] = None


def bootstrap(path: Optional[str] = None) -> Config:
    global CFG
    CFG = load(path)
    return CFG


def require() -> Config:
    if CFG is None:
        raise RuntimeError("配置尚未装载：应先调用 control.config.bootstrap()")
    return CFG
