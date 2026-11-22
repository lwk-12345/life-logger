"""跨进程读缓存失效协议（2026-10-24 P0）。

背景：同进程写后失效早已 PASS，但 `processing_worker`、Watch ASR 子进程、
CLI 都是**其它进程**，写完只能等扫描缓存 10s TTL + 读快照 8s TTL。
本文件锁住新的磁盘代际协议：

* 写进程：`bump_scan_epoch(root)` 原子写 `<root>/.event-cache-epoch`；
* 读进程：`build_event_view(use_cache=True)` 扫描前 `sync_scan_epoch(root)`，
  token 变了就立刻清缓存并 +1 代数（在途扫描结果不写回）；
* 路径两侧统一 `realpath`，符号链接 root 也能失效；
* 旧调用 `invalidate_scan_cache(root)` 语义不变，且顺带通知其它进程。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import life_logger.event_view as event_view
from life_logger.event_view import (
    build_event_view,
    bump_scan_epoch,
    invalidate_scan_cache,
    read_scan_epoch,
    sync_scan_epoch,
)

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
DATE = "2026-10-22"


def _write_day(root: Path, titles: list[str]) -> Path:
    directory = root / "jobs" / "job-20260101-000000-x" / "files" / "abc" / "result" / DATE
    directory.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "event_id": f"e{index}",
            "title": title,
            "categories": ["work_communication"],
            "time": {"absolute": f"{DATE}T1{index}:00:00+08:00"},
            "evidence": {"quote": title, "reported_at": f"{DATE}T1{index}:00:00+08:00"},
        }
        for index, title in enumerate(titles)
    ]
    path = directory / "daily_summary.json"
    path.write_text(json.dumps({"timeline": events}, ensure_ascii=False), encoding="utf-8")
    return path


def _titles(root: Path, *, cache: bool = True) -> list[str]:
    payload = build_event_view(
        root, date_from=DATE, date_to=DATE, scope="provisional", view="display", use_cache=cache
    )
    return [str(event.get("title")) for event in payload.get("events", [])]


CHILD = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from life_logger.event_view import bump_scan_epoch

root = Path(sys.argv[2])
titles = json.loads(sys.argv[3])
mode = sys.argv[4]
directory = root / "jobs" / "job-20260101-000000-x" / "files" / "abc" / "result" / "2026-10-22"
directory.mkdir(parents=True, exist_ok=True)
events = [
    {"event_id": f"c{i}", "title": t, "categories": ["work_communication"],
     "time": {"absolute": f"2026-10-22T1{i}:00:00+08:00"},
     "evidence": {"quote": t, "reported_at": f"2026-10-22T1{i}:00:00+08:00"}}
    for i, t in enumerate(titles)
]
(directory / "daily_summary.json").write_text(json.dumps({"timeline": events}, ensure_ascii=False), encoding="utf-8")
token = bump_scan_epoch(root, "child-write") if mode == "bump" else None
print(token or "")
"""


def _spawn_child(root: Path, titles: list[str], *, bump: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(SRC))
    return subprocess.run(
        [sys.executable, "-c", CHILD, str(SRC), str(root), json.dumps(titles, ensure_ascii=False),
         "bump" if bump else "silent"],
        capture_output=True, text=True, timeout=60, env=env,
    )


@pytest.fixture(autouse=True)
def _clean():
    with event_view._SCAN_CACHE_LOCK:
        event_view._SCAN_CACHE.clear()
        event_view._SCAN_INFLIGHT.clear()
    with event_view._JSON_CACHE_LOCK:
        event_view._JSON_CACHE.clear()
    with event_view._EPOCH_LOCK:
        event_view._EPOCH_TOKENS.clear()
    yield
    with event_view._SCAN_CACHE_LOCK:
        event_view._SCAN_CACHE.clear()
        event_view._SCAN_INFLIGHT.clear()
    with event_view._JSON_CACHE_LOCK:
        event_view._JSON_CACHE.clear()
    with event_view._EPOCH_LOCK:
        event_view._EPOCH_TOKENS.clear()


