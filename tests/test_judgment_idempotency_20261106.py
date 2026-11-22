"""F1：同一 op_id 重发只入账一笔；撤销后账本归零；并发也只写一次。"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from life_logger import judgment_queue as jq


def _item(sid, reason, date="2026-11-05", quote="原话"):
    return {"source_id": sid, "reason": reason, "group": "needs_review", "state": "open",
            "date": date, "recorded_at": f"{date}T10:00:00+08:00", "quote": quote,
            "segment_id": "s1"}


@pytest.fixture
def root(tmp_path, monkeypatch):
    jq.invalidate_cache()
    monkeypatch.setattr(jq, "_collect_pending_items", lambda root, records: ({
        "a": _item("a", "amount_missing"),
        "b": _item("b", "amount_missing"),
        "c": _item("c", "amount_missing"),
    }, 0, []))
    return tmp_path


class _Store:
    def __init__(self, root: Path):
        self.root = root
        self.saved = []
        self._n = 0

    def create_job(self):
        self._n += 1
        job = f"job-{self._n}"
        (self.root / "jobs" / job).mkdir(parents=True, exist_ok=True)
        return job

    def save_manual_event(self, job_id, **kwargs):
        event = {"event_id": f"manual_{job_id}", **kwargs}
        self.saved.append(event)
        path = self.root / "jobs" / job_id / "manual-events.json"
        path.write_text(json.dumps({"events": [event]}), encoding="utf-8")
        return event


def _ledger_total(store: _Store) -> float:
    total = 0.0
    for event in store.saved:
        total += float(((event.get("data") or {}).get("expense") or {}).get("amount") or 0)
    return total


def test_same_op_id_twice_writes_one_event(root):
    store = _Store(root)
    fw = root / "fw"
    first = jq.answer(store, {"source_id": "a", "operation": "enter_expense",
                              "op_id": "op-same", "amount": "14"}, flywheel_root=fw)
    second = jq.answer(store, {"source_id": "a", "operation": "enter_expense",
                               "op_id": "op-same", "amount": "14"}, flywheel_root=fw)

    assert len(store.saved) == 1, f"同 op_id 写了两笔：{len(store.saved)}"
    assert second.get("idempotent") is True
    assert second["event_id"] == first["event_id"]
    assert _ledger_total(store) == 14.0


def test_undo_after_duplicate_returns_ledger_to_zero(root):
    store = _Store(root)
    fw = root / "fw"
    jq.answer(store, {"source_id": "a", "operation": "enter_expense", "op_id": "op-u", "amount": "14"}, flywheel_root=fw)
    jq.answer(store, {"source_id": "a", "operation": "enter_expense", "op_id": "op-u", "amount": "14"}, flywheel_root=fw)
    jq.undo(store, {"source_id": "a", "op_id": "op-u"}, flywheel_root=fw)

    remaining = []
    for path in (root / "jobs").glob("*/manual-events.json"):
        remaining.extend(json.loads(path.read_text()).get("events") or [])
    assert remaining == [], f"撤销后仍有事件：{remaining}"
    assert _ledger_total(_Store(root)) == 0.0


def test_concurrent_same_op_id_writes_once(root):
    store = _Store(root)
    fw = root / "fw"
    errors = []

    def run():
        try:
            jq.answer(store, {"source_id": "a", "operation": "enter_expense",
                              "op_id": "op-conc", "amount": "9"}, flywheel_root=fw)
        except Exception as error:  # noqa: BLE001 - 并发下允许的唯一失败是「已不在列表」
            errors.append(repr(error))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(store.saved) == 1, f"并发写了 {len(store.saved)} 笔：{store.saved}"
    assert _ledger_total(store) == 9.0


def test_different_op_ids_write_one_each(root):
    store = _Store(root)
    fw = root / "fw"
    jq.answer(store, {"source_id": "a", "operation": "enter_expense", "op_id": "op-1", "amount": "3"}, flywheel_root=fw)
    jq.answer(store, {"source_id": "b", "operation": "enter_expense", "op_id": "op-2", "amount": "4"}, flywheel_root=fw)
    assert len(store.saved) == 2
    assert _ledger_total(store) == 7.0


def test_replay_does_not_rewrite_flywheel_answer(root):
    store = _Store(root)
    fw = root / "fw"
    jq.answer(store, {"source_id": "a", "operation": "enter_expense", "op_id": "op-f", "amount": "5"}, flywheel_root=fw)
    path = fw / "answers" / "op-f.json"
    before = path.read_text()
    jq.answer(store, {"source_id": "a", "operation": "enter_expense", "op_id": "op-f", "amount": "5"}, flywheel_root=fw)
    assert path.read_text() == before
