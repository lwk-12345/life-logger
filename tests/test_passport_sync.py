"""Tests for the AI Passport 240x280 LVGL screen display and synchronization module."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from life_logger.local_web import JobStore, create_server
from life_logger.passport_sync import (
    BINARY_HEADER_SIZE,
    CARD_SCHEMA_VERSION,
    MAX_BLE_CHUNK_SIZE,
    CardType,
    OfflineCardCache,
    PacketAssembler,
    PassportCard,
    PassportCardDeck,
    PassportPager,
    StatusCard,
    SummaryCard,
    TodoCard,
    chunk_deck,
    deck_to_packets_hex,
    extract_passport_cards,
    fetch_live_passport_deck,
    format_currency,
    format_duration,
    pack_packet,
    unpack_packet,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_mock_daily_summary(
    job_root: Path,
    *,
    job_id: str,
    digest: str,
    date: str,
    events: list[dict],
    expense_total_cny: float = 0.0,
    segment_durations: list[float] | None = None,
) -> Path:
    base = job_root / "jobs" / job_id / "files" / digest
    result_dir = base / "result" / date
    result_dir.mkdir(parents=True, exist_ok=True)

    summary_file = result_dir / "daily_summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "schema_version": "0.1",
                "date": date,
                "timezone": "Asia/Shanghai",
                "event_count": len(events),
                "timeline": events,
                "expense_total_cny": expense_total_cny,
                "warnings": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    if segment_durations is not None:
        manifest_file = base / "manifest.json"
        manifest_file.write_text(
            json.dumps(
                {
                    "segments": [
                        {"segment_id": f"seg_{i}", "duration_seconds": dur}
                        for i, dur in enumerate(segment_durations)
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    return summary_file


# ---------------------------------------------------------------------------
# 1. Card Schema & Validation Tests
# ---------------------------------------------------------------------------


def test_base_card_validation() -> None:
    card = PassportCard(
        card_id="card_01",
        card_type=CardType.STATUS.value,
        title="测试卡片",
        updated_at="2026-09-28T20:00:00+08:00",
    )
    card.validate()
    d = card.to_dict()
    assert d["card_id"] == "card_01"
    assert d["card_type"] == "STATUS_CARD"
    assert json.loads(card.to_json())["card_id"] == "card_01"

    # Empty card_id
    with pytest.raises(ValueError, match="card_id"):
        PassportCard(
            card_id="",
            card_type=CardType.STATUS.value,
            title="标题",
            updated_at="2026-09-28T20:00:00",
        ).validate()

    # Invalid card_type
    with pytest.raises(ValueError, match="invalid card_type"):
        PassportCard(
            card_id="c1",
            card_type="UNKNOWN_CARD",
            title="标题",
            updated_at="2026-09-28T20:00:00",
        ).validate()

    # Empty title
    with pytest.raises(ValueError, match="title"):
        PassportCard(
            card_id="c1",
            card_type=CardType.TODO.value,
            title="",
            updated_at="2026-09-28T20:00:00",
        ).validate()


def test_todo_card_validation_and_methods() -> None:
    todo = TodoCard(
        card_id="card_todo_01",
        title="提交周报",
        updated_at="2026-09-28T20:00:00+08:00",
        task_key="task_001",
        status="pending",
        priority="P1",
        due_time="18:00",
        project="life-logger",
        tags=["work"],
        source_event_id="evt_101",
    )
    todo.validate()
    assert todo.card_type == CardType.TODO.value

    # Invalid status
    with pytest.raises(ValueError, match="status must be one of"):
        TodoCard(
            card_id="c2",
            title="无效状态",
            updated_at="2026-09-28T20:00:00",
            task_key="k1",
            status="invalid_status",
        ).validate()

    # Invalid priority
    with pytest.raises(ValueError, match="priority must be one of"):
        TodoCard(
            card_id="c2",
            title="无效优先级",
            updated_at="2026-09-28T20:00:00",
            task_key="k1",
            priority="P99",
        ).validate()

    # LVGL screen representation
    lvgl = todo.to_lvgl_dict()
    assert lvgl["task_key"] == "task_001"
    assert lvgl["status_label"] == "待办"
    assert lvgl["priority_color"] == "#FA8C16"  # P1 Orange
    assert "[P1 待办]" in lvgl["badge_text"]
    assert "[中键长按]" in lvgl["action_hint"]

    # OpenClaw conversion roundtrip
    oc_task = todo.to_openclaw_task()
    assert oc_task["schema_version"] == "unified-task-v1"
    assert oc_task["id"] == "task_001"
    assert oc_task["priority"] == "P1"
    assert oc_task["source"]["event_id"] == "evt_101"

    reconstructed_todo = TodoCard.from_openclaw_task(oc_task)
    assert reconstructed_todo.task_key == "task_001"
    assert reconstructed_todo.priority == "P1"
    assert reconstructed_todo.source_event_id == "evt_101"


def test_summary_card_validation_and_lvgl() -> None:
    summary = SummaryCard(
        card_id="card_summary_2026-09-28",
        title="今日概况 (09-28)",
        updated_at="2026-09-28T20:00:00+08:00",
        date="2026-09-28",
        recording_duration_seconds=7320.0,
        expense_count=3,
        expense_total_cny=128.50,
        key_events=[
            {"time": "09:30", "title": "晨会沟通", "category": "work_communication"},
            {"time": "12:15", "title": "外卖午餐", "category": "food"},
        ],
    )
    summary.validate()
    assert summary.card_type == CardType.SUMMARY.value
    assert summary.recording_duration_formatted == "2小时2分"

    lvgl = summary.to_lvgl_dict()
    assert "2小时2分" in lvgl["duration_text"]
    assert "¥128.50" in lvgl["expense_text"]
    assert len(lvgl["event_lines"]) == 2
    assert "09:30 晨会沟通" in lvgl["event_lines"][0]

    # Invalid date
    with pytest.raises(ValueError, match="date must follow YYYY-MM-DD"):
        SummaryCard(
            card_id="cs",
            title="概况",
            updated_at="2026-09-28T20:00:00",
            date="2026/09/28",
        ).validate()

    # Negative values
    with pytest.raises(ValueError, match="recording_duration_seconds cannot be negative"):
        SummaryCard(
            card_id="cs",
            title="概况",
            updated_at="2026-09-28T20:00:00",
            date="2026-09-28",
            recording_duration_seconds=-5.0,
        ).validate()


def test_status_card_validation_and_lvgl() -> None:
    status = StatusCard(
        card_id="card_status_01",
        title="系统状态",
        updated_at="2026-09-28T20:00:00+08:00",
        device_name="AirPassport-C3",
        battery_level=85,
        is_charging=True,
        connection_status="ready",
        sync_time="20:00:00",
        ble_encrypted=True,
    )
    status.validate()
    assert status.card_type == CardType.STATUS.value

    lvgl = status.to_lvgl_dict()
    assert "⚡ 85%" in lvgl["battery_text"]
    assert "就绪" in lvgl["connection_text"]
    assert "BLE加密 (MTU 185)" in lvgl["security_text"]

    # Invalid battery percentage
    with pytest.raises(ValueError, match="battery_level must be between 0 and 100"):
        StatusCard(
            card_id="st",
            title="状态",
            updated_at="2026-09-28T20:00:00",
            battery_level=120,
        ).validate()


def test_polymorphic_card_serialization() -> None:
    todo_dict = {
        "card_id": "card_t1",
        "card_type": "TODO_CARD",
        "title": "购买文具",
        "updated_at": "2026-09-28T20:00:00+08:00",
        "task_key": "task_buy",
        "status": "pending",
        "priority": "P2",
    }
    card = PassportCard.from_dict(todo_dict)
    assert isinstance(card, TodoCard)
    assert card.task_key == "task_buy"

    summary_dict = {
        "card_id": "card_s1",
        "card_type": "SUMMARY_CARD",
        "title": "今日概况",
        "updated_at": "2026-09-28T20:00:00+08:00",
        "date": "2026-09-28",
        "recording_duration_seconds": 120.0,
        "expense_count": 1,
        "expense_total_cny": 25.0,
    }
    card_s = PassportCard.from_dict(summary_dict)
    assert isinstance(card_s, SummaryCard)
    assert card_s.expense_total_cny == 25.0


# ---------------------------------------------------------------------------
# 2. Card Deck & Differential Hashing Tests
# ---------------------------------------------------------------------------


def test_card_deck_operations_and_version_hash() -> None:
    c1 = StatusCard(
        card_id="c_status",
        title="系统状态",
        updated_at="2026-09-28T20:00:00+08:00",
        device_name="AirPassport",
        battery_level=90,
    )
    c2 = TodoCard(
        card_id="c_todo_1",
        title="完成测试",
        updated_at="2026-09-28T20:00:00+08:00",
        task_key="t1",
        priority="P0",
    )
    deck = PassportCardDeck(deck_id="deck_001", cards=[c1, c2])
    deck.validate()

    assert deck.schema_version == CARD_SCHEMA_VERSION
    assert len(deck.cards) == 2
    orig_hash = deck.version_hash
    assert len(orig_hash) == 16

    # Same content produces identical hash
    deck_copy = PassportCardDeck(deck_id="deck_002", cards=[c1, c2])
    assert deck_copy.compute_version_hash() == orig_hash

    # Mutating a card changes version hash
    c2.priority = "P1"
    new_hash = deck.compute_version_hash()
    assert new_hash != orig_hash

    # to_dict & from_dict roundtrip
    d = deck.to_dict()
    restored = PassportCardDeck.from_dict(d)
    assert restored.deck_id == "deck_001"
    assert len(restored.cards) == 2
    assert restored.cards[1].card_id == "c_todo_1"

    # Filter and find
    assert len(deck.filter_cards("TODO_CARD")) == 1
    assert deck.get_card("c_status") is c1
    assert deck.get_card("nonexistent") is None

    # LVGL deck representation
    lvgl_deck = deck.to_lvgl_deck()
    assert lvgl_deck["total_pages"] == 2
    assert lvgl_deck["pages"][0]["page_index"] == 1
    assert lvgl_deck["pages"][1]["page_index"] == 2


# ---------------------------------------------------------------------------
# 3. Data Extraction Tests
# ---------------------------------------------------------------------------


def test_extract_passport_cards_normal(tmp_path: Path) -> None:
    date = "2026-09-28"
    events = [
        {
            "event_id": "evt_001",
            "categories": ["routine"],
            "title": "起床与晨间拉伸",
            "time": {"absolute": f"{date}T08:00:00+08:00", "precision": "exact"},
            "data": {},
        },
        {
            "event_id": "evt_002",
            "categories": ["expense"],
            "title": "购买午餐外卖",
            "time": {"absolute": f"{date}T12:30:00+08:00", "precision": "exact"},
            "data": {"expense": {"amount": 45.5}},
        },
        {
            "event_id": "evt_003",
            "categories": ["todo"],
            "title": "紧急：修复生产服务器告警",
            "time": {"absolute": f"{date}T14:00:00+08:00", "precision": "exact"},
            "data": {},
        },
    ]

    _write_mock_daily_summary(
        tmp_path,
        job_id="job-001",
        digest="rec_test",
        date=date,
        events=events,
        expense_total_cny=45.5,
        segment_durations=[60.0, 120.0, 30.5],
    )

    # Custom task state
    task_states = [
        {
            "task_key": "task_custom_1",
            "title": "重要：审查代码合并请求",
            "status": "pending",
            "priority": "P1",
            "updated_at": f"{date}T15:00:00+08:00",
        }
    ]

    device_status = {
        "device_name": "AirPassport-ESP32",
        "battery_level": 78,
        "is_charging": False,
        "connection_status": "connected",
        "sync_time": "16:00:00",
    }

    deck = extract_passport_cards(
        tmp_path,
        date=date,
        task_states=task_states,
        device_status=device_status,
    )

    # Deck should have StatusCard, SummaryCard, and 2 TodoCards
    assert len(deck.cards) == 4
    status_card = deck.cards[0]
    assert isinstance(status_card, StatusCard)
    assert status_card.device_name == "AirPassport-ESP32"
    assert status_card.battery_level == 78

    summary_card = deck.cards[1]
    assert isinstance(summary_card, SummaryCard)
    assert summary_card.recording_duration_seconds == pytest.approx(210.5)
    assert summary_card.expense_count == 1
    assert summary_card.expense_total_cny == 45.5
    assert len(summary_card.key_events) >= 2

    # Check todo cards: P0 should be first, then P1
    todo_1 = deck.cards[2]
    assert isinstance(todo_1, TodoCard)
    assert todo_1.priority == "P0"
    assert "紧急" in todo_1.title

    todo_2 = deck.cards[3]
    assert isinstance(todo_2, TodoCard)
    assert todo_2.priority == "P1"
    assert todo_2.task_key == "task_custom_1"


def test_extract_passport_cards_with_openclaw_tasks(tmp_path: Path) -> None:
    date = "2026-09-28"
    openclaw_tasks = [
        {
            "id": "oc_task_99",
            "title": "准备技术方案汇报",
            "status": "in_progress",
            "priority": "P1",
            "project_id": "air-passport",
            "tags": ["planning", "ai"],
        }
    ]

    deck = extract_passport_cards(
        tmp_path,
        date=date,
        openclaw_tasks=openclaw_tasks,
    )
    # Status, Summary, and OpenClaw Todo
    assert len(deck.cards) == 3
    todo = deck.cards[2]
    assert isinstance(todo, TodoCard)
    assert todo.task_key == "oc_task_99"
    assert todo.status == "in_progress"
    assert todo.project == "air-passport"


def test_extract_passport_cards_empty_boundary(tmp_path: Path) -> None:
    """Ensure extracting from an empty directory produces a valid deck without errors."""
    empty_root = tmp_path / "empty_job_root"
    empty_root.mkdir()

    deck = extract_passport_cards(empty_root, date="2026-09-28")
    assert len(deck.cards) == 2  # StatusCard + SummaryCard
    status_card = deck.cards[0]
    assert isinstance(status_card, StatusCard)
    summary_card = deck.cards[1]
    assert isinstance(summary_card, SummaryCard)
    assert summary_card.recording_duration_seconds == 0.0
    assert summary_card.expense_count == 0
    assert summary_card.expense_total_cny == 0.0
    assert summary_card.key_events == []


def test_extract_passport_cards_corrupted_files(tmp_path: Path) -> None:
    """Ensure malformed JSON files are gracefully ignored."""
    corrupt_dir = tmp_path / "jobs" / "job-err" / "files" / "bad" / "result" / "2026-09-28"
    corrupt_dir.mkdir(parents=True)
    (corrupt_dir / "daily_summary.json").write_text("{this is corrupted json", encoding="utf-8")

    deck = extract_passport_cards(tmp_path, date="2026-09-28")
    assert len(deck.cards) >= 2


# ---------------------------------------------------------------------------
# 4. Binary Serialization & Packet Protocol Framing Tests
# ---------------------------------------------------------------------------


def test_pack_and_unpack_packet_roundtrip() -> None:
    payload = b"Hello ESP32 LVGL Screen"
    packet = pack_packet(seq=0, total=1, payload=payload, is_last=True, is_compressed=False)

    assert len(packet) == BINARY_HEADER_SIZE + len(payload)
    seq, total, is_last, is_compressed, extracted_payload = unpack_packet(packet)
    assert seq == 0
    assert total == 1
    assert is_last is True
    assert is_compressed is False
    assert extracted_payload == payload


def test_unpack_packet_corruptions() -> None:
    payload = b"Sample Payload"
    packet = bytearray(pack_packet(seq=0, total=1, payload=payload, is_last=True, is_compressed=False))

    # 1. Truncated packet
    with pytest.raises(ValueError, match="Packet too short"):
        unpack_packet(bytes(packet[:10]))

    # 2. Corrupted Magic
    corrupt_magic = bytearray(packet)
    corrupt_magic[0] = ord(b"X")
    with pytest.raises(ValueError, match="Invalid magic"):
        unpack_packet(bytes(corrupt_magic))

    # 3. Payload corruption triggering CRC32 mismatch
    corrupt_payload = bytearray(packet)
    corrupt_payload[-1] ^= 0xFF
    with pytest.raises(ValueError, match="CRC32 mismatch"):
        unpack_packet(bytes(corrupt_payload))


def test_chunk_and_assemble_deck_stream() -> None:
    # Build a deck with multiple cards to force multi-chunk splitting
    cards: list[PassportCard] = [
        StatusCard(
            card_id="st",
            title="系统状态",
            updated_at="2026-09-28T20:00:00",
            battery_level=95,
        ),
        SummaryCard(
            card_id="sm",
            title="今日概况",
            updated_at="2026-09-28T20:00:00",
            date="2026-09-28",
            recording_duration_seconds=3600.0,
            expense_total_cny=88.8,
            key_events=[{"time": "10:00", "title": f"任务事项 {i}"} for i in range(5)],
        ),
    ]
    for i in range(10):
        cards.append(
            TodoCard(
                card_id=f"todo_{i}",
                title=f"待办事项第 {i} 条：详细测试描述文本信息",
                updated_at="2026-09-28T20:00:00",
                task_key=f"task_{i}",
                priority=f"P{i % 4}",
            )
        )

    deck = PassportCardDeck(deck_id="deck_test_chunks", cards=cards)

    # Chunk with small chunk_size to test multi-packet framing (e.g. 100 bytes)
    packets = chunk_deck(deck, max_chunk_size=100, compress=True)
    assert len(packets) > 1

    # Verify each packet size satisfies BLE MTU 185
    for p in packets:
        assert len(p) <= BINARY_HEADER_SIZE + 100
        assert len(p) <= 185

    # Assemble in order
    assembler = PacketAssembler()
    assert assembler.is_complete() is False
    assert assembler.progress == 0.0

    for p in packets:
        assembler.add_packet(p)

    assert assembler.is_complete() is True
    assert assembler.progress == 1.0
    assert assembler.missing_chunks() == []

    restored_deck = assembler.assemble()
    assert restored_deck.deck_id == "deck_test_chunks"
    assert len(restored_deck.cards) == 12
    assert restored_deck.cards[2].title == cards[2].title


def test_packet_assembler_out_of_order_and_duplicates() -> None:
    cards = [
        TodoCard(
            card_id=f"t_{i}",
            title=f"任务 {i}",
            updated_at="2026-09-28T20:00:00",
            task_key=f"k_{i}",
        )
        for i in range(8)
    ]
    deck = PassportCardDeck(deck_id="deck_shuffle", cards=cards)
    packets = chunk_deck(deck, max_chunk_size=64, compress=True)

    # Shuffle packets to simulate out-of-order BLE reception
    shuffled = list(packets)
    random.seed(42)
    random.shuffle(shuffled)

    assembler = PacketAssembler()
    # Add first few
    for p in shuffled[: len(shuffled) // 2]:
        assembler.add_packet(p)

    assert assembler.is_complete() is False
    missing = assembler.missing_chunks()
    assert len(missing) > 0

    # Incomplete assembly raises error
    with pytest.raises(ValueError, match="Cannot assemble incomplete deck"):
        assembler.assemble()

    # Add duplicates of already received packets
    assembler.add_packet(shuffled[0])

    # Add remaining packets
    for p in shuffled[len(shuffled) // 2 :]:
        assembler.add_packet(p)

    assert assembler.is_complete() is True
    restored = assembler.assemble()
    assert restored.deck_id == "deck_shuffle"
    assert len(restored.cards) == 8


def test_deck_to_packets_hex() -> None:
    deck = PassportCardDeck(
        deck_id="deck_hex",
        cards=[StatusCard(card_id="s1", title="状态", updated_at="2026-09-28T20:00:00")],
    )
    hex_list = deck_to_packets_hex(deck, max_chunk_size=MAX_BLE_CHUNK_SIZE)
    assert isinstance(hex_list, list)
    assert len(hex_list) >= 1
    # Check magic hex "41504331" (APC1)
    assert hex_list[0].startswith("41504331")


# ---------------------------------------------------------------------------
# 5. Navigation & Pager Tests (中键翻页与离线分页交互)
# ---------------------------------------------------------------------------


def test_passport_pager_navigation() -> None:
    c1 = StatusCard(card_id="c1", title="状态", updated_at="2026-09-28T20:00:00")
    c2 = SummaryCard(card_id="c2", title="概况", updated_at="2026-09-28T20:00:00", date="2026-09-28")
    c3 = TodoCard(card_id="c3", title="待办1", updated_at="2026-09-28T20:00:00", task_key="t1")

    deck = PassportCardDeck(deck_id="deck_pager", cards=[c1, c2, c3])
    pager = PassportPager(deck, loop=True)

    assert pager.total_pages == 3
    assert pager.current_card() is c1
    assert pager.current_page == 0

    # Next page (short press middle button)
    assert pager.next_page() is c2
    assert pager.current_page == 1

    assert pager.next_page() is c3
    assert pager.current_page == 2

    # Loop back to 0
    assert pager.next_page() is c1
    assert pager.current_page == 0

    # Prev page (loops backwards to 2)
    assert pager.prev_page() is c3
    assert pager.current_page == 2

    # Jump to page
    assert pager.jump_to(1) is c2
    with pytest.raises(IndexError):
        pager.jump_to(99)

    # Render screen representation
    screen = pager.render_screen()
    assert screen["page_index"] == 2
    assert screen["total_pages"] == 3
    assert "AI Passport (2/3)" in screen["header"]
    assert screen["card"]["card_id"] == "c2"


def test_passport_pager_action_click_toggles_todo() -> None:
    c_status = StatusCard(card_id="c_s", title="状态", updated_at="2026-09-28T20:00:00")
    c_todo = TodoCard(
        card_id="c_t",
        title="测试任务",
        updated_at="2026-09-28T20:00:00",
        task_key="k_test",
        status="pending",
    )
    deck = PassportCardDeck(deck_id="deck_action", cards=[c_status, c_todo])
    pager = PassportPager(deck)

    # On status card: action returns refresh
    action_1 = pager.action_click()
    assert action_1["action"] == "refresh"

    # Advance to todo card
    pager.next_page()
    assert pager.current_card() is c_todo
    assert c_todo.status == "pending"

    # Action click on todo card (long press middle button) -> toggles to completed
    action_2 = pager.action_click()
    assert action_2["action"] == "toggle_todo"
    assert action_2["new_status"] == "completed"
    assert c_todo.status == "completed"

    # Click again -> toggles back to pending
    action_3 = pager.action_click()
    assert action_3["action"] == "toggle_todo"
    assert action_3["new_status"] == "pending"
    assert c_todo.status == "pending"


def test_passport_pager_empty_deck() -> None:
    empty_deck = PassportCardDeck(deck_id="empty_deck", cards=[])
    pager = PassportPager(empty_deck)
    assert pager.current_card() is None
    assert pager.next_page() is None
    assert pager.prev_page() is None
    assert pager.action_click()["action"] == "none"

    screen = pager.render_screen()
    assert screen["page_index"] == 0
    assert "暂无卡片" in screen["footer"]


# ---------------------------------------------------------------------------
# 6. Offline Cache Tests
# ---------------------------------------------------------------------------


def test_offline_card_cache_lifecycle(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    deck = PassportCardDeck(
        deck_id="deck_cache_test",
        cards=[
            StatusCard(card_id="s1", title="系统状态", updated_at="2026-09-28T20:00:00"),
            TodoCard(card_id="t1", title="离线待办", updated_at="2026-09-28T20:00:00", task_key="tk1"),
        ],
    )

    # Save to disk
    target = OfflineCardCache.save(deck, cache_dir)
    assert target.is_file()
    assert OfflineCardCache.is_stale(cache_dir, max_age_seconds=3600) is False

    # Load from disk
    loaded = OfflineCardCache.load(cache_dir)
    assert loaded is not None
    assert loaded.deck_id == "deck_cache_test"
    assert len(loaded.cards) == 2
    assert loaded.cards[1].card_id == "t1"

    # Check stale condition
    assert OfflineCardCache.is_stale(cache_dir, max_age_seconds=-1.0) is True

    # Corrupt the cache file
    target.write_text("invalid json content", encoding="utf-8")
    assert OfflineCardCache.load(cache_dir) is None

    # Clear cache
    assert OfflineCardCache.clear(cache_dir) is True
    assert OfflineCardCache.load(cache_dir) is None


# ---------------------------------------------------------------------------
# 7. Web API Integration Tests
# ---------------------------------------------------------------------------


def test_local_web_api_passport_cards(tmp_path: Path) -> None:
    local_root = tmp_path / "local-web"
    config_path = tmp_path / "local-config.json"
    config_path.write_text("{}", encoding="utf-8")

    date = "2026-09-28"
    _write_mock_daily_summary(
        local_root,
        job_id="job-web-01",
        digest="rec_web",
        date=date,
        events=[
            {
                "event_id": "evt_w1",
                "categories": ["routine"],
                "title": "早会同步",
                "time": {"absolute": f"{date}T09:00:00+08:00"},
            }
        ],
        expense_total_cny=15.0,
    )

    # Prepopulate a task state
    job_store = JobStore(local_root, recover=False)
    job_store.save_task_state(task_key="task_web_1", status="pending", title="修复UI对齐")

    server = create_server(host="127.0.0.1", port=0, store=job_store, config_path=config_path)
    server_port = server.server_address[1]

    import threading
    import urllib.request

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        # 1. GET /api/passport/cards (JSON format)
        url_json = f"http://127.0.0.1:{server_port}/api/passport/cards?date={date}"
        with urllib.request.urlopen(url_json) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["schema_version"] == CARD_SCHEMA_VERSION
            assert len(data["cards"]) >= 2
            card_types = [c["card_type"] for c in data["cards"]]
            assert "STATUS_CARD" in card_types
            assert "SUMMARY_CARD" in card_types
            assert "TODO_CARD" in card_types

        # 2. GET /api/passport/cards?format=lvgl
        url_lvgl = f"http://127.0.0.1:{server_port}/api/passport/cards?date={date}&format=lvgl"
        with urllib.request.urlopen(url_lvgl) as resp:
            assert resp.status == 200
            lvgl_data = json.loads(resp.read().decode("utf-8"))
            assert "total_pages" in lvgl_data
            assert len(lvgl_data["pages"]) >= 2

        # 3. GET /api/passport/cards?format=packets
        url_packets = f"http://127.0.0.1:{server_port}/api/passport/cards?date={date}&format=packets"
        with urllib.request.urlopen(url_packets) as resp:
            assert resp.status == 200
            pkt_data = json.loads(resp.read().decode("utf-8"))
            assert "packets_hex" in pkt_data
            assert len(pkt_data["packets_hex"]) >= 1

        # 4. POST /api/passport/cards/action (Toggle todo status)
        action_req = urllib.request.Request(
            f"http://127.0.0.1:{server_port}/api/passport/cards/action",
            data=json.dumps(
                {
                    "action": "toggle_todo",
                    "task_key": "task_web_1",
                    "status": "completed",
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(action_req) as resp:
            assert resp.status == 200
            act_data = json.loads(resp.read().decode("utf-8"))
            assert act_data["ok"] is True
            assert act_data["task"]["status"] == "completed"

    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# 8. Utility Formatting Tests
# ---------------------------------------------------------------------------


def test_utility_formatters() -> None:
    assert format_duration(0) == "0秒"
    assert format_duration(45) == "45秒"
    assert format_duration(60) == "1分"
    assert format_duration(75) == "1分15秒"
    assert format_duration(3600) == "1小时0分"
    assert format_duration(3720) == "1小时2分"

    assert format_currency(0) == "¥0.00"
    assert format_currency(12.5) == "¥12.50"
    assert format_currency(199.999) == "¥200.00"


def test_fetch_live_passport_deck(tmp_path: Path) -> None:
    deck = fetch_live_passport_deck(tmp_path, date="2026-09-28")
    assert isinstance(deck, PassportCardDeck)
    assert len(deck.cards) >= 2
    assert deck.cards[0].card_type == "STATUS_CARD"
    assert deck.cards[1].card_type == "SUMMARY_CARD"

