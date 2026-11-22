from life_logger.domain import TranscriptSegment
from life_logger.extract import RuleBasedExtractor, _is_actionable_todo

def test_without_pinch_future_intent_is_not_actionable_todo():
    # Ordinary statement without hotword or pinch should NOT be actionable todo
    text = "明天要去银行把卡领了"
    assert not _is_actionable_todo(text)

def test_with_double_pinch_within_window_becomes_actionable_todo():
    # Double pinch within 60s elevates "明天要去银行把卡领了" to actionable todo!
    text = "明天要去银行把卡领了"
    assert _is_actionable_todo(text, physical_pin="double_pinch", pin_age_seconds=15.0)

def test_with_double_pinch_expired_window_falls_back():
    # Double pinch older than 60s expires and does not elevate
    text = "明天要去银行把卡领了"
    assert not _is_actionable_todo(text, physical_pin="double_pinch", pin_age_seconds=75.0)

def test_extractor_elevates_pinch_segment_to_todo_event():
    extractor = RuleBasedExtractor()
    seg_no_pinch = TranscriptSegment(
        segment_id="seg_001",
        recording_id="rec_001",
        start_offset_seconds=0.0,
        end_offset_seconds=3.0,
        absolute_started_at="2026-10-17T10:00:00+08:00",
        absolute_ended_at="2026-10-17T10:00:03+08:00",
        text="明天要去银行把卡领了",
        source="watch",
        language="zh",
        transcription_confidence=0.95,
    )
    # Without pinch -> extracted as idea (future_intent)
    events_no_pinch = extractor.extract(seg_no_pinch)
    assert any("idea" in e.categories for e in events_no_pinch)
    assert not any("todo" in e.categories for e in events_no_pinch)

    # With pinch -> extracted as todo!
    seg_pinch = TranscriptSegment(
        segment_id="seg_002",
        recording_id="rec_001",
        start_offset_seconds=0.0,
        end_offset_seconds=3.0,
        absolute_started_at="2026-10-17T10:00:00+08:00",
        absolute_ended_at="2026-10-17T10:00:03+08:00",
        text="明天要去银行把卡领了",
        source="watch",
        language="zh",
        transcription_confidence=0.95,
        physical_pin="double_pinch",
        pin_age_seconds=5.0,
    )
    events_with_pinch = extractor.extract(seg_pinch)
    assert any("todo" in e.categories for e in events_with_pinch)
    todo_event = next(e for e in events_with_pinch if "todo" in e.categories)
    assert "银行" in todo_event.title
