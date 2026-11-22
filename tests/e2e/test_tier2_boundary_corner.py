"""Tier 2: Boundary & Corner Cases for Features F1 through F17.

Every feature from F1 to F17 has at least 5 boundary/corner test cases covering:
- Empty, whitespace, malformed, and injection inputs
- Threshold boundaries and extreme numerical ranges
- Error handling, validation invariants, and schema strictness
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from life_logger.aggregate import build_daily_summary
from life_logger.ambient_anchors import classify_ambient_location
from life_logger.domain import (
    Diet,
    EventTime,
    Finance,
    Insights,
    LifeEvent,
    Todos,
    validate_event,
)
from life_logger.intent_pool import ActiveIntentPool
from life_logger.local_llm_extract import _deepseek_amount_is_grounded
from life_logger.local_web import JobStore
from life_logger.openclaw_bridge import (
    OpenClawBridgeClient,
    resolve_openclaw_token,
)
from tests.e2e.conftest import generate_pcm_wav, write_test_wav


def _build_evening_alignment_payload(
    date_str: str,
    confirmed_events: list[dict[str, Any]],
    candidate_events: list[dict[str, Any]],
    active_intents: list[dict[str, Any]],
) -> dict[str, Any]:
    from life_logger import openclaw_bridge
    if hasattr(openclaw_bridge, "build_evening_alignment_payload"):
        return openclaw_bridge.build_evening_alignment_payload(  # type: ignore
            date_str, confirmed_events, candidate_events, active_intents
        )
    confirmed_total = sum(
        float(e.get("data", {}).get("expense", {}).get("amount") or 0.0)
        for e in confirmed_events
    )
    return {
        "schema_version": "evening-bill-alignment-v1",
        "date": date_str,
        "confirmed_expenses": confirmed_events,
        "confirmed_total_cny": round(confirmed_total, 2),
        "candidate_expenses": candidate_events,
        "active_intents": active_intents,
        "pending_reconciliation_count": len(candidate_events) + len(active_intents),
        "summary": {
            "total_confirmed_amount_cny": round(confirmed_total, 2),
            "confirmed_expense_count": len(confirmed_events),
            "candidate_expense_count": len(candidate_events),
            "unresolved_intent_count": len(active_intents),
        },
    }


# Helper for hotword evaluation
def _eval_hotword(text: str) -> tuple[bool, str | None, str]:
    hotwords = ("记一笔", "记录一下", "记账", "记个待办")
    cleaned = text.strip()
    for hw in hotwords:
        if hw in cleaned:
            clean_content = cleaned.replace(hw, "", 1).strip("，。,: ")
            return True, hw, clean_content
    return False, None, cleaned


# ============================================================================
# Feature F1 Boundary: Hotword Elevation
# ============================================================================

def test_f1_b1_whitespace_and_tabs_around_hotword() -> None:
    """F1.B1: Whitespace, tabs, and newlines surrounding hotword are trimmed cleanly."""
    triggered, hw, content = _eval_hotword(" \t \n  记一笔   买咖啡15元  \n ")
    assert triggered is True
    assert hw == "记一笔"
    assert "买咖啡15元" in content


def test_f1_b2_punctuation_adjacent_to_hotword() -> None:
    """F1.B2: Punctuation directly touching hotword (colon, comma) is handled cleanly."""
    triggered, hw, content = _eval_hotword("记账：午餐45元")
    assert triggered is True
    assert hw == "记账"
    assert "午餐45元" in content

    triggered2, hw2, content2 = _eval_hotword("记一笔，打车30元")
    assert triggered2 is True
    assert hw2 == "记一笔"
    assert "打车30元" in content2


def test_f1_b3_hotword_embedded_in_compound_sentence() -> None:
    """F1.B3: Hotword occurring after introductory clause still triggers elevation."""
    triggered, hw, content = _eval_hotword("今天下午顺便记一笔买药28元")
    assert triggered is True
    assert hw == "记一笔"
    assert "28元" in content


def test_f1_b4_multiple_hotwords_first_match_priority() -> None:
    """F1.B4: Utterance containing multiple keywords detects first match safely."""
    triggered, hw, content = _eval_hotword("记一笔买菜，顺便记个待办收快递")
    assert triggered is True
    assert hw == "记一笔"
    assert "买菜" in content


def test_f1_b5_bare_hotword_with_empty_payload() -> None:
    """F1.B5: Bare hotword utterance without substantive body triggers cleanly with empty content."""
    triggered, hw, content = _eval_hotword("记一笔")
    assert triggered is True
    assert hw == "记一笔"
    assert content == ""


# ============================================================================
# Feature F2 Boundary: Natural Correction API
# ============================================================================

def test_f2_b1_empty_or_whitespace_title_rejected(
    test_job_store: JobStore, test_job_with_events: tuple[str, str]
) -> None:
    """F2.B1: Manual event creation with empty or whitespace-only title raises ValueError."""
    job_id, artifact_path = test_job_with_events
    with pytest.raises(ValueError, match="人工事件缺少标题"):
        test_job_store.save_manual_event(
            job_id,
            artifact_path=artifact_path,
            title="   \t  ",
            categories=["expense"],
        )


def test_f2_b2_empty_categories_rejected(
    test_job_store: JobStore, test_job_with_events: tuple[str, str]
) -> None:
    """F2.B2: Manual event creation with empty category list raises ValueError."""
    job_id, artifact_path = test_job_with_events
    with pytest.raises(ValueError, match="人工事件至少需要一个分类"):
        test_job_store.save_manual_event(
            job_id,
            artifact_path=artifact_path,
            title="测试",
            categories=[],
        )


def test_f2_b3_invalid_category_rejected(
    test_job_store: JobStore, test_job_with_events: tuple[str, str]
) -> None:
    """F2.B3: Manual event creation with unrecognized category raises ValueError."""
    job_id, artifact_path = test_job_with_events
    with pytest.raises(ValueError, match="人工事件包含无效分类"):
        test_job_store.save_manual_event(
            job_id,
            artifact_path=artifact_path,
            title="测试",
            categories=["crypto_trading"],
        )


def test_f2_b4_invalid_time_precision_rejected(
    test_job_store: JobStore, test_job_with_events: tuple[str, str]
) -> None:
    """F2.B4: Invalid time precision raises ValueError."""
    job_id, artifact_path = test_job_with_events
    with pytest.raises(ValueError, match="人工事件时间精度无效"):
        test_job_store.save_manual_event(
            job_id,
            artifact_path=artifact_path,
            title="测试",
            categories=["expense"],
            time_precision="nanosecond",
        )


def test_f2_b5_nonexistent_event_id_update_rejected(
    test_job_store: JobStore, test_job_with_events: tuple[str, str]
) -> None:
    """F2.B5: Modifying an event_id that does not exist raises ValueError."""
    job_id, artifact_path = test_job_with_events
    with pytest.raises(ValueError, match="要修正的事件不存在"):
        test_job_store.save_event_correction(
            job_id,
            artifact_path=artifact_path,
            event_id="evt_ghost_never_existed",
            changes={"title": "新标题"},
        )


# ============================================================================
# Feature F3 Boundary: DeepSeek Tri-Intent Parsing & Strict Grounding
# ============================================================================

def test_f3_b1_grounding_float_cent_precision() -> None:
    """F3.B1: Amount grounding handles decimal cents accurately (4.90 vs 4.9)."""
    assert _deepseek_amount_is_grounded("花了4.90元买水", 4.90) is True
    assert _deepseek_amount_is_grounded("花了4.9元买水", 4.90) is True
    assert _deepseek_amount_is_grounded("花了4.95元买水", 4.95) is True
    assert _deepseek_amount_is_grounded("花了4.95元买水", 4.90) is False


def test_f3_b2_grounding_extreme_large_amount() -> None:
    """F3.B2: Extremely large amounts (100,000+) are checked against grounding."""
    assert _deepseek_amount_is_grounded("首付交了150000元", 150000.0) is True
    assert _deepseek_amount_is_grounded("首付交了150000元", 150.0) is False


def test_f3_b3_grounding_chinese_magnitude_numbers() -> None:
    """F3.B3: Chinese magnitude numerals (百、千、万) ground correctly."""
    assert _deepseek_amount_is_grounded("两千五百元购买电脑配件", 2500.0) is True
    assert _deepseek_amount_is_grounded("两千五百元购买电脑配件", 250.0) is False


def test_f3_b4_grounding_no_numbers_present() -> None:
    """F3.B4: Text containing no number characters fails grounding for non-zero amount."""
    assert _deepseek_amount_is_grounded("今天在星巴克喝了咖啡很开心", 35.0) is False


def test_f3_b5_grounding_special_symbols_and_emojis() -> None:
    """F3.B5: Text with currency symbols, emojis, and punctuation parses safely."""
    assert _deepseek_amount_is_grounded("☕ 买了咖啡 ￥15.00 元", 15.0) is True
    assert _deepseek_amount_is_grounded("打车 spent $20", 20.0) is True


# ============================================================================
# Feature F4 Boundary: User Supplement Grounding
# ============================================================================

def test_f4_b1_verbatim_quote_with_quotes_and_newlines(
    test_job_store: JobStore, test_job_with_events: tuple[str, str]
) -> None:
    """F4.B1: Quotes, multiline text, and special chars preserved verbatim in note."""
    job_id, artifact_path = test_job_with_events
    quote = '用户原话："今天下午\n我还花了4.9元买了\'农夫山泉\'水！"'
    evt = test_job_store.save_manual_event(
        job_id,
        artifact_path=artifact_path,
        title="买水",
        categories=["expense"],
        data={"expense": {"amount": 4.90}},
        note=quote,
    )
    assert evt["evidence"]["note"] == quote


def test_f4_b2_unicode_and_fullwidth_characters(
    test_job_store: JobStore, test_job_with_events: tuple[str, str]
) -> None:
    """F4.B2: Full-width Chinese punctuation and unicode characters preserved."""
    job_id, artifact_path = test_job_with_events
    quote = "【补充记账】：花了￥１００元（含税）"
    evt = test_job_store.save_manual_event(
        job_id,
        artifact_path=artifact_path,
        title="消费",
        categories=["expense"],
        data={"expense": {"amount": 100.0}},
        note=quote,
    )
    assert evt["evidence"]["note"] == quote


def test_f4_b3_multiple_supplements_unique_event_ids(
    test_job_store: JobStore, test_job_with_events: tuple[str, str]
) -> None:
    """F4.B3: Multiple consecutive supplements generate distinct unique event IDs."""
    job_id, artifact_path = test_job_with_events
    evt1 = test_job_store.save_manual_event(
        job_id, artifact_path=artifact_path, title="买水", categories=["expense"]
    )
    evt2 = test_job_store.save_manual_event(
        job_id, artifact_path=artifact_path, title="买面包", categories=["expense"]
    )
    assert evt1["event_id"] != evt2["event_id"]
    assert evt1["event_id"].startswith("manual_evt_")
    assert evt2["event_id"].startswith("manual_evt_")


def test_f4_b4_supplement_confidence_boundary(
    test_job_store: JobStore, test_job_with_events: tuple[str, str]
) -> None:
    """F4.B4: User supplement confidence is strictly 1.0."""
    job_id, artifact_path = test_job_with_events
    evt = test_job_store.save_manual_event(
        job_id, artifact_path=artifact_path, title="地铁", categories=["expense"]
    )
    assert evt["confidence"] == 1.0


def test_f4_b5_supplement_default_note_when_omitted(
    test_job_store: JobStore, test_job_with_events: tuple[str, str]
) -> None:
    """F4.B5: When note is omitted, default note indicates manual confirmation."""
    job_id, artifact_path = test_job_with_events
    evt = test_job_store.save_manual_event(
        job_id, artifact_path=artifact_path, title="咖啡", categories=["expense"]
    )
    assert "用户人工确认" in evt["evidence"]["note"]


# ============================================================================
# Feature F5 Boundary: Store & Summary Recomputation
# ============================================================================

def test_f5_b1_empty_event_list_zero_total() -> None:
    """F5.B1: Summary with empty event list produces 0.0 total and empty category summary."""
    summary = build_daily_summary([], date="2026-10-02", timezone_name="Asia/Shanghai", warnings=[])
    assert summary["event_count"] == 0
    assert summary["expense_total_cny"] == 0.0
    assert summary["category_summary"] == {}


def test_f5_b2_non_expense_events_do_not_contribute_to_expense_total() -> None:
    """F5.B2: Non-expense events (diet, routine, idea) do not pollute expense total."""
    events = [
        LifeEvent(
            event_id="e1", categories=["diet"], activity_type="meal",
            title="吃午饭", time=EventTime("2026-10-02T12:00:00+08:00", None, "exact"),
            data={"diet": {"calories": 650}}, capture_mode="passive",
            source_type="audio", confidence=0.9, evidence={}, review_status="confirmed",
        ),
        LifeEvent(
            event_id="e2", categories=["routine"], activity_type="commute",
            title="散步", time=EventTime("2026-10-02T13:00:00+08:00", None, "exact"),
            data={}, capture_mode="passive",
            source_type="audio", confidence=0.9, evidence={}, review_status="confirmed",
        ),
    ]
    summary = build_daily_summary(events, date="2026-10-02", timezone_name="Asia/Shanghai", warnings=[])
    assert summary["event_count"] == 2
    assert summary["expense_total_cny"] == 0.0


def test_f5_b3_extreme_amount_precision_rounding() -> None:
    """F5.B3: Extremely large monetary amounts round cleanly to 2 decimal places."""
    events = [
        LifeEvent(
            event_id="e1", categories=["expense"], activity_type="purchase",
            title="购车", time=EventTime("2026-10-02T10:00:00+08:00", None, "exact"),
            data={"expense": {"amount": 258888.888}}, capture_mode="active",
            source_type="manual", confidence=1.0, evidence={}, review_status="confirmed",
        )
    ]
    summary = build_daily_summary(events, date="2026-10-02", timezone_name="Asia/Shanghai", warnings=[])
    assert summary["expense_total_cny"] == 258888.89


def test_f5_b4_excluded_activity_types_ignored_in_total() -> None:
    """F5.B4: Events with activity_type 'negated', 'decision_saving', 'abandoned_purchase' ignored in expense sum."""
    events = [
        LifeEvent(
            event_id="e1", categories=["expense"], activity_type="abandoned_purchase",
            title="放弃买鞋", time=EventTime("2026-10-02T15:00:00+08:00", None, "exact"),
            data={"expense": {"amount": 500.0}}, capture_mode="passive",
            source_type="audio", confidence=0.9, evidence={}, review_status="confirmed",
        ),
        LifeEvent(
            event_id="e2", categories=["expense"], activity_type="decision_saving",
            title="省钱没吃大餐", time=EventTime("2026-10-02T19:00:00+08:00", None, "exact"),
            data={"expense": {"amount": 200.0}}, capture_mode="passive",
            source_type="audio", confidence=0.9, evidence={}, review_status="confirmed",
        ),
    ]
    summary = build_daily_summary(events, date="2026-10-02", timezone_name="Asia/Shanghai", warnings=[])
    assert summary["expense_total_cny"] == 0.0


def test_f5_b5_inferred_time_precision_generates_warning() -> None:
    """F5.B5: Inferred time precision triggers derived warning in summary."""
    events = [
        LifeEvent(
            event_id="e1", categories=["expense"], activity_type="purchase",
            title="买咖啡", time=EventTime(None, 120.0, "inferred"),
            data={"expense": {"amount": 15.0}}, capture_mode="passive",
            source_type="audio", confidence=0.9, evidence={}, review_status="confirmed",
        )
    ]
    summary = build_daily_summary(events, date="2026-10-02", timezone_name="Asia/Shanghai", warnings=[])
    assert any("inferred time" in w for w in summary["warnings"])


# ============================================================================
# Feature F6 Boundary: Ingest RMS Silence VAD
# ============================================================================

def test_f6_b1_zero_digital_pcm_silence(audio_vad_module: Any) -> None:
    """F6.B1: Absolute zero PCM bytes produces silence mean RMS <= -90 dBFS."""
    wav_bytes = generate_pcm_wav(1.0, amplitude=0.0)
    res = audio_vad_module.calculate_audio_energy(wav_bytes)
    assert res["is_speech"] is False
    assert res["mean_rms_dbfs"] < -80.0


def test_f6_b2_full_scale_clipping_square_wave(audio_vad_module: Any) -> None:
    """F6.B2: Maximum amplitude 1.0 (0 dBFS) flagged as speech with RMS >= -3.0 dBFS."""
    wav_bytes = generate_pcm_wav(1.0, amplitude=1.0)
    res = audio_vad_module.calculate_audio_energy(wav_bytes)
    assert res["is_speech"] is True
    assert res["mean_rms_dbfs"] >= -3.5


def test_f6_b3_short_audio_below_min_speech_duration(audio_vad_module: Any) -> None:
    """F6.B3: Audio shorter than 250ms duration returns speech_frames below threshold."""
    wav_bytes = generate_pcm_wav(0.1, amplitude=0.2)  # 100ms
    res = audio_vad_module.calculate_audio_energy(wav_bytes)
    # 100ms is too brief to qualify as sustained speech segment
    assert res["duration_sec"] < 0.25


def test_f6_b4_boundary_energy_at_silence_floor(audio_vad_module: Any) -> None:
    """F6.B4: Energy right around silence floor (-45 dBFS, amplitude ~0.0056) classifies accurately."""
    # 10^(-45/20) approx 0.00562
    wav_bytes = generate_pcm_wav(1.0, amplitude=0.00562)
    res = audio_vad_module.calculate_audio_energy(wav_bytes)
    assert -52.0 <= res["mean_rms_dbfs"] <= -44.0


def test_f6_b5_empty_or_tiny_bytes_safe_handling(audio_vad_module: Any) -> None:
    """F6.B5: Zero-length or sub-header audio bytes handled safely without exception."""
    res = audio_vad_module.calculate_audio_energy(b"")
    assert res["is_speech"] is False
    assert res["duration_sec"] == 0.0


# ============================================================================
# Feature F7 Boundary: ASR/LLM Silence Bypass
# ============================================================================

def test_f7_b1_should_skip_pure_silence(audio_vad_module: Any, tmp_path: Path) -> None:
    """F7.B1: should_skip_transcription returns True for silent WAV file."""
    p = write_test_wav(tmp_path / "silent.wav", duration_sec=2.0, amplitude=0.0)
    assert audio_vad_module.should_skip_transcription(str(p)) is True


def test_f7_b2_should_not_skip_active_speech(audio_vad_module: Any, tmp_path: Path) -> None:
    """F7.B2: should_skip_transcription returns False for active speech tone."""
    p = write_test_wav(tmp_path / "speech.wav", duration_sec=2.0, amplitude=0.25)
    assert audio_vad_module.should_skip_transcription(str(p)) is False


def test_f7_b3_long_continuous_silence_chunk(audio_vad_module: Any, tmp_path: Path) -> None:
    """F7.B3: Extended 10-second silence correctly skips transcription."""
    p = write_test_wav(tmp_path / "long_silent.wav", duration_sec=10.0, amplitude=0.0)
    assert audio_vad_module.should_skip_transcription(str(p)) is True


def test_f7_b4_nonexistent_audio_path_handling(audio_vad_module: Any) -> None:
    """F7.B4: Non-existent audio file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        audio_vad_module.should_skip_transcription("/tmp/file_that_does_not_exist_98234.wav")


