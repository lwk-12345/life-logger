import json
import socket
import threading
import urllib.request
import wave
from datetime import datetime
from pathlib import Path

from life_logger import local_web, passport_stream
from life_logger.local_web import JobStore, create_server
from life_logger.passport_stream import (
    CAPTURE_SOURCE,
    PassportStreamStore,
    crc32_header,
    create_passport_stream_server,
    start_passport_receiver,
    start_receiver_discovery,
)


def _headers(
    *,
    session: str,
    chunk: int,
    body: bytes,
    device: str = "AI-Passport-TEST",
    end: bool = False,
    stream_type: str = "audio",
) -> dict[str, str]:
    return {
        "X-LifeLogger-Session": session,
        "X-LifeLogger-Device": device,
        "X-LifeLogger-Chunk": str(chunk),
        "X-LifeLogger-Checksum": crc32_header(body),
        "X-LifeLogger-Sample-Rate": "16000",
        "X-LifeLogger-Format": "s16le",
        "X-LifeLogger-End": "1" if end else "0",
        "X-LifeLogger-Type": stream_type,
    }


def test_complete_passport_session_preserves_raw_wav_and_imports_normal_job(
    tmp_path: Path,
) -> None:
    local_root = tmp_path / "local-web"
    job_store = JobStore(local_root, recover=False)
    config_path = tmp_path / "local-config.json"
    config_path.write_text("{}", encoding="utf-8")
    imported: list[str] = []

    def import_complete(session_dir: Path, summary: dict[str, object]) -> str | None:
        result = job_store.import_passport_recording(
            session_dir,
            summary,
            config_path,
            auto_start=False,
        )
        imported.append(str(result["job_id"]))
        return str(result["job_id"])

    receiver = PassportStreamStore(
        local_root / "passport-stream",
        on_complete=import_complete,
    )
    first = b"\x00\x00" * 160
    second = b"\x01\x00" * 160

    status, _ = receiver.receive(
        "/v1/audio/chunk",
        _headers(session="sess-complete", chunk=0, body=first),
        first,
    )
    assert status == 200
    status, _ = receiver.receive(
        "/v1/audio/chunk",
        _headers(session="sess-complete", chunk=1, body=second),
        second,
    )
    assert status == 200
    status, ended = receiver.receive(
        "/v1/audio/end",
        _headers(session="sess-complete", chunk=2, body=b"", end=True),
        b"",
    )

    assert status == 200
    summary = ended["summary"]
    assert summary["complete"] is True
    assert summary["missing_chunks"] == []
    assert summary["job_id"] == imported[0]

    session_dir = (
        local_root
        / "passport-stream"
        / "AI-Passport-TEST"
        / "sess-complete"
    )
    assert (session_dir / "chunk-00000000.s16le").read_bytes() == first
    assert (session_dir / "chunk-00000001.s16le").read_bytes() == second
    recording = session_dir / "recording.wav"
    assert recording.is_file()
    with wave.open(str(recording), "rb") as stream:
        assert stream.getnchannels() == 1
        assert stream.getsampwidth() == 2
        assert stream.getframerate() == 16000
        assert stream.getnframes() == 320

    job = job_store.read(imported[0])
    file_state = job["files"][0]
    assert file_state["capture_source"] == CAPTURE_SOURCE
    assert file_state["source_device"] == "AI-Passport-TEST"
    assert file_state["source_session"] == "sess-complete"
    assert file_state["source_transport"] == "lan-http-pcm"
    assert file_state["source_integrity"] == "complete-crc32-verified"
    assert file_state["relative_path"].endswith(
        "AI Passport/AI-Passport-TEST/sess-complete/recording.wav"
    )


