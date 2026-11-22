import json
from datetime import datetime
from pathlib import Path

from life_logger.local_reminders import LocalReminderStore
from life_logger.patterns import build_pattern_observations


class FakeNotifier:
    name = "fake-local"

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def notify(self, *, title: str, body: str) -> bool:
        self.messages.append((title, body))
        return True


def _event(
    event_id: str,
    category: str,
    title: str,
    timestamp: str,
    *,
    amount: float | None = None,
) -> dict:
    data: dict[str, object] = {category: {}}
    if category == "expense":
        data["expense"] = {"amount": amount, "currency": "CNY"}
    return {
        "event_id": event_id,
        "categories": [category],
        "title": title,
        "time": {"absolute": timestamp, "precision": "exact"},
        "data": data,
        "confidence": 0.9,
        "evidence": {"quote": title},
    }


def _write_day(job_root: Path, day: str, events: list[dict]) -> None:
    target = (
        job_root
        / "jobs"
        / f"job-{day}"
        / "files"
        / day
        / "result"
        / day
        / "daily_summary.json"
    )
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                "date": day,
                "timezone": "Asia/Shanghai",
                "timeline": events,
                "event_count": len(events),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_todo_becomes_local_reminder_and_due_notification(tmp_path: Path) -> None:
    notifier = FakeNotifier()
    store = LocalReminderStore(tmp_path, notifier=notifier)

    reminders = store.sync_tasks(
        [{"task_key": "todo-1", "title": "交水电费", "status": "pending"}]
    )
    assert reminders[0]["status"] == "unscheduled"
    assert reminders[0]["backend"] == "local"

    scheduled = store.schedule(
        "todo-1",
        remind_at="2026-09-22T09:00:00+08:00",
    )
    assert scheduled["status"] == "scheduled"

    delivered = store.dispatch_due(
        now=datetime.fromisoformat("2026-09-22T09:01:00+08:00")
    )
    assert len(delivered) == 1
    assert delivered[0]["status"] == "sent"
    assert notifier.messages == [("Life Logger 待办提醒", "交水电费")]

    completed = store.sync_tasks(
        [{"task_key": "todo-1", "title": "交水电费", "status": "completed"}]
    )
    assert completed[0]["status"] == "completed"


def test_dismissed_todo_cancels_scheduled_local_reminder(tmp_path: Path) -> None:
    notifier = FakeNotifier()
    store = LocalReminderStore(tmp_path, notifier=notifier)
    store.sync_tasks(
        [{"task_key": "todo-ignore", "title": "过期待办", "status": "pending"}]
    )
    store.schedule(
        "todo-ignore",
        remind_at="2026-09-22T09:00:00+08:00",
    )

    dismissed = store.sync_tasks(
        [{"task_key": "todo-ignore", "title": "过期待办", "status": "dismissed"}]
    )

    assert dismissed[0]["status"] == "dismissed"
    assert store.dispatch_due(
        now=datetime.fromisoformat("2026-09-22T09:01:00+08:00")
    ) == []
    assert notifier.messages == []


def test_pattern_alert_candidates_are_observational_and_sample_gated(
    tmp_path: Path,
) -> None:
    root = tmp_path / "local-web"
    for index, day in enumerate(("2026-09-20", "2026-09-21", "2026-09-22"), start=1):
        _write_day(
            root,
            day,
            [
                _event(
                    f"wake-{index}",
                    "routine",
                    "起床",
                    f"{day}T{6 + index * 2:02d}:00:00+08:00",
                ),
                _event(
                    f"food-{index}",
                    "food",
                    "夜宵",
                    f"{day}T22:00:00+08:00",
                ),
            ],
        )

    payload = build_pattern_observations(
        root,
        date_from="2026-09-20",
        date_to="2026-09-22",
    )

    assert payload["sample_ready"] is True
    candidates = payload["alert_candidates"]
    kinds = {item["kind"] for item in candidates}
    assert "exercise_record_gap" in kinds
    assert "wake_time_spread" in kinds
    assert "late_food_records" in kinds
    assert all(item["severity"] == "info" for item in candidates)
    assert all(
        "诊断" in item["statement"] or "观察" in item["label"]
        for item in candidates
    )
