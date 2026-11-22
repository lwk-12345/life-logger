"""Range statistics derived from durable Life Logger daily outputs."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Callable, Mapping
from zoneinfo import ZoneInfo

from life_logger.event_view import build_event_view, repair_event_time
from life_logger.timecode import parse_timestamp_from_name

STAT_CATEGORIES = (
    "routine",
    "food",
    "expense",
    "exercise",
    "work_communication",
    "idea",
    "todo",
)
_ROUTINE_BUCKETS = (
    ("00-05", 0, 6),
    ("06-11", 6, 12),
    ("12-17", 12, 18),
    ("18-23", 18, 24),
)
SHANGHAI = ZoneInfo("Asia/Shanghai")
# 任务级 status 出现 partial 表示"任务跑完了但有文件被判失败"；它和 failed 一样
# 属于"没有真正处理成功"的桶，统计里必须单独露出来，不能混进 completed。
# degraded 是"产物可用、但有子步骤失败没被解决"，既不算干净完成也不算没处理，
# 单独一桶，别让它把 completed 的信任度拉满。
_UNPROCESSED_JOB_STATUSES = ("partial", "failed", "interrupted", "draft")
_DEGRADED_JOB_STATUSES = ("degraded",)
# 还有未解决 stage_errors 却自称 completed 的任务：历史遗留的假成功。
_FALSE_SUCCESS_STATUSES = ("completed",)


def _unresolved_stage_errors(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """列出 status.json 里未解决的结构化失败（stage_errors）。"""
    errors = state.get("stage_errors")
    if not isinstance(errors, list):
        return []
    return [
        item
        for item in errors
        if isinstance(item, Mapping) and not item.get("resolved")
    ]


def _job_day(state: Mapping[str, Any]) -> str:
    for key in ("created_at", "manual_start_at", "completed_at"):
        value = str(state.get(key) or "")
        if len(value) >= 10 and value[4] == "-" and value[7] == "-":
            return value[:10]
    return ""


def _processing_outcomes(job_root: Path, start: str, end: str) -> dict[str, Any]:
    """按日期范围汇总"文件到底处理成功没有"，把失败从假完成里剥出来。"""
    jobs_dir = Path(job_root) / "jobs"
    job_status_counts: Counter[str] = Counter()
    failed_files: list[dict[str, Any]] = []
    stage_errors: list[dict[str, Any]] = []
    processed_files = 0
    for path in sorted(jobs_dir.glob("job-*/status.json")):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(state, dict):
            continue
        day = _job_day(state)
        if not start <= day <= end:
            continue
        status = str(state.get("status") or "unknown")
        job_status_counts[status] += 1
        unresolved = _unresolved_stage_errors(state)
        if unresolved:
            stage_errors.append(
                {
                    "job_id": str(state.get("job_id") or path.parent.name),
                    "date": day,
                    "status": status,
                    "count": len(unresolved),
                    "reasons": [
                        {
                            "stage": item.get("stage"),
                            "kind": item.get("kind"),
                            "severity": item.get("severity"),
                            "error": item.get("error"),
                        }
                        for item in unresolved
                    ],
                }
            )
        for item in state.get("files") or []:
            if item.get("status") == "completed":
                processed_files += 1
            elif item.get("status") == "failed":
                failed_files.append(
                    {
                        "job_id": str(state.get("job_id") or path.parent.name),
                        "date": day,
                        "name": item.get("original_name"),
                        "stage": next(
                            (
                                name
                                for name, value in (item.get("stages") or {}).items()
                                if value.get("status") == "failed"
                            ),
                            item.get("current_stage"),
                        ),
                        "error": item.get("error"),
                    }
                )
    failed_jobs = sum(
        job_status_counts.get(status, 0) for status in _UNPROCESSED_JOB_STATUSES
    )
    degraded_jobs = sum(
        job_status_counts.get(status, 0) for status in _DEGRADED_JOB_STATUSES
    )
    false_success_jobs = [
        row
        for row in stage_errors
        if row["status"] in _FALSE_SUCCESS_STATUSES
    ]
    return {
        "job_count": sum(job_status_counts.values()),
        "completed_job_count": job_status_counts.get("completed", 0),
        "degraded_job_count": degraded_jobs,
        "degraded_jobs": [
            {"job_id": row["job_id"], "date": row["date"]}
            for row in stage_errors
            if row["status"] in _DEGRADED_JOB_STATUSES
        ],
        # 自称 completed 却还有未解决的子步骤失败：这就是「假成功」。
        "false_success_job_count": len(false_success_jobs),
        "false_success_jobs": false_success_jobs,
        "unprocessed_job_count": failed_jobs,
        "job_status_counts": dict(job_status_counts),
        "processed_file_count": processed_files,
        "failed_file_count": len(failed_files),
        "failed_files": failed_files,
    }


def _validate_date(value: str, label: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError as error:
        raise ValueError(f"{label}格式无效") from error
    return parsed.date().isoformat()


def _summary_paths(job_root: Path) -> Iterable[Path]:
    yield from sorted(
        Path(job_root).glob("jobs/job-*/files/*/result/*/daily_summary.json")
    )


def _event_key(event: Mapping[str, Any]) -> str:
    event_id = str(event.get("event_id") or "").strip()
    if event_id:
        return event_id
    time_info = event.get("time") if isinstance(event.get("time"), dict) else {}
    return "|".join(
        (
            str(event.get("title") or ""),
            str(time_info.get("absolute") or time_info.get("relative_seconds") or ""),
            ",".join(str(item) for item in event.get("categories", [])),
        )
    )


def _parse_event_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _event_datetime(event: Mapping[str, Any]) -> datetime | None:
    time_info = event.get("time")
    if not isinstance(time_info, Mapping):
        return None
    return _parse_event_datetime(time_info.get("absolute"))


def _summary_source_start(path: Path) -> datetime | None:
    try:
        file_id = path.parents[2].name
        status_path = path.parents[4] / "status.json"
    except IndexError:
        return None
    if not status_path.is_file():
        return None
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    files = status.get("files") if isinstance(status, Mapping) else None
    if not isinstance(files, list):
        return None
    source = next(
        (
            item
            for item in files
            if isinstance(item, Mapping) and str(item.get("file_id") or "") == file_id
        ),
        None,
    )
    if source is None:
        return None
    for key in ("original_name", "relative_path"):
        name = str(source.get(key) or "").strip()
        if not name:
            continue
        parsed = parse_timestamp_from_name(Path(name))
        if parsed is not None:
            return parsed.replace(tzinfo=SHANGHAI)
    for key in ("effective_start_at", "inferred_start_at"):
        parsed = _parse_event_datetime(source.get(key))
        if parsed is not None:
            return parsed
    return None


def _repair_event_time(
    event: Mapping[str, Any],
    source_start: datetime | None,
) -> dict[str, Any]:
    """Compatibility wrapper around the shared, non-destructive time repair.

    Valid occurrence timestamps are preserved; only missing/legacy timestamps
    are repaired when the source offset makes that safe.
    """

    return repair_event_time(event, source_start)


def _event_date(event: Mapping[str, Any], fallback: str) -> str:
    parsed = _event_datetime(event)
    return parsed.date().isoformat() if parsed is not None else fallback


def _raw_expense_amount(event: Mapping[str, Any]) -> float | None:
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    expense = data.get("expense")
    if not isinstance(expense, dict):
        return None
    amount = expense.get("amount")
    if isinstance(amount, (int, float)):
        return float(amount)
    return None


def _expense_amount(event: Mapping[str, Any]) -> float:
    data = event.get("data")
    if not isinstance(data, dict):
        return 0.0
    expense = data.get("expense")
    if not isinstance(expense, dict):
        return 0.0
    amount = expense.get("amount")
    if isinstance(amount, (int, float)):
        return float(amount)
    return 0.0


def _routine_hour(event: Mapping[str, Any]) -> int | None:
    parsed = _event_datetime(event)
    return parsed.hour if parsed is not None else None


def _activity_ledger_days(job_root: Path, start: str, end: str) -> list[str]:
    """范围内哪几天有活动层产物（`activity-timeline/jev/<day>.json`）。

    [2026-11-07] 只有真有活动文件时才切到「按天账本」路径：
    纯内存 events 的旧单元测试（临时目录里没有活动产物）行为完全不变。
    """
    from life_logger.day_facts import data_root

    base = data_root(Path(job_root)) / "activity-timeline" / "jev"
    days: list[str] = []
    current = datetime.fromisoformat(start)
    last = datetime.fromisoformat(end)
    while current <= last:
        day = current.date().isoformat()
        try:
            if (base / f"{day}.json").is_file():
                days.append(day)
        except OSError:
            pass
        current += timedelta(days=1)
    return days


def build_range_statistics(
    job_root: Path,
    *,
    date_from: str,
    date_to: str,
    scope: str = "provisional",
    view: str = "all",
    event_filter: Callable[[Any], list[dict[str, Any]]] | None = None,
    use_scan_cache: bool = False,
) -> dict[str, Any]:
    """Aggregate canonical generated events for a date range.

    The default ``scope="provisional"`` reads the formal artifacts plus live
    Watch events with exact cross-source duplicates represented once. The
    default ``view="all"`` keeps the historical statistics behaviour of using
    the raw timeline rather than ``display_timeline``. Callers can request
    ``scope="settled"`` or ``view="display"`` explicitly.

    【2026-10-17】新增 ``event_filter``：``local_web`` 需要让「隐藏电脑前说话」
    的读层过滤同样作用于统计数字，否则会出现「日记页少了 N 条、统计页还是全量」
    这种两套口径打架的情况（实测 /api/daily 与 /api/stats 对不上）。
    过滤器只作用于统计读数，产物文件一律不动。
    """

    start = _validate_date(date_from, "开始日期")
    end = _validate_date(date_to, "结束日期")
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")

    event_view = build_event_view(
        Path(job_root),
        date_from=start,
        date_to=end,
        scope=scope,
        view=view,
        use_cache=use_scan_cache,
    )
    events: list[dict[str, Any]] = [
        dict(event) for event in event_view["events"]
    ]
    prefilter_event_count = len(events)
    if event_filter is not None:
        filtered = event_filter(events)
        if not isinstance(filtered, list):
            raise TypeError("event_filter must return a list of events")
        events = [dict(e) for e in filtered if isinstance(e, Mapping)]

    # [2026-11-08] 有活动层产物的日期：消费与健身数都改由「按天账本」给，
    # 事件循环里就不再重复数这些日期，避免双计。
    ledger_days = _activity_ledger_days(job_root, start, end)
    ledger_day_set = set(ledger_days)

    category_counts = {category: 0 for category in STAT_CATEGORIES}
    routine_distribution = {label: 0 for label, _start, _end in _ROUTINE_BUCKETS}
    hourly_distribution = [0] * 24
    daily_counts: Counter[str] = Counter()
    exercise_count = 0
    event_day_by_id: dict[str, str] = {}

    for event in events:
        categories = [str(item) for item in event.get("categories", [])]
        day = str(event.get("effective_date") or start)
        daily_counts[day] += 1
        event_id = str(event.get("event_id") or "")
        if event_id:
            event_day_by_id[event_id] = day
        hour = _routine_hour(event)
        if hour is not None and 0 <= hour < 24:
            hourly_distribution[hour] += 1
        for category in STAT_CATEGORIES:
            if category in categories:
                category_counts[category] += 1
        if "exercise" in categories and day not in ledger_day_set:
            exercise_count += 1
        if "routine" in categories:
            if hour is not None:
                for label, bucket_start, bucket_end in _ROUTINE_BUCKETS:
                    if bucket_start <= hour < bucket_end:
                        routine_distribution[label] += 1
                        break

    # Use the ledger worker's single accounting formula lazily. This module
    # must not maintain a second, divergent money formula.
    from life_logger.day_facts import day_expense_ledger, event_only_expense_ledger

    # [2026-11-07 唯一事实来源] /api/stats 原来只算事件轨，漏掉活动层消费
    # （10-27~11-06 共少 756 元）。活动产物存在时，按天改用 day_expense_ledger
    # ——与 /api/daily 的 expense_ledger 完全同源。
    expense_ledger = event_only_expense_ledger(events)
    day_ledgers: dict[str, dict[str, Any]] = {}
    day_events_by_day: dict[str, list[dict[str, Any]]] = {}
    if ledger_days:
        for day in ledger_days:
            day_events = [
                event for event in events
                if str(event.get("effective_date") or _event_date(event, start)) == day
            ]
            day_events_by_day[day] = day_events
            day_ledgers[day] = day_expense_ledger(day_events, day, root=job_root)
    currency_totals: dict[str, float] = {}
    currency_counts: dict[str, int] = {}
    daily_expenses: Counter[str] = Counter()
    raw_amount_by_event = {
        str(event.get("event_id") or ""): _raw_expense_amount(event)
        for event in events
        if event.get("event_id")
    }
    unknown_amount_count = int(expense_ledger.get("pending_count") or 0)
    transactions = [
        transaction for transaction in expense_ledger.get("transactions", [])
        if isinstance(transaction, Mapping)
        and transaction.get("kind", "consumption") in {"consumption", "transfer"}
    ]
    if not isinstance(transactions, list):
        transactions = []
    for transaction in transactions:
        if not isinstance(transaction, Mapping):
            continue
        currency = str(transaction.get("currency") or "CNY").strip() or "CNY"
        currency_counts[currency] = currency_counts.get(currency, 0) + 1
        amount = transaction.get("amount")
        numeric_amount = float(amount) if isinstance(amount, (int, float)) else None
        if numeric_amount is not None:
            currency_totals[currency] = round(
                currency_totals.get(currency, 0.0) + numeric_amount, 2
            )
        event_id = str(transaction.get("event_id") or "")
        if numeric_amount == 0.0 and raw_amount_by_event.get(event_id) is None:
            # The ledger closes a missing amount at 0.0. Keep that visible as
            # unknown instead of silently treating it as a confirmed zero.
            unknown_amount_count += 1
        if currency == "CNY" and event_id and event_id in event_day_by_id:
            if numeric_amount is not None and not day_ledgers:
                daily_expenses[event_day_by_id[event_id]] += numeric_amount

    # 按天账本作为唯一口径：金额、笔数、按天趋势都从它来。
    if day_ledgers:
        currency_totals = {}
        currency_counts = {}
        transactions = []
        unknown_amount_count = 0
        daily_expenses = Counter()
        for day, ledger in day_ledgers.items():
            for transaction in ledger.get("transactions", []):
                if not isinstance(transaction, Mapping):
                    continue
                if transaction.get("kind", "consumption") not in {"consumption", "transfer"}:
                    continue
                transactions.append(transaction)
                currency = str(transaction.get("currency") or "CNY").strip() or "CNY"
                currency_counts[currency] = currency_counts.get(currency, 0) + 1
                amount = transaction.get("amount")
                numeric_amount = float(amount) if isinstance(amount, (int, float)) else None
                if numeric_amount is not None:
                    currency_totals[currency] = round(
                        currency_totals.get(currency, 0.0) + numeric_amount, 2
                    )
                if (numeric_amount == 0.0
                        and raw_amount_by_event.get(str(transaction.get("event_id") or "")) is None):
                    unknown_amount_count += 1
                if currency == "CNY" and numeric_amount is not None:
                    daily_expenses[day] += numeric_amount
            unknown_amount_count += int(ledger.get("pending_count") or 0)

    # [2026-11-08 唯一事实来源] 健身训练数同样改由 domain_ledgers 给
    # （与 /api/daily 的 fitness_ledger.workout_count 完全同源）；
    # 没有活动层产物的日期仍在上面按 events 计数，行为不变。
    if day_ledgers:
        from life_logger.day_facts import domain_ledgers

        exercise_count += sum(
            int((domain_ledgers(day_events_by_day[day], day, root=job_root, only={"fitness"})["fitness"]
                 .get("workout_count") or 0))
            for day in ledger_days
        )

    metadata = event_view.get("metadata", {})
    expense_total_cny = round(currency_totals.get("CNY", 0.0), 2)
    selected_counts = {
        "event_count": len(events),
        "prefilter_event_count": prefilter_event_count,
        "event_filter_applied": event_filter is not None,
        "date_basis_counts": dict(
            Counter(str(event.get("date_basis") or "unknown") for event in events)
        ),
        "source_kind_counts": dict(
            Counter(
                str(
                    event.get("canonical_source_kind")
                    or event.get("source_kind")
                    or "unknown"
                )
                for event in events
            )
        ),
    }

    return {
        "schema_version": "statistics-v1",
        "date_from": start,
        "date_to": end,
        "scope": scope,
        "view": view,
        "accounting_scope": scope,
        "days_with_data": len(daily_counts),
        "event_count": len(events),
        "event_count_basis": {
            "scope": scope,
            "view": view,
            "prefilter_event_count": prefilter_event_count,
            "event_filter_applied": event_filter is not None,
            "date_basis_counts": selected_counts["date_basis_counts"],
            "source_kind_counts": selected_counts["source_kind_counts"],
        },
        "selected_counts": selected_counts,
        "event_view": metadata,
        # Legacy field: CNY only. Other currencies are exposed separately so a
        # USD amount is never silently added to a CNY total.
        "expense_total_cny": expense_total_cny,
        "expense_total_cny_basis": "build_expense_ledger; CNY transactions only",
        "expense_currency_totals": dict(sorted(currency_totals.items())),
        "expense_currency_counts": dict(sorted(currency_counts.items())),
        "expense_transaction_count": len(transactions),
        "expense_unknown_amount_count": unknown_amount_count,
        "exercise_count": exercise_count,
        "category_counts": category_counts,
        "routine_distribution": routine_distribution,
        "hourly_distribution": hourly_distribution,
        "daily_event_counts": [
            {"date": day, "count": daily_counts[day]}
            for day in sorted(daily_counts)
        ],
        # 【2026-10-19】口径标注（避免下一个人再误解）：
        # `daily_expenses` 是**事件/交易口径**——由 expense_ledger.transactions 按事件日期
        # 逐笔累加（numeric_amount），并且包含 kind="transfer" 的转账类交易；
        # 它**不是**当期账本的 `total_amount`（后者只算 consumption、另有聚合对账与排除规则），
        # 所以两者在"含转账/含未知金额"的日子里可能不等。
        # 用途：统计页的按天柱状趋势（诊断/趋势观察）。
        # 需要"这天到底花了多少钱"的权威数字，请用 `/api/expense-range` 的
        # `by_day[].total_cny`（event_view + expense_policy，与 /api/daily 同源）。
        "daily_expenses": [
            {"date": day, "amount": round(daily_expenses.get(day, 0.0), 2)}
            for day in sorted(set(daily_counts) | set(daily_expenses))
        ],
        "source_daily_summaries": metadata.get("sources", {}).get(
            "formal_summaries", []
        ),
        "processing": _processing_outcomes(Path(job_root), start, end),
    }
