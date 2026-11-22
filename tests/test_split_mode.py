"""split 模式：Web 保留现有读路径和 VIP，后台循环全部交给独立 Worker。"""
import ast
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from life_logger.local_web import JobStore, create_server
from life_logger.processing_runtime import ProcessingHost, ProcessingWorker
from life_logger.runtime_launch import refuse_legacy_processing
from life_logger.runtime_queue import TaskQueue

BACKGROUND_NAMES = {
    "activity-timeline-10min",
    "watch-formal-asr",
    "life-logger-local-reminders",
    "life-logger-auto-ingest",
}


def _config(tmp_path, *, timeline=False, capture=False) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "activity_timeline": {"enabled": timeline},
        "input_context_capture": {"enabled": capture},
    }))
    return path


def _web(tmp_path, monkeypatch, **config):
    monkeypatch.setenv("LIFE_LOGGER_DISABLE_VIP_WARMUP", "1")
    root = tmp_path / "root"
    store = JobStore(root, recover=False)
    return create_server(
        host="127.0.0.1", port=0, store=store,
        config_path=_config(tmp_path, **config), processing_mode="split",
    )


def test_split_web_starts_no_background_loops(tmp_path, monkeypatch):
    before = {t.name for t in threading.enumerate()}
    server = _web(tmp_path, monkeypatch, timeline=True)
    try:
        started = {t.name for t in threading.enumerate()} - before
        assert not started & BACKGROUND_NAMES
    finally:
        server.server_close()


def test_split_web_keeps_the_existing_read_path(tmp_path, monkeypatch):
    server = _web(tmp_path, monkeypatch)
    try:
        assert server.processing_mode == "split"
        # external 模式（WebReadService）已删除：读接口只会走 combined 那条路径
        assert not hasattr(server, "processing")
    finally:
        server.server_close()


def test_split_web_sends_jobs_to_the_queue_instead_of_running_them(tmp_path, monkeypatch):
    server = _web(tmp_path, monkeypatch)
    try:
        assert isinstance(server.store.dispatch_queue, TaskQueue)
        assert server.store.dispatch_queue.root == server.store.root
    finally:
        server.server_close()


def test_split_web_reports_worker_health(tmp_path, monkeypatch):
    server = _web(tmp_path, monkeypatch)
    try:
        assert server.processing_health()["online"] is False
    finally:
        server.server_close()


# 读模型层（projection / DayViewStore / DayProjector）已于 2026-11-04 删除：
# 原来三条用例里两条只测已删除的 API（不再扫描 / 仍会建 projection），一并删除；
# 保留下面的「产物变化仍要通知 Web 失效」——这是必须保留的行为。
def test_worker_still_tells_web_when_data_changes(tmp_path):
    worker = ProcessingWorker(tmp_path / "root", _config(tmp_path), background=False)
    reasons = []
    worker._bump_read_epoch = reasons.append
    worker.refresh_catalog()
    assert reasons
    worker.host.close()


@pytest.fixture
def quiet_loops(monkeypatch):
    for flag in ("WATCH_LOOP", "AUTO_INGEST", "REMINDERS", "VIP_WARMUP"):
        monkeypatch.setenv(f"LIFE_LOGGER_DISABLE_{flag}", "1")


def _host(tmp_path, config):
    queue = TaskQueue(tmp_path / "root")
    return ProcessingHost(tmp_path / "root", config, queue)


def test_host_runs_activity_timeline_when_enabled(tmp_path, quiet_loops):
    host = _host(tmp_path, _config(tmp_path, timeline=True))
    ticks = []
    host.activity_timeline.tick = lambda: ticks.append(1)
    host._activity_timeline_wait = 0.01
    host.start()
    try:
        deadline = time.time() + 3
        while not ticks and time.time() < deadline:
            time.sleep(0.01)
        assert ticks
    finally:
        host.close()


def test_host_skips_activity_timeline_when_disabled(tmp_path, quiet_loops):
    host = _host(tmp_path, _config(tmp_path, timeline=False))
    host.start()
    try:
        assert "worker-activity-timeline" not in {t.name for t in host.threads}
    finally:
        host.close()


def test_host_starts_and_stops_input_context_when_enabled(tmp_path, quiet_loops):
    host = _host(tmp_path, _config(tmp_path, capture=True))
    events = []

    class Manager:
        def start(self):
            events.append("start")

        def stop(self):
            events.append("stop")

    host.input_context_manager = Manager()
    host.start()
    host.close()
    assert events == ["start", "stop"]


def test_host_leaves_input_context_off_when_disabled(tmp_path, quiet_loops):
    host = _host(tmp_path, _config(tmp_path, capture=False))
    host.input_context_manager = None
    host.start()
    host.close()


def test_split_role_counts_as_safe_for_the_worker(tmp_path, monkeypatch):
    from life_logger import local_web
    import os
    root = tmp_path / "root"
    (root / "runtime").mkdir(parents=True)
    (root / "runtime" / "web.json").write_text(json.dumps({"pid": os.getpid(), "mode": "split", "port": 8765}))
    monkeypatch.setattr(local_web, "DEFAULT_JOB_ROOT", root)
    refuse_legacy_processing(root)


def _moved_methods():
    source = (Path(__file__).resolve().parents[1] / "src/life_logger/processing_runtime.py").read_text(encoding="utf-8")
    return re.findall(r"'(\w+)'", re.search(r"methods = \((.*?)\)\n", source, re.S).group(1))


