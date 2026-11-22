"""Tests for exact projection identity audit integration in event_view.

Verifies:
1. Real production dates 2026-10-24 and 2026-10-25 return unchanged events collection,
   with additive projection_identity_audit metadata and honest counting.
2. 5000 events same-recording benchmark runs in near-linear sub-second (< 1.0s).
3. 11 events same-ID with tail duplicate pair: prioritizes coordinate sub-bucket, detects duplicate,
   explicitly flags audit_incomplete=True, truncated_buckets_count=1, and records truncated_bucket_keys.
4. Simulated cross-projection same-origin events produce positive alias suggestions.
5. Conflict cases (different coordinates, speakers, or utterance IDs) stay different/conflict.
6. Business entities (order vs transaction vs activity) remain distinct.
7. Incomplete coordinates result in unknown/fail-open visibility.
"""

from __future__ import annotations

import time
from pathlib import Path

from life_logger.event_view import build_event_view
from life_logger.exact_projection_identity import (
    evaluate_projection_identity,
    build_projection_identity_audit,
)

ROOT = Path(__file__).resolve().parents[1]
PROD_ROOT = ROOT / "output/local-web"

# [2026-11-04 夹具入库] 这条资源无法入库（体积过大 / 含真实录音 / 含本机配置），
# 缺失时 skip 而不是 fail —— 新克隆不该因为缺少本机数据而红。
import pytest as _pytest  # noqa: E402

if not ((ROOT / "output" / "local-web").is_dir()):
    _pytest.skip(
        "缺少不入库的夹具：output/local-web 真实运行数据",
        allow_module_level=True,
    )


def test_real_production_event_view_additive_audit_unchanged_events():
    """真实生产数据：events 集合与金额完全不变，增量挂载 projection_identity_audit，真实统计。"""
    # 1. 2026-10-24
    res_24 = build_event_view(str(PROD_ROOT), date_from="2026-10-24", date_to="2026-10-24", scope="provisional", view="all", use_cache=False)
    events_24 = res_24["events"]
    assert len(events_24) == 7
    assert "projection_identity_audit" in res_24
    assert "projection_identity_audit" in res_24["metadata"]
    audit_24 = res_24["projection_identity_audit"]
    assert audit_24["summary"]["same_positive_count"] == 0
    assert audit_24["actual_evaluated_pairs_count"] == 0
    assert audit_24["not_evaluated_pairs_count"] == 21
    assert audit_24["audit_incomplete"] is False
    assert audit_24["bucket_complete"] is True
    assert audit_24["truncated_buckets_count"] == 0

    # 2. 2026-10-25
    res_25 = build_event_view(str(PROD_ROOT), date_from="2026-10-25", date_to="2026-10-25", scope="provisional", view="all", use_cache=False)
    events_25 = res_25["events"]
    assert len(events_25) == res_25["metadata"]["selected_counts"]["event_count"]
    assert len(events_25) >= 20
    assert "projection_identity_audit" in res_25
    audit_25 = res_25["projection_identity_audit"]
    assert audit_25["summary"]["same_positive_count"] == 0
    assert audit_25["actual_evaluated_pairs_count"] == 0
    assert audit_25["not_evaluated_pairs_count"] == audit_25["total_possible_pairs_count"]
    assert audit_25["audit_incomplete"] is False
    assert audit_25["bucket_complete"] is True


def test_performance_5000_events_same_recording_near_linear_subsecond():
    """性能基准：5000 条同 recording 但不同 canonical 的事件，耗时近线性 < 1 秒。"""
    events = [
        {
            "event_id": f"ev_{i}",
            "canonical_event_id": f"canon_{i}",
            "title": f"事件_{i}",
            "evidence": {
                "recording_id": "rec_massive_same_session",
                "segment_id": f"seg_{i % 50}",
                "start_offset_seconds": i * 1.0,
                "end_offset_seconds": i * 1.0 + 0.8,
                "source_label": "self",
            },
        }
        for i in range(5000)
    ]
    start = time.perf_counter()
    audit = build_projection_identity_audit(events)
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0, f"Performance failed: {elapsed:.4f}s >= 1.0s"
    assert audit["actual_evaluated_pairs_count"] == 0
    assert audit["not_evaluated_pairs_count"] == 12497500
    assert audit["summary"]["same_positive_count"] == 0
    assert audit["audit_incomplete"] is False


