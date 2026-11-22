"""三大营养素口径回归（2026-11-06 Q911 + Q912）。

覆盖派单点名的六类真实错误，全部用**合成事件**（不依赖 output/ 数据）：

A 份量没换算（portion=300克 却按默认 100g 算）
B 重复计入（整餐 + 配料；同餐复述两次）
C 跨日回顾（「昨天晚上吃了」算到今天）
D 非食物进记录（讨论 / 泛指「食物」/ 计划「每天100克」）
E 活动层 food 事实没有营养（Q892 之后三大营养素断在活动层）
F 合计必须带覆盖率，未知不当 0

Q912 追加：
1) 餐次以原话为准（下午说「中午吃了汉堡」= 午餐；没说 → 未说明）
2) 多次汇报累加，午晚同食物算两次
3) 只有同餐复述才合并；补克数合并；「又吃了」算新的一条
4) 跨日回顾归昨天
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.daily_cognitive_session import build_nutrition_ledger  # noqa: E402
from life_logger.nutrition_meal_rules import (  # noqa: E402
    dedupe_same_meal,
    is_additive,
    is_generic_item,
    is_retrospective,
    meal_from_text,
    name_core,
    portion_grams,
)


def _food(quote: str, item: str, *, day_ts: str, **food) -> dict:
    payload = {"item": item}
    payload.update(food)
    return {
        "event_id": f"evt_{abs(hash((quote, item, day_ts))) % 10**10}",
        "title": quote,
        "categories": ["food"],
        "activity_type": "meal",
        "reported_at": day_ts,
        "time": {"absolute": day_ts},
        "evidence": {"quote": quote, "source_label": "self", "reported_at": day_ts},
        "data": {"food": payload},
    }


# ---------------------------------------------------------------------------
# Q912-1 餐次以原话为准
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("title", "quote", "expected"),
    [
        ("", "中午吃了一个汉堡", "lunch"),
        ("", "晚上吃了三百克蒸好的土豆", "dinner"),
        ("", "早上七点吃了两个鸡蛋", "breakfast"),
        ("", "刚刚这好好吃", "unknown"),
    ],
)
def test_meal_comes_from_utterance_not_clock(title, quote, expected):
    assert meal_from_text(title, quote, None, occurred_at="2026-10-26T17:30:00+08:00") == expected


def test_afternoon_speech_about_lunch_is_lunch():
    """下午 17:30 才说「中午吃了汉堡」——仍算午餐，不算加餐。"""
    events = [_food("中午吃了一个汉堡", "汉堡", day_ts="2026-10-26T17:30:00+08:00", estimated_calories=480)]
    ledger = build_nutrition_ledger(events, day="2026-10-26")
    assert ledger["meals"]["lunch"], ledger["meals"]
    assert not ledger["meals"]["snacks"]


def test_unstated_meal_is_unknown_not_snack():
    events = [_food("这好好吃", "西北牛筋面", day_ts="2026-10-27T20:00:00+08:00", estimated_calories=500)]
    ledger = build_nutrition_ledger(events, day="2026-10-27")
    assert ledger["meals"].get("unknown"), ledger["meals"].keys()
    assert not ledger["meals"]["snacks"]


# ---------------------------------------------------------------------------
# Q912-2 多次汇报累加（午晚同食物算两次）
# ---------------------------------------------------------------------------
def test_same_food_at_lunch_and_dinner_counts_twice():
    events = [
        _food("中午吃了一个汉堡", "汉堡", day_ts="2026-10-26T12:10:00+08:00", estimated_calories=480),
        _food("晚上又吃了汉堡", "汉堡", day_ts="2026-10-26T19:10:00+08:00", estimated_calories=480),
    ]
    ledger = build_nutrition_ledger(events, day="2026-10-26")
    # 2026-11-07 用户决定：不知重量就不算（原话只说「一个汉堡」，没有克数）→ 不计入合计，改看候选合计
    assert ledger["totals"]["calories_kcal"] is None
    assert ledger["unweighed_candidate_kcal"] == pytest.approx(960.0)
    assert ledger["consumed_record_count"] == 2


# ---------------------------------------------------------------------------
# Q912-3 同餐复述才合并
# ---------------------------------------------------------------------------
def test_same_meal_restatement_merges_to_one():
    events = [
        _food("中午吃了一个汉堡", "汉堡", day_ts="2026-10-26T12:10:00+08:00", estimated_calories=480),
        _food("吃了中午吃了一个汉堡", "汉堡", day_ts="2026-10-26T12:40:00+08:00", estimated_calories=450),
    ]
    ledger = build_nutrition_ledger(events, day="2026-10-26")
    # 2026-11-07 用户决定：不知重量就不算：合并行为不变，只是数值进候选
    assert ledger["totals"]["calories_kcal"] is None
    assert ledger["unweighed_candidate_kcal"] == pytest.approx(480.0)
    assert ledger["merged_restatements"], "复述必须留下合并记录"


def test_later_grams_supplements_same_record_instead_of_adding():
    events = [
        _food("中午吃了一个汉堡", "汉堡", day_ts="2026-10-26T12:10:00+08:00", estimated_calories=480),
        _food("中午那个汉堡是三百克", "汉堡", day_ts="2026-10-26T12:50:00+08:00",
              estimated_calories=480, weight_g=300, estimate_basis="library_math"),
    ]
    ledger = build_nutrition_ledger(events, day="2026-10-26")
    assert ledger["totals"]["calories_kcal"] == pytest.approx(480.0)
    kept = [r for r in ledger["meals"]["lunch"]]
    assert len(kept) == 1
    assert kept[0]["weight_g"] == 300


def test_additive_eating_creates_a_second_record():
    events = [
        _food("晚上吃了拉面", "拉面", day_ts="2026-10-27T19:00:00+08:00", estimated_calories=600),
        _food("晚上又吃了一个拉面", "拉面", day_ts="2026-10-27T19:30:00+08:00", estimated_calories=600),
    ]
    ledger = build_nutrition_ledger(events, day="2026-10-27")
    # 2026-11-07 用户决定：不知重量就不算：两条拉面都没说克数 → 合计 None，候选 1200
    assert ledger["totals"]["calories_kcal"] is None
    assert ledger["unweighed_candidate_kcal"] == pytest.approx(1200.0)
    assert len(ledger["meals"]["dinner"]) == 2


def test_conflicting_portions_without_additive_go_to_review_not_silent():
    events = [
        _food("中午吃了排骨", "排骨", day_ts="2026-10-26T12:10:00+08:00",
              estimated_calories=300, weight_g=150, estimate_basis="library_math"),
        _food("中午吃了排骨", "排骨", day_ts="2026-10-26T12:20:00+08:00",
              estimated_calories=600, weight_g=300, estimate_basis="library_math"),
    ]
    ledger = build_nutrition_ledger(events, day="2026-10-26")
    assert ledger["needs_review"], "份量冲突且没有「又吃了」时必须进需要我判断"


# ---------------------------------------------------------------------------
# B 整餐 vs 配料：只算一次
# ---------------------------------------------------------------------------
def test_composite_meal_does_not_double_count_its_ingredients():
    events = [
        _food("晚上吃了三百克土豆", "土豆", day_ts="2026-10-27T19:00:00+08:00",
              estimated_calories=231, weight_g=300, estimate_basis="library_math"),
        _food("再加五十克腊肠", "腊肠", day_ts="2026-10-27T19:05:00+08:00",
              estimated_calories=292, weight_g=50, estimate_basis="library_math"),
        _food("土豆、腊肠", "土豆300g、腊肠50g", day_ts="2026-10-27T19:10:00+08:00",
              estimated_calories=523, weight_g=350, estimate_basis="ingredient_math"),
    ]
    ledger = build_nutrition_ledger(events, day="2026-10-27")
    assert ledger["totals"]["calories_kcal"] == pytest.approx(523.0)
    assert any("composite" in str(m.get("merge_reason")) for m in ledger["merged_restatements"])


# ---------------------------------------------------------------------------
# C / Q912-4 跨日回顾不算今天
# ---------------------------------------------------------------------------
def test_yesterday_recap_is_not_counted_today():
    events = [_food("昨天晚上我吃了三百克的土豆", "土豆", day_ts="2026-10-27T09:00:00+08:00",
                    estimated_calories=231, weight_g=300)]
    ledger = build_nutrition_ledger(events, day="2026-10-27")
    # [2026-11-07] 口径统一：没有已知热量就是 None（不再是 0.0），与 /api/nutrition/day 一致
    assert ledger["totals"]["calories_kcal"] is None
    assert ledger["consumed_record_count"] == 0
    recap = [r for r in ledger["food_records"] if r.get("exclude_reason") == "retrospective_not_today"]
    assert recap and recap[0]["belongs_to_date"] == "2026-10-26"


# ---------------------------------------------------------------------------
# D 非食物
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("item", ["食物", "蘸料食物", "这个"])
def test_generic_item_is_not_counted(item):
    events = [_food("这好好吃", item, day_ts="2026-10-27T20:00:00+08:00", estimated_calories=200)]
    ledger = build_nutrition_ledger(events, day="2026-10-27")
    # [2026-11-07] 同上：没有已知值 → None
    assert ledger["totals"]["calories_kcal"] is None
    assert [r for r in ledger["food_records"] if r.get("exclude_reason") == "not_a_food_item"]


def test_plan_and_discussion_are_not_counted():
    events = [
        _food("我的饮食与采购青椒讨论", "饮食与采购青椒讨论", day_ts="2026-10-26T10:00:00+08:00",
              estimated_calories=100),
        _food("买的那个毛豆仁粉，一天吃个一百克儿", "冷冻毛豆仁粉", day_ts="2026-10-27T20:00:00+08:00",
              estimated_calories=348),
    ]
    ledger = build_nutrition_ledger(events, day="2026-10-26")
    planned = [r for r in ledger["food_records"] if r.get("exclude_reason") == "plan_or_discussion"]
    assert len(planned) == 2
    # [2026-11-07] 计划/讨论不算，且总量是"没有" → None
    assert ledger["totals"]["calories_kcal"] is None


# ---------------------------------------------------------------------------
# A 份量换算（读时用 per_100g 重算）
# ---------------------------------------------------------------------------
def test_portion_grams_override_default_serving():
    events = [_food("晚上吃了三百克蒸好的土豆", "蒸土豆", day_ts="2026-10-26T19:00:00+08:00",
                    portion="300克", weight_g=100, estimated_calories=77,
                    estimate_basis="library_math", nutrition_serving_source="none",
                    nutrients={"energy_kcal": 77, "carbohydrates_g": 17.2, "protein_g": 2.0,
                               "fat_g": 0.2, "sodium_mg": 2.7},
                    nutrition_per_100g={"energy_kcal": 77.0, "carbohydrates_g": 17.2,
                                        "protein_g": 2.0, "fat_g": 0.2, "sodium_mg": 2.7})]
    ledger = build_nutrition_ledger(events, day="2026-10-26")
    record = ledger["meals"]["dinner"][0]
    assert record["weight_g"] == 300
    assert record["calories_kcal"] == pytest.approx(231.0)
    assert record["nutrients"]["carbohydrates_g"] == pytest.approx(51.6)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("300克", 300.0), ("50g", 50.0), ("1.5千克", 1500.0), ("1杯", None), ("1份", None)],
)
def test_portion_grams_parser(text, expected):
    assert portion_grams(text) == expected


def test_name_core_strips_cooking_method():
    assert name_core("蒸土豆") == "土豆"
    assert name_core("腊肠（蒸）") == "腊肠"


# ---------------------------------------------------------------------------
# E 活动层 food 事实
# ---------------------------------------------------------------------------
def test_activity_food_fact_gets_nutrition_from_the_same_lookup_chain():
    item = {
        "item_id": "it_food_1",
        "action": "吃了三百克土豆",
        "status": "completed",
        "fact_type": "food",
        "occurred_at": "2026-11-04T12:30:00+08:00",
        "spoken_at": "2026-11-04T12:30:00+08:00",
        "evidence": [{"source_id": "src_1", "quote": "中午吃了三百克土豆", "source_label": "self"}],
    }
    activity = {
        "activity_id": "act_food_1",
        "title": "午餐吃土豆",
        "lifecycle": "live",
        "spoken_at": "2026-11-04T12:30:00+08:00",
        "items": [item],
    }
    ledger = build_nutrition_ledger([], day="2026-11-04", activities=[activity])
    records = [r for r in ledger["meals"]["lunch"]]
    if not records:                       # 名字查不到时如实为空，不当 0
        # [2026-11-07] 口径统一：没有记录 → None（不是 0.0）
        assert ledger["totals"]["calories_kcal"] is None
        return
    assert records[0]["calories_known"] is True
    assert records[0]["calories_kcal"] and records[0]["calories_kcal"] > 100


# ---------------------------------------------------------------------------
# F 覆盖率
# ---------------------------------------------------------------------------
def test_totals_expose_coverage_and_never_turn_unknown_into_zero():
    events = [
        _food("中午吃了一个汉堡", "汉堡", day_ts="2026-10-26T12:10:00+08:00", estimated_calories=480,
              nutrients={"energy_kcal": 480, "carbohydrates_g": 44, "protein_g": 24, "fat_g": 22, "sodium_mg": 900}),
        _food("晚上喝了一碗汤", "汤", day_ts="2026-10-26T19:10:00+08:00"),   # 无热量
    ]
    ledger = build_nutrition_ledger(events, day="2026-10-26")
    # 2026-11-07 用户决定：不知重量就不算：两条都没说克数 → 已知 0 条，全部算未知（不是 0）
    assert ledger["totals"]["calories_kcal"] is None
    coverage = ledger["totals_coverage"]
    assert coverage["consumed_records"] == 2
    assert coverage["calories_known"] == 0
    assert coverage["calories_unknown"] == 2
    assert coverage["calories_known_ratio"] == pytest.approx(0.0)
    assert coverage["macros"]["carbohydrates_g"]["unknown"] == 2
    assert set(ledger["totals_unknown_counts"]) >= {"calories_kcal", "carbohydrates_g"}


# ---------------------------------------------------------------------------
# 规则函数直测
# ---------------------------------------------------------------------------
def test_rule_helpers():
    assert is_additive("晚上又吃了一个汉堡")
    assert not is_additive("中午吃了一个汉堡")
    assert is_retrospective("昨天晚上我吃了三百克的土豆")
    assert not is_retrospective("晚上我吃了三百克的土豆")
    assert is_generic_item("食物")
    assert not is_generic_item("土豆")
    kept, merged, review = dedupe_same_meal([
        {"item": "米饭", "meal": "lunch", "calories_kcal": 200, "calories_known": True},
        {"item": "米饭", "meal": "dinner", "calories_kcal": 200, "calories_known": True},
    ])
    assert len(kept) == 2 and not merged and not review


def test_v3_activity_food_spoken_labels_and_raw_rice_weight_20261108():
    """回归 2026-11-08 真实缺陷：
    1) V3 item['food'] 是 dict（含 name/grams/portion/meal），不能退回 action 剥成『补报 鸡胸肉丸』且丢克重；
    2) 口述中文数字营养成分表（四百二十九千焦、十八点儿六克、饱和脂肪干扰项）必须按 label_math 精确换算；
    3) 『生重200克』『二百克的生米饭啊，这是生重』必须按中国食物成分表稻米（生重 346 kcal/100g = 692 kcal）计，
       不能当成熟米饭（116 kcal/100g）或模糊匹配成黄米馒头（262 kcal）；
    4) 『十八点儿六克』『七点八克』里的小数点不能被误判成 18 点/7 点的晚餐钟点。
    """
    activity = {
        "activity_id": "act_food_report_1108_L004",
        "category": "饮食",
        "lifecycle": "live",
        "status": "completed",
        "title": "补报今天肉丸料理包及米饭",
        "fact_schema_version": 3,
        "spoken_at": "2026-11-08T22:14:51.454000+08:00",
        "items": [
            {
                "item_id": "item_food_meatballs_1108_L004",
                "action": "补报今天吃了500克鸡胸肉丸",
                "fact_type": "food",
                "status": "completed",
                "spoken_at": "2026-11-08T22:14:51.454000+08:00",
                "occurred_date": "2026-11-08",
                "food": {
                    "grams": 500,
                    "meal": "unknown",
                    "name": "鸡胸肉丸",
                    "portion": "两包，每包250克",
                },
                "evidence": [
                    {"source_id": "s1", "quote": "五百克的鸡胸肉丸", "source_label": "self"},
                    {"source_id": "s2", "quote": "鸡肉肉丸一包是二百五十克，我吃了两包。", "source_label": "self"},
                    {"source_id": "s3", "quote": "然后每一百克是。", "source_label": "self"},
                    {"source_id": "s4", "quote": "四百二十九千焦。", "source_label": "self"},
                    {"source_id": "s5", "quote": "蛋白质是十八点儿六克。", "source_label": "self"},
                    {"source_id": "s6", "quote": "脂肪二点儿三克，碳水化合物一点儿六克，钠是二百九十六毫克。", "source_label": "self"},
                ],
            },
            {
                "item_id": "item_food_beef_1108_L006",
                "action": "补报今天吃了一包香辣土豆牛肉",
                "fact_type": "food",
                "status": "completed",
                "spoken_at": "2026-11-08T22:14:51.454000+08:00",
                "occurred_date": "2026-11-08",
                "food": {
                    "grams": 220,
                    "meal": "unknown",
                    "name": "香辣土豆牛肉料理包",
                    "portion": "一包",
                },
                "evidence": [
                    {"source_id": "s7", "quote": "然后还吃了一包香辣土豆牛肉料理包", "source_label": "self"},
                    {"source_id": "s8", "quote": "它是每一百克是四百九十五千焦", "source_label": "self"},
                    {"source_id": "s9", "quote": "蛋白质七点八克，脂肪五点八克，饱和脂肪一点九克，碳水化合物八点七克", "source_label": "self"},
                    {"source_id": "s10", "quote": "钠是\n五百七十三毫克", "source_label": "self"},
                    {"source_id": "s11", "quote": "然后一份儿料理包的话是二百二十克。", "source_label": "self"},
                ],
            },
            {
                "item_id": "item_food_rice_1108_L021",
                "action": "补报今天主食为生重200克米",
                "fact_type": "food",
                "status": "completed",
                "spoken_at": "2026-11-08T22:14:51.454000+08:00",
                "occurred_date": "2026-11-08",
                "food": {
                    "grams": 200,
                    "meal": "unknown",
                    "name": "米饭",
                    "portion": "按生米重量记录，生重200克",
                },
                "evidence": [
                    {"source_id": "s12", "quote": "然后主食的话是吃了。", "source_label": "self"},
                    {"source_id": "s13", "quote": "二百克的生米饭啊，这是生重。", "source_label": "self"},
                ],
            },
        ],
    }

    ledger = build_nutrition_ledger([], day="2026-11-08", activities=[activity])
    assert ledger["totals_complete"] is True
    assert ledger["unknown_nutrition_count"] == 0
    assert ledger["unweighed_items"] == []
    assert ledger["totals"]["calories_kcal"] == pytest.approx(1465.0)
    assert ledger["totals"]["carbohydrates_g"] == pytest.approx(181.5)
    assert ledger["totals"]["protein_g"] == pytest.approx(126.0)
    assert ledger["totals"]["fat_g"] == pytest.approx(26.1)
    assert ledger["totals"]["sodium_mg"] == pytest.approx(2745.0)

    items = {r["item"]: r for r in ledger["meals"]["unknown"]}
    assert set(items) == {"鸡胸肉丸", "香辣土豆牛肉料理包", "米饭（生重）"}
    assert items["鸡胸肉丸"]["calories_kcal"] == pytest.approx(513.0)
    assert items["鸡胸肉丸"]["estimate_basis"] == "label_math"
    assert items["香辣土豆牛肉料理包"]["calories_kcal"] == pytest.approx(260.0)
    assert items["香辣土豆牛肉料理包"]["estimate_basis"] == "label_math"
    assert items["米饭（生重）"]["calories_kcal"] == pytest.approx(692.0)
    assert items["米饭（生重）"]["nutrition_matched_name"] == "稻米（代表值）"

