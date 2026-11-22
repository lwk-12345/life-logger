"""event_view 的两层缓存（2026-10-24 P0：消除日接口 4s 读超时墙）。

背景：``/api/daily`` 冷读实测 4.1~5.6s，日志反复出现
``read-producer ... producer=4.00-4.26s timeout=True``；缓存命中只要 0.5~1s。
根因是**每次冷读都全量重扫** 220 个 ``daily_summary.json`` + 41 个 session，
在 ASR 满载时被放大到 4s+。

本轮加了两层，本文件锁住它们的语义：

1. **单文件 JSON 缓存**（键 = 路径 + mtime_ns + size）：文件一改就失效，
   所以「写后立刻读」永远拿到新内容；它只省 read_text/json.loads。
2. **扫描结果缓存**（``use_cache=True``，TTL 10s，single-flight）：
   只给 Web 只读路径用；测试/批处理默认关闭，保证写后读不陈旧。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

import life_logger.event_view as event_view
from life_logger.event_view import build_event_view


def _summary(root: Path, day: str, events: list[dict]) -> Path:
    directory = root / "jobs" / f"job-20260101-000000-{day.replace('-', '')}" / "files" / "abc" / "result" / day
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "daily_summary.json"
    path.write_text(json.dumps({"timeline": events}, ensure_ascii=False), encoding="utf-8")
    return path


def _event(event_id: str, title: str, absolute: str) -> dict:
    return {
        "event_id": event_id,
        "title": title,
        "categories": ["work_communication"],
        "time": {"absolute": absolute},
        "evidence": {"quote": title, "reported_at": absolute},
    }


@pytest.fixture(autouse=True)
def _clean_caches():
    with event_view._SCAN_CACHE_LOCK:
        event_view._SCAN_CACHE.clear()
        event_view._SCAN_INFLIGHT.clear()
    with event_view._JSON_CACHE_LOCK:
        event_view._JSON_CACHE.clear()
    yield
    with event_view._SCAN_CACHE_LOCK:
        event_view._SCAN_CACHE.clear()
        event_view._SCAN_INFLIGHT.clear()
    with event_view._JSON_CACHE_LOCK:
        event_view._JSON_CACHE.clear()


def test_default_call_is_never_cached_so_write_then_read_is_fresh(tmp_path):
    """默认 use_cache=False：写完立刻读必须拿到新内容（测试/批处理的安全默认）。"""
    _summary(tmp_path, "2026-10-21", [_event("e1", "旧内容", "2026-10-21T10:00:00+08:00")])
    first = build_event_view(tmp_path, date_from="2026-10-21", date_to="2026-10-21")
    assert [e["event_id"] for e in first["events"]] == ["e1"]

    _summary(tmp_path, "2026-10-21", [_event("e2", "新内容", "2026-10-21T11:00:00+08:00")])
    second = build_event_view(tmp_path, date_from="2026-10-21", date_to="2026-10-21")
    assert [e["event_id"] for e in second["events"]] == ["e2"], "默认路径不许返回旧快照"


def test_per_file_cache_invalidates_on_mtime_change(tmp_path):
    path = _summary(tmp_path, "2026-10-21", [_event("e1", "旧", "2026-10-21T10:00:00+08:00")])
    assert event_view._read_json_cached(path)["timeline"][0]["event_id"] == "e1"

    # 重写同一文件（内容变、mtime/size 变）→ 缓存必须失效
    path.write_text(json.dumps({"timeline": [_event("e9", "新", "2026-10-21T12:00:00+08:00")]},
                               ensure_ascii=False), encoding="utf-8")
    os_stat = path.stat()
    assert event_view._read_json_cached(path)["timeline"][0]["event_id"] == "e9"
    assert os_stat.st_size > 0


def test_scan_cache_is_reused_only_when_enabled(tmp_path):
    _summary(tmp_path, "2026-10-22", [_event("e1", "A", "2026-10-22T10:00:00+08:00")])
    cached_first = build_event_view(tmp_path, date_from="2026-10-22", date_to="2026-10-22", use_cache=True)
    assert [e["event_id"] for e in cached_first["events"]] == ["e1"]

    # 直接改文件（不经过写接口）→ 扫描缓存仍在 10s 内，所以 Web 路径读到旧值；
    # 这正是「Web 读最多陈旧 10s」的设计取舍，而不是 bug。
    _summary(tmp_path, "2026-10-22", [_event("e2", "B", "2026-10-22T11:00:00+08:00")])
    still_cached = build_event_view(tmp_path, date_from="2026-10-22", date_to="2026-10-22", use_cache=True)
    assert [e["event_id"] for e in still_cached["events"]] == ["e1"]

    # 未启用缓存的路径必须立刻看到新值
    fresh = build_event_view(tmp_path, date_from="2026-10-22", date_to="2026-10-22")
    assert [e["event_id"] for e in fresh["events"]] == ["e2"]


def test_scan_cache_expires_after_ttl(tmp_path, monkeypatch):
    _summary(tmp_path, "2026-10-23", [_event("e1", "A", "2026-10-23T10:00:00+08:00")])
    build_event_view(tmp_path, date_from="2026-10-23", date_to="2026-10-23", use_cache=True)
    _summary(tmp_path, "2026-10-23", [_event("e2", "B", "2026-10-23T11:00:00+08:00")])

    real = time.monotonic
    monkeypatch.setattr(event_view.time, "monotonic", lambda: real() + 11.0)
    after_ttl = build_event_view(tmp_path, date_from="2026-10-23", date_to="2026-10-23", use_cache=True)
    assert [e["event_id"] for e in after_ttl["events"]] == ["e2"]


def test_concurrent_same_window_scans_only_once(tmp_path, monkeypatch):
    """并发同 key 只允许一个 producer 真扫，其余等待复用（防 4s 排队）。"""
    _summary(tmp_path, "2026-10-24", [_event("e1", "A", "2026-10-24T10:00:00+08:00")])
    calls = {"n": 0}
    real_loader = event_view._load_formal_events

    def counted(*args, **kwargs):
        calls["n"] += 1
        time.sleep(0.2)
        return real_loader(*args, **kwargs)

    monkeypatch.setattr(event_view, "_load_formal_events", counted)
    import threading

    results = []
    def worker():
        view = build_event_view(tmp_path, date_from="2026-10-24", date_to="2026-10-24", use_cache=True)
        results.append(len(view["events"]))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not [t for t in threads if t.is_alive()]
    assert calls["n"] == 1, f"同一窗口被扫了 {calls['n']} 次（应只扫 1 次）"
    assert results == [1, 1, 1, 1]


def _web_source() -> str:
    """local_web 拆分后，方法分布在 local_web.py 和 web_*.py。"""
    folder = Path(event_view.__file__).parent
    return "\n".join(p.read_text(encoding="utf-8") for p in [folder / "local_web.py", *sorted(folder.glob("web_*.py"))])


def test_web_layer_enables_scan_cache_but_tests_do_not():
    """local_web 的 build_daily_brief 必须显式打开缓存；daily_brief 默认关闭。"""
    from life_logger import daily_brief as db

    source = Path(db.__file__).read_text(encoding="utf-8")
    assert "use_scan_cache: bool = False" in source
    assert "use_scan_cache=True" in _web_source()


# ---------------------------------------------------------------------------
# 2026-10-24 P0 收口：写后立即失效（不靠 sleep，不等 10s TTL）
# ---------------------------------------------------------------------------
def test_red_light_old_ttl_cache_would_serve_stale_without_invalidation(tmp_path):
    """红灯：证明"什么都不做"时缓存确实陈旧 —— 这条锁住问题本身。"""
    _summary(tmp_path, "2026-10-25", [_event("old", "旧", "2026-10-25T10:00:00+08:00")])
    first = build_event_view(tmp_path, date_from="2026-10-25", date_to="2026-10-25", use_cache=True)
    assert [e["event_id"] for e in first["events"]] == ["old"]

    _summary(tmp_path, "2026-10-25", [_event("new", "新", "2026-10-25T11:00:00+08:00")])
    stale = build_event_view(tmp_path, date_from="2026-10-25", date_to="2026-10-25", use_cache=True)
    assert [e["event_id"] for e in stale["events"]] == ["old"], "未失效时应读到旧快照（这正是要修的 bug）"


def test_explicit_invalidation_makes_next_build_see_new_file_immediately(tmp_path):
    _summary(tmp_path, "2026-10-26", [_event("old", "旧", "2026-10-26T10:00:00+08:00")])
    build_event_view(tmp_path, date_from="2026-10-26", date_to="2026-10-26", use_cache=True)

    _summary(tmp_path, "2026-10-26", [_event("new", "新", "2026-10-26T11:00:00+08:00")])
    removed = event_view.invalidate_scan_cache(tmp_path)
    assert removed >= 1
    fresh = build_event_view(tmp_path, date_from="2026-10-26", date_to="2026-10-26", use_cache=True)
    assert [e["event_id"] for e in fresh["events"]] == ["new"], "失效后必须立即看到新文件"


def test_invalidation_discards_inflight_scan_result(tmp_path):
    """失效发生在扫描中途时，那份"扫到一半"的结果不许写回缓存。"""
    import threading

    _summary(tmp_path, "2026-10-27", [_event("old", "旧", "2026-10-27T10:00:00+08:00")])
    real_loader = event_view._load_formal_events
    started = threading.Event()
    release = threading.Event()

    def slow_loader(*args, **kwargs):
        started.set()
        release.wait(5)
        return real_loader(*args, **kwargs)

    event_view._load_formal_events = slow_loader
    try:
        box = {}
        def run():
            box["view"] = build_event_view(tmp_path, date_from="2026-10-27", date_to="2026-10-27", use_cache=True)
        thread = threading.Thread(target=run)
        thread.start()
        assert started.wait(5)
        # 扫描进行中：文件被改 + 显式失效
        _summary(tmp_path, "2026-10-27", [_event("new", "新", "2026-10-27T11:00:00+08:00")])
        event_view.invalidate_scan_cache(tmp_path)
        release.set()
        thread.join(10)
        assert not thread.is_alive()
    finally:
        event_view._load_formal_events = real_loader

    # 旧结果已被丢弃：下一次读必须是新的
    after = build_event_view(tmp_path, date_from="2026-10-27", date_to="2026-10-27", use_cache=True)
    assert [e["event_id"] for e in after["events"]] == ["new"]


def test_invalidate_scan_cache_only_touches_given_root(tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _summary(root_a, "2026-10-28", [_event("a1", "A", "2026-10-28T10:00:00+08:00")])
    _summary(root_b, "2026-10-28", [_event("b1", "B", "2026-10-28T10:00:00+08:00")])
    build_event_view(root_a, date_from="2026-10-28", date_to="2026-10-28", use_cache=True)
    build_event_view(root_b, date_from="2026-10-28", date_to="2026-10-28", use_cache=True)
    with event_view._SCAN_CACHE_LOCK:
        keys_before = set(event_view._SCAN_CACHE)
    event_view.invalidate_scan_cache(root_a)
    with event_view._SCAN_CACHE_LOCK:
        keys_after = set(event_view._SCAN_CACHE)
    assert any(str(root_a) in key for key in keys_before)
    assert any(str(root_b) in key for key in keys_after), "另一个 root 的缓存不许被误删"
    assert not any(str(root_a) in key for key in keys_after)


def test_local_web_write_paths_invalidate_read_cache(monkeypatch):
    """写点接线：4 个事件写方法 + 2 个 Watch 写点都必须调用失效。"""
    source = _web_source()
    # JobStore 的四个写方法走模块级 invalidate_read_caches(self.root, ...)
    for reason in (
        "save_event_correction",
        "save_manual_event",
        "save_review_decision",
        "save_review_decisions_batch",
    ):
        assert f'invalidate_read_caches(self.root, "{reason}")' in source, reason
    # LocalWebServer 的两个 Watch 写点走实例方法
    for reason in ("watch_formal_asr_segment", "watch_session_imported"):
        assert f'invalidate_event_reads("{reason}")' in source, reason


def test_end_to_end_write_then_read_over_http_is_fresh(tmp_path, monkeypatch):
    """端到端：/api/daily 命中缓存 → 写一条人工事件 → 立刻再读必须包含它（不 sleep）。"""
    import threading
    import urllib.request

    from life_logger.local_web import JobStore, LocalWebServer, create_server

    # [2026-11-04 串扰修复] 必须用 monkeypatch：裸 setattr 会把 LocalWebServer 的
    # _watch_live_loop / _run_watch_formal_asr_scan 永久换成空函数，导致后续
    # test_local_web.py 的 5 个 watch-live-loop 用例（P03–P07）全部静默失效。
    for name in ("_watch_live_loop", "_reminder_loop", "_auto_ingest_loop",
                 "_warm_vip_transcriber", "_run_watch_formal_scan",
                 "_run_watch_formal_asr_scan"):
        if hasattr(LocalWebServer, name):
            monkeypatch.setattr(LocalWebServer, name, lambda self, *a, **k: None)

    store = JobStore(tmp_path, recover=False)
    job_id = store.create_job(start_at="2026-10-20T00:00:00+08:00")
    source = tmp_path / "jobs" / job_id / "files" / "abc" / "result" / "2026-10-20" / "events.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("[]", encoding="utf-8")
    relative = str(source.relative_to(tmp_path / "jobs" / job_id))

    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    server = create_server(host="127.0.0.1", port=0, store=store,
                          config_path=config, access_token="tok")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    headers = {"Authorization": "Bearer tok"}

    def read_daily():
        with urllib.request.urlopen(
            urllib.request.Request(f"{base}/api/daily?date=2026-10-20", headers=headers), timeout=30
        ) as response:
            return json.load(response)

    try:
        before = read_daily()
        assert not [e for e in before.get("events", []) if e.get("source_type") == "manual"]

        store.save_manual_event(
            job_id, artifact_path=relative, title="人工补录事件",
            categories=["expense"],
            absolute_time="2026-10-20T18:11:23+08:00", time_precision="inferred",
            activity_type="purchase",
            data={"expense": {"amount": None, "currency": "CNY", "item": "测试"}},
            note="写后立即可见",
        )

        after = read_daily()   # 不 sleep：靠写路径的显式失效
        titles = [e.get("title") for e in after.get("events", [])]
        assert "人工补录事件" in titles, titles
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 2026-10-24 P0 收口：HTTP 结果快照（cached_read 8s）也必须跟随跨进程 epoch
# ---------------------------------------------------------------------------
_CHILD_WRITE = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from life_logger.event_view import bump_scan_epoch

root = Path(sys.argv[2]); date = sys.argv[3]; titles = json.loads(sys.argv[4])
d = root / "jobs" / "job-1" / "files" / "abc" / "result" / date
d.mkdir(parents=True, exist_ok=True)
events = [{"event_id": f"c{i}", "title": t, "categories": ["work_communication"],
           "time": {"absolute": f"{date}T1{i}:00:00+08:00"},
           "evidence": {"quote": t, "reported_at": f"{date}T1{i}:00:00+08:00"}}
          for i, t in enumerate(titles)]
(d / "daily_summary.json").write_text(json.dumps({"timeline": events}, ensure_ascii=False), encoding="utf-8")
print(bump_scan_epoch(root, "child") or "")
"""


