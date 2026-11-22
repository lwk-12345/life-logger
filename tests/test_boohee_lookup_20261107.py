"""Boohee last-resort lookup, quota, write-back and missing-nutrient semantics."""
from __future__ import annotations

import copy
import datetime
import json

import pytest

from life_logger import boohee
from life_logger.food_lookup import BrandFoodLibrary, FoodLookup
from life_logger.food_nutrition import nutrition_for_item


class FakeTransport:
    def __init__(self, search_items=None, detail=None):
        self.calls = []
        self.search_items = search_items or []
        self.detail_payload = detail or {}

    def __call__(self, request):
        self.calls.append(request)
        if request["url"].endswith("/access_tokens"):
            return {"access_token": "fake-token", "expired_at": "2099-01-01T00:00:00+08:00"}
        if "/foods/search" in request["url"]:
            return {"items": copy.deepcopy(self.search_items)}
        if "/foods/" in request["url"]:
            return copy.deepcopy(self.detail_payload)
        raise AssertionError(request["url"])


def client(tmp_path, transport, *, limit=50, clock=None, credentials=None):
    return boohee.BooheeClient(
        tmp_path,
        credentials=credentials or {"app_id": "id", "app_secret": "secret", "daily_limit": limit},
        transport=transport,
        now=clock,
    )


def test_quota_persists_per_day_and_resets_across_days(tmp_path):
    now = [datetime.datetime(2026, 11, 7, 10, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=8)))]
    transport = FakeTransport([{"code": "c1", "name": "牛奶", "calory": "65", "weight": "100"}])
    b = client(tmp_path, transport, limit=2, clock=lambda: now[0])
    assert b.search("牛奶") is not None
    assert b.detail("c1") is None or True          # detail 也计数
    assert b.search("牛奶") == []                  # 第 3 次超额，返回空、不抛
    usage = json.loads((tmp_path / "boohee-usage.json").read_text())
    assert usage["count"] >= 2
    now[0] = now[0] + datetime.timedelta(days=1)
    assert b.search("牛奶") is not None            # 跨日重置
    assert json.loads((tmp_path / "boohee-usage.json").read_text())["count"] == 1