def test_11_events_same_id_prioritizes_tail_duplicate_and_flags_incomplete():
    """Q430 关键用例：11 个事件同一 ID (55 对，超 50 预算)。
    末尾事件与第 1 个事件坐标匹配，优先子桶必须检出该正例，同时明确标记 audit_incomplete 与 truncated_bucket_keys。
    """
    events = []
    # 构造 11 个事件具有相同 canonical_event_id
    # 前 10 个在不同 segment
    for i in range(10):
        events.append({
            "event_id": f"ev_{i}",
            "canonical_event_id": "canon_multi_11",
            "title": f"事件_{i}",
            "evidence": {
                "recording_id": "rec_multi",
                "segment_id": f"seg_distinct_{i}",
                "start_offset_seconds": 10.0,
                "end_offset_seconds": 11.0,
                "source_label": "self",
            },
        })
    # 第 11 个事件 (ev_10)：与第 1 个事件 (ev_0) 物理坐标完全重合！
    events.append({
        "event_id": "ev_10_tail_duplicate",
        "canonical_event_id": "canon_multi_11",
        "title": "事件_尾部重复",
        "evidence": {
            "recording_id": "rec_multi",
            "segment_id": "seg_distinct_0",  # 与 ev_0 同 segment
            "start_offset_seconds": 10.0,
            "end_offset_seconds": 11.0,
            "source_label": "self",
        },
    })

    audit = build_projection_identity_audit(events)

    # 1. 预算与截断断言
    assert audit["actual_evaluated_pairs_count"] == 50
    assert audit["not_evaluated_pairs_count"] == 5  # 55 - 50 = 5
    assert audit["audit_incomplete"] is True
    assert audit["bucket_complete"] is False
    assert audit["truncated_buckets_count"] == 1    # 每个桶只计 1 次！
    assert audit["truncated_bucket_keys"] == ["canonical:canon_multi_11"]
    assert "canonical:canon_multi_11" in audit["truncated_reasons"]

    # 2. 优先子桶捕获断言：末尾重复对由于同坐标优先级最高，成功在 50 预算内被检出！
    assert audit["summary"]["same_positive_count"] == 1
    assert len(audit["alias_suggestions"]) == 1
    assert audit["alias_suggestions"][0]["matched_anchor"] == "canonical:canon_multi_11"


def test_cross_projection_same_event_positive_alias_suggestion():
    """模拟同一物理发声跨 live 与 formal 投影：精准识别 same 并给出 alias 建议。"""
    ev_live = {
        "event_id": "evt_live_duck_1",
        "canonical_event_id": "canon_duck_purchase",
        "title": "购买鸭腿 (¥10.00)",
        "source_kind": "live",
        "evidence": {
            "recording_id": "rec_001",
            "segment_id": "seg_010",
            "start_offset_seconds": 1547.845,
            "end_offset_seconds": 1549.025,
            "source_label": "self",
            "quote": "来一份鸭腿",
        },
        "data": {"expense": {"amount": 10.0, "currency": "CNY"}},
    }
    ev_formal = {
        "event_id": "evt_formal_duck_1",
        "canonical_event_id": "canon_duck_purchase",
        "title": "购买鸭腿一份 (¥10.00)",
        "source_kind": "formal",
        "evidence": {
            "recording_id": "rec_001",
            "segment_id": "seg_010",
            "start_offset_seconds": 1547.850,
            "end_offset_seconds": 1549.020,
            "source_label": "self",
            "quote": "来一份鸭腿",
        },
        "data": {"expense": {"amount": 10.0, "currency": "CNY"}},
    }

    audit = build_projection_identity_audit([ev_live, ev_formal])
    assert audit["summary"]["same_positive_count"] == 1
    assert len(audit["alias_suggestions"]) == 1
    suggestion = audit["alias_suggestions"][0]
    assert suggestion["primary_event_id"] == "evt_formal_duck_1"
    assert suggestion["suggested_alias_id"] == "evt_live_duck_1"
    assert suggestion["matched_anchor"] == "canonical:canon_duck_purchase"


