#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查「已证伪的方向」有没有被人重新加回来。

背景
----
`work/do-not-retry-20261103/REPORT.md` 列了 9 个今晚上验证失败的方向。
那是文档，人会忘；这个脚本把其中**最容易复发、且能用静态特征识别**的几条做成闸门：

  R1  远场阈值下调到 0.22 以下        → 真实误收 68%（组级 32/47），变体B 79%（70/89）
  R2  组内二次切分                    → 收益 1%（100 段只有 1 段越阈），成本 +5.2 分钟/日
  R3  换 ERes2NetV2                   → AUC 0.650→0.553，d′ 0.51→0.09，清洗后 0.808→0.639
  R4  会话级投票当分类器              → 有 self precision 0.94–1.00 但 recall 仅 0.52–0.58；
                                        低 self→媒体 precision 只有 0.56/0.80

另加两条便宜的（默认只 WARN）：
  R5  前端内联 MEDIA_IDS / LIVE_IDS 字面量   → 与后端 lifecycle 分歧 72/177（41%）
  R6  前端谱减/滤波式降噪                    → 区分度 +0.0488 → −0.0190（媒体反超本人）

判据刻意做成**静态文本特征**（不是语义判断）：宁可漏报，不可误报。

用法
----
    python3 scripts/check_refuted_approaches.py            # 人类可读
    python3 scripts/check_refuted_approaches.py --json     # 机器可读
    python3 scripts/check_refuted_approaches.py --strict   # WARN 也算失败（用于发版前）
    python3 scripts/check_refuted_approaches.py --root /tmp/fixture   # 测单测夹具

退出码
------
    0 = 没有命中任何已证伪方向
    1 = 命中（FAIL；--strict 下 WARN 也算）
    2 = 用法/内部错误（不是"通过"）

如何合法地"明知故犯"
--------------------
在那一行加 `# refuted-ok: <理由>`，脚本会跳过该行并把它列进 "已豁免" 里，
这样审批痕迹留在代码里，而不是靠记忆。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 扫描范围（相对 root）。第一项是主目标，其余是配套。
DEFAULT_TARGETS = (
    "src/lifecycle",       # 占位（不存在时自动跳过）
    "src/life_logger",
    "scripts",
    "web",
    "tests",
)
DEFAULT_FILES = ("local-config.json",)

#: 这些目录里的东西**不是生产逻辑**（模板/备份/第三方），扫它们只会造噪音。
SKIP_DIR_PARTS = (
    "work", "node_modules", "__pycache__", ".git", "backups", "output",
    "outputs", "golden", ".venv", "fixtures", "recordings_test",
)
SKIP_SUFFIX = (
    ".pyc", ".m4a", ".wav", ".onnx", ".ckpt", ".bin", ".png", ".jpg",
    ".jpeg", ".gif", ".webp", ".zip", ".gz", ".tar", ".db", ".sqlite", ".sqlite3",
    ".orig", ".bak",
)
#: 文件**名**里带这些片段的不是生产逻辑（备份/原型页/历史副本），扫它们只会造噪音。
SKIP_NAME_PARTS = ("-prototype", ".bak-", ".pre-", ".before", ".orig",
                   "test_check_refuted_approaches")
#: 一个数字出现在 min()/max() 里是「钳位」不是「阈值」，不能当成已证伪方向。
_RE_CLAMP = re.compile(r"\b(?:min|max)\s*\(")
#: 单文件扫描上限：超过就跳过（避免把大 JSON 全读进内存）
MAX_BYTES = 2_000_000

EXEMPT_MARK = "refuted-ok"

COMMENT_PREFIXES = ("#", "//", "*", "/*", "--", ";;", "%")


def _is_comment(line: str) -> bool:
    s = line.lstrip()
    return any(s.startswith(p) for p in COMMENT_PREFIXES)


#: 已知存量（本次之前就在生产里的），默认不判 FAIL，只当「已登记债务」列出。
#: 文件格式：{"known": [{"rule": "R4", "file": "src/...", "pattern": "...", "why": "..."}]}
DEFAULT_BASELINE = REPO_ROOT / "scripts" / "refuted_approaches_baseline.json"


