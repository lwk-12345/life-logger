"""Offline tests for the source-to-contextual-event pipeline.

DeepSeek is always stubbed here. No network or live API key is used.
"""

from __future__ import annotations

import json
import socket
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError

import pytest

from life_logger.contextual_extraction import (
    context_batch_input_key,
    read_context_batch_attempt_summaries,
    run_contextual_extraction,
)
from life_logger.expense_policy import build_expense_ledger
from life_logger.intent_pool import ActiveIntentPool

TEST_KEY = "test-only-contextual-key"


@pytest.fixture(autouse=True)
def isolated_intent_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("LIFE_LOGGER_INTENT_POOL_PATH", str(tmp_path / "intents.json"))


class Response:
    def __init__(self, content: dict[str, Any]) -> None:
        self.body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(content, ensure_ascii=False)
                        }
                    }
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


def source(
    segment_id: str,
    text: str,
    *,
    source_label: str = "self",
    recording_id: str = "rec-2026-10-02",
    start: float = 100.0,
    is_dictation: bool = False,
) -> dict[str, Any]:
    return {
        "recording_id": recording_id,
        "segment_id": segment_id,
        "absolute_started_at": "2026-10-02T18:17:15+08:00",
        "start_offset_seconds": start,
        "end_offset_seconds": start + 5,
        "source_label": source_label,
        "review_priority": "high",
        "is_dictation": is_dictation,
        "text": text,
    }


def chat_stub(
    response_factory: Callable[[dict[str, Any], int], dict[str, Any]],
    captured: list[dict[str, Any]],
    *, capture_sources=None,
):
    def opener(request, timeout):
        body = json.loads(request.data.decode("utf-8"))
        captured.append(body)
        if capture_sources is not None:
            packets = json.loads(body["messages"][0]["content"].rsplit("输入记录：\n", 1)[1])
            capture_sources(packets)
        return Response(response_factory(body, len(captured) - 1))

    return opener


def request_texts(bodies: list[dict[str, Any]]) -> str:
    return json.dumps(bodies, ensure_ascii=False)


def test_direct_extraction_keeps_every_attribution_record() -> None:
    records = [
        source(f"seg-{index}", f"来源段落{index}，保留为上下文。", start=index * 10.0)
        for index in range(5)
    ]
    submitted_sources: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = []

    def capture_sources(batch: list[dict[str, Any]]) -> dict[str, Any]:
        submitted_sources.extend(batch)

    result = run_contextual_extraction(
        records,
        deepseek_api_key=TEST_KEY,
        batch_size=2,
        deepseek_opener=chat_stub(lambda _body, _index: {"records": []}, requests, capture_sources=capture_sources),
    )

    submitted_text = request_texts(requests)
    assert len(submitted_sources) == len(records)
    assert all(item["text"] in submitted_text for item in records)
    source_ids = {item["source_id"] for item in submitted_sources}
    batch_ids = [source_id for batch in result["batches"] for source_id in batch["source_ids"]]
    assert set(batch_ids) == source_ids
    assert len(batch_ids) == len(set(batch_ids)) == len(records)
    assert len(result["batches"]) == 3
    assert "kev" not in result


