from unittest.mock import patch
from life_logger.session_aggregator import format_session_transcript, aggregate_session_events

def test_format_session_transcript():
    rows = [
        {"absolute_started_at": "2026-10-11T13:15:16+08:00", "source_label": "self", "text": "现在正式去健身房。"},
        {"absolute_started_at": "2026-10-11T13:21:33+08:00", "source_label": "self", "text": "第二组间歇一分钟，第二组是40公斤。"},
    ]
    formatted = format_session_transcript(rows)
    assert "[13:15:16] [self] 现在正式去健身房。" in formatted
    assert "[13:21:33] [self] 第二组间歇一分钟，第二组是40公斤。" in formatted

def test_aggregate_session_events_calls_llm():
    rows = [
        {"absolute_started_at": "2026-10-11T13:15:16+08:00", "source_label": "self", "text": "去健身房练胸。"},
        {"absolute_started_at": "2026-10-11T13:21:33+08:00", "source_label": "self", "text": "第二组40公斤。"},
        {"absolute_started_at": "2026-10-11T14:25:58+08:00", "source_label": "self", "text": "结束了离开。"},
    ]
    mock_events = [
        {
            "title": "健身：平板杠铃卧推训练",
            "categories": ["exercise"],
            "event_state": "completed",
            "time": {"absolute": "2026-10-11T13:15:16+08:00"},
            "evidence": {"quote": "去健身房练胸。"},
            "data": {
                "exercise": {
                    "workout_type": "strength",
                    "exercises": [{"name": "平板卧推", "sets": [{"set": 1, "weight_kg": 40}]}]
                }
            }
        }
    ]

    with patch("life_logger.session_aggregator.call_magpie_deepseek", return_value={"events": mock_events}):
        events = aggregate_session_events(rows, session_type_hint="exercise")

    assert len(events) == 1
    assert events[0]["title"] == "健身：平板杠铃卧推训练"
    assert events[0]["data"]["exercise"]["exercises"][0]["name"] == "平板卧推"
