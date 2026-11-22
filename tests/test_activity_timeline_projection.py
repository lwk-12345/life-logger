"""通用活动投影（activity projection）· 第一切片（2026-10-25）。

设计依据：`work/meal-event-integration-audit-20261025/REPORT.md`（用户已批准）。
被测函数：`life_logger.event_episodes.build_activity_projection()`（纯函数，不读写盘）。

本文件的样本来源：
* **真实样本**（离线读取，缺失则 skip）：`work/meal-event-integration-audit-20261025/`
  - `daily25.json`：鸭腿点单 + 口味补充 + 6 条收款播报（9 条）
  - `2026-10-22.json`：力量训练 62 分钟 + 首组左右各 10 公斤 + 夜间计划慢爬坡 60 分钟
  - `2026-10-23.json`：计划跑步 1 小时 + 局部"跑步 20 分钟/总训练 1 小时"
* **明确标注的合成样本**：游戏（用户举例，无真实会话），以及重复组次/不同主体/取消/跨日等边界。
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.event_episodes import (  # noqa: E402
    ACTIVITY_SCHEMA,
    ACTIVITY_STATES,
    build_activity_projection,
    build_episodes,
)

FIXTURES = ROOT / "work" / "meal-event-integration-audit-20261025"


def _load(name: str) -> dict:
    path = FIXTURES / name
    if not path.is_file():
        pytest.skip(f"离线样本缺失：{path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def _duck_sample() -> dict:
    return _load("daily25.json")


def _event(**overrides) -> dict:
    """合成事件（非真实数据，用于边界用例）。"""
    base = {
        "event_id": "evt_synth",
        "title": "合成事件",
        "categories": ["other"],
        "time": {"precision": "unknown", "absolute": None},
        "evidence": {"quote": "", "source_label": "self", "reported_at": None},
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# 0. 兼容与契约
# --------------------------------------------------------------------------

def test_build_episodes_is_still_compatible() -> None:
    events = [
        _event(event_id="e1", title="讨论前端", categories=["work_communication"],
               evidence={"quote": "讨论前端代码怎么拆", "reported_at": "2026-10-24T10:00:00+00:00"}),
        _event(event_id="e2", title="讨论后端", categories=["work_communication"],
               evidence={"quote": "后端接口要不要合并", "reported_at": "2026-10-24T10:00:20+00:00"}),
    ]
    result = build_episodes(events)
    assert set(result) >= {"episodes", "alias", "dropped", "stats"}
    assert result["stats"]["input_events"] == 2


def test_contract_declares_hard_rules() -> None:
    result = build_activity_projection([])
    assert result["schema"] == ACTIVITY_SCHEMA
    must_not = " ".join(result["contract"]["must_not"])
    for phrase in ("重算金额", "播报", "录音跨度", "计划", "unknown"):
        assert phrase in must_not
    assert result["contract"]["ledger"].startswith("ledger_refs 只引用")
    assert "planned" in ACTIVITY_STATES and "conflict" in ACTIVITY_STATES
    assert "not_done" not in ACTIVITY_STATES


def test_is_pure_and_does_not_mutate_inputs() -> None:
    data = _duck_sample()
    events = copy.deepcopy(data["events"])
    snapshot = copy.deepcopy(events)
    build_activity_projection(events, ledger=data.get("expense_ledger"))
    assert events == snapshot


# --------------------------------------------------------------------------
# 1. 真实样本：鸭腿点单 + 口味（重复/修订/补充分开）
# --------------------------------------------------------------------------

def test_duck_sample_every_event_id_is_traceable() -> None:
    data = _duck_sample()
    result = build_activity_projection(data["events"], ledger=data.get("expense_ledger"))
    event_ids = {event["event_id"] for event in data["events"]}
    assert set(result["alias"]) == event_ids
    assert set(result["alias"].values()) <= {row["activity_id"] for row in result["activities"]}
    for activity in result["activities"]:
        assert set(activity["source_event_ids"]) <= event_ids
        assert activity["evidence"]


def test_duck_order_and_flavour_form_one_activity_with_revision() -> None:
    data = _duck_sample()
    result = build_activity_projection(data["events"], ledger=data.get("expense_ledger"))
    duck = [row for row in result["activities"] if "鸭腿" in row["title"]]
    assert len(duck) == 1, [row["title"] for row in result["activities"]]
    activity = duck[0]
    assert {action["label"] for action in activity["actions"]} >= {"选择鸭腿加辣", "明确要求鸭腿微辣"}
    revision = [rel for rel in activity["relations"] if rel["type"] == "revision"]
    assert revision and all(rel["resolved"] for rel in revision)
    # 旧值保留：辣 → 微辣 两条都在 actions 里，没有被覆盖删除
    assert len(activity["actions"]) == 3


def test_broadcasts_never_merge_into_the_order_activity() -> None:
    data = _duck_sample()
    result = build_activity_projection(data["events"], ledger=data.get("expense_ledger"))
    for activity in result["activities"]:
        roles = {ref["role"] for ref in activity["ledger_refs"]}
        if "broadcast" in roles:
            assert roles == {"broadcast"}, activity["title"]
    duck = [row for row in result["activities"] if "鸭腿" in row["title"]][0]
    assert [ref["role"] for ref in duck["ledger_refs"]] == ["ledger"]


def test_same_amount_different_channels_are_not_merged() -> None:
    data = _duck_sample()
    result = build_activity_projection(data["events"], ledger=data.get("expense_ledger"))
    ten_yuan = [
        row
        for row in result["activities"]
        if any(ref["amount"] == 10.0 and ref["role"] == "broadcast" for ref in row["ledger_refs"])
    ]
    assert len(ten_yuan) == 2, "两条 ¥10 播报必须各自成卡"
    possible = [
        rel
        for rel in result["relations"]
        if rel["type"] == "possible" and not rel["resolved"]
    ]
    assert possible, "同额不同渠道应给出 possible 关系提示"


def test_amounts_are_referenced_never_summed() -> None:
    data = _duck_sample()
    result = build_activity_projection(data["events"], ledger=data.get("expense_ledger"))
    for activity in result["activities"]:
        assert "total" not in activity and "amount_total" not in activity
        for ref in activity["ledger_refs"]:
            assert ref["amount_policy"] == "reference_only_no_sum"
            assert set(ref) >= {"event_id", "amount", "role", "source"}
    # 账本只有 ¥10 一笔入账；播报金额不得进入任何合计字段
    ledger_refs = [
        ref
        for activity in result["activities"]
        for ref in activity["ledger_refs"]
        if ref["role"] == "ledger"
    ]
    assert [ref["amount"] for ref in ledger_refs] == [10.0]


def test_meal_activity_is_not_marked_completed() -> None:
    data = _duck_sample()
    result = build_activity_projection(data["events"], ledger=data.get("expense_ledger"))
    duck = [row for row in result["activities"] if "鸭腿" in row["title"]][0]
    assert duck["state"] != "completed"
    assert "同额播报仅作候选证据" in " ".join(duck["boundary_notes"])


def test_duration_is_never_taken_from_recording_span() -> None:
    data = _duck_sample()
    result = build_activity_projection(data["events"], ledger=data.get("expense_ledger"))
    duck = [row for row in result["activities"] if "鸭腿" in row["title"]][0]
    assert duck["duration"]["actual_minutes"] is None
    assert duck["duration"]["planned_minutes"] is None
    assert duck["duration"]["note"] == "recording_span_is_not_duration"
    for action in duck["actions"]:
        assert action["duration"]["minutes"] is None


# --------------------------------------------------------------------------
# 2. 真实样本：健身（显式时长、每侧 vs 合计、计划 vs 实际）
# --------------------------------------------------------------------------

def test_fitness_explicit_duration_field_is_used() -> None:
    data = _load("2026-10-22.json")
    result = build_activity_projection(data["events"], ledger=data.get("expense_ledger"))
    strength = [row for row in result["activities"] if "力量训练" in row["title"]]
    assert strength
    activity = next(row for row in strength if row["duration"]["actual_minutes"])
    assert activity["duration"]["actual_minutes"] == 62.0
    assert activity["duration"]["basis"].startswith("explicit_field:")
    assert activity["duration"]["evidence_event_ids"]


def test_per_side_and_total_weight_are_one_fact_not_two() -> None:
    data = _load("2026-10-22.json")
    result = build_activity_projection(data["events"], ledger=data.get("expense_ledger"))
    facts = [
        fact
        for activity in result["activities"]
        for fact in activity["facts"]
        if fact["key"] == "exercise_set"
    ]
    per_side = [f for f in facts if f["value"].get("weight_note") == "左右各十公斤"]
    assert per_side, "真实样本应保留 weight_note"
    assert len(per_side) == 1
    assert per_side[0]["value"]["weight_kg"] == 10
    # 每侧 10kg 与合计 20kg 不得在同一活动里各算一组
    for activity in result["activities"]:
        weights = [
            fact["value"].get("weight_kg")
            for fact in activity["facts"]
            if fact["key"] == "exercise_set"
        ]
        assert not (10 in weights and 20 in weights and len(weights) > 1)


def test_planned_cardio_is_separate_from_strength_session() -> None:
    data = _load("2026-10-22.json")
    result = build_activity_projection(data["events"], ledger=data.get("expense_ledger"))
    planned = [
        row for row in result["activities"]
        if row["kind"] == "fitness" and row["states"]["planned"]
    ]
    assert planned, "10-22 有夜间计划慢爬坡"
    assert planned[0]["duration"]["planned_minutes"] == 60.0
    assert planned[0]["duration"]["actual_minutes"] is None
    strength = [row for row in result["activities"] if "力量训练" in row["title"]][0]
    assert strength["activity_id"] != planned[0]["activity_id"]


def test_plan_vs_actual_on_same_object_is_only_possible() -> None:
    data = _load("2026-10-23.json")
    result = build_activity_projection(data["events"], ledger=data.get("expense_ledger"))
    fitness = [row for row in result["activities"] if row["kind"] == "fitness"]
    assert len(fitness) >= 2
    states = {row["activity_id"]: row["state"] for row in fitness}
    assert "planned" in states.values()
    possible = [
        rel
        for rel in result["relations"]
        if rel["type"] == "possible" and "计划与实际" in rel["reason"]
    ]
    assert possible, "同对象计划/实际应给出 possible 提示，而不是合并或一键完成"
    assert all(rel["resolved"] is False for rel in possible)


# --------------------------------------------------------------------------
# 3. 合成边界（明确标注：非真实数据）
# --------------------------------------------------------------------------

def test_repeated_sets_with_same_weight_are_not_merged() -> None:
    """合成：同名同重量的两组，必须保留两组，不能折成一组。"""
    events = [
        _event(
            event_id="set1",
            title="卧推第一组",
            categories=["exercise"],
            data={"exercise": {"exercises": [{"name": "卧推", "sets": [{"set": 1, "reps": 8, "weight_kg": 40}]}]}},
            evidence={"quote": "第一组卧推八个四十公斤", "source_label": "self",
                      "reported_at": "2026-10-24T10:00:00+00:00"},
        ),
        _event(
            event_id="set2",
            title="卧推第二组",
            categories=["exercise"],
            data={"exercise": {"exercises": [{"name": "卧推", "sets": [{"set": 2, "reps": 8, "weight_kg": 40}]}]}},
            evidence={"quote": "第二组还是八个四十公斤", "source_label": "self",
                      "reported_at": "2026-10-24T10:30:00+00:00"},
        ),
    ]
    result = build_activity_projection(events)
    sets = [
        fact
        for activity in result["activities"]
        for fact in activity["facts"]
        if fact["key"] == "exercise_set"
    ]
    assert len(sets) == 2
    assert {fact["set_index"] for fact in sets} == {1, 2}


def test_different_subjects_are_never_merged() -> None:
    """合成：同一句话被 self 与 non_self 各抽一次 → 两个活动，不合并。"""
    events = [
        _event(event_id="self-1", title="买咖啡", categories=["expense"],
               data={"expense": {"amount": 20.0, "item": "咖啡"}},
               evidence={"quote": "我买了一杯咖啡", "source_label": "self",
                         "reported_at": "2026-10-24T09:00:00+00:00"}),
        _event(event_id="other-1", title="买咖啡", categories=["expense"],
               data={"expense": {"amount": 20.0, "item": "咖啡"}},
               evidence={"quote": "他买了一杯咖啡", "source_label": "non_self",
                         "reported_at": "2026-10-24T09:00:05+00:00"}),
    ]
    result = build_activity_projection(events)
    assert len(result["activities"]) == 2
    assert {activity["subject"]["actor"] for activity in result["activities"]} == {"self", "non_self"}


def test_missing_duration_is_not_invented() -> None:
    """合成：只有讲述时间，没有任何时长证据 → duration 为空，不给 0、不给固定值。"""
    events = [
        _event(event_id="gym-1", title="去了健身房", categories=["exercise"],
               evidence={"quote": "我到健身房了", "source_label": "self",
                         "reported_at": "2026-10-24T18:00:00+00:00",
                         "start_offset_seconds": 12.0}),
    ]
    result = build_activity_projection(events)
    activity = result["activities"][0]
    assert activity["duration"]["actual_minutes"] is None
    assert activity["duration"]["planned_minutes"] is None
    assert activity["duration"]["note"] == "recording_span_is_not_duration"
    assert "只有录音跨度" in " ".join(activity["boundary_notes"])


def test_unknown_state_is_not_reported_as_not_done() -> None:
    """合成：什么都没说 → unknown；不得出现未做/没做语义。"""
    events = [_event(event_id="u1", title="提到周末露营", categories=["routine"],
                     evidence={"quote": "周末可能去露营", "source_label": "self",
                               "reported_at": "2026-10-24T20:00:00+00:00"})]
    result = build_activity_projection(events)
    activity = result["activities"][0]
    assert activity["state"] == "unknown"
    payload = json.dumps(activity, ensure_ascii=False)
    assert "not_done" not in payload and "没做" not in payload and "未完成" not in payload


def test_cancelled_only_from_explicit_marker() -> None:
    """合成：明确取消才 cancelled，并且不退化成 completed。"""
    events = [_event(event_id="c1", title="取消今晚跑步", categories=["exercise"],
                     evidence={"quote": "今晚跑步取消，不去了", "source_label": "self",
                               "reported_at": "2026-10-24T21:00:00+00:00"})]
    result = build_activity_projection(events)
    assert result["activities"][0]["state"] == "cancelled"


def test_cross_day_keeps_sub_item_dates() -> None:
    """合成：跨午夜的两段同一录音 → 一张活动，但子项各自日期不搬移。"""
    base = {"recording_id": "rec_x", "source_label": "self"}
    events = [
        _event(event_id="d1", title="点餐咖喱饭", categories=["food"],
               data={"meal": {"item": "咖喱牛肉蛋炒饭"}},
               evidence={**base, "quote": "来一份咖喱牛肉蛋炒饭", "segment_id": "rec_x_seg_1",
                         "reported_at": "2026-10-24T15:59:00+00:00"}),
        _event(event_id="d2", title="加点炸薯条", categories=["food"],
               data={"meal": {"item": "炸薯条"}},
               evidence={**base, "quote": "再加一份炸薯条", "segment_id": "rec_x_seg_2",
                         "reported_at": "2026-10-24T16:01:00+00:00"}),
    ]
    result = build_activity_projection(events)
    activity = result["activities"][0]
    # 15:59Z = 10-24 23:59 (+08)；16:01Z = 10-25 00:01 (+08)
    assert activity["date_span"] == ["2026-10-24", "2026-10-25"]
    dates = [action["time"]["date"] for action in activity["actions"]]
    assert dates == ["2026-10-24", "2026-10-25"], "子项必须保留各自日期，不得搬移"


def test_activity_id_is_stable_when_later_evidence_arrives() -> None:
    data = _duck_sample()
    events = [event for event in data["events"] if "鸭腿" in (event.get("title") or "")]
    before = build_activity_projection(events, ledger=data.get("expense_ledger"))
    base_id = before["activities"][0]["activity_id"]
    extra = _event(
        event_id="late-1",
        title="鸭腿不要香菜",
        categories=["food"],
        evidence={"quote": "鸭腿不要香菜", "source_label": "self",
                  "reported_at": "2026-10-24T16:19:58+00:00"},
    )
    after = build_activity_projection(events + [extra], ledger=data.get("expense_ledger"))
    duck = [row for row in after["activities"] if "鸭腿" in row["title"]]
    assert len(duck) == 1
    assert duck[0]["activity_id"] == base_id, "补充一句话不得改变活动 ID"


def test_game_sample_synthetic() -> None:
    """**合成样本**（用户举例，非真实会话）：游戏名称/局数/时长有依据才展示。"""
    events = [
        _event(event_id="game-1", title="在家玩《某游戏》", categories=["game"],
               data={"game": {"name": "某游戏", "mode": "排位", "rounds": 3}},
               evidence={"quote": "在家打了两三局排位", "source_label": "self",
                         "reported_at": "2026-10-24T22:00:00+00:00"}),
        _event(event_id="game-2", title="又玩了一局", categories=["game"],
               data={"game": {"name": "某游戏", "rounds": 1}},
               evidence={"quote": "又玩了一局", "source_label": "self",
                         "reported_at": "2026-10-24T22:20:00+00:00",
                         "recording_id": "rec_game", "segment_id": "rec_game_seg_2"}),
    ]
    result = build_activity_projection(events)
    activity = result["activities"][0]
    assert activity["kind"] == "game"
    fact_keys = {fact["key"] for fact in activity["facts"]}
    assert "game.rounds" in fact_keys
    assert activity["duration"]["actual_minutes"] is None
    payload = json.dumps(activity, ensure_ascii=False)
    assert "已结束" not in payload and "没玩" not in payload


def test_possible_relations_are_flagged_not_merged_for_broadcast_pairs() -> None:
    data = _duck_sample()
    result = build_activity_projection(data["events"], ledger=data.get("expense_ledger"))
    merged_broadcasts = [
        row
        for row in result["activities"]
        if len([ref for ref in row["ledger_refs"] if ref["role"] == "broadcast"]) > 1
    ]
    assert merged_broadcasts == []
    assert all(rel["resolved"] is False for rel in result["relations"])


# --------------------------------------------------------------------------
# 4. 返工（2026-10-25 独立验收 FAIL 后）：活动标识唯一性 + 跨 session 不误合
# --------------------------------------------------------------------------

def _meal(eid: str, clock: str, *, rec_id: str = "watch_A", quote: str = "", **extra) -> dict:
    """合成：同一天的同对象事件（用于标识冲突/稳定性回归）。"""
    event = _event(
        event_id=eid,
        title="吃了鸭腿饭",
        categories=["food"],
        activity_type="meal",
        time={"precision": "exact", "absolute": f"2026-10-25T{clock}:00+08:00"},
        evidence={"quote": quote or "吃了鸭腿饭", "source_label": "self",
                  "recording_id": rec_id, "reported_at": f"2026-10-25T{clock}:00+08:00"},
    )
    event.update(extra)
    return event


def test_same_day_two_sessions_of_same_object_have_distinct_ids() -> None:
    """回归 P0：同一天、同对象的两场独立活动必须有不同的 activity_id。"""
    result = build_activity_projection([_meal("m1", "12:10"), _meal("m2", "19:10")])
    activities = result["activities"]
    assert len(activities) == 2
    ids = [row["activity_id"] for row in activities]
    assert len(set(ids)) == 2, ids
    buckets = {row["activity_identity"]["time_bucket"] for row in activities}
    assert buckets == {"2026-10-25T12:10", "2026-10-25T19:10"}


def test_same_bucket_two_activities_still_get_unique_ids() -> None:
    """同桶内两场（同一 10 分钟）→ 用 ordinal 区分，仍然唯一。"""
    events = [
        _meal("b1", "12:10", rec_id="watch_A"),
        _meal("b2", "12:12", rec_id="watch_B"),
    ]
    result = build_activity_projection(events)
    activities = result["activities"]
    if len(activities) < 2:  # 被判定为同一场时视为契约外，不静默通过
        pytest.fail("不同 recording 的两场同对象活动不应合并")
    assert len({row["activity_id"] for row in activities}) == len(activities)
    assert {row["activity_identity"]["basis"] for row in activities} == {"snapshot_local"}


def test_ids_are_unique_across_all_real_samples() -> None:
    data = _duck_sample()
    for payload in (data, _load("2026-10-22.json"), _load("2026-10-23.json")):
        result = build_activity_projection(payload["events"], ledger=payload.get("expense_ledger"))
        ids = [row["activity_id"] for row in result["activities"]]
        assert len(ids) == len(set(ids))
        for row in result["activities"]:
            identity = row["activity_identity"]
            assert identity["basis"] in {"snapshot_local", "source_instance_id"}
            assert identity["stable_across_revisions"] is identity["persistent_reference_ok"]


def test_different_recordings_close_in_time_are_not_merged() -> None:
    """回归 P1：不同 session 仅时间相近+对象同 → 两个活动（证据不足，不合并）。"""
    result = build_activity_projection([
        _meal("s1", "12:10", rec_id="watch_A"),
        _meal("s2", "12:11", rec_id="watch_B"),
    ])
    assert len(result["activities"]) == 2
    assert {row["subject"]["actor"] for row in result["activities"]} == {"self"}


def test_explicit_link_across_recordings_is_allowed() -> None:
    """有明确关联证据（同单号 / 回头指代）时允许跨录音并入同一活动。"""
    by_order = build_activity_projection([
        _meal("o1", "12:10", rec_id="watch_A", data={"order_id": "ord-9"}),
        _meal("o2", "12:11", rec_id="watch_B", data={"order_id": "ord-9"}),
    ])
    assert len(by_order["activities"]) == 1, "同 order_id 应允许合并"

    by_reference = build_activity_projection([
        _meal("f1", "12:10", rec_id="watch_A"),
        _meal("f2", "12:12", rec_id="watch_B", quote="还是刚才那家，再来一份鸭腿饭"),
    ])
    # 三次返工：中文回头指代**不再**是合并证据，只能给 possible（不再扩词表）
    assert len(by_reference["activities"]) == 2, "纯文字指代不得合并两个不同实例"
    assert any(rel["type"] == "possible" for rel in by_reference["relations"])


def test_three_complements_plus_revision_stay_one_activity() -> None:
    """≥3 条互补 + 1 条更正 → 同一活动；补充第 5 条后 ID 不变。"""
    events = [
        _meal("c1", "12:10"),
        _meal("c2", "12:12", quote="加个辣椒"),
        _meal("c3", "12:13", quote="再加一份米饭"),
        _meal("c4", "12:14", quote="不要辣椒，微辣就好"),
    ]
    first = build_activity_projection(events)
    assert len(first["activities"]) == 1
    activity = first["activities"][0]
    assert len(activity["actions"]) == 4
    assert any(rel["type"] == "revision" for rel in activity["relations"])
    activity_id = activity["activity_id"]

    fifth = events + [_meal("c5", "12:15", quote="再来杯可乐")]
    again = build_activity_projection(fifth)
    assert len(again["activities"]) == 1
    assert again["activities"][0]["activity_id"] == activity_id
    assert len(again["activities"][0]["actions"]) == 5


def test_deleted_anchor_changes_identity_without_breaking_alias() -> None:
    """删除锚点样本：ID **会**变（契约边界），但 alias/唯一性/identity 必须自洽。"""
    events = [_meal("a1", "12:10"), _meal("a2", "12:40"), _meal("a3", "12:41")]
    with_anchor = build_activity_projection(events)
    anchor_activity = next(
        row for row in with_anchor["activities"] if "a1" in row["source_event_ids"]
    )
    without_anchor = build_activity_projection([event for event in events if event["event_id"] != "a1"])
    assert all("a1" not in row["source_event_ids"] for row in without_anchor["activities"])
    remaining = [
        row for row in without_anchor["activities"] if set(row["source_event_ids"]) & {"a2", "a3"}
    ]
    assert remaining
    new_activity = remaining[0]
    # 锚点被删 → 时间桶前移 → ID 变化，这是**契约内**的行为，必须显式暴露原因
    assert new_activity["activity_id"] != anchor_activity["activity_id"]
    assert new_activity["activity_identity"]["time_bucket"] == "2026-10-25T12:40"
    assert anchor_activity["activity_identity"]["time_bucket"] == "2026-10-25T12:10"
    # alias 不能悬挂：所有 source_event_ids 都能在 alias 里找到
    assert set(without_anchor["alias"]) == {"a2", "a3"}
    ids = [row["activity_id"] for row in without_anchor["activities"]]
    assert len(ids) == len(set(ids))


def test_input_order_does_not_change_activity_ids() -> None:
    """排序变化：打乱输入顺序 → 活动集合与 ID 完全一致。"""
    events = [
        _meal("p1", "12:10"),
        _meal("p2", "12:12", quote="加个辣椒"),
        _meal("p3", "19:10"),
    ]
    forward = build_activity_projection(events)
    backward = build_activity_projection(list(reversed(events)))
    assert [row["activity_id"] for row in forward["activities"]] == [
        row["activity_id"] for row in backward["activities"]
    ]
    assert forward["alias"] == backward["alias"]


def test_contract_documents_stability_boundaries() -> None:
    contract = build_activity_projection([])["contract"]
    assert "instance" in contract["id_stability"] and "snapshot_local" in contract["id_stability"]
    assert contract["stability_guarantees"] and contract["stability_boundaries"]
    assert "identity" in contract["migration"]
    assert any("锚点" in item for item in contract["stability_boundaries"])
    assert "possible" in contract["grouping_evidence"]


def test_anchor_deletion_same_bucket_keeps_id_cross_bucket_changes_it() -> None:
    """删除锚点的**精确边界**：同桶内删除 → ID 不变；跨桶删除 → ID 变化（契约内）。"""
    same_bucket = [
        _meal("y1", "12:10", data={"order_id": "p"}),
        _meal("y2", "12:12", data={"order_id": "p"}),
    ]
    full = build_activity_projection(same_bucket)
    assert len(full["activities"]) == 1
    stable_id = full["activities"][0]["activity_id"]
    after = build_activity_projection([same_bucket[1]])
    assert after["activities"][0]["activity_identity"]["time_bucket"] == "2026-10-25T12:10"
    assert after["activities"][0]["activity_id"] == stable_id

    cross_bucket = [
        _meal("x1", "12:10", data={"order_id": "o"}),
        _meal("x2", "12:25", data={"order_id": "o"}),
    ]
    joined = build_activity_projection(cross_bucket)
    assert len(joined["activities"]) == 1
    joined_id = joined["activities"][0]["activity_id"]
    dropped = build_activity_projection([cross_bucket[1]])
    assert dropped["activities"][0]["activity_identity"]["time_bucket"] == "2026-10-25T12:20"
    assert dropped["activities"][0]["activity_id"] != joined_id, "跨桶删除锚点必须暴露 ID 变化"


# --------------------------------------------------------------------------
# 5. 三次返工（2026-10-25）：撤销词表合并、结构化实例键、快照身份
# --------------------------------------------------------------------------

def _order(eid: str, clock: str, *, order_id: str | None = None, amount: float | None = None,
           quote: str = "点了一份鸭腿饭", rec_id: str = "watch_A") -> dict:
    event = _event(
        event_id=eid,
        title="点了一份鸭腿饭",
        categories=["food", "expense"],
        activity_type="meal",
        time={"precision": "exact", "absolute": f"2026-10-25T{clock}:00+08:00"},
        evidence={"quote": quote, "source_label": "self", "recording_id": rec_id,
                  "reported_at": f"2026-10-25T{clock}:00+08:00"},
    )
    data: dict = {}
    if order_id:
        data["order_id"] = order_id
    if amount is not None:
        data["amount"] = amount
    if data:
        event["data"] = data
    return event


def test_same_phrase_new_order_is_not_merged() -> None:
    """「跟刚才一样，再来一份」= 新订单，不得并成一单。"""
    result = build_activity_projection([
        _order("o1", "12:10"),
        _order("o2", "12:14", quote="跟刚才一样，再来一份"),
    ])
    assert len(result["activities"]) == 2
    assert any(rel["type"] == "possible" for rel in result["relations"])


def test_different_order_id_is_hard_negative_over_any_text() -> None:
    """两个不同 order_id 是硬否定，优先于任何中文指代。"""
    result = build_activity_projection([
        _order("a", "12:10", order_id="ORD-1", quote="点了鸭腿饭"),
        _order("b", "12:12", order_id="ORD-2", quote="同一单再加一份"),
    ])
    assert len(result["activities"]) == 2
    bases = {row["activity_identity"]["basis"] for row in result["activities"]}
    assert bases == {"source_instance_id"}


def test_same_order_id_merges_regardless_of_time_gap() -> None:
    """同一 order_id → 同一实例，跨时间桶也合并，且 ID 由实例键派生。"""
    close = build_activity_projection([
        _order("s1", "12:10", order_id="ORD-9"),
        _order("s2", "12:12", order_id="ORD-9", quote="再加一份米饭"),
    ])
    assert len(close["activities"]) == 1
    far = build_activity_projection([
        _order("f1", "12:10", order_id="ORD-9"),
        _order("f2", "15:40", order_id="ORD-9", quote="同一单又加了一份"),
    ])
    assert len(far["activities"]) == 1
    assert far["activities"][0]["activity_identity"]["stable_across_revisions"] is True
    assert far["activities"][0]["activity_identity"]["persistent_reference_ok"] is True


def test_two_amount_bearing_purchases_never_merge_without_instance_id() -> None:
    """两笔带金额的购买（即使同对象、同会话、时间近）也不合并。"""
    result = build_activity_projection([
        _order("p1", "12:10", amount=15.0),
        _order("p2", "12:12", amount=15.0, quote="再来一份鸭腿饭"),
    ])
    assert len(result["activities"]) == 2


def test_snapshot_local_identity_is_labelled_not_persistent() -> None:
    result = build_activity_projection([_order("x1", "12:10")])
    identity = result["activities"][0]["activity_identity"]
    assert identity["basis"] == "snapshot_local"
    assert identity["stable_across_revisions"] is False
    assert identity["persistent_reference_ok"] is False
    assert "ordinal" not in identity


def test_neighbour_add_delete_does_not_drift_identity() -> None:
    """同桶多场：删除/插入**邻居**不得改变本场 ID（不再用 ordinal）。"""
    p1 = _order("p1", "12:10", amount=15.0)
    p2 = _order("p2", "12:14", amount=22.0)
    alone = build_activity_projection([p2])
    id_alone = alone["activities"][0]["activity_id"]
    with_neighbour = build_activity_projection([p1, p2])
    id_with = next(
        row["activity_id"] for row in with_neighbour["activities"] if "p2" in row["source_event_ids"]
    )
    assert id_alone == id_with
    earlier = _order("p0", "12:11", amount=9.0)
    inserted = build_activity_projection([earlier, p1, p2])
    id_inserted = next(
        row["activity_id"] for row in inserted["activities"] if "p2" in row["source_event_ids"]
    )
    assert id_inserted == id_alone
    assert len({row["activity_id"] for row in inserted["activities"]}) == len(inserted["activities"])


def test_ungrouped_records_are_still_listed() -> None:
    """未分组记录必须可见（不能因为"合并"丢掉原始子项）。"""
    result = build_activity_projection([
        _order("u1", "12:10"),
        _order("u2", "12:14", quote="跟刚才一样，再来一份"),
    ])
    assert set(result["unattached"]) == {"u1", "u2"}
    assert {event_id for row in result["activities"] for event_id in row["source_event_ids"]} == {"u1", "u2"}
