"""吸烟账按事实分类：买烟 / 念头 不进次数（2026-11-06）。

三条真实数据的形状（脱敏后逐字保留 title/quote/结构化字段）：
  * 10-26 evt_session_9ea2f06412f7f4ba「购买紫云香烟和打火机」→ purchase
  * 11-02 evt_ctx_94d683aafd4440f8「当下仍有抽烟的念头」     → urge
  * 11-05 act_20261105_1833_smoke_gym_plan「刚抽了一根烟」     → smoke
"""
from __future__ import annotations

from life_logger.daily_cognitive_session import (
    SMOKING_FACT_ACTIONS,
    build_smoking_ledger,
    classify_smoking_event,
)

PURCHASE_EVENT = {
    "event_id": "evt_session_9ea2f06412f7f4ba",
    "activity_type": "general",
    "categories": ["expense"],
    "title": "购买紫云香烟和打火机",
    "data": {"expense": {"amount": None, "currency": "CNY",
                         "note": "紫云硬盒香烟及打火机，金额未提及"}},
    "evidence": {"quote": "拿包紫云……硬盒的……另外你们打火机"},
}

CRAVING_EVENT = {
    "event_id": "evt_ctx_94d683aafd4440f8",
    "activity_type": "state",
    "categories": ["routine"],
    "title": "当下仍有抽烟的念头（自我陈述的吸烟渴望）",
    "time": {"clock": "13:09"},
    "data": {"routine": {"state": "想抽烟", "behavior": "吸烟渴望",
                         "note": "仅记录本人原话表达的当下念头，未提及是否已吸烟"}},
    "evidence": {"quote": "现在还想抽烟", "source_label": "self"},
}


def _activity_smoke_event(**over) -> dict:
    event = {
        "event_id": "activity:2026-11-05:act_20261105_1833_smoke_gym_plan:it_1833_smoke_1",
        "title": "自述刚抽了一根烟",
        "categories": ["smoking"],
        "activity_type": "smoking",
        "data": {"smoking": {"action": "smoke", "count": None}},
        "_activity_source": {"origin": "activity", "activity_id": "act_20261105_1833_smoke_gym_plan",
                             "item_id": "it_1833_smoke_1", "fact_type": "smoke"},
        "evidence": {"quote": "刚抽了一根烟，现在去健身房"},
    }
    event.update(over)
    return event


# --- 三条真实数据 ---------------------------------------------------------

def test_buying_cigarettes_is_a_purchase_and_does_not_count():
    ledger = build_smoking_ledger([PURCHASE_EVENT])
    assert ledger["total_count"] == 0
    assert ledger["purchase_count"] == 1
    assert ledger["sessions"][0]["action"] == "purchase"
    assert ledger["purchases"][0]["event_id"] == "evt_session_9ea2f06412f7f4ba"


def test_craving_is_an_urge_and_does_not_count():
    ledger = build_smoking_ledger([CRAVING_EVENT])
    assert ledger["total_count"] == 0
    assert ledger["urge_count"] == 1
    assert ledger["sessions"][0]["action"] == "urge"
    assert ledger["sessions"][0]["action_basis"] == "activity_type:state"


def test_confirmed_smoking_counts_exactly_once():
    ledger = build_smoking_ledger([_activity_smoke_event()])
    assert ledger["total_count"] == 1
    assert ledger["purchase_count"] == 0
    assert ledger["urge_count"] == 0
    assert ledger["sessions"][0]["action"] == "smoke"


# --- 判据本身 -------------------------------------------------------------

def test_purchase_verb_overrides_a_wrong_structured_action():
    """模型把"买烟"标成 action=smoke 时，文本是购买语义就要降级。"""
    event = {
        "event_id": "evt_buy_cig",
        "title": "消费支出：购买芒果味细支香烟与打火机零钱",
        "categories": ["expense"],
        "data": {"smoking": {"action": "smoke", "item": "芒果香烟（10元档）"}},
        "evidence": {"quote": "买包芒果香烟，十块钱的那个。"},
    }
    assert classify_smoking_event(event)[0] == "purchase"
    assert build_smoking_ledger([event])["total_count"] == 0