@dataclass
class Rule:
    """一条已证伪方向。"""
    rid: str
    title: str
    severity: str                      # "fail" | "warn"
    evidence: str                      # 失败数字（引用报告）
    report: str                        # 报告路径
    patterns: tuple[tuple[str, str], ...] = ()   # (名字, 正则)
    hits: list[dict] = field(default_factory=list)

    def scan(self, path: Path, rel: str, text: str,
             exempt_lines: dict[int, str] | None = None) -> None:
        lines = text.splitlines()
        skip = exempt_lines or {}
        for lineno, line in enumerate(lines, 1):
            if _is_comment(line) or lineno in skip:
                continue
            if self.rid == "R1" and _RE_CLAMP.search(line):
                # `min(x, 0.22)` / `max(x, 0.22)` 是钳位，不是把自阈值调到 0.22
                continue
            for name, pat in self.patterns:
                m = re.search(pat, line)
                if m:
                    self.hits.append({
                        "rule": self.rid, "severity": self.severity, "file": rel,
                        "line": lineno, "pattern": name, "snippet": line.strip()[:160],
                    })
                    break


# --------------------------------------------------------------------------
# 规则定义（正则都要求"赋值/参数"语境，避免注释与字符串里的数字误报）
# --------------------------------------------------------------------------
_NUM = r"(\d*\.?\d+)"


def _build_rules() -> list[Rule]:
    return [
        Rule(
            rid="R1",
            title="远场阈值下调到 0.22 以下",
            severity="fail",
            evidence=(
                "11-02 真实数据：召回 38%（15/39）但**误收 68%**（32/47）；变体B 误收 **79%**（70/89）。"
                "根因：真正有内容的 far 组中位 0.154 / p90 0.262，90% 本来就低于 0.22。"
            ),
            report="work/far-threshold-20261103/REPORT.md",
            patterns=(
                ("self_threshold<=0.22", r'self_threshold"?\s*[:=]\s*0?\.(?:2[0-2]|1\d|0\d)\d*\b'),
                ("unknown_threshold<=0.22", r'unknown_threshold"?\s*[:=]\s*0?\.(?:2[0-2]|1\d|0\d)\d*\b'),
                ("far_threshold<0.30", r'far(?:_field)?_(?:self_)?threshold"?\s*[:=]\s*0?\.[0-2]\d*\b'),
                # 只认「在相似度/阈值语境下的比较式」；裸 0.22 不认（CSS rgba / 频谱系数里到处是）
                ("similarity<=0.22 比较",
                 r"(?:similarity|sim_score|voice_sim)\w*\s*(?:<=|<|>=|>)\s*0?\.22\b"),
            ),
        ),
        Rule(
            rid="R2",
            title="组内二次切分（把同一组低分再拆一遍）",
            severity="fail",
            evidence=(
                "抽 100 段逐段重算只有 **1 段**越过 0.40（收益≈1%）；外推约 40 行/日，"
                "成本 **+5.2 分钟/日**。且组内 cos 中位 0.617/0.480/0.590 **单峰**，本来就是同一声源。"
            ),
            report="work/verify-group-split-20261102/REPORT.md",
            patterns=(
                ("组内切分函数", r"def\s+\w*(?:group|cluster)_?(?:split|bisect|resplit|subdivide)\w*\s*\("),
                ("二次切分标识", r"\b(?:second_split\w*|resplit_group\w*|intra_group_split\w*|subcluster_?\w*)"),
                ("组内 kmeans", r"\bKMeans\s*\([^)]*\)\s*\.\s*fit\w*\([^)]*group"),
            ),
        ),
        Rule(
            rid="R3",
            title="换 ERes2NetV2 做声纹",
            severity="fail",
            evidence=(
                "AUC **0.650 → 0.553**，d′ **0.51 → 0.09**（≈无区分力）；清洗错标后 **0.808 → 0.639**。"
                "机制：远场整体抬升且抬媒体(+0.130)比抬本人(+0.103)更多。代价 3.2× 慢、72 MB、需 torch。"
            ),
            report="work/eres2net-v3-20261103/REPORT.md",
            patterns=(
                ("ERes2NetV2", r"\beres2net", ),
                ("模型 ID", r"speech_eres2netv2|ERes2NetV2"),
            ),
        ),
        Rule(
            rid="R4",
            title="会话级投票当身份分类器",
            severity="fail",
            evidence=(
                "有 self → precision 0.94–1.00 但 recall 仅 **0.52–0.58**；低 self→媒体 precision 只有 "
                "**0.56（宽松）/0.80（严格）**；录音级准确率 0.83/0.64。只能做单向保险，不能判「是媒体」。"
            ),
            report="work/session-vote-v2-20261103/REPORT.md",
            patterns=(
                ("会话投票标识", r"\b(?:session_vot\w*|vote_session|session_level_vot\w*)\b"),
                ("self 占比判媒体", r"self_ratio\s*(?:<|<=)\s*0?\.\d+\s*(?::|and|\)|\s+then)"),
                ("self 占比判本人", r"self_ratio\s*(?:>|>=)\s*0?\.\d+\s*(?::|and|\)|\s+then)"),
            ),
        ),
        Rule(
            rid="R5",
            title="前端内联 MEDIA_IDS / LIVE_IDS 字面量",
            severity="warn",
            evidence=(
                "124 项手写常量与后端 lifecycle 分歧 **72/177（41%）**：63 条后端判 live 被误折叠、"
                "9 条 media 被放出；`act_NNN` 每天重号，跨日必误命中。"
            ),
            report="work/six/mismatch-60/REPORT.md",
            patterns=(
                ("内联 ID 集合", r"\bvar\s+(?:LIVE_IDS|MEDIA_IDS)\s*=\s*new\s+Set\("),
                ("内联 ID 集合(const)", r"\bconst\s+(?:LIVE_IDS|MEDIA_IDS)\s*=\s*new\s+Set\("),
            ),
        ),
        Rule(
            rid="R6",
            title="前端谱减/滤波式降噪",
            severity="warn",
            evidence=(
                "谱减让本人中位 0.2140→**0.1347**，区分度 **+0.0488→−0.0190**（媒体反超本人）；"
                "高通+归一化区分度只剩 +0.0425；两种处理下 ≥0.40 都是 **0/15**。"
            ),
            report="work/denoise-test-20261103/REPORT.md",
            patterns=(
                ("谱减法", r"\b(?:spectral_subtraction|spectral_subtract|spec_sub)\b"),
                ("维纳/谱门限", r"\b(?:wiener_?filter|spectral_gate|noise_gate)\s*\("),
            ),
        ),
    ]