def test_conflict_veto_preserves_both_visibility():
    """冲突否决：同 canonical 但 segment 冲突、说话人冲突或话语不同，严格判定 different/conflict。"""
    # 1. segment 冲突 (C1)
    e1 = {
        "event_id": "e1", "canonical_event_id": "canon_same",
        "evidence": {"recording_id": "r1", "segment_id": "s1", "start_offset_seconds": 1.0, "end_offset_seconds": 2.0}
    }
    e2 = {
        "event_id": "e2", "canonical_event_id": "canon_same",
        "evidence": {"recording_id": "r1", "segment_id": "s2", "start_offset_seconds": 1.0, "end_offset_seconds": 2.0}
    }
    res_c1 = evaluate_projection_identity(e1, e2)
    assert res_c1["verdict"] == "different"
    assert res_c1["action"] == "retain_both"

    # 2. 话语实例冲突 (C3)
    e3 = {
        "event_id": "e3", "utterance_id": "u1",
        "evidence": {"recording_id": "r1", "segment_id": "s1", "start_offset_seconds": 10.0, "end_offset_seconds": 11.0}
    }
    e4 = {
        "event_id": "e4", "utterance_id": "u2",
        "evidence": {"recording_id": "r1", "segment_id": "s1", "start_offset_seconds": 10.2, "end_offset_seconds": 11.2}
    }
    res_c3 = evaluate_projection_identity(e3, e4)
    assert res_c3["verdict"] == "different"
    assert res_c3["action"] == "retain_both"

    # 3. 说话人冲突 (C6)
    e5 = {
        "event_id": "e5",
        "evidence": {"recording_id": "r1", "segment_id": "s1", "start_offset_seconds": 5.0, "end_offset_seconds": 6.0, "source_label": "self"}
    }
    e6 = {
        "event_id": "e6",
        "evidence": {"recording_id": "r1", "segment_id": "s1", "start_offset_seconds": 5.1, "end_offset_seconds": 6.1, "source_label": "other"}
    }
    res_c6 = evaluate_projection_identity(e5, e6)
    assert res_c6["verdict"] == "different"
    assert res_c6["action"] == "retain_both"


def test_business_entities_isolation():
    """业务实体隔离：order_id 与 transaction_id 绝不作为同一投影合并。"""
    e_order = {"event_id": "eo", "order_id": "ORD_001", "canonical_event_id": "canon_same"}
    e_tx = {"event_id": "et", "transaction_id": "TX_001", "canonical_event_id": "canon_same"}
    res = evaluate_projection_identity(e_order, e_tx)
    assert res["verdict"] == "different"
    assert res["action"] == "retain_both"
    assert res["reason"] == "distinct_business_entities"


def test_incomplete_coordinates_fail_open_unknown():
    """缺坐标一律降级为 unknown，绝不盲目判定已证或静默抑制。"""
    e_no_coord = {"event_id": "en", "canonical_event_id": "canon_same"}
    e_with_coord = {
        "event_id": "ew", "canonical_event_id": "canon_same",
        "evidence": {"recording_id": "r1", "segment_id": "s1", "start_offset_seconds": 1.0, "end_offset_seconds": 2.0}
    }
    res = evaluate_projection_identity(e_no_coord, e_with_coord)
    assert res["verdict"] == "unknown"
    assert res["action"] == "retain_both"
    assert res["reason"] == "unverified_without_coordinates"
