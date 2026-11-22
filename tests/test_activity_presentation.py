"""activity_presentation：显式同 scope 声明的有界紧凑汇总。"""
from __future__ import annotations

import copy

from life_logger.activity_cards import build_activity_cards
from life_logger.activity_links import build_activity_links
from life_logger.daily_cognitive_session import build_expense_ledger
from life_logger.activity_presentation import build_activity_presentation


def _events():
    return [
        {"event_id": "evt_fit", "event_key": "live:1", "title": "健身", "activity_type": "exercise",
         "data": {"activity_ref": {"namespace": "activity", "id": "act_gym"},
                  "exercise": {"body_part": "胸"},
                  "exercises": [{"name": "卧推", "sets": [{"set": 1, "reps": 10, "weight_kg": 10}]}]},
         "time": {"absolute": "2026-10-24T09:00:00+08:00", "precision": "minute"},
         "evidence": {"quote": "健身", "recording_id": "rec_1", "segment_id": "seg_1"}},
        {"event_id": "evt_set2", "event_key": "live:2", "title": "第二组卧推", "activity_type": "exercise",
         "data": {"activity_ref": {"namespace": "activity", "id": "act_gym"},
                  "exercises": [{"name": "卧推", "sets": [{"set": 2, "reps": 8, "weight_kg": 10}]}]},
         "time": {"absolute": "2026-10-24T09:40:00+08:00", "precision": "minute"},
         "evidence": {"quote": "第二组卧推"}},
        {"event_id": "evt_game", "event_key": "live:3", "title": "打游戏", "activity_type": "game",
         "data": {"game": {"name": "艾尔登法环", "rounds": 2}},
         "time": {"absolute": "2026-10-24T15:00:00+08:00", "precision": "minute"},
         "evidence": {"quote": "打游戏"}},
    ]


def _build(events):
    cards = build_activity_cards(events, ledger=build_expense_ledger(events)).get("cards", [])
    links = build_activity_links(events)
    return cards, links, build_activity_presentation(cards, links)


def test_explicit_same_scope_groups_into_compact_summary():
    cards, links, out = _build(_events())
    assert out["schema"] == "activity-presentation-v1"
    assert len(out["groups"]) == 1
    group = out["groups"][0]
    assert group["activity_ref"] == "activity:act_gym"
    assert group["basis"]["label"] == "显式声明 · 未核实"
    assert group["basis"]["activity_entity_verified"] is False
    assert sorted(group["member_event_ids"]) == ["evt_fit", "evt_set2"]
    assert group["summary"]["amounts_summed"] is False
    assert group["compact"] is True


def test_members_keep_time_state_location_transactions():
    cards, links, out = _build(_events())
    member = next(m for m in out["groups"][0]["members"] if m["event_id"] == "evt_fit")
    assert member["time"]["clock"] == "09:00"
    assert member["state"] == "unknown"           # 未提到状态 → unknown，不是 done
    assert member["location"] is None
    assert member["transaction_ids"] == []        # 健身事件无交易引用
    assert "data.exercise.body_part" in member["facts_paths"]


def test_original_events_kept_and_nothing_dropped():
    cards, links, out = _build(_events())
    assert out["ungrouped_event_ids"] == ["evt_game"]
    assert out["coverage"]["events_total"] == 3
    assert out["coverage"]["events_grouped"] == 2
    assert out["coverage"]["events_ungrouped"] == 1
    assert out["coverage"]["transactions_dropped"] == 0


