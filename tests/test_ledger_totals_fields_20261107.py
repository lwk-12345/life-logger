"""唯一事实来源：后端给出「按餐合计」与「健身组数合计」。"""
from __future__ import annotations

import pytest

from life_logger.daily_cognitive_session import build_fitness_ledger, build_nutrition_ledger

DAY = "2026-11-05"


def food(quote, item, *, meal="lunch", event_id=None, calories=None, nutrients=None, **extra):
    payload = {"item": item, "meal": meal}
    if calories is not None:
        payload["estimated_calories"] = calories
    if nutrients is not None:
        payload["nutrients"] = nutrients
    payload.update(extra)
    return {"event_id": event_id or f"ev_{item}_{quote}", "title": quote, "categories": ["food"],
            "activity_type": "meal", "time": {"absolute": f"{DAY}T12:00:00+08:00"},
            "evidence": {"quote": quote, "source_label": "self"},
            "data": {"food": payload}}


NUTRIENTS = {"energy_kcal": 200, "carbohydrates_g": 30.0, "protein_g": 10.0,
             "fat_g": 5.0, "sodium_mg": 300.0}


def test_meal_totals_sum_equals_day_totals():
    events = [
        food("中午吃了一百克米饭", "米饭", meal="lunch", calories=200,
             nutrients=NUTRIENTS, weight_g=100),
        food("晚上吃了二百克鸡胸肉", "鸡胸肉", meal="dinner", calories=300,
             nutrients={"energy_kcal": 300, "carbohydrates_g": 0.0, "protein_g": 60.0,
                        "fat_g": 6.0, "sodium_mg": 150.0}, weight_g=200),
        food("早上喝了二百毫升牛奶", "牛奶", meal="breakfast", calories=130,
             nutrients={"energy_kcal": 130, "carbohydrates_g": 10.0, "protein_g": 6.0,
                        "fat_g": 7.0, "sodium_mg": 100.0}, weight_g=200),
    ]
    ledger = build_nutrition_ledger(events, day=DAY)
    totals = ledger["totals"]
    meal_totals = ledger["meal_totals"]
    for key in ("calories_kcal", "carbohydrates_g", "protein_g", "fat_g", "sodium_mg"):
        bucket_sum = sum(value[key] for value in meal_totals.values() if value[key] is not None)
        known = sum(value["known_counts"][key] for value in meal_totals.values())
        if totals[key] is None:
            assert known == 0, key
        else:
            assert bucket_sum == pytest.approx(totals[key]), key
    assert totals["calories_kcal"] == pytest.approx(630.0)
    assert meal_totals["lunch"]["calories_kcal"] == pytest.approx(200.0)
    assert meal_totals["dinner"]["calories_kcal"] == pytest.approx(300.0)


def test_meal_totals_unknown_is_not_zero():
    """某项全部未知 → None（不是 0）；未知条数记在 unknown_counts。"""
    events = [
        food("中午吃了一百克米饭", "米饭", meal="lunch", calories=None),
        food("晚上吃了二百克鸡胸肉", "鸡胸肉", meal="dinner", calories=None,
             nutrients={"protein_g": 60.0}),
    ]
    ledger = build_nutrition_ledger(events, day=DAY)
    lunch = ledger["meal_totals"]["lunch"]
    assert lunch["calories_kcal"] is None
    assert lunch["unknown_counts"]["calories_kcal"] == 1
    assert lunch["known_counts"]["calories_kcal"] == 0
    dinner = ledger["meal_totals"]["dinner"]
    assert dinner["protein_g"] == pytest.approx(60.0)
    assert dinner["carbohydrates_g"] is None          # 未知，不是 0
    assert ledger["totals"]["carbohydrates_g"] is None


def workout(event_id, at, title, exercises):
    return {"event_id": event_id, "title": title, "quote": title,
            "spoken_at": f"{DAY}T{at}:00+08:00", "data": {"exercise": {"exercises": exercises}},
            "categories": ["exercise"]}


