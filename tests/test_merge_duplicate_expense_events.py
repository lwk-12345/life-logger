"""跨分段重复消费合并（2026-10-22 P1）。

实测 10-21：同一次录音 `rec_0f5a20101612` 的连续分段 16/17/18（20:46:42/:44/:46）
原文是一句「花了26.8点了一份 / 丑宝汉堡王套餐 / 三件套26.8」，
分段级独立抽取 → 3 条事件（1 条带金额、2 条只有 items），页面出 3 张卡。

本文件锁四件事：
  A 正例：三段落一句话 → 合并成 1 条（其余只打 suppressed，不删除）
  B 反例：同商户 60s 内但两条都带金额（真实两笔）→ 不合并
  C 反例：不同商户 / 时间差 >60s / 非消费事件 → 不合并
  D 金额硬约束：合并前后账本总额与交易数完全不变
"""
from __future__ import annotations

import copy

from life_logger.contextual_extraction import merge_duplicate_expense_events
from life_logger.expense_policy import build_expense_ledger

REC = "rec_0f5a20101612"


def _event(sid: str, clock: str, *, title: str, quote: str, categories, expense=None, rec=REC):
    evidence = {
        "quote": quote,
        "segment_id": f"{rec}_seg_{sid}",
        "extractor": "deepseek_context_batch",
    }
    payload = {"categories": list(categories), "title": title, "evidence": evidence,
               "event_id": f"evt_{rec}_{sid}", "time": {"absolute": f"2026-10-21T{clock}+00:00"}}
    if expense is not None:
        payload["data"] = {"expense": dict(expense)}
    return payload


def _three_segments():
    return [
        _event("16", "12:46:42", title="花26.8元点了一份套餐", quote="花了26.8点了一份",
               categories=["expense"],
               expense={"amount": 26.8, "currency": "CNY", "merchant": "汉堡王",
                        "items": ["三件套套餐"], "transaction_evidence": "花了26.8点了一份"}),
        _event("17", "12:46:44", title="汉堡王套餐（三件套）", quote="丑宝汉堡王套餐",
               categories=["food"], expense={"items": ["丑宝汉堡王套餐三件套"]}),
        _event("18", "12:46:46", title="汉堡王三件套套餐（26.8元）", quote="三件套26.8",
               categories=["food"],
               expense={"merchant": "汉堡王", "items": ["丑宝汉堡王套餐三件套"],
                        "transaction_evidence": "三件套26.8"}),
    ]


def test_positive_three_fragments_collapse_into_one_purchase():
    events = _three_segments()
    merged = merge_duplicate_expense_events(events)
    visible = [e for e in merged if not e.get("suppressed")]
    hidden = [e for e in merged if e.get("suppressed")]

    assert len(merged) == 3, "原始事件不能删除，只能打标记"
    assert len(visible) == 1
    assert len(hidden) == 2
    anchor = visible[0]
    assert anchor["data"]["expense"]["amount"] == 26.8
    assert anchor["data"]["expense"]["merchant"] == "汉堡王"
    assert len(anchor["merged_from"]) == 2
    assert anchor["merged_event_count"] == 3
    assert sorted(anchor["merged_quotes"]) == ["三件套26.8", "丑宝汉堡王套餐"]
    for donor in hidden:
        assert donor["merged_into"] == anchor["event_id"]
        assert donor["merge_reason"] == "same_purchase_split_segments"


def test_merge_keeps_ledger_total_unchanged():
    events = _three_segments()
    before = build_expense_ledger(events)
    after = build_expense_ledger(merge_duplicate_expense_events(events))
    assert before["total_amount"] == after["total_amount"] == 26.8
    assert before["transaction_count"] == after["transaction_count"] == 1


def test_repeated_merge_is_idempotent():
    once = merge_duplicate_expense_events(_three_segments())
    twice = merge_duplicate_expense_events(copy.deepcopy(once))
    anchors = [e for e in twice if e.get("merged_from")]
    assert len(anchors) == 1
    assert len(anchors[0]["merged_from"]) == 2, "重复执行不得反复追加 merged_from"
    assert sum(1 for e in twice if e.get("suppressed")) == 2


def test_two_real_purchases_with_amounts_are_not_merged():
    """10-12 02:57 实测：同金额两笔（真实连买）必须保留两条。"""
    events = [
        _event("20", "02:57:21", title="购买烟花费3元", quote="来包烟三块",
               categories=["expense"], expense={"amount": 3.0, "currency": "CNY"}),
        _event("21", "02:57:30", title="购买烟花费3元", quote="再来一包烟三块",
               categories=["expense"], expense={"amount": 3.0, "currency": "CNY"}),
    ]
    merged = merge_duplicate_expense_events(events)
    assert not [e for e in merged if e.get("suppressed")]
    assert build_expense_ledger(merged)["total_amount"] == 6.0


def test_different_merchants_are_not_merged():
    events = [
        _event("30", "12:10:00", title="汉堡王套餐", quote="汉堡王套餐",
               categories=["expense"], expense={"amount": 26.8, "merchant": "汉堡王"}),
        _event("31", "12:10:20", title="星巴克咖啡", quote="星巴克咖啡",
               categories=["food"], expense={"merchant": "星巴克"}),
    ]
    merged = merge_duplicate_expense_events(events)
    assert not [e for e in merged if e.get("suppressed")]


def test_same_merchant_outside_window_is_not_merged():
    events = [
        _event("40", "12:00:00", title="汉堡王套餐", quote="汉堡王套餐",
               categories=["expense"], expense={"amount": 26.8, "merchant": "汉堡王"}),
        _event("41", "12:03:00", title="汉堡王套餐（三件套）", quote="汉堡王套餐三件套",
               categories=["food"], expense={"merchant": "汉堡王", "items": ["三件套"]}),
    ]
    merged = merge_duplicate_expense_events(events)
    assert not [e for e in merged if e.get("suppressed")], "超过 60s 的同类事件不得合并"


def test_non_expense_events_never_merge():
    events = [
        _event("50", "12:00:00", title="查询：X上6Luna对比", quote="查一下X上6Luna的对比",
               categories=["todo"]),
        _event("51", "12:00:05", title="查询：X上6Luna和LunaMax对比",
               quote="查一下X上6Luna和LunaMax的对比", categories=["todo"]),
    ]
    merged = merge_duplicate_expense_events(events)
    assert not [e for e in merged if e.get("suppressed")]


def test_different_recordings_never_merge():
    events = _three_segments()
    events[1]["evidence"]["segment_id"] = "rec_other_seg_000017"
    merged = merge_duplicate_expense_events(events)
    suppressed = [e for e in merged if e.get("suppressed")]
    assert len(suppressed) <= 1, "跨录音不应合并成一条"
