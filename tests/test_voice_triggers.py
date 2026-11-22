"""Unit tests for Voice Triggers (TODO, Expense, Idea, Privacy)."""

from __future__ import annotations

import json
from pathlib import Path
import pytest
from life_logger.voice_triggers import QuickCaptureStore, scan_voice_triggers


def test_scan_voice_triggers_todo() -> None:
    captures = scan_voice_triggers("提醒我：明天下午三点给李总发方案")
    assert len(captures) == 1
    assert captures[0].trigger_type == "todo"
    assert "明天下午三点给李总发方案" in captures[0].extracted_content

    captures2 = scan_voice_triggers("记一下买两包纸巾")
    assert len(captures2) == 1
    assert captures2[0].trigger_type == "todo"
    assert "买两包纸巾" in captures2[0].extracted_content

    captures3 = scan_voice_triggers("记录下购买清单：买一双跑鞋")
    assert len(captures3) == 1
    assert captures3[0].trigger_type == "todo"
    assert "购买清单" in captures3[0].extracted_content


def test_scan_voice_triggers_query() -> None:
    captures = scan_voice_triggers("我要查一下这个VPN怎么设置")
    assert len(captures) == 1
    assert captures[0].trigger_type == "query"
    assert "这个VPN怎么设置" in captures[0].extracted_content

    captures2 = scan_voice_triggers("检查一下我的OpenCloud")
    assert len(captures2) == 1
    assert captures2[0].trigger_type == "query"
    assert "我的OpenCloud" in captures2[0].extracted_content


def test_scan_voice_triggers_expense() -> None:
    captures = scan_voice_triggers("刚才买了一杯拿铁花了28.5元")
    assert len(captures) == 1
    assert captures[0].trigger_type == "expense"
    assert captures[0].metadata["amount"] == 28.5
    assert "28.50" in captures[0].extracted_content

    captures2 = scan_voice_triggers("付了车费15块")
    assert len(captures2) == 1
    assert captures2[0].trigger_type == "expense"
    assert captures2[0].metadata["amount"] == 15.0


def test_scan_voice_triggers_idea() -> None:
    captures = scan_voice_triggers("有个点子：把手表心率与录音做时间对齐")
    assert len(captures) == 1
    assert captures[0].trigger_type == "idea"
    assert "把手表心率与录音做时间对齐" in captures[0].extracted_content


def test_scan_voice_triggers_privacy() -> None:
    captures = scan_voice_triggers("刚才这段密码别记了，这条别记")
    assert len(captures) == 1
    assert captures[0].trigger_type == "privacy_mute"
    assert captures[0].metadata["action"] == "discard_segment"


def test_quick_capture_store(tmp_path: Path) -> None:
    store_file = tmp_path / "captures.json"
    store = QuickCaptureStore(store_file)
    assert store.list_all() == []

    c = scan_voice_triggers("记一下：买牛奶", timestamp="2026-10-11T09:00:00")
    store.append(c)

    all_items = store.list_all()
    assert len(all_items) == 1
    assert all_items[0]["trigger_type"] == "todo"

    today_items = store.filter_by_date("2026-10-11")
    assert len(today_items) == 1

    other_items = store.filter_by_date("2026-10-12")
    assert len(other_items) == 0


def test_build_pinned_memos_and_hallucination_guard() -> None:
    from life_logger.daily_cognitive_session import build_pinned_memos
    from life_logger.watch_formal_asr import is_hallucinated_asr_noise

    assert is_hallucinated_asr_noise("3,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0")
    assert is_hallucinated_asr_noise("t.T.A.A.A.A.A.A.A.A.A.A.A.A.A.A.A.A.A.A")
    assert not is_hallucinated_asr_noise("记录下购买清单。买一双跑鞋。")
    assert not is_hallucinated_asr_noise("检查一下我的VPN设置，我想让悠悠远程走本地直连。")

    events = [
        {
            "event_id": "ev-memo-1",
            "title": "便签：购买清单：买一双跑鞋、买个飞机杯",
            "categories": ["todo"],
            "pinned_memo": True,
            "time": {"absolute": "2026-10-11T14:50:20Z"},
            "evidence": {
                "quote": "记录下购买清单。买一双跑鞋。再买个什么呢?买个飞机杯。",
                "reported_at": "2026-10-11T14:50:20Z",
                "pinned_memo": True,
            },
        },
        {
            "event_id": "ev-query-1",
            "title": "查询：检查一下我的VPN设置",
            "categories": ["todo"],
            "query_dispatch": True,
            "time": {"absolute": "2026-10-11T12:24:51Z"},
            "evidence": {
                "quote": "检查一下我的VPN设置,我想让这个悠悠远程走本地直连。",
                "reported_at": "2026-10-11T12:24:51Z",
                "query_dispatch": True,
            },
        },
    ]
    memos = build_pinned_memos(events)
    assert len(memos) == 2
    assert memos[0]["kind"] == "memo"
    assert "买一双跑鞋" in memos[0]["text"]
    assert memos[1]["kind"] == "query"
    assert "VPN设置" in memos[1]["text"]


