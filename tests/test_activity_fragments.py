import copy
import json

import pytest

from life_logger import activity_fragments as fragments

DAY = '2026-11-05'
T = DAY + 'T00:26:10+08:00'


def row(sid='s1', text='涨工资', score=.49, speaker='说话人一', seconds=10, scope='chunk-1'):
    return {'source_id':sid,'text':text,'voice_similarity':score,'speaker':speaker,
            'timestamp':DAY+f'T00:26:{seconds:02d}+08:00','scope':scope}


def activity(text='涨工资'):
    return {'activity_id':'salary','title':'重复提及涨工资',
            'summary':'没有证据表明已到账或形成消费','lifecycle':'live',
            'items':[{'item_id':'i1','amount':None,'fact_type':'none',
                      'evidence':[{'source_id':'s1','quote':text,'source_label':'self'}]}]}


def test_real_salary_regression_and_explicit_controls():
    # Frozen real regression facts, not a dependency on ignored work artifacts.
    salary=activity();salary['activity_id']='act_20261105_002610_salary_raise_mention'
    salary['items'][0]['evidence']=[{'source_id':'src_3ff77f13ff2d4e6d','quote':'涨工资','source_label':'self'},
                                   {'source_id':'src_3c9977070167523c','quote':'涨工资','source_label':'self'}]
    rows=[row(sid='src_3ff77f13ff2d4e6d',score=.492676),row(sid='src_3c9977070167523c',score=.492676,seconds=15)]
    before=copy.deepcopy(salary)
    hints,_=fragments.assess(salary,rows)
    assert hints['looks_like_fragment'] is True and hints['fragment_money_protected'] is False
    assert '不确定是谁说的' in hints['fragment_reason'] and salary==before
    for text,similarity in [('别忘了LiveLog',.639626),('走回去走还是六十吧',.786671)]:
        assert fragments.assess(activity(text),[row(text=text,score=similarity)])[0]['looks_like_fragment'] is False


@pytest.mark.parametrize('value,expected',[(.59999,True),(.6,False),(.9,False),(None,True),(float('nan'),True),(True,True)])
def test_similarity_boundary_and_unknown(value,expected):
    assert fragments.assess(activity(),[row(score=value)])[0]['looks_like_fragment'] is expected


@pytest.mark.parametrize('text,length',[('嗯，好的！',3),('别忘了LiveLog',10),("I'm",2),('一 二，三。四！五？六',6)])
def test_punctuation_removed_but_latin_letters_retained(text,length):
    assert len(fragments.compact(text))==length


def test_distinct_sentence_in_same_chunk_is_context_including_same_activity():
    a=activity('写上我的。');a['items'][0]['evidence'].append({'source_id':'s2','quote':'第二组。'})
    rows=[row(text='写上我的。',score=.560938),row(sid='s2',text='第二组。',score=.560938,seconds=25)]
    assert fragments.assess(a,rows)[1]=='same_speaker_context'
    assert fragments.assess(activity(),[row(),row(sid='s2',text='明天再说',seconds=59)])[1]=='same_speaker_context'


@pytest.mark.parametrize('change',[{'speaker':'说话人二'},{'scope':'chunk-2'},{'timestamp':DAY+'T00:27:11+08:00'}])
def test_other_speaker_chunk_or_outside_sixty_seconds_is_not_context(change):
    neighbor={**row(sid='s2',text='明天再说'),**change}
    assert fragments.assess(activity(),[row(),neighbor])[0]['looks_like_fragment'] is True


def test_exact_sixty_second_neighbor_counts_as_context():
    neighbor={**row(sid='s2',text='明天再说'),'timestamp':DAY+'T00:27:10+08:00'}
    assert fragments.assess(activity(),[row(),neighbor])[1]=='same_speaker_context'


@pytest.mark.parametrize('text',['买水','多少钱','十块','付五元','订阅','扫码','退款'])
def test_consumption_quotes_are_never_fragmented(text):
    a=activity(text)
    flags,reason=fragments.assess(a,[row(text=text)])
    assert not flags['looks_like_fragment'] and flags['fragment_money_protected'] and reason=='spend_protected'


def test_title_ordering_and_structured_money_are_protected_but_not_summary_negation():
    a=activity('来一个红烧。');a['title']='口头点“一个红烧”'
    assert fragments.assess(a,[row(text='来一个红烧。')])[1]=='spend_protected'
    for fields in [{'amount':4},{'fact_type':'expense'}]:
        a=activity();a['items'][0].update(fields)
        assert fragments.assess(a,[row()])[1]=='spend_protected'
    a=activity();a['items'][0].update(amount=20,amount_kind='weight',unit='kg')
    assert fragments.assess(a,[row()])[0]['looks_like_fragment']


def test_short_quote_does_not_discard_its_long_original_sentence_context():
    assert fragments.assess(activity(),[row(text='我说我想涨工资')])[1]=='long_source_sentence_context'
    assert fragments.assess(activity(),[row(),row(sid='s2',text='……！')])[0]['looks_like_fragment'] is True


