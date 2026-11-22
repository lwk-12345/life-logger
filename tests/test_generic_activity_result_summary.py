"""通用活动结果摘要：状态分开、领域事实有证据才写、金额/时长不合计、长活动按真实时间。"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.activity_presentation import build_activity_presentation  # noqa: E402


def card(event_id, title, *, state="unknown", clock="09:00", facts=None, kind="fitness"):
    return {"source_event_id": event_id, "title": title, "kind": kind, "state": state,
            "time": {"clock": clock, "occurred_at": f"2026-10-25T{clock}:00+08:00", "precision": "minute"},
            "transactions": [], "facts": facts or [], "unknown_fields": []}


def linked(cards, ref="activity:act_x"):
    return build_activity_presentation(cards, {
        "members": {ref: [c["source_event_id"] for c in cards]},
        "member_meta": {ref: {"declaration_consistent": True, "conflicted": []}},
    })


def test_training_summary_keeps_actions_and_sets_without_summing_amounts():
    cards = [
        card("e1", "深蹲", facts=[{"path": "data.exercise.exercises[0].name", "value": "深蹲"}], clock="18:03"),
        card("e2", "卧推", facts=[{"path": "data.exercise.exercises[0].name", "value": "卧推"}], clock="18:20"),
    ]
    group = linked(cards)["groups"][0]
    summary = group["result_summary"]
    assert summary["schema"] == "activity-result-summary-v1"
    assert summary["actions"] == ["深蹲", "卧推"]
    assert summary["amounts_summed"] is False and summary["durations_summed"] is False
    assert "amount" not in summary and "total_amount" not in summary
    assert [s["event_id"] for s in group["steps"]] == ["e1", "e2"], "按真实时间顺序"


def test_mixed_states_are_not_reported_as_completed():
    cards = [
        card("p1", "计划跑步", state="planned", clock="08:00"),
        card("a1", "跑了 5 公里", state="completed", clock="09:00"),
    ]
    summary = linked(cards)["groups"][0]["result_summary"]
    assert summary["status"] == "conflict", summary
    assert summary["state_breakdown"] == {"planned": 1, "completed": 1}
    assert summary["status_note"]


def test_cancelled_and_unknown_are_separate():
    cards = [card("c1", "取消练腿", state="cancelled"), card("u1", "提到周末露营", state="unknown")]
    summary = linked(cards)["groups"][0]["result_summary"]
    assert summary["status"] == "mixed"
    assert set(summary["state_breakdown"]) == {"cancelled", "unknown"}


def test_model_only_facts_are_not_written_as_confirmed_values():
    cards = [
        card("m1", "深蹲", facts=[
            {"path": "data.exercise.exercises[0].name", "value": "深蹲"},
            {"path": "data.exercise.exercises[0].sets[3].weight_kg", "value": 90, "support": "model_only"},
            {"path": "data.exercise.exercises[0].sets[0].weight_kg", "value": 40, "support": "quote_verbatim"},
        ], clock="18:03"),
        card("m2", "深蹲第二组", facts=[{"path": "data.exercise.exercises[0].name", "value": "深蹲"}], clock="18:10"),
    ]
    summary = linked(cards)["groups"][0]["result_summary"]
    assert "90" not in summary["items"], "model_only 不得写成确定值"
    assert any(f["value"] == 90 and f["support"] == "model_only" for f in summary["model_only_facts"])


def test_game_and_food_domain_facts():
    game = card("g1", "艾尔登法环", kind="game", facts=[
        {"path": "data.game.name", "value": "艾尔登法环"},
        {"path": "data.game.rounds", "value": 3},
    ], clock="20:00")
    game2 = card("g2", "又玩了一局", kind="game", clock="20:30",
                 facts=[{"path": "data.game.name", "value": "艾尔登法环"}])
    summary = linked([game, game2])["groups"][0]["result_summary"]
    assert summary["title"] == "艾尔登法环", "领域 name 作为活动名（actions[0]）"
    assert summary["actions"] == ["艾尔登法环"]
    assert "3" in summary["items"], "局数保留为子项"


def test_location_and_duration_only_when_present():
    with_evidence = card("l1", "健身房训练", facts=[
        {"path": "data.exercise.location", "value": "某健身房"},
        {"path": "data.exercise.duration_minutes", "value": 70},
    ])
    without = card("l2", "随手记录")
    filler1 = card("l1b", "训练第二段", clock="19:00")
    out = build_activity_presentation([with_evidence, filler1], {
        "members": {"activity:a": ["l1", "l1b"]},
        "member_meta": {"activity:a": {"declaration_consistent": True}}})
    summary = out["groups"][0]["result_summary"]
    assert summary["locations"] == ["某健身房"]
    assert summary["durations"][0]["minutes"] == 70.0
    filler2 = card("l2b", "随手记录二", clock="19:00")
    out2 = build_activity_presentation([without, filler2], {
        "members": {"activity:b": ["l2", "l2b"]},
        "member_meta": {"activity:b": {"declaration_consistent": True}}})
    summary2 = out2["groups"][0]["result_summary"]
    assert summary2["locations"] == [] and summary2["durations"] == []


def test_inputs_and_existing_group_fields_unchanged():
    cards = [card("e1", "深蹲"), card("e2", "卧推")]
    snapshot = copy.deepcopy(cards)
    out = linked(cards)
    assert cards == snapshot, "入参不得被修改"
    group = out["groups"][0]
    for key in ("activity_ref", "member_event_ids", "members", "summary", "compact"):
        assert key in group, key
    assert group["summary"]["amounts_summed"] is False
    assert out["coverage"]["transactions_dropped"] == 0


def test_no_group_no_summary():
    out = build_activity_presentation([card("solo", "打游戏", kind="game")], {"members": {}, "member_meta": {}})
    assert out["groups"] == []