def test_missing_passport_chunk_is_reported_and_never_imported(tmp_path: Path) -> None:
    imported: list[str] = []
    receiver = PassportStreamStore(
        tmp_path / "passport-stream",
        on_complete=lambda _path, _summary: imported.append("called") or "job-bad",
    )
    body = b"\x00\x00" * 80
    receiver.receive(
        "/v1/audio/chunk",
        _headers(session="sess-missing", chunk=0, body=body),
        body,
    )

    status, ended = receiver.receive(
        "/v1/audio/end",
        _headers(session="sess-missing", chunk=2, body=b"", end=True),
        b"",
    )

    assert status == 200
    assert ended["summary"]["complete"] is False
    assert ended["summary"]["missing_chunks"] == [1]
    session_dir = tmp_path / "passport-stream" / "AI-Passport-TEST" / "sess-missing"
    assert not (session_dir / "recording.wav").exists()
    assert imported == []


def test_unfinished_passport_session_becomes_stalled_after_inactivity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "passport-stream"
    stale_dir = root / "AI-Passport-TEST" / "sess-stale"
    stale_dir.mkdir(parents=True)
    (stale_dir / "manifest.jsonl").write_text(
        json.dumps(
            {
                "type": "audio",
                "chunk": 0,
                "checksum": "crc32:00000000",
                "received_at": "2026-09-25T01:00:00+08:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    recent_dir = root / "AI-Passport-TEST" / "sess-recent"
    recent_dir.mkdir(parents=True)
    (recent_dir / "manifest.jsonl").write_text(
        json.dumps(
            {
                "type": "audio",
                "chunk": 0,
                "checksum": "crc32:00000000",
                "received_at": "2026-09-25T01:00:45+08:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    sessions = passport_stream.list_passport_sessions(
        root,
        reference_now=datetime.fromisoformat("2026-09-25T01:01:00+08:00"),
        stale_after_seconds=30,
    )
    by_session = {item["session"]: item for item in sessions}

    assert by_session["sess-stale"]["status"] == "stalled"
    assert by_session["sess-stale"]["activity_age_seconds"] == 60.0
    assert by_session["sess-stale"]["complete"] is False
    assert by_session["sess-recent"]["status"] == "receiving"
    assert by_session["sess-recent"]["activity_age_seconds"] == 15.0
    assert by_session["sess-stale"]["missing_chunks"] is None
    assert by_session["sess-stale"]["recording_available"] is False
    assert by_session["sess-stale"]["job_id"] is None
    assert not (stale_dir / "recording.wav").exists()


def test_stalled_passport_session_returns_to_receiving_after_new_chunk(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "passport-stream"
    session_dir = root / "AI-Passport-TEST" / "sess-resume"
    session_dir.mkdir(parents=True)
    (session_dir / "manifest.jsonl").write_text(
        json.dumps(
            {
                "type": "audio",
                "chunk": 0,
                "checksum": "crc32:00000000",
                "received_at": "2026-09-25T01:00:00+08:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    before = passport_stream.list_passport_sessions(
        root,
        reference_now=datetime.fromisoformat("2026-09-25T01:01:00+08:00"),
    )[0]
    assert before["status"] == "stalled"
    assert before["missing_chunks"] is None

    monkeypatch.setattr(
        passport_stream,
        "_iso",
        lambda: "2026-09-25T01:01:05+08:00",
    )
    receiver = PassportStreamStore(root)
    body = b"\x01\x00" * 80
    status, _ = receiver.receive(
        "/v1/audio/chunk",
        _headers(session="sess-resume", chunk=1, body=body),
        body,
    )
    assert status == 200

    after = passport_stream.list_passport_sessions(
        root,
        reference_now=datetime.fromisoformat("2026-09-25T01:01:10+08:00"),
    )[0]
    assert after["status"] == "receiving"
    assert after["activity_age_seconds"] == 5.0
    assert after["missing_chunks"] is None
    assert after["recording_available"] is False
    assert after["job_id"] is None
    assert not (session_dir / "recording.wav").exists()


def test_passport_checksum_mismatch_is_rejected_before_chunk_is_saved(
    tmp_path: Path,
) -> None:
    receiver = PassportStreamStore(tmp_path / "passport-stream")
    body = b"\x00\x00" * 10
    headers = _headers(session="sess-bad-crc", chunk=0, body=body)
    headers["X-LifeLogger-Checksum"] = "crc32:deadbeef"

    status, payload = receiver.receive("/v1/audio/chunk", headers, body)

    assert status == 422
    assert payload["error"] == "checksum mismatch"
    assert not (
        tmp_path
        / "passport-stream"
        / "AI-Passport-TEST"
        / "sess-bad-crc"
        / "chunk-00000000.s16le"
    ).exists()


def test_passport_short_event_uses_firmware_event_headers(tmp_path: Path) -> None:
    receiver = PassportStreamStore(tmp_path / "passport-stream")
    headers = _headers(
        session="sess-event",
        chunk=0,
        body=b"",
        stream_type="event",
    )
    headers["X-LifeLogger-Event"] = "short"
    headers["X-LifeLogger-Event-Time-Ms"] = "12345"

    status, payload = receiver.receive("/v1/audio/chunk", headers, b"")

    assert status == 200
    assert payload["event"] == "short"
    event_path = tmp_path / "passport-stream" / "AI-Passport-TEST" / "events.jsonl"
    event = json.loads(event_path.read_text(encoding="utf-8").splitlines()[0])
    assert event["event_time_ms"] == "12345"
    assert event["capture_source"] == CAPTURE_SOURCE
    sessions = passport_stream.list_passport_sessions(tmp_path / "passport-stream")
    assert sessions[0]["session"] == "sess-event"
    assert sessions[0]["status"] == "event"
    assert sessions[0]["event_count"] == 1
    assert sessions[0]["recording_available"] is False


def test_passport_http_receiver_accepts_chunk_and_end_paths(tmp_path: Path) -> None:
    server = create_passport_stream_server(
        host="127.0.0.1",
        port=0,
        output_root=tmp_path / "passport-stream",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    body = b"\x00\x00" * 40
    try:
        with urllib.request.urlopen(f"{base}/health") as response:
            health = json.loads(response.read())
        assert response.status == 200
        assert health["ok"] is True
        assert health["port"] == server.server_port

        chunk = urllib.request.Request(
            f"{base}/v1/audio/chunk",
            data=body,
            headers=_headers(session="sess-http", chunk=0, body=body),
            method="POST",
        )
        with urllib.request.urlopen(chunk) as response:
            assert response.status == 200

        end = urllib.request.Request(
            f"{base}/v1/audio/end",
            data=b"",
            headers=_headers(session="sess-http", chunk=1, body=b"", end=True),
            method="POST",
        )
        with urllib.request.urlopen(end) as response:
            payload = json.loads(response.read())
        assert payload["summary"]["complete"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_receiver_discovery_protocol_matches_passport_firmware() -> None:
    responder = getattr(passport_stream, "receiver_discovery_response", None)
    assert callable(responder)
    assert responder(b"LL_DISCOVER_V1 0123abcd", 8766) == (
        b"LL_RECEIVER_V1 0123abcd 8766"
    )
    assert responder(b"LL_DISCOVER_V1 invalid", 8766) is None


def test_receiver_udp_discovery_announces_live_http_port() -> None:
    stop, thread, port = start_receiver_discovery(
        host="127.0.0.1", discovery_port=0, receiver_port=8766
    )
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(1)
    try:
        client.sendto(b"LL_DISCOVER_V1 deadbeef", ("127.0.0.1", port))
        response, _ = client.recvfrom(128)
        assert response == b"LL_RECEIVER_V1 deadbeef 8766"
    finally:
        stop.set()
        thread.join(timeout=1)
        client.close()


def test_combined_passport_runtime_serves_health_and_udp_discovery(
    tmp_path: Path,
) -> None:
    runtime = start_passport_receiver(
        host="127.0.0.1",
        port=0,
        discovery_port=0,
        output_root=tmp_path / "passport-stream",
    )
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(1)
    try:
        status = runtime.status()
        assert status["running"] is True
        assert status["http_running"] is True
        assert status["discovery_running"] is True
        assert status["degraded"] is False
        assert status["http_port"] > 0
        assert status["discovery_port"] > 0
        with urllib.request.urlopen(
            f"http://127.0.0.1:{status['http_port']}/health"
        ) as response:
            health = json.loads(response.read())
        assert health["service"] == "LifeLoggerReceiver"
        assert health["version"] == 1
        assert health["port"] == status["http_port"]

        client.sendto(
            b"LL_DISCOVER_V1 cafebabe",
            ("127.0.0.1", status["discovery_port"]),
        )
        payload, _ = client.recvfrom(128)
        assert payload == (
            f"LL_RECEIVER_V1 cafebabe {status['http_port']}".encode("ascii")
        )
        runtime.discovery_stop.set()
        runtime.discovery_thread.join(timeout=1)
        degraded = runtime.status()
        assert degraded["running"] is False
        assert degraded["http_running"] is True
        assert degraded["discovery_running"] is False
        assert degraded["degraded"] is True
        assert degraded["degraded_reason"] == "UDP discovery thread stopped"
    finally:
        client.close()
        runtime.close()
    assert runtime.http_thread.is_alive() is False
    assert runtime.discovery_thread.is_alive() is False


def test_web_companion_receiver_imports_complete_session_as_draft_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    config_path = tmp_path / "local-config.json"
    config_path.write_text(
        json.dumps(
            {
                "passport_stream": {
                    "enabled": True,
                    "host": "127.0.0.1",
                    "port": 8766,
                    "discovery_port": 8767,
                    "auto_start": False,
                }
            }
        ),
        encoding="utf-8",
    )
    real_start = start_passport_receiver

    def start_ephemeral(**kwargs):
        return real_start(
            host="127.0.0.1",
            port=0,
            discovery_port=0,
            output_root=kwargs["output_root"],
            on_complete=kwargs.get("on_complete"),
        )

    monkeypatch.setattr(local_web, "start_passport_receiver", start_ephemeral)
    runtime, status = local_web._start_passport_receiver_for_web(store, config_path)
    assert runtime is not None
    assert status["running"] is True
    assert status["auto_start"] is False
    body = b"\x02\x00" * 120
    base = f"http://127.0.0.1:{status['http_port']}"
    try:
        chunk = urllib.request.Request(
            f"{base}/v1/audio/chunk",
            data=body,
            headers=_headers(session="sess-web", chunk=0, body=body),
            method="POST",
        )
        urllib.request.urlopen(chunk).read()
        end = urllib.request.Request(
            f"{base}/v1/audio/end",
            data=b"",
            headers=_headers(session="sess-web", chunk=1, body=b"", end=True),
            method="POST",
        )
        ended = json.loads(urllib.request.urlopen(end).read())
        assert ended["summary"]["complete"] is True
        assert ended["summary"]["job_id"]
        job = store.read(ended["summary"]["job_id"])
        assert job["status"] == "draft"
        assert job["files"][0]["capture_source"] == CAPTURE_SOURCE
        assert job["files"][0]["source_session"] == "sess-web"
    finally:
        runtime.close()


def test_local_web_lists_passport_sessions_for_ui(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    session_dir = store.root / "passport-stream" / "AI-Passport-TEST" / "sess-ui"
    session_dir.mkdir(parents=True)
    (session_dir / "summary.json").write_text(
        json.dumps(
            {
                "schema_version": "ai-passport-stream-session-v1",
                "capture_source": CAPTURE_SOURCE,
                "device": "AI-Passport-TEST",
                "session": "sess-ui",
                "complete": False,
                "expected_chunks": 3,
                "received_unique_chunks": 2,
                "missing_chunks": [1],
                "unexpected_chunks": [],
                "updated_at": "2026-09-23T05:40:00+08:00",
            }
        ),
        encoding="utf-8",
    )
    server = create_server(host="127.0.0.1", port=0, store=store)
    server.passport_receiver_status = {
        "enabled": True,
        "running": True,
        "http_port": 8766,
        "discovery_port": 8767,
    }

    class _FakeRuntime:
        def status(self):
            return {
                "enabled": True,
                "running": False,
                "http_running": True,
                "discovery_running": False,
                "degraded": True,
                "degraded_reason": "UDP discovery thread stopped",
                "http_port": 8766,
                "discovery_port": 8767,
            }

    server.passport_receiver_runtime = _FakeRuntime()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.loads(
            urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/passport-sessions"
            ).read()
        )
        assert payload["capture_source"] == CAPTURE_SOURCE
        assert payload["receiver"]["running"] is False
        assert payload["receiver"]["http_running"] is True
        assert payload["receiver"]["discovery_running"] is False
        assert payload["receiver"]["degraded_reason"] == "UDP discovery thread stopped"
        assert payload["receiver"]["http_port"] == 8766
        assert payload["receiver"]["discovery_port"] == 8767
        assert payload["sessions"][0]["status"] == "incomplete"
        assert payload["sessions"][0]["missing_chunks"] == [1]
        assert payload["sessions"][0]["device_label"] == "AI Passport"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_complete_passport_session_is_playable_from_local_web(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    session_dir = (
        store.root / "passport-stream" / "AI-Passport-TEST" / "sess-play"
    )
    session_dir.mkdir(parents=True)
    recording = session_dir / "recording.wav"
    with wave.open(str(recording), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\x00\x00" * 80)
    (session_dir / "summary.json").write_text(
        json.dumps(
            {
                "schema_version": "ai-passport-stream-session-v1",
                "capture_source": CAPTURE_SOURCE,
                "device": "AI-Passport-TEST",
                "session": "sess-play",
                "complete": True,
                "expected_chunks": 1,
                "received_unique_chunks": 1,
                "missing_chunks": [],
                "unexpected_chunks": [],
                "recording_path": str(recording),
                "updated_at": "2026-09-23T05:45:00+08:00",
            }
        ),
        encoding="utf-8",
    )
    server = create_server(host="127.0.0.1", port=0, store=store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        sessions = json.loads(
            urllib.request.urlopen(f"{base}/api/passport-sessions").read()
        )["sessions"]
        assert sessions[0]["status"] == "complete"
        assert sessions[0]["recording_available"] is True
        assert sessions[0]["audio_url"].startswith("/api/passport-sessions/audio?")
        with urllib.request.urlopen(base + sessions[0]["audio_url"]) as response:
            assert response.headers["Content-Type"] == "audio/x-wav"
            assert response.read(4) == b"RIFF"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_passport_web_surface_exposes_sessions_source_and_audio_player() -> None:
    project_root = Path(__file__).resolve().parents[1]
    index = (project_root / "web" / "index.html").read_text(encoding="utf-8")
    app = (project_root / "web" / "app.js").read_text(encoding="utf-8")

    assert "AI PASSPORT · LAN" in index
    assert "passport-stream-sessions" in index
    assert "passport-receiver-summary" in index
    assert "TCP <code>:8766</code>" in index
    assert "UDP <code>:8767</code>" in index
    assert "/api/passport-sessions" in app
    assert "Companion 正常" in app
    assert "TCP ${httpPort} · ${httpRunning" in app
    assert "UDP ${discoveryPort} · ${discoveryRunning" in app
    assert "配网页 endpoint 可以留空" in app
    assert 'latest.status === "event"' in app
    assert 'latest.status === "stalled"' in app
    assert "接收停滞 · 未收到结束标记" in app
    assert "recordingSourceLabel" in app
    assert 'capture_source === "ai-passport-wifi"' in app
    assert ("<audio controls" in app) or ("audio_url" in app and "player.src" in app)
