"""「不知重量就不算」第二阶段：无重量不补热量、候选保留、荒谬营养与配错条目不采用。"""
from __future__ import annotations

import pytest

from life_logger.daily_cognitive_session import build_nutrition_ledger
from life_logger.food_lookup import FoodNutrition
from life_logger.food_nutrition import contains_core_mismatch, nutrition_implausible

DAY = "2026-11-06"


def event(quote, item, *, food=None, label="self", title=None, event_id="e1"):
    return {
        "event_id": event_id, "title": title or quote[:20], "categories": ["food"],
        "activity_type": "meal",
        "evidence": {"quote": quote, "source_id": "s1", "source_label": label},
        "data": {"food": food or {"item": item}},
    }


class FakeLookup:
    def __init__(self, result=None):
        self.result = result or FoodNutrition(
            query="x", name="x", matched_name="x",
            per_100g={"energy_kcal": 100.0, "protein_g": 3.0, "fat_g": 1.0,
                      "carbohydrates_g": 20.0, "sodium_mg": 100.0},
            source="brand", confidence="high")

    def lookup(self, name, grams=None, *, allow_off=True):
        return self.result


def test_no_weight_no_estimate_but_candidate_kept():
    ledger = build_nutrition_ledger(
        [event("米饭因为最近这些年涨价", "米饭",
               food={"item": "米饭", "estimated_calories": 230,
                     "nutrients": {"energy_kcal": 230, "protein_g": 4.0, "fat_g": 0.5,
                                   "carbohydrates_g": 50.0, "sodium_mg": 2.0}})],
        day=DAY, nutrition_lookup=FakeLookup(),
    )
    rec = ledger["food_records"][0]
    assert rec["calories_kcal"] is None and rec["calories_known"] is False
    assert rec["estimate_basis"] == "no_portion_spoken"
    assert rec["portion_estimate_candidate"]["calories_kcal"] is not None
    assert ledger["unweighed_candidate_kcal"] > 0


def test_count_only_two_buns_is_candidate_not_total():
    ledger = build_nutrition_ledger(
        [event("中午吃了两个馒头", "馒头",
               food={"item": "馒头", "estimated_calories": 230,
                     "nutrients": {"energy_kcal": 230, "protein_g": 4.0, "fat_g": 0.5,
                                   "carbohydrates_g": 50.0, "sodium_mg": 2.0}})],
        day=DAY, nutrition_lookup=FakeLookup(),
    )
    assert ledger["consumed_record_count"] == 1
    # 「没有已知值」= None（不是 0）：Q96x 决议，0 会被前端当成「确实吃了 0 千卡」
    assert ledger["totals"]["calories_kcal"] is None
    assert ledger["food_records"][0]["estimate_basis"] == "no_weight_spoken"
    assert ledger["food_records"][0]["portion_estimate_candidate"]["basis"] in {"count_unit", "standard_portion"}


def test_spoken_400g_keeps_calculation():
    ledger = build_nutrition_ledger(
        [event("吃了四百克三全猪肉芹菜速冻水饺", "三全猪肉芹菜速冻水饺",
               food={"item": "三全猪肉芹菜速冻水饺", "weight_g": 400,
                     "estimated_calories": 940,
                     "nutrients": {"energy_kcal": 940, "protein_g": 32.0, "fat_g": 28.0,
                                   "carbohydrates_g": 132.0, "sodium_mg": 1800.0}})],
        day=DAY, nutrition_lookup=FakeLookup(),
    )
    rec = ledger["food_records"][0]
    assert rec["calories_known"] is True and rec["weight_g"] == 400
    assert ledger["totals"]["calories_kcal"] > 0


def test_contains_mismatch_is_not_used():
    bad = FoodNutrition(query="鱼骨头汤", name="无印良品 油菜和笋的和风汤",
                        matched_name="无印良品 油菜和笋的和风汤",
                        per_100g={"energy_kcal": 278.0, "protein_g": 2.0, "fat_g": 8.0,
                                  "carbohydrates_g": 48.0, "sodium_mg": 10140.0},
                        source="boohee", confidence="medium", match_kind="contains")
    ledger = build_nutrition_ledger(
        [event("鱼骨头和鸡骨头熬的汤", "鱼骨头和鸡骨头熬的汤",
               food={"item": "鱼骨头和鸡骨头熬的汤", "estimated_calories": 120,
                     "nutrients": {"energy_kcal": 120, "protein_g": 5.0, "fat_g": 2.0,
                                   "carbohydrates_g": 3.0, "sodium_mg": 400.0},
                     "nutrition_match_kind": "contains",
                     "nutrition_matched_name": bad.matched_name,
                     "nutrition_per_100g": dict(bad.per_100g)})],
        day=DAY, nutrition_lookup=FakeLookup(bad),
    )
    rec = ledger["food_records"][0]
    assert rec["calories_known"] is False
    assert rec["estimate_basis"] in {"contains_mismatch", "implausible_nutrition"}


