"""New captures preserve raw transcript coordinates without identity inference."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from life_logger.voice_triggers import scan_voice_triggers


def _source(**overrides):
    source = {
        "recording_id": "rec_001",
        "session_id": "SESSION_001",
        "segment_id": "rec_001_seg_000003",
        "start_offset_seconds": 4.34,
        "end_offset_seconds": 7.89,
        "absolute_started_at": "2026-10-24T06:34:17.153000+00:00",
        "absolute_ended_at": "2026-10-24T06:34:21.013000+00:00",
    }
    source.update(overrides)
    return source


def test_old_call_without_source_keeps_existing_metadata_and_no_invented_fields() -> None:
    capture = scan_voice_triggers("记一下：买牛奶", timestamp="2026-10-24T06:34:17+00:00")[0]
    assert capture.id
    assert capture.metadata == {"status": "pending"}
    assert "source_provenance" not in capture.metadata


def test_new_call_attaches_whitelisted_coordinates_and_units() -> None:
    capture = scan_voice_triggers(
        "记一下：买牛奶",
        timestamp="2026-10-24T06:34:17+00:00",
        source_provenance=_source(),
    )[0]
    provenance = capture.metadata["source_provenance"]
    assert provenance["schema"] == "capture-source-v1"
    assert provenance["status"] == "complete"
    assert provenance["recording_id"] == "rec_001"
    assert provenance["session_id"] == "SESSION_001"
    assert provenance["segment_id"] == "rec_001_seg_000003"
    assert provenance["start_offset_seconds"] == 4.34
    assert provenance["end_offset_seconds"] == 7.89
    assert provenance["absolute_started_at"] == "2026-10-24T06:34:17.153000+00:00"
    assert provenance["time_reference"] == "recording_start"
    assert provenance["time_unit"] == "seconds"
    assert "utterance_id" not in provenance
    assert "text_hash" not in provenance
    # The existing trigger status remains intact.
    assert capture.metadata["status"] == "pending"


def test_different_segments_same_sentence_are_not_merged() -> None:
    first = scan_voice_triggers("记一下：买牛奶", source_provenance=_source(segment_id="seg-A"))[0]
    second = scan_voice_triggers("记一下：买牛奶", source_provenance=_source(segment_id="seg-B"))[0]
    assert first.id != second.id
    assert first.metadata["source_provenance"]["segment_id"] == "seg-A"
    assert second.metadata["source_provenance"]["segment_id"] == "seg-B"


def test_same_segment_different_offsets_remain_distinct_coordinates() -> None:
    first = scan_voice_triggers(
        "记一下：买牛奶",
        source_provenance=_source(start_offset_seconds=10.0, end_offset_seconds=11.0),
    )[0]
    second = scan_voice_triggers(
        "记一下：买牛奶",
        source_provenance=_source(start_offset_seconds=20.0, end_offset_seconds=21.0),
    )[0]
    assert first.metadata["source_provenance"]["start_offset_seconds"] == 10.0
    assert second.metadata["source_provenance"]["start_offset_seconds"] == 20.0
    assert first.id != second.id


def test_missing_source_is_partial_and_paths_tokens_are_not_persisted() -> None:
    capture = scan_voice_triggers(
        "记一下：买牛奶",
        source_provenance={
            "recording_id": "$HOME/secret.wav",
            "session_id": "token=super-secret",
            "segment_id": "seg-ok",
        },
    )[0]
    provenance = capture.metadata["source_provenance"]
    assert provenance["status"] == "partial"
    assert "recording_id" not in provenance
    assert "session_id" not in provenance
    assert provenance["segment_id"] == "seg-ok"
    assert "recording_id" in provenance["missing_fields"]
    assert "$HOME" not in json.dumps(provenance, ensure_ascii=False)
    assert "super-secret" not in json.dumps(provenance, ensure_ascii=False)


def test_explicit_empty_source_is_unknown_not_fabricated() -> None:
    capture = scan_voice_triggers("记一下：买牛奶", source_provenance={})[0]
    provenance = capture.metadata["source_provenance"]
    assert provenance["status"] == "partial"
    assert provenance["missing_fields"]
    assert "recording_id" not in provenance
    assert "segment_id" not in provenance


def test_watch_formal_reader_passes_session_and_row_coordinates(tmp_path: Path) -> None:
    from life_logger.watch_formal_asr import WatchFormalASRProcessor

    root = tmp_path / "local-web"
    output = root / "watch-sessions" / "watch-device" / "SESSION_X" / "formal-asr" / "segments" / "000001"
    output.mkdir(parents=True)
    (output / "run-summary.json").write_text(json.dumps({
        "status": "completed", "transcript_count": 1,
    }))
    (output / "transcript.json").write_text(json.dumps([{
        "segment_id": "watch_000001_000001",
        "recording_id": "rec_watch_1",
        "start_offset_seconds": 0.5,
        "end_offset_seconds": 4.0,
        "absolute_started_at": "2026-10-24T06:34:17.153000+00:00",
        "absolute_ended_at": "2026-10-24T06:34:20.653000+00:00",
        "text": "记一下：买牛奶",
        "source_label": "self",
        "acoustic_proximity": "near",
    }], ensure_ascii=False))
    (output / "recordings.json").write_text("[]")

    processor = WatchFormalASRProcessor.__new__(WatchFormalASRProcessor)
    processor.store = SimpleNamespace(root=root)
    rows = processor._read_segment_result(
        output, 1, 0, "2026-10-24T06:34:16+00:00",
        device_id="watch-device", session_id="SESSION_X",
    )
    assert len(rows) == 1
    captures = json.loads((root.parent / "quick_captures.json").read_text())
    provenance = captures[0]["metadata"]["source_provenance"]
    assert provenance["recording_id"] == "rec_watch_1"
    assert provenance["session_id"] == "SESSION_X"
    assert provenance["segment_id"] == "watch_000001_000001"
    assert provenance["start_offset_seconds"] == 0.5
    assert provenance["end_offset_seconds"] == 4.0
    assert provenance["time_reference"] == "recording_start"
    assert provenance["time_unit"] == "seconds"
