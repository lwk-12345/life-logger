"""Unit and integration tests for OpenClaw evening bill alignment bridge."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
from typing import Any

import pytest

from life_logger.openclaw_bridge import (
    OpenClawBridgeClient,
    build_evening_alignment_payload,
)


class _MockOpenClawDaemon(BaseHTTPRequestHandler):
    dispatched_payloads: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        data = json.loads(body) if body else {}
        self.__class__.dispatched_payloads.append(data)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        resp = {"status": "success", "card_id": "card_test_98765"}
        self.wfile.write(json.dumps(resp).encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def mock_openclaw_server() -> Any:
    server = HTTPServer(("127.0.0.1", 0), _MockOpenClawDaemon)
    _MockOpenClawDaemon.dispatched_payloads = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}"
    yield url, _MockOpenClawDaemon.dispatched_payloads
    server.shutdown()
    server.server_close()


def test_build_evening_alignment_payload() -> None:
    """Test building structured evening alignment payload."""
    confirmed = [
        {"event_id": "e1", "title": "朴朴超市", "data": {"expense": {"amount": 140.0}}},
        {"event_id": "e2", "title": "炸鸡", "data": {"expense": {"amount": 16.0}}},
        {"event_id": "e3", "title": "烟", "data": {"expense": {"amount": 9.0}}},
    ]
    intents = [
        {"intent_id": "int_1", "text": "要去买水", "status": "pending"},
    ]
    payload = build_evening_alignment_payload(
        date_str="2026-10-02",
        confirmed_events=confirmed,
        candidate_events=[],
        active_intents=intents,
    )
    assert payload["schema_version"] == "evening-bill-alignment-v1"
    assert payload["date"] == "2026-10-02"
    assert payload["summary"]["confirmed_expense_count"] == 3
    assert payload["summary"]["total_confirmed_amount_cny"] == 165.0
    assert payload["summary"]["unresolved_intent_count"] == 1


def test_openclaw_client_dispatch(mock_openclaw_server: tuple[str, list[dict[str, Any]]]) -> None:
    """Test dispatching evening alignment payload to OpenClaw daemon."""
    url, received = mock_openclaw_server
    client = OpenClawBridgeClient(base_url=url, bearer_token="test_tok")
    payload = {"date": "2026-10-02", "confirmed_total": 165.0}
    res = client.dispatch_evening_alignment(payload)
    assert res["status"] == "success"
    assert res["card_id"] == "card_test_98765"
    assert len(received) == 1
    assert received[0]["confirmed_total"] == 165.0


def test_openclaw_client_rejects_external_hosts() -> None:
    """Test OpenClaw client rejects non-localhost URLs (SSRF protection)."""
    with pytest.raises(ValueError, match="Localhost daemon"):
        OpenClawBridgeClient(base_url="http://192.168.1.100:8791")
