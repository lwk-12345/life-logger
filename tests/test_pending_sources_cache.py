"""待审核投影的短时缓存 + single-flight（2026-10-24 性能修复）。

现场：`POST /api/pending-sources/status` 成功后，刷新 `GET /api/pending-sources`
单次 5.2~8.7s（高并发 26.7s、页面并发刷新链 91s），UI 一直停在「正在记录处理状态……」。
根因：每次 GET 都重读 389 个产物 JSON + 1.3MB 审核台账，并做 7000+ 次路径运算。

本文件锁住四条语义：
1. 第二次读命中缓存（毫秒级）且内容与首次完全一致；
2. **写后立刻可见**：写入审核状态后，下一次读必须带上新状态（不能等 TTL）；
3. 并发同 key 只真算一次（single-flight），其余等待复用；
4. 单文件 JSON 缓存按 (mtime, size) 失效，文件改了绝不返回旧内容。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from life_logger import pending_review as pr


def _write_artifact(root: Path, session: str = "S1") -> Path:
    path = root / "watch-sessions" / "watch-demo" / session / "events.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([], ensure_ascii=False), encoding="utf-8")
    ctx = path.parent / "formal-asr" / "live-context" / "context-extraction.json"
    ctx.parent.mkdir(parents=True, exist_ok=True)
    ctx.write_text(json.dumps({
        "pending_sources": [{
            "source_id": "src_1",
            "quote": "明天早上七点提醒我",
            "reason": "no_event_but_instruction",
            "segment_id": "watch_000001_000001",
        }]
    }, ensure_ascii=False), encoding="utf-8")
    return ctx


def test_cached_collect_is_identical_and_fast(tmp_path):
    _write_artifact(tmp_path)
    pr.invalidate_pending_cache()
    cold_start = time.monotonic()
    first = pr.collect_pending_sources_cached(tmp_path, include_collapsed=True, include_resolved=True)
    cold = time.monotonic() - cold_start

    hot_start = time.monotonic()
    second = pr.collect_pending_sources_cached(tmp_path, include_collapsed=True, include_resolved=True)
    hot = time.monotonic() - hot_start

    assert first == second, "缓存命中必须返回完全一致的内容"
    assert hot < max(0.05, cold / 5), f"命中应显著更快（冷 {cold:.3f}s / 热 {hot:.3f}s）"


def test_write_invalidates_cache_immediately(tmp_path):
    _write_artifact(tmp_path)
    pr.invalidate_pending_cache()
    def handled_count(payload: dict) -> int:
        counts = (payload.get("review_states") or {}).get("ledger_counts") or {}
        return int(counts.get("handled") or 0)

    before = pr.collect_pending_sources_cached(tmp_path, include_collapsed=True, include_resolved=True)
    assert handled_count(before) == 0

    pr.set_pending_review_state(tmp_path, source_id="src_1", status="handled", note="用户标记已处理")

    after = pr.collect_pending_sources_cached(tmp_path, include_collapsed=True, include_resolved=True)
    assert handled_count(after) == 1, "写后第一次读必须看到新状态（不等 TTL）"
    # 写后计数立即从 0 变 1 —— 若缓存没失效，这里仍会读到 0
    assert handled_count(before) == 0 and handled_count(after) == 1
    assert int(((after.get("review_states") or {}).get("total") or 0)) >= 1


def test_concurrent_reads_compute_once(tmp_path, monkeypatch):
    _write_artifact(tmp_path)
    pr.invalidate_pending_cache()
    calls = {"n": 0}
    real = pr.collect_pending_sources

    def counted(*args, **kwargs):
        calls["n"] += 1
        time.sleep(0.3)
        return real(*args, **kwargs)

    monkeypatch.setattr(pr, "collect_pending_sources", counted)
    results = []

    def worker():
        results.append(pr.collect_pending_sources_cached(
            tmp_path, include_collapsed=True, include_resolved=True))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not [t for t in threads if t.is_alive()], "线程未结束 = single-flight 死锁"
    assert calls["n"] == 1, f"同 key 并发应只算一次，实际 {calls['n']} 次"
    assert len(results) == 4 and all(r == results[0] for r in results)


def test_json_cache_invalidates_on_file_change(tmp_path):
    path = tmp_path / "payload.json"
    path.write_text(json.dumps({"v": 1}), encoding="utf-8")
    assert pr._read_json(path) == {"v": 1}
    time.sleep(0.01)
    path.write_text(json.dumps({"v": 2}), encoding="utf-8")
    assert pr._read_json(path) == {"v": 2}, "文件变了必须读到新内容"


def test_failure_is_not_cached(tmp_path, monkeypatch):
    """生产函数抛错时不许缓存「空壳成功」，错误要原样抛给调用方。"""
    _write_artifact(tmp_path)
    pr.invalidate_pending_cache()
    boom = {"n": 0}

    def failing(*args, **kwargs):
        boom["n"] += 1
        if boom["n"] == 1:
            raise pr.PendingReviewError("boom")
        return {"ok": True}

    monkeypatch.setattr(pr, "collect_pending_sources", failing)
    try:
        pr.collect_pending_sources_cached(tmp_path)
    except pr.PendingReviewError:
        pass
    else:
        raise AssertionError("异常必须原样抛出")
    assert pr.collect_pending_sources_cached(tmp_path) == {"ok": True}
