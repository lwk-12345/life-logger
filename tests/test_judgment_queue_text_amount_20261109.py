"""2026-11-09：活动条目 amount 是文字（"一百八十二亿"，外放新闻）时 /api/judgment-queue 整体 400。"""
from __future__ import annotations

import json
from pathlib import Path

from life_logger import judgment_queue as jq


def test_as_amount_rejects_text_and_bool():
    assert jq._as_amount("一百八十二亿") is None
    assert jq._as_amount(True) is None
    assert jq._as_amount(None) is None
    assert jq._as_amount("15.2") == 15.2
    assert jq._as_amount(5) == 5.0


def test_possible_duplicates_skips_text_amount(tmp_path: Path, monkeypatch):
    job = tmp_path / "jobs" / "job-1"
    job.mkdir(parents=True)
    (job / "manual-events.json").write_text(json.dumps({"events": [
        {"event_id": "manual_evt_1", "artifact_path": "2026-11-04/events.json", "title": "面条",
         "data": {"expense": {"amount": 15.2}}},
        {"event_id": "manual_evt_2", "artifact_path": "2026-11-04/events.json", "title": "坏数据",
         "data": {"expense": {"amount": "很多"}}},
    ]}, ensure_ascii=False), encoding="utf-8")
    activities = [{"items": [
        {"fact_type": "media", "amount": "一百八十二亿"},
        {"fact_type": "expense", "amount": "一百八十二亿"},
        {"fact_type": "expense", "amount": 15.2, "action": "买面条", "spoken_at": "2026-11-04T12:30:00+08:00"},
    ]}]
    monkeypatch.setattr(jq, "load_ledger_review", lambda *a, **k: {})
    monkeypatch.setattr(jq, "_read_state", lambda root: {})
    monkeypatch.setattr(jq, "activities_for_day", lambda day, root=None: activities)
    found = jq.possible_duplicates(tmp_path, {"2026-11-04"})
    assert [f["event_id"] for f in found] == ["manual_evt_1"]
    assert found[0]["matched_time"] == "12:30"
