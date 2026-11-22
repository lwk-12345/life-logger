from copy import deepcopy
from datetime import datetime, timedelta
import json
import threading
import urllib.error
import urllib.request

import pytest

from life_logger import activity_digest as digest

def classified(batch,category='工作·项目',media=False,reason='测试原话'):
    return {'categories':[{'activity_id':a['activity_id'],'category_model':category,'looks_like_media':media,'reason':reason} for a in batch]}


@pytest.fixture(autouse=True)
def isolate_classifier_http(monkeypatch):
    # Card tests inject model_call; the newly independent classifier must also
    # stay offline. Classification-specific tests inject category_call below.
    original = digest.call_categories
    monkeypatch.setattr(digest,'call_categories',classified)
    return original


DAY = "2026-11-05"
NOW = datetime.fromisoformat(DAY + "T21:00:00+08:00")


def activities():
    return [
        {"activity_id": "a1", "lifecycle": "live", "spoken_at": DAY+"T19:00:00+08:00",
         "spoken_end": DAY+"T19:01:00+08:00", "title": "现场训练",
         "items": [{"item_id": "i1", "fact_type": "exercise", "status": "completed",
                    "spoken_at": DAY+"T19:00:00+08:00", "occurred_at": None,
                    "evidence": [{"quote": "平板卧推做了三组，每组十二次，单边12.5千克", "source_label": "self"}]}]},
        {"activity_id": "a2", "lifecycle": "live", "spoken_at": DAY+"T19:20:00+08:00",
         "title": "哑铃推肩", "items": [{"item_id": "i2", "fact_type": "exercise", "status": "completed",
                    "spoken_at": DAY+"T19:20:00+08:00", "occurred_at": None,
                    "evidence": [{"quote": "哑铃推肩，又做了几组", "source_label": "self"}]}]},
    ]


def response():
    return {"cards": [{"kind": "fitness", "title": "胸肩训练", "summary": "卧推与推肩记录",
                       "activity_ids": ["a1", "a2"], "details": [
                           {"name": "平板卧推", "sets": 3, "reps": 12, "weight": "12.5千克", "note": "", "source_item_ids": ["a1::i1"]},
                           {"name": "哑铃推肩", "sets": None, "reps": None, "weight": None, "note": "", "source_item_ids": ["a2::i2"]}],
                       "uncertain_notes": []}]}


