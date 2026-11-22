"""Additive capture provenance projection in dashboard captured_todos."""

from __future__ import annotations

import json
from pathlib import Path

from life_logger.daily_dashboard import DailyDashboardBuilder


def _write_captures(root: Path, captures: list[dict]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "quick_captures.json").write_text(
        json.dumps(captures, ensure_ascii=False), encoding="utf-8"
    )


def _write_summary(root: Path, events: list[dict]) -> None:
    target = root / "jobs" / "job-provenance" / "files" / "rec-1" / "result" / "2026-10-24" / "daily_summary.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                "date": "2026-10-24",
                "timezone": "Asia/Shanghai",
                "timeline": events,
                "display_timeline": events,
                "warnings": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _pinned_event(event_id: str, quote: str, **evidence_extra) -> dict:
    return {
        "event_id": event_id,
        "categories": ["todo"],
        "title": f"便签：{quote}",
        "time": {"absolute": "2026-10-24T06:34:17.153000+00:00", "precision": "exact"},
        "evidence": {
            "quote": quote,
            "reported_at": "2026-10-24T06:34:17.153000+00:00",
            "recording_id": f"rec-{event_id}",
            "segment_id": f"seg-{event_id}",
            "source_id": f"src-{event_id}",
            "source_label": "self",
            **evidence_extra,
        },
        "pinned_memo": True,
        "confidence": 0.95,
    }


def _capture(capture_id: str = "cap-1") -> dict:
    return {
        "id": capture_id,
        "trigger_type": "todo",
        "raw_text": "记录一下今天的裸重八十五点八五",
        "extracted_content": "今天的裸重八十五点八五",
        "metadata": {"status": "pending"},
        "captured_at": "2026-10-24T06:34:17.153000+00:00",
    }


def _items(root: Path, origin: str) -> list[dict]:
    day = DailyDashboardBuilder(root).build_for_date("2026-10-24")
    return [item for item in day.captured_todos if item.get("_origin") == origin]


