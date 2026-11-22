"""Mechanical integrity checks for L2-proposed expense transactions.

The language model owns the *meaning* of a purchase: whether the speaker
actually paid, whether the amount refers to a completed transaction rather
than a quoted price/plan/negation, and the normalized numeric amount.  This
module deliberately does **not** parse Chinese money grammar or convert spoken
numerals.  It only checks that the model's answer has a safe numeric shape and
that the model preserved verbatim source evidence for both the amount and the
transaction claim.

A failed check is a review condition, not permission to silently drop the
proposal.  Callers keep the proposed amount in pending evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

EXPENSE_MODEL_CONTRACT_VERSION = "expense-model-evidence-v1"

CONFIRMED_TRANSACTION_STATES = frozenset({"completed", "ongoing"})

# Only an explicit positive claim may authorize formal money.  Keeping this as
# a closed allow-list avoids silently accepting arbitrary/missing claim values
# or indefinitely enumerating every phrase that merely means "not paid".
POSITIVE_TRANSACTION_CLAIM_TYPES = frozenset(
    {
        "event",
        "purchase",
        "transaction",
        "expense",
        "payment",
        "paid",
        "correction",
    }
)

_MAX_EXPENSE_AMOUNT = Decimal("10000000")


@dataclass(frozen=True)
class ExpenseContractResult:
    """Result of validating one model-proposed expense payload."""

    ok: bool
    reason: str | None
    amount: float | None
    currency: str
    amount_evidence: str | None
    transaction_evidence: str | None
    raw_amount: Any

    def as_evidence(self) -> dict[str, Any]:
        """Serializable audit record; safe to attach to event.evidence."""
        payload: dict[str, Any] = {
            "version": EXPENSE_MODEL_CONTRACT_VERSION,
            "status": "accepted" if self.ok else "pending",
            "reason": self.reason,
            "amount": self.amount,
            "currency": self.currency,
            "amount_evidence": self.amount_evidence,
            "transaction_evidence": self.transaction_evidence,
        }
        if self.amount is None and self.raw_amount is not None:
            payload["proposed_amount_raw"] = str(self.raw_amount)[:80]
        return payload


def _normalized_amount(raw: Any) -> tuple[float | None, str | None]:
    if raw is None:
        return None, "amount_missing"
    if isinstance(raw, bool) or not isinstance(raw, (int, float, Decimal)):
        return None, "amount_invalid_type"
    try:
        numeric = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None, "amount_invalid_value"
    if not numeric.is_finite():
        return None, "amount_not_finite"
    if numeric <= 0 or numeric >= _MAX_EXPENSE_AMOUNT:
        return None, "amount_out_of_range"
    return float(numeric), None


def _verbatim_evidence(
    raw: Any, quote: str, *, field: str
) -> tuple[str | None, str | None]:
    if not isinstance(raw, str) or not raw.strip():
        return None, f"{field}_missing"
    text = raw.strip()
    if text not in quote:
        return None, f"{field}_not_verbatim"
    return text, None


def validate_expense_model_contract(
    expense: Mapping[str, Any],
    *,
    quote: str,
    event_state: str | None = None,
    claim_type: str | None = None,
    authorization: str | None = None,
) -> ExpenseContractResult:
    """Validate a model-proposed ``data.expense`` payload.

    ``event_state``/``claim_type`` are explicit semantic answers from the
    model; a missing or arbitrary answer is not defaulted into a completed
    payment.  ``authorization="correction"`` is the one explicit exception:
    a correction command may use ``claim_type="correction"`` to authorize a
    correction without pretending it is a newly observed purchase.  The
    program never decides meaning by looking at Chinese amount syntax.
    """
    state = str(event_state or "").strip().lower()
    claim = str(claim_type or "").strip().lower()
    auth = str(authorization or "").strip().lower()
    quote_text = quote if isinstance(quote, str) else ""

    amount, amount_reason = _normalized_amount(expense.get("amount"))
    amount_evidence, amount_evidence_reason = _verbatim_evidence(
        expense.get("amount_evidence"), quote_text, field="amount_evidence"
    )
    transaction_evidence, transaction_evidence_reason = _verbatim_evidence(
        expense.get("transaction_evidence"),
        quote_text,
        field="transaction_evidence",
    )

    raw_currency = expense.get("currency")
    currency = (
        str(raw_currency).strip()[:12].upper()
        if isinstance(raw_currency, str) and raw_currency.strip()
        else "CNY"
    )

    reason: str | None = None
    correction_authorized = auth == "correction" and claim == "correction"
    if not state and not correction_authorized:
        reason = "transaction_state_missing"
    elif state and state not in CONFIRMED_TRANSACTION_STATES:
        reason = "transaction_not_completed"
    elif not claim:
        reason = "transaction_claim_missing"
    elif claim not in POSITIVE_TRANSACTION_CLAIM_TYPES:
        reason = "transaction_claim_not_positive"
    elif amount_reason:
        reason = amount_reason
    elif amount_evidence_reason:
        reason = amount_evidence_reason
    elif transaction_evidence_reason:
        reason = transaction_evidence_reason

    return ExpenseContractResult(
        ok=reason is None,
        reason=reason,
        amount=amount,
        currency=currency,
        amount_evidence=amount_evidence,
        transaction_evidence=transaction_evidence,
        raw_amount=expense.get("amount"),
    )
