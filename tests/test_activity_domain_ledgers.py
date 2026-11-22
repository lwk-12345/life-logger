from copy import deepcopy
from datetime import date, timedelta

import pytest

from life_logger import day_facts
from life_logger import daily_cognitive_session as dcs

DAY = '2026-11-04'
BUILDERS = {'food': dcs.build_nutrition_ledger, 'exercise': dcs.build_fitness_ledger, 'smoke': dcs.build_smoking_ledger}
ACTIONS = {'food': '吃完饭', 'exercise': '做了深蹲', 'smoke': '抽了烟'}
CATEGORIES = {'food': 'food', 'exercise': 'exercise', 'smoke': 'smoking'}


def activity(kind, **changes):
    item = {'item_id': 'i1', 'action': ACTIONS[kind], 'fact_type': kind, 'status': 'completed',
            'spoken_at': f'{DAY}T20:00:00+08:00', 'occurred_at': None,
            'evidence': [{'source_id': 'src-one', 'quote': f'我{ACTIONS[kind]}了', 'source_label': 'self'}]}
    item.update(changes)
    return {'activity_id': 'a1', 'lifecycle': 'live', 'items': [item]}


def count(kind, ledger):
    # exercise 用 workouts（逐条投影）验证去重；workout_count 已改为按会话计。
    if kind == 'exercise':
        return len(ledger['workouts'])
    return ledger[{'food': 'consumed_record_count'}.get(kind, 'total_count')]


@pytest.mark.parametrize('kind', BUILDERS)
def test_activity_fact_enters_correct_ledger_without_inventing_metrics(kind):
    ledger = BUILDERS[kind]([], activities=[activity(kind)], day=DAY)
    assert count(kind, ledger) == 1
    records = ledger[{'food': 'food_records', 'exercise': 'workouts', 'smoke': 'sessions'}[kind]]
    assert records[0]['origin'] == 'activity'
    assert records[0]['activity_id'] == 'a1'
    assert records[0]['occurred_at'] is None
    if kind == 'food':
        assert records[0]['calories_kcal'] is None
        assert records[0]['weight_g'] is None
    elif kind == 'exercise':
        assert not records[0]['exercises'][0].get('sets')
    else:
        assert records[0]['brand'] is None
        assert records[0]['count'] is None


def test_unknown_meal_slot_is_not_invented_as_a_snack():
    ledger=dcs.build_nutrition_ledger([],activities=[activity('food')],day=DAY)
    assert ledger['meal_counts']=={'unknown':1}
    assert ledger['meals']['snacks']==[]


def test_undated_workout_is_not_invented_as_happening_today():
    ledger=dcs.build_fitness_ledger([],activities=[activity('exercise')],day=DAY)
    assert ledger['workouts'][0]['day'] is None


@pytest.mark.parametrize('kind', BUILDERS)
@pytest.mark.parametrize('case', ['media', 'item_media', 'unknown_lifecycle', 'unknown_item', 'other', 'unknown_source', 'none', 'missing_type', 'unknown_status', 'plan', 'question'])
def test_media_plans_and_uncertain_items_do_not_enter(kind, case):
    a=activity(kind); item=a['items'][0]
    if case=='media': a['lifecycle']='media'
    if case=='item_media': item['item_lifecycle']='media'
    if case=='unknown_lifecycle': a['lifecycle']='unknown'
    if case=='unknown_item': item['item_lifecycle']='unknown'
    if case=='other': item['evidence'][0]['source_label']='non_self'
    if case=='unknown_source': item['evidence'][0]['source_label']='unknown'
    if case=='none': item['fact_type']='none'
    if case=='missing_type': item.pop('fact_type')
    if case=='unknown_status': item['status']='unknown'
    if case=='plan': item['status']='planned'
    if case=='question': item['action']='询问是否可以'+ACTIONS[kind]
    assert count(kind, BUILDERS[kind]([], activities=[a], day=DAY)) == 0


