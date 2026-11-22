"""消费漏记：只补待确认，不把候选金额变成已确认消费。"""
from copy import deepcopy
from datetime import date

import pytest

from life_logger.expense_policy import build_expense_ledger


def activity(quote, amount=None, currency=None, *, source="self", status="unknown", day="2026-11-04", **fields):
    item = {"action": "消费记录", "status": status, "amount": amount, "currency": currency,
            "amount_kind": "unknown", "spoken_at": f"{day}T11:45:00+08:00",
            "evidence": [{"quote": quote, "source_label": source}], **fields}
    return {"activity_id": "candidate", "category": "生活·日常", "items": [item]}


def ledger(a):
    return build_expense_ledger([], activities=[a], activity_rules_from=date(2026, 10, 26).isoformat())


@pytest.mark.parametrize("quote,amount", [("谷歌的订阅是四百二十三", 423), ("Cloud的订阅是一百四", 140)])
def test_subscription_without_currency_is_pending(quote, amount):
    a = activity(quote, amount)
    before = deepcopy(a)
    result = ledger(a)
    assert result["total_amount"] == 0
    assert len(result["pending_transactions"]) == 1
    pending = result["pending_transactions"][0]
    assert pending["proposed_amount"] == amount
    assert pending["currency"] == "CNY"
    assert a == before


def test_take_pack_without_amount_is_pending_despite_planned_status():
    result = ledger(activity("拿一包那个红塔山", status="planned", action="计划拿一包红塔山试看"))
    assert result["total_amount"] == 0
    pending = result["pending_transactions"][0]
    assert pending["proposed_amount"] is None
    assert pending["reason"] == "activity_purchase_amount_unspoken"
    assert pending["currency"] == "CNY"


def test_gift_card_keeps_existing_pending():
    result = ledger(activity("买苹果的礼宾卡，七十，他给我减了十块。", 70, "CNY", status="completed"))
    assert result["total_amount"] == 0
    assert len(result["pending_transactions"]) == 1
    assert result["pending_transactions"][0]["proposed_amount"] == 70


def test_completed_water_keeps_existing_confirmed_total():
    result = ledger(activity("一桶水，然后花了四块", 4, day="2026-11-02"))
    assert result["total_amount"] == 4
    assert not result["pending_transactions"]


@pytest.mark.parametrize("quote", ["这个订阅多少钱", "想买订阅四百元", "要买订阅四百元", "打算买订阅四百元", "如果订阅四百元", "订阅四百元吗", "他买了订阅四百元", "朋友的订阅是四百元", "报道称订阅四百元", "拿一包要多少钱", "没买烟", "体重七十公斤", "订阅一个月", "这个三块是吧", "去年七十块钱不抵十块钱", "这个券减十元", "两百块钱买四个", "这种要搞就拆屏幕搞，两百块钱咯", "一美元吗？一美元换十美元", "讨论成本这一块儿", "是五块钱三个", "现在大概两千块钱左右"])
def test_inquiry_plan_others_and_quantities_do_not_become_pending(quote):
    assert not ledger(activity(quote, 400))["pending_transactions"]


@pytest.mark.parametrize("source", ["non_self", "unknown"])
def test_other_speaker_not_pending(source):
    assert not ledger(activity("订阅四百元", 400, source=source))["pending_transactions"]


@pytest.mark.parametrize("field,value", [("category", "浏览·视频"), ("lifecycle", "media")])
def test_media_activity_not_pending(field, value):
    a = activity("订阅四百元", 400)
    a[field] = value
    assert not ledger(a)["pending_transactions"]


def test_media_item_not_pending():
    assert not ledger(activity("订阅四百元", 400, item_lifecycle="media"))["pending_transactions"]


def test_before_cutover_not_pending():
    assert not ledger(activity("订阅四百元", 400, day="2026-10-25"))["pending_transactions"]


def test_split_self_quotes_and_duplicate_event():
    a = activity("Cloud的订阅是", 140)
    a["items"][0]["evidence"].append({"quote": "一百四", "source_label": "self"})
    assert ledger(a)["pending_transactions"][0]["proposed_amount"] == 140
    event = {"event_id": "existing", "categories": ["expense"], "evidence": {"quote": "Cloud的订阅是 一百四"},
             "data": {"expense": {"amount": 140, "currency": "USD"}}}
    result = build_expense_ledger([event], activities=[a], activity_rules_from=date(2026, 10, 26).isoformat())
    assert not result["pending_transactions"]


def test_bought_without_price_is_pending():
    result = ledger(activity("买了三个苹果"))
    assert len(result["pending_transactions"]) == 1
    assert result["pending_transactions"][0]["proposed_amount"] is None


def test_day_facts_entry_adds_pending_without_changing_confirmed(monkeypatch):
    from life_logger import day_facts
    monkeypatch.setattr(day_facts, "activities_for_day", lambda day: [activity("谷歌的订阅是四百二十三", 423)])
    monkeypatch.setattr(day_facts, "load_ledger_review", lambda: {})
    result = day_facts.day_expense_ledger([], "2026-11-04")
    assert result["total_amount"] == 0
    assert result["pending_transactions"][0]["proposed_amount"] == 423
    assert result["pending_transactions"][0]["currency"] == "CNY"


def test_coupon_context_does_not_become_spending():
    a = activity("买五个五十块钱的。", 50)
    a["title"] = "讲解优惠券活动"
    assert not ledger(a)["pending_transactions"]


