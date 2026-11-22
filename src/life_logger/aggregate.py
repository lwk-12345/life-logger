"""Build daily derived views from canonical life events."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from life_logger.domain import LifeEvent

_TIMELINE_CONCRETE_CATEGORIES = {
    "routine",
    "expense",
    "food",
    "exercise",
}
_TIMELINE_WORK_ACTION_MARKERS = (
    "开会",
    "会议",
    "汇报",
    "修复",
    "断网",
    "切换工作",
    "提交",
    "完成",
    "测试",
    "开发",
    "修改",
    "处理",
    "上线",
    "部署",
    "面试",
    "电话沟通",
    "和同事沟通",
    "跟同事沟通",
)


def _time_key(item: dict[str, Any]) -> tuple[int, float, float]:
    event_time = item["time"]
    absolute = event_time.get("absolute")
    if absolute:
        try:
            return (0, datetime.fromisoformat(absolute).timestamp(), 0.0)
        except ValueError:
            return (0, 0.0, event_time.get("relative_seconds") or 0.0)
    return (1, 0.0, event_time.get("relative_seconds") or 0.0)


def _event_text(event: LifeEvent) -> str:
    quote = str(event.evidence.get("quote") or "").strip()
    title = event.title.strip()
    if not quote or quote == title or title in quote:
        return quote or title
    return f"{title} {quote}".strip()


def _is_timeline_worthy(event: LifeEvent) -> bool:
    """Keep the day axis selective while preserving all canonical events."""

    if event.review_status == "rejected":
        return False
    if event.source_type == "manual" or event.review_status == "confirmed":
        return True

    categories = set(event.categories)
    if categories.intersection(_TIMELINE_CONCRETE_CATEGORIES):
        return True

    if "work_communication" in categories:
        if event.capture_mode == "active":
            return True
        text = _event_text(event)
        return any(marker in text for marker in _TIMELINE_WORK_ACTION_MARKERS)

    # Pure ideas and future/reminder items belong in the workspace lanes rather
    # than pretending they already happened on the chronological day axis.
    return False


def _event_seconds(event: LifeEvent) -> float | None:
    absolute = event.time.absolute
    if absolute:
        try:
            return datetime.fromisoformat(absolute).timestamp()
        except ValueError:
            pass
    return event.time.relative_seconds


def _timeline_signature(event: LifeEvent) -> tuple[tuple[str, ...], str]:
    title = "".join(
        char
        for char in event.title.lower()
        if char.isalnum() or "一" <= char <= "龥"
    )
    return tuple(sorted(event.categories)), title


def _normalized_event_text(event: LifeEvent) -> str:
    text = _event_text(event).lower()
    prefixes = (
        "嗯",
        "呃",
        "啊",
        "这个",
        "那个",
        "然后",
        "就是",
        "我觉得",
        "我想",
    )
    changed = True
    while changed:
        changed = False
        stripped = text.lstrip(" ，,。！？!?；;：:")
        for prefix in prefixes:
            if stripped.startswith(prefix):
                text = stripped[len(prefix) :]
                changed = True
                break
        else:
            text = stripped
    return "".join(
        char for char in text if char.isalnum() or "一" <= char <= "龥"
    )


def _char_ngrams(value: str, size: int = 3) -> set[str]:
    if not value:
        return set()
    if len(value) <= size:
        return {value}
    return {value[index : index + size] for index in range(len(value) - size + 1)}


def _todo_intent_text(event: LifeEvent) -> str:
    text = _normalized_event_text(event)
    for marker in (
        "提醒我",
        "记得",
        "待办",
        "明天要",
        "今天要",
        "我需要",
        "还需要",
        "需要我",
        "需要",
    ):
        text = text.replace(marker, "")
    return text


def _structured_food_duplicate(left: LifeEvent, right: LifeEvent) -> bool:
    if "food" not in left.categories or "food" not in right.categories:
        return False

    def _items(event: LifeEvent) -> set[str]:
        food = event.data.get("food") if isinstance(event.data, dict) else None
        if not isinstance(food, dict):
            return set()
        values: list[str] = []
        item = food.get("item")
        if item:
            values.append(str(item))
        for row in food.get("items") or []:
            if isinstance(row, dict) and row.get("name"):
                values.append(str(row["name"]))
        return {
            value.replace("纯牛奶", "牛奶").strip().lower()
            for value in values
            if value.strip()
        }

    left_items = _items(left)
    right_items = _items(right)
    return bool(left_items and right_items and left_items.intersection(right_items))


def _duplicate_window_seconds(
    left: LifeEvent,
    right: LifeEvent,
    default_seconds: float,
) -> float:
    left_categories = set(left.categories)
    right_categories = set(right.categories)
    if "todo" in left_categories and "todo" in right_categories:
        return max(default_seconds, 900.0)
    if (
        "food" in left_categories
        and "food" in right_categories
        or "exercise" in left_categories
        and "exercise" in right_categories
    ):
        return max(default_seconds, 600.0)
    return default_seconds


def _semantic_duplicate(left: LifeEvent, right: LifeEvent) -> bool:
    left_categories = set(left.categories)
    right_categories = set(right.categories)
    if left_categories != right_categories and not (
        "todo" in left_categories and "todo" in right_categories
    ):
        return False
    if (
        left.activity_type
        and right.activity_type
        and left.activity_type != right.activity_type
    ):
        return False
    left_text = _normalized_event_text(left)
    right_text = _normalized_event_text(right)
    if not left_text or not right_text:
        return False
    if _structured_food_duplicate(left, right):
        return True
    if "todo" in left_categories and "todo" in right_categories:
        left_text = _todo_intent_text(left)
        right_text = _todo_intent_text(right)
        if not left_text or not right_text:
            return False
    if left_text == right_text:
        return True
    shorter, longer = sorted((left_text, right_text), key=len)
    if len(shorter) >= 8 and shorter in longer and len(shorter) / len(longer) >= 0.62:
        return True
    left_grams = _char_ngrams(left_text)
    right_grams = _char_ngrams(right_text)
    union = left_grams | right_grams
    if not union:
        return False
    return len(left_grams & right_grams) / len(union) >= 0.64


def _merge_duplicate_event(primary: LifeEvent, duplicate: LifeEvent) -> LifeEvent:
    evidence = dict(primary.evidence)
    merged_ids = list(evidence.get("merged_event_ids") or [])
    for event_id in (primary.event_id, duplicate.event_id):
        if event_id not in merged_ids:
            merged_ids.append(event_id)
    evidence["merged_event_ids"] = merged_ids
    quotes = list(evidence.get("merged_quotes") or [])
    for candidate in (
        primary.evidence.get("quote"),
        duplicate.evidence.get("quote"),
    ):
        if candidate and candidate not in quotes:
            quotes.append(candidate)
    if quotes:
        evidence["merged_quotes"] = quotes
    # Prefer the more informative title but retain the first event id/time so
    # corrections and chronological ordering remain stable.
    title = primary.title
    if len(_normalized_event_text(duplicate)) > len(_normalized_event_text(primary)):
        title = duplicate.title
    return replace(
        primary,
        title=title,
        confidence=max(primary.confidence, duplicate.confidence),
        evidence=evidence,
    )


def select_timeline_events(
    events: list[LifeEvent],
    *,
    duplicate_window_seconds: float = 180.0,
) -> list[LifeEvent]:
    """Return high-signal, de-duplicated events for the user-facing day axis."""

    selected = [event for event in events if _is_timeline_worthy(event)]
    selected.sort(key=lambda event: _time_key(event.to_dict()))
    result: list[LifeEvent] = []
    for event in selected:
        seconds = _event_seconds(event)
        duplicate_index: int | None = None
        for index in range(len(result) - 1, -1, -1):
            previous_event = result[index]
            previous_seconds = _event_seconds(previous_event)
            if seconds is not None and previous_seconds is not None:
                delta = seconds - previous_seconds
                pair_window = _duplicate_window_seconds(
                    previous_event,
                    event,
                    duplicate_window_seconds,
                )
                if delta > max(900.0, duplicate_window_seconds):
                    break
                if delta > pair_window or delta < 0:
                    continue
            if _semantic_duplicate(previous_event, event):
                duplicate_index = index
                break
        if duplicate_index is not None:
            result[duplicate_index] = _merge_duplicate_event(
                result[duplicate_index], event
            )
            continue
        result.append(event)
    return result

def build_daily_summary(
    events: list[LifeEvent],
    *,
    date: str | None,
    timezone_name: str,
    warnings: list[str],
    job_root: Path | str | None = None,
) -> dict[str, Any]:
    """Return a human- and machine-readable summary for one processing run."""
    timeline = [
        event.to_dict() if hasattr(event, "to_dict") else dict(event)
        for event in events
    ]
    timeline.sort(key=_time_key)
    display_timeline = [
        event.to_dict() if hasattr(event, "to_dict") else dict(event)
        for event in select_timeline_events(events)
    ]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    derived_warnings = list(warnings)
    for item in timeline:
        for category in item["categories"]:
            grouped[category].append(item)
        if item["time"]["precision"] != "exact":
            derived_warnings.append(
                f"event {item['event_id']} has {item['time']['precision']} time"
            )
        if item["confidence"] < 0.7:
            derived_warnings.append(f"event {item['event_id']} has low confidence")

    category_summary = {
        category: {
            "count": len(items),
            "highlights": [item["title"] for item in items[:5]],
        }
        for category, items in sorted(grouped.items())
    }
    # [2026-11-07 唯一事实来源] 原来在这里自己 sum 金额（第二个口径）：
    # 不含活动层消费、也不走账本策略。现在统一交给 day_facts 的同一个账本公式。
    from life_logger.day_facts import day_expense_ledger, event_only_expense_ledger

    expense_events = [
        item
        for item in timeline
        if item.get("activity_type")
        not in {
            "decision_saving",
            "negated",
            "abandoned_purchase",
            "experience_sharing",
            "curiosity_question",
        }
        and item.get("data", {}).get("decision", {}).get("state") != "negated"
    ]
    if job_root is not None and date:
        expense_ledger = day_expense_ledger(expense_events, date, root=job_root)
    else:
        expense_ledger = event_only_expense_ledger(expense_events)
    expense_total = sum(
        float(transaction.get("amount") or 0.0)
        for transaction in expense_ledger.get("transactions", [])
        if isinstance(transaction, Mapping)
        and str(transaction.get("currency") or "CNY").strip() == "CNY"
        and transaction.get("kind", "consumption") in {"consumption", "transfer"}
    )
    return {
        "schema_version": "0.1",
        "date": date,
        "timezone": timezone_name,
        "event_count": len(events),
        "timeline": timeline,
        "display_timeline_count": len(display_timeline),
        "display_timeline": display_timeline,
        "category_summary": category_summary,
        "expense_total_cny": round(expense_total, 2),
        "warnings": list(dict.fromkeys(derived_warnings)),
    }
