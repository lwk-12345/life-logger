"""HTTP-level accounting contract: same scope/view yields same events and money."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from life_logger.local_web import JobStore, LocalWebServer, create_server


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False))


def _expense(
    identifier, quote, amount, currency="CNY", when="2026-10-13T00:30:00+08:00"
):
    return {
        "event_id": identifier,
        "categories": ["expense"],
        "title": quote,
        "confidence": 0.95,
        "time": {"absolute": when, "precision": "exact"},
        "data": {"expense": {"item": quote, "amount": amount, "currency": currency}},
        "evidence": {
            "quote": quote,
            "reported_at": when,
            "source_label": "self",
            "event_state": "completed",
            "claim_type": "event",
        },
    }


def test_daily_stats_brief_share_provisional_and_settled_views(
    tmp_path: Path, monkeypatch
):
    # Prevent capture/ASR/background loops: this is a web contract, not model inference.
    for method in ["_watch_live_loop", "_reminder_loop", "_auto_ingest_loop"]:
        monkeypatch.setattr(LocalWebServer, method, lambda self: None)
    config = tmp_path / "local-config.json"
    config.write_text('{"hide_dictation_from_dashboard": false}')
    store = JobStore(tmp_path / "web", recover=False)
    formal = _expense("formal-longan", "买了一包桂圆二十四元", 24)
    summary = (
        store.root / "jobs/job-cross/files/hash/result/2026-10-12/daily_summary.json"
    )
    _write(
        summary, {"timeline": [formal], "display_timeline": [formal], "warnings": []}
    )
    live = dict(formal, event_id="live-longan")
    dollars = _expense(
        "live-usd", "花了七美元购买订阅", 7, "USD", "2026-10-13T01:00:00+08:00"
    )
    _write(store.root / "watch-sessions/dev/session/events.json", [live, dollars])
    _write(
        store.root / "watch-sessions/dev/session/manifest.json",
        {
            "state": "complete",
            "completion_job_id": "job-cross",
            "segments": [],
            "started_at": "2026-10-12T16:00:00Z",
        },
    )
    server = create_server(host="127.0.0.1", port=0, store=store, config_path=config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def get(route):
        with urlopen(base + route, timeout=10) as response:
            return json.load(response)

    try:
        for scope, expected in [("provisional", 2), ("settled", 1)]:
            stats = get("/api/stats?from=2026-10-13&to=2026-10-13&scope=" + scope)
            daily = get("/api/daily?date=2026-10-13&scope=" + scope)
            brief = get("/api/daily-brief?date=2026-10-13&scope=" + scope)
            assert stats["event_count"] == len(daily["events"]) == expected
            brief_payload = brief.get("brief", brief)
            assert brief_payload["event_count"] == expected
            assert (
                stats["expense_total_cny"]
                == daily["expense_ledger"]["total_amount"]
                == 24
            )
            assert daily["expense_ledger"]["totals_by_currency"].get("USD", 0) == (
                7 if scope == "provisional" else 0
            )
            assert stats["accounting_scope"] == scope
        with pytest.raises(HTTPError) as error:
            get("/api/stats?from=2026-10-13&to=2026-10-13&scope=bogus")
        assert error.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _start_test_server(tmp_path: Path, monkeypatch, config_payload: dict | None = None):
    for method in ["_watch_live_loop", "_reminder_loop", "_auto_ingest_loop"]:
        monkeypatch.setattr(LocalWebServer, method, lambda self: None)
    config = tmp_path / "local-config.json"
    config.write_text(json.dumps(config_payload or {}))
    store = JobStore(tmp_path / "web", recover=False)
    server = create_server(host="127.0.0.1", port=0, store=store, config_path=config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return store, server, thread, f"http://127.0.0.1:{server.server_port}"


def _stop_test_server(server, thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_empty_day_and_invalid_read_contracts_return_explicit_errors(
    tmp_path: Path, monkeypatch
) -> None:
    _, server, thread, base = _start_test_server(tmp_path, monkeypatch)
    try:

        def get(route):
            with urlopen(base + route, timeout=10) as response:
                return json.load(response)

        stats = get("/api/stats?from=2026-10-20&to=2026-10-20")
        assert stats["event_count"] == 0
        assert stats["read_contract"]["schema_version"] == "event-read-contract-v1"
        assert stats["read_contract"]["scope"] == "provisional"
        assert stats["read_contract"]["view"] == "display"

        daily = get("/api/daily?date=2026-10-20")
        assert daily["events"] == []
        assert daily["read_contract"]["canonical_event_view"] is True

        brief = get("/api/daily-brief?date=2026-10-20")
        assert brief["schema_version"] == "agent-brief-v1"
        assert brief["event_count"] == 0
        assert brief["confidence_filter"]["source_event_count"] == 0
        assert brief["read_contract"]["scope"] == "provisional"

        for route in [
            "/api/stats?from=2026-10-20&to=2026-10-20&scope=bogus",
            "/api/daily?date=2026-10-20&view=bogus",
            "/api/daily-brief?date=2026-10-20&scope=bogus",
        ]:
            with pytest.raises(HTTPError) as error:
                get(route)
            assert error.value.code == 400
    finally:
        _stop_test_server(server, thread)


def test_dictation_filter_is_identical_for_stats_and_daily(
    tmp_path: Path, monkeypatch
) -> None:
    store, server, thread, base = _start_test_server(
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

        stats = get(
            "/api/stats?from=2026-10-13&to=2026-10-13&scope=provisional&view=display"
        )
        daily = get("/api/daily?date=2026-10-13&scope=provisional&view=display")
        assert stats["event_count"] == len(daily["events"]) == 1
        assert [event["event_id"] for event in daily["events"]] == ["kept-purchase"]
        assert (
            stats["expense_total_cny"] == daily["expense_ledger"]["total_amount"] == 3
        )
        assert stats["read_contract"]["scope"] == daily["read_contract"]["scope"]
        assert stats["read_contract"]["view"] == daily["read_contract"]["view"]
    finally:
        _stop_test_server(server, thread)


def test_external_agent_brief_labels_confidence_filtering(
    tmp_path: Path, monkeypatch
) -> None:
    store, server, thread, base = _start_test_server(
        tmp_path, monkeypatch, {"hide_dictation_from_dashboard": False}
    )
    high = _expense("high-confidence", "买了一包桂圆二十四元", 24)
    low = _expense("low-confidence", "买了一个不确定的东西七元", 7)
    low["confidence"] = 0.55
    _write(store.root / "watch-sessions/dev/session/events.json", [high, low])
    _write(
        store.root / "watch-sessions/dev/session/manifest.json",
        {
            "state": "complete",
            "segments": [],
            "started_at": "2026-10-13T00:00:00+08:00",
        },
    )
    try:

        def get(route):
            with urlopen(base + route, timeout=10) as response:
                return json.load(response)

        stats = get(
            "/api/stats?from=2026-10-13&to=2026-10-13&scope=provisional&view=display"
        )
        daily = get("/api/daily?date=2026-10-13&scope=provisional&view=display")
        brief = get("/api/daily-brief?date=2026-10-13&scope=provisional&view=display")

        # Canonical read keeps both facts; only the external projection is filtered.
        assert stats["event_count"] == len(daily["events"]) == 2
        assert (
            stats["expense_total_cny"] == daily["expense_ledger"]["total_amount"] == 31
        )
        # 【2026-10-19】三接口口径统一后：event_count 报**权威数**（与 stats/daily 一致），
        # 置信度投影单独叫 projection_event_count。此前 brief 把 event_count 报成投影数，
        # 导致同一天三个页面三个数（实测 10-14：44 vs 30）。
        assert brief["event_count"] == 2
        assert brief["projection_event_count"] == 1
        assert brief["expense_total_cny"] == 31
        assert brief["expense_total"]["total_amount"] == 31
        assert brief["high_confidence_points"][0]["event_id"] == "high-confidence"
        assert brief["event_count_basis"] == "canonical_event_view"
        assert brief["confidence_filter"] == {
            "policy": "external_agent_brief",
            "min_confidence": 0.90,
            "source_event_count": 2,
            "included_event_count": 1,
            "filtered_event_count": 1,
        }
        assert brief["read_contract"]["scope"] == "provisional"
        assert brief["read_contract"]["view"] == "display"
        assert "low-confidence" not in json.dumps(brief, ensure_ascii=False)
    finally:
        _stop_test_server(server, thread)


def test_diagnostic_routes_are_labeled_not_canonical(
    tmp_path: Path, monkeypatch
) -> None:
    store, server, thread, base = _start_test_server(tmp_path, monkeypatch)
    _write(store.root / "watch-sessions/dev/session/events.json", [])
    try:

        def get(route):
            with urlopen(base + route, timeout=10) as response:
                return json.load(response)

        search = get("/api/search?q=%E6%B5%8B%E8%AF%95")
        assert search["read_contract"]["canonical_event_view"] is False
        assert search["read_contract"]["scope"] == "diagnostic_raw"

        live = get("/api/watch-live-events")
        assert live["read_contract"]["canonical_event_view"] is False
        assert live["read_contract"]["scope"] == "diagnostic_raw"
    finally:
        _stop_test_server(server, thread)
