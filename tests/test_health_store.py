"""Unit tests for Health Data Store."""

from __future__ import annotations

from pathlib import Path
import pytest
from life_logger.health_store import (
    HealthDataStore,
    HeartRatePoint,
    SleepSessionRecord,
)


def test_health_store_heart_rate(tmp_path: Path) -> None:
    store = HealthDataStore(tmp_path)
    points = [
        HeartRatePoint(timestamp="2026-10-11T10:00:00", bpm=72.0),
        HeartRatePoint(timestamp="2026-10-11T10:05:00", bpm=110.0),
        HeartRatePoint(timestamp="2026-10-11T10:10:00", bpm=78.0),
    ]
    store.record_heart_rates(points)

    latest = store.get_latest_heart_rate()
    assert latest is not None
    assert latest["bpm"] == 78.0

    in_range = store.get_heart_rate_for_range("2026-10-11T09:59:00", "2026-10-11T10:06:00")
    assert len(in_range) == 2


def test_health_store_daily_summary(tmp_path: Path) -> None:
    store = HealthDataStore(tmp_path)
    store.record_heart_rates([
        HeartRatePoint(timestamp="2026-10-11T08:00:00", bpm=70.0),
        HeartRatePoint(timestamp="2026-10-11T12:00:00", bpm=80.0),
        HeartRatePoint(timestamp="2026-10-11T18:00:00", bpm=115.0),
    ])
    store.record_steps("2026-10-11", 8500)
    store.record_sleep(
        SleepSessionRecord(
            date="2026-10-11",
            start_time="2026-10-11T00:30:00",
            end_time="2026-10-11T07:30:00",
            total_minutes=420,
            deep_minutes=95,
            core_minutes=240,
            rem_minutes=85,
            awake_minutes=15,
        )
    )

    summary = store.get_daily_summary("2026-10-11")
    assert summary.avg_heart_rate == pytest.approx(88.3, 0.2)
    assert summary.min_heart_rate == 70.0
    assert summary.max_heart_rate == 115.0
    assert summary.total_steps == 8500
    assert summary.sleep_minutes == 420
    assert summary.deep_sleep_minutes == 95
    assert summary.stress_spikes_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# [2026-11-07 V68] 手表上传「当日累计步数 + 值所属自然日」→ 同一天只增不减。
# 老手表（V67 及以前）走的 record_steps() 保持旧行为（整条覆盖），不能变。
# ─────────────────────────────────────────────────────────────────────────────
def test_record_daily_steps_keeps_the_larger_value_for_the_same_day(tmp_path: Path) -> None:
    store = HealthDataStore(tmp_path)
    first = store.record_daily_steps("2026-11-04", 1212, sampled_at="2026-11-04T07:30:00+08:00")
    assert first["accepted"] is True and first["steps"] == 1212

    bigger = store.record_daily_steps("2026-11-04", 7666, sampled_at="2026-11-04T23:59:00+08:00")
    assert bigger["accepted"] is True
    assert bigger["previous_steps"] == 1212
    assert store.get_daily_summary("2026-11-04").total_steps == 7666


def test_record_daily_steps_ignores_a_smaller_late_value(tmp_path: Path) -> None:
    """迟到的小值（延迟上传的早段 / 手表重启后从 0 重数）不能把大值盖掉。"""
    store = HealthDataStore(tmp_path)
    store.record_daily_steps("2026-11-04", 7666, sampled_at="2026-11-04T23:59:00+08:00")
    late = store.record_daily_steps("2026-11-04", 1212, sampled_at="2026-11-05T12:06:55+08:00")
    assert late["accepted"] is False
    assert late["previous_steps"] == 7666
    assert store.get_daily_summary("2026-11-04").total_steps == 7666, "小值不得覆盖大值"


def test_record_daily_steps_keeps_days_separate(tmp_path: Path) -> None:
    """跨日不串：11-02 的 8328 不能出现在 11-03（诊断里真实发生过）。"""
    store = HealthDataStore(tmp_path)
    store.record_daily_steps("2026-11-02", 8328, sampled_at="2026-11-02T23:12:00+08:00")
    store.record_daily_steps("2026-11-03", 1088, sampled_at="2026-11-03T04:20:00+08:00")
    assert store.get_daily_summary("2026-11-02").total_steps == 8328
    assert store.get_daily_summary("2026-11-03").total_steps == 1088


def test_record_steps_old_field_keeps_last_write_wins(tmp_path: Path) -> None:
    """老字段（V67 及以前）行为不变：record_steps 仍是整条覆盖。这里锁住它，防止被顺手改掉。"""
    store = HealthDataStore(tmp_path)
    store.record_steps("2026-11-04", 7666)
    store.record_steps("2026-11-04", 1212)
    assert store.get_daily_summary("2026-11-04").total_steps == 1212


def test_record_daily_steps_rejects_bad_input(tmp_path: Path) -> None:
    store = HealthDataStore(tmp_path)
    for bad_date, bad_steps in (("", 10), ("2026-11-04", -1)):
        try:
            store.record_daily_steps(bad_date, bad_steps)
        except ValueError:
            continue
        raise AssertionError(f"应当拒绝 date={bad_date!r} steps={bad_steps}")
