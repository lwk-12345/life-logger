"""v3 wire references, isolated empty activities, and bounded history."""
import copy
import datetime
import json

import pytest

from life_logger import activity_prompt_v3 as v3, fact_schema
from test_activity_v3_shadow import RECORD, CUTOFF, LOOKBACK, v3_body, setup as setup


def wire_response(body):
    return {'choices':[{'message':{'content':json.dumps(body,ensure_ascii=False)}}]}


def wire_body(reference='L001'):
    body=v3_body()
    body['activities'][0]['items'][0]['evidence']=[{'line_id':reference}]
    return body


def test_wire_mapping_uses_actual_verbatim_record_and_preserves_raw_response(setup):
    record={**RECORD,'text':'他花了十四元。\n不是我付款｜原文不能改。'}
    runtime=setup({'version':'v3','mode':'shadow','user_profile':''},lambda _:wire_response(wire_body()))
    report=runtime._run_activity_v3(CUTOFF,LOOKBACK,[record],{'activities':[]},'shadow')
    assert report['status']=='valid'
    assert report['attempts'][0]['body']['activities'][0]['items'][0]['evidence']==[{'line_id':'L001'}]
    evidence=report['result']['activities'][0]['items'][0]['evidence']
    assert evidence==[{'source_id':record['source_id'],'quote':record['text']}]
    assert v3.project_live(report,[record])['activities'][0]['items'][0]['evidence'][0]['quote']==record['text']


@pytest.mark.parametrize('reference',['L000','L002','L999','L1','1',1,True,None])
def test_invalid_or_outside_line_reference_is_rejected(setup,reference):
    runtime=setup({'version':'v3','mode':'shadow','user_profile':''},lambda _:wire_response(wire_body(reference)))
    report=runtime._run_activity_v3(CUTOFF,LOOKBACK,[RECORD],{'activities':[]},'shadow')
    assert report['status']=='invalid'
    assert any(e['error']=='invalid_line_reference' for e in report['validation']['errors'])


def test_wire_cannot_override_original_quote_or_source(setup):
    body=wire_body()
    body['activities'][0]['items'][0]['evidence'][0]['quote']='我付了999元'
    runtime=setup({'version':'v3','mode':'shadow','user_profile':''},lambda _:wire_response(body))
    report=runtime._run_activity_v3(CUTOFF,LOOKBACK,[RECORD],{'activities':[]},'shadow')
    assert report['status']=='invalid'
    assert any(e['error']=='invalid_line_reference' for e in report['validation']['errors'])


def test_prompt_hides_long_ids_and_wire_schema_only_requests_line_reference():
    prompt=v3.build_prompt({'activity_prompt':{'user_profile':''}},CUTOFF,LOOKBACK,[RECORD],[])
    assert RECORD['source_id'] not in prompt
    assert 'L001' in prompt
    schema=fact_schema.request_json_schema(['L001'])
    activity=schema['properties']['activities']['items']
    evidence=activity['properties']['items']['items']['properties']['evidence']
    assert set(evidence['items']['properties'])=={'line_id'}
    assert evidence['items']['properties']['line_id']['enum']==['L001']
    assert evidence['minItems']==fact_schema.MIN_EVIDENCE_ITEMS==1
    assert activity['properties']['items']['minItems']==fact_schema.MIN_ACTIVITY_ITEMS==1


def test_empty_activity_is_retained_with_reason_but_not_published(setup):
    good=v3_body()
    empty={**copy.deepcopy(good['activities'][0]),'activity_id':'empty','items':[]}
    good['activities'].insert(0,empty)
    result=fact_schema.validate_payload(good)
    assert result['ok']
    assert [a['activity_id'] for a in result['confirmed_payload']['activities']]==['v3-a']
    assert result['non_confirmed'][0]['reason']=='no_evidence'
    assert result['non_confirmed'][0]['activity']['items']==[]
    runtime=setup({'activity_prompt':{}},None)
    projected=v3.project_live({'status':'valid','result':good,'confirmed_result':result['confirmed_payload'],'non_confirmed':result['non_confirmed']},[RECORD])
    assert runtime._verify_and_sanitize_v2_response(projected,[RECORD],CUTOFF,{'activities':[]})['activities'][0]['activity_id']=='v3-a'


