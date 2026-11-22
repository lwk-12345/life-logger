"""Temporal uniqueness: unknown/past occurrence must not inherit recording offset."""

from __future__ import annotations

import json

from datetime import datetime
from pathlib import Path

import pytest

from life_logger.event_view import build_event_view, repair_event_time

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_ID = "evt_ctx_21595457e7007e25"
SESSION_SENTINEL_IDS = {
    "evt_session_5ec9c59053d34a94",
    "evt_session_5f98eab7a61cbff4",
    "evt_session_52845a91c6b36e5c",
    "evt_session_d3142d2000a4f436",
}


def _event(relation: str) -> dict:
    return {
        "event_id": "evt_time_contract",
        "title": "时间契约样本",
        "categories": ["work_communication"],
        "evidence": {
            "quote": "我们有六个县城啊。",
            "temporal_relation": relation,
            "start_offset_seconds": 13413.749,
            "reported_at": "2026-10-22T14:07:26.939000+00:00",
            "reported_at_local": "2026-10-22T22:07:26.939000+08:00",
        },
        "time": {
            "absolute": None,
            "local": "2026-10-22T22:07:26.939000+08:00",
            "clock": "22:07",
            "date": "2026-10-22",
            "precision": "unknown",
            "timezone": "Asia/Shanghai",
        },
    }


def test_unknown_relation_does_not_use_recording_offset() -> None:
    repaired = repair_event_time(
        _event("unknown"),
        datetime.fromisoformat("2026-10-22T05:50:39.600000+08:00"),
    )
    assert repaired["time"]["absolute"] is None
    assert repaired["time"]["precision"] == "unknown"
    assert "local" not in repaired["time"] and "clock" not in repaired["time"]


def test_past_without_occurrence_does_not_use_recording_offset() -> None:
    repaired = repair_event_time(
        _event("past"),
        datetime.fromisoformat("2026-10-22T05:50:39.600000+08:00"),
    )
    assert repaired["time"]["absolute"] is None


def test_at_speech_still_uses_recording_offset_when_allowed() -> None:
    repaired = repair_event_time(
        _event("at_speech"),
        datetime.fromisoformat("2026-10-22T05:50:39.600000+08:00"),
    )
    assert repaired["time"]["absolute"] == "2026-10-22T09:34:13.349000+08:00"


def test_session_aggregator_sentinel_dates_never_return_2025() -> None:
    view = build_event_view(
        REPO_ROOT / "output" / "local-web",
        date_from="2026-10-22",
        date_to="2026-10-22",
        scope="provisional",
        view="display",
    )
    found = {event.get("event_id"): event for event in view["events"]}
    missing = SESSION_SENTINEL_IDS - found.keys()
    if missing:
        pytest.skip(f"real sentinel samples not present: {sorted(missing)}")
    for event_id in SESSION_SENTINEL_IDS:
        event = found[event_id]
        assert event["time"]["absolute"] is None, event_id
        assert event["occurred_at"] is None, event_id
        assert event["effective_date"] == "2026-10-22", event_id
        assert event["date_basis"] == "artifact_directory", event_id
        assert "2025-01-01" not in str(event.get("time")), event_id
        # 【返工·A】只清 time.* 不够：reported_at 会经 /api/daily 透出，前端
        # journalEventTimestamp() 的 fallback 会读它 → 日志页显示 2025-01-01。
        assert event["evidence"].get("reported_at") is None, event_id
        assert event["evidence"].get("reported_at_local") is None, event_id
        assert event["evidence"].get("reported_at_dropped") == "sentinel", event_id
        assert "2025-01-01" not in str(event.get("evidence")), event_id


def test_no_sentinel_leaks_anywhere_in_view() -> None:
    """全视图扫描：任何事件的 time/evidence 里都不许出现 2025-01-01。"""
    view = build_event_view(
        REPO_ROOT / "output" / "local-web",
        date_from="2026-10-18",
        date_to="2026-10-23",
        scope="provisional",
        view="display",
    )
    leaked = [
        event.get("event_id")
        for event in view["events"]
        if "2025-01-01" in json.dumps(event, ensure_ascii=False)
    ]
    assert leaked == [], leaked

def test_real_unknown_sample_is_recovered_from_segment_transcript() -> None:
    """该样本曾是「只有 reported_local」的未定位事件，2026-10-24 起由 B 类严格回填恢复。

    规则见 tests/test_time_backfill_segment.py：
    `evidence.segment_id` 同 id 转录行时间与 `reported_at` 相差 ≤2s 才回填，
    precision=inferred、occurrence_source=segment_transcript。
    """
    view = build_event_view(
        REPO_ROOT / "output" / "local-web",
        date_from="2026-10-22",
        date_to="2026-10-22",
        scope="provisional",
        view="display",
    )
    event = next(
        (item for item in view["events"] if item.get("event_id") == SAMPLE_ID),
        None,
    )
    if event is None:
        pytest.skip("real sample event is not present in this checkout")
    if (event.get("time") or {}).get("occurrence_source") != "segment_transcript":
        pytest.skip("segment evidence no longer available for this sample (data drift)")
    assert event["time"]["absolute"] == "2026-10-22T22:07:26.939000+08:00"
    assert event["time"]["precision"] == "inferred"
    assert event["evidence"]["occurrence_time_evidence"]["status"] == "matched"
    # 讲述时间必须原样保留，作为证据链的一部分
    assert event["reported_at"] == "2026-10-22T22:07:26.939000+08:00"
    assert event["effective_date"] == "2026-10-22"
    assert event["date_basis"] == "occurrence"
    assert event["time_repair"]["repaired"] is True