# --------------------------------------------------------------------------
# 文件遍历
# --------------------------------------------------------------------------
def iter_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for t in DEFAULT_TARGETS:
        base = root / t
        if not base.exists():
            continue
        if base.is_file():
            out.append(base)
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            rel_parts = p.relative_to(root).parts
            if any(part in SKIP_DIR_PARTS for part in rel_parts):
                continue
            if p.suffix.lower() in SKIP_SUFFIX:
                continue
            if any(part in p.name for part in SKIP_NAME_PARTS):
                continue
            if p.name == Path(__file__).name:
                continue
            try:
                if p.stat().st_size > MAX_BYTES:
                    continue
            except OSError:
                continue
            out.append(p)
    for f in DEFAULT_FILES:
        p = root / f
        if p.is_file():
            out.append(p)
    return sorted(set(out))


def scan(root: Path, rules: list[Rule], baseline: dict | None = None) -> dict:
    files = iter_files(root)
    for p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:                   # noqa: BLE001
            continue
        rel = str(p.relative_to(root))
        # 豁免标记作用于**整行**：先收集行号，再让各规则跳过这些行，
        # 否则一行豁免会被 6 条规则各记一次（历史 bug）。
        exempt_lines: dict[int, str] = {}
        for lineno, line in enumerate(text.splitlines(), 1):
            if EXEMPT_MARK in line and not _is_comment(line):
                exempt_lines[lineno] = line.strip()[:160]
        for lineno, snippet in exempt_lines.items():
            rule = next((r for r in rules if any(re.search(pat, snippet) for _, pat in r.patterns)), None)
            (rule or rules[0]).hits.append({
                "rule": (rule.rid if rule else "?"), "severity": "exempt", "file": rel,
                "line": lineno, "pattern": "(豁免)", "snippet": snippet,
                "_exempt_line": lineno,
            })
        for r in rules:
            r.scan(p, rel, text, exempt_lines)

    known = (baseline or {}).get("known") or []
    known_keys = {(k.get("rule"), k.get("file"), k.get("pattern")) for k in known}
    known_why = {(k.get("rule"), k.get("file"), k.get("pattern")): k.get("why", "") for k in known}

    findings = []
    for r in rules:
        for h in r.hits:
            key = (r.rid, h["file"], h["pattern"])
            severity = h["severity"]
            if severity != "exempt" and key in known_keys:
                severity = "known"
                h = {**h, "why_known": known_why.get(key, "")}
            findings.append({**h, "severity": severity, "title": r.title,
                             "evidence": r.evidence, "report": r.report})

    fails = [f for f in findings if f["severity"] == "fail"]
    warns = [f for f in findings if f["severity"] == "warn"]
    exempt = [f for f in findings if f["severity"] == "exempt"]
    kn = [f for f in findings if f["severity"] == "known"]
    return {
        "root": str(root),
        "files_scanned": len(files),
        "rules": [{"id": r.rid, "title": r.title, "severity": r.severity,
                   "evidence": r.evidence, "report": r.report} for r in rules],
        "findings": findings,
        "counts": {"fail": len(fails), "warn": len(warns),
                   "exempt": len(exempt), "known": len(kn)},
    }


