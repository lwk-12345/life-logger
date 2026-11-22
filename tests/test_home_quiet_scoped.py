"""Q628：`home_quiet` 必须只统计**当天**的 no_speech，不许用全局计数。

背景（Q626 诊断）：
`daily_dashboard._watch_scene_aggregate()` 里 `no_speech` 是**跨所有会话/日期**的
累加量，而 `build_for_date()` 把 `agg["no_speech"]` 整个加进**当天**的
`scene_counts["home_quiet"]`。结果：任何走 fallback 分支的日期，
场景占比都被这个**全局常量**污染 —— 实测 2026-10-30 / 10-31 / 11-02
三天的 `scene_breakdown_percentages` **逐字节相同**（`home_quiet: 100.0`）。

本文件把这些固化成测试：
  1. 聚合层必须给出「按天」的 no_speech 计数；
  2. 不同日期的 home_quiet 不得相同（除非当天真实数据恰好相同）；
  3. 当天的 no_speech 不得被别的日期污染。
"""
from __future__ import annotations

import json
from pathlib import Path

from life_logger.daily_dashboard import _watch_scene_aggregate


def _write_session(
    watch_root: Path,
    *,
    session_id: str,
    started_at: str,
    no_speech_sequences: list[int],
    device: str = "watch-test",
) -> None:
    """造一个最小会话：manifest + N 个 no_speech 段（含 state.json）。"""
    session = watch_root / device / session_id
    seg_dir = session / "formal-asr" / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    (session / "manifest.json").write_text(json.dumps({
        "started_at": started_at,
        "segments": [
            {"sequence": seq, "start_offset_ms": seq * 60_000}
            for seq in no_speech_sequences
        ],
    }), encoding="utf-8")
    for seq in no_speech_sequences:
        d = seg_dir / f"{seq:06d}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(json.dumps({
            "sequence": seq,
            "status": "no_speech",
            "rows": [],
            "error": None,
        }), encoding="utf-8")


def _fresh_aggregate(watch_root: Path) -> dict:
    """绕过内存/磁盘缓存，强制重算。"""
    from life_logger import daily_dashboard as dd

    dd._WATCH_AGG_CACHE.clear()
    dd._WATCH_SCAN_MEMO.clear()
    return _watch_scene_aggregate(watch_root)


def test_aggregate_reports_no_speech_per_day(tmp_path):
    """聚合结果必须**按天**给出 no_speech 计数（当前实现只给全局量）。"""
    watch_root = tmp_path / "watch-sessions"
    _write_session(watch_root, session_id="2026-10-30T10:00:00+08:00" + "-a",
                   started_at="2026-10-30T10:00:00+08:00",
                   no_speech_sequences=[0, 1, 2])          # 3 段，10-30
    _write_session(watch_root, session_id="2026-10-31T10:00:00+08:00" + "-b",
                   started_at="2026-10-31T10:00:00+08:00",
                   no_speech_sequences=[0])                # 1 段，10-31

    agg = _fresh_aggregate(watch_root)
    dates = agg.get("dates") or {}
    assert dates.get("2026-10-30", {}).get("no_speech") == 3, (
        f"10-30 应有 3 段 no_speech，实际 {dates.get('2026-10-30', {}).get('no_speech')!r}"
        f"（全局量={agg.get('no_speech')}，说明没按天归桶）"
    )
    assert dates.get("2026-10-31", {}).get("no_speech") == 1, (
        f"10-31 应有 1 段 no_speech，实际 {dates.get('2026-10-31', {}).get('no_speech')!r}"
    )


def test_no_speech_not_contaminated_across_days(tmp_path):
    """A 日的 no_speech 不能被 B 日污染（这是「三天输出完全相同」的根因）。"""
    watch_root = tmp_path / "watch-sessions"
    _write_session(watch_root, session_id="s-a", started_at="2026-10-30T09:00:00+08:00",
                   no_speech_sequences=[0])
    _write_session(watch_root, session_id="s-b", started_at="2026-10-31T09:00:00+08:00",
                   no_speech_sequences=[0, 1, 2, 3, 4, 5, 6])

    agg = _fresh_aggregate(watch_root)
    dates = agg.get("dates") or {}
    a = (dates.get("2026-10-30") or {}).get("no_speech")
    b = (dates.get("2026-10-31") or {}).get("no_speech")
    assert (a, b) == (1, 7), f"按天计数错误：10-30={a!r}（应 1），10-31={b!r}（应 7）"
    assert a != b, "两天的 no_speech 计数相同 → 说明仍在用全局量"


def test_no_speech_day_derived_from_manifest_offset(tmp_path):
    """跨零点的段必须落到**它自己**所在的北京日（00:30 前一段属前一天）。"""
    watch_root = tmp_path / "watch-sessions"
    session = watch_root / "watch-test" / "s-midnight"
    seg_dir = session / "formal-asr" / "segments"
    seg_dir.mkdir(parents=True)
    # 会话 23:50 开始，第 2 段偏移 +20 分钟 → 次日 00:10
    (session / "manifest.json").write_text(json.dumps({
        "started_at": "2026-10-30T23:50:00+08:00",
        "segments": [
            {"sequence": 0, "start_offset_ms": 0},
            {"sequence": 1, "start_offset_ms": 20 * 60_000},
        ],
    }), encoding="utf-8")
    for seq in (0, 1):
        d = seg_dir / f"{seq:06d}"
        d.mkdir()
        (d / "state.json").write_text(json.dumps({
            "sequence": seq, "status": "no_speech", "rows": [], "error": None,
        }), encoding="utf-8")

    agg = _fresh_aggregate(watch_root)
    dates = agg.get("dates") or {}
    assert (dates.get("2026-10-30") or {}).get("no_speech") == 1
    assert (dates.get("2026-10-31") or {}).get("no_speech") == 1
