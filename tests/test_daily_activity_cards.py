"""The daily brief exposes activity_cards as a pure sidecar."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from life_logger.activity_cards import STATES, build_activity_cards
from life_logger.daily_brief import build_daily_brief
from life_logger.daily_cognitive_session import build_expense_ledger
from life_logger.event_view import build_event_view


DATE = "2026-10-24"


def _write_summary(root: Path, events: list[dict]) -> None:
    target = root / "jobs" / "job-activity" / "files" / "rec-1" / "result" / DATE / "daily_summary.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({
        "date": DATE,
        "timezone": "Asia/Shanghai",
        "timeline": events,
        "display_timeline": events,
        "warnings": [],
    }, ensure_ascii=False), encoding="utf-8")


def _events() -> list[dict]:
    return [
        {
            "event_id": "evt-card-routine",
            "categories": ["routine"],
            "title": "起床",
            "activity_type": "routine",
            "time": {"absolute": "2026-10-24T01:00:00+08:00", "precision": "exact"},
            "evidence": {"quote": "起床了", "reported_at": "2026-10-24T01:00:00+08:00"},
            "data": {},
        },
        {
            "event_id": "evt-card-expense",
            "categories": ["expense"],
            "title": "购买香烟",
            "activity_type": "purchase",
            "time": {"absolute": "2026-10-24T02:00:00+08:00", "precision": "exact"},
            "evidence": {"quote": "买包烟十块", "reported_at": "2026-10-24T02:00:00+08:00"},
            "data": {"expense": {"amount": 10.0, "currency": "CNY", "item": "香烟"}},
        },
        {
            "event_id": "evt-card-unknown",
            "categories": ["food"],
            "title": "吃了点东西",
            "activity_type": "meal",
            "time": {"absolute": None, "precision": "unknown"},
            "evidence": {"quote": "吃了点东西"},
            "data": {},
        },
    ]


def test_daily_brief_adds_activity_cards_without_changing_events_or_money(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    source_events = _events()
    _write_summary(root, source_events)

    before_view = build_event_view(root, date_from=DATE, date_to=DATE)
    before_events = copy.deepcopy(before_view["events"])
    before_ledger = build_expense_ledger(before_events)

    brief = build_daily_brief(root, date=DATE)
    assert brief["activity_cards"] == brief["activity_cards"]  # list is present and JSON-safe
    assert isinstance(brief["activity_cards"], list)
    assert brief["events"] == before_events
    assert brief["event_count"] == len(before_events)
    assert [item["event_id"] for item in brief["events"]] == [item["event_id"] for item in before_events]
    assert build_expense_ledger(brief["events"]) == before_ledger

    cards = brief["activity_cards"]
    assert [card["source_event_id"] for card in cards] == [item["event_id"] for item in before_events]
    for card in cards:
        assert card["state"] in STATES
        assert card["source_event_id"] in {item["event_id"] for item in before_events}
    unknown_card = next(card for card in cards if card["source_event_id"] == "evt-card-unknown")
    assert unknown_card["duration"] is None
    assert "duration" in unknown_card["unknown_fields"]
    assert "time" in unknown_card["unknown_fields"] or unknown_card["time"]["basis"] == "unknown"
    expense_card = next(card for card in cards if card["source_event_id"] == "evt-card-expense")
    assert expense_card["transactions"][0]["source"] == "expense_ledger"
    assert expense_card["transactions"][0]["amount_policy"] == "reference_only_no_sum"


def test_missing_event_id_does_not_get_a_synthetic_card() -> None:
    result = build_activity_cards([{
        "title": "无 event_id 的事件",
        "categories": ["routine"],
        "time": {"absolute": "2026-10-24T01:00:00+08:00"},
        "evidence": {},
        "data": {},
    }])
    assert result["cards"] == []
    assert result["alias"] == {}
    assert result["stats"]["input_events"] == 0


def test_isolated_daily_api_returns_activity_cards_without_changing_truth(tmp_path: Path) -> None:
    import threading
    import urllib.request

    from life_logger.local_web import JobStore, create_server

    root = tmp_path / "local-web"
    _write_summary(root, _events())
    before_events = copy.deepcopy(build_event_view(root, date_from=DATE, date_to=DATE)["events"])
    before_ledger = build_expense_ledger(before_events)

    store = JobStore(root, recover=False)
    token = "activity-api-token"
    server = create_server(host="127.0.0.1", port=0, store=store, access_token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/daily?date={DATE}",
            headers={"Authorization": f"Bearer {token}"},
        )
        payload = json.loads(urllib.request.urlopen(request, timeout=15).read())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert payload["events"] == before_events
    assert len(payload["events"]) == len(before_events)
    assert payload["expense_ledger"]["total_amount"] == before_ledger["total_amount"]
    daily_summary = payload["daily_summary"]
    assert isinstance(daily_summary["activity_cards"], list)
    assert [card["source_event_id"] for card in daily_summary["activity_cards"]] == [
        event["event_id"] for event in before_events
    ]
