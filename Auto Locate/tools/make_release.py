"""打包发行归档。

用法::

    python tools/make_release.py                    # 版本号取自 aruco_locator.__version__
    python tools/make_release.py --out release --zip-only

产物::

    release/aruco-locator-<版本>.zip      # 可直接解压使用的完整工程
    release/aruco-locator-<版本>.sha256   # 归档校验和
    release/MANIFEST-<版本>.txt           # 逐文件校验和与说明

归档内容 = 包 + 文档（含插图和证据） + 示例 + 工具 + 配置 + 标记图案 SVG。
**不含**实拍样本照片（`samples/` 共 116MB，体积过大；样本留在仓库内自行分发）。
排除 `__pycache__`、临时输出目录（`out*/`、`release/`）与 `tools/` 下的一次性诊断脚本（`_*.py`）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aruco_locator import __version__  # noqa: E402

# 顶层文件（存在才打包）
TOP_FILES = [
    "README.md",
    "CHANGELOG.md",
    "config.yaml",
    "calibrate_camera.py",
    "selftest_synthetic.py",
    "maze_extract.py",  # 迷宫结构解算（独立脚本，不并入包；用户 2026-10-04 定为长期交付）
    "4x4_1000-0.svg",
    "4x4_1000-1.svg",
    "4x4_1000-2.svg",
    "4x4_1000-3.svg",
    "4x4_1000-4.svg",
]
# 目录（递归）
TREES = ["aruco_locator", "docs", "examples", "tools"]
# 实拍样本目录：**默认不打进归档**（v1.1.0 起，`samples/` 共 116MB）。
# 需要随包分发素材时，把它加回下面的列表即可。
SAMPLE_DIRS: list[str] = []

EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".ipynb_checkpoints"}


def _skip(path: str) -> bool:
    """只排除缓存产物。

    注意：**不能**按"_"前缀过滤 —— `aruco_locator/__init__.py` 也以 `_` 开头，
    曾因此把包入口整个漏掉（归档解压后 import 得到空命名空间包）。
    项目根目录的调试脚本本来就不会进包（顶层文件走白名单），无需在这里兜。
    """
    parts = path.replace("\\", "/").split("/")
    if any(p in EXCLUDE_DIRS for p in parts):
        return True
    if os.path.basename(path).endswith((".pyc", ".pyo")):
        return True
    # tools/ 下的 `_xxx.py` 是一次性诊断脚本，不进归档。
    # 这条规则**只能**限定在 tools/ 内：全局按 `_` 前缀过滤会连
    # `aruco_locator/__init__.py` 一起吃掉（历史事故）。
    if len(parts) >= 2 and parts[0] == "tools" and parts[-1].startswith("_"):
        return True
    return False


def _walk_tree(rel: str) -> list[str]:
    out = []
    for cur, dirs, files in os.walk(os.path.join(ROOT, rel)):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in sorted(files):
            p = os.path.join(cur, f)
            if _skip(p):
                continue
            out.append(os.path.relpath(p, ROOT).replace("\\", "/"))
    return sorted(out)


def collect() -> list[str]:
    files = [f for f in TOP_FILES if os.path.isfile(os.path.join(ROOT, f))]
    for t in TREES:
        if os.path.isdir(os.path.join(ROOT, t)):
            files += _walk_tree(t)
    for d in SAMPLE_DIRS:
        if os.path.isdir(os.path.join(ROOT, d)):
            files += _walk_tree(d)
    return sorted(set(files))


def check_completeness(files: list[str]) -> list[str]:
    """归档必须包含 TREES 下除缓存外的**全部**文件，且包入口必须在。

    返回缺失清单（空 = 通过）。这条断言是为了防止"打包规则把关键文件吃掉"
    这类静默事故 —— 历史上就漏过一次 `aruco_locator/__init__.py`。
    """
    have = set(files)
    missing = []
    for t in TREES:
        if not os.path.isdir(os.path.join(ROOT, t)):
            continue
        for rel in _walk_tree(t):
            if rel not in have:
                missing.append(rel)
    for must in ("aruco_locator/__init__.py", "aruco_locator/api.py", "config.yaml",
                 "tools/make_release.py", "tools/validate_new_samples2.py",
                 "tools/compare_vertex_modes.py", "tools/validate_vertex_truth.py",
                 "docs/参数参考.md", "docs/API.md", "CHANGELOG.md"):
        if os.path.isfile(os.path.join(ROOT, must)) and must not in have:
            missing.append(must)
    return sorted(set(missing))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="打包发行归档")
    ap.add_argument("--out", default="release", help="归档输出目录（默认 release）")
    ap.add_argument("--version", default=__version__, help="版本号（默认取包内 __version__）")
    args = ap.parse_args(argv)

    ver = args.version
    pkg = f"aruco-locator-{ver}"
    out_dir = os.path.join(ROOT, args.out)
    os.makedirs(out_dir, exist_ok=True)

    files = collect()
    if not files:
        print("没有收集到任何文件", file=sys.stderr)
        return 1

    missing = check_completeness(files)
    if missing:
        print("归档不完整，缺少以下文件：", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        return 2

    zip_path = os.path.join(out_dir, f"{pkg}.zip")
    sha_lines = []
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for rel in files:
            abs_p = os.path.join(ROOT, rel)
            rel_arc = f"{pkg}/{rel}"
            zf.write(abs_p, rel_arc)
            sha_lines.append(f"{_sha256(abs_p)}  {rel}")

    zip_sha = _sha256(zip_path)
    size_mb = os.path.getsize(zip_path) / 1048576

    manifest = [
        f"# {pkg} 归档清单",
        f"# 生成自 aruco_locator.__version__ = {ver}",
        f"# 文件数 {len(files)}，归档大小 {size_mb:.2f} MB",
        f"# 归档 SHA256: {zip_sha}",
        "",
        "# ---- 逐文件 SHA256 ----",
        *sha_lines,
    ]
    m_path = os.path.join(out_dir, f"MANIFEST-{ver}.txt")
    with open(m_path, "w", encoding="utf-8") as fp:
        fp.write("\n".join(manifest) + "\n")
    s_path = os.path.join(out_dir, f"{pkg}.sha256")
    with open(s_path, "w", encoding="utf-8") as fp:
        fp.write(f"{zip_sha}  {pkg}.zip\n")

    print(f"归档   : {zip_path}  ({size_mb:.2f} MB, {len(files)} 个文件)")
    print(f"校验和 : {s_path}")
    print(f"清单   : {m_path}")
    return 0


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
