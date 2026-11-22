"""Q364/Q369：餐费总额与追加点单必须被抽成 order 候选，背景播报继续隔离。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.contextual_extraction import meal_total_candidates  # noqa: E402

REC = {"recording_id": "watch_F786", "start_offset_seconds": 1.0}


def rec(sid, text, label="self", seg=None):
    return {**REC, "source_id": sid, "text": text, "source_label": label, "segment_id": seg}


def test_scenario_a_multi_dish_total_becomes_order_candidate():
    records = [
        rec("s1", "要一份这个咖喱牛肉炒饭。", seg="s1"),
        rec("s2", "然后一份儿炸薯条。", seg="s2"),
        rec("s3", "清补凉能不能给我加个雪球。", seg="s3"),
        rec("s4", "一共三十八。", seg="s4"),
    ]
    cands = meal_total_candidates(records)
    assert len(cands) == 1, cands
    cand = cands[0]
    assert cand["source_id"] == "s4"
    assert cand["amount"] == 38.0
    assert cand["claim_type"] == "order"
    assert "三十八" in cand["quote"]
    assert cand["amount_source"] == "quote_total"
    assert cand["segment_id"] == "s4"


def test_scenario_b_addon_order_becomes_candidate():
    records = [
        rec("a1", "来一份鸭腿饭", seg="a1"),
        rec("a2", "我加个烤肠。", seg="a2"),
        rec("a3", "三块。", seg="a3"),
    ]
    cands = meal_total_candidates(records)
    assert [c["amount"] for c in cands] == [3.0]
    assert cands[0]["source_id"] == "a3"


def test_scenario_c_drama_broadcast_is_isolated():
    records = [
        rec("d1", "玄宗门，收钱吧到账十三元。", label="non_self", seg="d1"),
        rec("d2", "收钱吧，到账十三元。", label="self", seg="d2"),   # 即使标 self，含播报标记也跳过
        rec("d3", "支付宝到账十元", label="non_self", seg="d3"),
    ]
    assert meal_total_candidates(records) == []


def test_existing_source_and_non_self_are_skipped():
    records = [rec("e1", "一共三十八"), rec("e2", "一共三十八", label="non_self")]
    assert meal_total_candidates(records, existing_source_ids={"e1"}) == []
    assert [c["source_id"] for c in meal_total_candidates(records)] == ["e1"]


def test_no_total_no_candidate():
    records = [rec("n1", "要一份咖喱牛肉炒饭"), rec("n2", "味道不错")]
    assert meal_total_candidates(records) == []


def test_ambiguous_chinese_numeral_is_not_guessed():
    records = [rec("m1", "一百二")]
    assert meal_total_candidates(records) == []