def test_every_attribute_used_by_moved_methods_exists_on_the_host(tmp_path, quiet_loops):
    folder = Path(__file__).resolve().parents[1] / "src/life_logger"
    methods = {}
    # 方法可能在 LocalWebServer 本身或它的 mixin 里（local_web 拆分后）。
    for path in [folder / "local_web.py", *folder.glob("web_server_*.py")]:
        for cls in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(cls, ast.ClassDef) and (cls.name == "LocalWebServer" or cls.name.startswith("_Server")):
                methods.update({n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)})
    host = _host(tmp_path, _config(tmp_path))
    missing = {}
    for name in _moved_methods():
        for node in ast.walk(methods[name]):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
                if not hasattr(host, node.attr):
                    missing.setdefault(node.attr, []).append(name)
    host.close()
    assert missing == {}


def test_host_invalidate_event_reads_tells_web_through_the_epoch(tmp_path, quiet_loops, monkeypatch):
    from life_logger import local_web
    calls = []
    monkeypatch.setattr(local_web, "invalidate_read_caches", lambda root, reason: calls.append((root, reason)))
    host = _host(tmp_path, _config(tmp_path))
    host.invalidate_event_reads("watch_formal_asr_segment")
    host.close()
    assert calls == [(host.store.root, "watch_formal_asr_segment")]


def _hold_write_lock(queue):
    db = sqlite3.connect(queue.path, isolation_level=None)
    db.execute("BEGIN IMMEDIATE")
    return db


def test_idle_claim_does_not_need_the_write_lock(tmp_path):
    """空队列轮询不抢写锁，否则 6 个线程每 0.1 秒抢一次，磁盘一卡就撞锁。"""
    queue = TaskQueue(tmp_path)
    holder = _hold_write_lock(queue)
    try:
        started = time.monotonic()
        assert queue.claim("w", kinds=("pipeline",)) is None
        assert time.monotonic() - started < 1
    finally:
        holder.close()


def test_claim_still_takes_a_queued_task(tmp_path):
    queue = TaskQueue(tmp_path)
    queue.enqueue("pipeline", "k", {"job_id": "j"})
    task = queue.claim("w", kinds=("pipeline",))
    assert task["status"] == "running" and task["owner"] == "w"
    assert queue.claim("other", kinds=("pipeline",)) is None


def test_consumer_thread_survives_a_locked_queue(tmp_path, monkeypatch, capsys):
    worker = ProcessingWorker(tmp_path, _config(tmp_path), background=False)
    calls = []

    def locked_then_stop(kinds):
        calls.append(kinds)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        worker.stop.set()
        return False

    monkeypatch.setattr(worker, "consume_once", locked_then_stop)
    monkeypatch.setattr(worker, "QUEUE_RETRY_SECONDS", 0, raising=False)
    worker._consume_loop(("pipeline",))
    assert len(calls) == 2
    assert "database is locked" in capsys.readouterr().err


def test_heartbeat_failure_does_not_kill_the_worker(tmp_path, monkeypatch, capsys):
    worker = ProcessingWorker(tmp_path, _config(tmp_path), background=False)

    def locked(*_):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(worker.queue, "heartbeat", locked)
    worker.beat()
    assert "database is locked" in capsys.readouterr().err


def _split_worker(tmp_path, monkeypatch, **kw):
    worker = ProcessingWorker(tmp_path, _config(tmp_path), background=False, **kw)
    started = []
    monkeypatch.setattr(worker.host.store, "start_job", lambda job_id, *_a, **_k: started.append(job_id))
    return worker, started


def test_split_worker_resumes_jobs_it_just_marked_interrupted(tmp_path, monkeypatch):
    """状态提示承诺“恢复后自动继续”，split 下 Worker 必须兑现，否则任务永远停在中断。"""
    worker, started = _split_worker(tmp_path, monkeypatch)
    worker.resume_after_boot(["job-a", "job-b"])
    assert started == ["job-a", "job-b"]


def test_one_failed_resume_does_not_stop_the_others_or_the_worker(tmp_path, monkeypatch, capsys):
    worker, started = _split_worker(tmp_path, monkeypatch)

    def flaky(job_id, *_a, **_k):
        if job_id == "job-bad":
            raise ValueError("事件提取恢复正在运行")
        started.append(job_id)

    monkeypatch.setattr(worker.host.store, "start_job", flaky)
    worker.resume_after_boot(["job-bad", "job-ok"])
    assert started == ["job-ok"]
    assert "事件提取恢复正在运行" in capsys.readouterr().err


def test_slow_model_task_does_not_block_recording_work_and_worker_stops_cleanly(tmp_path):
    worker = ProcessingWorker(tmp_path, _config(tmp_path), background=False)
    started, release, ran = threading.Event(), threading.Event(), threading.Event()

    def slow(_payload):
        started.set()
        assert release.wait(5)
        return {"done": True}

    def pipeline(_payload):
        ran.set()
        return {"done": True}

    worker.handlers["synthesis"] = slow
    worker.handlers["pipeline"] = pipeline
    worker.queue.enqueue("synthesis", "model-one", {})
    worker.queue.enqueue("pipeline", "job-one", {"job_id": "job-one"})
    errors = []

    def run():
        try:
            worker.serve()
        except Exception as error:  # noqa: BLE001 - 测试要把异常带回主线程
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert started.wait(3)
        assert ran.wait(3), "慢的模型任务挡住了录音处理"
        assert worker.queue.health()["online"]
    finally:
        release.set()
        worker.stop.set()
        thread.join(5)
    assert not thread.is_alive()
    assert errors == []
    assert not worker.queue.health()["online"]
