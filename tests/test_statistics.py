import json
import sqlite3
from pathlib import Path

from life_logger.statistics import build_range_statistics


def _event(
    event_id: str,
    category: str,
    title: str,
    timestamp: str,
    *,
    amount: float | None = None,
) -> dict:
    data = {category: {}}
    if category == "expense":
        data["expense"] = {
            "amount": amount,
            "currency": "CNY",
            "item": title,
        }
    return {
        "event_id": event_id,
        "categories": [category],
        "title": title,
        "time": {
            "absolute": timestamp,
            "relative_seconds": 0,
            "precision": "exact",
        },
        "data": data,
        "confidence": 0.9,
        "evidence": {"quote": title},
    }


def _write_day(
    job_root: Path,
    *,
    job_id: str,
    digest: str,
    day: str,
    events: list[dict],
) -> Path:
    day_dir = job_root / "jobs" / job_id / "files" / digest / "result" / day
    day_dir.mkdir(parents=True)
    summary = {
        "schema_version": "0.1",
        "date": day,
        "timezone": "Asia/Shanghai",
        "event_count": len(events),
        "timeline": events,
        "category_summary": {},
        "warnings": [],
    }
    (day_dir / "daily_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False),
        encoding="utf-8",
    )
    database = day_dir.parent / "life_logger.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "create table if not exists events "
            "(event_id text primary key, payload_json text not null)"
        )
        connection.executemany(
            "insert or replace into events values (?, ?)",
            [
                (event["event_id"], json.dumps(event, ensure_ascii=False))
                for event in events
            ],
        )
    return database


def test_range_statistics_match_daily_and_sqlite_totals(tmp_path: Path) -> None:
    job_root = tmp_path / "local-web"
    day_one = [
        _event(
            "evt-expense-1",
            "expense",
            "午饭",
            "2026-09-15T12:10:00+08:00",
            amount=25.5,
        ),
        _event(
            "evt-exercise-1",
            "exercise",
            "健身",
            "2026-09-15T19:00:00+08:00",
        ),
        _event(
            "evt-routine-1",
            "routine",
            "起床",
            "2026-09-15T07:30:00+08:00",
        ),
    ]
    day_two = [
        _event(
            "evt-expense-2",
            "expense",
            "咖啡",
            "2026-09-16T09:00:00+08:00",
            amount=18,
        ),
        _event(
            "evt-exercise-2",
            "exercise",
            "走路",
            "2026-09-16T18:20:00+08:00",
        ),
        _event(
            "evt-routine-2",
            "routine",
            "睡觉",
            "2026-09-16T00:30:00+08:00",
        ),
    ]
    db_one = _write_day(
        job_root,
        job_id="job-a",
        digest="a",
        day="2026-09-15",
        events=day_one,
    )
    db_two = _write_day(
        job_root,
        job_id="job-b",
        digest="b",
        day="2026-09-16",
        events=day_two,
    )

    stats = build_range_statistics(
        job_root,
        date_from="2026-09-15",
        date_to="2026-09-16",
    )

    sqlite_events: list[dict] = []
    for database in (db_one, db_two):
        with sqlite3.connect(database) as connection:
            sqlite_events.extend(
                json.loads(row[0])
                for row in connection.execute("select payload_json from events")
            )
    sqlite_expense = sum(
        float(event.get("data", {}).get("expense", {}).get("amount") or 0)
        for event in sqlite_events
        if "expense" in event.get("categories", [])
    )

    assert stats["event_count"] == len(sqlite_events) == 6
    assert stats["expense_total_cny"] == sqlite_expense == 43.5
    assert stats["exercise_count"] == 2
    assert stats["category_counts"]["expense"] == 2
    assert stats["category_counts"]["exercise"] == 2
    assert stats["category_counts"]["routine"] == 2
    assert stats["routine_distribution"] == {
        "00-05": 1,
        "06-11": 1,
        "12-17": 0,
        "18-23": 0,
    }
    assert stats["days_with_data"] == 2
    assert stats["daily_event_counts"] == [
        {"date": "2026-09-15", "count": 3},
        {"date": "2026-09-16", "count": 3},
    ]


