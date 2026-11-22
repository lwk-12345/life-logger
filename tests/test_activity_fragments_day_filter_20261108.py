"""[2026-11-08] load_day_rows 冷读提速：按 manifest 偏移剪掉不相干会话 + 进程内解析缓存。

结果必须与旧实现逐字一致：
* 只返回目标日的行；10 天前的会话整个跳过（连 state.json 都不读）；
* manifest 缺失的会话仍要读（回退语义不变）；
* state.json 改动（mtime/size 变）后缓存失效，能读到新行。
"""
import gzip
import json
import os

import pytest

from life_logger import activity_fragments as fragments

DAY = '2026-11-05'


@pytest.fixture(autouse=True)
def _clear_caches():
    for cache in (fragments._CACHE, fragments._STATE_CACHE, fragments._MANIFEST_CACHE):
        cache.clear()
    yield
    for cache in (fragments._CACHE, fragments._STATE_CACHE, fragments._MANIFEST_CACHE):
        cache.clear()


def _row(text, when, seg):
    return {'text': text, 'recording_id': 'rec', 'segment_id': seg, 'absolute_started_at': when,
            'speaker': '说话人一', 'voice_similarity': 0.5}


def _session(root, name, started_at, rows, *, manifest=True, sequence=1, offset_ms=0, gz=False):
    session = root / 'watch-sessions' / 'watch-dev' / name
    seg_dir = session / 'formal-asr' / 'segments' / f'{sequence:06d}'
    seg_dir.mkdir(parents=True)
    if manifest:
        (session / 'manifest.json').write_text(json.dumps(
            {'started_at': started_at, 'segments': [{'sequence': sequence, 'start_offset_ms': offset_ms}]}))
    body = json.dumps({'status': 'completed', 'rows': rows})  # ASCII 转义：允许落盘孤立代理字符
    if gz:
        path = seg_dir / 'state.json.gz'
        path.write_bytes(gzip.compress(body.encode()))
    else:
        path = seg_dir / 'state.json'
        path.write_text(body)
    return path


def _spy_reads(monkeypatch):
    seen = []
    real = fragments.read_json

    def spy(path):
        seen.append(str(path))
        return real(path)

    monkeypatch.setattr(fragments, 'read_json', spy)
    return seen


def test_only_target_day_rows_and_old_session_is_skipped_unread(tmp_path, monkeypatch):
    today = _session(tmp_path, 'today', f'{DAY}T09:00:00+08:00',
                     [_row('今天的话', f'{DAY}T09:00:05+08:00', 's1')])
    old = _session(tmp_path, 'old', '2026-10-26T09:00:00+08:00',
                   [_row('十天前的话', '2026-10-26T09:00:05+08:00', 's1')])
    seen = _spy_reads(monkeypatch)

    rows, errors = fragments.load_day_rows(tmp_path, DAY)

    assert errors == 0
    assert [r['text'] for r in rows] == ['今天的话']
    assert rows[0]['scope'] == str(today.parent.resolve())
    assert str(today) in seen
    assert not any(str(old.parent) in p for p in seen), seen  # 10 天前的 state.json 根本不读


def test_session_without_manifest_is_still_read(tmp_path):
    _session(tmp_path, 'old', '2026-10-26T09:00:00+08:00',
             [_row('十天前的话', '2026-10-26T09:00:05+08:00', 's1')])
    _session(tmp_path, 'nomanifest', None, [_row('无清单也要读', f'{DAY}T10:00:00+08:00', 's2'),
                                            _row('别的日子', '2026-11-01T10:00:00+08:00', 's3')],
             manifest=False)
    _session(tmp_path, 'badmanifest', None, [_row('坏清单也要读', f'{DAY}T11:00:00+08:00', 's4')],
             manifest=False)
    (tmp_path / 'watch-sessions/watch-dev/badmanifest/manifest.json').write_text('{not json')

    rows, errors = fragments.load_day_rows(tmp_path, DAY)

    assert errors == 0
    assert sorted(r['text'] for r in rows) == ['坏清单也要读', '无清单也要读']


def test_gz_state_is_found_like_glob_json(tmp_path):
    _session(tmp_path, 'packed', f'{DAY}T08:00:00+08:00',
             [_row('压缩的转写', f'{DAY}T08:00:01+08:00', 's1')], gz=True)
    rows, errors = fragments.load_day_rows(tmp_path, DAY)
    assert errors == 0 and [r['text'] for r in rows] == ['压缩的转写']


def test_state_cache_hits_when_unchanged_and_invalidates_on_mtime_change(tmp_path, monkeypatch):
    path = _session(tmp_path, 'today', f'{DAY}T09:00:00+08:00',
                    [_row('第一句', f'{DAY}T09:00:05+08:00', 's1')])
    rows, _ = fragments.load_day_rows(tmp_path, DAY)
    assert [r['text'] for r in rows] == ['第一句']

    # 60 秒日快照清掉后，文件没变 → 走进程内解析缓存，不重读 state.json。
    fragments._CACHE.clear()
    seen = _spy_reads(monkeypatch)
    rows, _ = fragments.load_day_rows(tmp_path, DAY)
    assert [r['text'] for r in rows] == ['第一句']
    assert str(path) not in seen

    # 改文件（mtime 变）→ 缓存失效，读到新行。
    st = path.stat()
    path.write_text(json.dumps({'status': 'completed', 'rows': [
        _row('第一句', f'{DAY}T09:00:05+08:00', 's1'),
        _row('新加的一句', f'{DAY}T09:00:30+08:00', 's2')]}, ensure_ascii=False))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    fragments._CACHE.clear()
    rows, errors = fragments.load_day_rows(tmp_path, DAY)
    assert errors == 0
    assert [r['text'] for r in rows] == ['第一句', '新加的一句']
    assert str(path) in seen


def test_out_of_range_bad_row_does_not_count_but_in_range_bad_row_does(tmp_path):
    # 旧实现：越界行在算 source_id 之前就 continue；在范围内的坏行才计 1 个 error 并停读该文件。
    bad = {'text': '\ud800坏', 'recording_id': 'rec', 'segment_id': 'sx', 'speaker': 'x'}
    _session(tmp_path, 'mixed', f'{DAY}T09:00:00+08:00', [
        {**bad, 'absolute_started_at': '2026-11-04T09:00:00+08:00'},
        _row('好句子', f'{DAY}T09:00:05+08:00', 's1'),
        {**bad, 'absolute_started_at': f'{DAY}T09:00:06+08:00'},
        _row('坏行之后读不到', f'{DAY}T09:00:07+08:00', 's2')])
    rows, errors = fragments.load_day_rows(tmp_path, DAY)
    assert [r['text'] for r in rows] == ['好句子'] and errors == 1
