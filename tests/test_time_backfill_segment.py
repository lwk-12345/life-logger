"""B 类严格回填：按 segment 同 id 的转录行恢复发生时间（2026-10-24）。

规则边界（不可放宽）：
  * 只认 `formal-asr/segments/<dir>/state.json` 里 `segment_id` **完全相同**的行；
  * 该行 `absolute_started_at` 与 `evidence.reported_at` 相差必须 ≤2 秒；
  * 段文件不唯一 / 行不唯一 / 找不到 / 超差 → 一律保持 unknown；
  * 事件总数不变、既有 absolute 不被覆盖；A 类（无 segment_id）不填精确时刻。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.event_view import (  # noqa: E402
    SEGMENT_OCCURRENCE_TOLERANCE_SECONDS,
    _prepare_event,
    build_event_view,
)

WEB_ROOT = ROOT / "output" / "local-web"


def _write_segment(root: Path, session: str, seg_dir: str, rows: list[dict]) -> Path:
    path = root / "watch-sessions" / "watch-dev" / session / "formal-asr" / "segments" / seg_dir
    path.mkdir(parents=True, exist_ok=True)
    (path / "state.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")
    return path


def _event(segment_id: str, reported_at: str, recording: str = "watch_SESS1") -> dict:
    return {
        "event_id": "evt_ctx_test",
        "time": {"precision": "unknown", "absolute": None},
        "evidence": {
            "segment_id": segment_id,
            "reported_at": reported_at,
            "recording_id": recording,
            "quote": "测试原话",
        },
    }


def test_tolerance_is_two_seconds() -> None:
    assert SEGMENT_OCCURRENCE_TOLERANCE_SECONDS == 2.0


def test_backfills_when_row_time_matches_within_2s(tmp_path: Path) -> None:
    _write_segment(tmp_path, "SESS1", "000220", [
        {"segment_id": "watch_000220_000001", "absolute_started_at": "2026-10-22T14:04:26.728000+00:00"},
    ])
    prepared, info = _prepare_event(
        _event("watch_000220_000001", "2026-10-22T14:04:26.728000+00:00"),
        None,
        web_root=tmp_path,
    )
    assert info["basis"] == "segment_transcript_same_id"
    assert prepared["time"]["absolute"] == "2026-10-22T22:04:26.728000+08:00"
    assert prepared["time"]["precision"] == "inferred"
    assert prepared["time"]["occurrence_source"] == "segment_transcript"
    ev = prepared["evidence"]["occurrence_time_evidence"]
    assert ev["status"] == "matched"
    assert ev["delta_seconds"] == 0.0
    # event_view 统一把时间规范化到本地时区（Asia/Shanghai +08:00），审计字段同样如此
    assert ev["segment_started_at"] == "2026-10-22T22:04:26.728000+08:00"


def test_backfills_at_exactly_2s_boundary(tmp_path: Path) -> None:
    _write_segment(tmp_path, "SESS1", "000010", [
        {"segment_id": "watch_000010_000001", "absolute_started_at": "2026-10-22T14:00:00+00:00"},
    ])
    prepared, info = _prepare_event(
        _event("watch_000010_000001", "2026-10-22T14:00:02+00:00"), None, web_root=tmp_path
    )
    assert info["basis"] == "segment_transcript_same_id"
    assert prepared["evidence"]["occurrence_time_evidence"]["delta_seconds"] == 2.0


def test_does_not_backfill_beyond_2s(tmp_path: Path) -> None:
    _write_segment(tmp_path, "SESS1", "000010", [
        {"segment_id": "watch_000010_000001", "absolute_started_at": "2026-10-22T14:00:00+00:00"},
    ])
    event = _event("watch_000010_000001", "2026-10-22T14:00:03+00:00")
    prepared, info = _prepare_event(event, None, web_root=tmp_path)
    assert info["repaired"] is False
    assert prepared["time"]["absolute"] is None
    assert prepared["evidence"]["occurrence_time_evidence"]["status"] == "out_of_tolerance"


def test_missing_segment_file_stays_unknown(tmp_path: Path) -> None:
    (tmp_path / "watch-sessions").mkdir()
    prepared, info = _prepare_event(
        _event("watch_000220_000001", "2026-10-22T14:04:26.728000+00:00"), None, web_root=tmp_path
    )
    assert prepared["time"]["absolute"] is None
    assert prepared["evidence"]["occurrence_time_evidence"]["status"] == "missing"


def test_ambiguous_segment_file_stays_unknown(tmp_path: Path) -> None:
    # 同一 segment dir 出现在两个会话下、且事件没带 recording_id → 无法判定唯一
    _write_segment(tmp_path, "SESS1", "000010", [
        {"segment_id": "watch_000010_000001", "absolute_started_at": "2026-10-22T14:00:00+00:00"},
    ])
    _write_segment(tmp_path, "SESS2", "000010", [
        {"segment_id": "watch_000010_000001", "absolute_started_at": "2026-10-22T14:00:00+00:00"},
    ])
    event = _event("watch_000010_000001", "2026-10-22T14:00:00+00:00", recording="")
    prepared, _info = _prepare_event(event, None, web_root=tmp_path)
    assert prepared["time"]["absolute"] is None
    assert prepared["evidence"]["occurrence_time_evidence"]["status"] == "ambiguous"
    assert prepared["evidence"]["occurrence_time_evidence"]["candidate_files"] == 2


def test_ambiguous_rows_stay_unknown(tmp_path: Path) -> None:
    _write_segment(tmp_path, "SESS1", "000010", [
        {"segment_id": "watch_000010_000001", "absolute_started_at": "2026-10-22T14:00:00+00:00"},
        {"segment_id": "watch_000010_000001", "absolute_started_at": "2026-10-22T14:00:01+00:00"},
    ])
    prepared, _info = _prepare_event(
        _event("watch_000010_000001", "2026-10-22T14:00:00+00:00"), None, web_root=tmp_path
    )
    assert prepared["time"]["absolute"] is None
    assert prepared["evidence"]["occurrence_time_evidence"]["status"] == "row_ambiguous"


def test_existing_absolute_is_never_overwritten(tmp_path: Path) -> None:
    _write_segment(tmp_path, "SESS1", "000010", [
        {"segment_id": "watch_000010_000001", "absolute_started_at": "2026-10-22T14:00:00+00:00"},
    ])
    event = _event("watch_000010_000001", "2026-10-22T14:00:00+00:00")
    event["time"] = {"precision": "exact", "absolute": "2026-10-22T09:00:00+00:00"}
    prepared, info = _prepare_event(event, None, web_root=tmp_path)
    assert prepared["time"]["absolute"] == "2026-10-22T09:00:00+00:00"
    assert info["basis"] != "segment_transcript_same_id"
    assert "occurrence_time_evidence" not in prepared["evidence"]


def test_session_aggregate_without_segment_stays_unknown(tmp_path: Path) -> None:
    event = {
        "event_id": "evt_session_x",
        "time": {"precision": "unknown", "absolute": None},
        "evidence": {"segment_id": None, "reported_at": None, "quote": "老板还有肉夹馍吗"},
    }
    prepared, info = _prepare_event(event, None, web_root=tmp_path)
    assert prepared["time"]["absolute"] is None
    assert info["repaired"] is False


def test_local_date_and_timezone_are_derived_from_backfill(tmp_path: Path) -> None:
    _write_segment(tmp_path, "SESS1", "000010", [
        {"segment_id": "watch_000010_000001", "absolute_started_at": "2026-10-22T16:30:00+00:00"},
    ])
    prepared, _info = _prepare_event(
        _event("watch_000010_000001", "2026-10-22T16:30:00+00:00"), None, web_root=tmp_path
    )
    # 16:30Z = 次日 00:30 Asia/Shanghai —— 本地日期必须按本地算，不能按 UTC 日期
    assert prepared["time"]["absolute"] == "2026-10-23T00:30:00+08:00"
    local = prepared["time"].get("absolute_local") or prepared["time"].get("local")
    assert local is None or local.startswith("2026-10-23")


@pytest.mark.skipif(not (WEB_ROOT / "watch-sessions").is_dir(), reason="需要本地 watch 数据")
def test_real_data_recovers_exactly_the_12_segment_events() -> None:
    """真实数据守卫：10-22 未定位 14→5，10-23 未定位 17→14，合计回填 12 条且全部 Δ≤2s。"""

    total_fixed = 0
    for day, expected_before, expected_after in (
        ("2026-10-22", 14, 5),
        ("2026-10-23", 17, 14),
    ):
        view = build_event_view(
            WEB_ROOT, date_from=day, date_to=day, scope="provisional", view="display", use_cache=True
        )
        events = view["events"]
        unlocated = [e for e in events if not (e.get("time") or {}).get("absolute")]
        fixed = [
            e for e in events
            if (e.get("time") or {}).get("occurrence_source") == "segment_transcript"
        ]
        assert len(unlocated) == expected_after, f"{day} 未定位数应为 {expected_after}"
        assert expected_after + len(fixed) == expected_before
        for event in fixed:
            assert event["time"]["precision"] == "inferred"
            evidence = event["evidence"]["occurrence_time_evidence"]
            assert evidence["status"] == "matched"
            assert evidence["delta_seconds"] <= SEGMENT_OCCURRENCE_TOLERANCE_SECONDS
        total_fixed += len(fixed)
    assert total_fixed == 12


@pytest.mark.skipif(not (WEB_ROOT / "watch-sessions").is_dir(), reason="需要本地 watch 数据")
def test_event_count_is_unchanged_by_backfill() -> None:
    """回填只改时间字段，不得增删事件。"""
    day = "2026-10-22"
    cached = build_event_view(
        WEB_ROOT, date_from=day, date_to=day, scope="provisional", view="display", use_cache=True
    )
    fresh = build_event_view(
        WEB_ROOT, date_from=day, date_to=day, scope="provisional", view="display", use_cache=False
    )
    assert len(cached["events"]) == len(fresh["events"])
    assert {e["event_id"] for e in cached["events"]} == {e["event_id"] for e in fresh["events"]}


# --- 返工（2026-10-24 · A0 独立验收）：at_speech + source_start 不得在段证据失败后按偏移回退 ---


SOURCE_START = datetime(2026, 10, 22, 14, 0, 0, tzinfo=timezone.utc)


def _at_speech_event(segment_id: str, reported_at: str, offset: float = 30.0) -> dict:
    """at_speech 事件：既有偏移量、也有 source_start —— 正是原来的"回退空间"。"""
    event = _event(segment_id, reported_at)
    event["evidence"].update(
        {
            "temporal_relation": "at_speech",
            "start_offset_seconds": offset,
        }
    )
    return event


def _assert_no_offset_fallback(prepared: dict, info: dict) -> None:
    assert prepared["time"]["absolute"] is None, prepared["time"]
    assert prepared["time"]["precision"] == "unknown"
    assert info["repaired"] is False
    assert "repaired_absolute" not in prepared["time"]
    assert prepared.get("occurred_at") is None


def test_at_speech_row_missing_does_not_fall_back_to_offset(tmp_path: Path) -> None:
    _write_segment(tmp_path, "SESS1", "000010", [
        {"segment_id": "watch_000010_000009", "absolute_started_at": "2026-10-22T14:00:00+00:00"},
    ])  # 目录存在，但没有同 id 行
    prepared, info = _prepare_event(
        _at_speech_event("watch_000010_000001", "2026-10-22T14:00:00+00:00"),
        SOURCE_START,
        web_root=tmp_path,
    )
    assert prepared["evidence"]["occurrence_time_evidence"]["status"] == "row_missing"
    _assert_no_offset_fallback(prepared, info)


def test_at_speech_row_ambiguous_does_not_fall_back_to_offset(tmp_path: Path) -> None:
    _write_segment(tmp_path, "SESS1", "000010", [
        {"segment_id": "watch_000010_000001", "absolute_started_at": "2026-10-22T14:00:00+00:00"},
        {"segment_id": "watch_000010_000001", "absolute_started_at": "2026-10-22T14:00:01+00:00"},
    ])
    prepared, info = _prepare_event(
        _at_speech_event("watch_000010_000001", "2026-10-22T14:00:00+00:00"),
        SOURCE_START,
        web_root=tmp_path,
    )
    assert prepared["evidence"]["occurrence_time_evidence"]["status"] == "row_ambiguous"
    _assert_no_offset_fallback(prepared, info)


def test_at_speech_out_of_tolerance_does_not_fall_back_to_offset(tmp_path: Path) -> None:
    _write_segment(tmp_path, "SESS1", "000010", [
        {"segment_id": "watch_000010_000001", "absolute_started_at": "2026-10-22T14:00:00+00:00"},
    ])
    prepared, info = _prepare_event(
        _at_speech_event("watch_000010_000001", "2026-10-22T14:00:09+00:00"),
        SOURCE_START,
        web_root=tmp_path,
    )
    assert prepared["evidence"]["occurrence_time_evidence"]["status"] == "out_of_tolerance"
    _assert_no_offset_fallback(prepared, info)


def test_at_speech_segment_file_missing_does_not_fall_back_to_offset(tmp_path: Path) -> None:
    (tmp_path / "watch-sessions").mkdir()
    prepared, info = _prepare_event(
        _at_speech_event("watch_000010_000001", "2026-10-22T14:00:00+00:00"),
        SOURCE_START,
        web_root=tmp_path,
    )
    assert prepared["evidence"]["occurrence_time_evidence"]["status"] == "missing"
    _assert_no_offset_fallback(prepared, info)


def test_event_without_segment_id_still_uses_offset_fallback(tmp_path: Path) -> None:
    """合法路径必须保留：无 segment_id 的历史事件仍按 source_start+offset 修补。"""
    event = {
        "event_id": "evt_manual_offset",
        "time": {"precision": "unknown", "absolute": None},
        "evidence": {"segment_id": None, "start_offset_seconds": 30.0},
    }
    prepared, info = _prepare_event(event, SOURCE_START, web_root=tmp_path)
    assert info["basis"] == "source_start_plus_offset"
    assert prepared["time"]["absolute"] is not None
    assert prepared["time"]["absolute"].startswith("2026-10-22T14:00:30")
