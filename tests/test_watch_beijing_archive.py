"""Isolated Watch intake and ASR archival tests; no model or production I/O."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from datetime import datetime

import pytest

from life_logger.health_store import HealthDataStore
from life_logger.timeutil import local_date
from life_logger.watch_stream import WatchSessionStore
import life_logger.watch_formal_asr as formal


@pytest.fixture(params=['America/Los_Angeles', 'Asia/Shanghai', 'UTC'])
def host_tz(request):
    previous = os.environ.get('TZ')
    os.environ['TZ'] = request.param
    time.tzset()
    try:
        yield request.param
    finally:
        if previous is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = previous
        time.tzset()


def receive(store, sequence=0, offset=0):
    body = b'isolated sealed Watch audio'
    return store.receive_segment(
        device_id='watch-test', session_id='delayed', sequence=sequence,
        start_offset_ms=offset, duration_ms=1000,
        checksum='sha256:' + hashlib.sha256(body).hexdigest(), body=body,
        heart_rate_bpm=80, step_count=123 + sequence,
    )


@pytest.mark.parametrize('started,offset,day', [
    ('2026-10-25T15:59:59+00:00', 0, '2026-10-25'),
    ('2026-10-25T15:59:59+00:00', 1000, '2026-10-26'),
    ('2026-03-08T01:59:59-08:00', 2000, '2026-03-08'),
    ('2026-11-01T01:30:00-07:00', 0, '2026-11-01'),
    ('2026-11-01T01:30:00-08:00', 0, '2026-11-01'),
])
def test_delayed_upload_archives_capture_day(tmp_path, host_tz, started, offset, day):
    store = WatchSessionStore(tmp_path / 'watch-sessions')
    store.start_session(device_id='watch-test', session_id='delayed', started_at=started)
    if offset != 0:
        # First segment must start at zero; send a dummy then the delayed one.
        receive(store, sequence=0, offset=0)
        receive(store, sequence=1, offset=offset)
        segment = store.load_session('watch-test', 'delayed')['segments'][1]
    else:
        receive(store, offset=offset)
        segment = store.load_session('watch-test', 'delayed')['segments'][0]
    assert segment['start_offset_ms'] == offset, segment
    manifest = store.load_session('watch-test', 'delayed')
    assert manifest['started_at'] == started
    health = HealthDataStore(tmp_path / 'health')
    steps = json.loads(health.steps_path.read_text())
    day_steps = next((s for s in steps if s['date'] == day), None)
    assert day_steps is not None, steps
    assert day_steps['steps'] == 123 + (1 if offset != 0 else 0)
    assert health.get_daily_summary(day).avg_heart_rate == 80
    hrs = json.loads(health.hr_path.read_text())
    target_hrs = [h for h in hrs if local_date(h['timestamp']) == day]
    assert target_hrs, hrs


def test_unknown_capture_never_uses_upload_time(tmp_path, host_tz):
    store = WatchSessionStore(tmp_path / 'watch-sessions')
    store.start_session(device_id='watch-test', session_id='delayed',
                        started_at='2026-10-25T16:00:00+00:00')
    path = store.session_directory('watch-test', 'delayed') / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest.pop('started_at')  # legacy incomplete manifest
    path.write_text(json.dumps(manifest))
    receive(store)
    health = HealthDataStore(tmp_path / 'health')
    assert json.loads(health.steps_path.read_text()) == []
    assert json.loads(health.hr_path.read_text()) == []
    assert store.load_session('watch-test', 'delayed')['segments'][0]['health_snapshot']


def test_health_midnight_does_not_count_sample_on_both_days(tmp_path, host_tz):
    health = HealthDataStore(tmp_path / 'health')
    rows = [{'timestamp': '2026-10-25T15:59:59Z', 'bpm': 60},
            {'timestamp': '2026-10-25T16:00:00Z', 'bpm': 90},
            {'timestamp': '2026-10-26T00:00:00+08:00', 'bpm': 100},
            {'timestamp': 'invalid', 'bpm': 200}]
    health.record_heart_rates(rows)
    assert health.get_daily_summary('2026-10-25').avg_heart_rate == 60
    assert health.get_daily_summary('2026-10-26').avg_heart_rate == 95
    assert sorted(json.loads(health.hr_path.read_text()), key=lambda x: x['timestamp']) == sorted(rows, key=lambda x: x['timestamp'])


def test_formal_asr_future_date_directory_is_beijing(tmp_path, monkeypatch, host_tz):
    store = WatchSessionStore(tmp_path / 'watch-sessions')
    started = '2026-10-25T08:00:00-08:00'
    store.start_session(device_id='watch-test', session_id='delayed', started_at=started)
    receive(store)
    path = store.session_directory('watch-test', 'delayed') / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['timezone'] = 'America/Los_Angeles'
    path.write_text(json.dumps(manifest))
    before = path.read_bytes()
    monkeypatch.setattr(formal, 'load_local_config', lambda _: {})
    monkeypatch.setattr(formal, 'validate_local_pipeline_config', lambda _: None)
    monkeypatch.setattr(formal, 'local_pipeline_env', lambda _: os.environ.copy())
    # Real subprocess and real daily writer, replacing only model inference.
    script = '''
import sys
from pathlib import Path
from datetime import datetime
from unittest.mock import MagicMock
from scripts.run_full_funasr_pipeline import _build_daily_outputs
import scripts.run_full_funasr_pipeline as _pipeline
import life_logger.ingest as _ingest
from life_logger.domain import RecordingSegment

def _fake_recording(_source, **kwargs):
    return [RecordingSegment(
        recording_id='test-rec', source_file=str(_source),
        started_at='2026-10-26T00:00:00+08:00', ended_at='2026-10-26T00:00:01+08:00',
        duration_seconds=1.0, sequence=0, time_source='user',
        time_precision='exact', ingestion_warnings=[])]
# Fake recording discovery so the test's byte payload doesn't need ffprobe.
_pipeline.discover_recordings = _fake_recording
_ingest.discover_recordings = _fake_recording
_build_daily_outputs(output_dir=Path(sys.argv[1]), source=Path(sys.argv[2]),
 transcript_records=[], start_at=datetime.fromisoformat(sys.argv[3]),
 timezone_name=sys.argv[4], model_path='isolated-test', warnings=[])
import json
out_dir = Path(sys.argv[1])
(out_dir / 'run-summary.json').write_text(json.dumps({
    'status': 'completed', 'transcript_count': 0, 'warnings': []}))
(out_dir / 'recordings.json').write_text(json.dumps([]))
(out_dir / 'transcript.json').write_text(json.dumps([]))
'''
    calls = []
    def command(**kwargs):
        calls.append(kwargs)
        return [sys.executable, '-c', script, str(kwargs['output_dir']),
                str(kwargs['audio']), kwargs['start_at'], kwargs['timezone_name']]
    monkeypatch.setattr(formal, 'build_asr_command', command)
    processor = formal.WatchFormalASRProcessor(
        store, tmp_path / 'config.json', threading.Event(),
        slot_acquirer=lambda owner: {'owner': owner}, slot_releaser=lambda _: None)
    assert processor.scan_once()
    root = store.session_directory('watch-test', 'delayed') / 'formal-asr/segments/000000'
    state = json.loads((root / 'state.json').read_text())
    assert state['status'] == 'no_speech', state
    assert list(root.rglob('2026-10-26/transcript.json'))
    assert not list(root.rglob('2026-10-25/transcript.json'))
    assert datetime.fromisoformat(calls[0]['start_at']) == datetime.fromisoformat(started)
    assert path.read_bytes() == before


# ── Q480 追加：新产物一律北京偏移；采集日 ≠ 宿主日；排序按采集 instant ──────────

def test_new_artifacts_carry_beijing_offset_under_foreign_tz(tmp_path, host_tz):
    """TZ=LA 时新写出的 manifest 字段仍必须是 +08:00（不是宿主的 -07:00）。"""
    store = WatchSessionStore(tmp_path / 'watch-sessions')
    store.start_session(device_id='watch-test', session_id='delayed', started_at='2026-10-25T16:00:00Z')
    receive(store)
    manifest = store.load_session('watch-test', 'delayed')
    assert manifest['timezone'] == 'Asia/Shanghai', manifest.get('timezone')
    assert manifest['started_at_local'].endswith('+08:00'), manifest.get('started_at_local')
    # 原始绝对时刻不动（`Z` 与 `+00:00` 是同一 instant；字面归一化是既有行为）
    from datetime import timezone
    from life_logger.timeutil import parse_ts
    assert parse_ts(manifest['started_at']) == datetime(2026, 10, 25, 16, tzinfo=timezone.utc)
    for key in ('updated_at', 'created_at'):
        if isinstance(manifest.get(key), str):
            assert manifest[key].endswith('+08:00'), (key, manifest[key])
    for segment in manifest['segments']:
        assert segment['started_at_local'].endswith('+08:00'), segment
        for key in ('received_at', 'updated_at'):
            if isinstance(segment.get(key), str):
                assert segment[key].endswith('+08:00'), (key, segment[key])


def test_capture_day_is_beijing_not_host_day(tmp_path, host_tz):
    """采集 instant = 北京 10-26 12:00，在 LA 是 10-25 21:00 → 必须归到 10-26。"""
    store = WatchSessionStore(tmp_path / 'watch-sessions')
    store.start_session(device_id='watch-test', session_id='delayed', started_at='2026-10-26T04:00:00Z')
    receive(store)
    health = HealthDataStore(tmp_path / 'health')
    assert [s['date'] for s in json.loads(health.steps_path.read_text())] == ['2026-10-26']
    assert health.get_daily_summary('2026-10-26').total_steps == 123
    assert health.get_daily_summary('2026-10-25').total_steps == 0


def test_steps_updated_at_is_offset_aware(tmp_path, host_tz):
    health = HealthDataStore(tmp_path / 'health')
    health.record_steps('2026-10-26', 500)
    entry = json.loads(health.steps_path.read_text())[0]
    assert entry['updated_at'].endswith('+08:00'), entry['updated_at']


def test_heart_rate_rows_sorted_by_instant_not_string(tmp_path, host_tz):
    """混偏移的字符串排序会把 15:00Z 排到 20:00+08:00 之后；必须按 instant 排。"""
    health = HealthDataStore(tmp_path / 'health')
    rows = [{'timestamp': '2026-10-25T20:00:00+08:00', 'bpm': 80},
            {'timestamp': '2026-10-25T11:00:00Z', 'bpm': 70},
            {'timestamp': 'not-a-time', 'bpm': 90}]
    health.record_heart_rates(rows)
    stored = json.loads(health.hr_path.read_text())
    assert [r['timestamp'] for r in stored] == [
        '2026-10-25T11:00:00Z', '2026-10-25T20:00:00+08:00', 'not-a-time']
    assert {r['timestamp'] for r in stored} == {r['timestamp'] for r in rows}   # 一个不丢、不改写


@pytest.mark.parametrize('started,expected_day', [
    ('2026-03-08T09:59:59Z', '2026-03-08'),   # 北京 17:59:59（LA DST 开始日）
    ('2026-03-08T16:00:00Z', '2026-03-09'),   # 北京次日 00:00（LA 仍是 03-08 08:00）
    ('2026-11-01T15:59:59Z', '2026-11-01'),   # 北京 23:59:59（LA DST 结束日）
    ('2026-11-01T16:00:00Z', '2026-11-02'),   # 北京次日 00:00（LA 仍是 11-01 08:00）
])
def test_dst_samples_land_on_beijing_day(tmp_path, host_tz, started, expected_day):
    store = WatchSessionStore(tmp_path / 'watch-sessions')
    store.start_session(device_id='watch-test', session_id='delayed', started_at=started)
    receive(store)
    health = HealthDataStore(tmp_path / 'health')
    assert [s['date'] for s in json.loads(health.steps_path.read_text())] == [expected_day]
