"""时间口径统一（timeutil）回归测试。

存在理由：2026-10-17 排查时，脚本对 UTC 的 ``time.absolute`` 直接截 ``[11:19]``，
把北京时间下午 14:56 的烧鸭饭读成早上 06:56，进而编出「通宵后吃早餐」的假叙事。
本测试锁死两件事：
  1. 时间解析必须认偏移，且能统一换算到 Asia/Shanghai；
  2. 落盘事件 / 会话清单必须自带本地可读字段，读取方不需要再心算。
"""

from __future__ import annotations

from datetime import datetime, timezone

from life_logger.timeutil import (
    LOCAL_TZ,
    annotate_local_time,
    annotate_segment_local_times,
    local_clock,
    local_date,
    local_iso,
    parse_ts,
    to_local,
    utc_iso,
)


def test_utc_timestamp_is_converted_to_beijing_time() -> None:
    # 这是 10-17 事故里的真实值：manifest.start + 797.011s
    assert local_clock("2026-10-17T06:56:32.132000+00:00") == "14:56"
    assert local_iso("2026-10-17T06:56:32.132000+00:00") == "2026-10-17T14:56:32.132000+08:00"


def test_naive_and_offset_aware_inputs_agree() -> None:
    assert local_iso("2026-10-17T14:56:32") == "2026-10-17T14:56:32+08:00"
    assert local_iso("2026-10-17T14:56:32+08:00") == "2026-10-17T14:56:32+08:00"


def test_utc_iso_normalizes_back_to_utc() -> None:
    assert utc_iso("2026-10-17T14:56:32+08:00") == "2026-10-17T06:56:32+00:00"


def test_local_date_handles_midnight_crossover() -> None:
    """UTC 日期 ≠ 北京日期：凌晨 00:30 在 UTC 里属于前一天。

    任何按 ``ts[:10]`` 归档日期的写法都会把凌晨事件记到昨天。
    """
    assert local_date("2026-10-16T16:30:00+00:00") == "2026-10-17"
    assert "2026-10-16T16:30:00+00:00"[:10] == "2026-10-16"


def test_parse_ts_accepts_epoch_and_z_suffix() -> None:
    assert parse_ts("2026-10-17T06:56:32Z") == datetime(
        2026, 10, 17, 6, 56, 32, tzinfo=timezone.utc
    )
    parsed = parse_ts(1760696192)
    assert parsed is not None and parsed.tzinfo is not None
    assert to_local(1760696192) is not None


def test_parse_ts_returns_none_on_garbage_instead_of_raising() -> None:
    for bad in (None, "", "not-a-time", [], True):
        assert parse_ts(bad) is None
    assert local_clock(None) is None
    assert local_date("") is None


def test_annotate_local_time_adds_readable_fields_without_touching_utc() -> None:
    event = {
        "title": "购买烧鸭饭 (¥12.00)",
        "time": {"absolute": "2026-10-17T06:56:32.132000+00:00", "precision": "exact"},
        "evidence": {"reported_at": "2026-10-17T06:56:32.132000+00:00", "quote": "要一份烧鸭饭"},
    }
    original_utc = event["time"]["absolute"]
    annotate_local_time(event)

    # UTC 原值必须原样保留（下游读 UTC 的逻辑不受影响）
    assert event["time"]["absolute"] == original_utc
    assert event["time"]["local"] == "2026-10-17T14:56:32.132000+08:00"
    assert event["time"]["clock"] == "14:56"
    assert event["time"]["date"] == "2026-10-17"
    assert event["time"]["timezone"] == "Asia/Shanghai"
    assert event["evidence"]["reported_at_local"] == "2026-10-17T14:56:32.132000+08:00"


def test_annotate_local_time_is_idempotent() -> None:
    event = {"time": {"absolute": "2026-10-17T06:56:32+00:00"}}
    annotate_local_time(event)
    first = dict(event["time"])
    annotate_local_time(event)
    assert event["time"] == first


def test_annotate_segment_local_times_makes_manifest_self_describing() -> None:
    manifest = {
        "started_at": "2026-10-17T06:43:15.121000+00:00",
        "segments": [
            {"sequence": 0, "start_offset_ms": 0, "duration_ms": 60097},
            # 事故里的烧鸭饭就在这一段：797.011s ≈ 13 分 17 秒
            {"sequence": 13, "start_offset_ms": 797011, "duration_ms": 60000},
        ],
    }
    annotate_segment_local_times(manifest)

    assert manifest["started_at_local"] == "2026-10-17T14:43:15.121000+08:00"
    assert manifest["segments"][0]["started_at_local"] == "2026-10-17T14:43:15.121000+08:00"
    assert manifest["segments"][1]["started_at_local"].startswith("2026-10-17T14:56:")
    assert manifest["timezone"] == "Asia/Shanghai"


def test_local_tz_is_asia_shanghai() -> None:
    assert str(LOCAL_TZ) == "Asia/Shanghai"