def test_unified_voice_lexicon_between_contextual_extraction_and_cognitive_session() -> None:
    from life_logger.daily_cognitive_session import build_pinned_memos
    from life_logger.voice_lexicon import MEMO_TRIGGER_TERMS, QUERY_TRIGGER_TERMS

    assert "记下" in MEMO_TRIGGER_TERMS
    assert "记下来" in MEMO_TRIGGER_TERMS
    assert "记个备忘" in MEMO_TRIGGER_TERMS
    assert "搜一下" in QUERY_TRIGGER_TERMS
    assert "搜索一下" in QUERY_TRIGGER_TERMS
    assert "查一查" in QUERY_TRIGGER_TERMS
    assert "查查" in QUERY_TRIGGER_TERMS

    events = [
        {
            "event_id": "ev-memo-jixia",
            "title": "买牛奶",
            "categories": ["todo"],
            "time": {"absolute": "2026-10-12T09:00:00Z"},
            "evidence": {
                "quote": "记下明天买牛奶",
                "reported_at": "2026-10-12T09:00:00Z",
            },
        },
        {
            "event_id": "ev-memo-jixialai",
            "title": "买咖啡豆",
            "categories": ["todo"],
            "time": {"absolute": "2026-10-12T09:05:00Z"},
            "evidence": {
                "quote": "记下来买咖啡豆",
                "reported_at": "2026-10-12T09:05:00Z",
            },
        },
        {
            "event_id": "ev-query-souyixia",
            "title": "路由器配置",
            "categories": ["todo"],
            "time": {"absolute": "2026-10-12T09:10:00Z"},
            "evidence": {
                "quote": "帮我搜一下路由器配置",
                "reported_at": "2026-10-12T09:10:00Z",
            },
        },
    ]
    memos = build_pinned_memos(events)
    assert [m["kind"] for m in memos] == ["memo", "memo", "query"]
    assert memos[0]["text"] == "明天买牛奶"
    assert memos[1]["text"] == "买咖啡豆"
    assert memos[2]["text"] == "路由器配置"


def test_run_today_session_pipeline_capture_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    from life_logger.domain import TranscriptSegment
    from scripts import run_today_session_pipeline

    session_dir = tmp_path / "session"
    formal_dir = session_dir / "formal-asr"
    segments_dir = session_dir / "segments"
    segments_dir.mkdir(parents=True)
    (segments_dir / "seg-000001.m4a").write_bytes(b"fake-audio")
    (session_dir / "manifest.json").write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "sequence": 1,
                        "file": "seg-000001.m4a",
                        "received_at": "2026-10-12T10:00:00+08:00",
                        "duration_ms": 5000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    class _FakeTranscriber:
        def transcribe(self, rec):
            return [
                TranscriptSegment(
                    segment_id="seg_1",
                    recording_id=rec.recording_id,
                    start_offset_seconds=0.0,
                    end_offset_seconds=5.0,
                    absolute_started_at="2026-10-12T10:00:00+08:00",
                    absolute_ended_at="2026-10-12T10:00:05+08:00",
                    text="记一下：明天买咖啡豆",
                    source="fake",
                    language="zh",
                    transcription_confidence=0.95,
                )
            ]

    monkeypatch.setattr(run_today_session_pipeline, "SESSION_DIR", session_dir)
    monkeypatch.setattr(run_today_session_pipeline, "FORMAL_DIR", formal_dir)
    monkeypatch.setattr(run_today_session_pipeline, "SEGMENTS_DIR", segments_dir)
    monkeypatch.setattr(
        run_today_session_pipeline,
        "LocalTurboCompositeTranscriber",
        _FakeTranscriber,
    )

    run_today_session_pipeline.main()

    captures = json.loads(
        (session_dir / "quick_captures.json").read_text(encoding="utf-8")
    )
    assert len(captures) == 1
    assert captures[0]["trigger_type"] == "todo"
    assert captures[0]["raw_text"] == "记一下：明天买咖啡豆"
    assert "明天买咖啡豆" in captures[0]["extracted_content"]