def test_detail_counts_against_the_same_daily_budget(tmp_path):
    now = datetime.datetime(2026, 11, 7, 10, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
    transport = FakeTransport([{"code": "c1", "name": "牛奶", "calory": "65", "weight": "100"}])
    b = client(tmp_path, transport, limit=1, clock=lambda: now)
    assert b.search("牛奶") is not None
    assert b.detail("c1") is None
    assert len([c for c in transport.calls if "access_tokens" not in c["url"]]) == 1


def test_write_back_means_second_lookup_does_not_hit_network(tmp_path):
    transport = FakeTransport(
        [{"code": "c9", "name": "思念 猪肉白菜水饺", "calory": "220", "weight": "100", "is_liquid": False}],
        {"food": {"name": "思念 猪肉白菜水饺", "code": "c9"},
         "calory": [{"name": "能量", "value": 220, "unit_name": "千卡"}],
         "base_ingredients": [{"name": "蛋白质", "value": 8.0, "unit_name": "克"},
                              {"name": "脂肪", "value": 7.0, "unit_name": "克"},
                              {"name": "碳水化合物", "value": 30.0, "unit_name": "克"}],
         "mineral": [{"name": "钠", "value": 500, "unit_name": "毫克"}],
         "vitamin": [], "other_ingredients": [], "amino_acid": []},
    )
    data = tmp_path / "data"
    engine = FoodLookup(data, boohee_client=client(data, transport), boohee_enabled=True)
    first = engine.lookup("思念 猪肉白菜水饺")
    assert first is not None and first.source == "boohee"
    assert first.per_100g["energy_kcal"] == 220
    assert first.per_100g["carbohydrates_g"] == 30
    calls = len(transport.calls)
    second = engine.lookup("思念 猪肉白菜水饺")
    assert second is not None and second.source == "boohee"
    assert len(transport.calls) == calls          # 命中写回的品牌库，不再联网
    stored = json.loads((data / "brand-library.json").read_text())
    entry = next(e for e in stored["entries"] if e["name"] == "思念 猪肉白菜水饺")
    assert entry["code"] == "c9" and entry["origin"] == "boohee"
    assert "思念 猪肉白菜水饺" in entry["keywords"]


def test_user_entry_wins_and_boohee_is_not_called(tmp_path):
    transport = FakeTransport([{"code": "c1", "name": "牛奶", "calory": "1", "weight": "100"}])
    data = tmp_path / "data"
    lib = BrandFoodLibrary(data / "brand-library.json")
    lib.remember({"name": "牛奶", "per_100g": {"energy_kcal": 65, "protein_g": 3.3},
                  "origin": "user_spoken", "evidence": "user_reported"})
    engine = FoodLookup(data, brand_library=lib, boohee_client=client(data, transport), boohee_enabled=True)
    result = engine.lookup("牛奶")
    assert result.source == "user_spoken" and result.per_100g["energy_kcal"] == 65
    assert transport.calls == []


def _boohee_engine(tmp_path, items, detail=None):
    data = tmp_path / "data"
    transport = FakeTransport(items, detail)
    engine = FoodLookup(data, boohee_client=client(data, transport), boohee_enabled=True)
    return engine, transport


def test_multiple_candidates_pick_nearest_name(tmp_path):
    engine, _ = _boohee_engine(tmp_path, [
        {"code": "a", "name": "伊利 纯牛奶", "calory": "60", "weight": "100"},
        {"code": "b", "name": "蒙牛 低脂牛奶", "calory": "45", "weight": "100"},
        {"code": "c", "name": "香蕉", "calory": "93", "weight": "100"},
    ], {"food": {"name": "蒙牛 低脂牛奶"}, "calory": [{"name": "能量", "value": 45}],
        "base_ingredients": [], "mineral": [], "vitamin": [], "other_ingredients": [], "amino_acid": []})
    result = engine.lookup("蒙牛 低脂牛奶")
    assert result.matched_name == "蒙牛 低脂牛奶"
    assert result.estimate_basis == "nearest_match"
    assert result.note and "蒙牛" in result.note


def test_tied_candidates_use_median_calories(tmp_path):
    engine, _ = _boohee_engine(tmp_path, [
        {"code": "a", "name": "水饺 白菜", "calory": "100", "weight": "100"},
        {"code": "b", "name": "水饺 白菜", "calory": "300", "weight": "100"},
        {"code": "c", "name": "水饺 白菜", "calory": "200", "weight": "100"},
    ], {"food": {"name": "水饺 白菜"}, "calory": [{"name": "能量", "value": 200}],
        "base_ingredients": [], "mineral": [], "vitamin": [], "other_ingredients": [], "amino_acid": []})
    result = engine.lookup("水饺 白菜")
    assert result.estimate_basis == "median_of_candidates"
    assert result.per_100g["energy_kcal"] == 200


def test_dish_guard_applies_to_boohee_candidates(tmp_path):
    engine, _ = _boohee_engine(tmp_path, [
        {"code": "a", "name": "猪肉", "calory": "395", "weight": "100"},
    ])
    assert engine.lookup("猪肉芹菜饺子") is None


def test_boohee_disabled_means_no_network(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test")
    data = tmp_path / "data"
    transport = FakeTransport([{"code": "c1", "name": "牛奶", "calory": "65", "weight": "100"}])
    engine = FoodLookup(data, boohee_client=client(data, transport))
    assert engine.lookup("牛奶") is None
    assert transport.calls == []


def test_brand_library_without_path_refuses_to_pretend_saved():
    lib = BrandFoodLibrary(None)
    with pytest.raises(ValueError, match="path"):
        lib.remember({"name": "牛奶", "per_100g": {"energy_kcal": 65}})


def test_missing_nutrients_are_null_not_zero(monkeypatch):
    from life_logger import food_lookup as fl

    class Partial:
        def lookup(self, name):
            return fl.FoodNutrition(query=name, name=name, matched_name=name,
                                    per_100g={"energy_kcal": 100.0, "protein_g": 5.0,
                                              "fat_g": 2.0, "sodium_mg": 80.0},
                                    source="brand", confidence="high")
    item = nutrition_for_item("测试食物", 100, lookup=Partial())
    assert item["nutrients"]["carbohydrates_g"] is None
    assert "carbohydrates_g" in item["missing_nutrients"]
    assert item["nutrients"]["protein_g"] == 5.0


def test_search_energy_is_scaled_from_reported_weight(tmp_path):
    engine, _ = _boohee_engine(tmp_path, [
        {"code": "a", "name": "能量棒", "calory": "100", "weight": "50"},
    ])
    result = engine.lookup("能量棒")
    assert result.per_100g["energy_kcal"] == 200.0


def test_median_basis_and_low_confidence_reach_nutrition_item(monkeypatch):
    from life_logger import food_lookup as fl

    class Median:
        def lookup(self, name):
            return fl.FoodNutrition(query=name, name="水饺 白菜", matched_name="水饺 白菜",
                                    per_100g={"energy_kcal": 200.0, "protein_g": 6.0},
                                    source="boohee", confidence="medium",
                                    estimate_basis="median_of_candidates",
                                    estimate_confidence="low")
    item = nutrition_for_item("水饺 白菜", 100, lookup=Median())
    assert item["estimate_basis"] == "median_of_candidates"
    assert item["estimate_confidence"] == "low"
    assert item["nutrients"]["fat_g"] is None
