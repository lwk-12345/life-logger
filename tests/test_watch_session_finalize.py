"""收口「已被新会话取代」的接收中 Watch 会话。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from life_logger.watch_stream import (
    IncompleteWatchSessionError,
    WATCH_RECEIVING_STALE_SECONDS,
    WatchSessionConflictError,
    WatchSessionStore,
)


def _start(store: WatchSessionStore, session: str, started_at: str, count: int) -> None:
    store.start_session(
        device_id="watch-test",
        session_id=session,
        started_at=started_at,
    )
    for sequence in range(count):
        body = f"{session}-{sequence}".encode()
        store.receive_segment(
            device_id="watch-test",
            session_id=session,
            sequence=sequence,
            start_offset_ms=sequence * 60_000,
            duration_ms=60_000,
            checksum="sha256:" + hashlib.sha256(body).hexdigest(),
            body=body,
        )


def test_dry_run_reports_without_touching_state(tmp_path: Path) -> None:
    store = WatchSessionStore(tmp_path)
    _start(store, "old", "2026-09-27T10:00:00+08:00", 3)
    _start(store, "new", "2026-09-28T10:00:00+08:00", 1)

    report = store.finalize_superseded_sessions(apply=False)

    assert [item["status"] for item in report] == ["dry_run"]
    assert report[0]["session_id"] == "old"
    assert report[0]["received_duration_ms"] == 180_000
    assert report[0]["expected_segments"] == 3
    assert store.load_session("watch-test", "old")["state"] == "receiving"


def test_apply_completes_superseded_session_and_releases_audio(tmp_path: Path) -> None:
    store = WatchSessionStore(tmp_path)
    _start(store, "old", "2026-09-27T10:00:00+08:00", 3)
    _start(store, "new", "2026-09-28T10:00:00+08:00", 1)

    report = store.finalize_superseded_sessions(apply=True)

    assert [item["status"] for item in report] == ["completed"]
    manifest = store.load_session("watch-test", "old")
    assert manifest["state"] == "complete"
    assert manifest["expected_segments"] == 3
    assert manifest["total_duration_ms"] == 180_000
    assert manifest["ended_at"] >= "2026-09-27T10:03:00+08:00"
    # 最新的会话才是可能仍在录的那一个，必须保持原样。
    assert store.load_session("watch-test", "new")["state"] == "receiving"


def test_superseded_prefix_waits_for_watch_completion_intent(tmp_path: Path) -> None:
    store = WatchSessionStore(tmp_path)
    _start(store, "old", "2026-09-27T10:00:00+08:00", 1)
    _start(store, "new", "2026-09-28T10:00:00+08:00", 1)

    assert store.finalize_completion_intents() == []
    assert store.load_session("watch-test", "old")["state"] == "receiving"

    with pytest.raises(IncompleteWatchSessionError) as error:
        store.complete_session(
            device_id="watch-test",
            session_id="old",
            expected_segments=2,
            ended_at="2026-09-27T10:02:00+08:00",
        )
    assert error.value.missing_sequences == [1]
    assert store.load_session("watch-test", "old")["completion_intent"][
        "expected_segments"
    ] == 2
    old_summary = next(
        row
        for row in store.list_session_summaries()
        if row["session_id"] == "old"
    )
    assert old_summary["completion_pending"] is True
    assert store.finalize_completion_intents()[0]["status"] == "waiting_segments"
    with pytest.raises(WatchSessionConflictError):
        store.complete_session(
            device_id="watch-test",
            session_id="old",
            expected_segments=1,
            ended_at="2026-09-27T10:01:00+08:00",
        )

    body = b"old-1"
    store.receive_segment(
        device_id="watch-test",
        session_id="old",
        sequence=1,
        start_offset_ms=60_000,
        duration_ms=60_000,
        checksum="sha256:" + hashlib.sha256(body).hexdigest(),
        body=body,
    )
    assert store.finalize_completion_intents()[0]["status"] == "completed"
    assert store.load_session("watch-test", "old")["state"] == "complete"


def test_latest_session_is_never_finalized(tmp_path: Path) -> None:
    store = WatchSessionStore(tmp_path)
    _start(store, "only", "2026-09-27T10:00:00+08:00", 2)
    _age_manifest(store, "only", "2026-09-27T10:01:00+08:00")

    assert store.finalize_superseded_sessions(
        apply=True
    ) == []
    assert store.load_session("watch-test", "only")["state"] == "receiving"


def test_holey_session_is_reported_not_finalized(tmp_path: Path) -> None:
    store = WatchSessionStore(tmp_path)
    store.start_session(
        device_id="watch-test",
        session_id="holey",
        started_at="2026-09-27T10:00:00+08:00",
    )
    for sequence in (0, 2):
        body = f"hole-{sequence}".encode()
        store.receive_segment(
            device_id="watch-test",
            session_id="holey",
            sequence=sequence,
            start_offset_ms=sequence * 60_000,
            duration_ms=60_000,
            checksum="sha256:" + hashlib.sha256(body).hexdigest(),
            body=body,
        )
    _start(store, "new", "2026-09-28T10:00:00+08:00", 1)

    report = store.finalize_superseded_sessions(apply=True)

    assert [item["status"] for item in report] == ["skipped_missing"]
    assert report[0]["missing"] == [1]
    assert store.load_session("watch-test", "holey")["state"] == "receiving"


def test_empty_session_is_reported_not_finalized(tmp_path: Path) -> None:
    store = WatchSessionStore(tmp_path)
    _start(store, "empty", "2026-09-27T10:00:00+08:00", 0)
    _start(store, "new", "2026-09-28T10:00:00+08:00", 1)

    report = store.finalize_superseded_sessions(apply=True)

    assert [item["status"] for item in report] == ["skipped_empty"]
    assert store.load_session("watch-test", "empty")["state"] == "receiving"


def test_summaries_flag_superseded_and_stalled_without_touching_state(
    tmp_path: Path,
) -> None:
    store = WatchSessionStore(tmp_path)
    _start(store, "old", "2026-09-27T10:00:00+08:00", 2)
    _start(store, "new", "2026-09-28T10:00:00+08:00", 1)

    summaries = {item["session_id"]: item for item in store.list_session_summaries()}

    old = summaries["old"]
    assert old["superseded"] is True
    assert old["status"] == "superseded"
    # 老的字段必须原样保留，前端还在读它
    assert old["state"] == "receiving"
    assert old["received_segments"] == 2
    assert old["stale_after_seconds"] == float(WATCH_RECEIVING_STALE_SECONDS)
    # updated_at 是刚写的，所以它"被取代"但不"停滞"——两个信号必须分开
    assert old["stalled"] is False

    newest = summaries["new"]
    assert newest["superseded"] is False
    assert newest["status"] == "receiving"


def _age_manifest(store: WatchSessionStore, session: str, updated_at: str) -> None:
    path = store.root / "watch-test" / session / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["updated_at"] = updated_at
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


def test_long_idle_receiving_session_is_flagged_stalled(tmp_path: Path) -> None:
    store = WatchSessionStore(tmp_path)
    _start(store, "quiet", "2026-09-27T10:00:00+08:00", 1)
    _age_manifest(store, "quiet", "2026-09-27T10:01:00+08:00")

    summary = store.list_session_summaries()[0]

    assert summary["stalled"] is True
    assert summary["status"] == "stalled"
    assert summary["activity_age_seconds"] >= WATCH_RECEIVING_STALE_SECONDS
    assert summary["superseded"] is False


def test_completed_session_is_not_flagged_as_stalled(tmp_path: Path) -> None:
    store = WatchSessionStore(tmp_path)
    _start(store, "done", "2026-09-27T10:00:00+08:00", 1)
    store.complete_session(
        device_id="watch-test",
        session_id="done",
        expected_segments=1,
        ended_at="2026-09-27T10:01:00+08:00",
    )

    summary = store.list_session_summaries()[0]

    assert summary["stalled"] is False
    assert summary["state"] == "complete"
    assert summary["status"] == "complete"
