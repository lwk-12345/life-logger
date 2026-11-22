from life_logger.domain import EventTime, LifeEvent
from life_logger.merge import merge_adjacent_events


def event(
    event_id: str,
    *,
    categories: list[str],
    title: str,
    start: float,
    end: float,
    segment_id: str = "seg_001_u01",
    recording_id: str = "rec_001",
    data: dict | None = None,
) -> LifeEvent:
    return LifeEvent(
        event_id=event_id,
        categories=categories,
        activity_type=None,
        title=title,
        time=EventTime(
            absolute=f"2026-09-02T08:00:{int(start):02d}+08:00",
            relative_seconds=start,
            precision="exact",
        ),
        data=data or {categories[0]: {}},
        capture_mode="passive",
        source_type="audio",
        confidence=0.72,
        evidence={
            "recording_id": recording_id,
            "segment_id": segment_id,
            "start_offset_seconds": start,
            "end_offset_seconds": end,
            "quote": title,
        },
        review_status="unreviewed",
    )


def test_merges_adjacent_related_events_from_one_asr_segment() -> None:
    first = event(
        "evt_1",
        categories=["expense", "food"],
        title="花了三十块四毛买炸鸡",
        start=10.0,
        end=14.0,
        data={"expense": {"amount": 30.4}, "food": {"item": "炸鸡"}},
    )
    second = event(
        "evt_2",
        categories=["food"],
        title="我点了很多炸鸡",
        start=16.0,
        end=19.0,
        segment_id="seg_001_u02",
        data={"food": {"item": "炸鸡"}},
    )

    result = merge_adjacent_events([first, second])

    assert len(result) == 1
    assert result[0].categories == ["expense", "food"]
    assert result[0].data["expense"]["amount"] == 30.4
    assert result[0].evidence["segment_ids"] == ["seg_001_u01", "seg_001_u02"]
    assert result[0].evidence["quotes"] == ["花了三十块四毛买炸鸡", "我点了很多炸鸡"]


def test_does_not_merge_different_original_segments() -> None:
    first = event(
        "evt_1",
        categories=["work_communication"],
        title="汇报",
        start=10.0,
        end=14.0,
        segment_id="seg_001_u01",
        data={"work_communication": {}},
    )
    second = event(
        "evt_2",
        categories=["work_communication"],
        title="继续工作",
        start=15.0,
        end=19.0,
        segment_id="seg_002_u01",
        data={"work_communication": {}},
    )

    assert len(merge_adjacent_events([first, second])) == 2


def test_does_not_merge_unrelated_categories_or_large_gaps() -> None:
    first = event(
        "evt_1",
        categories=["food"],
        title="吃饭",
        start=10.0,
        end=14.0,
        data={"food": {}},
    )
    unrelated = event(
        "evt_2",
        categories=["expense"],
        title="买东西",
        start=15.0,
        end=19.0,
        segment_id="seg_001_u02",
        data={"expense": {"amount": 5.0}},
    )
    distant = event(
        "evt_3",
        categories=["food"],
        title="再次吃饭",
        start=30.0,
        end=34.0,
        segment_id="seg_001_u03",
        data={"food": {}},
    )

    assert len(merge_adjacent_events([first, unrelated, distant])) == 3