def test_implausible_per_100g_is_rejected():
    bad = FoodNutrition(query="汤", name="汤", matched_name="汤",
                        per_100g={"energy_kcal": 278.0, "protein_g": 2.0, "fat_g": 8.0,
                                  "carbohydrates_g": 48.0, "sodium_mg": 10140.0},
                        source="boohee", confidence="high", match_kind="exact")
    ledger = build_nutrition_ledger(
        [event("喝了三百克汤", "汤",
               food={"item": "汤", "weight_g": 300, "estimated_calories": 834,
                     "nutrients": {"energy_kcal": 834, "protein_g": 6.0, "fat_g": 24.0,
                                   "carbohydrates_g": 144.0, "sodium_mg": 30420},
                     "nutrition_per_100g": dict(bad.per_100g)})],
        day=DAY, nutrition_lookup=FakeLookup(bad),
    )
    rec = ledger["food_records"][0]
    assert rec["calories_known"] is False
    assert rec["estimate_basis"] == "implausible_nutrition"


class _LookupWithPer100(FakeLookup):
    """让 _calibrate_activity_food 走一次真实查库（带 per_100g），再验证读时闸门。"""


def test_ledger_exposes_unweighed_items_and_candidate_kcal():
    ledger = build_nutrition_ledger(
        [event("米饭因为最近这些年涨价", "米饭",
               food={"item": "米饭", "estimated_calories": 230,
                     "nutrients": {"energy_kcal": 230, "protein_g": 4.0, "fat_g": 0.5,
                                   "carbohydrates_g": 50.0, "sodium_mg": 2.0}})],
        day=DAY, nutrition_lookup=FakeLookup(),
    )
    assert ledger["unweighed_candidate_kcal"] > 0
    assert len(ledger["unweighed_items"]) == 1
    row = ledger["unweighed_items"][0]
    assert row["item"] == "米饭"
    assert row["estimate_basis"] == "no_portion_spoken"
    assert set(row) >= {"event_id", "item", "quote", "meal", "spoken_at",
                        "estimate_basis", "portion_estimate_candidate"}


def test_thresholds_are_module_level_constants():
    import life_logger.food_nutrition as fn
    assert fn.MAX_SODIUM_MG_PER_100G == 20000.0
    assert fn.MAX_KCAL_PER_100G == 900.0
    assert fn.MAX_KCAL_FAT_GUARD == 90.0
    assert fn.MAX_MACRO_SUM_G_PER_100G == 105.0
    assert fn.SOUP_SHAPE_MAX_SODIUM_MG == 3000.0
    assert fn.ATWATER_MIN_KCAL_PER_100G == 50.0
    assert fn.ATWATER_MIN_RATIO == 0.5


@pytest.mark.parametrize("per_100g,expected_reason", [
    ({"sodium_mg": 30000, "energy_kcal": 100, "protein_g": 1, "fat_g": 0, "carbohydrates_g": 1},
     "sodium_over_absolute_cap"),
    ({"sodium_mg": 10, "energy_kcal": 950, "protein_g": 0, "fat_g": 5, "carbohydrates_g": 0},
     "energy_over_cap_without_fat"),
    ({"sodium_mg": 10, "energy_kcal": 900, "protein_g": 50, "fat_g": 50, "carbohydrates_g": 50},
     "macro_sum_over_cap"),
    ({"sodium_mg": 5000, "energy_kcal": 100, "protein_g": 2, "fat_g": 1, "carbohydrates_g": 10}, "soup_shape_with_high_sodium"),
    ({"sodium_mg": 10, "energy_kcal": 1, "protein_g": 1, "fat_g": 1, "carbohydrates_g": 78},
     "energy_below_atwater"),
])
def test_implausible_reasons(per_100g, expected_reason):
    """形态层只有名字含汤/汁/羹时命中，所以这条要带名字另测。"""
    name = "汤" if expected_reason == "soup_shape_with_high_sodium" else "白米饭"
    assert nutrition_implausible(per_100g, name) == expected_reason


