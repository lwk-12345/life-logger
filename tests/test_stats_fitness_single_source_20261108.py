"""唯一事实来源：/api/stats 的 exercise_count 必须与 /api/daily 的 fitness_ledger 同源。"""
from __future__ import annotations

import json
from pathlib import Path

from life_logger.day_facts import domain_ledgers
from life_logger.event_view import build_event_view
from life_logger.statistics import build_range_statistics

DAY_WITH_ACTIVITY = "2026-11-05"
DAY_CUTOVER = "2026-11-06"


def write_summary(root: Path, day: str, events: list[dict]) -> None:
    target = root / "jobs" / "job-test" / "files" / "dg" / "result" / day / "daily_summary.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "schema_version": "0.1", "date": day, "timezone": "Asia/Shanghai",
        "event_count": len(events), "timeline": events,
        "category_summary": {}, "warnings": [],
    }, ensure_ascii=False), encoding="utf-8")


def write_activity(root: Path, day: str, sessions: int) -> None:
    items = []
    for index in range(sessions):
        items.append({
            "item_id": f"i{index}", "action": f"练胸第{index + 1}场", "status": "completed",
            "fact_type": "exercise",
            "spoken_at": f"{day}T{18 + index * 3}:00:00+08:00", "occurred_at": None,
            "amount": None, "currency": None, "unit": None,
            "evidence": [{"source_id": f"src{index}", "quote": f"练胸第{index + 1}场",
                          "source_label": "self"}],
        })
    path = root / "activity-timeline" / "jev" / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"activities": [
        {"activity_id": "a1", "lifecycle": "live", "items": items}]}, ensure_ascii=False),
        encoding="utf-8")


def exercise_event(day: str, event_id: str = "ev-ex") -> dict:
    return {"event_id": event_id, "title": "健身锻炼：练腿", "categories": ["exercise"],
            "activity_type": "exercise", "time": {"absolute": f"{day}T17:00:00+08:00",
                                                  "precision": "exact", "relative_seconds": 0},
            "data": {"exercise": {"exercises": [{"name": "深蹲", "sets": 3}]}},
            "capture_mode": "passive", "source_type": "audio", "confidence": 0.9,
            "evidence": {"quote": "练腿", "source_label": "self"}, "review_status": "unreviewed"}


def test_activity_only_day_matches_fitness_ledger(tmp_path):
    """只有活动层健身会话的日期：exercise_count 与 fitness_ledger.workout_count 一致。"""
    root = tmp_path / "local-web"
    write_activity(root, DAY_WITH_ACTIVITY, sessions=2)
    write_summary(root, DAY_WITH_ACTIVITY, [])
    stats = build_range_statistics(root, date_from=DAY_WITH_ACTIVITY, date_to=DAY_WITH_ACTIVITY, view="all")
    events = build_event_view(root, date_from=DAY_WITH_ACTIVITY, date_to=DAY_WITH_ACTIVITY)["events"]
    expected = domain_ledgers(events, DAY_WITH_ACTIVITY, root=root)["fitness"]["workout_count"]
    assert stats["exercise_count"] == expected == 2


def test_event_plus_activity_counts_once_after_cutover(tmp_path):
    """11-06 之后：自动 exercise 事件 + 活动层 2 场训练 → 以账本为准（=2，不重复计事件）。"""
    root = tmp_path / "local-web"
    write_activity(root, DAY_CUTOVER, sessions=2)
    write_summary(root, DAY_CUTOVER, [exercise_event(DAY_CUTOVER)])
    stats = build_range_statistics(root, date_from=DAY_CUTOVER, date_to=DAY_CUTOVER, view="all")
    events = build_event_view(root, date_from=DAY_CUTOVER, date_to=DAY_CUTOVER)["events"]
    expected = domain_ledgers(events, DAY_CUTOVER, root=root)["fitness"]["workout_count"]
    assert stats["exercise_count"] == expected == 2


def test_without_activity_products_falls_back_to_event_count(tmp_path):
    """没有活动层产物的旧日期：仍按 `exercise` 类别计数（行为不变）。"""
    root = tmp_path / "local-web"
    root.mkdir(parents=True)
    write_summary(root, "2026-10-20", [exercise_event("2026-10-20"), exercise_event("2026-10-20", "ev-ex2")])
    stats = build_range_statistics(root, date_from="2026-10-20", date_to="2026-10-20", view="all")
    assert stats["exercise_count"] == 2
    assert stats["category_counts"]["exercise"] == 2