def test_multiple_transactions_all_referenced_without_summing():
    events = [
        {"event_id": "evt_whey", "event_key": "live:1", "title": "蛋白粉", "activity_type": "expense",
         "data": {"activity_ref": {"namespace": "activity", "id": "act_buy"}, "expense": {"amount": 25}},
         "time": {"absolute": "2026-10-24T09:40:00+08:00", "precision": "minute"}, "evidence": {"quote": "蛋白粉"}},
        {"event_id": "evt_coffee", "event_key": "live:2", "title": "咖啡", "activity_type": "expense",
         "data": {"activity_ref": {"namespace": "activity", "id": "act_buy"}, "expense": {"amount": 18}},
         "time": {"absolute": "2026-10-24T09:45:00+08:00", "precision": "minute"}, "evidence": {"quote": "咖啡"}},
    ]
    cards, links, out = _build(events)
    group = out["groups"][0]
    tx = group["summary"]["transaction_ids"]
    assert set(tx) == {"evt_whey", "evt_coffee"}, tx
    assert group["summary"]["transaction_count"] == 2
    assert "amount" not in group["summary"], "不得对金额求和或计算总额"


def test_no_grouping_without_explicit_declaration():
    events = [
        {"event_id": "e1", "event_key": "live:1", "title": "午饭", "activity_type": "meal",
         "data": {"meal": {"items": [{"name": "面"}]}},
         "time": {"absolute": "2026-10-24T12:00:00+08:00", "precision": "minute"}, "evidence": {"quote": "午饭"}},
        {"event_id": "e2", "event_key": "live:2", "title": "午饭2", "activity_type": "meal",
         "time": {"absolute": "2026-10-24T12:05:00+08:00", "precision": "minute"}, "evidence": {"quote": "午饭"}},
    ]
    cards, links, out = _build(events)
    assert out["groups"] == [], "时间相近/文本相同不得合并"
    assert len(out["ungrouped_event_ids"]) == 2


def test_conflict_not_grouped_and_kept_ungrouped():
    cards = [
        {"source_event_id": "e1", "title": "A", "state": "unknown", "time": {"clock": "09:00"},
         "transactions": [], "facts": [], "unknown_fields": []},
        {"source_event_id": "e2", "title": "B", "state": "unknown", "time": {"clock": "09:10"},
         "transactions": [], "facts": [], "unknown_fields": []},
    ]
    links = {
        "members": {"activity:act_x": ["e1", "e2"]},
        "member_meta": {"activity:act_x": {"conflicted": ["e2"], "declaration_consistent": False}},
    }
    out = build_activity_presentation(cards, links)
    assert out["groups"] == []
    assert out["conflicts"] and out["conflicts"][0]["reason"] == "declaration_conflict"
    assert sorted(out["ungrouped_event_ids"]) == ["e1", "e2"]


def test_inputs_not_mutated():
    events = _events()
    cards = build_activity_cards(events, ledger=build_expense_ledger(events)).get("cards", [])
    links = build_activity_links(events)
    snap_cards, snap_links = copy.deepcopy(cards), copy.deepcopy(links)
    build_activity_presentation(cards, links)
    assert cards == snap_cards
    assert links == snap_links


def test_long_activity_is_summary_only_with_references():
    events = []
    for i in range(12):
        events.append({"event_id": f"e{i}", "event_key": f"live:{i}", "title": f"组内 {i}",
                       "activity_type": "exercise",
                       "data": {"activity_ref": {"namespace": "activity", "id": "act_long"}},
                       "time": {"absolute": f"2026-10-24T{9 + i // 6}:{(i % 6) * 10:02d}:00+08:00", "precision": "minute"},
                       "evidence": {"quote": f"q{i}"}})
    cards, links, out = _build(events)
    group = out["groups"][0]
    assert group["summary"]["member_count"] == 12
    assert len(group["member_event_ids"]) == 12
    assert group["compact"] is True
    assert "members" in group and len(group["members"]) == 12
    assert group["summary"]["amounts_summed"] is False