def test_no_evidence_item_does_not_reject_other_activity(setup):
    body=v3_body()
    bad=copy.deepcopy(body['activities'][0]);bad['activity_id']='empty-evidence'
    bad['items'][0]['evidence']=[]
    body['activities'].append(bad)
    checked=fact_schema.validate_payload(body)
    assert checked['ok']
    assert len(checked['confirmed_payload']['activities'])==1
    assert checked['non_confirmed'][0]['reason']=='no_evidence'
    projected=v3.project_live({'status':'valid','result':body,'confirmed_result':checked['confirmed_payload'],'non_confirmed':checked['non_confirmed']},[RECORD])
    assert not projected.get('unassociated_items')


def test_history_is_bounded_compact_and_keeps_distant_related_id():
    activities=[{'activity_id':f'activity-{i}','title':'其他事情','category':'闲聊·其他','status':'completed','items':[{'evidence':[{'quote':'不可带入历史的大段原话'*500}]}]} for i in range(100)]
    activities[0]['title']='健身房卧推训练'
    selected=v3.select_history(activities,[{**RECORD,'text':'接着记卧推第二组'}],CUTOFF,LOOKBACK)
    assert len(selected)<=v3.HISTORY_MAX_ACTIVITIES
    assert {a['activity_id'] for a in selected}>={'activity-0','activity-99'}
    assert all(set(a)=={'activity_id','title','category','status'} for a in selected)
    assert len(json.dumps(selected,ensure_ascii=False))<=v3.HISTORY_MAX_CHARS
    prompt=v3.build_prompt({'activity_prompt':{'user_profile':''}},CUTOFF,LOOKBACK,[RECORD],activities)
    assert '不可带入历史的大段原话' not in prompt
    assert len(prompt)<25000


def test_nearby_history_and_full_selected_ids_survive():
    lo=LOOKBACK-datetime.timedelta(minutes=10)
    activities=[{'activity_id':'a-very-important-complete-id','title':'旧事','status':'ongoing','category':'工作·项目','reported_at':lo.isoformat()}]
    selected=v3.select_history(activities,[RECORD],CUTOFF,LOOKBACK)
    assert selected[0]['activity_id']=='a-very-important-complete-id'


def test_multiple_references_keep_input_order_and_do_not_change_source_rows():
    rows=[copy.deepcopy(RECORD),{**RECORD,'source_id':'second-source','text':'第二句，完整。'}]
    before=copy.deepcopy(rows)
    body=wire_body()
    body['activities'][0]['items'][0]['evidence']=[{'line_id':'L002'},{'line_id':'L001'}]
    normalized,errors=v3.resolve_references(body,rows)
    assert not errors and rows==before
    assert normalized['activities'][0]['items'][0]['evidence']==[
        {'source_id':'second-source','quote':'第二句，完整。'},
        {'source_id':RECORD['source_id'],'quote':RECORD['text']},
    ]
    assert body['activities'][0]['items'][0]['evidence'][0]=={'line_id':'L002'}


def test_history_count_and_character_budgets_hold_without_shortening_ids():
    activities=[{'activity_id':f'{i}-'+('a'*900),'title':'相同主题'*100,'category':'工作·项目','status':'ongoing'} for i in range(100)]
    selected=v3.select_history(activities,[{**RECORD,'text':'继续相同主题'}],CUTOFF,LOOKBACK)
    assert 0<len(selected)<=v3.HISTORY_MAX_ACTIVITIES
    assert len(json.dumps(selected,ensure_ascii=False))<=v3.HISTORY_MAX_CHARS
    assert all(a['activity_id'] in {old['activity_id'] for old in activities} for a in selected)
    small=[{**a,'activity_id':str(i)} for i,a in enumerate(activities)]
    assert len(v3.select_history(small,[{**RECORD,'text':'继续相同主题'}],CUTOFF,LOOKBACK))==v3.HISTORY_MAX_ACTIVITIES


def test_future_revisions_are_not_history_candidates():
    future={'activity_id':'future','title':'同一主题','status':'ongoing','category':'工作·项目',
            'provenance':{'last_updated_at_cutoff':(CUTOFF+datetime.timedelta(minutes=1)).isoformat()}}
    assert v3.select_history([future],[{**RECORD,'text':'同一主题'}],CUTOFF,LOOKBACK)==[]
