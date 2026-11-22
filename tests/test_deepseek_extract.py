"""Offline DeepSeek extraction tests with stubbed completions and test-only keys."""

from __future__ import annotations

import json
from pathlib import Path

from life_logger.domain import TranscriptSegment
from life_logger.local_llm_extract import (
    DeepSeekEventExtractor,
    _deepseek_amount_is_grounded,
)
from life_logger.local_pipeline import local_pipeline_env

TEST_KEY = "test-only-deepseek-key-do-not-use"


def transcript(segment_id: str, text: str) -> TranscriptSegment:
    return TranscriptSegment(
        segment_id=segment_id,
        recording_id="rec-2026-10-02",
        start_offset_seconds=123.0,
        end_offset_seconds=130.0,
        absolute_started_at="2026-10-02T18:17:00+08:00",
        absolute_ended_at="2026-10-02T18:17:07+08:00",
        text=text,
        source="test",
        language="zh",
        transcription_confidence=0.91,
    )


class Response:
    def __init__(self, content: dict[str, object]) -> None:
        self.body = json.dumps(
            {
                "choices": [
                    {"message": {"content": json.dumps(content, ensure_ascii=False)}}
                ]
            },
            ensure_ascii=False,
        ).encode("utf-8")

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def read(self) -> bytes:
        return self.body


def extractor_for(content: dict[str, object], captured: dict[str, object]):
    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return Response(content)

    return DeepSeekEventExtractor(
        enabled=True,
        api_key=TEST_KEY,
        model="deepseek-flash",
        opener=opener,
    )


def test_deepseek_finds_supermarket_expense_omitted_by_rules() -> None:
    captured: dict[str, object] = {}
    extractor = extractor_for(
        {
            "is_event": True,
            "categories": ["expense"],
            "title": "朴朴超市购买一周食材",
            "activity_type": "purchase",
            "confidence": 0.96,
            "event_state": "completed",
            "claim_type": "event",
            "data": {
                "expense": {
                    "amount": 140,
                    "currency": "CNY",
                    "item": "朴朴超市食材",
                    "amount_evidence": "一百四",
                    "transaction_evidence": "一百四买的朴朴超市",
                }
            },
        },
        captured,
    )

    events = extractor.extract(
        transcript("supermarket", "一百四买的朴朴超市，买一周的食材吧。")
    )

    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["authorization"] == f"Bearer {TEST_KEY}"
    assert captured["body"]["model"] == "deepseek-flash"
    assert len(events) == 1
    assert events[0].categories == ["expense"]
    assert events[0].data["expense"]["amount"] == 140
    assert events[0].evidence["quote"] == "一百四买的朴朴超市，买一周的食材吧。"
    assert events[0].evidence["segment_id"] == "supermarket"
    assert events[0].evidence["extractor"] == "deepseek_cloud"


def test_deepseek_uses_net_paid_amount_for_chicken() -> None:
    captured: dict[str, object] = {}
    extractor = extractor_for(
        {
            "is_event": True,
            "categories": ["expense"],
            "title": "购买炸鸡，返款后实付16元",
            "activity_type": "purchase",
            "confidence": 0.95,
            "event_state": "completed",
            "claim_type": "event",
            "data": {
                "expense": {
                    "amount": 16,
                    "currency": "CNY",
                    "item": "炸鸡",
                    "amount_evidence": "十六块钱",
                    "transaction_evidence": "总共十六块钱的炸鸡",
                }
            },
        },
        captured,
    )

    events = extractor.extract(
        transcript(
            "chicken",
            "然后还有三十块钱的炸鸡，再返给十四，总共十六块钱的炸鸡啊，别算错了。",
        )
    )

    assert len(events) == 1
    assert events[0].data["expense"]["amount"] == 16
    assert events[0].evidence["quote"].startswith("然后还有三十块钱的炸鸡")


def test_deepseek_rejects_speculative_amount_as_expense() -> None:
    captured: dict[str, object] = {}
    extractor = extractor_for({"is_event": False}, captured)

    events = extractor.extract(
        transcript(
            "speculative",
            "这就是人性，利益永远是最高的高科技，只要我们持续让他赚到钱，这个可能会就是一百块。",
        )
    )

    assert events == []
    assert captured["body"]["messages"][0]["content"]


