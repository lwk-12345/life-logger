"""daily_brief 旁挂 activity_links：结构完整、不改 events / 金额。"""
from __future__ import annotations

import copy

import life_logger.event_view as event_view
from life_logger.daily_brief import build_daily_brief

EVENTS = [
    {"event_id": "evt_parent", "event_key": "live:1", "title": "健身",
     "data": {"activity_ref": {"namespace": "activity", "id": "act_1"}},
     "time": {"absolute": "2026-10-24T09:00:00+08:00", "precision": "minute"}},
    {"event_id": "evt_child", "event_key": "live:2", "title": "卧推",
     "parent_event_id": "evt_parent", "canonical_event_id": "evt_parent",
     "data": {"activity_ref": {"namespace": "activity", "id": "act_1"}},
     "time": {"absolute": "2026-10-24T09:10:00+08:00", "precision": "minute"}},
    {"event_id": "evt_order", "event_key": "live:3", "title": "买咖啡",
     "evidence": {"order_binding": True}, "source_session": "sess_1",
     "expense": {"amount": 18}, "time": {"absolute": "2026-10-24T10:00:00+08:00", "precision": "minute"}},
]


def _fake_event_view(*_args, **_kwargs):
    return {"events": copy.deepcopy(EVENTS), "metadata": {"timezone": "Asia/Shanghai", "sources": {}}}


def test_activity_links_sidecar_present_and_structured(monkeypatch, tmp_path):
    monkeypatch.setattr(event_view, "build_event_view", _fake_event_view)
    out = build_daily_brief(tmp_path, date="2026-10-24")
    assert "activity_links" in out
    links = out["activity_links"]
    assert links["schema"] == "activity-links-v1"
    for key in ("edges", "members", "unlinked", "conflicts", "transactions", "stats", "contract"):
        assert key in links, key
    relations = {e["relation"] for e in links["edges"]}
    assert "child_of" in relations
    assert "member_of" in relations


def test_events_and_amounts_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(event_view, "build_event_view", _fake_event_view)
    out = build_daily_brief(tmp_path, date="2026-10-24")
    assert [e["event_id"] for e in out["events"]] == [e["event_id"] for e in EVENTS]
    order = next(e for e in out["events"] if e["event_id"] == "evt_order")
    assert order["expense"]["amount"] == 18, "交易金额不得被活动关系改写或吞并"


def test_activity_cards_still_present(monkeypatch, tmp_path):
    monkeypatch.setattr(event_view, "build_event_view", _fake_event_view)
    out = build_daily_brief(tmp_path, date="2026-10-24")
    assert "activity_cards" in out and isinstance(out["activity_cards"], list)