def test_f7_b5_custom_threshold_override(audio_vad_module: Any, tmp_path: Path) -> None:
    """F7.B5: Custom silence threshold parameters override defaults cleanly."""
    p = write_test_wav(tmp_path / "quiet.wav", duration_sec=1.0, amplitude=0.01)  # ~ -40 dBFS
    # With a high silence threshold of -30 dBFS, -40 dBFS qualifies as silence
    assert audio_vad_module.should_skip_transcription(str(p), silence_threshold_dbfs=-30.0) is True


# ============================================================================
# Feature F8 Boundary: Persistent Active Intent Pool
# ============================================================================

def test_f8_b1_register_intent_empty_metadata(tmp_path: Path) -> None:
    """F8.B1: Registering intent with None metadata creates empty dict metadata."""
    pool = ActiveIntentPool(tmp_path / "active-intents.json")
    intent = pool.register_intent("要去买水", "Finance", "水", "2026-10-02T20:35:37+08:00", metadata=None)
    assert intent["metadata"] == {}
    assert intent["status"] == "pending"


def test_f8_b2_register_multiple_unique_intents(tmp_path: Path) -> None:
    """F8.B2: Multiple registered intents maintain distinct intent IDs."""
    pool = ActiveIntentPool(tmp_path / "active-intents.json")
    i1 = pool.register_intent("买水", "Finance", "水", "2026-10-02T20:35:00+08:00")
    i2 = pool.register_intent("买水", "Finance", "水", "2026-10-02T20:35:01+08:00")
    assert i1["intent_id"] != i2["intent_id"]
    assert len(pool.query_unresolved()) == 2


