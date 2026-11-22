"""Regression tests for the conservative expense/nutrition ledger policy.

Fixtures are minimal copies of real 2026-10-12 / 2026-10-14 events read from
``output/local-web``.  They keep the original quotes, reported times and
provenance ids so the ledger decisions are reproducible without touching user
data.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from life_logger.daily_cognitive_session import build_nutrition_ledger
from life_logger.expense_policy import build_expense_ledger as policy_expense_ledger


def _event(
    event_id,
    *,
    title,
    quote,
    amount=None,
    currency="CNY",
    item=None,
    reported_at,
    activity_type="purchase",
    categories=("expense",),
    evidence_extra=None,
    recording_id="rec_test",
    source_id=None,
    proposed_amount=None,
):
    evidence = {
        "recording_id": recording_id,
        "segment_id": f"{recording_id}_seg_{event_id}",
        "source_id": source_id,
        "reported_at": reported_at,
        "quote": quote,
        "event_state": "completed",
        "claim_type": "event",
        "temporal_relation": "past",
    }
    if proposed_amount is not None:
        evidence["proposed_expense_amount"] = proposed_amount
    if evidence_extra:
        evidence.update(evidence_extra)
    return {
        "event_id": event_id,
        "categories": list(categories),
        "activity_type": activity_type,
        "title": title,
        "time": {"absolute": reported_at, "precision": "exact"},
        "data": {"expense": {"amount": amount, "currency": currency, "item": item}},
        "evidence": evidence,
        "review_status": "unreviewed",
    }


def _food_event(
    event_id,
    *,
    title,
    quote,
    reported_at,
    activity_type="meal",
    food=None,
    categories=("food",),
):
    return {
        "event_id": event_id,
        "categories": list(categories),
        "activity_type": activity_type,
        "title": title,
        "time": {"absolute": reported_at, "precision": "exact"},
        "data": {"food": food or {}},
        "evidence": {
            "recording_id": "rec_food_test",
            "reported_at": reported_at,
            "quote": quote,
            "event_state": "completed",
            "claim_type": "event",
        },
    }


def _real_1014_events():
    """Minimal copies of the real 10-14 live/formal expense records."""
    guiyuan_quote = "记录一下，晚上买了一包桂圆二十四，买了一瓶这个"
    tea_quote = "维他命柠檬茶，两块六"
    return [
        # Formal 桂圆: upstream amount is null, proposed amount 24.
        _event(
            "evt_ctx_7734eff2e1637bd6",
            title="便签：购入一包桂圆，24元",
            quote=guiyuan_quote,
            amount=None,
            item="桂圆一包",
            reported_at="2026-10-14T15:28:26.550000+00:00",
            proposed_amount=24,
            recording_id="rec_3511233ac4a7",
            source_id="src_bb76256d7251772f2c17",
        ),
        # Live 桂圆 duplicate: same quote/minute, different event id.
        _event(
            "evt_ctx_1cecc53832045c9e",
            title="便签：晚上买一包桂圆，24元",
            quote=guiyuan_quote,
            amount=None,
            item="桂圆",
            reported_at="2026-10-14T15:28:26.550000+00:00",
            proposed_amount=24,
            recording_id="watch_08D349110F5B413A899E173E3E146B88",
            source_id="src_46032e8e4834fc148935",
        ),
        _event(
            "evt_ctx_36f2f4d069033542",
            title="购入维他命柠檬茶一瓶，2.6元",
            quote=tea_quote,
            amount=2.6,
            item="维他命柠檬茶一瓶",
            reported_at="2026-10-14T15:28:33.340000+00:00",
            recording_id="rec_3511233ac4a7",
            source_id="src_372293bee0fc0720f7df",
        ),
        _event(
            "evt_ctx_61aae843cdcf7ac7",
            title="买维他命柠檬茶，2.6元",
            quote=tea_quote,
            amount=2.6,
            item="维他命柠檬茶",
            reported_at="2026-10-14T15:28:33.340000+00:00",
            recording_id="watch_08D349110F5B413A899E173E3E146B88",
            source_id="src_c20ca3ccd10925e2091a",
        ),
        # Real summary quote has no 合计 keyword; the title carries it.
        _event(
            "evt_ctx_4231aef634c9fcf7",
            title="本次采买合计26.6元（桂圆24元+柠檬茶2.6元）",
            quote="二十六块六",
            amount=26.6,
            item="桂圆+维他命柠檬茶合计",
            reported_at="2026-10-14T15:28:37.240000+00:00",
            recording_id="rec_3511233ac4a7",
            source_id="src_4655c49ee9021bc4e6f3",
            evidence_extra={"claim_type": "observation"},
        ),
        # Real USD conflict: data says CNY, quote explicitly says 美元.
        _event(
            "evt_ctx_7ebab726a1df42f5",
            title="花费 7 美元购买 OpenDesign 订阅",
            quote="花费七美元。",
            amount=7,
            currency="CNY",
            item="OpenDesign 订阅（首月）",
            reported_at="2026-10-14T10:27:49.877000+00:00",
            recording_id="watch_33BC16CAD90141448F7BDDF33E654F44",
            source_id="src_3a5f7698dc0a28cc2be8",
            evidence_extra={"temporal_relation": "at_speech"},
        ),
        # Real withdrawal: must not be consumption.
        _event(
            "evt_ctx_2f8d079ab1627284",
            title="提现10元",
            quote="提个10块钱就行了 提现",
            amount=10,
            item="提现",
            reported_at="2026-10-12T02:28:28.880000+00:00",
            activity_type="withdrawal",
            recording_id="watch_D4A1A35CC987444AB86DD64B0E34411C",
            source_id="src_fea9f51484cac24419a5",
        ),
    ]


def test_real_1014_ledger_reproduces_266_cny_and_7_usd():
    ledger = policy_expense_ledger(_real_1014_events())

    assert ledger["totals_by_currency"] == {"CNY": 26.6, "USD": 7.0}
    assert ledger["total_amount"] == 26.6
    assert ledger["currency"] == "CNY"
    assert ledger["category_totals_by_currency"]["CNY"]["food"] == 24.0
    assert ledger["category_totals_by_currency"]["CNY"]["beverage"] == 2.6
    assert ledger["withdrawal_total_by_currency"] == {"CNY": 10.0}
    assert ledger["pending_count"] == 0
    assert ledger["duplicates_removed_count"] == 2
    assert len(ledger["aggregate_reconciliations"]) == 1
    reconciliation = ledger["aggregate_reconciliations"][0]
    assert reconciliation["amount"] == 26.6
    assert set(reconciliation["detail_event_ids"]) == {
        "evt_ctx_7734eff2e1637bd6",
        "evt_ctx_36f2f4d069033542",
    }

    by_id = {tx["event_id"]: tx for tx in ledger["transactions"]}
    all_by_id = {tx["event_id"]: tx for tx in ledger["all_transactions"]}
    assert by_id["evt_ctx_7734eff2e1637bd6"]["amount"] == 24.0
    assert by_id["evt_ctx_7734eff2e1637bd6"]["amount_source"] == "recovered_quote"
    assert by_id["evt_ctx_7ebab726a1df42f5"]["currency"] == "USD"
    assert by_id["evt_ctx_7ebab726a1df42f5"]["currency_conflict"] is True
    assert all_by_id["evt_ctx_2f8d079ab1627284"]["kind"] == "withdrawal"
    assert "evt_ctx_2f8d079ab1627284" not in by_id
    # The summary row is not counted as a third purchase.
    assert "evt_ctx_4231aef634c9fcf7" not in by_id


def test_plans_negation_questions_and_withdrawals_are_not_completed_facts():
    events = [
        _event(
            "evt_plan",
            title="我快被气死了，我等会儿想去买烟抽一下。",
            quote="我快被气死了，我等会儿想去买烟抽一下。",
            amount=None,
            item=None,
            reported_at="2026-10-13T16:53:00.541000+00:00",
            evidence_extra={"event_state": None, "claim_type": None},
        ),
        _event(
            "evt_negated",
            title="现在主食还有三份面,目前主食都先不买了",
            quote="现在主食还有三份面,目前主食都先不买了。",
            amount=None,
            item="了",
            reported_at="2026-10-13T15:21:22.990000+00:00",
            activity_type="decision_saving",
            evidence_extra={"event_state": "negated", "claim_type": None},
        ),
        _event(
            "evt_question",
            title="这个多少钱？",
            quote="这个多少钱？",
            amount=None,
            item=None,
            reported_at="2026-10-13T15:21:30.000000+00:00",
            evidence_extra={"event_state": None, "claim_type": "question"},
        ),
        _event(
            "evt_paid_transfer",
            title="转账给便利店 5 元",
            quote="转给便利店五块钱。",
            amount=5,
            item="转账",
            reported_at="2026-10-13T15:22:00.000000+00:00",
            activity_type="transfer",
        ),
        _event(
            "evt_withdrawal",
            title="提现10元",
            quote="提个10块钱就行了 提现",
            amount=10,
            item="提现",
            reported_at="2026-10-13T15:23:00.000000+00:00",
            activity_type="withdrawal",
        ),
    ]

    ledger = policy_expense_ledger(events)
    assert {tx["event_id"] for tx in ledger["transactions"]} == {"evt_paid_transfer"}
    assert {tx["event_id"] for tx in ledger["non_payment_transactions"]} == {
        "evt_withdrawal"
    }
    assert {tx["event_id"] for tx in ledger["all_transactions"]} == {
        "evt_paid_transfer",
        "evt_withdrawal",
    }
    assert ledger["totals_by_currency"] == {"CNY": 5.0}
    assert ledger["transfer_total_by_currency"] == {"CNY": 5.0}
    assert ledger["withdrawal_total_by_currency"] == {"CNY": 10.0}
    assert {item["reason"] for item in ledger["excluded_events"]} == {
        "planned_or_hypothetical",
        "explicit_non_fact_state:negated",
        "explicit_non_fact_state:question",
    }


def test_unknown_amounts_stay_pending_and_are_never_zero():
    events = [
        _event(
            "evt_pending",
            title="这一块。",
            quote="这一块。",
            amount=None,
            item=None,
            reported_at="2026-10-12T15:55:16.148000+00:00",
        ),
        _event(
            "evt_nan",
            title="买了一包烟，金额异常",
            quote="买了一包烟，金额异常。",
            amount=float("nan"),
            item="烟",
            reported_at="2026-10-12T16:00:00.000000+00:00",
        ),
        _event(
            "evt_inf",
            title="买了一瓶水，金额异常",
            quote="买了一瓶水，金额异常。",
            amount=float("inf"),
            item="水",
            reported_at="2026-10-12T16:01:00.000000+00:00",
        ),
        _event(
            "evt_zero",
            title="买了一瓶水，零元",
            quote="买了一瓶水，零元。",
            amount=0,
            item="水",
            reported_at="2026-10-12T16:02:00.000000+00:00",
        ),
    ]

    ledger = policy_expense_ledger(events)
    assert ledger["transactions"] == []
    assert ledger["totals_by_currency"] == {}
    assert ledger["pending_count"] == 4
    reasons = {
        item["event_id"]: item["reason"] for item in ledger["pending_transactions"]
    }
    assert reasons["evt_pending"] == "amount_unknown"
    assert reasons["evt_nan"] == "invalid_amount"
    assert reasons["evt_inf"] == "invalid_amount"
    assert reasons["evt_zero"] == "non_positive_amount"
    assert all(item["amount"] is None for item in ledger["pending_transactions"])
    assert ledger["totals_partial"] is True


def test_null_amount_uses_validated_quote_not_title():
    events = [
        _event(
            "evt_recover",
            title="便签：购入一包桂圆，24元",
            quote="记录一下，晚上买了一包桂圆二十四，买了一瓶这个",
            amount=None,
            item="桂圆一包",
            reported_at="2026-10-14T15:28:26.550000+00:00",
            proposed_amount=24,
        ),
        _event(
            "evt_title_only",
            title="合计24元",
            quote="买了一点东西。",
            amount=None,
            item="不明",
            reported_at="2026-10-14T15:29:26.550000+00:00",
            proposed_amount=24,
        ),
    ]

    ledger = policy_expense_ledger(events)
    by_id = {tx["event_id"]: tx for tx in ledger["transactions"]}
    assert by_id["evt_recover"]["amount"] == 24.0
    assert by_id["evt_recover"]["amount_source"] == "recovered_quote"
    assert "evt_title_only" not in by_id
    pending = {item["event_id"]: item for item in ledger["pending_transactions"]}
    assert pending["evt_title_only"]["reason"] == "amount_unknown"
    assert pending["evt_title_only"]["proposed_amount"] == 24.0


def test_bare_dollar_is_not_assumed_to_be_usd():
    event = _event(
        "evt_bare_dollar",
        title="买了一个小玩意",
        quote="花了$5。",
        amount=5,
        currency=None,
        item="小玩意",
        reported_at="2026-10-14T12:00:00.000000+00:00",
    )
    event["data"]["expense"]["currency"] = None

    ledger = policy_expense_ledger([event])
    assert ledger["totals_by_currency"] == {}
    assert ledger["pending_count"] == 1
    pending = ledger["pending_transactions"][0]
    assert pending["reason"] == "currency_unknown"
    assert pending["currency"] == "UNKNOWN"
    assert pending["amount"] == 5.0


def test_same_quote_same_minute_duplicate_collapses_but_different_minutes_stay():
    quote = "买了一瓶水，两块钱。"
    events = [
        _event(
            "evt_a",
            title="购买矿泉水",
            quote=quote,
            amount=2,
            item="水",
            reported_at="2026-10-14T10:00:10.000000+00:00",
        ),
        _event(
            "evt_b",
            title="购买矿泉水（live 重复）",
            quote=quote,
            amount=2,
            item="水",
            reported_at="2026-10-14T10:00:10.000000+00:00",
        ),
        _event(
            "evt_c",
            title="购买矿泉水（真正第二次）",
            quote=quote,
            amount=2,
            item="水",
            reported_at="2026-10-14T10:01:10.000000+00:00",
        ),
    ]

    ledger = policy_expense_ledger(events)
    assert ledger["transaction_count"] == 2
    assert ledger["duplicates_removed_count"] == 1
    assert ledger["totals_by_currency"] == {"CNY": 4.0}
    kept = {tx["event_id"]: tx for tx in ledger["transactions"]}
    assert "evt_b" not in kept
    assert kept["evt_a"]["duplicate_event_ids"] == ["evt_b"]
    assert "evt_c" in kept


def test_aggregate_reconciliation_keeps_unrelated_purchases_and_rejects_far_sums():
    events = [
        _event(
            "evt_gy",
            title="购入一包桂圆，24元",
            quote="记录一下，晚上买了一包桂圆二十四",
            amount=24,
            item="桂圆",
            reported_at="2026-10-14T15:28:26.550000+00:00",
        ),
        _event(
            "evt_tea",
            title="购入维他命柠檬茶一瓶，2.6元",
            quote="维他命柠檬茶，两块六",
            amount=2.6,
            item="维他命柠檬茶",
            reported_at="2026-10-14T15:28:33.340000+00:00",
        ),
        _event(
            "evt_summary",
            title="本次采买合计26.6元（桂圆24元+柠檬茶2.6元）",
            quote="二十六块六",
            amount=26.6,
            item="桂圆+维他命柠檬茶合计",
            reported_at="2026-10-14T15:28:37.240000+00:00",
        ),
        # A real unrelated 26.6 purchase, far away and with no matching items.
        _event(
            "evt_unrelated",
            title="购买耳机，26.6元",
            quote="买了个耳机二十六块六",
            amount=26.6,
            item="耳机",
            reported_at="2026-10-14T20:00:00.000000+00:00",
        ),
        # Two far-away items summing to 26.6 must not reconcile a summary.
        _event(
            "evt_far_a",
            title="购买咖啡，24元",
            quote="买了杯咖啡二十四块",
            amount=24,
            item="咖啡",
            reported_at="2026-10-14T09:00:00.000000+00:00",
        ),
        _event(
            "evt_far_b",
            title="购买面包，2.6元",
            quote="买了个面包两块六",
            amount=2.6,
            item="面包",
            reported_at="2026-10-14T09:05:00.000000+00:00",
        ),
        _event(
            "evt_far_summary",
            title="本次采买合计26.6元",
            quote="二十六块六",
            amount=26.6,
            item="合计",
            reported_at="2026-10-14T09:20:00.000000+00:00",
        ),
    ]

    ledger = policy_expense_ledger(events)
    by_id = {tx["event_id"]: tx for tx in ledger["transactions"]}
    assert "evt_summary" not in by_id
    assert "evt_unrelated" in by_id
    assert "evt_far_summary" in by_id
    assert ledger["totals_by_currency"] == {"CNY": 106.4}
    assert len(ledger["aggregate_reconciliations"]) == 1
    assert len(ledger["unreconciled_aggregates"]) == 1


def test_nutrition_preserves_unknown_vs_zero_and_purchase_is_not_intake():
    events = [
        _food_event(
            "evt_zero",
            title="喝了一杯白水",
            quote="喝了一杯白水。",
            reported_at="2026-10-14T09:00:00.000000+00:00",
            activity_type="drink",
            food={
                "item": "白水",
                "meal": "snack",
                "estimated_calories": 0,
                "nutrients": {
                    "energy_kcal": 0,
                    "carbohydrates_g": 0,
                    "protein_g": 0,
                    "fat_g": 0,
                    "sodium_mg": 0,
                },
            },
        ),
        _food_event(
            "evt_unknown",
            title="吃了点东西",
            quote="吃了点东西。",
            reported_at="2026-10-14T12:00:00.000000+00:00",
            food={
                "item": "不明食物",
                "meal": "lunch",
                "estimated_calories": None,
                "nutrients": {
                    "energy_kcal": None,
                    "carbohydrates_g": None,
                    "protein_g": None,
                    "fat_g": None,
                    "sodium_mg": None,
                },
            },
        ),
        _food_event(
            "evt_purchase",
            title="购买两个冰杯",
            quote="购买两个冰杯。",
            reported_at="2026-10-14T13:00:00.000000+00:00",
            activity_type="purchase",
            food={
                "item": "冰杯",
                "estimated_calories": 0,
                "nutrients": {"energy_kcal": 0, "carbohydrates_g": 0},
            },
        ),
    ]

    ledger = build_nutrition_ledger(events)
    by_id = {record["event_id"]: record for record in ledger["food_records"]}
    assert by_id["evt_zero"]["calories_kcal"] == 0.0
    assert by_id["evt_zero"]["calories_known"] is True
    assert by_id["evt_unknown"]["calories_kcal"] is None
    assert by_id["evt_unknown"]["calories_known"] is False
    assert by_id["evt_purchase"]["record_type"] == "purchase"

    assert ledger["meals"]["lunch"][0]["event_id"] == "evt_unknown"
    assert ledger["meals"]["snacks"][0]["event_id"] == "evt_zero"
    assert [record["event_id"] for record in ledger["purchases"]] == ["evt_purchase"]
    assert ledger["consumed_record_count"] == 2
    assert ledger["purchase_record_count"] == 1
    assert ledger["record_count"] == 3
    assert ledger["estimated_count"] == 1
    assert ledger["unknown_nutrition_count"] == 1
    assert ledger["zero_calorie_count"] == 1
    assert ledger["calorie_coverage"] == 0.5
    assert ledger["totals"]["calories_kcal"] == 0.0
    assert ledger["totals_complete"] is False
    assert [item["event_id"] for item in ledger["unknown_nutrition_items"]] == [
        "evt_unknown"
    ]
    # Purchase-only ice cups never become a consumed meal.
    assert all(
        record["event_id"] != "evt_purchase"
        for meal_records in ledger["meals"].values()
        for record in meal_records
    )


def test_money_uses_decimal_and_rejects_non_finite_values():
    events = [
        _event(
            "evt_01",
            title="买了一支笔",
            quote="买了一支笔，一毛钱。",
            amount=0.1,
            item="笔",
            reported_at="2026-10-14T08:00:00.000000+00:00",
        ),
        _event(
            "evt_02",
            title="买了一个本子",
            quote="买了一个本子，两毛钱。",
            amount=0.2,
            item="本子",
            reported_at="2026-10-14T08:01:00.000000+00:00",
        ),
    ]
    ledger = policy_expense_ledger(events)
    assert ledger["totals_by_currency"] == {"CNY": 0.3}
    assert all(math.isfinite(tx["amount"]) for tx in ledger["transactions"])


def test_quote_like_live_item_does_not_block_formal_duplicate_collapse():
    quote = "刚才买了两瓶杯两块钱。"
    events = [
        _event(
            "evt_live_raw_item",
            title="live 原始物名",
            quote=quote,
            amount=None,
            item="两瓶杯两块钱",
            reported_at="2026-10-12T15:24:05.365000+00:00",
            proposed_amount=2,
            recording_id="watch_6404EF4EF8A242E1B57C69C3678A78F2",
        ),
        _event(
            "evt_formal_item",
            title="正式物名",
            quote=quote,
            amount=2,
            item="冰杯",
            reported_at="2026-10-12T15:24:05.365000+00:00",
            recording_id="rec_formal",
        ),
    ]

    ledger = policy_expense_ledger(events)
    assert ledger["transaction_count"] == 1
    assert ledger["total_amount"] == 2.0
    assert ledger["duplicates_removed_count"] == 1


def test_real_1012_frozen_fixture_separates_withdrawal_and_keeps_pending():
    fixture = (
        Path(__file__).parent
        / "fixtures"
        / "audit_expenses_20261017"
        / "2026-10-12.json"
    )
    events = json.loads(fixture.read_text(encoding="utf-8"))

    ledger = policy_expense_ledger(events)
    assert ledger["totals_by_currency"] == {"CNY": 6.0}
    assert ledger["withdrawal_total_by_currency"] == {"CNY": 10.0}
    assert ledger["transaction_count"] == 3
    assert ledger["pending_count"] == 8
    assert ledger["excluded_count"] == 5
    assert ledger["duplicates_removed_count"] == 3


def test_transfer_kinds_separate_account_moves_topups_and_verified_purchases():
    events = [
        _event(
            "evt_self_transfer",
            title="转账100元到自己的另一个账户",
            quote="转账100元到自己的另一个账户。",
            amount=100,
            item="转账",
            reported_at="2026-10-14T10:00:00.000000+00:00",
            activity_type="transfer",
        ),
        _event(
            "evt_topup",
            title="充值100元到微信零钱",
            quote="充值100元到微信零钱。",
            amount=100,
            item="充值",
            reported_at="2026-10-14T10:01:00.000000+00:00",
            activity_type="transfer",
        ),
        _event(
            "evt_person_transfer",
            title="转账给小明100元",
            quote="转账给小明100元。",
            amount=100,
            item="转账",
            reported_at="2026-10-14T10:02:00.000000+00:00",
            activity_type="transfer",
        ),
        _event(
            "evt_merchant_transfer",
            title="给店家转账24元买桂圆",
            quote="给店家转账24元买桂圆。",
            amount=24,
            item="桂圆",
            reported_at="2026-10-14T10:03:00.000000+00:00",
            activity_type="transfer",
        ),
    ]

    ledger = policy_expense_ledger(events)
    assert ledger["totals_by_currency"] == {"CNY": 24.0}
    assert ledger["transfer_total_by_currency"] == {"CNY": 24.0}
    assert ledger["nonpayment_total_by_currency"] == {"CNY": 300.0}
    assert ledger["nonpayment_kind_totals_by_currency"] == {
        "account_transfer": {"CNY": 100.0},
        "person_transfer": {"CNY": 100.0},
        "topup": {"CNY": 100.0},
    }
    assert ledger["payment_kind_totals_by_currency"] == {"transfer": {"CNY": 24.0}}

    all_by_id = {tx["event_id"]: tx for tx in ledger["all_transactions"]}
    assert all_by_id["evt_self_transfer"]["kind"] == "account_transfer"
    assert all_by_id["evt_topup"]["kind"] == "topup"
    assert all_by_id["evt_person_transfer"]["kind"] == "person_transfer"
    assert all_by_id["evt_merchant_transfer"]["kind"] == "transfer"
    assert all_by_id["evt_merchant_transfer"]["category"] == "food"
    assert {tx["event_id"] for tx in ledger["transactions"]} == {
        "evt_merchant_transfer"
    }
    assert ledger["nonpayment_transaction_count"] == 3
    assert ledger["pending_count"] == 0


def test_ambiguous_summary_is_pending_instead_of_double_counting_details():
    events = [
        _event(
            "evt_gy",
            title="购入桂圆，24元",
            quote="买了一包桂圆二十四。",
            amount=24,
            item="桂圆",
            reported_at="2026-10-14T11:59:50.000000+00:00",
        ),
        _event(
            "evt_tea",
            title="购入柠檬茶，2.6元",
            quote="维他命柠檬茶，两块六。",
            amount=2.6,
            item="维他命柠檬茶",
            reported_at="2026-10-14T11:59:55.000000+00:00",
        ),
        _event(
            "evt_coffee",
            title="购入咖啡，13元",
            quote="买了一杯咖啡十三块。",
            amount=13,
            item="咖啡",
            reported_at="2026-10-14T11:59:52.000000+00:00",
        ),
        _event(
            "evt_bread",
            title="购入面包，13.6元",
            quote="买了个面包十三块六。",
            amount=13.6,
            item="面包",
            reported_at="2026-10-14T11:59:57.000000+00:00",
        ),
        _event(
            "evt_summary",
            title="本次采买合计26.6元（桂圆+柠檬茶+咖啡+面包）",
            quote="二十六块六",
            amount=26.6,
            item="桂圆+柠檬茶+咖啡+面包合计",
            reported_at="2026-10-14T12:00:00.000000+00:00",
        ),
        # A real unrelated purchase at another time is not a summary.
        _event(
            "evt_unrelated",
            title="购买耳机，26.6元",
            quote="买了个耳机二十六块六。",
            amount=26.6,
            item="耳机",
            reported_at="2026-10-14T20:00:00.000000+00:00",
        ),
    ]

    ledger = policy_expense_ledger(events)
    by_id = {tx["event_id"]: tx for tx in ledger["transactions"]}
    assert "evt_summary" not in by_id
    assert "evt_unrelated" in by_id
    # Details and the explicit unrelated purchase remain; the ambiguous
    # summary is withheld rather than added on top.
    assert ledger["totals_by_currency"] == {"CNY": 79.8}
    assert ledger["pending_count"] == 1
    assert ledger["pending_by_reason"] == {"aggregate_reconciliation_ambiguous": 1}
    assert ledger["totals_partial"] is True
    assert len(ledger["aggregate_reconciliation_conflicts"]) == 1
    conflict = ledger["aggregate_reconciliation_conflicts"][0]
    assert conflict["aggregate_event_id"] == "evt_summary"
    assert set(conflict["nearby_detail_event_ids"]) == {
        "evt_gy",
        "evt_tea",
        "evt_coffee",
        "evt_bread",
    }
    assert ledger["aggregate_reconciliations"] == []
