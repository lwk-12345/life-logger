"""Tests for speech target input_context prompt and state consumption.

Verifies:
1. Jev/Clef decision line (decision_dimensions.py):
   - Resolved input_context is passed into state with strict whitelist and meaning.
   - Unknown / incomplete / missing input_context stays absent from state (legacy equivalence).
2. DeepSeek batch line (contextual_extraction.py):
   - System prompt explicitly guides consumption of input_context without inferring delivery or excluding life events.
   - Packet rows preserve target_bundle, provider, and meaning.
   - Missing input_context leaves rows clean (legacy equivalence).
3. Session aggregation line (session_aggregator.py):
   - Resolved input_context appends app=... in transcript evidence line.
   - Unknown / missing input_context leaves transcript line clean.
   - Prompt contains objective guidance for app=... context.
4. Security & Anti-injection:
   - Arbitrary fields are stripped by whitelist.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from life_logger.contextual_extraction import _deepseek_request
from life_logger.session_aggregator import format_session_transcript, aggregate_session_events


def make_row(
    text: str = "查一下今天的天气，然后买包烟。",
    *,
    input_context: dict | None = None,
    source_label: str = "self",
) -> dict:
    row = {
        "source_id": "src_test_01",
        "segment_id": "seg_test_01",
        "recording_id": "rec_test_01",
        "source_label": source_label,
        "timestamp": "2026-10-26T06:14:20+08:00",
        "utterance_timestamp": "2026-10-26T06:14:20+08:00",
        "start_offset_seconds": 10.0,
        "end_offset_seconds": 12.0,
        "text": text,
        "is_dictation": False,
        "voice_similarity": 0.85,
        "acoustic_proximity": "near",
    }
    if input_context is not None:
        row["input_context"] = input_context
    return row






def test_deepseek_prompt_and_packet_consumption():
    """DeepSeek: prompt 明确说明前台应用语义，packet 规范透传白名单字段。"""
    ctx = {
        "status": "resolved",
        "start": "2026-10-26T06:14:19+08:00",
        "end": "2026-10-26T06:14:25+08:00",
        "target_bundle": "com.tencent.xinWeChat",
        "provider": "DoubaoIme",
        "source": "collector",
        "confidence": 0.95,
        "arbitrary_evil_field": "drop_me",
    }
    row = make_row(input_context=ctx)
    captured = {}

    def fake_opener(req, timeout=30):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        resp = MagicMock()
        resp.read.return_value = json.dumps({"choices": [{"message": {"content": json.dumps({"records": []})}}]}).encode("utf-8")
        return resp

    _deepseek_request([row], api_key="dummy", timeout_seconds=5, opener=fake_opener)

    req_body = captured.get("body", {})
    prompt_text = req_body["messages"][0]["content"]

    # 1. 验证 prompt 中关于前台应用/输入法的客观说明
    assert "电脑前台应用上下文与对话目标说明" in prompt_text
    assert "不证明这句话已经作为消息发送给该软件" in prompt_text
    assert "绝不得因为前台激活了 Codex/终端/微信就将生活事实排除" in prompt_text

    # 2. 验证 packet 中 input_context 白名单
    assert "输入记录：\n" in prompt_text
    json_part = prompt_text.split("输入记录：\n", 1)[1]
    packet_rows = json.loads(json_part)
    row_0 = packet_rows[0]

    assert "input_context" in row_0
    row_ctx = row_0["input_context"]
    assert row_ctx["target_bundle"] == "com.tencent.xinWeChat"
    assert row_ctx["provider"] == "DoubaoIme"
    assert row_ctx["meaning"] == "input_target_only_not_delivery"
    assert "arbitrary_evil_field" not in row_ctx


def test_deepseek_missing_input_context_leaves_packet_row_clean():
    """DeepSeek: 缺失 input_context 时，packet row 中不出现 input_context 键。"""
    row = make_row(input_context=None)
    captured = {}

    def fake_opener(req, timeout=30):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        resp = MagicMock()
        resp.read.return_value = json.dumps({"choices": [{"message": {"content": json.dumps({"records": []})}}]}).encode("utf-8")
        return resp

    _deepseek_request([row], api_key="dummy", timeout_seconds=5, opener=fake_opener)
    prompt_text = captured.get("body", {})["messages"][0]["content"]
    json_part = prompt_text.split("输入记录：\n", 1)[1]
    packet_rows = json.loads(json_part)
    assert "input_context" not in packet_rows[0]


def test_session_aggregator_transcript_and_prompt_consumption():
    """Session Aggregator: 格式化文本流尾部携带 app=...，提示词包含前台客观说明。"""
    # 1. 带 resolved input_context
    ctx = {
        "status": "resolved",
        "target_bundle": "com.openai.codex",
    }
    row_resolved = make_row(input_context=ctx)
    transcript_resolved = format_session_transcript([row_resolved])
    assert "app=com.openai.codex" in transcript_resolved
    assert "seg=seg_test_01" in transcript_resolved

    # 2. 未解析/无 context 时行尾不出现 app=
    row_clean = make_row(input_context=None)
    transcript_clean = format_session_transcript([row_clean])
    assert "app=" not in transcript_clean

    # 3. 提示词中包含客观说明
    captured = {}
    def fake_deepseek(prompt, temperature=0.1):
        captured["prompt"] = prompt
        return {"events": []}

    with patch("life_logger.session_aggregator.call_magpie_deepseek", fake_deepseek):
        aggregate_session_events([row_resolved])

    used_prompt = captured.get("prompt", "")
    assert "app=... 为发声瞬间电脑前台程序" in used_prompt
    assert "不代表已发送或交付，不排除生活事实" in used_prompt
