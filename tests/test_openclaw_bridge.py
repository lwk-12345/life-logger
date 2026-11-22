"""Tests for OpenClaw Task Bridge integration."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from life_logger.domain import EventTime, LifeEvent
from life_logger.openclaw_bridge import OpenClawBridgeClient, resolve_openclaw_token


class _MockOpenClawBridgeHandler(BaseHTTPRequestHandler):
    tasks: list[dict[str, Any]] = []

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/v1/tasks"):
            tasks = self.__class__.tasks
            if "status=" in self.path:
                status = self.path.split("status=")[1].split("&")[0]
                tasks = [t for t in tasks if t.get("status") == status]
            payload = json.dumps({"schema_version": "unified-task-list-v1", "tasks": tasks}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/v1/tasks":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length))
            idem = self.headers.get("Idempotency-Key")

            # Check existing idempotency
            if idem:
                for existing in self.__class__.tasks:
                    if existing.get("idempotency_key") == idem:
                        payload = json.dumps({"task": existing, "created": False}).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return

            task = {
                "schema_version": "unified-task-v1",
                "id": f"oc_task_{len(self.__class__.tasks) + 1:04d}",
                "title": data.get("title", ""),
                "status": "pending",
                "priority": data.get("priority", "P2"),
                "project_id": data.get("project_id"),
                "tags": data.get("tags", []),
                "source": data.get("source", {}),
                "revision": 1,
                "idempotency_key": idem,
            }
            self.__class__.tasks.append(task)
            payload = json.dumps({"task": task, "created": True}).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(404)

    def do_PATCH(self) -> None:  # noqa: N802
        if "/v1/tasks/" in self.path:
            task_id = self.path.split("/v1/tasks/")[1]
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length))
            for task in self.__class__.tasks:
                if task["id"] == task_id:
                    task.update(data)
                    task["revision"] += 1
                    payload = json.dumps(task).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
            self.send_error(404)
            return
        self.send_error(404)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def mock_openclaw_server():
    _MockOpenClawBridgeHandler.tasks = [
        {
            "schema_version": "unified-task-v1",
            "id": "oc_task_legacy_wx01",
            "title": "微信群确认方案需求",
            "status": "pending",
            "priority": "P1",
            "project_id": None,
            "tags": ["微信"],
            "source": {"system": "wechat", "external_id": "wx-12345678"},
            "revision": 1,
        },
        {
            "schema_version": "unified-task-v1",
            "id": "oc_task_legacy_gen01",
            "title": "购买机械键盘",
            "status": "pending",
            "priority": "P2",
            "project_id": None,
            "tags": ["生活"],
            "source": {"system": "openclaw"},
            "revision": 1,
        },
    ]
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockOpenClawBridgeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_token_resolution(monkeypatch):
    assert resolve_openclaw_token("test-explicit") == "test-explicit"
    monkeypatch.setenv("OPENCLAW_TASK_BRIDGE_TOKEN", "env-token-xyz")
    assert resolve_openclaw_token() == "env-token-xyz"


def test_mock_bridge_list_and_dashboard(mock_openclaw_server):
    client = OpenClawBridgeClient(base_url=mock_openclaw_server, token="mock-token")
    assert client.is_healthy() is True

    tasks = client.list_tasks()
    assert len(tasks) == 2

    dashboard = client.get_unified_dashboard_todos()
    assert dashboard["total_open"] == 2
    assert len(dashboard["wechat_matters"]) == 1
    assert dashboard["wechat_matters"][0]["title"] == "微信群确认方案需求"
    assert len(dashboard["general_todos"]) == 1


def test_create_task_from_life_event(mock_openclaw_server):
    client = OpenClawBridgeClient(base_url=mock_openclaw_server, token="mock-token")

    event = LifeEvent(
        event_id="evt_20260929_todo_001",
        categories=["todo"],
        activity_type="task_created",
        title="明天联系供应商核对报价",
        time=EventTime(absolute="2026-09-29T10:00:00+08:00", relative_seconds=10.5, precision="exact"),
        data={"openclaw": {"candidate_type": "todo_candidate"}},
        capture_mode="passive",
        source_type="audio",
        confidence=0.95,
        evidence={"quote": "明天记得联系供应商核对一下报价", "recording_id": "rec_001"},
        review_status="unreviewed",
    )

    res = client.create_task_from_life_event(event)
    assert res["created"] is True
    assert res["task"]["title"] == "明天联系供应商核对报价"
    assert res["task"]["source"]["system"] == "life_logger"
    assert res["task"]["source"]["event_id"] == "evt_20260929_todo_001"

    # Idempotent second call
    res2 = client.create_task_from_life_event(event)
    assert res2["created"] is False
    assert res2["task"]["id"] == res["task"]["id"]


def test_sync_todo_events_batch(mock_openclaw_server):
    client = OpenClawBridgeClient(base_url=mock_openclaw_server, token="mock-token")

    events = [
        {
            "event_id": "evt_001",
            "categories": ["expense"],
            "title": "买咖啡 25 元",
            "data": {},
        },
        {
            "event_id": "evt_002",
            "categories": ["todo"],
            "title": "给妈妈打电话",
            "data": {"openclaw": {"candidate_type": "todo_candidate"}},
            "evidence": {"quote": "记得晚上给妈妈打个电话"},
        },
    ]

    outcomes = client.sync_todo_events(events)
    assert len(outcomes) == 1
    assert outcomes[0]["event_id"] == "evt_002"
    assert outcomes[0]["created"] is True
    assert outcomes[0]["title"] == "给妈妈打电话"