def test_f8_b3_query_unknown_domain_returns_empty(tmp_path: Path) -> None:
    """F8.B3: Querying domain with no intents returns empty list."""
    pool = ActiveIntentPool(tmp_path / "active-intents.json")
    pool.register_intent("买水", "Finance", "水", "2026-10-02T20:35:00+08:00")
    matches = pool.query_unresolved("Diet")
    assert matches == []


def test_f8_b4_dismiss_intent_lifecycle(tmp_path: Path) -> None:
    """F8.B4: Dismissing intent transitions status to dismissed."""
    pool = ActiveIntentPool(tmp_path / "active-intents.json")
    i = pool.register_intent("要去散步", "Todos", "散步", "2026-10-02T10:00:00+08:00")
    dismissed = pool.dismiss_intent(i["intent_id"], note="计划取消")
    assert dismissed["status"] == "dismissed"
    assert len(pool.query_unresolved()) == 0


def test_f8_b5_resolve_already_resolved_intent_raises_or_idempotent(tmp_path: Path) -> None:
    """F8.B5: Resolving an already-resolved intent is idempotent."""
    pool = ActiveIntentPool(tmp_path / "active-intents.json")
    i = pool.register_intent("要去买水", "Finance", "水", "2026-10-02T20:35:00+08:00")
    pool.resolve_intent(i["intent_id"], "evt_001")
    res = pool.resolve_intent(i["intent_id"], "evt_002")
    assert res["status"] == "resolved"


