"""会话级聚合（session_aggregator）接入生产链路的回归。

为什么需要这份测试：
`src/life_logger/session_aggregator.py` 早就存在，也能单测通过，但**没有任何生产
调用方引用它** —— 也就是说「用百万上下文理解整场会话」这个能力只活在测试里。
本文件锁住三件事：
  1. `_sync_live_session_events`（真实生产入口）确实调用了 `aggregate_session_events`；
  2. 会话级产出的事件真的并进了 `events.json`，并带可追溯的 `extractor` 标记；
  3. 触发条件是受控的（太短 / self 行太少 / 配置关闭 都不调用），
     并且每次都会落一份 `session-aggregation.json` 旁证。
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pytest

from life_logger.watch_formal_asr import WatchFormalASRProcessor
from life_logger.watch_stream import WatchSessionStore


def _build_session(
    tmp_path: Path,
    *,
    rows: list[dict],
    config_extra: dict | None = None,
):
    """造一个最小可跑的 watch 会话（manifest + 一段 state.json）。"""
    store = WatchSessionStore(tmp_path / "watch-sessions")
    store.start_session(
        device_id="watch", session_id="session", started_at="2026-10-11T13:15:00+08:00"
    )
    audio = b"sealed-test-audio"
    store.receive_segment(
        device_id="watch",
        session_id="session",
        sequence=0,
        start_offset_ms=0,
        duration_ms=60000,
        checksum="sha256:" + hashlib.sha256(audio).hexdigest(),
        body=audio,
    )
    directory = store.session_directory("watch", "session")
    state = directory / "formal-asr/segments/000000/state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps({"status": "completed", "rows": rows}, ensure_ascii=False),
        encoding="utf-8",
    )
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"deepseek": {"api_key": "offline"}, **(config_extra or {})}))
    processor = WatchFormalASRProcessor(store, config, threading.Event())
    return processor, directory, config


def _row(offset: float, text: str, *, label: str = "self") -> dict:
    return {
        "segment_id": f"s{int(offset)}",
        "source_label": label,
        "text": text,
        "absolute_started_at": f"2026-10-11T13:{15 + int(offset // 60):02d}:00+08:00",
        "start_offset_seconds": offset,
        "end_offset_seconds": offset + 5,
    }


@pytest.fixture(autouse=True)
def _no_model_work(monkeypatch):
    """批次抽取走离线桩：本文件只验证会话级接线，不打真实模型。"""
    monkeypatch.setattr(
        "life_logger.contextual_extraction.run_contextual_extraction",
        lambda *_a, **_k: {"formal_events": []},
    )


def test_production_path_really_calls_session_aggregator(tmp_path, monkeypatch):
    """核心断言：真实入口调用到了会话级聚合，且产出并进 events.json。"""
    calls: list[list[dict]] = []

    def fake_aggregate(session_rows, **_kwargs):
        calls.append(list(session_rows))
        return [
            {
                "title": "健身：平板杠铃卧推训练",
                "categories": ["exercise"],
                "event_state": "completed",
                "time": {"absolute": "2026-10-11T13:15:16+08:00", "end": "2026-10-11T14:25:00+08:00"},
                "evidence": {"quote": "现在正式去健身房"},
                "data": {
                    "exercise": {
                        "workout_type": "strength",
                        "exercises": [{"name": "平板卧推", "sets": [{"set": 1, "weight_kg": 40}]}],
                    }
                },
            }
        ]

    monkeypatch.setattr(
        "life_logger.session_aggregator.aggregate_session_events", fake_aggregate
    )

    rows = [_row(i * 60, f"第{i}组卧推40公斤") for i in range(30)]
    processor, directory, _ = _build_session(tmp_path, rows=rows)

    processor._sync_live_session_events("watch", "session", {"deepseek": {"api_key": "offline"}})

    assert calls, "生产链路必须真的调用 aggregate_session_events"
    assert len(calls[0]) == len(rows), "必须把整场会话（而不是单句）交给会话级聚合"

    events = json.loads((directory / "events.json").read_text())
    session_events = [e for e in events if (e.get("evidence") or {}).get("extractor") == "session_aggregator"]
    assert session_events, "会话级产出必须并进 events.json"
    assert session_events[0]["title"] == "健身：平板杠铃卧推训练"
    assert session_events[0]["categories"] == ["exercise"]
    # 可追溯：能看出这是「整场会话理解」而非单句抽取
    assert session_events[0]["capture_mode"] == "session_aggregation"
    assert session_events[0]["evidence"]["session_scope"]["rows"] == len(rows)


def test_sidecar_records_that_aggregation_ran(tmp_path, monkeypatch):
    """每场会话都落 session-aggregation.json —— 不靠读代码相信。"""
    monkeypatch.setattr(
        "life_logger.session_aggregator.aggregate_session_events", lambda *_a, **_k: []
    )
    rows = [_row(i * 60, f"第{i}组") for i in range(30)]
    processor, directory, _ = _build_session(tmp_path, rows=rows)
    processor._sync_live_session_events("watch", "session", {"deepseek": {"api_key": "offline"}})

    sidecar = directory / "formal-asr" / "live-context" / "session-aggregation.json"
    assert sidecar.is_file(), "缺少调用旁证文件"
    stats = json.loads(sidecar.read_text())
    assert stats["called"] is True
    assert stats["rows"] == len(rows)
    assert stats["self_rows"] == len(rows)
    assert "elapsed_seconds" in stats


def test_short_session_is_not_sent_to_llm(tmp_path, monkeypatch):
    """短会话不调用：只有 2 分钟、8 行，不值得烧一次百万上下文。"""
    monkeypatch.setattr(
        "life_logger.session_aggregator.aggregate_session_events",
        lambda *_a, **_k: pytest.fail("短会话不该调用会话级聚合"),
    )
    rows = [_row(i * 15, f"第{i}句") for i in range(8)]
    processor, directory, _ = _build_session(tmp_path, rows=rows)
    processor._sync_live_session_events("watch", "session", {"deepseek": {"api_key": "offline"}})

    stats = json.loads((directory / "formal-asr" / "live-context" / "session-aggregation.json").read_text())
    assert stats["called"] is False
    assert stats["skip_reason"] == "too_short"


def test_too_few_self_rows_skips_aggregation(tmp_path, monkeypatch):
    """全场都是别人的声音 → 没有必要做「本人生活」会话级理解。"""
    monkeypatch.setattr(
        "life_logger.session_aggregator.aggregate_session_events",
        lambda *_a, **_k: pytest.fail("self 行不足时不该调用"),
    )
    rows = [_row(i * 60, f"电视里第{i}句", label="non_self") for i in range(30)]
    processor, directory, _ = _build_session(tmp_path, rows=rows)
    processor._sync_live_session_events("watch", "session", {"deepseek": {"api_key": "offline"}})

    stats = json.loads((directory / "formal-asr" / "live-context" / "session-aggregation.json").read_text())
    assert stats["called"] is False
    assert stats["skip_reason"] == "too_few_self_rows"


def test_config_can_disable_session_aggregation(tmp_path, monkeypatch):
    """配置关闭后绝不调用（成本开关）。"""
    monkeypatch.setattr(
        "life_logger.session_aggregator.aggregate_session_events",
        lambda *_a, **_k: pytest.fail("配置关闭时不该调用"),
    )
    rows = [_row(i * 60, f"第{i}句") for i in range(30)]
    processor, directory, _ = _build_session(tmp_path, rows=rows)
    # 生产里 config 来自 load_local_config()，含完整配置节；这里显式传关闭开关。
    processor._sync_live_session_events(
        "watch",
        "session",
        {"deepseek": {"api_key": "offline"}, "session_aggregation": {"enabled": False}},
    )

    stats = json.loads((directory / "formal-asr" / "live-context" / "session-aggregation.json").read_text())
    assert stats["called"] is False
    assert stats["skip_reason"] == "disabled"


def test_session_event_duplicating_batch_expense_is_dropped(tmp_path, monkeypatch):
    """会话级不能把批次已经记过的消费再记一遍。"""
    monkeypatch.setattr(
        "life_logger.contextual_extraction.run_contextual_extraction",
        lambda *_a, **_k: {
            "formal_events": [
                {
                    "event_id": "e1",
                    "title": "购买烧鸭饭 (¥12.00)",
                    "categories": ["expense", "food"],
                    "time": {"absolute": "2026-10-11T13:30:00+08:00", "precision": "exact"},
                    "evidence": {"reported_at": "2026-10-11T13:30:00+08:00", "quote": "要一份烧鸭饭"},
                    "data": {"expense": {"amount": 12.0, "item": "烧鸭饭"}},
                }
            ]
        },
    )
    monkeypatch.setattr(
        "life_logger.session_aggregator.aggregate_session_events",
        lambda *_a, **_k: [
            {
                "title": "午餐：烧鸭饭",
                "categories": ["food", "expense"],
                "time": {"absolute": "2026-10-11T13:32:00+08:00"},
                "evidence": {"quote": "要一份烧鸭饭"},
                "data": {"expense": {"amount": 12.0, "item": "烧鸭饭"}},
            }
        ],
    )
    rows = [_row(i * 60, f"第{i}句") for i in range(30)]
    processor, directory, _ = _build_session(tmp_path, rows=rows)
    processor._sync_live_session_events("watch", "session", {"deepseek": {"api_key": "offline"}})

    events = json.loads((directory / "events.json").read_text())
    expenses = [
        e for e in events
        if "expense" in (e.get("categories") or [])
        and ((e.get("data") or {}).get("expense") or {}).get("amount")
    ]
    assert len(expenses) == 1, f"同一笔 ¥12 不应重复计入：{expenses}"
    stats = json.loads((directory / "formal-asr" / "live-context" / "session-aggregation.json").read_text())
    assert stats["called"] is True
    assert stats["skipped_duplicate"] == 1


def test_unknown_category_from_session_model_is_rejected(tmp_path, monkeypatch):
    """会话级模型不许凭空造类别（只收既有七类）。"""
    monkeypatch.setattr(
        "life_logger.session_aggregator.aggregate_session_events",
        lambda *_a, **_k: [
            {"title": "凭空发明的东西", "categories": ["made_up_category"], "data": {}},
            {"title": "真正的工作沟通", "categories": ["work_communication"], "data": {}},
        ],
    )
    rows = [_row(i * 60, f"第{i}句") for i in range(30)]
    processor, directory, _ = _build_session(tmp_path, rows=rows)
    processor._sync_live_session_events("watch", "session", {"deepseek": {"api_key": "offline"}})

    events = json.loads((directory / "events.json").read_text())
    titles = [e.get("title") for e in events]
    assert "凭空发明的东西" not in titles
    assert "真正的工作沟通" in titles
    stats = json.loads((directory / "formal-asr" / "live-context" / "session-aggregation.json").read_text())
    assert stats["skipped_invalid"] == 1
    assert stats["merged"] == 1


def test_low_value_routine_session_event_is_sent_to_review_not_timeline(tmp_path, monkeypatch):
    """会话级闲聊（routine）不能冒充正式事件进时间轴，但必须落在待复核里。

    实测真实调用产出过「哼唱情歌片段」「晨间睡眠状况闲聊」，都是 routine。
    直接标 reviewed 会把噪音写成生活事实；直接丢弃又违反「不许静默丢数据」。
    """
    monkeypatch.setattr(
        "life_logger.session_aggregator.aggregate_session_events",
        lambda *_a, **_k: [
            {"title": "哼唱情歌片段", "categories": ["routine"], "data": {}},
            {"title": "健身：卧推训练", "categories": ["exercise"], "data": {}},
        ],
    )
    rows = [_row(i * 60, f"第{i}句") for i in range(30)]
    processor, directory, _ = _build_session(tmp_path, rows=rows)
    processor._sync_live_session_events("watch", "session", {"deepseek": {"api_key": "offline"}})

    events = json.loads((directory / "events.json").read_text())
    by_title = {e["title"]: e for e in events}
    assert by_title["哼唱情歌片段"]["review_status"] == "unreviewed"
    assert by_title["健身：卧推训练"]["review_status"] == "reviewed"
    # 两条都留下来了，只是分流
    assert len(by_title) == 2
