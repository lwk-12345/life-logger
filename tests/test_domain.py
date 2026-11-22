from life_logger.domain import (
    EventTime,
    LifeEvent,
    RecordingSegment,
    TranscriptSegment,
    validate_event,
)


def test_recording_preserves_time_provenance() -> None:
    item = RecordingSegment(
        recording_id="rec_001",
        source_file="input/2026-09-02_08-00.m4a",
        started_at="2026-09-02T08:00:00+08:00",
        ended_at=None,
        duration_seconds=None,
        sequence=1,
        time_source="filename",
        time_precision="exact",
        ingestion_warnings=[],
    )

    assert item.to_dict()["time_source"] == "filename"
    assert item.to_dict()["time_precision"] == "exact"


def test_event_supports_multiple_categories_and_evidence() -> None:
    event = LifeEvent(
        event_id="evt_001",
        categories=["expense", "food"],
        activity_type="purchase_drink",
        title="购买一瓶饮料",
        time=EventTime(
            absolute="2026-09-02T08:02:05+08:00",
            relative_seconds=125.4,
            precision="exact",
        ),
        data={
            "expense": {"amount": 4.9, "currency": "CNY"},
            "food": {"item": "饮料", "quantity": 1, "unit": "瓶"},
        },
        capture_mode="active",
        source_type="audio",
        confidence=0.96,
        evidence={"recording_id": "rec_001", "segment_id": "seg_001"},
        review_status="unreviewed",
    )

    validate_event(event.to_dict())
    assert event.to_dict()["categories"] == ["expense", "food"]


def test_transcript_preserves_relative_offsets() -> None:
    item = TranscriptSegment(
        segment_id="seg_001",
        recording_id="rec_001",
        start_offset_seconds=1.5,
        end_offset_seconds=4.0,
        absolute_started_at=None,
        absolute_ended_at=None,
        text="我刚起床。",
        source="demo",
        language="zh",
        transcription_confidence=None,
    )

    assert item.to_dict()["start_offset_seconds"] == 1.5
