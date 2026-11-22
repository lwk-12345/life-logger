"""“需要我判断”的用途分类、飞轮写入和撤销。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from life_logger import judgment_queue as jq


def _item(sid, reason, date="2026-11-05", quote="原话"):
    return {"source_id": sid, "reason": reason, "group": "needs_review", "state": "open",
            "date": date, "recorded_at": f"{date}T10:00:00+08:00", "quote": quote, "segment_id": "s1"}


@pytest.fixture
def root(tmp_path, monkeypatch):
    jq.invalidate_cache()
    monkeypatch.setattr(jq, "_collect_pending_items", lambda root, records: ({
        "a": _item("a", "amount_missing"),
        "b": _item("b", "model_no_event_but_directive"),
        "c": _item("c", "unknown_speaker"),
        "d": _item("d", "low_confidence"),
        "e": _item("e", "low_confidence", date="2026-10-01"),
        "f": {**_item("f", "low_confidence"), "state": "ignored"},
        "g": {**_item("g", "deepseek_batch_failed"), "group": "batch_failure"},
    }, 0, []))
    return tmp_path


def test_each_open_item_lands_in_one_purpose_with_reason_and_effect(root):
    out = jq.build_queue(root, today="2026-11-05", flywheel_root=root / "fw")
    by = {row["key"]: row for row in out["purposes"]}
    assert out["total_open"] == 5
    assert {key: by[key]["total_open"] for key in by} == {"money": 1, "todo": 1, "voiceprint": 1, "scene": 2}
    assert by["scene"]["recent_open"] == 1 and by["scene"]["older_open"] == 1
    assert all(row["why"] and row["effect"] for row in out["purposes"])


def test_recent_only_by_default_and_filter_by_purpose(root):
    out = jq.build_queue(root, today="2026-11-05", flywheel_root=root / "fw")
    assert {row["source_id"] for row in out["items"]} == {"a", "b", "c", "d"}
    scoped = jq.build_queue(root, today="2026-11-05", purpose="scene", include_older=True, flywheel_root=root / "fw")
    assert {row["source_id"] for row in scoped["items"]} == {"d", "e"}


def test_media_answer_writes_negative_and_undo_removes_it(tmp_path):
    fw = tmp_path / "fw"
    jq.record_answer(_item("d", "low_confidence", quote="视频里的台词内容"), "media", "op1", flywheel_root=fw)
    assert len(list((fw / "negatives").glob("*.json"))) == 1
    assert jq.flywheel_stats(fw)["answers"] == 1
    record = jq.undo_answer("op1", flywheel_root=fw)
    assert record["purpose"] == "scene"
    assert list((fw / "negatives").glob("*.json")) == []
    assert jq.flywheel_stats(fw)["answers"] == 0


def test_voice_answer_counts_as_sample_and_uncertain_records_nothing(tmp_path):
    fw = tmp_path / "fw"
    jq.record_answer(_item("c", "unknown_speaker"), "personal", "op2", flywheel_root=fw)
    assert jq.flywheel_stats(fw)["voice_samples"] == 1
    assert jq.record_answer(_item("c", "unknown_speaker"), "uncertain", "op3", flywheel_root=fw) is None


def _lost(root: Path):
    path = root / "jobs" / "job-1" / "manual-events.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"events": [{
        "event_id": "m1", "artifact_path": "2026-10-03/events.json", "title": "咖啡",
        "time": {"absolute": None}, "data": {"expense": {"amount": 15.0}}, "evidence": {"quote": "记一笔"},
    }]}), encoding="utf-8")
    return path


def test_lost_manual_listed_until_decided_and_undo_returns_it(tmp_path):
    path = _lost(tmp_path)
    assert [row["event_id"] for row in jq.lost_manual_events(tmp_path, "2026-11-05")] == ["m1"]
    jq.decide_lost_manual(tmp_path, job_id="job-1", event_id="m1", action="restore")
    assert json.loads(path.read_text())["events"][0]["restored_date"] == "2026-10-03"
    assert jq.lost_manual_events(tmp_path, "2026-11-05") == []
    jq.decide_lost_manual(tmp_path, job_id="job-1", event_id="m1", action="undo")
    assert "restored_date" not in json.loads(path.read_text())["events"][0]
    assert len(jq.lost_manual_events(tmp_path, "2026-11-05")) == 1


def test_bad_job_id_rejected(tmp_path):
    with pytest.raises(ValueError):
        jq.decide_lost_manual(tmp_path, job_id="../../x", event_id="m1", action="restore")


class _Store:
    def __init__(self, root):
        self.root = Path(root)
        self.saved = []

    def create_job(self):
        path = self.root / "jobs" / "job-new"
        path.mkdir(parents=True, exist_ok=True)
        return "job-new"

    def save_manual_event(self, job_id, **kwargs):
        event = {"event_id": "manual_1", **kwargs}
        self.saved.append(event)
        path = self.root / "jobs" / job_id / "manual-events.json"
        path.write_text(json.dumps({"events": [{"event_id": "manual_1"}]}), encoding="utf-8")
        return event


def test_money_entry_writes_event_then_undo_removes_it(root):
    store = _Store(root)
    fw = root / "fw"
    out = jq.answer(store, {"source_id": "a", "operation": "enter_expense", "op_id": "op9", "amount": "32"}, flywheel_root=fw)
    event = store.saved[0]
    assert event["categories"] == ["expense"]
    assert event["data"]["expense"]["amount"] == 32.0
    assert event["artifact_path"] == "2026-11-05/events.json"
    assert out["state"] == "answered"
    assert "a" not in {row["source_id"] for row in jq.build_queue(root, today="2026-11-05", flywheel_root=fw)["items"]}
    jq.undo(store, {"source_id": "a", "op_id": "op9"}, flywheel_root=fw)
    assert json.loads((root / "jobs" / "job-new" / "manual-events.json").read_text())["events"] == []
    assert "a" in {row["source_id"] for row in jq.build_queue(root, today="2026-11-05", flywheel_root=fw)["items"]}


@pytest.mark.parametrize("amount", ["", "0", "-5", "abc", None])
def test_money_entry_rejects_bad_amount(root, amount):
    store = _Store(root)
    with pytest.raises(ValueError):
        jq.answer(store, {"source_id": "a", "operation": "enter_expense", "op_id": "x", "amount": amount}, flywheel_root=root / "fw")
    assert store.saved == []


def test_answer_rejects_item_not_open(root):
    with pytest.raises(ValueError):
        jq.answer(_Store(root), {"source_id": "f", "operation": "media", "op_id": "x"}, flywheel_root=root / "fw")


def test_entries_reach_event_view_with_real_store(root):
    """用真实 JobStore：入账和待办都要进当天视图，撤销后消失。"""
    from life_logger.event_view import build_event_view
    from life_logger.local_web import JobStore

    store = JobStore(root, recover=False)
    fw = root / "fw"
    jq.answer(store, {"source_id": "a", "operation": "enter_expense", "op_id": "real1", "amount": "32", "label": "买菜"}, flywheel_root=fw)
    jq.answer(store, {"source_id": "b", "operation": "enter_todo", "op_id": "real2"}, flywheel_root=fw)
    view = build_event_view(root, date_from="2026-11-05", date_to="2026-11-05", scope="provisional", view="display")
    assert sorted(cat for event in view["events"] for cat in event["categories"]) == ["expense", "todo"]
    money = next(event for event in view["events"] if "expense" in event["categories"])
    assert money["data"]["expense"]["amount"] == 32.0
    jq.undo(store, {"source_id": "a", "op_id": "real1"}, flywheel_root=fw)
    view = build_event_view(root, date_from="2026-11-05", date_to="2026-11-05", scope="provisional", view="display")
    assert [event["categories"] for event in view["events"]] == [["todo"]]


def _manual(root, event_id="m9", amount=16.0, day="2026-11-05"):
    path = root / "jobs" / "job-9" / "manual-events.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"events": [{
        "event_id": event_id, "artifact_path": f"{day}/events.json", "title": "买菜",
        "data": {"expense": {"amount": amount}},
    }]}), encoding="utf-8")


def _patch_day(monkeypatch, review=None):
    monkeypatch.setattr(jq, "load_ledger_review", lambda: dict(review or {}))
    monkeypatch.setattr(jq, "activities_for_day", lambda day, root=None: [{"items": [
        {"amount": 16, "fact_type": "expense", "action": "买菜花了十六", "spoken_at": "2026-11-05T12:49:21+08:00"},
        {"amount": 99, "fact_type": "none", "action": "聊价格"},
    ]}])


def test_same_day_same_amount_flagged_but_never_auto_removed(tmp_path, monkeypatch):
    _manual(tmp_path)
    _patch_day(monkeypatch)
    rows = jq.possible_duplicates(tmp_path, {"2026-11-05"})
    assert [(row["event_id"], row["matched_time"]) for row in rows] == [("m9", "12:49")]
    assert not (tmp_path / "ledger-review.json").exists()


def test_different_amount_or_non_expense_not_flagged(tmp_path, monkeypatch):
    _manual(tmp_path, amount=99.0)
    _patch_day(monkeypatch)
    assert jq.possible_duplicates(tmp_path, {"2026-11-05"}) == []


def test_decision_excludes_manual_and_undo_restores(tmp_path, monkeypatch):
    _manual(tmp_path)
    _patch_day(monkeypatch)
    jq.decide_duplicate(tmp_path, event_id="m9", action="same", date="2026-11-05")
    review = json.loads((tmp_path / "ledger-review.json").read_text())
    assert review["decisions"]["m9"]["action"] == "exclude"
    assert jq.possible_duplicates(tmp_path, {"2026-11-05"}) == []
    jq.decide_duplicate(tmp_path, event_id="m9", action="undo")
    assert "m9" not in json.loads((tmp_path / "ledger-review.json").read_text())["decisions"]
    assert len(jq.possible_duplicates(tmp_path, {"2026-11-05"})) == 1


def test_separate_keeps_both_and_stops_asking(tmp_path, monkeypatch):
    _manual(tmp_path)
    _patch_day(monkeypatch)
    jq.decide_duplicate(tmp_path, event_id="m9", action="separate")
    assert jq.possible_duplicates(tmp_path, {"2026-11-05"}) == []
    assert "m9" not in json.loads((tmp_path / "ledger-review.json").read_text())["decisions"]