@pytest.mark.parametrize('kind', BUILDERS)
def test_before_cutover_uses_original_events_only(kind):
    old_day=(date.fromisoformat(day_facts.FACT_CUTOVER_DATE)-timedelta(days=1)).isoformat()
    events=[{'event_id':'old','title':ACTIONS[kind], 'categories':[CATEGORIES[kind]],
             'evidence':{'quote':'旧原句'}, 'data':{CATEGORIES[kind]:{}}}]
    after = BUILDERS[kind](events, activities=[activity(kind)], day=old_day)
    before = BUILDERS[kind](events)
    if kind == 'exercise':
        # 会话对象比纯事件多出 session 元数据；这里只验证事实计数未变。
        assert count(kind, after) == count(kind, before)
    else:
        assert after == before
    assert count(kind, BUILDERS[kind]([], activities=[activity(kind)], day=day_facts.FACT_CUTOVER_DATE)) == 1


@pytest.mark.parametrize('kind', BUILDERS)
@pytest.mark.parametrize('identity', ['source', 'event_ref'])
def test_same_fact_in_legacy_event_and_activity_is_not_double_counted(kind, identity):
    a=activity(kind); i=a['items'][0]
    evidence={'quote':i['evidence'][0]['quote'], 'source_id':'src-one'}
    if identity=='event_ref':
        i['source_event_id']='old'; evidence['source_id']='different'
    events=[{'event_id':'old', 'title':ACTIONS[kind], 'categories':[CATEGORIES[kind]],
             'evidence':evidence, 'data':{CATEGORIES[kind]:{}}}]
    before=BUILDERS[kind](events)
    after=BUILDERS[kind](events, activities=[a], day=DAY)
    if kind == 'exercise':
        assert count(kind, after) == count(kind, before)
    else:
        assert after == before


@pytest.mark.parametrize('kind', BUILDERS)
def test_duplicate_projection_is_suppressed_but_distinct_source_is_kept(kind):
    first=activity(kind)
    other=deepcopy(first);other['activity_id']='a2';other['items'][0]['item_id']='i2'
    # Same text does not prove the same occurrence.
    other['items'][0]['evidence'][0]['source_id']='src-two'
    assert count(kind,BUILDERS[kind]([],activities=[first,deepcopy(first),other],day=DAY))==2


@pytest.mark.parametrize('case,expected', [('same_utterance',1),('different_second',2),('unknown_time',2),('short_quote',2),('precise_mismatch',2),('ambiguous',3)])
def test_legacy_and_activity_source_namespaces_use_unique_verbatim_speech_anchor(case,expected):
    a=activity('food'); i=a['items'][0]
    quote='我刚刚已经吃完这份晚饭了'
    if case=='short_quote': quote='吃了'
    i['evidence'][0].update(source_id='watch-source',quote=quote)
    i['spoken_at']=f'{DAY}T20:00:00+08:00'
    ev={'event_id':'legacy','title':'吃完晚饭','categories':['food'],
        'evidence':{'source_id':'formal-source','source_label':'self','quote':quote,'reported_at':f'{DAY}T12:00:00.567+00:00'}}
    if case=='different_second': i['spoken_at']=f'{DAY}T20:00:01+08:00'
    if case=='unknown_time': ev['evidence'].pop('reported_at')
    if case=='precise_mismatch': i['spoken_at']=f'{DAY}T20:00:00.123+08:00'
    events=[ev]
    if case=='ambiguous': events.append({**deepcopy(ev),'event_id':'legacy-2'})
    ledger=dcs.build_nutrition_ledger(events,activities=[a],day=DAY)
    assert ledger['consumed_record_count']==expected


def test_different_actions_in_same_utterance_are_not_merged():
    a=activity('exercise'); second=deepcopy(a['items'][0]);second.update(item_id='i2',action='练了肩')
    a['items'].append(second)
    ledger=dcs.build_fitness_ledger([],activities=[a],day=DAY)
    assert len(ledger['workouts'])==2 and ledger['workout_count']==1