def test_event_origin_preserves_public_source_references_without_paths_or_tokens(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    _write_captures(root, [])
    _write_summary(
        root,
        [
            _pinned_event(
                "evt-source-1",
                "检查一下现在DeepSick上面的一个自动化流程。",
                source_file="$HOME/private/recording.m4a",
                api_token="token=super-secret",
                extractor="deepseek_context_batch",
            )
        ],
    )

    items = _items(root, "event")
    assert len(items) == 1
    item = items[0]
    assert item["source_event_id"] == "evt-source-1"
    assert item["source_evidence"]["recording_id"] == "rec-evt-source-1"
    assert item["source_evidence"]["segment_id"] == "seg-evt-source-1"
    assert item["source_evidence"]["source_id"] == "src-evt-source-1"
    assert item["source_evidence"]["source_label"] == "self"
    assert item["capture_provenance"]["origin"] == "event"
    assert item["capture_provenance"]["status"] == "resolved"
    assert item["capture_provenance"]["same_utterance"] is None
    assert item["capture_provenance"]["quote_sha256"]
    payload = json.dumps(item, ensure_ascii=False)
    assert "$HOME" not in payload
    assert "super-secret" not in payload
    assert "source_file" not in payload
    assert "api_token" not in payload


def test_capture_without_source_is_explicit_unknown_and_keeps_legacy_fields(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    source = _capture()
    _write_captures(root, [source])
    _write_summary(root, [])

    items = _items(root, "capture")
    assert len(items) == 1
    item = items[0]
    for key in ("id", "trigger_type", "raw_text", "extracted_content", "metadata", "captured_at"):
        assert item[key] == source[key]
    assert item["capture_provenance"]["status"] == "unknown"
    assert item["capture_provenance"]["reason"] == "source_not_recorded"
    assert item["capture_provenance"]["recording_id"] is None
    assert item["capture_provenance"]["same_utterance"] is None


def test_capture_existing_source_is_preserved_and_sanitized(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    source = _capture("cap-source")
    source["source_event_id"] = "evt-existing"
    source["source_evidence"] = {
        "recording_id": "rec-existing",
        "segment_id": "seg-existing",
        "source_id": "src-existing",
        "source_label": "self",
        "quote": "记录一下今天的裸重八十五点八五",
        "source_file": "/tmp/private.m4a",
    }
    source["capture_provenance"] = {"same_utterance": True}
    _write_captures(root, [source])
    _write_summary(root, [])

    item = _items(root, "capture")[0]
    assert item["source_event_id"] == "evt-existing"
    assert item["source_evidence"]["recording_id"] == "rec-existing"
    assert item["capture_provenance"]["status"] == "resolved"
    assert item["capture_provenance"]["recording_id"] == "rec-existing"
    assert item["capture_provenance"]["same_utterance"] is None
    assert "/tmp/private.m4a" not in json.dumps(item, ensure_ascii=False)


def test_multiple_events_keep_distinct_source_identities(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    _write_captures(root, [])
    _write_summary(
        root,
        [
            _pinned_event("evt-a", "检查A项目进度"),
            _pinned_event("evt-b", "检查B项目进度"),
        ],
    )

    items = _items(root, "event")
    by_id = {item["source_event_id"]: item for item in items}
    assert set(by_id) == {"evt-a", "evt-b"}
    assert by_id["evt-a"]["source_evidence"]["recording_id"] == "rec-evt-a"
    assert by_id["evt-b"]["source_evidence"]["recording_id"] == "rec-evt-b"
    assert by_id["evt-a"]["capture_provenance"]["segment_id"] != by_id["evt-b"]["capture_provenance"]["segment_id"]


def test_capture_session_only_is_partial_not_same_utterance(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    source = _capture("cap-session")
    source["metadata"] = {"status": "pending", "session_id": "SESSION-ONLY"}
    _write_captures(root, [source])
    _write_summary(root, [])

    item = _items(root, "capture")[0]
    assert item["capture_provenance"]["status"] == "partial"
    assert item["capture_provenance"]["reason"] == "session_only_not_utterance"
    assert item["capture_provenance"]["session_id"] == "SESSION-ONLY"
    assert item["capture_provenance"]["same_utterance"] is None


def test_capture_conflicting_identity_is_not_reported_as_same_utterance(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    source = _capture("cap-conflict")
    source["source_event_id"] = "evt-new"
    source["source_evidence"] = {
        "recording_id": "rec-new",
        "segment_id": "seg-new",
    }
    source["capture_provenance"] = {
        "source_event_id": "evt-old",
        "recording_id": "rec-old",
        "segment_id": "seg-old",
        "same_utterance": True,
    }
    _write_captures(root, [source])
    _write_summary(root, [])

    item = _items(root, "capture")[0]
    assert item["capture_provenance"]["status"] == "conflict"
    assert item["capture_provenance"]["reason"] == "source_identity_conflict"
    assert item["capture_provenance"]["same_utterance"] is None
    assert set(item["capture_provenance"]["conflict_fields"]) == {
        "recording_id", "segment_id", "source_event_id"
    }


def test_new_projection_does_not_copy_quote_and_keeps_hash_only(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    _write_captures(root, [])
    _write_summary(
        root,
        [
            _pinned_event(
                "evt-secret-quote",
                "普通业务原话 sk-live-1234567890 $HOME/secret.m4a",
                extractor="deepseek_context_batch",
            )
        ],
    )

    item = _items(root, "event")[0]
    assert "quote" not in item["source_evidence"]
    assert "quote" not in item["capture_provenance"]["source_evidence"]
    assert item["capture_provenance"]["quote_sha256"]
    # The new provenance projection must not duplicate the quote/token/path.
    prov = json.dumps(item["capture_provenance"], ensure_ascii=False)
    assert "sk-live-1234567890" not in prov
    assert "$HOME" not in prov


def test_token_and_path_boundaries_in_source_fields_are_redacted_without_harming_business_text(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    _write_captures(root, [])
    _write_summary(
        root,
        [
            _pinned_event(
                "evt-filter",
                "原话",
                recording_id="sk-live-1234567890",
                segment_id="$HOME/secret/seg",
                source_id="ghp_abcdefghijklmnop",
                extractor="deepseek_context_batch",
            )
        ],
    )

    item = _items(root, "event")[0]
    ev = item["source_evidence"]
    assert ev["recording_id"] == "[redacted]"
    assert ev["segment_id"] == "[redacted]"
    assert ev["source_id"] == "[redacted]"
    assert ev["extractor"] == "deepseek_context_batch"
    assert "sk-live-1234567890" not in json.dumps(item, ensure_ascii=False)
    assert "ghp_abcdefghijklmnop" not in json.dumps(item, ensure_ascii=False)
    assert "$HOME" not in json.dumps(item, ensure_ascii=False)


def test_windows_drive_path_and_unc_are_redacted_without_harming_business_ids(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    _write_captures(root, [])
    _write_summary(
        root,
        [
            _pinned_event(
                "evt-windows",
                "原话",
                recording_id=r"C:\Users\lwk\secret.wav",
                segment_id=r"\\server\share\secret.wav",
                source_id="watch_A",
                extractor="deepseek_context_batch",
            )
        ],
    )

    item = _items(root, "event")[0]
    ev = item["source_evidence"]
    assert ev["recording_id"] == "[redacted]", ev
    assert ev["segment_id"] == "[redacted]", ev
    assert ev["source_id"] == "watch_A"
    assert ev["extractor"] == "deepseek_context_batch"
    assert "C:\\Users" not in json.dumps(item, ensure_ascii=False)
    assert "\\\\server" not in json.dumps(item, ensure_ascii=False)


def test_common_business_id_shapes_are_not_redacted(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    _write_captures(root, [])
    _write_summary(
        root,
        [
            _pinned_event(
                "evt-business",
                "原话",
                recording_id="watch_A",
                segment_id="segment-000123.m4a",
                source_id="order-12345",
                extractor="sk-1",
            )
        ],
    )

    item = _items(root, "event")[0]
    ev = item["source_evidence"]
    assert ev["recording_id"] == "watch_A"
    assert ev["segment_id"] == "segment-000123.m4a"
    assert ev["source_id"] == "order-12345"
    assert ev["extractor"] == "sk-1"
