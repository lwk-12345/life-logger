"""前台是工作软件时，语音口令词不触发（2026-11-06 用户规则）。

规则：说话那一刻 Mac 前台是工作软件（Alma / 终端 / Codex / Xcode / Antigravity）时，
查询 / 备忘 / 待办增删完成这些**口令词**不触发，按普通工作对话处理。
手表「捏两下」(double_pinch) 60 秒窗口内的显式指令照常触发。
**判不出来（没有时间 / 时间没时区 / 那一刻没窗口）一律当未知 → 照常触发。**
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from life_logger.extract import _is_actionable_todo
from life_logger.voice_triggers import scan_voice_triggers
from life_logger.work_apps import (
    SUPPRESSED_REASON,
    WORK_APP_BUNDLES,
    is_work_app_bundle,
    normalize_context,
    should_suppress,
)

DAY = "2026-11-06"


def _resolved(bundle: str) -> dict:
    """两种历史形态里 watch 管线用的那种（status=resolved）。"""
    return {"status": "resolved", "target_bundle": bundle, "confidence": "high"}


def _appctx(bundle: str) -> dict:
    """app_context.context_at() 的形态。"""
    return {"app_label": "terminal", "target_bundle": bundle, "kind": "dictation", "confidence": "high"}


@pytest.fixture
def windows_root(tmp_path: Path) -> Path:
    """造一个 11-06 12:00-12:01 的 mic 窗口，前台是终端。

    返回的是**store root**（`work_apps.front_app_at` 内部再拼 `watch-sessions`），
    与 `output/local-web` 的用法一致。
    """
    root = tmp_path / "watch-sessions"
    day_file = root / "input-context" / "days" / f"{DAY}.jsonl"
    day_file.parent.mkdir(parents=True, exist_ok=True)
    day_file.write_text(json.dumps({
        "started_at": f"{DAY}T12:00:00+08:00",
        "ended_at": f"{DAY}T12:01:00+08:00",
        "target_app": "com.apple.Terminal",
        "target_unknown": False,
        "complete": True,
        "provider": {"source": "mic_event", "pid": 42},
        "provider_pid": 42,
        "front_bundles": ["com.apple.Terminal"],
        "sample_count": 3,
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    return tmp_path


# --- 共享常量：一处定义 -----------------------------------------------------

def test_work_app_bundles_are_the_six_user_confirmed_ones():
    assert WORK_APP_BUNDLES == frozenset({
        "com.yetone.alma", "com.apple.terminal", "com.openai.codex",
        "com.apple.dt.xcode", "com.google.antigravity",
        "com.lbjlaq.antigravity-tools-lite",
    })


def test_bundle_match_is_case_insensitive_and_exact():
    assert is_work_app_bundle("com.apple.Terminal")          # 大小写飘
    assert is_work_app_bundle("com.yetone.alma")
    assert not is_work_app_bundle("com.tencent.xinWeChat")   # 微信不算
    assert not is_work_app_bundle("com.google.Chrome")       # 浏览器不算
    assert not is_work_app_bundle("") and not is_work_app_bundle(None)


# --- 判据 -------------------------------------------------------------------

def test_unresolved_context_is_unknown_and_never_suppresses():
    assert normalize_context({"status": "uncertain", "target_bundle": "com.apple.Terminal"}) is None
    assert should_suppress(context={"status": "uncertain", "target_bundle": "com.apple.Terminal"}) is None
    assert should_suppress(context=None, moment=None) is None


def test_naive_timestamp_is_not_suppressed(windows_root: Path):
    """没有时区的时间不猜时区 → 未知 → 照常触发。"""
    assert should_suppress(context=None, moment="2026-11-06T12:00:00", root=windows_root) is None


def test_double_pinch_within_60s_is_never_suppressed():
    ctx = _appctx("com.apple.Terminal")
    assert should_suppress(context=ctx, physical_pin="double_pinch", pin_age_seconds=15) is None
    # 超出 60 秒窗口就不算显式指令，回到普通规则
    assert should_suppress(context=ctx, physical_pin="double_pinch", pin_age_seconds=75) is not None


def test_should_suppress_reads_windows_when_context_missing(windows_root: Path):
    mark = should_suppress(context=None, moment=f"{DAY}T12:00:30+08:00", root=windows_root)
    assert mark is not None
    assert mark["suppressed_reason"] == SUPPRESSED_REASON == "work_app_front"
    assert mark["target_bundle"] == "com.apple.Terminal"


# --- 入口 1：voice_triggers（即时抓取）--------------------------------------

def test_terminal_front_suppresses_query_trigger():
    assert scan_voice_triggers("检查一下这个页面", timestamp=f"{DAY}T12:00:30+08:00",
                               input_context=_resolved("com.apple.Terminal")) == []


def test_wechat_front_still_triggers():
    captures = scan_voice_triggers("检查一下这个页面", timestamp=f"{DAY}T12:00:30+08:00",
                                   input_context=_resolved("com.tencent.xinWeChat"))
    assert [c.trigger_type for c in captures] == ["query"]


def test_unknown_front_still_triggers():
    """上下文未知（没传）→ 照常触发，绝不因未知而压制。"""
    captures = scan_voice_triggers("检查一下这个页面", timestamp=f"{DAY}T12:00:30+08:00")
    assert [c.trigger_type for c in captures] == ["query"]


def test_double_pinch_inside_window_still_triggers_on_terminal():
    captures = scan_voice_triggers(
        "检查一下这个页面", timestamp=f"{DAY}T12:00:30+08:00",
        input_context=_resolved("com.apple.Terminal"),
        physical_pin="double_pinch", pin_age_seconds=20,
    )
    assert [c.trigger_type for c in captures] == ["query"]


def test_work_app_does_not_suppress_expense():
    """说到钱不折：记账不是口令词，工作软件前台也照常记。"""
    captures = scan_voice_triggers("买了冰杯花了2块钱", timestamp=f"{DAY}T12:00:30+08:00",
                                   input_context=_resolved("com.apple.Terminal"))
    assert [c.trigger_type for c in captures] == ["expense"]


def test_work_app_suppresses_memo_and_todo_commands():
    for text, kind in (("记录一下明天买牛奶", "todo"), ("提醒我交房租", "todo"),
                       ("删除待办 买水", "todo_delete"), ("待办打卡 买水", "todo_done")):
        assert scan_voice_triggers(text, timestamp=f"{DAY}T12:00:30+08:00",
                                   input_context=_resolved("com.apple.Terminal")) == [], text
        assert [c.trigger_type for c in scan_voice_triggers(
            text, timestamp=f"{DAY}T12:00:30+08:00",
            input_context=_resolved("com.tencent.xinWeChat"))] == [kind], text


# --- 入口 2：extract._is_actionable_todo（手表规则抽取）---------------------

def test_actionable_todo_suppressed_on_work_app():
    assert _is_actionable_todo("记个待办 明天买牛奶",
                               input_context=_resolved("com.openai.codex")) is False
    assert _is_actionable_todo("记个待办 明天买牛奶") is True          # 未知 → 照常
    assert _is_actionable_todo("记个待办 明天买牛奶",
                               input_context=_resolved("com.tencent.xinWeChat")) is True


def test_actionable_todo_double_pinch_exempt_even_on_work_app():
    assert _is_actionable_todo(
        "记个待办 明天买牛奶", physical_pin="double_pinch", pin_age_seconds=10,
        input_context=_resolved("com.openai.codex")) is True


def test_actionable_todo_naive_moment_is_not_suppressed(windows_root: Path):
    assert _is_actionable_todo(
        "记个待办 明天买牛奶", moment="2026-11-06T12:00:30",
        context_root=windows_root) is True


# --- 入口 3：contextual_extraction（事件层「查询：/便签：」）---------------

def _model_event(**over) -> dict:
    event = {"category": "todo", "event_state": "completed",
             "quote": "检查一下这个页面", "title": "检查 LiveLog 页面并推荐优化措施",
             "confidence": 0.95}
    event.update(over)
    return event


def _record(context=None, **over) -> dict:
    record = {
        "source_id": "src_1", "segment_id": "seg_1", "recording_id": "rec_1",
        "source_label": "self", "text": "检查一下这个页面",
        "timestamp": f"{DAY}T12:00:30+08:00",
        "absolute_started_at": f"{DAY}T12:00:30+08:00",
        "start_offset_seconds": 0.0, "end_offset_seconds": 2.0,
    }
    if context is not None:
        record["input_context"] = context
    record.update(over)
    return record


def test_query_event_on_work_app_is_not_turned_into_dispatch():
    from life_logger.contextual_extraction import _event_from_model

    event, reason = _event_from_model(_model_event(), _record(_resolved("com.apple.Terminal")))

    assert reason is None
    assert event is not None
    assert event.get("query_dispatch") is not True
    assert not str(event["title"]).startswith("查询：")          # 不追加查询前缀
    assert "todo" not in (event.get("categories") or [])        # 不抬成待办
    assert event["evidence"]["suppressed_reason"] == "work_app_front"
    assert event["evidence"]["suppressed_app_label"] == "Terminal"
    assert event["evidence"]["quote"] == "检查一下这个页面"      # 原话不删


def test_query_event_on_wechat_is_still_a_dispatch():
    from life_logger.contextual_extraction import _event_from_model

    event, reason = _event_from_model(_model_event(), _record(_resolved("com.tencent.xinWeChat")))

    assert reason is None
    assert event.get("query_dispatch") is True
    assert str(event["title"]).startswith("查询：")
    assert "suppressed_reason" not in event["evidence"]


def test_query_event_with_unknown_context_keeps_old_behaviour():
    from life_logger.contextual_extraction import _event_from_model

    event, _ = _event_from_model(_model_event(), _record())

    assert event.get("query_dispatch") is True
    assert "suppressed_reason" not in event["evidence"]


def test_front_app_without_dictation_does_not_suppress() -> None:
    """前台是 Alma 但没有语音输入到它（kind=front_app）时，口令照常触发。"""
    from life_logger.work_apps import should_suppress

    ctx = {"app_label": "Alma", "target_bundle": "com.yetone.alma", "kind": "front_app", "confidence": "high"}
    assert should_suppress(context=ctx) is None
    dictation = dict(ctx, kind="dictation")
    assert should_suppress(context=dictation)["suppressed_reason"] == "work_app_front"
