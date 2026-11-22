"""占位动作名不得覆盖有意义的原标题（Q429 截图 18:06/18:18）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.activity_cards import build_activity_cards  # noqa: E402

PLACEHOLDERS = ("未说明", "未明确", "未知", "unknown", "未命名", "未识别", "动作未说明", "N/A", "None", "")


def _card(title: str, name: str) -> dict:
    event = {
        "event_id": "evt_placeholder",
        "title": title,
        "categories": ["exercise"],
        "time": {"precision": "minute", "absolute": "2026-10-25T18:06:09+08:00"},
        "data": {"exercise": {"exercises": [{"name": name, "sets": [{"set": 1, "weight_kg": 80}]}]}},
        "evidence": {"quote": "第三组八十", "source_label": "self", "extractor": "deepseek_context_batch"},
    }
    return build_activity_cards([event])["cards"][0]


@pytest.mark.parametrize("placeholder", PLACEHOLDERS)
def test_placeholder_name_does_not_override_meaningful_title(placeholder: str) -> None:
    card = _card("腿部训练：第三组八十", placeholder)
    assert card["title"] == "腿部训练：第三组八十", card["title"]
    assert card["title_original"] == "腿部训练：第三组八十"
    assert card["title_source"] == "event.title"


@pytest.mark.parametrize("placeholder", PLACEHOLDERS)
def test_placeholder_name_does_not_replace_generic_title_either(placeholder: str) -> None:
    """标题泛化但动作名是占位 → 保持原标题（不得变成「未说明」）。"""
    card = _card("力量训练", placeholder)
    assert card["title"] == "力量训练", card["title"]


def test_meaningful_action_name_still_wins_for_generic_title() -> None:
    card = _card("力量训练", "深蹲")
    assert card["title"] == "深蹲"
    assert card["title_source"] == "data.exercise.exercises[0].name"


def test_no_guessing_from_neighbouring_or_retrospective() -> None:
    """占位动作名时不得从回顾/邻近内容猜「深蹲」。"""
    event = {
        "event_id": "evt_placeholder_retro",
        "title": "晚间训练记录",
        "categories": ["exercise"],
        "retrospective": True,
        "time": {"precision": "unknown", "absolute": None},
        "data": {"exercise": {"exercises": [{"name": "未说明", "sets": [{"set": 1, "weight_kg": 60}]}]}},
        "evidence": {"quote": "我今天练了深蹲", "source_label": "self",
                     "reported_at": "2026-10-25T20:42:26+08:00"},
    }
    card = build_activity_cards([event])["cards"][0]
    assert card["title"] == "晚间训练记录"
    assert "深蹲" not in card["title"]
    assert card["time"]["occurred_at"] is None


def test_integer_sets_keeps_the_count_instead_of_crashing_the_day():
    """模型偶尔把 sets 写成组数（如 3）：不能让当天 /api/daily 崩溃，也不能丢掉组数。"""
    from life_logger.daily_cognitive_session import build_fitness_ledger

    event = {
        "event_id": "e1",
        "title": "做卧推",
        "categories": ["exercise"],
        "data": {"exercise": {"exercises": [{"name": "卧推", "sets": 3}]}},
    }
    ledger = build_fitness_ledger([event])
    exercise = ledger["workouts"][0]["exercises"][0]
    assert exercise["set_count"] == 3
    assert "sets" not in exercise
