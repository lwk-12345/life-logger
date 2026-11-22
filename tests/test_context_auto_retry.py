import json
from pathlib import Path

import pytest

from life_logger.context_auto_retry import inspect_retry_candidate


def _candidate(tmp_path: Path, *, status: str = "failed", batches=None):
    file_id = "a" * 64
    file_root = tmp_path / "files" / file_id
    result_dir = file_root / "result"
    asr_dir = file_root / "asr" / "2026-10-06"
    result_dir.mkdir(parents=True)
    asr_dir.mkdir(parents=True)
    transcript = [{
        "segment_id": "seg-1", "recording_id": "rec-1",
        "start_offset_seconds": 0, "end_offset_seconds": 1,
        "absolute_started_at": "2026-10-06T09:00:00+08:00",
        "absolute_ended_at": "2026-10-06T09:00:01+08:00",
        "text": "private transcript text",
    }]
    recordings = [{"recording_id": "rec-1", "source_file": "recording.m4a"}]
    attribution = [{**transcript[0], "source_label": "self"}]
    transcript_path = asr_dir / "transcript.json"
    recordings_path = asr_dir / "recordings.json"
    attribution_path = result_dir / "source-attribution.json"
    transcript_path.write_text(json.dumps(transcript), encoding="utf-8")
    recordings_path.write_text(json.dumps(recordings), encoding="utf-8")
    attribution_path.write_text(json.dumps(attribution), encoding="utf-8")
    report_path = result_dir / "context-extraction.json"
    report_path.write_text(json.dumps({"status": status, "batches": batches or []}), encoding="utf-8")
    state = {
        "job_id": "job-auto-retry",
        "status": "completed",
        "files": [{"sha256": file_id, "status": "completed"}],
    }
    return state, file_id, report_path, transcript_path, recordings_path, attribution_path


def _transient_batch(**overrides):
    return {
        "status": "failed",
        "failure_kind": "http_error",
        "retryable_transient": True,
        "http_status": 503,
        **overrides,
    }


def test_accepts_only_all_failed_transient_batches_and_returns_safe_fingerprint(tmp_path):
    state, file_id, *_ = _candidate(tmp_path, batches=[_transient_batch()])

    result = inspect_retry_candidate(tmp_path, state)

    assert result is not None
    assert result["file_ids"] == [file_id]
    assert result["failure_kinds"] == ["http_error"]
    assert len(result["input_fingerprint"]) == 64
    assert "private transcript text" not in repr(result)


@pytest.mark.parametrize("batches", [
    [_transient_batch(), {"status": "completed"}],
    [_transient_batch(), {"status": "failed", "failure_kind": "timeout"}],
    [],
])
def test_rejects_partial_success_and_missing_batch_evidence(tmp_path, batches):
    state, *_ = _candidate(tmp_path, batches=batches)

    assert inspect_retry_candidate(tmp_path / "auth", state) is None


def test_rejects_degraded_report_even_when_batch_metadata_looks_transient(tmp_path):
    state, *_ = _candidate(tmp_path, status="degraded", batches=[_transient_batch()])

    assert inspect_retry_candidate(tmp_path, state) is None


def test_rejects_missing_batch_key_and_nontransient_4xx(tmp_path):
    state, *_ = _candidate(tmp_path, batches=[{
        "status": "failed", "failure_kind": "http_error",
        "retryable_transient": True, "http_status": 401,
    }])

    assert inspect_retry_candidate(tmp_path, state) is None


@pytest.mark.parametrize("missing", ["transcript", "recordings", "attribution"])
def test_rejects_missing_asr_or_attribution_checkpoint(tmp_path, missing):
    state, _, _, transcript, recordings, attribution = _candidate(
        tmp_path, batches=[_transient_batch()]
    )
    {"transcript": transcript, "recordings": recordings, "attribution": attribution}[missing].unlink()

    assert inspect_retry_candidate(tmp_path, state) is None


def test_rejects_job_that_is_not_completed_or_has_skipped_recovery_target(tmp_path):
    state, _, _, _, _, attribution = _candidate(
        tmp_path, batches=[_transient_batch()]
    )
    state["status"] = "partial"
    assert inspect_retry_candidate(tmp_path, state) is None

    state["status"] = "completed"
    # Replace the expected checkpoint with an unreadable/missing target.
    attribution.unlink()
    assert inspect_retry_candidate(tmp_path, state) is None


def test_fingerprint_changes_when_report_or_checkpoint_input_changes(tmp_path):
    state, _, report, transcript, *_ = _candidate(tmp_path, batches=[_transient_batch()])
    first = inspect_retry_candidate(tmp_path, state)
    report.write_text(json.dumps({"status": "failed", "batches": [
        _transient_batch(http_status=429)
    ]}), encoding="utf-8")
    second = inspect_retry_candidate(tmp_path, state)
    transcript.write_text(transcript.read_text(encoding="utf-8") + " ", encoding="utf-8")
    third = inspect_retry_candidate(tmp_path, state)

    assert first and second and third
    assert first["input_fingerprint"] != second["input_fingerprint"]
    assert second["input_fingerprint"] != third["input_fingerprint"]


def test_ignores_completed_extraction_files(tmp_path):
    state, file_id, *_ = _candidate(tmp_path, status="completed")

    assert inspect_retry_candidate(tmp_path, state) is None


def test_retryability_is_derived_from_kind_not_conflicting_legacy_flag(tmp_path):
    state, *_ = _candidate(tmp_path, batches=[_transient_batch(
        retryable_transient=False,
    )])
    assert inspect_retry_candidate(tmp_path, state) is not None

    state, *_ = _candidate(tmp_path / "auth", batches=[{
        "status": "failed",
        "failure_kind": "http_error",
        "retryable_transient": True,
        "http_status": 401,
    }])
    assert inspect_retry_candidate(tmp_path / "auth", state) is None
