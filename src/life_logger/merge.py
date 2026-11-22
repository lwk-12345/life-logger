"""Conservative merging of one event split across adjacent utterances."""

from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from typing import Any

from life_logger.domain import LifeEvent, validate_event

_CATEGORY_ORDER = (
    "routine",
    "expense",
    "food",
    "exercise",
    "work_communication",
    "idea",
    "todo",
    "other",
)
_UTTERANCE_SUFFIX = re.compile(r"_u\d+$")


def _source_segment_id(event: LifeEvent) -> str | None:
    value = event.evidence.get("segment_id")
    if not value:
        return None
    return _UTTERANCE_SUFFIX.sub("", str(value))


def _start_seconds(event: LifeEvent) -> float | None:
    value = event.evidence.get("start_offset_seconds")
    if value is not None:
        return float(value)
    return event.time.relative_seconds


def _end_seconds(event: LifeEvent) -> float | None:
    value = event.evidence.get("end_offset_seconds")
    if value is not None:
        return float(value)
    return event.time.relative_seconds


def _merge_compatible(
    left: LifeEvent,
    right: LifeEvent,
    *,
    max_gap_seconds: float,
) -> bool:
    if left.evidence.get("recording_id") != right.evidence.get("recording_id"):
        return False
    left_source = _source_segment_id(left)
    right_source = _source_segment_id(right)
    if left_source is None or left_source != right_source:
        return False
    left_end = _end_seconds(left)
    right_start = _start_seconds(right)
    if left_end is None or right_start is None:
        return False
    if right_start < left_end or right_start - left_end > max_gap_seconds:
        return False
    if not set(left.categories).intersection(right.categories):
        return False
    if "expense" in left.categories and "expense" in right.categories:
        left_expense = (
            left.data.get("expense") if isinstance(left.data, dict) else None
        )
        right_expense = (
            right.data.get("expense") if isinstance(right.data, dict) else None
        )
        if (
            isinstance(left_expense, dict)
            and isinstance(right_expense, dict)
            and (
                left_expense.get("amount") is not None
                or right_expense.get("amount") is not None
            )
            and (
                left_expense.get("amount") != right_expense.get("amount")
                or left_expense.get("item") != right_expense.get("item")
            )
        ):
            return False
    return True


def _merge_dicts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(left)
    for key, value in right.items():
        if key not in result:
            result[key] = deepcopy(value)
        elif isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge_dicts(result[key], value)
        elif isinstance(result[key], list) and isinstance(value, list):
            for item in value:
                if item not in result[key]:
                    result[key].append(deepcopy(item))
    return result


def _quotes(event: LifeEvent) -> list[str]:
    existing = event.evidence.get("quotes")
    if isinstance(existing, list):
        return [str(item) for item in existing]
    quote = event.evidence.get("quote")
    return [str(quote)] if quote else []


def _merged_event(left: LifeEvent, right: LifeEvent) -> LifeEvent:
    categories = sorted(
        set(left.categories).union(right.categories),
        key=_CATEGORY_ORDER.index,
    )
    left_quotes = _quotes(left)
    right_quotes = _quotes(right)
    quotes = left_quotes + [quote for quote in right_quotes if quote not in left_quotes]
    segment_ids = []
    for event in (left, right):
        segment_id = event.evidence.get("segment_id")
        if segment_id and segment_id not in segment_ids:
            segment_ids.append(segment_id)
    evidence = deepcopy(left.evidence)
    evidence.update(
        {
            "segment_ids": segment_ids,
            "quotes": quotes,
            "quote": " ".join(quotes),
            "start_offset_seconds": min(
                value
                for value in (_start_seconds(left), _start_seconds(right))
                if value is not None
            ),
            "end_offset_seconds": max(
                value
                for value in (_end_seconds(left), _end_seconds(right))
                if value is not None
            ),
        }
    )
    raw_id = "|".join(sorted((left.event_id, right.event_id)))
    event_id = f"evt_{hashlib.sha256(('merged|' + raw_id).encode()).hexdigest()[:12]}"
    merged = LifeEvent(
        event_id=event_id,
        categories=categories,
        activity_type=left.activity_type or right.activity_type,
        title="；".join(dict.fromkeys((left.title, right.title))),
        time=left.time,
        data=_merge_dicts(left.data, right.data),
        capture_mode=(
            "active"
            if "active" in {left.capture_mode, right.capture_mode}
            else left.capture_mode
        ),
        source_type=left.source_type,
        confidence=min(left.confidence, right.confidence),
        evidence=evidence,
        review_status=(
            "rejected"
            if "rejected" in {left.review_status, right.review_status}
            else (
                "confirmed"
                if {left.review_status, right.review_status} == {"confirmed"}
                else "unreviewed"
            )
        ),
    )
    validate_event(merged.to_dict())
    return merged


def merge_adjacent_events(
    events: list[LifeEvent],
    *,
    max_gap_seconds: float = 8.0,
) -> list[LifeEvent]:
    """Merge related utterances without crossing original ASR segment boundaries."""

    if max_gap_seconds < 0:
        raise ValueError("max_gap_seconds must be non-negative")
    result: list[LifeEvent] = []
    for event in events:
        if result and _merge_compatible(
            result[-1], event, max_gap_seconds=max_gap_seconds
        ):
            result[-1] = _merged_event(result[-1], event)
        else:
            result.append(event)
    return result
