import importlib.util
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('voice_group_candidates', ROOT/'scripts/voice_group_candidates.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def row(i, seconds, label, text, **extra):
    return {'id':str(i),'source_id':'src_'+str(i),'when':f'2026-11-04T20:00:{seconds:02}+08:00',
            'epoch':float(seconds),'day':'2026-11-04','text':text,'source_label':label,
            'speaker':'说话人一' if label=='self' else '说话人二','proximity':'far','content_class':'stream',
            'recording':'r','chunk':'chunk0','audio':'unused','audio_origin':0,'start':float(seconds),
            'end':float(seconds)+2,'window_ids':[],'name_hints':[],**extra}


def dialogue():
    return [row(1,0,'self','喂，你那边听得到吗'),row(2,5,'non_self','可以听到你说话'),
            row(3,10,'self','我现在听见你说话了'),row(4,15,'non_self','妈妈刚才已经吃饭了'),
            row(5,20,'self','好的我知道了')]


def test_far_stream_remote_voice_is_kept_without_using_local_speaker_number_as_identity():
    original=dialogue()
    candidates,refs,windows,stats=m.call_candidates(original)
    assert {r['id'] for r in candidates}=={'2','4'}
    assert all(r['proximity']=='far' and r['content_class']=='stream' for r in candidates)
    assert len(refs)==3 and len(windows)==1
    assert original[1]['source_label']=='non_self'
    assert {h['name'] for w in windows for h in w['name_hints']}=={'妈'}
    assert all('contact_label' not in r for r in candidates)


@pytest.mark.parametrize('own_text',['','嗯','好'])
def test_unanswered_commentary_is_rejected_even_if_it_mentions_calls(own_text):
    rows=[row(1,0,'non_self','喂，今天直播间介绍一下'),row(2,5,'non_self','接着介绍下一件产品'),
          row(3,10,'non_self','这个产品给大家讲清楚')]
    if own_text:rows.insert(1,row(4,3,'self',own_text))
    candidates,refs,windows,stats=m.call_candidates(rows)
    assert candidates==[] and windows==[] and refs==[]
    assert stats['cue_without_bidirectional_reply']==1


@pytest.mark.parametrize('mention',['女主要挂了，下一个剧情怎么写','你们工厂有事情就给我打电话','姐，四瓶多少钱？'])
def test_storytelling_phone_mentions_and_in_person_address_are_not_call_evidence(mention):
    rows=dialogue();rows[0]['text']=mention
    assert m.call_candidates(rows)[0]==[]


def test_self_replies_must_be_close_in_time_not_merely_adjacent_in_file():
    rows=dialogue();rows[-1]['epoch']=1000;rows[2]['epoch']=990
    rows.sort(key=lambda r:r['epoch'])
    assert m.call_candidates(rows)[0]==[]


def test_overlapping_cue_windows_do_not_double_count_candidates():
    rows=dialogue();rows[2]['text']='喂，你现在听清楚了吗'
    candidates,refs,windows,stats=m.call_candidates(rows)
    assert len(windows)==2
    assert len(candidates)==2 and len(refs)==3


def test_unknown_other_is_candidate_evidence_not_relabelled_as_self():
    rows=dialogue();rows[1]['source_label']='unknown'
    candidates=m.call_candidates(rows)[0]
    assert next(r for r in candidates if r['id']=='2')['source_label']=='unknown'


def test_same_speaker_number_in_another_chunk_is_not_a_merge_key():
    a=row(1,0,'non_self','第一段一句话')
    b=row(2,2,'non_self','另一个chunk一句话',chunk='chunk1')
    assert len(m.units_from_rows([a,b],'candidate'))==2


def test_contiguous_local_rows_can_merge_but_long_silence_cannot():
    a=row(1,0,'non_self','第一句')
    b=row(2,2,'non_self','第二句')
    c=row(3,10,'non_self','第三句')
    units=m.units_from_rows([a,b,c],'candidate')
    assert len(units)==2 and units[0]['clip_seconds']==4
    assert len(units[0]['utterances'])==2
    assert a['text']=='第一句'  # projection did not modify original input


def test_complete_link_does_not_merge_a_similarity_chain():
    units=[{'id':i} for i in ['a','b','c']]
    vectors={i:[math.cos(a),math.sin(a)] for i,a in zip(['a','b','c'],[0,math.pi/6,math.pi/3])}
    groups=m.cluster(units,vectors,math.cos(math.radians(40)))
    assert sorted(len(g) for g in groups)==[1,2]
    assert m.cluster(list(reversed(units)),vectors,math.cos(math.radians(40)))==groups


@pytest.mark.parametrize('a,b',[([],[]),([1],[1,0]),([float('nan')],[1]),([0],[1])])
def test_invalid_embeddings_do_not_silently_form_a_group(a,b):
    with pytest.raises(ValueError):m.cosine(a,b)


def test_empty_clustering_is_explicitly_empty():
    assert m.cluster([],{},0.65)==[]


@pytest.mark.parametrize('header',[False,True])
def test_manifest_hash_accepts_documented_header_without_weakening_integrity(tmp_path,header):
    p=tmp_path/'audio';p.write_bytes(b'real audio bytes')
    expected=m.hashlib.sha256(p.read_bytes()).hexdigest()
    assert m.verified_audio_hash(p,('sha256:' if header else '')+expected)==expected
    p.write_bytes(b'changed bytes')
    with pytest.raises(ValueError,match='audio_hash_changed'):m.verified_audio_hash(p,expected)


def test_inventory_uses_manifest_origin_instead_of_sequence_times_sixty(tmp_path):
    root=tmp_path/'watch';rec=root/'device'/'recording'
    state_dir=rec/'formal-asr'/'segments'/'000010';state_dir.mkdir(parents=True)
    audio=rec/'segments'/'segment-000010.m4a';audio.parent.mkdir();audio.write_bytes(b'audio')
    (rec/'manifest.json').write_text(json.dumps({'segments':[{'sequence':10,'file':audio.name,
            'start_offset_ms':612345,'duration_ms':60000,'sha256':'sha256:abc'}]}))
    raw={'status':'completed','rows':[{'absolute_started_at':'2026-11-04T12:00:00+00:00',
         'start_offset_seconds':613,'end_offset_seconds':615,'text':'可以听到你说话','source_label':'non_self'}]}
    state=state_dir/'state.json';state.write_text(json.dumps(raw));before=state.read_bytes()
    rows,stats,issues=m.read_rows(root,m.date(2026,11,4),m.date(2026,11,4))
    assert len(rows)==1 and rows[0]['audio_origin']==612.345
    assert rows[0]['when']=='2026-11-04T20:00:00+08:00'
    assert state.read_bytes()==before and audio.read_bytes()==b'audio'
    assert issues==[]
    assert m.read_rows(root,m.date(2026,11,5),m.date(2026,11,5))[0]==[]


def test_decode_seeks_with_manifest_offset_and_checks_truncation(monkeypatch):
    import numpy as np
    seen=[]
    def run(cmd,**kwargs):
        seen.append(cmd);return SimpleNamespace(returncode=0,stdout=np.zeros(32000,dtype='<f4').tobytes())
    monkeypatch.setattr(m.subprocess,'run',run)
    u={'start':613.595,'audio_origin':612.345,'clip_seconds':2,'audio':'audio.m4a'}
    assert len(m.decode(u))==32000
    assert seen[0][seen[0].index('-ss')+1]=='1.25'
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0,stdout=b''))
    with pytest.raises(ValueError,match='truncated_audio'):m.decode(u)


