"""Q839 / P0-D3：items[].amount 语义标注的最小回归。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.amount_kind import classify_amount_kind, tag_item, tag_payload  # noqa: E402


@pytest.mark.parametrize("item,expected", [
    ({"amount": 400, "currency": "CNY"}, "money"),
    ({"amount": 9.9, "currency": "CNY"}, "money"),
    ({"amount": 2000, "currency": "TRY"}, "money"),          # 外币仍是钱
    ({"amount": 12, "unit": "元"}, "money"),                 # 口语元
    ({"amount": 3, "unit": "块"}, "money"),
    ({"amount": 87.3}, "unknown"),                           # 体重 87.3kg：不猜
    ({"amount": 1500}, "unknown"),                           # 皮肤价 1500（无单位）
    ({"amount": 100, "unit": "克"}, "weight"),
    ({"amount": 2.5, "unit": "kg"}, "weight"),
    ({"amount": 500, "unit": "毫升"}, "weight"),
    ({"amount": 10, "unit": "抽"}, "quantity"),
    ({"amount": 2, "unit": "个"}, "quantity"),
    ({"amount": 666, "unit": "月"}, "quantity"),
    ({"amount": 12099, "unit": "点"}, "quantity"),
    ({"amount": 8, "unit": "万"}, "unknown"),                 # 车价"八万"：量级不是量纲
    ({"amount": 8, "unit": "万元"}, "money"),                 # 但"万元"就是钱
    ({"amount": 3, "unit": "千克"}, "weight"),
    ({"amount": 5, "unit": "百"}, "unknown"),
    ({"amount": None}, None),
    ({}, None),
])
def test_classify_amount_kind(item, expected):
    assert classify_amount_kind(item) == expected


def test_currency_wins_over_unit():
    """"2 千里拉" 这种：unit 写成元也不影响"这是钱"。"""
    assert classify_amount_kind({"amount": 2000, "currency": "TRY", "unit": "元"}) == "money"


def test_non_numeric_amount_is_ignored():
    assert classify_amount_kind({"amount": "很多"}) is None
    assert classify_amount_kind({"amount": True}) is None


def test_tag_item_is_idempotent_and_non_destructive():
    item = {"item_id": "i1", "amount": 84.35}
    tag_item(item)
    assert item["amount_kind"] == "unknown" and item["amount"] == 84.35
    tag_item(item)
    assert item["amount_kind"] == "unknown"


def test_tag_payload_covers_activities_and_unassociated():
    payload = {
        "activities": [{"items": [{"amount": 400, "currency": "CNY"},
                                  {"amount": 100, "unit": "克"},
                                  {"amount": None}]}],
        "unassociated_items": [{"items": [{"amount": 10, "unit": "抽"}]}],
    }
    tag_payload(payload)
    a = payload["activities"][0]["items"]
    assert [i.get("amount_kind") for i in a] == ["money", "weight", None]
    assert payload["unassociated_items"][0]["items"][0]["amount_kind"] == "quantity"


def test_tag_payload_does_not_change_any_amount_value():
    payload = {"activities": [{"items": [
        {"amount": 84.35}, {"amount": 500}, {"amount": 8}, {"amount": 400, "currency": "CNY"}]}]}
    before = [dict(i) for i in payload["activities"][0]["items"]]
    tag_payload(payload)
    after = payload["activities"][0]["items"]
    assert [i["amount"] for i in after] == [i["amount"] for i in before]
    assert all(set(b) <= set(a) for b, a in zip(before, after)), "只许加字段，不许改/删"
