"""读模型输入的清单与指纹（SQLite 读模型已于 2026-11-04 退役）。

只保留 `source_fingerprint` / `SOURCE_PATTERNS` / `GLOBAL_FILES`：
它们仍是区间账本缓存失效（`local_web._expense_range_signature`）的唯一输入清单，
与已删除的 SQLite 读模型 / 投影层无关。
"""
from __future__ import annotations

from datetime import date as Date, timedelta
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable

#: 【2026-10-24 F2-S1】读模型输入的**唯一清单**：任何会改变事件/账本/日报的产物都要在这里出现一次。
#: 此前 `local_web._expense_range_signature` 只列了 3 条 glob，漏掉手工补录 / 摘要 /
#: 复核派生 / 复核决定 / 播报确认等输入，导致区间账本缓存不失效。
SOURCE_PATTERNS: tuple[str, ...] = (
    'jobs/job-*/files/*/result/*/daily_summary.json',
    'jobs/job-*/files/*/result/*/events.json',
    'jobs/job-*/files/*/result/*/self-transcript.json',
    'jobs/job-*/status.json', 'jobs/job-*/manual-events.json',
    'jobs/job-*/event-corrections.json', 'jobs/job-*/review-derived-events.json',
    'jobs/job-*/review-decisions.json', 'jobs/job-*/context-relation-reviews.json',
    'watch-sessions/*/*/events.json', 'watch-sessions/*/*/manifest.json',
    'watch-sessions/*/*/segments/*/meta.json',
    'daily-synthesis/synthesis-*.json', 'broadcast-decisions/*.json',
    'task-states.json', 'preferences.json', 'ledger-review.json',
)

#: 全局输入：影响所有日期的读模型。
GLOBAL_FILES: frozenset[str] = frozenset({'task-states.json', 'preferences.json', 'ledger-review.json', '@config', '@build'})

_DATE_IN_PATH = re.compile(r'/(\d{4}-\d{2}-\d{2})/')
_DATE_IN_NAME = re.compile(r'(\d{4}-\d{2}-\d{2})')


def _path_dates(relative: str) -> set[str]:
    """从相对路径取日期：优先 `…/<date>/…`，否则看文件名（synthesis-<date>.json 等）。"""
    dates = set(_DATE_IN_PATH.findall('/' + relative))
    if not dates:
        dates = set(_DATE_IN_NAME.findall(Path(relative).name))
    return dates


def _session_overlaps_dates(root: Path, relative: str, wanted: set[str]) -> bool:
    """watch 会话是否覆盖指定日期；manifest 缺日期时**保守包含**（宁多失效不误留旧值）。"""
    manifest = root / '/'.join(relative.split('/')[:3]) / 'manifest.json'
    try:
        payload = json.loads(manifest.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return True
    if not isinstance(payload, dict):
        return True
    found = {m for value in (payload.get('started_at'), payload.get('ended_at'), payload.get('date'))
             if value for m in _DATE_IN_NAME.findall(str(value))}
    if not found:
        return True
    # 按会话日期查集合，不按 wanted 循环：搜索一次会传 36525 天。
    for stamp in found:
        try:
            other = Date.fromisoformat(stamp)
        except ValueError:
            continue
        for offset in (-1, 0, 1):
            if (other + timedelta(days=offset)).isoformat() in wanted:
                return True
    return False


def _has_invalid_day(wanted: set[str]) -> bool:
    for day in wanted:
        try:
            Date.fromisoformat(day)
        except ValueError:
            return True
    return False


def source_fingerprint(root: Path | str, dates: Iterable[str] | None = None) -> str:
    """读模型输入指纹（F2-S1）：返回 16 位 hex，**不含** date_from/date_to/scope/view。"""
    root = Path(root)
    wanted = {str(day) for day in (dates or []) if str(day).strip()} or None
    job_dates: set[str] = set()
    if wanted is not None:
        for hit in root.glob('jobs/*/files/*/result/*'):
            parts = hit.parts  # …/jobs/<job>/files/<hash>/result/<date>
            # parts[-1]=<date> [-2]='result' [-3]=<hash> [-4]='files' [-5]=<job>
            if hit.name in wanted and len(parts) >= 5:
                job_dates.add(parts[-5])
        # 只有手动补录的 job 没有 files/*/result/<date>，日期产物在 jobs/<job>/<date>/。
        # 2026-11-06：11-05 补录红塔山 10 元后区间账本仍命中旧缓存（36.5 vs 46.5）。
        for hit in root.glob('jobs/*/*'):
            if hit.name in wanted and hit.is_dir():
                job_dates.add(hit.parent.name)
    include_all_sessions = wanted is not None and _has_invalid_day(wanted)
    entries: list[str] = []
    for pattern in SOURCE_PATTERNS:
        for path in root.glob(pattern):
            try:
                stat = path.stat()
            except OSError:
                continue
            relative = path.relative_to(root).as_posix()
            if wanted is not None:
                path_days = _path_dates(relative)
                if path_days:
                    if not (path_days & wanted):
                        continue
                elif relative.startswith('jobs/'):
                    if relative.split('/')[1] not in job_dates:
                        continue
                elif relative.startswith('watch-sessions/'):
                    if not include_all_sessions and not _session_overlaps_dates(root, relative, wanted):
                        continue
            entries.append(f'{relative}:{stat.st_mtime_ns}:{stat.st_size}')
    entries.sort()
    return hashlib.sha256('|'.join(entries).encode('utf-8')).hexdigest()[:16]
