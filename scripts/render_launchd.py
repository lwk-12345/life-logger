#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 deploy/launchd/*.plist.tmpl 渲染成可直接安装的 plist —— **只生成文件，不加载、不注册**。

为什么需要
----------
已安装的 plist 里满是本机绝对路径（仓库位置、家目录、解释器），换台机器 clone 下来
只能手改 10 份 XML。模板化以后，渲染只需三条输入：仓库在哪、家在哪、用哪个解释器。

占位符（只有三个）
------------------
``__REPO__``    仓库根目录（脚本位置推导，可用 --repo 覆盖）
``__PYTHON__``  该 job 要用的 Python 解释器（--python 全局默认 + --python-map 按 label 覆盖）
``__HOME__``    当前用户家目录（日志路径、外部 venv 等）

安全边界（硬编码在代码里，不是提示）
------------------------------------
1. 默认渲染到**系统临时目录**，路径打印出来供人工检查；
2. 目标目录若是 `~/Library/LaunchAgents`（或任何以 LaunchAgents 结尾的目录）→ **直接拒绝**，
   并给出"请自行确认后手动复制"的提示；
3. 本脚本**不执行任何系统管理命令**：不 import subprocess，不调用任何注册/加载命令。

用法
----
    python3 scripts/render_launchd.py                        # 渲染到临时目录并打印路径
    python3 scripts/render_launchd.py --out /tmp/my-plists   # 渲染到指定目录
    python3 scripts/render_launchd.py --python /usr/bin/python3
    python3 scripts/render_launchd.py --python-map com.life-logger.daily-synthesis=/opt/anaconda3/bin/python3
    python3 scripts/render_launchd.py --list                 # 只列模板与占位符统计
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

PLACEHOLDERS = ("__REPO__", "__PYTHON__", "__HOME__")

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "deploy" / "launchd"
TEMPLATE_SUFFIX = ".plist.tmpl"

#: 明确拒绝的渲染目标（绝不写入系统 launchd 目录）
FORBIDDEN_TARGET_PARTS = ("Library/LaunchAgents",)


class RenderRefused(RuntimeError):
    """渲染目标被安全策略拒绝。"""


def templates() -> list[Path]:
    if not TEMPLATE_DIR.is_dir():
        raise SystemExit(f"模板目录不存在：{TEMPLATE_DIR}")
    files = sorted(TEMPLATE_DIR.glob(f"*{TEMPLATE_SUFFIX}"))
    if not files:
        raise SystemExit(f"{TEMPLATE_DIR} 下没有 *{TEMPLATE_SUFFIX}")
    return files


def label_of(template: Path) -> str:
    return template.name[: -len(TEMPLATE_SUFFIX)] if template.name.endswith(TEMPLATE_SUFFIX) else template.stem


def _default_python(repo: Path) -> str:
    """项目自带解释器优先；不存在则退回当前解释器。"""
    venv = repo / ".venv" / "bin" / "python"
    if venv.is_file():
        return str(venv)
    return sys.executable


def _check_target(out_dir: Path) -> None:
    text = str(out_dir)
    for part in FORBIDDEN_TARGET_PARTS:
        if part in text:
            raise RenderRefused(
                f"拒绝渲染到 {out_dir}：本脚本只生成文件，绝不写系统 launchd 目录。"
                "请渲染到临时目录，人工确认后再自行复制。"
            )


def render_text(template: Path, *, repo: Path, home: Path, python: str, label: str,
                python_map: dict[str, str] | None = None) -> str:
    text = template.read_text(encoding="utf-8")
    py = (python_map or {}).get(label) or python
    text = text.replace("__REPO__", str(repo))
    text = text.replace("__HOME__", str(home))
    text = text.replace("__PYTHON__", str(py))
    leftover = [p for p in PLACEHOLDERS if p in text]
    if leftover:
        raise RenderRefused(f"{template.name} 渲染后仍残留占位符：{leftover}")
    return text


def render_all(
    *,
    out_dir: Path | str,
    repo: Path | str | None = None,
    home: Path | str | None = None,
    python: str | None = None,
    python_map: dict[str, str] | None = None,
) -> list[Path]:
    """渲染全部模板到 out_dir，返回写出的文件列表。不写系统目录、不注册任何东西。"""
    out = Path(out_dir)
    _check_target(out)
    repo_path = Path(repo) if repo else ROOT
    home_path = Path(home) if home else Path.home()
    py = python or _default_python(repo_path)

    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for template in templates():
        label = label_of(template)
        text = render_text(template, repo=repo_path, home=home_path, python=py,
                           label=label, python_map=python_map)
        target = out / f"{label}.plist"
        target.write_text(text, encoding="utf-8")
        written.append(target)
    return written


def _count_placeholders(template: Path) -> dict[str, int]:
    text = template.read_text(encoding="utf-8")
    return {p: text.count(p) for p in PLACEHOLDERS}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="渲染 deploy/launchd 模板（只生成文件，不加载）")
    ap.add_argument("--out", default=None, help="输出目录；默认系统临时目录")
    ap.add_argument("--repo", default=None, help="仓库根目录；默认按脚本位置推导")
    ap.add_argument("--home", default=None, help="家目录；默认 Path.home()")
    ap.add_argument("--python", default=None, help="__PYTHON__ 默认值；默认 <repo>/.venv/bin/python")
    ap.add_argument("--python-map", default="", help="按 label 覆盖解释器：label=path,label=path")
    ap.add_argument("--list", action="store_true", help="只列出模板与占位符统计，不渲染")
    args = ap.parse_args(argv)

    if args.list:
        for template in templates():
            counts = _count_placeholders(template)
            used = ", ".join(f"{k}×{v}" for k, v in counts.items() if v)
            print(f"{label_of(template):38s} {used or '（无占位符）'}")
        return 0

    python_map: dict[str, str] = {}
    for chunk in (args.python_map or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise SystemExit(f"--python-map 需要 label=path 形式：{chunk!r}")
        key, value = chunk.split("=", 1)
        python_map[key.strip()] = value.strip()

    out_dir = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="launchd-render-"))
    try:
        written = render_all(out_dir=out_dir, repo=args.repo, home=args.home,
                             python=args.python, python_map=python_map)
    except RenderRefused as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 3

    print(f"已渲染 {len(written)} 个 plist → {out_dir}")
    for path in written:
        print(f"  {path}")
    print("\n下一步（需人工确认，本脚本不会执行）：检查上面文件内容无误后，自行复制到目标目录。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