def test_model_cannot_invent_amount_not_grounded_in_transcript() -> None:
    captured: dict[str, object] = {}
    extractor = extractor_for(
        {
            "is_event": True,
            "categories": ["expense"],
            "title": "超市消费",
            "activity_type": "purchase",
            "confidence": 0.96,
            "data": {"expense": {"amount": 999, "currency": "CNY", "item": "食材"}},
        },
        captured,
    )

    events = extractor.extract(transcript("invented", "我去超市买了一周的食材。"))

    assert events
    assert all(event.evidence["review_required"] is True for event in events)
    assert all(event.data.get("expense", {}).get("amount") is None for event in events)


def test_single_digit_substring_cannot_ground_amount_inside_larger_number() -> None:
    captured: dict[str, object] = {}
    extractor = extractor_for(
        {
            "is_event": True,
            "categories": ["expense"],
            "title": "超市食材",
            "activity_type": "purchase",
            "confidence": 0.96,
            "data": {"expense": {"amount": 4, "currency": "CNY", "item": "食材"}},
        },
        captured,
    )

    events = extractor.extract(
        transcript("digit-substring", "我在朴朴超市买了一周食材，实付140元。")
    )

    assert events
    assert all(event.data.get("expense", {}).get("amount") != 4 for event in events)


def test_colloquial_yibaisi_is_140_not_104() -> None:
    captured: dict[str, object] = {}
    extractor = extractor_for(
        {
            "is_event": True,
            "categories": ["expense"],
            "title": "朴朴超市购买食材",
            "activity_type": "purchase",
            "confidence": 0.96,
            "event_state": "completed",
            "claim_type": "event",
            "data": {
                "expense": {
                    "amount": 140,
                    "currency": "CNY",
                    "item": "食材",
                    "amount_evidence": "一百四",
                    "transaction_evidence": "一百四买的朴朴超市",
                }
            },
        },
        captured,
    )

    events = extractor.extract(
        transcript("colloquial-amount", "一百四买的朴朴超市，买一周的食材吧。")
    )

    assert len(events) == 1
    assert events[0].data["expense"]["amount"] == 140
    assert events[0].evidence["expense_model_contract"]["status"] == "accepted"


def test_amount_grounding_matches_full_chinese_and_decimal_values() -> None:
    assert _deepseek_amount_is_grounded("实付一百四元", 140)
    assert not _deepseek_amount_is_grounded("实付一百四元", 104)
    assert _deepseek_amount_is_grounded("实付140.50元", 140.5)
    assert not _deepseek_amount_is_grounded("实付140.50元", 140)


def test_malformed_deepseek_payloads_are_not_accepted_as_cloud_events() -> None:
    malformed_payloads = ({"is_event": True, "data": None}, {"categories": ["expense"]})
    for index, content in enumerate(malformed_payloads):
        captured: dict[str, object] = {}
        extractor = extractor_for(content, captured)
        events = extractor.extract(
            transcript(f"malformed-{index}", "九块钱买了一包烟，哎呦。")
        )

        assert events
        assert all(
            event.evidence.get("extractor") != "deepseek_cloud" for event in events
        )
        assert all(event.evidence.get("review_required") is True for event in events)


def test_unavailable_deepseek_marks_existing_rule_event_for_review() -> None:
    def fail_opener(*_args, **_kwargs):
        raise OSError("offline stub")

    extractor = DeepSeekEventExtractor(
        enabled=True, api_key=TEST_KEY, opener=fail_opener
    )
    events = extractor.extract(transcript("fallback", "九块钱买了一包烟，哎呦。"))

    assert events
    assert all(event.evidence["deepseek_review"] == "unavailable" for event in events)
    assert all(event.evidence["review_required"] is True for event in events)
    # Until a human/model confirms an unavailable result, statistics must not count it.
    assert all(event.data.get("expense", {}).get("amount") is None for event in events)
    assert all(
        event.evidence["unverified_expense_data"]["amount"] == 9
        for event in events
        if "expense" in event.categories
    )


def test_pipeline_passes_cloud_key_through_environment_not_argv() -> None:
    env = local_pipeline_env({"_deepseek_api_key": TEST_KEY})

    assert env["DEEPSEEK_API_KEY"] == TEST_KEY
    # This check protects the command-line contract: the key is supplied by env only.
    from life_logger.local_pipeline import build_asr_command

    command = build_asr_command(
        audio=Path("recording.m4a"),
        output_dir=Path("output"),
        config={"asr": "asr", "vad": "vad", "cpu_threads": 2},
    )
    assert TEST_KEY not in command
    assert "DEEPSEEK_API_KEY" not in command
