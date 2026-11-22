from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import pytest

import life_logger.watch_live as watch_live
from life_logger.watch_live import WatchLiveProcessor, preview_summary
from life_logger.watch_stream import WatchSessionStore


def _session(tmp_path: Path, count: int = 1):
    store = WatchSessionStore(tmp_path)
    store.start_session(
        device_id="watch-test",
        session_id="session-test",
        started_at="2026-09-27T10:00:00+08:00",
    )
    for sequence in range(count):
        body = f"audio-{sequence}".encode()
        store.receive_segment(
            device_id="watch-test",
            session_id="session-test",
            sequence=sequence,
            start_offset_ms=sequence * 60_000,
            duration_ms=60_000,
            checksum="sha256:" + hashlib.sha256(body).hexdigest(),
            body=body,
        )
    return store


def _processor(
    store: WatchSessionStore,
    processor,
    *,
    slot_acquirer=lambda _: {"token": "test"},
    slot_releaser=lambda _: None,
):
    return WatchLiveProcessor(
        store,
        Path("unused"),
        threading.Event(),
        processor,
        slot_acquirer=slot_acquirer,
        slot_releaser=slot_releaser,
    )


def test_preview_remains_pending_when_shared_asr_slot_is_unavailable(
    tmp_path: Path,
) -> None:
    store = _session(tmp_path)
    called = False

    def process(*_):
        nonlocal called
        called = True
        return []

    worker = _processor(store, process, slot_acquirer=lambda _: None)

    assert worker.scan_once() is False
    assert called is False
    directory = store.session_directory("watch-test", "session-test")
    state_path = directory / "live/000000/state.json"
    assert state_path.exists() is False
    summary = preview_summary(
        directory, store.load_session("watch-test", "session-test")
    )
    assert summary["pending_segments"] == 1
    assert summary["processing_segments"] == 0


def test_preview_releases_shared_asr_slot_after_processor_failure(
    tmp_path: Path,
) -> None:
    store = _session(tmp_path)
    token = {"token": "owned"}
    released = []
    worker = _processor(
        store,
        lambda *_: (_ for _ in ()).throw(RuntimeError("preview failed")),
        slot_acquirer=lambda _: token,
        slot_releaser=released.append,
    )

    assert worker.scan_once() is True
    assert released == [token]


def test_successful_empty_asr_is_terminal_no_speech_and_skips_attribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _session(tmp_path)
    directory = store.session_directory("watch-test", "session-test")
    output = directory / "live/000000"
    (output / "asr").mkdir(parents=True)
    (output / "asr/run-summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "transcript_count": 0,
            }
        )
    )
    worker = WatchLiveProcessor(
        store,
        Path("unused"),
        threading.Event(),
        slot_acquirer=lambda _: {"token": "test"},
        slot_releaser=lambda _: None,
    )
    monkeypatch.setattr(
        watch_live,
        "load_local_config",
        lambda _: {
            "python": "python",
            "asr": "asr",
            "vad": "vad",
            "template": "template",
            "speaker": "cam++",
        },
    )
    monkeypatch.setattr(watch_live, "validate_local_pipeline_config", lambda _: None)
    monkeypatch.setattr(watch_live, "build_asr_command", lambda **_: ["asr"])
    monkeypatch.setattr(
        watch_live,
        "build_attribution_command",
        lambda **_: pytest.fail("attribution must be skipped"),
    )
    monkeypatch.setattr(worker, "_run", lambda command, log, config, stage: None)

    assert worker.scan_once()
    summary = preview_summary(
        directory, store.load_session("watch-test", "session-test")
    )
    state = json.loads((directory / "live/000000/state.json").read_text())
    assert state["status"] == "no_speech"
    assert state["rows"] == []
    assert summary["no_speech_segments"] == 1
    assert summary["pending_segments"] == 0
    assert summary["rows"] == []


@pytest.mark.parametrize(
    "failure",
    [
        "ASR阶段：ASR阶段失败（1）",
        "ASR产物校验阶段：run-summary.json 状态或 transcript_count 无效",
        "ASR产物校验阶段：missing transcript.json",
        "声纹归属阶段：归属失败",
    ],
)
def test_asr_process_summary_and_attribution_failures_remain_retryable(
    tmp_path: Path,
    failure: str,
) -> None:
    store = _session(tmp_path)
    worker = _processor(store, lambda *_: (_ for _ in ()).throw(RuntimeError(failure)))

    assert worker.scan_once()
    directory = store.session_directory("watch-test", "session-test")
    state = json.loads((directory / "live/000000/state.json").read_text())
    summary = preview_summary(
        directory, store.load_session("watch-test", "session-test")
    )
    assert state["status"] == "failed"
    assert state["error"] == failure
    assert state["retry_at"] > time.time()
    assert summary["failed_segments"] == 1
    assert summary["pending_segments"] == 0


