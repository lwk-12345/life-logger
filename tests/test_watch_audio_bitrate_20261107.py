"""V68 optional bitrate metadata through the real Watch HTTP upload handler."""
import hashlib
from http.client import HTTPConnection
import json
import threading

import pytest

from life_logger.local_web import JobStore, create_server


@pytest.fixture
def watch_api(tmp_path):
    server = create_server(port=0, store=JobStore(tmp_path / 'store', recover=False),
                           config_path=tmp_path / 'config.json', access_token='test-bitrate-only',
                           processing_mode='split')
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()

    def request(method, path, body, headers=None):
        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        try:
            connection.request(method, path, body=body,
                               headers={'Authorization': 'Bearer test-bitrate-only',
                                        'Origin': f'http://127.0.0.1:{server.server_port}',
                                        'X-Life-Logger-CSRF': '1', **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    try:
        status, _ = request('POST', '/api/watch-sessions', json.dumps({
            'device_id': 'watch-bitrate-test', 'session_id': 'session-001',
            'started_at': '2026-11-07T12:00:00+08:00', 'codec': 'm4a-aac',
        }), {'Content-Type': 'application/json'})
        assert status == 201
        yield server, request
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def upload(request, rate=None):
    body = b'test-sealed-audio'
    headers = {'Content-Type': 'audio/mp4',
               'X-LifeLogger-Checksum': 'sha256:' + hashlib.sha256(body).hexdigest(),
               'X-LifeLogger-Start-Offset-Ms': '0', 'X-LifeLogger-Duration-Ms': '1000'}
    if rate is not None:
        headers['X-LifeLogger-Audio-Bitrate'] = str(rate)
    return request('PUT', '/api/watch-sessions/watch-bitrate-test/session-001/segments/0', body, headers)


@pytest.mark.parametrize('rate', [None, '64000', '32378', 'not-a-number', '0', '-1', '1000001'])
def test_optional_bitrate_persists_without_changing_upload_contract(watch_api, rate):
    server, request = watch_api
    status, response = upload(request, rate)
    assert status == 201
    assert response['ok'] and not response['duplicate']
    # Reload a fresh store to prove the field reached the on-disk manifest, not just an ACK.
    from life_logger.watch_stream import WatchSessionStore
    manifest = WatchSessionStore(server.watch_session_store.root).load_session('watch-bitrate-test', 'session-001')
    row = manifest['segments'][0]
    if rate in {'64000', '32378'}:
        assert row['audio_bitrate'] == int(rate)
    else:
        assert 'audio_bitrate' not in row
    assert manifest['sample_rate'] == 16000 and manifest['channels'] == 1
    assert row['duration_ms'] == 1000


def test_retry_preserves_first_measurement_and_old_client_can_omit_it(watch_api):
    server, request = watch_api
    assert upload(request, '32378')[0] == 201
    assert upload(request, '64000')[0] == 200
    assert upload(request)[0] == 200
    row = server.watch_session_store.load_session('watch-bitrate-test', 'session-001')['segments'][0]
    assert row['audio_bitrate'] == 32378


def test_retry_can_fill_previously_missing_measurement(watch_api):
    server, request = watch_api
    assert upload(request)[0] == 201
    assert upload(request, '64000')[0] == 200
    row = server.watch_session_store.load_session('watch-bitrate-test', 'session-001')['segments'][0]
    assert row['audio_bitrate'] == 64000
