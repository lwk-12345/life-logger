"""Explicit activity references survive model -> event_view -> activity_links."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from life_logger.activity_links import build_activity_links
from life_logger.contextual_extraction import _event_from_model
from life_logger.event_view import build_event_view


def _record(source_id: str, text: str) -> dict:
    return {
        "source_id": source_id,
        "text": text,
        "recording_id": "rec_ref_1",
        "segment_id": "rec_ref_1_seg_000001",
        "absolute_started_at": "2026-10-24T00:19:00+08:00",
        "timestamp": "2026-10-24T00:19:00+08:00",
        "start_offset_seconds": 0.0,
        "end_offset_seconds": 2.0,
        "source_label": "self",
        "acoustic_proximity": "near",
        "voice_similarity": 0.9,
    }


def _model_event(quote: str, **extra) -> dict:
    event = {
        "category": "food",
        "event_state": "completed",
        "title": quote,
        "activity_type": "meal",
        "topic": "餐饮",
        "claim_type": "event",
        "quote": quote,
        "confidence": 0.95,
        "data": {"food": {"item": "鸭腿", "portion": "1份"}},
        "temporal_relation": "at_speech",
        "occurrence_time": None,
    }
    event.update(extra)
    return event


def _write_summary(root: Path, events: list[dict]) -> None:
    target = root / "jobs" / "job-refs" / "files" / "rec-1" / "result" / "2026-10-24" / "daily_summary.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({
        "date": "2026-10-24",
        "timezone": "Asia/Shanghai",
        "timeline": events,
        "display_timeline": events,
        "warnings": [],
    }, ensure_ascii=False), encoding="utf-8")


def test_top_level_references_are_preserved_through_event_view_and_links(tmp_path: Path) -> None:
    quote = "来一份鸭腿"
    model = _model_event(
        quote,
        activity_ref={"namespace": "activity", "id": "act_demo"},
        parent_event_id="evt_parent",
        related_event_ids=["evt_sibling", "evt_sibling", "bad id"],
    )
    record = _record("src_a", quote)
    model_before = copy.deepcopy(model)
    event, reason = _event_from_model(model, record)
    assert reason is None, reason
    assert event["data"]["activity_ref"] == {
        "namespace": "activity",
        "id": "act_demo",
        "basis": "model.activity_ref",
        "source_path": "model.activity_ref",
    }
    assert event["data"]["parent_event_id"] == "evt_parent"
    assert event["data"]["related_event_ids"] == ["evt_sibling"]
    assert event["data"]["food"] == model_before["data"]["food"]
    assert event["time"]["absolute"] == "2026-10-24T00:19:00+08:00"
    assert event["evidence"]["source_label"] == "self"
    assert model == model_before  # no mutation of model response

    root = tmp_path / "local-web"
    _write_summary(root, [event])
    view_event = build_event_view(root, date_from="2026-10-24", date_to="2026-10-24")["events"][0]
    assert view_event["data"]["activity_ref"]["namespace"] == "activity"
    assert view_event["data"]["parent_event_id"] == "evt_parent"
    links = build_activity_links([view_event])
    assert links["members"].get("activity:act_demo") == [event["event_id"]]


def test_category_payload_reference_keeps_original_basis_path(tmp_path: Path) -> None:
    quote = "来一份鸭腿"
    model = _model_event(quote)
    model["data"]["food"]["activity_ref"] = {"namespace": "activity", "id": "act_category"}
    event, reason = _event_from_model(model, _record("src_b", quote))
    assert reason is None
    assert event["data"]["activity_ref"] == {
        "namespace": "activity",
        "id": "act_category",
        "basis": "data.food.activity_ref",
        "source_path": "data.food.activity_ref",
    }


def test_invalid_or_missing_references_are_not_synthesized(tmp_path: Path) -> None:
    quote = "来一份鸭腿"
    model = _model_event(
        quote,
        activity_ref="free text is not an activity id",
        parent_event_id="not-an-event",
        related_event_ids=["bad id", 123, "", "not-an-event"],
    )
    event, reason = _event_from_model(model, _record("src_c", quote))
    assert reason is None
    assert "activity_ref" not in event["data"]
    assert "parent_event_id" not in event["data"]
    assert "related_event_ids" not in event["data"]
    assert event["evidence"]["recording_id"] == "rec_ref_1"


def test_no_reference_input_does_not_generate_one() -> None:
    quote = "来一份鸭腿"
    event, reason = _event_from_model(_model_event(quote), _record("src_d", quote))
    assert reason is None
    assert "activity_ref" not in event["data"]
    assert "parent_event_id" not in event["data"]
    assert "related_event_ids" not in event["data"]


def test_two_events_with_same_explicit_ref_form_one_member_group_after_view(tmp_path: Path) -> None:
    ref = {"namespace": "activity", "id": "act_shared"}
    first = _event_from_model(_model_event("来一份鸭腿", activity_ref=ref), _record("src_e1", "来一份鸭腿"))[0]
    second = _event_from_model(_model_event("来一份鸡腿", activity_ref=ref), _record("src_e2", "来一份鸡腿"))[0]
    root = tmp_path / "local-web"
    _write_summary(root, [first, second])
    events = build_event_view(root, date_from="2026-10-24", date_to="2026-10-24")["events"]
    links = build_activity_links(events)
    assert sorted(links["members"]["activity:act_shared"]) == sorted([first["event_id"], second["event_id"]])
