"""口头点餐合成：多菜总额、追加点单、既有鸭腿、短剧广告负例。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.contextual_extraction import synthesize_conversational_orders  # noqa: E402


def rec(text, offset, label="self", rec_id="watch_F786", ts="2026-10-24T15:55:00+00:00"):
    return {"source_id": f"src_{offset}", "text": text, "source_label": label,
            "recording_id": rec_id, "start_offset_seconds": offset,
            "segment_id": f"seg_{int(offset)}", "timestamp": ts}


def amounts(events):
    return [round(float((e.get("data") or {}).get("expense", {}).get("amount") or 0), 2) for e in events]


def test_multi_dish_with_chinese_total_synthesizes_38():
    records = [
        rec("呃，要一份这个咖喱牛肉炒饭。", 0.0),
        rec("然后一份儿炸薯条。", 6.0),
        rec("清补凉能不能给我加个雪球。", 12.0),
        rec("说三十八。", 20.0),
    ]
    events = synthesize_conversational_orders(records)
    assert len(events) == 1, events
    event = events[0]
    assert amounts(events) == [38.0]
    assert event["categories"] == ["expense", "food"]
    title = event["title"]
    assert "炒饭" in title and "薯条" in title, title
    assert "咖喱牛肉炒饭" in (event["data"]["food"]["item"] + title)


def test_addon_order_with_three_yuan_total():
    records = [
        rec("来一份鸭腿饭", 0.0),
        rec("我加个烤肠。", 30.0),
        rec("三块。", 34.0),
    ]
    events = synthesize_conversational_orders(records)
    assert amounts(events) == [3.0], events
    assert "烤肠" in events[0]["title"]


def test_existing_duck_order_unchanged():
    records = [rec("来一份鸭腿", 0.0), rec("十块", 4.0)]
    events = synthesize_conversational_orders(records)
    assert amounts(events) == [10.0]
    assert "鸭腿" in events[0]["title"]


def test_drama_broadcast_is_not_synthesized():
    records = [
        rec("玄宗门弟子听着，收钱吧到账十三元。", 0.0, label="non_self"),
        rec("收钱吧，到账十三元。", 5.0),
        rec("支付宝到账十元", 9.0),
        rec("微信收款十元", 12.0),
    ]
    assert synthesize_conversational_orders(records) == []


def test_dish_without_total_yields_nothing():
    records = [rec("要一份咖喱牛肉炒饭", 0.0), rec("味道不错", 6.0)]
    assert synthesize_conversational_orders(records) == []


def test_ambiguous_chinese_number_is_not_guessed():
    records = [rec("来一份炒饭", 0.0), rec("一百二", 5.0)]
    assert synthesize_conversational_orders(records) == []
