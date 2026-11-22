"""activity_presentation 展示层同实体活动聚合测试（inferred_entity_group）。"""
from __future__ import annotations

from life_logger.activity_cards import build_activity_cards
from life_logger.activity_links import build_activity_links
from life_logger.activity_presentation import build_activity_presentation


def _duck_events():
    """10-25 真实数据中鸭腿相关的 4 条无显式 activity_ref 记录。"""
    return [
        {"event_id": "evt_order_duck", "event_key": "live:1", "title": "购买鸭腿 (¥10.00)", "activity_type": "expense",
         "time": {"absolute": "2026-10-24T16:19:46.685000+00:00"},
         "data": {"expense": {"amount": 10.0, "currency": "CNY", "item": "鸭腿"}, "food": {"item": "鸭腿"}},
         "evidence": {"source_label": "self"}},
        {"event_id": "evt_ctx_portion", "event_key": "live:2", "title": "购买鸭腿一份（微辣）", "activity_type": "meal",
         "time": {"absolute": "2026-10-24T16:19:46.685000+00:00"},
         "data": {"food": {"item": "鸭腿（微辣）"}},
         "evidence": {"source_label": "self"}},
        {"event_id": "evt_ctx_reorder", "event_key": "live:3", "title": "再次点鸭腿一份", "activity_type": "meal",
         "time": {"absolute": "2026-10-24T16:19:51.925000+00:00"},
         "data": {"food": {"item": "鸭腿"}},
         "evidence": {"source_label": "self"}},
        {"event_id": "evt_ctx_spicy", "event_key": "live:4", "title": "明确要求鸭腿微辣", "activity_type": "meal",
         "time": {"absolute": "2026-10-24T16:19:55.565000+00:00"},
         "data": {"food": {"item": "鸭腿（微辣）"}},
         "evidence": {"source_label": "self"}},
    ]


def test_inferred_entity_group_for_duck_records():
    events = _duck_events()
    cards = build_activity_cards(events).get("cards", [])
    links = build_activity_links(events)
    # 确认 links 没有显式 activity_ref（members 为空）
    assert links.get("members") == {}

    out = build_activity_presentation(cards, links)
    assert len(out["groups"]) == 1
    group = out["groups"][0]
    assert group["inferred"] is True
    assert group["basis"]["kind"] == "inferred_entity_group"
    assert group["basis"]["inferred"] is True
    assert group["basis"]["verified"] is False
    assert group["basis"]["activity_entity_verified"] is False
    assert group["basis"]["entity"] == "鸭腿"
    assert sorted(group["member_event_ids"]) == sorted([
        "evt_order_duck", "evt_ctx_portion", "evt_ctx_reorder", "evt_ctx_spicy"
    ])
    assert group["summary"]["member_count"] == 4
    assert group["activity_ref"] == "inferred:meal:鸭腿"
    # 原事件 ID、金额等不被篡改
    assert out["coverage"]["events_grouped"] == 4
    assert out["coverage"]["events_ungrouped"] == 0


