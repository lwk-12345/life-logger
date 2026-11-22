import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from life_logger import local_web


@dataclass(frozen=True)
class Dashboard:
    value: str

    def to_dict(self):
        return {"value": self.value}


def server(tmp_path, monkeypatch):
    app = local_web.LocalWebServer.__new__(local_web.LocalWebServer)
    app.store = SimpleNamespace(root=tmp_path)
    app._sync_external_write_epoch = lambda: None
    monkeypatch.setattr(local_web, "read_cache_epoch", lambda root: 0)
    app._read_cache_lock = threading.Lock()
    app._read_cache = {"e0|dashboard|day": (time.monotonic() - 600, Dashboard("old"))}
    app._read_inflight = {}
    app._read_inflight_meta = {}
    app._read_producer_slots = threading.BoundedSemaphore(1)
    app._read_producer_timeout = 0.03
    app._read_producer_run_timeout = 0.03
    app._read_producers_active = 0
    # [2026-11-04] `LocalWebServer.__init__` 后来新增了两个后台 producer 计数与上限；
    # 本测试用 `__new__` 绕过 __init__，必须同步补齐，否则会在
    # `local_web.py` 用到它们的地方抛 AttributeError（与本次改动无关的存量缺口）。
    app._read_producers_background = 0
    app._read_producer_background_limit = 1
    app._read_slot_waiting = 0
    app._read_cache_max_entries = 10
    return app


def test_deadline_marks_stale_without_rejuvenating_and_retains_flight(tmp_path, monkeypatch):
    app = server(tmp_path, monkeypatch)
    release = threading.Event()
    def producer():
        assert release.wait(2)
        return Dashboard("new")
    old_timestamp = app._read_cache["e0|dashboard|day"][0]
    try:
        result = app.cached_read("dashboard|day", producer)
        assert result.value == "old"
        assert getattr(result, "_read_status", None) == "stale_busy"
        assert app._read_cache["e0|dashboard|day"][0] == old_timestamp
        assert app._read_inflight_meta["e0|dashboard|day"]["background"]
        follower = app.cached_read("dashboard|day", producer)
        assert getattr(follower, "_read_status", None) == "stale_busy"
    finally:
        release.set()
    deadline = time.monotonic() + 2
    while app._read_inflight and time.monotonic() < deadline:
        time.sleep(0.01)
    fresh = app.cached_read("dashboard|day", producer)
    assert fresh.value == "new"
    assert getattr(fresh, "_read_status", None) is None


def test_slot_exhaustion_marks_dashboard_snapshot(tmp_path, monkeypatch):
    app = server(tmp_path, monkeypatch)
    assert app._read_producer_slots.acquire()
    try:
        result = app.cached_read("dashboard|day", lambda: Dashboard("new"))
        assert getattr(result, "_read_status", None) == "stale_busy"
    finally:
        app._read_producer_slots.release()


def test_cold_timeout_keeps_single_producer_without_fabricating_snapshot(tmp_path, monkeypatch):
    app = server(tmp_path, monkeypatch)
    app._read_cache.clear()
    release = threading.Event()
    calls = []
    def producer():
        calls.append(1)
        assert release.wait(2)
        return Dashboard("new")
    try:
        with pytest.raises(local_web.ReadProducerBusy) as first:
            app.cached_read("dashboard|day", producer)
        assert first.value.stale is None and first.value.background
        with pytest.raises(local_web.ReadProducerBusy) as second:
            app.cached_read("dashboard|day", producer)
        assert second.value.stale is None and second.value.background
        assert len(calls) == 1
    finally:
        release.set()
    deadline = time.monotonic() + 2
    while app._read_inflight and time.monotonic() < deadline:
        time.sleep(0.01)
    assert app.cached_read("dashboard|day", producer).value == "new"


def test_dashboard_http_timeout_reports_stale_then_fresh(tmp_path, monkeypatch):
    import json
    import urllib.request
    from life_logger.daily_dashboard import DailyDashboardBuilder

    release = threading.Event()
    def build(_self, _date):
        assert release.wait(3)
        return Dashboard("new")
    monkeypatch.setattr(DailyDashboardBuilder, "build_for_date", build)
    monkeypatch.setattr(local_web, "_watch_last_segment_received", lambda *_: {})
    app = local_web.create_server(port=0, store=local_web.JobStore(tmp_path / "local-web", recover=False))
    app._read_producer_run_timeout = 0.05
    app._sync_external_write_epoch = lambda: None
    monkeypatch.setattr(local_web, "read_cache_epoch", lambda root: 0)
    key = "e0|dashboard|2026-10-26"
    app._read_cache[key] = (time.monotonic() - 600, Dashboard("old"))
    thread = threading.Thread(target=app.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{app.server_port}/api/dashboard/daily?date=2026-10-26"
    try:
        def get():
            with urllib.request.urlopen(url, timeout=3) as response:
                return json.load(response)
        stale = get()
        assert stale["dashboard"]["value"] == "old"
        assert stale["read_status"] == "stale_busy"
        assert stale["read_note"]
        assert get()["read_status"] == "stale_busy"
        release.set()
        deadline = time.monotonic() + 2
        while app._read_inflight and time.monotonic() < deadline:
            time.sleep(0.01)
        fresh = get()
        assert fresh["dashboard"]["value"] == "new"
        assert "read_status" not in fresh
    finally:
        release.set()
        app.shutdown()
        app.server_close()
        thread.join(timeout=2)


def test_concurrent_followers_receive_stale_without_new_producers(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    app = server(tmp_path, monkeypatch)
    release = threading.Event()
    calls = []
    def producer():
        calls.append(1)
        assert release.wait(3)
        return Dashboard("new")
    try:
        assert getattr(app.cached_read("dashboard|day", producer), "_read_status") == "stale_busy"
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: app.cached_read("dashboard|day", producer), range(36)))
        assert all(value.value == "old" and getattr(value, "_read_status") == "stale_busy" for value in results)
        assert len(calls) == 1
        assert getattr(app._read_cache["e0|dashboard|day"][1], "_read_status", None) is None
    finally:
        release.set()
    deadline = time.monotonic() + 2
    while app._read_inflight and time.monotonic() < deadline:
        time.sleep(0.01)
    assert app.cached_read("dashboard|day", producer).value == "new"