def test_nonself_amount_must_not_supply_self_subscription_candidate():
    a = activity("Cloud的订阅是", 140)
    a["items"][0]["evidence"].append({"quote": "一百四", "source_label": "non_self"})
    assert not ledger(a)["pending_transactions"]


def test_user_confirmed_amount_counts_and_overrides_transcript():
    activity = {
        "activity_id": "a1", "title": "寄平板",
        "items": [{"action": "快递费", "status": "unknown", "amount": 43, "amount_kind": "money",
                   "spoken_at": "2026-11-04T22:56:21+08:00",
                   "evidence": [{"quote": "快递费四十三", "source_label": "self"}]}],
    }
    pending = build_expense_ledger([], activities=[activity], activity_rules_from="2026-10-26")
    assert pending["total_amount"] == 0 and len(pending["pending_transactions"]) == 1
    done = build_expense_ledger([], activities=[activity], activity_rules_from="2026-10-26",
                                confirmed={"a1#it1": 41.6})
    assert done["total_amount"] == 41.6
    assert done["pending_transactions"] == []
    assert done["transactions"][0]["currency"] == "CNY"


def _broadcast_activity(*, self_item=True, category="生活·日常"):
    items = [{"action": "听到支付播报", "status": "completed", "amount": 4, "currency": "CNY", "amount_kind": "money",
              "spoken_at": "2026-11-04T22:53:38+08:00",
              "evidence": [{"quote": "收钱吧，到账四元。", "source_label": "non_self"}]}]
    if self_item:
        items.append({"action": "确认价格", "status": "unknown", "spoken_at": "2026-11-04T22:53:30+08:00",
                      "evidence": [{"quote": "啊，四块是吧，对。", "source_label": "self"}]})
    return {"activity_id": "b1", "title": "付款", "category": category, "items": items}


def test_payment_broadcast_next_to_own_speech_is_pending_not_counted():
    ledger = build_expense_ledger([], activities=[_broadcast_activity()], activity_rules_from="2026-10-26")
    assert ledger["total_amount"] == 0
    assert [t["proposed_amount"] for t in ledger["pending_broadcast"]] == [4.0]


@pytest.mark.parametrize("activity", [_broadcast_activity(self_item=False), _broadcast_activity(category="浏览·视频")])
def test_payment_broadcast_without_own_speech_or_in_media_is_ignored(activity):
    ledger = build_expense_ledger([], activities=[activity], activity_rules_from="2026-10-26")
    # 旧播报逻辑可能另行归桶，这里只保证本规则不把它算成“现场付款”，也绝不计入合计。
    assert ledger["total_amount"] == 0
    assert all(t["reason"] != "activity_payment_broadcast_unconfirmed" for t in ledger["pending_broadcast"])


def test_confirmed_broadcast_counts_and_is_not_duplicated_by_activity():
    event = {"event_id": "evt_pos", "title": "商户播报到账四元", "categories": ["expense"],
             "activity_type": "pos_payment_broadcast", "data": {"expense": {"amount": 4, "currency": "CNY"}},
             "evidence": {"quote": "收钱吧，到账四元。", "source_label": "non_self"},
             "time": {"absolute": "2026-11-04T22:53:38+08:00"}}
    pending = build_expense_ledger([event], activities=[_broadcast_activity()], activity_rules_from="2026-10-26")
    assert pending["total_amount"] == 0 and len(pending["pending_broadcast"]) == 1
    done = build_expense_ledger([event], activities=[_broadcast_activity()], activity_rules_from="2026-10-26",
                                confirmed={"evt_pos": 4.0})
    assert done["total_amount"] == 4
    assert done["pending_broadcast"] == []


def _same_sentence_activity(day):
    return [{
        "activity_id": "act_x", "title": "买东西", "category": "生活·日常",
        "occurrence_start": f"{day}T12:49:00+08:00",
        "items": [{
            "item_id": "it3", "action": "去买菜，买菜花了十六", "amount": 16, "status": "completed",
            "fact_type": "expense", "spoken_at": f"{day}T12:49:21+08:00",
            "evidence": [{"quote": "买冰杯花了两块，买菜花了", "source_label": "self"}, {"quote": "十六", "source_label": "self"}],
        }],
    }]


def _sentence_event(day):
    return {
        "event_id": "evt_ice", "title": "买冰杯花费2元", "categories": ["expense"],
        "time": {"absolute": f"{day}T12:49:21+08:00"}, "effective_date": day,
        "evidence": {"quote": "买冰杯花了两块，买菜花了"},
        "data": {"expense": {"amount": 2.0, "currency": "CNY", "item": "冰杯", "transaction_evidence": "花了两块", "amount_evidence": "两块"}},
    }


def test_second_purchase_in_same_sentence_is_not_a_duplicate():
    day = "2026-11-05"
    ledger = build_expense_ledger([_sentence_event(day)], activities=_same_sentence_activity(day), activity_rules_from="2026-10-26")
    assert sorted(t["amount"] for t in ledger["transactions"]) == [2.0, 16.0]


def test_same_amount_in_same_sentence_still_counts_once():
    day = "2026-11-05"
    activity = _same_sentence_activity(day)
    activity[0]["items"][0]["amount"] = 2
    ledger = build_expense_ledger([_sentence_event(day)], activities=activity, activity_rules_from="2026-10-26")
    assert [t["amount"] for t in ledger["transactions"]] == [2.0]


def test_frozen_days_keep_old_quote_dedupe():
    day = "2026-11-04"
    ledger = build_expense_ledger([_sentence_event(day)], activities=_same_sentence_activity(day), activity_rules_from="2026-10-26")
    assert [t["amount"] for t in ledger["transactions"]] == [2.0]
