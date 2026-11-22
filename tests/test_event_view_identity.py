"""Adversarial supervisor checks beyond the delegated happy paths."""
from __future__ import annotations

import json
from pathlib import Path

from life_logger.event_view import build_event_view


def _put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False))


def _event(identifier, item, amount):
    return {'event_id': identifier, 'title': item, 'categories': ['expense'],
            'time': {'absolute': '2026-10-14T23:28:26+08:00'},
            'evidence': {'quote': '买了桂圆二十四元，柠檬茶两块六',
                         'reported_at': '2026-10-14T23:28:26+08:00',
                         'event_state': 'completed', 'claim_type': 'event'},
            'data': {'expense': {'item': item, 'amount': amount, 'currency': 'CNY'}}}


def test_multi_purchase_utterance_has_one_to_one_live_formal_aliases(tmp_path: Path):
    formal = [_event('formal-a', '桂圆', 24), _event('formal-b', '柠檬茶', 2.6)]
    live = [_event('live-a', '桂圆', 24), _event('live-b', '柠檬茶', 2.6)]
    _put(tmp_path / 'jobs/job-one/files/hash/result/2026-10-14/daily_summary.json', {'timeline': formal})
    _put(tmp_path / 'watch-sessions/dev/session/events.json', live)
    _put(tmp_path / 'watch-sessions/dev/session/manifest.json', {'completion_job_id': 'job-one', 'segments': []})
    result = build_event_view(tmp_path, date_from='2026-10-14', date_to='2026-10-14')
    assert len(result['events']) == 2
    for event in result['events']:
        assert len(event['aliases']) == 1
        alias = event['aliases'][0]['event']
        assert alias['data']['expense']['item'] == event['data']['expense']['item']


def test_canonical_key_survives_formal_arrival(tmp_path: Path):
    live = _event('watch-internal-id', '桂圆', 24)
    _put(tmp_path / 'watch-sessions/dev/session/events.json', [live])
    _put(tmp_path / 'watch-sessions/dev/session/manifest.json', {'completion_job_id': 'job-one', 'segments': []})
    earlier = build_event_view(tmp_path, date_from='2026-10-14', date_to='2026-10-14')['events'][0]
    formal = dict(live, event_id='formal-internal-id')
    _put(tmp_path / 'jobs/job-one/files/hash/result/2026-10-14/daily_summary.json', {'timeline': [formal]})
    later = build_event_view(tmp_path, date_from='2026-10-14', date_to='2026-10-14')['events'][0]
    assert earlier['event_key'] == later['event_key']
    assert later['event_id'] == 'formal-internal-id'
    assert {p['event_id'] for p in later['provenance']} == {'watch-internal-id', 'formal-internal-id'}