def test_inferred_entity_group_with_flavor_and_requirement_modifiers():
    """验证包含属性修饰词（口味/要求/调整为/辣/微辣等）的真实标题精准归一化聚合成一组。"""
    events = [
        {"event_id": "e_order", "title": "点了一份鸭腿", "activity_type": "meal",
         "time": {"absolute": "2026-10-24T16:19:46+00:00"}},
        {"event_id": "e_buy", "title": "购买鸭腿", "activity_type": "expense",
         "time": {"absolute": "2026-10-24T16:19:46+00:00"}},
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
    assert group["inferred"] is True
    assert group["activity_ref"] == "inferred:meal:鸭腿"
    assert group["basis"]["entity"] == "鸭腿"
    assert sorted(group["member_event_ids"]) == sorted(["e_order", "e_buy", "e_flavor1", "e_flavor2"])
    assert group["summary"]["member_count"] == 4
    assert out["coverage"]["events_grouped"] == 4
    assert out["coverage"]["events_ungrouped"] == 0


def test_no_inference_across_different_hours():
    events = [
        {"event_id": "e1", "title": "购买鸭腿", "activity_type": "meal",
         "time": {"absolute": "2026-10-24T16:19:46+00:00"}, "data": {"food": {"item": "鸭腿"}}},
        {"event_id": "e2", "title": "再次点鸭腿一份", "activity_type": "meal",
         "time": {"absolute": "2026-10-25T04:19:46+00:00"}, "data": {"food": {"item": "鸭腿"}}},
    ]
    cards = build_activity_cards(events).get("cards", [])
    links = build_activity_links(events)
    out = build_activity_presentation(cards, links)
    assert out["groups"] == [], "跨小时事件不得强行聚合"
    assert len(out["ungrouped_event_ids"]) == 2


def test_no_inference_for_different_actors():
    events = [
        {"event_id": "e1", "title": "购买鸭腿", "activity_type": "meal",
         "time": {"absolute": "2026-10-24T16:19:46+00:00"}, "data": {"food": {"item": "鸭腿"}},
         "evidence": {"source_label": "张三"}},
        {"event_id": "e2", "title": "购买鸭腿一份（微辣）", "activity_type": "meal",
         "time": {"absolute": "2026-10-24T16:19:46+00:00"}, "data": {"food": {"item": "鸭腿"}},
         "evidence": {"source_label": "李四"}},
    ]
    cards = build_activity_cards(events).get("cards", [])
    links = build_activity_links(events)
    out = build_activity_presentation(cards, links)
    assert out["groups"] == [], "不同主体事件不得聚合"


def test_no_inference_for_non_food_events():
    events = [
        {"event_id": "e1", "title": "健身卧推", "activity_type": "exercise",
         "time": {"absolute": "2026-10-25T02:00:00+00:00"}},
        {"event_id": "e2", "title": "健身深蹲", "activity_type": "exercise",
         "time": {"absolute": "2026-10-25T02:00:00+00:00"}},
    ]
    cards = build_activity_cards(events).get("cards", [])
    links = build_activity_links(events)
    out = build_activity_presentation(cards, links)
    assert out["groups"] == [], "非餐饮事件无显式引用不得推断聚合"


def test_no_inference_for_generic_meal_terms():
    events = [
        {"event_id": "e1", "title": "吃午饭", "activity_type": "meal",
         "time": {"absolute": "2026-10-25T04:00:00+00:00"}},
        {"event_id": "e2", "title": "午餐打包", "activity_type": "meal",
         "time": {"absolute": "2026-10-25T04:00:00+00:00"}},
    ]
    cards = build_activity_cards(events).get("cards", [])
    links = build_activity_links(events)
    out = build_activity_presentation(cards, links)
    assert out["groups"] == [], "泛餐饮词无具体实体不得成组"


def test_explicit_activity_ref_takes_precedence():
    events = [
        {"event_id": "e1", "title": "购买鸭腿", "activity_type": "meal",
         "time": {"absolute": "2026-10-24T16:19:46+00:00"},
         "data": {"activity_ref": {"namespace": "custom", "id": "act_explicit"}}},
        {"event_id": "e2", "title": "再次点鸭腿一份", "activity_type": "meal",
         "time": {"absolute": "2026-10-24T16:19:46+00:00"},
         "data": {"activity_ref": {"namespace": "custom", "id": "act_explicit"}}},
    ]
    cards = build_activity_cards(events).get("cards", [])
    links = build_activity_links(events)
    out = build_activity_presentation(cards, links)
    assert len(out["groups"]) == 1
    # 应该由显式规则成组，而不是推断组
    group = out["groups"][0]
    assert group["basis"]["kind"] == "explicit_activity_declaration"
    assert group["activity_ref"] == "custom:act_explicit"
    assert "inferred" not in group
