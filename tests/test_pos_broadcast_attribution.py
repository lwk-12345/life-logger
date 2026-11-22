"""P-46：别人的支付播报（non_self POS）不许计入本人账本。

实测背景（2026-10-20）：用户在小店吃饭，麦克风录进**别的顾客**的
「支付宝到账十五元 / 十元」，抽取层生成 `pos_payment_broadcast` 时完全不看
`source_label`，账本层也不过滤 → 陌生人付款进了本人账本。
22 天共 6 笔 / ¥42.80（10-16 ¥2.0、10-17 ¥1.8+¥11.0、10-19 ¥3.0、10-20 ¥15.0+¥10.0）。

本文件锁三件事：
1. `non_self` 的 POS 播报 → 金额不进 `transactions`，改落 `pending_transactions`；
2. 事件本身**仍存在**（零数据丢失），只是不计钱；
3. `self` / 非 POS 事件的记账行为**完全不变**。
"""
from __future__ import annotations

from life_logger.expense_policy import build_expense_ledger


def _event(event_id: str, amount: float, *, label: str, activity="pos_payment_broadcast",
           quote="支付宝到账十五元", title="线下支付：手呢 (¥15.00)"):
    return {
        "event_id": event_id,
        "title": title,
        "categories": ["expense"],
        "activity_type": activity,
        "time": {"absolute": "2026-10-20T00:01:00+00:00", "clock": "08:01"},
        "evidence": {
            "quote": quote,
            "source_label": label,
            "title_source": "pos_broadcast" if activity == "pos_payment_broadcast" else None,
            "item_source": "non_self" if label == "non_self" else "self",
            "order_binding": label != "non_self",
        },
        "data": {"expense": {"amount": amount, "currency": "CNY", "item": "手呢"}},
    }


def test_non_self_pos_broadcast_is_not_counted_as_my_expense():
    a = _event("evt_pos_a", 15.0, label="non_self", quote="支付宝到账十五元")
    b = _event("evt_pos_b", 10.0, label="non_self", quote="支付宝到账十元",
               title="线下支付：支付宝到账十五元 (¥10.00)")
    b["time"] = {"absolute": "2026-10-20T00:02:00+00:00", "clock": "08:02"}
    ledger = build_expense_ledger([a, b])

    assert ledger["total_amount"] == 0.0
    assert ledger["transaction_count"] == 0
    # B 档：播报类不进 total，单独进 pending_broadcast（可一键确认）
    assert ledger["pending_broadcast_count"] == 2
    assert ledger["pending_broadcast_total_cny"] == 25.0
    assert all(
        p.get("reason") == "pos_broadcast_pending_confirmation"
        for p in ledger["pending_broadcast"]
    )
    # 事件没有被删除：pending 里还带着原金额与原文，用户能自己确认
    pending = list(ledger["pending_broadcast"])
    assert {p["event_id"] for p in pending} == {"evt_pos_a", "evt_pos_b"}
    assert {p["proposed_amount"] for p in pending} == {15.0, 10.0}, "播报金额要留在候选值里"


def test_self_pos_broadcast_with_outflow_wording_still_counts():
    """本人自述的付款（A 档）照常计入；播报类才降级 B 档。"""
    ledger = build_expense_ledger([
        _event("evt_ctx_self", 8.0, label="self", activity="meal",
               quote="要一个肉夹馍八块", title="购买肉夹馍 (¥8.00)"),
    ])
    assert ledger["total_amount"] == 8.0
    assert ledger["transaction_count"] == 1


def test_self_describing_a_broadcast_still_goes_to_pending():
    """10-18 实测反例：`self` 说「像那个播报收款收款二十二元」——描述播报现象，不是花钱。

    用户 10-22 裁决：播报「很可能」是本人付款（店家音箱），但不是 100%，
    所以一律降级 B 档待确认；只有本人明确说「花了/买了 X」才是 A 档。
    """
    ledger = build_expense_ledger([
        _event("evt_ctx_18", 22.0, label="self", activity="other",
               quote="呃，也可以，像那个播报收款收款二十二元。",
               title="线下支付：二元配一个 (¥22.00)"),
    ])
    assert ledger["total_amount"] == 0.0
    assert ledger["pending_broadcast_count"] == 1