def source(root, data=None):
    path = root/"activity-timeline"/"jev"/(DAY+".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"activities": data if data is not None else activities()}, ensure_ascii=False))
    return path


def validate(raw=None, data=None):
    return digest.validate_cards(raw if raw is not None else response(), digest.prepare(data if data is not None else activities(), DAY))


def test_merges_with_grounded_details_and_spoken_span():
    data=activities();before=deepcopy(data)
    cards, errors=validate(data=data)
    assert not errors and len(cards)==1 and data==before
    card=cards[0]
    assert card['activity_ids']==['a1','a2']
    assert card['duration_minutes']==20 and card['time_basis']=='spoken'
    bench,shoulder=card['details']
    assert (bench['sets'],bench['reps'],bench['weight'])==(3,12,'单边12.5千克')
    assert bench['source_item_ids']==['i1'] and bench['source_item_refs']==['a1::i1']
    assert shoulder['sets'] is None and shoulder['note']=='几组'
    assert '不代表实际' in ' '.join(card['uncertain_notes'])


def test_explicit_no_merge_and_singleton_rejection():
    assert validate({'cards':[]})==([],[])
    raw=response();raw['cards'][0]['activity_ids']=['a1']
    cards,errors=validate(raw)
    assert not cards and errors[0]['reason']=='requires_two_activities'


@pytest.mark.parametrize('mutation', ['missing_activity','duplicate_activity','missing_item','foreign_item','bad_kind','no_details','media','item_media','wechat'])
def test_invalid_cards_are_discarded_without_crashing(mutation):
    raw=response();data=activities();card=raw['cards'][0]
    if mutation=='missing_activity':card['activity_ids'][1]='invented'
    elif mutation=='duplicate_activity':card['activity_ids']=['a1','a1']
    elif mutation=='missing_item':card['details'][0]['source_item_ids']=[]
    elif mutation=='foreign_item':card['details'][0]['source_item_ids']=['a2::i1']
    elif mutation=='bad_kind':card['kind']='invented'
    elif mutation=='no_details':card['details']=[]
    elif mutation=='media':data[1]['lifecycle']='media'
    elif mutation=='item_media':data[1]['items'][0]['item_lifecycle']='media'
    else:
        data[1]['items'][0]['fact_type']='none'
        data[1]['items'][0]['status']='unknown'
        data[1]['items'][0]['evidence'][0]['quote']='查看微信聊天记录，健身舞是否运行成功？'
    cards,errors=validate(raw,data)
    assert cards==[] and len(errors)==1


def test_bad_card_does_not_drop_other_valid_cards():
    raw=response();raw['cards'].insert(0,{'kind':'invented'})
    cards,errors=validate(raw)
    assert len(cards)==1 and len(errors)==1


def test_missing_numbers_null_not_filled_from_other_units_or_model_prose():
    raw=response();detail=raw['cards'][0]['details'][0]
    detail.update(sets=12,reps=3,weight='45千克',note='总共完成四十五千克')
    raw['cards'][0].update(title='完成十二组训练',summary='总重45千克')
    cards,errors=validate(raw)
    assert not errors
    card=cards[0];detail=card['details'][0]
    assert detail['sets'] is detail['reps'] is detail['weight'] is None
    assert detail['note']==''
    assert '十二组' not in card['title'] and '45' not in card['summary']


def test_ordinal_not_count_and_chinese_decimal_retains_single_side_addition():
    data=activities();data[0]['items'][0]['evidence'][0]['quote']='第三组，一边加二点五千克'
    raw=response();raw['cards'][0]['details'][0].update(sets=3,reps=None,weight='2.5千克')
    card=validate(raw,data)[0][0];detail=card['details'][0]
    assert detail['sets'] is None
    assert detail['weight']=='一边加二点五千克'
    assert '第三组' in detail['note']


@pytest.mark.parametrize('weight', ['20千克','5kg','2.5公斤'])
def test_weight_does_not_convert_jin_or_derive_totals(weight):
    assert digest.supported_weight(weight,['单边五斤']) is None


def test_raw_numeric_weight_keeps_units_and_bar_qualifier():
    assert digest.supported_weight(20,['钢重是20千克'])=='钢重是20千克'
    assert digest.supported_weight('12.5千克；20千克',['单边12.5千克','钢重是20千克'])=='单边12.5千克；钢重是20千克'


def test_ambiguous_unqualified_item_id_is_rejected():
    data=activities();data[1]['items'][0]['item_id']='i1'
    raw=response();raw['cards'][0]['details'][0]['source_item_ids']=['i1']
    assert validate(raw,data)[0]==[]


def test_duration_is_calculated_and_occurrence_only_when_all_references_known():
    data=activities()
    for a in data:
        a['items'][0]['occurred_at']=a['spoken_at'].replace('19:','18:')
    raw=response();raw['cards'][0].update(duration_minutes=999,start='2099-01-01',end='2099-12-31')
    card=validate(raw,data)[0][0]
    assert card['time_basis']=='occurred' and card['duration_minutes']==20
    assert card['start']==DAY+'T18:00:00+08:00'


def test_missing_time_stays_unknown():
    data=activities()
    for a in data:
        a.pop('spoken_at',None);a.pop('spoken_end',None)
        a['items'][0]['spoken_at']=None
    card=validate(data=data)[0][0]
    assert card['time_basis']=='unknown'
    assert card['start'] is card['end'] is card['duration_minutes'] is None


def test_cache_hit_never_calls_model_again_or_changes_source(tmp_path):
    path=source(tmp_path);before=path.read_bytes();calls=[]
    def model(data):calls.append(data);return response()
    first=digest.generate_digest(tmp_path,DAY,now=NOW,model_call=model)
    second=digest.generate_digest(tmp_path,DAY,now=NOW+timedelta(hours=1),model_call=model)
    assert first==second and len(calls)==1 and path.read_bytes()==before


def test_today_thirty_minute_limit_even_when_fingerprint_changes(tmp_path):
    source(tmp_path);calls=[]
    def model(data):calls.append(data);return response()
    first=digest.generate_digest(tmp_path,DAY,now=NOW,model_call=model)
    data=activities();data[0]['items'][0]['evidence'][0]['quote']+='，有点累';source(tmp_path,data)
    held=digest.generate_digest(tmp_path,DAY,now=NOW+timedelta(minutes=29),model_call=model)
    assert held==first and len(calls)==1
    changed=digest.generate_digest(tmp_path,DAY,now=NOW+timedelta(minutes=30),model_call=model)
    assert len(calls)==2 and changed['fingerprint']!=first['fingerprint']


def test_historical_changed_fingerprint_regenerates_without_today_limit(tmp_path):
    source(tmp_path)
    later=NOW+timedelta(days=1)
    first=digest.generate_digest(tmp_path,DAY,now=later,model_call=lambda _:response())
    data=activities();data[0]['title']='明确补充';source(tmp_path,data)
    second=digest.generate_digest(tmp_path,DAY,now=later+timedelta(minutes=1),model_call=lambda _:response())
    assert second['fingerprint']!=first['fingerprint']


def test_model_failure_keeps_old_cards_fingerprint_and_records_safe_error(tmp_path):
    source(tmp_path)
    first=digest.generate_digest(tmp_path,DAY,now=NOW,model_call=lambda _:response())
    data=activities();data[0]['title']='新记录';source(tmp_path,data)
    secret_marker='must-not-persist-provider-text'
    calls=[]
    def fail(_):calls.append(1);raise TimeoutError(secret_marker)
    failed=digest.generate_digest(tmp_path,DAY,now=NOW+timedelta(minutes=31),model_call=fail)
    assert failed['cards']==first['cards'] and failed['fingerprint']==first['fingerprint']
    assert failed['status']=='error' and failed['error']['type']=='TimeoutError'
    assert secret_marker not in digest.cache_path(tmp_path,DAY).read_text()
    digest.generate_digest(tmp_path,DAY,now=NOW+timedelta(minutes=32),model_call=fail)
    assert len(calls)==1


def test_bad_response_or_all_rejected_keeps_old_cards(tmp_path):
    source(tmp_path)
    first=digest.generate_digest(tmp_path,DAY,now=NOW,model_call=lambda _:response())
    data=activities();data[0]['title']='更新';source(tmp_path,data)
    bad=response();bad['cards'][0]['activity_ids']=['not-real','a2']
    failed=digest.generate_digest(tmp_path,DAY,now=NOW+timedelta(minutes=31),model_call=lambda _:bad)
    assert failed['status']=='error' and failed['cards']==first['cards'] and failed['validation_errors']


def test_corrupt_source_keeps_old_results_without_calling_model(tmp_path):
    path=source(tmp_path)
    first=digest.generate_digest(tmp_path,DAY,now=NOW,model_call=lambda _:response())
    path.write_text('{broken')
    failed=digest.generate_digest(tmp_path,DAY,now=NOW+timedelta(hours=1),model_call=lambda _:pytest.fail('must not call'))
    assert failed['status']=='error' and failed['cards']==first['cards']


def test_pre_cutover_is_disabled_even_with_existing_cache(tmp_path):
    day='2026-10-25';p=digest.cache_path(tmp_path,day);p.parent.mkdir();p.write_text(json.dumps({'cards':[{}],'status':'ready'}))
    before=p.read_bytes()
    assert digest.generate_digest(tmp_path,day,model_call=lambda _:pytest.fail('must not call'))['status']=='disabled'
    assert digest.read_cached(tmp_path,day)['cards']==[] and not digest.trigger_generation(tmp_path,day)
    assert p.read_bytes()==before


@pytest.mark.parametrize('day',['../2026-11-05','2026-02-30','20261105','2026-1-01','2026-11-05/extra'])
def test_date_validation(day,tmp_path):
    with pytest.raises(ValueError):digest.generate_digest(tmp_path,day)
    assert list(tmp_path.iterdir())==[]


def test_same_day_file_lock_prevents_duplicate_model_call(tmp_path):
    source(tmp_path)
    with digest.day_lock(tmp_path,DAY) as locked:
        assert locked
        result=digest.generate_digest(tmp_path,DAY,now=NOW,model_call=lambda _:pytest.fail('must not run concurrently'))
        assert result['status']=='pending'


def test_background_same_day_coalesces_and_request_returns_old_cache(tmp_path,monkeypatch):
    source(tmp_path);started=threading.Event();release=threading.Event();done=threading.Event();calls=[]
    def model(_):
        calls.append(1);started.set();assert release.wait(3);return response()
    original=digest.generate_digest
    def generate(root,day):
        try:return original(root,day,now=NOW,model_call=model)
        finally:done.set()
    monkeypatch.setattr(digest,'generate_digest',generate)
    try:
        assert digest.trigger_generation(tmp_path,DAY)
        assert started.wait(2)
        assert not digest.trigger_generation(tmp_path,DAY)
        assert digest.read_cached(tmp_path,DAY)['status']=='generating'
    finally:
        release.set();assert done.wait(3)
    assert len(calls)==1 and digest.read_cached(tmp_path,DAY)['status']=='ready'


def test_no_borrowing_bench_count_for_shoulder_even_with_both_source_refs():
    raw=response();raw['cards'][0]['details'][1].update(sets=3,source_item_ids=['a1::i1','a2::i2'])
    assert validate(raw)[0][0]['details'][1]['sets'] is None


def test_an_activity_cannot_extend_card_time_without_a_referenced_detail():
    raw=response();raw['cards'][0]['details']=raw['cards'][0]['details'][:1]
    cards,errors=validate(raw)
    assert cards==[] and errors[0]['reason']=='unreferenced_activity'


@pytest.mark.parametrize('weight,quote',[('-5千克','单边5千克'),('5千克','负5千克')])
def test_negative_weight_not_rewritten_as_positive(weight,quote):
    assert digest.supported_weight(weight,[quote]) is None


def test_provider_request_uses_existing_settings_timeout_and_one_attempt(monkeypatch):
    from io import BytesIO
    seen=[]
    monkeypatch.setattr(digest.cognitive,'_get_deepseek_settings',lambda:('https://example.invalid','test-only','configured-model',None,None))
    monkeypatch.setattr(digest.cognitive,'_model_timeout',lambda n:17)
    def open_once(request,timeout):
        seen.append((json.loads(request.data),timeout))
        return BytesIO(json.dumps({'choices':[{'message':{'content':'{"cards":[]}'}}]}).encode())
    monkeypatch.setattr(digest.urllib.request,'urlopen',open_once)
    assert digest.call_model([])=={'cards':[]}
    assert len(seen)==1 and seen[0][1]==17 and seen[0][0]['model']=='configured-model'
    assert seen[0][0]['reasoning_effort']=='none'


def test_empty_provider_content_is_classified_without_retry(monkeypatch):
    from io import BytesIO
    calls=[]
    monkeypatch.setattr(digest.cognitive,'_get_deepseek_settings',lambda:('https://example.invalid','test-only','configured-model',None,None))
    def open_once(*args,**kwargs):
        calls.append(1);return BytesIO(json.dumps({'choices':[{'message':{'content':None},'finish_reason':'length'}]}).encode())
    monkeypatch.setattr(digest.urllib.request,'urlopen',open_once)
    with pytest.raises(ValueError,match='empty_model_content'):digest.call_model([])
    assert len(calls)==1


def test_uncertainty_string_is_one_note_not_individual_characters():
    raw=response();raw['cards'][0]['uncertain_notes']='器械尚未说明'
    data=activities();data[0]['uncertainties']='动作关联待确认'
    card=validate(raw,data)[0][0]
    assert '器械尚未说明' in card['uncertain_notes']
    assert '动作关联待确认' in card['uncertain_notes']
    assert '器' not in card['uncertain_notes']


def test_background_worker_failure_is_logged_and_releases_day_slot(tmp_path,monkeypatch,caplog):
    done=threading.Event()
    def fail(root,day):
        done.set();raise OSError('sensitive-provider-text')
    monkeypatch.setattr(digest,'generate_digest',fail)
    assert digest.trigger_generation(tmp_path,DAY)
    assert done.wait(2)
    # The job releases its key in finally; joining the named daemon is bounded.
    for thread in threading.enumerate():
        if thread.name=='activity-digest-'+DAY:thread.join(timeout=2)
    assert (str(tmp_path.resolve()),DAY) not in digest._jobs
    assert 'OSError' in caplog.text and 'sensitive-provider-text' not in caplog.text


def test_http_returns_old_cache_while_model_is_blocked(tmp_path,monkeypatch):
    from life_logger.local_web import JobStore,create_server
    source(tmp_path)
    cached=digest.generate_digest(tmp_path,DAY,now=NOW,model_call=lambda _:response())
    data=activities();data[0]['title']='新一轮更新';source(tmp_path,data)
    entered=threading.Event();release=threading.Event();done=threading.Event()
    original=digest.generate_digest
    def model(_):
        entered.set();assert release.wait(4);return response()
    def generate(root,day):
        try:return original(root,day,now=NOW+timedelta(minutes=31),model_call=model)
        finally:done.set()
    monkeypatch.setattr(digest,'generate_digest',generate)
    server=create_server(host='127.0.0.1',port=0,store=JobStore(tmp_path,recover=False),access_token='digest-test-only')
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        req=urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/activity-digest?date='+DAY,headers={'Authorization':'Bearer digest-test-only'})
        with urllib.request.urlopen(req,timeout=2) as r:result=json.load(r)
        assert result['cards']==cached['cards']
        assert entered.wait(2) and not release.is_set()
        with urllib.request.urlopen(req,timeout=2) as r:assert json.load(r)['generation_scheduled'] is False
    finally:
        release.set();assert done.wait(3)
        server.shutdown();server.server_close();thread.join(timeout=3)


def test_get_route_auth_date_and_cached_response_without_waiting(tmp_path,monkeypatch):
    from life_logger.local_web import JobStore,create_server
    source(tmp_path)
    expected=digest.generate_digest(tmp_path,DAY,now=NOW,model_call=lambda _:response())
    scheduled=[]
    monkeypatch.setattr(digest,'trigger_generation',lambda root,day:scheduled.append((root,day)) or True)
    server=create_server(host='127.0.0.1',port=0,store=JobStore(tmp_path,recover=False),access_token='digest-test-only')
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}/api/activity-digest'
    def get(query,authenticated=True):
        headers={'Authorization':'Bearer digest-test-only'} if authenticated else {}
        with urllib.request.urlopen(urllib.request.Request(base+query,headers=headers),timeout=3) as r:return json.load(r)
    try:
        with pytest.raises(urllib.error.HTTPError) as error:get('?date='+DAY,False)
        assert error.value.code==401 and not scheduled
        with pytest.raises(urllib.error.HTTPError) as error:get('?date=../bad')
        assert error.value.code==400 and not scheduled
        result=get('?date='+DAY)
        assert result['cards']==expected['cards'] and result['generation_scheduled'] is True
        assert scheduled==[(tmp_path,DAY)]
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)


