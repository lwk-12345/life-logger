"""End-to-end read/write contract: scan -> QuickCaptureStore -> dashboard."""

from __future__ import annotations

import json
from pathlib import Path

from life_logger.daily_dashboard import DailyDashboardBuilder
from life_logger.voice_triggers import QuickCaptureStore, scan_voice_triggers


def _source(**overrides):
    source = {
        "recording_id": "rec_contract_1",
        "session_id": "SESSION_CONTRACT",
        "segment_id": "rec_contract_1_seg_000004",
        "start_offset_seconds": 3725.5,
        "end_offset_seconds": 3729.0,
        "absolute_started_at": "2026-10-24T06:34:17.153000+00:00",
        "absolute_ended_at": "2026-10-24T06:34:21.013000+00:00",
    }
    source.update(overrides)
    return source


def _build(root: Path):
    return DailyDashboardBuilder(root).build_for_date("2026-10-24")


def _capture_items(root: Path):
    return [item for item in _build(root).captured_todos if item.get("_origin") == "capture"]


def test_real_scan_store_dashboard_preserves_nine_source_fields(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    store = QuickCaptureStore(root / "quick_captures.json")
    store.append(scan_voice_triggers("记一下：明天买牛奶", timestamp="2026-10-24T06:34:17.153000+00:00", source_provenance=_source()))
    item = _capture_items(root)[0]
    provenance = item["capture_provenance"]

    assert provenance["source_contract"]["status"] == "complete"
    assert provenance["source_contract"]["missing_fields"] == []
    assert provenance["recording_id"] == "rec_contract_1"
    assert provenance["session_id"] == "SESSION_CONTRACT"
    assert provenance["segment_id"] == "rec_contract_1_seg_000004"
    assert provenance["start_offset_seconds"] == 3725.5
    assert provenance["end_offset_seconds"] == 3729.0
    assert provenance["absolute_started_at"] == "2026-10-24T06:34:17.153000+00:00"
    assert provenance["absolute_ended_at"] == "2026-10-24T06:34:21.013000+00:00"
    assert provenance["time_reference"] == "recording_start"
    assert provenance["time_unit"] == "seconds"
    assert provenance["source_contract"]["time_reference"] == "recording_start"
    assert provenance["source_contract"]["time_unit"] == "seconds"
    assert provenance["status"] == "resolved"
    assert provenance["same_utterance"] is None


def test_real_partial_source_preserves_contract_status_and_missing_fields(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    store = QuickCaptureStore(root / "quick_captures.json")
    store.append(scan_voice_triggers(
        "记一下：明天买牛奶",
        timestamp="2026-10-24T06:34:17.153000+00:00",
        source_provenance={"recording_id": "rec_partial", "segment_id": "seg_partial"},
    ))
    item = _capture_items(root)[0]
    provenance = item["capture_provenance"]
    assert provenance["source_contract"]["status"] == "partial"
    assert set(provenance["source_contract"]["missing_fields"]) >= {
        "session_id", "start_offset_seconds", "end_offset_seconds",
        "absolute_started_at", "absolute_ended_at",
    }
    assert provenance["status"] == "partial"
    assert provenance["reason"] == "source_contract_partial"
    assert provenance["recording_id"] == "rec_partial"
    assert provenance["session_id"] is None


def test_missing_source_remains_unknown_without_fabrication(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    store = QuickCaptureStore(root / "quick_captures.json")
    store.append(scan_voice_triggers("记一下：明天买牛奶", timestamp="2026-10-24T06:34:17.153000+00:00"))
    item = _capture_items(root)[0]
    provenance = item["capture_provenance"]
    assert provenance["status"] == "unknown"
    assert provenance["reason"] == "source_not_recorded"
    assert provenance["source_contract"]["status"] == "unknown"
    assert provenance["recording_id"] is None
    assert provenance["segment_id"] is None


def test_legacy_flat_metadata_is_read_as_fallback(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    root.mkdir(parents=True)
    (root / "quick_captures.json").write_text(json.dumps([{
        "id": "legacy-flat",
        "trigger_type": "todo",
        "raw_text": "记一下：买牛奶",
        "extracted_content": "买牛奶",
        "captured_at": "2026-10-24T06:34:17.153000+00:00",
        "metadata": {
            "status": "pending",
            "recording_id": "rec_legacy",
            "segment_id": "seg_legacy",
            "start_offset_seconds": 12.5,
            "end_offset_seconds": 20.0,
            "absolute_started_at": "2026-10-24T06:34:17.153000+00:00",
            "absolute_ended_at": "2026-10-24T06:34:24.653000+00:00",
            "time_reference": "recording_start",
            "time_unit": "seconds",
        },
    }], ensure_ascii=False))
    item = _capture_items(root)[0]
    provenance = item["capture_provenance"]
    assert provenance["recording_id"] == "rec_legacy"
    assert provenance["segment_id"] == "seg_legacy"
    assert provenance["start_offset_seconds"] == 12.5
    assert provenance["source_contract"]["schema"] == "legacy-flat"
    assert provenance["source_contract"]["status"] == "partial"


def test_nested_and_flat_conflict_is_explicit_not_silently_overwritten(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    root.mkdir(parents=True)
    (root / "quick_captures.json").write_text(json.dumps([{
        "id": "conflict-read",
        "trigger_type": "todo",
        "raw_text": "记一下：买牛奶",
        "extracted_content": "买牛奶",
        "captured_at": "2026-10-24T06:34:17.153000+00:00",
        "metadata": {
            "status": "pending",
            "recording_id": "rec_flat",
            "segment_id": "seg_flat",
            "source_provenance": {
                "schema": "capture-source-v1",
                "status": "complete",
                "missing_fields": [],
                "recording_id": "rec_nested",
                "segment_id": "seg_nested",
                "session_id": "SESS_NESTED",
                "start_offset_seconds": 1.0,
                "end_offset_seconds": 2.0,
                "absolute_started_at": "2026-10-24T06:34:17+00:00",
                "absolute_ended_at": "2026-10-24T06:34:18+00:00",
                "time_reference": "recording_start",
                "time_unit": "seconds",
            },
        },
    }], ensure_ascii=False))
    item = _capture_items(root)[0]
    provenance = item["capture_provenance"]
    assert provenance["status"] == "conflict"
    assert provenance["reason"] == "source_identity_conflict"
    assert "recording_id" in provenance["conflict_fields"]
    assert "segment_id" in provenance["conflict_fields"]
    assert provenance["recording_id"] == "rec_nested"
    assert provenance["source_contract"]["status"] == "complete"


def test_nested_vs_source_evidence_conflict_keeps_both_branches_and_selects_nested(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    root.mkdir(parents=True)
    nested = {
        "schema": "capture-source-v1", "status": "complete", "missing_fields": [],
        "recording_id": "rec_nested", "segment_id": "seg_nested", "session_id": "SESS_NESTED",
        "start_offset_seconds": 1.0, "end_offset_seconds": 2.0,
        "absolute_started_at": "2026-10-24T06:34:17+00:00",
        "absolute_ended_at": "2026-10-24T06:34:18+00:00",
        "time_reference": "recording_start", "time_unit": "seconds",
    }
    (root / "quick_captures.json").write_text(json.dumps([{
        "id": "nested-vs-evidence",
        "trigger_type": "todo",
        "raw_text": "记一下：买牛奶",
        "extracted_content": "买牛奶",
        "captured_at": "2026-10-24T06:34:17.153000+00:00",
        "metadata": {"status": "pending", "source_provenance": nested},
        "source_evidence": {"recording_id": "rec_flat", "segment_id": "seg_flat"},
    }], ensure_ascii=False))
    provenance = _capture_items(root)[0]["capture_provenance"]
    assert provenance["status"] == "conflict"
    assert set(provenance["conflict_fields"]) >= {"recording_id", "segment_id"}
    assert provenance["recording_id"] == "rec_nested"
    assert provenance["segment_id"] == "seg_nested"
    assert provenance["selected_branch"] == "nested"
    assert provenance["source_contract"]["status"] == "complete"
    assert provenance["conflicting_values"]["recording_id"]["nested"] == "rec_nested"
    assert provenance["conflicting_values"]["recording_id"]["source_evidence"] == "rec_flat"


def test_bool_and_negative_offsets_are_invalid_not_coerced(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    root.mkdir(parents=True)
    (root / "quick_captures.json").write_text(json.dumps([{
        "id": "bad-offset",
        "trigger_type": "todo",
        "raw_text": "记一下：买牛奶",
        "extracted_content": "买牛奶",
        "captured_at": "2026-10-24T06:34:17.153000+00:00",
        "metadata": {
            "status": "pending",
            "source_provenance": {
                "schema": "capture-source-v1", "status": "partial", "missing_fields": ["start_offset_seconds", "end_offset_seconds"],
                "recording_id": "rec_bad", "segment_id": "seg_bad",
                "start_offset_seconds": True, "end_offset_seconds": -2.0,
            },
        },
    }], ensure_ascii=False))
    provenance = _capture_items(root)[0]["capture_provenance"]
    assert provenance["start_offset_seconds"] is None
    assert provenance["end_offset_seconds"] is None
    assert provenance["time_reference"] is None
    assert provenance["source_contract"]["status"] == "partial"
    assert "start_offset_seconds" in provenance["source_contract"]["missing_fields"]


def test_all_capture_types_get_provenance_from_shared_entry(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    store = QuickCaptureStore(root / "quick_captures.json")
    timestamp = "2026-10-24T06:34:17.153000+00:00"
    store.append(scan_voice_triggers("记一下：明天买牛奶", timestamp=timestamp, source_provenance=_source()))
    store.append(scan_voice_triggers("查一下VPN怎么设置", timestamp=timestamp, source_provenance=_source()))
    store.append(scan_voice_triggers("买午餐花了35元", timestamp=timestamp, source_provenance=_source()))
    store.append(scan_voice_triggers("有个点子：把手表心率与录音做时间对齐", timestamp=timestamp, source_provenance=_source()))

    day = _build(root)
    assert len(day.captured_todos) == 2
    assert len(day.captured_expenses) == 1
    assert len(day.captured_ideas) == 1
    items = day.captured_todos + day.captured_expenses + day.captured_ideas
    assert len({item["id"] for item in items}) == 4
    for item in items:
        provenance = item["capture_provenance"]
        assert provenance["status"] == "resolved"
        assert provenance["source_contract"]["status"] == "complete"
        assert provenance["recording_id"] == "rec_contract_1"
        assert provenance["session_id"] == "SESSION_CONTRACT"
        assert provenance["segment_id"] == "rec_contract_1_seg_000004"
        assert provenance["time_reference"] == "recording_start"
        assert provenance["time_unit"] == "seconds"
    assert day.captured_expenses[0]["metadata"]["amount"] == 35.0
    assert day.captured_expenses[0]["capture_provenance"]["start_offset_seconds"] == 3725.5
