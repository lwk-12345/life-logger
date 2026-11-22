import json

import pytest

from life_logger.domain import TranscriptSegment
from life_logger.local_llm_extract import LocalLLMEventExtractor


def transcript(text: str) -> TranscriptSegment:
    return TranscriptSegment(
        segment_id="seg-local-llm",
        recording_id="rec-local-llm",
        start_offset_seconds=123.0,
        end_offset_seconds=130.0,
        absolute_started_at="2026-09-21T08:02:03+08:00",
        absolute_ended_at="2026-09-21T08:02:10+08:00",
        text=text,
        source="test",
        language="zh",
        transcription_confidence=0.91,
    )


class Response:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._body


def test_disabled_local_llm_uses_rule_extractor_without_network() -> None:
    def fail_opener(*_args, **_kwargs):
        raise AssertionError("disabled extractor must not call Ollama")

    extractor = LocalLLMEventExtractor(
        enabled=False,
        model="qwen2.5:3b",
        opener=fail_opener,
    )

    events = extractor.extract(transcript("今天早上八点起床了。"))

    assert len(events) == 1
    assert events[0].categories == ["routine"]
    assert "extractor" not in events[0].evidence


def test_high_confidence_rule_event_can_be_refined_by_local_llm() -> None:
    captured: dict[str, object] = {}

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["request"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return Response(
            {
                "response": json.dumps(
                    {
                        "is_event": True,
                        "categories": ["expense"],
                        "title": "花16元买了一包短剧内容",
                        "activity_type": "purchase",
                        "confidence": 0.94,
                        "data": {
                            "expense": {
                                "amount": 16,
                                "currency": "CNY",
                                "item": "一包短剧内容",
                            }
                        },
                    },
                    ensure_ascii=False,
                )
            }
        )

    extractor = LocalLLMEventExtractor(
        enabled=True,
        model="qwen2.5:3b",
        opener=opener,
    )
    events = extractor.extract(
        transcript("我刚才花16买了一包短剧的乐土，第一句就该检测到消费行为。")
    )

    assert len(events) == 1
    event = events[0]
    assert captured["url"] == "http://127.0.0.1:11434/api/generate"
    assert captured["request"]["model"] == "qwen2.5:3b"
    assert event.categories == ["expense"]
    assert event.data["expense"]["amount"] == 16
    assert event.confidence == 0.94
    assert event.evidence["quote"].startswith("我刚才花16")
    assert event.evidence["segment_id"] == "seg-local-llm"
    assert event.evidence["start_offset_seconds"] == 123.0
    assert event.evidence["extractor"] == "local_llm_second_pass"
    assert event.evidence["rule_baseline_confidence"] == 0.95


def test_unreachable_local_llm_falls_back_to_rule_result() -> None:
    def opener(*_args, **_kwargs):
        raise OSError("ollama offline")

    extractor = LocalLLMEventExtractor(
        enabled=True,
        model="qwen2.5:3b",
        opener=opener,
    )

    events = extractor.extract(transcript("下午跑步三十分钟。"))

    assert len(events) == 1
    assert events[0].categories == ["exercise"]
    assert events[0].data["exercise"]["duration_minutes"] == 30
    assert events[0].confidence == 0.92


def test_simple_unsupported_text_does_not_spend_local_llm_call() -> None:
    def fail_opener(*_args, **_kwargs):
        raise AssertionError("simple unsupported text should remain on rule path")

    extractor = LocalLLMEventExtractor(
        enabled=True,
        model="qwen2.5:3b",
        opener=fail_opener,
    )

    assert extractor.extract(transcript("今天风很大。")) == []


def test_complex_fragment_rejected_by_rules_never_gets_promoted_by_local_llm() -> None:
    def fail_opener(*_args, **_kwargs):
        raise AssertionError(
            "transcript-only fragments must not be promoted by the LLM"
        )

    extractor = LocalLLMEventExtractor(
        enabled=True,
        model="qwen2.5:3b",
        opener=fail_opener,
    )

    assert extractor.extract(
        transcript("然后这个的话，其实我觉得可能先看看，后面再说也行。")
    ) == []


def test_local_llm_refuses_non_localhost_endpoint() -> None:
    with pytest.raises(ValueError, match="localhost"):
        LocalLLMEventExtractor(
            enabled=True,
            model="qwen2.5:3b",
            base_url="https://example.com",
        )
