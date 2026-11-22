#!/usr/bin/env python3
"""列出「代码用到、但 pyproject 里没声明」的第三方包。

用途：依赖声明与代码对齐的唯一闸门。tests/test_deps_declared.py 调它，
CI / 本地都可以直接跑：

    .venv/bin/python scripts/check_deps.py
    .venv/bin/python scripts/check_deps.py --dirs src scripts

退出码
  0  所有用到的三方包都已在 pyproject 声明（也允许「声明了但没用」，只提示）
  1  存在用到但未声明的包
  2  读不到 pyproject / 解析失败

判定口径
  * 只扫 `--dirs`（默认 src、scripts）下的 .py，按 AST 取 import，不执行代码。
  * 排除标准库（sys.stdlib_module_names）、仓库自己的模块（src/life_logger/*、
    scripts/*.py 的同名模块）、相对 import。
  * 「硬依赖」= 模块顶层 import；「软依赖」= 函数/方法体内 import（本项目大部分
    ASR 依赖都是这种，缺了不影响服务启动）。两者都必须声明，只是报告里分开列。
  * import 名与发行名不一致时走 IMPORT_TO_DIST 表；表里没有的按同名处理。
"""
from __future__ import annotations

import argparse
import ast
import sys
import tomllib
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIRS = ("src", "scripts")

# import 名 -> PyPI 发行名（只列不一致的）
IMPORT_TO_DIST = {
    "faster_whisper": "faster-whisper",
    "funasr_onnx": "funasr-onnx",
    "mlx_whisper": "mlx-whisper",
    "sherpa_onnx": "sherpa-onnx",
    "typesafe_sdk": "typesafe-sdk",
    "qwen_asr": "qwen-asr",
    "panns_inference": "panns-inference",
    "yaml": "pyyaml",
    "PIL": "pillow",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "google": "protobuf",
}

# 明确不需要声明的：仓库内模块（动态算出来，见 local_modules()）+ 这些
ALWAYS_ALLOWED = {
    "life_logger",
    "conftest",
}

# 已声明、但任何环境都没装过 → 无法给出实际版本（锁文件里不钉）。
# qwen_asr 只出现在 scripts/asr_benchmark.py 的延迟 import 里；PyPI 上的包名尚未核实。
UNPINNED_OK = {"qwen-asr"}

# 不需要被 import 扫描证明的声明：测试/构建工具，或只在 subprocess 脚本里出现
TOOLING_ONLY = {
    "pytest",
    "ruff",
    "panns-inference",   # 只出现在 audio_scene.py 的 subprocess 脚本文本里
    "socksio",           # 代理用，按需安装，代码里没有直接 import
    "setuptools",
}


def normalize(name: str) -> str:
    """PEP 503 规范化：大小写、-_. 一律视为相同。"""
    return name.strip().lower().replace("_", "-").replace(".", "-")


def local_modules(dirs: tuple[str, ...], base: Path | None = None) -> set[str]:
    """仓库内可直接 import 的模块名（src/life_logger 与 scripts/*.py 的 stem）。"""
    names = set(ALWAYS_ALLOWED) | set(dirs) | {"src"}
    root = Path(base) if base is not None else ROOT
    for d in dirs:
        d_root = root / d
        if not d_root.is_dir():
            continue
        for f in d_root.rglob("*.py"):
            names.add(f.stem)
        for sub in d_root.iterdir():
            if sub.is_dir() and (sub / "__init__.py").exists():
                names.add(sub.name)
    return names


def collect_imports(dirs: tuple[str, ...] = DEFAULT_DIRS, base: Path | None = None) -> dict[str, dict[str, set[str]]]:
    """扫描目录，返回 {模块名: {"hard": {...文件}, "soft": {...文件}}}（已剔除标准库/本地模块）。"""
    base = Path(base) if base is not None else ROOT
    stdlib = set(sys.stdlib_module_names)
    local = local_modules(dirs, base)
    found: dict[str, dict[str, set[str]]] = defaultdict(lambda: {"hard": set(), "soft": set()})

    for d in dirs:
        scan_root = base / d
        for f in sorted(scan_root.rglob("*.py")):
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                continue
            rel = str(f.relative_to(base))
            top_level: set[str] = set()
            for node in tree.body:
                if isinstance(node, ast.Import):
                    top_level.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    top_level.add(node.module.split(".")[0])
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level or not node.module:
                        continue
                    names = [node.module.split(".")[0]]
                else:
                    continue
                for name in names:
                    if name in stdlib or name in local:
                        continue
                    bucket = "hard" if name in top_level else "soft"
                    found[name][bucket].add(rel)
    return dict(found)


