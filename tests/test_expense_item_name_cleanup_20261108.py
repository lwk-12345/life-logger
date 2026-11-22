from __future__ import annotations

from life_logger.expense_policy import build_expense_ledger, clean_expense_item_name


def test_clean_expense_item_name_strips_narrative_and_amounts() -> None:
    cases = [
        (("冰杯", "买冰杯花费2元"), "冰杯"),
        (("买冰杯花费2元", "买冰杯花费2元"), "冰杯"),
        (("去买菜，买菜花了十六", "去买菜，买菜花了十六"), "买菜"),
        (("买零食花费14元", "买零食花费14元"), "零食"),
        (("买一包红塔山", "买一包红塔山"), "红塔山"),
        (("陈述一桶水并花了四块", "陈述一桶水并花了四块"), "一桶水"),
        (("称买了一瓶椰子水，四块五", "称买了一瓶椰子水，四块五"), "椰子水"),
        (("称练完之后买了一包烟", "称练完之后买了一包烟"), "买烟"),
        (("称又买了一瓶水", "称又买了一瓶水"), "买水"),
        (("买一份炒河粉", "买一份炒河粉"), "炒河粉"),
        (("买烟（硬盒红塔山）", "买烟（硬盒红塔山）"), "硬盒红塔山"),
        (("线下付款（收款播报）", "线下付款（收款播报）"), "线下付款（收款播报）"),
    ]
    for (raw_item, raw_title), expected in cases:
        assert clean_expense_item_name(raw_item, raw_title) == expected


def test_build_expense_ledger_outputs_cleaned_item_names_without_changing_amounts() -> None:
    events = [
        {
            "event_id": "evt_1",
            "title": "买冰杯花费2元",
            "categories": ["expense"],
            "time": {"absolute": "2026-11-05T06:10:00Z", "clock": "14:10"},
            "evidence": {"quote": "买了个冰杯花了两块钱。", "source_label": "self"},
            "data": {"expense": {"item": "买冰杯花费2元", "amount": 2.0, "currency": "CNY"}},
        },
        {
            "event_id": "evt_2",
            "title": "去买菜，买菜花了十六",
            "categories": ["expense"],
            "time": {"absolute": "2026-11-05T07:20:00Z", "clock": "15:20"},
            "evidence": {"quote": "去买菜，买菜花了十六块钱。", "source_label": "self"},
            "data": {"expense": {"item": "去买菜，买菜花了十六", "amount": 16.0, "currency": "CNY"}},
        },
    ]
    ledger = build_expense_ledger(events)
    assert ledger["total_amount"] == 18.0
    assert [tx["item"] for tx in ledger["payment_transactions"]] == ["冰杯", "买菜"]
