from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from life_logger.data_integrity import operational_issues
from life_logger.local_web import JobStore


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def test_receiving_requires_stale_receipts_not_just_old_started_at(tmp_path: Path):
    _write(tmp_path / 'watch-sessions/dev/old/manifest.json', {
        'state': 'receiving', 'started_at': '2026-10-01T00:00:00Z',
        'updated_at': '2026-10-01T01:00:00Z', 'segments': []})
    _write(tmp_path / 'watch-sessions/dev/active/manifest.json', {
        'state': 'receiving', 'started_at': '2026-10-01T00:00:00Z',
        'segments': [{'received_at': '2026-10-17T03:00:00Z'}]})
    issues = operational_issues(tmp_path, now=datetime(2026, 10, 17, 4, tzinfo=timezone.utc))
    assert [(i['kind'], i.get('session_id')) for i in issues] == [('stale_receiving', 'old')]


def test_data_health_surfaces_operational_warning_and_json_error(tmp_path: Path):
    store = JobStore(tmp_path / 'web', recover=False)
    _write(store.root / 'watch-sessions/dev/demo/manifest.json', {
        'state': 'receiving', 'started_at': '2000-01-01T00:00:00Z', 'segments': []})
    report = store.data_health()
    assert report['status'] == 'warning'
    assert report['warning_count'] >= 1
    assert any(i['kind'] == 'stale_receiving' for i in report['issues'])
    (store.root / 'preferences.json').write_text('{broken')
    report = store.data_health()
    assert report['status'] == 'error'
    assert report['error_count'] >= 1
