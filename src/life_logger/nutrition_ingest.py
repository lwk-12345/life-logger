"""Read-only nutrition day projection; intake comes from the Watch pipeline.

饮食只来自手表，2026-11-06 用户决定。OpenClaw only reads these results.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from life_logger.day_facts import domain_ledgers
from life_logger.event_view import build_event_view


class NutritionStorageError(RuntimeError):
    """A legacy nutrition file cannot be read safely; never overwrite it."""


def validate_day(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("date 必须是 YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date 无效") from exc
    return value


def _ledger(root: Path, day: str) -> dict[str, Any]:
    from life_logger.food_lookup import FoodLookup, default_food_lookup

    view = build_event_view(root, date_from=day, date_to=day, view="display", use_cache=False)
    path = root / "activity-timeline" / "jev" / f"{day}.json"
    activities = []
    if path.exists():
        activity_payload = json.loads(path.read_text(encoding="utf-8"))
        activities = activity_payload.get("activities") or []
    # Same local library/indices; online OFF could write brand-cache during GET.
    engine = default_food_lookup()
    readonly = FoodLookup(data_dir=engine.data_dir, brand_library=engine.brand, off_enabled=False)
    return domain_ledgers(
        view["events"], activities=activities, day=day,
        nutrition_options={"web_root": root, "nutrition_lookup": readonly},
    )["nutrition"]


def _totals(ledger: Mapping[str, Any]) -> dict[str, Any] | None:
    if not ledger["has_records"]:
        return None
    return {key: value if ledger["totals_known_counts"].get(key) else None
            for key, value in ledger["totals"].items()}


def read_day(root: Path, day: str) -> dict[str, Any]:
    day = validate_day(day)
    # Keep the existing GET failure contract for any legacy file, without
    # receipts, idempotency or a write path. Production currently has none.
    path = Path(root) / "jobs" / f"job-nutrition-{day.replace('-', '')}" / "manual-events.json"
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise NutritionStorageError("饮食手动事件文件不可读，未覆盖") from exc
        if not isinstance(saved, dict) or not isinstance(saved.get("events"), list):
            raise NutritionStorageError("饮食手动事件文件格式无效，未覆盖")
    ledger = _ledger(Path(root), day)
    return {"ok": True, "day": day, "has_records": ledger["has_records"],
            "totals": _totals(ledger), "meals": ledger["meals"],
            "estimated_share": ledger["estimated_share"],
            "estimated_calories_kcal": ledger["estimated_calories_kcal"],
            "coverage": ledger["totals_coverage"],
            "unknown_nutrition_count": ledger["unknown_nutrition_count"],
            "needs_review": ledger["needs_review"]}
