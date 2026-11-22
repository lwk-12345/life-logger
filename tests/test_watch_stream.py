from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from life_logger.watch_stream import (
    IncompleteWatchSessionError,
    WatchSessionConflictError,
    WatchSessionStore,
)


def _sha256(body: bytes) -> str:
    return f"sha256:{hashlib.sha256(body).hexdigest()}"


def _start(store: WatchSessionStore) -> None:
    store.start_session(
        device_id="watch-s5-test",
        session_id="session-001",
        started_at="2026-09-25T11:00:00+08:00",
    )


def test_session_summary_reports_received_audio_without_private_paths(
    tmp_path: Path,
) -> None:
    store = WatchSessionStore(tmp_path)
    _start(store)
    _put(store, sequence=0, body=b"audio")
    sessions = store.list_session_summaries()
    assert len(sessions) == 1
    assert sessions[0]["received_segments"] == 1
    assert sessions[0]["received_duration_ms"] == 60_000
    assert sessions[0]["state"] == "receiving"
    assert "segments" not in sessions[0]


def test_session_summary_exposes_reserved_completion_job_id(tmp_path: Path) -> None:
    store = WatchSessionStore(tmp_path)
    _start(store)
    store.mark_completion_job("watch-s5-test", "session-001", "job-reserved-1234")

    summary = store.list_session_summaries()[0]

    assert summary["completion_job_id"] == "job-reserved-1234"
    assert summary["job_id"] is None


def test_coverage_intervals_preserve_gaps_and_cross_midnight(tmp_path: Path) -> None:
    store = WatchSessionStore(tmp_path)
    store.start_session(
        device_id="watch-s5-test",
        session_id="midnight-session",
        started_at="2026-09-25T23:59:30+08:00",
    )
    store.receive_segment(
        device_id="watch-s5-test",
        session_id="midnight-session",
        sequence=0,
        start_offset_ms=0,
        duration_ms=10_000,
        checksum=_sha256(b"first"),
        body=b"first",
    )
    store.receive_segment(
        device_id="watch-s5-test",
        session_id="midnight-session",
        sequence=2,
        start_offset_ms=40_000,
        duration_ms=5_000,
        checksum=_sha256(b"second"),
        body=b"second",
    )

    intervals = store.list_coverage_intervals()

    assert intervals == [
        {
            "device_id": "watch-s5-test",
            "session_id": "midnight-session",
            "sequence": 0,
            "start_at": "2026-09-25T23:59:30+08:00",
            "end_at": "2026-09-25T23:59:40+08:00",
        },
        {
            "device_id": "watch-s5-test",
            "session_id": "midnight-session",
            "sequence": 2,
            "start_at": "2026-09-26T00:00:10+08:00",
            "end_at": "2026-09-26T00:00:15+08:00",
        },
    ]


def test_coverage_intervals_forward_battery_snapshot(tmp_path: Path) -> None:
    store = WatchSessionStore(tmp_path)
    store.start_session(
        device_id="watch-s5-test",
        session_id="battery-session",
        started_at="2026-09-25T11:00:00+08:00",
    )
    store.receive_segment(
        device_id="watch-s5-test",
        session_id="battery-session",
        sequence=0,
        start_offset_ms=0,
        duration_ms=60_000,
        checksum=_sha256(b"seg0"),
        body=b"seg0",
        battery_level_percent=88,
        battery_state="unplugged",
    )
    store.receive_segment(
        device_id="watch-s5-test",
        session_id="battery-session",
        sequence=1,
        start_offset_ms=64_000,
        duration_ms=60_000,
        checksum=_sha256(b"seg1"),
        body=b"seg1",
    )
    intervals = store.list_coverage_intervals()
    assert len(intervals) == 2
    assert intervals[0]["battery_snapshot"] == {
        "level_percent": 88,
        "state": "unplugged",
    }
    assert "battery_snapshot" not in intervals[1]


