import json
import sqlite3
from pathlib import Path

from life_logger.domain import EventTime, LifeEvent, RecordingSegment, TranscriptSegment
from life_logger.render import write_daily_outputs


def test_write_daily_outputs_creates_json_markdown_and_sqlite(tmp_path: Path) -> None:
    recording = RecordingSegment(
        recording_id="rec_001",
        source_file="input/demo.m4a",
        started_at="2026-09-02T08:00:00+08:00",
        ended_at=None,
        duration_seconds=None,
        sequence=1,
        time_source="user",
        time_precision="exact",
        ingestion_warnings=[],
    )
    transcript = TranscriptSegment(
        segment_id="seg_001",
        recording_id="rec_001",
        start_offset_seconds=1.0,
        end_offset_seconds=2.0,
        absolute_started_at="2026-09-02T08:00:01+08:00",
        absolute_ended_at="2026-09-02T08:00:02+08:00",
        text="起床。",
        source="demo",
        language="zh",
        transcription_confidence=None,
    )
    event = LifeEvent(
        event_id="evt_001",
        categories=["routine"],
        activity_type="waking_up",
        title="起床",
        time=EventTime(
            absolute="2026-09-02T08:00:01+08:00",
            relative_seconds=1.0,
            precision="exact",
        ),
        data={"routine": {}},
        capture_mode="passive",
        source_type="audio",
        confidence=0.8,
        evidence={
            "recording_id": "rec_001",
            "segment_id": "seg_001",
            "quote": "起床。",
        },
        review_status="unreviewed",
    )

    paths = write_daily_outputs(
        tmp_path,
        date="2026-09-02",
        recordings=[recording],
        transcripts=[transcript],
        events=[event],
        summary={
            "date": "2026-09-02",
            "timezone": "Asia/Shanghai",
            "event_count": 1,
            "expense_total_cny": 0.0,
            "timeline": [event.to_dict()],
            "category_summary": {"routine": {"count": 1, "highlights": ["起床"]}},
            "warnings": [],
        },
    )

    assert paths["events"].exists()
    events = json.loads(paths["events"].read_text(encoding="utf-8"))
    assert events[0]["event_id"] == "evt_001"
    assert "起床" in paths["markdown"].read_text(encoding="utf-8")
    with sqlite3.connect(paths["database"]) as connection:
        assert connection.execute("select count(*) from recordings").fetchone()[0] == 1
        assert (
            connection.execute("select count(*) from transcript_segments").fetchone()[0]
            == 1
        )
        assert connection.execute("select count(*) from events").fetchone()[0] == 1
