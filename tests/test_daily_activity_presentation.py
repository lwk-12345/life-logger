"""daily_brief 旁挂 activity_presentation：只读投影，不改 events/cards/links/ledger。"""
from __future__ import annotations

import copy

import life_logger.event_view as event_view
from life_logger.daily_brief import build_daily_brief

EVENTS = [
    {"event_id": "evt_a", "event_key": "live:1", "title": "健身", "activity_type": "exercise",
     "data": {"activity_ref": {"namespace": "activity", "id": "act_gym"}, "exercise": {"body_part": "胸"}},
     "time": {"absolute": "2026-10-24T09:00:00+08:00", "precision": "minute"},
     "evidence": {"quote": "健身", "recording_id": "rec_1", "segment_id": "seg_1"}},
    {"event_id": "evt_b", "event_key": "live:2", "title": "第二组", "activity_type": "exercise",
     "data": {"activity_ref": {"namespace": "activity", "id": "act_gym"}},
     "time": {"absolute": "2026-10-24T09:30:00+08:00", "precision": "minute"},
     "evidence": {"quote": "第二组"}},
    {"event_id": "evt_pay1", "event_key": "live:3", "title": "蛋白粉", "activity_type": "expense",
     "data": {"activity_ref": {"namespace": "activity", "id": "act_buy"}, "expense": {"amount": 25}},
     "time": {"absolute": "2026-10-24T10:00:00+08:00", "precision": "minute"}, "evidence": {"quote": "蛋白粉"}},
    {"event_id": "evt_pay2", "event_key": "live:4", "title": "咖啡", "activity_type": "expense",
     "data": {"activity_ref": {"namespace": "activity", "id": "act_buy"}, "expense": {"amount": 18}},
     "time": {"absolute": "2026-10-24T10:05:00+08:00", "precision": "minute"}, "evidence": {"quote": "咖啡"}},
    {"event_id": "evt_solo", "event_key": "live:5", "title": "打游戏", "activity_type": "game",
     "data": {"game": {"name": "艾尔登法环"}},
     "time": {"absolute": "2026-10-24T15:00:00+08:00", "precision": "minute"}, "evidence": {"quote": "打游戏"}},
    {"event_id": "evt_scope_a", "event_key": "live:6", "title": "跨 scope A", "activity_type": "exercise",
     "data": {"activity_ref": {"namespace": "activity", "id": "act_cross"}},
     "time": {"absolute": "2026-10-24T18:00:00+08:00", "precision": "minute"}, "evidence": {"quote": "A"}},
    {"event_id": "evt_scope_b", "event_key": "live:7", "title": "跨 scope B", "activity_type": "exercise",
     "data": {"activity_ref": {"namespace": "workout", "id": "act_cross"}},
     "time": {"absolute": "2026-10-24T18:10:00+08:00", "precision": "minute"}, "evidence": {"quote": "B"}},
]


def _fake_event_view(*_args, **_kwargs):
    return {"events": copy.deepcopy(EVENTS), "metadata": {"timezone": "Asia/Shanghai", "sources": {}}}


def _brief(monkeypatch, tmp_path):
    monkeypatch.setattr(event_view, "build_event_view", _fake_event_view)
    return build_daily_brief(tmp_path, date="2026-10-24")


def test_presentation_sidecar_present(monkeypatch, tmp_path):
    out = _brief(monkeypatch, tmp_path)
    assert "activity_presentation" in out
    pres = out["activity_presentation"]
    assert pres["schema"] == "activity-presentation-v1"
    assert set(pres) >= {"groups", "conflicts", "ungrouped_event_ids", "coverage", "contract"}


def test_events_cards_links_ledger_unchanged(monkeypatch, tmp_path):
    out = _brief(monkeypatch, tmp_path)
    assert [e["event_id"] for e in out["events"]] == [e["event_id"] for e in EVENTS]
    amounts = sorted(e["data"]["expense"]["amount"] for e in out["events"] if e.get("data", {}).get("expense"))
    assert amounts == [18, 25], "金额不得被展示层改写或求和"
    assert isinstance(out["activity_cards"], list) and out["activity_cards"]
    assert out["activity_links"]["schema"] == "activity-links-v1"


def test_same_group_multi_transactions_all_referenced(monkeypatch, tmp_path):
    out = _brief(monkeypatch, tmp_path)
    pres = out["activity_presentation"]
    buy = next(g for g in pres["groups"] if g["activity_ref"] == "activity:act_buy")
    assert buy["summary"]["transaction_count"] == 2
    assert pres["coverage"]["transactions_dropped"] == 0


def test_no_ref_event_remains_ungrouped(monkeypatch, tmp_path):
    out = _brief(monkeypatch, tmp_path)
    assert "evt_solo" in out["activity_presentation"]["ungrouped_event_ids"]


def test_cross_scope_not_merged(monkeypatch, tmp_path):
    out = _brief(monkeypatch, tmp_path)
    pres = out["activity_presentation"]
    grouped = {e for g in pres["groups"] for e in g["member_event_ids"]}
    assert not ({"evt_scope_a", "evt_scope_b"} <= grouped), "namespace 不同不得合并"


def test_conflict_not_grouped(monkeypatch, tmp_path):
    import life_logger.activity_links as links_mod
    monkeypatch.setattr(event_view, "build_event_view", _fake_event_view)
    conflicting = {
        "schema": "activity-links-v1",
        "edges": [], "unlinked": [], "transactions": {}, "stats": {}, "contract": {},
        "members": {"activity:act_gym": ["evt_a", "evt_b"]},
        "member_meta": {"activity:act_gym": {"conflicted": ["evt_b"], "declaration_consistent": False}},
    }
    monkeypatch.setattr(links_mod, "build_activity_links", lambda events: conflicting)
    out = build_daily_brief(tmp_path, date="2026-10-24")
    pres = out["activity_presentation"]
    assert pres["groups"] == [], "冲突不得成组"
    assert pres["conflicts"], "冲突需显式记录"
    assert pres["conflicts"][0]["reason"] == "declaration_conflict"


def test_unknown_state_chain_not_reported_done(monkeypatch, tmp_path):
    out = _brief(monkeypatch, tmp_path)
    pres = out["activity_presentation"]
    for group in pres["groups"]:
        assert group["summary"]["amounts_summed"] is False
        for member in group["members"]:
            assert member.get("state", "unknown") in {"unknown", "done", "in_progress", "planned", "cancelled"}
    gym = next(g for g in pres["groups"] if g["activity_ref"] == "activity:act_gym")
    assert "done" not in gym["summary"]["states"], "未提供状态不得推断完成"
