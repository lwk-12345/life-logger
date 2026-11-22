"""Tests for InputContextRuntimeManager lifecycle and bounded persistence.

Verifies:
1. Explicit start/stop lifecycle (no auto-start, clean stop).
2. Clean process management (stops and terminates tracked child processes).
3. Capacity cap strictly aligned with reader (2,000,000 bytes) and single window truncation.
4. Underlying collector bounded memory pruning (no leak on windows/front_samples/mic_events).
5. Safe bootstrapping from non-repo cwd without global sys.path pollution.
6. Strict timezone contract: naive rejected, aware preserved.
7. Rolling bounded retention (retains >=15 min closed windows, prunes >30 min).
8. Open window semantics (incomplete windows strictly marked target_unknown=True, complete=False).
9. Concurrent atomic publishing without file corruption.
10. End-to-end integration with WatchFormalASR reader.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock


from life_logger.input_context_runtime import (
    InputContextRuntimeManager,
    ARTIFACT_RELATIVE_PATH,
    SCHEMA_VERSION,
    DEFAULT_MAX_FILE_BYTES,
    SHANGHAI_TZ,
    _parse_iso,
    _iso,
)
from life_logger.watch_formal_asr import (
    load_input_context_windows,
    attach_watch_input_context,
    INPUT_CONTEXT_MAX_BYTES,
)

ROOT = Path(__file__).resolve().parents[1]


def _make_window(start: datetime, end: datetime | None, app: str = "com.openai.codex") -> dict:
    return {
        "started_at": start.astimezone(SHANGHAI_TZ).isoformat(),
        "ended_at": end.astimezone(SHANGHAI_TZ).isoformat() if end else None,
        "duration_seconds": round((end - start).total_seconds(), 3) if end else None,
        "provider": {"pid": 1234, "process": "DoubaoIme", "source": "mic_event"},
        "provider_pid": 1234,
        "front_bundles": [app],
        "target_app": app,
        "target_unknown": False,
        "complete": end is not None,
        "delivery_inferred": False,
    }


def test_capacity_cap_strictly_aligned_with_reader():
    """写者上限必须严格对齐读者上限 (2,000,000 字节)。"""
    assert DEFAULT_MAX_FILE_BYTES == INPUT_CONTEXT_MAX_BYTES == 2_000_000


def test_explicit_start_stop_lifecycle(tmp_path):
    """验证显式 start/stop 生命周期，默认不自启动，停止后状态正确。"""
    mgr = InputContextRuntimeManager(tmp_path, poll_interval_seconds=0.05)
    st_init = mgr.status()
    assert st_init["running"] is False
    assert st_init["artifact_exists"] is False

    mgr.start()
    assert mgr.is_running is True
    time.sleep(0.15)
    st_running = mgr.status()
    assert st_running["running"] is True
    assert st_running["artifact_exists"] is True

    mgr.stop(timeout_seconds=1.0)
    assert mgr.is_running is False
    st_stopped = mgr.status()
    assert st_stopped["running"] is False


def test_subprocess_cleanup_on_stop(tmp_path):
    """验证 stop() 自动终止并清理注册的子进程，杜绝僵尸进程。"""
    mgr = InputContextRuntimeManager(tmp_path)
    proc = subprocess.Popen(["sleep", "30"])
    mgr.register_subprocess(proc)
    assert proc.poll() is None

    mgr.stop()
    assert proc.poll() is not None


def test_single_window_oversize_strictly_capped(tmp_path):
    """验证单窗口超大时截断保护，产物文件大小严格 <= max_file_size_bytes。"""
    mgr = InputContextRuntimeManager(tmp_path, max_file_size_bytes=500)
    now = datetime.now().astimezone(SHANGHAI_TZ)
    padding = "x" * 2000
    huge_window = _make_window(now - timedelta(minutes=5), now - timedelta(minutes=4))
    huge_window["provider"]["note"] = padding

    mgr._windows_history = [huge_window]
    payload = mgr._prune_and_publish(now=now)

    target_file = tmp_path / ARTIFACT_RELATIVE_PATH
    assert target_file.is_file()
    assert target_file.stat().st_size <= 500
    assert payload.get("truncated_oversize") is True


def test_underlying_collector_memory_bounded_pruning(tmp_path):
    """验证底层 collector 的 windows、front_samples、mic_events 随采样滚动清理，阻断内存泄漏。"""
    from scripts.mac_input_context import InputContextCollector, FrontSample, MicEvent

    class DummySource:
        def sample(self): return FrontSample(datetime.now().astimezone(SHANGHAI_TZ), "com.app", "1")
        def poll(self, *a): return []

    collector = InputContextCollector(provider_pid="123", front_source=DummySource(), mic_source=DummySource())
    mgr = InputContextRuntimeManager(tmp_path, collector=collector)
    now = datetime.now().astimezone(SHANGHAI_TZ)

    # 模拟 50 次步进
    for i in range(50):
        t = now + timedelta(seconds=i * 2)
        collector.add_front_sample(FrontSample(t, "com.app", "1"))
        collector.add_mic_event(MicEvent(t, "123", True))
        collector.add_mic_event(MicEvent(t + timedelta(seconds=1), "123", False))
        mgr.step_once(at=t + timedelta(seconds=1.5))

    # 底层 collector 历史 windows 必须被清空（由 manager 集中接管），samples 和 mic_events 有界截断
    assert len(collector.windows) == 0
    assert len(collector.front_samples) <= 20
    assert len(collector.mic_events) <= 20


def test_safe_import_without_repo_cwd():
    """验证从非 repo cwd (如 /tmp) 且仅有 src 在 sys.path 时也能安全加载 collector。"""
    cmd = [
        sys.executable, "-c",
        f"import sys; sys.path.insert(0, '{str(ROOT / 'src')}'); "
        "from life_logger.input_context_runtime import InputContextRuntimeManager; "
        "mgr = InputContextRuntimeManager('/tmp/test_boot'); "
        "coll = mgr._ensure_collector(); "
        "assert coll is not None, 'Failed to bootstrap collector'"
    ]
    proc = subprocess.run(cmd, cwd="/tmp", capture_output=True, text=True)
    assert proc.returncode == 0, f"Bootstrap failed outside repo cwd: {proc.stderr}"


def test_naive_datetime_rejected_aware_preserved():
    """严格时区契约：naive 时间戳保留 tzinfo=None（并在导出/发布时被拒绝），aware 时间戳被正确保留。"""
    # 1. Naive: 绝不猜测上海或本地时区
    parsed_naive = _parse_iso("2026-10-26T00:00:00")
    assert parsed_naive.tzinfo is None, "Naive 时间绝不能被擅自标记上海时区"
    assert _iso(parsed_naive) is None, "Naive 时间在导出时必须被拒绝 (返回 None)"

    # 2. Aware: 正确保留
    parsed_aware = _parse_iso("2026-10-26T00:00:00+08:00")
    assert parsed_aware.tzinfo is not None
    assert _iso(parsed_aware) == "2026-10-26T00:00:00+08:00"

    parsed_z = _parse_iso("2026-10-25T16:00:00Z")
    assert parsed_z.tzinfo is not None
    assert _iso(parsed_z) == "2026-10-26T00:00:00+08:00"


def test_rolling_retention_and_30min_expiration(tmp_path):
    """验证滚动保留策略：修剪 >30 分钟窗口，确保保留最近 15 分钟闭合窗口。"""
    mgr = InputContextRuntimeManager(tmp_path, retention_minutes=30.0, min_closed_window_minutes=15.0)
    now = datetime.now().astimezone(SHANGHAI_TZ)

    w_old = _make_window(now - timedelta(minutes=40), now - timedelta(minutes=38))
    w_mid = _make_window(now - timedelta(minutes=25), now - timedelta(minutes=23))
    w_rec = _make_window(now - timedelta(minutes=10), now - timedelta(minutes=8))

    mgr._windows_history = [w_old, w_mid, w_rec]
    payload = mgr._prune_and_publish(now=now)

    windows = payload["windows"]
    starts = [w["started_at"] for w in windows]
    assert w_old["started_at"] not in starts
    assert w_mid["started_at"] in starts
    assert w_rec["started_at"] in starts


def test_open_window_marked_unknown_and_not_complete(tmp_path):
    """验证未闭合的录音中 open 窗口导出为 target_unknown=True，complete=False。"""
    mock_collector = MagicMock()
    now = datetime.now().astimezone(SHANGHAI_TZ)
    mock_collector.open_window = {"start": now - timedelta(seconds=15), "pid": 9999}
    mock_collector.windows = []

    mgr = InputContextRuntimeManager(tmp_path, collector=mock_collector)
    payload = mgr._prune_and_publish(now=now)

    assert len(payload["windows"]) == 1
    open_exported = payload["windows"][0]
    assert open_exported["ended_at"] is None
    assert open_exported["complete"] is False
    assert open_exported["target_unknown"] is True
    assert open_exported["reason"] == "recording_in_progress"
    assert open_exported["meaning"] == "open_window_target_unverified"


def test_concurrent_atomic_publish_integrity(tmp_path):
    """验证多线程并发发布下，latest.json 永远保持原子性，零损坏。"""
    mgr = InputContextRuntimeManager(tmp_path)
    now = datetime.now().astimezone(SHANGHAI_TZ)
    mgr._windows_history = [_make_window(now - timedelta(minutes=5), now - timedelta(minutes=4))]

    errors = []
    def worker():
        try:
            for _ in range(20):
                mgr._prune_and_publish(now=datetime.now().astimezone(SHANGHAI_TZ))
                target_path = tmp_path / ARTIFACT_RELATIVE_PATH
                content = target_path.read_text(encoding="utf-8")
                parsed = json.loads(content)
                assert parsed["schema_version"] == SCHEMA_VERSION
        except Exception as exc:
            errors.append(exc)

    import threading
    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert len(errors) == 0, f"Concurrent corruption errors: {errors}"


def test_end_to_end_watch_reader_compatibility(tmp_path):
    """端到端验证：WatchFormalASR 读者从 manager 发布的最新产物中能正确解析并附带上下文。"""
    mgr = InputContextRuntimeManager(tmp_path)
    now = datetime.now().astimezone(SHANGHAI_TZ)
    win_start = now - timedelta(minutes=3)
    win_end = now - timedelta(minutes=1)
    
    mgr._windows_history = [_make_window(win_start, win_end, app="com.openai.codex")]
    mgr._prune_and_publish(now=now)

    windows = load_input_context_windows(tmp_path)
    assert len(windows) == 1
    assert windows[0]["target_app"] == "com.openai.codex"

    row = {
        "source_id": "src_1",
        "text": "查一下天气",
        "source_label": "self",
        "absolute_started_at": (win_start + timedelta(seconds=10)).isoformat(),
        "absolute_ended_at": (win_start + timedelta(seconds=15)).isoformat(),
    }
    stats = attach_watch_input_context([row], windows)
    assert stats["resolved"] == 1
    assert row["input_context"]["status"] == "resolved"
    assert row["input_context"]["target_bundle"] == "com.openai.codex"