def test_daily_http_reports_stale_then_fresh(tmp_path, monkeypatch):
    import json
    import urllib.request

    release = threading.Event()
    def brief(**kwargs):
        assert release.wait(3)
        return {"date": "2026-09-21", "events": [], "event_count": 0, "marker": "new"}
    app = local_web.create_server(port=0, store=local_web.JobStore(tmp_path / "local-web", recover=False))
    monkeypatch.setattr(app, "build_daily_brief", brief)
    monkeypatch.setattr(app, "_sync_external_write_epoch", lambda: None)
    monkeypatch.setattr(local_web, "read_cache_epoch", lambda root: 0)
    monkeypatch.setattr(local_web, "_capture_funnel_nonblocking", lambda *_: {})
    app._read_producer_run_timeout = 0.05
    key = "e0|daily|2026-09-21|provisional|display|True"
    app._read_cache[key] = (time.monotonic() - 600, {"date": "2026-09-21", "events": [], "event_count": 0, "marker": "old"})
    thread = threading.Thread(target=app.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{app.server_port}/api/daily?date=2026-09-21"
    try:
        def get():
            with urllib.request.urlopen(url, timeout=3) as response:
                return json.load(response)
        stale = get()
        assert stale["daily_summary"]["marker"] == "old"
        assert stale["read_status"] == "stale_busy"
        release.set()
        deadline = time.monotonic() + 2
        while app._read_inflight and time.monotonic() < deadline:
            time.sleep(0.01)
        fresh = get()
        assert fresh["daily_summary"]["marker"] == "new"
        assert "read_status" not in fresh
    finally:
        release.set()
        app.shutdown()
        app.server_close()
        thread.join(timeout=2)

def test_cold_follower_timeout_never_starts_duplicate(tmp_path, monkeypatch):
    app = server(tmp_path, monkeypatch)
    app._read_cache.clear()
    class TimedOutEvent(threading.Event):
        def wait(self, timeout=None):
            return False
    flight = TimedOutEvent()
    app._read_inflight["e0|dashboard|day"] = flight
    calls = []
    monkeypatch.setattr(app, "_produce_with_read_limit", lambda *args: calls.append(1))
    with pytest.raises(local_web.ReadProducerBusy) as busy:
        app._follow_read_flight("e0|dashboard|day", flight, lambda: None, 10)
    assert busy.value.stale is None
    assert not calls
    assert app._read_inflight["e0|dashboard|day"] is flight


def test_background_failure_releases_flight_and_allows_retry(tmp_path, monkeypatch):
    app = server(tmp_path, monkeypatch)
    app._read_cache.clear()
    release = threading.Event()
    def broken():
        assert release.wait(2)
        raise ValueError("background failure")
    try:
        with pytest.raises(local_web.ReadProducerBusy):
            app.cached_read("dashboard|day", broken)
        release.set()
        deadline = time.monotonic() + 2
        while app._read_inflight and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not app._read_inflight
        assert not app._read_cache
        assert app.cached_read("dashboard|day", lambda: Dashboard("recovered")).value == "recovered"
    finally:
        release.set()


def test_dashboard_http_cold_busy_then_fresh(tmp_path, monkeypatch):
    import json
    import urllib.request
    from life_logger.daily_dashboard import DailyDashboardBuilder

    release = threading.Event()
    calls = []
    def build(_self, _date):
        calls.append(1)
        assert release.wait(3)
        return Dashboard("fresh")
    monkeypatch.setattr(DailyDashboardBuilder, "build_for_date", build)
    monkeypatch.setattr(local_web, "_watch_last_segment_received", lambda *_: {})
    app = local_web.create_server(port=0, store=local_web.JobStore(tmp_path / "local-web", recover=False))
    app._read_producer_run_timeout = 0.05
    app._sync_external_write_epoch = lambda: None
    monkeypatch.setattr(local_web, "read_cache_epoch", lambda root: 0)
    thread = threading.Thread(target=app.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{app.server_port}/api/dashboard/daily?date=2026-10-26"
    try:
        for _ in range(2):
            with urllib.request.urlopen(url, timeout=3) as response:
                body = json.load(response)
                assert response.status == 202
                assert body["error"] == "read_busy"
                assert body["retry_after_ms"] > 0
        assert len(calls) == 1
        release.set()
        deadline = time.monotonic() + 2
        while app._read_inflight and time.monotonic() < deadline:
            time.sleep(0.01)
        with urllib.request.urlopen(url, timeout=3) as response:
            body = json.load(response)
            assert response.status == 200
            assert body["dashboard"]["value"] == "fresh"
            assert "read_status" not in body
    finally:
        release.set()
        app.shutdown()
        app.server_close()
        thread.join(timeout=2)