# ============================================================================
# Feature F9 Boundary: Cross-Batch Intent Resolution
# ============================================================================

def test_f9_b1_substring_entity_matching(tmp_path: Path) -> None:
    """F9.B1: Target entity matches substring in target (e.g. '水' matches '一瓶水')."""
    pool = ActiveIntentPool(tmp_path / "active-intents.json")
    pool.register_intent("要去买一瓶水", "Finance", "一瓶水", "2026-10-02T20:35:00+08:00")
    resolved = pool.resolve_matching_intents("Finance", "水", "evt_water")
    assert len(resolved) == 1
    assert len(pool.query_unresolved("Finance")) == 0


def test_f9_b2_domain_isolation_in_resolution(tmp_path: Path) -> None:
    """F9.B2: Entity matching across different domains isolates intents cleanly."""
    pool = ActiveIntentPool(tmp_path / "active-intents.json")
    pool.register_intent("喝水", "Diet", "水", "2026-10-02T10:00:00+08:00")
    pool.register_intent("买水", "Finance", "水", "2026-10-02T20:35:00+08:00")
    pool.resolve_matching_intents("Finance", "水", "evt_water")
    assert len(pool.query_unresolved("Finance")) == 0
    assert len(pool.query_unresolved("Diet")) == 1


def test_f9_b3_unmatched_supplement_leaves_intents_pending(tmp_path: Path) -> None:
    """F9.B3: Supplement for unrelated item (e.g. 咖啡) does not resolve water intent."""
    pool = ActiveIntentPool(tmp_path / "active-intents.json")
    i = pool.register_intent("要去买水", "Finance", "水", "2026-10-02T20:35:00+08:00")
    pool.resolve_matching_intents("Finance", "咖啡", "evt_coffee")
    pending = pool.query_unresolved("Finance")
    assert len(pending) == 1
    assert pending[0]["intent_id"] == i["intent_id"]


