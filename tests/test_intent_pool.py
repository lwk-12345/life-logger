"""Comprehensive unit and integration tests for ActiveIntentPool and domain decoupling.

Verifies Features F8, F9, and F10:
- F8: Persistent active intent pool (active-intents.json), lifecycle management
  (pending -> resolved | dismissed | expired).
- F9: Cross-batch intent resolution with natural language / LifeEvent supplement.
- F10: Multi-domain schema decoupling (Finance, Diet, Todos, Insights).
- Integration: Hooking contextual extraction into the intent pool per ADR 0004.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

import pytest

from life_logger.contextual_extraction import run_contextual_extraction
from life_logger.domain import (
    Diet,
    DomainEvent,
    EventTime,
    Finance,
    Insights,
    LifeEvent,
    Todos,
    calculate_net_amount,
    domain_event_from_life_event,
    is_dietary_intake,
    is_grocery_purchase,
    validate_diet,
    validate_domain_payload,
    validate_finance,
    validate_insights,
    validate_todos,
)
from life_logger.intent_pool import (
    ActiveIntentPool,
    stable_intent_id,
    extract_planned_clause,
    extract_target_entity,
    infer_intent_domain,
    is_planned_utterance,
    normalize_domain_name,
    validate_intent,
)

# ============================================================================
# Feature F8: Persistent Active Intent Pool Tests
# ============================================================================


def test_intent_pool_initialization_empty(tmp_path: Path) -> None:
    """Pool starts empty when storage path does not exist."""
    pool_file = tmp_path / "active-intents.json"
    pool = ActiveIntentPool(pool_file)
    assert len(pool) == 0
    assert pool.query_unresolved() == []
    assert not pool_file.exists()


def test_intent_registration_persists_to_disk(tmp_path: Path) -> None:
    """Registering an intent saves valid JSON adhering to active-intents-v1 schema."""
    pool_file = tmp_path / "active-intents.json"
    pool = ActiveIntentPool(pool_file)

    intent = pool.register_intent(
        text="要去买瓶水",
        domain="Finance",
        target_entity="水",
        timestamp="2026-10-02T20:35:37+08:00",
        source_segment_id="seg_water_01",
        metadata={"note": "20:35 ambient speech"},
    )

    assert pool_file.is_file()
    assert intent["status"] == "pending"
    assert intent["target_entity"] == "水"
    assert intent["domain"] == "Finance"
    assert intent["text"] == "要去买瓶水"
    assert intent["quote"] == "要去买瓶水"
    assert intent["created_at"] == "2026-10-02T20:35:37+08:00"
    assert intent["resolved_at"] is None
    assert intent["resolved_by_event_id"] is None
    assert intent["metadata"]["source_segment_id"] == "seg_water_01"

    # Verify disk contents
    disk_data = json.loads(pool_file.read_text(encoding="utf-8"))
    assert disk_data["schema_version"] == "active-intents-v1"
    assert len(disk_data["intents"]) == 1
    assert disk_data["intents"][0]["intent_id"] == intent["intent_id"]


def test_intent_pool_reload_from_existing_disk(tmp_path: Path) -> None:
    """New pool instance loads state previously persisted to disk."""
    pool_file = tmp_path / "active-intents.json"
    pool1 = ActiveIntentPool(pool_file)
    registered = pool1.register_intent(
        text="准备去买咖啡",
        domain="Finance",
        target_entity="咖啡",
        timestamp="2026-10-02T15:00:00+08:00",
    )

    # Initialize second instance on same file
    pool2 = ActiveIntentPool(pool_file)
    assert len(pool2) == 1
    loaded = pool2.get_intent(registered["intent_id"])
    assert loaded is not None
    assert loaded["target_entity"] == "咖啡"
    assert loaded["status"] == "pending"


def test_intent_pool_idempotent_registration(tmp_path: Path) -> None:
    """Duplicate registration with identical segment or text+time returns existing
    item.
    """
    pool_file = tmp_path / "active-intents.json"
    pool = ActiveIntentPool(pool_file)

    it1 = pool.register_intent(
        text="要去买瓶水",
        domain="Finance",
        target_entity="水",
        timestamp="2026-10-02T20:35:37+08:00",
        source_segment_id="seg_002",
    )
    it2 = pool.register_intent(
        text="要去买瓶水",
        domain="Finance",
        target_entity="水",
        timestamp="2026-10-02T20:35:37+08:00",
        source_segment_id="seg_002",
    )
    assert it1["intent_id"] == it2["intent_id"]
    assert len(pool) == 1


def test_stable_intent_id_is_source_and_content_scoped() -> None:
    first = stable_intent_id("source-1", "expense", " 水 ", "要去买瓶水")
    replay = stable_intent_id("source-1", "Finance", "水", "要去买瓶水")
    different_quote = stable_intent_id("source-1", "Finance", "水", "准备买瓶水")
    assert first == replay
    assert first != different_quote


@pytest.mark.parametrize("terminal_status", ["pending", "resolved", "dismissed"])
def test_explicit_intent_id_replay_preserves_terminal_record(
    tmp_path: Path, terminal_status: str
) -> None:
    pool = ActiveIntentPool(tmp_path / "active-intents.json")
    original = pool.register_intent(
        "要去买瓶水", "Finance", "水", "2026-10-02T20:35:37+08:00",
        intent_id="stable-water",
    )
    if terminal_status == "resolved":
        original = pool.resolve_intent("stable-water", "evt-water")
    elif terminal_status == "dismissed":
        original = pool.dismiss_intent("stable-water", note="取消")

    replay = pool.register_intent(
        "要去买瓶水", "Finance", "水", "2026-10-03T20:35:37+08:00",
        intent_id="stable-water",
    )
    assert replay == original
    assert replay["status"] == terminal_status


def test_explicit_intent_id_content_conflict_is_rejected(tmp_path: Path) -> None:
    pool = ActiveIntentPool(tmp_path / "active-intents.json")
    pool.register_intent("要去买瓶水", "Finance", "水", "t1", intent_id="same-id")
    with pytest.raises(ValueError, match="conflicts with existing intent"):
        pool.register_intent("要去买咖啡", "Finance", "咖啡", "t2", intent_id="same-id")


def test_distinct_explicit_intent_ids_from_same_segment_remain_pending(
    tmp_path: Path,
) -> None:
    pool = ActiveIntentPool(tmp_path / "active-intents.json")

    first = pool.register_intent(
        text="我想开始练英语",
        domain="Todos",
        target_entity="英语",
        timestamp="2026-10-09T09:00:00+08:00",
        source_segment_id="seg-two-plans",
        quote="我想开始练英语",
        intent_id="plan-english",
    )
    second = pool.register_intent(
        text="我想制定健身计划",
        domain="Todos",
        target_entity="健身",
        timestamp="2026-10-09T09:00:00+08:00",
        source_segment_id="seg-two-plans",
        quote="我想制定健身计划",
        intent_id="plan-fitness",
    )

    assert first["status"] == second["status"] == "pending"
    assert first["intent_id"] != second["intent_id"]
    assert {item["intent_id"] for item in pool.list_pending_intents()} == {
        "plan-english", "plan-fitness",
    }


def test_same_intent_replay_fills_missing_open_topic_metadata(
    tmp_path: Path,
) -> None:
    pool = ActiveIntentPool(tmp_path / "active-intents.json")
    original = pool.register_intent(
        text="我想制定英语健身计划",
        domain="Todos",
        target_entity="英语健身计划",
        timestamp="2026-10-09T09:00:00+08:00",
        source_segment_id="seg-open-topic",
        metadata={"keep": "original", "topic": "existing-topic"},
        quote="我想制定英语健身计划",
        intent_id="open-topic-plan",
    )
    resolved = pool.resolve_intent("open-topic-plan", "evt-followup")

    replay = pool.register_intent(
        text="我想制定英语健身计划",
        domain="Todos",
        target_entity="英语健身计划",
        timestamp="2026-10-10T09:00:00+08:00",
        source_segment_id="seg-open-topic",
        metadata={
            "keep": "replacement-must-not-win",
            "proposed_category": "fitness_plan",
            "topic": "new-topic-must-not-replace-existing-topic",
            "claim_type": "plan",
        },
        quote="我想制定英语健身计划",
        intent_id="open-topic-plan",
    )

    assert original["status"] == "pending"
    assert replay["status"] == resolved["status"] == "resolved"
    assert replay["resolved_by_event_id"] == "evt-followup"
    assert replay["metadata"]["keep"] == "original"
    assert replay["metadata"]["topic"] == "existing-topic"
    assert replay["metadata"]["proposed_category"] == "fitness_plan"
    assert replay["metadata"]["claim_type"] == "plan"
    reloaded = ActiveIntentPool(tmp_path / "active-intents.json").get_intent(
        "open-topic-plan"
    )
    assert reloaded is not None
    assert reloaded["metadata"]["topic"] == "existing-topic"
    assert reloaded["metadata"]["proposed_category"] == "fitness_plan"


def test_intent_schema_validation() -> None:
    """validate_intent enforces required fields, allowed statuses, and valid domains."""
    valid_record = {
        "intent_id": "intent_test_01",
        "text": "要去买瓶水",
        "quote": "要去买瓶水",
        "domain": "Finance",
        "target_entity": "水",
        "status": "pending",
        "created_at": "2026-10-02T20:35:37+08:00",
        "resolved_at": None,
        "resolved_by_event_id": None,
        "metadata": {},
    }
    validate_intent(valid_record)  # Should not raise

    # Missing field
    invalid_record = dict(valid_record)
    del invalid_record["target_entity"]
    with pytest.raises(ValueError, match="missing required fields"):
        validate_intent(invalid_record)

    # Invalid status
    bad_status = dict(valid_record, status="in_progress")
    with pytest.raises(ValueError, match="invalid intent status"):
        validate_intent(bad_status)

    # Invalid domain
    bad_domain = dict(valid_record, domain="InvalidDomain")
    with pytest.raises(ValueError, match="invalid intent domain"):
        validate_intent(bad_domain)

    # Invalid metadata type
    bad_meta = dict(valid_record, metadata="not_a_dict")
    with pytest.raises(ValueError, match="metadata must be a dictionary"):
        validate_intent(bad_meta)


def test_intent_dismissal(tmp_path: Path) -> None:
    """Dismissing an intent updates status to dismissed and stores dismissal note."""
    pool_file = tmp_path / "active-intents.json"
    pool = ActiveIntentPool(pool_file)
    item = pool.register_intent(
        "要去买瓶水", "Finance", "水", "2026-10-02T20:35:37+08:00"
    )

    dismissed = pool.dismiss_intent(item["intent_id"], note="用户取消购买计划")
    assert dismissed["status"] == "dismissed"
    assert dismissed["resolved_at"] is not None
    assert dismissed["dismiss_note"] == "用户取消购买计划"
    assert dismissed["metadata"]["dismiss_note"] == "用户取消购买计划"
    assert len(pool.query_unresolved()) == 0


def test_intent_expiration_by_id(tmp_path: Path) -> None:
    """expire_intent expires a specific pending intent by ID."""
    pool_file = tmp_path / "active-intents.json"
    pool = ActiveIntentPool(pool_file)
    item = pool.register_intent(
        "要去买瓶水", "Finance", "水", "2026-10-02T20:35:37+08:00"
    )

    expired = pool.expire_intent(item["intent_id"], note="超过有效期")
    assert expired["status"] == "expired"
    assert expired["resolved_at"] is not None
    assert len(pool.query_unresolved()) == 0


def test_intent_expiration_batch_by_age(tmp_path: Path) -> None:
    """expire_intents expires only pending intents older than max_age_seconds."""
    pool_file = tmp_path / "active-intents.json"
    pool = ActiveIntentPool(pool_file)

    old_intent = pool.register_intent(
        "旧计划", "Finance", "商品", "2026-09-01T10:00:00+00:00"
    )
    new_intent = pool.register_intent(
        "新计划", "Finance", "商品", "2026-10-02T10:00:00+00:00"
    )

    expired = pool.expire_intents(
        max_age_seconds=7 * 86400,
        reference_time="2026-10-02T12:00:00+00:00",
    )
    assert len(expired) == 1
    assert expired[0]["intent_id"] == old_intent["intent_id"]

    unresolved = pool.query_unresolved()
    assert len(unresolved) == 1
    assert unresolved[0]["intent_id"] == new_intent["intent_id"]


def test_intent_pool_thread_safety(tmp_path: Path) -> None:
    """Concurrent registration and querying operations remain consistent under load."""
    pool_file = tmp_path / "active-intents.json"
    pool = ActiveIntentPool(pool_file)

    def worker(worker_id: int) -> None:
        for i in range(10):
            pool.register_intent(
                text=f"任务_{worker_id}_{i}",
                domain="Todos",
                target_entity=f"任务_{worker_id}_{i}",
                timestamp=f"2026-10-02T10:{worker_id:02d}:{i:02d}+08:00",
            )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(pool) == 50
    assert len(pool.query_unresolved("Todos")) == 50


# ============================================================================
# Feature F9: Cross-Batch Intent Resolution Tests
# ============================================================================


def test_resolve_intent_by_id(tmp_path: Path) -> None:
    """resolve_intent transitions intent to resolved with event provenance."""
    pool_file = tmp_path / "active-intents.json"
    pool = ActiveIntentPool(pool_file)
    item = pool.register_intent(
        "要去买瓶水", "Finance", "水", "2026-10-02T20:35:37+08:00"
    )

    resolved = pool.resolve_intent(
        item["intent_id"],
        resolved_by_event_id="evt_supp_water_4_90",
        resolution_note="今天下午我还花了4.9元买了一瓶水",
    )
    assert resolved["status"] == "resolved"
    assert resolved["resolved_by_event_id"] == "evt_supp_water_4_90"
    assert resolved["resolution_note"] == "今天下午我还花了4.9元买了一瓶水"
    assert resolved["resolved_at"] is not None
    assert len(pool.query_unresolved()) == 0


def test_resolve_by_natural_language_supplement(tmp_path: Path) -> None:
    """resolve_by_supplement parses natural speech and auto-resolves matching intent."""
    pool_file = tmp_path / "active-intents.json"
    pool = ActiveIntentPool(pool_file)

    pool.register_intent(
        text="要去买瓶水",
        domain="Finance",
        target_entity="水",
        timestamp="2026-10-02T20:35:37+08:00",
    )

    supplement_text = "今天下午我还花了4.9元买了一瓶水"
    resolved_list = pool.resolve_by_supplement(
        supplement=supplement_text,
        resolved_by_event_id="evt_manual_water",
        domain="Finance",
    )

    assert len(resolved_list) == 1
    assert resolved_list[0]["target_entity"] == "水"
    assert resolved_list[0]["status"] == "resolved"
    assert resolved_list[0]["resolved_by_event_id"] == "evt_manual_water"
    assert len(pool.query_unresolved("Finance")) == 0


def test_resolve_by_life_event_dictionary(tmp_path: Path) -> None:
    """resolve_by_supplement extracts entity and category from LifeEvent dict."""
    pool_file = tmp_path / "active-intents.json"
    pool = ActiveIntentPool(pool_file)

    pool.register_intent(
        text="准备去买咖啡",
        domain="Finance",
        target_entity="咖啡",
        timestamp="2026-10-02T14:30:00+08:00",
    )

    event_dict = {
        "event_id": "evt_coffee_15",
        "categories": ["expense"],
        "title": "购买美式咖啡",
        "data": {"expense": {"amount": 15.0, "item": "咖啡"}},
        "evidence": {"quote": "花了15元买咖啡"},
    }

    resolved = pool.resolve_by_supplement(event_dict)
    assert len(resolved) == 1
    assert resolved[0]["target_entity"] == "咖啡"
    assert resolved[0]["status"] == "resolved"
    assert resolved[0]["resolved_by_event_id"] == "evt_coffee_15"


def test_unrelated_supplement_does_not_resolve_intent(tmp_path: Path) -> None:
    """Supplement with unrelated entity leaves target intent pending."""
    pool_file = tmp_path / "active-intents.json"
    pool = ActiveIntentPool(pool_file)

    pool.register_intent("要去买瓶水", "Finance", "水", "2026-10-02T20:35:37+08:00")

    resolved = pool.resolve_by_supplement("晚上吃了16块钱的炸鸡", domain="Finance")
    assert len(resolved) == 0

    pending = pool.query_unresolved("Finance")
    assert len(pending) == 1
    assert pending[0]["target_entity"] == "水"


def test_domain_mismatch_prevents_cross_domain_resolution(tmp_path: Path) -> None:
    """Todo intent for water is not resolved by a financial purchase supplement."""
    pool_file = tmp_path / "active-intents.json"
    pool = ActiveIntentPool(pool_file)

    pool.register_intent(
        "记个待办提醒同事喝水", "Todos", "水", "2026-10-02T10:00:00+08:00"
    )

    # Financial supplement for water should not close the Todo
    resolved = pool.resolve_by_supplement("买了4.9元水", domain="Finance")
    assert len(resolved) == 0
    assert len(pool.query_unresolved("Todos")) == 1


# ============================================================================
# Feature F10: Multi-Domain Schema Decoupling Tests
# ============================================================================


def test_finance_model_and_net_amount_calculation() -> None:
    """Finance dataclass calculates net amounts accounting for refunds."""
    f1 = Finance(amount=140.0, currency="CNY", item="朴朴超市食材")
    assert f1.amount == 140.0
    assert f1.currency == "CNY"
    validate_finance(f1.to_dict())

    # Direct expenditure with refund calculation
    net = calculate_net_amount(amount=30.0, refund_offset=14.0)
    assert net == 16.0

    # Total refund
    refund_net = calculate_net_amount(amount=50.0, is_refund=True)
    assert refund_net == -50.0

    f_refund = Finance(amount=50.0, net_amount=refund_net, is_refund=True)
    assert f_refund.is_refund is True
    assert f_refund.net_amount == -50.0
    validate_finance(f_refund.to_dict())


def test_diet_model_intake_vs_grocery_distinction() -> None:
    """Diet dataclass models nutritional intake and distinguishes from grocery
    purchase.
    """
    diet = Diet(
        food_items=["炸鸡", "可乐"],
        meal_type="dinner",
        beverage="可乐",
        is_intake=True,
        estimated_calories=650.0,
    )
    assert diet.is_intake is True
    assert diet.food_items == ["炸鸡", "可乐"]
    validate_diet(diet.to_dict())

    grocery_data = {
        "categories": ["expense"],
        "data": {"expense": {"item": "超市生鲜食材", "amount": 140.0}},
    }
    meal_data = {
        "categories": ["food"],
        "data": {"food": {"items": [{"name": "炸鸡"}], "is_intake": True}},
        "activity_type": "meal",
    }

    assert is_grocery_purchase(grocery_data) is True
    assert is_dietary_intake(grocery_data) is False
    assert is_grocery_purchase(meal_data) is False
    assert is_dietary_intake(meal_data) is True


def test_todos_model_lifecycle_statuses() -> None:
    """Todos dataclass validates pending, completed, and dismissed statuses."""
    todo1 = Todos(task="核对报价单", status="pending", priority="high")
    assert todo1.status == "pending"
    validate_todos(todo1.to_dict())

    todo2 = Todos(
        task="核对报价单", status="completed", completed_at="2026-10-02T18:00:00+08:00"
    )
    assert todo2.status == "completed"
    validate_todos(todo2.to_dict())

    with pytest.raises(ValueError, match="Unknown Todos status"):
        validate_todos({"task": "无效任务", "status": "unknown_status"})

    with pytest.raises(ValueError, match="Todos require a non-empty task"):
        validate_todos({"task": "  ", "status": "pending"})


def test_insights_model_types() -> None:
    """Insights dataclass validates thought, idea, and reflection types."""
    ins1 = Insights(
        content="引入滑动窗口VAD降低MPS功耗", insight_type="idea", tags=["architecture"]
    )
    assert ins1.insight_type == "idea"
    validate_insights(ins1.to_dict())

    ins2 = Insights(content="今天工作节奏很好", insight_type="reflection")
    assert ins2.insight_type == "reflection"
    validate_insights(ins2.to_dict())

    with pytest.raises(ValueError, match="Unknown Insights type"):
        validate_insights({"content": "无效思考", "insight_type": "gossip"})


def test_validate_domain_payload_dispatcher() -> None:
    """validate_domain_payload validates schema by domain name."""
    validate_domain_payload("Finance", {"amount": 100.0, "currency": "CNY"})
    validate_domain_payload("Diet", {"food_items": ["水"], "is_intake": True})
    validate_domain_payload("Todos", {"task": "写代码", "status": "pending"})
    validate_domain_payload("Insights", {"content": "灵感", "insight_type": "thought"})

    with pytest.raises(ValueError, match="Unknown domain"):
        validate_domain_payload("SpaceTravel", {})


def test_domain_event_conversion_from_life_event() -> None:
    """domain_event_from_life_event converts legacy LifeEvent into decoupled
    DomainEvent.
    """
    life_evt = LifeEvent(
        event_id="evt_fin_99",
        categories=["expense"],
        activity_type="purchase",
        title="超市采购",
        time=EventTime("2026-10-02T18:17:15+08:00", None, "exact"),
        data={"expense": {"amount": 140.0, "currency": "CNY", "item": "食材"}},
        capture_mode="passive",
        source_type="audio",
        confidence=0.98,
        evidence={"quote": "一百四买的朴朴超市"},
        review_status="confirmed",
    )
    domain_evt = domain_event_from_life_event(life_evt)
    assert isinstance(domain_evt, DomainEvent)
    assert domain_evt.domain == "Finance"
    assert domain_evt.payload["amount"] == 140.0
    assert domain_evt.event_id == "evt_fin_99"


def test_domain_models_from_dict_and_from_event() -> None:
    """Domain model classmethods from_dict and from_event construct clean objects."""
    fin = Finance.from_dict({"amount": 16.0, "item": "炸鸡"})
    assert fin.amount == 16.0
    assert fin.item == "炸鸡"

    diet = Diet.from_dict({"items": [{"name": "汉堡"}, {"name": "可乐"}]})
    assert diet.food_items == ["汉堡", "可乐"]

    todo = Todos.from_dict({"title": "开会讨论", "status": "pending"})
    assert todo.task == "开会讨论"

    ins = Insights.from_dict({"topic": "架构思考", "insight_type": "idea"})
    assert ins.content == "架构思考"
    assert ins.insight_type == "idea"


# ============================================================================
# Utterance Extraction & Domain Inference Helpers Tests
# ============================================================================


def test_extract_target_entity_variations() -> None:
    """extract_target_entity extracts accurate nouns across diverse utterances."""
    assert extract_target_entity("要去买瓶水") == "水"
    assert extract_target_entity("今天下午我还花了4.9元买了一瓶水") == "水"
    assert extract_target_entity("买了4.9元水") == "水"
    assert extract_target_entity("准备去买咖啡") == "咖啡"
    assert extract_target_entity("打算吃一份炸鸡") == "炸鸡"
    assert extract_target_entity("九块钱买了一包烟") == "烟"
    assert extract_target_entity("记个待办核对报价") == "核对报价"


def test_planned_utterance_detection_and_clause_extraction() -> None:
    """is_planned_utterance and extract_planned_clause identify planned expressions."""
    assert is_planned_utterance("要去买瓶水") is True
    assert is_planned_utterance("打算去超市") is True
    assert is_planned_utterance("今天天气真好") is False

    clause = extract_planned_clause("就就我外甥买了一包烟，要去买瓶水。")
    assert clause == "要去买瓶水"

    clause2 = extract_planned_clause("打算去买咖啡")
    assert clause2 == "打算去买咖啡"


def test_infer_intent_domain_classification() -> None:
    """infer_intent_domain accurately routes keywords to domains."""
    assert infer_intent_domain("要去买瓶水") == "Finance"
    assert infer_intent_domain("打算吃炸鸡") == "Diet"
    assert infer_intent_domain("记个待办开晨会") == "Todos"
    assert infer_intent_domain("有个灵感优化缓存") == "Insights"


def test_normalize_domain_name() -> None:
    """normalize_domain_name standardizes lowercase and legacy category names."""
    assert normalize_domain_name("finance") == "Finance"
    assert normalize_domain_name("expense") == "Finance"
    assert normalize_domain_name("diet") == "Diet"
    assert normalize_domain_name("food") == "Diet"
    assert normalize_domain_name("todo") == "Todos"
    assert normalize_domain_name("todos") == "Todos"
    assert normalize_domain_name("idea") == "Insights"
    assert normalize_domain_name("insights") == "Insights"


# ============================================================================
# Integration: Contextual Extraction Hook & ADR 0004 Compliance
# ============================================================================


class _DummyResponse:
    def __init__(self, content: dict[str, Any]) -> None:
        self.body = json.dumps(
            {
                "choices": [
                    {"message": {"content": json.dumps(content, ensure_ascii=False)}}
                ]
            },
            ensure_ascii=False,
        ).encode("utf-8")

    def __enter__(self) -> _DummyResponse:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def read(self) -> bytes:
        return self.body


def test_planned_utterance_hooks_into_intent_pool_with_adr0004_compliance(
    tmp_path: Path,
) -> None:
    """When 20:35 '要去买瓶水' is ingested, it is registered into intent pool as pending

    and NOT emitted as a formal expense event per ADR 0004.
    """
    pool_file = tmp_path / "active-intents.json"
    pool = ActiveIntentPool(pool_file)

    records = [
        {
            "recording_id": "rec_20261002",
            "segment_id": "seg_2035_water",
            "absolute_started_at": "2026-10-02T20:35:37+08:00",
            "start_offset_seconds": 12.35,
            "end_offset_seconds": 16.61,
            "source_label": "self",
            "text": "就就我外甥买了一包烟，要去买瓶水。",
        }
    ]

    captured_source_ids: list[str] = []

    def kev_annotator(batch: list[dict[str, Any]]) -> dict[str, Any]:
        for item in batch:
            captured_source_ids.append(item["source_id"])
        return {
            batch[0]["source_id"]: {
                "category": "other",
                "event_state": "planned",
                "confidence": 0.85,
            }
        }

    # Stub DeepSeek returning no formal event for the planned water purchase
    def deepseek_opener(request: Any, timeout: float) -> _DummyResponse:
        # 从请求正文取 source_id：Kev 关闭时 kev_annotator 不会被调用，不能靠它捕获编号。
        body = request.data.decode("utf-8") if getattr(request, "data", None) else ""
        found = re.findall(r"src_[0-9a-f]{8,}", body)
        sid = captured_source_ids[0] if captured_source_ids else (found[0] if found else "src_test_water")
        return _DummyResponse(
            {
                "records": [
                    {
                        "source_id": sid,
                        "is_event": False,
                        "events": [],
                    }
                ]
            }
        )

    result = run_contextual_extraction(
        records,
        deepseek_api_key="test-key",
        intent_pool=pool,
        deepseek_opener=deepseek_opener,
    )

    # ADR 0004: Must NOT produce formal expense events
    assert len(result["formal_events"]) == 0

    # Must register planned water intent into the intent pool
    unresolved = pool.query_unresolved("Finance")
    assert len(unresolved) == 1
    intent = unresolved[0]
    assert intent["target_entity"] == "水"
    assert intent["status"] == "pending"
    assert "要去买瓶水" in intent["text"]
    assert intent["metadata"]["source_segment_id"] == "seg_2035_water"

    # Also reflected in extraction result
    assert len(result["active_intents"]) == 1
    assert result["counts"]["active_intents"] == 1

    # Simulate evening supplement resolving the pending intent
    resolved = pool.resolve_by_supplement(
        supplement="今天下午我还花了4.9元买了一瓶水",
        resolved_by_event_id="evt_evening_supp_001",
    )
    assert len(resolved) == 1
    assert resolved[0]["status"] == "resolved"
    assert resolved[0]["resolved_by_event_id"] == "evt_evening_supp_001"
    assert len(pool.query_unresolved("Finance")) == 0


def test_stale_intents_auto_expire_after_48h_ttl_to_prevent_prompt_and_pool_bloat(tmp_path):
    from datetime import datetime, timezone, timedelta
    from life_logger.intent_pool import ActiveIntentPool
    pool_file = tmp_path / "test_ttl_intents.json"
    pool = ActiveIntentPool(pool_file)
    now = datetime.now(timezone.utc)
    t_old = (now - timedelta(hours=50)).isoformat()
    t_fresh = (now - timedelta(hours=5)).isoformat()
    t_future = (now + timedelta(days=10)).isoformat()

    # 1. Old casual intent (50h ago): should auto-expire
    reg1 = pool.register_intent("要去买瓶水", "Diet", "水", t_old)
    # 2. Fresh casual intent (5h ago): should stay pending
    reg2 = pool.register_intent("下周准备买电脑", "Finance", "电脑", t_fresh)
    # 3. Old intent with future due date: should stay pending
    reg3 = pool.register_intent("看牙医", "Health", "牙医", t_old, metadata={"due_date": t_future})

    assert reg1["status"] == "pending"
    assert reg2["status"] == "pending"
    assert reg3["status"] == "pending"

    # Querying pending intents triggers auto-expiration of stale items
    pending = pool.list_pending_intents(auto_expire=True)
    pending_ids = [p["intent_id"] for p in pending]

    assert reg1["intent_id"] not in pending_ids
    assert reg2["intent_id"] in pending_ids
    assert reg3["intent_id"] in pending_ids

    # The old intent is marked as expired, not deleted
    expired_record = pool.get_intent(reg1["intent_id"])
    assert expired_record["status"] == "expired"
    assert "auto_expired_ttl_48h" in expired_record["resolution_note"]