def classification(aid,category='工作·项目',media=False,reason='原话是在验收项目'):
    return {'activity_id':aid,'category_model':category,'looks_like_media':media,'reason':reason}


@pytest.mark.parametrize('patch',[
    {'category_model':'生活·日常'}, {'category_model':['工作·项目']},
    {'activity_id':'invented'}, {'looks_like_media':'true'}, {'looks_like_media':1},
    {'reason':''}, {'reason':None}, {'activity_id':None},
])
def test_invalid_category_fields_are_discarded(patch):
    row={**classification('a1'),**patch}
    found,errors=digest.validate_categories({'categories':[row]},digest.prepare(activities(),DAY,include_media=True))
    assert found=={} and errors


def test_category_duplicates_do_not_silently_choose_one():
    rows=[classification('a1'),classification('a1','媒体内容',True),classification('a1')]
    assert digest.validate_categories({'categories':rows},digest.prepare(activities(),DAY))[0]=={}


def test_category_map_and_missing_fields_are_backward_compatible(tmp_path):
    prepared=digest.prepare(activities(),DAY)
    assert digest.validate_categories({'cards':[]},prepared)==({},[])
    found,errors=digest.validate_categories({'categories':{'a1':{'category_model':'工作·项目','looks_like_media':False,'reason':'项目请求'}}},prepared)
    assert not errors and found['a1']['category_model']=='工作·项目'
    old={'schema':digest.SCHEMA,'date':DAY,'status':'ready','cards':[]}
    digest.atomic_save(digest.cache_path(tmp_path,DAY),old)
    assert digest.read_cached(tmp_path,DAY)==old and 'categories' not in digest.read_cached(tmp_path,DAY)


