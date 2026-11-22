"""Tests for Stage-1 local candidate extractor and OpenClaw data contract."""

from life_logger.aggregate import build_daily_summary
from life_logger.candidate_extractor import CandidateExtractor
from life_logger.domain import TranscriptSegment


def segment(text: str, segment_id: str = "seg-1") -> TranscriptSegment:
    return TranscriptSegment(
        segment_id=segment_id,
        recording_id="rec-test",
        start_offset_seconds=10.0,
        end_offset_seconds=15.0,
        absolute_started_at="2026-09-28T17:30:00+08:00",
        absolute_ended_at="2026-09-28T17:30:05+08:00",
        text=text,
        source="funasr",
        language="zh",
        transcription_confidence=0.95,
    )


def test_fact_candidate_extracted_with_openclaw_payload() -> None:
    extractor = CandidateExtractor()
    events = extractor.extract(segment("今天花了五十块钱买的河马"))

    assert len(events) == 1
    event = events[0]
    assert event.categories == ["expense"]
    assert event.data["openclaw"]["candidate_type"] == "fact_candidate"
    assert event.data["openclaw"]["state"] == "completed"
    assert event.data["expense"]["amount"] == 50.0


def test_decision_restraint_not_buying_extracted_as_saving() -> None:
    extractor = CandidateExtractor()
    events = extractor.extract(segment("看了两千块的那个显示器，太贵了没买。"))

    assert len(events) == 1
    event = events[0]
    assert event.categories == ["expense"]
    assert event.activity_type == "decision_saving"
    assert event.data["openclaw"]["candidate_type"] == "decision_candidate"
    assert event.data["openclaw"]["subtype"] == "saving_restraint"
    assert event.data["openclaw"]["state"] == "negated"
    assert event.data["decision"]["action"] == "rejected_purchase"
    # Crucial: amount is None so financial total is never inflated
    assert event.data["expense"]["amount"] is None


def test_plan_cancellation_extracted_as_decision_candidate() -> None:
    extractor = CandidateExtractor()
    events = extractor.extract(segment("本来打算晚上去跑步，下雨了不去了。"))

    assert len(events) == 1
    event = events[0]
    assert event.categories == ["routine"]
    assert event.activity_type == "plan_cancelled"
    assert event.data["openclaw"]["candidate_type"] == "decision_candidate"
    assert event.data["openclaw"]["subtype"] == "plan_cancelled"


def test_hypothetical_reasoning_extracted_as_reasoning_candidate() -> None:
    extractor = CandidateExtractor()
    events = extractor.extract(
        segment("如果后面用 ESP32 做网关，网络吞吐可能会受限，必须做轻量分包。")
    )

    assert len(events) == 1
    event = events[0]
    assert event.categories == ["idea"]
    assert event.activity_type == "hypothetical_reasoning"
    assert event.data["openclaw"]["candidate_type"] == "reasoning_candidate"
    assert event.data["openclaw"]["state"] == "hypothetical"
    assert "ESP32" in event.data["reasoning"]["hypothesis"]


def test_curiosity_question_extracted_as_question_candidate() -> None:
    extractor = CandidateExtractor()
    events = extractor.extract(
        segment("AirPassport 的蓝牙低功耗广播间隔最短能设多少毫秒？")
    )

    assert len(events) == 1
    event = events[0]
    assert event.categories == ["idea"]
    assert event.activity_type == "curiosity_question"
    assert event.data["openclaw"]["candidate_type"] == "question_candidate"
    assert event.data["openclaw"]["state"] == "question"


def test_pure_chitchat_is_filtered_out() -> None:
    extractor = CandidateExtractor()
    events = extractor.extract(segment("嗯，对对对，好的。"))

    assert events == []


def test_decision_saving_does_not_inflate_expense_total_cny() -> None:
    extractor = CandidateExtractor()
    events = []
    events.extend(extractor.extract(segment("今天花了五十块钱买河马", "s1")))
    events.extend(extractor.extract(segment("两千块的显示器太贵了没买", "s2")))

    assert len(events) == 2
    summary = build_daily_summary(
        events,
        date="2026-09-28",
        timezone_name="Asia/Shanghai",
        warnings=[],
    )

    # 50 was spent; 2000 was restrained. Total must strictly be 50.0!
    assert summary["expense_total_cny"] == 50.0
    assert summary["event_count"] == 2