def test_preview_summary_reports_bounded_latest_segment_states_and_allowlisted_rows(
    tmp_path: Path,
) -> None:
    store = _session(tmp_path, count=10)
    directory = store.session_directory("watch-test", "session-test")
    manifest = store.load_session("watch-test", "session-test")
    statuses = ["completed", "no_speech", "failed", "processing", "pending"] * 2
    for segment, status in zip(manifest["segments"], statuses, strict=True):
        state = {
            "sha256": segment["sha256"],
            "status": status,
            "updated_at": "2026-09-27T10:02:00+08:00",
            "error": "<b>bad</b>" if status == "failed" else None,
            "rows": [
                {
                    "text": "hello",
                    "absolute_started_at": "2026-09-27T10:00:00+08:00",
                    "start_offset_seconds": 1.25,
                    "end_offset_seconds": 2.5,
                    "source_label": "speaker-a",
                    "voice_similarity": 0.87,
                    "private_path": "/must/not/leak",
                }
            ],
        }
        if status == "pending":
            state["sha256"] = "sha256:mismatch"
        path = directory / "live" / f"{segment['sequence']:06d}" / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))

    preview = preview_summary(directory, manifest)

    assert [segment["sequence"] for segment in preview["segments"]] == list(
        range(2, 10)
    )
    assert {segment["status"] for segment in preview["segments"]} == set(statuses)
    assert preview["completed_segments"] == 2
    assert preview["no_speech_segments"] == 2
    assert preview["failed_segments"] == 2
    assert preview["processing_segments"] == 2
    assert preview["pending_segments"] == 2
    assert preview["segments"][-1]["start_at"] == "2026-09-27T10:09:00+08:00"
    assert preview["segments"][-1]["duration_ms"] == 60_000
    completed = next(
        item for item in preview["segments"] if item["status"] == "completed"
    )
    assert set(completed["rows"][0]) == {
        "text",
        "absolute_started_at",
        "start_offset_seconds",
        "end_offset_seconds",
        "source_label",
        "voice_similarity",
    }
    failed = next(item for item in preview["segments"] if item["status"] == "failed")
    assert failed["error"] == "<b>bad</b>"


def test_preview_missing_or_checksum_mismatched_state_is_pending(
    tmp_path: Path,
) -> None:
    store = _session(tmp_path, count=2)
    directory = store.session_directory("watch-test", "session-test")
    manifest = store.load_session("watch-test", "session-test")
    first = directory / "live/000000/state.json"
    first.parent.mkdir(parents=True)
    first.write_text(json.dumps({"sha256": "sha256:wrong", "status": "completed"}))

    preview = preview_summary(directory, manifest)

    assert [segment["status"] for segment in preview["segments"]] == [
        "pending",
        "pending",
    ]
    assert preview["pending_segments"] == 2
    assert preview["rows"] == []


def test_malformed_asr_summary_is_retryable_with_summary_stage_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _session(tmp_path)
    worker = WatchLiveProcessor(
        store,
        Path("unused"),
        threading.Event(),
        slot_acquirer=lambda _: {"token": "test"},
        slot_releaser=lambda _: None,
    )
    monkeypatch.setattr(
        watch_live,
        "load_local_config",
        lambda _: {
            "python": "python",
            "asr": "asr",
            "vad": "vad",
            "template": "template",
            "speaker": "cam++",
        },
    )
    monkeypatch.setattr(watch_live, "validate_local_pipeline_config", lambda _: None)
    monkeypatch.setattr(watch_live, "build_asr_command", lambda **_: ["asr"])
    monkeypatch.setattr(worker, "_run", lambda command, log, config, stage: None)
    directory = store.session_directory("watch-test", "session-test")
    asr = directory / "live/000000/asr"
    asr.mkdir(parents=True)
    (asr / "run-summary.json").write_text("not-json")

    assert worker.scan_once()

    state = json.loads((directory / "live/000000/state.json").read_text())
    assert state["status"] == "failed"
    assert "ASR产物校验阶段" in state["error"]
    assert state["retry_at"] > time.time()


def test_older_retry_backoff_does_not_block_newer_eligible_segment(
    tmp_path: Path,
) -> None:
    store = _session(tmp_path, count=2)
    called = []
    worker = _processor(store, lambda audio, *_: called.append(audio.name) or [])
    directory = store.session_directory("watch-test", "session-test")
    old_output = directory / "live/000000"
    old_output.mkdir(parents=True)
    (old_output / "state.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "attempt": 1,
                "sha256": store.load_session("watch-test", "session-test")["segments"][
                    0
                ]["sha256"],
                "retry_at": time.time() + 300,
            }
        )
    )

    assert worker.scan_once()
    assert called == ["segment-000001.m4a"]
    assert (
        json.loads((directory / "live/000001/state.json").read_text())["status"]
        == "completed"
    )


def test_terminal_no_speech_segment_is_not_retried_or_reselected(
    tmp_path: Path,
) -> None:
    store = _session(tmp_path, count=2)
    processed: list[str] = []

    def process(audio: Path, *_args):
        processed.append(audio.name)
        if audio.name == "segment-000000.m4a":
            return watch_live._NoSpeechPreview()
        return []

    worker = _processor(store, process)
    directory = store.session_directory("watch-test", "session-test")

    assert worker.scan_once()
    first_state = json.loads((directory / "live/000000/state.json").read_text())
    assert first_state["status"] == "no_speech"

    assert worker.scan_once()
    second_state = json.loads((directory / "live/000001/state.json").read_text())
    assert second_state["status"] == "completed"
    assert processed == ["segment-000000.m4a", "segment-000001.m4a"]