def test_member_keeps_title_and_fact_values():
    cards = [
        {"source_event_id": "m1", "title": "鸭腿", "kind": "meal", "state": "unknown",
         "time": {"clock": "12:00"}, "transactions": [],
         "facts": [{"path": "data.meal.items[0].name", "value": "鸭腿"},
                   {"path": "data.meal.items[0].flavor", "value": "微辣"}], "unknown_fields": []},
        {"source_event_id": "m2", "title": "午餐", "kind": "meal", "state": "unknown",
         "time": {"clock": "12:10"}, "transactions": [], "facts": [], "unknown_fields": []},
    ]
    links = {"members": {"activity:act_meal": ["m1", "m2"]},
             "member_meta": {"activity:act_meal": {"declaration_consistent": True, "conflicted": []}}}
    out = build_activity_presentation(cards, links)
    member = out["groups"][0]["members"][0]
    assert member["title"] == "鸭腿"
    assert {f["path"]: f["value"] for f in member["facts"]}["data.meal.items[0].flavor"] == "微辣"
    assert "鸭腿" in out["groups"][0]["summary"]["titles"]


def test_mixed_group_and_ungrouped_transactions_not_reported_dropped():
    cards = [
        {"source_event_id": "a", "title": "A", "state": "unknown", "time": {"clock": "09:00"},
         "transactions": [{"event_id": "tx_a", "amount": 10}], "facts": [], "unknown_fields": []},
        {"source_event_id": "b", "title": "B", "state": "unknown", "time": {"clock": "09:10"},
         "transactions": [{"event_id": "tx_b", "amount": 20}], "facts": [], "unknown_fields": []},
        {"source_event_id": "c", "title": "C", "state": "unknown", "time": {"clock": "10:00"},
         "transactions": [{"event_id": "tx_c", "amount": 30}], "facts": [], "unknown_fields": []},
    ]
    links = {"members": {"activity:act_mix": ["a", "b"]},
             "member_meta": {"activity:act_mix": {"declaration_consistent": True, "conflicted": []}}}
    out = build_activity_presentation(cards, links)
    cov = out["coverage"]
    assert cov["transactions_total"] == 3
    assert cov["transactions_referenced_in_groups"] == 2
    assert cov["transactions_retained_ungrouped"] == 1
    assert cov["transactions_dropped"] == 0
    assert out["retained_ungrouped_transactions"] == {"c": ["tx_c"]}


def test_missing_member_meta_does_not_group():
    cards = [
        {"source_event_id": "p", "title": "P", "state": "unknown", "time": {"clock": "09:00"},
         "transactions": [], "facts": [], "unknown_fields": []},
        {"source_event_id": "q", "title": "Q", "state": "unknown", "time": {"clock": "09:10"},
         "transactions": [], "facts": [], "unknown_fields": []},
    ]
    out = build_activity_presentation(cards, {"members": {"activity:act_z": ["p", "q"]}})
    assert out["groups"] == []
    assert out["conflicts"][0]["reason"] == "declaration_unverified"
    assert sorted(out["ungrouped_event_ids"]) == ["p", "q"]


def test_duplicate_members_counted_once():
    cards = [
        {"source_event_id": "d1", "title": "D1", "state": "unknown", "time": {"clock": "09:00"},
         "transactions": [], "facts": [], "unknown_fields": []},
        {"source_event_id": "d2", "title": "D2", "state": "unknown", "time": {"clock": "09:05"},
         "transactions": [], "facts": [], "unknown_fields": []},
    ]
    links = {"members": {"activity:act_dup": ["d1", "d2", "d1"]},
             "member_meta": {"activity:act_dup": {"declaration_consistent": True, "conflicted": []}}}
    out = build_activity_presentation(cards, links)
    assert out["groups"][0]["summary"]["member_count"] == 2
    assert out["groups"][0]["member_event_ids"] == ["d1", "d2"]