def declared_distributions(pyproject: Path) -> tuple[dict[str, str], dict[str, str]]:
    """返回 (core {dist: 原始声明}, extras {dist: 组名})。"""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data.get("project") or {}
    core: dict[str, str] = {}
    extras: dict[str, str] = {}
    for spec in project.get("dependencies") or []:
        core[_spec_name(spec)] = spec
    for group, specs in (project.get("optional-dependencies") or {}).items():
        for spec in specs or []:
            extras.setdefault(_spec_name(spec), group)
    for group, specs in (data.get("dependency-groups") or {}).items():
        for spec in specs or []:
            if isinstance(spec, str):
                extras.setdefault(_spec_name(spec), f"group:{group}")
    return core, extras


def _spec_name(spec: str) -> str:
    """从 `foo>=1.0` / `foo[bar]==2` 里取出发行名（规范化后）。"""
    for sep in ("[", " ", ">", "<", "=", "!", "~", ";"):
        if sep in spec:
            spec = spec.split(sep, 1)[0]
    return normalize(spec)


def evaluate(imports: dict[str, dict[str, set[str]]], declared: set[str]) -> dict[str, list[str]]:
    """纯函数：给出未声明/未使用的模块清单（便于测试，不碰磁盘）。"""
    undeclared, unused = [], []
    for module in sorted(imports):
        dist = normalize(IMPORT_TO_DIST.get(module, module))
        if dist not in declared:
            undeclared.append((module, dist, imports[module]))
    for dist in sorted(declared):
        if not any(normalize(IMPORT_TO_DIST.get(m, m)) == dist for m in imports):
            unused.append(dist)
    return {"undeclared": undeclared, "unused": unused}


def lock_versions(path: Path) -> dict[str, str]:
    """读 requirements.lock.txt 里形如 `name==1.2.3` 的行（注释与未固定行忽略）。"""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "==" in line:
            name, ver = line.split("==", 1)
            out[normalize(name)] = ver.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="检查 pyproject 是否声明了代码用到的三方包")
    ap.add_argument("--dirs", nargs="+", default=list(DEFAULT_DIRS), help="要扫描的目录（默认 src scripts）")
    ap.add_argument("--pyproject", default=str(ROOT / "pyproject.toml"))
    ap.add_argument("--lock", default=str(ROOT / "requirements.lock.txt"))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    pyproject = Path(args.pyproject)
    if not pyproject.is_file():
        print(f"找不到 {pyproject}", file=sys.stderr)
        return 2
    try:
        core, extras = declared_distributions(pyproject)
    except (OSError, tomllib.TOMLDecodeError) as error:
        print(f"pyproject 解析失败：{error}", file=sys.stderr)
        return 2

    imports = collect_imports(tuple(args.dirs))
    declared = set(core) | set(extras)
    result = evaluate(imports, declared)

    undeclared = result["undeclared"]
    unused = [d for d in result["unused"] if d not in TOOLING_ONLY]
    tooling = [d for d in result["unused"] if d in TOOLING_ONLY]

    if not args.quiet:
        print(f"扫描目录：{', '.join(args.dirs)}")
        print(f"三方 import：{len(imports)} 个；已声明：{len(declared)} 个"
              f"（core {len(core)} + extras {len(extras)}）\n")
        print(f"{'模块':<18}{'发行名':<18}{'硬/软':<10}声明位置")
        print("-" * 68)
        for module in sorted(imports):
            dist = normalize(IMPORT_TO_DIST.get(module, module))
            g = imports[module]
            kind = f"{len(g['hard'])}/{len(g['soft'])}"
            where = "core" if dist in core else extras.get(dist, "— 未声明 —")
            print(f"{module:<18}{dist:<18}{kind:<10}{where}")
        print()

    lock = lock_versions(Path(args.lock))
    if undeclared:
        print("用到但未声明（必须修）：")
        for module, dist, groups in undeclared:
            files = sorted(groups["hard"] or groups["soft"])[:3]
            print(f"  - {module}（发行名 {dist}）硬依赖 {len(groups['hard'])} 处 / 软依赖 {len(groups['soft'])} 处")
            for f in files:
                print(f"      {f}")
        print()
    if unused:
        print("声明了但代码里没用到（可清理，不阻断）：")
        print("  - " + "、".join(unused))
        print()
    if tooling and not args.quiet:
        print("工具/子进程类声明（不做 import 扫描）：" + "、".join(tooling))
        print()
    if lock:
        drift = [m for m in sorted(imports)
                 if normalize(IMPORT_TO_DIST.get(m, m)) in lock]
        if drift and not args.quiet:
            unpinned = sorted(normalize(IMPORT_TO_DIST.get(m, m)) for m in imports
                              if normalize(IMPORT_TO_DIST.get(m, m)) not in lock)
            print(f"锁文件 {Path(args.lock).name} 覆盖 {len(drift)}/{len(imports)} 个用到的包"
                  f"；未钉版本：{'、'.join(unpinned) or '无'}")
            print()

    if undeclared:
        print(f"RESULT FAIL —— {len(undeclared)} 个包用到但未在 pyproject 声明")
        return 1
    print(f"RESULT PASS —— {len(imports)} 个三方 import 全部已声明")
    return 0


if __name__ == "__main__":
    sys.exit(main())
