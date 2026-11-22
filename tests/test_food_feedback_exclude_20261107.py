"""Diet must not count complaints to the assistant, nor sentence-like food names."""
from __future__ import annotations

from life_logger.daily_cognitive_session import build_nutrition_ledger
from life_logger.food_lookup import FoodNutrition
from life_logger.nutrition_meal_rules import is_feedback_to_assistant, is_implausible_food_name

DAY = "2026-11-06"


def activity(item_name: str, quote: str, *, item_id: str = "i1"):
    return {
        "activity_id": "act-food", "title": quote[:30], "lifecycle": "live",
        "occurrence_start": f"{DAY}T12:00:00+08:00",
        "reported_at": f"{DAY}T12:00:00+08:00",
        "items": [{
            "item_id": item_id, "action": quote, "food_name": item_name,
            "fact_type": "food", "status": "completed",
            "spoken_at": f"{DAY}T12:00:00+08:00",
            "evidence": [{"source_id": "s1", "quote": quote, "source_label": "self"}],
        }],
    }


class RaisingLookup:
    def lookup(self, *args, **kwargs):
        raise AssertionError("excluded record must not trigger a food lookup")


class FakeLookup:
    def __init__(self):
        self.calls = 0

    def lookup(self, name, grams=None, *, allow_off=True):
        self.calls += 1
        return FoodNutrition(query=name, name=name, matched_name=name,
                             per_100g={"energy_kcal": 100.0, "protein_g": 1.0},
                             source="brand", confidence="high")


def test_feedback_to_assistant_classifier_requires_record_pointer():
    assert is_feedback_to_assistant("就没有看到我下午吃了一个晚餐的。")
    assert is_feedback_to_assistant("陈述自己 但记录中未显示")
    assert is_feedback_to_assistant("你怎么没记上我中午吃的面？")
    # 真实吃饭：只有“没看到价格”，没有记录/显示/记上，不能误杀
    assert not is_feedback_to_assistant("我吃了没看到价格的面")


def test_implausible_food_name_rejects_sentences_but_keeps_real_dishes():
    assert is_implausible_food_name("就没有看到我下午吃了一个晚餐的")
    assert is_implausible_food_name("陈述自己 但记录中未显示")
    for name in ("三全猪肉芹菜速冻水饺", "米饭", "馒头", "鱼骨头和鸡骨头熬的汤"):
        assert not is_implausible_food_name(name), name


def test_real_fake_dinner_entries_are_excluded_without_lookup():
    ledger = build_nutrition_ledger(
        [], activities=[
            activity("就没有看到我下午吃了一个晚餐的", "就没有看到我下午吃了一个晚餐的。", item_id="i1"),
            activity("陈述自己 但记录中未显示", "陈述自己 但记录中未显示", item_id="i2"),
        ], day=DAY, nutrition_lookup=RaisingLookup(),
    )
    reasons = {r["exclude_reason"] for r in ledger["food_records"]}
    assert "feedback_to_assistant" in reasons
    assert ledger["consumed_record_count"] == 0
    assert (ledger["totals"].get("calories_kcal") or 0) == 0


def test_real_meals_are_kept_and_counted():
    lookup = FakeLookup()
    ledger = build_nutrition_ledger(
        [], activities=[
            activity("三全猪肉芹菜速冻水饺", "今天早上吃了400克三全猪肉芹菜速冻水饺", item_id="i1"),
            activity("米饭", "晚上吃了一碗米饭", item_id="i2"),
            activity("馒头", "中午吃了两个馒头", item_id="i3"),
        ], day=DAY, nutrition_lookup=lookup,
    )
    assert ledger["consumed_record_count"] == 3
    assert ledger["totals"]["calories_kcal"] > 0
    assert all(r.get("exclude_reason") is None for r in ledger["food_records"])


def test_excluded_records_do_not_enter_totals_even_next_to_real_food():
    ledger = build_nutrition_ledger(
        [], activities=[
            activity("米饭", "晚上吃了一碗米饭", item_id="i1"),
            activity("就没有看到我下午吃了一个晚餐的", "就没有看到我下午吃了一个晚餐的。", item_id="i2"),
        ], day=DAY, nutrition_lookup=FakeLookup(),
    )
    assert ledger["consumed_record_count"] == 1
    # 2026-11-07 用户决定：不知重量就不算（「晚上吃了一碗米饭」只有碗数没有克数）
    # → 合计不再计热，改为核对「只留下米饭这一条候选」
    assert ledger["totals"]["calories_kcal"] is None
    assert ledger["unweighed_candidate_kcal"] > 0
    assert [item["item"] for item in ledger["unweighed_items"]] == ["米饭"]