def test_purchase_and_consumption_in_one_utterance_still_counts():
    """既买又抽 → 是抽了，不能被买烟降级吞掉。"""
    event = {
        "event_id": "evt_buy_and_smoke",
        "title": "买了一包烟，抽了一根",
        "categories": ["routine"],
        "evidence": {"quote": "买了一包烟，抽了一根"},
    }
    assert classify_smoking_event(event)[0] == "smoke"


def test_buying_while_smoking_counts_as_smoked():
    """10-10 真实原话：买烟同时在抽、自述破戒 → 抽了，不被买烟降级吞掉。"""
    event = {
        "event_id": "evt_1010_relapse",
        "title": "买烟并抽烟（破戒）",
        "categories": ["expense"],
        "evidence": {"quote": "我在买烟，我在抽，他妈的破戒了，操。"},
    }
    assert classify_smoking_event(event)[0] == "smoke"
    assert build_smoking_ledger([event])["total_count"] == 1


def test_relapse_statement_counts_as_smoked():
    """10-22 真实原话「又破戒了」：复吸是已发生的吸烟。"""
    event = {
        "event_id": "evt_1022_relapse",
        "title": "又破戒了",
        "categories": ["routine"],
        "evidence": {"quote": "又破戒了。"},
    }
    assert classify_smoking_event(event)[0] == "smoke"


def test_deciding_not_to_buy_is_not_a_purchase():
    """10-23 真实原话：想了想没买。"""
    event = {
        "event_id": "evt_1023_no_buy",
        "title": "考虑买烟后放弃",
        "categories": ["routine"],
        "evidence": {"quote": "抽烟啊，我想想要不要买。不想买，关掉了。"},
    }
    assert classify_smoking_event(event)[0] != "purchase"
    assert build_smoking_ledger([event])["purchase_count"] == 0


def test_drawer_is_not_smoking():
    event = {
        "event_id": "evt_drawer",
        "title": "香烟放在抽屉里",
        "categories": ["routine"],
        "evidence": {"quote": "香烟我在抽屉里放着"},
    }
    assert classify_smoking_event(event)[0] != "smoke"


def test_memo_is_an_urge_not_a_smoked_cigarette():
    event = {
        "event_id": "evt_memo_smoke",
        "title": "便签：抽根烟",
        "categories": ["idea"],
        "data": {"memo": {"content": "抽根烟", "type": "pinned_voice_memo"}},
        "evidence": {"quote": "记录一下，抽根烟", "pinned_memo": True},
    }
    assert classify_smoking_event(event) == ("urge", "data.memo")
    assert build_smoking_ledger([event])["total_count"] == 0


def test_unknown_is_listed_but_never_counted():
    """有"烟"字但看不出事实 → unknown，只列出，绝不变成"确定抽了"。"""
    event = {
        "event_id": "evt_ambiguous",
        "title": "关于香烟的讨论",
        "categories": ["routine"],
        "evidence": {"quote": "那个烟啊……"},
    }
    action, basis = classify_smoking_event(event)
    assert action == "unknown"
    assert basis == "no_explicit_fact"
    ledger = build_smoking_ledger([event])
    assert ledger["total_count"] == 0
    assert len(ledger["sessions"]) == 1


def test_every_action_is_in_the_declared_vocabulary():
    for event in (PURCHASE_EVENT, CRAVING_EVENT, _activity_smoke_event()):
        assert classify_smoking_event(event)[0] in SMOKING_FACT_ACTIONS


def test_expense_ledger_is_untouched_by_the_smoking_fix():
    """消费账不受影响：同一批事件，吸烟账改成 0 次，消费账金额不变。"""
    from life_logger.daily_cognitive_session import build_expense_ledger

    events = [PURCHASE_EVENT]
    ledger = build_expense_ledger(events)
    assert ledger["transaction_count"] == 0  # 金额未提及 → 不入账（既有口径）
    assert build_smoking_ledger(events)["total_count"] == 0
