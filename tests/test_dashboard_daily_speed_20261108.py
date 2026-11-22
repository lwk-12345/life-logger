"""Dashboard speed must preserve immediate file freshness, including restored mtimes."""
import json
import os

import pytest

from life_logger import daily_dashboard as dashboard

DAY = '2026-11-07'


def write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False))


@pytest.fixture(autouse=True)
def clear_caches():
    dashboard.invalidate_dashboard_caches()
    yield
    dashboard.invalidate_dashboard_caches()


def session(root):
    path = root / 'watch-sessions/watch-test/session-001'
    manifest = {'device_id': 'watch-test', 'session_id': 'session-001', 'state': 'complete',
                'started_at': DAY + 'T12:00:00+08:00',
                'segments': [{'sequence': 0, 'start_offset_ms': 0, 'duration_ms': 60_000}]}
    write(path / 'manifest.json', manifest)
    return path, manifest


def state(text='工作', seconds=60):
    return {'status': 'completed', 'sequence': 0, 'rows': [{
        'text': text, 'absolute_started_at': DAY + 'T12:00:00+08:00',
        'start_offset_seconds': 0, 'end_offset_seconds': seconds, 'source_label': 'self'}]}


def build(root, day=DAY):
    return dashboard.DailyDashboardBuilder(root, use_scan_cache=True).build_for_date(day).to_dict()


def test_new_state_is_visible_on_next_build_without_waiting_for_ttl(tmp_path):
    path, _ = session(tmp_path)
    before = build(tmp_path)
    write(path / 'formal-asr/segments/000000/state.json', state('哈哈'))
    after = build(tmp_path)
    assert before['laughter_count'] == 0
    assert after['laughter_count'] == 1
    assert after['speech_duration_minutes'] == 1


def test_in_place_state_edit_with_restored_mtime_is_visible(tmp_path):
    path, manifest = session(tmp_path)
    manifest['segments'][0]['duration_ms'] = 300_000
    write(path / 'manifest.json', manifest)
    file = path / 'formal-asr/segments/000000/state.json'
    write(file, state('工作'))
    before = build(tmp_path)
    saved = file.stat()
    write(file, state('哈哈'))
    assert file.stat().st_size == saved.st_size
    os.utime(file, ns=(saved.st_atime_ns, saved.st_mtime_ns))
    after = build(tmp_path)
    assert before['laughter_count'] == 0 and after['laughter_count'] == 1
    assert after['timeline_scenes'] != before['timeline_scenes']


def test_manifest_duration_edit_changes_next_coverage(tmp_path):
    path, manifest = session(tmp_path)
    assert build(tmp_path)['received_recording_coverage_minutes'] == 1
    saved = (path / 'manifest.json').stat()
    manifest['segments'][0]['duration_ms'] = 120_000
    write(path / 'manifest.json', manifest)
    os.utime(path / 'manifest.json', ns=(saved.st_atime_ns, saved.st_mtime_ns))
    assert build(tmp_path)['received_recording_coverage_minutes'] == 2


def test_manifest_change_rebuckets_no_speech_even_when_state_unchanged(tmp_path):
    path, manifest = session(tmp_path)
    manifest['segments'][0]['duration_ms'] = 300_000
    write(path / 'manifest.json', manifest)
    write(path / 'formal-asr/segments/000000/state.json', {'status': 'no_speech', 'sequence': 0})
    before = build(tmp_path)
    assert before['scene_breakdown_percentages']['quiet'] == 100
    saved = (path / 'manifest.json').stat()
    manifest['started_at'] = '2026-11-08T12:00:00+08:00'
    write(path / 'manifest.json', manifest)
    os.utime(path / 'manifest.json', ns=(saved.st_atime_ns, saved.st_mtime_ns))
    after = build(tmp_path)
    assert after['received_recording_coverage_minutes'] == 0
    assert after['timeline_scenes'] == []
    assert after['scene_breakdown_percentages']['home_quiet'] == 0
    assert build(tmp_path, '2026-11-08')['received_recording_coverage_minutes'] == 5


def test_exactly_one_fresh_watch_scan_per_build(tmp_path, monkeypatch):
    path, _ = session(tmp_path)
    write(path / 'formal-asr/segments/000000/state.json', state())
    scans = []
    original = dashboard._watch_scan

    def observe(root, **kwargs):
        scans.append(kwargs)
        return original(root, **kwargs)

    monkeypatch.setattr(dashboard, '_watch_scan', observe)
    build(tmp_path)
    assert scans == [{'use_memo': False}]
    scans.clear()
    build(tmp_path)
    assert scans == [{'use_memo': False}]


def test_job_transcript_changes_are_not_hidden_by_parse_cache(tmp_path):
    path = tmp_path / f'jobs/job-test/files/f/result/{DAY}/transcript.json'
    write(path, state(seconds=60)['rows'])
    assert build(tmp_path)['speech_duration_minutes'] == 1
    saved = path.stat()
    write(path, state(seconds=90)['rows'])
    os.utime(path, ns=(saved.st_atime_ns, saved.st_mtime_ns))
    assert build(tmp_path)['speech_duration_minutes'] == 1.5


def test_coverage_memo_keeps_midnight_clipping_and_roots_separate(tmp_path):
    path, manifest = session(tmp_path / 'first')
    manifest['started_at'] = DAY + 'T23:59:00+08:00'
    manifest['segments'][0]['duration_ms'] = 120_000
    write(path / 'manifest.json', manifest)
    assert build(tmp_path / 'first')['received_recording_coverage_minutes'] == 1
    assert build(tmp_path / 'first', '2026-11-08')['received_recording_coverage_minutes'] == 1
    assert build(tmp_path / 'second')['received_recording_coverage_minutes'] == 0


def test_build_result_mutation_cannot_corrupt_cached_inputs(tmp_path):
    path, manifest = session(tmp_path)
    manifest['segments'][0]['duration_ms'] = 300_000
    write(path / 'manifest.json', manifest)
    write(path / 'formal-asr/segments/000000/state.json', state())
    before = build(tmp_path)
    before['timeline_scenes'].clear()
    assert build(tmp_path)['timeline_scenes']