def test_unresolved_source_empty_quote_and_long_evidence_are_not_claimed_isolated():
    assert fragments.assess(activity(),[])[1]=='source_context_unavailable'
    a=activity();a['items'][0]['evidence'].append({'source_id':'s2','quote':'这是一句更长的话'})
    assert fragments.assess(a,[row()])[1]=='not_all_short'
    assert fragments.assess(activity(''),[row()])[1]=='not_all_short'


def test_read_projection_only_source_file_and_other_fields_unchanged(tmp_path,monkeypatch):
    from life_logger.activity_timeline_runtime import ActivityTimelineRuntime,SCHEMA
    path=tmp_path/'activity-timeline/jev'/f'{DAY}.json';path.parent.mkdir(parents=True)
    payload={'schema':SCHEMA,'date':DAY,'activities':[activity()],'coverage':{},'metadata':{},'status':'ready'}
    path.write_text(json.dumps(payload));before=path.read_bytes()
    monkeypatch.setattr(fragments,'load_day_rows',lambda *_:([row()],0))
    runtime=ActivityTimelineRuntime(tmp_path,tmp_path/'absent-config.json')
    projected=fragments.project_for_read(runtime.read_day(DAY),tmp_path,DAY)
    assert projected['activities'][0]['looks_like_fragment'] is True
    assert projected['activities'][0]['items'][0]['evidence'][0]['source_label']=='self'
    assert projected['activities'][0]['items'][0]['amount'] is None
    assert 'looks_like_fragment' not in runtime.read_day(DAY,for_display=False)['activities'][0]
    assert getattr(projected,'display_projection',False) is True
    assert path.read_bytes()==before


def test_http_read_applies_hints_without_model_or_source_writes(tmp_path,monkeypatch):
    import threading
    import urllib.request
    from life_logger.local_web import JobStore,create_server
    from life_logger.activity_timeline_runtime import SCHEMA
    from life_logger import activity_digest
    path=tmp_path/'activity-timeline/jev'/f'{DAY}.json';path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'schema':SCHEMA,'date':DAY,'activities':[activity()],'status':'ready'}))
    before=path.read_bytes()
    monkeypatch.setattr(fragments,'load_day_rows',lambda *_:([row()],0))
    monkeypatch.setattr(activity_digest,'call_model',lambda _:pytest.fail('read must never call model'))
    server=create_server(host='127.0.0.1',port=0,store=JobStore(tmp_path,recover=False),access_token='fragment-unit-only')
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        request=urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/activity-timeline?date='+DAY,headers={'Authorization':'Bearer fragment-unit-only'})
        with urllib.request.urlopen(request,timeout=3) as response:payload=json.load(response)
        assert payload['activities'][0]['looks_like_fragment'] is True
        assert payload['fragment_assessment']['source_rows']==1 and path.read_bytes()==before
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)


def test_raw_loader_uses_declared_offsets_is_read_only_and_reuses_cache(tmp_path,monkeypatch):
    session=tmp_path/'watch-sessions/recording/session'
    path=session/'formal-asr/segments/000001/state.json';path.parent.mkdir(parents=True)
    # Nontrivial declared offset: sequence*60 would incorrectly skip this source.
    (session/'manifest.json').write_text(json.dumps({'started_at':'2026-11-02T00:00:00+08:00','segments':[{'sequence':1,'start_offset_ms':259200000}]}))
    raw={'status':'completed','rows':[{'text':'涨工资','recording_id':'recording','segment_id':'s1','absolute_started_at':T,'voice_similarity':.492676,'speaker':'说话人一'}]}
    path.write_text(json.dumps(raw));before=path.read_bytes()
    rows,errors=fragments.load_day_rows(tmp_path,DAY)
    assert not errors and len(rows)==1 and rows[0]['voice_similarity']==.492676
    monkeypatch.setattr(fragments,'read_json',lambda _:pytest.fail('warm cache must not reread files'))
    assert fragments.load_day_rows(tmp_path,DAY)==(rows,errors)
    assert path.read_bytes()==before and not list(tmp_path.rglob('.scan-cursor.json'))


def test_tagging_does_not_mutate_input():
    payload={'activities':[activity()]};before=copy.deepcopy(payload)
    result=fragments.tag_payload(payload,[row()])
    assert result['activities'][0]['looks_like_fragment'] is True and payload==before


@pytest.mark.parametrize('fact_type,quote,title',[
    ('smoke','现在抽一根烟','抽一根烟'),
    ('food','吃个苹果','吃苹果'),
    ('exercise','深蹲五组','做深蹲'),
])
def test_self_smoke_food_exercise_facts_are_protected_from_fragment(fact_type,quote,title):
    a=activity(quote)
    a['title']=title
    a['items'][0]['fact_type']=fact_type
    flags,reason=fragments.assess(a,[row(text=quote,score=.314738)])
    assert flags['looks_like_fragment'] is False
    assert flags['fragment_money_protected'] is True
    assert reason=='spend_protected'


def test_wechat_speech_is_protected_from_fragment():
    a=activity('好的马上回')
    a['title']='回复微信消息'
    a['speech_target']='微信'
    flags,reason=fragments.assess(a,[row(text='好的马上回',score=.35)])
    assert flags['looks_like_fragment'] is False
    assert flags['fragment_money_protected'] is True
    assert reason=='spend_protected'

