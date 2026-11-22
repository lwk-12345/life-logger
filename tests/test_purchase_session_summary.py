"""同次购买跨商品摘要：显式追加可关联；同词/同小时/同额不关联；缺商品只报缺口。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.activity_presentation import build_purchase_session_summaries  # noqa: E402

REC = "watch_F786"


def ev(eid, item, amount, *, offset, quote=None, rec=REC, label="self", tx=None):
    data = {"expense": {"amount": amount, "currency": "CNY", "item": item}}
    if tx:
        data["transaction_id"] = tx
    return {
        "event_id": eid,
        "title": f"购买{item} (¥{amount:.2f})",
        "categories": ["expense", "food"],
        "activity_type": "meal",
        "data": data,
        "evidence": {"quote": quote or item, "source_label": label, "recording_id": rec,
                     "segment_id": "seg_1", "start_offset_seconds": offset, "claim_type": "order"},
    }


def test_duck_then_sausage_become_one_session_with_two_items():
    events = [
        ev("d1", "鸭腿", 10.0, offset=10.0, tx="T1"),
        ev("s1", "烤肠", 3.0, offset=44.0, quote="我加个烤肠", tx="T2"),
    ]
    result = build_purchase_session_summaries(events)
    assert result["stats"]["sessions"] == 1
    session = result["sessions"][0]
    assert [item["item"] for item in session["items"]] == ["鸭腿", "烤肠"]
    # 【返工】不再有合计字段：金额必须逐项列出（禁止当正式账本）
    assert "display_total" not in session
    assert session["amounts_listed_individually"] == [10.0, 3.0]
    assert session["not_a_ledger_total"] is True
    assert session["addon_links"][0]["basis"] == "explicit_addon"
    # 交易逐笔保留、不去重
    assert [ref["id"] for item in session["items"] for ref in item["transaction_refs"]] == ["T1", "T2"]
    assert all(item["payment_status"] == "unknown" for item in session["items"])


def test_same_hour_different_recording_is_not_linked():
    events = [
        ev("a1", "鸭腿", 10.0, offset=10.0, rec=REC),
        ev("b1", "烤肠", 3.0, offset=50.0, quote="来一根烤肠", rec="watch_OTHER"),
    ]
    result = build_purchase_session_summaries(events)
    assert result["stats"]["sessions"] == 2, "跨录音不关联"


def test_same_amount_close_in_time_without_addon_is_rejected():
    events = [
        ev("x1", "烤肠", 3.0, offset=100.0),
        ev("x2", "可乐", 3.0, offset=100.0 + 600.0),   # 10 分钟后，无追加语义
    ]
    result = build_purchase_session_summaries(events)
    assert result["stats"]["sessions"] == 2
    assert any(item["reason"] == "no_addon_link" for item in result["skipped"])


def test_broadcast_3_yuan_is_not_a_purchase_item():
    events = [
        ev("d1", "鸭腿", 10.0, offset=10.0),
        ev("p1", "收款", 3.0, offset=20.0, quote="微信收款三块", label="non_self"),
    ]
    result = build_purchase_session_summaries(events)
    assert result["stats"]["sessions"] == 1
    assert [item["item"] for item in result["sessions"][0]["items"]] == ["鸭腿"]
    assert any(item["reason"] == "broadcast_not_purchase" for item in result["skipped"])


def test_adjacent_context_without_addon_word_is_not_linked():
    """【返工 2026-10-25】同录音 ≤180s 相邻**不是**活动关联证据（用户明确边界）。"""
    events = [
        ev("d1", "鸭腿", 10.0, offset=10.0),
        ev("s1", "烤肠", 3.0, offset=40.0),   # 30 秒内、同录音，但没有追加语义
    ]
    result = build_purchase_session_summaries(events)
    assert result["stats"]["sessions"] == 2
    assert any(item["reason"] == "no_addon_link" for item in result["skipped"])


def test_addon_without_locatable_anchor_is_not_linked():
    """锚点没有 offset/segment → 无法定位，不关联。"""
    events = [
        ev("d1", "鸭腿", 10.0, offset=None),
        ev("s1", "烤肠", 3.0, offset=44.0, quote="我加个烤肠"),
    ]
    result = build_purchase_session_summaries(events)
    assert result["stats"]["sessions"] == 2


def test_unknown_actor_in_same_recording_is_not_same_person():
    """同录音但主体未知/不同 → 不当同一个人。"""
    events = [
        ev("d1", "鸭腿", 10.0, offset=10.0, label="self"),
        ev("s1", "烤肠", 3.0, offset=44.0, quote="我加个烤肠", label="unknown"),
    ]
    result = build_purchase_session_summaries(events)
    assert result["stats"]["sessions"] == 2


def test_transcript_addon_without_canonical_item_is_pending_not_booked():
    transcript = [
        {"source_id": "src_1", "text": "我加个烤肠", "source_label": "self",
         "recording_id": REC, "segment_id": "000026"},
    ]
    result = build_purchase_session_summaries([ev("d1", "鸭腿", 10.0, offset=10.0)], transcript_rows=transcript)
    assert result["pending_candidates"][0]["reason"] == "addon_mentioned_but_no_canonical_item"
    assert result["pending_candidates"][0]["status"] == "pending_candidate_not_booked"
    # 仍只有鸭腿一条，没假称烤肠已入账
    assert [i["item"] for i in result["sessions"][0]["items"]] == ["鸭腿"]


def test_cross_segment_local_offset_is_not_global_adjacency():
    """不同 segment 的局部 offset 不得当全局相邻：没有绝对时间就不能算间隔。"""
    base = ev("d1", "鸭腿", 10.0, offset=10.0)
    addon = ev("s1", "烤肠", 3.0, offset=5.0, quote="我加个烤肠")
    base["evidence"]["segment_id"] = "seg_1"
    addon["evidence"]["segment_id"] = "seg_2"
    result = build_purchase_session_summaries([base, addon])
    assert result["stats"]["sessions"] == 2, "跨 segment 且无绝对时间 → 不得用 offset 判定相邻"


def test_same_segment_offsets_are_comparable():
    base = ev("d1", "鸭腿", 10.0, offset=10.0)
    addon = ev("s1", "烤肠", 3.0, offset=40.0, quote="我加个烤肠")
    base["evidence"]["segment_id"] = addon["evidence"]["segment_id"] = "seg_1"
    result = build_purchase_session_summaries([base, addon])
    assert result["stats"]["sessions"] == 1
    assert result["sessions"][0]["addon_links"][0]["gap_seconds"] == 30.0
