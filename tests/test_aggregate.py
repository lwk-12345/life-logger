from datetime import datetime, timedelta

from life_logger.aggregate import (
    _duplicate_window_seconds,
    _semantic_duplicate,
    build_daily_summary,
    select_timeline_events,
)
from life_logger.domain import EventTime, LifeEvent


def event(
    event_id: str,
    category: str,
    title: str,
    second: float,
    *,
    data: dict | None = None,
) -> LifeEvent:
    return LifeEvent(
        event_id=event_id,
        categories=[category],
        activity_type=None,
        title=title,
        time=EventTime(
            absolute=(
                datetime.fromisoformat("2026-09-02T08:00:00+08:00")
                + timedelta(seconds=second)
            ).isoformat(),
            relative_seconds=second,
            precision="exact",
        ),
        data=data or {category: {}},
        capture_mode="passive",
        source_type="audio",
        confidence=0.8,
        evidence={"quote": title},
        review_status="unreviewed",
    )


def test_daily_summary_sorts_timeline_and_counts_categories() -> None:
    summary = build_daily_summary(
        [
            event("e2", "food", "吃早餐", 60),
            event("e1", "routine", "起床", 10),
        ],
        date="2026-09-02",
        timezone_name="Asia/Shanghai",
        warnings=["one time is inferred"],
    )

    assert [item["event_id"] for item in summary["timeline"]] == ["e1", "e2"]
    assert summary["category_summary"]["food"]["count"] == 1
    assert summary["warnings"] == ["one time is inferred"]


def test_display_timeline_filters_non_events_and_deduplicates_repeats() -> None:
    routine = event("e1", "routine", "起床", 10)
    idea = event("e2", "idea", "我觉得可以换一个方案", 20)
    generic_work = event("e3", "work_communication", "这个项目我再想想", 30)
    meeting = event("e4", "work_communication", "十点开会讨论需求", 40)
    duplicate_meeting = event("e5", "work_communication", "十点开会讨论需求", 80)

    selected = select_timeline_events(
        [routine, idea, generic_work, meeting, duplicate_meeting]
    )

    assert [item.event_id for item in selected] == ["e1", "e4"]



def test_display_timeline_semantically_merges_adjacent_repeated_utterances() -> None:
    first = event(
        "e1",
        "work_communication",
        "开发 Life Logger 的录音处理功能",
        40,
    )
    second = event(
        "e2",
        "work_communication",
        "然后继续开发 Life Logger 录音处理功能",
        75,
    )
    third = event(
        "e3",
        "work_communication",
        "开发另外一个独立项目的测试页面",
        90,
    )

    selected = select_timeline_events([first, second, third])

    assert [item.event_id for item in selected] == ["e1", "e3"]
    assert selected[0].evidence["merged_event_ids"] == ["e1", "e2"]

def test_daily_summary_keeps_full_events_but_exposes_filtered_display_timeline(
) -> None:
    summary = build_daily_summary(
        [
            event("e1", "routine", "起床", 10),
            event("e2", "idea", "我觉得可以换一个方案", 20),
        ],
        date="2026-09-02",
        timezone_name="Asia/Shanghai",
        warnings=[],
    )

    assert summary["event_count"] == 2
    assert len(summary["timeline"]) == 2
    assert summary["display_timeline_count"] == 1
    assert [item["event_id"] for item in summary["display_timeline"]] == ["e1"]


def test_food_duplicates_use_ten_minute_window_and_structured_item() -> None:
    events = [
        event(
            "milk-1",
            "food",
            "今天喝了九百毫升的纯牛奶",
            0,
            data={"food": {"item": "纯牛奶"}},
        ),
        event(
            "milk-2",
            "food",
            "刚喝了九百毫升牛奶",
            300,
            data={"food": {"item": "牛奶"}},
        ),
        event(
            "milk-3",
            "food",
            "今天喝的还是纯牛奶",
            540,
            data={"food": {"item": "纯牛奶"}},
        ),
        event(
            "milk-later",
            "food",
            "又喝了九百毫升纯牛奶",
            1200,
            data={"food": {"item": "纯牛奶"}},
        ),
    ]

    selected = select_timeline_events(events)

    assert [item.event_id for item in selected] == ["milk-1", "milk-later"]
    assert selected[0].evidence["merged_event_ids"] == ["milk-1", "milk-2", "milk-3"]


def test_todo_duplicates_compare_intent_inside_fifteen_minute_window() -> None:
    first = event("todo-1", "todo", "提醒我提交周报", 0)
    second = event("todo-2", "todo", "我需要去提交周报", 600)

    assert _duplicate_window_seconds(first, second, 180.0) == 900.0
    assert _semantic_duplicate(first, second) is True
