import pytest

from life_logger.fact_types import FACT_TYPES, has_self_evidence, item_fact_type


def _item(action, quote="", status="已陈述", amount=None, label="self"):
    return {"action": action, "status": status, "amount": amount,
            "evidence": [{"quote": quote, "source_label": label}]}


@pytest.mark.parametrize("item,expected", [
    (_item("陈述一桶水并花了四块", "一桶水，然后花了四块", amount=4), "expense"),
    (_item("吃了一碗瘦肉粉", "早餐吃了瘦肉粉"), "food"),
    (_item("做卧推三组", "卧推做了三组"), "exercise"),
    (_item("抽了一根烟", "刚抽了一根烟"), "smoke"),
    (_item("提醒明天取快递", "记得提醒我明天取快递"), "todo"),
    (_item("讲述新的产品点子", "我有个想法"), "idea"),
    (_item("讨论代码结构", "这个函数太长了"), "none"),
])
def test_clear_facts(item, expected):
    assert item_fact_type(item) == expected


@pytest.mark.parametrize("item", [
    _item("想买一瓶水", "等会儿想买水", amount=3),
    _item("询价", "这个多少钱", amount=5),
    _item("计划买鞋花费两百", "明天买鞋花费两百", status="planned", amount=200),
])
def test_intent_and_plans_are_not_spending(item):
    assert item_fact_type(item) != "expense"


def test_media_is_never_a_fact():
    item = _item("吃了一碗面", "吃了一碗面")
    assert item_fact_type(item, {"lifecycle": "media"}) == "none"
    assert item_fact_type({**item, "item_lifecycle": "media"}) == "none"


@pytest.mark.parametrize("item", [
    _item("称手机还有百分之三电", "手机还有百分之三电"),
    _item("询问新疆还有啥好吃的", "吃了吗，新疆有啥好吃的"),
    _item("陈述今天比较累、感觉不想健身", "不想练了"),
    _item("吃了一碗面", "吃了一碗面", label="non_self"),
    _item("说话人二以嗯回应", "练了三组"),
])
def test_noise_seen_in_real_days_is_not_a_fact(item):
    assert item_fact_type(item, {"category": "运动·健身"}) == "none"


def test_money_without_a_paid_verb_is_not_expense():
    assert item_fact_type(_item("提到价格", "这个七十", amount=70)) == "none"


def test_result_is_always_a_known_type():
    assert item_fact_type({}) in FACT_TYPES


def test_self_evidence():
    assert has_self_evidence(_item("a", "b"))
    assert not has_self_evidence(_item("a", "b", label="non_self"))
    assert not has_self_evidence({})


def test_model_declared_type_wins_but_media_still_blocks_it():
    item = {**_item("继续", "好"), "fact_type": "food"}
    assert item_fact_type(item) == "food"
    assert item_fact_type(item, {"lifecycle": "media"}) == "none"


def test_unknown_declared_type_falls_back_to_rules():
    assert item_fact_type({**_item("吃了一碗面", "吃了一碗面"), "fact_type": "banana"}) == "food"
