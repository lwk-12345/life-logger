"""事件抽取提示词：无 0.90 区间、无一次性反例、有 unknown、无 per-100g 乘除要求。"""
from __future__ import annotations

from pathlib import Path

import pytest

from life_logger import contextual_extraction as cx

SOURCE = Path(cx.__file__).read_text(encoding="utf-8")
START = '"从完整的中文转写上下文中提取明确的个人生活事件。\\n"'


def prompt_text() -> str:
    """把源码里 `prompt = (...)` 那段字面量切出来（不含同文件里的过滤词表/测试夹具）。"""
    start = SOURCE.index(START)
    # prompt = ( ... ) 的闭合：从 START 往后第一个「换行+4空格+右括号」
    open_at = SOURCE.rindex("    prompt = (", 0, start)
    end = SOURCE.index("\n    )\n", start)
    assert open_at < start < end
    return SOURCE[open_at:end]


def test_prompt_slice_is_reasonable():
    text = prompt_text()
    assert 3000 < len(text) < 30000, len(text)
    assert "category" in text and "event_state" in text


@pytest.mark.parametrize("phrase", ["0.90", "跳跳乐", "头甲枪", "SenseVoice", "单声道", "双声道"])
def test_prompt_has_no_one_off_examples_or_confidence_range(phrase):
    assert phrase not in prompt_text(), f"提示词仍有：{phrase}"


@pytest.mark.parametrize("phrase", ["per-100g", "配料优先铁律", "estimated_calories", "estimate_basis"])
def test_prompt_does_not_ask_model_to_do_nutrition_math(phrase):
    assert phrase not in prompt_text(), f"提示词仍在要求模型算营养：{phrase}"


def test_prompt_keeps_food_fields_only():
    text = prompt_text()
    assert "食物名" in text and "weight_g" in text and "meal" in text
    assert "不要自己乘除" in text


def test_prompt_lists_unknown_as_allowed_state():
    text = prompt_text()
    assert "unknown" in text
    for value in ("completed", "ongoing", "planned", "idea", "hypothetical", "negated", "question", "not_event"):
        assert value in text, value


def test_prompt_has_generic_work_rule():
    text = prompt_text()
    assert "对编程工具、终端、AI 助手说的话" in text
    assert "不是生活事件" in text


def test_prompt_confidence_is_real_posterior_not_preset_band():
    text = prompt_text()
    assert "confidence 给真实把握" in text
    assert "低分会交给用户确认，不会丢" in text


def test_unknown_state_goes_to_review_not_ledger():
    """unknown 不再被 invalid_event_state 丢掉，也不当成已发生：进待确认。"""
    assert "unknown" in cx._STATES
    event, reason = cx._event_from_model(
        {"category": "expense", "event_state": "unknown", "confidence": 0.9,
         "quote": "红塔山十块", "title": "买烟",
         "data": {"expense": {"amount": 10, "currency": "CNY", "item": "红塔山"}}},
        {"text": "红塔山十块", "source_id": "s1", "segment_id": "seg1",
         "recording_id": "rec1", "source_label": "self"},
    )
    # 返回事件且带原因 → 调用方放进 pending_events（待确认），不进 formal_events
    assert event is not None
    assert reason and reason != "invalid_event_state"