def render_human(res: dict) -> None:
    print("已证伪方向检查 check_refuted_approaches.py")
    print(f"仓库    : {res['root']}")
    print(f"扫描文件: {res['files_scanned']}")
    print()
    if not res["findings"]:
        print("结论: PASS（没有命中任何已证伪方向）")
        return

    by_rule: dict[str, list] = {}
    for f in res["findings"]:
        by_rule.setdefault(f["rule"], []).append(f)

    for rid in sorted(by_rule):
        items = by_rule[rid]
        sev = items[0]["severity"]
        tag = {"fail": "✗ FAIL", "warn": "⚠ WARN", "exempt": "· 豁免", "known": "· 已登记"}[sev]
        print(f"{tag}  [{rid}] {items[0]['title']}")
        if sev in ("fail", "warn"):
            print(f"       失败数字: {items[0]['evidence']}")
            print(f"       依据报告: {items[0]['report']}")
        for it in items:
            print(f"       {it['file']}:{it['line']}  ({it['pattern']})")
            print(f"         {it['snippet']}")
            if it.get("why_known"):
                print(f"         已登记理由: {it['why_known']}")
        print()

    c = res["counts"]
    if c["fail"]:
        print(f"结论: FAIL（新增已证伪方向 {c['fail']} 处；WARN {c['warn']}；"
              f"已登记存量 {c['known']}；豁免 {c['exempt']}）")
    elif c["warn"]:
        print(f"结论: PASS（WARN {c['warn']} 处；已登记存量 {c['known']}；豁免 {c['exempt']}）"
              "—— --strict 下会判 FAIL")
    else:
        print(f"结论: PASS（已登记存量 {c['known']}；豁免 {c['exempt']}）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="检查已证伪方向有没有被重新加回来（见 work/do-not-retry-20261103/REPORT.md）")
    ap.add_argument("--root", default=str(REPO_ROOT), help="仓库根（单测用临时夹具）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--strict", action="store_true", help="WARN 也算失败")
    ap.add_argument("--quiet", action="store_true", help="只打结论行（--json 优先）")
    ap.add_argument("--list-rules", action="store_true", help="只列规则与失败数字")
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE),
                    help="已知存量清单（默认 scripts/refuted_approaches_baseline.json；传空串则不用）")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"ERROR: --root 不是目录：{root}", file=sys.stderr)
        return 2

    rules = _build_rules()
    if args.list_rules:
        for r in rules:
            print(f"[{r.rid}] ({r.severity}) {r.title}")
            print(f"    {r.evidence}")
            print(f"    报告: {r.report}")
        return 0

    baseline = None
    if args.baseline:
        bp = Path(args.baseline)
        if bp.is_file():
            try:
                baseline = json.loads(bp.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                print(f"WARN: baseline 读不了，忽略：{exc}", file=sys.stderr)
        elif str(bp) != str(DEFAULT_BASELINE):
            print(f"WARN: --baseline 不存在：{bp}", file=sys.stderr)

    try:
        res = scan(root, rules, baseline)
    except Exception as exc:                    # noqa: BLE001
        print(f"ERROR: 扫描失败 {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif not args.quiet:
        render_human(res)
    else:
        c = res["counts"]
        print(f"check_refuted_approaches: fail={c['fail']} warn={c['warn']} "
              f"known={c['known']} exempt={c['exempt']}")

    c = res["counts"]
    if c["fail"]:
        return 1
    if args.strict and c["warn"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
