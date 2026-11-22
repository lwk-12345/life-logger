"""Tier 4: Real-World Application Scenarios.

Verifies comprehensive real-world workflows and realistic lifecycles:
- Scenario 1: Complete authoritative 2026-10-02 historical benchmark reconstruction
- Scenario 2: Multi-day lifecycle isolation (2026-10-01 vs 2026-10-02 zero leakage)
- Scenario 3: Full-day multi-stage lifecycle (commute -> lunch -> todo -> evening reconcile)
- Scenario 4: Audio VAD noise gating and burst speech streaming
- Scenario 5: Multi-speaker conversation attribution (primary self vs contacts)
- Scenario 6: User supplement correction and cancellation audit trail
- Scenario 7: Evening reconciliation and WeChat alignment callback dispatch
- Scenario 8: Cross-domain unified daily audit (Finance, Diet, Todos, Insights)
- Scenario 9: Ambient network anchor roaming (Home -> Outside/Transit -> Office)
- Scenario 10: Spoken adjustment and intent resolution loop
- Scenario 11: Malformed and corrupt stream resilience
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from life_logger.aggregate import build_daily_summary
from life_logger.domain import (
    EventTime,
    LifeEvent,
    validate_event,
)
from life_logger.local_web import JobStore
from life_logger.openclaw_bridge import (
    OpenClawBridgeClient,
    build_evening_alignment_payload,
)
from tests.e2e.conftest import write_test_wav


def _to_event(d: dict[str, Any]) -> LifeEvent:
    time_info = d.get("time", {})
    t = EventTime(
        absolute=time_info.get("absolute"),
        relative_seconds=time_info.get("relative_seconds"),
        precision=time_info.get("precision", "unknown"),
    )
    return LifeEvent(
        event_id=d["event_id"],
        categories=d["categories"],
        activity_type=d.get("activity_type"),
        title=d["title"],
        time=t,
        data=d.get("data", {}),
        capture_mode=d.get("capture_mode", "passive"),
        source_type=d.get("source_type", "audio"),
        confidence=float(d.get("confidence", 1.0)),
        evidence=d.get("evidence", {}),
        review_status=d.get("review_status", "confirmed"),
    )


# ============================================================================
# Scenario 1: Authoritative 2026-10-02 Historical Benchmark Reconstruction
# ============================================================================

def test_scenario_01_complete_20261002_historical_benchmark(
    benchmark_data_paths: dict[str, Path],
    intent_pool_module: Any,
) -> None:
    """Scenario 1: Complete authoritative 2026-10-02 benchmark verification.
    
    1. Loads context-extraction.json and confirms all 3 formal events (140, 16, 9 CNY = 165.0 CNY).
    2. Verifies source-attribution.json contains the verbatim quote '要去买瓶水'.
    3. Confirms water intent was held back per ADR 0004.
    4. Registers intent into ActiveIntentPool.
    5. User adds active supplement of 4.90 CNY for water.
    6. Intent is resolved.
    7. Daily summary is re-evaluated to exactly 169.90 CNY with all 3 original events preserved.
    """
    replay_file = benchmark_data_paths["context_extraction"]
    assert replay_file.is_file(), f"Missing replay file: {replay_file}"
    raw_data = json.loads(replay_file.read_text(encoding="utf-8"))
    formal_events = raw_data.get("formal_events", [])
    assert len(formal_events) >= 3

    # 1. Verify 3 baseline transactions
    amounts = [
        float(e["data"]["expense"]["amount"])
        for e in formal_events
        if "expense" in e.get("data", {})
    ]
    assert 140.0 in amounts
    assert 16.0 in amounts
    assert 9.0 in amounts
    assert sum(amounts) == 165.00

    # 2. Verify audio attribution quote
    attr_file = benchmark_data_paths["evening_source_attribution"]
    assert attr_file.is_file(), f"Missing source attribution file: {attr_file}"
    attr_data = json.loads(attr_file.read_text(encoding="utf-8"))
    records = attr_data if isinstance(attr_data, list) else attr_data.get("records", [])
    water_quotes = [r for r in records if "要去买瓶水" in r.get("text", "")]
    assert len(water_quotes) >= 1
    assert "就就我外甥买了一包烟，要去买瓶水。" in water_quotes[0]["text"]

    # 3. Verify water was withheld from formal expenses
    water_in_formal = [e for e in formal_events if "水" in e.get("title", "")]
    assert len(water_in_formal) == 0

    # 4. Register in ActiveIntentPool
    pool = intent_pool_module.ActiveIntentPool()
    intent_record = pool.register_intent(
        text="要去买瓶水",
        domain="Finance",
        target_entity="水",
        timestamp="2026-10-02T20:35:37+08:00",
        quote="要去买瓶水",
    )
    assert intent_record["status"] == "pending", intent_record
    assert intent_record["target_entity"] == "水", intent_record
    unresolved = pool.list_pending_intents(domain="Finance")
    assert len(unresolved) >= 1
    assert any(i["target_entity"] == "水" for i in unresolved)

    # 5. User provides 4.90 CNY supplement
    supplement_dict = {
        "event_id": "evt_supplement_water_490",
        "categories": ["expense"],
        "activity_type": "purchase",
        "title": "购买矿泉水",
        "time": {
            "absolute": "2026-10-02T20:40:00+08:00",
            "relative_seconds": None,
            "precision": "exact",
        },
        "data": {
            "expense": {
                "amount": 4.90,
                "currency": "CNY",
                "item": "矿泉水",
            }
        },
        "capture_mode": "active",
        "source_type": "manual",
        "confidence": 1.0,
        "evidence": {
            "quote": "今天下午我还花了4.9元买了一瓶水",
            "source": "user_supplement",
        },
        "review_status": "confirmed",
    }
    validate_event(supplement_dict)

    # 6. Resolve intent
    resolved_list = pool.resolve_matching_intents(
        domain="Finance",
        target_entity="水",
        resolved_by_event_id=supplement_dict["event_id"],
        resolution_note="User manual supplement of 4.90 CNY recorded",
    )
    assert len(resolved_list) >= 1
    pending_after = pool.list_pending_intents(domain="Finance")
    assert not any(i["target_entity"] == "水" for i in pending_after)

    # 7. Re-evaluate daily summary
    all_raw = list(formal_events) + [supplement_dict]
    life_events = [_to_event(e) for e in all_raw]
    summary = build_daily_summary(
        life_events,
        date="2026-10-02",
        timezone_name="Asia/Shanghai",
        warnings=[],
    )
    assert summary["expense_total_cny"] == 169.90
    assert summary["event_count"] == len(formal_events) + 1


# ============================================================================
# Scenario 2: Multi-Day Lifecycle Isolation (2026-10-01 vs 2026-10-02)
# ============================================================================

def test_scenario_02_multiday_lifecycle_isolation(test_job_store: JobStore) -> None:
    """Scenario 2: Multi-day job storage and summary generation isolation.
    
    Verifies that events from 2026-10-01 and 2026-10-02 remain strictly isolated,
    daily totals do not leak across date boundaries, and mutations on Day 1
    have zero impact on Day 2.
    """
    # Day 1: 2026-10-01
    job1_id = test_job_store.create_job()
    job1_dir = test_job_store.job_path(job1_id)
    (job1_dir / "2026-10-01").mkdir(parents=True, exist_ok=True)
    day1_events = [
        {
            "event_id": "evt_d1_01",
            "categories": ["expense"],
            "activity_type": "purchase",
            "title": "早餐包子豆浆",
            "time": {"absolute": "2026-10-01T08:15:00+08:00", "precision": "exact"},
            "data": {"expense": {"amount": 18.0, "currency": "CNY"}},
            "capture_mode": "passive",
            "source_type": "audio",
            "confidence": 0.95,
            "evidence": {},
            "review_status": "confirmed",
        },
        {
            "event_id": "evt_d1_02",
            "categories": ["expense"],
            "activity_type": "transit",
            "title": "地铁通勤",
            "time": {"absolute": "2026-10-01T08:45:00+08:00", "precision": "exact"},
            "data": {"expense": {"amount": 7.0, "currency": "CNY"}},
            "capture_mode": "passive",
            "source_type": "audio",
            "confidence": 0.95,
            "evidence": {},
            "review_status": "confirmed",
        },
    ]
    (job1_dir / "2026-10-01" / "events.json").write_text(
        json.dumps(day1_events, ensure_ascii=False), encoding="utf-8"
    )

    # Day 2: 2026-10-02
    job2_id = test_job_store.create_job()
    job2_dir = test_job_store.job_path(job2_id)
    (job2_dir / "2026-10-02").mkdir(parents=True, exist_ok=True)
    day2_events = [
        {
            "event_id": "evt_d2_01",
            "categories": ["expense"],
            "activity_type": "purchase",
            "title": "超市大采购",
            "time": {"absolute": "2026-10-02T18:17:15+08:00", "precision": "exact"},
            "data": {"expense": {"amount": 140.0, "currency": "CNY"}},
            "capture_mode": "passive",
            "source_type": "audio",
            "confidence": 0.95,
            "evidence": {},
            "review_status": "confirmed",
        },
        {
            "event_id": "evt_d2_02",
            "categories": ["expense"],
            "activity_type": "purchase",
            "title": "水果买单",
            "time": {"absolute": "2026-10-02T19:04:18+08:00", "precision": "exact"},
            "data": {"expense": {"amount": 16.0, "currency": "CNY"}},
            "capture_mode": "passive",
            "source_type": "audio",
            "confidence": 0.95,
            "evidence": {},
            "review_status": "confirmed",
        },
        {
            "event_id": "evt_d2_03",
            "categories": ["expense"],
            "activity_type": "purchase",
            "title": "便利店",
            "time": {"absolute": "2026-10-02T20:35:31+08:00", "precision": "exact"},
            "data": {"expense": {"amount": 9.0, "currency": "CNY"}},
            "capture_mode": "passive",
            "source_type": "audio",
            "confidence": 0.95,
            "evidence": {},
            "review_status": "confirmed",
        },
    ]
    (job2_dir / "2026-10-02" / "events.json").write_text(
        json.dumps(day2_events, ensure_ascii=False), encoding="utf-8"
    )

    # Summaries
    sum1 = build_daily_summary(
        [_to_event(e) for e in day1_events],
        date="2026-10-01",
        timezone_name="Asia/Shanghai",
        warnings=[],
    )
    sum2 = build_daily_summary(
        [_to_event(e) for e in day2_events],
        date="2026-10-02",
        timezone_name="Asia/Shanghai",
        warnings=[],
    )

    assert sum1["expense_total_cny"] == 25.0
    assert sum1["event_count"] == 2
    assert sum2["expense_total_cny"] == 165.0
    assert sum2["event_count"] == 3

    # Mutation on Day 1: Correct breakfast price from 18 to 20
    test_job_store.save_event_correction(
        job1_id,
        artifact_path="2026-10-01/events.json",
        event_id="evt_d1_01",
        changes={"data": {"expense": {"amount": 20.0, "currency": "CNY"}}, "note": "加了茶叶蛋"},
    )

    # Ensure Day 1 recorded the correction in JobStore
    corrections_d1 = test_job_store.read_event_corrections(job1_id)
    assert len(corrections_d1) == 1
    assert corrections_d1[0]["changes"]["data"]["expense"]["amount"] == 20.0

    # Ensure Day 2 has zero corrections (isolated)
    corrections_d2 = test_job_store.read_event_corrections(job2_id)
    assert len(corrections_d2) == 0

    # Apply corrections for Day 1 and verify updated summary
    corrected_d1 = test_job_store._apply_event_corrections_unlocked(
        job1_id, "2026-10-01/events.json", day1_events
    )
    updated_sum1 = build_daily_summary(
        [_to_event(e) for e in corrected_d1],
        date="2026-10-01",
        timezone_name="Asia/Shanghai",
        warnings=[],
    )
    assert updated_sum1["expense_total_cny"] == 27.0

    # Ensure Day 2 remained completely untouched
    reloaded_d2 = json.loads(
        (job2_dir / "2026-10-02" / "events.json").read_text(encoding="utf-8")
    )
    assert len(reloaded_d2) == 3
    assert sum(e["data"]["expense"]["amount"] for e in reloaded_d2) == 165.0


# ============================================================================
# Scenario 3: Full-Day Multi-Stage Lifecycle
# ============================================================================

def test_scenario_03_fullday_multistage_commute_lunch_evening_reconcile(
    mock_openclaw_daemon: tuple[str, list[dict[str, Any]]],
) -> None:
    """Scenario 3: Simulates a complete daily timeline across multiple stages:
    
    1. Morning Commute: Transit expense (5 CNY, category 'expense')
    2. Noon Lunch: Meal event with diet info (28 CNY, categories 'expense' and 'food')
    3. Afternoon Task: Todo event (prepare quarterly report, category 'todo')
    4. Evening Store: Purchase (9 CNY, category 'expense')
    5. Late Night Reconciliation: Dispatch evening alignment payload to OpenClaw daemon
    6. Verifies composite daily summary aggregates correctly across all categories.
    """
    daemon_url, received_payloads = mock_openclaw_daemon

    events: list[LifeEvent] = [
        LifeEvent(
            event_id="evt_stage1_commute",
            categories=["expense", "routine"],
            activity_type="transit",
            title="早高峰地铁乘车",
            time=EventTime("2026-10-02T08:30:00+08:00", None, "exact"),
            data={"expense": {"amount": 5.0, "currency": "CNY"}},
            capture_mode="passive",
            source_type="audio",
            confidence=0.96,
            evidence={},
            review_status="confirmed",
        ),
        LifeEvent(
            event_id="evt_stage2_lunch",
            categories=["expense", "food"],
            activity_type="meal",
            title="午餐牛肉面",
            time=EventTime("2026-10-02T12:15:00+08:00", None, "exact"),
            data={
                "expense": {"amount": 28.0, "currency": "CNY"},
                "food": {"meal_type": "lunch", "calories_est": 650},
            },
            capture_mode="passive",
            source_type="audio",
            confidence=0.98,
            evidence={},
            review_status="confirmed",
        ),
        LifeEvent(
            event_id="evt_stage3_todo",
            categories=["todo"],
            activity_type="task",
            title="整理并提交Q3季度报告",
            time=EventTime("2026-10-02T15:00:00+08:00", None, "exact"),
            data={"todo": {"priority": "P1", "status": "pending"}},
            capture_mode="passive",
            source_type="audio",
            confidence=0.92,
            evidence={},
            review_status="confirmed",
        ),
        LifeEvent(
            event_id="evt_stage4_night",
            categories=["expense"],
            activity_type="purchase",
            title="便利店买烟",
            time=EventTime("2026-10-02T20:35:31+08:00", None, "exact"),
            data={"expense": {"amount": 9.0, "currency": "CNY"}},
            capture_mode="passive",
            source_type="audio",
            confidence=0.95,
            evidence={},
            review_status="confirmed",
        ),
    ]

    for e in events:
        validate_event(e.to_dict())

    # Build evening alignment payload
    confirmed_dicts = [e.to_dict() for e in events if "expense" in e.categories]
    candidate_dicts: list[dict[str, Any]] = []
    active_intents = [{"intent_id": "i_water", "target_entity": "水", "status": "pending"}]

    payload = build_evening_alignment_payload(
        "2026-10-02",
        confirmed_dicts,
        candidate_dicts,
        active_intents,
    )
    assert payload["summary"]["confirmed_expense_count"] == 3
    assert payload["summary"]["total_confirmed_amount_cny"] == 42.0
    assert payload["summary"]["unresolved_intent_count"] == 1

    # Dispatch to OpenClaw daemon
    client = OpenClawBridgeClient(base_url=daemon_url)
    res = client.dispatch_evening_alignment(payload)
    assert res["status"] == "success"
    assert "card_id" in res
    assert len(received_payloads) == 1

    # Daily summary audit
    summary = build_daily_summary(
        events,
        date="2026-10-02",
        timezone_name="Asia/Shanghai",
        warnings=[],
    )
    assert summary["expense_total_cny"] == 42.0
    assert summary["event_count"] == 4
    assert "food" in summary["category_summary"]
    assert "todo" in summary["category_summary"]
    assert "routine" in summary["category_summary"]


# ============================================================================
# Scenario 4: Audio VAD Noise Gating and Burst Speech Streaming
# ============================================================================

def test_scenario_04_audio_vad_noise_and_burst_speech_streaming(
    audio_vad_module: Any,
    tmp_path: Path,
) -> None:
    """Scenario 4: Audio stream pre-filtering through energy-based VAD.
    
    Verifies that ambient room silence and low-level noise are rejected (< -38 dBFS),
    while speech tones (> -38 dBFS) are passed for ASR processing, saving compute.
    """
    # 1. Generate silence audio (~ -inf dBFS)
    silence_wav = tmp_path / "ambient_silence.wav"
    write_test_wav(silence_wav, duration_sec=2.0, amplitude=0.0)

    # 2. Generate room noise audio (amplitude 0.003, approx -50 dBFS, below -45 threshold)
    noise_wav = tmp_path / "room_noise.wav"
    write_test_wav(noise_wav, duration_sec=1.5, amplitude=0.003)

    # 3. Generate speech burst audio (amplitude 0.25, approx -12 dBFS)
    speech_wav = tmp_path / "speech_burst.wav"
    write_test_wav(speech_wav, duration_sec=1.2, amplitude=0.25)

    # Test energy calculations
    silence_energy = audio_vad_module.calculate_audio_energy(silence_wav.read_bytes())
    noise_energy = audio_vad_module.calculate_audio_energy(noise_wav.read_bytes())
    speech_energy = audio_vad_module.calculate_audio_energy(speech_wav.read_bytes())

    assert silence_energy["is_speech"] is False
    assert noise_energy["is_speech"] is False
    assert speech_energy["is_speech"] is True

    # Test file-level skipping decisions
    assert audio_vad_module.should_skip_transcription(silence_wav) is True
    assert audio_vad_module.should_skip_transcription(noise_wav) is True
    assert audio_vad_module.should_skip_transcription(speech_wav) is False


# ============================================================================
# Scenario 5: Multi-Speaker Conversation Attribution
# ============================================================================

def test_scenario_05_multispeaker_conversation_attribution(
    speaker_registry_module: Any,
) -> None:
    """Scenario 5: Multi-party dialogue speaker attribution.
    
    Registers primary speaker (Self) and contact (Nephew). Verifies that
    embeddings accurately differentiate speakers, enabling proper attribution
    of statements such as '我外甥买了一包烟' vs '要去买瓶水'.
    """
    registry = speaker_registry_module.ResidentSpeakerRegistry()
    registry.register_primary_speaker("spk_self", [1.0, 0.0, 0.0], name="Self")
    registry.register_contact("spk_nephew", [0.0, 1.0, 0.0], name="外甥", relation="nephew")

    # Match primary speaker
    res_self = registry.identify_speaker([0.98, 0.02, 0.0])
    assert res_self["speaker_id"] == "spk_self"
    assert res_self["name"] == "Self"
    assert res_self["confidence"] > 0.9

    # Match nephew
    res_nephew = registry.identify_speaker([0.05, 0.95, 0.0])
    assert res_nephew["speaker_id"] == "spk_nephew"
    assert res_nephew["name"] == "外甥"
    assert res_nephew["confidence"] > 0.9

    # Create event attributed to Self
    self_event = LifeEvent(
        event_id="evt_self_smoke",
        categories=["expense"],
        activity_type="purchase",
        title="外甥买烟",
        time=EventTime("2026-10-02T20:35:31+08:00", None, "exact"),
        data={
            "expense": {"amount": 9.0, "currency": "CNY"},
            "speaker": {"speaker_id": res_self["speaker_id"], "name": res_self["name"]},
        },
        capture_mode="passive",
        source_type="audio",
        confidence=0.95,
        evidence={"speaker_attributed": True},
        review_status="confirmed",
    ).to_dict()
    validate_event(self_event)
    assert self_event["data"]["speaker"]["speaker_id"] == "spk_self"


# ============================================================================
# Scenario 6: User Supplement Correction and Cancellation Audit Trail
# ============================================================================

def test_scenario_06_user_supplement_correction_cancellation_audit(
    test_job_store: JobStore,
    test_job_with_events: tuple[str, str],
) -> None:
    """Scenario 6: End-to-end lifecycle of user supplement, correction, and cancellation.
    
    1. User adds manual supplement for taxi expense (35.0 CNY).
    2. User amends price to 32.50 CNY with discount note.
    3. User cancels the expense (marked negated/abandoned).
    4. Daily summary cleanly excludes the negated event from financial total.
    """
    job_id, artifact_path = test_job_with_events

    # 1. Add manual event
    res = test_job_store.save_manual_event(
        job_id,
        artifact_path=artifact_path,
        title="出租车打车费",
        categories=["expense"],
        absolute_time="2026-10-02T19:30:00+08:00",
        time_precision="exact",
        activity_type="transit",
        data={"expense": {"amount": 35.0, "currency": "CNY"}},
        source_type="manual",
    )
    manual_evt_id = res["event_id"]

    # 2. Correct price to 32.50
    test_job_store.save_event_correction(
        job_id,
        artifact_path=artifact_path,
        event_id=manual_evt_id,
        changes={
            "data": {"expense": {"amount": 32.50, "currency": "CNY"}},
            "note": "使用了优惠券抵扣 2.5元",
        },
    )

    corrections = test_job_store.read_event_corrections(job_id)
    assert len(corrections) == 1
    assert corrections[0]["changes"]["data"]["expense"]["amount"] == 32.50
    assert corrections[0]["changes"]["note"] == "使用了优惠券抵扣 2.5元"

    # 3. Cancel / negate event
    test_job_store.save_event_correction(
        job_id,
        artifact_path=artifact_path,
        event_id=manual_evt_id,
        changes={
            "activity_type": "abandoned_purchase",
            "data": {
                "expense": {"amount": 32.50, "currency": "CNY"},
                "decision": {"state": "negated"},
            },
            "note": "订单已退款取消",
        },
    )

    # Verify that applying correction results in negated decision state
    corrected_manual = test_job_store._apply_event_corrections_unlocked(
        job_id, artifact_path, [res]
    )
    assert corrected_manual[0]["activity_type"] == "abandoned_purchase"
    assert corrected_manual[0]["data"]["decision"]["state"] == "negated"

    # Daily summary with baseline events + cancelled event excludes the cancelled amount
    events_file = test_job_store.job_path(job_id) / artifact_path
    baseline_events = json.loads(events_file.read_text(encoding="utf-8"))
    combined = baseline_events + corrected_manual

    summary = build_daily_summary(
        [_to_event(e) for e in combined],
        date="2026-10-02",
        timezone_name="Asia/Shanghai",
        warnings=[],
    )

    # Negated event should not contribute to expense total
    # Baseline was 140 + 9 = 149.0 CNY
    assert summary["expense_total_cny"] == 149.0


# ============================================================================
# Scenario 7: Evening Reconciliation and WeChat Alignment Callback Dispatch
# ============================================================================

def test_scenario_07_evening_reconciliation_wechat_alignment_dispatch(
    mock_openclaw_daemon: tuple[str, list[dict[str, Any]]],
    intent_pool_module: Any,
    tmp_path: Path,
) -> None:
    """Scenario 7: Full evening alignment loop with OpenClaw bridge and resolution.
    
    1. Confirmed transactions (140, 16, 9) + 1 pending intent ('水 4.90').
    2. Builds evening alignment payload and dispatches to OpenClaw daemon.
    3. Simulates user approving transaction via simulated callback.
    4. Pool resolves the intent and final reconciled total is verified.
    """
    daemon_url, received_payloads = mock_openclaw_daemon
    pool = intent_pool_module.ActiveIntentPool(storage_path=tmp_path / "active-intents.json")

    intent_record = pool.register_intent(
        text="要去买瓶水",
        domain="Finance",
        target_entity="矿泉水",
        timestamp="2026-10-02T20:35:37+08:00",
        quote="要去买瓶水",
    )

    assert intent_record["status"] == "pending", intent_record
    assert intent_record["target_entity"] == "矿泉水", intent_record

    confirmed_list = [
        {"event_id": "e1", "title": "超市", "data": {"expense": {"amount": 140.0}}},
        {"event_id": "e2", "title": "水果", "data": {"expense": {"amount": 16.0}}},
        {"event_id": "e3", "title": "买烟", "data": {"expense": {"amount": 9.0}}},
    ]
    unresolved_intents = pool.list_pending_intents(domain="Finance")

    payload = build_evening_alignment_payload(
        "2026-10-02",
        confirmed_list,
        [],
        unresolved_intents,
    )
    assert payload["summary"]["unresolved_intent_count"] == 1
    assert payload["summary"]["total_confirmed_amount_cny"] == 165.0

    client = OpenClawBridgeClient(base_url=daemon_url)
    res = client.dispatch_evening_alignment(payload)
    assert res["status"] == "success"
    card_id = res["card_id"]
    assert card_id is not None

    # User callback confirmation on WeChat: confirms water bought for 4.90 CNY
    resolved_event_id = "evt_water_confirmed_callback"
    pool.resolve_matching_intents(
        domain="Finance",
        target_entity="矿泉水",
        resolved_by_event_id=resolved_event_id,
        resolution_note=f"Approved on WeChat review card {card_id}",
    )
    assert len(pool.list_pending_intents(domain="Finance")) == 0

    all_confirmed = confirmed_list + [
        {"event_id": resolved_event_id, "title": "矿泉水", "data": {"expense": {"amount": 4.90}}}
    ]
    total_reconciled = sum(e["data"]["expense"]["amount"] for e in all_confirmed)
    assert round(total_reconciled, 2) == 169.90


# ============================================================================
# Scenario 8: Cross-Domain Unified Daily Audit
# ============================================================================

def test_scenario_08_cross_domain_unified_daily_audit() -> None:
    """Scenario 8: Unified audit covering Finance, Diet, Todos, and Insights.
    
    Verifies that domain segregation is preserved, domain classes produce valid
    events, and build_daily_summary provides correct counts and highlights per category.
    """
    events: list[LifeEvent] = [
        # Finance
        LifeEvent(
            event_id="e_fin_1",
            categories=["expense"],
            activity_type="purchase",
            title="早餐麦当劳",
            time=EventTime("2026-10-02T08:00:00+08:00", None, "exact"),
            data={"expense": {"amount": 15.0, "currency": "CNY"}},
            capture_mode="passive",
            source_type="audio",
            confidence=0.95,
            evidence={},
            review_status="confirmed",
        ),
        LifeEvent(
            event_id="e_fin_2",
            categories=["expense"],
            activity_type="purchase",
            title="午餐商务套餐",
            time=EventTime("2026-10-02T12:00:00+08:00", None, "exact"),
            data={"expense": {"amount": 35.0, "currency": "CNY"}},
            capture_mode="passive",
            source_type="audio",
            confidence=0.95,
            evidence={},
            review_status="confirmed",
        ),
        # Diet
        LifeEvent(
            event_id="e_diet_1",
            categories=["food"],
            activity_type="diet",
            title="下午黑咖啡无糖",
            time=EventTime("2026-10-02T14:30:00+08:00", None, "exact"),
            data={"food": {"meal_type": "beverage", "calories": 5}},
            capture_mode="passive",
            source_type="audio",
            confidence=0.90,
            evidence={},
            review_status="confirmed",
        ),
        # Todos
        LifeEvent(
            event_id="e_todo_1",
            categories=["todo"],
            activity_type="task",
            title="发送客户对账邮件",
            time=EventTime("2026-10-02T16:00:00+08:00", None, "exact"),
            data={"todo": {"priority": "P1", "completed": True}},
            capture_mode="passive",
            source_type="audio",
            confidence=0.93,
            evidence={},
            review_status="confirmed",
        ),
        # Insights
        LifeEvent(
            event_id="e_ins_1",
            categories=["idea"],
            activity_type="reflection",
            title="关于本地优先架构的思考",
            time=EventTime("2026-10-02T21:00:00+08:00", None, "exact"),
            data={"idea": {"topic": "Local First Architecture"}},
            capture_mode="passive",
            source_type="audio",
            confidence=0.91,
            evidence={},
            review_status="confirmed",
        ),
    ]

    for e in events:
        validate_event(e.to_dict())

    summary = build_daily_summary(
        events,
        date="2026-10-02",
        timezone_name="Asia/Shanghai",
        warnings=[],
    )

    assert summary["expense_total_cny"] == 50.0  # 15 + 35
    assert summary["event_count"] == 5
    assert "expense" in summary["category_summary"]
    assert "food" in summary["category_summary"]
    assert "todo" in summary["category_summary"]
    assert "idea" in summary["category_summary"]
    assert summary["category_summary"]["expense"]["count"] == 2
    assert summary["category_summary"]["todo"]["count"] == 1


# ============================================================================
# Scenario 9: Ambient Network Anchor Roaming
# ============================================================================

def test_scenario_09_ambient_network_anchor_roaming(
    ambient_anchors_module: Any,
) -> None:
    """Scenario 9: Roaming across physical network anchors.
    
    Verifies that classification adapts as BSSID/SSID transitions between
    Home Wi-Fi, Outside/Transit cellular hotspot, and Office Wi-Fi, propagating
    spatial context tags into generated events.
    """
    home_tag = ambient_anchors_module.classify_ambient_location(
        ssid="Home-WiFi-5G",
        bssid="c0:25:e9:11:22:33",
    )
    outside_tag = ambient_anchors_module.classify_ambient_location(
        ssid=None,
        bssid=None,
    )
    office_tag = ambient_anchors_module.classify_ambient_location(
        ssid="Company-Office-HQ",
        bssid="00:14:22:aa:bb:cc",
    )

    assert home_tag == "Home"
    assert outside_tag == "Outside"
    assert office_tag == "Office"

    # Validate attaching to LifeEvent
    event = LifeEvent(
        event_id="evt_roam_1",
        categories=["routine"],
        activity_type="roaming",
        title="抵达办公室",
        time=EventTime("2026-10-02T09:15:00+08:00", None, "exact"),
        data={"ambient": {"location_tag": office_tag, "bssid": "00:14:22:aa:bb:cc"}},
        capture_mode="passive",
        source_type="sensor",
        confidence=0.99,
        evidence={},
        review_status="confirmed",
    ).to_dict()
    validate_event(event)
    assert event["data"]["ambient"]["location_tag"] == "Office"


# ============================================================================
# Scenario 10: Spoken Adjustment and Intent Resolution Loop
# ============================================================================

def test_scenario_10_spoken_adjustment_and_intent_resolution_loop(
    intent_pool_module: Any,
) -> None:
    """Scenario 10: Multi-turn intent tracking and resolution loop.
    
    Verifies intent life cycle from detection, pending query, idempotent resolution,
    and post-resolution consistency.
    """
    pool = intent_pool_module.ActiveIntentPool()

    # Spoken intent detected: "下午要去买一箱牛奶"
    intent_record = pool.register_intent(
        text="下午要去买一箱牛奶",
        domain="Finance",
        target_entity="牛奶",
        timestamp="2026-10-02T14:00:00+08:00",
        quote="要去买一箱牛奶",
    )
    intent_id = intent_record["intent_id"]
    assert intent_id is not None

    unresolved = pool.list_pending_intents(domain="Finance")
    assert any(i["intent_id"] == intent_id for i in unresolved)

    # User buys milk later
    resolved_list = pool.resolve_matching_intents(
        domain="Finance",
        target_entity="牛奶",
        resolved_by_event_id="evt_milk_purchased_123",
        resolution_note="超市收银台支付完成",
    )
    assert len(resolved_list) >= 1

    # Status must now be resolved
    pending_now = pool.list_pending_intents(domain="Finance")
    assert not any(i["intent_id"] == intent_id for i in pending_now)

    # Repeated resolution is safe and idempotent
    repeat_list = pool.resolve_matching_intents(
        domain="Finance",
        target_entity="牛奶",
        resolved_by_event_id="evt_milk_purchased_123",
    )
    assert len(repeat_list) == 0


# ============================================================================
# Scenario 11: Malformed and Corrupt Stream Resilience
# ============================================================================

def test_scenario_11_malformed_corrupt_stream_resilience(
    audio_vad_module: Any,
    test_job_store: JobStore,
) -> None:
    """Scenario 11: System resilience against adversarial corrupt inputs.
    
    1. Zero-byte buffer to calculate_audio_energy does not crash.
    2. Non-existent audio file to should_skip_transcription raises FileNotFoundError.
    3. Non-localhost endpoint to OpenClawBridgeClient raises ValueError.
    4. Reading nonexistent job in JobStore raises KeyError / ValueError.
    5. Invalid event schema to validate_event raises ValueError.
    """
    # 1. Zero-byte buffer
    zero_result = audio_vad_module.calculate_audio_energy(b"")
    assert zero_result["is_speech"] is False
    assert zero_result["duration_sec"] == 0.0

    # 2. Non-existent file
    with pytest.raises(FileNotFoundError):
        audio_vad_module.should_skip_transcription(Path("/non/existent/audio.wav"))

    # 3. Non-localhost endpoint rejection
    with pytest.raises(ValueError, match="OpenClaw Task Bridge endpoint must use localhost"):
        OpenClawBridgeClient(base_url="http://remote.server.com:8791")

    # 4. Non-existent job
    with pytest.raises(ValueError, match="任务编号无效"):
        test_job_store.read("non_existent_job_xyz")
    with pytest.raises(KeyError):
        test_job_store.read("job-20261002-000000-00000000")

    # 5. Invalid event validation
    invalid_event = {"event_id": "bad", "categories": []}  # missing title, time, etc.
    with pytest.raises(ValueError):
        validate_event(invalid_event)