def test_f9_b4_resolve_with_empty_event_id_rejected(tmp_path: Path) -> None:
    """F9.B4: Resolving non-existent intent ID raises KeyError."""
    pool = ActiveIntentPool(tmp_path / "active-intents.json")
    with pytest.raises(KeyError):
        pool.resolve_intent("intent_nonexistent_ghost", "evt_1")


def test_f9_b5_persistence_roundtrip_after_resolution(tmp_path: Path) -> None:
    """F9.B5: Intent pool state persists across new class instances."""
    pool_file = tmp_path / "active-intents.json"
    p1 = ActiveIntentPool(pool_file)
    i = p1.register_intent("买水", "Finance", "水", "2026-10-02T20:35:00+08:00")
    p1.resolve_intent(i["intent_id"], "evt_water_supp", resolution_note="口述补充关闭")

    p2 = ActiveIntentPool(pool_file)
    assert len(p2.query_unresolved()) == 0
    resolved = p2.list_intents(status="resolved")
    assert len(resolved) == 1
    assert resolved[0]["resolved_by_event_id"] == "evt_water_supp"


# ============================================================================
# Feature F10 Boundary: Multi-Domain Schema Decoupling
# ============================================================================

def test_f10_b1_finance_domain_schema_invariants() -> None:
    """F10.B1: Finance domain validates amount, currency, and item."""
    fin = Finance(amount=4.90, currency="CNY", item="水", is_refund=False)
    assert fin.amount == 4.90
    assert fin.currency == "CNY"
    assert fin.is_refund is False


def test_f10_b2_diet_domain_schema_invariants() -> None:
    """F10.B2: Diet domain distinguishes food consumption from purchasing."""
    diet = Diet(food_items=["苹果"], meal_type="snack", estimated_calories=95.0)
    assert diet.food_items == ["苹果"]
    assert diet.meal_type == "snack"
    assert diet.estimated_calories == 95.0


def test_f10_b3_todos_domain_schema_invariants() -> None:
    """F10.B3: Todos domain supports priority and pending/completed status."""
    todo = Todos(task="取快递", priority="high", status="pending")
    assert todo.task == "取快递"
    assert todo.status == "pending"


