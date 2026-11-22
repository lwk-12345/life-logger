"""生活看板隐藏 dictation（电脑前说话）内容 —— 读层过滤的行为与「产物不动」保证。

被测机制见 ``life_logger/dictation.py``：

* 上游已经打好标记（``is_dictation`` / ``evidence.dictation_context``）的事件隐藏；
* 没打标记的按读时重算：时间窗（``dictation-windows.jsonl`` +
  ``dictation_windows_extra``）重合过半，或命中「对着电脑说话」语义特征；
* 生活保护词与显式口令（置顶便签 / 查询派发）优先保留；
* 只动 API 返回层：产物文件、待确认区、事实源一条不动；
* ``local-config.json`` 的 ``hide_dictation_from_dashboard`` 关掉即完全恢复原样。
"""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path
from typing import Any

from life_logger.dictation import (
    DashboardDictationFilter,
    build_dashboard_dictation_filter,
    is_dictation_record,
    span_is_dictation,
)
from life_logger.local_web import JobStore, create_server

WINDOW_START = "2026-10-13T09:00:00+08:00"
WINDOW_END = "2026-10-13T09:10:00+08:00"


def _event(
    event_id: str,
    title: str,
    *,
    iso: str,
    categories: list[str] | None = None,
    quote: str = "",
    dictation: bool = False,
    pinned: bool = False,
    query: bool = False,
    duration: float = 8.0,
    confidence: float = 0.95,
    review_status: str = "reviewed",
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "activity_type": "task",
        "capture_mode": "passive",
        "categories": categories or ["todo"],
        "confidence": confidence,
        "data": {},
        "event_id": event_id,
        "evidence": {
            "quote": quote or title,
            "reported_at": iso,
            "start_offset_seconds": 0.0,
            "end_offset_seconds": duration,
            "dictation_context": dictation,
            "extractor": "test",
        },
        "review_status": review_status,
        "source_type": "audio",
        "time": {"absolute": iso, "precision": "exact", "relative_seconds": 0.0},
        "title": title,
    }
    if pinned:
        event["pinned_memo"] = True
    if query:
        event["query_dispatch"] = True
    return event


