"""Dashboard must use the shared canonical event view and received audio coverage."""

from __future__ import annotations

import json
from pathlib import Path

from life_logger.daily_dashboard import DailyDashboardBuilder


def _event(
    event_id: str,
    quote: str,
    *,
    absolute: str | None,
    reported_at: str | None = None,
    categories: list[str] | None = None,
    title: str | None = None,
) -> dict:
    categories = categories or ["food"]
    return {
        "event_id": event_id,
        "categories": categories,
        "title": title or quote,
        "time": {"absolute": absolute, "precision": "exact"},
        "data": {},
        "confidence": 0.95,
        "evidence": {"quote": quote, "reported_at": reported_at},
        "source_type": "audio",
        "review_status": "reviewed",
    }


def _write_summary(
    root: Path,
    *,
    directory_date: str,
    events: list[dict],
    job_id: str = "job-1",
    digest: str = "file-1",
) -> Path:
    target = (
        root
        / "jobs"
        / job_id
        / "files"
        / digest
        / "result"
        / directory_date
        / "daily_summary.json"
    )
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                "date": directory_date,
                "timezone": "Asia/Shanghai",
                "timeline": events,
                "display_timeline": events,
                "warnings": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return target


def _write_watch_session(
    root: Path,
    *,
    device: str,
    session: str,
    started_at: str,
    segments: list[dict],
    events: list[dict] | None = None,
) -> None:
    target = root / "watch-sessions" / device / session
    target.mkdir(parents=True)
    (target / "manifest.json").write_text(
        json.dumps(
            {
                "started_at": started_at,
                "segments": segments,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (target / "events.json").write_text(
        json.dumps(events or [], ensure_ascii=False), encoding="utf-8"
    )


def _segment(sequence: int, offset_ms: int, duration_ms: int) -> dict:
    return {
        "sequence": sequence,
        "start_offset_ms": offset_ms,
        "duration_ms": duration_ms,
    }


def test_received_coverage_is_clipped_at_midnight(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    _write_watch_session(
        root,
        device="watch-test",
        session="SESS_CROSS",
        started_at="2026-10-16T22:00:00+08:00",
        # 22:00 → next day 02:00: only 120 minutes belong to 2026-10-17.
        segments=[_segment(0, 0, 4 * 60 * 60 * 1000)],
    )
    attr_dir = root / "jobs" / "job-1" / "files" / "rec-1" / "attribution"
    result_dir = root / "jobs" / "job-1" / "files" / "rec-1" / "result" / "2026-10-17"
    attr_dir.mkdir(parents=True)
    result_dir.mkdir(parents=True)
    (attr_dir / "source-attribution.json").write_text(
        json.dumps(
            {
                "records": [
                    {
                        "recording_id": "r1",
                        "segment_id": "s1",
                        "absolute_started_at": "2026-10-17T09:00:00+08:00",
                        "start_offset_seconds": 0.0,
                        # 30 minutes of ASR speech; coverage is still 120 minutes.
                        "end_offset_seconds": 1800.0,
                        "source_label": "self",
                        "speaker": "self",
                        "text": "测试语音",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    dash = DailyDashboardBuilder(root).build_for_date("2026-10-17")

    assert dash.audio_duration_basis == "received_recording_coverage"
    assert dash.received_recording_coverage_minutes == 120.0
    assert dash.total_audio_recorded_minutes == 120.0
    assert dash.speech_duration_minutes == 30.0


def test_overlapping_received_recordings_are_unioned(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    _write_watch_session(
        root,
        device="watch-test",
        session="SESS_A",
        started_at="2026-10-17T10:00:00+08:00",
        segments=[_segment(0, 0, 2 * 60 * 60 * 1000)],
    )
    _write_watch_session(
        root,
        device="watch-test",
        session="SESS_B",
        started_at="2026-10-17T11:00:00+08:00",
        segments=[_segment(0, 0, 2 * 60 * 60 * 1000)],
    )

    dash = DailyDashboardBuilder(root).build_for_date("2026-10-17")

    # 10:00–13:00, not 4 hours of double-counted overlap.
    assert dash.received_recording_coverage_minutes == 180.0
    assert dash.total_audio_recorded_minutes == 180.0


def test_no_coverage_keeps_labeled_asr_speech_fallback(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    root.mkdir()
    dash = DailyDashboardBuilder(root).build_for_date("2026-10-17")

    assert dash.audio_duration_basis == "asr_speech"
    assert dash.received_recording_coverage_minutes == 0.0
    assert dash.total_audio_recorded_minutes == dash.speech_duration_minutes
    assert "不代表整段录音时长" in dash.ai_daily_highlights


def test_formal_event_in_directory_12_with_time_13_anchors_scene_13(
    tmp_path: Path,
) -> None:
    root = tmp_path / "local-web"
    _write_summary(
        root,
        directory_date="2026-10-12",
        events=[
            _event(
                "evt-cross-date",
                "购买烧鸭饭",
                absolute="2026-10-13T10:00:00+08:00",
                reported_at="2026-10-13T10:00:00+08:00",
                categories=["food"],
                title="购买烧鸭饭",
            )
        ],
    )
    builder = DailyDashboardBuilder(root)

    day_13 = builder.build_for_date("2026-10-13")
    anchors = [s for s in day_13.timeline_scenes if s.get("detail") == "购买烧鸭饭"]
    assert len(anchors) == 1
    assert anchors[0]["scene"] == "dining"
    assert anchors[0]["start_minute"] == 590

    day_12 = builder.build_for_date("2026-10-12")
    assert not any(
        s.get("detail") == "购买烧鸭饭" for s in day_12.timeline_scenes
    )


def test_live_formal_duplicate_creates_one_anchor(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    duplicate = _event(
        "evt-formal",
        "购买两个冰杯",
        absolute="2026-10-13T14:15:00+08:00",
        reported_at="2026-10-13T14:15:00+08:00",
        categories=["expense"],
        title="购买两个冰杯",
    )
    _write_summary(root, directory_date="2026-10-13", events=[duplicate])
    _write_watch_session(
        root,
        device="watch-test",
        session="SESS_DUP",
        started_at="2026-10-13T14:00:00+08:00",
        segments=[],
        events=[{**duplicate, "event_id": "evt-live"}],
    )
    builder = DailyDashboardBuilder(root)

    events = builder._dashboard_events("2026-10-13")
    assert len(events) == 1

    scenes = builder.build_timeline_scenes("2026-10-13")
    anchors = [s for s in scenes if s.get("detail") == "购买两个冰杯"]
    assert len(anchors) == 1


def test_unknown_occurrence_does_not_use_reported_at_as_location(
    tmp_path: Path,
) -> None:
    root = tmp_path / "local-web"
    _write_summary(
        root,
        directory_date="2026-10-13",
        events=[
            _event(
                "evt-unknown-time",
                "回忆里的午饭",
                absolute=None,
                reported_at="2026-10-13T15:00:00+08:00",
                categories=["food"],
                title="回忆里的午饭",
            )
        ],
    )

    scenes = DailyDashboardBuilder(root).build_timeline_scenes("2026-10-13")

    assert not any(s.get("detail") == "回忆里的午饭" for s in scenes)


def test_pinned_memo_uses_shared_view_and_does_not_duplicate(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    pinned = _event(
        "evt-pinned-formal",
        "便签：更新台账",
        absolute="2026-10-13T11:00:00+08:00",
        reported_at="2026-10-13T11:00:00+08:00",
        categories=["todo"],
        title="便签：更新台账",
    )
    pinned["pinned_memo"] = True
    # Formal source directory is 10-12, actual pinned-memo occurrence is 10-13.
    _write_summary(root, directory_date="2026-10-12", events=[pinned])
    _write_watch_session(
        root,
        device="watch-test",
        session="SESS_PIN",
        started_at="2026-10-13T10:50:00+08:00",
        segments=[],
        events=[{**pinned, "event_id": "evt-pinned-live"}],
    )
    builder = DailyDashboardBuilder(root)

    day_13 = builder.build_for_date("2026-10-13")
    matching = [
        item
        for item in day_13.captured_todos
        if item.get("extracted_content") == "更新台账"
    ]
    assert len(matching) == 1
    assert matching[0]["captured_at"] == "2026-10-13T11:00:00+08:00"

    day_12 = builder.build_for_date("2026-10-12")
    assert not any(
        item.get("extracted_content") == "更新台账"
        for item in day_12.captured_todos
    )
