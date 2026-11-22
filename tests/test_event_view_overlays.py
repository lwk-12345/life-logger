"""Read-only overlay compatibility for the canonical event view."""

from __future__ import annotations

import json
from pathlib import Path

from life_logger.daily_brief import build_daily_brief
from life_logger.event_view import build_event_view
from life_logger.statistics import build_range_statistics


def _event(
    event_id: str,
    *,
    quote: str,
    absolute: str,
    amount: float | None = 10.0,
    title: str = "原始标题",
    categories: list[str] | None = None,
) -> dict:
    return {
        "event_id": event_id,
        "categories": categories or ["expense"],
        "title": title,
        "time": {"absolute": absolute, "precision": "exact"},
        "data": {
            "expense": {
                "amount": amount,
                "currency": "CNY",
                "item": quote,
            }
        },
        "confidence": 0.9,
        "evidence": {
            "quote": quote,
            "reported_at": absolute,
            "event_state": "completed",
            "claim_type": "event",
        },
        "review_status": "unreviewed",
    }


def _write_job_artifact(
    root: Path,
    *,
    job_id: str,
    digest: str,
    date: str,
    events: list[dict],
    display_timeline: list[dict] | None = None,
) -> tuple[Path, Path]:
    summary = (
        root
        / "jobs"
        / job_id
        / "files"
        / digest
        / "result"
        / date
        / "daily_summary.json"
    )
    summary.parent.mkdir(parents=True)
    artifact = summary.with_name("events.json")
    artifact.write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
    summary.write_text(
        json.dumps(
            {
                "date": date,
                "timezone": "Asia/Shanghai",
                "timeline": events,
                "display_timeline": (
                    events if display_timeline is None else display_timeline
                ),
                "warnings": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return summary, artifact


def _write_correction(
    root: Path,
    *,
    job_id: str,
    artifact_path: str,
    event_id: str,
    changes: dict | None = None,
    rejected: bool = False,
) -> Path:
    path = root / "jobs" / job_id / "event-corrections.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "0.1",
                "corrections": {
                    f"{artifact_path}::{event_id}": {
                        "job_id": job_id,
                        "artifact_path": artifact_path,
                        "event_id": event_id,
                        "changes": changes or {},
                        "rejected": rejected,
                        "updated_at": "2026-10-17T12:00:00+08:00",
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _write_manual_events(root: Path, *, job_id: str, events: list[dict]) -> Path:
    path = root / "jobs" / job_id / "manual-events.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": "0.1", "events": events}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _write_live_session(
    root: Path,
    *,
    events: list[dict],
    job_id: str | None,
) -> None:
    session = root / "watch-sessions" / "dev" / "session"
    session.mkdir(parents=True, exist_ok=True)
    (session / "events.json").write_text(
        json.dumps(events, ensure_ascii=False), encoding="utf-8"
    )
    (session / "manifest.json").write_text(
        json.dumps(
            {
                "device_id": "dev",
                "completion_job_id": job_id,
                "segments": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_correction_updates_title_amount_ledger_and_edit_artifact(
    tmp_path: Path,
) -> None:
    original = _event(
        "evt-corrected",
        quote="买了一包桂圆二十四元",
        absolute="2026-10-12T23:50:00+08:00",
        amount=24.0,
    )
    summary, artifact = _write_job_artifact(
        tmp_path,
        job_id="job-one",
        digest="hash",
        date="2026-10-12",
        events=[original],
    )
    artifact_path = "files/hash/result/2026-10-12/events.json"
    _write_correction(
        tmp_path,
        job_id="job-one",
        artifact_path=artifact_path,
        event_id="evt-corrected",
        changes={
            "title": "用户修正后的桂圆",
            "data": {
                "expense": {
                    "amount": 42.0,
                    "currency": "CNY",
                    "item": "桂圆",
                }
            },
        },
    )
    before = artifact.read_bytes()

    view = build_event_view(
        tmp_path,
        date_from="2026-10-12",
        date_to="2026-10-12",
        scope="settled",
        view="display",
    )
    event = view["events"][0]

    assert event["title"] == "用户修正后的桂圆"
    assert event["data"]["expense"]["amount"] == 42.0
    assert event["correction_status"] == "corrected"
    assert event["review_status"] == "confirmed"
    assert event["correction_provenance"]["artifact_path"] == artifact_path
    assert event["artifact_path"] == artifact_path
    assert Path(event["source_event_artifact"]).is_file()
    assert event["source_event_artifact"].endswith("events.json")
    assert event["source_summary_artifact"] == str(summary.resolve())

    stats = build_range_statistics(
        tmp_path,
        date_from="2026-10-12",
        date_to="2026-10-12",
    )
    assert stats["expense_total_cny"] == 42.0
    assert artifact.read_bytes() == before
    assert json.loads(before)[0]["title"] == "原始标题"


def test_corrected_date_moves_across_midnight_and_range_is_consistent(
    tmp_path: Path,
) -> None:
    original = _event(
        "evt-moved",
        quote="跨午夜消费",
        absolute="2026-10-12T23:50:00+08:00",
    )
    _write_job_artifact(
        tmp_path,
        job_id="job-move",
        digest="hash",
        date="2026-10-12",
        events=[original],
    )
    artifact_path = "files/hash/result/2026-10-12/events.json"
    _write_correction(
        tmp_path,
        job_id="job-move",
        artifact_path=artifact_path,
        event_id="evt-moved",
        changes={"time": {"absolute": "2026-10-13T01:10:00+08:00"}},
    )
    old_day = build_event_view(
        tmp_path,
        date_from="2026-10-12",
        date_to="2026-10-12",
        scope="settled",
        view="all",
    )
    new_day = build_event_view(
        tmp_path,
        date_from="2026-10-13",
        date_to="2026-10-13",
        scope="settled",
        view="all",
    )
    wide = build_event_view(
        tmp_path,
        date_from="2026-10-11",
        date_to="2026-10-14",
        scope="settled",
        view="all",
    )

    assert old_day["events"] == []
    assert [event["event_id"] for event in new_day["events"]] == ["evt-moved"]
    assert new_day["events"][0]["effective_date"] == "2026-10-13"
    assert [event["event_id"] for event in wide["events"] if event["effective_date"] == "2026-10-13"] == [
        "evt-moved"
    ]


def test_rejected_formal_event_suppresses_matching_live_twin(tmp_path: Path) -> None:
    quote = "这条被用户拒绝了"
    when = "2026-10-13T09:00:00+08:00"
    formal = _event("evt-rejected", quote=quote, absolute=when)
    live = _event("evt-live-twin", quote=quote, absolute=when)
    _write_job_artifact(
        tmp_path,
        job_id="job-reject",
        digest="hash",
        date="2026-10-13",
        events=[formal],
    )
    _write_live_session(tmp_path, events=[live], job_id="job-reject")
    _write_correction(
        tmp_path,
        job_id="job-reject",
        artifact_path="files/hash/result/2026-10-13/events.json",
        event_id="evt-rejected",
        rejected=True,
    )

    view = build_event_view(
        tmp_path,
        date_from="2026-10-13",
        date_to="2026-10-13",
        scope="provisional",
        view="display",
    )

    assert view["events"] == []
    assert view["metadata"]["dedup"]["rejected_tombstone_count"] == 1
    assert view["metadata"]["dedup"]["rejected_live_suppressed_count"] == 1


def test_manual_only_entry_without_summary_is_included_once(tmp_path: Path) -> None:
    artifact_path = "files/hash/result/2026-10-14/events.json"
    artifact = tmp_path / "jobs" / "job-manual" / artifact_path
    artifact.parent.mkdir(parents=True)
    artifact.write_text("[]", encoding="utf-8")
    manual = _event(
        "manual_evt_1",
        quote="用户人工补记的事件",
        absolute="2026-10-14T08:00:00+08:00",
        amount=5.0,
    )
    manual["artifact_path"] = artifact_path
    manual["source_type"] = "manual"
    manual["review_status"] = "confirmed"
    _write_manual_events(tmp_path, job_id="job-manual", events=[manual])

    view = build_event_view(
        tmp_path,
        date_from="2026-10-14",
        date_to="2026-10-14",
        scope="provisional",
        view="display",
    )
    brief = build_daily_brief(tmp_path, date="2026-10-14")

    assert [event["event_id"] for event in view["events"]] == ["manual_evt_1"]
    assert view["events"][0]["source_kind"] == "manual"
    assert view["events"][0]["artifact_path"] == artifact_path
    assert Path(view["events"][0]["source_event_artifact"]).is_file()
    assert [event["event_id"] for event in brief["events"]] == ["manual_evt_1"]
    assert brief["source_manual_event_files"]


def test_same_event_id_in_another_job_is_not_cross_corrected(tmp_path: Path) -> None:
    quote = "同一 event_id 的不同产物"
    first = _event(
        "evt-same",
        quote=quote,
        absolute="2026-10-15T10:00:00+08:00",
        title="第一份",
    )
    second = _event(
        "evt-same",
        quote=quote,
        absolute="2026-10-16T10:00:00+08:00",
        title="第二份",
    )
    _write_job_artifact(
        tmp_path,
        job_id="job-a",
        digest="hash-a",
        date="2026-10-15",
        events=[first],
    )
    _write_job_artifact(
        tmp_path,
        job_id="job-b",
        digest="hash-b",
        date="2026-10-16",
        events=[second],
    )
    _write_correction(
        tmp_path,
        job_id="job-a",
        artifact_path="files/hash-a/result/2026-10-15/events.json",
        event_id="evt-same",
        changes={"title": "只修正 job-a"},
    )

    corrected_view = build_event_view(
        tmp_path,
        date_from="2026-10-15",
        date_to="2026-10-15",
        scope="settled",
        view="display",
    )
    other_view = build_event_view(
        tmp_path,
        date_from="2026-10-16",
        date_to="2026-10-16",
        scope="settled",
        view="display",
    )

    assert corrected_view["events"][0]["title"] == "只修正 job-a"
    assert corrected_view["events"][0]["correction_status"] == "corrected"
    assert other_view["events"][0]["title"] == "第二份"
    assert "correction_status" not in other_view["events"][0]
    assert other_view["events"][0]["artifact_path"] == (
        "files/hash-b/result/2026-10-16/events.json"
    )


def test_manual_and_review_derived_survive_empty_display_timeline(
    tmp_path: Path,
) -> None:
    generated = _event(
        "evt-generated",
        quote="生成事件被展示层筛掉",
        absolute="2026-10-17T09:00:00+08:00",
    )
    _write_job_artifact(
        tmp_path,
        job_id="job-mixed",
        digest="hash",
        date="2026-10-17",
        events=[generated],
        display_timeline=[],
    )
    artifact_path = "files/hash/result/2026-10-17/events.json"
    artifact = tmp_path / "jobs" / "job-mixed" / artifact_path
    artifact.write_text("[]", encoding="utf-8")
    manual = _event(
        "manual_evt_empty_display",
        quote="人工补记不应消失",
        absolute="2026-10-17T10:00:00+08:00",
        amount=6.0,
    )
    manual["artifact_path"] = artifact_path
    manual["source_type"] = "manual"
    _write_manual_events(tmp_path, job_id="job-mixed", events=[manual])
    derived = _event(
        "derived_evt_1",
        quote="审核派生事件",
        absolute="2026-10-17T11:00:00+08:00",
        amount=8.0,
    )
    derived["artifact_path"] = artifact_path
    derived["source_type"] = "review_derived"
    (tmp_path / "jobs" / "job-mixed" / "review-derived-events.json").write_text(
        json.dumps({"schema_version": "0.1", "events": [derived]}, ensure_ascii=False),
        encoding="utf-8",
    )

    # The generated display timeline is authoritative-empty, but explicit
    # human additions are not generated events and must remain visible.
    view = build_event_view(
        tmp_path,
        date_from="2026-10-17",
        date_to="2026-10-17",
        scope="provisional",
        view="display",
    )
    ids = {event["event_id"] for event in view["events"]}
    assert ids == {"manual_evt_empty_display", "derived_evt_1"}


def test_corrected_absolute_recomputes_derived_local_clock_and_date(
    tmp_path: Path,
) -> None:
    event = _event(
        "evt-clock",
        quote="时钟必须跟随权威绝对时间",
        absolute="2026-10-14T15:28:00Z",
    )
    event["time"].update(
        {
            "local": "2026-10-14T23:28:00+08:00",
            "absolute_local": "2026-10-14T23:28:00+08:00",
            "clock": "23:28",
            "date": "2026-10-14",
            "timezone": "Asia/Shanghai",
            "precision": "exact",
        }
    )
    event["evidence"]["reported_at"] = "2026-10-14T15:28:00Z"
    event["evidence"]["reported_at_local"] = "2026-10-14T23:28:00+08:00"
    _write_job_artifact(
        tmp_path,
        job_id="job-clock",
        digest="hash",
        date="2026-10-14",
        events=[event],
    )
    artifact_path = "files/hash/result/2026-10-14/events.json"
    _write_correction(
        tmp_path,
        job_id="job-clock",
        artifact_path=artifact_path,
        event_id="evt-clock",
        changes={"time": {"absolute": "2026-10-14T16:30:00Z"}},
    )
    raw_before = (tmp_path / "jobs" / "job-clock" / artifact_path).read_bytes()

    view = build_event_view(
        tmp_path,
        date_from="2026-10-15",
        date_to="2026-10-15",
        scope="settled",
        view="all",
    )
    assert len(view["events"]) == 1
    event_out = view["events"][0]
    assert event_out["effective_date"] == "2026-10-15"
    assert event_out["occurred_at"] == "2026-10-15T00:30:00+08:00"
    time_out = event_out["time"]
    assert time_out["absolute"] == "2026-10-14T16:30:00Z"
    assert time_out["absolute_local"] == "2026-10-15T00:30:00+08:00"
    assert time_out["local"] == "2026-10-15T00:30:00+08:00"
    assert time_out["clock"] == "00:30"
    assert time_out["date"] == "2026-10-15"
    assert time_out["timezone"] == "Asia/Shanghai"
    # Reported local is regenerated from reported_at, not from occurrence.
    assert event_out["evidence"]["reported_at_local"] == "2026-10-14T23:28:00+08:00"
    assert (tmp_path / "jobs" / "job-clock" / artifact_path).read_bytes() == raw_before


def test_stale_derived_local_does_not_promote_unknown_occurrence(
    tmp_path: Path,
) -> None:
    event = _event(
        "evt-unknown",
        quote="发生时间其实未知",
        absolute=None,
    )
    event["time"] = {
        "absolute": None,
        "local": "2026-10-14T23:28:00+08:00",
        "absolute_local": "2026-10-14T23:28:00+08:00",
        "clock": "23:28",
        "date": "2026-10-14",
        "precision": "unknown",
    }
    event["evidence"].update(
        {
            "reported_at": "2026-10-14T23:28:00+08:00",
            "reported_at_local": "2026-10-14T23:28:00+08:00",
            "temporal_relation": "unknown",
        }
    )
    _write_job_artifact(
        tmp_path,
        job_id="job-unknown",
        digest="hash",
        date="2026-10-14",
        events=[event],
    )

    view = build_event_view(
        tmp_path,
        date_from="2026-10-14",
        date_to="2026-10-14",
        scope="settled",
        view="all",
    )
    assert len(view["events"]) == 1
    event_out = view["events"][0]
    assert event_out["occurred_at"] is None
    assert event_out["occurrence_date"] is None
    assert event_out["date_basis"] == "reported_local"
    assert event_out["date_is_unknown"] is True
    assert "local" not in event_out["time"]
    assert "clock" not in event_out["time"]
    assert "date" not in event_out["time"]


def test_legacy_local_only_with_known_precision_remains_occurrence(
    tmp_path: Path,
) -> None:
    event = _event(
        "evt-legacy-local",
        quote="旧产物的本地时间",
        absolute=None,
    )
    event["time"] = {
        "absolute": None,
        "local": "2026-10-14T23:28:00+08:00",
        "precision": "exact",
    }
    event["evidence"].pop("reported_at", None)
    _write_job_artifact(
        tmp_path,
        job_id="job-legacy-local",
        digest="hash",
        date="2026-10-14",
        events=[event],
    )

    view = build_event_view(
        tmp_path,
        date_from="2026-10-14",
        date_to="2026-10-14",
        scope="settled",
        view="all",
    )
    event_out = view["events"][0]
    assert event_out["occurred_at"] == "2026-10-14T23:28:00+08:00"
    assert event_out["date_basis"] == "occurrence"
    assert event_out["time"]["clock"] == "23:28"
    assert event_out["time"]["date"] == "2026-10-14"


def test_correction_clearing_absolute_removes_stale_occurrence_fields(
    tmp_path: Path,
) -> None:
    event = _event(
        "evt-cleared",
        quote="修正后发生时间未知",
        absolute="2026-10-14T15:28:00Z",
    )
    event["time"].update(
        {
            "local": "2026-10-14T23:28:00+08:00",
            "clock": "23:28",
            "date": "2026-10-14",
            "precision": "exact",
        }
    )
    _write_job_artifact(
        tmp_path,
        job_id="job-cleared",
        digest="hash",
        date="2026-10-14",
        events=[event],
    )
    _write_correction(
        tmp_path,
        job_id="job-cleared",
        artifact_path="files/hash/result/2026-10-14/events.json",
        event_id="evt-cleared",
        changes={"time": {"absolute": None, "precision": "unknown"}},
    )

    new_day = build_event_view(
        tmp_path,
        date_from="2026-10-15",
        date_to="2026-10-15",
        scope="settled",
        view="all",
    )
    reported_day = build_event_view(
        tmp_path,
        date_from="2026-10-14",
        date_to="2026-10-14",
        scope="settled",
        view="all",
    )

    assert new_day["events"] == []
    assert len(reported_day["events"]) == 1
    event_out = reported_day["events"][0]
    assert event_out["occurred_at"] is None
    assert event_out["date_basis"] == "reported_local"
    assert "local" not in event_out["time"]
    assert "clock" not in event_out["time"]
    assert "date" not in event_out["time"]


def test_home_input_manual_event_without_time_uses_artifact_date(tmp_path: Path) -> None:
    """首页“记一笔”没说时间也要进当天，不能提示成功后静默消失。"""
    manual = _event("manual_evt_home", quote="记一笔，买冰杯花了2元", absolute=None, amount=2.0)
    manual["artifact_path"] = "2026-11-05/events.json"
    manual["source_type"] = "user_supplement"
    manual["review_status"] = "confirmed"
    manual["time"] = {"absolute": None, "relative_seconds": None, "precision": "relative"}
    _write_manual_events(tmp_path, job_id="job-home", events=[manual])

    view = build_event_view(
        tmp_path, date_from="2026-11-05", date_to="2026-11-05", scope="provisional", view="display",
    )

    assert [event["event_id"] for event in view["events"]] == ["manual_evt_home"]
    assert view["events"][0]["effective_date"] == "2026-11-05"


def test_older_home_input_events_stay_out_of_frozen_days(tmp_path: Path) -> None:
    manual = _event("manual_evt_old", quote="记一笔买咖啡15元", absolute=None, amount=15.0)
    manual["artifact_path"] = "2026-10-03/events.json"
    manual["source_type"] = "user_supplement"
    manual["review_status"] = "confirmed"
    manual["time"] = {"absolute": None, "relative_seconds": None, "precision": "relative"}
    _write_manual_events(tmp_path, job_id="job-old", events=[manual])

    view = build_event_view(
        tmp_path, date_from="2026-10-03", date_to="2026-10-03", scope="provisional", view="display",
    )

    assert view["events"] == []


def test_restored_manual_event_returns_to_its_day(tmp_path: Path) -> None:
    manual = _event("manual_evt_restored", quote="记一笔买咖啡15元", absolute=None, amount=15.0)
    manual["artifact_path"] = "2026-10-03/events.json"
    manual["source_type"] = "user_supplement"
    manual["review_status"] = "confirmed"
    manual["restored_date"] = "2026-10-03"
    manual["time"] = {"absolute": None, "relative_seconds": None, "precision": "relative"}
    _write_manual_events(tmp_path, job_id="job-old", events=[manual])

    view = build_event_view(
        tmp_path, date_from="2026-10-03", date_to="2026-10-03", scope="provisional", view="display",
    )

    assert [event["event_id"] for event in view["events"]] == ["manual_evt_restored"]
