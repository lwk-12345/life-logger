"""人工复核：只影响账本口径，原始事件不动，删掉决定就恢复。"""
import json

import pytest

from life_logger import day_facts
from life_logger.expense_policy import build_expense_ledger


def _event(event_id="e1", amount=10):
    return {
        "event_id": event_id,
        "title": "买水",
        "categories": ["expense"],
        "data": {"expense": {"amount": amount, "currency": "CNY", "item": "水"}},
        "evidence": {"quote": f"买了一瓶水，{amount}块钱", "source_label": "self"},
        "time": {"absolute": "2026-10-09T10:00:00+08:00"},
    }


def test_no_review_changes_nothing():
    assert build_expense_ledger([_event()], review={}) == build_expense_ledger([_event()])


def test_excluded_item_leaves_the_total_but_stays_auditable():
    ledger = build_expense_ledger([_event("keep", 10), _event("drop", 3)], review={"drop": "exclude"})
    assert ledger["total_amount"] == 10
    assert [t["event_id"] for t in ledger["transactions"]] == ["keep"]
    assert any(e["event_id"] == "drop" and e["reason"] == "user_review_excluded" for e in ledger["excluded_events"])


def test_pending_item_moves_to_pending_and_keeps_the_candidate_amount():
    ledger = build_expense_ledger([_event("keep", 10), _event("ask", 68)], review={"ask": "pending"})
    assert ledger["total_amount"] == 10
    pending = [p for p in ledger["pending_transactions"] if p["event_id"] == "ask"]
    assert pending and pending[0]["reason"] == "user_review_pending"
    assert pending[0]["proposed_amount"] == 68


def test_unknown_action_is_ignored_rather_than_dropping_money():
    assert build_expense_ledger([_event()], review={"e1": "oops"})["total_amount"] == 10


def test_review_applies_to_activity_items():
    activity = {"activity_id": "a1", "title": "买水", "items": [{
        "action": "买了一瓶水", "status": "已完成", "amount": 5, "currency": "CNY",
        "evidence": [{"quote": "买了一瓶水，5块钱", "source_label": "self"}],
    }]}
    ledger = build_expense_ledger([], activities=[activity], review={"a1#it1": "exclude"})
    assert ledger["total_amount"] == 0


@pytest.fixture
def review_root(tmp_path, monkeypatch):
    monkeypatch.setattr(day_facts, "PROJECT_ROOT", tmp_path)
    root = tmp_path / "output/local-web"
    root.mkdir(parents=True)
    return root


def _write(root, decisions):
    (root / "ledger-review.json").write_text(json.dumps({"schema": "ledger-review-v1", "decisions": decisions}))


def test_every_entry_point_honours_the_review(review_root):
    _write(review_root, {"e1": {"action": "exclude"}})
    assert day_facts.event_only_expense_ledger([_event()])["total_amount"] == 0
    assert day_facts.day_expense_ledger([_event()], "2026-10-09")["total_amount"] == 0
    assert day_facts.range_expense_ledger([_event()], "2026-10-09", "2026-10-09")["total_amount"] == 0


def test_removing_the_decision_restores_the_amount(review_root):
    _write(review_root, {"e1": {"action": "exclude"}})
    assert day_facts.event_only_expense_ledger([_event()])["total_amount"] == 0
    _write(review_root, {})
    assert day_facts.event_only_expense_ledger([_event()])["total_amount"] == 10


@pytest.mark.parametrize("content", ["not json", json.dumps({"decisions": "bad"}), json.dumps([1])])
def test_broken_review_file_never_changes_the_money(review_root, content):
    (review_root / "ledger-review.json").write_text(content)
    assert day_facts.event_only_expense_ledger([_event()])["total_amount"] == 10


def test_range_cache_sees_review_changes(review_root):
    from life_logger.day_views import source_fingerprint

    before = source_fingerprint(review_root, ["2026-10-09"])
    _write(review_root, {"e1": {"action": "exclude"}})
    assert source_fingerprint(review_root, ["2026-10-09"]) != before


def _activity(quote, action="记录", status="已陈述", amount=4, label="self", when="2026-11-02T10:00:00+08:00"):
    return {"activity_id": "a1", "title": "活动", "items": [{
        "action": action, "status": status, "amount": amount, "currency": "CNY", "spoken_at": when,
        "evidence": [{"quote": quote, "source_label": label}],
    }]}


RULES = {"activity_rules_from": "2026-10-26"}


def test_rule_one_counts_self_paid_speech_even_without_completed_status():
    ledger = build_expense_ledger([], activities=[_activity("一桶水，然后花了四块")], **RULES)
    assert ledger["total_amount"] == 4


def test_rule_one_needs_the_speaker_to_be_self():
    ledger = build_expense_ledger([], activities=[_activity("一桶水，花了四块", label="non_self")], **RULES)
    assert ledger["total_amount"] == 0


def test_rule_one_does_not_touch_frozen_history():
    old = _activity("一桶水，然后花了四块", when="2026-10-20T10:00:00+08:00")
    assert build_expense_ledger([], activities=[old], **RULES)["total_amount"] == 0


def test_rule_one_is_off_without_the_cutover():
    assert build_expense_ledger([], activities=[_activity("一桶水，然后花了四块")])["total_amount"] == 0


def test_rule_one_ignores_plans_and_questions():
    for quote, status in (("明天花了再说", "已陈述"), ("买了多少钱", "已陈述"), ("花了四块", "planned"), ("花了四块", "cancelled")):
        ledger = build_expense_ledger([], activities=[_activity(quote, status=status)], **RULES)
        assert ledger["total_amount"] == 0, quote


def test_rule_two_sends_bare_purchase_to_pending():
    item = _activity("买苹果的礼宾卡，七十，他给我减了十块。", status="completed", amount=70)
    ledger = build_expense_ledger([], activities=[item], **RULES)
    assert ledger["total_amount"] == 0
    pending = ledger["pending_transactions"]
    assert pending and pending[0]["reason"] == "activity_purchase_unconfirmed"
    assert pending[0]["proposed_amount"] == 70


def test_rule_two_ignores_offers_that_are_still_in_progress():
    item = _activity("两百块钱买四个。", status="ongoing", amount=200)
    ledger = build_expense_ledger([], activities=[item], **RULES)
    assert ledger["total_amount"] == 0 and not ledger["pending_transactions"]
