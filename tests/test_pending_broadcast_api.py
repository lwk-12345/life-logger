"""P-51: pending broadcast accept/reject/undo API contract."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from life_logger.broadcast_decisions import load_decisions
from life_logger.local_web import JobStore, LocalWebServer, create_server

DATE = "2026-10-20"
TOKEN = "test-token"
PENDING_ID = "evt_pos_pending"


def _event() -> dict:
    return {
        "event_id": PENDING_ID,
        "title": "线下支付：手呢 (¥15.00)",
        "categories": ["expense"],
        "activity_type": "pos_payment_broadcast",
        "time": {"absolute": "2026-10-20T00:01:00+00:00", "clock": "08:01"},
        "evidence": {
            "quote": "支付宝到账十五元",
            "source_label": "non_self",
            "title_source": "pos_broadcast",
            "item_source": "non_self",
            "order_binding": False,
        },
        "data": {"expense": {"amount": 15.0, "currency": "CNY", "item": "手呢"}},
    }


def _request(base: str, path: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    data = None
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
        # Same-origin + CSRF are required by the production write gate.
        headers["Origin"] = base
        headers["X-Life-Logger-CSRF"] = "1"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


@pytest.fixture()
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "local-web"
    config = tmp_path / "local-config.json"
    config.write_text("{}", encoding="utf-8")
    store = JobStore(root, recover=False)

    def fake_brief(self, *, date=None, allow_empty=False, scope="provisional", view="display"):
        target = date or DATE
        event = _event()
        return {
            "date": target,
            "events": [event],
            "event_count": 1,
            "accounting_scope": scope,
            "scope": scope,
            "view": view,
        }

    monkeypatch.setattr(LocalWebServer, "build_daily_brief", fake_brief)
    monkeypatch.setattr(LocalWebServer, "_watch_live_loop", lambda self: None)
    monkeypatch.setattr(LocalWebServer, "_reminder_loop", lambda self: None)
    monkeypatch.setattr(LocalWebServer, "_auto_ingest_loop", lambda self: None)
    monkeypatch.setattr(
        LocalWebServer, "_warm_vip_transcriber", staticmethod(lambda: None)
    )
    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        config_path=config,
        access_token=TOKEN,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, root
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _daily_total(base: str) -> tuple[float, list[str]]:
    status, payload = _request(base, f"/api/daily?date={DATE}&scope=provisional&view=display")
    assert status == 200, payload
    ledger = payload["expense_ledger"]
    return ledger["total_amount"], [
        str(tx.get("event_id") or "") for tx in ledger.get("transactions", [])
    ]


def test_list_pending_broadcast(api) -> None:
    base, _root = api
    status, payload = _request(base, f"/api/expense/pending-broadcast?date={DATE}")
    assert status == 200
    assert payload["pending_broadcast_count"] == 1
    assert payload["pending_broadcast"][0]["event_id"] == PENDING_ID
    assert payload["pending_broadcast"][0]["proposed_amount"] == 15.0
    assert payload["total_amount"] == 0.0


def test_accept_changes_total_and_moves_to_transactions(api) -> None:
    base, root = api
    status, payload = _request(
        base,
        "/api/expense/pending-broadcast/accept",
        "POST",
        {"date": DATE, "event_id": PENDING_ID, "amount": 15.0},
    )
    assert status == 200, payload
    assert payload["changed"] is True
    assert payload["total_amount"] == 15.0
    assert payload["pending_broadcast_count"] == 0
    assert _daily_total(base) == (15.0, [PENDING_ID])
    decisions = load_decisions(root, DATE)
    assert decisions["decisions"][0] == {
        **decisions["decisions"][0],
        "event_id": PENDING_ID,
        "action": "accept",
        "amount": 15.0,
        "prev_action": "pending",
    }


def test_reject_keeps_total_and_hides_pending(api) -> None:
    base, root = api
    status, payload = _request(
        base,
        "/api/expense/pending-broadcast/reject",
        "POST",
        {"date": DATE, "event_id": PENDING_ID},
    )
    assert status == 200, payload
    assert payload["total_amount"] == 0.0
    assert payload["pending_broadcast_count"] == 0
    assert _daily_total(base) == (0.0, [])
    assert load_decisions(root, DATE)["decisions"][0]["action"] == "reject"


def test_undo_returns_to_pending(api) -> None:
    base, root = api
    assert _request(
        base,
        "/api/expense/pending-broadcast/accept",
        "POST",
        {"date": DATE, "event_id": PENDING_ID, "amount": 15.0},
    )[0] == 200
    status, payload = _request(
        base,
        "/api/expense/pending-broadcast/undo",
        "POST",
        {"date": DATE, "event_id": PENDING_ID},
    )
    assert status == 200, payload
    assert payload["changed"] is True
    assert payload["total_amount"] == 0.0
    assert payload["pending_broadcast_count"] == 1
    assert _daily_total(base) == (0.0, [])
    assert load_decisions(root, DATE)["decisions"] == []


def test_repeated_accept_is_idempotent(api) -> None:
    base, root = api
    body = {"date": DATE, "event_id": PENDING_ID, "amount": 15.0}
    first = _request(base, "/api/expense/pending-broadcast/accept", "POST", body)
    second = _request(base, "/api/expense/pending-broadcast/accept", "POST", body)
    assert first[0] == second[0] == 200
    assert first[1]["total_amount"] == second[1]["total_amount"] == 15.0
    assert second[1]["changed"] is False
    assert len(load_decisions(root, DATE)["decisions"]) == 1
    assert _daily_total(base)[0] == 15.0


def test_invalid_event_id_is_400(api) -> None:
    base, _root = api
    status, payload = _request(
        base,
        "/api/expense/pending-broadcast/accept",
        "POST",
        {"date": DATE, "event_id": "evt_missing", "amount": 1.0},
    )
    assert status == 400
    assert "pending broadcast" in payload["error"]


def test_invalid_date_is_400(api) -> None:
    base, _root = api
    status, payload = _request(
        base,
        "/api/expense/pending-broadcast/accept",
        "POST",
        {"date": "2026-13-99", "event_id": PENDING_ID, "amount": 1.0},
    )
    assert status == 400
    assert "YYYY-MM-DD" in payload["error"]


def test_invalid_amount_is_400(api) -> None:
    base, _root = api
    status, payload = _request(
        base,
        "/api/expense/pending-broadcast/accept",
        "POST",
        {"date": DATE, "event_id": PENDING_ID, "amount": 0},
    )
    assert status == 400
    assert "positive" in payload["error"]


def test_concurrent_accept_counts_once(api) -> None:
    base, root = api
    body = {"date": DATE, "event_id": PENDING_ID, "amount": 15.0}

    def call() -> tuple[int, dict]:
        return _request(base, "/api/expense/pending-broadcast/accept", "POST", body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: call(), range(2)))
    assert [item[0] for item in results] == [200, 200]
    assert _daily_total(base) == (15.0, [PENDING_ID])
    assert len(load_decisions(root, DATE)["decisions"]) == 1
