"""Q391：会话聚合的时间证据契约（全 mock，不调模型）。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.session_aggregator import (  # noqa: E402
    aggregate_session_events,
    format_session_transcript,
    session_time_bounds,
    validate_aggregated_events,
)


def rows(start="2026-10-24T23:50:00+08:00", n=3):
    base = [
        ("2026-10-24T23:50:00+08:00", "开始深蹲训练。"),
        ("2026-10-25T00:10:00+08:00", "第一组 40 公斤 10 次。"),
        ("2026-10-25T00:20:00+08:00", "第二组 50 公斤 8 次。"),
    ]
    return [{"absolute_started_at": ts, "source_label": "self", "text": text,
             "segment_id": f"seg_{i}", "source_id": f"src_{i}"} for i, (ts, text) in enumerate(base[:n])]


def exercise_event(absolute, *, quote="开始深蹲训练。"):
    return {
        "title": "深蹲训练",
        "categories": ["exercise"],
        "event_state": "completed",
        "time": {"absolute": absolute},
        "evidence": {"quote": quote},
        "data": {"exercise": {"exercises": [
            {"name": "深蹲", "sets": [{"set": 1, "weight_kg": 40, "reps": 10},
                                     {"set": 2, "weight_kg": 50, "reps": 8}]}
        ]}},
    }


def test_transcript_keeps_full_iso_time_zone_and_evidence():
    lines = format_session_transcript(rows())
    assert "[23:50:00] [self] 开始深蹲训练。" in lines          # 旧可读前缀保留
    assert "2026-10-24T23:50:00+08:00" in lines                  # 完整 ISO + 时区
    assert "seg=seg_0" in lines and "src=src_0" in lines          # segment/source 证据


def test_bounds_cover_midnight_and_list_both_local_dates():
    bounds = session_time_bounds(rows())
    assert bounds["local_dates"] == ["2026-10-24", "2026-10-25"]
    assert bounds["start"].startswith("2026-10-24T23:50")


def test_wrong_may_date_is_isolated_not_overwritten():
    events = [exercise_event("2026-05-29T09:00:00+08:00")]
    validated = validate_aggregated_events(events, rows())
    assert len(validated) == 1
    event = validated[0]
    assert event["time"]["absolute"] is None, "越界日期必须清空，不能改成会话日期"
    assert event["time"]["precision"] == "unknown"
    assert event["review_status"] == "pending_review"
    assert event["pending_reason"] == "time_outside_session"
    assert event["model_reported_time"] == "2026-05-29T09:00:00+08:00"   # 保留原返回审计
    # 多组动作与重量保留
    sets = event["data"]["exercise"]["exercises"][0]["sets"]
    assert [s["weight_kg"] for s in sets] == [40, 50]
    assert event["evidence"]["session_bounds"]["local_dates"] == ["2026-10-24", "2026-10-25"]


def test_correct_october_date_is_kept():
    events = [exercise_event("2026-10-25T00:10:00+08:00")]
    validated = validate_aggregated_events(events, rows())
    assert validated[0]["time"]["absolute"] == "2026-10-25T00:10:00+08:00"
    assert validated[0].get("review_status") != "pending_review"


def test_cross_midnight_is_legal():
    events = [exercise_event("2026-10-24T23:55:00+08:00"),
              exercise_event("2026-10-25T00:15:00+08:00")]
    validated = validate_aggregated_events(events, rows())
    assert [e["time"]["absolute"] for e in validated] == [
        "2026-10-24T23:55:00+08:00", "2026-10-25T00:15:00+08:00"]


def test_retrospective_statement_is_flagged_not_refilled():
    events = [exercise_event("2026-10-24T23:50:00+08:00", quote="昨天练的深蹲，40 公斤。")]
    validated = validate_aggregated_events(events, rows())
    event = validated[0]
    assert event["retrospective"] is True
    assert event["pending_reason"] == "retrospective_statement"
    assert event["time"]["absolute"] is not None, "回顾不改写时间，只标记待复核"


def test_missing_time_stays_unknown_and_is_not_filled_with_session_date():
    ev = exercise_event(None)
    validated = validate_aggregated_events([ev], rows())
    assert validated[0]["time"]["absolute"] is None
    assert validated[0]["time"]["precision"] == "unknown"


def test_aggregate_session_events_validates_model_output():
    with patch(
        "life_logger.session_aggregator.call_magpie_deepseek",
        return_value={"events": [exercise_event("2026-05-29T09:00:00+08:00")]},
    ):
        events = aggregate_session_events(rows(), session_type_hint="exercise")
    assert len(events) == 1
    assert events[0]["review_status"] == "pending_review"
    assert events[0]["time"]["absolute"] is None


def test_prompt_contains_session_range_and_full_iso_rows():
    captured = {}

    def fake(prompt, temperature=0.1):
        captured["prompt"] = prompt
        return {"events": []}

    with patch("life_logger.session_aggregator.call_magpie_deepseek", fake):
        aggregate_session_events(rows())
    prompt = captured["prompt"]
    assert "时间契约" in prompt
    assert "2026-10-24T23:50:00+08:00" in prompt
    assert "seg=seg_0" in prompt
    assert "不要只看 HH:MM" in prompt


# ---------------------------------------------------------------- Q396：真实 watch 消费链回归

def _watch_processor():
    from unittest.mock import MagicMock
    from life_logger.watch_formal_asr import WatchFormalASRProcessor

    processor = WatchFormalASRProcessor.__new__(WatchFormalASRProcessor)
    processor.store = MagicMock()
    processor.store.root = Path("/tmp/mock_store")
    return processor


def _watch_rows(n=36):
    base = [
        ("2026-10-24T23:50:00+08:00", "开始深蹲训练。"),
        ("2026-10-25T00:10:00+08:00", "第一组 40 公斤 10 次。"),
        ("2026-10-25T00:20:00+08:00", "第二组 50 公斤 8 次。"),
    ] * (n // 3 + 1)
    return [{"absolute_started_at": ts, "source_label": "self", "text": text,
             "start_offset_seconds": i * 60.0, "end_offset_seconds": (i + 1) * 60.0,
             "segment_id": f"seg_{i}", "source_id": f"src_{i}"}
            for i, (ts, text) in enumerate(base[:n])]


def _aggregate(processor, event, rows=None):
    with patch("life_logger.session_aggregator.call_magpie_deepseek", return_value={"events": [event]}):
        return processor._aggregate_session_level_events(
            device_id="watch_test", session_id="sess_test", rows=rows or _watch_rows(),
            existing_events=[], config={"session_aggregation": {"enabled": True,
                                                                "min_duration_minutes": 1, "min_rows": 5}},
        )


def test_watch_merge_preserves_time_isolation_contract_fields():
    """独立 probe 同输入：watch 合并不得把 pending_review 洗白成 reviewed。"""
    merged, stats = _aggregate(_watch_processor(), exercise_event("2026-05-29T09:00:00+08:00"))
    assert len(merged) == 1
    event = merged[0]
    assert event["review_status"] == "pending_review"
    assert event["pending_reason"] == "time_outside_session"
    assert event["model_reported_time"] == "2026-05-29T09:00:00+08:00"
    assert event["time"]["absolute"] is None and event["time"]["precision"] == "unknown"
    evidence = event["evidence"]
    assert evidence["session_bounds"]["local_dates"] == ["2026-10-24", "2026-10-25"]
    assert evidence["session_source_ids"][:3] == ["src_0", "src_1", "src_2"]
    assert evidence["session_segment_ids"][:3] == ["seg_0", "seg_1", "seg_2"]
    assert stats["time_isolated"] == 1
    # 多组动作/重量未被洗掉
    sets = event["data"]["exercise"]["exercises"][0]["sets"]
    assert [s["weight_kg"] for s in sets] == [40, 50]


def test_watch_merge_legal_event_keeps_old_review_rules():
    merged, _stats = _aggregate(_watch_processor(), exercise_event("2026-10-25T00:10:00+08:00"))
    assert merged[0]["review_status"] == "reviewed"
    assert "pending_reason" not in merged[0]

    routine = exercise_event("2026-10-25T00:10:00+08:00")
    routine["categories"] = ["routine"]
    merged_routine, _ = _aggregate(_watch_processor(), routine)
    assert merged_routine[0]["review_status"] == "unreviewed", "合法 routine 仍降级为 unreviewed"


def test_watch_merge_does_not_copy_hostile_raw_fields():
    """只显式保留合同字段，不盲拷整个 raw（模型塞进来的未知字段不得进事件）。"""
    event = exercise_event("2026-05-29T09:00:00+08:00")
    event["__evil__"] = "rm -rf /"
    event["evidence"]["__evil_evidence__"] = "x"
    merged, _ = _aggregate(_watch_processor(), event)
    assert "__evil__" not in merged[0]
    assert "__evil_evidence__" not in merged[0]["evidence"]
