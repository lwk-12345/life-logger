import json
from pathlib import Path

from life_logger.patterns import build_pattern_observations


def _event(
    event_id: str,
    category: str,
    title: str,
    timestamp: str,
    *,
    amount: float | None = None,
    activity_type: str | None = None,
) -> dict:
    data = {category: {}}
    if category == "expense":
        data["expense"] = {
            "amount": amount,
            "currency": "CNY",
            "item": title,
        }
    if category == "food":
        data["food"] = {"item": title}
    if category == "routine" and activity_type == "waking_up":
        data["routine"] = {"reported_time": timestamp[11:16]}
    payload = {
        "event_id": event_id,
        "categories": [category],
        "activity_type": activity_type,
        "title": title,
        "time": {
            "absolute": timestamp,
            "relative_seconds": 0,
            "precision": "exact",
        },
        "data": data,
        "confidence": 0.9,
        "evidence": {"quote": title},
        "capture_mode": "active",
        "source_type": "audio",
        "review_status": "unreviewed",
    }
    return payload


def _write_day(
    job_root: Path,
    *,
    job_id: str,
    digest: str,
    day: str,
    events: list[dict],
) -> None:
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


def test_pattern_observations_are_evidence_backed(tmp_path: Path) -> None:
    job_root = tmp_path / "local-web"
    _write_day(
        job_root,
        job_id="job-a",
        digest="a",
        day="2026-09-15",
        events=[
            _event(
                "wake-1",
                "routine",
                "起床",
                "2026-09-15T07:20:00+08:00",
                activity_type="waking_up",
            ),
            _event(
                "food-1",
                "food",
                "早餐",
                "2026-09-15T08:00:00+08:00",
            ),
            _event(
                "ex-1",
                "exercise",
                "跑步",
                "2026-09-15T19:00:00+08:00",
            ),
            _event(
                "exp-1",
                "expense",
                "午饭",
                "2026-09-15T12:30:00+08:00",
                amount=32,
            ),
        ],
    )
    _write_day(
        job_root,
        job_id="job-b",
        digest="b",
        day="2026-09-16",
        events=[
            _event(
                "wake-2",
                "routine",
                "起床",
                "2026-09-16T08:10:00+08:00",
                activity_type="waking_up",
            ),
            _event(
                "food-2",
                "food",
                "晚饭",
                "2026-09-16T18:40:00+08:00",
            ),
            _event(
                "exp-2",
                "expense",
                "咖啡",
                "2026-09-16T09:15:00+08:00",
                amount=18,
            ),
        ],
    )

    result = build_pattern_observations(
        job_root,
        date_from="2026-09-15",
        date_to="2026-09-16",
    )

    assert result["schema_version"] == "pattern-observations-v1"
    assert result["minimum_sample_days"] == 3
    assert result["sample_ready"] is False
    assert "至少积累 3 个有数据日期" in result["sample_hint"]
    assert "不是" in result["disclaimer"] or "观察" in result["disclaimer"]
    kinds = {item["kind"] for item in result["observations"]}
    assert kinds == {
        "wake_time_range",
        "exercise_frequency",
        "food_time_windows",
        "expense_pattern",
    }
    wake = next(
        item for item in result["observations"] if item["kind"] == "wake_time_range"
    )
    assert wake["metrics"]["earliest"] == "07:20"
    assert wake["metrics"]["latest"] == "08:10"
    assert wake["metrics"]["sample_count"] == 2
    assert {item["event_id"] for item in wake["evidence"]} == {"wake-1", "wake-2"}
    assert all(item.get("quote") for item in wake["evidence"])

    exercise = next(
        item for item in result["observations"] if item["kind"] == "exercise_frequency"
    )
    assert exercise["metrics"]["days_with_exercise"] == 1
    assert exercise["metrics"]["exercise_event_count"] == 1

    food = next(
        item for item in result["observations"] if item["kind"] == "food_time_windows"
    )
    assert food["metrics"]["food_event_count"] == 2
    assert food["metrics"]["bucket_counts"]["morning"] == 1
    assert food["metrics"]["bucket_counts"]["evening"] == 1

    expense = next(
        item for item in result["observations"] if item["kind"] == "expense_pattern"
    )
    assert expense["metrics"]["total_cny"] == 50
    assert expense["metrics"]["median_cny"] == 25
    assert "诊断" in wake["statement"] or "观察" in wake["statement"]


def test_patterns_repair_legacy_evidence_day_and_wall_clock(tmp_path: Path) -> None:
    job_root = tmp_path / "local-web"
    event = _event(
        "legacy-food",
        "food",
        "夜间加餐",
        "2026-09-22T00:16:35+00:00",
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

    result = build_pattern_observations(
        job_root,
        date_from="2026-09-21",
        date_to="2026-09-21",
    )

    food = next(
        item for item in result["observations"] if item["kind"] == "food_time_windows"
    )
    assert food["metrics"]["bucket_counts"]["midday"] == 1
    assert food["evidence"][0]["date"] == "2026-09-21"
    assert food["evidence"][0]["absolute"] == "2026-09-21T13:54:46+08:00"


def test_pattern_observations_require_valid_date_range(tmp_path: Path) -> None:
    job_root = tmp_path / "local-web"
    job_root.mkdir()
    try:
        build_pattern_observations(
            job_root,
            date_from="2026-09-16",
            date_to="2026-09-15",
        )
        assert False, "expected ValueError"
    except ValueError as error:
        assert "开始日期" in str(error)