def test_smoking_purchase_is_a_purchase_not_a_smoked_cigarette():
    a=activity('smoke',action='买了一包烟')
    ledger=dcs.build_smoking_ledger([],activities=[a],day=DAY)
    assert len(ledger['sessions'])==1
    assert ledger['sessions'][0]['action']=='purchase'
    assert ledger['total_count']==0
    planned=activity('smoke',action='准备买烟',status='planned')
    assert dcs.build_smoking_ledger([],activities=[planned],day=DAY)['sessions']==[]


def test_day_entry_reads_activity_file_only_since_cutover(monkeypatch):
    calls=[]
    def read(day):
        calls.append(day); return [activity('food')]
    monkeypatch.setattr(day_facts,'activities_for_day',read)
    old_day=(date.fromisoformat(day_facts.FACT_CUTOVER_DATE)-timedelta(days=1)).isoformat()
    assert day_facts.domain_ledgers([],old_day)['nutrition']['record_count']==0
    assert calls==[]
    assert day_facts.domain_ledgers([],DAY)['nutrition']['record_count']==1
    assert calls==[DAY]


@pytest.mark.parametrize('fails', [False, True])
def test_synthesis_keeps_activity_domain_facts_with_unrelated_legacy_events(tmp_path,monkeypatch,fails):
    monkeypatch.setattr(dcs,'_get_deepseek_settings',lambda:('http://fake.invalid','k','m',None,None))
    def model(*args,**kwargs):
        if fails: raise RuntimeError('unavailable')
        return {'executive_summary':'测试'},[]
    monkeypatch.setattr(dcs,'_call_model',model)
    monkeypatch.setattr(dcs,'_pause_on_model_failure',lambda:False)
    monkeypatch.setattr(day_facts,'load_ledger_review',lambda:{})
    monkeypatch.setattr(day_facts,'load_ledger_confirmed',lambda:{})
    monkeypatch.setattr(day_facts,'activities_for_day',lambda day: [])  # money input unchanged
    result=dcs.generate_daily_cognitive_synthesis(DAY,[{'event_id':'work','title':'工作记录','categories':['work']}],
            activities=[activity('food'),activity('exercise'),activity('smoke')],force=True,cache_dir=tmp_path/'s')
    assert result['nutrition_ledger']['consumed_record_count']==1
    assert result['fitness_ledger']['workout_count']==1
    assert len(result['smoking_ledger']['sessions'])==1
    assert result['event_count']==1
    assert result['expense_ledger']['total_amount']==0


def test_activity_fact_change_invalidates_synthesis_cache(tmp_path,monkeypatch):
    monkeypatch.setattr(dcs,'_get_deepseek_settings',lambda:('http://fake.invalid','k','m',None,None))
    calls=[]
    def model(*args,**kwargs): calls.append(1);return {'executive_summary':'测试'},[]
    monkeypatch.setattr(dcs,'_call_model',model)
    monkeypatch.setattr(day_facts,'activities_for_day',lambda day: [])
    monkeypatch.setattr(day_facts,'load_ledger_review',lambda:{})
    monkeypatch.setattr(day_facts,'load_ledger_confirmed',lambda:{})
    a=activity('exercise')
    first=dcs.generate_daily_cognitive_synthesis(DAY,[],activities=[a],cache_dir=tmp_path/'s')
    again=dcs.generate_daily_cognitive_synthesis(DAY,[],activities=[a],cache_dir=tmp_path/'s')
    assert again['cache_state']=='hit'
    b=deepcopy(a);b['activity_id']='a2';b['items'][0]['item_id']='i2';b['items'][0]['evidence'][0]['source_id']='src-two'
    changed=dcs.generate_daily_cognitive_synthesis(DAY,[],activities=[a,b],cache_dir=tmp_path/'s')
    assert changed['fitness_ledger']['workout_count']==2
    assert first['fitness_ledger']['workout_count']==1
    assert len(calls)==2