def test_self_plain_purchase_is_tier_a_and_counts():
    """A 档：本人自述「花了25」→ 正常计入。"""
    ledger = build_expense_ledger([
        _event("evt_ctx_a", 25.0, label="self", activity="meal",
               quote="然后肉夹馍是八块，煎豆腐七块，然后捞粉是这个十块，二十五",
               title="购买肉夹馍、煎豆腐、捞粉"),
    ])
    assert ledger["total_amount"] == 25.0
    assert ledger["pending_broadcast_count"] == 0


def test_ambiguous_pos_broadcast_goes_to_pending_not_ledger():
    """POS 播报但文本无金额方向词 → 仍按 B 档待确认，不进总额。"""
    ledger = build_expense_ledger([
        _event("evt_pos_unknown", 3.0, label="unknown", quote="三元", title="线下扫码支付 (¥3.00)"),
    ])
    assert ledger["total_amount"] == 0.0
    assert ledger["pending_broadcast_count"] == 1


def test_non_self_non_pos_event_is_handled_by_existing_rules_only():
    """POS 闸门只作用于 POS 播报；普通 non_self 对话事件仍走既有 non_fact 规则。

    （既有口径：non_self 的消费叙述不算财务事实——见 contextual_extraction:2359。
    本测试只是确认新闸门没有顺带改动这条老径。）
    """
    ledger = build_expense_ledger([
        _event("evt_ctx_x", 26.8, label="non_self",
               activity="meal", title="购买汉堡王套餐 (¥26.8)", quote="花了26.8点了一份"),
    ])
    reasons = {p.get("reason") for p in ledger["pending_transactions"]}
    assert "pos_broadcast_unattributed" not in reasons, "新闸门不许波及非 POS 事件"


def test_mixed_day_total_matches_expected_after_gate():
    """10-20 场景复现：¥25 餐 + 两条 non_self POS（¥15/¥10）→ 账本只算 ¥25。"""
    events = [
        _event("evt_ctx_meal", 25.0, label="self", activity="meal",
               title="购买肉夹馍8元、煎豆腐7元、捞粉10元", quote="肉夹馍八块 煎豆腐七块 捞粉十块"),
        _event("evt_pos_a", 15.0, label="non_self"),
        _event("evt_pos_b", 10.0, label="non_self"),
    ]
    ledger = build_expense_ledger(events)
    assert ledger["total_amount"] == 25.0
    assert ledger["transaction_count"] == 1


def test_itemized_aggregate_and_details_are_counted_once():
    """10-20 实测：同一段音频被 live/batch 各抽一遍（合计 ¥25 vs 明细 8+7+10）。

    四条 offset 完全相同 → 只算一次；保留明细（信息更多），合计进 duplicates_removed。
    """
    base = "2026-10-20T02:00:55+08:00"

    def _e(eid, amount, title, quote):
        return {
            "event_id": eid, "title": title, "categories": ["expense"],
            "activity_type": "meal", "review_status": "reviewed",
            "time": {"absolute": base}, "evidence": {"quote": quote, "source_label": "self",
                                                     "reported_at": base},
            "data": {"expense": {"amount": amount, "currency": "CNY", "item": title}},
        }

    events = [
        _e("agg", 25.0, "购买肉夹馍8元、煎豆腐7元、捞粉10元",
           "肉夹馍是八块，煎豆腐七块，然后捞粉是这个十块"),
        _e("d1", 8.0, "购买肉夹馍8元", "肉夹馍八块"),
        _e("d2", 7.0, "购买煎豆腐7元", "煎豆腐七块"),
        _e("d3", 10.0, "购买捞粉10元", "捞粉十块"),
    ]
    ledger = build_expense_ledger(events)
    assert ledger["total_amount"] == 25.0, "一顿饭只能算一次"
    assert ledger["transaction_count"] == 3
    removed = [r for r in ledger["duplicates_removed"]
               if r.get("reason") == "itemized_aggregate_matches_nearby_details"]
    assert len(removed) == 1 and removed[0]["event_id"] == "agg"


