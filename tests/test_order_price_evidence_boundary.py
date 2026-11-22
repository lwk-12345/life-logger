"""Tests for order price evidence boundary vs background POS broadcast.

Covers:
1. Positive: self order + clerk price quote ("我加个烤肠 多少钱 三块") + background POS broadcast ("支付宝到账三元")
2. Positive: explicit related_quotes with segment/source coordinates
3. Positive: real Q399 candidate evt_order_e530b53879bdd163 (3.0 CNY) accepted
4. Negative: same broadcast different person (source_label = non_self)
5. Negative: mismatched item (quoted 烤肠, item says 奶茶)
6. Negative: mismatched price (clerk quoted 5, event says 3)
7. Negative: missing coordinates in related_quotes
8. Negative: standalone POS broadcast with no order action
9. Negative: order action present but missing clerk quote (only background POS broadcast) -> pos_broadcast_unbound_order
10. Preservation: 38 CNY with "能不能加个雪球" stays accepted
"""

from __future__ import annotations

from life_logger.expense_policy import build_expense_ledger


def base_order_event(
    quote: str,
    amount: float,
    *,
    item: str = "烤肠",
    source_label: str = "self",
    related_quotes: list[dict] | None = None,
    event_id: str = "evt_order_test_1",
) -> dict:
    ev = {
        "event_id": event_id,
        "title": f"购买{item} (¥{amount:.2f})",
        "categories": ["expense", "food"],
        "activity_type": "purchase",
        "claim_type": "event",
        "event_state": "completed",
        "time": {
            "absolute": "2026-10-25T00:20:00+08:00",
            "precision": "minute",
        },
        "data": {
            "expense": {
                "amount": amount,
                "currency": "CNY",
                "item": item,
                "payment_channel": "offline_conversational",
            },
            "food": {"item": item, "meal_type": "正餐/快餐"},
        },
        "evidence": {
            "source_label": source_label,
            "quote": quote,
            "recording_id": "watch_test_rec",
        },
        "review_status": "pending_review",
    }
    if related_quotes is not None:
        ev["evidence"]["related_quotes"] = related_quotes
    return ev


def test_positive_self_order_with_clerk_quote_and_pos_broadcast():
    """本人点单 + 店员报价 + 背景播报 -> 正常入账 3 元，不被 POS 误杀。"""
    quote = "我加个烤肠 多少钱 三块 支付宝到账三元"
    event = base_order_event(quote, 3.0, item="烤肠", source_label="self")
    ledger = build_expense_ledger([event])

    assert ledger["total_amount"] == 3.0
    assert len(ledger["transactions"]) == 1
    assert ledger["transactions"][0]["amount"] == 3.0
    assert len(ledger["pending_broadcast"]) == 0


def test_positive_explicit_related_quotes_with_coordinates():
    """带有明确坐标的 related_quotes 分角色证据 -> 正常入账。"""
    related = [
        {"text": "我加个烤肠", "source_label": "self", "segment_id": "seg_001", "timestamp": "2026-10-25T00:20:45+08:00"},
        {"text": "三块", "source_label": "unknown", "segment_id": "seg_002", "timestamp": "2026-10-25T00:20:47+08:00"},
    ]
    quote = "我加个烤肠 多少钱 三块 支付宝到账三元"
    event = base_order_event(quote, 3.0, item="烤肠", related_quotes=related)
    ledger = build_expense_ledger([event])

    assert ledger["total_amount"] == 3.0
    assert len(ledger["transactions"]) == 1
    assert len(ledger["pending_broadcast"]) == 0


def test_positive_q399_candidate_full_quote():
    """真实 Q399 候选：长原话包含先前点单与背景音，但有明确烤肠3元点单报价 -> 正常入账。"""
    quote = (
        "拿一个是吧 拿一个是吧 对 多少钱 十块 支付宝到账十元 报名疯狂碰友节 "
        "顾客碰一下支付最高立减三元 微信收款十元 我加个烤肠 多少钱 三块 支付宝到账三元"
    )
    event = base_order_event(quote, 3.0, item="烤肠", source_label="self", event_id="evt_order_e530b53879bdd163")
    ledger = build_expense_ledger([event])

    assert ledger["total_amount"] == 3.0
    assert len(ledger["transactions"]) == 1
    assert ledger["transactions"][0]["event_id"] == "evt_order_e530b53879bdd163"


def test_negative_different_person_same_broadcast():
    """同一播报但点单人不是 self (例如 non_self) -> 绝不进本人账本。"""
    quote = "我加个烤肠 多少钱 三块 支付宝到账三元"
    event = base_order_event(quote, 3.0, item="烤肠", source_label="non_self")
    ledger = build_expense_ledger([event])

    assert ledger["total_amount"] == 0.0
    assert len(ledger["transactions"]) == 0
    # non_self 走播报未归属
    reasons = [p["reason"] for p in ledger["pending_broadcast"]] + [p["reason"] for p in ledger["pending_transactions"]]
    assert "pos_broadcast_unattributed" in reasons