def test_transaction_id_namespace_separate_from_ledger_event_id():
    cards = [
        {"source_event_id": "t1", "title": "T1", "state": "unknown", "time": {"clock": "09:00"},
         "transactions": [{"transaction_id": "txn_1", "event_id": "ledger_evt_1", "amount": 5}],
         "facts": [], "unknown_fields": []},
        {"source_event_id": "t2", "title": "T2", "state": "unknown", "time": {"clock": "09:10"},
         "transactions": [{"event_id": "ledger_evt_2", "amount": 6}], "facts": [], "unknown_fields": []},
    ]
    links = {"members": {"activity:act_t": ["t1", "t2"]},
             "member_meta": {"activity:act_t": {"declaration_consistent": True, "conflicted": []}}}
    out = build_activity_presentation(cards, links)
    m1 = out["groups"][0]["members"][0]
    assert m1["transaction_ids"] == ["txn_1"]
    assert m1["ledger_event_ids"] == ["ledger_evt_1"]
    assert set(out["groups"][0]["summary"]["transaction_ids"]) == {"txn_1", "ledger_evt_2"}
    assert out["coverage"]["transactions_dropped"] == 0

# ===== 冲突可追溯（3号 actor 冲突在 members 剩单成员时消失） =====
def test_single_member_conflict_still_recorded():
    cards = [{"source_event_id": "e1", "title": "A", "state": "unknown", "time": {"clock": "09:00"},
              "transactions": [], "facts": [], "unknown_fields": []}]
    links = {"members": {"activity:act_one": ["e1"]},
             "member_meta": {"activity:act_one": {"conflicted": ["e1"], "declaration_consistent": False}}}
    out = build_activity_presentation(cards, links)
    assert out["groups"] == []
    assert len(out["conflicts"]) == 1
    conflict = out["conflicts"][0]
    assert conflict["activity_ref"] == "activity:act_one"
    assert conflict["event_ids"] == ["e1"]
    assert conflict["conflicted_event_ids"] == ["e1"]
    assert conflict["reason"] == "declaration_conflict"
    assert conflict["source"] == "member_meta"


def test_upstream_links_conflicts_preserved_when_members_missing():
    links = {"members": {}, "member_meta": {},
             "conflicts": [{"activity_ref": "activity:act_actor", "event_ids": ["e1", "e2"],
                            "conflicted_event_ids": ["e2"], "reason": "actor_conflict"}]}
    out = build_activity_presentation([], links)
    assert len(out["conflicts"]) == 1
    c = out["conflicts"][0]
    assert c["activity_ref"] == "activity:act_actor"
    assert c["event_ids"] == ["e1", "e2"]
    assert c["conflicted_event_ids"] == ["e2"]
    assert c["reason"] == "actor_conflict"
    assert c["source"] == "links.conflicts"


def test_conflicts_not_double_counted():
    links = {"members": {"activity:act_x": ["e1", "e2"]},
             "member_meta": {"activity:act_x": {"conflicted": ["e2"], "declaration_consistent": False}},
             "conflicts": [{"activity_ref": "activity:act_x", "event_ids": ["e1", "e2"],
                            "conflicted_event_ids": ["e2"], "reason": "declaration_conflict"}]}
    cards = [{"source_event_id": e, "title": e, "state": "unknown", "time": {"clock": "09:00"},
              "transactions": [], "facts": [], "unknown_fields": []} for e in ("e1", "e2")]
    out = build_activity_presentation(cards, links)
    assert len(out["conflicts"]) == 1, "同一冲突不得重复计"


def test_conflict_does_not_change_members_or_state():
    cards = [{"source_event_id": e, "title": e, "state": "unknown", "time": {"clock": "09:00"},
              "transactions": [{"event_id": "tx_" + e, "amount": 10}], "facts": [], "unknown_fields": []}
             for e in ("e1", "e2")]
    snapshot = copy.deepcopy(cards)
    links = {"members": {"activity:act_x": ["e1", "e2"]},
             "member_meta": {"activity:act_x": {"conflicted": ["e2"], "declaration_consistent": False}}}
    out = build_activity_presentation(cards, links)
    assert cards == snapshot
    assert sorted(out["ungrouped_event_ids"]) == ["e1", "e2"]
    assert out["coverage"]["transactions_dropped"] == 0, "冲突不丢交易"