def test_classification_includes_media_but_does_not_change_source_labels(tmp_path):
    data=activities();data[0]['category']='生活·日常';data[1]['lifecycle']='media'
    path=source(tmp_path,data);before=path.read_bytes();calls=[]
    def model(batch):
        calls.append(batch)
        raw=response();raw['categories']=[classification('a1','运动·健身',False,'身体训练'),classification('a2','媒体内容',True,'剧情对白')]
        return raw
    result=digest.generate_digest(tmp_path,DAY,now=NOW,model_call=lambda _:{'cards':[]},category_call=model)
    assert len(calls)==1 and {a['activity_id'] for a in calls[0]}=={'a1','a2'}
    assert result['categories']['a2']['looks_like_media'] is True
    assert result['cards']==[] and result['cards_status']=='empty'
    assert path.read_bytes()==before
    assert json.loads(path.read_text())['activities'][1]['lifecycle']=='media'
    assert json.loads(path.read_text())['activities'][0]['category']=='生活·日常'


def test_single_activity_still_gets_classification(tmp_path):
    source(tmp_path,activities()[:1]);calls=[]
    def model(batch):
        calls.append(batch);return {'cards':[],'categories':[classification('a1','运动·健身',False,'卧推训练')]}
    result=digest.generate_digest(tmp_path,DAY,now=NOW,model_call=lambda _:{'cards':[]},category_call=model)
    assert len(calls)==1 and result['cards']==[] and set(result['categories'])=={'a1'}


