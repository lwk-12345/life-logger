"""Focused mechanical contract tests for L2-proposed expense money."""

from __future__ import annotations

from life_logger.expense_model_contract import validate_expense_model_contract

QUOTE = "然后又买了四斤半成品的黑椒鸡排，是三十八点八。"
EXPENSE = {
    "amount": 38.8,
    "currency": "CNY",
    "item": "黑椒鸡排",
    "amount_evidence": "三十八点八",
    "transaction_evidence": "买了四斤半成品的黑椒鸡排",
}


def test_valid_model_expense_requires_explicit_positive_semantics() -> None:
    result = validate_expense_model_contract(
        EXPENSE, quote=QUOTE, event_state="completed", claim_type="event"
    )

    assert result.ok is True
    assert result.amount == 38.8
    assert result.reason is None


def test_missing_event_state_cannot_default_to_completed() -> None:
    result = validate_expense_model_contract(
        EXPENSE, quote=QUOTE, event_state=None, claim_type="event"
    )

    assert result.ok is False
    assert result.reason == "transaction_state_missing"


def test_missing_or_arbitrary_claim_cannot_authorize_completed_payment() -> None:
    missing = validate_expense_model_contract(
        EXPENSE, quote=QUOTE, event_state="completed", claim_type=None
    )
    arbitrary = validate_expense_model_contract(
        EXPENSE, quote=QUOTE, event_state="completed", claim_type="looked_legit"
    )
    observation = validate_expense_model_contract(
        EXPENSE,
        quote=QUOTE,
        event_state="completed",
        claim_type="price_observation",
    )

    assert missing.ok is False
    assert missing.reason == "transaction_claim_missing"
    assert arbitrary.ok is False
    assert arbitrary.reason == "transaction_claim_not_positive"
    assert observation.ok is False
    assert observation.reason == "transaction_claim_not_positive"


def test_explicit_correction_claim_authorizes_correction_command() -> None:
    result = validate_expense_model_contract(
        EXPENSE,
        quote=QUOTE,
        event_state=None,
        claim_type="correction",
        authorization="correction",
    )

    assert result.ok is True
    assert result.amount == 38.8


def test_correction_claim_without_explicit_authorization_is_still_rejected() -> None:
    result = validate_expense_model_contract(
        EXPENSE,
        quote=QUOTE,
        event_state=None,
        claim_type="correction",
    )

    assert result.ok is False
    assert result.reason == "transaction_state_missing"
