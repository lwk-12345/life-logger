from __future__ import annotations

from copy import deepcopy
from life_logger.daily_cognitive_session import build_expense_ledger
from life_logger.statistics import build_range_statistics
import json


def _purchase(identifier, timestamp, item='冰水', amount=2):
    return {'event_id': identifier, 'title': '买了冰水', 'categories': ['expense'],
            'time': {'absolute': timestamp},
            'evidence': {'quote': '买了冰水两块钱', 'reported_at': timestamp,
                         'event_state': 'completed', 'claim_type': 'event'},
            'data': {'expense': {'item': item, 'amount': amount, 'currency': 'CNY'}}}


def test_real_repeated_purchases_within_one_minute_are_not_deduplicated():
    events = [_purchase('first', '2026-10-14T10:00:05+08:00'),
              _purchase('second', '2026-10-14T10:00:45+08:00')]
    ledger = build_expense_ledger(events)
    assert ledger['total_amount'] == 4
    assert ledger['payment_transaction_count'] == 2


def test_equal_price_distinct_items_from_one_utterance_stay_distinct():
    first = _purchase('first', '2026-10-14T10:00:05+08:00', '冰水')
    first['evidence']['quote'] = '买了冰水和茶，各两块钱'
    second = deepcopy(first)
    second['event_id'] = 'second'
    second['data']['expense']['item'] = '茶'
    ledger = build_expense_ledger([first, second])
    assert ledger['total_amount'] == 4
    assert ledger['payment_transaction_count'] == 2


def test_statistics_does_not_readd_withdrawals_from_ledger_transactions(tmp_path):
    purchase = _purchase('purchase', '2026-10-14T10:00:05+08:00')
    withdrawal = _purchase('withdrawal', '2026-10-14T11:00:05+08:00', '提现', 10)
    withdrawal['title'] = '提现十元'
    withdrawal['evidence']['quote'] = '提现十元'
    path = tmp_path / 'jobs/job-day/files/hash/result/2026-10-14/daily_summary.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'timeline': [purchase, withdrawal]}))
    stats = build_range_statistics(tmp_path, date_from='2026-10-14', date_to='2026-10-14')
    assert stats['expense_total_cny'] == 2
    assert stats['daily_expenses'] == [{'date': '2026-10-14', 'amount': 2}]
