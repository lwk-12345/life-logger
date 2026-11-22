"""点餐总额绑定与 event_view 的边界（当前**未接线**，先固化事实与阻塞）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.event_episodes import bind_meal_totals  # noqa: E402
from life_logger.event_view import build_event_view  # noqa: E402
from life_logger.expense_policy import build_expense_ledger  # noqa: E402

WEB_ROOT = ROOT / "output" / "local-web"
TOTAL_QUOTES = ("三十八", "38元", "三十八块", "烤肠", "三块")


def total(events):
    ledger = build_expense_ledger(events)
    return ledger.get("total_amount"), ledger.get("transaction_count")


def test_binding_works_on_synthetic_events():
    events = [
        {"event_id": "d1", "title": "咖喱牛肉炒饭", "categories": ["food"], "activity_type": "meal",
         "time": {"precision": "exact", "absolute": "2026-10-24T12:10:00+08:00"}, "data": {},
         "evidence": {"quote": "来一份咖喱牛肉炒饭", "source_label": "self",
                      "recording_id": "watch_A", "claim_type": "order"}},
        {"event_id": "t1", "title": "一共三十八", "categories": ["food"], "activity_type": "meal",
         "time": {"precision": "exact", "absolute": "2026-10-24T12:20:00+08:00"}, "data": {},
         "evidence": {"quote": "一共三十八", "source_label": "self",
                      "recording_id": "watch_A", "claim_type": "order"}},
    ]
    bound = bind_meal_totals(events)
    assert [(b["event_id"], b["amount"]) for b in bound["bindings"]] == [("t1", 38.0)]
    assert total(bound["events"]) == (38.0, 1)


@pytest.mark.skipif(not WEB_ROOT.is_dir(), reason="需要真实 store")
def test_real_store_has_no_total_quote_yet_and_ledger_unchanged():
    """阻塞记录：10-24/10-25 的 canonical 事件里**没有**"三十八/烤肠/三块"原文，
    因此 event_view 接入后也无法透出 38.0；接入前账本保持原值，不得误计。"""
    for day, expected in (("2026-10-24", 0.0), ("2026-10-25", 10.0)):
        view = build_event_view(WEB_ROOT, date_from=day, date_to=day,
                                scope="provisional", view="display", use_cache=True)
        assert total(view["events"])[0] == expected, day
        quoted = " ".join(
            str((event.get("evidence") or {}).get("quote") or "")
            for event in view["events"] if isinstance(event, dict)
        )
        assert not any(key in quoted for key in TOTAL_QUOTES), (
            f"{day} 出现了总额原文，应把断言改为 38.0/3.0 并接线"
        )
