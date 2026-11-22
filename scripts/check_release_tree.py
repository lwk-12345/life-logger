#!/usr/bin/env python3
"""发布树门禁：挡住 PII / 运行时状态 / 硬依赖漏提。

设计目标（2026-10-23 A0 派单）：
1. **默认只看"这次要提交什么"**：暂存区文件（`git diff --cached`）+ 显式 `--files`；
   `--all-worktree` 用于预检"如果 git add -A 会带进什么"。
2. 拦四类东西：
   · 凭据/密钥（.env / *.pem / *.key / id_rsa / secrets*.json / token 文件）
   · 运行时产物（output/ work/ backups/ golden/ *.log *.jsonl agent-board 的 queue/notify/outbox/gate-report）
   · 超大临时文件（>5MB 直接 ERROR，>2MB WARN）
   · PII（手机号 / 邮箱 / 私钥块 / 各家 token）——**只报类别、路径、次数，绝不回显内容**
3. **硬依赖批次检查**：8 个运行时模块必须"要么全进、要么全不进"，
   否则 clone 后会 ImportError（见 work/release-safety-20261023/dependency-graph.txt）。
4. 允许白名单：验收类 `.md`（`*acceptance.md`、`work/automation-*/**.md`）不算运行时垃圾。
5. 输出：人读的简短中文结论 + `--json` 机器可读；退出码 0=OK / 1=有违规 / 2=用法或 git 出错。
   **不执行** git add / commit / push，也不修改任何文件。

用法：
    python3 scripts/check_release_tree.py                    # 只查暂存区
    python3 scripts/check_release_tree.py --all-worktree      # 预检：git add -A 会带什么
    python3 scripts/check_release_tree.py --files a.py b.md   # 查指定文件
    python3 scripts/check_release_tree.py --all-worktree --json
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path

SCHEMA = "release-tree-guard-v1"

# ── 硬依赖：必须整批出现（缺一个 → ImportError）────────────────────────────
HARD_DEPENDENCY_CLUSTER = [
    "src/life_logger/runtime_queue.py",
    "src/life_logger/day_views.py",
    "src/life_logger/processing_runtime.py",
    "src/life_logger/processing_actions.py",
    "src/life_logger/runtime_launch.py",
    "scripts/processing_worker.py",
]

# ── 路径级禁止（运行时产物 / 凭据目录）────────────────────────────────────
FORBIDDEN_PATH_PATTERNS = [
    # 运行时与临时产物
    "output/*",
    "work/*",
    "backups/*",
    "golden/*",
    "*.log",
    "*.log.[0-9]*",
    "*.jsonl",
    "*.tmp",
    "*.temp",
    "*.swp",
    "*.swo",
    "*.crash",
    "*.tar.gz",
    "*.zip",
    # 凭据与密钥材料
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa*",
    "id_ed25519*",
    "secrets*.json",
    "*secrets*.json",
    "*.secret",
    "*.credentials",
    "local-config.json",
    # agent-board 的运行态（验收 md 走白名单）
    "agent-board/outbox/*",
    "agent-board/queue*.json",
    "agent-board/notify.*",
    "agent-board/state*.json",
    "agent-board/gate-report.json",
    "agent-board/*.log",
    "agent-board/*.jsonl",
]

# ── 白名单：允许入库的文档（先于禁止规则判定）─────────────────────────────
ALLOWLIST_PATTERNS = [
    "work/automation-*/*.md",
    "work/automation-*/**/*.md",
    "*/acceptance.md",
    "*acceptance.md",
    "docs/*.md",
    "docs/**/*.md",
    "*.md",
]

# ── PII / secret 模式（只统计，不回显）────────────────────────────────────
PII_PATTERNS = {
    "私钥块": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "OpenAI密钥": r"sk-[A-Za-z0-9_-]{20,}",
    "GitHub令牌": r"ghp_[A-Za-z0-9]{20,}",
    "AWS密钥": r"AKIA[0-9A-Z]{16}",
    "Slack令牌": r"xox[baprs]-[0-9A-Za-z-]{10,}",
    "TelegramBot": r"Bot\s*\d{8,}:[A-Za-z0-9_-]{20,}",
    "手机号": r"(?<!\d)1[3-9]\d{9}(?!\d)",
    "邮箱": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    # 只认「看起来像真值」的赋值：排除 test/dummy/fake/example/xxx/changeme 之类占位，
    # 否则每个测试夹具都会被判成密钥泄漏（实测 tests/test_external_web.py 就是这么误报的）。
    "疑似密钥赋值": (
        r"(?i)['\"]?(api[_-]?key|secret|password|passwd|access[_-]?token)['\"]?\s*[:=]\s*['\"]"
        r"(?![^'\"]*(?:test|dummy|fake|example|placeholder|changeme|xxxx|\*\*\*))[^'\"]{12,}['\"]"
    ),
}

SIZE_ERROR_BYTES = 5 * 1024 * 1024
SIZE_WARN_BYTES = 2 * 1024 * 1024
TEXT_SUFFIXES = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".json", ".md", ".txt", ".sh",
    ".toml", ".yml", ".yaml", ".ini", ".cfg", ".html", ".css", ".swift", ".sql",
}


def _matches(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, pat) for pat in patterns)


def _git(root: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)
    return proc.returncode, proc.stdout


def collect_targets(root: Path, *, all_worktree: bool, files: list[str]) -> tuple[list[str], str | None]:
    """返回 (相对路径列表, 错误信息)。"""
    targets: list[str] = []
    if all_worktree:
        # 预检 = "现在 git add -A 会进提交的文件"：
        #   已跟踪但被改动（M/A/D 的 M/A）+ 未跟踪且没被 ignore（??）
        # 不能用 `ls-files --cached`：那会把 166 个已入库的 golden/ 也算进来，
        # 于是一堆历史遗留把新问题淹没（实测 159 条噪音）。
        code, out = _git(root, "status", "--porcelain", "-uall")
        if code != 0:
            return [], "git status 失败（不是 git 仓库？）"
        for line in out.splitlines():
            if len(line) < 4:
                continue
            status, rel = line[:2], line[3:].strip()
            if "D" in status:
                continue
            if status.strip() in {"M", "A", "??", "AM", "MM", "MA", "RM", "RA"} or status.strip().startswith("R"):
                rel = rel.split(" -> ")[-1]
                targets.append(rel)
    else:
        code, out = _git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR")
        if code == 0:
            targets.extend(p for p in out.splitlines() if p.strip())
    targets.extend(f.strip().lstrip("./") for f in files if f.strip())
    # 去重且保序
    seen, uniq = set(), []
    for t in targets:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq, None


def tracked_set(root: Path) -> set[str]:
    code, out = _git(root, "ls-files")
    return set(out.splitlines()) if code == 0 else set()


def check_paths(root: Path, targets: list[str], tracked: set[str]) -> list[dict]:
    errors: list[dict] = []
    for rel in targets:
        if _matches(rel, ALLOWLIST_PATTERNS):
            continue
        if _matches(rel, FORBIDDEN_PATH_PATTERNS):
            already = rel in tracked
            errors.append({
                "kind": "forbidden_path",
                "path": rel,
                "severity": "WARN" if already else "ERROR",
                "message": (
                    "运行时产物/凭据类路径，且**已在历史里跟踪**（属历史遗留，需 git rm --cached 才彻底干净）"
                    if already else
                    "运行时产物/凭据类路径不应入库"
                ),
            })
            continue
        path = root / rel
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > SIZE_ERROR_BYTES:
            errors.append({
                "kind": "oversize_file",
                "path": rel,
                "bytes": size,
                "message": f"文件 {size/1048576:.1f}MB 超过 5MB 上限（可能是二进制/产物误入）",
            })
        elif size > SIZE_WARN_BYTES:
            errors.append({
                "kind": "large_file_warn",
                "path": rel,
                "bytes": size,
                "severity": "WARN",
                "message": f"文件 {size/1048576:.1f}MB 偏大（>2MB），请确认是否应入库",
            })
    return errors


def check_pii(root: Path, targets: list[str], tracked: set[str]) -> list[dict]:
    """只统计类别与次数；**不读回内容到报告里**。"""
    findings: list[dict] = []
    for rel in targets:
        # 注意：这里**不**套 ALLOWLIST——验收 md 允许入库，但里面的 PII 仍须报出来
        path = root / rel
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hit = {}
        for name, pat in PII_PATTERNS.items():
            n = len(re.findall(pat, text))
            if n:
                hit[name] = n
        if hit:
            findings.append({
                "kind": "pii_pattern",
                "path": rel,
                "categories": hit,
                "severity": "WARN" if rel in tracked else "ERROR",
                "message": (
                    "疑似 PII/密钥样式（已跟踪的历史文件 → WARN；只报类别与次数，未输出内容）"
                    if rel in tracked else
                    "疑似 PII/密钥样式（新增文件 → ERROR；只报类别与次数，未输出内容）"
                ),
            })
    return findings


def check_hard_dependencies(targets: list[str]) -> list[dict]:
    """8 个运行时模块必须整批出现；部分出现 → ERROR。"""
    present = [p for p in HARD_DEPENDENCY_CLUSTER if p in targets]
    missing = [p for p in HARD_DEPENDENCY_CLUSTER if p not in targets]
    if not present or not missing:
        return []
    return [{
        "kind": "hard_dependency_partial",
        "present": present,
        "missing": missing,
        "message": (
            "硬依赖只提交了一部分："
            f"缺 {len(missing)}/{len(HARD_DEPENDENCY_CLUSTER)} 个，"
            "clone 后会 ImportError（必须整批同提交，或整批都不提交）"
        ),
    }]


def summarize(targets: list[str], errors: list[dict], pii: list[dict]) -> dict:
    hard = [e for e in errors if e["kind"] == "hard_dependency_partial"]
    blocking = [e for e in errors if e.get("severity") != "WARN"]
    warns = [e for e in errors if e.get("severity") == "WARN"]
    pii_block = [p for p in pii if p.get("severity") != "WARN"]
    pii_warn = [p for p in pii if p.get("severity") == "WARN"]
    warns.extend(pii_warn)
    ok = not blocking and not pii_block and not hard
    return {
        "schema_version": SCHEMA,
        "checked_files": len(targets),
        "hard_dependency_cluster": HARD_DEPENDENCY_CLUSTER,
        "hard_dependency_present": [p for p in HARD_DEPENDENCY_CLUSTER if p in targets],
        "blocking": blocking,
        "warnings": warns,
        "pii": pii,
        "pii_blocking_count": len(pii_block),
        "verdict": "PASS" if ok else "FAIL",
        "exit_code": 0 if ok else 1,
    }


def render_human(report: dict) -> str:
    lines = [f"发布树门禁：检查 {report['checked_files']} 个文件"]
    present = report["hard_dependency_present"]
    lines.append(
        f"  硬依赖：{len(present)}/{len(HARD_DEPENDENCY_CLUSTER)} 个在本次批次"
        + ("（整批齐全）" if len(present) == len(HARD_DEPENDENCY_CLUSTER) else
           "（0 个，视为本次不涉及运行时簇）" if not present else "（⚠️ 不完整）")
    )
    blockers = report["blocking"]
    if blockers:
        lines.append(f"  ❌ 阻断项 {len(blockers)} 条：")
        for b in blockers[:20]:
            lines.append(f"     · [{b['kind']}] {b.get('path') or ''} — {b['message']}")
        if len(blockers) > 20:
            lines.append(f"     …… 其余 {len(blockers)-20} 条见 --json")
    if report["warnings"]:
        lines.append(f"  ⚠️ 警告 {len(report['warnings'])} 条：")
        for w in report["warnings"][:10]:
            lines.append(f"     · [{w['kind']}] {w.get('path')} — {w['message']}")
    if report["pii"]:
        blk = report.get("pii_blocking_count", 0)
        lines.append(
            f"  {'❌' if blk else '⚠️'} PII/密钥样式命中 {len(report['pii'])} 个文件"
            f"（其中新增阻断 {blk} 个；只报类别）："
        )
        for p in report["pii"][:10]:
            cats = "、".join(f"{k}×{v}" for k, v in p["categories"].items())
            lines.append(f"     · {p['path']} — {cats}")
    lines.append(f"  结论：{report['verdict']}（退出码 {report['exit_code']}）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="发布树门禁：PII / 运行时状态 / 硬依赖")
    ap.add_argument("--root", default=".", help="仓库根（默认当前目录）")
    ap.add_argument("--all-worktree", action="store_true",
                    help="预检模式：检查 `git add -A` 会带入的全部文件（含未跟踪、尊重 .gitignore）")
    ap.add_argument("--files", nargs="*", default=[], help="显式指定要检查的文件（相对仓库根）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--no-pii", action="store_true", help="跳过 PII 扫描（只看路径与体积）")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"root 不存在：{root}", file=sys.stderr)
        return 2

    targets, err = collect_targets(root, all_worktree=args.all_worktree, files=args.files)
    if err:
        print(err, file=sys.stderr)
        return 2
    if not targets:
        report = {
            "schema_version": SCHEMA, "checked_files": 0,
            "hard_dependency_cluster": HARD_DEPENDENCY_CLUSTER,
            "hard_dependency_present": [], "blocking": [], "warnings": [], "pii": [],
            "verdict": "PASS", "exit_code": 0,
            "note": "没有待检查的文件（暂存区为空且未传 --files / --all-worktree）",
        }
        print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else
              "发布树门禁：暂存区为空，无需检查（PASS）")
        return 0

    tracked = tracked_set(root)
    errors = check_paths(root, targets, tracked)
    errors.extend(check_hard_dependencies(targets))
    pii = [] if args.no_pii else check_pii(root, targets, tracked)
    report = summarize(targets, errors, pii)

    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_human(report))
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
