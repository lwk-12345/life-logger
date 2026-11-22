"""吃的是熟的：标准份（一碗 300g）不能配生面/干面的成分。"""
from __future__ import annotations

import pytest

from life_logger import food_lookup
from life_logger.food_nutrition import lookup_food_nutrition

COOKED = "面条（富强粉，煮）"


def _hit(name):
    return lookup_food_nutrition(name)


@pytest.mark.parametrize("name", ["面条", "汤面", "拉面", "乌冬"])
def test_cooked_noodle_maps_to_cooked_entry(name):
    hit = _hit(name)
    assert hit is not None
    assert hit.matched_name == COOKED, hit.matched_name
    assert (hit.per_100g or {}).get("energy_kcal") == pytest.approx(107.0, abs=0.5)


@pytest.mark.parametrize("name", ["干面", "挂面", "生面", "买了面条", "买了一包面条", "面粉"])
def test_raw_purchase_context_is_not_converted_to_cooked(name):
    """生料/购买语境不能被熟制别名换掉。"""
    hit = _hit(name)
    if hit is None:                      # 库里没有「干面/生面」条目：返回空也算没有误改
        return
    assert hit.matched_name != COOKED, f"{name} 被错改成 {COOKED}"
    assert "生" in hit.matched_name or "挂面" in hit.matched_name or "面粉" in hit.matched_name


def test_raw_context_helper():
    assert food_lookup._raw_context("买了一包面条") is True
    assert food_lookup._raw_context("挂面") is True
    assert food_lookup._raw_context("面条") is False
    assert food_lookup._raw_context("汤面") is False


def test_uncooked_entries_without_cooked_counterpart_stay_unchanged():
    """河粉/米线没有熟制条目 → 保持现状，不编数值。"""
    assert "河粉" not in food_lookup._ALIASES
    assert "米线" not in food_lookup._ALIASES
    hit = _hit("河粉")
    assert hit is not None and hit.matched_name == "河粉"


def test_alias_target_exists_in_cfct():
    """别名必须指向成分表里真实存在的条目（不新增数据）。"""
    lib = food_lookup.default_food_lookup()
    matched = lib.cfct.match(COOKED)
    assert matched is not None and matched[0].get("name") == COOKED
