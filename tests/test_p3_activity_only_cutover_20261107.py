"""P3: new days with activities admit activity facts and explicit user records only."""
from copy import deepcopy
import json

import pytest

from life_logger import day_facts as facts
from life_logger.expense_policy import build_expense_ledger

DAY = '2026-11-06'


def expense(event_id='evt_auto', day=DAY, **extra):
    return {'event_id': event_id, 'title': '买水花了十元', 'categories': ['expense'],
            'time': {'absolute': f'{day}T12:00:00+08:00'},
            'data': {'expense': {'amount': 10, 'currency': 'CNY', 'item': '水'}},
            'evidence': {'quote': '买水花了十元'}, **extra}


def product(day=DAY, fact_type='none', action='记录今天的生活', **extra):
    return {'activity_id': f'act_{day}', 'status': 'completed', 'items': [{
        'item_id': 'i1', 'action': action, 'fact_type': fact_type, 'status': 'completed',
        'spoken_at': f'{day}T12:00:00+08:00',
        'evidence': [{'quote': action, 'source_id': 'src_1', 'source_label': 'self'}],
        **extra}]}


def put(root, day, activities):
    folder = root / 'activity-timeline/jev'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f'{day}.json').write_text(json.dumps({'activities': activities}))


@pytest.mark.parametrize('marker', [
    {'event_id': 'manual_entry'}, {'event_id': 'nutr_1'}, {'event_id': 'habit_1'}, {'event_id': 'user_1'},
    {'capture_source': 'manual'}, {'source': 'user'}, {'origin': 'quick_input'},
    {'extractor': 'nutrition_statement'}, {'capture_source': 'user_judgment'},
    {'source': 'judgment_queue'}, {'source': 'habit_capture'}, {'origin': 'manual_entry'},
    {'manual': True}, {'is_manual': True},
    {'evidence': {'source_id': 'manual_1'}}, {'evidence': {'source_id': 'nutr_stmt_1'}},
    {'evidence': {'source_id': 'judgment_1'}}, {'evidence': {'extractor': 'manual'}},
    {'evidence': {'extractor': 'user_judgment'}},
    {'data': {'food': {'nutrition_source': 'user_statement'}}},
    {'data': {'diet': {'nutrition_source': 'user_judgment'}}},
    {'data': {'expense': {'estimate_basis': 'user_adopted_candidate'}}},
    {'data': {'food': {'estimate_basis': 'user_weight'}}},
    # Actual repository writers also create these user records.
    {'source_type': 'user_supplement'},
    {'evidence': {'source': 'user_confirmation'}},
    {'review_status': 'confirmed', 'evidence': {'review_source': 'human', 'source_decision': 'self'}},
    {'review_status': 'confirmed', 'correction_status': 'corrected'},
])
def test_every_user_marker_survives_cutover(marker):
    event = expense(**marker)
    assert facts.is_user_or_manual_event(event)
    assert facts.filter_events_for_activity_cutover([event], day=DAY, activities=[product()]) == [event]


@pytest.mark.parametrize('extra', [{}, {'review_status': 'reviewed'}, {'review_status': 'confirmed'},
                                  {'source_type': 'audio'}, {'manual': 'true'},
                                  {'evidence': {'review_source': 'human', 'source_decision': 'self',
                                                'transcript_decision': None}}])
def test_auto_or_unconfirmed_review_is_not_user_entry(extra):
    assert not facts.is_user_or_manual_event(expense(**extra))


def test_auto_expense_and_pending_are_removed_but_manual_survives(tmp_path):
    put(tmp_path, DAY, [product()])
    automatic, manual = expense(), expense('manual_1')
    pending = expense('evt_pending', data={'expense': {'amount': 100}},
                      title='他一百块钱', evidence={'quote': '他一百块钱'})
    result = facts.day_expense_ledger([automatic, pending, manual], DAY, root=tmp_path)
    assert result['total_amount'] == 10
    assert [e['event_id'] for e in result['transactions']] == ['manual_1']
    assert result['pending_transactions'] == []


@pytest.mark.parametrize('fact_type,domain,data', [
    ('food', 'food', {'item': '鸡胸肉', 'weight_g': 30}),
    ('exercise', 'exercise', {'exercises': [{'name': '跑步', 'duration_seconds': 60}]}),
    ('smoke', 'smoking', {'action': 'smoke'}),
])
def test_auto_domain_events_removed_and_activity_survives(fact_type, domain, data):
    event = {'event_id': 'evt_old', 'categories': [domain], 'data': {domain: data}}
    actions = {'food': '吃了三十克鸡胸肉', 'exercise': '跑步一分钟', 'smoke': '抽了三根烟'}
    projected = facts.domain_events([event], [product(fact_type=fact_type, action=actions[fact_type])], DAY, fact_type)
    assert len(projected) == 1
    assert projected[0]['event_id'].startswith('activity:')