def test_workout_aggregates_match_detail():
    events = [
        workout("w1", "18:33", "今天练胸", [
            {"name": "卧推", "sets": [{"set": 1, "reps": 10, "weight_kg": 60},
                                      {"set": 2, "reps": 8, "weight_kg": 65}]},
            {"name": "哑铃推肩", "sets": 3},
        ]),
    ]
    ledger = build_fitness_ledger(events, day=DAY)
    entry = ledger["workouts"][0]
    assert entry["set_count"] == 5                     # 2 组明细 + 3 组（无明细）
    assert entry["rep_total"] == pytest.approx(18.0)   # 只有明细里的 10+8
    assert entry["volume_kg"] is None                  # 有缺重量/次数的组
    assert entry["volume_partial"] is True
    totals = ledger["totals"]
    assert totals["workout_count"] == 1
    assert totals["set_count"] == sum(w["set_count"] for w in ledger["workouts"])
    assert totals["rep_total"] == pytest.approx(18.0)


def test_volume_kg_when_all_sets_complete():
    events = [workout("w2", "19:00", "深蹲", [
        {"name": "深蹲", "sets": [{"set": 1, "reps": 5, "weight_kg": 100},
                                  {"set": 2, "reps": 5, "weight_kg": 100}]},
    ])]
    ledger = build_fitness_ledger(events, day=DAY)
    entry = ledger["workouts"][0]
    assert entry["volume_kg"] == pytest.approx(1000.0)
    assert entry["volume_partial"] is False
    assert entry["rep_total"] == pytest.approx(10.0)


def test_real_1105_review_duration_not_double_counted():
    """11-05 真实健身回顾（有氧 5400s）：合计只算一次，不与直播条目重复。"""
    events = [
        workout("live", "18:33", "今天练胸", [{"name": "卧推", "sets": [{"set": 1, "reps": 10, "weight_kg": 60}]}]),
        workout("review", "23:04", "回顾称做了一个半小时有氧",
                [{"name": "有氧", "duration_seconds": 5400}]),
    ]
    ledger = build_fitness_ledger(events, day=DAY)
    assert ledger["totals"]["workout_count"] == 2
    assert ledger["totals"]["duration_seconds"] == pytest.approx(5400.0)
    assert ledger["totals"]["set_count"] == 1
    assert ledger["totals"]["rep_total"] == pytest.approx(10.0)


def _session_workout(event_id, at, title, duration_seconds):
    """带 session 坐标的活动层健身条目（同一场训练里的多条口述）。"""
    return {"event_id": event_id, "title": title, "quote": title, "categories": ["exercise"],
            "_activity_source": {"spoken_at": f"{DAY}T{at}:00+08:00", "fact_type": "exercise"},
            "data": {"exercise": {"exercises": [{"name": "有氧", "duration_seconds": duration_seconds}]}}}


def test_totals_session_count_matches_sessions():
    ledger = build_fitness_ledger([
        _session_workout("w1", "18:33", "去健身房做了一个半小时", 5400),
        _session_workout("w2", "19:40", "爬坡慢走半小时", 1800),
    ], day=DAY)
    assert ledger["totals"]["session_count"] == len(ledger["sessions"]) == 1
    # 兼容：顶层 workout_count 仍是会话数，totals.workout_count 仍是原始条目数
    assert ledger["workout_count"] == 1
    assert ledger["totals"]["workout_count"] == 2


def test_totals_duration_uses_session_span_not_repeated_sum():
    """同一会话里「一个半小时」「半小时」重复口述：不能加成 7200 秒。"""
    ledger = build_fitness_ledger([
        _session_workout("w1", "18:33", "去健身房做了一个半小时", 5400),
        _session_workout("w2", "19:40", "爬坡慢走半小时", 1800),
    ], day=DAY)
    total = ledger["totals"]["duration_seconds"]
    assert total == pytest.approx(ledger["sessions"][0]["total_duration_seconds"])
    assert total == pytest.approx(4020.0)          # 18:33 → 19:40 的会话跨度
    assert total != pytest.approx(7200.0)          # 不是逐条累加
    assert ledger["totals"]["duration_basis"] == "session_span"


def test_totals_duration_falls_back_to_workout_sum():
    """sessions 没有 total_duration_seconds 时，回退到 workouts 逐条时长之和。"""
    ledger = build_fitness_ledger([
        _session_workout("w1", "18:33", "有氧", 1800),
        _session_workout("w2", "19:40", "爬坡", 600),
    ], day=DAY, sessions=[{"session_id": "s1", "actions": []}])   # 无跨度字段
    assert ledger["totals"]["duration_seconds"] == pytest.approx(2400.0)
    assert ledger["totals"]["duration_basis"] == "workout_sum"
