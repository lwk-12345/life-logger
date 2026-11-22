"""Q404: flavor questions inside a confirmed self order stay orders.

The real 10-24 quote contains both a flavor request ("能不能加个雪球") and a
quote for the three-item order ("三十八").  The ledger must not classify that
as an unmade plan merely because "能不能" is present.  Genuine plans, price
questions without an order, non-self sources and POS broadcasts remain excluded
or pending.
"""

from __future__ import annotations

from life_logger.expense_policy import build_expense_ledger

ORDER_QUOTE = (
    "你好。 你好。 你要什么？ 呃，要一份这个咖喱牛肉蛋炒饭。 嗯。 "
    "然后一份儿炸薯条，然后呃。 诶，清补凉能不能给我加个雪球啊？ "
    "清补凉应该加什么那个吗？十五。 好，可以。 三个一共多少钱？ "
    "嗯。 三十八。 好。"
)


def _event(
    *,
    event_id: str = "evt_q404_order",
    quote: str = ORDER_QUOTE,
    amount: float | None = 38.0,
    source_label: str = "self",
    activity_type: str = "meal",
    review_status: str = "pending_review",
    pending_reason: str | None = None,
) -> dict:
    evidence = {
        "quote": quote,
        "source_label": source_label,
        "reported_at": "2026-10-24T23:55:07.127000+08:00",
        "recording_id": "watch_F786C91BCA7743B5BC3D9C40FD93C33A",
        "segment_id": "watch_q404",
    }
    if pending_reason:
        evidence["pending_reason"] = pending_reason
    return {
        "event_id": event_id,
        "title": "购买咖喱牛肉蛋炒饭、炸薯条、清补凉",
        "categories": ["expense", "food"],
        "activity_type": activity_type,
        "time": {
            "absolute": "2026-10-24T23:55:07.127000+08:00",
            "precision": "minute",
            "relative_seconds": None,
        },
        "data": {
            "expense": {
                "amount": amount,
                "currency": "CNY",
                "item": "咖喱牛肉蛋炒饭、炸薯条、清补凉",
            }
        },
        "evidence": evidence,
        "review_status": review_status,
    }


def _ledger(event: dict) -> dict:
    return build_expense_ledger([event])


def test_q404_flavor_question_inside_confirmed_order_is_accepted() -> None:
    event = _event()
    ledger = _ledger(event)

    assert ledger["total_amount"] == 38.0
    assert [tx["event_id"] for tx in ledger["transactions"]] == ["evt_q404_order"]
    assert ledger["transactions"][0]["amount_source"] == "expense_amount"
    # The original quote is preserved; acceptance does not fabricate completion.
    assert ledger["transactions"][0]["quote"] == ORDER_QUOTE
    assert "event_state" not in event
    assert "claim_type" not in event
    assert event["review_status"] == "pending_review"


def test_hypothetical_plan_with_can_bu_neng_stays_excluded() -> None:
    event = _event(
        event_id="evt_q404_plan",
        quote="我想买一份咖喱牛肉蛋炒饭，能不能加个雪球？三十八。",
    )
    ledger = _ledger(event)

    assert ledger["total_amount"] == 0.0
    assert ledger["transactions"] == []
    assert ledger["excluded_events"][0]["reason"] in {
        "planned_or_hypothetical",
        "negated_purchase",
    }


def test_price_question_without_order_action_stays_excluded() -> None:
    event = _event(event_id="evt_q404_price", quote="这个套餐多少钱？三十八。")
    ledger = _ledger(event)

    assert ledger["total_amount"] == 0.0
    assert ledger["transactions"] == []
    assert ledger["excluded_events"]


def test_can_bu_neng_alone_never_qualifies() -> None:
    event = _event(event_id="evt_q404_can_only", quote="这个能不能三十八？")
    ledger = _ledger(event)

    assert ledger["total_amount"] == 0.0
    assert ledger["transactions"] == []
    assert ledger["excluded_events"]


def test_order_phrase_ending_in_question_is_not_a_confirmed_order() -> None:
    event = _event(event_id="evt_q404_order_question", quote="要一份吗？三十八。")
    ledger = _ledger(event)

    assert ledger["total_amount"] == 0.0
    assert ledger["transactions"] == []
    assert ledger["excluded_events"]


def test_non_self_same_order_remains_pending() -> None:
    event = _event(
        event_id="evt_q404_non_self",
        source_label="non_self",
        pending_reason="non_self_review",
    )
    ledger = _ledger(event)

    assert ledger["total_amount"] == 0.0
    assert ledger["transactions"] == []
    # A non-self flavor/question order may be either explicitly excluded by
    # the question/plan guard or held pending by the existing non_self_review
    # signal; neither may enter the ledger.
    assert ledger["pending_count"] == 1 or ledger["excluded_events"]
    if ledger["pending_count"]:
        assert ledger["pending_transactions"][0]["reason"] == "non_self_review"
        assert ledger["pending_transactions"][0]["proposed_amount"] == 38.0


def test_standalone_pos_broadcast_remains_pending() -> None:
    event = _event(
        event_id="evt_q404_pos",
        quote="支付宝到账三元",
        amount=3.0,
        activity_type="pos_payment_broadcast",
        review_status="reviewed",
    )
    ledger = _ledger(event)

    assert ledger["total_amount"] == 0.0
    assert ledger["transactions"] == []
    assert ledger["pending_broadcast_count"] == 1
    assert ledger["pending_broadcast"][0]["reason"] == "pos_broadcast_pending_confirmation"
