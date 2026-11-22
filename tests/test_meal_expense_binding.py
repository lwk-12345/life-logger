"""Q364 点餐总额绑定：本人点餐总额入账，短剧/广告收款音严格隔离。"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.event_episodes import bind_meal_totals, parse_meal_total  # noqa: E402
from life_logger.expense_policy import build_expense_ledger  # noqa: E402


def ev(eid, quote, *, cats=("food",), actor="self", rec="watch_A", seg=None, data=None, claim="order"):
    event = {
        "event_id": eid,
        "title": quote[:24],
        "categories": list(cats),
        "activity_type": "meal",
        "time": {"precision": "exact", "absolute": "2026-10-24T12:10:00+08:00"},
        "data": dict(data or {}),
        "evidence": {"quote": quote, "source_label": actor, "recording_id": rec,
                     "segment_id": seg, "claim_type": claim,
                     "reported_at": "2026-10-24T12:10:00+08:00"},
    }
    return event


def total_of(events):
    ledger = build_expense_ledger(events)
    return ledger.get("total_amount"), ledger.get("transaction_count"), ledger


# 场景 A：一单多菜 + 紧随总额"三十八"
def test_scenario_a_multi_dish_total_binds_38():
    dishes = [
        ev("d1", "要一份咖喱牛肉炒饭", seg="s1"),
        ev("d2", "再来一份炸薯条", seg="s2"),
        ev("d3", "清补凉加个雪球", seg="s3"),
        ev("t1", "一共三十八", seg="s4"),
    ]
    before = copy.deepcopy(dishes)
    result = bind_meal_totals(dishes)
    assert dishes == before, "不得修改入参"
    assert result["stats"]["bound"] == 1
    binding = result["bindings"][0]
    assert binding["event_id"] == "t1"
    assert binding["amount"] == 38.0
    assert "三十八" in binding["matched_text"]
    assert set(binding["dish_event_ids"]) >= {"d1", "d2", "d3"}, "必须引用各菜品"

    total, count, ledger = total_of(result["events"])
    assert total == 38.0 and count == 1, (total, count)
    row = ledger["transactions"][0]
    assert row["event_id"] == "t1" and row["amount"] == 38.0
    assert row["amount_source"] == "expense_amount"      # 账本自己的来源口径
    bound_event = next(e for e in result["events"] if e["event_id"] == "t1")
    assert bound_event["data"]["expense"]["binding"] == "meal_total"   # 绑定来源可追溯


# 场景 B：追加点单"加个烤肠 三块"
def test_scenario_b_addon_binds_3():
    events = [
        ev("o1", "来一份鸭腿饭", seg="s1"),
        ev("o2", "我加个烤肠", seg="s2", data={"food": {"items": [{"name": "烤肠"}]}}),
        ev("o3", "三块", seg="s3", cats=("food", "expense")),
    ]
    result = bind_meal_totals(events)
    amounts = sorted(item["amount"] for item in result["bindings"])
    assert amounts == [3.0], amounts
    total, count, _ = total_of(result["events"])
    assert total == 3.0 and count == 1


# 场景 C：短剧台词内嵌收款音 → 隔离不入账
def test_scenario_c_drama_broadcast_isolated():
    drama = [
        ev("x1", "玄宗门弟子听着", cats=("other",), actor="non_self"),
        ev("x2", "收钱吧，到账十三元", cats=("expense",), actor="non_self", claim="payment"),
        ev("x3", "收钱吧，到账十三元", cats=("expense",), actor="self", claim="payment"),  # 即便标 self 也含播报标记
    ]
    result = bind_meal_totals(drama)
    assert result["bindings"] == []
    assert "broadcast_or_ad_isolated" in {item["reason"] for item in result["skipped"]}
    total, count, _ = total_of(result["events"])
    assert (total or 0) == 0 and count == 0, (total, count)


def test_parse_total_variants():
    assert parse_meal_total("一共三十八")[0] == 38.0
    assert parse_meal_total("三块")[0] == 3.0
    assert parse_meal_total("十五块五")[0] == 15.0          # 只取显式"元/块"前的整数部分
    assert parse_meal_total("38元")[0] == 38.0
    assert parse_meal_total("一百二") is None                # 没有单位不出数
    assert parse_meal_total("味道不错") is None


def test_existing_amount_is_not_overwritten():
    events = [ev("a1", "一共三十八", data={"expense": {"amount": 20.0, "currency": "CNY"}})]
    result = bind_meal_totals(events)
    assert result["bindings"] == []
    assert result["skipped"][0]["reason"] == "amount_already_present"
    total, _, _ = total_of(result["events"])
    assert total == 20.0


def test_non_self_meal_is_skipped():
    events = [ev("n1", "一共三十八", actor="non_self")]
    result = bind_meal_totals(events)
    assert result["bindings"] == []
    assert result["skipped"][0]["reason"] == "not_self"
