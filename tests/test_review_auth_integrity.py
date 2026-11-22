"""Regression checks for coordinator-owned review counts and HTTP auth boundaries."""
from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

from life_logger import local_web
from life_logger.local_web import JobStore, create_server
from life_logger.pending_review import collect_pending_sources, set_pending_review_state, summarize_for_ui


def test_open_review_counts_decrease_without_rewriting_historical_totals(tmp_path: Path):
    artifact = tmp_path / 'watch-sessions/dev/SESSION/formal-asr/live-context/context-extraction.json'
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps({'pending_sources': [
        {'source_id': 'src_a', 'reason': 'low_confidence', 'quote': '需要确认'},
        {'source_id': 'src_b', 'reason': 'deepseek_batch_failed', 'quote': '整批失败'},
        {'source_id': 'src_c', 'reason': 'invalid_category', 'quote': '抽取异常'},
    ]}))
    original = artifact.read_bytes()
    before = collect_pending_sources(tmp_path)
    assert before['totals']['open_actionable'] == 1
    assert before['totals']['open_extraction_gap'] == 1
    set_pending_review_state(tmp_path, source_id='src_a', status='handled')
    after = collect_pending_sources(tmp_path, include_resolved=True)
    assert after['totals']['needs_review'] == 1  # historical, explicitly distinct
    assert after['totals']['open_actionable'] == 0
    assert after['totals']['scoped_open_actionable'] == 0
    assert summarize_for_ui(after)['actionable'] == 0
    assert after['totals']['open_batch_failure'] == 1
    assert artifact.read_bytes() == original


def test_non_ascii_bearer_get_returns_401_then_valid_get_still_works(tmp_path: Path):
    config = tmp_path / 'local-config.json'
    config.write_text('{}')
    store = JobStore(tmp_path / 'web', recover=False)
    server = create_server(host='127.0.0.1', port=0, store=store, config_path=config, access_token='test-access')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for credential, expected in [('令牌', b'401'), ('test-access', b'200')]:
            with socket.create_connection(('127.0.0.1', server.server_port), timeout=5) as client:
                request = 'GET /api/health HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer '+credential+'\r\nConnection: close\r\n\r\n'
                client.sendall(request.encode('utf-8'))
                response = client.recv(4096)
                assert expected in response.split(b'\r\n', 1)[0], response
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_doctor_cache_invalidates_after_config_change(tmp_path: Path, monkeypatch):
    config = tmp_path / 'local-config.json'
    config.write_text('{}')
    calls = []
    def doctor(*args, **kwargs):
        calls.append(1)
        return {'ok': True, 'checks': []}
    monkeypatch.setattr(local_web, 'run_doctor', doctor)
    server = create_server(host='127.0.0.1', port=0,
                          store=JobStore(tmp_path / 'web', recover=False), config_path=config)
    try:
        assert server.doctor_report()['ok'] is True
        assert server.doctor_report()['ok'] is True
        assert len(calls) == 1
        config.write_text('{"asr_provider":"moss"}')
        assert server.doctor_report()['ok'] is True
        assert len(calls) == 2
    finally:
        server.server_close()
