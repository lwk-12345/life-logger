"""Offline business-date regressions; no live server, model or notifier."""
import json
import os
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from life_logger import daily_brief, daily_dashboard, local_reminders, local_web
from life_logger.timeutil import LOCAL_TZ


@pytest.fixture(params=['America/Los_Angeles', 'Asia/Shanghai', 'UTC'])
def host_tz(request):
    previous = os.environ.get('TZ')
    os.environ['TZ'] = request.param
    time.tzset()
    yield request.param
    if previous is None:
        os.environ.pop('TZ', None)
    else:
        os.environ['TZ'] = previous
    time.tzset()


@pytest.fixture(params=[
    ('2026-10-25T15:59:59+00:00', '2026-10-25'),
    ('2026-10-25T16:00:00+00:00', '2026-10-26'),
    ('2026-03-08T09:59:59+00:00', '2026-03-08'),
    ('2026-03-08T10:00:00+00:00', '2026-03-08'),
    ('2026-11-01T08:59:59+00:00', '2026-11-01'),
    ('2026-11-01T09:00:00+00:00', '2026-11-01'),
])
def frozen(request, monkeypatch, host_tz):
    instant, day = request.param
    fixed = datetime.fromisoformat(instant)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.astimezone().replace(tzinfo=None)

    for module in (local_web, daily_brief, daily_dashboard, local_reminders):
        monkeypatch.setattr(module, 'datetime', Clock)
    return fixed, day


def handler(tmp_path, path):
    obj = local_web._Handler.__new__(local_web._Handler)
    obj.server = SimpleNamespace(store=SimpleNamespace(root=tmp_path),
        dashboard_dictation_filter=lambda: None,
        cached_read=lambda key, callback: callback(),
        watch_session_store=SimpleNamespace(root=tmp_path / 'watch'))
    obj.path = path
    obj._require_api_auth = lambda **kwargs: True
    obj._send_json = lambda payload, *args: setattr(obj, 'response', payload)
    return obj


@pytest.mark.parametrize('path,key', [('/api/health/summary', 'summary'),
    ('/api/dashboard/daily', 'dashboard'), ('/api/timeline-scenes', None)])
@pytest.mark.parametrize('explicit', [False, True])
def test_api_business_day(tmp_path, frozen, path, key, explicit):
    _, day = frozen
    requested = '2020-02-29' if explicit else day
    obj = handler(tmp_path, path + ('?date=2020-02-29' if explicit else ''))
    obj.do_GET()
    payload = obj.response[key] if key else obj.response
    assert payload['date'] == requested


def test_health_write_defaults(tmp_path, frozen):
    instant, day = frozen
    obj = handler(tmp_path, '/api/health/sync')
    obj._body_json = lambda: {'steps': 123, 'bpm': 72}
    obj.do_POST()
    from life_logger.health_store import HealthDataStore
    store = HealthDataStore(tmp_path / 'health')
    assert store.get_daily_summary(day).total_steps == 123
    raw = store.get_latest_heart_rate()['timestamp']
    assert datetime.fromisoformat(raw).utcoffset().total_seconds() == 28800
    assert datetime.fromisoformat(raw) == instant


def test_dashboard_timestamp(tmp_path, frozen):
    instant, day = frozen
    result = daily_dashboard.DailyDashboardBuilder(tmp_path).build_for_date(day)
    stamp = datetime.fromisoformat(result.updated_at)
    assert stamp == instant
    assert stamp.utcoffset().total_seconds() == 28800


def test_brief_ignores_source_timezone(tmp_path, frozen, monkeypatch):
    instant, day = frozen
    event = {'event_id': 'source-unchanged', 'title': 'unknown', 'categories': [],
             'time': {'absolute': None, 'temporal_relation': 'unknown'}}
    from life_logger import event_view
    monkeypatch.setattr(event_view, 'build_event_view', lambda *a, **kw: {
        'events': [event], 'metadata': {'timezone': 'America/Los_Angeles'}})
    brief = daily_brief.build_daily_brief(tmp_path, date=day)
    assert brief['timezone'] == 'Asia/Shanghai'
    assert datetime.fromisoformat(brief['generated_at']) == instant
    assert brief['events'] == [event]
    assert daily_brief.external_agent_brief({**brief, 'timezone': 'UTC'})['timezone'] == 'Asia/Shanghai'


@pytest.mark.parametrize('raw', ['2026-10-26', '2026-10-26T00:00:00',
    '2026-10-25T09:00:00-07:00', '2026-10-26T00:00:00+08:00'])
def test_reminder_business_due_time(tmp_path, host_tz, raw):
    class Notifier:
        name = 'test'
        def notify(self, **kwargs):
            return True
    store = local_reminders.LocalReminderStore(tmp_path, notifier=Notifier())
    store.schedule('source-id', remind_at=raw, title='test')
    assert store.dispatch_due(now=datetime(2026, 10, 25, 15, 59, 59, tzinfo=timezone.utc)) == []
    assert store.dispatch_due(now=datetime(2026, 10, 25, 16, tzinfo=timezone.utc))[0]['task_key'] == 'source-id'


