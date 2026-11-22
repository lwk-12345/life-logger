"""2026-11-07：活动层消费入账的两个边界。

① 11-06 `act-20261106-frag-499-01#it1`（视频里他人「两块一把」）不该进正式消费账。
② 11-07 `act_store_order_chaofen_01#it1`（店员报「十三块四」、本人「嗯，没有。」）
   不该被静默丢弃，而应进 `pending_transactions`。
③ 问价但未买（本人只说「炒粉多少钱？」）既不入账也不进待确认。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from life_logger.expense_policy import build_expense_ledger  # noqa: E402

RULES_FROM = "2026-11-03"


def _activity(activity_id, items, *, status="completed", title="活动"):
    return {"activity_id": activity_id, "title": title, "status": status, "items": items}


def _item(item_id, action, *, fact_type="none", status="completed", amount=None,
          evidence=(), spoken="2026-11-06T16:35:30+08:00", **extra):
    return {
        "item_id": item_id,
        "action": action,
        "fact_type": fact_type,
        "status": status,
        "amount": amount,
        "currency": None,
        "unit": None,
        "spoken_at": spoken,
        "evidence": [dict(e) for e in evidence],
        **extra,
    }


def _ev(quote, label):
    return {"quote": quote, "source_label": label, "source_id": "src_x", "segment_id": "seg_x"}


def _ledger(activities):
    return build_expense_ledger([], activities=activities, review={},
                                activity_rules_from=RULES_FROM, confirmed={})


def test_non_self_non_expense_video_amount_never_enters_transactions():
    """11-06 视频里「五根，两块一把」→ 不得记成本人 2 元。"""
    activity = _activity("act-20261106-frag-499-01", [_item(
        "it-499-1",
        "说话人一泛叹成年后自己把东西做/买，并说“这个里面五根，五根，两块一把”",
        fact_type="none", status="completed", amount=2,
        evidence=[_ev("就是你小时候你看大人做这些，你把东西做，你买了，这个里面五根，五根，两块一把，"
                      "但是这个稍微应该算一块钱一把。", "non_self")],
    )], title="录音中他人回顾买东西五根两块")

    ledger = _ledger([activity])
    assert ledger["total_amount"] == 0
    assert ledger["transactions"] == []
    assert ledger["pending_transactions"] == []
    assert ledger["pending_count"] == 0


def test_amount_reported_by_clerk_goes_to_pending_not_transactions():
    """11-07 店员报「十三块四」+ 本人「嗯，没有。」→ 待确认，不入已付账。"""
    activity = _activity("act_store_order_chaofen_01", [_item(
        "it_store_01",
        "店内结账，本人确认不再加商品，店员报出总价十三块四",
        fact_type="expense", status="stated", amount=13.4,
        spoken="2026-11-07T03:51:18+08:00",
        evidence=[
            _ev("还有什么要加的吗？", "unknown"),
            _ev("嗯，没有。", "self"),
            _ev("十三块四。", "unknown"),
        ],
    )], status="ongoing", title="录音中疑似：在零食店结账点单并定制一份炒河粉")

    ledger = _ledger([activity])
    assert ledger["total_amount"] == 0, "不能直接算进已付金额"
    assert ledger["transactions"] == []
    assert ledger["pending_count"] == 1
    pending = ledger["pending_transactions"][0]
    # 账本对外把原因放在 `reason`；事件内部字段叫 `activity_pending_reason`
    reason = pending.get("activity_pending_reason") or pending.get("reason")
    assert reason == "amount_from_other_speaker_unconfirmed"
    # 待确认行不把金额写进 amount（避免被当成已付），而是 proposed_amount
    assert pending.get("amount") is None
    assert float(pending["proposed_amount"]) == pytest.approx(13.4)
    assert ledger["pending_by_reason"].get("amount_from_other_speaker_unconfirmed") == 1


def test_price_question_without_confirmation_is_not_counted():
    """本人只问「炒粉多少钱？」且没有确认句 → 不入账、不进待确认。"""
    activity = _activity("act_ask_price_only", [_item(
        "it_ask_01",
        "询问炒粉多少钱",
        fact_type="expense", status="stated", amount=13.4,
        spoken="2026-11-07T03:51:56+08:00",
        evidence=[
            _ev("啊，炒粉多少钱？", "self"),
            _ev("炒粉十三块四。", "unknown"),
        ],
    )], status="ongoing", title="问价")

    ledger = _ledger([activity])
    assert ledger["transactions"] == []
    assert ledger["pending_count"] == 0


def test_self_evidence_purchase_still_counts():
    """对照组：本人亲口说「买了一瓶椰子水，四块五」仍应入账。"""
    activity = _activity("act_buy_coconut", [_item(
        "it_1", "称买了一瓶椰子水，四块五",
        fact_type="expense", status="completed", amount=4.5,
        spoken="2026-11-05T23:06:11+08:00",
        evidence=[_ev("买了一瓶椰子水，四块五。", "self")],
    )])
    ledger = _ledger([activity])
    assert ledger["total_amount"] == pytest.approx(4.5)
    assert ledger["pending_count"] == 0


# ---------------------------------------------------------------- 2026-11-07 二次返工

def test_audit_question_about_a_number_is_not_a_pending_expense():
    """11-06 `act-20261106-win-03#it4`：质问系统「快递费四十一点六从哪里来」≠ 消费。"""
    activity = _activity("act-20261106-win-03", [_item(
        "it4",
        "追问快递费四十一点六从哪里来，称转写没有这个数",
        fact_type="none", status="completed", spoken="2026-11-06T21:10:00+08:00",
        evidence=[_ev("快递费四十一点六从哪里来，转写没有这个数，我是明确的说了。", "self")],
    )], title="核对快递费")
    ledger = _ledger([activity])
    assert ledger["total_amount"] == 0
    assert ledger["transactions"] == []
    assert ledger["pending_count"] == 0


def test_query_with_yixia_and_word_fei_is_not_a_pending_expense():
    """11-07 `act_expense_review_1107_0627#it3`：「看一下是消费的什么」里的「一」不是金额。"""
    activity = _activity("act_expense_review_1107_0627", [_item(
        "it3",
        "要求查看那笔消费是什么、金额多少、有没有统计好",
        fact_type="none", status="asked", spoken="2026-11-07T06:27:00+08:00",
        evidence=[_ev("看一下是消费的什么，金额是多少，有没有统计好", "self")],
    )], status="ongoing", title="查账")
    ledger = _ledger([activity])
    assert ledger["total_amount"] == 0
    assert ledger["transactions"] == []
    assert ledger["pending_count"] == 0


def test_real_spend_with_only_a_fee_word_still_goes_to_pending():
    """正例：本人说「交了物业费一百二」→ 进待确认（不能被查账过滤误杀）。"""
    activity = _activity("act_real_spend", [_item(
        "it1", "交物业费一百二十元", fact_type="none", status="completed",
        spoken="2026-11-07T10:00:00+08:00", evidence=[_ev("交了物业费一百二", "self")],
    )], title="消费")
    ledger = _ledger([activity])
    assert ledger["transactions"] == []
    assert ledger["pending_count"] == 1
    assert ledger["pending_transactions"][0]["reason"] == "activity_spend_unconfirmed"


def test_real_self_purchase_still_books_directly():
    """正例：本人亲口「买了一盒蓝莓十五块」→ 仍按已付直接入账，不能被新规则打掉。"""
    activity = _activity("act_real_buy", [_item(
        "it1", "买蓝莓花了十五块", fact_type="none", status="completed",
        spoken="2026-11-07T10:05:00+08:00", evidence=[_ev("买了一盒蓝莓十五块", "self")],
    )], title="消费")
    ledger = _ledger([activity])
    assert ledger["pending_count"] == 0
    assert len(ledger["transactions"]) == 1
    assert float(ledger["transactions"][0]["amount"]) == pytest.approx(15.0)