def test_one_self_source_can_yield_two_grounded_formal_expenses() -> None:
    original = (
        "一百四买的朴朴超市，买一周的食材吧。然后还有三十块钱的炸鸡，"
        "再返给十四，总共十六块钱的炸鸡啊，别算错了。"
    )
    records = [source("seg-finance", original)]
    ids_by_text: dict[str, str] = {}
    requests: list[dict[str, Any]] = []

    def capture_sources(batch: list[dict[str, Any]]) -> dict[str, Any]:
        for item in batch:
            ids_by_text[item["text"]] = item["source_id"]

    def response(_body: dict[str, Any], _index: int) -> dict[str, Any]:
        source_id = ids_by_text[original]
        return {
            "records": [
                {
                    "source_id": source_id,
                    "is_event": True,
                    "events": [
                        {
                            "category": "expense",
                            "event_state": "completed",
                            "title": "朴朴超市食材",
                            "activity_type": "purchase",
                            "quote": "一百四买的朴朴超市，买一周的食材吧。",
                            "confidence": 0.96,
                            "claim_type": "event",
                            "data": {
                                "expense": {
                                    "amount": 140,
                                    "currency": "CNY",
                                    "item": "一周食材",
                                    "amount_evidence": "一百四",
                                    "transaction_evidence": "一百四买的朴朴超市",
                                }
                            },
                        },
                        {
                            "category": "expense",
                            "event_state": "completed",
                            "title": "炸鸡实付16元",
                            "activity_type": "purchase",
                            "quote": "然后还有三十块钱的炸鸡，再返给十四，总共十六块钱的炸鸡",
                            "confidence": 0.95,
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
                    ],
                }
            ]
        }

    result = run_contextual_extraction(
        records,
        deepseek_api_key=TEST_KEY,
        deepseek_opener=chat_stub(response, requests, capture_sources=capture_sources),
    )

    expenses = [event for event in result["formal_events"] if "expense" in event["categories"]]
    assert [event["data"]["expense"]["amount"] for event in expenses] == [140, 16]
    assert [event["evidence"]["quote"] for event in expenses] == [
        "一百四买的朴朴超市，买一周的食材吧。",
        "然后还有三十块钱的炸鸡，再返给十四，总共十六块钱的炸鸡",
    ]
    assert all(event["evidence"]["segment_id"] == "seg-finance" for event in expenses)
    assert all(event["evidence"]["source_id"] == ids_by_text[original] for event in expenses)


def test_unknown_expense_is_pending_and_nonself_remains_context_only() -> None:
    unknown_text = "有人说可能花一百块買东西，但还没有确认。"
    nonself_text = "视频里提到花费二百元购买设备。"
    records = [
        source("seg-unknown", unknown_text, source_label="unknown", start=10.0),
        source("seg-video", nonself_text, source_label="non_self", start=20.0),
    ]
    ids_by_text: dict[str, str] = {}
    requests: list[dict[str, Any]] = []

    def capture_sources(batch: list[dict[str, Any]]) -> dict[str, Any]:
        for item in batch:
            ids_by_text[item["text"]] = item["source_id"]

    def response(_body: dict[str, Any], _index: int) -> dict[str, Any]:
        return {
            "records": [
                {
                    "source_id": source_id,
                    "is_event": True,
                    "events": [
                        {
                            "category": "expense",
                            "event_state": "completed",
                            "title": "提及消费",
                            "activity_type": "purchase",
                            "quote": text,
                            "confidence": 0.95,
                            "data": {"expense": {"amount": 100, "item": "东西"}},
                        }
                    ],
                }
                for text, source_id in ids_by_text.items()
            ]
        }

    result = run_contextual_extraction(
        records,
        deepseek_api_key=TEST_KEY,
        deepseek_opener=chat_stub(response, requests, capture_sources=capture_sources),
    )

    formal_ids = {event["evidence"]["source_id"] for event in result["formal_events"]}
    assert ids_by_text[nonself_text] not in formal_ids
    nonself_candidates = [
        event for event in result["pending_events"]
        if event["evidence"]["source_id"] == ids_by_text[nonself_text]
    ]
    assert len(nonself_candidates) == 1
    assert nonself_candidates[0]["evidence"]["pending_reason"] == "non_self_review"
    assert nonself_candidates[0]["data"]["expense"]["amount"] is None
    unknown_events = [
        event
        for event in result["pending_events"]
        if event["evidence"]["source_id"] == ids_by_text[unknown_text]
    ]
    assert unknown_events
    assert all(event["data"]["expense"]["amount"] is None for event in unknown_events)
    # No invented occurrence time for unknown/non-self sources.
    assert all(event["time"].get("absolute") is None for event in result["pending_events"])
    assert ids_by_text[nonself_text] not in {
        event["evidence"]["source_id"] for event in result["formal_events"]
    }
    assert nonself_text in request_texts(requests)
    assert len(result["source_outcomes"]) == len(records)
    nonself_outcome = next(
        item for item in result["source_outcomes"]
        if item["source_id"] == ids_by_text[nonself_text]
    )
    assert nonself_outcome["semantic_status"] == "pending"
    assert nonself_outcome["event_refs"] == [nonself_candidates[0]["event_id"]]


def test_novel_category_is_preserved_as_reviewable_other_event() -> None:
    text = "我开始照顾阳台上的植物了，每天浇水。"
    records = [source("seg-novel-topic", text)]
    source_id: str | None = None
    requests: list[dict[str, Any]] = []

    def capture_sources(batch: list[dict[str, Any]]) -> dict[str, Any]:
        nonlocal source_id
        source_id = batch[0]["source_id"]

    def response(_body: dict[str, Any], _index: int) -> dict[str, Any]:
        assert source_id is not None
        return {"records": [{
            "source_id": source_id,
            "is_event": True,
            "events": [{
                "category": "gardening", "topic": "照料阳台植物", "claim_type": "routine",
                "event_state": "ongoing", "title": "照顾阳台植物", "activity_type": "care",
                "quote": "每天浇水", "confidence": 0.92,
                "data": {"gardening": {"plant": "阳台植物"}},
            }],
        }]}

    result = run_contextual_extraction(
        records, deepseek_api_key=TEST_KEY,
        deepseek_opener=chat_stub(response, requests, capture_sources=capture_sources),
    )
    assert len(result["formal_events"]) == 1
    candidate = result["formal_events"][0]
    assert candidate["categories"] == ["other"]
    assert candidate["evidence"]["proposed_category"] == "gardening"
    assert candidate["evidence"]["topic"] == "照料阳台植物"
    assert candidate["evidence"]["claim_type"] == "routine"
    assert result["source_outcomes"][0]["semantic_status"] == "processed"


def test_distinct_novel_topics_from_same_source_do_not_share_event_ids() -> None:
    text = "我在阳台上照顾植物，每天浇水。"
    records = [source("seg-multiple-novel-topics", text)]
    source_id: str | None = None
    requests: list[dict[str, Any]] = []

    def capture_sources(batch: list[dict[str, Any]]) -> dict[str, Any]:
        nonlocal source_id
        source_id = batch[0]["source_id"]

    def response(_body: dict[str, Any], _index: int) -> dict[str, Any]:
        assert source_id is not None
        common = {
            "event_state": "ongoing", "title": "生活记录", "activity_type": "routine",
            "quote": "每天浇水", "confidence": 0.92,
        }
        return {"records": [{
            "source_id": source_id,
            "is_event": True,
            "events": [
                {**common, "category": "gardening", "topic": "照料植物", "claim_type": "routine"},
                {**common, "category": "gardening", "topic": "阳台园艺", "claim_type": "routine"},
                {**common, "category": "birdwatching", "topic": "照料植物", "claim_type": "routine"},
            ],
        }]}

    result = run_contextual_extraction(
        records, deepseek_api_key=TEST_KEY,
        deepseek_opener=chat_stub(response, requests, capture_sources=capture_sources),
    )
    assert len(result["formal_events"]) == 3
    assert len({event["event_id"] for event in result["formal_events"]}) == 3
    assert {event["evidence"]["topic"] for event in result["formal_events"]} == {
        "照料植物", "阳台园艺",
    }


def test_source_outcome_keeps_pending_when_another_event_is_formal() -> None:
    text = "我计划减肥，今天吃了午饭。"
    records = [source("seg-mixed-source-outcome", text)]
    source_id: str | None = None
    requests: list[dict[str, Any]] = []

    def capture_sources(batch: list[dict[str, Any]]) -> dict[str, Any]:
        nonlocal source_id
        source_id = batch[0]["source_id"]

    def response(_body: dict[str, Any], _index: int) -> dict[str, Any]:
        assert source_id is not None
        return {"records": [{
            "source_id": source_id,
            "is_event": True,
            "events": [
                {
                    "category": "todo", "topic": "减肥", "claim_type": "plan",
                    "event_state": "planned", "title": "减肥计划", "activity_type": "plan",
                    "quote": "我计划减肥", "confidence": 0.91, "data": {"todo": {}},
                },
                {
                    "category": "food", "topic": "午餐", "claim_type": "event",
                    "event_state": "completed", "title": "吃午饭", "activity_type": "meal",
                    "quote": "今天吃了午饭", "confidence": 0.95, "data": {"food": {}},
                },
            ],
        }]}

    result = run_contextual_extraction(
        records, deepseek_api_key=TEST_KEY,
        deepseek_opener=chat_stub(response, requests, capture_sources=capture_sources),
    )
    outcome = result["source_outcomes"][0]
    assert len(result["pending_events"]) == 1
    assert len(result["formal_events"]) == 1
    assert outcome["semantic_status"] == "pending"
    assert outcome["reason"] == "non_confirmed_state"


def test_open_category_plans_are_pending_and_registered_with_source_and_target(
    tmp_path: Path,
) -> None:
    text = "我想制定英语健身计划，每周练三次，也计划学习营养。"
    records = [source("seg-fitness-plan", text)]
    pool = ActiveIntentPool(tmp_path / "active-intents.json")
    source_id: str | None = None
    requests: list[dict[str, Any]] = []

    def capture_sources(batch: list[dict[str, Any]]) -> dict[str, Any]:
        nonlocal source_id
        source_id = batch[0]["source_id"]

    def response(_body: dict[str, Any], _index: int) -> dict[str, Any]:
        assert source_id is not None
        return {"records": [{
            "source_id": source_id,
            "is_event": True,
            "events": [{
                "category": "fitness_plan", "topic": "英语健身计划",
                "claim_type": "plan", "event_state": "planned",
                "title": "英语健身计划", "activity_type": "plan",
                "quote": "我想制定英语健身计划，每周练三次",
                "confidence": 0.93,
            }, {
                "category": "nutrition_plan", "topic": "营养学习计划",
                "claim_type": "plan", "event_state": "planned",
                "title": "营养学习计划", "activity_type": "plan",
                "quote": "也计划学习营养",
                "confidence": 0.91,
            }],
        }]}

    result = run_contextual_extraction(
        records, deepseek_api_key=TEST_KEY, intent_pool=pool,
         deepseek_opener=chat_stub(response, requests, capture_sources=capture_sources),
    )

    assert result["formal_events"] == []
    assert len(result["pending_events"]) == 2
    assert all(event["evidence"]["event_state"] == "planned" for event in result["pending_events"])
    assert len(result["active_intents"]) == 2
    intents_by_category = {
        item["metadata"]["proposed_category"]: item
        for item in result["active_intents"]
    }
    assert set(intents_by_category) == {"fitness_plan", "nutrition_plan"}
    for intent in result["active_intents"]:
        assert intent["status"] == "pending"
        assert intent["target_entity"]
        assert intent["metadata"]["source_id"] == source_id
        assert intent["metadata"]["recording_id"] == "rec-2026-10-02"
        assert intent["metadata"]["source_label"] == "self"
        assert intent["metadata"]["topic"]
        assert intent["metadata"]["claim_type"] == "plan"
    assert {item["intent_id"] for item in pool.list_pending_intents()} == {
        item["intent_id"] for item in result["active_intents"]
    }


def test_ungrounded_planned_event_does_not_register_active_intent(
    tmp_path: Path,
) -> None:
    text = "今天记录一段日常语音。"
    records = [source("seg-untrusted-plan", text)]
    pool = ActiveIntentPool(tmp_path / "active-intents.json")
    source_id: str | None = None
    requests: list[dict[str, Any]] = []

    def capture_sources(batch: list[dict[str, Any]]) -> dict[str, Any]:
        nonlocal source_id
        source_id = batch[0]["source_id"]

    def response(_body: dict[str, Any], _index: int) -> dict[str, Any]:
        assert source_id is not None
        return {"records": [{
            "source_id": source_id,
            "is_event": True,
            "events": [{
                "category": "fitness_plan", "topic": "英语健身计划",
                "claim_type": "plan", "event_state": "planned",
                "title": "英语健身计划", "activity_type": "plan",
                "quote": "我承诺每天跑十公里",
                "confidence": 0.93,
            }],
        }]}

    result = run_contextual_extraction(
        records, deepseek_api_key=TEST_KEY, intent_pool=pool,
         deepseek_opener=chat_stub(response, requests, capture_sources=capture_sources),
    )

    assert result["active_intents"] == []
    assert pool.list_pending_intents() == []


def test_low_confidence_planned_event_does_not_register_active_intent(
    tmp_path: Path,
) -> None:
    text = "今天记录一段日常语音。"
    records = [source("seg-low-confidence-plan", text)]
    pool = ActiveIntentPool(tmp_path / "active-intents.json")
    source_id: str | None = None
    requests: list[dict[str, Any]] = []

    def capture_sources(batch: list[dict[str, Any]]) -> dict[str, Any]:
        nonlocal source_id
        source_id = batch[0]["source_id"]

    def response(_body: dict[str, Any], _index: int) -> dict[str, Any]:
        assert source_id is not None
        return {"records": [{
            "source_id": source_id,
            "is_event": True,
            "events": [{
                "category": "fitness_plan", "topic": "英语健身计划",
                "claim_type": "plan", "event_state": "planned",
                "title": "英语健身计划", "activity_type": "plan",
                "quote": text,
                "confidence": 0.79,
            }],
        }]}

    result = run_contextual_extraction(
        records, deepseek_api_key=TEST_KEY, intent_pool=pool,
         deepseek_opener=chat_stub(response, requests, capture_sources=capture_sources),
    )

    assert result["active_intents"] == []
    assert pool.list_pending_intents() == []


def test_unknown_speaker_plan_stays_pending_without_active_intent(
    tmp_path: Path,
) -> None:
    text = "我想制定英语健身计划，每周练三次。"
    records = [source("seg-unknown-plan", text, source_label="unknown")]
    pool = ActiveIntentPool(tmp_path / "active-intents.json")
    source_id: str | None = None
    requests: list[dict[str, Any]] = []

    def capture_sources(batch: list[dict[str, Any]]) -> dict[str, Any]:
        nonlocal source_id
        source_id = batch[0]["source_id"]

    def response(_body: dict[str, Any], _index: int) -> dict[str, Any]:
        assert source_id is not None
        return {"records": [{
            "source_id": source_id,
            "is_event": True,
            "events": [{
                "category": "fitness_plan", "topic": "英语健身计划",
                "claim_type": "plan", "event_state": "planned",
                "title": "英语健身计划", "activity_type": "plan",
                "quote": "我想制定英语健身计划，每周练三次",
                "confidence": 0.93,
            }],
        }]}

    result = run_contextual_extraction(
        records, deepseek_api_key=TEST_KEY, intent_pool=pool,
         deepseek_opener=chat_stub(response, requests, capture_sources=capture_sources),
    )

    assert result["formal_events"] == []
    assert len(result["pending_events"]) == 1
    assert result["pending_events"][0]["evidence"]["pending_reason"]
    assert result["active_intents"] == []
    assert pool.list_pending_intents() == []


def test_explicit_no_event_gets_one_complete_source_outcome() -> None:
    records = [source("seg-explicit-none", "今天聊了一句天气。")]
    requests: list[dict[str, Any]] = []
    source_id: str | None = None

    def capture_sources(batch: list[dict[str, Any]]) -> dict[str, Any]:
        nonlocal source_id
        source_id = batch[0]["source_id"]

    def response(_body: dict[str, Any], _index: int) -> dict[str, Any]:
        assert source_id is not None
        return {"records": [{"source_id": source_id, "is_event": False, "events": []}]}

    result = run_contextual_extraction(
        records, deepseek_api_key=TEST_KEY,
        deepseek_opener=chat_stub(response, requests, capture_sources=capture_sources),
    )
    assert len(result["source_outcomes"]) == 1
    assert result["source_outcomes"][0] == {
        "source_id": source_id,
        "segment_id": "seg-explicit-none",
        "recording_id": "rec-2026-10-02",
        "speaker_label": "self",
        "semantic_status": "no_event",
        "reason": "model_no_event",
        "event_refs": [],
    }


def test_relation_proposals_are_grounded_review_only_and_do_not_resolve_intents(tmp_path: Path) -> None:
    text = "今天练了二十分钟英语听力。"
    records = [source("seg-english-followup", text)]
    pool = ActiveIntentPool(tmp_path / "active-intents.json")
    intent = pool.register_intent(
        text="开始练英语听力", domain="Todos", target_entity="英语听力",
        # 【2026-10-17 修】原来写死 timestamp="2026-10-08T09:00:00+08:00"，
        # 而 ActiveIntentPool 有 48h TTL（DEFAULT_INTENT_TTL_HOURS）。测试跑在写死日期
        # 两天之后就会自动过期 → 意图不再被送进模型 → relation_candidates 恒为 0。
        # 这不是产品逻辑错，是**测试随墙钟腐烂**。改为相对当前时间，永远在 TTL 内。
        timestamp=(datetime.now(timezone.utc) - timedelta(hours=1)).astimezone().isoformat(),
        source_segment_id="seg-original-plan",
        metadata={"proposed_category": "language_practice", "topic": "英语听力练习", "claim_type": "plan"},
        quote="开始练英语听力", intent_id="intent-existing-english",
    )
    source_id: str | None = None
    requests: list[dict[str, Any]] = []

    def capture_sources(batch: list[dict[str, Any]]) -> dict[str, Any]:
        nonlocal source_id
        source_id = batch[0]["source_id"]

    def response(_body: dict[str, Any], _index: int) -> dict[str, Any]:
        assert source_id is not None
        return {"records": [{
            "source_id": source_id,
            "is_event": False,
            "events": [],
            "relations": [
                {"target_intent_id": intent["intent_id"], "relation_type": "progress_of",
                 "quote": "练了二十分钟英语听力", "confidence": 0.91},
                {"target_intent_id": "fabricated-intent", "relation_type": "supports",
                 "quote": "今天练了二十分钟英语听力。", "confidence": 0.99},
                {"target_intent_id": intent["intent_id"], "relation_type": "supports",
                 "quote": "我已经完成整个英语课程", "confidence": 0.99},
            ],
        }]}

    result = run_contextual_extraction(
        records, deepseek_api_key=TEST_KEY, intent_pool=pool,
         deepseek_opener=chat_stub(response, requests, capture_sources=capture_sources),
    )
    assert len(result["relation_candidates"]) == 1
    candidate = result["relation_candidates"][0]
    assert candidate["target_intent_id"] == intent["intent_id"]
    assert candidate["relation_type"] == "progress_of"
    assert candidate["quote"] == "练了二十分钟英语听力"
    assert candidate["review_status"] == "pending"
    assert candidate["candidate_id"].startswith("rel_ctx_")
    assert result["source_outcomes"][0]["relation_refs"] == [candidate["candidate_id"]]
    assert {item["decision"] for item in result["decisions"]} >= {
        "unknown_intent_target", "relation_quote_not_grounded",
    }
    assert pool.list_pending_intents() == [intent]
    request_payload = requests[0]["messages"][0]["content"]
    assert intent["intent_id"] in request_payload
    prompt_records = json.loads(request_payload.split("输入记录：\n", 1)[1])
    supplied_intent = prompt_records[0]["pending_intents"][0]
    assert supplied_intent["proposed_category"] == "language_practice"
    assert supplied_intent["topic"] == "英语听力练习"
    assert supplied_intent["claim_type"] == "plan"
    without_context = context_batch_input_key(records)
    with_context = context_batch_input_key(
        records, related_intents=[{
            "intent_id": intent["intent_id"], "text": intent["text"],
            "quote": intent["quote"], "created_at": intent["created_at"],
        }],
    )
    assert with_context != without_context


def test_bad_quote_and_unknown_source_reference_never_become_formal() -> None:
    text = "一百四买的朴朴超市，买一周的食材吧。"
    records = [source("seg-grounding", text)]
    source_id: str | None = None
    requests: list[dict[str, Any]] = []

    def capture_sources(batch: list[dict[str, Any]]) -> dict[str, Any]:
        nonlocal source_id
        source_id = batch[0]["source_id"]

    def response(_body: dict[str, Any], _index: int) -> dict[str, Any]:
        assert source_id is not None
        return {
            "records": [
                {
                    "source_id": source_id,
                    "is_event": True,
                    "events": [
                        {
                            "category": "expense",
                            "event_state": "completed",
                            "title": "虚构金额",
                            "activity_type": "purchase",
                            "quote": "原文没有这句话，买了999元商品。",
                            "confidence": 0.99,
                            "data": {"expense": {"amount": 999, "item": "商品"}},
                        }
                    ],
                },
                {
                    "source_id": "invented-source-id",
                    "is_event": True,
                    "events": [
                        {
                            "category": "expense",
                            "title": "串到别的来源",
                            "activity_type": "purchase",
                            "quote": text,
                            "confidence": 0.99,
                            "data": {"expense": {"amount": 140, "item": "食材"}},
                        }
                    ],
                },
            ]
        }

    result = run_contextual_extraction(
        records,
        deepseek_api_key=TEST_KEY,
        deepseek_opener=chat_stub(response, requests, capture_sources=capture_sources),
    )

    assert result["formal_events"] == []
    assert {item["decision"] for item in result["decisions"]} >= {
        "quote_not_grounded",
        "unknown_source_id",
    }


def test_identical_duplicate_events_from_same_source_are_deduplicated() -> None:
    text = "九块钱买了一包烟。"
    records = [source("seg-duplicate", text)]
    source_id: str | None = None
    requests: list[dict[str, Any]] = []

    def capture_sources(batch: list[dict[str, Any]]) -> dict[str, Any]:
        nonlocal source_id
        source_id = batch[0]["source_id"]

    def response(_body: dict[str, Any], _index: int) -> dict[str, Any]:
        assert source_id is not None
        event = {
            "category": "expense",
            "event_state": "completed",
            "title": "买了一包烟",
            "activity_type": "purchase",
            "quote": text,
            "confidence": 0.95,
            "claim_type": "event",
            "data": {
                "expense": {
                    "amount": 9,
                    "item": "烟",
                    "amount_evidence": "九块钱",
                    "transaction_evidence": "九块钱买了一包烟",
                }
            },
        }
        return {
            "records": [
                {
                    "source_id": source_id,
                    "is_event": True,
                    "events": [event, dict(event)],
                }
            ]
        }

    result = run_contextual_extraction(
        records,
        deepseek_api_key=TEST_KEY,
        deepseek_opener=chat_stub(response, requests, capture_sources=capture_sources),
    )

    assert len(result["formal_events"]) == 1
    assert result["formal_events"][0]["data"]["expense"]["amount"] == 9


def test_deepseek_timeout_marks_batch_sources_pending() -> None:
    records = [source("seg-timeout-a", "今天花了五十元买菜。"), source("seg-timeout-b", "下午跑步半小时。", start=20)]

    def timeout(*_args: Any, **_kwargs: Any) -> Response:
        raise TimeoutError("DeepSeek timeout stub")

    result = run_contextual_extraction(
        records,
        deepseek_api_key=TEST_KEY,
        deepseek_opener=timeout,
    )

    assert result["formal_events"] == []
    assert {item["source_id"] for item in result["pending_sources"]} == {
        batch_id
        for batch in result["batches"]
        for batch_id in batch["source_ids"]
    }
    assert all(batch["status"] != "completed" for batch in result["batches"])
    assert all(batch["failure_kind"] == "timeout" for batch in result["batches"])
    assert all(batch["retryable_transient"] is True for batch in result["batches"])
    assert {item["semantic_status"] for item in result["source_outcomes"]} == {"failed"}
    assert {item["reason"] for item in result["source_outcomes"]} == {"deepseek_batch_failed"}


def test_deepseek_http_failure_metadata_distinguishes_transient_statuses() -> None:
    for status, retryable in ((408, True), (429, True), (503, True), (400, False)):
        def fail(_request: Any, timeout: float, *, code: int = status) -> Response:
            raise HTTPError("https://api.deepseek.com/chat/completions", code, "stub", {}, None)

        result = run_contextual_extraction(
            [source(f"seg-http-{status}", "今天买了水。")],
            deepseek_api_key=TEST_KEY,
            deepseek_opener=fail,
        )
        batch = result["batches"][0]
        assert batch["failure_kind"] == "http_error"
        assert batch["http_status"] == status
        assert batch["retryable_transient"] is retryable


def test_missing_key_and_invalid_json_are_not_retryable() -> None:
    missing = run_contextual_extraction([source("seg-no-key", "今天买水。")], deepseek_api_key="")
    assert missing["batches"][0]["failure_kind"] == "missing_api_key"
    assert missing["batches"][0]["retryable_transient"] is False

    class InvalidJsonResponse(Response):
        def __init__(self) -> None:
            self.body = b"not-json"

    def invalid_json(_request: Any, timeout: float) -> InvalidJsonResponse:
        return InvalidJsonResponse()

    result = run_contextual_extraction(
        [source("seg-invalid-json", "今天买水。")],
        deepseek_api_key=TEST_KEY,
        deepseek_opener=invalid_json,
    )
    assert result["batches"][0]["failure_kind"] == "invalid_json"
    assert result["batches"][0]["retryable_transient"] is False


def test_context_extraction_reuses_only_matching_successful_batch_cache(tmp_path: Path) -> None:
    output_dir = tmp_path / "job-output"
    records = [source("seg-cache", "今天买了水。")]
    requests: list[dict[str, Any]] = []
    opener = chat_stub(lambda _body, _index: {"records": []}, requests)
    run_contextual_extraction(records, deepseek_api_key=TEST_KEY, output_dir=output_dir,
                               deepseek_opener=opener)
    run_contextual_extraction(records, deepseek_api_key=TEST_KEY, output_dir=output_dir,
                               deepseek_opener=opener)
    assert len(requests) == 1

    changed = [source("seg-cache", "今天买了两瓶水。")]
    run_contextual_extraction(changed, deepseek_api_key=TEST_KEY, output_dir=output_dir,
                               deepseek_opener=opener)
    assert len(requests) == 2

    run_contextual_extraction(records, deepseek_api_key=TEST_KEY, output_dir=output_dir,
                              deepseek_model="other-extractor-model", deepseek_opener=opener)
    assert len(requests) == 3


def test_corrupt_context_batch_cache_falls_back_to_request(tmp_path: Path) -> None:
    output_dir = tmp_path / "job-output"
    records = [source("seg-corrupt", "今天买了水。")]
    requests: list[dict[str, Any]] = []
    opener = chat_stub(lambda _body, _index: {"records": []}, requests)
    run_contextual_extraction(records, deepseek_api_key=TEST_KEY, output_dir=output_dir,
                               deepseek_opener=opener)
    cache_files = [
        path for path in (tmp_path / ".context-extraction-batches").glob("*.json")
        if not path.name.startswith("attempt-")
    ]
    assert len(cache_files) == 1
    cache_key = cache_files[0].stem
    attempts = read_context_batch_attempt_summaries(
        tmp_path / ".context-extraction-batches", [cache_key]
    )
    assert attempts == [{
        "input_key": cache_key,
        "status": "completed",
        "failure_kind": None,
        "http_status": None,
        "retryable_transient": False,
    }]
    cache_files[0].write_text("corrupt", encoding="utf-8")
    run_contextual_extraction(records, deepseek_api_key=TEST_KEY, output_dir=output_dir,
                               deepseek_opener=opener)
    assert len(requests) == 2


def test_dictation_without_explicit_transaction_is_not_formal() -> None:
    text = "这个可能会就是一百块。"
    records = [source("seg-dictation", text, is_dictation=True)]
    source_id: str | None = None
    requests: list[dict[str, Any]] = []

    def capture_sources(batch: list[dict[str, Any]]) -> dict[str, Any]:
        nonlocal source_id
        source_id = batch[0]["source_id"]

    def response(_body: dict[str, Any], _index: int) -> dict[str, Any]:
        assert source_id is not None
        return {
            "records": [
                {
                    "source_id": source_id,
                    "is_event": True,
                    "events": [
                        {
                            "category": "expense",
                            "event_state": "hypothetical",
                            "title": "一百块",
                            "activity_type": "purchase",
                            "quote": text,
                            "confidence": 0.99,
                            "data": {"expense": {"amount": 100, "item": "unknown"}},
                        }
                    ],
                }
            ]
        }

    result = run_contextual_extraction(
        records,
        deepseek_api_key=TEST_KEY,
        deepseek_opener=chat_stub(response, requests, capture_sources=capture_sources),
    )

    assert result["formal_events"] == []
    pending_ids = {
        item["source_id"] for item in result["pending_sources"]
    } | {
        event["evidence"]["source_id"] for event in result["pending_events"]
    }
    assert source_id in pending_ids


def test_voice_triggers_pinned_memo_and_query_dispatch_are_formal_events() -> None:
    records = [
        source("seg-1", "记一下买两包纸巾", start=1.0),
        source("seg-2", "我要查一下VPN怎么设置", start=2.0),
        source("seg-3", "记录下购买清单", start=3.0),
        source("seg-4", "检查一下我的OpenCloud", start=4.0),
    ]
    source_ids: list[str] = []
    requests: list[dict[str, Any]] = []

    def capture_sources(batch: list[dict[str, Any]]) -> dict[str, Any]:
        nonlocal source_ids
        source_ids = [b["source_id"] for b in batch]

    def response(_body: dict[str, Any], _index: int) -> dict[str, Any]:
        return {
            "records": [
                {
                    "source_id": source_ids[0],
                    "is_event": True,
                    "events": [
                        {
                            "category": "todo",
                            "event_state": "completed",
                            "title": "买两包纸巾",
                            "activity_type": "task",
                            "quote": "记一下买两包纸巾",
                            "confidence": 0.95,
                            "data": {},
                        }
                    ],
                },
                {
                    "source_id": source_ids[1],
                    "is_event": True,
                    "events": [
                        {
                            "category": "todo",
                            "event_state": "planned",
                            "title": "VPN怎么设置",
                            "activity_type": "task",
                            "quote": "我要查一下VPN怎么设置",
                            "confidence": 0.95,
                            "data": {},
                        }
                    ],
                },
                {
                    "source_id": source_ids[2],
                    "is_event": True,
                    "events": [
                        {
                            "category": "todo",
                            "event_state": "completed",
                            "title": "购买清单",
                            "activity_type": "task",
                            "quote": "记录下购买清单",
                            "confidence": 0.95,
                            "data": {},
                        }
                    ],
                },
                {
                    "source_id": source_ids[3],
                    "is_event": True,
                    "events": [
                        {
                            "category": "todo",
                            "event_state": "planned",
                            "title": "检查我的OpenCloud",
                            "activity_type": "task",
                            "quote": "检查一下我的OpenCloud",
                            "confidence": 0.95,
                            "data": {},
                        }
                    ],
                },
            ]
        }

    result = run_contextual_extraction(
        records,
        deepseek_api_key=TEST_KEY,
        deepseek_opener=chat_stub(response, requests, capture_sources=capture_sources),
    )

    assert len(result["formal_events"]) == 4
    titles = [ev["title"] for ev in result["formal_events"]]
    assert any("便签：买两包纸巾" in t for t in titles)
    assert any("查询：VPN怎么设置" in t for t in titles)
    assert any("便签：购买清单" in t for t in titles)
    assert any("查询：" in t and "OpenCloud" in t for t in titles)
    for ev in result["formal_events"]:
        assert ev["evidence"]["pending_reason"] is None
        assert ev["review_status"] == "reviewed"


def _run_single_expense_event(
    text: str,
    event: dict[str, Any],
    *,
    segment_id: str = "seg-expense-contract",
) -> dict[str, Any]:
    records = [source(segment_id, text)]
    requests: list[dict[str, Any]] = []
    source_id: str | None = None

    def capture_sources(batch: list[dict[str, Any]]) -> dict[str, Any]:
        nonlocal source_id
        source_id = batch[0]["source_id"]

    def response(_body: dict[str, Any], _index: int) -> dict[str, Any]:
        assert source_id is not None
        return {
            "records": [
                {
                    "source_id": source_id,
                    "is_event": True,
                    "events": [dict(event)],
                }
            ]
        }

    return run_contextual_extraction(
        records,
        deepseek_api_key=TEST_KEY,
        deepseek_opener=chat_stub(response, requests, capture_sources=capture_sources),
    )


@pytest.mark.parametrize(
    ("branch", "title"),
    [
        ("jev", "购买四斤半成品黑椒鸡排，38.8元"),
        ("clef", "购买四斤半成品黑椒鸡排（38.8元）"),
    ],
)
def test_captured_real_chicken_purchase_38_8_is_accepted_without_grammar_reparse(
    branch: str, title: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression at the actual contextual acceptance seam.

    ``work/l2-expense-20261019/real-model-before.json`` captured both real L2
    proposals with amount=38.8 and quote “...是三十八点八。”. The old program
    re-parsed spoken money and returned amount_not_grounded. The model's own
    normalized answer plus verbatim evidence must now survive.
    """
    text = "然后又买了四斤半成品的黑椒鸡排，是三十八点八。"
    event = {
        "category": "expense",
        "event_state": "completed",
        "claim_type": "event",
        "temporal_relation": "past",
        "occurrence_time": None,
        "title": title,
        "activity_type": "purchase",
        "topic": "食材采购" if branch == "jev" else "食品采购",
        "quote": text,
        "confidence": 0.95 if branch == "jev" else 0.93,
        "data": {
            "expense": {
                "amount": 38.8,
                "currency": "CNY",
                "item": "半成品黑椒鸡排",
                "quantity": "4斤",
                "amount_evidence": "三十八点八",
                "transaction_evidence": "买了四斤半成品的黑椒鸡排",
            }
        },
    }

    def forbidden_grammar(*_args: Any, **_kwargs: Any) -> bool:
        raise AssertionError("L2 expense acceptance must not call spoken-money grammar")

    monkeypatch.setattr(
        "life_logger.local_llm_extract._deepseek_amount_is_grounded",
        forbidden_grammar,
    )

    result = _run_single_expense_event(text, event, segment_id=f"seg-real-{branch}")

    assert len(result["formal_events"]) == 1
    accepted = result["formal_events"][0]
    assert accepted["title"] == title
    assert accepted["data"]["expense"]["amount"] == 38.8
    assert accepted["data"]["expense"]["amount_evidence"] == "三十八点八"
    assert accepted["data"]["expense"]["transaction_evidence"] == "买了四斤半成品的黑椒鸡排"
    assert accepted["evidence"]["quote"] == text
    assert accepted["evidence"]["pending_reason"] is None
    assert accepted["evidence"]["expense_model_contract"]["status"] == "accepted"
    assert accepted["evidence"]["expense_model_contract"]["reason"] is None

    ledger = build_expense_ledger(result["formal_events"])
    assert ledger["totals_by_currency"] == {"CNY": 38.8}
    assert len(ledger["transactions"]) == 1
    assert ledger["transactions"][0]["amount"] == 38.8
    assert all(tx["amount"] != 14 for tx in ledger["transactions"])


def test_price_only_spinach_14_is_pending_not_a_completed_payment() -> None:
    text = "我看有卖菠菜干的。四百克十四，菠菜干的这样子，它的营养价值。"
    event = {
        "category": "expense",
        "event_state": "idea",
        "claim_type": "observation",
        "title": "菠菜干价格观察",
        "activity_type": "price_observation",
        "quote": text,
        "confidence": 0.95,
        "data": {
            "expense": {
                "amount": 14,
                "currency": "CNY",
                "item": "菠菜干",
                "amount_evidence": "四百克十四",
                "transaction_evidence": "我看有卖菠菜干的",
            }
        },
    }

    result = _run_single_expense_event(text, event, segment_id="seg-spinach-price")

    assert result["formal_events"] == []
    assert result["pending_events"]
    pending = result["pending_events"][0]
    assert pending["evidence"]["pending_reason"] == "transaction_not_completed"
    assert pending["data"]["expense"]["amount"] is None
    assert pending["evidence"]["proposed_expense_amount"] == 14
    assert pending["evidence"]["expense_model_contract"]["status"] == "pending"

    ledger = build_expense_ledger(result["formal_events"])
    assert ledger["totals_by_currency"] == {}
    assert ledger["transactions"] == []
    assert all(tx["amount"] != 14 for tx in ledger["transactions"])


@pytest.mark.parametrize(
    ("state", "claim_type", "text", "amount_evidence", "transaction_evidence"),
    [
        (
            "planned",
            "plan",
            "明天想买黑椒鸡排，三十八点八。",
            "三十八点八",
            "想买黑椒鸡排",
        ),
        (
            "negated",
            "negation",
            "我没买黑椒鸡排，不是三十八点八。",
            "三十八点八",
            "没买黑椒鸡排",
        ),
        (
            "hypothetical",
            "hypothetical",
            "要是三十八点八能买到黑椒鸡排就好了。",
            "三十八点八",
            "能买到黑椒鸡排",
        ),
    ],
)
def test_planned_negated_or_hypothetical_purchase_is_not_accepted(
    state: str,
    claim_type: str,
    text: str,
    amount_evidence: str,
    transaction_evidence: str,
) -> None:
    event = {
        "category": "expense",
        "event_state": state,
        "claim_type": claim_type,
        "title": "黑椒鸡排",
        "activity_type": "purchase",
        "quote": text,
        "confidence": 0.95,
        "data": {
            "expense": {
                "amount": 38.8,
                "currency": "CNY",
                "item": "黑椒鸡排",
                "amount_evidence": amount_evidence,
                "transaction_evidence": transaction_evidence,
            }
        },
    }

    result = _run_single_expense_event(text, event, segment_id=f"seg-{state}")

    assert result["formal_events"] == []
    assert result["pending_events"]
    pending = result["pending_events"][0]
    assert pending["evidence"]["pending_reason"] == "transaction_not_completed"
    assert pending["data"]["expense"]["amount"] is None
    assert pending["evidence"]["proposed_expense_amount"] == 38.8


@pytest.mark.parametrize(
    ("amount", "amount_evidence", "expected_reason", "expected_proposal"),
    [
        ("38.8", "三十八点八", "amount_invalid_type", None),
        (float("inf"), "三十八点八", "amount_not_finite", None),
        (38.8, "四十四", "amount_evidence_not_verbatim", 38.8),
        (38.8, "", "amount_evidence_missing", 38.8),
    ],
)
def test_invalid_numeric_or_evidence_contract_is_visible_pending(
    amount: Any,
    amount_evidence: str,
    expected_reason: str,
    expected_proposal: float | None,
) -> None:
    text = "然后又买了四斤半成品的黑椒鸡排，是三十八点八。"
    event = {
        "category": "expense",
        "event_state": "completed",
        "claim_type": "event",
        "title": "黑椒鸡排",
        "activity_type": "purchase",
        "quote": text,
        "confidence": 0.95,
        "data": {
            "expense": {
                "amount": amount,
                "currency": "CNY",
                "item": "黑椒鸡排",
                "amount_evidence": amount_evidence,
                "transaction_evidence": "买了四斤半成品的黑椒鸡排",
            }
        },
    }

    result = _run_single_expense_event(
        text, event, segment_id=f"seg-invalid-{expected_reason}"
    )

    assert result["formal_events"] == []
    assert result["pending_events"]
    pending = result["pending_events"][0]
    assert pending["evidence"]["pending_reason"] == expected_reason
    assert pending["data"]["expense"]["amount"] is None
    assert pending["evidence"]["proposed_expense_amount"] == expected_proposal
    assert pending["evidence"]["expense_model_contract"]["reason"] == expected_reason


def test_context_batch_cache_key_includes_expense_contract_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [source("seg-cache-contract", "然后又买了半成品黑椒鸡排，是三十八点八。")]
    first = context_batch_input_key(records)
    monkeypatch.setattr(
        "life_logger.contextual_extraction.EXPENSE_MODEL_CONTRACT_VERSION",
        "expense-model-evidence-v2-test",
    )
    second = context_batch_input_key(records)
    assert first != second

def test_contextual_missing_claim_type_is_pending_not_accepted() -> None:
    text = "然后又买了四斤半成品的黑椒鸡排，是三十八点八。"
    event = {
        "category": "expense",
        "event_state": "completed",
        # claim_type deliberately missing
        "title": "黑椒鸡排",
        "activity_type": "purchase",
        "quote": text,
        "confidence": 0.95,
        "data": {
            "expense": {
                "amount": 38.8,
                "currency": "CNY",
                "item": "黑椒鸡排",
                "amount_evidence": "三十八点八",
                "transaction_evidence": "买了四斤半成品的黑椒鸡排",
            }
        },
    }

    result = _run_single_expense_event(text, event, segment_id="seg-missing-claim")

    assert result["formal_events"] == []
    assert result["pending_events"]
    pending = result["pending_events"][0]
    assert pending["evidence"]["pending_reason"] == "transaction_claim_missing"
    assert pending["data"]["expense"]["amount"] is None
    assert pending["evidence"]["proposed_expense_amount"] == 38.8


def test_contextual_missing_event_state_is_not_defaulted_to_completed() -> None:
    text = "然后又买了四斤半成品的黑椒鸡排，是三十八点八。"
    event = {
        "category": "expense",
        # event_state deliberately missing
        "claim_type": "event",
        "title": "黑椒鸡排",
        "activity_type": "purchase",
        "quote": text,
        "confidence": 0.95,
        "data": {
            "expense": {
                "amount": 38.8,
                "currency": "CNY",
                "item": "黑椒鸡排",
                "amount_evidence": "三十八点八",
                "transaction_evidence": "买了四斤半成品的黑椒鸡排",
            }
        },
    }

    result = _run_single_expense_event(text, event, segment_id="seg-missing-state")

    assert result["formal_events"] == []
    assert any(
        item["reason"] == "invalid_event_state" for item in result["pending_sources"]
    )



def test_expense_prompt_requires_positive_semantics_and_verbatim_evidence() -> None:
    records = [source("seg-expense-prompt", "我看有卖菠菜干的。四百克十四。")]
    requests: list[dict[str, Any]] = []

    run_contextual_extraction(
        records,
        deepseek_api_key=TEST_KEY,
        deepseek_opener=chat_stub(lambda _body, _index: {"records": []}, requests),
    )

    prompt = requests[0]["messages"][0]["content"]
    assert "amount_evidence" in prompt
    assert "transaction_evidence" in prompt
    assert "价格观察" in prompt
    assert "逐字连续子串" in prompt


@pytest.mark.parametrize(
    ("status", "needle"),
    ((401, "鉴权"), (402, "额度"), (403, "权限")),
)
def test_auth_and_quota_fail_fast_with_explicit_reason(
    status: int, needle: str
) -> None:
    calls: list[int] = []

    def fail(_request: Any, timeout: float) -> Response:
        calls.append(1)
        raise HTTPError(
            "https://api.deepseek.com/chat/completions",
            status,
            "stub",
            {},
            None,
        )

    result = run_contextual_extraction(
        [source(f"seg-auth-{status}", "今天买水。")],
        deepseek_api_key=TEST_KEY,
        deepseek_opener=fail,
        batch_size=1,
    )

    assert calls == [1]
    batch = result["batches"][0]
    assert batch["status"] == "failed"
    assert batch["retryable_transient"] is False
    assert needle in batch["error"]
    assert result["pending_sources"][0]["quote"] == "今天买水。"
    assert result["pending_sources"][0]["retryable"] is False


def test_transient_failure_is_retried_but_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "life_logger.contextual_extraction.time.sleep", lambda _seconds: None
    )
    calls: list[int] = []

    def fail(_request: Any, timeout: float) -> Response:
        calls.append(1)
        raise TimeoutError("stub timeout")

    result = run_contextual_extraction(
        [source("seg-bounded", "今天买水。")],
        deepseek_api_key=TEST_KEY,
        deepseek_opener=fail,
        batch_size=1,
    )

    assert len(calls) == 3
    batch = result["batches"][0]
    assert batch["retryable_transient"] is True
    assert "超时" in batch["error"]
    assert result["pending_sources"][0]["quote"] == "今天买水。"
    assert result["pending_sources"][0]["retryable"] is True


def test_repeated_batch_failure_opens_circuit_without_trying_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "life_logger.contextual_extraction.time.sleep", lambda _seconds: None
    )
    texts = [f"第{index}条原文" for index in range(5)]
    calls: list[int] = []

    def fail(_request: Any, timeout: float) -> Response:
        calls.append(1)
        raise HTTPError(
            "https://api.deepseek.com/chat/completions",
            503,
            "stub",
            {},
            None,
        )

    result = run_contextual_extraction(
        [source(f"seg-circuit-{index}", text) for index, text in enumerate(texts)],
        deepseek_api_key=TEST_KEY,
        deepseek_opener=fail,
        batch_size=1,
    )

    # 每批 3 次有界重试；连续 2 批失败后熔断，剩余批次不再调用模型。
    assert len(calls) == 6
    assert result["status"] == "failed"
    assert result["circuit"] == {
        "opened": True,
        "after_batch": 2,
        "aborted_batches": 3,
        "aborted_sources": 3,
        "failure_kind": "http_error",
        "http_status": 503,
    }
    assert {item["segment_id"] for item in result["pending_sources"]} == {
        f"seg-circuit-{index}" for index in range(5)
    }
    assert {item["quote"] for item in result["pending_sources"]} == set(texts)
    assert sum(bool(item.get("aborted")) for item in result["pending_sources"]) == 3


@pytest.mark.parametrize(
    "error",
    (
        socket.gaierror("dns down"),
        ssl.SSLError("ssl down"),
        URLError(ssl.SSLError("wrapped ssl down")),
    ),
)
def test_dns_and_ssl_errors_are_transient_and_bounded(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(
        "life_logger.contextual_extraction.time.sleep", lambda _seconds: None
    )
    calls: list[int] = []

    def fail(_request: Any, timeout: float) -> Response:
        calls.append(1)
        raise error

    result = run_contextual_extraction(
        [source("seg-network", "今天买水。")],
        deepseek_api_key=TEST_KEY,
        deepseek_opener=fail,
        batch_size=1,
    )

    assert len(calls) == 3
    batch = result["batches"][0]
    assert batch["failure_kind"] == "connection_error"
    assert batch["retryable_transient"] is True
    assert result["pending_sources"][0]["retryable"] is True
    assert result["pending_sources"][0]["quote"] == "今天买水。"