def many_activities(count):
    return [{'activity_id':f'entry_{i:03d}','lifecycle':'live','title':'项目验收','spoken_at':DAY+f'T{8+i//60:02d}:{i%60:02d}:00+08:00','items':[]} for i in range(count)]


def test_batches_merge_classifications_with_at_most_twenty_and_one_round_cooldown(tmp_path):
    data=many_activities(161);path=source(tmp_path,data);before=path.read_bytes();sizes=[]
    def model(batch):
        sizes.append(len(batch));return {'cards':[],'categories':[classification(a['activity_id']) for a in batch]}
    first=digest.generate_digest(tmp_path,DAY,now=NOW,model_call=lambda _:{'cards':[]},category_call=model)
    assert sorted(sizes)==[1]+[20]*8 and len(first['categories'])==161 and first['category_batch_count']==9
    assert first['category_coverage']=={'input':161,'classified':161,'missing':0}
    assert path.read_bytes()==before
    data[0]['title']='验收有更新';source(tmp_path,data)
    held=digest.generate_digest(tmp_path,DAY,now=NOW+timedelta(minutes=29),model_call=lambda _:{'cards':[]},category_call=model)
    assert held==first and len(sizes)==9
    updated=digest.generate_digest(tmp_path,DAY,now=NOW+timedelta(minutes=30),model_call=lambda _:{'cards':[]},category_call=model)
    assert sorted(sizes)==[1,1]+[20]*8 and updated['fingerprint']!=first['fingerprint']


