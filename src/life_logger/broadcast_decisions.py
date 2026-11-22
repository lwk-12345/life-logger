"""Persistent accept/reject decisions for POS broadcast expense candidates.

The expense policy intentionally leaves ``pending_broadcast`` out of
``total_amount``.  This module is the read/write overlay that lets a human
confirm or reject one of those candidates without mutating extracted events or
``expense_policy.py``.

Decisions live in ``<job-root>/broadcast-decisions/<date>.json``.  A file-level
``flock`` serializes writers; the JSON write itself uses tmp + ``os.replace`` so
a crash cannot leave a half-written decision file.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
from contextlib import contextmanager
from datetime import date as date_type
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

DECISIONS_DIRNAME = "broadcast-decisions"
SUPPORTED_ACTIONS = {"accept", "reject"}


class BroadcastDecisionError(ValueError):
    """Invalid date/event/amount or a decision-file write failure."""


def validate_date(value: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = date_type.fromisoformat(text)
    except ValueError as error:
        raise BroadcastDecisionError("date must use YYYY-MM-DD") from error
    if parsed.isoformat() != text:
        raise BroadcastDecisionError("date must use YYYY-MM-DD")
    return text


def decisions_path(root: Path, date: str) -> Path:
    return Path(root) / DECISIONS_DIRNAME / f"{validate_date(date)}.json"


@contextmanager
def _locked_file(root: Path, date: str) -> Iterator[Path]:
    directory = Path(root) / DECISIONS_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / f"{validate_date(date)}.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield decisions_path(root, date)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _empty_payload(date: str) -> dict[str, Any]:
    return {"date": date, "decisions": [], "undo_log": []}


def _read_payload(path: Path, date: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_payload(date)
    if not isinstance(payload, dict):
        raise BroadcastDecisionError("decision file is not a JSON object")
    payload.setdefault("date", date)
    if not isinstance(payload.get("decisions"), list):
        payload["decisions"] = []
    if not isinstance(payload.get("undo_log"), list):
        payload["undo_log"] = []
    return payload


def _write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_decisions(root: Path, date: str) -> dict[str, Any]:
    date = validate_date(date)
    path = decisions_path(root, date)
    try:
        return _read_payload(path, date)
    except OSError as error:
        raise BroadcastDecisionError(f"cannot read broadcast decisions: {error}") from error


def effective_decisions(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in payload.get("decisions", []):
        if not isinstance(record, Mapping):
            continue
        event_id = str(record.get("event_id") or "").strip()
        action = str(record.get("action") or "").strip()
        if event_id and action in SUPPORTED_ACTIONS:
            latest[event_id] = dict(record)
    return latest


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def record_decision(
    root: Path,
    date: str,
    event_id: str,
    action: str,
    *,
    amount: float | None = None,
) -> tuple[dict[str, Any], bool]:
    """Append one decision; return ``(payload, changed)``.

    Re-applying the same effective action is a no-op, which is what makes
    double-click / retry safe.  ``prev_action`` is the effective action before
    this write.
    """
    date = validate_date(date)
    event_id = str(event_id or "").strip()
    if not event_id:
        raise BroadcastDecisionError("event_id is required")
    if action not in SUPPORTED_ACTIONS:
        raise BroadcastDecisionError("action must be accept or reject")
    if amount is not None:
        try:
            numeric = float(amount)
        except (TypeError, ValueError) as error:
            raise BroadcastDecisionError("amount must be a positive number") from error
        if not numeric > 0:
            raise BroadcastDecisionError("amount must be a positive number")
        amount = round(numeric, 2)
    with _locked_file(root, date) as path:
        payload = _read_payload(path, date)
        latest = effective_decisions(payload)
        previous = latest.get(event_id)
        previous_action = str(previous.get("action")) if previous else "pending"
        if previous_action == action:
            return payload, False
        record = {
            "event_id": event_id,
            "action": action,
            "amount": amount,
            "decided_at": _now_iso(),
            "prev_action": previous_action,
        }
        payload["decisions"].append(record)
        _write_payload(path, payload)
        return payload, True


def undo_decision(root: Path, date: str, event_id: str) -> tuple[dict[str, Any], bool]:
    """Pop the latest decision for ``event_id``; return ``(payload, changed)``."""
    date = validate_date(date)
    event_id = str(event_id or "").strip()
    if not event_id:
        raise BroadcastDecisionError("event_id is required")
    with _locked_file(root, date) as path:
        payload = _read_payload(path, date)
        decisions = payload.get("decisions", [])
        for index in range(len(decisions) - 1, -1, -1):
            record = decisions[index]
            if isinstance(record, Mapping) and str(record.get("event_id") or "") == event_id:
                payload["undo_log"].append(
                    {
                        "event_id": event_id,
                        "undone_action": str(record.get("action") or ""),
                        "undone_amount": record.get("amount"),
                        "decided_at": _now_iso(),
                    }
                )
                decisions.pop(index)
                _write_payload(path, payload)
                return payload, True
        return payload, False


def _add_money(mapping: dict[str, Any], currency: str, amount: float) -> None:
    previous = mapping.get(currency)
    mapping[currency] = round(float(previous or 0) + amount, 2)


def apply_decisions(
    ledger: Mapping[str, Any], payload: Mapping[str, Any]
) -> dict[str, Any]:
    """Return a ledger with accepted broadcasts counted and rejected ones hidden."""
    result = copy.deepcopy(dict(ledger))
    pending = list(result.get("pending_broadcast") or [])
    latest = effective_decisions(payload)
    remaining: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    for item in pending:
        if not isinstance(item, Mapping):
            continue
        event_id = str(item.get("event_id") or "")
        decision = latest.get(event_id) or {}
        action = str(decision.get("action") or "")
        if action == "reject":
            continue
        if action == "accept":
            amount = decision.get("amount")
            if amount is None:
                amount = item.get("proposed_amount")
            try:
                numeric = float(amount)
            except (TypeError, ValueError):
                remaining.append(dict(item))
                continue
            if numeric <= 0:
                remaining.append(dict(item))
                continue
            transaction = dict(item)
            transaction.update(
                {
                    "amount": round(numeric, 2),
                    "amount_cents": int(round(numeric * 100)),
                    "review_status": "confirmed",
                    "decision": "pending_broadcast_accept",
                }
            )
            transaction.pop("proposed_amount", None)
            transaction.pop("reason", None)
            accepted.append(transaction)
            continue
        remaining.append(dict(item))

    result["pending_broadcast"] = remaining
    result["pending_broadcast_count"] = len(remaining)
    result["pending_broadcast_total_cny"] = round(
        sum(
            float(item.get("proposed_amount") or 0)
            for item in remaining
            if str(item.get("currency") or "CNY").upper() == "CNY"
        ),
        2,
    )
    if not accepted:
        return result

    transactions = list(result.get("transactions") or [])
    all_transactions = list(result.get("all_transactions") or transactions)
    payment_transactions = list(result.get("payment_transactions") or transactions)
    category_breakdown = dict(result.get("category_breakdown") or {})
    totals_by_currency = dict(result.get("totals_by_currency") or {})
    category_totals = {
        currency: dict(rows or {})
        for currency, rows in (result.get("category_totals_by_currency") or {}).items()
    }
    kind_totals = {
        kind: dict(rows or {})
        for kind, rows in (result.get("kind_totals_by_currency") or {}).items()
    }
    payment_kind_totals = {
        kind: dict(rows or {})
        for kind, rows in (result.get("payment_kind_totals_by_currency") or {}).items()
    }
    for transaction in accepted:
        currency = str(transaction.get("currency") or "CNY").upper()
        amount = float(transaction["amount"])
        category = str(transaction.get("category") or "other")
        kind = str(transaction.get("kind") or "consumption")
        transactions.append(transaction)
        all_transactions.append(transaction)
        if kind in {"consumption", "transfer", "withdrawal", "refund", "topup"}:
            payment_transactions.append(transaction)
        _add_money(totals_by_currency, currency, amount)
        category_totals.setdefault(currency, {})
        _add_money(category_totals[currency], category, amount)
        kind_totals.setdefault(kind, {})
        _add_money(kind_totals[kind], currency, amount)
        payment_kind_totals.setdefault(kind, {})
        _add_money(payment_kind_totals[kind], currency, amount)
        if currency == "CNY":
            _add_money(category_breakdown, category, amount)

    accepted_count = len(accepted)
    result["transactions"] = transactions
    result["payment_transactions"] = payment_transactions
    result["all_transactions"] = all_transactions
    result["totals_by_currency"] = totals_by_currency
    result["category_totals_by_currency"] = category_totals
    result["kind_totals_by_currency"] = kind_totals
    result["payment_kind_totals_by_currency"] = payment_kind_totals
    result["category_breakdown"] = category_breakdown
    result["total_amount"] = round(float(totals_by_currency.get("CNY") or 0), 2)
    result["transaction_count"] = int(result.get("transaction_count") or 0) + accepted_count
    result["payment_transaction_count"] = (
        int(result.get("payment_transaction_count") or 0) + accepted_count
    )
    result["payment_transactions_count"] = result["payment_transaction_count"]
    result["all_transaction_count"] = (
        int(result.get("all_transaction_count") or 0) + accepted_count
    )
    return result