# ---------------------------------------------------------------------------
# 2026-10-17 事故回归：电视台词变成待办卡片 + 碎片口令
# ---------------------------------------------------------------------------
def test_far_speech_from_unknown_speaker_never_becomes_a_capture() -> None:
    """背景影音（电视剧台词）不能生成即时抓取卡片。

    实测事故：看板上出现「别忘了，」「那什么事都提前算在时间，大明呢，大明最后怎么样了」
    这两张「即时抓取·待办 pending」卡，而源行的真实属性是
    source_label=unknown / acoustic_proximity=far / voice_similarity=0.366 ——
    是电视里的人在说话，不是用户。
    """
    drama_line = "每个人下一步做什么，我都知道，别忘了，那什么事都提前算在时间，大明呢，大明最后怎么样了，"
    captures = scan_voice_triggers(
        drama_line,
        source_label="unknown",
        acoustic_proximity="far",
        voice_similarity=0.366,
    )
    assert captures == []


def test_non_self_speaker_never_becomes_a_capture() -> None:
    assert scan_voice_triggers(
        "记一下：明天买咖啡豆",
        source_label="non_self",
        acoustic_proximity="near",
    ) == []


def test_trusted_self_voice_still_produces_capture() -> None:
    """本人近场说话必须照常抓取 —— 门槛不能把真实口令一起挡掉。"""
    captures = scan_voice_triggers(
        "记一下：明天买咖啡豆",
        source_label="self",
        acoustic_proximity="near",
        voice_similarity=0.72,
    )
    assert [c.trigger_type for c in captures] == ["todo"]
    assert "明天买咖啡豆" in captures[0].extracted_content


def test_capture_without_identity_context_keeps_legacy_behaviour() -> None:
    """没传身份信息（旧脚本 / 纯文本调用）时保持原行为，不误伤。"""
    captures = scan_voice_triggers("记一下：明天买咖啡豆")
    assert [c.trigger_type for c in captures] == ["todo"]


def test_fragment_only_captures_are_dropped() -> None:
    """空串 / 单字语气词 / 悬空指代不能变成卡片。

    实测垃圾卡：「别忘了，」→ 空串；「查一下吧」→「吧」；「你帮我查一下他的」→「他的」。
    """
    for raw in ("别忘了，", "我的文件夹里面有，查一下吧。", "你帮我查一下他的。", "然后我还有一个想法，就是。"):
        assert scan_voice_triggers(
            raw, source_label="self", acoustic_proximity="near"
        ) == [], raw


def test_short_but_meaningful_proper_noun_survives() -> None:
    """3 字专名要能过闸（「虾牛马」曾被误杀，是真实查询目标）。"""
    captures = scan_voice_triggers(
        "我查一下虾牛马。", source_label="self", acoustic_proximity="near"
    )
    assert [c.trigger_type for c in captures] == ["query"]
    assert captures[0].extracted_content == "虾牛马"


def test_quick_capture_store_filters_by_local_date_not_utc_prefix(tmp_path) -> None:
    """凌晨的即时抓取属于本地当天，不能被 UTC 前缀挂到前一天。

    captured_at=2026-10-16T16:30:00+00:00 是北京时间 10-17 00:30。
    旧实现先 `ts.startswith(date_str)` 命中 10-16，直接归档到前一天。
    """
    from life_logger.voice_triggers import QuickCaptureStore

    path = tmp_path / "quick_captures.json"
    path.write_text(
        json.dumps(
            [
                {"trigger_type": "todo", "extracted_content": "凌晨的待办",
                 "captured_at": "2026-10-16T16:30:00+00:00"},
                {"trigger_type": "todo", "extracted_content": "前一天的待办",
                 "captured_at": "2026-10-16T05:00:00+00:00"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = QuickCaptureStore(path)

    assert [c["extracted_content"] for c in store.filter_by_date("2026-10-17")] == ["凌晨的待办"]
    assert [c["extracted_content"] for c in store.filter_by_date("2026-10-16")] == ["前一天的待办"]
