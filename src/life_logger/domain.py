"""Canonical data contracts for the Life Logger pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping

Category = Literal[
    "routine",
    "food",
    "expense",
    "exercise",
    "work_communication",
    "idea",
    "todo",
    "other",
]
TimeSource = Literal[
    "filename",
    "metadata",
    "sidecar",
    "filesystem",
    "user",
    "unavailable",
]
TimePrecision = Literal["exact", "inferred", "relative", "unknown"]
CaptureMode = Literal["active", "passive", "unknown"]
SourceType = Literal["audio", "image", "video", "sensor", "manual", "import"]
ReviewStatus = Literal["unreviewed", "confirmed", "rejected"]

ALLOWED_CATEGORIES = {
    "routine",
    "food",
    "expense",
    "exercise",
    "work_communication",
    "idea",
    "todo",
    "other",
}


@dataclass(frozen=True)
class RecordingSegment:
    recording_id: str
    source_file: str
    started_at: str | None
    ended_at: str | None
    duration_seconds: float | None
    sequence: int
    time_source: TimeSource
    time_precision: TimePrecision
    ingestion_warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TranscriptSegment:
    segment_id: str
    recording_id: str
    start_offset_seconds: float
    end_offset_seconds: float
    absolute_started_at: str | None
    absolute_ended_at: str | None
    text: str
    source: str
    language: str
    transcription_confidence: float | None
    speaker: str | None = None
    gender: str | None = None
    emotion: str | None = None
    words: list[dict[str, Any]] | None = None
    physical_pin: str | None = None
    pin_age_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EventTime:
    absolute: str | None
    relative_seconds: float | None
    precision: TimePrecision

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LifeEvent:
    event_id: str
    categories: list[Category]
    activity_type: str | None
    title: str
    time: EventTime
    data: dict[str, Any]
    capture_mode: CaptureMode
    source_type: SourceType
    confidence: float
    evidence: dict[str, Any]
    review_status: ReviewStatus

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_event(event: dict[str, Any]) -> None:
    """Validate the stable fields that every serialized event must contain."""
    required = {
        "event_id",
        "categories",
        "title",
        "time",
        "data",
        "capture_mode",
        "source_type",
        "confidence",
        "evidence",
        "review_status",
    }
    missing = required.difference(event)
    if missing:
        raise ValueError(f"event is missing fields: {sorted(missing)}")
    if not event["categories"]:
        raise ValueError("event must have at least one category")
    invalid = set(event["categories"]).difference(ALLOWED_CATEGORIES)
    if invalid:
        raise ValueError(f"unknown categories: {sorted(invalid)}")
    if not isinstance(event["confidence"], (int, float)):
        raise ValueError("confidence must be numeric")
    if not 0 <= event["confidence"] <= 1:
        raise ValueError("confidence must be between 0 and 1")
    if event["time"]["precision"] == "exact" and not event["time"]["absolute"]:
        raise ValueError("exact event time requires an absolute timestamp")


# ---------------------------------------------------------------------------
# Decoupled Multi-Domain Schemas (Features F8, F10)
# ---------------------------------------------------------------------------

DomainType = Literal["Finance", "Diet", "Todos", "Insights"]
ALLOWED_DOMAINS: set[str] = {"Finance", "Diet", "Todos", "Insights"}

TodoStatus = Literal["pending", "completed", "dismissed"]
ALLOWED_TODO_STATUSES: set[str] = {"pending", "completed", "dismissed"}

InsightType = Literal["thought", "idea", "reflection"]
ALLOWED_INSIGHT_TYPES: set[str] = {"thought", "idea", "reflection"}


@dataclass(frozen=True)
class Finance:
    """Finance domain model: direct expenditures, refunds, net amounts.

    Also tracks approximate indicators.
    """

    amount: float | int | None = None
    currency: str = "CNY"
    net_amount: float | int | None = None
    is_refund: bool = False
    is_approximate: bool = False
    item: str | None = None
    counterparty: str | None = None
    category: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Finance:
        amt = data.get("amount")
        is_ref = bool(data.get("is_refund", False))
        net = data.get("net_amount", calculate_net_amount(amt, is_refund=is_ref))
        return cls(
            amount=amt,
            currency=str(data.get("currency") or "CNY"),
            net_amount=net,
            is_refund=is_ref,
            is_approximate=bool(data.get("is_approximate", False)),
            item=data.get("item"),
            counterparty=data.get("counterparty"),
            category=data.get("category"),
            metadata=dict(data.get("metadata") or {}),
        )

    @classmethod
    def from_event(cls, event: LifeEvent | dict[str, Any]) -> Finance:
        d = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        exp = d.get("data", {}).get("expense", {})
        return cls.from_dict(exp)


@dataclass(frozen=True)
class Diet:
    """Diet domain model: actual food/beverage intake and nutritional consumption.

    Distinct from grocery purchase.
    """

    food_items: list[str] = field(default_factory=list)
    meal_type: str | None = None
    beverage: str | None = None
    # True indicates actual consumption/intake, distinct from grocery purchase
    is_intake: bool = True
    estimated_calories: float | None = None
    nutritional_notes: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Diet:
        items = data.get("food_items")
        if items is None:
            raw_items = data.get("items") or []
            items = [
                item["name"] if isinstance(item, dict) and "name" in item else str(item)
                for item in raw_items
            ]
        return cls(
            food_items=items,
            meal_type=data.get("meal_type"),
            beverage=data.get("beverage"),
            is_intake=bool(data.get("is_intake", True)),
            estimated_calories=data.get("estimated_calories"),
            nutritional_notes=data.get("nutritional_notes"),
            metadata=dict(data.get("metadata") or {}),
        )

    @classmethod
    def from_event(cls, event: LifeEvent | dict[str, Any]) -> Diet:
        d = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        food_data = d.get("data", {}).get("food", {})
        return cls.from_dict(food_data)


@dataclass(frozen=True)
class Todos:
    """Todos domain model: action items, statuses (pending/completed/dismissed)."""

    task: str
    status: TodoStatus = "pending"
    due_time: str | None = None
    priority: str | None = None
    completed_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Todos:
        return cls(
            task=str(data.get("task") or data.get("title") or "").strip(),
            status=data.get("status", "pending"),
            due_time=data.get("due_time"),
            priority=data.get("priority"),
            completed_at=data.get("completed_at"),
            metadata=dict(data.get("metadata") or {}),
        )

    @classmethod
    def from_event(cls, event: LifeEvent | dict[str, Any]) -> Todos:
        d = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        todo_data = d.get("data", {}).get("todo", {})
        if not todo_data.get("task") and d.get("title"):
            todo_data = dict(todo_data)
            todo_data["task"] = d["title"]
        return cls.from_dict(todo_data)


@dataclass(frozen=True)
class Insights:
    """Insights domain model: ephemeral thoughts, ideas, reflections."""

    content: str
    insight_type: InsightType = "thought"
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Insights:
        content = data.get("content") or data.get("summary") or data.get("topic") or ""
        raw_type = data.get("insight_type") or data.get("activity_type") or "thought"
        itype = raw_type if raw_type in ALLOWED_INSIGHT_TYPES else "thought"
        return cls(
            content=str(content).strip(),
            insight_type=itype,
            tags=list(data.get("tags") or []),
            metadata=dict(data.get("metadata") or {}),
        )

    @classmethod
    def from_event(cls, event: LifeEvent | dict[str, Any]) -> Insights:
        d = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        idea_data = d.get("data", {}).get("idea", {})
        if not idea_data.get("content") and d.get("title"):
            idea_data = dict(idea_data)
            idea_data["content"] = d["title"]
        return cls.from_dict(idea_data)


def is_grocery_purchase(data_or_event: Mapping[str, Any]) -> bool:
    """Identify whether data represents grocery purchase rather than meal intake."""
    raw = (
        data_or_event.to_dict()
        if hasattr(data_or_event, "to_dict")
        else dict(data_or_event)
    )
    cats = raw.get("categories") or []
    if "expense" in cats and "food" not in cats:
        return True
    data = raw.get("data") or {}
    exp = data.get("expense", {}) if isinstance(data, dict) else {}
    item_str = str(exp.get("item") or raw.get("title") or "").lower()
    return any(
        k in item_str for k in ("超市", "食材", "生鲜", "买菜", "朴朴", "山姆", "盒马")
    )


def is_dietary_intake(data_or_event: Mapping[str, Any]) -> bool:
    """Identify whether data represents food/beverage intake and consumption."""
    raw = (
        data_or_event.to_dict()
        if hasattr(data_or_event, "to_dict")
        else dict(data_or_event)
    )
    cats = raw.get("categories") or []
    if "food" in cats and "expense" not in cats:
        return True
    data = raw.get("data") or {}
    food = data.get("food", {}) if isinstance(data, dict) else {}
    if "is_intake" in food:
        return bool(food["is_intake"])
    activity = str(raw.get("activity_type") or "").lower()
    return activity in {
        "meal",
        "snack",
        "drink",
        "beverage",
        "dining",
        "eating",
        "food_intake",
    }


def calculate_net_amount(
    amount: float | int | None,
    is_refund: bool = False,
    refund_offset: float | int = 0,
) -> float | int | None:
    """Calculate net expenditure amount accounting for refunds or offsets."""
    if amount is None:
        return None
    if is_refund:
        return -abs(amount)
    if refund_offset:
        return amount - refund_offset
    return amount


def validate_finance(data: dict[str, Any]) -> None:
    """Validate Finance domain schema fields."""
    if "amount" in data and data["amount"] is not None:
        if not isinstance(data["amount"], (int, float)):
            raise ValueError("Finance amount must be numeric or None")
    if "net_amount" in data and data["net_amount"] is not None:
        if not isinstance(data["net_amount"], (int, float)):
            raise ValueError("Finance net_amount must be numeric or None")
    if "currency" in data and not isinstance(data["currency"], str):
        raise ValueError("Finance currency must be a string")
    if "is_refund" in data and not isinstance(data["is_refund"], bool):
        raise ValueError("Finance is_refund must be a boolean")
    if "is_approximate" in data and not isinstance(data["is_approximate"], bool):
        raise ValueError("Finance is_approximate must be a boolean")


def validate_diet(data: dict[str, Any]) -> None:
    """Validate Diet domain schema fields."""
    if "food_items" in data and not isinstance(data["food_items"], list):
        raise ValueError("Diet food_items must be a list of strings")
    if "is_intake" in data and not isinstance(data["is_intake"], bool):
        raise ValueError("Diet is_intake must be a boolean")
    if "estimated_calories" in data and data["estimated_calories"] is not None:
        if not isinstance(data["estimated_calories"], (int, float)):
            raise ValueError("Diet estimated_calories must be numeric or None")


def validate_todos(data: dict[str, Any]) -> None:
    """Validate Todos domain schema fields."""
    if "task" not in data or not str(data["task"]).strip():
        raise ValueError("Todos require a non-empty task")
    if "status" in data and data["status"] not in ALLOWED_TODO_STATUSES:
        raise ValueError(f"Unknown Todos status: {data['status']}")


def validate_insights(data: dict[str, Any]) -> None:
    """Validate Insights domain schema fields."""
    if "content" not in data or not str(data["content"]).strip():
        raise ValueError("Insights require non-empty content")
    if "insight_type" in data and data["insight_type"] not in ALLOWED_INSIGHT_TYPES:
        raise ValueError(f"Unknown Insights type: {data['insight_type']}")


def validate_domain_payload(domain: str, payload: dict[str, Any]) -> None:
    """Validate domain payload according to domain schema."""
    if domain == "Finance":
        validate_finance(payload)
    elif domain == "Diet":
        validate_diet(payload)
    elif domain == "Todos":
        validate_todos(payload)
    elif domain == "Insights":
        validate_insights(payload)
    else:
        raise ValueError(f"Unknown domain: {domain}")


CATEGORY_TO_DOMAIN_MAP: dict[str, DomainType] = {
    "expense": "Finance",
    "food": "Diet",
    "todo": "Todos",
    "idea": "Insights",
    "routine": "Todos",
    "exercise": "Diet",
    "work_communication": "Todos",
    "other": "Insights",
}


def map_category_to_domain(category: str | None) -> DomainType:
    """Map legacy event category to decoupled domain schema."""
    if not category:
        return "Insights"
    normalized = str(category).strip().lower()
    return CATEGORY_TO_DOMAIN_MAP.get(normalized, "Insights")


@dataclass(frozen=True)
class DomainEvent:
    """Decoupled life event mapped to an explicit domain schema."""

    event_id: str
    domain: DomainType
    title: str
    time: EventTime
    payload: dict[str, Any]
    capture_mode: CaptureMode
    source_type: SourceType
    confidence: float
    evidence: dict[str, Any]
    review_status: ReviewStatus

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def domain_event_from_life_event(life_event: LifeEvent | dict[str, Any]) -> DomainEvent:
    """Convert a standard LifeEvent into a decoupled DomainEvent."""
    raw = life_event.to_dict() if hasattr(life_event, "to_dict") else dict(life_event)
    cats = raw.get("categories") or ["other"]
    domain = map_category_to_domain(cats[0])
    data = raw.get("data") or {}
    payload = data.get(cats[0], {}) if isinstance(data, dict) else {}
    time_raw = raw.get("time") or {}
    event_time = (
        time_raw
        if isinstance(time_raw, EventTime)
        else EventTime(
            absolute=time_raw.get("absolute"),
            relative_seconds=time_raw.get("relative_seconds"),
            precision=time_raw.get("precision", "unknown"),
        )
    )
    return DomainEvent(
        event_id=raw["event_id"],
        domain=domain,
        title=raw.get("title", ""),
        time=event_time,
        payload=payload if isinstance(payload, dict) else {},
        capture_mode=raw.get("capture_mode", "passive"),
        source_type=raw.get("source_type", "audio"),
        confidence=float(raw.get("confidence", 0.0)),
        evidence=dict(raw.get("evidence") or {}),
        review_status=raw.get("review_status", "unreviewed"),
    )