def test_batch_response_cannot_classify_ids_outside_its_input(tmp_path):
    source(tmp_path,many_activities(81))
    def model(batch):
        return {'cards':[],'categories':[classification('entry_080')]}
    result=digest.generate_digest(tmp_path,DAY,now=NOW,model_call=lambda _:{'cards':[]},category_call=model)
    assert set(result['categories'])=={'entry_080'} and result['category_errors']
    assert result['category_coverage']['missing']==80


def test_failed_batch_preserves_only_its_old_categories_and_other_batches_continue(tmp_path):
    data=many_activities(81);source(tmp_path,data)
    old=digest.generate_digest(tmp_path,DAY,now=NOW,model_call=lambda _:{'cards':[]},category_call=classified)
    for a in data:a['title']='新一轮'+a['activity_id']
    source(tmp_path,data);calls=[]
    def fail_one(batch):
        calls.append([a['activity_id'] for a in batch])
        if batch[0]['activity_id']=='entry_020':raise TimeoutError('provider-sensitive-text')
        return classified(batch,'媒体内容',True,'候选剧情')
    result=digest.generate_digest(tmp_path,DAY,now=NOW+timedelta(minutes=31),model_call=lambda _:{'cards':[]},category_call=fail_one)
    assert len(calls)==5 and result['cards_status']=='empty'
    assert result['category_coverage']=={'input':81,'classified':61,'missing':20}
    assert result['category_status']=='partial' and len(result['category_errors'])==1
    assert result['categories']['entry_020']==old['categories']['entry_020']
    assert result['categories']['entry_080']['looks_like_media'] is True
    assert 'provider-sensitive-text' not in json.dumps(result)
    retried=[]
    fixed=digest.generate_digest(tmp_path,DAY,now=NOW+timedelta(minutes=62),model_call=lambda _:pytest.fail('cards must be cached'),
                                 category_call=lambda batch:retried.extend(a['activity_id'] for a in batch) or classified(batch))
    assert retried==[f'entry_{i:03d}' for i in range(20,40)]
    assert fixed['category_coverage']=={'input':81,'classified':81,'missing':0}
    assert fixed['cards']==result['cards']


