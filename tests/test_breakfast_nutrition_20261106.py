"""2026-11-06：活动层的「400 克水饺」丢了克数、且复合菜名被单一原料抢走。

两条都要在**转换/查库**层面修好，不改历史产物、不改模型提示词。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from life_logger.day_facts import (  # noqa: E402
    _food_payload,
    _weight_grams_from_item,
    domain_events,
    food_name_from_action,
)
from life_logger.food_lookup import dish_guard_allows, lookup_food  # noqa: E402

DAY = "2026-11-06"


def _activity(action: str, **item_extra) -> dict:
    item = {
        "item_id": "it-01",
        "action": action,
        "status": "completed",
        "fact_type": "food",
        "spoken_at": f"{DAY}T08:16:00+08:00",
        "occurred_at": None,
        "amount": 400,
        "unit": "克",
        "evidence": [{"source_id": "src_a", "segment_id": "watch_000000_000002",
                      "quote": "今天早上吃了400克三全猪肉芹菜速冻水饺", "source_label": "self"}],
    }
    item.update(item_extra)
    return {"activity_id": "act-20261106-bf-01", "items": [item]}


# ---------------------------------------------------------------- A 克数 + 名字

def test_weight_comes_from_structured_fields_only() -> None:
    assert _weight_grams_from_item({"amount": 400, "unit": "克"}) == 400.0
    assert _weight_grams_from_item({"amount": 400, "unit": "克", "amount_kind": "weight"}) == 400.0
    # 含糊计数不是重量
    assert _weight_grams_from_item({"amount": 1, "unit": "份"}) is None
    assert _weight_grams_from_item({"amount": 2, "unit": "个", "amount_kind": "quantity"}) is None
    # 其他重量单位
    assert _weight_grams_from_item({"amount": 0.4, "unit": "kg"}) == 400.0


def test_food_name_strips_time_verb_and_amount() -> None:
    assert food_name_from_action("今天早上吃了400克三全猪肉芹菜速冻水饺") == "三全猪肉芹菜速冻水饺"
    assert food_name_from_action("中午吃了一份炸鸡") == "炸鸡"
    # 剥不干净就不硬凑
    assert food_name_from_action("吃了") == ""


def test_domain_events_food_carries_weight_and_clean_name() -> None:
    """回归：之前是 item=整句 action、且完全没有 weight_g。"""
    events = domain_events([], [_activity("今天早上吃了400克三全猪肉芹菜速冻水饺")], DAY, "food")
    food = [e for e in events if (e.get("data") or {}).get("food")]
    assert len(food) == 1
    payload = food[0]["data"]["food"]
    assert payload["weight_g"] == 400.0
    assert payload["portion"] == "400克"
    assert payload["item"] == "三全猪肉芹菜速冻水饺"
    assert "吃了" not in payload["item"]


def test_food_payload_prefers_structured_name() -> None:
    payload = _food_payload({"amount": 400, "unit": "克", "food_name": "猪肉芹菜水饺"}, "随便说了句")
    assert payload["item"] == "猪肉芹菜水饺"
    assert payload["weight_g"] == 400.0


# ---------------------------------------------------------------- B 复合菜名

def test_compound_dish_never_falls_back_to_single_ingredient() -> None:
    """回归：之前「猪肉芹菜饺子」错配到「猪肉（清蒸）」（198 kcal/100g、碳水 0）。"""
    result = lookup_food("猪肉芹菜饺子")
    assert result is None, f"不应把一道菜算成一块肉，实际命中 {result and result.name}"


@pytest.mark.parametrize("query", ["水饺", "饺子", "速冻水饺"])
def test_dumpling_names_do_not_steal_meat(query: str) -> None:
    result = lookup_food(query)
    if result is not None:
        assert dish_guard_allows(query, result.name), f"{query} 命中了非饺子条目 {result.name}"


def test_dish_guard_keeps_same_family() -> None:
    assert dish_guard_allows("猪肉芹菜饺子", "水饺（猪肉馅）")
    assert not dish_guard_allows("猪肉芹菜饺子", "猪肉（清蒸）")
    # 非复合菜名不受限制
    assert dish_guard_allows("苹果", "苹果")
    assert dish_guard_allows("猪肉", "猪肉（清蒸）")
