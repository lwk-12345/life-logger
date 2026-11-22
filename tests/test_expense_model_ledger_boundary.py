"""Ledger boundary: explicit L2 review must not be bypassed by recovery.

The expense ledger may keep the legacy quote recovery path for events that
predate the model evidence contract.  It must not use that path to resurrect a
model result that was explicitly rejected/pending, because doing so would undo
the upstream fail-safe and count an unverified amount.
"""

from __future__ import annotations

from typing import Any

import pytest

import life_logger.expense_policy as expense_policy
from life_logger.contextual_extraction import _event_from_model
from life_logger.domain import TranscriptSegment
from life_logger.expense_policy import build_expense_ledger
from life_logger.local_llm_extract import DeepSeekEventExtractor


def _actual_pending_model_event() -> dict[str, Any]:
    quote = "买咖啡花了十五元。"
    event, reason = _event_from_model(
        {
            "category": "expense",
            "event_state": "completed",
            "claim_type": "event",
            "title": "咖啡",
            "quote": quote,
            "confidence": 0.95,
            "data": {
                "expense": {"amount": 15, "currency": "CNY", "item": "咖啡"}
            },
        },
        {
            "source_id": "src_ledger_probe",
            "recording_id": "rec_probe",
            "segment_id": "seg_probe",
            "source_label": "self",
            "text": quote,
            "utterance_timestamp": "2026-10-18T04:28:00+08:00",
        },
    )
    assert reason == "amount_evidence_missing"
    assert event is not None
    assert event["evidence"]["expense_model_contract"]["status"] == "pending"
    assert event["data"]["expense"]["amount"] is None
    return event


def _forbid_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("legacy amount recovery must not run for L2 review")

    monkeypatch.setattr(expense_policy, "_recover_amount", _boom)


def test_actual_pending_model_event_is_pending_and_never_recovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_recovery(monkeypatch)

    ledger = build_expense_ledger([_actual_pending_model_event()])

    assert ledger["transactions"] == []
    assert ledger["total_amount"] == 0.0
    assert ledger["totals_by_currency"] == {}
    assert ledger["pending_count"] == 1
    pending = ledger["pending_transactions"][0]
    assert pending["reason"] == "amount_evidence_missing"
    assert pending["amount"] is None
    assert pending["proposed_amount"] == 15.0
    assert pending["provenance"]["l2_review"] == {
        "source": "pending_reason",
        "status": "pending",
        "reason": "amount_evidence_missing",
        "contract_version": "expense-model-evidence-v1",
    }
    assert "amount_not_recovered_for_explicit_l2_review" in pending["explanation"]


def test_rejected_contract_with_accidental_raw_amount_is_not_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_recovery(monkeypatch)
    event = _actual_pending_model_event()
    # Simulate a caller leaking the rejected raw amount back into data.
    event["data"]["expense"]["amount"] = 15

    ledger = build_expense_ledger([event])

    assert ledger["transactions"] == []
    assert ledger["total_amount"] == 0.0
    assert ledger["pending_count"] == 1
    pending = ledger["pending_transactions"][0]
    assert pending["amount"] is None
    assert pending["proposed_amount"] == 15.0


def test_legacy_deepseek_review_fallback_is_pending_and_not_recovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_recovery(monkeypatch)

    def fail_opener(*_args: Any, **_kwargs: Any):
        raise OSError("offline stub")

    extractor = DeepSeekEventExtractor(
        enabled=True,
        api_key="test-only-key",
        opener=fail_opener,
    )
    segment = TranscriptSegment(
        segment_id="seg_legacy_fallback",
        recording_id="rec_legacy_fallback",
        start_offset_seconds=10.0,
        end_offset_seconds=15.0,
        absolute_started_at="2026-10-18T05:00:00+08:00",
        absolute_ended_at="2026-10-18T05:00:05+08:00",
        text="九块钱买了一包烟，哎呦。",
        source="test",
        language="zh",
        transcription_confidence=0.9,
    )
    events = extractor.extract(segment)
    assert events
    assert all(event.evidence["deepseek_review"] == "unavailable" for event in events)
    assert all(
        event.evidence["unverified_expense_data"]["amount"] == 9
        for event in events
        if "expense" in event.categories
    )

    ledger = build_expense_ledger([event.to_dict() for event in events])

    assert ledger["transactions"] == []
    assert ledger["total_amount"] == 0.0
    assert ledger["pending_count"] == 1
    pending = ledger["pending_transactions"][0]
    assert pending["reason"] == "unavailable"
    assert pending["amount"] is None
    assert pending["proposed_amount"] == 9.0
    assert pending["provenance"]["l2_review"]["source"] == "deepseek_review"