def _ev(eid, amount, *, recording, quote, title, at, extractor="deepseek_context_batch",
        segment=None):
    return {
        "event_id": eid, "title": title, "categories": ["expense"],
        "activity_type": "purchase", "review_status": "reviewed",
        "time": {"absolute": at},
        "evidence": {"quote": quote, "source_label": "self", "reported_at": at,
                     "recording_id": recording, "segment_id": segment, "extractor": extractor},
        "data": {"expense": {"amount": amount, "currency": "CNY", "item": title}},
    }


def test_cross_channel_order_and_payment_are_merged_once():
    """10-19 实测：同一次 watch 录音里「点单」与「付款」各抽一遍 → 只算一笔。

    原始转录：23:53:42「要一个肉夹馍」→ 23:54:59「啊，付过了老板」→ 23:55:01「八块」，
    相隔 82 秒、跨两个底层 segment，但确实是同一笔 ¥8。
    """
    events = [
        _ev("evt_order_9b2c5747dd841eae", 8.0, recording="watch_6EBCD807CC3B4177A7A208DCC08DFD56",
            quote="要一个肉夹馍 八块", title="购买肉夹馍 (¥8.00)",
            at="2026-10-19T23:53:42+08:00", extractor=None),
        _ev("evt_ctx_fad147c6dc7e0781", 8.0, recording="watch_6EBCD807CC3B4177A7A208DCC08DFD56",
            quote="八块", title="付款8元", at="2026-10-19T23:55:01+08:00",
            segment="watch_000075_000002"),
    ]
    ledger = build_expense_ledger(events)
    assert ledger["total_amount"] == 8.0, "点单+付款只能是同一笔 ¥8"
    assert ledger["transaction_count"] == 1
    removed = [r for r in ledger["duplicates_removed"]
               if r.get("reason") == "cross_channel_order_duplicate"]
    assert len(removed) == 1
    assert removed[0]["event_id"] == "evt_ctx_fad147c6dc7e0781"
    assert removed[0]["merged_into"] == "evt_order_9b2c5747dd841eae"


def test_two_genuine_same_amount_purchases_are_not_merged():
    """10-18 实测防回归：¥15.9 止汗露(off 1122) 与 ¥15.9 冷冻菠菜(off 3364)。

    相隔 37 分钟、两条都是 ctx 通道、物品名不同 → 绝不能合并。
    """
    events = [
        _ev("evt_ctx_5620b1067eff5dfe", 15.9, recording="rec_9a256ba273b2",
            quote="又花了十五块九买了一份儿。止汗露。",
            title="购买止汗露，花费15.9元", at="2026-10-18T10:00:00+08:00",
            segment="rec_9a256ba273b2_seg_000020"),
        _ev("evt_ctx_fb318011c36c6a12", 15.9, recording="rec_9a256ba273b2",
            quote="花了十五块九买了一千克的冷冻菠菜。本来是八块九的。",
            title="购买冷冻菠菜1千克，15.9元", at="2026-10-18T10:37:00+08:00",
            segment="rec_9a256ba273b2_seg_000047"),
    ]
    ledger = build_expense_ledger(events)
    assert ledger["total_amount"] == 31.8, "两笔真实消费都要保留"
    assert ledger["transaction_count"] == 2
    assert not [r for r in ledger["duplicates_removed"]
                if r.get("reason") == "cross_channel_order_duplicate"]
