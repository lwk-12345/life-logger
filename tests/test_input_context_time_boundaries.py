"""Tests for input_context time boundary rules:
- Naive vs aware timezone safety without guessing
- Positive duration endpoint touch (0ms contact) is not an overlap
- Partial overlap must NOT bind the whole sentence (unknown / partial_overlap with audit fields)
- Positive duration row requires complete unbroken coverage to resolve
- Point moment follows exact half-open interval [w_start, w_end)
- Counterexamples: 250ms/25s, 5s/10s, full coverage, same-app seamless, cross-app, missing end, endpoint touch
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.input_context import (  # noqa: E402
    record_input_context,
    resolve_input_context,
)

REC_START = "2026-10-26T08:00:00.000+08:00"
REC_END = "2026-10-26T08:00:10.000+08:00"


# ------------------------------------------------------------------------------
# 1. 时区等价与 Naive/Aware 不混比 (Timezone Equivalence & Safety)
# ------------------------------------------------------------------------------
def test_utc_z_and_p08_timezone_equivalence():
    """UTC 'Z' and +08:00 representing the exact same physical instant resolve cleanly."""
    res = resolve_input_context(
        record_start="2026-10-26T00:00:00Z",
        record_end="2026-10-26T00:00:05Z",
        windows=[{
            "started_at": "2026-10-26T08:00:00+08:00",
            "ended_at": "2026-10-26T08:00:05+08:00",
            "target_app": "com.tencent.xinWeChat",
        }],
    )
    assert res["status"] == "resolved"
    assert res["target_bundle"] == "com.tencent.xinWeChat"


def test_naive_record_timestamp_safe_unknown_no_guess():
    """Naive record timestamp (missing timezone offset) does not guess, returns time_zone_unknown."""
    res = resolve_input_context(
        record_start="2026-10-26T08:00:00",  # naive
        record_end="2026-10-26T08:00:05",    # naive
        windows=[{
            "started_at": "2026-10-26T08:00:00+08:00",
            "ended_at": "2026-10-26T08:00:05+08:00",
            "target_app": "com.tencent.xinWeChat",
        }],
    )
    assert res["status"] == "unknown"
    assert res["reason"] == "time_zone_unknown"


def test_naive_window_timestamp_safe_unknown_no_guess():
    """Naive window timestamp (missing timezone offset) does not guess, returns time_zone_unknown."""
    res = resolve_input_context(
        record_start="2026-10-26T08:00:00+08:00",
        record_end="2026-10-26T08:00:05+08:00",
        windows=[{
            "started_at": "2026-10-26T08:00:00",  # naive
            "ended_at": "2026-10-26T08:00:05",    # naive
            "target_app": "com.tencent.xinWeChat",
        }],
    )
    assert res["status"] == "unknown"
    assert res["reason"] == "time_zone_unknown"


def test_record_timestamp_inverted_returns_time_conflict():
    """Record end strictly before start returns time_conflict."""
    res = resolve_input_context(
        record_start="2026-10-26T08:00:10+08:00",
        record_end="2026-10-26T08:00:00+08:00",
        windows=[{
            "started_at": "2026-10-26T08:00:00+08:00",
            "ended_at": "2026-10-26T08:00:10+08:00",
            "target_app": "com.tencent.xinWeChat",
        }],
    )
    assert res["status"] == "unknown"
    assert res["reason"] == "time_conflict"


# ------------------------------------------------------------------------------
# 2. 毫秒边界与端点接触 (0ms Endpoint Touch for Positive Duration Intervals)
# ------------------------------------------------------------------------------
def test_window_ended_exactly_at_record_start_is_no_overlap():
    """Window closed at 08:00:05.000, speech started at 08:00:05.000 (0ms touch) -> no_overlap."""
    res = resolve_input_context(
        record_start="2026-10-26T08:00:05.000+08:00",
        record_end="2026-10-26T08:00:10.000+08:00",
        windows=[{
            "started_at": "2026-10-26T08:00:00.000+08:00",
            "ended_at": "2026-10-26T08:00:05.000+08:00",
            "target_app": "com.tencent.xinWeChat",
        }],
    )
    assert res["status"] == "unknown"
    assert res["reason"] == "no_overlap"


def test_window_started_exactly_at_record_end_is_no_overlap():
    """Record ended at 08:00:05.000, window opened at 08:00:05.000 (0ms touch) -> no_overlap."""
    res = resolve_input_context(
        record_start="2026-10-26T08:00:00.000+08:00",
        record_end="2026-10-26T08:00:05.000+08:00",
        windows=[{
            "started_at": "2026-10-26T08:00:05.000+08:00",
            "ended_at": "2026-10-26T08:00:10.000+08:00",
            "target_app": "com.tencent.xinWeChat",
        }],
    )
    assert res["status"] == "unknown"
    assert res["reason"] == "no_overlap"


def test_adjacent_app_switch_boundary_touch_does_not_poison_active_app():
    """App A closes at 08:00:05.000, App B covers 08:00:05.000-08:00:20.000.
    Speech is 08:00:05.000-08:00:10.000.
    App A (0ms touch) does not poison App B into multiple_targets.
    """
    res = resolve_input_context(
        record_start="2026-10-26T08:00:05.000+08:00",
        record_end="2026-10-26T08:00:10.000+08:00",
        windows=[
            {
                "started_at": "2026-10-26T08:00:00.000+08:00",
                "ended_at": "2026-10-26T08:00:05.000+08:00",
                "target_app": "com.apple.dt.Xcode",
            },
            {
                "started_at": "2026-10-26T08:00:05.000+08:00",
                "ended_at": "2026-10-26T08:00:20.000+08:00",
                "target_app": "com.tencent.xinWeChat",
            },
        ],
    )
    assert res["status"] == "resolved"
    assert res["target_bundle"] == "com.tencent.xinWeChat"


# ------------------------------------------------------------------------------
# 3. 部分重叠不得冒认整句 (Partial Overlap Must NOT Bind Whole Row)
# ------------------------------------------------------------------------------
def test_250ms_sliver_overlap_of_25s_utterance_is_partial_overlap():
    """Case 250ms / 25s: 250ms overlap of 25.0s speech must be unknown / partial_overlap, not resolved."""
    res = resolve_input_context(
        record_start="2026-10-26T08:00:00.000+08:00",
        record_end="2026-10-26T08:00:25.000+08:00",
        windows=[{
            "started_at": "2026-10-26T07:59:50.000+08:00",
            "ended_at": "2026-10-26T08:00:00.250+08:00",  # only 0.25s overlap
            "target_app": "com.apple.mobilesafari",
            "provider": {"pid": "1234"},
        }],
    )
    assert res["status"] == "unknown", "250ms/25s partial overlap must NOT be resolved"
    assert res["reason"] == "partial_overlap"
    assert res["target_bundle"] == "com.apple.mobilesafari"
    assert res["overlap_seconds"] == 0.25
    assert res["record_seconds"] == 25.0
    assert res["coverage_ratio"] == 0.01
    assert res["provider"] == {"pid": "1234", "process": None, "source": None}


def test_5s_partial_overlap_of_10s_utterance_is_partial_overlap():
    """Case 5s / 10s: 5s overlap of 10s speech must be unknown / partial_overlap, not resolved."""
    res = resolve_input_context(
        record_start="2026-10-26T08:00:00.000+08:00",
        record_end="2026-10-26T08:00:10.000+08:00",
        windows=[{
            "started_at": "2026-10-26T08:00:05.000+08:00",
            "ended_at": "2026-10-26T08:00:20.000+08:00",  # 5s overlap out of 10s
            "target_app": "com.apple.dt.Xcode",
        }],
    )
    assert res["status"] == "unknown", "5s/10s partial overlap must NOT be resolved"
    assert res["reason"] == "partial_overlap"
    assert res["target_bundle"] == "com.apple.dt.Xcode"
    assert res["overlap_seconds"] == 5.0
    assert res["record_seconds"] == 10.0
    assert res["coverage_ratio"] == 0.5


def test_complete_coverage_resolves():
    """Positive duration row completely covered by window resolves."""
    res = resolve_input_context(
        record_start="2026-10-26T08:00:00.000+08:00",
        record_end="2026-10-26T08:00:10.000+08:00",
        windows=[{
            "started_at": "2026-10-26T07:59:00.000+08:00",
            "ended_at": "2026-10-26T08:00:20.000+08:00",
            "target_app": "com.apple.dt.Xcode",
            "confidence": 0.85,
        }],
    )
    assert res["status"] == "resolved"
    assert res["target_bundle"] == "com.apple.dt.Xcode"
    assert res["confidence"] == 0.85
    assert res["coverage_ratio"] == 1.0


# ------------------------------------------------------------------------------
# 4. 同 Bundle 多窗口完整覆盖 vs 间隙拒绝 (Same-Bundle Multi-Window Merging)
# ------------------------------------------------------------------------------
def test_same_bundle_multi_window_unbroken_coverage_merges():
    """Case 同app无缝: Two windows of the same app seamlessly cover the speech -> merged and resolved."""
    res = resolve_input_context(
        record_start="2026-10-26T08:00:02.000+08:00",
        record_end="2026-10-26T08:00:08.000+08:00",
        windows=[
            {
                "started_at": "2026-10-26T08:00:00.000+08:00",
                "ended_at": "2026-10-26T08:00:05.000+08:00",
                "target_app": "com.apple.Notes",
            },
            {
                "started_at": "2026-10-26T08:00:05.000+08:00",
                "ended_at": "2026-10-26T08:00:10.000+08:00",
                "target_app": "com.apple.Notes",
            },
        ],
    )
    assert res["status"] == "resolved"
    assert res["target_bundle"] == "com.apple.Notes"
    assert res["start"] == "2026-10-26T08:00:00.000+08:00"
    assert res["end"] == "2026-10-26T08:00:10.000+08:00"


def test_same_bundle_multi_window_with_gap_is_partial_overlap():
    """Two windows of same app with a 2s gap in the middle -> rejected as partial_overlap."""
    res = resolve_input_context(
        record_start="2026-10-26T08:00:02.000+08:00",
        record_end="2026-10-26T08:00:08.000+08:00",
        windows=[
            {
                "started_at": "2026-10-26T08:00:00.000+08:00",
                "ended_at": "2026-10-26T08:00:04.000+08:00",
                "target_app": "com.apple.Notes",
            },
            {
                "started_at": "2026-10-26T08:00:06.000+08:00",
                "ended_at": "2026-10-26T08:00:10.000+08:00",
                "target_app": "com.apple.Notes",
            },
        ],
    )
    assert res["status"] == "unknown"
    assert res["reason"] == "partial_overlap"
    assert res["target_bundle"] == "com.apple.Notes"


def test_same_bundle_multi_window_provider_conflict_rejected():
    """Two unbroken windows of same app but conflicting provider PIDs -> multiple_targets."""
    res = resolve_input_context(
        record_start="2026-10-26T08:00:02.000+08:00",
        record_end="2026-10-26T08:00:08.000+08:00",
        windows=[
            {
                "started_at": "2026-10-26T08:00:00.000+08:00",
                "ended_at": "2026-10-26T08:00:05.000+08:00",
                "target_app": "com.apple.Notes",
                "provider": {"pid": "1001"},
            },
            {
                "started_at": "2026-10-26T08:00:05.000+08:00",
                "ended_at": "2026-10-26T08:00:10.000+08:00",
                "target_app": "com.apple.Notes",
                "provider": {"pid": "2002"},
            },
        ],
    )
    assert res["status"] == "unknown"
    assert res["reason"] == "multiple_targets"


def test_cross_app_multiple_targets_rejected():
    """Case 跨app: Overlapping windows of different bundles -> multiple_targets."""
    res = resolve_input_context(
        record_start="2026-10-26T08:00:02.000+08:00",
        record_end="2026-10-26T08:00:08.000+08:00",
        windows=[
            {
                "started_at": "2026-10-26T08:00:00.000+08:00",
                "ended_at": "2026-10-26T08:00:05.000+08:00",
                "target_app": "com.apple.Notes",
            },
            {
                "started_at": "2026-10-26T08:00:05.000+08:00",
                "ended_at": "2026-10-26T08:00:10.000+08:00",
                "target_app": "com.google.Chrome",
            },
        ],
    )
    assert res["status"] == "unknown"
    assert res["reason"] == "multiple_targets"


# ------------------------------------------------------------------------------
# 5. 缺尾与点时刻半开区间 (Missing End & Point Moment Semantics)
# ------------------------------------------------------------------------------
def test_missing_end_is_unknown_missing_end():
    """Case 缺尾: Window missing ended_at returns unknown: missing_end."""
    res = resolve_input_context(
        record_start="2026-10-26T08:00:05.000+08:00",
        record_end="2026-10-26T08:00:10.000+08:00",
        windows=[{
            "started_at": "2026-10-26T08:00:00.000+08:00",
            "ended_at": None,
            "target_app": "com.openai.codex",
        }],
    )
    assert res["status"] == "unknown"
    assert res["reason"] == "missing_end"


def test_point_moment_exact_half_open_interval():
    """Point moment (0 duration) inside [w_start, w_end) resolves; right endpoint touch is no_overlap."""
    # Point at 08:00:05.000 falls in [08:00:00.000, 08:00:10.000)
    inside = resolve_input_context(
        record_start="2026-10-26T08:00:05.000+08:00",
        record_end="2026-10-26T08:00:05.000+08:00",
        windows=[{
            "started_at": "2026-10-26T08:00:00.000+08:00",
            "ended_at": "2026-10-26T08:00:10.000+08:00",
            "target_app": "com.openai.codex",
        }],
    )
    assert inside["status"] == "resolved"
    assert inside["target_bundle"] == "com.openai.codex"

    # Point at 08:00:05.000 touches right endpoint of [08:00:00.000, 08:00:05.000) -> excluded
    right_touch = resolve_input_context(
        record_start="2026-10-26T08:00:05.000+08:00",
        record_end="2026-10-26T08:00:05.000+08:00",
        windows=[{
            "started_at": "2026-10-26T08:00:00.000+08:00",
            "ended_at": "2026-10-26T08:00:05.000+08:00",
            "target_app": "com.openai.codex",
        }],
    )
    assert right_touch["status"] == "unknown"
    assert right_touch["reason"] == "no_overlap"


# ------------------------------------------------------------------------------
# 6. 时钟偏差不凭空校时 (No Invented Clock Skew)
# ------------------------------------------------------------------------------
def test_clock_skew_no_overlap_not_calibrated():
    """When timestamps miss by 30s due to device clock skew, returns no_overlap without guessing."""
    res = resolve_input_context(
        record_start="2026-10-26T08:00:40.000+08:00",
        record_end="2026-10-26T08:00:45.000+08:00",
        windows=[{
            "started_at": "2026-10-26T08:00:00.000+08:00",
            "ended_at": "2026-10-26T08:00:05.000+08:00",
            "target_app": "com.tencent.xinWeChat",
        }],
    )
    assert res["status"] == "unknown"
    assert res["reason"] == "no_overlap"


# ------------------------------------------------------------------------------
# 7. Schema 兼容性与缺信号等价性 (Schema Compatibility & Missing Signal)
# ------------------------------------------------------------------------------
def test_schema_collector_vs_legacy_both_supported():
    """Both collector schema (started_at/target_app) and legacy schema (start/target_bundle) resolve."""
    collector_res = resolve_input_context(
        record_start="2026-10-26T08:00:02+08:00",
        record_end="2026-10-26T08:00:04+08:00",
        windows={"windows": [{
            "started_at": "2026-10-26T08:00:00+08:00",
            "ended_at": "2026-10-26T08:00:10+08:00",
            "target_app": "com.example.collector",
            "complete": True,
            "target_unknown": False,
        }]},
    )
    assert collector_res["status"] == "resolved"
    assert collector_res["target_bundle"] == "com.example.collector"
    assert collector_res["schema_source"] == "collector"

    legacy_res = resolve_input_context(
        record_start="2026-10-26T08:00:02+08:00",
        record_end="2026-10-26T08:00:04+08:00",
        windows=[{
            "start": "2026-10-26T08:00:00+08:00",
            "end": "2026-10-26T08:00:10+08:00",
            "target_bundle": "com.example.legacy",
        }],
    )
    assert legacy_res["status"] == "resolved"
    assert legacy_res["target_bundle"] == "com.example.legacy"
    assert legacy_res["schema_source"] == "legacy"


def test_missing_signal_returns_no_signal_old_path_equivalent():
    """Missing or empty windows cleanly returns no_signal, record_input_context returns None."""
    assert resolve_input_context(record_start=REC_START, record_end=REC_END, windows=None)["reason"] == "no_signal"
    assert resolve_input_context(record_start=REC_START, record_end=REC_END, windows=[])["reason"] == "no_signal"
    assert record_input_context({"timestamp": REC_START}, None) is None


def test_meaning_strictly_declares_not_delivery():
    """All resolved contexts explicitly preserve meaning='input_target_only_not_delivery'."""
    res = resolve_input_context(
        record_start="2026-10-26T08:00:02+08:00",
        record_end="2026-10-26T08:00:04+08:00",
        windows=[{
            "started_at": "2026-10-26T08:00:00+08:00",
            "ended_at": "2026-10-26T08:00:10+08:00",
            "target_app": "com.tencent.xinWeChat",
        }],
    )
    assert res["status"] == "resolved"
    assert res["meaning"] == "input_target_only_not_delivery"