def test_accepted_contract_amount_38_8_bypasses_legacy_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_recovery(monkeypatch)
    quote = "买了鸡胸肉，三十八块八。"
    event, reason = _event_from_model(
        {
            "category": "expense",
            "event_state": "completed",
            "claim_type": "event",
            "title": "鸡胸肉",
            "quote": quote,
            "confidence": 0.95,
            "data": {
                "expense": {
                    "amount": 38.8,
                    "currency": "CNY",
                    "item": "鸡胸肉",
                    "amount_evidence": "三十八块八",
                    "transaction_evidence": "买了鸡胸肉",
                }
            },
        },
        {
            "source_id": "src_ledger_accepted",
            "recording_id": "rec_accepted",
            "segment_id": "seg_accepted",
            "source_label": "self",
            "text": quote,
            "utterance_timestamp": "2026-10-18T06:00:00+08:00",
        },
    )

    assert reason is None
    assert event is not None
    assert event["evidence"]["expense_model_contract"]["status"] == "accepted"
    ledger = build_expense_ledger([event])
    assert ledger["pending_count"] == 0
    assert ledger["totals_by_currency"] == {"CNY": 38.8}
    assert ledger["transactions"][0]["amount_source"] == "expense_amount"


def test_legacy_missing_amount_recovery_still_works_without_review_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = expense_policy._recover_amount
    calls: list[tuple[str, Any]] = []

    def _spy(quote: str, proposed: Any):
        calls.append((quote, proposed))
        return original(quote, proposed)

    monkeypatch.setattr(expense_policy, "_recover_amount", _spy)
    event = {
        "event_id": "evt_legacy_recover",
        "categories": ["expense"],
        "activity_type": "purchase",
        "title": "购入一包桂圆",
        "time": {"absolute": "2026-10-18T07:00:00+08:00", "precision": "exact"},
        "data": {
            "expense": {"amount": None, "currency": "CNY", "item": "桂圆"}
        },
        "evidence": {
            "recording_id": "rec_legacy",
            "segment_id": "seg_legacy",
            "reported_at": "2026-10-18T07:00:00+08:00",
            "quote": "买了一包桂圆二十四。",
            "event_state": "completed",
            "claim_type": "event",
            "proposed_expense_amount": 24,
        },
        "review_status": "unreviewed",
    }

    ledger = build_expense_ledger([event])

    assert calls == [("买了一包桂圆二十四。", expense_policy._decimal(24))]
    assert ledger["pending_count"] == 0
    assert ledger["totals_by_currency"] == {"CNY": 24.0}
    assert ledger["transactions"][0]["amount_source"] == "recovered_quote"