def test_coverage_intervals_skip_sessions_and_segments_with_bad_metadata(
    tmp_path: Path,
) -> None:
    store = WatchSessionStore(tmp_path)
    store.start_session(
        device_id="watch-s5-test",
        session_id="session-001",
        started_at="2026-09-25T11:00:00+08:00",
    )
    _put(store, sequence=0, body=b"valid")
    manifest_path = (
        tmp_path
        / "watch-s5-test"
        / "session-001"
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["segments"].extend(
        [
            {"sequence": 1, "start_offset_ms": 60_000},
            {"sequence": "bad", "start_offset_ms": 70_000, "duration_ms": 5_000},
            {"sequence": 3, "start_offset_ms": -1, "duration_ms": 5_000},
            {"sequence": 4, "start_offset_ms": 80_000, "duration_ms": 0},
        ]
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    missing_start_path = (
        tmp_path / "watch-s5-test" / "missing-start-session" / "manifest.json"
    )
    missing_start_path.parent.mkdir(parents=True)
    missing_start_path.write_text(
        json.dumps(
            {
                "device_id": "watch-s5-test",
                "session_id": "missing-start-session",
                "segments": [
                    {"sequence": 0, "start_offset_ms": 0, "duration_ms": 5000}
                ],
            }
        ),
        encoding="utf-8",
    )

    intervals = store.list_coverage_intervals()

    assert intervals == [
        {
            "device_id": "watch-s5-test",
            "session_id": "session-001",
            "sequence": 0,
            "start_at": "2026-09-25T11:00:00+08:00",
            "end_at": "2026-09-25T11:01:00+08:00",
        }
    ]


def _put(
    store: WatchSessionStore,
    *,
    sequence: int,
    body: bytes,
    start_ms: int | None = None,
    step_count: int | None = None,
    steps_today: int | None = None,
    steps_date: str | None = None,
) -> dict[str, object]:
    return store.receive_segment(
        device_id="watch-s5-test",
        session_id="session-001",
        sequence=sequence,
        start_offset_ms=sequence * 60_000 if start_ms is None else start_ms,
        duration_ms=60_000,
        checksum=_sha256(body),
        body=body,
        step_count=step_count,
        steps_today=steps_today,
        steps_date=steps_date,
    )


def test_segment_ack_means_file_and_manifest_are_persisted(tmp_path: Path) -> None:
    store = WatchSessionStore(tmp_path / "watch-sessions")
    _start(store)
    body = b"m4a-segment-0"

    receipt = _put(store, sequence=0, body=body)

    segment = (
        tmp_path
        / "watch-sessions"
        / "watch-s5-test"
        / "session-001"
        / "segments"
        / "segment-000000.m4a"
    )
    manifest_path = segment.parents[1] / "manifest.json"
    assert receipt["duplicate"] is False
    assert receipt["stored_bytes"] == len(body)
    assert receipt["sha256"] == _sha256(body)
    assert segment.read_bytes() == body
    assert manifest_path.is_file()


def test_segment_battery_snapshot_is_optional_and_persisted(tmp_path: Path) -> None:
    store = WatchSessionStore(tmp_path / "watch-sessions")
    _start(store)
    body = b"battery-aware-segment"

    store.receive_segment(
        device_id="watch-s5-test",
        session_id="session-001",
        sequence=0,
        start_offset_ms=0,
        duration_ms=1000,
        checksum=_sha256(body),
        body=body,
        battery_level_percent=76,
        battery_state="unplugged",
    )
    store.receive_segment(
        device_id="watch-s5-test",
        session_id="session-001",
        sequence=0,
        start_offset_ms=0,
        duration_ms=1000,
        checksum=_sha256(body),
        body=body,
    )

    manifest = store.load_session("watch-s5-test", "session-001")
    assert manifest["segments"][0]["battery_snapshot"] == {
        "level_percent": 76,
        "state": "unplugged",
    }

    _put(store, sequence=1, body=b"legacy-client-segment")
    manifest = store.load_session("watch-s5-test", "session-001")
    assert "battery_snapshot" not in manifest["segments"][1]


def test_duplicate_retry_merges_missing_battery_snapshot_fields(tmp_path: Path) -> None:
    store = WatchSessionStore(tmp_path / "watch-sessions")
    _start(store)
    body = b"battery-partial-snapshot"
    common = {
        "device_id": "watch-s5-test",
        "session_id": "session-001",
        "sequence": 0,
        "start_offset_ms": 0,
        "duration_ms": 1000,
        "checksum": _sha256(body),
        "body": body,
    }

    store.receive_segment(**common, battery_level_percent=76)
    store.receive_segment(**common, battery_state="unplugged")

    manifest = store.load_session("watch-s5-test", "session-001")
    assert manifest["segments"][0]["battery_snapshot"] == {
        "level_percent": 76,
        "state": "unplugged",
    }


def test_duplicate_retry_does_not_overwrite_battery_snapshot(tmp_path: Path) -> None:
    store = WatchSessionStore(tmp_path / "watch-sessions")
    _start(store)
    body = b"battery-conflicting-snapshot"
    common = {
        "device_id": "watch-s5-test",
        "session_id": "session-001",
        "sequence": 0,
        "start_offset_ms": 0,
        "duration_ms": 1000,
        "checksum": _sha256(body),
        "body": body,
    }

    store.receive_segment(
        **common, battery_level_percent=76, battery_state="unplugged"
    )
    store.receive_segment(
        **common, battery_level_percent=80, battery_state="charging"
    )

    manifest = store.load_session("watch-s5-test", "session-001")
    assert manifest["segments"][0]["battery_snapshot"] == {
        "level_percent": 76,
        "state": "unplugged",
    }


@pytest.mark.parametrize(
    ("level_percent", "state"),
    [(101, "charging"), (-1, "unplugged"), (50, "unknown-state")],
)
def test_segment_rejects_invalid_battery_metadata(
    tmp_path: Path, level_percent: int, state: str
) -> None:
    store = WatchSessionStore(tmp_path / "watch-sessions")
    _start(store)
    body = b"battery-aware-segment"

    with pytest.raises(Exception, match="battery"):
        store.receive_segment(
            device_id="watch-s5-test",
            session_id="session-001",
            sequence=0,
            start_offset_ms=0,
            duration_ms=1000,
            checksum=_sha256(body),
            body=body,
            battery_level_percent=level_percent,
            battery_state=state,
        )


def test_segment_accepts_small_encoder_duration_padding(tmp_path: Path) -> None:
    store = WatchSessionStore(tmp_path / "watch-sessions")
    _start(store)
    body = b"encoded-audio"
    receipt = store.receive_segment(
        device_id="watch-s5-test",
        session_id="session-001",
        sequence=0,
        start_offset_ms=0,
        duration_ms=60_064,
        checksum=_sha256(body),
        body=body,
    )
    assert receipt["stored_bytes"] == len(body)


def test_duplicate_segment_retry_is_idempotent_and_acknowledged(tmp_path: Path) -> None:
    root = tmp_path / "watch-sessions"
    store = WatchSessionStore(root)
    _start(store)
    body = b"same-segment"

    first = _put(store, sequence=0, body=body)
    second = _put(store, sequence=0, body=body)

    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["sha256"] == _sha256(body)
    manifest = store.load_session("watch-s5-test", "session-001")
    assert [segment["sequence"] for segment in manifest["segments"]] == [0]


def test_duplicate_retry_cannot_mutate_segment_timing_metadata(tmp_path: Path) -> None:
    store = WatchSessionStore(tmp_path / "watch-sessions")
    _start(store)
    body = b"same-segment"
    _put(store, sequence=0, body=body)

    with pytest.raises(Exception, match="first segment|timing"):
        store.receive_segment(
            device_id="watch-s5-test",
            session_id="session-001",
            sequence=0,
            start_offset_ms=1_000,
            duration_ms=59_000,
            checksum=_sha256(body),
            body=body,
        )

    manifest = store.load_session("watch-s5-test", "session-001")
    assert manifest["segments"][0]["start_offset_ms"] == 0
    assert manifest["segments"][0]["duration_ms"] == 60_000


@pytest.mark.parametrize(
    ("sequence", "start_ms"),
    [
        (0, 1),
    ],
)
def test_first_segment_must_start_at_zero(
    tmp_path: Path,
    sequence: int,
    start_ms: int,
) -> None:
    store = WatchSessionStore(tmp_path / "watch-sessions")
    _start(store)
    body = f"segment-{sequence}".encode()

    with pytest.raises(Exception, match="first segment"):
        _put(store, sequence=sequence, body=body, start_ms=start_ms)


def test_out_of_order_segments_must_keep_monotonic_start_offsets(tmp_path: Path) -> None:
    store = WatchSessionStore(tmp_path / "watch-sessions")
    _start(store)
    _put(store, sequence=2, body=b"segment-two", start_ms=2_000)

    with pytest.raises(Exception, match="increase"):
        _put(store, sequence=1, body=b"segment-one", start_ms=3_000)


def test_adjacent_segments_reject_large_overlap_but_allow_aac_padding(
    tmp_path: Path,
) -> None:
    store = WatchSessionStore(tmp_path / "watch-sessions")
    _start(store)
    body0 = b"segment-zero"
    store.receive_segment(
        device_id="watch-s5-test",
        session_id="session-001",
        sequence=0,
        start_offset_ms=0,
        duration_ms=60_064,
        checksum=_sha256(body0),
        body=body0,
    )
    _put(store, sequence=1, body=b"segment-one", start_ms=60_000)

    with pytest.raises(Exception, match="overlaps"):
        _put(store, sequence=2, body=b"segment-two", start_ms=60_500)


def test_adjacent_segments_allow_real_gap_without_forcing_fixed_chunk_length(
    tmp_path: Path,
) -> None:
    store = WatchSessionStore(tmp_path / "watch-sessions")
    _start(store)
    body0 = b"segment-zero"
    store.receive_segment(
        device_id="watch-s5-test",
        session_id="session-001",
        sequence=0,
        start_offset_ms=0,
        duration_ms=1_000,
        checksum=_sha256(body0),
        body=body0,
    )
    receipt = store.receive_segment(
        device_id="watch-s5-test",
        session_id="session-001",
        sequence=1,
        start_offset_ms=5_000,
        duration_ms=1_000,
        checksum=_sha256(b"segment-one"),
        body=b"segment-one",
    )

    assert receipt["ok"] is True


@pytest.mark.parametrize("duration_ms", [1, 61_000])
def test_segment_duration_accepts_supported_boundaries(
    tmp_path: Path,
    duration_ms: int,
) -> None:
    store = WatchSessionStore(tmp_path / "watch-sessions")
    _start(store)
    body = b"boundary"
    receipt = store.receive_segment(
        device_id="watch-s5-test",
        session_id="session-001",
        sequence=0,
        start_offset_ms=0,
        duration_ms=duration_ms,
        checksum=_sha256(body),
        body=body,
    )
    assert receipt["ok"] is True


def test_segment_duration_rejects_above_supported_maximum(tmp_path: Path) -> None:
    store = WatchSessionStore(tmp_path / "watch-sessions")
    _start(store)
    body = b"too-long"
    with pytest.raises(Exception, match="timing"):
        store.receive_segment(
            device_id="watch-s5-test",
            session_id="session-001",
            sequence=0,
            start_offset_ms=0,
            duration_ms=61_001,
            checksum=_sha256(body),
            body=body,
        )


def test_watch_fractional_iso_timestamp_is_idempotent_on_session_retry(
    tmp_path: Path,
) -> None:
    store = WatchSessionStore(tmp_path / "watch-sessions")
    parameters = {
        "device_id": "watch-s5-test",
        "session_id": "session-001",
        "started_at": "2026-09-25T06:20:00.000Z",
    }

    first = store.start_session(**parameters)
    retry = store.start_session(**parameters)

    assert first["duplicate"] is False
    assert retry["duplicate"] is True
    assert retry["session"]["started_at"] == "2026-09-25T06:20:00+00:00"


def test_reusing_segment_sequence_with_different_audio_conflicts(
    tmp_path: Path,
) -> None:
    store = WatchSessionStore(tmp_path / "watch-sessions")
    _start(store)
    _put(store, sequence=0, body=b"first-payload")

    with pytest.raises(WatchSessionConflictError):
        _put(store, sequence=0, body=b"different-payload")


def test_checksum_mismatch_is_rejected_without_creating_a_file(tmp_path: Path) -> None:
    store = WatchSessionStore(tmp_path / "watch-sessions")
    _start(store)
    body = b"corrupt-payload"

    with pytest.raises(ValueError, match="checksum"):
        store.receive_segment(
            device_id="watch-s5-test",
            session_id="session-001",
            sequence=0,
            start_offset_ms=0,
            duration_ms=60_000,
            checksum="sha256:" + "0" * 64,
            body=body,
        )

    manifest = store.load_session("watch-s5-test", "session-001")
    assert manifest["segments"] == []


def test_out_of_order_arrival_is_supported_but_completion_reports_missing(
    tmp_path: Path,
) -> None:
    store = WatchSessionStore(tmp_path / "watch-sessions")
    _start(store)
    _put(store, sequence=1, body=b"segment-one")

    with pytest.raises(IncompleteWatchSessionError) as error:
        store.complete_session(
            device_id="watch-s5-test",
            session_id="session-001",
            expected_segments=2,
            ended_at="2026-09-25T11:02:00+08:00",
        )

    assert error.value.missing_sequences == [0]
    manifest = store.load_session("watch-s5-test", "session-001")
    assert manifest["state"] == "receiving"
    assert manifest["completion_intent"]["expected_segments"] == 2
    assert manifest["completion_intent"]["ended_at"] == "2026-09-25T11:02:00+08:00"
    assert manifest["completion_intent"]["requested_at"]


def test_complete_session_requires_all_segments_and_is_idempotent(
    tmp_path: Path,
) -> None:
    store = WatchSessionStore(tmp_path / "watch-sessions")
    _start(store)
    _put(store, sequence=1, body=b"segment-one")
    _put(store, sequence=0, body=b"segment-zero")

    complete = store.complete_session(
        device_id="watch-s5-test",
        session_id="session-001",
        expected_segments=2,
        ended_at="2026-09-25T11:02:00+08:00",
    )
    repeated = store.complete_session(
        device_id="watch-s5-test",
        session_id="session-001",
        expected_segments=2,
        ended_at="2026-09-25T11:02:00+08:00",
    )

    assert complete["complete"] is True
    assert complete["received_segments"] == 2
    assert repeated["duplicate"] is True
    manifest = store.load_session("watch-s5-test", "session-001")
    assert manifest["state"] == "complete"
    assert manifest["expected_segments"] == 2
    summary = store.list_session_summaries()[0]
    assert summary["completion_pending"] is False


def test_complete_session_retry_is_answered_not_refused(tmp_path: Path) -> None:
    """A completed session is closed; the Watch re-sends `complete` on every sync until it
    gets its acknowledgement. Refusing the retry over a string comparison of `ended_at` meant
    the Watch retried forever (its clock runs hours ahead of the Mac's), so the duplicate path
    now answers with the existing job instead of 409-ing on metadata the retry cannot change.
    """
    store = WatchSessionStore(tmp_path / "watch-sessions")
    _start(store)
    _put(store, sequence=0, body=b"segment-zero")

    first = store.complete_session(
        device_id="watch-s5-test",
        session_id="session-001",
        expected_segments=1,
        ended_at="2026-09-25T11:01:00+08:00",
    )
    assert first["duplicate"] is False

    retry = store.complete_session(
        device_id="watch-s5-test",
        session_id="session-001",
        expected_segments=1,
        ended_at="2026-09-25T11:01:05+08:00",
    )
    assert retry["duplicate"] is True
    assert retry["complete"] is True

    manifest = store.load_session("watch-s5-test", "session-001")
    assert manifest["ended_at"] == "2026-09-25T11:01:00+08:00"


def test_complete_session_rejects_end_before_received_audio_timeline(
    tmp_path: Path,
) -> None:
    store = WatchSessionStore(tmp_path / "watch-sessions")
    _start(store)
    _put(store, sequence=0, body=b"segment-zero")

    with pytest.raises(Exception, match="audio timeline"):
        store.complete_session(
            device_id="watch-s5-test",
            session_id="session-001",
            expected_segments=1,
            ended_at="2026-09-25T11:00:10+08:00",
        )

    manifest = store.load_session("watch-s5-test", "session-001")
    assert manifest["state"] == "receiving"


def test_complete_watch_audio_segments_assemble_into_one_recording(
    tmp_path: Path,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip(
            "ffmpeg and ffprobe are required for the audio-assembly integration test"
        )

    store = WatchSessionStore(tmp_path / "watch-sessions")
    _start(store)
    for sequence, frequency in enumerate((440, 660)):
        source = tmp_path / f"source-{sequence}.m4a"
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:sample_rate=16000:duration=1",
                "-ac",
                "1",
                "-c:a",
                "aac",
                "-b:a",
                "32k",
                str(source),
            ],
            check=True,
            capture_output=True,
        )
        body = source.read_bytes()
        store.receive_segment(
            device_id="watch-s5-test",
            session_id="session-001",
            sequence=sequence,
            start_offset_ms=sequence * 1_000,
            duration_ms=1_000,
            checksum=_sha256(body),
            body=body,
        )

    store.complete_session(
        device_id="watch-s5-test",
        session_id="session-001",
        expected_segments=2,
        ended_at="2026-09-25T11:00:02+08:00",
    )
    recording = store.assemble_recording("watch-s5-test", "session-001")
    duration = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(recording),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert recording.is_file()
    assert 1.8 <= float(duration.stdout.strip()) <= 2.3
    manifest = store.load_session("watch-s5-test", "session-001")
    assert manifest["recording_file"] == "recording.m4a"
    assert manifest["recording_bytes"] == recording.stat().st_size


def test_empty_superseded_session_is_abandoned_and_the_manifest_is_kept(
    tmp_path: Path,
) -> None:
    store = WatchSessionStore(tmp_path)
    store.start_session(
        device_id="watch-s5-test",
        session_id="empty-001",
        started_at="2026-09-25T11:00:00+08:00",
    )
    store.start_session(
        device_id="watch-s5-test",
        session_id="later-002",
        started_at="2026-09-25T12:00:00+08:00",
    )

    preview = store.abandon_empty_sessions()
    assert [item["session_id"] for item in preview] == ["empty-001"]
    assert preview[0]["reason"] == "superseded"
    assert preview[0]["status"] == "dry_run"
    # dry-run 不许改盘。
    assert store.load_session("watch-s5-test", "empty-001")["state"] == "receiving"

    applied = store.abandon_empty_sessions(apply=True)
    assert applied[0]["status"] == "abandoned"
    manifest = store.load_session("watch-s5-test", "empty-001")
    assert manifest["state"] == "abandoned"
    assert manifest["abandon_reason"] == "superseded"
    assert manifest["segments"] == []

    summaries = {item["session_id"]: item for item in store.list_session_summaries()}
    assert summaries["empty-001"]["status"] == "abandoned"
    assert summaries["later-002"]["status"] == "receiving"


def test_idle_empty_session_is_abandoned_even_as_the_latest_on_the_device(
    tmp_path: Path,
) -> None:
    store = WatchSessionStore(tmp_path)
    store.start_session(
        device_id="watch-s5-test",
        session_id="idle-001",
        started_at="2026-09-25T11:00:00+08:00",
    )
    manifest_path = next(tmp_path.glob("*/*/manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["updated_at"] = "2026-09-25T11:00:05+08:00"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = store.abandon_empty_sessions(apply=True)

    assert [(item["session_id"], item["reason"]) for item in report] == [
        ("idle-001", "idle")
    ]
    assert store.load_session("watch-s5-test", "idle-001")["state"] == "abandoned"


def test_abandon_skips_sessions_with_audio_and_fresh_empty_ones(tmp_path: Path) -> None:
    store = WatchSessionStore(tmp_path)
    _start(store)
    _put(store, sequence=0, body=b"audio")
    store.start_session(
        device_id="watch-s5-test",
        session_id="fresh-empty",
        started_at="2026-09-25T12:00:00+08:00",
    )

    report = store.abandon_empty_sessions(apply=True)

    assert report == []
    assert store.load_session("watch-s5-test", "session-001")["state"] == "receiving"
    assert store.load_session("watch-s5-test", "fresh-empty")["state"] == "receiving"


def test_an_abandoned_session_still_accepts_audio_the_watch_uploads_later(
    tmp_path: Path,
) -> None:
    """Abandoning must never cost audio: an offline Watch that comes back still gets in."""
    store = WatchSessionStore(tmp_path)
    store.start_session(
        device_id="watch-s5-test",
        session_id="empty-001",
        started_at="2026-09-25T11:00:00+08:00",
    )
    store.start_session(
        device_id="watch-s5-test",
        session_id="later-002",
        started_at="2026-09-25T12:00:00+08:00",
    )
    store.abandon_empty_sessions(apply=True)

    acknowledgement = store.receive_segment(
        device_id="watch-s5-test",
        session_id="empty-001",
        sequence=0,
        start_offset_ms=0,
        duration_ms=60_000,
        checksum=_sha256(b"late-audio"),
        body=b"late-audio",
    )

    assert acknowledgement["ok"] is True
    manifest = store.load_session("watch-s5-test", "empty-001")
    assert manifest["state"] == "receiving"
    assert "abandoned_at" not in manifest
    assert manifest["segments"][0]["sequence"] == 0


def test_receive_segment_persists_optional_location_and_omits_when_throttled(
    tmp_path: Path,
) -> None:
    store = WatchSessionStore(tmp_path)
    _start(store)

    loc = {
        "latitude": 31.2304,
        "longitude": 121.4737,
        "horizontal_accuracy": 65.0,
        "timestamp": "2026-10-12T21:30:00+08:00",
        "source": "significant_change",
    }
    store.receive_segment(
        device_id="watch-s5-test",
        session_id="session-001",
        sequence=0,
        start_offset_ms=0,
        duration_ms=60_000,
        checksum=_sha256(b"seg-0"),
        body=b"seg-0",
        location=loc,
    )
    store.receive_segment(
        device_id="watch-s5-test",
        session_id="session-001",
        sequence=1,
        start_offset_ms=60_000,
        duration_ms=60_000,
        checksum=_sha256(b"seg-1"),
        body=b"seg-1",
        location=None,
    )

    manifest = store.load_session("watch-s5-test", "session-001")
    assert manifest["location"] == loc
    assert manifest["segments"][0]["location"] == loc
    assert "location" not in manifest["segments"][1]



# ─────────────────────────────────────────────────────────────────────────────
# [2026-11-07 V68] 手表上传「当日累计步数 + 值所属自然日」：按 steps_date 归档、同日取大。
# 老字段 step_count 仍按旧的「采集日 + 覆盖」路径，行为不变。
# ─────────────────────────────────────────────────────────────────────────────
def _steps_file(tmp_path: Path) -> Path:
    return tmp_path / "health" / "health_steps.json"


def test_receive_segment_files_steps_today_by_steps_date_and_keeps_the_max(
    tmp_path: Path,
) -> None:
    store = WatchSessionStore(tmp_path / "watch-sessions")
    _start(store)
    _put(store, sequence=0, body=b"a", steps_today=1212, steps_date="2026-11-04")
    assert json.loads(_steps_file(tmp_path).read_text())[0]["steps"] == 1212

    # 同一天更大的值 → 更新
    _put(store, sequence=1, body=b"b", steps_today=7666, steps_date="2026-11-04")
    assert json.loads(_steps_file(tmp_path).read_text())[0]["steps"] == 7666

    # 延迟上传的旧段带着小值 → 忽略，不能把 7666 覆盖回 1212
    _put(store, sequence=2, body=b"c", steps_today=1212, steps_date="2026-11-04")
    assert json.loads(_steps_file(tmp_path).read_text())[0]["steps"] == 7666


def test_receive_segment_steps_today_does_not_bleed_across_days(tmp_path: Path) -> None:
    """11-02 的 8328 不能写进 11-03（诊断里真实发生过）。"""
    store = WatchSessionStore(tmp_path / "watch-sessions")
    _start(store)
    _put(store, sequence=0, body=b"a", steps_today=8328, steps_date="2026-11-02")
    _put(store, sequence=1, body=b"b", steps_today=1088, steps_date="2026-11-03")
    rows = {r["date"]: r["steps"] for r in json.loads(_steps_file(tmp_path).read_text())}
    assert rows == {"2026-11-02": 8328, "2026-11-03": 1088}


def test_receive_segment_steps_today_rejects_bad_date(tmp_path: Path) -> None:
    """日期不合法时**不猜**：只记进 manifest 的健康快照，不落 steps 文件。"""
    store = WatchSessionStore(tmp_path / "watch-sessions")
    _start(store)
    _put(store, sequence=0, body=b"a", steps_today=500, steps_date="2026/11/04")
    assert not _steps_file(tmp_path).exists() or not json.loads(_steps_file(tmp_path).read_text())


def test_receive_segment_old_step_count_still_uses_capture_day(tmp_path: Path) -> None:
    """老字段（V67 及以前）语义不变：按采集日归档、最后写入者赢。"""
    store = WatchSessionStore(tmp_path / "watch-sessions")
    _start(store)
    _put(store, sequence=0, body=b"a", step_count=7666)
    _put(store, sequence=1, body=b"b", step_count=1212)
    rows = json.loads(_steps_file(tmp_path).read_text())
    assert len(rows) == 1 and rows[0]["steps"] == 1212, rows
    assert "source" not in rows[0] or rows[0].get("source") != "watch-daily"