def test_legit_high_sodium_and_oil_pass():
    """盐、酱油、纯油脂是真条目，绝不能被门槛误杀。"""
    assert nutrition_implausible({"sodium_mg": 39000, "energy_kcal": 0, "protein_g": 0,
                                  "fat_g": 0, "carbohydrates_g": 0}, "精盐") == "sodium_over_absolute_cap"
    assert nutrition_implausible({"sodium_mg": 6000, "energy_kcal": 60, "protein_g": 5,
                                  "fat_g": 0.1, "carbohydrates_g": 10}, "酱油") is None
    assert nutrition_implausible({"sodium_mg": 0, "energy_kcal": 899, "protein_g": 0,
                                  "fat_g": 99.9, "carbohydrates_g": 0}, "花生油") is None


def test_zero_calorie_sweetener_exempt_from_atwater():
    assert nutrition_implausible({"sodium_mg": 0, "energy_kcal": 0, "protein_g": 0,
                                  "fat_g": 0, "carbohydrates_g": 100}, "stevia") is None


def test_contains_core_mismatch_rules():
    assert contains_core_mismatch("鱼骨头汤", "无印良品 油菜和笋的和风汤") is True
    assert contains_core_mismatch("牛肉面", "牛肉（代表值）") is False


def _user_adopted(item="猪肉芹菜水饺", *, kcal=400.0, grams=200.0, source_event_id="ev-orig"):
    return {"item": item, "estimated_calories": kcal, "weight_g": grams,
            "estimate_basis": "user_adopted_candidate", "nutrition_source": "user_judgment",
            "source_event_id": source_event_id,
            "nutrients": {"energy_kcal": kcal, "protein_g": 20.0, "fat_g": 10.0,
                          "carbohydrates_g": 50.0, "sodium_mg": 900.0}}


def test_user_adopted_candidate_counts_without_weight_in_text():
    """用户采纳的候选热量：原话没重量也要计入（不是猜的，是用户给的）。"""
    ledger = build_nutrition_ledger(
        [event("吃了猪肉芹菜水饺", "猪肉芹菜水饺", event_id="ev-orig",
               food={"item": "猪肉芹菜水饺"}),
         event("猪肉芹菜水饺 200 克 400 千卡", "猪肉芹菜水饺", event_id="manual_cal_1",
               food=_user_adopted())],
        day=DAY, nutrition_lookup=FakeLookup(),
    )
    assert ledger["totals"]["calories_kcal"] == 400.0
    assert ledger["unknown_nutrition_count"] == 0
    assert ledger["consumed_record_count"] == 1
    assert ledger["unweighed_items"] == []
    assert ledger["unweighed_candidate_kcal"] == 0


def test_user_adopted_original_record_not_listed_again():
    """被采纳的原条目不再出现在 unweighed_items，也不重复计入。"""
    ledger = build_nutrition_ledger(
        [event("吃了猪肉芹菜水饺", "猪肉芹菜水饺", event_id="ev-orig",
               food={"item": "猪肉芹菜水饺", "estimated_calories": 300,
                     "nutrients": {"energy_kcal": 300, "protein_g": 10.0, "fat_g": 8.0,
                                   "carbohydrates_g": 40.0, "sodium_mg": 600.0}}),
         event("水饺 200 克", "猪肉芹菜水饺", event_id="manual_cal_1", food=_user_adopted())],
        day=DAY, nutrition_lookup=FakeLookup(),
    )
    ids = {row["event_id"] for row in ledger["unweighed_items"]}
    assert "ev-orig" not in ids
    assert ledger["totals"]["calories_kcal"] == 400.0          # 只有采纳那一条
    records = {r["event_id"]: r for r in ledger["food_records"]}
    assert records["ev-orig"]["is_consumed"] is False
    assert records["ev-orig"]["record_type"] == "superseded_by_user_adoption"


def test_known_zero_calorie_drink_stays_zero():
    """已知 0 千卡的饮品（水/茶/黑咖啡）：0 与份量无关，不许被抹成 None。"""
    ledger = build_nutrition_ledger(
        [event("喝了一杯白水", "白水", event_id="evt_zero",
               food={"item": "白水", "estimated_calories": 0,
                     "nutrients": {"energy_kcal": 0, "protein_g": 0, "fat_g": 0,
                                   "carbohydrates_g": 0, "sodium_mg": 0}})],
        day=DAY, nutrition_lookup=FakeLookup(),
    )
    rec = ledger["food_records"][0]
    assert rec["calories_kcal"] == 0.0 and rec["calories_known"] is True
    assert ledger["totals"]["calories_kcal"] == 0.0
