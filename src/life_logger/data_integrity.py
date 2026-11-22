"""Read-only operational consistency checks, separate from JSON/reference validation."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from life_logger.jsonio import glob_json, read_json
from life_logger.pending_review import collect_pending_sources


def _object(path: Path) -> dict[str, Any]:
    # [2026-11-05] 中间 JSON 可能是 `x.json.gz`（未压缩优先）。
    try:
        value = read_json(path)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def _timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        # Persisted server metadata should have a zone. Avoid inventing one.
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def operational_issues(root: Path, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Report actionable storage conditions without declaring sparse audio a loss.

    A receiving session is stale only after 24h without receipts/updates. Root
    empty arrays in Jev/Clef comparison mode are deliberately not an error.
    """
    root = Path(root)
    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(hours=24)
    issues: list[dict[str, Any]] = []

    for path in glob_json(root, 'watch-sessions/*/*/manifest.json'):
        manifest = _object(path)
        if manifest.get('state') != 'receiving':
            continue
        timestamps = [_timestamp(manifest.get(key)) for key in ('updated_at', 'started_at')]
        for segment in manifest.get('segments') or []:
            if isinstance(segment, dict):
                timestamps.append(_timestamp(segment.get('received_at')))
        latest = max((stamp for stamp in timestamps if stamp), default=None)
        if latest and latest < cutoff:
            issues.append({'kind': 'stale_receiving', 'severity': 'warning',
                           'file': str(path.relative_to(root)),
                           'session_id': manifest.get('session_id') or path.parent.name,
                           'last_activity_at': latest.isoformat(),
                           'detail': '超过24小时未收到分段或会话更新；请核对结束确认，不自动关闭。'})

    job_states: Counter[str] = Counter()
    for path in glob_json(root, 'jobs/job-*/status.json'):
        status = _object(path)
        state = str(status.get('status') or '')
        job_states[state] += 1
        latest = _timestamp(status.get('updated_at')) or _timestamp(status.get('created_at'))
        if state == 'draft' and latest and latest < cutoff:
            issues.append({'kind': 'stale_draft', 'severity': 'warning',
                           'file': str(path.relative_to(root)), 'job_id': path.parent.name,
                           'detail': '草稿任务超过24小时，尚未开始正式处理；保留原任务。'})
    unsettled = {key: job_states[key] for key in ('failed', 'degraded', 'partial') if job_states[key]}
    if unsettled:
        issues.append({'kind': 'incomplete_jobs', 'severity': 'warning',
                       'counts': unsettled, 'detail': '存在失败或部分完成任务，请检查各阶段结果。'})

    undated = glob_json(root, 'jobs/job-*/files/*/result/undated/daily_summary.json')
    if undated:
        issues.append({'kind': 'undated_summaries', 'severity': 'warning',
                       'count': len(undated),
                       'empty_count': sum(not _object(path).get('timeline') for path in undated),
                       'detail': '存在未确定日期的历史摘要；不将其自动分配到今天。'})

    pending = collect_pending_sources(root)
    totals = pending.get('totals') or {}
    actionable = int(totals.get('open_actionable') or 0)
    gaps = int(totals.get('open_extraction_gap') or 0)
    if actionable or gaps:
        issues.append({'kind': 'open_review_sources', 'severity': 'warning',
                       'needs_review': actionable, 'extraction_gap': gaps,
                       'detail': '需要人工判断与抽取异常分别计数，整批失败不计入人工主徽章。'})
    if totals.get('undated'):
        issues.append({'kind': 'undated_sources', 'severity': 'warning',
                       'count': totals['undated'], 'detail': '来源日期未知，需检查转写与来源映射。'})
    if totals.get('open_batch_failure'):
        issues.append({'kind': 'batch_extraction_failures', 'severity': 'warning',
                       'count': totals['open_batch_failure'],
                       'detail': '历史整批抽取失败来源，独立展示，不视为全部需要人工逐条判断。'})
    return issues
