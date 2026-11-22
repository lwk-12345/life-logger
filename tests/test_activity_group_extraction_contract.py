"""Optional activity-group prompt/response contract (offline)."""

from __future__ import annotations

import json
from typing import Any

from life_logger.contextual_extraction import (
    _activity_request_namespace,
    _deepseek_request,
    _event_from_model,
)


def _record(source_id: str, text: str, *, label: str = "self") -> dict[str, Any]:
    return {
        "source_id": source_id,
        "segment_id": f"seg-{source_id}",
        "recording_id": "rec-contract",
        "source_label": label,
        "timestamp": "2026-10-25T00:19:00+08:00",
        "utterance_timestamp": "2026-10-25T00:19:00+08:00",
        "absolute_started_at": "2026-10-25T00:19:00+08:00",
        "start_offset_seconds": 0.0,
        "end_offset_seconds": 2.0,
        "text": text,
        "is_dictation": False,
    }


def _model_event(quote: str, **extra: Any) -> dict[str, Any]:
    event = {
        "category": "food",
        "event_state": "completed",
        "title": quote,
        "activity_type": "meal",
        "topic": "餐饮",
        "claim_type": "event",
        "quote": quote,
        "confidence": 0.95,
        "data": {"food": {"item": "鸭腿"}},
        "temporal_relation": "at_speech",
        "occurrence_time": None,
    }
    event.update(extra)
    return event


class _Response:
    def __init__(self, content: dict[str, Any]) -> None:
        self.body = json.dumps({
            "choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}],
        }, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self.body


def _opener(response: dict[str, Any], prompts: list[str]):
    def open(request, timeout):
        prompts.append(json.loads(request.data.decode("utf-8"))["messages"][0]["content"])
        return _Response(response)
    return open


def test_prompt_uses_request_scoped_namespace_and_explicit_contract() -> None:
    records = [_record("src-a", "来一份鸭腿"), _record("src-b", "要辣")]
    prompts: list[str] = []
    response, error, _ = _deepseek_request(
        records, api_key="test", timeout_seconds=1,
        opener=_opener({"records": []}, prompts),
    )
    assert error is None
    assert response["activity_namespace"] == _activity_request_namespace(records)
    prompt = prompts[0]
    assert "activity_namespace" in prompt or response["activity_namespace"] in prompt
    assert "activity_group" in prompt
    assert "禁止无 scope 的全局 id" in prompt
    assert "act1 在本 request scope" in prompt
    assert "basis_quote" in prompt
    # The packet carries the request-scoped anchor for every source.
    assert response["activity_namespace"] in prompt


def test_valid_group_is_preserved_only_with_scope_and_exact_quote() -> None:
    namespace = "ctx-test-scope"
    source_id = "src-a"
    quote = "来一份鸭腿"
    model = _model_event(quote, activity_group={
        "namespace": namespace,
        "id": "g1",
        "basis_source_id": source_id,
        "basis_quote": quote,
    })
    event, reason = _event_from_model(
        model,
        _record(source_id, quote),
        activity_namespace=namespace,
        activity_source_texts={source_id: quote},
        activity_source_labels={source_id: "self"},
    )
    assert reason is None
    assert event["data"]["activity_group"] == {
        "namespace": namespace,
        "id": "g1",
        "basis_source_id": source_id,
        "basis_quote": quote,
        "source_path": "model.activity_group",
    }
    assert event["data"]["activity_ref"]["namespace"] == namespace
    assert event["data"]["activity_ref"]["id"] == "g1"


def test_group_with_wrong_scope_or_quote_is_rejected_not_globalized() -> None:
    quote = "来一份鸭腿"
    wrong_scope = _model_event(quote, activity_group={
        "namespace": "model-global-act1",
        "id": "act1",
        "basis_source_id": "src-a",
        "basis_quote": quote,
    })
    event, _ = _event_from_model(
        wrong_scope, _record("src-a", quote),
        activity_namespace="ctx-real",
        activity_source_texts={"src-a": quote},
    )
    assert "activity_group" not in event["data"]
    assert "activity_ref" not in event["data"]

    wrong_quote = _model_event(quote, activity_group={
        "namespace": "ctx-real",
        "id": "g2",
        "basis_source_id": "src-a",
        "basis_quote": "不是原文",
    })
    event2, _ = _event_from_model(
        wrong_quote, _record("src-a", quote),
        activity_namespace="ctx-real",
        activity_source_texts={"src-a": quote},
    )
    assert "activity_group" not in event2["data"]
    assert "activity_ref" not in event2["data"]


def test_cross_actor_group_is_rejected_without_synthesizing_a_group() -> None:
    quote = "来一份鸭腿"
    model = _model_event(quote, activity_group={
        "namespace": "ctx-real",
        "id": "shared",
        "basis_source_id": "src-other",
        "basis_quote": quote,
    })
    event, _ = _event_from_model(
        model, _record("src-self", quote),
        activity_namespace="ctx-real",
        activity_source_texts={"src-other": quote},
        activity_source_labels={"src-other": "non_self"},
    )
    assert "activity_group" not in event["data"]
    assert "activity_ref" not in event["data"]


def test_no_group_means_no_group_or_activity_ref() -> None:
    quote = "来一份鸭腿"
    event, reason = _event_from_model(
        _model_event(quote), _record("src-a", quote),
        activity_namespace="ctx-real", activity_source_texts={"src-a": quote},
    )
    assert reason is None
    assert "activity_group" not in event["data"]
    assert "activity_ref" not in event["data"]

def test_transaction_ids_survive_conversion_and_two_payments_remain_distinct() -> None:
    from life_logger.activity_links import build_activity_links

    namespace = "ctx-two-payments"
    quote_a = "来一份鸭腿饭"
    quote_b = "再付一份鸭腿饭"
    model_a = _model_event(quote_a, transaction_id="T1", activity_group={
        "namespace": namespace, "id": "g-order", "basis_source_id": "src-a", "basis_quote": quote_a,
    })
    model_b = _model_event(quote_b, transaction_id="T2", activity_group={
        "namespace": namespace, "id": "g-order", "basis_source_id": "src-b", "basis_quote": quote_b,
    })
    event_a, _ = _event_from_model(
        model_a, _record("src-a", quote_a),
        activity_namespace=namespace,
        activity_source_texts={"src-a": quote_a, "src-b": quote_b},
    )
    event_b, _ = _event_from_model(
        model_b, _record("src-b", quote_b),
        activity_namespace=namespace,
        activity_source_texts={"src-a": quote_a, "src-b": quote_b},
    )
    assert event_a["data"]["transaction_id"] == "T1"
    assert event_b["data"]["transaction_id"] == "T2"
    links = build_activity_links([event_a, event_b])
    transaction_refs = [
        ref for refs in links["transactions"].values()
        for ref in refs if ref.get("namespace") == "transaction_id"
    ]
    assert {ref["id"] for ref in transaction_refs} == {"T1", "T2"}
    assert len(links["members"]["ctx-two-payments:g-order"]) == 2


def test_top_level_and_data_transaction_conflict_is_recorded_not_overwritten() -> None:
    quote = "来一份鸭腿饭"
    model = _model_event(quote, transaction_id="T1")
    model["data"]["food"]["transaction_id"] = "T2"
    event, reason = _event_from_model(model, _record("src-conflict", quote))
    assert reason is None
    assert event["data"]["food"]["transaction_id"] == "T2"
    assert event["data"]["transaction_id"] == "T2"
    conflicts = event["data"]["reference_conflicts"]
    assert conflicts["transaction_id"]["model"] == "T1"
    assert conflicts["transaction_id"]["data"] == "T2"
