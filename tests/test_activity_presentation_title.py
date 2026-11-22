"""activity_presentation 活动组代表标题加权选择测试。"""
from __future__ import annotations

from life_logger.activity_cards import build_activity_cards
from life_logger.activity_links import build_activity_links
from life_logger.activity_presentation import build_activity_presentation


def test_duck_group_selects_purchase_title_over_flavor_modifier():
    """断言 10-25 鸭腿组的 summary.title 正确选出带交易/金额的核心卡，而非纯属性卡。"""
    events = [
        {"event_id": "e_flavor1", "title": "鸭腿口味要求辣", "activity_type": "meal",
         "time": {"absolute": "2026-10-24T16:19:50+00:00"}},
        {"event_id": "e_buy", "title": "购买鸭腿 (¥10.00)", "activity_type": "expense",
         "time": {"absolute": "2026-10-24T16:19:46+00:00"},
         "data": {"expense": {"amount": 10.0, "currency": "CNY", "item": "鸭腿"}}},
        {"event_id": "e_order", "title": "点了一份鸭腿", "activity_type": "meal",
         "time": {"absolute": "2026-10-24T16:19:46+00:00"}},
        {"event_id": "e_flavor2", "title": "鸭腿口味调整为微辣", "activity_type": "meal",
         "time": {"absolute": "2026-10-24T16:19:55+00:00"}},
    ]
    cards = build_activity_cards(events).get("cards", [])
    links = build_activity_links(events)
    out = build_activity_presentation(cards, links)

    assert len(out["groups"]) == 1
    group = out["groups"][0]
    # 第一优先：必须是带有交易金额的核心卡片
    assert group["summary"]["title"] == "购买鸭腿 (¥10.00)"
    assert group["summary"]["title"] != "鸭腿口味要求辣"
    assert group["summary"]["title"] != "鸭腿口味调整为微辣"


def test_second_priority_selects_order_action_over_modifier():
    """在无金额卡片时，第二优先必须选明确的点单/购买核心行为卡，而非纯修饰卡。"""
    events = [
        {"event_id": "e_flavor1", "title": "鸭腿口味要求辣", "activity_type": "meal",
         "time": {"absolute": "2026-10-24T16:19:50+00:00"}},
        {"event_id": "e_order", "title": "点了一份鸭腿", "activity_type": "meal",
         "time": {"absolute": "2026-10-24T16:19:46+00:00"}},
        {"event_id": "e_flavor2", "title": "鸭腿口味调整为微辣", "activity_type": "meal",
         "time": {"absolute": "2026-10-24T16:19:55+00:00"}},
    ]
    cards = build_activity_cards(events).get("cards", [])
    links = build_activity_links(events)
    out = build_activity_presentation(cards, links)

    assert len(out["groups"]) == 1
    group = out["groups"][0]
    assert group["summary"]["title"] == "点了一份鸭腿"


def test_pure_modifier_used_as_fallback_when_no_better_candidate():
    """若全为纯属性/口味卡，则最末兜底正常返回有效标题。"""
    events = [
        {"event_id": "e_flavor1", "title": "鸭腿口味要求辣", "activity_type": "meal",
         "time": {"absolute": "2026-10-24T16:19:50+00:00"}},
        {"event_id": "e_flavor2", "title": "鸭腿口味调整为微辣", "activity_type": "meal",
         "time": {"absolute": "2026-10-24T16:19:55+00:00"}},
    ]
    cards = build_activity_cards(events).get("cards", [])
    links = build_activity_links(events)
    out = build_activity_presentation(cards, links)

    assert len(out["groups"]) == 1
    group = out["groups"][0]
    assert group["summary"]["title"] in ("鸭腿口味要求辣", "鸭腿口味调整为微辣")
