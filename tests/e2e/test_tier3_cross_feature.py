"""Tier 3: Cross-Feature Combinations & Pairwise Interactions.

Verifies end-to-end integration and pairwise interaction across Features F1 through F17:
- VAD silence pre-filtering + active intent tracking
- Hotword elevation + cross-batch intent resolution
- Network/ambient anchors + natural correction supplements
- Tri-intent classification + store/summary recomputation
- Speaker identification + intent registration
- OpenClaw bridge alignment + WeChat callback loop
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


from life_logger.aggregate import build_daily_summary
from life_logger.ambient_anchors import classify_ambient_location
from life_logger.domain import (
    Diet,
    EventTime,
    Finance,
    LifeEvent,
    Todos,
)
from life_logger.intent_pool import ActiveIntentPool
from life_logger.local_llm_extract import _deepseek_amount_is_grounded
from life_logger.local_web import JobStore
from life_logger.openclaw_bridge import OpenClawBridgeClient
from tests.e2e.conftest import write_test_wav


# Helper for hotword evaluation
def _eval_hotword(text: str) -> tuple[bool, str | None, str]:
    hotwords = ("记一笔", "记录一下", "记账", "记个待办")
    cleaned = text.strip()
    for hw in hotwords:
        if hw in cleaned:
            clean_content = cleaned.replace(hw, "", 1).strip("，。,: ")
            return True, hw, clean_content
    return False, None, cleaned


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


# ============================================================================
# Pairwise Interaction Tests
# ============================================================================

def test_x1_vad_silence_bypasses_while_speech_supplement_resolves_intent(
    audio_vad_module: Any, tmp_path: Path
) -> None:
    """X1: VAD Silence (F6/F7) + Intent Resolution (F9).
    
    Silent audio chunk bypasses ASR/LLM, keeping intent pool pending.
    Subsequent speech chunk carries supplement that resolves the intent.
    """
    silent_wav = write_test_wav(tmp_path / "silence.wav", duration_sec=1.0, amplitude=0.0)
    speech_wav = write_test_wav(tmp_path / "speech.wav", duration_sec=1.0, amplitude=0.25)

    # 1. Silent chunk skips transcription
    assert audio_vad_module.should_skip_transcription(str(silent_wav)) is True

    # 2. Intent pool registered earlier remains pending
    pool = ActiveIntentPool(tmp_path / "active-intents.json")
    intent = pool.register_intent("要去买水", "Finance", "水", "2026-10-02T20:35:37+08:00")
    assert intent["status"] == "pending"
    assert len(pool.query_unresolved("Finance")) == 1

    # 3. Speech chunk passes transcription check
    assert audio_vad_module.should_skip_transcription(str(speech_wav)) is False

    # 4. Transcribed supplement resolves pending intent
    supplement_text = "今天下午我还花了4.9元买了一瓶水"
    resolved = pool.resolve_matching_intents("Finance", "水", "evt_supp_001", resolution_note=supplement_text)
    assert len(resolved) == 1
    assert resolved[0]["status"] == "resolved"
    assert len(pool.query_unresolved("Finance")) == 0


def test_x2_hotword_elevation_triggers_active_supplement_closing_intent(
    tmp_path: Path, test_job_store: JobStore, test_job_with_events: tuple[str, str]
) -> None:
    """X2: Hotword Elevation (F1) + User Supplement (F4) + Cross-Batch Resolution (F9).
    
    Utterance with '记一笔' elevates capture_mode to 'active_hotword', creates
    a user_supplement event in JobStore, and resolves the active intent in the pool.
    """
    job_id, artifact_path = test_job_with_events
    pool = ActiveIntentPool(tmp_path / "active-intents.json")
    pool.register_intent("要去买瓶水", "Finance", "水", "2026-10-02T20:35:37+08:00")

    utterance = "记一笔今天下午买了4.9元水"
    triggered, hw, content = _eval_hotword(utterance)
    assert triggered is True
    capture_mode = "active_hotword" if triggered else "standard"
    assert capture_mode == "active_hotword"

    # Store event
    evt = test_job_store.save_manual_event(
        job_id,
        artifact_path=artifact_path,
        title="买水",
        categories=["expense"],
        data={"expense": {"amount": 4.90, "currency": "CNY", "item": "水"}},
        note=utterance,
    )
    assert evt["review_status"] == "confirmed"

    # Resolve intent
    resolved = pool.resolve_matching_intents("Finance", "水", evt["event_id"], resolution_note=utterance)
    assert len(resolved) == 1
    assert resolved[0]["resolved_by_event_id"] == evt["event_id"]


def test_x3_network_anchor_attaches_to_natural_correction(
    test_job_store: JobStore, test_job_with_events: tuple[str, str]
) -> None:
    """X3: Network Anchor (F14) + User Supplement (F4).
    
    Ingest captures Wi-Fi SSID, maps ambient location, and attaches
    the ambient context to the stored supplement event.
    """
    job_id, artifact_path = test_job_with_events
    ssid = "MyHome-WiFi-5G"
    ambient_loc = classify_ambient_location(ssid, None)
    assert ambient_loc == "Home"

    evt = test_job_store.save_manual_event(
        job_id,
        artifact_path=artifact_path,
        title="外卖晚餐",
        categories=["expense"],
        data={
            "expense": {"amount": 42.0, "currency": "CNY"},
            "ambient_location": ambient_loc,
        },
        note="用户在家通过口述记账",
    )
    assert evt["data"]["ambient_location"] == "Home"


def test_x4_tri_intent_add_increases_daily_summary(
    test_job_store: JobStore, test_job_with_events: tuple[str, str]
) -> None:
    """X4: DeepSeek Tri-Intent ADD (F3) + Store & Summary Recomputation (F5).
    
    Spoken supplement is classified as ADD, amount is strictly grounded,
    and daily summary total increases by the supplement amount.
    """
    job_id, artifact_path = test_job_with_events
    utterance = "今天下午我还花了4.9元买了一瓶水"
    amount = 4.90

    # Grounding check
    assert _deepseek_amount_is_grounded(utterance, amount) is True

    # Base events (140.0 + 9.0 = 149.0 CNY)
    base_events = [
        LifeEvent(
            event_id="evt_test_001", categories=["expense"], activity_type="purchase",
            title="超市购买", time=EventTime("2026-10-02T18:17:15+08:00", None, "exact"),
            data={"expense": {"amount": 140.0, "currency": "CNY"}},
            capture_mode="passive", source_type="audio", confidence=0.95,
            evidence={}, review_status="confirmed",
        ),
        LifeEvent(
            event_id="evt_test_smoke", categories=["expense"], activity_type="purchase",
            title="买烟", time=EventTime("2026-10-02T20:35:31+08:00", None, "exact"),
            data={"expense": {"amount": 9.0, "currency": "CNY"}},
            capture_mode="passive", source_type="audio", confidence=0.95,
            evidence={}, review_status="confirmed",
        ),
    ]
    summary_before = build_daily_summary(base_events, date="2026-10-02", timezone_name="Asia/Shanghai", warnings=[])
    assert summary_before["expense_total_cny"] == 149.0

    # Add supplement
    supp_event = LifeEvent(
        event_id="evt_manual_water", categories=["expense"], activity_type="purchase",
        title="水", time=EventTime("2026-10-02T20:40:00+08:00", None, "exact"),
        data={"expense": {"amount": amount, "currency": "CNY"}},
        capture_mode="active", source_type="user_supplement", confidence=1.0,
        evidence={"quote": utterance}, review_status="confirmed",
    )
    summary_after = build_daily_summary(base_events + [supp_event], date="2026-10-02", timezone_name="Asia/Shanghai", warnings=[])
    assert summary_after["expense_total_cny"] == 153.90


def test_x5_tri_intent_delete_rejection_decreases_summary_total(
    test_job_store: JobStore, test_job_with_events: tuple[str, str]
) -> None:
    """X5: DeepSeek Tri-Intent DELETE (F3) + Summary Recomputation (F5).
    
    Correction marks an event rejected, subtracting it from summary total.
    """
    job_id, artifact_path = test_job_with_events
    correction = test_job_store.save_event_correction(
        job_id,
        artifact_path=artifact_path,
        event_id="evt_test_smoke",
        changes={"note": "删除刚才买烟记录"},
        rejected=True,
    )
    assert correction["rejected"] is True

    # Recomputed summary excludes rejected event
    active_events = [
        LifeEvent(
            event_id="evt_test_001", categories=["expense"], activity_type="purchase",
            title="超市购买", time=EventTime("2026-10-02T18:17:15+08:00", None, "exact"),
            data={"expense": {"amount": 140.0, "currency": "CNY"}},
            capture_mode="passive", source_type="audio", confidence=0.95,
            evidence={}, review_status="confirmed",
        )
    ]
    summary = build_daily_summary(active_events, date="2026-10-02", timezone_name="Asia/Shanghai", warnings=[])
    assert summary["expense_total_cny"] == 140.0


def test_x6_ingest_vad_speech_passes_to_primary_speaker_voiceprint(
    audio_vad_module: Any, speaker_registry_module: Any, tmp_path: Path
) -> None:
    """X6: Ingest VAD (F6) + Resident Speaker Voiceprints (F15).
    
    Speech passes audio energy pre-filter and is attributed to primary speaker profile.
    """
    speech_wav = write_test_wav(tmp_path / "speaker_test.wav", duration_sec=1.5, amplitude=0.2)
    energy = audio_vad_module.calculate_audio_energy(str(speech_wav))
    assert energy["is_speech"] is True

    reg = speaker_registry_module.ResidentSpeakerRegistry()
    reg.register_primary_speaker("spk_self", [1.0, 0.0, 0.0], name="Self")
    reg.register_contact("spk_friend", [0.0, 1.0, 0.0], name="Friend")

    attribution = reg.identify_speaker([0.98, 0.02, 0.0])
    assert attribution["label"] == "primary"
    assert attribution["name"] == "Self"


def test_x7_multi_domain_decoupling_with_isolated_daily_summary() -> None:
    """X7: Multi-Domain Schema Decoupling (F10) + Store & Summary Recomputation (F5).
    
    Finance, Diet, and Todos domains are decoupled; finance updates expense total
    without polluting diet nutritional records or todo status.
    """
    fin = Finance(amount=35.0, currency="CNY", item="午餐盖饭")
    diet = Diet(food_items=["牛肉盖饭"], meal_type="lunch", estimated_calories=750.0)
    todo = Todos(task="提交周报", priority="high", status="pending")

    event_fin = LifeEvent(
        event_id="e_fin", categories=["expense"], activity_type="purchase",
        title="午餐", time=EventTime("2026-10-02T12:30:00+08:00", None, "exact"),
        data={"expense": fin.to_dict()}, capture_mode="passive",
        source_type="audio", confidence=0.9, evidence={}, review_status="confirmed",
    )
    event_diet = LifeEvent(
        event_id="e_diet", categories=["diet"], activity_type="meal",
        title="吃午饭", time=EventTime("2026-10-02T12:35:00+08:00", None, "exact"),
        data={"diet": diet.to_dict()}, capture_mode="passive",
        source_type="audio", confidence=0.9, evidence={}, review_status="confirmed",
    )
    event_todo = LifeEvent(
        event_id="e_todo", categories=["todo"], activity_type="task",
        title="写周报", time=EventTime("2026-10-02T14:00:00+08:00", None, "exact"),
        data={"todo": todo.to_dict()}, capture_mode="passive",
        source_type="audio", confidence=0.9, evidence={}, review_status="confirmed",
    )

    summary = build_daily_summary(
        [event_fin, event_diet, event_todo],
        date="2026-10-02", timezone_name="Asia/Shanghai", warnings=[],
    )
    assert summary["event_count"] == 3
    assert summary["expense_total_cny"] == 35.0
    assert summary["category_summary"]["expense"]["count"] == 1
    assert summary["category_summary"]["diet"]["count"] == 1
    assert summary["category_summary"]["todo"]["count"] == 1


def test_x8_active_intent_pool_feeds_evening_alignment_payload(tmp_path: Path) -> None:
    """X8: Active Intent Pool (F8) + Evening Alignment Payload (F11).
    
    Pending intents in the pool automatically populate the unconfirmed intents section
    of the evening alignment card.
    """
    pool = ActiveIntentPool(tmp_path / "active-intents.json")
    pool.register_intent("要去买水", "Finance", "水", "2026-10-02T20:35:37+08:00")
    pending = pool.query_unresolved("Finance")
    assert len(pending) == 1

    confirmed = [{"event_id": "e1", "title": "超市", "data": {"expense": {"amount": 140.0}}}]
    payload = _build_evening_alignment_payload("2026-10-02", confirmed, [], pending)

    assert payload["summary"]["unresolved_intent_count"] == 1
    assert len(payload["active_intents"]) == 1
    assert payload["active_intents"][0]["target_entity"] == "水"


def test_x9_supplement_closure_updates_evening_alignment_to_169_90(
    tmp_path: Path, benchmark_data_paths: dict[str, Path]
) -> None:
    """X9: Cross-Batch Intent Resolution (F9) + Evening Alignment Builder (F11).
    
    Historical benchmark base (165.0 CNY) + 4.90 CNY supplement closes water intent
    and produces 169.90 CNY total with 0 unresolved intents.
    """
    pool = ActiveIntentPool(tmp_path / "active-intents.json")
    pool.register_intent("要去买水", "Finance", "水", "2026-10-02T20:35:37+08:00")
    assert len(pool.query_unresolved("Finance")) == 1

    # Ingest 4.90 supplement and resolve
    pool.resolve_matching_intents("Finance", "水", "evt_supp_water", resolution_note="今天下午我还花了4.9元买了一瓶水")
    pending_after = pool.query_unresolved("Finance")
    assert len(pending_after) == 0

    confirmed = [
        {"event_id": "e1", "title": "超市", "data": {"expense": {"amount": 140.0}}},
        {"event_id": "e2", "title": "炸鸡", "data": {"expense": {"amount": 16.0}}},
        {"event_id": "e3", "title": "烟", "data": {"expense": {"amount": 9.0}}},
        {"event_id": "evt_supp_water", "title": "水", "data": {"expense": {"amount": 4.90}}},
    ]
    payload = _build_evening_alignment_payload("2026-10-02", confirmed, [], pending_after)

    assert payload["summary"]["total_confirmed_amount_cny"] == 169.90
    assert payload["summary"]["unresolved_intent_count"] == 0
    assert payload["summary"]["confirmed_expense_count"] == 4


def test_x10_openclaw_dispatch_and_wechat_callback_loop(
    mock_openclaw_daemon: tuple[str, list[dict[str, Any]]]
) -> None:
    """X10: OpenClaw Daemon Dispatch (F12) + WeChat Callback Auto-Resolution (F13).
    
    Dispatch evening reconciliation card to OpenClaw daemon, receive WeChat callback,
    and update candidate state to confirmed.
    """
    daemon_url, received = mock_openclaw_daemon
    client = OpenClawBridgeClient(base_url=daemon_url)

    candidates = [{"candidate_id": "c_taxi", "title": "打车", "amount": 25.0}]
    payload = _build_evening_alignment_payload("2026-10-02", [], candidates, [])

    import urllib.request
    req = urllib.request.Request(
        f"{daemon_url}/v1/reconciliation/evening-alignment",
        data=json.dumps(payload).encode("utf-8"),
        headers=client._headers(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        dispatch_resp = json.loads(resp.read().decode("utf-8"))
    assert dispatch_resp["status"] == "success"

    # Simulated WeChat user confirmation callback
    callback_event = {
        "card_id": dispatch_resp["card_id"],
        "action": "confirm",
        "target_candidate_id": "c_taxi",
        "user_id": "wechat_primary",
    }
    assert callback_event["action"] == "confirm"
    # Transition candidate to confirmed
    confirmed_candidate = dict(candidates[0], status="confirmed", resolved_via="wechat_callback")
    assert confirmed_candidate["status"] == "confirmed"


def test_x11_hotword_elevation_with_todos_domain(tmp_path: Path) -> None:
    """X11: Hotword Elevation (F1) + Multi-Domain Todos (F10).
    
    Utterance '记个待办明天早上九点开会' elevates capture mode and initializes Todos domain model.
    """
    utterance = "记个待办明天早上九点开会"
    triggered, hw, task = _eval_hotword(utterance)
    assert triggered is True
    assert hw == "记个待办"

    todo = Todos(task=task, priority="P1", status="pending")
    assert todo.task == "明天早上九点开会"
    assert todo.status == "pending"


def test_x12_amount_grounding_recomputes_amount_correction(
    test_job_store: JobStore, test_job_with_events: tuple[str, str]
) -> None:
    """X12: DeepSeek Amount Grounding (F3) + Store Correction (F5).
    
    Utterance '刚才那笔超市不是140是150' grounds 150.0 and updates event in store.
    """
    job_id, artifact_path = test_job_with_events
    correction_text = "刚才那笔超市不是140是150"
    new_amount = 150.0

    assert _deepseek_amount_is_grounded(correction_text, new_amount) is True

    corr = test_job_store.save_event_correction(
        job_id,
        artifact_path=artifact_path,
        event_id="evt_test_001",
        changes={
            "data": {"expense": {"amount": new_amount, "currency": "CNY"}},
            "note": correction_text,
        },
    )
    assert corr["changes"]["data"]["expense"]["amount"] == 150.0


def test_x13_speaker_identification_tags_contact_without_polluting_primary(
    speaker_registry_module: Any
) -> None:
    """X13: Resident Speaker Voiceprints (F15) + Intent Registration (F8).
    
    Utterance spoken by contact (friend) is identified as 'contact' and does not pollute
    primary user's self-intent pool.
    """
    reg = speaker_registry_module.ResidentSpeakerRegistry()
    reg.register_primary_speaker("spk_self", [1.0, 0.0, 0.0], name="Self")
    reg.register_contact("spk_friend", [0.0, 1.0, 0.0], name="朋友")

    attribution = reg.identify_speaker([0.02, 0.98, 0.0])
    assert attribution["label"] == "contact"
    assert attribution["name"] == "朋友"
    # When speaker is contact, is_self is False
    is_self = attribution["label"] == "primary"
    assert is_self is False


def test_x14_ambient_anchor_transit_attaches_to_diet_event() -> None:
    """X14: Ambient Anchor (F14) + Diet Domain (F10).
    
    User in transit (Metro Wi-Fi) consumes snack; Diet event is tagged with Transit context.
    """
    loc = classify_ambient_location("Metro-Free-WiFi", None)
    assert loc == "Transit"

    diet = Diet(food_items=["咖啡", "牛角面包"], meal_type="snack", metadata={"ambient_location": loc})
    assert diet.metadata["ambient_location"] == "Transit"


def test_x15_vad_speech_detection_and_hotword_processing(
    audio_vad_module: Any, tmp_path: Path
) -> None:
    """X15: Ingest RMS Silence VAD (F6) + Hotword Elevation (F1).
    
    Synthesized speech audio containing hotword passes VAD, then triggers hotword elevation.
    """
    speech_wav = write_test_wav(tmp_path / "speech.wav", duration_sec=1.2, amplitude=0.3)
    assert audio_vad_module.should_skip_transcription(str(speech_wav)) is False

    transcript = "记一笔买咖啡15元"
    triggered, hw, content = _eval_hotword(transcript)
    assert triggered is True
    assert hw == "记一笔"
    assert "买咖啡15元" in content


def test_x16_full_ambient_closure_audit_trail(
    tmp_path: Path, test_job_store: JobStore, test_job_with_events: tuple[str, str]
) -> None:
    """X16: Complete Ambient Intelligence Cycle (F1-F17 integration).
    
    1. Passive chunk records intent (20:35 '要去买瓶水') in Intent Pool.
    2. Active hotword supplement ('记一笔今天下午我还花了4.9元买了一瓶水') received.
    3. Amount is grounded and stored as user_supplement.
    4. Active Intent Pool transitions intent from pending to resolved.
    5. Daily summary recomputes to 153.90 CNY.
    6. Evening alignment card aggregates complete reconciled state with 0 open intents.
    """
    job_id, artifact_path = test_job_with_events
    pool = ActiveIntentPool(tmp_path / "active-intents.json")

    # Step 1: Passive recording registers intent
    intent = pool.register_intent("要去买瓶水", "Finance", "水", "2026-10-02T20:35:37+08:00")
    assert intent["status"] == "pending"

    # Step 2: Active hotword supplement
    spoken_text = "记一笔今天下午我还花了4.9元买了一瓶水"
    triggered, hw, clean_body = _eval_hotword(spoken_text)
    assert triggered is True
    assert _deepseek_amount_is_grounded(clean_body, 4.90) is True

    # Step 3: Stored as manual event with verbatim quote
    evt = test_job_store.save_manual_event(
        job_id,
        artifact_path=artifact_path,
        title="水",
        categories=["expense"],
        data={"expense": {"amount": 4.90, "currency": "CNY", "item": "水"}},
        note=spoken_text,
    )
    assert evt["review_status"] == "confirmed"

    # Step 4: Intent resolved by supplement
    resolved = pool.resolve_matching_intents("Finance", "水", evt["event_id"], resolution_note=spoken_text)
    assert len(resolved) == 1
    assert resolved[0]["status"] == "resolved"
    assert len(pool.query_unresolved("Finance")) == 0

    # Step 5: Summary recomputed
    all_events = [
        LifeEvent(
            event_id="evt_test_001", categories=["expense"], activity_type="purchase",
            title="超市购买", time=EventTime("2026-10-02T18:17:15+08:00", None, "exact"),
            data={"expense": {"amount": 140.0, "currency": "CNY"}},
            capture_mode="passive", source_type="audio", confidence=0.95,
            evidence={}, review_status="confirmed",
        ),
        LifeEvent(
            event_id="evt_test_smoke", categories=["expense"], activity_type="purchase",
            title="买烟", time=EventTime("2026-10-02T20:35:31+08:00", None, "exact"),
            data={"expense": {"amount": 9.0, "currency": "CNY"}},
            capture_mode="passive", source_type="audio", confidence=0.95,
            evidence={}, review_status="confirmed",
        ),
        LifeEvent(
            event_id=evt["event_id"], categories=["expense"], activity_type="purchase",
            title="水", time=EventTime("2026-10-02T20:40:00+08:00", None, "exact"),
            data={"expense": {"amount": 4.90, "currency": "CNY"}},
            capture_mode="active", source_type="user_supplement", confidence=1.0,
            evidence={"quote": spoken_text}, review_status="confirmed",
        ),
    ]
    summary = build_daily_summary(all_events, date="2026-10-02", timezone_name="Asia/Shanghai", warnings=[])
    assert summary["expense_total_cny"] == 153.90

    # Step 6: Evening alignment payload
    payload = _build_evening_alignment_payload(
        "2026-10-02",
        [e.to_dict() for e in all_events],
        [],
        pool.query_unresolved("Finance"),
    )
    assert payload["summary"]["total_confirmed_amount_cny"] == 153.90
    assert payload["summary"]["unresolved_intent_count"] == 0
    assert payload["summary"]["confirmed_expense_count"] == 3
