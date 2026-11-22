"""输入上下文语音输入程序（provider）证据保留与焦点语义分离测试。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.input_context import (
    resolve_input_context,
)

REC_START = "2026-10-25T18:06:09+08:00"
REC_END = "2026-10-25T18:06:11+08:00"


def test_real_collector_schema_retains_full_provider_evidence():
    """真实采集器输出（包含顶层 provider 与 window 内结构化 provider）接入 resolver 不丢字段。"""
    collector_payload = {
        "schema_version": "mac-input-context-v1",
        "provider": {
            "pid": "79304",
            "process": "/Library/Input Methods/DoubaoIme.app/Contents/MacOS/DoubaoIme",
            "source": "argument"
        },
        "windows": [
            {
                "started_at": "2026-10-25T18:06:00+08:00",
                "ended_at": "2026-10-25T18:06:20+08:00",
                "duration_seconds": 20.0,
                "provider": {
                    "pid": "79304",
                    "process": "DoubaoIme",
                    "source": "argument"
                },
                "provider_pid": "79304",
                "target_app": "com.openai.codex",
                "target_unknown": False,
                "complete": True,
                "front_bundles": ["com.openai.codex"],
                "reason": "single_front_app"
            }
        ]
    }

    out = resolve_input_context(
        record_start=REC_START,
        record_end=REC_END,
        windows=collector_payload
    )

    assert out["status"] == "resolved"
    # 核心断言：provider 证据完整保留且结构化
    assert out["provider"] == {
        "pid": "79304",
        "process": "DoubaoIme",
        "source": "argument"
    }
    # 核心断言：provider 与前台 target 彻底分离
    assert out["target_bundle"] == "com.openai.codex"
    assert out["meaning"] == "input_target_only_not_delivery", "绝不能等同于已发送或接收者"


def test_fallback_when_window_only_has_provider_pid():
    """兼容旧 schema：若 window 内仅有 provider_pid，从顶层或 PID 回退恢复 provider。"""
    payload_with_root = {
        "provider": {
            "pid": "79304",
            "process": "DoubaoIme",
            "source": "mic_event"
        },
        "windows": [
            {
                "started_at": "2026-10-25T18:06:00+08:00",
                "ended_at": "2026-10-25T18:06:20+08:00",
                "provider_pid": "79304",
                "target_app": "com.tencent.xinWeChat",
                "target_unknown": False,
                "complete": True
            }
        ]
    }

    out = resolve_input_context(
        record_start=REC_START,
        record_end=REC_END,
        windows=payload_with_root
    )
    assert out["status"] == "resolved"
    assert out["provider"] == {
        "pid": "79304",
        "process": "DoubaoIme",
        "source": "mic_event"
    }
    assert out["target_bundle"] == "com.tencent.xinWeChat"

    # 仅裸 window 数组且仅带 provider_pid
    bare_windows = [
        {
            "start": "2026-10-25T18:06:00+08:00",
            "end": "2026-10-25T18:06:20+08:00",
            "provider_pid": "88888",
            "target_bundle": "com.apple.finder"
        }
    ]
    out_bare = resolve_input_context(
        record_start=REC_START,
        record_end=REC_END,
        windows=bare_windows
    )
    assert out_bare["status"] == "resolved"
    assert out_bare["provider"] == {
        "pid": "88888",
        "process": None,
        "source": None
    }


def test_target_unknown_when_not_verified_no_keyword_exclusion():
    """未证实绑定时必须返回 target_unknown，绝不按 Codex 关键词强行推断或排除。"""
    window_unknown = {
        "started_at": "2026-10-25T18:06:00+08:00",
        "ended_at": "2026-10-25T18:06:20+08:00",
        "target_app": None,
        "target_unknown": True,
        "front_bundles": ["com.openai.codex", "com.apple.finder"],
        "reason": "front_changed_during_recording"
    }
    out = resolve_input_context(
        record_start=REC_START,
        record_end=REC_END,
        windows={"windows": [window_unknown]}
    )
    assert out["status"] == "unknown"
    assert out["reason"] == "target_unknown"
    assert "target_bundle" not in out


def test_no_signal_equivalence_on_empty_windows():
    """空窗口或无信号时保持旧路等价。"""
    assert resolve_input_context(record_start=REC_START, record_end=REC_END, windows=[]) == {
        "schema": "input-context-v1",
        "status": "unknown",
        "reason": "no_signal"
    }
    assert resolve_input_context(record_start=REC_START, record_end=REC_END, windows=None) == {
        "schema": "input-context-v1",
        "status": "unknown",
        "reason": "no_signal"
    }


def test_timezone_and_offset_preserved_without_rewrite():
    """原始时区与时间偏移完整保留，不改写。"""
    raw_start = "2026-10-25T18:06:00.123+08:00"
    raw_end = "2026-10-25T18:06:20.456+08:00"
    window = {
        "started_at": raw_start,
        "ended_at": raw_end,
        "target_app": "com.openai.codex",
        "provider": {"pid": "79304", "process": "DoubaoIme", "source": "argument"},
        "complete": True
    }
    out = resolve_input_context(
        record_start="2026-10-25T18:06:10+08:00",
        record_end="2026-10-25T18:06:15+08:00",
        windows=[window]
    )
    assert out["status"] == "resolved"
    assert out["start"] == raw_start
    assert out["end"] == raw_end
