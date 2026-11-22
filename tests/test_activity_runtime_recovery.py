import datetime as dt
import json
from types import SimpleNamespace

import pytest

import life_logger.activity_timeline_runtime as module
from life_logger.activity_timeline_runtime import ActivityTimelineRuntime


def fixed_clock(monkeypatch):
    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 26, 10, 30, tzinfo=module.BEIJING_TZ)

    monkeypatch.setattr(module, "datetime", SimpleNamespace(datetime=Clock, timedelta=dt.timedelta,
                                                          timezone=dt.timezone))


@pytest.mark.parametrize("capture_day", ["2026-10-26", "2026-10-25"])
def test_late_record_stays_on_capture_day_and_restart_never_repeats_model(tmp_path, monkeypatch, capture_day):
    fixed_clock(monkeypatch)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"activity_timeline": {"enabled": True,
                          "enabled_at": "2026-10-26T10:20:00+08:00"}}))
    row = {"source_id": "late", "segment_id": "seg", "row_id": 1,
           "timestamp": capture_day+"T09:10:00+08:00", "text": "现场询价"}
    calls = []

    def extract(prompt):
        calls.append(prompt)
        return {"activities": [{"activity_id": "a", "title": "询价", "items": [
            {"item_id": "i", "action": "询价", "status": "unknown", "occurred_at": None,
             "evidence": [{"source_id": "late", "segment_id": "seg", "row_id": 1, "quote": "现场询价"}]}]}]}

    runtime = ActivityTimelineRuntime(tmp_path, config, extract)
    monkeypatch.setattr(runtime, "scan_recent_asr_records", lambda **_: [row])
    assert runtime.tick()["status"] == "success"
    assert len(runtime.read_day(capture_day)["activities"]) == 1
    restarted = ActivityTimelineRuntime(tmp_path, config, extract)
    monkeypatch.setattr(restarted, "scan_recent_asr_records", lambda **_: [row])
    restarted.tick()
    assert len(calls) == 1


def test_existing_source_id_does_not_authorize_new_quote_or_different_segment(tmp_path):
    runtime = ActivityTimelineRuntime(tmp_path, tmp_path / "config.json")
    old = {"source_id": "s", "segment_id": "seg", "row_id": 1, "quote": "原话"}
    payload = {"activities": [{"items": [{"evidence": [old]}]}]}
    response = {"activities": [], "unassociated_items": [{"item_id": "fake", "evidence": [
        {**old, "quote": "未说过的话"}]}]}
    with pytest.raises(ValueError):
        runtime._verify_and_sanitize_v2_response(response, [], dt.datetime.now(module.BEIJING_TZ), payload)


def test_bounded_scanner_eventually_visits_more_than_one_batch(tmp_path):
    runtime = ActivityTimelineRuntime(tmp_path, tmp_path / "config.json")
    for index in range(24):
        directory = tmp_path / "watch-sessions/d/s/formal-asr/segments" / str(index)
        directory.mkdir(parents=True)
        (directory / "state.json").write_text(json.dumps({"status": "completed", "rows": [{
            "segment_id": str(index), "text": "record", "absolute_started_at": "2026-10-26T10:00:00+08:00"}]}))
    visited = set()
    for _ in range(5):
        visited.update(r["segment_id"] for r in runtime.scan_recent_asr_records(max_files=7))
    assert visited == {str(i) for i in range(24)}
