from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from life_logger.local_web import JobStore, create_server


def _auth_headers(base: str, token: str) -> dict[str, str]:
    host = urllib.parse.urlparse(base).netloc
    return {
        "Authorization": f"Bearer {token}",
        "Origin": f"http://{host}",
        "X-Life-Logger-CSRF": "1",
    }


def _request(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    body: bytes = b"",
) -> tuple[int, dict[str, object]]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        response = urllib.request.urlopen(request)
    except urllib.error.HTTPError as error:
        payload = json.loads(error.read())
        return error.code, payload
    return response.status, json.loads(response.read())


def _make_m4a_segment(tmp_path: Path) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required for the Watch-to-Life-Logger integration test")
    source = tmp_path / "watch-segment.m4a"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000:duration=1",
            "-ac",
            "1",
            "-c:a",
            "aac",
            "-b:a",
            "32k",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    return source.read_bytes()


def test_watch_coverage_requires_auth_and_returns_only_interval_metadata(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "web", recover=False)
    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        config_path=tmp_path / "config.json",
        access_token="test-token",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        url = f"{base}/api/watch-coverage"
        status, payload = _request(url, method="GET", headers={})
        assert status == 401
        assert payload["error"] == "unauthorized"

        session_store = server.watch_session_store
        session_store.start_session(
            device_id="test-watch",
            session_id="timeline-session",
            started_at="2026-09-27T23:59:50+08:00",
        )
        session_store.receive_segment(
            device_id="test-watch",
            session_id="timeline-session",
            sequence=0,
            start_offset_ms=0,
            duration_ms=20_000,
            checksum=f"sha256:{hashlib.sha256(b'private-audio').hexdigest()}",
            body=b"private-audio",
        )
        status, payload = _request(
            url, method="GET", headers=_auth_headers(base, "test-token")
        )

        assert status == 200
        assert payload["intervals"] == [
            {
                "device_id": "test-watch",
                "session_id": "timeline-session",
                "sequence": 0,
                "start_at": "2026-09-27T23:59:50+08:00",
                "end_at": "2026-09-28T00:00:10+08:00",
            }
        ]
        # Q773：覆盖接口附带设备失联摘要；没上报过设备状态时 level 必须为空。
        assert payload["device_offline"]["level"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_watch_reception_list_requires_browser_auth(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "web", recover=False)
    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        config_path=tmp_path / "config.json",
        access_token="test-token",
    )
    server.watch_local_access_enabled = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/watch-reception"
        status, _ = _request(url, method="GET", headers={})
        assert status == 401
        body = b"preview-audio"
        session_store = server.watch_session_store
        session_store.start_session(
            device_id="test-watch",
            session_id="preview-session",
            started_at="2026-09-27T10:00:00+08:00",
        )
        session_store.receive_segment(
            device_id="test-watch",
            session_id="preview-session",
            sequence=0,
            start_offset_ms=0,
            duration_ms=1234,
            checksum="sha256:" + hashlib.sha256(body).hexdigest(),
            body=body,
        )
        state_path = (
            session_store.session_directory("test-watch", "preview-session")
            / "live/000000/state.json"
        )
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
                    "status": "no_speech",
                    "updated_at": "2026-09-27T10:00:02+08:00",
                    "rows": [],
                }
            )
        )
        status, payload = _request(
            url, method="GET", headers={"Authorization": "Bearer test-token"}
        )
        assert status == 200
        session = payload["sessions"][0]
        assert "preview" not in session
        assert session["formal_asr"]["status"] == "processing"
        assert session["formal_asr"]["counts"]["pending"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_watch_segment_upload_returns_durable_idempotent_ack(
    tmp_path: Path, monkeypatch
) -> None:
    token = "test-access-token"
    store = JobStore(tmp_path / "local-web", recover=False)
    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        config_path=tmp_path / "local-config.json",
        access_token=token,
    )
    formal_asr_ready = False

    def ready_for_session(_device_id: str, _session_id: str) -> bool:
        return formal_asr_ready

    def materialize_for_job(
        _device_id: str,
        _session_id: str,
        _recording_path: Path,
        target_asr_dir: Path,
        _timezone_name: str,
    ) -> bool:
        target_asr_dir.mkdir(parents=True, exist_ok=True)
        (target_asr_dir / "transcript.json").write_text("[]", encoding="utf-8")
        (target_asr_dir / "recordings.json").write_text("[]", encoding="utf-8")
        (target_asr_dir / "checkpoint.json").write_text(
            json.dumps({"status": "completed", "completed_windows": 1,
                        "total_windows": 1, "progress_unit": "segments", "transcripts": []}),
            encoding="utf-8",
        )
        (target_asr_dir / "run-summary.json").write_text(
            json.dumps({"status": "completed", "transcript_count": 0}),
            encoding="utf-8",
        )
        return True

    server.watch_formal_asr.ready_for_session = ready_for_session
    server.watch_formal_asr.materialize_for_job = materialize_for_job
    dispatched_jobs: list[str] = []
    worker_started = threading.Event()

    def observe_worker_start(
        _store: JobStore,
        job_id: str,
        _job_dir: Path,
        _config_path: Path,
        _secrets_path: Path | None,
    ) -> None:
        dispatched_jobs.append(job_id)
        worker_started.set()

    monkeypatch.setattr(JobStore, "_worker", observe_worker_start)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        auth = _auth_headers(base, token)
        create_status, created = _request(
            f"{base}/api/watch-sessions",
            method="POST",
            headers={**auth, "Content-Type": "application/json"},
            body=json.dumps(
                {
                    "device_id": "watch-s5-test",
                    "session_id": "session-001",
                    "started_at": "2026-09-25T11:00:00+08:00",
                    "codec": "m4a-aac",
                }
            ).encode(),
        )
        assert create_status == 201
        assert created["session"]["state"] == "receiving"

        body = _make_m4a_segment(tmp_path)
        checksum = f"sha256:{hashlib.sha256(body).hexdigest()}"
        segment_url = f"{base}/api/watch-sessions/watch-s5-test/session-001/segments/0"
        segment_headers = {
            **auth,
            "Content-Type": "audio/mp4",
            "Content-Length": str(len(body)),
            "X-LifeLogger-Checksum": checksum,
            "X-LifeLogger-Start-Offset-Ms": "0",
            "X-LifeLogger-Duration-Ms": "1000",
            "X-LifeLogger-Battery-Level-Percent": "76",
            "X-LifeLogger-Battery-State": "unplugged",
        }

        first_status, first = _request(
            segment_url,
            method="PUT",
            headers=segment_headers,
            body=body,
        )
        retry_status, retry = _request(
            segment_url,
            method="PUT",
            headers=segment_headers,
            body=body,
        )

        assert first_status == 201
        assert first["ack"]["sha256"] == checksum
        assert retry_status == 200
        assert retry["duplicate"] is True
        manifest = server.watch_session_store.load_session(
            "watch-s5-test", "session-001"
        )
        assert manifest["segments"][0]["battery_snapshot"] == {
            "level_percent": 76,
            "state": "unplugged",
        }

        complete_status, complete = _request(
            f"{base}/api/watch-sessions/watch-s5-test/session-001/complete",
            method="POST",
            headers={**auth, "Content-Type": "application/json"},
            body=json.dumps(
                {
                    "expected_segments": 1,
                    "ended_at": "2026-09-25T11:00:01+08:00",
                }
            ).encode(),
        )
        assert complete_status == 200
        assert complete["complete"] is True
        assert complete["job_id"]
        reserved_job_id = str(complete["job_id"])
        assert complete["processing"] == "waiting_for_formal_asr"
        assert not worker_started.is_set()
        assert store.read(reserved_job_id)["status"] == "draft"
        assert not (
            server.watch_session_store.session_directory("watch-s5-test", "session-001")
            / "recording.m4a"
        ).exists()

        formal_asr_ready = True
        complete_status, complete = _request(
            f"{base}/api/watch-sessions/watch-s5-test/session-001/complete",
            method="POST",
            headers={**auth, "Content-Type": "application/json"},
            body=json.dumps(
                {
                    "expected_segments": 1,
                    "ended_at": "2026-09-25T11:00:01+08:00",
                }
            ).encode(),
        )
        assert complete_status == 200
        assert complete["job_id"] == reserved_job_id
        assert complete["processing"] == "formal_job_started"
        assert worker_started.wait(timeout=2)
        job = store.read(str(complete["job_id"]))
        assert job["files"][0]["capture_source"] == "apple-watch"
        assert job["files"][0]["source_session"] == "session-001"
        assert job["files"][0]["stages"]["asr"]["source"] == "watch_segment_checkpoints"
        assert dispatched_jobs == [str(complete["job_id"])]
        assembled_recording = (
            server.watch_session_store.session_directory("watch-s5-test", "session-001")
            / "recording.m4a"
        )
        assert assembled_recording.is_file()
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            pytest.skip("ffprobe is required to verify the assembled Watch recording")
        duration = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(assembled_recording),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert 0.8 <= float(duration.stdout.strip()) <= 1.3

        repeated_status, repeated = _request(
            f"{base}/api/watch-sessions/watch-s5-test/session-001/complete",
            method="POST",
            headers={**auth, "Content-Type": "application/json"},
            body=json.dumps(
                {
                    "expected_segments": 1,
                    "ended_at": "2026-09-25T11:00:01+08:00",
                }
            ).encode(),
        )
        assert repeated_status == 200
        assert repeated["job_id"] == complete["job_id"]
        assert repeated["duplicate"] is True
        assert repeated["processing"] == "formal_job_started"
        assert len(store.list()) == 1
        assert dispatched_jobs == [str(complete["job_id"])]

        _, incomplete = _request(
            f"{base}/api/watch-sessions",
            method="POST",
            headers={**auth, "Content-Type": "application/json"},
            body=json.dumps(
                {
                    "device_id": "watch-s5-test",
                    "session_id": "session-incomplete",
                    "started_at": "2026-09-25T12:00:00+08:00",
                }
            ).encode(),
        )
        assert incomplete["session"]["state"] == "receiving"
        missing_status, missing = _request(
            f"{base}/api/watch-sessions/watch-s5-test/session-incomplete/complete",
            method="POST",
            headers={**auth, "Content-Type": "application/json"},
            body=json.dumps(
                {
                    "expected_segments": 1,
                    "ended_at": "2026-09-25T12:01:00+08:00",
                    "auto_start": False,
                }
            ).encode(),
        )
        assert missing_status == 409
        assert missing["missing_sequences"] == [0]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_watch_write_requires_api_auth_and_same_origin_csrf(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        config_path=tmp_path / "local-config.json",
        access_token="test-access-token",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        status, payload = _request(
            f"{base}/api/watch-sessions",
            method="POST",
            headers={"Content-Type": "application/json"},
            body=b'{"device_id":"watch-s5-test","session_id":"session-unauth","started_at":"2026-09-25T11:00:00+08:00"}',
        )
        assert status == 401
        assert payload["error"] == "unauthorized"

        status, payload = _request(
            f"{base}/api/watch-sessions",
            method="POST",
            headers={
                **_auth_headers(base, "test-access-token"),
                "Origin": "http://attacker.invalid",
                "Content-Type": "application/json",
            },
            body=b'{"device_id":"watch-s5-test","session_id":"session-csrf","started_at":"2026-09-25T11:00:00+08:00"}',
        )
        assert status == 403
        assert payload["error"] == "forbidden"

        status, payload = _request(
            f"{base}/api/watch-sessions",
            method="POST",
            headers={
                **_auth_headers(base, "test-access-token"),
                "Origin": f"https://{urllib.parse.urlparse(base).netloc}",
                "Content-Type": "application/json",
            },
            body=b'{"device_id":"watch-s5-test","session_id":"session-https-origin","started_at":"2026-09-25T11:00:00+08:00"}',
        )
        assert status == 201
        assert payload["session"]["state"] == "receiving"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_retried_watch_import_dispatches_existing_unstarted_job(
    tmp_path: Path, monkeypatch
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    session_dir = tmp_path / "watch-session"
    session_dir.mkdir()
    (session_dir / "recording.m4a").write_bytes(b"assembled-watch-audio")
    summary = {
        "complete": True,
        "device_id": "watch-s5-test",
        "session_id": "session-retry-001",
        "recording_file": "recording.m4a",
        "started_at": "2026-09-25T11:00:00+08:00",
    }
    config_path = tmp_path / "local-config.json"

    first = store.import_watch_recording(
        session_dir,
        summary,
        config_path,
        auto_start=False,
    )
    assert first["reused"] is False

    dispatched: list[str] = []

    def record_dispatch(
        job_id: str,
        _config_path: Path,
        *,
        secrets_path: Path | None = None,
    ) -> dict[str, object]:
        dispatched.append(job_id)
        return store.read(job_id)

    monkeypatch.setattr(store, "start_job", record_dispatch)
    retry = store.import_watch_recording(
        session_dir,
        summary,
        config_path,
        auto_start=True,
    )

    assert retry["reused"] is True
    assert retry["job_id"] == first["job_id"]
    assert dispatched == [str(first["job_id"])]


@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
def test_retried_watch_import_does_not_restart_terminal_job(
    tmp_path: Path, monkeypatch, terminal_status: str
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    session_dir = tmp_path / "watch-session"
    session_dir.mkdir()
    (session_dir / "recording.m4a").write_bytes(b"assembled-watch-audio")
    summary = {
        "complete": True,
        "device_id": "watch-s5-test",
        "session_id": "session-terminal-001",
        "recording_file": "recording.m4a",
        "started_at": "2026-09-25T11:00:00+08:00",
    }
    config_path = tmp_path / "local-config.json"
    first = store.import_watch_recording(
        session_dir,
        summary,
        config_path,
        auto_start=False,
    )
    original_list = store.list

    def list_with_terminal_status() -> list[dict[str, object]]:
        jobs = original_list()
        for job in jobs:
            if job["job_id"] == first["job_id"]:
                job["status"] = terminal_status
        return jobs

    dispatched: list[str] = []

    def record_dispatch(
        job_id: str,
        _config_path: Path,
        *,
        secrets_path: Path | None = None,
    ) -> dict[str, object]:
        dispatched.append(job_id)
        return store.read(job_id)

    monkeypatch.setattr(store, "list", list_with_terminal_status)
    monkeypatch.setattr(store, "start_job", record_dispatch)
    retry = store.import_watch_recording(
        session_dir,
        summary,
        config_path,
        auto_start=True,
    )

    assert retry["reused"] is True
    assert retry["job_id"] == first["job_id"]
    assert dispatched == []


def test_concurrent_watch_imports_create_only_one_job(
    tmp_path: Path, monkeypatch
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    session_dir = tmp_path / "watch-session"
    session_dir.mkdir()
    (session_dir / "recording.m4a").write_bytes(b"assembled-watch-audio")
    summary = {
        "complete": True,
        "device_id": "watch-s5-test",
        "session_id": "session-concurrent-001",
        "recording_file": "recording.m4a",
        "started_at": "2026-09-25T11:00:00+08:00",
    }
    config_path = tmp_path / "local-config.json"
    original_list = store.list
    empty_snapshot_barrier = threading.Barrier(2)

    def synchronize_empty_reads() -> list[dict[str, object]]:
        jobs = original_list()
        if not jobs:
            try:
                empty_snapshot_barrier.wait(timeout=0.2)
            except threading.BrokenBarrierError:
                pass
        return jobs

    monkeypatch.setattr(store, "list", synchronize_empty_reads)

    def import_recording() -> dict[str, object]:
        return store.import_watch_recording(
            session_dir,
            summary,
            config_path,
            auto_start=False,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: import_recording(), range(2)))

    assert results[0]["job_id"] == results[1]["job_id"]
    assert len(original_list()) == 1


def test_watch_segment_battery_headers_are_optional_and_validated(
    tmp_path: Path,
) -> None:
    token = "test-access-token"
    store = JobStore(tmp_path / "local-web", recover=False)
    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        config_path=tmp_path / "local-config.json",
        access_token=token,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        auth = _auth_headers(base, token)
        create_status, _ = _request(
            f"{base}/api/watch-sessions",
            method="POST",
            headers={**auth, "Content-Type": "application/json"},
            body=json.dumps(
                {
                    "device_id": "watch-s5-test",
                    "session_id": "battery-validation-session",
                    "started_at": "2026-09-28T10:00:00+08:00",
                    "codec": "m4a-aac",
                }
            ).encode(),
        )
        assert create_status == 201

        body = b"test-audio-segment"
        checksum = f"sha256:{hashlib.sha256(body).hexdigest()}"
        segment_url = (
            f"{base}/api/watch-sessions/watch-s5-test/"
            "battery-validation-session/segments/0"
        )
        base_headers = {
            **auth,
            "Connection": "close",
            "Content-Type": "audio/mp4",
            "Content-Length": str(len(body)),
            "X-LifeLogger-Checksum": checksum,
            "X-LifeLogger-Start-Offset-Ms": "0",
            "X-LifeLogger-Duration-Ms": "1000",
        }
        invalid_battery_headers = [
            {"X-LifeLogger-Battery-Level-Percent": "not-a-number"},
            {"X-LifeLogger-Battery-Level-Percent": "101"},
            {"X-LifeLogger-Battery-State": "unknown"},
        ]
        for battery_headers in invalid_battery_headers:
            status, _ = _request(
                segment_url,
                method="PUT",
                headers={**base_headers, **battery_headers},
                body=body,
            )
            assert status == 400
            manifest = server.watch_session_store.load_session(
                "watch-s5-test", "battery-validation-session"
            )
            assert manifest["segments"] == []

        accepted_status, _ = _request(
            segment_url,
            method="PUT",
            headers=base_headers,
            body=body,
        )
        assert accepted_status == 201
        manifest = server.watch_session_store.load_session(
            "watch-s5-test", "battery-validation-session"
        )
        assert "battery_snapshot" not in manifest["segments"][0]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
