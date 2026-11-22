"""原事件事实视图（activity cards）：一条事件一张卡，只呈现已有事实。"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.activity_cards import SCHEMA, STATES, build_activity_cards  # noqa: E402

FIXTURES = ROOT / "work" / "meal-event-integration-audit-20261025"


def ev(eid="evt_1", *, title="事件", cats=("other",), atype=None, absolute=None, reported=None,
       quote="", claim=None, data=None, state=None, rec=None, seg=None, offsets=None) -> dict:
    event = {
        "event_id": eid,
        "title": title,
        "categories": list(cats),
        "activity_type": atype,
        "time": {"precision": "exact" if absolute else "unknown", "absolute": absolute},
        "evidence": {"quote": quote or title, "source_label": "self",
                     "reported_at": reported, "claim_type": claim,
                     "recording_id": rec, "segment_id": seg},
    }
    if state:
        event["event_state"] = state
    if data:
        event["data"] = data
    if offsets:
        event["evidence"]["start_offset_seconds"], event["evidence"]["end_offset_seconds"] = offsets
    return event


# ---------------------------------------------------------------- 粒度与引用

def test_one_event_one_card_and_no_merging() -> None:
    events = [
        ev("e1", title="买了一份鸭腿饭", cats=["food"], claim="order", absolute="2026-10-24T12:10:00+08:00"),
        ev("e2", title="又买了一份鸭腿饭", cats=["food"], claim="order", absolute="2026-10-24T12:14:00+08:00"),
    ]
    result = build_activity_cards(events)
    assert [card["source_event_id"] for card in result["cards"]] == ["e1", "e2"]
    assert result["alias"] == {"e1": result["cards"][0]["card_id"], "e2": result["cards"][1]["card_id"]}
    assert all(card["identity_scope"] == "single_event_only" for card in result["cards"])


def test_card_id_is_stable_and_not_an_activity_entity() -> None:
    first = build_activity_cards([ev("e1")])["cards"][0]["card_id"]
    second = build_activity_cards([ev("e9", title="别的事件"), ev("e1")])["alias"]["e1"]
    assert first == second
    assert first.startswith("card_")
    assert "activity" not in build_activity_cards([ev("e1")])["contract"]["identity"].split("；")[0]


def test_is_pure_and_does_not_mutate_inputs() -> None:
    events = [ev("e1", data={"exercise": {"duration_minutes": 30}})]
    snapshot = copy.deepcopy(events)
    build_activity_cards(events, ledger={"transactions": [{"event_id": "e1", "amount": 10.0}]})
    assert events == snapshot


# ---------------------------------------------------------------- 健身

def test_fitness_keeps_body_part_sets_and_per_side_note() -> None:
    card = build_activity_cards([ev(
        "fit1", title="力量训练第一组", cats=["exercise"], claim="event",
        data={"exercise": {"body_part": "胸", "duration_minutes": 62,
                           "exercises": [{"name": "卧推",
                                          "sets": [{"set": 1, "reps": 8, "weight_kg": 20,
                                                    "weight_note": "左右各10公斤"}]}]}},
    )])["cards"][0]
    paths = {fact["path"]: fact["value"] for fact in card["facts"]}
    assert paths["data.exercise.body_part"] == "胸"
    assert paths["data.exercise.exercises[0].name"] == "卧推"
    assert paths["data.exercise.exercises[0].sets[0].reps"] == 8
    assert paths["data.exercise.exercises[0].sets[0].weight_kg"] == 20
    assert paths["data.exercise.exercises[0].sets[0].weight_note"] == "左右各10公斤"
    assert card["duration"] == {"minutes": 62.0, "unit": "minutes", "path": "data.exercise.duration_minutes"}
    # 每侧与合计不双计：只有一条 set fact 组
    assert sum(1 for path in paths if path.endswith(".weight_kg")) == 1


def test_fitness_without_duration_reports_unknown_and_never_uses_recording_span() -> None:
    card = build_activity_cards([ev(
        "fit2", title="去了健身房", cats=["exercise"], claim="event",
        reported="2026-10-24T18:00:00+08:00", offsets=(12.0, 30.0),
    )])["cards"][0]
    assert card["duration"] is None
    assert "duration" in card["unknown_fields"]
    assert card["field_sources"]["duration"] == "recording_span_is_not_duration"


def test_planned_fitness_is_planned_not_done() -> None:
    """【订正 2026-10-25】自由文本"计划…"不再判 planned（纯文本不足 → unknown）；
    有 `claim_type="plan"` 时才是 planned。原断言理由是"计划词表"，已按 A0 规则废弃。"""
    text_only = build_activity_cards([ev(
        "fit3", title="计划今晚跑步", cats=["exercise"], quote="计划跑一个小时",
    )])["cards"][0]
    assert text_only["state"] == "unknown"
    assert text_only["state_basis"]["reason"] == "no_structured_state"
    structured = build_activity_cards([ev(
        "fit3b", title="计划今晚跑步", cats=["exercise"], quote="计划跑一个小时", claim="plan",
    )])["cards"][0]
    assert structured["state"] == "planned"
    assert structured["state_basis"]["path"] == "evidence.claim_type"


def test_cancelled_only_from_structured_state() -> None:
    """【订正 2026-10-25】"取消"文本不再判 cancelled（"没有取消"会被误判）；
    只有结构化 state=取消 才算。"""
    text_only = build_activity_cards([ev("c1", title="今晚跑步取消，不去了", cats=["exercise"])])["cards"][0]
    assert text_only["state"] == "unknown"
    structured = build_activity_cards([ev(
        "c1b", title="今晚跑步", cats=["exercise"], data={"exercise": {"state": "cancelled"}},
    )])["cards"][0]
    assert structured["state"] == "cancelled"
    assert structured["state_basis"]["path"] == "data.exercise.state"


# ---------------------------------------------------------------- 游戏

def test_game_keeps_name_and_rounds_without_inventing_end() -> None:
    card = build_activity_cards([ev(
        "g1", title="在家玩某游戏", cats=["game"], claim="event",
        data={"game": {"name": "某游戏", "mode": "排位", "rounds": 3}},
    )])["cards"][0]
    paths = {fact["path"]: fact["value"] for fact in card["facts"]}
    assert paths["data.game.name"] == "某游戏"
    assert paths["data.game.rounds"] == 3
    assert card["duration"] is None and "duration" in card["unknown_fields"]
    assert "已结束" not in str(card.get("title"))
    # 【订正】claim_type=event 不再等于"已完成"；无结构化状态 → unknown
    assert card["state"] == "unknown"


# ---------------------------------------------------------------- 餐饮 / 交易

def test_meal_keeps_order_but_never_claims_eaten() -> None:
    card = build_activity_cards([ev(
        "m1", title="来一份鸭腿饭", cats=["food"], claim="order",
        data={"meal": {"items": [{"name": "鸭腿饭", "portion": "1份", "flavor": "微辣"}],
                       "consumed": None}},
    )])["cards"][0]
    paths = {fact["path"]: fact["value"] for fact in card["facts"]}
    assert paths["data.meal.items[0].name"] == "鸭腿饭"
    assert paths["data.meal.items[0].flavor"] == "微辣"
    # 【订正】claim_type=order 不再等于"已完成"；点单存在但状态未在结构化字段声明 → unknown
    assert card["state"] == "unknown"
    assert "consumed" not in card and "吃过了" not in str(card)


def test_transactions_are_referenced_never_summed_or_merged() -> None:
    ledger = {"transactions": [
        {"event_id": "t1", "amount": 10.0, "currency": "CNY"},
        {"event_id": "t1", "amount": 3.0, "currency": "CNY"},   # 同事件两笔
    ]}
    card = build_activity_cards([ev("t1", title="买鸭腿加烤肠", cats=["expense"], claim="payment",
                                    data={"expense": {"amount": 13.0}})], ledger=ledger)["cards"][0]
    # 账本索引按 event_id 收一条（引用语义），且卡片没有合计字段
    assert all(ref["amount_policy"] == "reference_only_no_sum" for ref in card["transactions"])
    assert "total" not in card and "amount_total" not in card
    other = build_activity_cards([ev("t2", title="另一笔", cats=["expense"], claim="payment")],
                                 ledger=ledger)["cards"][0]
    assert other["transactions"] == []


def test_two_payment_broadcasts_stay_separate_cards() -> None:
    result = build_activity_cards([
        ev("p1", title="支付宝到账十元", cats=["expense"], claim="payment"),
        ev("p2", title="微信收款十元", cats=["expense"], claim="payment"),
    ])
    assert len(result["cards"]) == 2
    assert result["stats"]["cards"] == result["stats"]["input_events"]


def test_instances_are_referenced_not_compared() -> None:
    cards = build_activity_cards([
        ev("i1", title="点了鸭腿饭", cats=["food"], claim="order", data={"order_id": "O1"}),
        ev("i2", title="同一单再加", cats=["food"], claim="order", data={"order_id": "O1"}),
    ])["cards"]
    assert [card["instances"][0]["id"] for card in cards] == ["O1", "O1"]
    assert "order_id" in cards[0]["field_sources"]["instances"][0]


# ---------------------------------------------------------------- 未知 / 保真

def test_unknown_structure_is_preserved_without_diet_template() -> None:
    card = build_activity_cards([ev(
        "u1", title="修了个东西", cats=["other"],
        data={"custom_block": {"part": "水泵", "count": 2}},
    )])["cards"][0]
    paths = {fact["path"]: fact["value"] for fact in card["facts"]}
    assert paths == {"data.custom_block.part": "水泵", "data.custom_block.count": 2}
    assert "meal" not in card and card["kind"] == "other"
    assert "domain_facts" not in card["unknown_fields"]


def test_quote_numbers_never_become_facts() -> None:
    card = build_activity_cards([ev(
        "q1", title="买了一份十五元的东西", cats=["expense"], claim="event", quote="我花了十五块",
    )])["cards"][0]
    assert card["facts"] == []
    assert all("amount" not in fact for fact in card["facts"])
    assert card["instances"] == []


def test_state_unknown_means_not_mentioned() -> None:
    card = build_activity_cards([ev("n1", title="提到周末露营", cats=["routine"])])["cards"][0]
    assert card["state"] == "unknown"
    assert card["state_basis"]["reason"] == "no_structured_state"
    assert "state" in card["unknown_fields"]
    payload = str(card)
    assert "not_done" not in payload and "没做" not in payload


def test_contract_and_stats_are_stable() -> None:
    result = build_activity_cards([ev("s1", cats=["exercise"]), ev("s2", cats=["game"])])
    assert result["schema"] == SCHEMA
    assert set(result["stats"]["by_state"]) == set(STATES)
    assert result["contract"]["granularity"].startswith("one canonical event per card")
    assert "reference_only_no_sum" in result["contract"]["money"]


@pytest.mark.skipif(not (FIXTURES / "daily25.json").is_file(), reason="离线真实样本缺失")
def test_real_duck_sample_yields_one_card_per_event() -> None:
    import json

    events = json.loads((FIXTURES / "daily25.json").read_text())["events"]
    result = build_activity_cards(events)
    assert result["stats"]["cards"] == len(events)
    duck = next(card for card in result["cards"] if card["source_event_id"] == "evt_order_bf2741cf4f9c2415")
    assert duck["kind"] == "meal"
    assert all(ref["amount_policy"] == "reference_only_no_sum" for ref in duck["transactions"])


# ---------------------------------------------------------------- 4号 独立反例（S1-S4）

def test_s1_plan_text_with_done_substring_is_not_done() -> None:
    card = build_activity_cards([ev(
        "s1", title="明天练完腿再回家", cats=["exercise"], quote="明天练完腿再回家",
    )])["cards"][0]
    assert card["state"] != "done"
    assert card["state"] == "unknown"
    assert "明天练完腿再回家" in (card["evidence"]["quote"] or "")


def test_s2_negated_cancel_text_is_not_cancelled() -> None:
    card = build_activity_cards([ev(
        "s2", title="没有取消训练", cats=["exercise"], quote="今天的训练没有取消",
    )])["cards"][0]
    assert card["state"] != "cancelled"
    assert card["state"] == "unknown"


def test_s3_claim_type_event_is_not_completed() -> None:
    card = build_activity_cards([ev(
        "s3", title="吃了鸭腿饭", cats=["food"], claim="event",
    )])["cards"][0]
    assert card["state"] == "unknown"
    assert card["state_basis"]["reason"] == "no_structured_state"


def test_s4_explicit_completed_beats_text_negation() -> None:
    card = build_activity_cards([ev(
        "s4", title="已完成，没有取消", cats=["exercise"], state="completed",
        quote="已完成，没有取消",
    )])["cards"][0]
    assert card["state"] == "done"
    assert card["state_basis"]["path"] == "event_state"


def test_conflicting_structured_states_become_unknown_with_conflicts() -> None:
    card = build_activity_cards([ev(
        "cf", title="状态冲突样本", cats=["exercise"], state="completed",
        data={"exercise": {"state": "cancelled"}},
    )])["cards"][0]
    assert card["state"] == "unknown"
    assert card["state_basis"]["reason"] == "conflicting_or_unmapped_structured_state"
    assert {item["path"] for item in card["state_basis"]["conflicts"]} == {"event_state", "data.exercise.state"}


def test_in_progress_and_planned_are_structural_only() -> None:
    started = build_activity_cards([ev("ip", title="开始训练", cats=["exercise"], state="in_progress")])["cards"][0]
    assert started["state"] == "in_progress"
    planned = build_activity_cards([ev("pl", title="计划训练", cats=["exercise"], claim="plan")])["cards"][0]
    assert planned["state"] == "planned"
