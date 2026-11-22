"""Q635 · 历史回溯开关（默认关闭）。

要求：
  1. 开关关闭 ⇒ 行为与旧版完全一致（扫描 since 仍 = activation，**不发起全量扫描**）；
  2. 开关打开 ⇒ 只捞「已有 timeline 文件的天」，且受 max_rows / max_files 约束；
  3. 反例：无 timeline 文件的天，即使开关打开也**绝不**被选中。
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from life_logger.activity_timeline_runtime import (  # noqa: E402
    ActivityTimelineRuntime,
    BEIJING_TZ,
)

ACTIVATION = "2026-10-26T20:15:40+08:00"


def _write_state(root: Path, idx: int, started_at: str, recording: str = "rec") -> dict:
    d = root / "watch-sessions" / "w" / "r" / "formal-asr" / "segments" / f"{idx:05d}"
    d.mkdir(parents=True, exist_ok=True)
    row = {
        "segment_id": f"seg_{idx:05d}",
        "text": f"第 {idx} 条",
        "absolute_started_at": started_at,
        "recording_id": recording,
        "start_offset_seconds": float(idx),
    }
    (d / "state.json").write_text(json.dumps({"status": "completed", "rows": [row]}), encoding="utf-8")
    return row


def _runtime(root: Path, *, backfill: bool | None = None, max_rows: int | None = None,
             max_files: int | None = None) -> ActivityTimelineRuntime:
    cfg = {
        "activity_timeline": {
            "enabled": True,
            "route": "jev",
            "cadence_minutes": 10,
            "context_minutes": 30,
            "enabled_at": ACTIVATION,
        }
    }
    if backfill is not None:
        cfg["activity_timeline"]["historical_backfill_enabled"] = backfill
    if max_rows is not None:
        cfg["activity_timeline"]["historical_backfill_max_rows"] = max_rows
    if max_files is not None:
        cfg["activity_timeline"]["historical_backfill_max_files"] = max_files
    path = root / "local-config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return ActivityTimelineRuntime(root, path)


def _timeline_file(root: Path, day: str, processed: list[str] | None = None) -> None:
    d = root / "activity-timeline" / "jev"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{day}.json").write_text(
        json.dumps({
            "schema": "activity-timeline-v1", "date": day, "route": "jev",
            "status": "ready", "activities": [], "errors": [],
            "coverage": {"through": None, "source_count": len(processed or [])},
            "metadata": {"processed_source_ids": processed or []},
            "window_history": [],
        }),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 1) 开关关闭：不发起全量扫描，since 仍是 activation
# ---------------------------------------------------------------------------
def test_disabled_keeps_legacy_since_and_never_scans_full(tmp_path):
    calls: list[dict] = []

    def fake_scan(self, since_mtime=None, max_files=150):
        calls.append({"since": since_mtime, "max_files": max_files})
        return []

    tm = ActivityTimelineRuntime
    orig = tm.scan_recent_asr_records
    tm.scan_recent_asr_records = fake_scan
    try:
        rt = _runtime(tmp_path, backfill=False)
        rt.tick()
        assert calls, "tick 没有调用扫描"
        assert all(c["since"] is not None for c in calls), f"关闭时出现了 since=None 的全量扫描: {calls}"
        assert calls[0]["max_files"] == 150, f"关闭时 max_files 被改动: {calls[0]}"
        activation_ts = datetime.datetime.fromisoformat(ACTIVATION).timestamp()
        assert abs(calls[0]["since"] - activation_ts) < 1, f"since 不等于 activation: {calls[0]['since']}"
    finally:
        tm.scan_recent_asr_records = orig


def _backdate(root: Path, mtime: float) -> None:
    """把落盘文件的 mtime 调到 activation 之前（模拟"enable 之前就录好的文件"）。"""
    import os
    for p in (root / "watch-sessions").rglob("state.json"):
        os.utime(p, (mtime, mtime))


def test_disabled_does_not_touch_old_rows(tmp_path):
    """旧文件（mtime 早于 activation）在关闭时不能被扫到。"""
    _write_state(tmp_path, 1, "2026-10-26T03:38:39+08:00")
    _backdate(tmp_path, datetime.datetime.fromisoformat(ACTIVATION).timestamp() - 3600)
    _timeline_file(tmp_path, "2026-10-26")
    rt = _runtime(tmp_path, backfill=False)
    out = rt._bounded_step()
    assert out.get("status") in {"idle", "advanced_empty", "cooldown"}, out


def test_enabled_catches_old_files_that_disabled_misses(tmp_path):
    """同一个旧文件：关闭时扫不到，打开后能被历史回溯抓到。"""
    _write_state(tmp_path, 1, "2026-10-26T03:38:39+08:00")
    _backdate(tmp_path, datetime.datetime.fromisoformat(ACTIVATION).timestamp() - 3600)
    _timeline_file(tmp_path, "2026-10-26")

    rt_off = _runtime(tmp_path, backfill=False)
    seen_off = rt_off.scan_recent_asr_records(
        since_mtime=datetime.datetime.fromisoformat(ACTIVATION).timestamp())
    assert seen_off == [], f"关闭态竟然扫到了旧文件: {seen_off}"

    rt_on = _runtime(tmp_path, backfill=True, max_rows=10)
    picked = rt_on._collect_historical_backfill(
        datetime.datetime(2026, 11, 2, 13, 40, tzinfo=BEIJING_TZ), "jev", {},
        rt_on.get_timeline_settings())
    assert picked and picked.get("2026-10-26"), picked


# ---------------------------------------------------------------------------
# 2) 开关打开：只捞有 timeline 的天，且受 max_rows 约束
# ---------------------------------------------------------------------------
def test_enabled_only_picks_days_with_timeline(tmp_path):
    rows = []
    for i in range(5):
        rows.append({"source_id": f"src_old_{i}", "segment_id": f"seg_old_{i}",
                     "timestamp": f"2026-10-26T0{i}:00:00+08:00", "text": f"旧 {i}",
                     "recording_id": "rec", "row_id": i})
    rows.append({"source_id": "src_notracked", "segment_id": "seg_x",
                 "timestamp": "2026-10-20T10:00:00+08:00", "text": "从未跟踪的天",
                 "recording_id": "rec", "row_id": 99})
    _timeline_file(tmp_path, "2026-10-26")           # 只有这天有 timeline

    rt = _runtime(tmp_path, backfill=True, max_rows=10)
    days: dict = {}
    cutoff = datetime.datetime(2026, 11, 2, 13, 40, tzinfo=BEIJING_TZ)
    rt.scan_recent_asr_records = lambda since_mtime=None, max_files=150: rows  # type: ignore
    picked = rt._collect_historical_backfill(cutoff, "jev", days, rt.get_timeline_settings())

    assert set(picked) == {"2026-10-26"}, f"捞了不该捞的天: {sorted(picked)}"
    assert len(picked["2026-10-26"]) == 5


def test_enabled_respects_max_rows(tmp_path):
    rows = [{"source_id": f"src_{i}", "segment_id": f"seg_{i}",
             "timestamp": f"2026-10-26T0{i}:30:00+08:00", "text": f"x{i}",
             "recording_id": "rec", "row_id": i} for i in range(10)]
    _timeline_file(tmp_path, "2026-10-26")
    rt = _runtime(tmp_path, backfill=True, max_rows=3)
    rt.scan_recent_asr_records = lambda since_mtime=None, max_files=150: rows  # type: ignore
    picked = rt._collect_historical_backfill(
        datetime.datetime(2026, 11, 2, 13, 40, tzinfo=BEIJING_TZ), "jev", {},
        rt.get_timeline_settings())
    assert sum(len(v) for v in picked.values()) == 3, picked


# ---------------------------------------------------------------------------
# 3) 反例：无 timeline 的天，打开开关也不碰
# ---------------------------------------------------------------------------
def test_day_without_timeline_is_never_picked(tmp_path):
    rows = [{"source_id": "src_a", "segment_id": "seg_a",
             "timestamp": "2026-10-15T10:00:00+08:00", "text": "无 timeline 的天",
             "recording_id": "rec", "row_id": 1}]
    _timeline_file(tmp_path, "2026-10-26")           # 只有别的天有 timeline
    rt = _runtime(tmp_path, backfill=True, max_rows=100)
    rt.scan_recent_asr_records = lambda since_mtime=None, max_files=150: rows  # type: ignore
    picked = rt._collect_historical_backfill(
        datetime.datetime(2026, 11, 2, 13, 40, tzinfo=BEIJING_TZ), "jev", {},
        rt.get_timeline_settings())
    assert picked == {}, f"无 timeline 的天被选中: {picked}"


def test_already_processed_rows_are_skipped(tmp_path):
    rows = [{"source_id": "src_done", "segment_id": "seg_d",
             "timestamp": "2026-10-26T09:00:00+08:00", "text": "已处理",
             "recording_id": "rec", "row_id": 1}]
    _timeline_file(tmp_path, "2026-10-26", processed=["src_done"])
    rt = _runtime(tmp_path, backfill=True, max_rows=10)
    rt.scan_recent_asr_records = lambda since_mtime=None, max_files=150: rows  # type: ignore
    picked = rt._collect_historical_backfill(
        datetime.datetime(2026, 11, 2, 13, 40, tzinfo=BEIJING_TZ), "jev", {},
        rt.get_timeline_settings())
    assert picked == {}, picked


# ---------------------------------------------------------------------------
# 4) 配置默认值
# ---------------------------------------------------------------------------
def test_defaults_are_off(tmp_path):
    rt = _runtime(tmp_path)
    s = rt.get_timeline_settings()
    assert s["historical_backfill_enabled"] is False
    assert s["historical_backfill_max_rows"] == 24
    assert s["historical_backfill_max_files"] >= 150


def test_invalid_config_values_fall_back(tmp_path):
    rt = _runtime(tmp_path, backfill=True, max_rows=999999, max_files=1)
    s = rt.get_timeline_settings()
    assert s["historical_backfill_max_rows"] == 24          # 越界回落默认
    assert s["historical_backfill_max_files"] == 20_000     # 越界回落默认
