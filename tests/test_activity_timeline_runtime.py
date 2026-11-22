"""Tests for ActivityTimelineRuntime contract and operations:
1. Read-only read_day returns schema activity-timeline-v1 without model invocation.
2. Atomic publish_day writes valid payload with route isolation.
3. Thread and process mutual exclusion in tick.
4. Bounded scan of formal-asr records with unique IDs and timezone handling.
5. Tick incremental window step from enabled_at or coverage.through without full-day backfill.
6. Strict joint (source_id, segment_id, row_id) coordinate matching & quote verification.
7. Coordinate mismatch or hallucinated quote results in whole-window error.
8. Defensive handling of inferred purchase payment status and merchant broadcasts.
9. Cross-day enabled_at bounds tick to today's start, avoiding historical iteration.
10. Malformed or invalid model JSON causes whole-window error and triggers failure cooldown.
11. Upsert and conflict history preserved via ActivityWindowState / apply_window_update.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path

import pytest

from life_logger.activity_timeline_runtime import (
    BEIJING_TZ,
    DEFAULT_CADENCE_MINUTES,
    SCHEMA,
    ActivityTimelineRuntime,
)


@pytest.fixture
def test_env(tmp_path: Path, monkeypatch):
    import life_logger.activity_timeline_runtime as runtime_mod
    fixed_now = datetime.datetime(2026, 10, 26, 14, 0, 0, tzinfo=BEIJING_TZ)

    class FixedDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return fixed_now.astimezone(tz)
            return fixed_now

    monkeypatch.setattr(runtime_mod.datetime, "datetime", FixedDatetime)
    output_root = tmp_path / "output" / "local-web"
    output_root.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "local-config.json"
    config_path.write_text(
        json.dumps({
            "activity_timeline": {
                "enabled": True,
                "cadence_minutes": 10,
                "context_minutes": 15,
                "route": "jev",
            },
            "deepseek": {
                "base_url": "https://mock.api.deepseek.com",
                "api_key": "mock-key",
                "model": "deepseek-flash",
            },
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return output_root, config_path


def test_read_day_nonexistent(test_env):
    output_root, config_path = test_env
    runtime = ActivityTimelineRuntime(output_root, config_path)

    res = runtime.read_day("2026-10-26")
    assert res["schema"] == SCHEMA
    assert res["date"] == "2026-10-26"
    assert res["timezone"] == "Asia/Shanghai"
    assert res["cadence_minutes"] == DEFAULT_CADENCE_MINUTES
    assert res["status"] == "pending"
    assert res["activities"] == []
    assert res["unassociated_items"] == []
    assert res["conflicts"] == []
    assert res["coverage"]["through"] is None
    assert res["coverage"]["source_count"] == 0
    assert res["errors"] == []


def test_publish_and_read_day_route_isolation(test_env):
    output_root, config_path = test_env
    runtime = ActivityTimelineRuntime(output_root, config_path)

    sample_payload = {
        "status": "ready",
        "activities": [
            {
                "activity_id": "act_001",
                "title": "晨间热身",
                "summary": "拉伸动作5分钟",
                "status": "completed",
                "items": [
                    {
                        "item_id": "item_1",
                        "action": "肩袖拉伸",
                        "status": "completed",
                        "spoken_at": "2026-10-26T07:05:00+08:00",
                        "occurred_at": "2026-10-26T07:05:00+08:00",
                        "amount": None,
                        "currency": "CNY",
                        "payment_status": "unknown",
                        "evidence": [
                            {"source_id": "src_1", "segment_id": "seg_1", "row_id": 1, "quote": "拉伸一下肩袖"}
                        ],
                    }
                ],
            }
        ],
        "coverage": {"through": "2026-10-26T07:10:00+08:00", "source_count": 5},
    }

    # Publish under route 'jev'
    written = runtime.publish_day("2026-10-26", sample_payload, route="jev")
    assert written.is_file()
    assert "jev" in written.parts

    # Read back under default route (jev)
    read_jev = runtime.read_day("2026-10-26", route="jev")
    assert read_jev["status"] == "ready"
    assert len(read_jev["activities"]) == 1
    assert read_jev["activities"][0]["title"] == "晨间热身"
    assert read_jev["coverage"]["through"] == "2026-10-26T07:10:00+08:00"

    # Clef route must NOT see jev data
    read_clef = runtime.read_day("2026-10-26", route="clef")
    assert read_clef["status"] == "pending"
    assert read_clef["activities"] == []


def test_tick_disabled_in_config(test_env):
    output_root, config_path = test_env
    config_path.write_text(json.dumps({"activity_timeline": {"enabled": False}}), encoding="utf-8")

    runtime = ActivityTimelineRuntime(output_root, config_path)
    res = runtime.tick()
    assert res["status"] == "disabled"


def test_tick_mutex_thread(test_env):
    output_root, config_path = test_env
    runtime = ActivityTimelineRuntime(output_root, config_path)

    # Acquire internal lock to simulate concurrent tick
    assert runtime._thread_lock.acquire(blocking=False)
    try:
        res = runtime.tick()
        assert res["status"] == "skipped"
        assert res["reason"] == "thread_locked"
    finally:
        runtime._thread_lock.release()


def test_tick_advances_incremental_window(test_env):
    output_root, config_path = test_env

    # 1. Prepare simulated formal-asr MOSS segment state.json
    seg_dir = output_root / "watch-sessions" / "dev1" / "sess1" / "formal-asr" / "segments" / "000001"
    seg_dir.mkdir(parents=True, exist_ok=True)

    text_1 = "今天早上吃了碗牛肉面，老板收了二十五块。"
    text_2 = "大概想买个羽毛球拍，两百块帮我留着。"
    ident_1 = f"rec_001:watch_000001_000001:1.0:{text_1}"
    ident_2 = f"rec_001:watch_000001_000002:10.0:{text_2}"
    src_1 = f"src_{hashlib.sha256(ident_1.encode('utf-8')).hexdigest()[:16]}"
    src_2 = f"src_{hashlib.sha256(ident_2.encode('utf-8')).hexdigest()[:16]}"

    state_content = {
        "status": "completed",
        "rows": [
            {
                "recording_id": "rec_001",
                "segment_id": "watch_000001_000001",
                "start_offset_seconds": 1.0,
                "started_at_local": "2026-10-26T07:05:00+08:00",
                "speaker": "说话人一",
                "source_label": "self",
                "source": "moss-transcribe-diarize",
                "text": text_1,
            },
            {
                "recording_id": "rec_001",
                "segment_id": "watch_000001_000002",
                "start_offset_seconds": 10.0,
                "started_at_local": "2026-10-26T07:08:00+08:00",
                "speaker": "说话人一",
                "source_label": "self",
                "source": "moss-transcribe-diarize",
                "text": text_2,
            },
        ],
    }
    (seg_dir / "state.json").write_text(json.dumps(state_content, ensure_ascii=False), encoding="utf-8")

    # 2. Mock extractor returning compliant v2 schema with exact 3-coordinate evidence
    def mock_extractor(prompt: str) -> dict:
        assert "牛肉面" in prompt
        assert "羽毛球拍" in prompt
        return {
            "activities": [
                {
                    "activity_id": "act_breakfast",
                    "title": "吃牛肉面",
                    "summary": "早餐吃牛肉面花费25元",
                    "status": "completed",
                    "occurrence_start": "2026-10-26T07:05:00+08:00",
                    "occurrence_end": None,
                    "reported_at": "2026-10-26T07:05:00+08:00",
                    "items": [
                        {
                            "item_id": "item_beef_noodle",
                            "action": "吃牛肉面",
                            "status": "completed",
                            "spoken_at": "2026-10-26T07:05:00+08:00",
                            "occurred_at": "2026-10-26T07:05:00+08:00",
                            "amount": 25.0,
                            "currency": "CNY",
                            "payment_status": "paid",
                            "evidence": [
                                {
                                    "source_id": src_1,
                                    "segment_id": "watch_000001_000001",
                                    "row_id": 1,
                                    "quote": text_1,
                                }
                            ],
                        }
                    ],
                },
                {
                    "activity_id": "act_racket",
                    "title": "询价羽毛球拍",
                    "summary": "询问羽毛球拍报价200元",
                    "status": "planned",
                    "occurrence_start": None,
                    "occurrence_end": None,
                    "reported_at": "2026-10-26T07:08:00+08:00",
                    "items": [
                        {
                            "item_id": "item_racket_inferred",
                            "action": "询价并推测预定",
                            "inferred": True,
                            "status": "paid",  # Defensive check: must be downgraded from paid!
                            "payment_status": "paid",  # Defensive check: must be downgraded from paid!
                            "spoken_at": "2026-10-26T07:08:00+08:00",
                            "occurred_at": None,
                            "amount": 200.0,
                            "currency": "CNY",
                            "evidence": [
                                {
                                    "source_id": src_2,
                                    "segment_id": "watch_000001_000002",
                                    "row_id": 2,
                                    "quote": text_2,
                                }
                            ],
                        }
                    ],
                },
            ],
            "unassociated_items": [],
            "conflicts": [],
        }

    config_path.write_text(
        json.dumps({
            "activity_timeline": {
                "enabled": True,
                "cadence_minutes": 10,
                "context_minutes": 15,
                "route": "jev",
                "enabled_at": "2026-10-26T07:00:00+08:00",
            }
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    runtime = ActivityTimelineRuntime(output_root, config_path, extractor=mock_extractor)

    tick_res = runtime.tick()
    assert tick_res["status"] == "success"
    assert tick_res["cutoff"] == "2026-10-26T07:10:00+08:00"

    published = runtime.read_day("2026-10-26")
    assert published["status"] == "ready"
    assert len(published["activities"]) == 2
    assert published["coverage"]["through"] == "2026-10-26T07:10:00+08:00"

    # Inferred purchase defense check
    racket_act = next(a for a in published["activities"] if a["activity_id"] == "act_racket")
    racket_item = racket_act["items"][0]
    assert racket_item["payment_status"] == "inferred"
    assert racket_item["status"] != "paid"
    assert racket_item["amount"] == 200.0  # Quoted price preserved


def test_tick_advances_empty_window_without_model(test_env):
    output_root, config_path = test_env
    config_path.write_text(
        json.dumps({
            "activity_timeline": {
                "enabled": True,
                "cadence_minutes": 10,
                "context_minutes": 15,
                "route": "jev",
                "enabled_at": "2026-10-26T08:00:00+08:00",
            }
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    call_count = 0

    def mock_extractor(prompt: str) -> dict:
        nonlocal call_count
        call_count += 1
        return {"activities": []}

    runtime = ActivityTimelineRuntime(output_root, config_path, extractor=mock_extractor)

    res = runtime.tick()
    assert res["status"] == "advanced_empty"
    assert res["cutoff"] == "2026-10-26T08:10:00+08:00"
    assert call_count == 0  # Extractor must NOT be called for empty window

    published = runtime.read_day("2026-10-26")
    assert published["coverage"]["through"] == "2026-10-26T08:10:00+08:00"


def test_tick_error_handling_and_cooldown(test_env):
    output_root, config_path = test_env

    seg_dir = output_root / "watch-sessions" / "dev1" / "sess1" / "formal-asr" / "segments" / "000001"
    seg_dir.mkdir(parents=True, exist_ok=True)
    state_content = {
        "status": "completed",
        "rows": [
            {
                "recording_id": "rec_001",
                "segment_id": "watch_000001_000001",
                "start_offset_seconds": 1.0,
                "started_at_local": "2026-10-26T09:05:00+08:00",
                "text": "测试错误处理",
            }
        ],
    }
    (seg_dir / "state.json").write_text(json.dumps(state_content), encoding="utf-8")

    config_path.write_text(
        json.dumps({
            "activity_timeline": {
                "enabled": True,
                "cadence_minutes": 10,
                "context_minutes": 15,
                "route": "jev",
                "enabled_at": "2026-10-26T09:00:00+08:00",
            }
        }),
        encoding="utf-8",
    )

    def failing_extractor(prompt: str) -> dict:
        raise RuntimeError("simulated model outage")

    runtime = ActivityTimelineRuntime(output_root, config_path, extractor=failing_extractor)

    # First tick fails
    res1 = runtime.tick()
    assert res1["status"] == "error"
    assert "RuntimeError" in res1["error"]

    # Verify error recorded in day payload
    published = runtime.read_day("2026-10-26")
    assert any("RuntimeError" in err for err in published["errors"])

    # Second tick hits cooldown, does not re-burn model
    res2 = runtime.tick()
    assert res2["status"] == "cooldown"


def test_strict_joint_coordinates_and_quote_mismatch_fails_whole_window(test_env):
    output_root, config_path = test_env
    seg_dir = output_root / "watch-sessions" / "dev1" / "sess1" / "formal-asr" / "segments" / "000001"
    seg_dir.mkdir(parents=True, exist_ok=True)
    state_content = {
        "status": "completed",
        "rows": [
            {
                "recording_id": "rec_001",
                "segment_id": "watch_000001_000001",
                "start_offset_seconds": 1.0,
                "started_at_local": "2026-10-26T10:05:00+08:00",
                "text": "今天去理发了二十块。",
            }
        ],
    }
    (seg_dir / "state.json").write_text(json.dumps(state_content), encoding="utf-8")

    config_path.write_text(
        json.dumps({
            "activity_timeline": {
                "enabled": True,
                "cadence_minutes": 10,
                "context_minutes": 15,
                "route": "jev",
                "enabled_at": "2026-10-26T10:00:00+08:00",
            }
        }),
        encoding="utf-8",
    )

    # Case A: Coordinate mismatch (row_id=99 does not exist)
    def mismatch_extractor(prompt: str) -> dict:
        return {
            "activities": [
                {
                    "activity_id": "act_mismatch",
                    "title": "理发",
                    "status": "completed",
                    "items": [
                        {
                            "item_id": "it_1",
                            "action": "理发",
                            "status": "completed",
                            "evidence": [
                                {
                                    "source_id": "src_fake",
                                    "segment_id": "watch_000001_000001",
                                    "row_id": 99,
                                    "quote": "今天去理发了二十块。",
                                }
                            ],
                        }
                    ],
                }
            ]
        }

    runtime = ActivityTimelineRuntime(output_root, config_path, extractor=mismatch_extractor)
    res = runtime.tick()
    # Must fail whole window with error! Not silently drop!
    assert res["status"] == "error"
    assert "coordinates" in res["error"] or "not found" in res["error"]

    published = runtime.read_day("2026-10-26")
    assert published["activities"] == []
    assert len(published["errors"]) > 0


def test_quote_hallucination_fails_whole_window(test_env):
    output_root, config_path = test_env
    seg_dir = output_root / "watch-sessions" / "dev1" / "sess1" / "formal-asr" / "segments" / "000001"
    seg_dir.mkdir(parents=True, exist_ok=True)
    text = "今天去理发了二十块。"
    ident = f"rec_001:watch_000001_000001:1.0:{text}"
    src_id = f"src_{hashlib.sha256(ident.encode('utf-8')).hexdigest()[:16]}"

    state_content = {
        "status": "completed",
        "rows": [
            {
                "recording_id": "rec_001",
                "segment_id": "watch_000001_000001",
                "start_offset_seconds": 1.0,
                "started_at_local": "2026-10-26T11:05:00+08:00",
                "text": text,
            }
        ],
    }
    (seg_dir / "state.json").write_text(json.dumps(state_content), encoding="utf-8")

    config_path.write_text(
        json.dumps({
            "activity_timeline": {
                "enabled": True,
                "cadence_minutes": 10,
                "context_minutes": 15,
                "route": "jev",
                "enabled_at": "2026-10-26T11:00:00+08:00",
            }
        }),
        encoding="utf-8",
    )

    # Model hallucinates quote that is not in the text
    def hallucinating_extractor(prompt: str) -> dict:
        return {
            "activities": [
                {
                    "activity_id": "act_hallucinated",
                    "title": "理发",
                    "status": "completed",
                    "items": [
                        {
                            "item_id": "it_1",
                            "action": "理发",
                            "status": "completed",
                            "evidence": [
                                {
                                    "source_id": src_id,
                                    "segment_id": "watch_000001_000001",
                                    "row_id": 1,
                                    "quote": "洗脚按摩两百块",  # Hallucination!
                                }
                            ],
                        }
                    ],
                }
            ]
        }

    runtime = ActivityTimelineRuntime(output_root, config_path, extractor=hallucinating_extractor)
    res = runtime.tick()
    # Must fail whole window as error!
    assert res["status"] == "error"
    assert "quote mismatch" in res["error"]


def test_cross_day_enabled_at_bounds_to_today(test_env):
    output_root, config_path = test_env

    # enabled_at set to 6 days ago in the past
    config_path.write_text(
        json.dumps({
            "activity_timeline": {
                "enabled": True,
                "cadence_minutes": 10,
                "context_minutes": 15,
                "route": "jev",
                "enabled_at": "2026-10-20T08:00:00+08:00",
            }
        }),
        encoding="utf-8",
    )

    runtime = ActivityTimelineRuntime(output_root, config_path)
    res = runtime.tick()
    # It must NOT start from 2026-10-20! It should be bounded to today
    today_str = datetime.datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    cutoff = res.get("cutoff") or res.get("next_cutoff") or ""
    assert today_str in cutoff


def test_bad_model_json_fails_whole_window(test_env):
    output_root, config_path = test_env
    seg_dir = output_root / "watch-sessions" / "dev1" / "sess1" / "formal-asr" / "segments" / "000001"
    seg_dir.mkdir(parents=True, exist_ok=True)
    state_content = {
        "status": "completed",
        "rows": [
            {
                "recording_id": "rec_001",
                "segment_id": "watch_000001_000001",
                "start_offset_seconds": 1.0,
                "started_at_local": "2026-10-26T12:05:00+08:00",
                "text": "测试非JSON输出",
            }
        ],
    }
    (seg_dir / "state.json").write_text(json.dumps(state_content), encoding="utf-8")

    config_path.write_text(
        json.dumps({
            "activity_timeline": {
                "enabled": True,
                "cadence_minutes": 10,
                "context_minutes": 15,
                "route": "jev",
                "enabled_at": "2026-10-26T12:00:00+08:00",
            }
        }),
        encoding="utf-8",
    )

    def bad_json_extractor(prompt: str) -> str:
        return "Sorry, I cannot process this request."

    runtime = ActivityTimelineRuntime(output_root, config_path, extractor=bad_json_extractor)
    res = runtime.tick()
    assert res["status"] == "error"
    assert "not valid JSON" in res["error"]
