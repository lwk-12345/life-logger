"""测试只读展示层按相同物理证据坐标（event.event_key）合并为单个购买会话。

验证目标：
1. 炒饼丝双通道投影事件（evt_ctx_0f3a65c4337404b9 与 evt_ctx_7daa1db97c201e7c）合并为单个会话；
2. 消除空冗余会话 ps_511f39111bac；
3. 单个会话同时挂载金额（5.0 CNY）与品项分量（炒饼丝、1份）；
4. 会话 member_ids 完整保留两笔关联事件 ID，不隐藏任何事实；
5. 原始 events.json 与财务账本毫厘未动（ledger total 维持 5.0 CNY，1笔）；
6. 严格禁止基于相同 quote、相同金额或相同录音盲目去重。
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.activity_presentation import build_purchase_session_summaries
from life_logger.event_view import build_event_view
from life_logger.expense_policy import build_expense_ledger
from life_logger.daily_brief import build_daily_brief

OUTPUT_ROOT = ROOT / "output/local-web"

# [2026-11-04 夹具入库] 这条资源无法入库（体积过大 / 含真实录音 / 含本机配置），
# 缺失时 skip 而不是 fail —— 新克隆不该因为缺少本机数据而红。
import pytest as _pytest  # noqa: E402

if not ((ROOT / "output" / "local-web").is_dir()):
    _pytest.skip(
        "缺少不入库的夹具：output/local-web 真实运行数据（单日目录含大量产物，且含真实原话）",
        allow_module_level=True,
    )


def _bingsi_fixtures():
    """从 2026-10-26 真实事件中获取炒饼丝双通道投影事件。"""
    ev_view = build_event_view(OUTPUT_ROOT, date_from="2026-10-26", date_to="2026-10-26", use_cache=False)
    events = ev_view.get("events", [])
    e1 = next(e for e in events if e.get("event_id") == "evt_ctx_0f3a65c4337404b9")
    e2 = next(e for e in events if e.get("event_id") == "evt_ctx_7daa1db97c201e7c")
    return copy.deepcopy(e1), copy.deepcopy(e2)


def test_exact_projection_merges_into_single_session_and_eliminates_empty_ps():
    """验证按相同 event_key 合并为单个会话，消除 ps_511f39111bac 空冗余。"""
    e1, e2 = _bingsi_fixtures()
    assert e1["event_key"] == e2["event_key"] == "entity:8fe03b3cbb34cc3201758ab9"

    # 执行会话构建
    summary = build_purchase_session_summaries([e1, e2])
    sessions = summary.get("sessions", [])

    # 断言：会话数严格为 1
    assert len(sessions) == 1, f"Expected 1 session, got {len(sessions)}"
    session = sessions[0]

    # 断言：会话 ID 为以主消费事件命名的 ps_3c9340a2458b，ps_511f39111bac 被彻底消除
    assert session["session_id"] == "ps_3c9340a2458b"
    assert session["session_id"] != "ps_511f39111bac"

    # 断言：member_ids 完整保留两笔关联事件 ID，零数据隐藏
    assert "evt_ctx_0f3a65c4337404b9" in session["member_ids"]
    assert "evt_ctx_7daa1db97c201e7c" in session["member_ids"]
    assert len(session["member_ids"]) == 2

    # 断言：子项同时挂载金额与饮食分量
    items = session.get("items", [])
    assert len(items) == 1
    item = items[0]
    assert item["event_id"] == "evt_ctx_0f3a65c4337404b9"
    assert item["item"] == "炒饼丝"
    assert item["amount"] == 5.0
    assert item["currency"] == "CNY"
    assert item.get("portion") == "1份"
    assert item.get("linked_event_ids") == ["evt_ctx_0f3a65c4337404b9", "evt_ctx_7daa1db97c201e7c"]

    # 断言：金额数组仅列出 5.0，不重复
    assert session.get("amounts_listed_individually") == [5.0]


def test_distinct_events_with_different_keys_are_never_merged():
    """验证严格基于物理坐标，相同金额或相同 quote 绝不盲目去重。"""
    # 模拟两笔不同时段（不同物理坐标/不同 event_key）的独立 5 元消费
    rec = "watch_TEST_REC"
    tx1 = {
        "event_id": "evt_bingsi_1",
        "event_key": "entity:key_111111111111111111111111",
        "title": "购买炒饼丝 (¥5.00)",
        "categories": ["expense", "food"],
        "activity_type": "purchase",
        "time": {"absolute": "2026-10-26T01:56:00+08:00"},
        "data": {"expense": {"amount": 5.0, "currency": "CNY", "item": "炒饼丝"}},
        "evidence": {
            "quote": "五块钱买了一份炒饼丝儿",
            "source_label": "self",
            "recording_id": rec,
            "segment_id": "seg_001",
            "start_offset_seconds": 100.0,
            "end_offset_seconds": 105.0,
            "claim_type": "order",
        },
    }
    tx2 = {
        "event_id": "evt_bingsi_2",
        "event_key": "entity:key_222222222222222222222222",  # 不同物理坐标
        "title": "购买第二份炒饼丝 (¥5.00)",
        "categories": ["expense", "food"],
        "activity_type": "purchase",
        "time": {"absolute": "2026-10-26T02:30:00+08:00"},
        "data": {"expense": {"amount": 5.0, "currency": "CNY", "item": "炒饼丝"}},
        "evidence": {
            "quote": "五块钱买了一份炒饼丝儿",  # quote 相同
            "source_label": "self",
            "recording_id": rec,
            "segment_id": "seg_002",
            "start_offset_seconds": 500.0,
            "end_offset_seconds": 505.0,
            "claim_type": "order",
        },
    }

    # 执行构建
    summary = build_purchase_session_summaries([tx1, tx2])
    sessions = summary.get("sessions", [])

    # 断言：由于 event_key 不同，绝不合并，形成 2 个独立会话
    assert len(sessions) == 2, "相同金额/相同quote但在不同坐标，绝不得合并"
    assert {s["session_id"] for s in sessions} == {
        "ps_" + str(Path(__file__).stem),  # dummy
    } or len(sessions) == 2


def test_real_20261026_daily_brief_eliminates_empty_session_and_preserves_ledger():
    """验证真实 2026-10-26 日报构建中：消除空冗余，账本金额与交易数保持不变。"""
    # 1. 验证 daily_brief purchase_sessions
    brief = build_daily_brief(OUTPUT_ROOT, date="2026-10-26")
    sessions = brief.get("purchase_sessions", [])
    
    # 全天炒饼丝相关会话严格为 1 个
    bingsi_sessions = [s for s in sessions if any(
        it.get("event_id") in ["evt_ctx_0f3a65c4337404b9", "evt_ctx_7daa1db97c201e7c"]
        for it in s.get("items", [])
    )]
    assert len(bingsi_sessions) == 1, f"Expected exactly 1 bingsi session, got {len(bingsi_sessions)}"
    assert bingsi_sessions[0]["session_id"] == "ps_3c9340a2458b"
    assert not any(s.get("session_id") == "ps_511f39111bac" for s in sessions)

    # 2. 验证财务账本未被篡改
    ev_view = build_event_view(OUTPUT_ROOT, date_from="2026-10-26", date_to="2026-10-26", use_cache=False)
    events = ev_view.get("events", [])
    ledger = build_expense_ledger(events)

    # 账本仍维持 1 笔 5.00 元，未增未减
    assert ledger.get("total_amount") == 5.0
    assert ledger.get("transaction_count") == 1
    assert ledger.get("transactions")[0]["event_id"] == "evt_ctx_0f3a65c4337404b9"

    # 原始两个事件仍完整存在于 events 视图中（零物理删除）
    event_ids = {e.get("event_id") for e in events}
    assert "evt_ctx_0f3a65c4337404b9" in event_ids
    assert "evt_ctx_7daa1db97c201e7c" in event_ids