def test_model_projection_compacts_media_without_losing_card_quote_provenance():
    data=activities();data[1]['lifecycle']='media'
    data[1]['items']=[{'item_id':f'v{i}','evidence':[{'quote':f'剧情台词{i}'}]} for i in range(60)]
    prepared=digest.prepare(data,DAY,include_media=True);before=deepcopy(prepared)
    projected={a['activity_id']:a for a in digest.model_projection(prepared)}
    assert projected['a1']['items'][0]['ref']=='a1::i1'
    assert projected['a1']['items'][0]['quotes']==prepared[0]['items'][0]['quotes']
    assert projected['a2']['items']==[] and len(projected['a2']['classification_quotes'])<=18
    assert projected['a2']['evidence_sampled'] is True and prepared==before


def test_missing_source_preserves_category_only_cache(tmp_path):
    path=source(tmp_path)
    old=digest.generate_digest(tmp_path,DAY,now=NOW,model_call=lambda _:{'cards':[]},category_call=lambda batch:{'categories':[classification(a['activity_id'],'运动·健身',False,'训练记录') for a in batch]})
    assert old['cards']==[] and len(old['categories'])==2
    path.unlink()  # Only a tmp_path fixture, never the actual activity source.
    result=digest.generate_digest(tmp_path,DAY,now=NOW+timedelta(hours=1),model_call=lambda _:pytest.fail('missing source must not call model'))
    assert result['status']=='error' and result['categories']==old['categories']
    assert result['fingerprint']==old['fingerprint']


def test_small_classification_http_contract(monkeypatch,isolate_classifier_http):
    from io import BytesIO
    captured={}
    monkeypatch.setattr(digest.cognitive,'_get_deepseek_settings',lambda:('http://unit.invalid','unit-key','unit-model',None,None))
    def http(request,timeout):
        captured.update(body=json.loads(request.data),timeout=timeout)
        return BytesIO(json.dumps({'choices':[{'message':{'content':json.dumps({'categories':[]})}}]}).encode())
    monkeypatch.setattr(digest.urllib.request,'urlopen',http)
    p=digest.prepare(activities(),DAY,include_media=True)
    p[0]['title']='题'*100;p[0]['items'][0]['quotes']=['原'*100]*20
    assert isolate_classifier_http(p)=={'categories':[]}
    body=captured['body'];data=json.loads(body['messages'][1]['content'])['activities']
    assert captured['timeout']==120 and body['max_tokens']==16000
    assert len(data)==2 and all(len(a['t'])<=50 and len(a['q'])<=5 and all(len(q)<=40 for q in a['q']) for a in data)
    assert 'cards' not in body['messages'][0]['content']
    with pytest.raises(ValueError,match='classification_batch_too_large'):isolate_classifier_http(p*11)


def test_short_category_schema_still_enforces_batch_ids():
    rows,errors=digest.validate_categories({'categories':[{'id':'a1','c':'工作·项目','m':False,'why':'项目验收'},
                                                          {'id':'foreign','c':'饮食','m':False,'why':'吃饭'}]},digest.prepare(activities(),DAY))
    assert set(rows)=={'a1'} and errors and rows['a1']['category_model']=='工作·项目'


def test_card_failure_keeps_fresh_classification_and_previous_cards(tmp_path):
    source(tmp_path)
    old=digest.generate_digest(tmp_path,DAY,now=NOW,model_call=lambda _:response())
    data=activities();data[0]['title']='项目讨论';source(tmp_path,data)
    result=digest.generate_digest(tmp_path,DAY,now=NOW+timedelta(minutes=31),model_call=lambda _:(_ for _ in ()).throw(TimeoutError('private provider text')))
    assert result['cards']==old['cards'] and result['fingerprint']==old['fingerprint']
    assert result['cards_status']=='error' and result['category_status']=='ready'
    assert result['category_coverage']=={'input':2,'classified':2,'missing':0}
    assert 'private provider text' not in json.dumps(result)


def test_category_failure_does_not_invalidate_new_cards(tmp_path):
    source(tmp_path)
    result=digest.generate_digest(tmp_path,DAY,now=NOW,model_call=lambda _:response(),
                                  category_call=lambda _:(_ for _ in ()).throw(TimeoutError('private provider text')))
    assert result['cards_status']=='ready' and result['cards'] and result['status']=='ready'
    assert result['category_status']=='error' and result['category_coverage']['missing']==2
    assert result['category_errors'][0]['error']['type']=='TimeoutError'
    assert 'private provider text' not in json.dumps(result)


