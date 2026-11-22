"""标题事实接地（2026-10-23）：模型不得在（可能听错的）ASR 文本上补全新实体。

现场反例（会话 AAC382B0…）：
  ASR「六个A进的」→ 模型标题「说明六个A级接入」（原文没有"级"也没有"接入"）
处置：拒绝该标题，退回原文截断，并保持 review_status=unreviewed。
"""
from __future__ import annotations

import pytest

from life_logger.contextual_extraction import (
    ground_title,
    title_grounding_violations,
    title_grounding_warnings,
)


def test_ungrounded_level_and_access_is_rejected():
    """「六个A进的」不得被改写成「六个A级接入」。"""
    violations = title_grounding_violations("说明六个A级接入", "六个A进的。")
    assert violations, "级/接入 原文没有，必须判为不接地"
    title, source, found = ground_title("说明六个A级接入", "六个A进的。")
    assert source == "quote_fallback"
    assert title == "六个A进的。"
    assert "A级" not in title and "接入" not in title
    assert found


def test_grounded_terminal_title_is_kept():
    """「对，六个终端。」→「确认监控的是六个终端」允许（终端原文有）。"""
    title, source, found = ground_title(
        "确认监控的是六个终端", "对，六个终端。", "跟人讨论一个问题，就是我在DevOps上面监控终端里的六个agent的"
    )
    assert found == []
    assert source == "model"
    assert title == "确认监控的是六个终端"


def test_ambiguous_place_vs_terminal_is_not_auto_resolved():
    """原文「我们有六个县城啊」有歧义（现场/终端待定）→ 不许标题替它选一个。"""
    assert title_grounding_violations("确认现场有六个终端", "我们有六个县城啊。")
    title, source, _found = ground_title("确认现场有六个终端", "我们有六个县城啊。")
    assert source == "quote_fallback"
    assert title == "我们有六个县城啊。"
    assert "终端" not in title and "现场" not in title


def test_verbatim_like_title_is_kept():
    assert ground_title("说明有六个县城", "我们有六个县城啊。")[1] == "model"


def test_latin_proper_nouns_must_be_grounded():
    """Agent / Codex 这类专名：原文有就保留，原文没有就不许出现。"""
    assert title_grounding_violations("说明使用Agent完成任务", "今天天气不错。")
    ok, source, found = ground_title(
        "准备交接给Codex并监控终端",
        "准备交接给Codex……你去监控这四个终端，给他们合理派活",
    )
    assert found == [] and source == "model" and "Codex" in ok


def test_count_phrases_must_be_grounded():
    assert title_grounding_violations("说明有六个终端", "我们有一个终端。")
    assert ground_title("确认六个终端", "对，六个终端。")[1] == "model"


@pytest.mark.parametrize("quote,title", [
    ("六个A进的。", "说明六个A级接入"),
    ("我们有六个县城啊。", "确认现场有六个终端"),
])
def test_rejected_titles_fall_back_to_quote_prefix(quote, title):
    final, source, violations = ground_title(title, quote)
    assert violations and source == "quote_fallback"
    assert final == quote.strip()[:240]


# ---------------------------------------------------------------------------
# 接线测试：拒绝后必须真的落到事件上（标题退回原文 + review_status=unreviewed）
# ---------------------------------------------------------------------------
from life_logger.contextual_extraction import _event_from_model  # noqa: E402


def _build(model_event, quote):
    return _event_from_model(
        model_event,
        {
            "source_id": "src_title_probe",
            "recording_id": "rec_title_probe",
            "segment_id": "seg_title_probe",
            "source_label": "self",
            "text": quote,
            "utterance_timestamp": "2026-10-22T22:07:29+08:00",
        },
    )


def test_event_title_falls_back_to_quote_when_ungrounded():
    quote = "六个A进的。"
    event, reason = _build(
        {
            "category": "work_communication",
            "event_state": "completed",
            "claim_type": "event",
            "title": "说明六个A级接入",
            "quote": quote,
            "confidence": 0.9,
            "data": {"work_communication": {"summary": "接入"}},
        },
        quote,
    )
    assert event is not None, reason
    assert event["title"] == quote
    assert event["evidence"]["title_source"] == "quote_fallback"
    assert event["evidence"]["title_grounding"]["status"] == "rejected"
    assert event["review_status"] == "unreviewed"
    assert event["evidence"]["pending_reason"] == "title_not_grounded"


def test_event_title_kept_when_grounded():
    quote = "对，六个终端。"
    event, reason = _build(
        {
            "category": "work_communication",
            "event_state": "completed",
            "claim_type": "event",
            "title": "确认监控的是六个终端",
            "quote": quote,
            "confidence": 0.9,
            "data": {"work_communication": {"summary": "终端"}},
        },
        quote,
    )
    assert event is not None, reason
    assert event["title"] == "确认监控的是六个终端"
    assert event["evidence"]["title_grounding"]["status"] == "accepted"


# ---------------------------------------------------------------------------
# 返工（2026-10-23）：查询类接地、虚词归一、软词降级
# ---------------------------------------------------------------------------
def test_query_title_grounds_against_query_context():
    """查询类：quote 只有「帮我查一下。」，内容在 topic/前文里 → 不得误杀。"""
    quote = "帮我查一下。"
    ctx = "谷歌邮箱GIO订阅链接查询 十八个月"
    title = "查询：谷歌邮箱退出、18个月印度GIO订阅"
    assert title_grounding_violations(title, quote, ctx) == []
    assert ground_title(title, quote, ctx)[1] == "model"


def test_filler_words_are_normalized():
    """「派个活」≈「派活」：虚词不该造成误杀。"""
    assert title_grounding_violations("派个活给Agent并检查进度", "派活给agent", "派活") == []
    assert title_grounding_violations("把窗口关掉", "把窗口关了") == []


def test_generic_terms_are_warnings_not_rejections():
    """平台/架构/系统 这类概括词 → 只警告，不拒绝。"""
    quote = "他在小红书，抖音，推特。"
    title = "运营平台：小红书、抖音、推特"
    assert title_grounding_violations(title, quote) == []
    assert "soft_term:平台" in title_grounding_warnings(title, quote)
    assert ground_title(title, quote)[1] == "model"


def test_real_level_and_access_completion_still_rejected():
    """返工不能把真正该拒的放过：A级接入 仍必须拒绝。"""
    violations = title_grounding_violations("说明六个A级接入", "六个A进的。")
    assert violations
    assert ground_title("说明六个A级接入", "六个A进的。")[1] == "quote_fallback"
