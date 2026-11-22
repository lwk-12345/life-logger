"""Past self-statements must not promote utterance time to occurrence time."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from life_logger.contextual_extraction import _event_from_model
from life_logger.event_view import build_event_view


def _record(text: str) -> dict:
    return {
        "source_id": "src-squat",
        "segment_id": "seg-squat",
        "recording_id": "rec-squat",
        "source_label": "self",
        "timestamp": "2026-10-25T12:42:26.426Z",
        "utterance_timestamp": "2026-10-25T12:42:26.426Z",
        "absolute_started_at": "2026-10-25T12:42:26.426Z",
        "start_offset_seconds": 1.0,
        "end_offset_seconds": 2.0,
        "text": text,
        "is_dictation": False,
    }


def _model(text: str, *, relation: str, occurrence: str | None = None) -> dict:
    return {
        "category": "exercise",
        "event_state": "completed",
        "title": "深蹲",
        "activity_type": "strength_training",
        "topic": "训练",
        "claim_type": "event",
        "quote": text,
        "confidence": 0.95,
        "temporal_relation": relation,
        "occurrence_time": occurrence,
        "data": {"exercise": {"exercises": [{"name": "深蹲"}]}},
    }


def _view(event: dict):
    root = Path(tempfile.mkdtemp()) / "local-web"
    result = root / "jobs" / "job-x" / "files" / "rec-1" / "result" / "2026-10-25"
    result.mkdir(parents=True)
    (result / "daily_summary.json").write_text(json.dumps({
        "date": "2026-10-25", "timezone": "Asia/Shanghai",
        "timeline": [event], "display_timeline": [event], "warnings": [],
    }, ensure_ascii=False), encoding="utf-8")
    return build_event_view(root, date_from="2026-10-25", date_to="2026-10-25")["events"][0]


def test_today_past_without_clock_keeps_occurrence_unknown_but_keeps_reported_date() -> None:
    event, reason = _event_from_model(
        _model("首先我今天练的深蹲", relation="past"), _record("首先我今天练的深蹲"),
    )
    assert reason is None
    assert event["time"]["absolute"] is None
    assert event["time"]["precision"] == "unknown"
    assert event["evidence"]["reported_at"] == "2026-10-25T12:42:26.426Z"
    assert event["evidence"]["occurrence_time_evidence"] is None
    view = _view(event)
    assert view["occurred_at"] is None
    assert view["display_time"] is None
    assert view["event_time_basis"] == "reported_local"
    assert view["date_basis"] == "reported_local"
    assert view["effective_date"] == "2026-10-25"


def test_yesterday_past_without_clock_is_unknown_not_reported_as_occurrence() -> None:
    event, reason = _event_from_model(
        _model("昨天练了深蹲", relation="past"), _record("昨天练了深蹲"),
    )
    assert reason is None
    assert event["time"]["absolute"] is None
    assert event["time"]["precision"] == "unknown"
    view = _view(event)
    assert view["occurred_at"] is None
    assert view["display_time"] is None


def test_explicit_past_clock_and_current_at_speech_are_not_downgraded() -> None:
    explicit, reason = _event_from_model(
        _model("下午5点练了深蹲", relation="past", occurrence="2026-10-25T17:00:00+08:00"),
        _record("下午5点练了深蹲"),
    )
    assert reason is None
    assert explicit["time"]["absolute"] == "2026-10-25T17:00:00+08:00"
    explicit_view = _view(explicit)
    assert explicit_view["event_time_basis"] == "exact_occurrence"

    current, reason = _event_from_model(
        _model("正在练深蹲", relation="at_speech"), _record("正在练深蹲"),
    )
    assert reason is None
    assert current["time"]["absolute"] == "2026-10-25T12:42:26.426Z"
    assert current["time"]["precision"] == "exact"
    current_view = _view(current)
    assert current_view["occurred_at"] == "2026-10-25T20:42:26.426000+08:00"
    assert current_view["display_time"]["basis"] == "exact_occurrence"


def test_past_without_today_stays_unknown() -> None:
    event, reason = _event_from_model(
        _model("练了深蹲", relation="past"), _record("练了深蹲"),
    )
    assert reason is None
    assert event["time"]["absolute"] is None
    assert event["time"]["precision"] == "unknown"
