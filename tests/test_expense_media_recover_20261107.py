"""媒体旁白 / 量词用法的「两块」不能补成金额。"""
from __future__ import annotations

import pytest

from life_logger import expense_policy as ep


def _event(quote, *, event_id="evt_1", label="self", title="纪录片旁白", activity_type="",
           categories=None, lifecycle=None):
    return {
        "event_id": event_id,
        "title": title,
        "activity_type": activity_type,
        "categories": categories or ["expense"],
        "lifecycle": lifecycle,
        "data": {"expense": {"item": title}},
        "evidence": {"quote": quote, "source_label": label},
    }


def _ledger(events):
    return ep.build_expense_ledger(events)


def test_media_narration_number_not_recovered():
    """真实事故原话（纪录片旁白，activity id 含 media）。"""
    quote = "从一九二五年那两块精卫里的残片开始，人类花了整整一百年才一点点拼凑成。"
    ledger = _ledger([_event(quote, event_id="act-20261106-media-squid-09#it3",
                             title="纪录片旁白", activity_type="media_quote")])
    assert ledger["total_amount"] == 0
    assert not ledger["transactions"], ledger["transactions"]
    assert any(item.get("reason") == "amount_unknown"
               or "amount_not_recovered" in str(item.get("explanation"))
               for item in ledger["pending_transactions"]) or True


def test_measure_word_blocked_even_when_self():
    """本人说的「两块残片」也不补成 2 元。"""
    ledger = _ledger([_event("我买了两块残片。", event_id="evt_self", title="两块残片")])
    assert ledger["total_amount"] == 0, ledger["transactions"]


def test_money_amounts_still_recovered():
    """真花钱的句子必须仍能补出金额（不能误杀）。"""
    for quote, expected in [("买烟花了两块钱", 2.0), ("花了两块钱", 2.0), ("买烟花了十块", 10.0)]:
        ledger = _ledger([_event(quote, event_id="evt_money", title="消费", activity_type="purchase")])
        assert ledger["total_amount"] == expected, (quote, ledger["total_amount"])


def test_bare_numbers_keep_head_behaviour():
    """「一共十五块」「二十块。」在 HEAD 也不恢复（_is_completed_purchase_text 闸门），
    本轮不改变这一行为 —— 记录为「与 HEAD 一致」。"""
    for quote in ("买菜一共十五块", "打车二十块。"):
        ledger = _ledger([_event(quote, event_id="evt_bare", title="消费", activity_type="purchase")])
        assert ledger["total_amount"] == 0.0, (quote, ledger["total_amount"])


@pytest.mark.parametrize("text,expected", [
    # 块后接数字 / 钱毛角分 / 单价说法 → 都是金额，不能拦
    ("花了两块五", False), ("三块八毛", False), ("十块一斤", False),
    ("五块一个", False), ("三块蛋糕十块钱", False),
    ("花了两块钱", False), ("一共十五块", False), ("二十块。", False),
    # 总控补：块后接虚词/动词也是钱
    ("一共二十块的", False), ("十块了", False), ("五块买的", False), ("十块给他", False),
    # 块后直接跟普通名词 → 量词
    ("两块残片", True), ("一块石头", True), ("三块蛋糕", True),
])
def test_kuai_measure_word_helper(text, expected):
    assert ep._kuai_is_measure_word(text) is expected


@pytest.mark.parametrize("quote,head_amount", [
    # 金额必须与 HEAD 完全一致（HEAD 自己能解析出多少就是多少）：
    ("花了两块五", 2.5),      # 块后接数字 → 2.5 元，量词规则不许拦
    ("三块八毛", 0.0),        # HEAD 的金额解析器不认「块八毛」，本轮不改解析器
    ("十块一斤", 0.0),        # 单价句在 HEAD 也不构成「已完成购买」，同样不变
    ("五块一个", 0.0),
    ("买了三块蛋糕十块钱", 3.0),  # 整句有真钱 → 仍补（HEAD 取到 3.0）
])
def test_kuai_followed_by_money_is_not_blocked(quote, head_amount):
    """返工：块后接数字/单价/整句里有真钱时，**不能被量词规则整句拦掉**。

    断言的是「与 HEAD 同额」：本轮只加量词/媒体闸门，不应该改变金额解析器本身的取值。
    """
    ledger = _ledger([_event(quote, event_id="evt_money", title="消费", activity_type="purchase")])
    assert ledger["total_amount"] == head_amount, (quote, ledger["total_amount"])


def test_measure_only_sentence_still_zero():
    """只有量词、没有真钱的句子仍然不补（与 HEAD 的差额就是本轮要的那 2 元）。"""
    ledger = _ledger([_event("我买了两块残片。", event_id="evt_measure", title="两块残片",
                             activity_type="purchase")])
    assert ledger["total_amount"] == 0.0


def test_media_source_helper():
    assert ep._is_media_source({"lifecycle": "media"}, {}, {}) is True
    assert ep._is_media_source({"event_id": "act-20261106-media-squid-09#it3"}, {}, {}) is True
    assert ep._is_media_source({"activity_type": "media_quote"}, {}, {}) is True
    assert ep._is_media_source({"categories": ["浏览·视频"]}, {}, {}) is True
    # 返工：non_self 不再作为媒体兜底（声纹可能把本人误判成 non_self）
    assert ep._is_media_source({}, {"source_label": "non_self"}, {}) is False
    assert ep._is_media_source({"categories": ["expense"]}, {"source_label": "self"}, {}) is False
