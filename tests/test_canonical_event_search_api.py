"""Canonical event search HTTP contract (one timeline rule)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from life_logger.local_web import JobStore, LocalWebServer, create_server


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False))


def _expense(
    identifier: str,
    quote: str,
    amount,
    *,
    currency: str = "CNY",
    when: str = "2026-10-13T00:30:00+08:00",
    confidence: float = 0.95,
) -> dict:
    return {
        "event_id": identifier,
        "categories": ["expense"],
        "title": quote,
        "confidence": confidence,
        "time": {"absolute": when, "precision": "exact"},
        "data": {
            "expense": {
                "item": quote,
                "amount": amount,
                "currency": currency,
            }
        },
        "evidence": {
            "quote": quote,
            "reported_at": when,
            "source_label": "self",
            "event_state": "completed",
            "claim_type": "event",
        },
    }


def _start(tmp_path: Path, monkeypatch, config_payload: dict | None = None):
    for method in ["_watch_live_loop", "_reminder_loop", "_auto_ingest_loop"]:
        monkeypatch.setattr(LocalWebServer, method, lambda self: None)
    store = JobStore(tmp_path / "web", recover=False)
    config = tmp_path / "local-config.json"
    config.write_text(json.dumps(config_payload or {}), encoding="utf-8")
    server = create_server(host="127.0.0.1", port=0, store=store, config_path=config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return store, server, thread, f"http://127.0.0.1:{server.server_port}"


def _stop(server, thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _seed_canonical_fixture(store: JobStore) -> None:
    formal = _expense("formal-longan", "买了一包桂圆二十四元", 24)
    formal_usd = _expense(
        "formal-usd",
        "花了七美元购买订阅",
        7,
        currency="CNY",
        when="2026-10-13T01:00:00+08:00",
    )
    pending = _expense(
        "formal-pending",
        "这一块。",
        None,
        when="2026-10-13T02:00:00+08:00",
    )
    summary_path = (
        store.root / "jobs/job-cross/files/hash/result/2026-10-12/daily_summary.json"
    )
    _write(
        summary_path,
        {
            "timeline": [formal, formal_usd, pending],
            "display_timeline": [formal, formal_usd, pending],
            "warnings": [],
        },
    )
    # Same utterance also exists in the live branch with a different event id.
    live = dict(formal, event_id="live-longan")
    _write(
        store.root / "watch-sessions/dev/session/events.json",
        [live],
    )
    _write(
        store.root / "watch-sessions/dev/session/manifest.json",
        {
            "state": "complete",
            "segments": [],
            "started_at": "2026-10-13T00:00:00+08:00",
        },
    )
    _write(
        store.root / "jobs/job-cross/status.json",
        {
            "job_id": "job-cross",
            "status": "completed",
            "files": [{"file_id": "hash", "capture_source": "apple-watch"}],
        },
    )
    # Speech-time evidence for the utterance branch.
    _write(
        store.root / "jobs/job-cross/files/hash/result/2026-10-12/transcript.json",
        [
            {
                "text": "这是原始发言证据",
                "absolute_started_at": "2026-10-13T00:31:00+08:00",
                "recording_id": "rec-cross",
                "segment_id": "seg-cross",
            }
        ],
    )


def test_canonical_event_search_uses_occurrence_date_and_dedups(
    tmp_path: Path, monkeypatch
):
    store, server, thread, base = _start(tmp_path, monkeypatch)
    _seed_canonical_fixture(store)
    try:

        def get(route):
            with urlopen(base + route, timeout=10) as response:
                return json.load(response)

        day_13 = get(
            "/api/events/search?q=%E6%A1%82%E5%9C%86&date=2026-10-13&scope=provisional&view=display"
        )
        assert day_13["read_contract"]["schema_version"] == "event-read-contract-v1"
        assert day_13["read_contract"]["canonical_event_view"] is True
        events = [item for item in day_13["results"] if item["result_type"] == "event"]
        # Cross-midnight event belongs to occurrence date 10-13, not folder 10-12.
        assert [item["event_id"] for item in events] == ["formal-longan"]
        assert events[0]["date"] == "2026-10-13"
        assert events[0]["date_basis"] == "occurrence"
        assert events[0]["job_id"] == "job-cross"
        assert events[0]["artifact_path"] == (
            "files/hash/result/2026-10-12/daily_summary.json"
        )
        assert events[0]["source_job_id"] == "job-cross"
        assert events[0]["source_artifact"].endswith(
            "result/2026-10-12/daily_summary.json"
        )
        assert events[0]["aliases"][0]["event_id"] == "live-longan"
        assert events[0]["expense_amount"] == 24
        assert events[0]["expense_currency"] == "CNY"
        assert {
            "result_type",
            "job_id",
            "artifact_path",
            "date",
            "timestamp",
            "text",
            "quote",
            "source_file",
            "import_source",
            "categories",
            "source_type",
            "review_status",
            "event_id",
            "expense_amount_cny",
        } <= set(events[0])

        day_12 = get("/api/events/search?q=%E6%A1%82%E5%9C%86&date=2026-10-12")
        assert day_12["event_count"] == 0

        # live/formal duplicate is one canonical result, not two.
        all_duplicate = get(
            "/api/events/search?q=%E6%A1%82%E5%9C%86&from=2026-10-13&to=2026-10-13"
        )
        assert all_duplicate["event_count"] == 1

        watch_import = get(
            "/api/events/search?q=%E6%A1%82%E5%9C%86&date=2026-10-13&import_source=apple-watch"
        )
        assert watch_import["event_count"] == 1
        upload_import = get(
            "/api/events/search?q=%E6%A1%82%E5%9C%86&date=2026-10-13&import_source=file-upload"
        )
        assert upload_import["event_count"] == 0
    finally:
        _stop(server, thread)


def test_canonical_event_search_financial_filters_use_shared_policy(
    tmp_path: Path, monkeypatch
):
    store, server, thread, base = _start(tmp_path, monkeypatch)
    _seed_canonical_fixture(store)
    try:

        def get(route):
            with urlopen(base + route, timeout=10) as response:
                return json.load(response)

        usd = get("/api/events/search?q=%E8%AE%A2%E9%98%85&date=2026-10-13")
        assert usd["event_count"] == 1
        item = usd["results"][0]
        assert item["expense_amount"] == 7
        assert item["expense_currency"] == "USD"
        assert item["expense_amount_cny"] is None

        cny_amount = get(
            "/api/events/search?q=%E8%AE%A2%E9%98%85&date=2026-10-13&amount_min=6&amount_max=8"
        )
        assert cny_amount["event_count"] == 0
        assert cny_amount["results"] == []

        usd_amount = get(
            "/api/events/search?q=%E8%AE%A2%E9%98%85&date=2026-10-13&amount_min=6&amount_max=8&currency=USD"
        )
        assert usd_amount["event_count"] == 1
        assert usd_amount["results"][0]["expense_currency"] == "USD"

        pending = get(
            "/api/events/search?q=%E8%BF%99%E4%B8%80%E5%9D%97&date=2026-10-13"
        )
        assert pending["event_count"] == 1
        pending_item = pending["results"][0]
        assert pending_item["financial_status"] == "pending"
        assert pending_item["expense_amount"] is None
        assert pending_item["expense_amount_cny"] is None
        assert pending_item["expense_amount"] != 0

        pending_amount = get(
            "/api/events/search?q=%E8%BF%99%E4%B8%80%E5%9D%97&date=2026-10-13&amount_min=0&amount_max=100"
        )
        assert pending_amount["event_count"] == 0
    finally:
        _stop(server, thread)


def test_canonical_event_search_type_and_validation(tmp_path: Path, monkeypatch):
    store, server, thread, base = _start(tmp_path, monkeypatch)
    _seed_canonical_fixture(store)
    try:

        def get(route):
            with urlopen(base + route, timeout=10) as response:
                return json.load(response)

        utterances = get(
            "/api/events/search?q=%E5%8E%9F%E5%A7%8B%E5%8F%91%E8%A8%80&type=utterance"
        )
        assert utterances["event_count"] == 0
        assert utterances["utterance_count"] == 1
        utterance = utterances["results"][0]
        assert utterance["result_type"] == "utterance"
        assert utterance["date_basis"] == "speech_time"
        assert utterance["read_contract"]["canonical_event_view"] is False

        events = get("/api/events/search?q=%E6%A1%82%E5%9C%86&type=event")
        assert events["event_count"] == 1
        assert events["utterance_count"] == 0

        # Category/amount filters select the event branch even when type=all.
        category = get(
            "/api/events/search?q=%E6%A1%82%E5%9C%86&type=all&category=expense"
        )
        assert category["event_count"] == 1
        assert category["utterance_count"] == 0

        empty = get("/api/events/search?date=2026-10-20")
        assert empty["result_count"] == 0
        assert empty["read_contract"]["canonical_event_view"] is True

        for route in [
            "/api/events/search?date=2026-10-13&scope=bogus",
            "/api/events/search?date=2026-10-13&view=bogus",
            "/api/events/search?date=2026-10-13&currency=CN",
            "/api/events/search?date=2026-13-40",
        ]:
            with pytest.raises(HTTPError) as error:
                get(route)
            assert error.value.code == 400
    finally:
        _stop(server, thread)


def test_canonical_event_search_applies_dashboard_dictation_filter(
    tmp_path: Path, monkeypatch
):
    store, server, thread, base = _start(
        tmp_path,
        monkeypatch,
        {
            "hide_dictation_from_dashboard": True,
            "dictation_windows_extra": [
                {
                    "start": "2026-10-13T09:00:00+08:00",
                    "end": "2026-10-13T09:10:00+08:00",
                }
            ],
        },
    )
    hidden = _expense(
        "hidden-dictation",
        "设计沟通：红色平行线",
        5,
        when="2026-10-13T09:02:00+08:00",
    )
    hidden["evidence"]["start_offset_seconds"] = 0.0
    hidden["evidence"]["end_offset_seconds"] = 8.0
    kept = _expense(
        "kept-purchase",
        "买了一瓶水三块钱",
        3,
        when="2026-10-13T10:00:00+08:00",
    )
    _write(store.root / "watch-sessions/dev/session/events.json", [hidden, kept])
    _write(
        store.root / "watch-sessions/dev/session/manifest.json",
        {
            "state": "complete",
            "segments": [],
            "started_at": "2026-10-13T08:00:00+08:00",
        },
    )
    try:

        def get(route):
            with urlopen(base + route, timeout=10) as response:
                return json.load(response)

        hidden_result = get(
            "/api/events/search?q=%E8%AE%BE%E8%AE%A1%E6%B2%9F%E9%80%9A&date=2026-10-13"
        )
        assert hidden_result["event_count"] == 0
        kept_result = get("/api/events/search?q=%E6%B0%B4&date=2026-10-13")
        assert kept_result["event_count"] == 1
        assert kept_result["results"][0]["event_id"] == "kept-purchase"
    finally:
        _stop(server, thread)