def test_privacy_display_redacts_phone_and_coordinates_without_inventing_names():
    phone = ''.join(map(str, [1, 3, 8] + [0] * 8))
    text=m.safe_text('请联系 '+phone+'，位置 '+'30.'+'123456,104.'+'123456，妈妈说明天再聊')
    assert phone not in text and '30.'+'123456' not in text and '104.'+'123456' not in text
    assert '妈妈说明天再聊' in text


def test_output_path_cannot_escape_work_through_symlink(tmp_path,monkeypatch):
    monkeypatch.setattr(m,'ROOT',tmp_path)
    (tmp_path/'work').mkdir();(tmp_path/'output').mkdir()
    (tmp_path/'work'/'escape').symlink_to(tmp_path/'output',target_is_directory=True)
    assert m.work_path(tmp_path/'work'/'ok.json').is_relative_to(tmp_path/'work')
    with pytest.raises(ValueError):m.work_path(tmp_path/'work'/'escape'/'bad.json')


def test_empty_inventory_still_persists_a_checkpoint_for_grouping(tmp_path,monkeypatch):
    import sys
    from life_logger import campplus_voiceprint as backend
    model=tmp_path/'model.onnx';model.write_bytes(b'local test weights')
    fake=SimpleNamespace(SpeakerEmbeddingExtractor=lambda config:object(),SpeakerEmbeddingExtractorConfig=lambda **kw:kw)
    monkeypatch.setitem(sys.modules,'sherpa_onnx',fake)
    monkeypatch.setattr(backend,'resolve_campplus_model_dir',lambda:(tmp_path,'test'))
    monkeypatch.setattr(backend,'resolve_campplus_onnx_path',lambda path:model)
    monkeypatch.setattr(m.os,'nice',lambda n:0)
    work=tmp_path/'work';work.mkdir()
    (work/'inventory.json').write_text(json.dumps({'units':[]}))
    assert m.embed(SimpleNamespace(work_dir=work,batch_size=8,pause=0))==0
    assert json.loads((work/'embeddings.json').read_text())['items']=={}


def test_batch_size_cannot_exceed_eight():
    with pytest.raises(SystemExit) as exc:m.main(['embed','--batch-size','9'])
    assert exc.value.code==2


def test_summary_reports_shortfall_and_never_certifies_an_unreviewed_identity():
    u=m.units_from_rows([row(1,0,'non_self','这里是一句原话')],'candidate')[0]
    vec=[1.0]+[0.0]*191
    group=m.summarize([u],{u['id']:vec},[],0.65)
    assert group['unit_count']==1 and group['day_count']==1
    assert len(group['representatives'])==1 and group['representative_shortfall']==2
    assert group['identity_verified'] is False and group['naming_eligible'] is False
    assert group['max_self_reference_similarity'] is None
