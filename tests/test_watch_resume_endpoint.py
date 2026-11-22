"""断点续传（第二档）：缺口查询接口 + 重复 PUT 幂等回归。

背景（2026-10-22 P-32）：一次 413 段的会话实测跨两天、约 30 小时才补传完
（`9443BA19` 最终 0..412 全齐）。中间反复全量重传，因为客户端不知道「Mac 已经有哪几段」。
本文件锁三件事：
  1. `GET …/received-segments` 如实返回 received / missing / next_expected / resume_hint；
  2. 同 sequence + 同 sha256 重传必须 200 + duplicate:true，且**不改写文件**；
  3. 鉴权：手表密钥或 access token 可读，无凭证 401。
"""

from __future__ import annotations

import hashlib
import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from life_logger.local_web import JobStore, LocalWebServer, create_server

WATCH_KEY = "test-watch-key"
DEVICE = "watch-test"
SESSION = "SESSION0001"


@pytest.fixture()
def server(tmp_path, monkeypatch):
    for name in ("_watch_live_loop", "_reminder_loop", "_auto_ingest_loop",
                 "_warm_vip_transcriber", "_run_watch_formal_asr_scan"):
        if hasattr(LocalWebServer, name):
            monkeypatch.setattr(LocalWebServer, name, lambda self, *a, **k: None)
    config = tmp_path / "config.json"
    config.write_text('{"hide_dictation_from_dashboard": false}')
    store = JobStore(tmp_path / "web", recover=False)
    srv = create_server(host="127.0.0.1", port=0, store=store, config_path=config, access_token="tok")
    srv.watch_local_access_enabled = True
    srv.watch_upload_key = WATCH_KEY
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv, store
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def _base(srv) -> str:
    return f"http://127.0.0.1:{srv.server_port}"


def _put_segment(base, seq, payload, *, start_ms=0):
    headers = {
        "X-LifeLogger-Watch-Key": WATCH_KEY,
        "X-LifeLogger-Start-Offset-Ms": str(start_ms),
        "X-LifeLogger-Duration-Ms": "60000",
        "X-LifeLogger-Checksum": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "Content-Type": "audio/mp4",
    }
    request = Request(f"{base}/api/watch-sessions/{DEVICE}/{SESSION}/segments/{seq}",
                      data=payload, headers=headers, method="PUT")
    try:
        with urlopen(request, timeout=20) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def _get_gaps(base, *, key=WATCH_KEY, token=None):
    headers = {}
    if key:
        headers["X-LifeLogger-Watch-Key"] = key
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(f"{base}/api/watch-sessions/{DEVICE}/{SESSION}/received-segments",
                      headers=headers)
    try:
        with urlopen(request, timeout=20) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def _start_session(base):
    request = Request(
        f"{base}/api/watch-sessions",
        data=json.dumps({"device_id": DEVICE, "session_id": SESSION,
                         "started_at": "2026-10-22T10:00:00+08:00"}).encode(),
        headers={"X-LifeLogger-Watch-Key": WATCH_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        assert response.status in (200, 201)


def test_gap_report_lists_received_and_missing(server):
    srv, _store = server
    base = _base(srv)
    _start_session(base)
    for seq in (0, 1, 3):
        code, body = _put_segment(base, seq, f"audio-{seq}".encode(), start_ms=seq * 60000)
        assert code in (200, 201), body

    code, report = _get_gaps(base)
    assert code == 200, report
    assert report["received_sequences"] == [0, 1, 3]
    assert report["missing_sequences"] == [2]
    assert report["next_expected_sequence"] == 2
    assert report["gap_count"] == 1
    assert report["resume_hint"]["action"] == "resume_from_gap"
    assert report["resume_hint"]["from_sequence"] == 2
    assert report["state"] == "receiving"


def test_gap_report_on_empty_session_starts_from_zero(server):
    srv, _store = server
    base = _base(srv)
    _start_session(base)
    code, report = _get_gaps(base)
    assert code == 200
    assert report["received_count"] == 0
    assert report["missing_sequences"] == []
    assert report["resume_hint"]["action"] == "start_from_zero"


def test_gap_report_uses_declared_expected_segments_for_tail(server):
    srv, store = server
    base = _base(srv)
    _start_session(base)
    for seq in range(4):
        _put_segment(base, seq, f"audio-{seq}".encode(), start_ms=seq * 60000)
    manifest_path = store.root / "watch-sessions" / DEVICE / SESSION / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["expected_segments"] = 5
    manifest_path.write_text(json.dumps(manifest))

    code, report = _get_gaps(base)
    assert code == 200
    assert report["expected_segments"] == 5
    assert report["missing_sequences"] == [4]
    assert report["resume_hint"]["action"] == "resume_from_gap"

    _put_segment(base, 4, b"audio-4", start_ms=4 * 60000)
    code, report = _get_gaps(base)
    assert report["missing_sequences"] == []
    assert report["resume_hint"]["action"] == "complete"
    assert report["next_expected_sequence"] == 5


def test_duplicate_put_is_idempotent_and_does_not_rewrite(server):
    srv, store = server
    base = _base(srv)
    _start_session(base)
    payload = b"same-audio-bytes"
    code1, body1 = _put_segment(base, 0, payload)
    assert code1 == 201, body1
    assert body1.get("ok") is True

    audio = store.root / "watch-sessions" / DEVICE / SESSION / "segments" / "segment-000000.m4a"
    mtime_before = audio.stat().st_mtime_ns

    code2, body2 = _put_segment(base, 0, payload)
    assert code2 == 200, body2
    assert body2.get("duplicate") is True
    assert body2["ack"]["sequence"] == 0

    assert audio.stat().st_mtime_ns == mtime_before, "重复 PUT 不得二次改写音频文件"
    code, report = _get_gaps(base)
    assert report["received_sequences"] == [0]


def test_gap_report_requires_watch_key_or_token(server):
    srv, _store = server
    base = _base(srv)
    _start_session(base)

    code, _ = _get_gaps(base, key=None)
    assert code == 401
    code, _ = _get_gaps(base, key="wrong-key")
    assert code == 401
    code, _ = _get_gaps(base, key=WATCH_KEY)
    assert code == 200
    code, _ = _get_gaps(base, key=None, token="tok")
    assert code == 200


def test_gap_report_404_for_unknown_session(server):
    srv, _store = server
    base = _base(srv)
    request = Request(
        f"{base}/api/watch-sessions/{DEVICE}/NOSUCHSESSION/received-segments",
        headers={"X-LifeLogger-Watch-Key": WATCH_KEY},
    )
    with pytest.raises(HTTPError) as error:
        urlopen(request, timeout=20)
    assert error.value.code == 404
    payload = json.loads(error.value.read())
    assert payload["error"] == "watch_session_not_found"
