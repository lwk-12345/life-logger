#!/usr/bin/env python3
"""失败会话选择性恢复工具（默认 dry-run，绝不自动调用模型）。

背景（2026-10-24 Q143 审计）：6,529 条 `deepseek_batch_failed` 全部来自 13 个会话，
这 13 个会话有 2,102 段录音、约 1.37 GB，但**事件层为 0**。音频与 ASR 转录都在，
`pending-review-states.json` 里它们是 `archived: 历史整批 API 失败`（自动归档，不是人点的）。

本工具只做三件事：
1. 从审计 JSON 生成"按日期/会话/job/失败原因"排序的恢复清单；
2. 区分**可恢复**（failed_archived + 瞬时错误）与**不可恢复/不该碰**（人工忽略、非瞬时错误、无转录）；
3. 输出每个会话的预计段数、quote 数、目标产物、回滚快照路径。

安全边界：
* 默认 dry-run，只打印清单；
* `--apply` 必须同时给 `--session`（可多次）与 `--confirm`，否则直接 exit 2；
* `--apply` 当前**只生成执行 runbook**（要跑的命令文本），不调用模型、不写生产；
* 并发硬上限 2，可恢复条数默认 5、上限 13。

用法：
    scripts/run-with-timeout.py 120 -- python3 scripts/recover_failed_context_sessions.py
    scripts/run-with-timeout.py 120 -- python3 scripts/recover_failed_context_sessions.py --json
    scripts/run-with-timeout.py 120 -- python3 scripts/recover_failed_context_sessions.py \
        --apply --session 5C3492... --confirm
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tarfile
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO = Path(__file__).resolve().parents[1]
DEFAULT_AUDIT_DIR = REPO / "work" / "deepseek-failure-audit-20261024"
DEFAULT_STATES = REPO / "output" / "local-web" / "pending-review-states.json"
DEFAULT_JOB_ROOT = REPO / "output" / "local-web"
MAX_CONCURRENCY = 2
MAX_RECOVERABLE = 13

# 只有这些错误是"重试可能成功"的；判不准的一律不进可恢复集合
TRANSIENT_TOKENS = (
    "connection", "urlerror", "timeout", "timed out", "temporarily",
    "reset", "econnreset", "502", "503", "504", "rate_limit", "429",
)
# 明确"重试也不会好"的错误：鉴权/参数/内容审核
PERMANENT_TOKENS = ("auth", "401", "403", "400", "invalid_request", "content_filter",
                    "not_found", "404", "permission")
# 人工处置过的标记（出现在 pending-review-states.json 的 note 里）
HUMAN_NOTES = ("用户忽略", "用户标记已处理")

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: session 只允许安全字符（与 watch_stream.SAFE_ID 同口径），防路径穿越
SESSION_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

RECOVERABLE = "recoverable_failed_archived"
TRIAGE = "needs_manual_triage"
REJECTED = "rejected_invalid_identifier"
SKIP_HUMAN = "skipped_human_ignored"
SKIP_NON_TRANSIENT = "skipped_non_transient_error"
SKIP_NO_TRANSCRIPT = "skipped_no_transcript"


@dataclass
class SessionPlan:
    date: str
    session: str
    job: str | None
    failure_reason: str
    segments: int
    quotes: int
    classification: str
    target_events: str
    target_context: str | None
    rollback_snapshot: str
    notes: list[str] = field(default_factory=list)

    @property
    def recoverable(self) -> bool:
        return self.classification == RECOVERABLE


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


class SnapshotError(RuntimeError):
    """回滚快照生成或校验失败。"""


def valid_date(value: object) -> bool:
    text = str(value or "").strip()
    if not DATE_PATTERN.match(text):
        return False
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def valid_session(value: object) -> bool:
    return bool(SESSION_PATTERN.match(str(value or "").strip()))


def _safe_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _job_ids_from_label(label: str) -> list[str]:
    """从任意字符串里抽 job id：既支持 "任务 job-2026..."，也支持 "jobs/job-2026..."。"""
    import re
    return re.findall(r"job-\d{8}-\d{6}-[0-9a-f]{8}", str(label or ""))


def load_audit(audit_dir: Path) -> dict[str, Any]:
    zero = _read_json(audit_dir / "sessions_with_zero_events.json", [])
    failed_jobs = _read_json(audit_dir / "failed_jobs.json", [])
    causes = _read_json(audit_dir / "per_job_causes.json", [])
    sources = _read_json(audit_dir / "unique_failed_sources.json", [])
    return {"zero": zero, "failed_jobs": failed_jobs, "causes": causes, "sources": sources}


def map_jobs(audit: Mapping[str, Any]) -> tuple[dict[str, list[str]], dict[str, dict]]:
    """返回 session -> [job_id]，以及 job_id -> cause 记录。"""
    session_jobs: dict[str, list[str]] = {}
    for entry in audit.get("failed_jobs") or []:
        jobs = _job_ids_from_label(str(entry.get("label") or ""))
        state = entry.get("session_state") or {}
        session = str(state.get("session") or "").strip()
        if not session:
            continue
        bucket = session_jobs.setdefault(session, [])
        for job in jobs:
            if job not in bucket:
                bucket.append(job)
    cause_by_job: dict[str, dict] = {}
    for cause in audit.get("causes") or []:
        artifact = str(cause.get("artifact") or "")
        for job in _job_ids_from_label(artifact):
            cause_by_job[job] = dict(cause)
    return session_jobs, cause_by_job


def human_touched(states_path: Path, session: str, job: str | None) -> bool:
    """该会话/该 job 是否被人手动处置过（只认『用户忽略 / 用户标记已处理』）。"""
    payload = _read_json(states_path, {})
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, dict):
        return False
    needles = [n for n in (session, job) if n]
    for key, value in records.items():
        if not isinstance(value, Mapping):
            continue
        note = str(value.get("note") or "")
        if not any(marker in note for marker in HUMAN_NOTES):
            continue
        if any(needle in str(key) or needle in note for needle in needles):
            return True
    return False


def cause_is_transient(cause: Mapping[str, Any] | None) -> bool:
    """只有明确的瞬时故障才算「重试可能成功」。

    `kinds=['None']`（旧产物没写错误码）与 degraded（部分成功）都不算 —— 它们进 triage，
    由人决定要不要重跑，工具不替用户赌。
    """
    if not cause:
        return False
    raw_kinds = [str(k) for k in (cause.get("kinds") or {}).keys()]
    if any(k.lower() in {"none", "unknown", ""} for k in raw_kinds) or not raw_kinds:
        return False
    kinds = " ".join(raw_kinds).lower()
    errors = " ".join(str(e[0]) for e in (cause.get("top_errors") or []) if e).lower()
    return any(token in f"{kinds} {errors}" for token in TRANSIENT_TOKENS)


def _kinds_are_blank(cause: Mapping[str, Any] | None) -> bool:
    raw = [str(k).lower() for k in ((cause or {}).get("kinds") or {}).keys()]
    return (not raw) or all(k in {"none", "unknown", ""} for k in raw)


def cause_is_permanent(cause: Mapping[str, Any] | None) -> bool:
    if not cause:
        return False
    kinds = " ".join(str(k) for k in (cause.get("kinds") or {}).keys()).lower()
    errors = " ".join(str(e[0]) for e in (cause.get("top_errors") or []) if e).lower()
    return any(token in f"{kinds} {errors}" for token in PERMANENT_TOKENS)


def cause_is_degraded(cause: Mapping[str, Any] | None) -> bool:
    return str((cause or {}).get("status") or "").lower() in {"degraded", "partial"}


def count_quotes(audit: Mapping[str, Any], job_ids: Sequence[str]) -> int:
    if not job_ids:
        return 0
    total = 0
    for row in audit.get("sources") or []:
        artifact = str(row.get("artifact") or "")
        if any(job in artifact for job in job_ids):
            total += 1
    return total


def target_paths(job_root: Path, job: str | None) -> tuple[str, str | None]:
    """目标产物：events.json 与（若存在）context-extraction.json。"""
    if not job:
        return "（无 job，需先建任务）", None
    base = job_root / "jobs" / job
    events = ""
    ctx = None
    for candidate in sorted(base.glob("files/*/result/*/events.json")):
        if not _safe_under(candidate, job_root if job_root.is_dir() else REPO):
            continue
        events = _rel(candidate)
    for candidate in sorted(base.glob("files/*/result/context-extraction.json")):
        ctx = _rel(candidate)
    return events or f"jobs/{job}/files/<hash>/result/<date>/events.json", ctx


def rollback_path(job: str | None, date: str, out_dir: Path | None = None) -> str:
    """回滚快照路径：**必须**落在 out_dir/rollback 下（防路径穿越）。"""
    base = out_dir or (REPO / "work" / "failed-context-recovery-20261024")
    safe_job = job if job and re.fullmatch(r"job-\d{8}-\d{6}-[0-9a-f]{8}", job) else "no-job"
    target = base / "rollback" / f"{date}-{safe_job}.tar"
    return _rel(target)

def build_plan(
    audit_dir: Path = DEFAULT_AUDIT_DIR,
    *,
    states_path: Path = DEFAULT_STATES,
    job_root: Path = DEFAULT_JOB_ROOT,
    only_sessions: Iterable[str] | None = None,
    limit: int = MAX_RECOVERABLE,
    out_dir: Path | None = None,
) -> list[SessionPlan]:
    audit = load_audit(audit_dir)
    session_jobs, cause_by_job = map_jobs(audit)
    wanted = {str(s).strip() for s in (only_sessions or []) if str(s).strip()}
    plans: list[SessionPlan] = []
    # 同一 session 可能出现在多行（不同 job/重复扫描）：按段数最大者合并，只处理一次
    merged: dict[str, dict[str, Any]] = {}
    for row in audit.get("zero") or []:
        session = str(row.get("session") or "").strip()
        if not session:
            continue
        previous = merged.get(session)
        if previous is None:
            merged[session] = dict(row)
        elif int(row.get("segments") or 0) > int(previous.get("segments") or 0):
            merged[session] = dict(row)

    for row in merged.values():
        session = str(row.get("session") or "")
        if not session:
            continue
        if wanted and session not in wanted:
            continue
        date = str(row.get("date") or "").strip()
        jobs = session_jobs.get(session, [])
        job = jobs[0] if jobs else None
        cause = cause_by_job.get(job) if job else None
        kinds = ", ".join((cause or {}).get("kinds", {}).keys()) or "unknown"
        segments = int(row.get("segments") or 0)
        quotes = count_quotes(audit, jobs)
        events_path, ctx_path = target_paths(job_root, job)

        notes: list[str] = []
        if not valid_session(session) or not valid_date(date):
            classification = REJECTED
            notes.append("session/date 不合法（防路径穿越），已拒绝并跳过")
        elif human_touched(states_path, session, job):
            classification = SKIP_HUMAN
            notes.append("该会话已被人工处置（忽略/标记已处理）")
        elif not segments:
            classification = SKIP_NO_TRANSCRIPT
            notes.append("无录音分段，无内容可恢复")
        elif cause_is_transient(cause):
            classification = RECOVERABLE
        elif cause_is_degraded(cause):
            classification = TRIAGE
            notes.append("job 状态 degraded（部分成功），需人决定是否整段重跑")
        elif cause is None:
            classification = TRIAGE
            notes.append("审计里没有该 job 的失败原因记录，需人工判定")
        elif _kinds_are_blank(cause):
            classification = TRIAGE
            notes.append("整批失败但未记录错误码，重试是唯一判定手段")
        elif cause_is_permanent(cause):
            classification = SKIP_NON_TRANSIENT
            notes.append(f"失败原因为永久性错误（{kinds}），重试不会成功")
        else:
            classification = TRIAGE
            notes.append(f"失败原因 {kinds} 不是已知瞬时错误，需人决定是否重跑")

        snapshot = rollback_path(job, date, out_dir) if (valid_date(date) and valid_session(session)) else ""
        plans.append(SessionPlan(
            date=date, session=session, job=job, failure_reason=kinds,
            segments=segments, quotes=quotes, classification=classification,
            target_events=events_path, target_context=ctx_path,
            rollback_snapshot=snapshot, notes=notes,
        ))

    plans.sort(key=lambda p: (p.date, p.session, p.job or "", p.failure_reason))
    recoverable = [p for p in plans if p.recoverable]
    if len(recoverable) > limit:
        allowed = {p.session for p in recoverable[:limit]}
        for plan in plans:
            if plan.recoverable and plan.session not in allowed:
                plan.classification = "deferred_over_limit"
                plan.notes.append(f"超出本次上限（{limit}），下批处理")
    return plans


def archived_record_count(states_path: Path) -> int:
    """pending-review-states 里 `archived: 历史整批 API 失败` 的记录数（Q143 = 6,529）。"""
    payload = _read_json(states_path, {})
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, dict):
        return 0
    return sum(1 for value in records.values()
               if isinstance(value, Mapping)
               and str(value.get("note") or "").startswith("archived:"))


def summarize(plans: Sequence[SessionPlan]) -> dict[str, Any]:
    from collections import Counter
    counts = Counter(p.classification for p in plans)
    recoverable = [p for p in plans if p.recoverable]
    return {
        "sessions_total": len(plans),
        "recoverable": len(recoverable),
        "by_classification": dict(sorted(counts.items())),
        "recoverable_segments": sum(p.segments for p in recoverable),
        "recoverable_quotes": sum(p.quotes for p in recoverable),
        "dates": sorted({p.date for p in plans}),
    }


def priority_batches(plans: Sequence[SessionPlan], dates: Sequence[str]) -> list[dict[str, Any]]:
    """按日期给批次；可恢复与待人工判定分开列，避免把 triage 混进自动重跑。"""
    batches = []
    for date in dates:
        auto = [p for p in plans if p.date == date and p.recoverable]
        triage = [p for p in plans if p.date == date and p.classification == TRIAGE]
        if not auto and not triage:
            continue
        batches.append({
            "date": date,
            "auto_sessions": [p.session for p in auto],
            "triage_sessions": [p.session for p in triage],
            "auto_segments": sum(p.segments for p in auto),
            "triage_segments": sum(p.segments for p in triage),
            "auto_quotes": sum(p.quotes for p in auto),
            "triage_quotes": sum(p.quotes for p in triage),
            "jobs": [p.job for p in (auto + triage) if p.job],
        })
    return batches


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)  # 仓库外（测试/自定义 job_root）保留绝对路径，校验交给 caller


def make_rollback_snapshot(plan: SessionPlan, out_dir: Path, job_root: Path | None = None) -> dict[str, Any]:
    """真实生成并校验回滚 tar（apply 前必须先做）。

    校验三件事：tar 可被重新打开、成员非空、每个成员 size > 0。
    目标路径必须落在 out_dir/rollback 下，否则拒绝（防路径穿越）。
    """
    if not plan.rollback_snapshot:
        raise SnapshotError(f"{plan.session}: 没有合法的回滚路径")
    target = Path(plan.rollback_snapshot)
    if not _safe_under(target, out_dir / "rollback"):
        raise SnapshotError(f"{plan.session}: 回滚路径越界 —— {target}")
    members: list[Path] = []
    for candidate in (plan.target_events, plan.target_context):
        if not candidate:
            continue
        path = Path(candidate)
        if not path.is_absolute():
            candidate_repo = REPO / path
            path = candidate_repo if candidate_repo.exists() or not job_root else (job_root / path)
        if not path.is_file():
            continue
        # 产物只允许来自 job_root 或仓库内；其它位置一律拒绝（防被 fixture 带出仓库）
        allowed = [REPO] + ([job_root] if job_root else [])
        if not any(_safe_under(path, parent) for parent in allowed):
            raise SnapshotError(f"{plan.session}: 目标产物越界 —— {path}")
        members.append(path)
    if not members:
        raise SnapshotError(f"{plan.session}: 找不到可快照的目标产物")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(target, "w:gz") as archive:
            for path in members:
                if path.is_relative_to(REPO):
                    arcname = str(path.relative_to(REPO))
                elif job_root and path.is_relative_to(job_root):
                    arcname = str(path.relative_to(job_root))
                else:
                    arcname = path.name
                archive.add(path, arcname=arcname)
        with tarfile.open(target, "r:gz") as archive:
            entries = archive.getmembers()
            if not entries:
                raise SnapshotError(f"{plan.session}: tar 内没有任何成员")
            if any(member.size <= 0 for member in entries if member.isfile()):
                raise SnapshotError(f"{plan.session}: tar 内含空文件")
            names = [member.name for member in entries]
    except (OSError, tarfile.TarError, ValueError) as error:
        raise SnapshotError(f"{plan.session}: 生成快照失败 —— {error}") from error
    return {"path": str(target.relative_to(REPO)) if target.is_relative_to(REPO) else str(target),
            "bytes": target.stat().st_size, "members": names}


def render_markdown(plans: Sequence[SessionPlan], summary: Mapping[str, Any],
                    batches: Sequence[Mapping[str, Any]], audit_dir: Path) -> str:
    lines = [
        "# 失败会话选择性恢复 · 清单（dry-run）",
        "",
        f"审计数据：`{_rel(audit_dir)}`",
        f"生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        "",
        "**未调用任何模型、未改生产事件/音频。**",
        "",
        "## 汇总",
        "",
        f"- 会话总数：**{summary['sessions_total']}**，可恢复：**{summary['recoverable']}**",
        f"- 可恢复分段：**{summary['recoverable_segments']}**，可恢复 quote：**{summary['recoverable_quotes']}**",
        f"- 分类：{summary['by_classification']}",
        f"- **口径区分**：状态文件里 `archived: 历史整批 API 失败` 共 **{summary.get('archived_records', 0)}** 条记录；"
        f"这 13 个会话实际能追到 source quote 的是 **{summary.get('matched_quotes_total', 0)}** 条（差额来自无 job / 无 quote 的归档项）。",
        "",
        "## 逐会话清单（按日期/会话/job/原因排序）",
        "",
        "| 日期 | 会话 | job | 失败原因 | 段 | quote | 分类 |",
        "|---|---|---|---|---:|---:|---|",
    ]
    for plan in plans:
        lines.append(
            f"| {plan.date} | `{plan.session[:12]}…` | `{plan.job or '—'}` | {plan.failure_reason} | "
            f"{plan.segments} | {plan.quotes} | {plan.classification} |"
        )
    lines += ["", "## 优先批次", ""]
    if not batches:
        lines.append("（无可恢复批次）")
    for batch in batches:
        lines.append(
            f"- **{batch['date']}**：自动可恢复 {len(batch['auto_sessions'])} 个会话 / "
            f"{batch['auto_segments']} 段 / {batch['auto_quotes']} quote；"
            f"待人工判定 {len(batch['triage_sessions'])} 个 / {batch['triage_segments']} 段"
        )
    lines += [
        "",
        "## 每个可恢复会话的目标与回滚",
        "",
        "| 日期 | job | 目标产物 | 回滚快照 |",
        "|---|---|---|---|",
    ]
    for plan in plans:
        if plan.recoverable:
            lines.append(f"| {plan.date} | `{plan.job}` | `{plan.target_events}` | `{plan.rollback_snapshot}` |")
    lines += [
        "",
        "## 安全边界",
        "",
        "- 默认 dry-run；`--apply` 必须同时给 `--session` 与 `--confirm`。",
        "- 人工忽略（`用户忽略` / `用户标记已处理`）一律跳过。",
        f"- 只处理 failed_archived + 瞬时错误；并发上限 {MAX_CONCURRENCY}，条数上限 {MAX_RECOVERABLE}。",
        "- 本版本 `--apply` 只生成 runbook，不调用模型。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-dir", type=Path, default=DEFAULT_AUDIT_DIR)
    parser.add_argument("--states", type=Path, default=DEFAULT_STATES)
    parser.add_argument("--job-root", type=Path, default=DEFAULT_JOB_ROOT)
    parser.add_argument("--session", action="append", default=[],
                        help="只处理指定会话（可重复）")
    parser.add_argument("--limit", type=int, default=MAX_RECOVERABLE)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--apply", action="store_true", help="生成执行 runbook（不调用模型）")
    parser.add_argument("--confirm", action="store_true", help="与 --apply 同时使用才生效")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out-dir", type=Path,
                        default=REPO / "work" / "failed-context-recovery-20261024")
    args = parser.parse_args(argv)

    limit = max(1, min(int(args.limit), MAX_RECOVERABLE))
    concurrency = max(1, min(int(args.concurrency), MAX_CONCURRENCY))
    sessions = [s.strip() for s in args.session if str(s).strip()]

    if args.apply:
        if not sessions or not args.confirm:
            print("拒绝执行 --apply：必须同时指定至少一个非空 --session 和 --confirm；"
                  "本工具不允许批量自动重跑。", file=sys.stderr)
            return 2
    plans = build_plan(args.audit_dir, states_path=args.states, job_root=args.job_root,
                       only_sessions=sessions or None, limit=limit, out_dir=args.out_dir)
    summary = summarize(plans)
    summary["archived_records"] = archived_record_count(args.states)
    summary["matched_quotes_total"] = sum(p.quotes for p in plans)
    batches = priority_batches(plans, ["2026-10-04", "2026-10-07", "2026-10-15", "2026-10-16"])

    payload = {
        "schema_version": "failed-context-recovery-v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dry_run": not args.apply,
        "concurrency": concurrency,
        "limit": limit,
        "summary": summary,
        "priority_batches": batches,
        "sessions": [asdict(p) for p in plans],
        "safety": {
            "skips_human_ignored": True,
            "only_transient_and_archived": True,
            "max_concurrency": MAX_CONCURRENCY,
            "max_recoverable": MAX_RECOVERABLE,
            "calls_model": False,
        },
    }

    if args.apply:
        selected = [p for p in plans if p.recoverable]
        if not selected:
            print("拒绝执行 --apply：指定会话里没有「可恢复」项（人工忽略/非瞬时错误/无内容都被过滤掉了）。",
                  file=sys.stderr)
            return 2
        snapshots: dict[str, dict[str, Any]] = {}
        try:
            for plan in selected:
                snapshots[plan.session] = make_rollback_snapshot(plan, args.out_dir, args.job_root)
        except SnapshotError as error:
            print(f"拒绝执行 --apply：回滚快照校验失败 —— {error}", file=sys.stderr)
            return 4
        payload["snapshots"] = snapshots
        runbook = ["# 恢复 runbook（未执行模型）", ""]
        for plan in selected:
            runbook += [
                f"## {plan.date} · {plan.session} · {plan.job}",
                f"- 预计段数：{plan.segments}；quote：{plan.quotes}",
                f"- 回滚快照：`{snapshots[plan.session]['path']}`"
                f"（{snapshots[plan.session]['bytes']} B，{len(snapshots[plan.session]['members'])} 个成员，已校验）",
                "```bash",
                "# 1) 快照当前产物",
                f"mkdir -p $(dirname {plan.rollback_snapshot})",
                f"tar -czf {plan.rollback_snapshot} {plan.target_events} 2>/dev/null || true",
                "# 2) 重跑该会话的抽取（需人工确认后执行）",
                f"python3 scripts/rerun_context_extraction.py --job {plan.job} --confirm",
                "```",
                "",
            ]
        run_dir = args.out_dir / "runbook"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "RUNBOOK.md").write_text("\n".join(runbook), encoding="utf-8")
        payload["runbook"] = _rel(run_dir / "RUNBOOK.md")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "plan.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not args.apply:
        (args.out_dir / "PLAN.md").write_text(
            render_markdown(plans, summary, batches, args.audit_dir), encoding="utf-8")

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"dry_run": payload["dry_run"], "summary": summary,
                          "priority_batches": batches,
                          "outputs": [_rel(args.out_dir / n)
                                      for n in (("plan.json", "runbook/RUNBOOK.md") if args.apply else ("plan.json", "PLAN.md"))]},
                         ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