@pytest.mark.parametrize('event_id,food', [
    ('manual_food', {'nutrition_source': 'user_judgment', 'estimate_basis': 'user_adopted_candidate'}),
    ('evt_statement', {'nutrition_source': 'user_statement'}),
    ('evt_adopted', {'estimate_basis': 'user_adopted_candidate'}),
    ('evt_weight', {'estimate_basis': 'user_weight'}),
])
def test_user_food_stays_in_domain_projection(event_id, food):
    event = {'event_id': event_id, 'categories': ['food'], 'data': {'food': food}}
    assert facts.domain_events([event], [product()], DAY, 'food') == [event]


@pytest.mark.parametrize('day', ['2026-11-04', '2026-11-05'])
def test_historical_ledgers_are_unchanged(day, tmp_path):
    activities = [product(day)]
    put(tmp_path, day, activities)
    events = [expense(day=day)]
    assert facts.day_expense_ledger(events, day, root=tmp_path) == build_expense_ledger(
        events, activities=activities, activity_rules_from=facts.FACT_CUTOVER_DATE, review={}, confirmed={})
    assert facts.domain_events(events, activities, day, 'food') == events


@pytest.mark.parametrize('activities', [None, []])
def test_absent_or_empty_activities_preserve_old_behavior(activities, tmp_path):
    events = [expense()]
    assert facts.filter_events_for_activity_cutover(events, day=DAY, activities=activities) == events
    assert facts.domain_events(events, activities, DAY, 'food') == events
    if activities == []:
        put(tmp_path, DAY, activities)
    assert facts.day_expense_ledger(events, DAY, root=tmp_path)['total_amount'] == 10


def test_domain_ledgers_also_filters_events(tmp_path):
    old = {'event_id': 'evt_smoke', 'categories': ['smoking'], 'title': '抽了根烟',
           'data': {'smoking': {'action': 'smoke'}}, 'evidence': {'quote': '抽了根烟'}}
    manual = {**old, 'event_id': 'manual_smoke'}
    result = facts.domain_ledgers([old, manual], DAY, activities=[product()], root=tmp_path)
    assert result['smoking']['total_count'] == 1
    assert result['smoking']['sessions'][0]['event_id'] == 'manual_smoke'


def test_range_cutover_checks_each_day_and_missing_product(tmp_path):
    for day in ['2026-11-05', DAY, '2026-11-07']:
        put(tmp_path, day, [product(day)])
    events = [expense(f'evt_{day}', day) for day in ['2026-11-05', DAY, '2026-11-07', '2026-11-08']]
    events.append(expense('manual_keep', '2026-11-07'))
    result = facts.range_expense_ledger(events, '2026-11-05', '2026-11-08', root=tmp_path)
    assert {e['event_id'] for e in result['transactions']} == {'evt_2026-11-05', 'evt_2026-11-08', 'manual_keep'}
    assert result['total_amount'] == 30


def test_filter_uses_shanghai_day_and_does_not_mutate_input():
    early = expense('evt_early', time={'absolute': '2026-11-05T15:59:59Z'})
    late = expense('evt_late', time={'absolute': '2026-11-05T16:00:00Z'})
    reported = expense('evt_reported', time={'absolute': None}, reported_at='2026-11-06T00:00:00+08:00')
    events = [early, late, reported]
    saved = deepcopy(events)
    assert facts.filter_events_for_activity_cutover(events, day=None, activities=[product()]) == [early]
    assert events == saved
    assert facts.filter_events_for_activity_cutover(events, day='2026-11-05', activities=[product()]) == events


def test_food_projection_keeps_structured_weight_and_name():
    cases = [('今天早上吃了400克三全猪肉芹菜速冻水饺', 400, '三全猪肉芹菜速冻水饺'),
             ('称吃了三十克即食鸡胸肉', 30, '即食鸡胸肉')]
    for action, grams, name in cases:
        event = facts.domain_events([], [product(fact_type='food', action=action, amount=grams,
                                               amount_kind='weight', unit='克')], DAY, 'food')[0]
        assert event['data']['food']['weight_g'] == grams
        assert event['data']['food']['item'] == name
