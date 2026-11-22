"""GET keeps standard-serving provenance for ordinary Watch/activity facts."""
import json

from test_nutrition_api import DAY, api as api, day, fingerprint, food_event, seed_events


def seed_activities(api, names):
    items = [{"item_id": f"i{index}", "action": name, "fact_type": "food", "status": "completed",
              "spoken_at": DAY + "T12:00:00+08:00", "occurred_at": None,
              "evidence": [{"source_id": f"watch-food-{index}", "quote": "午饭吃了" + name, "source_label": "self"}]}
             for index, name in enumerate(names)]
    path = api[0].root / "activity-timeline" / "jev" / (DAY + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"activities": [{"activity_id": "a1", "lifecycle": "live", "items": items}]}, ensure_ascii=False))


def test_get_activity_standard_portion_uses_existing_library_and_labels_estimate(api):
    seed_activities(api, ["米饭"])
    before = fingerprint(api[0].root.parent)
    status, snapshot = day(api)
    assert status == 200
    row = snapshot["meals"]["lunch"][0]
    assert row["weight_g"] == 150
    # 2026-11-07 用户决定：不知重量就不算（原话「午饭吃了米饭」没克数）→ 不计热，库算法只作候选
    assert row["calories_kcal"] is None
    assert row["basis"] == "unknown"
    assert row["estimate_basis"] == "no_portion_spoken"
    assert row["portion_estimate_candidate"]["calories_kcal"] == 174
    assert row["estimate_confidence"] == "low"
    assert snapshot["estimated_calories_kcal"] is None
    assert snapshot["estimated_share"] is None
    # /api/nutrition/day 的投影目前不透出 unweighed_*（web 层不在本单范围），用记录级字段核对
    assert row["portion_estimate_candidate"]["basis"] == "standard_portion"
    assert fingerprint(api[0].root.parent) == before


def test_get_mixed_weighed_standard_unknown_share_uses_only_known_calories(api):
    seed_events(api, [food_event()])
    seed_activities(api, ["米饭", "未知ZXQ"])
    code, snapshot = day(api)
    assert code == 200
    # 2026-11-07 用户决定：不知重量就不算：三条都没说克数 → 合计 None，只有候选
    assert snapshot["totals"]["calories_kcal"] is None
    assert snapshot["estimated_calories_kcal"] is None
    assert snapshot["estimated_share"] is None
    assert {r["item"]: r["basis"] for r in snapshot["meals"]["lunch"]} == {
        "土豆": "unknown", "米饭": "unknown", "未知ZXQ": "unknown"}
    assert snapshot["unknown_nutrition_count"] == 3


def test_get_unknown_activity_food_stays_null(api):
    seed_activities(api, ["未知ZXQ"])
    code, snapshot = day(api)
    assert code == 200 and snapshot["has_records"]
    row = snapshot["meals"]["lunch"][0]
    assert row["calories_kcal"] is None
    assert all(value is None for value in row["nutrients"].values())
    assert row["basis"] == "unknown"
    assert snapshot["estimated_share"] is snapshot["estimated_calories_kcal"] is None


def test_get_known_zero_does_not_divide_by_zero(api):
    seed_events(api, [food_event("水", calories=0, nutrients={"protein_g": 0, "carbohydrates_g": 0, "fat_g": 0, "sodium_mg": 0})])
    code, snapshot = day(api)
    assert code == 200 and snapshot["has_records"]
    assert snapshot["totals"]["calories_kcal"] == 0
    assert snapshot["estimated_share"] is None
