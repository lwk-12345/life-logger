from life_logger.domain import TranscriptSegment
from life_logger.segment import split_utterances


def test_split_utterances_preserves_text_and_approximates_offsets() -> None:
    source = TranscriptSegment(
        segment_id="seg_001",
        recording_id="rec_001",
        start_offset_seconds=10.0,
        end_offset_seconds=20.0,
        absolute_started_at="2026-09-02T08:00:10+08:00",
        absolute_ended_at="2026-09-02T08:00:20+08:00",
        text="记录一下，花4块9买了一瓶饮料。下午跑步三十分钟。",
        source="demo",
        language="zh",
        transcription_confidence=None,
    )

    result = split_utterances(source)

    assert [item.text for item in result] == [
        "记录一下，花4块9买了一瓶饮料。",
        "下午跑步三十分钟。",
    ]
    assert result[0].start_offset_seconds == 10.0
    assert result[-1].end_offset_seconds == 20.0