def test_accepted_money_contract_does_not_override_source_review_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_recovery(monkeypatch)
    quote = "买了鸡胸肉，三十八块八。"
    event, reason = _event_from_model(
        {
            "category": "expense",
            "event_state": "completed",
            "claim_type": "event",
            "title": "鸡胸肉",
            "quote": quote,
            "confidence": 0.95,
            "data": {
                "expense": {
                    "amount": 38.8,
                    "currency": "CNY",
                    "item": "鸡胸肉",
                    "amount_evidence": "三十八块八",
                    "transaction_evidence": "买了鸡胸肉",
                }
            },
        },
        {
            "source_id": "src_ledger_non_self",
            "recording_id": "rec_non_self",
            "segment_id": "seg_non_self",
            "source_label": "non_self",
            "text": quote,
            "utterance_timestamp": "2026-10-18T06:30:00+08:00",
        },
    )

    assert reason == "non_self_review"
    assert event is not None
    # Money evidence was accepted, but the event still awaits source review.
    assert event["evidence"]["expense_model_contract"]["status"] == "accepted"
    assert event["evidence"]["pending_reason"] == "non_self_review"
    assert event["data"]["expense"]["amount"] is None

    # Simulate an overlay/leak restoring the accepted model amount.
    event["data"]["expense"]["amount"] = 38.8
    ledger = build_expense_ledger([event])

    assert ledger["transactions"] == []
    assert ledger["total_amount"] == 0.0
    assert ledger["pending_count"] == 1
    pending = ledger["pending_transactions"][0]
    assert pending["reason"] == "non_self_review"
    assert pending["amount"] is None
    assert pending["proposed_amount"] == 38.8
    assert pending["provenance"]["l2_review"]["source"] == "pending_reason"


@pytest.mark.parametrize(
    ("marker", "expected_reason", "expected_source"),
    [
        ({"pending_reason": "unknown_speaker"}, "unknown_speaker", "pending_reason"),
        ({"deepseek_review": "invalid_response"}, "invalid_response", "deepseek_review"),
        (
            {"unverified_expense_data": {"amount": 38.8}},
            "unverified_expense_data",
            "unverified_expense_data",
        ),
    ],
)
def test_accepted_contract_cannot_discard_explicit_pending_marker(
    monkeypatch: pytest.MonkeyPatch,
    marker: dict[str, Any],
    expected_reason: str,
    expected_source: str,
) -> None:
    _forbid_recovery(monkeypatch)
    event = _actual_pending_model_event()
    event["evidence"]["expense_model_contract"] = {
        "version": "expense-model-evidence-v1",
        "status": "accepted",
        "reason": None,
        "amount": 38.8,
        "currency": "CNY",
    }
    event["evidence"].pop("pending_reason", None)
    event["evidence"].update(marker)
    event["data"]["expense"]["amount"] = 38.8

    ledger = build_expense_ledger([event])

    assert ledger["transactions"] == []
    assert ledger["total_amount"] == 0.0
    assert ledger["pending_count"] == 1
    pending = ledger["pending_transactions"][0]
    assert pending["reason"] == expected_reason
    assert pending["provenance"]["l2_review"]["source"] == expected_source
    assert pending["proposed_amount"] == 38.8


@pytest.mark.parametrize("status", ["rejected", "invalid"])
def test_non_accepted_contract_status_blocks_legacy_recovery(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    _forbid_recovery(monkeypatch)
    event = {
        "event_id": "evt_non_accepted_status",
        "categories": ["expense"],
        "activity_type": "purchase",
        "title": "咖啡",
        "time": {"absolute": "2026-10-18T08:00:00+08:00", "precision": "exact"},
        "data": {
            "expense": {"amount": None, "currency": "CNY", "item": "咖啡"}
        },
        "evidence": {
            "recording_id": "rec_non_accepted",
            "segment_id": "seg_non_accepted",
            "reported_at": "2026-10-18T08:00:00+08:00",
            "quote": "买咖啡花了十五元。",
            "expense_model_contract": {
                "version": "expense-model-evidence-v1",
                "status": status,
                "reason": f"{status}_reason",
                "amount": 15,
            },
            "proposed_expense_amount": 15,
        },
        "review_status": "unreviewed",
    }

    ledger = build_expense_ledger([event])

    assert ledger["transactions"] == []
    assert ledger["total_amount"] == 0.0
    assert ledger["pending_count"] == 1
    pending = ledger["pending_transactions"][0]
    assert pending["reason"] == f"{status}_reason"
    assert pending["proposed_amount"] == 15.0