def test_range_statistics_repairs_legacy_event_day_from_source_filename(
    tmp_path: Path,
) -> None:
    job_root = tmp_path / "local-web"
    event = _event(
        "evt-legacy-expense",
        "expense",
        "夜间消费",
        "2026-09-22T00:16:35+00:00",
        amount=16,
    )
    event["time"]["relative_seconds"] = 120
    _write_day(
        job_root,
        job_id="job-legacy",
        digest="legacy-file",
        day="2026-09-21",
        events=[event],
    )
    job_dir = job_root / "jobs" / "job-legacy"
    (job_dir / "status.json").write_text(
        json.dumps(
            {
                "files": [
                    {
                        "file_id": "legacy-file",
                        "original_name": "2026年9月21日 13_52_46.m4a",
                        "effective_start_at": "2026-09-21T15:22:03+00:00",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    stats = build_range_statistics(
        job_root,
        date_from="2026-09-21",
        date_to="2026-09-21",
    )

    assert stats["event_count"] == 1
    assert stats["expense_total_cny"] == 16
    assert stats["daily_event_counts"] == [{"date": "2026-09-21", "count": 1}]


def test_range_statistics_deduplicates_same_event_across_outputs(
    tmp_path: Path,
) -> None:
    job_root = tmp_path / "local-web"
    duplicate = _event(
        "evt-same",
        "expense",
        "重复导入",
        "2026-09-15T12:00:00+08:00",
        amount=10,
    )
    _write_day(
        job_root,
        job_id="job-a",
        digest="a",
        day="2026-09-15",
        events=[duplicate],
    )
    _write_day(
        job_root,
        job_id="job-b",
        digest="b",
        day="2026-09-15",
        events=[duplicate],
    )

    stats = build_range_statistics(
        job_root,
        date_from="2026-09-15",
        date_to="2026-09-15",
    )

    assert stats["event_count"] == 1
    assert stats["expense_total_cny"] == 10


def test_event_filter_is_applied_before_counts_and_ledger(tmp_path: Path) -> None:
    job_root = tmp_path / "local-web"
    _write_day(
        job_root,
        job_id="job-day",
        digest="d1",
        day="2026-09-15",
        events=[
            _event(
                "evt-keep",
                "expense",
                "保留",
                "2026-09-15T12:00:00+08:00",
                amount=10,
            ),
            _event(
                "evt-hide",
                "expense",
                "隐藏",
                "2026-09-15T13:00:00+08:00",
                amount=99,
            ),
        ],
    )

    stats = build_range_statistics(
        job_root,
        date_from="2026-09-15",
        date_to="2026-09-15",
        event_filter=lambda events: [
            event for event in events if event.get("event_id") == "evt-keep"
        ],
    )

    assert stats["event_count"] == 1
    assert stats["expense_total_cny"] == 10
    assert stats["event_count_basis"]["prefilter_event_count"] == 2
    assert stats["event_count_basis"]["event_filter_applied"] is True
    assert stats["event_view"]["selected_counts"]["event_count"] == 2


def test_event_filter_error_is_not_swallowed(tmp_path: Path) -> None:
    import pytest

    job_root = tmp_path / "local-web"
    _write_day(
        job_root,
        job_id="job-day",
        digest="d1",
        day="2026-09-15",
        events=[
            _event(
                "evt-any",
                "routine",
                "任意",
                "2026-09-15T12:00:00+08:00",
            )
        ],
    )

    with pytest.raises(RuntimeError, match="filter failed"):
        build_range_statistics(
            job_root,
            date_from="2026-09-15",
            date_to="2026-09-15",
            event_filter=lambda _events: (_ for _ in ()).throw(
                RuntimeError("filter failed")
            ),
        )