def test_negative_mismatched_item():
    """原话点的是烤肠，但 item 标为奶茶 -> 不予放行。"""
    quote = "我加个烤肠 多少钱 三块 支付宝到账三元"
    event = base_order_event(quote, 3.0, item="奶茶", source_label="self")
    ledger = build_expense_ledger([event])

    assert ledger["total_amount"] == 0.0
    assert len(ledger["transactions"]) == 0
    # 保持在 pending，明确理由为 order_item_mismatch
    pending_all = ledger["pending_transactions"] + ledger["pending_broadcast"]
    assert any(p.get("reason") == "order_item_mismatch" for p in pending_all)


def test_negative_mismatched_price():
    """店员说五块，金额却标 3 元 -> 价格不符，不予放行。"""
    quote = "我加个烤肠 多少钱 五块 支付宝到账五元"
    event = base_order_event(quote, 3.0, item="烤肠", source_label="self")
    ledger = build_expense_ledger([event])

    assert ledger["total_amount"] == 0.0
    assert len(ledger["transactions"]) == 0
    pending_all = ledger["pending_transactions"] + ledger["pending_broadcast"]
    assert any(p.get("reason") == "pos_broadcast_unbound_order" for p in pending_all)


def test_negative_missing_coordinates():
    """声称有 related_quotes 但缺少任何段坐标或时间戳 -> 不能放行。"""
    related = [
        {"text": "我加个烤肠", "source_label": "self"},
        {"text": "三块", "source_label": "unknown"},
    ]
    quote = "我加个烤肠 多少钱 三块 支付宝到账三元"
    event = base_order_event(quote, 3.0, item="烤肠", related_quotes=related)
    ledger = build_expense_ledger([event])

    assert ledger["total_amount"] == 0.0
    assert len(ledger["transactions"]) == 0
    pending_all = ledger["pending_transactions"] + ledger["pending_broadcast"]
    assert any(p.get("reason") == "order_evidence_missing_coordinates" for p in pending_all)


def test_negative_standalone_pos_broadcast():
    """纯单 POS 播报，无任何点单或店员报价 -> 保持既有 B 档待确认。"""
    event = {
        "event_id": "evt_pos_standalone",
        "title": "线下支付 (¥3.00)",
        "categories": ["expense"],
        "activity_type": "pos_payment_broadcast",
        "data": {"expense": {"amount": 3.0, "currency": "CNY"}},
        "evidence": {"source_label": "non_self", "quote": "支付宝到账三元"},
        "review_status": "unreviewed",
    }
    ledger = build_expense_ledger([event])

    assert ledger["total_amount"] == 0.0
    assert len(ledger["transactions"]) == 0
    assert len(ledger["pending_broadcast"]) == 1
    assert ledger["pending_broadcast"][0]["reason"] == "pos_broadcast_pending_confirmation"


def test_negative_order_action_without_clerk_quote():
    """只有点单动作和背景音箱播报，无店员独立报价问答 -> 拒绝盲目绑定，保持 pending。"""
    quote = "我加个烤肠 支付宝到账三元"
    event = base_order_event(quote, 3.0, item="烤肠", source_label="self")
    ledger = build_expense_ledger([event])

    assert ledger["total_amount"] == 0.0
    assert len(ledger["transactions"]) == 0
    pending_all = ledger["pending_transactions"] + ledger["pending_broadcast"]
    assert any(p.get("reason") == "pos_broadcast_unbound_order" for p in pending_all)


def test_preservation_of_q404_taste_question_order():
    """验证 88a76fd9 口味问句点单 (38元) 依然完整被接受。"""
    quote = "要一份咖喱牛肉蛋炒饭，然后一份儿炸薯条。清补凉能不能给我加个雪球啊？三个一共多少钱？说三十八。"
    event = {
        "event_id": "evt_order_38",
        "title": "购买晚餐 (¥38.00)",
        "categories": ["expense", "food"],
        "activity_type": "meal",
        "claim_type": "event",
        "event_state": "completed",
        "time": {"absolute": "2026-10-24T23:55:00+08:00", "precision": "minute"},
        "data": {"expense": {"amount": 38.0, "currency": "CNY", "item": "炒饭、薯条、清补凉"}},
        "evidence": {"source_label": "self", "quote": quote},
        "review_status": "pending_review",
    }
    ledger = build_expense_ledger([event])

    assert ledger["total_amount"] == 38.0
    assert len(ledger["transactions"]) == 1
    assert len(ledger["excluded_events"]) == 0