def _write_session(
    root: Path,
    events: list[dict[str, Any]],
    *,
    device: str = "watch-test",
    session_id: str = "SESS0001",
) -> Path:
    session_dir = root / "watch-sessions" / device / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "events.json"
    path.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _write_config(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "local-config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _start_server(store: JobStore, config_path: Path | None):
    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        config_path=config_path,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _get(base: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{base}{path}", timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _stop(server, thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


# ---------------------------------------------------------------------------
# 过滤规则本体（不经过 HTTP）
# ---------------------------------------------------------------------------


def test_semantic_marker_and_life_guard_decide_dictation() -> None:
    assert is_dictation_record({"text": "帮我在 GitHub 上找一下"}, []) is True
    assert is_dictation_record({"text": "我买了两瓶水"}, []) is False


def test_span_overlap_needs_more_than_half_and_handles_zero_duration() -> None:
    from datetime import datetime

    windows = [
        (
            datetime.fromisoformat("2026-10-13T09:00:00+08:00"),
            datetime.fromisoformat("2026-10-13T09:00:10+08:00"),
        )
    ]
    inside = (
        datetime.fromisoformat("2026-10-13T09:00:02+08:00"),
        datetime.fromisoformat("2026-10-13T09:00:08+08:00"),
    )
    outside = (
        datetime.fromisoformat("2026-10-13T09:00:00+08:00"),
        datetime.fromisoformat("2026-10-13T09:01:00+08:00"),
    )
    assert span_is_dictation(inside, windows) is True
    assert span_is_dictation(outside, windows) is False
    moment = (
        datetime.fromisoformat("2026-10-13T09:00:05+08:00"),
        datetime.fromisoformat("2026-10-13T09:00:05+08:00"),
    )
    # 流水线口径：零时长不算 dictation（保持原语义）
    assert span_is_dictation(moment, windows) is False
    # 看板的即时抓取卡片只有时刻，走点命中口径
    assert span_is_dictation(moment, windows, point_if_empty=True) is True


def test_board_filter_does_not_hide_on_tech_words_alone() -> None:
    """判据是「录音绝对时间落在窗口内」，不是文本里出现技术词。"""
    without_windows = build_dashboard_dictation_filter({}, root=None)
    event = _event("e1", "帮我在 GitHub 上找一下模型", iso="2026-10-13T09:02:00+08:00")
    assert without_windows.hide_event(event) is False

    with_window = build_dashboard_dictation_filter(
        {"dictation_windows_extra": [{"start": WINDOW_START, "end": WINDOW_END}]},
        root=None,
    )
    outside_window = _event(
        "e2", "帮我在 GitHub 上找一下模型", iso="2026-10-13T11:00:00+08:00"
    )
    assert with_window.hide_event(outside_window) is False


def test_event_window_uses_record_absolute_time_when_present() -> None:
    """事件/记录自带 absolute_started_at 时，走与流水线相同的 record_time_window。"""
    from datetime import datetime

    from life_logger.dictation import event_time_window

    record = {
        "absolute_started_at": "2026-10-13T09:02:00+08:00",
        "absolute_ended_at": "2026-10-13T09:02:12+08:00",
        # 偏移字段若被错误叠加就会把时间轴推后一倍（2026-10-02 踩过的坑）
        "start_offset_seconds": 7200.0,
        "end_offset_seconds": 7212.0,
    }
    assert event_time_window(record) == (
        datetime.fromisoformat("2026-10-13T09:02:00+08:00"),
        datetime.fromisoformat("2026-10-13T09:02:12+08:00"),
    )


def test_extra_windows_parsing_skips_junk() -> None:
    filter_ = build_dashboard_dictation_filter(
        {
            "dictation_windows_extra": [
                {"start": WINDOW_START, "end": WINDOW_END},
                {"start": "不是时间", "end": WINDOW_END},
                "垃圾",
            ]
        },
        root=None,
    )
    assert len(filter_.extra_windows) == 1


def test_switch_off_is_a_complete_noop() -> None:
    filter_ = build_dashboard_dictation_filter(
        {"hide_dictation_from_dashboard": False},
        root=None,
    )
    assert filter_.enabled is False
    event = _event(
        "e1", "设计沟通：红色线", iso="2026-10-13T09:02:00+08:00", dictation=True
    )
    assert filter_.hide_event(event) is False
    assert filter_.hide_event({"title": "GitHub 上的模型", "evidence": {}}) is False


def test_shared_module_is_the_single_implementation() -> None:
    """流水线脚本与看板过滤必须用同一份实现，避免两处漂移。"""
    from scripts.attribute_funasr_transcript import (
        is_dictation_record as script_is_dictation_record,
    )

    assert script_is_dictation_record is is_dictation_record


def test_life_guard_wins_over_window_overlap() -> None:
    filter_ = DashboardDictationFilter(
        enabled=True,
        extra_windows=build_dashboard_dictation_filter(
            {"dictation_windows_extra": [{"start": WINDOW_START, "end": WINDOW_END}]},
            root=None,
        ).extra_windows,
    )
    bought = _event("e1", "买了两个冰杯", iso="2026-10-13T09:02:00+08:00")
    assert filter_.hide_event(bought) is False
    design_talk = _event("e2", "设计沟通：绿色条", iso="2026-10-13T09:02:00+08:00")
    assert filter_.hide_event(design_talk) is True


def test_scene_fully_inside_window_is_hidden() -> None:
    filter_ = build_dashboard_dictation_filter(
        {"dictation_windows_extra": [{"start": WINDOW_START, "end": WINDOW_END}]},
        root=None,
    )
    inside = {"start_minute": 9 * 60, "end_minute": 9 * 60 + 5, "scene": "office"}
    partial = {
        "start_minute": 9 * 60 + 5,
        "end_minute": 9 * 60 + 20,
        "scene": "general",
    }
    assert filter_.hide_scene(inside, date="2026-10-13") is True
    assert filter_.hide_scene(partial, date="2026-10-13") is False
    assert filter_.hide_scene(inside, date="2026-10-14") is False


# ---------------------------------------------------------------------------
# HTTP 读层：/api/daily、/api/daily-brief、/api/reminders、/api/pending-sources
# ---------------------------------------------------------------------------


def _daily_fixture(tmp_path: Path) -> tuple[JobStore, list[dict[str, Any]]]:
    store = JobStore(tmp_path / "local-web", recover=False)
    events = [
        _event(
            "evt_dictation_flagged",
            "设计沟通：这块按我说的改",
            iso="2026-10-13T08:00:00+08:00",
            dictation=True,
        ),
        _event(
            "evt_keep_life",
            "正在抽烟",
            iso="2026-10-13T10:00:00+08:00",
            categories=["routine"],
        ),
        _event(
            "evt_in_window",
            "设计沟通：红色平行线",
            iso="2026-10-13T09:02:00+08:00",
        ),
        _event(
            "evt_pinned",
            "便签：更新台账",
            iso="2026-10-13T09:04:00+08:00",
            pinned=True,
        ),
    ]
    _write_session(store.root, events)
    return store, events


def test_daily_api_hides_dictation_and_recomputes_counts(tmp_path: Path) -> None:
    store, events = _daily_fixture(tmp_path)
    config_path = _write_config(
        tmp_path,
        {
            "hide_dictation_from_dashboard": True,
            "dictation_windows_extra": [{"start": WINDOW_START, "end": WINDOW_END}],
        },
    )
    artifact = store.root / "watch-sessions" / "watch-test" / "SESS0001" / "events.json"
    before_bytes = artifact.read_bytes()
    before_stat = artifact.stat()

    server, thread = _start_server(store, config_path)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        payload = _get(base, "/api/daily?date=2026-10-13")
        titles = [item["title"] for item in payload["events"]]
        # 对照：10:00 的「正在抽烟」在窗口外，必须仍然可见；
        # 便签是显式口令产物，也不隐藏。
        #
        # 【2026-10-17 修】顺序改为**时间正序**。原因：`/api/daily` 的事件表已统一由
        # `event_view.build_event_view` 提供（全仓唯一权威读层），它按时间排序；
        # 之前这里断言「产物写入顺序」（便签在后），那是旧拼接实现留下的耦合。
        # 本用例真正要锁的是「谁被隐藏」，不是数组顺序，所以顺序按新读层校正。
        assert titles == ["便签：更新台账", "正在抽烟"]
        assert payload["daily_summary"]["event_count"] == 2
        assert [item["title"] for item in payload["daily_summary"]["events"]] == titles
        assert "设计沟通：红色平行线" not in json.dumps(payload, ensure_ascii=False)
        assert "设计沟通：这块按我说的改" not in json.dumps(payload, ensure_ascii=False)
        # 便签是显式口令产物，不因落在窗口里被吃掉
        assert payload["pinned_memos"]
        # 由事件派生的台账也按过滤后的结果算
        assert "设计沟通" not in json.dumps(
            payload["work_cognition_ledger"], ensure_ascii=False
        )
        after = artifact.read_bytes()
        assert after == before_bytes
        assert artifact.stat().st_mtime_ns == before_stat.st_mtime_ns
        assert len(events) == 4  # 产物里的四条一条没少
    finally:
        _stop(server, thread)


def test_switch_off_restores_the_original_view(tmp_path: Path) -> None:
    store, events = _daily_fixture(tmp_path)
    config_path = _write_config(
        tmp_path,
        {
            "hide_dictation_from_dashboard": False,
            "dictation_windows_extra": [{"start": WINDOW_START, "end": WINDOW_END}],
        },
    )
    server, thread = _start_server(store, config_path)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        payload = _get(base, "/api/daily?date=2026-10-13")
        assert len(payload["events"]) == len(events)
        assert payload["daily_summary"]["event_count"] == len(events)
    finally:
        _stop(server, thread)


def test_agent_brief_hides_dictation(tmp_path: Path) -> None:
    store, _ = _daily_fixture(tmp_path)
    config_path = _write_config(
        tmp_path,
        {"dictation_windows_extra": [{"start": WINDOW_START, "end": WINDOW_END}]},
    )
    server, thread = _start_server(store, config_path)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        brief = _get(base, "/api/daily-brief?date=2026-10-13")
        text = json.dumps(brief, ensure_ascii=False)
        assert "设计沟通：红色平行线" not in text
        assert brief["event_count"] == len(brief["high_confidence_points"])
        assert "正在抽烟" in brief["searchable_summary"]
    finally:
        _stop(server, thread)


def test_pending_sources_endpoint_is_not_filtered(tmp_path: Path) -> None:
    """待确认区是人工审核队列，不属于生活看板，必须原样保留。"""
    store, events = _daily_fixture(tmp_path)
    live_context = (
        store.root
        / "watch-sessions"
        / "watch-test"
        / "SESS0001"
        / "formal-asr"
        / "live-context"
    )
    live_context.mkdir(parents=True, exist_ok=True)
    (live_context / "context-extraction.json").write_text(
        json.dumps(
            {
                "schema_version": "context-extraction-v1",
                "formal_events": events,
                "pending_sources": [
                    {
                        "source_id": "src_pending_1",
                        "segment_id": "watch_000001_000001",
                        "quote": "设计沟通：这块按我说的改",
                        "source_label": "self",
                        "reason": "model_no_event_but_directive",
                        "retryable": True,
                    }
                ],
                "source_outcomes": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    config_path = _write_config(
        tmp_path,
        {"dictation_windows_extra": [{"start": WINDOW_START, "end": WINDOW_END}]},
    )
    server, thread = _start_server(store, config_path)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        # 不带 date：待确认区是全量审核队列，这里只验证它没有被看板过滤波及
        pending = _get(base, "/api/pending-sources")
        quotes = [
            item["quote"]
            for group in pending.get("groups", [])
            for item in group.get("items", [])
        ]
        assert "设计沟通：这块按我说的改" in quotes
        # 同一个产物里的正式事件照旧完整保留（看板过滤不写回产物）
        artifact = json.loads(
            (
                store.root
                / "watch-sessions"
                / "watch-test"
                / "SESS0001"
                / "formal-asr"
                / "live-context"
                / "context-extraction.json"
            ).read_text(encoding="utf-8")
        )
        assert len(artifact["formal_events"]) == len(events)
    finally:
        _stop(server, thread)


def test_dashboard_scenes_hide_only_fully_covered_spans(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    session_dir = store.root / "watch-sessions" / "watch-test" / "SESS0002"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "manifest.json").write_text(
        json.dumps(
            {
                "started_at": "2026-10-13T08:50:00+08:00",
                "segments": [
                    # 09:00–09:10，整段落在补充 dictation 窗口里 → 隐藏
                    {
                        "sequence": 0,
                        "start_offset_ms": 600_000,
                        "duration_ms": 600_000,
                    },
                    # 12:00–12:10，与窗口无关 → 保留
                    {
                        "sequence": 1,
                        "start_offset_ms": 11_400_000,
                        "duration_ms": 600_000,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (session_dir / "events.json").write_text("[]", encoding="utf-8")
    config_path = _write_config(
        tmp_path,
        {"dictation_windows_extra": [{"start": WINDOW_START, "end": WINDOW_END}]},
    )
    server, thread = _start_server(store, config_path)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        scenes = _get(base, "/api/timeline-scenes?date=2026-10-13")["scenes"]
        spans = [(scene["start_time"], scene["end_time"]) for scene in scenes]
        assert spans == [("12:00", "12:10")]
    finally:
        _stop(server, thread)


def test_reminders_response_hides_dictation_sourced_todos(
    tmp_path: Path, monkeypatch
) -> None:
    store, events = _daily_fixture(tmp_path)
    config_path = _write_config(
        tmp_path,
        {"dictation_windows_extra": [{"start": WINDOW_START, "end": WINDOW_END}]},
    )
    server, thread = _start_server(store, config_path)
    artifact = "watch-sessions/watch-test/SESS0001/events.json"

    def fake_sync() -> list[dict[str, Any]]:
        return [
            {
                "task_key": "evt_in_window",
                "title": "设计沟通：红色平行线",
                "event_id": "evt_in_window",
                "artifact_path": artifact,
                "status": "pending",
                "source_type": "audio",
            },
            {
                "task_key": "oc_manual",
                "title": "跟陈晨哥谈一下项目",
                "event_id": None,
                "status": "pending",
                "source_type": "manual",
            },
        ]

    monkeypatch.setattr(server, "sync_local_reminders", fake_sync)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        payload = _get(base, "/api/reminders")
        keys = [item["task_key"] for item in payload["reminders"]]
        assert keys == ["oc_manual"]
        # 事实源（产物）一条不动
        assert len(json.loads((store.root / artifact).read_text(encoding="utf-8"))) == 4
        assert len(events) == 4
    finally:
        _stop(server, thread)