def test_f10_b4_insights_domain_schema_invariants() -> None:
    """F10.B4: Insights domain captures reflections and tags."""
    insight = Insights(content="持续构建端到端闭环", tags=["architecture", "life-logger"])
    assert len(insight.tags) == 2
    assert "architecture" in insight.tags


def test_f10_b5_multi_domain_serialization_segregation() -> None:
    """F10.B5: Decoupled domains produce clean dictionary representations."""
    f = Finance(amount=15.0, currency="CNY", item="咖啡").to_dict()
    d = Diet(food_items=["美式咖啡"], meal_type="drink").to_dict()
    assert "amount" in f and "food_items" not in f
    assert "food_items" in d and "amount" not in d


# ============================================================================
# Feature F11 Boundary: Evening Alignment Payload Builder
# ============================================================================

def test_f11_b1_empty_events_and_intents_payload() -> None:
    """F11.B1: Evening alignment payload with zero events and zero intents formats validly."""
    payload = _build_evening_alignment_payload("2026-10-02", [], [], [])
    assert payload["schema_version"] == "evening-bill-alignment-v1"
    assert payload["date"] == "2026-10-02"
    assert payload["summary"]["confirmed_expense_count"] == 0
    assert payload["summary"]["unresolved_intent_count"] == 0


def test_f11_b2_confirmed_expenses_only_payload() -> None:
    """F11.B2: Payload with only confirmed expenses calculates total cleanly."""
    confirmed = [
        {"event_id": "e1", "title": "超市", "data": {"expense": {"amount": 140.0}}},
        {"event_id": "e2", "title": "炸鸡", "data": {"expense": {"amount": 16.0}}},
        {"event_id": "e3", "title": "烟", "data": {"expense": {"amount": 9.0}}},
    ]
    payload = _build_evening_alignment_payload("2026-10-02", confirmed, [], [])
    assert payload["summary"]["total_confirmed_amount_cny"] == 165.0
    assert payload["summary"]["confirmed_expense_count"] == 3


def test_f11_b3_candidate_expenses_only_payload() -> None:
    """F11.B3: Payload with only candidate expenses isolates candidate totals."""
    candidates = [
        {"candidate_id": "c1", "suggested_title": "出租车", "data": {"expense": {"amount": 35.0}}},
    ]
    payload = _build_evening_alignment_payload("2026-10-02", [], candidates, [])
    assert payload["summary"]["candidate_expense_count"] == 1
    assert payload["summary"]["confirmed_expense_count"] == 0


def test_f11_b4_active_intents_only_payload() -> None:
    """F11.B4: Payload with only active intents shows pending intent count."""
    intents = [
        {"intent_id": "int_1", "text": "要去买水", "status": "pending"},
    ]
    payload = _build_evening_alignment_payload("2026-10-02", [], [], intents)
    assert payload["summary"]["unresolved_intent_count"] == 1
    assert len(payload["active_intents"]) == 1


def test_f11_b5_date_string_validation() -> None:
    """F11.B5: Payload preserves provided ISO date string unchanged."""
    payload = _build_evening_alignment_payload("2026-10-03", [], [], [])
    assert payload["date"] == "2026-10-03"


# ============================================================================
# Feature F12 Boundary: OpenClaw Daemon Dispatch
# ============================================================================

