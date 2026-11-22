"""Control endpoints must stay fast even with a large Watch history.

Regression target (2026-10-23): per-session formal-ASR summaries read every
segment and made /api/watch-reception take ~30s; /api/version and /api/jobs
then queued behind the same GIL. These tests use 45 sessions and assert the
control snapshots return in <2s without calling the expensive ASR summary.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from life_logger.local_web import JobStore, LocalWebServer, create_server
from life_logger.openclaw_bridge import OpenClawBridgeClient
from life_logger.watch_formal_asr import WatchFormalASRProcessor

TOKEN = "control-token"


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _request(base: str, path: str) -> tuple[int, dict, float]:
    started = time.monotonic()
    request = urllib.request.Request(
        base + path, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
            return response.status, body, time.monotonic() - started
    except urllib.error.HTTPError as error:
        body = json.loads(error.read().decode("utf-8"))
        return error.code, body, time.monotonic() - started


def _make_watch_sessions(root: Path, count: int) -> None:
    for index in range(count):
        _write(
            root / "watch-sessions" / "dev" / f"session-{index:04d}" / "manifest.json",
            {
                "device_id": "dev",
                "session_id": f"session-{index:04d}",
                "state": "complete",
                "started_at": "2026-10-20T10:00:00+08:00",
                "updated_at": "2026-10-20T10:01:00+08:00",
                "segments": [],
            },
        )


@pytest.fixture()
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "local-web"
    for index in range(45):
        _write(
            root / "watch-sessions" / "dev" / f"session-{index:04d}" / "manifest.json",
            {
                "device_id": "dev",
                "session_id": f"session-{index:04d}",
                "state": "complete",
                "started_at": "2026-10-20T10:00:00+08:00",
                "updated_at": "2026-10-20T10:01:00+08:00",
                "segments": [],
            },
        )
    config = tmp_path / "local-config.json"
    config.write_text("{}", encoding="utf-8")
    store = JobStore(root, recover=False)

    monkeypatch.setattr(LocalWebServer, "_watch_live_loop", lambda self: None)
    monkeypatch.setattr(LocalWebServer, "_reminder_loop", lambda self: None)
    monkeypatch.setattr(LocalWebServer, "_auto_ingest_loop", lambda self: None)
    monkeypatch.setattr(LocalWebServer, "_warm_vip_transcriber", staticmethod(lambda: None))
    server = create_server(
        host="127.0.0.1", port=0, store=store, config_path=config, access_token=TOKEN
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


def test_version_control_endpoint_is_fast(api) -> None:
    base, _root = api
    status, payload, elapsed = _request(base, "/api/version")
    assert status == 200
    assert payload["app_version"]
    assert elapsed < 2.0, f"/api/version took {elapsed:.3f}s"


def test_version_cached_flag_is_real_and_never_runs_git(api, monkeypatch: pytest.MonkeyPatch) -> None:
    base, _root = api
    from life_logger import version as version_module

    def forbidden_git(*_args, **_kwargs):
        raise AssertionError("request path must not run git")

    monkeypatch.setattr(version_module, "git_commit", forbidden_git)
    first = _request(base, "/api/version")[1]
    second = _request(base, "/api/version")[1]
    assert first["cached"] is False
    assert second["cached"] is True

def test_jobs_control_endpoint_is_fast(api) -> None:
    base, _root = api
    status, payload, elapsed = _request(base, "/api/jobs")
    assert status == 200
    assert "jobs" in payload
    assert elapsed < 2.0, f"/api/jobs took {elapsed:.3f}s"


def test_control_endpoints_do_not_wait_for_openclaw(api, monkeypatch: pytest.MonkeyPatch) -> None:
    """Slow OpenClaw must not be on the version/jobs/watch control path."""
    base, _root = api

    def slow_openclaw(*_args, **_kwargs):
        time.sleep(3.0)
        return {"tasks": []}

    monkeypatch.setattr(OpenClawBridgeClient, "is_healthy", lambda self: time.sleep(3.0) or False)
    monkeypatch.setattr(OpenClawBridgeClient, "list_tasks", slow_openclaw)
    for path in ("/api/version", "/api/jobs", "/api/watch-reception"):
        status, _payload, elapsed = _request(base, path)
        assert status == 200
        assert elapsed < 2.0, f"{path} took {elapsed:.3f}s while OpenClaw was slow"

def test_tasks_returns_degraded_when_openclaw_times_out(api, monkeypatch: pytest.MonkeyPatch) -> None:
    base, _root = api

    def slow_then_fail(self):
        time.sleep(3.0)
        raise RuntimeError("OpenClaw timed out")

    monkeypatch.setattr(OpenClawBridgeClient, "get_unified_dashboard_todos", slow_then_fail)
    status, payload, elapsed = _request(base, "/api/tasks")
    assert status == 200
    assert elapsed < 4.0, f"/api/tasks took {elapsed:.3f}s"
    assert payload["openclaw"]["degraded"] is True
    assert payload["openclaw"]["retry_after"] == 5
    assert payload["openclaw"].get("error") == "openclaw_unavailable"

def test_20_concurrent_jobs_busy_returns_http_error_not_disconnect(api, monkeypatch: pytest.MonkeyPatch) -> None:
    base, _root = api
    calls: list[int] = []

    def slow_list(self):
        calls.append(1)
        time.sleep(1.5)
        return []

    monkeypatch.setattr(JobStore, "list", slow_list)
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: _request(base, "/api/jobs"), range(20)))
    assert len(calls) == 1, f"jobs producer called {len(calls)} times"
    assert all(item[0] in {200, 503} for item in results)
    assert all(item[0] != 0 for item in results)
    assert any(item[0] == 200 for item in results)
    assert any(item[0] == 503 for item in results)
    busy = next(item[1] for item in results if item[0] == 503)
    assert busy["snapshot_busy"] is True and busy["retry_after"] == 1
    time.sleep(0.2)
    status, payload, _elapsed = _request(base, "/api/jobs")
    assert status == 200 and payload["cached"] is True

def test_20_concurrent_watch_reception_busy_returns_http_error(api, monkeypatch: pytest.MonkeyPatch) -> None:
    base, _root = api
    calls: list[int] = []

    def slow_watch(self, sessions):
        calls.append(1)
        time.sleep(1.5)
        return {"sessions": sessions, "degraded": True, "status_summary": {"complete": False}}

    monkeypatch.setattr(LocalWebServer, "_fast_watch_reception_payload", slow_watch)
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: _request(base, "/api/watch-reception"), range(20)))
    assert len(calls) == 1, f"watch producer called {len(calls)} times"
    assert all(item[0] in {200, 503} for item in results)
    assert all(item[0] != 0 for item in results)
    assert any(item[0] == 200 for item in results)
    assert any(item[0] == 503 for item in results)
    busy = next(item[1] for item in results if item[0] == 503)
    assert busy["snapshot_busy"] is True and busy["retry_after"] == 1

def test_20_concurrent_version_busy_returns_static_not_disconnect(api, monkeypatch: pytest.MonkeyPatch) -> None:
    base, _root = api
    calls: list[int] = []
    original = LocalWebServer._fast_version_info

    def slow_version(self):
        calls.append(1)
        time.sleep(1.5)
        return original(self)

    monkeypatch.setattr(LocalWebServer, "_fast_version_info", slow_version)
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: _request(base, "/api/version"), range(20)))
    assert len(calls) == 1
    assert all(item[0] == 200 for item in results)
    assert any(item[1].get("snapshot_busy") is True for item in results)
    assert all(item[1].get("app_version") for item in results)

def test_20_concurrent_health_busy_never_disconnects(api, monkeypatch: pytest.MonkeyPatch) -> None:
    base, _root = api
    original = LocalWebServer._fast_version_info

    def slow_version(self):
        time.sleep(1.5)
        return original(self)

    monkeypatch.setattr(LocalWebServer, "_fast_version_info", slow_version)
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: _request(base, "/api/health"), range(20)))
    assert all(item[0] in {200, 503} for item in results)
    assert all(item[0] != 0 for item in results)
    assert any(item[0] == 200 for item in results)
    assert any(item[0] == 503 for item in results)

def test_20_concurrent_control_endpoints_never_disconnect(api, monkeypatch: pytest.MonkeyPatch) -> None:
    base, _root = api

    def fake_tasks(self):
        return {"total_open": 0, "wechat_matters": [], "life_logger_todos": [], "general_todos": [], "raw_count": 0}

    monkeypatch.setattr(OpenClawBridgeClient, "get_unified_dashboard_todos", fake_tasks)
    for path in ("/api/version", "/api/jobs", "/api/watch-reception", "/api/tasks", "/api/health"):
        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(lambda _: _request(base, path), range(20)))
        assert all(item[0] in {200, 503} for item in results), (path, [item[0] for item in results])
        assert all(item[0] != 0 for item in results), path

def test_tasks_cached_flag_is_real(api, monkeypatch: pytest.MonkeyPatch) -> None:
    base, _root = api

    def fake_tasks(self):
        return {"total_open": 0, "wechat_matters": [], "life_logger_todos": [], "general_todos": [], "raw_count": 0}

    monkeypatch.setattr(OpenClawBridgeClient, "get_unified_dashboard_todos", fake_tasks)
    first = _request(base, "/api/tasks")[1]["openclaw"]
    second = _request(base, "/api/tasks")[1]["openclaw"]
    assert first["cached"] is False
    assert second["cached"] is True
    assert first["degraded"] is False
    assert second["degraded"] is False

def test_tasks_cold_20_concurrent_only_one_openclaw_call(api, monkeypatch: pytest.MonkeyPatch) -> None:
    base, _root = api
    calls: list[float] = []

    def fake_tasks(self):
        calls.append(time.monotonic())
        time.sleep(0.5)
        return {"total_open": 0, "wechat_matters": [], "life_logger_todos": [], "general_todos": [], "raw_count": 0}

    monkeypatch.setattr(OpenClawBridgeClient, "get_unified_dashboard_todos", fake_tasks)
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: _request(base, "/api/tasks"), range(20)))
    assert all(item[0] == 200 for item in results)
    assert len(calls) == 1, f"OpenClaw called {len(calls)} times for 20 concurrent requests"

    first = _request(base, "/api/tasks")[1]["openclaw"]
    second = _request(base, "/api/tasks")[1]["openclaw"]
    assert first["cached"] is True
    assert second["cached"] is True

def test_watch_reception_degrades_instead_of_scanning_all_segments(
    api, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, _root = api

    def slow_summary(*_args, **_kwargs):
        time.sleep(2.0)
        return {}

    monkeypatch.setattr(WatchFormalASRProcessor, "summary_for_session", slow_summary)
    status, payload, elapsed = _request(base, "/api/watch-reception")
    assert status == 200
    assert elapsed < 2.0, f"/api/watch-reception took {elapsed:.3f}s"
    assert payload.get("degraded") is True
    assert payload["status_summary"].get("degraded") is True
    assert payload["status_summary"]["session_count"] == 45


@pytest.mark.parametrize("count", [31, 40, 145])
def test_watch_reception_large_history_has_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    root = tmp_path / "local-web"
    _make_watch_sessions(root, count)
    config = tmp_path / "local-config.json"
    config.write_text("{}", encoding="utf-8")
    store = JobStore(root, recover=False)
    monkeypatch.setattr(LocalWebServer, "_watch_live_loop", lambda self: None)
    monkeypatch.setattr(LocalWebServer, "_reminder_loop", lambda self: None)
    monkeypatch.setattr(LocalWebServer, "_auto_ingest_loop", lambda self: None)
    monkeypatch.setattr(LocalWebServer, "_warm_vip_transcriber", staticmethod(lambda: None))
    server = create_server(host="127.0.0.1", port=0, store=store, config_path=config, access_token=TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, payload, elapsed = _request(base, "/api/watch-reception")
        assert status == 200
        assert elapsed < 4.0, f"{count} sessions took {elapsed:.3f}s"
        assert payload["total_sessions"] == count
        assert payload["returned_sessions"] == min(count, 50)
        assert payload["truncated"] is (count > 50)
        assert payload["degraded"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
