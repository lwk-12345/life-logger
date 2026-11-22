"""Q591 · 扫描游标持久化 / mtime 水位：覆盖保证 + 重启续扫 + 持续新增 + 旧实现对照。

背景（生产事故）：`scan_recent_asr_records()` 原来用**内存索引游标**轮转：
  * 进程重启 ⇒ `self._scan_cursor = 0`，永远从剩余列表开头扫；
  * 列表每轮按 mtime 重排 ⇒ 同一索引指向的文件一直在变；
  * 1809 个候选 / 每轮 140 ⇒ 要 13 轮才转一圈，而新文件产生更快。
结果：一批 ASR 文件**永远轮不到**，表现为"覆盖缺口"。

本文件验证改后的 mtime+path 水位游标：
  1. 200 个文件在 5 轮内全部至少被扫到一次；
  2. 进程"重启"（新实例）后接着往下扫，不重复、不漏；
  3. 每轮新增 5 个文件，跑 20 轮，老文件仍会被扫到；
  4. 对照组：用改前的索引游标实现跑同样场景，证明它会漏。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from life_logger.activity_timeline_runtime import ActivityTimelineRuntime  # noqa: E402


BASE_MTIME = 1_700_000_000.0


def _make_state_file(root: Path, index: int, *, mtime: float) -> Path:
    """造一个 completed 的 formal-asr state.json，segment_id 唯一，mtime 可控。"""
    directory = root / "watch-sessions" / "w" / "r" / "formal-asr" / "segments" / f"{index:05d}"
    directory.mkdir(parents=True, exist_ok=True)
    state = directory / "state.json"
    state.write_text(
        json.dumps(
            {
                "status": "completed",
                "rows": [
                    {
                        "segment_id": f"seg_{index:05d}",
                        "text": f"第 {index} 条",
                        "absolute_started_at": "2026-11-01T10:00:00+08:00",
                        "recording_id": "rec_test",
                        "start_offset_seconds": float(index),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    os.utime(state, (mtime, mtime))
    return state


def _new_runtime(root: Path) -> ActivityTimelineRuntime:
    config = root / "local-config.json"
    if not config.exists():
        config.write_text(json.dumps({"activity_timeline": {"enabled": True}}), encoding="utf-8")
    return ActivityTimelineRuntime(root, config)


def _seg_ids(records) -> set[str]:
    return {str(r["segment_id"]) for r in records}


# ---------------------------------------------------------------------------
# 1) 200 个文件、5 轮内全部覆盖
# ---------------------------------------------------------------------------
def test_two_hundred_files_all_visited_within_five_rounds(tmp_path):
    for i in range(200):
        # mtime 递增：i=0 最老，i=199 最新
        _make_state_file(tmp_path, i, mtime=BASE_MTIME + i)

    runtime = _new_runtime(tmp_path)
    budget = 50  # newest=10, 每轮水位推进 40
    visited: set[str] = set()
    per_round: list[int] = []
    for _ in range(5):
        before = len(visited)
        visited |= _seg_ids(runtime.scan_recent_asr_records(max_files=budget))
        per_round.append(len(visited) - before)

    assert len(visited) == 200, f"5 轮后只覆盖 {len(visited)}/200"
    assert all(n > 0 for n in per_round), f"存在空轮次: {per_round}"

    # 游标必须落盘，且能读出水位
    cursor_file = tmp_path / "activity-timeline" / ".scan-cursor.json"
    assert cursor_file.is_file(), "扫描游标没有持久化"
    data = json.loads(cursor_file.read_text(encoding="utf-8"))
    assert data["schema"] == "activity-scan-cursor-v1"
    assert data["total_candidates"] == 200


# ---------------------------------------------------------------------------
# 2) 重启续扫：新实例接着往下，不重复（水位以下）、不漏
# ---------------------------------------------------------------------------
def test_restart_continues_cursor_without_repeats_or_gaps(tmp_path):
    total, budget = 120, 40  # newest=10, quota=30
    for i in range(total):
        _make_state_file(tmp_path, i, mtime=BASE_MTIME + i)

    first = _new_runtime(tmp_path)
    round1 = _seg_ids(first.scan_recent_asr_records(max_files=budget))
    cursor_before = json.loads(
        (tmp_path / "activity-timeline" / ".scan-cursor.json").read_text(encoding="utf-8")
    )

    # 模拟进程重启：全新实例，只共享磁盘上的游标
    restarted = _new_runtime(tmp_path)
    visited: set[str] = set(round1)
    rounds = 1
    while rounds < 10:
        got = _seg_ids(restarted.scan_recent_asr_records(max_files=budget))
        rounds += 1
        visited |= got
        if len(visited) >= total:
            break

    assert len(visited) == total, f"重启后只覆盖 {len(visited)}/{total}"

    # 重启后的第一轮必须从水位继续（不能把第 1 轮那 30 个再扫一遍）
    restarted2 = _new_runtime(tmp_path)
    # 先把水位写回"第 1 轮结束"的状态，再验证第 2 轮内容
    (tmp_path / "activity-timeline" / ".scan-cursor.json").write_text(
        json.dumps(cursor_before), encoding="utf-8"
    )
    second_round = _seg_ids(restarted2.scan_recent_asr_records(max_files=budget))
    overlap = (round1 - _newest_window(round1, total)) & second_round
    assert not overlap, f"重启后重复扫了水位以下的文件: {sorted(overlap)[:5]}"


def _newest_window(scanned: set[str], total: int) -> set[str]:
    """最新 10 个（每轮都扫，属设计内重复），从断言里排除。"""
    return {f"seg_{i:05d}" for i in range(total - 10, total)}


# ---------------------------------------------------------------------------
# 3) 持续新增：每轮 +5 个文件，跑 20 轮，老文件仍会被扫到
# ---------------------------------------------------------------------------
def test_old_files_still_reached_while_new_files_keep_coming(tmp_path):
    old_count, budget, rounds = 150, 40, 20
    for i in range(old_count):
        _make_state_file(tmp_path, i, mtime=BASE_MTIME + i)

    runtime = _new_runtime(tmp_path)
    oldest_before = {f"seg_{i:05d}" for i in range(0, 30)}  # 最老的 30 个
    visited: set[str] = set()
    next_index = old_count
    for r in range(rounds):
        for _ in range(5):  # 每轮新增 5 个（mtime 更新）
            _make_state_file(tmp_path, next_index, mtime=BASE_MTIME + 1000 + next_index)
            next_index += 1
        visited |= _seg_ids(runtime.scan_recent_asr_records(max_files=budget))

    missing = oldest_before - visited
    assert not missing, f"持续新增下，最老的文件仍未被扫到: {sorted(missing)[:5]}"
    assert len(visited) >= old_count, f"只覆盖了 {len(visited)} 个（老文件 {old_count}）"


# ---------------------------------------------------------------------------
# 4) 对照组：旧实现（内存索引 + 不持久化）在同样场景下会漏
# ---------------------------------------------------------------------------
def _legacy_scan(files: list[tuple[float, Path]], cursor: int, budget: int):
    """改前的实现原样复刻：内存索引游标 + 每轮按 mtime 重排 + 固定最新 10 个。"""
    ordered = sorted(files, key=lambda x: x[0], reverse=True)
    newest_count = min(10, max(1, budget // 4))
    selected = ordered[:newest_count]
    remaining = ordered[newest_count:]
    if remaining:
        start = cursor % len(remaining)
        rotated = remaining[start:] + remaining[:start]
        selected += rotated[: budget - newest_count]
        cursor = (start + budget - newest_count) % len(remaining)
    return [p for _, p in selected], cursor


def _seg_of(path: Path) -> str:
    return f"seg_{int(path.parent.name):05d}"


def test_legacy_index_cursor_still_misses_files(tmp_path):
    """把 bug 形态固化：同样 200 个文件、同样 budget，旧实现"重启即归零"⇒ 中间那批永远扫不到。"""
    files: list[tuple[float, Path]] = []
    for i in range(200):
        path = _make_state_file(tmp_path, i, mtime=BASE_MTIME + i)
        files.append((BASE_MTIME + i, path))

    budget = 50
    visited: set[str] = set()
    cursor = 0
    for _ in range(5):
        # 每次都"重启"：旧实现的游标是内存变量，进程重启即归零
        cursor = 0
        picked, cursor = _legacy_scan(files, cursor, budget)
        visited |= {_seg_of(p) for p in picked}

    assert len(visited) < 200, (
        f"对照组的旧实现居然覆盖了全部 {len(visited)} 个文件——场景没复现出 bug"
    )
    # 记录实际覆盖数，写进报告用
    print(f"[legacy] 5 轮（每轮重启）只覆盖 {len(visited)}/200")


def test_recent_files_are_scanned_every_round(tmp_path, monkeypatch):
    """最近写入的转写不排队轮转：第 11 新的文件也要在当轮被看到。"""
    runtime = ActivityTimelineRuntime.__new__(ActivityTimelineRuntime)
    runtime._scan_cursor = None
    runtime._scan_sweeps = 0
    now = 2_000_000_000.0
    monkeypatch.setattr("life_logger.activity_timeline_runtime.time.time", lambda: now)
    recent = [(now - minute * 60, Path(f"recent/{minute:03d}")) for minute in range(30)]
    old = [(now - 30 * 86400 - i, Path(f"old/{i:04d}")) for i in range(100)]

    selected, _ = runtime._select_scan_files(recent + old, budget=20, newest_count=5)

    paths = {path for _, path in selected}
    assert {path for _, path in recent} <= paths