def test_authorized_training_context_is_generic_bounded_and_excludes_software():
    data=activities();third=deepcopy(data[1]);third['activity_id']='unrelated-generated-id'
    third['items'][0].update(item_id='pedal',spoken_at=DAY+'T19:45:00+08:00',evidence=[{'quote':'反重力蹬不上了'}])
    data.append(third)
    software=deepcopy(third);software['activity_id']='software';software['items'][0]['evidence']=[{'quote':'微信健身舞运行成功吗'}];data.append(software)
    prepared=digest.prepare(data,DAY);before=deepcopy(prepared);context=digest.training_context(prepared)
    assert prepared==before
    assert {a['activity_id'] for a in context if a.get('trusted_training_context')}=={'a1','a2','unrelated-generated-id'}
    assert 'software' not in json.dumps([a.get('trusted_training_context') for a in context])
    assert all(a.get('trusted_training_context',{}).get('scene_confirmed') for a in context if a['activity_id']!='software')
    third['items'][0]['spoken_at']=DAY+'T20:20:00+08:00'
    boundary=digest.training_context(digest.prepare(data[:2]+[third],DAY))
    assert boundary[-1]['trusted_training_context']['same_session_activity_ids']==['a2']
    third['items'][0]['spoken_at']=DAY+'T20:21:01+08:00'
    far=digest.training_context(digest.prepare(data[:2]+[third],DAY))
    assert not far[-1].get('trusted_training_context')


def test_trusted_scene_context_supersedes_old_scene_doubts_only_in_card():
    data=activities();data[0]['uncertainties']=['与上轮是否同一场次无法确证','发生钟点未说明']
    data[1]['items'][0]['uncertainties']=['无地点证据，无法确认是否与上轮属于同一场景','器械重量未说明']
    prepared=digest.training_context(digest.prepare(data,DAY));before=deepcopy(prepared)
    cards,errors=digest.validate_cards(response(),prepared)
    assert not errors and prepared==before
    notes=cards[0]['uncertain_notes']
    assert not any('同一场次无法确证' in n or '无法确认是否与上轮属于同一场景' in n for n in notes)
    assert any('可信训练上下文' in n for n in notes)
    assert '发生钟点未说明' in notes and '器械重量未说明' in notes


def test_spoken_set_proposal_does_not_inherit_source_completion_inference():
    data=activities();data[0]['items'][0]['evidence'][0]['quote']='平板卧推做三组可以了'
    data[0]['uncertainties']=['该句按完成三组计']
    prepared=digest.prepare(data,DAY);before=deepcopy(prepared)
    cards,errors=digest.validate_cards(response(),prepared)
    assert not errors and prepared==before
    detail=cards[0]['details'][0]
    assert detail['sets']==3 and '不确认已完成' in detail['note']
    assert not any('按完成三组计' in n for n in cards[0]['uncertain_notes'])
    assert any('未据此确认已完成' in n for n in cards[0]['uncertain_notes'])


def test_projection_pool_is_bounded_to_three_and_does_not_serialize_lanes(tmp_path):
    data=many_activities(41);source(tmp_path,data)
    lock=threading.Lock();active=[0];maximum=[0];release=threading.Event()
    def predict(batch,kind):
        with lock:
            active[0]+=1;maximum[0]=max(maximum[0],active[0])
            if active[0]==3:release.set()
        assert release.wait(2)
        with lock:active[0]-=1
        return {'cards':[]} if kind=='cards' else classified(batch)
    result=digest.generate_digest(tmp_path,DAY,now=NOW,model_call=lambda b:predict(b,'cards'),category_call=lambda b:predict(b,'categories'))
    assert maximum[0]==3 and result['category_coverage']['classified']==41
    assert result['cards_status']=='empty' and result['category_status']=='ready'


def test_classification_media_suggestion_never_relabels_original_live(tmp_path):
    data=activities();path=source(tmp_path,data);before=path.read_bytes()
    result=digest.generate_digest(tmp_path,DAY,now=NOW,model_call=lambda _:{'cards':[]},category_call=lambda batch:{'categories':[classification(a['activity_id'],'媒体内容',True,'疑似剧集对白') for a in batch]})
    assert all(v['looks_like_media'] for v in result['categories'].values())
    assert path.read_bytes()==before and all(a['lifecycle']=='live' for a in json.loads(path.read_text())['activities'])

