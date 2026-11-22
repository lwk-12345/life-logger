"""守脚本可移植性：`scripts/*.py|sh` 里不再写死本机仓库/工具路径（Q860）。

背景
----
`work/distribution-audit-20261104/hardcoded_paths.csv` 审计出 27 处 scripts/ 下的写死路径。
换台机器 clone 下来，这些脚本会因为 `$HOME/...` 不存在而直接跑不起来，
而报错形态往往是"文件不存在"，看起来像数据问题。

本测试守两条不变量
------------------
1. **仓库绝对路径**（`$HOME/life-logger`）
   不得出现在 `scripts/*.py` / `scripts/*.sh` 里 —— 应改成 `Path(__file__).resolve().parents[1]`
   或 `$(cd "$(dirname "$0")/.." && pwd)` 推导。
2. **任何 `$HOME` 字面量**必须出现在"环境变量 + 原值默认"的上下文里
   （`${LL_XXX:-/Users/...}`、`os.environ.get(...) or "/Users/..."`、`process.env.X || '...'`），
   否则就是写死。

白名单必须逐条写原因（见 ALLOWLIST），新增条目要在 PR 里说明为什么不能改。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

REPO_ABS = str(ROOT)
HOME_LITERAL = "$HOME"

#: 允许出现"仓库绝对路径"的文件 → 原因（逐条必须写清）
ALLOWLIST_REPO_PATH: dict[str, str] = {
    "scripts/test_air_passport.sh": (
        "AirPassport 收包测试脚本：派单明确划为「不改 AirPassport 相关脚本」；"
        "它用 REPO_DIR 定位 fixtures，改动需与该子系统一起回归"
    ),
}

#: 允许保留 `$HOME` 字面量但**不**带环境变量默认的文件 → 原因
ALLOWLIST_HOME_LITERAL: dict[str, str] = {
    "scripts/probe_moss_campplus_e2e.py": "只在模块 docstring 的示例命令里（注释性质，不参与执行）",
}

#: 判定"这一行已经是 env + 原值默认"的形态
ENV_DEFAULT_PATTERNS = (
    re.compile(r"\$\{[A-Z0-9_]+:-$HOME"),          # shell ${LL_X:-$HOME/...}
    re.compile(r"os\.environ\.get\([^)]*\)\s*or\s*[\"']$HOME"),   # py: get(...) or "..."（同一行）
    re.compile(r"^\s*or\s+[\"']$HOME"),                              # py: 上一行 get(...) 换行后 or "..."
    re.compile(r"if\s+_?\w+\s+else\s+Path\([\"']$HOME"),          # py: x if env else Path("...")
    re.compile(r"process\.env\.[A-Z0-9_]+\s*\|\|\s*['\"]$HOME"),  # js: process.env.X || '...'
    re.compile(r"os\.environ\.get\([\"'][A-Z0-9_]+[\"']\s*,\s*[\"']$HOME"),  # py: get("X", "/Users...")
)


def _scan_targets() -> list[Path]:
    files = sorted(SCRIPTS.glob("*.py")) + sorted(SCRIPTS.glob("*.sh"))
    return [p for p in files if p.is_file()]


def _lines(path: Path) -> list[tuple[int, str]]:
    return list(enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1))


def test_scan_targets_is_not_empty() -> None:
    """防止 glob 写错导致"扫到 0 个文件"的假绿。"""
    targets = _scan_targets()
    assert len(targets) > 20, f"scripts/ 下只找到 {len(targets)} 个 .py/.sh，扫描范围异常"
    assert any(t.suffix == ".sh" for t in targets) and any(t.suffix == ".py" for t in targets)


def test_no_repo_absolute_path_in_scripts() -> None:
    """仓库绝对路径必须改成按脚本位置推导。"""
    offenders: list[str] = []
    for path in _scan_targets():
        rel = str(path.relative_to(ROOT))
        if rel in ALLOWLIST_REPO_PATH:
            continue
        for lineno, line in _lines(path):
            if REPO_ABS in line:
                offenders.append(f"{rel}:{lineno}: {line.strip()[:120]}")
    assert offenders == [], (
        "scripts/ 下仍有写死的仓库绝对路径（应改为 Path(__file__).resolve().parents[1] "
        "或 $(cd \"$(dirname \"$0\")/..\" && pwd)）：\n  " + "\n  ".join(offenders)
    )


def test_home_literals_are_env_defaults() -> None:
    """出现的每个 $HOME 都必须是"环境变量 + 原值默认"，而不是裸写死。"""
    offenders: list[str] = []
    for path in _scan_targets():
        rel = str(path.relative_to(ROOT))
        if rel in ALLOWLIST_HOME_LITERAL or rel in ALLOWLIST_REPO_PATH:
            # 仓库路径白名单里的文件天然含 $HOME（仓库就在家目录下），一并放行。
            continue
        for lineno, line in _lines(path):
            if HOME_LITERAL not in line:
                continue
            if any(p.search(line) for p in ENV_DEFAULT_PATTERNS):
                continue
            offenders.append(f"{rel}:{lineno}: {line.strip()[:120]}")
    assert offenders == [], (
        "以下 $HOME 字面量既不在白名单里，也没有环境变量默认包裹：\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("rel", sorted(ALLOWLIST_REPO_PATH))
def test_allowlist_entries_still_exist_and_are_still_needed(rel: str) -> None:
    """白名单不能烂在代码里：文件必须存在，且**真的**还含仓库路径（否则该删条目）。"""
    path = ROOT / rel
    assert path.is_file(), f"白名单里的文件不存在了：{rel}"
    assert REPO_ABS in path.read_text(encoding="utf-8", errors="replace"), (
        f"{rel} 已经不再含仓库绝对路径，请把这条白名单删掉"
    )
    assert len(ALLOWLIST_REPO_PATH[rel].strip()) > 10, "白名单必须写明原因"


@pytest.mark.parametrize("rel", sorted(ALLOWLIST_HOME_LITERAL))
def test_home_literal_allowlist_entries_are_still_needed(rel: str) -> None:
    path = ROOT / rel
    assert path.is_file(), f"白名单里的文件不存在了：{rel}"
    assert HOME_LITERAL in path.read_text(encoding="utf-8", errors="replace"), (
        f"{rel} 已经不再含 $HOME，请把这条白名单删掉"
    )
    assert len(ALLOWLIST_HOME_LITERAL[rel].strip()) > 10, "白名单必须写明原因"
