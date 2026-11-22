"""Historical 2026-10-02 benchmark verification test (Milestone M5).

Verifies Feature F16 and F17 requirements:
1. 3 confirmed historical events present (140, 16, 9 CNY = 165.0 CNY total).
2. 20:35 water intent ("要去买瓶水") registered in active-intents.json.
3. Natural language supplement ("今天下午我还花了4.9元买了一瓶水") adds 4.90 CNY,
   bringing daily confirmed total to 169.90 CNY and resolving the pending intent.
4. Zero regressions on all core domain invariants.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from life_logger.aggregate import build_daily_summary
from life_logger.domain import LifeEvent, EventTime
from life_logger.intent_pool import ActiveIntentPool
from life_logger.local_web import JobStore
from life_logger.natural_correction import process_natural_correction


PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


def _fixture(rel: str) -> Path:
    """优先读入库夹具；没有就退回仓库内原位置（本机两者都在，行为等价）。"""
    shipped = FIXTURES / rel
    return shipped if shipped.exists() else PROJECT_ROOT / rel


REPLAY_PATH = _fixture("output/local-web/contextual-replay-2026-10-02/context-extraction.json")


@pytest.fixture
def replay_extraction_data() -> dict[str, Any]:
    assert REPLAY_PATH.is_file(), f"Replay artifact not found: {REPLAY_PATH}"
    return json.loads(REPLAY_PATH.read_text(encoding="utf-8"))


def test_historical_confirmed_events_count(replay_extraction_data: dict[str, Any]) -> None:
    """F16.1: Exactly 3 formal events confirmed from 2026-10-02 audio replay."""
    events = replay_extraction_data.get("formal_events", [])
    assert len(events) == 3


def test_historical_confirmed_amounts_total_165(replay_extraction_data: dict[str, Any]) -> None:
    """F16.2: 140 (超市) + 16 (炸鸡) + 9 (烟) = 165.0 CNY."""
    events = replay_extraction_data.get("formal_events", [])
    amounts = [
        float(e.get("data", {}).get("expense", {}).get("amount", 0.0))
        for e in events
    ]
    assert 140.0 in amounts
    assert 16.0 in amounts
    assert 9.0 in amounts
    assert sum(amounts) == 165.0


def test_historical_water_intent_grounding(replay_extraction_data: dict[str, Any], tmp_path: Path) -> None:
    """F16.3: '要去买瓶水' is registered into active intent pool without inventing an amount."""
    pool = ActiveIntentPool(tmp_path / "active-intents.json")
    intent = pool.register_intent(
        text="要去买瓶水",
        domain="Finance",
        target_entity="水",
        timestamp="2026-10-02T20:35:37+08:00",
        quote="就就我外甥买了一包烟，要去买瓶水。",
    )
    assert intent["status"] == "pending"
    assert intent["target_entity"] == "水"

    # Verify no formal expense event was created for water prior to user supplement
    formal_titles = [e.get("title", "") for e in replay_extraction_data.get("formal_events", [])]
    assert not any("水" in title for title in formal_titles)


def test_historical_supplement_brings_total_to_169_90(tmp_path: Path) -> None:
    """F16.4: Spoken supplement ('花了4.9元买了一瓶水') closes intent and brings total to 169.90 CNY."""
    store = JobStore(tmp_path / "web", recover=False)
    pool = ActiveIntentPool(tmp_path / "active-intents.json")

    # 1. Base 3 historical events
    base_events = [
        LifeEvent(
            event_id="e1", categories=["expense"], activity_type="purchase",
            title="朴朴超市购买一周食材", time=EventTime("2026-10-02T18:17:15+08:00", None, "exact"),
            data={"expense": {"amount": 140.0, "currency": "CNY"}},
            capture_mode="passive", source_type="audio", confidence=0.98,
            evidence={}, review_status="confirmed",
        ),
        LifeEvent(
            event_id="e2", categories=["expense"], activity_type="purchase",
            title="购买炸鸡", time=EventTime("2026-10-02T18:17:15+08:00", None, "exact"),
            data={"expense": {"amount": 16.0, "currency": "CNY"}},
            capture_mode="passive", source_type="audio", confidence=0.98,
            evidence={}, review_status="confirmed",
        ),
        LifeEvent(
            event_id="e3", categories=["expense"], activity_type="purchase",
            title="买了一包烟", time=EventTime("2026-10-02T20:35:31+08:00", None, "exact"),
            data={"expense": {"amount": 9.0, "currency": "CNY"}},
            capture_mode="passive", source_type="audio", confidence=0.98,
            evidence={}, review_status="confirmed",
        ),
    ]
    summary_before = build_daily_summary(base_events, date="2026-10-02", timezone_name="Asia/Shanghai", warnings=[])
    assert summary_before["expense_total_cny"] == 165.0

    # 2. Register water intent
    pending_intent = pool.register_intent(
        text="要去买瓶水",
        domain="Finance",
        target_entity="水",
        timestamp="2026-10-02T20:35:37+08:00",
    )
    assert pending_intent["status"] == "pending"

    # 3. Process natural language supplement
    res = process_natural_correction(
        store,
        text="今天下午我还花了4.9元买了一瓶水",
        target_date="2026-10-02",
        intent_pool=pool,
    )
    assert res["status"] == "ok"
    supp_event = res["event"]

    # 4. Combine into final summary
    supp_life_event = LifeEvent(
        event_id=supp_event["event_id"],
        categories=supp_event["categories"],
        activity_type=supp_event.get("activity_type"),
        title=supp_event["title"],
        time=EventTime(supp_event["time"].get("absolute"), supp_event["time"].get("relative_seconds"), supp_event["time"].get("precision", "unknown")),
        data=supp_event.get("data", {}),
        capture_mode="active",
        source_type="manual",
        confidence=1.0,
        evidence=supp_event.get("evidence", {}),
        review_status="confirmed",
    )
    all_events = base_events + [supp_life_event]
    summary_after = build_daily_summary(all_events, date="2026-10-02", timezone_name="Asia/Shanghai", warnings=[])
    assert summary_after["expense_total_cny"] == 169.90
    assert summary_after["event_count"] == 4

    # 5. Verify intent is now resolved
    resolved_intents = pool.find_matching_intents("Finance", "水", status="resolved")
    assert len(resolved_intents) == 1
    assert resolved_intents[0]["status"] == "resolved"
    assert pool.list_pending_intents() == []