def _spawn_cross_process_write(root: Path, date: str, titles: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
    return subprocess.run(
        [sys.executable, "-c", _CHILD_WRITE, str(REPO / "src"), str(root), date,
         json.dumps(titles, ensure_ascii=False)],
        capture_output=True, text=True, timeout=60, env=env,
    )


def _quiet_server(tmp_path: Path, monkeypatch, token: str = "tok"):
    """起一个只在内存里跑的 LocalWebServer（关掉所有后台线程）。

    [2026-11-04 串扰修复] 补丁必须经 monkeypatch 打（见同文件另一处注释）：
    裸 setattr 会把这些方法永久留在空函数状态，污染同进程后续的用例。
    """
    import threading
    from life_logger.local_web import JobStore, LocalWebServer, create_server

    for name in ("_watch_live_loop", "_reminder_loop", "_auto_ingest_loop",
                 "_warm_vip_transcriber", "_run_watch_formal_scan",
                 "_run_watch_formal_asr_scan"):
        if hasattr(LocalWebServer, name):
            monkeypatch.setattr(LocalWebServer, name, lambda self, *a, **k: None)

    store = JobStore(tmp_path, recover=False)
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    server = create_server(host="127.0.0.1", port=0, store=store,
                           config_path=config, access_token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}", {"Authorization": f"Bearer {token}"}


def _bare_server(root: Path):
    """真实 LocalWebServer 实例（不 serve），复用 cached_read 的全部内部状态。"""
    from life_logger.local_web import JobStore, create_server

    config = root / "config.json"
    if not config.exists():
        config.write_text("{}", encoding="utf-8")
    server = create_server(host="127.0.0.1", port=0, store=JobStore(root, recover=False),
                           config_path=config, access_token="tok")
    return server

def test_cached_read_drops_snapshot_when_another_process_bumps(tmp_path):
    """cached_read 命中 8s 快照时也必须发现跨进程写（否则 /api/daily 陈旧 8s）。"""
    server = _bare_server(tmp_path)

    calls = {"n": 0}

    def producer():
        calls["n"] += 1
        return {"value": calls["n"], "fresh": (tmp_path / "value.txt").read_text()}

    (tmp_path / "value.txt").write_text("old", encoding="utf-8")
    assert server.cached_read("k", producer)["fresh"] == "old"
    assert server.cached_read("k", producer)["fresh"] == "old"
    assert calls["n"] == 1, "无写时应命中缓存（不能退化成每次都重建）"

    (tmp_path / "value.txt").write_text("new", encoding="utf-8")
    event_view.bump_scan_epoch(tmp_path, "external-writer")
    # ⚠️ 同进程 bump 已把 token 记进本进程 seen 集合；这里清掉 seen 模拟"别的进程"
    with event_view._EPOCH_LOCK:
        event_view._EPOCH_TOKENS.pop(event_view._root_key(tmp_path), None)

    assert server.cached_read("k", producer)["fresh"] == "new", "外部写后必须重建"
    assert calls["n"] == 2


def test_http_daily_reflects_cross_process_write_and_does_not_keep_penetrating(tmp_path, monkeypatch):
    """端到端：子进程写 + bump → 下一次 /api/daily 更新；随后缓存恢复命中。"""
    import urllib.request

    date = "2026-10-21"
    root = tmp_path / "output"
    root.mkdir()
    server, thread, base, headers = _quiet_server(root, monkeypatch)
    try:
        def read_daily():
            request = urllib.request.Request(f"{base}/api/daily?date={date}", headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)

        first = read_daily()
        assert [e.get("title") for e in first.get("events", [])] == []

        result = _spawn_cross_process_write(root, date, ["子进程写入的事件"])
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip(), "子进程必须 bump 出 token"

        second = read_daily()   # 不 sleep：靠 epoch
        assert "子进程写入的事件" in [e.get("title") for e in second.get("events", [])]

        # 无新写 → 必须恢复命中，不能持续穿透
        scans = {"n": 0}
        original = event_view._load_formal_events

        def counted(*args, **kwargs):
            scans["n"] += 1
            return original(*args, **kwargs)

        event_view._load_formal_events = counted
        try:
            read_daily()
            read_daily()
        finally:
            event_view._load_formal_events = original
        assert scans["n"] == 0, f"无新写时不应重扫，实际 {scans['n']} 次"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_cross_process_epoch_is_isolated_between_roots(tmp_path):
    """/api/daily 的 root 隔离：A 的 bump 不能清掉 B 的快照。"""
    servers = []
    for name in ("a", "b"):
        path = tmp_path / name
        path.mkdir()
        servers.append((_bare_server(path), path))

    (servers[0][1] / "v.txt").write_text("a1", encoding="utf-8")
    (servers[1][1] / "v.txt").write_text("b1", encoding="utf-8")
    calls = {"a": 0, "b": 0}

    def producer_for(name, path):
        def producer():
            calls[name] += 1
            return path.joinpath("v.txt").read_text()
        return producer

    for name, (server, path) in zip(("a", "b"), servers):
        server.cached_read("k", producer_for(name, path))
    assert calls == {"a": 1, "b": 1}

    event_view.bump_scan_epoch(servers[0][1], "only-a")
    with event_view._EPOCH_LOCK:
        event_view._EPOCH_TOKENS.pop(event_view._root_key(servers[0][1]), None)

    assert servers[0][0].cached_read("k", producer_for("a", servers[0][1])) is not None
    assert calls["a"] == 2, "A 应因 bump 重建"
    assert servers[1][0].cached_read("k", producer_for("b", servers[1][1])) is not None
    assert calls["b"] == 1, "B 不应被 A 的 bump 影响"


def test_cached_read_survives_epoch_detection_failure(tmp_path, monkeypatch):
    """代际检测抛异常时读接口不能挂：退化为普通 8s 快照缓存。"""
    server = _bare_server(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("epoch file unreadable")

    monkeypatch.setattr(event_view, "sync_scan_epoch", boom)
    calls = {"n": 0}

    def producer():
        calls["n"] += 1
        return calls["n"]

    assert server.cached_read("k", producer) == 1
    assert server.cached_read("k", producer) == 1
    assert calls["n"] == 1