def test_future_jobs_shanghai_archive(tmp_path, frozen):
    instant, day = frozen
    store = local_web.JobStore(tmp_path, recover=False)
    raw = '2026-10-25T09:00:00-07:00'
    job = store.create_job(start_at=raw, timezone_name='America/Los_Angeles')
    state = store.read(job)
    assert state['timezone'] == 'Asia/Shanghai'
    assert state['manual_start_at'] == raw
    assert datetime.fromisoformat(state['manual_start_at']) == datetime.fromisoformat(raw)
    assert job.startswith('job-' + instant.astimezone(LOCAL_TZ).strftime('%Y%m%d-%H%M%S'))
    assert local_web._parse_client_time('2026-10-26T00:00:00', 'UTC') == '2026-10-26T00:00:00+08:00'


def test_health_explicit_source_values_preserved(tmp_path, host_tz):
    raw = '2026-10-25T09:00:00-07:00'
    obj = handler(tmp_path, '/api/health/sync')
    obj._body_json = lambda: {'steps': 10, 'date': '2020-02-29', 'bpm': 80,
                             'timestamp': raw, 'source': 'unchanged-id'}
    obj.do_POST()
    from life_logger.health_store import HealthDataStore
    store = HealthDataStore(tmp_path / 'health')
    assert store.get_daily_summary('2020-02-29').total_steps == 10
    assert store.get_latest_heart_rate()['timestamp'] == raw
    assert store.get_latest_heart_rate()['source'] == 'unchanged-id'


def test_scene_aggregate_beijing_bucket(tmp_path, host_tz):
    import json
    root = tmp_path / 'watch'
    state = root / 'device/session/formal-asr/segments/0/state.json'
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({'status': 'completed', 'rows': [{
        'absolute_started_at': '2026-10-25T09:00:00-07:00',
        'text': '测试', 'start_offset_seconds': 0, 'end_offset_seconds': 60}]}))
    # Old cached buckets must not hide a timezone-policy migration.
    fingerprint = daily_dashboard._fingerprint_dirs(root)
    cache = tmp_path / 'cache/watch-scene-aggregate.json'
    cache.parent.mkdir()
    cache.write_text(json.dumps({'fingerprint': list(fingerprint),
                                'aggregate': {'dates': {'2026-10-25': {}}}}))
    result = daily_dashboard._watch_scene_aggregate(root)
    assert set(result['dates']) == {'2026-10-26'}
    assert result['dates']['2026-10-26']['minutes'] == 1


def test_badge_clock_beijing(host_tz):
    import json
    import re
    import subprocess
    body = re.search(r'function loadedClock\(\) \{(.*?)\n  \}',
                     local_web._VERSION_BADGE_SCRIPT, re.S).group(1)
    script = ('const info = {deployed_at:"2026-10-25T09:03:00-07:00"};'
              'function loadedClock(){' + body + '};'
              'console.log(JSON.stringify([loadedClock(),info.deployed_at]));')
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == ['00:03', '2026-10-25T09:03:00-07:00']


# ═══════════════════════════════════════════════════════════════════════════
# Q476：系统时区漂移（Mac 改到美国/欧洲）时，业务时间仍必须按北京时间
# 下面三条是**先失败后修好**的反例（红 → 绿），固化在此防止回归。
# ═══════════════════════════════════════════════════════════════════════════

def test_search_result_seconds_ignores_host_tz(host_tz):
    """无 offset 的时间戳必须按**北京时间**解释，不能跟随系统时区。

    原实现 `datetime.fromisoformat(ts).timestamp()` 对 naive 值按系统本地时区解释，
    TZ=America/Los_Angeles 时同一行会偏移 15 小时 → 搜索结果排序/区间过滤错位。
    """
    seconds = local_web._search_result_seconds({"timestamp": "2026-11-01T00:30:00"})
    expected = datetime(2026, 11, 1, 0, 30, tzinfo=LOCAL_TZ).timestamp()
    assert seconds == expected, (
        f"TZ={host_tz} 下按系统时区解释了 naive 时间戳：{seconds} != {expected}"
    )


def test_search_result_seconds_respects_explicit_offset(host_tz):
    """带 offset 的时间戳保留其 instant（不受系统时区影响）。"""
    seconds = local_web._search_result_seconds({"timestamp": "2026-10-31T16:30:00+00:00"})
    expected = datetime(2026, 10, 31, 16, 30, tzinfo=timezone.utc).timestamp()
    assert seconds == expected


def test_watch_aggregate_cache_generated_at_is_beijing(tmp_path, host_tz, monkeypatch):
    """缓存文件的 generated_at 必须是 +08:00，而不是宿主时区。

    原实现 `datetime.now().astimezone()` 在 TZ=America/Los_Angeles 下写出 -08:00，
    与全站「北京时间」口径自相矛盾。
    """
    watch_root = tmp_path / "watch-sessions" / "watch-x"
    watch_root.mkdir(parents=True)
    daily_dashboard._publish_watch_aggregate(watch_root, ("fp",), {"days": {}})
    cache = tmp_path / "watch-sessions" / "cache" / "watch-scene-aggregate.json"
    assert cache.is_file(), "缓存文件未写出"
    payload = json.loads(cache.read_text(encoding="utf-8"))
    generated = payload.get("generated_at") or ""
    assert generated.endswith("+08:00"), (
        f"TZ={host_tz} 下 generated_at={generated}，不是北京时间"
    )


