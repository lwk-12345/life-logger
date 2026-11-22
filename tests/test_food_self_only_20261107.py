"""Only the user's own speech may enter the nutrition ledger (A/B/C modes)."""
from __future__ import annotations

from life_logger import nutrition_meal_rules as rules
from life_logger.daily_cognitive_session import build_nutrition_ledger

DAY = "2026-11-06"


def food_event(*, title, quote, label="self", claim_type=None, item="米饭"):
    evidence = {"quote": quote, "source_id": "s1", "source_label": label}
    if claim_type:
        evidence["claim_type"] = claim_type
    return {
        "event_id": f"evt-{title}", "title": title, "categories": ["food"],
        "activity_type": "meal",
        "evidence": evidence,
        "data": {"food": {"item": item, "meal": "dinner", "estimated_calories": 100,
                          "nutrients": {"energy_kcal": 100, "protein_g": 2,
                                        "fat_g": 1, "carbohydrates_g": 20, "sodium_mg": 10}}},
    }


class RaisingLookup:
    def lookup(self, *args, **kwargs):
        raise AssertionError("excluded records must not trigger food lookup")


def test_non_self_observation_excluded_in_all_modes(monkeypatch):
    event = food_event(title="查询：他提到配餐里的米饭因涨价而变化",
                       quote="米饭因为最近这些年涨价", label="non_self", claim_type="observation")
    for mode in ("A", "B", "C"):
        monkeypatch.setattr(rules, "FOOD_SELF_ONLY_MODE", mode)
        ledger = build_nutrition_ledger([event], day=DAY, nutrition_lookup=RaisingLookup())
        assert ledger["consumed_record_count"] == 0, mode
        assert ledger["food_records"][0]["exclude_reason"] in {"not_self_speaker", "unverified_speaker"}


def test_self_breakfast_stays_in_all_modes(monkeypatch):
    event = food_event(title="早餐吃了三全水饺", quote="今天早上吃了400克三全猪肉芹菜速冻水饺",
                       item="三全猪肉芹菜速冻水饺")
    for mode in ("A", "B", "C"):
        monkeypatch.setattr(rules, "FOOD_SELF_ONLY_MODE", mode)
        ledger = build_nutrition_ledger([event], day=DAY)
        assert ledger["consumed_record_count"] == 1, mode
        assert ledger["totals"]["calories_kcal"] == 100


def test_missing_label_only_excluded_by_mode_c(monkeypatch):
    event = food_event(title="吃了米饭", quote="我吃了米饭", label=None)
    event["evidence"].pop("source_label")
    monkeypatch.setattr(rules, "FOOD_SELF_ONLY_MODE", "A")
    assert build_nutrition_ledger([event], day=DAY)["consumed_record_count"] == 1
    monkeypatch.setattr(rules, "FOOD_SELF_ONLY_MODE", "C")
    ledger = build_nutrition_ledger([event], day=DAY)
    assert ledger["consumed_record_count"] == 0
    assert ledger["food_records"][0]["exclude_reason"] == "unverified_speaker"


def test_activity_self_food_is_kept_in_mode_c(monkeypatch):
    activity = {"activity_id": "a", "title": "吃米饭", "lifecycle": "live",
                "reported_at": f"{DAY}T12:00:00+08:00",
                "items": [{"item_id": "i1", "action": "吃了米饭", "food_name": "米饭",
                           "fact_type": "food", "status": "completed",
                           "spoken_at": f"{DAY}T12:00:00+08:00",
                           "evidence": [{"source_id": "s1", "quote": "我吃了米饭",
                                         "source_label": "self"}]}]}
    monkeypatch.setattr(rules, "FOOD_SELF_ONLY_MODE", "C")
    ledger = build_nutrition_ledger([], activities=[activity], day=DAY)
    assert ledger["consumed_record_count"] == 1


def test_b_mode_also_excludes_query_titles_without_non_self_label(monkeypatch):
    event = food_event(title="查询：他提到米饭涨价", quote="他提到米饭涨价",
                       label="unknown", claim_type="observation")
    monkeypatch.setattr(rules, "FOOD_SELF_ONLY_MODE", "B")
    ledger = build_nutrition_ledger([event], day=DAY)
    assert ledger["consumed_record_count"] == 0
    assert ledger["food_records"][0]["exclude_reason"] == "not_self_speaker"