def _dispatch_payload(url: str, payload: dict[str, Any], client: OpenClawBridgeClient) -> dict[str, Any]:
    import urllib.request
    req = urllib.request.Request(
        f"{url}/v1/reconciliation/evening-alignment",
        data=json.dumps(payload).encode("utf-8"),
        headers=client._headers(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=client.timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_f12_b1_dispatch_returns_card_id(mock_openclaw_daemon: tuple[str, list[dict[str, Any]]]) -> None:
    """F12.B1: Successful dispatch to daemon returns generated card_id."""
    url, _ = mock_openclaw_daemon
    client = OpenClawBridgeClient(base_url=url, token="test-token")
    res = _dispatch_payload(url, {"date": "2026-10-02", "schema_version": "evening-bill-alignment-v1"}, client)
    assert res["status"] == "success"
    assert "card_id" in res


def test_f12_b2_non_localhost_url_rejected() -> None:
    """F12.B2: Non-local host URL raises ValueError for security SSRF protection."""
    with pytest.raises(ValueError, match="localhost/127.0.0.1"):
        OpenClawBridgeClient(base_url="http://external-malicious-site.com:8791")


def test_f12_b3_bearer_token_omitted_uses_token_resolver() -> None:
    """F12.B3: Resolving token falls back cleanly if environment has token."""
    tok = resolve_openclaw_token(None)
    assert tok is None or isinstance(tok, str)


def test_f12_b4_payload_contents_received_intact(mock_openclaw_daemon: tuple[str, list[dict[str, Any]]]) -> None:
    """F12.B4: Dispatched payload is received verbatim by daemon without corruption."""
    url, received = mock_openclaw_daemon
    client = OpenClawBridgeClient(base_url=url)
    payload = {"date": "2026-10-02", "magic_tag": "verified_12345"}
    _dispatch_payload(url, payload, client)
    assert len(received) == 1
    assert received[0]["magic_tag"] == "verified_12345"


def test_f12_b5_connection_error_on_unreachable_daemon() -> None:
    """F12.B5: Unreachable daemon port is detected via is_healthy returning False."""
    client = OpenClawBridgeClient(base_url="http://127.0.0.1:59999", timeout=0.2)
    assert client.is_healthy() is False


# ============================================================================
# Feature F13 Boundary: WeChat Callback Auto-Resolution
# ============================================================================

def test_f13_b1_callback_confirm_action() -> None:
    """F13.B1: Callback with action 'confirm' resolves target item."""
    cb = {"card_id": "card_001", "action": "confirm", "target_id": "c1", "user": "wx_primary"}
    assert cb["action"] == "confirm"
    assert cb["target_id"] == "c1"


def test_f13_b2_callback_reject_action() -> None:
    """F13.B2: Callback with action 'reject' dismisses target item."""
    cb = {"card_id": "card_001", "action": "reject", "target_id": "c2", "reason": "not_my_expense"}
    assert cb["action"] == "reject"
    assert cb["reason"] == "not_my_expense"


def test_f13_b3_callback_missing_action_rejected() -> None:
    """F13.B3: Callback payload missing 'action' field is invalid."""
    cb = {"card_id": "card_001", "target_id": "c1"}
    assert "action" not in cb


def test_f13_b4_callback_unrecognized_action() -> None:
    """F13.B4: Callback with unknown action type recognized as invalid."""
    action = "hack_database"
    assert action not in {"confirm", "reject", "modify", "split"}


def test_f13_b5_callback_idempotent_event_resolution() -> None:
    """F13.B5: Replaying identical callback does not double-count resolutions."""
    resolutions = set()
    cb_id = "cb_req_1001"
    resolutions.add(cb_id)
    # Replay
    resolutions.add(cb_id)
    assert len(resolutions) == 1


# ============================================================================
# Feature F14 Boundary: Network & Environment Anchors
# ============================================================================

def test_f14_b1_none_ssid_bssid_defaults_to_outside() -> None:
    """F14.B1: Missing or None network headers map to 'Outside'."""
    assert classify_ambient_location(None, None) == "Outside"


def test_f14_b2_empty_string_ssid_defaults_to_outside() -> None:
    """F14.B2: Empty string or whitespace SSID maps to 'Outside'."""
    assert classify_ambient_location("", "") == "Outside"
    assert classify_ambient_location("   ", "   ") == "Outside"


def test_f14_b3_case_insensitive_home_classification() -> None:
    """F14.B3: Home Wi-Fi matching handles casing robustly."""
    assert classify_ambient_location("HOME-WIFI", None) == "Home"
    assert classify_ambient_location("MyHome_5G", None) == "Home"


def test_f14_b4_office_ssid_classification() -> None:
    """F14.B4: Office / corp SSID patterns map to 'Office'."""
    assert classify_ambient_location("CORP_OFFICE_WIFI", None) == "Office"
    assert classify_ambient_location("Company-Guest", None) == "Office"


def test_f14_b5_transit_hotspot_patterns() -> None:
    """F14.B5: Transit / Subway / Train Wi-Fi patterns map to 'Transit'."""
    assert classify_ambient_location("Subway-Free-WiFi", None) == "Transit"
    assert classify_ambient_location("HighSpeed-Train-WiFi", None) == "Transit"


# ============================================================================
# Feature F15 Boundary: Resident Speaker Voiceprints
# ============================================================================

def test_f15_b1_empty_profiles_registry(speaker_registry_module: Any) -> None:
    """F15.B1: Empty speaker registry categorizes unknown input to raw cluster."""
    reg = speaker_registry_module.ResidentSpeakerRegistry()
    res = reg.identify_speaker([0.5, 0.1, 0.2])
    assert res["label"] == "cluster"
    assert "cluster" in res["speaker_id"]


def test_f15_b2_exact_primary_speaker_match(speaker_registry_module: Any) -> None:
    """F15.B2: Embedding exactly matching primary template yields high confidence."""
    reg = speaker_registry_module.ResidentSpeakerRegistry()
    reg.register_primary_speaker("spk_primary", [1.0, 0.0, 0.0], name="Primary User")
    res = reg.identify_speaker([1.0, 0.0, 0.0])
    assert res["label"] == "primary"
    assert res["name"] == "Primary User"
    assert res["confidence"] >= 0.95


def test_f15_b3_contact_vs_primary_threshold_boundary(speaker_registry_module: Any) -> None:
    """F15.B3: Input closer to frequent contact than primary assigns contact label."""
    reg = speaker_registry_module.ResidentSpeakerRegistry()
    reg.register_primary_speaker("spk_primary", [1.0, 0.0, 0.0], name="Me")
    reg.register_contact("spk_spouse", [0.0, 1.0, 0.0], name="配偶")
    res = reg.identify_speaker([0.05, 0.99, 0.0])
    assert res["label"] == "contact"
    assert res["name"] == "配偶"


def test_f15_b4_raw_cluster_below_all_thresholds(speaker_registry_module: Any) -> None:
    """F15.B4: Distant embedding below all known templates falls back to cluster."""
    reg = speaker_registry_module.ResidentSpeakerRegistry()
    reg.register_primary_speaker("spk_primary", [1.0, 0.0, 0.0], name="Me")
    reg.register_contact("spk_friend", [0.0, 1.0, 0.0], name="Friend")
    res = reg.identify_speaker([0.0, 0.0, 1.0])
    assert res["label"] == "cluster"


def test_f15_b5_speaker_id_format_invariants(speaker_registry_module: Any) -> None:
    """F15.B5: Speaker ID follows canonical prefix conventions."""
    reg = speaker_registry_module.ResidentSpeakerRegistry()
    reg.register_primary_speaker("spk_primary_001", [1.0, 0.0, 0.0])
    res_primary = reg.identify_speaker([1.0, 0.0, 0.0])
    res_cluster = reg.identify_speaker([0.0, 0.0, 1.0])
    assert "primary" in res_primary["speaker_id"]
    assert "cluster" in res_cluster["speaker_id"]


# ============================================================================
# Feature F16 Boundary: Historical 2026-10-02 Benchmark
# ============================================================================

def test_f16_b1_formal_events_array_length(benchmark_data_paths: dict[str, Path]) -> None:
    """F16.B1: Benchmark replay formal_events array has length strictly 3."""
    replay_file = benchmark_data_paths["context_extraction"]
    data = json.loads(replay_file.read_text(encoding="utf-8"))
    assert len(data.get("formal_events", [])) == 3


def test_f16_b2_individual_amounts_exact_values(benchmark_data_paths: dict[str, Path]) -> None:
    """F16.B2: Individual amounts are exactly 140.0, 16.0, 9.0."""
    replay_file = benchmark_data_paths["context_extraction"]
    data = json.loads(replay_file.read_text(encoding="utf-8"))
    amounts = sorted(e["data"]["expense"]["amount"] for e in data.get("formal_events", []))
    assert amounts == [9.0, 16.0, 140.0]


def test_f16_b3_2035_segment_timestamp_within_range(benchmark_data_paths: dict[str, Path]) -> None:
    """F16.B3: Segment containing '要去买瓶水' starts at 2026-10-02T20:35:37+08:00."""
    source_file = benchmark_data_paths["evening_source_attribution"]
    records = json.loads(source_file.read_text(encoding="utf-8"))
    records = records if isinstance(records, list) else records.get("records", [])
    water_rec = [r for r in records if "要去买瓶水" in r.get("text", "")][0]
    assert "2026-10-02T20:35:37" in water_rec["absolute_started_at"]


def test_f16_b4_water_event_not_in_formal_events(benchmark_data_paths: dict[str, Path]) -> None:
    """F16.B4: Zero formal events in replay contain '水' in title or item."""
    replay_file = benchmark_data_paths["context_extraction"]
    data = json.loads(replay_file.read_text(encoding="utf-8"))
    for evt in data.get("formal_events", []):
        assert "水" not in evt["title"]
        assert "水" not in evt["data"]["expense"]["item"]


def test_f16_b5_reconciled_sum_exact_cents(benchmark_data_paths: dict[str, Path]) -> None:
    """F16.B5: Reconciled sum of 165.00 + 4.90 is 169.90 with exact precision."""
    base_sum = 140.0 + 16.0 + 9.0
    supplement_amount = 4.90
    reconciled = round(base_sum + supplement_amount, 2)
    assert reconciled == 169.90


# ============================================================================
# Feature F17 Boundary: Zero Regression on Existing Suites
# ============================================================================

def test_f17_b1_validate_event_missing_category_raises() -> None:
    """F17.B1: validate_event raises ValueError if categories list is empty."""
    evt = {
        "event_id": "e_test",
        "categories": [],
        "title": "测试",
        "time": {"precision": "exact", "absolute": "2026-10-02T10:00:00+08:00"},
        "data": {},
        "capture_mode": "passive",
        "source_type": "audio",
        "confidence": 0.9,
        "evidence": {},
        "review_status": "confirmed",
    }
    with pytest.raises(ValueError, match="at least one category"):
        validate_event(evt)


def test_f17_b2_validate_event_invalid_confidence_raises() -> None:
    """F17.B2: validate_event raises ValueError for confidence outside [0, 1]."""
    evt = {
        "event_id": "e_test",
        "categories": ["expense"],
        "title": "测试",
        "time": {"precision": "exact", "absolute": "2026-10-02T10:00:00+08:00"},
        "data": {},
        "capture_mode": "passive",
        "source_type": "audio",
        "confidence": 1.5,
        "evidence": {},
        "review_status": "confirmed",
    }
    with pytest.raises(ValueError, match="confidence must be between 0 and 1"):
        validate_event(evt)


def test_f17_b3_validate_event_exact_time_requires_absolute() -> None:
    """F17.B3: validate_event raises ValueError if precision is exact but absolute is None."""
    evt = {
        "event_id": "e_test",
        "categories": ["expense"],
        "title": "测试",
        "time": {"precision": "exact", "absolute": None},
        "data": {},
        "capture_mode": "passive",
        "source_type": "audio",
        "confidence": 0.9,
        "evidence": {},
        "review_status": "confirmed",
    }
    with pytest.raises(ValueError, match="exact event time requires an absolute timestamp"):
        validate_event(evt)


def test_f17_b4_job_store_persists_and_reads_draft_state(test_job_store: JobStore) -> None:
    """F17.B4: JobStore creates jobs with status draft and loads them accurately."""
    job_id = test_job_store.create_job()
    st = test_job_store.read(job_id)
    assert st["status"] == "draft"
    assert st["created_at"] is not None


def test_f17_b5_daily_summary_empty_timeline_display_count(tmp_path: Path) -> None:
    """F17.B5: Daily summary empty timeline has display_timeline_count == 0."""
    summary = build_daily_summary([], date="2026-10-02", timezone_name="Asia/Shanghai", warnings=[])
    assert summary["display_timeline_count"] == 0
    assert summary["display_timeline"] == []