def test_child_process_write_is_visible_immediately_without_sleep(tmp_path):
    """A 进程读 + 缓存；B 进程写事件并 bump；A 下一次立刻看到（不 sleep、不等 TTL）。"""
    _write_day(tmp_path, ["旧事件"])
    assert _titles(tmp_path) == ["旧事件"]

    result = _spawn_child(tmp_path, ["旧事件", "子进程新增"], bump=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip(), "子进程应打印新 token"

    started = time.monotonic()
    titles = _titles(tmp_path)
    elapsed = time.monotonic() - started
    assert titles == ["旧事件", "子进程新增"], titles
    assert elapsed < 5, f"应立即生效，实际 {elapsed:.2f}s"


def test_silent_write_without_bump_is_still_cached_until_ttl(tmp_path):
    """反例：子进程只写文件不 bump → 读进程仍在 TTL 内返回旧值（证明协议是必要的）。"""
    _write_day(tmp_path, ["旧事件"])
    assert _titles(tmp_path) == ["旧事件"]
    result = _spawn_child(tmp_path, ["旧事件", "偷偷写入"], bump=False)
    assert result.returncode == 0, result.stderr
    assert _titles(tmp_path) == ["旧事件"], "没有 bump 就不该立刻可见"


def test_symlink_root_invalidates_in_both_directions(tmp_path):
    real = tmp_path / "real-store"
    real.mkdir()
    link = tmp_path / "link-store"
    link.symlink_to(real, target_is_directory=True)
    _write_day(real, ["旧事件"])

    assert _titles(link) == ["旧事件"]      # 通过链接读 → 缓存键落 realpath
    _write_day(real, ["旧事件", "新增"])

    removed = invalidate_scan_cache(real)   # 真实路径失效
    assert removed >= 1, removed
    assert _titles(link) == ["旧事件", "新增"]

    _write_day(real, ["旧事件", "新增", "再来一条"])
    assert _titles(link) == ["旧事件", "新增"]   # 重新缓存
    removed = invalidate_scan_cache(link)        # 链接路径失效
    assert removed >= 1, removed
    assert _titles(real) == ["旧事件", "新增", "再来一条"]


def test_inflight_scan_result_is_dropped_when_epoch_bumps(tmp_path):
    """扫描进行中别的进程 bump → 这份结果不能写回缓存。"""
    started = threading.Event()

    def slow_producer():
        started.set()
        time.sleep(0.4)
        return {"events": {"x": {"title": "旧快照"}}, "rejected_tombstones": [], "metadata": {"sources": {}}}

    output: dict[str, object] = {}
    worker = threading.Thread(target=lambda: output.update({"v": event_view._cached_scan(
        ("formal", event_view._root_key(tmp_path), DATE, DATE, "display"), slow_producer)}))
    worker.start()
    assert started.wait(5)
    token = bump_scan_epoch(tmp_path, "mid-flight")
    assert token
    worker.join(timeout=10)
    assert output.get("v"), output

    key = ("formal", event_view._root_key(tmp_path), DATE, DATE, "display")
    with event_view._SCAN_CACHE_LOCK:
        assert key not in event_view._SCAN_CACHE, "在途结果被写回了旧快照"


def test_concurrent_writers_do_not_corrupt_epoch_and_reader_sees_new_data(tmp_path):
    """4 个子进程并发写+bump；读进程不报错，且最终能看到写入。"""
    _write_day(tmp_path, ["初始"])
    assert _titles(tmp_path) == ["初始"]

    procs = [
        subprocess.Popen(
            [sys.executable, "-c", CHILD, str(SRC), str(tmp_path),
             json.dumps(["初始", f"写入{i}"], ensure_ascii=False), "bump"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=dict(os.environ, PYTHONPATH=str(SRC)),
        )
        for i in range(4)
    ]
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
        assert out.strip(), "每个写进程都应拿到 token"

    titles = _titles(tmp_path)
    assert "初始" in titles
    assert len(titles) == 2, titles
  
    token = read_scan_epoch(tmp_path)
    assert token and token.strip()


def test_invalidate_returns_count_and_notifies_other_processes(tmp_path):
    _write_day(tmp_path, ["旧事件"])
    assert _titles(tmp_path) == ["旧事件"]
    _write_day(tmp_path, ["旧事件", "新增"])
    removed = invalidate_scan_cache(tmp_path)
    assert removed >= 1, removed
    assert read_scan_epoch(tmp_path), "invalidate 也要写 token 通知其它进程"
    assert _titles(tmp_path) == ["旧事件", "新增"]


def test_sync_reports_change_once_then_idempotent(tmp_path):
    """外部进程写的 token（不经过本进程 helper）→ 第一次检出，第二次静默。"""
    _write_day(tmp_path, ["a"])
    sync_scan_epoch(tmp_path)  # 建立基线（此时无 token）
    (tmp_path / event_view._EPOCH_FILENAME).write_text("foreign-token\n", encoding="utf-8")
    assert sync_scan_epoch(tmp_path) is True
    assert sync_scan_epoch(tmp_path) is False
    # 本进程自己 bump 后不必再"发现"一次（已经清过了）
    assert bump_scan_epoch(tmp_path, "self") is not None
    assert sync_scan_epoch(tmp_path) is False


def test_worker_and_asr_bump_helpers_write_token(tmp_path):
    from life_logger.processing_runtime import ProcessingWorker
    from life_logger.watch_formal_asr import _bump_read_epoch

    _bump_read_epoch(tmp_path, "test-asr")
    assert read_scan_epoch(tmp_path), "ASR 子进程 helper 必须写 token"

    # ProcessingWorker._bump_read_epoch 是未绑定方法，只依赖 self.root
    class _Stub:
        root = tmp_path
    ProcessingWorker._bump_read_epoch(_Stub(), "test-worker")
    assert read_scan_epoch(tmp_path)
